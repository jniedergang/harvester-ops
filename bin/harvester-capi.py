#!/usr/bin/env python3
"""Clusters RKE2 sur Harvester par Cluster API : la pile, puis les clusters.

    harvester-capi status    --cluster C [--components DIR] [--json]
    harvester-capi install   --cluster C --components DIR [--push-images]
    harvester-capi cleanup-legacy --cluster C [--dry-run]
    harvester-capi uninstall --cluster C [--dry-run] [--remove-images] [--timeout 600] [--json] [--id ID]
    harvester-capi inventory --cluster C [--components DIR] --json
    harvester-capi check     --cluster C --spec FICHIER [--components DIR] [--identity I] [--many] [--json]
    harvester-capi render    --cluster C --spec FICHIER [--show-secrets]
    harvester-capi create    --cluster C --spec FICHIER [--components DIR] [--timeout S]
                             [--identity restricted|console] [--origin plan=P,template=M,run=ID]
    harvester-capi delete    --cluster C --name ns/nom [--timeout S]
    harvester-capi state     --cluster C --name ns/nom [--json]
    harvester-capi manifest-check  --cluster C --spec FICHIER
    harvester-capi manifest-deploy --cluster C --spec FICHIER [--timeout S]
    harvester-capi manifest-remove --cluster C --name ns/cluster/élément
    harvester-capi manifests       --cluster C [--json]
    harvester-capi policy-render   --spec FICHIER
    harvester-capi token     --cluster C --name ns/cluster --account ns/compte [--seconds N] [--by P]

Le cluster Harvester sert lui-même de cluster de gestion : Harvester 1.9
embarque Rancher Turtles et son cœur Cluster API. `install` y déclare les
fournisseurs RKE2 et CAPHV du paquet (`--components` : un paquet extrait, ou
le fichier .tar.gz), sans rien descendre d'Internet.

Une demande (`--spec`, JSON, `-` pour l'entrée standard) porte les options
de bin/lib/capi_cluster.py ; les manifestes sont produits par
`caphv-generate` (livré à côté, dépôt CAPHV) puis corrigés. `--cluster`
nomme un cluster de la configuration (`HARVESTER_OPS_CONFIG`), ou
`--kubeconfig` un fichier.

2.5.0 : chaque nouveau cluster reçoit sa propre identité CAPHV restreinte
(`--identity restricted`, par défaut ; bin/lib/capi_identity.py), créée
avant les manifestes et retirée APRÈS le cluster à la suppression (CAPHV en
a besoin pour supprimer VMs, volumes, répartiteur et pool). `console` garde
le kubeconfig de la console, pour un diagnostic.

`state` (2.5.0) lit un Cluster en une requête : présence, origine posée
par `create --origin`, état (capi_cluster.cluster_state, compteurs du
statut v1beta2). Code 1 s'il est absent, avec `{"present": false}`.

2.7.0 : les éléments `manifest` et `policy` d'un paquet, et le socle de
politiques et d'accès de la console (bin/lib/child_policies.py), sont
livrés aux clusters enfants par ClusterResourceSet (bin/lib/capi_manifests.py),
retrait suivi par la console ; `token` émet un jeton court (TokenRequest)
d'un compte d'automatisation, écrit sur la sortie standard seulement. Le
cluster enfant est atteint par son Secret `<cluster>-kubeconfig`, lu avec
l'identité passée par `--kubeconfig` (bin/lib/child_access.py).

Progression sur stderr au format STEP_EVENT|<étape>|<statut>|<message>, lue
par la console. Codes de sortie : 0 succès, 1 échec, 2 refus du contrôle
préalable, 3 annulation.

Bibliothèque standard seulement (plus kubectl et yq) : l'outil tourne sur un
hôte airgap.

Voir docs/design/2026-09-25-creation-cluster-capi.md.
"""

import argparse
import base64
import contextlib
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import capi_cluster as cc  # noqa: E402
import capi_manifests as mf  # noqa: E402
import child_access as ca  # noqa: E402
import child_policies as cp  # noqa: E402
import capi_identity as ci  # noqa: E402
import capi_stack as cs  # noqa: E402
import capi_services as sv  # noqa: E402
import longhorn_room as lr  # noqa: E402
import node_images  # noqa: E402
import registries as rg  # noqa: E402
import work_dir  # noqa: E402
from kube import Kube, KubeError, cluster_config, is_missing_type  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_BLOCKED, EXIT_CANCELLED = 0, 1, 2, 3
GENERATOR = Path(os.environ.get("HARVESTER_OPS_CAPHV_GEN", str(HERE / "caphv-generate")))


class Cancelled(BaseException):
    # v1.99.0 : BaseException, comme le Cancelled de la réplication. Un
    # signal levé dans une lecture gardée par `except Exception` (configuration,
    # relance d'une API, nettoyage) y était avalé : l'outil continuait comme
    # si de rien n'était, ou concluait à une erreur au lieu d'une annulation.
    pass


def step(sid, status, msg=""):
    clean = " ".join(str(msg).split())
    sys.stderr.write(f"STEP_EVENT|{sid}|{status}|{clean}\n")
    sys.stderr.flush()


def _on_signal(signum, frame):
    raise Cancelled(f"signal {signum}")


# ---------------------------------------------------------------------------
# Entrées
# ---------------------------------------------------------------------------

def kube_from(args):
    """`--kubeconfig` l'emporte (la console passe celui qu'elle a préparé,
    à l'identité de l'exploitant) ; `--cluster` donne aussi les nœuds et
    l'accès SSH de la configuration."""
    entry = cluster_config(args.cluster) if args.cluster else None
    kc = args.kubeconfig or (entry or {}).get("kubeconfig")
    if not kc:
        raise SystemExit("give --cluster (with a kubeconfig in the configuration) or --kubeconfig")
    return Kube(kc), kc, entry


def components_from(path):
    """Un paquet extrait, ou son .tar.gz : seuls `turtles.json`, les
    composants et les images de ces fournisseurs en sont extraits (le paquet
    porte aussi les images de l'installation d'avant Turtles), dans
    l'espace de travail (bin/lib/work_dir.py, jamais /tmp)."""
    if not path:
        return None
    p = Path(path)
    if p.is_file():
        p = _extract_turtles(p, work_dir.mkdtemp("hops-capi-"))
    if not (p / "turtles.json").is_file():
        raise SystemExit("this bundle has no turtles.json (built before harvester-ops 1.48)")
    return cs.load_components(p)


def _extract_turtles(archive, work):
    with tarfile.open(archive, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.isfile()
                   and ".." not in Path(m.name).parts and not m.name.startswith("/")]
        top = {Path(m.name).parts[0] for m in members if len(Path(m.name).parts) > 1}
        prefix = Path(top.pop()) if len(top) == 1 else Path()

        def rel(m):
            return Path(m.name).relative_to(prefix) if prefix != Path() else Path(m.name)
        desc = next((m for m in members if str(rel(m)) == "turtles.json"), None)
        if desc is None:
            return work
        wanted = {"turtles.json"}
        index = json.load(tar.extractfile(desc))
        for prov in index.get("providers") or []:
            wanted |= set(prov.get("image_files") or [])
        for m in members:
            r = rel(m)
            if str(r) in wanted or r.parts[:1] == ("turtles",):
                out = work / r
                out.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(m) as src, open(out, "wb") as f:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
    return work


def spec_from(path):
    raw = json.load(sys.stdin) if path == "-" else json.loads(Path(path).read_text())
    return cc.normalize(raw)


# ---------------------------------------------------------------------------
# Relevés
# ---------------------------------------------------------------------------

def _ref(o, name=None):
    md = o.get("metadata") or {}
    return f"{md.get('namespace')}/{name or md.get('name')}"


def _image_info(img):
    spec, st = img.get("spec") or {}, img.get("status") or {}
    display = spec.get("displayName") or img["metadata"]["name"]
    url = (spec.get("url") or "").lower()
    iso = display.lower().endswith(".iso") or url.endswith(".iso")
    imported = any(c.get("type") == "Imported" and c.get("status") == "True"
                   for c in st.get("conditions") or [])
    ready = imported or st.get("progress") == 100
    lower = display.lower()
    hint = next((os_ for key, os_ in (("opensuse", "opensuse"), ("leap", "opensuse"),
                                      ("tumbleweed", "opensuse"), ("sles", "sles"),
                                      ("suse", "sles"), ("rocky", "rocky"),
                                      ("rhel", "rhel"), ("ubuntu", "ubuntu"), ("debian", "debian"))
                 if key in lower), None)
    # Référence par le nom de l'objet, pas le nom affiché : CAPHV accepte
    # les deux, mais un nom affiché peut porter des espaces (« Rocky Linux 8
    # GenericCloud » sur harv1) et deux images peuvent en partager un.
    return {"ref": _ref(img), "name": img["metadata"]["name"], "display_name": display,
            "ready": bool(ready), "iso": iso, "size": st.get("size"),
            "virtual_size": st.get("virtualSize"), "os": hint,
            # 2.5.0 : la classe que Harvester a créée pour l'image (`lh-<uuid>`
            # pour les récentes) : jamais un nom fabriqué
            "storage_class": st.get("storageClassName")}


# Proposée en premier : l'image dont le système a été éprouvé avec RKE2 ici.
OS_RANK = ("sles", "opensuse", "ubuntu", "rocky", "rhel", "debian")


def _image_rank(i):
    os_ = OS_RANK.index(i["os"]) if i.get("os") in OS_RANK else len(OS_RANK)
    return (i["iso"], not i["ready"], os_, i["display_name"].lower())


def _image_facts(images):
    """Une demande peut nommer l'image par son objet (ce que propose la
    console) ou par son nom affiché (ce qu'écrit un script à la main)."""
    out = {}
    for i in images:
        info = {"ready": i["ready"], "iso": i["iso"], "os": i.get("os"),
                "storage_class": i.get("storage_class"), "virtual_size": i.get("virtual_size")}
        ns = i["ref"].split("/", 1)[0]
        out.setdefault(f"{ns}/{i['display_name']}", info)
        out[i["ref"]] = info
    return out


SSH_USERS = {"sles": "sles", "opensuse": "opensuse", "rocky": "rocky", "rhel": "cloud-user",
             "ubuntu": "ubuntu", "debian": "debian"}


def _free_capacity(kube):
    """Place restante sur les nœuds : allouable moins les demandes des pods
    (les VMs y comptent par leur pod virt-launcher)."""
    cpu = mem = 0.0
    try:
        for n in kube.list("nodes"):
            alloc = (n.get("status") or {}).get("allocatable") or {}
            cpu += _cpu(alloc.get("cpu"))
            mem += _mem(alloc.get("memory"))
        for p in kube.list("pods"):
            if (p.get("status") or {}).get("phase") in ("Succeeded", "Failed"):
                continue
            for c in (p.get("spec") or {}).get("containers") or []:
                req = (c.get("resources") or {}).get("requests") or {}
                cpu -= _cpu(req.get("cpu"))
                mem -= _mem(req.get("memory"))
    except KubeError:
        return None, None
    return max(0, int(cpu * 1000)), max(0, int(mem))


def _overcommit(kube):
    """Le surengagement de Harvester (réglage `overcommit-config`)."""
    try:
        s = kube.get("settings.harvesterhci.io", None, "overcommit-config") or {}
        return json.loads(s.get("value") or s.get("default") or "{}")
    except (KubeError, ValueError):
        return {}


def _cpu(v):
    if not v:
        return 0.0
    v = str(v)
    return float(v[:-1]) / 1000 if v.endswith("m") else float(v)


def _mem(v):
    if not v:
        return 0.0
    units = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4,
             "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}
    v = str(v)
    for u, f in units.items():
        if v.endswith(u):
            return float(v[:-len(u)]) * f
    return float(v)


def _reachable(kube):
    try:
        kube.run("get", "--raw", "/version", timeout=10)
        return True
    except KubeError:
        return False


EMPTY_INVENTORY = {"namespaces": [], "clusters": [], "images": [], "keypairs": [],
                   "networks": [], "storage_classes": [], "pools": []}


def inventory(kube, desc):
    versions = [v["version"] if isinstance(v, dict) else v
                for v in (desc or {}).get("kubernetes_versions") or []]
    base = {"versions": (desc or {}).get("kubernetes_versions") or [],
            "version_names": versions,
            "default_version": (desc or {}).get("default_kubernetes_version")
            or (versions[0] if versions else None),
            "ssh_users": SSH_USERS}
    if not _reachable(kube):
        # un cluster éteint ou injoignable : des listes vides et une raison,
        # jamais le bruit de kubectl
        return dict(EMPTY_INVENTORY, **base, unreachable=True, stack=None,
                    free_cpu_m=None, free_memory=None, overcommit={})
    wanted = (desc or {}).get("providers") or []
    stack = cs.stack_status(kube, wanted)
    images = [_image_info(i) for i in kube.list("virtualmachineimages.harvesterhci.io")]
    pools = [cc.pool_info(p) for p in kube.list("ippools.loadbalancer.harvesterhci.io")]
    nets = []
    for n in kube.list("network-attachment-definitions.k8s.cni.cncf.io"):
        try:
            conf = json.loads((n.get("spec") or {}).get("config") or "{}")
        except ValueError:
            conf = {}
        labels = (n.get("metadata") or {}).get("labels") or {}
        nets.append({"ref": _ref(n), "vlan": conf.get("vlan"),
                     "cluster_network": labels.get("network.harvesterhci.io/clusternetwork"),
                     "type": labels.get("network.harvesterhci.io/type")})
    scs, replicas = [], {}
    for s in kube.list("storageclasses"):
        ann = (s.get("metadata") or {}).get("annotations") or {}
        name = s["metadata"]["name"]
        if s.get("provisioner") == "driver.longhorn.io":
            replicas[name] = _replicas(s)
        if name.startswith(("longhorn-image-", "lh-")):
            continue                              # classes propres à une image
        scs.append({"name": name, "replicas": replicas.get(name),
                    "default": ann.get("storageclass.kubernetes.io/is-default-class") == "true"})
    try:
        lh_nodes, over, minimal = lr.read_numbers(kube)
        longhorn = {"nodes": lh_nodes, "over": over, "minimal": minimal}
    except KubeError:
        longhorn = None
    free_cpu, free_mem = _free_capacity(kube)
    capi_clusters = kube.list(cs.K_CLUSTER)
    return dict(base, **{
        "unreachable": False,
        "stack": stack,
        "namespaces": sorted(n["metadata"]["name"] for n in kube.list("namespaces")),
        "clusters": sorted(_ref(c) for c in capi_clusters),
        # 2.7.0 (m7) : HarvesterCluster restés d'un cluster supprimé
        "cluster_uids": {_ref(c): (c.get("metadata") or {}).get("uid") for c in capi_clusters},
        "harvester_clusters": {_ref(h): cc.harvester_cluster_brief(h) for h in kube.list(cc.K_HARVESTERCLUSTER)},
        "images": sorted(images, key=_image_rank),
        "keypairs": sorted(_ref(k) for k in kube.list("keypairs.harvesterhci.io")),
        "networks": sorted(nets, key=lambda n: n["ref"]),
        "storage_classes": scs, "pools": pools, "class_replicas": replicas, "longhorn": longhorn,
        "free_cpu_m": free_cpu, "free_memory": free_mem, "overcommit": _overcommit(kube),
    })


def _replicas(sc):
    """Répliques d'une classe Longhorn (3 par défaut, comme longhorn_room)."""
    try:
        n = int(((sc.get("parameters") or {}).get("numberOfReplicas")) or 3)
    except (TypeError, ValueError):
        n = 3
    return max(1, n)


def facts_from(inv):
    return {
        "stack": inv["stack"], "namespaces": inv["namespaces"], "clusters": inv["clusters"],
        "images": _image_facts(inv["images"]),
        "keypairs": inv["keypairs"], "networks": [n["ref"] for n in inv["networks"]],
        "storage_classes": [s["name"] for s in inv["storage_classes"]],
        "pools": {p["name"]: p for p in inv["pools"]},
        # les versions éprouvées : une version listée mais jamais créée pour
        # de vrai mérite l'avertissement autant qu'une version inconnue
        "versions": [v["version"] for v in inv["versions"]
                     if isinstance(v, dict) and v.get("tested")] or inv["version_names"],
        "free_cpu_m": inv["free_cpu_m"], "free_memory": inv["free_memory"],
        "overcommit": inv["overcommit"],
        "class_replicas": inv.get("class_replicas") or {}, "longhorn": inv.get("longhorn"),
        "cluster_uids": inv.get("cluster_uids") or {}, "harvester_clusters": inv.get("harvester_clusters") or {},
    }


def amount(spec, overcommit=None):
    vms = spec["cp_replicas"] + spec["worker_replicas"]
    disk = cc._bytes(spec["disk_size"]) + cc._bytes(spec.get("extra_disk_size") or "0Gi")
    req_cpu, req_mem = cc.requests_needed(spec, overcommit)
    return {"vms": vms, "cpu": vms * spec["cpu"], "memory": vms * cc._bytes(spec["memory"]),
            "disk": vms * disk, "addresses": cc.addresses_needed(spec),
            "requests_cpu_m": req_cpu, "requests_memory": req_mem}


# ---------------------------------------------------------------------------
# Manifestes
# ---------------------------------------------------------------------------

def render(spec, identity_kubeconfig, origin=None, registries=None):
    """Les documents du cluster, corrigés (liste de dict) ; `registries` :
    configuration de registres de RKE2 (`registry_config`)."""
    argv = ["bash", str(GENERATOR)] + cc.to_argv(spec, identity_kubeconfig)
    p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        # le générateur n'écrit jamais le contenu du kubeconfig sur stderr
        raise RuntimeError("caphv-generate failed: " + (p.stderr or p.stdout).strip()[:300])
    y = subprocess.run(["yq", "-o=json", "-I=0", "."], input=p.stdout, capture_output=True,
                       text=True, timeout=60)
    if y.returncode != 0:
        raise RuntimeError("cannot parse the generated manifests: " + y.stderr.strip()[:200])
    docs = [json.loads(line) for line in y.stdout.splitlines() if line.strip() not in ("", "null")]
    out = cc.postprocess(docs, spec, origin, registries)
    for d in out:
        if d.get("kind") == "Namespace":
            d.setdefault("metadata", {}).setdefault("labels", {})[cs.MANAGED] = "true"
    return out


def as_yaml(docs):
    body = "\n".join(json.dumps(d) for d in docs)
    y = subprocess.run(["yq", "-P", "-p=json", "."], input=body, capture_output=True,
                       text=True, timeout=60)
    if y.returncode != 0:
        return body
    return y.stdout


# ---------------------------------------------------------------------------
# Suivi
# ---------------------------------------------------------------------------

def _pct(state):
    total = sum(state["cp"][1:]) + sum(state["workers"][1:])
    running = state["cp"][0] + state["workers"][0]
    p = 10 if state["infrastructure"] else 0
    p += 30 if state["control_plane"] else 0
    if total:
        p += int(60 * running / total)
    return min(100 if state["ready"] else 99, p)


def wait_ready(kube, ns, name, timeout, now=time.time, sleep=time.sleep):
    deadline = now() + timeout
    last = None
    while True:
        cl = kube.get(cc_kind(), ns, name)
        if cl is None:
            raise RuntimeError(f"cluster {ns}/{name} disappeared")
        machines = kube.list("machines.cluster.x-k8s.io", ns)
        machines = [m for m in machines
                    if ((m.get("metadata") or {}).get("labels") or {})
                    .get("cluster.x-k8s.io/cluster-name") == name]
        st = cc.cluster_state(cl, machines)
        msg = (f"infrastructure {'ready' if st['infrastructure'] else 'pending'}, "
               f"control plane {st['cp'][0]}/{st['cp'][1]}, "
               f"workers {st['workers'][0]}/{st['workers'][1]} ({_pct(st)}%)")
        if msg != last:
            step("provision", "running", msg)
            last = msg
        if st["ready"]:
            return st
        if st["failed"]:
            raise RuntimeError(f"cluster failed: {st['message'] or 'phase Failed'}")
        if now() > deadline:
            raise TimeoutError(st["message"] or msg)
        sleep(15)


def cc_kind():
    return cs.K_CLUSTER


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------

def cmd_status(args):
    kube, _, _ = kube_from(args)
    desc = components_from(args.components)
    st = cs.stack_status(kube, (desc or {}).get("providers") or [])
    if args.json:
        print(json.dumps(st))
    else:
        print(f"Harvester {st['harvester_version'] or '?'}; Turtles: {'yes' if st['turtles'] else 'no'}; "
              f"core: {(st['core'] or {}).get('version') or '-'}")
        for p in st["providers"]:
            print(f"  {p['type']}/{p['name']} {p['version']} {p['phase']}")
        print("ready" if st["ready"] else "missing: " + ", ".join(st["missing"]))
        if st["legacy"]:
            print("legacy installation: " + ", ".join(st["legacy_namespaces"]))
    return EXIT_OK if st["ready"] else EXIT_FAIL


def cmd_install(args):
    if _refuse_during_uninstall(args, "providers"):
        return EXIT_BLOCKED
    kube, _, entry = kube_from(args)
    desc = components_from(args.components)
    if desc is None:
        raise SystemExit("--components is required")
    if args.push_images:
        push_images(entry, desc)
    try:
        cs.install(kube, desc, step, wait=not args.no_wait, timeout=args.timeout, pushed=args.push_images)
    except (RuntimeError, KubeError) as e:
        step("providers", "error", str(e)[:300])
        return EXIT_FAIL
    return EXIT_OK


def push_images(entry, desc):
    """Airgap : chaque image du paquet importée dans le containerd RKE2 de
    chaque nœud, par SSH (comme l'installation d'avant Turtles ;
    bin/lib/node_images.py depuis la 2.6.0)."""
    if not entry:
        raise SystemExit("--push-images needs --cluster (nodes and SSH come from the configuration)")
    files = [f for p in desc["providers"] for f in p.get("image_files") or []]
    node_images.import_on_nodes(entry, files, step)


def cmd_cleanup(args):
    kube, _, _ = kube_from(args)
    try:
        cs.cleanup_legacy(kube, step, dry_run=args.dry_run)
    except (RuntimeError, KubeError) as e:
        step("legacy", "error", str(e)[:300])
        return EXIT_FAIL
    return EXIT_OK


def deploy_holder(harvester):
    """{"run", "plan"} du déroulé de plan qui tient le cluster Harvester
    `harvester` (verrou `<état>/deploy.d/<cluster>.lock` de la console,
    pris par flock), ou None. Le verrou est essayé sans attendre et rendu
    aussitôt ; un fichier absent ou libre = personne."""
    import fcntl
    import re
    # même forme de nom que le verrou de la console (_DEPLOY_LOCK_NAME_RE)
    if not harvester or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$", harvester) or ".." in harvester:
        return None
    path = Path(_state()) / "deploy.d" / f"{harvester}.lock"
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return None
        except OSError:
            note = os.pread(fd, 512, 0).decode(errors="replace").strip()
        try:
            holder = json.loads(note or "{}")
        except ValueError:
            holder = {}
        return holder if isinstance(holder, dict) and holder else {"run": "?", "plan": "?"}
    finally:
        os.close(fd)


UNINSTALL_ACTION = "capi-uninstall"


def hold_cluster_lock(harvester, note):
    """(descripteur, None) : verrou du cluster (le même que les déroulés de
    la console, `<état>/deploy.d/<cluster>.lock`) pris et `note` écrite ;
    (None, {"run", "plan", ...} du détenteur) s'il est tenu ; (None, None)
    sans nom de cluster valide (rien à verrouiller). Rendu par la fermeture
    du descripteur ou la mort du processus."""
    import fcntl
    import re
    if not harvester or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$", harvester) or ".." in harvester:
        return None, None
    d = Path(_state()) / "deploy.d"
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    fd = os.open(d / f"{harvester}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None, deploy_holder(harvester) or {"run": "?", "plan": "?"}
    os.ftruncate(fd, 0)
    os.pwrite(fd, (json.dumps(note) + "\n").encode(), 0)
    return fd, None


def uninstall_refusal(harvester):
    """Message de refus si un retrait de la pile tient ce cluster (2.7.1,
    revue 2.7.0 m3 iii), sinon None. Un déroulé de plan qui tient le verrou
    ne refuse pas : c'est lui qui lance créations et suppressions."""
    holder = deploy_holder(harvester)
    if holder and holder.get("action") == UNINSTALL_ACTION:
        return (f"capi-uninstall-running: the Cluster API providers of this cluster are being removed "
                f"(action {holder.get('run') or '?'})")
    return None


def _refuse_during_uninstall(args, sid):
    msg = uninstall_refusal(_harvester(args))
    if msg:
        step(sid, "error", msg)
    return msg


def cmd_uninstall(args):
    """Retrait de la pile Turtles posée par la console (2.7.0, décision
    215) : refusé tant qu'un Cluster, un HelmChartProxy (CAAPH retiré) ou un
    déroulé de plan existe ; les CRD restent toujours. `--remove-images` :
    images que la console a chargées pour les fournisseurs retirés,
    effacées des nœuds par SSH, refusé si une autre charge de travail en
    emploie une (2.7.1). Hors essai, le verrou du cluster est tenu de bout
    en bout (2.7.1) : ni déroulé, ni création, ni suppression pendant ce
    temps."""
    kube, _, entry = kube_from(args)
    if args.remove_images and not entry:
        raise SystemExit("--remove-images needs --cluster (nodes and SSH come from the configuration)")
    lock_fd = None
    if args.dry_run:
        running = deploy_holder(_harvester(args))
    else:
        lock_fd, running = hold_cluster_lock(_harvester(args), {"run": args.id or UNINSTALL_ACTION,
                                                                "plan": "", "action": UNINSTALL_ACTION})
    try:
        try:
            plan = cs.uninstall(kube, step, dry_run=args.dry_run, timeout=args.timeout, running=running,
                                remove_images=args.remove_images)
        except (RuntimeError, KubeError) as e:
            step("providers", "error", str(e)[:300])
            return EXIT_FAIL
        if args.json:
            print(json.dumps(plan))
        if plan["blocked"]:
            return EXIT_BLOCKED
        if args.remove_images and not plan["images"]:
            step("images", "done", "no image loaded by the console is recorded on these providers; "
                                   "nothing removed from the nodes")
        elif args.remove_images:
            if args.dry_run:
                step("images", "running", "would remove " + ", ".join(plan["images"]))
            else:
                try:
                    node_images.remove_on_nodes(entry, plan["images"], step)
                except RuntimeError as e:
                    step("images", "error", f"{e}; the providers are removed")
                    return EXIT_FAIL
        return EXIT_OK
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


def cmd_inventory(args):
    kube, _, _ = kube_from(args)
    print(json.dumps(inventory(kube, components_from(args.components))))
    return EXIT_OK


def _cloud_provider_bindings(kube, name):
    """{nom: étiquettes} de la ClusterRoleBinding au nom du cluster, si elle
    existe (ci.findings : nom déjà pris par un cluster voisin)."""
    try:
        b = kube.get("clusterrolebindings.rbac.authorization.k8s.io", None, name)
    except KubeError:
        return {}
    return {name: (b.get("metadata") or {}).get("labels") or {}} if b else {}


def _vm_namespace_labels(kube, spec):
    """Étiquettes du namespace des VMs s'il existe déjà, sinon None
    (ci.findings : namespace pris par autre chose que ce cluster)."""
    try:
        o = kube.get("namespaces", None, ci.vm_namespace(spec["namespace"]))
    except KubeError:
        return None
    return ((o.get("metadata") or {}).get("labels") or {}) if o else None


# ---------------------------------------------------------------------------
# 2.6.0 : registres des clusters enfants (tâche 9)
# ---------------------------------------------------------------------------
HIDDEN_CREDS = {"username": "<hidden>", "password": "<hidden>"}


def _state():
    import cluster_decl
    return cluster_decl.state_dir()


def _registry_decls(spec):
    """{nom: déclaration} des registres de la demande (absents omis)."""
    out = {}
    for e in spec.get("registries") or []:
        d = rg.load(_state(), e["registry"])
        if d is not None:
            out[e["registry"]] = d
    return out


def registry_findings(kube, spec, harvester):
    """Constats des registres d'une demande : déclaration, rattachement au
    cluster Harvester `harvester` (compte de lecture posé, lu avec l'identité
    de la personne), usage, miroirs, repli (point 198)."""
    if not spec.get("registries"):
        return []
    decls = _registry_decls(spec)
    attached = {}
    for n in decls:
        try:
            attached[n] = bool(kube.get("secrets", rg.NAMESPACE, rg.secret_names(n)["pull"]))
        except KubeError:
            attached[n] = False
    return rg.check_child(spec["registries"], decls, harvester, attached,
                          rg.system_ready_registries(_state(), decls),
                          vm_probe=_vm_network_probe(kube, spec, decls, harvester))


def _vm_network_probe(kube, spec, decls, harvester):
    """2.6.1 (point 192) : sonde des registres depuis le réseau des VMs du
    cluster enfant (pod brève, bin/lib/vm_probe.py), gardée 5 minutes : la
    fenêtre de création relance ses contrôles à chaque saisie. None sans
    registre d'images connu."""
    import vm_probe
    endpoints = rg.child_probe_endpoints(spec["registries"], decls)
    if not endpoints:
        return None
    if not spec.get("network"):
        return {"network": "", "status": "unchecked", "reason": "no-network", "detail": "", "results": {}}
    return VM_PROBE(_state(), harvester, kube, spec["network"], endpoints, vm_probe.probe_image(_state()))


def _vm_probe_cached(state, cluster, kube, network, endpoints, image):
    import vm_probe
    return vm_probe.cached_run(state, cluster, kube, network, endpoints, image)


VM_PROBE = _vm_probe_cached     # remplacé par les tests


def registry_config(kube, spec, harvester, hidden=False):
    """{config, files, secrets} pour capi_cluster.postprocess, None sans
    registre. Compte de lecture lu dans le Secret `-pull` du cluster
    Harvester (jamais le compte d'écriture, point 188) ; `hidden` (aperçu,
    essai) : rien n'est lu, les Secrets portent des valeurs masquées."""
    if not spec.get("registries"):
        return None
    decls = _registry_decls(spec)
    missing = [e["registry"] for e in spec["registries"] if e["registry"] not in decls]
    if missing:
        raise ValueError(f"cluster-registry-unknown: {', '.join(missing)}")
    names = [n for n, d in decls.items() if "images" in (d.get("usage") or [])]
    if hidden:
        creds = {n: dict(HIDDEN_CREDS) for n in names}
    else:
        try:
            creds = rg.pull_creds_for(kube, names)
        except KeyError as e:
            raise ValueError(f"cluster-registry-not-attached: {e.args[0]} on {harvester}") from None
    doc, files, secrets = rg.rke2_config(spec["registries"], decls,
                                         {"name": spec["name"], "namespace": spec["namespace"]}, creds)
    return {"files": files, "secrets": secrets, "mirrors": sorted(doc.get("mirrors") or {}),
            "no_fallback": any(f["path"] == rg.NO_FALLBACK_FILE for f in files)}


def registries_step(spec, regs):
    """Ligne de l'étape `registries` d'une création."""
    return (f"{len(spec['registries'])} registry(ies): mirrors {', '.join(regs['mirrors']) or '-'}"
            f"{', no fallback to Internet' if regs['no_fallback'] else ''}")


def _check(kube, spec, desc, identity="console", harvester=None):
    inv = inventory(kube, desc)
    if inv.get("unreachable"):
        findings = [cc.finding("cluster-unreachable", "block")]
        return {"findings": findings, "blocked": True, "amount": amount(spec), "spec": spec}
    findings = cc.check(spec, facts_from(inv)) + registry_findings(kube, spec, harvester)
    version = ci.caphv_version(inv.get("stack"), desc)
    if identity == "restricted":
        findings += ci.findings(spec, version, identity, _cloud_provider_bindings(kube, spec["name"]),
                                _vm_namespace_labels(kube, spec))
        if ci.rules_for(version) is not None:
            # ce que la personne posera (namespace, identité, manifestes) et
            # retirera avec le cluster : refusé ici plutôt qu'à mi-création
            reviews = cc.manifest_reviews(spec) + _identity_reviews(spec, version)
            findings += cc.rights_findings(
                {"denied": rights_denied(kube, list(dict.fromkeys(reviews)))})
    return {"findings": findings, "blocked": bool(cc.blocking(findings)),
            "amount": amount(spec, inv["overcommit"]), "spec": spec,
            "identity": identity, "caphv_version": version}


def _identity_reviews(spec, version):
    """`ci.access_reviews`, vide si la version n'a pas de règles ou si la
    demande est incomplète (les constats de `check` le disent déjà)."""
    if ci.rules_for(version) is None:
        return []
    try:
        return ci.access_reviews(spec, version)
    except (ValueError, KeyError):
        return []


def rights_denied(kube, reviews):
    """Accès refusés par le serveur à l'identité du kubeconfig, en une
    requête (SelfSubjectAccessReview, une par accès) ; None si la lecture
    échoue."""
    if not reviews:
        return []
    items = []
    for ns, verb, group, resource, name in reviews:
        attrs = {"verb": verb, "group": group, "resource": resource}
        if ns:
            attrs["namespace"] = ns
        if name:
            attrs["name"] = name
        items.append({"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview",
                      "spec": {"resourceAttributes": attrs}})
    try:
        got = kube.create_many(items)
    except (KubeError, ValueError):
        return None
    if len(got or []) != len(reviews):
        return None
    return [r for r, res in zip(reviews, got) if not ((res.get("status") or {}).get("allowed"))]


def _check_many(kube, specs, desc, identity="restricted", harvester=None):
    """`check_many` sur une seule lecture du cluster, droits de la personne
    compris (tout ce que posera la chaîne : namespace, identité, manifestes)."""
    total = {}
    inv = inventory(kube, desc)
    if inv.get("unreachable"):
        return {"findings": [cc.finding("cluster-unreachable", "block")], "blocked": True,
                "amount": total, "identity": identity, "caphv_version": None}
    facts = facts_from(inv)
    version = ci.caphv_version(inv.get("stack"), desc)
    reviews, extra = [], []
    for s in specs:
        reviews += cc.manifest_reviews(s)
        extra += [dict(f, cluster=f"{s['namespace']}/{s['name']}") for f in registry_findings(kube, s, harvester)]
        if identity == "restricted":
            extra += [dict(f, cluster=f"{s['namespace']}/{s['name']}")
                      for f in ci.findings(s, version, identity, _cloud_provider_bindings(kube, s["name"]),
                                           _vm_namespace_labels(kube, s))]
            reviews += _identity_reviews(s, version)
    facts["denied"] = rights_denied(kube, list(dict.fromkeys(reviews)))
    findings = cc.check_many(specs, facts) + extra
    for s in specs:
        for k, v in amount(s, inv["overcommit"]).items():
            total[k] = total.get(k, 0) + v
    return {"findings": findings, "blocked": bool(cc.blocking(findings)), "amount": total,
            "identity": identity, "caphv_version": version}


def specs_from(path):
    """Liste JSON de demandes (`check --many`), chacune normalisée."""
    raw = json.load(sys.stdin) if path == "-" else json.loads(Path(path).read_text())
    if not isinstance(raw, list) or not raw:
        raise ValueError("--many expects a non-empty JSON list of requests")
    return [cc.normalize(r) for r in raw]


def cmd_check(args):
    kube, _, _ = kube_from(args)
    if args.many:
        try:
            specs = [ci.force_target_namespace(s, args.identity) for s in specs_from(args.spec)]
        except ValueError as e:
            out = {"findings": [cc.finding("invalid", "block", option="-", message=str(e))],
                   "blocked": True}
            print(json.dumps(out) if args.json else str(e))
            return EXIT_BLOCKED
        out = _check_many(kube, specs, components_from(args.components), args.identity, _harvester(args))
        if args.json:
            print(json.dumps(out))
        else:
            for f in out["findings"]:
                print(f"[{f['level']}] {f['code']} {f.get('cluster') or ''} {json.dumps(f['facts'])}")
        return EXIT_BLOCKED if out["blocked"] else EXIT_OK
    try:
        spec = ci.force_target_namespace(spec_from(args.spec), args.identity)
    except ValueError as e:
        out = {"findings": [cc.finding("invalid", "block", option="-", message=str(e))],
               "blocked": True}
        print(json.dumps(out) if args.json else str(e))
        return EXIT_BLOCKED
    out = _check(kube, spec, components_from(args.components), args.identity, _harvester(args))
    if args.json:
        print(json.dumps(out))
    else:
        for f in out["findings"]:
            print(f"[{f['level']}] {f['code']} {json.dumps(f['facts'])}")
    return EXIT_BLOCKED if out["blocked"] else EXIT_OK


def _harvester(args):
    """Nom du cluster Harvester dans la console (rattachement des
    registres) : `--harvester-name`, sinon `--cluster`."""
    return getattr(args, "harvester_name", None) or getattr(args, "cluster", None)


def cmd_render(args):
    """Aperçu : ce que `create` appliquerait, namespace des VMs imposé comme
    lui ; avec l'identité restreinte, rendu sur le kubeconfig d'essai (le
    vrai jeton n'existe qu'à la création)."""
    kube, kc, _ = kube_from(args)
    spec = ci.force_target_namespace(spec_from(args.spec), args.identity)
    errs = cc.validate(spec)
    if errs:
        for k, m in errs:
            sys.stderr.write(f"{k}: {m}\n")
        return EXIT_BLOCKED
    restricted = args.identity == "restricted"
    rkc = _work_file(DRY_RUN_KUBECONFIG) if restricted else kc
    try:
        # aperçu : les comptes des registres ne sont jamais lus (masqués)
        docs = render(spec, rkc, registries=registry_config(kube, spec, _harvester(args), hidden=True))
    finally:
        if restricted:
            Path(rkc).unlink(missing_ok=True)
    print(as_yaml(docs if args.show_secrets else cc.masked(docs)))
    return EXIT_OK


# Kubeconfig donné au générateur pour un essai (`--dry-run`) avec l'identité
# restreinte : rien n'est créé, l'aperçu montre la forme sans aucun jeton.
DRY_RUN_KUBECONFIG = ci.kubeconfig("https://harvester.invalid:6443",
                                   base64.b64encode(b"dry-run").decode(), "dry-run-no-token")
TOKEN_WAIT = 30


def _work_file(content):
    """Fichier 0600 de l'espace de travail (bin/lib/work_dir.py) ; le
    chemin n'est jamais écrit sur stdout, stderr ni dans une étape."""
    return work_dir.private_file(content, prefix="hops-caphv-", suffix=".kubeconfig")


def _ensure_namespace(kube, ns):
    """Crée le namespace du cluster s'il manque (étiqueté comme ceux que
    rend la console) ; rend True s'il a été créé ici."""
    if kube.get("namespaces", None, ns) is not None:
        return False
    kube.apply([{"apiVersion": "v1", "kind": "Namespace",
                 "metadata": {"name": ns, "labels": {cs.MANAGED: "true"}}}], timeout=60)
    return True


def identity_up(kube, spec, version, now=None, sleep=None):
    """Pose l'identité restreinte, attend son jeton, rend le chemin d'un
    kubeconfig 0600 à effacer par l'appelant."""
    now, sleep = now or time.time, sleep or time.sleep
    docs = ci.docs(spec, version)
    kube.apply(docs, timeout=120)
    ns = spec["namespace"]
    deadline = now() + TOKEN_WAIT
    data = {}
    while True:
        data = (kube.get("secrets", ns, ci.TOKEN_SECRET) or {}).get("data") or {}
        if data.get("token") and data.get("ca.crt"):
            break
        if now() > deadline:
            raise RuntimeError(f"the token of {ns}/{ci.IDENTITY} was not issued within {TOKEN_WAIT} s")
        sleep(1)
    vip = cs.harvester_vip(kube)
    if not vip:
        raise RuntimeError("the Harvester address is unknown (kube-system/rke2-traefik and "
                           "ingress-expose have no address)")
    token = base64.b64decode(data["token"]).decode()
    path = _work_file(ci.kubeconfig(f"https://{vip}:6443", data["ca.crt"], token))
    rules = sum(len(d.get("rules") or []) for d in docs)
    step("identity", "done", f"restricted identity {ns}/{ci.IDENTITY}: {rules} rule(s) "
                             f"for CAPHV {version}")
    return path


def _not_found(e):
    return "NotFound" in str(e) or "not found" in str(e)


def _forbidden(e):
    return "Forbidden" in str(e) or "forbidden" in str(e)


def identity_down(kube, ns, name):
    """Retire les objets de l'identité d'un cluster, chacun par son nom
    (`ci.teardown`) : aucune liste à l'échelle du cluster, que l'identité
    d'une personne n'a pas forcément. Un objet au même nom qui porte les
    étiquettes d'un autre cluster n'est pas touché. Rend (retirés, gardés) ;
    `gardés` liste ce qui n'a pas pu être lu ou supprimé : l'appelant
    échoue alors (jamais « supprimé » avec un jeton encore vivant)."""
    side = None
    absent = False
    try:
        sa = kube.get("serviceaccounts", ns, ci.IDENTITY)
        if sa is not None:
            raw = ((sa.get("metadata") or {}).get("annotations") or {}).get(ci.SIDE_NAMESPACES) or ""
            side = [n for n in raw.split(",") if n]
        else:
            # Revue finale 2.5.0 (I2) : le compte part en dernier et reste dès
            # qu'un objet reste ; son absence prouve qu'il n'y a rien à retirer
            # (cluster d'avant la 2.5.0, ou identité déjà retirée). Une lecture
            # refusée ne bloque alors pas la suppression.
            absent = True
    except KubeError as e:
        step("identity", "running", f"serviceaccounts {ci.IDENTITY} not read: {str(e)[:120]}")
    gone, kept = 0, []
    for kind, ons, oname in ci.teardown(ns, name, side):
        if kept and (kind, ons, oname) == ("serviceaccounts", ns, ci.IDENTITY):
            break   # gardé pour la reprise : il porte les namespaces annexes
        where = f"{kind.partition('.')[0]} {ons + '/' if ons else ''}{oname}"
        try:
            obj = kube.get(kind, ons, oname)
        except KubeError as e:
            if absent and _forbidden(e):
                continue
            kept.append(where)
            step("identity", "running", f"{where} not read: {str(e)[:120]}")
            continue
        if obj is None:
            continue
        lab = (obj.get("metadata") or {}).get("labels") or {}
        if lab.get(ci.L_CLUSTER) != name or lab.get(ci.L_NAMESPACE) != ns:
            # même nom, autre propriétaire (liaison créée par CAPHV pour un
            # cluster de la 2.4, ou cluster voisin) : laissé en place
            continue
        try:
            kube.delete(kind, ons, oname)
            gone += 1
        except KubeError as e:
            if _not_found(e):
                continue
            kept.append(where)
            step("identity", "running", f"{where} kept: {str(e)[:120]}")
    return gone, kept


def cmd_create(args):
    if not args.dry_run and _refuse_during_uninstall(args, "check"):
        return EXIT_BLOCKED
    kube, kc, _ = kube_from(args)
    origin = cc.parse_origin(args.origin)
    restricted = args.identity == "restricted"
    spec = ci.force_target_namespace(spec_from(args.spec), args.identity)
    desc = components_from(args.components)
    out = _check(kube, spec, desc, args.identity, _harvester(args))
    blockers = cc.blocking(out["findings"])
    if blockers:
        step("check", "error", "the pre-check refuses: " + ", ".join(f["code"] for f in blockers))
        if args.json:
            print(json.dumps(out))
        return EXIT_BLOCKED
    step("check", "done", f"{out['amount']['vms']} VM(s), {out['amount']['addresses']} address(es)")
    ns, name = spec["namespace"], spec["name"]
    if args.dry_run:
        dry_kc = _work_file(DRY_RUN_KUBECONFIG) if restricted else kc
        try:
            docs = render(spec, dry_kc, origin, registries=registry_config(kube, spec, _harvester(args), hidden=True))
        finally:
            if restricted:
                Path(dry_kc).unlink(missing_ok=True)
        step("render", "done", f"{len(docs)} object(s) for {ns}/{name}")
        if restricted:
            step("identity", "done", f"would create the restricted identity {ns}/{ci.IDENTITY} "
                                     f"({len(ci.docs(spec, out['caphv_version']))} object(s))")
        print(as_yaml(cc.masked(docs)))
        return EXIT_OK
    # Revue finale 2.5.0 (I1) : seul un namespace créé ICI peut être retiré
    # par la reprise ; un état inconnu (lecture en échec) vaut « existait »
    ns_created = False
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    applied = False
    try:
        ident = None
        # comptes de lecture des registres relus avant tout geste : un Secret
        # retiré depuis le contrôle arrête la création ici
        regs = registry_config(kube, spec, _harvester(args))
        if regs:
            step("registries", "done", registries_step(spec, regs))
        try:
            if restricted:
                ns_created = _ensure_namespace(kube, ns)
                ident = identity_up(kube, spec, out["caphv_version"])
            docs = render(spec, ident or kc, origin, registries=regs)
        finally:
            if ident:
                Path(ident).unlink(missing_ok=True)
        step("render", "done", f"{len(docs)} object(s) for {ns}/{name}")
        kube.apply(docs, timeout=120)
        applied = True
        step("apply", "done", f"cluster {ns}/{name} declared on the management cluster")
        if args.no_wait:
            return EXIT_OK
        st = wait_ready(kube, ns, name, args.timeout)
        step("provision", "done", f"control plane {st['cp'][0]}/{st['cp'][1]}, "
                                  f"workers {st['workers'][0]}/{st['workers'][1]} (100%)")
        step("done", "done", f"{ns}/{name} is available")
        return EXIT_OK
    except Cancelled:
        step("cancel", "error", "cancelled, the cluster is being deleted")
        _undo(kube, ns, name, ns_created, applied, args.timeout)
        return EXIT_CANCELLED
    except TimeoutError as e:
        step("provision", "error", f"not available in time ({str(e)[:200]}); "
                                   "the cluster is left in place for inspection")
        return EXIT_FAIL
    except (RuntimeError, KubeError, ValueError) as e:
        step("provision" if applied else "prepare", "error", str(e)[:300])
        if not applied:
            # rien n'a été déclaré : l'identité et le namespace créé partent
            _undo(kube, ns, name, ns_created, False, args.timeout)
        return EXIT_FAIL
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.SIG_DFL)


def _undo(kube, ns, name, ns_created, applied, timeout=2400):
    """Défait une création interrompue. Manifestes appliqués : la suppression
    complète (le cluster d'abord, son identité ensuite) ; sinon l'identité et
    le namespace, seulement s'il a été créé par cette création
    (`ns_created`) : un namespace qui existait n'est jamais supprimé."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        if applied:
            remove_cluster(kube, ns, name, timeout)
            return
        _, kept = identity_down(kube, ns, name)
        if kept:
            step("cancel", "error", f"identity of {ns}/{name}: {len(kept)} object(s) kept "
                                    f"({', '.join(kept[:6])}); run delete again")
            return
        if ns_created:
            kube.delete("namespaces", None, ns)
    except KubeError as e:
        step("cancel", "error", f"could not delete {ns}/{name}: {str(e)[:200]}")


HC_LEFTOVER_TIMEOUT = 120


def _remove_leftover_hc(kube, ns, name, now, sleep):
    """2.7.0 (m7, décision 216) : un HarvesterCluster resté sans son
    Cluster est supprimé, puis attendu HC_LEFTOVER_TIMEOUT secondes. Encore
    là : étape en erreur qui nomme le finaliseur qui le retient ; la console
    ne retire JAMAIS un finaliseur (CAPHV le lève quand ce qu'il garde est
    nettoyé). Rend None, ou le code de sortie de l'échec."""
    try:
        kube.delete(cc.K_HARVESTERCLUSTER, ns, name)
    except KubeError as e:
        if not _not_found(e):
            raise
    step("delete", "running", f"HarvesterCluster {ns}/{name} left without its Cluster: removing it")
    deadline = now() + HC_LEFTOVER_TIMEOUT
    while True:
        hc = kube.get(cc.K_HARVESTERCLUSTER, ns, name)
        if hc is None:
            step("delete", "running", f"HarvesterCluster {ns}/{name} removed")
            return None
        if now() > deadline:
            fins = ((hc.get("metadata") or {}).get("finalizers")) or []
            step("delete", "error", f"HarvesterCluster {ns}/{name} still there after {HC_LEFTOVER_TIMEOUT} s, "
                                    f"held by the finalizer(s) {', '.join(fins) or '-'}; the console never removes "
                                    "a finalizer: fix what holds it (CAPHV, its identity), then run delete again")
            return EXIT_FAIL
        sleep(5)


def remove_cluster(kube, ns, name, timeout, sleep=None, now=None):
    """Ordre imposé : le Cluster, attente de sa disparition (CAPHV a besoin
    de son identité pour supprimer VMs, volumes, répartiteur et pool), puis
    l'identité, puis ce que la console a rendu et le namespace qu'elle a
    créé. Rend un code de sortie."""
    now, sleep = now or time.time, sleep or time.sleep
    if kube.get(cs.K_CLUSTER, ns, name) is None and kube.get(cc.K_HARVESTERCLUSTER, ns, name) is not None:
        failed = _remove_leftover_hc(kube, ns, name, now, sleep)
        if failed:
            return failed
    if kube.get(cs.K_CLUSTER, ns, name) is not None:
        kube.delete(cs.K_CLUSTER, ns, name)
        step("delete", "running", f"{ns}/{name}: machines and VMs being removed")
    deadline = now() + timeout
    while kube.get(cs.K_CLUSTER, ns, name) is not None:
        if now() > deadline:
            step("delete", "error", f"{ns}/{name} still deleting after {timeout} s; "
                                    "its identity is kept, CAPHV still needs it")
            return EXIT_FAIL
        left = len([m for m in kube.list("machines.cluster.x-k8s.io", ns)
                    if ((m.get("metadata") or {}).get("labels") or {})
                    .get("cluster.x-k8s.io/cluster-name") == name])
        step("delete", "running", f"{ns}/{name}: {left} machine(s) left")
        sleep(10)
    gone, kept = identity_down(kube, ns, name)
    if kept:
        # le namespace reste : il porte le compte qui liste les namespaces
        # annexes, une nouvelle suppression reprend là
        step("delete", "error", f"identity of {ns}/{name}: {len(kept)} object(s) kept "
                                f"({', '.join(kept[:6])}); fix the rights and run delete again")
        return EXIT_FAIL
    if gone:
        step("delete", "running", f"identity of {ns}/{name} removed ({gone} object(s))")
    nsobj = kube.get("namespaces", None, ns) or {}
    others = [c for c in kube.list(cs.K_CLUSTER, ns)]
    if others:
        # Revue finale 2.6.0 : d'autres clusters gardent le namespace et les
        # objets partagés, mais les Secrets de registre de CE cluster
        # (registries.yaml avec le compte de lecture, -auth, -ca, -chart-)
        # partent, par nom exact : sinon le compte reste sur Harvester et le
        # registre se dit employé pour toujours
        _remove_registry_secrets(kube, ns, name)
    else:
        # Ce que la console a rendu à côté du Cluster : ClusterClass, gabarits,
        # compléments, et le secret d'identité qui porte un kubeconfig Harvester.
        gone = 0
        for kind in cc.GENERATED_KINDS:
            try:
                found = kube.list(kind, ns, selector=f"{cc.GENERATED}=true")
            except KubeError as e:
                # un genre illisible n'empêche pas de retirer les autres,
                # dont le secret qui porte un kubeconfig Harvester
                step("delete", "running", f"{kind.partition('.')[0]} not listed in {ns}: {str(e)[:120]}")
                continue
            for obj in found:
                try:
                    kube.delete(kind, ns, obj["metadata"]["name"])
                    gone += 1
                except KubeError as e:
                    if "(NotFound)" in str(e):
                        # 2.6.0, vu en réel : CAPI efface déjà les objets dont
                        # le Cluster est propriétaire, entre la liste et ici
                        gone += 1
                        continue
                    step("delete", "running", f"{kind} {obj['metadata']['name']} kept: {e}")
        if gone:
            step("delete", "running", f"{gone} generated object(s) removed from {ns}")
        if ((nsobj.get("metadata") or {}).get("labels") or {}).get(cs.MANAGED) == "true":
            kube.delete("namespaces", None, ns)
            step("delete", "running", f"namespace {ns} removed (created by the console)")
    step("delete", "done", f"{ns}/{name} deleted")
    return EXIT_OK


def _registry_secrets_of(generated, ns, name):
    """Secrets de registre rendus pour le cluster `name`, par nom exact
    (`<c>-registries`, et pour chaque registre étiqueté `-registry-<r>-auth`,
    `-registry-<r>-ca`, `-chart-<r>`) : un cluster `web2` n'emporte rien de
    `web`."""
    wanted = {rg.nodes_secret_name(name)}
    for s in generated:
        reg = ((s.get("metadata") or {}).get("labels") or {}).get(rg.LABEL)
        if reg:
            wanted |= set(rg.child_secret_names(name, reg).values())
            wanted.add(sv.registry_secret_names({"cluster": f"{ns}/{name}"}, reg)["auth"])
    return [s for s in generated if (s.get("metadata") or {}).get("name") in wanted]


def _remove_registry_secrets(kube, ns, name):
    try:
        found = _registry_secrets_of(kube.list("secrets", ns, selector=f"{cc.GENERATED}=true"), ns, name)
    except KubeError as e:
        step("delete", "running", f"registry secrets of {ns}/{name} not listed: {str(e)[:120]}")
        return
    gone = 0
    for s in found:
        sname = s["metadata"]["name"]
        try:
            kube.delete("secrets", ns, sname)
            gone += 1
        except KubeError as e:
            if "(NotFound)" in str(e):
                gone += 1
                continue
            step("delete", "running", f"secrets {sname} kept: {e}")
    if gone:
        step("delete", "running", f"{gone} registry secret(s) of {name} removed from {ns} "
                                  "(other clusters keep the namespace)")


def destroy_plan(kube, ns, name):
    """2.7.0 (Défaire) : ce que `delete` emporterait, sans rien écrire :
    machines, VMs et volumes du namespace des VMs, répartiteurs de charge,
    namespace créé par la console."""
    cl = kube.get(cs.K_CLUSTER, ns, name)
    out = {"cluster": f"{ns}/{name}", "present": cl is not None, "machines": 0, "target_namespace": None,
           "vms": [], "volumes": [], "loadbalancers": [], "namespace_removed": False}
    if cl is None:
        return out
    var = next((v for v in ((cl.get("spec") or {}).get("topology") or {}).get("variables") or []
                if v.get("name") == "targetNamespace"), {})
    target = var.get("value") or ns
    out["target_namespace"] = target
    machines = [m for m in kube.list("machines.cluster.x-k8s.io", ns)
                if ((m.get("metadata") or {}).get("labels") or {}).get("cluster.x-k8s.io/cluster-name") == name]
    out["machines"] = len(machines)
    vms = sorted({((m.get("spec") or {}).get("infrastructureRef") or {}).get("name") for m in machines} - {None})
    out["vms"] = vms
    out["volumes"] = sorted(p["metadata"]["name"] for p in kube.list("persistentvolumeclaims", target)
                            if any(p["metadata"]["name"].startswith(v + "-") for v in vms))
    out["loadbalancers"] = sorted(lb["metadata"]["name"] for lb in kube.list(
        "loadbalancers.loadbalancer.harvesterhci.io", target) if name in lb["metadata"]["name"])
    nsobj = kube.get("namespaces", None, ns) or {}
    out["namespace_removed"] = ((nsobj.get("metadata") or {}).get("labels") or {}).get(cs.MANAGED) == "true" \
        and len(kube.list(cs.K_CLUSTER, ns)) == 1
    return out


def cmd_delete(args):
    kube, _, _ = kube_from(args)
    ns, _, name = args.name.partition("/")
    if not ns or not name:
        raise SystemExit("--name expects namespace/name")
    if getattr(args, "dry_run", False):
        print(json.dumps(destroy_plan(kube, ns, name)))
        return EXIT_OK
    if _refuse_during_uninstall(args, "delete"):
        return EXIT_BLOCKED
    cl = kube.get(cs.K_CLUSTER, ns, name)
    refused = cc.run_mismatch(cl, getattr(args, "expect_run", None)) if cl is not None else None
    if refused:
        step("delete", "error", refused)
        return EXIT_BLOCKED
    if cl is None:
        # Cluster déjà parti, identité restée (suppression précédente qui a
        # échoué sur un droit) : la reprise finit le nettoyage
        sa = kube.get("serviceaccounts", ns, ci.IDENTITY)
        if (sa is None or ((sa.get("metadata") or {}).get("labels") or {}).get(ci.L_CLUSTER) != name) \
                and kube.get(cc.K_HARVESTERCLUSTER, ns, name) is None:      # 2.7.0 (m7) : reste à retirer
            step("delete", "error", f"cluster {ns}/{name} not found")
            return EXIT_FAIL
        step("delete", "running", f"{ns}/{name} already gone, its identity is left: resuming")
    return remove_cluster(kube, ns, name, args.timeout)


def cluster_brief(kube, ns, name):
    """Un Cluster en une lecture : {present, namespace, name, origin,
    state} (état sans liste de machines : compteurs du statut) ; absent :
    {present: false, namespace_present}."""
    cl = kube.get(cs.K_CLUSTER, ns, name)
    out = {"present": cl is not None, "namespace": ns, "name": name}
    if cl is not None:
        out["origin"] = cc.origin_of(cl)
        out["state"] = cc.cluster_state(cl)
    else:
        # absent : le namespace existe-t-il déjà (consigné par un plan, qui
        # dit ainsi s'il l'a créé) ; seconde lecture, pour ce cas seulement
        out["namespace_present"] = kube.get("namespaces", None, ns) is not None
    return out


def cmd_state(args):
    kube, _, _ = kube_from(args)
    ns, _, name = args.name.partition("/")
    if not cc.NS_NAME_RE.match(args.name or ""):
        raise ValueError("--name expects namespace/name")
    out = cluster_brief(kube, ns, name)
    if args.json:
        print(json.dumps(out))
    elif out["present"]:
        st = out["state"]
        print(f"{ns}/{name}: {'ready' if st['ready'] else 'failed' if st['failed'] else st['phase']}, "
              f"control plane {st['cp'][0]}/{st['cp'][1]}, workers {st['workers'][0]}/{st['workers'][1]}"
              + (f"; plan {out['origin'].get('plan')}, template {out['origin'].get('template')}"
                 if out["origin"] else ""))
    else:
        print(f"{ns}/{name}: not found")
    return EXIT_OK if out["present"] else EXIT_FAIL


# ---------------------------------------------------------------------------
# v1.52.0 (B2) : services sur les clusters créés (CAAPH, HelmChartProxy)
# ---------------------------------------------------------------------------

def _caaph_ready(kube):
    try:
        provs = [cs.provider_state(p) for p in kube.list(cs.K_CAPIPROVIDER)]
    except KubeError:
        return False
    return any(p["type"] == "addon" and p["name"] == "helm" and p["ready"] for p in provs)


def _clusters_without_kube_vip(kube):
    """Les clusters du fournisseur de cloud Harvester créés avant la v1.52.0 :
    sans kube-vip, leurs services LoadBalancer n'ont pas d'adresse."""
    try:
        have = {c["metadata"].get("namespace") for c in kube.list(cc.K_CRS)
                if c["metadata"].get("name") == cc.KUBE_VIP_CRS}
    except KubeError:
        return []
    return sorted(f"{c['metadata']['namespace']}/{c['metadata']['name']}"
                  for c in kube.list(cs.K_CLUSTER)
                  if (c["metadata"].get("labels") or {}).get("ccm") == "external"
                  and c["metadata"].get("namespace") not in have)


def _ensure_kube_vip(kube, ns, cluster):
    c = kube.get(cs.K_CLUSTER, ns, cluster) or {}
    if ((c.get("metadata") or {}).get("labels") or {}).get("ccm") != "external":
        return
    if kube.get(cc.K_CRS, ns, cc.KUBE_VIP_CRS) is None:
        kube.apply(cc.kube_vip_docs(ns))
        step("lb", "done", f"kube-vip added to {cluster}, so that LoadBalancer services get an address")


def _service_facts(kube):
    objs = [c for c in kube.list(cs.K_CLUSTER)
            if c["metadata"].get("namespace") not in ("fleet-local", "fleet-default")]
    clusters = [f"{c['metadata']['namespace']}/{c['metadata']['name']}" for c in objs]
    # clusters qui portent l'étiquette d'un service (sélecteur du HCP)
    labelled = {}
    for c in objs:
        md = c["metadata"]
        for k, v in (md.get("labels") or {}).items():
            if k.startswith(sv.LABEL_PREFIX) and v == "on":
                labelled.setdefault(f"{md['namespace']}/{k[len(sv.LABEL_PREFIX):]}", []).append(
                    f"{md['namespace']}/{md['name']}")
    try:
        hcps = kube.list(sv.K_HCP)
        hrps = kube.list(sv.K_HRP)
    except KubeError:
        hcps, hrps = [], []
    return {"caaph": _caaph_ready(kube), "clusters": sorted(clusters),
            "no_lb": _clusters_without_kube_vip(kube),
            "services": [f"{h['metadata']['namespace']}/{h['metadata']['name']}" for h in hcps],
            "service_clusters": labelled, "_hcps": hcps, "_hrps": hrps}


def service_spec_from(path):
    raw = json.load(sys.stdin) if path == "-" else json.loads(Path(path).read_text())
    spec = sv.normalize(raw)
    if "origin" in spec:
        spec["origin"] = cc.parse_origin(",".join(f"{k}={v}" for k, v in spec["origin"].items()))
    return spec


def service_meta(spec):
    """2.7.0 : étiquettes d'élément (et d'origine) d'un chart de paquet, ou
    None pour un service du catalogue."""
    src = spec.get("source")
    if not src:
        return None
    return mf.meta_of(spec["name"], src["package"], src["version"], spec["cluster"].partition("/")[2],
                      spec.get("origin"), spec.get("by", ""))


def cmd_services(args):
    kube, _, _ = kube_from(args)
    f = _service_facts(kube)
    services = sv.state(f["_hcps"], f["_hrps"])
    by_ref = {(h["metadata"].get("namespace"), h["metadata"].get("name")): h for h in f["_hcps"]}
    for s in services:
        # 2.7.0 : chart d'un paquet (élément, version, qui, origine d'un plan)
        s["artefact"] = mf.artefact_of(by_ref.get((s["namespace"], s["name"])))
    out = {"caaph": f["caaph"], "clusters": f["clusters"], "catalog": sv.catalog(),
           "services": services}
    print(json.dumps(out) if args.json else json.dumps(out, indent=1))
    return EXIT_OK


def service_source(kube, spec, harvester, read_creds=False):
    """2.6.0 (tâche 10) : un chart publié dans un registre déclaré. Rend
    (demande résolue, constats, contexte) ; le contexte porte la
    déclaration et, pour le déploiement (`read_creds`), le compte de
    LECTURE lu dans le Secret `-pull` du cluster Harvester."""
    import artefacts as art
    import oci_layout as ol
    if not spec.get("source"):
        return spec, [], None
    src = spec["source"]
    reg, ref = src["registry"], f"{src['package']}-{src['version']}"
    decl = rg.load(_state(), reg)
    if decl is None:
        return spec, [sv.finding("service-registry-unknown", "block", registry=reg)], None
    found = []
    if "charts" not in (decl.get("usage") or []):
        found.append(sv.finding("service-registry-no-charts", "block", registry=reg))
    try:
        pull = rg.read_creds(kube, reg, "pull") if harvester in (decl.get("clusters") or []) else None
    except KubeError:
        pull = None
    if pull is None:
        found.append(sv.finding("service-registry-not-attached", "block", registry=reg, cluster=harvester))
    try:
        pkg = art.load_package(_state(), src["package"], src["version"])
    except art.ArtefactError:
        return spec, found + [sv.finding("service-package-missing", "block", package=ref)], None
    item = next((i for i in pkg["index"].get("items") or [] if i.get("name") == src["item"]), None)
    if item is None or item.get("kind") != "chart":
        return spec, found + [sv.finding("service-item-not-chart", "block", package=ref, item=src["item"])], None
    try:
        meta = ol.chart_meta(Path(pkg["path"]) / item["chart"])
        resolved = sv.resolve_source(spec, decl["endpoint"], meta, item.get("values") or {})
    except ValueError as e:
        return spec, found + [sv.finding("invalid-values", "block", message=str(e)[:200])], None
    if not any((p.get("package"), p.get("version")) == (src["package"], src["version"])
               for p in decl.get("published") or []):
        found.append(sv.finding("service-chart-not-published", "warn", registry=reg, package=ref))
    ns, _, child = spec["cluster"].partition("/")
    try:
        mirrored = kube.get("secrets", ns, rg.child_secret_names(child, reg)["auth"]) is not None
    except KubeError:
        mirrored = False
    if not mirrored:
        found.append(sv.finding("service-registry-not-mirrored", "warn", registry=reg, cluster=spec["cluster"]))
    ctx = {"decl": decl, "creds": pull if read_creds else None}
    return resolved, found, ctx


def cmd_service_check(args):
    kube, _, _ = kube_from(args)
    spec = service_spec_from(args.spec)
    spec, extra, _ = service_source(kube, spec, _harvester(args))
    found = sv.check(spec, _service_facts(kube)) + extra
    blocked = bool(sv.blocking(found))
    print(json.dumps({"blocked": blocked, "findings": found,
                      "values": sv.render_values(spec)}))
    return EXIT_BLOCKED if blocked else EXIT_OK


def _release_of(kube, ns, name, cluster):
    for r in kube.list(sv.K_HRP, ns):
        owners = [o.get("name") for o in r["metadata"].get("ownerReferences") or []]
        if name in owners and (r.get("spec") or {}).get("clusterRef", {}).get("name") == cluster:
            return r
    return None


def cmd_service_deploy(args):
    """Déclare le service (HelmChartProxy), vise le cluster par son
    étiquette, et suit l'installation jusqu'à ce que CAAPH la dise prête."""
    kube, _, _ = kube_from(args)
    spec = service_spec_from(args.spec)
    spec, extra, src = service_source(kube, spec, _harvester(args), read_creds=True)
    found = sv.check(spec, _service_facts(kube)) + extra
    if sv.blocking(found):
        for f in sv.blocking(found):
            step("check", "error", f"{f['code']} {json.dumps(f['facts'])}")
        return EXIT_BLOCKED
    ns, _, cluster = spec["cluster"].partition("/")
    step("check", "done", f"{spec['name']} ({spec['chart']} {spec['version'] or 'latest'}) for {spec['cluster']}")
    _ensure_kube_vip(kube, ns, cluster)
    docs, registry = [], None
    if src:
        # compte de lecture au format config.json et CA, dans le namespace du
        # cluster : la CA posée par la création du cluster est reprise
        decl, reg = src["decl"], spec["source"]["registry"]
        names = sv.registry_secret_names(spec, reg)
        ca = decl.get("ca_pem") if decl.get("ca_pem") and kube.get("secrets", ns, names["ca"]) is None else None
        docs = sv.registry_secrets(spec, reg, decl["endpoint"], src["creds"], ca,
                                   labels={cc.GENERATED: "true", rg.LABEL: reg})
        registry = {"name": reg, "ca": bool(decl.get("ca_pem"))}
        step("registry", "done", f"chart {spec['chart']} {spec['version']} from {reg} ({decl['endpoint']}), "
                                 "read account")
    kube.apply(docs + [sv.manifest(spec, registry, service_meta(spec))])
    kube.patch(cs.K_CLUSTER, ns, cluster, {"metadata": {"labels": {sv.label_of(spec["name"]): "on"}}})
    step("apply", "done", f"service {spec['name']} declared, cluster {cluster} selected")
    deadline = time.time() + args.timeout
    last = None
    while time.time() < deadline:
        # Une mise à jour ne se juge qu'une fois lue par CAAPH : le service
        # dans sa nouvelle version, puis la release refaite. Avant, la release
        # d'avant, encore prête, faisait finir l'action sans rien attendre.
        hcp = kube.get(sv.K_HCP, ns, spec["name"]) or {"metadata": {"name": spec["name"], "namespace": ns}}
        r = _release_of(kube, ns, spec["name"], cluster)
        st = sv.state([dict(hcp, metadata=dict(hcp["metadata"], name=spec["name"], namespace=ns))],
                      [r] if r else [])[0]
        rel = st["releases"][0] if st["releases"] else None
        if not st["current"]:
            msg = "waiting for CAAPH to read the service"
        elif rel is None:
            msg = "waiting for CAAPH to pick the cluster"
        else:
            msg = f"release {rel['status'] or 'pending'} (revision {rel['revision'] or '-'})"
            if not rel["current"]:
                msg += ", being updated"
            elif rel["message"]:
                msg += f": {rel['message'][:160]}"
        if msg != last:
            step("install", "running", msg)
            last = msg
        if st["current"] and rel and rel["ready"] and rel["current"]:
            step("install", "done", f"{spec['name']} installed on {cluster} "
                                    f"(namespace {spec['namespace']}, revision {rel['revision']})")
            return EXIT_OK
        time.sleep(10)
    step("install", "error", f"not ready after {args.timeout} s: {last}")
    return EXIT_FAIL


def cmd_service_remove(args):
    """Retire le service : CAAPH désinstalle la release de chaque cluster
    visé, puis l'étiquette est ôtée."""
    kube, _, _ = kube_from(args)
    ns, _, name = args.name.partition("/")
    hcp = kube.get(sv.K_HCP, ns, name)
    if hcp is None:
        step("remove", "error", f"service {ns}/{name} not found")
        return EXIT_FAIL
    refused = cc.run_mismatch(hcp, getattr(args, "expect_run", None))
    if refused:
        step("remove", "error", refused)
        return EXIT_BLOCKED
    label = sv.label_of(name)
    kube.delete(sv.K_HCP, ns, name)
    step("remove", "running", f"service {ns}/{name} deleted, releases being uninstalled")
    # 2.6.0 : identifiants et CA posés pour ce service, s'il était le dernier
    # à s'en servir (jamais la CA posée par la création du cluster)
    try:
        others = [h for h in kube.list(sv.K_HCP, ns) if h["metadata"]["name"] != name]
        for sname in sv.secrets_to_remove(hcp, others, kube.list("secrets", ns, selector=f"{cc.GENERATED}=true")):
            kube.delete("secrets", ns, sname)
            step("remove", "running", f"secret {sname} removed")
    except KubeError as e:
        step("remove", "running", f"registry secrets of {name} kept: {str(e)[:120]}")
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        left = [r for r in kube.list(sv.K_HRP, ns)
                if name in [o.get("name") for o in r["metadata"].get("ownerReferences") or []]]
        if not left:
            break
        time.sleep(5)
    for c in kube.list(cs.K_CLUSTER, ns):
        if ((c["metadata"].get("labels") or {}).get(label)):
            kube.patch(cs.K_CLUSTER, ns, c["metadata"]["name"],
                       {"metadata": {"labels": {label: None}}})
    _release_namespace_down(kube, ns, hcp)
    step("remove", "done", f"service {ns}/{name} removed")
    return EXIT_OK


def _release_namespace_down(kube, ns, hcp):
    """2.7.0 (essai 2) : CAAPH laisse le namespace de la release que
    `createNamespace` a créé. Pour un chart de paquet, il est retiré s'il
    est né après le service (créé pour lui) et ne porte plus rien ;
    sinon il est gardé et nommé. Jamais d'erreur : le service est parti."""
    art = mf.artefact_of(hcp)
    rns = ((hcp.get("spec") or {}).get("namespace") or "")
    if not art or not rns or mf.is_system_namespace(rns) or rns in mf.IMMORTAL_NAMESPACES:
        return
    child_name = art["cluster"]
    if kube.get(cs.K_CLUSTER, ns, child_name) is None:
        return                                  # parti avec le cluster
    born = (hcp.get("metadata") or {}).get("creationTimestamp") or ""
    try:
        with ca.session(kube, ns, child_name) as path:
            child = Kube(path)
            obj = child.get("namespaces", None, rns)
            if obj is None:
                return
            if ((obj.get("metadata") or {}).get("creationTimestamp") or "") < born:
                step("remove", "running", f"namespace {rns} kept on {child_name}: it existed before the chart")
                return
            others = _namespace_others(child, rns, art["item"], child_name)
            if others:
                step("remove", "running", f"namespace {rns} kept on {child_name}: it holds {len(others)} object(s)")
                return
            child.delete("namespaces", None, rns)
            step("remove", "running", f"namespace {rns} removed from {child_name} (created for the chart)")
    except (ca.ChildAccessError, KubeError) as e:
        step("remove", "running", f"namespace {rns} kept on {child_name}: {str(e)[:120]}")


# ---------------------------------------------------------------------------
# 2.7.0 : manifestes, politiques et jetons des clusters enfants
# ---------------------------------------------------------------------------
MANIFEST_KEYS = {"cluster", "package", "version", "item", "origin", "policies", "by"}
POLICIES_PACKAGE, POLICIES_VERSION = "harvester-ops", "builtin"
BY_MAX = 128


def _child_name(text, parts=2):
    """`ns/cluster` (ou `ns/cluster/élément`) contrôlé ; ValueError sinon."""
    bits = str(text or "").split("/")
    if len(bits) != parts or not all(cc.K8S_NAME_RE.match(b) for b in bits):
        raise ValueError("--name expects " + "/".join(("namespace", "cluster", "item")[:parts]))
    return bits


def manifest_spec_from(path):
    """Demande de `manifest-*` : `cluster` <ns>/<c>, puis soit `package`,
    `version`, `item`, soit `policies` (socle et accès) ; `origin` et `by`
    facultatifs. ValueError qui nomme le champ."""
    raw = json.load(sys.stdin) if path == "-" else json.loads(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError("the request is not an object")
    for k in sorted(set(raw) - MANIFEST_KEYS):
        raise ValueError(f"unknown key: {k}")
    ns, cluster = _child_name(raw.get("cluster"))
    out = {"namespace": ns, "cluster": cluster}
    if "policies" in raw:
        if any(k in raw for k in ("package", "version", "item")):
            raise ValueError("policies: give either policies or package, version and item")
        out.update(policies=cp.normalize(raw["policies"]), item=cp.ITEM,
                   package=POLICIES_PACKAGE, version=POLICIES_VERSION)
    else:
        for k in ("package", "version", "item"):
            if not isinstance(raw.get(k), str) or not raw[k]:
                raise ValueError(f"{k}: required")
        out.update(package=raw["package"], version=raw["version"], item=raw["item"])
    origin = raw.get("origin") or {}
    if not isinstance(origin, dict):
        raise ValueError("origin: an object")
    out["origin"] = cc.parse_origin(",".join(f"{k}={v}" for k, v in origin.items()))
    by = raw.get("by", "")
    if not isinstance(by, str) or len(by) > BY_MAX or "\n" in by:
        raise ValueError(f"by: one line, {BY_MAX} characters at most")
    out["by"] = by
    return out


def manifest_objects(spec):
    """(objets, constats) de l'élément demandé : rendus pour le socle, lus
    dans le paquet du répertoire d'état sinon."""
    import artefacts as art
    item = spec["item"]
    if not mf.item_name_ok(item):
        return [], [mf.finding("artefact-name-too-long", item=item, max=42)]
    if "policies" in spec:
        objs = cp.objects(spec["policies"])
        return objs, mf.check(objs, spec["cluster"], item)
    ref = f"{spec['package']}-{spec['version']}"
    if item == cp.ITEM or spec["package"] == POLICIES_PACKAGE:
        # revue 1-3, m3 : le socle de la console ne se confond avec aucun
        # élément de paquet (ses comptes reçoivent des jetons, point 208)
        return [], [mf.finding("artefact-name-reserved", item=item, package=spec["package"])]
    try:
        pkg = art.load_package(_state(), spec["package"], spec["version"])
    except art.ArtefactError:
        return [], [mf.finding("artefact-package-missing", package=ref)]
    it = next((i for i in pkg["index"].get("items") or [] if i.get("name") == item), None)
    if it is None or it.get("kind") not in ("manifest", "policy") or not it.get("files"):
        return [], [mf.finding("artefact-item-not-manifest", package=ref, item=item)]
    root = Path(pkg["path"]).resolve()
    files = {}
    for rel in it["files"]:
        p = (root / rel).resolve()
        if root not in p.parents or not p.is_file():
            return [], [mf.finding("artefact-file-missing", package=ref, file=rel)]
        files[rel] = p.read_text()
    try:
        objs = mf.parse(files)
    except ValueError as e:
        return [], [mf.finding("manifest-unreadable", package=ref, message=str(e)[:200])]
    return objs, mf.check(objs, spec["cluster"], item)


def _manifest_owner(kube, spec):
    """Constats sur le cluster de gestion : Cluster présent, élément du
    même nom posé pour un autre cluster ou un autre paquet."""
    ns, cluster, item = spec["namespace"], spec["cluster"], spec["item"]
    found = []
    if kube.get(cs.K_CLUSTER, ns, cluster) is None:
        found.append(mf.finding("manifest-cluster-missing", cluster=f"{ns}/{cluster}"))
    crs = kube.get(mf.K_CRS, ns, mf.name_of(item))
    if crs is not None:
        lab = (crs.get("metadata") or {}).get("labels") or {}
        if lab.get(mf.L_CLUSTER) != cluster or lab.get(mf.L_PACKAGE) != spec["package"]:
            found.append(mf.finding("artefact-taken", item=item, cluster=lab.get(mf.L_CLUSTER) or "",
                                    package=lab.get(mf.L_PACKAGE) or ""))
    return found


def _team_namespaces(child, spec):
    """Point 207 : un namespace d'équipe déjà présent dans le cluster enfant
    sans les étiquettes de la console bloque ; socle décoché, un namespace
    de rôle ou de compte absent bloque aussi (revue 1-3 m6)."""
    out = []
    for ns in cp.team_namespaces(spec["policies"]):
        obj = child.get("Namespace", None, ns)
        if obj is not None and not mf.is_ours(obj, spec["item"], spec["cluster"]):
            out.append(mf.finding("team-namespace-taken", namespace=ns))
    # revue 1-3 m6 : socle décoché, rôles et comptes vont dans un namespace
    # existant ; absent, le binding ne s'appliquerait jamais
    for ns in cp.used_namespaces(spec["policies"]):
        if child.get("Namespace", None, ns) is None:
            out.append(mf.finding("team-namespace-missing", namespace=ns))
    return out


def _taken(child, objs, item, cluster):
    """Revue 1-3, I2 : un objet du contenu déjà présent dans le cluster
    enfant sans les étiquettes de cet élément bloque (le CRS le
    réécrirait, puis le retrait le supprimerait)."""
    out = []
    for o in objs:
        ref = mf.ref_of(o)
        try:
            cur = child.get(mf.resource_of(ref), ref[2] or None, ref[3])
        except KubeError as e:
            if is_missing_type(e):
                continue                      # sorte absente : rien n'existe encore
            raise
        if cur is not None and not mf.is_ours(cur, item, cluster):
            out.append(mf.finding("manifest-object-taken", object=f"{ref[1]} {ref[2] + '/' if ref[2] else ''}{ref[3]}"))
    return out


def _child_findings(kube, spec, objs):
    """Contrôles faits dans le cluster enfant, avec l'identité de la
    personne (son droit d'administrateur, m1) : sortes servies et portée
    (I1), objets déjà présents (I2), namespaces d'équipe (207). Lève
    ChildAccessError sans le droit."""
    ns, cluster, item = spec["namespace"], spec["cluster"], spec["item"]
    with ca.session(kube, ns, cluster) as path:
        child = Kube(path)
        scopes = mf.parse_api_resources(child.api_resources())
        found = mf.check(objs, cluster, item, scopes=scopes)
        if not mf.blocking(found):
            found += _taken(child, objs, item, cluster)
        if "policies" in spec:
            found += _team_namespaces(child, spec)
    return found


def _static_findings(kube, spec):
    """(objets, constats) sans le cluster enfant ; `manifest-kind-unknown`
    y est rejugé contre le cluster enfant, pas tranché ici."""
    objs, found = manifest_objects(spec)
    found = [f for f in found if f["code"] != "manifest-kind-unknown"]
    return objs, found + _manifest_owner(kube, spec)


def cmd_manifest_check(args):
    kube, _, _ = kube_from(args)
    spec = manifest_spec_from(args.spec)
    objs, found = _static_findings(kube, spec)
    if objs and not mf.blocking(found):
        try:
            found = [f for f in found if not f["code"].startswith("manifest-")] + _child_findings(kube, spec, objs)
        except ca.ChildAccessError as e:
            found.append(mf.finding(e.code, cluster=f"{spec['namespace']}/{spec['cluster']}"))
    elif objs:
        found += [f for f in manifest_objects(spec)[1] if f["code"] == "manifest-kind-unknown"]
    blocked = bool(mf.blocking(found))
    print(json.dumps({"blocked": blocked, "findings": found, "item": spec["item"], "objects": len(objs)}))
    return EXIT_BLOCKED if blocked else EXIT_OK


def _namespace_others(child, ns, item, cluster):
    """Objets d'un namespace du cluster enfant qui ne viennent pas de cet
    élément, toutes sortes listables comprises (revue 1-3, C1) ; KubeError
    si une sorte ne se liste pas (l'appelant garde le namespace)."""
    kinds = mf.scan_resources(child.api_resources(namespaced=True, verbs="list").split())
    inside = child.list(",".join(kinds), ns, missing_ok=False) if kinds else []
    return mf.foreign(inside, item, cluster)


def _judge(child, refs, item, cluster, sid):
    """Passe 1 du retrait, sans écriture : chaque objet lu, puis sa
    suppression jugée par le serveur. Rend (à supprimer, refus, gardés) ;
    un objet introuvable compte comme retiré, un objet sans étiquette est
    gardé."""
    todo, refused, kept = [], [], []
    for ref in mf.delete_order(refs):
        res, where = mf.resource_of(ref), f"{ref[1]} {ref[2] + '/' if ref[2] else ''}{ref[3]}"
        try:
            obj = child.get(res, ref[2] or None, ref[3])
        except KubeError as e:
            if not is_missing_type(e):
                raise
            # revue 1-3, I5 : la sorte n'est plus servie (CRD retirée avec
            # son chart, ordre de retrait 211) : l'objet est parti avec elle
            step(sid, "running", f"{where}: kind no longer served by {cluster}, gone with it")
            continue
        if obj is None:
            continue
        if not mf.is_ours(obj, item, cluster):
            step(sid, "running", f"kept-relabelled: {where} kept, modified by hand (labels of the console removed)")
            kept.append({"object": where, "code": "kept-relabelled"})
            continue
        if ref[1] == "Namespace":
            # revue 1-3, C1 : toutes les sortes listables, échec fermé
            try:
                others = _namespace_others(child, ref[3], item, cluster)
            except KubeError as e:
                step(sid, "running", f"kept-in-use: {where} kept, its content could not be listed: {str(e)[:120]}")
                kept.append({"object": where, "code": "kept-in-use"})
                continue
            if others:
                step(sid, "running", f"kept-in-use: {where} kept, it holds {len(others)} object(s) "
                                     "not delivered by this item")
                kept.append({"object": where, "code": "kept-in-use"})
                continue
        try:
            child.delete(res, ref[2] or None, ref[3], dry_run=True)
        except KubeError as e:
            refused.append(f"{where}: {str(e)[:160]}")
            continue
        todo.append((res, ref, where))
    return todo, refused, kept


def _prune(child, todo, sid):
    for res, ref, where in todo:
        try:
            child.delete(res, ref[2] or None, ref[3])
        except KubeError as e:
            if not _not_found(e):
                raise
        step(sid, "running", f"{where} deleted")
    return len(todo)


def _wait_binding(kube, ns, cluster, cm_doc, timeout, now=time.time, sleep=time.sleep):
    """Attend que le binding du cluster porte l'empreinte du ConfigMap."""
    deadline, last = now() + timeout, None
    name = cm_doc["metadata"]["name"]
    while True:
        crs = kube.get(mf.K_CRS, ns, name)
        binding = kube.get(mf.K_CRSB, ns, cluster)
        st = mf.state(crs, [binding] if binding else [], cm_doc)
        if st["applied"]:
            return st
        if st["message"] != last:
            step("reconcile", "running", st["message"])
            last = st["message"]
        if now() > deadline:
            raise TimeoutError(st["message"])
        sleep(5)


def cmd_manifest_deploy(args):
    """Déclare ConfigMap et CRS, sélectionne le cluster, attend le binding à
    l'empreinte du nouveau contenu, puis retire du cluster enfant les objets
    sortis du contenu qui portent encore les étiquettes de la console."""
    kube, _, _ = kube_from(args)
    spec = manifest_spec_from(args.spec)
    ns, cluster, item = spec["namespace"], spec["cluster"], spec["item"]
    objs, found = _static_findings(kube, spec)
    if not mf.blocking(found):
        # toujours dans le cluster enfant : droit de la personne (m1), sortes
        # servies (I1), objets déjà présents (I2), namespaces d'équipe (207)
        found = [f for f in found if not f["code"].startswith("manifest-")] + _child_findings(kube, spec, objs)
    if mf.blocking(found):
        for f in mf.blocking(found):
            step("check", "error", f"{f['code']} {json.dumps(f['facts'])}")
        return EXIT_BLOCKED
    step("check", "done", f"{item} ({spec['package']} {spec['version']}): {len(objs)} object(s) for {ns}/{cluster}")
    old_cm = kube.get("configmaps", ns, mf.name_of(item))
    # I3 : ce qui sort du contenu est écrit AVANT l'application ; un
    # déploiement arrêté le retire à la reprise
    pending = mf.pending_after(old_cm, objs)
    docs = mf.docs(ns, cluster, item, spec["package"], spec["version"], objs, spec["origin"], spec["by"])
    docs[0] = mf.with_pending(docs[0], pending)
    kube.apply(docs)
    kube.patch(cs.K_CLUSTER, ns, cluster, {"metadata": {"labels": {mf.label_of(item): "on"}}})
    step("apply", "done", f"{mf.name_of(item)} declared, cluster {cluster} selected")
    try:
        _wait_binding(kube, ns, cluster, docs[0], args.timeout)
    except TimeoutError as e:
        step("reconcile", "error", f"not applied after {args.timeout} s: {e}")
        return EXIT_FAIL
    step("reconcile", "done", f"{mf.name_of(item)} applied to {cluster}")
    if not pending:
        step("prune", "done", "no object left the content")
        return EXIT_OK
    with ca.session(kube, ns, cluster) as path:
        child = Kube(path)
        todo, refused, _kept = _judge(child, pending, item, cluster, "prune")
        if refused:
            step("prune", "error", "the server refuses: " + "; ".join(refused[:4]))
            return EXIT_FAIL
        n = _prune(child, todo, "prune")
    kube.patch("configmaps", ns, mf.name_of(item), {"metadata": {"annotations": {mf.A_PENDING: None}}})
    step("prune", "done", f"{n} object(s) that left the content deleted from {cluster}")
    return EXIT_OK


def cmd_manifest_remove(args):
    """CRS d'abord (plus de réapplication), étiquette du Cluster ôtée, objets
    du ConfigMap supprimés dans le cluster enfant (seulement s'ils portent
    les étiquettes), puis ConfigMap. Les suppressions sont jugées par le
    serveur avant toute écriture."""
    kube, _, _ = kube_from(args)
    ns, cluster, item = _child_name(args.name, 3)
    name = mf.name_of(item)
    crs, cmap = kube.get(mf.K_CRS, ns, name), kube.get("configmaps", ns, name)
    if crs is None and cmap is None:
        if getattr(args, "dry_run", False):
            print(json.dumps({"item": item, "cluster": f"{ns}/{cluster}", "absent": True,
                              "delete": [], "kept": [], "refused": []}))
            return EXIT_OK
        step("remove", "done", f"{item} is not applied to {ns}/{cluster}")
        return EXIT_OK
    for o in (crs, cmap):
        lab = ((o or {}).get("metadata") or {}).get("labels") or {}
        if o is not None and (lab.get(mf.L_CLUSTER) != cluster or lab.get(mf.L_ITEM) != item):
            step("remove", "error", f"artefact-taken: {ns}/{name} belongs to {lab.get(mf.L_CLUSTER) or 'something else'}")
            return EXIT_BLOCKED
    refused = cc.run_mismatch(crs or cmap, getattr(args, "expect_run", None))
    if refused:
        step("remove", "error", refused)
        return EXIT_BLOCKED
    # I3 : ce qu'un déploiement interrompu n'a pas encore retiré compte aussi
    objs = mf.objects_of(cmap)
    refs = [mf.ref_of(o) for o in objs]
    refs += [r for r in mf.pending_of(cmap) if r not in refs]
    present = kube.get(cs.K_CLUSTER, ns, cluster) is not None
    # revue 1-3 m5 : ConfigMap supprimé à la main, la liste des objets est
    # perdue ; ses objets étiquetés restent dans le cluster enfant, c'est dit
    lost = [f"object list lost (ConfigMap {ns}/{name} missing): the labelled objects of {item} "
            f"stay in {cluster}"] if cmap is None and present else []
    if getattr(args, "dry_run", False) and not (present and refs):
        print(json.dumps({"item": item, "cluster": f"{ns}/{cluster}", "cluster_present": present,
                          "delete": [], "kept": lost, "refused": []}))
        return EXIT_OK
    if lost:
        step("remove", "running", lost[0])
    todo = []
    with contextlib.ExitStack() as stack:
        child = None
        kept = []
        if present and refs:
            child = Kube(stack.enter_context(ca.session(kube, ns, cluster)))
            todo, refused, kept = _judge(child, refs, item, cluster, "remove")
            if getattr(args, "dry_run", False):
                # 2.7.0 (plan de destruction du Défaire) : jugé par le
                # serveur, rien n'est écrit
                print(json.dumps({"item": item, "cluster": f"{ns}/{cluster}", "cluster_present": True,
                                  "delete": [w for _r, _ref, w in todo], "kept": kept, "refused": refused}))
                return EXIT_OK
            if refused:
                step("remove", "error", "the server refuses: " + "; ".join(refused[:4]) + "; nothing deleted")
                return EXIT_FAIL
        if crs is not None:
            # orphan : Cluster API pose sur le ConfigMap une référence de
            # propriétaire vers le CRS ; sans cela, le ConfigMap (la liste
            # des objets à retirer) partirait avec lui (vu sur harvlab2)
            kube.delete(mf.K_CRS, ns, name, cascade="orphan")
            step("remove", "running", f"ClusterResourceSet {ns}/{name} deleted")
        if present:
            kube.patch(cs.K_CLUSTER, ns, cluster, {"metadata": {"labels": {mf.label_of(item): None}}})
            n = _prune(child, todo, "remove") if child else 0
            step("remove", "running", f"{n} object(s) deleted from {cluster}")
        else:
            step("remove", "running", f"cluster {ns}/{cluster} is gone: its objects left with it")
    if cmap is not None:
        try:
            kube.delete("configmaps", ns, name)
        except KubeError as e:
            if not _not_found(e):
                raise
    step("remove", "done", f"{item} removed from {ns}/{cluster}")
    return EXIT_OK


def cmd_manifest_verify(args):
    """2.7.0 (tâche 4) : un élément appliqué est-il entier ? Le binding à
    l'empreinte du ConfigMap ne le prouve pas (un objet supprimé à la main
    n'est jamais recréé, essai 1) : chaque objet du contenu est relu dans
    le cluster enfant, avec ses étiquettes, et rien n'attend d'être retiré.
    Rend {present, applied, pending, missing, relabelled, ok, origin,
    version} ; lecture seule."""
    kube, _, _ = kube_from(args)
    ns, cluster, item = _child_name(args.name, 3)
    name = mf.name_of(item)
    crs, cmap = kube.get(mf.K_CRS, ns, name), kube.get("configmaps", ns, name)
    out = {"present": crs is not None or cmap is not None, "applied": False, "pending": 0,
           "missing": [], "relabelled": [], "ok": False, "origin": cc.origin_of(crs or cmap),
           "version": (mf.artefact_of(crs or cmap) or {}).get("version") or ""}
    if crs is not None and cmap is not None and kube.get(cs.K_CLUSTER, ns, cluster) is not None:
        binding = kube.get(mf.K_CRSB, ns, cluster)
        out["applied"] = mf.state(crs, [binding] if binding else [], cmap)["applied"]
        out["pending"] = len(mf.pending_of(cmap))
        with ca.session(kube, ns, cluster) as path:
            child = Kube(path)
            for o in mf.objects_of(cmap):
                ref = mf.ref_of(o)
                where = f"{ref[1]} {ref[2] + '/' if ref[2] else ''}{ref[3]}"
                try:
                    cur = child.get(mf.resource_of(ref), ref[2] or None, ref[3])
                except KubeError as e:
                    if not is_missing_type(e):
                        raise
                    cur = None
                if cur is None:
                    out["missing"].append(where)
                elif not mf.is_ours(cur, item, cluster):
                    out["relabelled"].append(where)
        out["ok"] = out["applied"] and not out["pending"] and not out["missing"] and not out["relabelled"]
    print(json.dumps(out))
    return EXIT_OK


def cmd_manifests(args):
    """État de chaque élément appliqué (CRS de la console, binding, empreinte)."""
    kube, _, _ = kube_from(args)
    out = []
    crss = kube.list(mf.K_CRS, selector=mf.L_ITEM)
    bindings = {}
    for b in kube.list(mf.K_CRSB):
        bindings.setdefault(b["metadata"].get("namespace"), []).append(b)
    for crs in crss:
        md = crs["metadata"]
        ns, lab, ann = md.get("namespace"), md.get("labels") or {}, md.get("annotations") or {}
        cmap = kube.get("configmaps", ns, md["name"])
        objs = mf.objects_of(cmap)
        row = {"namespace": ns, "cluster": lab.get(mf.L_CLUSTER), "item": lab.get(mf.L_ITEM),
               "package": lab.get(mf.L_PACKAGE), "version": ann.get(mf.A_VERSION),
               "applied_by": ann.get(mf.A_BY) or "", "origin": cc.origin_of(crs),
               "objects": len(objs), "state": mf.state(crs, bindings.get(ns, []), cmap)}
        if row["item"] == cp.ITEM:
            # 2.7.0 : comptes d'automatisation déclarés par la console (lus
            # dans le ConfigMap, sans accès au cluster enfant) et leur rôle
            roles = {((o.get("metadata") or {}).get("namespace"), (o.get("metadata") or {}).get("name")):
                     (o.get("roleRef") or {}).get("name") for o in objs if o.get("kind") == "RoleBinding"}
            row["accounts"] = [{"namespace": o["metadata"].get("namespace"), "name": o["metadata"].get("name"),
                                "role": roles.get((o["metadata"].get("namespace"),
                                                   "hops-sa-" + str(o["metadata"].get("name"))))}
                               for o in objs if o.get("kind") == "ServiceAccount" and o.get("metadata")]
        out.append(row)
    print(json.dumps(out) if args.json else json.dumps(out, indent=1))
    return EXIT_OK


def cmd_policy_render(args):
    raw = json.load(sys.stdin) if args.spec == "-" else json.loads(Path(args.spec).read_text())
    print(json.dumps(cp.render(cp.normalize(raw))))
    return EXIT_OK


def cmd_token(args):
    """Jeton `TokenRequest` d'un compte d'automatisation : sur la sortie
    standard seulement ({token, expires_at}), jamais en étape."""
    seconds = ca.check_seconds(args.seconds)
    if len(args.by) > BY_MAX or any(c in args.by for c in "\n\r|"):
        # revue 1-3, m4 : `--by` est écrit dans une étape
        raise ValueError(f"--by: one line, {BY_MAX} characters at most, no '|'")
    ns, cluster = _child_name(args.name)
    tns, account = _child_name(args.account)
    kube, _, _ = kube_from(args)
    with ca.session(kube, ns, cluster) as path:
        child = Kube(path)
        sa = child.get("serviceaccounts", tns, account)
        if sa is None:
            step("token", "error", f"account-not-found: {tns}/{account} on {cluster}")
            return EXIT_BLOCKED
        if not mf.is_ours(sa, cp.ITEM, cluster):
            step("token", "error", f"account-not-managed: {tns}/{account} was not declared by the console")
            return EXIT_BLOCKED
        token, expires = ca.token(child, tns, account, seconds)
    sys.stdout.write(json.dumps({"token": token, "expires_at": expires}) + "\n")
    sys.stdout.flush()
    step("token", "done", f"token issued for {tns}/{account} on {cluster}, {seconds} s, by {args.by or '?'}")
    return EXIT_OK


CHILD_COMMANDS = ("manifest-check", "manifest-deploy", "manifest-remove", "manifest-verify", "token")


def _exit_cancelled(signum, frame):
    raise SystemExit(EXIT_CANCELLED)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="harvester-capi", allow_abbrev=False,
                                 description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--cluster", help="cluster name in the configuration")
        sp.add_argument("--kubeconfig", help="kubeconfig file of the Harvester cluster "
                                             "(overrides the one of --cluster)")
        sp.add_argument("--harvester-name", help="name of the Harvester cluster in the console "
                                                 "(registries attached to it; default --cluster)")
        sp.add_argument("--state-dir", help="console state directory (registries, packages)")

    for name, fn, extra in (
            ("status", cmd_status, ("components", "json")),
            ("install", cmd_install, ("components", "install")),
            ("cleanup-legacy", cmd_cleanup, ("dry",)),
            ("uninstall", cmd_uninstall, ("dry", "json", "uninstall")),
            ("inventory", cmd_inventory, ("components", "json")),
            ("check", cmd_check, ("spec", "components", "json", "identity")),
            ("render", cmd_render, ("spec", "secrets", "identity")),
            ("create", cmd_create, ("spec", "components", "json", "dry", "timeout", "identity")),
            ("delete", cmd_delete, ("name", "timeout", "dry")),
            ("state", cmd_state, ("name", "json")),
            ("services", cmd_services, ("json",)),
            ("service-check", cmd_service_check, ("sspec",)),
            ("service-deploy", cmd_service_deploy, ("sspec", "timeout")),
            ("service-remove", cmd_service_remove, ("sname", "timeout")),
            ("manifest-check", cmd_manifest_check, ("mspec",)),
            ("manifest-deploy", cmd_manifest_deploy, ("mspec", "timeout")),
            ("manifest-remove", cmd_manifest_remove, ("mname", "dry")),
            ("manifest-verify", cmd_manifest_verify, ("mname",)),
            ("manifests", cmd_manifests, ("json",)),
            ("policy-render", cmd_policy_render, ("pspec",)),
            ("token", cmd_token, ("token",))):
        sp = sub.add_parser(name, allow_abbrev=False)
        common(sp)
        sp.set_defaults(fn=fn)
        if "components" in extra:
            sp.add_argument("--components", help="extracted bundle directory or bundle .tar.gz")
        if "json" in extra:
            sp.add_argument("--json", action="store_true")
        if "spec" in extra:
            sp.add_argument("--spec", required=True, help="JSON request, '-' for stdin")
        if "dry" in extra:
            sp.add_argument("--dry-run", action="store_true")
        if "secrets" in extra:
            sp.add_argument("--show-secrets", action="store_true",
                            help="include the identity secret (the Harvester kubeconfig)")
        if "install" in extra:
            sp.add_argument("--push-images", action="store_true",
                            help="import the bundle images on every node (airgap)")
            sp.add_argument("--no-wait", action="store_true")
            sp.add_argument("--timeout", type=int, default=300)
        if "uninstall" in extra:
            sp.add_argument("--remove-images", action="store_true",
                            help="also remove the images of the removed providers from every node (SSH)")
            sp.add_argument("--timeout", type=int, default=600,
                            help="seconds to wait for the deployments of each provider to go")
            sp.add_argument("--id", default="", help="console action id, written in the cluster lock")
        if "timeout" in extra:
            sp.add_argument("--timeout", type=int,
                            default=900 if name.startswith(("service", "manifest")) else 2400)
            if name == "create":
                sp.add_argument("--no-wait", action="store_true")
        if name in ("delete", "service-remove", "manifest-remove"):
            sp.add_argument("--expect-run", default="", metavar="ID",
                            help="refuse unless the object carries this plan run (the undo of a plan)")
        if "name" in extra:
            sp.add_argument("--name", required=True, help="namespace/name of the cluster")
        if "sname" in extra:
            sp.add_argument("--name", required=True, help="namespace/name of the service")
        if "mspec" in extra:
            sp.add_argument("--spec", required=True, help="JSON request (cluster, package, version, item "
                                                          "or policies), '-' for stdin")
        if "mname" in extra:
            sp.add_argument("--name", required=True, help="namespace/cluster/item")
        if "pspec" in extra:
            sp.add_argument("--spec", required=True, help="JSON policies request, '-' for stdin")
        if "token" in extra:
            sp.add_argument("--name", required=True, help="namespace/cluster of the child cluster")
            sp.add_argument("--account", required=True, help="namespace/name of the automation account")
            sp.add_argument("--seconds", type=int, default=ca.TOKEN_DEFAULT,
                            help=f"{ca.TOKEN_MIN} to {ca.TOKEN_MAX}, default {ca.TOKEN_DEFAULT}")
            sp.add_argument("--by", default="", help="person who asks (written in the step)")
        if "sspec" in extra:
            sp.add_argument("--spec", required=True, help="JSON service request, '-' for stdin")
        if name == "check":
            sp.add_argument("--many", action="store_true",
                            help="--spec is a JSON list of requests, checked together (capacity, pools, rights)")
        if "identity" in extra:
            sp.add_argument("--identity", choices=ci.IDENTITIES, default="restricted",
                            help="restricted: a CAPHV identity of its own for the cluster (default); "
                                 "console: the console kubeconfig, for a diagnosis")
            if name == "create":
                sp.add_argument("--origin", default="",
                                help="plan=P,template=M,run=ID: origin labels set on the Cluster")
    args = ap.parse_args(argv)
    if getattr(args, "state_dir", None):
        os.environ["HARVESTER_OPS_STATE_DIR"] = os.path.abspath(args.state_dir)
    if args.cmd in CHILD_COMMANDS:
        # revue 1-3, I4 : l'arrêt de la console (SIGTERM) passe par les
        # `finally` : le kubeconfig du cluster enfant est effacé
        signal.signal(signal.SIGTERM, _exit_cancelled)
        signal.signal(signal.SIGINT, _exit_cancelled)
    try:
        return args.fn(args)
    except ValueError as e:
        step(args.cmd, "error", str(e)[:300])
        return EXIT_BLOCKED
    except ca.ChildAccessError as e:
        step(args.cmd, "error", f"{e.code}: {str(e)[:300]}")
        return EXIT_BLOCKED
    except KubeError as e:
        step(args.cmd, "error", str(e)[:300])
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
