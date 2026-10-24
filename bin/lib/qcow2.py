"""Lecture de l'en-tête d'une image qcow2 avant son envoi sur un hôte KVM
(prochaine version KVM, tâche 1).

Une image téléversée ou téléchargée peut déclarer un fichier de base ou un
fichier de données externe : QEMU irait alors lire un fichier de l'hôte
choisi par l'auteur de l'image. La console lit les premiers octets
(`HEADER_READ`) et refuse ces images avant d'écrire quoi que ce soit.

Format : docs/interop/qcow2.txt de QEMU (en-tête gros-boutiste, version 2
ou 3, extensions d'en-tête après `header_length`).
"""

import struct

MAGIC = b"QFI\xfb"
HEADER_READ = 65536               # assez pour l'en-tête et ses extensions
V2_HEADER = 72
V3_MIN_HEADER = 104

# bits de incompatible_features
INCOMPAT_DIRTY = 1 << 0
INCOMPAT_CORRUPT = 1 << 1
INCOMPAT_DATA_FILE = 1 << 2
INCOMPAT_COMPRESSION = 1 << 3
INCOMPAT_EXTL2 = 1 << 4
_KNOWN_INCOMPAT = (INCOMPAT_DIRTY | INCOMPAT_CORRUPT | INCOMPAT_DATA_FILE
                   | INCOMPAT_COMPRESSION | INCOMPAT_EXTL2)
AUTOCLEAR_RAW_DATA = 1 << 1

# extensions d'en-tête
EXT_END = 0x00000000
EXT_BACKING_FORMAT = 0xE2792ACA
EXT_DATA_FILE = 0x44415441        # « DATA » : nom du fichier de données externe
EXT_CRYPTO = 0x0537BE77

REASON_NOT_QCOW2 = "not a qcow2 image"
REASON_TRUNCATED = "truncated qcow2 header"
REASON_BACKING = ("the image declares a backing file: QEMU would read a host file chosen "
                  "by the image author")
REASON_DATA_FILE = ("the image declares an external data file: QEMU would read a host file "
                    "chosen by the image author")
REASON_ENCRYPTED = "encrypted image: the console cannot provide its key"
REASON_CORRUPT = "the image is marked corrupt"
REASON_UNKNOWN_FEATURES = "the image uses unknown incompatible features"
REASON_VERSION = "unsupported qcow2 version"
# taille virtuelle maximale acceptée par défaut (réglable par l'appelant) : un
# en-tête qui annonce des pétaoctets remplirait le pool à la première écriture
MAX_VIRTUAL_SIZE = 16 << 40

# autres formats qui peuvent citer un fichier de l'hôte
_OTHER_RISKY = ((b"QED\x00", "QED"), (b"KDMV", "VMDK"), (b"# Disk DescriptorFile", "VMDK"),
                (b"vhdxfile", "VHDX"), (b"conectix", "VHD"))


class Qcow2Error(ValueError):
    """En-tête illisible (pas un qcow2, tronqué)."""


def header(first_bytes):
    """En-tête d'une image qcow2 : {version, virtual_size, backing_file,
    external_data_file, encrypted, cluster_bits, incompatible_features,
    dirty, corrupt, unknown_features}. Lève Qcow2Error si ce n'est pas un
    qcow2 ou si l'en-tête est tronqué."""
    b = bytes(first_bytes or b"")
    if b[:4] != MAGIC:
        raise Qcow2Error(REASON_NOT_QCOW2)
    if len(b) < V2_HEADER:
        raise Qcow2Error(REASON_TRUNCATED)
    (version, backing_off, backing_size, cluster_bits, size, crypt,
     _l1_size, _l1_off, _rc_off, _rc_clusters, _nb_snap, _snap_off) = struct.unpack_from(
        ">IQIIQIIQQIIQ", b, 4)
    incompat = compat = autoclear = 0
    header_length = V2_HEADER
    if version == 3:
        if len(b) < V3_MIN_HEADER:
            raise Qcow2Error(REASON_TRUNCATED)
        incompat, compat, autoclear, _rc_order, header_length = struct.unpack_from(
            ">QQQII", b, 72)
        if header_length < V3_MIN_HEADER:
            raise Qcow2Error(REASON_TRUNCATED)
    elif version != 2:
        raise Qcow2Error(REASON_VERSION)
    exts = _extensions(b, header_length, backing_off)
    return {
        "version": version,
        "virtual_size": size,
        "cluster_bits": cluster_bits,
        "backing_file": backing_off != 0 or backing_size != 0 or EXT_BACKING_FORMAT in exts,
        "external_data_file": bool(incompat & INCOMPAT_DATA_FILE) or EXT_DATA_FILE in exts
        or bool(autoclear & AUTOCLEAR_RAW_DATA),
        "encrypted": crypt != 0 or EXT_CRYPTO in exts,
        "incompatible_features": incompat,
        "dirty": bool(incompat & INCOMPAT_DIRTY),
        "corrupt": bool(incompat & INCOMPAT_CORRUPT),
        "unknown_features": bool(incompat & ~_KNOWN_INCOMPAT),
    }


def _extensions(b, start, backing_off):
    """Types des extensions d'en-tête lisibles dans les octets fournis
    (arrêt à la fin, au nom du fichier de base, ou au premier bloc illisible)."""
    found = set()
    pos = start
    limit = len(b)
    if backing_off and backing_off < limit:
        limit = backing_off
    for _ in range(256):
        if pos + 8 > limit:
            break
        kind, length = struct.unpack_from(">II", b, pos)
        if kind == EXT_END:
            break
        found.add(kind)
        pos += 8 + ((length + 7) & ~7)
    return found


def check_upload(first_bytes, max_virtual_size=MAX_VIRTUAL_SIZE):
    """Raisons de refuser l'envoi d'une image (liste vide = acceptable).
    Une image qui n'est pas un qcow2 (raw, ISO) n'est pas refusée, sauf les
    formats qui peuvent eux aussi citer un fichier de l'hôte. Un qcow2 dont
    la taille virtuelle dépasse `max_virtual_size` (None : pas de plafond)
    est refusé."""
    b = bytes(first_bytes or b"")
    if b[:4] != MAGIC:
        for magic, name in _OTHER_RISKY:
            if b.startswith(magic):
                return [f"{name} image: it can cite a host file; convert it to qcow2 or raw first"]
        return []
    try:
        h = header(b)
    except Qcow2Error as exc:
        return [str(exc)]
    reasons = []
    if h["backing_file"]:
        reasons.append(REASON_BACKING)
    if h["external_data_file"]:
        reasons.append(REASON_DATA_FILE)
    if h["encrypted"]:
        reasons.append(REASON_ENCRYPTED)
    if h["corrupt"]:
        reasons.append(REASON_CORRUPT)
    if h["unknown_features"]:
        reasons.append(REASON_UNKNOWN_FEATURES)
    if max_virtual_size is not None and h["virtual_size"] > max_virtual_size:
        reasons.append(f"the image announces a virtual size of {h['virtual_size']} bytes, above "
                       f"the limit of {max_virtual_size} bytes")
    return reasons
