"""harvester-ops : import d'un cluster enfant dans un Rancher externe (2.8.0).

Décision 125, points 220 à 222 : le cluster enfant s'importe dans le Rancher
de la session de la personne (celui qui gère le cluster Harvester), par
l'import générique de Rancher : un `provisioning.cattle.io/v1` `Cluster`
vide créé par l'API de Rancher avec le jeton de la session, puis le
manifeste d'enregistrement lu chez ce Rancher et appliqué par la console
dans le cluster enfant. L'option `rancher_import` existante (étiquette
d'auto-import de Turtles) vise le Rancher intégré à Harvester : les deux
sont exclusives (point 239), deux agents se disputeraient `cattle-system`.

Ce que la tâche 1 a relevé (Rancher v2.14.1, 07/11/2026, voir « Essais
préalables (tâche 1) » du plan 2.8.0) : `status.clusterName` en quelques
secondes ; `/v3/clusterregistrationtokens?clusterId=` rend un
`manifestUrl` déjà substitué ; le manifeste ne porte qu'une image, celle de
l'agent ; au retrait, Rancher nettoie `cattle-system` et
`cattle-fleet-system` mais laisse des objets de portée cluster
(`LEFTOVERS`).

Ce module ne parle à aucun cluster et ne voit jamais le jeton
d'enregistrement : il valide des adresses et des objets. Bibliothèque
standard seulement ; lire le YAML du manifeste demande PyYAML (chargé à
l'appel), comme `capi_manifests.parse`.
"""

import fnmatch
import hashlib
import re
import urllib.parse

NAMESPACE = "fleet-default"
API_V1 = "/v1/provisioning.cattle.io.clusters"
SCHEMA = "/v1/schemas/provisioning.cattle.io.cluster"
MANAGED = "harvester-ops.io/managed"
L_HARVESTER = "harvester-ops.io/harvester"
L_CHILD_NS = "harvester-ops.io/child-namespace"
L_CHILD = "harvester-ops.io/child"
A_BY = "harvester-ops.io/applied-by"
A_RUN = "harvester-ops.io/plan-run"
# marque posée sur le Cluster Cluster API (cluster de gestion)
A_MARK = "harvester-ops.io/rancher-external"
NAME_MAX = 63
MANIFEST_MAX = 10 * 1024 * 1024
IMPORT_PATH_RE = re.compile(r"^/v3/import/[A-Za-z0-9_.-]{8,300}\.yaml$")
CID_RE = re.compile(r"^c-(m-)?[a-z0-9]{3,12}$")
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
WAIT_DEFAULT, WAIT_MIN, WAIT_MAX = 900, 60, 3600
UNIMPORT_WAIT = 600

# Restes laissés par Rancher dans le cluster enfant après son propre Job de
# nettoyage, relevés sur rke2-banc (Rancher v2.14.1, RKE2 v1.34.11, 07/11/2026,
# plan 2.8.0, « Essais préalables (tâche 1) », point 2). Motifs fnmatch sur
# le nom. Jamais les CRD `k3s.cattle.io` ni `helm.cattle.io` : elles sont à
# RKE2 lui-même.
LEFTOVER_NAMESPACES = ("local",)
LEFTOVER_CLUSTERROLES = ("cattle-*", "cluster-owner", "create-ns", "project-owner", "project-owner-promoted",
                         "proxy-clusterrole-kubeapiserver", "p-*-namespaces-edit", "p-*-namespaces-readonly")
LEFTOVER_CLUSTERROLEBINDINGS = ("cattle-*", "crb-*", "globaladmin-*", "proxy-role-binding-kubernetes-master")
LEFTOVER_CRD_GROUPS = ("management.cattle.io", "catalog.cattle.io", "ui.cattle.io", "upgrade.cattle.io",
                       "auditlog.cattle.io", "telemetry.cattle.io")
LEFTOVERS = {"Namespace": LEFTOVER_NAMESPACES, "ClusterRole": LEFTOVER_CLUSTERROLES,
             "ClusterRoleBinding": LEFTOVER_CLUSTERROLEBINDINGS}
# Revue 1-4 (I-1) : un nom ne suffit pas (`crb-monitoring`, `create-ns` d'un
# autre outil). Un reste n'est retiré que s'il porte une marque de Rancher :
# une étiquette ou une annotation d'un domaine `cattle.io` (`cattle.io/creator`,
# `authz.cluster.cattle.io/...`, `lifecycle.cattle.io/...`), ou une référence
# de propriétaire vers un objet d'un groupe `cattle.io`. Les objets du
# manifeste d'enregistrement (point 1 des essais : noms fixés par Rancher,
# appliqués par la console elle-même) n'en portent pas : ils sont reconnus à
# leur nom exact. Marques à relever sur le banc (tâche 10, R2) ; un objet
# sans marque est gardé (`kept-not-rancher`).
REGISTRATION_OBJECTS = {("ClusterRole", "proxy-clusterrole-kubeapiserver"), ("ClusterRole", "cattle-admin"),
                        ("ClusterRoleBinding", "proxy-role-binding-kubernetes-master"),
                        ("ClusterRoleBinding", "cattle-admin-binding")}


class RancherImportError(Exception):
    """Refus explicable ; `code` le nomme (rancher-token-unreadable,
    rancher-manifest-too-big, rancher-manifest-path...)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Nom et objet dans Rancher
# ---------------------------------------------------------------------------
def rancher_name(harvester, ns, cluster):
    """`<harvester>-<c>` si le namespace vaut le nom du cluster enfant, sinon
    `<harvester>-<ns>-<c>` ; au-delà de 63 caractères, 54 premiers plus `-`
    et 8 hexadécimaux de sha256 du nom complet (stable)."""
    # revue 1-4 (m-8) : un nom de cluster de la console hors RFC 1123
    # (soulignés, points) donnerait un 422 brut de Rancher
    if not isinstance(harvester, str) or not NAME_RE.match(harvester.lower()):
        raise RancherImportError("rancher-name-invalid", f"{str(harvester)[:40]}: the Harvester cluster name is not "
                                                         "a valid Rancher name (RFC 1123)")
    full = f"{harvester}-{cluster}" if ns == cluster else f"{harvester}-{ns}-{cluster}"
    full = full.lower()
    if len(full) <= NAME_MAX:
        return full
    return full[:54].rstrip("-") + "-" + hashlib.sha256(full.encode()).hexdigest()[:8]


def labels(harvester, ns, cluster):
    return {MANAGED: "true", L_HARVESTER: harvester, L_CHILD_NS: ns, L_CHILD: cluster}


def cluster_body(name, harvester, ns, cluster, by, run=None):
    """Corps de `POST /v1/provisioning.cattle.io.clusters` : cluster importé
    (aucune `rkeConfig`), étiquettes d'origine, auteur et exécution."""
    ann = {A_BY: by or ""}
    if run:
        ann[A_RUN] = run
    return {"type": "provisioning.cattle.io.cluster",
            "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels(harvester, ns, cluster),
                         "annotations": ann},
            "spec": {}}


def ours(prov, harvester, ns, cluster, expected_run=None):
    """Vrai si le cluster de provisionnement porte les étiquettes de CE
    cluster enfant (et l'exécution attendue, quand elle est donnée)."""
    md = (prov or {}).get("metadata") or {}
    lab = md.get("labels") or {}
    if any(lab.get(k) != v for k, v in labels(harvester, ns, cluster).items()):
        return False
    if expected_run and (md.get("annotations") or {}).get(A_RUN) != expected_run:
        return False
    return True


def cluster_id(prov):
    """`status.clusterName` (`c-m-...`), ou "" tant que Rancher ne l'a pas posé."""
    cid = ((prov or {}).get("status") or {}).get("clusterName") or ""
    return cid if CID_RE.match(cid) else ""


def ready(prov, mgmt):
    """Prêt : cluster de provisionnement `status.ready` et cluster de gestion
    `active` (lu par `/v3/clusters/<id>`)."""
    return bool(((prov or {}).get("status") or {}).get("ready")) and (mgmt or {}).get("state") == "active"


def can_create(schema):
    """Le schéma de la collection permet-il `POST` à cette personne ?"""
    return "POST" in ((schema or {}).get("collectionMethods") or [])


# ---------------------------------------------------------------------------
# Jeton et manifeste d'enregistrement
# ---------------------------------------------------------------------------
def registration_manifest_url(tokens):
    """`manifestUrl` d'une réponse de `/v3/clusterregistrationtokens` (liste
    ou réponse `{data: [...]}`) : l'API norman y a substitué le jeton (vu en
    2.14). Encore `{token}` (forme du CRD de 2.15) ou absent :
    RancherImportError `rancher-token-unreadable`."""
    items = tokens.get("data") if isinstance(tokens, dict) else tokens
    for t in items or []:
        url = (t or {}).get("manifestUrl") or ""
        if url and "{token}" not in url and "%7btoken%7d" not in url.lower():
            return url
    raise RancherImportError("rancher-token-unreadable",
                             "no registration manifest address with its token yet")


def manifest_path(url, session_url=""):
    """Chemin `/v3/import/<...>.yaml` de l'adresse : la console relit le
    manifeste par l'adresse de la session Rancher, jamais par l'hôte écrit
    dans `manifestUrl` (vu sur la maison : il se résout sur l'adresse
    publique). Revue 1-4 (m-4) : un Rancher servi sous un préfixe
    (`https://h/rancher`) voit ce préfixe retiré, la session l'ajoute."""
    path = urllib.parse.urlparse(url or "").path
    prefix = urllib.parse.urlparse(session_url or "").path.rstrip("/")
    if prefix and path.startswith(prefix + "/"):
        path = path[len(prefix):]
    if not IMPORT_PATH_RE.match(path):
        raise RancherImportError("rancher-manifest-path", "unexpected registration manifest address")
    return path


def check_size(n):
    if n > MANIFEST_MAX:
        raise RancherImportError("rancher-manifest-too-big", f"registration manifest over {MANIFEST_MAX} bytes")


def _docs(text):
    import yaml
    try:
        return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    except yaml.YAMLError as e:
        raise RancherImportError("rancher-manifest-unreadable", f"registration manifest: {str(e)[:120]}") from None


def manifest_images(text):
    """Images des Deployment, DaemonSet, StatefulSet et Job du manifeste,
    conteneurs d'initialisation compris, sans doublon, dans l'ordre."""
    out = []
    for d in _docs(text):
        if d.get("kind") not in ("Deployment", "DaemonSet", "StatefulSet", "Job"):
            continue
        spec = (((d.get("spec") or {}).get("template") or {}).get("spec")) or {}
        for c in (spec.get("initContainers") or []) + (spec.get("containers") or []):
            img = (c or {}).get("image")
            if isinstance(img, str) and img and img not in out:
                out.append(img)
    return out


def secret_values(text, path=""):
    """Chaînes à masquer dans tout message d'une application du manifeste :
    le jeton du chemin d'import (partie avant le dernier `_`), et les
    valeurs des Secrets du manifeste, en clair et en base64."""
    import base64
    out = []
    seg = path.rsplit("/", 1)[-1]
    if seg.endswith(".yaml"):
        seg = seg[:-len(".yaml")]
        out += [seg, seg.rsplit("_", 1)[0]]
    try:
        docs = _docs(text)
    except RancherImportError:
        docs = []
    for d in docs:
        if d.get("kind") != "Secret":
            continue
        for v in list((d.get("data") or {}).values()) + list((d.get("stringData") or {}).values()):
            v = str(v)
            out.append(v)
            try:
                out.append(base64.b64decode(v, validate=True).decode())
            except (ValueError, UnicodeDecodeError):
                pass
    return sorted({x for x in out if len(x) >= 8}, key=len, reverse=True)


def split_image(image):
    """(hôte, dépôt, étiquette) d'une référence d'image, docker.io et
    `library/` par défaut ; l'empreinte prend la place de l'étiquette."""
    ref = image
    tag = "latest"
    if "@" in ref:
        ref, tag = ref.split("@", 1)
    else:
        last = ref.rsplit("/", 1)[-1]
        if ":" in last:
            ref, tag = ref.rsplit(":", 1)
    first, sep, rest = ref.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        host, repo = first, rest
    else:
        host, repo = "docker.io", ref
    if host == "docker.io" and "/" not in repo:
        repo = "library/" + repo
    return host, repo, tag


def probe_targets(image, registries_doc):
    """Où le cluster enfant tirerait `image` d'après son `registries.yaml`
    (mirrors, endpoint, rewrite) : [(hôte du point d'accès, dépôt réécrit,
    étiquette)] dans l'ordre ; [] si l'origine n'a pas de miroir."""
    host, repo, tag = split_image(image)
    m = ((registries_doc or {}).get("mirrors") or {}).get(host)
    if not m:
        return []
    target = repo
    for rx, repl in ((m.get("rewrite") or {}).items()):
        try:
            if re.match(rx, repo):
                target = re.sub(rx, repl.replace("$", "\\"), repo, count=1)
                break
        except re.error:
            continue
    out = []
    for ep in m.get("endpoint") or []:
        h = urllib.parse.urlparse(ep).netloc or ep
        out.append((h, target, tag))
    return out


def image_findings(images, registries_doc, found, no_fallback):
    """Constats des images de l'agent : `found` = {image: bool | None}
    (présente dans un miroir, absente, non sondée). Absente et repli coupé :
    `rancher-agent-image-missing` bloquant ; absente avec repli :
    avertissement `rancher-agent-image-fallback`."""
    out = []
    for img in images:
        if not probe_targets(img, registries_doc):
            if no_fallback:
                # revue 1-4 (m-3) : hôte sans miroir et repli coupé, l'agent
                # ne se tire pas (registry.rancher.com rarement miroité)
                out.append({"code": "rancher-agent-image-unmirrored", "level": "warn",
                            "facts": {"image": img, "host": split_image(img)[0]}})
            continue
        if found.get(img):
            continue
        out.append({"code": "rancher-agent-image-missing" if no_fallback else "rancher-agent-image-fallback",
                    "level": "block" if no_fallback else "warn", "facts": {"image": img}})
    return out


# ---------------------------------------------------------------------------
# Marque sur le cluster de gestion
# ---------------------------------------------------------------------------
def server_id(url):
    """Identifiant d'un Rancher : son hôte (et port), en minuscules."""
    return (urllib.parse.urlparse(url or "").netloc or "").lower()


def mark(server, name, cid):
    return f"{server}/{name}/{cid}"


def parse_mark(text):
    """Inverse de `mark` : {server, name, cid}, ou None si la forme est fausse."""
    parts = str(text or "").split("/")
    if len(parts) != 3 or not parts[0] or not NAME_RE.match(parts[1]) or not CID_RE.match(parts[2]):
        return None
    return {"server": parts[0], "name": parts[1], "cid": parts[2]}


def mark_of(capi_cluster):
    ann = (((capi_cluster or {}).get("metadata") or {}).get("annotations") or {})
    return parse_mark(ann.get(A_MARK))


# ---------------------------------------------------------------------------
# Retrait
# ---------------------------------------------------------------------------
# 2.8.2 (décision 247, point 4 du banc 2.8.1) : Rancher 2.14 laisse dans
# `fleet-default` le Bundle de l'agent Fleet du cluster importé après son
# retrait (vu : `fleet-agent-harv4-r12a`, propriétaire le Namespace,
# empreinte d'objectset, encore là 45 min après). Nom et cible posés par
# Fleet (agentmanagement, manageagent : `fleet-agent-<cluster>`, une cible
# `clusterName: <cluster>`) ; au-delà de 63 caractères Fleet raccourcit le
# nom, qui n'est alors plus reconnu exactement.
BUNDLE_API = "/v1/fleet.cattle.io.bundles"
AGENT_BUNDLE_PREFIX = "fleet-agent-"


def agent_bundle_name(name):
    """`fleet-agent-<nom dans Rancher>`, ou "" s'il dépasse 63 caractères."""
    full = AGENT_BUNDLE_PREFIX + str(name or "")
    return full if name and len(full) <= NAME_MAX else ""


def agent_bundle_ours(bundle, name):
    """Vrai si le Bundle est l'agent de CE cluster seul : nom exact dans
    `fleet-default`, au moins une cible et chacune nomme ce cluster, une
    marque de Rancher ou de Fleet (domaine `cattle.io`), aucune étiquette
    de la console."""
    md = (bundle or {}).get("metadata") or {}
    if md.get("name") != agent_bundle_name(name) or md.get("namespace") != NAMESPACE or not md.get("name"):
        return False
    targets = ((bundle.get("spec") or {}).get("targets")) or []
    if not targets or any(not isinstance(t, dict) or t.get("clusterName") != name for t in targets):
        return False
    if any(k.startswith("harvester-ops.io/") for k in (md.get("labels") or {})):
        return False
    return rancher_marked(bundle)


def _crd_group(name):
    return name.split(".", 1)[1] if "." in name else ""


def _rancher_domain(key):
    domain = key.split("/", 1)[0] if "/" in key else ""
    return domain == "cattle.io" or domain.endswith(".cattle.io")


def rancher_marked(obj):
    """Vrai si l'objet porte une marque de Rancher (voir REGISTRATION_OBJECTS)."""
    md = (obj or {}).get("metadata") or {}
    if (obj.get("kind"), md.get("name")) in REGISTRATION_OBJECTS:
        return True
    keys = list((md.get("labels") or {})) + list((md.get("annotations") or {}))
    if any(_rancher_domain(k) for k in keys):
        return True
    return any(str(r.get("apiVersion") or "").split("/", 1)[0].endswith("cattle.io")
               for r in md.get("ownerReferences") or [])


def leftovers(objs, unmarked=None):
    """Restes connus de Rancher parmi des objets du cluster enfant :
    [(sorte, namespace, nom)], namespaces en dernier. Une CRD des groupes de
    Rancher (le groupe est la marque) ; un autre objet dont le nom suit un
    motif ET qui porte une marque de Rancher (`rancher_marked`). Les noms
    qui suivent un motif sans marque vont dans `unmarked` (liste fournie)."""
    out = []
    for o in objs or []:
        kind = o.get("kind") or ""
        name = ((o.get("metadata") or {}).get("name")) or ""
        if kind == "CustomResourceDefinition":
            if _crd_group(name) in LEFTOVER_CRD_GROUPS:
                out.append((kind, "", name))
            continue
        if any(fnmatch.fnmatchcase(name, p) for p in LEFTOVERS.get(kind, ())):
            if rancher_marked(o):
                out.append((kind, "", name))
            elif unmarked is not None:
                unmarked.append((kind, "", name))
    return sorted(out, key=lambda r: (r[0] == "Namespace", r[0], r[2]))
