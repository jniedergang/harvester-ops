"""harvester-ops : identité CAPHV restreinte, une par cluster enfant (2.5.0).

Le cluster Harvester est aussi le cluster de gestion : CAPHV agit sur lui
avec le kubeconfig du secret désigné par `HarvesterCluster.spec.identitySecret`
(relu à chaque réconciliation). Jusqu'en 2.4.0, c'était le kubeconfig de la
console (`system:masters`). Ici, chaque cluster enfant reçoit son compte de
service, avec les seuls droits que CAPHV emploie, relevés appel par appel
dans sa source à l'étiquette de la version installée (décision 126).

Une version de CAPHV non relue n'a pas de règles : l'identité restreinte est
alors refusée (`identity-rules-unknown`), jamais devinée.

Décisions : 164 (namespace des VMs propre au cluster, `<namespace>-vms` depuis
la revue du 05/11/2026), 165 (identité
restreinte par défaut), 166 (compte et liaison du fournisseur de cloud créés
d'avance, jamais de droit `bind`), 167 (jeton porté par un secret, supprimé
avec le cluster). Voir docs/design/2026-11-05-plans-capi-250-plan.md.

Bibliothèque standard seulement, aucune lecture du cluster (pur).
"""

import base64
import re

# Étiquettes : la suppression retrouve tout ce que la console a posé pour un
# cluster, objets de portée cluster compris (que la suppression du namespace
# n'emporte pas). `GENERATED` est celle de capi_cluster.
GENERATED = "harvester-ops.io/generated"
L_CLUSTER = "harvester-ops.io/caphv-cluster"
L_NAMESPACE = "harvester-ops.io/caphv-namespace"
MANAGED = "harvester-ops.io/managed"        # capi_stack.MANAGED : namespace créé par la console

IDENTITY = "caphv-identity"
TOKEN_SECRET = "caphv-identity-token"
CLOUD_PROVIDER_ROLE = "harvesterhci.io:cloudprovider"   # util/cloud_provider.go:45 (v0.10.1)
IDENTITIES = ("restricted", "console")
# Namespaces des rôles annexes (image, clé, harvester-system, kube-system),
# notés sur le compte de l'identité : la suppression retrouve chaque objet
# par son nom, sans liste à l'échelle du cluster (droits de la personne).
SIDE_NAMESPACES = "harvester-ops.io/caphv-side-namespaces"
# Rôles annexes posés quel que soit la demande (règles à portée fixe)
FIXED_SIDE_NAMESPACES = ("harvester-system", "kube-system")
# Namespace des VMs d'un cluster à identité restreinte : `<namespace>-vms`,
# créé et supprimé par la console avec l'identité. Pas le namespace du
# cluster : CAPHV y crée le jeton du fournisseur de cloud sous le nom
# `<cluster>-token` (util/cloud_provider.go:132, v0.10.1), celui que CAPRKE2
# donne au jeton d'amorçage du cluster dans le namespace du cluster ; dans le
# même namespace, CAPHV relit le secret de CAPRKE2, sans `token`, et le
# cluster n'est jamais prêt (vu sur harvlab2 le 05/11/2026).
VM_NS_SUFFIX = "-vms"

# Genres des objets de l'identité (liaisons avant les rôles), pour le
# sélecteur des diagnostics ; la suppression passe par `teardown` (noms).
KINDS = (
    ("clusterrolebindings.rbac.authorization.k8s.io", False),
    ("clusterroles.rbac.authorization.k8s.io", False),
    ("rolebindings.rbac.authorization.k8s.io", True),
    ("roles.rbac.authorization.k8s.io", True),
    ("secrets", True),
    ("serviceaccounts", True),
)

# ---------------------------------------------------------------------------
# Règles par version de CAPHV
#
# Source : dépôt cluster-api-provider-harvester, étiquette v0.10.1 (commit
# 734c983), client typé de l'identité (`HarvesterClient`, `hvClient`,
# `lbClient`), tests exclus. Chemins relatifs à internal/controller/ sauf
# util/. Portées :
#   ns        namespace des VMs du cluster, `<namespace>-vms` (décision 164)
#   image     namespace de l'image ; keypair : namespace de la clé
#   <nom>     un namespace fixe (harvester-system, kube-system)
#   cluster   objets de portée cluster
# `names` : None (tous), une liste fixe, ou un jeton résolu par la demande
# (namespace, pools, generated-pool, cloud-provider, keypair).
# ---------------------------------------------------------------------------

RULES = {
    "v0.10.1": [
        {"scope": "ns", "group": "kubevirt.io", "resource": "virtualmachines",
         "verbs": ["get", "create", "delete"], "names": None,
         "src": ["harvestermachine_controller.go:381", "harvestermachine_controller.go:761",
                 "harvestermachine_controller.go:1611", "harvestermachine_controller.go:1628",
                 "harvestercluster_controller.go:1513"]},
        {"scope": "ns", "group": "kubevirt.io", "resource": "virtualmachineinstances",
         "verbs": ["get"], "names": None,
         "src": ["harvestermachine_controller.go:666 (getIPAddressesFromVMI, called at :404)"]},
        {"scope": "ns", "group": "", "resource": "secrets",
         "verbs": ["get", "create", "update", "delete"], "names": None,
         "src": ["harvestermachine_controller.go:952", "harvestermachine_controller.go:958",
                 "harvestermachine_controller.go:965", "harvestermachine_controller.go:1594",
                 "util/cloud_provider.go:136", "util/cloud_provider.go:160"]},
        {"scope": "ns", "group": "", "resource": "services",
         "verbs": ["get", "create", "update", "delete"], "names": None,
         "src": ["harvestercluster_controller.go:378", "harvestercluster_controller.go:430",
                 "harvestercluster_controller.go:573", "harvestercluster_controller.go:758"]},
        # create (util/cloud_provider.go:81) jamais appelé : le compte est créé
        # d'avance par la console (décision 166), le get le trouve
        {"scope": "ns", "group": "", "resource": "serviceaccounts",
         "verbs": ["get"], "names": "cloud-provider",
         "src": ["util/cloud_provider.go:69", "util/cloud_provider.go:126"]},
        {"scope": "ns", "group": "", "resource": "persistentvolumeclaims",
         "verbs": ["list", "delete"], "names": None,
         "src": ["harvestermachine_controller.go:1661", "harvestermachine_controller.go:1674"]},
        {"scope": "ns", "group": "loadbalancer.harvesterhci.io", "resource": "loadbalancers",
         "verbs": ["get", "create", "delete"], "names": None,
         "src": ["harvestercluster_controller.go:693", "harvestercluster_controller.go:1398",
                 "harvestercluster_controller.go:722"]},
        {"scope": "image", "group": "harvesterhci.io", "resource": "virtualmachineimages",
         "verbs": ["list"], "names": None,
         "src": ["harvestermachine_controller.go:843"]},
        {"scope": "keypair", "group": "harvesterhci.io", "resource": "keypairs",
         "verbs": ["get"], "names": "keypair",
         "src": ["harvestermachine_controller.go:874"]},
        {"scope": "harvester-system", "group": "apps", "resource": "deployments",
         "verbs": ["get"], "names": ["harvester"],
         "src": ["harvestercluster_controller.go:1327"]},
        # v0.10.1 ne lit que ingress-expose ; rke2-traefik n'est lu qu'à partir
        # de la PR #276 (règles à relire pour cette version, décision 163)
        {"scope": "kube-system", "group": "", "resource": "services",
         "verbs": ["get"], "names": ["ingress-expose"],
         "src": ["util/cloud_provider.go:166"]},
        # create (harvestercluster_controller.go:317) jamais appelé : le
        # namespace du cluster existe déjà (créé par la console s'il manque)
        {"scope": "cluster", "group": "", "resource": "namespaces",
         "verbs": ["get"], "names": "namespace",
         "src": ["harvestercluster_controller.go:314"]},
        # adresses des machines : allocation dans `status.allocated` par Update
        {"scope": "cluster", "group": "loadbalancer.harvesterhci.io", "resource": "ippools",
         "verbs": ["get", "update"], "names": "pools",
         "src": ["harvestermachine_controller.go:1404", "harvestermachine_controller.go:1447",
                 "harvestermachine_controller.go:1504", "harvestermachine_controller.go:1528",
                 "harvestercluster_controller.go:1126"]},
        # suppression INCONDITIONNELLE du pool généré : un refus n'est pas un
        # NotFound, CAPHV relance la suppression du cluster indéfiniment
        {"scope": "cluster", "group": "loadbalancer.harvesterhci.io", "resource": "ippools",
         "verbs": ["delete"], "names": "generated-pool",
         "src": ["harvestercluster_controller.go:774"]},
        {"scope": "cluster", "group": "", "resource": "nodes",
         "verbs": ["list"], "names": None,
         "src": ["failure_domains.go:89"]},
        # create (util/cloud_provider.go:116) jamais appelé : la liaison est
        # créée d'avance par la console, sinon il faudrait `bind` (décision 166)
        {"scope": "cluster", "group": "rbac.authorization.k8s.io", "resource": "clusterrolebindings",
         "verbs": ["get"], "names": "cloud-provider",
         "src": ["util/cloud_provider.go:92"]},
    ],
}

# Appels de la source que l'identité ne reçoit pas, et pourquoi (relus en
# même temps que RULES ; une version nouvelle les relit aussi).
NOT_GRANTED = {
    "v0.10.1": [
        ("namespaces create", "harvestercluster_controller.go:317",
         "the cluster namespace is created by the console before the identity"),
        ("serviceaccounts create", "util/cloud_provider.go:81",
         "the cloud provider account is created by the console (decision 166)"),
        ("clusterrolebindings create", "util/cloud_provider.go:116",
         "would need `bind` on harvesterhci.io:cloudprovider; created by the console (decision 166)"),
        ("ippools get, update (load balancer pool)", "harvestercluster_controller.go:622, :684",
         "only with loadBalancerConfig.ipamType pool; the console declares dhcp"),
        ("ippools create, delete (inline pools)",
         "harvestercluster_controller.go:739, :795, :1164, :1205, :1453",
         "only for pools declared inline in the HarvesterCluster; the console references existing pools"),
    ],
}


def rules_for(version):
    """Les règles relues pour cette version de CAPHV, ou None."""
    return RULES.get(str(version or "").strip())


# ---------------------------------------------------------------------------
# Noms
# ---------------------------------------------------------------------------

def rfc1035_name(parts):
    """Recopie de `GenerateRFC1035Name` (util/util.go:190, v0.10.1) : jonction
    par `-`, minuscules, caractères hors [a-z0-9-] remplacés par `-`, tirets
    de bord retirés, préfixe `a-` si le premier caractère n'est pas une
    lettre, coupé à 63 (`maximumLabelLength`, util/util.go:45)."""
    name = "-".join(parts).lower()
    name = re.sub(r"[^a-z0-9-]", "-", name)
    name = name.strip("-")
    if name and not ("a" <= name[0] <= "z"):
        name = "a-" + name
    return name[:63]


def vm_namespace(namespace):
    """Namespace des VMs d'un cluster à identité restreinte."""
    return f"{namespace}{VM_NS_SUFFIX}"


def generated_pool(spec):
    """Le pool que CAPHV supprime à la suppression du cluster :
    `<namespace du HarvesterCluster>-<nom du HarvesterCluster>-ippool`. La
    console nomme le HarvesterCluster comme le cluster
    (capi_cluster.postprocess, infrastructureNamingStrategy)."""
    return rfc1035_name([spec["namespace"], spec["name"], "ippool"])


def pools_of(spec):
    names = [spec.get("ip_pool")] + list(spec.get("ip_pool_refs") or [])
    out = []
    for p in names:
        if p and p not in out:
            out.append(p)
    return out


def _resolve(rule, spec):
    """Noms d'une règle, ou None (tous). Une liste vide ou un nom vide lève
    ValueError : pour RBAC, `resourceNames: []` vaut TOUS les noms."""
    names = rule["names"]
    if names is None:
        return None
    if isinstance(names, list):
        out = list(names)
    else:
        out = {
            "namespace": [spec["target_namespace"]],
            "pools": pools_of(spec),
            "generated-pool": [generated_pool(spec)],
            "cloud-provider": [spec["name"]],
            "keypair": [str(spec.get("ssh_keypair") or "").partition("/")[2]],
        }[names]
    if not out or not all(out):
        raise ValueError(f"no name for the {rule['resource']} rule ({names}): "
                         "an empty list would grant every name")
    return out


def _scope_ns(rule, spec):
    scope = rule["scope"]
    if scope == "ns":
        return spec["target_namespace"]
    if scope == "image":
        return str(spec.get("image") or "").partition("/")[0]
    if scope == "keypair":
        return str(spec.get("ssh_keypair") or "").partition("/")[0]
    if scope == "cluster":
        return None
    return scope


def grants(spec, version):
    """[(namespace ou None, groupe, ressource, verbes, noms ou None, sources)]
    pour cette demande ; ValueError si la version n'a pas de règles."""
    rules = rules_for(version)
    if rules is None:
        raise ValueError(f"identity-rules-unknown: no rules for CAPHV {version}")
    out = []
    for r in rules:
        out.append((_scope_ns(r, spec), r["group"], r["resource"], list(r["verbs"]),
                    _resolve(r, spec), list(r["src"])))
    return out


# ---------------------------------------------------------------------------
# Objets
# ---------------------------------------------------------------------------

def labels(spec):
    return {GENERATED: "true", L_CLUSTER: spec["name"], L_NAMESPACE: spec["namespace"]}


def selector(name, namespace):
    return f"{L_CLUSTER}={name},{L_NAMESPACE}={namespace}"


def _rbac_rule(group, resource, verbs, names):
    r = {"apiGroups": [group], "resources": [resource], "verbs": list(verbs)}
    if names is not None:
        r["resourceNames"] = list(names)
    return r


def _meta(name, ns, spec):
    m = {"name": name, "labels": labels(spec)}
    if ns:
        m["namespace"] = ns
    return m


CLUSTER_ROLE_PREFIX = "harvester-ops-caphv-"


def side_role_name(spec):
    return f"{IDENTITY}-{spec['namespace']}"


def cluster_role_name(spec):
    return f"{CLUSTER_ROLE_PREFIX}{spec['namespace']}"


def side_namespaces(spec, version):
    """Namespaces des rôles annexes de cette demande (hors celui du
    cluster), triés."""
    return sorted({n for n, *_ in grants(spec, version) if n and n != spec["namespace"]})


def teardown(namespace, name, side=None):
    """Les objets de l'identité d'un cluster, par leur nom, dans l'ordre de
    suppression : [(genre, namespace ou None, nom)]. Le namespace des VMs
    (`vm_namespace`) part avec ses rôles et le compte du fournisseur de
    cloud. Le compte de l'identité vient en dernier : il porte la liste des
    namespaces annexes (`SIDE_NAMESPACES`), une reprise la relit tant qu'il
    reste quelque chose. `side` : ces namespaces (les namespaces fixes et
    celui des VMs sont toujours compris)."""
    spec = {"namespace": namespace, "name": name}
    crole = cluster_role_name(spec)
    vms = vm_namespace(namespace)
    out = [
        ("clusterrolebindings.rbac.authorization.k8s.io", None, name),
        ("clusterrolebindings.rbac.authorization.k8s.io", None, crole),
        ("clusterroles.rbac.authorization.k8s.io", None, crole),
    ]
    for n in sorted((set(side or ()) | set(FIXED_SIDE_NAMESPACES) | {vms}) - {namespace}):
        out += [("rolebindings.rbac.authorization.k8s.io", n, side_role_name(spec)),
                ("roles.rbac.authorization.k8s.io", n, side_role_name(spec))]
    out += [
        ("serviceaccounts", vms, name),
        ("namespaces", None, vms),
        ("secrets", namespace, TOKEN_SECRET),
        ("serviceaccounts", namespace, IDENTITY),
    ]
    return out


def reserved_name(name, namespace=None):
    """Un nom de cluster qui confondrait ses objets avec ceux de l'identité :
    la liaison `<nom>` du fournisseur de cloud prendrait le nom de celle du
    ClusterRole, ou un compte `caphv-identity...` celui de l'identité ; et un
    namespace en `-vms` serait celui des VMs d'un autre cluster, dont
    l'identité lirait les secrets."""
    name, namespace = str(name or ""), str(namespace or "")
    return (name in (IDENTITY, TOKEN_SECRET) or name.startswith(IDENTITY + "-")
            or name.startswith(CLUSTER_ROLE_PREFIX) or namespace.endswith(VM_NS_SUFFIX))


def docs(spec, version):
    """Objets à poser avec l'identité de la personne, avant les manifestes du
    cluster. ValueError si la version n'a pas de règles."""
    ns, name = spec["namespace"], spec["name"]
    sa_subject = {"kind": "ServiceAccount", "name": IDENTITY, "namespace": ns}
    by_ns = {}
    for scope_ns, group, resource, verbs, names, _src in grants(spec, version):
        by_ns.setdefault(scope_ns, []).append(_rbac_rule(group, resource, verbs, names))
    vms = spec["target_namespace"]
    identity_meta = dict(_meta(IDENTITY, ns, spec),
                         annotations={SIDE_NAMESPACES: ",".join(side_namespaces(spec, version))})
    out = [
        # namespace des VMs, propre au cluster, avant les rôles qu'il porte
        {"apiVersion": "v1", "kind": "Namespace",
         "metadata": {"name": vms, "labels": dict(labels(spec), **{MANAGED: "true"})}},
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": identity_meta},
        {"apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/service-account-token",
         "metadata": dict(_meta(TOKEN_SECRET, ns, spec),
                          annotations={"kubernetes.io/service-account.name": IDENTITY})},
    ]
    for scope_ns in sorted(by_ns, key=lambda k: (k is None, k != ns, k or "")):
        rules = by_ns[scope_ns]
        if scope_ns is None:
            rname = cluster_role_name(spec)
            out += [
                {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
                 "metadata": _meta(rname, None, spec), "rules": rules},
                {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
                 "metadata": _meta(rname, None, spec), "subjects": [sa_subject],
                 "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole",
                             "name": rname}},
            ]
            continue
        rname = IDENTITY if scope_ns == ns else side_role_name(spec)
        out += [
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
             "metadata": _meta(rname, scope_ns, spec), "rules": rules},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
             "metadata": _meta(rname, scope_ns, spec), "subjects": [sa_subject],
             "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": rname}},
        ]
    # compte et liaison du fournisseur de cloud, aux noms et au namespace
    # qu'attend CAPHV (GetCloudConfigB64(..., scope.Cluster.Name,
    # TargetNamespace), harvestercluster_controller.go:860) : CAPHV les
    # trouve par get
    out += [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": _meta(name, vms, spec)},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
         "metadata": _meta(name, None, spec),
         "subjects": [{"kind": "ServiceAccount", "name": name, "namespace": vms}],
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole",
                     "name": CLOUD_PROVIDER_ROLE}},
    ]
    return out


def kubeconfig(server, ca_b64, token, name="caphv-identity"):
    """Kubeconfig à jeton : un cluster, un utilisateur, un contexte. `server`
    est l'adresse de Harvester lue sur le cluster, jamais l'URL du kubeconfig
    de la personne (un relais Rancher pour une session SSO)."""
    if not re.match(r"^https://[A-Za-z0-9.\-\[\]:]+$", str(server or "")):
        raise ValueError("the Harvester address is not a plain https URL")
    base64.b64decode(ca_b64, validate=True)
    if not token or any(c.isspace() for c in token):
        raise ValueError("the identity token is empty")
    return (
        "apiVersion: v1\nkind: Config\n"
        f"clusters:\n- name: harvester\n  cluster:\n    server: {server}\n"
        f"    certificate-authority-data: {ca_b64}\n"
        f"users:\n- name: {name}\n  user:\n    token: {token}\n"
        f"contexts:\n- name: {name}\n  context:\n    cluster: harvester\n    user: {name}\n"
        f"    namespace: default\ncurrent-context: {name}\n"
    )


def access_reviews(spec, version):
    """Ce que la personne doit pouvoir faire pour poser l'identité, puis la
    retirer avec le cluster : [(namespace ou None, verbe, groupe, ressource,
    nom ou None)]. L'application côté serveur crée et modifie (create,
    patch) ; la suppression relit chaque objet par son nom (get, étiquettes
    du cluster vérifiées) puis le retire (delete). Créer un rôle exige de détenir
    les droits qu'il accorde (contrôle d'escalade du serveur), un accès par
    nom accordé ; lier le rôle du fournisseur de cloud exige `bind` sur lui."""
    out = []
    for d in docs(spec, version):
        group = d["apiVersion"].rpartition("/")[0]
        res = {"Namespace": "namespaces", "ServiceAccount": "serviceaccounts", "Secret": "secrets",
               "Role": "roles",
               "RoleBinding": "rolebindings", "ClusterRole": "clusterroles",
               "ClusterRoleBinding": "clusterrolebindings"}[d["kind"]]
        ns = d["metadata"].get("namespace")
        out.append((ns, "create", group, res, None))
        for verb in ("patch", "get", "delete"):
            out.append((ns, verb, group, res, d["metadata"]["name"]))
    for scope_ns, group, resource, verbs, names, _src in grants(spec, version):
        for v in verbs:
            for n in (names or [None]):
                out.append((scope_ns, v, group, resource, n))
    out.append((None, "bind", "rbac.authorization.k8s.io", "clusterroles", CLOUD_PROVIDER_ROLE))
    seen, uniq = set(), []
    for r in out:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq


# ---------------------------------------------------------------------------
# Constats
# ---------------------------------------------------------------------------

def force_target_namespace(spec, identity, default="default"):
    """Décision 164, revue le 05/11/2026 : avec l'identité restreinte, les
    VMs vont dans un namespace propre au cluster, `<namespace>-vms`
    (`vm_namespace`). La valeur par défaut est remplacée ; une autre valeur
    choisie est refusée par `findings`. Rend une copie."""
    spec = dict(spec)
    if identity == "restricted" and spec.get("target_namespace") in (None, "", default):
        spec["target_namespace"] = vm_namespace(spec.get("namespace"))
    return spec


def findings(spec, version, identity, bindings=None, vm_namespace_labels=None):
    """Constats propres à l'identité : [{"code","level","facts"}].
    `bindings` : {nom: étiquettes} des ClusterRoleBindings existantes au nom
    du cluster ; `vm_namespace_labels` : étiquettes du namespace des VMs s'il
    existe déjà, None sinon (lectures faites par l'appelant)."""
    out = []
    if identity != "restricted":
        return out
    if rules_for(version) is None:
        out.append({"code": "identity-rules-unknown", "level": "block",
                    "facts": {"version": version or "?", "known": ", ".join(sorted(RULES))}})
    if reserved_name(spec.get("name"), spec.get("namespace")):
        out.append({"code": "identity-name-reserved", "level": "block",
                    "facts": {"name": spec.get("name"), "identity": IDENTITY,
                              "prefix": CLUSTER_ROLE_PREFIX}})
    if spec.get("target_namespace") != vm_namespace(spec.get("namespace")):
        out.append({"code": "target-namespace-forced", "level": "block",
                    "facts": {"target_namespace": spec.get("target_namespace"),
                              "namespace": vm_namespace(spec.get("namespace"))}})
    if len(vm_namespace(spec.get("namespace") or "")) > 63:
        # revue finale n2 : `<namespace>-vms` dépasserait la limite d'un nom
        # de namespace (RFC 1123), l'identité serait refusée à la création
        out.append({"code": "vm-namespace-too-long", "level": "block",
                    "facts": {"namespace": vm_namespace(spec.get("namespace") or ""), "max": 63}})
    if vm_namespace_labels is not None and (vm_namespace_labels.get(L_CLUSTER) != spec.get("name")
                                            or vm_namespace_labels.get(L_NAMESPACE) != spec.get("namespace")):
        # namespace des VMs déjà là, pas à ce cluster : l'identité aurait les
        # VMs et les secrets de quelqu'un d'autre
        out.append({"code": "vm-namespace-taken", "level": "block",
                    "facts": {"namespace": vm_namespace(spec.get("namespace"))}})
    lb = (bindings or {}).get(spec.get("name"))
    if lb is not None and (lb.get(L_NAMESPACE) != spec.get("namespace")
                           or lb.get(L_CLUSTER) != spec.get("name")):
        # ClusterRoleBinding au nom du cluster, à quelqu'un d'autre : l'écraser
        # retirerait au cluster voisin son fournisseur de cloud
        out.append({"code": "cloud-provider-binding-taken", "level": "block",
                    "facts": {"binding": spec.get("name")}})
    return out


def caphv_version(stack, desc=None):
    """Version de CAPHV qui agira avec l'identité : celle installée sur le
    cluster, à défaut celle du paquet."""
    for p in (stack or {}).get("providers") or []:
        if p.get("type") == "infrastructure" and p.get("name") == "harvester" and p.get("version"):
            return p["version"]
    for p in (desc or {}).get("providers") or []:
        if p.get("type") == "infrastructure" and p.get("name") == "harvester":
            return p.get("version")
    return None
