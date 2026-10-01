"""Profils d'installation bare-metal multi-nœuds (1.80.0).

Un profil est une configuration d'installation nommée (les champs de la
fenêtre d'installation, sans identifiants de BMC ni valeurs propres à un
nœud, et le YAML avancé) dont toute chaîne peut porter des variables
`{{nom}}`. Une série (batch) applique un profil à un tableau de machines :
une ligne par machine, une valeur par variable. La première ligne crée le
cluster, les suivantes le rejoignent.

Rangement, comme les clusters déclarés par la console (cluster_decl) :

    <état>/profiles.d/<nom>.yaml     un profil (0600), nom RFC 1123

Aucun secret n'y entre : ni jeton du cluster, ni mot de passe de l'OS, ni
mot de passe de BMC. Ils sont saisis au lancement de la série.

Partagé par la console (web/app.py) et la ligne de commande
(bin/harvester-baremetal.py profile ...). Ce module ne dépend pas du schéma
de l'installeur (web/) : la console lui passe ce qu'il lui faut.
"""

import csv
import io
import os
import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

PROFILE_DIR = "profiles.d"
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
VAR_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,31}$")
VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.-]*)\s*\}\}")
BUILTIN_VARS = ("hostname", "ip", "mgmt_mac", "vip")
# colonnes d'une ligne de série qui ne sont pas des variables
ROW_FIXED = ("bmc_host", "bmc_user", "bmc_password")
BMC_HOST_RE = re.compile(r"^[A-Za-z0-9.:\[\]-]{1,253}$")
CLUSTER_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,59}[a-z0-9])?$")
MAX_ROWS = 64
MAX_VALUE = 512
DEFAULT_CONCURRENCY = 2

# Champs de la fenêtre d'installation qu'un profil peut porter. Les autres
# sont tenus par la série (mode, server_url, vip, cluster_name), par la
# console (iso_url, power_off, own_disk_names) ou sont des secrets.
PROFILE_FIELDS = frozenset((
    "iso", "hostname", "device", "data_disk", "wipe_all_disks", "wipe_disks_list",
    "mgmt_interfaces", "mgmt_interface", "bond_mode", "bond_miimon", "bond_lacp_rate",
    "bond_xmit_hash_policy", "vlan_id", "method", "ip", "subnet_mask", "gateway",
    "vip_mode", "dns", "ntp", "labels", "modules", "ssh_keys", "pools",
    "extra_args", "skipchecks",
))
SECRET_FIELDS = frozenset(("token", "password", "bmc_password"))


class ProfileError(ValueError):
    """Refus avec ses raisons : [(où, raison)], jamais une valeur secrète."""

    def __init__(self, errors, message="invalid profile"):
        super().__init__(message)
        self.errors = list(errors)


# --- variables ---------------------------------------------------------------

def variables_in(obj):
    """Noms de variables employés dans une valeur (chaînes, listes, dicts)."""
    out = set()
    if isinstance(obj, str):
        out.update(VAR_RE.findall(obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out |= variables_in(k) | variables_in(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out |= variables_in(v)
    return out


def check_value(name, value):
    """Raison du refus d'une valeur de variable, None si elle est bonne. Une
    ligne, sans caractère de contrôle : une valeur passe dans des fichiers de
    configuration (keyfiles, sshd) où un saut de ligne en ajouterait une."""
    if not isinstance(value, str):
        return "not a string"
    if len(value) > MAX_VALUE:
        return "too long"
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return "control character or line break"
    return None


def substitute_text(text, values):
    """Remplace chaque `{{nom}}` ; KeyError(nom) pour une valeur absente."""
    def repl(m):
        name = m.group(1)
        v = values.get(name)
        if v is None or v == "":
            raise KeyError(name)
        return str(v)
    return VAR_RE.sub(repl, text)


def substitute_obj(obj, values):
    if isinstance(obj, str):
        return substitute_text(obj, values)
    if isinstance(obj, dict):
        return {substitute_obj(k, values): substitute_obj(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute_obj(v, values) for v in obj]
    return obj


def render_advanced(text, values, field_type=None):
    """YAML avancé d'un profil, variables remplacées, en dictionnaire.

    Le texte n'est PAS substitué puis relu : une valeur portant `: ` ou `#`
    casserait le YAML, et un `{{ip}}` nu se lirait comme un dictionnaire de
    flux. Chaque variable devient d'abord un jeton neutre (lettres et
    chiffres), le texte est relu, puis les jetons sont remplacés dans les
    chaînes : la valeur arrive telle quelle, y compris dans un bloc
    `content: |` d'`os.write_files`. Une chaîne qui n'était qu'une variable,
    sur un champ entier ou booléen du schéma (`field_type(chemin)`), prend
    ce type (`vlan_id: {{vlan}}`)."""
    if not text or not str(text).strip():
        return None
    tag = "hopsv" + secrets.token_hex(6)
    sentinels = {}

    def to_sentinel(m):
        name = m.group(1)
        sentinels.setdefault(name, f"{tag}n{len(sentinels)}x")
        return sentinels[name]
    neutral = VAR_RE.sub(to_sentinel, str(text))
    try:
        doc = yaml.safe_load(neutral)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f"advanced_yaml (line {mark.line + 1})" if mark else "advanced_yaml"
        raise ProfileError([(where, "invalid YAML")]) from None
    if doc is None:
        return None
    if not isinstance(doc, dict):
        raise ProfileError([("advanced_yaml", "not a mapping")])
    back = {s: n for n, s in sentinels.items()}
    sent_re = re.compile(re.escape(tag) + r"n\d+x")

    def value_of(sentinel):
        name = back[sentinel]
        v = values.get(name)
        if v is None or v == "":
            raise KeyError(name)
        return str(v)

    def walk(obj, path):
        if isinstance(obj, str):
            if obj in back and field_type:
                ftype = field_type(path)
                v = value_of(obj)
                if ftype in ("int", "uint") and re.fullmatch(r"-?\d+", v):
                    return int(v)
                if ftype == "bool" and v.lower() in ("true", "false"):
                    return v.lower() == "true"
            return sent_re.sub(lambda m: value_of(m.group(0)), obj)
        if isinstance(obj, dict):
            return {walk(k, path): walk(v, f"{path}.{k}" if path else str(k))
                    for k, v in obj.items()}
        if isinstance(obj, list):
            return [walk(v, path) for v in obj]
        return obj
    return walk(doc, "")


# --- profils -----------------------------------------------------------------

def check_profile(doc):
    """Profil normalisé {description, variables, fields, advanced_yaml}, ou
    ProfileError. Contrôle la forme et les variables ; le rendu complet
    contre le schéma de l'installeur est fait par la console."""
    if not isinstance(doc, dict):
        raise ProfileError([("profile", "not a mapping")])
    errors = []
    desc = doc.get("description") or ""
    if not isinstance(desc, str) or len(desc) > 500:
        errors.append(("description", "invalid"))
    variables = doc.get("variables") or []
    if isinstance(variables, str):
        variables = [v.strip() for v in re.split(r"[,\s]+", variables) if v.strip()]
    if not isinstance(variables, list):
        errors.append(("variables", "not a list"))
        variables = []
    custom = []
    for v in variables:
        if not isinstance(v, str) or not VAR_NAME_RE.match(v):
            errors.append((f"variables[{v}]", "invalid name"))
        elif v in BUILTIN_VARS or v in ROW_FIXED:
            errors.append((f"variables[{v}]", "reserved name"))
        elif v not in custom:
            custom.append(v)
    fields = doc.get("fields") or {}
    if not isinstance(fields, dict):
        errors.append(("fields", "not a mapping"))
        fields = {}
    for k in fields:
        if k in SECRET_FIELDS:
            errors.append((f"fields.{k}", "secret: entered when a batch starts"))
        elif k not in PROFILE_FIELDS:
            errors.append((f"fields.{k}", "not a profile field"))
    adv = doc.get("advanced_yaml") or ""
    if not isinstance(adv, str):
        errors.append(("advanced_yaml", "not a text"))
        adv = ""
    known = set(BUILTIN_VARS) | set(custom)
    for name in sorted(variables_in(fields) | variables_in(adv)):
        if name not in known:
            errors.append((f"{{{{{name}}}}}", "undeclared variable"))
    if errors:
        raise ProfileError(errors)
    return {"description": desc, "variables": custom, "fields": dict(fields),
            "advanced_yaml": adv}


def used_variables(profile):
    """Variables qu'une ligne de série doit renseigner pour ce profil."""
    return sorted(variables_in(profile.get("fields") or {})
                  | variables_in(profile.get("advanced_yaml") or ""))


def profile_path(state, name):
    if not isinstance(name, str) or not NAME_RE.match(name):
        return None
    return Path(state) / PROFILE_DIR / f"{name}.yaml"


def _private_dir(d):
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


def save_profile(state, name, doc):
    """Écrit le profil (0600, remplacement atomique) ; ProfileError."""
    path = profile_path(state, name)
    if path is None:
        raise ProfileError([("name", "invalid name (RFC 1123)")])
    prof = check_profile(doc)
    _private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump({"name": name, **prof}, f, sort_keys=False, allow_unicode=True)
    os.replace(tmp, path)
    return dict(prof, name=name)


def load_profile(state, name):
    """Profil lu et vérifié, ou None (absent, illisible, invalide)."""
    path = profile_path(state, name)
    if path is None or not path.is_file():
        return None
    try:
        doc = yaml.safe_load(path.read_text())
        prof = check_profile(doc)
    except (OSError, yaml.YAMLError, UnicodeDecodeError, ProfileError):
        return None
    if doc.get("name") not in (None, name):
        return None
    return dict(prof, name=name)


def list_profiles(state):
    d = Path(state) / PROFILE_DIR
    out = []
    try:
        files = sorted(p for p in d.glob("*.yaml") if p.is_file())
    except OSError:
        return out
    for p in files:
        prof = load_profile(state, p.stem)
        if prof is None:
            out.append({"name": p.stem, "invalid": True})
            continue
        out.append({"name": prof["name"], "description": prof["description"],
                    "variables": prof["variables"], "uses": used_variables(prof),
                    "updated": p.stat().st_mtime})
    return out


def delete_profile(state, name):
    path = profile_path(state, name)
    if path is None or not path.is_file():
        return False
    path.unlink()
    return True


# --- tableau des nœuds -------------------------------------------------------

def parse_nodes_csv(text, variables):
    """Lignes d'une série depuis un CSV collé ou lu dans un fichier. La
    première ligne nomme les colonnes : bmc_host, bmc_user, bmc_password
    (facultative), puis les variables. Séparateur `,`, `;` ou tabulation."""
    lines = [ln for ln in str(text or "").splitlines() if ln.strip()]
    if not lines:
        raise ProfileError([("csv", "empty")])
    head = lines[0]
    delim = "\t" if "\t" in head else (";" if head.count(";") > head.count(",") else ",")
    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delim)
    rows = list(reader)
    header = [h.strip() for h in rows[0]]
    allowed = set(ROW_FIXED) | set(BUILTIN_VARS) - {"vip"} | set(variables)
    errors = [(f"column {h or '(empty)'}", "unknown column") for h in header if h not in allowed]
    seen = set()
    for h in header:
        if h in seen:
            errors.append((f"column {h}", "duplicated"))
        seen.add(h)
    if "bmc_host" not in header:
        errors.append(("column bmc_host", "missing"))
    if errors:
        raise ProfileError(errors)
    out = []
    for i, cells in enumerate(rows[1:], start=2):
        if len(cells) > len(header):
            raise ProfileError([(f"line {i}", "more cells than columns")])
        cells = [c.strip() for c in cells] + [""] * (len(header) - len(cells))
        rec = dict(zip(header, cells))
        row = {k: rec.pop(k) for k in ROW_FIXED if k in rec}
        row["values"] = rec
        out.append(row)
    if len(out) > MAX_ROWS:
        raise ProfileError([("csv", f"more than {MAX_ROWS} rows")])
    return out


def check_batch(profile, batch):
    """Refus d'une série avant d'allumer quoi que ce soit : nom de cluster,
    VIP, lignes, doublons, variables absentes. [(où, raison)]."""
    errors = []
    name = str(batch.get("cluster_name") or "")
    if not CLUSTER_NAME_RE.match(name):
        errors.append(("cluster_name", "invalid (RFC 1123)"))
    vip = str(batch.get("vip") or "")
    if not vip or check_value("vip", vip):
        errors.append(("vip", "missing or invalid"))
    rows = batch.get("rows")
    if not isinstance(rows, list) or not rows:
        return errors + [("rows", "no node")]
    if len(rows) > MAX_ROWS:
        return errors + [("rows", f"more than {MAX_ROWS} nodes")]
    need = [v for v in used_variables(profile) if v != "vip"]
    seen = {"bmc_host": {}, "hostname": {}, "ip": {}, "mgmt_mac": {}}
    for i, row in enumerate(rows, start=1):
        where = f"row {i}"
        if not isinstance(row, dict):
            errors.append((where, "not a mapping"))
            continue
        host = str(row.get("bmc_host") or "")
        if not BMC_HOST_RE.match(host):
            errors.append((f"{where} bmc_host", "missing or invalid"))
        if not row.get("bmc_user"):
            errors.append((f"{where} bmc_user", "missing"))
        values = row.get("values") or {}
        if not isinstance(values, dict):
            errors.append((f"{where} values", "not a mapping"))
            continue
        for k, v in values.items():
            if k not in profile["variables"] and k not in BUILTIN_VARS:
                errors.append((f"{where} {k}", "unknown variable"))
            elif k == "vip":
                errors.append((f"{where} vip", "set once for the batch"))
            else:
                why = check_value(k, v)
                if why:
                    errors.append((f"{where} {k}", why))
        for v in need:
            if not str(values.get(v) or ""):
                errors.append((f"{where} {{{{{v}}}}}", "missing variable"))
        for key in seen:
            val = host if key == "bmc_host" else str(values.get(key) or "")
            if val:
                if val in seen[key]:
                    errors.append((f"{where} {key}", f"same as row {seen[key][val]}"))
                else:
                    seen[key][val] = i
    return errors


def node_opts(profile, batch, index, field_type=None, dump=None):
    """Options d'installation d'une ligne (corps de /api/baremetal/install,
    sans secrets) : champs du profil substitués, YAML avancé substitué,
    mode créer pour la première ligne, rejoindre ensuite. ProfileError
    pour une variable absente, avec la ligne et le nom."""
    row = batch["rows"][index]
    values = dict(row.get("values") or {}, vip=str(batch.get("vip") or ""))
    where = f"row {index + 1}"
    try:
        opts = substitute_obj(dict(profile.get("fields") or {}), values)
        adv = render_advanced(profile.get("advanced_yaml"), values, field_type)
    except KeyError as e:
        raise ProfileError([(f"{where} {{{{{e.args[0]}}}}}", "missing variable")]) from None
    except ProfileError as e:
        raise ProfileError([(f"{where} {w}", r) for w, r in e.errors]) from None
    if adv:
        opts["advanced_yaml"] = dump(adv) if dump else yaml.safe_dump(adv, sort_keys=False)
    if batch.get("iso"):
        opts["iso"] = batch["iso"]
    opts["bmc_host"] = row.get("bmc_host")
    opts["bmc_user"] = row.get("bmc_user")
    if index == 0:
        opts["mode"] = "create"
        opts["vip"] = values["vip"]
        opts["cluster_name"] = batch["cluster_name"]
    else:
        opts["mode"] = "join"
        opts["server_url"] = f"https://{values['vip']}:443"
    return opts


# --- déroulé d'une série -----------------------------------------------------

def run_batch(count, launch, wait, report, concurrency=DEFAULT_CONCURRENCY,
              cancelled=lambda: False):
    """Déroule une série de `count` nœuds. La ligne 0 crée le cluster : elle
    part seule, et les autres n'attendent qu'elle soit TERMINÉE (cluster
    déclaré) ; un échec arrête la série. Les jonctions partent ensuite dans
    l'ordre, `concurrency` à la fois.

    launch(i) -> poignée (lève une exception dont le texte est montré) ;
    wait(i, poignée) -> état final (done, error, cancelled) ;
    report(i, état, message). Rend la liste des états."""
    states = ["pending"] * count
    lock = threading.Lock()

    def one(i):
        if cancelled():
            with lock:
                states[i] = "skipped"
            report(i, "skipped", "batch cancelled")
            return
        try:
            handle = launch(i)
        except Exception as e:                       # noqa: BLE001
            with lock:
                states[i] = "error"
            report(i, "error", str(e)[:300])
            return
        report(i, "running", str(handle))
        final = wait(i, handle)
        with lock:
            states[i] = final
        report(i, final, str(handle))

    if count <= 0:
        return states
    one(0)
    if states[0] != "done":
        for i in range(1, count):
            states[i] = "skipped"
            report(i, "skipped", "the first node did not create the cluster")
        return states
    with ThreadPoolExecutor(max_workers=max(1, int(concurrency))) as ex:
        list(ex.map(one, range(1, count)))
    return states
