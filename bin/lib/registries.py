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


RESERVED = ("bootstrap",)            # /api/registries/bootstrap/<cluster> est le geste d'amorçage


def save(state, name, doc, revision=None):
    if name in RESERVED:
        raise dp.ProfileError([("name", "reserved name")])
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
def check(decl, facts, name=None):
    """Constats d'un registre sur un cluster Harvester. `facts` :
    {"attached": bool, "reachable": bool, "ca_ok": bool,
     "auth": {"pull": bool|None, "push": bool|None}, "pull_is_push": bool}."""
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


def check_child(entries, decls, cluster, attached, system_ready=()):
    """Constats de la liste de registres d'un cluster enfant (tâche 9) :
    `decls` {nom: déclaration} (absent = inconnu), `cluster` le cluster
    Harvester, `attached` {nom: bool} (compte de lecture posé sur ce
    cluster), `system_ready` les registres qui portent les images système
    de RKE2."""
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
    out += fallback_findings(fallback_asked(known), known, decls, system_ready)
    return out


def system_ready_registries(state, decls):
    """Registres dont un paquet publié porte les images système de RKE2
    (`<origine>/rancher/rke2-runtime`), relevé dans les paquets rangés."""
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


def rke2_config(entries, decls, cluster, pull_creds, no_fallback=None):
    """(privateRegistriesConfig, files, secrets) d'un cluster enfant (pur).

    `entries` : liste ordonnée de registres (noms ou {registry}) ;
    `decls` : {nom: déclaration} ; `cluster` : {"name", "namespace"} ;
    `pull_creds` : {nom: compte de lecture}, lu dans les Secrets `-pull`
    du cluster Harvester (jamais le compte d'écriture).
    `no_fallback` : coupe le repli vers le point d'accès d'origine pour les
    registres miroités (fichier config.yaml.d de RKE2) ; par défaut, ce que
    demandent les entrées (`fallback_asked`), jamais coupé sinon (point 198 ;
    `fallback_findings`, rejoué par `check_child`, refuse la demande avant).

    Secrets : `<cluster>-registry-<registre>-auth` (`username`, `password`)
    et `-ca` (`Opaque`, `ca.crt` seul : un Secret `kubernetes.io/tls` vide
    de `tls.crt` casse containerd, essais préalables de la 2.6.0), étiquetés
    `GENERATED` pour partir avec le cluster ; `insecureSkipVerify` jamais
    posé sauf demande explicite (vérification active)."""
    ents = normalize_entries(entries)
    used = {}
    for e in ents:
        if "images" in (decls[e["registry"]].get("usage") or []):
            used.setdefault(e["registry"], e)
    mirrors, configs, secrets = {}, {}, []
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
        conf = {"authSecret": {"name": sn["auth"], "namespace": ns}}
        creds = pull_creds[name]
        secrets.append({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                        "metadata": {"name": sn["auth"], "namespace": ns, "labels": dict(labels)},
                        "stringData": {"username": creds["username"], "password": creds["password"]}})
        tls = {}
        if d.get("ca_pem"):
            tls["tlsConfigSecret"] = {"name": sn["ca"], "namespace": ns}
            secrets.append({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                            "metadata": {"name": sn["ca"], "namespace": ns, "labels": dict(labels)},
                            "stringData": {"ca.crt": d["ca_pem"]}})
        if d.get("insecure_skip_verify"):
            tls["insecureSkipVerify"] = True
        if tls:
            conf["tls"] = tls
        configs[d["endpoint"]] = conf
    if no_fallback is None:
        no_fallback = fallback_asked(ents)
    files = []
    if no_fallback and mirrors:
        files.append({"path": NO_FALLBACK_FILE, "owner": "root:root", "permissions": "0600",
                      "content": "disable-default-registry-endpoint: true\n"})
    prc = {"mirrors": mirrors, "configs": configs} if mirrors or configs else {}
    return prc, files, secrets
