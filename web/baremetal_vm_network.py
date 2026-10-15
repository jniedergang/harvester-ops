"""Optional post-join VM uplinks. No installer YAML or existing network writes."""
import copy
import hashlib
import re

VC = 'vlanconfigs.network.harvesterhci.io'
CN = 'clusternetworks.network.harvesterhci.io'
VS = 'vlanstatuses.network.harvesterhci.io'
LM = 'linkmonitors.network.harvesterhci.io'
OWNER = 'harvester-ops.io/baremetal-vm-uplink'


def selection(value, management):
    if not isinstance(value, dict):
        raise ValueError('VM network: expected an object')
    template = value.get('template')
    if not isinstance(template, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}', template):
        raise ValueError('VM network: select an existing network configuration')
    macs = value.get('macs')
    if not isinstance(macs, list) or not 1 <= len(macs) <= 16:
        raise ValueError('VM network: select between 1 and 16 NICs')
    if any(not isinstance(m, str) or not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', m) for m in macs):
        raise ValueError('VM network: each NIC needs a MAC address')
    macs = [m.lower() for m in macs]
    if len(set(macs)) != len(macs):
        raise ValueError('VM network: duplicate NIC')
    if set(macs) & {str(m).lower() for m in management}:
        raise ValueError('VM network: management NICs cannot also be VM uplinks')
    return {'template': template, 'macs': macs}


def matches(config, labels):
    return all(labels.get(k) == v for k, v in (config.get('spec', {}).get('nodeSelector') or {}).items())


def plan(choice, hostname, labels, configs, networks):
    if not isinstance(hostname, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}', hostname):
        raise ValueError('VM network: invalid hostname')
    templates = [c for c in configs if c['metadata']['name'] == choice['template']]
    if len(templates) != 1 or templates[0]['metadata'].get('deletionTimestamp'):
        raise ValueError('VM network: template is missing or deleting')
    template = templates[0]
    cn = template.get('spec', {}).get('clusterNetwork')
    if cn == 'mgmt' or cn not in {c['metadata']['name'] for c in networks if not c['metadata'].get('deletionTimestamp')}:
        raise ValueError('VM network: select an existing non-management cluster network')
    if not template.get('spec', {}).get('uplink', {}).get('nics'):
        raise ValueError('VM network: template has no uplink')
    # Hostname selectors are Harvester node labels, not arbitrary client labels.
    target_labels = dict(labels or {}, **{'kubernetes.io/hostname': hostname})
    overlaps = [c['metadata']['name'] for c in configs
                if c.get('spec', {}).get('clusterNetwork') == cn and matches(c, target_labels)]
    if overlaps:
        raise ValueError('VM network: overlapping configurations: ' + ', '.join(overlaps)
                         + '. Scope them to their existing hosts in Networks first.')
    digest = hashlib.sha256((cn + '/' + hostname).encode()).hexdigest()[:12]
    name = 'bm-vm-' + digest
    if any(c['metadata']['name'] == name for c in configs):
        raise ValueError('VM network: configuration name already exists: ' + name)
    return {'name': name, 'hostname': hostname, 'cluster_network': cn,
            'macs': choice['macs'], 'template': copy.deepcopy(template),
            'nodeSelector': {'kubernetes.io/hostname': hostname}}


def manifest(prepared, node, configs, networks, links, vmis):
    if node.get('metadata', {}).get('name') != prepared['hostname']:
        raise ValueError('VM network: joined node identity does not match')
    labels = node['metadata'].get('labels') or {}
    if (labels.get('kubernetes.io/hostname') != prepared['hostname']
            or 'node-role.harvesterhci.io/witness' in labels):
        raise ValueError('VM network: target hostname label is missing, different or belongs to a witness')
    if any(v.get('status', {}).get('nodeName') == prepared['hostname'] for v in vmis):
        raise ValueError('VM network: target host has VMIs; leave its networks unchanged')
    # Re-run selector/network checks with actual post-join labels and fresh inventory.
    fresh = plan({'template': prepared['template']['metadata']['name'], 'macs': prepared['macs']},
                 prepared['hostname'], node['metadata'].get('labels'), configs, networks)
    template = fresh['template']
    if (template['metadata'].get('uid') != prepared['template']['metadata'].get('uid')
            or template['spec'] != prepared['template']['spec']):
        raise ValueError('VM network: template changed during installation; review it in Networks')
    nics = []
    for mac in prepared['macs']:
        found = [l for l in links if str(l.get('mac') or '').lower() == mac]
        if len(found) != 1 or found[0].get('masterIndex') not in (None, 0):
            raise ValueError('VM network: NIC is missing, ambiguous or already enslaved: ' + mac)
        nic = found[0].get('name', '')
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', nic) or nic == 'lo' or found[0].get('type') != 'device':
            raise ValueError('VM network: expected a physical NIC for ' + mac)
        if found[0].get('state') != 'up':
            raise ValueError('VM network: NIC link is not up: ' + nic)
        nics.append(nic)
    if len(set(nics)) != len(nics):
        raise ValueError('VM network: MACs resolved to the same NIC')
    uplink = copy.deepcopy(template['spec']['uplink'])
    uplink['nics'] = nics
    return {'apiVersion': 'network.harvesterhci.io/v1beta1', 'kind': 'VlanConfig',
            'metadata': {'name': prepared['name'], 'labels': {OWNER: 'true'}},
            'spec': {'clusterNetwork': prepared['cluster_network'],
                     'nodeSelector': prepared['nodeSelector'], 'uplink': uplink}}


def ready(statuses, name, hostname):
    relevant = [s for s in statuses if s.get('status', {}).get('vlanConfig') == name
                and s.get('status', {}).get('node') == hostname]
    return len(relevant) == 1 and not relevant[0].get('metadata', {}).get('deletionTimestamp') and any(
        str(c.get('type', '')).lower() == 'ready' and c.get('status') == 'True'
        for c in relevant[0].get('status', {}).get('conditions', []))
