"""Real dialog with fixture APIs; no BMC, cluster or guest is contacted."""
import json
from pathlib import Path
import sys
from playwright.sync_api import expect
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'web'))
import baremetal_onboarding as profiles


def test_storage_profiles_preview_launch_disable_and_vm_uplinks(context, flask_server):
    context.add_init_script("localStorage.setItem('harvester_ops_language', 'en');")
    page = context.new_page()
    errors, captured = [], []
    page.on('pageerror', lambda e: errors.append(str(e)))
    host = '192.0.2.31'
    macs = ['02:00:00:00:00:%02x' % i for i in range(1, 9)]
    node = {'ok': True, 'host': host, 'power_state': 'Off', 'virtualmedia_path': '/media',
            'boot_targets': ['Cd'], 'nics': [{'mac': m, 'name': 'NIC'} for m in macs]}
    def api(route):
        path = route.request.url.split('/api/', 1)[-1]
        if path == 'bmc/discover': body = {'nodes': [node]}
        elif path == 'isos': body = {'isos': [{'name': 'harvester.iso'}]}
        elif path.startswith('baremetal/inventory/'): body = {}
        elif path == 'baremetal/config/parse': body = {'form': {'mode': 'join', 'server_url': 'https://cluster.example', 'hostname': 'worker.example', 'device': '/dev/sdb', 'mgmt_interfaces': macs[:2], 'method': 'dhcp'}, 'advanced': '', 'notes': [], 'import_id': 'opaque', 'has_token': True, 'has_password': True}
        elif path == 'baremetal/onboarding/options': body = {'profiles': profiles.options()}
        elif path.startswith('baremetal/vm-network/options'): body = {'templates': [{'name': 'vm-bond', 'spec': {'clusterNetwork': 'vm-network', 'uplink': {'nics': ['old0', 'old1']}}}]}
        elif path in ('baremetal/config/preview', 'baremetal/install'):
            captured.append(route.request.post_data_json)
            body = {'yaml': 'install: {mode: join}'} if path.endswith('preview') else {'action_id': 'fixture-install'}
        else: route.fallback(); return
        route.fulfill(status=202 if path == 'baremetal/install' else 200, content_type='application/json', body=json.dumps(body))
    base = flask_server['base_url'];page.route(base + '/api/**', api)
    page.goto(base, wait_until='domcontentloaded')
    page.wait_for_function('window.BMC && window.FloatingPanels && window.i18n')
    page.click('#host-nav [data-hnav-root="automation"][data-hnav-tab="pxe"]')   # 2.0.0 : entrée du menu des hôtes
    page.mouse.move(1300, 450)
    form = page.locator('#bmc-discover-form');form.locator('[name="hosts"]').fill(host);form.locator('[name="password"]').fill('fixture-bmc-password');form.locator('button[type="submit"]').click()
    page.locator('.bmc-install').click();win = page.locator('#bm-install-form')
    # création de cluster : la section est masquée, comme le réseau VM
    expect(win.locator('#bm-onboarding')).to_be_hidden()
    expect(win.locator('[name="onboard_enabled"]')).not_to_be_checked()
    win.locator('[data-bm="file"]').set_input_files({'name': 'join.yaml', 'mimeType': 'text/yaml', 'buffer': b'install: {mode: join}'})
    expect(win.locator('#bm-onboarding')).to_be_visible()
    expect(win.locator('[name="onboard_enabled"]')).to_be_enabled()
    win.locator('[name="onboard_enabled"]').check()
    expect(win.locator('[name="onboard_profile"] option')).to_have_count(3)
    win.locator('[name="onboard_profile"]').select_option('storage-network-only')
    expect(win.locator('[data-onboard-wwid]')).to_be_hidden()
    # chaque contrôle porte sa bulle d'aide, et le nom Linux n'est plus demandé
    for name in ('onboard_profile', 'onboard_storage1_mac', 'onboard_storage1_address'):
        assert win.locator('label:has([name="%s"])' % name).get_attribute('data-tip')
    assert win.locator('[name="onboard_storage1_name"]').count() == 0
    for i, role in enumerate(('storage1', 'storage2')):
        win.locator('[name="onboard_%s_mac"]' % role).select_option(macs[i + 2])
        win.locator('[name="onboard_%s_address"]' % role).fill(('192.0.2.74/24', '198.51.100.74/24')[i])
    def preview():
        win.locator('[data-bm="preview"]').click();page.locator('.bm-preview-text').wait_for()
        page.locator('[id^="fp-bm-preview-"] [data-action="close"]').click()
    preview()
    assert captured[-1]['onboarding']['profile'] == 'storage-network-only'
    assert captured[-1]['onboarding']['boot_wwid'] == ''
    assert len(captured[-1]['onboarding']['ports']) == 2
    assert captured[-1]['onboarding']['ports']['storage1'] == {'mac': macs[2], 'address': '192.0.2.74/24'}
    win.locator('[name="onboard_profile"]').select_option('pure-iscsi-nvme')
    expect(win.locator('[data-onboard-wwid]')).to_be_visible()
    win.locator('[name="onboard_boot_wwid"]').fill('360000000000000000000000000000001')
    for i, role in enumerate(('iscsi1', 'iscsi2', 'nvme1', 'nvme2')):
        win.locator('[name="onboard_%s_mac"]' % role).select_option(macs[i + 2])
        win.locator('[name="onboard_%s_address"]' % role).fill(('192.0.2.74/24','198.51.100.74/24','198.18.6.74/24','198.18.10.74/24')[i])
    win.locator('[name="vm_enabled"]').check();expect(win.locator('[name="vm_template"] option')).to_have_count(2)
    win.locator('[name="vm_template"]').select_option('vm-bond')
    for mac in macs[6:]:win.locator('[name="vm_nic"][value="%s"]' % mac).check()
    preview()
    assert len(captured[-1]['onboarding']['ports']) == 4
    assert captured[-1]['vm_network'] == {'template': 'vm-bond', 'macs': macs[6:]}
    page.on('dialog', lambda d:d.accept())
    win.evaluate('(form) => form.dispatchEvent(new Event("submit", {bubbles:true,cancelable:true}))')
    expect(page.locator('#bm-install-result')).to_contain_text('fixture-install')
    assert captured[-1]['onboarding']['profile'] == 'pure-iscsi-nvme'
    win.locator('[name="onboard_enabled"]').uncheck();preview()
    assert 'onboarding' not in captured[-1] and 'vm_network' in captured[-1]
    assert not any(k.startswith('onboard_') for k in captured[-1])
    assert not errors
