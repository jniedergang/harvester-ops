"""Plans de déploiement et modèles de déclaration Terraform (2.4.0).

Un plan de déploiement est nommé et réutilisable. Il s'applique à un
cluster Harvester déclaré, ou s'enchaîne après une installation bare-metal
(un profil le désigne par son champ `plan`). Il active des phases :
disponibilité et contenu Terraform en 2.4.0 ; pile Cluster API et clusters
enfants en 2.5.0 ; registres et publication des paquets en 2.6.0 (phase
Registre, entre Terraform et Cluster API) ; artefacts appliqués aux
clusters enfants (charts, manifestes, politiques) en 2.7.0.

Un modèle de déclaration porte les ressources d'une déclaration Terraform
(format de web/tf_store.py), chaînes à variables `{{nom}}`. La phase
Terraform en fait des déclarations ordinaires nommées `<plan>-<modèle>`.

Rangement, comme les profils (bm_profiles) :

    <état>/plans.d/<nom>.yaml            un plan (0600), nom RFC 1123
    <état>/tf-templates.d/<nom>.yaml     un modèle (0600), nom RFC 1123
    <état>/capi-templates.d/<nom>.yaml   un modèle de cluster (2.5.0)

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
import capi_cluster as _cc
import hcl_guard as _hcl
import kvm_templates as _kvt

ProfileError = _bmp.ProfileError
NAME_RE = _bmp.NAME_RE

PLAN_DIR = "plans.d"
TEMPLATE_DIR = "tf-templates.d"
# 2.5.0 : modèles de cluster (demande du formulaire de création Cluster API,
# chaînes à variables) ; nom, namespace et namespace des VMs viennent de
# l'entrée du plan et de la règle du namespace (décision 164)
CAPI_TEMPLATE_DIR = "capi-templates.d"
CAPI_TEMPLATE_FORBIDDEN = ("name", "namespace", "target_namespace")
PHASES = ("readiness", "terraform", "registry", "capi", "clusters", "artefacts", "gitops")
AVAILABLE = PHASES
BUILTIN_VARS = ("cluster", "vip")
MAX_TEMPLATES = 32
TIMEOUT_MIN, TIMEOUT_MAX, TIMEOUT_DEFAULT = 1, 180, 20
# 2.5.0 : délais des phases Cluster API (minutes) : (min, max, défaut)
CAPI_TIMEOUT = (1, 60, 10)
CLUSTERS_TIMEOUT = (5, 180, 30)
MAX_CLUSTERS = 16
# `n` : variable intégrée du seul raccourci `name_pattern` (1 à `count`)
PATTERN_VAR = "n"
MAX_DESCRIPTION = 500
MAX_DECL_NAME = 63
# 2.6.0 : phase Registre (registres d'amorçage posés ou relus, registres du
# client vérifiés, paquets publiés) : (min, max, défaut) en minutes
REGISTRY_TIMEOUT = (1, 60, 10)
PUBLISH_TIMEOUT = (5, 600, 60)
REGISTRY_MODES = ("bootstrap", "external")
REGISTRY_SIZE = (5, 2048, 20)
MAX_REGISTRIES = 8
MAX_PUBLISH = 32
# 2.7.0 : phase Artefacts (Contraintes communes) : entrées, éléments une fois
# les clusters dépliés, délai par élément (minutes)
ARTEFACTS_TIMEOUT = (1, 60, 15)
MAX_ARTEFACT_ENTRIES = 32
MAX_ARTEFACT_UNITS = 128
ARTEFACT_KINDS = ("chart", "manifest", "policy")
ARTEFACT_REFUSED_KINDS = {"images": "published by the Registry phase", "gitops": "applied by a GitOps entry",
                          "rke2": "published by the Registry phase"}
# 2.8.0 : phase GitOps (Contraintes communes) : entrées, éléments une fois
# les clusters dépliés, délai par élément (minutes) ; une entrée devient
# l'élément `art-gitops-<nom>` d'un cluster (child_gitops.NAME_MAX)
GITOPS_TIMEOUT = (1, 60, 15)
MAX_GITOPS_ENTRIES = 16
MAX_GITOPS_UNITS = 64
GITOPS_ENTRY_KEYS = {"name", "tool", "clusters", "target_namespace", "source", "privileges",
                     "interval_minutes", "prune"}

# Noms de clés qui désignent un objet public ou le NOM d'un Secret, pas un
# secret : la clé publique d'une ressource ssh_key, les clés d'une VM
# désignées par nom, le Secret qui porte le cloud-init (décision 150 : un
# secret de VM passe par `user_data_secret_name` vers un Secret existant).
# `key` seul : la clé d'étiquette des `match_label_expressions` d'une classe
# de stockage (2.4.0), jamais une valeur secrète (`api_key` reste retiré).
_PUBLIC_KEYS = _hcl.PUBLIC_NAMES | {"key"}
# nom (ou espace de noms) d'un Secret : `user_data_secret_name`, et les
# paramètres d'une classe chiffrée `csi.storage.k8s.io/...-secret-name`
_SECRET_NAME_RE = _hcl.SECRET_NAME_OK_RE
# cloud-init retiré entier s'il parle de mot de passe (décision 152)
_TEXT_FIELDS = frozenset(("user_data",))
_PASSWORD_TEXT_RE = re.compile(r"password|passwd|chpasswd", re.I)
# HCL brut (`raw`, champ `tf`) : jamais retiré en silence, REFUSÉ par la
# règle de toute déclaration (web/tf_guard.py) puis par la garde des secrets
# de hcl_guard ; ses valeurs de variables y sont échappées et le rendu relu.
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


def _check_timeout(ph, where, bounds, errors):
    lo, hi, default = bounds
    t = ph.get("timeout_minutes", default)
    if isinstance(t, bool) or not isinstance(t, int) or not lo <= t <= hi:
        errors.append((f"{where}.timeout_minutes", f"an integer from {lo} to {hi}"))
        return default
    return t


def _check_capi_phase(ph, errors):
    """Phase capi : {enabled, timeout_minutes} (pile Cluster API du paquet
    actif ; CAAPH toujours déclaré avec elle, décision 172)."""
    unknown = sorted(set(ph) - {"enabled", "timeout_minutes"})
    errors.extend((f"phases.capi.{k}", "unknown key") for k in unknown)
    return {"enabled": ph.get("enabled", True) is not False,
            "timeout_minutes": _check_timeout(ph, "phases.capi", CAPI_TIMEOUT, errors)}


def _check_registry_phase(ph, errors, variables):
    """Phase registry (2.6.0, tâche 11) : {enabled, timeout_minutes,
    publish_timeout_minutes, registries: [{name, mode, address, size_gib,
    storage_class, mirrors, dns_name}], publish: [{package, version,
    registry}]}. Un registre `external` est déclaré et rattaché d'avance
    (contrôlé au lancement) ; un `bootstrap` ne porte aucun identifiant
    (générés par la console). Variables du plan admises dans l'adresse, la
    classe, les miroirs et le nom DNS."""
    import artefacts as _art
    where = "phases.registry"
    unknown = sorted(set(ph) - {"enabled", "timeout_minutes", "publish_timeout_minutes", "registries", "publish"})
    errors.extend((f"{where}.{k}", "unknown key") for k in unknown)
    timeout = _check_timeout(ph, where, REGISTRY_TIMEOUT, errors)
    lo, hi, d = PUBLISH_TIMEOUT
    pt = ph.get("publish_timeout_minutes", d)
    if isinstance(pt, bool) or not isinstance(pt, int) or not lo <= pt <= hi:
        errors.append((f"{where}.publish_timeout_minutes", f"an integer from {lo} to {hi}"))
        pt = d
    known = set(BUILTIN_VARS) | set(variables or [])

    def text(v, w, check=None, required=True):
        if v is None and not required:
            return None
        if not isinstance(v, str) or not v:
            errors.append((w, "required"))
            return None
        bad = [n for n in _bmp.VAR_RE.findall(v) if n not in known]
        if bad:
            errors.append((w, f"undeclared variable {bad[0]}"))
            return None
        if check and not _bmp.VAR_RE.search(v) and not check(v):
            errors.append((w, "invalid value"))
            return None
        return v
    regs_in = ph.get("registries") or []
    if not isinstance(regs_in, list) or len(regs_in) > MAX_REGISTRIES:
        errors.append((f"{where}.registries", f"a list of {MAX_REGISTRIES} registries at most"))
        regs_in = []
    regs, names = [], []
    for i, r in enumerate(regs_in):
        w = f"{where}.registries[{i}]"
        if not isinstance(r, dict):
            errors.append((w, "not a mapping"))
            continue
        mode = r.get("mode")
        allowed = {"name", "mode"} | ({"address", "size_gib", "storage_class", "mirrors", "dns_name"}
                                      if mode == "bootstrap" else set())
        errors.extend((f"{w}.{k}", "unknown key" if mode == "bootstrap" or k not in
                       {"address", "size_gib", "storage_class", "mirrors", "dns_name"} else "only for a bootstrap registry")
                      for k in sorted(set(r) - allowed))
        name = r.get("name")
        if not isinstance(name, str) or not _cc.REGISTRY_NAME_RE.match(name) or name == "bootstrap":
            errors.append((f"{w}.name", "RFC 1123, 30 characters at most"))
            continue
        if name in names:
            errors.append((f"{w}.name", f"{name} listed twice"))
            continue
        if mode not in REGISTRY_MODES:
            errors.append((f"{w}.mode", "bootstrap or external"))
            continue
        names.append(name)
        item = {"name": name, "mode": mode}
        if mode == "bootstrap":
            item["address"] = text(r.get("address"), f"{w}.address", _cc._is_ipv4)
            item["storage_class"] = text(r.get("storage_class"), f"{w}.storage_class", _cc.K8S_NAME_RE.match)
            lo, hi, d = REGISTRY_SIZE
            size = r.get("size_gib", d)
            if isinstance(size, bool) or not isinstance(size, int) or not lo <= size <= hi:
                errors.append((f"{w}.size_gib", f"an integer from {lo} to {hi}"))
                size = d
            item["size_gib"] = size
            mirrors = r.get("mirrors") or []
            if not isinstance(mirrors, list):
                errors.append((f"{w}.mirrors", "a list of registry hosts"))
                mirrors = []
            item["mirrors"] = [m for j, m in enumerate(mirrors)
                               if text(m, f"{w}.mirrors[{j}]", _cc.REGISTRY_HOST_RE.match) is not None]
            dns = text(r.get("dns_name"), f"{w}.dns_name", _cc.REGISTRY_HOST_RE.match, required=False)
            if dns:
                item["dns_name"] = dns
        regs.append(item)
    pubs_in = ph.get("publish") or []
    if not isinstance(pubs_in, list) or len(pubs_in) > MAX_PUBLISH:
        errors.append((f"{where}.publish", f"a list of {MAX_PUBLISH} publications at most"))
        pubs_in = []
    pubs, seen = [], set()
    for i, p in enumerate(pubs_in):
        w = f"{where}.publish[{i}]"
        if not isinstance(p, dict) or set(p) - {"package", "version", "registry"}:
            errors.append((w, "{package, version, registry}"))
            continue
        pkg, ver, reg = p.get("package"), str(p.get("version") or ""), p.get("registry")
        if not isinstance(pkg, str) or not _art.NAME_RE.match(pkg):
            errors.append((f"{w}.package", "a package name"))
            continue
        if not _art.VERSION_RE.match(ver):
            errors.append((f"{w}.version", "x.y.z or YYYY.MM[.DD][-n]"))
            continue
        if not isinstance(reg, str) or not _cc.REGISTRY_NAME_RE.match(reg):
            errors.append((f"{w}.registry", "a registry name"))
            continue
        if (pkg, ver, reg) in seen:
            errors.append((w, f"{pkg} {ver} to {reg} listed twice"))
            continue
        seen.add((pkg, ver, reg))
        pubs.append({"package": pkg, "version": ver, "registry": reg})
    return {"enabled": ph.get("enabled", True) is not False, "timeout_minutes": timeout,
            "publish_timeout_minutes": pt, "registries": regs, "publish": pubs}


def _child_ref_shape_ok(ref):
    """`<ns>/<nom>` d'un cluster enfant existant, variables remplacées par
    une lettre, chaque moitié RFC 1123."""
    if not isinstance(ref, str) or ref.count("/") != 1:
        return False
    ns, name = _bmp.VAR_RE.sub("a", ref).split("/")
    return bool(_cc.K8S_NAME_RE.match(ns)) and bool(_cc.K8S_NAME_RE.match(name))


def _cluster_refs(raw, w, cluster_names, known, errors, code="artefact-cluster-unknown"):
    """Clusters d'une entrée (phases Artefacts et GitOps) : un cluster de la
    phase Clusters (nom tel qu'écrit) ou un cluster enfant existant
    `<ns>/<nom>`, variables du plan permises, sans doublon."""
    if not isinstance(raw, list) or not raw:
        errors.append((w, "a list of clusters, one at least"))
        return []
    out = []
    for j, c in enumerate(raw):
        if c in (cluster_names or ()):
            pass
        elif not _child_ref_shape_ok(c):
            errors.append((f"{w}[{j}]", f"{code}: {str(c)[:60]} is neither a cluster of "
                                        "the Clusters phase nor <namespace>/<name>"))
            continue
        bad = [v for v in _bmp.VAR_RE.findall(c) if v not in known]
        if bad:
            errors.append((f"{w}[{j}]", f"undeclared variable {bad[0]}"))
            continue
        if c in out:
            errors.append((f"{w}[{j}]", f"{c} listed twice"))
            continue
        out.append(c)
    return out


def _values_refusals(values, where):
    """Règles de secrets du plan sur les valeurs d'un chart : une clé qui
    désigne un secret (règle de `scrub`) ou une adresse à identifiants est
    refusée, jamais retirée en silence (le chart changerait de sens)."""
    _clean, removed = scrub([values])
    out = [(w.replace("resources[0]", where, 1), SECRET_REFUSED) for w in removed]
    out += [(w.replace("resources[0]", where, 1), why) for w, why in secret_refusals([values])]
    return out


def artefact_entry_refusals(entry, kind, where):
    """[(où, raison)] d'une entrée d'artefact dont la sorte est connue
    (paquet importé) : registre exigé pour un chart et refusé ailleurs,
    valeurs pour un chart seulement, sortes non applicables. Rejoué au
    lancement, quand le paquet est là (même règle, un seul endroit)."""
    if kind in ARTEFACT_REFUSED_KINDS:
        return [(f"{where}.item", f"a {kind} item is not applied: {ARTEFACT_REFUSED_KINDS[kind]}")]
    if kind not in ARTEFACT_KINDS:
        return [(f"{where}.item", f"unknown item kind {kind}")]
    out = []
    if kind == "chart" and not entry.get("registry"):
        out.append((f"{where}.registry", "required for a chart (published in this registry)"))
    if kind != "chart":
        out += [(f"{where}.{k}", f"only for a chart, not a {kind}") for k in ("registry", "values") if k in entry]
    return out


def _check_artefacts_phase(ph, errors, variables, cluster_names, item_kind=None):
    """Phase artefacts (2.7.0, tâche 4) : {enabled, timeout_minutes,
    policies: [{clusters, base, teams, roles, accounts}], items: [{package,
    version, item, registry, values, clusters, depends_on}]}. `clusters`
    désigne un cluster de la phase Clusters (nom tel qu'écrit) ou un cluster
    enfant existant `<ns>/<nom>` ; variables du plan permises dans les
    clusters et le registre. `item_kind(paquet, version, élément)` rend la
    sorte d'un élément d'un paquet importé, ou None : sans elle, les règles
    de sorte sont rejouées au lancement."""
    import artefacts as _art
    import child_policies as _cp
    where = "phases.artefacts"
    unknown = sorted(set(ph) - {"enabled", "timeout_minutes", "policies", "items"})
    errors.extend((f"{where}.{k}", "unknown key") for k in unknown)
    timeout = _check_timeout(ph, where, ARTEFACTS_TIMEOUT, errors)
    known = set(BUILTIN_VARS) | set(variables or [])
    units = 0

    def clusters_of(raw, w):
        return _cluster_refs(raw, w, cluster_names, known, errors)
    pols_in = ph.get("policies") or []
    if not isinstance(pols_in, list):
        errors.append((f"{where}.policies", "not a list"))
        pols_in = []
    pols, pol_clusters = [], set()
    for i, p in enumerate(pols_in):
        w = f"{where}.policies[{i}]"
        if not isinstance(p, dict):
            errors.append((w, "not a mapping"))
            continue
        cl = clusters_of(p.get("clusters"), f"{w}.clusters")
        raw = {k: v for k, v in p.items() if k != "clusters"}
        try:
            _cp.normalize(raw)
        except ValueError as e:
            field, _, why = str(e).partition(": ")
            errors.append((f"{w}.{field.removeprefix('policies.')}" if why else w, why or str(e)))
            continue
        twice = sorted(set(cl) & pol_clusters)
        if twice:
            errors.append((f"{w}.clusters", f"{twice[0]} already has its policies"))
            continue
        pol_clusters |= set(cl)
        units += len(cl)
        pols.append(dict(raw, clusters=cl))
    items_in = ph.get("items") or []
    if not isinstance(items_in, list):
        errors.append((f"{where}.items", "not a list"))
        items_in = []
    if len(items_in) + len(pols_in) > MAX_ARTEFACT_ENTRIES:
        errors.append((f"{where}.items", f"{MAX_ARTEFACT_ENTRIES} entries at most"))
        items_in = []
    items, seen = [], []
    for i, e in enumerate(items_in):
        w = f"{where}.items[{i}]"
        if not isinstance(e, dict):
            errors.append((w, "not a mapping"))
            continue
        unknown = sorted(set(e) - {"package", "version", "item", "registry", "values", "clusters", "depends_on"})
        errors.extend((f"{w}.{k}", "unknown key") for k in unknown)
        pkg, ver, item = e.get("package"), str(e.get("version") or ""), e.get("item")
        if not isinstance(pkg, str) or not _art.NAME_RE.match(pkg):
            errors.append((f"{w}.package", "a package name"))
            continue
        if not _art.VERSION_RE.match(ver):
            errors.append((f"{w}.version", "x.y.z or YYYY.MM[.DD][-n]"))
            continue
        if not isinstance(item, str) or not _art.ITEM_RE.match(item):
            errors.append((f"{w}.item", "an item name (RFC 1123)"))
            continue
        if item == _cp.ITEM:
            errors.append((f"{w}.item", f"{item} is the name of the policies of the console"))
            continue
        if item in seen:
            errors.append((f"{w}.item", f"{item} listed twice (one object name per item in a cluster)"))
            continue
        cl = clusters_of(e.get("clusters"), f"{w}.clusters")
        entry = {"package": pkg, "version": ver, "item": item, "clusters": cl}
        reg = e.get("registry")
        if reg is not None:
            if not isinstance(reg, str) or not reg:
                errors.append((f"{w}.registry", "a registry name"))
            else:
                bad = [v for v in _bmp.VAR_RE.findall(reg) if v not in known]
                if bad:
                    errors.append((f"{w}.registry", f"undeclared variable {bad[0]}"))
                elif not _bmp.VAR_RE.search(reg) and not _cc.REGISTRY_NAME_RE.match(reg):
                    errors.append((f"{w}.registry", "a registry name"))
                else:
                    entry["registry"] = reg
        if "values" in e:
            vals = e.get("values")
            if not isinstance(vals, dict):
                errors.append((f"{w}.values", "a mapping"))
            else:
                bad = _values_refusals(vals, f"{w}.values")
                errors.extend(bad)
                if not bad:
                    entry["values"] = vals
        kind = item_kind(pkg, ver, item) if item_kind else None
        if kind is not None:
            errors.extend(artefact_entry_refusals(e, kind, w))
        deps = e.get("depends_on") or []
        if isinstance(deps, str):
            deps = [deps]
        if not isinstance(deps, list):
            errors.append((f"{w}.depends_on", "not a list"))
            deps = []
        clean = []
        for d in deps:
            if d == item:
                errors.append((f"{w}.depends_on", "an item does not depend on itself"))
            elif d not in seen:
                errors.append((f"{w}.depends_on", f"{d} is not listed before this item"))
            elif d not in clean:
                clean.append(d)
        if clean:
            entry["depends_on"] = clean
        seen.append(item)
        units += len(cl)
        items.append(entry)
    if units > MAX_ARTEFACT_UNITS:
        errors.append((f"{where}.items", f"{units} items once the clusters are unfolded, "
                                         f"{MAX_ARTEFACT_UNITS} at most"))
    return {"enabled": ph.get("enabled", True) is not False, "timeout_minutes": timeout,
            "policies": pols, "items": items}


def plan_artefacts(plan):
    """(politiques, éléments) de la phase Artefacts d'un plan ([] si absente)."""
    ph = (plan.get("phases") or {}).get("artefacts") or {}
    return list(ph.get("policies") or []), list(ph.get("items") or [])


def _check_gitops_phase(ph, errors, variables, cluster_names):
    """Phase gitops (2.8.0, tâche 5) : {enabled, timeout_minutes, entries:
    [{name, tool, clusters, target_namespace, source, privileges,
    interval_minutes, prune}]}. Une entrée suit child_gitops.normalize (le
    namespace visé s'écrit `target_namespace`) ; `clusters` comme la phase
    Artefacts ; variables du plan permises dans les clusters et dans le
    registre d'une source ; `source.secret` nomme un secret du plan, saisi
    au lancement et jamais rangé (décision 129)."""
    import child_gitops as _cg
    where = "phases.gitops"
    unknown = sorted(set(ph) - {"enabled", "timeout_minutes", "entries"})
    errors.extend((f"{where}.{k}", "unknown key") for k in unknown)
    timeout = _check_timeout(ph, where, GITOPS_TIMEOUT, errors)
    known = set(BUILTIN_VARS) | set(variables or [])
    raw = ph.get("entries") or []
    if not isinstance(raw, list):
        errors.append((f"{where}.entries", "not a list"))
        raw = []
    if len(raw) > MAX_GITOPS_ENTRIES:
        errors.append((f"{where}.entries", f"{MAX_GITOPS_ENTRIES} entries at most"))
        raw = []
    out, seen, units = [], [], 0
    for i, e in enumerate(raw):
        w = f"{where}.entries[{i}]"
        if not isinstance(e, dict):
            errors.append((w, "not a mapping"))
            continue
        bad_keys = sorted(set(e) - GITOPS_ENTRY_KEYS)
        errors.extend((f"{w}.{k}", "unknown key") for k in bad_keys)
        if bad_keys:
            continue
        name = e.get("name")
        if not isinstance(name, str) or not _cg.NAME_RE.match(name):
            errors.append((f"{w}.name", f"RFC 1123, {_cg.NAME_MAX} characters at most"))
            continue
        if name in seen:
            errors.append((f"{w}.name", f"{name} listed twice"))
            continue
        cl = _cluster_refs(e.get("clusters"), f"{w}.clusters", cluster_names, known, errors,
                           code="gitops-cluster-unknown")
        if not cl:
            continue
        src = e.get("source")
        reg = src.get("registry") if isinstance(src, dict) else None
        if isinstance(reg, str) and _bmp.VAR_RE.search(reg):
            bad = [v for v in _bmp.VAR_RE.findall(reg) if v not in known]
            if bad:
                errors.append((f"{w}.source.registry", f"undeclared variable {bad[0]}"))
                continue
        elif reg is not None and (not isinstance(reg, str) or not _cc.REGISTRY_NAME_RE.match(reg)):
            errors.append((f"{w}.source.registry", "a registry name"))
            continue
        trial = {k: v for k, v in e.items() if k not in ("clusters", "target_namespace")}
        trial["namespace"] = e.get("target_namespace")
        if isinstance(src, dict) and isinstance(reg, str):
            trial["source"] = dict(src, registry=_bmp.VAR_RE.sub("a", reg))
        try:
            norm = _cg.normalize(trial)
        except ValueError as ex:
            field, _, why = str(ex).partition(": ")
            field = field.removeprefix("gitops.").replace("namespace", "target_namespace", 1) \
                if field.startswith("gitops.namespace") else field.removeprefix("gitops.")
            errors.append((f"{w}.{field}" if why else w, why or str(ex)))
            continue
        if isinstance(src, dict) and isinstance(reg, str):
            norm["source"]["registry"] = reg
        entry = {k: v for k, v in norm.items() if k != "namespace"}
        entry.update(target_namespace=norm["namespace"], clusters=cl)
        seen.append(name)
        units += len(cl)
        out.append(entry)
    if units > MAX_GITOPS_UNITS:
        errors.append((f"{where}.entries", f"{units} items once the clusters are unfolded, "
                                           f"{MAX_GITOPS_UNITS} at most"))
    return {"enabled": ph.get("enabled", True) is not False, "timeout_minutes": timeout, "entries": out}


def plan_gitops(plan):
    """Entrées de la phase GitOps d'un plan ([] si absente)."""
    return list(((plan.get("phases") or {}).get("gitops") or {}).get("entries") or [])


def gitops_secret_names(plan):
    """Noms des secrets du plan que la phase GitOps demande au lancement."""
    if not ((plan.get("phases") or {}).get("gitops") or {}).get("enabled"):
        return []
    return sorted({e["source"]["secret"] for e in plan_gitops(plan) if e["source"].get("secret")})


def render_artefact_clusters(plan, names, values, rendered_plan_clusters, phase="artefacts"):
    """Clusters d'une entrée rendus : un nom de la phase Clusters devient
    `<nom rendu>/<nom rendu>` (namespace = nom, règle 164), une référence
    `<ns>/<nom>` est rendue avec les valeurs du lancement. Rend [(ref,
    nom de la phase Clusters ou None)] ; ProfileError."""
    out, errors = [], []
    for c in names:
        if c in rendered_plan_clusters:
            r = rendered_plan_clusters[c]
            out.append((f"{r}/{r}", r))
            continue
        missing = [v for v in _bmp.VAR_RE.findall(c) if v not in (values or {})]
        if missing:
            errors.append((f"{phase}.clusters[{c}]", f"missing variable {missing[0]}"))
            continue
        ref = _bmp.VAR_RE.sub(lambda m: str(values[m.group(1)]), c)
        if not _child_ref_shape_ok(ref) or _bmp.VAR_RE.search(ref):
            errors.append((f"{phase}.clusters[{c}]", f"rendered {ref[:60]} is not <namespace>/<name>"))
            continue
        out.append((ref, None))
    if errors:
        raise ProfileError(errors, "cannot render the clusters of an artefact")
    return out


def render_artefact_registry(reg, values, where="artefacts.registry"):
    """Registre d'une entrée rendu (phases Artefacts et GitOps) ; ProfileError."""
    missing = [v for v in _bmp.VAR_RE.findall(reg) if v not in (values or {})]
    if missing:
        raise ProfileError([(where, f"missing variable {missing[0]}")], "missing variable")
    out = _bmp.VAR_RE.sub(lambda m: str(values[m.group(1)]), reg)
    if not _cc.REGISTRY_NAME_RE.match(out):
        raise ProfileError([(where, f"rendered {out[:40]} is not a registry name")], "invalid")
    return out


def plan_registries(plan):
    """Registres de la phase Registre d'un plan, dans l'ordre ([] si absente)."""
    return list(((plan.get("phases") or {}).get("registry") or {}).get("registries") or [])


def render_plan_registries(plan, values):
    """Registres de la phase Registre rendus avec les valeurs du lancement
    (adresse, classe, miroirs, nom DNS), contrôlés une fois rendus ;
    ProfileError (variable absente, valeur invalide)."""
    out, errors = [], []
    for r in plan_registries(plan):
        w = f"registry[{r['name']}]"
        item = dict(r)
        for k in ("address", "storage_class", "dns_name"):
            if isinstance(r.get(k), str):
                missing = [n for n in _bmp.VAR_RE.findall(r[k]) if n not in (values or {})]
                if missing:
                    errors.append((f"{w}.{k}", f"missing variable {missing[0]}"))
                    continue
                item[k] = _bmp.VAR_RE.sub(lambda m: str(values[m.group(1)]), r[k])
        if r.get("mirrors"):
            try:
                item["mirrors"] = render_registries([{"registry": "x", "mirrors": r["mirrors"]}], values,
                                                    f"{w}.mirrors")[0].get("mirrors", [])
            except ProfileError as e:
                errors.extend(e.errors)
                continue
        if r["mode"] == "bootstrap":
            if item.get("address") and not _cc._is_ipv4(item["address"]):
                errors.append((f"{w}.address", f"{item['address'][:40]}: an IPv4 address"))
            if item.get("storage_class") and not _cc.K8S_NAME_RE.match(item["storage_class"]):
                errors.append((f"{w}.storage_class", "a storage class name"))
            for m in item.get("mirrors") or []:
                if not _cc.REGISTRY_HOST_RE.match(m):
                    errors.append((f"{w}.mirrors", f"{m[:60]}: a registry host"))
            if item.get("dns_name") and not _cc.REGISTRY_HOST_RE.match(item["dns_name"]):
                errors.append((f"{w}.dns_name", "a host name"))
        out.append(item)
    if errors:
        raise ProfileError(errors, "cannot render the registries")
    return out


def plan_publications(plan):
    return list(((plan.get("phases") or {}).get("registry") or {}).get("publish") or [])


def _plan_docs(state):
    d = Path(state) / PLAN_DIR
    try:
        files = sorted(p for p in d.glob("*.yaml") if p.is_file())
    except OSError:
        return []
    return [(p.stem, _read(p) or {}) for p in files]


def _phase(doc, pid):
    phases = doc.get("phases") if isinstance(doc.get("phases"), dict) else {}
    ph = phases.get(pid)
    return ph if isinstance(ph, dict) else {}


def _entry_names(regs):
    return {(e if isinstance(e, str) else (e or {}).get("registry")) for e in regs or []
            if isinstance(e, (str, dict))}


def registry_users(state, name):
    """Plans et modèles de cluster enregistrés qui désignent ce registre
    (phase Registre, publications, registres d'une entrée de cluster,
    registres d'un modèle), activés ou non : `plan:<nom>`, `template:<nom>`."""
    out = []
    for stem, doc in _plan_docs(state):
        reg = _phase(doc, "registry")
        names = {r.get("name") for r in reg.get("registries") or [] if isinstance(r, dict)}
        names |= {p.get("registry") for p in reg.get("publish") or [] if isinstance(p, dict)}
        for it in _phase(doc, "clusters").get("items") or []:
            if isinstance(it, dict):
                names |= _entry_names(it.get("registries"))
        names |= {it.get("registry") for it in _phase(doc, "artefacts").get("items") or [] if isinstance(it, dict)}
        names |= {(e.get("source") or {}).get("registry") for e in _phase(doc, "gitops").get("entries") or []
                  if isinstance(e, dict) and isinstance(e.get("source"), dict)}
        if name in names:
            out.append(f"plan:{stem}")
        elif any(isinstance(n, str) and _bmp.VAR_RE.search(n) for n in names):
            # revue 2.6.0 m-11 : un registre désigné par une variable peut
            # être celui-ci au lancement
            out.append(f"plan:{stem} (possibly, through a variable)")
    d = Path(state) / CAPI_TEMPLATE_DIR
    try:
        files = sorted(p for p in d.glob("*.yaml") if p.is_file())
    except OSError:
        files = []
    for p in files:
        spec = (_read(p) or {}).get("spec")
        if isinstance(spec, dict) and name in _entry_names(spec.get("registries")):
            out.append(f"template:{p.stem}")
    return out


def package_users(state, name, version):
    """Plans enregistrés qui publient ce paquet (phase Registre) ou en
    appliquent un élément (phase Artefacts, 2.7.0 ; source d'une entrée
    GitOps, 2.8.0)."""
    out = []
    for stem, doc in _plan_docs(state):
        if any(isinstance(p, dict) and p.get("package") == name and str(p.get("version")) == str(version)
               for p in list(_phase(doc, "registry").get("publish") or [])
               + list(_phase(doc, "artefacts").get("items") or [])
               + [e.get("source") for e in _phase(doc, "gitops").get("entries") or [] if isinstance(e, dict)]):
            out.append(stem)
    return out


def cluster_name_shape_ok(name):
    """Forme d'un nom de cluster enfant AVANT rendu : chaque variable
    remplacée par une lettre, le résultat suit capi_cluster.NAME_RE. Le nom
    rendu est contrôlé de nouveau au lancement (longueur réelle)."""
    return isinstance(name, str) and bool(_cc.NAME_RE.match(_bmp.VAR_RE.sub("a", name)))


def _expand_shortcut(ph, where, errors):
    """Raccourci `{template, count, name_pattern}` développé en entrées
    (`n` de 1 à `count`), ou []."""
    if not any(k in ph for k in ("count", "name_pattern", "template")):
        return []
    count, pattern, tname = ph.get("count"), ph.get("name_pattern"), ph.get("template")
    bad = False
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_CLUSTERS:
        errors.append((f"{where}.count", f"an integer from 1 to {MAX_CLUSTERS}"))
        bad = True
    if not isinstance(pattern, str) or PATTERN_VAR not in _bmp.VAR_RE.findall(pattern):
        errors.append((f"{where}.name_pattern", "a name holding {{n}}"))
        bad = True
    if not isinstance(tname, str) or not NAME_RE.match(tname):
        errors.append((f"{where}.template", "missing or invalid template name"))
        bad = True
    if bad:
        return []
    return [{"template": tname, "name": _bmp.VAR_RE.sub(
        lambda m: str(i) if m.group(1) == PATTERN_VAR else m.group(0), pattern)}
        for i in range(1, count + 1)]


def _check_clusters_phase(ph, errors, variables):
    """Phase clusters : {enabled, timeout_minutes, items: [{template, name,
    depends_on}]} ; le raccourci est développé à la suite des entrées.
    Noms à variables (plan et intégrées), uniques, 16 entrées au plus ;
    `depends_on` désigne une entrée placée avant (règle de la phase
    Terraform)."""
    where = "phases.clusters"
    unknown = sorted(set(ph) - {"enabled", "timeout_minutes", "items", "template", "count", "name_pattern"})
    errors.extend((f"{where}.{k}", "unknown key") for k in unknown)
    timeout = _check_timeout(ph, where, CLUSTERS_TIMEOUT, errors)
    raw = ph.get("items")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        errors.append((f"{where}.items", "not a list"))
        raw = []
    raw = list(raw) + _expand_shortcut(ph, where, errors)
    if len(raw) > MAX_CLUSTERS:
        errors.append((f"{where}.items", f"more than {MAX_CLUSTERS} clusters"))
        raw = []
    known = set(BUILTIN_VARS) | set(variables or [])
    out, seen = [], []
    for i, entry in enumerate(raw):
        w = f"{where}.items[{i}]"
        if not isinstance(entry, dict):
            errors.append((w, "not a mapping"))
            continue
        unknown = sorted(set(entry) - {"template", "name", "depends_on", "registries"})
        errors.extend((f"{w}.{k}", "unknown key") for k in unknown)
        tname, name = entry.get("template"), entry.get("name")
        if not isinstance(tname, str) or not NAME_RE.match(tname):
            errors.append((f"{w}.template", "missing or invalid template name"))
            continue
        if not cluster_name_shape_ok(name):
            errors.append((f"{w}.name", "lowercase letters, digits and '-', starting with a letter, "
                                        "variables allowed"))
            continue
        undeclared = [v for v in _bmp.VAR_RE.findall(name) if v not in known]
        if undeclared:
            errors.append((f"{w}.name", f"undeclared variable {undeclared[0]}"))
            continue
        if name in seen:
            errors.append((f"{w}.name", f"{name} listed twice"))
            continue
        deps = entry.get("depends_on") or []
        if isinstance(deps, str):
            deps = [deps]
        if not isinstance(deps, list):
            errors.append((f"{w}.depends_on", "not a list"))
            deps = []
        clean = []
        for d in deps:
            if d == name:
                errors.append((f"{w}.depends_on", "a cluster does not depend on itself"))
            elif d not in seen:
                errors.append((f"{w}.depends_on", f"{d} is not listed before this cluster"))
            elif d not in clean:
                clean.append(d)
        regs = entry.get("registries")
        if regs is not None:
            # 2.6.0 : registres de cette entrée, à la place de ceux du modèle ;
            # un registre du plan (phase Registre) se désigne par son nom.
            # 2.7.0 (m-13) : `[]` explicite remplace aussi (aucun registre)
            if not registries_shape_ok(regs):
                errors.append((f"{w}.registries", "a list of registry names or {registry, mirrors, no_fallback}"))
                regs = None
            else:
                undeclared = [v for v in sorted(_bmp.variables_in(regs)) if v not in known]
                if undeclared:
                    errors.append((f"{w}.registries", f"undeclared variable {undeclared[0]}"))
                    regs = None
        seen.append(name)
        item = {"template": tname, "name": name}
        if clean:
            item["depends_on"] = clean
        if regs is not None:
            item["registries"] = regs
        out.append(item)
    return {"enabled": ph.get("enabled", True) is not False, "timeout_minutes": timeout, "items": out}


def check_plan_warnings(doc, known_templates=None, item_kind=None):
    """(plan normalisé, avertissements) ; voir check_plan. Avertissements :
    clés d'une phase désactivée d'un plan plus ancien (Cluster API de 2.4.0,
    Artefacts de 2.6.0), retirées. `item_kind` : voir
    _check_artefacts_phase."""
    if not isinstance(doc, dict):
        raise ProfileError([("plan", "not a mapping")], "invalid plan")
    errors, warnings = [], []
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
        cl_raw = phases_in.get("clusters") or {}
        cluster_names = [c["name"] for c in _check_clusters_phase(cl_raw, [], variables)["items"]] \
            if pid in ("artefacts", "gitops") and isinstance(cl_raw, dict) else []
        if pid in ("capi", "clusters", "registry", "artefacts", "gitops") and not enabled:
            # Plan écrit en 2.4.0 : la phase désactivée y gardait des clés
            # libres (format non figé alors). Acceptée, clés inconnues
            # ignorées et retirées au prochain enregistrement, avec un
            # avertissement ; activée, la règle stricte s'applique.
            sub = []
            phases[pid] = (_check_capi_phase(ph, sub) if pid == "capi"
                           else _check_registry_phase(ph, sub, variables) if pid == "registry"
                           else _check_artefacts_phase(ph, sub, variables, cluster_names, item_kind)
                           if pid == "artefacts"
                           else _check_gitops_phase(ph, sub, variables, cluster_names) if pid == "gitops"
                           else _check_clusters_phase(ph, sub, variables))
            phases[pid]["enabled"] = False
            for where, why in sub:
                warnings.append(f"{where}: {why}, ignored (phase disabled)")
            continue
        if pid == "capi":
            phases[pid] = _check_capi_phase(ph, errors)
            continue
        if pid == "clusters":
            phases[pid] = _check_clusters_phase(ph, errors, variables)
            continue
        if pid == "registry":
            phases[pid] = _check_registry_phase(ph, errors, variables)
            continue
        if pid == "artefacts":
            phases[pid] = _check_artefacts_phase(ph, errors, variables, cluster_names, item_kind)
            continue
        if pid == "gitops":
            phases[pid] = _check_gitops_phase(ph, errors, variables, cluster_names)
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
    return {"description": desc, "variables": variables, "phases": phases}, warnings


def check_plan(doc, known_templates=None, item_kind=None):
    """Plan normalisé {description, variables, phases}, ou ProfileError
    [(où, raison)]. `known_templates` (noms) : refuse un modèle absent ;
    sans lui, l'existence des modèles est contrôlée au lancement. De même
    `item_kind` pour la sorte des éléments d'artefacts."""
    return check_plan_warnings(doc, known_templates, item_kind)[0]


def enabled_phases(plan):
    return [p for p in PHASES if (plan.get("phases") or {}).get(p, {}).get("enabled")]


def plan_templates(plan):
    """Entrées de la phase Terraform d'un plan, dans l'ordre ([] si absente)."""
    return list(((plan.get("phases") or {}).get("terraform") or {}).get("templates") or [])


def plan_clusters(plan):
    """Entrées de la phase clusters d'un plan, dans l'ordre ([] si absente)."""
    return list(((plan.get("phases") or {}).get("clusters") or {}).get("items") or [])


def render_cluster_name(name, values):
    """Nom d'une entrée de la phase clusters rendu avec les valeurs du
    lancement ; ProfileError (variable absente, nom rendu invalide)."""
    missing = [v for v in _bmp.VAR_RE.findall(name) if v not in (values or {})]
    if missing:
        raise ProfileError([(f"clusters[{name}].name", f"missing variable {missing[0]}")], "missing variable")
    out = _bmp.VAR_RE.sub(lambda m: str(values[m.group(1)]), name)
    if not _cc.NAME_RE.match(out):
        raise ProfileError([(f"clusters[{name}].name", f"rendered name {out[:45]} is not valid: lowercase "
                                                       "letters, digits and '-', starting with a letter, "
                                                       "1 to 40 characters")], "invalid name")
    return out


def _named(doc, name):
    return dict(doc, name=name) if isinstance(doc, dict) else doc


def item_kind_from(state):
    """`item_kind` lu dans les paquets importés de l'état (None : paquet ou
    élément absent, contrôlé au lancement)."""
    import artefacts as _art

    def kind(pkg, ver, item):
        try:
            idx = _art.load_package(state, pkg, ver)["index"]
        except (_art.ArtefactError, TypeError):
            return None
        return next((i.get("kind") for i in idx.get("items") or [] if i.get("name") == item), None)
    return kind


def save_plan(state, name, doc, revision=None):
    """Écrit le plan (0600, remplacement atomique) à la révision lue ;
    PlanConflict si elle est périmée, ProfileError s'il est invalide. Le nom
    d'enregistrement fait foi pour le contrôle des noms de déclaration ; la
    sorte des éléments d'artefacts est lue dans les paquets importés."""
    kinds = item_kind_from(state)
    out = _save(state, PLAN_DIR, name, doc, revision, lambda d: check_plan_warnings(_named(d, name), item_kind=kinds))
    out, warnings = out
    # avertissements rendus, jamais écrits (clés d'une phase désactivée retirées)
    return dict(out, warnings=warnings) if warnings else out


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

def _raw_refusal(text):
    """Raison du refus d'un HCL brut de modèle : d'abord la règle de toute
    déclaration (web/tf_guard.py : blocs, types, fonctions, provisioners),
    puis ce qui est propre aux modèles (secrets, hcl_guard). None s'il passe.
    Les variables `{{x}}` hors chaîne se lisent comme `{ {x} }` : relu tel
    quel, le modèle passe par la même lecture que son rendu."""
    bad = _hcl._tfg.check_resources(text, "harvester")
    if bad:
        return str(bad[0]).split(": ", 1)[-1]
    return _hcl.refusal(text)


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
                why = _raw_refusal(v) if k == "tf" and isinstance(v, str) else None
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
    unknown = sorted(set(doc) - {"name", "revision", "description", "variables", "resources", "removed",
                                 "updated_by"})
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


def save_template(state, name, doc, revision=None, clean_resources=None, by=""):
    """(modèle écrit, chemins retirés) ; PlanConflict, ProfileError. Les
    chemins retirés sont aussi gardés dans le fichier (`removed`), pour que
    l'interface les annonce à la relecture. `by` : qui écrit cette révision
    (décision 161 : la chaîne lancée par un opérateur consigne l'auteur du
    modèle dont elle applique le code), jamais pris du document."""
    def check(d):
        clean, removed = check_template(d, clean_resources)
        return dict(clean, removed=removed, updated_by=str(by or "")[:128]), removed
    return _save(state, TEMPLATE_DIR, name, doc, revision, check)


def load_template(state, name, clean_resources=None):
    """Modèle lu et vérifié ({name, revision, description, variables,
    resources, removed}), ou None."""
    t = _load(state, TEMPLATE_DIR, name, lambda d: check_template(d, clean_resources))
    if t is not None:
        doc = _read(template_path(state, name)) or {}
        removed = doc.get("removed")
        t["removed"] = [str(x) for x in removed] if isinstance(removed, list) else []
        by = doc.get("updated_by")
        t["updated_by"] = by if isinstance(by, str) else ""
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


# --- modèles de cluster (2.5.0) -------------------------------------------------

def capi_template_path(state, name):
    return _path(state, CAPI_TEMPLATE_DIR, name)


def _capi_url_refusals(spec, base):
    """Identifiants dans une adresse (`fleet_repo` surtout) : refusés, comme
    dans un modèle de déclaration."""
    out = []
    for k, v in spec.items():
        for i, s in enumerate(v if isinstance(v, list) else [v]):
            if isinstance(s, str) and (_URL_USERINFO_RE.search(s) or _URL_QUERY_SECRET_RE.search(s)):
                out.append((f"{base}.{k}" + (f"[{i}]" if isinstance(v, list) else ""), "credentials in a URL"))
    return out


def registries_shape_ok(v):
    """Forme d'une liste de registres d'un modèle ou d'une entrée de plan
    (2.6.0) : noms (variables admises) ou {registry, mirrors, no_fallback}."""
    if not isinstance(v, list):
        return False
    for e in v:
        if isinstance(e, str):
            continue
        if not isinstance(e, dict) or set(e) - _cc.REGISTRY_ENTRY_KEYS or not isinstance(e.get("registry"), str):
            return False
        if e.get("mirrors") is not None and not (isinstance(e["mirrors"], list)
                                                 and all(isinstance(m, str) for m in e["mirrors"])):
            return False
        if e.get("no_fallback") not in (None, True, False):
            return False
    return True


def render_registries(entries, values, where):
    """Liste de registres rendue avec les valeurs du lancement, puis
    normalisée (capi_cluster.normalize_registry_entries) ; ProfileError."""
    def sub(v):
        if isinstance(v, str):
            missing = [n for n in _bmp.VAR_RE.findall(v) if n not in (values or {})]
            if missing:
                raise ProfileError([(where, f"missing variable {missing[0]}")], "missing variable")
            return _bmp.VAR_RE.sub(lambda m: str(values[m.group(1)]), v)
        if isinstance(v, list):
            return [sub(x) for x in v]
        if isinstance(v, dict):
            return {k: sub(x) for k, x in v.items()}
        return v
    rendered = sub(entries)
    try:
        return _cc.normalize_registry_entries(rendered)
    except ValueError as e:
        raise ProfileError([(where, str(e))], "invalid registries") from None


def check_capi_template(doc):
    """Modèle de cluster normalisé {description, variables, spec}, ou
    ProfileError. `spec` : la demande du formulaire de création (liste
    blanche de capi_cluster, moins nom et namespaces) ; les valeurs sans
    variable passent par capi_cluster.validate sur une demande d'essai, pour
    refuser tôt ce qui ne passerait jamais."""
    if not isinstance(doc, dict):
        raise ProfileError([("template", "not a mapping")], "invalid cluster template")
    errors = []
    name = doc.get("name")
    if name is not None and (not isinstance(name, str) or not NAME_RE.match(name)):
        errors.append(("name", "invalid name (RFC 1123)"))
    unknown = sorted(set(doc) - {"name", "revision", "description", "variables", "spec", "updated_by"})
    errors.extend((k, "unknown key") for k in unknown)
    desc = _check_description(doc, errors)
    variables = _check_variables(doc, errors)
    spec = doc.get("spec")
    spec = {} if spec is None else spec
    if not isinstance(spec, dict):
        errors.append(("spec", "not a mapping"))
        spec = {}
    clean = {}
    for k, v in spec.items():
        if k in CAPI_TEMPLATE_FORBIDDEN:
            errors.append((f"spec.{k}", "set by the plan, not by the template"))
        elif k not in _cc.KNOWN:
            errors.append((f"spec.{k}", "unknown key"))
        elif k == "registries" and v not in (None, "", []):
            if registries_shape_ok(v):
                clean[k] = v
            else:
                errors.append((f"spec.{k}", "a list of registry names or {registry, mirrors, no_fallback}"))
        elif k == "oidc" and v not in (None, "", {}):
            # 2.8.0 : réglages OIDC de l'API (child_oidc), des chaînes
            # seulement, variables admises ; contrôlés plus bas une fois fixes
            import child_oidc as _co
            if not isinstance(v, dict) or not all(isinstance(x, str) for x in v.values()):
                errors.append((f"spec.{k}", "an object of strings (issuer_url, audience, ...)"))
            elif set(v) - set(_co.KEYS):
                errors.append((f"spec.{k}.{sorted(set(v) - set(_co.KEYS))[0]}", "unknown key"))
            else:
                clean[k] = v
        elif not (isinstance(v, (str, int, bool)) or v is None
                  or isinstance(v, list) and all(isinstance(x, str) for x in v)):
            errors.append((f"spec.{k}", "a string, a number, a boolean or a list of strings"))
        elif v not in (None, "", []):
            clean[k] = v
    known = set(BUILTIN_VARS) | set(variables)
    for var in sorted(_bmp.variables_in(clean)):
        if var not in known:
            errors.append((f"{{{{{var}}}}}", "undeclared variable"))
    errors.extend(_capi_url_refusals(clean, "spec"))
    fixed = {k: v for k, v in clean.items() if not _bmp.variables_in(v)}
    trial = dict(fixed, name="trial", namespace="trial", target_namespace="trial")
    try:
        for k, why in _cc.validate(_cc.normalize(trial)):
            if k in fixed:
                errors.append((f"spec.{k}", why))
    except ValueError as e:                    # entier illisible (`cpu: deux`)
        errors.append(("spec", str(e)))
    if "oidc" in fixed:
        import child_oidc as _co
        version = fixed.get("k8s_version")
        for f in _co.blocking(_co.check(fixed["oidc"], version or "v1.34.0")[1]):
            if f["code"] != "oidc-version-too-old" or version:
                errors.append(("spec.oidc", f["code"]))
    if fixed.get("rancher_import") and fixed.get("rancher_external"):
        errors.append(("spec.rancher_external", "rancher-import-conflict: the embedded Rancher import "
                                                "(rancher_import) and an external Rancher exclude each other"))
    if errors:
        raise ProfileError(errors, "invalid cluster template")
    return {"description": desc, "variables": variables, "spec": clean}


def save_capi_template(state, name, doc, revision=None, by=""):
    """Modèle de cluster écrit (0600, remplacement atomique) à la révision
    lue ; PlanConflict, ProfileError. `by` : qui écrit cette révision."""
    def check(d):
        return dict(check_capi_template(d), updated_by=str(by or "")[:128])
    return _save(state, CAPI_TEMPLATE_DIR, name, doc, revision, check)


def load_capi_template(state, name):
    t = _load(state, CAPI_TEMPLATE_DIR, name, check_capi_template)
    if t is not None:
        by = (_read(capi_template_path(state, name)) or {}).get("updated_by")
        t["updated_by"] = by if isinstance(by, str) else ""
    return t


def list_capi_templates(state):
    return _list(state, CAPI_TEMPLATE_DIR, check_capi_template, lambda t: {
        "name": t["name"], "revision": t["revision"], "description": t["description"],
        "variables": t["variables"], "uses": sorted(_bmp.variables_in(t["spec"])),
        "vms": int(t["spec"].get("cp_replicas", _cc.DEFAULTS["cp_replicas"]) or 0)
        + int(t["spec"].get("worker_replicas", _cc.DEFAULTS["worker_replicas"]) or 0)
        if not _bmp.variables_in([t["spec"].get("cp_replicas"), t["spec"].get("worker_replicas")]) else None})


def delete_capi_template(state, name):
    return _delete(state, CAPI_TEMPLATE_DIR, name)


def plans_using_capi_template(state, name):
    """Plans enregistrés qui désignent ce modèle de cluster (phase clusters,
    entrées ou raccourci `template`), activée ou non."""
    out = []
    d = Path(state) / PLAN_DIR
    try:
        files = sorted(p for p in d.glob("*.yaml") if p.is_file())
    except OSError:
        return out
    for p in files:
        doc = _read(p) or {}
        ph = ((doc.get("phases") or {}) if isinstance(doc.get("phases"), dict) else {}).get("clusters")
        if not isinstance(ph, dict):
            continue
        items = ph.get("items") if isinstance(ph.get("items"), list) else []
        names = {i.get("template") for i in items if isinstance(i, dict)} | {ph.get("template")}
        if name in names:
            out.append(p.stem)
    return out


def render_capi_template(t, values, name, where=None):
    """Demande de création rendue pour le cluster `name` : variables posées
    par bm_profiles.render_advanced (jetons neutres, une valeur à syntaxe
    YAML arrive telle quelle), entiers pour les options entières, puis nom,
    namespace et namespace des VMs = `name` (décision 164), puis
    capi_cluster.normalize. ProfileError : variable absente (avec son
    chemin), valeur invalide, nom invalide, identifiants dans une adresse."""
    where = where or f"clusters[{name}].template[{t.get('name')}]"
    errors = []
    for k, v in sorted((values or {}).items()):
        why = _bmp.check_value(k, v)
        if why:
            errors.append((f"variables.{k}", why))
    if errors:
        raise ProfileError(errors, "invalid values")
    spec = t.get("spec") or {}
    text = yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=1 << 20)
    ints = set(_cc.INT_OPTIONS)
    try:
        out = _bmp.render_advanced(text, values or {}, lambda p: "int" if p in ints else None) or {}
    except KeyError as e:
        raise ProfileError([(_var_path(spec, e.args[0], f"{where}.spec") or f"{where}.spec",
                             "missing variable")], "cannot render the cluster template") from None
    if not isinstance(name, str) or not _cc.NAME_RE.match(name):
        errors.append((f"{where}.name", "lowercase letters, digits and '-', starting with a letter, "
                                        "1 to 40 characters"))
    errors.extend(_capi_url_refusals(out, f"{where}.spec"))
    if errors:
        raise ProfileError(errors, "cannot render the cluster template")
    # namespace des VMs : imposé par harvester-capi (identité restreinte,
    # capi_identity.force_target_namespace), jamais par le modèle
    out = dict(out, name=name, namespace=name)
    out.pop("target_namespace", None)
    try:
        return _cc.normalize(out)
    except ValueError as e:
        raise ProfileError([(f"{where}.spec", str(e))], "cannot render the cluster template") from None
