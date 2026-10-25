"""Écrivain ISO 9660 minimal, avec extension Joliet, pour l'ISO NoCloud de
cloud-init (prochaine version KVM, tâche 1).

Pourquoi ici : rien n'est installé sur l'hôte KVM (ni genisoimage, ni
xorriso, ni cloud-localds). La console fabrique l'image en Python pur et la
téléverse comme volume du pool.

Ce qui est écrit : niveau 1 (noms 8.3 de repli `USER_DAT.;1`), un seul
répertoire (la racine), un descripteur Joliet (noms UCS-2 lus tels quels :
`user-data`, `meta-data`, `network-config`), ni Rock Ridge ni El Torito.
Sortie reproductible : dates fixes (`SOURCE_DATE_EPOCH` ou paramètre).

Disposition (secteurs de 2048 octets) : 0-15 zone système vide, 16
descripteur primaire, 17 Joliet, 18 terminateur, puis tables de chemins,
répertoires racine et données des fichiers (partagées par les deux
arborescences).
"""

import datetime
import json
import os
import re
import struct

SECTOR = 2048
FIRST_DATA = 16
LABEL_MAX = 32
JOLIET_LABEL_MAX = 16
JOLIET_NAME_MAX = 64
_JOLIET_ESCAPE = b"%/E"           # UCS-2 niveau 3
PAD_SECTORS = 150
DEFAULT_EPOCH = 946684800         # 2000-01-01T00:00:00Z, date fixe par défaut
SEED_FILES = ("meta-data", "user-data", "network-config", "vendor-data")

_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                          r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
_INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class IsoError(ValueError):
    """Entrée refusée ou image illisible."""


def _both16(n):
    return struct.pack("<H", n) + struct.pack(">H", n)


def _both32(n):
    return struct.pack("<I", n) + struct.pack(">I", n)


def _pad(data, size, fill=b" "):
    if len(data) > size:
        raise IsoError("field too long")
    return data + fill * (size - len(data))


def _ucs2_field(text, size):
    raw = text.encode("utf-16-be")
    if len(raw) > size:
        raise IsoError("field too long")
    out = raw
    while len(out) + 2 <= size:
        out += b"\x00 "
    if len(out) < size:
        out += b"\x00"
    return out


def _epoch(date):
    if date is None:
        env = os.environ.get("SOURCE_DATE_EPOCH")
        if env and env.strip().isdigit():
            return int(env.strip())
        return DEFAULT_EPOCH
    if isinstance(date, datetime.datetime):
        if date.tzinfo is None:
            date = date.replace(tzinfo=datetime.timezone.utc)
        return int(date.timestamp())
    if isinstance(date, int) and not isinstance(date, bool) and date >= 0:
        return date
    raise IsoError("date: a datetime or a positive epoch")


def _dt(epoch):
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)


def _dir_date(epoch):
    d = _dt(epoch)
    return bytes([d.year - 1900, d.month, d.day, d.hour, d.minute, d.second, 0])


def _vd_date(epoch):
    if epoch is None:
        return b"0" * 16 + b"\x00"
    d = _dt(epoch)
    return d.strftime("%Y%m%d%H%M%S").encode() + b"00" + b"\x00"


def _iso_name(name, taken):
    """Nom de niveau 1 (8.3, majuscules, `_`), unique dans `taken`."""
    up = re.sub(r"[^A-Z0-9_.]", "_", name.upper())
    base, dot, ext = up.rpartition(".")
    if not dot:
        base, ext = up, ""
    base = base.replace(".", "_")[:8] or "_"
    ext = ext[:3]
    cand = f"{base}.{ext};1"
    n = 1
    while cand in taken:
        suffix = f"~{n}"
        cand = f"{base[:8 - len(suffix)]}{suffix}.{ext};1"
        n += 1
    taken.add(cand)
    return cand


def _dir_record(name_bytes, extent, size, flags, epoch):
    nl = len(name_bytes)
    length = 33 + nl + (1 if nl % 2 == 0 else 0)
    rec = bytes([length, 0]) + _both32(extent) + _both32(size) + _dir_date(epoch) + \
        bytes([flags, 0, 0]) + _both16(1) + bytes([nl]) + name_bytes
    if nl % 2 == 0:
        rec += b"\x00"
    return rec


def _directory(entries, self_extent, parent_extent, epoch):
    """Contenu d'un répertoire : `.`, `..`, puis (nom, étendue, taille)."""
    recs = [_dir_record(b"\x00", self_extent, SECTOR, 2, epoch),
            _dir_record(b"\x01", parent_extent, SECTOR, 2, epoch)]
    for name, extent, size in entries:
        recs.append(_dir_record(name, extent, size, 0, epoch))
    out = b""
    for r in recs:
        # un enregistrement ne chevauche jamais deux secteurs
        if len(out) // SECTOR != (len(out) + len(r) - 1) // SECTOR:
            out += b"\x00" * (SECTOR - len(out) % SECTOR)
        out += r
    return out


def _path_table(root_extent, big_endian):
    fmt = ">" if big_endian else "<"
    return bytes([1, 0]) + struct.pack(fmt + "I", root_extent) + struct.pack(fmt + "H", 1) + b"\x00\x00"


def _volume_descriptor(kind, label, total, pt_size, l_pt, m_pt, root_rec, epoch):
    joliet = kind == 2
    vd = bytearray(SECTOR)
    vd[0] = kind
    vd[1:6] = b"CD001"
    vd[6] = 1
    if joliet:
        vd[8:40] = _ucs2_field("", 32)
        vd[40:72] = _ucs2_field(label, 32)
        vd[88:91] = _JOLIET_ESCAPE
    else:
        vd[8:40] = _pad(b"", 32)
        vd[40:72] = _pad(label.encode("ascii"), 32)
    vd[80:88] = _both32(total)
    vd[120:124] = _both16(1)
    vd[124:128] = _both16(1)
    vd[128:132] = _both16(SECTOR)
    vd[132:140] = _both32(pt_size)
    vd[140:144] = struct.pack("<I", l_pt)
    vd[148:152] = struct.pack(">I", m_pt)
    vd[156:190] = root_rec
    for off, size in ((190, 128), (318, 128), (446, 128), (574, 128),
                      (702, 37), (739, 37), (776, 37)):
        vd[off:off + size] = _ucs2_field("", size) if joliet else _pad(b"", size)
    vd[813:830] = _vd_date(epoch)
    vd[830:847] = _vd_date(epoch)
    vd[847:864] = _vd_date(None)
    vd[864:881] = _vd_date(epoch)
    vd[881] = 1
    return bytes(vd)


def _check_files(files):
    if not isinstance(files, dict) or not files:
        raise IsoError("at least one file is needed")
    for name, data in files.items():
        if not isinstance(name, str) or not name or len(name) > JOLIET_NAME_MAX \
                or any(c in name for c in "/\\;*:?\"<>|") or name in (".", "..") \
                or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise IsoError(f"invalid file name: {name!r}"[:100])
        if not isinstance(data, (bytes, bytearray)):
            raise IsoError(f"file {name!r} must be bytes"[:100])
    if len(files) > 50:
        raise IsoError("too many files (50 at most)")


def build_iso(files, label="cidata", joliet=True, date=None):
    """Image ISO 9660 (niveau 1, Joliet si `joliet`) d'un répertoire racine
    contenant `files` ({nom: octets}). `label` : étiquette de volume
    (`cidata` pour NoCloud), lettres, chiffres et `_`. `date` : datetime ou
    epoch ; défaut SOURCE_DATE_EPOCH, sinon une date fixe."""
    _check_files(files)
    if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,%d}" % LABEL_MAX, label):
        raise IsoError("invalid volume label")
    epoch = _epoch(date)
    names = sorted(files)
    taken = set()
    iso_names = {n: _iso_name(n, taken) for n in names}
    # emplacement des structures
    sector = FIRST_DATA
    pvd_at = sector
    sector += 1
    svd_at = None
    if joliet:
        svd_at = sector
        sector += 1
    sector += 1                                   # terminateur
    l_pt, m_pt = sector, sector + 1
    sector += 2
    jl_pt = jm_pt = None
    if joliet:
        jl_pt, jm_pt = sector, sector + 1
        sector += 2
    # taille des répertoires : un secteur suffit pour 50 fichiers courts,
    # on la calcule tout de même
    def dir_len(entries):
        return len(_directory(entries, 0, 0, epoch))
    p_entries_proto = [(iso_names[n].encode("ascii"), 0, 0) for n in names]
    j_entries_proto = [(n.encode("utf-16-be"), 0, 0) for n in names]
    p_dir_sectors = -(-dir_len(p_entries_proto) // SECTOR)
    root_p = sector
    sector += p_dir_sectors
    root_j = None
    j_dir_sectors = 0
    if joliet:
        j_dir_sectors = -(-dir_len(j_entries_proto) // SECTOR)
        root_j = sector
        sector += j_dir_sectors
    extents = {}
    for n in names:
        # un fichier vide n'occupe aucun secteur (étendue 0, comme mkisofs)
        extents[n] = sector if files[n] else 0
        sector += -(-len(files[n]) // SECTOR)
    # remplissage final comme mkisofs (-pad, 150 secteurs) : certains lecteurs
    # lisent par blocs au-delà du dernier fichier (isoinfo : « Short read »)
    total = sector + PAD_SECTORS
    img = bytearray(total * SECTOR)

    def put(at, data):
        img[at * SECTOR:at * SECTOR + len(data)] = data

    p_entries = sorted(((iso_names[n].encode("ascii"), extents[n], len(files[n])) for n in names),
                       key=lambda e: e[0])
    p_dir = _directory(p_entries, root_p, root_p, epoch)
    p_root_rec = _dir_record(b"\x00", root_p, p_dir_sectors * SECTOR, 2, epoch)
    put(pvd_at, _volume_descriptor(1, label, total, 10, l_pt, m_pt, p_root_rec, epoch))
    put(l_pt, _path_table(root_p, False))
    put(m_pt, _path_table(root_p, True))
    put(root_p, p_dir)
    if joliet:
        j_entries = sorted(((n.encode("utf-16-be"), extents[n], len(files[n])) for n in names),
                           key=lambda e: e[0])
        j_dir = _directory(j_entries, root_j, root_j, epoch)
        j_root_rec = _dir_record(b"\x00", root_j, j_dir_sectors * SECTOR, 2, epoch)
        put(svd_at, _volume_descriptor(2, label[:JOLIET_LABEL_MAX], total, 10, jl_pt, jm_pt,
                                       j_root_rec, epoch))
        put(jl_pt, _path_table(root_j, False))
        put(jm_pt, _path_table(root_j, True))
        put(root_j, j_dir)
    term = bytearray(SECTOR)
    term[0] = 255
    term[1:6] = b"CD001"
    term[6] = 1
    put((svd_at or pvd_at) + 1, bytes(term))
    for n in names:
        if files[n]:
            put(extents[n], bytes(files[n]))
    return bytes(img)


# --- lecture (tests et contrôle) -------------------------------------------

def _u32(b, off):
    return struct.unpack_from("<I", b, off)[0]


def _read_dir(data, extent, size, joliet):
    out = {}
    start = extent * SECTOR
    end = start + size
    if end > len(data) or extent <= 0:
        raise IsoError("directory outside the image")
    pos = start
    while pos < end:
        length = data[pos]
        if length == 0:
            pos = (pos // SECTOR + 1) * SECTOR
            continue
        if length < 34 or pos + length > end:
            raise IsoError("malformed directory record")
        nl = data[pos + 32]
        name = data[pos + 33:pos + 33 + nl]
        flags = data[pos + 25]
        f_ext, f_size = _u32(data, pos + 2), _u32(data, pos + 10)
        if name not in (b"\x00", b"\x01") and not flags & 2:
            text = name.decode("utf-16-be") if joliet else name.decode("ascii")
            if text.endswith(";1"):
                text = text[:-2]
            if not joliet:
                text = text.rstrip(".")
            if f_size and (f_ext * SECTOR + f_size > len(data)):
                raise IsoError("file outside the image")
            out[text] = bytes(data[f_ext * SECTOR:f_ext * SECTOR + f_size]) if f_size else b""
        pos += length
    return out


def read_iso(data):
    """Lecteur minimal : {label, files: {nom Joliet: octets}, primary_files:
    {nom de niveau 1 sans ;1: octets}, joliet: bool}. `files` reprend les
    noms primaires si l'image n'a pas de Joliet."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < (FIRST_DATA + 2) * SECTOR:
        raise IsoError("too short for an ISO 9660 image")
    label, primary, jfiles = None, None, None
    sector = FIRST_DATA
    while (sector + 1) * SECTOR <= len(data):
        vd = data[sector * SECTOR:(sector + 1) * SECTOR]
        if vd[1:6] != b"CD001":
            raise IsoError("not an ISO 9660 image")
        kind = vd[0]
        if kind == 255:
            break
        root = vd[156:190]
        r_ext, r_size = _u32(root, 2), _u32(root, 10)
        if kind == 1:
            label = vd[40:72].decode("ascii", "replace").rstrip(" ")
            primary = _read_dir(data, r_ext, r_size, False)
        elif kind == 2 and vd[88:91] in (b"%/@", b"%/C", b"%/E"):
            jfiles = _read_dir(data, r_ext, r_size, True)
        sector += 1
    if primary is None:
        raise IsoError("no primary volume descriptor")
    return {"label": label, "files": jfiles if jfiles is not None else primary,
            "primary_files": primary, "joliet": jfiles is not None}


def seed_files(user_data, network_data, instance_id, hostname, vendor_data=None):
    """Fichiers de la source NoCloud : `meta-data` (instance-id,
    local-hostname, écrit en JSON, que YAML lit, pour qu'aucune valeur ne
    puisse casser le document), `user-data`, `network-config` si fourni,
    `vendor-data` si fourni. Rend {nom: octets}."""
    if not isinstance(instance_id, str) or not _INSTANCE_RE.match(instance_id):
        raise IsoError("invalid instance-id")
    if not isinstance(hostname, str) or not _HOSTNAME_RE.match(hostname):
        raise IsoError("invalid guest host name")
    files = {"meta-data": (json.dumps({"instance-id": instance_id, "local-hostname": hostname},
                                      indent=2) + "\n").encode("utf-8")}
    for name, value in (("user-data", user_data), ("network-config", network_data),
                        ("vendor-data", vendor_data)):
        if value is None:
            if name == "user-data":
                files[name] = b""
            continue
        if isinstance(value, str):
            value = value.encode("utf-8")
        if not isinstance(value, (bytes, bytearray)):
            raise IsoError(f"{name} must be text")
        files[name] = bytes(value)
    return files
