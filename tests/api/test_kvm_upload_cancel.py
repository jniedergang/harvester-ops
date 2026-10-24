"""Prochaine version KVM, tâche 6 (revue) : annulation d'un téléversement.

- un corps coupé avant Content-Length (navigateur fermé) finit `cancelled`
  (étape `receive` sautée, « interrupted by the browser »), jamais `error` ;
- une annulation par le dock pendant la réception : étape `cancelled` ;
- le jeton tiré par la page désigne l'action AVANT la réponse 202 :
  `POST /api/kvm/uploads/<jeton>/cancel` (même personne, opérateur), valable
  pendant la réception et l'envoi seulement ;
- course après le dernier octet : l'annulation arrive pendant le contrôle du
  fichier reçu, ou entre la réponse 202 et l'envoi, ou pendant l'envoi ;
  l'action dit l'issue réelle (rien envoyé, volume partiel défait, ou fini).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_kvm_routes_196 import (  # noqa: E402,F401 : fixtures partagées
    _NoThread, as_user, client, env, only_run, wapp)
from test_kvm_routes_create import FakePopen, _ShortStream, staged, upload  # noqa: E402,F401

TOKEN = "0123456789abcdef0123456789abcdef"
URL = "/api/kvm/hosts/node2/images/upload?pool=images&name=a.img"


def steps(run):
    return [(e["step_id"], e["status"], e.get("message")) for e in run.events if e.get("type") == "step"]


@pytest.fixture(autouse=True)
def _tokens():
    with wapp._KVM_UPLOAD_LOCK:
        saved = dict(wapp._KVM_UPLOAD_TOKENS)
        wapp._KVM_UPLOAD_TOKENS.clear()
    yield
    with wapp._KVM_UPLOAD_LOCK:
        wapp._KVM_UPLOAD_TOKENS.clear()
        wapp._KVM_UPLOAD_TOKENS.update(saved)


class _Hook(_ShortStream):
    """Corps lu par petits blocs ; `during(n)` appelé avant chaque lecture."""

    def __init__(self, data, during):
        super().__init__(data)
        self.during, self.n = during, 0

    def read(self, n):
        self.n += 1
        self.during(self.n)
        return super().read(min(n, 10))


def post_stream(c, stream, length, token=TOKEN, path=URL):
    q = f"&token={token}" if token else ""
    return c.post(path + q, environ_overrides={"CONTENT_LENGTH": str(length), "wsgi.input": stream},
                  content_type="application/octet-stream")


# --- interruption et annulation pendant la réception -----------------------------------------

@pytest.mark.parametrize("how,step_status,message", [
    ("browser", "skipped", "interrupted by the browser, nothing kept"),
    ("dock", "cancelled", "cancelled, nothing kept"),
    ("token", "cancelled", "cancelled, nothing kept"),
    # la page annule par le jeton puis interrompt : la lecture en cours rend
    # la fin du corps, l'annulation demandée prime sur l'interruption
    ("token-then-abort", "cancelled", "cancelled, nothing kept"),
])
def test_receive_ends_cancelled_never_error(client, upload, how, step_status, message):
    def during(n):
        if n != (3 if how == "token-then-abort" else 2):
            return
        if how == "dock":
            assert client.delete(f"/api/action/{only_run().id}").status_code == 200
        elif how.startswith("token"):
            r = client.post(f"/api/kvm/uploads/{TOKEN}/cancel")
            assert r.status_code == 202 and r.get_json()["phase"] == "receive"
    # corps coupé : à 30 (navigateur), à 20 (annulé puis interrompu : 3e lecture vide)
    body = b"x" * {"browser": 30, "token-then-abort": 20}.get(how, 100)
    r = post_stream(client, _Hook(body, during), 100)
    assert r.status_code == 409, r.get_json()
    d = r.get_json()
    assert d["kind"] == "cancelled" and d["message"] == message and d["action_id"]
    run = only_run()
    assert run.status == "cancelled" and run.exit_code == 3 and run.error_summary == message
    assert run.result["output"]["kind"] == "cancelled"
    assert ("receive", step_status, message) in steps(run)
    assert not any(s[1] == "error" for s in steps(run))
    assert staged(upload) == [] and _NoThread.started == [] and FakePopen.calls == []
    assert wapp._KVM_UPLOAD_TOKENS == {}                    # jeton oublié à la fin


# --- le jeton -------------------------------------------------------------------------------

@pytest.mark.parametrize("token,code", [
    ("ABCDEF0123456789abcdef0123456789", 400), ("short", 400), ("0" * 33, 400), ("g" * 32, 400)])
def test_bad_tokens_are_refused_before_reading(client, upload, token, code):
    r = client.post(f"{URL}&token={token}", data=b"abc")
    assert r.status_code == code and wapp.ACTIONS == {} and staged(upload) == []
    assert client.post(f"/api/kvm/uploads/{token}/cancel").status_code in (400, 404)


def test_a_token_in_use_is_refused(client, upload):
    def during(n):
        if n == 1:
            r = client.post(f"{URL}&token={TOKEN}", data=b"abc")
            assert r.status_code == 409 and "already in use" in r.get_json()["error"]
    r = post_stream(client, _Hook(b"x" * 20, during), 20)
    assert r.status_code == 202 and len(wapp.ACTIONS) == 1


@pytest.mark.parametrize("state", ["unknown", "ended", "reserved"])
def test_cancel_outside_an_active_upload_is_404(client, upload, state):
    if state == "ended":
        r = client.post(f"{URL}&token={TOKEN}", data=b"abc")
        _NoThread.started[-1].target()                     # envoi fini
        assert only_run().status == "done"
    elif state == "reserved":
        with wapp._KVM_UPLOAD_LOCK:                         # action pas encore créée
            wapp._KVM_UPLOAD_TOKENS[TOKEN] = None
    r = client.post(f"/api/kvm/uploads/{TOKEN}/cancel")
    assert r.status_code == 404 and r.get_json()["reason"] == "not-active"


@pytest.mark.parametrize("user,code", [("alice", 202), ("bob", 403), ("curieux", 403)])
def test_only_the_uploader_with_the_operator_role_cancels(as_user, upload, monkeypatch, user, code):
    roles = wapp.ROLES_PATH
    roles.write_text(roles.read_text() + "  bob: operator\n")
    monkeypatch.setattr(wapp, "_roles_cache", {"mtime": None, "data": None})
    r = as_user("alice", "POST", f"{URL}&token={TOKEN}", data=b"abc")
    assert r.status_code == 202
    run = only_run()
    r = as_user(user, "POST", f"/api/kvm/uploads/{TOKEN}/cancel")
    assert r.status_code == code, r.get_json()
    assert run.cancel_requested is (code == 202)
    if user == "bob":
        assert r.get_json()["reason"] == "not-yours"


# --- course après le dernier octet -----------------------------------------------------------

def test_cancel_during_the_check_after_the_last_byte_sends_nothing(client, upload, monkeypatch):
    real = wapp._kvm_check_received

    def check(path, sha_expected, sha_got):
        # tous les octets sont arrivés ; la page annule par le jeton
        r = client.post(f"/api/kvm/uploads/{TOKEN}/cancel")
        assert r.status_code == 202
        return real(path, sha_expected, sha_got)
    monkeypatch.setattr(wapp, "_kvm_check_received", check)
    r = client.post(f"{URL}&token={TOKEN}", data=b"x" * 50)
    # contrôle passé, la marque est vue avant l'envoi : rien n'est envoyé
    assert r.status_code == 202
    _NoThread.started[-1].target()
    run = only_run()
    assert run.status == "cancelled" and run.exit_code == 3 and FakePopen.calls == []
    assert ("send", "cancelled", "cancelled before sending: nothing sent") in steps(run)
    assert run.result["output"]["kind"] == "cancelled" and staged(upload) == []


def test_cancel_after_the_202_before_the_send_starts(client, upload):
    r = client.post(f"{URL}&token={TOKEN}&hosts=ghost", data=b"x" * 50)
    assert r.status_code == 202
    c = client.post(f"/api/kvm/uploads/{TOKEN}/cancel")
    assert c.status_code == 202 and c.get_json()["phase"] == "send"
    run = only_run()
    assert run.status == "running"                         # attend la fin réelle
    _NoThread.started[-1].target()
    assert run.status == "cancelled" and FakePopen.calls == [] and staged(upload) == []


class _SlowPopen(FakePopen):
    """Envoi en cours : `poll` dit vivant tant que `terminate` n'est pas venu ;
    le script défait alors le volume partiel et sort en 3."""
    live = []

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.terminated = False
        _SlowPopen.live.append(self)

    def poll(self):
        return 3 if self.terminated else None

    def terminate(self):
        self.terminated = True
        self.rc = 3


def test_cancel_while_sending_waits_for_the_rollback(client, upload, monkeypatch):
    monkeypatch.setattr(wapp, "_KVM_POPEN", _SlowPopen)
    FakePopen.plan["node2"] = (0, {"ok": False, "kind": "cancelled",
                                   "error": "upload cancelled (SIGTERM): the partial volume was deleted"}, [])
    r = client.post(f"{URL}&token={TOKEN}&hosts=ghost", data=b"x" * 50)
    assert r.status_code == 202
    run = only_run()
    seen = {}
    real_cli = wapp._kvm_run_cli

    def cli(run_, cmd, prefix=""):
        # le script tourne (run.proc posé par le vrai relais) : annulation par le jeton
        orig = wapp._KVM_POPEN

        def popen(*a, **k):
            p = orig(*a, **k)
            run_.proc = p
            c = client.post(f"/api/kvm/uploads/{TOKEN}/cancel")
            seen["code"] = c.status_code
            seen["status_during"] = run_.status
            return p
        monkeypatch.setattr(wapp, "_KVM_POPEN", popen)
        try:
            return real_cli(run_, cmd, prefix)
        finally:
            monkeypatch.setattr(wapp, "_KVM_POPEN", orig)
    monkeypatch.setattr(wapp, "_kvm_run_cli", cli)
    _NoThread.started[-1].target()
    assert seen == {"code": 202, "status_during": "running"}     # pas fini avant le retour arrière
    assert _SlowPopen.live and _SlowPopen.live[-1].terminated
    assert run.status == "cancelled" and len(FakePopen.calls) == 1          # ghost ne reçoit rien
    assert "partial volume was deleted" in run.error_summary


def test_cancel_too_late_reports_the_completed_send(client, upload, monkeypatch):
    """Envoi fini avant que l'annulation n'arrive : l'action dit `done`."""
    r = client.post(f"{URL}&token={TOKEN}", data=b"x" * 50)
    assert r.status_code == 202
    _NoThread.started[-1].target()
    run = only_run()
    assert run.status == "done"
    c = client.post(f"/api/kvm/uploads/{TOKEN}/cancel")
    assert c.status_code == 404 and run.status == "done"
    assert json.dumps(run.result["output"]).count('"ok": true') >= 1


def test_cancel_before_any_host_says_nothing_was_sent(client, upload):
    r = client.post(f"{URL}&token={TOKEN}", data=b"x" * 50)
    assert r.status_code == 202
    client.post(f"/api/kvm/uploads/{TOKEN}/cancel")
    _NoThread.started[-1].target()
    run = only_run()
    assert run.error_summary == "cancelled before sending: nothing sent"
    assert run.result["output"]["hosts"] == []
