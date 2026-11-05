"""Plans de déploiement et modèles de déclaration Terraform (2.4.0).

Un plan de déploiement est nommé et réutilisable. Il s'applique à un
cluster Harvester déclaré, ou s'enchaîne après une installation bare-metal
(un profil le désigne par son champ `plan`). Il active des phases :
disponibilité et contenu Terraform en 2.4.0 ; gestion Cluster API,
clusters enfants, registre et artefacts existent déjà dans le format, mais
seulement désactivés (« coming in 2.5+ »), pour que le format ne change pas.

Un modèle de déclaration porte les ressources d'une déclaration Terraform
(format de web/tf_store.py), chaînes à variables `{{nom}}`. La phase
Terraform en fait des déclarations ordinaires nommées `<plan>-<modèle>`.

Rangement, comme les profils (bm_profiles) :

    <état>/plans.d/<nom>.yaml          un plan (0600), nom RFC 1123
    <état>/tf-templates.d/<nom>.yaml   un modèle (0600), nom RFC 1123

Aucun secret n'y entre. Un modèle perd à l'enregistrement toute valeur d'une
clé qui désigne un secret (règle de kvm_templates.scrub), sauf les clés
publiques et les noms de Secrets ; un `user_data` qui parle de mot de passe
est retiré entier. La liste de ce qui a été retiré est rendue. Ce qui ne se
retire pas sans changer le sens est refusé : le code libre d'une ressource
`raw` qui parle de mot de passe ou affecte un attribut au nom de secret, une
adresse qui porte des identifiants. Les mêmes règles sont rejouées sur les
ressources rendues (refus `secret-looking value`) : une valeur de variable
ne fait pas entrer un secret que le modèle relu n'avait pas.

Variables : le moteur des profils (bm_profiles.render_advanced, jetons
neutres, type tiré du schéma), plus les variables intégrées `cluster` et
`vip`, fournies par la console.

Partagé par la console (web/app.py) et la ligne de commande
(bin/harvester-plans.py).
"""

import fcntl
import os
import re
import sys
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

import yaml

import bm_profiles as _bmp
import hcl_guard as _hcl
import kvm_templates as _kvt

ProfileError = _bmp.ProfileError
NAME_RE = _bmp.NAME_RE

PLAN_DIR = "plans.d"
TEMPLATE_DIR = "tf-templates.d"
PHASES = ("readiness", "terraform", "capi", "clusters", "registry", "artefacts")
AVAILABLE = ("readiness", "terraform")
BUILTIN_VARS = ("cluster", "vip")
MAX_TEMPLATES = 32
TIMEOUT_MIN, TIMEOUT_MAX, TIMEOUT_DEFAULT = 1, 180, 20
MAX_DESCRIPTION = 500
MAX_DECL_NAME = 63
COMING = "coming in 2.5+"

# Noms de clés qui désignent un objet public ou le NOM d'un Secret, pas un
# secret : la clé publique d'une ressource ssh_key, les clés d'une VM
# désignées par nom, le Secret qui porte le cloud-init (décision 150 : un
# secret de VM passe par `user_data_secret_name` vers un Secret existant).
# `key` seul : la clé d'étiquette des `match_label_expressions` d'une classe
# de stockage (2.4.0), jamais une valeur secrète (`api_key` reste retiré).
_PUBLIC_KEYS = frozenset(("public_key", "ssh_keys", "key"))
# nom (ou espace de noms) d'un Secret : `user_data_secret_name`, et les
# paramètres d'une classe chiffrée `csi.storage.k8s.io/...-secret-name`
_SECRET_NAME_RE = re.compile(r"secret[_-]name(space)?$", re.I)
# cloud-init retiré entier s'il parle de mot de passe (décision 152)
_TEXT_FIELDS = frozenset(("user_data",))
_PASSWORD_TEXT_RE = re.compile(r"password|passwd|chpasswd", re.I)
# HCL brut (`raw`, champ `tf`) : jamais retiré en silence, REFUSÉ par la
# garde de hcl_guard (garde contre les accidents : secrets, fichiers de
# l'hôte, blocs qui exécutent) ; ses valeurs de variables y sont échappées.
# identifiants dans une adresse (`https://user:pass@hôte/...`)
_URL_USERINFO_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s@]+@")
# ... ou dans sa requête (`?token=...`, signature d'une adresse présignée)
_URL_QUERY_SECRET_RE = re.compile(
    r"://\S*[?&][\w.-]*(password|passwd|secret|token|credential|signature|api[_-]?key|access[_-]?key)"
    r"[\w.-]*=", re.I)
SECRET_REFUSED = "secret-looking value"

_LOCK = threading.Lock()


class PlanConflict(Exception):
    """Écriture à révision périmée : la révision courante est jointe (409)."""

    def __init__(self, current_revision):
        super().__init__("the object changed in the meantime")
        self.current_revision = current_revision


# --- chemins et écriture -----------------------------------------------------

def _path(state, sub, name):
    if not isinstance(name, str) or not NAME_RE.match(name):
        return None
    return Path(state) / sub / f"{name}.yaml"


def plan_path(state, name):
    return _path(state, PLAN_DIR, name)


def template_path(state, name):
    return _path(state, TEMPLATE_DIR, name)


@contextmanager
def _dir_lock(d):
    """Verrou d'écriture du répertoire : la console et la ligne de commande
    peuvent écrire en même temps (deux processus)."""
    _bmp._private_dir(d)
    with _LOCK:
        fd = os.open(d / ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def _write(path, doc):
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _read(path):
    try:
        doc = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _current_revision(path):
    doc = _read(path) if path.is_file() else None
    if doc is None:
        return 0 if not path.exists() else None
    rev = doc.get("revision")
    return rev if isinstance(rev, int) and not isinstance(rev, bool) and rev > 0 else None


def _save(state, sub, name, doc, revision, check):
    path = _path(state, sub, name)
    if path is None:
        raise ProfileError([("name", "invalid name (RFC 1123)")])
    if isinstance(doc, dict) and doc.get("name") not in (None, name):
        raise ProfileError([("name", "differs from the name being saved")])
    clean = check(doc)
    extra = None
    if isinstance(clean, tuple):
        clean, extra = clean
    with _dir_lock(path.parent):
        current = _current_revision(path)
        wanted = revision if isinstance(revision, int) and not isinstance(revision, bool) else None
        # absent : création (révision 0 ou rien) ; présent : la révision lue ;
        # présent mais illisible : jamais écrasé en silence (le supprimer d'abord)
        if current is None or current != (wanted or 0):
            raise PlanConflict(current)
        out = {"name": name, "revision": (current or 0) + 1, **clean}
        _write(path, out)
    return out if extra is None else (out, extra)


def _load(state, sub, name, check):
    path = _path(state, sub, name)
    if path is None or not path.is_file():
        return None
    doc = _read(path)
    if doc is None or doc.get("name") not in (None, name):
        return None
    rev = doc.get("revision")
    try:
        clean = check(doc)
    except ProfileError:
        return None
    if isinstance(clean, tuple):
        clean = clean[0]
    return {"name": name, "revision": rev if isinstance(rev, int) else 0, **clean}


def _list(state, sub, check, summary):
    d = Path(state) / sub
    try:
        files = sorted(p for p in d.glob("*.yaml") if p.is_file())
    except OSError:
        return []
    out = []
    for p in files:
        obj = _load(state, sub, p.stem, check)
        out.append({"name": p.stem, "invalid": True} if obj is None else summary(obj))
    return out


def _delete(state, sub, name):
    path = _path(state, sub, name)
    if path is None or not path.is_file():
        return False
    with _dir_lock(path.parent):
        path.unlink()
    return True


# --- variables ---------------------------------------------------------------

def _check_variables(doc, errors):
    variables = doc.get("variables") or []
    if isinstance(variables, str):
        variables = [v.strip() for v in re.split(r"[,\s]+", variables) if v.strip()]
    if not isinstance(variables, list):
        errors.append(("variables", "not a list"))
        return []
    out = []
    for v in variables:
        if not isinstance(v, str) or not _bmp.VAR_NAME_RE.match(v):
            errors.append((f"variables[{v}]", "invalid name"))
        elif v in BUILTIN_VARS:
            errors.append((f"variables[{v}]", "reserved name"))
        elif v not in out:
            out.append(v)
    return out


def _check_description(doc, errors):
    desc = doc.get("description") or ""
    if not isinstance(desc, str) or len(desc) > MAX_DESCRIPTION:
        errors.append(("description", "invalid"))
        return ""
    return desc


def declaration_name(plan, template):
    """Nom de la déclaration produite ; refusé au-delà de 63 caractères
    (pas de troncature : deux modèles ne doivent pas se rencontrer)."""
    name = f"{plan}-{template}"
    if len(name) > MAX_DECL_NAME:
        raise ProfileError([(f"templates[{template}]",
                             f"declaration name {name[:20]}... longer than {MAX_DECL_NAME} characters")])
    return name


# --- plans -------------------------------------------------------------------

def _check_terraform(ph, errors, plan_name, known_templates):
    raw = ph.get("templates")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        errors.append(("phases.terraform.templates", "not a list"))
        return []
    if len(raw) > MAX_TEMPLATES:
        errors.append(("phases.terraform.templates", f"more than {MAX_TEMPLATES} templates"))
        return []
    out, seen = [], []
    for i, entry in enumerate(raw):
        where = f"phases.terraform.templates[{i}]"
        if isinstance(entry, str):
            entry = {"template": entry}
        if not isinstance(entry, dict):
            errors.append((where, "not a mapping"))
            continue
        unknown = sorted(set(entry) - {"template", "depends_on"})
        if unknown:
            errors.append((f"{where}.{unknown[0]}", "unknown key"))
        name = entry.get("template")
        if not isinstance(name, str) or not NAME_RE.match(name):
            errors.append((f"{where}.template", "missing or invalid template name"))
            continue
        if name in seen:
            errors.append((f"{where}.template", f"{name} listed twice"))
            continue
        if known_templates is not None and name not in known_templates:
            errors.append((f"{where}.template", f"template {name} not found"))
        if plan_name:
            try:
                declaration_name(plan_name, name)
            except ProfileError as e:
                errors.extend((f"{where}.template", r) for _w, r in e.errors)
        deps = entry.get("depends_on") or []
        if isinstance(deps, str):
            deps = [deps]
        if not isinstance(deps, list):
            errors.append((f"{where}.depends_on", "not a list"))
            deps = []
        clean_deps = []
        for d in deps:
            if d == name:
                errors.append((f"{where}.depends_on", "a template does not depend on itself"))
            elif d not in seen:
                # absent du plan, ou placé après : l'ordre du plan est l'ordre d'exécution
                errors.append((f"{where}.depends_on", f"{d} is not listed before this template"))
            elif d not in clean_deps:
                clean_deps.append(d)
        seen.append(name)
        item = {"template": name}
        if clean_deps:
            item["depends_on"] = clean_deps
        out.append(item)
    return out


def check_plan(doc, known_templates=None):
    """Plan normalisé {description, variables, phases}, ou ProfileError
    [(où, raison)]. `known_templates` (noms) : refuse un modèle absent ;
    sans lui, l'existence des modèles est contrôlée au lancement."""
    if not isinstance(doc, dict):
        raise ProfileError([("plan", "not a mapping")], "invalid plan")
    errors = []
    name = doc.get("name")
    if name is not None and (not isinstance(name, str) or not NAME_RE.match(name)):
        errors.append(("name", "invalid name (RFC 1123)"))
        name = None
    unknown = sorted(set(doc) - {"name", "revision", "description", "variables", "phases"})
    errors.extend((k, "unknown key") for k in unknown)
    desc = _check_description(doc, errors)
    variables = _check_variables(doc, errors)
    phases_in = doc.get("phases")
    phases_in = {} if phases_in is None else phases_in
    if not isinstance(phases_in, dict):
        errors.append(("phases", "not a mapping"))
        phases_in = {}
    phases = {}
    for pid in phases_in:
        if pid not in PHASES:
            errors.append((f"phases.{pid}", "unknown phase"))
    for pid in PHASES:
        if pid not in phases_in:
            continue
        ph = phases_in[pid]
        if ph is None:
            ph = {}
        if not isinstance(ph, dict):
            errors.append((f"phases.{pid}", "not a mapping"))
            continue
        enabled = ph.get("enabled", True)
        if not isinstance(enabled, bool):
            errors.append((f"phases.{pid}.enabled", "not a boolean"))
            continue
        if pid not in AVAILABLE:
            if enabled:
                errors.append((f"phases.{pid}", f"{COMING}: set enabled: false"))
                continue
            # format de 2.5+ non figé : gardé tel quel, jamais lu en 2.4.0
            phases[pid] = dict(ph, enabled=False)
            continue
        if pid == "readiness":
            unknown = sorted(set(ph) - {"enabled", "timeout_minutes"})
            errors.extend((f"phases.readiness.{k}", "unknown key") for k in unknown)
            t = ph.get("timeout_minutes", TIMEOUT_DEFAULT)
            if isinstance(t, bool) or not isinstance(t, int) or not TIMEOUT_MIN <= t <= TIMEOUT_MAX:
                errors.append(("phases.readiness.timeout_minutes",
                               f"an integer from {TIMEOUT_MIN} to {TIMEOUT_MAX}"))
                t = TIMEOUT_DEFAULT
            phases[pid] = {"enabled": enabled, "timeout_minutes": t}
        else:
            unknown = sorted(set(ph) - {"enabled", "templates"})
            errors.extend((f"phases.terraform.{k}", "unknown key") for k in unknown)
            phases[pid] = {"enabled": enabled,
                           "templates": _check_terraform(ph, errors, name, known_templates)}
    if errors:
        raise ProfileError(errors, "invalid plan")
    return {"description": desc, "variables": variables, "phases": phases}


def enabled_phases(plan):
    return [p for p in PHASES if (plan.get("phases") or {}).get(p, {}).get("enabled")]


def plan_templates(plan):
    """Entrées de la phase Terraform d'un plan, dans l'ordre ([] si absente)."""
    return list(((plan.get("phases") or {}).get("terraform") or {}).get("templates") or [])


def _named(doc, name):
    return dict(doc, name=name) if isinstance(doc, dict) else doc


def save_plan(state, name, doc, revision=None):
    """Écrit le plan (0600, remplacement atomique) à la révision lue ;
    PlanConflict si elle est périmée, ProfileError s'il est invalide. Le nom
    d'enregistrement fait foi pour le contrôle des noms de déclaration."""
    return _save(state, PLAN_DIR, name, doc, revision, lambda d: check_plan(_named(d, name)))


def load_plan(state, name):
    """Plan lu et vérifié, ou None (absent, illisible, invalide)."""
    return _load(state, PLAN_DIR, name, lambda d: check_plan(_named(d, name)))


def list_plans(state):
    return _list(state, PLAN_DIR, lambda d: check_plan(d), lambda p: {
        "name": p["name"], "revision": p["revision"], "description": p["description"],
        "variables": p["variables"], "phases_enabled": enabled_phases(p)})


def delete_plan(state, name):
    return _delete(state, PLAN_DIR, name)


# --- modèles de déclaration ---------------------------------------------------

def secret_refusals(resources, base="resources"):
    """[(chemin, raison)] de ce qui ne se retire pas sans changer le sens et
    se refuse donc : HCL brut qui porte un secret, identifiants dans une
    adresse. Rejoué sur les ressources RENDUES (une valeur de variable peut
    apporter le secret que le modèle n'avait pas)."""
    out = []

    def walk(value, where):
        if isinstance(value, dict):
            for k, v in value.items():
                here = f"{where}.{k}"
                why = _hcl.refusal(v) if k == "tf" and isinstance(v, str) else None
                if why:
                    out.append((here, why))
                    continue
                walk(v, here)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, f"{where}[{i}]")
        elif isinstance(value, str) and (_URL_USERINFO_RE.search(value)
                                         or _URL_QUERY_SECRET_RE.search(value)):
            out.append((where, "credentials in a URL"))
    for i, r in enumerate(resources):
        walk(r, f"{base}[{i}]")
    return out


def scrub(resources):
    """(ressources sans secret, chemins retirés). Règle de clés de
    kvm_templates (SECRET_KEY_RE, à toute profondeur, valeur non vide),
    clés publiques et noms de Secrets exemptés ; `user_data` gardé, sauf
    s'il parle de mot de passe (alors retiré entier). Le HCL brut `tf`
    n'est jamais retiré : `secret_refusals` le refuse."""
    removed = []

    def walk(value, where):
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                here = f"{where}.{k}"
                if isinstance(k, str) and k in _TEXT_FIELDS and isinstance(v, str):
                    if _PASSWORD_TEXT_RE.search(v):
                        removed.append(here)
                        continue
                    out[k] = v
                    continue
                if k == "tf" and isinstance(v, str):
                    out[k] = v
                    continue
                if (isinstance(k, str) and _kvt.SECRET_KEY_RE.search(k) and k not in _PUBLIC_KEYS
                        and not _SECRET_NAME_RE.search(k) and v not in (None, "", [], {})):
                    removed.append(here)
                    continue
                out[k] = walk(v, here)
            return out
        if isinstance(value, list):
            return [walk(v, f"{where}[{i}]") for i, v in enumerate(value)]
        return value
    return [walk(r, f"resources[{i}]") for i, r in enumerate(resources)], removed


def _tf_store():
    """web/tf_store.py (bibliothèque standard seulement), source du format
    des ressources et des genres admis."""
    try:
        import tf_store
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "web"))
        import tf_store
    return tf_store


def check_template(doc, clean_resources=None):
    """(modèle normalisé {description, variables, resources}, chemins
    retirés), ou ProfileError. Les ressources sont celles d'une déclaration
    Harvester (`tf_store.clean_resources`) ; un identifiant manquant est
    attribué."""
    if not isinstance(doc, dict):
        raise ProfileError([("template", "not a mapping")], "invalid template")
    errors = []
    name = doc.get("name")
    if name is not None and (not isinstance(name, str) or not NAME_RE.match(name)):
        errors.append(("name", "invalid name (RFC 1123)"))
    unknown = sorted(set(doc) - {"name", "revision", "description", "variables", "resources", "removed"})
    errors.extend((k, "unknown key") for k in unknown)
    desc = _check_description(doc, errors)
    variables = _check_variables(doc, errors)
    resources = doc.get("resources")
    resources = [] if resources is None else resources
    if not isinstance(resources, list):
        errors.append(("resources", "not a list"))
        resources = []
    staged = []
    for i, r in enumerate(resources):
        if not isinstance(r, dict):
            errors.append((f"resources[{i}]", "not a mapping"))
            continue
        staged.append(dict(r, id=r.get("id") or uuid.uuid4().hex[:12]))
    if not errors:
        if clean_resources is None:
            clean_resources = _tf_store().clean_resources
        try:
            staged = clean_resources(staged, "harvester")
        except Exception as e:                 # StoreError du magasin des déclarations
            errors.append(("resources", str(e)))
    if not errors and not staged:
        errors.append(("resources", "no resource"))
    clean, removed = scrub(staged) if not errors else ([], [])
    errors.extend(secret_refusals(clean))
    known = set(BUILTIN_VARS) | set(variables)
    for var in sorted(_bmp.variables_in(clean)):
        if var not in known:
            errors.append((f"{{{{{var}}}}}", "undeclared variable"))
    if errors:
        raise ProfileError(errors, "invalid template")
    return {"description": desc, "variables": variables, "resources": clean}, removed


def save_template(state, name, doc, revision=None, clean_resources=None):
    """(modèle écrit, chemins retirés) ; PlanConflict, ProfileError. Les
    chemins retirés sont aussi gardés dans le fichier (`removed`), pour que
    l'interface les annonce à la relecture."""
    def check(d):
        clean, removed = check_template(d, clean_resources)
        return dict(clean, removed=removed), removed
    return _save(state, TEMPLATE_DIR, name, doc, revision, check)


def load_template(state, name, clean_resources=None):
    """Modèle lu et vérifié ({name, revision, description, variables,
    resources, removed}), ou None."""
    t = _load(state, TEMPLATE_DIR, name, lambda d: check_template(d, clean_resources))
    if t is not None:
        doc = _read(template_path(state, name)) or {}
        removed = doc.get("removed")
        t["removed"] = [str(x) for x in removed] if isinstance(removed, list) else []
    return t


def list_templates(state, clean_resources=None):
    return _list(state, TEMPLATE_DIR, lambda d: check_template(d, clean_resources), lambda t: {
        "name": t["name"], "revision": t["revision"], "description": t["description"],
        "variables": t["variables"], "kinds": sorted({r["kind"] for r in t["resources"]}),
        "uses": sorted(_bmp.variables_in(t["resources"]))})


def delete_template(state, name):
    return _delete(state, TEMPLATE_DIR, name)


# --- rendu ---------------------------------------------------------------------

def used_variables(plan, templates):
    """Variables que le lancement doit renseigner (intégrées comprises) :
    celles qu'emploient les modèles de la phase Terraform du plan.
    `templates` : {nom: modèle chargé} ; un modèle absent est ignoré ici
    (constat `template-missing` du lancement)."""
    out = set()
    for entry in plan_templates(plan):
        t = templates.get(entry["template"])
        if t:
            out |= _bmp.variables_in(t.get("resources") or [])
    return sorted(out)


def _var_path(obj, name, where):
    """Premier chemin où la variable apparaît, pour le message de refus."""
    if isinstance(obj, str):
        return where if name in _bmp.VAR_RE.findall(obj) else None
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = _var_path(v, name, f"{where}.{k}")
            if p:
                return p
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            p = _var_path(v, name, f"{where}[{i}]")
            if p:
                return p
    return None


def render_templates(plan, templates, values, field_type=None):
    """[(nom du modèle, ressources rendues)] dans l'ordre du plan.

    Chaque `spec` passe par bm_profiles.render_advanced : jetons neutres,
    relu, puis valeurs posées telles quelles (`a: b # c` arrive entier) ;
    une chaîne qui n'est qu'une variable prend le type du schéma Terraform
    (`field_type(kind, chemin)` -> "int", "uint", "bool" ou None).
    ProfileError : modèle absent, valeur invalide, variable absente (avec
    son chemin `templates[<modèle>].resources[<i>].spec.<champ>`)."""
    errors = []
    for k, v in sorted((values or {}).items()):
        why = _bmp.check_value(k, v)
        if why:
            errors.append((f"variables.{k}", why))
    if errors:
        raise ProfileError(errors, "invalid values")
    out = []
    for entry in plan_templates(plan):
        tname = entry["template"]
        declaration_name(plan.get("name") or "", tname)
        t = templates.get(tname)
        if t is None:
            errors.append((f"templates[{tname}]", "template not found"))
            continue
        rendered = []
        for i, r in enumerate(t.get("resources") or []):
            where = f"templates[{tname}].resources[{i}].spec"
            kind = r.get("kind")
            ft = (lambda p, _k=kind: field_type(_k, p)) if field_type else None
            spec = r.get("spec") or {}
            try:
                if kind == "raw" and isinstance(spec.get("tf"), str):
                    # revue B2-I4 : la valeur est placée selon sa place dans le
                    # HCL (échappée dans une chaîne, nombre ou booléen hors
                    # chaîne) ; elle ne change jamais la structure relue
                    tf_out, refused = _hcl.render(spec["tf"], values or {})
                    if refused:
                        errors.extend((f"{where}.tf", f"variable {n}: {why}") for n, why in refused)
                        continue
                    spec_out = dict(spec, tf=tf_out)
                else:
                    text = yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=1 << 20)
                    spec_out = _bmp.render_advanced(text, values or {}, ft) or {}
            except KeyError as e:
                errors.append((_var_path(spec, e.args[0], where) or where, "missing variable"))
                continue
            except ProfileError as e:
                errors.extend((f"{where} {w}", why) for w, why in e.errors)
                continue
            # décision 150 : la valeur d'une variable ne fait pas entrer un
            # secret que le modèle relu n'avait pas. Refusé, jamais retiré : un
            # retrait silencieux produirait une autre déclaration que la relue.
            res = dict(r, spec=spec_out)
            _clean, found = scrub([res])
            found = [(p, SECRET_REFUSED) for p in found] + secret_refusals([res])
            errors.extend((f"templates[{tname}].resources[{i}]{p[len('resources[0]'):]}", why)
                          for p, why in found)
            rendered.append(res)
        out.append((tname, rendered))
    if errors:
        raise ProfileError(errors, "cannot render the plan")
    return out
