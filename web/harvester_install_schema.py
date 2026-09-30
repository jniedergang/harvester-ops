"""Schéma de la configuration d'installation de Harvester.

Source : https://github.com/harvester/harvester-installer
  - fichier  : pkg/config/config.go (structures) et pkg/config/rename.go
  - étiquette : v1.9.0-dev-20260705 (le dépôt n'a pas d'étiquette v1.9.0 ;
    c'est aussi la tête de la branche de version v1.9)
  - commit   : 8b8f0f223b67f95a60ec64a5da954a911b024c15

L'installeur lit le YAML en dictionnaire, renomme les clés (rename.go,
`FuzzyNames`) puis le convertit dans ses structures Go. Une clé qu'il ne
reconnaît pas est ignorée EN SILENCE : c'est pourquoi la console la refuse
ici, avec son chemin (`server_url` placé sous `install` jusqu'en 1.44.1 n'a
jamais servi, et a coûté une réinstallation).

Noms acceptés, par champ : la forme snake_case que produit
`convert.ToYAMLKey` à partir du nom JSON (`subnetMask` -> `subnet_mask`), et
le nom JSON lui-même (`hwAddr`). rename.go accepte aussi des variantes
accidentelles (minuscules collées, singulier obtenu en retirant « s » ou
« es » : `interfac`, `o` pour `os`) : elles sont refusées ici, pour qu'un
même champ ne puisse pas être écrit sous deux noms, l'un par le formulaire,
l'autre par le YAML avancé. Le correspondant `password` -> `passphrase` de
rename.go ne vise aucun champ de ce schéma (il n'y a pas de `passphrase`).
"""

import re
from collections import namedtuple

import yaml

HARVESTER_INSTALLER_TAG = "v1.9.0-dev-20260705"
HARVESTER_INSTALLER_COMMIT = "8b8f0f223b67f95a60ec64a5da954a911b024c15"

# Types feuilles : "str", "int", "uint" (entier >= 0, uint32 côté Go), "bool", "list[str]", "dict[str,str]",
# "dict[str,list[str]]", "any". Types composés : "object" (sous-schéma),
# "list[object]" (liste d'objets), "dict[object]" (dictionnaire d'objets).
Field = namedtuple("Field", "key type sub aliases json")


def to_yaml_key(name):
    """Portage exact de `convert.ToYAMLKey` (rancher/mapper) : une
    majuscule après une minuscule devient « _ » + minuscule. Le drapeau
    `cap` n'est levé que si la PREMIÈRE lettre est une majuscule : une suite
    de majuscules en milieu de nom est donc découpée lettre par lettre
    (`guaranteedEngineManagerCPU` -> `guaranteed_engine_manager_c_p_u`)."""
    out = []
    cap = False
    for i, ch in enumerate(name):
        if i == 0:
            cap = ch.isupper()
            out.append(ch.lower())
            continue
        if ch.isupper():
            out.append(ch.lower() if cap else "_" + ch.lower())
        else:
            cap = False
            out.append(ch)
    return "".join(out)


# Nom écrit par la console quand ce n'est pas la forme snake_case.
# `hwAddr` : orthographe de la documentation Harvester, et celle que la
# console écrit depuis la v1.18.0 (le rendu ne doit pas changer).
# Les champs `...CPU` : la forme snake_case de ToYAMLKey (`_c_p_u`) est
# acceptée par l'installeur mais illisible ; la documentation garde le nom
# JSON.
_OUTPUT_NAME = {
    "hwAddr": "hwAddr",
    "guaranteedEngineManagerCPU": "guaranteedEngineManagerCPU",
    "guaranteedReplicaManagerCPU": "guaranteedReplicaManagerCPU",
    "guaranteedInstanceManagerCPU": "guaranteedInstanceManagerCPU",
}


def _struct(*fields):
    """Construit un sous-schéma depuis des (nom JSON, type[, sous-schéma])
    recopiés des structures Go, dans leur ordre."""
    out = {}
    for spec in fields:
        json_name, ftype = spec[0], spec[1]
        sub = spec[2] if len(spec) > 2 else None
        snake = to_yaml_key(json_name)
        key = _OUTPUT_NAME.get(json_name, snake)
        aliases = tuple(sorted({snake, json_name} - {key}))
        out[key] = Field(key, ftype, sub, aliases, json_name)
    return out


# --- structures de pkg/config/config.go --------------------------------------

NETWORK_INTERFACE = _struct(
    ("name", "str"),
    ("hwAddr", "str"),
)

NETWORK = _struct(
    ("interfaces", "list[object]", NETWORK_INTERFACE),
    ("method", "str"),
    ("ip", "str"),
    ("subnetMask", "str"),
    ("gateway", "str"),
    # DefaultRoute porte `json:"-"` : absent de la configuration.
    ("bondOptions", "dict[str,str]"),
    ("mtu", "int"),
    ("vlanId", "int"),
)

HTTP_BASIC_AUTH = _struct(
    ("user", "str"),
    ("password", "str"),
)

WEBHOOK = _struct(
    ("event", "str"),
    ("method", "str"),
    ("headers", "dict[str,list[str]]"),
    ("url", "str"),
    ("payload", "str"),
    ("insecure", "bool"),
    ("basicAuth", "object", HTTP_BASIC_AUTH),
)

ADDON = _struct(
    ("enabled", "bool"),
    ("valuesContent", "str"),
)

LH_DEFAULT_SETTINGS = _struct(
    ("guaranteedEngineManagerCPU", "uint"),
    ("guaranteedReplicaManagerCPU", "uint"),
    ("guaranteedInstanceManagerCPU", "uint"),
    ("storageReservedPercentageForDefaultDisk", "uint"),
)

LONGHORN_CHART_VALUES = _struct(
    ("defaultSettings", "object", LH_DEFAULT_SETTINGS),
)

STORAGE_CLASS = _struct(
    ("replicaCount", "uint"),
)

HARVESTER_CHART_VALUES = _struct(
    ("storageClass", "object", STORAGE_CLASS),
    ("longhorn", "object", LONGHORN_CHART_VALUES),
    ("enableGoCoverDir", "bool"),
)

INSTALL = _struct(
    ("automatic", "bool"),
    ("skipchecks", "bool"),       # tout en minuscules : `skip_checks` n'existe pas
    ("mode", "str"),
    ("managementInterface", "object", NETWORK),
    ("vip", "str"),
    ("vipHwAddr", "str"),
    ("vipMode", "str"),
    ("clusterDns", "str"),
    ("clusterPodCidr", "str"),
    ("clusterServiceCidr", "str"),
    ("forceEfi", "bool"),
    ("device", "str"),
    ("configUrl", "str"),
    ("silent", "bool"),
    ("isoUrl", "str"),
    ("powerOff", "bool"),
    ("noFormat", "bool"),
    ("debug", "bool"),
    ("tty", "str"),
    ("forceGpt", "bool"),
    ("role", "str"),
    ("withNetImages", "bool"),
    ("wipeAllDisks", "bool"),
    ("wipeDisksList", "list[str]"),
    ("forceMbr", "bool"),
    ("dataDisk", "str"),
    ("webhooks", "list[object]", WEBHOOK),
    ("addons", "dict[object]", ADDON),
    ("harvester", "object", HARVESTER_CHART_VALUES),
    ("rawDiskImagePath", "str"),
    ("persistentPartitionSize", "str"),
)

FILE = _struct(
    ("encoding", "str"),
    ("content", "str"),
    ("owner", "str"),
    ("path", "str"),
    ("permissions", "str"),       # RawFilePermissions
)

SSHD_CONFIG = _struct(
    ("sftp", "bool"),
    ("disablePasswordAuth", "bool"),
)

EXTERNAL_STORAGE_CONFIG = _struct(
    ("enabled", "bool"),
    # interface{} côté Go : deux formes acceptées (liste de disques, ou
    # objet blacklist/exceptions), analysées plus tard par l'installeur.
    ("multiPathConfig", "any"),
)

OS = _struct(
    ("afterInstallChrootCommands", "list[str]"),
    ("sshAuthorizedKeys", "list[str]"),
    ("writeFiles", "list[object]", FILE),
    ("hostname", "str"),
    ("modules", "list[str]"),
    ("sysctls", "dict[str,str]"),
    ("ntpServers", "list[str]"),
    ("dnsNameservers", "list[str]"),
    ("password", "str"),
    ("environment", "dict[str,str]"),
    ("labels", "dict[str,str]"),
    ("sshd", "object", SSHD_CONFIG),
    ("persistentStatePaths", "list[str]"),
    ("externalStorageConfig", "object", EXTERNAL_STORAGE_CONFIG),
    ("additionalKernelArguments", "str"),
)

HARVESTER_CONFIG = _struct(
    ("schemeVersion", "uint"),
    ("serverUrl", "str"),
    ("token", "str"),
    ("sans", "list[str]"),
    ("os", "object", OS),
    ("install", "object", INSTALL),
    ("runtimeVersion", "str"),
    ("rancherVersion", "str"),
    ("harvesterChartVersion", "str"),
    ("monitoringChartVersion", "str"),
    ("systemSettings", "dict[str,str]"),
    ("loggingChartVersion", "str"),
    ("kubeovnChartVersion", "str"),
)

# GetSystemSettingsAllowList() : l'installeur refuse tout autre réglage
# (pkg/console/validator.go, checkSystemSettings).
SYSTEM_SETTINGS_ALLOWED = frozenset((
    "additional-ca", "api-ui-version", "cluster-registration-url",
    "server-version", "ui-index", "ui-path", "ui-source", "ui-plugin-index",
    "volume-snapshot-class", "backup-target", "upgradable-versions",
    "upgrade-checker-enabled", "upgrade-checker-url", "release-download-url",
    "log-level", "ssl-certificates", "ssl-parameters", "support-bundle-image",
    "support-bundle-namespaces", "support-bundle-timeout",
    "support-bundle-expiration", "support-bundle-node-collection-timeout",
    "default-storage-class", "http-proxy", "vm-force-reset-policy",
    "overcommit-config", "vip-pools", "auto-disk-provision-paths",
    "csi-driver-config", "containerd-registry", "storage-network",
    "default-vm-termination-grace-period-seconds", "auto-rotate-rke2-certs",
    "kubeconfig-default-token-ttl-minutes", "harvester-csi-ccm-versions",
    "ntp-servers", "additional-guest-memory-overhead-ratio",
    "csi-online-expand-validation", "ui-plugin-bundled-version",
    "upgrade-config", "longhorn-v2-data-engine-enabled", "max-hotplug-ratio",
    "vm-migration-network", "rancher-cluster", "kubevirt-migration",
    "cluster-pod-security-standard",
))

ROOT = Field("", "object", HARVESTER_CONFIG, (), "")


# --- parcours ----------------------------------------------------------------

def _resolver(struct):
    """Nom écrit -> nom de sortie, pour toutes les orthographes acceptées."""
    names = {}
    for key, f in struct.items():
        names[key] = key
        for a in f.aliases:
            names[a] = key
    return names


def _join(path, key):
    key = str(key)
    return f"{path}.{key}" if path else key


def _map_key(path, key):
    """Clé libre d'un dictionnaire (libellés, réglages) : entre crochets,
    elle peut contenir des points (`topology.kubernetes.io/zone`)."""
    return f"{path}[{key}]"


def _map_value_ok(v):
    """Valeur d'un dictionnaire de textes. L'installeur convertit tout
    scalaire en texte (NewToMap) : on n'accepte que ce dont la conversion ne
    surprend pas. Texte, et entier (`miimon: 100`). Refusés : booléen
    (`yes` ou `on` en YAML 1.1 deviennent « true » en silence) et nombre à
    virgule (`1.10` deviendrait « 1.1 »)."""
    return isinstance(v, str) or (isinstance(v, int) and not isinstance(v, bool))


def _type_ok(ftype, v):
    if ftype == "str":
        return isinstance(v, str)
    if ftype == "int":
        return isinstance(v, int) and not isinstance(v, bool)
    if ftype == "uint":
        return isinstance(v, int) and not isinstance(v, bool) and v >= 0
    if ftype == "bool":
        return isinstance(v, bool)
    if ftype == "list[str]":
        return isinstance(v, list) and all(isinstance(x, str) for x in v)
    if ftype == "any":
        return True
    return False


def _check(value, field, path, out):
    """Ajoute à `out` les couples (chemin, raison) d'une valeur."""
    t = field.type
    if t == "object":
        if not isinstance(value, dict):
            out.append((path or "(root)", "type:object"))
            return
        names = _resolver(field.sub)
        seen = {}
        for k, v in value.items():
            p = _join(path, k)
            if not isinstance(k, str) or k not in names:
                out.append((p, "unknown"))
                continue
            canon = names[k]
            if canon in seen:
                out.append((p, "duplicate"))
                continue
            seen[canon] = k
            _check(v, field.sub[canon], p, out)
        return
    if t == "list[object]":
        if not isinstance(value, list):
            out.append((path, "type:list"))
            return
        item = Field(field.key, "object", field.sub, (), field.json)
        for i, v in enumerate(value):
            _check(v, item, f"{path}[{i}]", out)
        return
    if t == "dict[object]":
        if not isinstance(value, dict):
            out.append((path, "type:dict"))
            return
        item = Field(field.key, "object", field.sub, (), field.json)
        for k, v in value.items():
            _check(v, item, _map_key(path, k), out)
        return
    if t in ("dict[str,str]", "dict[str,list[str]]"):
        if not isinstance(value, dict):
            out.append((path, "type:dict"))
            return
        for k, v in value.items():
            p = _map_key(path, k)
            if not isinstance(k, str):
                out.append((p, "type:str"))
            elif t == "dict[str,str]" and not _map_value_ok(v):
                out.append((p, "type:str"))
            elif t == "dict[str,list[str]]" and not _type_ok("list[str]", v):
                out.append((p, "type:list[str]"))
            elif path == "system_settings" and k not in SYSTEM_SETTINGS_ALLOWED:
                out.append((p, "unknown-setting"))
        return
    if not _type_ok(t, value):
        out.append((path, f"type:{t}"))
    elif path.endswith("management_interface.vlan_id") and not 0 <= value <= 4094:
        # pkg/console/validator.go : 0 veut dire « pas de VLAN »
        out.append((path, "range:0-4094"))


def check_install_config(cfg):
    """Liste des (chemin, raison) ; raison parmi `unknown`, `duplicate`,
    `unknown-setting`, `type:<attendu>`. Vide si la configuration est
    conforme au schéma."""
    out = []
    _check(cfg, ROOT, "", out)
    return out


def validate_install_config(cfg):
    """Chemins en erreur (clé inconnue, orthographe en double, mauvais
    type), vide si valide."""
    return [p for p, _ in check_install_config(cfg)]


def canonicalize(value, field=ROOT):
    """Copie de `value` où chaque clé connue prend son nom de sortie.
    Les clés inconnues sont gardées telles quelles (la validation les
    signale) ; les clés libres des dictionnaires ne sont pas touchées."""
    t = field.type
    if t == "object" and isinstance(value, dict):
        names = _resolver(field.sub)
        out = {}
        for k, v in value.items():
            canon = names.get(k) if isinstance(k, str) else None
            if canon is None:
                out[k] = v
            elif canon not in out:
                out[canon] = canonicalize(v, field.sub[canon])
        return out
    if t == "list[object]" and isinstance(value, list):
        item = Field(field.key, "object", field.sub, (), field.json)
        return [canonicalize(v, item) for v in value]
    if t == "dict[object]" and isinstance(value, dict):
        item = Field(field.key, "object", field.sub, (), field.json)
        return {k: canonicalize(v, item) for k, v in value.items()}
    return value


def field_at(path):
    """Champ du schéma désigné par un chemin pointé de noms de sortie
    (`install.management_interface.vlan_id`), ou None."""
    field = ROOT
    for part in path.split("."):
        if field.type != "object" or part not in field.sub:
            return None
        field = field.sub[part]
    return field


# =============================================================================
# Construction, fusion, sérialisation et découpage (1.77.0)
# =============================================================================

# Clés tenues par la console : refusées dans le YAML avancé. `install.mode`
# et `server_url` viennent du choix créer/rejoindre, `install.iso_url` de
# l'ISO servie par la console, `install.automatic` de la ligne de commande
# du noyau, `token` et `os.password` des champs secrets du formulaire.
RESERVED_PATHS = (
    "install.iso_url", "install.automatic", "install.mode",
    "server_url", "token", "os.password",
    # v1.78.0 : la console fait éteindre l'installeur à la fin, pour savoir
    # que l'installation est finie et démarrer elle-même sur le disque.
    "install.power_off",
)

_MAC_RE = re.compile(r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}")


class InstallConfigError(ValueError):
    """Configuration refusée. `paths` : chemins pointés en cause ;
    `reasons` : chemin -> raison (`unknown`, `reserved`, `conflict`,
    `type:<attendu>`, ...). Ni le message ni les chemins ne portent de
    valeur : un jeton ou un mot de passe n'en sort jamais."""

    def __init__(self, paths, message, reasons=None):
        super().__init__(message)
        self.paths = list(paths)
        self.message = message
        self.reasons = dict(reasons or {})


class _LiteralDumper(yaml.SafeDumper):
    """SafeDumper propre à la console : les textes multi-lignes sortent en
    bloc littéral `|` (fichiers de `write_files` lisibles). Sous-classe
    dédiée, le SafeDumper global n'est pas modifié."""


def _str_representer(dumper, value):
    if "\n" in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", value)


_LiteralDumper.add_representer(str, _str_representer)


def dump_install_config(cfg):
    """YAML de la configuration, ordre des clés conservé, sans repli des
    lignes longues (clés SSH)."""
    return yaml.dump(cfg, Dumper=_LiteralDumper, sort_keys=False,
                     default_flow_style=False, allow_unicode=True,
                     width=1 << 16)


def _split_list(text, seps=r"[,\n]"):
    if isinstance(text, (list, tuple)):
        return [str(x).strip() for x in text if str(x).strip()]
    return [x.strip() for x in re.split(seps, str(text or "")) if x.strip()]


def _as_int(value):
    """Entier depuis un champ de formulaire ; une valeur non numérique est
    laissée telle quelle pour que la validation désigne son chemin."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    s = str(value).strip()
    return int(s) if re.fullmatch(r"-?\d+", s) else s


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _parse_labels(value, errors):
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    labels = {}
    for line in str(value or "").splitlines():
        line = line.strip()
        if not line:
            continue
        k, sep, v = line.partition("=")
        if not sep or not k.strip():
            errors.append(("os.labels", "format:key=value"))
            continue
        labels[k.strip()] = v.strip()
    return labels


def build_form_config(opts, errors):
    """Dictionnaire produit par les champs du formulaire seuls, dans l'ordre
    d'écriture historique (le rendu d'une configuration d'avant 1.77.0 ne
    doit pas changer).

    Cette garantie vaut là où l'ancienne sortie était bien typée. L'ancien
    rendu écrivait certaines valeurs sans guillemets : une MAC tout en
    chiffres (`52:54:00:12:34:56`) y était lue par YAML 1.1 comme un entier
    sexagésimal, un nom d'hôte tout en chiffres comme un entier. Le nouveau
    rendu les écrit en texte, ce qu'attend l'installeur."""
    cfg = {"scheme_version": 1}
    joining = opts.get("mode") == "join"
    # Champ de PREMIER niveau (HarvesterConfig.ServerURL). Placé sous
    # `install` jusqu'en 1.44.1, il y était ignoré : un nœud ne pouvait pas
    # rejoindre un cluster.
    if joining:
        cfg["server_url"] = str(opts.get("server_url") or "")
    cfg["token"] = str(opts.get("token") or "")

    os_ = {}
    if opts.get("hostname") is not None:
        os_["hostname"] = str(opts["hostname"])
    if opts.get("password"):
        os_["password"] = str(opts["password"])
    keys = [k.strip() for k in str(opts.get("ssh_keys") or "").splitlines() if k.strip()]
    if keys:
        os_["ssh_authorized_keys"] = keys
    ntp = _split_list(opts.get("ntp"), r",")
    if ntp:
        os_["ntp_servers"] = ntp
    dns = _split_list(opts.get("dns"), r",")
    if dns:
        os_["dns_nameservers"] = dns
    labels = _parse_labels(opts.get("labels"), errors)
    if labels:
        os_["labels"] = labels
    modules = _split_list(opts.get("modules"))
    if modules:
        os_["modules"] = modules
    cfg["os"] = os_

    inst = {"mode": str(opts.get("mode") or "create")}
    if opts.get("device") is not None:
        inst["device"] = str(opts["device"])
    if opts.get("data_disk"):
        inst["data_disk"] = str(opts["data_disk"]).strip()
    if _truthy(opts.get("wipe_all_disks")):
        inst["wipe_all_disks"] = True
    # posé par le déroulé d'installation de la console, jamais par le
    # formulaire (clé réservée) : l'installeur s'éteint au lieu de redémarrer
    if opts.get("power_off") is True:
        inst["power_off"] = True
    # Disques à effacer un par un (1.78.0) : liste, ou texte séparé par
    # des virgules ou des retours à la ligne ; absent = rendu inchangé.
    wipe_list = _split_list(opts.get("wipe_disks_list"))
    if wipe_list:
        inst["wipe_disks_list"] = wipe_list
    # Obligatoire en mode automatique : l'installeur refuse la
    # configuration sans, avec « iso_url is required in automatic
    # installation », même quand l'image est déjà montée en média virtuel.
    if opts.get("iso_url"):
        inst["iso_url"] = str(opts["iso_url"])

    mi = {}
    # Désigner la carte par son ADRESSE MAC quand on l'a. Redfish ne publie
    # pas le nom que Linux donnera à l'interface (sur les XL170r les deux
    # NICs s'appellent toutes deux « System Ethernet Interface ») alors que
    # la MAC, elle, identifie sans ambiguïté et survit au renommage.
    ifaces = opts.get("mgmt_interfaces")
    ifaces = _split_list(ifaces) if ifaces else []
    if not ifaces and opts.get("mgmt_interface"):
        ifaces = [str(opts["mgmt_interface"]).strip()]
    if ifaces:
        mi["interfaces"] = [
            {"hwAddr": i.lower().replace("-", ":")} if _MAC_RE.fullmatch(i)
            else {"name": i}
            for i in ifaces]
    method = str(opts.get("method") or "dhcp")
    mi["method"] = method
    if method == "static":
        for k in ("ip", "subnet_mask", "gateway"):
            mi[k] = str(opts.get(k) or "")
    # Champ absent : valeurs historiques de la console (balance-tlb, 100).
    # Champ présent mais vide : rien d'écrit pour cette option (un fichier
    # importé dont `bond_options` n'avait pas de `mode` le reste).
    bond = {}
    mode = opts.get("bond_mode")
    if mode is None:
        bond["mode"] = "balance-tlb"
    elif str(mode).strip():
        bond["mode"] = str(mode).strip()
    miimon = opts.get("bond_miimon")
    if miimon is None:
        bond["miimon"] = 100
    elif str(miimon).strip():
        bond["miimon"] = _as_int(miimon)
    if opts.get("bond_lacp_rate"):
        bond["lacp_rate"] = str(opts["bond_lacp_rate"])
    if opts.get("bond_xmit_hash_policy"):
        bond["xmit_hash_policy"] = str(opts["bond_xmit_hash_policy"])
    mi["bond_options"] = bond
    if opts.get("vlan_id") not in (None, ""):
        mi["vlan_id"] = _as_int(opts["vlan_id"])
    inst["management_interface"] = mi
    # La VIP appartient au cluster créé, pas à un nœud qui le rejoint.
    if not joining:
        inst["vip"] = str(opts.get("vip") or "")
        inst["vip_mode"] = str(opts.get("vip_mode") or "static")
    cfg["install"] = inst
    return cfg


def _yaml_error_position(exc):
    """Ligne et colonne d'une erreur YAML, sans l'extrait de texte que
    PyYAML joint à son message (il pourrait contenir un secret)."""
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return ""
    return f" (line {mark.line + 1}, column {mark.column + 1})"


def parse_yaml_mapping(text, where):
    """Charge un texte YAML qui doit être un dictionnaire."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise InstallConfigError(
            [where], f"{where}: invalid YAML{_yaml_error_position(e)}",
            {where: "yaml"}) from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise InstallConfigError([where], f"{where}: not a YAML mapping",
                                 {where: "type:object"})
    return data


def _get_path(cfg, path):
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _merge(base, extra, path, errors):
    """Fusion profonde de `extra` dans `base`. Un chemin posé des deux
    côtés est un CONFLIT, jamais écrasé en silence ; deux dictionnaires se
    fusionnent clé par clé."""
    for k, v in extra.items():
        p = f"{path}.{k}" if path else str(k)
        if k not in base:
            base[k] = v
        elif isinstance(base[k], dict) and isinstance(v, dict):
            _merge(base[k], v, p, errors)
        else:
            errors.append((p, "conflict"))


def _raise(errors, message):
    if errors:
        paths = []
        for p, _ in errors:
            if p not in paths:
                paths.append(p)
        raise InstallConfigError(paths, message, dict(errors))


def _strip_own_disks(cfg, aliases=None):
    """Retire de `install.wipe_disks_list` les disques système et de
    données (1.78.0) : l'installeur les formate lui-même et vide une liste
    qui les contient (harvester-installer, isWipeDisksPanelNeeded). Cocher
    « effacer » sur ces disques ne sert qu'à lever le contrôle `has-data`.
    Comparaison au chemin écrit, plus `aliases` : tous les noms de ces deux
    disques dans l'inventaire de la machine, fournis par la console."""
    inst = cfg.get("install")
    if not isinstance(inst, dict) or not isinstance(inst.get("wipe_disks_list"), list):
        return
    own = {str(x).strip() for x in (inst.get("device"), inst.get("data_disk")) if x}
    own.update(str(a).strip() for a in (aliases or []) if a)
    kept = [w for w in inst["wipe_disks_list"] if str(w).strip() not in own]
    if kept:
        inst["wipe_disks_list"] = kept
    else:
        del inst["wipe_disks_list"]


def render_install_config(opts):
    """Configuration finale (dict) : formulaire + YAML avancé fusionnés et
    validés contre le schéma. Lève InstallConfigError."""
    errors = []
    cfg = build_form_config(opts, errors)
    _raise(errors, "invalid form field")

    adv_text = opts.get("advanced_yaml")
    if adv_text and str(adv_text).strip():
        adv = parse_yaml_mapping(str(adv_text), "advanced_yaml")
        errors = check_install_config(adv)
        _raise(errors, "advanced YAML does not match the installer schema")
        adv = canonicalize(adv)
        errors = [(p, "reserved") for p in RESERVED_PATHS
                  if _get_path(adv, p)[0]]
        _raise(errors, "advanced YAML sets a key owned by the console")
        _merge(cfg, adv, "", errors)
        _raise(errors, "advanced YAML sets a key already set by the form")

    _strip_own_disks(cfg, opts.get("own_disk_names"))
    errors = check_install_config(cfg)
    _raise(errors, "configuration does not match the installer schema")
    # Vu en réel (banc bmcfg, 30/09/2026) : dès que `os.ntp_servers` est
    # posé, l'installeur réécrit le réglage `ntp-servers` depuis lui
    # (updateSystemSettings, pkg/console/util.go) ; une valeur de
    # `system_settings` serait perdue sans un mot.
    if (cfg.get("os") or {}).get("ntp_servers") and \
            "ntp-servers" in (cfg.get("system_settings") or {}):
        _raise([("system_settings[ntp-servers]", "superseded")],
               "system_settings.ntp-servers is replaced by os.ntp_servers")
    return cfg


# --- découpage d'un fichier importé -----------------------------------------

def _pop(d, key):
    return d.pop(key) if isinstance(d, dict) and key in d else None


def _prune(d):
    """Retire récursivement les dictionnaires vidés par le découpage."""
    if isinstance(d, dict):
        for k in list(d):
            if isinstance(d[k], dict):
                _prune(d[k])
                if not d[k]:
                    del d[k]
    return d


def _plain_list(v, sep):
    return (isinstance(v, list) and v
            and all(isinstance(x, str) and x.strip() == x and x
                    and sep not in x and "\n" not in x for x in v))


def split_imported_config(text, known_disks=None):
    """Découpe un fichier de configuration en ce que le formulaire sait
    montrer (`form`), le reste en YAML avancé (`advanced`), les secrets
    (`secrets` : jeton, mot de passe, à garder côté serveur, jamais renvoyés
    au navigateur), des remarques (`notes`, codes) et les chemins refusés
    (`errors` ; rien n'est alors rempli).

    `known_disks` (1.78.0) : noms des disques de l'inventaire de la machine
    (chemins stables, liens, /dev/<nom>). `install.wipe_disks_list` ne passe
    au formulaire (cases « effacer » du tableau des disques) que si chacun
    de ses chemins y figure ; sinon il reste dans le YAML avancé, avec la
    remarque `wipe-list-advanced`."""
    empty = {"form": {}, "advanced": "", "secrets": {}, "notes": [], "errors": []}
    try:
        data = parse_yaml_mapping(text, "file")
    except InstallConfigError as e:
        return dict(empty, errors=e.paths)
    errors = validate_install_config(data)
    if errors:
        return dict(empty, errors=errors)
    cfg = canonicalize(data)
    form, secrets, notes = {}, {}, []

    _pop(cfg, "scheme_version")
    token = _pop(cfg, "token")
    if token:
        secrets["token"] = token
    server_url = _pop(cfg, "server_url")
    os_ = cfg.get("os") or {}
    inst = cfg.get("install") or {}
    mode = _pop(inst, "mode")
    if server_url:
        if mode == "create":
            # l'installeur refuse un `server_url` en création
            # (ErrMsgModeCreateContainsServerURL)
            return dict(empty, errors=["server_url"])
        if mode is None:
            notes.append("join-detected")
        mode = "join"
        form["server_url"] = server_url
    form["mode"] = mode or "create"
    if _pop(inst, "iso_url") is not None:
        notes.append("iso-url-replaced")
    if _pop(inst, "automatic") is not None:
        notes.append("automatic-ignored")
    if _pop(inst, "power_off") is not None:
        notes.append("power-off-ignored")

    password = _pop(os_, "password")
    if password:
        secrets["password"] = password
    if isinstance(os_.get("hostname"), str):
        form["hostname"] = os_.pop("hostname")
    keys = os_.get("ssh_authorized_keys")
    if _plain_list(keys, "\n"):
        form["ssh_keys"] = "\n".join(os_.pop("ssh_authorized_keys"))
    for key, name in (("ntp_servers", "ntp"), ("dns_nameservers", "dns")):
        if _plain_list(os_.get(key), ","):
            form[name] = ", ".join(os_.pop(key))
    labels = os_.get("labels")
    if (isinstance(labels, dict) and labels
            and all("=" not in k and "\n" not in k and k.strip() == k
                    and isinstance(v, str) and "\n" not in v and v.strip() == v
                    for k, v in labels.items())):
        form["labels"] = "\n".join(f"{k}={v}" for k, v in os_.pop("labels").items())
    if _plain_list(os_.get("modules"), ","):
        form["modules"] = ", ".join(os_.pop("modules"))

    if isinstance(inst.get("device"), str):
        form["device"] = inst.pop("device")
    if isinstance(inst.get("data_disk"), str):
        form["data_disk"] = inst.pop("data_disk")
    if inst.get("wipe_all_disks") is True:
        form["wipe_all_disks"] = inst.pop("wipe_all_disks")
    wipe_list = inst.get("wipe_disks_list")
    if wipe_list:
        known = set(known_disks or ())
        if _plain_list(wipe_list, ",") and all(w in known for w in wipe_list):
            form["wipe_disks_list"] = list(inst.pop("wipe_disks_list"))
        else:
            notes.append("wipe-list-advanced")
    if mode != "join":
        # la VIP n'est écrite par le formulaire qu'en création
        if isinstance(inst.get("vip"), str):
            form["vip"] = inst.pop("vip")
        if isinstance(inst.get("vip_mode"), str):
            form["vip_mode"] = inst.pop("vip_mode")

    mi = inst.get("management_interface") or {}
    ifaces = mi.get("interfaces")
    if isinstance(ifaces, list) and ifaces and all(
            isinstance(i, dict) and len(i) == 1
            and (isinstance(i.get("name"), str) or isinstance(i.get("hwAddr"), str))
            for i in ifaces):
        form["mgmt_interfaces"] = [i.get("hwAddr") or i.get("name")
                                   for i in mi.pop("interfaces")]
    method = mi.get("method")
    if isinstance(method, str):
        form["method"] = mi.pop("method")
        if method == "static":
            for k in ("ip", "subnet_mask", "gateway"):
                if isinstance(mi.get(k), str):
                    form[k] = mi.pop(k)
    bond = mi.get("bond_options")
    if isinstance(bond, dict):
        for k, name in (("mode", "bond_mode"), ("miimon", "bond_miimon"),
                        ("lacp_rate", "bond_lacp_rate"),
                        ("xmit_hash_policy", "bond_xmit_hash_policy")):
            if k in bond:
                form[name] = str(bond.pop(k))
            elif k in ("mode", "miimon"):
                # vide = ne pas l'écrire : le formulaire n'ajoute pas un mode
                # que le fichier ne donnait pas
                form[name] = ""
    else:
        # Sans `bond_options`, l'installeur pose active-backup et miimon 100
        # (pkg/config/cos.go, updateBond) ; le formulaire, lui, écrirait
        # balance-tlb. On rend explicite ce que l'installeur aurait fait.
        form["bond_mode"] = "active-backup"
        form["bond_miimon"] = "100"
        notes.append("bond-default-active-backup")
    if isinstance(mi.get("vlan_id"), int):
        form["vlan_id"] = str(mi.pop("vlan_id"))

    _prune(cfg)
    advanced = dump_install_config(cfg) if cfg else ""
    return {"form": form, "advanced": advanced, "secrets": secrets,
            "notes": notes, "errors": []}
