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
    """[(chemin, capacités)] de la politique de la console (texte exact du
    plan, « Politique de la console »)."""
    m = _check(MOUNT_RE, mount, "mount")
    p = _check(PREFIX_RE, prefix, "prefix")
    role = console_role(cid)
    rules = [
        (f"{m}/data/{p}/*", ["create", "read", "update", "delete", "list"]),
        (f"{m}/metadata/{p}/*", ["create", "read", "update", "patch", "list", "delete"]),
        (f"{m}/destroy/{p}/*", ["update"]),
        (f"{m}/config", ["read"]),
        (f"{TRANSIT}/keys/hops-{cid}-*", ["create", "update"]),
        (f"{TRANSIT}/encrypt/hops-{cid}-*", ["update"]),
        (f"{TRANSIT}/decrypt/hops-{cid}-*", ["update"]),
        (f"{TRANSIT}/keys/hops-{cid}-*/rotate", ["update"]),
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


def agent_rules(cid, mount=DEFAULT_MOUNT):
    """Politique de l'agent de l'hôte en B1 (point 271) : rotation de la clé
    racine après un changement de clé de descellement (essai 7), retrait des
    entités `tf-<décl>`, réglage du montage KV, lecture des instantanés, et
    le `secret_id` de la console reposé après une restauration (les
    `secret_id` d'un instantané sont ceux de sa date)."""
    m = _check(MOUNT_RE, mount, "mount")
    return [
        ("sys/rotate/root", ["update", "sudo"]),
        ("identity/entity/name/tf-*", ["read", "delete"]),
        (f"{m}/config", ["read", "update"]),
        ("sys/storage/raft/snapshot", ["read"]),
        (f"auth/approle/role/{console_role(cid)}/custom-secret-id", ["update"]),
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


def tf_token_role_params(cid):
    return {"allowed_policies": [tf_role(cid)], "allowed_entity_aliases": ["tf-*"],
            "orphan": False, "renewable": False}


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
        ("token-role-tf", "update", f"auth/token/roles/{tf}", tf_token_role_params(cid), False),
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


def b1_config(cid, key_id, template, previous_key_id=None, init=True, mount=DEFAULT_MOUNT):
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
    # placeholders sur leur propre ligne seulement (jamais dans un commentaire)
    out = re.sub(r"^__SEAL__$", lambda _m: seal, template, flags=re.M)
    return re.sub(r"^__INITIALIZE__$", lambda _m: init_text, out, flags=re.M)


def logrotate_conf(audit_dir, owner="harvester-ops-openbao", container="harvester-ops-openbao", keep=14):
    """Rotation du journal d'audit de B1 par l'hôte (14 fichiers), `SIGHUP`
    au serveur qui rouvre son fichier ; le nouveau fichier appartient au
    compte du conteneur."""
    return (f"{audit_dir}/audit.log {{\n  daily\n  rotate {int(keep)}\n  compress\n  delaycompress\n"
            f"  missingok\n  notifempty\n  create 0600 {owner} root\n  sharedscripts\n  postrotate\n"
            f"    /usr/bin/podman kill --signal HUP {container} >/dev/null 2>&1 || true\n"
            f"  endscript\n}}\n")
