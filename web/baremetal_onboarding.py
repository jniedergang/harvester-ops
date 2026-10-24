"""Opt-in storage profile planning. No cluster or host writes."""
import copy
import ipaddress
from pathlib import Path
import re
import uuid

import yaml

PROFILE_DIR = Path(__file__).parent / 'storage_profiles'
TAINT = {'key': 'harvester-ops.io/onboarding', 'value': 'pending', 'effect': 'NoSchedule'}
LM = 'linkmonitors.network.harvesterhci.io'
VC = 'vlanconfigs.network.harvesterhci.io'
LABEL = 'harvester-ops.io/storage-profile'
# Seule source de la clé d'identité d'installation (label posé par
# l'installeur, comparé avant toute écriture sur le nœud).
INSTALL_ID_LABEL = 'harvester-ops.io/install-id'
# harvester/harvester v1.9.0, pkg/util/constants.go
# (HarvesterPromoteStatusAnnotationKey) et pkg/util/node.go (PromoteStatus*).
PROMOTE_STATUS = 'harvesterhci.io/promote-status'
PROMOTE_RUNNING, PROMOTE_COMPLETE = 'running', 'complete'
# Cordon natif de l'installeur (harvester/harvester v1.9.0, backport #11301,
# 85064f4fee) : `install.cordoned: true` (pkg/installer/config/config.go,
# Install.Cordoned) fait poser ce label par rancherd à chaque démarrage de
# rke2 (templates/rancherd-config.yaml) ; le contrôleur de promotion
# (pkg/controller/master/node/promote_controller.go, OnNodeChanged et
# reconcileInstallCordonedNode) cordonne alors le nœud une seule fois, sauf
# s'il est le dernier disponible, et pose l'annotation de traitement.
# Personne ne lève ce cordon ; selectPromoteNode écarte un nœud cordonné.
# Clés : pkg/util/constants.go (HarvesterInstallCordonedLabel,
# HarvesterInstallCordonedProcessedAnnotation).
NATIVE_CORDON_LABEL = 'harvesterhci.io/install-cordoned'
NATIVE_CORDON_DONE = 'harvesterhci.io/install-cordoned-processed'
# Première version publiée qui le porte (absent de v1.8.2 et de la branche
# v1.8 ; les préversions de v1.9.0 gardent le mécanisme de la console).
NATIVE_CORDON_SINCE = (1, 9, 0)
# Répertoire volatile (tmpfs) où le script de démarrage range le nom noyau
# résolu de chaque carte de stockage, à partir de sa MAC.
NIC_DIR = '/run/harvester-ops/storage-nics'
# Étapes yip admises dans un fichier OEM de profil. Le fichier est écrit par
# l'étape initramfs du système installé : ses propres étapes rootfs et
# initramfs ne tourneraient pas au premier démarrage.
STAGES = ('fs', 'boot', 'network')

_MAC_RE = re.compile(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}')
_MP_TEXT_RE = re.compile(r'[A-Za-z0-9 ._:/()+-]{1,64}')
_MP_WWID_RE = re.compile(r'[A-Za-z0-9^$.*_:+-]{1,128}')
# Clés de MultiPathOption2 (harvester/harvester v1.9.0,
# pkg/installer/config/config.go), lues par json.Unmarshal : camelCase exigé.
_MP_DEVICE_KEYS = ('blacklist', 'blacklistExceptions')
_MP_WWID_KEYS = ('blacklistWwids', 'blacklistExceptionWwids')


class ProfileError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ProfileError(message)


def _check_multipath(value):
    require(value is None or isinstance(value, dict) and value
            and set(value) <= set(_MP_DEVICE_KEYS + _MP_WWID_KEYS), 'invalid profile multipath')
    for key in _MP_DEVICE_KEYS:
        devices = (value or {}).get(key, [])
        require(isinstance(devices, list) and all(
            isinstance(d, dict) and set(d) == {'vendor', 'product'}
            and all(isinstance(v, str) and _MP_TEXT_RE.fullmatch(v) for v in d.values())
            for d in devices), 'invalid profile multipath ' + key)
    for key in _MP_WWID_KEYS:
        wwids = (value or {}).get(key, [])
        require(isinstance(wwids, list) and all(isinstance(w, str) and _MP_WWID_RE.fullmatch(w)
                                                for w in wwids), 'invalid profile multipath ' + key)


def _check_files(root, spec):
    require('profile.yaml' not in spec['files'] and all(
        (root / f).is_file() and (root / f).resolve().parent == root for f in spec['files']),
        'profile OEM files are unavailable')
    for filename in spec['files']:
        try:
            doc = yaml.safe_load((root / filename).read_text())
        except (OSError, yaml.YAMLError):
            raise ProfileError('invalid profile OEM file') from None
        stages = doc.get('stages') if isinstance(doc, dict) else None
        require(isinstance(stages, dict) and set(stages) <= set(STAGES)
                and all(isinstance(v, list) for v in stages.values()),
                'profile OEM files may only use the fs, boot and network stages')
        for stage, entries in stages.items():
            text = yaml.safe_dump(entries)
            # Le nom résolu n'existe qu'une fois l'étape network commencée.
            require(stage == 'network' or '{{' not in text,
                    'NIC placeholders are only allowed in the network stage')
            require(set(re.findall(r'\{\{([^}]*)\}\}', text)) <= set(spec['ports']),
                    'profile OEM files refer to an undeclared port')


def definition(profile_id):
    """Load only administrator-reviewed, packaged profiles; no caller paths or scripts."""
    require(isinstance(profile_id, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', profile_id),
            'invalid storage profile')
    root = (PROFILE_DIR / profile_id).resolve()
    require(root.parent == PROFILE_DIR.resolve(), 'invalid storage profile directory')
    try:
        spec = yaml.safe_load((root / 'profile.yaml').read_text())
    except (OSError, yaml.YAMLError):
        raise ProfileError('storage profile is unavailable') from None
    expected = {'label', 'description', 'ports', 'files', 'boot_disk_exclusion', 'multipath',
                'modules', 'services', 'iscsi_ifaces', 'csi_node'}
    require(isinstance(spec, dict) and set(spec) == expected, 'invalid packaged profile manifest')
    require(all(isinstance(spec[k], str) and spec[k] for k in ('label', 'description')),
            'invalid profile description')
    require(isinstance(spec['ports'], dict) and 1 <= len(spec['ports']) <= 16
            and all(isinstance(k, str) and re.fullmatch(r'[a-z][a-z0-9]{0,15}', k)
                    and isinstance(v, str) and v for k, v in spec['ports'].items()),
            'invalid profile ports')
    _check_multipath(spec['multipath'])
    require(isinstance(spec['boot_disk_exclusion'], bool)
            and (not spec['boot_disk_exclusion'] or spec['multipath'] is not None),
            'invalid boot disk policy')
    for field, pattern in [('files', r'[A-Za-z0-9_-]+\.yaml'), ('modules', r'[a-z][a-z0-9_]*'),
                           ('services', r'[A-Za-z0-9][A-Za-z0-9_.@-]*')]:
        values = spec[field]
        require(isinstance(values, list)
                and all(isinstance(v, str) and re.fullmatch(pattern, v) for v in values)
                and len(values) == len(set(values)), 'invalid profile ' + field)
    _check_files(root, spec)
    require(isinstance(spec['iscsi_ifaces'], dict) and all(
        k in spec['ports'] and isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_-]+', v)
        for k, v in spec['iscsi_ifaces'].items()), 'invalid iSCSI bindings')
    csi = spec['csi_node']
    require(csi is None or isinstance(csi, dict) and set(csi) == {'namespace', 'daemonset'} and all(
        isinstance(v, str) and re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}', v) for v in csi.values()),
        'invalid CSI DaemonSet reference')
    return spec


def options():
    out = []
    for path in sorted(PROFILE_DIR.glob('*/profile.yaml')):
        spec = definition(path.parent.name)
        keys = ('label', 'description', 'ports', 'boot_disk_exclusion')
        out.append({'id': path.parent.name, **{k: spec[k] for k in keys}})
    return out


def native_cordon(version):
    """`version` : résultat de hv_upgrade.parse_version (tuple, « dev » ou
    None). Vrai seulement pour une version publiée >= v1.9.0 ou une
    préversion d'une version ultérieure ; illisible ou dev : non."""
    if not isinstance(version, tuple):
        return False
    return version[:3] > NATIVE_CORDON_SINCE or (version[:3] == NATIVE_CORDON_SINCE
                                                  and not version[3])


def selected(data):
    return data.get('onboarding') not in (None, False, {})


def selection(data, cfg):
    value = data.get('onboarding')
    require(cfg.get('install', {}).get('role') != 'witness',
            'storage onboarding is not supported for witness nodes')
    require(isinstance(value, dict), 'onboarding must be an object')
    require(data.get('mode') == 'join', 'onboarding is available only for a declared cluster join')
    require(set(value) == {'profile', 'boot_wwid', 'ports'},
            'unexpected or missing onboarding fields')
    spec = definition(value['profile'])
    require(isinstance(value['boot_wwid'], str) and (
        bool(re.fullmatch(r'[235][0-9a-f]{16,64}', value['boot_wwid']))
        if spec['boot_disk_exclusion'] else value['boot_wwid'] == ''),
        'enter the local boot disk SCSI WWID only when required by the profile')
    ports = value['ports']
    require(isinstance(ports, dict) and set(ports) == set(spec['ports']),
            'configure every port of the selected profile')
    mgmt = cfg['install']['management_interface']['interfaces']
    management = {p.get('hwAddr', '').lower() for p in mgmt}
    require(bool(management) and all(_MAC_RE.fullmatch(m) for m in management),
            'management NICs must use MAC addresses with onboarding')
    require(data.get('vm_network') is None or isinstance(data['vm_network'], dict),
            'VM network must be an object')
    vm_macs = (data.get('vm_network') or {}).get('macs') or []
    require(isinstance(vm_macs, list) and len(vm_macs) <= 16, 'select VM uplink MAC addresses')
    out = copy.deepcopy(value)
    macs, addresses = [], []
    for role in spec['ports']:
        port = ports[role]
        require(isinstance(port, dict) and set(port) == {'mac', 'address'},
                'each storage port needs a MAC and an IPv4/CIDR address')
        mac, address = port['mac'], port['address']
        try:
            interface = ipaddress.IPv4Interface(address)
        except (ValueError, TypeError):
            raise ProfileError('storage addresses must be explicit IPv4/CIDR values') from None
        # Écarter l'adresse de réseau et de diffusion écarte aussi /31 et /32,
        # où l'hôte est forcément l'une des deux.
        require(str(interface) == address
                and interface.ip not in (interface.network.network_address,
                                         interface.network.broadcast_address)
                and not interface.ip.is_loopback and not interface.ip.is_multicast
                and not interface.ip.is_unspecified, 'invalid storage host address')
        out['ports'][role]['address'] = str(interface)
        addresses.append(str(interface.ip))
        macs.append(mac)
    require(len(set(addresses)) == len(addresses), 'storage IP addresses must be distinct')
    all_macs = macs + vm_macs
    require(all(isinstance(m, str) and _MAC_RE.fullmatch(m) for m in all_macs),
            'invalid NIC MAC address')
    all_macs = [m.lower() for m in all_macs]
    require(all(m != '00:00:00:00:00:00' and not int(m[:2], 16) & 1 for m in all_macs),
            'NIC MAC addresses must be unicast')
    require(len(set(all_macs)) == len(all_macs) and not set(all_macs) & management,
            'management, VM and storage NICs must not overlap')
    for i, role in enumerate(spec['ports']):
        out['ports'][role]['mac'] = all_macs[i]
    return out


def nic_ref(role):
    """Nom noyau résolu au démarrage, tel qu'un script shell le lit."""
    return '$(cat ' + NIC_DIR + '/' + role + ')'


def _resolve_script(choice):
    """Résout chaque MAC de stockage en nom noyau : une seule carte physique
    (lien `device` dans sysfs), non asservie à un bond ou un pont. Aucune
    hypothèse sur le préfixe du nom (ens, enp, eth, eno, p1p1...)."""
    lines = ['mkdir -p ' + NIC_DIR,
             'nic_by_mac() {',
             '  found=""',
             '  for dev in /sys/class/net/*; do',
             '    [ -e "$dev/device" ] || continue',
             '    [ -e "$dev/master" ] && continue',
             '    [ "$(cat "$dev/address")" = "$1" ] || continue',
             '    [ -z "$found" ] || return 1',
             '    found=${dev##*/}',
             '  done',
             '  [ -n "$found" ] && echo "$found"',
             '}']
    for role in choice['ports']:
        mac = choice['ports'][role]['mac']
        lines.append('nic=$(nic_by_mac ' + mac + ') || { echo "no unique physical NIC for '
                     + mac + '" >&2; exit 1; }')
        lines.append('echo "$nic" > ' + NIC_DIR + '/' + role)
    return lines


def oem(choice, spec=None):
    """Render a reviewed profile with explicit host addresses and identities."""
    spec = spec or definition(choice['profile'])
    stages = {}
    base = PROFILE_DIR / choice['profile']
    for filename in spec['files']:
        text = (base / filename).read_text()
        for role in choice['ports']:
            text = text.replace('{{' + role + '}}', nic_ref(role))
        for stage, entries in yaml.safe_load(text)['stages'].items():
            stages.setdefault(stage, []).extend(entries)
    connections, commands = [], ['set -eu']
    if spec['boot_disk_exclusion']:
        commands.extend([
            'boot_source=$(findmnt -n -o SOURCE /oem)',
            'boot_parent=$(lsblk -n -o PKNAME "$boot_source")',
            'test -n "$boot_parent"',
            'test "$(/usr/lib/udev/scsi_id --whitelisted --device=/dev/$boot_parent)" = "'
            + choice['boot_wwid'] + '"'])
    # Toutes les MAC sont résolues avant d'activer la moindre connexion.
    commands.extend(_resolve_script(choice))
    commands.append('systemctl start NetworkManager')
    for role in choice['ports']:
        p = choice['ports'][role]
        connection_id = 'harvester-ops-storage-' + role
        connection_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, connection_id + '/' + p['mac']))
        path = '/etc/NetworkManager/system-connections/' + connection_id + '.nmconnection'
        # Liée à la MAC seule (pas d'interface-name) : un renommage de la
        # carte ne détourne pas la connexion vers une autre.
        text = ('[connection]\nid=' + connection_id + '\nuuid=' + connection_uuid
                + '\ntype=ethernet\nautoconnect=true\nautoconnect-priority=100\n'
                + '[ethernet]\nmac-address=' + p['mac'] + '\n'
                + '[ipv4]\nmethod=manual\naddress1=' + p['address'] + '\nnever-default=true\n'
                + '[ipv6]\nmethod=disabled\n')
        connections.append({'path': path, 'permissions': 0o600, 'content': text})
        commands.extend(['nmcli connection load ' + path,
                         'nmcli --wait 30 connection up uuid ' + connection_uuid])
    stages.setdefault('boot', []).append({'name': 'Persist storage network connections',
                                          'files': connections})
    stages.setdefault('network', []).insert(0, {
        'name': 'Resolve storage NICs by MAC and activate storage networks',
        'commands': ['\n'.join(commands)]})
    completion = ['set -eu']
    if spec['boot_disk_exclusion']:
        completion.append("grep -Fq 'wwid \"^" + choice['boot_wwid'] + "$\"' /etc/multipath.conf")
    completion.extend('test -d /sys/module/' + module for module in spec['modules'])
    completion.extend('systemctl is-active --quiet ' + service for service in spec['services'])
    for role in choice['ports']:
        port = choice['ports'][role]
        completion.append('ip -o -4 addr show dev "' + nic_ref(role) + '" | grep -Fq " inet '
                          + port['address'] + ' "')
    for role, iface in spec['iscsi_ifaces'].items():
        completion.append('iscsiadm -m iface -I ' + iface + ' | grep -Eq "^iface.net_ifacename = '
                          + nic_ref(role) + '$"')
    completion.extend([
        'mkdir -p /etc/rancher/rke2/config.yaml.d',
        "printf 'node-label+:\\n  - " + LABEL + '=' + choice['profile']
        + "\\n' > /etc/rancher/rke2/config.yaml.d/99-harvester-ops-storage-ready.yaml"])
    stages['network'].append({
        'name': 'Verify active storage prerequisites before advertising completion',
        'commands': ['\n'.join(completion)]})
    return yaml.safe_dump({'name': 'Storage profile: ' + choice['profile'], 'stages': stages},
                          sort_keys=False)


def augment(cfg, choice, spec=None, native=False):
    """`native` : le cluster rejoint applique `install.cordoned` (v1.9.0 et
    plus) ; sinon la console pose une taint d'enregistrement et cordonne le
    nœud elle-même."""
    spec = spec or definition(choice['profile'])
    cfg = copy.deepcopy(cfg)
    os_cfg = cfg.setdefault('os', {})
    install = cfg.setdefault('install', {})
    require(install.get('cordoned') in (None, native),
            'advanced YAML sets install.cordoned; the storage profile owns it')
    require(INSTALL_ID_LABEL not in os_cfg.get('labels', {}), 'install identity label is reserved')
    require(LABEL not in os_cfg.get('labels', {}), 'storage completion label is reserved')
    files = os_cfg.setdefault('write_files', [])
    # L'étape initramfs du système installé réécrit ces fichiers à CHAQUE
    # démarrage. Le fichier de taint n'agit qu'à l'enregistrement du nœud :
    # kubelet n'applique --register-with-taints qu'à la création de l'objet
    # Node, un redémarrage ultérieur ne remet pas la taint.
    generated = [
        {'path': '/oem/90-harvester-ops-storage.yaml', 'permissions': '0600',
         'content': oem(choice, spec)},
        {'path': '/etc/rancher/rke2/config.yaml.d/99-harvester-ops-onboarding.yaml',
         'permissions': '0600',
         'content': 'node-taint+:\n  - ' + TAINT['key'] + '=' + TAINT['value'] + ':'
                    + TAINT['effect'] + '\n'},
    ]
    if native:
        # Le cordon natif suffit : kubelet enregistre le nœud sans le label,
        # rke2 le pose en initialisant le nœud, qui garde jusque-là la taint
        # node.cloudprovider.kubernetes.io/uninitialized ; vu sur le banc
        # (deux jonctions), le cordon arrive dans la même seconde que le
        # retrait de cette taint, 17 à 18 s après l'enregistrement.
        install['cordoned'] = True
        generated.pop()
    require(not any(f.get('path') in {g['path'] for g in generated} for f in files),
            'advanced YAML conflicts with onboarding files')
    files.extend(generated)
    args = os_cfg.get('additional_kernel_arguments', '').split()
    require(not any(a.startswith('systemd.debug_shell') for a in args),
            'debug shell must not be persisted in the installed system')
    # Modules chargés par l'installeur lui-même (os.modules : modprobe dans
    # son étape initramfs, à chaque démarrage).
    modules = list(os_cfg.get('modules') or [])
    os_cfg['modules'] = modules + [m for m in spec['modules'] if m not in modules]
    if not os_cfg['modules']:
        del os_cfg['modules']
    if spec['multipath'] is not None:
        # Multipath par la voie de Harvester : os.external_storage_config
        # active multipathd et rend /etc/multipath.conf (setupExternalStorage,
        # pkg/installer/config/cos.go, harvester/harvester v1.9.0) ; l'installeur
        # n'ajoute alors plus multipath=off (pkg/installer/console/util.go).
        require('external_storage_config' not in os_cfg,
                'advanced YAML already sets os.external_storage_config; the profile owns it')
        require('multipath=off' not in args,
                'multipath=off in the kernel arguments conflicts with the profile multipath')
        config = copy.deepcopy(spec['multipath'])
        if spec['boot_disk_exclusion']:
            config['blacklistWwids'] = ['^' + choice['boot_wwid'] + '$'] + config.get(
                'blacklistWwids', [])
        os_cfg['external_storage_config'] = {'enabled': True, 'multi_path_config': config}
    return cfg


def guard_patch(node):
    ops = [{'op': 'test', 'path': '/metadata/uid', 'value': node['metadata']['uid']},
           {'op': 'test', 'path': '/metadata/resourceVersion',
            'value': node['metadata']['resourceVersion']},
           {'op': 'add', 'path': '/spec/unschedulable', 'value': True}]
    taints = node['spec'].get('taints', [])
    remaining = [t for t in taints if not all(t.get(k) == v for k, v in TAINT.items())]
    if remaining != taints:
        ops.append({'op': 'replace', 'path': '/spec/taints', 'value': remaining})
    return ops


def promote_status(node):
    return (node.get('metadata', {}).get('annotations') or {}).get(PROMOTE_STATUS)


def readiness(node, longhorn, pods, choice, csi_node):
    """`pods` : ceux du namespace CSI déjà filtrés côté serveur sur ce nœud ;
    `csi_node` : référence du profil, lue une fois par exécution."""
    host = node['metadata']['name']
    conditions = {c['type']: c['status']
                  for c in ((node.get('status') or {}).get('conditions') or [])}
    disks = (((longhorn or {}).get('status') or {}).get('diskStatus') or {}).values()
    disk = any(any(c.get('type') == 'Ready' and c.get('status') == 'True'
                   for c in (d.get('conditions') or []))
               for d in disks if d.get('diskPath') == '/var/lib/harvester/defaultdisk')
    result = {'node': conditions.get('Ready') == 'True',
              'cordoned': bool(node['spec'].get('unschedulable')),
              'local_disk': disk,
              'storage_boot_setup':
                  node['metadata'].get('labels', {}).get(LABEL) == choice['profile'],
              'pressure_clear': all(conditions.get(k) == 'False'
                                    for k in ('MemoryPressure', 'DiskPressure', 'PIDPressure'))}
    if csi_node:
        plugins = [p for p in pods
                   if p['metadata'].get('namespace') == csi_node['namespace']
                   and p.get('spec', {}).get('nodeName') == host
                   and any(o.get('kind') == 'DaemonSet' and o.get('name') == csi_node['daemonset']
                           for o in p['metadata'].get('ownerReferences', []))
                   and not p['metadata'].get('deletionTimestamp')]
        result['csi_node'] = len(plugins) == 1 and (
            (plugins[0].get('status') or {}).get('phase') == 'Running') and any(
            c.get('type') == 'Ready' and c.get('status') == 'True'
            for c in ((plugins[0].get('status') or {}).get('conditions') or []))
    return result
