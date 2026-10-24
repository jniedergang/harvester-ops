"""Images, volumes et pools d'un hôte KVM (prochaine version KVM).

Tout passe par `virsh` sur l'hôte, à travers kvm_host.VirshHost. Les XML
(pools, volumes) sont fabriqués ici et partent sur l'entrée standard
(`--file /dev/stdin`), jamais dans un fichier temporaire de l'hôte.

Règles reprises des versions précédentes :
- lectures rejouées (`idempotent=True`), écritures jamais ;
- écritures longues (téléversement, copie, contrôle d'empreinte) sur une
  connexion propre (`run_stream`, `fresh=True`) ;
- un volume, un pool ou un réseau n'est supprimé que si aucune définition
  de domaine (active ou inactive) ne le cite ; une lecture manquée ne
  prouve rien et refuse.

Constats faits sur node2 (libvirt 12.8.0, OpenSSH 10.6p1, 24/10/2026) :
- `vol-upload --file /dev/stdin` accepte un tuyau à travers ssh ; avec
  `--sparse`, libvirt cherche la position du flux et échoue (« Unable to get
  current position in stream », « Illegal seek ») : jamais `--sparse` ici ;
- `vol-download --file /dev/stdout` ajoute un saut de ligne final (celui que
  virsh écrit après chaque commande) : toute lecture d'octets passe par
  `virsh -q`, sinon l'empreinte calculée sur l'hôte est fausse ;
- libvirt sonde le format réel (qcow2) dès la fin du téléversement d'un
  volume créé en `raw` dans un pool `dir`.
"""

import datetime
import hashlib
import ipaddress
import json
import os
import posixpath
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import kvm_host as kh
import kvm_ops as ko
import qcow2

OpError = ko.OpError

VOLUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,250}$")
# pool créé par la console ; un pool existant se désigne par tout nom libvirt
POOL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
VOLUME_FORMATS = ("qcow2", "raw")
# décision 67 : seuls ces types se créent depuis la console
CREATABLE_POOL_TYPES = ("dir", "netfs")
# cibles de pool interdites : égales, parentes ou enfants (sauf de « / »).
# Un pool `dir` imbriqué dans le répertoire d'un autre pool `dir` est accepté :
# vu sur node2 (7 pools sur 9 sous /var/lib/libvirt/images, lui-même un pool),
# libvirt ne descend pas dans les sous-répertoires, un fichier n'appartient
# donc qu'à un pool ; le pool parent voit seulement le répertoire de l'enfant
# comme un volume de format `dir`, que la console ne liste ni ne supprime.
# Tout autre type (netfs, fs, logical...) qui contient la cible d'un pool ou
# s'y trouve est refusé : un montage cacherait les fichiers de l'autre pool.
# Les cibles se comparent par identité (`stat -L -c %d:%i`, liens suivis) en
# plus du texte.
FORBIDDEN_POOL_PATHS = ("/", "/boot", "/dev", "/etc", "/proc", "/run", "/sys", "/usr",
                        "/var/lib/libvirt/qemu", "/var/run")
_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                          r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
NOTES_DIR = "kvm-images"
REASON_UNPROVEN = "could not prove that no VM uses it"
REASON_DIRECTORY = "a directory, not a disk image"
# plafond de taille virtuelle d'un qcow2 téléversé (réglable)
MAX_VIRTUAL_SIZE = qcow2.MAX_VIRTUAL_SIZE
# débit minimal supposé pour borner un contrôle d'empreinte sur l'hôte
VERIFY_MIN_BPS = 50 << 20


# --- noms -------------------------------------------------------------------

def check_volume_name(name):
    """Nom d'un volume créé ou téléversé par la console : ni `/` ni `..`,
    commence par une lettre ou un chiffre."""
    if not isinstance(name, str) or not VOLUME_RE.match(name) or ".." in name:
        raise OpError("invalid", "invalid volume name: letters, digits, '.', '_', '+' and '-', "
                      "starting with a letter or digit, without '..'")
    return name


def check_pool_name(name, new=False):
    """`new` : pool créé par la console (POOL_NAME_RE) ; sinon tout nom
    libvirt (pas de caractère de contrôle)."""
    if new:
        if not isinstance(name, str) or not POOL_NAME_RE.match(name):
            raise OpError("invalid", "invalid pool name: letters, digits, '.', '_' and '-', "
                          "64 characters at most, starting with a letter or digit")
        return name
    try:
        return ko.check_vm_name(name)
    except OpError:
        raise OpError("invalid", "invalid pool name") from None


def _check_size(value, what):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OpError("invalid", f"{what} must be a positive number of bytes")
    return value


# --- aides communes ------------------------------------------------------------

def virsh_input(host, args, data, timeout=None, secrets=(), fresh=False):
    """`virsh <args>` avec `data` (XML) sur l'entrée standard."""
    return host.run_input(["env", "LC_ALL=C", *host._virsh_argv(args)], data, timeout=timeout,
                          secrets=secrets, fresh=fresh)


def read_domains(host):
    """Définitions de tous les domaines : {nom: {"active", "persistent"?,
    "inactive": vm, "live": vm|None}} plus `unknown` (domaines illisibles).
    Une lecture manquée est rendue, jamais ignorée : l'appelant refuse."""
    first = host.batch({"all": ("list", "--all", "--name"), "active": ("list", "--name")},
                       idempotent=True)
    names = kh.parse_names(ko._need(first, "all"))
    active = set(kh.parse_names(ko._need(first, "active")))
    cmds = {}
    for d in names:
        cmds[f"inactive\0{d}"] = ("dumpxml", "--inactive", "--domain", d)
        if d in active:
            cmds[f"live\0{d}"] = ("dumpxml", "--domain", d)
    out = host.batch(cmds, idempotent=True) if cmds else {}
    doms, unknown = {}, []
    for d in names:
        entry = {"active": d in active, "inactive": None, "live": None}
        for kind in ("inactive", "live"):
            v = out.get(f"{kind}\0{d}")
            if v is None:
                continue
            if not v.ok:
                if v.error.kind != "notfound":
                    unknown.append(d)
                continue
            try:
                entry[kind] = kh.parse_domain_xml(str(v))
            except (ET.ParseError, ValueError):
                unknown.append(d)
        if entry["inactive"] or entry["live"]:
            doms[d] = entry
    return {"domains": doms, "unknown": sorted(set(unknown))}


def _under(path, parent):
    path, parent = posixpath.normpath(path), posixpath.normpath(parent)
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def _disk_in_pool(disk, pool, target_path):
    """Vrai si le disque cite un volume du pool (`pool/volume`) ou un
    fichier sous sa cible."""
    src = disk.get("source") or ""
    if disk.get("type") == "volume":
        return src.partition("/")[0] == pool
    if target_path and src.startswith("/"):
        return _under(src, target_path)
    return False


def pool_users(domains, pool, target_path, live_only=False):
    """Domaines dont une définition cite le pool. `live_only` : seulement
    les domaines en marche, d'après leur XML vivant."""
    users = []
    for name, e in domains["domains"].items():
        vms = [e["live"]] if live_only else [e["inactive"], e["live"]]
        if live_only and not e["active"]:
            continue
        if any(_disk_in_pool(d, pool, target_path) for vm in vms if vm
               for d in vm.get("disks") or []):
            users.append(name)
    return sorted(users)


def volume_users(domains, pool, volume, path):
    """Domaines dont une définition cite ce volume (par `pool/volume` ou
    par son chemin)."""
    users = []
    for name, e in domains["domains"].items():
        for vm in (e["inactive"], e["live"]):
            for d in (vm or {}).get("disks") or []:
                src = d.get("source") or ""
                if (d.get("type") == "volume" and src == f"{pool}/{volume}") or (
                        path and src.startswith("/")
                        and posixpath.normpath(src) == posixpath.normpath(path)):
                    if name not in users:
                        users.append(name)
    return sorted(users)


def pool_state(host, pool):
    """{name, type, target_path, available, state, autostart, persistent,
    source_host, source_dir} d'un pool ; OpError notfound s'il n'existe pas."""
    out = host.batch({"xml": ("pool-dumpxml", "--pool", pool),
                      "info": ("pool-info", "--pool", pool)}, idempotent=True)
    for k in ("xml", "info"):
        if not out[k].ok and out[k].error.kind == "notfound":
            raise OpError("notfound", f"{host.name}: pool {pool} not found")
    info = kh._colon_fields(ko._need(out, "info"))
    xml = ko._need(out, "xml")
    st = kh.parse_pool_xml(xml)
    root = kh._safe_xml(xml)
    h = root.find("source/host")
    st.update(state=info.get("State", ""), autostart=info.get("Autostart") == "yes",
              persistent=info.get("Persistent") == "yes",
              source_host=h.get("name") if h is not None else None,
              source_dir=(root.find("source/dir").get("path")
                          if root.find("source/dir") is not None else None))
    return st


def list_pools(host):
    """Tous les pools (actifs ou non) avec leur XML lu : [{...pool_state}]."""
    names = kh.parse_names(host.virsh("pool-list", "--all", "--name", idempotent=True))
    cmds = {}
    for p in names:
        cmds[f"xml\0{p}"] = ("pool-dumpxml", "--pool", p)
        cmds[f"info\0{p}"] = ("pool-info", "--pool", p)
    out = host.batch(cmds, idempotent=True) if cmds else {}
    pools = []
    for p in names:
        x, i = out[f"xml\0{p}"], out[f"info\0{p}"]
        if not x.ok or not i.ok:
            if (not x.ok and x.error.kind == "notfound") or (not i.ok and i.error.kind == "notfound"):
                continue                    # retiré entre-temps
            raise (x.error if not x.ok else i.error)
        st = kh.parse_pool_xml(str(x))
        info = kh._colon_fields(str(i))
        st.update(state=info.get("State", ""), autostart=info.get("Autostart") == "yes",
                  persistent=info.get("Persistent") == "yes")
        pools.append(st)
    return pools


def pool_volumes(host, pool):
    """{nom: vol} (parse_vol_xml) des volumes d'un pool actif."""
    names = [v["name"] for v in kh.parse_vol_list_details(
        host.virsh("vol-list", "--pool", pool, "--details", idempotent=True))]
    cmds = {n: ("vol-dumpxml", "--pool", pool, "--vol", n) for n in names}
    out = host.batch(cmds, idempotent=True) if cmds else {}
    vols = {}
    for n in names:
        v = out[n]
        if v.ok:
            vols[n] = kh.parse_vol_xml(str(v))
        elif v.error.kind != "notfound":
            raise v.error
    return vols


def _volume(host, pool, name):
    """parse_vol_xml d'un volume, None s'il n'existe pas."""
    try:
        return kh.parse_vol_xml(host.virsh("vol-dumpxml", "--pool", pool, "--vol", name,
                                           idempotent=True))
    except kh.KvmError as e:
        if e.kind == "notfound":
            return None
        raise


def _require_active_pool(host, pool):
    st = pool_state(host, pool)
    if st["state"] != "running":
        raise OpError("refused", f"pool {pool} is not active ({st['state'] or 'unknown'}): "
                      "start it first")
    return st


# --- notes de la console sur les images ------------------------------------------

def notes_path(state, host_name):
    return Path(state) / NOTES_DIR / f"{host_name}.json"


def load_notes(state, host_name):
    """{"pool/volume": {sha256, url, date, author}} ; {} si absent ou illisible."""
    try:
        doc = json.loads(notes_path(state, host_name).read_text())
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def save_note(state, host_name, pool, volume, note):
    """Écrit (ou retire, `note` None) la note d'une image : fichier 0600
    remplacé d'un coup (écriture dans un fichier temporaire puis rename)."""
    path = notes_path(state, host_name)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    notes = load_notes(state, host_name)
    key = f"{pool}/{volume}"
    if note is None:
        notes.pop(key, None)
    else:
        notes[key] = {k: v for k, v in note.items() if k in ("sha256", "url", "date", "author")}
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".notes-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(notes, f, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return notes


# --- catalogue d'images ------------------------------------------------------------

# lecture de la signature ISO 9660 (`CD001` à l'octet 32769) par libvirt, sur
# l'hôte ; `virsh -q` pour qu'aucun saut de ligne ne s'ajoute
_ISO_PROBE = ('virsh -q -c "$1" vol-download --pool "$2" --vol "$3" --file /dev/stdout '
              '--offset 32769 --length 5 2>/dev/null | od -An -c')


def _is_iso_probe(text):
    return "".join(text.split()) == "CD001"


def list_images(host, decl=None, notes=None, fresh=False):
    """Catalogue d'images d'un hôte : volumes des pools d'images déclarés
    (`decl["image_pools"]`), sinon volumes libres (cités par aucun domaine)
    de tous les pools actifs. Rend {"images": [{pool, volume, path, format,
    iso, capacity, allocation, sha256, url, date, author, used_by,
    backing_users}], "pools": [noms lus], "unknown": [lectures manquées],
    "survey_age_s"}.

    Vue seulement : le relevé vient du cache par hôte (ko.SURVEYS, rafraîchi
    en arrière-plan, oublié à chaque écriture de la console) sauf `fresh`.
    Les répertoires (volumes de format `dir`, par exemple la cible d'un pool
    imbriqué) ne sont jamais listés."""
    notes = notes or {}
    wanted = list((decl or {}).get("image_pools") or [])
    if fresh:
        survey, age = ko._Survey(host), 0.0
    else:
        survey, age = ko.SURVEYS.get(host)
    users = survey.users()
    pools = [p for p in (wanted or sorted(survey.vols)) if p in survey.vols]
    images, probes = [], []
    for p in pools:
        for n, v in sorted(survey.vols[p].items()):
            if v.get("format") == "dir":
                continue
            ids = {f"{p}/{n}", ko._norm(v["path"]) if v.get("path") else None} - {None}
            used_by = sorted({u for i in ids for u in users.get(i, [])})
            if not wanted and used_by:
                continue
            fmt = v.get("format")
            backers = survey.backing_users(v["path"]) if v.get("path") else []
            note = notes.get(f"{p}/{n}") or {}
            item = {"pool": p, "volume": n, "path": v.get("path"), "format": fmt,
                    "iso": fmt == "iso", "capacity": v.get("capacity"),
                    "allocation": v.get("allocation"), "sha256": note.get("sha256"),
                    "url": note.get("url"), "date": note.get("date"),
                    "author": note.get("author"), "used_by": used_by,
                    "backing_users": backers}
            if fmt == "raw":
                probes.append(item)
            images.append(item)
    if probes:
        uri = host.decl.get("uri", "qemu:///system")
        res = ko._sh_batch(host, [["sh", "-c", _ISO_PROBE, "sh", uri, i["pool"], i["volume"]]
                                  for i in probes], idempotent=True)
        for item, (rc, text) in zip(probes, res):
            item["iso"] = (rc == 0 and _is_iso_probe(text)) or (
                rc != 0 and item["volume"].lower().endswith(".iso"))
    missing = [p for p in wanted if p not in survey.vols]
    return {"images": images, "pools": pools, "missing_pools": missing,
            "unknown": sorted(set(survey.unknown)), "survey_age_s": round(age, 1)}


# --- contrôle d'empreinte sur l'hôte ----------------------------------------------

# `virsh -q` : sans lui, un saut de ligne final fausse la somme (vu en réel)
_SHA_SCRIPT = ('command -v sha256sum >/dev/null 2>&1 || { echo nosha256sum; exit 0; }; '
               # code de sortie de virsh relu par le descripteur 3 (sh n'a pas
               # pipefail) : une lecture en échec ne rend pas la somme du vide
               '{ rc=$( { { virsh -q -c "$1" vol-download --pool "$2" --vol "$3" '
               '--file /dev/stdout; echo $? >&3; } | sha256sum >&4; } 3>&1 ); } 4>&1; '
               '[ "$rc" = 0 ]')


def remote_sha256(host, pool, volume, size=None):
    """Somme SHA-256 d'un volume calculée SUR l'hôte (libvirt lit avec ses
    droits, `sha256sum` de coreutils). None si `sha256sum` manque (le
    contrôle est alors sauté, jamais de rapatriement de l'image)."""
    uri = host.decl.get("uri", "qemu:///system")
    timeout = max(120, int((size or 0) / VERIFY_MIN_BPS) + 60)
    out = host.run(["sh", "-c", _SHA_SCRIPT, "sh", uri, pool, volume], timeout=timeout,
                   idempotent=True, fresh=True)
    if out.strip() == "nosha256sum":
        return None
    m = re.match(r"^([0-9a-f]{64})\b", out.strip())
    if not m:
        raise OpError("failed", f"unreadable checksum output from the host for {pool}/{volume}")
    return m.group(1)


def verify_image(host, pool, volume, expected=None):
    """Recalcule l'empreinte d'un volume sur l'hôte. Rend {sha256, expected,
    match (None si rien à comparer), skipped}."""
    check_pool_name(pool)
    ko.check_vm_name(volume)
    v = _volume(host, pool, volume)
    if v is None:
        raise OpError("notfound", f"{host.name}: volume {pool}/{volume} not found")
    got = remote_sha256(host, pool, volume, v.get("capacity"))
    if got is None:
        return {"sha256": None, "expected": expected, "match": None,
                "skipped": "sha256sum is not installed on the host"}
    return {"sha256": got, "expected": expected,
            "match": None if not expected else got == expected.lower(), "skipped": None}


# --- téléversement --------------------------------------------------------------

def local_format(first):
    """Format d'un fichier local d'après ses premiers octets : qcow2, iso
    (signature `CD001` à 32769) ou raw."""
    if first[:4] == b"QFI\xfb":
        return "qcow2"
    if first[32769:32774] == b"CD001":
        return "iso"
    return "raw"


def check_image_file(path, max_virtual_size=None):
    """Contrôles d'un fichier avant envoi : taille, en-tête qcow2
    (qcow2.check_upload : fichier de base, fichier de données externe,
    chiffrement, taille virtuelle au-delà de `max_virtual_size`, par défaut
    MAX_VIRTUAL_SIZE). Rend {size, format, refusals}."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        first = f.read(max(qcow2.HEADER_READ, 32774))
    limit = MAX_VIRTUAL_SIZE if max_virtual_size is None else max_virtual_size
    refusals = qcow2.check_upload(first, limit) if size else ["the file is empty"]
    return {"size": size, "format": local_format(first), "refusals": refusals}


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(4 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _delete_partial(host, pool, name):
    """Supprime un volume partiel ; rend None ou un reste nommé."""
    try:
        host.virsh("vol-delete", "--pool", pool, "--vol", name)
    except kh.KvmError as e:
        if e.kind != "notfound":
            return {"kind": "volume", "pool": pool, "volume": name, "error": e.message}
    return None


def upload_volume(host, pool, name, path, fmt_hint=None, sha256=None, verify=True,
                  progress=None, cancel=None, limit_bps=None, notes_state=None, author=None,
                  url=None, step=None, max_virtual_size=None):
    """Téléverse un fichier local comme nouveau volume du pool : nom
    contrôlé, volume absent, pool actif avec la place, en-tête qcow2
    contrôlé, `vol-create-as --format raw`, `vol-upload --file /dev/stdin`
    sur une connexion propre (jamais `--sparse` : refusé sur un tuyau),
    format relu (libvirt le sonde), empreinte recalculée sur l'hôte si
    `verify`, note écrite dans `notes_state`.

    `progress(octets)` ; `cancel()` ; `limit_bps` ; `max_virtual_size` :
    plafond de taille virtuelle d'un qcow2 (MAX_VIRTUAL_SIZE par défaut). Sur échec ou annulation
    (kh.Cancelled), le volume partiel est supprimé ; ce qui n'a pas pu
    l'être est attaché à l'exception (`exc.leftovers`). `step(id, statut,
    message)` reçoit `check`, `upload`, `verify`."""
    check_pool_name(pool)
    check_volume_name(name)
    if sha256 is not None and (not isinstance(sha256, str)
                               or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256)):
        raise OpError("invalid", "invalid SHA-256: 64 hexadecimal characters")

    def st(sid, status, msg=""):
        if step:
            step(sid, status, msg)

    st("check", "running", f"{pool}/{name}")
    info = check_image_file(path, max_virtual_size)
    if info["refusals"]:
        raise OpError("refused", "; ".join(info["refusals"]), {"refusals": info["refusals"]})
    if fmt_hint and fmt_hint not in ("qcow2", "raw", "iso"):
        raise OpError("invalid", "format must be qcow2, raw or iso")
    if fmt_hint and fmt_hint != info["format"] and not (fmt_hint == "raw"
                                                        and info["format"] == "iso"):
        raise OpError("refused", f"the file is {info['format']}, not {fmt_hint}")
    local_sha = file_sha256(path)
    if sha256 and local_sha != sha256.lower():
        raise OpError("refused", "the file does not match the expected SHA-256",
                      {"refusals": ["checksum mismatch before upload"]})
    pst = _require_active_pool(host, pool)
    if _volume(host, pool, name) is not None:
        raise OpError("refused", f"a volume named {name} already exists in {pool}")
    if pst.get("available") is not None and info["size"] > pst["available"]:
        raise OpError("refused", f"not enough space in pool {pool}: {info['size']} bytes "
                      f"needed, {pst['available']} available")
    st("check", "done", f"{info['format']}, {info['size']} bytes")
    started = time.monotonic()
    ko.invalidate_survey(host)
    host.virsh("vol-create-as", "--pool", pool, "--name", name, "--capacity",
               str(info["size"]), "--format", "raw")
    try:
        st("upload", "running", f"{info['size']} bytes")
        host.run_stream(["env", "LC_ALL=C", "virsh", "-q", "-c",
                         host.decl.get("uri", "qemu:///system"), "vol-upload", "--pool", pool,
                         "--vol", name, "--file", "/dev/stdin"], path, size=info["size"],
                        progress=progress, cancel=cancel, limit_bps=limit_bps)
        st("upload", "done", f"{info['size']} bytes")
        vol = _volume(host, pool, name)
        if vol is None:
            raise OpError("failed", f"{pool}/{name} disappeared after the upload")
        want = "qcow2" if info["format"] == "qcow2" else None
        if want and vol.get("format") != want:
            host.virsh("pool-refresh", "--pool", pool)
            vol = _volume(host, pool, name) or vol
        if want and vol.get("format") != want:
            raise OpError("failed", f"libvirt sees {pool}/{name} as {vol.get('format')}, "
                          f"not {want}")
        verified, got = None, None
        if verify:
            st("verify", "running", "SHA-256 on the host")
            got = remote_sha256(host, pool, name, info["size"])
            if got is None:
                verified = "skipped"
                st("verify", "warn", "sha256sum is not installed on the host: not verified")
            elif got != local_sha:
                raise OpError("failed", "the volume written on the host does not match the "
                              "file (SHA-256): it was deleted")
            else:
                verified = True
                st("verify", "done", got)
    except BaseException as exc:
        left = _delete_partial(host, pool, name)
        exc.leftovers = [left] if left else []
        if isinstance(exc, OpError):
            exc.data["leftovers"] = exc.leftovers
        raise
    finally:
        ko.invalidate_survey(host)
    if notes_state is not None:
        save_note(notes_state, host.name, pool, name, {
            "sha256": local_sha, "url": url, "author": author,
            "date": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")})
    return {"pool": pool, "volume": name, "path": vol.get("path"), "format": vol.get("format"),
            "capacity": vol.get("capacity"), "sha256": local_sha, "verified": verified,
            "elapsed_s": round(time.monotonic() - started, 1)}


# --- volumes ---------------------------------------------------------------------

def create_volume_argv(pool, name, capacity, fmt="qcow2", backing=None, backing_format=None,
                       prealloc_metadata=False):
    """Arguments virsh d'un volume vierge ou lié (`--backing-vol`)."""
    args = ["vol-create-as", "--pool", pool, "--name", name, "--capacity", str(capacity),
            "--format", fmt]
    if backing:
        args += ["--backing-vol", backing, "--backing-vol-format", backing_format or "qcow2"]
    if prealloc_metadata:
        args.append("--prealloc-metadata")
    return args


def create_volume(host, pool, name, capacity, fmt="qcow2", backing=None,
                  prealloc_metadata=False):
    """Volume vierge (ou lié à `backing`, une image du même pool) :
    refus nommés (nom, volume existant, pool inactif, image absente ou de
    format inconnu, place d'un volume raw). Rend parse_vol_xml du volume."""
    check_pool_name(pool)
    check_volume_name(name)
    _check_size(capacity, "capacity")
    if fmt not in VOLUME_FORMATS:
        raise OpError("invalid", "format must be qcow2 or raw")
    if backing is not None and fmt != "qcow2":
        raise OpError("invalid", "a linked volume must be qcow2")
    pst = _require_active_pool(host, pool)
    if _volume(host, pool, name) is not None:
        raise OpError("refused", f"a volume named {name} already exists in {pool}")
    bfmt = None
    if backing is not None:
        ko.check_vm_name(backing)
        img = _volume(host, pool, backing)
        if img is None:
            raise OpError("refused", f"image {backing} not found in pool {pool}")
        if img.get("format") not in VOLUME_FORMATS:
            raise OpError("refused", f"image {backing} is {img.get('format')}: only qcow2 and "
                          "raw images can back a disk")
        bfmt = img["format"]
        capacity = max(capacity, img.get("capacity") or 0)
    if fmt == "raw" and pst.get("available") is not None and capacity > pst["available"]:
        raise OpError("refused", f"not enough space in pool {pool}: {capacity} bytes needed, "
                      f"{pst['available']} available")
    with ko.writing(host):
        host.virsh(*create_volume_argv(pool, name, capacity, fmt, backing, bfmt,
                                       prealloc_metadata))
    return _volume(host, pool, name)


def _stat_more(survey, paths):
    """Identité `stat -L` de chemins que le relevé n'a pas encore lus
    (cibles des pools, volumes d'un pool) ; un échec laisse le chemin sans
    identité (comparé par texte seulement)."""
    more = sorted({ko._norm(p) for p in paths if ko._is_path(p)} - set(survey.inode)
                  - survey.absent)
    if not more:
        return
    res = ko._sh_batch(survey.host, [["stat", "-L", "-c", "%d:%i", "--", p] for p in more],
                       timeout=survey.timeout, idempotent=True)
    for p, (rc, text) in zip(more, res):
        if rc == 0 and re.fullmatch(r"\d+:\d+", (text or "").strip()):
            survey.inode[p] = text.strip()


def _volume_survey(host, pool, name, timeout=None):
    """(volume, relevé) pour une décision d'écriture sur un volume : volume
    lu d'abord (son chemin), puis relevé NEUF de l'hôte avec ce chemin en
    `extra` (identité périphérique:inode et système de fichiers lus), et
    identité des cibles de tous les pools (répertoire d'un pool imbriqué)."""
    v = _volume(host, pool, name)
    if v is None:
        pool_state(host, pool)                  # pool absent : notfound plus clair
        raise OpError("notfound", f"{host.name}: volume {pool}/{name} not found")
    path = v.get("path")
    survey = ko._Survey(host, extra=[path] if ko._is_path(path) else [], timeout=timeout)
    again = survey.vols.get(pool, {}).get(name)
    if again is None:
        if pool not in survey.vols:
            raise OpError("notfound", f"{host.name}: pool {pool} not found or not active")
        raise OpError("notfound", f"{host.name}: volume {pool}/{name} not found")
    if ko._norm(again.get("path") or "") != ko._norm(path or ""):
        raise OpError("plan-changed", f"{pool}/{name} changed while it was read: try again")
    _stat_more(survey, [i.get("target_path") for i in survey.pools.values()])
    return again, survey


def _pool_target_of(survey, path):
    """Nom du pool dont `path` est la cible (texte ou identité), ou None."""
    keys = survey.keys([path])
    for p, info in sorted(survey.pools.items()):
        t = info.get("target_path")
        if t and survey.keys([t]) & keys:
            return p
    return None


def _volume_proof(survey, pool, name, v, ack, running_only=False):
    """Preuve qu'aucun domaine ne se sert d'un volume, comme `_owned` de la
    1.98 (kvm_ops) : `sharers` (domaines qui citent le volume, comparés par
    texte ET par périphérique:inode, donc aussi sous un lien), `backers`
    (chaînes de sauvegarde qui passent par lui), stockage partagé entre
    hôtes (REASON_SHARED_STORAGE : un autre hôte peut s'en servir), système
    de fichiers illisible, lecture manquée (seul un fichier illisible pour
    refus de droits est acceptable, `ack`). `running_only` : seuls les
    domaines en marche comptent parmi les `sharers` (redimensionnement).
    Rend (refus, {"sharers", "backers"})."""
    path = v.get("path")
    refusals = []
    if v.get("format") == "dir":
        owner = _pool_target_of(survey, path) if path else None
        refusals.append(f"the directory of pool {owner}" if owner else REASON_DIRECTORY)
    ids = {f"{pool}/{name}"} | ({ko._norm(path)} if path else set())
    keys = survey.keys(ids)
    sharers = survey.sharers(keys)
    if running_only:
        sharers = [d for d in sharers if survey.domains.get(d, {}).get("active")]
        if sharers:
            refusals.append(f"used by a running VM ({', '.join(sharers)}): shut it down first")
    elif sharers:
        refusals.append(f"used by {', '.join(sharers)}")
    backers = [b for b in survey.backers(keys) if b not in ids
               and ko._norm(b) != ko._norm(path or "")]
    if backers:
        refusals.append(f"backing file of {backers[0]}")
    if ko._shared_storage(survey, {"pool": pool, "path": path}):
        refusals.append(f"{ko.REASON_SHARED_STORAGE} (shared storage: another host may use it)")
    elif path and ko._norm(path) not in survey.absent and \
            survey.fstype.get(ko._norm(path), "") is None:
        refusals.append(f"{REASON_UNPROVEN} (file system of the volume unreadable)")
    if survey.blocked(ack):
        left = sorted(p for p in survey.unreadable if p not in ack)
        refusals.append(REASON_UNPROVEN + (f" (files the SSH user cannot read: {', '.join(left)})"
                                           if left else ""))
    return refusals, {"sharers": sharers, "backers": backers}


def resize_volume(host, pool, name, capacity, acknowledged_unreadable=None):
    """Agrandit un volume (`vol-resize`) ; refus d'une réduction, d'un
    volume cité par un domaine en marche (décision 66 : redimensionner
    seulement VM arrêtée), d'une image de base, d'un stockage partagé, et
    quand rien n'est prouvé (même preuve que la suppression)."""
    check_pool_name(pool)
    ko.check_vm_name(name)
    _check_size(capacity, "capacity")
    ack = ko.check_acknowledged(acknowledged_unreadable)
    v = _volume(host, pool, name)
    if v is None:
        raise OpError("notfound", f"{host.name}: volume {pool}/{name} not found")
    if capacity <= (v.get("capacity") or 0):
        raise OpError("refused", f"the new size must be larger than {v.get('capacity')} bytes "
                      "(shrinking is not offered)")
    v, survey = _volume_survey(host, pool, name)
    refusals, _ = _volume_proof(survey, pool, name, v, ack, running_only=True)
    if refusals:
        raise OpError("refused", "; ".join(refusals), {"refusals": refusals,
                                                       "unreadable": sorted(survey.unreadable)})
    with ko.writing(host):
        host.virsh("vol-resize", "--pool", pool, "--vol", name, "--capacity", str(capacity))
    return _volume(host, pool, name)


def volume_digest(plan):
    """Empreinte d'un plan de suppression de volume : pool, nom, clé libvirt,
    chemin et identité périphérique:inode. Un volume recréé sous le même nom
    entre l'aperçu et l'exécution n'a plus la même empreinte."""
    doc = json.dumps({k: plan.get(k) for k in ("pool", "volume", "key", "path", "identity")},
                     sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(doc.encode()).hexdigest()


def delete_volume_plan(host, pool, name, acknowledged_unreadable=None, survey_timeout=None):
    """Plan de suppression d'un volume, relevé NEUF (jamais le cache des
    vues) : {pool, volume, key, path, identity, refusals, unreadable,
    acknowledged_unreadable, digest}. Refus (`_volume_proof`) : cité par un
    domaine (texte ou identité), image de base d'un autre volume ou d'un
    disque, stockage partagé, répertoire (cible d'un pool imbriqué), lecture
    manquée. `acknowledged_unreadable` : fichiers illisibles que l'exploitant
    accepte de considérer comme ne citant pas ce volume (décision 46, comme
    la suppression d'une VM)."""
    check_pool_name(pool)
    ko.check_vm_name(name)
    ack = ko.check_acknowledged(acknowledged_unreadable)
    v, survey = _volume_survey(host, pool, name, timeout=survey_timeout)
    refusals, _ = _volume_proof(survey, pool, name, v, ack)
    path = v.get("path")
    unreadable = sorted(survey.unreadable)
    plan = {"pool": pool, "volume": name, "key": v.get("key"), "path": path,
            "identity": survey.inode.get(ko._norm(path)) if path else None,
            "refusals": refusals, "unreadable": unreadable,
            "acknowledged_unreadable": sorted(ack & set(unreadable))}
    plan["digest"] = volume_digest(plan)
    return plan


def delete_volume(host, pool, name, notes_state=None, acknowledged_unreadable=None,
                  expect_digest=None, progress=None, survey_timeout=None):
    """Supprime un volume après un plan recalculé ici (jamais l'aperçu).
    `expect_digest` : `digest` de l'aperçu montré ; s'il ne correspond plus
    (volume recréé, déplacé), OpError `plan-changed` et rien n'est fait.
    `progress(étape, statut, message)` reçoit `plan` et `delete` (action
    suivie de la console)."""
    ko.check_digest(expect_digest)

    def step(sid, status, msg=""):
        if progress:
            progress(sid, status, msg)

    step("plan", "running", f"{pool}/{name}")
    plan = delete_volume_plan(host, pool, name, acknowledged_unreadable, survey_timeout)
    if plan["refusals"]:
        step("plan", "failed", "; ".join(plan["refusals"]))
        raise OpError("refused", "; ".join(plan["refusals"]), {"refusals": plan["refusals"],
                                                                "plan": plan})
    if expect_digest is not None and expect_digest != plan["digest"]:
        msg = f"{pool}/{name} changed since the preview: nothing was done; preview again"
        step("plan", "failed", msg)
        raise OpError("plan-changed", msg, {"refusals": [], "plan": plan})
    step("plan", "done", "nothing uses it")
    step("delete", "running", f"{pool}/{name}")
    with ko.writing(host):
        host.virsh("vol-delete", "--pool", pool, "--vol", name)
    if notes_state is not None:
        save_note(notes_state, host.name, pool, name, None)
    step("delete", "done", f"{pool}/{name}")
    return {**plan, "deleted": True}


# --- pools ------------------------------------------------------------------------

def _norm_abs(path, what):
    if not isinstance(path, str) or not path.startswith("/") or any(
            ord(c) < 32 or ord(c) == 127 for c in path) or "'" in path or '"' in path:
        raise OpError("invalid", f"{what} must be an absolute path")
    norm = posixpath.normpath(path)
    if norm.startswith("//"):
        norm = "/" + norm.lstrip("/")
    if ".." in path.split("/"):
        raise OpError("invalid", f"{what} must not contain '..'")
    return norm


def _ancestors(path):
    """Le chemin puis chacun de ses parents jusqu'à « / »."""
    out, p = [], posixpath.normpath(path)
    while True:
        out.append(p)
        if p in ("/", "//", "."):
            return out
        p = posixpath.dirname(p)


def path_ids(host, paths):
    """{chemin: « périphérique:inode »} (`stat -L`, liens suivis) de chaque
    chemin et de chacun de ses parents, en un appel ; un chemin absent ou
    illisible n'a pas d'identité (comparé par texte seulement)."""
    todo = sorted({a for p in paths if isinstance(p, str) and p.startswith("/")
                   for a in _ancestors(p)})
    if not todo:
        return {}
    res = ko._sh_batch(host, [["stat", "-L", "-c", "%d:%i", "--", p] for p in todo],
                       idempotent=True)
    return {p: text.strip() for p, (rc, text) in zip(todo, res)
            if rc == 0 and re.fullmatch(r"\d+:\d+", (text or "").strip())}


def path_relation(a, b, ids=None):
    """Relation de deux répertoires : `same`, `inside` (a sous b), `contains`
    (b sous a) ou None. Par texte, et par identité quand `ids` (path_ids) la
    donne : un lien vers le répertoire d'un pool est le même répertoire."""
    ids = ids or {}
    a, b = posixpath.normpath(a), posixpath.normpath(b)
    ia, ib = ids.get(a), ids.get(b)
    if a == b or (ia and ia == ib):
        return "same"
    if _under(a, b) or (ib and ib in {ids.get(x) for x in _ancestors(a)[1:]}):
        return "inside"
    if _under(b, a) or (ia and ia in {ids.get(x) for x in _ancestors(b)[1:]}):
        return "contains"
    return None


def check_pool_spec(spec, pools, ids=None):
    """Raisons de refus d'un pool à créer. `spec` : {name, type (dir|netfs),
    path, source_host, source_dir, autostart} ; `pools` : list_pools() ;
    `ids` : path_ids() de la cible, des cibles des pools et des répertoires
    système (sans lui, comparaison par texte seulement)."""
    reasons = []
    if not isinstance(spec, dict):
        return ["the pool spec must be an object"]
    try:
        check_pool_name(spec.get("name"), new=True)
    except OpError as e:
        reasons.append(e.message)
    if spec.get("type") not in CREATABLE_POOL_TYPES:
        reasons.append("only dir and NFS (netfs) pools can be created by the console")
    try:
        path = _norm_abs(spec.get("path"), "the target path")
    except OpError as e:
        reasons.append(e.message)
        path = None
    if path:
        for bad in FORBIDDEN_POOL_PATHS:
            rel = path_relation(path, bad, ids)
            if rel == "same" or (rel and bad != "/"):
                reasons.append(f"the target path {path} is or contains a system directory "
                               f"({bad})")
                break
        for p in pools:
            other = p.get("target_path")
            rel = path_relation(path, other, ids) if other else None
            if p.get("name") == spec.get("name"):
                reasons.append(f"a pool named {p['name']} already exists")
            elif rel == "same":
                reasons.append(f"the target path is the directory of pool {p['name']}: a file "
                               "seen by two pools would break the ownership checks")
            elif rel and not (spec.get("type") == "dir" and p.get("type") == "dir"):
                where = "inside" if rel == "inside" else "around"
                reasons.append(f"the target path is {where} pool {p['name']} ({other}, "
                               f"{p.get('type')}): only two dir pools may be nested")
    if spec.get("type") == "netfs":
        h = spec.get("source_host")
        try:
            ipaddress.ip_address(h)
        except (ValueError, TypeError):
            if not isinstance(h, str) or not _HOSTNAME_RE.match(h):
                reasons.append("NFS: invalid server name or address")
        try:
            _norm_abs(spec.get("source_dir"), "NFS: the exported directory")
        except OpError as e:
            reasons.append(e.message)
    if "autostart" in spec and not isinstance(spec["autostart"], bool):
        reasons.append("autostart must be true or false")
    return reasons


def nested_pools(spec, pools, ids=None):
    """Pools `dir` dont la cible contient celle du nouveau pool ou y est
    contenue (accepté entre deux pools `dir`, dit dans le plan)."""
    path = posixpath.normpath(spec["path"])
    return sorted(p["name"] for p in pools if p.get("target_path")
                  and path_relation(path, p["target_path"], ids) in ("inside", "contains"))


def pool_xml(spec):
    """XML d'un pool `dir` ou `netfs` (NFS) créé par la console."""
    pool = ET.Element("pool", type=spec["type"])
    ET.SubElement(pool, "name").text = spec["name"]
    if spec["type"] == "netfs":
        src = ET.SubElement(pool, "source")
        ET.SubElement(src, "host", name=spec["source_host"])
        ET.SubElement(src, "dir", path=_norm_abs(spec["source_dir"], "dir"))
        ET.SubElement(src, "format", type="nfs")
    target = ET.SubElement(pool, "target")
    ET.SubElement(target, "path").text = _norm_abs(spec["path"], "path")
    ET.indent(pool, space="  ")
    return ET.tostring(pool, encoding="unicode") + "\n"


def define_pool(host, spec, step=None, dry_run=False):
    """Crée un pool sur demande d'un administrateur : contrôles, `pool-define
    --validate --file /dev/stdin`, `pool-build`, `pool-start`, autostart si
    demandé. Retour arrière au mieux sur échec (le répertoire n'est retiré
    que s'il a été créé par ce geste et reste vide). `dry_run` : contrôles
    et XML seulement."""
    def st(sid, status, msg=""):
        if step:
            step(sid, status, msg)

    st("plan", "running", str(spec.get("name") if isinstance(spec, dict) else "?"))
    pools = list_pools(host)
    reasons = check_pool_spec(spec, pools)
    ids = {}
    if not reasons:
        ids = path_ids(host, [spec["path"], *FORBIDDEN_POOL_PATHS,
                              *(p["target_path"] for p in pools if p.get("target_path"))])
        reasons = check_pool_spec(spec, pools, ids)
    if reasons:
        st("plan", "failed", "; ".join(reasons))
        raise OpError("refused", "; ".join(reasons), {"refusals": reasons})
    path = _norm_abs(spec["path"], "path")
    existed = ko._sh_batch(host, [["test", "-e", path]], idempotent=True)[0][0] == 0
    xml = pool_xml(spec)
    nested = nested_pools(spec, pools, ids)
    st("plan", "done", f"{spec['type']} pool at {path}"
       + (" (existing directory)" if existed else "")
       + (f" (nested with pool {', '.join(nested)})" if nested else ""))
    if dry_run:
        return {"name": spec["name"], "dry_run": True, "xml": xml, "existing_directory": existed,
                "nested_with": nested}
    name = spec["name"]
    done = []
    ko.invalidate_survey(host)
    try:
        st("define", "running", name)
        virsh_input(host, ("pool-define", "--validate", "--file", "/dev/stdin"), xml)
        done.append("define")
        host.virsh("pool-build", "--pool", name)
        done.append("build")
        host.virsh("pool-start", "--pool", name)
        done.append("start")
        if spec.get("autostart"):
            host.virsh("pool-autostart", "--pool", name)
        st("define", "done", name)
    except BaseException as exc:
        leftovers = []
        for verb in reversed(done):
            try:
                if verb == "start":
                    host.virsh("pool-destroy", "--pool", name)
                elif verb == "build" and not existed:
                    host.virsh("pool-delete", "--pool", name)
                elif verb == "define":
                    host.virsh("pool-undefine", "--pool", name)
            except kh.KvmError as e:
                leftovers.append({"kind": "pool", "name": name, "step": verb, "error": e.message})
        exc.leftovers = leftovers
        if isinstance(exc, OpError):
            exc.data["leftovers"] = leftovers
        raise
    finally:
        ko.invalidate_survey(host)
    return {"name": name, "dry_run": False, "state": pool_state(host, name),
            "existing_directory": existed, "nested_with": nested}


def start_pool(host, name):
    check_pool_name(name)
    with ko.writing(host):
        host.virsh("pool-start", "--pool", name)
    return pool_state(host, name)


def refresh_pool(host, name):
    check_pool_name(name)
    with ko.writing(host):
        host.virsh("pool-refresh", "--pool", name)
    return pool_state(host, name)


def autostart_pool(host, name, enable=True):
    check_pool_name(name)
    args = ["pool-autostart", "--pool", name] + ([] if enable else ["--disable"])
    with ko.writing(host):
        host.virsh(*args)
    return pool_state(host, name)


def stop_pool(host, name):
    """Arrête un pool (`pool-destroy` : ne détruit aucune donnée) ; refus si
    un domaine en marche en utilise un volume ou un fichier."""
    check_pool_name(name)
    st = pool_state(host, name)
    doms = read_domains(host)
    users = pool_users(doms, name, st.get("target_path"), live_only=True)
    if users:
        raise OpError("refused", f"used by a running VM ({', '.join(users)})",
                      {"refusals": [f"used by {u}" for u in users]})
    if doms["unknown"]:
        raise OpError("refused", f"{REASON_UNPROVEN}: unreadable VM definition(s) "
                      f"{', '.join(doms['unknown'])}")
    with ko.writing(host):
        host.virsh("pool-destroy", "--pool", name)
    return pool_state(host, name)


def delete_pool_plan(host, name, remove_dir=False, busy=(), acknowledged_unreadable=None,
                     survey_timeout=None):
    """Plan de suppression d'un pool, relevé neuf : refus si une définition
    de domaine (active ou inactive) cite un de ses volumes ou un chemin sous
    sa cible (texte, ou identité périphérique:inode : un lien compte), si la
    chaîne de sauvegarde d'un domaine ou d'un volume d'un autre pool passe
    par un fichier du pool, si une opération de la console y écrit (`busy` :
    libellés), ou si une lecture a manqué (un fichier illisible pour refus de
    droits peut être accepté, `acknowledged_unreadable`, décision 46).
    `remove_dir` : `pool-delete` seulement si le pool est vide."""
    check_pool_name(name)
    ack = ko.check_acknowledged(acknowledged_unreadable)
    st = pool_state(host, name)
    target = st.get("target_path")
    survey = ko._Survey(host, extra=[target] if ko._is_path(target) else [],
                        timeout=survey_timeout)
    vols = survey.vols.get(name, {})
    vol_paths = [v["path"] for v in vols.values() if ko._is_path(v.get("path"))]
    _stat_more(survey, vol_paths)
    pool_keys = survey.keys(vol_paths) | {("p", f"{name}/{n}") for n in vols}

    def in_pool(p):
        return bool(ko._is_path(p) and target and _under(ko._norm(p), target)) or bool(
            survey.keys([p]) & pool_keys)

    users, chains = set(), set()
    for r in survey.refs:
        inside = r["path"] and in_pool(r["path"])
        if r["owner"] not in (None, ko._SELF):
            if inside or any(i.startswith(f"{name}/") or in_pool(i) for i in r["ids"]):
                users.add(r["owner"])
                continue
        elif inside:
            continue                    # volume libre du pool lui-même : part avec lui
        if any(in_pool(c) for c in r["chain"]):
            chains.add(r["owner"] if r["owner"] not in (None, ko._SELF) else r["top"])
    refusals = []
    if users:
        refusals.append(f"used by {', '.join(sorted(users))}")
    if chains:
        refusals.append(f"a backing chain uses a file of the pool: {', '.join(sorted(chains))}")
    if survey.blocked(ack):
        left = sorted(p for p in survey.unreadable if p not in ack)
        refusals.append(REASON_UNPROVEN + (f" (files the SSH user cannot read: {', '.join(left)})"
                                           if left else
                                           f": {', '.join(sorted(set(survey.unknown)))}"))
    if busy:
        refusals.append(f"an operation of the console writes to it: {', '.join(busy)}")
    names = None
    if remove_dir:
        if st["state"] == "running":
            names = sorted(pool_volumes(host, name))
            if names:
                refusals.append(f"the pool still holds {len(names)} volume(s): the directory is "
                                "only removed when empty")
        else:
            refusals.append("start the pool first so that the console can see it is empty")
        if st.get("type") not in ("dir", "netfs"):
            refusals.append("the console only removes the directory of dir and NFS pools")
    return {"name": name, "state": st, "remove_dir": bool(remove_dir), "volumes": names,
            "refusals": refusals, "unreadable": sorted(survey.unreadable),
            "acknowledged_unreadable": sorted(ack & survey.unreadable)}


def delete_pool(host, name, remove_dir=False, busy=(), step=None, acknowledged_unreadable=None,
                survey_timeout=None):
    """Supprime un pool après un plan recalculé : `pool-destroy` (s'il est
    actif), `pool-delete` si demandé et vide, `pool-undefine`. Jamais de
    suppression de volumes en masse."""
    def stp(sid, status, msg=""):
        if step:
            step(sid, status, msg)

    stp("plan", "running", name)
    plan = delete_pool_plan(host, name, remove_dir, busy, acknowledged_unreadable, survey_timeout)
    if plan["refusals"]:
        stp("plan", "failed", "; ".join(plan["refusals"]))
        raise OpError("refused", "; ".join(plan["refusals"]), {"refusals": plan["refusals"]})
    stp("plan", "done", "nothing uses it")
    stp("delete", "running", name)
    ko.invalidate_survey(host)
    if plan["state"]["state"] == "running":
        if not remove_dir:
            host.virsh("pool-destroy", "--pool", name)
        else:
            # pool-delete exige un pool arrêté pour dir/netfs
            host.virsh("pool-destroy", "--pool", name)
            host.virsh("pool-delete", "--pool", name)
    if plan["state"].get("persistent", True):
        host.virsh("pool-undefine", "--pool", name)
    ko.invalidate_survey(host)
    stp("delete", "done", name)
    return {"name": name, "deleted": True, "directory_removed": bool(remove_dir)}
