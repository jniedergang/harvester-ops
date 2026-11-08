"""2.9.0 tâche 4 : lecteurs rangés derrière le magasin de secrets, une ligne
par lecteur.

Pour chaque lecteur : en mode local (aucune référence), la valeur ou le
chemin d'avant, inchangé ; avec une référence `openbao:` (faux serveur
OpenBao, magasin du processus remplacé), la valeur d'OpenBao, ou un chemin
de travail sur tmpfs qui la porte.

Mutation jouée (rapport des tâches 2 à 4) : « copie d'identité écrite sous
/var/lib avec OpenBao actif ».
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(ROOT / "web"))

import openbao_client as oc      # noqa: E402
import secret_store as ss        # noqa: E402
from fake_openbao import FakeOpenBao  # noqa: E402

CID = "abc123def456"
PREFIX = f"consoles/{CID}"
VALUE = "S3CRET-reader-290"


def _mi(*paths):
    lines = ["22 1 8:1 / / rw - ext4 /dev/sda1 rw"]
    lines += [f"{30 + i} 22 0:{40 + i} / {p} rw - tmpfs tmpfs rw" for i, p in enumerate(paths)]
    return "\n".join(lines) + "\n"


@pytest.fixture
def bao():
    s = FakeOpenBao().start()
    s.creds = s.add_role()
    yield s
    s.stop()


@pytest.fixture
def env(tmp_path, bao, monkeypatch):
    """État de console, OpenBao actif (mode a) sur le faux serveur, magasin
    du processus remplacé ; copies de travail dans `run/hops-secrets`."""
    state, run = tmp_path / "state", tmp_path / "run"
    state.mkdir()
    run.mkdir()
    (state / ss.SETTINGS_FILE).write_text(json.dumps({
        "mode": "a", "addr": bao.addr, "console_id": CID, "secret_files_dir": str(run / "hops-secrets")}))
    base = oc.Client(bao.addr, timeout=2, retry_delays=(0.01,))
    base.login_approle(*bao.creds)
    st = ss.MultiStore(state, client_factory=lambda actor=None, action=None: base.with_headers(action, actor),
                       mountinfo=_mi(str(run)))
    monkeypatch.setattr(ss, "_STORE", st)
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(state))

    def put(cls, path, field, value=VALUE):
        bao.put_raw("harvester-ops", f"{PREFIX}/{cls}/{path}", {field: value})
        return ss.make_ref(cls, path, field, "openbao")
    return {"state": state, "run": run / "hops-secrets", "store": st, "put": put, "bao": bao}


def _on_tmpfs(env, p):
    return Path(p).parent == env["run"] and Path(p).read_text() == VALUE


@pytest.fixture
def local(tmp_path, monkeypatch):
    """Magasin du processus en mode local, état vide."""
    st = ss.MultiStore(tmp_path / "local-state")
    monkeypatch.setattr(ss, "_STORE", st)
    return st


# ---------------------------------------------------------------------------
# web/app.py
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def wapp():
    import app
    return app


@pytest.mark.parametrize("value", ["/etc/harvester-ops/kc/h1.yaml", "/var/lib/harvester-ops/kubeconfigs/h1.yaml", ""])
def test_app_kubeconfig_path_local(wapp, local, value):
    assert wapp._cluster_kubeconfig_path({"name": "h1", "kubeconfig": value}) == value


def test_app_kubeconfig_path_by_name(wapp, local, monkeypatch):
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {"clusters": [{"name": "h1", "kubeconfig": "/k/h1.yaml"}]})
    assert wapp._cluster_kubeconfig_path("h1") == "/k/h1.yaml"
    assert wapp._cluster_kubeconfig_path("zz") is None


def test_app_kubeconfig_path_openbao(wapp, env):
    ref = env["put"]("kubeconfig", "h1", "kubeconfig")
    p = wapp._cluster_kubeconfig_path({"name": "h1", "kubeconfig": ref})
    assert _on_tmpfs(env, p)
    assert wapp._cluster_kubeconfig_path({"name": "h1", "kubeconfig": ref}) == p      # copie partagée


def test_app_kubeconfig_path_openbao_down(wapp, env):
    ref = env["put"]("kubeconfig", "h1", "kubeconfig")
    env["bao"].stop()
    with pytest.raises(ss.SecretError) as e:
        wapp._cluster_kubeconfig_path({"name": "h1", "kubeconfig": ref})
    assert e.value.code == "openbao-unreachable" and e.value.name == "h1"
    assert wapp._cluster_kubeconfig_path({"name": "h1", "kubeconfig": ref}, missing_ok=True) is None


def test_app_kubectl_for_cluster_uses_the_reader(wapp, env, monkeypatch):
    ref = env["put"]("kubeconfig", "h1", "kubeconfig")
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {"clusters": [{"name": "h1", "kubeconfig": ref}]})
    monkeypatch.setattr(wapp, "_READ_WORKER_CTX", None)
    monkeypatch.setattr(wapp, "_sso_session", lambda: None)
    monkeypatch.setattr(wapp, "current_cluster_identity", lambda: None)
    assert _on_tmpfs(env, wapp._kubectl_for_cluster("h1"))


@pytest.mark.parametrize("ssh,expected", [({"key": "/s/h1_id"}, "/s/h1_id"), ({}, None), (None, None),
                                          ({"key": ""}, "")])
def test_app_ssh_key_path_local(wapp, local, ssh, expected):
    assert wapp._cluster_ssh_key_path(ssh) == expected


def test_app_ssh_key_path_openbao(wapp, env):
    ref = env["put"]("ssh-cluster", "h1", "private_key")
    assert _on_tmpfs(env, wapp._cluster_ssh_key_path({"key": ref}))


def test_app_identity_copy_on_tmpfs_when_active(wapp, env, tmp_path, monkeypatch):
    var_lib = tmp_path / "var-lib-identities"
    monkeypatch.setattr(wapp, "IDENTITY_DIR", var_lib)
    monkeypatch.setattr(wapp, "_identity_cache", {})
    kc = tmp_path / "kc.yaml"
    kc.write_text("users:\n- name: u\n  user: {token: " + VALUE + "}\n")
    out = wapp._identity_kubeconfig(str(kc), {"user": "alice", "groups": []})
    assert Path(out).parent == env["run"] / "identities"
    assert not var_lib.exists()


def test_app_identity_copy_unchanged_when_local(wapp, local, tmp_path, monkeypatch):
    var_lib = tmp_path / "var-lib-identities"
    monkeypatch.setattr(wapp, "IDENTITY_DIR", var_lib)
    monkeypatch.setattr(wapp, "_identity_cache", {})
    kc = tmp_path / "kc.yaml"
    kc.write_text("users:\n- name: u\n  user: {token: x}\n")
    assert Path(wapp._identity_kubeconfig(str(kc), {"user": "alice"})).parent == var_lib


def test_app_stage_kubeconfig(wapp, env, local, tmp_path, monkeypatch):
    kc = tmp_path / "kc.yaml"
    kc.write_text(VALUE)
    ws_local = tmp_path / "ws1"
    ws_local.mkdir()
    monkeypatch.setattr(ss, "_STORE", local)
    d = wapp._stage_kubeconfig(kc, ws_local)
    assert d.is_file() and not d.is_symlink() and (d.stat().st_mode & 0o777) == 0o600
    monkeypatch.setattr(ss, "_STORE", env["store"])
    ws = tmp_path / "ws2"
    ws.mkdir()
    d = wapp._stage_kubeconfig(kc, ws)
    assert d.is_symlink() and Path(os.readlink(d)).parent == env["run"] and d.read_text() == VALUE


def test_app_tf_kvm_secret(wapp, env, local, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(ss, "_STORE", local)
    v = wapp._tf_kvm_secret(ws, "rke2_token")
    assert len(v) == 64 and wapp._tf_kvm_secret(ws, "rke2_token") == v
    ref = env["put"]("tf-kvm", "k1/d1/rke2_token", "value")
    (ws / ".secrets" / "rke2_token").write_text(ref + "\n")
    monkeypatch.setattr(ss, "_STORE", env["store"])
    assert wapp._tf_kvm_secret(ws, "rke2_token") == VALUE


def test_app_tf_kvm_stage_key(wapp, env, tmp_path, monkeypatch):
    ref = env["put"]("ssh-kvm", "k1", "private_key")
    (env["state"] / "ssh").mkdir()
    (env["state"] / "ssh" / "k1_known").write_text("[h]:22 ssh-ed25519 AAAA\n")
    monkeypatch.setattr(wapp, "_state_dir", lambda: env["state"])
    ws = tmp_path / "ws"
    ws.mkdir()
    wapp._tf_kvm_stage(ws, {"name": "k1", "ssh_key": ref, "known_hosts": "ssh/k1_known", "address": "h",
                            "user": "root"})
    import tf_kvm
    assert (ws / tf_kvm.KEY_FILE).read_text() == VALUE
    wapp._tf_kvm_unstage(ws)


# ---------------------------------------------------------------------------
# bin/lib
# ---------------------------------------------------------------------------
def test_cluster_decl_keeps_refs():
    import cluster_decl as cd
    e = {"name": "h1", "kubeconfig": "openbao:kubeconfig/h1#kubeconfig",
         "ssh": {"key": "openbao:ssh-cluster/h1#private_key"}}
    assert cd.resolve(e, "/state") == e
    assert cd.relativize(e, "/state") == e
    assert cd.resolve({"kubeconfig": "kubeconfigs/h1.yaml"}, "/state")["kubeconfig"] == "/state/kubeconfigs/h1.yaml"


def test_cluster_decl_materialize(env):
    import cluster_decl as cd
    plain = {"name": "h1", "kubeconfig": "/k.yaml", "ssh": {"key": "/s"}}
    assert cd.materialize(plain) is plain
    e = {"name": "h1", "kubeconfig": env["put"]("kubeconfig", "h1", "kubeconfig"),
         "ssh": {"user": "u", "key": env["put"]("ssh-cluster", "h1", "private_key")}}
    out = cd.materialize(e)
    assert _on_tmpfs(env, out["kubeconfig"]) and _on_tmpfs(env, out["ssh"]["key"]) and out["ssh"]["user"] == "u"
    assert e["kubeconfig"].startswith("openbao:")              # entrée d'origine intacte


def test_kube_cluster_config_materializes(env, monkeypatch, tmp_path):
    import kube
    ref = env["put"]("kubeconfig", "h1", "kubeconfig")
    (env["state"] / "clusters.d").mkdir()
    (env["state"] / "clusters.d" / "h1.yaml").write_text(f"name: h1\nkubeconfig: {ref}\nnodes: []\n")
    monkeypatch.setenv("HARVESTER_OPS_CONFIG", str(tmp_path / "absent.yaml"))
    assert _on_tmpfs(env, kube.cluster_config("h1")["kubeconfig"])


def test_kube_cluster_config_local_unchanged(tmp_path, monkeypatch, local):
    import kube
    st = tmp_path / "s"
    (st / "clusters.d").mkdir(parents=True)
    (st / "clusters.d" / "h1.yaml").write_text("name: h1\nkubeconfig: kubeconfigs/h1.yaml\nnodes: []\n")
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(st))
    monkeypatch.setenv("HARVESTER_OPS_CONFIG", str(tmp_path / "absent.yaml"))
    assert kube.cluster_config("h1")["kubeconfig"] == str(st / "kubeconfigs" / "h1.yaml")


@pytest.mark.parametrize("ssh_key,ok", [("ssh/k1_id", True), ("openbao:ssh-kvm/k1#private_key", True),
                                         ("openbao:ssh-kvm/k2#private_key", False), ("/etc/k", False)])
def test_kvm_decl_accepts_key_ref(ssh_key, ok):
    import kvm_decl
    d = {"name": "k1", "address": "10.0.0.1", "user": "root", "ssh_key": ssh_key, "known_hosts": "ssh/k1_known"}
    assert (kvm_decl.check_decl(d) is None) is ok


def test_kvm_host_ssh_base_key(env):
    import kvm_host
    ref = env["put"]("ssh-kvm", "k1", "private_key")
    import shutil
    import tempfile
    short = tempfile.mkdtemp(prefix="hk", dir="/tmp")        # socket ControlMaster : chemin court
    try:
        d = {"name": "k1", "address": "10.0.0.1", "user": "root", "ssh_key": ref, "known_hosts": "ssh/k1_known"}
        argv = kvm_host._ssh_base(d, short)
        assert _on_tmpfs(env, argv[argv.index("-i") + 1])
        d["ssh_key"] = "ssh/k1_id"
        argv = kvm_host._ssh_base(d, short)
        assert argv[argv.index("-i") + 1] == str(Path(short) / "ssh" / "k1_id")
    finally:
        shutil.rmtree(short)


def test_node_images_ssh_options(env):
    import node_images
    assert node_images.ssh_options({"key": "/s/id"})[-2:] == ["-i", "/s/id"]
    opts = node_images.ssh_options({"key": env["put"]("ssh-cluster", "h1", "private_key")})
    assert _on_tmpfs(env, opts[-1])


def test_rancher_read_secret(env, tmp_path):
    import rancher_servers as rs
    f = tmp_path / "r1.oidc-secret"
    f.write_text("plain-secret\n")
    assert rs.read_secret(str(f)) == "plain-secret"
    assert rs.read_secret(str(tmp_path / "none")) is None and rs.read_secret(None) is None
    ref = env["put"]("rancher-oidc", "r1", "client_secret")
    f.write_text(ref + "\n")
    assert rs.read_secret(str(f)) == VALUE
    assert rs.read_secret(ref) == VALUE
    env["bao"].log.clear()
    assert rs.sso_enabled({"sso": {"client_id": "c", "secret_file": ref}}) is True
    assert env["bao"].log == []                                # une liste ne lit pas OpenBao
    assert rs.sso_enabled({"sso": {"client_id": "c", "secret_file": str(tmp_path / "none")}}) is False


def test_rancher_abs_keeps_ref(tmp_path):
    import rancher_servers as rs
    store = rs.Store(lambda: tmp_path)
    assert store._abs("openbao:rancher-oidc/r1#client_secret") == "openbao:rancher-oidc/r1#client_secret"
    assert store._abs("rancher.d/r1.oidc-secret") == str(tmp_path / "rancher.d/r1.oidc-secret")


def _notify_cfg(token, hdr):
    return {"enabled": True, "channels": [
        {"id": "mx", "kind": "matrix", "homeserver": "https://m", "room": "!r", "token": token},
        {"id": "wh", "kind": "webhook", "url": "https://w", "headers": {"A": "1"}, "secret_headers": {"X-K": hdr}}]}


def test_replication_notify_resolve(env):
    import replication_notify as rn
    plain = _notify_cfg("t", "h")
    assert rn.resolve_secrets(plain) is plain
    cfg = _notify_cfg(env["put"]("replication-notify", "global", "mx__token"),
                      ss.make_ref("replication-notify", "global", "wh__hdr_0000000000", "openbao"))
    env["bao"].put_raw("harvester-ops", f"{PREFIX}/replication-notify/global",
                       {"mx__token": VALUE, "wh__hdr_0000000000": "H-" + VALUE})
    out = rn.resolve_secrets(cfg)
    assert out["channels"][0]["token"] == VALUE and out["channels"][1]["secret_headers"] == {"X-K": "H-" + VALUE}
    assert cfg["channels"][0]["token"].startswith("openbao:")    # réglage rangé intact
    assert rn.mask(cfg)["channels"][0]["token"] == rn.MASK       # écran inchangé


def test_replication_hook_resolves_secret_headers(env):
    import replication_run as rr
    sent = {}
    import replication_http as rh
    orig = rh.request

    def fake_request(method, url, headers=None, **kw):
        sent.update(headers or {})
        return 204
    rh.request = fake_request
    try:
        ref = env["put"]("replication-hook", "rp-0123456789ab/pre", "hdr_0000000000")
        res = rr.call_hook({"name": "pre", "url": "https://h", "headers": {"A": "1"},
                            "secret_headers": {"Authorization": ref}})
        assert res["ok"] and sent["Authorization"] == VALUE and sent["A"] == "1"
        res = rr.call_hook({"name": "pre", "url": "https://h", "secret_headers": {"Authorization": "plain"}})
        assert res["ok"] and sent["Authorization"] == "plain"
        env["bao"].stop()
        env["store"].clear_cache()
        res = rr.call_hook({"name": "pre", "url": "https://h", "secret_headers": {"Authorization": ref}})
        assert res["ok"] is False and "openbao-unreachable" in res["error"] and VALUE not in res["error"]
    finally:
        rh.request = orig


# ---------------------------------------------------------------------------
# bin/lib/common.sh
# ---------------------------------------------------------------------------
def _sh(tmp_path, script, fake_tool=None):
    lib = tmp_path / "bin" / "lib"
    lib.mkdir(parents=True)
    (lib / "common.sh").write_text((ROOT / "bin" / "lib" / "common.sh").read_text())
    if fake_tool:
        (tmp_path / "bin" / "harvester-secrets.py").write_text(fake_tool)
    return subprocess.run(["bash", "-c", f'source "{lib}/common.sh"; {script}'], capture_output=True, text=True,
                          env=dict(os.environ, HARVESTER_OPS_STATE_DIR="/st"), timeout=30)


def test_common_sh_plain_value_unchanged(tmp_path):
    r = _sh(tmp_path, '_CLUSTER_ORIGIN=console; _secret_field V "kubeconfigs/h1.yaml"; echo "$V";'
                      ' _CLUSTER_ORIGIN=config; _secret_field W "/etc/kc"; echo "$W"')
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["/st/kubeconfigs/h1.yaml", "/etc/kc"]


def test_common_sh_reference_gets_tmpfs_copy_removed_at_exit(tmp_path):
    work = tmp_path / "copy"
    tool = ("import sys, pathlib\n"
            f"p = pathlib.Path({str(work)!r}); p.write_text('K'); assert sys.argv[1:] == ['file', "
            "'openbao:kubeconfig/h1#kubeconfig']\nprint(p)\n")
    r = _sh(tmp_path, '_secret_field V "openbao:kubeconfig/h1#kubeconfig"; cat "$V"', tool)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "K" and not work.exists()             # effacée à la sortie du script


def test_common_sh_reference_failure(tmp_path):
    r = _sh(tmp_path, '_secret_field V "openbao:kubeconfig/h1#kubeconfig" || echo REFUSED',
            "import sys\nsys.exit(1)\n")
    assert "REFUSED" in r.stdout


def test_console_drops_vault_and_bao_environment():
    """La console retire VAULT_* et BAO_* de son environnement au démarrage :
    aucun outil lancé ne les reçoit."""
    code = ("import os, sys; os.environ['BAO_TOKEN']='x'; os.environ['VAULT_ADDR']='y';"
            f"sys.path.insert(0, {str(ROOT / 'web')!r}); import app;"
            "print(sorted(k for k in os.environ if k.startswith(('VAULT_', 'BAO_'))))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                       env=dict(os.environ, BAO_ADDR="z"), cwd=str(ROOT))
    assert r.returncode == 0, r.stderr[-500:]
    assert r.stdout.strip().splitlines()[-1] == "[]"


# ---------------------------------------------------------------------------
# Revue des tâches 2 à 4 : copies d'identité (I2, I7)
# ---------------------------------------------------------------------------
def test_app_identity_copies_follow_their_shared_copy(wapp, env, tmp_path, monkeypatch):
    """I2 : une copie usurpée tirée d'une copie partagée est rendue avec
    elle ; une seule par cluster et par personne, effacée à l'arrêt."""
    monkeypatch.setattr(wapp, "IDENTITY_DIR", tmp_path / "var-lib-identities")
    monkeypatch.setattr(wapp, "_identity_cache", {})
    ref = env["put"]("kubeconfig", "h1", "kubeconfig", "users:\n- name: u\n  user: {token: " + VALUE + "}\n")
    entry = {"name": "h1", "kubeconfig": ref}
    p1 = wapp._cluster_kubeconfig_path(entry)
    d1 = wapp._identity_kubeconfig(p1, {"user": "alice", "groups": []})
    env["store"].clear_cache()
    p2 = wapp._cluster_kubeconfig_path(entry)
    d2 = wapp._identity_kubeconfig(p2, {"user": "alice", "groups": []})
    assert p2 != p1 and d2 != d1
    assert not Path(d1).exists() and not Path(p1).exists() and Path(d2).exists()
    assert len(wapp._identity_cache) == 1
    env["store"].purge()
    assert not Path(d2).exists()


def test_app_identity_copy_from_var_lib_not_served_once_active(wapp, env, local, tmp_path, monkeypatch):
    """Point 263 : une copie faite en mode local (sous /var/lib) n'est plus
    servie quand OpenBao devient actif."""
    var_lib = tmp_path / "var-lib-identities"
    monkeypatch.setattr(wapp, "IDENTITY_DIR", var_lib)
    monkeypatch.setattr(wapp, "_identity_cache", {})
    kc = tmp_path / "kc.yaml"
    kc.write_text("users:\n- name: u\n  user: {token: x}\n")
    assert Path(wapp._identity_kubeconfig(str(kc), {"user": "alice"})).parent == var_lib
    monkeypatch.setattr(ss, "_STORE", env["store"])
    assert Path(wapp._identity_kubeconfig(str(kc), {"user": "alice"})).parent == env["run"] / "identities"


def test_app_identity_refused_without_tmpfs_when_active(wapp, env, tmp_path, monkeypatch):
    """I7 : OpenBao actif et aucune copie possible : refus, jamais le
    kubeconfig partagé de la console."""
    monkeypatch.setattr(wapp, "_identity_cache", {})
    monkeypatch.setattr(wapp, "_identity_dir", lambda: None)
    kc = tmp_path / "kc.yaml"
    kc.write_text("users:\n- name: u\n  user: {token: x}\n")
    with pytest.raises(wapp.IdentityDelegationError):
        wapp._identity_kubeconfig(str(kc), {"user": "alice"})
    with wapp.app.test_request_context("/"):
        body, code = wapp._identity_delegation_refused(wapp.IdentityDelegationError("x"))
    assert code == 503 and body.get_json()["code"] == "identity-delegation-unavailable"
