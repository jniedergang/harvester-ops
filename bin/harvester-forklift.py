#!/usr/bin/env python3
"""Forklift sur un cluster Harvester : installation, image VDDK, fournisseur vCenter, inventaire.

    harvester-forklift status          --cluster C
    harvester-forklift install         --cluster C [--chart-version V] [--image-tag T]
                                       [--cert-manager-manifest FICHIER | --cert-manager-from-bundle PAQUET]
    harvester-forklift vddk-image      --archive VDDK.tar.gz --image REGISTRE/DEPOT:TAG
                                       [--base IMAGE] [--plain-http] [--auth-stdin | --spec FICHIER]
                                       [--cluster C | --kubeconfig K]
    harvester-forklift provider-apply  --cluster C --namespace NS --name N   (JSON sur stdin ou --spec FICHIER)
    harvester-forklift provider-delete --cluster C --namespace NS --name N [--with-secret]
    harvester-forklift inventory       --cluster C --namespace NS --name N --kind vms|networks|datastores
    harvester-forklift wave-apply      --cluster C   (JSON de la vague sur stdin ou --spec FICHIER)
    harvester-forklift wave-start      --cluster C --wave W
    harvester-forklift wave-cutover    --cluster C --wave W [--at RFC3339]
    harvester-forklift wave-status     --cluster C --wave W
    harvester-forklift waves           --cluster C
    harvester-forklift wave-rollback   --cluster C --wave W [--vm vm-16 ...]
    harvester-forklift wave-close      --cluster C --wave W [--clean-snapshots]
    harvester-forklift wave-delete     --cluster C --wave W
    harvester-forklift cdi-importer    --cluster C [--show | --upstream [--image IMG] | --original]
    harvester-forklift precopy-interval --cluster C MINUTES

Harvester 1.9 ne livre pas Forklift : `install` pose cert-manager (depuis le
manifeste que la console tire de son paquet Cluster API), l'add-on
expérimental forklift-operator puis le ForkliftController. Les secrets
(vCenter, registre) arrivent en JSON sur l'entrée standard ou dans un fichier
privé (`--spec FICHIER`, ce que fait la console), jamais en argument.
`provider-apply` ne reprend jamais un fournisseur fait par un autre outil
(ni le fournisseur `host` de Forklift) et pose l'accès à l'inventaire s'il
manque ; `provider-delete` ne touche qu'à un fournisseur vSphere.
Vagues à chaud (v1.76.0) : les identifiants du vCenter (retour à la source,
instantanés de Forklift) sont relus dans le Secret du fournisseur, jamais
passés en argument. Voir docs/design/2026-09-29-forklift-b2-plan.md.
Progression sur stderr au format STEP_EVENT|<étape>|<statut>|<message>.
Codes : 0 succès, 1 échec, 2 refus. Bibliothèque standard seulement.
Voir docs/design/2026-09-27-migrations-vmware.md.
"""

import argparse
import http.client
import json
import ssl
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import hv_forklift as hf  # noqa: E402
import oci_push as op  # noqa: E402
import vsphere_api as vs  # noqa: E402
from kube import Kube, KubeError, cluster_config  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_REFUSED = 0, 1, 2


def step(sid, status, msg=""):
    clean = " ".join(str(msg).split())
    sys.stderr.write(f"STEP_EVENT|{sid}|{status}|{clean}\n")
    sys.stderr.flush()


def kube_from(args):
    entry = cluster_config(args.cluster) if args.cluster else None
    kc = args.kubeconfig or (entry or {}).get("kubeconfig")
    if not kc:
        raise ValueError("give --cluster (with a kubeconfig in the configuration) or --kubeconfig")
    return Kube(kc)


def get_opt(kube, kind, ns, name):
    """Un objet d'une CRD qui peut ne pas exister encore (avant l'add-on)."""
    try:
        return kube.get(kind, ns, name)
    except KubeError as e:
        if "doesn't have a resource type" in str(e):
            return None
        raise


def read_stdin_json():
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("stdin: JSON expected") from None
    if not isinstance(data, dict):
        raise ValueError("stdin: a JSON object expected")
    return data


def read_json_input(args):
    """La demande : le fichier privé donné par --spec (la console), sinon l'entrée standard."""
    path = getattr(args, "spec", None)
    if not path:
        return read_stdin_json()
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        raise ValueError("--spec: a readable JSON file expected") from None
    if not isinstance(data, dict):
        raise ValueError("--spec: a JSON object expected")
    return data


def bundle_cert_manager(bundle, into):
    """Le manifeste cert-manager du paquet Cluster API de la console, écrit dans `into`.

    Le paquet pèse ~440 Mo compressé : il est parcouru membre après membre et
    la lecture s'arrête au manifeste, sans décompresser le reste
    (`getmembers()` lisait tout le paquet pour quelques kilo-octets)."""
    data = None
    try:
        with tarfile.open(bundle, "r:gz") as tar:
            for m in tar:
                if m.isfile() and m.name.endswith(hf.CERT_MANAGER_MEMBER):
                    data = tar.extractfile(m).read()
                    break
    except (OSError, EOFError, tarfile.TarError):
        raise ValueError("the Cluster API bundle cannot be read") from None
    if data is None:
        raise ValueError("the Cluster API bundle has no cert-manager manifest")
    out = Path(into) / "cert-manager.yaml"
    out.write_bytes(data)
    return str(out)


def until(fn, timeout, label, sleep=time.sleep, now=time.time, every=5):
    """Relit `fn()` -> (True|False|None, message) jusqu'à la fin ou le délai."""
    deadline, last = now() + timeout, None
    while True:
        res, msg = fn()
        if msg != last:
            step(label, "running" if res is None else ("done" if res else "error"), msg)
            last = msg
        if res is not None:
            return EXIT_OK if res else EXIT_FAIL
        if now() >= deadline:
            step(label, "error", f"not done after {timeout} s: {msg}")
            return EXIT_FAIL
        sleep(every)


def install_state(kube):
    by_name = lambda items: {(d.get("metadata") or {}).get("name"): d for d in items}  # noqa: E731
    return hf.install_state(hf.pick_addon(kube.list(hf.K_ADDON, None))[0],
                            by_name(kube.list(hf.K_DEPLOY, hf.NS)),
                            get_opt(kube, hf.K_CONTROLLER, hf.NS, hf.CONTROLLER_NAME),
                            by_name(kube.list(hf.K_DEPLOY, hf.CERT_MANAGER[0])),
                            kube.get("serviceaccounts", hf.NS, hf.INVENTORY_SA))


def cmd_status(args, kube=None):
    kube = kube or kube_from(args)
    st = install_state(kube)
    providers = []
    if st["controller"]:
        for p in kube.list(hf.K_PROVIDER, None):
            m = p.get("metadata") or {}
            res, msg = hf.provider_state(p)
            providers.append({"namespace": m.get("namespace"), "name": m.get("name"),
                              "type": (p.get("spec") or {}).get("type"), "ready": res, "message": msg})
    vddk = hf.vddk_record(get_opt(kube, "configmaps", hf.NS, hf.VDDK_CM))
    print(json.dumps({"install": st, "providers": providers, "vddk": vddk}))
    return EXIT_OK


def cmd_install(args, kube=None, sleep=time.sleep, now=time.time):
    kube = kube or kube_from(args)
    want = hf.addon_manifest(args.chart_version, args.image_tag)      # refuse avant d'écrire
    st = install_state(kube)
    if st["cert_manager"]:
        step("cert-manager", "done", "cert-manager is running")
    else:
        manifest = args.cert_manager_manifest
        tmp = None
        try:
            if not manifest and getattr(args, "cert_manager_from_bundle", None):
                tmp = tempfile.TemporaryDirectory(prefix="hfk-cm-")
                try:
                    manifest = bundle_cert_manager(args.cert_manager_from_bundle, tmp.name)
                except ValueError as e:
                    step("cert-manager", "error", str(e))
                    return EXIT_REFUSED
            if not manifest:
                step("cert-manager", "error", "cert-manager is missing: give --cert-manager-manifest "
                     "(the console takes it from its Cluster API bundle)")
                return EXIT_REFUSED
            step("cert-manager", "running", "installing cert-manager")
            kube.run("apply", "--server-side", "--force-conflicts", "-f", manifest, timeout=300)
        finally:
            if tmp:
                tmp.cleanup()

        def cm():
            s = install_state(kube)
            return (True, "cert-manager is running") if s["cert_manager"] else \
                (None, "waiting for " + ", ".join(s["cert_manager_missing"]))
        rc = until(cm, args.timeout, "cert-manager", sleep, now)
        if rc:
            return rc
    if kube.get("namespaces", None, hf.NS) is None:
        kube.create(hf.namespace_manifest())
    cur, theirs = hf.pick_addon(kube.list(hf.K_ADDON, None))
    if theirs:
        # celui de Harvester (1.9.1) : activé tel quel, jamais réécrit
        m = cur.get("metadata") or {}
        if not (cur.get("spec") or {}).get("enabled"):
            kube.patch(hf.K_ADDON, m.get("namespace"), m.get("name"), {"spec": {"enabled": True}})
            sleep(3)
        step("addon", "running", f"Harvester's own forklift-operator add-on ({m.get('namespace')}, "
             f"{(cur.get('spec') or {}).get('version')}): enabled, left as Harvester ships it")
    elif cur is None:
        kube.create(want)
        step("addon", "running", f"forklift-operator {args.chart_version} declared, images {args.image_tag}")
    elif any((cur.get("spec") or {}).get(k) != v for k, v in want["spec"].items()):
        kube.patch(hf.K_ADDON, hf.NS, hf.ADDON[1], {"spec": want["spec"]})
        step("addon", "running", f"forklift-operator set to {args.chart_version}, images {args.image_tag}")
        sleep(3)          # le contrôleur des add-ons prend la main : l'ancien statut n'est pas la fin

    def addon():
        s = install_state(kube)
        if s["addon"] == "failed":
            return False, s["addon_message"]
        if s["addon"] == "ready" and s["operator"]:
            return True, "forklift-operator is running"
        return None, s["addon_message"] if s["addon"] != "ready" else "waiting for the operator"
    rc = until(addon, args.timeout, "addon", sleep, now)
    if rc:
        return rc
    if get_opt(kube, hf.K_CONTROLLER, hf.NS, hf.CONTROLLER_NAME) is None:
        kube.create(hf.controller_manifest())
        step("controller", "running", "ForkliftController created: the operator deploys Forklift")

    def components():
        s = install_state(kube)
        if not s["components_missing"]:
            return True, "Forklift components are running"
        probs = hf.pod_problems(kube.list("pods", hf.NS))
        return None, "waiting for " + ", ".join(s["components_missing"]) + (f" ({'; '.join(probs)})" if probs else "")
    rc = until(components, args.timeout, "controller", sleep, now)
    if rc:
        return rc
    kube.apply(hf.inventory_rbac())
    step("install", "done", "Forklift is installed")
    return EXIT_OK


def cmd_vddk_image(args):
    if getattr(args, "spec", None):
        creds = read_json_input(args)
    else:
        creds = read_stdin_json() if args.auth_stdin else None
    if creds is not None and not (creds.get("username") and creds.get("password")):
        raise ValueError('registry credentials: {"username": ..., "password": ...} expected')
    step("vddk", "running", f"building the VDDK image from {Path(args.archive).name}")
    res = op.push_vddk_image(args.archive, args.image, base=args.base, target_creds=creds,
                             plain_http=args.plain_http, step=lambda m: step("vddk", "running", m))
    step("vddk", "done", f"{res['image']} ({res['digest'][:19]})")
    if getattr(args, "cluster", None) or getattr(args, "kubeconfig", None):
        kube = kube_from(args)
        when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        kube.apply([hf.namespace_manifest(),
                    hf.vddk_record_manifest(res["image"], res["digest"], Path(args.archive).name, when)])
        step("vddk-record", "done", f"the cluster remembers {res['image']}")
    print(json.dumps(res))
    return EXIT_OK


def cmd_provider_apply(args, kube=None, sleep=time.sleep, now=time.time):
    kube = kube or kube_from(args)
    ns, name = hf.check_name(args.namespace, "namespace"), hf.check_name(args.name, "provider")
    spec = read_json_input(args)
    secret, prov = hf.provider_secret(ns, name, spec), hf.provider_manifest(ns, name, spec)
    # Forklift doit tourner ; l'accès à l'inventaire, lui, est posé ici s'il
    # manque (installation arrêtée avant sa fin, Forklift posé autrement)
    st = install_state(kube)
    if not st["running"]:
        step("provider", "error", "Forklift is not installed and running on this cluster: run install first")
        return EXIT_REFUSED
    # un fournisseur de ce nom fait par un autre outil (ou le `host` de
    # Forklift) n'est jamais repris : l'application côté serveur forcerait
    # ses champs
    foreign = hf.foreign_provider(get_opt(kube, hf.K_PROVIDER, ns, name))
    if foreign:
        step("provider", "error", foreign)
        return EXIT_REFUSED
    # déjà posé : ne pas le réappliquer à chaque fournisseur (lu juste avant)
    if not st["inventory_access"]:
        kube.apply(hf.inventory_rbac())
    try:
        kube.apply([secret])
    except KubeError as e:
        msg = str(e)
        reason = msg.split("denied the request: ", 1)[1] if "denied the request: " in msg else msg
        step("provider", "error", reason)
        return EXIT_FAIL
    kube.apply([prov])
    step("provider", "running", f"provider {ns}/{name} applied: Forklift checks the vCenter")
    return until(lambda: hf.provider_state(kube.get(hf.K_PROVIDER, ns, name)), args.timeout, "provider", sleep, now)


def cmd_provider_delete(args, kube=None, sleep=time.sleep, now=time.time):
    kube = kube or kube_from(args)
    ns, name = hf.check_name(args.namespace, "namespace"), hf.check_name(args.name, "provider")
    cur = get_opt(kube, hf.K_PROVIDER, ns, name)
    if cur is None:
        raise ValueError(f"no provider {ns}/{name}")
    kind = (cur.get("spec") or {}).get("type") or "unknown"
    if kind != "vsphere":
        # le fournisseur `host` de Forklift en premier : sans lui, plus de migration
        step("provider", "error", f"provider {ns}/{name} is not a vCenter ({kind}): harvester-forklift leaves it alone")
        return EXIT_REFUSED
    users = hf.plans_using(ns, name, kube.list(hf.K_PLAN, None))
    if users:
        step("provider", "error", f"provider {ns}/{name} is used by migration plans: {', '.join(users)}")
        return EXIT_REFUSED
    kube.delete(hf.K_PROVIDER, ns, name)
    ref = (cur.get("spec") or {}).get("secret") or {}
    if args.with_secret and ref.get("name"):
        sec = kube.get("secrets", ref.get("namespace") or ns, ref["name"])
        if sec and ((sec.get("metadata") or {}).get("labels") or {}).get(hf.L_MANAGED) == "true":
            kube.delete("secrets", ref.get("namespace") or ns, ref["name"])
            step("provider", "running", f"secret {ref['name']} deleted")
    return until(lambda: (True, f"provider {ns}/{name} deleted") if get_opt(kube, hf.K_PROVIDER, ns, name) is None
                 else (None, "deleting"), args.timeout, "provider", sleep, now)


def fetch_json(url, token):
    """GET du service d'inventaire par le relais local : son certificat est
    celui du cluster (cert-manager), le canal est celui de kubectl. Toute
    panne (jeton refusé, relais tombé, JSON invalide) devient une KubeError
    courte, jamais une trace Python, et ne porte jamais le jeton."""
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        e.close()
        raise KubeError(f"inventory service: HTTP {e.code} {e.reason}") from None
    except urllib.error.URLError as e:
        raise KubeError(f"inventory service: {e.reason}") from None
    except OSError as e:
        raise KubeError(f"inventory service: {e}") from None
    try:
        return json.loads(body)
    except ValueError:
        raise KubeError("inventory service: invalid JSON") from None


def inventory(kube, ns, name, kind, fetch=None):
    """Les lignes d'inventaire d'un fournisseur, lues sur le service
    d'inventaire par un relais local avec un jeton court. Les VMs au
    détail 4 : outils VMware, instantané courant et uuid n'existent pas au
    détail 1 (v1.76.0)."""
    ns, name = hf.check_name(ns, "namespace"), hf.check_name(name, "provider")
    p = get_opt(kube, hf.K_PROVIDER, ns, name)
    if p is None:
        raise ValueError(f"no provider {ns}/{name}")
    uid = (p.get("metadata") or {}).get("uid")
    detail = 4 if kind == "vms" else 1
    token = kube.run("create", "token", hf.INVENTORY_SA, "-n", hf.NS, "--duration", "10m").strip()
    with kube.port_forward(hf.NS, f"svc/{hf.INVENTORY_SVC}", hf.INVENTORY_PORT) as port:
        data = (fetch or fetch_json)(f"https://127.0.0.1:{port}/providers/vsphere/{uid}/{kind}?detail={detail}", token)
    return hf.inventory_rows(kind, data)


def cmd_inventory(args, kube=None, fetch=None):
    kube = kube or kube_from(args)
    print(json.dumps(inventory(kube, args.namespace, args.name, args.kind, fetch)))
    return EXIT_OK


# --- vagues à chaud (v1.76.0) --------------------------------------------------
#
# Relevé sur le banc (harvlab2 + vmwlab, 29/09/2026) : une vague = NetworkMap,
# StorageMap, Plan et une Migration par essai, dans le namespace forklift ; la
# bascule arrête la source par les outils VMware ; le retour arrière arrête la
# VM Harvester (runStrategy Halted, ~17 s) puis rallume la source par vCenter.

K_VM = "virtualmachines.kubevirt.io"
K_VMI = "virtualmachineinstances.kubevirt.io"
K_CDI = "cdis.cdi.kubevirt.io"
CDI_DEPLOY = "cdi-deployment"
UPSTREAM_IMPORTER = "quay.io/kubevirt/cdi-importer"
RUNNING_STATES = ("copying", "cutover-scheduled", "cutting-over")
ROLLBACK_STATES = ("succeeded", "failed", "rolled-back")


def _labels(obj):
    return ((obj or {}).get("metadata") or {}).get("labels") or {}


def _true_cond(obj, ctype):
    return any(c.get("type") == ctype and str(c.get("status")) == "True"
               for c in ((obj or {}).get("status") or {}).get("conditions") or [])


def migration_ended(m):
    return any(_true_cond(m, t) for t in ("Succeeded", "Failed", "Canceled"))


def wave_migrations(kube, wave):
    return [m for m in kube.list(hf.K_MIGRATION, hf.NS)
            if (((m.get("spec") or {}).get("plan")) or {}).get("name") == wave]


def load_wave(kube, wave):
    """(plan, migrations) d'une vague de la console ; un plan fait par un autre
    outil n'est jamais touché."""
    wave = hf.check_wave_name(wave)
    plan = get_opt(kube, hf.K_PLAN, hf.NS, wave)
    if plan is None:
        raise ValueError(f"no wave {wave}")
    lab = _labels(plan)
    if lab.get(hf.L_MANAGED) != "true" or lab.get(hf.L_WAVE) != wave:
        raise ValueError(f"plan {hf.NS}/{wave} was not made by harvester-ops: it is left alone")
    return plan, wave_migrations(kube, wave)


def running_migration(plan, migrations):
    """La Migration en cours de la vague (la plus récente, pas finie), ou None."""
    cur = hf.current_migration(plan, migrations)
    return cur if cur is not None and not migration_ended(cur) else None


def next_migration_number(plan, migrations):
    """Le numéro suivant : les Migrations présentes et celles de l'historique
    du plan (une Migration supprimée garde son nom dans l'historique)."""
    names = [(m.get("metadata") or {}).get("name") or "" for m in migrations]
    names += [((h.get("migration") or {}).get("name")) or ""
              for h in (((plan.get("status") or {}).get("migration") or {}).get("history") or [])]
    wave = (plan.get("metadata") or {}).get("name")
    nums = [int(n.rsplit("-m", 1)[1]) for n in names
            if n.startswith(f"{wave}-m") and n.rsplit("-m", 1)[1].isdigit()]
    return max(nums, default=0) + 1


def plan_ready(plan):
    """(True prêt, False refusé avec la condition critique de Forklift telle
    quelle, None en cours de validation)."""
    if plan is None:
        return None, "waiting for the plan"
    st = plan.get("status") or {}
    gen, seen = (plan.get("metadata") or {}).get("generation"), st.get("observedGeneration")
    if gen is not None and seen is not None and seen < gen:
        return None, "Forklift is checking the plan (change not read yet)"
    crit = [c for c in st.get("conditions") or [] if c.get("category") == "Critical" and str(c.get("status")) == "True"]
    if crit:
        return False, "; ".join(c.get("message") or c.get("type") or "refused" for c in crit)
    if _true_cond(plan, "Ready"):
        return True, "plan ready: the wave can start"
    have = [c.get("type") for c in st.get("conditions") or [] if str(c.get("status")) == "True"]
    return None, "Forklift is checking the plan" + (f" ({', '.join(have)})" if have else "")


def wave_problems(spec, rows):
    """Ce qui interdit une vague, lu dans l'inventaire : VM absente, VM
    bloquée (CBT, outils VMware), réseau ou datastore d'une VM non mappé."""
    by_id = {r.get("id"): r for r in rows}
    nets = {str((n or {}).get("source") or "").strip() for n in spec.get("networks") or []}
    stos = {str((s or {}).get("source") or "").strip() for s in spec.get("storages") or []}
    out = []
    for vm in spec.get("vms") or []:
        vm = str(vm or "").strip()
        row = by_id.get(vm)
        if row is None:
            out.append(f"{vm}: not in the inventory of the provider")
            continue
        label = f"{vm} ({row.get('name')})" if row.get("name") else vm
        for b in hf.vm_warm_blockers(row):
            out.append(f"{label}: {b}")
        for n in row.get("networks") or []:
            if n and n not in nets:
                out.append(f"{label}: network {n} is not mapped")
        for d in row.get("disks") or []:
            ds = d.get("datastore")
            if ds and ds not in stos:
                out.append(f"{label}: datastore {ds} is not mapped")
    return list(dict.fromkeys(out))


def provider_hosts(kube):
    return {((p.get("metadata") or {}).get("namespace"), (p.get("metadata") or {}).get("name")): hf.provider_host(p)
            for p in kube.list(hf.K_PROVIDER, None)}


def cmd_wave_apply(args, kube=None, fetch=None, sleep=time.sleep, now=time.time):
    kube = kube or kube_from(args)
    spec = read_json_input(args)
    docs = hf.wave_manifests(spec)                       # refuse avant de lire le cluster
    wave, plan_doc = docs[2]["metadata"]["name"], docs[2]
    target = plan_doc["spec"]["targetNamespace"]
    src = plan_doc["spec"]["provider"]["source"]
    if not install_state(kube)["running"]:
        step("wave", "error", "Forklift is not installed and running on this cluster: run install first")
        return EXIT_REFUSED
    cur = get_opt(kube, hf.K_PLAN, hf.NS, wave)
    if cur is not None:
        plan, migs = load_wave(kube, wave)
        st = hf.wave_state(plan, migs)
        if st["state"] == "closed":
            step("wave", "error", f"wave {wave} is closed: pick another name")
            return EXIT_REFUSED
        if running_migration(plan, migs) is not None:
            step("wave", "error", f"wave {wave} has a migration running: it cannot be changed now")
            return EXIT_REFUSED
    if kube.get("namespaces", None, target) is None:
        step("wave", "error", f"target namespace {target} does not exist")
        return EXIT_REFUSED
    # une VM déjà prise par une autre vague ouverte de ce cluster (les autres
    # clusters : la console, qui les voit tous)
    hosts = provider_hosts(kube)
    host = hosts.get((src["namespace"], src["name"]))
    others = [p for p in kube.list(hf.K_PLAN, hf.NS) if (p.get("metadata") or {}).get("name") != wave]
    taken = hf.taken_vms(others, hosts, kube.list(hf.K_MIGRATION, hf.NS)) if host else {}
    busy = [f"{v['id']} is already in wave {taken[(host, v['id'])]['wave']} "
            f"({taken[(host, v['id'])]['state']}): close that wave first"
            for v in plan_doc["spec"]["vms"] if (host, v["id"]) in taken]
    if busy:
        step("wave", "error", "; ".join(busy))
        return EXIT_REFUSED
    step("wave", "running", f"reading the inventory of {src['namespace']}/{src['name']}")
    rows = inventory(kube, src["namespace"], src["name"], "vms", fetch)
    probs = wave_problems(spec, rows)
    mac = hf.mac_refusal(hf.mac_conflicts(rows, kube.list(K_VM), [v["id"] for v in plan_doc["spec"]["vms"]]))
    if mac:
        probs.append(mac)
    if probs:
        step("wave", "error", "; ".join(probs))
        return EXIT_REFUSED
    kube.apply(docs)
    step("wave", "running", f"wave {wave} applied ({len(plan_doc['spec']['vms'])} VMs): Forklift checks the plan")
    return until(lambda: plan_ready(get_opt(kube, hf.K_PLAN, hf.NS, wave)), args.timeout, "wave", sleep, now)


def wave_mac_refusal(kube, plan, fetch=None):
    """Le refus d'un conflit de MAC entre les VMs de la vague et celles du
    cluster, relu au moment du geste (v1.83.2)."""
    src = (((plan.get("spec") or {}).get("provider") or {}).get("source")) or {}
    ids = [v.get("id") for v in (plan.get("spec") or {}).get("vms") or []]
    try:
        rows = inventory(kube, src.get("namespace"), src.get("name"), "vms", fetch)
    except (KubeError, ValueError) as e:
        # l'inventaire ne répond pas : on le dit, sans bloquer le geste (une
        # bascule peut être urgente ; ce n'est pas pire qu'avant ce contrôle)
        step("macs", "running", f"MAC addresses not checked: {e}")
        return None
    return hf.mac_refusal(hf.mac_conflicts(rows, kube.list(K_VM), ids))


def cmd_wave_start(args, kube=None, sleep=time.sleep, now=time.time, fetch=None):
    kube = kube or kube_from(args)
    plan, migs = load_wave(kube, args.wave)
    wave = plan["metadata"]["name"]
    run = running_migration(plan, migs)
    if run is not None:
        step("start", "error", f"migration {run['metadata']['name']} of wave {wave} is still running")
        return EXIT_REFUSED
    st = hf.wave_state(plan, migs)
    refusals = {"closed": "the wave is closed", "rolled-back": "the wave was rolled back to the source",
                "succeeded": "the wave already succeeded",
                "invalid": f"Forklift refuses the plan: {st['message']}",
                "pending": "Forklift has not validated the plan yet"}
    if st["state"] in refusals:
        step("start", "error", f"wave {wave}: {refusals[st['state']]}")
        return EXIT_REFUSED
    mac = wave_mac_refusal(kube, plan, fetch)
    if mac:
        step("start", "error", mac)
        return EXIT_REFUSED
    doc = hf.migration_manifest(wave, next_migration_number(plan, migs))
    name = doc["metadata"]["name"]
    kube.create(doc)
    step("start", "running", f"migration {name} created: first copy of the disks")

    def started():
        m = get_opt(kube, hf.K_MIGRATION, hf.NS, name)
        if m is None:
            return None, "waiting for the migration"
        if _true_cond(m, "Failed") or _true_cond(m, "Canceled"):
            return False, hf.wave_state(plan, [m])["message"] or f"migration {name} failed"
        mst = m.get("status") or {}
        if mst.get("started") or mst.get("vms") or _true_cond(m, "Running") or migration_ended(m):
            return True, f"migration {name} is running"
        return None, f"migration {name} created, waiting for Forklift"
    return until(started, args.timeout, "start", sleep, now)


def cmd_wave_cutover(args, kube=None, fetch=None):
    kube = kube or kube_from(args)
    plan, migs = load_wave(kube, args.wave)
    run = running_migration(plan, migs)
    if run is None:
        step("cutover", "error", f"wave {plan['metadata']['name']} has no migration running")
        return EXIT_REFUSED
    # une bascule arrête la source : un conflit de MAC la laisserait sans VM
    # d'arrivée (vu en réel, v1.83.2)
    mac = wave_mac_refusal(kube, plan, fetch)
    if mac:
        step("cutover", "error", mac)
        return EXIT_REFUSED
    patch = hf.cutover_patch(args.at)
    name = run["metadata"]["name"]
    kube.patch(hf.K_MIGRATION, hf.NS, name, patch)
    when = patch["spec"]["cutover"]
    step("cutover", "done", f"migration {name}: switchover at {when}" + ("" if args.at else " (now)"))
    print(json.dumps({"migration": name, "cutover": when}))
    return EXIT_OK


def cmd_wave_status(args, kube=None):
    kube = kube or kube_from(args)
    plan, migs = load_wave(kube, args.wave)
    print(json.dumps(hf.wave_state(plan, migs)))
    return EXIT_OK


def cmd_waves(args, kube=None):
    kube = kube or kube_from(args)
    migs = kube.list(hf.K_MIGRATION, hf.NS)
    out = []
    for p in kube.list(hf.K_PLAN, hf.NS):
        lab = _labels(p)
        if lab.get(hf.L_MANAGED) == "true" and lab.get(hf.L_WAVE):
            out.append(hf.wave_state(p, migs))
    print(json.dumps(sorted(out, key=lambda w: w["name"] or "")))
    return EXIT_OK


def vcenter_of(kube, plan):
    """Les identifiants du vCenter source, relus dans le Secret du fournisseur
    (jamais sur la ligne de commande). Rend (hôte, {url, user, password,
    cacert, insecure})."""
    src = (((plan.get("spec") or {}).get("provider") or {}).get("source")) or {}
    prov = get_opt(kube, hf.K_PROVIDER, src.get("namespace"), src.get("name"))
    if prov is None:
        raise ValueError(f"the provider {src.get('namespace')}/{src.get('name')} of the wave is gone")
    ref = (prov.get("spec") or {}).get("secret") or {}
    sec = kube.get("secrets", ref.get("namespace") or src.get("namespace"), ref.get("name") or "") \
        if ref.get("name") else None
    vals = hf.secret_values(sec, "user", "password", "url", "cacert", "insecureSkipVerify")
    if not vals.get("user") or not vals.get("password"):
        raise ValueError(f"the secret of provider {src.get('namespace')}/{src.get('name')} has no user and password")
    return hf.provider_host(prov), {
        "url": vals.get("url") or (prov.get("spec") or {}).get("url"), "user": vals["user"],
        "password": vals["password"], "cacert": vals.get("cacert") or None,
        "insecure": str(vals.get("insecureSkipVerify") or "").lower() == "true"}


def make_vsphere(creds):
    return vs.VSphere(creds["url"], creds["user"], creds["password"], cacert=creds["cacert"],
                      insecure=creds["insecure"])


def target_vm_name(plan, migs, vm_id):
    """Le nom de la VM que Forklift a créée : targetName du plan, sinon
    newName (nom corrigé par Forklift), sinon le nom de la source."""
    for v in (plan.get("spec") or {}).get("vms") or []:
        if v.get("id") == vm_id and v.get("targetName"):
            return v["targetName"]
    cur = hf.current_migration(plan, migs)
    for src in (plan, cur or {}):
        for v in (((src.get("status") or {}).get("migration") or {}).get("vms")
                  or (src.get("status") or {}).get("vms") or []):
            if v.get("id") == vm_id and (v.get("newName") or v.get("name")):
                return v.get("newName") or v["name"]
    return ""


def wave_vm_ids(plan, wanted):
    ids = [v.get("id") for v in (plan.get("spec") or {}).get("vms") or [] if v.get("id")]
    if not wanted:
        return ids
    unknown = [v for v in wanted if v not in ids]
    if unknown:
        raise ValueError(f"not in the wave: {', '.join(unknown)}")
    return list(dict.fromkeys(wanted))


def halt_vm(kube, ns, name, vm_id, plan_uid, assume_created, timeout, sleep, now, label):
    """Arrête la VM Harvester et attend la disparition de sa VMI ; rend le
    code de sortie.

    Si le nom attendu (celui du plan) ne correspond à rien, la VM est
    cherchée encore par les étiquettes que Forklift pose sur celles qu'il
    crée (`vmID=<id>,plan=<uid du plan>`, relevées en réel le 29/09/2026) :
    Forklift peut avoir choisi un autre nom. Si elle reste introuvable et que
    `assume_created` dit que Forklift a fini de la créer (étape
    VirtualMachineCreation terminée, ou vague entièrement réussie), on refuse
    plutôt que de supposer son absence : rallumer la source laisserait deux
    machines avec la même IP et la même adresse MAC."""
    vm = kube.get(K_VM, ns, name) if name else None
    if vm is None and plan_uid:
        found = kube.list(K_VM, ns, selector=f"vmID={vm_id},plan={plan_uid}")
        if found:
            vm = found[0]
            name = ((vm.get("metadata") or {}).get("name")) or name
    if vm is None:
        if assume_created:
            step(label, "error", f"{vm_id}: no Harvester VM found by name or by Forklift's vmID/plan "
                 "labels, but the wave shows it was created: source left off")
            return EXIT_FAIL
        step(label, "running", f"{vm_id}: no Harvester VM found: nothing to stop")
        return EXIT_OK
    spec = vm.get("spec") or {}
    if "running" in spec and "runStrategy" not in spec:
        if spec.get("running"):
            kube.patch(K_VM, ns, name, {"spec": {"running": False}})
    elif spec.get("runStrategy") != "Halted":
        kube.patch(K_VM, ns, name, {"spec": {"runStrategy": "Halted"}})
    rc = until(lambda: (True, f"Harvester VM {ns}/{name} stopped") if kube.get(K_VMI, ns, name) is None
               else (None, f"stopping Harvester VM {ns}/{name}"), timeout, label, sleep, now)
    if rc != EXIT_OK:
        step(label, "error", f"{vm_id}: Harvester VM {ns}/{name} still running: source left off")
    return rc


def cmd_wave_rollback(args, kube=None, vsphere=None, sleep=time.sleep, now=time.time):
    """Retour à la source, VM par VM : la VM Harvester d'abord arrêtée (deux
    copies allumées auraient la même IP et la même MAC), puis la source
    rallumée ; ce qui a été écrit sur Harvester depuis la bascule est perdu."""
    kube = kube or kube_from(args)
    plan, migs = load_wave(kube, args.wave)
    wave = plan["metadata"]["name"]
    ids = wave_vm_ids(plan, args.vm)
    st = hf.wave_state(plan, migs)
    if st["state"] not in ROLLBACK_STATES:
        step("rollback", "error", f"wave {wave} is {st['state']}: a rollback follows a switchover "
             "(succeeded or failed migration)")
        return EXIT_REFUSED
    done = set(hf._id_list(hf._ann(plan).get(hf.A_ROLLED_BACK)))
    todo = [i for i in ids if i not in done]
    for i in ids:
        if i in done:
            step("rollback", "done", f"{i}: already rolled back, left as is")
    # une VM dont le pipeline n'a jamais atteint la bascule (échec pendant
    # DiskTransfer par exemple) n'a rien à défaire côté Harvester ; un
    # rollback la marquerait revenue et bloquerait tout nouveau lancement
    cutover_map = {v["id"]: bool(v.get("cutover_started")) for v in st["vms"]}
    not_started = [i for i in todo if not cutover_map.get(i)]
    if not_started:
        step("rollback", "error", f"{', '.join(not_started)}: no switchover has started for this VM in wave "
             f"{wave}: a rollback follows a switchover")
        return EXIT_REFUSED
    if not todo:
        print(json.dumps({"wave": wave, "rolled_back": [], "already": ids}))
        return EXIT_OK
    _, creds = vcenter_of(kube, plan)
    target = (plan.get("spec") or {}).get("targetNamespace") or ""
    plan_uid = (plan.get("metadata") or {}).get("uid") or ""
    client = (vsphere or make_vsphere)(creds)
    rc, rolled, powered = EXIT_OK, [], []
    try:
        # vCenter est paresseux : sans cette lecture, la première panne
        # (hôte injoignable, identifiants refusés) n'apparaîtrait qu'au
        # premier power_on, après que des VMs Harvester ont déjà été
        # arrêtées. On ouvre la session et on lit chaque source d'abord ;
        # une VSphereError refuse tout sans rien arrêter.
        try:
            for i in todo:
                client.power_state(i)
        except vs.VSphereError as e:
            step("rollback", "error", f"vCenter check before stopping any Harvester VM: {e}")
            return EXIT_FAIL
        for i in todo:
            name = target_vm_name(plan, migs, i)
            if name and not hf.NAME_RE.match(name):
                name = ""
            assume_created = st["state"] == "succeeded" or hf.vm_creation_done(hf.vm_status(plan, migs, i))
            if halt_vm(kube, target, name, i, plan_uid, assume_created, args.timeout, sleep, now,
                       "rollback") != EXIT_OK:
                rc = EXIT_FAIL
                continue
            try:
                on = client.power_on(i)
            except vs.VSphereError as e:
                step("rollback", "error", f"{i}: {e}")
                rc = EXIT_FAIL
                continue
            if on:
                powered.append(i)
            step("rollback", "done", f"{i}: source powered on" if on else f"{i}: source already on, left as is")
            cur = set(hf._id_list(hf._ann(get_opt(kube, hf.K_PLAN, hf.NS, wave)).get(hf.A_ROLLED_BACK)))
            cur.add(i)
            order = [x for x in ids if x in cur] + sorted(cur - set(ids))
            kube.patch(hf.K_PLAN, hf.NS, wave, {"metadata": {"annotations": {hf.A_ROLLED_BACK: ",".join(order)}}})
            rolled.append(i)
    finally:
        close = getattr(client, "close", None)
        if close:
            close()
    print(json.dumps({"wave": wave, "rolled_back": rolled, "powered_on": powered,
                      "already": [i for i in ids if i in done]}))
    return rc


def cmd_wave_close(args, kube=None, vsphere=None, clock=None):
    kube = kube or kube_from(args)
    plan, migs = load_wave(kube, args.wave)
    wave = plan["metadata"]["name"]
    run = running_migration(plan, migs)
    if run is not None:
        step("close", "error", f"migration {run['metadata']['name']} of wave {wave} is still running: "
             "switch over or wait for its end first")
        return EXIT_REFUSED
    if not hf._ann(plan).get(hf.A_CLOSED) or not (plan.get("spec") or {}).get("archived"):
        when = hf._fmt_ts(hf._now(clock))
        kube.patch(hf.K_PLAN, hf.NS, wave, {"metadata": {"annotations": {hf.A_CLOSED: hf._ann(plan).get(hf.A_CLOSED) or when}},
                                            "spec": {"archived": True}})
        step("close", "done", f"wave {wave} closed")
    else:
        step("close", "done", f"wave {wave} was already closed")
    removed, rc = {}, EXIT_OK
    if args.clean_snapshots:
        _, creds = vcenter_of(kube, plan)
        client = (vsphere or make_vsphere)(creds)
        try:
            for i in wave_vm_ids(plan, None):
                try:
                    removed[i] = client.clear_forklift_snapshots(i)
                except vs.VSphereError as e:
                    step("snapshots", "error", f"{i}: {e}")
                    rc = EXIT_FAIL
                    continue
                n = len(removed[i])
                step("snapshots", "done", f"{i}: {n} Forklift snapshot{'s' if n != 1 else ''} removed"
                     if n else f"{i}: no Forklift snapshot")
        finally:
            close = getattr(client, "close", None)
            if close:
                close()
    print(json.dumps({"wave": wave, "closed": True, "snapshots_removed": removed}))
    return rc


def cmd_wave_delete(args, kube=None, sleep=time.sleep, now=time.time):
    """Plan, Migrations et correspondances de la vague ; les VMs créées sur
    Harvester restent (suppression sans cascade)."""
    kube = kube or kube_from(args)
    plan, migs = load_wave(kube, args.wave)
    wave = plan["metadata"]["name"]
    run = running_migration(plan, migs)
    if run is not None:
        step("delete", "error", f"migration {run['metadata']['name']} of wave {wave} is still running")
        return EXIT_REFUSED
    for m in migs:
        kube.delete(hf.K_MIGRATION, hf.NS, m["metadata"]["name"], cascade="orphan")
    kube.delete(hf.K_PLAN, hf.NS, wave, cascade="orphan")
    for kind, name in ((hf.K_NETWORKMAP, f"{wave}-net"), (hf.K_STORAGEMAP, f"{wave}-sto")):
        o = get_opt(kube, kind, hf.NS, name)
        if o is not None and _labels(o).get(hf.L_WAVE) == wave and _labels(o).get(hf.L_MANAGED) == "true":
            kube.delete(kind, hf.NS, name)
    step("delete", "running", f"wave {wave}: plan, {len(migs)} migration(s) and maps deleted; "
         "the Harvester VMs stay")
    return until(lambda: (True, f"wave {wave} deleted") if get_opt(kube, hf.K_PLAN, hf.NS, wave) is None
                 else (None, "deleting the plan"), args.timeout, "delete", sleep, now)


def _deploy_env(dep, var):
    for c in ((((dep or {}).get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []:
        for e in c.get("env") or []:
            if e.get("name") == var:
                return str(e.get("value") or "")
    return None


def rolled_out(dep):
    if not dep:
        return False
    md, st = dep.get("metadata") or {}, dep.get("status") or {}
    want = max((dep.get("spec") or {}).get("replicas", 1), 1)
    return ((st.get("observedGeneration") or 0) >= (md.get("generation") or 0)
            and (st.get("updatedReplicas") or 0) >= want and (st.get("availableReplicas") or 0) >= want)


def cdi_version(kube, operator):
    """La version de CDI : statut de la ressource cdi, sinon la variable
    OPERATOR_VERSION du cdi-operator."""
    for c in kube.list(K_CDI, None):
        v = str(((c.get("status") or {}).get("observedVersion")) or "").strip()
        if v:
            return v.lstrip("v")
    return (_deploy_env(operator, "OPERATOR_VERSION") or "").strip().lstrip("v")


def cmd_cdi_importer(args, kube=None, sleep=time.sleep, now=time.time):
    kube = kube or kube_from(args)
    ns, name = hf.CDI_OPERATOR
    if args.image and not args.upstream:
        raise ValueError("--image goes with --upstream")
    op_dep = kube.get(hf.K_DEPLOY, ns, name)
    if op_dep is None:
        raise ValueError(f"no {ns}/{name} deployment: CDI is not the one of Harvester")
    state = hf.cdi_importer_state(op_dep)
    version = cdi_version(kube, op_dep)
    upstream = f"{UPSTREAM_IMPORTER}:v{version}" if version else ""
    if args.upstream or args.original:
        if args.original:
            if not state["original"]:
                step("cdi-importer", "error", "no original importer image recorded: nothing to go back to")
                return EXIT_REFUSED
            image = state["original"]
        else:
            image = args.image or upstream
            if not image:
                step("cdi-importer", "error", "the CDI version is unknown: give --image")
                return EXIT_REFUSED
        patch = hf.cdi_importer_patch(image, state)
        if args.original:
            patch["metadata"] = {"annotations": {hf.A_ORIGINAL_IMPORTER: None}}
        if state["image"] == image and not args.original:
            step("cdi-importer", "done", f"CDI already imports with {image}")
        else:
            kube.run("patch", hf.K_DEPLOY, name, "-n", ns, "--type", "strategic", "-p", json.dumps(patch))
            step("cdi-importer", "running", f"cdi-operator set to {image}: CDI restarts")

            def ready():
                o = kube.get(hf.K_DEPLOY, ns, name)
                if not rolled_out(o):
                    return None, "cdi-operator restarting"
                d = kube.get(hf.K_DEPLOY, ns, CDI_DEPLOY)
                env = _deploy_env(d, "IMPORTER_IMAGE")
                if d is None or (env is not None and env != image):
                    return None, f"waiting for cdi-operator to update {CDI_DEPLOY}"
                if not rolled_out(d):
                    return None, f"{CDI_DEPLOY} restarting"
                return True, f"CDI imports with {image}"
            rc = until(ready, args.timeout, "cdi-importer", sleep, now)
            if rc:
                return rc
        state = hf.cdi_importer_state(kube.get(hf.K_DEPLOY, ns, name))
    print(json.dumps(dict(state, cdi_version=version, upstream_image=upstream)))
    return EXIT_OK


def cmd_precopy_interval(args, kube=None, sleep=time.sleep, now=time.time):
    """Intervalle entre deux copies incrémentales : réglage global du
    ForkliftController ; l'opérateur redéploie forklift-controller."""
    kube = kube or kube_from(args)
    patch = hf.precopy_patch(args.minutes)
    minutes = patch["spec"]["controller_precopy_interval"]
    ctl = get_opt(kube, hf.K_CONTROLLER, hf.NS, hf.CONTROLLER_NAME)
    if ctl is None:
        step("precopy", "error", "no ForkliftController: run install first")
        return EXIT_REFUSED
    if hf.precopy_interval(ctl) == minutes and "controller_precopy_interval" in (ctl.get("spec") or {}):
        step("precopy", "done", f"copies already every {minutes} min")
        print(json.dumps({"minutes": minutes}))
        return EXIT_OK
    before = kube.get(hf.K_DEPLOY, hf.NS, "forklift-controller")
    gen0 = ((before or {}).get("metadata") or {}).get("generation") or 0
    kube.patch(hf.K_CONTROLLER, hf.NS, hf.CONTROLLER_NAME, patch)
    step("precopy", "running", f"copies every {minutes} min: the Forklift controller restarts "
         "(a copy already planned keeps its time)")

    def restarted():
        d = kube.get(hf.K_DEPLOY, hf.NS, "forklift-controller")
        env = _deploy_env(d, "PRECOPY_INTERVAL")
        changed = env == str(minutes) if env is not None else \
            ((d or {}).get("metadata") or {}).get("generation", 0) > gen0
        if not changed:
            return None, "waiting for the operator to redeploy the controller"
        if not rolled_out(d):
            return None, "forklift-controller restarting"
        return True, f"copies every {minutes} min"
    rc = until(restarted, args.timeout, "precopy", sleep, now)
    if rc == EXIT_OK:
        print(json.dumps({"minutes": minutes}))
    return rc


def build_parser():
    ap = argparse.ArgumentParser(prog="harvester-forklift",
                                 description="Forklift on a Harvester cluster: install, VDDK "
                                             "image, vCenter provider, inventory.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def cluster_args(sp):
        sp.add_argument("--cluster")
        sp.add_argument("--kubeconfig")

    sp = sub.add_parser("status", help="Forklift on the cluster and its providers, as JSON")
    cluster_args(sp)
    sp.set_defaults(fn=cmd_status)
    sp = sub.add_parser("install", help="cert-manager, the forklift-operator add-on and the ForkliftController")
    cluster_args(sp)
    sp.add_argument("--chart-version", default=hf.CHART_VERSION)
    sp.add_argument("--image-tag", default=hf.IMAGE_TAG, help="tag of the harvester-forklift-* images")
    sp.add_argument("--cert-manager-manifest", help="cert-manager YAML, applied if cert-manager is missing")
    sp.add_argument("--cert-manager-from-bundle",
                    help="the console's Cluster API bundle (.tar.gz): its cert-manager manifest "
                         "is applied if cert-manager is missing")
    sp.add_argument("--timeout", type=int, default=900)
    sp.set_defaults(fn=cmd_install)
    sp = sub.add_parser("vddk-image", help="build the VDDK init image from VMware's archive and push it")
    sp.add_argument("--archive", required=True, help="VMware-vix-disklib-*.x86_64.tar.gz")
    sp.add_argument("--image", required=True, help="registry/path:tag to push to")
    sp.add_argument("--base", default=op.DEFAULT_BASE, help="base image with cp")
    sp.add_argument("--plain-http", action="store_true", help="the target registry speaks plain HTTP")
    sp.add_argument("--auth-stdin", action="store_true", help='{"username": ..., "password": ...} on stdin')
    cluster_args(sp)
    sp.add_argument("--spec", help='a private JSON file {"username": ..., "password": ...} instead of stdin')
    sp.set_defaults(fn=cmd_vddk_image)
    for name, fn, hlp in (("provider-apply", cmd_provider_apply,
                           "declare or change a vCenter provider (JSON on stdin); never one made by another tool"),
                          ("provider-delete", cmd_provider_delete, "delete a vCenter provider")):
        sp = sub.add_parser(name, help=hlp)
        cluster_args(sp)
        sp.add_argument("--namespace", required=True)
        sp.add_argument("--name", required=True)
        sp.add_argument("--timeout", type=int, default=300)
        if name == "provider-delete":
            sp.add_argument("--with-secret", action="store_true", help="also delete the secret the console created")
        if name == "provider-apply":
            sp.add_argument("--spec", help="a private JSON file with the request instead of stdin")
        sp.set_defaults(fn=fn)
    sp = sub.add_parser("inventory", help="VMs, networks or datastores of a vCenter provider, as Forklift sees them")
    cluster_args(sp)
    sp.add_argument("--namespace", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--kind", choices=("vms", "networks", "datastores"), required=True)
    sp.set_defaults(fn=cmd_inventory)

    sp = sub.add_parser("wave-apply", help="compose or change a warm-migration wave (JSON on stdin or --spec)")
    cluster_args(sp)
    sp.add_argument("--spec", help="a private JSON file with the wave instead of stdin")
    sp.add_argument("--timeout", type=int, default=300)
    sp.set_defaults(fn=cmd_wave_apply)
    for name, fn, hlp, timeout in (
            ("wave-start", cmd_wave_start, "start the next migration of a wave (first copy, then incremental copies)", 300),
            ("wave-cutover", cmd_wave_cutover, "switch a wave over: now, or at --at", None),
            ("wave-status", cmd_wave_status, "state of a wave, as JSON", None),
            ("wave-rollback", cmd_wave_rollback,
             "stop the Harvester VMs and power the sources on again (writes on Harvester are lost)", 300),
            ("wave-close", cmd_wave_close, "close a wave (archived plan)", None),
            ("wave-delete", cmd_wave_delete, "delete plan, migrations and maps of a wave; the Harvester VMs stay", 120)):
        sp = sub.add_parser(name, help=hlp)
        cluster_args(sp)
        sp.add_argument("--wave", required=True)
        if timeout:
            sp.add_argument("--timeout", type=int, default=timeout)
        if name == "wave-cutover":
            sp.add_argument("--at", help="RFC 3339 time with a zone (default: now)")
        if name == "wave-rollback":
            sp.add_argument("--vm", action="append", default=[], help="source VM id (vm-NN), repeatable; default: all")
        if name == "wave-close":
            sp.add_argument("--clean-snapshots", action="store_true",
                            help="also remove the forklift-migration-precopy snapshots of the source VMs")
        sp.set_defaults(fn=fn)
    sp = sub.add_parser("waves", help="all the waves of the cluster, as JSON")
    cluster_args(sp)
    sp.set_defaults(fn=cmd_waves)
    sp = sub.add_parser("cdi-importer", help="the image CDI imports disks with (Harvester's has no VDDK plugin)")
    cluster_args(sp)
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--show", action="store_true", help="show the importer image (default)")
    g.add_argument("--upstream", action="store_true",
                   help="use the upstream importer of the same CDI version, or --image (a mirror)")
    g.add_argument("--original", action="store_true", help="go back to the image recorded before the switch")
    sp.add_argument("--image", help="with --upstream: the importer image to use (mirror)")
    sp.add_argument("--timeout", type=int, default=600)
    sp.set_defaults(fn=cmd_cdi_importer)
    sp = sub.add_parser("precopy-interval", help="minutes between two incremental copies (whole cluster)")
    cluster_args(sp)
    sp.add_argument("minutes")
    sp.add_argument("--timeout", type=int, default=600)
    sp.set_defaults(fn=cmd_precopy_interval)
    return ap, sub


def short_error(e):
    """Un message court pour une panne de fichier, d'archive ou de réseau :
    jamais de trace Python, et d'un chemin seulement son dernier élément."""
    if isinstance(e, OSError) and e.strerror:
        name = Path(str(e.filename)).name if e.filename else ""
        return e.strerror + (f": {name}" if name else "")
    text = str(e).strip()
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


def main(argv=None):
    ap, _ = build_parser()
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (ValueError, KubeError, op.RegistryError, vs.VSphereError) as e:
        step(args.cmd, "error", str(e))
        return EXIT_REFUSED if isinstance(e, ValueError) else EXIT_FAIL
    except (OSError, tarfile.TarError, EOFError, http.client.HTTPException) as e:
        # archive illisible ou coupée, registre qui raccroche, disque plein...
        step(args.cmd, "error", short_error(e))
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
