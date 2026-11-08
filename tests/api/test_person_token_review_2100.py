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
