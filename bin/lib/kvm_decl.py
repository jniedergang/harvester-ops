"""Hôtes KVM déclarés par la console (v1.96.0).

Même règles que les clusters déclarés (cluster_decl.py) : tout vit dans le
répertoire d'état de la console, les chemins rangés sont RELATIFS à lui.

    <état>/kvm.d/<nom>.yaml     une déclaration par hôte (0600)
    <état>/ssh/<nom>_id         sa clé privée (0600)
    <état>/ssh/<nom>_known      l'empreinte épinglée de l'hôte (0600)
    <état>/ssh/cm-<hash>        sockets ControlMaster (répertoire 0700)

Le fichier est écrit en JSON, sous-ensemble de YAML : le module reste dans la
bibliothèque standard, et un fichier corrigé à la main en YAML se relit quand
PyYAML est présent (console), comme kube._read_yaml.

L'empreinte de l'hôte est relevée par `scan_host_key` puis confirmée par
l'exploitant : on ne fait jamais confiance à la première connexion
(pas d'`accept-new` pour un hôte qui porte des VMs clientes).
"""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

DECL_DIR = "kvm.d"
SSH_DIR = "ssh"
DEFAULT_PORT = 22
DEFAULT_URI = "qemu:///system"
# même règle que cluster_decl : un nom qui commence par une lettre ou un
# chiffre ne remonte jamais d'un répertoire
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,60}$")
# nom DNS, IPv4 ou IPv6 ; jamais de tiret en tête (ssh le lirait comme option)
ADDRESS_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9._:-]{0,252}$")
USER_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9._-]{0,63}$")
LABEL_KEY_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,62}$")
FINGERPRINT_RE = re.compile(r"\bSHA256:[A-Za-z0-9+/=]+")


class DeclError(ValueError):
    """Déclaration refusée (nom, champ ou chemin invalide)."""


def decl_dir(state):
    return Path(state) / DECL_DIR


def ssh_dir(state):
    return Path(state) / SSH_DIR


def decl_path(state, name):
    """Fichier de déclaration d'un hôte ; DeclError pour un nom refusé."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise DeclError("invalid host name")
    return decl_dir(state) / f"{name}.yaml"


def key_rel(name):
    return f"{SSH_DIR}/{name}_id"


def known_hosts_rel(name):
    return f"{SSH_DIR}/{name}_known"


def resolve(state, path):
    """Chemin absolu d'un chemin rangé dans une déclaration. Relatif : sous
    le répertoire d'état, sans jamais en sortir (`../`) ; absolu (donné par
    l'exploitant) : tel quel."""
    if not isinstance(path, str) or not path:
        raise DeclError("empty path")
    if os.path.isabs(path):
        return Path(path)
    base = os.path.abspath(str(state))
    full = os.path.abspath(os.path.join(base, path))
    if not full.startswith(base + os.sep):
        raise DeclError("path escapes the state directory")
    return Path(full)


def relativize(state, path):
    """Chemin situé sous le répertoire d'état rendu relatif à lui ; un autre
    chemin absolu reste tel quel."""
    if not isinstance(path, str) or not os.path.isabs(path):
        return path
    base = os.path.abspath(str(state))
    ap = os.path.abspath(path)
    if not ap.startswith(base + os.sep):
        return path
    return Path(os.path.relpath(ap, base)).as_posix()


def _valid_uri(uri):
    # qemu:///system, ou un transport explicite (qemu+ssh://, qemu+tls://...)
    return uri == DEFAULT_URI or (isinstance(uri, str) and re.match(r"^qemu\+[a-z]+://\S*$", uri))


def check_decl(decl):
    """Raison du refus d'une déclaration, None si elle est bonne."""
    if not isinstance(decl, dict):
        return "not a mapping"
    if not isinstance(decl.get("name"), str) or not NAME_RE.match(decl["name"]):
        return "invalid or missing name"
    addr = decl.get("address")
    if not isinstance(addr, str) or not addr.strip():
        return "empty address"
    if not ADDRESS_RE.match(addr):
        return "invalid address"
    port = decl.get("port", DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return "port must be between 1 and 65535"
    if not isinstance(decl.get("user"), str) or not USER_RE.match(decl["user"]):
        return "invalid or missing user"
    for field in ("ssh_key", "known_hosts"):
        if not isinstance(decl.get(field), str) or not decl[field]:
            return f"missing {field}"
    if not _valid_uri(decl.get("uri", DEFAULT_URI)):
        return "uri must be qemu:///system or qemu+<transport>://..."
    labels = decl.get("labels", {})
    if not isinstance(labels, dict) or any(
            not isinstance(k, str) or not LABEL_KEY_RE.match(k) or not isinstance(v, str)
            for k, v in labels.items()):
        return "labels must map names to strings"
    return None


def normalize(decl):
    """Valeurs par défaut posées (port, URI, étiquettes) ; ne valide pas."""
    out = dict(decl)
    out.setdefault("port", DEFAULT_PORT)
    out.setdefault("uri", DEFAULT_URI)
    out.setdefault("labels", {})
    return out


def _read(path):
    text = Path(path).read_text()
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        import yaml  # présent dans la console, absent du strict minimum
        return yaml.safe_load(text)
    except Exception:                      # noqa: BLE001
        return None


def load(state, name):
    """Déclaration d'un hôte, chemins tels que rangés (relatifs).
    FileNotFoundError si elle n'existe pas, DeclError si elle est invalide."""
    path = decl_path(state, name)
    doc = _read(path)
    why = check_decl(doc)
    if not why and doc["name"] != name:
        why = "name does not match the file name"
    if why:
        raise DeclError(f"host declaration {path.name}: {why}")
    return normalize(doc)


def load_all(state, warn=None):
    """Déclarations valides, triées par nom ; un fichier illisible ou invalide
    est ignoré (signalé à `warn`), jamais fatal."""
    warn = warn or (lambda msg: None)
    try:
        files = sorted(p for p in decl_dir(state).glob("*.yaml") if p.is_file())
    except OSError:
        return []
    out = []
    for p in files:
        try:
            out.append(load(state, p.stem))
        except (DeclError, OSError) as e:
            warn(f"host declaration {p.name} ignored: {e}")
    return out


def _ensure_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def ensure_ssh_dir(state):
    """Répertoire des clés et des sockets ControlMaster, 0700."""
    return _ensure_dir(ssh_dir(state))


def write_private(path, data):
    """Écriture atomique d'un fichier 0600 (fichier temporaire du même
    répertoire, fsync, rename) : un lecteur voit l'ancien ou le nouveau."""
    path = Path(path)
    _ensure_dir(path.parent)
    if isinstance(data, str):
        data = data.encode()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def write(state, decl):
    """Écrit la déclaration (0600, répertoire 0700), chemins rendus relatifs
    au répertoire d'état. DeclError si elle est invalide."""
    doc = normalize(decl)
    for field in ("ssh_key", "known_hosts"):
        doc[field] = relativize(state, doc.get(field))
    why = check_decl(doc)
    if why:
        raise DeclError(why)
    for field in ("ssh_key", "known_hosts"):
        resolve(state, doc[field])          # refuse un chemin qui sort de l'état
    path = decl_path(state, doc["name"])
    write_private(path, json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return path


def write_key(state, name, data):
    """Range la clé privée de l'hôte (0600) ; rend son chemin relatif."""
    decl_path(state, name)
    if isinstance(data, bytes):
        data = data.decode()
    if not data.endswith("\n"):              # ssh refuse une clé sans fin de ligne
        data += "\n"
    write_private(Path(state) / key_rel(name), data)
    return key_rel(name)


def write_known_hosts(state, name, lines):
    """Range la ou les lignes d'empreinte épinglées (0600) ; rend le chemin relatif."""
    decl_path(state, name)
    if isinstance(lines, (str, bytes)):
        # une chaîne serait lue caractère par caractère, une ligne chacun
        raise TypeError("lines must be a list of known_hosts lines, not a string")
    text = "".join(line.rstrip("\n") + "\n" for line in lines)
    write_private(Path(state) / known_hosts_rel(name), text)
    return known_hosts_rel(name)


def remove(state, name):
    """Supprime la déclaration, la clé et l'empreinte de l'hôte. Seuls les
    fichiers situés sous le répertoire d'état sont effacés : une clé donnée
    ailleurs par l'exploitant reste à lui. Rend True si la déclaration
    existait."""
    path = decl_path(state, name)
    targets = {key_rel(name), key_rel(name) + ".pub", known_hosts_rel(name)}
    try:
        doc = _read(path)
    except OSError:
        doc = None
    if isinstance(doc, dict):
        for field in ("ssh_key", "known_hosts"):
            if isinstance(doc.get(field), str) and not os.path.isabs(doc[field]):
                targets.add(doc[field])
    existed = path.is_file()
    for rel in sorted(targets):
        try:
            resolve(state, rel).unlink()
        except (DeclError, FileNotFoundError):
            pass
    if existed:
        path.unlink()
    return existed


def scan_host_key(address, port=DEFAULT_PORT, runner=subprocess.run, timeout=15):
    """Clés publiques de l'hôte et leurs empreintes, à montrer à l'exploitant
    avant de les épingler : [{"type", "line", "fingerprint"}]. `line` est la
    ligne known_hosts telle que ssh la cherchera ([adresse]:port hors 22)."""
    if not isinstance(address, str) or not ADDRESS_RE.match(address):
        raise DeclError("invalid address")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise DeclError("port must be between 1 and 65535")
    try:
        res = runner(["ssh-keyscan", "-T", "10", "-p", str(port), "--", address],
                     capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise DeclError(f"{address}: host key scan timed out") from e
    out = []
    for line in (res.stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        fp = runner(["ssh-keygen", "-l", "-E", "sha256", "-f", "-"], input=line + "\n",
                    capture_output=True, text=True, timeout=timeout)
        m = FINGERPRINT_RE.search(fp.stdout or "")
        if fp.returncode != 0 or not m:
            continue
        out.append({"type": parts[1], "line": line, "fingerprint": m.group(0)})
    if not out:
        raise DeclError(f"{address}: no host key received on port {port}")
    return out
