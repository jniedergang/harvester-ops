"""2.9.0, corrections de la revue des tâches 5 à 7 (première passe).

Chaque cas a été vu rouge avant sa correction. Mutations jouées (rapport) :
« bail par fil seulement » (I1), « hold_secrets ignoré » (I1),
« bail jamais rendu » (I1).
"""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_secret_migrate_290 import KC, appenv, env, ss, wapp  # noqa: E402,F401


def _in_request_thread(fn):
    """Joue `fn` dans un fil qui meurt aussitôt (fil werkzeug d'une requête)."""
    box = {}
    t = threading.Thread(target=lambda: box.update(v=fn()))
    t.start()
    t.join()
    return box["v"]


# ---------------------------------------------------------------------------
# I1 : bail porté par l'action, pas par le fil
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("how", ["created-in-request", "held-explicitly", "in-command"])
def test_copy_resolved_by_the_request_is_kept_for_the_action(appenv, how):
    """Le fil de la requête reçoit la copie partagée, crée l'action et meurt ;
    la copie expire : elle reste tant que l'action n'est pas finie, puis
    part avec elle."""
    w, st = appenv["wapp"], appenv["store"]
    ref = st.put("kubeconfig", "h1", "kubeconfig", KC)

    def request():
        if how == "created-in-request":
            p = st.shared_file(ref, "kubectl")
            run = w.ActionRun("lease-1", "capi-install:h1", "h1", [])
        else:                                            # copie résolue APRÈS la création de l'action
            run = w.ActionRun("lease-1", "capi-install:h1", "h1", [])
            p = st.shared_file(ref, "kubectl")
            run.hold_secrets(str(p)) if how == "held-explicitly" else run.hold_secrets("tool", f"--kubeconfig={p}")
        return p, run
    p, run = _in_request_thread(request)
    st.clear_cache()                                     # copie retirée (expirée, cache vidé)
    st.openbao.sweep()
    assert p.exists() and p.read_text() == KC            # l'action l'utilise encore
    run.close()
    assert not p.exists()


def test_a_finished_request_without_action_does_not_keep_the_copy(appenv):
    st = appenv["store"]
    ref = st.put("kubeconfig", "h1", "kubeconfig", KC)
    p = _in_request_thread(lambda: st.shared_file(ref, "kubectl"))
    st.clear_cache()
    st.openbao.sweep()
    assert not p.exists()


def test_capi_install_holds_its_kubeconfig_and_key(appenv):
    """api_capi_install résout kubeconfig et clé dans la requête : l'action
    les tient (lu dans la source, la route complète demande un cluster)."""
    import inspect
    src = inspect.getsource(appenv["wapp"].api_capi_install)
    assert "run.hold_secrets(kc, key_path)" in src
    assert src.index("run.hold_secrets(kc, key_path)") < src.index("threading.Thread(")


# ---------------------------------------------------------------------------
# I4 (décision 275) : copie hors ligne de la clé, outil harvester-secrets
# ---------------------------------------------------------------------------
import json  # noqa: E402
import os  # noqa: E402
import stat  # noqa: E402

from test_secret_migrate_290 import cli, hsc, migrate  # noqa: E402

KEYBYTES = bytes(range(32))


@pytest.fixture
def b1(env, monkeypatch):
    doc = json.loads((env["state"] / ss.SETTINGS_FILE).read_text())
    doc["mode"] = "b1"
    (env["state"] / ss.SETTINGS_FILE).write_text(json.dumps(doc))
    calls = []
    etc = Path(os.environ["HARVESTER_OPS_OPENBAO_DIR"])

    def fake_agent(req, timeout=None):
        calls.append(dict(req))
        if req["op"] == "export-unseal-key":
            (etc / "unseal-export.key").write_bytes(KEYBYTES)
            return {"state": "done", "info": {"key_id": "hops-20261108-1"}}
        if req["op"] == "export-unseal-key-done":
            (etc / "unseal-export.key").unlink(missing_ok=True)
        return {"state": "done", "info": {}}
    monkeypatch.setattr(hsc, "run_agent", fake_agent)
    return dict(env, calls=calls, etc=etc)


def test_offline_copy_export_to_a_file(b1, capsys, tmp_path):
    dst = tmp_path / "usb" / "unseal.key"
    dst.parent.mkdir()
    rc, doc, err = cli(["offline-copy", "export", "--to", str(dst)], capsys)
    assert rc == 0 and doc == {"ok": True, "key_id": "hops-20261108-1", "copied": True}
    assert dst.read_bytes() == KEYBYTES and stat.S_IMODE(dst.stat().st_mode) == 0o600
    assert [c["op"] for c in b1["calls"]] == ["export-unseal-key", "export-unseal-key-done"]
    assert not (b1["etc"] / "unseal-export.key").exists()
    assert KEYBYTES.hex() not in err + json.dumps(doc)
    # jamais par-dessus un fichier existant
    rc, doc, _ = cli(["offline-copy", "export", "--to", str(dst)], capsys)
    assert rc == 1 and doc["code"] == "offline-copy-write" and not (b1["etc"] / "unseal-export.key").exists()


def test_offline_copy_ack_and_migration_gate(b1, capsys):
    doc = json.loads((b1["state"] / ss.SETTINGS_FILE).read_text())
    doc["offline_copy"] = {"pending": True, "since": 1}
    (b1["state"] / ss.SETTINGS_FILE).write_text(json.dumps(doc))
    rc, out, _ = migrate("rancher-oidc", capsys)
    assert rc == 2 and out["code"] == "offline-copy-ack-required"
    assert cli(["offline-copy", "ack"], capsys)[0] == 2                        # --key-id exigé
    rc, out, _ = cli(["offline-copy", "ack", "--key-id", "hops-20261108-1"], capsys)
    assert rc == 0 and b1["calls"][-1] == {"op": "ack-offline-copy", "key_id": "hops-20261108-1"}
    oc_ = json.loads((b1["state"] / ss.SETTINGS_FILE).read_text())["offline_copy"]
    assert oc_["pending"] is False and oc_["by"] == "alice"
    assert migrate("rancher-oidc", capsys)[0] == 0


def test_offline_copy_is_b1_only(env, capsys):
    rc, doc, _ = cli(["offline-copy", "export"], capsys)
    assert rc == 2 and doc["code"] == "b1-only"


# ---------------------------------------------------------------------------
# I4 (décision 275) : routes de la console, actions suivies, un seul téléchargement
# ---------------------------------------------------------------------------
@pytest.fixture
def b1app(appenv, monkeypatch):
    doc = json.loads((appenv["state"] / ss.SETTINGS_FILE).read_text())
    doc["mode"] = "b1"
    (appenv["state"] / ss.SETTINGS_FILE).write_text(json.dumps(doc))
    w = appenv["wapp"]
    launched = []

    def fake_cli(cluster, label, cmd, tool, **kw):
        launched.append((label, cmd[cmd.index("offline-copy"):]))
        return w.ActionRun(f"act{len(launched)}", label, "", []), None
    monkeypatch.setattr(w, "_cli_action", fake_cli)
    return dict(appenv, launched=launched, cid=doc["console_id"])


@pytest.mark.parametrize("path,method", [("/api/secrets/offline-copy/export", "POST"),
                                         ("/api/secrets/offline-copy/download", "GET"),
                                         ("/api/secrets/offline-copy/ack", "POST")])
def test_offline_copy_routes_are_admin_only(wapp, path, method):
    assert wapp.required_role_for(path, method) == "admin"


def test_offline_copy_export_needs_the_typed_console_id(b1app):
    w = b1app["wapp"]
    with w.app.test_client() as c:
        r = c.post("/api/secrets/offline-copy/export", json={"confirm": "yes"})
        assert r.status_code == 400 and r.get_json()["code"] == "confirmation-required" and not b1app["launched"]
        r = c.post("/api/secrets/offline-copy/export", json={"confirm": b1app["cid"]})
    assert r.status_code == 202 and r.get_json()["action_id"]
    assert b1app["launched"] == [("secrets:offline-copy-export", ["offline-copy", "export"])]


def test_offline_copy_download_once(b1app):
    w = b1app["wapp"]
    etc = Path(os.environ["HARVESTER_OPS_OPENBAO_DIR"])
    (etc / "unseal-export.key").write_bytes(KEYBYTES)
    run = w.ActionRun("exp1", "secrets:offline-copy-export", "", [])
    run.status, run.result = "done", {"output": {"ok": True, "key_id": "hops-20261108-1"}}
    with w.ACTIONS_LOCK:
        w.ACTIONS["exp1"] = run
    try:
        with w.app.test_client() as c:
            r = c.get("/api/secrets/offline-copy/download?action=exp1")
            assert r.status_code == 200 and r.data == KEYBYTES
            assert r.headers["Cache-Control"] == "no-store" and "hops-20261108-1" in r.headers["Content-Disposition"]
            assert b1app["launched"] == [("secrets:offline-copy-done", ["offline-copy", "done"])]
            r = c.get("/api/secrets/offline-copy/download?action=exp1")
            assert r.status_code == 410 and r.get_json()["code"] == "offline-copy-downloaded"
            assert c.get("/api/secrets/offline-copy/download?action=nope").status_code == 404
    finally:
        with w.ACTIONS_LOCK:
            w.ACTIONS.pop("exp1", None)
    # la clé n'est dans aucune action ni étape
    blob = json.dumps([list(r.events) for r in w.ACTIONS.values()], default=str)
    assert KEYBYTES.hex() not in blob


def test_offline_copy_ack_route(b1app):
    w = b1app["wapp"]
    with w.app.test_client() as c:
        assert c.post("/api/secrets/offline-copy/ack", json={"key_id": "../x"}).status_code == 400
        r = c.post("/api/secrets/offline-copy/ack", json={"key_id": "hops-20261108-1"})
    assert r.status_code == 202
    assert b1app["launched"] == [("secrets:offline-copy-ack:hops-20261108-1",
                                  ["offline-copy", "ack", "--key-id", "hops-20261108-1"])]


def test_offline_copy_routes_refused_outside_b1(appenv):
    w = appenv["wapp"]
    with w.app.test_client() as c:
        r = c.post("/api/secrets/offline-copy/export", json={"confirm": "x"})
    assert r.status_code == 409 and r.get_json()["code"] == "b1-only"


# ---------------------------------------------------------------------------
# M4 : redirection suivie seulement vers la même origine (le jeton suit)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("old,new,followed", [
    ("https://bao.example:8200/v1/a", "https://bao.example:8200/v1/b", True),
    ("https://bao.example:8200/v1/a", "https://evil.example:8200/v1/a", False),   # autre hôte, même en https
    ("https://bao.example:8200/v1/a", "https://bao.example:8443/v1/a", False),
    ("http://127.0.0.1:8210/v1/a", "http://127.0.0.1:8210/v1/b", True),
    ("https://bao.example:8200/v1/a", "http://bao.example:8200/v1/a", False),
])
def test_redirect_only_to_the_same_origin(old, new, followed):
    import urllib.request
    import openbao_client as oc
    req = urllib.request.Request(old, headers={"X-Vault-Token": "s.x"})
    got = oc._SafeRedirect().redirect_request(req, None, 307, "", {}, new)
    assert (got is not None) is followed
