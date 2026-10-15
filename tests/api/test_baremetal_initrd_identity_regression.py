import gzip
import hashlib
from unittest.mock import patch
import importlib.util
import io
import lzma
import os
from pathlib import Path
import re
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('identity', os.environ.get('INITRD_TEST_HELPER', str(ROOT / 'bin/lib/baremetal_initrd_identity.py')))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
ORIGINAL = ROOT / 'tests/fixtures/ifname-genrules.sh'


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.assertEqual(m.EXPECTED, "1530e28e30c0a6b6f2502fa87d807e2226b9a3303467a389045638e4d5081d9e")
        guard = patch.object(m, "EXPECTED", hashlib.sha256(ORIGINAL.read_bytes()).hexdigest())
        guard.start()
        self.addCleanup(guard.stop)
        self.hook = m.patched_hook(ORIGINAL.read_bytes()).decode()

    def generated_rules(self):
        # Execute the actual patched shell hook with only dracut argument
        # parsing stubbed; inspect the resulting udev rules, not a second
        # implementation of the generator.
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            s = self.hook.replace('/tmp/ifname-', tmp + '/ifname-')
            s = s.replace('/etc/udev/rules.d/80-ifname.rules', tmp + '/rules')
            prelude = '''getarg() { return 0; }
getargs() { echo hvboot0:f6:66:60:a0:05:45 hvboot1:f6:66:60:a0:05:46; }
parse_ifname_opts() { ifname_if=${1%%:*}; ifname_mac=${1#*:}; }
'''
            subprocess.check_call(['sh', '-c', prelude + s])
            return (Path(tmp) / 'rules').read_text().splitlines()

    def match(self, current, permanent=None):
        names = []
        for rule in self.generated_rules():
            if 'TEST=="bonding_slave"' in rule:
                if permanent is None:
                    continue
                attribute = 'bonding_slave/perm_hwaddr'
                actual = permanent
            else:
                if permanent is not None:
                    continue
                attribute = 'address'
                actual = current
            value = re.search(re.escape('ATTR{' + attribute + '}=="') + '([^"]+)', rule).group(1)
            if value == actual:
                names.append(re.search('NAME="([^"]+)"', rule).group(1))
        return names

    def test_initial_unbonded_names(self):
        self.assertEqual(self.match('f6:66:60:a0:05:45'), ['hvboot0'])
        self.assertEqual(self.match('f6:66:60:a0:05:46'), ['hvboot1'])

    def test_bond_uses_first_mac(self):
        self.assertEqual(self.match('f6:66:60:a0:05:45', 'f6:66:60:a0:05:45'), ['hvboot0'])
        self.assertEqual(self.match('f6:66:60:a0:05:45', 'f6:66:60:a0:05:46'), ['hvboot1'])

    def test_bond_uses_second_mac(self):
        self.assertEqual(self.match('f6:66:60:a0:05:46', 'f6:66:60:a0:05:45'), ['hvboot0'])
        self.assertEqual(self.match('f6:66:60:a0:05:46', 'f6:66:60:a0:05:46'), ['hvboot1'])

    def test_other_nic_untouched(self):
        self.assertEqual(self.match('f6:66:60:a0:05:47'), [])
        self.assertEqual(self.match('f6:66:60:a0:05:45', 'f6:66:60:a0:05:47'), [])

    def test_no_fallback_when_permanent_is_unavailable(self):
        self.assertEqual(self.match('f6:66:60:a0:05:45', ''), [])

    def test_unknown_hook_refused(self):
        with self.assertRaises(ValueError):
            m.patched_hook(ORIGINAL.read_bytes() + b'# unexpected update\n')

    def test_patch_archive_overlay_and_source_unchanged(self):
        early = m.cpio_entry('early_cpio', b'1') + m.cpio_entry('TRAILER!!!', b'', 0)
        main = m.cpio_entry(m.HOOK, ORIGINAL.read_bytes()) + m.cpio_entry('TRAILER!!!', b'', 0)
        content = early + b'\0' * ((-len(early)) % 512) + lzma.compress(main)
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / 'source', Path(tmp) / 'target'
            source.write_bytes(content)
            m.patch_initrd(source, target)
            self.assertEqual(source.read_bytes(), content)
            result = target.read_bytes()
            self.assertEqual(result[:len(content)], content)
            overlay = gzip.decompress(result[(len(content) + 3) // 4 * 4:])
            self.assertEqual(dict(m.entries(io.BytesIO(overlay)))[m.HOOK], self.hook.encode())
            with self.assertRaises(FileExistsError):
                m.patch_initrd(source, target)

    def test_in_place_patch_refused(self):
        with self.assertRaises(ValueError):
            m.patch_initrd(ORIGINAL, ORIGINAL)


if __name__ == '__main__':
    unittest.main()
