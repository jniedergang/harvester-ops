import ast
import io
import json
import os
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SOURCE = Path(os.environ.get('SVPADM_TEST_APP_SOURCE', str(Path(__file__).resolve().parents[2]/'web/app.py'))).read_text()
TREE = ast.parse(SOURCE)


def functions(names, env):
    module = ast.parse('')
    module.body = [n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name in names]
    exec(compile(module, '<actual-app-helpers>', 'exec'), env)


def sequence(initial='Off', off_ok=True, mount_ok=True, boot_ok=True,
             verified=True, cancelled=False, cancel_after_mount=False,
             fresh_power='Off', stuck_on=False):
    events = []
    state = [initial]
    run = SimpleNamespace(_cancel=cancelled)
    clock = SimpleNamespace(value=0)
    def sleep(seconds):
        clock.value += seconds
    def get(*args, **kwargs):
        events.append(('read', state[0]))
        if events and any(e[0] == 'boot' for e in events):
            return {'PowerState': fresh_power, 'Boot': {
                'BootSourceOverrideTarget': 'Cd' if verified else 'Hdd',
                'BootSourceOverrideEnabled': 'Once'}}
        return {'PowerState': state[0]}
    def reset(host, user, pwd, path, kind):
        events.append(('reset', kind))
        if kind == 'ForceOff':
            if not off_ok:
                return False, 'rejected'
            if not stuck_on:
                state[0] = 'Off'
        return True, ''
    def mount(*args):
        events.append(('mount', state[0]))
        if cancel_after_mount:
            run._cancel = True
        return mount_ok, 'mount rejected', {}
    def boot(*args):
        events.append(('boot', state[0]))
        return boot_ok, 'boot rejected'
    env = {'time': SimpleNamespace(time=lambda: clock.value, sleep=sleep),
           '_redfish_get': get, '_bm_reset': reset, '_bm_media_insert': mount,
           '_bm_boot_once_cd': boot, 'step': lambda *args: None,
           'fail': lambda sid, msg: ('failed', sid, msg), 'run': run,
           'host': 'fixture', 'kc_user': 'fixture', 'kc_pwd': 'unused',
           'iso_url': 'http://fixture.invalid/fixture.iso',
           'profile': {'system_path': '/redfish/v1/Systems/1/'}, 'keep_creds': [False]}
    functions({'_bm_power_off'}, env)
    start = SOURCE.index('    # --- média virtuel + amorce + allumage ---', SOURCE.index('def _baremetal_install_runner'))
    end = SOURCE.index("    # --- attente de l'installation ---", start)
    block = 'def sequence():\n' + SOURCE[start:end] + "    return 'booted'\n"
    exec(compile(block, '<actual-install-boot-sequence>', 'exec'), env)
    return env['sequence'](), events


class BootSequenceTests(unittest.TestCase):
    def test_off_host_never_boots_old_os(self):
        result, events = sequence()
        self.assertEqual(result, 'booted')
        self.assertEqual([e for e in events if e[0] != 'read'],
                         [('mount','Off'), ('boot','Off'), ('reset','On')])
    def test_on_host_is_off_before_mount_and_boot(self):
        result, events = sequence(initial='On')
        self.assertEqual(result, 'booted')
        self.assertEqual([e for e in events if e[0] != 'read'],
                         [('reset','ForceOff'), ('mount','Off'), ('boot','Off'), ('reset','On')])
    def test_unknown_state_blocks_all_mutations(self):
        result, events = sequence(initial=None)
        self.assertEqual(result[1], 'power-off')
        self.assertFalse([e for e in events if e[0] != 'read'])
    def test_off_rejected_does_not_mount_or_boot(self):
        result, events = sequence(initial='On', off_ok=False)
        self.assertEqual(result[1], 'power-off')
        self.assertEqual([e for e in events if e[0] != 'read'], [('reset','ForceOff')])
    def test_timeout_has_one_power_off_attempt(self):
        result, events = sequence(initial='On', stuck_on=True)
        self.assertEqual(result[1], 'power-off')
        self.assertEqual([e for e in events if e[0] != 'read'], [('reset','ForceOff')])
    def test_boot_rejection_never_powers_on(self):
        result, events = sequence(boot_ok=False)
        self.assertEqual(result[1], 'bmc-boot')
        self.assertNotIn(('reset','On'), events)
    def test_unverified_boot_never_powers_on(self):
        result, events = sequence(verified=False)
        self.assertEqual(result[1], 'bmc-boot')
        self.assertNotIn(('reset','On'), events)
    def test_external_power_change_blocks_power_on(self):
        result, events = sequence(fresh_power='On')
        self.assertEqual(result[1], 'bmc-boot')
        self.assertNotIn(('reset','On'), events)
    def test_cancelled_does_not_change_hardware(self):
        result, events = sequence(cancelled=True)
        self.assertEqual(result[1], 'power-off')
        self.assertEqual(events, [])
    def test_cancellation_after_mount_does_not_set_boot(self):
        result, events = sequence(cancel_after_mount=True)
        self.assertEqual(result[1], 'bmc-boot')
        self.assertFalse([e for e in events if e[0] in ('boot','reset')])
    def test_failed_mount_never_sets_boot(self):
        result, events = sequence(mount_ok=False)
        self.assertEqual(result[1], 'bmc-insert')
        self.assertFalse([e for e in events if e[0] in ('boot','reset')])
    def test_preflight_contains_no_power_commands(self):
        start = SOURCE.index('    # --- preflight:', SOURCE.index('def _baremetal_install_runner'))
        end = SOURCE.index('    # --- remasterisation ---', start)
        body = SOURCE[start:end]
        self.assertNotIn('_bm_reset(', body)
        self.assertNotIn('_redfish_send(', body)
        self.assertIn('_bmd.check_binding', body)
        self.assertIn('virtualmedia_path', body)
        self.assertIn('boot_targets', body)


class ErrorDetailsTests(unittest.TestCase):
    def call_error(self, body):
        env = {'json': json, '_redfish_etag': lambda *args: None}
        functions({'_redfish_send'}, env)
        error = urllib.error.HTTPError('https://fixture.invalid', 400, 'bad', {}, io.BytesIO(body))
        with patch('urllib.request.urlopen', side_effect=error):
            return env['_redfish_send']('fixture.invalid', '/redfish/v1/Systems/1/', 'fixture', 'unused', 'PATCH', {})
    def test_preserves_complete_error_id_without_echoing_secret(self):
        message_id = 'iLO.2.25.UnableToModifyDuringSystemPOST'
        body = json.dumps({'error': {'message': 'x'*800, '@Message.ExtendedInfo': [
            {'MessageId':message_id, 'MessageArgs':['BootSourceOverrideTarget','SENSITIVE_FIXTURE']}]}}).encode()
        ok, status, detail = self.call_error(body)
        self.assertFalse(ok)
        self.assertEqual(status, 400)
        self.assertIn(message_id, detail)
        self.assertIn('BootSourceOverrideTarget', detail)
        self.assertNotIn('SENSITIVE_FIXTURE', detail)
    def test_non_json_error_is_not_logged_raw(self):
        self.assertEqual(self.call_error(b'SENSITIVE_FIXTURE'), (False,400,'HTTP 400'))


if __name__ == '__main__':
    unittest.main()
