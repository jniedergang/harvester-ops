"""harvester-ops : les menus « Advanced » de Harvester (v1.64.0).

Modèles de VM et leurs versions, modèles cloud-init (ConfigMaps), classes de
stockage (chiffrement, LVM, Longhorn v2, topologies), secrets typés et leur
modification, clés SSH modifiables. Relevé dans harvester-ui-extension et le
serveur de Harvester v1.9.0 (scratchpad du chantier :
parity/harvester-storage-formats-1.9.md, sections 3 à 7).

Fonctions pures ; bin/harvester-resources.py les applique.
"""

import base64
import copy
import json
import re

K_TEMPLATE = "virtualmachinetemplates.harvesterhci.io"
K_VERSION = "virtualmachinetemplateversions.harvesterhci.io"
K_CM = "configmaps"
CLOUD_LABEL = "harvesterhci.io/cloud-init-template"
DESC = "field.cattle.io/description"
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
KEY_RE = re.compile(r"^[-._a-zA-Z0-9]{1,253}$")
LH = "driver.longhorn.io"
LVM = "lvm.driver.harvesterhci.io"
CRYPTO_KEYS = ("CRYPTO_KEY_CIPHER", "CRYPTO_KEY_HASH", "CRYPTO_KEY_SIZE", "CRYPTO_PBKDF",
               "CRYPTO_KEY_PROVIDER", "CRYPTO_KEY_VALUE")
CRYPTO_CHOICES = {"CRYPTO_KEY_CIPHER": ("aes-xts-plain", "aes-xts-plain64", "aes-cbc-plain", "aes-cbc-plain64",
                                        "aes-cbc-essiv:sha256"),
                  "CRYPTO_KEY_HASH": ("sha256", "sha384", "sha512"),
                  "CRYPTO_KEY_SIZE": ("256", "384", "512"),
                  "CRYPTO_PBKDF": ("argon2i", "argon2id", "pbkdf2")}
SECRET_TYPES = {"Opaque": None, "kubernetes.io/basic-auth": ("username", "password"),
                "kubernetes.io/ssh-auth": ("ssh-privatekey",), "kubernetes.io/tls": ("tls.crt", "tls.key"),
                "kubernetes.io/dockerconfigjson": (".dockerconfigjson",)}


def check_name(name, what="name"):
    if not NAME_RE.match(name or ""):
        raise ValueError(f"{what}: lower-case letters, digits and dashes, 63 at most")
    return name


def _meta(o):
    return (o or {}).get("metadata") or {}


# ---------------------------------------------------------------------------
# Modèles de VM
# ---------------------------------------------------------------------------

def versions_of(template, versions):
    """Les versions d'un modèle, la plus récente d'abord, avec leur état."""
    ns, name = _meta(template).get("namespace"), _meta(template).get("name")
    default = ((template or {}).get("spec") or {}).get("defaultVersionId")
    out = []
    for v in versions:
        s = v.get("spec") or {}
        if s.get("templateId") != f"{ns}/{name}":
            continue
        st = v.get("status") or {}
        conds = {c.get("type"): str(c.get("status")) for c in st.get("conditions") or []}
        ref = f"{_meta(v).get('namespace')}/{_meta(v).get('name')}"
        out.append({"name": _meta(v).get("name"), "namespace": _meta(v).get("namespace"), "ref": ref,
                    "version": st.get("version"), "description": s.get("description") or "",
                    "ready": conds.get("ready") == "True", "default": ref == default,
                    "image": s.get("imageId"), "created": _meta(v).get("creationTimestamp")})
    out.sort(key=lambda r: (r["version"] or 0), reverse=True)
    return out


def set_default_patch(version_ref):
    if not re.match(r"^[a-z0-9-]+/[a-z0-9.-]+$", version_ref or ""):
        raise ValueError("version: namespace/name")
    return {"spec": {"defaultVersionId": version_ref}}


def delete_version_check(template, version_ref):
    if ((template or {}).get("spec") or {}).get("defaultVersionId") == version_ref:
        raise ValueError("the default version cannot be deleted: choose another default first")


# ---------------------------------------------------------------------------
# Modèles cloud-init
# ---------------------------------------------------------------------------

def cloud_template(name, ns, kind, text, description=""):
    """Un modèle cloud-init de Harvester : ConfigMap au label
    harvesterhci.io/cloud-init-template = user | network, texte dans data.cloudInit."""
    check_name(name, "template name")
    check_name(ns, "namespace")
    if kind not in ("user", "network"):
        raise ValueError("type: user or network")
    if not (text or "").strip():
        raise ValueError("the template text is empty")
    import yaml
    try:
        yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"not valid YAML: {str(e).splitlines()[0]}") from None
    out = {"apiVersion": "v1", "kind": "ConfigMap",
           "metadata": {"name": name, "namespace": ns, "labels": {CLOUD_LABEL: kind}},
           "data": {"cloudInit": text}}
    if description:
        out["metadata"]["annotations"] = {DESC: description[:1000]}
    return out


def cloud_templates(configmaps):
    out = []
    for cm in configmaps:
        kind = (_meta(cm).get("labels") or {}).get(CLOUD_LABEL)
        if kind not in ("user", "network"):
            continue
        out.append({"namespace": _meta(cm).get("namespace"), "name": _meta(cm).get("name"), "type": kind,
                    "text": (cm.get("data") or {}).get("cloudInit") or "",
                    "description": (_meta(cm).get("annotations") or {}).get(DESC) or "",
                    "created": _meta(cm).get("creationTimestamp")})
    out.sort(key=lambda r: (r["namespace"], r["name"]))
    return out


# ---------------------------------------------------------------------------
# Classes de stockage
# ---------------------------------------------------------------------------

def crypto_secret_ok(secret):
    """Un Secret utilisable pour le chiffrement Longhorn (clés exigées par le
    webhook de Harvester, valeurs dans les listes permises)."""
    data = (secret or {}).get("data") or {}
    try:
        vals = {k: base64.b64decode(data[k]).decode() for k in CRYPTO_KEYS}
    except (KeyError, ValueError):
        return False
    if vals["CRYPTO_KEY_PROVIDER"] != "secret" or not vals["CRYPTO_KEY_VALUE"]:
        return False
    return all(vals[k] in allowed for k, allowed in CRYPTO_CHOICES.items())


def crypto_secret(name, ns, key_value, cipher="aes-xts-plain64", key_hash="sha256", size="256", pbkdf="argon2i"):
    check_name(name, "secret name")
    if not key_value or len(key_value) < 8:
        raise ValueError("the passphrase needs 8 characters at least")
    vals = {"CRYPTO_KEY_CIPHER": cipher, "CRYPTO_KEY_HASH": key_hash, "CRYPTO_KEY_SIZE": str(size),
            "CRYPTO_PBKDF": pbkdf, "CRYPTO_KEY_PROVIDER": "secret", "CRYPTO_KEY_VALUE": key_value}
    for k, allowed in CRYPTO_CHOICES.items():
        if vals[k] not in allowed:
            raise ValueError(f"{k}: one of {', '.join(allowed)}")
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": name, "namespace": ns}, "stringData": vals}


# 2.10.1 (point 324 A) : Harvester 1.9.1 déprécie le type LVM `striped` et
# exige `dm-thin` (notes de version v1.9.1, pilote CSI LVM en GA).
LVM_DM_THIN_ONLY = (1, 9, 1)


def _dm_thin_only(version):
    import hv_upgrade
    v = hv_upgrade.parse_version(version)
    if v == "dev":
        return True
    return isinstance(v, tuple) and v[:3] >= LVM_DM_THIN_ONLY


def lvm_type_policy(version):
    """Ce que le formulaire propose pour le type LVM, selon la version lue
    du cluster : `dm-thin` par défaut ; `striped` refusé à partir de 1.9.1,
    averti avant (version illisible : averti)."""
    return {"default": "dm-thin", "striped": "refused" if _dm_thin_only(version) else "warn"}


def lvm_volumes(storageclasses, pvs):
    """Volumes LVM d'un cluster, pour le contrôle de mise à jour : `lvm`
    (réclamations `ns/nom` des volumes au pilote LVM ; sur un cluster d'avant
    1.9.1, ce sont des volumes d'avant la GA) et `striped` (ceux d'une classe
    `type: striped`)."""
    striped = {(_meta(c).get("name")) for c in storageclasses or []
               if c.get("provisioner") == LVM and (c.get("parameters") or {}).get("type") == "striped"}
    out = {"lvm": [], "striped": []}
    for p in pvs or []:
        sp = p.get("spec") or {}
        if (sp.get("csi") or {}).get("driver") != LVM:
            continue
        ref = sp.get("claimRef") or {}
        who = f"{ref.get('namespace')}/{ref.get('name')}" if ref.get("name") else _meta(p).get("name")
        out["lvm"].append(who)
        if sp.get("storageClassName") in striped:
            out["striped"].append(who)
    return {k: sorted(v) for k, v in out.items()}


def storage_class(spec, harvester_version=None):
    """Le formulaire de classe de Harvester : Longhorn v1 ou v2 (réplicas,
    sélecteurs, chiffrement), ou LVM (nœud, groupe de volumes, type),
    topologies permises, liaison, politique de récupération, description."""
    name = check_name(spec.get("name"), "storage class name")
    engine = spec.get("engine") or "longhorn-v1"
    reclaim = spec.get("reclaim") or "Delete"
    if reclaim not in ("Delete", "Retain"):
        raise ValueError("reclaim policy: Delete or Retain")
    binding = spec.get("binding") or ("WaitForFirstConsumer" if engine == "lvm" else "Immediate")
    if binding not in ("Immediate", "WaitForFirstConsumer"):
        raise ValueError("volume binding: Immediate or WaitForFirstConsumer")
    out = {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass", "metadata": {"name": name},
           "reclaimPolicy": reclaim, "volumeBindingMode": binding}
    if spec.get("description"):
        out["metadata"]["annotations"] = {DESC: str(spec["description"])[:1000]}
    if engine in ("longhorn-v1", "longhorn-v2"):
        replicas = str(spec.get("replicas") or "3")
        if replicas not in ("1", "2", "3"):
            raise ValueError("replicas: 1 to 3")
        params = {"numberOfReplicas": replicas, "staleReplicaTimeout": str(int(spec.get("stale_timeout") or 30)),
                  "migratable": "true" if spec.get("migratable", True) else "false",
                  "dataEngine": "v2" if engine == "longhorn-v2" else "v1"}
        for key, field in (("diskSelector", "disk_selector"), ("nodeSelector", "node_selector")):
            tags = [t.strip() for t in str(spec.get(field) or "").split(",") if t.strip()]
            if tags:
                params[key] = ",".join(tags)
        if spec.get("data_locality"):
            if spec["data_locality"] not in ("disabled", "best-effort"):
                raise ValueError("data locality: disabled or best-effort")
            params["dataLocality"] = spec["data_locality"]
        if spec.get("encrypted"):
            sec = str(spec.get("secret") or "")
            if "/" not in sec:
                raise ValueError("encryption: choose the secret (namespace/name) holding the passphrase")
            sns, sname = sec.split("/", 1)
            params["encrypted"] = "true"
            pairs = ["provisioner", "node-publish", "node-stage"] + (["node-expand"] if spec.get("expand_online") else [])
            for pfx in pairs:
                params[f"csi.storage.k8s.io/{pfx}-secret-name"] = sname
                params[f"csi.storage.k8s.io/{pfx}-secret-namespace"] = sns
        out["provisioner"] = LH
        out["allowVolumeExpansion"] = bool(spec.get("expansion", True))
        out["parameters"] = params
    elif engine == "lvm":
        node = str(spec.get("node") or "")
        vg = str(spec.get("vg") or "")
        if not node or not vg:
            raise ValueError("LVM: choose the node and its volume group")
        lvtype = spec.get("lvm_type") or "dm-thin"
        if lvtype not in ("striped", "dm-thin"):
            raise ValueError("LVM type: striped or dm-thin")
        if lvtype == "striped" and _dm_thin_only(harvester_version):
            raise ValueError("LVM type: Harvester v1.9.1 and later accept dm-thin only (striped is deprecated)")
        out["provisioner"] = LVM
        out["allowVolumeExpansion"] = bool(spec.get("expansion", False))
        out["parameters"] = {"vgName": vg, "type": lvtype}
        out["allowedTopologies"] = [{"matchLabelExpressions": [{"key": "topology.lvm.csi/node", "values": [node]}]}]
    else:
        raise ValueError("engine: longhorn-v1, longhorn-v2 or lvm")
    topo = [t for t in spec.get("topologies") or [] if t.get("key") and t.get("values")]
    if topo and engine != "lvm":
        out["allowedTopologies"] = [{"matchLabelExpressions": [
            {"key": t["key"], "values": [v.strip() for v in str(t["values"]).split(",") if v.strip()]} for t in topo]}]
    return out


# ---------------------------------------------------------------------------
# Secrets typés et clés SSH
# ---------------------------------------------------------------------------

def typed_secret(name, ns, stype, fields):
    """Un secret d'un des types que l'interface de Rancher sait éditer."""
    check_name(name, "secret name")
    check_name(ns, "namespace")
    if stype not in SECRET_TYPES:
        raise ValueError("type: " + ", ".join(SECRET_TYPES))
    f = fields or {}
    if stype == "Opaque":
        data = f.get("data") or {}
        if not data:
            raise ValueError("data: at least one key and its value")
        for k in data:
            if not KEY_RE.match(k):
                raise ValueError(f"key {k!r}: letters, digits, dot, dash, underscore")
        string = {k: str(v) for k, v in data.items()}
    elif stype == "kubernetes.io/basic-auth":
        if not f.get("username"):
            raise ValueError("user name required")
        string = {"username": f["username"], "password": f.get("password") or ""}
    elif stype == "kubernetes.io/ssh-auth":
        if "PRIVATE KEY" not in str(f.get("ssh-privatekey") or ""):
            raise ValueError("an SSH private key (-----BEGIN ... PRIVATE KEY-----)")
        string = {"ssh-privatekey": f["ssh-privatekey"]}
        if f.get("ssh-publickey"):
            string["ssh-publickey"] = f["ssh-publickey"]
    elif stype == "kubernetes.io/tls":
        if "BEGIN CERTIFICATE" not in str(f.get("tls.crt") or "") or "PRIVATE KEY" not in str(f.get("tls.key") or ""):
            raise ValueError("a PEM certificate and its private key")
        string = {"tls.crt": f["tls.crt"], "tls.key": f["tls.key"]}
    else:
        server = str(f.get("server") or "").strip()
        if not server or not f.get("username"):
            raise ValueError("registry server and user name required")
        string = {".dockerconfigjson": json.dumps({"auths": {server: {"username": f["username"],
                                                                     "password": f.get("password") or ""}}})}
    return {"apiVersion": "v1", "kind": "Secret", "type": stype,
            "metadata": {"name": name, "namespace": ns}, "stringData": string}


def secret_update(secret, fields):
    """Nouvelles valeurs d'un secret existant (le type ne change jamais) ;
    une valeur laissée vide garde l'ancienne. Rend l'objet à remplacer."""
    out = copy.deepcopy(secret)
    stype = out.get("type") or "Opaque"
    data = dict(out.get("data") or {})
    f = fields or {}
    if stype == "kubernetes.io/dockerconfigjson":
        if f.get("server") or f.get("username") or f.get("password"):
            old = json.loads(base64.b64decode(data.get(".dockerconfigjson", "e30=")).decode() or "{}")
            auths = old.get("auths") or {}
            server = f.get("server") or next(iter(auths), "")
            prev = auths.get(server) or {}
            auths = {server: {"username": f.get("username") or prev.get("username", ""),
                              "password": f.get("password") or prev.get("password", "")}}
            data[".dockerconfigjson"] = base64.b64encode(json.dumps({"auths": auths}).encode()).decode()
    else:
        for k, v in f.items():
            if k in ("remove",):
                continue
            if v in (None, ""):
                continue
            if not KEY_RE.match(k):
                raise ValueError(f"key {k!r}: letters, digits, dot, dash, underscore")
            data[k] = base64.b64encode(str(v).encode()).decode()
        for k in f.get("remove") or []:
            if stype != "Opaque":
                raise ValueError("only an Opaque secret loses keys")
            data.pop(k, None)
        if stype == "Opaque" and not data:
            raise ValueError("a secret keeps at least one key")
    out["data"] = data
    out.pop("stringData", None)
    for k in ("managedFields", "creationTimestamp", "uid"):
        out.get("metadata", {}).pop(k, None)
    return out


def keypair_update(kp, public_key, description=None):
    key = " ".join(str(public_key or "").split())
    if not re.match(r"^(ssh-(rsa|ed25519|dss)|ecdsa-sha2-nistp(256|384|521)|sk-(ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com) \S+", key):
        raise ValueError("public key: one OpenSSH public key line")
    out = copy.deepcopy(kp)
    out.setdefault("spec", {})["publicKey"] = key
    if description is not None:
        ann = out.setdefault("metadata", {}).setdefault("annotations", {})
        if description.strip():
            ann[DESC] = description.strip()[:1000]
        else:
            ann.pop(DESC, None)
    out.pop("status", None)
    return out
