"""Inventaire des écrans de la navigation 2.0 (tâche 10) : table unique.

Une entrée par ligne du tableau de correspondance de la conception
(`docs/design/2026-10-24-navigation-hotes-vms.md`, « Correspondance des
onglets ») et par écran ajouté depuis (index des hôtes, page d'un hôte KVM,
Disaster Recovery de la 1.101.0) : ancien emplacement, adresse nouvelle
(`{c}` = cluster Harvester, `{h}` = hôte KVM), section `tab-*` atteinte,
sélecteur DOM attendu, anciens jetons qui y mènent.

Lue par `tests/api/test_nav_inventory_200.py` (chaque section du modèle a
au moins une adresse, chaque ancien jeton a sa ligne, chaque adresse se
résout vers sa section) et `tests/e2e/test_nav_inventory_200.py` (chaque
adresse chargée à froid montre son sélecteur).
"""

import re

# (ancien emplacement, adresse, section, sélecteur, anciens jetons)
SCREENS = [
    ("Cluster selector (#cluster-select)", "#hosts", "hosts", "#host-nav #hnav-families .hnav-host", ()),
    ("+ button next to the selector", "#hosts", "hosts", "#host-nav #hnav-add", ()),
    ("Cluster > Overview > Metrics", "#harvester/{c}", "overview",
     '#tab-overview .overview-subtab[data-subtab="metrics"]', ("overview", "cluster")),
    ("Cluster > Overview > Cluster (node blocks)", "#harvester/{c}/params/nodes", "overview",
     '#tab-overview .overview-subtab[data-subtab="cluster"]', ()),
    ("Cluster > Overview > Events", "#harvester/{c}/overview/events", "overview",
     '#tab-overview .overview-subtab[data-subtab="events"]', ()),
    ("Cluster > Overview, Upgrade button", "#harvester/{c}/params/upgrade", "hupgrade", "#hupgrade-host", ()),
    ("Cluster > Storage", "#harvester/{c}/params/storage", "storage", "#tab-storage .section-pane", ("storage",)),
    ("Cluster > Network (7 tabs)", "#harvester/{c}/params/network/overlay", "network",
     '#tab-network .section-pane[data-pane="overlay"]', ("network",)),
    ("Cluster > Add-ons", "#harvester/{c}/params/addons", "addons", "#tab-addons", ("addons",)),
    ("Cluster > Security", "#harvester/{c}/params/security", "security", "#tab-security .section-pane", ("security",)),
    ("Settings > Cluster accounts", "#harvester/{c}/params/security/accounts", "security", "#husers-body", ()),
    ("Cluster > Monitoring & Logging", "#harvester/{c}/params/monlog", "monlog", "#tab-monlog", ("monlog",)),
    ("Cluster > Advanced", "#harvester/{c}/params/advanced/pci", "advanced",
     '#tab-advanced .section-pane[data-pane="pci"]', ("advanced",)),
    ("Cluster > Startup", "#harvester/{c}/params/power/startup", "startup", "#btn-startup", ("startup",)),
    ("Cluster > Shutdown", "#harvester/{c}/params/power/shutdown", "shutdown", "#btn-shutdown", ("shutdown",)),
    ("Virtual machines > Current cluster's VMs", "#harvester/{c}/vms", "namespaces", "#btn-vm-backups",
     ("namespaces",)),
    ("Virtual machines > All clusters VMs", "#vms", "allvms", "#all-vms-host", ("allvms",)),
    ("Folders (VMs, containers, hosts)", "#folders", "folders", "#folders-view-host", ()),
    ("Containers > Current cluster's containers", "#harvester/{c}/containers", "containers", "#containers-host", ()),
    ("Containers > All clusters containers", "#containers", "allcontainers", "#all-containers-host",
     ("containers", "allcontainers")),
    ("Shared resources > GPU", "#gpus", "gpus", "#gpus-host", ("gpus",)),
    ("Shared resources > Bookings", "#bookings", "bookings", "#bookings-host", ("bookings",)),
    ("Shared resources > Usage", "#usage", "usage", "#usage-host", ("usage",)),
    ("KVM > Hosts (index)", "#hosts/kvm", "hosts", "#hosts-index-host .hix-card", ("kvmhosts", "kvm-hosts")),
    ("KVM > Hosts (block of one host)", "#kvm/{h}", "kvmhosts", '#kvm-hosts-host [data-kvm-host]', ()),
    ("KVM > Hosts (Test, Remove)", "#kvm/{h}/params/conn", "kvmparams", "#kvmparams-host .hp-kvm-conn", ()),
    ("KVM host: versions, CPU, memory", "#kvm/{h}/params/system", "kvmparams", "#kvmparams-host .hp-kvm-system", ()),
    ("KVM host: labels", "#kvm/{h}/params/labels", "kvmparams", "#kvmparams-host .hp-kvm-labels", ()),
    ("KVM > Virtual machines", "#vms/host/kvm", "kvmvms", "#kvm-vms-host .kvm-card", ("kvmvms",)),
    # 2.0.0 (tâche 15) : la vue des VMs, panneau Hôtes sur un hôte
    ("Virtual machines of one host (Hosts panel)", "#vms/host/kvm/{h}", "allvms", "#all-vms-host .vmf-layout", ()),
    ("KVM > Virtual machines (one host)", "#kvm/{h}/vms", "kvmvms", "#kvm-vms-host .kvm-card", ()),
    ("KVM > Hosts (CPU and memory tiles of all hosts)", "#hosts/kvm/capacity", "kvmhosts",
     '#kvm-hosts-host [data-kvm-host]', ()),
    ("KVM > Storage (pools and images of all hosts)", "#hosts/kvm/pools", "kvmstorage",
     "#kvm-storage-host .kvm-card", ("kvmstorage",)),
    ("KVM > Networks (all hosts)", "#hosts/kvm/networks", "kvmnetworks", "#kvm-networks-host .kvm-card",
     ("kvmnetworks",)),
    ("KVM > Storage (one host)", "#kvm/{h}/params/pools", "kvmstorage", "#kvm-storage-host .kvm-card", ()),
    ("KVM > Networks (one host)", "#kvm/{h}/params/networks", "kvmnetworks", "#kvm-networks-host .kvm-card", ()),
    ("VM Import / Export > VM Import", "#harvester/{c}/imports/vmimport", "vmimport", "#tab-vmimport",
     ("vmimport", "vmio")),
    ("VM Import / Export > VMware migrations", "#harvester/{c}/imports/forklift", "forklift", "#tab-forklift",
     ("forklift",)),
    ("VM Import / Export > Migrations (all clusters)", "#migrations", "forkliftglobal", "#forklift-global-host",
     ("forkliftglobal",)),
    ("Automation > Cluster API", "#automation/capi", "automation",
     '#tab-automation > .sub-tab-content[data-subtab="capi"].active', ("automation", "automation/capi")),
    ("Automation > Terraform", "#automation/terraform", "automation",
     '#tab-automation > .sub-tab-content[data-subtab="terraform"].active', ("automation/terraform",)),
    ("Automation > Bare-metal", "#automation/pxe", "automation",
     '#tab-automation > .sub-tab-content[data-subtab="pxe"].active', ("automation/pxe",)),
    # 2.1.0 : Bare-metal en onglets ; #automation/pxe seul ouvre Machines
    ("Bare-metal > discovery and machines (one column)", "#automation/pxe/machines", "automation",
     '#bmc-body [data-bm-pane="machines"]:not([hidden])', ()),
    ("Bare-metal > installation profiles", "#automation/pxe/profiles", "automation",
     '#bmc-body [data-bm-pane="profiles"]:not([hidden])', ()),
    # 2.4.0 : plans de déploiement et modèles de déclaration
    ("Bare-metal > deployment plans", "#automation/pxe/plans", "automation",
     '#bmc-body [data-bm-pane="plans"]:not([hidden])', ()),
    ("Bare-metal > ISO store", "#automation/pxe/images", "automation",
     '#bmc-body [data-bm-pane="images"]:not([hidden])', ()),
    ("(new) Bare-metal > history", "#automation/pxe/history", "automation",
     '#bmc-body [data-bm-pane="history"]:not([hidden])', ()),
    # 2.2.0 : Cluster API et Terraform en onglets ; #automation/capi seul
    # ouvre Clusters, #automation/terraform seul ouvre Déclarations
    ("Cluster API > K8S Clusters", "#automation/capi/clusters", "automation",
     '#tab-automation .capi-tab-content[data-capi-tab="clusters"].active', ()),
    ("Cluster API > Services", "#automation/capi/services", "automation",
     '#tab-automation .capi-tab-content[data-capi-tab="services"].active', ()),
    ("(new) Cluster API > GitOps", "#automation/capi/gitops", "automation",
     '#tab-automation .capi-tab-content[data-capi-tab="gitops"].active', ()),
    ("(new) Cluster API > External Secrets", "#automation/capi/secrets", "automation",
     '#tab-automation .capi-tab-content[data-capi-tab="secrets"].active', ()),
    ("(new) Cluster API > Registries", "#automation/capi/registries", "automation",
     '#tab-automation .capi-tab-content[data-capi-tab="registries"].active', ()),
    ("(new) Cluster API > Artefacts", "#automation/capi/artefacts", "automation",
     '#tab-automation .capi-tab-content[data-capi-tab="artefacts"].active', ()),
    ("(new) Cluster API > history", "#automation/capi/history", "automation",
     '#tab-automation .capi-tab-content[data-capi-tab="history"].active', ()),
    ("Cluster API > Installation (was the first tab)", "#automation/capi/installation", "automation",
     '#tab-automation .capi-tab-content[data-capi-tab="installation"].active', ()),
    ("Terraform > Declarations", "#automation/terraform/declarations", "automation",
     '#tf-status-body [data-tf-pane="declarations"]:not([hidden])', ()),
    ("Terraform > Cluster resources", "#automation/terraform/resources", "automation",
     '#tf-status-body [data-tf-pane="resources"]:not([hidden])', ()),
    ("(new) Terraform > history", "#automation/terraform/history", "automation",
     '#tf-status-body [data-tf-pane="history"]:not([hidden])', ()),
    ("Terraform > Installation", "#automation/terraform/installation", "automation",
     '#tf-status-body [data-tf-pane="installation"]:not([hidden])', ()),
    ("Activity", "#activity", "activity", "#activity-running", ("activity",)),
    ("Activity filtered on a host", "#activity/host/harvester/{c}", "activity", "#act-f-cluster", ()),
    ("Settings > Clusters (shortcut)", "#harvester/{c}/params/conn", "hconn", "#hconn-host", ()),
    ("Disaster Recovery > Contracts", "#dr", "drplans", "#rp-plans-host", ("drplans",)),
    ("Disaster Recovery > Links", "#dr/links", "drlinks", "#rp-links-host", ("drlinks",)),
    ("Cluster > Disaster Recovery", "#harvester/{c}/dr", "clusterdr", "#rp-cluster-host", ("clusterdr",)),
    ("(new) Disaster Recovery of a KVM host", "#kvm/{h}/dr", "kvmdr", "#tab-kvmdr .kvm-dr-empty", ()),
    ("(new) Hosts index", "#hosts", "hosts", "#hosts-index-host .hix-card", ()),
]

SECTION_RE = re.compile(r'<section id="tab-([a-z]+)"')


def sections(html):
    """Identifiants des sections `tab-*` du modèle."""
    return sorted(set(SECTION_RE.findall(html)))


def orphans(html, screens=SCREENS):
    """Sections du modèle qu'aucune adresse de l'inventaire n'atteint."""
    reached = {s[2] for s in screens}
    return [s for s in sections(html) if s not in reached]


def address(entry, cluster, kvm):
    return entry[1].replace("{c}", cluster).replace("{h}", kvm)
