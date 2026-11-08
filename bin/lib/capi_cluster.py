"""harvester-ops : créer un cluster RKE2 sur Harvester par Cluster API (v1.48.0).

Décisions pures, sans accès au cluster : ce que l'exploitant peut
demander (liste blanche), comment cela devient des options du générateur
`caphv-generate` (livré dans `bin/`, tiré du dépôt CAPHV), ce qu'on corrige
dans ce qu'il produit, les calculs d'adresses à partir d'un IPPool
Harvester, le contrôle préalable, et la lecture de la progression d'un
cluster en v1beta1 comme en v1beta2.

Voir docs/design/2026-09-25-creation-cluster-capi.md.

Bibliothèque standard seulement : le script tourne sur un hôte airgap.
"""

import copy
from pathlib import Path
import ipaddress
import re

# ---------------------------------------------------------------------------
# Ce que l'exploitant peut demander
# ---------------------------------------------------------------------------

NAME_RE = re.compile(r"^[a-z]([-a-z0-9]{0,38}[a-z0-9])?$")
K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
NS_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?/[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
# Une image se désigne par son nom affiché, libre (`x86_64`, majuscules) :
# c'est ce que le générateur attend.
IMAGE_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?/[^/\s]{1,253}$")
QUANTITY_RE = re.compile(r"^[1-9][0-9]{0,5}(Mi|Gi|Ti)$")
VERSION_RE = re.compile(r"^v1\.[0-9]{2}\.[0-9]{1,3}$")

CNIS = ("calico", "canal", "cilium", "none")
ENCAPSULATIONS = ("VXLAN", "VXLANCrossSubnet", "IPIP", "IPIPCrossSubnet", "None")
BGP_MODES = ("Enabled", "Disabled")

# Espaces de noms à ne jamais prendre : ceux de Harvester, de Rancher, des
# fournisseurs Cluster API. Le générateur y écrirait une ClusterClass.
RESERVED_NAMESPACES = {
    "default", "kube-system", "kube-public", "kube-node-lease", "longhorn-system",
    "harvester-system", "harvester-public", "cattle-system", "cattle-capi-system",
    "cattle-turtles-system", "cattle-fleet-system", "cattle-fleet-local-system",
    "fleet-default", "fleet-local", "caphv-system", "rke2-bootstrap-system",
    "rke2-control-plane-system", "capi-system", "cert-manager",
}

# Valeurs par défaut, pour un cluster de test minimal ; le formulaire les
# remplace par ce que le cluster propose vraiment.
DEFAULTS = {
    "cp_replicas": 1, "worker_replicas": 1, "cpu": 2, "memory": "4Gi",
    "disk_size": "40Gi", "ssh_user": "sles", "cni": "calico",
    "pod_cidr": "10.42.0.0/16", "service_cidr": "10.43.0.0/16",
    "cni_mtu": 1500, "cni_encapsulation": "VXLANCrossSubnet", "cni_bgp": "Disabled",
    "fleet_branch": "main", "target_namespace": "default", "rancher_import": False,
    "rancher_external": False, "offline": False,
}

# Option du formulaire -> option du générateur. Tout le reste est refusé :
# le générateur accepte `--apply`, qui appliquerait avec le kubeconfig
# ambiant du service.
FLAGS = {
    "name": "--name", "namespace": "--namespace", "k8s_version": "--k8s-version",
    "cp_replicas": "--cp-replicas", "worker_replicas": "--worker-replicas",
    "cpu": "--cpu", "memory": "--memory", "disk_size": "--disk-size",
    "image": "--image", "ssh_user": "--ssh-user", "ssh_keypair": "--ssh-keypair",
    "network": "--network", "gateway": "--gateway", "subnet_mask": "--subnet-mask",
    "ip_pool": "--ip-pool", "cni": "--cni", "pod_cidr": "--pod-cidr",
    "cni_mtu": "--cni-mtu", "cni_encapsulation": "--cni-encapsulation",
    "cni_bgp": "--cni-bgp", "fleet_repo": "--fleet-addon-repo",
    "fleet_branch": "--fleet-addon-branch",
}
LIST_FLAGS = {"dns": "--dns", "ip_pool_refs": "--ip-pool-refs"}
# Options traitées par la console elle-même, après le générateur.
# 2.8.0 : `rancher_external` (import dans le Rancher de la session,
# bin/lib/rancher_import.py) et `oidc` (API du cluster enfant,
# bin/lib/child_oidc.py). `offline` et `offline_artifacts` : cluster amorcé
# sans Internet (bin/lib/rke2_offline.py).
CONSOLE_OPTIONS = {"extra_disk_size", "extra_disk_class", "target_namespace",
                   "rancher_import", "extra_networks", "service_cidr", "registries",
                   "rancher_external", "oidc", "offline", "offline_artifacts"}
OFFLINE_ARTIFACTS = ("fetch", "image")
KNOWN = set(FLAGS) | set(LIST_FLAGS) | CONSOLE_OPTIONS
# Options entières : `normalize` les convertit, le rendu d'un modèle de
# cluster (deploy_plans) donne ce type à une chaîne qui n'est qu'une variable.
INT_OPTIONS = ("cp_replicas", "worker_replicas", "cpu", "cni_mtu")


def _as_list(v):
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return [str(x).strip() for x in v if str(x).strip()]


# 2.6.0 : registres du cluster enfant (liste ordonnée, préféré d'abord).
# Mêmes règles de nom et d'hôte que bin/lib/registries.py (qui importe ce
# module : les règles vivent ici pour éviter l'import circulaire).
REGISTRY_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,28}[a-z0-9])?$")
REGISTRY_HOST_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
    r"|\d{1,3}(?:\.\d{1,3}){3})(?::\d{1,5})?$")
REGISTRY_ENTRY_KEYS = {"registry", "mirrors", "no_fallback"}


def normalize_registry_entries(v):
    """`registries` de la demande : liste de noms ou de {registry, mirrors,
    no_fallback}, rendue en [{registry[, mirrors][, no_fallback]}] dans
    l'ordre (le premier registre est préféré). `mirrors` absent = ceux de la
    déclaration ; `no_fallback` absent = décision de la console
    (registries.fallback_decision). ValueError sur une forme fausse ; les
    noms et les hôtes sont contrôlés par `validate`."""
    if v in (None, "", []):
        return []
    if isinstance(v, str):
        v = [x.strip() for x in v.split(",") if x.strip()]
    if not isinstance(v, list):
        raise ValueError("registries: a list of registry names or {registry, mirrors, no_fallback}")
    out = []
    for e in v:
        if isinstance(e, str):
            e = {"registry": e}
        if not isinstance(e, dict) or set(e) - REGISTRY_ENTRY_KEYS or not isinstance(e.get("registry"), str):
            raise ValueError("registries: a list of registry names or {registry, mirrors, no_fallback}")
        item = {"registry": e["registry"].strip()}
        m = e.get("mirrors")
        if m is not None:
            if not isinstance(m, list) or not all(isinstance(x, str) for x in m):
                raise ValueError("registries.mirrors: a list of registry hosts")
            item["mirrors"] = list(dict.fromkeys(x.strip() for x in m if x.strip()))
        nf = e.get("no_fallback")
        if nf is not None:
            if nf not in (True, False, "true", "false"):
                raise ValueError("registries.no_fallback: true or false")
            item["no_fallback"] = nf in (True, "true")
        out.append(item)
    return out


def normalize(raw):
    """La demande nettoyée, les valeurs par défaut posées. Lève ValueError
    sur une option inconnue (liste blanche)."""
    raw = dict(raw or {})
    unknown = sorted(k for k in raw if k not in KNOWN)
    if unknown:
        raise ValueError("unknown option(s): " + ", ".join(unknown))
    spec = dict(DEFAULTS)
    for k, v in raw.items():
        if v is None or v == "":
            continue
        spec[k] = v
    for k in INT_OPTIONS:
        if k in spec:
            try:
                spec[k] = int(spec[k])
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be an integer") from None
    spec["dns"] = _as_list(spec.get("dns"))
    spec["ip_pool_refs"] = _as_list(spec.get("ip_pool_refs"))
    spec["extra_networks"] = _as_list(spec.get("extra_networks"))
    spec["rancher_import"] = spec.get("rancher_import") in (True, "true", "1", 1, "on")
    spec["rancher_external"] = spec.get("rancher_external") in (True, "true", "1", 1, "on")
    spec["offline"] = spec.get("offline") in (True, "true", "1", 1, "on")
    if spec["offline"]:
        spec.setdefault("offline_artifacts", "fetch")
    else:
        spec.pop("offline_artifacts", None)
    if "oidc" in spec and not isinstance(spec["oidc"], dict):
        raise ValueError("oidc: an object (issuer_url, audience, ...)")
    regs = normalize_registry_entries(spec.pop("registries", None))
    if regs:
        spec["registries"] = regs
    if not spec.get("namespace") and spec.get("name"):
        spec["namespace"] = spec["name"]
    return spec


def validate(spec):
    """Les erreurs de forme, avant de lire le cluster : [(option, message)]."""
    errs = []

    def need(key, ok, msg):
        if not ok:
            errs.append((key, msg))

    name = spec.get("name") or ""
    need("name", bool(NAME_RE.match(name)),
         "lowercase letters, digits and '-', starting with a letter, 1 to 40 characters")
    ns = spec.get("namespace") or ""
    need("namespace", bool(K8S_NAME_RE.match(ns)), "not a valid namespace name")
    need("namespace", ns not in RESERVED_NAMESPACES,
         f"'{ns}' belongs to Harvester, Rancher or Cluster API")
    tns = spec.get("target_namespace") or ""
    need("target_namespace", bool(K8S_NAME_RE.match(tns)), "not a valid namespace name")
    v = spec.get("k8s_version") or ""
    need("k8s_version", bool(VERSION_RE.match(v)), "expected v1.MINOR.PATCH, e.g. v1.33.5")
    need("cp_replicas", 1 <= spec.get("cp_replicas", 0) <= 7, "between 1 and 7")
    need("worker_replicas", 0 <= spec.get("worker_replicas", -1) <= 50, "between 0 and 50")
    need("cpu", 1 <= spec.get("cpu", 0) <= 64, "between 1 and 64")
    for key in ("memory", "disk_size"):
        need(key, bool(QUANTITY_RE.match(str(spec.get(key) or ""))), "a size such as 8Gi")
    need("image", bool(IMAGE_RE.match(str(spec.get("image") or ""))),
         "expected namespace/display name")
    for key in ("ssh_keypair", "network"):
        need(key, bool(NS_NAME_RE.match(str(spec.get(key) or ""))), "expected namespace/name")
    for net in spec.get("extra_networks") or []:
        need("extra_networks", bool(NS_NAME_RE.match(net)), f"'{net}': expected namespace/name")
    need("ssh_user", bool(re.match(r"^[a-z_][a-z0-9_-]{0,31}$", spec.get("ssh_user") or "")),
         "a Linux user name")
    need("ip_pool", bool(K8S_NAME_RE.match(spec.get("ip_pool") or "")), "an IPPool name")
    for p in spec.get("ip_pool_refs") or []:
        need("ip_pool_refs", bool(K8S_NAME_RE.match(p)), f"'{p}': an IPPool name")
    for key in ("gateway",):
        need(key, _is_ipv4(spec.get(key)), "an IPv4 address")
    need("subnet_mask", _mask_prefix(spec.get("subnet_mask")) is not None,
         "a contiguous IPv4 mask such as 255.255.255.0")
    need("dns", bool(spec.get("dns")), "at least one DNS server (the provider refuses none)")
    for d in spec.get("dns") or []:
        need("dns", _is_ipv4(d), f"'{d}': an IPv4 address")
    need("cni", spec.get("cni") in CNIS, "calico, canal, cilium or none")
    for key in ("pod_cidr", "service_cidr"):
        need(key, _net(spec.get(key)) is not None, "a CIDR such as 10.42.0.0/16")
    need("cni_mtu", 576 <= spec.get("cni_mtu", 0) <= 9000, "between 576 and 9000")
    need("cni_encapsulation", spec.get("cni_encapsulation") in ENCAPSULATIONS,
         ", ".join(ENCAPSULATIONS))
    need("cni_bgp", spec.get("cni_bgp") in BGP_MODES, "Enabled or Disabled")
    repo = spec.get("fleet_repo")
    if repo:
        need("fleet_repo", bool(re.match(r"^(https?://|ssh://|git@)[^\s]+$", repo)),
             "a git URL (https, ssh)")
        need("fleet_branch", bool(re.match(r"^[A-Za-z0-9._/-]{1,100}$", spec.get("fleet_branch") or "")),
             "a branch name")
    size = spec.get("extra_disk_size")
    if size:
        need("extra_disk_size", bool(QUANTITY_RE.match(str(size))), "a size such as 20Gi")
        need("extra_disk_class", bool(K8S_NAME_RE.match(spec.get("extra_disk_class") or "")),
             "a storage class")
    if spec.get("offline"):
        need("offline_artifacts", spec.get("offline_artifacts") in OFFLINE_ARTIFACTS, "fetch or image")
    seen = set()
    for e in spec.get("registries") or []:
        n = e.get("registry") or ""
        need("registries", bool(REGISTRY_NAME_RE.match(n)), f"'{n[:40]}': a registry name (RFC 1123, 30 at most)")
        need("registries", n not in seen, f"'{n}' listed twice")
        seen.add(n)
        for m in e.get("mirrors") or []:
            need("registries", bool(REGISTRY_HOST_RE.match(m)), f"'{m[:60]}': a registry host (host[:port])")
    return errs


def to_argv(spec, harvester_kubeconfig):
    """Les options du générateur, dans un ordre stable. Jamais `--apply`."""
    argv = []
    for key, flag in FLAGS.items():
        v = spec.get(key)
        if v is None or v == "":
            continue
        if key in ("fleet_branch",) and not spec.get("fleet_repo"):
            continue
        if key in ("cni_mtu", "cni_encapsulation", "cni_bgp") and not spec.get("fleet_repo"):
            # le générateur ne les applique qu'en mode Fleet
            continue
        argv += [flag, str(v)]
    for key, flag in LIST_FLAGS.items():
        if spec.get(key):
            argv += [flag, ",".join(spec[key])]
    if spec.get("extra_disk_size"):
        argv += ["--extra-disk", f"{spec['extra_disk_size']}:{spec['extra_disk_class']}"]
    argv += ["--harvester-kubeconfig", str(harvester_kubeconfig)]
    return argv


# ---------------------------------------------------------------------------
# Ce qu'on corrige dans ce que le générateur produit
# ---------------------------------------------------------------------------

HARVESTER_KINDS = {"HarvesterClusterTemplate", "HarvesterMachineTemplate",
                   "HarvesterCluster", "HarvesterMachine"}
AUTO_IMPORT = "cluster-api.cattle.io/rancher-auto-import"
# Posé sur chaque objet rendu : la suppression retrouve ce que la console a
# créé (le secret d'identité porte un kubeconfig du cluster Harvester).
GENERATED = "harvester-ops.io/generated"
GENERATED_KINDS = (
    "clusterclasses.cluster.x-k8s.io",
    "harvesterclustertemplates.infrastructure.cluster.x-k8s.io",
    "harvestermachinetemplates.infrastructure.cluster.x-k8s.io",
    "rke2controlplanetemplates.controlplane.cluster.x-k8s.io",
    "rke2configtemplates.bootstrap.cluster.x-k8s.io",
    "clusterresourcesets.addons.cluster.x-k8s.io",
    "machinehealthchecks.cluster.x-k8s.io",
    "configmaps", "secrets",
)
# v1.52.0 : kube-vip dans chaque cluster créé. Le fournisseur de cloud
# Harvester que CAPHV y pose attend kube-vip pour annoncer l'adresse des
# services LoadBalancer ; CAPHV ne le pose pas, et sans lui ces services
# restent sans adresse (vu sur harv1 : podinfo « pending » dix minutes, la
# release en échec). Même sélecteur que le fournisseur de cloud.
KUBE_VIP_MANIFEST = Path(__file__).resolve().parent / "kube-vip.yaml"
KUBE_VIP_CM = "kube-vip-addon"
KUBE_VIP_CRS = "crs-kube-vip"
K_CRS = "clusterresourcesets.addons.cluster.x-k8s.io"
CCM_SELECTOR = {"ccm": "external"}


def kube_vip_docs(namespace):
    """Le ConfigMap qui porte kube-vip et le ClusterResourceSet qui l'applique
    aux clusters du fournisseur de cloud Harvester de cet espace de noms."""
    labels = {GENERATED: "true"}
    return [
        {"apiVersion": "v1", "kind": "ConfigMap",
         "metadata": {"name": KUBE_VIP_CM, "namespace": namespace, "labels": labels},
         "data": {"kube-vip.yaml": KUBE_VIP_MANIFEST.read_text()}},
        {"apiVersion": "addons.cluster.x-k8s.io/v1beta1", "kind": "ClusterResourceSet",
         "metadata": {"name": KUBE_VIP_CRS, "namespace": namespace, "labels": dict(labels)},
         "spec": {"clusterSelector": {"matchLabels": dict(CCM_SELECTOR)},
                  "resources": [{"kind": "ConfigMap", "name": KUBE_VIP_CM}],
                  "strategy": "Reconcile"}},
    ]


# 2.5.0 : le HarvesterCluster porte le nom du cluster (et non
# `<cluster>-<aléa>`, gabarit par défaut du cœur, exp/topology/desiredstate,
# CAPI v1.13) : CAPHV supprime à la fin le pool `<ns>-<nom du
# HarvesterCluster>-ippool`, que l'identité restreinte doit pouvoir nommer
# d'avance (capi_identity.generated_pool).
INFRA_NAMING = {"template": "{{ .cluster.name }}"}
# 2.5.0 : origine d'un cluster créé par la chaîne d'un plan (reprise, refus
# d'un nom déjà pris par autre chose)
ORIGIN_LABELS = {"plan": "harvester-ops.io/plan", "template": "harvester-ops.io/plan-template"}
ORIGIN_ANNOTATIONS = {"run": "harvester-ops.io/plan-run"}

INFRA_OLD = "infrastructure.cluster.x-k8s.io/v1alpha1"
INFRA_NEW = "infrastructure.cluster.x-k8s.io/v1beta1"


def _retarget_refs(node):
    """Toute référence de gabarit `infrastructure.../v1alpha1` passe en
    v1beta1 (ClusterClass : `ref` / `templateRef`)."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "apiVersion" and v == INFRA_OLD:
                node[k] = INFRA_NEW
            else:
                _retarget_refs(v)
    elif isinstance(node, list):
        for v in node:
            _retarget_refs(v)


RKE2_TEMPLATE_KINDS = ("RKE2ControlPlaneTemplate", "RKE2ConfigTemplate")


def _apply_registries(docs, registries):
    """2.6.0 (tâche 9) : configuration de registres privés de RKE2 posée dans
    les DEUX gabarits, plan de contrôle et workers (un worker sans elle
    tirerait d'Internet) : `registries.yaml`, une CA par registre et le
    fichier `config.yaml.d` ajoutés aux `files` existants (jamais
    `privateRegistriesConfig`, voir registries.rke2_config), Secrets ajoutés
    aux documents tels que rendus par registries.rke2_config."""
    if not registries:
        return docs
    files = registries.get("files") or []
    for d in docs:
        if d.get("kind") not in RKE2_TEMPLATE_KINDS:
            continue
        tpl = d.setdefault("spec", {}).setdefault("template", {})
        inner = tpl["spec"] = tpl.get("spec") or {}
        if files:
            have = [f for f in inner.get("files") or [] if f.get("path") not in {x["path"] for x in files}]
            inner["files"] = have + copy.deepcopy(files)
    return docs + [copy.deepcopy(s) for s in registries.get("secrets") or []]


def _apply_oidc(docs, spec):
    """2.8.0 (point 228) : configuration d'authentification structurée posée
    dans le SEUL gabarit du plan de contrôle (l'API ne tourne pas sur les
    workers). ValueError si les réglages sont refusés ou si le gabarit porte
    déjà un argument ou un fichier du même nom (`oidc-arg-conflict`)."""
    import child_oidc
    cfg, found = child_oidc.check(spec["oidc"], spec.get("k8s_version"))
    bad = child_oidc.blocking(found)
    if bad:
        raise ValueError(f"{bad[0]['code']}: the oidc option is refused")
    patch = child_oidc.template_patch(cfg, spec.get("k8s_version"))
    for d in docs:
        if d.get("kind") == "Cluster":
            d.setdefault("metadata", {}).setdefault("annotations", {})[child_oidc.ANNOTATION] = child_oidc.public(cfg)
        if d.get("kind") == "RKE2ControlPlaneTemplate":
            tpl = d.setdefault("spec", {}).setdefault("template", {})
            tpl["spec"] = child_oidc.apply_patch(tpl.get("spec") or {}, patch)
    return docs


def renew_templates(docs, spec, whats, registries=None):
    """2.8.0 (renouvellement, point 233) : gabarits RKE2 d'un cluster
    existant portés à la demande `spec` (copie) pour les gestes `whats` :
    `registries` et `ca`, fichiers de registres retirés puis ceux de
    `registries` ({files} de registries.rke2_config) posés dans les deux
    gabarits (`_apply_registries`) ; `oidc`, fichier et argument de
    child_oidc retirés du gabarit du plan de contrôle puis reposés
    (`_apply_oidc`) si la demande en porte. Les autres documents passent
    tels quels. Même code que la création, un seul endroit."""
    import child_oidc
    import registries as rg
    out = copy.deepcopy(docs)

    def managed(path):
        return path in (rg.REGISTRIES_FILE, rg.NO_FALLBACK_FILE) or path.startswith(rg.CA_DIR + "/")
    for d in out:
        if d.get("kind") not in RKE2_TEMPLATE_KINDS:
            continue
        inner = d.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {})
        if {"registries", "ca"} & set(whats):
            inner["files"] = [f for f in inner.get("files") or [] if not managed(f.get("path") or "")]
        if "oidc" in whats and d.get("kind") == "RKE2ControlPlaneTemplate":
            inner["files"] = [f for f in inner.get("files") or [] if f.get("path") != child_oidc.FILE]
            api = (inner.get("serverConfig") or {}).get("kubeAPIServer") or {}
            if "extraArgs" in api:
                api["extraArgs"] = [a for a in api["extraArgs"]
                                    if str(a).lstrip("-").split("=", 1)[0] != child_oidc.ARG]
        if not inner.get("files"):
            inner.pop("files", None)
    if "oidc" in whats and spec.get("oidc") is not None:
        _apply_oidc(out, spec)
    if {"registries", "ca"} & set(whats) and registries:
        out = _apply_registries(out, {"files": registries.get("files") or []})
    return out


def _apply_offline(docs, offline):
    """2.8.0 (point 237) : cluster amorcé sans Internet, dans les DEUX
    gabarits RKE2 : fichiers (netrc et CA, depuis un Secret), commandes de
    récupération ajoutées à la fin de `preRKE2Commands`, et
    `agentConfig.airGapped: true` (CAPRKE2 lance alors /opt/install.sh sur
    /opt/rke2-artifacts). `offline` = {files, preRKE2Commands, secrets}
    rendu par l'appelant (rke2_offline.template_patch), vide en mode
    `image` (l'image de VM porte déjà les fichiers)."""
    if offline is None:
        return docs
    for d in docs:
        if d.get("kind") not in RKE2_TEMPLATE_KINDS:
            continue
        inner = d.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {})
        paths = {f["path"] for f in offline.get("files") or []}
        if paths:
            inner["files"] = [f for f in inner.get("files") or [] if f.get("path") not in paths] \
                + copy.deepcopy(offline["files"])
        if offline.get("preRKE2Commands"):
            inner["preRKE2Commands"] = list(inner.get("preRKE2Commands") or []) + list(offline["preRKE2Commands"])
        inner.setdefault("agentConfig", {})["airGapped"] = True
    return docs + [copy.deepcopy(s) for s in offline.get("secrets") or []]


def postprocess(docs, spec, origin=None, registries=None, offline=None):
    """Corrige les documents produits par le générateur ; rend une copie.

    - **v1alpha1 -> v1beta1 pour les objets Harvester.** Le générateur écrit
      encore l'ancienne version ; CAPHV v0.10 la convertit en v1beta1 en y
      ajoutant `spec.template.metadata: {}`, que son propre schéma refuse
      ensuite quand la topologie recopie le gabarit (vu sur harv1).
    - **Pas d'import automatique dans Rancher**, sauf demande : sur Harvester,
      le Rancher intégré n'est pas fait pour gérer d'autres clusters.
    - **Espace de noms des VMs** (`targetNamespace`), réseaux
      supplémentaires : des variables de la ClusterClass que le générateur
      ne sait pas poser.
    - **CIDR des services** : le générateur ne le pose pas (RKE2 prend
      10.43.0.0/16).
    - **Étiquette `GENERATED`** sur tout ce qui n'est ni l'espace de noms ni
      le Cluster, pour que la suppression n'oublie rien.
    - **kube-vip** (v1.52.0), sans quoi aucun service LoadBalancer du
      cluster n'obtient d'adresse.
    - **Nom du HarvesterCluster** = nom du cluster (2.5.0, INFRA_NAMING).
    - **Origine** (2.5.0) : étiquettes et annotation d'un cluster créé par un
      plan, passées par `origin` ({plan, template, run}), jamais par la
      demande, qui reste une liste blanche.
    - **Registres** (2.6.0) : `registries` = {files, secrets} rendu
      par registries.rke2_config, passé par l'appelant (jamais lu dans la
      demande brute) ; voir `_apply_registries`.
    - **OIDC** (2.8.0) : `_apply_oidc`, gabarit du plan de contrôle seul.
    - `rancher_external` (2.8.0) ne change aucun document : l'import se fait
      après la création (harvester-capi rancher-import).
    - **Sans Internet** (2.8.0) : `offline` rendu par l'appelant, voir
      `_apply_offline`.
    """
    out = []
    for d in docs:
        if not d:
            continue
        d = copy.deepcopy(d)
        _retarget_refs(d)
        if d.get("kind") not in ("Namespace", "Cluster"):
            meta = d.setdefault("metadata", {})
            meta["labels"] = dict(meta.get("labels") or {}, **{GENERATED: "true"})
        if d.get("kind") == "ClusterClass":
            d.setdefault("spec", {})["infrastructureNamingStrategy"] = dict(INFRA_NAMING)
        if d.get("kind") == "Cluster":
            labels = d.setdefault("metadata", {}).setdefault("labels", {})
            for k, label in ORIGIN_LABELS.items():
                if (origin or {}).get(k):
                    labels[label] = origin[k]
            for k, ann in ORIGIN_ANNOTATIONS.items():
                if (origin or {}).get(k):
                    d["metadata"].setdefault("annotations", {})[ann] = origin[k]
            if spec.get("rancher_import"):
                labels[AUTO_IMPORT] = "true"
            else:
                labels.pop(AUTO_IMPORT, None)
            topo = d.get("spec", {}).get("topology", {})
            for var in topo.get("variables", []):
                if var.get("name") == "targetNamespace":
                    var["value"] = spec.get("target_namespace") or "default"
                elif var.get("name") == "vmNetworks" and spec.get("extra_networks"):
                    var["value"] = list(var.get("value") or []) + [
                        n for n in spec["extra_networks"] if n not in (var.get("value") or [])]
            svc = spec.get("service_cidr")
            if svc and svc != DEFAULTS["service_cidr"]:
                d["spec"].setdefault("clusterNetwork", {}).setdefault("services", {})[
                    "cidrBlocks"] = [svc]
        out.append(d)
    if spec.get("oidc") is not None:
        _apply_oidc(out, spec)
    cluster = next((d for d in out if d.get("kind") == "Cluster"), None)
    have = {(d.get("kind"), (d.get("metadata") or {}).get("name")) for d in out}
    if cluster and ("ClusterResourceSet", KUBE_VIP_CRS) not in have:
        out.extend(kube_vip_docs(cluster["metadata"].get("namespace") or "default"))
    if spec.get("offline"):
        out = _apply_offline(out, offline if offline is not None else {})
    return _apply_registries(out, registries)


def parse_origin(text):
    """`plan=<p>,template=<m>,run=<id>` -> dict. Clés connues seulement ;
    `plan` et `template` sont des noms RFC 1123 (valeurs d'étiquette), `run`
    un identifiant d'action (12 hexadécimaux). ValueError sinon."""
    out = {}
    for part in [x for x in str(text or "").split(",") if x.strip()]:
        k, sep, v = part.strip().partition("=")
        if not sep or k not in ORIGIN_LABELS and k not in ORIGIN_ANNOTATIONS:
            raise ValueError(f"--origin: unknown key '{k}' (plan, template, run)")
        if k in out:
            raise ValueError(f"--origin: '{k}' given twice")
        ok = re.match(r"^[0-9a-f]{12}$", v) if k == "run" else K8S_NAME_RE.match(v)
        if not ok:
            raise ValueError(f"--origin: invalid value for '{k}'")
        out[k] = v
    return out


def origin_of(cluster):
    """Origine lue sur un Cluster (inverse de `postprocess`) : {plan,
    template, run}, clés absentes omises."""
    meta = (cluster or {}).get("metadata") or {}
    labels, ann = meta.get("labels") or {}, meta.get("annotations") or {}
    out = {k: labels[v] for k, v in ORIGIN_LABELS.items() if labels.get(v)}
    out.update({k: ann[v] for k, v in ORIGIN_ANNOTATIONS.items() if ann.get(v)})
    return out


def run_mismatch(obj, expected):
    """Revue finale 2.7.0 (m-b) : le Défaire juge « à nous » puis lance la
    suppression par nom ; l'outil relit la marque juste avant d'écrire.
    None si `expected` est vide ou si `obj` porte cette exécution, sinon le
    refus `undo-not-ours` (rien n'est supprimé)."""
    if not expected:
        return None
    got = origin_of(obj).get("run")
    if got == expected:
        return None
    return (f"undo-not-ours: recreated by run {got}, left untouched" if got
            else f"undo-not-ours: no mark of run {expected} (recreated by hand?), left untouched")


def masked(docs):
    """Pour un aperçu : le secret d'identité porte le kubeconfig Harvester."""
    out = []
    for d in docs:
        d = copy.deepcopy(d)
        if d.get("kind") == "Secret":
            for field in ("data", "stringData"):
                if field in d:
                    d[field] = {k: "<hidden>" for k in d[field]}
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Adresses
# ---------------------------------------------------------------------------

def _is_ipv4(v):
    try:
        ipaddress.IPv4Address(str(v))
        return True
    except (ValueError, ipaddress.AddressValueError):
        return False


def _net(v):
    try:
        return ipaddress.IPv4Network(str(v), strict=False)
    except (ValueError, TypeError):
        return None


def _mask_prefix(mask):
    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
    except (ValueError, TypeError):
        return None


def mask_of(prefixlen):
    return str(ipaddress.IPv4Network(f"0.0.0.0/{prefixlen}").netmask)


def pool_info(pool):
    """Ce qu'un IPPool Harvester (loadbalancer.harvesterhci.io) dit d'utile
    au formulaire : sous-réseau, passerelle, masque, plages, places."""
    spec = (pool or {}).get("spec") or {}
    st = (pool or {}).get("status") or {}
    ranges = []
    size = 0
    for r in spec.get("ranges") or []:
        net = _net(r.get("subnet"))
        start = r.get("rangeStart") or (str(next(net.hosts())) if net else None)
        end = r.get("rangeEnd") or (str(net.broadcast_address - 1) if net else None)
        n = 0
        if start and end and _is_ipv4(start) and _is_ipv4(end):
            n = int(ipaddress.IPv4Address(end)) - int(ipaddress.IPv4Address(start)) + 1
        size += max(0, n)
        ranges.append({"subnet": r.get("subnet"), "gateway": r.get("gateway"),
                       "start": start, "end": end, "size": max(0, n)})
    first = ranges[0] if ranges else {}
    net = _net(first.get("subnet"))
    total = st.get("total") if isinstance(st.get("total"), int) else size
    # Le compteur « available » de Harvester dérive : vu à 24 sur un pool de
    # 16 adresses après quelques suppressions de clusters. La table des
    # adresses allouées fait foi (absente quand rien n'est alloué).
    available = max(0, total - len(st.get("allocated") or {}))
    return {
        "name": (pool or {}).get("metadata", {}).get("name"),
        "subnet": first.get("subnet"), "gateway": first.get("gateway"),
        "mask": mask_of(net.prefixlen) if net else None,
        "ranges": ranges, "total": total, "available": available,
        "used": sorted((st.get("allocated") or {}).keys()),
    }


def addresses_needed(spec):
    """Une adresse par machine, plus une pour la machine de remplacement
    qu'un déploiement progressif crée avant d'en retirer une (maxSurge 1)."""
    return spec.get("cp_replicas", 0) + spec.get("worker_replicas", 0) + 1


def cidr_overlaps(spec, vm_subnet):
    """Chevauchements qui cassent le routage :
    [(option, autre, (plage, autre plage))]."""
    out = []
    pod, svc, vm = _net(spec.get("pod_cidr")), _net(spec.get("service_cidr")), _net(vm_subnet)
    if pod and svc and pod.overlaps(svc):
        out.append(("pod_cidr", "service_cidr", (str(pod), str(svc))))
    for key, net in (("pod_cidr", pod), ("service_cidr", svc)):
        if net and vm and net.overlaps(vm):
            out.append((key, "vm_subnet", (str(net), str(vm))))
    return out


# ---------------------------------------------------------------------------
# Contrôle préalable
# ---------------------------------------------------------------------------

def finding(code, level, **facts):
    return {"code": code, "level": level, "facts": facts}


K_HARVESTERCLUSTER = "harvesterclusters.infrastructure.cluster.x-k8s.io"


def harvester_cluster_brief(obj):
    """Ce que `check` lit d'un HarvesterCluster : propriétaires `Cluster`
    (nom, uid), suppression en cours, finaliseurs."""
    md = obj.get("metadata") or {}
    return {"owners": [{"name": o.get("name"), "uid": o.get("uid")} for o in md.get("ownerReferences") or []
                       if o.get("kind") == "Cluster"],
            "deleting": bool(md.get("deletionTimestamp")), "finalizers": list(md.get("finalizers") or [])}


def harvester_cluster_findings(ref, facts):
    """2.7.0 (m7, décision 216) : un HarvesterCluster `ref` laissé par un
    cluster supprimé empêche le nouveau de démarrer : bloquant quand son
    `Cluster` propriétaire n'existe plus (ou porte un autre uid), ou quand il
    est en suppression et retenu par un finaliseur. Sans propriétaire, la
    topologie l'adopte (essai 7 de la 2.7.0) : simple avertissement.
    `facts["harvester_clusters"]` : {ref: harvester_cluster_brief},
    `facts["cluster_uids"]` : {"ns/nom": uid}."""
    hc = (facts.get("harvester_clusters") or {}).get(ref)
    if hc is None:
        return []
    ns = ref.split("/", 1)[0]
    uids = facts.get("cluster_uids") or {}
    stale = [o for o in hc["owners"] if uids.get(f"{ns}/{o['name']}") != o["uid"]]
    if stale or (hc["deleting"] and hc["finalizers"]):
        return [finding("harvester-cluster-leftover", "block", name=ref,
                        finalizers=", ".join(hc["finalizers"]) or "-")]
    if not hc["owners"]:
        return [finding("harvester-cluster-orphan", "warn", name=ref)]
    return []


def access_findings(spec):
    """2.8.0 : accès humain au cluster enfant. `rancher_import` (Turtles du
    Rancher intégré) et `rancher_external` (Rancher de la session) sont
    exclusifs (point 239) : deux agents se disputeraient `cattle-system`.
    L'option `oidc` est contrôlée par child_oidc.check (point 228), qui
    avertit quand l'import externe est demandé aussi (point 231)."""
    out = []
    if spec.get("rancher_import") and spec.get("rancher_external"):
        out.append(finding("rancher-import-conflict", "block"))
    if spec.get("oidc") is not None:
        import child_oidc
        _, found = child_oidc.check(spec["oidc"], spec.get("k8s_version"), rancher=spec.get("rancher_external"))
        out += found
    return out


def check(spec, facts):
    """Constats sur une demande, à partir de ce qu'on a lu du cluster.

    `facts` : {
      "stack": {"ready": bool, "missing": [..], "turtles": bool, "legacy": bool},
      "namespaces": [...], "clusters": ["ns/name", ...],
      "images": {"ns/displayName": {"ready": bool, "iso": bool}},
      "keypairs": [...], "networks": [...], "storage_classes": [...],
      "pools": {"name": pool_info(...)},
      "versions": ["v1.33.5", ...],
      "free_cpu_m": millicœurs | None, "free_memory": octets | None,
      "overcommit": {"cpu": 1600, "memory": 150},
    }
    Niveaux : block, warn, ok (comme le transfert de VM).
    """
    out = []
    out += access_findings(spec)
    stack = facts.get("stack") or {}
    if not stack.get("ready"):
        out.append(finding("stack-not-ready", "block", missing=", ".join(stack.get("missing") or [])))
    if stack.get("legacy"):
        out.append(finding("stack-legacy", "warn"))
    for key, msg in validate(spec):
        out.append(finding("invalid", "block", option=key, message=msg))
    ns, name = spec.get("namespace"), spec.get("name")
    clusters = set(facts.get("clusters") or [])
    neighbours = sorted(c for c in clusters if c.split("/", 1)[0] == ns)
    if f"{ns}/{name}" in clusters:
        out.append(finding("name-taken", "block", name=f"{ns}/{name}"))
    elif neighbours:
        # Le générateur nomme ses objets sans le nom du cluster (ClusterClass,
        # secret d'identité, compléments) : un second cluster les écraserait.
        out.append(finding("namespace-has-cluster", "block", namespace=ns,
                           cluster=neighbours[0]))
    elif ns in set(facts.get("namespaces") or []):
        out.append(finding("namespace-exists", "warn", namespace=ns))
    out += harvester_cluster_findings(f"{ns}/{name}", facts)
    img = (facts.get("images") or {}).get(spec.get("image"))
    if spec.get("image") and img is None:
        out.append(finding("image-missing", "block", image=spec.get("image")))
    elif img and img.get("iso"):
        out.append(finding("image-iso", "block", image=spec.get("image")))
    elif img and not img.get("ready"):
        out.append(finding("image-not-ready", "block", image=spec.get("image")))
    elif img and isinstance(img.get("virtual_size"), int) and img["virtual_size"] > _bytes(spec.get("disk_size")) > 0:
        # 2.6.0, vu sur harvlab2 : Longhorn refuse un disque racine plus
        # petit que la taille virtuelle de l'image (« volume size should be
        # larger than the backing image size ») ; la PVC reste Pending et la
        # VM en Scheduling jusqu'au délai, sans autre signe
        out.append(finding("disk-smaller-than-image", "block", image=spec.get("image"),
                           disk=spec.get("disk_size"), image_size=f"{-(-img['virtual_size'] // _QUANTITY['Gi'])}Gi"))
    if img and img.get("ready") and not img.get("iso") and img.get("os") == "sles":
        # 2.7.0 (m-17) : rendu aussi avec disk-smaller-than-image (un `elif`
        # le cachait). Vu sur harv1 : une image SLES non enregistrée n'a aucun dépôt,
        # cloud-init n'installe ni iptables ni qemu-guest-agent, et les pods
        # à port d'hôte (ingress-nginx) restent en ContainerCreating.
        out.append(finding("image-sles-repos", "warn", image=spec.get("image")))
    if spec.get("ssh_keypair") and spec["ssh_keypair"] not in set(facts.get("keypairs") or []):
        out.append(finding("keypair-missing", "block", keypair=spec["ssh_keypair"]))
    nets = set(facts.get("networks") or [])
    for net in [spec.get("network")] + list(spec.get("extra_networks") or []):
        if net and net not in nets:
            out.append(finding("network-missing", "block", network=net))
    if spec.get("extra_disk_class") and spec.get("extra_disk_size") and \
            spec["extra_disk_class"] not in set(facts.get("storage_classes") or []):
        out.append(finding("storage-class-missing", "block", storage_class=spec["extra_disk_class"]))
    pools = facts.get("pools") or {}
    names = [spec.get("ip_pool")] + [p for p in spec.get("ip_pool_refs") or [] if p != spec.get("ip_pool")]
    available = 0
    for p in names:
        if not p:
            continue
        info = pools.get(p)
        if info is None:
            out.append(finding("pool-missing", "block", pool=p))
            continue
        available += info.get("available") or 0
    first = pools.get(spec.get("ip_pool")) or {}
    need = addresses_needed(spec)
    if names and all(p in pools for p in names if p) and available < need:
        out.append(finding("pool-short", "block", needed=need, available=available))
    if first.get("gateway") and spec.get("gateway") and first["gateway"] != spec["gateway"]:
        out.append(finding("gateway-differs", "warn", gateway=spec["gateway"], pool=first["gateway"]))
    if first.get("mask") and spec.get("subnet_mask") and first["mask"] != spec["subnet_mask"]:
        out.append(finding("mask-differs", "warn", mask=spec["subnet_mask"], pool=first["mask"]))
    for key, other, (rng, other_rng) in cidr_overlaps(spec, first.get("subnet")):
        out.append(finding("cidr-overlap", "block", option=key, other=other,
                           range=rng, other_range=other_rng))
    if spec.get("cp_replicas", 1) % 2 == 0:
        out.append(finding("cp-even", "warn", count=spec["cp_replicas"]))
    if spec.get("cp_replicas") == 1:
        out.append(finding("cp-single", "warn"))
    mem = _bytes(spec.get("memory"))
    if spec.get("cpu", 2) < 2 or (mem and mem < 4 * 1024 ** 3):
        out.append(finding("small-nodes", "warn", cpu=spec.get("cpu"), memory=spec.get("memory")))
    versions = facts.get("versions") or []
    if versions and spec.get("k8s_version") not in versions:
        out.append(finding("version-untested", "warn", version=spec.get("k8s_version"),
                           tested=", ".join(versions)))
    if not spec.get("fleet_repo") and (spec.get("cni_mtu") != DEFAULTS["cni_mtu"]
                                       or spec.get("cni_encapsulation") != DEFAULTS["cni_encapsulation"]
                                       or spec.get("cni_bgp") != DEFAULTS["cni_bgp"]):
        out.append(finding("cni-tuning-ignored", "warn"))
    need_cpu, need_mem = requests_needed(spec, facts.get("overcommit"))
    free_cpu, free_mem = facts.get("free_cpu_m"), facts.get("free_memory")
    if free_cpu is not None and need_cpu > free_cpu:
        out.append(finding("cpu-short", "warn", needed=round(need_cpu / 1000, 2),
                           free=round(free_cpu / 1000, 2)))
    if free_mem is not None and need_mem > free_mem:
        out.append(finding("memory-short", "warn", needed=need_mem, free=free_mem))
    out.append(finding("endpoint-dhcp", "ok"))
    return out


def _volumes(spec, facts):
    """Les volumes Longhorn que créeront les VMs d'une demande : un disque
    racine par VM (`disk_size`, classe de l'image : `status.storageClassName`,
    jamais un nom fabriqué) et le disque supplémentaire s'il est demandé.
    Une classe inconnue est laissée aux constats de `check`."""
    replicas = facts.get("class_replicas") or {}
    img = (facts.get("images") or {}).get(spec.get("image")) or {}
    disks = [("root", spec.get("disk_size"), img.get("storage_class"))]
    if spec.get("extra_disk_size"):
        disks.append(("extra", spec["extra_disk_size"], spec.get("extra_disk_class")))
    out = []
    for i in range(spec.get("cp_replicas", 0) + spec.get("worker_replicas", 0)):
        for kind, size, klass in disks:
            if klass in replicas and _bytes(size):
                out.append({"name": f"{spec.get('namespace')}/{spec.get('name')}/vm{i + 1}-{kind}",
                            "size": _bytes(size), "replicas": replicas[klass], "class": klass})
    return out


def _pool_demands(specs, pools, extra=None):
    """Adresses demandées par pool : un cluster à un seul pool y puise tout ;
    un cluster à plusieurs pools remplit ses pools dans l'ordre, et ce qui
    manque reste compté sur le dernier. Rend {pool: (demandé, places)}."""
    left = {p: (pools.get(p) or {}).get("available") or 0 for p in pools}
    asked = {}
    for pool, n in (extra or {}).items():
        asked[pool] = asked.get(pool, 0) + int(n)
        left[pool] = left.get(pool, 0) - int(n)
    multi = []
    for s in specs:
        names = [p for p in [s.get("ip_pool")] + list(s.get("ip_pool_refs") or []) if p]
        names = [p for i, p in enumerate(names) if p not in names[:i] and p in pools]
        if not names:
            continue
        if len(names) == 1:
            n = addresses_needed(s)
            asked[names[0]] = asked.get(names[0], 0) + n
            left[names[0]] -= n
        else:
            multi.append((s, names))
    for s, names in multi:
        need = addresses_needed(s)
        for p in names:
            take = min(need, max(0, left[p])) if p != names[-1] else need
            asked[p] = asked.get(p, 0) + take
            left[p] -= take
            need -= take
            if need <= 0:
                break
    return {p: (asked[p], (pools.get(p) or {}).get("available") or 0)
            for p in asked if left.get(p, 0) < 0}


def check_many(specs, facts, extra_addresses=None):
    """Constats pour N demandes lues en une fois (chaîne d'un plan, décision
    169) : `check` pour chacune (règles du formulaire, inchangées, chaque
    constat porte `cluster`), puis les constats de flotte, BLOQUANTS : la
    chaîne tourne sans surveillance, alors que les manques de `check`
    restent des avertissements pour une personne qui décide.

    `facts` : ceux de `check`, plus `longhorn` ({nodes, over, minimal}),
    `class_replicas` ({classe: répliques}), `images[...]["storage_class"]`,
    et `denied` (accès refusés par le serveur, None si non contrôlés).
    `extra_addresses` : {pool: adresses} prévues en plus (services
    LoadBalancer des artefacts, 2.7.0 ; rien en 2.5.0)."""
    import longhorn_room as _lr
    out = []
    for s in specs:
        ref = f"{s.get('namespace')}/{s.get('name')}"
        for f in check(s, facts):
            out.append(dict(f, cluster=ref))
    names, spaces = {}, {}
    for s in specs:
        names[s.get("name")] = names.get(s.get("name"), 0) + 1
        spaces[s.get("namespace")] = spaces.get(s.get("namespace"), 0) + 1
    for v in sorted(n for n, c in names.items() if c > 1):
        out.append(finding("fleet-name-twice", "block", name=v))
    # un cluster par namespace (le générateur nomme ses objets sans le nom
    # du cluster) : deux noms différents dans le même namespace s'écraseraient
    for v in sorted(n for n, c in spaces.items() if c > 1 and names.get(n, 0) < 2):
        out.append(finding("fleet-name-twice", "block", namespace=v))
    cpu = sum(requests_needed(s, facts.get("overcommit"))[0] for s in specs)
    mem = sum(requests_needed(s, facts.get("overcommit"))[1] for s in specs)
    free_cpu, free_mem = facts.get("free_cpu_m"), facts.get("free_memory")
    if free_cpu is not None and cpu > free_cpu:
        out.append(finding("fleet-cpu-short", "block", needed=round(cpu / 1000, 2),
                           free=round(free_cpu / 1000, 2), clusters=len(specs)))
    if free_mem is not None and mem > free_mem:
        out.append(finding("fleet-memory-short", "block", needed=mem, free=free_mem, clusters=len(specs)))
    lh = facts.get("longhorn")
    volumes = [v for s in specs for v in _volumes(s, facts)]
    # revue m10 : une image sans classe (import en cours) laisse ses disques
    # racine hors de la somme ; le dire plutôt que les compter pour rien
    replicas = facts.get("class_replicas") or {}
    for img in sorted({s.get("image") for s in specs}):
        info = (facts.get("images") or {}).get(img)
        if info is not None and info.get("storage_class") not in replicas:
            out.append(finding("fleet-storage-unknown", "warn", image=img,
                               storage_class=info.get("storage_class") or "-"))
    if lh is not None:
        nodes = _lr.storage_room(lh.get("nodes") or [], [], lh.get("over", 200.0),
                                 lh.get("minimal", 25.0))["schedulable_nodes"]
        unplaceable = set()
        for klass in sorted({v["class"] for v in volumes}):
            want = next(v["replicas"] for v in volumes if v["class"] == klass)
            if want > nodes:
                unplaceable.add(klass)
                out.append(finding("fleet-replicas-nodes", "block", storage_class=klass,
                                   replicas=want, nodes=nodes))
        # une classe déjà signalée ne se place nulle part : la compter encore
        # dans la place ferait un second constat pour la même cause
        room = _lr.fits(lh.get("nodes") or [], lh.get("over", 200.0), lh.get("minimal", 25.0),
                        [v for v in volumes if v["class"] not in unplaceable])
        if not room["fits"]:
            first = room["short"][0]
            out.append(finding("fleet-storage-short", "block", volumes=len(room["short"]),
                               volume=first["volume"], storage_class=first["class"],
                               size=first["size"], nodes=", ".join(first["nodes"]) or "-"))
    for pool, (asked, avail) in sorted(_pool_demands(specs, facts.get("pools") or {},
                                                     extra_addresses).items()):
        out.append(finding("fleet-pool-short", "block", pool=pool, needed=asked, available=avail))
    out += rights_findings(facts)
    out.append(finding("fleet-dhcp-leases", "ok", leases=len(specs)))
    out.append(finding("fleet-sizes", "ok", clusters=len(specs),
                       vms=sum(s.get("cp_replicas", 0) + s.get("worker_replicas", 0) for s in specs),
                       vcpu=sum((s.get("cp_replicas", 0) + s.get("worker_replicas", 0)) * s.get("cpu", 0)
                                for s in specs),
                       memory=sum((s.get("cp_replicas", 0) + s.get("worker_replicas", 0))
                                  * _bytes(s.get("memory")) for s in specs),
                       disks=sum(v["size"] for v in volumes),
                       addresses=sum(addresses_needed(s) for s in specs)
                       + sum(int(n) for n in (extra_addresses or {}).values())))
    return out


def rights_findings(facts):
    """`rights-missing` (bloquant) ou `rights-unchecked` d'après
    `facts["denied"]` : absent = pas contrôlé, rien ; None = lecture
    impossible ; liste = accès refusés par le serveur."""
    denied = facts.get("denied")
    if denied:
        return [finding("rights-missing", "block", count=len(denied),
                        denied=", ".join(_access_text(d) for d in denied[:8]))]
    if denied is None and "denied" in facts:
        return [finding("rights-unchecked", "warn")]
    return []


def _access_text(a):
    ns, verb, group, resource, name = a
    res = f"{resource}.{group}" if group else resource
    return f"{verb} {res}{'/' + name if name else ''}{' in ' + ns if ns else ''}"


# Ressource (groupe, nom au pluriel) de chaque genre que pose le rendu
RENDERED_RESOURCES = (("cluster.x-k8s.io", "clusters"),) + tuple(
    (k.partition(".")[2], k.partition(".")[0]) for k in GENERATED_KINDS)


def manifest_reviews(spec):
    """Ce que la personne doit pouvoir faire pour poser les manifestes du
    cluster (application côté serveur : create et patch) et son namespace :
    [(namespace ou None, verbe, groupe, ressource, nom ou None)]."""
    ns = spec.get("namespace")
    out = [(None, "create", "", "namespaces", None)]
    for group, res in RENDERED_RESOURCES:
        for verb in ("create", "patch"):
            out.append((ns, verb, group, res, None))
    return out


# Ce que coûte une VM à l'ordonnanceur, au-delà de sa mémoire invitée
# (relevé sur harv1 : 4 Gio demandés pour 3,08 Gio de requête à 150 %).
VM_MEMORY_OVERHEAD = 350 * 1024 ** 2


def requests_needed(spec, overcommit=None):
    """Ce que les VMs du cluster demanderont à l'ordonnanceur, en tenant
    compte du surengagement de Harvester (`overcommit-config` : un vCPU à
    1600 % ne demande qu'un seizième de cœur). Rend (millicœurs, octets)."""
    oc = overcommit or {}
    cpu_oc = max(100, int(oc.get("cpu") or 100))
    mem_oc = max(100, int(oc.get("memory") or 100))
    vms = spec.get("cp_replicas", 0) + spec.get("worker_replicas", 0)
    cpu_m = vms * spec.get("cpu", 0) * 1000 * 100 / cpu_oc
    mem = vms * (_bytes(spec.get("memory")) * 100 / mem_oc + VM_MEMORY_OVERHEAD)
    return int(cpu_m), int(mem)


def blocking(findings):
    return [f for f in findings if f["level"] == "block"]


_QUANTITY = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4}


def _bytes(q):
    m = re.match(r"^([0-9]+)(Ki|Mi|Gi|Ti)$", str(q or ""))
    return int(m.group(1)) * _QUANTITY[m.group(2)] if m else 0


# ---------------------------------------------------------------------------
# Progression d'un cluster (v1beta1 comme v1beta2)
# ---------------------------------------------------------------------------

def _cond(obj, ctype):
    for c in ((obj or {}).get("status") or {}).get("conditions") or []:
        if c.get("type") == ctype:
            return c
    return None


def cluster_state(cluster, machines=None):
    """État lisible d'un Cluster CAPI et de ses machines, quelle que soit la
    version servie : v1beta2 publie `status.initialization.*`, v1beta1
    `status.controlPlaneReady` et `status.infrastructureReady`."""
    st = (cluster or {}).get("status") or {}
    init = st.get("initialization") or {}
    infra = bool(init.get("infrastructureProvisioned", st.get("infrastructureReady")))
    cp = bool(init.get("controlPlaneInitialized", st.get("controlPlaneReady")))
    phase = st.get("phase") or "Pending"
    cp_total = cp_running = wk_total = wk_running = 0
    for m in machines or []:
        labels = (m.get("metadata") or {}).get("labels") or {}
        is_cp = "cluster.x-k8s.io/control-plane" in labels
        running = _machine_up(m)
        if is_cp:
            cp_total += 1
            cp_running += running
        else:
            wk_total += 1
            wk_running += running
    if machines is None:
        # Sans la liste des machines (vue liste) : les compteurs que le cœur
        # v1beta2 publie dans le statut du Cluster.
        for key, out in (("controlPlane", "cp"), ("workers", "wk")):
            rep = st.get(key) or {}
            if "desiredReplicas" in rep:
                got = rep.get("availableReplicas", rep.get("readyReplicas")) or 0
                if out == "cp":
                    cp_running, cp_total = got, rep["desiredReplicas"] or 0
                else:
                    wk_running, wk_total = got, rep["desiredReplicas"] or 0
    # Le nombre voulu vient de la topologie : au début, aucune machine
    # n'existe encore et « 0/0 » ne disait rien de ce qui est attendu.
    topo = ((cluster or {}).get("spec") or {}).get("topology") or {}
    want_cp = (topo.get("controlPlane") or {}).get("replicas")
    mds = (topo.get("workers") or {}).get("machineDeployments") or []
    want_wk = sum(md.get("replicas") or 0 for md in mds) if mds else None
    if isinstance(want_cp, int):
        cp_total = want_cp
    if isinstance(want_wk, int):
        wk_total = want_wk
    avail = _cond(cluster, "Available")
    # Available ne suffit pas : la topologie ne crée les MachineDeployments
    # qu'une fois le plan de contrôle initialisé, et dans l'intervalle le
    # cluster se dit disponible sans aucun worker (vu sur harv1 : « prêt »
    # annoncé quatre minutes avant que le worker existe).
    counts = cp_total > 0 and cp_running >= cp_total and wk_running >= wk_total
    if avail is not None:
        ready = counts and avail.get("status") == "True"
    else:
        # cœur d'avant la condition Available (v1beta1 seulement)
        ready = counts and phase == "Provisioned" and cp and infra
    failed = phase == "Failed"
    message = ""
    if not ready and avail is not None and avail.get("status") != "True":
        message = (avail.get("message") or "").replace("\n", " ").strip()[:300]
    return {"phase": phase, "infrastructure": infra, "control_plane": cp,
            "cp": [cp_running, cp_total], "workers": [wk_running, wk_total],
            "ready": bool(ready), "failed": failed, "message": message}


def _machine_up(machine):
    """Une machine compte quand son nœud est prêt, pas seulement inscrit :
    vu sur harv1 (essai3), le worker avait son nodeRef une demi-minute avant
    que son nœud passe Ready (calico encore en initialisation)."""
    st = (machine or {}).get("status") or {}
    if st.get("phase") != "Running" or not st.get("nodeRef"):
        return False
    conds = {c.get("type"): c.get("status") for c in st.get("conditions") or []}
    old = {c.get("type"): c.get("status")
           for c in ((st.get("deprecated") or {}).get("v1beta1") or {}).get("conditions") or []}
    for kind in ("NodeReady", "NodeHealthy"):
        if kind in conds:
            return conds[kind] == "True"
    if "NodeHealthy" in old:
        return old["NodeHealthy"] == "True"
    return True


def class_of(cluster):
    """Nom de la ClusterClass : `topology.class` (v1beta1) ou
    `topology.classRef.name` (v1beta2)."""
    topo = ((cluster or {}).get("spec") or {}).get("topology") or {}
    return topo.get("class") or (topo.get("classRef") or {}).get("name")
