"""harvester-ops : la mise à jour de Harvester (v1.69.0).

Relevé dans harvester-ui-extension et harvester v1.9.0 (scratchpad du
chantier : parity/harvester-upgrade-1.9.md) :

- une `Version` (harvester-system) donne l'URL et le SHA-512 d'un ISO ;
  l'interface de Harvester propose toutes celles qui existent ; un
  `Upgrade` {generateName hvst-upgrade-, spec.version | spec.image,
  logEnabled} lance la mise à jour ; en airgap, l'ISO est une
  VirtualMachineImage CDI en `longhorn-static` annotée os-upgrade-image ;
- le contrôleur avance par conditions (LogReady, ImageReady, RepoReady,
  NodesPrepared, SystemServicesUpgraded, NodesUpgraded, Completed), états
  par nœud et labels (upgradeState, upgradeCleanup, latestUpgrade) ;
- le contrôle de version n'a lieu qu'APRÈS le téléchargement et le montage
  de l'ISO : la console le refait avant, sur le version.yaml ou sur le
  harvester-release.yaml de l'ISO ;
- le SHA-512 d'un ISO n'est pas vérifié par le backend CDI : la console le
  vérifie elle-même avant de servir l'ISO.

Fonctions pures ; bin/harvester-resources.py `upgrade` les applique.
"""

import json
import re

NS = "harvester-system"
K_VERSION = "versions.harvesterhci.io"
# « upgrade » au singulier désigne plans.upgrade.cattle.io pour kubectl
K_UPGRADE = "upgrades.harvesterhci.io"
K_UPGRADELOG = "upgradelogs.harvesterhci.io"
K_IMAGE = "virtualmachineimages.harvesterhci.io"
L_LATEST = "harvesterhci.io/latestUpgrade"
L_STATE = "harvesterhci.io/upgradeState"
L_CLEANUP = "harvesterhci.io/upgradeCleanup"
L_READ = "harvesterhci.io/read-message"
A_SKIP_SINGLE = "harvesterhci.io/skipSingleReplicaDetachedVol"
A_PAUSE = "harvesterhci.io/node-upgrade-pause-map"
A_OS_IMAGE = "harvesterhci.io/os-upgrade-image"
# 2.10.1 (relecture I-2) : valeur de `upgrade-config` d'avant la mise à jour,
# gardée sur l'Upgrade créée par la console (JSON brut, sans secret) pour la
# rendre même si le processus qui suivait la mise à jour est perdu
A_CONFIG_BEFORE = "harvester-ops.io/upgrade-config-before"
CONDITIONS = ("LogReady", "ImageReady", "RepoReady", "NodesPrepared", "SystemServicesUpgraded",
              "NodesUpgraded", "Completed")
SHA512_RE = re.compile(r"^[0-9a-f]{128}$")
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,61}[a-z0-9])?$")
NOTES = "https://github.com/harvester/harvester/releases/tag/"


def _meta(o):
    return (o or {}).get("metadata") or {}


def check_name(name, what="name"):
    name = str(name or "").strip()
    if not NAME_RE.match(name):
        raise ValueError(f"{what}: a lowercase name (letters, digits, dashes, dots)")
    return name


# ---------------------------------------------------------------------------
# Versions et éligibilité (la règle du contrôleur, faite avant)
# ---------------------------------------------------------------------------

_VER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-(.+))?$")


def parse_version(v):
    """(majeur, mineur, correctif, préversion) ; « dev » pour une version de
    développement ; None si illisible."""
    s = str(v or "").strip()
    if s in ("dev", "master") or s.endswith("-dev") or "-head" in s:
        return "dev"
    m = _VER.match(s)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) or ""


def eligible(current, target, min_upgradable=None):
    """(True, "") ou (False, la raison), comme Harvester la donnerait après
    avoir téléchargé l'ISO (versionguard) ; (None, raison) si illisible."""
    c, t = parse_version(current), parse_version(target)
    if t == "dev":
        return True, ""
    if c == "dev":
        return False, "upgrading from dev versions to non-dev versions is prohibited"
    if c is None or t is None:
        return None, f"cannot read the versions {current} and {target}"
    if c[:3] == t[:3] and c[3] == t[3]:
        return True, "same version"
    if (c[3] or t[3]) and c[:3] != t[:3]:
        return False, "cross-version upgrades from/to any prerelease version are prohibited"
    if t[:3] < c[:3] or (t[:3] == c[:3] and t[3] and not c[3]):
        return False, "downgrading is prohibited"
    m = parse_version(min_upgradable) if min_upgradable else None
    if m and m != "dev" and c[:3] < m[:3]:
        return False, f"current version does not meet minimum upgrade requirement ({min_upgradable})"
    return True, ""


def version_from_yaml(text):
    """Un version.yaml publié (releases.rancher.com/harvester/<v>/version.yaml)."""
    import yaml
    try:
        d = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"version file: not YAML ({e})") from None
    spec = d.get("spec") or {}
    name = (d.get("metadata") or {}).get("name")
    if d.get("kind") not in (None, "Version") or not name or not spec.get("isoURL"):
        raise ValueError("version file: a Harvester Version with metadata.name and spec.isoURL")
    return {"name": name, "iso_url": spec["isoURL"], "checksum": spec.get("isoChecksum") or "",
            "release_date": str(spec.get("releaseDate") or ""),
            "min_upgradable": spec.get("minUpgradableVersion") or "", "tags": list(spec.get("tags") or [])}


def release_info(text):
    """Le harvester-release.yaml d'un ISO : version, OS, Kubernetes, minimum."""
    import yaml
    try:
        d = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        d = {}
    if not isinstance(d, dict) or not d.get("harvester"):
        raise ValueError("this ISO has no harvester-release.yaml: not a Harvester ISO")
    return {"harvester": str(d["harvester"]), "os": str(d.get("os") or ""), "kubernetes": str(d.get("kubernetes") or ""),
            "rancher": str(d.get("rancher") or ""), "min_upgradable": str(d.get("minUpgradableVersion") or "")}


def version_manifest(name, iso_url, checksum, release_date="", min_upgradable="", tags=()):
    check_name(name, "version name")
    if not re.match(r"^https?://", str(iso_url or "")):
        raise ValueError("ISO URL: an http(s) address the cluster can reach")
    checksum = str(checksum or "").strip().lower()
    if checksum and not SHA512_RE.match(checksum):
        raise ValueError("ISO checksum: a SHA-512 (128 hexadecimal characters)")
    spec = {"isoURL": iso_url}
    if checksum:
        spec["isoChecksum"] = checksum
    if release_date:
        spec["releaseDate"] = str(release_date)
    if min_upgradable:
        spec["minUpgradableVersion"] = str(min_upgradable)
    if tags:
        spec["tags"] = [str(t) for t in tags]
    return {"apiVersion": "harvesterhci.io/v1beta1", "kind": "Version",
            "metadata": {"name": name, "namespace": NS}, "spec": spec}


def version_rows(versions, current):
    out = []
    for v in versions or []:
        name = _meta(v).get("name")
        sp = v.get("spec") or {}
        ok, why = eligible(current, name, sp.get("minUpgradableVersion"))
        out.append({"name": name, "iso_url": sp.get("isoURL") or "", "checksum": bool(sp.get("isoChecksum")),
                    "release_date": sp.get("releaseDate") or "", "min_upgradable": sp.get("minUpgradableVersion") or "",
                    "tags": sp.get("tags") or [], "eligible": ok, "reason": why,
                    "notes": NOTES + name if parse_version(name) not in (None, "dev") else ""})
    return sorted(out, key=lambda r: r["name"], reverse=True)


# ---------------------------------------------------------------------------
# Objets à créer
# ---------------------------------------------------------------------------

def upgrade_manifest(version=None, image=None, log=True, skip_single=False):
    """L'Upgrade de l'interface de Harvester ; l'annotation de saut n'est
    posée que si elle est demandée, et à "true" (Harvester saute le contrôle
    pour toute valeur non vide, "false" compris)."""
    if not version and not image:
        raise ValueError("give a version or an OS image")
    spec = {"logEnabled": bool(log)}
    if image:
        ns, _, name = str(image).partition("/")
        check_name(ns, "image namespace")
        check_name(name, "image name")
        spec["image"] = f"{ns}/{name}"
    else:
        spec["version"] = check_name(version, "version")
    meta = {"generateName": "hvst-upgrade-", "namespace": NS}
    if skip_single:
        meta["annotations"] = {A_SKIP_SINGLE: "true"}
    return {"apiVersion": "harvesterhci.io/v1beta1", "kind": "Upgrade", "metadata": meta, "spec": spec}


def os_image_manifest(display_name, url, checksum=""):
    """L'ISO d'une mise à jour airgap : image CDI en longhorn-static annotée
    os-upgrade-image (sans l'annotation, le webhook des PVC refuse la classe
    réservée et l'import reste en échec)."""
    display_name = str(display_name or "").strip()
    if not display_name or len(display_name) > 63:
        raise ValueError("image name: 63 characters at most")
    spec = {"displayName": display_name, "sourceType": "download", "url": url, "backend": "cdi",
            "targetStorageClassName": "longhorn-static", "retry": 3}
    if checksum:
        spec["checksum"] = str(checksum).lower()
    return {"apiVersion": "harvesterhci.io/v1beta1", "kind": "VirtualMachineImage",
            "metadata": {"generateName": "image-", "namespace": NS,
                         "annotations": {A_OS_IMAGE: "True", "harvesterhci.io/image-name": display_name}},
            "spec": spec}


def pause_patch(upg, node, action="unpause"):
    if action not in ("pause", "unpause"):
        raise ValueError("pause or unpause")
    try:
        cur = json.loads((_meta(upg).get("annotations") or {}).get(A_PAUSE) or "{}")
    except ValueError:
        cur = {}
    cur[check_name(node, "node")] = action
    return {"metadata": {"annotations": {A_PAUSE: json.dumps(cur, separators=(",", ":"))}}}


# ---------------------------------------------------------------------------
# État
# ---------------------------------------------------------------------------

def _labels(u):
    return _meta(u).get("labels") or {}


def running(upgrades):
    """L'Upgrade qui bloque une nouvelle mise à jour (règle du webhook :
    état ni Succeeded ni Failed, y compris sans état), ou None."""
    for u in upgrades or []:
        if _labels(u).get(L_STATE) not in ("Succeeded", "Failed"):
            return _meta(u).get("name")
    return None


def cleanup_pending(upgrades):
    return sorted(_meta(u).get("name") for u in upgrades or [] if _labels(u).get(L_CLEANUP) == "Pending")


def latest(upgrades):
    lat = [u for u in upgrades or [] if _labels(u).get(L_LATEST) == "true"]
    pool = lat or list(upgrades or [])
    return max(pool, key=lambda u: _meta(u).get("creationTimestamp") or "", default=None)


def _cond(u, t):
    return next((c for c in ((u.get("status") or {}).get("conditions") or []) if c.get("type") == t), None)


def failure(u):
    """Cause d'un échec : Completed (le contrôleur met le texte dans reason),
    sinon la première condition fausse, sinon un nœud en échec."""
    c = _cond(u, "Completed")
    if c and str(c.get("status")) == "False" and (c.get("message") or c.get("reason")):
        return c.get("message") or c.get("reason")
    for t in CONDITIONS[:-1]:
        c = _cond(u, t)
        if c and str(c.get("status")) == "False" and c.get("reason") != "Disabled":
            return f"{t}: {c.get('message') or c.get('reason') or 'failed'}"
    for n, s in ((u.get("status") or {}).get("nodeStatuses") or {}).items():
        if s.get("state") == "Failed":
            return f"{n}: {s.get('message') or s.get('reason') or 'failed'}"
    return ""


def upgrade_view(u, image=None, nodes=()):
    if not u:
        return None
    st = u.get("status") or {}
    labels = _labels(u)
    conds = []
    for t in CONDITIONS:
        c = _cond(u, t)
        conds.append({"type": t, "status": (c or {}).get("status") or "", "reason": (c or {}).get("reason") or "",
                      "message": (c or {}).get("message") or "",
                      "time": (c or {}).get("lastTransitionTime") or (c or {}).get("lastUpdateTime") or ""})
    done = _cond(u, "Completed")
    completed = None if not done or str(done.get("status")) == "Unknown" else str(done.get("status")) == "True"
    ns_ = st.get("nodeStatuses") or {}
    names = sorted(set(ns_) | {_meta(n).get("name") for n in nodes or []} - {None})
    try:
        paused = {k for k, v in json.loads((_meta(u).get("annotations") or {}).get(A_PAUSE) or "{}").items() if v == "pause"}
    except ValueError:
        paused = set()
    node_rows = [{"name": n, "state": (ns_.get(n) or {}).get("state") or "Pending",
                  "reason": (ns_.get(n) or {}).get("reason") or "", "message": (ns_.get(n) or {}).get("message") or "",
                  "paused": (ns_.get(n) or {}).get("state") == "Node-upgrade paused" or n in paused} for n in names]
    repo = {}
    try:
        import yaml
        repo = ((yaml.safe_load(st.get("repoInfo") or "") or {}).get("release") or {}) if st.get("repoInfo") else {}
    except Exception:  # noqa: BLE001 - repoInfo illisible : on s'en passe
        repo = {}
    target = repo.get("harvester") or (u.get("spec") or {}).get("version") or ""
    nodes_cond = _cond(u, "NodesUpgraded")
    return {
        "name": _meta(u).get("name"), "created": _meta(u).get("creationTimestamp"),
        "version": (u.get("spec") or {}).get("version") or "", "image": (u.get("spec") or {}).get("image") or "",
        "target": target, "previous": st.get("previousVersion") or "", "log": bool((u.get("spec") or {}).get("logEnabled", True)),
        "state": labels.get(L_STATE) or "", "cleanup": labels.get(L_CLEANUP) or "", "dismissed": labels.get(L_READ) == "true",
        "completed": completed, "failure": failure(u) if completed is False or any(
            c["status"] == "False" and c["reason"] != "Disabled" for c in conds) else "",
        "conditions": conds, "nodes": node_rows, "single_node": st.get("singleNode") or "",
        "image_progress": ((image or {}).get("status") or {}).get("progress"),
        "repo": {k: str(repo.get(k) or "") for k in ("harvester", "os", "kubernetes", "rancher", "monitoringChart")},
        "notes": NOTES + target if parse_version(target) not in (None, "dev") else "",
        "log_name": st.get("upgradeLog") or "",
        # l'abandon est refusé par Harvester pendant toute la phase nœuds
        "can_abort": completed is None and not (nodes_cond and str(nodes_cond.get("status")) == "Unknown"),
        "can_dismiss": completed is not None and not labels.get(L_READ) == "true",
    }


def settled(u):
    """(True, msg) réussi, (False, msg) échoué, (None, msg) en cours."""
    v = upgrade_view(u)
    if v is None:
        return False, "the upgrade object disappeared"
    if v["completed"] is True:
        return True, f"Harvester upgraded to {v['target'] or v['version']}"
    if v["completed"] is False:
        return False, v["failure"] or "the upgrade failed"
    cur = next((c for c in reversed(v["conditions"]) if c["status"] in ("True", "Unknown")), None)
    nodes = ", ".join(f"{n['name']}: {n['state']}" for n in v["nodes"])
    return None, f"{v['state'] or 'starting'}" + (f" ({cur['type']})" if cur else "") + (f", {nodes}" if nodes else "")


# ---------------------------------------------------------------------------
# Pré-contrôles faits d'avance (le webhook refuserait)
# ---------------------------------------------------------------------------

def prechecks(nodes=(), volumes=(), vmbackups=(), schedules=(), addons=(), charts=(), upgrades=()):
    """[{ok, text}] : ce que la console peut voir d'avance parmi les refus
    du webhook ; le reste (place disque, certificats, VMs non migrables...)
    reste dit par Harvester à la création."""
    out = []
    run = running(upgrades)
    out.append({"ok": not run, "key": "running", "text": f"upgrade {run} is still in progress" if run else ""})
    pend = cleanup_pending(upgrades)
    out.append({"ok": not pend, "key": "cleanup", "text": ", ".join(pend)})
    bad = [_meta(n).get("name") for n in nodes or []
           if not any(c.get("type") == "Ready" and c.get("status") == "True" for c in (n.get("status") or {}).get("conditions") or [])]
    out.append({"ok": not bad, "key": "nodes-ready", "text": ", ".join(bad)})
    cordoned = [_meta(n).get("name") for n in nodes or [] if (n.get("spec") or {}).get("unschedulable")]
    out.append({"ok": not cordoned, "key": "nodes-schedulable", "text": ", ".join(cordoned)})
    if len(nodes or []) >= 3:
        deg = [_meta(v).get("name") for v in volumes or [] if ((v.get("status") or {}).get("robustness")) == "degraded"]
        out.append({"ok": not deg, "key": "volumes", "text": ", ".join(deg[:5])})
    busy = [f"{_meta(b).get('namespace')}/{_meta(b).get('name')}" for b in vmbackups or []
            if not ((b.get("status") or {}).get("readyToUse")) and not (b.get("status") or {}).get("error")]
    out.append({"ok": not busy, "key": "backups", "text": ", ".join(busy[:5])})
    active = [f"{_meta(s).get('namespace')}/{_meta(s).get('name')}" for s in schedules or []
              if not (s.get("spec") or {}).get("suspend")]
    out.append({"ok": not active, "key": "schedules", "text": ", ".join(active[:5])})
    moving = [f"{_meta(a).get('namespace')}/{_meta(a).get('name')}" for a in addons or []
              if ((a.get("status") or {}).get("status") or "") not in ("", "AddonDeploySuccessful", "AddonDisabled",
                                                                      "AddonInitState")]
    out.append({"ok": not moving, "key": "addons", "text": ", ".join(moving)})
    notready = [_meta(c).get("name") for c in charts or []
                if not any(x.get("type") == "Ready" and x.get("status") == "True"
                           for x in (c.get("status") or {}).get("conditions") or [])]
    out.append({"ok": not notready, "key": "charts", "text": ", ".join(notready)})
    return out


# ---------------------------------------------------------------------------
# 2.10.1 : contrôles propres à une cible v1.9.1 ou plus (notes de version
# v1.9.1 : pilote CSI LVM en GA, volumes d'avant la GA perdus ; type LVM
# striped déprécié ; add-on rancher-vcluster déprécié, retiré en v1.10.0 ;
# ingress-nginx remplacé par Traefik ; upgrade-config gagne
# shutdownVMOnMigrationFailure). Jugés sur la version LUE de la cible.
# ---------------------------------------------------------------------------

TARGET_191 = (1, 9, 1)
# Clé au premier niveau du défaut de `upgrade-config` (lu sur harvlab et harv1
# en 1.9.1 le 27/11/2026 : `"shutdownVMOnMigrationFailure": false`)
MIGRATION_POLICY_KEY = "shutdownVMOnMigrationFailure"
VCLUSTER_ADDON = "rancher-vcluster"
NGINX_ANN_PREFIX = "nginx.ingress.kubernetes.io/"


def _at_least(v, ref):
    p = parse_version(v)
    return p == "dev" or (isinstance(p, tuple) and p[:3] >= ref)


# Ingress de Harvester et de Rancher : la mise à jour les migre elle-même
# (upgrade_manifests.sh v1.9.1, ensure_ingress_class_name pour rancher-expose ;
# vu sur harvlab et harv1 en 1.9.1 : rancher-expose passé en traefik).
SYSTEM_INGRESS_NAMESPACES = ("kube-system", "harvester-system", "longhorn-system")


def _system_ns(ns):
    return ns in SYSTEM_INGRESS_NAMESPACES or str(ns or "").startswith("cattle-")


def _nginx_ingresses(ingresses):
    out = []
    for i in ingresses or []:
        md = _meta(i)
        if _system_ns(md.get("namespace")):
            continue
        cls = (i.get("spec") or {}).get("ingressClassName") or \
            (md.get("annotations") or {}).get("kubernetes.io/ingress.class")
        if cls == "nginx" or any(str(k).startswith(NGINX_ANN_PREFIX) for k in md.get("annotations") or {}):
            out.append(f"{md.get('namespace')}/{md.get('name')}")
    return sorted(out)


def target_checks(current, target, lvm=None, addons=(), ingresses=(), ingress=None, console_shim=False):
    """[{key, ok, level, forcible, text}] d'une mise à jour vers `target`
    (version lue : nom de la Version, release de l'ISO). Contrôles 1.9.1 :
    rien si la cible est illisible ou en dessous de v1.9.1. `lvm` :
    hv_advanced.lvm_volumes ; `ingress` : hv_settings.ingress_kind du
    cluster. `console_shim` (relecture I-3, toute cible) : le service
    `ingress-expose` de la console pour CAPHV v0.10.1 existe ; Harvester le
    supprime au redémarrage de son contrôleur quand Traefik est là
    (pkg/controller/master/rancher/register.go, v1.9.0 et v1.9.1), il se
    repose après par « Réparer »."""
    out = []
    if console_shim:
        out.append({"key": "console-ingress-expose-present", "ok": False, "level": "warn",
                    "forcible": False, "text": "kube-system/ingress-expose"})
    if not _at_least(target, TARGET_191):
        return out
    lvm = lvm or {}
    if (lvm.get("lvm") or []) and not _at_least(current, TARGET_191):
        out.append({"key": "lvm-pre-ga-volumes", "ok": False, "level": "block", "forcible": True,
                    "text": ", ".join(lvm["lvm"][:5]) + (" ..." if len(lvm["lvm"]) > 5 else "")})
    if lvm.get("striped"):
        out.append({"key": "lvm-striped-volumes", "ok": False, "level": "warn", "forcible": False,
                    "text": ", ".join(lvm["striped"][:5])})
    on = [f"{_meta(a).get('namespace')}/{_meta(a).get('name')}" for a in addons or []
          if _meta(a).get("name") == VCLUSTER_ADDON and (a.get("spec") or {}).get("enabled")]
    if on:
        out.append({"key": "rancher-vcluster-deprecated", "ok": False, "level": "warn", "forcible": False,
                    "text": ", ".join(on)})
    ngx = _nginx_ingresses(ingresses) if ingress in ("nginx", "both") else []
    if ngx:
        out.append({"key": "ingress-nginx-objects", "ok": False, "level": "warn", "forcible": False,
                    "text": ", ".join(ngx[:5]) + (" ..." if len(ngx) > 5 else "")})
    out.append({"key": "vm-migration-failure-policy", "ok": True, "level": "info", "forcible": False, "text": ""})
    return out


def policy_pending(upgrades):
    """Nom d'une Upgrade finie qui porte encore la valeur d'origine de
    `upgrade-config` (à rendre par follow ou dismiss), ou None."""
    for u in upgrades or []:
        if A_CONFIG_BEFORE in (_meta(u).get("annotations") or {}) \
                and _labels(u).get(L_STATE) in ("Succeeded", "Failed"):
            return _meta(u).get("name")
    return None


def migration_policy_value(setting, on):
    """(nouvelle valeur, valeur d'origine) du réglage `upgrade-config` avec
    `shutdownVMOnMigrationFailure` = `on` ; la valeur d'origine est rendue
    telle quelle après la mise à jour (vide = défaut de Harvester)."""
    orig = (setting or {}).get("value") or ""
    try:
        doc = json.loads(orig or (setting or {}).get("default") or "{}")
    except ValueError:
        raise ValueError("upgrade-config: not valid JSON, fix the setting first") from None
    if not isinstance(doc, dict):
        raise ValueError("upgrade-config: a JSON object is expected")
    doc[MIGRATION_POLICY_KEY] = bool(on)
    return json.dumps(doc, separators=(",", ":")), orig
