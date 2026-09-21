"""v1.41.0 : la console partagée, côté application.

`vnc_mux` fait le partage ; l'application décide QUI peut rejoindre et
POURQUOI une console s'est fermée :

  * chaque navigateur est vérifié par le cluster sous sa propre identité,
    même s'il rejoint une console ouverte par quelqu'un d'autre, et la
    connexion vers KubeVirt porte l'usurpation (elle ne passe pas par
    kubectl, et ouvrait jusqu'ici la console avec les pleins pouvoirs) ;
  * une fermeture par KubeVirt est classée : VM redémarrée ou arrêtée (la
    console se reconnecte), ou écran pris par un AUTRE client (elle
    s'arrête, sinon les deux se reprendraient l'écran en boucle).
"""

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402
import vnc_mux  # noqa: E402


class Run:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/kc")
    wapp.app.config["TESTING"] = True
    return wapp.app.test_client()


@pytest.fixture(autouse=True)
def _clean():
    wapp._vnc_last_close.clear()
    wapp._vnc_tickets.clear()
    yield
    wapp._vnc_last_close.clear()
    wapp._vnc_tickets.clear()


# ---------------------------------------------------------------------------
# Qui peut rejoindre
# ---------------------------------------------------------------------------

def test_the_ticket_remembers_who_asked_and_which_instance(client, monkeypatch):
    monkeypatch.setattr(wapp.subprocess, "run", lambda *a, **k: Run(0, "Running uid-123"))
    monkeypatch.setattr(wapp, "current_user", lambda: "alice")
    r = client.post("/api/vm/c1/ns1/vm1/console-ticket")
    assert r.status_code == 200
    entry = wapp._vnc_take_ticket(r.get_json()["ticket"], "c1", "ns1", "vm1")
    assert entry["user"] == "alice" and entry["uid"] == "uid-123"
    assert entry["identity"] is None


def test_a_delegated_identity_is_checked_by_the_cluster(client, monkeypatch):
    """Rejoindre la console de quelqu'un d'autre ne dispense pas du droit :
    c'est le cluster qui dit oui ou non, pour CETTE identité."""
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if "can-i" in cmd:
            return Run(1, "no\n")
        return Run(0, "Running uid-1")
    monkeypatch.setattr(wapp.subprocess, "run", run)
    monkeypatch.setattr(wapp, "current_cluster_identity",
                        lambda: {"user": "bob", "groups": ["ops"]})
    r = client.post("/api/vm/c1/ns1/vm1/console-ticket")
    assert r.status_code == 403
    assert not wapp._vnc_tickets, "un ticket a été émis malgré le refus"
    can = next(c for c in calls if "can-i" in c)
    assert "--subresource=vnc" in can and "virtualmachineinstances" in can


def test_an_allowed_identity_gets_a_ticket_carrying_it(client, monkeypatch):
    monkeypatch.setattr(wapp.subprocess, "run",
                        lambda cmd, **k: Run(0, "yes\n") if "can-i" in cmd
                        else Run(0, "Running uid-1"))
    ident = {"user": "bob", "groups": ["ops"]}
    monkeypatch.setattr(wapp, "current_cluster_identity", lambda: ident)
    r = client.post("/api/vm/c1/ns1/vm1/console-ticket")
    assert r.status_code == 200
    entry = wapp._vnc_take_ticket(r.get_json()["ticket"], "c1", "ns1", "vm1")
    assert entry["identity"] == ident


def test_the_upstream_connection_carries_the_impersonation():
    """Le websocket ne passe pas par kubectl : sans ces en-têtes, la console
    s'ouvrait avec l'identité du toolkit, quelle que soit la délégation."""
    h = wapp._vnc_upstream_headers("tok", {"user": "bob", "groups": ["ops", "dev"]})
    assert ("Authorization", "Bearer tok") in h
    assert ("Impersonate-User", "bob") in h
    assert ("Impersonate-Group", "ops") in h and ("Impersonate-Group", "dev") in h
    assert wapp._vnc_upstream_headers(None, None) == []


def test_the_relay_goes_through_the_shared_console():
    src = (ROOT / "web" / "app.py").read_text()
    body = src.split("def ws_vnc(", 1)[1].split("\n# ====", 1)[0]
    assert "vnc_mux.attach(" in body
    assert "_vnc_upstream_headers(" in body
    # Plus de relais octet à octet par navigateur.
    assert "_pump_down" not in src


# ---------------------------------------------------------------------------
# Pourquoi elle s'est fermée
# ---------------------------------------------------------------------------

@pytest.fixture()
def cluster_cfg(monkeypatch):
    monkeypatch.setattr(wapp, "load_config",
                        lambda: {"clusters": [{"name": "c1", "kubeconfig": "/kc"}]})


@pytest.mark.parametrize("out,rc,reason", [
    ("Running uid-1", 0, "taken"),          # même instance, toujours là
    ("Running uid-2", 0, "vm-restarted"),   # recréée : réinitialisation
    ("Failed uid-1", 0, "vm-restarted"),
    ("", 1, "vm-stopped"),                  # plus d'instance
])
def test_a_loss_is_told_apart(cluster_cfg, monkeypatch, out, rc, reason):
    monkeypatch.setattr(wapp.subprocess, "run", lambda *a, **k: Run(rc, out))
    assert wapp._vnc_classify_loss("c1", "ns1", "vm1", "uid-1") == reason
    assert wapp._vnc_last_close[("c1", "ns1", "vm1")]["reason"] == reason


def test_a_loss_without_a_known_instance_is_never_called_taken(cluster_cfg, monkeypatch):
    """Sans uid de départ on ne peut pas prouver que c'est la même VM : on
    ne bloque pas la reconnexion sur une supposition."""
    monkeypatch.setattr(wapp.subprocess, "run", lambda *a, **k: Run(0, "Running uid-1"))
    assert wapp._vnc_classify_loss("c1", "ns1", "vm1", None) == "vm-restarted"


def test_the_status_says_who_watches_and_why_it_closed(client, monkeypatch):
    monkeypatch.setattr(vnc_mux, "sessions",
                        lambda: {("c1", "ns1", "vm1"): ["alice", "bob"]})
    wapp._vnc_last_close[("c1", "ns1", "vm1")] = {"reason": "taken", "at": time.time()}
    d = client.get("/api/vm/c1/ns1/vm1/console-status").get_json()
    assert d["count"] == 2 and d["viewers"] == ["alice", "bob"]
    assert d["last_close"]["reason"] == "taken"


def test_an_old_reason_is_forgotten(client, monkeypatch):
    monkeypatch.setattr(vnc_mux, "sessions", lambda: {})
    wapp._vnc_last_close[("c1", "ns1", "vm1")] = {
        "reason": "taken", "at": time.time() - wapp._VNC_CLOSE_MEMORY - 1}
    d = client.get("/api/vm/c1/ns1/vm1/console-status").get_json()
    assert d["count"] == 0 and d["last_close"] is None


def test_the_session_cap_counts_browsers_across_vms(client, monkeypatch):
    monkeypatch.setattr(vnc_mux, "sessions", lambda: {
        ("c1", "a", "x"): ["u"] * 5, ("c1", "b", "y"): ["v"] * 3})
    r = client.post("/api/vm/c1/ns1/vm1/console-ticket")
    assert r.status_code == 429
