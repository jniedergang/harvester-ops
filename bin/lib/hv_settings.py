"""harvester-ops : le menu « Advanced » de Harvester, suite (v1.67.0).

Les réglages de Harvester (Advanced > Settings) avec leur forme, leurs
contrôles et leurs dangers ; la cible de sauvegarde (NFS, S3) ; le paquet de
support de Harvester ; un kubeconfig sûr à télécharger (compte de service,
rôle choisi, jeton qui expire). Relevé dans harvester-ui-extension et
harvester v1.9.0 (scratchpad du chantier :
parity/harvester-settings-devices-1.9.md).

Fonctions pures ; bin/harvester-resources.py les applique. Aucune valeur
secrète ne ressort d'ici vers la page : clé privée TLS, clés S3, mots de
passe de registre et identifiants de proxy sont masqués.
"""

import base64
import json
import re
import secrets as _secrets
from datetime import datetime, timedelta, timezone

K_SETTING = "settings.harvesterhci.io"
K_BUNDLE = "supportbundles.harvesterhci.io"
HASH_ANN = "harvesterhci.io/hash"
BUNDLE_NS = "harvester-system"
KC_NS = "harvester-ops-kubeconfigs"
KC_LABEL = "harvester-ops.io/kubeconfig"
KC_EXPIRES = "harvester-ops.io/expires"
KC_ROLE = "harvester-ops.io/role"
KC_SCOPE = "harvester-ops.io/scope"
MASK = "•••"
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
KC_ROLES = ("view", "edit", "admin", "cluster-admin", "harvesterhci.io:view", "harvesterhci.io:edit")
HARVESTER_PROXY = "/api/v1/namespaces/harvester-system/services/https:harvester:8443/proxy"

# Chaque réglage : groupe, forme du champ, bornes, danger, présence d'un
# contrôleur (appliqué = annotation de hash à jour), et s'il est tenu ailleurs
# (réseaux) ou en lecture seule.
META = {
    "log-level": {"group": "general", "kind": "enum", "choices": ("info", "debug", "trace"), "sync": True},
    "default-vm-termination-grace-period-seconds": {"group": "general", "kind": "number", "min": 0},
    "vm-force-reset-policy": {"group": "general", "kind": "json"},
    "ntp-servers": {"group": "general", "kind": "json", "sync": True},
    "auto-disk-provision-paths": {"group": "general", "kind": "text", "sync": True, "danger": "disks"},
    "http-proxy": {"group": "network", "kind": "json", "sync": True, "danger": "rke2"},
    "containerd-registry": {"group": "network", "kind": "json", "sync": True, "danger": "rke2"},
    "cluster-registration-url": {"group": "network", "kind": "json", "sync": True, "danger": "rancher"},
    "rancher-cluster": {"group": "network", "kind": "json"},
    "storage-network": {"group": "network", "kind": "json", "sync": True, "elsewhere": "network"},
    "vm-migration-network": {"group": "network", "kind": "json", "sync": True, "elsewhere": "network"},
    "rwx-network": {"group": "network", "kind": "json", "sync": True, "elsewhere": "network"},
    "ssl-certificates": {"group": "security", "kind": "json", "sync": True, "danger": "tls"},
    # 2.10.1 : sans effet selon le contrôleur d'ingress de Harvester, lu sur
    # le cluster (ingress_kind) ; plus de règle fixe par version
    "ssl-parameters": {"group": "security", "kind": "json", "sync": True, "inert_under": "traefik"},
    "traefik-default-tls-options": {"group": "security", "kind": "json", "sync": True, "danger": "tls",
                                    "inert_under": "nginx"},
    "additional-ca": {"group": "security", "kind": "pem", "sync": True},
    "cluster-pod-security-standard": {"group": "security", "kind": "json", "sync": True},
    "auto-rotate-rke2-certs": {"group": "security", "kind": "json", "sync": True, "danger": "rke2"},
    "kubeconfig-default-token-ttl-minutes": {"group": "security", "kind": "number", "min": 0, "max": 52560000, "sync": True},
    "overcommit-config": {"group": "performance", "kind": "json", "sync": True},
    "additional-guest-memory-overhead-ratio": {"group": "performance", "kind": "text", "sync": True},
    "max-hotplug-ratio": {"group": "performance", "kind": "number", "min": 1, "max": 20, "sync": True},
    "kubevirt-migration": {"group": "performance", "kind": "json", "sync": True},
    "instance-manager-resources": {"group": "performance", "kind": "json", "sync": True},
    "longhorn-v2-data-engine-enabled": {"group": "performance", "kind": "bool", "sync": True},
    "longhorn-v2-data-engine-hugepage-enabled": {"group": "performance", "kind": "bool", "sync": True},
    "longhorn-v2-data-engine-memory-size": {"group": "performance", "kind": "number", "min": 2, "sync": True},
    "csi-driver-config": {"group": "performance", "kind": "json"},
    "csi-online-expand-validation": {"group": "performance", "kind": "json"},
    "backup-target": {"group": "backup", "kind": "backup", "sync": True},
    "server-version": {"group": "upgrade", "kind": "text", "readonly": True},
    "upgrade-checker-enabled": {"group": "upgrade", "kind": "bool"},
    "upgrade-checker-url": {"group": "upgrade", "kind": "text"},
    "release-download-url": {"group": "upgrade", "kind": "text"},
    "upgrade-config": {"group": "upgrade", "kind": "json"},
    "support-bundle-image": {"group": "support", "kind": "json", "sync": True},
    "support-bundle-namespaces": {"group": "support", "kind": "text"},
    "support-bundle-file-name": {"group": "support", "kind": "text"},
    "support-bundle-timeout": {"group": "support", "kind": "number", "min": 0},
    "support-bundle-expiration": {"group": "support", "kind": "number", "min": 0},
    "support-bundle-node-collection-timeout": {"group": "support", "kind": "number", "min": 0},
    "ui-source": {"group": "ui", "kind": "enum", "choices": ("auto", "external", "bundled"), "danger": "ui"},
    "ui-index": {"group": "ui", "kind": "text"},
}
GROUPS = ("general", "network", "security", "performance", "backup", "upgrade", "support", "ui")


def _meta(o):
    return (o or {}).get("metadata") or {}


def check_name(name, what="name"):
    if not NAME_RE.match(name or ""):
        raise ValueError(f"{what}: lower-case letters, digits and dashes, 63 at most")
    return name


# ---------------------------------------------------------------------------
# Masquage des secrets
# ---------------------------------------------------------------------------

def _mask_url(u):
    return re.sub(r"(//[^:/@]+:)[^@]+@", r"\1" + MASK + "@", str(u))


def _mask_import(u):
    """L'URL d'import de Rancher porte le jeton d'enregistrement du cluster,
    qui donne les identifiants de son agent (vu sur harv1)."""
    return re.sub(r"(/v3/import/)[^/?#]+", r"\1" + MASK, str(u))


def redact(name, value):
    """Une valeur de réglage telle qu'on peut la montrer : les secrets
    qu'elle porte sont masqués (la valeur réelle reste sur le cluster)."""
    if value and name == "cluster-registration-url":
        try:
            v = json.loads(value)
        except ValueError:
            return _mask_import(value)                 # URL nue (forme d'avant)
        if isinstance(v, dict) and v.get("url"):
            v["url"] = _mask_import(v["url"])
            return json.dumps(v, separators=(",", ":"), ensure_ascii=False)
        return value
    if not value or name not in ("ssl-certificates", "backup-target", "containerd-registry", "http-proxy"):
        return value
    try:
        v = json.loads(value)
    except ValueError:
        return value
    if not isinstance(v, dict):
        return value
    if name == "ssl-certificates" and v.get("privateKey"):
        v["privateKey"] = MASK
    if name == "backup-target":
        for k in ("accessKeyId", "secretAccessKey"):
            if v.get(k):
                v[k] = MASK
    if name == "containerd-registry":
        for cfg in (v.get("Configs") or {}).values():
            auth = (cfg or {}).get("Auth") or {}
            for k in ("Password", "Auth", "IdentityToken"):
                if auth.get(k):
                    auth[k] = MASK
    if name == "http-proxy":
        for k in ("httpProxy", "httpsProxy"):
            if v.get(k):
                v[k] = _mask_url(v[k])
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False)


def unmask(name, new_value, current_value):
    """Une valeur revenue du formulaire avec des secrets masqués : on y remet
    les vrais depuis la valeur actuelle. Les clés S3 et les mots de passe de
    registre, que Harvester efface du réglage, doivent être ressaisis."""
    if not new_value or MASK not in new_value:
        return new_value
    if name == "cluster-registration-url":
        # la même URL revenue masquée : on garde celle du cluster ; une autre
        # URL se donne en entier
        def url_of(x):
            try:
                d = json.loads(x)
            except ValueError:
                return x.strip(), None
            return (d.get("url") or "", d) if isinstance(d, dict) else (x, None)
        new_url, new_d = url_of(new_value)
        cur_url, _ = url_of(current_value or "")
        if _mask_import(cur_url) != new_url:
            raise ValueError("registration URL: paste the whole import URL given by Rancher")
        if new_d is None:
            return cur_url
        new_d["url"] = cur_url
        return json.dumps(new_d, separators=(",", ":"))
    try:
        v, cur = json.loads(new_value), json.loads(current_value or "{}")
    except ValueError:
        raise ValueError(f"{name}: not valid JSON") from None
    if name == "ssl-certificates" and v.get("privateKey") == MASK:
        v["privateKey"] = cur.get("privateKey") or ""
    if name == "http-proxy":
        for k in ("httpProxy", "httpsProxy"):
            if MASK in str(v.get(k) or ""):
                if _mask_url(cur.get(k) or "") != v[k]:
                    raise ValueError(f"{k}: type the proxy password again")
                v[k] = cur.get(k)
    if MASK in json.dumps(v, ensure_ascii=False):
        raise ValueError("Harvester keeps these secrets out of the setting: type them again")
    return json.dumps(v, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Lecture : lignes, état appliqué
# ---------------------------------------------------------------------------

def applied_state(obj):
    """ok | pending | error | "" (sans contrôleur : appliqué dès l'écriture)."""
    name = _meta(obj).get("name")
    if not (META.get(name) or {}).get("sync"):
        return "", ""
    cond = next((c for c in ((obj.get("status") or {}).get("conditions") or []) if c.get("type") == "configured"), None)
    if cond and str(cond.get("status")) == "False" and cond.get("message"):
        return "error", cond.get("message")
    if not (_meta(obj).get("annotations") or {}).get(HASH_ANN):
        return ("pending", "") if obj.get("value") else ("", "")
    return "ok", ""


# Contrôleurs d'ingress de RKE2 dans kube-system : HelmChart du chart embarqué
# et service qu'il publie (rke2-ingress-nginx-controller, rke2-traefik).
# Hypothèse 2.10.1 à relire sur harvlab en 1.9.1 : la migration vers Traefik
# retire le chart et le service d'ingress-nginx.
INGRESS_OBJECTS = {"nginx": ({"rke2-ingress-nginx"}, {"rke2-ingress-nginx-controller"}),
                   "traefik": ({"rke2-traefik"}, {"rke2-traefik"})}


def ingress_kind(charts, services):
    """Contrôleur d'ingress de Harvester : "nginx", "traefik", "both" ou
    "unknown". `charts` : HelmChart de kube-system ; `services` : services
    de kube-system ; None pour une lecture en échec."""
    names = lambda objs: {_meta(o).get("name") for o in objs or []}  # noqa: E731
    got_charts, got_services = names(charts), names(services)
    found = {k for k, (c, sv) in INGRESS_OBJECTS.items() if c & got_charts or sv & got_services}
    if found == {"nginx", "traefik"}:
        return "both"
    return found.pop() if found else "unknown"


def rows(settings, show_hidden=False, ingress=None):
    """`ingress` (2.10.1) : contrôleur lu par ingress_kind ; un réglage n'est
    dit sans effet que sous le contrôleur qui l'ignore (None : jamais)."""
    out = []
    for s in sorted(settings or [], key=lambda x: _meta(x).get("name")):
        name = _meta(s).get("name")
        m = META.get(name)
        if not m and not show_hidden:
            continue
        m = m or {"group": "hidden", "kind": "text", "readonly": True}
        value, default = s.get("value") or "", s.get("default") or ""
        state, msg = applied_state(s)
        out.append({"name": name, "group": m["group"], "kind": m["kind"], "choices": list(m.get("choices") or []),
                    "value": redact(name, value), "default": redact(name, default),
                    "modified": bool(value) and value != default, "applied": state, "message": msg,
                    "danger": m.get("danger") or "", "readonly": bool(m.get("readonly")),
                    "elsewhere": m.get("elsewhere") or "",
                    "inert": bool(m.get("inert_under")) and m.get("inert_under") == ingress,
                    "min": m.get("min"), "max": m.get("max")})
    return out


# ---------------------------------------------------------------------------
# Contrôles d'une nouvelle valeur (ceux du webhook, faits d'avance)
# ---------------------------------------------------------------------------

def _json(value, name):
    try:
        return json.loads(value)
    except ValueError:
        raise ValueError(f"{name}: not valid JSON") from None


def _int(value, name, lo=None, hi=None):
    if not re.fullmatch(r"-?\d+", str(value).strip()):
        raise ValueError(f"{name}: a whole number")
    n = int(value)
    if (lo is not None and n < lo) or (hi is not None and n > hi):
        raise ValueError(f"{name}: from {lo} to {hi}" if hi is not None else f"{name}: {lo} or more")
    return n


def validate(name, value):
    """La valeur normalisée à écrire, ou ValueError avec la raison que
    Harvester donnerait. `value` vide = revenir au défaut."""
    m = META.get(name)
    if not m:
        raise ValueError(f"{name}: not a setting the console changes")
    if m.get("readonly"):
        raise ValueError(f"{name} is read-only")
    if m.get("elsewhere"):
        raise ValueError(f"{name} is changed in Network > Cluster Networks")
    value = "" if value is None else str(value)
    if value.strip() == "":
        return ""
    k = m["kind"]
    if k == "enum" and value not in m["choices"]:
        raise ValueError(f"{name}: one of {', '.join(m['choices'])}")
    if k == "bool" and value not in ("true", "false"):
        raise ValueError(f"{name}: true or false")
    if k == "number":
        n = _int(value, name, m.get("min"), m.get("max"))
        if name == "longhorn-v2-data-engine-memory-size" and n % 2:
            raise ValueError(f"{name}: an even number of MiB")
        return str(n)
    if k == "backup":
        return backup_target_value(_json(value, name))
    if name == "additional-guest-memory-overhead-ratio":
        try:
            f = float(value)
        except ValueError:
            raise ValueError(f"{name}: 0, or a number from 1.0 to 10.0") from None
        if f != 0 and not 1.0 <= f <= 10.0:
            raise ValueError(f"{name}: 0, or a number from 1.0 to 10.0")
        return value.strip()
    if k == "pem":
        if "-----BEGIN CERTIFICATE-----" not in value:
            raise ValueError(f"{name}: one or more PEM certificates")
        return value
    if k != "json":
        return value.strip()
    v = _json(value, name)
    if name == "overcommit-config":
        for key in ("cpu", "memory", "storage"):
            if key in v and int(v[key]) < 100:
                raise ValueError(f"Cannot undercommit. Should be greater than or equal to 100 but got {v[key]}")
    if name == "ntp-servers":
        servers = v.get("ntpServers") if isinstance(v, dict) else None
        if servers is None:
            raise ValueError("ntp-servers: {\"ntpServers\": [..]}")
        seen = set()
        for sv in servers:
            if re.match(r"^https?://", str(sv)):
                raise ValueError(f"ntp server {sv}: no http:// prefix")
            if not re.fullmatch(r"[A-Za-z0-9.:-]+", str(sv)):
                raise ValueError(f"ntp server {sv}: an address or a host name")
            if sv in seen:
                raise ValueError(f"ntp server {sv} given twice")
            seen.add(sv)
    if name == "vm-force-reset-policy":
        if int(v.get("period", 1)) <= 0 or int(v.get("vmMigrationTimeout", 0)) < 0:
            raise ValueError("vm-force-reset-policy: period above 0, vmMigrationTimeout 0 or more")
    if name == "auto-rotate-rke2-certs" and not 1 <= int(v.get("expiringInHours", 240)) <= 8759:
        raise ValueError("auto-rotate-rke2-certs: expiringInHours from 1 to 8759")
    if name == "http-proxy" and not isinstance(v, dict):
        raise ValueError("http-proxy: {\"httpProxy\", \"httpsProxy\", \"noProxy\"}")
    return json.dumps(v, separators=(",", ":"))


def backup_target_value(spec):
    """La cible de sauvegarde : NFS {endpoint} ou S3 {endpoint, bucket,
    région, clés, style}. Les clés S3 ne ressortent jamais : Harvester les
    range dans un secret et les efface du réglage."""
    t = (spec or {}).get("type") or ""
    interval = spec.get("refreshIntervalInSeconds")
    if interval not in (None, ""):
        if int(interval) < 0:
            raise ValueError("Refresh interval should be greater than or equal to 0")
    if t == "nfs":
        ep = str(spec.get("endpoint") or "").strip()
        if not ep:
            raise ValueError("NFS backup target should have an endpoint like nfs://server:/path/")
        if not ep.startswith("nfs://"):
            ep = "nfs://" + ep
        out = {"type": "nfs", "endpoint": ep}
    elif t == "s3":
        if not spec.get("bucketName") or not spec.get("bucketRegion"):
            raise ValueError("S3 backup target should have bucket name and region")
        if not spec.get("accessKeyId") or not spec.get("secretAccessKey"):
            raise ValueError("S3 backup target should have access key and access key id")
        out = {"type": "s3", "endpoint": str(spec.get("endpoint") or ""), "bucketName": spec["bucketName"],
               "bucketRegion": spec["bucketRegion"], "accessKeyId": spec["accessKeyId"],
               "secretAccessKey": spec["secretAccessKey"], "virtualHostedStyle": bool(spec.get("virtualHostedStyle"))}
    else:
        raise ValueError("Invalid backup target type: nfs or s3")
    if interval not in (None, ""):
        out["refreshIntervalInSeconds"] = int(interval)
    return json.dumps(out, separators=(",", ":"))


def settled(obj, before_hash, elapsed=None, grace=30):
    """Appliqué ? (True, False, None en cours). Sans contrôleur : oui dès
    l'écriture. Avec : le hash doit avoir bougé, et la condition configured
    ne pas porter d'erreur (l'erreur dite telle quelle après un délai)."""
    name = _meta(obj).get("name")
    if not (META.get(name) or {}).get("sync"):
        return True, "saved"
    state, msg = applied_state(obj)
    h = (_meta(obj).get("annotations") or {}).get(HASH_ANN)
    if state == "error" and (elapsed is None or elapsed >= grace or h != before_hash):
        return False, msg
    if h and h != before_hash:
        return True, "applied by Harvester"
    if not obj.get("value") and state != "error":
        return True, "back to the default"
    return None, "waiting for Harvester"


# ---------------------------------------------------------------------------
# Paquet de support de Harvester
# ---------------------------------------------------------------------------

def bundle_manifest(spec, rnd=_secrets):
    desc = str(spec.get("description") or "").strip()
    if not desc:
        raise ValueError("a description is required")
    name = spec.get("name") or f"bundle-{rnd.token_hex(3)}"
    check_name(name, "bundle name")
    body = {"description": desc[:1000]}
    if spec.get("issue_url"):
        if not re.match(r"^https?://", spec["issue_url"]):
            raise ValueError("issue URL: an http(s) link")
        body["issueURL"] = spec["issue_url"]
    nss = [check_name(n.strip(), "namespace") for n in spec.get("namespaces") or [] if str(n).strip()]
    if nss:
        body["extraCollectionNamespaces"] = nss
    for key, field in (("timeout", "timeout"), ("expiration", "expiration"), ("node_timeout", "nodeTimeout")):
        if spec.get(key) not in (None, ""):
            body[field] = _int(spec[key], key, 0)
    return {"apiVersion": "harvesterhci.io/v1beta1", "kind": "SupportBundle",
            "metadata": {"name": name, "namespace": BUNDLE_NS}, "spec": body}


def bundle_rows(items):
    out = []
    for b in items or []:
        st = b.get("status") or {}
        out.append({"name": _meta(b).get("name"), "description": (b.get("spec") or {}).get("description") or "",
                    "state": st.get("state") or "", "progress": st.get("progress") or 0,
                    "filename": st.get("filename") or "", "filesize": st.get("filesize") or 0,
                    "created": _meta(b).get("creationTimestamp")})
    return sorted(out, key=lambda r: r["created"] or "", reverse=True)


def bundle_download_path(name, retain=True):
    check_name(name, "bundle name")
    return f"{HARVESTER_PROXY}/v1/harvester/supportbundles/{name}/download" + ("?retain=true" if retain else "")


BACKUP_HEALTH_PATH = f"{HARVESTER_PROXY}/v1/harvester/backuptarget/healthz"


# ---------------------------------------------------------------------------
# Kubeconfig : compte de service, rôle, jeton qui expire
# ---------------------------------------------------------------------------

DURATIONS = {"1h": 3600, "8h": 8 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "90d": 90 * 86400}


def kubeconfig_objects(spec, now=None):
    """Le compte de service et sa liaison : un kubeconfig qui ne donne que
    le rôle choisi (sur le cluster ou un namespace) et dont le jeton expire.
    Révoquer = supprimer le compte."""
    name = check_name(spec.get("name"), "kubeconfig name")
    role = spec.get("role")
    if role not in KC_ROLES:
        raise ValueError("role: " + ", ".join(KC_ROLES))
    dur = spec.get("duration") or "24h"
    if dur not in DURATIONS:
        raise ValueError("duration: " + ", ".join(DURATIONS))
    ns = str(spec.get("namespace") or "").strip()
    if ns:
        check_name(ns, "namespace")
    if role == "cluster-admin" and ns:
        raise ValueError("cluster-admin is given on the whole cluster, not a namespace")
    now = now or datetime.now(timezone.utc)
    expires = (now + timedelta(seconds=DURATIONS[dur])).strftime("%Y-%m-%dT%H:%M:%SZ")
    labels = {KC_LABEL: name}
    ann = {KC_EXPIRES: expires, KC_ROLE: role, KC_SCOPE: ns or "*",
           "field.cattle.io/description": str(spec.get("description") or "")[:500]}
    sa = {"apiVersion": "v1", "kind": "ServiceAccount",
          "metadata": {"name": f"kc-{name}", "namespace": KC_NS, "labels": labels, "annotations": ann}}
    subject = [{"kind": "ServiceAccount", "name": f"kc-{name}", "namespace": KC_NS}]
    if ns:
        binding = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
                   "metadata": {"name": f"harvester-ops-kc-{name}", "namespace": ns, "labels": labels},
                   "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": role},
                   "subjects": subject}
    else:
        binding = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
                   "metadata": {"name": f"harvester-ops-kc-{name}", "labels": labels},
                   "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": role},
                   "subjects": subject}
    return sa, binding, DURATIONS[dur]


def kubeconfig_yaml(cluster, server, ca_pem, token, user):
    """Le fichier, en YAML écrit à la main (pas de dépendance)."""
    ca = base64.b64encode(ca_pem.encode()).decode()
    return (f"apiVersion: v1\nkind: Config\nclusters:\n- name: {cluster}\n  cluster:\n    server: {server}\n"
            f"    certificate-authority-data: {ca}\nusers:\n- name: {user}\n  user:\n    token: {token}\n"
            f"contexts:\n- name: {user}@{cluster}\n  context:\n    cluster: {cluster}\n    user: {user}\n"
            f"current-context: {user}@{cluster}\n")


def roles_info(clusterroles):
    """Les rôles proposés pour un kubeconfig, et ceux qui lisent les
    secrets. Vu sur harv1 : Harvester agrège `harvesterhci.io:view` dans
    `view`, qui lit donc les Secrets du namespace (cloud-init compris),
    contrairement au `view` d'origine de Kubernetes."""
    by = {_meta(c).get("name"): c for c in clusterroles or []}
    out = []
    for name in KC_ROLES:
        c = by.get(name)
        reads = any(("secrets" in (r.get("resources") or []) or "*" in (r.get("resources") or []))
                    and ("" in (r.get("apiGroups") or []) or "*" in (r.get("apiGroups") or []))
                    and set(r.get("verbs") or []) & {"get", "list", "*"}
                    for r in ((c or {}).get("rules") or []))
        out.append({"name": name, "exists": c is not None, "reads_secrets": reads})
    return out


def kubeconfig_rows(sas, now=None):
    now = now or datetime.now(timezone.utc)
    out = []
    for sa in sas or []:
        m = _meta(sa)
        ann = m.get("annotations") or {}
        exp = ann.get(KC_EXPIRES) or ""
        try:
            expired = datetime.strptime(exp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) <= now
        except ValueError:
            expired = False
        out.append({"name": (m.get("labels") or {}).get(KC_LABEL) or m.get("name"), "role": ann.get(KC_ROLE) or "",
                    "scope": ann.get(KC_SCOPE) or "*", "expires": exp, "expired": expired,
                    "description": ann.get("field.cattle.io/description") or "", "created": m.get("creationTimestamp")})
    return sorted(out, key=lambda r: r["created"] or "", reverse=True)
