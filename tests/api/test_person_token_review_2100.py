"""2.10.0 : corrections de la revue des tâches 2 à 5 (I1 à I6 et mineurs).

Application réelle, OpenBao simulé (méthode jwt), jeton par personne allumé
(fixture `console` des tests de la tâche 4). Chaque cas échoue sans sa
correction (rapport des tâches 6 et 7, section « Review fixes 2-5 »).
"""

import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).parent))
import app as wapp  # noqa: E402
import secret_store as ss  # noqa: E402
from fake_openbao import fake_jwt  # noqa: E402
from test_sso_person_token_2100 import AUD, ISS, JWT_MOUNT, KV, PREFIX, console, open_session  # noqa: E402,F401

KVM_REF = "openbao:ssh-kvm/k1#private_key"
DECL = {"name": "k1", "address": "192.0.2.10", "user": "root", "port": 22, "ssh_key": KVM_REF,
        "known_hosts": "ssh/k1_known"}


def other_session(sub="u-other"):
    now = int(time.time())
    idt = fake_jwt({"iss": ISS, "aud": [AUD], "sub": sub, "exp": now + 600, "iat": now, "nonce": "n0"})
    return wapp._sso_store().open({"id": sub, "username": sub}, "operator", "acc-" + sub, "refresh-0", 3600,
                                  id_token=idt)


def as_who(sess, fn, path="/"):
    with wapp.app.test_request_context(path):
        wapp.g.sso_session = sess
        return fn()


# ---------------------------------------------------------------------------
# I1 (décision 311) : vues KVM partagées lues par le service
# ---------------------------------------------------------------------------
@pytest.fixture
def kvm_world(console, monkeypatch, tmp_path):
    bao = console["bao"]
    bao.put_raw(KV, f"{PREFIX}/ssh-kvm/k1", {"private_key": "KVM-KEY"})
    monkeypatch.setattr(wapp._kd, "load_all", lambda state, warn=None: [dict(DECL)])
    monkeypatch.setattr(wapp._kd, "load", lambda state, name: dict(DECL))
    seen = []

    def read_all(hosts, **kw):
        seen.extend(h.key_path for h in hosts)
        return [{"name": "k1", "vms": [], "errors": []}]
    monkeypatch.setattr(wapp._kvm_cli, "read_all", read_all)
    monkeypatch.setattr(wapp, "READ_SHARE", wapp._rsh.ReadShare())
    return dict(console, seen=seen)


@pytest.mark.parametrize("reader", ["vms_all_kvm", "kvm_read_all", "shared_view"])
def test_shared_kvm_reads_are_service_for_every_person(kvm_world, reader):
    """Une personne refusée sur l'hôte, une autre permise, un compte local :
    la vue partagée ne lit jamais la clé avec un jeton de personne, ne
    tombe pas, et ne sert pas à l'une ce qu'OpenBao lui aurait refusé."""
    bao = kvm_world["bao"]
    bao.jwt[JWT_MOUNT]["allow"] = []                       # la personne A n'a rien

    @wapp.shared_read(scope=wapp.KVM_SCOPE)
    def view():
        vh = wapp._kvm_virsh(dict(DECL), str(wapp._state_dir()), timeout=5)
        return wapp.jsonify({"key": vh.key_path})

    calls = {"vms_all_kvm": lambda: wapp._vms_all_kvm(),
             "kvm_read_all": lambda: wapp._kvm_read_all(),
             "shared_view": lambda: view().get_json()}[reader]
    for sess in (open_session(), other_session(), None):
        out = as_who(sess, calls)
        assert out is not None
    assert not bao.jwt_logins                              # aucun jeton de personne demandé
    assert all(k is None for k in kvm_world["seen"])       # clé lue paresseusement par le service
    assert not [r for r in bao.log if r["path"] == f"{KV}/data/{PREFIX}/ssh-kvm/k1"
                and r["headers"].get("x-vault-token") != kvm_world["base"]._tok.value]


def test_kvm_gesture_still_reads_with_the_person_token(kvm_world):
    bao, sess = kvm_world["bao"], open_session()
    bao.jwt[JWT_MOUNT]["allow"] = [(f"{KV}/data/{PREFIX}/ssh-kvm/", {"read"})]
    vh, err = as_who(sess, lambda: wapp._kvm_host(str(wapp._state_dir()), "k1"))
    assert err is None and Path(vh.key_path).read_text() == "KVM-KEY"
    assert bao.jwt_logins


def test_warm_surveys_never_read_as_a_person(kvm_world, monkeypatch):
    bao, sess = kvm_world["bao"], open_session()
    bao.jwt[JWT_MOUNT]["allow"] = []
    warmed = []
    monkeypatch.setattr(wapp._ko.SURVEYS, "warm", lambda vh, slot=None: warmed.append(vh.key_path))
    as_who(sess, lambda: wapp._kvm_warm_surveys(["k1"]))
    assert warmed == [None] and not bao.jwt_logins


# ---------------------------------------------------------------------------
# I2 : valeur absente au déclenchement, compteur et fichier de spécification
# ---------------------------------------------------------------------------
REF_KEY = "openbao:ssh-cluster/h1#private_key"


@pytest.fixture
def action_world(console, monkeypatch):
    cfg = {"clusters": [{"name": "h1", "kubeconfig": "/nonexistent/kc", "ssh": {"key": REF_KEY, "user": "rancher"}}]}
    monkeypatch.setattr(wapp, "load_config_ro", lambda: cfg)
    monkeypatch.setattr(wapp, "load_config", lambda: cfg)
    return console


def _in_flight():
    return wapp.metric_actions_in_flight._value.get()


def _wait(run):
    for _ in range(200):
        if run.status in ("done", "error"):
            return
        time.sleep(0.05)


def test_missing_personal_secret_is_a_warning_not_a_500(action_world, caplog):
    import logging
    caplog.set_level(logging.WARNING)
    bao, sess = action_world["bao"], open_session()
    bao.kv.pop((KV, f"{PREFIX}/ssh-cluster/h1"))               # la valeur a disparu
    run, err = as_who(sess, lambda: wapp._cli_action("h1", "test-2100:missing", ["/usr/bin/env", "true"], "true",
                                                    check_busy=False))
    assert err is None and run is not None
    _wait(run)
    assert run.status == "done" and REF_KEY not in run.secret_files
    assert "ssh-cluster/h1" in caplog.text and "missing" in caplog.text
    # lue une fois, au déclenchement, avec le jeton de la personne ; jamais
    # relue par le service dans le fil
    reads = [r["headers"].get("x-vault-token") for r in bao.log
             if r["path"] == f"{KV}/data/{PREFIX}/ssh-cluster/h1"]
    assert reads == [action_world["pt"].get(sess.sid)._tok.value]
    assert run.secret_identity == "person"


@pytest.mark.parametrize("launcher", ["cli_action", "start_action"])
def test_any_freeze_error_leaves_no_counter_nor_spec_file(action_world, monkeypatch, tmp_path, launcher):
    sess = open_session()
    monkeypatch.setattr(wapp, "_capi_work_dir", lambda: tmp_path)

    def boom(run):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(wapp, "_freeze_person_secrets", boom)
    before = _in_flight()
    with pytest.raises(RuntimeError):
        if launcher == "cli_action":
            as_who(sess, lambda: wapp._cli_action("h1", "test-2100:boom", ["/usr/bin/env", "true"], "true",
                                                  check_busy=False, spec={"x": 1}))
        else:
            as_who(sess, lambda: wapp.start_action("ns-stop", "h1", namespace="default"))
    assert _in_flight() == before
    assert not list(tmp_path.glob("*-spec-*.json"))


# ---------------------------------------------------------------------------
# I5, M1 à M4 : registre des jetons de personne
# ---------------------------------------------------------------------------
from test_secret_identity_2100 import REF, KEY, World, jwt_for, srv  # noqa: E402,F401
import test_secret_identity_2100 as t3  # noqa: E402


@pytest.fixture
def w(srv, tmp_path):
    return World(srv, tmp_path)


def login_posts(srv):
    return [r for r in srv.log if r["path"] == f"auth/{t3.JWT_MOUNT}/login"]


def test_renewal_hook_never_waits_for_openbao(console):
    """I5 : le fil qui renouvelle les sessions Rancher n'attend pas un
    OpenBao lent ; la reconnexion part dans son propre fil."""
    from test_sso_person_token_2100 import read_key_as
    bao, sess = console["bao"], open_session()
    read_key_as(sess)
    now = int(time.time())
    sess.id_token = fake_jwt({"iss": ISS, "aud": [AUD], "sub": "user-kk67j", "exp": now + 600, "iat": now})
    bao.hang = 1.5
    t0 = time.monotonic()
    wapp._sso_person_renewed(sess)
    assert time.monotonic() - t0 < 0.5
    bao.hang = 0
    console["pt"].join(10)


def test_failed_reconnect_in_outage_keeps_the_outage_cache(w, srv):
    """I5 : une panne pendant la reconnexion garde l'entrée et l'entité ;
    la personne reste servie par le cache de panne (15 min, point 288)."""
    alice = w.person("s-a", "user-alice")
    assert w.store.get(REF, identity=alice) == KEY
    ent = w.pt.entity("s-a")
    srv.sealed = True
    assert w.pt.renew("s-a", jwt_for("user-alice", iat=int(time.time()) + 1)) is False
    assert w.pt.entity("s-a") == ent
    assert w.store.get(REF, identity=alice) == KEY


def test_reconnect_is_tried_once(w, srv):
    alice = w.person("s-a", "user-alice")
    w.store.get(REF, identity=alice)
    n = len(login_posts(srv))
    srv.fail_next.extend([(502, {"errors": ["bad gateway"]})] * 4)
    assert w.pt.renew("s-a", jwt_for("user-alice", iat=int(time.time()) + 1)) is False
    assert len(login_posts(srv)) == n + 1
    srv.fail_next.clear()


def test_refused_reconnect_drops(w, srv):
    alice = w.person("s-a", "user-alice")
    w.store.get(REF, identity=alice)
    old = w.pt.get("s-a")._tok.value
    assert w.pt.renew("s-a", jwt_for("user-alice", iss="https://evil/oidc")) is False
    assert w.pt.entity("s-a") is None and old not in srv.tokens


def test_parallel_first_reads_log_in_once(w, srv):
    """M1 : deux premières lectures simultanées d'une même session : une
    seule connexion (sinon un jeton jamais révoqué pendant 15 min)."""
    alice = w.person("s-a", "user-alice")
    srv.hang = 0.3
    out = []
    ts = [threading.Thread(target=lambda: out.append(w.store.get(REF, identity=alice))) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    srv.hang = 0
    assert out == [KEY] * 3
    assert len(srv.jwt_logins) == 1


def test_close_during_reconnect_does_not_bring_the_entry_back(w, srv):
    alice = w.person("s-a", "user-alice")
    w.store.get(REF, identity=alice)
    real = w.pt._connect
    got = {}

    def connect(*a, **kw):
        c, ent = real(*a, **kw)
        got["tok"] = c._tok.value
        w.pt.drop("s-a")                                  # session close pendant la connexion
        return c, ent
    w.pt._connect = connect
    assert w.pt.renew("s-a", jwt_for("user-alice", iat=int(time.time()) + 1)) is False
    assert w.pt.get("s-a") is None and w.pt.entity("s-a") is None
    assert got["tok"] not in srv.tokens                   # le nouveau jeton est révoqué


def test_forget_of_a_replaced_client_keeps_the_new_one(w, srv):
    alice = w.person("s-a", "user-alice")
    w.store.get(REF, identity=alice)
    stale = w.pt.get("s-a").with_headers("act", "x")
    assert w.pt.renew("s-a", jwt_for("user-alice", iat=int(time.time()) + 1)) is True
    fresh = w.pt.get("s-a")
    w.pt.forget("s-a", stale)
    assert w.pt.get("s-a") is fresh


def test_setting_off_stops_reconnects(console):
    """M2 : réglage éteint, un rafraîchissement Rancher révoque le jeton de
    la personne au lieu de le reconnecter."""
    from test_sso_person_token_2100 import read_key_as
    bao, sess = console["bao"], open_session()
    read_key_as(sess)
    tok = console["pt"].get(sess.sid)._tok.value
    p = Path(ss._STORE.state_dir) / ss.SETTINGS_FILE
    doc = json.loads(p.read_text())
    doc["person_token"]["enabled"] = False
    p.write_text(json.dumps(doc))
    ss._STORE._sig, ss._STORE._checked = None, 0.0
    n = len(bao.jwt_logins)
    wapp._sso_person_renewed(sess)
    console["pt"].join()
    assert len(bao.jwt_logins) == n and tok not in bao.tokens and console["pt"].get(sess.sid) is None


def test_closing_one_session_keeps_the_other_sessions_cache(w, srv):
    """M3 : même personne dans deux sessions ; en fermer une ne vide pas le
    cache de panne de l'autre."""
    a1, a2 = w.person("s-1", "user-alice"), w.person("s-2", "user-alice")
    w.store.get(REF, identity=a1)
    w.store.get(REF, identity=a2)
    assert w.pt.entity("s-1") == w.pt.entity("s-2")
    w.pt.drop("s-1")
    srv.sealed = True
    assert w.store.get(REF, identity=a2) == KEY


@pytest.mark.parametrize("claim", [{"token": "token-abc:rancher"}, {"scope": ["openid"]}])
def test_an_access_token_is_never_sent(w, srv, claim):
    """M4 (point 280) : un jeton d'accès de Rancher, même audience, n'est
    jamais présenté à OpenBao, ni à la connexion ni à la reconnexion."""
    alice = w.person("s-a", "user-alice")
    w.tokens["s-a"] = jwt_for("user-alice", **claim)
    with pytest.raises(ss.SecretError) as e:
        w.store.get(REF, identity=alice)
    assert e.value.code == "openbao-person-login-failed" and not srv.jwt_logins
    w.tokens["s-a"] = jwt_for("user-alice")
    w.store.get(REF, identity=alice)
    n = len(srv.jwt_logins)
    assert w.pt.renew("s-a", jwt_for("user-alice", **claim)) is False
    assert len(srv.jwt_logins) == n


# ---------------------------------------------------------------------------
# I6 : actions enfants d'une chaîne lancée par une personne
# ---------------------------------------------------------------------------
import ast  # noqa: E402


def _worker(fn):
    out = {}

    def go():
        try:
            out["v"] = fn()
        except Exception as e:                         # noqa: BLE001
            out["e"] = e
    t = threading.Thread(target=go)
    t.start()
    t.join()
    if "e" in out:
        raise out["e"]
    return out.get("v")


def _chain_run(person_launched):
    import types
    r = types.SimpleNamespace(secret_identity=None, person_launched=person_launched, events=[])
    r.emit = r.events.append
    return r


@pytest.mark.parametrize("launched,want", [(True, "late-service"), (False, None)])
def test_late_reads_of_a_person_launched_run_are_marked(console, launched, want):
    """Déroulé (ou action enfant) lancé par une personne : une lecture
    d'une classe personnelle par le service dans son fil est marquée
    `late-service` (étape et `secret_identity`), sans valeur ; lancé par un
    compte local, rien ne change."""
    run = _chain_run(launched)

    def worker():
        with wapp._late_reads(run):
            return ss._STORE.get("openbao:ssh-cluster/h1#private_key", identity=ss.SERVICE)
    assert _worker(worker) == "KEY-h1"
    assert run.secret_identity == want
    steps = [e for e in run.events if e.get("step_id") == "secrets"]
    assert bool(steps) is launched and "KEY-h1" not in json.dumps(run.events)


def test_chain_runs_and_children_carry_the_launcher_mark():
    """Les fils d'un déroulé et d'un défaire passent par `_late_reads`, et
    chaque action Terraform qu'ils lancent reçoit `late_service`."""
    src = (ROOT / "web" / "app.py").read_text()
    tree = ast.parse(src)
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for fn in ("_deploy_runner", "_undo_runner"):
        assert "_late_reads(run)" in ast.get_source_segment(src, funcs[fn]), fn
    launch = ast.get_source_segment(src, funcs["_tf_decl_launch"])
    assert "run.person_launched = bool(late_service)" in launch
    for fn in ("_deploy_start", "_undo_start"):
        assert "_person_launched()" in ast.get_source_segment(src, funcs[fn]), fn


def test_person_launched_only_for_a_person_with_a_token(console):
    assert as_who(open_session(), wapp._person_launched) is True
    assert as_who(None, wapp._person_launched) is False
    assert as_who(open_session(kind="direct"), wapp._person_launched) is False
    assert wapp._person_launched() is False                       # hors requête


def _chain_launches():
    tree = ast.parse((ROOT / "web" / "app.py").read_text())
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    out = []
    for fn in ("_deploy_terraform_phase", "_undo_phases"):
        for c in ast.walk(funcs[fn]):
            if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "_tf_decl_launch":
                out.append((fn, c.lineno, any(k.arg == "late_service" for k in c.keywords)))
    return out


def test_every_chain_launch_carries_the_launcher_mark():
    launches = _chain_launches()
    assert len(launches) >= 3
    assert [x for x in launches if not x[2]] == []


def test_decrypt_state_freezes_the_person_identity():
    src = (ROOT / "web" / "app.py").read_text()
    i = src.index('"decrypt-state", user=current_user()')
    assert "secret_identities=" in src[i:i + 500]
