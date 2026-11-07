"""harvester-ops : paquet d'artefacts signé (v2.6.0).

Un `tar.gz` au format du paquet de mise à jour (décision 121), signé par une
clé du client : index (`artefacts.yaml`), charts, images en disposition OCI,
manifestes, et `SHA256SUMS` en premier membre pour une extraction vérifiée
en flux. Plan : docs/design/2026-11-05-artefacts-registre-260-plan.md.

Confiance permissive par défaut (précision de 121, point 184) : un paquet
non signé ou signé par une clé inconnue est accepté après un acquittement
explicite égal à l'état constaté ; une signature fausse ou une empreinte
fausse est toujours refusée ; le mode strict refuse tout ce qui n'est pas
signé par une clé de confiance.

Bibliothèque standard seulement (chargé par `harvester-artefacts pack` sur
une machine quelconque) ; `ssh-keygen` est le seul outil externe, pour la
signature.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tarfile
import time
from pathlib import Path

import self_update as su

SIG_NAMESPACE = "harvester-artefacts"
NAME_RE = re.compile(r"^[a-z](?:[a-z0-9-]{0,38}[a-z0-9])?$")
VERSION_RE = re.compile(r"^(?:\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?|\d{4}\.(?:0[1-9]|1[0-2])(?:\.(?:0[1-9]|[12]\d|3[01]))?(?:-\d+)?)$")
ARCHIVE_RE = re.compile(r"^(?P<name>[a-z](?:[a-z0-9-]{0,38}[a-z0-9])?)-(?P<version>\d[0-9A-Za-z.+-]*)\.tar\.gz$")
ITEM_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
KINDS = ("chart", "manifest", "policy", "images")
LATER_KINDS = ("gitops",)
# Sortes livrées par ClusterResourceSet (2.7.0, bin/lib/capi_manifests.py)
MANIFEST_KINDS = ("manifest", "policy")
STATES = ("valid", "unknown-key", "unsigned", "invalid", "sums-mismatch")
ACKNOWLEDGEABLE = ("unknown-key", "unsigned")
SUMS = "SHA256SUMS"
INDEX = "artefacts.yaml"
KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa")
RSA_MIN_BITS = 3072
SIGNER_OPTIONS = ("namespaces", "valid-after", "valid-before")
_FPR_RE = re.compile(r"\b(SHA256:[A-Za-z0-9+/]{43})\b")


class ArtefactError(Exception):
    """Refus explicable ; `code` le nomme pour l'interface et le journal."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------
_TOP_KEYS = {"name", "version", "description", "items"}
_ITEM_KEYS = {"name", "kind", "version", "description", "chart", "values", "files", "images"}


def check_index(doc, image_names=None):
    """(index, erreurs) de `artefacts.yaml`. `image_names` : les noms
    (`io.containerd.image.name`) de images/index.json ; une image citée par
    un élément doit y être. La sorte `gitops` est gardée et marquée `later`
    (montrée « à venir », jamais appliquée) ; `policy` est un manifeste
    depuis la 2.7.0."""
    errors = []
    if not isinstance(doc, dict):
        return None, ["the index is not an object"]
    for k in sorted(set(doc) - _TOP_KEYS):
        errors.append(f"unknown key: {k}")
    name, version = str(doc.get("name") or ""), str(doc.get("version") or "")
    if not NAME_RE.match(name):
        errors.append("name: RFC 1123, a letter first, 40 characters at most")
    if not VERSION_RE.match(version):
        errors.append("version: x.y.z or YYYY.MM[.DD][-n]")
    if not isinstance(doc.get("description", ""), str):
        errors.append("description: text")
    items = doc.get("items")
    if not isinstance(items, list):
        errors.append("items: a list")
        items = []
    seen, out = set(), []
    known = set(image_names) if image_names is not None else None
    for i, it in enumerate(items):
        where = f"items[{i}]"
        if not isinstance(it, dict):
            errors.append(f"{where}: not an object")
            continue
        for k in sorted(set(it) - _ITEM_KEYS):
            errors.append(f"{where}: unknown key: {k}")
        iname, kind = str(it.get("name") or ""), it.get("kind")
        if not ITEM_RE.match(iname):
            errors.append(f"{where}.name: RFC 1123")
        elif iname in seen:
            errors.append(f"{where}.name: {iname} appears twice")
        seen.add(iname)
        if kind not in KINDS + LATER_KINDS:
            errors.append(f"{where}.kind: one of {', '.join(KINDS + LATER_KINDS)}")
        if kind == "chart":
            chart = str(it.get("chart") or "")
            if not re.match(r"^charts/[A-Za-z0-9._-]+\.tgz$", chart):
                errors.append(f"{where}.chart: charts/<chart>-<version>.tgz")
            if not isinstance(it.get("values", {}), dict):
                errors.append(f"{where}.values: an object")
        elif "chart" in it or "values" in it:
            errors.append(f"{where}: chart and values are for a chart")
        if kind in MANIFEST_KINDS or (kind in LATER_KINDS and "files" in it):
            files = it.get("files")
            if not isinstance(files, list) or not files or not all(
                    isinstance(f, str) and re.match(rf"^manifests/{re.escape(iname)}/[A-Za-z0-9._-]+\.ya?ml$", f)
                    for f in files):
                errors.append(f"{where}.files: manifests/{iname}/<file>.yaml, one at least")
        elif "files" in it:
            errors.append(f"{where}: files are for a manifest")
        images = it.get("images", [])
        if not isinstance(images, list) or not all(isinstance(x, str) for x in images):
            errors.append(f"{where}.images: a list of image references")
            images = []
        if kind == "images" and not images:
            errors.append(f"{where}.images: one image at least")
        for ref in images:
            if known is not None and ref not in known:
                errors.append(f"{where}.images: {ref} is not in images/index.json")
        entry = {k: it[k] for k in _ITEM_KEYS if k in it}
        if kind in LATER_KINDS:
            entry["later"] = True
        out.append(entry)
    if errors:
        return None, errors
    return {"name": name, "version": version, "description": doc.get("description", ""), "items": out}, []


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------
def parse_sums(text):
    """{chemin relatif: empreinte} du format de sha256sum."""
    out = {}
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        m = re.match(r"^([0-9a-f]{64}) [ *](.+)$", line)
        if not m or m.group(2) in out or not su._safe_member(m.group(2)):
            raise ArtefactError("sums-mismatch", f"{SUMS}: line {n} is not '<sha256>  <path>'")
        out[m.group(2)] = m.group(1)
    return out


def _split(m):
    parts = Path(m.name).parts
    return parts[0] if parts else "", "/".join(parts[1:])


SUMS_MAX = 1 << 22               # 4 Mio, des dizaines de milliers de fichiers


def _sums_size_ok(m):
    """Revue 2.6.0 (M6) : une liste tronquée faisait échouer l'import plus
    loin, sans dire pourquoi ; au-delà de SUMS_MAX, refus nommé."""
    if m.size > SUMS_MAX:
        raise ArtefactError("sums-too-large", f"{SUMS} is larger than 4 MiB ({m.size} bytes)")


def inspect_package(path):
    """Contrôle d'un paquet sans l'extraire : nom de fichier, membres sûrs
    (mêmes règles que la mise à jour), une seule racine `<nom>-<version>`
    égale à celle de l'index et du nom de fichier, SHA256SUMS en premier
    fichier, index valide. Rend {"name", "version", "top", "index",
    "files", "size"}."""
    fm = ARCHIVE_RE.match(Path(path).name)
    if not fm:
        raise ArtefactError("not-a-package", f"{Path(path).name}: expected <name>-<version>.tar.gz")
    tops, first, sums, index, files, size, names = set(), None, None, None, 0, 0, []
    try:
        with tarfile.open(path, "r|gz") as tf:
            for m in tf:
                why = su.unsafe_member(m)
                if why:
                    raise ArtefactError("unsafe-member", why)
                if m.issym() or m.islnk():
                    raise ArtefactError("unsafe-member", f"link in the package: {m.name}")
                top, rest = _split(m)
                tops.add(top)
                if len(tops) > 1:
                    raise ArtefactError("two-roots", "the package must have one top directory")
                if not m.isfile():
                    continue
                files += 1
                size += m.size
                names.append(rest)
                if first is None:
                    first = rest
                    if rest != SUMS:
                        raise ArtefactError("sums-not-first", f"{SUMS} must be the first file of the package")
                    _sums_size_ok(m)
                    sums = parse_sums(tf.extractfile(m).read(SUMS_MAX).decode("utf-8", "replace"))
                elif rest == INDEX:
                    if m.size > 1 << 22:
                        raise ArtefactError("index-invalid", f"{INDEX} is too large")
                    try:
                        index = json.loads(tf.extractfile(m).read())
                    except ValueError:
                        raise ArtefactError("index-invalid", f"{INDEX} is not JSON") from None
    except (OSError, tarfile.TarError, EOFError) as e:
        raise ArtefactError("not-a-package", f"not a readable .tar.gz archive ({e})") from None
    if sums is None:
        raise ArtefactError("sums-not-first", f"no {SUMS} in the package")
    if index is None:
        raise ArtefactError("index-invalid", f"no {INDEX} in the package")
    idx, errors = check_index(index)
    if errors:
        raise ArtefactError("index-invalid", f"{INDEX}: " + "; ".join(errors[:10]))
    top = tops.pop()
    want = f"{idx['name']}-{idx['version']}"
    if top != want or f"{fm.group('name')}-{fm.group('version')}" != want:
        raise ArtefactError("root-mismatch", f"the package's directory ({top}), its file name and its index "
                                             f"({want}) do not match")
    return {"name": idx["name"], "version": idx["version"], "top": top, "index": idx, "files": files, "size": size}


def extract_verified(path, dest, on_bytes=None):
    """Extraction en flux dans `dest` (qui ne doit pas exister) : SHA256SUMS
    lu d'abord, chaque fichier haché pendant l'écriture ; un fichier absent
    de la liste, une empreinte fausse ou un fichier listé absent =
    ArtefactError `sums-mismatch`, et `dest` est supprimé. Répertoires 0700,
    fichiers 0600, sans propriétaires ni droits de l'archive."""
    dest = Path(dest)
    if dest.exists():
        raise ArtefactError("exists", f"{dest.name} already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.mkdir(mode=0o700)
    try:
        sums, seen, total, top = None, set(), 0, None
        with tarfile.open(path, "r|gz") as tf:
            for m in tf:
                why = su.unsafe_member(m)
                if why or m.issym() or m.islnk():
                    raise ArtefactError("unsafe-member", why or f"link in the package: {m.name}")
                t, rest = _split(m)
                if top is None:
                    top = t
                elif t != top:
                    raise ArtefactError("two-roots", "the package must have one top directory")
                if m.isdir():
                    if rest:
                        (dest / rest).mkdir(mode=0o700, parents=True, exist_ok=True)
                    continue
                if not m.isfile():
                    raise ArtefactError("unsafe-member", f"special file in the package: {m.name}")
                f = tf.extractfile(m)
                if sums is None:
                    if rest != SUMS:
                        raise ArtefactError("sums-not-first", f"{SUMS} must be the first file of the package")
                    _sums_size_ok(m)
                    data = f.read(SUMS_MAX)
                    sums = parse_sums(data.decode("utf-8", "replace"))
                    out = dest / SUMS
                    out.write_bytes(data)
                    out.chmod(0o600)
                    continue
                if rest not in sums or rest in seen:
                    raise ArtefactError("sums-mismatch", f"{rest} is not listed in {SUMS}")
                out = dest / rest
                out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                h = hashlib.sha256()
                fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as w:
                    for b in iter(lambda: f.read(1 << 20), b""):
                        h.update(b)
                        w.write(b)
                        total += len(b)
                        if on_bytes:
                            on_bytes(len(b))
                if h.hexdigest() != sums[rest]:
                    raise ArtefactError("sums-mismatch", f"{rest} does not match {SUMS}")
                seen.add(rest)
        if sums is None:
            raise ArtefactError("sums-not-first", f"no {SUMS} in the package")
        missing = sorted(set(sums) - seen)
        if missing:
            raise ArtefactError("sums-mismatch", f"listed in {SUMS} but absent: {', '.join(missing[:5])}")
    except (OSError, tarfile.TarError, EOFError) as e:
        shutil.rmtree(dest, ignore_errors=True)
        raise ArtefactError("not-a-package", f"not a readable .tar.gz archive ({e})") from None
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return {"files": len(seen), "bytes": total}


# ---------------------------------------------------------------------------
# Confiance
# ---------------------------------------------------------------------------
def _fingerprint(text):
    m = _FPR_RE.search(text or "")
    return m.group(1) if m else None


def verify_trust(path, sig, signers, runner=subprocess.run):
    """État de confiance d'un paquet : {"state", "principal",
    "key_fingerprint"}, jamais la sortie brute de ssh-keygen.

    - pas de `.sig` : `unsigned` ;
    - clé trouvée dans `signers` (find-principals) et signature juste pour
      ce principal et l'espace de noms des artefacts : `valid` ;
    - clé absente des clés de confiance mais signature juste
      (check-novalidate) : `unknown-key` ;
    - tout le reste (signature d'un autre fichier, autre espace de noms,
      `.sig` illisible) : `invalid`."""
    out = {"state": "unsigned", "principal": None, "key_fingerprint": None}
    if not sig or not Path(sig).exists():
        return out
    out["state"] = "invalid"
    try:
        if Path(sig).stat().st_size == 0:
            return out
    except OSError:
        return out
    principals = []
    if signers and Path(signers).is_file() and Path(signers).stat().st_size:
        try:
            r = runner(["ssh-keygen", "-Y", "find-principals", "-s", str(sig), "-f", str(signers)],
                       capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            raise ArtefactError("no-ssh-keygen", "ssh-keygen is needed to check signatures") from None
        if su._no_y(r):
            raise ArtefactError("no-ssh-keygen", "ssh-keygen 8.2 or newer is needed (`ssh-keygen -Y`)")
        if r.returncode == 0:
            principals = [p for p in (r.stdout or "").split() if p]
    for p in principals:
        try:
            su.verify_signature(path, sig, signers, runner=runner, namespace=SIG_NAMESPACE, identity=p)
        except su.UpdateError:
            continue
        out.update(state="valid", principal=p, key_fingerprint=_novalidate(path, sig, runner)[1])
        return out
    if principals:
        return out                       # clé connue, signature fausse
    ok, fpr = _novalidate(path, sig, runner)
    if ok:
        out.update(state="unknown-key", key_fingerprint=fpr)
    return out


def _novalidate(path, sig, runner):
    """(signature juste pour l'espace de noms des artefacts, empreinte de clé)."""
    try:
        with open(path, "rb") as data:
            r = runner(["ssh-keygen", "-Y", "check-novalidate", "-n", SIG_NAMESPACE, "-s", str(sig)],
                       stdin=data, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        raise ArtefactError("no-ssh-keygen", "ssh-keygen is needed to check signatures") from None
    if su._no_y(r):
        raise ArtefactError("no-ssh-keygen", "ssh-keygen 8.2 or newer is needed (`ssh-keygen -Y`)")
    return r.returncode == 0, _fingerprint((r.stdout or "") + (r.stderr or ""))


def decide(trust, strict, acknowledge=None):
    """(accepté, code) d'après le tableau des contraintes communes du plan.
    L'acquittement doit être égal à l'état constaté, jamais deviné."""
    state = trust.get("state") if isinstance(trust, dict) else trust
    if state not in STATES:
        return False, "invalid"
    if acknowledge not in (None, "") + ACKNOWLEDGEABLE:
        return False, "acknowledge-invalid"
    if state == "valid":
        return True, "valid"
    if state in ("invalid", "sums-mismatch"):
        return False, state
    if strict:
        return False, f"strict-refuses-{state}"
    if acknowledge == state:
        return True, f"accepted-{state}"
    return False, f"acknowledge-{state}"


def journal_line(trust, name, version, person):
    """La ligne du journal de l'action pour un paquet accepté."""
    state = trust.get("state") if isinstance(trust, dict) else trust
    if state == "valid":
        return f"accepted package {name}-{version} signed by {trust.get('principal')} ({trust.get('key_fingerprint')})"
    what = {"unsigned": "UNSIGNED", "unknown-key": "UNKNOWN-KEY"}.get(state, state.upper())
    return f"accepted {what} package {name}-{version} after acknowledgement by {person}"


def write_trust(pkg_dir, trust, person, strict, now=time.time):
    """trust.json du paquet rangé : état, clé, personne, date, mode."""
    data = {"state": trust["state"], "principal": trust.get("principal"),
            "key_fingerprint": trust.get("key_fingerprint"), "person": person,
            "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now())),
            "mode": "strict" if strict else "permissive"}
    su.write_json_atomic(Path(pkg_dir) / "trust.json", data, mode=0o600)
    return data


# ---------------------------------------------------------------------------
# Réglages et clés de confiance
# ---------------------------------------------------------------------------
def _dir(state_dir):
    return Path(state_dir) / "artefacts"


def signers_path(state_dir):
    return _dir(state_dir) / "signers"


def _key_info(ktype, b64):
    """(empreinte SHA256, bits) d'une clé publique OpenSSH, après contrôle
    que le blob est bien du type annoncé."""
    try:
        blob = base64.b64decode(b64, validate=True)
    except ValueError:
        raise ValueError("the key is not base64") from None

    def take(b, i):
        if i + 4 > len(b):
            raise ValueError("truncated key")
        n = struct.unpack(">I", b[i:i + 4])[0]
        if i + 4 + n > len(b):
            raise ValueError("truncated key")
        return b[i + 4:i + 4 + n], i + 4 + n
    t, i = take(blob, 0)
    if t.decode(errors="replace") != ktype:
        raise ValueError(f"the key data is not of type {ktype}")
    bits = 256
    if ktype == "ssh-rsa":
        _, i = take(blob, i)
        n, _ = take(blob, i)
        bits = int.from_bytes(n, "big").bit_length()
    elif ktype == "ssh-ed25519":
        k, _ = take(blob, i)
        if len(k) != 32:
            raise ValueError("not an ed25519 key")
    fpr = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return fpr, bits


def parse_signer_line(line):
    """Une ligne allowed_signers vérifiée : {"principal", "options",
    "key_type", "key", "comment", "fingerprint", "line"} ; l'option
    `namespaces="harvester-artefacts"` est ajoutée si elle manque. Lève
    ValueError avec la raison."""
    tokens = line.split()
    try:
        ti = next(i for i, t in enumerate(tokens) if t in KEY_TYPES)
    except StopIteration:
        raise ValueError("no supported key type (ssh-ed25519, ecdsa-sha2-*, ssh-rsa)") from None
    if ti not in (1, 2) or ti + 1 >= len(tokens):
        raise ValueError("expected: <principal> [options] <key type> <key> [comment]")
    principal, ktype, key = tokens[0], tokens[ti], tokens[ti + 1]
    if not re.match(r"^[A-Za-z0-9@._*,+-]{1,128}$", principal):
        raise ValueError("principal: letters, digits and @ . _ - + * , only")
    opts = {}
    if ti == 2:
        for o in re.findall(r'([a-z-]+)(?:="([^"]*)")?(?:,|$)', tokens[1]):
            if o[0] not in SIGNER_OPTIONS:
                raise ValueError(f"option {o[0]!r} is not accepted")
            opts[o[0]] = o[1]
    ns = [n.strip() for n in opts.get("namespaces", SIG_NAMESPACE).split(",")]
    if SIG_NAMESPACE not in ns:
        raise ValueError(f"the key is restricted to other namespaces ({','.join(ns)})")
    opts["namespaces"] = ",".join(ns)
    fpr, bits = _key_info(ktype, key)
    if ktype == "ssh-rsa" and bits < RSA_MIN_BITS:
        raise ValueError(f"RSA key of {bits} bits, {RSA_MIN_BITS} at least")
    comment = " ".join(tokens[ti + 2:])
    opt_text = ",".join(f'{k}="{v}"' for k, v in opts.items())
    return {"principal": principal, "options": opts, "key_type": ktype, "key": key, "comment": comment,
            "fingerprint": fpr, "line": " ".join(x for x in (principal, opt_text, ktype, key, comment) if x)}


def parse_signers(text):
    """(clés, erreurs) d'un texte allowed_signers ; lignes vides et
    commentaires ignorés."""
    keys, errors = [], []
    for n, line in enumerate((text or "").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            keys.append(parse_signer_line(line.strip()))
        except ValueError as e:
            errors.append(f"line {n}: {e}")
    return keys, errors


def save_signers(state_dir, text):
    """Écrit les clés de confiance (0600) si chaque ligne est valable ;
    rend les clés, sans leur matière."""
    keys, errors = parse_signers(text)
    if errors:
        raise ArtefactError("signers-invalid", "; ".join(errors))
    d = _dir(state_dir)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    p = signers_path(state_dir)
    tmp = p.with_name(".signers.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("".join(k["line"] + "\n" for k in keys))
    os.replace(tmp, p)
    return [_public(k) for k in keys]


def _public(k):
    return {"principal": k["principal"], "key_type": k["key_type"], "fingerprint": k["fingerprint"],
            "comment": k["comment"]}


def settings(state_dir, config=None):
    """{"strict", "locked", "signers"} : mode strict de Paramètres >
    Artefacts, verrouillé si config.yaml porte `artefacts: {strict: true}`."""
    locked = bool(((config or {}).get("artefacts") or {}).get("strict") is True)
    saved = su.read_json(_dir(state_dir) / "settings.json", {}) or {}
    try:
        keys, _ = parse_signers(signers_path(state_dir).read_text())
    except OSError:
        keys = []
    return {"strict": locked or saved.get("strict") is True, "locked": locked,
            "signers": [_public(k) for k in keys]}


def save_settings(state_dir, strict, config=None):
    if settings(state_dir, config)["locked"] and not strict:
        raise ArtefactError("strict-locked", "strict mode is locked by the console configuration")
    _dir(state_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
    su.write_json_atomic(_dir(state_dir) / "settings.json", {"strict": bool(strict)}, mode=0o600)
    return settings(state_dir, config)


# ---------------------------------------------------------------------------
# Paquets rangés
# ---------------------------------------------------------------------------
def packages_dir(state_dir):
    return Path(state_dir) / "artefacts.d"


def package_dir(state_dir, name, version):
    if not NAME_RE.match(name or "") or not VERSION_RE.match(version or ""):
        raise ArtefactError("not-found", "no such package")
    return packages_dir(state_dir) / f"{name}-{version}"


def load_package(state_dir, name, version):
    d = package_dir(state_dir, name, version)
    idx = su.read_json(d / INDEX)
    if not isinstance(idx, dict):
        raise ArtefactError("not-found", f"no package {name}-{version}")
    trust = su.read_json(d / "trust.json", {}) or {}
    return {"name": name, "version": version, "path": str(d), "index": idx, "trust": trust}


def list_packages(state_dir):
    out = []
    root = packages_dir(state_dir)
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        idx = su.read_json(d / INDEX)
        if not d.is_dir() or not isinstance(idx, dict) or f"{idx.get('name')}-{idx.get('version')}" != d.name:
            continue
        trust = su.read_json(d / "trust.json", {}) or {}
        out.append({"name": idx["name"], "version": idx["version"], "description": idx.get("description", ""),
                    "items": len(idx.get("items") or []), "trust": trust.get("state"),
                    "principal": trust.get("principal"), "imported_by": trust.get("person"),
                    "imported_at": trust.get("date")})
    return out


def remove_package(state_dir, name, version, used_by=()):
    """Supprime un paquet rangé ; refusé (`package-in-use`) si un plan
    enregistré le désigne (`used_by` : noms des plans, calculés par
    l'appelant)."""
    used_by = sorted(set(used_by or ()))
    if used_by:
        raise ArtefactError("package-in-use", f"{name}-{version} is used by plan(s): {', '.join(used_by)}")
    d = package_dir(state_dir, name, version)
    if not d.is_dir():
        raise ArtefactError("not-found", f"no package {name}-{version}")
    shutil.rmtree(d)
    return True
