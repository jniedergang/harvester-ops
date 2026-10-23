"""Hôtes KVM déclarés par la console (v1.96.0).

Même règles que les clusters déclarés (cluster_decl.py) : tout vit dans le
répertoire d'état de la console, les chemins rangés sont RELATIFS à lui.

    <état>/kvm.d/<nom>.yaml     une déclaration par hôte (0600)
    <état>/ssh/<nom>_id         sa clé privée (0600)
    <état>/ssh/<nom>_known      l'empreinte épinglée de l'hôte (0600)
    <état>/ssh/cm-<nom>-<hash>  sockets ControlMaster (répertoire 0700)

La clé et l'empreinte d'un hôte sont TOUJOURS ces deux chemins dérivés de
son nom : une déclaration qui pointe ailleurs (autre hôte, actions.db, clé
de l'exploitant) est refusée, et un retrait n'efface jamais rien d'autre.

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
    """Chemin absolu d'un chemin rangé dans une déclaration : relatif, sous
    le répertoire d'état, sans jamais en sortir (`../`). Un chemin absolu est
    refusé : la console ne lit que ce qu'elle a rangé elle-même."""
    if not isinstance(path, str) or not path:
        raise DeclError("empty path")
    if os.path.isabs(path):
        raise DeclError("absolute path refused")
    base = os.path.abspath(str(state))
    full = os.path.abspath(os.path.join(base, path))
    if not full.startswith(base + os.sep):
        raise DeclError("path escapes the state directory")
    return Path(full)


# transports de libvirt acceptés ; `ext` (commande arbitraire), `libssh`,
# `libssh2` et toute requête (`?command=`, `?socket=`, `?keyfile=`) sont
# refusés : l'URI part telle quelle à virsh sur l'hôte
URI_RE = re.compile(r"^qemu\+(?:ssh|tls|tcp|unix)://[^?\s]*$")


def _valid_uri(uri):
    return isinstance(uri, str) and (uri == DEFAULT_URI or bool(URI_RE.match(uri)))


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
    for field, expected in (("ssh_key", key_rel(decl["name"])),
                            ("known_hosts", known_hosts_rel(decl["name"]))):
        if decl.get(field) != expected:
            return f"{field} must be {expected}"
    if not _valid_uri(decl.get("uri", DEFAULT_URI)):
        return "uri must be qemu:///system or qemu+ssh|tls|tcp|unix://... without a query"
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
    """Écrit la déclaration (0600, répertoire 0700). DeclError si elle est
    invalide (dont des chemins de clé ou d'empreinte autres que ceux dérivés
    du nom)."""
    doc = normalize(decl)
    why = check_decl(doc)
    if why:
        raise DeclError(why)
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


def _check_known_line(line):
    """Ligne known_hosts épinglable : `hôtes type clé`, sur une seule ligne,
    sans marqueur `@cert-authority` / `@revoked` (qui changerait son sens)."""
    if not isinstance(line, str):
        raise DeclError("known_hosts line must be a string")
    if line.endswith("\n"):
        line = line[:-1]
    if "\n" in line or "\r" in line:
        raise DeclError("known_hosts line must not contain a line break")
    if line.lstrip().startswith("@"):
        raise DeclError("known_hosts markers (@cert-authority, @revoked) are refused")
    if len(line.split()) < 3:
        raise DeclError("known_hosts line must read: hosts keytype key")
    return line


def write_known_hosts(state, name, lines):
    """Range la ou les lignes d'empreinte épinglées (0600) ; rend le chemin
    relatif. TypeError pour une chaîne, DeclError pour une ligne invalide :
    rien n'est écrit dans les deux cas."""
    decl_path(state, name)
    if isinstance(lines, (str, bytes)):
        # une chaîne serait lue caractère par caractère, une ligne chacun
        raise TypeError("lines must be a list of known_hosts lines, not a string")
    text = "".join(_check_known_line(line) + "\n" for line in lines)
    write_private(Path(state) / known_hosts_rel(name), text)
    return known_hosts_rel(name)


def remove(state, name):
    """Supprime la déclaration, puis la clé et l'empreinte de l'hôte : les
    deux chemins dérivés de son nom, jamais un chemin lu dans le fichier (une
    déclaration abîmée ou forgée ne fait rien effacer d'autre). Un fichier
    déjà absent est toléré. Rend True si la déclaration existait.

    L'appelant ferme d'abord la connexion ControlMaster
    (kvm_host.close_master) : elle survivrait sinon au retrait."""
    path = decl_path(state, name)
    existed = path.is_file()
    # la déclaration d'abord : un retrait interrompu ne laisse jamais une
    # déclaration qui pointe vers une clé effacée
    path.unlink(missing_ok=True)
    for rel in (key_rel(name), known_hosts_rel(name)):
        (Path(state) / rel).unlink(missing_ok=True)
    return existed


def scan_host_key(address, port=DEFAULT_PORT, runner=subprocess.run, timeout=15):
    """Clés publiques de l'hôte et leurs empreintes, à montrer à l'exploitant
    avant de les épingler : [{"type", "line", "fingerprint"}]. `line` est la
    ligne known_hosts telle que ssh la cherchera ([adresse]:port hors 22).
    DeclError pour une adresse ou un port refusé ; kvm_host.KvmError
    (`timeout`, `failed`) quand le relevé échoue."""
    if not isinstance(address, str) or not ADDRESS_RE.match(address):
        raise DeclError("invalid address")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise DeclError("port must be between 1 and 65535")
    from kvm_host import KvmError           # import tardif : kvm_host importe ce module
    try:
        res = runner(["ssh-keyscan", "-T", "10", "-p", str(port), "--", address],
                     stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise KvmError("timeout", f"{address}: timeout: host key scan timed out",
                       host=address) from e
    except OSError as e:
        raise KvmError("failed", f"{address}: failed: cannot run ssh-keyscan ({e.strerror})",
                       host=address) from e
    out = []
    for line in (res.stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            fp = runner(["ssh-keygen", "-l", "-E", "sha256", "-f", "-"], input=line + "\n",
                        capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise KvmError("timeout", f"{address}: timeout: fingerprint computation timed out",
                           host=address) from e
        except OSError as e:
            raise KvmError("failed", f"{address}: failed: cannot run ssh-keygen ({e.strerror})",
                           host=address) from e
        m = FINGERPRINT_RE.search(fp.stdout or "")
        if fp.returncode != 0 or not m:
            continue
        out.append({"type": parts[1], "line": line, "fingerprint": m.group(0)})
    if not out:
        raise KvmError("failed", f"{address}: failed: no host key received on port {port}",
                       host=address)
    return out
