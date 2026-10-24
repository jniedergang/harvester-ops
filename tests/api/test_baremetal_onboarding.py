"""Pure profile, identity guards, current-user orchestration and UI contracts."""
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


@pytest.fixture
def choice():
    return {'profile': 'pure-iscsi-nvme', 'boot_wwid': '360000000000000000000000000000001',
            'ports': {role: {'name': name, 'mac': '02:00:00:00:00:%02x' % (i + 5), 'address': address}
                      for i, (role, name, address) in enumerate(zip(('iscsi1', 'iscsi2', 'nvme1', 'nvme2'),
                        ['ens3f6', 'ens3f7', 'enp55s1f0', 'enp55s1f1'],
                        ['192.0.2.74/24', '198.51.100.74/24', '198.18.6.74/24', '198.18.10.74/24']))}}


@pytest.fixture
def cfg():
    return {'os': {'hostname': 'worker.example', 'labels': {'svpadm.io/placement-rz5-host': 'true'}},
            'install': {'device': '/dev/sdb', 'management_interface': {'interfaces': [{'hwAddr': '02:00:00:00:00:01'}]}}}


@pytest.fixture
def node():
    return {'metadata': {'name': 'worker.example', 'uid': 'node-uid', 'resourceVersion': '7',
                         'labels': {'svpadm.io/placement-rz5-host': 'true', 'harvester-ops.io/install-id': 'this-run', p.LABEL: 'pure-iscsi-nvme'}},
            'spec': {'unschedulable': True, 'taints': [p.TAINT, {'key': 'other', 'effect': 'NoSchedule'}]},
            'status': {'conditions': [{'type': t, 'status': v} for t, v in [
                ('Ready', 'True'), ('MemoryPressure', 'False'), ('DiskPressure', 'False'), ('PIDPressure', 'False')]]}}


def test_optional_and_valid_selection(choice, cfg):
    assert not p.selected({})
    assert p.selection({'mode': 'join', 'onboarding': choice}, cfg) == choice
    assert cfg['install']['device'] == '/dev/sdb'


@pytest.mark.parametrize('change', ['mgmt_overlap', 'vm_overlap', 'duplicate_ip', 'bad_ip', 'bad_name', 'multicast', 'bad_wwid', 'unknown', 'create'])
def test_reject_unsafe_inputs(choice, cfg, change):
    data = {'mode': 'join', 'onboarding': choice}
    if change == 'mgmt_overlap': choice['ports']['iscsi1']['mac'] = '02:00:00:00:00:01'
    if change == 'vm_overlap': data['vm_network'] = {'macs': [choice['ports']['iscsi1']['mac']]}
    if change == 'duplicate_ip': choice['ports']['iscsi2']['address'] = choice['ports']['iscsi1']['address']
    if change == 'bad_ip': choice['ports']['iscsi1']['address'] = '192.0.2.0/24'
    if change == 'bad_name': choice['ports']['iscsi1']['name'] = 'ens3f6;reboot'
    if change == 'multicast': choice['ports']['iscsi1']['mac'] = '01:00:00:00:00:05'
    if change == 'bad_wwid': choice['boot_wwid'] = '.*'
    if change == 'unknown': choice['commands'] = ['reboot']
    if change == 'create': data['mode'] = 'create'
    with pytest.raises(p.ProfileError): p.selection(data, cfg)


def test_generated_config_preserves_source_and_blocks_conflicts(choice, cfg):
    original = copy.deepcopy(cfg)
    out = p.augment(cfg, choice)
    assert cfg == original
    assert out['os']['additional_kernel_arguments'] == 'multipath=off'
    assert out['os']['labels'] == cfg['os']['labels']
    files = {f['path']: f for f in out['os']['write_files']}
    hold = files['/etc/rancher/rke2/config.yaml.d/99-harvester-ops-onboarding.yaml']['content']
    assert yaml.safe_load(hold)['node-taint+'] == ['harvester-ops.io/onboarding=pending:NoSchedule']
    cfg['os']['write_files'] = [files['/oem/90-harvester-ops-storage.yaml']]
    with pytest.raises(p.ProfileError): p.augment(cfg, choice)


def test_oem_is_host_specific_persistent_and_valid_shell(choice):
    text = p.oem(choice)
    doc = yaml.safe_load(text)
    assert '{{' not in text
    assert '192.0.2.74/24' in text and '172.30.113.72' not in text
    stages = doc['stages']
    assert stages['boot'][0]['files'][0]['path'].endswith('98-local-boot-disk.conf')
    assert choice['boot_wwid'] in stages['initramfs'][0]['files'][0]['content']
    active = stages['network'][0]['commands'][0]
    assert active.index('test "$(cat /sys/class/net/') < active.index('nmcli connection')
    assert 'scsi_id' in active and 'findmnt' in active
    nm = stages['boot'][-1]['files']
    assert len(nm) == 4 and all(f['permissions'] == 0o600 for f in nm)
    assert all('never-default=true' in f['content'] for f in nm)
    for entries in stages.values():
        for entry in entries:
            for command in entry.get('commands', []):
                subprocess.run(['bash', '-n'], input=command, text=True, check=True)


def test_guard_only_removes_own_taint_atomically(node):
    ops = p.guard_patch(node)
    assert ops[:2] == [{'op': 'test', 'path': '/metadata/uid', 'value': 'node-uid'},
                      {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '7'}]
    assert ops[-1]['value'] == [{'key': 'other', 'effect': 'NoSchedule'}]
    assert ops[2] == {'op': 'add', 'path': '/spec/unschedulable', 'value': True}


def test_runtime_guard_refuses_foreign_node_identity(node):
    api = SimpleNamespace(_kubectl_run=lambda *a, **kw: pytest.fail('must not patch'))
    with pytest.raises(p.ProfileError): rt.guard(api, '/current-user.kubeconfig', node, SimpleNamespace(_onboarding_install_id='other-run'))


def test_uncertain_patch_is_not_retried(node):
    calls = []
    def run(*args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired('kubectl', 25)
    with pytest.raises(p.ProfileError, match='uncertain'):
        rt.patch(SimpleNamespace(_kubectl_run=run), '/current-user.kubeconfig', node['metadata']['name'], p.guard_patch(node))
    assert len(calls) == 1


def test_readiness_requires_exact_host_and_real_csi_readiness(node, choice):
    lh = {'status': {'diskStatus': {'disk': {'diskPath': '/var/lib/harvester/defaultdisk', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}}}
    pod = {'metadata': {'namespace': 'portworx', 'ownerReferences': [{'kind': 'DaemonSet', 'name': 'px-pure-csi-node'}]},
           'spec': {'nodeName': node['metadata']['name']}, 'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}
    assert all(p.readiness(node, lh, [pod], choice).values())
    pod['status']['conditions'][0]['status'] = 'False'
    assert not p.readiness(node, lh, [pod], choice)['csi_node']
    pod['status']['conditions'][0]['status'] = 'True'
    pod['metadata']['namespace'] = 'untrusted'
    assert not p.readiness(node, lh, [pod], choice)['csi_node']


def test_ui_is_opt_in_and_sends_profile_fields():
    source = (ROOT / 'web/static/js/bmc.js').read_text()
    assert 'type="checkbox" name="onboard_enabled"' in source
    assert 'name="onboard_enabled" checked' not in source
    assert "if (onboardEnabled.checked)" in source
    assert 'body.onboarding = ' in source
    assert '/api/baremetal/onboarding/options' in source


def test_readiness_does_not_accept_files_without_boot_completion(node, choice):
    node['metadata']['labels'].pop(p.LABEL)
    assert not p.readiness(node, None, [], choice)['storage_boot_setup']


def test_preflight_uses_current_user_and_does_not_write(choice, cfg):
    calls = []
    def query(kc, *args, **kw):
        assert kc == '/operator.kubeconfig'
        calls.append(args)
        if args[1] == 'daemonset': return {'metadata': {'name': args[2]}}
        return {'items': []}
    def run(argv, **kw):
        assert argv[:3] == ['kubectl', '--kubeconfig', '/operator.kubeconfig']
        assert argv[3:] == ['auth', 'can-i', 'patch', 'nodes']
        return SimpleNamespace(returncode=0, stdout='yes\n')
    api = SimpleNamespace(_kubectl_json=query, _kubectl_run=run)
    out = rt.preflight(api, {'mode': 'join', 'onboarding': choice, '_cluster_kc': '/operator.kubeconfig'}, cfg)
    assert out['choice'] == choice and len(out['install_id']) == 32
    assert all(c[0] == 'get' for c in calls)


def test_existing_host_blocks_before_permission_probe(choice, cfg, node):
    api = SimpleNamespace(_kubectl_json=lambda *a, **kw: {'items': [node]},
                          _kubectl_run=lambda *a, **kw: pytest.fail('no write or permission probe expected'))
    with pytest.raises(p.ProfileError, match='new node'):
        rt.preflight(api, {'mode': 'join', 'onboarding': choice, '_cluster_kc': '/operator'}, cfg)


def test_profile_preview_hides_credentials_and_keeps_identity_after_render(choice, monkeypatch):
    import app
    app.app.config['TESTING'] = True
    monkeypatch.setattr(app, 'current_user', lambda: 'operator')
    monkeypatch.setattr(app, 'current_cluster_identity', lambda: None)
    monkeypatch.setattr(app, '_bm_disk_role_check', lambda *_: [])
    monkeypatch.setattr(app, '_bm_nic_warning', lambda *_: '')
    body = {'mode': 'join', 'server_url': 'https://cluster.example', 'hostname': 'worker.example',
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
    body['_onboarding_plan'] = {'install_id': 'server-generated'}
    rendered = yaml.safe_load(app._harvester_install_config(body, boot_identity=True))
    assert rendered['os']['labels']['harvester-ops.io/install-id'] == 'server-generated'
    assert 'systemd.debug_shell' not in rendered['os']['additional_kernel_arguments']


def test_disabled_profile_is_byte_for_byte_original_render(cfg):
    import app
    assert app._bm_onboarding_config({}, cfg) is cfg


def test_enabled_profile_validation_is_returned_as_schema_error(choice, cfg):
    import app
    choice['ports']['iscsi1']['name'] = 'bad;command'
    with pytest.raises(app.InstallConfigError) as err:
        app._bm_onboarding_config({'mode': 'join', 'onboarding': choice}, cfg)
    assert err.value.paths == ['onboarding']


@pytest.mark.parametrize('status', [None, {}, {'diskStatus': None}, {'diskStatus': {}}])
def test_fresh_longhorn_node_waits_for_disk_status(node, choice, status):
    result = p.readiness(node, {'status': status}, [], choice)
    assert result['local_disk'] is False
    assert result['node'] is True
    assert result['cordoned'] is True


def test_partial_controller_status_stays_pending(node, choice):
    lh = {'status': {'diskStatus': {'disk': {'diskPath': '/var/lib/harvester/defaultdisk', 'conditions': None}}}}
    pod = {'metadata': {'namespace': 'portworx', 'ownerReferences': [{'kind': 'DaemonSet', 'name': 'px-pure-csi-node'}]},
           'spec': {'nodeName': node['metadata']['name']}, 'status': {'phase': 'Running', 'conditions': None}}
    result = p.readiness(node, lh, [pod], choice)
    assert not result['local_disk'] and not result['csi_node']


def network_choice():
    return {'profile': 'storage-network-only', 'boot_wwid': '', 'ports': {
        'storage1': {'name': 'ens3f6', 'mac': '02:00:00:00:00:05', 'address': '192.0.2.74/24'},
        'storage2': {'name': 'ens3f7', 'mac': '02:00:00:00:00:06', 'address': '198.51.100.74/24'}}}


def test_generic_profile_has_no_provider_or_protocol_side_effects(cfg, node):
    choice = network_choice()
    assert p.selection({'mode': 'join', 'onboarding': choice}, cfg) == choice
    out = p.augment(cfg, choice)
    assert out['os']['additional_kernel_arguments'] == ''
    text = p.oem(choice)
    doc = yaml.safe_load(text)
    assert 'multipath' not in text and 'iscsi' not in text and 'Pure' not in text
    assert len(doc['stages']['boot'][-1]['files']) == 2
    complete = doc['stages']['network'][-1]['commands'][0]
    assert 'harvester-ops.io/storage-profile=storage-network-only' in complete
    subprocess.run(['bash', '-n'], input=complete, text=True, check=True)
    node['metadata']['labels'][p.LABEL] = choice['profile']
    assert 'csi_node' not in p.readiness(node, None, [], choice)


@pytest.mark.parametrize('name', ['../secrets', '/etc/passwd', 'pure-iscsi-nvme/../other', 'missing', None])
def test_profile_id_cannot_select_caller_paths(name):
    with pytest.raises(p.ProfileError): p.definition(name)


def test_different_provider_ports_and_csi_reference_are_manifest_data(tmp_path, monkeypatch, cfg, node):
    spec = p.definition('storage-network-only')
    spec['ports'] = {'data1': 'Other provider data port'}
    spec['csi_node'] = {'namespace': 'other-storage', 'daemonset': 'other-csi-node'}
    folder = tmp_path / 'other-provider'; folder.mkdir()
    (folder / 'profile.yaml').write_text(yaml.safe_dump(spec))
    monkeypatch.setattr(p, 'PROFILE_DIR', tmp_path)
    choice = network_choice(); choice['profile'] = 'other-provider'; choice['ports'] = {'data1': choice['ports']['storage1']}
    p.selection({'mode': 'join', 'onboarding': choice}, cfg)
    rendered = yaml.safe_load(p.oem(choice))
    assert len(rendered['stages']['boot'][-1]['files']) == 1
    assert 'iscsiadm' not in p.oem(choice)
    node['metadata']['labels'][p.LABEL] = 'other-provider'
    pod = {'metadata': {'namespace': 'other-storage', 'ownerReferences': [{'kind': 'DaemonSet', 'name': 'other-csi-node'}]},
           'spec': {'nodeName': node['metadata']['name']}, 'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}
    assert p.readiness(node, None, [pod], choice)['csi_node']
    pod['metadata']['namespace'] = 'portworx'
    assert not p.readiness(node, None, [pod], choice)['csi_node']


def test_catalog_contains_metadata_not_oem_scripts():
    import app
    with app.app.test_client() as client:
        response = client.get('/api/baremetal/onboarding/options')
    assert response.status_code == 200
    catalog = response.get_json()['profiles']
    assert {c['id'] for c in catalog} == {'pure-iscsi-nvme', 'storage-network-only'}
    assert all(set(c) == {'id', 'label', 'description', 'ports', 'boot_disk_exclusion'} for c in catalog)
    assert '/etc/' not in response.get_data(as_text=True)


def test_runtime_waits_through_fresh_longhorn_status(choice, node, monkeypatch):
    readings = []
    def query(kc, verb, kind, *args, **kw):
        assert kc == '/operator'
        if kind == 'node': return copy.deepcopy(node)
        if kind == p.LM: return {'status': {'linkStatus': {node['metadata']['name']: [dict(port, type='device', masterIndex=0) for port in choice['ports'].values()]}}}
        if kind == 'nodes.longhorn.io':
            readings.append(1)
            return {'status': {'diskStatus': None if len(readings) == 1 else {'disk': {'diskPath': '/var/lib/harvester/defaultdisk', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}}}
        if kind == 'pods': return {'items': [{'metadata': {'namespace': 'portworx', 'ownerReferences': [{'kind': 'DaemonSet', 'name': 'px-pure-csi-node'}]}, 'spec': {'nodeName': node['metadata']['name']}, 'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}]}
        pytest.fail(kind)
    monkeypatch.setattr(rt, 'time', SimpleNamespace(time=lambda: 1, sleep=lambda _: None))
    api = SimpleNamespace(_kubectl_json=query, _kubectl_run=lambda *a, **k: pytest.fail('finish must not write'))
    events = []
    opts = {'_cluster_kc': '/operator', 'hostname': node['metadata']['name'], '_onboarding_plan': {'choice': choice, 'install_id': 'this-run'}}
    out = rt.finish(api, opts, SimpleNamespace(_cancel=False), lambda *a: events.append(a), 10)
    assert all(out.values()) and len(readings) == 2
    assert events[-1][1] == 'done'


@pytest.mark.parametrize('change', ['foreign', 'uncordoned', 'cancelled'])
def test_runtime_does_not_mutate_or_release_unexpected_host(choice, node, monkeypatch, change):
    if change == 'foreign': node['metadata']['labels']['harvester-ops.io/install-id'] = 'other'
    if change == 'uncordoned': node['spec']['unschedulable'] = False
    api = SimpleNamespace(_kubectl_json=lambda *a, **k: node, _kubectl_run=lambda *a, **k: pytest.fail('no write expected'))
    opts = {'_cluster_kc': '/operator', 'hostname': node['metadata']['name'], '_onboarding_plan': {'choice': choice, 'install_id': 'this-run'}}
    monkeypatch.setattr(rt, 'time', SimpleNamespace(time=lambda: 1))
    with pytest.raises(p.ProfileError):rt.finish(api, opts, SimpleNamespace(_cancel=change == 'cancelled'), lambda *a: None, 10)


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
            events.append('pools');return pools(*args)
        def do_vm(*args):
            events.append('vm');return vm_network(*args)
        monkeypatch.setattr(app, '_bm_apply_pools', do_pools)
        monkeypatch.setattr(app, '_bm_vm_apply', do_vm)
        real_runner(run, opts)
    def finish(api, opts, run, step, deadline):
        assert opts['_cluster_kc'] == '/delegated/kubeconfig'
        assert run._onboarding_install_id == 'this-run'
        events.append('storage')
    monkeypatch.setattr(app, '_baremetal_install_runner', runner)
    monkeypatch.setattr(app._bmor, 'finish', finish)
    existing.test_install_runner_runs_pools_before_vm_network(monkeypatch, tmp_path, failure)
    assert events == (['pools'] if failure == 'pools' else ['pools', 'vm'] if failure else ['pools', 'vm', 'storage'])


def test_profile_requires_existing_csi_before_host_actions(choice, cfg):
    def query(kc, verb, kind, *args, **kwargs):
        return None if kind == 'daemonset' else {'items': []}
    api = SimpleNamespace(_kubectl_json=query, _kubectl_run=lambda *a, **k: SimpleNamespace(returncode=0, stdout='yes'))
    with pytest.raises(p.ProfileError, match='already be installed'):
        rt.preflight(api, {'mode': 'join', 'onboarding': choice, '_cluster_kc': '/operator'}, cfg)


def test_profile_boot_completion_label_is_real_yaml(choice):
    doc = yaml.safe_load(p.oem(choice))
    command = doc['stages']['network'][-1]['commands'][0]
    # Verify the shell writes a profile value, not a Python expression or quoted token.
    printf = command.splitlines()[-1].split(' > ', 1)[0]
    out = subprocess.check_output(['bash', '-c', printf], text=True)
    assert yaml.safe_load(out) == {'node-label+': ['harvester-ops.io/storage-profile=pure-iscsi-nvme']}


def test_automatic_network_cannot_claim_storage_nic_before_install(choice, cfg):
    def query(kc, verb, kind, *args, **kwargs):
        if kind == 'vlanconfigs.network.harvesterhci.io':
            return {'items': [{'spec': {'nodeSelector': {}, 'uplink': {'nics': ['ens3f6']}}}]}
        return {'items': []}
    api = SimpleNamespace(_kubectl_json=query, _kubectl_run=lambda *a, **k: pytest.fail('conflict must be detected before permission probe or writes'))
    with pytest.raises(p.ProfileError, match='automatic VM network'):
        rt.preflight(api, {'mode': 'join', 'onboarding': choice, '_cluster_kc': '/operator'}, cfg)
