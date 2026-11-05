"""Revue 2.5.0 (D2) : l'ancien retrait de la pile Cluster API supprime des
namespaces et des images, pas les objets CAPIProvider. Sur une pile gérée
par Rancher Turtles il est refusé par le serveur, et son bouton est grisé
avec la raison."""

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))

import app as wapp  # noqa: E402

JS = (ROOT / "web" / "static" / "js" / "capi.js").read_text()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    wapp.app.config["TESTING"] = True
    kc = tmp_path / "kc"
    kc.write_text("apiVersion: v1\n")
    monkeypatch.setattr(wapp, "current_role", lambda: "admin")
    monkeypatch.setattr(wapp, "current_cluster_identity", lambda: None)
    monkeypatch.setattr(wapp, "load_config", lambda: {"clusters": [{"name": "harvlab", "kubeconfig": str(kc)}]})
    monkeypatch.setattr(wapp, "_identity_kubeconfig", lambda k, ident: k)
    monkeypatch.setattr(wapp, "_capi_base", lambda c, sub: (["harvester-capi", sub], None))
    started = []
    monkeypatch.setattr(wapp, "_capi_uninstall_runner", lambda *a, **kw: started.append(a))
    yield wapp.app.test_client(), started, monkeypatch


@pytest.mark.parametrize("stack,status", [
    ({"turtles": True, "core": {"ready": True}, "ready": True}, 409),
    ({"turtles": True, "core": None, "ready": False}, 409),     # Turtles sans cœur : toujours à lui
    ({"turtles": False, "ready": False}, 201),                  # pile d'avant Turtles : retrait permis
])
def test_legacy_uninstall_is_refused_on_a_turtles_stack(client, stack, status):
    c, started, mp = client
    mp.setattr(wapp, "_capi_run_json", lambda cmd, spec=None, timeout=180: ((dict(stack), 0), None))
    r = c.post("/api/capi/harvlab/uninstall", json={"dry_run": True})
    assert r.status_code == status, r.get_json()
    if status == 409:
        assert r.get_json()["code"] == "turtles-stack" and started == []
    else:
        for _ in range(100):
            if started:
                break
            time.sleep(0.01)
        assert started


def test_the_uninstall_button_is_greyed_with_its_reason_on_a_turtles_stack():
    assert "else render(out, d, stack);" in JS
    assert "const turtles = !!(stack && stack.turtles);" in JS
    btn = JS.split('id="btn-capi-uninstall"', 1)[1].split(">", 1)[0]
    assert "${turtles ? 'disabled' : ''}" in btn
    assert "tr('capi.tip.uninstallTurtles')" in JS.split('id="btn-capi-uninstall"', 1)[1].split("</button>", 1)[0]
