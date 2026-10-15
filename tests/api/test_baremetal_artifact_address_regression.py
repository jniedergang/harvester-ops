import ast
import os
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

APP = Path(os.environ.get('ADDRESS_TEST_APP', str(Path(__file__).resolve().parents[2]/'web/app.py'))).read_text()
TREE = ast.parse(APP)


def helper(resolver):
    env={'re':re, '_bm_local_ip_for':resolver}
    module=ast.parse('')
    module.body=[n for n in TREE.body if isinstance(n,ast.FunctionDef) and n.name=='_bm_artifact_base_urls']
    exec(compile(module,'<actual-address-helper>','exec'),env)
    return env['_bm_artifact_base_urls']


class FakePath:
    suffix='.iso'
    name='fixture.iso'
    def __truediv__(self,other):return FakePath()
    def is_file(self):return True
    def with_suffix(self,suffix):return FakePath()
    def write_text(self,text):pass
    def chmod(self,mode):pass


class AddressTests(unittest.TestCase):
    def setUp(self):
        self.calls=[]
        def route(host):
            self.calls.append(host)
            return {'198.51.100.115':'198.51.100.196','192.0.2.73':'203.0.113.9'}[host]
        self.route=route
        self.fn=helper(route)
        self.opts={'bmc_host':'198.51.100.115','method':'static','ip':'192.0.2.73'}
    def test_separate_source_interfaces_are_selected(self):
        self.assertEqual(self.fn(self.opts,8091),('http://198.51.100.196:8091','http://203.0.113.9:8091'))
        self.assertEqual(self.calls,['198.51.100.115','192.0.2.73'])
    def test_manual_media_host_does_not_override_node_route(self):
        opts=dict(self.opts,advertise_host='198.51.100.197')
        self.assertEqual(self.fn(opts,8091),('http://198.51.100.197:8091','http://203.0.113.9:8091'))
    def test_manual_installer_host_is_supported(self):
        opts=dict(self.opts,installer_advertise_host='203.0.113.10')
        self.assertEqual(self.fn(opts,8091)[1],'http://203.0.113.10:8091')
        self.assertEqual(self.calls,['198.51.100.115'])
    def test_dhcp_keeps_previous_address_without_known_node_ip(self):
        opts=dict(self.opts,method='dhcp');opts.pop('ip')
        self.assertEqual(self.fn(opts,8091),('http://198.51.100.196:8091','http://198.51.100.196:8091'))
    def test_one_interface_host_still_works(self):
        self.assertEqual(self.fn(self.opts,8091,lambda host:'192.0.2.1'),('http://192.0.2.1:8091','http://192.0.2.1:8091'))
    def test_failed_route_selection_does_not_advertise_loopback(self):
        with self.assertRaises(ValueError):self.fn(self.opts,8091,lambda host:'127.0.0.1')
    def test_unspecified_address_is_rejected(self):
        with self.assertRaises(ValueError):self.fn(dict(self.opts,installer_advertise_host='0.0.0.0'),8091)
    def test_host_injection_is_rejected_without_echoing_value(self):
        with self.assertRaises(ValueError) as e:self.fn(dict(self.opts,installer_advertise_host='SENSITIVE_FIXTURE/pxe'),8091)
        self.assertNotIn('SENSITIVE_FIXTURE',str(e.exception))
    def test_real_runner_uses_node_address_for_both_installer_downloads(self):
        opts=dict(self.opts,mode='join',cluster='fixture',iso='fixture.iso')
        record={}
        def config(data):record.update(data);return 'fixture-yaml'
        tokens=iter(['iso-fixture','config-fixture'])
        env={'initial_opts':opts,'host':opts['bmc_host'],'run':SimpleNamespace(id='fixture'),
             'pxe_server':SimpleNamespace(start=lambda:8091,issue=lambda *a:next(tokens)),
             '_bm_artifact_base_urls':self.fn,'_iso_dir':FakePath,'_iso_work_dir':FakePath,
             '_bm_join_pubkey':lambda cluster:None,'_harvester_install_config':config,
             'InstallConfigError':type('FixtureError',(Exception,),{}),'scratch':[],
             'creds':[],'tokens':[],'fail':lambda *args:('failed',args)}
        start=APP.index('    port = pxe_server.start()',APP.index('def _baremetal_install_runner'))
        end=APP.index('    step("remaster", "running"',start)
        exec(compile('def capture():\n    opts = dict(initial_opts)\n'+APP[start:end]+'    return iso_url, install_iso_url, config_url\n','<actual-runner-url-stage>','exec'),env)
        media,iso,cfg=env['capture']()
        self.assertEqual(media,'http://198.51.100.196:8091/pxe/iso/iso-fixture.iso')
        self.assertEqual(iso,'http://203.0.113.9:8091/pxe/iso/iso-fixture.iso')
        self.assertEqual(cfg,'http://203.0.113.9:8091/pxe/config/config-fixture.yaml')
        self.assertEqual(record['iso_url'],iso)
        self.assertEqual(record['mode'],'join')
        self.assertTrue(record['power_off'])
        self.assertEqual(env['tokens'],['iso-fixture','config-fixture'])


if __name__=='__main__':unittest.main()
