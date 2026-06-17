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
