"""2.3.0 : déclarations Terraform qui visent un hôte KVM (décisions 107 et 108).

Une seule liste de déclarations ; chacune choisit sa cible, cluster Harvester
ou hôte KVM déclaré dans la console. Les routes, le runner (tofu simulé), le
magasin, l'embarquement du provider libvirt et sa vérification (signature
GPG + somme) sont essayés ici ; l'essai réel est fait sur node2 (objets
kvmtest-* seulement).
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402
import tf_kvm as k  # noqa: E402
import tf_state as st  # noqa: E402
import tf_store as ts  # noqa: E402

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJdD ops@site"
BASE = "/var/lib/libvirt/images/kvmtest-pool/base.qcow2"


def kvm_vm(rid, name="kvmtest-tf1", **over):
    s = {"name": name, "pool": "kvmtest-pool", "base_image": BASE, "network": "kvmtest-net"}
    s.update(over)
    return {"id": rid, "kind": "kvm_vm", "spec": s}


def rke2(rid):
    return {"id": rid, "kind": "kvm_rke2", "spec": {
        "name": "lab", "servers": 1, "agents": 1, "pool": "kvmtest-pool", "base_image": BASE,
        "network": "kvmtest-net", "address_start": "10.213.77.50", "gateway": "10.213.77.1", "ssh_key": KEY}}


def declare_host(state, name="tfhost", uri="qemu:///system"):
    (state / "kvm.d").mkdir(parents=True, exist_ok=True)
    (state / "ssh").mkdir(parents=True, exist_ok=True)
    (state / "kvm.d" / f"{name}.yaml").write_text(json.dumps({
        "name": name, "address": "192.0.2.10", "port": 22, "user": "ops", "uri": uri,
        "ssh_key": f"ssh/{name}_id", "known_hosts": f"ssh/{name}_known", "labels": {}}))
    (state / "ssh" / f"{name}_id").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n")
    (state / "ssh" / f"{name}_known").write_text("192.0.2.10 ssh-ed25519 AAAAC3fake\n")


# ---------------------------------------------------------------------------
# Magasin
# ---------------------------------------------------------------------------
@pytest.fixture
def store(tmp_path):
    return ts.DeclStore(tmp_path / "tf.db")


def test_an_old_database_gains_the_target_column(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE tf_declarations (id TEXT PRIMARY KEY, cluster TEXT NOT NULL, name TEXT NOT NULL, "
              "description TEXT NOT NULL DEFAULT '', resources TEXT NOT NULL DEFAULT '[]', revision INTEGER "
              "NOT NULL DEFAULT 1, created_at TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT '', updated_at "
              "TEXT NOT NULL, updated_by TEXT NOT NULL DEFAULT '', last_applied_at TEXT, last_applied_status "
              "TEXT, last_applied_by TEXT)")
    c.execute("INSERT INTO tf_declarations (id, cluster, name, created_at, updated_at) "
              "VALUES ('aaaaaaaaaaaa', 'harv1', 'web', 'x', 'x')")
    c.commit()
    c.close()
    s = ts.DeclStore(db)
    assert s.get("aaaaaaaaaaaa")["target_kind"] == "harvester"


@pytest.mark.parametrize("target, kind, ok", [
    ("harvester", "vm", True), ("harvester", "raw", True), ("harvester", "kvm_vm", False),
    ("kvm", "kvm_vm", True), ("kvm", "kvm_rke2", True), ("kvm", "raw", False), ("kvm", "vm", False),
    ("kvm", "ssh_key", False),
])
def test_resources_go_to_their_family_only(store, target, kind, ok):
    res = [{"id": "0" * 12, "kind": kind, "spec": {}}]
    if ok:
        assert store.create("a" * 12, "h", "d", resources=res, target_kind=target)["target_kind"] == target
    else:
        with pytest.raises(ts.StoreError, match="does not go to"):
            store.create("a" * 12, "h", "d", resources=res, target_kind=target)


def test_names_are_unique_per_target_not_across_families(store):
    store.create("a" * 12, "node2", "web", target_kind="kvm")
    store.create("b" * 12, "node2", "web", target_kind="harvester")       # même nom, autre famille
    with pytest.raises(ts.StoreError) as e:
        store.create("c" * 12, "node2", "WEB", target_kind="kvm")
    assert e.value.code == "name-taken"
    assert [d["id"] for d in store.list("node2")] == ["b" * 12]            # défaut : Harvester
    assert [d["id"] for d in store.list("node2", "kvm")] == ["a" * 12]
    assert len(store.list(target_kind="kvm")) == 1 and len(store.list()) == 2
    with pytest.raises(ts.StoreError) as e:
        store.update("a" * 12, 1, resources=[{"id": "1" * 12, "kind": "vm", "spec": {}}])
    assert e.value.code == "invalid-resources"
    with pytest.raises(ts.StoreError) as e:
        store.create("d" * 12, "x", "y", target_kind="vmware")
    assert e.value.code == "invalid-target"


# ---------------------------------------------------------------------------
# Rendu côté console
# ---------------------------------------------------------------------------
def test_render_dispatch_and_address_collisions():
    rendered, errors = wapp._tf_render_resources([kvm_vm("0" * 12), rke2("1" * 12)], "kvm")
    assert not errors and [r[4] for r in rendered] == ["libvirt_domain.kvmtest_tf1", "libvirt_domain.lab"]
    # une VM « a » produit libvirt_volume.a_disk : un volume nommé a_disk la heurte
    clash = [kvm_vm("0" * 12, name="a"),
             {"id": "1" * 12, "kind": "kvm_volume", "spec": {"name": "a_disk", "pool": "p"}}]
    _, errors = wapp._tf_render_resources(clash, "kvm")
    assert errors and "already produced by resource #1" in errors[0]["error"]
    _, errors = wapp._tf_render_resources([kvm_vm("0" * 12)], "harvester")
    assert "does not go to a harvester target" in errors[0]["error"]
    _, errors = wapp._tf_render_resources([kvm_vm("0" * 12, name="bad name")], "kvm")
    assert "invalid name" in errors[0]["error"]
    rendered, _ = wapp._tf_render_resources([kvm_vm("0" * 12, name="9lives")], "kvm")
    assert rendered[0][0] == "r_9lives"                    # une adresse Terraform commence par une lettre


# ---------------------------------------------------------------------------
# Routes (serveur de test)
# ---------------------------------------------------------------------------
def test_kvm_declaration_routes(api, test_config):
    state = test_config["root"] / "state"
    declare_host(state)
    status, d = api("POST", "/api/tf-declarations", expect_status=None, json_body={
        "cluster": "tfhost", "target_kind": "kvm", "name": "lab", "resources": [kvm_vm("0" * 12, ssh_key=KEY)]})
    assert status == 201, d
    assert d["target_kind"] == "kvm" and d["resources"][0]["address"] == "libvirt_domain.kvmtest_tf1"
    assert d["resources"][0]["addresses"][-1] == "libvirt_volume.kvmtest_tf1_seed"
    status, lst = api("GET", "/api/tf-declarations?family=kvm")
    assert [x["id"] for x in lst["declarations"]] == [d["id"]]
    assert d["id"] not in [x["id"] for x in api("GET", "/api/tf-declarations?family=harvester")[1]["declarations"]]
    status, code = api("GET", f"/api/tf-declarations/{d['id']}/code")
    assert 'provider "libvirt"' in code["files"][0]["content"]
    assert "qemu+sshcmd://ops@192.0.2.10/system" in code["files"][0]["content"]
    blob = json.dumps(code)
    assert "tfhost_id" not in blob and str(state) not in blob and "PRIVATE KEY" not in blob
    # sans provider libvirt, refus clair (le service de test n'en a pas)
    status, out = api("POST", f"/api/terraform/kvm/tfhost/apply_declaration", expect_status=None,
                      json_body={"declaration": {"id": d["id"]}, "dry_run": True})
    assert status == 503 and "libvirt Terraform provider" in out["error"]
    api("DELETE", f"/api/tf-declarations/{d['id']}")


@pytest.mark.parametrize("path, body, want", [
    ("/api/tf-declarations", {"cluster": "nohost", "target_kind": "kvm", "name": "x"}, 404),
    ("/api/tf-declarations", {"cluster": "../etc", "target_kind": "kvm", "name": "x"}, 400),
    ("/api/tf-declarations", {"cluster": "tfhost", "target_kind": "vmware", "name": "x"}, 400),
    ("/api/tf-declarations", {"cluster": "tfcustom", "target_kind": "kvm", "name": "x"}, 400),
    ("/api/terraform/kvm/tfhost/apply_declaration", {"declaration": {}}, 400),
    ("/api/terraform/kvm/tfhost/apply_declaration", {"declaration": {"id": "bad"}}, 400),
    ("/api/terraform/kvm/nohost/destroy_declaration", {"declaration": {"id": "a" * 12}}, 404),
    ("/api/terraform/kvm/tfhost/destroy_declaration", {"declaration": {"id": "a" * 12}}, 404),
    ("/api/tf-declarations?family=vmware", None, 400),
])
def test_kvm_route_error_paths(api, test_config, path, body, want):
    state = test_config["root"] / "state"
    declare_host(state)
    declare_host(state, "tfcustom", uri="qemu+tls://elsewhere/system")
    method = "GET" if body is None else "POST"
    status, out = api(method, path, expect_status=None, json_body=body)
    assert status == want, out


def test_a_harvester_route_refuses_a_kvm_declaration(api, test_config):
    declare_host(test_config["root"] / "state")
    _, d = api("POST", "/api/tf-declarations", json_body={
        "cluster": "tfhost", "target_kind": "kvm", "name": "cross", "resources": [kvm_vm("0" * 12)]},
        expect_status=201)
    status, out = api("POST", "/api/terraform/harv-fake/apply_declaration", expect_status=None,
                      json_body={"declaration": {"id": d["id"]}, "dry_run": True})
    assert status == 400 and "targets a kvm host" in out["error"]
    api("DELETE", f"/api/tf-declarations/{d['id']}")


def test_kvm_runs_need_the_operator_role():
    assert wapp.required_role_for("/api/terraform/kvm/node2/apply_declaration", "POST") == "operator"
    assert wapp.required_role_for("/api/terraform/kvm/node2/destroy_declaration", "POST") == "operator"
    assert wapp.required_role_for("/api/tf-declarations", "GET") == "viewer"


# ---------------------------------------------------------------------------
# Runner (dans le processus, tofu simulé)
# ---------------------------------------------------------------------------
class FakeTofu:
    """Rejoue init / plan / show / apply / output / destroy ; note ce que
    voit chaque appel (fichiers posés, environnement)."""

    def __init__(self, rc=0, err=""):
        self.calls, self.rc, self.err, self.seen = [], rc, err, []

    def __call__(self, ws, kc, args, timeout=180, extra_env=None, drop_env=()):
        ws = Path(ws)
        self.calls.append(args[0])
        self.seen.append({"files": sorted(p.name for p in ws.iterdir()), "env": dict(extra_env or {}),
                          "drop": tuple(drop_env), "kc": kc})
        if args[0] == "show":
            return 0, json.dumps({"resource_changes": []}), ""
        if args[0] == "output":
            return 0, json.dumps({"lab_nodes": {"sensitive": False, "value": {"lab-server-0": "10.213.77.50"}},
                                  "secret": {"sensitive": True, "value": "hidden"},
                                  "stray": {"sensitive": False, "value": "not a template output"}}), ""
        if args[0] == "plan" and "-out=tfplan" in args:
            (ws / "tfplan").write_text("plan")
        if args[0] == "init":
            (ws / ".terraform").mkdir(exist_ok=True)
        return self.rc, f"{args[0]} ok", self.err + f" {ws}/kvm_ssh"


@pytest.fixture
def kvm_world(tmp_path, monkeypatch):
    state = tmp_path / "state"
    declare_host(state)
    store = ts.DeclStore(tmp_path / "tf.db")
    monkeypatch.setattr(wapp, "TF_DECLS", store)
    monkeypatch.setattr(wapp, "TF_WORKSPACES", tmp_path / "ws")
    monkeypatch.setattr(wapp, "_state_dir", lambda: state)
    monkeypatch.setattr(wapp, "_tf_plugin_cache_init_libvirt", lambda ws: "0.9.9")
    monkeypatch.setattr(wapp, "_tf_kvm_preflight", lambda ws, host, env: None)
    monkeypatch.setattr(wapp, "_tf_kvm_check_bases", lambda host, rendered: None)
    return store


def run_kvm(store, resources, mode, fake, monkeypatch, plan_hash=None):
    monkeypatch.setattr(wapp, "_tf_run_cmd", fake)
    d = store.get("a" * 12) or store.create("a" * 12, "tfhost", "lab", resources=resources, target_kind="kvm")
    rendered, errors = wapp._tf_render_resources(d["resources"], "kvm")
    assert not errors, errors
    run = wapp.ActionRun("k" + mode, "x", "tfhost", [])
    h = wapp._tf_decl_hash(rendered)
    wapp.TF_KVM_RUNNING.add(d["id"])
    wapp._tf_decl_runner(run, "tfhost", None, d, rendered, mode, h, bool(plan_hash), "ju")
    return run, d, wapp._tf_decl_ws(d, create=False)


def test_a_plan_stages_the_host_key_only_for_the_run(kvm_world, monkeypatch):
    fake = FakeTofu()
    run, d, ws = run_kvm(kvm_world, [rke2("1" * 12)], "plan", fake, monkeypatch)
    assert run.status == "done", run.error_summary
    during = fake.seen[0]
    assert {"kvm_ssh", "kvm_ssh_key", "kvm_known_hosts", "_providers.tf"} <= set(during["files"])
    assert during["drop"] == ("SSH_AUTH_SOCK",) and during["kc"] == ""
    var = "TF_VAR_" + k.rke2_token_var("lab")
    token = during["env"][var]
    assert len(token) == 64 and during["env"]["TMPDIR"] == str(ws / ".tmp")
    assert not any(p.name.startswith("kvm_ssh") or p.name == "kvm_known_hosts" for p in ws.iterdir())
    assert stat.S_IMODE(ws.stat().st_mode) == 0o700
    assert stat.S_IMODE((ws / ".secrets" / k.rke2_token_var("lab")).stat().st_mode) == 0o600
    assert token not in json.dumps(run.result) and token not in json.dumps(list(run.events))
    assert d["id"] not in wapp.TF_KVM_RUNNING
    # le jeton est tiré une fois, puis relu
    fake2 = FakeTofu()
    run_kvm(kvm_world, None, "plan", fake2, monkeypatch)
    assert fake2.seen[-1]["env"][var] == token


def test_a_failed_run_still_removes_the_key_and_hides_paths(kvm_world, monkeypatch):
    fake = FakeTofu(rc=1, err="Error: failed to connect")
    run, d, ws = run_kvm(kvm_world, [kvm_vm("0" * 12)], "plan", fake, monkeypatch)
    assert run.status == "error"
    assert "<workspace>/kvm_ssh" in run.error_summary and str(ws) not in run.error_summary
    assert not (ws / "kvm_ssh_key").exists() and not (ws / "kvm_ssh").exists()
    assert d["id"] not in wapp.TF_KVM_RUNNING


def test_an_exception_does_not_leave_the_key_behind(kvm_world, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("boom")
    run, d, ws = run_kvm(kvm_world, [kvm_vm("0" * 12)], "plan", boom, monkeypatch)
    assert run.status == "error" and "boom" in run.error_summary
    assert not (ws / "kvm_ssh_key").exists()


def test_the_preflight_refusal_stops_before_tofu(kvm_world, monkeypatch):
    monkeypatch.setattr(wapp, "_tf_kvm_preflight", lambda ws, host, env: "tfhost: hostkey: Host key verification failed.")
    fake = FakeTofu()
    run, d, ws = run_kvm(kvm_world, [kvm_vm("0" * 12)], "plan", fake, monkeypatch)
    assert run.status == "error" and "hostkey" in run.error_summary and fake.calls == []
    assert not (ws / "kvm_known_hosts").exists()


def test_apply_keeps_only_non_sensitive_outputs_and_destroy_removes_them(kvm_world, monkeypatch):
    fake = FakeTofu()
    run, d, ws = run_kvm(kvm_world, [rke2("1" * 12)], "plan", fake, monkeypatch)
    run, d, ws = run_kvm(kvm_world, None, "apply", FakeTofu(), monkeypatch, plan_hash=run.result["plan_hash"])
    assert run.status == "done", run.error_summary
    out = json.loads((ws / ".outputs.json").read_text())
    assert out == {"lab_nodes": {"lab-server-0": "10.213.77.50"}}          # ni sensible, ni inconnue
    assert stat.S_IMODE((ws / ".outputs.json").stat().st_mode) == 0o600
    assert wapp._tf_decl_view(kvm_world.get(d["id"]))["outputs"] == out
    run_kvm(kvm_world, None, "plan", FakeTofu(), monkeypatch)              # un plan ne les efface pas
    assert (ws / ".outputs.json").exists()
    run, d, ws = run_kvm(kvm_world, None, "destroy", FakeTofu(), monkeypatch)
    assert run.status == "done" and not (ws / ".outputs.json").exists()


def test_preflight_classifies_a_host_key_refusal(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / k.WRAPPER_FILE).write_text("#!/bin/sh\necho 'Host key verification failed.' >&2\nexit 255\n")
    os.chmod(ws / k.WRAPPER_FILE, 0o700)
    why = wapp._tf_kvm_preflight(ws, {"name": "tfhost", "address": "192.0.2.10", "port": 22, "user": "ops"}, {})
    assert why.startswith("tfhost: hostkey:") and str(ws) not in why
    (ws / k.WRAPPER_FILE).write_text("#!/bin/sh\nexit 0\n")
    assert wapp._tf_kvm_preflight(ws, {"name": "tfhost", "address": "192.0.2.10", "port": 22, "user": "ops"}, {}) is None


def test_the_rc_pins_every_mirrored_provider_under_both_registries(tmp_path, monkeypatch):
    binp = tmp_path / "terraform-provider-libvirt"
    binp.write_text("#!/bin/sh\n")
    binp.chmod(0o755)
    ws = tmp_path / "ws"
    ws.mkdir()
    wapp._tf_mirror_provider(ws, "dmacvicar", "libvirt", "0.9.9", binp)
    rc = wapp._tf_write_rc(ws).read_text()
    for host in ("registry.terraform.io", "registry.opentofu.org"):
        assert (ws / "plugins" / host / "dmacvicar/libvirt/0.9.9" / f"linux_{wapp._tf_host_arch()}"
                / "terraform-provider-libvirt_v0.9.9").exists()
        assert rc.count(f'"{host}/dmacvicar/libvirt"') == 2           # inclus au miroir, exclu du direct
    assert wapp._tf_write_rc(tmp_path / "empty") is None


def test_the_libvirt_provider_is_found_by_its_card(tmp_path, monkeypatch):
    d = tmp_path / "lv"
    monkeypatch.setattr(wapp, "TF_LIBVIRT_DIR", d)
    assert wapp._tf_libvirt_binary() is None and wapp._tf_libvirt_version() == ""
    (d / "bin").mkdir(parents=True)
    (d / "bin" / "terraform-provider-libvirt").write_text("x")
    (d / "bin" / "terraform-provider-libvirt").chmod(0o755)
    (d / "provider.json").write_text(json.dumps({"version": "0.9.9"}))
    assert wapp._tf_libvirt_binary() and wapp._tf_libvirt_version() == "0.9.9"
    (d / "provider.json").write_text(json.dumps({"version": "0.9.9; rm -rf /"}))
    assert wapp._tf_libvirt_version() == ""


# ---------------------------------------------------------------------------
# Livrable : embarquement et vérification du provider
# ---------------------------------------------------------------------------
def test_the_package_embeds_and_pins_the_libvirt_provider():
    pins = (ROOT / "scripts" / "embedded-providers.env").read_text()
    assert "TF_LIBVIRT_PROVIDER_VERSION=0.9.9" in pins
    assert "TF_LIBVIRT_PROVIDER_FPR=F6D373750D6F5983E70634DC0833E38C51E74D26" in pins
    pkg = (ROOT / "package.sh").read_text()
    assert "scripts/fetch-libvirt-provider.sh" in pkg and "bundles/terraform-provider-libvirt" in pkg
    assert (ROOT / "config" / "terraform-provider-keys" / "dmacvicar-libvirt.asc").read_text().startswith(
        "-----BEGIN PGP PUBLIC KEY BLOCK-----")


def _install_bundles(script_dir, state_dir):
    src = (ROOT / "install.sh").read_text()
    fn = src[src.index("install_bundles() {"):].split("\n}\n", 1)[0] + "\n}\n"
    script = ("set -eo pipefail\nok(){ echo \"OK $*\"; }\nwarn(){ echo \"WARN $*\"; }\n"
              f"SCRIPT_DIR={script_dir}\nSTATE_DIR={state_dir}\n{fn}\ninstall_bundles\n")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_install_bundles_puts_the_libvirt_provider_where_the_console_looks(tmp_path):
    lv = tmp_path / "pkg" / "bundles" / "terraform-provider-libvirt"
    (lv / "bin").mkdir(parents=True)
    (lv / "bin" / "terraform-provider-libvirt").write_text("#!/bin/sh\n")
    (lv / "bin" / "terraform-provider-libvirt").chmod(0o755)
    (lv / "provider.json").write_text(json.dumps({"version": "0.9.9", "signer_fingerprint": "F6D3"}))
    state = tmp_path / "state"
    out = _install_bundles(tmp_path / "pkg", state)
    dest = state / ".local/share/harvester-ops/terraform-provider-libvirt"
    assert "libvirt Terraform provider 0.9.9 installed" in out
    assert os.access(dest / "bin" / "terraform-provider-libvirt", os.X_OK)
    assert json.loads((dest / "provider.json").read_text())["version"] == "0.9.9"


GPG = shutil.which("gpg")


@pytest.fixture(scope="module")
def release(tmp_path_factory):
    """Une fausse release signée par une clé jetable, servie en file://."""
    if not GPG:
        pytest.skip("gpg unavailable")
    d = tmp_path_factory.mktemp("rel")
    home = d / "gnupg"
    home.mkdir(mode=0o700)
    env = {**os.environ, "GNUPGHOME": str(home)}

    def gpg(*a, **kw):
        return subprocess.run([GPG, "--batch", "--quiet", *a], env=env, check=True, capture_output=True, **kw)

    keys = {}
    for who in ("good", "other"):
        gpg("--pinentry-mode", "loopback", "--passphrase", "", "--quick-gen-key", f"{who}@test", "ed25519", "sign", "never")
        out = gpg("--with-colons", "--list-keys", f"{who}@test").stdout.decode()
        keys[who] = [l.split(":")[9] for l in out.splitlines() if l.startswith("fpr")][0]
        (d / f"{who}.asc").write_bytes(gpg("--armor", "--export", f"{who}@test").stdout)
    (d / "both.asc").write_bytes((d / "good.asc").read_bytes() + (d / "other.asc").read_bytes())

    def build(name, signer="good", tamper_zip=False, tamper_sums=False):
        v = d / name / "v9.9.9"
        v.mkdir(parents=True)
        z = v / "terraform-provider-libvirt_9.9.9_linux_amd64.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("terraform-provider-libvirt_v9.9.9", b"\x7fELF" + b"\0" * 60)
        import hashlib
        sums = v / "terraform-provider-libvirt_9.9.9_SHA256SUMS"
        sums.write_text(f"{hashlib.sha256(z.read_bytes()).hexdigest()}  {z.name}\n")
        gpg("--local-user", f"{signer}@test", "--detach-sign", "-o", str(sums) + ".sig", str(sums))
        if tamper_zip:
            with zipfile.ZipFile(z, "a") as f:
                f.writestr("extra", b"x")
        if tamper_sums:
            sums.write_text(sums.read_text().replace("  ", "  ./"))
        return f"file://{d / name}"

    return {"dir": d, "keys": keys, "build": build}


@pytest.mark.parametrize("case, signer, keyfile, pin, tamper_zip, tamper_sums, want", [
    ("good", "good", "good.asc", "good", False, False, "verified"),
    ("wrong-pin", "good", "good.asc", "other", False, False, "only the pinned key"),
    ("two-keys", "other", "both.asc", "good", False, False, "only the pinned key"),
    ("other-signer", "other", "good.asc", "good", False, False, "bad signature"),
    ("zip-tampered", "good", "good.asc", "good", True, False, "checksum mismatch"),
    ("sums-tampered", "good", "good.asc", "good", False, True, "bad signature"),
])
def test_the_fetch_script_verifies_signature_then_checksum(release, tmp_path, case, signer, keyfile, pin,
                                                           tamper_zip, tamper_sums, want):
    base = release["build"](case, signer=signer, tamper_zip=tamper_zip, tamper_sums=tamper_sums)
    dest = tmp_path / "dest"
    r = subprocess.run([str(ROOT / "scripts" / "fetch-libvirt-provider.sh"), "9.9.9", release["keys"][pin],
                        str(release["dir"] / keyfile), str(dest), "test"],
                       capture_output=True, text=True, env={**os.environ, "LIBVIRT_PROVIDER_BASE_URL": base})
    if want == "verified":
        assert r.returncode == 0, r.stderr
        meta = json.loads((dest / "provider.json").read_text())
        assert meta["version"] == "9.9.9" and meta["signer_fingerprint"] == release["keys"]["good"]
        assert os.access(dest / "bin" / "terraform-provider-libvirt", os.X_OK)
    else:
        assert r.returncode != 0 and want in r.stderr, r.stderr
        assert not (dest / "bin" / "terraform-provider-libvirt").exists()


@pytest.mark.parametrize("args, want", [
    (["0.9", "F" * 40], "invalid version"),
    (["0.9.9", "abc"], "invalid pinned fingerprint"),
])
def test_the_fetch_script_refuses_bad_pins(tmp_path, args, want):
    r = subprocess.run([str(ROOT / "scripts" / "fetch-libvirt-provider.sh"), *args, "/nonexistent", str(tmp_path)],
                       capture_output=True, text=True)
    assert r.returncode != 0 and want in r.stderr


# ---------------------------------------------------------------------------
# Interface (source)
# ---------------------------------------------------------------------------
def test_the_view_routes_runs_by_target_and_filters_by_family():
    js = (ROOT / "web" / "static" / "js" / "tf-decl-view.js").read_text()
    assert "`/api/terraform/kvm/${enc(d.cluster)}`" in js
    assert "class=\"tfd-family tip\"" in js and "data-tip=\"${esc(tr('tfd.t.family'))}\"" in js
    assert "kvm: ['kvm_vm', 'kvm_volume', 'kvm_network', 'kvm_rke2'] };" in js
    assert "/api/kvm/hosts?declared=1" in js
    assert "${esc(targetText(d))}" in js                      # cible échappée
    form = (ROOT / "web" / "static" / "js" / "tf-form.js").read_text()
    assert "data-ref-url" in form and "replace('{target}', encodeURIComponent(currentCluster))" in form


def test_tf_run_cmd_drops_the_agent_and_passes_the_token_by_env(tmp_path, monkeypatch):
    seen = {}

    class R:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(argv, **kw):
        seen["argv"], seen["env"] = argv, kw["env"]
        return R()

    monkeypatch.setattr(wapp.subprocess, "run", fake_run)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/user/1000/agent.sock")
    wapp._tf_run_cmd(tmp_path, "", ["plan"], extra_env={"TF_VAR_lab_rke2_token": "s3cret"},
                     drop_env=("SSH_AUTH_SOCK",))
    assert "SSH_AUTH_SOCK" not in seen["env"] and seen["env"]["TF_VAR_lab_rke2_token"] == "s3cret"
    assert "s3cret" not in " ".join(seen["argv"])
    wapp._tf_run_cmd(tmp_path, "", ["plan"])
    assert seen["env"]["SSH_AUTH_SOCK"] == "/run/user/1000/agent.sock"     # Harvester : inchangé


# ---------------------------------------------------------------------------
# Relecture indépendante (2.3.0) : images de base, rôles, restes
# ---------------------------------------------------------------------------
CATALOGUE = {"images": [
    {"pool": "kvmtest-pool", "volume": "base.qcow2", "path": BASE, "format": "qcow2", "iso": False, "used_by": []},
    {"pool": "kvmtest-pool", "volume": "seed.iso", "path": "/var/lib/libvirt/images/kvmtest-pool/seed.iso",
     "format": "raw", "iso": True, "used_by": []},
    {"pool": "other", "volume": "base2.qcow2", "path": "/srv/other/base2.qcow2", "format": "qcow2", "iso": False,
     "used_by": []},
    {"pool": "kvmtest-pool", "volume": "live.qcow2", "path": "/var/lib/libvirt/images/kvmtest-pool/live.qcow2",
     "format": "qcow2", "iso": False, "used_by": ["prod-db"]},
]}


@pytest.mark.parametrize("base, fmt, want", [
    (BASE, "qcow2", None),
    ("/dev/sda", "raw", "not an image of the catalogue"),
    ("/var/lib/libvirt/images/kvmtest-pool/seed.iso", "raw", "not an image of the catalogue"),
    ("/srv/other/base2.qcow2", "qcow2", "must be in the pool of the new disk"),
    ("/var/lib/libvirt/images/kvmtest-pool/live.qcow2", "qcow2", "used as a disk by prod-db"),
    (BASE, "raw", "its format is qcow2, not raw"),
])
def test_base_images_must_come_from_the_host_catalogue(monkeypatch, tmp_path, base, fmt, want):
    monkeypatch.setattr(wapp, "_state_dir", lambda: tmp_path)
    monkeypatch.setattr(wapp._kh, "VirshHost", lambda decl, state, timeout=0: type("H", (), {"decl": decl})())
    monkeypatch.setattr(wapp._ks, "list_images", lambda vh, decl, notes, fresh=False: CATALOGUE)
    rendered, errors = wapp._tf_render_resources([kvm_vm("0" * 12, base_image=base, base_format=fmt)], "kvm")
    assert not errors
    why = wapp._tf_kvm_check_bases({"name": "tfhost"}, rendered)
    assert (why is None) if want is None else (want in why), why


def test_a_vm_without_base_image_reads_no_catalogue(monkeypatch):
    monkeypatch.setattr(wapp._ks, "list_images", lambda *a, **k: (_ for _ in ()).throw(AssertionError("read")))
    rendered, _ = wapp._tf_render_resources([kvm_vm("0" * 12, base_image="")], "kvm")
    assert wapp._tf_kvm_check_bases({"name": "tfhost"}, rendered) is None


def test_the_runner_stops_on_a_refused_base_image(kvm_world, monkeypatch):
    monkeypatch.setattr(wapp, "_tf_kvm_check_bases", lambda host, rendered: "base image /dev/sda: not an image")
    fake = FakeTofu()
    run, d, ws = run_kvm(kvm_world, [kvm_vm("0" * 12)], "plan", fake, monkeypatch)
    assert run.status == "error" and "not an image" in run.error_summary and fake.calls == []
    assert not (ws / "kvm_ssh_key").exists()


@pytest.mark.parametrize("resources, mode, counts, want", [
    ([kvm_vm("0" * 12)], "plan", None, "operator"),
    ([kvm_vm("0" * 12)], "apply", {"create": 3}, "operator"),
    ([kvm_vm("0" * 12)], "apply", {"update": 1}, "operator"),
    ([kvm_vm("0" * 12)], "apply", {"replace": 1}, "admin"),
    ([kvm_vm("0" * 12)], "apply", {"delete": 1}, "admin"),
    ([kvm_vm("0" * 12)], "destroy-plan", None, "operator"),
    ([kvm_vm("0" * 12)], "destroy", None, "admin"),
    ([{"id": "1" * 12, "kind": "kvm_network", "spec": {"name": "n", "mode": "isolated"}}], "apply", {"create": 1}, "admin"),
    ([{"id": "1" * 12, "kind": "kvm_network", "spec": {"name": "n", "mode": "isolated"}}], "plan", None, "operator"),
])
def test_kvm_roles_follow_the_kvm_module(tmp_path, monkeypatch, resources, mode, counts, want):
    monkeypatch.setattr(wapp, "TF_WORKSPACES", tmp_path / "ws")
    decl = {"id": "a" * 12, "cluster": "tfhost", "target_kind": "kvm", "resources": resources}
    ws = wapp._tf_decl_ws(decl)
    if counts is not None:
        (ws / "tfplan.meta").write_text(json.dumps({"summary": {"counts": counts}}))
    assert wapp._tf_kvm_needed_role(decl, mode) == want


def test_a_kvm_apply_needs_a_reviewed_plan(api, test_config):
    declare_host(test_config["root"] / "state")
    _, d = api("POST", "/api/tf-declarations", expect_status=201, json_body={
        "cluster": "tfhost", "target_kind": "kvm", "name": "noplan", "resources": [kvm_vm("0" * 12)]})
    status, out = api("POST", "/api/terraform/kvm/tfhost/apply_declaration", expect_status=None,
                      json_body={"declaration": {"id": d["id"]}, "dry_run": False})
    assert status == 400 and "reviewed plan" in out["error"]
    api("DELETE", f"/api/tf-declarations/{d['id']}")


def test_viewers_do_not_read_cloud_init(monkeypatch):
    view = {"resources": [{"id": "0" * 12, "kind": "kvm_vm", "spec": {"name": "a", "user_data": "#cloud-config\npassword: x"}},
                          {"id": "1" * 12, "kind": "vm", "spec": {"cloudinit": [{"type": "noCloud", "user_data": "pw"}]}}]}
    monkeypatch.setattr(wapp, "current_role", lambda: "viewer")
    out = wapp._tf_decl_for_role(view)
    assert out["resources"][0]["spec"]["user_data"] == "(hidden)"
    assert out["resources"][1]["spec"]["cloudinit"][0]["user_data"] == "(hidden)"
    assert view["resources"][0]["spec"]["user_data"].startswith("#cloud")        # l'original n'est pas touché
    monkeypatch.setattr(wapp, "current_role", lambda: "operator")
    assert wapp._tf_decl_for_role(view) is view
    assert wapp.required_role_for("/api/tf-declarations/aaaaaaaaaaaa/code", "GET") == "operator"


def test_a_running_kvm_declaration_cannot_be_deleted(tmp_path, monkeypatch):
    store = ts.DeclStore(tmp_path / "tf.db")
    monkeypatch.setattr(wapp, "TF_DECLS", store)
    monkeypatch.setattr(wapp, "TF_WORKSPACES", tmp_path / "ws")
    d = store.create("b" * 12, "tfhost", "busy", target_kind="kvm")
    with wapp.TF_KVM_RUNNING_LOCK:
        wapp.TF_KVM_RUNNING.add(d["id"])
    try:
        with wapp.app.test_client() as c:
            r = c.delete(f"/api/tf-declarations/{d['id']}")
        assert r.status_code == 409 and r.get_json()["code"] == "running"
        assert store.get(d["id"]) is not None
    finally:
        with wapp.TF_KVM_RUNNING_LOCK:
            wapp.TF_KVM_RUNNING.discard(d["id"])
    with wapp.app.test_client() as c:
        assert c.delete(f"/api/tf-declarations/{d['id']}").status_code == 200


def test_the_startup_sweep_removes_staged_files(tmp_path, monkeypatch):
    monkeypatch.setattr(wapp, "TF_WORKSPACES", tmp_path)
    ws = tmp_path / ".kvm" / "tfhost" / "decls" / ("a" * 12)
    ws.mkdir(parents=True)
    for n in (k.KEY_FILE, k.KNOWN_FILE, k.WRAPPER_FILE, "terraform.tfstate"):
        (ws / n).write_text("x")
    assert wapp._tf_kvm_sweep() == 3
    assert sorted(p.name for p in ws.iterdir()) == ["terraform.tfstate"]


def test_the_ssh_wrapper_refuses_a_dollar_in_the_path():
    with pytest.raises(k.SpecError):
        k.ssh_wrapper("/srv/${HOME}/ws")


@pytest.mark.parametrize("stderr, lock, calls, lock_left", [
    ("Error: ... the local package for registry.opentofu.org/harvester/harvester 1.9.0 doesn't match any\n"
     "of the checksums previously recorded in the dependency lock file (this might be", True, 2, False),
    ("Error: Failed to query available provider packages", True, 1, True),
    ("doesn't match any of the checksums previously recorded in the dependency lock file", False, 1, False),
])
def test_a_stale_lock_file_is_recreated_once(tmp_path, monkeypatch, stderr, lock, calls, lock_left):
    """Le verrou d'un espace écrit d'après un téléchargement Internet ne
    bloque pas le miroir local (vu en réel avec OpenTofu 1.12)."""
    seen = []

    class R:
        def __init__(self, rc, err):
            self.returncode, self.stdout, self.stderr = rc, "", err

    def fake_run(argv, **kw):
        seen.append(argv)
        return R(1, stderr) if len(seen) == 1 else R(0, "")

    monkeypatch.setattr(wapp.subprocess, "run", fake_run)
    if lock:
        (tmp_path / ".terraform.lock.hcl").write_text("old")
    wapp._tf_run_cmd(tmp_path, "", ["init", "-input=false"])
    assert len(seen) == calls and (tmp_path / ".terraform.lock.hcl").exists() is lock_left
    seen.clear()
    wapp._tf_run_cmd(tmp_path, "", ["plan"])                    # jamais pour une autre commande
    assert len(seen) == 1
