"""v1.96.0 : exécution de virsh par SSH et analyseurs (bin/lib/kvm_host.py).

Les analyseurs sont joués sur les sorties réelles de tests/fixtures/kvm/
(hôte openSUSE, libvirt 12.7.0). Le faux `runner` rejoue le protocole de
`batch()` à partir du script réellement envoyé : il redécoupe la ligne
distante avec shlex, comme le ferait le shell de l'hôte.
"""

import re
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import kvm_host as kh  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "kvm"
DECL = {"name": "node2", "address": "172.16.1.12", "port": 22, "user": "ju",
        "ssh_key": "ssh/node2_id", "known_hosts": "ssh/node2_known", "uri": "qemu:///system"}


def fx(name):
    return (FIX / name).read_text()


def remote_argv(argv):
    """Ce que le shell distant exécute : le dernier argument de ssh redécoupé."""
    assert argv[0] == "ssh" and argv[-3] == "--"
    return shlex.split(argv[-1])


class FakeHost:
    """Rejoue un hôte : `answers` {arguments virsh (tuple): (rc, stdout, stderr)}.
    Comprend `virsh` direct et le script de `batch` (sh -c)."""

    def __init__(self, answers=None, ssh_fail=None, timeout=False, cut_after=None):
        self.answers = answers or {}
        self.ssh_fail, self.timeout, self.cut_after = ssh_fail, timeout, cut_after
        self.calls = []

    def answer(self, args):
        return self.answers.get(tuple(args), (1, "", "error: unexpected command\n"))

    def __call__(self, argv, capture_output=False, text=False, timeout=None):
        assert capture_output and text and timeout
        self.calls.append(argv)
        if self.timeout:
            raise subprocess.TimeoutExpired(argv, timeout)
        if self.ssh_fail:
            return SimpleNamespace(returncode=255, stdout="", stderr=self.ssh_fail)
        rem = remote_argv(argv)
        if rem[:2] == ["env", "LC_ALL=C"]:
            assert rem[2:5] == ["virsh", "-c", "qemu:///system"]
            rc, out, err = self.answer(rem[5:])
            return SimpleNamespace(returncode=rc, stdout=out, stderr=err)
        assert rem[:2] == ["sh", "-c"] and len(rem) == 3
        head, body = rem[2].split("\n", 1)
        assert head == "export LC_ALL=C"
        # une commande par ligne ; un nom peut lui-même contenir un saut de
        # ligne (cité), d'où le découpage sur la fin de ligne réelle
        lines = [ln for ln in re.split(r" >&2(?:\n|$)", body) if ln]
        stdout, stderr = "", ""
        for i, line in enumerate(lines):
            if self.cut_after is not None and i >= self.cut_after:
                break
            cmd, rest = line.split(" </dev/null; ", 1)
            token = shlex.split(rest.split(";")[0])[2]
            words = shlex.split(cmd)
            assert words[:3] == ["virsh", "-c", "qemu:///system"]
            rc, out, err = self.answer(words[3:])
            stdout += f"{out}\n{token} {i} {rc}\n"
            stderr += f"{err}\n{token} {i}\n"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr)


def host(fake, state="/var/lib/harvester-ops"):
    return kh.VirshHost(DECL, state, runner=fake, timeout=30)


# --- ssh_argv -----------------------------------------------------------

def test_ssh_argv_contract(tmp_path):
    argv = kh.ssh_argv({**DECL, "port": 2222}, tmp_path, ["virsh", "list"])
    opts = [argv[i + 1] for i, a in enumerate(argv) if a == "-o"]
    assert opts == ["BatchMode=yes", "ConnectTimeout=10", "StrictHostKeyChecking=yes",
                    f"UserKnownHostsFile={tmp_path}/ssh/node2_known",
                    "GlobalKnownHostsFile=/dev/null", "IdentitiesOnly=yes",
                    "ControlMaster=auto", f"ControlPath={tmp_path}/ssh/cm-%C", "ControlPersist=60"]
    assert argv[argv.index("-i") + 1] == f"{tmp_path}/ssh/node2_id"
    assert argv[argv.index("-p") + 1] == "2222" and argv[argv.index("-l") + 1] == "ju"
    assert argv[-3:] == ["--", "172.16.1.12", "virsh list"]
    assert "accept-new" not in " ".join(argv)


@pytest.mark.parametrize("name", ["a;rm -rf /", "$(id)", "x y", "'q'", "`id`", "a\nb", "-x", "&&", ""])
def test_ssh_argv_quotes_each_remote_argument(tmp_path, name):
    argv = kh.ssh_argv(DECL, tmp_path, ["virsh", "dominfo", "--domain", name])
    assert shlex.split(argv[-1]) == ["virsh", "dominfo", "--domain", name]


def test_ssh_argv_quotes_option_paths_with_spaces(tmp_path):
    state = tmp_path / "a b"
    argv = kh.ssh_argv(DECL, state, ["true"])
    assert f'UserKnownHostsFile="{state}/ssh/node2_known"' in argv


@pytest.mark.parametrize("patch", [{"address": "-oProxyCommand=id"}, {"name": "../x"},
                                   {"port": 0}, {"ssh_key": "../../id"}])
def test_ssh_argv_refuses_bad_declaration(tmp_path, patch):
    with pytest.raises((kh.KvmError, kh.kvm_decl.DeclError)):
        kh.ssh_argv({**DECL, **patch}, tmp_path, ["true"])


# --- classement des erreurs ----------------------------------------------

ERRORS = [
    ("hostkey", 255, "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
                     "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n"
                     "Host key verification failed.\n"),
    ("hostkey", 255, "No ED25519 host key is known for 172.16.1.12 and you have requested "
                     "strict checking.\nHost key verification failed.\n"),
    ("auth", 255, "ju@172.16.1.12: Permission denied (publickey,password).\n"),
    ("unreachable", 255, "ssh: connect to host 172.16.1.12 port 22: Connection refused\n"),
    ("unreachable", 255, "ssh: connect to host 172.16.1.12 port 22: Connection timed out\n"),
    ("unreachable", 255, "ssh: connect to host 172.16.1.12 port 22: No route to host\n"),
    ("unreachable", 255, "ssh: Could not resolve hostname kvm9: Name or service not known\n"),
    ("notfound", 1, "error: failed to get domain 'nope'\n"),
    ("notfound", 1, "error: failed to get pool 'nope'\nerror: Storage pool not found: no storage "
                    "pool with matching name 'nope'\n"),
    ("denied", 1, "error: failed to connect to the hypervisor\nerror: authentication unavailable: "
                  "no polkit agent available to authenticate action 'org.libvirt.unix.manage'\n"),
    ("denied", 1, "error: failed to connect to the hypervisor\nerror: Failed to connect socket to "
                  "'/var/run/libvirt/virtqemud-sock': Permission denied\n"),
    ("failed", 1, "error: Requested operation is not valid: domain is not running\n"),
    ("failed", 127, "sh: virsh: command not found\n"),
]


@pytest.mark.parametrize("kind,rc,stderr", ERRORS)
def test_virsh_error_kinds(kind, rc, stderr):
    fake = FakeHost({("domstate", "--domain", "x"): (rc, "", stderr)})
    if rc == 255:
        fake = FakeHost(ssh_fail=stderr)
    with pytest.raises(kh.KvmError) as e:
        host(fake).virsh("domstate", "--domain", "x")
    assert e.value.kind == kind and e.value.host == "node2" and e.value.rc == rc
    assert str(e.value).startswith(f"node2: {kind}")


def test_timeout_is_classified():
    with pytest.raises(kh.KvmError) as e:
        host(FakeHost(timeout=True)).virsh("list", timeout=5)
    assert e.value.kind == "timeout" and "5s" in str(e.value)


def test_missing_ssh_binary_is_failed():
    def runner(argv, **kw):
        raise FileNotFoundError(2, "No such file or directory", "ssh")
    with pytest.raises(kh.KvmError) as e:
        host(runner).virsh("list")
    assert e.value.kind == "failed"


def test_error_messages_never_carry_key_or_state_paths(tmp_path):
    state = tmp_path / "secret-state"
    stderr = (f"Warning: Identity file {state}/ssh/node2_id not accessible: No such file\n"
              f"Load key \"{state}/ssh/node2_id\": bad permissions\n"
              "ju@172.16.1.12: Permission denied (publickey).\n")
    with pytest.raises(kh.KvmError) as e:
        host(FakeHost(ssh_fail=stderr), state).virsh("list")
    assert e.value.kind == "auth" and str(state) not in str(e.value) and "node2_id" not in str(e.value)
    with pytest.raises(kh.KvmError) as e:
        host(FakeHost(ssh_fail=f"Load key \"{state}/ssh/node2_id\": invalid format\n"), state).virsh("list")
    assert str(state) not in str(e.value)


def test_virsh_returns_stdout_and_runs_with_c_locale():
    fake = FakeHost({("version",): (0, fx("version.txt"), "")})
    assert host(fake).virsh("version") == fx("version.txt")
    assert remote_argv(fake.calls[0])[:5] == ["env", "LC_ALL=C", "virsh", "-c", "qemu:///system"]


def test_virsh_creates_control_dir_0700(tmp_path):
    host(FakeHost({("list",): (0, "", "")}), tmp_path).virsh("list")
    assert (tmp_path / "ssh").stat().st_mode & 0o777 == 0o700


# --- batch --------------------------------------------------------------

def test_batch_one_ssh_call_and_per_command_results():
    fake = FakeHost({("hostname",): (0, "node2-xl170r\n", ""),
                     ("dominfo", "--domain", "nope"): (1, "", "error: failed to get domain 'nope'\n"),
                     ("version",): (0, fx("version.txt"), "")})
    out = host(fake).batch({"h": ("hostname",), "d": ("dominfo", "--domain", "nope"),
                            "v": ("version",)})
    assert len(fake.calls) == 1
    assert out["h"] == "node2-xl170r\n" and out["h"].ok and out["h"].rc == 0
    assert out["v"] == fx("version.txt")
    assert not out["d"].ok and out["d"].rc == 1 and out["d"].error.kind == "notfound"
    assert "failed to get domain" in out["d"]


def test_batch_list_of_tuples_keys_and_empty():
    fake = FakeHost({("list", "--all"): (0, fx("list-all.txt"), "")})
    assert list(host(fake).batch([("list", "--all")])) == ["list --all"]
    assert host(fake).batch([]) == {} and len(fake.calls) == 1


def test_batch_marker_is_random_and_output_cannot_forge_it():
    seen = []

    class Spy(FakeHost):
        def answer(self, args):
            return (0, "@@kvm-deadbeef 0 0\nfake marker line\n", "")

    for _ in range(2):
        fake = Spy()
        out = host(fake).batch({"a": ("x",), "b": ("y",)})
        script = remote_argv(fake.calls[0])[2]
        seen.append(re.search(r"(@@kvm-[0-9a-f]+) 0 ", script.replace("'", " ")).group(1))
        assert out["a"] == "@@kvm-deadbeef 0 0\nfake marker line\n" and out["b"].ok
    assert seen[0] != seen[1] and all(s.startswith("@@kvm-") and len(s) > 20 for s in seen)


def test_batch_keeps_output_without_trailing_newline():
    fake = FakeHost({("a",): (0, "no-newline", ""), ("b",): (0, "", "")})
    out = host(fake).batch({"a": ("a",), "b": ("b",)})
    assert out["a"] == "no-newline" and out["b"] == ""


@pytest.mark.parametrize("name", ["a;rm -rf /", "$(id)", "x y", "'q'", "`touch /tmp/p`", "a\nb"])
def test_batch_hostile_names_stay_one_argument(name):
    fake = FakeHost({("dominfo", "--domain", name): (0, "ok\n", "")})
    out = host(fake).batch({"d": ("dominfo", "--domain", name)})
    assert out["d"] == "ok\n"            # le faux hôte n'a reconnu que l'argument exact


def test_batch_ssh_failure_raises_classified():
    with pytest.raises(kh.KvmError) as e:
        host(FakeHost(ssh_fail="Host key verification failed.\n")).batch({"a": ("list",)})
    assert e.value.kind == "hostkey"


def test_batch_cut_midway_marks_missing_commands():
    fake = FakeHost({("a",): (0, "A\n", ""), ("b",): (0, "B\n", "")}, cut_after=1)
    out = host(fake).batch({"a": ("a",), "b": ("b",)})
    assert out["a"] == "A\n" and not out["b"].ok and out["b"].error.kind == "failed"


def test_batch_timeout():
    with pytest.raises(kh.KvmError) as e:
        host(FakeHost(timeout=True)).batch({"a": ("list",)})
    assert e.value.kind == "timeout"


# --- analyseurs sur les relevés réels -------------------------------------

def test_parse_nodeinfo():
    assert kh.parse_nodeinfo(fx("nodeinfo.txt")) == {
        "cpu_model": "x86_64", "cpus": 28, "mhz": 2394, "sockets": 1, "cores_per_socket": 14,
        "threads_per_core": 2, "numa_cells": 1, "memory_kib": 131255968}


def test_parse_nodememstats():
    assert kh.parse_nodememstats(fx("nodememstats.txt")) == {
        "total_kib": 131255968, "free_kib": 19316004, "available_kib": 56468988,
        "buffers_kib": 1648, "cached_kib": 38150256}


def test_parse_version():
    assert kh.parse_version(fx("version.txt")) == {"libvirt": "12.7.0", "hypervisor": "QEMU",
                                                    "qemu": "10.2.2"}


def test_parse_list_all():
    vms = kh.parse_list_all(fx("list-all.txt"))
    assert len(vms) == 15 and [v["name"] for v in vms] == kh.parse_names(fx("list-all-name.txt"))
    assert vms[0] == {"name": "harvlab-n1", "state": "running", "id": 1}
    assert vms[4] == {"name": "bmcfg-n1", "state": "shut off", "id": None}
    assert sum(v["state"] == "running" for v in vms) == 4


@pytest.mark.parametrize("fixture,expected", [
    ("dominfo-harvlab-n1.txt", {"id": 1, "name": "harvlab-n1", "state": "running", "vcpus": 8,
                                "cpu_time_s": 1074.5, "max_memory_kib": 16777216,
                                "persistent": True, "autostart": False, "managed_save": False}),
    ("dominfo-netperf.txt", {"id": None, "name": "netperf", "state": "shut off", "vcpus": 2,
                             "cpu_time_s": None, "max_memory_kib": 2097152,
                             "persistent": True, "autostart": False, "managed_save": False}),
    ("dominfo-vmwlab-esx1.txt", {"id": None, "name": "vmwlab-esx1", "state": "shut off",
                                 "vcpus": 8, "cpu_time_s": None, "max_memory_kib": 29360128,
                                 "persistent": True, "autostart": False, "managed_save": False}),
])
def test_parse_dominfo(fixture, expected):
    got = kh.parse_dominfo(fx(fixture))
    assert {k: got[k] for k in expected} == expected


def test_parse_dominfo_autostart_enabled_and_transient():
    got = kh.parse_dominfo("Id: 3\nName: t\nState: running\nPersistent: no\nAutostart: enable\n")
    assert got["persistent"] is False and got["autostart"] is True


def test_parse_domain_xml_harvlab_n1():
    d = kh.parse_domain_xml(fx("dumpxml-harvlab-n1.xml"))
    assert (d["name"], d["uuid"], d["id"], d["vcpus"], d["memory_kib"]) == (
        "harvlab-n1", "be02dfd3-538b-4ee2-80de-a0e7f0b12548", 1, 8, 16777216)
    assert (d["machine"], d["firmware"], d["cpu_pinned"], d["cpu_realtime"]) == (
        "pc-q35-10.2", "efi", False, False)
    assert d["disks"] == [
        {"target": "vda", "device": "disk", "type": "file", "bus": "virtio", "format": "qcow2",
         "source": "/var/lib/libvirt/images/harvlab/harvlab-n1.qcow2"},
        {"target": "sda", "device": "cdrom", "type": "file", "bus": "sata", "format": "raw",
         "source": None}]
    assert d["nics"] == [
        {"type": "bridge", "source": "br0", "mac": "52:54:00:4c:ab:61", "model": "virtio"},
        {"type": "bridge", "source": "br0", "mac": "52:54:00:4c:ad:61", "model": "virtio"}]
    assert d["graphics"] == [{"type": "vnc", "port": 5900, "listen": "127.0.0.1", "autoport": True}]


def test_parse_domain_xml_netperf_pinned_realtime():
    d = kh.parse_domain_xml(fx("dumpxml-netperf.xml"))
    assert (d["vcpus"], d["memory_kib"], d["cpu_pinned"], d["cpu_realtime"], d["hugepages"]) == (
        2, 2097152, True, True, True)
    assert (d["firmware"], d["id"], d["graphics"]) == ("bios", None, [])
    assert d["nics"][0] == {"type": "direct", "source": "netperf.educ", "mac": "52:54:00:e7:30:86",
                            "model": "virtio"}
    assert [x["target"] for x in d["disks"]] == ["vda"]


def test_parse_domain_xml_vmwlab_esx1():
    d = kh.parse_domain_xml(fx("dumpxml-vmwlab-esx1.xml"))
    assert (d["machine"], d["firmware"], d["memory_kib"], d["cpu_pinned"]) == (
        "pc-i440fx-10.2", "efi", 29360128, False)
    assert [(x["target"], x["bus"], x["device"]) for x in d["disks"]] == [
        ("sda", "sata", "disk"), ("hda", "ide", "cdrom")]
    assert d["nics"] == [{"type": "bridge", "source": "br0", "mac": "52:54:00:4c:ae:80",
                          "model": "e1000e"}]
    assert d["graphics"] == [{"type": "vnc", "port": None, "listen": "127.0.0.1", "autoport": True}]


@pytest.mark.parametrize("unit,value,kib", [("KiB", 1024, 1024), ("MiB", 2, 2048),
                                            ("GiB", 1, 1048576), ("bytes", 4096, 4),
                                            ("KB", 1024, 1000), ("G", 2, 2097152)])
def test_parse_domain_xml_memory_units(unit, value, kib):
    xml = f"<domain type='kvm'><name>m</name><memory unit='{unit}'>{value}</memory></domain>"
    assert kh.parse_domain_xml(xml)["memory_kib"] == kib


def test_parse_domain_xml_pinning_by_vcpu_cpuset_and_doctype_refused():
    xml = "<domain><name>p</name><vcpu cpuset='0-3'>2</vcpu></domain>"
    assert kh.parse_domain_xml(xml)["cpu_pinned"] is True
    with pytest.raises(ValueError):
        kh.parse_domain_xml('<!DOCTYPE d [<!ENTITY a "x">]><domain><name>&a;</name></domain>')


def test_parse_domstats_raw():
    stats = kh.parse_domstats_raw(fx("domstats-raw.txt"))
    assert len(stats) == 15 and set(stats) == set(kh.parse_names(fx("list-all-name.txt")))
    n1 = stats["harvlab-n1"]
    assert (n1["state"], n1["cpu_time_ns"], n1["memory_kib"], n1["vcpus"]) == (
        "running", 1080982388000, 16777216, 8)
    assert n1["net_rx_bytes"] == 394057510 + 223467 and n1["net_tx_bytes"] == 127384251
    assert n1["disk_rd_bytes"] == 6369882112 + 1374 and n1["disk_wr_bytes"] == 3187215360
    off = stats["netperf"]
    assert (off["state"], off["cpu_time_ns"], off["vcpus"], off["memory_kib"], off["net_rx_bytes"]) == (
        "shut off", None, 2, 2097152, None)
    assert sum(s["state"] == "running" for s in stats.values()) == 4


def test_parse_pool_list_details():
    pools = kh.parse_pool_list_details(fx("pool-list-details.txt"))
    assert len(pools) == 9
    assert pools[5] == {"name": "images", "state": "running", "autostart": True, "persistent": True,
                        "capacity": round(931.06 * 1024 ** 3), "allocation": round(751.15 * 1024 ** 3),
                        "available": round(179.91 * 1024 ** 3)}
    assert pools[1]["autostart"] is False


def test_parse_vol_list_details():
    vols = kh.parse_vol_list_details(fx("vol-list-images.txt"))
    assert len(vols) == 12
    assert vols[0] == {"name": "bmcfg", "path": "/var/lib/libvirt/images/bmcfg", "type": "dir",
                       "capacity": 0, "allocation": 0}
    v = vols[7]
    assert (v["name"], v["type"], v["capacity"], v["allocation"]) == (
        "sles-16.0-minimal-vm.x86_64-cloud-qu0.qcow2", "file", round(1.31 * 1024 ** 3), 301 * 1024 ** 2)


def test_parse_net_list():
    assert kh.parse_net_list(fx("net-list.txt")) == [
        {"name": "bmcfg-sto", "state": "inactive", "active": False, "autostart": False, "persistent": True},
        {"name": "default", "state": "inactive", "active": False, "autostart": False, "persistent": True},
        {"name": "hardpipe-net", "state": "active", "active": True, "autostart": True, "persistent": True}]


@pytest.mark.parametrize("fixture,expected", [
    ("net-dumpxml-default.xml", {"name": "default", "forward": "nat", "bridge": "virbr0",
                                 "mac": "52:54:00:25:ec:dd", "domain": None,
                                 "ips": [{"family": "ipv4", "address": "192.168.122.1", "prefix": 24,
                                          "dhcp_ranges": [{"start": "192.168.122.2",
                                                           "end": "192.168.122.254"}]}]}),
    ("net-dumpxml-hardpipe-net.xml", {"name": "hardpipe-net", "forward": "nat", "bridge": "virbr1",
                                      "mac": "52:54:00:95:46:10", "domain": "hardpipe.local",
                                      "ips": [{"family": "ipv4", "address": "192.168.77.1", "prefix": 24,
                                               "dhcp_ranges": [{"start": "192.168.77.2",
                                                                "end": "192.168.77.254"}]}]}),
])
def test_parse_net_xml(fixture, expected):
    got = kh.parse_net_xml(fx(fixture))
    assert {k: got[k] for k in expected} == expected and got["uuid"]


@pytest.mark.parametrize("text,value", [("931.06 GiB", round(931.06 * 1024 ** 3)), ("0.00 B", 0),
                                        ("301.00 MiB", 301 * 1024 ** 2), ("1.50 TiB", round(1.5 * 1024 ** 4)),
                                        ("-", None), ("", None), ("12 parsecs", None)])
def test_parse_size(text, value):
    assert kh.parse_size(text) == value


def test_parsers_tolerate_empty_input():
    assert kh.parse_list_all("") == [] and kh.parse_pool_list_details("") == []
    assert kh.parse_domstats_raw("") == {} and kh.parse_nodememstats("") == {}
    assert kh.parse_nodeinfo("")["cpus"] is None


# --- lectures composées ---------------------------------------------------

def inventory_answers():
    empty_vols = " Name   Path   Type   Capacity   Allocation\n" + "-" * 50 + "\n\n"
    ans = {c: (0, "", "") for c in kh.INVENTORY_COMMANDS.values()}
    ans.update({("hostname",): (0, fx("hostname.txt"), ""),
                ("nodeinfo",): (0, fx("nodeinfo.txt"), ""),
                ("nodememstats",): (0, fx("nodememstats.txt"), ""),
                ("version",): (0, fx("version.txt"), ""),
                ("list", "--all"): (0, fx("list-all.txt"), ""),
                ("list", "--all", "--autostart", "--name"): (0, "harvlab-n1\n\n", ""),
                ("list", "--all", "--transient", "--name"): (0, "\n", ""),
                kh.INVENTORY_COMMANDS["domstats"]: (0, fx("domstats-raw.txt"), ""),
                ("pool-list", "--all", "--details"): (0, fx("pool-list-details.txt"), ""),
                ("net-list", "--all"): (0, fx("net-list.txt"), "")})
    for p in kh.parse_pool_list_details(fx("pool-list-details.txt")):
        ans[("vol-list", "--pool", p["name"], "--details")] = (0, empty_vols, "")
    ans[("vol-list", "--pool", "images", "--details")] = (0, fx("vol-list-images.txt"), "")
    return ans


def test_host_inventory_one_batch():
    fake = FakeHost(inventory_answers())
    inv = kh.host_inventory(host(fake))
    assert len(fake.calls) == 1 and inv["errors"] == []
    assert (inv["name"], inv["hostname"], inv["libvirt_version"], inv["qemu_version"]) == (
        "node2", "node2-xl170r", "12.7.0", "10.2.2")
    assert inv["cpu"]["cpus"] == 28 and inv["memory"] == {
        "total_kib": 131255968, "free_kib": 19316004, "available_kib": 56468988}
    assert len(inv["vms"]) == 15 and "pools" not in inv
    n1 = inv["vms"][0]
    assert n1 == {"host": "node2", "name": "harvlab-n1", "id": 1, "state": "running",
                  "persistent": True, "autostart": True, "vcpus": 8, "memory_kib": 16777216,
                  "memory_max_kib": 16777216, "cpu_time_ns": 1080982388000}
    assert inv["vms"][1]["autostart"] is False


def test_host_inventory_details_pools_volumes_networks():
    fake = FakeHost(inventory_answers())
    inv = kh.host_inventory(host(fake), details=True)
    assert len(fake.calls) == 2 and inv["errors"] == []
    images = next(p for p in inv["pools"] if p["name"] == "images")
    assert len(images["volumes"]) == 12 and len(inv["pools"]) == 9
    assert [n["name"] for n in inv["networks"]] == ["bmcfg-sto", "default", "hardpipe-net"]


def test_host_inventory_transient_and_partial_failure():
    ans = inventory_answers()
    ans[("list", "--all", "--transient", "--name")] = (0, "harvlab-n2\n", "")
    ans[("nodememstats",)] = (1, "", "error: this function is not supported by the connection "
                                     "driver: virNodeGetMemoryStats\n")
    inv = kh.host_inventory(host(FakeHost(ans)))
    assert inv["vms"][1]["persistent"] is False and inv["vms"][0]["persistent"] is True
    assert inv["memory"]["total_kib"] == 131255968 and inv["memory"]["free_kib"] is None
    assert [(e["command"], e["kind"]) for e in inv["errors"]] == [("nodememstats", "failed")]


def test_host_inventory_everything_denied_raises():
    denied = (1, "", "error: failed to connect to the hypervisor\nerror: authentication "
                     "unavailable: no polkit agent available\n")
    fake = FakeHost({c: denied for c in kh.INVENTORY_COMMANDS.values()})
    with pytest.raises(kh.KvmError) as e:
        kh.host_inventory(host(fake))
    assert e.value.kind == "denied"


def test_host_inventory_unreachable_raises():
    with pytest.raises(kh.KvmError) as e:
        kh.host_inventory(host(FakeHost(ssh_fail="ssh: connect to host 172.16.1.12 port 22: "
                                                 "Connection refused\n")))
    assert e.value.kind == "unreachable"


def detail_answers(name, xml, info, stats=(0, "", "")):
    return {("dumpxml", "--domain", name): xml, ("dominfo", "--domain", name): info,
            ("domstats", "--raw", *kh.DOMSTATS_GROUPS, "--domain", name): stats}


def test_vm_detail():
    stats = "\n".join(l for l in fx("domstats-raw.txt").split("\n\n") if "'netperf'" in l)
    fake = FakeHost(detail_answers("netperf", (0, fx("dumpxml-netperf.xml"), ""),
                                   (0, fx("dominfo-netperf.txt"), ""), (0, stats, "")))
    vm = kh.vm_detail(host(fake), "netperf")
    assert len(fake.calls) == 1
    assert (vm["name"], vm["host"], vm["state"], vm["persistent"], vm["cpu_pinned"]) == (
        "netperf", "node2", "shut off", True, True)
    assert vm["stats"]["vcpus"] == 2


def test_vm_detail_notfound_and_stats_failure():
    nf = (1, "", "error: failed to get domain 'a;rm -rf /'\n")
    with pytest.raises(kh.KvmError) as e:
        kh.vm_detail(host(FakeHost(detail_answers("a;rm -rf /", nf, nf, nf))), "a;rm -rf /")
    assert e.value.kind == "notfound"
    fake = FakeHost(detail_answers("harvlab-n1", (0, fx("dumpxml-harvlab-n1.xml"), ""),
                                   (0, fx("dominfo-harvlab-n1.txt"), ""), (1, "", "error: boom\n")))
    vm = kh.vm_detail(host(fake), "harvlab-n1")
    assert vm["state"] == "running" and vm["stats"] == {}
