"""harvester-ops : kubectl pour les outils de bin/ (partagé depuis v1.48.0).

Client minimal par kubectl, sans bibliothèque Kubernetes : les outils tournent
sur un hôte airgap avec la bibliothèque standard seulement. Une erreur ne
recopie jamais la ligne de commande : elle porte le chemin du kubeconfig.
"""

import json
import os
import subprocess
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

    def server_host(self):
        try:
            url = self.run("config", "view", "--minify", "-o",
                           "jsonpath={.clusters[0].cluster.server}")
            return urlparse(url.strip()).hostname
        except (KubeError, subprocess.SubprocessError):
            return None


def cluster_config(name):
    """L'entrée d'un cluster dans la configuration de la console
    (`HARVESTER_OPS_CONFIG`, lue avec yq, sinon PyYAML s'il est là)."""
    cfg = os.environ.get("HARVESTER_OPS_CONFIG", "/etc/harvester-ops/config.yaml")
    data = None
    try:
        out = subprocess.run(["yq", "-o=json", ".", cfg], capture_output=True, text=True,
                             timeout=20)
        if out.returncode == 0:
            data = json.loads(out.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        data = None
    if data is None:
        try:
            import yaml
            data = yaml.safe_load(Path(cfg).read_text())
        except Exception:                      # noqa: BLE001
            data = None
    for c in (data or {}).get("clusters") or []:
        if c.get("name") == name:
            return c
    raise SystemExit(f"unknown cluster: {name} (not in {cfg})")
