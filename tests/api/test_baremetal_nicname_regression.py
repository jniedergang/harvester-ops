import ast
import copy
import importlib.util
import os
import sys
import unittest
from pathlib import Path

BASE=Path(__file__).resolve().parents[2]
WEB=Path(str(BASE/'web'))
sys.path.insert(0,str(WEB))
from baremetal_boot_network import installer_boot_identity,boot_network_args,BootNetworkError

CFG={'scheme_version':1,'os':{'hostname':'node4.example.test'},'install':{'mode':'join','management_interface':{
    'interfaces':[{'hwAddr':'F6:66:60:A0:05:45'},{'hwAddr':'F6:66:60:A0:05:46'}],
    'bond_options':{'mode':'802.3ad','miimon':'100'},'method':'static','ip':'192.0.2.73',
    'subnet_mask':'255.255.255.0','gateway':'192.0.2.1','vlan_id':1324}}}


class NicIdentityTests(unittest.TestCase):
    def setUp(self):self.cfg=copy.deepcopy(CFG)
    def test_bond_mac_change_does_not_require_late_mac_lookup(self):
        out=installer_boot_identity(self.cfg)
        live={'hvboot0':'f6:66:60:a0:05:45','hvboot1':'f6:66:60:a0:05:45'}
        interfaces=out['install']['management_interface']['interfaces']
        self.assertEqual([x['name'] for x in interfaces],list(live))
        self.assertTrue(all(x['name'] in live for x in interfaces))
        self.assertEqual(interfaces[1]['hwAddr'],'f6:66:60:a0:05:46')
    def test_mapping_survives_installed_boot(self):
        out=installer_boot_identity(self.cfg)
        persisted=out['os']['additional_kernel_arguments'].split()
        # 1.88.0 : multipath=off, défaut de l'installeur sans stockage externe, est gardé
        self.assertEqual(persisted,['multipath=off','ifname=hvboot0:f6:66:60:a0:05:45','ifname=hvboot1:f6:66:60:a0:05:46'])
        persisted=[x for x in persisted if x.startswith('ifname=')]
        early=[x for x in boot_network_args(out).split() if x.startswith('ifname=')]
        self.assertEqual(early,persisted)
    def test_identity_transform_does_not_modify_plan_or_secrets(self):
        self.cfg['token']='SENSITIVE_FIXTURE';self.cfg['os']['password']='SENSITIVE_FIXTURE'
        before=copy.deepcopy(self.cfg);out=installer_boot_identity(self.cfg)
        self.assertEqual(self.cfg,before)
        self.assertEqual(out['token'],before['token'])
        self.assertEqual(out['os']['password'],before['os']['password'])
        self.assertNotIn('SENSITIVE_FIXTURE',out['os']['additional_kernel_arguments'])
    def test_idempotent(self):
        out=installer_boot_identity(self.cfg)
        self.assertEqual(installer_boot_identity(out),out)
    def test_installed_boot_rules_use_permanent_identity_and_final_names(self):
        out=installer_boot_identity(self.cfg)
        rules=out['os']['write_files'][0]
        self.assertEqual(rules['path'],'/etc/udev/rules.d/79-harvester-bond-identity.rules')
        self.assertIn('ATTR{bonding_slave/perm_hwaddr}=="f6:66:60:a0:05:46"',rules['content'])
        self.assertIn('NAME:="hvboot1"',rules['content'])
        self.assertEqual(rules['content'].count('TEST!="bonding_slave"'),2)
    def test_conflicting_installed_rule_is_not_overwritten(self):
        self.cfg['os']['write_files']=[{'path':'/etc/udev/rules.d/79-harvester-bond-identity.rules','content':'operator rule'}]
        with self.assertRaises(BootNetworkError):installer_boot_identity(self.cfg)
    def test_existing_kernel_arguments_are_preserved(self):
        self.cfg['os']['additional_kernel_arguments']='intel_iommu=on'
        out=installer_boot_identity(self.cfg)
        self.assertTrue(out['os']['additional_kernel_arguments'].startswith('intel_iommu=on '))
    def test_matching_existing_ifname_is_not_duplicated(self):
        self.cfg['os']['additional_kernel_arguments']='ifname=hvboot0:f6:66:60:a0:05:45'
        out=installer_boot_identity(self.cfg)
        self.assertEqual(out['os']['additional_kernel_arguments'].split().count('ifname=hvboot0:f6:66:60:a0:05:45'),1)
    def test_conflicting_existing_ifname_fails_closed(self):
        self.cfg['os']['additional_kernel_arguments']='ifname=other:f6:66:60:a0:05:46'
        with self.assertRaises(BootNetworkError):installer_boot_identity(self.cfg)
    def test_explicit_selected_names_drive_both_boot_and_installer(self):
        self.cfg['install']['management_interface']['interfaces'][0]['name']='mgmtleft'
        out=installer_boot_identity(self.cfg)
        self.assertEqual(out['install']['management_interface']['interfaces'][0]['name'],'mgmtleft')
        self.assertIn('ifname=mgmtleft:f6:66:60:a0:05:45',boot_network_args(out).split())
        self.assertIn('ifname=mgmtleft:f6:66:60:a0:05:45',out['os']['additional_kernel_arguments'].split())
    def test_explicit_names_without_macs_are_not_renamed(self):
        self.cfg['install']['management_interface']['interfaces']=[{'name':'ens3f0'}]
        out=installer_boot_identity(self.cfg)
        self.assertEqual(out['install']['management_interface']['interfaces'],[{'name':'ens3f0'}])
        self.assertNotIn('additional_kernel_arguments',out['os'])
    def test_real_render_and_preview_paths_produce_same_identity(self):
        app=Path(str(BASE/'web/app.py')).read_text()
        schema=Path(str(BASE/'web/harvester_install_schema.py'))
        spec=importlib.util.spec_from_file_location('test_schema',str(schema));his=importlib.util.module_from_spec(spec);spec.loader.exec_module(his)
        env={'_his':his,'InstallConfigError':his.InstallConfigError,'BootNetworkError':BootNetworkError,
             '_bm_installer_boot_identity':installer_boot_identity,'_bm_boot_network_args':boot_network_args,
             '_BM_ISO_URL_PLACEHOLDER':'http://fixture.invalid/iso'}
        module=ast.parse('');module.body=[n for n in ast.parse(app).body if isinstance(n,ast.FunctionDef) and n.name in (
            '_harvester_install_config','_bm_render_checked','_bm_with_installer_identity','_bm_render_boot_checked')]
        exec(compile(module,'<actual-render-functions>','exec'),env)
        opts=his.split_imported_config(his.dump_install_config(self.cfg))['form']
        import yaml
        actual=yaml.safe_load(env['_harvester_install_config'](dict(opts,iso_url='http://fixture.invalid/iso'),boot_identity=True))
        # 1.88.0 : sans l'ISO remasterisée (banc harvlab, PXE de l'exploitant),
        # ni noms hvbootN ni ifname= : l'installeur ne les trouverait pas
        plain=yaml.safe_load(env['_harvester_install_config'](dict(opts,iso_url='http://fixture.invalid/iso')))
        self.assertFalse([i for i in plain['install']['management_interface']['interfaces'] if str(i.get('name','')).startswith('hvboot')])
        self.assertNotIn('ifname=',str(plain['os'].get('additional_kernel_arguments') or ''))
        preview=env['_bm_render_boot_checked'](opts)
        self.assertEqual(actual,preview)
        self.assertEqual(actual['install']['management_interface']['interfaces'][1],{'name':'hvboot1','hwAddr':'f6:66:60:a0:05:46'})
        self.assertEqual(his.check_install_config(actual),[])


if __name__=='__main__':unittest.main()
