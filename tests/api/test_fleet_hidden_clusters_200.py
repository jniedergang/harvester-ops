"""2.0.0, décision 93 : une vue de flotte ne nomme jamais un cluster que
Rancher cache à la personne.

Avant, /api/vms-all (et les vues de conteneurs, de GPU et de migrations de
tous les clusters) rendait un cluster caché par Rancher sous son nom, avec
l'état « unknown » : le menu des hôtes, lui, le taisait. Désormais ces vues
taisent le nom comme le menu et n'en disent que le nombre (`hidden_count`).

Deux identités, mêmes clusters : l'administrateur local voit les trois noms,
une session Rancher qui n'a pas accès à `secret-lab` ne lit ce nom nulle part
dans la réponse. Une identité déléguée (sans Rancher) dont le cluster refuse
la lecture voit toujours ce cluster, dit « denied », comme dans le menu."""

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402

CLUSTERS = ("alpha", "bravo", "secret-lab")
HIDDEN = "secret-lab"


def _vms(name, kc):
    if "deny" in str(kc):
        return {"error": "kubectl failed", "detail": "forbidden"}, 500, ["Error from server (Forbidden): x"]
    return {"cluster": name, "vms": [{"name": f"vm-{name}", "namespace": "default", "phase": "Running"}]}, 200, []


def _containers(name, kc):
    return {"cluster": name, "workloads": []}, 200, []


def _gpus(name, kc):
    return {"cluster": name, "units": [], "addon": True, "available": True}, 200, []


def _fk(name, kc):
    return {"cluster": name, "reachable": bool(kc), "forklift_ready": False,
            "cdi_importer_kind": None, "providers": [], "waves": []}


@pytest.fixture
def world(tmp_path, monkeypatch):
    cfg = {"clusters": [{"name": n, "kubeconfig": f"/k/{n}.yaml"} for n in CLUSTERS]}
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")       # mode ouvert
    monkeypatch.setattr(wapp, "load_config", lambda: cfg)
    monkeypatch.setattr(wapp, "load_config_ro", lambda: cfg)
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc, **k: True)
    monkeypatch.setattr(wapp, "_vms_collect", _vms)
    monkeypatch.setattr(wapp, "_containers_collect", _containers)
    monkeypatch.setattr(wapp, "_gpus_collect", _gpus)
    monkeypatch.setattr(wapp, "_fk_global_cluster", _fk)
    monkeypatch.setattr(wapp, "_FK_GLOBAL_CACHE", {})
    monkeypatch.setattr(wapp, "_vms_all_kvm_shared", lambda: ([], []))
    monkeypatch.setenv("HARVESTER_OPS_FOLDERS_DB", str(tmp_path / "folders.db"))
    return cfg


def as_admin(monkeypatch):
    """Administrateur local : kubeconfig de la console pour chaque cluster."""
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: f"/staged/{c}.yaml" if c in CLUSTERS else None)


def as_rancher_member(monkeypatch):
    """Session Rancher sans accès à `secret-lab` : le vrai _kubectl_for_cluster
    passe par _sso_kubeconfig puis _sso_cluster_state (seul ce dernier est
    simulé, comme dans test_hosts_routes_200)."""
    monkeypatch.setattr(wapp, "_sso_session",
                        lambda: types.SimpleNamespace(login="member", role="viewer", token="t"))
    monkeypatch.setattr(wapp, "_session_settings", lambda sess: {"origin": "config"})
    monkeypatch.setattr(wapp, "_sso_cluster_state",
                        lambda e: (None, "no-access") if e["name"] == HIDDEN else (f"/sso/{e['name']}.yaml", None))


def as_delegated_denied(monkeypatch):
    """Identité déléguée sans Rancher : le cluster `secret-lab` refuse la lecture
    (RBAC), il n'est pas caché : le menu des hôtes le montre « denied »."""
    monkeypatch.setattr(wapp, "_kubectl_for_cluster",
                        lambda c: f"/deny/{c}.yaml" if c == HIDDEN else f"/as/{c}.yaml")


IDENTITIES = {"admin": as_admin, "rancher-member": as_rancher_member, "delegated": as_delegated_denied}

# (route, clé de la liste des clusters, clé du nom dans une entrée)
ROUTES = [
    ("/api/vms-all", "clusters", "cluster"),
    ("/api/vms-all?kinds=harvester,kvm", "sources", "source"),
    ("/api/containers-all", "clusters", "cluster"),
    ("/api/gpus-all", "clusters", "cluster"),
    ("/api/forklift-global", "clusters", "cluster"),
]


@pytest.mark.parametrize("route,list_key,name_key", ROUTES)
@pytest.mark.parametrize("identity,names,hidden,hidden_state", [
    ("admin", ["alpha", "bravo", "secret-lab"], 0, None),
    ("rancher-member", ["alpha", "bravo"], 1, None),
    ("delegated", ["alpha", "bravo", "secret-lab"], 0, "denied"),
])
def test_a_fleet_view_never_names_a_cluster_rancher_hides(world, monkeypatch, route, list_key, name_key,
                                                         identity, names, hidden, hidden_state):
    IDENTITIES[identity](monkeypatch)
    with wapp.app.test_client() as c:
        r = c.get(route)
        assert r.status_code == 200, r.get_data(as_text=True)
        body, raw = r.get_json(), r.get_data(as_text=True)
    assert sorted(x[name_key] for x in body[list_key]) == names
    if route == "/api/vms-all":                     # réponse d'avant la 2.0.0, forme gardée
        assert "hidden_count" not in body
    else:
        assert body["hidden_count"] == hidden
    if hidden:
        assert HIDDEN not in raw                     # ni entrée, ni ligne, ni message
    if hidden_state and route.startswith("/api/vms-all"):
        st = {x[name_key]: x for x in body[list_key]}
        assert st[HIDDEN]["state"] == hidden_state


def test_the_vms_view_and_the_hosts_menu_name_the_same_clusters(world, monkeypatch):
    """Même règle que le menu des hôtes, pour la même session Rancher."""
    as_rancher_member(monkeypatch)
    monkeypatch.setattr(wapp, "_config_cluster_names", lambda: set(CLUSTERS))
    monkeypatch.setattr(wapp, "_kvm_hosts_menu", lambda: [])
    with wapp.app.test_client() as c:
        fleet = c.get("/api/vms-all?kinds=harvester,kvm").get_json()
        menu = c.get("/api/hosts").get_json()
    menu_names = sorted(h["name"] for h in menu["hosts"] if h["kind"] == "harvester")
    assert sorted(s["source"] for s in fleet["sources"]) == menu_names == ["alpha", "bravo"]


def test_the_vms_screen_shows_only_an_anonymous_count():
    """L'écran dit le nombre, jamais un nom : la chaîne n'a que {n}."""
    js = (ROOT / "web/static/js/all-vms.js").read_text()
    assert "data.hidden_count" in js and "tr('avm.cl.hidden', { n: data.hidden_count })" in js
    i18n = (ROOT / "web/static/js/i18n.js").read_text()
    lines = [ln for ln in i18n.splitlines() if "'avm.cl.hidden':" in ln]
    assert len(lines) == 5 and all("{n}" in ln and "{cluster}" not in ln for ln in lines)
