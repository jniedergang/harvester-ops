"""2.10.0 tâche 5 : identité des secrets figée au déclenchement des actions.

Application réelle, OpenBao simulé (méthode jwt), jeton par personne allumé :

* les secrets PERSONNELS qu'une action remet à son outil sont lus dans la
  requête qui la déclenche, avec le jeton de la personne, puis tenus par le
  bail de l'action ; le fil de travail ne lit plus rien avec un jeton de
  personne (il ne peut même pas : le registre refuse hors requête) ;
* un refus d'OpenBao empêche l'action (aucune relecture par le service) ;
* une lecture tardive inévitable est faite par le service et marquée
  `late-service` (étape et `secret_identity`) ;
* compte local, connexion directe, réglage éteint : comportement 2.9.x.

Mutations jouées (rapport des tâches 2 à 5) : « lecture de personne dans le
fil », « identité relue dans le fil (None) puis service sans marque ».
"""

import json
import sqlite3
import sys
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).parent))
import app as wapp  # noqa: E402
import secret_store as ss  # noqa: E402
from test_sso_person_token_2100 import CID, JWT_MOUNT, KV, PREFIX, console, open_session  # noqa: E402,F401

REF_KEY = "openbao:ssh-cluster/h1#private_key"
REF_KC = "openbao:kubeconfig/h1#kubeconfig"


@pytest.fixture
def world(console, monkeypatch):
    bao = console["bao"]
    bao.put_raw(KV, f"{PREFIX}/kubeconfig/h1", {"kubeconfig": "KC-h1"})
    cfg = {"clusters": [{"name": "h1", "kubeconfig": REF_KC, "ssh": {"key": REF_KEY, "user": "rancher"}}]}
    monkeypatch.setattr(wapp, "load_config_ro", lambda: cfg)
    monkeypatch.setattr(wapp, "load_config", lambda: cfg)
    return dict(console, cfg=cfg)


def fake_run(**kw):
    events, held = [], []
    r = types.SimpleNamespace(cluster="h1", secret_clusters=(), secret_refs=(), secret_files={},
                              secret_identity=None, events=events, held=held)
    r.emit = events.append
    r.hold_secrets = lambda *p: held.extend(p) or len(p)
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def in_request(sess, fn):
    with wapp.app.test_request_context("/"):
        wapp.g.sso_session = sess
        return fn()


def in_thread(fn):
    out = {}

    def go():
        try:
            out["v"] = fn()
        except Exception as e:                        # noqa: BLE001
            out["e"] = e
    t = threading.Thread(target=go)
    t.start()
    t.join()
    if "e" in out:
        raise out["e"]
    return out.get("v")


def kv(bao, path, method="GET"):
    return [r for r in bao.log if r["path"] == f"{KV}/data/{PREFIX}/{path}" and r["method"] == method]


def service_token(world):
    return world["base"]._tok.value


# ---------------------------------------------------------------------------
# Lecture au déclenchement, copies servies au fil
# ---------------------------------------------------------------------------
def test_freeze_reads_personal_secret_with_person_token(world):
    bao, sess = world["bao"], open_session()
    run = fake_run()
    in_request(sess, lambda: wapp._freeze_person_secrets(run))
    assert run.secret_identity == "person"
    assert set(run.secret_files) == {REF_KEY}                           # kubeconfig : classe de service
    assert Path(run.secret_files[REF_KEY]).read_text() == "KEY-h1" and run.held == [run.secret_files[REF_KEY]]
    assert [r["headers"]["x-vault-token"] for r in kv(bao, "ssh-cluster/h1")] == \
        [world["pt"].get(sess.sid)._tok.value]
    assert not kv(bao, "kubeconfig/h1")
    # le fil de l'action : service, la copie de la personne est reprise
    env, mp = in_thread(lambda: wapp._spawn_secret_env(["h1"], (), run.secret_files, identity=ss.SERVICE))
    m = json.loads(Path(mp).read_text())
    assert m[REF_KEY] == run.secret_files[REF_KEY] and Path(m[REF_KC]).read_text() == "KC-h1"
    assert len(kv(bao, "ssh-cluster/h1")) == 1                          # aucune relecture
    assert [r["headers"]["x-vault-token"] for r in kv(bao, "kubeconfig/h1")] == [service_token(world)]
    Path(mp).unlink()


def test_freeze_refused_by_openbao_no_service_read(world):
    bao, sess = world["bao"], open_session()
    bao.jwt[JWT_MOUNT]["allow"] = [(f"{KV}/data/{PREFIX}/ssh-kvm/", {"read"})]
    run = fake_run()
    with pytest.raises(ss.SecretError) as e:
        in_request(sess, lambda: wapp._freeze_person_secrets(run))
    assert e.value.code == "openbao-denied" and e.value.cls == "ssh-cluster"
    assert all(r["headers"]["x-vault-token"] != service_token(world) for r in kv(bao, "ssh-cluster/h1"))
    assert run.secret_files == {}


@pytest.mark.parametrize("who,expect", [("local", "service"), ("direct", "service")])
def test_freeze_local_and_direct_are_service(world, who, expect):
    bao = world["bao"]
    sess = None if who == "local" else open_session(kind="direct")
    run = fake_run()
    in_request(sess, lambda: wapp._freeze_person_secrets(run))
    assert run.secret_identity == expect and run.secret_files == {}
    assert not kv(bao, "ssh-cluster/h1") and not bao.jwt_logins


def test_freeze_setting_off_is_2_9_behaviour(world):
    bao, sess = world["bao"], open_session()
    p = Path(ss._STORE.state_dir) / ss.SETTINGS_FILE
    doc = json.loads(p.read_text())
    doc["person_token"]["enabled"] = False
    p.write_text(json.dumps(doc))
    ss._STORE._sig, ss._STORE._checked = None, 0.0
    run = fake_run()
    in_request(sess, lambda: wapp._freeze_person_secrets(run))
    assert run.secret_identity is None and run.secret_files == {}
    assert not kv(bao, "ssh-cluster/h1") and not bao.jwt_logins
    # et la lecture d'une requête passe par le service
    in_request(sess, lambda: wapp._cluster_ssh_key_path({"key": REF_KEY}))
    assert [r["headers"]["x-vault-token"] for r in kv(bao, "ssh-cluster/h1")] == [service_token(world)]


def test_late_read_marked(world):
    """Copie absente dans le fil (cache vidé) : lecture par le service,
    marquée `late-service` dans l'action et dans une étape."""
    bao, sess = world["bao"], open_session()
    run = fake_run()
    in_request(sess, lambda: wapp._freeze_person_secrets(run))
    run.secret_files = {}
    ss._STORE.clear_cache()

    def worker():
        with wapp._late_reads(run):
            return wapp._spawn_secret_env(["h1"], (), run.secret_files, identity=ss.SERVICE)
    _env, mp = in_thread(worker)
    assert run.secret_identity == "late-service"
    steps = [e for e in run.events if e.get("step_id") == "secrets"]
    assert steps and steps[0]["status"] == "warn" and "ssh-cluster/h1" in steps[0]["message"]
    assert "KEY-h1" not in json.dumps(run.events)
    assert kv(bao, "ssh-cluster/h1")[-1]["headers"]["x-vault-token"] == service_token(world)
    Path(mp).unlink()


def test_no_mark_for_service_actions(world):
    run = fake_run(secret_identity="service")

    def worker():
        with wapp._late_reads(run):
            return wapp._spawn_secret_env(["h1"], (), {}, identity=ss.SERVICE)
    _env, mp = in_thread(worker)
    assert run.secret_identity == "service" and not run.events
    Path(mp).unlink()


def test_person_read_refused_in_worker_thread(world):
    """Le fil d'une action ne crée ni n'emploie de jeton de personne."""
    bao, sess = world["bao"], open_session()
    idt = in_request(sess, lambda: wapp._secret_identity("ssh-cluster"))
    assert idt.kind == "person"
    with pytest.raises(ss.SecretError) as e:
        in_thread(lambda: ss._STORE.get(REF_KEY, identity=idt))
    assert e.value.code == "openbao-person-no-token"
    assert not bao.jwt_logins and not kv(bao, "ssh-cluster/h1")
    assert in_thread(lambda: wapp._secret_identity("ssh-cluster")) == ss.SERVICE


# ---------------------------------------------------------------------------
# Actions réelles : _cli_action
# ---------------------------------------------------------------------------
def test_cli_action_freezes_identity_and_persists_it(world, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    bao, sess = world["bao"], open_session()

    def launch():
        return wapp._cli_action("h1", "test-2100:freeze", ["/usr/bin/env", "true"], "true",
                                check_busy=False)
    run, err = in_request(sess, launch)
    assert err is None and run.secret_identity == "person"
    assert REF_KEY in run.secret_files
    for _ in range(100):
        if run.status in ("done", "error"):
            break
        time.sleep(0.05)
    assert run.status == "done"
    assert run.to_dict()["secret_identity"] == "person"
    assert len(kv(bao, "ssh-cluster/h1")) == 1                          # lu une fois, au déclenchement
    conn = sqlite3.connect(str(wapp.ACTIONS_DB))
    row = conn.execute("SELECT secret_identity FROM actions WHERE id = ?", (run.id,)).fetchone()
    full = conn.execute("SELECT * FROM actions WHERE id = ?", (run.id,)).fetchone()
    conn.close()
    assert row == ("person",)
    # aucune chaîne connue nulle part : valeur témoin, jeton d'identité,
    # jeton d'accès, jeton OpenBao de la personne
    person_tok = world["pt"].get(sess.sid)._tok.value
    blob = (json.dumps(run.to_dict()) + json.dumps(list(run.events)) + json.dumps([str(x) for x in full])
            + caplog.text)
    for known in ("KEY-h1", sess.id_token, sess.id_token.split(".")[1], sess.token, person_tok):
        assert known not in blob


def test_cli_action_refused_never_registered(world):
    bao, sess = world["bao"], open_session()
    bao.jwt[JWT_MOUNT]["allow"] = []
    before = set(wapp.ACTIONS)
    with pytest.raises(ss.SecretError) as e:
        in_request(sess, lambda: wapp._cli_action("h1", "test-2100:denied", ["/usr/bin/env", "true"], "true",
                                                  check_busy=False))
    assert e.value.code == "openbao-denied"
    assert set(wapp.ACTIONS) == before


# ---------------------------------------------------------------------------
# Hôtes KVM et déclarations Terraform KVM
# ---------------------------------------------------------------------------
def test_kvm_virsh_reads_key_in_request(world, tmp_path):
    bao, sess = world["bao"], open_session()
    bao.put_raw(KV, f"{PREFIX}/ssh-kvm/k1", {"private_key": "KVM-KEY"})
    decl = {"name": "k1", "address": "192.0.2.10", "user": "root", "port": 22,
            "ssh_key": "openbao:ssh-kvm/k1#private_key", "known_hosts": "ssh/k1_known"}
    vh = in_request(sess, lambda: wapp._kvm_virsh(decl, str(tmp_path), timeout=5))
    assert Path(vh.key_path).read_text() == "KVM-KEY"
    assert kv(bao, "ssh-kvm/k1")[-1]["headers"]["x-vault-token"] == world["pt"].get(sess.sid)._tok.value
    vh2 = in_request(None, lambda: wapp._kvm_virsh(decl, str(tmp_path), timeout=5))
    assert vh2.key_path is None                                        # service : lecture paresseuse


def test_tf_kvm_prefetch_person(world, tmp_path, monkeypatch):
    bao, sess = world["bao"], open_session()
    # politique hops-person du plan : data tf-kvm (read, create, update), metadata (read)
    bao.jwt[JWT_MOUNT]["allow"] += [(f"{KV}/data/{PREFIX}/tf-kvm/", {"read", "create", "update"}),
                                    (f"{KV}/metadata/{PREFIX}/tf-kvm/", {"read"})]
    bao.put_raw(KV, f"{PREFIX}/ssh-kvm/k1", {"private_key": "KVM-KEY"})
    p = Path(ss._STORE.state_dir) / ss.SETTINGS_FILE
    doc = json.loads(p.read_text())
    doc["migrated"] = {"tf-kvm": {"at": 1}}
    p.write_text(json.dumps(doc))
    ss._STORE._sig, ss._STORE._checked = None, 0.0
    ws = tmp_path / "tf" / ".kvm" / "k1" / "decls" / "d1"
    ws.mkdir(parents=True)
    monkeypatch.setattr(wapp, "_tf_kvm_host", lambda h: ({"name": "k1", "ssh_key": "openbao:ssh-kvm/k1#private_key"},
                                                         None))
    monkeypatch.setattr(wapp, "_tf_decl_ws", lambda decl, create=True: ws)
    monkeypatch.setattr(wapp._tfk, "token_vars", lambda rendered: ["rke2_token"])
    idts = in_request(sess, lambda: {c: wapp._secret_identity(c) for c in ("ssh-kvm", "tf-kvm")})
    mark = in_request(sess, lambda: wapp._tf_kvm_prefetch({"cluster": "k1"}, [], idts))
    assert mark == "person"
    tok = world["pt"].get(sess.sid)._tok.value
    assert kv(bao, "ssh-kvm/k1")[-1]["headers"]["x-vault-token"] == tok
    puts = kv(bao, "tf-kvm/k1/d1/rke2_token", "POST") + kv(bao, "tf-kvm/k1/d1/rke2_token", "PUT")
    assert puts and puts[-1]["headers"]["x-vault-token"] == tok
    # le fil : service, servi par le cache commun, aucune requête de plus
    n = len(bao.log)
    val = in_thread(lambda: wapp._tf_kvm_secret(ws, "rke2_token", identity=ss.SERVICE))
    assert len(val) == 64 and len(bao.log) == n
    # refus : TfLaunchError côté lancement, SecretError ici
    bao.jwt[JWT_MOUNT]["allow"] = []
    world["pt"].drop(sess.sid)
    with pytest.raises(ss.SecretError):
        in_request(sess, lambda: wapp._tf_kvm_prefetch({"cluster": "k1"}, [], idts))


def test_tf_kvm_prefetch_off_or_service(world, monkeypatch):
    sess = open_session()
    assert wapp._tf_kvm_prefetch({"cluster": "k1"}, [], None) is None
    monkeypatch.setattr(wapp, "_tf_kvm_host", lambda h: ({"name": "k1", "ssh_key": "openbao:ssh-kvm/k1#private_key"},
                                                         None))
    monkeypatch.setattr(wapp._tfk, "token_vars", lambda rendered: [])
    mark = in_request(None, lambda: wapp._tf_kvm_prefetch({"cluster": "k1"}, [],
                                                          {"ssh-kvm": ss.Identity("service", "admin")}))
    assert mark == "service" and not world["bao"].jwt_logins
    assert sess is not None


def test_action_secret_refs():
    cfg = {"clusters": [{"name": "h1", "kubeconfig": REF_KC, "ssh": {"key": REF_KEY}},
                        {"name": "h2", "kubeconfig": "/path/kc"}]}
    orig = wapp.load_config_ro
    wapp.load_config_ro = lambda: cfg
    try:
        assert wapp._action_secret_refs(["h1", "h2", None], ["openbao:replication-notify/global#a__token"]) == [
            "openbao:replication-notify/global#a__token", REF_KC, REF_KEY]
    finally:
        wapp.load_config_ro = orig
