import ast
import copy
import os
import subprocess
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
WEB = Path(os.environ.get('BOOT_TEST_WEB', str(BASE/'web')))
sys.path.insert(0, str(WEB))
from baremetal_boot_network import boot_network_args, BootNetworkError

CFG = {'os': {'hostname':'node4.example.test', 'dns_nameservers':['198.51.100.131','198.51.100.132']},
       'install': {'mode':'join', 'management_interface': {
           'interfaces':[{'hwAddr':'F6:66:60:A0:05:45'},{'hwAddr':'F6:66:60:A0:05:46'}],
           'bond_options':{'mode':'802.3ad','miimon':'100','lacp_rate':'slow','xmit_hash_policy':'layer2'},
           'method':'static','ip':'192.0.2.73','subnet_mask':'255.255.255.0','gateway':'192.0.2.1','vlan_id':1324}}}
SCRIPT = Path(os.environ.get('BOOT_TEST_SCRIPT', str(BASE/'bin/harvester-iso-remaster.sh'))).read_text()
APP = Path(os.environ.get('BOOT_TEST_APP', str(BASE/'web/app.py'))).read_text()


def assemble(network='', extra='', kernel=''):
    start = SCRIPT.index('if [[ -n "$KERNEL_ARGS" ]]; then\n    KARGS=')
    end = SCRIPT.index('# --- 1.', start)
    body = SCRIPT[start:end]
    env = dict(os.environ, NETWORK_ARGS=network, EXTRA_ARGS=extra, KERNEL_ARGS=kernel,
               CONFIG_URL='' if kernel else 'http://fixture.invalid/config.yaml')
    return subprocess.run(['bash','-e','-c', 'log_error() { printf "%s\\n" "$*" >&2; }\n' + body + '\nprintf "%s\\n" "$KARGS"'],
                          env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(CFG)
        self.net = self.cfg['install']['management_interface']
    def test_real_target_network_is_ready_before_config_download(self):
        args = boot_network_args(self.cfg).split()
        self.assertIn('ifname=hvboot0:f6:66:60:a0:05:45',args)
        self.assertIn('ifname=hvboot1:f6:66:60:a0:05:46',args)
        self.assertIn('vlan=hvbond0.1324:hvbond0',args)
        self.assertIn('ip=192.0.2.73::192.0.2.1:255.255.255.0:node4.example.test:hvbond0.1324:none',args)
        self.assertIn('bootdev=hvbond0.1324',args)
        self.assertIn('nameserver=198.51.100.131',args)
        bond = next(x for x in args if x.startswith('bond='))
        self.assertTrue(bond.startswith('bond=hvbond0:hvboot0,hvboot1:'))
        self.assertIn('mode=802.3ad',bond)
        self.assertIn('lacp_rate=slow',bond)
        self.assertEqual(sum(x.startswith('ip=') for x in args),1)
    def test_remaster_replaces_default_dhcp(self):
        r=assemble(boot_network_args(self.cfg),'console=ttyS1,115200')
        self.assertEqual(r.returncode,0,r.stderr)
        args=r.stdout.decode().split()
        self.assertEqual(sum(x.startswith('ip=') for x in args),1)
        self.assertNotIn('ip=dhcp',args)
        self.assertIn('console=ttyS1,115200',args)
    def test_legacy_no_network_override_keeps_dhcp(self):
        r=assemble()
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('ip=dhcp',r.stdout.decode().split())
    def test_discovery_kernel_arguments_unchanged(self):
        r=assemble(kernel='systemd.run=/discover.sh ip=dhcp rd.neednet=1')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(r.stdout.decode().strip(),'systemd.run=/discover.sh ip=dhcp rd.neednet=1')
    def test_duplicate_ip_is_rejected_by_remaster(self):
        r=assemble(boot_network_args(self.cfg),'ip=dhcp')
        self.assertNotEqual(r.returncode,0)
    def test_dhcp_tagged_network_is_derived(self):
        self.net['method']='dhcp'
        args=boot_network_args(self.cfg).split()
        self.assertIn('ip=hvbond0.1324:dhcp',args)
        self.assertIn('vlan=hvbond0.1324:hvbond0',args)
    def test_untagged_static_uses_bond_directly(self):
        self.net['vlan_id']=0
        args=boot_network_args(self.cfg).split()
        self.assertFalse(any(x.startswith('vlan=') for x in args))
        self.assertIn('bootdev=hvbond0',args)
    def test_named_single_interface_is_supported(self):
        self.net['interfaces']=[{'name':'ens3f0'}]
        args=boot_network_args(self.cfg)
        self.assertIn('bond=hvbond0:ens3f0:',args)
        self.assertNotIn('ifname=',args)
    def test_mtu_is_passed_to_bond(self):
        self.net['mtu']=1500
        bond=next(x for x in boot_network_args(self.cfg).split() if x.startswith('bond='))
        self.assertTrue(bond.endswith(':1500'))
    def test_network_extra_overrides_are_rejected(self):
        for value in ('ip=dhcp','vlan=wrong:eth0','bond=bond0:eth0','nameserver=1.1.1.1','ifname=x:aa:bb:cc:dd:ee:ff'):
            with self.subTest(value=value):
                with self.assertRaises(BootNetworkError):boot_network_args(self.cfg,value)
    def test_non_network_extras_do_not_disable_checks(self):
        args=boot_network_args(self.cfg,'console=ttyS1,115200')
        self.assertNotIn('skipchecks',args)
    def test_secret_fields_never_enter_kernel_arguments(self):
        self.cfg['token']='SENSITIVE_FIXTURE';self.cfg['os']['password']='SENSITIVE_FIXTURE'
        self.assertNotIn('SENSITIVE_FIXTURE',boot_network_args(self.cfg))
    def test_bad_gateway_is_rejected(self):
        self.net['gateway']='192.168.0.1'
        with self.assertRaises(BootNetworkError):boot_network_args(self.cfg)
    def test_invalid_netmask_is_rejected(self):
        self.net['subnet_mask']='255.0.255.0'
        with self.assertRaises(BootNetworkError):boot_network_args(self.cfg)
    def test_unsupported_ipv6_is_explicitly_rejected(self):
        self.net['ip']='2001:db8::1'
        with self.assertRaises(BootNetworkError):boot_network_args(self.cfg)
    def test_duplicate_mac_is_rejected(self):
        self.net['interfaces'][1]=self.net['interfaces'][0]
        with self.assertRaises(BootNetworkError):boot_network_args(self.cfg)
    def test_missing_nics_is_rejected(self):
        self.net['interfaces']=[]
        with self.assertRaises(BootNetworkError):boot_network_args(self.cfg)
    def test_vlan_range_and_type_are_checked(self):
        for vlan in (-1,4095,True,'1324'):
            self.net['vlan_id']=vlan
            with self.assertRaises(BootNetworkError):boot_network_args(self.cfg)
    def test_injection_in_bond_is_rejected_without_echoing_value(self):
        self.net['bond_options']['mode']='SENSITIVE_FIXTURE;reboot'
        with self.assertRaises(BootNetworkError) as e:boot_network_args(self.cfg)
        self.assertNotIn('SENSITIVE_FIXTURE',str(e.exception))
    def test_profile_schema_render_remains_separate(self):
        tree=ast.parse(APP)
        plain=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_bm_render_checked')
        self.assertNotIn('_bm_boot_network_args',ast.dump(plain))
        prepare=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_bm_prepare_install')
        self.assertIn('_bm_render_boot_checked',ast.dump(prepare))
    def test_runner_and_preview_are_wired(self):
        tree=ast.parse(APP)
        runner=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_baremetal_install_runner')
        preview=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='api_baremetal_config_preview')
        self.assertIn('--network-args',ast.dump(runner))
        self.assertIn('boot_network_args',ast.dump(preview))


if __name__=='__main__':unittest.main()
