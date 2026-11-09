"""Classes de secrets de la console (2.9.0), table pure.

Une classe dit où vit un secret aujourd'hui (support fichier), où il ira dans
OpenBao, qui le lit, et s'il peut migrer. Organisation par classe puis par
cluster (place du jeton par personne de la 2.10.0, point 265) :

    kubeconfig/<cluster>                  #kubeconfig
    ssh-cluster/<cluster>                 #private_key
    ssh-kvm/<hôte>                        #private_key
    rancher-oidc/<id>                     #client_secret
    replication-notify/global             #<canal>__<secret>
    replication-hook/<plan>/<action>      #hdr_<empreinte du nom d'en-tête>
    tf-kvm/<hôte>/<décl>/<variable>       #value
    plan-run/<action_id>/<nom>            #<champ>
    eso/<clé du cluster>/<namespace>/<nom> #<champ>   (2.10.0, OpenBao seulement)

Les noms de champ d'OpenBao sont limités à `[a-z0-9_]{1,64}` : un identifiant
de canal (`[a-z0-9-]`) y entre en remplaçant `-` par `_` (bijectif, un
identifiant n'a pas de `_`) ; un nom d'en-tête HTTP (casse, ponctuation) par
une empreinte courte de son nom (le nom reste en clair dans le JSON local, à
côté de la référence). Jamais d'empreinte d'une VALEUR.

Aucune lecture ni écriture ici : les emplacements sont rendus sous forme de
descripteurs, lus par le support fichier de `secret_store`.
"""

import collections
import hashlib
import re
from pathlib import Path

ClassSpec = collections.namedtuple("ClassSpec", "local_paths bao_path migratable readers person",
                                   defaults=(False,))
# 2.10.0 : `person` = classe lue (ou écrite) avec le jeton OpenBao de la
# personne connectée par Rancher quand elle en a un (table « Qui agit » du
# plan 2.10.0) ; les autres classes restent lues par l'identité de service.
# 2.9.0 tâche 8 : `eligible` (code d'un secret qui reste local, ou None),
# `originals` (originaux gardés après migration, sans valeur) et
# `erase_original` (confirmation ou délai de garde) ; facultatifs pour un
# adaptateur simulé.
Adapter = collections.namedtuple("Adapter", "list_local read_local write_ref restore_local eligible originals "
                                 "erase_original original_value", defaults=(None, None, None, None))

# Emplacements locaux (descriptifs), chemin dans OpenBao (sous le préfixe de
# la console), lecteurs autorisés (`fichier:fonction`, relus par le test de
# source de la tâche 4).
CLASS_TABLE = {
    "kubeconfig": ClassSpec(
        ("<state>/clusters.d/<n>.yaml (kubeconfig)", "<state>/kubeconfigs/<n>.yaml"),
        "kubeconfig/{name}", True,
        ("web/app.py:_cluster_kubeconfig_path", "bin/lib/cluster_decl.py:materialize",
         "bin/lib/common.sh:_secret_field")),
    "ssh-cluster": ClassSpec(
        ("<state>/clusters.d/<n>.yaml (ssh.key)", "<state>/ssh/<n>_id"),
        "ssh-cluster/{name}", True,
        ("web/app.py:_cluster_ssh_key_path", "bin/lib/cluster_decl.py:materialize",
         "bin/lib/node_images.py:ssh_options", "bin/lib/common.sh:_secret_field"), True),
    "ssh-kvm": ClassSpec(
        ("<state>/kvm.d/<n>.yaml (ssh_key)", "<state>/ssh/<n>_id"),
        "ssh-kvm/{name}", True,
        ("bin/lib/kvm_host.py:_ssh_base", "web/app.py:_tf_kvm_stage"), True),
    "rancher-oidc": ClassSpec(
        ("<state>/rancher.d/<id>.oidc-secret",),
        "rancher-oidc/{name}", True,
        ("web/rancher_servers.py:read_secret",)),
    "replication-notify": ClassSpec(
        ("<state>/replication.db settings.notify (token, url, password, secret_headers)",),
        "replication-notify/global", True,
        ("bin/lib/replication_notify.py:resolve_secrets",)),
    "replication-hook": ClassSpec(
        ("<state>/replication.db plans.options.hooks[].secret_headers",),
        "replication-hook/{plan}/{action}", True,
        ("bin/lib/replication_run.py:call_hook",)),
    "tf-kvm": ClassSpec(
        ("<terraform>/.kvm/<hôte>/decls/<décl>/.secrets/<variable>",),
        "tf-kvm/{host}/{decl}/{var}", True,
        ("web/app.py:_tf_kvm_secret",), True),
    "plan-run": ClassSpec(
        ("memory",), "plan-run/{action}/{name}", True,
        ("web/app.py:_deploy_install_secrets_get", "web/app.py:_deploy_gitops_secret_get")),
    # 2.10.0 : valeurs lues par External Secrets Operator dans un cluster
    # enfant ; écrites par une personne (son jeton), jamais lues par la
    # console, sans emplacement local (rien à migrer)
    "eso": ClassSpec((), "eso/{key}/{namespace}/{name}", False, (), True),
    # restent locaux, toujours (refus `class-not-migratable`)
    "accounts": ClassSpec(("<state>/accounts.json", "<state>/setup-token"), None, False, ()),
    "web-auth": ClassSpec(("/etc/harvester-ops/htpasswd", "/etc/harvester-ops/tls/key.pem"), None, False, ()),
    "public-keys": ClassSpec(("update signers", "artefact trust", "*.pub", "*_known_hosts", "*_known"),
                             None, False, ()),
    "openbao-credentials": ClassSpec(("/etc/harvester-ops/openbao/",), None, False, ()),
    "config-kubeconfig": ClassSpec(("config.yaml clusters[].kubeconfig",), None, False, ()),
}

MIGRATABLE = tuple(c for c, s in CLASS_TABLE.items() if s.migratable)
# 2.10.0 : classes personnelles (ssh-cluster, ssh-kvm, tf-kvm, eso) et
# classes qui ne vivent que dans OpenBao (aucun emplacement local)
PERSON = tuple(c for c, s in CLASS_TABLE.items() if s.person)
BAO_ONLY = ("eso",)
# Ordre proposé par l'interface (point 255) ; `plan-run` n'a rien à migrer.
MIGRATION_ORDER = ("replication-notify", "replication-hook", "rancher-oidc", "tf-kvm", "ssh-kvm",
                   "ssh-cluster", "kubeconfig")

FIELD_RE = re.compile(r"^[a-z0-9_]{1,64}$")
FIELDS = {"kubeconfig": "kubeconfig", "ssh-cluster": "private_key", "ssh-kvm": "private_key",
          "rancher-oidc": "client_secret", "tf-kvm": "value"}
NOTIFY_PATH = "global"
NOTIFY_SECRETS = {"matrix": ("token",), "slack": ("url",), "webhook": (), "email": ("password",)}


class NotMigratable(ValueError):
    """Classe qui reste locale : `class-not-migratable`."""

    code = "class-not-migratable"

    def __init__(self, cls):
        super().__init__(f"class-not-migratable: {cls}")
        self.cls = cls


def spec(cls):
    try:
        return CLASS_TABLE[cls]
    except KeyError:
        raise ValueError("unknown secret class") from None


def is_person(cls):
    """Classe lue avec le jeton de la personne quand elle en a un."""
    s = CLASS_TABLE.get(cls)
    return bool(s and s.person)


def require_migratable(cls):
    if not spec(cls).migratable:
        raise NotMigratable(cls)
    return cls


def header_field(header, prefix="hdr_"):
    """Champ d'un en-tête secret : empreinte du NOM (jamais de la valeur)."""
    return prefix + hashlib.sha256(str(header).lower().encode()).hexdigest()[:10]


def notify_field(channel_id, secret):
    """Champ d'un secret d'un canal de notification ; `secret` vaut `token`,
    `url`, `password`, ou `("header", nom)` pour un en-tête secret."""
    cid = str(channel_id).replace("-", "_")
    if isinstance(secret, tuple):
        return f"{cid}__{header_field(secret[1])}"
    return f"{cid}__{secret}"


def local_location(cls, path, field, state_dir, tf_dir=None, repl_db=None):
    """Descripteur de l'emplacement local d'un secret, sans rien lire :

    * ("file", Path, None) : un fichier dont le contenu est la valeur ;
    * ("decl-file", Path, (decl, clé)) : un fichier désigné par un champ de
      déclaration (si la déclaration existe et que le champ est un chemin),
      `Path` étant l'emplacement par défaut ;
    * ("notify", Path de la base, (canal, secret)) ;
    * ("hook", Path de la base, (plan, action, champ)) ;
    * ("memory", None, None).

    ValueError si le chemin ne correspond pas à la classe."""
    state = Path(state_dir)
    segs = path.split("/")
    name = segs[0]
    if cls == "plan-run":
        return ("memory", None, None)
    if cls in FIELDS and field != FIELDS[cls]:
        raise ValueError("unknown field for this class")
    if cls == "kubeconfig":
        _one(segs)
        return ("decl-file", state / "kubeconfigs" / f"{name}.yaml",
                (state / "clusters.d" / f"{name}.yaml", "kubeconfig"))
    if cls == "ssh-cluster":
        _one(segs)
        return ("decl-file", state / "ssh" / f"{name}_id", (state / "clusters.d" / f"{name}.yaml", "ssh.key"))
    if cls == "ssh-kvm":
        _one(segs)
        # 2.9.0 : la déclaration de l'hôte porte la référence après migration
        return ("decl-file", state / "ssh" / f"{name}_id", (state / "kvm.d" / f"{name}.yaml", "ssh_key"))
    if cls == "rancher-oidc":
        _one(segs)
        return ("file", state / "rancher.d" / f"{name}.oidc-secret", None)
    if cls == "tf-kvm":
        if len(segs) != 3:
            raise ValueError("tf-kvm path is <host>/<decl>/<variable>")
        root = Path(tf_dir) if tf_dir else state / "terraform"
        return ("file", root / ".kvm" / segs[0] / "decls" / segs[1] / ".secrets" / segs[2], None)
    db = Path(repl_db) if repl_db else state / "replication.db"
    if cls == "replication-notify":
        if path != NOTIFY_PATH or "__" not in field:
            raise ValueError("replication-notify path is global, field <channel>__<secret>")
        cid, _, secret = field.rpartition("__")       # un identifiant peut contenir "--" (revue M10)
        return ("notify", db, (cid.replace("_", "-"), secret))
    if cls == "replication-hook":
        if len(segs) != 2 or not field.startswith("hdr_"):
            raise ValueError("replication-hook path is <plan>/<action>, field hdr_<hash>")
        return ("hook", db, (segs[0], segs[1], field))
    raise ValueError("no local location for this class")


def _one(segs):
    if len(segs) != 1:
        raise ValueError("this class takes a single name")


def adapter(cls):
    """Adaptateur de migration d'une classe : lecture, liste, pose de la
    référence (original gardé : `<fichier>.migrated` ou table
    `secret_rollback`), retour en local, originaux gardés et leur effacement.
    Les entrées-sorties sont celles du support fichier `fs` passé à chaque
    appel (secret_store.FileStore)."""
    require_migratable(cls)
    if cls == "plan-run":
        return Adapter(lambda fs: [], lambda fs, path, field: None, _nothing, _nothing,
                       lambda fs, path, field: None, lambda fs: [], _nothing)
    return Adapter(lambda fs: fs.list_local(cls),
                   lambda fs, path, field: fs.read_local(cls, path, field),
                   lambda fs, path, field, ref, value=None, action=None: fs.write_ref(cls, path, field, ref,
                                                                                       value, action),
                   lambda fs, path, field, value: fs.restore_local(cls, path, field, value),
                   lambda fs, path, field: fs.eligible(cls, path, field),
                   lambda fs: fs.originals(cls),
                   lambda fs, path, field: fs.erase_original(cls, path, field),
                   lambda fs, path, field: fs.original_value(cls, path, field))


def _nothing(*_a, **_k):
    raise ValueError("plan-run secrets are never migrated (new runs go to OpenBao directly)")
