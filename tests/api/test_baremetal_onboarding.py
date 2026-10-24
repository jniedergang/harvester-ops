"""Storage profiles, identity guards, current-user orchestration and UI contracts."""
import copy
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'web'))
import baremetal_onboarding as p
import baremetal_onboarding_runtime as rt
import harvester_install_schema as his
sys.path.insert(0, str(ROOT / 'bin' / 'lib'))
import hv_upgrade

HOST = 'worker.example'
DISK = {'disk': {'diskPath': '/var/lib/harvester/defaultdisk',
                 'conditions': [{'type': 'Ready', 'status': 'True'}]}}


@pytest.fixture
def choice():
    return {'profile': 'pure-iscsi-nvme', 'boot_wwid': '360000000000000000000000000000001',
            'ports': {role: {'mac': '02:00:00:00:00:%02x' % (i + 5), 'address': address}
                      for i, (role, address) in enumerate(zip(
                          ('iscsi1', 'iscsi2', 'nvme1', 'nvme2'),
                          ['192.0.2.74/24', '198.51.100.74/24', '198.18.6.74/24', '198.18.10.74/24']))}}


@pytest.fixture
def cfg():
    return {'os': {'hostname': HOST, 'labels': {'svpadm.io/placement-rz5-host': 'true'}},
            'install': {'device': '/dev/sdb',
                        'management_interface': {'interfaces': [{'hwAddr': '02:00:00:00:00:01'}]}}}


@pytest.fixture
def node():
    return {'metadata': {'name': HOST, 'uid': 'node-uid', 'resourceVersion': '7',
                         'labels': {'svpadm.io/placement-rz5-host': 'true',
                                    p.INSTALL_ID_LABEL: 'this-run', p.LABEL: 'pure-iscsi-nvme'}},
            'spec': {'unschedulable': True, 'taints': [p.TAINT, {'key': 'other', 'effect': 'NoSchedule'}]},
            'status': {'conditions': [{'type': t, 'status': v} for t, v in [
                ('Ready', 'True'), ('MemoryPressure', 'False'), ('DiskPressure', 'False'),
                ('PIDPressure', 'False')]]}}


def run_for(**extra):
    return SimpleNamespace(**dict({'_cancel': False, '_onboarding_plan': {'install_id': 'this-run'}}, **extra))


def links_for(choice, **override):
    return [dict({'name': 'nic%d' % i, 'mac': port['mac'], 'type': 'device', 'masterIndex': 0}, **override)
            for i, port in enumerate(choice['ports'].values())]


def csi_pod(namespace='portworx', daemonset='px-pure-csi-node', **meta):
    return {'metadata': dict({'namespace': namespace,
                              'ownerReferences': [{'kind': 'DaemonSet', 'name': daemonset}]}, **meta),
            'spec': {'nodeName': HOST},
            'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}


def network_choice():
    return {'profile': 'storage-network-only', 'boot_wwid': '', 'ports': {
        'storage1': {'mac': '02:00:00:00:00:05', 'address': '192.0.2.74/24'},
        'storage2': {'mac': '02:00:00:00:00:06', 'address': '198.51.100.74/24'}}}


# --- sélection -------------------------------------------------------------

def test_optional_and_valid_selection(choice, cfg):
    assert not p.selected({})
    assert p.selection({'mode': 'join', 'onboarding': choice}, cfg) == choice
    assert cfg['install']['device'] == '/dev/sdb'


def _set(path, value):
    def change(data, cfg):
        target = data
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return change


def _cfg(path, value):
    def change(data, cfg):
        target = cfg
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return change


PORT = ('onboarding', 'ports', 'iscsi1')

REJECTIONS = [
    ('management MAC reused', _set(PORT + ('mac',), '02:00:00:00:00:01'), 'must not overlap'),
    ('VM uplink MAC reused', lambda d, c: d.update(vm_network={'macs': [d['onboarding']['ports']['iscsi1']['mac']]}),
     'must not overlap'),
    ('two storage ports share a MAC', _set(('onboarding', 'ports', 'iscsi2', 'mac'), '02:00:00:00:00:05'),
     'must not overlap'),
    ('duplicate IP', _set(('onboarding', 'ports', 'iscsi2', 'address'), '192.0.2.74/24'), 'must be distinct'),
    ('network address', _set(PORT + ('address',), '192.0.2.0/24'), 'invalid storage host address'),
    ('broadcast address', _set(PORT + ('address',), '192.0.2.255/24'), 'invalid storage host address'),
    ('point-to-point /31', _set(PORT + ('address',), '192.0.2.4/31'), 'invalid storage host address'),
    ('host route /32', _set(PORT + ('address',), '192.0.2.4/32'), 'invalid storage host address'),
    ('prefix missing', _set(PORT + ('address',), '192.0.2.4'), 'invalid storage host address'),
    ('loopback', _set(PORT + ('address',), '127.0.0.5/8'), 'invalid storage host address'),
    ('not an address', _set(PORT + ('address',), 'storage-a'), 'explicit IPv4/CIDR'),
    ('IPv6 address', _set(PORT + ('address',), '2001:db8::5/64'), 'explicit IPv4/CIDR'),
    ('multicast MAC', _set(PORT + ('mac',), '01:00:00:00:00:05'), 'unicast'),
    ('zero MAC', _set(PORT + ('mac',), '00:00:00:00:00:00'), 'unicast'),
    ('malformed MAC', _set(PORT + ('mac',), '02:00:00:00:00'), 'invalid NIC MAC'),
    ('free-text NIC name', _set(PORT + ('name',), 'ens3f6'), 'needs a MAC and an IPv4/CIDR'),
    ('port missing', lambda d, c: d['onboarding']['ports'].pop('nvme2'), 'every port'),
    ('regex as WWID', _set(('onboarding', 'boot_wwid'), '.*'), 'boot disk SCSI WWID'),
    ('unknown field', _set(('onboarding', 'commands'), ['reboot']), 'unexpected or missing'),
    ('create mode', _set(('mode',), 'create'), 'declared cluster join'),
    ('witness role', _cfg(('install', 'role'), 'witness'), 'witness'),
    ('management NIC by name', _cfg(('install', 'management_interface', 'interfaces'), [{'name': 'eno1'}]),
     'management NICs must use MAC'),
    ('unknown profile', _set(('onboarding', 'profile'), 'missing'), 'storage profile is unavailable'),
]


@pytest.mark.parametrize('label,change,message', REJECTIONS, ids=[r[0] for r in REJECTIONS])
def test_reject_unsafe_inputs(choice, cfg, label, change, message):
    data = {'mode': 'join', 'onboarding': choice}
    change(data, cfg)
    with pytest.raises(p.ProfileError, match=message):
        p.selection(data, cfg)


@pytest.mark.parametrize('extra', [{'skipchecks': True}, {'skipchecks': 'true'},
                                   {'extra_args': 'console=ttyS0 harvester.install.skipchecks=true'}])
def test_skipchecks_is_allowed_with_a_profile(choice, cfg, extra):
    # Le profil ne dépend pas des contrôles matériels de l'installeur.
    assert p.selection(dict({'mode': 'join', 'onboarding': choice}, **extra), cfg) == choice


# --- rendu ------------------------------------------------------------------

def test_generated_config_uses_installer_multipath_and_modules(choice, cfg):
    original = copy.deepcopy(cfg)
    out = p.augment(cfg, choice)
    assert cfg == original
    # aucun argument noyau : l'installeur n'ajoute plus multipath=off quand
    # external_storage_config est actif, et la console non plus
    assert out['os'].get('additional_kernel_arguments', '') == ''
    assert out['os']['modules'] == ['dm_multipath', 'scsi_dh_alua', 'nvme_tcp']
    storage = out['os']['external_storage_config']
    assert storage['enabled'] is True
    mp = storage['multi_path_config']
    assert mp['blacklistWwids'] == ['^' + choice['boot_wwid'] + '$']
    assert {'vendor': 'PURE', 'product': 'FlashArray'} in mp['blacklistExceptions']
    assert {'vendor': 'HPE', 'product': 'LOGICAL VOLUME'} in mp['blacklist']
    assert out['os']['labels'] == cfg['os']['labels']
    files = {f['path']: f for f in out['os']['write_files']}
    hold = files['/etc/rancher/rke2/config.yaml.d/99-harvester-ops-onboarding.yaml']['content']
    assert yaml.safe_load(hold)['node-taint+'] == ['harvester-ops.io/onboarding=pending:NoSchedule']
    assert his.check_install_config(out) == []


def test_console_does_not_add_multipath_off_when_profile_enables_external_storage(choice, cfg):
    import baremetal_boot_network as bn
    cfg['install']['management_interface']['interfaces'][0]['hwAddr'] = '02:00:00:00:00:01'
    out = bn.installer_boot_identity(p.augment(cfg, choice))
    assert 'multipath=off' not in out['os'].get('additional_kernel_arguments', '')


@pytest.mark.parametrize('label,change,message', [
    ('onboarding file already in advanced YAML',
     lambda c, ch: c['os'].update(write_files=[{'path': '/oem/90-harvester-ops-storage.yaml'}]),
     'conflicts with onboarding files'),
    ('external storage already set', lambda c, ch: c['os'].update(external_storage_config={'enabled': False}),
     'profile owns it'),
    ('multipath=off forced', lambda c, ch: c['os'].update(additional_kernel_arguments='multipath=off'),
     'conflicts with the profile multipath'),
    ('debug shell persisted', lambda c, ch: c['os'].update(additional_kernel_arguments='systemd.debug_shell'),
     'debug shell'),
    ('install id label from client', lambda c, ch: c['os']['labels'].update({p.INSTALL_ID_LABEL: 'x'}),
     'install identity label is reserved'),
    ('completion label from client', lambda c, ch: c['os']['labels'].update({p.LABEL: 'x'}),
     'completion label is reserved'),
], ids=lambda v: v if isinstance(v, str) else '')
def test_augment_refuses_conflicts(choice, cfg, label, change, message):
    change(cfg, choice)
    with pytest.raises(p.ProfileError, match=message):
        p.augment(cfg, choice)


def test_existing_modules_are_kept_once(choice, cfg):
    cfg['os']['modules'] = ['nvme_tcp', 'kvm']
    assert p.augment(cfg, choice)['os']['modules'] == ['nvme_tcp', 'kvm', 'dm_multipath', 'scsi_dh_alua']


def test_oem_binds_by_mac_and_resolves_names_before_activation(choice):
    text = p.oem(choice)
    doc = yaml.safe_load(text)
    assert '{{' not in text
    stages = doc['stages']
    assert set(stages) <= set(p.STAGES)
    active = stages['network'][0]['commands'][0]
    assert active.index('nic_by_mac') < active.index('nmcli connection')
    assert 'scsi_id' in active and 'findmnt' in active
    nm = stages['boot'][-1]['files']
    assert len(nm) == 4 and all(f['permissions'] == 0o600 for f in nm)
    for f, port in zip(nm, choice['ports'].values()):
        assert 'interface-name' not in f['content']
        assert 'mac-address=' + port['mac'] in f['content']
        assert 'never-default=true' in f['content']
    # le profil Pure désigne ses cartes par le nom résolu au démarrage
    assert p.nic_ref('iscsi1') in text
    for entries in stages.values():
        for entry in entries:
            for command in entry.get('commands', []):
                subprocess.run(['bash', '-n'], input=command, text=True, check=True)


def _fake_sysfs(root, nics):
    for name, (mac, physical, enslaved) in nics.items():
        d = root / name
        d.mkdir(parents=True)
        (d / 'address').write_text(mac + '\n')
        if physical:
            (d / 'device').mkdir()
        if enslaved:
            (d / 'master').mkdir()


def _resolution(choice, sysfs, out):
    script = '\n'.join(['set -eu'] + p._resolve_script(choice))
    return subprocess.run(['sh', '-c', script.replace('/sys/class/net', str(sysfs)).replace(p.NIC_DIR, str(out))],
                          capture_output=True, text=True)


def test_boot_script_resolves_any_kernel_name_from_the_mac(tmp_path):
    choice = network_choice()
    _fake_sysfs(tmp_path / 'net', {
        'p1p1': ('02:00:00:00:00:05', True, False),         # préfixe hors ens/enp/eth
        'eno7': ('02:00:00:00:00:06', True, False),
        'p1p1.30': ('02:00:00:00:00:05', False, False),     # VLAN : pas de lien device
        'bond0': ('02:00:00:00:00:06', False, False)})
    result = _resolution(choice, tmp_path / 'net', tmp_path / 'out')
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'out' / 'storage1').read_text().strip() == 'p1p1'
    assert (tmp_path / 'out' / 'storage2').read_text().strip() == 'eno7'


@pytest.mark.parametrize('nics', [
    {'eno1': ('02:00:00:00:00:05', True, False)},                               # MAC absente
    {'eno1': ('02:00:00:00:00:05', True, False), 'eno2': ('02:00:00:00:00:06', True, True)},  # asservie
    {'eno1': ('02:00:00:00:00:05', True, False), 'eno2': ('02:00:00:00:00:06', True, False),
     'eno3': ('02:00:00:00:00:06', True, False)},                                # ambiguë
], ids=['absent', 'enslaved', 'ambiguous'])
def test_boot_script_stops_without_one_free_physical_nic(tmp_path, nics):
    _fake_sysfs(tmp_path / 'net', nics)
    result = _resolution(network_choice(), tmp_path / 'net', tmp_path / 'out')
    assert result.returncode != 0
    assert 'no unique physical NIC for 02:00:00:00:00:06' in result.stderr


def test_profile_boot_completion_label_is_real_yaml(choice):
    doc = yaml.safe_load(p.oem(choice))
    command = doc['stages']['network'][-1]['commands'][0]
    printf = command.splitlines()[-1].split(' > ', 1)[0]
    out = subprocess.check_output(['bash', '-c', printf], text=True)
    assert yaml.safe_load(out) == {'node-label+': ['harvester-ops.io/storage-profile=pure-iscsi-nvme']}


def test_generic_profile_has_no_provider_or_protocol_side_effects(cfg, node):
    choice = network_choice()
    assert p.selection({'mode': 'join', 'onboarding': choice}, cfg) == choice
    out = p.augment(cfg, choice)
    assert 'external_storage_config' not in out['os'] and 'modules' not in out['os']
    text = p.oem(choice)
    doc = yaml.safe_load(text)
    assert 'multipath' not in text and 'iscsi' not in text and 'Pure' not in text
    assert len(doc['stages']['boot'][-1]['files']) == 2
    complete = doc['stages']['network'][-1]['commands'][0]
    assert 'harvester-ops.io/storage-profile=storage-network-only' in complete
    subprocess.run(['bash', '-n'], input=complete, text=True, check=True)
    node['metadata']['labels'][p.LABEL] = choice['profile']
    assert 'csi_node' not in p.readiness(node, None, [], choice, None)


# --- manifestes de profil ---------------------------------------------------

def _profile(tmp_path, monkeypatch, files=None, **changes):
    spec = p.definition('storage-network-only')
    spec.update(changes)
    folder = tmp_path / 'other-provider'
    folder.mkdir()
    for name, content in (files or {}).items():
        (folder / name).write_text(content)
    spec['files'] = sorted(files or {})
    (folder / 'profile.yaml').write_text(yaml.safe_dump(spec))
    monkeypatch.setattr(p, 'PROFILE_DIR', tmp_path)
    return 'other-provider'


NETWORK_FILE = 'stages:\n  network:\n  - name: x\n    commands: ["ip link show {{storage1}}"]\n'

BAD_PROFILES = [
    ('initramfs stage', {'files': {'a.yaml': 'stages:\n  initramfs:\n  - name: x\n    commands: [true]\n'}},
     'only use the fs, boot and network'),
    ('placeholder before network', {'files': {'a.yaml': NETWORK_FILE.replace('network', 'boot')}},
     'only allowed in the network stage'),
    ('undeclared port in a placeholder', {'files': {'a.yaml': NETWORK_FILE.replace('storage1', 'data9')}},
     'undeclared port'),
    ('boot disk exclusion without multipath', {'boot_disk_exclusion': True}, 'invalid boot disk policy'),
    ('snake case multipath key', {'multipath': {'blacklist_wwids': ['.*']}}, 'invalid profile multipath'),
    ('quote in vendor', {'multipath': {'blacklist': [{'vendor': 'PURE"', 'product': 'x'}]}},
     'invalid profile multipath blacklist'),
    ('empty multipath', {'multipath': {}}, 'invalid profile multipath'),
    ('legacy bootstrap_args', {'bootstrap_args': ['multipath=off']}, 'invalid packaged profile manifest'),
]


@pytest.mark.parametrize('label,changes,message', BAD_PROFILES, ids=[b[0] for b in BAD_PROFILES])
def test_profile_manifest_refused(tmp_path, monkeypatch, label, changes, message):
    name = _profile(tmp_path, monkeypatch, **changes)
    with pytest.raises(p.ProfileError, match=message):
        p.definition(name)


@pytest.mark.parametrize('name', ['../secrets', '/etc/passwd', 'pure-iscsi-nvme/../other', 'missing', None])
def test_profile_id_cannot_select_caller_paths(name):
    with pytest.raises(p.ProfileError):
        p.definition(name)


def test_different_provider_ports_and_csi_reference_are_manifest_data(tmp_path, monkeypatch, cfg, node):
    name = _profile(tmp_path, monkeypatch, files={'a.yaml': NETWORK_FILE.replace('storage1', 'data1')},
                    ports={'data1': 'Other provider data port'},
                    csi_node={'namespace': 'other-storage', 'daemonset': 'other-csi-node'})
    choice = network_choice()
    choice['profile'] = name
    choice['ports'] = {'data1': choice['ports']['storage1']}
    p.selection({'mode': 'join', 'onboarding': choice}, cfg)
    text = p.oem(choice)
    rendered = yaml.safe_load(text)
    assert len(rendered['stages']['boot'][-1]['files']) == 1
    assert 'ip link show ' + p.nic_ref('data1') in text and 'iscsiadm' not in text
    node['metadata']['labels'][p.LABEL] = name
    csi = p.definition(name)['csi_node']
    assert p.readiness(node, None, [csi_pod('other-storage', 'other-csi-node')], choice, csi)['csi_node']
    assert not p.readiness(node, None, [csi_pod()], choice, csi)['csi_node']


def test_catalog_contains_metadata_not_oem_scripts():
    import app
    with app.app.test_client() as client:
        response = client.get('/api/baremetal/onboarding/options')
    assert response.status_code == 200
    catalog = response.get_json()['profiles']
    assert {c['id'] for c in catalog} == {'pure-iscsi-nvme', 'storage-network-only'}
    assert all(set(c) == {'id', 'label', 'description', 'ports', 'boot_disk_exclusion'} for c in catalog)
    assert '/etc/' not in response.get_data(as_text=True)
    pure = next(c for c in catalog if c['id'] == 'pure-iscsi-nvme')
    assert 'unverified' in pure['label'].lower()


# --- garde du nœud ----------------------------------------------------------

def test_guard_only_removes_own_taint_atomically(node):
    ops = p.guard_patch(node)
    assert ops[:2] == [{'op': 'test', 'path': '/metadata/uid', 'value': 'node-uid'},
                       {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '7'}]
    assert ops[-1]['value'] == [{'key': 'other', 'effect': 'NoSchedule'}]
    assert ops[2] == {'op': 'add', 'path': '/spec/unschedulable', 'value': True}


class Cluster:
    """Transport factice : un nœud qui change, des patchs scriptés."""

    def __init__(self, node, patches=(), **reads):
        self.node = node
        self.patches = list(patches)
        self.reads = reads
        self.calls = []

    def _kubectl_json(self, kc, verb, kind, *args, **kw):
        assert kc == '/operator'
        self.calls.append((kind,) + args)
        if kind == 'node':
            return copy.deepcopy(self.node)
        value = self.reads[kind]
        return value(*args) if callable(value) else copy.deepcopy(value)

    def _kubectl_run(self, argv, **kw):
        assert argv[:3] == ['kubectl', '--kubeconfig', '/operator'] and argv[3] == 'patch'
        ops = json.loads(argv[argv.index('-p') + 1])
        self.calls.append(('patch', ops))
        outcome = self.patches.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == 'ok':
            out = copy.deepcopy(self.node)
            out['spec']['unschedulable'] = True
            out['spec']['taints'] = next((o['value'] for o in ops if o['path'] == '/spec/taints'),
                                         out['spec'].get('taints', []))
            self.node = out
            return SimpleNamespace(returncode=0, stdout=json.dumps(out), stderr='')
        return SimpleNamespace(returncode=1, stdout='', stderr=outcome)

    def patch_ops(self):
        return [c[1] for c in self.calls if c[0] == 'patch']


@pytest.fixture
def fresh(node):
    fresh = copy.deepcopy(node)
    fresh['spec']['unschedulable'] = False
    return fresh


def test_runtime_guard_refuses_foreign_node_identity(fresh):
    api = Cluster(fresh)
    with pytest.raises(p.ProfileError, match='installation identity'):
        rt.guard(api, '/operator', fresh, SimpleNamespace(_onboarding_plan={'install_id': 'other-run'}))
    assert api.patch_ops() == []


def test_uncertain_patch_is_not_retried(fresh):
    api = Cluster(fresh, [subprocess.TimeoutExpired('kubectl', 25)])
    with pytest.raises(p.ProfileError, match='uncertain'):
        rt.guard(api, '/operator', fresh, run_for())
    assert len(api.patch_ops()) == 1


CONFLICT = 'The request is invalid: the server rejected our request due to an error in our request'


def test_conflicting_patch_is_read_again_and_retried(fresh):
    api = Cluster(fresh, [CONFLICT, 'ok'])
    changed = copy.deepcopy(fresh)
    changed['metadata']['resourceVersion'] = '8'
    api.node = changed
    run = run_for()
    out = rt.guard(api, '/operator', fresh, run)
    first, second = api.patch_ops()
    assert first[1]['value'] == '7' and second[1]['value'] == '8'
    assert out['spec']['unschedulable'] and run._onboarding_guarded
    assert out['spec']['taints'] == [{'key': 'other', 'effect': 'NoSchedule'}]


def test_conflict_retries_are_bounded(fresh, monkeypatch):
    monkeypatch.setattr(rt, 'PATCH_ATTEMPTS', 3)
    api = Cluster(fresh, [CONFLICT] * 3)
    with pytest.raises(p.ProfileError, match='3 cordon attempts'):
        rt.guard(api, '/operator', fresh, run_for())
    assert len(api.patch_ops()) == 3


def test_forbidden_patch_stops_at_once(fresh):
    api = Cluster(fresh, ['Error from server (Forbidden): nodes "worker.example" is forbidden'])
    with pytest.raises(p.ProfileError, match='refused for the current user'):
        rt.guard(api, '/operator', fresh, run_for())
    assert len(api.patch_ops()) == 1


def test_identity_is_checked_again_after_a_conflict(fresh):
    api = Cluster(fresh, [CONFLICT])
    foreign = copy.deepcopy(fresh)
    foreign['metadata']['labels'][p.INSTALL_ID_LABEL] = 'other-run'
    api.node = foreign
    with pytest.raises(p.ProfileError, match='installation identity'):
        rt.guard(api, '/operator', fresh, run_for())
    assert len(api.patch_ops()) == 1


def test_guard_waits_while_promotion_runs_then_cordons(fresh):
    fresh['metadata']['annotations'] = {p.PROMOTE_STATUS: 'running'}
    fresh['spec']['unschedulable'] = True
    api = Cluster(fresh, ['ok'])
    run = run_for()
    assert rt.guard(api, '/operator', fresh, run) == fresh
    assert api.patch_ops() == [] and not getattr(run, '_onboarding_guarded', False)
    fresh['metadata']['annotations'][p.PROMOTE_STATUS] = 'complete'
    fresh['spec']['unschedulable'] = False
    out = rt.guard(api, '/operator', fresh, run)
    assert out['spec']['unschedulable'] and run._onboarding_guarded
    assert run._onboarding_promote_seen == 'complete'
    assert out['spec']['taints'] == [{'key': 'other', 'effect': 'NoSchedule'}]


@pytest.mark.parametrize('status', ['failed', 'unknown'])
def test_guard_stops_on_failed_promotion(fresh, status):
    fresh['metadata']['annotations'] = {p.PROMOTE_STATUS: status}
    api = Cluster(fresh, ['ok'])
    with pytest.raises(p.ProfileError, match='ended as "%s"' % status):
        rt.guard(api, '/operator', fresh, run_for())
    assert api.patch_ops() == []


# --- vérifications finales --------------------------------------------------

def reads_for(choice, links=None, configs=(), disk=DISK, pods=None):
    return {p.LM: {'status': {'linkStatus': {HOST: links_for(choice) if links is None else links}}},
            p.VC: {'items': list(configs)},
            'nodes.longhorn.io': lambda *a: {'status': {'diskStatus': disk}},
            'pods': {'items': [csi_pod()] if pods is None else pods}}


def opts_for(choice):
    return {'_cluster_kc': '/operator', 'hostname': HOST,
            '_onboarding_plan': {'choice': choice, 'install_id': 'this-run'}}


@pytest.fixture
def clock(monkeypatch):
    now = [0]
    monkeypatch.setattr(rt, 'time', SimpleNamespace(time=lambda: now[0],
                                                    sleep=lambda s: now.__setitem__(0, now[0] + s)))
    return now


def test_runtime_waits_through_fresh_longhorn_status(choice, node, clock):
    readings = []

    def longhorn(*args):
        readings.append(args)
        return {'status': {'diskStatus': None if len(readings) == 1 else DISK}}
    reads = reads_for(choice)
    reads['nodes.longhorn.io'] = longhorn
    api = Cluster(node, **reads)
    events = []
    out = rt.finish(api, opts_for(choice), run_for(_onboarding_guarded=True), lambda *a: events.append(a), 100)
    assert all(out.values()) and len(readings) == 2
    assert events[-1][1] == 'done'
    assert api.patch_ops() == []
    # pods lus côté serveur pour ce seul nœud
    assert ('pods', '-n', 'portworx', '--field-selector', 'spec.nodeName=' + HOST) in api.calls


def test_finish_cordons_a_node_seen_first_here(choice, fresh, clock):
    api = Cluster(fresh, ['ok'], **reads_for(choice))
    fresh['metadata']['labels'][p.LABEL] = 'pure-iscsi-nvme'
    out = rt.finish(api, opts_for(choice), run_for(), lambda *a: None, 100)
    assert all(out.values()) and len(api.patch_ops()) == 1


@pytest.mark.parametrize('label,change', [
    ('foreign', lambda n: n['metadata']['labels'].update({p.INSTALL_ID_LABEL: 'other'})),
    ('uncordoned', lambda n: n['spec'].update(unschedulable=False)),
    ('uncordoned by an earlier promotion', lambda n: (n['spec'].update(unschedulable=False),
                                                      n['metadata'].update(annotations={p.PROMOTE_STATUS: 'complete'}))),
    ('cancelled', None),
], ids=lambda v: v if isinstance(v, str) else '')
def test_runtime_does_not_mutate_or_release_unexpected_host(choice, node, clock, label, change):
    if change:
        change(node)
    api = Cluster(node, **reads_for(choice))
    run = run_for(_onboarding_guarded=True, _cancel=label == 'cancelled',
                  _onboarding_promote_seen='complete' if 'promotion' in label else None)
    with pytest.raises(p.ProfileError):
        rt.finish(api, opts_for(choice), run, lambda *a: None, 100)
    assert api.patch_ops() == []


def test_finish_recordons_after_promotion_completes(choice, node, clock):
    node['spec']['unschedulable'] = False
    node['metadata']['annotations'] = {p.PROMOTE_STATUS: 'complete'}
    api = Cluster(node, ['ok'], **reads_for(choice))
    events = []
    run = run_for(_onboarding_guarded=True, _onboarding_promote_seen=None)
    out = rt.finish(api, opts_for(choice), run, lambda *a: events.append(a[2]), 100)
    assert all(out.values()) and len(api.patch_ops()) == 1
    assert any('cordoned again' in e for e in events)


def test_finish_waits_for_a_promotion_started_after_the_cordon(choice, node, clock):
    node['metadata']['annotations'] = {p.PROMOTE_STATUS: 'running'}
    api = Cluster(node, **reads_for(choice))
    events = []
    with pytest.raises(p.ProfileError, match='timed out'):
        rt.finish(api, opts_for(choice), run_for(_onboarding_guarded=True), lambda *a: events.append(a[2]), 30)
    assert events and all('promotion' in e for e in events)
    assert ('nodes.longhorn.io', HOST, '-n', 'longhorn-system') not in api.calls


@pytest.mark.parametrize('label,links', [
    ('MAC absent', lambda c: links_for(c)[1:]),
    ('NIC in a bond', lambda c: links_for(c, masterIndex=3)),
    ('only a bond carries the MAC', lambda c: links_for(c, type='bond')),
    ('MAC reported twice', lambda c: links_for(c) + links_for(c)[:1]),
], ids=lambda v: v if isinstance(v, str) else '')
def test_linkmonitor_mismatch_stops_with_host_cordoned(choice, node, clock, label, links):
    api = Cluster(node, **reads_for(choice, links=links(choice)))
    with pytest.raises(p.ProfileError, match='identity or ownership changed'):
        rt.finish(api, opts_for(choice), run_for(_onboarding_guarded=True), lambda *a: None, 100)
    assert api.patch_ops() == []


def test_automatic_network_cannot_claim_a_resolved_storage_nic(choice, node, clock):
    configs = [{'metadata': {'name': 'scoped-away'}, 'spec': {'nodeSelector': {'rack': 'b'},
                                                              'uplink': {'nics': ['nic0']}}},
               {'metadata': {'name': 'all-hosts'}, 'spec': {'nodeSelector': {}, 'uplink': {'nics': ['nic2', 'eno9']}}}]
    api = Cluster(node, **reads_for(choice, configs=configs))
    with pytest.raises(p.ProfileError, match='all-hosts would use storage NIC nvme1'):
        rt.finish(api, opts_for(choice), run_for(_onboarding_guarded=True), lambda *a: None, 100)


def test_readiness_timeout_is_tunable(choice, node, clock, monkeypatch):
    monkeypatch.setattr(rt, 'READINESS_TIMEOUT', 12)
    api = Cluster(node, **reads_for(choice, disk=None))
    with pytest.raises(p.ProfileError, match='timed out.*local_disk: pending'):
        rt.finish(api, opts_for(choice), run_for(_onboarding_guarded=True), lambda *a: None, 10_000)
    assert clock[0] == 15


def test_readiness_requires_exact_host_and_real_csi_readiness(node, choice):
    lh = {'status': {'diskStatus': DISK}}
    csi = p.definition('pure-iscsi-nvme')['csi_node']
    pod = csi_pod()
    assert all(p.readiness(node, lh, [pod], choice, csi).values())
    pod['status']['conditions'][0]['status'] = 'False'
    assert not p.readiness(node, lh, [pod], choice, csi)['csi_node']
    assert not p.readiness(node, lh, [csi_pod('untrusted')], choice, csi)['csi_node']


def test_pod_being_deleted_is_ignored(node, choice):
    csi = p.definition('pure-iscsi-nvme')['csi_node']
    leaving = csi_pod(deletionTimestamp='2026-10-24T10:00:00Z')
    assert not p.readiness(node, None, [leaving], choice, csi)['csi_node']
    assert p.readiness(node, None, [leaving, csi_pod()], choice, csi)['csi_node']


def test_readiness_does_not_accept_files_without_boot_completion(node, choice):
    node['metadata']['labels'].pop(p.LABEL)
    assert not p.readiness(node, None, [], choice, None)['storage_boot_setup']


@pytest.mark.parametrize('status', [None, {}, {'diskStatus': None}, {'diskStatus': {}}])
def test_fresh_longhorn_node_waits_for_disk_status(node, choice, status):
    result = p.readiness(node, {'status': status}, [], choice, None)
    assert result['local_disk'] is False and result['node'] is True and result['cordoned'] is True


def test_partial_controller_status_stays_pending(node, choice):
    lh = {'status': {'diskStatus': {'disk': {'diskPath': '/var/lib/harvester/defaultdisk', 'conditions': None}}}}
    pod = csi_pod()
    pod['status']['conditions'] = None
    result = p.readiness(node, lh, [pod], choice, p.definition('pure-iscsi-nvme')['csi_node'])
    assert not result['local_disk'] and not result['csi_node']


# --- contrôles avant installation -------------------------------------------

class Preflight:
    _hup = hv_upgrade

    def __init__(self, node_rc=1, node_err='Error from server (NotFound): nodes "worker.example" not found',
                 can_i='yes', lm=None, ds=True, version='v1.8.2'):
        self.node_rc, self.node_err, self.can_i, self.lm, self.ds = node_rc, node_err, can_i, lm, ds
        self.version = version
        self.calls = []

    def _harvester_server_version(self, kc):
        assert kc == '/operator'
        self.calls.append(('server-version',))
        return self.version

    def _kubectl_json(self, kc, verb, kind, *args, **kw):
        assert kc == '/operator' and verb == 'get'
        self.calls.append((kind,) + args)
        if kind == p.LM:
            return self.lm if self.lm is not None else {'status': {'linkStatus': {'old-host': []}}}
        if kind == 'daemonset':
            return {'metadata': {'name': args[0]}} if self.ds else None
        return {'items': []}

    def _kubectl_run(self, argv, **kw):
        assert argv[:3] == ['kubectl', '--kubeconfig', '/operator']
        self.calls.append(tuple(argv[3:]))
        if argv[3:5] == ['get', 'node']:
            return SimpleNamespace(returncode=self.node_rc, stdout='', stderr=self.node_err)
        assert argv[3:] == ['auth', 'can-i', 'patch', 'nodes']
        return SimpleNamespace(returncode=0 if self.can_i == 'yes' else 1, stdout=self.can_i + '\n', stderr='')


def data_for(choice):
    return {'mode': 'join', 'onboarding': choice, '_cluster_kc': '/operator'}


def test_preflight_uses_current_user_and_does_not_write(choice, cfg):
    api = Preflight()
    out = rt.preflight(api, data_for(choice), cfg)
    assert out['choice'] == choice and len(out['install_id']) == 32
    assert ('get', 'node', HOST, '-o', 'json') in api.calls
    assert ('pods', '-n', 'portworx', '--field-selector', 'spec.nodeName=' + HOST) in api.calls
    assert ('nodes',) not in api.calls
    assert not any('patch' in c[:1] for c in api.calls)


@pytest.mark.parametrize('label,api,message', [
    ('registered host', Preflight(node_rc=0, node_err=''), 'new node'),
    ('nodes unreadable', Preflight(node_err='Error from server (Forbidden): nodes is forbidden'),
     'cannot read the target cluster nodes'),
    ('patch not allowed', Preflight(can_i='no'), 'node patch permission'),
    ('no LinkMonitor', Preflight(lm=False), 'LinkMonitor'),
    ('storage MAC on another host', Preflight(lm={'status': {'linkStatus': {'old-host': [
        {'name': 'eno5', 'mac': '02:00:00:00:00:07'}]}}}), 'already reported by registered host old-host'),
    ('CSI DaemonSet missing', Preflight(ds=False), 'already be installed'),
], ids=lambda v: v if isinstance(v, str) else '')
def test_preflight_refusals(choice, cfg, label, api, message):
    if api.lm is False:
        api._kubectl_json = (lambda orig: lambda kc, verb, kind, *a, **kw: None if kind == p.LM
                             else orig(kc, verb, kind, *a, **kw))(api._kubectl_json)
    with pytest.raises(p.ProfileError, match=message):
        rt.preflight(api, data_for(choice), cfg)
    assert not any(c[:1] == ('patch',) for c in api.calls)


def test_preflight_reason_reaches_the_client(monkeypatch, choice):
    import app
    import test_baremetal_vm_network as vmt
    monkeypatch.setattr(app, '_kubectl_for_cluster', lambda c: '/test/kubeconfig')
    monkeypatch.setattr(app, '_cluster_for_server', lambda s: 'test-cluster')
    vmt.route_helpers(monkeypatch)
    monkeypatch.setattr(app, '_bm_render_checked', lambda data: {})

    def refuse(api, data, cfg):
        raise p.ProfileError('node patch permission is required to keep the host cordoned')
    monkeypatch.setattr(app._bmor, 'preflight', refuse)
    data = vmt.install_data()
    data.pop('vm_network')
    data.pop('_vm_network_plan')
    data['onboarding'] = choice
    with app.app.test_request_context():
        result, error = app._bm_prepare_install(data)
    assert result is None and error[1] == 400
    body = error[0].get_json()
    assert body['error'] == ('onboarding preflight failed: node patch permission is required '
                             'to keep the host cordoned')
    assert '/test/kubeconfig' not in json.dumps(body)


# --- intégration à l'application -------------------------------------------

def test_profile_preview_hides_credentials_and_keeps_identity_after_render(choice, monkeypatch):
    import app
    app.app.config['TESTING'] = True
    monkeypatch.setattr(app, 'current_user', lambda: 'operator')
    monkeypatch.setattr(app, 'current_cluster_identity', lambda: None)
    monkeypatch.setattr(app, '_bm_disk_role_check', lambda *_: [])
    monkeypatch.setattr(app, '_bm_nic_warning', lambda *_: '')
    body = {'mode': 'join', 'server_url': 'https://cluster.example', 'hostname': HOST,
            'device': '/dev/sdb', 'mgmt_interfaces': ['02:00:00:00:00:01'],
            'method': 'dhcp', 'token': 'DO-NOT-PRINT-TOKEN', 'password': 'DO-NOT-PRINT-PASSWORD',
            'onboarding': choice}
    with app.app.test_client() as client:
        response = client.post('/api/baremetal/config/preview', json=body)
    assert response.status_code == 200, response.get_json()
    text = response.get_data(as_text=True)
    assert 'DO-NOT-PRINT' not in text
    rendered = yaml.safe_load(response.get_json()['yaml'])
    assert rendered['install']['device'] == '/dev/sdb'
    assert any(f['path'] == '/oem/90-harvester-ops-storage.yaml' for f in rendered['os']['write_files'])
    assert rendered['os']['external_storage_config']['multi_path_config']['blacklistWwids'][0] \
        == '^' + choice['boot_wwid'] + '$'
    body['_onboarding_plan'] = {'install_id': 'server-generated'}
    rendered = yaml.safe_load(app._harvester_install_config(body, boot_identity=True))
    assert rendered['os']['labels'][p.INSTALL_ID_LABEL] == 'server-generated'
    args = rendered['os'].get('additional_kernel_arguments', '')
    assert 'systemd.debug_shell' not in args and 'multipath=off' not in args


def test_disabled_profile_is_byte_for_byte_original_render(cfg):
    import app
    assert app._bm_onboarding_config({}, cfg) is cfg


def test_enabled_profile_validation_is_returned_as_schema_error(choice, cfg):
    import app
    choice['ports']['iscsi1']['address'] = 'bad;command'
    with pytest.raises(app.InstallConfigError) as err:
        app._bm_onboarding_config({'mode': 'join', 'onboarding': choice}, cfg)
    assert err.value.paths == ['onboarding']


@pytest.mark.parametrize('failure', [None, 'pools', 'vm-network'])
def test_real_runner_preserves_pools_vm_and_storage_order(monkeypatch, tmp_path, choice, failure):
    # Reuse upstream's real-runner transport harness, not a source-text assertion.
    import app
    import test_baremetal_vm_network as existing
    real_runner = app._baremetal_install_runner
    events = []

    def runner(run, opts):
        opts = dict(opts, onboarding=choice, _onboarding_plan={'choice': choice, 'install_id': 'this-run'})
        pools, vm_network = app._bm_apply_pools, app._bm_vm_apply

        def do_pools(*args):
            events.append('pools')
            return pools(*args)

        def do_vm(*args):
            events.append('vm')
            return vm_network(*args)
        monkeypatch.setattr(app, '_bm_apply_pools', do_pools)
        monkeypatch.setattr(app, '_bm_vm_apply', do_vm)
        real_runner(run, opts)

    def finish(api, opts, run, step, deadline):
        assert opts['_cluster_kc'] == '/delegated/kubeconfig'
        assert run._onboarding_plan['install_id'] == 'this-run'
        events.append('storage')
    monkeypatch.setattr(app, '_baremetal_install_runner', runner)
    monkeypatch.setattr(app._bmor, 'finish', finish)
    existing.test_install_runner_runs_pools_before_vm_network(monkeypatch, tmp_path, failure)
    assert events == (['pools'] if failure == 'pools' else ['pools', 'vm'] if failure else ['pools', 'vm', 'storage'])


# --- interface ---------------------------------------------------------------

def test_ui_is_opt_in_hidden_in_create_mode_and_explained():
    source = (ROOT / 'web/static/js/bmc.js').read_text()
    assert 'type="checkbox" name="onboard_enabled"' in source
    assert 'name="onboard_enabled" checked' not in source
    assert '<fieldset id="bm-onboarding" class="bm-onboarding" hidden>' in source
    assert "form.querySelector('.bm-onboarding').hidden = !join;" in source
    assert "field('mode').addEventListener" not in source
    assert 'body.onboarding = ' in source and '/api/baremetal/onboarding/options' in source
    assert 'onboard_${esc(role)}_name' not in source
    for key in ('enable', 'profile', 'bootWwid', 'mac', 'address'):
        assert "tipAttr(tr('bmc.onboard.tip.%s'))" % key in source


# --- cordon natif de l'installeur (v1.9.0 et plus) ---------------------------

CORDON_BY_VERSION = [
    ('v1.8.2', False),
    ('v1.9.0', True),
    ('v1.9.1-rc1', True),
    ('v1.10.0', True),
    # préversion de v1.9.0 : la console garde son propre mécanisme
    ('v1.9.0-rc4', False),
    # version illisible ou de développement : idem
    ('', False),
    ('dev', False),
]


@pytest.mark.parametrize('version,native', CORDON_BY_VERSION, ids=[v or 'unreadable' for v, _ in CORDON_BY_VERSION])
def test_preflight_chooses_the_cordon_by_cluster_version(choice, cfg, version, native):
    api = Preflight(version=version)
    out = rt.preflight(api, data_for(choice), cfg)
    assert out['native_cordon'] is native
    assert ('server-version',) in api.calls
    # le cordon natif n'a besoin d'aucun droit d'écriture sur les nœuds
    assert (('auth', 'can-i', 'patch', 'nodes') in api.calls) is not native


@pytest.mark.parametrize('version,native', CORDON_BY_VERSION[:3], ids=[v for v, _ in CORDON_BY_VERSION[:3]])
def test_patch_permission_is_only_required_without_native_cordon(choice, cfg, version, native):
    api = Preflight(version=version, can_i='no')
    if native:
        assert rt.preflight(api, data_for(choice), cfg)['native_cordon']
    else:
        with pytest.raises(p.ProfileError, match='node patch permission'):
            rt.preflight(api, data_for(choice), cfg)


TAINT_FILE = '/etc/rancher/rke2/config.yaml.d/99-harvester-ops-onboarding.yaml'


@pytest.mark.parametrize('native', [False, True], ids=['v1.8 console taint', 'v1.9 install.cordoned'])
def test_install_config_uses_native_cordon_or_console_taint(choice, cfg, native):
    out = p.augment(cfg, choice, native=native)
    paths = [f['path'] for f in out['os']['write_files']]
    assert '/oem/90-harvester-ops-storage.yaml' in paths
    assert (TAINT_FILE in paths) is not native
    assert out['install'].get('cordoned') is (True if native else None)
    assert his.validate_install_config(his.canonicalize(out)) == []


@pytest.mark.parametrize('native,value', [(True, False), (False, True)], ids=['v1.9 false', 'v1.8 true'])
def test_advanced_install_cordoned_conflicts_with_the_profile(choice, cfg, native, value):
    cfg['install']['cordoned'] = value
    with pytest.raises(p.ProfileError, match='install.cordoned'):
        p.augment(cfg, choice, native=native)


def test_advanced_install_cordoned_true_is_kept_on_native_path(choice, cfg):
    cfg['install']['cordoned'] = True
    assert p.augment(cfg, choice, native=True)['install']['cordoned'] is True


def native_run(**extra):
    return run_for(_onboarding_plan={'install_id': 'this-run', 'native_cordon': True}, **extra)


@pytest.fixture
def natively_cordoned(node):
    node['metadata']['labels'][p.NATIVE_CORDON_LABEL] = 'true'
    node['metadata']['annotations'] = {p.NATIVE_CORDON_DONE: 'true'}
    node['spec']['taints'] = [{'key': 'other', 'effect': 'NoSchedule'}]
    return node


def test_native_guard_never_patches(natively_cordoned):
    natively_cordoned['spec']['unschedulable'] = False
    api = Cluster(natively_cordoned)
    assert rt.guard(api, '/operator', natively_cordoned, native_run()) == natively_cordoned
    assert api.patch_ops() == []


def test_native_finish_waits_for_the_cordon_then_checks_without_writing(choice, natively_cordoned, clock):
    pending = copy.deepcopy(natively_cordoned)
    pending['metadata']['annotations'] = {}
    pending['spec']['unschedulable'] = False
    seen = []

    class Later(Cluster):
        def _kubectl_json(self, kc, verb, kind, *args, **kw):
            if kind == 'node':
                seen.append(1)
                self.node = pending if len(seen) == 1 else natively_cordoned
            return super()._kubectl_json(kc, verb, kind, *args, **kw)
    api = Later(pending, **reads_for(choice))
    events = []
    out = rt.finish(api, opts_for(choice) | {'_onboarding_plan': {'choice': choice, 'install_id': 'this-run',
                                                                  'native_cordon': True}},
                    native_run(), lambda *a: events.append(a[2]), 100)
    assert all(out.values()) and api.patch_ops() == []
    assert events[0] == 'Waiting for Harvester to apply the install cordon'


@pytest.mark.parametrize('label,change,message', [
    ('last available node left schedulable', lambda n: n['spec'].update(unschedulable=False), 'host is schedulable'),
    ('foreign node', lambda n: n['metadata']['labels'].update({p.INSTALL_ID_LABEL: 'other'}), 'installation identity'),
], ids=lambda v: v if isinstance(v, str) else '')
def test_native_finish_stops_without_writing(choice, natively_cordoned, clock, label, change, message):
    change(natively_cordoned)
    api = Cluster(natively_cordoned, **reads_for(choice))
    with pytest.raises(p.ProfileError, match=message):
        rt.finish(api, opts_for(choice), native_run(), lambda *a: None, 100)
    assert api.patch_ops() == []


def test_final_install_config_carries_native_cordon_from_the_plan(choice):
    import app
    body = {'mode': 'join', 'server_url': 'https://cluster.example', 'hostname': HOST,
            'device': '/dev/sdb', 'mgmt_interfaces': ['02:00:00:00:00:01'], 'method': 'dhcp',
            'token': 'x', 'password': 'y', 'onboarding': choice,
            '_onboarding_plan': {'install_id': 'server-generated', 'native_cordon': True}}
    rendered = yaml.safe_load(app._harvester_install_config(body, boot_identity=True))
    assert rendered['install']['cordoned'] is True
    assert TAINT_FILE not in [f['path'] for f in rendered['os']['write_files']]
    body['_onboarding_plan']['native_cordon'] = False
    rendered = yaml.safe_load(app._harvester_install_config(body, boot_identity=True))
    assert 'cordoned' not in rendered['install']
    assert TAINT_FILE in [f['path'] for f in rendered['os']['write_files']]


def test_native_guard_accepts_a_node_registered_before_the_rke2_labels(natively_cordoned):
    # vu sur le banc : kubelet crée le nœud avec ses seuls --node-labels ;
    # le label install-cordoned arrive 17 à 18 s plus tard, avec ceux de rke2
    natively_cordoned['metadata']['labels'].pop(p.NATIVE_CORDON_LABEL)
    natively_cordoned['metadata']['annotations'] = {}
    natively_cordoned['spec']['unschedulable'] = False
    api = Cluster(natively_cordoned)
    assert rt.guard(api, '/operator', natively_cordoned, native_run()) == natively_cordoned
    assert api.patch_ops() == []


def test_native_finish_times_out_when_the_installer_ignored_the_option(choice, natively_cordoned, clock):
    natively_cordoned['metadata']['labels'].pop(p.NATIVE_CORDON_LABEL)
    natively_cordoned['metadata']['annotations'] = {}
    natively_cordoned['spec']['unschedulable'] = False
    api = Cluster(natively_cordoned, **reads_for(choice))
    with pytest.raises(p.ProfileError, match='timed out.*installer older than the cluster'):
        rt.finish(api, opts_for(choice), native_run(), lambda *a: None, 30)
    assert api.patch_ops() == []
    assert ('nodes.longhorn.io', HOST, '-n', 'longhorn-system') not in api.calls


def test_onboarding_guard_refusal_while_waiting_for_the_node_fails_the_run(monkeypatch, tmp_path, choice):
    """Vu en réel : une ProfileError de la garde dans wait-node tuait le fil
    du déroulé, l'action restait « running » et ses artefacts en place."""
    import app
    from unittest.mock import Mock
    import test_baremetal_vm_network as vmt
    (tmp_path / 'harvester.iso').write_bytes(b'fixture')
    opts = dict(vmt.install_data(), cluster='test-cluster', _cluster_kc='/delegated/kubeconfig',
                onboarding=choice, _onboarding_plan={'choice': choice, 'install_id': 'this-run'})
    opts.pop('vm_network', None)
    run = SimpleNamespace(id='fixture', _cancel=False, emit=Mock(), close=Mock())
    profile = {'ok': True, 'power_state': 'Off', 'virtualmedia_path': '/media',
               'boot_targets': ['Cd'], 'uefi_targets': ['HD.fixture'], 'system_path': '/system'}

    def refuse(*a):
        raise p.ProfileError('registered node does not carry this installation identity; no node was patched')
    for name, replacement in {
        '_bmc_discover_one': lambda *a: profile, '_bm_nic_warning': lambda *a: None,
        '_iso_dir': lambda: tmp_path, '_iso_work_dir': lambda: tmp_path,
        '_bm_artifact_base_urls': lambda *a: ('http://fixture', 'http://fixture'),
        '_bm_join_pubkey': lambda *a: None, '_harvester_install_config': lambda *a, **kw: '{}',
        '_bm_boot_network_args': lambda *a: '', '_bm_remaster_stream': lambda *a: 0,
        '_bm_power_off': lambda *a: (True, ''), '_bm_media_insert': lambda *a: (True, '', {}),
        '_bm_boot_once_cd': lambda *a: (True, ''),
        '_redfish_get': lambda *a, **kw: {'PowerState': 'Off', 'Boot': {
            'BootSourceOverrideTarget': 'Cd', 'BootSourceOverrideEnabled': 'Once'}},
        '_bm_reset': lambda *a: (True, ''), '_bm_wait_power_off': lambda *a, **kw: True,
        '_bm_media_eject': lambda *a: (True, ''), '_bm_boot_once_target': lambda *a: (True, ''),
        '_bm_wait_node_ready': refuse,
    }.items():
        monkeypatch.setattr(app, name, replacement)
    monkeypatch.setattr(app._bmd, 'load_inventory', lambda *a: None)
    monkeypatch.setattr(app.pxe_server, 'start', lambda: 8091)
    monkeypatch.setattr(app.pxe_server, 'issue', lambda *a: 'fixture')
    revoke = Mock()
    monkeypatch.setattr(app.pxe_server, 'revoke', revoke)
    app._baremetal_install_runner(run, opts)
    assert run.status == 'error'
    assert 'storage onboarding stopped: registered node does not carry' in run.error_summary
    revoke.assert_called()
    assert not list(tmp_path.glob('config-*'))
    run.close.assert_called_once()
