"""v1.8.0 — visual disk & network editors of the VM edit panel.

Covers the three layers of the redesign:
  1. The KubeVirt<->form mappers (pure JS, executed through node): the
     round-trip on a realistic spec must be semantically identity, new
     blank/image disks must produce volumeClaimTemplates entries, and the
     passthrough must protect volumes the form does not model.
  2. The backend additions: /api/pvcs list endpoint (list-endpoint
     conventions), the metadata-patch fix (a v1.6.x ternary popped the
     WHOLE metadata dict, silently dropping every annotation patch), and
     the vm-edit ActionRun (project rule: every mutation is tracked).
  3. Source-level wiring: engine reuse (no fake TF_SCHEMA kind), advanced
     JSON fallback kept, i18n keys present in EN and FR.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
WEB = ROOT / "web"
VM_EDIT_JS = WEB / "static" / "js" / "vm-edit.js"
JS = VM_EDIT_JS.read_text()

sys.path.insert(0, str(WEB))
import app as wapp  # noqa: E402

# A trimmed but structurally faithful Harvester VM (PVC root disk with
# bootOrder, cloud-init volume, cdrom without volume, bridge NIC with MAC).
VM_FIXTURE = {
    "metadata": {
        "name": "vmx", "namespace": "default",
        "annotations": {
            "harvesterhci.io/volumeClaimTemplates": json.dumps([
                {"metadata": {"name": "vmx-rootdisk-abc",
                              "annotations": {"harvesterhci.io/imageId": "default/image-x"}},
                 "spec": {"accessModes": ["ReadWriteMany"],
                          "resources": {"requests": {"storage": "20Gi"}},
                          "volumeMode": "Block",
                          "storageClassName": "longhorn-image-x"}},
            ]),
        },
    },
    "spec": {"template": {"spec": {
        "domain": {"devices": {
            "disks": [
                {"name": "rootdisk", "bootOrder": 1, "disk": {"bus": "virtio"}},
                {"name": "cloudinitdisk", "disk": {"bus": "virtio"}},
                {"name": "installcd", "cdrom": {"bus": "sata"}},
            ],
            "interfaces": [
                {"name": "nic-1", "bridge": {}, "model": "virtio",
                 "macAddress": "aa:bb:cc:dd:ee:ff"},
            ],
        }},
        "volumes": [
            {"name": "rootdisk",
             "persistentVolumeClaim": {"claimName": "vmx-rootdisk-abc"}},
            {"name": "cloudinitdisk",
             "cloudInitNoCloud": {"secretRef": {"name": "vmx-cloudinit"}}},
        ],
        "networks": [
            {"name": "nic-1", "multus": {"networkName": "default/production"}},
        ],
    }}},
}


def _run_node(script):
    """Evaluate vm-edit.js in node (window shim) and run `script`."""
    prelude = (
        "globalThis.window = globalThis;\n"
        + VM_EDIT_JS.read_text()
        + f"\nconst VM = {json.dumps(VM_FIXTURE)};\n"
        + "const M = window.VMEdit._mappers;\n"
        + "const sortByName = a => [...a].sort((x, y) => x.name.localeCompare(y.name));\n"
        + "const canon = o => JSON.stringify(o, Object.keys(JSON.parse(JSON.stringify(o))).sort());\n"
        + "const deepSorted = o => JSON.parse(JSON.stringify(o, (k, v) => "
        + "  (v && typeof v === 'object' && !Array.isArray(v)) ? "
        + "  Object.fromEntries(Object.entries(v).sort()) : v));\n"
        + "const eq = (a, b) => JSON.stringify(deepSorted(a)) === JSON.stringify(deepSorted(b));\n"
    )
    r = subprocess.run(["node", "-"], input=prelude + script,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node failed:\n{r.stderr}"
    return r.stdout.strip()


# ---------------------------------------------------------------------------
# 1. Mappers
# ---------------------------------------------------------------------------

def test_disks_round_trip_is_semantic_identity():
    out = _run_node("""
const { items, passthrough } = M.vmDisksToForm(VM);
const patch = M.formDisksToPatch(items, passthrough, VM);
const orig = VM.spec.template.spec;
const got = patch.spec.template.spec;
console.log(JSON.stringify({
  vols: eq(sortByName(got.volumes), sortByName(orig.volumes)),
  disks: eq(sortByName(got.domain.devices.disks),
            sortByName(orig.domain.devices.disks)),
  vctKept: JSON.parse(patch.metadata.annotations['harvesterhci.io/volumeClaimTemplates']).length,
  editable: items.length,
}));""")
    d = json.loads(out)
    assert d["vols"] and d["disks"], out
    assert d["vctKept"] == 1        # existing template preserved
    assert d["editable"] == 1       # only the PVC disk is form-editable


def test_disks_passthrough_protects_special_volumes():
    out = _run_node("""
const { items, passthrough } = M.vmDisksToForm(VM);
console.log(JSON.stringify({
  ptVols: passthrough.volumes.map(v => v.name).sort(),
  ptDisks: passthrough.disks.map(d => d.name).sort(),
}));""")
    d = json.loads(out)
    assert d["ptVols"] == ["cloudinitdisk"]
    assert d["ptDisks"] == ["cloudinitdisk", "installcd"]   # cdrom w/o volume


def test_new_blank_disk_creates_vct_entry():
    out = _run_node("""
const { items, passthrough } = M.vmDisksToForm(VM);
const patch = M.formDisksToPatch([...items,
  { name: 'data', device: 'disk', bus: 'virtio', boot_order: 0,
    source: 'blank', size: '5Gi', storage_class: 'harv-rep1' }], passthrough, VM);
const vct = JSON.parse(patch.metadata.annotations['harvesterhci.io/volumeClaimTemplates']);
const nw = vct[vct.length - 1];
console.log(JSON.stringify({
  n: vct.length, sc: nw.spec.storageClassName,
  size: nw.spec.resources.requests.storage,
  modes: nw.spec.accessModes, mode: nw.spec.volumeMode,
  volNames: patch.spec.template.spec.volumes.map(v => v.name).sort(),
}));""")
    d = json.loads(out)
    assert d["n"] == 2
    assert d["sc"] == "harv-rep1" and d["size"] == "5Gi"
    assert d["modes"] == ["ReadWriteMany"] and d["mode"] == "Block"
    assert "data" in d["volNames"]


def test_new_image_disk_inherits_image_storage_class():
    out = _run_node("""
const { items, passthrough } = M.vmDisksToForm(VM);
const patch = M.formDisksToPatch([...items,
  { name: 'img', device: 'disk', bus: 'virtio', boot_order: 2,
    source: 'image', size: '10Gi', image: 'default/image-y7abc' }], passthrough, VM);
const vct = JSON.parse(patch.metadata.annotations['harvesterhci.io/volumeClaimTemplates']);
const nw = vct[vct.length - 1];
console.log(JSON.stringify({ sc: nw.spec.storageClassName,
  imageId: nw.metadata.annotations['harvesterhci.io/imageId'] }));""")
    d = json.loads(out)
    assert d["sc"] == "longhorn-image-y7abc"
    assert d["imageId"] == "default/image-y7abc"


def test_removed_disk_drops_its_vct_entry():
    out = _run_node("""
const { passthrough } = M.vmDisksToForm(VM);
const patch = M.formDisksToPatch([], passthrough, VM);   // rootdisk removed
const vct = JSON.parse(patch.metadata.annotations['harvesterhci.io/volumeClaimTemplates']);
console.log(JSON.stringify({ vct: vct.length,
  vols: patch.spec.template.spec.volumes.map(v => v.name) }));""")
    d = json.loads(out)
    assert d["vct"] == 0            # orphaned template dropped
    assert d["vols"] == ["cloudinitdisk"]


def test_mapper_validation_errors():
    out = _run_node("""
const { items, passthrough } = M.vmDisksToForm(VM);
const errs = [];
const tryp = (its) => { try { M.formDisksToPatch(its, passthrough, VM); }
                        catch (e) { errs.push(e.message); } };
tryp([{ name: 'BAD_NAME', device: 'disk', source: 'pvc', pvc: 'default/x' }]);
tryp([...items, { ...items[0] }]);                       // duplicate name
tryp([{ name: 'ok', device: 'disk', source: 'pvc', pvc: 'otherns/x' }]);
tryp([{ name: 'ok', device: 'disk', source: 'blank' }]); // no size
try { M.formNetsToPatch([{ name: 'n1', type: 'bridge', network: '' }],
                        { interfaces: [], networks: [] }, VM); }
catch (e) { errs.push(e.message); }
try { M.formNetsToPatch([{ name: 'n1', type: 'bridge', network: 'default/production',
                           mac: 'nope' }], { interfaces: [], networks: [] }, VM); }
catch (e) { errs.push(e.message); }
console.log(JSON.stringify(errs.length));""")
    assert json.loads(out) == 6


def test_nets_round_trip_and_masquerade():
    out = _run_node("""
const n = M.vmNetsToForm(VM);
const same = M.formNetsToPatch(n.items, n.passthrough, VM).spec.template.spec;
const masq = M.formNetsToPatch([{ name: 'nat0', type: 'masquerade', model: 'virtio' }],
                               { interfaces: [], networks: [] }, VM).spec.template.spec;
console.log(JSON.stringify({
  rt: eq(sortByName(same.networks), sortByName(VM.spec.template.spec.networks))
      && eq(sortByName(same.domain.devices.interfaces),
            sortByName(VM.spec.template.spec.domain.devices.interfaces)),
  masqNet: masq.networks[0], masqItf: masq.domain.devices.interfaces[0],
}));""")
    d = json.loads(out)
    assert d["rt"] is True
    assert d["masqNet"] == {"name": "nat0", "pod": {}}
    assert d["masqItf"] == {"name": "nat0", "model": "virtio", "masquerade": {}}


# ---------------------------------------------------------------------------
# 2. Backend
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    wapp.app.config["TESTING"] = True
    return wapp.app.test_client()


def test_pvcs_endpoint_unknown_cluster_is_502(client, monkeypatch):
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: None)
    # list-endpoint convention: 502 with an error message, never a 500
    r = client.get("/api/pvcs/nope")
    assert r.status_code == 502
    assert "unknown" in r.get_json()["error"].lower()


def test_reduce_pvc_shape():
    item = {"metadata": {"name": "p1", "namespace": "ns",
                         "annotations": {"harvesterhci.io/owned-by": "[]"}},
            "spec": {"storageClassName": "sc1", "volumeMode": "Block",
                     "resources": {"requests": {"storage": "5Gi"}}},
            "status": {"phase": "Bound", "capacity": {"storage": "5Gi"}}}
    d = wapp._reduce_pvc(item)
    assert d == {"name": "p1", "namespace": "ns", "capacity": "5Gi",
                 "storage_class": "sc1", "phase": "Bound",
                 "volume_mode": "Block", "owned_by": "[]"}


class _FakeRun:
    def __init__(self, returncode, stdout=b"{}", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_patch_keeps_annotations_strips_identity(client, monkeypatch):
    """The v1.6.x 'safety' ternary popped the whole metadata dict — the
    disk editor's volumeClaimTemplates annotation (and the General tab's
    description) must survive; identity fields must not."""
    seen = {}

    def fake_check_output(cmd, **kw):
        seen["patch"] = json.loads(cmd[cmd.index("-p") + 1])
        return b"{}"
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/dev/null")
    monkeypatch.setattr(wapp.subprocess, "check_output", fake_check_output)
    r = client.patch("/api/vm/c1/ns1/vm1", json={"patch": {
        "metadata": {"name": "evil", "uid": "x", "resourceVersion": "1",
                     "annotations": {"harvesterhci.io/volumeClaimTemplates": "[]"}},
        "spec": {"runStrategy": "Halted"},
    }, "dry_run": True})
    assert r.status_code == 200
    md = seen["patch"]["metadata"]
    assert md == {"annotations": {"harvesterhci.io/volumeClaimTemplates": "[]"}}


def test_patch_tracked_as_action(client, monkeypatch):
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/dev/null")
    monkeypatch.setattr(wapp.subprocess, "check_output", lambda *a, **kw: b"{}")
    before = set()
    with wapp.ACTIONS_LOCK:
        before = set(wapp.ACTIONS)
    r = client.patch("/api/vm/c1/ns1/vm1", json={"patch": {"spec": {}}})
    assert r.status_code == 200
    with wapp.ACTIONS_LOCK:
        new = [wapp.ACTIONS[k] for k in set(wapp.ACTIONS) - before]
    assert any(run.action == "vm-edit:ns1/vm1" and run.status == "done"
               for run in new)
    with wapp.ACTIONS_LOCK:
        for k in set(wapp.ACTIONS) - before:
            wapp.ACTIONS.pop(k, None)


def test_patch_dry_run_not_tracked(client, monkeypatch):
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/dev/null")
    monkeypatch.setattr(wapp.subprocess, "check_output", lambda *a, **kw: b"{}")
    with wapp.ACTIONS_LOCK:
        before = set(wapp.ACTIONS)
    client.patch("/api/vm/c1/ns1/vm1", json={"patch": {"spec": {}}, "dry_run": True})
    with wapp.ACTIONS_LOCK:
        assert not any(wapp.ACTIONS[k].action.startswith("vm-edit:")
                       for k in set(wapp.ACTIONS) - before)


# ---------------------------------------------------------------------------
# 3. Source-level wiring
# ---------------------------------------------------------------------------

def test_no_fake_tf_schema_kind():
    """The editors pass schema OBJECTS to TFForm — registering kinds in
    TF_SCHEMA would break test_tf_schema.py's backend-branch contract."""
    tf_schema = (WEB / "static" / "js" / "tf-schema.js").read_text()
    assert "vm-disks" not in tf_schema and "vm-networks" not in tf_schema
    assert "TFForm.render(DISK_SCHEMA" in JS
    assert "TFForm.render(NET_SCHEMA" in JS


def test_advanced_json_fallback_kept():
    assert 'data-yaml="disks"' in JS and 'data-yaml="network"' in JS
    assert "vm-edit-adv" in JS


def test_refresh_registry_replaces_dom_lookup():
    """v1.6.x looked _refreshVM up on the DOM node — Reset and post-Apply
    refresh were dead. The module registry is the fix."""
    assert "refreshers" in JS
    assert "._refreshVM" not in JS


def test_vm_edit_i18n_keys_en_fr():
    i18n = (WEB / "static" / "js" / "i18n.js").read_text()
    for key in ("vm.edit.disksTitle", "vm.edit.netTitle", "vm.edit.advanced",
                "vm.edit.restartHint", "vm.edit.errPvcNs", "vm.edit.errMac"):
        assert i18n.count(f"'{key}'") >= 2, f"{key} must exist in EN and FR"


# ---------------------------------------------------------------------------
# 4. v1.8.1 — cloud-init assistant (one-way YAML generators)
# ---------------------------------------------------------------------------

def _gen(script):
    return _run_node("const G = window.VMEdit._mappers;\n" + script)


def test_ci_userdata_generator_yaml_is_valid():
    import yaml as _yaml
    out = _gen("""
console.log(G.genUserData({ hostname: 'vm1', timezone: 'Europe/Paris',
  package_update: true, packages: 'htop\\nvim',
  runcmd: 'echo "a: b" > /tmp/x',
  user: [{ name: 'ju', password: "p'wd: x", sudo: true,
           ssh_key: 'ssh-ed25519 AAA ju@n1', ssh_key_extra: 'ssh-rsa BBB c@d' }] }));""")
    d = _yaml.safe_load(out)
    assert out.startswith("#cloud-config")
    assert d["hostname"] == "vm1" and d["package_update"] is True
    u = d["users"][0]
    assert u["name"] == "ju" and u["sudo"] == "ALL=(ALL) NOPASSWD:ALL"
    assert u["plain_text_passwd"] == "p'wd: x" and u["lock_passwd"] is False
    assert u["ssh_authorized_keys"] == ["ssh-ed25519 AAA ju@n1", "ssh-rsa BBB c@d"]
    assert d["packages"] == ["htop", "vim"]
    assert d["runcmd"] == ['echo "a: b" > /tmp/x']


def test_ci_userdata_minimal_is_just_header():
    out = _gen("console.log(JSON.stringify(G.genUserData({})));")
    assert json.loads(out) == "#cloud-config\n"


def test_ci_networkdata_static_and_dhcp():
    import yaml as _yaml
    out = _gen("""
console.log(JSON.stringify({
  s: G.genNetworkData({ dns: '1.1.1.1, 9.9.9.9',
      nic: [{ iface: 'ens3', mode: 'static',
              address: '10.0.0.5/24', gateway: '10.0.0.1' }] }),
  d: G.genNetworkData({}),
}));""")
    both = json.loads(out)
    s = _yaml.safe_load(both["s"])
    assert s["version"] == 1
    sub = s["config"][0]["subnets"][0]
    assert s["config"][0]["name"] == "ens3"
    assert sub == {"type": "static", "address": "10.0.0.5/24",
                   "gateway": "10.0.0.1"}
    ns = [c for c in s["config"] if c["type"] == "nameserver"][0]
    assert ns["address"] == ["1.1.1.1", "9.9.9.9"]
    d = _yaml.safe_load(both["d"])
    assert d["config"][0]["subnets"][0] == {"type": "dhcp"}


def test_ci_networkdata_static_requires_address():
    out = _gen("""
try { G.genNetworkData({ nic: [{ iface: 'eth0', mode: 'static' }] }); console.log('NOERR'); }
catch (e) { console.log('ERR'); }""")
    assert out == "ERR"


def test_ci_wizard_wired_in_section():
    assert "vm-edit-ci-wizard" in JS
    assert 'data-action="gen-userdata"' in JS and 'data-action="gen-netdata"' in JS
    # generating over existing content must ask first
    assert "vm.edit.ci.confirmReplace" in JS
    # the Harvester SSH keys feed the user form (existing endpoint)
    assert "'/api/sshkeys'" in JS


def test_ci_i18n_keys_en_fr():
    i18n = (WEB / "static" / "js" / "i18n.js").read_text()
    for key in ("vm.edit.ci.wizard", "vm.edit.ci.genUser", "vm.edit.ci.genNet",
                "vm.edit.ci.confirmReplace", "vm.edit.ci.errAddr"):
        assert i18n.count(f"'{key}'") >= 2, f"{key} must exist in EN and FR"


def test_ci_userdata_full_module_coverage():
    """v1.8.2 — the assistant covers the requested breadth: identity,
    SSH access policy, users with groups/shell, packages with
    reboot-if-required, growpart, extra-disk format+mount, write_files
    (literal blocks), NTP, trusted CAs (PEM), bootcmd/runcmd."""
    import yaml as _yaml
    out = _gen("""
console.log(G.genUserData({
  hostname: 'vm1', fqdn: 'vm1.home.lo', locale: 'fr_FR.UTF-8', keyboard: 'fr',
  ssh_pwauth: true, disable_root: false, expire_passwords: true,
  package_update: true, package_reboot: true, packages: 'htop',
  growpart: false, ntp_servers: '172.16.3.6',
  ca_certs: '-----BEGIN CERTIFICATE-----\\nMIIabc\\n-----END CERTIFICATE-----',
  bootcmd: 'echo early', runcmd: 'echo done',
  user: [{ name: 'ju', sudo: true, groups: 'wheel, docker', shell: '/usr/bin/zsh' }],
  fs: [{ device: '/dev/vdb', filesystem: 'xfs', mount_point: '/data' }],
  file: [{ path: '/etc/motd', permissions: '0755', content: 'a: b\\nline2' }] }));""")
    d = _yaml.safe_load(out)
    assert d["fqdn"] == "vm1.home.lo" and d["manage_etc_hosts"] is True
    assert d["keyboard"] == {"layout": "fr"}
    assert d["ssh_pwauth"] is True and d["disable_root"] is False
    assert d["chpasswd"] == {"expire": True}
    assert d["package_reboot_if_required"] is True
    assert d["growpart"]["mode"] in ("off", False)   # both disable growpart
    u = d["users"][0]
    assert u["groups"] == "wheel, docker" and u["shell"] == "/usr/bin/zsh"
    assert d["fs_setup"] == [{"device": "/dev/vdb", "filesystem": "xfs",
                              "overwrite": False}]
    assert d["mounts"] == [["/dev/vdb", "/data", "xfs", "defaults", "0", "2"]]
    wf = d["write_files"][0]
    assert wf["permissions"] == "0755"               # string, not octal int
    assert wf["content"] == "a: b\nline2\n"   # literal block keeps one final newline
    assert d["ntp"] == {"enabled": True, "servers": ["172.16.3.6"]}
    assert "BEGIN CERTIFICATE" in d["ca_certs"]["trusted"][0]
    assert d["bootcmd"] == ["echo early"] and d["runcmd"] == ["echo done"]


def test_ci_networkdata_multi_nic_and_nameserver():
    import yaml as _yaml
    out = _gen("""
console.log(G.genNetworkData({ dns: '172.16.3.6, 1.1.1.1', search: 'home.lo',
  nic: [{ iface: 'eth0', mode: 'static', address: '10.0.0.5/24',
          gateway: '10.0.0.1', mtu: 9000 },
        { iface: 'eth1', mode: 'dhcp' }] }));""")
    d = _yaml.safe_load(out)
    phys = [c for c in d["config"] if c["type"] == "physical"]
    assert len(phys) == 2 and phys[0]["mtu"] == 9000
    ns = [c for c in d["config"] if c["type"] == "nameserver"][0]
    assert ns["address"] == ["172.16.3.6", "1.1.1.1"] and ns["search"] == ["home.lo"]
