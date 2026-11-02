"""harvester-ops : déclarations Terraform qui visent un hôte KVM (2.3.0).

Une déclaration choisit sa cible : un cluster Harvester (provider
harvester/harvester) ou un hyperviseur KVM déclaré dans la console (provider
dmacvicar/libvirt, embarqué dans le livrable). Ce module fabrique le code
Terraform d'une déclaration KVM : le bloc du provider, et chaque ressource du
formulaire (VM, volume, réseau, et le modèle « VMs libvirt + RKE2 »).

Connexion : `qemu+sshcmd://<user>@<adresse>/system?command=<espace>/kvm_ssh`.
Le provider lance alors `kvm_ssh`, un petit script posé dans l'espace de la
déclaration le temps d'une exécution (0700, avec la clé de l'hôte et son
empreinte épinglée en 0600) : il appelle OpenSSH en imposant EN TÊTE
`-F /dev/null`, `StrictHostKeyChecking=yes`, l'empreinte épinglée seule
(`UserKnownHostsFile`, `GlobalKnownHostsFile=/dev/null`), la clé de l'hôte
seule (`IdentitiesOnly`), `BatchMode`, sans agent ni multiplexage. OpenSSH
garde la PREMIÈRE valeur de chaque option : rien de ce que le provider ajoute
ensuite ne peut les défaire.

Pourquoi pas `qemu+ssh://...?keyfile=&knownhosts=` (client SSH écrit en Go,
lu dans la source v0.9.9, internal/libvirt/dialers/factory.go et go-libvirt
socket/dialers/gossh.go) : vu en réel le 02/11/2026, il négocie un type de
clé d'hôte sans tenir compte de ceux épinglés et refuse alors l'hôte
(« knownhosts: key mismatch ») dès que l'empreinte épinglée n'est pas de ce
type. Et dans les deux transports, `no_verify` désactive la vérification dès
qu'il APPARAÎT, quelle que soit sa valeur (`query.Has`, `no_verify=0`
compris) : l'URI n'en porte aucun.

Toute valeur venue d'un formulaire passe par `hcl_string` (guillemets,
barres obliques, retours à la ligne, et les séquences de gabarit `${` et
`%{` neutralisées) ou par un contrôle de forme (noms, adresses, nombres)
avant d'entrer dans le code. Aucun heredoc : une valeur ne peut pas fermer
le bloc qui la contient.

Ce module ne lance ni Terraform ni ssh : il est essayé seul.
"""

import hashlib
import ipaddress
import json
import os
import re
import shlex

LIBVIRT_SOURCE = "dmacvicar/libvirt"
KEY_FILE = "kvm_ssh_key"
KNOWN_FILE = "kvm_known_hosts"
WRAPPER_FILE = "kvm_ssh"
SECRETS_DIR = ".secrets"
GIB = 1024 ** 3

# Objets libvirt créés par une déclaration : un nom simple, sans espace ni
# barre oblique (il nomme aussi des fichiers de volume sur l'hôte).
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$")
# Préfixe du modèle RKE2 : il devient le nom d'hôte des nœuds (DNS-1123).
RKE2_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,38}[a-z0-9])?$")
# Chemin d'une image de base sur l'hôte, tel que le catalogue le rend.
PATH_RE = re.compile(r"^/[A-Za-z0-9._+@/-]{1,1023}$")
BRIDGE_RE = re.compile(r"^[a-zA-Z0-9._-]{1,15}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SSH_KEY_RE = re.compile(
    r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com|"
    r"sk-ecdsa-sha2-nistp256@openssh\.com) [A-Za-z0-9+/]+={0,3}( [^\x00-\x1f\x7f]{0,200})?$")
RKE2_VERSION_RE = re.compile(r"^v1\.\d{2}\.\d{1,2}\+rke2r\d{1,2}$")
RKE2_CHANNEL_RE = re.compile(r"^(stable|latest|testing|v1\.\d{2})$")
DISK_FORMATS = ("qcow2", "raw")
NET_MODES = ("nat", "route", "isolated")
VM_NET_TYPES = ("network", "bridge")
# Sans <cpu>, libvirt donne un qemu64 sans SSE4.2 ni POPCNT : Leap 16 et
# SLES 16 (x86-64-v2) paniquent au démarrage d'init (vu en réel le 02/11/2026).
# host-model par défaut, comme la création de VM KVM de la console.
CPU_MODES = ("host-model", "host-passthrough")
MAX_SERVERS = 7
MAX_AGENTS = 20


class SpecError(ValueError):
    """Ressource incomplète ou refusée : le message est destiné à l'opérateur."""


# ---------------------------------------------------------------------------
# Échappement
# ---------------------------------------------------------------------------
def hcl_string(value):
    """Chaîne HCL entre guillemets, sans interpolation possible.

    Vérifié avec OpenTofu 1.12 : `$${` rend `${` littéral et `%%{` rend `%{`,
    un `$` seul reste un `$`. Les caractères de contrôle partent en
    séquences d'échappement : le résultat tient toujours sur une ligne."""
    out = []
    for ch in str(value):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    text = "".join(out).replace("${", "$${").replace("%{", "%%{")
    return '"' + text + '"'


def yaml_scalar(value):
    """Scalaire YAML entre guillemets doubles (JSON est un sous-ensemble de
    YAML) : une clé SSH qui porte `: ` ou `#` ne change pas la structure."""
    return json.dumps(str(value))


def safe_id(text):
    """Nom d'adresse Terraform (même règle que _tf_render_resources)."""
    return re.sub(r"[^a-z0-9_]+", "_", str(text or "").lower()).strip("_") or "res"


# ---------------------------------------------------------------------------
# Contrôles de forme
# ---------------------------------------------------------------------------
def _req(spec, key, label=None):
    v = spec.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        raise SpecError(f"{label or key} is required")
    return v.strip() if isinstance(v, str) else v


def _name(spec, key="name", regex=NAME_RE, label=None):
    v = _req(spec, key, label)
    if not isinstance(v, str) or not regex.match(v):
        raise SpecError(f"{label or key}: invalid name {v!r}")
    return v


def _opt_name(spec, key, regex=NAME_RE):
    v = spec.get(key)
    if v in (None, ""):
        return ""
    if not isinstance(v, str) or not regex.match(v.strip()):
        raise SpecError(f"{key}: invalid name {v!r}")
    return v.strip()


def _int(spec, key, default, lo, hi):
    v = spec.get(key)
    if v in (None, ""):
        v = default
    if isinstance(v, bool):
        raise SpecError(f"{key} must be a number")
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise SpecError(f"{key} must be a number") from None
    if str(n) != str(v).strip() and not isinstance(v, int):
        raise SpecError(f"{key} must be a whole number")
    if not lo <= n <= hi:
        raise SpecError(f"{key} must be between {lo} and {hi}")
    return n


def _bool(spec, key, default):
    v = spec.get(key)
    if v in (None, ""):
        return default
    if isinstance(v, bool):
        return v
    if str(v).lower() in ("true", "1", "yes", "on"):
        return True
    if str(v).lower() in ("false", "0", "no", "off"):
        return False
    raise SpecError(f"{key} must be true or false")


def _enum(spec, key, values, default):
    v = spec.get(key) or default
    if v not in values:
        raise SpecError(f"{key} must be one of {', '.join(values)}")
    return v


def _path(spec, key, required=False):
    v = spec.get(key)
    if v in (None, ""):
        if required:
            raise SpecError(f"{key} is required")
        return ""
    v = str(v).strip()
    if not PATH_RE.match(v) or "/../" in v + "/" or "/./" in v + "/":
        raise SpecError(f"{key}: invalid path on the host")
    return v


def _ssh_key(spec, key="ssh_key", required=False):
    v = spec.get(key)
    if v in (None, ""):
        if required:
            raise SpecError(f"{key} is required")
        return ""
    v = str(v).strip()
    if "\n" in v or not SSH_KEY_RE.match(v):
        raise SpecError(f"{key}: one public key in OpenSSH format (ssh-ed25519 AAAA...)")
    return v


def _ipv4(text, key):
    try:
        ip = ipaddress.ip_address(str(text).strip())
    except ValueError:
        raise SpecError(f"{key}: invalid IPv4 address") from None
    if ip.version != 4:
        raise SpecError(f"{key}: IPv4 only")
    return ip


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
URI_PATHS = {"qemu:///system": "/system", "qemu:///session": "/session"}


def connection_uri_expr(host):
    """L'expression HCL de l'URI du provider pour un hôte déclaré (dict de
    kvm_decl, déjà contrôlé). Le script `kvm_ssh` est lu dans l'espace de la
    déclaration (`abspath(path.module)`), encodé pour l'URL : aucun chemin
    réel n'est écrit dans le code. SpecError si l'hôte ne se joint pas ainsi
    (URI libvirt autre que qemu:///system ou qemu:///session)."""
    path = URI_PATHS.get(host.get("uri") or "qemu:///system")
    if not path:
        raise SpecError("this host reaches libvirt through a custom URI; Terraform declarations "
                        "need qemu:///system or qemu:///session")
    addr = str(host["address"])
    if ":" in addr:
        addr = f"[{addr}]"
    port = int(host.get("port") or 22)
    hostport = addr if port == 22 else f"{addr}:{port}"
    base = f"qemu+sshcmd://{host['user']}@{hostport}{path}"
    # pas de no_verify ni de known_hosts_verify, sous AUCUNE forme (voir
    # l'en-tête du module) ; le reste est imposé par le script kvm_ssh
    return f'"{base}?command=${{urlencode("${{abspath(path.module)}}/{WRAPPER_FILE}")}}"'


def _ssh_opt_value(path):
    # ssh découpe la valeur d'une option -o sur les espaces : on la cite
    return f'"{path}"' if re.search(r"\s", path) else path


def ssh_wrapper(ws):
    """Le script `kvm_ssh` d'un espace (chemin absolu) : OpenSSH avec les
    options strictes EN TÊTE, puis les arguments du provider. SpecError pour
    un chemin que ssh ne lirait pas tel quel (`%` développé par ssh,
    guillemet, caractère de contrôle)."""
    ws = os.path.abspath(str(ws))
    if any(c in ws for c in '%"\\') or any(ord(c) < 32 for c in ws):
        raise SpecError("the Terraform workspace path holds a character ssh would rewrite")
    opts = ["-F", "/dev/null",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "UserKnownHostsFile=" + _ssh_opt_value(os.path.join(ws, KNOWN_FILE)),
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "IdentitiesOnly=yes",
            "-o", "IdentityFile=" + _ssh_opt_value(os.path.join(ws, KEY_FILE)),
            "-o", "IdentityAgent=none",
            "-o", "BatchMode=yes",
            "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15",
            "-o", "LogLevel=ERROR",
            "-o", "ControlMaster=no",
            "-o", "ControlPath=none",
            "-o", "ForwardAgent=no",
            "-o", "ForwardX11=no",
            "-o", "ClearAllForwardings=yes",
            "-o", "PermitLocalCommand=no",
            "-o", "ProxyCommand=none"]
    return ("#!/bin/sh\n"
            "# harvester-ops : OpenSSH strict pour le provider libvirt (empreinte épinglée\n"
            "# seule, clé de l'hôte seule). Posé et retiré à chaque exécution.\n"
            "exec ssh " + " ".join(shlex.quote(o) for o in opts) + ' "$@"\n')


def header(host):
    return ("terraform {\n"
            "  required_providers {\n"
            "    libvirt = {\n"
            f'      source = "{LIBVIRT_SOURCE}"\n'
            "    }\n"
            "  }\n"
            "}\n\n"
            "# Connexion SSH avec la clé de l'hôte et son empreinte épinglée, copiées\n"
            "# par la console le temps de chaque exécution (jamais dans ce code).\n"
            'provider "libvirt" {\n'
            f"  uri = {connection_uri_expr(host)}\n"
            "}\n\n")


# ---------------------------------------------------------------------------
# Fragments
# ---------------------------------------------------------------------------
def _disk_volume(addr, name_expr, pool, base, base_format, capacity_gib, count_line=""):
    lines = [f'resource "libvirt_volume" "{addr}" {{']
    if count_line:
        lines.append(count_line)
    lines += [f"  name     = {name_expr}",
              f"  pool     = {hcl_string(pool)}",
              f"  capacity = {capacity_gib * GIB}",
              '  target = {',
              '    format = { type = "qcow2" }',
              '  }']
    if base:
        lines += ['  backing_store = {',
                  f"    path   = {hcl_string(base)}",
                  f"    format = {{ type = {hcl_string(base_format)} }}",
                  '  }']
    lines.append("}")
    return "\n".join(lines)


def _interface(net_type, net, mac_expr=None):
    src = (f"source = {{ network = {{ network = {hcl_string(net)} }} }}" if net_type == "network"
           else f"source = {{ bridge = {{ bridge = {hcl_string(net)} }} }}")
    mac = f"\n        mac    = {{ address = {mac_expr} }}" if mac_expr else ""
    return ("      {\n"
            '        model  = { type = "virtio" }\n'
            f"        {src}{mac}\n"
            "      }")


def _domain(addr, name_expr, vcpu, memory_mib, disk_ref, seed_ref, iface, running, autostart,
            count_line="", description="", cpu_mode="host-model"):
    disks = ["      {\n"
             f"        source = {{ volume = {{ pool = {disk_ref}.pool, volume = {disk_ref}.name }} }}\n"
             '        target = { bus = "virtio", dev = "vda" }\n'
             '        driver = { type = "qcow2" }\n'
             "      }"]
    if seed_ref:
        disks.append("      {\n"
                     '        device = "cdrom"\n'
                     f"        source = {{ volume = {{ pool = {seed_ref}.pool, volume = {seed_ref}.name }} }}\n"
                     '        target = { bus = "sata", dev = "sda" }\n'
                     "      }")
    lines = [f'resource "libvirt_domain" "{addr}" {{']
    if count_line:
        lines.append(count_line)
    lines += [f"  name        = {name_expr}",
              '  type        = "kvm"',
              f"  vcpu        = {vcpu}",
              f"  memory      = {memory_mib}",
              '  memory_unit = "MiB"',
              f"  running     = {'true' if running else 'false'}",
              f"  autostart   = {'true' if autostart else 'false'}",
              f"  cpu         = {{ mode = {hcl_string(cpu_mode)} }}",
              # Sans <features>, le provider ne pose ni ACPI ni APIC : sur q35
              # les périphériques PCIe ne répondent plus (disque introuvable à
              # l'initrd, carte réseau sans porteuse ; vu en réel le 02/11/2026)
              "  features    = { acpi = true, apic = {} }"]
    if description:
        lines.append(f"  description = {hcl_string(description)}")
    lines += ['  os = {',
              '    type         = "hvm"',
              '    type_arch    = "x86_64"',
              '    type_machine = "q35"',
              '  }',
              '  devices = {',
              '    disks = [',
              ",\n".join(disks),
              '    ]',
              '    interfaces = [',
              iface,
              '    ]',
              '    graphics = [',
              '      { vnc = { auto_port = true, listen = "127.0.0.1" } }',
              '    ]',
              '  }',
              "}"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Ressources
# ---------------------------------------------------------------------------
def render_vm(spec, safe):
    name = _name(spec)
    vcpu = _int(spec, "vcpu", 1, 1, 64)
    memory = _int(spec, "memory_mib", 512, 64, 1024 * 1024)
    pool = _name(spec, "pool")
    base = _path(spec, "base_image")
    base_format = _enum(spec, "base_format", DISK_FORMATS, "qcow2")
    disk_gib = _int(spec, "disk_gib", 10, 1, 65536)
    net_type = _enum(spec, "network_type", VM_NET_TYPES, "network")
    net = (_name(spec, "bridge", BRIDGE_RE) if net_type == "bridge"
           else _name(spec, "network"))
    running = _bool(spec, "running", True)
    autostart = _bool(spec, "autostart", False)
    cpu_mode = _enum(spec, "cpu_mode", CPU_MODES, "host-model")
    user_data = str(spec.get("user_data") or "")
    key = _ssh_key(spec)
    if len(user_data) > 64 * 1024:
        raise SpecError("user_data larger than 64 KiB")
    if not user_data.strip() and key:
        user_data = "#cloud-config\nssh_authorized_keys:\n  - " + yaml_scalar(key) + "\n"
    out = [_disk_volume(f"{safe}_disk", hcl_string(f"{name}-disk.qcow2"), pool, base,
                        base_format, disk_gib)]
    seed = None
    if user_data.strip():
        meta = f"instance-id: {name}\nlocal-hostname: {name}\n"
        out.append("\n".join([
            f'resource "libvirt_cloudinit_disk" "{safe}" {{',
            f"  name      = {hcl_string(name + '-seed')}",
            f"  user_data = {hcl_string(user_data)}",
            f"  meta_data = {hcl_string(meta)}",
            "}"]))
        out.append("\n".join([
            f'resource "libvirt_volume" "{safe}_seed" {{',
            f"  name = {hcl_string(name + '-seed.iso')}",
            f"  pool = {hcl_string(pool)}",
            "  create = {",
            f"    content = {{ url = libvirt_cloudinit_disk.{safe}.path }}",
            "  }",
            "}"]))
        seed = f"libvirt_volume.{safe}_seed"
    out.append(_domain(safe, hcl_string(name), vcpu, memory, f"libvirt_volume.{safe}_disk", seed,
                       _interface(net_type, net), running, autostart,
                       description=str(spec.get("description") or "")[:500], cpu_mode=cpu_mode))
    return "\n\n".join(out) + "\n"


def render_volume(spec, safe):
    name = _name(spec)
    pool = _name(spec, "pool")
    fmt = _enum(spec, "format", DISK_FORMATS, "qcow2")
    gib = _int(spec, "capacity_gib", 10, 1, 65536)
    base = _path(spec, "base_image")
    base_format = _enum(spec, "base_format", DISK_FORMATS, "qcow2")
    if base and fmt != "qcow2":
        raise SpecError("a volume on a base image must be qcow2")
    lines = [f'resource "libvirt_volume" "{safe}" {{',
             f"  name     = {hcl_string(name)}",
             f"  pool     = {hcl_string(pool)}",
             f"  capacity = {gib * GIB}",
             "  target = {",
             f"    format = {{ type = {hcl_string(fmt)} }}",
             "  }"]
    if base:
        lines += ["  backing_store = {",
                  f"    path   = {hcl_string(base)}",
                  f"    format = {{ type = {hcl_string(base_format)} }}",
                  "  }"]
    lines.append("}")
    return "\n".join(lines) + "\n"


def render_network(spec, safe):
    name = _name(spec)
    mode = _enum(spec, "mode", NET_MODES, "nat")
    autostart = _bool(spec, "autostart", True)
    addr = str(spec.get("address") or "").strip()
    lines = [f'resource "libvirt_network" "{safe}" {{',
             f"  name      = {hcl_string(name)}",
             f"  autostart = {'true' if autostart else 'false'}"]
    if mode != "isolated":
        lines.append(f"  forward = {{ mode = {hcl_string(mode)} }}")
    if addr:
        gw = _ipv4(addr, "address")
        prefix = _int(spec, "prefix", 24, 8, 30)
        net = ipaddress.ip_network(f"{gw}/{prefix}", strict=False)
        if gw in (net.network_address, net.broadcast_address):
            raise SpecError("address must be a host address of its subnet")
        ip = [f"      address = {hcl_string(gw)}", f"      prefix  = {prefix}"]
        if _bool(spec, "dhcp", True):
            start = spec.get("dhcp_start") or str(net.network_address + 10)
            end = spec.get("dhcp_end") or str(net.broadcast_address - 1)
            s, e = _ipv4(start, "dhcp_start"), _ipv4(end, "dhcp_end")
            if s not in net or e not in net or int(s) > int(e) or gw in (s, e):
                raise SpecError("the DHCP range must sit in the subnet, in order, without the address")
            ip.append(f"      dhcp = {{ ranges = [{{ start = {hcl_string(s)}, end = {hcl_string(e)} }}] }}")
        lines += ["  ips = [", "    {", "\n".join(ip), "    }", "  ]"]
    elif mode != "isolated":
        raise SpecError("a nat or route network needs an address")
    lines.append("}")
    return "\n".join(lines) + "\n"


def rke2_token_var(safe):
    return f"{safe}_rke2_token"


def _mac(seed, index):
    h = hashlib.sha256(f"{seed}:{index}".encode()).hexdigest()
    return "52:54:00:%s:%s:%s" % (h[0:2], h[2:4], h[4:6])


def rke2_nodes(spec, seed=""):
    """Les nœuds du modèle : [{name, role, ip, mac}] (serveurs d'abord),
    adresses tirées dans l'ordre à partir de `address_start`."""
    prefix_name = _name(spec, "name", RKE2_NAME_RE)
    servers = _int(spec, "servers", 1, 1, MAX_SERVERS)
    if servers % 2 == 0:
        raise SpecError("servers must be odd (1, 3, 5, 7): etcd needs a majority")
    agents = _int(spec, "agents", 0, 0, MAX_AGENTS)
    start = _ipv4(_req(spec, "address_start"), "address_start")
    prefix = _int(spec, "prefix", 24, 8, 30)
    gw = _ipv4(_req(spec, "gateway"), "gateway")
    net = ipaddress.ip_network(f"{start}/{prefix}", strict=False)
    if gw not in net:
        raise SpecError("gateway is outside the subnet of address_start")
    nodes = []
    for i in range(servers + agents):
        ip = start + i
        if ip not in net or ip in (net.network_address, net.broadcast_address):
            raise SpecError("the node addresses run past the end of the subnet")
        if ip == gw:
            raise SpecError("the node addresses include the gateway")
        role = "server" if i < servers else "agent"
        n = i if role == "server" else i - servers
        nodes.append({"name": f"{prefix_name}-{role}-{n}", "role": role, "ip": str(ip),
                      "mac": _mac(seed or prefix_name, i)})
    return nodes


TOKEN_MARK = "\x00TOKEN\x00"


def rke2_user_data(spec, node, first_server_ip):
    """Le cloud-init d'un nœud, avec TOKEN_MARK à la place du jeton."""
    user = _req(spec, "ssh_user") if spec.get("ssh_user") else "rke2"
    if not USER_RE.match(user):
        raise SpecError("ssh_user: invalid user name")
    key = _ssh_key(spec, required=True)
    version = str(spec.get("rke2_version") or "").strip()
    channel = str(spec.get("rke2_channel") or "stable").strip()
    if version and not RKE2_VERSION_RE.match(version):
        raise SpecError("rke2_version: expected v1.NN.N+rke2rN")
    if not version and not RKE2_CHANNEL_RE.match(channel):
        raise SpecError("rke2_channel: stable, latest, testing or v1.NN")
    pick = f"INSTALL_RKE2_VERSION={version}" if version else f"INSTALL_RKE2_CHANNEL={channel}"
    kind = "server" if node["role"] == "server" else "agent"
    cfg = [f"      token: {TOKEN_MARK}",
           f"      node-ip: {node['ip']}"]
    if kind == "server":
        cfg += ["      tls-san:", f"        - {node['ip']}", '      write-kubeconfig-mode: "0600"']
    if node["ip"] != first_server_ip:
        cfg.append(f"      server: https://{first_server_ip}:9345")
    install = (f"curl -sfL https://get.rke2.io | INSTALL_RKE2_METHOD=tar {pick}"
               + (" INSTALL_RKE2_TYPE=agent" if kind == "agent" else "") + " sh -")
    return "\n".join([
        "#cloud-config",
        f"hostname: {node['name']}",
        "users:",
        "  - default",
        f"  - name: {user}",
        '    sudo: "ALL=(ALL) NOPASSWD:ALL"',
        "    shell: /bin/bash",
        "    ssh_authorized_keys:",
        f"      - {yaml_scalar(key)}",
        "write_files:",
        "  - path: /etc/rancher/rke2/config.yaml",
        '    permissions: "0600"',
        "    owner: root:root",
        "    content: |",
        *cfg,
        "runcmd:",
        f"  - [sh, -c, {yaml_scalar(install)}]",
        f"  - [systemctl, enable, --now, rke2-{kind}.service]",
        ""])


def rke2_network_config(spec, node):
    dns = [x.strip() for x in str(spec.get("dns") or spec.get("gateway") or "").split(",") if x.strip()]
    if not dns or len(dns) > 3:
        raise SpecError("dns: one to three IPv4 addresses, comma separated")
    dns = [str(_ipv4(d, "dns")) for d in dns]
    prefix = _int(spec, "prefix", 24, 8, 30)
    gw = str(_ipv4(spec.get("gateway"), "gateway"))
    return "\n".join([
        "version: 2",
        "ethernets:",
        "  nic0:",
        "    match:",
        f'      macaddress: "{node["mac"]}"',
        f"    addresses: [{node['ip']}/{prefix}]",
        "    routes:",
        f"      - {{to: 0.0.0.0/0, via: {gw}}}",
        "    nameservers:",
        f"      addresses: [{', '.join(dns)}]",
        ""])


def render_rke2(spec, safe, seed=""):
    nodes = rke2_nodes(spec, seed)
    pool = _name(spec, "pool")
    base = _path(spec, "base_image", required=True)
    base_format = _enum(spec, "base_format", DISK_FORMATS, "qcow2")
    net = _name(spec, "network")
    vcpu = _int(spec, "vcpu", 2, 1, 64)
    memory = _int(spec, "memory_mib", 4096, 2048, 1024 * 1024)
    disk = _int(spec, "disk_gib", 30, 10, 65536)
    cpu_mode = _enum(spec, "cpu_mode", CPU_MODES, "host-model")
    first = nodes[0]["ip"]
    var = rke2_token_var(safe)
    loc = f"local.{safe}_nodes"
    entries = []
    for n in nodes:
        ud_a, ud_b = rke2_user_data(spec, n, first).split(TOKEN_MARK)
        entries.append("    {\n"
                       f"      name    = {hcl_string(n['name'])}\n"
                       f"      role    = {hcl_string(n['role'])}\n"
                       f"      ip      = {hcl_string(n['ip'])}\n"
                       f"      mac     = {hcl_string(n['mac'])}\n"
                       f"      ud_a    = {hcl_string(ud_a)}\n"
                       f"      ud_b    = {hcl_string(ud_b)}\n"
                       f"      network = {hcl_string(rke2_network_config(spec, n))}\n"
                       "    }")
    user = spec.get("ssh_user") or "rke2"
    blocks = [
        "\n".join([f'variable "{var}" {{',
                   "  description = \"Jeton RKE2, tiré par la console et gardé dans l'espace de la "
                   "déclaration ; jamais affiché.\"",
                   "  type        = string",
                   "  sensitive   = true",
                   "}"]),
        "locals {\n" + f"  {safe}_nodes = [\n" + ",\n".join(entries) + "\n  ]\n}",
        _disk_volume(f"{safe}_disk", f'"${{{loc}[count.index].name}}-disk.qcow2"', pool, base,
                     base_format, disk, count_line=f"  count    = length({loc})"),
        "\n".join([f'resource "libvirt_cloudinit_disk" "{safe}" {{',
                   f"  count          = length({loc})",
                   f'  name           = "${{{loc}[count.index].name}}-seed"',
                   f'  user_data      = "${{{loc}[count.index].ud_a}}${{var.{var}}}${{{loc}[count.index].ud_b}}"',
                   f'  meta_data      = "instance-id: ${{{loc}[count.index].name}}\\nlocal-hostname: '
                   f'${{{loc}[count.index].name}}\\n"',
                   f"  network_config = {loc}[count.index].network",
                   "}"]),
        "\n".join([f'resource "libvirt_volume" "{safe}_seed" {{',
                   f"  count = length({loc})",
                   f'  name  = "${{{loc}[count.index].name}}-seed.iso"',
                   f"  pool  = {hcl_string(pool)}",
                   "  create = {",
                   f"    content = {{ url = libvirt_cloudinit_disk.{safe}[count.index].path }}",
                   "  }",
                   "}"]),
        _domain(safe, f"{loc}[count.index].name", vcpu, memory, f"libvirt_volume.{safe}_disk[count.index]",
                f"libvirt_volume.{safe}_seed[count.index]",
                _interface("network", net, mac_expr=f"{loc}[count.index].mac"), True, False,
                count_line=f"  count       = length({loc})", cpu_mode=cpu_mode),
        "\n".join([f'output "{safe}_nodes" {{',
                   "  description = \"Adresse de chaque nœud.\"",
                   f"  value       = {{ for n in {loc} : n.name => n.ip }}",
                   "}"]),
        "\n".join([f'output "{safe}_kubeconfig" {{',
                   "  description = \"Comment lire le kubeconfig du premier serveur.\"",
                   "  value       = " + hcl_string(
                       f"ssh {user}@{first} sudo cat /etc/rancher/rke2/rke2.yaml "
                       f"| sed 's/127.0.0.1/{first}/' > kubeconfig  (network private to the host: "
                       f"add -J <user>@<hypervisor>)"),
                   "}"]),
    ]
    return "\n\n".join(blocks) + "\n"


RENDERERS = {"kvm_vm": render_vm, "kvm_volume": render_volume, "kvm_network": render_network}


def render(kind, spec, safe, seed=""):
    """Le code d'une ressource KVM (sans le bloc du provider). SpecError
    pour une ressource incomplète ou une valeur refusée."""
    if not isinstance(spec, dict):
        raise SpecError("spec must be an object")
    if kind == "kvm_rke2":
        return render_rke2(spec, safe, seed)
    fn = RENDERERS.get(kind)
    if fn is None:
        raise SpecError(f"unknown KVM resource kind {kind!r}")
    return fn(spec, safe)


def addresses(kind, safe, spec=None):
    """Les adresses Terraform que produit une ressource, la principale en
    tête (celle qui dit « déployée »)."""
    if kind == "kvm_vm":
        out = [f"libvirt_domain.{safe}", f"libvirt_volume.{safe}_disk"]
        s = spec or {}
        if str(s.get("user_data") or "").strip() or str(s.get("ssh_key") or "").strip():
            out += [f"libvirt_cloudinit_disk.{safe}", f"libvirt_volume.{safe}_seed"]
        return out
    if kind == "kvm_volume":
        return [f"libvirt_volume.{safe}"]
    if kind == "kvm_network":
        return [f"libvirt_network.{safe}"]
    if kind == "kvm_rke2":
        return [f"libvirt_domain.{safe}", f"libvirt_volume.{safe}_disk",
                f"libvirt_cloudinit_disk.{safe}", f"libvirt_volume.{safe}_seed"]
    return []


def token_vars(rendered):
    """Les variables de jeton RKE2 d'une déclaration rendue
    [(nom_sûr, kind, ...)]."""
    return [rke2_token_var(r[0]) for r in rendered if r[1] == "kvm_rke2"]
