"""harvester-ops : états OpenTofu chiffrés par le moteur transit d'OpenBao
(2.9.0, tâche 9, décision 140, points 261 et 270).

Pur : aucun appel réseau, aucun processus. La console (web/app.py) s'en
sert pour écrire le fichier réservé `_encryption.tf` d'un espace de
déclaration, construire l'environnement du seul processus `tofu`, décider
s'il faut chiffrer et lire la sortie de `tofu state list`.

Ce que l'essai 8 (plan 2.9.0) a établi et que ce module suit :
- OpenTofu appelle `transit/datakey/plaintext/<clé>` pour chiffrer et
  `transit/decrypt/<clé>` pour lire, jamais `transit/encrypt` ;
- une clé par déclaration, `hops-<console>-tf-<décl>`, désignée par l'alias
  d'entité `tf-<décl>` du jeton enfant (politique fixe à gabarit, jamais
  une politique par déclaration) ;
- adresse, jeton et CA par `BAO_ADDR`, `BAO_TOKEN`, `BAO_CACERT`, dans
  l'environnement du processus, jamais en argument ni dans un fichier ;
- migration d'un état clair : méthode `unencrypted` en repli, `enforced =
  false`, `apply -refresh-only`, puis le bloc définitif ; retour en clair
  par le chemin inverse ;
- jamais `state mv -state-out` (la destination est écrite en clair) :
  `state pull` puis `state push -`.
"""

import json
import re

import openbao_policy as pol

ENCRYPTION_FILE = "_encryption.tf"
TRANSIT_PATH = "/transit"
KEY_LENGTH = 32
TOKEN_MARGIN_S = 300                  # ttl du jeton enfant = délai de l'action + 5 min
MODES = ("enforced", "migrate-in", "migrate-out")

WARN_NEEDS_OPENTOFU = "tf-encryption-needs-opentofu"
WARN_VAULT_UNVERIFIED = "tf-encryption-vault-unverified"
RESERVED = "tf-encryption-reserved"
# Le fournisseur `openbao` d'OpenTofu est documenté compatible avec Vault
# 1.14 (dernière version MPL) ; au-delà, chiffrement après acquittement de
# l'administrateur (point 270 B).
VAULT_VERIFIED = (1, 14)

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)")
_ADDR_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[A-Za-z0-9_-]+$")


def key_name(cid, decl):
    """`hops-<console>-tf-<décl>` (nom validé par openbao_policy)."""
    return pol.tf_key(cid, decl)


def alias(decl):
    """Alias d'entité du jeton enfant : `tf-<décl>`."""
    return pol.tf_alias(decl)


def block(cid, decl, mode="enforced"):
    """Texte exact de `_encryption.tf`.

    `enforced` : bloc définitif, état et plan chiffrés, un état clair est
    refusé. `migrate-in` : chiffre à la prochaine écriture, lit encore un
    état ou un plan clairs (repli `unencrypted`). `migrate-out` : écrit en
    clair, lit encore l'état chiffré (retour en clair)."""
    if mode not in MODES:
        raise ValueError("unknown encryption mode")
    key = key_name(cid, decl)
    head = (
        "# Written by harvester-ops: OpenTofu state encryption (OpenBao transit).\n"
        "# Never edit: the console rewrites or refuses this file.\n"
        "terraform {\n"
        "  encryption {\n"
        '    key_provider "openbao" "hops" {\n'
        f'      key_name            = "{key}"\n'
        f'      transit_engine_path = "{TRANSIT_PATH}"\n'
        f"      key_length          = {KEY_LENGTH}\n"
        "    }\n"
        '    method "aes_gcm" "hops" {\n'
        "      keys = key_provider.openbao.hops\n"
        "    }\n"
    )
    if mode == "enforced":
        primary, fallback, enforced = "method.aes_gcm.hops", None, "true"
    elif mode == "migrate-in":
        primary, fallback, enforced = "method.aes_gcm.hops", "method.unencrypted.migrate", "false"
    else:
        primary, fallback, enforced = "method.unencrypted.migrate", "method.aes_gcm.hops", "false"
    if fallback:
        head += '    method "unencrypted" "migrate" {}\n'
    body = ""
    for target in ("state", "plan"):
        body += f"    {target} {{\n      method   = {primary}\n      enforced = {enforced}\n"
        if fallback:
            body += f"      fallback {{\n        method = {fallback}\n      }}\n"
        body += "    }\n"
    return head + body + "  }\n}\n"


def all_blocks(cid, decl):
    """Les seuls textes que la console écrit dans `_encryption.tf` pour
    cette déclaration (relus par tf_guard)."""
    return tuple(block(cid, decl, m) for m in MODES)


def is_encrypted(state):
    """Vrai si `state` (texte ou dict déjà lu) est un état chiffré par
    OpenTofu (`encrypted_data` + `encryption_version`)."""
    if isinstance(state, (str, bytes)):
        try:
            state = json.loads(state)
        except ValueError:
            return False
    return isinstance(state, dict) and "encrypted_data" in state and "encryption_version" in state


def child_ttl(timeout_s):
    """Durée du jeton enfant : délai de l'action + 5 min, en secondes."""
    return int(timeout_s) + TOKEN_MARGIN_S


def env(addr, token, ca_file=None):
    """Variables ajoutées à l'environnement du seul processus `tofu`."""
    out = {"BAO_ADDR": str(addr), "BAO_TOKEN": str(token)}
    if ca_file:
        out["BAO_CACERT"] = str(ca_file)
    return out


def server_version(seal_status):
    """(majeur, mineur) lu dans `sys/seal-status`, ou None."""
    m = _VERSION_RE.match(str((seal_status or {}).get("version") or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def server_verdict(mode, seal_status):
    """None (rien à dire) ou WARN_VAULT_UNVERIFIED. B1 est un OpenBao posé
    par la console. En mode A : OpenBao est en 2.x et plus (fork de Vault
    1.14) ; un Vault 1.x plus récent que 1.14, ou une version illisible,
    demande l'acquittement de l'administrateur."""
    if mode == "b1":
        return None
    v = server_version(seal_status)
    if v is None:
        return WARN_VAULT_UNVERIFIED
    if v[0] == 1 and v > VAULT_VERIFIED:
        return WARN_VAULT_UNVERIFIED
    return None


def ack_matches(ack, addr, seal_status):
    """L'acquittement vaut pour cette adresse et cette version majeure.mineure."""
    if not isinstance(ack, dict):
        return False
    v = server_version(seal_status)
    return ack.get("addr") == addr and v is not None and ack.get("version") == f"{v[0]}.{v[1]}"


def decide(flavor, active, encrypted_state, warning=None, acked=False):
    """{"encrypt": bool, "warning": code|None, "refuse": code|None}.

    - un état déjà chiffré se lit toujours avec OpenBao et OpenTofu (sans
      eux : refus, l'état serait illisible) ;
    - OpenBao inactif : rien ;
    - Terraform : jamais de chiffrement, avertissement ;
    - serveur non vérifié et non acquitté : avertissement, état en clair."""
    if encrypted_state:
        if flavor != "opentofu":
            return {"encrypt": False, "warning": None, "refuse": WARN_NEEDS_OPENTOFU}
        if not active:
            return {"encrypt": False, "warning": None, "refuse": "tf-state-encrypted-openbao-inactive"}
        return {"encrypt": True, "warning": None, "refuse": None}
    if not active:
        return {"encrypt": False, "warning": None, "refuse": None}
    if flavor != "opentofu":
        return {"encrypt": False, "warning": WARN_NEEDS_OPENTOFU, "refuse": None}
    if warning and not acked:
        return {"encrypt": False, "warning": warning, "refuse": None}
    return {"encrypt": True, "warning": warning, "refuse": None}


def parse_state_list(text):
    """Adresses des ressources gérées du module racine, lues dans la sortie
    de `tofu state list` (une par ligne) : sources de données et modules
    écartés, index d'instance retirés, comme tf_state.addresses sur un état
    clair."""
    out = set()
    for line in (text or "").splitlines():
        a = line.strip()
        if not a or a.startswith(("data.", "module.")):
            continue
        a = a.split("[", 1)[0]
        if _ADDR_RE.match(a):
            out.add(a)
    return sorted(out)
