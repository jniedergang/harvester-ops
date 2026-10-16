"""VM uplink planning and the actual app orchestration, without a cluster."""
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
import app as wapp

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


def render(p=None, node=None, configs=None, networks=None, links=None):
    return vm.manifest(p or prepared(), node or NODE, [TEMPLATE] if configs is None else configs,
                       NETWORKS if networks is None else networks, LINKS if links is None else links)


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


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'enslaved', 'virtual', 'bad-name'])
def test_runtime_nic_guard(change):
    links = copy.deepcopy(LINKS)
    if change == 'missing': links.pop()
    if change == 'duplicate': links.append(copy.deepcopy(links[0]))
    if change == 'enslaved': links[0]['masterIndex'] = 11
    if change == 'virtual': links[0]['type'] = 'bond'
    if change == 'bad-name': links[0]['name'] = 'bad;name'
    with pytest.raises(ValueError): render(links=links)


@pytest.mark.parametrize("state", ["down", "unknown", "up"])
def test_free_nic_state_does_not_block_bond_creation(state):
    links = copy.deepcopy(LINKS)
    links[0]["state"] = state
    assert render(links=links)["spec"]["uplink"]["nics"] == ["ens3f6", "ens3f7"]


def test_template_hardware_address_is_not_copied():
    template = copy.deepcopy(TEMPLATE)
    template["spec"]["uplink"]["linkAttributes"]["hardwareAddr"] = "02:00:00:00:00:99"
    plan = vm.plan({"template": "vm-bond", "macs": MACS}, HOST, {}, [template], NETWORKS)
    result = render(p=plan, configs=[template])
    assert "hardwareAddr" not in result["spec"]["uplink"]["linkAttributes"]
    assert template["spec"]["uplink"]["linkAttributes"]["hardwareAddr"] == "02:00:00:00:00:99"


def status(ready='True', node=HOST, name=None):
    return {'metadata': {}, 'status': {'vlanConfig': name or prepared()['name'], 'node': node,
            'conditions': [{'type': 'ready', 'status': ready}]}}


def test_readiness_requires_exact_host_config_and_ready_condition():
    assert vm.ready([status()], prepared()['name'], HOST)
    for items in ([], [status('False')], [status(node='other')], [status(name='other')], [status(), status()]):
        assert not vm.ready(items, prepared()['name'], HOST)


@pytest.fixture(autouse=True)
def app_dependencies(monkeypatch):
    """Real app functions, with transport and clock replaced per test."""
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/test/kubeconfig")
    monkeypatch.setattr(wapp, "_cluster_for_server", lambda s: "test-cluster")
    monkeypatch.setattr(wapp, "time", SimpleNamespace(time=lambda: 100, sleep=Mock()))
    def get(kc, action, kind, *args, **kwargs):
        if kind == vm.LM:
            return {"status": {"linkStatus": {HOST: LINKS}}}
        return {"items": {"nodes": [NODE], vm.VC: [TEMPLATE], vm.CN: NETWORKS,
                          vm.VS: [status()]}[kind]}
    monkeypatch.setattr(wapp, "_kubectl_json", Mock(side_effect=get))
    monkeypatch.setattr(wapp, "_kubectl_run", Mock(
        return_value=SimpleNamespace(returncode=0, stdout=json.dumps(render()))))


def app_helpers():
    return vars(wapp)


@pytest.fixture
def client():
    wapp.app.config["TESTING"] = True
    return wapp.app.test_client()


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
    return ns['_bm_vm_apply']({'_vm_network_plan': p, 'cluster': 'test-cluster', '_cluster_kc': '/delegated/kubeconfig'}, SimpleNamespace(_cancel=cancelled), Mock(), deadline)


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
    failed = status('False')
    failed['status']['conditions'][0]['message'] = 'agent refused bond'
    ns['_kubectl_json'].side_effect = lambda kc, verb, kind, *a, **kw: (
        {'items': [failed]} if kind == vm.VS else original(kc, verb, kind, *a, **kw))
    ns['time'].sleep.side_effect = lambda seconds: setattr(ns['time'], 'time', lambda: 201)
    with pytest.raises(ValueError, match='readiness timeout.*agent refused bond'):
        apply(ns)
    assert ns['_kubectl_run'].call_count == 2


def route_helpers(monkeypatch):
    cfg = {"os": {"hostname": HOST, "password": "private-password"}, "token": "private-token",
           "install": {"management_interface": {"interfaces": [{"hwAddr": m} for m in MGMT]}}}
    monkeypatch.setattr(wapp, "_bm_render_boot_checked", lambda data: copy.deepcopy(cfg))
    monkeypatch.setattr(wapp, "_bm_apply_import", lambda data: None)
    monkeypatch.setattr(wapp, "_bm_own_disk_names", lambda data: [])
    monkeypatch.setattr(wapp, "_bm_nic_warning", lambda data: "")
    monkeypatch.setattr(wapp, "_bm_boot_network_args", lambda *a: "boot-args")
    monkeypatch.setattr(wapp, "_bm_pool_disk_refusals", lambda data: [])
    monkeypatch.setattr(wapp, "_bm_disk_role_check", lambda data: [])
    monkeypatch.setattr(wapp, "_bm_artifact_base_urls", lambda *a: ())
    return vars(wapp)


def test_preview_separates_post_join_plan_and_keeps_secrets_masked(client, monkeypatch):
    route_helpers(monkeypatch)
    response = client.post("/api/baremetal/config/preview", json={
        "mode": "join", "vm_network": {"template": "vm-bond", "macs": MACS},
        "server_url": "https://cluster.test"})
    assert response.status_code == 200
    result = response.get_json()
    assert result["vm_network"]["macs"] == MACS
    assert "private-password" not in str(result)
    assert "private-token" not in str(result)
    assert "vm_network" not in result["yaml"]


@pytest.mark.parametrize("failure", [None, "undeclared", "read"])
def test_options_route(client, monkeypatch, failure):
    if failure == "undeclared":
        monkeypatch.setattr(wapp, "_cluster_for_server", lambda s: None)
    if failure == "read":
        monkeypatch.setattr(wapp, "_kubectl_json", lambda *a, **kw: None)
    response = client.get("/api/baremetal/vm-network/options?server_url=https://cluster.test")
    assert response.status_code == (200 if failure is None else 400)
    if failure is None:
        assert response.get_json()["templates"] == [{"name": "vm-bond", "spec": TEMPLATE["spec"]}]


def install_data():
    return {'mode': 'join', 'server_url': 'https://cluster.test', 'hostname': HOST,
            'bmc_host': '192.0.2.3', 'bmc_user': 'test', 'bmc_password': 'mock-only',
            'iso': 'harvester.iso', 'device': '/dev/sda', 'mgmt_interfaces': MGMT, 'token': 'mock-token',
            '_vm_network_plan': {'name': 'untrusted-client-value'},
            'vm_network': {'template': 'vm-bond', 'macs': MACS}}


def test_launch_rebuilds_plan_instead_of_trusting_client(monkeypatch):
    ns = route_helpers(monkeypatch)
    with wapp.app.test_request_context():
        result, error = ns['_bm_prepare_install'](install_data())
    assert error is None
    assert result['_vm_network_plan']['name'] == prepared()['name']
    assert ns['_kubectl_run'].call_count == 0


def test_launch_refuses_overlap_before_creating_install_action(monkeypatch):
    ns = route_helpers(monkeypatch)
    template = copy.deepcopy(TEMPLATE)
    template['spec']['nodeSelector'] = {}
    original = ns['_kubectl_json'].side_effect
    ns['_kubectl_json'].side_effect = lambda kc, verb, kind, *a, **kw: (
        {'items': [template]} if kind == vm.VC else original(kc, verb, kind, *a, **kw))
    with wapp.app.test_request_context():
        result, error = ns['_bm_prepare_install'](install_data())
    assert result is None
    assert error[1] == 400
    assert 'overlapping' in error[0].get_json()['error']
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


def test_delegated_kubeconfig_used_for_all_post_join_reads_and_writes(monkeypatch):
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", Mock(side_effect=AssertionError("worker lookup")))
    apply(app_helpers())
    assert all(call.args[0] == "/delegated/kubeconfig" for call in wapp._kubectl_json.call_args_list)
    assert all(call.args[0][2] == "/delegated/kubeconfig" for call in wapp._kubectl_run.call_args_list)


@pytest.mark.parametrize("read_failure", [ValueError("read failed"), OSError("temporary")])
def test_transient_status_read_is_retried_and_agent_message_reported(read_failure):
    ns = app_helpers()
    original = ns["_kubectl_json"].side_effect
    failed = status("False")
    failed["status"]["conditions"][0]["message"] = "bond refused by agent"
    replies = iter([read_failure, {"items": [failed]}, {"items": [status()]}])
    def get(kc, verb, kind, *args, **kwargs):
        if kind == vm.VS:
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply
        return original(kc, verb, kind, *args, **kwargs)
    ns["_kubectl_json"].side_effect = get
    step = Mock()
    p = prepared()
    wapp._bm_vm_apply({"_vm_network_plan": p, "_cluster_kc": "/delegated/kubeconfig"},
                     SimpleNamespace(_cancel=False), step, 200)
    assert any("bond refused by agent" in call.args[2] for call in step.call_args_list)
    assert step.call_args_list[-1].args[1] == "done"
    assert ns["_kubectl_run"].call_count == 2


@pytest.mark.parametrize("failure", [None, "pools", "vm-network"])
def test_install_runner_runs_pools_before_vm_network(monkeypatch, tmp_path, failure):
    """Exercise the real runner through mocked BMC, remaster and cluster transports."""
    events = []
    (tmp_path / "harvester.iso").write_bytes(b"fixture")
    opts = dict(install_data(), cluster="test-cluster", _cluster_kc="/delegated/kubeconfig",
                _vm_network_plan=prepared(), pools=[{"tag": "data", "replicas": 1}])
    run = SimpleNamespace(id="fixture", _cancel=False, emit=Mock(), close=Mock())
    profile = {"ok": True, "power_state": "Off", "virtualmedia_path": "/media",
               "boot_targets": ["Cd"], "uefi_targets": ["HD.fixture"], "system_path": "/system"}
    replacements = {
        "_bmc_discover_one": lambda *a: profile,
        "_bm_nic_warning": lambda *a: None,
        "_iso_dir": lambda: tmp_path,
        "_iso_work_dir": lambda: tmp_path,
        "_bm_artifact_base_urls": lambda *a: ("http://fixture", "http://fixture"),
        "_bm_join_pubkey": lambda *a: None,
        "_harvester_install_config": lambda *a, **kw: "{}",
        "_bm_boot_network_args": lambda *a: "",
        "_bm_remaster_stream": lambda *a: 0,
        "_bm_power_off": lambda *a: (True, ""),
        "_bm_media_insert": lambda *a: (True, "", {}),
        "_bm_boot_once_cd": lambda *a: (True, ""),
        "_redfish_get": lambda *a, **kw: {"PowerState": "Off", "Boot": {
            "BootSourceOverrideTarget": "Cd", "BootSourceOverrideEnabled": "Once"}},
        "_bm_reset": lambda *a: (True, ""),
        "_bm_wait_power_off": lambda *a, **kw: True,
        "_bm_media_eject": lambda *a: (True, ""),
        "_bm_boot_once_target": lambda *a: (True, ""),
        "_bm_wait_node_ready": lambda *a: True,
        "_bm_add_joined_node": lambda *a: "joined",
    }
    for name, replacement in replacements.items():
        monkeypatch.setattr(wapp, name, replacement)
    monkeypatch.setattr(wapp._bmd, "load_inventory", lambda *a: None)
    monkeypatch.setattr(wapp.pxe_server, "start", lambda: 8091)
    monkeypatch.setattr(wapp.pxe_server, "issue", lambda *a: "fixture")
    monkeypatch.setattr(wapp.pxe_server, "revoke", Mock())
    def pools(*args):
        events.append("pools")
        assert args[1] == "/delegated/kubeconfig"
        return (1, "pool failure") if failure == "pools" else (0, "")
    def network(*args):
        events.append("vm-network")
        if failure == "vm-network":
            raise ValueError("agent refused bond")
    monkeypatch.setattr(wapp, "_bm_apply_pools", pools)
    monkeypatch.setattr(wapp, "_bm_vm_apply", network)
    wapp._baremetal_install_runner(run, opts)
    assert events == (["pools"] if failure == "pools" else ["pools", "vm-network"])
    assert run.status == ("done" if failure is None else "error")
    if failure:
        assert failure in run.error_summary
    run.close.assert_called_once()


def test_down_link_warning_does_not_prevent_create(monkeypatch):
    original = wapp._kubectl_json.side_effect
    links = copy.deepcopy(LINKS)
    links[0]["state"] = "down"
    def get(kc, verb, kind, *args, **kwargs):
        if kind == vm.LM:
            return {"status": {"linkStatus": {HOST: links}}}
        return original(kc, verb, kind, *args, **kwargs)
    monkeypatch.setattr(wapp._kubectl_json, "side_effect", get)
    step = Mock()
    wapp._bm_vm_apply({"_vm_network_plan": prepared(), "_cluster_kc": "/delegated/kubeconfig"},
                     SimpleNamespace(_cancel=False), step, 200)
    assert any("not up yet: ens3f6" in call.args[2] for call in step.call_args_list)
    assert wapp._kubectl_run.call_count == 2


def test_options_hide_management_and_deleting_templates(client, monkeypatch):
    management = copy.deepcopy(TEMPLATE)
    management["spec"]["clusterNetwork"] = "mgmt"
    deleting = copy.deepcopy(TEMPLATE)
    deleting["metadata"]["deletionTimestamp"] = "now"
    monkeypatch.setattr(wapp, "_kubectl_json", lambda *a, **kw: {"items": [TEMPLATE, management, deleting]})
    response = client.get("/api/baremetal/vm-network/options?server_url=https://cluster.test")
    assert response.status_code == 200
    assert len(response.get_json()["templates"]) == 1


@pytest.mark.parametrize("overlap", [False, True])
def test_launch_route_revalidates_before_starting_worker(client, monkeypatch, overlap):
    route_helpers(monkeypatch)
    track = Mock(return_value=("fixture-action", None))
    monkeypatch.setattr(wapp, "_bm_track", track)
    if overlap:
        template = copy.deepcopy(TEMPLATE)
        template["spec"]["nodeSelector"] = {}
        original = wapp._kubectl_json.side_effect
        monkeypatch.setattr(wapp._kubectl_json, "side_effect",
                            lambda kc, verb, kind, *a, **kw: (
                                {"items": [template]} if kind == vm.VC
                                else original(kc, verb, kind, *a, **kw)))
    response = client.post("/api/baremetal/install", json=install_data())
    assert response.status_code == (400 if overlap else 202)
    if overlap:
        track.assert_not_called()
    else:
        assert track.call_args.args[3]["_vm_network_plan"]["name"] == prepared()["name"]
        assert "_cluster_kc" not in response.get_data(as_text=True)
    wapp._kubectl_run.assert_not_called()
