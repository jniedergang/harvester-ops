"""VM uplink planning and the actual app orchestration, without a cluster."""
import ast
import copy
import json
import sys
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'web'))
import baremetal_vm_network as vm

HOST = 'node4.example.test'
MACS = ['f6:66:60:a0:05:47', 'f6:66:60:a0:05:48']
MGMT = ['f6:66:60:a0:05:45', 'f6:66:60:a0:05:46']
TEMPLATE = {'metadata': {'name': 'vm-bond', 'uid': 'template-uid'}, 'spec': {
    'clusterNetwork': 'k8s-vms', 'nodeSelector': {'uplink-layout': 'old'},
    'uplink': {'nics': ['ens3f4', 'ens3f5'], 'bondOptions': {'mode': '802.3ad', 'miimon': -1},
               'linkAttributes': {'mtu': 1500, 'txQLen': -1}}}}
NETWORKS = [{'metadata': {'name': 'k8s-vms'}}]
NODE = {'metadata': {'name': HOST, 'uid': 'node-uid', 'labels': {'kubernetes.io/hostname': HOST}}}
LINKS = [{'name': name, 'mac': mac, 'type': 'device', 'state': 'up', 'masterIndex': 0}
         for name, mac in zip(['ens3f6', 'ens3f7'], MACS)]


def prepared():
    return vm.plan(vm.selection({'template': 'vm-bond', 'macs': MACS}, MGMT), HOST, {}, [TEMPLATE], NETWORKS)


def render(p=None, node=None, configs=None, networks=None, links=None, vmis=None):
    return vm.manifest(p or prepared(), node or NODE, [TEMPLATE] if configs is None else configs,
                       NETWORKS if networks is None else networks, LINKS if links is None else links,
                       [] if vmis is None else vmis)


def test_mapping_preserves_bond_attributes_and_scopes_only_new_node():
    before = copy.deepcopy(TEMPLATE)
    out = render()
    assert out['spec']['uplink']['nics'] == ['ens3f6', 'ens3f7']
    assert out['spec']['uplink']['bondOptions'] == TEMPLATE['spec']['uplink']['bondOptions']
    assert out['spec']['uplink']['linkAttributes'] == TEMPLATE['spec']['uplink']['linkAttributes']
    assert out['spec']['nodeSelector'] == {'kubernetes.io/hostname': HOST}
    assert TEMPLATE == before
    assert set(out) == {'apiVersion', 'kind', 'metadata', 'spec'}
    assert prepared()['name'] == prepared()['name']


@pytest.mark.parametrize('macs', [[], [MACS[0]] * 2, MGMT, ['not-a-mac'], 'eth0', [None]])
def test_bad_or_management_nics_refused(macs):
    with pytest.raises(ValueError):
        vm.selection({'template': 'vm-bond', 'macs': macs}, MGMT)


@pytest.mark.parametrize('selector', [{}, {'kubernetes.io/hostname': HOST}, {'site': 'RZ3'}])
def test_overlapping_selector_refused_before_install(selector):
    overlap = copy.deepcopy(TEMPLATE)
    overlap['spec']['nodeSelector'] = selector
    with pytest.raises(ValueError, match='overlapping'):
        vm.plan({'template': 'vm-bond', 'macs': MACS}, HOST, {'site': 'RZ3'}, [overlap], NETWORKS)


def test_actual_node_labels_rechecked_after_join():
    node = copy.deepcopy(NODE)
    node['metadata']['labels']['uplink-layout'] = 'old'
    with pytest.raises(ValueError, match='overlapping'):
        render(node=node)


@pytest.mark.parametrize('change', ['uid', 'mode', 'deleted', 'absent', 'network'])
def test_changed_or_missing_template_or_network_refused(change):
    config = copy.deepcopy(TEMPLATE)
    networks = copy.deepcopy(NETWORKS)
    if change == 'uid': config['metadata']['uid'] = 'replacement'
    if change == 'mode': config['spec']['uplink']['bondOptions']['mode'] = 'active-backup'
    if change == 'deleted': config['metadata']['deletionTimestamp'] = 'now'
    if change == 'network': networks = []
    with pytest.raises(ValueError):
        render(configs=[] if change == 'absent' else [config], networks=networks)


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'enslaved', 'down', 'virtual', 'bad-name'])
def test_runtime_nic_guard(change):
    links = copy.deepcopy(LINKS)
    if change == 'missing': links.pop()
    if change == 'duplicate': links.append(copy.deepcopy(links[0]))
    if change == 'enslaved': links[0]['masterIndex'] = 11
    if change == 'down': links[0]['state'] = 'down'
    if change == 'virtual': links[0]['type'] = 'bond'
    if change == 'bad-name': links[0]['name'] = 'bad;name'
    with pytest.raises(ValueError): render(links=links)


def test_node_with_vmis_refused_but_other_hosts_allowed():
    with pytest.raises(ValueError, match='VMIs'):
        render(vmis=[{'status': {'nodeName': HOST}}])
    render(vmis=[{'status': {'nodeName': 'other-node'}}])


def status(ready='True', node=HOST, name=None):
    return {'metadata': {}, 'status': {'vlanConfig': name or prepared()['name'], 'node': node,
            'conditions': [{'type': 'ready', 'status': ready}]}}


def test_readiness_requires_exact_host_config_and_ready_condition():
    assert vm.ready([status()], prepared()['name'], HOST)
    for items in ([], [status('False')], [status(node='other')], [status(name='other')], [status(), status()]):
        assert not vm.ready(items, prepared()['name'], HOST)


def app_helpers():
    """Execute the real helpers, with only API/clock dependencies substituted."""
    tree = ast.parse((ROOT / 'web/app.py').read_text())
    names = {'_bm_vm_items', '_bm_vm_plan', '_bm_vm_apply'}
    code = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[])
    ns = {'_bmvm': vm, 'json': json, 'subprocess': subprocess, '_kubectl_for_cluster': lambda c: '/test/kubeconfig',
          '_cluster_for_server': lambda s: 'test-cluster', 'time': SimpleNamespace(time=lambda: 100, sleep=Mock())}
    exec(compile(code, '<actual-app-helpers>', 'exec'), ns)
    def get(kc, action, kind, *args, **kwargs):
        if kind == vm.LM: return {'status': {'linkStatus': {HOST: LINKS}}}
        return {'items': {'nodes': [NODE], vm.VC: [TEMPLATE], vm.CN: NETWORKS,
                          'virtualmachineinstances.kubevirt.io': [], vm.VS: [status()]}[kind]}
    ns['_kubectl_json'] = Mock(side_effect=get)
    desired = render()
    ns['_kubectl_run'] = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(desired)))
    return ns


def test_disabled_feature_makes_no_api_calls():
    ns = app_helpers()
    assert ns['_bm_vm_plan']({}, {}) is None
    ns['_kubectl_json'].assert_not_called()


def test_preflight_uses_rendered_management_and_labels():
    ns = app_helpers()
    data = {'mode': 'join', 'server_url': 'https://cluster.test',
            'vm_network': {'template': 'vm-bond', 'macs': MACS}}
    cfg = {'os': {'hostname': HOST}, 'install': {'management_interface': {'interfaces': [{'hwAddr': m} for m in MGMT]}}}
    p = ns['_bm_vm_plan'](data, cfg)
    assert p['cluster'] == 'test-cluster'
    data['vm_network']['macs'] = MGMT
    with pytest.raises(ValueError, match='management'): ns['_bm_vm_plan'](data, cfg)
    data['mode'] = 'create'
    with pytest.raises(ValueError, match='joining'): ns['_bm_vm_plan'](data, cfg)


def test_read_failure_does_not_become_empty_inventory():
    ns = app_helpers()
    ns['_kubectl_json'].return_value = None
    ns['_kubectl_json'].side_effect = None
    with pytest.raises(ValueError, match='cannot read'): ns['_bm_vm_items']('/test', vm.VC)


def apply(ns, cancelled=False, deadline=200):
    p = prepared()
    p['cluster'] = 'test-cluster'
    return ns['_bm_vm_apply']({'_vm_network_plan': p}, SimpleNamespace(_cancel=cancelled), Mock(), deadline)


def test_apply_dry_runs_then_creates_exactly_once_and_waits_ready():
    ns = app_helpers()
    apply(ns)
    calls = ns['_kubectl_run'].call_args_list
    assert len(calls) == 2
    assert '--dry-run=server' in calls[0].args[0]
    assert '--dry-run=server' not in calls[1].args[0]
    assert json.loads(calls[0].kwargs['input']) == render()
    assert calls[0].kwargs['input'] == calls[1].kwargs['input']
    assert all('create' in c.args[0] for c in calls)


@pytest.mark.parametrize('failure', ['dry-refused', 'mutated', 'create-refused', 'timeout', 'cancelled'])
def test_apply_never_retries_or_cleans_up_on_failure(failure):
    ns = app_helpers()
    ok = SimpleNamespace(returncode=0, stdout=json.dumps(render()))
    bad = SimpleNamespace(returncode=1, stdout='')
    changed = render(); changed['spec']['uplink']['nics'] = ['wrong']
    if failure == 'dry-refused': ns['_kubectl_run'].side_effect = [bad]
    if failure == 'mutated': ns['_kubectl_run'].side_effect = [SimpleNamespace(returncode=0, stdout=json.dumps(changed))]
    if failure == 'create-refused': ns['_kubectl_run'].side_effect = [ok, bad]
    if failure == 'timeout': ns['_kubectl_run'].side_effect = [ok, TimeoutError('uncertain')]
    with pytest.raises((ValueError, TimeoutError)):
        apply(ns, cancelled=failure == 'cancelled')
    assert ns['_kubectl_run'].call_count <= 2
    assert all('create' in c.args[0] for c in ns['_kubectl_run'].call_args_list)


def test_readiness_timeout_leaves_created_resource_for_review():
    ns = app_helpers()
    original = ns['_kubectl_json'].side_effect
    ns['_kubectl_json'].side_effect = lambda kc, verb, kind, *a, **kw: (
        {'items': [status('False')]} if kind == vm.VS else original(kc, verb, kind, *a, **kw))
    ns['time'].sleep.side_effect = lambda seconds: setattr(ns['time'], 'time', lambda: 201)
    with pytest.raises(ValueError, match='readiness timeout'): apply(ns)
    assert ns['_kubectl_run'].call_count == 2


def route_helpers():
    ns = app_helpers()
    tree = ast.parse((ROOT / 'web/app.py').read_text())
    for function in tree.body:
        if isinstance(function, ast.FunctionDef) and function.name in {'api_baremetal_config_preview', '_bm_prepare_install', 'api_bm_vm_network_options'}:
            function.decorator_list = []
            exec(compile(ast.Module(body=[function], type_ignores=[]), '<actual-app-route>', 'exec'), ns)
    cfg = {'os': {'hostname': HOST, 'password': 'private-password'}, 'token': 'private-token',
           'install': {'management_interface': {'interfaces': [{'hwAddr': m} for m in MGMT]}}}
    ns.update({'re': __import__('re'), 'jsonify': lambda value: value,
               '_bm_render_boot_checked': lambda data: copy.deepcopy(cfg),
               '_bm_apply_import': lambda data: None, 'InstallConfigError': type('InstallConfigError', (Exception,), {}),
               '_bm_own_disk_names': lambda data: [], '_bm_nic_warning': lambda data: '',
               '_bm_boot_network_args': lambda *a: 'boot-args', '_BM_MASK': 'MASKED',
               '_his': SimpleNamespace(dump_install_config=json.dumps, _split_list=lambda value: value or [], _truthy=bool),
               '_hh': SimpleNamespace(check_pools=lambda value: value or []),
               '_bm_pool_disk_refusals': lambda data: [], '_bm_disk_role_check': lambda data: [],
               '_safe_artifact_name': lambda name: name, '_bm_artifact_base_urls': lambda *args: (),
               '_bm_config_refusal': Mock()})
    return ns


def test_preview_separates_post_join_plan_and_keeps_secrets_masked():
    ns = route_helpers()
    data = {'mode': 'join', 'vm_network': {'template': 'vm-bond', 'macs': MACS},
            'server_url': 'https://cluster.test'}
    ns['request'] = SimpleNamespace(get_json=lambda **kwargs: data)
    result = ns['api_baremetal_config_preview']()
    assert result['vm_network']['macs'] == MACS
    assert 'private-password' not in str(result)
    assert 'private-token' not in str(result)
    assert 'vm_network' not in json.loads(result['yaml'])


def install_data():
    return {'mode': 'join', 'server_url': 'https://cluster.test', 'hostname': HOST,
            'bmc_host': '192.0.2.3', 'bmc_user': 'test', 'bmc_password': 'mock-only',
            'iso': 'harvester.iso', 'device': '/dev/sda', 'mgmt_interfaces': MGMT, 'token': 'mock-token',
            '_vm_network_plan': {'name': 'untrusted-client-value'},
            'vm_network': {'template': 'vm-bond', 'macs': MACS}}


def test_launch_rebuilds_plan_instead_of_trusting_client():
    ns = route_helpers()
    result, error = ns['_bm_prepare_install'](install_data())
    assert error is None
    assert result['_vm_network_plan']['name'] == prepared()['name']
    assert ns['_kubectl_run'].call_count == 0


def test_launch_refuses_overlap_before_creating_install_action():
    ns = route_helpers()
    template = copy.deepcopy(TEMPLATE)
    template['spec']['nodeSelector'] = {}
    original = ns['_kubectl_json'].side_effect
    ns['_kubectl_json'].side_effect = lambda kc, verb, kind, *a, **kw: (
        {'items': [template]} if kind == vm.VC else original(kc, verb, kind, *a, **kw))
    result, error = ns['_bm_prepare_install'](install_data())
    assert result is None
    assert error[1] == 400
    assert 'overlapping' in error[0]['error']
    ns['_kubectl_run'].assert_not_called()


def test_waits_for_first_linkmonitor_report_after_node_ready():
    ns = app_helpers()
    original = ns['_kubectl_json'].side_effect
    reports = iter([{}, {'status': {'linkStatus': {HOST: LINKS}}}])
    ns['_kubectl_json'].side_effect = lambda kc, verb, kind, *a, **kw: (
        next(reports) if kind == vm.LM else original(kc, verb, kind, *a, **kw))
    apply(ns)
    ns['time'].sleep.assert_called_once_with(3)
    assert ns['_kubectl_run'].call_count == 2


@pytest.mark.parametrize('kind', ['missing', 'different', 'witness'])
def test_hostname_label_and_non_witness_identity_required(kind):
    node = copy.deepcopy(NODE)
    if kind == 'missing': node['metadata']['labels'].clear()
    if kind == 'different': node['metadata']['labels']['kubernetes.io/hostname'] = 'different'
    if kind == 'witness': node['metadata']['labels']['node-role.harvesterhci.io/witness'] = 'true'
    with pytest.raises(ValueError, match='hostname label'): render(node=node)


def test_duplicate_hostname_labels_do_not_target_two_hosts():
    ns = app_helpers()
    other = copy.deepcopy(NODE)
    other['metadata']['name'] = 'another-host'
    original = ns['_kubectl_json'].side_effect
    ns['_kubectl_json'].side_effect = lambda kc, verb, kind, *a, **kw: (
        {'items': [NODE, other]} if kind == 'nodes' else original(kc, verb, kind, *a, **kw))
    with pytest.raises(ValueError, match='uniquely identify'): apply(ns)
    ns['_kubectl_run'].assert_not_called()
