"""Pure early-boot networking for downloading the Harvester install config.

The remote YAML cannot configure the network until after it has been fetched.
Use dracut arguments derived from the same rendered management config instead.
"""
import ipaddress
import copy
import re


class BootNetworkError(ValueError):
    def __init__(self, path, reason):
        self.path = path
        self.reason = reason
        super().__init__('invalid boot network: ' + path + ' (' + reason + ')')


def _fail(path, reason):
    raise BootNetworkError(path, reason)


def _boot_interface_bindings(cfg):
    net = (cfg.get('install') or {}).get('management_interface') or {}
    interfaces = net.get('interfaces') or []
    if not interfaces:
        _fail('install.management_interface.interfaces', 'missing')
    bindings = []
    seen_macs = set()
    for index, iface in enumerate(interfaces):
        path = 'install.management_interface.interfaces[%d]' % index
        mac = iface.get('hwAddr') or iface.get('hw_addr')
        if mac:
            if not isinstance(mac, str) or not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', mac):
                _fail(path + '.hwAddr', 'invalid-mac')
            mac = mac.lower()
            if mac in seen_macs:
                _fail(path + '.hwAddr', 'duplicate-mac')
            seen_macs.add(mac)
            name = iface.get('name') or ('hvboot%d' % index)
            if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_-][a-zA-Z0-9_.-]{0,14}', name):
                _fail(path + '.name', 'invalid-interface-name')
        else:
            name = iface.get('name')
            if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_-][a-zA-Z0-9_.-]{0,14}', name):
                _fail(path + '.name', 'invalid-interface-name')
        if name in [n for n, _ in bindings]:
            _fail(path, 'duplicate-interface')
        bindings.append((name, mac))
    return bindings


MANAGED_ARGS = {'ip', 'ifname', 'bond', 'vlan', 'bootdev', 'BOOTIF', 'nameserver',
                'rd.neednet', 'rd.bootif', 'rd.route', 'net.ifnames', 'biosdevname'}


def check_extra_args(extra_args):
    """The early network is built from the management config: refuse extra
    kernel arguments that would compete with it (also at profile save)."""
    for token in str(extra_args or '').split():
        if token.split('=', 1)[0] in MANAGED_ARGS:
            _fail('extra_args', 'conflicting-network-argument')


def boot_network_args(cfg, extra_args=''):
    check_extra_args(extra_args)
    os_cfg = cfg.get('os') or {}
    net = (cfg.get('install') or {}).get('management_interface') or {}
    bindings = _boot_interface_bindings(cfg)
    args = ['rd.neednet=1', 'rd.bootif=0']
    slaves = []
    for name, mac in bindings:
        slaves.append(name)
        if mac:
            args.append('ifname=%s:%s' % (name, mac))
    bond = net.get('bond_options') or {}
    # The rendered config always carries a mode (the console renders
    # balance-tlb by default); these defaults only cover a bare config.
    options = dict(bond)
    options.setdefault('mode', 'active-backup')
    options.setdefault('miimon', '100')
    rendered = []
    for key in sorted(options):
        value = str(options[key])
        if not re.fullmatch(r'[a-zA-Z0-9_]+', str(key)) or not re.fullmatch(r'[a-zA-Z0-9_.+-]+', value):
            _fail('install.management_interface.bond_options', 'unsupported-option-syntax')
        rendered.append(str(key) + '=' + value)
    device = 'hvbond0'
    bond_arg = 'bond=%s:%s:%s' % (device, ','.join(slaves), ','.join(rendered))
    mtu = net.get('mtu')
    if mtu is not None:
        if not isinstance(mtu, int) or isinstance(mtu, bool) or not 576 <= mtu <= 9000:
            _fail('install.management_interface.mtu', 'invalid-mtu')
        bond_arg += ':' + str(mtu)
    args.append(bond_arg)
    vlan = net.get('vlan_id', 0)
    if not isinstance(vlan, int) or isinstance(vlan, bool) or not 0 <= vlan <= 4094:
        _fail('install.management_interface.vlan_id', 'invalid-vlan')
    if vlan:
        device += '.' + str(vlan)
        args.append('vlan=%s:hvbond0' % device)
    method = net.get('method', 'dhcp')
    if method == 'static':
        values = {}
        for key in ('ip', 'gateway'):
            try:
                values[key] = str(ipaddress.IPv4Address(net.get(key, '')))
            except (ValueError, TypeError):
                _fail('install.management_interface.' + key, 'invalid-ipv4')
        try:
            network = ipaddress.IPv4Network(values['ip'] + '/' + str(net.get('subnet_mask', '')), strict=False)
        except ValueError:
            _fail('install.management_interface.subnet_mask', 'invalid-netmask')
        if ipaddress.IPv4Address(values['gateway']) not in network:
            _fail('install.management_interface.gateway', 'outside-subnet')
        hostname = os_cfg.get('hostname') or ''
        if hostname and (not isinstance(hostname, str) or len(hostname) > 253 or not re.fullmatch(r'[a-z0-9.-]+', hostname)):
            _fail('os.hostname', 'invalid-hostname')
        args.append('ip=%s::%s:%s:%s:%s:none' % (values['ip'], values['gateway'], network.netmask, hostname, device))
    elif method == 'dhcp':
        args.append('ip=%s:dhcp' % device)
    else:
        _fail('install.management_interface.method', 'unsupported-method')
    args.append('bootdev=' + device)
    for dns in os_cfg.get('dns_nameservers') or []:
        try:
            dns = str(ipaddress.IPv4Address(dns))
        except (ValueError, TypeError):
            _fail('os.dns_nameservers', 'invalid-ipv4')
        args.append('nameserver=' + dns)
    return ' '.join(args)


def installer_boot_identity(cfg):
    """Preserve both selected MAC identity and stable names through installation.

    Bonding can replace slave current MACs before Harvester resolves hwAddr.
    Supplying name avoids that late lookup; additional kernel arguments keep
    the same names on installed boots where NM profiles refer to them.
    """
    bindings = _boot_interface_bindings(cfg)
    out = copy.deepcopy(cfg)
    interfaces = out['install']['management_interface']['interfaces']
    wanted = ['ifname=%s:%s' % (name, mac) for name, mac in bindings if mac]
    os_cfg = out.setdefault('os', {})
    current = str(os_cfg.get('additional_kernel_arguments') or '').split()
    selected_names = {name for name, mac in bindings if mac}
    selected_macs = {mac for name, mac in bindings if mac}
    for token in current:
        if not token.startswith('ifname='):
            continue
        body = token[len('ifname='):]
        name, separator, mac = body.partition(':')
        if not separator:
            _fail('os.additional_kernel_arguments', 'invalid-ifname')
        canonical = 'ifname=%s:%s' % (name, mac.lower())
        if (name in selected_names or mac.lower() in selected_macs) and canonical not in wanted:
            _fail('os.additional_kernel_arguments', 'conflicting-ifname')
    existing = {token.lower() for token in current}
    for token in wanted:
        if token.lower() not in existing:
            current.append(token)
            existing.add(token.lower())
    if wanted:
        os_cfg['additional_kernel_arguments'] = ' '.join(current)
    for iface, (name, mac) in zip(interfaces, bindings):
        iface['name'] = name
        # Keep the original profile MAC; do not replace it with the bond MAC.
        if mac:
            iface['hwAddr'] = mac
    if wanted:
        # NAME:= prevents later MAC-based 80-ifname.rules from overriding an
        # identity resolved using the bond slave's permanent MAC.
        rules = []
        for name, mac in bindings:
            if not mac:
                continue
            prefix = 'SUBSYSTEM=="net", ACTION=="add", DRIVERS=="?*", '
            suffix = 'ATTR{type}=="1", NAME:="%s"' % name
            rules.append(prefix + 'TEST!="bonding_slave", ATTR{address}=="%s", ' % mac + suffix)
            rules.append(prefix + 'TEST=="bonding_slave", ATTR{bonding_slave/perm_hwaddr}=="%s", ' % mac + suffix)
        entry = {'path': '/etc/udev/rules.d/79-harvester-bond-identity.rules',
                 'content': '\n'.join(rules) + '\n', 'permissions': '0644', 'owner': 'root:root'}
        files = os_cfg.setdefault('write_files', [])
        existing = [f for f in files if f.get('path') == entry['path']]
        if existing and existing != [entry]:
            _fail('os.write_files', 'conflicting-identity-rule')
        if not existing:
            files.append(entry)
    return out
