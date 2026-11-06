#!/usr/bin/env python3
"""Clusters RKE2 sur Harvester par Cluster API : la pile, puis les clusters.

    harvester-capi status    --cluster C [--components DIR] [--json]
    harvester-capi install   --cluster C --components DIR [--push-images]
    harvester-capi cleanup-legacy --cluster C [--dry-run]
    harvester-capi inventory --cluster C [--components DIR] --json
    harvester-capi check     --cluster C --spec FICHIER [--components DIR] [--identity I] [--many] [--json]
    harvester-capi render    --cluster C --spec FICHIER [--show-secrets]
    harvester-capi create    --cluster C --spec FICHIER [--components DIR] [--timeout S]
                             [--identity restricted|console] [--origin plan=P,template=M,run=ID]
    harvester-capi delete    --cluster C --name ns/nom [--timeout S]
    harvester-capi state     --cluster C --name ns/nom [--json]

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

Progression sur stderr au format STEP_EVENT|<étape>|<statut>|<message>, lue
par la console. Codes de sortie : 0 succès, 1 échec, 2 refus du contrôle
préalable, 3 annulation.

Bibliothèque standard seulement (plus kubectl et yq) : l'outil tourne sur un
hôte airgap.

Voir docs/design/2026-09-25-creation-cluster-capi.md.
"""

import argparse
import base64
import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import capi_cluster as cc  # noqa: E402
import capi_identity as ci  # noqa: E402
import capi_stack as cs  # noqa: E402
import capi_services as sv  # noqa: E402
import longhorn_room as lr  # noqa: E402
import node_images  # noqa: E402
import registries as rg  # noqa: E402
from kube import Kube, KubeError, cluster_config  # noqa: E402

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
    `HARVESTER_OPS_WORK_DIR` plutôt que /tmp (en mémoire dans le service)."""
    if not path:
        return None
    p = Path(path)
    if p.is_file():
        work = Path(tempfile.mkdtemp(prefix="hops-capi-", dir=os.environ.get("HARVESTER_OPS_WORK_DIR")))
        p = _extract_turtles(p, work)
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
    return dict(base, **{
        "unreachable": False,
        "stack": stack,
        "namespaces": sorted(n["metadata"]["name"] for n in kube.list("namespaces")),
        "clusters": sorted(_ref(c) for c in kube.list(cs.K_CLUSTER)),
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
    kube, _, entry = kube_from(args)
    desc = components_from(args.components)
    if desc is None:
        raise SystemExit("--components is required")
    if args.push_images:
        push_images(entry, desc)
    try:
        cs.install(kube, desc, step, wait=not args.no_wait, timeout=args.timeout)
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
                          rg.system_ready_registries(_state(), decls))


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
    prc, files, secrets = rg.rke2_config(spec["registries"], decls,
                                         {"name": spec["name"], "namespace": spec["namespace"]}, creds)
    return {"config": prc, "files": files, "secrets": secrets}


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
    """Fichier 0600 de l'espace de travail (HARVESTER_OPS_WORK_DIR) ; le
    chemin n'est jamais écrit sur stdout, stderr ni dans une étape."""
    fd, path = tempfile.mkstemp(prefix="hops-caphv-", suffix=".kubeconfig",
                                dir=os.environ.get("HARVESTER_OPS_WORK_DIR"))
    with os.fdopen(fd, "w") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(content)
    return path


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
            step("registries", "done", f"{len(spec['registries'])} registry(ies): mirrors "
                                       f"{', '.join(sorted(regs['config'].get('mirrors') or {})) or '-'}"
                                       f"{', no fallback to Internet' if regs['files'] else ''}")
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


def remove_cluster(kube, ns, name, timeout, sleep=None, now=None):
    """Ordre imposé : le Cluster, attente de sa disparition (CAPHV a besoin
    de son identité pour supprimer VMs, volumes, répartiteur et pool), puis
    l'identité, puis ce que la console a rendu et le namespace qu'elle a
    créé. Rend un code de sortie."""
    now, sleep = now or time.time, sleep or time.sleep
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
    if not others:
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


def cmd_delete(args):
    kube, _, _ = kube_from(args)
    ns, _, name = args.name.partition("/")
    if not ns or not name:
        raise SystemExit("--name expects namespace/name")
    cl = kube.get(cs.K_CLUSTER, ns, name)
    if cl is None:
        # Cluster déjà parti, identité restée (suppression précédente qui a
        # échoué sur un droit) : la reprise finit le nettoyage
        sa = kube.get("serviceaccounts", ns, ci.IDENTITY)
        if sa is None or ((sa.get("metadata") or {}).get("labels") or {}).get(ci.L_CLUSTER) != name:
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
    clusters = [f"{c['metadata']['namespace']}/{c['metadata']['name']}"
                for c in kube.list(cs.K_CLUSTER)
                if c["metadata"].get("namespace") not in ("fleet-local", "fleet-default")]
    try:
        hcps = kube.list(sv.K_HCP)
        hrps = kube.list(sv.K_HRP)
    except KubeError:
        hcps, hrps = [], []
    return {"caaph": _caaph_ready(kube), "clusters": sorted(clusters),
            "no_lb": _clusters_without_kube_vip(kube),
            "services": [f"{h['metadata']['namespace']}/{h['metadata']['name']}" for h in hcps],
            "_hcps": hcps, "_hrps": hrps}


def service_spec_from(path):
    raw = json.load(sys.stdin) if path == "-" else json.loads(Path(path).read_text())
    return sv.normalize(raw)


def cmd_services(args):
    kube, _, _ = kube_from(args)
    f = _service_facts(kube)
    out = {"caaph": f["caaph"], "clusters": f["clusters"], "catalog": sv.catalog(),
           "services": sv.state(f["_hcps"], f["_hrps"])}
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
    kube.apply(docs + [sv.manifest(spec, registry)])
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
    step("remove", "done", f"service {ns}/{name} removed")
    return EXIT_OK


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
            ("inventory", cmd_inventory, ("components", "json")),
            ("check", cmd_check, ("spec", "components", "json", "identity")),
            ("render", cmd_render, ("spec", "secrets", "identity")),
            ("create", cmd_create, ("spec", "components", "json", "dry", "timeout", "identity")),
            ("delete", cmd_delete, ("name", "timeout")),
            ("state", cmd_state, ("name", "json")),
            ("services", cmd_services, ("json",)),
            ("service-check", cmd_service_check, ("sspec",)),
            ("service-deploy", cmd_service_deploy, ("sspec", "timeout")),
            ("service-remove", cmd_service_remove, ("sname", "timeout"))):
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
        if "timeout" in extra:
            sp.add_argument("--timeout", type=int,
                            default=900 if name.startswith("service") else 2400)
            if name == "create":
                sp.add_argument("--no-wait", action="store_true")
        if "name" in extra:
            sp.add_argument("--name", required=True, help="namespace/name of the cluster")
        if "sname" in extra:
            sp.add_argument("--name", required=True, help="namespace/name of the service")
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
    try:
        return args.fn(args)
    except ValueError as e:
        step(args.cmd, "error", str(e)[:300])
        return EXIT_BLOCKED
    except KubeError as e:
        step(args.cmd, "error", str(e)[:300])
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
