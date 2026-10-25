"""Prochaine version KVM : réseaux libvirt (bin/lib/kvm_net.py)."""

import shlex
import shutil
import subprocess
import tempfile

import pytest

from kvm_next_sim import Sim2, fx
import kvm_host as kh
import kvm_net as kn
import kvm_ops as ko

LINKS = kn.parse_links(fx("ip-link.json"), fx("ip-addr.json"), fx("ip-route.json"))
NETS = [{"name": "hardpipe-net", "bridge": "virbr1",
         "ips": [{"family": "ipv4", "address": "192.168.77.1", "prefix": 24}]},
        {"name": "default", "bridge": "virbr0",
         "ips": [{"family": "ipv4", "address": "192.168.122.1", "prefix": 24}]}]


def test_parse_links_on_node2_fixtures():
    assert [b["name"] for b in LINKS["bridges"]] == ["br0", "virbr1", "docker0"]
    assert [p["name"] for p in LINKS["physical"]] == ["eno1", "eno2"]
    nets = {(n["network"], n["source"]) for n in LINKS["networks"]}
    assert ("172.16.0.0/16", "address") in nets and ("172.16.0.0/16", "route") in nets
    assert ("172.17.0.0/16", "address") in nets


@pytest.mark.parametrize("label,spec,text", [
    ("home LAN", {"subnet": "172.16.200.0/24"}, "overlaps 172.16.0.0/16 (address of br0)"),
    ("docker", {"subnet": "172.17.5.0/24"}, "overlaps 172.17.0.0/16"),
    ("other libvirt network", {"subnet": "192.168.122.128/25"}, "overlaps network default"),
    ("public range", {"subnet": "8.8.8.0/24"}, "private range"),
    ("prefix", {"subnet": "10.1.0.0/31"}, "between /8 and /30"),
    ("not a subnet", {"subnet": "10.1.0"}, "invalid IPv4 subnet"),
    ("ipv6", {"subnet": "fd00::/64"}, "invalid IPv4 subnet"),
    ("dhcp outside", {"subnet": "10.9.9.0/24", "dhcp_start": "10.9.8.2",
                      "dhcp_end": "10.9.9.9"}, "inside the subnet"),
    ("dhcp gateway", {"subnet": "10.9.9.0/24", "dhcp_start": "10.9.9.1",
                      "dhcp_end": "10.9.9.9"}, "gateway 10.9.9.1"),
    ("bridge taken", {"subnet": "10.9.9.0/24", "bridge": "virbr1"}, "already used"),
    ("bridge too long", {"subnet": "10.9.9.0/24", "bridge": "x" * 16}, "invalid bridge"),
    ("name", {"name": "-x"}, "invalid network name"),
    ("name taken", {"name": "default"}, "already exists"),
    ("mode", {"mode": "vepa"}, "mode must be"),
    ("domain", {"subnet": "10.9.9.0/24", "domain": "a b"}, "invalid domain"),
    ("bridge mode unknown bridge", {"mode": "bridge", "bridge": "eno1", "subnet": None},
     "not a bridge of the host"),
    ("bridge mode with subnet", {"mode": "bridge", "bridge": "br0"}, "has no subnet"),
])
def test_check_network_refusals(label, spec, text):
    full = {"name": "kvmtest-net", "mode": "nat", "subnet": "10.9.9.0/24", **spec}
    reasons = kn.check_network(full, LINKS, NETS)
    assert any(text in r for r in reasons), reasons


def test_overlap_is_said_once_per_network():
    reasons = kn.check_network({"name": "n", "mode": "nat", "subnet": "172.16.200.0/24"},
                               LINKS, NETS)
    assert len([r for r in reasons if "172.16.0.0/16" in r]) == 1


@pytest.mark.parametrize("spec", [
    {"name": "kvmtest-net", "mode": "isolated", "subnet": "10.213.77.0/24"},
    {"name": "n2", "mode": "nat", "subnet": "10.9.0.0/16", "domain": "lab.lo", "autostart": True},
    {"name": "n3", "mode": "route", "subnet": "10.9.9.0/24", "dhcp": False, "bridge": "kvbr9"},
    {"name": "n4", "mode": "bridge", "bridge": "br0"},
])
def test_check_network_accepts(spec):
    assert kn.check_network(spec, LINKS, NETS) == []


@pytest.mark.parametrize("spec,expected", [
    ({"name": "i", "mode": "isolated", "subnet": "10.213.77.0/24"},
     '<network>\n  <name>i</name>\n  <bridge stp="on" delay="0" />\n'
     '  <ip address="10.213.77.1" prefix="24">\n    <dhcp>\n'
     '      <range start="10.213.77.2" end="10.213.77.254" />\n    </dhcp>\n  </ip>\n'
     '</network>\n'),
    ({"name": "n", "mode": "nat", "subnet": "10.9.0.0/24", "bridge": "kv0", "domain": "lab.lo",
      "dhcp_start": "10.9.0.100", "dhcp_end": "10.9.0.200"},
     '<network>\n  <name>n</name>\n  <forward mode="nat" />\n'
     '  <bridge name="kv0" stp="on" delay="0" />\n  <domain name="lab.lo" />\n'
     '  <ip address="10.9.0.1" prefix="24">\n    <dhcp>\n'
     '      <range start="10.9.0.100" end="10.9.0.200" />\n    </dhcp>\n  </ip>\n</network>\n'),
    ({"name": "r", "mode": "route", "subnet": "10.8.0.0/24", "dhcp": False},
     '<network>\n  <name>r</name>\n  <forward mode="route" />\n'
     '  <bridge stp="on" delay="0" />\n  <ip address="10.8.0.1" prefix="24" />\n</network>\n'),
    ({"name": "b", "mode": "bridge", "bridge": "br0"},
     '<network>\n  <name>b</name>\n  <forward mode="bridge" />\n  <bridge name="br0" />\n'
     '</network>\n'),
])
def test_network_xml(spec, expected):
    assert kn.network_xml(spec) == expected


def net_sim():
    s = Sim2()
    s.add_net("default", "192.168.122.0/24", active=False)
    return s


def test_define_network_sequence_xml_on_stdin():
    s = net_sim()
    res = kn.define_network(s, {"name": "kvmtest-net", "mode": "isolated",
                                "subnet": "10.213.77.0/24", "autostart": True})
    assert s.writes == [("net-define", "--validate", "--file", "/dev/stdin"),
                        ("net-start", "--network", "kvmtest-net"),
                        ("net-autostart", "--network", "kvmtest-net")]
    inp = [c for c in s.calls if c[0] == "input"][0]
    assert inp[1][5:] == ("net-define", "--validate", "--file", "/dev/stdin")
    assert "<name>kvmtest-net</name>" in inp[2]
    assert res["network"]["active"] is True and res["network"]["autostart"] is True


def test_define_network_refused_writes_nothing():
    s = net_sim()
    with pytest.raises(ko.OpError) as e:
        kn.define_network(s, {"name": "bad", "mode": "nat", "subnet": "172.16.5.0/24"})
    assert e.value.kind == "refused" and "172.16.0.0/16" in e.value.message and s.writes == []


def test_define_network_rollback_on_start_failure():
    s = net_sim()
    s.fail_next["net-start"] = (1, "error: Failed to start network: bridge busy\n")
    with pytest.raises(kh.KvmError) as e:
        kn.define_network(s, {"name": "n", "mode": "isolated", "subnet": "10.1.1.0/24"})
    assert [w[0] for w in s.writes] == ["net-define", "net-start", "net-undefine"]
    assert "n" not in s.networks and e.value.leftovers == []


def test_define_network_dry_run():
    s = net_sim()
    res = kn.define_network(s, {"name": "n", "mode": "isolated", "subnet": "10.1.1.0/24"},
                            dry_run=True)
    assert res["dry_run"] and "<name>n</name>" in res["xml"] and s.writes == []


def nic_dom(name, net="kvmtest-net"):
    return (f"<domain type='kvm'><name>{name}</name><uuid>u-{name}</uuid><devices>"
            f"<interface type='network'><mac address='52:54:00:00:00:01'/>"
            f"<source network='{net}'/></interface></devices></domain>")


def test_stop_network_refused_while_a_running_vm_has_a_card():
    s = net_sim()
    s.add_net("kvmtest-net", "10.213.77.0/24")
    s.add_domain("vm1", nic_dom("vm1"), state="running", live=nic_dom("vm1"))
    with pytest.raises(ko.OpError, match="running VM has a card on it .vm1."):
        kn.stop_network(s, "kvmtest-net")
    assert s.writes == []
    s.domains["vm1"].update(state="shut off", live=None)
    kn.stop_network(s, "kvmtest-net")
    assert s.writes == [("net-destroy", "--network", "kvmtest-net")]


def test_delete_network_refused_by_an_inactive_definition():
    s = net_sim()
    s.add_net("kvmtest-net", "10.213.77.0/24")
    s.add_domain("vm1", nic_dom("vm1"))
    with pytest.raises(ko.OpError, match="used by vm1"):
        kn.delete_network(s, "kvmtest-net")
    assert s.writes == []


def test_delete_network_refused_when_a_definition_is_unreadable():
    s = net_sim()
    s.add_net("kvmtest-net", "10.213.77.0/24")
    s.add_domain("vm1", nic_dom("vm1", "other"))
    s.fail["dumpxml"] = (1, "error: internal error\n")
    with pytest.raises(ko.OpError, match="could not prove"):
        kn.delete_network(s, "kvmtest-net")


@pytest.mark.parametrize("active,verbs", [(True, ["net-destroy", "net-undefine"]),
                                          (False, ["net-undefine"])])
def test_delete_network_sequence(active, verbs):
    s = net_sim()
    s.add_net("kvmtest-net", "10.213.77.0/24", active=active)
    s.add_domain("vm1", nic_dom("vm1", "other"))
    kn.delete_network(s, "kvmtest-net")
    assert [w[0] for w in s.writes] == verbs and "kvmtest-net" not in s.networks


def test_autostart_and_start():
    s = net_sim()
    kn.autostart_network(s, "default", enable=False)
    kn.start_network(s, "default")
    assert s.writes == [("net-autostart", "--network", "default", "--disable"),
                        ("net-start", "--network", "default")]


def test_host_links_failure_is_named():
    s = net_sim()
    s.link_json = "not json"
    with pytest.raises(ko.OpError, match="unreadable output of ip"):
        kn.host_links(s)


@pytest.mark.parametrize("name", ["a;rm -rf /", "$(id)", "x y", "-x", "a\nb", "'q'"])
def test_hostile_network_name_is_one_remote_argument(name):
    seen = []

    def runner(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    st = tempfile.mkdtemp(prefix="kvmn-", dir="/tmp")
    try:
        decl = {"name": "node2", "address": "172.16.1.12", "user": "ju",
                "ssh_key": "ssh/node2_id", "known_hosts": "ssh/node2_known"}
        h = kh.VirshHost(decl, st, runner=runner)
        h.virsh("net-destroy", "--network", name)
        words = shlex.split(seen[-1][-1])
        assert words[-3:] == ["net-destroy", "--network", name]
    finally:
        shutil.rmtree(st, ignore_errors=True)
