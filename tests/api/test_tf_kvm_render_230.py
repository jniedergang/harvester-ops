"""2.3.0 : le code Terraform des déclarations qui visent un hôte KVM.

Ce que les essais réels sur node2 ont appris et que ces tests verrouillent :
- l'URI ne porte jamais `no_verify` (le provider désactive la vérification de
  l'empreinte dès que le paramètre APPARAÎT, `no_verify=0` compris) ;
- le transport `qemu+sshcmd` passe par le script `kvm_ssh`, OpenSSH aux
  options strictes EN TÊTE (le client SSH en Go du transport `qemu+ssh`
  refusait l'hôte dont seule la clé ed25519 est épinglée) ;
- chaque domaine porte `cpu = host-model` (Leap 16 paniquait sur qemu64) et
  `features = { acpi, apic }` (sans eux, disque et carte réseau muets sur q35) ;
- toute valeur de formulaire entre dans le code échappée (`${`, `%{`,
  guillemets, retours à la ligne) : aucune injection HCL.
"""

import ipaddress
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import tf_kvm as k  # noqa: E402

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJdD ops@site"
HOST = {"name": "node2", "address": "172.16.1.12", "port": 22, "user": "ju", "uri": "qemu:///system"}
BASE = "/var/lib/libvirt/images/kvmtest-pool/base.qcow2"


def vm(**over):
    s = {"name": "kvmtest-tf1", "pool": "kvmtest-pool", "base_image": BASE, "network": "kvmtest-net"}
    s.update(over)
    return s


def rke2(**over):
    s = {"name": "lab", "servers": 1, "agents": 2, "pool": "kvmtest-pool", "base_image": BASE,
         "network": "kvmtest-net", "address_start": "10.213.77.50", "prefix": 24,
         "gateway": "10.213.77.1", "ssh_key": KEY}
    s.update(over)
    return s


# ---------------------------------------------------------------------------
# Échappement
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw, want", [
    ("plain", '"plain"'),
    ('a"b', '"a\\"b"'),
    ("a\\b", '"a\\\\b"'),
    ("l1\nl2\r\t", '"l1\\nl2\\r\\t"'),
    ("${file(\"/etc/passwd\")}", '"$${file(\\"/etc/passwd\\")}"'),
    ("%{ if true }x%{ endif }", '"%%{ if true }x%%{ endif }"'),
    ("$${already}", '"$$${already}"'),
    ("lone $ and % signs", '"lone $ and % signs"'),
    ("bell\x07", '"bell\\u0007"'),
])
def test_hcl_string(raw, want):
    assert k.hcl_string(raw) == want
    assert "\n" not in k.hcl_string(raw)


ROUND_TRIP = ["plain", 'q"uote', "back\\slash", "nl\nnext", "${var.x}", "%{if 1}", "$${y}", "$$${w}",
              "tab\tend", "mixed ${a} %{b} $ % \\ \""]


def _tofu():
    for cand in (os.environ.get("HARVESTER_OPS_TF_BIN"), shutil.which("tofu"), shutil.which("terraform")):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _evaluate(tmp_path, escape):
    """Valeurs que Terraform/OpenTofu lit pour chaque chaîne échappée."""
    tofu = _tofu()
    if not tofu:
        pytest.skip("neither tofu nor terraform installed")
    body = "".join(f'output "o{i}" {{ value = {escape(s)} }}\n' for i, s in enumerate(ROUND_TRIP))
    (tmp_path / "main.tf").write_text(body)
    r = subprocess.run([tofu, "apply", "-auto-approve", "-no-color", "-input=false"], cwd=tmp_path,
                       capture_output=True, text=True, timeout=120,
                       env={**os.environ, "TF_CLI_CONFIG_FILE": "/dev/null", "TF_IN_AUTOMATION": "1"})
    if r.returncode != 0:
        return None
    o = subprocess.run([tofu, "output", "-json"], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    import json
    vals = json.loads(o.stdout)
    return [vals[f"o{i}"]["value"] for i in range(len(ROUND_TRIP))]


def test_escaped_strings_read_back_identical_in_terraform(tmp_path):
    assert _evaluate(tmp_path, k.hcl_string) == ROUND_TRIP


def test_the_round_trip_catches_an_escape_without_template_guard(tmp_path):
    """Mutation : sans la garde `${` -> `$${`, le contrôle précédent échoue."""
    mutant = lambda s: '"' + str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") \
        .replace("\t", "\\t") + '"'
    assert _evaluate(tmp_path, mutant) != ROUND_TRIP


# ---------------------------------------------------------------------------
# Connexion
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("host, want", [
    (HOST, "qemu+sshcmd://ju@172.16.1.12/system?command="),
    (dict(HOST, port=2222), "qemu+sshcmd://ju@172.16.1.12:2222/system?command="),
    (dict(HOST, address="fd00::12"), "qemu+sshcmd://ju@[fd00::12]/system?command="),
    (dict(HOST, uri="qemu:///session"), "qemu+sshcmd://ju@172.16.1.12/session?command="),
    (dict(HOST, uri=None), "qemu+sshcmd://ju@172.16.1.12/system?command="),
])
def test_connection_uri(host, want):
    expr = k.connection_uri_expr(host)
    assert want in expr
    assert 'urlencode("${abspath(path.module)}/kvm_ssh")' in expr
    for forbidden in ("no_verify", "known_hosts_verify", "keyfile", "knownhosts"):
        assert forbidden not in expr


@pytest.mark.parametrize("uri", ["qemu+ssh://other/system", "qemu+tls://h/system", "qemu:///embed"])
def test_a_custom_libvirt_uri_is_refused(uri):
    with pytest.raises(k.SpecError, match="custom URI"):
        k.connection_uri_expr(dict(HOST, uri=uri))


def test_the_header_names_the_provider_and_no_secret():
    h = k.header(HOST)
    assert 'source = "dmacvicar/libvirt"' in h and 'provider "libvirt"' in h
    assert "kvm_ssh_key" not in h and "PRIVATE" not in h


def test_the_ssh_wrapper_forces_strict_options_first(tmp_path):
    w = k.ssh_wrapper(tmp_path / "ws")
    line = [x for x in w.splitlines() if x.startswith("exec ssh ")][0]
    assert line.endswith(' "$@"')
    head = line[:line.index('"$@"')]
    for opt in ("-F /dev/null", "StrictHostKeyChecking=yes", "GlobalKnownHostsFile=/dev/null",
                "IdentitiesOnly=yes", "IdentityAgent=none", "BatchMode=yes", "ControlMaster=no",
                "ProxyCommand=none", f"UserKnownHostsFile={tmp_path}/ws/kvm_known_hosts",
                f"IdentityFile={tmp_path}/ws/kvm_ssh_key"):
        assert opt in head, opt
    assert "accept-new" not in w and "StrictHostKeyChecking=no" not in w


def test_the_ssh_wrapper_quotes_a_path_with_spaces():
    w = k.ssh_wrapper("/srv/my state/ws")
    assert "'UserKnownHostsFile=\"/srv/my state/ws/kvm_known_hosts\"'" in w


@pytest.mark.parametrize("ws", ["/srv/50%/ws", '/srv/a"b/ws', "/srv/a\\b", "/srv/a\nb"])
def test_the_ssh_wrapper_refuses_a_path_ssh_would_rewrite(ws):
    with pytest.raises(k.SpecError):
        k.ssh_wrapper(ws)


# ---------------------------------------------------------------------------
# Ressources : rendu
# ---------------------------------------------------------------------------
def test_a_vm_on_a_base_image_with_cloud_init():
    hcl = k.render("kvm_vm", vm(ssh_key=KEY), "vm1")
    assert 'resource "libvirt_volume" "vm1_disk"' in hcl
    assert f'path   = "{BASE}"' in hcl
    assert 'resource "libvirt_cloudinit_disk" "vm1"' in hcl and 'resource "libvirt_volume" "vm1_seed"' in hcl
    assert 'device = "cdrom"' in hcl
    assert 'cpu         = { mode = "host-model" }' in hcl
    assert "features    = { acpi = true, apic = {} }" in hcl
    assert 'source = { network = { network = "kvmtest-net" } }' in hcl
    assert k.addresses("kvm_vm", "vm1", vm(ssh_key=KEY)) == [
        "libvirt_domain.vm1", "libvirt_volume.vm1_disk", "libvirt_cloudinit_disk.vm1", "libvirt_volume.vm1_seed"]


def test_a_vm_without_cloud_init_on_a_bridge():
    s = vm(base_image="", network_type="bridge", bridge="br0", network="", cpu_mode="host-passthrough")
    hcl = k.render("kvm_vm", s, "vm2")
    assert "cloudinit" not in hcl and "backing_store" not in hcl
    assert 'source = { bridge = { bridge = "br0" } }' in hcl and '"host-passthrough"' in hcl
    assert k.addresses("kvm_vm", "vm2", s) == ["libvirt_domain.vm2", "libvirt_volume.vm2_disk"]


def test_form_text_cannot_inject_terraform():
    evil = 'x" }\nresource "libvirt_domain" "pwn" { name = "${file("/etc/shadow")}'
    hcl = k.render("kvm_vm", vm(user_data="#cloud-config\n" + evil, description=evil), "vm3")
    assert hcl.count('resource "libvirt_domain"') == 1
    assert "${file(" not in hcl.replace("$${file(", "")


def test_volume_and_networks():
    assert 'format = { type = "raw" }' in k.render("kvm_volume", {"name": "d.img", "pool": "p", "format": "raw"}, "d")
    iso = k.render("kvm_network", {"name": "kvmtest-net-tf1", "mode": "isolated"}, "n")
    assert "forward" not in iso and "ips" not in iso
    nat = k.render("kvm_network", {"name": "n2", "mode": "nat", "address": "10.9.9.1", "prefix": 24}, "n2")
    assert 'forward = { mode = "nat" }' in nat
    assert 'start = "10.9.9.10", end = "10.9.9.254"' in nat


SPEC_ERRORS = [
    ("kvm_vm", vm(name="bad name"), "invalid name"),
    ("kvm_vm", vm(name="-lead"), "invalid name"),
    ("kvm_vm", vm(pool=""), "pool is required"),
    ("kvm_vm", vm(pool="a/b"), "invalid name"),
    ("kvm_vm", vm(base_image="relative.qcow2"), "invalid path"),
    ("kvm_vm", vm(base_image="/var/lib/../etc/shadow"), "invalid path"),
    ("kvm_vm", vm(base_image="/a b.qcow2"), "invalid path"),
    ("kvm_vm", vm(network=""), "network is required"),
    ("kvm_vm", vm(network_type="bridge", bridge="br0; reboot"), "invalid name"),
    ("kvm_vm", vm(network_type="macvtap"), "network_type must be one of"),
    ("kvm_vm", vm(ssh_key=KEY + "\n" + KEY), "OpenSSH format"),
    ("kvm_vm", vm(ssh_key="not a key"), "OpenSSH format"),
    ("kvm_vm", vm(vcpu=0), "between 1 and 64"),
    ("kvm_vm", vm(vcpu="2.5"), "must be a"),
    ("kvm_vm", vm(vcpu=True), "must be a number"),
    ("kvm_vm", vm(memory_mib=10), "between 64"),
    ("kvm_vm", vm(cpu_mode="qemu64"), "cpu_mode must be one of"),
    ("kvm_vm", vm(running="maybe"), "true or false"),
    ("kvm_vm", vm(user_data="x" * (64 * 1024 + 1)), "64 KiB"),
    ("kvm_volume", {"name": "v", "pool": "p", "format": "raw", "base_image": BASE}, "must be qcow2"),
    ("kvm_volume", {"name": "v", "pool": "p", "capacity_gib": 0}, "between 1"),
    ("kvm_network", {"name": "n", "mode": "nat"}, "needs an address"),
    ("kvm_network", {"name": "n", "mode": "nat", "address": "10.0.0.0", "prefix": 24}, "host address"),
    ("kvm_network", {"name": "n", "mode": "nat", "address": "10.0.0.1", "dhcp_start": "10.0.1.5"}, "DHCP range"),
    ("kvm_network", {"name": "n", "mode": "nat", "address": "fd00::1"}, "IPv4 only"),
    ("kvm_network", {"name": "n", "mode": "bridge"}, "mode must be one of"),
    ("kvm_rke2", rke2(servers=2), "must be odd"),
    ("kvm_rke2", rke2(agents=21), "between 0 and 20"),
    ("kvm_rke2", rke2(name="Lab"), "invalid name"),
    ("kvm_rke2", rke2(base_image=""), "base_image is required"),
    ("kvm_rke2", rke2(ssh_key=""), "ssh_key is required"),
    ("kvm_rke2", rke2(gateway="10.0.0.1"), "outside the subnet"),
    ("kvm_rke2", rke2(address_start="10.213.77.253", agents=3), "past the end"),
    ("kvm_rke2", rke2(address_start="10.213.77.0", servers=1, agents=0, gateway="10.213.77.1"), "past the end"),
    ("kvm_rke2", rke2(address_start="10.213.77.1"), "include the gateway"),
    ("kvm_rke2", rke2(rke2_version="1.33"), "rke2_version"),
    ("kvm_rke2", rke2(rke2_channel="stable; curl evil"), "rke2_channel"),
    ("kvm_rke2", rke2(memory_mib=1024), "between 2048"),
    ("kvm_rke2", rke2(ssh_user="Root"), "ssh_user"),
    ("kvm_rke2", rke2(dns="1.1.1.1,2.2.2.2,3.3.3.3,4.4.4.4"), "one to three"),
    ("kvm_rke2", rke2(dns="dns.example"), "invalid IPv4"),
    ("kvm_bogus", {}, "unknown KVM resource kind"),
]


@pytest.mark.parametrize("kind, spec, msg", SPEC_ERRORS)
def test_refused_specs(kind, spec, msg):
    with pytest.raises(k.SpecError, match=re.escape(msg)):
        k.render(kind, spec, "r")


# ---------------------------------------------------------------------------
# Modèle RKE2
# ---------------------------------------------------------------------------
def test_rke2_nodes_addresses_and_macs():
    nodes = k.rke2_nodes(rke2(servers=3, agents=2), seed="0123456789ab")
    assert [n["name"] for n in nodes] == ["lab-server-0", "lab-server-1", "lab-server-2", "lab-agent-0", "lab-agent-1"]
    assert [n["ip"] for n in nodes] == [f"10.213.77.{i}" for i in range(50, 55)]
    assert len({n["mac"] for n in nodes}) == 5 and all(n["mac"].startswith("52:54:00:") for n in nodes)
    assert nodes == k.rke2_nodes(rke2(servers=3, agents=2), seed="0123456789ab")      # stable d'un plan à l'autre
    assert nodes[0]["mac"] != k.rke2_nodes(rke2(servers=3, agents=2), seed="ffffffffffff")[0]["mac"]


@pytest.mark.parametrize("index, role, joins", [(0, "server", False), (1, "server", True), (3, "agent", True)])
def test_rke2_cloud_init_per_role(index, role, joins):
    spec = rke2(servers=3, agents=1, rke2_version="v1.33.5+rke2r1")
    nodes = k.rke2_nodes(spec)
    ud = k.rke2_user_data(spec, nodes[index], nodes[0]["ip"])
    assert ud.startswith("#cloud-config\n") and k.TOKEN_MARK in ud
    assert ud.count(k.TOKEN_MARK) == 1
    assert ("server: https://10.213.77.50:9345" in ud) is joins
    assert f"systemctl, enable, --now, rke2-{role}.service" in ud
    assert ("INSTALL_RKE2_TYPE=agent" in ud) is (role == "agent")
    assert "INSTALL_RKE2_VERSION=v1.33.5+rke2r1" in ud and "INSTALL_RKE2_METHOD=tar" in ud
    assert ("tls-san" in ud) is (role == "server")
    assert '"' + KEY + '"' in ud                       # clé citée : ": " ou "#" sans effet


def test_rke2_network_config_matches_the_mac():
    spec = rke2(dns="10.213.77.1, 9.9.9.9")
    n = k.rke2_nodes(spec)[1]
    net = k.rke2_network_config(spec, n)
    assert f'macaddress: "{n["mac"]}"' in net and "addresses: [10.213.77.51/24]" in net
    assert "via: 10.213.77.1" in net and "addresses: [10.213.77.1, 9.9.9.9]" in net


def test_rke2_render_keeps_the_token_out_of_the_code():
    hcl = k.render("kvm_rke2", rke2(), "lab", seed="abc")
    var = k.rke2_token_var("lab")
    assert f'variable "{var}"' in hcl and "sensitive   = true" in hcl
    assert f"${{var.{var}}}" in hcl and hcl.count(f"var.{var}") == 1
    assert "\x00" not in hcl and "TOKEN" not in hcl
    assert "count    = length(local.lab_nodes)" in hcl
    assert 'output "lab_nodes"' in hcl and 'output "lab_kubeconfig"' in hcl
    assert k.token_vars([("lab", "kvm_rke2"), ("x", "kvm_vm")]) == [var]


def test_rke2_addresses():
    assert k.addresses("kvm_rke2", "lab") == ["libvirt_domain.lab", "libvirt_volume.lab_disk",
                                              "libvirt_cloudinit_disk.lab", "libvirt_volume.lab_seed"]
    assert k.addresses("raw", "x") == []


def test_ip_math_stays_in_the_subnet():
    nodes = k.rke2_nodes(rke2(address_start="10.0.0.250", prefix=24, gateway="10.0.0.1", servers=1, agents=3))
    assert all(ipaddress.ip_address(n["ip"]) in ipaddress.ip_network("10.0.0.0/24") for n in nodes)
