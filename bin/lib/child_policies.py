"""harvester-ops : socle de politiques et accès d'un cluster enfant (2.7.0).

Catalogue intégré de la console (point 206), décoché par défaut (décision
127) : namespaces d'équipe, `ResourceQuota`, `LimitRange`, `NetworkPolicy`
de refus par défaut et ouvertures. S'y ajoutent des rôles par groupe
(`RoleBinding` vers les `ClusterRole` intégrées admin, edit, view) et des
comptes de service réservés à l'automatisation (décision 125, point 208),
dont les jetons sont émis à la demande par `TokenRequest`
(`child_access.token`) : aucun Secret de jeton n'est jamais rendu.

Le rendu est livré par `capi_manifests` comme un élément nommé
`hops-policies` (même ClusterResourceSet, même retrait suivi).

Bibliothèque standard seulement ; ce module ne parle à aucun cluster.
"""

import copy
import hashlib
import re

import capi_manifests as cm

ITEM = "hops-policies"
TEAM_LABEL = "harvester-ops.io/team"
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
MAX_TEAMS = 16
MAX_ACCOUNTS = 16
GROUP_MAX = 128
ROLES = ("admin", "edit", "view")
ACCOUNT_ROLES = ("edit", "view")

# Valeurs par défaut (Contraintes du plan 2.7.0, point 206), chacune réglable.
QUOTA = {"requests.cpu": "4", "requests.memory": "8Gi", "limits.cpu": "8", "limits.memory": "16Gi",
         "pods": "50", "persistentvolumeclaims": "10", "requests.storage": "200Gi",
         "services.loadbalancers": "2"}
LIMITS = {"default": {"cpu": "500m", "memory": "512Mi"},
          "defaultRequest": {"cpu": "100m", "memory": "128Mi"},
          "max": {"cpu": "2", "memory": "4Gi"}}
NETWORK = {"default_deny": True, "allow_ingress": True}

# Contrôleur d'ingress de RKE2 (chart rke2-ingress-nginx, namespace
# kube-system) : étiquettes de ses pods relevées sur le cluster enfant
# rke2-banc (RKE2 v1.34.11+rke2r1, harvlab2, 07/11/2026), voir « Essais
# préalables (tâche 1) » du plan 2.7.0.
INGRESS_NAMESPACE = "kube-system"
INGRESS_POD_LABELS = {"app.kubernetes.io/name": "rke2-ingress-nginx",
                      "app.kubernetes.io/component": "controller"}
DNS_POD_LABELS = {"k8s-app": "kube-dns"}

_CPU_RE = re.compile(r"^(\d+(\.\d+)?|\d+m)$")
_BYTES_RE = re.compile(r"^\d+(\.\d+)?(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E)?$")
_COUNT_RE = re.compile(r"^\d+$")


def _quantity_re(key):
    if key.endswith("cpu"):
        return _CPU_RE
    if key.endswith("memory") or key.endswith("storage"):
        return _BYTES_RE
    return _COUNT_RE


def ingress_selector():
    """Sélecteur (namespace et pods) du contrôleur d'ingress du cluster
    enfant, pour `hops-allow-ingress`."""
    return {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": INGRESS_NAMESPACE}},
            "podSelector": {"matchLabels": dict(INGRESS_POD_LABELS)}}


# ---------------------------------------------------------------------------
# Demande
# ---------------------------------------------------------------------------
def _name(v, where, max_len=63):
    s = str(v or "")
    if len(s) > max_len or not NAME_RE.match(s):
        raise ValueError(f"{where}: RFC 1123 name, {max_len} characters at most")
    return s


def _quantities(raw, defaults, where):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: an object")
    for k in sorted(set(raw) - set(defaults)):
        raise ValueError(f"{where}.{k}: unknown key ({', '.join(defaults)})")
    out = {}
    for k, dv in defaults.items():
        v = str(raw.get(k, dv))
        if not _quantity_re(k).match(v):
            raise ValueError(f"{where}.{k}: invalid quantity '{v[:20]}'")
        out[k] = v
    return out


_SUFFIX = {"m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18,
           "Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30, "Ti": 2 ** 40, "Pi": 2 ** 50, "Ei": 2 ** 60}


def _amount(v):
    """Valeur d'une quantité déjà validée par `_quantity_re` (cpu, octets)."""
    m = re.match(r"^(\d+(?:\.\d+)?)([a-zA-Z]*)$", v)
    return float(m.group(1)) * _SUFFIX.get(m.group(2), 1)


def _limits(raw, where):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: an object")
    for k in sorted(set(raw) - set(LIMITS)):
        raise ValueError(f"{where}.{k}: unknown key ({', '.join(LIMITS)})")
    out = {k: _quantities(raw.get(k), LIMITS[k], f"{where}.{k}") for k in LIMITS}
    # revue 1-3 m6 : l'API refuse un LimitRange désordonné, et le binding ne
    # s'appliquerait jamais (délai d'attente au lieu d'une raison)
    for res in ("cpu", "memory"):
        if _amount(out["defaultRequest"][res]) > _amount(out["default"][res]):
            raise ValueError(f"{where}.defaultRequest.{res}: greater than default.{res}")
        if _amount(out["default"][res]) > _amount(out["max"][res]):
            raise ValueError(f"{where}.default.{res}: greater than max.{res}")
    return out


def _network(raw, where):
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: an object")
    for k in sorted(set(raw) - set(NETWORK)):
        raise ValueError(f"{where}.{k}: unknown key ({', '.join(NETWORK)})")
    out = {}
    for k, dv in NETWORK.items():
        v = raw.get(k, dv)
        if not isinstance(v, bool):
            raise ValueError(f"{where}.{k}: true or false")
        out[k] = v
    return out


def _list(raw, key, where):
    v = raw.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, dict) for x in v):
        raise ValueError(f"{where}{key}: a list of objects")
    return v


def normalize(raw):
    """Demande complétée de ses défauts ; ValueError qui nomme le champ."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("policies: an object")
    for k in sorted(set(raw) - {"base", "teams", "roles", "accounts"}):
        raise ValueError(f"policies.{k}: unknown key (base, teams, roles, accounts)")
    base = raw.get("base", False)
    if not isinstance(base, bool):
        raise ValueError("base: true or false")
    teams, seen = [], set()
    raw_teams = _list(raw, "teams", "")
    if len(raw_teams) > MAX_TEAMS:
        raise ValueError(f"teams: {MAX_TEAMS} namespaces at most")
    for i, t in enumerate(raw_teams):
        where = f"teams[{i}]"
        for k in sorted(set(t) - {"namespace", "quota", "limits", "network"}):
            raise ValueError(f"{where}.{k}: unknown key")
        ns = _name(t.get("namespace"), f"{where}.namespace")
        if cm.is_system_namespace(ns) or ns == "default":
            raise ValueError(f"{where}.namespace: {ns} is a system namespace")
        if ns in seen:
            raise ValueError(f"{where}.namespace: {ns} appears twice")
        seen.add(ns)
        teams.append({"namespace": ns, "quota": _quantities(t.get("quota"), QUOTA, f"{where}.quota"),
                      "limits": _limits(t.get("limits"), f"{where}.limits"),
                      "network": _network(t.get("network"), f"{where}.network")})
    if base and not teams:
        raise ValueError("teams: one team namespace at least when base is on")
    roles = []
    for i, r in enumerate(_list(raw, "roles", "")):
        where = f"roles[{i}]"
        for k in sorted(set(r) - {"namespace", "group", "role"}):
            raise ValueError(f"{where}.{k}: unknown key")
        ns = str(r.get("namespace") or "")
        if ns not in seen:
            raise ValueError(f"{where}.namespace: not a declared team namespace")
        group = r.get("group")
        if not isinstance(group, str) or not group.strip() or len(group) > GROUP_MAX or "\n" in group \
                or group.startswith("system:"):
            raise ValueError(f"{where}.group: one line, {GROUP_MAX} characters at most, not system:*")
        if r.get("role") not in ROLES:
            raise ValueError(f"{where}.role: one of {', '.join(ROLES)}")
        roles.append({"namespace": ns, "group": group, "role": r["role"]})
    accounts, names = [], set()
    raw_acc = _list(raw, "accounts", "")
    if len(raw_acc) > MAX_ACCOUNTS:
        raise ValueError(f"accounts: {MAX_ACCOUNTS} at most")
    for i, a in enumerate(raw_acc):
        where = f"accounts[{i}]"
        for k in sorted(set(a) - {"namespace", "name", "role"}):
            raise ValueError(f"{where}.{k}: unknown key")
        ns = str(a.get("namespace") or "")
        if ns not in seen:
            raise ValueError(f"{where}.namespace: not a declared team namespace")
        name = _name(a.get("name"), f"{where}.name")
        if (ns, name) in names:
            raise ValueError(f"{where}.name: {ns}/{name} appears twice")
        names.add((ns, name))
        if a.get("role") not in ACCOUNT_ROLES:
            raise ValueError(f"{where}.role: one of {', '.join(ACCOUNT_ROLES)}")
        accounts.append({"namespace": ns, "name": name, "role": a["role"]})
    return {"base": base, "teams": teams, "roles": roles, "accounts": accounts}


# ---------------------------------------------------------------------------
# Rendu
# ---------------------------------------------------------------------------
def _np(ns, name, spec):
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": name, "namespace": ns}, "spec": spec}


def _team_objects(t):
    ns = t["namespace"]
    out = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": {TEAM_LABEL: "true"}}},
           {"apiVersion": "v1", "kind": "ResourceQuota", "metadata": {"name": "hops-quota", "namespace": ns},
            "spec": {"hard": dict(t["quota"])}},
           {"apiVersion": "v1", "kind": "LimitRange", "metadata": {"name": "hops-limits", "namespace": ns},
            "spec": {"limits": [dict({"type": "Container"}, **copy.deepcopy(t["limits"]))]}}]
    if not t["network"]["default_deny"]:
        return out
    out += [
        _np(ns, "hops-default-deny", {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}),
        _np(ns, "hops-allow-same-namespace", {"podSelector": {}, "policyTypes": ["Ingress", "Egress"],
                                              "ingress": [{"from": [{"podSelector": {}}]}],
                                              "egress": [{"to": [{"podSelector": {}}]}]}),
        _np(ns, "hops-allow-dns", {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{
            "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                    "podSelector": {"matchLabels": dict(DNS_POD_LABELS)}}],
            "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}]}),
    ]
    if t["network"]["allow_ingress"]:
        out.append(_np(ns, "hops-allow-ingress", {"podSelector": {}, "policyTypes": ["Ingress"],
                                                  "ingress": [{"from": [ingress_selector()]}]}))
    return out


def role_binding_name(role, group):
    return f"hops-group-{role}-{hashlib.sha256(group.encode()).hexdigest()[:10]}"


def render(spec):
    """{"base": [...], "roles": [...], "accounts": [...]} d'une demande
    normalisée. `base: false` : aucun objet du socle."""
    base = [o for t in spec["teams"] for o in _team_objects(t)] if spec["base"] else []
    roles = [{"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
              "metadata": {"name": role_binding_name(r["role"], r["group"]), "namespace": r["namespace"]},
              "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": r["role"]},
              "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": r["group"]}]}
             for r in spec["roles"]]
    accounts = []
    for a in spec["accounts"]:
        accounts += [
            {"apiVersion": "v1", "kind": "ServiceAccount", "automountServiceAccountToken": False,
             "metadata": {"name": a["name"], "namespace": a["namespace"]}},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
             "metadata": {"name": f"hops-sa-{a['name']}", "namespace": a["namespace"]},
             "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": a["role"]},
             "subjects": [{"kind": "ServiceAccount", "name": a["name"], "namespace": a["namespace"]}]}]
    return {"base": base, "roles": roles, "accounts": accounts}


def objects(spec):
    """Tous les objets, dans l'ordre d'application (socle, rôles, comptes)."""
    r = render(spec)
    return r["base"] + r["roles"] + r["accounts"]


def team_namespaces(spec):
    """Namespaces que le socle crée (vide quand il est décoché)."""
    return [t["namespace"] for t in spec["teams"]] if spec["base"] else []


def used_namespaces(spec):
    """Namespaces d'équipe où vont des rôles ou des comptes sans que le socle
    les crée : ils doivent exister dans le cluster enfant (revue 1-3 m6)."""
    made = set(team_namespaces(spec))
    return sorted({x["namespace"] for x in spec["roles"] + spec["accounts"]} - made)


# ---------------------------------------------------------------------------
# 2.8.0 : groupe administrateur OIDC (point 229)
# ---------------------------------------------------------------------------
ADMIN_BINDING = "hops-oidc-admins"


def admin_binding(groups_prefix, admin_group):
    """La SEULE `ClusterRoleBinding` vers `cluster-admin` que la console
    livre : sujet `Group <préfixe><groupe>` du fournisseur OIDC du cluster
    enfant. Rendue depuis l'option `oidc` du cluster (annotation posée à la
    création), jamais depuis une demande ni un paquet ; None sans groupe."""
    if not admin_group:
        return None
    return {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
            "metadata": {"name": ADMIN_BINDING},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "cluster-admin"},
            "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group",
                          "name": f"{groups_prefix}{admin_group}"}]}


def group_with_prefix(group, groups_prefix):
    """Groupe tel que l'API le voit : préfixe du cluster ajouté s'il manque
    (proposé par la console pour les rôles par groupe, point 229)."""
    if not groups_prefix or group.startswith(groups_prefix):
        return group
    return groups_prefix + group
