"""Prochaine version KVM, tâche 6 : les vues Stockage et Réseaux relisent dès
la fin d'une action.

Vu en réel sur node2 : la page relit le catalogue d'images (et les pools) à
l'événement `end` de l'action ; la console oubliait ses lectures partagées
APRÈS avoir fermé l'action, donc la relecture rendait l'ancienne réponse (une
image téléversée sans sa somme, un pool créé absent). Les vues doivent être
oubliées avant la fermeture, pour les actions du script comme pour l'envoi
d'une image."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_kvm_routes_196 import (  # noqa: E402,F401 : fixtures partagées
    _NoThread, client, env, only_run, wapp)
from test_kvm_routes_create import (  # noqa: E402,F401
    FakePopen, lib, staged, upload)


@pytest.fixture
def order(monkeypatch):
    """Ordre des oublis de lectures partagées, des générations avancées et
    des fermetures d'action."""
    seen = []
    real_close = wapp.ActionRun.close
    monkeypatch.setattr(wapp.READ_SHARE, "invalidate", lambda *a, **k: seen.append("invalidate"))
    monkeypatch.setattr(wapp._ko, "bump_generation", lambda *a, **k: seen.append("bump"))

    def close(self):
        seen.append("close")
        return real_close(self)
    monkeypatch.setattr(wapp.ActionRun, "close", close)
    return seen


@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/kvm/hosts/node2/pools/images/start", {}),
    ("post", "/api/kvm/hosts/node2/networks/default/autostart", {"enable": False}),
    ("delete", "/api/kvm/hosts/node2/volumes/images/a.qcow2", {"expect_delete": "b" * 64}),
    ("put", "/api/kvm/hosts/node2/image-pools", {"pools": ["images"]}),
])
def test_script_actions_forget_the_views_before_they_end(client, env, lib, monkeypatch, order, method, path, body):
    def runner(run, cmd, capture=False):
        run.status = "done"
        run.close()
    monkeypatch.setattr(wapp, "_vm_transfer_runner", runner)
    r = getattr(client, method)(path, json=body)
    assert r.status_code == 202, r.get_json()
    _NoThread.started[-1].target()
    first_close = order.index("close")
    assert "invalidate" in order[:first_close] and "bump" in order[:first_close], order


def test_an_image_send_forgets_the_views_before_it_ends(client, upload, order):
    r = client.post("/api/kvm/hosts/node2/images/upload?pool=images&name=a.img", data=b"x" * 1000)
    assert r.status_code == 202, r.get_json()
    del order[:]
    _NoThread.started[-1].target()
    assert only_run().status == "done"
    first_close = order.index("close")
    assert order[:first_close] == ["invalidate", "bump"], order
    assert staged(upload) == []


@pytest.mark.parametrize("closes,raises", [(True, False), (False, False), (False, True)])
def test_views_are_forgotten_once_per_action(client, env, lib, monkeypatch, order, closes, raises):
    """La fermeture oublie déjà les vues : `after` ne le refait pas. Un relais
    qui n'a pas fermé (interrompu, exception) laisse `after` les oublier."""
    def runner(run, cmd, capture=False):
        run.status = "done"
        if closes:
            run.close()
        if raises:
            raise RuntimeError("relay stopped")
    monkeypatch.setattr(wapp, "_vm_transfer_runner", runner)
    kvm = []
    monkeypatch.setattr(wapp.READ_SHARE, "invalidate",
                        lambda scope=None, *a, **k: kvm.append(scope) if scope == wapp.KVM_SCOPE else None)
    r = client.post("/api/kvm/hosts/node2/pools/images/start", json={})
    assert r.status_code == 202, r.get_json()
    del order[:], kvm[:]
    t = _NoThread.started[-1]
    if raises:
        with pytest.raises(RuntimeError):
            t.target()
    else:
        t.target()
    assert kvm == [wapp.KVM_SCOPE] and order.count("bump") == 1, (kvm, order)
