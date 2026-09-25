#!/usr/bin/env python3
"""Clusters RKE2 sur Harvester par Cluster API : la pile, puis les clusters.

    harvester-capi status    --cluster C [--components DIR] [--json]
    harvester-capi install   --cluster C --components DIR [--push-images]
    harvester-capi cleanup-legacy --cluster C [--dry-run]
    harvester-capi inventory --cluster C [--components DIR] --json
    harvester-capi check     --cluster C --spec FICHIER [--components DIR] [--json]
    harvester-capi render    --cluster C --spec FICHIER [--show-secrets]
    harvester-capi create    --cluster C --spec FICHIER [--components DIR] [--timeout S]
    harvester-capi delete    --cluster C --name ns/nom [--timeout S]

Le cluster Harvester sert lui-même de cluster de gestion : Harvester 1.9
embarque Rancher Turtles et son cœur Cluster API. `install` y déclare les
fournisseurs RKE2 et CAPHV du paquet (`--components` : un paquet extrait, ou
le fichier .tar.gz), sans rien descendre d'Internet.

Une demande (`--spec`, JSON, `-` pour l'entrée standard) porte les options
de bin/lib/capi_cluster.py ; les manifestes sont produits par
`caphv-generate` (livré à côté, dépôt CAPHV) puis corrigés. `--cluster`
nomme un cluster de la configuration (`HARVESTER_OPS_CONFIG`), ou
`--kubeconfig` un fichier.

Progression sur stderr au format STEP_EVENT|<étape>|<statut>|<message>, lue
par la console. Codes de sortie : 0 succès, 1 échec, 2 refus du contrôle
préalable, 3 annulation.

Bibliothèque standard seulement (plus kubectl et yq) : l'outil tourne sur un
hôte airgap.

Voir docs/design/2026-09-25-creation-cluster-capi.md.
"""

import argparse
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
import capi_stack as cs  # noqa: E402
from kube import Kube, KubeError, cluster_config  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_BLOCKED, EXIT_CANCELLED = 0, 1, 2, 3
GENERATOR = Path(os.environ.get("HARVESTER_OPS_CAPHV_GEN", str(HERE / "caphv-generate")))


class Cancelled(Exception):
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
            "virtual_size": st.get("virtualSize"), "os": hint}


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
        info = {"ready": i["ready"], "iso": i["iso"], "os": i.get("os")}
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
    scs = []
    for s in kube.list("storageclasses"):
        ann = (s.get("metadata") or {}).get("annotations") or {}
        name = s["metadata"]["name"]
        if name.startswith(("longhorn-image-", "lh-")):
            continue                              # classes propres à une image
        scs.append({"name": name,
                    "default": ann.get("storageclass.kubernetes.io/is-default-class") == "true"})
    free_cpu, free_mem = _free_capacity(kube)
    return dict(base, **{
        "unreachable": False,
        "stack": stack,
        "namespaces": sorted(n["metadata"]["name"] for n in kube.list("namespaces")),
        "clusters": sorted(_ref(c) for c in kube.list(cs.K_CLUSTER)),
        "images": sorted(images, key=_image_rank),
        "keypairs": sorted(_ref(k) for k in kube.list("keypairs.harvesterhci.io")),
        "networks": sorted(nets, key=lambda n: n["ref"]),
        "storage_classes": scs, "pools": pools,
        "free_cpu_m": free_cpu, "free_memory": free_mem, "overcommit": _overcommit(kube),
    })


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

def render(spec, identity_kubeconfig):
    """Les documents du cluster, corrigés (liste de dict)."""
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
    out = cc.postprocess(docs, spec)
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
    chaque nœud, par SSH (comme l'installation d'avant Turtles)."""
    if not entry:
        raise SystemExit("--push-images needs --cluster (nodes and SSH come from the configuration)")
    ssh = entry.get("ssh") or {}
    nodes = [n.get("ip") for n in entry.get("nodes") or [] if n.get("ip")]
    files = [f for p in desc["providers"] for f in p.get("image_files") or []]
    step("images", "running", f"{len(files)} image(s) to load on {len(nodes)} node(s)")
    remote = ("set -eo pipefail; TMP=$(mktemp); cat > \"$TMP\"; "
              "{ zcat \"$TMP\" 2>/dev/null || cat \"$TMP\"; } | sudo /var/lib/rancher/rke2/bin/ctr "
              "--address /run/k3s/containerd/containerd.sock --namespace k8s.io images import -; "
              "rm -f \"$TMP\"")
    opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new", "-p", str(ssh.get("port", 22))]
    if ssh.get("key"):
        opts += ["-i", ssh["key"]]
    for node in nodes:
        for f in files:
            with open(f, "rb") as fh:
                p = subprocess.run(["ssh", *opts, f"{ssh.get('user', 'rancher')}@{node}", remote],
                                   stdin=fh, capture_output=True, timeout=600)
            if p.returncode != 0:
                raise RuntimeError(f"{node}: import of {Path(f).name} failed")
            step("images", "running", f"{node}: {Path(f).name}")
    step("images", "done", f"{len(files) * len(nodes)} image import(s)")


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


def _check(kube, spec, desc):
    inv = inventory(kube, desc)
    if inv.get("unreachable"):
        findings = [cc.finding("cluster-unreachable", "block")]
        return {"findings": findings, "blocked": True, "amount": amount(spec), "spec": spec}
    findings = cc.check(spec, facts_from(inv))
    return {"findings": findings, "blocked": bool(cc.blocking(findings)),
            "amount": amount(spec, inv["overcommit"]), "spec": spec}


def cmd_check(args):
    kube, _, _ = kube_from(args)
    try:
        spec = spec_from(args.spec)
    except ValueError as e:
        out = {"findings": [cc.finding("invalid", "block", option="-", message=str(e))],
               "blocked": True}
        print(json.dumps(out) if args.json else str(e))
        return EXIT_BLOCKED
    out = _check(kube, spec, components_from(args.components))
    if args.json:
        print(json.dumps(out))
    else:
        for f in out["findings"]:
            print(f"[{f['level']}] {f['code']} {json.dumps(f['facts'])}")
    return EXIT_BLOCKED if out["blocked"] else EXIT_OK


def cmd_render(args):
    _, kc, _ = kube_from(args)
    spec = spec_from(args.spec)
    errs = cc.validate(spec)
    if errs:
        for k, m in errs:
            sys.stderr.write(f"{k}: {m}\n")
        return EXIT_BLOCKED
    docs = render(spec, kc)
    print(as_yaml(docs if args.show_secrets else cc.masked(docs)))
    return EXIT_OK


def cmd_create(args):
    kube, kc, _ = kube_from(args)
    spec = spec_from(args.spec)
    desc = components_from(args.components)
    out = _check(kube, spec, desc)
    blockers = cc.blocking(out["findings"])
    if blockers:
        step("check", "error", "the pre-check refuses: " + ", ".join(f["code"] for f in blockers))
        if args.json:
            print(json.dumps(out))
        return EXIT_BLOCKED
    step("check", "done", f"{out['amount']['vms']} VM(s), {out['amount']['addresses']} address(es)")
    ns, name = spec["namespace"], spec["name"]
    ns_existed = ns in inventory_namespaces(kube)
    docs = render(spec, kc)
    step("render", "done", f"{len(docs)} object(s) for {ns}/{name}")
    if args.dry_run:
        print(as_yaml(cc.masked(docs)))
        return EXIT_OK
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    try:
        kube.apply(docs, timeout=120)
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
        _undo(kube, ns, name, ns_existed)
        return EXIT_CANCELLED
    except TimeoutError as e:
        step("provision", "error", f"not available in time ({str(e)[:200]}); "
                                   "the cluster is left in place for inspection")
        return EXIT_FAIL
    except (RuntimeError, KubeError) as e:
        step("provision", "error", str(e)[:300])
        return EXIT_FAIL
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.SIG_DFL)


def inventory_namespaces(kube):
    try:
        return {n["metadata"]["name"] for n in kube.list("namespaces")}
    except KubeError:
        return set()


def _undo(kube, ns, name, ns_existed):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        kube.delete(cs.K_CLUSTER, ns, name)
        if not ns_existed:
            kube.delete("namespaces", None, ns)
    except KubeError as e:
        step("cancel", "error", f"could not delete {ns}/{name}: {str(e)[:200]}")


def cmd_delete(args):
    kube, _, _ = kube_from(args)
    ns, _, name = args.name.partition("/")
    if not ns or not name:
        raise SystemExit("--name expects namespace/name")
    cl = kube.get(cs.K_CLUSTER, ns, name)
    if cl is None:
        step("delete", "error", f"cluster {ns}/{name} not found")
        return EXIT_FAIL
    kube.delete(cs.K_CLUSTER, ns, name)
    step("delete", "running", f"{ns}/{name}: machines and VMs being removed")
    deadline = time.time() + args.timeout
    while kube.get(cs.K_CLUSTER, ns, name) is not None:
        if time.time() > deadline:
            step("delete", "error", f"{ns}/{name} still deleting after {args.timeout} s")
            return EXIT_FAIL
        left = len([m for m in kube.list("machines.cluster.x-k8s.io", ns)
                    if ((m.get("metadata") or {}).get("labels") or {})
                    .get("cluster.x-k8s.io/cluster-name") == name])
        step("delete", "running", f"{ns}/{name}: {left} machine(s) left")
        time.sleep(10)
    nsobj = kube.get("namespaces", None, ns) or {}
    others = [c for c in kube.list(cs.K_CLUSTER, ns)]
    if not others:
        # Ce que la console a rendu à côté du Cluster : ClusterClass, gabarits,
        # compléments, et le secret d'identité qui porte un kubeconfig Harvester.
        gone = 0
        for kind in cc.GENERATED_KINDS:
            for obj in kube.list(kind, ns, selector=f"{cc.GENERATED}=true"):
                try:
                    kube.delete(kind, ns, obj["metadata"]["name"])
                    gone += 1
                except KubeError as e:
                    step("delete", "running", f"{kind} {obj['metadata']['name']} kept: {e}")
        if gone:
            step("delete", "running", f"{gone} generated object(s) removed from {ns}")
        if ((nsobj.get("metadata") or {}).get("labels") or {}).get(cs.MANAGED) == "true":
            kube.delete("namespaces", None, ns)
            step("delete", "running", f"namespace {ns} removed (created by the console)")
    step("delete", "done", f"{ns}/{name} deleted")
    return EXIT_OK


def main(argv=None):
    ap = argparse.ArgumentParser(prog="harvester-capi", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--cluster", help="cluster name in the configuration")
        sp.add_argument("--kubeconfig", help="kubeconfig file of the Harvester cluster "
                                             "(overrides the one of --cluster)")

    for name, fn, extra in (
            ("status", cmd_status, ("components", "json")),
            ("install", cmd_install, ("components", "install")),
            ("cleanup-legacy", cmd_cleanup, ("dry",)),
            ("inventory", cmd_inventory, ("components", "json")),
            ("check", cmd_check, ("spec", "components", "json")),
            ("render", cmd_render, ("spec", "secrets")),
            ("create", cmd_create, ("spec", "components", "json", "dry", "timeout")),
            ("delete", cmd_delete, ("name", "timeout"))):
        sp = sub.add_parser(name)
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
            sp.add_argument("--timeout", type=int, default=2400)
            if name == "create":
                sp.add_argument("--no-wait", action="store_true")
        if "name" in extra:
            sp.add_argument("--name", required=True, help="namespace/name of the cluster")
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except KubeError as e:
        step(args.cmd, "error", str(e)[:300])
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
