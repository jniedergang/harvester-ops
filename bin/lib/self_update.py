"""Mise à jour de harvester-ops (v1.82.0), partagé entre la console et l'agent
de l'hôte (bin/harvester-ops-update). Bibliothèque standard seulement : l'agent
tourne avec le python3 de l'hôte.

Conception : docs/design/2026-10-01-mise-a-jour-console.md. L'essentiel :
l'agent exécute en root l'install.sh d'une archive que la console peut fournir,
donc seule la SIGNATURE de l'archive, vérifiée avec des clés que la console ne
peut pas modifier, protège l'hôte.
"""
import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path

SIG_NAMESPACE = "harvester-ops-release"
SIG_IDENTITY = "release@harvester-ops"
ARCHIVE_RE = re.compile(r"^harvester-ops-(\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?)\.tar\.gz$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?$")
REQUIRED = ("VERSION", "install.sh", "images/harvester-ops-ui.tar")

# Fichiers d'échange dans <état>/updates (l'état est partagé avec l'hôte)
REQUEST = "request.json"
STATUS = "status.json"
STAGED = "staged"


class UpdateError(Exception):
    """Refus explicable à la personne qui demande la mise à jour."""


def parse_version(v):
    """'1.82.0' -> (1, 82, 0, 1) ; une préversion ('1.82.0-rc1') passe avant
    la version finale : (1, 82, 0, 0, 'rc1')."""
    v = (v or "").strip()
    if not VERSION_RE.match(v):
        raise UpdateError(f"not a version: {v!r}")
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[-.](.+))?$", v)
    major, minor, patch, pre = m.groups()
    base = (int(major), int(minor), int(patch))
    return base + ((0, pre) if pre else (1,))


def is_newer(candidate, current):
    try:
        return parse_version(candidate) > parse_version(current)
    except UpdateError:
        return False


def sha256_file(path, on_chunk=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            if on_chunk:
                on_chunk(len(b))
    return h.hexdigest()


def write_json_atomic(path, data, mode=0o640):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------
def _safe_member(name):
    p = Path(name)
    return not p.is_absolute() and ".." not in p.parts


def inspect_archive(path):
    """Contenu d'une archive de livraison : {"version", "top"}. Refuse ce qui
    n'en est pas une : membre absolu ou en '..', lien hors de l'arborescence,
    fichier attendu absent, VERSION illisible."""
    try:
        tf = tarfile.open(path, "r:gz")
    except (OSError, tarfile.TarError) as e:
        raise UpdateError(f"not a readable .tar.gz archive ({e})") from None
    with tf:
        tops, names, version = set(), set(), None
        for m in tf:
            if not _safe_member(m.name):
                raise UpdateError(f"unsafe path in the archive: {m.name}")
            if (m.issym() or m.islnk()) and (os.path.isabs(m.linkname) or ".." in Path(m.linkname).parts):
                raise UpdateError(f"unsafe link in the archive: {m.name} -> {m.linkname}")
            if m.isdev():
                raise UpdateError(f"device file in the archive: {m.name}")
            parts = Path(m.name).parts
            if not parts:
                continue
            tops.add(parts[0])
            rest = "/".join(parts[1:])
            names.add(rest)
            if rest == "VERSION" and m.isfile() and m.size < 100:
                version = tf.extractfile(m).read().decode("utf-8", "replace").strip()
    if len(tops) != 1:
        raise UpdateError("not a harvester-ops release: expected one top directory")
    missing = [r for r in REQUIRED if r not in names]
    if missing:
        raise UpdateError("not a harvester-ops release: missing " + ", ".join(missing))
    if not version or not VERSION_RE.match(version):
        raise UpdateError("not a harvester-ops release: no readable VERSION")
    top = tops.pop()
    if top != f"harvester-ops-{version}":
        raise UpdateError(f"the archive's directory ({top}) does not match its version ({version})")
    return {"version": version, "top": top}


def extract_archive(path, dest):
    """Extraction après `inspect_archive`, sans propriétaires ni droits
    spéciaux de l'archive."""
    with tarfile.open(path, "r:gz") as tf:
        members = []
        for m in tf:
            if not _safe_member(m.name) or m.isdev():
                raise UpdateError(f"unsafe member: {m.name}")
            m.uid = m.gid = 0
            m.uname = m.gname = "root"
            m.mode &= 0o755
            members.append(m)
        try:
            tf.extractall(dest, members=members, filter="data")
        except TypeError:          # Python sans filtre d'extraction
            tf.extractall(dest, members=members)


# ---------------------------------------------------------------------------
# Signature (OpenSSH, `ssh-keygen -Y`)
# ---------------------------------------------------------------------------
def signers_file(etc_dir="/etc/harvester-ops", opt_dir="/opt/harvester-ops"):
    """Les clés de confiance : celles de l'exploitant si présentes, sinon
    celles livrées avec la version installée."""
    for p in (Path(etc_dir) / "update-signers", Path(opt_dir) / "update-signers"):
        try:
            if p.is_file() and any(line.strip() and not line.lstrip().startswith("#")
                                   for line in p.read_text().splitlines()):
                return p
        except OSError:
            continue
    return None


# OpenSSH avant 8.2 (RHEL 8 : 8.0) ne connaît pas `ssh-keygen -Y` : il répond
# par son mode d'emploi. Vu par un contributeur (PR #3), puis 1.87.2.
_NO_Y_RE = re.compile(r"(unknown|illegal|invalid) option -- ?'?Y|usage: ssh-keygen", re.I)


def _no_y(r):
    return r is not None and r.returncode != 0 and bool(_NO_Y_RE.search((r.stderr or "") + (r.stdout or "")))


def verify_signature(archive, signature, signers, runner=subprocess.run, in_image=None):
    """True si `signature` est une signature valide de `archive` par une clé
    de `signers` (format allowed_signers d'OpenSSH). Lève UpdateError avec la
    raison sinon.

    `in_image(archive, signature, signers)` (v1.87.2) : quand le
    `ssh-keygen` de l'hôte manque ou ne connaît pas `-Y`, la même
    vérification faite par celui de l'image de la console installée (rend un
    CompletedProcess). Sans lui, l'erreur dit ce qu'il faut."""
    if not signature or not Path(signature).is_file():
        raise UpdateError("the release has no signature (.sig)")
    if not signers:
        raise UpdateError("no trusted release key on this host")
    try:
        with open(archive, "rb") as data:
            r = runner(["ssh-keygen", "-Y", "verify", "-f", str(signers), "-I", SIG_IDENTITY,
                        "-n", SIG_NAMESPACE, "-s", str(signature)],
                       stdin=data, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        r = None
    if r is None or _no_y(r):
        if in_image is None:
            raise UpdateError("this host's ssh-keygen cannot verify signatures (OpenSSH 8.2 or newer "
                              "needed, `ssh-keygen -Y`)")
        r = in_image(archive, signature, signers)
        if r is None or _no_y(r):
            raise UpdateError("neither this host's ssh-keygen nor the console image can verify "
                              "signatures (OpenSSH 8.2 or newer needed, `ssh-keygen -Y`)")
    if r.returncode != 0:
        why = (r.stderr or r.stdout or "").strip().splitlines()
        raise UpdateError("signature refused: " + (why[-1] if why else "invalid signature"))
    return True


def allow_unsigned(conf_path="/etc/harvester-ops/update.conf"):
    try:
        for line in Path(conf_path).read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "allow_unsigned" and v.strip().lower() in ("1", "true", "yes"):
                return True
    except OSError:
        pass
    return False


# ---------------------------------------------------------------------------
# Manifeste de publication (release.json)
# ---------------------------------------------------------------------------
def check_manifest(m):
    """Valide release.json et rend sa forme utile ; refuse un nom de fichier
    qui sortirait de la source."""
    if not isinstance(m, dict):
        raise UpdateError("release.json: not an object")
    version = str(m.get("version") or "")
    parse_version(version)
    archive = str(m.get("archive") or "")
    am = ARCHIVE_RE.match(archive)
    if not am or am.group(1) != version:
        raise UpdateError("release.json: the archive name does not match the version")
    sha = str(m.get("sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise UpdateError("release.json: sha256 missing or malformed")
    sig = m.get("signature")
    if sig is not None and sig != archive + ".sig":
        raise UpdateError("release.json: unexpected signature name")
    notes = m.get("notes") if isinstance(m.get("notes"), list) else []
    return {"version": version, "archive": archive, "sha256": sha, "signature": sig,
            "size": int(m.get("size") or 0), "date": str(m.get("date") or ""),
            "title": str(m.get("title") or ""), "notes": notes[:30]}


def source_url(base, name):
    """URL d'un fichier de la source (répertoire HTTP ou publications GitHub)."""
    base = (base or "").strip()
    if not re.match(r"^https?://", base):
        raise UpdateError("the update source must be an http(s) address")
    return base.rstrip("/") + "/" + name


_HEAD_RE = re.compile(r"^## \[([^\]]+)\]\s*-\s*(\S+)\s*-\s*(.*)$")


def changelog_notes(text, limit=20):
    """Les dernières versions de CHANGELOG.md : version, date, titre, et les
    lignes de chaque section (pour dire ce qu'apporte une mise à jour)."""
    out, cur, sec = [], None, None
    for line in text.splitlines():
        m = _HEAD_RE.match(line)
        if m:
            if len(out) >= limit:
                break
            cur = {"version": m.group(1), "date": m.group(2), "title": m.group(3).strip(), "sections": []}
            out.append(cur)
            sec = None
        elif cur is not None and line.startswith("### "):
            sec = {"name": line[4:].strip(), "items": []}
            cur["sections"].append(sec)
        elif sec is not None and line.startswith("- "):
            sec["items"].append(line[2:].strip())
    return out


def release_manifest(version, archive_path, changelog_text, signed):
    """Contenu de release.json, publié à côté de l'archive."""
    p = Path(archive_path)
    notes = changelog_notes(changelog_text)
    head = next((n for n in notes if n["version"] == version), None)
    return {"version": version, "archive": p.name, "sha256": sha256_file(p),
            "size": p.stat().st_size, "signature": (p.name + ".sig") if signed else None,
            "date": head["date"] if head else "", "title": head["title"] if head else "",
            "notes": notes}
