"""Real bare-metal dialog, isolated browser and mocked APIs: no host actions."""
import json
from pathlib import Path

from playwright.sync_api import expect

ROOT = Path(__file__).resolve().parents[2]
HOST = '192.0.2.31'
MACS = ['02:00:00:00:00:%02x' % i for i in range(1, 5)]


def test_optional_vm_nics_preview_and_launch_payload(context):
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    captured = []
    template = {'name': 'vm-bond', 'spec': {'clusterNetwork': 'k8s-vms',
                'uplink': {'nics': ['old0', 'old1'], 'bondOptions': {'mode': '802.3ad'}, 'linkAttributes': {'mtu': 1500}}}}
    node = {'ok': True, 'host': HOST, 'power_state': 'Off', 'virtualmedia_path': '/media',
            'boot_targets': ['Cd'], 'nics': [{'mac': m, 'name': 'NIC'} for m in MACS]}
    imported = {'mode': 'join', 'server_url': 'https://cluster.example.test', 'hostname': 'node4',
                'device': '/dev/sda', 'mgmt_interfaces': MACS[:2], 'method': 'dhcp'}
    def api(route):
        path = route.request.url.split('/api/', 1)[-1]
        if path == 'bmc/discover': body = {'nodes': [node]}
        elif path == 'isos': body = {'isos': [{'name': 'harvester.iso'}]}
        elif path.startswith('baremetal/inventory/'): body = {}
        elif path == 'baremetal/config/parse':
            body = {'form': imported, 'advanced': '', 'notes': [], 'import_id': 'opaque', 'has_token': True, 'has_password': True}
        elif path.startswith('baremetal/vm-network/options'):
            body = {'templates': [template]}
        elif path == 'baremetal/config/preview':
            captured.append(route.request.post_data_json)
            body = {'yaml': 'install:\n  mode: join\n', 'vm_network': {
                'name': 'bm-vm-example', 'hostname': 'node4', 'cluster_network': 'k8s-vms',
                'nodeSelector': {'kubernetes.io/hostname': 'node4'}, 'macs': MACS[2:],
                'template': {'metadata': {'name': 'vm-bond'}, 'spec': template['spec']}}}
        elif path == 'baremetal/install':
            captured.append(route.request.post_data_json)
            body = {'action_id': 'mock-install-only'}
        else: body = {}
        route.fulfill(content_type='application/json', body=json.dumps(body))
    page.route('https://uplinks.test/api/**', api)
    page.route('https://uplinks.test/', lambda r: r.fulfill(content_type='text/html', body='<div id="bmc-body"></div>'))
    page.goto('https://uplinks.test/')
    page.evaluate("localStorage.setItem('harvester_ops_language', 'en')")
    page.add_style_tag(path=str(ROOT / 'web/static/css/style.css'))
    for name in ('icons.js', 'i18n.js', 'floating-panels.js', 'bmc.js'):
        page.add_script_tag(path=str(ROOT / 'web/static/js' / name))
    page.evaluate('BMC.init(); BMC.render()')
    form = page.locator('#bmc-discover-form')
    form.locator('[name="hosts"]').fill(HOST)
    form.locator('[name="password"]').fill('mock-bmc-password')
    form.locator('button[type="submit"]').click()
    page.locator('.bmc-install').click()
    win = page.locator('#bm-install-form')
    expect(win.locator('.bm-vm-network')).to_be_hidden()
    win.locator('[data-bm="file"]').set_input_files({'name': 'join.yaml', 'mimeType': 'text/yaml', 'buffer': b'install: {mode: join}'})
    expect(win.locator('.bm-vm-network')).to_be_visible()
    expect(win.locator('[name="vm_nic"]:checked')).to_have_count(0)
    expect(win.locator('[name="mgmt_nic"]:checked')).to_have_count(2)
    win.locator('[name="vm_enabled"]').check()
    expect(win.locator('[name="vm_template"] option')).to_have_count(2)
    win.locator('[name="vm_template"]').select_option('vm-bond')
    for mac in MACS[2:]: win.locator('[name="vm_nic"][value="%s"]' % mac).check()
    win.locator('[data-bm="preview"]').click()
    page.locator('.bm-preview-text').wait_for()
    assert 'bm-vm-example' in page.locator('.bm-preview-text').input_value()
    assert captured[-1]['mgmt_interfaces'] == MACS[:2]
    assert captured[-1]['vm_network'] == {'template': 'vm-bond', 'macs': MACS[2:]}
    assert 'vm_nic' not in captured[-1]
    page.locator('[id^="fp-bm-preview-"] [data-action="close"]').click()
    page.on('dialog', lambda d: d.accept())
    win.evaluate('(form) => form.dispatchEvent(new Event("submit", {bubbles:true, cancelable:true}))')
    expect(page.locator('#bm-install-result')).to_contain_text('mock-install-only')
    assert captured[-1]['vm_network']['macs'] == MACS[2:]
    win.locator('[name="vm_enabled"]').uncheck()
    win.locator('[data-bm="preview"]').click()
    page.locator('.bm-preview-text').wait_for()
    assert 'vm_network' not in captured[-1]
    assert not errors
