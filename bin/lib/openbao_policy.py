"""Politiques OpenBao, commandes de l'assistant du mode A et configuration
du mode B1 (2.9.0, tâche 5). Module pur : du texte, aucune requête.

Noms (plan 2.9.0, « Authentification de la console », « Mode B1 ») pour un
identifiant de console `<id>` (12 hexadécimaux) :

    harvester-ops-<id>    politique et rôle AppRole de la console
    hops-agent-<id>       politique et rôle AppRole de l'agent de l'hôte (B1)
    hops-boot-<id>        politique du jeton d'amorce de l'agent (B1, 15 min)
    hops-tf-<id>          politique fixe et rôle de jeton des jetons enfants
                          d'OpenTofu (une seule, à gabarit d'identité)
    hops-<id>-tf-<décl>   clé transit d'une déclaration

Ce que les essais préalables (tâche 1) ont imposé :

* `custom_metadata` ne s'écrit que par le chemin `metadata` : la console y a
  `create, update, patch`, limités à son préfixe (point 272) ;
* une politique par déclaration demanderait un droit d'écriture sur
  `sys/policies/acl/`, qui équivaut à l'administration (escalade montrée,
  essai 8) : d'où la politique fixe `hops-tf-<id>` à gabarit
  `{{identity.entity.aliases.<accesseur de token/>.name}}`, l'alias étant
  posé à la création du jeton enfant (`entity_alias = "tf-<décl>"`) ;
* OpenTofu appelle `transit/datakey/plaintext/<clé>` et `transit/decrypt/
  <clé>`, jamais `transit/encrypt` ;
* `max_versions` juste après le montage KV répond « invalid request » (mise
  à niveau du moteur) : requête en DERNIER, `allow_failure = true`, relue et
  posée par l'agent si elle manque (essai 2) ;
* les valeurs d'amorce (`secret_id` de la console et de l'agent, identifiant
  du jeton d'amorce) viennent de fichiers (`eval_source = "file"`) : jamais
  de valeur dans la configuration (essai 3) ;
* `token_bound_cidrs` et `secret_id_bound_cidrs` = 127.0.0.1/32 ne valent
  qu'en `--network host` (essai 4), ce que l'unité de B1 fait.
"""

import json
import re

import openbao_jwt as oj

ID_RE = re.compile(r"^[0-9a-f]{12}$")
MOUNT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}(/[a-z0-9][a-z0-9_-]{0,62}){0,3}$")
DECL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
ACCESSOR_RE = re.compile(r"^(auth_token_[0-9a-f]{6,64}|\$ACCESSOR)$")
CIDR_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}/\d{1,2}$|^[0-9a-fA-F:]+/\d{1,3}$")
KEY_ID_RE = re.compile(r"^hops-[0-9]{8}-[0-9]{1,4}$")

DEFAULT_MOUNT = "harvester-ops"
TRANSIT = "transit"
MAX_VERSIONS = 10
TOKEN_TTL = "1h"
BOOT_TOKEN_TTL = "15m"
WRAP_TTL = "15m"

# Mode B1 (plan, « Mode B1 : OpenBao à côté de la console »)
B1_LISTEN = "127.0.0.1:8210"
B1_CLUSTER_LISTEN = "127.0.0.1:8211"
B1_ADDR = "https://127.0.0.1:8210"
B1_CLUSTER_ADDR = "https://127.0.0.1:8211"
B1_CIDR = "127.0.0.1/32"
B1_NODE_ID = "hops-1"
# Chemins dans le conteneur
C_CONFIG = "/openbao/config/openbao.hcl"
C_DATA = "/openbao/data"
C_AUDIT = "/openbao/audit/audit.log"
C_TLS_CERT = "/openbao/tls/server.pem"
C_TLS_KEY = "/openbao/tls/server.key"
# 2.10.0 (point 298) : second écouteur, pour ESO seulement, avec son propre
# certificat (la CA de l'écouteur local est contrainte à 127.0.0.1 et sa clé
# n'est pas gardée : une CA dédiée, contrainte à l'adresse de l'écouteur)
C_ESO_CERT = "/openbao/tls/eso.pem"
C_ESO_KEY = "/openbao/tls/eso.key"
ESO_PORT = 8212
C_UNSEAL = "/run/secrets/hops-openbao-unseal"
C_UNSEAL_PREV = "/run/secrets/hops-openbao-unseal-prev"
BOOT_DIR = "/run/hops-openbao-boot"          # même chemin sur l'hôte (tmpfs) et dans le conteneur
BOOT_FILES = ("console-secret-id", "agent-secret-id", "boot-token")
AUDIT_HEADERS = ("X-Hops-Action", "X-Hops-Actor")


def _check(rx, value, what):
    if not isinstance(value, str) or not rx.match(value):
        raise ValueError(f"invalid {what}")
    return value


def console_role(cid):
    return f"harvester-ops-{_check(ID_RE, cid, 'console id')}"


def agent_role(cid):
    return f"hops-agent-{_check(ID_RE, cid, 'console id')}"


def boot_policy_name(cid):
    return f"hops-boot-{_check(ID_RE, cid, 'console id')}"


def tf_role(cid):
    return f"hops-tf-{_check(ID_RE, cid, 'console id')}"


def tf_key(cid, decl):
    return f"hops-{_check(ID_RE, cid, 'console id')}-tf-{_check(DECL_RE, decl, 'declaration name')}"


def tf_alias(decl):
    """Alias d'entité du jeton enfant d'une déclaration : `tf-<décl>`, nom
    validé (DECL_RE) ; le rôle de jeton accepte `tf-*`, qui laisserait
    passer `/`, `..` ou `*` (revue M6 des tâches 5-7)."""
    return f"tf-{_check(DECL_RE, decl, 'declaration name')}"


def secret_id_path(cid):
    """`creation_path` attendu d'une enveloppe de `secret_id` (essai 4)."""
    return f"auth/approle/role/{console_role(cid)}/secret-id"


# ---------------------------------------------------------------------------
# Politiques
# ---------------------------------------------------------------------------
def _hcl_policy(rules):
    return "".join(f'path "{p}" {{ capabilities = [{", ".join(json.dumps(c) for c in caps)}] }}\n'
                   for p, caps in rules)


def console_rules(mount, prefix, cid, b1=False):
    """[(chemin, capacités)] de la politique de la console (texte du plan,
    « Politique de la console »). Revue M5 des tâches 5-7 : la ligne
    `transit/keys/hops-<id>-*/rotate` du plan est retirée, un `*` au milieu
    d'un chemin n'est pas un motif pour OpenBao ; la rotation passe par le
    motif final de `transit/keys/hops-<id>-*` (create, update), qui permet
    aussi `.../config` et `.../trim` de ces seules clés."""
    m = _check(MOUNT_RE, mount, "mount")
    p = _check(PREFIX_RE, prefix, "prefix")
    role = console_role(cid)
    rules = [
        (f"{m}/data/{p}/*", ["create", "read", "update", "delete", "list"]),
        (f"{m}/metadata/{p}/*", ["create", "read", "update", "patch", "list", "delete"]),
        (f"{m}/destroy/{p}/*", ["update"]),
        (f"{m}/config", ["read"]),
        # 2.9.0, décision 278 : `delete` pour retirer la clé d'une déclaration
        # supprimée au bout de 30 jours (`deletion_allowed` posé par `.../config`)
        (f"{TRANSIT}/keys/hops-{cid}-*", ["create", "update", "delete"]),
        (f"{TRANSIT}/encrypt/hops-{cid}-*", ["update"]),
        (f"{TRANSIT}/decrypt/hops-{cid}-*", ["update"]),
        (f"auth/token/create/{tf_role(cid)}", ["update"]),
        (f"auth/approle/role/{role}/secret-id", ["update"]),
        (f"auth/approle/role/{role}/secret-id-accessor/destroy", ["update"]),
        ("sys/capabilities-self", ["update"]),
    ]
    if b1:
        rules.append(("sys/storage/raft/snapshot", ["read"]))
    return rules


def console_policy(mount, prefix, cid, b1=False):
    return _hcl_policy(console_rules(mount, prefix, cid, b1))


def required_capabilities(mount, prefix, cid, b1=False):
    """{chemin concret: capacités} à vérifier par `sys/capabilities-self`
    (vérifications avant activation) : les règles de la politique, un `*`
    remplacé par un nom d'essai."""
    out = {}
    for path, caps in console_rules(mount, prefix, cid, b1):
        out[path.replace("*", "check-x")] = caps
    return out


def tf_child_policy(cid, token_accessor):
    """La politique fixe des jetons enfants d'OpenTofu (essai 8) : une clé
    transit par déclaration, désignée par l'alias d'entité du jeton."""
    acc = _check(ACCESSOR_RE, token_accessor, "token auth accessor")
    tmpl = f"hops-{_check(ID_RE, cid, 'console id')}-{{{{identity.entity.aliases.{acc}.name}}}}"
    return _hcl_policy([(f"{TRANSIT}/datakey/plaintext/{tmpl}", ["update"]),
                        (f"{TRANSIT}/decrypt/{tmpl}", ["update"])])


# ---------------------------------------------------------------------------
# 2.10.0 : jeton OpenBao par personne, groupes, ESO
# ---------------------------------------------------------------------------
# Classes que la politique commune ouvre (plan 2.10.0, « Politiques ») :
# lecture des trois classes personnelles et de leurs versions, écriture de
# `tf-kvm` (variable d'une déclaration), `ssh-cluster` (clé téléversée) et,
# décision 309 (revue I3 des tâches 2 à 5), `ssh-kvm` (déclaration d'un hôte
# KVM) : OpenBao juge. Écriture sans lecture de `eso` (métadonnées comprises,
# décision 312). `custom_metadata`
# (`hops_actor`, `hops_identity`, décision 274) ne s'écrit que par le chemin
# `metadata` : une classe qu'une personne écrit y reçoit `create, update`,
# sinon la valeur s'écrit et son attribution échoue.
PERSON_READ = ("ssh-cluster", "ssh-kvm", "tf-kvm")
PERSON_WRITE = ("ssh-cluster", "ssh-kvm", "tf-kvm")
PERSON_WRITE_ONLY = ("eso",)            # écrite par une personne, jamais relue par la console
GROUP_CLASSES = ("kubeconfig", "ssh-cluster", "ssh-kvm", "tf-kvm", "eso")
GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
TARGET_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")      # cluster, hôte KVM ou clé ESO : aucun motif
ESO_KEY_RE = re.compile(r"^[0-9a-f]{8}$")
ENTITY_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
JWT_ACCESSOR_RE = re.compile(r"^(auth_jwt_[0-9a-f]{6,64}|\$ACCESSOR)$")
# revue 6-7 M-8 : bornes de ce que la console peut faire poser dans OpenBao
# (disponibilité : une console ne bourre pas l'instance de montages, de
# groupes ou de membres) ; relues par l'agent, l'outil et les routes
MAX_PERSON_MOUNTS = 16                   # méthodes jwt des Rancher
MAX_GROUPS = 32                          # groupes internes hops-<id>-*
MAX_GROUP_MEMBERS = 200                  # entités d'un groupe
MAX_ESO_CLUSTERS = 64                    # clusters enfants avec External Secrets


def person_policy_name(cid):
    return oj.person_policy_name(_check(ID_RE, cid, "console id"))


def group_name(cid, name):
    """Groupe d'identité interne d'OpenBao `hops-<id>-<nom>`."""
    return f"hops-{_check(ID_RE, cid, 'console id')}-{_check(GROUP_RE, name, 'group name')}"


def group_policy_name(cid, name):
    return f"hops-group-{_check(ID_RE, cid, 'console id')}-{_check(GROUP_RE, name, 'group name')}"


def eso_policy_name(cid, key):
    return f"hops-eso-{_check(ID_RE, cid, 'console id')}-{_check(ESO_KEY_RE, key, 'ESO cluster key')}"


def person_rules(mount, prefix, cid, groups_only=False):
    """Politique commune `hops-person-<id>` des personnes connectées par
    Rancher (texte de référence du plan 2.10.0, complété par le concern 1).
    `groups_only` (réglage « lecture seulement par groupe ») : seulement
    `capabilities-self`, tout le reste vient des groupes."""
    m = _check(MOUNT_RE, mount, "mount")
    p = _check(PREFIX_RE, prefix, "prefix")
    person_policy_name(cid)
    rules = []
    if not groups_only:
        for cls in PERSON_READ:
            rules.append((f"{m}/data/{p}/{cls}/*", _data_caps(cls)))
        for cls in PERSON_WRITE_ONLY:
            rules.append((f"{m}/data/{p}/{cls}/*", _data_caps(cls)))
        for cls in PERSON_READ + PERSON_WRITE_ONLY:
            rules.append((f"{m}/metadata/{p}/{cls}/*", _meta_caps(cls)))
    rules.append(("sys/capabilities-self", ["update"]))
    return rules


def _data_caps(cls):
    if cls in PERSON_WRITE_ONLY:
        return ["create", "update"]
    return ["read", "create", "update"] if cls in PERSON_WRITE else ["read"]


def _meta_caps(cls):
    # décision 312 : une valeur ESO écrite par une personne garde son auteur
    # (`custom_metadata`), sans lecture des métadonnées
    if cls in PERSON_WRITE_ONLY:
        return ["create", "update"]
    return ["read", "create", "update"] if cls in PERSON_WRITE else ["read"]


def person_policy(mount, prefix, cid, groups_only=False):
    return _hcl_policy(person_rules(mount, prefix, cid, groups_only))


def _group_spec(name, classes, clusters):
    classes = list(classes or [])
    clusters = list(clusters or [])
    _check(GROUP_RE, name, "group name")
    if not classes or any(c not in GROUP_CLASSES for c in classes) or len(set(classes)) != len(classes):
        raise ValueError("group classes: " + ", ".join(GROUP_CLASSES))
    if not clusters or len(set(clusters)) != len(clusters) or len(clusters) > 200:
        raise ValueError("group clusters: a non-empty list of distinct names")
    for c in clusters:
        _check(TARGET_RE, c, "cluster name")
    if "eso" in classes and not all(ESO_KEY_RE.match(c) for c in clusters):
        raise ValueError("the eso class takes ESO cluster keys (8 hexadecimals)")
    return classes, clusters


def group_rules(mount, prefix, cid, name, classes, clusters):
    """Politique `hops-group-<id>-<nom>` d'un groupe interne : les mêmes
    droits que la politique commune, limités à `<classe>/<cluster>` pour
    chaque cluster (ou hôte KVM, ou clé ESO) de la liste ; `kubeconfig` en
    lecture (sans effet en 2.10.0, classe de service)."""
    m = _check(MOUNT_RE, mount, "mount")
    p = _check(PREFIX_RE, prefix, "prefix")
    group_policy_name(cid, name)
    classes, clusters = _group_spec(name, classes, clusters)
    rules = []
    for cls in classes:
        for c in clusters:
            # tf-kvm/<hôte>/<décl>/<variable> : tout l'hôte ; eso/<clé>/<ns>/<nom> : tout le cluster
            leaf = f"{cls}/{c}/*" if cls in ("tf-kvm", "eso") else f"{cls}/{c}"
            rules.append((f"{m}/data/{p}/{leaf}", _data_caps(cls)))
            rules.append((f"{m}/metadata/{p}/{leaf}", _meta_caps(cls)))
    return rules


def group_policy(mount, prefix, cid, name, classes, clusters):
    return _hcl_policy(group_rules(mount, prefix, cid, name, classes, clusters))


def group_body(cid, name, members):
    """Corps de `identity/group/name/hops-<id>-<nom>` : groupe interne, sa
    seule politique, ses membres (entités tenues par l'administrateur
    d'OpenBao ; en B1, par l'agent à la demande de la console)."""
    members = list(members or [])
    if len(set(members)) != len(members) or not all(isinstance(e, str) and ENTITY_RE.match(e) for e in members):
        raise ValueError("group members: distinct entity ids (UUID)")
    if len(members) > MAX_GROUP_MEMBERS:
        raise ValueError(f"group members: at most {MAX_GROUP_MEMBERS}")
    return {"type": "internal", "policies": [group_policy_name(cid, name)], "member_entity_ids": members}


def eso_rules(mount, prefix, cid, key, accessor):
    """Politique `hops-eso-<id>-<clé>` d'un cluster enfant (une seule, à
    gabarit : essai 5) : un namespace ne lit que `eso/<clé>/<namespace>/*`,
    le namespace venant des métadonnées de l'alias (`claim_mappings`)."""
    m = _check(MOUNT_RE, mount, "mount")
    p = _check(PREFIX_RE, prefix, "prefix")
    eso_policy_name(cid, key)
    acc = _check(JWT_ACCESSOR_RE, accessor, "jwt auth accessor")
    leaf = f"eso/{key}/{{{{identity.entity.aliases.{acc}.metadata.namespace}}}}/*"
    return [(f"{m}/data/{p}/{leaf}", ["read"]), (f"{m}/metadata/{p}/{leaf}", ["read", "list"])]


def eso_policy(mount, prefix, cid, key, accessor):
    return _hcl_policy(eso_rules(mount, prefix, cid, key, accessor))


def person_capability_checks(mount, prefix, cid, groups_only=False):
    """Vérification 4 de `person-token check` : {chemin concret: attendu},
    attendu = "read" (lecture permise), "write-only" (écriture sans
    lecture) ou "deny". `check-x` ne désigne aucun cluster réel : une
    politique de groupe (par cluster) ne l'ouvre pas."""
    m = _check(MOUNT_RE, mount, "mount")
    p = _check(PREFIX_RE, prefix, "prefix")
    _check(ID_RE, cid, "console id")
    head, _, last = p.rpartition("/")
    other = (head + "/" if head else "") + ("000000000000" if last != "000000000000" else "ffffffffffff")
    out = {}
    for cls in PERSON_READ:
        out[f"{m}/data/{p}/{cls}/check-x"] = "deny" if groups_only else "read"
    out[f"{m}/data/{p}/eso/check-x/ns/check-x"] = "deny" if groups_only else "write-only"
    for cls in ("kubeconfig", "rancher-oidc", "plan-run"):
        out[f"{m}/data/{p}/{cls}/check-x"] = "deny"
    out[f"{m}/data/{other}/ssh-kvm/check-x"] = "deny"
    return out


def capability_verdict(expect, caps):
    caps = set(caps or [])
    if expect == "read":
        return "read" in caps
    if expect == "write-only":
        return bool(caps & {"create", "update"}) and "read" not in caps
    return not (caps - {"deny"})


def agent_rules(cid, mount=DEFAULT_MOUNT):
    """Politique de l'agent de l'hôte en B1 (point 271) : rotation de la clé
    racine après un changement de clé de descellement (essai 7), retrait des
    entités `tf-<décl>`, réglage du montage KV, lecture des instantanés, et
    le `secret_id` de la console reposé après une restauration (les
    `secret_id` d'un instantané sont ceux de sa date).

    Décision 276 (revue I5 des tâches 5-7) : l'agent garde le droit de
    corriger les politiques `hops-*` de cette console (la sienne et celle
    des jetons d'OpenTofu ; jamais celle de la console), seul moyen de
    mettre une politique à niveau après la pose (plus de jeton racine). Les
    entités créées par `entity_alias` s'appellent `entity_<id>` (essai P4) :
    `identity/entity/name/tf-*` ne désignait rien ; elles se retrouvent par
    leur alias (`identity/lookup/entity`, accesseur de `token/` lu dans
    `sys/auth`) et se retirent par identifiant. Une instance B1 ne sert
    qu'une console : `identity/entity/id/*` ne touche que les siennes."""
    m = _check(MOUNT_RE, mount, "mount")
    return [
        ("sys/rotate/root", ["update", "sudo"]),
        ("sys/auth", ["read"]),
        ("identity/lookup/entity", ["update"]),
        ("identity/entity/id/*", ["read", "delete"]),
        (f"sys/policies/acl/{agent_role(cid)}", ["read", "create", "update"]),
        (f"sys/policies/acl/{tf_role(cid)}", ["read", "create", "update"]),
        (f"{m}/config", ["read", "update"]),
        ("sys/storage/raft/snapshot", ["read"]),
        (f"auth/approle/role/{console_role(cid)}/custom-secret-id", ["update"]),
        # accesseur du secret_id de la console relu après une restauration
        (f"auth/approle/role/{console_role(cid)}/secret-id/lookup", ["update"]),
        # 2.9.0 tâche 11 (revue M2 de la seconde passe) : après une
        # restauration, l'agent passe en revue les jetons de l'instantané
        # (politiques seulement, jamais leur valeur) ; un instantané fabriqué
        # avec la clé de descellement exportée ne glisse pas un jeton racine
        ("auth/token/accessors", ["list", "sudo"]),
        ("auth/token/lookup-accessor", ["update"]),
        # relecture finale I3 : revue par liste permise de toute la
        # configuration restaurée (noms et textes des politiques, méthodes
        # d'authentification, rôles AppRole et leurs secret_id, rôles de jeton,
        # groupes d'identité, montages, audit), puis révocation de tous les
        # jetons de l'instantané ; lectures seulement, sauf la révocation
        ("sys/policies/acl", ["list"]),
        ("sys/policies/acl/*", ["read"]),
        ("auth/approle/role", ["list"]),
        ("auth/approle/role/*", ["read"]),
        (f"auth/approle/role/{console_role(cid)}/secret-id", ["list"]),
        (f"auth/approle/role/{agent_role(cid)}/secret-id", ["list"]),
        (f"auth/approle/role/{agent_role(cid)}/secret-id/lookup", ["update"]),
        ("auth/token/roles", ["list"]),
        ("auth/token/roles/*", ["read"]),
        # revue 8-10 I-5 : rôle des jetons enfants d'OpenTofu lié à
        # 127.0.0.1/32, reposé par b1-policies sur une B1 d'avant la 2.10.0
        (f"auth/token/roles/{tf_role(cid)}", ["read", "update"]),
        ("identity/group/id", ["list"]),
        # relecture des corrections finales IMP-1 : les entités d'identité
        # (leurs politiques s'appliquent à tout jeton émis après la
        # révocation) et leurs alias, lus par `identity/entity/id/*` ; m4 :
        # aucun espace de noms (ses jetons échapperaient à la révocation)
        ("identity/entity/id", ["list"]),
        ("sys/namespaces", ["list"]),
        ("sys/mounts", ["read"]),
        ("sys/audit", ["read", "sudo"]),
        ("auth/token/revoke-accessor", ["update"]),
        # 2.10.0 (plan, « Mode A et mode B1 », essai 6) : méthodes `jwt` des
        # personnes et des clusters enfants de CETTE console, leurs rôles
        # (`list` pour relire les rôles d'un montage), la politique commune
        # des personnes, celles des groupes et d'ESO, les groupes d'identité
        # créés par leur nom (`POST identity/group` avec le nom dans le corps
        # n'est pas couvert). Les chemins par identifiant ne se restreignent
        # pas par nom : l'agent n'y vise que les groupes `hops-<id>-*` qu'il
        # vient de lire par nom. Il peut poser n'importe quelle politique sur
        # ces groupes (`allowed_parameters` ne l'empêche pas, une chaîne à
        # virgules contourne le motif) : décision 306 A, la revue d'un
        # instantané et la liste fermée des opérations sont les gardes. Aucun
        # droit sur `identity/group-alias` : groupes internes seulement
        # (décision 303 A).
        (f"sys/auth/{oj.MOUNT_PREFIX}{cid}-*", ["create", "update", "read", "delete", "sudo"]),
        (f"auth/{oj.MOUNT_PREFIX}{cid}-*", ["create", "update", "read", "delete", "list"]),
        (f"sys/policies/acl/{person_policy_name(cid)}", ["create", "update", "read", "delete"]),
        (f"sys/policies/acl/hops-group-{cid}-*", ["create", "update", "read", "delete"]),
        (f"sys/policies/acl/hops-eso-{cid}-*", ["create", "update", "read", "delete"]),
        (f"identity/group/name/hops-{cid}-*", ["create", "update", "read", "delete"]),
        ("identity/group/id/*", ["update", "read", "delete"]),
    ]


def agent_policy(cid, mount=DEFAULT_MOUNT):
    return _hcl_policy(agent_rules(cid, mount))


def boot_rules(cid, mount=DEFAULT_MOUNT):
    """Jeton d'amorce de l'agent (essai 3 (b)), 15 min, révoqué après usage :
    `max_versions` s'il manque, accesseur de `token/` et politique des jetons
    enfants (son texte dépend de cet accesseur), `role_id` des deux rôles,
    accesseur du premier `secret_id` de la console, restauration d'un
    instantané dans une instance neuve (essai 6), retrait de sa propre
    politique."""
    m = _check(MOUNT_RE, mount, "mount")
    return [
        (f"{m}/config", ["read", "update"]),
        ("sys/auth", ["read"]),
        (f"sys/policies/acl/{tf_role(cid)}", ["create", "update"]),
        (f"sys/policies/acl/{boot_policy_name(cid)}", ["delete"]),
        (f"auth/approle/role/{console_role(cid)}/role-id", ["read"]),
        (f"auth/approle/role/{agent_role(cid)}/role-id", ["read"]),
        (f"auth/approle/role/{console_role(cid)}/secret-id/lookup", ["update"]),
        ("sys/storage/raft/snapshot", ["read", "update", "sudo"]),
    ]


def boot_policy(cid, mount=DEFAULT_MOUNT):
    return _hcl_policy(boot_rules(cid, mount))


def console_role_params(cidr):
    """Rôle AppRole de la console (plan, « Authentification de la
    console ») ; `cidr` = adresse de la console."""
    _check(CIDR_RE, cidr, "console CIDR")
    return {"token_ttl": TOKEN_TTL, "token_max_ttl": 0, "token_period": TOKEN_TTL,
            "secret_id_ttl": 0, "secret_id_num_uses": 0,
            "secret_id_bound_cidrs": [cidr], "token_bound_cidrs": [cidr]}


def agent_role_params():
    return {"token_ttl": "15m", "token_max_ttl": "1h", "secret_id_ttl": 0, "secret_id_num_uses": 0,
            "secret_id_bound_cidrs": [B1_CIDR], "token_bound_cidrs": [B1_CIDR]}


def tf_token_role_params(cid, bound_cidrs=None):
    """Rôle de jeton des jetons enfants d'OpenTofu. Revue 8-10 I-5 : en B1,
    liés à 127.0.0.1/32 comme les rôles AppRole (l'écouteur ESO expose l'API
    sur le réseau ; un jeton enfant passe par l'environnement d'OpenTofu et
    de ses fournisseurs)."""
    out = {"allowed_policies": [tf_role(cid)], "allowed_entity_aliases": ["tf-*"],
           "orphan": False, "renewable": False}
    if bound_cidrs:
        out["token_bound_cidrs"] = list(bound_cidrs)
    return out


# ---------------------------------------------------------------------------
# Mode A : commandes `bao` à exécuter chez le client
# ---------------------------------------------------------------------------
def _kv_args(params):
    out = []
    for k, v in params.items():
        if isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, list):
            v = ",".join(v)
        out.append(f"{k}={v}" if re.match(r"^[A-Za-z0-9_./:*,-]*$", str(v)) else f'{k}="{v}"')
    return " ".join(out)


def mode_a_commands(addr, mount, prefix, cid, console_cidr):
    """Commandes `bao` (CLI d'OpenBao, compatible `vault`) qui préparent le
    serveur du client pour cette console. Aucune valeur secrète : la
    dernière rend un `secret_id` EMBALLÉ (15 min), que l'administrateur
    colle dans la console ; le `role_id` n'est pas secret."""
    if not isinstance(addr, str) or not re.match(r"^https?://[A-Za-z0-9.:\[\]-]+(/)?$", addr):
        raise ValueError("invalid OpenBao address")
    m = _check(MOUNT_RE, mount, "mount")
    _check(PREFIX_RE, prefix, "prefix")
    role, tf = console_role(cid), tf_role(cid)
    return [
        f"export BAO_ADDR={addr.rstrip('/')}",
        "# Montage KV v2 (sautez si le montage existe déjà en version 2)",
        f"bao secrets enable -path={m} -version=2 kv",
        f"bao write {m}/config max_versions={MAX_VERSIONS}",
        "# Transit (états OpenTofu chiffrés) et AppRole (sautez s'ils existent)",
        f"bao secrets enable -path={TRANSIT} transit",
        "bao auth enable approle",
        f"bao policy write {role} - <<'EOF'\n{console_policy(m, prefix, cid)}EOF",
        "ACCESSOR=$(bao read -field=accessor sys/auth/token)",
        f"bao policy write {tf} - <<EOF\n{tf_child_policy(cid, '$ACCESSOR')}EOF",
        f"bao write auth/approle/role/{role} token_policies={role} {_kv_args(console_role_params(console_cidr))}",
        f"bao write auth/token/roles/{tf} {_kv_args(tf_token_role_params(cid))}",
        *[f"bao write sys/config/auditing/request-headers/{h} hmac=false" for h in AUDIT_HEADERS],
        f"bao read -field=role_id auth/approle/role/{role}/role-id",
        f"bao write -wrap-ttl={WRAP_TTL} -f auth/approle/role/{role}/secret-id",
    ]


def _heredoc_json(cmd, body):
    return f"{cmd} - <<'EOF'\n{json.dumps(body, indent=2, sort_keys=True)}\nEOF"


def _addr_line(addr):
    if not isinstance(addr, str) or not re.match(r"^https?://[A-Za-z0-9.:\[\]-]+(/)?$", addr):
        raise ValueError("invalid OpenBao address")
    return f"export BAO_ADDR={addr.rstrip('/')}"


def person_token_commands(addr, cid, rancher, client_id, issuer, ca_pem, console_cidr, keys,
                          mount=DEFAULT_MOUNT, prefix=None, groups_only=False, jwks_sha256=None):
    """Mode A (décision 287) : commandes `bao` qui posent la méthode `jwt`
    d'un Rancher pour cette console (montage, politique commune,
    configuration, rôle `person`). `keys` : None (découverte par l'émetteur,
    avec `ca_pem` si la console en a une) ou la liste des clés publiques PEM
    (clés statiques, point 286). Aucune valeur secrète : CA et clés
    publiques seulement ; corps JSON lus sur l'entrée standard (`-`)."""
    pm = oj.person_mount(_check(ID_RE, cid, "console id"), rancher)
    prefix = prefix or f"consoles/{cid}"
    config = oj.mount_config(issuer, ca_pem=None if keys else ca_pem, pubkeys=keys or None)
    role = oj.person_role_params(client_id, console_cidr, cid)
    return [
        _addr_line(addr),
        f"# Méthode jwt du Rancher {rancher} (sautez la ligne suivante si le montage {pm}/ existe déjà)",
        f"bao auth enable -path={pm} jwt",
        f"bao policy write {person_policy_name(cid)} - <<'EOF'\n{person_policy(mount, prefix, cid, groups_only)}EOF",
        *(jwks_check_lines(issuer, jwks_sha256) if keys else []),
        _heredoc_json(f"bao write auth/{pm}/config", config),
        _heredoc_json(f"bao write auth/{pm}/role/{oj.PERSON_ROLE}", role),
        f"bao list auth/{pm}/role",
    ]


def person_policy_commands(addr, mount, prefix, cid, groups_only=False):
    """Mode A : la politique commune seule (réglage « lecture seulement par
    groupe » changé)."""
    return [_addr_line(addr), f"bao policy write {person_policy_name(cid)} - <<'EOF'\n"
                              f"{person_policy(mount, prefix, cid, groups_only)}EOF"]


def jwks_check_lines(issuer, jwks_sha256):
    """Revue 6-7 M-5 : en mode A, l'administrateur d'OpenBao reconnaît les
    clés qu'il colle (la confiance dans les clés lui appartient) : empreinte
    du JWKS publié et commande qui la recalcule depuis Rancher lui-même."""
    if not jwks_sha256:
        return []
    if not re.match(r"^[0-9a-f]{64}$", str(jwks_sha256)):
        raise ValueError("invalid JWKS fingerprint")
    url = oj.jwks_url(oj.mount_config(issuer)["bound_issuer"])
    return [f"# Clés tirées du JWKS publié par Rancher, empreinte {jwks_sha256} (kid, kty, n, e triés) ;",
            f"# vérifiez-la avant de coller : curl -s {url} (comparer kid et n aux clés ci-dessous)"]


def person_keys_commands(addr, cid, rancher, issuer, keys, jwks_sha256=None):
    """Mode A, tâche 7 : clés statiques remplacées par celles que Rancher
    publie (pendant une rotation, son JWKS porte l'ancienne et la nouvelle ;
    revue 6-7 I-1 : jamais une clé lue dans les réglages de la console)."""
    pm = oj.person_mount(_check(ID_RE, cid, "console id"), rancher)
    if not keys:
        raise ValueError("static keys expected")
    return [_addr_line(addr), *jwks_check_lines(issuer, jwks_sha256),
            _heredoc_json(f"bao write auth/{pm}/config", oj.mount_config(issuer, pubkeys=keys))]


def person_remove_commands(addr, cid, rancher, last=False):
    """Mode A : montage d'un Rancher retiré ; la politique commune avec le
    dernier. Les entités restent (point 289)."""
    pm = oj.person_mount(_check(ID_RE, cid, "console id"), rancher)
    out = [_addr_line(addr), f"bao auth disable {pm}"]
    if last:
        out.append(f"bao policy delete {person_policy_name(cid)}")
    return out


def group_commands(addr, mount, prefix, cid, name, classes, clusters, members):
    """Mode A : politique d'un groupe interne et le groupe lui-même (créé
    ou mis à jour par son nom). Groupes externes reportés (décision 303 A)."""
    return [
        _addr_line(addr),
        f"bao policy write {group_policy_name(cid, name)} - <<'EOF'\n"
        f"{group_policy(mount, prefix, cid, name, classes, clusters)}EOF",
        _heredoc_json(f"bao write identity/group/name/{group_name(cid, name)}", group_body(cid, name, members)),
    ]


def eso_cluster_commands(addr, mount, prefix, cid, key, issuer, pubkeys, namespaces, node_cidr=None, sa_uids=None):
    """Mode A (tâche 9) : commandes `bao` d'un cluster enfant pour ESO :
    montage `jwt` (s'il manque), configuration (clés publiques des comptes
    de service, lues par la console), rôle `eso` (namespaces activés,
    décision 304 B), politique à gabarit. L'accesseur du montage est relu
    par `bao read -field=accessor sys/auth/<montage>` (gabarit de la
    politique) : le corps de la politique est un document non cité, seul
    `$ACCESSOR` y est développé."""
    jm = oj.eso_mount(_check(ID_RE, cid, "console id"), key)
    prefix = prefix or f"consoles/{cid}"
    config = oj.eso_mount_config(issuer, pubkeys)
    role = oj.eso_role_params(cid, key, namespaces, node_cidr, sa_uids)
    policy = eso_policy(mount, prefix, cid, key, "$ACCESSOR")
    return [
        _addr_line(addr),
        f"# External Secrets du cluster enfant de clé {key} (sautez la ligne suivante si {jm}/ existe déjà)",
        f"bao auth enable -path={jm} jwt",
        f"ACCESSOR=$(bao read -field=accessor sys/auth/{jm})",
        f"bao policy write {eso_policy_name(cid, key)} - <<EOF\n{policy}EOF",
        _heredoc_json(f"bao write auth/{jm}/config", config),
        _heredoc_json(f"bao write auth/{jm}/role/{oj.ESO_ROLE}", role),
        f"bao list auth/{jm}/role",
    ]


def eso_role_commands(addr, cid, key, namespaces, node_cidr=None, sa_uids=None):
    """Mode A : le rôle seul (namespace activé ou retiré, décision 304 B)."""
    jm = oj.eso_mount(_check(ID_RE, cid, "console id"), key)
    return [_addr_line(addr), _heredoc_json(f"bao write auth/{jm}/role/{oj.ESO_ROLE}",
                                            oj.eso_role_params(cid, key, namespaces, node_cidr, sa_uids))]


def eso_keys_commands(addr, cid, key, issuer, pubkeys):
    jm = oj.eso_mount(_check(ID_RE, cid, "console id"), key)
    return [_addr_line(addr), _heredoc_json(f"bao write auth/{jm}/config", oj.eso_mount_config(issuer, pubkeys))]


def eso_remove_commands(addr, cid, key):
    jm = oj.eso_mount(_check(ID_RE, cid, "console id"), key)
    return [_addr_line(addr), f"bao auth disable {jm}", f"bao policy delete {eso_policy_name(cid, key)}"]


ESO_LISTEN_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})$")


def check_eso_listen(address):
    """`<IPv4>:<port>` d'un second écouteur : adresse unicast de l'hôte (ni
    la boucle locale, déjà servie, ni 0.0.0.0 qui ouvrirait les rôles de la
    console sur toutes les interfaces), port non privilégié distinct de
    l'écouteur local. Rend (adresse, port) ; ValueError sinon."""
    import ipaddress
    m = ESO_LISTEN_RE.match(str(address or ""))
    if not m:
        raise ValueError("listener: <IPv4 address of the host>:<port>")
    ip, port = ipaddress.IPv4Address(m.group(1)), int(m.group(2))
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_link_local or str(ip).endswith(".255"):
        raise ValueError("listener: a unicast address of the host (not loopback, not 0.0.0.0)")
    if not 1024 <= port <= 65535 or port in (8210, 8211):
        raise ValueError("listener: a port from 1024 to 65535, not 8210 or 8211")
    return str(ip), port


def eso_listener_block(address):
    """Strophe du second écouteur (point 298) : TLS par le certificat de
    l'écouteur ESO, sans adresse de cluster (raft reste sur 127.0.0.1)."""
    ip, port = check_eso_listen(address)
    # revue 8-10 I-5 : cet écouteur expose toute l'API sur le réseau des nœuds
    # enfants. Les rôles AppRole de la console et de l'agent, le rôle des
    # personnes et les jetons enfants d'OpenTofu sont liés à 127.0.0.1/32 :
    # seuls les rôles `eso` y aboutissent. Les points non authentifiés de
    # rekey sont fermés (option acceptée par OpenBao 2.7.2, qui y répond
    # déjà 405 ; une version qui ne la connaît pas l'ignore). Pare-feu de
    # l'hôte : n'ouvrir le port qu'aux réseaux des nœuds enfants.
    return "\n".join(['# Écouteur ESO (2.10.0) : API joignable du réseau des nœuds enfants ; les rôles de la',
                      '# console, des personnes et les jetons d\'OpenTofu sont liés à 127.0.0.1/32',
                      'listener "tcp" {', f'  address       = "{ip}:{port}"',
                      f'  tls_cert_file = "{C_ESO_CERT}"', f'  tls_key_file  = "{C_ESO_KEY}"',
                      "  disable_unauthed_rekey_endpoints = true", "}"])


def group_remove_commands(addr, cid, name):
    return [_addr_line(addr), f"bao delete identity/group/name/{group_name(cid, name)}",
            f"bao policy delete {group_policy_name(cid, name)}"]


# ---------------------------------------------------------------------------
# Mode B1 : configuration du serveur
# ---------------------------------------------------------------------------
def _hcl(v, indent=0):
    pad = "  " * indent
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(_hcl(x, indent) for x in v) + "]"
    if isinstance(v, dict):
        inner = "".join(f"{pad}  {json.dumps(str(k))} = {_hcl(x, indent + 1)}\n" for k, x in v.items())
        return "{\n" + inner + pad + "}"
    raise TypeError(f"no HCL form for {type(v).__name__}")


def _file_source(name):
    return {"eval_source": "file", "eval_type": "string", "path": f"{BOOT_DIR}/{name}"}


def initialize_requests(cid, mount=DEFAULT_MOUNT):
    """[(nom, opération, chemin, données, allow_failure)] de la strophe
    `initialize` de B1, dans l'ordre (essais 2 et 3)."""
    m = _check(MOUNT_RE, mount, "mount")
    prefix = f"consoles/{_check(ID_RE, cid, 'console id')}"
    role, agent, boot, tf = console_role(cid), agent_role(cid), boot_policy_name(cid), tf_role(cid)
    reqs = [
        ("mount-kv", "update", f"sys/mounts/{m}", {"type": "kv", "options": {"version": "2"}}, False),
        ("mount-transit", "update", f"sys/mounts/{TRANSIT}", {"type": "transit"}, False),
        ("auth-approle", "update", "sys/auth/approle", {"type": "approle"}, False),
        ("policy-console", "update", f"sys/policies/acl/{role}", {"policy": console_policy(m, prefix, cid, b1=True)},
         False),
        ("policy-agent", "update", f"sys/policies/acl/{agent}", {"policy": agent_policy(cid, m)}, False),
        ("policy-boot", "update", f"sys/policies/acl/{boot}", {"policy": boot_policy(cid, m)}, False),
        ("role-console", "update", f"auth/approle/role/{role}",
         dict(token_policies=[role], **console_role_params(B1_CIDR)), False),
        ("role-agent", "update", f"auth/approle/role/{agent}", dict(token_policies=[agent], **agent_role_params()),
         False),
        ("secret-id-console", "update", f"auth/approle/role/{role}/custom-secret-id",
         {"secret_id": _file_source("console-secret-id")}, False),
        ("secret-id-agent", "update", f"auth/approle/role/{agent}/custom-secret-id",
         {"secret_id": _file_source("agent-secret-id")}, False),
        ("token-role-tf", "update", f"auth/token/roles/{tf}", tf_token_role_params(cid, [B1_CIDR]), False),
    ]
    reqs += [(f"audit-header-{h.lower()}", "update", f"sys/config/auditing/request-headers/{h}", {"hmac": False},
              False) for h in AUDIT_HEADERS]
    reqs += [
        ("boot-token", "update", "auth/token/create",
         {"id": _file_source("boot-token"), "policies": [boot], "ttl": BOOT_TOKEN_TTL, "no_parent": True,
          "renewable": False, "display_name": "hops-boot"}, False),
        # EN DERNIER (essai 2) : le moteur KV refuse le réglage pendant sa mise à niveau
        ("max-versions", "update", f"{m}/config", {"max_versions": MAX_VERSIONS}, True),
    ]
    return reqs


def initialize_block(cid, mount=DEFAULT_MOUNT):
    lines = ['initialize "hops" {']
    for name, op, path, data, allow in initialize_requests(cid, mount):
        lines.append(f'  request "{name}" {{')
        lines.append(f"    operation = {json.dumps(op)}")
        lines.append(f"    path = {json.dumps(path)}")
        lines.append(f"    data = {_hcl(data, 2)}")
        if allow:
            lines.append("    allow_failure = true")
        lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def seal_block(key_id, previous_key_id=None):
    _check(KEY_ID_RE, key_id, "unseal key id")
    out = ['seal "static" {', f"  current_key_id = {json.dumps(key_id)}",
           f'  current_key = "file://{C_UNSEAL}"']
    if previous_key_id:
        _check(KEY_ID_RE, previous_key_id, "previous unseal key id")
        if previous_key_id == key_id:
            raise ValueError("the previous unseal key id equals the current one")
        out += [f"  previous_key_id = {json.dumps(previous_key_id)}", f'  previous_key = "file://{C_UNSEAL_PREV}"']
    return "\n".join(out + ["}"]) + "\n"


def template_without_eso(template):
    """Revue finale 2.10.0 I-2 : le modèle de B1 sans la ligne
    `__ESO_LISTENER__`, que le rendu de la 2.9.x laisserait telle quelle
    (configuration illisible). Ce que lit un agent 2.9.x après un retour."""
    return re.sub(r"^__ESO_LISTENER__\n?", "", template, flags=re.M)


def b1_config(cid, key_id, template, previous_key_id=None, init=True, mount=DEFAULT_MOUNT, eso_listener=None):
    """Configuration de B1 depuis le modèle (`config/openbao/openbao.hcl.tpl`,
    porté par le paquet OpenBao) : écoute, stockage raft, audit, descellement
    `static` (et `previous_key` pendant une rotation), `initialize` à la
    première pose. Le modèle doit porter `__SEAL__` et `__INITIALIZE__` ;
    toute strophe qui contredit le plan est refusée."""
    _check(ID_RE, cid, "console id")
    if not (re.search(r"^__SEAL__$", template, re.M) and re.search(r"^__INITIALIZE__$", template, re.M)):
        raise ValueError("OpenBao template without __SEAL__ or __INITIALIZE__ lines")
    body = re.sub(r"#[^\n]*", "", template)
    if re.search(r'storage\s+"file"', body):
        raise ValueError("the OpenBao template uses the file storage (removed in 2.7.0)")
    if re.search(r'^\s*(token|root_token)\s*=', body, re.M):
        raise ValueError("the OpenBao template carries a token")
    for addr in re.findall(r'address\s*=\s*"([^"]+)"', body):
        if not addr.startswith("127.0.0.1:"):
            raise ValueError("the OpenBao listener must stay on 127.0.0.1")
    seal = seal_block(key_id, previous_key_id).rstrip("\n")
    init_text = initialize_block(cid, mount).rstrip("\n") if init else ""
    # 2.10.0 (point 298) : second écouteur ESO, posé seulement à la ligne
    # `__ESO_LISTENER__` du modèle (un modèle plus ancien ne le porte pas)
    has_eso = bool(re.search(r"^__ESO_LISTENER__$", template, re.M))
    if eso_listener and not has_eso:
        raise ValueError("the OpenBao template has no __ESO_LISTENER__ line (template older than 2.10.0)")
    eso_text = eso_listener_block(eso_listener) if eso_listener else ""
    # placeholders sur leur propre ligne seulement (jamais dans un commentaire)
    out = re.sub(r"^__SEAL__$", lambda _m: seal, template, flags=re.M)
    out = re.sub(r"^__ESO_LISTENER__$", lambda _m: eso_text, out, flags=re.M)
    return re.sub(r"^__INITIALIZE__$", lambda _m: init_text, out, flags=re.M)


def logrotate_conf(audit_dir, owner="harvester-ops-openbao", container="harvester-ops-openbao", keep=14):
    """Rotation du journal d'audit de B1 par l'hôte (14 fichiers), `SIGHUP`
    au serveur qui rouvre son fichier ; le nouveau fichier appartient au
    compte du conteneur."""
    return (f"{audit_dir}/audit.log {{\n  daily\n  rotate {int(keep)}\n  compress\n  delaycompress\n"
            f"  missingok\n  notifempty\n  su {owner} root\n  create 0600 {owner} root\n  sharedscripts\n"
            f"  postrotate\n"
            f"    /usr/bin/podman kill --signal HUP {container} >/dev/null 2>&1 || true\n"
            f"  endscript\n}}\n")
