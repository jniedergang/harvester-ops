"""harvester-ops : kubectl pour les outils de bin/ (partagé depuis v1.48.0).

Client minimal par kubectl, sans bibliothèque Kubernetes : les outils tournent
sur un hôte airgap avec la bibliothèque standard seulement. Une erreur ne
recopie jamais la ligne de commande : elle porte le chemin du kubeconfig.
"""

import contextlib
import json
import os
import re
import select
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse


class KubeError(Exception):
    pass



# ---------------------------------------------------------------------------
# kubectl
# ---------------------------------------------------------------------------

class Kube:
    """Client kubectl (transfert de VM, Cluster API)."""

    def __init__(self, kubeconfig, timeout=60):
        self.kubeconfig = kubeconfig
        self.timeout = timeout

    def _base(self):
        return ["kubectl", "--kubeconfig", self.kubeconfig]

    def run(self, *args, input=None, timeout=None):
        t = timeout or self.timeout
        try:
            p = subprocess.run(self._base() + list(args), input=input, capture_output=True,
                               text=True, timeout=t)
        except subprocess.TimeoutExpired:
            # jamais la ligne de commande : elle porte le chemin du kubeconfig
            raise KubeError(f"kubectl {' '.join(str(a) for a in args[:2])} timed out after {t} s") \
                from None
        if p.returncode != 0:
            raise KubeError((p.stderr or p.stdout or "kubectl failed").strip()[:500])
        return p.stdout

    @staticmethod
    def _ns(ns):
        return ["-n", ns] if ns else []

    def get(self, kind, ns, name):
        try:
            return json.loads(self.run("get", kind, name, *self._ns(ns), "-o", "json"))
        except KubeError as e:
            if "NotFound" in str(e) or "not found" in str(e):
                return None
            raise

    def list(self, kind, ns=None, selector=None):
        args = ["get", kind, "-o", "json"]
        args += self._ns(ns) if ns else ["-A"]
        if selector:
            args += ["-l", selector]
        try:
            return json.loads(self.run(*args)).get("items", [])
        except KubeError as e:
            if "the server doesn't have a resource type" in str(e):
                return []
            raise

    def create(self, obj):
        return json.loads(self.run("create", "-f", "-", "-o", "json", input=json.dumps(obj)))

    def replace(self, obj):
        return json.loads(self.run("replace", "-f", "-", "-o", "json", input=json.dumps(obj)))

    def patch(self, kind, ns, name, patch):
        self.run("patch", kind, name, *self._ns(ns), "--type", "merge", "-p", json.dumps(patch))

    def apply(self, docs, field_manager="harvester-ops", timeout=None):
        """Application côté serveur d'une liste d'objets (JSON). Rend la
        sortie de kubectl, une ligne par objet."""
        body = json.dumps({"apiVersion": "v1", "kind": "List", "items": list(docs)})
        return self.run("apply", "--server-side", "--force-conflicts",
                        f"--field-manager={field_manager}", "-f", "-",
                        input=body, timeout=timeout)

    def delete(self, kind, ns, name, cascade=None):
        args = ["delete", kind, name, *self._ns(ns), "--wait=false"]
        if cascade:
            args.append(f"--cascade={cascade}")
        self.run(*args)

    def raw_stream(self, path):
        p = subprocess.Popen(self._base() + ["get", "--raw", path], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        try:
            while True:
                chunk = p.stdout.read(1 << 20)
                if not chunk:
                    break
                yield chunk
            if p.wait() != 0:
                raise KubeError((p.stderr.read() or b"").decode(errors="replace").strip()[:300]
                                or "download failed")
        finally:
            if p.poll() is None:
                p.kill()
            p.stdout.close()
            p.stderr.close()

    @contextlib.contextmanager
    def port_forward(self, ns, target, port, timeout=20):
        """Un port local vers `target` (svc/nom) : rend le port choisi par
        kubectl, et coupe le relais en sortie.

        Tube binaire, lu par `select`/`os.read` : `readline()` sur le tube
        texte tamponné pouvait bloquer bien après `timeout` si la ligne de
        kubectl arrivait en plusieurs morceaux (le numéro de port sans son
        saut de ligne, par exemple)."""
        p = subprocess.Popen(self._base() + ["port-forward", "-n", ns, target, f":{port}"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.time() + timeout
            local, buf, fd = None, b"", p.stdout.fileno()
            while local is None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                ready, _, _ = select.select([fd], [], [], min(1, remaining))
                if ready:
                    chunk = os.read(fd, 4096)
                    if not chunk:
                        break                   # kubectl a refermé sa sortie : il quitte
                    buf += chunk
                    m = re.search(rb"127\.0\.0\.1:(\d+)", buf)
                    if m:
                        local = int(m.group(1))
                if local is None and p.poll() is not None:
                    break
            if local is None:
                if p.poll() is not None:
                    msg = (p.stderr.read() or b"").decode(errors="replace").strip()
                else:
                    msg = "no port"
                raise KubeError(("port-forward failed: " + msg)[:300])
            yield local
        finally:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
            p.stdout.close()
            p.stderr.close()

    def server_host(self):
        try:
            url = self.run("config", "view", "--minify", "-o",
                           "jsonpath={.clusters[0].cluster.server}")
            return urlparse(url.strip()).hostname
        except (KubeError, subprocess.SubprocessError):
            return None


def _read_yaml(path):
    """Document YAML lu avec yq, sinon PyYAML s'il est là ; None s'il est
    illisible."""
    try:
        out = subprocess.run(["yq", "-o=json", ".", str(path)], capture_output=True, text=True,
                             timeout=20)
        if out.returncode == 0:
            return json.loads(out.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        import yaml
        return yaml.safe_load(Path(path).read_text())
    except Exception:                      # noqa: BLE001
        return None


def cluster_config(name):
    """L'entrée d'un cluster dans la configuration de la console
    (`HARVESTER_OPS_CONFIG`, lue avec yq, sinon PyYAML s'il est là), puis
    v1.78.0 parmi les clusters déclarés par la console elle-même
    (`<HARVESTER_OPS_STATE_DIR>/clusters.d/<nom>.yaml`, chemins relatifs au
    répertoire d'état). config.yaml l'emporte à nom égal."""
    import cluster_decl
    cfg = os.environ.get("HARVESTER_OPS_CONFIG", "/etc/harvester-ops/config.yaml")
    data = _read_yaml(cfg) if Path(cfg).is_file() else None
    for c in (data or {}).get("clusters") or []:
        if isinstance(c, dict) and c.get("name") == name:
            return c
    state = cluster_decl.state_dir()
    path = cluster_decl.decl_path(state, name)
    if path is not None and path.is_file():
        doc = _read_yaml(path)
        if cluster_decl.check_decl(doc, path) is None:
            return cluster_decl.resolve(doc, state)
    raise SystemExit(f"unknown cluster: {name} (not in {cfg} nor in {state / cluster_decl.DECL_DIR})")
