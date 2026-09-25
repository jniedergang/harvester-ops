#!/usr/bin/env python3
"""Déplace, exporte ou importe une VM entre clusters Harvester.

    harvester-vm-transfer check   --from A --vm ns/nom --to B [--json]
    harvester-vm-transfer migrate --from A --vm ns/nom --to B [options]
    harvester-vm-transfer export  --from A --vm ns/nom --out fichier.hvx
    harvester-vm-transfer import  --to B --in fichier.hvx [options]

Deux moteurs, choisis seuls (voir docs/design/2026-09-24-migration-vm.md) :

* **sauvegarde** quand les deux clusters partagent leur cible de
  sauvegarde (même NFS ou S3) : sauvegarde Harvester, synchronisation,
  restauration en nouvelle VM, correspondances appliquées. Seul ce moteur
  permet l'arrêt court (`--mode short`) ;
* **fichier** sinon : chaque disque figé en image temporaire, lu en flux,
  écrit dans une archive `.hvx` ou servi directement au cluster cible, qui
  vient le chercher par HTTP (CDI). Les nœuds de la cible doivent alors
  joindre cet hôte sur le port du guichet (`--serve-address`, 8094 par
  défaut).

Clusters : `--from`/`--to` nomment des clusters de la configuration
(`HARVESTER_OPS_CONFIG`, lue avec yq), ou `--from-kubeconfig`/
`--to-kubeconfig` des fichiers.

Progression sur stderr au format STEP_EVENT|<étape>|<statut>|<message>,
lue telle quelle par la console. Codes de sortie : 0 succès, 1 échec,
2 refus du contrôle préalable, 3 annulation.

Bibliothèque standard seulement : le script tourne sur un hôte airgap.
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import longhorn_room  # noqa: E402
import vm_transfer as vt  # noqa: E402
import vm_transfer_run as run  # noqa: E402
import vm_transfer_serve as vs  # noqa: E402

DEFAULT_PORT = 8094
MIB = 1024 * 1024
SPEEDS = ("eco", "normal", "max")
EXIT_OK, EXIT_FAIL, EXIT_BLOCKED, EXIT_CANCELLED = 0, 1, 2, 3


class KubeError(Exception):
    pass


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# kubectl
# ---------------------------------------------------------------------------

class Kube:
    """Client de `vm_transfer_run`, par kubectl."""

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


# ---------------------------------------------------------------------------
# Configuration et relevés
# ---------------------------------------------------------------------------

def cluster_kubeconfig(name):
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
            return c.get("kubeconfig")
    raise SystemExit(f"unknown cluster: {name} (not in {cfg})")


def _setting(kube, name):
    s = kube.get(run.K_SETTING, None, name) or {}
    return s.get("value") or s.get("default") or "", s


def _target_ok(setting_obj):
    for c in ((setting_obj.get("status") or {}).get("conditions")) or []:
        if (c.get("type") or "").lower() == "configured":
            return c.get("status") == "True"
    return False


def _longhorn_numbers(kube):
    out = {}
    for name, default in (("storage-over-provisioning-percentage", 200.0),
                          ("storage-minimal-available-percentage", 25.0)):
        s = kube.get("settings.longhorn.io", "longhorn-system", name) or {}
        try:
            out[name] = float(s.get("value"))
        except (TypeError, ValueError):
            out[name] = default
    nodes = kube.list("nodes.longhorn.io", "longhorn-system")
    return nodes, out["storage-over-provisioning-percentage"], out["storage-minimal-available-percentage"]


def _images(kube):
    out = {}
    for i in kube.list(run.K_IMAGE):
        md, st = i.get("metadata") or {}, i.get("status") or {}
        out[f"{md.get('namespace')}/{md.get('name')}"] = {
            "display": (i.get("spec") or {}).get("displayName"),
            "size": st.get("size"), "virtual_size": st.get("virtualSize")}
    return out


def _default_class(scs):
    for sc in scs:
        ann = (sc.get("metadata") or {}).get("annotations") or {}
        if ann.get("storageclass.kubernetes.io/is-default-class") == "true":
            return sc["metadata"]["name"]
    return None


def collect_source(kube, ns, name, cluster=""):
    vm = kube.get(run.K_VM, ns, name)
    if vm is None:
        raise SystemExit(f"VM {ns}/{name} not found on {cluster or 'the source cluster'}")
    vmi = kube.get(run.K_VMI, ns, name)
    if vmi is not None:
        vm.setdefault("status", {})["printableStatus"] = (vmi.get("status") or {}).get("phase")
    tspec = ((vm.get("spec") or {}).get("template") or {}).get("spec") or {}
    pvcs, used = {}, {}
    for vol in tspec.get("volumes") or []:
        claim = (vol.get("persistentVolumeClaim") or {}).get("claimName")
        if not claim:
            continue
        p = kube.get(run.K_PVC, ns, claim) or {}
        pvcs[claim] = p
        pv = (p.get("spec") or {}).get("volumeName")
        if pv:
            lv = kube.get("volumes.longhorn.io", "longhorn-system", pv) or {}
            size = (lv.get("status") or {}).get("actualSize")
            if size:
                used[claim] = int(size)
    inv = vt.vm_inventory(vm, pvcs, used)
    version, _ = _setting(kube, "server-version")
    bt_raw, bt = _setting(kube, "backup-target")
    nodes, over, minimal = _longhorn_numbers(kube)
    one = {"metadata": {"name": "_one"}, "provisioner": "driver.longhorn.io",
           "parameters": {"numberOfReplicas": "1"}}
    room = longhorn_room.storage_room(nodes, [one], over, minimal)
    scs = kube.list("storageclasses")
    images = _images(kube)
    wanted = {d["image"] for d in inv["disks"] if d.get("image")}
    return {"cluster": cluster, "version": version, "vm": vm, "inventory": inv,
            "backup_target": vt.normalize_backup_target(bt_raw), "backup_target_ok": _target_ok(bt),
            "images": {k: v for k, v in images.items() if k in wanted},
            "room_one": room["classes"]["_one"]["allocatable"],
            "default_storage_class": _default_class(scs)}


def collect_target(kube, namespace, name, cluster=""):
    try:
        kube.run("get", "--raw", "/readyz", timeout=15)
    except (KubeError, subprocess.SubprocessError):
        return {"cluster": cluster, "reachable": False}
    crd_objs = {c["metadata"]["name"]: c for c in kube.list("customresourcedefinitions")}
    crds = set(crd_objs)
    cdi_ok = False
    if "datavolumes.cdi.kubevirt.io" in crds:
        cdis = kube.list("cdis.cdi.kubevirt.io")
        cdi_ok = any((c.get("status") or {}).get("phase") == "Deployed" for c in cdis)
    version, _ = _setting(kube, "server-version")
    bt_raw, bt = _setting(kube, "backup-target")
    scs = kube.list("storageclasses")
    nodes, over, minimal = _longhorn_numbers(kube)
    room = longhorn_room.storage_room(nodes, scs, over, minimal)
    classes = {}
    nodes_ok = room["schedulable_nodes"]
    for sc in scs:
        n = sc["metadata"]["name"]
        if n in room["classes"]:
            info = dict(room["classes"][n], nodes=nodes_ok,
                        image=bool((sc.get("parameters") or {}).get("backingImage")))
            if info["replicas"] > nodes_ok > 0:
                # la place d'un volume dégradé : autant de répliques que de nœuds
                fake = dict(sc, parameters=dict(sc.get("parameters") or {},
                                                numberOfReplicas=str(nodes_ok)))
                info["degraded_allocatable"] = longhorn_room.storage_room(
                    nodes, [fake], over, minimal)["classes"][n]["allocatable"]
            classes[n] = info
    nets = [f"{n['metadata']['namespace']}/{n['metadata']['name']}"
            for n in kube.list("network-attachment-definitions.k8s.cni.cncf.io")]
    vms = kube.list(run.K_VM)
    macs = {}
    for v in vms:
        for m in vt.vm_macs(v):
            macs[m] = f"{v['metadata'].get('namespace')}/{v['metadata']['name']}"
    return {"cluster": cluster, "reachable": True,
            "kubevirt": "virtualmachines.kubevirt.io" in crds, "cdi": cdi_ok,
            "restore_halt": vt.restore_supports_halt(
                crd_objs.get("virtualmachinerestores.harvesterhci.io")),
            "version": version,
            "namespaces": [n["metadata"]["name"] for n in kube.list(run.K_NS)],
            "vm_names": [v["metadata"]["name"] for v in vms
                         if v["metadata"].get("namespace") == namespace],
            "macs": macs,
            "networks": nets, "storage_classes": classes,
            "default_storage_class": _default_class(scs),
            "backup_target": vt.normalize_backup_target(bt_raw),
            "backup_target_ok": _target_ok(bt),
            "images": _images(kube)}


def facts_from_archive(reader, path):
    if not reader.complete:
        raise SystemExit(f"{path}: incomplete archive (interrupted export?)")
    m = reader.manifest()
    if m.get("format") != vt.FORMAT:
        raise SystemExit(f"{path}: unsupported archive format {m.get('format')}")
    src = m.get("source") or {}
    # les MAC se relisent sur la VM du manifeste (nettoyée, elle les porte
    # dans ses interfaces) : l'inventaire d'une archive plus ancienne ne les
    # liste pas
    inv = dict(m["inventory"], macs=sorted(vt.vm_macs(m.get("vm") or {})))
    return m, {"cluster": src.get("cluster", ""), "version": src.get("version", ""),
               "inventory": inv, "backup_target": None, "backup_target_ok": False,
               "images": {}, "room_one": 0}


# ---------------------------------------------------------------------------
# Demande
# ---------------------------------------------------------------------------

def _pairs(values):
    out = {}
    for v in values or []:
        a, sep, b = v.partition("=")
        if not sep:
            raise SystemExit(f"expected source=target, got {v!r}")
        out[a] = b or None
    return out


def build_request(args, kind, src, dst):
    inv = src["inventory"]
    if kind == "import":
        vm_ns, vm_name = (src.get("namespace") or "default"), (src.get("name") or "")
    else:
        vm_ns, _, vm_name = args.vm.partition("/")
    user_nets = _pairs(getattr(args, "map_net", None))
    user_scs = _pairs(getattr(args, "map_sc", None))
    if dst:
        # le moteur dépend des correspondances (un réseau renommé impose la
        # copie par la console), et les classes proposées dépendent du moteur
        nets = dict(vt.default_mappings(inv, dst, "backup")["networks"], **user_nets)
        engine, _ = vt.choose_engine(src, dst, {"kind": kind, "networks": nets,
                                               "engine": getattr(args, "engine", None)})
        defaults = vt.default_mappings(inv, dst, engine)
    else:
        defaults = {"networks": {}, "storage_classes": {}}
    running = inv.get("running")
    source = getattr(args, "source", None) or ("running" if (kind == "export" and running) else "stopped")
    req = {
        "kind": kind, "vm_ns": vm_ns, "vm_name": vm_name,
        "name": getattr(args, "name", None) or vm_name,
        "namespace": getattr(args, "namespace", None) or vm_ns,
        "mode": getattr(args, "mode", None) or "stop",
        "source": source,
        "target": getattr(args, "target", None) or "started",
        "keep_mac": not getattr(args, "new_mac", False),
        "networks": dict(defaults["networks"], **user_nets),
        "storage_classes": dict(defaults["storage_classes"], **user_scs),
        "create_namespace": bool(getattr(args, "create_namespace", False)),
        "keep_backups": bool(getattr(args, "keep_backups", False)),
        "run_strategy": inv.get("run_strategy"),
        "restore_halt": bool((dst or {}).get("restore_halt")),
    }
    # vitesse : profil, puis réglages explicites qui l'emportent
    speed = getattr(args, "speed", None) or "normal"
    req["speed"] = speed
    req["parallel"] = getattr(args, "parallel", None) or (1 if speed == "eco" else None)
    req["boost"] = speed == "max"
    req["bandwidth"] = getattr(args, "bandwidth", None)
    if getattr(args, "engine", None):
        req["engine"] = args.engine
    return req


def _public_inventory(inv):
    return {k: v for k, v in inv.items() if k != "secrets"} | {"secrets": len(inv.get("secrets") or [])}


def amount(inv):
    """Ce qu'il y a à transférer : taille des disques, et occupation réelle
    Longhorn quand elle est connue."""
    disks = inv.get("disks") or []
    used = [d.get("used") for d in disks]
    return {"disks": len(disks), "size": sum(int(d.get("size") or 0) for d in disks),
            "used": sum(used) if used and all(u is not None for u in used) else None}


def report(args, src, dst, req, findings):
    engine = next(f["facts"] for f in findings if f["code"] == "engine") if any(
        f["code"] == "engine" for f in findings) else {"engine": None, "reason": None}
    out = {"engine": engine["engine"], "reason": engine["reason"], "findings": findings,
           "mappings": {"networks": req["networks"], "storage_classes": req["storage_classes"]},
           "inventory": _public_inventory(src["inventory"]),
           "amount": amount(src["inventory"]),
           "request": {k: req.get(k) for k in ("name", "namespace", "mode", "source", "target",
                                                "keep_mac", "create_namespace", "speed",
                                                "parallel", "boost", "bandwidth")},
           "source": {"cluster": src.get("cluster"), "version": src.get("version")},
           "target": {"cluster": (dst or {}).get("cluster"), "version": (dst or {}).get("version"),
                      "networks": (dst or {}).get("networks"),
                      "storage_classes": sorted(((dst or {}).get("storage_classes") or {}).keys()),
                      "namespaces": (dst or {}).get("namespaces")}}
    if getattr(args, "json", False):
        print(json.dumps(out, indent=1))
    else:
        print(f"engine: {out['engine']} ({out['reason']})")
        a = out["amount"]
        used = f" ({a['used'] / MIB / 1024:.1f} GiB used)" if a["used"] is not None else ""
        print(f"to transfer: {a['disks']} disk(s), {a['size'] / MIB / 1024:.1f} GiB{used}")
        for f in findings:
            if f["code"] == "engine":
                continue
            facts = ", ".join(f"{k}={v}" for k, v in f["facts"].items())
            print(f"  [{f['level']}] {f['code']}" + (f": {facts}" if facts else ""))
        if not vt.blocking(findings):
            print("  no blocker")
    return out


# ---------------------------------------------------------------------------
# Exécution
# ---------------------------------------------------------------------------

def _kube(args, side):
    kc = getattr(args, f"{side}_kubeconfig", None)
    name = getattr(args, side, None)
    if not kc:
        if not name:
            raise SystemExit(f"--{side} or --{side}-kubeconfig is required")
        kc = cluster_kubeconfig(name)
    return Kube(kc), name or Path(kc).stem


def _advertise(args, dst_kube):
    host, port = None, DEFAULT_PORT
    if args.serve_address:
        h, _, p = args.serve_address.rpartition(":")
        if h:
            host = h
            port = int(p)
        else:
            host = args.serve_address
    if not host:
        target = dst_kube.server_host()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((target or "8.8.8.8", 443))
            host = s.getsockname()[0]
        except OSError:
            host = "127.0.0.1"
        finally:
            s.close()
    return host, port


def _on_signal(signum, frame):
    raise Cancelled(f"signal {signum}")


def execute(args, kind):
    tid = getattr(args, "id", None) or uuid.uuid4().hex[:8]
    src_kube = src_name = dst_kube = dst_name = None
    reader = None
    manifest = None
    if kind != "import":
        src_kube, src_name = _kube(args, "from")
    if kind != "export":
        dst_kube, dst_name = _kube(args, "to")

    vt.step("check", "running", "reading both clusters")
    if kind == "import":
        reader = vt.ArchiveReader(args.input)
        manifest, src = facts_from_archive(reader, args.input)
        src_view = dict(src, namespace=manifest["source"].get("namespace"),
                        name=manifest["source"].get("name"))
    else:
        vm_ns, _, vm_name = args.vm.partition("/")
        src = collect_source(src_kube, vm_ns, vm_name, src_name)
        src_view = src
    dst = None
    if kind != "export":
        ns = getattr(args, "namespace", None) or (src_view.get("namespace") if kind == "import"
                                                  else args.vm.partition("/")[0])
        dst = collect_target(dst_kube, ns, getattr(args, "name", None), dst_name)
    # `check` juge un transfert entre clusters
    req = build_request(args, "migrate" if kind == "check" else kind, src_view, dst)
    if kind == "export":
        out = Path(args.out)
        if out.is_dir():
            out = out / f"{req['vm_name']}-{time.strftime('%Y%m%d-%H%M%S')}{vt.ARCHIVE_SUFFIX}"
        args.out = str(out)
        try:
            st = os.statvfs(out.parent)
            req["store_free"] = st.f_bavail * st.f_frsize
        except OSError:
            req["store_free"] = None
    findings = vt.check(src, dst, req)
    report(args, src, dst, req, findings)
    if vt.blocking(findings):
        vt.step("check", "error", "the pre-check refuses this transfer")
        return EXIT_BLOCKED
    engine = next(f["facts"]["engine"] for f in findings if f["code"] == "engine")
    vt.step("check", "done", f"engine: {engine}")
    if kind == "check" or getattr(args, "dry_run", False):
        return EXIT_OK

    server = None
    advertise = None
    if kind in ("import",) or (kind == "migrate" and engine == "file"):
        host, port = _advertise(args, dst_kube)
        rate = int(req["bandwidth"] * MIB) if req.get("bandwidth") else None
        server = vs.DiskServer(bind="0.0.0.0", port=port, rate=rate)
        server.start()
        advertise = f"{host}:{server.port}"
        vt.step("serve", "done", f"disks served on {advertise}")

    ctx = run.Ctx(src_kube, dst_kube, req, tid, server=server, advertise=advertise,
                  source_cluster=(src_name if kind != "import" else src.get("cluster", "")),
                  target_cluster=dst_name or "")
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    writer = None
    try:
        if kind == "export":
            writer = vt.ArchiveWriter(args.out)
            export_class = src.get("default_storage_class") or "harvester-longhorn"
            run.run_export(ctx, writer, export_class, src.get("version", ""))
            run.finish_export(ctx)
            vt.step("done", "done", f"archive written: {args.out}")
        elif kind == "import":
            sources = {}
            sums = reader.sums()
            for d in manifest["inventory"]["disks"]:
                sources[d["volume"]] = (_member_opener(reader, d["member"], sums.get(d["member"])),
                                        reader.member(d["member"])[1])
            run.run_import(ctx, manifest, sources)
            run.finalize(ctx)
            vt.step("done", "done", f"{req['namespace']}/{req['name']} imported on {dst_name}")
        elif engine == "backup":
            run.run_backup(ctx)
            run.finalize(ctx)
            vt.step("done", "done", f"{req['namespace']}/{req['name']} now on {dst_name}")
        else:
            run.run_direct(ctx, src.get("default_storage_class") or "harvester-longhorn")
            run.finalize(ctx)
            vt.step("done", "done", f"{req['namespace']}/{req['name']} now on {dst_name}")
        return EXIT_OK
    except (Cancelled, KeyboardInterrupt) as e:
        vt.step("cancel", "error", f"cancelled ({e})")
        _undo(ctx, writer, args)
        return EXIT_CANCELLED
    except Exception as e:                     # noqa: BLE001
        sid = getattr(e, "sid", "transfer")
        vt.step(sid, "error", str(e)[:400])
        _undo(ctx, writer, args)
        return EXIT_FAIL
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        if server:
            server.stop()


def _undo(ctx, writer, args):
    # un second signal pendant le retour arrière ne doit pas le couper
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        run.rollback(ctx)
    except Exception as e:                     # noqa: BLE001
        vt.step("rollback", "error", str(e)[:300])
    if writer is not None:
        try:
            writer.f.close()
        except Exception:                      # noqa: BLE001
            pass
        Path(args.out).unlink(missing_ok=True)


def _member_opener(reader, member, digest):
    """Lit un disque de l'archive en vérifiant sa somme à la volée : une
    archive abîmée fait échouer l'import au lieu de livrer un disque faux."""
    import hashlib

    def opener():
        h = hashlib.sha256()
        with reader.open_member(member) as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
                yield chunk
        if digest and h.hexdigest() != digest:
            raise IOError(f"{member}: checksum mismatch, the archive is damaged")
    return opener


# ---------------------------------------------------------------------------
# Ligne de commande
# ---------------------------------------------------------------------------

def parser():
    p = argparse.ArgumentParser(prog="harvester-vm-transfer",
                                description="Move, export or import a VM between Harvester clusters.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def source(sp):
        sp.add_argument("--from", dest="from", help="source cluster (name in the configuration)")
        sp.add_argument("--from-kubeconfig", dest="from_kubeconfig", help="source kubeconfig file")
        sp.add_argument("--vm", required=True, help="namespace/name of the VM")

    def target(sp):
        sp.add_argument("--to", dest="to", help="target cluster (name in the configuration)")
        sp.add_argument("--to-kubeconfig", dest="to_kubeconfig", help="target kubeconfig file")
        sp.add_argument("--name", help="VM name on the target (default: same)")
        sp.add_argument("--namespace", help="namespace on the target (default: same)")
        sp.add_argument("--map-net", action="append", metavar="SRC=DST",
                        help="network mapping, ns/name=ns/name (repeatable)")
        sp.add_argument("--map-sc", action="append", metavar="SRC=DST",
                        help="storage class mapping (repeatable)")
        sp.add_argument("--create-namespace", action="store_true",
                        help="create the target namespace if missing")
        sp.add_argument("--target", choices=["started", "stopped"],
                        help="state of the VM on the target (default: started)")
        sp.add_argument("--new-mac", action="store_true",
                        help="give the target VM new MAC addresses")
        sp.add_argument("--serve-address", metavar="HOST[:PORT]",
                        help=f"address the target reaches this host on (default port {DEFAULT_PORT})")

    def speed(sp):
        sp.add_argument("--speed", choices=SPEEDS,
                        help="eco: one disk at a time; normal (default): all disks at once; "
                             "max: also raise Longhorn's backup and restore concurrency "
                             "during the transfer (more CPU and network on every node)")
        sp.add_argument("--parallel", type=int, metavar="N",
                        help="disks copied at the same time through this host (default: all)")
        sp.add_argument("--bandwidth", type=float, metavar="MIB_S",
                        help="cap on the disks served by this host, in MiB/s (default: none)")

    def common(sp):
        sp.add_argument("--json", action="store_true", help="print the pre-check as JSON")
        sp.add_argument("--dry-run", action="store_true", help="check and stop, change nothing")
        sp.add_argument("--id", help=argparse.SUPPRESS)

    c = sub.add_parser("check", help="pre-check a transfer, change nothing")
    source(c)
    target(c)
    common(c)
    c.add_argument("--mode", choices=["stop", "short"])
    c.add_argument("--source", choices=["running", "stopped", "deleted"])
    c.add_argument("--engine", choices=["file"], help="force the copy through this host")
    speed(c)

    m = sub.add_parser("migrate", help="move or copy a VM to another cluster")
    source(m)
    target(m)
    common(m)
    m.add_argument("--mode", choices=["stop", "short"],
                   help="stop: VM stopped during the copy; short: first copy while it runs "
                        "(needs a shared backup target)")
    m.add_argument("--source", choices=["running", "stopped", "deleted"],
                   help="state of the source afterwards (default: stopped)")
    m.add_argument("--keep-backups", action="store_true",
                   help="keep the transfer backups on the backup target")
    m.add_argument("--engine", choices=["file"], help="force the copy through this host")
    speed(m)

    e = sub.add_parser("export", help="export a VM to an archive file")
    source(e)
    common(e)
    e.add_argument("--out", required=True, help="archive file (.hvx) or directory")
    e.add_argument("--source", choices=["running", "stopped"],
                   help="state of the VM afterwards (default: as it was)")

    i = sub.add_parser("import", help="import a VM from an archive file")
    i.add_argument("--in", dest="input", required=True, help="archive file (.hvx)")
    target(i)
    common(i)
    speed(i)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.cmd in ("migrate", "check") and not args.source:
        args.source = "stopped"
    try:
        return execute(args, args.cmd)
    except SystemExit as e:
        if isinstance(e.code, str):
            vt.step("check", "error", e.code)
            print(e.code, file=sys.stderr)
            return EXIT_FAIL
        raise
    except KubeError as e:
        vt.step("check", "error", str(e))
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
