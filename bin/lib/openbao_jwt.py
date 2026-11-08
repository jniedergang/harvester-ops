"""Jeton OpenBao par personne (2.10.0), règles pures.

Paramètres de la méthode `jwt` d'OpenBao qui fait confiance au fournisseur
OIDC d'un Rancher (montage, configuration, rôle `person`), lecture des
revendications utiles d'un jeton d'identité, empreinte d'un JWKS, et la table
« Qui agit » du plan : avec quelle identité la console lit un secret.

Aucune entrée-sortie ici, sauf `fetch_json` (GET sans jeton des documents
publics de Rancher, découverte et JWKS) : les textes et paramètres sont
rendus, la console ne les écrit jamais dans OpenBao (décision 287 : agent en B1, commandes de
l'assistant en mode A).

Valeurs exactes : plan `docs/design/2026-11-08-jeton-personne-2100-plan.md`,
« Méthode `jwt` d'une personne », vérifiées sur OpenBao 2.7.2 et Vault 1.14.2
à la tâche 1 (essais 2 à 4) :

* `user_claim = "sub"`, `bound_audiences = [client]` ; le jeton d'accès de
  Rancher porte la même audience, la console n'envoie donc QUE le jeton
  d'identité (garantie de la console, pas d'OpenBao) ;
* jamais de `groups_claim` (point 303 : un rôle qui en porte un refuse toute
  personne dont le jeton n'a pas ce claim) ;
* `auth_provider` est le nom du claim émis par Rancher 2.14.1 ;
* une seule source de clés par montage (découverte OU clés statiques).
"""

import hashlib
import json
import re

import secret_classes

PERSON_ROLE = "person"
TOKEN_TTL = "15m"
TOKEN_MAX_TTL = "15m"
ALGS = ["RS256"]
MOUNT_PREFIX = "jwt-hops-"
# Revue 6-7 I-3 (décision 314 A) : deux familles de montages aux préfixes
# disjoints, `p-` pour les personnes, `eso-` pour les clusters enfants. Un
# Rancher nommé `eso-1d905023` (accepté par RANCHER_ID_RE) donnait le nom du
# montage ESO de la clé `1d905023`.
PERSON_INFIX = "p-"
ESO_INFIX = "eso-"
ESO_KEY_RE = re.compile(r"^[0-9a-f]{8}$")
RANCHER_ID_MAX = 12
CONSOLE_ID_RE = re.compile(r"^[0-9a-f]{12}$")
RANCHER_ID_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
CIDR_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}/\d{1,2}$|^[0-9a-fA-F:]+/\d{1,3}$")
# revendications d'un jeton d'identité utiles à la console (jamais le jeton)
USEFUL_CLAIMS = ("iss", "sub", "aud", "exp", "iat", "name", "auth_provider")
DEFAULT_SETTINGS = {"enabled": False, "servers": {}, "groups_only": False}


def person_mount(console_id, rancher_id):
    """`jwt-hops-<console>-p-<rancher>` ; un identifiant de Rancher de plus de
    12 caractères devient ses 6 premiers suivis de 6 hexadécimaux de son
    empreinte (deux Rancher aux noms longs proches ne partagent pas un
    montage)."""
    if not CONSOLE_ID_RE.match(str(console_id or "")):
        raise ValueError("invalid console id")
    rid = str(rancher_id or "")
    if not RANCHER_ID_RE.match(rid):
        raise ValueError("invalid Rancher id")
    if len(rid) > RANCHER_ID_MAX:
        rid = rid[:6].rstrip("-") + hashlib.sha256(rid.encode()).hexdigest()[:6]
    return f"{MOUNT_PREFIX}{console_id}-{PERSON_INFIX}{rid}"


def eso_mount(console_id, key):
    """`jwt-hops-<console>-eso-<clé>` d'un cluster enfant (ESO, point 295 ;
    clé : child_eso.cluster_key, 8 hexadécimaux)."""
    if not CONSOLE_ID_RE.match(str(console_id or "")):
        raise ValueError("invalid console id")
    if not ESO_KEY_RE.match(str(key or "")):
        raise ValueError("invalid ESO cluster key (8 hexadecimals)")
    return f"{MOUNT_PREFIX}{console_id}-{ESO_INFIX}{key}"


def is_person_mount(console_id, mount):
    """Montage `jwt` d'une personne de cette console (préfixe `p-`)."""
    return str(mount or "").rstrip("/").startswith(f"{MOUNT_PREFIX}{console_id}-{PERSON_INFIX}")


def is_eso_mount(console_id, mount):
    """Montage `jwt` d'un cluster enfant de cette console."""
    m = str(mount or "").rstrip("/")
    head = f"{MOUNT_PREFIX}{console_id}-{ESO_INFIX}"
    return m.startswith(head) and bool(ESO_KEY_RE.match(m[len(head):]))


def person_policy_name(console_id):
    if not CONSOLE_ID_RE.match(str(console_id or "")):
        raise ValueError("invalid console id")
    return f"hops-person-{console_id}"


def person_role_params(client_id, console_cidr, console_id, groups_claim=None):
    """Paramètres du rôle `person` (`auth/<montage>/role/person`). Le
    jeton OpenBao d'une personne vit 15 minutes, n'est accepté que depuis
    la console (`token_bound_cidrs`, vérifié dès la connexion : essai 2) et
    porte la politique commune des personnes ; les politiques de groupe
    viennent des groupes internes d'OpenBao (point 303)."""
    if groups_claim is not None:
        raise ValueError("groups_claim is never set in 2.10.0 (decision 303)")
    cid = str(client_id or "").strip()
    if not cid or len(cid) > 253 or any(c.isspace() for c in cid):
        raise ValueError("invalid OIDC client id")
    if not CIDR_RE.match(str(console_cidr or "")):
        raise ValueError("invalid console address (CIDR expected)")
    return {
        "role_type": "jwt",
        "user_claim": "sub",
        "bound_audiences": [cid],
        "claim_mappings": {"name": "name", "auth_provider": "provider"},
        "token_policies": [person_policy_name(console_id)],
        "token_type": "service",
        "token_ttl": TOKEN_TTL,
        "token_max_ttl": TOKEN_MAX_TTL,
        "token_num_uses": 0,
        "token_no_default_policy": False,
        "token_bound_cidrs": [str(console_cidr)],
        "verbose_oidc_logging": False,
    }


def mount_config(issuer, ca_pem=None, pubkeys=None):
    """Configuration du montage (`auth/<montage>/config`) : découverte par
    l'émetteur de Rancher (avec sa CA si la console en a une), ou clés
    publiques statiques quand OpenBao ne joint pas Rancher (point 286).
    OpenBao refuse deux sources (« exactly one of ... must be set »)."""
    iss = str(issuer or "").rstrip("/")
    if not iss.startswith("https://") or not iss.endswith("/oidc"):
        raise ValueError("the issuer is <Rancher URL>/oidc over https")
    out = {"bound_issuer": iss, "jwt_supported_algs": list(ALGS), "default_role": PERSON_ROLE}
    if pubkeys:
        if ca_pem:
            raise ValueError("one key source only: discovery or static keys")
        keys = [str(k).strip() for k in pubkeys]
        if not all(k.startswith("-----BEGIN PUBLIC KEY-----") for k in keys):
            raise ValueError("static keys are PEM public keys")
        out["jwt_validation_pubkeys"] = keys
        return out
    out["oidc_discovery_url"] = iss
    if ca_pem:
        out["oidc_discovery_ca_pem"] = str(ca_pem)
    return out


def keys_sha256(jwks):
    """Empreinte d'un JWKS : sha256 des clés publiques triées par `kid`
    (`kid`, `kty`, `n`, `e` seulement : l'ordre ou un champ décoratif ne la
    change pas). Rotation de Rancher = empreinte changée dès l'ajout de la
    nouvelle clé publique."""
    keys = (jwks or {}).get("keys") if isinstance(jwks, dict) else None
    if not isinstance(keys, list) or not keys:
        raise ValueError("no key in the JWKS")
    norm = sorted(({k: str(key.get(k) or "") for k in ("kid", "kty", "n", "e")} for key in keys
                   if isinstance(key, dict)), key=lambda d: (d["kid"], d["n"]))
    return hashlib.sha256(json.dumps(norm, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _der_len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _der(tag, body):
    return bytes([tag]) + _der_len(len(body)) + body


def _der_int(value):
    b = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    if b[0] & 0x80:
        b = b"\x00" + b
    return _der(0x02, b)


def _b64u_int(text):
    import base64
    raw = base64.urlsafe_b64decode(str(text) + "=" * (-len(str(text)) % 4))
    return int.from_bytes(raw, "big")


RSA_OID = bytes.fromhex("06092a864886f70d0101010500")      # rsaEncryption + NULL


def jwk_to_pem(jwk):
    """Clé publique RSA d'un JWK (`kty` RSA, `n`, `e`) en PEM
    SubjectPublicKeyInfo, la forme que prend `jwt_validation_pubkeys`
    (tâche 7, clés statiques). Une clé qui n'est pas RSA, ou de moins de
    2048 bits, est refusée."""
    import base64
    if not isinstance(jwk, dict) or jwk.get("kty") != "RSA" or jwk.get("use") not in (None, "sig"):
        raise ValueError("RSA signing key expected")
    try:
        n, e = _b64u_int(jwk["n"]), _b64u_int(jwk["e"])
    except (KeyError, ValueError, TypeError):
        raise ValueError("RSA key without n or e") from None
    if n.bit_length() < 2048 or e < 3:
        raise ValueError("RSA key below 2048 bits")
    spki = _der(0x30, _der(0x30, RSA_OID) + _der(0x03, b"\x00" + _der(0x30, _der_int(n) + _der_int(e))))
    b64 = base64.b64encode(spki).decode()
    return "-----BEGIN PUBLIC KEY-----\n" + "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64)) \
        + "\n-----END PUBLIC KEY-----"


def pubkeys_of(jwks):
    """Clés publiques PEM d'un JWKS, dans l'ordre des `kid` (Rancher : une
    clé `kid` `key`, toutes les clés publiques du Secret
    `cattle-system/oidc-signing-key` pendant une rotation)."""
    keys = (jwks or {}).get("keys") if isinstance(jwks, dict) else None
    if not isinstance(keys, list) or not keys:
        raise ValueError("no key in the JWKS")
    if len(keys) > 8:
        raise ValueError("too many keys in the JWKS")
    return [jwk_to_pem(k) for k in sorted(keys, key=lambda k: str((k or {}).get("kid") or ""))]


def merge_pubkeys(old, new):
    """Rotation sans coupure (tâche 7) : ancienne(s) et nouvelle(s) clés
    ensemble jusqu'à la confirmation ; ordre stable, sans doublon."""
    out = []
    for k in list(new or []) + list(old or []):
        k = str(k).strip()
        if k and k not in out:
            out.append(k)
    return out


def discovery_url(issuer):
    return str(issuer).rstrip("/") + "/.well-known/openid-configuration"


def jwks_url(issuer):
    return str(issuer).rstrip("/") + "/.well-known/jwks.json"


def check_discovery(doc, issuer):
    """Vérification 1 de `person-token check` : [(raison, ok)] du document de
    découverte de Rancher (émetteur égal au réglage, `jwks_uri`, RS256)."""
    doc = doc if isinstance(doc, dict) else {}
    iss = str(issuer).rstrip("/")
    algs = doc.get("id_token_signing_alg_values_supported")
    return [(f"issuer {doc.get('issuer')!s:.120}", str(doc.get("issuer") or "").rstrip("/") == iss),
            ("jwks_uri announced", bool(doc.get("jwks_uri"))),
            ("RS256 announced", isinstance(algs, list) and "RS256" in algs)]


def fetch_json(url, ca_pem=None, timeout=10, opener=None):
    """GET sans authentification d'un document public de Rancher (découverte,
    JWKS : lisibles sans jeton, essai 1), CA épinglée s'il y en a une, sans
    proxy ni redirection hors https. Seule entrée-sortie du module ; `opener`
    injectable (tests). Rend le JSON (dict) ou lève ValueError."""
    import ssl
    import urllib.request
    if not str(url).startswith("https://"):
        raise ValueError("https URL expected")
    if opener is None:
        ctx = ssl.create_default_context(cadata=ca_pem) if ca_pem else ssl.create_default_context()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                             urllib.request.HTTPSHandler(context=ctx))
    try:
        with opener.open(urllib.request.Request(url, headers={"Accept": "application/json"}),
                         timeout=timeout) as r:
            raw = r.read(1 << 20)
    except OSError as e:
        raise ValueError(f"{url}: {type(e).__name__}") from None
    try:
        doc = json.loads(raw)
    except ValueError:
        raise ValueError(f"{url}: not JSON") from None
    if not isinstance(doc, dict):
        raise ValueError(f"{url}: not an object")
    return doc


def useful_claims(claims):
    """Les revendications qu'il est permis de garder ou d'afficher (jamais
    `nonce`, ni un jeton Rancher porté par un jeton d'accès)."""
    return {k: claims[k] for k in USEFUL_CLAIMS if isinstance(claims, dict) and k in claims}


def is_access_token(claims):
    """Un jeton d'accès du fournisseur OIDC de Rancher porte `token`
    (`token-...`, un jeton Rancher utilisable sur l'API et `/k8s/clusters`)
    et `scope` ; un jeton d'identité ne les porte pas (essai 1)."""
    return isinstance(claims, dict) and ("token" in claims or "scope" in claims)


def settings_of(secrets_settings):
    """Réglage `person_token` complété (absent ou illisible : éteint)."""
    raw = (secrets_settings or {}).get("person_token")
    out = dict(DEFAULT_SETTINGS, servers={})
    if isinstance(raw, dict):
        out["enabled"] = raw.get("enabled") is True
        out["groups_only"] = raw.get("groups_only") is True
        if isinstance(raw.get("servers"), dict):
            out["servers"] = {str(k): v for k, v in raw["servers"].items() if isinstance(v, dict)}
    return out


def identity_for(cls, session, settings, actor=None):
    """La table « Qui agit » du plan, pour une lecture ou une écriture d'un
    secret de la classe `cls` dans le contexte d'une requête :

    * personne connectée par Rancher en connexion unique (`kind = "sso"`),
      classe personnelle, réglage allumé et méthode `jwt` réglée pour son
      Rancher : identité de la personne (son jeton OpenBao) ;
    * tout le reste (classe de service, connexion directe, compte local,
      réglage éteint, OpenBao local) : identité de service, avec la personne
      dans `X-Hops-Actor`.

    `session` : la session Rancher de la requête (None pour un compte local
    ou hors requête) ; `settings` : réglages du magasin de secrets."""
    import secret_store
    who = actor
    if who is None and session is not None:
        who = getattr(session, "login", None)
    if session is None or getattr(session, "kind", "sso") != "sso":
        return secret_store.Identity("service", who)
    if not secret_classes.is_person(cls) or not secret_store.openbao_active(settings or {}):
        return secret_store.Identity("service", who)
    pt = settings_of(settings)
    if not pt["enabled"]:
        return secret_store.Identity("service", who)
    server = pt["servers"].get(str(getattr(session, "server_id", None) or "")) or (
        pt["servers"].get("config") if getattr(session, "server_id", None) is None else None)
    if not server or not server.get("mount"):
        # ce Rancher n'a pas de méthode jwt réglée : rien à présenter
        return secret_store.Identity("service", who)
    return secret_store.Identity("person", who, session_id=getattr(session, "sid", None),
                                 mount=server["mount"], role=PERSON_ROLE)

