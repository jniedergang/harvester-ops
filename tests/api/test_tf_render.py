"""v1.4.39 — comprehensive coverage of `_render_tf_for_kind()` and its
helpers. These checks DON'T spin up terraform; they assert the HCL we
generate for each kind has every field the user expects, in the right
form, and that the various edge cases (heredoc, legacy shape, missing
required fields, unknown kind) behave correctly.

If a regression sneaks in here, the runtime symptom is usually one of:
  - terraform plan failing on an unknown attribute, or
  - the VM booting without cloud-init / wrong size disk / etc.

Naming convention: `test_<kind>_<scenario>`.
"""

import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp


# ---------------------------------------------------------------------------
# _hcl_str — quoting helper
# ---------------------------------------------------------------------------

def test_hcl_str_quotes_simple_value():
    assert wapp._hcl_str("hello") == '"hello"'


def test_hcl_str_escapes_double_quotes():
    assert wapp._hcl_str('a"b') == '"a\\"b"'


def test_hcl_str_escapes_backslash():
    assert wapp._hcl_str("a\\b") == '"a\\\\b"'


def test_hcl_str_uses_heredoc_for_newlines():
    out = wapp._hcl_str("line1\nline2")
    assert out.startswith("<<-EOT\n")
    assert out.endswith("\nEOT")
    assert "line1\nline2" in out


def test_hcl_str_none_returns_empty_string_literal():
    assert wapp._hcl_str(None) == '""'


def test_hcl_id_sanitizes_identifiers():
    assert wapp._hcl_id("my-vm-01") == "my_vm_01"
    assert wapp._hcl_id("foo.bar.baz") == "foo_bar_baz"
    assert wapp._hcl_id("") == "x"


# ---------------------------------------------------------------------------
# Disk / NIC / cloudinit nested-block renderers
# ---------------------------------------------------------------------------

def test_disk_with_image_omits_storage_class_name():
    out = wapp._render_disk_block({
        "image": "default/img-x", "storage_class_name": "longhorn",
        "size": "10Gi",
    })
    assert 'image      = "default/img-x"' in out
    assert "storage_class_name" not in out


def test_disk_blank_keeps_storage_class_name():
    out = wapp._render_disk_block({
        "size": "10Gi", "storage_class_name": "harvester-longhorn",
    })
    assert 'storage_class_name = "harvester-longhorn"' in out
    assert "image      =" not in out


def test_disk_defaults_applied_when_keys_absent():
    out = wapp._render_disk_block({})
    assert 'name       = "rootdisk"' in out
    assert 'type       = "disk"' in out
    assert 'bus        = "virtio"' in out
    assert 'size       = "20Gi"' in out
    assert "boot_order = 1" in out


def test_disk_boot_order_coerced_to_int():
    out = wapp._render_disk_block({"boot_order": "3"})
    assert "boot_order = 3" in out


def test_nic_default_bridge_and_virtio_model():
    out = wapp._render_nic_block({})
    assert 'name         = "nic-1"' in out
    assert 'type         = "bridge"' in out
    assert 'model        = "virtio"' in out


def test_nic_emits_network_name_when_set():
    out = wapp._render_nic_block({"network_name": "default/production"})
    assert 'network_name = "default/production"' in out


def test_nic_emits_wait_for_lease_only_when_true():
    out_true = wapp._render_nic_block({"wait_for_lease": True})
    out_false = wapp._render_nic_block({"wait_for_lease": False})
    assert "wait_for_lease = true" in out_true
    assert "wait_for_lease" not in out_false


def test_cloudinit_user_data_heredoc_when_multiline():
    out = wapp._render_cloudinit_block({
        "user_data": "#cloud-config\nhostname: foo\nruncmd:\n  - echo hi"
    })
    assert "user_data = <<-EOT" in out
    assert "hostname: foo" in out
    assert "EOT" in out


def test_cloudinit_user_data_secret_name_emitted():
    out = wapp._render_cloudinit_block({
        "user_data_secret_name": "default/my-ci-secret",
    })
    assert 'user_data_secret_name = "default/my-ci-secret"' in out


# ---------------------------------------------------------------------------
# VM rendering — covers ssh_keys, hostname, efi, multi-{disk,nic}, cloudinit,
# legacy flat shape, missing required fields.
# ---------------------------------------------------------------------------

def _vm_minimum_nested():
    """Minimal nested shape that yields a valid VM .tf snippet."""
    return {
        "name": "v1",
        "disk": [{"image": "default/img-x", "size": "10Gi"}],
        "network_interface": [{"network_name": "default/management"}],
    }


def test_vm_minimum_emits_resource_block():
    out = wapp._render_tf_for_kind("vm", _vm_minimum_nested())
    assert 'resource "harvester_virtualmachine" "v1"' in out
    assert 'name      = "v1"' in out
    assert 'namespace = "default"' in out  # default applied
    assert "cpu    = 2" in out
    assert 'memory = "4Gi"' in out
    assert 'run_strategy = "RerunOnFailure"' in out


def test_vm_emits_header_so_runner_can_split_it():
    out = wapp._render_tf_for_kind("vm", _vm_minimum_nested())
    assert out.startswith(wapp._TF_HEADER)


def test_vm_returns_empty_when_disk_missing():
    spec = {"name": "v1", "network_interface": [{"network_name": "x"}]}
    assert wapp._render_tf_for_kind("vm", spec) == ""


def test_vm_returns_empty_when_nic_missing():
    spec = {"name": "v1", "disk": [{"image": "default/img"}]}
    assert wapp._render_tf_for_kind("vm", spec) == ""


def test_vm_emits_hostname_efi_secure_boot_when_set():
    spec = {**_vm_minimum_nested(),
            "hostname": "myhost", "efi": True, "secure_boot": True}
    out = wapp._render_tf_for_kind("vm", spec)
    assert 'hostname = "myhost"' in out
    assert "efi = true" in out
    assert "secure_boot = true" in out


def test_vm_omits_hostname_efi_when_not_set():
    out = wapp._render_tf_for_kind("vm", _vm_minimum_nested())
    assert "hostname" not in out
    assert "efi" not in out
    assert "secure_boot" not in out


def test_vm_ssh_keys_list_rendered_as_hcl_array():
    spec = {**_vm_minimum_nested(),
            "ssh_keys": ["default/key-a", "default/key-b"]}
    out = wapp._render_tf_for_kind("vm", spec)
    assert 'ssh_keys = ["default/key-a", "default/key-b"]' in out


def test_vm_ssh_keys_single_string_normalized_to_list():
    spec = {**_vm_minimum_nested(), "ssh_keys": "default/key-a"}
    out = wapp._render_tf_for_kind("vm", spec)
    assert 'ssh_keys = ["default/key-a"]' in out


def test_vm_description_quoted_or_heredoc():
    spec = {**_vm_minimum_nested(), "description": "single line"}
    out = wapp._render_tf_for_kind("vm", spec)
    assert 'description = "single line"' in out


def test_vm_legacy_flat_shape_still_works():
    """v1.4.36 fix: forms posting `image_id` / `network_id` / `disk_size`
    (the pre-refactor flat shape) must still render. New schema users
    use the nested shape; we keep both alive for back-compat."""
    spec = {
        "name": "v-old", "namespace": "default",
        "image_id": "default/img-x",
        "network_id": "default/management",
        "disk_size": "30Gi",
        "ssh_user": "sles",
    }
    out = wapp._render_tf_for_kind("vm", spec)
    assert 'resource "harvester_virtualmachine" "v_old"' in out
    assert 'image      = "default/img-x"' in out
    assert 'size       = "30Gi"' in out
    assert 'network_name = "default/management"' in out
    assert "tags = { ssh-user" in out  # legacy tag still emitted


def test_vm_multiple_disks_all_emitted_with_their_boot_order():
    spec = {
        **_vm_minimum_nested(),
        "disk": [
            {"name": "root", "image": "default/img-x", "size": "20Gi",
             "boot_order": 1},
            {"name": "data", "size": "100Gi", "boot_order": 0,
             "storage_class_name": "harvester-longhorn"},
        ],
    }
    out = wapp._render_tf_for_kind("vm", spec)
    # Both blocks present
    assert out.count("disk {") == 2
    assert 'name       = "root"' in out
    assert 'name       = "data"' in out
    assert 'storage_class_name = "harvester-longhorn"' in out
    assert "boot_order = 1" in out
    assert "boot_order = 0" in out


def test_vm_multiple_nics_all_emitted():
    spec = {
        **_vm_minimum_nested(),
        "network_interface": [
            {"name": "nic-1", "network_name": "default/management"},
            {"name": "nic-2", "network_name": "default/production"},
        ],
    }
    out = wapp._render_tf_for_kind("vm", spec)
    assert out.count("network_interface {") == 2
    assert 'network_name = "default/management"' in out
    assert 'network_name = "default/production"' in out


def test_vm_cloudinit_block_emitted_when_present():
    spec = {**_vm_minimum_nested(),
            "cloudinit": {"user_data": "#cloud-config\nhostname: x"}}
    out = wapp._render_tf_for_kind("vm", spec)
    assert "cloudinit {" in out
    assert "user_data = <<-EOT" in out


def test_vm_cloudinit_block_accepted_as_single_element_list():
    """tf-form.js renders nested blocks (max:1 included) as JS arrays,
    so the spec on the wire is `cloudinit: [{…}]` even when there's at
    most one. The backend must unwrap that."""
    spec = {**_vm_minimum_nested(),
            "cloudinit": [{"user_data": "#cloud-config\nhostname: x"}]}
    out = wapp._render_tf_for_kind("vm", spec)
    assert "cloudinit {" in out
    assert "user_data = <<-EOT" in out


def test_vm_cpu_and_memory_coerced_and_defaulted():
    spec = {**_vm_minimum_nested(), "cpu": "8", "memory": "16Gi"}
    out = wapp._render_tf_for_kind("vm", spec)
    assert "cpu    = 8" in out
    assert 'memory = "16Gi"' in out


def test_vm_run_strategy_propagated():
    spec = {**_vm_minimum_nested(), "run_strategy": "Halted"}
    out = wapp._render_tf_for_kind("vm", spec)
    assert 'run_strategy = "Halted"' in out


# ---------------------------------------------------------------------------
# Image rendering — every source_type
# ---------------------------------------------------------------------------

def test_image_download_minimum():
    spec = {
        "name": "img-1", "display_name": "openSUSE Tumbleweed",
        "source_type": "download",
        "url": "https://example.com/cloud.qcow2",
    }
    out = wapp._render_tf_for_kind("image", spec)
    assert 'resource "harvester_image" "img_1"' in out
    assert 'name         = "img-1"' in out
    assert 'display_name = "openSUSE Tumbleweed"' in out
    assert 'source_type  = "download"' in out
    assert 'url          = "https://example.com/cloud.qcow2"' in out


def test_image_upload_url_optional():
    """source_type=upload uploads happen out-of-band (file path), so the
    URL is not required. The rendered .tf still parses."""
    spec = {
        "name": "img-up", "display_name": "Uploaded",
        "source_type": "upload",
    }
    out = wapp._render_tf_for_kind("image", spec)
    assert 'source_type  = "upload"' in out
    assert "url" not in out


def test_image_emits_storage_class_name_and_checksum():
    spec = {
        "name": "img-1", "display_name": "img",
        "source_type": "download",
        "url": "https://example.com/cloud.qcow2",
        "storage_class_name": "harvester-longhorn",
        "checksum": "sha512:" + "0" * 128,
    }
    out = wapp._render_tf_for_kind("image", spec)
    assert 'storage_class_name = "harvester-longhorn"' in out
    assert 'checksum     = "sha512:' in out


def test_image_returns_empty_without_name():
    assert wapp._render_tf_for_kind(
        "image", {"display_name": "x", "source_type": "download"}) == ""


def test_image_display_name_falls_back_to_name():
    spec = {"name": "fb", "source_type": "download",
            "url": "https://example.com/x.qcow2"}
    out = wapp._render_tf_for_kind("image", spec)
    assert 'display_name = "fb"' in out


# ---------------------------------------------------------------------------
# ssh_key + raw
# ---------------------------------------------------------------------------

def test_ssh_key_minimum():
    spec = {
        "name": "key1", "namespace": "default",
        "public_key": "ssh-ed25519 AAAA my@host",
    }
    out = wapp._render_tf_for_kind("ssh_key", spec)
    assert 'resource "harvester_ssh_key" "key1"' in out
    assert 'name      = "key1"' in out
    assert 'public_key = "ssh-ed25519 AAAA my@host"' in out


def test_ssh_key_strips_surrounding_whitespace():
    spec = {"name": "k", "public_key": "  ssh-ed25519 AAAA  \n"}
    out = wapp._render_tf_for_kind("ssh_key", spec)
    assert 'public_key = "ssh-ed25519 AAAA"' in out


def test_ssh_key_returns_empty_without_required_fields():
    assert wapp._render_tf_for_kind("ssh_key", {"name": "k"}) == ""
    assert wapp._render_tf_for_kind("ssh_key", {"public_key": "x"}) == ""


def test_raw_passes_through_verbatim():
    raw = ('resource "harvester_virtualmachine" "raw_vm" { '
           'name = "raw" }')
    out = wapp._render_tf_for_kind("raw", {"tf": raw})
    assert out == raw


def test_raw_empty_when_tf_missing():
    assert wapp._render_tf_for_kind("raw", {}) == ""


# ---------------------------------------------------------------------------
# Dispatcher behaviour
# ---------------------------------------------------------------------------

def test_unknown_kind_returns_empty_string():
    """The /apply endpoint converts "" into a 400 with the supported
    list. Anything truthy here would falsely succeed."""
    assert wapp._render_tf_for_kind("does_not_exist", {"name": "x"}) == ""


def test_unknown_kind_with_legitimate_spec_still_empty():
    assert wapp._render_tf_for_kind("totally_unknown",
                                     _vm_minimum_nested()) == ""


# ---------------------------------------------------------------------------
# v1.52.1 : le type de cloud-init tel que le provider l'accepte.
#
# Vu à l'audit du 26/09/2026 : le formulaire proposait `nocloud` et
# `configdrive`, le provider v1.9.0 refuse tout sauf `noCloud` et
# `configDrive` (constantes de harvester/pkg/builder/cloudinit.go), si bien
# qu'aucune VM ne passait le plan.
# ---------------------------------------------------------------------------
PROVIDER_CLOUDINIT_TYPES = {"noCloud", "configDrive"}


def test_cloudinit_type_defaults_to_the_provider_value():
    assert 'type = "noCloud"' in wapp._render_cloudinit_block({})


@pytest.mark.parametrize("given, rendered", [
    ("nocloud", "noCloud"), ("configdrive", "configDrive"),
    ("noCloud", "noCloud"), ("configDrive", "configDrive")])
def test_cloudinit_types_saved_before_the_fix_are_translated(given, rendered):
    assert f'type = "{rendered}"' in wapp._render_cloudinit_block({"type": given})


def test_the_form_only_offers_values_the_provider_accepts():
    import re
    text = (ROOT / "web" / "static" / "js" / "tf-schema.js").read_text()
    text = text[text.index("cloudinit: {"):]
    m = re.search(r"name: 'type', type: 'enum', default: '(\w+)',\s*enum_values: \[([^\]]*)\]", text)
    assert m, "cloud-init type field not found in tf-schema.js"
    offered = set(re.findall(r"'(\w+)'", m.group(2)))
    assert offered == PROVIDER_CLOUDINIT_TYPES and m.group(1) in offered


# ---------------------------------------------------------------------------
# v1.52.1 : une VM détruite emporte ses disques.
#
# Vu à l'audit du 26/09/2026, puis à l'essai réel de la 1.52.1 : la VM
# détruite, son disque de 10 Gi restait Bound dans Harvester.
# ---------------------------------------------------------------------------
def test_disks_go_with_the_vm_unless_asked_otherwise():
    assert "auto_delete = true" in wapp._render_disk_block({"image": "default/img"})
    assert "auto_delete = false" in wapp._render_disk_block({"image": "default/img", "auto_delete": False})
    text = (ROOT / "web" / "static" / "js" / "tf-schema.js").read_text()
    assert "name: 'auto_delete', type: 'bool', default: true" in text


# ---------------------------------------------------------------------------
# 2.4.0 : réseau de VM, classe de stockage, pool d'adresses (tâche 8).
#
# Champs et noms exactement ceux du provider Harvester 1.9.0 (constats du plan
# 2026-11-03). Une valeur refusée lève tf_kvm.SpecError avec un message qui
# dit laquelle ; le rendu d'une déclaration la range dans ses erreurs.
#
# Mutations jouées (rapport de la tâche 8) : passerelle hors sous-réseau
# acceptée, vlan_id 4095 accepté ; chacune casse un cas de REFUSALS.
# ---------------------------------------------------------------------------
import tf_kvm as _tfk  # noqa: E402


def _net(**over):
    s = {"name": "vlan42", "cluster_network_name": "mgmt", "vlan_id": 42}
    s.update(over)
    return s


def _sc(**over):
    s = {"name": "fast", "parameters": {"migratable": "true"}}
    s.update(over)
    return s


def _rng(**over):
    r = {"start": "10.0.0.10", "end": "10.0.0.20", "subnet": "10.0.0.0/24", "gateway": "10.0.0.1"}
    r.update(over)
    return r


def _pool(**over):
    s = {"name": "pool-a", "range": [_rng()]}
    s.update(over)
    return s


RENDERS = [
    # (genre, spec, lignes attendues, lignes absentes)
    ("network", _net(),
     ['resource "harvester_network" "vlan42" {', 'name                 = "vlan42"',
      'namespace            = "default"', 'cluster_network_name = "mgmt"', "vlan_id              = 42"],
     ["route_mode", "labels", "description", "config"]),
    ("network", _net(vlan_id=0), ["vlan_id              = 0"], []),
    ("network", _net(vlan_id="4094"), ["vlan_id              = 4094"], []),
    ("network", _net(namespace="lab", description="Site A", route_mode="manual",
                     route_cidr="172.16.42.0/24", route_gateway="172.16.42.1",
                     config='{"cniVersion": "0.3.1"}',
                     labels={"site": "a", "team.io/owner": "ops"}),
     ['namespace            = "lab"', 'description = "Site A"', 'route_mode = "manual"',
      'route_cidr = "172.16.42.0/24"', 'route_gateway = "172.16.42.1"',
      'config = "{\\"cniVersion\\": \\"0.3.1\\"}"',
      "labels = {", '"site" = "a"', '"team.io/owner" = "ops"'], []),
    ("network", _net(route_mode="auto", route_dhcp_server_ip="172.16.42.2"),
     ['route_mode = "auto"', 'route_dhcp_server_ip = "172.16.42.2"'], ["route_cidr", "route_gateway"]),
    ("storageclass", _sc(),
     ['resource "harvester_storageclass" "fast" {', 'name = "fast"', "parameters = {",
      '"migratable" = "true"'],
     ["is_default", "allow_volume_expansion", "allowed_topologies", "reclaim_policy"]),
    ("storageclass", _sc(parameters={"migratable": True, "numberOfReplicas": 3}),
     ['"migratable" = "true"', '"numberOfReplicas" = "3"'], []),
    ("storageclass", _sc(volume_provisioner="driver.longhorn.io", reclaim_policy="Retain",
                         volume_binding_mode="WaitForFirstConsumer", allow_volume_expansion=True,
                         is_default="false",
                         match_label_expressions=[{"key": "zone", "values": "a, b"},
                                                  {"key": "rack", "values": ["r1"]}]),
     ['volume_provisioner = "driver.longhorn.io"', 'reclaim_policy = "Retain"',
      'volume_binding_mode = "WaitForFirstConsumer"', "allow_volume_expansion = true",
      "is_default = false", "allowed_topologies {", 'key    = "zone"', 'values = ["a", "b"]',
      'values = ["r1"]'], []),
    ("ippool", _pool(),
     ['resource "harvester_ippool" "pool_a" {', 'name = "pool-a"', "range {",
      'start   = "10.0.0.10"', 'end     = "10.0.0.20"', 'subnet  = "10.0.0.0/24"',
      'gateway = "10.0.0.1"'],
     ["selector", "namespace"]),
    ("ippool", _pool(range=[_rng(), _rng(start="10.0.0.30", end="10.0.0.30")],
                     selector=[{"network": "default/vlan42", "priority": 5}],
                     scope=[{"guest_cluster": "rke2-a", "namespace": "default"}, {"project": "p-1"}]),
     ['start   = "10.0.0.30"', "selector {", 'network  = "default/vlan42"', "priority = 5",
      "scope {", 'guest_cluster = "rke2-a"', 'namespace = "default"', 'project = "p-1"'], []),
    ("ippool", _pool(selector={"priority": "0"}), ["priority = 0"], ["network  ="]),
    ("ippool", _pool(range=[_rng(start="fd00::10", end="fd00::20", subnet="fd00::/64", gateway="fd00::1")]),
     ['subnet  = "fd00::/64"'], []),
]


@pytest.mark.parametrize("kind, spec, present, absent", RENDERS)
def test_new_kinds_render(kind, spec, present, absent):
    out = wapp._render_tf_for_kind(kind, spec)
    assert out.startswith(wapp._TF_HEADER)
    body = out[len(wapp._TF_HEADER):]
    for line in present:
        assert line in body, (line, body)
    for word in absent:
        assert word not in body, (word, body)
    assert body.count("{") == body.count("}")


REFUSALS = [
    # (genre, spec, extrait du message)
    ("network", _net(name=""), "name is required"),
    ("network", _net(name="Bad_Name"), "not a valid Kubernetes name"),
    ("network", _net(namespace="Bad NS"), "namespace"),
    ("network", _net(cluster_network_name=""), "cluster_network_name is required"),
    ("network", _net(vlan_id=None), "vlan_id is required"),
    ("network", _net(vlan_id=4095), "between 0 and 4094"),
    ("network", _net(vlan_id=-1), "between 0 and 4094"),
    ("network", _net(vlan_id="12a"), "whole number"),
    ("network", _net(vlan_id=True), "whole number"),
    ("network", _net(route_mode="static"), "route_mode must be one of auto, manual"),
    ("network", _net(route_mode="manual", route_cidr="10.1.0.0/24"), "needs route_cidr and route_gateway"),
    ("network", _net(route_cidr="10.1.0.0/33"), "route_cidr"),
    ("network", _net(route_cidr="10.1.0.0/24", route_gateway="10.2.0.1"), "not inside route_cidr"),
    ("network", _net(route_gateway="10.2.0.300"), "route_gateway"),
    ("network", _net(route_dhcp_server_ip="dhcp"), "route_dhcp_server_ip"),
    ("network", _net(route_dhcp_server_ip="10.1.0.2", route_cidr="10.1.0.0/24"), "cannot go with route_cidr"),
    ("network", _net(route_dhcp_server_ip="10.1.0.2", route_gateway="10.1.0.1"), "cannot go with route_cidr"),
    ("network", _net(config="not json"), "config must be a JSON object"),
    ("network", _net(config="[1]"), "config must be a JSON object"),
    ("network", _net(labels={" ": "x"}), "every entry needs a key"),
    ("network", _net(labels=[{"key": "a", "value": "1"}]), "labels must be a map"),
    ("storageclass", _sc(name=""), "name is required"),
    ("storageclass", _sc(parameters={}), "at least one entry"),
    ("storageclass", _sc(parameters=None), "at least one entry"),
    ("storageclass", _sc(parameters="migratable=true"), "parameters must be a map"),
    ("storageclass", _sc(reclaim_policy="Recycle"), "reclaim_policy must be one of Delete, Retain"),
    ("storageclass", _sc(volume_binding_mode="Later"), "volume_binding_mode"),
    ("storageclass", _sc(is_default="yes"), "is_default must be true or false"),
    ("storageclass", _sc(allow_volume_expansion="1"), "allow_volume_expansion must be true or false"),
    ("storageclass", _sc(match_label_expressions=[{"key": "zone", "values": ""}]),
     "match_label_expressions #1 needs a key and at least one value"),
    ("ippool", _pool(name="x_y"), "not a valid Kubernetes name"),
    ("ippool", _pool(range=[]), "at least one range"),
    ("ippool", _pool(range=[_rng(gateway="")]), "range #1: gateway is required"),
    ("ippool", _pool(range=[_rng(), _rng(start="")]), "range #2: start is required"),
    ("ippool", _pool(range=[_rng(subnet="10.0.0.0")]), None),   # /32 implicite : start hors sous-réseau
    ("ippool", _pool(range=[_rng(subnet="10.0.0/24")]), "is not a subnet"),
    ("ippool", _pool(range=[_rng(start="10.0.0.999")]), "range #1 start '10.0.0.999' is not an IP address"),
    ("ippool", _pool(range=[_rng(start="10.0.1.10")]), "range #1: start 10.0.1.10 is not inside subnet"),
    ("ippool", _pool(range=[_rng(end="10.0.1.20")]), "range #1: end 10.0.1.20 is not inside subnet"),
    ("ippool", _pool(range=[_rng(gateway="10.0.1.1")]), "range #1: gateway 10.0.1.1 is not inside subnet"),
    ("ippool", _pool(range=[_rng(start="10.0.0.30")]), "start 10.0.0.30 comes after end 10.0.0.20"),
    ("ippool", _pool(range=[_rng(start="fd00::10")]), "is not inside subnet"),
    ("ippool", _pool(selector=[{"priority": 1}, {"priority": 2}]), "at most one selector"),
    ("ippool", _pool(selector=[{"network": "a/b/c"}]), "network"),
    ("ippool", _pool(selector=[{"priority": -1}]), "priority must be between"),
    ("ippool", _pool(scope=[{}]), "scope #1 needs a guest cluster, a namespace or a project"),
]


@pytest.mark.parametrize("kind, spec, message", REFUSALS)
def test_new_kinds_refuse(kind, spec, message):
    with pytest.raises(_tfk.SpecError) as e:
        wapp._render_tf_for_kind(kind, spec)
    if message:
        assert message in str(e.value)


@pytest.mark.parametrize("kind, spec", [
    ("network", _net(description='${file("/etc/passwd")}')),
    ("storageclass", _sc(parameters={"a\"b": "%{ if true }x"})),
    ("ippool", _pool(scope=[{"project": "p\"} resource \"x\" \"y\" {"}])),
])
def test_new_kinds_quote_every_string_through_hcl_str(kind, spec, monkeypatch):
    """Toute chaîne passe par `_hcl_str` (règle de 2.3.0) : la remplacer par
    une sentinelle la fait apparaître à chaque valeur, aucune n'y échappe."""
    seen = []
    monkeypatch.setattr(wapp, "_hcl_str", lambda s: seen.append(s) or '"Q"')
    body = wapp._render_tf_for_kind(kind, spec)[len(wapp._TF_HEADER):]
    rest = body.split("\n", 1)[1]           # sans la ligne `resource "type" "id" {`
    import re
    assert seen and set(re.findall(r'"[^"]*"', rest)) == {'"Q"'}
    assert "passwd" not in body and "%{" not in body and 'resource "x"' not in body


def test_a_refused_resource_lands_in_the_declaration_errors():
    rendered, errors = wapp._tf_render_resources([
        {"id": "a" * 12, "kind": "network", "spec": _net(vlan_id=4095)},
        {"id": "b" * 12, "kind": "ippool", "spec": _pool()},
    ])
    assert errors == [{"index": 0, "kind": "network", "name": "vlan42",
                       "error": "vlan_id must be between 0 and 4094"}]
    assert [(r[0], r[4]) for r in rendered] == [("pool_a", "harvester_ippool.pool_a")]


@pytest.mark.parametrize("kind, spec, address", [
    ("network", _net(), "harvester_network.vlan42"),
    ("storageclass", _sc(), "harvester_storageclass.fast"),
    ("ippool", _pool(), "harvester_ippool.pool_a"),
])
def test_new_kinds_have_their_terraform_address(kind, spec, address):
    rendered, errors = wapp._tf_render_resources([{"id": "c" * 12, "kind": kind, "spec": spec}])
    assert errors == [] and rendered[0][4] == address
    assert f'resource "{address.split(".")[0]}" "{address.split(".")[1]}"' in rendered[0][3]


def test_new_kinds_go_to_harvester_targets_only():
    import tf_store
    for kind in ("network", "storageclass", "ippool"):
        assert kind in tf_store.KINDS_OF["harvester"] and kind not in tf_store.KINDS_OF["kvm"]
        _, errors = wapp._tf_render_resources([{"id": "d" * 12, "kind": kind, "spec": {}}], "kvm")
        assert errors[0]["error"] == f"a {kind} resource does not go to a kvm target"


def test_the_legacy_apply_route_answers_400_on_a_refused_value():
    client = wapp.app.test_client()
    wapp.app.config["TESTING"] = True
    orig = wapp._kubectl_for_cluster
    wapp._kubectl_for_cluster = lambda c: ["kubectl"]
    try:
        r = client.post("/api/terraform/harv1/apply", json={"kind": "network", "spec": _net(vlan_id=5000)})
    finally:
        wapp._kubectl_for_cluster = orig
    assert r.status_code in (400, 401)
    if r.status_code == 400:
        assert r.get_json() == {"error": "vlan_id must be between 0 and 4094", "kind": "network"}


# ---------------------------------------------------------------------------
# Rendu relu par `terraform validate` contre le provider du miroir (sinon skip)
# ---------------------------------------------------------------------------
def test_rendered_hcl_passes_terraform_validate(tmp_path):
    import os
    import shutil
    if not wapp._tf_provider_binary() or not shutil.which(wapp.TF_BIN):
        pytest.skip("Harvester provider or terraform not installed")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "_providers.tf").write_text(wapp._TF_HEADER)
    for i, (kind, spec, _p, _a) in enumerate(RENDERS):
        spec = dict(spec, name=f"r{i}")
        (ws / f"r{i}.tf").write_text(wapp._render_tf_for_kind(kind, spec)[len(wapp._TF_HEADER):])
    wapp._tf_plugin_cache_init(ws)
    kc = str(ws / "kubeconfig")
    rc, out, err = wapp._tf_run_cmd(ws, kc, ["init", "-input=false", "-no-color"], timeout=120)
    assert rc == 0, err[-800:]
    rc, out, err = wapp._tf_run_cmd(ws, kc, ["validate", "-no-color"], timeout=120)
    assert rc == 0, (out + err)[-1500:]
    assert os.path.exists(ws / ".terraform")


# ---------------------------------------------------------------------------
# Modèles de déclaration (tâche 2) : les trois genres y entrent sans autre
# code ; une chaîne qui n'est qu'une variable prend le type du champ
# (TF_TEMPLATE_FIELD_TYPES), puis le rendu HCL l'écrit sans guillemets.
# ---------------------------------------------------------------------------
def test_templates_of_the_new_kinds_render_typed_values():
    sys.path.insert(0, str(ROOT / "bin" / "lib"))
    import deploy_plans as dp
    import tf_store
    resources = tf_store.clean_resources([
        {"id": "1" * 12, "kind": "network",
         "spec": {"name": "{{net}}", "cluster_network_name": "mgmt", "vlan_id": "{{vlan}}"}},
        {"id": "2" * 12, "kind": "storageclass",
         "spec": {"name": "fast", "is_default": "{{default}}", "allow_volume_expansion": "{{grow}}",
                  "parameters": {"migratable": "{{grow}}", "csi.storage.k8s.io/provisioner-secret-name": "enc"},
                  "match_label_expressions": [{"key": "zone", "values": "{{zone}}"}]}},
        {"id": "3" * 12, "kind": "ippool",
         "spec": {"name": "pool", "range": [{"start": "{{first}}", "end": "10.0.0.20",
                                             "subnet": "10.0.0.0/24", "gateway": "10.0.0.1"}],
                  "selector": [{"network": "default/{{net}}", "priority": "{{prio}}"}]}},
    ])
    plan = {"name": "site", "phases": {"terraform": {"enabled": True, "templates": [{"template": "t"}]}}}
    values = {"net": "vlan42", "vlan": "42", "default": "false", "grow": "true", "first": "10.0.0.10",
              "prio": "7", "zone": "a"}
    [(name, out)] = dp.render_templates(plan, {"t": {"resources": resources}}, values,
                                        wapp._tf_template_field_type)
    net, sc, pool = (r["spec"] for r in out)
    assert net["vlan_id"] == 42 and net["name"] == "vlan42"
    assert sc["is_default"] is False and sc["allow_volume_expansion"] is True
    # texte : reste une chaîne ; ni la clé d'étiquette ni le nom de Secret ne sont retirés
    assert sc["parameters"] == {"migratable": "true", "csi.storage.k8s.io/provisioner-secret-name": "enc"}
    assert sc["match_label_expressions"] == [{"key": "zone", "values": "a"}]
    assert pool["selector"][0]["priority"] == 7 and pool["range"][0]["start"] == "10.0.0.10"
    rendered, errors = wapp._tf_render_resources(out)
    assert errors == []
    hcl = "".join(r[3] for r in rendered)
    for line in ("vlan_id              = 42", "is_default = false", "allow_volume_expansion = true",
                 "priority = 7", 'network  = "default/vlan42"'):
        assert line in hcl


def test_template_field_types_of_the_new_kinds():
    assert wapp.TF_TEMPLATE_FIELD_TYPES["network"] == {"vlan_id": "int"}
    assert wapp.TF_TEMPLATE_FIELD_TYPES["storageclass"] == {"allow_volume_expansion": "bool", "is_default": "bool"}
    assert wapp.TF_TEMPLATE_FIELD_TYPES["ippool"] == {"selector.priority": "int"}
    assert wapp._tf_template_field_type("network", "description") is None
