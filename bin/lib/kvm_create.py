"""Création de VMs sur un hôte KVM (prochaine version KVM).

Rien n'est installé sur l'hôte : la console lit ses capacités, fabrique le
XML (kvm_xml.build_domain), les volumes (`vol-create-as`, `vol-create-from`)
et l'ISO cloud-init (iso9660, téléversée par `vol-upload`), puis `virsh
define --validate /dev/stdin`.

Ordre d'une instance : volumes, seed, define, autostart, start, relecture.
L'uuid du domaine est tiré par la console et posé dans le XML : le seed
porte donc dès sa fabrication l'`instance-id` = uuid du domaine, sans
définir d'abord la VM avec un lecteur vide (une écriture de moins, et
jamais de VM définie sans son seed). libvirt refuse un uuid déjà pris.

Retour arrière sur échec ou annulation, dans l'ordre inverse : arrêt si
démarrée, `undefine --nvram` si définie (seulement si l'uuid est le nôtre),
`vol-delete` des volumes créés par cette action (jamais d'un volume existant
choisi comme source). Ce qui reste est nommé (`leftovers`).

Un volume que libvirt refuse de créer parce qu'il existe déjà (créé ailleurs
entre le plan et l'écriture) n'est jamais noté comme créé : le retour
arrière ne le supprime pas. L'absence des volumes de chaque instance est
aussi revérifiée juste avant de les créer (plusieurs instances : le plan a
pu être fait des minutes plus tôt).
"""

import threading

import io
import re
import secrets
import string
import uuid as uuidlib
import xml.etree.ElementTree as ET

import iso9660
import kvm_caps
import kvm_host as kh
import kvm_net
import kvm_ops as ko
import kvm_storage as ks
import kvm_xml as kx

OpError = ko.OpError

# relecture de `vol-info` pendant une copie complète (secondes)
COPY_POLL = 2.0
# attente de la fin d'une copie complète annulée avant d'en supprimer le volume
# (libvirtd la poursuit et refuse la suppression tant qu'elle dure)
BUILDING_DELETE_RETRY = 900.0

INSTANCES_MAX = 20
_VNC_ALPHABET = string.ascii_letters + string.digits
_HOST_CHARS_RE = re.compile(r"[^A-Za-z0-9-]+")


def vnc_password():
    """Mot de passe VNC tiré par la console : 8 caractères (limite du
    protocole), jamais rendu ni journalisé."""
    return "".join(secrets.choice(_VNC_ALPHABET) for _ in range(kx.VNC_PASSWORD_LEN))


def guest_hostname(name):
    """Nom d'hôte invité tiré du nom de la VM (lettres, chiffres, tirets)."""
    h = _HOST_CHARS_RE.sub("-", name).strip("-").lower()[:63].strip("-")
    return h or "vm"


# --- capacités de l'hôte -----------------------------------------------------------

def host_options(host):
    """Options du formulaire pour cet hôte (kvm_caps.form_options), lues par
    `capabilities` puis `domcapabilities` de chaque famille de machine."""
    caps = kvm_caps.parse_capabilities(host.virsh("capabilities", idempotent=True))
    arch = caps.get("arch") or "x86_64"
    cmds = {f: ("domcapabilities", "--virttype", "kvm", "--arch", arch, "--machine", f)
            for f in kvm_caps.MACHINE_FAMILIES}
    out = host.batch(cmds, idempotent=True)
    domcaps = {}
    for f in kvm_caps.MACHINE_FAMILIES:
        try:
            domcaps[f] = kvm_caps.parse_domcapabilities(str(out[f])) if out[f].ok else None
        except ValueError:
            domcaps[f] = None
    return kvm_caps.form_options(caps, domcaps)


# --- plan --------------------------------------------------------------------------

def instance_specs(spec):
    """Un spec par instance : `instances` (1 par défaut) ; au-delà d'une,
    noms suffixés `-01`, `-02`... OpError `invalid` pour un nombre refusé."""
    if not isinstance(spec, dict):
        raise OpError("invalid", "the spec must be an object")
    n = spec.get("instances", 1)
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= INSTANCES_MAX:
        raise OpError("invalid", f"instances: 1 to {INSTANCES_MAX}")
    base = {k: v for k, v in spec.items() if k not in ("instances", "start")}
    if n == 1:
        return [base]
    out = []
    for i in range(1, n + 1):
        s = dict(base)
        s["name"] = f"{spec.get('name')}-{i:02d}"
        out.append(s)
    return out


def _image_refs(specs):
    refs = set()
    for s in specs:
        for d in s.get("disks") or []:
            src = (d or {}).get("source") or {}
            if not isinstance(src, dict):
                continue
            if src.get("kind") == "image" and src.get("image"):
                refs.add((src.get("image_pool") or src.get("pool"), src["image"]))
            elif src.get("kind") in ("iso", "volume"):
                v = src.get("volume") or src.get("image")
                if v:
                    refs.add((src.get("pool"), v))
    return {r for r in refs if isinstance(r[0], str) and isinstance(r[1], str)}


def _read_world(host, specs):
    """Lectures du plan, groupées : images citées, domaines (noms, MAC),
    pools, réseaux, liens de l'hôte."""
    refs = _image_refs(specs)
    cmds = {f"img\0{p}\0{v}": ("vol-dumpxml", "--pool", p, "--vol", v) for p, v in refs}
    for s in specs:
        if isinstance(s.get("name"), str):
            cmds[f"dom\0{s['name']}"] = ("dominfo", "--domain", s["name"])
    out = host.batch(cmds, idempotent=True) if cmds else {}
    images, missing = {}, []
    for p, v in refs:
        r = out[f"img\0{p}\0{v}"]
        if r.ok:
            vol = kh.parse_vol_xml(str(r))
            images[f"{p}/{v}"] = {"capacity": vol.get("capacity"), "format": vol.get("format"),
                                  "allocation": vol.get("allocation"), "path": vol.get("path")}
        elif r.error.kind == "notfound":
            missing.append(f"{p}/{v}")
        else:
            raise r.error
    taken = []
    for s in specs:
        r = out.get(f"dom\0{s.get('name')}")
        if r is None:
            continue
        if r.ok:
            taken.append(s["name"])
        elif r.error.kind != "notfound":
            raise r.error
    doms = ks.read_domains(host)
    macs = sorted({(n.get("mac") or "").lower() for e in doms["domains"].values()
                   for vm in (e["inactive"], e["live"]) if vm for n in vm.get("nics") or []
                   if n.get("mac")})
    pools = {p["name"]: p for p in ks.list_pools(host)}
    nets = {n["name"]: n for n in kvm_net.list_networks(host)}
    need_links = any(n.get("kind") in ("bridge", "direct") for s in specs
                     for n in s.get("nics") or [] if isinstance(n, dict))
    links = kvm_net.host_links(host) if need_links else None
    return {"images": images, "missing": missing, "taken": taken, "macs": macs,
            "pools": pools, "networks": nets, "links": links, "unknown": doms["unknown"]}


def _volume_plan(s, vols, world):
    """Volumes que la console créera pour une instance (spec normalisé)."""
    out = []
    for d, vol in zip(s["disks"], vols["disks"]):
        if not vol or not vol.get("create"):
            continue
        src = d["source"]
        item = {"target": d["target"], "pool": vol["pool"], "volume": vol["volume"],
                "format": vol["format"], "kind": src["kind"], "capacity": None,
                "backing": None, "backing_format": None, "copy_from": None, "needed": 0}
        if src["kind"] == "blank":
            item["capacity"] = int(src["size_bytes"])
            item["needed"] = item["capacity"] if vol["format"] == "raw" else 0
        else:
            ipool = src.get("image_pool") or src["pool"]
            img = world["images"].get(f"{ipool}/{src['image']}") or {}
            cap = img.get("capacity") or 0
            item["capacity"] = max(int(src.get("size_bytes") or 0), cap)
            if src.get("linked") and ipool == src["pool"]:
                item.update(backing=src["image"], backing_format=img.get("format"), needed=0,
                            format="qcow2")
            else:
                item.update(copy_from={"pool": ipool, "volume": src["image"],
                                       "capacity": cap, "format": img.get("format")},
                            needed=min(img.get("allocation") or cap, cap) or cap)
        out.append(item)
    return out


def create_plan(host, spec, options=None, world=None):
    """Plan de création (lecture seule, aussi pour `--dry-run`). Rend
    {possible, refusals, warnings, instances: [{name, volumes, seed,
    xml_preview}]} ; le `xml_preview` porte `passwd='***'`."""
    specs = instance_specs(spec)
    options = options or host_options(host)
    world = world or _read_world(host, specs)
    opts = {**options, "images": world["images"], "macs_in_use": world["macs"]}
    refusals, warnings = [], []
    if spec.get("instances", 1) > 1 and any(isinstance(n, dict) and n.get("mac")
                                            for n in spec.get("nics") or []):
        refusals.append("several instances cannot share fixed MAC addresses")
    for m in world["missing"]:
        refusals.append(f"image or volume {m} not found")
    if world["unknown"]:
        warnings.append(f"unreadable VM definition(s) {', '.join(world['unknown'])}: their MAC "
                        "addresses were not checked")
    instances, space = [], {}
    for s0 in specs:
        reasons = kx.check_spec(s0, opts)
        refusals += [r for r in reasons if r not in refusals]
        if reasons:
            continue
        s = kx.normalize(s0, opts)
        if s["name"] in world["taken"]:
            refusals.append(f"a VM named {s['name']} already exists")
        vols = kx.resolve_volumes(s)
        vplan = _volume_plan(s, vols, world)
        used_pools = {v["pool"] for v in vols["disks"] if v} | (
            {vols["seed"]["pool"]} if vols["seed"] else set())
        for p in sorted(used_pools):
            info = world["pools"].get(p)
            if info is None:
                refusals.append(f"pool {p} not found")
            elif info.get("state") != "running":
                refusals.append(f"pool {p} is not active: start it first")
        to_create = [(v["pool"], v["volume"]) for v in vplan]
        if vols["seed"]:
            to_create.append((vols["seed"]["pool"], vols["seed"]["volume"]))
        for v in vplan:
            space[v["pool"]] = space.get(v["pool"], 0) + v["needed"]
            if v["kind"] == "blank" and v["format"] == "qcow2":
                avail = (world["pools"].get(v["pool"]) or {}).get("available")
                if avail is not None and v["capacity"] > avail:
                    warnings.append(f"disk {v['target']} ({v['capacity']} bytes) can grow beyond "
                                    f"the free space of pool {v['pool']} ({avail} bytes)")
        for n in s["nics"]:
            if n["kind"] == "network":
                net = world["networks"].get(n["source"])
                if net is None:
                    refusals.append(f"network {n['source']} not found")
                elif not net.get("active"):
                    msg = (f"the network {n['source']} is inactive: the VM will not start until "
                           "it is started")
                    if msg not in warnings:
                        warnings.append(msg)
            elif world["links"] is not None:
                names = ({b["name"] for b in world["links"]["bridges"]} if n["kind"] == "bridge"
                         else set(world["links"]["links"]))
                if n["source"] not in names:
                    refusals.append(f"{n['source']} is not a "
                                    f"{'bridge' if n['kind'] == 'bridge' else 'link'} of the host")
        instances.append({"name": s["name"], "spec": s, "vols": vols, "volumes": vplan,
                          "to_create": to_create, "seed": vols["seed"]})
    if not refusals:
        existing = _existing_volumes(host, {p for i in instances for p, _ in i["to_create"]})
        for i in instances:
            for p, v in i["to_create"]:
                if v in existing.get(p, ()):
                    refusals.append(f"a volume named {v} already exists in {p}")
        for p, need in space.items():
            avail = (world["pools"].get(p) or {}).get("available")
            if need and avail is not None and need > avail:
                refusals.append(f"not enough space in pool {p}: {need} bytes needed, "
                                f"{avail} available")
    for i in instances:
        try:
            i["xml_preview"] = kx.build_domain(i["spec"], opts, i["vols"],
                                               vnc_password="***" if i["spec"]["display"]["password"]
                                               else None)
        except OpError as e:
            refusals.append(e.message)
            i["xml_preview"] = None
    public = [{"name": i["name"], "volumes": i["volumes"], "seed": i["seed"],
               "xml_preview": i["xml_preview"]} for i in instances]
    return {"possible": not refusals, "refusals": refusals, "warnings": warnings,
            "instances": public, "_instances": instances, "_options": opts}


def _existing_volumes(host, pools):
    """{pool: {noms}} des volumes des pools (lecture groupée)."""
    cmds = {p: ("vol-list", "--pool", p) for p in pools}
    out = host.batch(cmds, idempotent=True) if cmds else {}
    return {p: {r.get("Name") for r in kh._parse_table(str(out[p]))}
            for p in pools if out[p].ok}


# --- exécution ---------------------------------------------------------------------

def _set_uuid(xml_text, uid):
    root = ET.fromstring(xml_text)
    el = root.find("uuid")
    if el is None:
        el = ET.Element("uuid")
        root.insert(list(root).index(root.find("name")) + 1, el)
    el.text = uid
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode") + "\n"


def seed_iso(cloudinit, instance_id, hostname, vendor_data=None):
    """Octets de l'ISO NoCloud d'une instance (rien n'est écrit sur disque)."""
    ci = cloudinit or {}
    try:
        files = iso9660.seed_files(ci.get("user_data"), ci.get("network_data"), instance_id,
                                   hostname, vendor_data=vendor_data)
        return iso9660.build_iso(files, label="cidata")
    except iso9660.IsoError as e:
        raise OpError("invalid", f"cloud-init seed: {e}") from None


def _already(err):
    """Vrai si libvirt refuse parce que le volume existe déjà."""
    msg = (getattr(err, "message", "") or "").lower()
    return "already" in msg or "exists" in msg


def upload_bytes(host, pool, name, data, cancel=None, created=None):
    """Crée un volume raw et y téléverse `data` (ISO de seed) sur une
    connexion propre. Le volume est noté dans `created` (pour le retour
    arrière de l'appelant) avant sa création, et retiré de la liste si
    libvirt dit qu'il existait déjà : un volume d'un autre n'est jamais
    supprimé."""
    item = {"kind": "volume", "pool": pool, "volume": name}
    if created is not None:
        created.append(item)
    try:
        host.virsh("vol-create-as", "--pool", pool, "--name", name, "--capacity",
                   str(len(data)), "--format", "raw")
    except kh.KvmError as e:
        if created is not None and _already(e):
            created.remove(item)
        raise
    host.run_stream(["env", "LC_ALL=C", "virsh", "-q", "-c",
                     host.decl.get("uri", "qemu:///system"), "vol-upload", "--pool", pool,
                     "--vol", name, "--file", "/dev/stdin"], io.BytesIO(data), size=len(data),
                    cancel=cancel)


def _vol_allocation(host, pool, vol):
    """Octets alloués d'un volume (`vol-info --bytes`), None si illisible."""
    try:
        info = kh._colon_fields(host.virsh("vol-info", "--bytes", "--pool", pool, "--vol", vol,
                                           idempotent=True))
        return int((info.get("Allocation") or "").split()[0])
    except (kh.KvmError, ValueError, IndexError):
        return None


def _with_progress(host, work, pool, vol, report, poll=None):
    """Lance `work()` (copie longue, connexion propre) dans un fil et relit
    `vol-info` du volume de destination toutes les `poll` secondes sur la
    connexion partagée ; `report(octets)` à chaque changement. L'exception
    de `work` est relevée telle quelle."""
    box = {}

    def run():
        try:
            box["out"] = work()
        except BaseException as e:      # noqa: BLE001 - relevée dans le fil appelant
            box["exc"] = e
    t = threading.Thread(target=run, daemon=True, name="kvm-copy")
    t.start()
    last = None
    poll = COPY_POLL if poll is None else poll
    while True:
        t.join(poll)
        if not t.is_alive():
            break
        alloc = _vol_allocation(host, pool, vol)
        if alloc is not None and alloc != last:
            last = alloc
            report(alloc)
    if "exc" in box:
        raise box["exc"]
    return box.get("out")


def _create_volume(host, v, created, cancel, report=None, poll=None):
    """Un volume du plan : vierge, lié ou copie complète (connexion propre,
    octets copiés rendus par `report(octets)` d'après `vol-info`)."""
    if v["copy_from"]:
        src = v["copy_from"]
        item = {"kind": "volume", "pool": v["pool"], "volume": v["volume"], "building": True}
        created.append(item)
        try:
            _with_progress(host, lambda: ks.virsh_input(
                host, ("vol-create-from", "--pool", v["pool"], "--file", "/dev/stdin",
                       "--vol", src["volume"], "--inputpool", src["pool"]),
                ko.volume_xml(v["volume"], src["capacity"], v["format"]),
                timeout=3600, fresh=True), v["pool"], v["volume"], report or (lambda n: None),
                poll)
        except kh.KvmError as e:
            if _already(e):
                created.remove(item)         # pas le nôtre : jamais supprimé
            raise
        item.pop("building")
        if v["capacity"] > (src["capacity"] or 0):
            host.virsh("vol-resize", "--pool", v["pool"], "--vol", v["volume"], "--capacity",
                       str(v["capacity"]))
    else:
        args = ks.create_volume_argv(v["pool"], v["volume"], v["capacity"], v["format"],
                                     v["backing"], v["backing_format"])
        created.append({"kind": "volume", "pool": v["pool"], "volume": v["volume"]})
        try:
            host.virsh(*args)
        except kh.KvmError as e:
            if _already(e):
                created.pop()                # pas le nôtre : jamais supprimé
            raise


def rollback(host, created, sleep=None, clock=None):
    """Défait ce qu'une création a fait, dans l'ordre inverse ; rend les
    restes. Un domaine n'est retiré que s'il porte l'uuid tiré par nous.

    Un volume en cours de copie complète (`building`, création annulée
    pendant `vol-create-from`) : libvirtd poursuit la copie et refuse la
    suppression (« still being allocated ») jusqu'à sa fin ; le retour
    arrière réessaie avec un recul borné (BUILDING_DELETE_RETRY secondes)
    puis le nomme en reste s'il est toujours en copie."""
    leftovers = []
    for item in reversed(created):
        public = {k: v for k, v in item.items() if k not in ("uuid", "building")}
        if item["kind"] == "volume" and item.get("building"):
            left = ks._delete_partial(host, item["pool"], item["volume"],
                                      retry_for=BUILDING_DELETE_RETRY, sleep=sleep, clock=clock)
            if left:
                leftovers.append({**public, "error": left["error"] + (
                    f"; libvirtd may still be building volume {item['pool']}/"
                    f"{item['volume']}: remove it once the copy ends")})
            continue
        try:
            if item["kind"] == "started":
                host.virsh("destroy", "--domain", item["name"])
            elif item["kind"] == "domain":
                info = kh.parse_dominfo(host.virsh("dominfo", "--domain", item["name"],
                                                   idempotent=True))
                if info.get("uuid") != item["uuid"]:
                    continue                 # ce n'est pas notre VM
                if info.get("state") not in ("shut off", None):
                    host.virsh("destroy", "--domain", item["name"])
                host.virsh("undefine", "--domain", item["name"], "--nvram")
            elif item["kind"] == "volume":
                host.virsh("vol-delete", "--pool", item["pool"], "--vol", item["volume"])
        except kh.KvmError as e:
            if e.kind == "notfound" or (item["kind"] == "started"
                                        and "not running" in e.message.lower()):
                continue
            msg = e.message
            if item.get("building"):
                msg += (f"; libvirtd may still be building volume {item['pool']}/"
                        f"{item['volume']}: remove it once the copy ends")
            leftovers.append({**public, "error": msg})
    return leftovers


def create_vm(host, spec, progress=None, cancel=None, options=None, uuid_gen=uuidlib.uuid4,
              password_gen=vnc_password, start=None, before_write=None):
    """Crée les instances d'un spec : plan recalculé ici (jamais l'aperçu),
    puis pour chaque instance volumes, seed, define, autostart, start,
    relecture. `before_write(volumes, networks)` : appelé une fois le plan
    accepté et avant toute écriture, avec les (pool, volume) que la création
    va créer (disques et seed) et les réseaux cités ; l'appelant y prend ses
    verrous, une exception l'arrête sans rien avoir créé.
    `progress(étape, statut, message)` reçoit `plan`,
    `volume-<cible>`, `seed`, `define`, `start`, `verify` (préfixés du nom
    d'instance quand il y en a plusieurs). `cancel()` : consulté entre deux
    étapes et pendant les téléversements ; lève kh.Cancelled.

    Une instance réussie reste ; sur échec d'une instance, ses objets sont
    retirés et les suivantes ne sont pas commencées : OpError (ou
    l'exception d'origine) avec `data` / attributs `created` (réussites),
    `failed` (nom) et `leftovers`."""
    start = spec.get("start", False) if start is None else start

    def step(sid, status, msg=""):
        if progress:
            progress(sid, status, msg)

    def check_cancel():
        if cancel is not None and cancel():
            raise kh.Cancelled(0, "creation cancelled")

    step("plan", "running", str(spec.get("name") if isinstance(spec, dict) else "?"))
    plan = create_plan(host, spec, options)
    if not plan["possible"]:
        step("plan", "failed", "; ".join(plan["refusals"]))
        raise OpError("refused", "; ".join(plan["refusals"]),
                      {"refusals": plan["refusals"], "plan": _public(plan)})
    if before_write is not None:
        before_write(sorted({pv for i in plan["_instances"] for pv in i["to_create"]}),
                     sorted({n["source"] for i in plan["_instances"] for n in i["spec"]["nics"]
                             if n.get("kind") == "network"}))
    step("plan", "done", f"{len(plan['instances'])} VM(s)"
         + (f"; {'; '.join(plan['warnings'])}" if plan["warnings"] else ""))
    opts = plan["_options"]
    multi = len(plan["_instances"]) > 1
    done = []
    with ko.writing(host):
        return _create_instances(host, plan, opts, multi, done, step, check_cancel, cancel,
                                 uuid_gen, password_gen, start)


def _create_instances(host, plan, opts, multi, done, step, check_cancel, cancel, uuid_gen,
                      password_gen, start):
    for inst in plan["_instances"]:
        s, name = inst["spec"], inst["name"]
        prefix = f"{name}:" if multi else ""
        created = []
        password = password_gen() if s["display"]["vnc"] and s["display"]["password"] else None
        try:
            check_cancel()
            # revérifié juste avant d'écrire : une instance précédente a pu
            # prendre des minutes, quelqu'un a pu créer un volume du même nom
            existing = _existing_volumes(host, {p for p, _ in inst["to_create"]})
            taken = [f"{p}/{v}" for p, v in inst["to_create"] if v in existing.get(p, ())]
            if taken:
                raise OpError("refused", f"volume(s) created meanwhile: {', '.join(taken)}",
                              {"refusals": [f"a volume named {t} already exists" for t in taken]})
            for v in inst["volumes"]:
                sid = f"{prefix}volume-{v['target']}"
                step(sid, "running", f"{v['pool']}/{v['volume']}"
                     + (" (linked)" if v["backing"] else " (copy)" if v["copy_from"] else ""))
                total = (v["copy_from"] or {}).get("capacity")
                _create_volume(host, v, created, cancel, report=lambda n, sid=sid, total=total: step(
                    sid, "running", f"{n} bytes copied" + (f" (image: {total} bytes)"
                                                           if total else "")))
                step(sid, "done", f"{v['pool']}/{v['volume']}")
                check_cancel()
            uid = str(uuid_gen())
            if inst["seed"]:
                seed = inst["seed"]
                step(f"{prefix}seed", "running", f"{seed['pool']}/{seed['volume']}")
                ci = s.get("cloudinit") or {}
                data = seed_iso(ci, uid, ci.get("hostname") or guest_hostname(name))
                upload_bytes(host, seed["pool"], seed["volume"], data, cancel, created=created)
                step(f"{prefix}seed", "done", f"{len(data)} bytes, user-data "
                     f"{'present' if ci.get('user_data') else 'empty'}")
                check_cancel()
            step(f"{prefix}define", "running", name)
            xml = _set_uuid(kx.build_domain(s, opts, inst["vols"], vnc_password=password), uid)
            created.append({"kind": "domain", "name": name, "uuid": uid})
            ks.virsh_input(host, ("define", "--validate", "/dev/stdin"), xml,
                           secrets=[password] if password else ())
            if s.get("autostart"):
                host.virsh("autostart", "--domain", name)
            step(f"{prefix}define", "done", f"uuid {uid}")
            check_cancel()
            if start:
                step(f"{prefix}start", "running", name)
                created.append({"kind": "started", "name": name})
                host.virsh("start", "--domain", name)
                step(f"{prefix}start", "done", name)
            step(f"{prefix}verify", "running", name)
            out = host.batch({"info": ("dominfo", "--domain", name),
                              "xml": ("dumpxml", "--inactive", "--domain", name)},
                             idempotent=True)
            info = kh.parse_dominfo(ko._need(out, "info"))
            vm = kh.parse_domain_xml(ko._need(out, "xml"))
            if info.get("uuid") != uid:
                raise OpError("failed", f"{name} has uuid {info.get('uuid')}, not the one defined")
            step(f"{prefix}verify", "done", info.get("state") or "")
            done.append({"name": name, "uuid": uid, "state": info.get("state"),
                         "autostart": info.get("autostart"),
                         "volumes": [f"{v['pool']}/{v['volume']}" for v in inst["volumes"]],
                         "seed": (f"{inst['seed']['pool']}/{inst['seed']['volume']}"
                                  if inst["seed"] else None),
                         "macs": [n.get("mac") for n in vm.get("nics") or []],
                         "vnc_password": bool(password)})
        except BaseException as exc:
            leftovers = rollback(host, created)
            exc.leftovers = leftovers
            exc.created = list(done)
            exc.failed = name
            if isinstance(exc, OpError):
                exc.data.update(leftovers=leftovers, created=list(done), failed=name)
            raise
    return {"created": done, "warnings": plan["warnings"], "leftovers": []}


def _public(plan):
    return {k: v for k, v in plan.items() if not k.startswith("_")}


def public_plan(plan):
    """Plan sans ses champs internes (spec normalisé, options)."""
    return _public(plan)
