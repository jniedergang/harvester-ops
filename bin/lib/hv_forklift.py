"""harvester-ops : Forklift sur Harvester, migrations depuis VMware (v1.75.0).

Harvester 1.9 ne livre pas Forklift. Relevé le 28/09/2026 (dépôts
harvester/sv-addons, harvester/charts, harvester/forklift-packaging) :

- l'add-on expérimental `forklift-operator` (namespace forklift) installe
  l'opérateur depuis charts.harvesterhci.io ; les valeurs par défaut du chart
  pointent sur rancher/nginx:latest, il faut TOUJOURS poser dépôt, image et
  tag (le même tag vaut pour tous les composants) ;
- chart 1.9.0 publié, images publiées jusqu'à v1.8.2 (Forklift amont 2.9) ;
- un ForkliftController fait déployer les composants (api, controller,
  validation, volume-populator-controller), qui exigent cert-manager,
  absent de Harvester ;
- le fournisseur de destination `host` (namespace forklift) est créé par
  Forklift ; un fournisseur vSphere se déclare avec un secret étiqueté
  createdForProviderType/createdForResourceType ;
- l'inventaire se lit sur le service forklift-inventory (8443) avec un
  jeton de compte de service : le proxy de l'apiserver retire l'en-tête
  Authorization, et le kubeconfig de la console n'a qu'un certificat.

Voir docs/design/2026-09-27-migrations-vmware.md et
docs/design/2026-09-28-forklift-b1-plan.md.
"""

import base64
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

G = "forklift.konveyor.io"
API = f"{G}/v1beta1"
NS = "forklift"
K_ADDON = "addons.harvesterhci.io"
K_CONTROLLER = f"forkliftcontrollers.{G}"
K_PROVIDER = f"providers.{G}"
K_PLAN = f"plans.{G}"
K_DEPLOY = "deployments.apps"
ADDON = (NS, "forklift-operator")
CONTROLLER_NAME = "forklift-controller"
CHART_REPO = "https://charts.harvesterhci.io"
CHART = "forklift-operator"
CHART_VERSION = "1.9.0"
IMAGE_REPO = "registry.rancher.com/harvester"
IMAGE_TAG = "v1.8.2"
OPERATOR_DEPLOY = "harvester-forklift-operator-ansible"
COMPONENTS = ("forklift-api", "forklift-controller", "forklift-validation", "forklift-volume-populator-controller")
CERT_MANAGER = ("cert-manager", ("cert-manager", "cert-manager-cainjector", "cert-manager-webhook"))
INVENTORY_SA = "harvester-ops-inventory"
INVENTORY_SVC, INVENTORY_PORT = "forklift-inventory", 8443
L_MANAGED = "harvester-ops.io/managed"

NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$")
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
ADDON_READY = ("AddonDeploySuccessful", "AddonUpdateSuccessful", "AddonDeployed")
POD_STUCK = ("ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff", "CreateContainerConfigError", "InvalidImageName")


def check_name(name, what="name"):
    name = str(name or "").strip()
    if not NAME_RE.match(name):
        raise ValueError(f"{what}: lowercase letters, digits and '-', 63 characters at most ({name!r})")
    return name


def operator_values(image_tag=IMAGE_TAG, image_repo=IMAGE_REPO):
    """Valeurs du chart : dépôt, image et tag de l'opérateur, toujours posés."""
    if not TAG_RE.match(str(image_tag or "")):
        raise ValueError(f"image tag: {image_tag!r} is not a valid tag")
    return {"fullnameOverride": "harvester",
            "forkliftOperatorAnsible": {"forkliftOperator": {
                "repo": image_repo, "operatorImage": "harvester-forklift-operator",
                "tag": image_tag, "imagePullPolicy": "IfNotPresent"}}}


def addon_manifest(chart_version=CHART_VERSION, image_tag=IMAGE_TAG, image_repo=IMAGE_REPO):
    if not VERSION_RE.match(str(chart_version or "")):
        raise ValueError(f"chart version: {chart_version!r} is not a version such as {CHART_VERSION}")
    return {"apiVersion": "harvesterhci.io/v1beta1", "kind": "Addon",
            "metadata": {"name": ADDON[1], "namespace": NS,
                         "labels": {"addon.harvesterhci.io/experimental": "true", L_MANAGED: "true"}},
            "spec": {"enabled": True, "repo": CHART_REPO, "chart": CHART, "version": chart_version,
                     # du JSON, que Harvester lit comme du YAML (JSON en est un sous-ensemble)
                     "valuesContent": json.dumps(operator_values(image_tag, image_repo), indent=2)}}


def namespace_manifest():
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NS}}


def controller_manifest():
    return {"apiVersion": API, "kind": "ForkliftController",
            "metadata": {"name": CONTROLLER_NAME, "namespace": NS},
            "spec": {"feature_ui_plugin": "false"}}


def deployment_ready(dep):
    if not dep:
        return False
    want = (dep.get("spec") or {}).get("replicas", 1)
    have = (dep.get("status") or {}).get("availableReplicas") or 0
    return have >= max(want, 1)


def addon_state(addon):
    """(état, message) : absent, disabled, deploying, failed, ready. Un échec
    se lit dans le statut (...Failed) ou la condition OperationFailed."""
    if addon is None:
        return "absent", "the forklift-operator add-on is not declared"
    if not (addon.get("spec") or {}).get("enabled"):
        return "disabled", "the forklift-operator add-on is disabled"
    st = addon.get("status") or {}
    status = st.get("status") or ""
    failed = next((c.get("message") or c.get("reason") for c in st.get("conditions") or []
                   if c.get("type") == "OperationFailed" and str(c.get("status")) == "True"), "")
    if "Failed" in status or failed:
        return "failed", "the add-on failed: " + (failed or status)
    if status in ADDON_READY:
        return "ready", "the forklift-operator add-on is deployed"
    return "deploying", f"the add-on is being deployed ({status or 'pending'})"


def pod_problems(pods):
    """« pod: raison (message) » des pods bloqués : image introuvable,
    redémarrages en boucle... ce qu'un déploiement pas prêt ne dit pas."""
    out = []
    for p in pods or []:
        st = p.get("status") or {}
        for cs in (st.get("initContainerStatuses") or []) + (st.get("containerStatuses") or []):
            w = (cs.get("state") or {}).get("waiting") or {}
            if w.get("reason") in POD_STUCK:
                name = (p.get("metadata") or {}).get("name")
                out.append(f"{name}: {w['reason']}" + (f" ({w['message']})" if w.get("message") else ""))
                break
    return out


def install_state(addon, deploys, controller, cert_manager_deploys, inventory_access):
    """L'installation lue dans l'ordre où elle se fait. `deploys` et
    `cert_manager_deploys` : {nom: Deployment} des deux namespaces ;
    `inventory_access` : le compte de service INVENTORY_SA, ou None.

    `running` : Forklift tourne ; `ready` : il tourne ET la console peut lire
    son inventaire. Le compte n'est posé qu'en fin d'installation : une
    installation arrêtée plus tôt (délai des composants), ou un Forklift
    posé autrement (l'add-on de Harvester), n'en a pas, et l'inventaire
    échouerait alors que l'onglet se dirait prêt."""
    a, amsg = addon_state(addon)
    cm_missing = [n for n in CERT_MANAGER[1] if not deployment_ready(cert_manager_deploys.get(n))]
    comp_missing = [n for n in COMPONENTS if not deployment_ready(deploys.get(n))]
    operator = deployment_ready(deploys.get(OPERATOR_DEPLOY))
    running = a == "ready" and operator and controller is not None and not comp_missing and not cm_missing
    return {"cert_manager": not cm_missing, "cert_manager_missing": cm_missing,
            "addon": a, "addon_message": amsg, "operator": operator,
            "controller": controller is not None, "components_missing": comp_missing,
            "inventory_access": inventory_access is not None,
            "running": running, "ready": running and inventory_access is not None}


def pick_addon(addons):
    """L'add-on forklift-operator à suivre, et s'il est celui de Harvester.
    Harvester 1.9.1 devrait livrer le sien : un add-on de ce nom SANS
    l'étiquette de la console n'est pas à elle, elle l'active sans jamais
    réécrire son chart ni ses valeurs. Sinon, celui qu'elle a déclaré."""
    mine = theirs = None
    for a in addons or []:
        m = a.get("metadata") or {}
        if m.get("name") != ADDON[1]:
            continue
        if (m.get("labels") or {}).get(L_MANAGED) == "true":
            mine = a
        else:
            theirs = a
    return (theirs, True) if theirs is not None else (mine, False)


def inventory_rbac():
    """Le compte qui lit l'inventaire : le service d'inventaire vérifie le
    jeton (TokenReview) et le droit de lire les fournisseurs."""
    labels = {L_MANAGED: "true"}
    return [
        {"apiVersion": "v1", "kind": "ServiceAccount",
         "metadata": {"name": INVENTORY_SA, "namespace": NS, "labels": labels}},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
         "metadata": {"name": INVENTORY_SA, "labels": labels},
         "rules": [{"apiGroups": [G], "resources": ["providers"], "verbs": ["get", "list"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
         "metadata": {"name": INVENTORY_SA, "labels": labels},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": INVENTORY_SA},
         "subjects": [{"kind": "ServiceAccount", "name": INVENTORY_SA, "namespace": NS}]},
    ]


# --- fournisseur vSphere ---------------------------------------------------

URL_RE = re.compile(r"^(?:https://)?([A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:]+\])(:\d{1,5})?(?:/sdk)?/?$")
IMAGE_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::\d{1,5})?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+"
                      r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?(?:@sha256:[0-9a-f]{64})?$")


def check_url(url):
    """https://<hôte>/sdk, comme Forklift l'attend ; l'hôte seul, avec ou sans
    https:// et /sdk, est complété. http:// est refusé."""
    u = str(url or "").strip()
    if u.lower().startswith("http://"):
        raise ValueError("vCenter URL: https only")
    m = URL_RE.match(u)
    if not m:
        raise ValueError(f"vCenter URL: {u!r} is not a host, an address or https://<host>/sdk")
    return f"https://{m.group(1)}{m.group(2) or ''}/sdk"


def check_image(ref, what="image"):
    ref = str(ref or "").strip()
    last = ref.rsplit("/", 1)[-1]
    if not IMAGE_RE.match(ref) or (":" not in last and "@" not in last):
        raise ValueError(f"{what}: {ref!r} is not a full image reference (registry/path:tag or @sha256:...)")
    return ref


def secret_name(provider):
    base = provider[:63 - len("-vsphere")].rstrip("-")
    return f"{base}-vsphere"


def provider_secret(ns, name, spec):
    """Le secret d'un fournisseur vSphere, étiqueté comme Forklift le lit. Les
    messages d'erreur ne citent jamais une credential (user ou password)."""
    name = check_name(name, "provider")
    ns = check_name(ns, "namespace")
    user = str(spec.get("user") or "").strip()
    password = str(spec.get("password") or "")
    if not user or not password:
        raise ValueError("user and password are required")
    data = {"user": user, "password": password, "url": check_url(spec.get("url")),
            "insecureSkipVerify": "true" if spec.get("insecure") else "false"}
    if spec.get("cacert"):
        ca = str(spec["cacert"]).strip()
        if "BEGIN CERTIFICATE" not in ca:
            raise ValueError("cacert: a PEM certificate")
        data["cacert"] = ca + "\n"
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": secret_name(name), "namespace": ns,
                         "labels": {"createdForProviderType": "vsphere", "createdForResourceType": "providers",
                                    L_MANAGED: "true"}},
            "stringData": data}


def provider_manifest(ns, name, spec):
    name = check_name(name, "provider")
    ns = check_name(ns, "namespace")
    settings = {"sdkEndpoint": "vcenter"}
    if spec.get("vddk_image"):
        settings["vddkInitImage"] = check_image(spec["vddk_image"], "VDDK image")
    return {"apiVersion": API, "kind": "Provider",
            "metadata": {"name": name, "namespace": ns,
                         "labels": {L_MANAGED: "true"}},
            "spec": {"type": "vsphere", "url": check_url(spec.get("url")),
                     "secret": {"name": secret_name(name), "namespace": ns},
                     "settings": settings}}


def provider_state(p):
    """(True prêt, False refusé, None en cours, message). Forklift classe ses
    conditions : une « Critical » vraie est un refus (identifiants,
    certificat, adresse), « Ready » vraie la fin."""
    if p is None:
        return None, "waiting for the provider"
    st = p.get("status") or {}
    # vu en réel : juste après une modification, les conditions sont encore
    # celles de la version précédente (Ready vrai) ; rien n'est lu avant que
    # Forklift ait relu la nouvelle version
    # (sans observedGeneration dans le statut, rien ne permet de le dire :
    # les conditions sont lues telles quelles)
    gen = (p.get("metadata") or {}).get("generation")
    seen = st.get("observedGeneration")
    if gen is not None and seen is not None and seen < gen:
        return None, f"being checked ({st.get('phase') or 'pending'}: change not read yet)"
    conds = st.get("conditions") or []
    crit = [c for c in conds if c.get("category") == "Critical" and str(c.get("status")) == "True"]
    if crit:
        return False, "; ".join(c.get("message") or c.get("type") or "refused" for c in crit)
    if any(c.get("type") == "Ready" and str(c.get("status")) == "True" for c in conds):
        return True, "ready: vCenter reached, inventory loaded"
    have = [c.get("type") for c in conds if str(c.get("status")) == "True"]
    return None, f"being checked ({st.get('phase') or 'pending'}" + (f": {', '.join(have)}" if have else "") + ")"


def foreign_provider(p):
    """Pourquoi la console ne réécrit pas ce fournisseur existant, ou "" s'il
    est à elle. Seul un fournisseur vSphere portant son étiquette est à elle :
    un fournisseur fait par un autre outil n'est jamais réécrit, et le
    fournisseur `host` (openshift) que Forklift crée lui-même encore moins."""
    if p is None:
        return ""
    m = p.get("metadata") or {}
    kind = (p.get("spec") or {}).get("type") or "unknown"
    where = f"{m.get('namespace')}/{m.get('name')}"
    if kind != "vsphere":
        return f"provider {where} exists and is not a vCenter ({kind}): pick another name"
    if (m.get("labels") or {}).get(L_MANAGED) != "true":
        return (f"provider {where} exists and was not made by harvester-ops: "
                "change it with the tool that made it, or pick another name")
    return ""


def plans_using(ns, name, plans):
    out = []
    for pl in plans or []:
        src = (((pl.get("spec") or {}).get("provider") or {}).get("source")) or {}
        if src.get("name") == name and src.get("namespace") == ns:
            m = pl.get("metadata") or {}
            out.append(f"{m.get('namespace')}/{m.get('name')}")
    return out


# --- ce que l'onglet de la console demande (v1.75.0) --------------------------

VDDK_CM = "harvester-ops-vddk"
CERT_MANAGER_MEMBER = "manifests/cert-manager/cert-manager.yaml"
ARCHIVE_RE = re.compile(r"^VMware-vix-disklib-(\d+\.\d+\.\d+)-\d+\.x86_64\.tar\.gz$")


def check_archive_name(name):
    """Le nom que VMware donne à l'archive VDDK ; rend sa version (8.0.3).

    `fullmatch`, pas `match` : avec `match`, le `$` de fin de motif laisse
    passer un saut de ligne final (nom collé depuis un terminal, par
    exemple), ce qui aurait accepté une archive au nom invalide."""
    m = ARCHIVE_RE.fullmatch(str(name or ""))
    if not m:
        raise ValueError("VDDK archive: VMware-vix-disklib-<version>-<build>.x86_64.tar.gz expected")
    return m.group(1)


def vddk_record_manifest(image, digest, archive, when):
    """La dernière image VDDK poussée pour ce cluster, gardée par le cluster
    lui-même : toute console qui le gère la retrouve (Préparation, formulaire
    de source)."""
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": VDDK_CM, "namespace": NS, "labels": {L_MANAGED: "true"}},
            "data": {"image": check_image(image, "VDDK image"), "digest": str(digest),
                     "archive": str(archive), "pushed_at": str(when)}}


def vddk_record(cm):
    d = (cm or {}).get("data") or {}
    if not d.get("image"):
        return None
    return {k: d.get(k, "") for k in ("image", "digest", "archive", "pushed_at")}


def _registry_setting(value):
    if isinstance(value, dict):
        return value
    try:
        v = json.loads(value) if value and str(value).strip() else {}
    except (ValueError, TypeError):
        return {}
    return v if isinstance(v, dict) else {}


def registry_hint(value, archive="", prov_cluster=None):
    """L'image VDDK proposée : le premier registre du réglage containerd-registry
    de Harvester (ses Configs, puis les points d'accès de ses miroirs), chemin
    harvops/vddk, étiquette = version du VDDK. Ne rend jamais d'identifiant,
    seulement s'il y en a (`auth`), dans le réglage ou, depuis Harvester 1.9,
    dans le secret que nomme le cluster `local` (`prov_cluster`)."""
    v = _registry_setting(value)
    hosts, plain = [], set()
    for host in (v.get("Configs") or {}):
        hosts.append(str(host))
    for mirror in (v.get("Mirrors") or {}).values():
        for ep in (mirror or {}).get("Endpoints") or []:
            ep = str(ep)
            host = re.sub(r"^https?://", "", ep).rstrip("/")
            if ep.startswith("http://"):
                plain.add(host)
            hosts.append(host)
    host = next((h for h in hosts if h), "")
    if not host:
        return {"image": "", "host": "", "plain_http": False, "auth": False}
    try:
        tag = check_archive_name(archive)
    except ValueError:
        tag = "latest"
    return {"image": f"{host}/harvops/vddk:{tag}", "host": host, "plain_http": host in plain,
            "auth": registry_auth(v, host) is not None or bool(registry_auth_secret(prov_cluster, host))}


def registry_auth(value, host):
    """Les identifiants que Harvester a déjà pour ce registre (Configs.<hôte>.Auth)."""
    auth = (((_registry_setting(value).get("Configs") or {}).get(host) or {}).get("Auth")) or {}
    if auth.get("Username") and auth.get("Password"):
        return {"username": str(auth["Username"]), "password": str(auth["Password"])}
    return None


PROV_CLUSTER = ("clusters.provisioning.cattle.io", "fleet-local", "local")


def registry_auth_secret(prov_cluster, host):
    """Vu en réel sur Harvester 1.9 : le réglage containerd-registry perd son
    Auth (null) ; les identifiants vont dans un Secret rke.cattle.io/auth-config
    de fleet-local, nommé par spec.rkeConfig.registries.configs.<hôte>.
    authConfigSecretName du cluster de provisionnement `local`. Rend ce nom."""
    cfg = ((((prov_cluster or {}).get("spec") or {}).get("rkeConfig") or {}).get("registries") or {}).get("configs") or {}
    return str((cfg.get(host) or {}).get("authConfigSecretName") or "")


def secret_values(secret, *keys):
    """Les valeurs décodées des clés présentes d'un Secret (champ data)."""
    data = (secret or {}).get("data") or {}
    return {k: base64.b64decode(data[k]).decode() for k in keys if data.get(k)}


def spec_from_vmimport(source, secret, vddk_image=""):
    """Un vCenter déjà déclaré dans VM Import (VmwareSource et son Secret)
    devient une demande de fournisseur. Lu côté serveur : le mot de passe ne
    passe jamais par le navigateur. Sans certificat d'autorité, VM Import ne
    vérifie pas TLS : le fournisseur non plus."""
    vals = secret_values(secret, "username", "password", "caCert")
    if not vals.get("username") or not vals.get("password"):
        raise ValueError("the VM Import source has no user and password to reuse")
    spec = {"url": ((source or {}).get("spec") or {}).get("endpoint"),
            "user": vals["username"], "password": vals["password"]}
    if vals.get("caCert"):
        spec["cacert"] = vals["caCert"]
    else:
        spec["insecure"] = True
    if vddk_image:
        spec["vddk_image"] = vddk_image
    return spec


# --- inventaire (service forklift-inventory) --------------------------------

def inventory_rows(kind, items):
    """Lignes utiles d'un inventaire vSphere de Forklift (détail=1, ou 4 pour
    les outils VMware, l'instantané courant et l'uuid)."""
    items = items or []
    if kind == "vms":
        out = []
        for v in items:
            out.append({
                "id": v.get("id"), "name": v.get("name"), "path": v.get("path"),
                "power": v.get("powerState"), "cbt": bool(v.get("changeTrackingEnabled")),
                "cpus": v.get("cpuCount"), "memory_mib": v.get("memoryMB"),
                "guest": v.get("guestName") or v.get("guestId"),
                "disks": [{"datastore": ((d.get("datastore") or {}).get("id")), "capacity": d.get("capacity")}
                          for d in v.get("disks") or []],
                "networks": [n.get("id") for n in v.get("networks") or []],
                "concerns": [{"category": c.get("category"), "label": c.get("label")} for c in v.get("concerns") or []],
                # détail=4 (v1.76.0) : outils VMware en marche, instantané courant, uuid ;
                # absents au détail=1, lus comme faux / vides
                "tools": bool(v.get("guestNameFromVmwareTools") or v.get("ipAddress")),
                "snapshot": str(((v.get("snapshot") or {}).get("id")) or ""),
                "uuid": str(v.get("uuid") or ""),
            })
        return out
    if kind == "networks":
        return [{"id": n.get("id"), "name": n.get("name"), "path": n.get("path")} for n in items]
    if kind == "datastores":
        return [{"id": d.get("id"), "name": d.get("name"), "path": d.get("path"),
                 "capacity": d.get("capacity"), "free": d.get("free")} for d in items]
    raise ValueError(f"inventory kind: vms, networks or datastores ({kind!r})")


# --- vagues à chaud (v1.76.0) ------------------------------------------------
#
# Relevé sur le banc le 29/09/2026 (harvlab2 + vmwlab, deux vraies vagues) :
# - une VM en échec garde `phase: Completed` : l'échec se lit dans sa
#   condition Failed et dans error.reasons ;
# - pipeline : Initialize, DiskTransfer, Cutover, ImageConversion (absente en
#   copie brute), VirtualMachineCreation ; une étape finie peut garder 0/1 ;
# - la dernière copie (celle de la bascule) n'a jamais de fin ;
# - warm.nextPrecopyAt reste posé après la bascule : il ne vaut que pendant
#   la copie.

L_WAVE = "harvester-ops.io/wave"
A_ROLLED_BACK = "harvester-ops.io/rolled-back"
A_CLOSED = "harvester-ops.io/closed"
A_ORIGINAL_IMPORTER = "harvester-ops.io/original-importer-image"
K_MIGRATION = f"migrations.{G}"
K_NETWORKMAP = f"networkmaps.{G}"
K_STORAGEMAP = f"storagemaps.{G}"
DEST_PROVIDER = "host"
WAVE_NAME_MAX = 40
CDI_OPERATOR = ("harvester-system", "cdi-operator")
CDI_IMAGE_ENVS = ("IMPORTER_IMAGE", "OVIRT_POPULATOR_IMAGE")
PRECOPY_DEFAULT, PRECOPY_MIN, PRECOPY_MAX = 60, 5, 1440
CUTOVER_GRACE = timedelta(minutes=5)
VM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")
RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
STEP_LABELS = {"Initialize": "initializing", "DiskTransfer": "copying disks", "Cutover": "final copy",
               "ImageConversion": "converting guest", "DiskAllocation": "allocating disks",
               "VirtualMachineCreation": "creating VM"}
WARM_BLOCKERS = {"cbt": "Changed Block Tracking is off",
                 "tools": "VMware Tools are not running: the switchover cannot shut the source down"}


def check_wave_name(name):
    """RFC 1123, 40 caractères au plus : les objets dérivés ajoutent -net,
    -sto, -m<n>."""
    n = str(name or "").strip()
    if len(n) > WAVE_NAME_MAX or not NAME_RE.match(n):
        raise ValueError(f"wave name: lowercase letters, digits and '-', {WAVE_NAME_MAX} characters at most ({n!r})")
    return n


def _wave_meta(name, wave):
    return {"name": name, "namespace": NS, "labels": {L_MANAGED: "true", L_WAVE: wave}}


def _unique(ids, what):
    seen = set()
    for i in ids:
        if i in seen:
            raise ValueError(f"{what} {i!r} is listed twice")
        seen.add(i)


def _source_id(value, what):
    v = str(value or "").strip()
    if not VM_ID_RE.match(v):
        raise ValueError(f"{what}: {v!r} is not an inventory id")
    return v


def _network_destination(dest):
    d = str(dest or "").strip()
    if d == "pod":
        return {"type": "pod"}
    ns, _, nad = d.partition("/")
    if not nad or not NAME_RE.match(ns) or not NAME_RE.match(nad):
        raise ValueError(f"network destination: 'pod' or '<namespace>/<network>' ({d!r})")
    return {"type": "multus", "namespace": ns, "name": nad}


def wave_manifests(spec):
    """[NetworkMap, StorageMap, Plan] d'une vague, dans le namespace forklift,
    étiquetés console et vague. Que chaque réseau et chaque datastore des VMs
    soit mappé ne se vérifie qu'avec l'inventaire (outil)."""
    wave = check_wave_name(spec.get("name"))
    target = check_name(spec.get("target_namespace"), "target namespace")
    prov = spec.get("provider") or {}
    source = {"namespace": check_name(prov.get("namespace"), "provider namespace"),
              "name": check_name(prov.get("name"), "provider")}
    vms = [str(v or "").strip() for v in spec.get("vms") or []]
    if not vms:
        raise ValueError("a wave needs at least one VM")
    for v in vms:
        if not VM_ID_RE.match(v):
            raise ValueError(f"VM id: {v!r} is not an inventory id")
    _unique(vms, "VM")
    nets = [(_source_id(n.get("source"), "network source"), _network_destination(n.get("destination")))
            for n in spec.get("networks") or []]
    _unique([s for s, _ in nets], "network")
    stos = []
    for s in spec.get("storages") or []:
        sc = str(s.get("storage_class") or "").strip()
        if not sc or len(sc) > 253 or not SUBDOMAIN_RE.match(sc):
            raise ValueError(f"storage class: {sc!r} is not a storage class name")
        stos.append((_source_id(s.get("source"), "storage source"), sc))
    _unique([s for s, _ in stos], "datastore")
    providers = {"source": source, "destination": {"namespace": NS, "name": DEST_PROVIDER}}
    netmap = {"apiVersion": API, "kind": "NetworkMap", "metadata": _wave_meta(f"{wave}-net", wave),
              "spec": {"provider": providers,
                       "map": [{"source": {"id": s}, "destination": d} for s, d in nets]}}
    stomap = {"apiVersion": API, "kind": "StorageMap", "metadata": _wave_meta(f"{wave}-sto", wave),
              "spec": {"provider": providers,
                       "map": [{"source": {"id": s}, "destination": {"storageClass": sc}} for s, sc in stos]}}
    pspec = {"warm": True, "targetNamespace": target, "provider": providers,
             "map": {"network": {"namespace": NS, "name": f"{wave}-net"},
                     "storage": {"namespace": NS, "name": f"{wave}-sto"}},
             "vms": [{"id": v} for v in vms],
             "skipGuestConversion": bool(spec.get("skip_conversion")),
             "preserveStaticIPs": bool(spec.get("preserve_static_ips"))}
    if spec.get("skip_conversion"):
        # vu en réel (vague-2) : copie brute sans mode de compatibilité =
        # disques virtio ; avec conversion, Forklift garde son défaut
        pspec["useCompatibilityMode"] = bool(spec.get("compat_mode"))
    plan = {"apiVersion": API, "kind": "Plan", "metadata": _wave_meta(wave, wave), "spec": pspec}
    return [netmap, stomap, plan]


def migration_manifest(wave, n):
    wave = check_wave_name(wave)
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError(f"migration number: a positive integer ({n!r})")
    return {"apiVersion": API, "kind": "Migration", "metadata": _wave_meta(f"{wave}-m{n}", wave),
            "spec": {"plan": {"namespace": NS, "name": wave}}}


def _now(now=None):
    return now or datetime.now(timezone.utc)


def _parse_ts(value):
    """Horodatage RFC 3339 (fuseau obligatoire) -> datetime UTC, ou None."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else None
    s = str(value or "").strip()
    if not RFC3339_RE.match(s):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _fmt_ts(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cutover_patch(when=None, now=None):
    """Patch de fusion d'une Migration : bascule à `when` (RFC 3339, None =
    maintenant). Plus de 5 min dans le passé : sans doute une erreur de
    fuseau, refusé."""
    now = _now(now)
    if when is None or when == "":
        at = now
    else:
        at = _parse_ts(when)
        if at is None:
            raise ValueError(f"cutover time: RFC 3339 with a time zone expected ({when!r})")
        if at < now - CUTOVER_GRACE:
            raise ValueError("cutover time: more than 5 minutes in the past")
    return {"spec": {"cutover": _fmt_ts(at.replace(microsecond=0))}}


def _cond(obj, ctype):
    """La condition `ctype` vraie d'un objet Forklift, ou None."""
    for c in ((obj or {}).get("status") or {}).get("conditions") or []:
        if c.get("type") == ctype and str(c.get("status")) == "True":
            return c
    return None


def _ann(obj):
    return ((obj or {}).get("metadata") or {}).get("annotations") or {}


def _id_list(value):
    return {x.strip() for x in str(value or "").split(",") if x.strip()}


def _mig_order(m):
    md = m.get("metadata") or {}
    ts = _parse_ts(md.get("creationTimestamp"))
    num = re.search(r"-m(\d+)$", md.get("name") or "")
    return (ts or datetime.min.replace(tzinfo=timezone.utc), int(num.group(1)) if num else 0)


def current_migration(plan, migrations):
    """La Migration la plus récente de ce plan (date de création, puis numéro)."""
    md = plan.get("metadata") or {}
    mine = [m for m in migrations or []
            if ((m.get("spec") or {}).get("plan") or {}).get("name") == md.get("name")
            and ((m.get("spec") or {}).get("plan") or {}).get("namespace", md.get("namespace")) == md.get("namespace")]
    return max(mine, key=_mig_order) if mine else None


def vm_status(plan, migrations, vm_id):
    """Le statut Forklift d'une VM de la vague : plan.status.migration.vms,
    sinon celui de la Migration courante (même lecture que wave_state)."""
    st = plan.get("status") or {}
    status_vms = {v.get("id"): v for v in ((st.get("migration") or {}).get("vms") or [])}
    cur = current_migration(plan, migrations)
    if cur is not None and not status_vms:
        status_vms = {v.get("id"): v for v in ((cur.get("status") or {}).get("vms") or [])}
    return status_vms.get(vm_id) or {}


def vm_creation_done(vm):
    """True si l'étape VirtualMachineCreation du pipeline Forklift est
    terminée pour cette VM : la VM Harvester existe forcément, même si son
    nom ou son étiquette nous échappent."""
    for s in (vm or {}).get("pipeline") or []:
        if s.get("name") == "VirtualMachineCreation":
            return s.get("phase") == "Completed"
    return False


def _vm_error(vm):
    reasons = list(((vm.get("error") or {}).get("reasons")) or [])
    if not reasons:
        for step in vm.get("pipeline") or []:
            reasons += ((step.get("error") or {}).get("reasons")) or []
    return "; ".join(str(r) for r in reasons)


def _vm_phase(vm):
    if not vm:
        return ""
    for ctype, phase in (("Failed", "Failed"), ("Canceled", "Canceled"), ("Succeeded", "Succeeded")):
        if _cond({"status": vm}, ctype):
            return phase
    if vm.get("error") and vm.get("completed"):
        return "Failed"
    return vm.get("phase") or ""


def _current_step(pipeline):
    """L'étape en cours : celle en erreur, sinon la première non finie qui a
    commencé, sinon la dernière finie, sinon la première."""
    if not pipeline:
        return None
    for s in pipeline:
        if s.get("error"):
            return s
    for s in pipeline:
        if s.get("phase") != "Completed" and (s.get("started") or s.get("phase") == "Running"):
            return s
    done = [s for s in pipeline if s.get("phase") == "Completed"]
    if done and len(done) == len(pipeline):
        return done[-1]
    return next((s for s in pipeline if s.get("phase") != "Completed"), pipeline[0])


def _precopy(p):
    start, end = _parse_ts(p.get("start")), _parse_ts(p.get("end"))
    return {"start": p.get("start") or "", "end": p.get("end") or "",
            "seconds": int((end - start).total_seconds()) if start and end else None}


def _vm_row(vm_id, vm, rolled, cutover_set=False):
    """`cutover_set` : la Migration courante porte déjà `spec.cutover` (la
    bascule a été déclenchée ou programmée). Un rollback ne se justifie
    qu'une fois la bascule au moins amorcée : ni un simple échec de copie
    (DiskTransfer, avant toute bascule) ne doit l'autoriser."""
    vm = vm or {}
    step = _current_step(vm.get("pipeline") or [])
    prog = (step or {}).get("progress") or {}
    done, total = int(prog.get("completed") or 0), int(prog.get("total") or 0)
    if step and step.get("phase") == "Completed":
        done = total           # vu en réel : VirtualMachineCreation finie à 0/1
    warm = vm.get("warm") or {}
    pre = warm.get("precopies") or []
    finished = [p for p in pre if p.get("end")]
    last = _precopy(finished[-1]) if finished else (_precopy(pre[-1]) if pre else None)
    # étape Cutover réellement amorcée (celle qui arrête les copies
    # incrémentales, `next_precopy` compris) : distincte de `cutover_started`
    # ci-dessous, qui compte aussi une bascule programmée mais pas encore
    # atteinte (une vague `cutover-scheduled` continue de copier jusque-là).
    cutover_step_started = any(
        s.get("name") == "Cutover"
        and (s.get("started") or s.get("error") or s.get("phase") not in (None, "", "Pending"))
        for s in vm.get("pipeline") or [])
    cutover_started = cutover_set or cutover_step_started
    copying = not vm.get("completed") and not vm.get("error") and not cutover_step_started
    name = (step or {}).get("name") or ""
    step_label = STEP_LABELS.get(name, name)
    phase = _vm_phase(vm)
    if phase == "CopyingPaused":
        # Vu en réel (vague chaude) : entre deux copies incrémentales,
        # Forklift laisse le pipeline sur son étape courante (souvent
        # Cutover en Pending), ce qui affiche "final copy 0/10240" et se lit
        # comme une bascule commencée. `status.migration.vms[].phase:
        # CopyingPaused` dit le contraire : la copie attend simplement son
        # prochain tour. On le montre comme tel, avec la progression de
        # l'étape DiskTransfer (la dernière copie faite), pas celle de
        # Cutover.
        name = "CopyingPaused"
        step_label = "waiting for the next copy"
        disk = next((s for s in vm.get("pipeline") or [] if s.get("name") == "DiskTransfer"), None)
        dprog = (disk or {}).get("progress") or {}
        done, total = int(dprog.get("completed") or 0), int(dprog.get("total") or 0)
    # v1.80.0 (vue en couloirs) : toutes les copies avec leurs dates, pas
    # seulement la dernière, et la fenêtre de bascule réellement vécue : du
    # début de l'étape Cutover à la fin de la VM (ouverte tant qu'elle dure)
    cut = next((s for s in vm.get("pipeline") or [] if s.get("name") == "Cutover" and s.get("started")), None)
    cutover_window = {"start": cut.get("started"), "end": vm.get("completed") or None} if cut else None
    return {"id": vm_id, "name": vm.get("name") or "", "phase": phase,
            "step": step_label, "step_name": name,
            "progress": {"done": done, "total": total}, "precopies": len(pre), "last_precopy": last,
            "copies": [_precopy(p) for p in pre if p.get("start")],
            "started": vm.get("started") or None, "completed": vm.get("completed") or None,
            "cutover_window": cutover_window,
            "next_precopy": (warm.get("nextPrecopyAt") or None) if copying else None,
            "error": _vm_error(vm), "rolled_back": vm_id in rolled,
            "cutover_started": cutover_started}


def wave_state(plan, migrations, now=None):
    """L'état d'une vague lu dans son Plan et ses Migrations.

    Ordre : close (annotation ou spec.archived), revenue à la source (toutes
    ses VMs dans l'annotation), refusée (condition Critical), puis la
    Migration courante (la plus récente) : réussie, en échec (Failed ou
    Canceled), bascule en cours (date passée), bascule prévue, copie. Sans
    Migration (vue globale), le statut du plan décide. `pending` : plan en
    cours de validation."""
    md = plan.get("metadata") or {}
    spec = plan.get("spec") or {}
    st = plan.get("status") or {}
    ann = _ann(plan)
    rolled = _id_list(ann.get(A_ROLLED_BACK))
    ids = [v.get("id") for v in spec.get("vms") or [] if v.get("id")]
    status_vms = {v.get("id"): v for v in ((st.get("migration") or {}).get("vms") or [])}
    cur = current_migration(plan, migrations)
    if cur is not None and not status_vms:
        status_vms = {v.get("id"): v for v in ((cur.get("status") or {}).get("vms") or [])}
    cutover = ((cur or {}).get("spec") or {}).get("cutover") or None
    vms = [_vm_row(i, status_vms.get(i), rolled, cutover is not None) for i in ids]
    message = ""
    errors = "; ".join(v["error"] for v in vms if v["error"])
    critical = [c for c in st.get("conditions") or []
                if c.get("category") == "Critical" and str(c.get("status")) == "True"]
    running = (st.get("migration") or {}).get("started") and not (st.get("migration") or {}).get("completed")
    if ann.get(A_CLOSED) or spec.get("archived"):
        state = "closed"
    elif ids and all(i in rolled for i in ids):
        state = "rolled-back"
    elif critical:
        state = "invalid"
        message = "; ".join(c.get("message") or c.get("type") or "refused" for c in critical)
    else:
        src = cur if cur is not None else plan
        end = _cond(src, "Succeeded"), _cond(src, "Failed") or _cond(src, "Canceled")
        if end[0]:
            state = "succeeded"
        elif end[1]:
            state = "failed"
            message = errors or end[1].get("message") or ""
        elif cur is not None or running or _cond(plan, "Executing"):
            at = _parse_ts(cutover)
            if at is None:
                state = "copying"
            elif at > _now(now):
                state = "cutover-scheduled"
            else:
                state = "cutting-over"
        elif _cond(plan, "Ready"):
            state = "ready"
        else:
            state = "pending"
    nexts = [v["next_precopy"] for v in vms if v["next_precopy"]]
    cutover_started = cutover is not None or any(v["cutover_started"] for v in vms)
    src = ((spec.get("provider") or {}).get("source")) or {}
    # v1.80.0 : début et fin de la migration courante (sinon celle que
    # résume le plan), pour placer la vague sur l'axe du temps des couloirs
    run = ((cur.get("status") or {}) if cur is not None else (st.get("migration") or {}))
    return {"name": md.get("name"), "created": md.get("creationTimestamp") or None,
            "started": run.get("started") or None, "completed": run.get("completed") or None, "target_namespace": spec.get("targetNamespace") or "",
            "provider": {"namespace": src.get("namespace") or "", "name": src.get("name") or ""},
            "state": state, "message": message, "migration": ((cur or {}).get("metadata") or {}).get("name"),
            "vms": vms, "cutover": cutover, "cutover_started": cutover_started,
            "next_precopy": min(nexts) if nexts and state in ("copying", "cutover-scheduled") else None}


def vm_warm_blockers(row):
    """Pourquoi une VM (ligne de inventory_rows) ne peut pas entrer dans une
    vague à chaud. Vu en réel : sans outils VMware, la bascule n'arrête pas
    la source (« VMware Tools is not running ») ; éteinte, rien à arrêter."""
    out = []
    if not row.get("cbt"):
        out.append(WARM_BLOCKERS["cbt"])
    if row.get("power") == "poweredOn" and not row.get("tools"):
        out.append(WARM_BLOCKERS["tools"])
    return out


def provider_host(provider):
    """L'hôte du vCenter d'un fournisseur, en minuscules : avec l'identifiant
    vm-NN, l'identité d'une VM source dans toute la console."""
    url = str((((provider or {}).get("spec") or {}).get("url")) or "")
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def taken_vms(plans, provider_hosts, migrations=()):
    """{(hôte, vm-NN): {"wave", "state"}} des vagues de la console non closes.
    `provider_hosts` : {(namespace, nom): hôte}. Un plan dont le fournisseur
    est inconnu est ignoré (rien à comparer). Une VM revenue à la source
    reste listée, à l'état rolled-back."""
    out = {}
    for p in plans or []:
        labels = (p.get("metadata") or {}).get("labels") or {}
        if labels.get(L_MANAGED) != "true" or not labels.get(L_WAVE):
            continue
        src = ((p.get("spec") or {}).get("provider") or {}).get("source") or {}
        host = provider_hosts.get((src.get("namespace"), src.get("name")))
        if not host:
            continue
        st = wave_state(p, migrations)
        if st["state"] == "closed":
            continue
        for vm in st["vms"]:
            out[(host, vm["id"])] = {"wave": st["name"], "state": "rolled-back" if vm["rolled_back"] else st["state"]}
    return out


# --- importeur CDI (Préparation) ---------------------------------------------
# L'importeur CDI de Harvester (SUSE, 1.65.0) n'a pas le greffon nbdkit VDDK :
# aucune copie VDDK possible. Contournement vérifié en réel : les variables
# IMPORTER_IMAGE et OVIRT_POPULATOR_IMAGE du cdi-operator sur l'image amont.

def _cdi_env(deploy):
    for c in ((((deploy or {}).get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []:
        for e in c.get("env") or []:
            if e.get("name") == "IMPORTER_IMAGE":
                return c.get("name"), str(e.get("value") or "")
    return CDI_OPERATOR[1], ""


def cdi_importer_state(operator_deploy):
    container, image = _cdi_env(operator_deploy)
    repo = image.rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image
    if repo.startswith("registry.suse.com/") and repo.endswith("/cdi-importer"):
        kind = "suse-no-vddk"
    elif repo == "quay.io/kubevirt/cdi-importer":
        kind = "upstream"
    else:
        kind = "other"
    return {"image": image, "kind": kind, "container": container,
            "original": str(_ann(operator_deploy).get(A_ORIGINAL_IMPORTER) or "")}


def cdi_importer_patch(image, keep_original):
    """Patch stratégique du Deployment cdi-operator. `keep_original` : l'état
    courant (cdi_importer_state) ; l'image d'origine n'est gardée en
    annotation qu'une fois, jamais réécrite, pas quand on y revient, et
    seulement quand l'image courante n'est pas déjà l'amont (sinon un
    changement de miroir enregistrerait quay comme "original"). Le nom du
    conteneur patché est celui trouvé dans le déploiement (`container` de
    cdi_importer_state), jamais un nom supposé."""
    image = check_image(image, "importer image")
    cur = keep_original or {}
    container = cur.get("container") or CDI_OPERATOR[1]
    patch = {"spec": {"template": {"spec": {"containers": [
        {"name": container, "env": [{"name": n, "value": image} for n in CDI_IMAGE_ENVS]}]}}}}
    if cur.get("image") and not cur.get("original") and cur["image"] != image \
            and cur.get("kind") in ("suse-no-vddk", "other"):
        patch["metadata"] = {"annotations": {A_ORIGINAL_IMPORTER: cur["image"]}}
    return patch


# --- intervalle des copies (réglage global du ForkliftController) -------------

def precopy_interval(controller):
    v = (((controller or {}).get("spec") or {}).get("controller_precopy_interval"))
    try:
        return int(v) if v is not None and not isinstance(v, bool) else PRECOPY_DEFAULT
    except (TypeError, ValueError):
        return PRECOPY_DEFAULT


def precopy_patch(minutes):
    """Minutes entre deux copies (5 à 1440). Vu en réel : le changement
    redémarre le contrôleur et ne replanifie pas une copie déjà prévue."""
    try:
        m = int(minutes) if not isinstance(minutes, bool) else None
    except (TypeError, ValueError):
        m = None
    if m is None or not PRECOPY_MIN <= m <= PRECOPY_MAX:
        raise ValueError(f"precopy interval: {PRECOPY_MIN} to {PRECOPY_MAX} minutes ({minutes!r})")
    return {"spec": {"controller_precopy_interval": m}}
