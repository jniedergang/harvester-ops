"""harvester-ops : lire, télécharger et modifier en YAML (v1.60.0).

L'interface de Harvester offre « Edit YAML » et « Download YAML » sur presque
chaque objet. La console fait de même, pour une liste FERMÉE de types : chaque
type dit sa ressource kubectl, s'il vit dans un namespace, et qui peut le lire
et l'écrire. Un Secret, un réglage (ssl-certificates porte une clé privée) et
la configuration d'un add-on ne se lisent qu'en administrateur : leur YAML
porte des valeurs.

Fonctions pures : l'objet lu est nettoyé pour être relu par une personne
(ni managedFields ni status), et un YAML modifié est contrôlé contre sa cible
(même type, même nom, même namespace) avant d'être envoyé à kubectl, qui le
remplace. Le resourceVersion est gardé : si l'objet a changé entre-temps,
Kubernetes refuse au lieu d'écraser la modification de quelqu'un d'autre.
"""

import re

NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")

# type : (ressource kubectl, namespacé, rôle pour lire, rôle pour écrire, apiVersion attendue)
KINDS = {
    "vm": ("virtualmachines.kubevirt.io", True, "viewer", "operator", "kubevirt.io/v1", "VirtualMachine"),
    "image": ("virtualmachineimages.harvesterhci.io", True, "viewer", "operator",
              "harvesterhci.io/v1beta1", "VirtualMachineImage"),
    "volume": ("persistentvolumeclaims", True, "viewer", "operator", "v1", "PersistentVolumeClaim"),
    "storageclass": ("storageclasses.storage.k8s.io", False, "viewer", "admin",
                     "storage.k8s.io/v1", "StorageClass"),
    "sshkey": ("keypairs.harvesterhci.io", True, "viewer", "operator", "harvesterhci.io/v1beta1", "KeyPair"),
    "secret": ("secrets", True, "admin", "admin", "v1", "Secret"),
    "network": ("network-attachment-definitions.k8s.cni.cncf.io", True, "viewer", "admin",
                "k8s.cni.cncf.io/v1", "NetworkAttachmentDefinition"),
    "template": ("virtualmachinetemplates.harvesterhci.io", True, "viewer", "operator",
                 "harvesterhci.io/v1beta1", "VirtualMachineTemplate"),
    "templateversion": ("virtualmachinetemplateversions.harvesterhci.io", True, "viewer", "operator",
                        "harvesterhci.io/v1beta1", "VirtualMachineTemplateVersion"),
    "namespace": ("namespaces", False, "viewer", "admin", "v1", "Namespace"),
    "addon": ("addons.harvesterhci.io", True, "admin", "admin", "harvesterhci.io/v1beta1", "Addon"),
    "schedule": ("schedulevmbackups.harvesterhci.io", True, "viewer", "operator",
                 "harvesterhci.io/v1beta1", "ScheduleVMBackup"),
    "vmbackup": ("virtualmachinebackups.harvesterhci.io", True, "viewer", "operator",
                 "harvesterhci.io/v1beta1", "VirtualMachineBackup"),
    "volsnap": ("volumesnapshots.snapshot.storage.k8s.io", True, "viewer", "operator",
                "snapshot.storage.k8s.io/v1", "VolumeSnapshot"),
    "clusternetwork": ("clusternetworks.network.harvesterhci.io", False, "viewer", "admin",
                       "network.harvesterhci.io/v1beta1", "ClusterNetwork"),
    "vlanconfig": ("vlanconfigs.network.harvesterhci.io", False, "viewer", "admin",
                   "network.harvesterhci.io/v1beta1", "VlanConfig"),
    "ippool": ("ippools.loadbalancer.harvesterhci.io", False, "viewer", "admin",
               "loadbalancer.harvesterhci.io/v1beta1", "IPPool"),
    "loadbalancer": ("loadbalancers.loadbalancer.harvesterhci.io", True, "viewer", "operator",
                     "loadbalancer.harvesterhci.io/v1beta1", "LoadBalancer"),
    # v1.65.0 : les réseaux d'hôte
    "hostnetwork": ("hostnetworkconfigs.network.harvesterhci.io", False, "viewer", "admin",
                    "network.harvesterhci.io/v1beta1", "HostNetworkConfig"),
    # v1.66.0 : politiques réseau (celles que le formulaire ne sait pas dire)
    "networkpolicy": ("networkpolicies.networking.k8s.io", True, "viewer", "admin",
                      "networking.k8s.io/v1", "NetworkPolicy"),
    "setting": ("settings.harvesterhci.io", False, "admin", "admin", "harvesterhci.io/v1beta1", "Setting"),
    "node": ("nodes", False, "viewer", "admin", "v1", "Node"),
    "cloudtemplate": ("configmaps", True, "viewer", "operator", "v1", "ConfigMap"),
    "quota": ("resourcequotas", True, "viewer", "admin", "v1", "ResourceQuota"),
    "vpc": ("vpcs.kubeovn.io", False, "viewer", "admin", "kubeovn.io/v1", "Vpc"),
    "subnet": ("subnets.kubeovn.io", False, "viewer", "admin", "kubeovn.io/v1", "Subnet"),
    "pcidevice": ("pcidevices.devices.harvesterhci.io", False, "viewer", "admin",
                  "devices.harvesterhci.io/v1beta1", "PCIDevice"),
    "usbdevice": ("usbdevices.devices.harvesterhci.io", False, "viewer", "admin",
                  "devices.harvesterhci.io/v1beta1", "USBDevice"),
    "sriovnetworkdevice": ("sriovnetworkdevices.devices.harvesterhci.io", False, "viewer", "admin",
                           "devices.harvesterhci.io/v1beta1", "SRIOVNetworkDevice"),
    # v1.91.0 : les charges de conteneurs, en lecture seule (NOBODY n'est
    # aucun rôle : ni Enregistrer ni Vérifier ne s'ouvrent)
    "deployment": ("deployments.apps", True, "viewer", "nobody", "apps/v1", "Deployment"),
    "statefulset": ("statefulsets.apps", True, "viewer", "nobody", "apps/v1", "StatefulSet"),
    "daemonset": ("daemonsets.apps", True, "viewer", "nobody", "apps/v1", "DaemonSet"),
    "replicaset": ("replicasets.apps", True, "viewer", "nobody", "apps/v1", "ReplicaSet"),
    "job": ("jobs.batch", True, "viewer", "nobody", "batch/v1", "Job"),
    "cronjob": ("cronjobs.batch", True, "viewer", "nobody", "batch/v1", "CronJob"),
    "pod": ("pods", True, "viewer", "nobody", "v1", "Pod"),
}

RANK = {"viewer": 0, "operator": 1, "admin": 2}
# rôle qu'aucun compte ne porte : un type en lecture seule
NOBODY = "nobody"
CONTAINER_KINDS = ("deployment", "statefulset", "daemonset", "replicaset", "job", "cronjob", "pod")

# Ce qui n'aide pas à relire ni à modifier un objet : l'historique d'écriture
# de chaque champ (des centaines de lignes sur une VM) et l'état, que le
# serveur ignore sur un remplacement.
DROP_META = ("managedFields", "selfLink")

CLOUD_TEMPLATE_LABEL = "harvesterhci.io/cloud-init-template"


def spec_of(kind):
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(sorted(KINDS))}")
    res, namespaced, read, write, api, k8s_kind = KINDS[kind]
    return {"resource": res, "namespaced": namespaced, "read": read, "write": write,
            "apiVersion": api, "kind": k8s_kind}


def allowed(kind, role, write=False):
    s = spec_of(kind)
    need = s["write" if write else "read"]
    if need == NOBODY:
        return False
    return RANK.get(role, -1) >= RANK[need]


def clean(obj):
    """L'objet tel qu'une personne le relit et le renvoie : sans status ni
    managedFields, avec son resourceVersion (garde contre l'écrasement)."""
    out = {k: v for k, v in (obj or {}).items() if k != "status"}
    meta = dict(out.get("metadata") or {})
    for k in DROP_META:
        meta.pop(k, None)
    ann = dict(meta.get("annotations") or {})
    # la copie complète de l'objet que garde `kubectl apply` : un doublon illisible
    ann.pop("kubectl.kubernetes.io/last-applied-configuration", None)
    if ann:
        meta["annotations"] = ann
    else:
        meta.pop("annotations", None)
    out["metadata"] = meta
    return out


def check_target(kind, obj, ns, name, creating=False):
    """Le YAML modifié vise bien l'objet ouvert (même type, nom, namespace).
    Rend l'objet, namespace complété. En création, le nom est libre."""
    s = spec_of(kind)
    if not isinstance(obj, dict):
        raise ValueError("the YAML must describe one object (a mapping), not a list")
    if obj.get("kind") in ("List",) or isinstance(obj.get("items"), list):
        raise ValueError("one object at a time: a List is not accepted here")
    if obj.get("kind") != s["kind"]:
        raise ValueError(f"this YAML describes a {obj.get('kind') or '?'}, a {s['kind']} is expected")
    api = str(obj.get("apiVersion") or "")
    if api.split("/")[0] != s["apiVersion"].split("/")[0] or ("/" in api) != ("/" in s["apiVersion"]):
        raise ValueError(f"apiVersion {api or '?'} does not belong to a {s['kind']} ({s['apiVersion']})")
    meta = obj.get("metadata")
    if not isinstance(meta, dict):
        raise ValueError("metadata is missing")
    got_name = meta.get("name")
    if creating:
        if not got_name and not meta.get("generateName"):
            raise ValueError("metadata.name is required")
        if got_name and not NAME_RE.match(str(got_name)):
            raise ValueError("metadata.name: lower-case letters, digits, dots and dashes")
    elif got_name != name:
        raise ValueError(f"metadata.name is {got_name!r}: the object opened is {name!r} (renaming is not an edit)")
    if s["namespaced"]:
        got_ns = meta.get("namespace") or ns
        if not creating and got_ns != ns:
            raise ValueError(f"metadata.namespace is {got_ns!r}: the object opened is in {ns!r}")
        if not got_ns or not NAME_RE.match(str(got_ns)):
            raise ValueError("metadata.namespace is required")
        meta["namespace"] = got_ns
    elif meta.get("namespace"):
        raise ValueError(f"a {s['kind']} has no namespace")
    if kind == "cloudtemplate":
        labels = meta.get("labels") or {}
        if CLOUD_TEMPLATE_LABEL not in labels:
            raise ValueError(f"a cloud configuration template carries the label {CLOUD_TEMPLATE_LABEL}")
    return obj


def check_readable(kind, obj):
    """Une ConfigMap n'est lue ici que si c'est un modèle cloud-init ; une
    charge de conteneurs, hors des namespaces système (masqués par la vue)."""
    if kind in CONTAINER_KINDS:
        import hv_containers
        ns = ((obj or {}).get("metadata") or {}).get("namespace")
        if hv_containers.is_system_namespace(ns):
            raise ValueError(f"{ns} is a system namespace: its workloads are not read here")
    if kind == "cloudtemplate":
        labels = ((obj or {}).get("metadata") or {}).get("labels") or {}
        if CLOUD_TEMPLATE_LABEL not in labels:
            raise ValueError("this ConfigMap is not a cloud configuration template")
    return obj
