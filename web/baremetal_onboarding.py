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
LABEL = 'harvester-ops.io/storage-profile'


class ProfileError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ProfileError(message)


def definition(profile_id):
    """Load only administrator-reviewed, packaged profiles; no caller paths or scripts."""
    require(isinstance(profile_id, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', profile_id), 'invalid storage profile')
    root = (PROFILE_DIR / profile_id).resolve()
    require(root.parent == PROFILE_DIR.resolve(), 'invalid storage profile directory')
    try:
        spec = yaml.safe_load((root / 'profile.yaml').read_text())
    except (OSError, yaml.YAMLError):
        raise ProfileError('storage profile is unavailable') from None
    expected = {'label', 'description', 'ports', 'files', 'boot_disk_exclusion', 'bootstrap_args',
                'modules', 'services', 'iscsi_ifaces', 'csi_node'}
    require(isinstance(spec, dict) and set(spec) == expected, 'invalid packaged profile manifest')
    require(all(isinstance(spec[k], str) and spec[k] for k in ('label', 'description')), 'invalid profile description')
    require(isinstance(spec['ports'], dict) and 1 <= len(spec['ports']) <= 16
            and all(isinstance(k, str) and re.fullmatch(r'[a-z][a-z0-9]{0,15}', k)
                    and isinstance(v, str) and v for k, v in spec['ports'].items()), 'invalid profile ports')
    require(isinstance(spec['boot_disk_exclusion'], bool), 'invalid boot disk policy')
    for field, pattern in [('files', r'[A-Za-z0-9_-]+\.yaml'), ('modules', r'[a-z][a-z0-9_]*'),
                           ('services', r'[A-Za-z0-9][A-Za-z0-9_.@-]*'), ('bootstrap_args', r'[A-Za-z0-9_.=-]+')]:
        values = spec[field]
        require(isinstance(values, list) and all(isinstance(v, str) and re.fullmatch(pattern, v) for v in values)
                and len(values) == len(set(values)), 'invalid profile ' + field)
    require('profile.yaml' not in spec['files'] and all((root / f).is_file() and (root / f).resolve().parent == root
            for f in spec['files']), 'profile OEM files are unavailable')
    require(isinstance(spec['iscsi_ifaces'], dict) and all(k in spec['ports'] and isinstance(v, str)
            and re.fullmatch(r'[A-Za-z0-9_-]+', v) for k, v in spec['iscsi_ifaces'].items()), 'invalid iSCSI bindings')
    csi = spec['csi_node']
    require(csi is None or isinstance(csi, dict) and set(csi) == {'namespace', 'daemonset'} and all(
        isinstance(v, str) and re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}', v) for v in csi.values()), 'invalid CSI DaemonSet reference')
    return spec


def options():
    out = []
    for path in sorted(PROFILE_DIR.glob('*/profile.yaml')):
        spec = definition(path.parent.name)
        out.append({'id': path.parent.name, **{k: spec[k] for k in ('label', 'description', 'ports', 'boot_disk_exclusion')}})
    return out


def selected(data):
    return data.get('onboarding') not in (None, False, {})


def selection(data, cfg):
    value = data.get('onboarding')
    require(cfg.get('install', {}).get('role') != 'witness', 'storage onboarding is not supported for witness nodes')
    require(isinstance(value, dict), 'onboarding must be an object')
    require(str(data.get('skipchecks') or '').lower() not in ('true', '1', 'yes', 'on') and 'harvester.install.skipchecks=true' not in str(data.get('extra_args') or '').split(), 'hardware checks must remain enabled for storage onboarding')
    require(data.get('mode') == 'join', 'onboarding is available only for a declared cluster join')
    require(set(value) == {'profile', 'boot_wwid', 'ports'}, 'unexpected or missing onboarding fields')
    spec = definition(value['profile'])
    for arg in spec['bootstrap_args']:
        require(not any(a.split('=')[0] == arg.split('=')[0] and a != arg for a in str(data.get('extra_args') or '').split()), 'installer arguments conflict with selected storage profile')
    require(isinstance(value['boot_wwid'], str) and (bool(re.fullmatch(r'[235][0-9a-f]{16,64}', value['boot_wwid'])) if spec['boot_disk_exclusion'] else value['boot_wwid'] == ''), 'enter the local boot disk SCSI WWID only when required by the profile')
    ports = value['ports']
    require(isinstance(ports, dict) and set(ports) == set(spec['ports']), 'configure every port of the selected profile')
    mgmt = cfg['install']['management_interface']['interfaces']
    management = {p.get('hwAddr', '').lower() for p in mgmt}
    require(all(re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', m) for m in management), 'management NICs must use MAC addresses')
    require(bool(management) and all(management), 'management NICs must use MAC addresses with onboarding')
    require(data.get('vm_network') is None or isinstance(data['vm_network'], dict), 'VM network must be an object')
    vm_macs = (data.get('vm_network') or {}).get('macs') or []
    require(isinstance(vm_macs, list) and len(vm_macs) <= 16, 'select VM uplink MAC addresses')
    out = copy.deepcopy(value)
    macs, names, addresses = [], [], []
    for role in spec['ports']:
        port = ports[role]
        require(isinstance(port, dict) and set(port) == {'name', 'mac', 'address'}, 'each storage port needs Linux name, MAC and IPv4/CIDR')
        name, mac, address = port['name'], port['mac'], port['address']
        require(isinstance(name, str) and re.fullmatch(r'(?:ens|enp|eth)[A-Za-z0-9_.-]{1,12}', name), 'invalid physical NIC name')
        try:
            interface = ipaddress.IPv4Interface(address)
        except (ValueError, TypeError):
            raise ProfileError('storage addresses must be explicit IPv4/CIDR values') from None
        require(str(interface) == address and interface.network.prefixlen < 31
                and interface.ip not in (interface.network.network_address, interface.network.broadcast_address)
                and not interface.ip.is_loopback and not interface.ip.is_multicast
                and not interface.ip.is_unspecified, 'invalid storage host address')
        out['ports'][role]['address'] = str(interface)
        names.append(name)
        addresses.append(str(interface.ip))
        macs.append(mac)
    require(len(set(names)) == len(names), 'storage NICs must be distinct')
    require(len(set(addresses)) == len(addresses), 'storage IP addresses must be distinct')
    all_macs = macs + vm_macs
    require(all(isinstance(m, str) and re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', m) for m in all_macs), 'invalid NIC MAC address')
    all_macs = [m.lower() for m in all_macs]
    require(all(m != '00:00:00:00:00:00' and not int(m[:2], 16) & 1 for m in all_macs), 'NIC MAC addresses must be unicast')
    require(len(set(all_macs)) == len(all_macs) and not set(all_macs) & management, 'management, VM and storage NICs must not overlap')
    for i, role in enumerate(spec['ports']):
        out['ports'][role]['mac'] = all_macs[i]
    return out


def oem(choice):
    """Render a reviewed profile with explicit host addresses and identities."""
    spec = definition(choice['profile'])
    stages = {}
    base = PROFILE_DIR / choice['profile']
    for filename in spec['files']:
        path = base / filename
        text = path.read_text()
        for role in choice['ports']:
            text = text.replace('{{' + role + '}}', choice['ports'][role]['name'])
        for stage, entries in yaml.safe_load(text)['stages'].items():
            stages.setdefault(stage, []).extend(entries)
    if spec['boot_disk_exclusion']:
        exclusion = {'path': '/etc/multipath/conf.d/98-local-boot-disk.conf', 'permissions': 0o644,
                     'content': 'blacklist {\n    wwid "^' + choice['boot_wwid'] + '$"\n}\n'}
        for stage in ('initramfs', 'boot'):
            stages.setdefault(stage, []).insert(0, {'name': 'Exclude the local boot disk from multipath', 'files': [copy.deepcopy(exclusion)]})
    connections, commands = [], ['set -eu']
    if spec['boot_disk_exclusion']:
        commands.extend([
            'boot_source=$(findmnt -n -o SOURCE /oem)',
            'boot_parent=$(lsblk -n -o PKNAME "$boot_source")',
            'test -n "$boot_parent"',
            'test "$(/usr/lib/udev/scsi_id --whitelisted --device=/dev/$boot_parent)" = "' + choice['boot_wwid'] + '"'])
    # Check every selected name/MAC before activating any storage connection.
    for role in choice['ports']:
        p = choice['ports'][role]
        commands.append('test "$(cat /sys/class/net/' + p['name'] + '/address)" = "' + p['mac'] + '"')
    commands.append('systemctl start NetworkManager')
    for role in choice['ports']:
        p = choice['ports'][role]
        connection_id = 'harvester-ops-storage-' + role
        connection_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, connection_id + '/' + p['mac']))
        path = '/etc/NetworkManager/system-connections/' + connection_id + '.nmconnection'
        text = ('[connection]\nid=' + connection_id + '\nuuid=' + connection_uuid
                + '\ntype=ethernet\ninterface-name=' + p['name'] + '\nautoconnect=true\nautoconnect-priority=100\n'
                + '[ethernet]\nmac-address=' + p['mac'] + '\n'
                + '[ipv4]\nmethod=manual\naddress1=' + p['address'] + '\nnever-default=true\n'
                + '[ipv6]\nmethod=disabled\n')
        connections.append({'path': path, 'permissions': 0o600, 'content': text})
        commands.extend(['nmcli connection load ' + path, 'nmcli --wait 30 connection up uuid ' + connection_uuid])
    stages.setdefault('boot', []).append({'name': 'Persist storage network connections', 'files': connections})
    stages.setdefault('network', []).insert(0, {'name': 'Verify NIC identity and activate storage networks', 'commands': ['\n'.join(commands)]})
    completion = ['set -eu']
    if spec['boot_disk_exclusion']:
        completion.append('test -s /etc/multipath/conf.d/98-local-boot-disk.conf')
    completion.extend('modprobe ' + module for module in spec['modules'])
    completion.extend('systemctl is-active --quiet ' + service for service in spec['services'])
    for role in choice['ports']:
        port = choice['ports'][role]
        completion.append('ip -o -4 addr show dev ' + port['name'] + ' | grep -Fq " inet ' + port['address'] + ' "')
    for role, iface in spec['iscsi_ifaces'].items():
        completion.append('iscsiadm -m iface -I ' + iface + ' | grep -Eq "^iface.net_ifacename = ' + choice['ports'][role]['name'] + '$"')
    completion.extend(['mkdir -p /etc/rancher/rke2/config.yaml.d',
                       "printf 'node-label+:\\n  - harvester-ops.io/storage-profile=" + choice['profile'] + "\\n' > /etc/rancher/rke2/config.yaml.d/99-harvester-ops-storage-ready.yaml"])
    stages['network'].append({'name': 'Verify active storage prerequisites before advertising completion', 'commands': ['\n'.join(completion)]})
    return yaml.safe_dump({'name': 'Storage profile: ' + choice['profile'], 'stages': stages}, sort_keys=False)


def augment(cfg, choice):
    cfg = copy.deepcopy(cfg)
    require('harvester-ops.io/install-id' not in cfg.get('os', {}).get('labels', {}), 'install identity label is reserved')
    require(LABEL not in cfg.get('os', {}).get('labels', {}), 'storage completion label is reserved')
    files = cfg.setdefault('os', {}).setdefault('write_files', [])
    generated = [
        {'path': '/oem/90-harvester-ops-storage.yaml', 'permissions': '0600', 'content': oem(choice)},
        {'path': '/etc/rancher/rke2/config.yaml.d/99-harvester-ops-onboarding.yaml', 'permissions': '0600',
         'content': 'node-taint+:\n  - harvester-ops.io/onboarding=pending:NoSchedule\n'},
    ]
    require(not any(f.get('path') in {g['path'] for g in generated} for f in files), 'advanced YAML conflicts with onboarding files')
    files.extend(generated)
    args = cfg['os'].get('additional_kernel_arguments', '').split()
    require(not any(a.startswith('systemd.debug_shell') for a in args), 'debug shell must not be persisted in the installed system')
    for arg in definition(choice['profile'])['bootstrap_args']:
        require(not any(a.split('=')[0] == arg.split('=')[0] and a != arg for a in args), 'installed arguments conflict with storage profile')
        if arg not in args:
            args.append(arg)
    cfg['os']['additional_kernel_arguments'] = ' '.join(args)
    return cfg


def guard_patch(node):
    ops = [{'op': 'test', 'path': '/metadata/uid', 'value': node['metadata']['uid']},
           {'op': 'test', 'path': '/metadata/resourceVersion', 'value': node['metadata']['resourceVersion']},
           {'op': 'add', 'path': '/spec/unschedulable', 'value': True}]
    taints = node['spec'].get('taints', [])
    remaining = [t for t in taints if not all(t.get(k) == v for k, v in TAINT.items())]
    if remaining != taints:
        ops.append({'op': 'replace', 'path': '/spec/taints', 'value': remaining})
    return ops


def readiness(node, longhorn, pods, choice):
    host = node['metadata']['name']
    conditions = {c['type']: c['status'] for c in ((node.get('status') or {}).get('conditions') or [])}
    disks = ((longhorn or {}).get('status') or {}).get('diskStatus') or {}
    disks = disks.values()
    disk = any(any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in (d.get('conditions') or [])) for d in disks if d.get('diskPath') == '/var/lib/harvester/defaultdisk')
    result = {'node': conditions.get('Ready') == 'True', 'cordoned': bool(node['spec'].get('unschedulable')),
              'local_disk': disk, 'storage_boot_setup': node['metadata'].get('labels', {}).get(LABEL) == choice['profile'],
              'pressure_clear': all(conditions.get(k) == 'False' for k in ('MemoryPressure', 'DiskPressure', 'PIDPressure'))}
    expected = definition(choice['profile'])['csi_node']
    if expected:
        plugins = [p for p in pods if p['metadata'].get('namespace') == expected['namespace'] and p.get('spec', {}).get('nodeName') == host
                   and any(o.get('kind') == 'DaemonSet' and o.get('name') == expected['daemonset'] for o in p['metadata'].get('ownerReferences', []))
                   and not p['metadata'].get('deletionTimestamp')]
        result['csi_node'] = len(plugins) == 1 and (plugins[0].get('status') or {}).get('phase') == 'Running' and any(
            c.get('type') == 'Ready' and c.get('status') == 'True' for c in ((plugins[0].get('status') or {}).get('conditions') or []))
    return result
