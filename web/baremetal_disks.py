"""Disques d'une installation bare-metal (1.78.0) : lecture de l'inventaire
renvoyé par le démarrage de découverte, chemin stable proposé pour chaque
disque, contrôle des rôles (système, données, pools, effacement) avant
l'installation, et champs de configuration qui en découlent.

Format de l'inventaire (contrat avec le script de découverte) : des
sections introduites par une ligne `== <nom>` ; une section inconnue est
ignorée, une section absente vaut vide.

- `== lsblk` : sortie JSON de `lsblk -J -b -O` (tailles en octets) ;
- `== links` : une ligne par lien de `/dev/disk/by-id` et
  `/dev/disk/by-path`, `<chemin complet du lien> <cible résolue>`, la
  cible donnée par `readlink -f` (`/dev/sdb`, `/dev/dm-0`, ...) ;
- `== nics` : sortie JSON de `ip -j link` ;
- `== speeds` (facultative) : `<interface> <Mbit/s>` lu dans
  `/sys/class/net/<interface>/speed` ; `-1` ou vide = inconnue ;
- `== dmi` (facultative) : `serial <valeur>` et `uuid <valeur>`, lus dans
  `/sys/class/dmi/id/product_serial` et `product_uuid`.

Module pur (aucune entrée/sortie), testé sans cluster."""
import json
import re

GIB = 1 << 30

# Tailles minimales de l'installeur v1.9 (pkg/config/config.go), en Gio
# entiers : l'installeur compare `octets >> 30`, arrondi par défaut.
SINGLE_DISK_MIN_GIB = 250      # SingleDiskMinSizeGiB : disque système seul
MULTIPLE_DISK_MIN_GIB = 180    # MultipleDiskMinSizeGiB : système + données
DATA_DISK_MIN_GIB = 50         # HardMinDataDiskSizeGiB : données ou pool

POOL_TAG_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,30}[a-z0-9])?$")

# Préfixes `by-id` retenus, dans l'ordre de préférence. Les autres
# (`dm-name-`, `dm-uuid-`, `lvm-pv-`, `md-`, `usb-`...) restent listés
# mais ne sont jamais proposés.
BY_ID_ORDER = ("nvme-eui.", "nvme-", "wwn-", "ata-", "scsi-")

# Types lsblk retenus comme disques au premier niveau : `disk`, qui
# couvre aussi un volume de contrôleur RAID matériel (une grappe logicielle
# n'apparaît qu'en enfant de ses membres). Le reste (loop, rom, ram, zram)
# n'est jamais un disque cible.
_DISK_TYPES = ("disk",)
_LIVE_MOUNT = "/run/initramfs/live"
# Signatures posées par le système live lui-même, pas des données du
# disque : `mpath_member` vient de multipathd (vérifié sur le banc).
_NOT_DATA_FSTYPES = ("mpath_member",)
_PART_SUFFIX_RE = re.compile(r"-part\d+$")


def split_sections(text):
    """`{nom: [lignes]}` des sections `== <nom>`."""
    sections, cur = {}, None
    for line in str(text or "").splitlines():
        m = re.match(r"^== (\S+)\s*$", line)
        if m:
            cur = m.group(1)
            sections.setdefault(cur, [])
        elif cur is not None:
            sections[cur].append(line)
    return sections


def _json_section(lines, default):
    body = "\n".join(lines).strip()
    if not body:
        return default
    try:
        return json.loads(body)
    except ValueError:
        return default


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true")
    return bool(value)


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_links(lines):
    """`{cible: {"by_id": [...], "by_path": [...]}}`, cible = nom noyau
    (`sdb`, `dm-0`)."""
    out = {}
    for line in lines:
        parts = line.split()
        if len(parts) != 2:
            continue
        link, target = parts
        m = re.match(r"^/dev/disk/by-(id|path)/[^/]+$", link)
        if not m or not target.startswith("/dev/"):
            continue
        kind = "by_id" if m.group(1) == "id" else "by_path"
        name = target[len("/dev/"):]
        entry = out.setdefault(name, {"by_id": [], "by_path": []})
        if link not in entry[kind]:
            entry[kind].append(link)
    return out


def _collect_partitions(dev):
    """Partitions d'un disque, y compris celles vues à travers son
    périphérique multipath (enfant `mpath`)."""
    parts = []
    for child in dev.get("children") or []:
        ctype = child.get("type")
        if ctype == "part":
            parts.append({"name": child.get("name"),
                          "fstype": _text(child.get("fstype")),
                          "size_bytes": _int(child.get("size"))})
        elif ctype == "mpath":
            parts.extend(_collect_partitions(child))
    return parts


def _mounted_at(dev, mount):
    if dev.get("mountpoint") == mount or mount in (dev.get("mountpoints") or []):
        return True
    return any(_mounted_at(c, mount) for c in dev.get("children") or [])


def _by_path_rank(link):
    base = link.rsplit("/", 1)[-1]
    # `pci-...` d'abord (`virtio-pci-` est l'ancienne forme) ; pour l'ATA,
    # la forme `ata-N.M` est la courante, `ata-N` la compatibilité.
    return (0 if base.startswith("pci-") else 1,
            1 if re.search(r"-ata-\d+$", base) else 0,
            base)


def stable_path(disk):
    """`(chemin, genre)` : `by-path`, sinon un `by-id` qui mène au disque
    lui-même dans l'ordre de BY_ID_ORDER, sinon `/dev/<nom>` (genre
    `kernel`, à signaler). Les liens de `disk["links"]` ne portent que ceux
    qui visent le disque : un lien vers un `dm-*` n'y figure jamais."""
    links = disk.get("links") or {}
    by_path = [x for x in links.get("by_path") or []
               if not _PART_SUFFIX_RE.search(x)]
    if by_path:
        return sorted(by_path, key=_by_path_rank)[0], "by-path"
    by_id = [x for x in links.get("by_id") or [] if not _PART_SUFFIX_RE.search(x)]
    for prefix in BY_ID_ORDER:
        cands = sorted(x for x in by_id
                       if x.rsplit("/", 1)[-1].startswith(prefix)
                       and not (prefix == "nvme-"
                                and x.rsplit("/", 1)[-1].startswith("nvme-eui.")))
        if cands:
            # le plus court d'abord : `nvme-<modèle>_<série>` avant la
            # variante `_1` qui désigne l'espace de noms ; la forme
            # `nvme-nvme.<vendeur>-...` en dernier
            return sorted(cands, key=lambda x: (
                "/nvme-nvme." in x, len(x), x))[0], "by-id"
    return "/dev/" + str(disk.get("name") or ""), "kernel"


def _disk(dev, links):
    name = dev.get("name") or ""
    kname = dev.get("kname") or name
    own = links.get(kname) or links.get(name) or {"by_id": [], "by_path": []}
    parts = _collect_partitions(dev)
    fstype = _text(dev.get("fstype"))
    has_data = bool(parts or _text(dev.get("pttype"))
                    or (fstype and fstype not in _NOT_DATA_FSTYPES))
    disk = {
        "name": name,
        "size_bytes": _int(dev.get("size")),
        "model": _text(dev.get("model")),
        "serial": _text(dev.get("serial")),
        "wwn": _text(dev.get("wwn")),
        "rotational": _bool(dev.get("rota")),
        "transport": _text(dev.get("tran")),
        "type": dev.get("type"),
        "fstype": fstype if fstype not in _NOT_DATA_FSTYPES else None,
        "multipath": any(c.get("type") == "mpath" for c in dev.get("children") or []),
        "partitions": parts,
        "has_data": has_data,
        "links": {"by_id": sorted(own["by_id"]), "by_path": sorted(own["by_path"])},
    }
    disk["stable_path"], disk["stable_kind"] = stable_path(disk)
    return disk


def _nics(entries, speeds):
    out = []
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict) or e.get("link_type") != "ether":
            continue
        name = e.get("ifname")
        out.append({"name": name,
                    "mac": _text(e.get("address")),
                    "state": _text(e.get("operstate")),
                    "speed": speeds.get(name)})
    return out


def _speeds(lines):
    out = {}
    for line in lines:
        parts = line.split()
        if len(parts) == 2 and re.fullmatch(r"\d+", parts[1]) and int(parts[1]) > 0:
            out[parts[0]] = int(parts[1])
    return out


def parse_discovery(text):
    """Inventaire du démarrage de découverte : `{"disks": [...], "nics":
    [...]}`. Le média de démarrage (monté en /run/initramfs/live), les
    lecteurs optiques et les périphériques loop sont écartés."""
    sec = split_sections(text)
    tree = _json_section(sec.get("lsblk", []), {})
    links = _parse_links(sec.get("links", []))
    disks = []
    for dev in (tree.get("blockdevices") if isinstance(tree, dict) else None) or []:
        if not isinstance(dev, dict) or dev.get("type") not in _DISK_TYPES:
            continue
        if _mounted_at(dev, _LIVE_MOUNT):
            continue
        disks.append(_disk(dev, links))
    out = {"disks": disks,
           "nics": _nics(_json_section(sec.get("nics", []), []),
                         _speeds(sec.get("speeds", [])))}
    # 1.78.0 : identité de la machine (`serial <v>`, `uuid <v>`), présente
    # seulement si l'inventaire porte la section ; valeurs brutes, un
    # remplissage de firmware (« Not Specified ») est laissé tel quel.
    if "dmi" in sec:
        dmi = {"serial": None, "uuid": None}
        for line in sec["dmi"]:
            key, _, value = line.partition(" ")
            if key in dmi:
                dmi[key] = value.strip() or None
        out["dmi"] = dmi
    return out


def _index(disks):
    """Chaque nom par lequel un disque peut être désigné (chemin stable,
    liens, /dev/<nom>) vers le disque."""
    idx = {}
    for d in disks:
        names = [d.get("stable_path"), "/dev/" + str(d.get("name") or "")]
        links = d.get("links") or {}
        names += list(links.get("by_id") or []) + list(links.get("by_path") or [])
        for n in names:
            if n:
                idx.setdefault(n, d)
    return idx


def _gib(disk):
    return int(disk.get("size_bytes") or 0) >> 30


def check_disk_roles(disks, roles, skipchecks=False, wipe_all=False):
    """Contrôles avant installation. `roles` = `{"os": id, "data": id|None,
    "pools": {étiquette: [ids]}, "wipe": [ids]}`, un id étant le chemin
    stable (tout lien connu du disque ou /dev/<nom> est aussi accepté).

    Rend `[(chemin, raison)]`, vide si tout va ; raisons : `no-os`,
    `unknown-disk`, `role-twice`, `too-small:<Gio minimum>`, `has-data`
    (partitions ou système de fichiers sans effacement demandé),
    `pool-tag` (le chemin porte alors l'étiquette refusée)."""
    roles = roles or {}
    idx = _index(disks or [])
    errors = []

    def add(path, reason):
        if (path, reason) not in errors:
            errors.append((path, reason))

    os_id = (roles.get("os") or "").strip() if isinstance(roles.get("os"), str) else ""
    data_id = (roles.get("data") or "").strip() if isinstance(roles.get("data"), str) else ""
    pools = roles.get("pools") or {}
    wipe_ids = [w for w in roles.get("wipe") or [] if isinstance(w, str) and w.strip()]

    # (id, rôle) dans l'ordre : système, données, pools
    assigned = []
    if not os_id:
        add("", "no-os")
    else:
        assigned.append((os_id, "os"))
    if data_id:
        assigned.append((data_id, "data"))
    for tag in sorted(pools):
        if not POOL_TAG_RE.match(str(tag)):
            add(str(tag), "pool-tag")
        for pid in pools.get(tag) or []:
            if isinstance(pid, str) and pid.strip():
                assigned.append((pid.strip(), "pool"))

    wiped = set()
    for w in wipe_ids:
        d = idx.get(w.strip())
        if d is None:
            add(w.strip(), "unknown-disk")
        else:
            wiped.add(d["name"])

    seen = {}
    for rid, role in assigned:
        d = idx.get(rid)
        if d is None:
            add(rid, "unknown-disk")
            continue
        if d["name"] in seen:
            add(rid, "role-twice")
            continue
        seen[d["name"]] = role
        if not skipchecks:
            if role == "os":
                need = MULTIPLE_DISK_MIN_GIB if data_id else SINGLE_DISK_MIN_GIB
            else:
                need = DATA_DISK_MIN_GIB
            if _gib(d) < need:
                add(rid, f"too-small:{need}")
        if d.get("has_data") and not wipe_all and d["name"] not in wiped:
            add(rid, "has-data")
    return errors


def install_fields(disks, roles):
    """Champs du formulaire d'installation (`device`, `data_disk`,
    `wipe_disks_list`) écrits avec le chemin stable de chaque disque, à
    passer à `harvester_install_schema.build_form_config`.

    Les disques système et de données sont retirés de `wipe_disks_list` :
    l'installeur les formate lui-même et vide la liste qui les contient.
    Un id inconnu est gardé tel quel (saisie libre), les contrôles de
    `check_disk_roles` le signalent à part."""
    roles = roles or {}
    idx = _index(disks or [])

    def resolve(rid):
        rid = (rid or "").strip() if isinstance(rid, str) else ""
        if not rid:
            return "", None
        d = idx.get(rid)
        return (d["stable_path"], d["name"]) if d else (rid, None)

    device, os_name = resolve(roles.get("os"))
    data_disk, data_name = resolve(roles.get("data"))
    skip = {x for x in (device, data_disk, os_name, data_name) if x}
    wipe = []
    for w in roles.get("wipe") or []:
        path, name = resolve(w)
        if not path or path in skip or (name and name in skip) or path in wipe:
            continue
        wipe.append(path)
    return {"device": device, "data_disk": data_disk, "wipe_disks_list": wipe}
