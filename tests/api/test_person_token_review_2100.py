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
