"""harvester-ops : registres déclarés et configuration RKE2 des clusters enfants (v2.6.0).

Plusieurs registres (précision de 119, point 187) : la console garde leurs
déclarations, sans secret, dans `<état>/registries.d/<nom>.yaml` (révision,
409 sur révision périmée, comme les modèles) ; les identifiants ne vivent
que dans des Secrets Kubernetes, sur chaque cluster Harvester qui emploie le
registre (`harvester-ops-registry/<registre>-push`, `-pull`, `-ca`).

Un cluster enfant désigne une liste ordonnée de registres : pour chaque
registre d'origine miroité (`docker.io`...), ceux qui le servent sont
essayés dans l'ordre (le premier est préféré). Seul le compte de lecture
va dans les clusters enfants (point 188) : il finit dans un fichier de
chaque nœud.

Rangement (point 186) : `<registre>/<hôte d'origine>/<chemin>`, d'où le
`rewrite` `^(.*)$` -> `<hôte d'origine>/$1` de chaque miroir.

Plan : docs/design/2026-11-05-artefacts-registre-260-plan.md (tâche 6).
"""

import base64
import json
import re
import ssl

import capi_cluster as cc
import deploy_plans as dp

DIR = "registries.d"
NAMESPACE = "harvester-ops-registry"
LABEL = "harvester-ops.io/registry"
MANAGED = "harvester-ops.io/managed"
KINDS = ("external", "bootstrap")
SOFTWARE = ("distribution",)
USAGES = ("images", "charts")
NAME_MAX = 30                       # <cluster>-registry-<registre>-auth tient dans 253
HOST_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
                     r"|\d{1,3}(?:\.\d{1,3}){3})(?::\d{1,5})?$")
# revue m-3 : `:` et espaces interdits dans le nom seulement (htpasswd,
# auth de config.json) ; un mot de passe sans fin de ligne ni NUL ; fullmatch
USER_RE = re.compile(r"[^\s:]{1,256}")
PASSWORD_RE = re.compile(r"[^\r\n\x00]{1,256}")
KEYS = {"name", "revision", "kind", "software", "endpoint", "push_endpoint", "ca_pem", "usage", "mirrors",
        "clusters", "insecure_skip_verify", "published", "updated_by"}
BOOTSTRAP_KEYS = {"cluster", "address", "size_gib", "storage_class", "dns_name"}
NO_FALLBACK_FILE = "/etc/rancher/rke2/config.yaml.d/50-harvester-ops-registry.yaml"


def _err(errors, where, why):
    errors.append((where, why))


def _host_list(v, where, errors):
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) and HOST_RE.fullmatch(x) for x in v):
        _err(errors, where, "a list of registry hosts (host[:port], no scheme)")
        return []
    return list(dict.fromkeys(v))


def ca_readable(pem):
    try:
        ssl.create_default_context(cadata=pem)
        return True
    except (ssl.SSLError, ValueError):
        return False


def check_decl(doc):
    """Déclaration normalisée, ou ProfileError [(où, raison)]."""
    errors = []
    if not isinstance(doc, dict):
        raise dp.ProfileError([("", "not an object")])
    kind = doc.get("kind")
    allowed = KEYS | (BOOTSTRAP_KEYS if kind == "bootstrap" else set())
    for k in sorted(set(doc) - allowed):
        _err(errors, k, "unknown key" if k not in BOOTSTRAP_KEYS else "only for a bootstrap registry")
    name = doc.get("name")
    if isinstance(name, str) and len(name) > NAME_MAX:
        _err(errors, "name", f"{NAME_MAX} characters at most")
    if kind not in KINDS:
        _err(errors, "kind", "external or bootstrap")
    out = {"kind": kind}
    if kind == "bootstrap":
        out["software"] = doc.get("software", "distribution")
        if out["software"] not in SOFTWARE:
            _err(errors, "software", "distribution")
    elif "software" in doc:
        _err(errors, "software", "only for a bootstrap registry")
    for key in ("endpoint", "push_endpoint"):
        v = doc.get(key)
        if v is None and key == "push_endpoint":
            continue
        if not isinstance(v, str) or not HOST_RE.fullmatch(v):
            _err(errors, key, "host[:port], without scheme nor path")
        else:
            out[key] = v
    ca = doc.get("ca_pem")
    if ca not in (None, ""):
        if not isinstance(ca, str) or "PRIVATE KEY" in ca or not ca_readable(ca):
            _err(errors, "ca_pem", "an unreadable CA certificate (PEM)")
        else:
            out["ca_pem"] = ca
    usage = doc.get("usage", list(USAGES))
    if not isinstance(usage, list) or not all(u in USAGES for u in usage):
        _err(errors, "usage", "a list of images, charts")
        usage = []
    out["usage"] = [u for u in USAGES if u in usage]
    out["mirrors"] = _host_list(doc.get("mirrors"), "mirrors", errors)
    clusters = doc.get("clusters") or []
    if not isinstance(clusters, list) or not all(isinstance(c, str) and dp._bmp.CLUSTER_NAME_RE.match(c)
                                                 for c in clusters):
        _err(errors, "clusters", "a list of Harvester cluster names")
        clusters = []
    out["clusters"] = list(dict.fromkeys(clusters))
    if not isinstance(doc.get("insecure_skip_verify", False), bool):
        _err(errors, "insecure_skip_verify", "true or false")
    elif doc.get("insecure_skip_verify"):
        out["insecure_skip_verify"] = True
    pub = doc.get("published") or []
    if not isinstance(pub, list) or not all(isinstance(p, dict) and set(p) <= {"package", "version", "at", "by"}
                                            for p in pub):
        _err(errors, "published", "a list of {package, version, at}")
    elif pub:
        out["published"] = pub
    if isinstance(doc.get("updated_by"), str):
        out["updated_by"] = doc["updated_by"]
    if kind == "bootstrap":
        for k in ("cluster", "address", "storage_class"):
            if not isinstance(doc.get(k), str) or not doc[k]:
                _err(errors, k, "required for a bootstrap registry")
            else:
                out[k] = doc[k]
        size = doc.get("size_gib")
        if not isinstance(size, int) or isinstance(size, bool) or not 5 <= size <= 2048:
            _err(errors, "size_gib", "an integer from 5 to 2048")
        else:
            out["size_gib"] = size
        if doc.get("dns_name"):
            out["dns_name"] = doc["dns_name"]
    if errors:
        raise dp.ProfileError(errors, "invalid registry declaration")
    return out


# /api/registries/bootstrap/<cluster> est le geste d'amorçage ;
# /api/registries/bootstrap/<cluster>/leftovers, les restes (revue finale I-2)
RESERVED = ("bootstrap", "leftovers", "settings")
WRITE_NAME_SUFFIX = "-write"         # Service d'écriture d'un registre d'amorçage (2.6.1)


# ---------------------------------------------------------------------------
# Réglages des registres (2.6.1) : adresses de la console pour le port
# d'écriture des registres d'amorçage
# ---------------------------------------------------------------------------
SETTINGS_FILE = "registries-settings.json"
SOURCES_MAX = 16


def normalize_sources(value):
    """Liste triée de réseaux IPv4 `a.b.c.d/n` (une adresse seule devient
    /32) depuis une liste ou un texte séparé par des virgules ou des
    espaces ; ValueError sinon. 0.0.0.0/0 refusé : c'est l'absence de
    restriction, qui s'écrit par une liste vide."""
    import ipaddress
    if value is None:
        return []
    items = value if isinstance(value, list) else re.split(r"[\s,]+", str(value))
    out = []
    for raw in items:
        if not isinstance(raw, str):
            raise ValueError("write sources: IPv4 addresses or networks")
        v = raw.strip()
        if not v:
            continue
        try:
            net = ipaddress.IPv4Network(v, strict=False)
        except ValueError:
            raise ValueError(f"{v[:60]}: an IPv4 address or network (a.b.c.d/n)") from None
        if net.prefixlen == 0:
            raise ValueError("0.0.0.0/0 opens the port to every address: leave the list empty instead")
        if str(net) not in out:
            out.append(str(net))
    if len(out) > SOURCES_MAX:
        raise ValueError(f"{SOURCES_MAX} networks at most")
    return sorted(out, key=lambda n: (int(ipaddress.IPv4Network(n).network_address), n))


class SettingsError(ValueError):
    """Fichier de réglages illisible : jamais lu comme « ouvert à tous »."""


def settings(state):
    """{"write_sources": [...]} : lu à chaque usage (modifiable à chaud).
    Fichier absent : aucune restriction. Fichier abîmé : SettingsError
    (revue 2.6.1 : le lire comme vide ouvrirait le port d'écriture)."""
    from pathlib import Path
    path = Path(state) / SETTINGS_FILE
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {"write_sources": []}
    except OSError as e:
        raise SettingsError(f"{SETTINGS_FILE}: unreadable ({e.strerror})") from None
    try:
        doc = json.loads(text)
        return {"write_sources": normalize_sources(doc.get("write_sources") or [])}
    except (ValueError, AttributeError) as e:
        raise SettingsError(f"{SETTINGS_FILE}: invalid ({str(e)[:120]}); save the setting again") from None


def save_settings(state, sources):
    from pathlib import Path
    import self_update as su
    doc = {"write_sources": normalize_sources(sources)}
    su.write_json_atomic(Path(state) / SETTINGS_FILE, doc, mode=0o600)
    return doc


def save(state, name, doc, revision=None):
    if name in RESERVED:
        raise dp.ProfileError([("name", "reserved name")])
    if name.endswith(WRITE_NAME_SUFFIX) and (doc or {}).get("kind") == "bootstrap":
        # revue 2.6.1 : `<a>-write` est le Service d'écriture du registre `a`
        raise dp.ProfileError([("name", f"a bootstrap registry name cannot end with {WRITE_NAME_SUFFIX}")])
    return dp._save(state, DIR, name, doc, revision, check_decl)


def load(state, name):
    return dp._load(state, DIR, name, check_decl)


def _summary(d):
    return {k: d.get(k) for k in ("name", "revision", "kind", "endpoint", "usage", "mirrors", "clusters",
                                  "cluster", "address")} | {"published": len(d.get("published") or [])}


def list_decls(state):
    return dp._list(state, DIR, check_decl, _summary)


def delete(state, name):
    return dp._delete(state, DIR, name)


def forget(state, name, used_by=()):
    """Retire la déclaration d'un registre du client ; refusé si un plan ou
    un cluster enfant le désigne (`used_by`, noms calculés par l'appelant)
    et pour un registre d'amorçage (geste `delete`, qui retire aussi le
    registre du cluster)."""
    d = load(state, name)
    if d is None:
        raise KeyError(name)
    if d["kind"] == "bootstrap":
        raise ValueError("a bootstrap registry is removed with delete, not forget")
    used_by = sorted(set(used_by or ()))
    if used_by:
        raise ValueError(f"registry-in-use: {', '.join(used_by)}")
    return delete(state, name)


def detach(kube, name, on_gone=None):
    """Retire les Secrets d'un registre du client d'un cluster Harvester
    (comptes d'écriture et de lecture, CA) ; un Secret déjà absent compte
    comme retiré. Rend les noms retirés ; KubeError sur un refus (droits).
    `on_gone(nom)` à chaque Secret retiré, avant le suivant : un refus en
    cours de route laisse la trace de ce qui est déjà parti."""
    from kube import KubeError
    gone = []
    for sname in secret_names(name).values():
        try:
            kube.delete("secrets", NAMESPACE, sname)
        except KubeError as e:
            if "(NotFound)" not in str(e):
                raise
            continue
        gone.append(sname)
        if on_gone:
            on_gone(sname)
    return gone


def update(state, name, change):
    """Relit, modifie (`change(décl)`) et réécrit à la révision lue."""
    d = load(state, name)
    if d is None:
        raise KeyError(name)
    rev = d.pop("revision")
    d.pop("name")
    change(d)
    return save(state, name, d, rev)


# ---------------------------------------------------------------------------
# Rattachement à un cluster Harvester (Secrets)
# ---------------------------------------------------------------------------
def secret_names(name):
    return {"push": f"{name}-push", "pull": f"{name}-pull", "ca": f"{name}-ca"}


def check_creds(creds):
    """{push, pull} normalisés ; `pull` absent = compte d'écriture repris
    (avertissement registry-pull-is-push). ValueError avec la raison, jamais
    la valeur."""
    if not isinstance(creds, dict) or set(creds) - {"push", "pull"}:
        raise ValueError("credentials: {push: {username, password}, pull: {username, password}}")
    out = {}
    for role in ("push", "pull"):
        c = creds.get(role)
        if c is None and role == "pull":
            continue
        if not isinstance(c, dict) or set(c) != {"username", "password"} or not (
                isinstance(c["username"], str) and USER_RE.fullmatch(c["username"])
                and isinstance(c["password"], str) and PASSWORD_RE.fullmatch(c["password"])):
            raise ValueError(f"credentials.{role}: username and password, no spaces nor ':' in the username")
        out[role] = {"username": c["username"], "password": c["password"]}
    out.setdefault("pull", dict(out["push"]))
    return out


def attach_manifests(decl, creds):
    """Namespace et Secrets d'un registre sur un cluster Harvester."""
    names = secret_names(decl["name"])
    labels = {LABEL: decl["name"]}
    docs = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE, "labels": {MANAGED: "true"}}}]
    for role in ("push", "pull"):
        docs.append({"apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/basic-auth",
                     "metadata": {"name": names[role], "namespace": NAMESPACE, "labels": labels},
                     "stringData": dict(creds[role])})
    if decl.get("ca_pem"):
        docs.append({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                     "metadata": {"name": names["ca"], "namespace": NAMESPACE, "labels": labels},
                     "stringData": {"ca.crt": decl["ca_pem"]}})
    return docs


def attach(kube, decl, creds):
    """Pose les Secrets (application côté serveur, corps sur l'entrée
    standard de kubectl) ; rend les noms posés et les avertissements."""
    if decl.get("kind") == "bootstrap":
        # revue I-3 : ses comptes sont générés par la console (htpasswd du
        # pod) ; des valeurs tapées les rendraient inutilisables
        raise ValueError("registry-bootstrap: the accounts of a bootstrap registry are generated by the console")
    creds = check_creds(creds)
    docs = attach_manifests(decl, creds)
    kube.apply(docs)
    findings = []
    if creds["pull"]["username"] == creds["push"]["username"]:
        findings.append(cc.finding("registry-pull-is-push", "warn", registry=decl["name"]))
    return {"secrets": [d["metadata"]["name"] for d in docs[1:]], "findings": findings}


def read_creds(kube, name, role):
    """{username, password} du Secret `<registre>-<rôle>`, ou None."""
    s = kube.get("secrets", NAMESPACE, secret_names(name)[role])
    if not s:
        return None
    data = s.get("data") or {}
    try:
        return {k: base64.b64decode(data.get(k) or "").decode() for k in ("username", "password")}
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Constats
# ---------------------------------------------------------------------------
VM_NETWORK_WARNING = "registry-vm-network-unchecked"
CHILD_VM_NETWORK_WARNING = "cluster-registry-vm-network-unchecked"
# 2.6.1 (point 192) : sonde par une pod du réseau des VMs (bin/lib/vm_probe.py)
VM_NETWORK_OK = "registry-vm-network-ok"
VM_NETWORK_UNREACHABLE = "registry-unreachable-from-vm-network"
CHILD_VM_NETWORK_OK = "cluster-registry-vm-network-ok"
CHILD_VM_NETWORK_UNREACHABLE = "cluster-registry-unreachable-from-vm-network"
VM_NETWORK_CODES = (VM_NETWORK_WARNING, VM_NETWORK_OK, VM_NETWORK_UNREACHABLE)
CHILD_VM_NETWORK_CODES = (CHILD_VM_NETWORK_WARNING, CHILD_VM_NETWORK_OK, CHILD_VM_NETWORK_UNREACHABLE)


def vm_network_findings(registry, endpoint, probe, child=False, block=False):
    """Constat de joignabilité depuis le réseau des VMs d'un point d'accès,
    d'après le résultat de `vm_probe.run` (None : aucune sonde faite, aucun
    réseau donné). `block` : le cluster enfant coupe le repli vers Internet,
    un registre injoignable l'empêche alors de tirer ses images."""
    codes = CHILD_VM_NETWORK_CODES if child else VM_NETWORK_CODES
    base = {"registry": registry, "endpoint": endpoint}
    if not probe or probe.get("status") != "probed":
        probe = probe or {"reason": "no-network"}
        return [cc.finding(codes[0], "warn", **base, network=probe.get("network") or "",
                           reason=probe.get("reason") or "no-network", detail=probe.get("detail") or "")]
    r = (probe.get("results") or {}).get(endpoint) or {"ok": False, "failed": "tcp"}
    if r.get("unknown"):
        # adresse de l'IPAM sans passerelle ni DNS : la sonde ne juge pas
        return [cc.finding(codes[0], "warn", **base, network=probe["network"], reason="ipam-incomplete",
                           detail="")]
    facts = dict(base, network=probe["network"], address=probe.get("address") or "")
    if r.get("ok"):
        return [cc.finding(codes[1], "ok", **facts)]
    return [cc.finding(codes[2], "block" if block else "warn", **facts, failed=r.get("failed") or "tcp")]


def check(decl, facts, name=None):
    """Constats d'un registre sur un cluster Harvester. `facts` :
    {"attached": bool, "reachable": bool, "ca_ok": bool,
     "auth": {"pull": bool|None, "push": bool|None}, "pull_is_push": bool,
     "vm_probe": résultat de vm_probe.run (2.6.1, réseau choisi) ou absent,
     "write_open": bool (2.6.1, port d'écriture ouvert à toute adresse)}."""
    if decl is None:
        return [cc.finding("registry-unknown", "block", registry=name)]
    n = decl["name"]
    out = []
    if not decl.get("usage"):
        out.append(cc.finding("registry-no-usage", "block", registry=n))
    if decl.get("insecure_skip_verify"):
        out.append(cc.finding("registry-insecure", "warn", registry=n))
    if facts.get("attached") is False:
        out.append(cc.finding("registry-not-attached", "block", registry=n))
        return out
    if facts.get("ca_ok") is False:
        out.append(cc.finding("registry-ca-invalid", "block", registry=n))
    elif facts.get("reachable") is False:
        out.append(cc.finding("registry-unreachable", "block", registry=n, endpoint=decl.get("endpoint")))
    else:
        refused = [r for r in ("pull", "push") if (facts.get("auth") or {}).get(r) is False]
        if refused:
            out.append(cc.finding("registry-auth-refused", "block", registry=n, accounts=refused))
    if facts.get("pull_is_push"):
        out.append(cc.finding("registry-pull-is-push", "warn", registry=n))
    if facts.get("write_open"):
        out.append(cc.finding("registry-write-port-open", "warn", registry=n, endpoint=decl.get("push_endpoint")))
    # 2.6.1 (point 192) : vu du réseau des VMs choisi, sinon dit non vérifié
    out += vm_network_findings(n, decl.get("endpoint"), facts.get("vm_probe"))
    return out


def probe(decl, creds, role, registry_cls=None):
    """(joignable, CA juste, compte accepté) d'un point d'accès : GET /v2/
    avec le compte, sans rien écrire."""
    import oci_push as op
    cls = registry_cls or op.Registry
    endpoint = decl.get("push_endpoint") if role == "push" and decl.get("push_endpoint") else decl["endpoint"]
    try:
        reg = cls(endpoint, creds, ca_pem=decl.get("ca_pem"), timeout=15)
        code, _, _ = reg.request("GET", "/v2/", "")
        return True, True, code == 200
    except op.RegistryError as e:
        text = str(e)
        if "authentication" in text:
            return True, True, False
        if "CERTIFICATE" in text.upper() or "unreadable registry CA" in text:
            return True, False, None
        return False, None, None


def facts(kube, decl, cluster, probe_fn=probe):
    """Ce que `check` lit : Secrets posés sur le cluster, puis sondes de
    lecture et d'écriture depuis la console."""
    if decl is None:
        return {}
    pull = read_creds(kube, decl["name"], "pull")
    push = read_creds(kube, decl["name"], "push")
    if cluster not in (decl.get("clusters") or []) or not pull or not push:
        return {"attached": False}
    out = {"attached": True, "auth": {}, "pull_is_push": pull["username"] == push["username"]}
    for role, creds in (("pull", pull), ("push", push)):
        reach, ca_ok, ok = probe_fn(decl, creds, role)
        if not reach:
            out["reachable"] = False
            break
        if ca_ok is False:
            out["ca_ok"] = False
            break
        out["auth"][role] = ok
    return out


# ---------------------------------------------------------------------------
# Clusters enfants : configuration de registres privés de RKE2
# ---------------------------------------------------------------------------
def normalize_entries(entries):
    """Liste ordonnée [{registry[, mirrors][, no_fallback]}] (règles de
    capi_cluster.normalize_registry_entries) ; ValueError sinon."""
    return cc.normalize_registry_entries(entries)


# Point 198 : les images système de RKE2 viennent de ces registres
# d'origine. Couper le repli vers Internet pour l'un d'eux, sans ces images
# dans un registre qui le miroite, empêche les nœuds de démarrer (essais
# préalables de la 2.6.0 : le cluster a monté en tirant ses images système
# de docker.io). Et RKE2 coupe le repli pour TOUS les registres miroités à
# la fois.
SYSTEM_ORIGINS = ("docker.io", "registry.rancher.com")
SYSTEM_IMAGE = "rancher/rke2-runtime"


def _entry_origins(e, d):
    """Registres d'origine qu'une entrée fait servir par son registre : ceux
    de l'entrée s'ils sont donnés (pris parmi ceux de la déclaration), sinon
    tous ceux de la déclaration."""
    declared = list(d.get("mirrors") or [])
    if e.get("mirrors") is None:
        return declared
    return [m for m in e["mirrors"] if m in declared]


def fallback_asked(entries):
    """Repli coupé si une entrée le demande (`no_fallback: true`) : une seule
    décision par cluster, RKE2 coupant le repli pour tous les registres
    miroités. Jamais par défaut (point 198, revue I-4)."""
    return any(e.get("no_fallback") is True for e in normalize_entries(entries))


def fallback_findings(no_fallback, entries, decls, system_ready=()):
    """Refus `cluster-registry-no-fallback-system` (bloquant) quand le repli
    est coupé alors qu'un registre d'origine des images système de RKE2 est
    miroité sans qu'un registre qui le sert porte ces images
    (`system_ready` : noms des registres qui les portent)."""
    if not no_fallback:
        return []
    served = {}
    for e in normalize_entries(entries):
        d = decls.get(e["registry"]) or {}
        if "images" in (d.get("usage") or []):
            for o in _entry_origins(e, d):
                served.setdefault(o, []).append(e["registry"])
    missing = sorted(o for o in SYSTEM_ORIGINS if o in served
                     and not set(served[o]) & set(system_ready or ()))
    return [cc.finding("cluster-registry-no-fallback-system", "block", origins=", ".join(missing))] if missing else []


def child_probe_endpoints(entries, decls):
    """Points d'accès que les VMs d'un cluster enfant joignent : ceux des
    registres connus qui servent des images (les charts sont tirés par
    CAAPH, sur le cluster Harvester)."""
    out = []
    for e in normalize_entries(entries):
        d = decls.get(e["registry"])
        if d and "images" in (d.get("usage") or []) and d.get("endpoint") not in out:
            out.append(d.get("endpoint"))
    return out


def check_child(entries, decls, cluster, attached, system_ready=(), vm_probe=None):
    """Constats de la liste de registres d'un cluster enfant (tâche 9) :
    `decls` {nom: déclaration} (absent = inconnu), `cluster` le cluster
    Harvester, `attached` {nom: bool} (compte de lecture posé sur ce
    cluster), `system_ready` les registres qui portent les images système
    de RKE2, `vm_probe` le résultat de vm_probe.run depuis le réseau des
    VMs du cluster enfant (2.6.1 ; None : non vérifié)."""
    out = []
    seen = set()
    for e in normalize_entries(entries):
        n = e["registry"]
        if n in seen:
            continue                     # refusé par capi_cluster.validate (`invalid`)
        seen.add(n)
        d = decls.get(n)
        if d is None:
            out.append(cc.finding("cluster-registry-unknown", "block", registry=n))
            continue
        if cluster not in (d.get("clusters") or []) or not attached.get(n):
            out.append(cc.finding("cluster-registry-not-attached", "block", registry=n, cluster=cluster))
        if "images" not in (d.get("usage") or []):
            out.append(cc.finding("cluster-registry-no-images", "warn", registry=n))
        bad = [m for m in e.get("mirrors") or [] if m not in (d.get("mirrors") or [])]
        if bad:
            out.append(cc.finding("cluster-registry-mirror-unknown", "block", registry=n, mirrors=", ".join(bad)))
    known = [e for e in normalize_entries(entries) if e["registry"] in decls]
    cut = fallback_asked(known)
    out += fallback_findings(cut, known, decls, system_ready)
    # 2.6.1 (point 192) : joignabilité vue du réseau des VMs du cluster
    # enfant ; bloquante quand le repli vers Internet est coupé
    for e in known:
        if "images" in (decls[e["registry"]].get("usage") or []):
            out += vm_network_findings(e["registry"], decls[e["registry"]].get("endpoint"), vm_probe,
                                       child=True, block=cut)
    return out


def system_ready_registries(state, decls):
    """Registres dont un paquet publié porte les images système de RKE2
    (`<origine>/rancher/rke2-runtime`), relevé dans les paquets rangés.

    Heuristique (revue finale 2.6.0, m-12) : une seule image cherchée, sans
    contrôle de la version de RKE2 ni des autres images système. Un registre
    retenu ici peut donc encore manquer d'une image que les nœuds tirent ;
    `no_fallback` permis sur cette base reste un pari éclairé, pas une
    preuve."""
    import artefacts as art
    out = set()
    for n, d in decls.items():
        for p in (d or {}).get("published") or []:
            try:
                idx = art.load_package(state, p.get("package"), p.get("version"))["index"]
            except (art.ArtefactError, TypeError):
                continue
            refs = [r if isinstance(r, str) else (r or {}).get("as") or (r or {}).get("ref") or ""
                    for it in idx.get("items") or [] for r in it.get("images") or []]
            if any(SYSTEM_IMAGE in str(r) for r in refs):
                out.add(n)
                break
    return out


def child_users(kube, name):
    """Clusters enfants (namespaces) qui emploient ce registre : Secrets
    étiquetés à son nom hors de son propre namespace (compte de lecture
    d'un cluster enfant, identifiants d'un service)."""
    found = kube.list("secrets", None, selector=f"{LABEL}={name},{cc.GENERATED}=true")
    return sorted({(s.get("metadata") or {}).get("namespace") for s in found
                   if (s.get("metadata") or {}).get("namespace") not in (None, NAMESPACE)})


def pull_creds_for(kube, names):
    """{registre: compte de lecture} lus sur le cluster Harvester, pour les
    Secrets des clusters enfants (point 188 : jamais le compte d'écriture).
    KeyError si un registre n'a pas de compte de lecture posé."""
    out = {}
    for n in names:
        c = read_creds(kube, n, "pull")
        if not c:
            raise KeyError(n)
        out[n] = c
    return out


def _origin_repo(host):
    """Préfixe de rangement d'un registre d'origine (oci_layout.target_repo)."""
    return host.replace(":", "_")


def child_secret_names(cluster_name, registry):
    return {"auth": f"{cluster_name}-registry-{registry}-auth", "ca": f"{cluster_name}-registry-{registry}-ca"}


REGISTRIES_FILE = "/etc/rancher/rke2/registries.yaml"
CA_DIR = "/etc/rancher/rke2/tls/harvester-ops"


def nodes_secret_name(cluster_name):
    return f"{cluster_name}-registries"


def rke2_config(entries, decls, cluster, pull_creds, no_fallback=None, nodes_secret=None):
    """(registries.yaml, files, secrets) d'un cluster enfant (pur).

    `entries` : liste ordonnée de registres (noms ou {registry}) ;
    `decls` : {nom: déclaration} ; `cluster` : {"name", "namespace"} ;
    `pull_creds` : {nom: compte de lecture}, lu dans les Secrets `-pull`
    du cluster Harvester (jamais le compte d'écriture).
    `no_fallback` : coupe le repli vers le point d'accès d'origine pour les
    registres miroités (fichier config.yaml.d de RKE2) ; par défaut, ce que
    demandent les entrées (`fallback_asked`), jamais coupé sinon (point 198 ;
    `fallback_findings`, rejoué par `check_child`, refuse la demande avant).

    Les nœuds reçoivent `registries.yaml` et une CA par registre par des
    `files` tirés du Secret `<cluster>-registries`, pas par
    `privateRegistriesConfig` : CAPRKE2 v0.25.2 (`pkg/rke2/registries.go`,
    encore sur main) recrée la table `Configs` à chaque registre et écrit
    toute CA dans le même `/etc/rancher/rke2/tls/ca.crt`, si bien qu'avec
    deux registres un seul gardait son compte et sa CA (vu sur harvlab2).

    Secrets : `<cluster>-registries` (`registries.yaml`, `ca-<registre>.crt`)
    pour les nœuds ; `<cluster>-registry-<registre>-auth` (`username`,
    `password`) et `-ca` (`Opaque`, `ca.crt` seul), étiquetés au nom du
    registre : ils disent quels clusters l'emploient (`child_users`) et la
    CA sert aux services tirés du registre (CAAPH). Tous étiquetés
    `GENERATED` pour partir avec le cluster ; `insecure_skip_verify` jamais
    posé sauf demande explicite (vérification active).

    2.8.0 : `nodes_secret` nomme le Secret des nœuds d'un renouvellement
    (`<cluster>-registries-r<N>`, capi_renew) : les nouvelles machines
    lisent le nouveau, les anciennes gardent le leur jusqu'à leur départ."""
    ents = normalize_entries(entries)
    used = {}
    for e in ents:
        if "images" in (decls[e["registry"]].get("usage") or []):
            used.setdefault(e["registry"], e)
    mirrors, configs, secrets, cas = {}, {}, [], {}
    ns, cname = cluster["namespace"], cluster["name"]
    for name, e in used.items():
        d = decls[name]
        labels = {cc.GENERATED: "true", LABEL: name}
        for origin in _entry_origins(e, d):
            m = mirrors.setdefault(origin, {"endpoint": [], "rewrite": {"^(.*)$": f"{_origin_repo(origin)}/$1"}})
            ep = f"https://{d['endpoint']}"
            if ep not in m["endpoint"]:
                m["endpoint"].append(ep)
        sn = child_secret_names(cname, name)
        creds = pull_creds[name]
        conf = {"auth": {"username": creds["username"], "password": creds["password"]}}
        secrets.append({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                        "metadata": {"name": sn["auth"], "namespace": ns, "labels": dict(labels)},
                        "stringData": {"username": creds["username"], "password": creds["password"]}})
        tls = {}
        if d.get("ca_pem"):
            tls["ca_file"] = f"{CA_DIR}/{name}/ca.crt"
            cas[name] = d["ca_pem"]
            secrets.append({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                            "metadata": {"name": sn["ca"], "namespace": ns, "labels": dict(labels)},
                            "stringData": {"ca.crt": d["ca_pem"]}})
        if d.get("insecure_skip_verify"):
            tls["insecure_skip_verify"] = True
        if tls:
            conf["tls"] = tls
        configs[d["endpoint"]] = conf
    if no_fallback is None:
        no_fallback = fallback_asked(ents)
    doc = {"mirrors": mirrors, "configs": configs} if mirrors or configs else {}
    files = []
    if doc:
        src = nodes_secret or nodes_secret_name(cname)
        secrets.append({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                        "metadata": {"name": src, "namespace": ns, "labels": {cc.GENERATED: "true"}},
                        # JSON : du YAML valide, lu tel quel par RKE2
                        "stringData": dict({"registries.yaml": json.dumps(doc, indent=2) + "\n"},
                                           **{f"ca-{n}.crt": pem for n, pem in cas.items()})})
        files.append({"path": REGISTRIES_FILE, "owner": "root:root", "permissions": "0600",
                      "contentFrom": {"secret": {"name": src, "key": "registries.yaml"}}})
        files += [{"path": f"{CA_DIR}/{n}/ca.crt", "owner": "root:root", "permissions": "0644",
                   "contentFrom": {"secret": {"name": src, "key": f"ca-{n}.crt"}}} for n in cas]
    if no_fallback and mirrors:
        files.append({"path": NO_FALLBACK_FILE, "owner": "root:root", "permissions": "0600",
                      "content": "disable-default-registry-endpoint: true\n"})
    return doc, files, secrets


# ---------------------------------------------------------------------------
# 2.8.0 : registres d'un cluster enfant existant (renouvellement)
# ---------------------------------------------------------------------------
NODES_SECRET_REV_RE = re.compile(r"^(?P<base>.+)-registries(?:-r(?P<n>[0-9]{1,4}))?$")


def nodes_secret_from(templates):
    """Nom du Secret des nœuds que désigne `registries.yaml` dans les
    gabarits du plan de contrôle d'un cluster (celui de la révision la plus
    haute), ou None sans registre."""
    best = (-1, None)
    for t in templates or []:
        try:
            rev = int((((t.get("metadata") or {}).get("annotations") or {}).get("harvester-ops.io/template-revision"))
                      or 0)
        except ValueError:
            rev = 0
        for f in ((((t.get("spec") or {}).get("template") or {}).get("spec") or {}).get("files")) or []:
            if f.get("path") == REGISTRIES_FILE:
                name = (((f.get("contentFrom") or {}).get("secret")) or {}).get("name")
                if name and rev > best[0]:
                    best = (rev, name)
    return best[1]


def is_nodes_secret_of(name, cluster_name):
    """`<c>-registries` ou `<c>-registries-r<N>` de ce cluster (jamais d'un
    cluster `<c>-x`)."""
    m = NODES_SECRET_REV_RE.match(name or "")
    return bool(m) and m.group("base") == cluster_name


USED_LABEL_PREFIX = "harvester-ops.io/registry-"


def used_label(registry):
    """Revue finale 2.6.0 (m-14), 2.8.0 : marque d'usage d'un registre par un
    cluster enfant, étiquette du Secret des nœuds `<c>-registries`."""
    return f"{USED_LABEL_PREFIX}{registry}"


def cluster_registry_names(nodes_secret, auth_secrets=(), decls=None):
    """Registres qu'emploie un cluster enfant, dans l'ordre de préférence :
    étiquettes `used_label` du Secret des nœuds (forme 2.8.0) et Secrets
    `<c>-registry-<r>-auth` étiquetés (forme d'avant), ordonnés par la
    première apparition de leur point d'accès dans `registries.yaml`."""
    names = []
    for k, v in (((nodes_secret or {}).get("metadata") or {}).get("labels") or {}).items():
        if k.startswith(USED_LABEL_PREFIX) and v == "used":
            names.append(k[len(USED_LABEL_PREFIX):])
    for sec in auth_secrets or []:
        n = (((sec.get("metadata") or {}).get("labels")) or {}).get(LABEL)
        if n and n not in names:
            names.append(n)
    data = (nodes_secret or {}).get("data") or {}
    try:
        doc = json.loads(base64.b64decode(data.get("registries.yaml") or "").decode() or "{}")
    except ValueError:
        doc = {}
    order = []
    for m in (doc.get("mirrors") or {}).values():
        for ep in m.get("endpoint") or []:
            if ep not in order:
                order.append(ep)
    decls = decls or {}

    def rank(n):
        ep = f"https://{(decls.get(n) or {}).get('endpoint')}"
        return order.index(ep) if ep in order else len(order)
    return sorted(dict.fromkeys(names), key=rank)
