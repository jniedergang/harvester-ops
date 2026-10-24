"""v1.98.0 : transport et analyseurs ajoutés à bin/lib/kvm_host.py.

`run_input` (XML sur l'entrée standard), `stdio_forward_argv` (tunnel VNC par
`ssh -W`), champs ajoutés à `parse_domain_xml`, et les analyseurs
d'instantanés, de volumes et de pools. Jeux d'essai relevés sur node2
(libvirt 12.7.0), domaines de banc `kvmtest-*`.
"""

import json
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import kvm_host as kh  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "kvm"
DECL = {"name": "node2", "address": "172.16.1.12", "port": 22, "user": "ju",
        "ssh_key": "ssh/node2_id", "known_hosts": "ssh/node2_known", "uri": "qemu:///system"}
FAKE_PASSWORD = "Fk7pw3Zq"          # mot de passe factice du domaine de banc kvmtest-1


def fx(name):
    return (FIX / name).read_text()


@pytest.fixture
def state():
    d = tempfile.mkdtemp(prefix="kvm-", dir="/tmp")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


# --- run_input ---------------------------------------------------------------

class InputRunner:
    """Faux runner : rend `rc` et note l'argv et les arguments nommés."""

    def __init__(self, rc=0, stdout="", stderr=""):
        self.rc, self.stdout, self.stderr = rc, stdout, stderr
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return SimpleNamespace(returncode=self.rc, stdout=self.stdout, stderr=self.stderr)


def test_run_input_sends_data_on_stdin_never_in_argv(state):
    runner = InputRunner(stdout="Domain 'x' defined from /dev/stdin\n")
    xml = f"<domain><graphics passwd='{FAKE_PASSWORD}'/></domain>"
    h = kh.VirshHost(DECL, state, runner=runner)
    out = h.run_input(["env", "LC_ALL=C", "virsh", "-c", "qemu:///system", "define",
                       "/dev/stdin"], xml)
    assert out.startswith("Domain 'x' defined")
    argv, kw = runner.calls[0]
    assert kw["input"] == xml and "stdin" not in kw
    assert kw["capture_output"] and kw["text"] and kw["timeout"] == 30
    assert shlex.split(argv[-1]) == ["env", "LC_ALL=C", "virsh", "-c", "qemu:///system",
                                     "define", "/dev/stdin"]
    assert all(FAKE_PASSWORD not in a for a in argv)


def test_run_input_error_never_carries_the_data(state):
    runner = InputRunner(rc=1, stderr="error: Failed to define domain from /dev/stdin\n"
                                      "error: XML error: bad\n")
    h = kh.VirshHost(DECL, state, runner=runner)
    with pytest.raises(kh.KvmError) as e:
        h.run_input(["virsh", "define", "/dev/stdin"], f"<x passwd='{FAKE_PASSWORD}'/>")
    assert e.value.kind == "failed" and "XML error: bad" in e.value.message
    assert FAKE_PASSWORD not in repr(e.value) and FAKE_PASSWORD not in str(e.value)


@pytest.mark.parametrize("data", [b"<x/>", None, 3])
def test_run_input_refuses_non_text(state, data):
    with pytest.raises(TypeError):
        kh.VirshHost(DECL, state, runner=InputRunner()).run_input(["true"], data)


def test_run_still_closes_stdin(state):
    runner = InputRunner()
    kh.VirshHost(DECL, state, runner=runner).run(["true"])
    assert runner.calls[0][1]["stdin"] is subprocess.DEVNULL and "input" not in runner.calls[0][1]


def test_run_input_real_shell(state):
    # le shell local remplace ssh : la donnée arrive bien sur l'entrée standard
    def local(argv, **kw):
        return subprocess.run(["/bin/sh", "-c", argv[-1]], **kw)
    h = kh.VirshHost(DECL, state, runner=local)
    data = "line 1\n$(id) `id` 'q'\n"
    assert h.run_input(["cat"], data) == data


# --- stdio_forward_argv ------------------------------------------------------

def test_stdio_forward_argv_contract(state):
    argv = kh.stdio_forward_argv(DECL, state, "127.0.0.1", 5900)
    opts = [argv[i + 1] for i, a in enumerate(argv) if a == "-o"]
    base = kh.ssh_argv(DECL, state, ["true"])
    base_opts = [base[i + 1] for i, a in enumerate(base) if a == "-o"]
    # mêmes options que les autres commandes, sauf ControlMaster=no et les sondes de vie
    assert opts == [("ControlMaster=no" if o == "ControlMaster=auto" else o) for o in base_opts] \
        + ["ServerAliveInterval=30", "ServerAliveCountMax=3"]
    assert f"ControlPath={kh.control_path(DECL, state)}" in opts
    assert "ControlMaster=auto" not in opts
    assert argv[-4:] == ["-W", "127.0.0.1:5900", "--", "172.16.1.12"]
    assert argv[argv.index("-i") + 1] == f"{state}/ssh/node2_id"


@pytest.mark.parametrize("address,port,target", [
    ("127.0.0.1", 5900, "127.0.0.1:5900"),
    ("172.16.1.12", 65535, "172.16.1.12:65535"),
    ("::1", 5901, "[::1]:5901"),
    ("fe80:0:0::1", 5902, "[fe80::1]:5902"),
])
def test_stdio_forward_argv_targets(state, address, port, target):
    argv = kh.stdio_forward_argv(DECL, state, address, port)
    assert argv[argv.index("-W") + 1] == target


@pytest.mark.parametrize("address,port", [
    ("localhost", 5900), ("127.0.0.1 -oProxyCommand=id", 5900), ("-oProxyCommand=id", 5900),
    ("", 5900), (None, 5900), ("127.0.0.1:5900", 5900), ("[::1]", 5900), ("host.lo", 5900),
    ("127.0.0.1", 5899), ("127.0.0.1", 65536), ("127.0.0.1", "5900"), ("127.0.0.1", True),
    ("127.0.0.1", None), ("127.0.0.1", 5900.0),
])
def test_stdio_forward_argv_refuses_hostile_target(state, address, port):
    with pytest.raises(kh.KvmError) as e:
        kh.stdio_forward_argv(DECL, state, address, port)
    assert e.value.kind == "failed"


def test_stdio_forward_argv_refuses_bad_declaration(state):
    with pytest.raises(kh.KvmError, match="invalid declaration"):
        kh.stdio_forward_argv({**DECL, "address": "-oProxyCommand=id"}, state, "127.0.0.1", 5900)


# --- parse_domain_xml : champs ajoutés -----------------------------------------

def test_parse_domain_xml_vnc_password_fixture():
    d = kh.parse_domain_xml(fx("dumpxml-vnc-password.xml"))
    vda, sda = d["disks"]
    assert (vda["readonly"], vda["shareable"], vda["transient"]) == (False, False, False)
    assert vda["backing"] == "/var/lib/libvirt/images/kvmtest/base.qcow2"
    assert vda["backing_chain"] == ["/var/lib/libvirt/images/kvmtest/base.qcow2"]
    assert (sda["device"], sda["readonly"], sda["backing"], sda["backing_chain"]) == (
        "cdrom", True, None, [])
    assert d["graphics"] == [{"type": "vnc", "port": 5900, "listen": "127.0.0.1",
                              "autoport": True, "listen_type": "address", "socket": None,
                              "password": True}]
    assert (d["nvram"], d["hostdevs"], d["seclabels_static"], d["sysinfo_uuid"]) == (
        None, 0, False, False)
    # la présence seulement : la valeur n'est nulle part dans le résultat
    assert FAKE_PASSWORD not in json.dumps(d)


def test_parse_domain_xml_socket_spice_uefi_fixtures():
    sock = kh.parse_domain_xml(fx("dumpxml-vnc-socket.xml"))["graphics"][0]
    assert (sock["type"], sock["listen_type"], sock["port"], sock["password"]) == (
        "vnc", "socket", None, False)
    assert sock["socket"] == "/var/lib/libvirt/qemu/domain-4-kvmtest-sock/vnc.sock"
    spice = kh.parse_domain_xml(fx("dumpxml-spice-only.xml"))["graphics"]
    assert [(g["type"], g["listen_type"], g["port"]) for g in spice] == [("spice", "address", 5902)]
    uefi = kh.parse_domain_xml(fx("dumpxml-kvmtest-uefi.xml"))
    assert uefi["nvram"] == {"path": "/var/lib/libvirt/qemu/nvram/kvmtest-uefi_VARS.qcow2",
                             "format": "qcow2",
                             "template": "/usr/share/qemu/ovmf-x86_64-4m-vars.qcow2",
                             "type": "file"}
    assert uefi["graphics"][0]["listen"] == "0.0.0.0"
    # XML inactif : pas de chaîne de sauvegarde
    inactive = kh.parse_domain_xml(fx("dumpxml-kvmtest-uefi-inactive.xml"))
    assert inactive["disks"][0]["backing_chain"] == [] and inactive["nvram"]["format"] == "qcow2"


SYNTH = """<domain type='kvm'><name>s</name>
  <os><type>hvm</type><nvram>/var/lib/libvirt/qemu/nvram/s_VARS.fd</nvram></os>
  <sysinfo type='smbios'><system><entry name='manufacturer'>x</entry>
    <entry name='uuid'>11111111-2222-3333-4444-555555555555</entry></system></sysinfo>
  <devices>
    <disk type='file' device='disk'><driver type='qcow2'/><source file='/i/top.qcow2'/>
      <backingStore type='file'><format type='qcow2'/><source file='/i/mid.qcow2'/>
        <backingStore type='file'><format type='raw'/><source file='/i/base.raw'/>
          <backingStore/></backingStore></backingStore>
      <target dev='vda'/></disk>
    <disk type='file' device='disk'><source file='/i/ro.img'/><target dev='vdb'/><readonly/></disk>
    <disk type='block' device='disk'><source dev='/dev/sdz'/><target dev='vdc'/><shareable/></disk>
    <disk type='file' device='disk'><source file='/i/t.img'/><target dev='vdd'/><transient/></disk>
    <hostdev mode='subsystem' type='pci'/><hostdev mode='subsystem' type='usb'/>
    <graphics type='vnc'><listen type='none'/></graphics>
    <graphics type='spice' port='5930'><listen type='network' network='default'
      address='192.168.122.1'/></graphics>
  </devices>
  <seclabel type='dynamic' model='dac'/><seclabel type='static' model='selinux'>
    <label>system_u:system_r:svirt_t:s0:c1,c2</label></seclabel>
</domain>"""


def test_parse_domain_xml_synthetic_fields():
    d = kh.parse_domain_xml(SYNTH)
    flags = [(x["target"], x["readonly"], x["shareable"], x["transient"]) for x in d["disks"]]
    assert flags == [("vda", False, False, False), ("vdb", True, False, False),
                     ("vdc", False, True, False), ("vdd", False, False, True)]
    assert d["disks"][0]["backing"] == "/i/mid.qcow2"
    assert d["disks"][0]["backing_chain"] == ["/i/mid.qcow2", "/i/base.raw"]
    assert d["nvram"] == {"path": "/var/lib/libvirt/qemu/nvram/s_VARS.fd", "format": None,
                          "template": None, "type": "file"}
    assert (d["hostdevs"], d["seclabels_static"], d["sysinfo_uuid"]) == (2, True, True)
    assert [(g["type"], g["listen_type"], g["listen"]) for g in d["graphics"]] == [
        ("vnc", "none", None), ("spice", "network", "192.168.122.1")]


def test_parse_domain_xml_empty_nvram_element():
    d = kh.parse_domain_xml("<domain><name>e</name><os><type>hvm</type>"
                            "<nvram template='/t.fd'/></os></domain>")
    assert d["nvram"] == {"path": None, "format": None, "template": "/t.fd", "type": "file"}
    assert (d["hostdevs"], d["seclabels_static"], d["sysinfo_uuid"]) == (0, False, False)


NVRAM_FORMS = [
    ("text path", "<nvram format='qcow2'>/n/a_VARS.qcow2</nvram>",
     {"path": "/n/a_VARS.qcow2", "format": "qcow2", "template": None, "type": "file"}),
    ("new file form", "<nvram type='file' template='/t.qcow2' format='qcow2'>"
                      "<source file='/n/b_VARS.qcow2'/></nvram>",
     {"path": "/n/b_VARS.qcow2", "format": "qcow2", "template": "/t.qcow2", "type": "file"}),
    ("block form", "<nvram type='block'><source dev='/dev/vg/nv'/></nvram>",
     {"path": "/dev/vg/nv", "format": None, "template": None, "type": "block"}),
    ("network form", "<nvram type='network'><source protocol='iscsi' name='t/1'/></nvram>",
     {"path": "t/1", "format": None, "template": None, "type": "network"}),
]


@pytest.mark.parametrize("label,nv,expected", NVRAM_FORMS, ids=[n[0] for n in NVRAM_FORMS])
def test_parse_domain_xml_nvram_forms(label, nv, expected):
    d = kh.parse_domain_xml(f"<domain><name>n</name><os><type>hvm</type>{nv}</os></domain>")
    assert d["nvram"] == expected


TPM_FORMS = [
    ("none", "", None),
    ("emulator default", "<tpm model='tpm-crb'><backend type='emulator' version='2.0'/></tpm>",
     {"model": "tpm-crb", "backend": "emulator", "version": "2.0", "persistent_state": False,
      "state_source": None}),
    ("emulator persistent", "<tpm model='tpm-tis'><backend type='emulator' version='2.0' "
                            "persistent_state='yes'/></tpm>",
     {"model": "tpm-tis", "backend": "emulator", "version": "2.0", "persistent_state": True,
      "state_source": None}),
    ("emulator explicit dir", "<tpm model='tpm-crb'><backend type='emulator' version='2.0'>"
                              "<source type='dir' path='/srv/tpm/x'/></backend></tpm>",
     {"model": "tpm-crb", "backend": "emulator", "version": "2.0", "persistent_state": False,
      "state_source": "/srv/tpm/x"}),
    ("passthrough", "<tpm model='tpm-tis'><backend type='passthrough'>"
                    "<device path='/dev/tpm0'/></backend></tpm>",
     {"model": "tpm-tis", "backend": "passthrough", "version": None, "persistent_state": False,
      "state_source": None}),
]


@pytest.mark.parametrize("label,tpm,expected", TPM_FORMS, ids=[t[0] for t in TPM_FORMS])
def test_parse_domain_xml_tpm(label, tpm, expected):
    d = kh.parse_domain_xml(f"<domain><name>t</name><devices>{tpm}</devices></domain>")
    assert d["tpm"] == expected


@pytest.mark.parametrize("xml,hostdevs,genid", [
    ("<devices/>", 0, None),
    ("<devices><interface type='hostdev'><source><address type='pci' bus='0x3'/></source>"
     "</interface><interface type='network'><source network='d'/></interface></devices>", 1, None),
    ("<genid/><devices><hostdev mode='subsystem' type='pci'/></devices>", 1, True),
    ("<genid>43dc0cf8-809b-4adb-9bea-a9abb5f3d90e</genid><devices/>", 0,
     "43dc0cf8-809b-4adb-9bea-a9abb5f3d90e"),
])
def test_parse_domain_xml_hostdev_cards_and_genid(xml, hostdevs, genid):
    d = kh.parse_domain_xml(f"<domain><name>h</name>{xml}</domain>")
    assert (d["hostdevs"], d["genid"]) == (hostdevs, genid)


# --- instantanés, volumes, pools -------------------------------------------------

def test_parse_snapshot_list_parent_fixture():
    # relevé réel : noms piégés « -x » et « -z w », dates à espaces
    assert kh.parse_snapshot_list(fx("snapshot-list-parent.txt")) == [
        {"name": "-x", "created": "2026-10-24T11:39:01+02:00", "state": "shutoff",
         "parent": "s2"},
        {"name": "-z w", "created": "2026-10-24T11:39:01+02:00", "state": "shutoff",
         "parent": "-x"},
        {"name": "s1", "created": "2026-10-24T11:39:00+02:00", "state": "running",
         "parent": None},
        {"name": "s2", "created": "2026-10-24T11:39:01+02:00", "state": "shutoff",
         "parent": "s1"}]


@pytest.mark.parametrize("text", ["", "\n", None, "garbage\n"])
def test_parse_snapshot_list_empty(text):
    assert kh.parse_snapshot_list(fx("snapshot-list-empty.txt")) == []
    assert kh.parse_snapshot_list(text) == []


def test_parse_snapshot_list_keeps_an_unreadable_date():
    text = (" Name   Creation Time         State     Parent\n"
            "----------------------------------------------\n"
            " a      yesterday afternoon   running   \n")
    assert kh.parse_snapshot_list(text) == [
        {"name": "a", "created": "yesterday afternoon", "state": "running", "parent": None}]


def test_parse_vol_xml_backing_fixture():
    assert kh.parse_vol_xml(fx("vol-dumpxml-backing.xml")) == {
        "name": "kvmtest-1.qcow2", "key": "/var/lib/libvirt/images/kvmtest/kvmtest-1.qcow2",
        "path": "/var/lib/libvirt/images/kvmtest/kvmtest-1.qcow2", "format": "qcow2",
        "capacity": 67108864, "allocation": 3284992,
        "backing": "/var/lib/libvirt/images/kvmtest/base.qcow2",
        "mode": "0644", "owner": "1000", "group": "100"}


def test_parse_vol_xml_without_backing_and_units():
    v = kh.parse_vol_xml("<volume><name>r.img</name><key>/p/r.img</key>"
                         "<capacity unit='KiB'>2</capacity><allocation>10</allocation>"
                         "<target><path>/p/r.img</path></target></volume>")
    assert v == {"name": "r.img", "key": "/p/r.img", "path": "/p/r.img", "format": None,
                 "capacity": 2048, "allocation": 10, "backing": None,
                 "mode": None, "owner": None, "group": None}


def test_parse_pool_xml_fixture():
    assert kh.parse_pool_xml(fx("pool-dumpxml-images.xml")) == {
        "name": "images", "type": "dir", "target_path": "/var/lib/libvirt/images",
        "available": 193176313856}


@pytest.mark.parametrize("parser", [kh.parse_vol_xml, kh.parse_pool_xml])
def test_new_xml_parsers_refuse_doctype(parser):
    with pytest.raises(ValueError):
        parser("<!DOCTYPE x [<!ENTITY a 'b'>]><volume/>")
