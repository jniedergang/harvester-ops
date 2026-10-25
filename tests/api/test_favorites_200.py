"""2.0.0 : les favoris de chacun (PUT/DELETE /api/me/favorites/<kind>/<host>).

Une préférence personnelle (une étoile sur un hôte du menu), gardée dans la
base de la console (folders.db) et retrouvée sur un autre poste. Lecteur
compris, comme son propre mot de passe ; ni action suivie ni ligne de
commande (décision 5 du 25/10/2026)."""

import base64
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import app as wapp  # noqa: E402
import folders_db as fdb  # noqa: E402
import kvm_decl as kd  # noqa: E402
import read_share as rsh  # noqa: E402


@pytest.fixture
def world(monkeypatch, tmp_path):
    state = Path(tempfile.mkdtemp(prefix="fav-", dir="/tmp"))
    kd.write(state, {"name": "node2", "address": "10.0.0.9", "port": 22, "user": "ju",
                     "ssh_key": kd.key_rel("node2"), "known_hosts": kd.known_hosts_rel("node2"), "labels": {}})
    cfg = {"clusters": [{"name": n, "kubeconfig": f"/k/{n}.yaml"} for n in ("harv1", "Lab_1")]}
    monkeypatch.setattr(wapp, "load_config", lambda: cfg)
    monkeypatch.setattr(wapp, "load_config_ro", lambda: cfg)
    monkeypatch.setattr(wapp, "_config_cluster_names", lambda: {"harv1"})
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: f"/staged/{c}.yaml")
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc, **k: True)
    monkeypatch.setattr(wapp, "_state_dir", lambda: state)
    monkeypatch.setenv("HARVESTER_OPS_FOLDERS_DB", str(tmp_path / "folders.db"))
    monkeypatch.setattr(wapp, "READ_SHARE", rsh.ReadShare())
    monkeypatch.setattr(wapp, "READ_SHARE_ENABLED", True)
    (tmp_path / "htpasswd").write_text("")
    roles = tmp_path / "roles.yaml"
    roles.write_text("default_role: viewer\nusers:\n  patronne: admin\n  alice: operator\n  curieux: viewer\n")
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")
    monkeypatch.setattr(wapp, "ROLES_PATH", roles)
    monkeypatch.setattr(wapp, "_roles_cache", {"mtime": None, "data": None})
    monkeypatch.setattr(wapp, "check_auth", lambda u, p: True)
    saved = dict(wapp.ACTIONS)
    wapp.ACTIONS.clear()
    yield {"tmp": tmp_path}
    wapp.ACTIONS.clear()
    wapp.ACTIONS.update(saved)
    import shutil
    shutil.rmtree(state, ignore_errors=True)


def call(user, method, path):
    token = base64.b64encode(f"{user}:x".encode()).decode()
    with wapp.app.test_client() as c:
        return c.open(path, method=method, headers={"Authorization": f"Basic {token}"})


def favs(user):
    d = call(user, "GET", "/api/hosts").get_json()
    return sorted((h["kind"], h["name"]) for h in d["hosts"] if h["favorite"])


@pytest.mark.parametrize("user", ["curieux", "alice", "patronne"])
def test_add_then_remove_for_every_role(world, user):
    r = call(user, "PUT", "/api/me/favorites/harvester/harv1")
    assert r.status_code == 200, r.get_json()
    assert r.get_json() == {"ok": True, "kind": "harvester", "name": "harv1", "favorite": True, "changed": True}
    assert favs(user) == [("harvester", "harv1")]
    r = call(user, "DELETE", "/api/me/favorites/harvester/harv1")
    assert r.get_json()["changed"] is True and r.get_json()["favorite"] is False
    assert favs(user) == []
    # aucune action suivie : une préférence de la personne (décision 5)
    assert wapp.ACTIONS == {}


def test_a_duplicate_is_idempotent_and_a_missing_removal_harmless(world):
    assert call("alice", "PUT", "/api/me/favorites/kvm/node2").get_json()["changed"] is True
    again = call("alice", "PUT", "/api/me/favorites/kvm/node2")
    assert again.status_code == 200 and again.get_json()["changed"] is False
    conn = fdb.connect(wapp._folders_db_path())
    assert conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0] == 1
    conn.close()
    call("alice", "DELETE", "/api/me/favorites/kvm/node2")
    gone = call("alice", "DELETE", "/api/me/favorites/kvm/node2")
    assert gone.status_code == 200 and gone.get_json()["changed"] is False


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
@pytest.mark.parametrize("path,code,error", [
    ("/api/me/favorites/xen/harv1", 400, "unknown host kind"),
    ("/api/me/favorites/vm/harv1", 400, "unknown host kind"),
    ("/api/me/favorites/harvester/-bad", 400, "invalid host name"),
    ("/api/me/favorites/harvester/" + "a" * 62, 400, "invalid host name"),
    ("/api/me/favorites/kvm/bad%20name", 400, "invalid host name"),
    ("/api/me/favorites/harvester/removed", 404, "host removed is not declared"),
    ("/api/me/favorites/kvm/ghost", 404, "host ghost is not declared"),
    # une famille ne vaut pas pour l'autre : node2 n'est pas un cluster
    ("/api/me/favorites/harvester/node2", 404, "host node2 is not declared"),
])
def test_refusals_write_nothing(world, method, path, code, error):
    r = call("alice", method, path)
    assert r.status_code == code and r.get_json()["error"] == error
    conn = fdb.connect(wapp._folders_db_path())
    assert conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0] == 0
    conn.close()


def test_a_declared_name_outside_rfc1123_is_accepted(world):
    assert call("alice", "PUT", "/api/me/favorites/harvester/Lab_1").status_code == 200
    assert favs("alice") == [("harvester", "Lab_1")]


def test_two_people_never_see_each_others_stars(world):
    call("alice", "PUT", "/api/me/favorites/harvester/harv1")
    call("curieux", "PUT", "/api/me/favorites/kvm/node2")
    assert favs("alice") == [("harvester", "harv1")]
    assert favs("curieux") == [("kvm", "node2")]
    assert favs("patronne") == []
    call("curieux", "DELETE", "/api/me/favorites/harvester/harv1")       # pas la sienne
    assert favs("alice") == [("harvester", "harv1")]


def test_a_star_shows_at_once_in_the_hosts_list(world):
    """La liste des hôtes est partagée 3 s : poser une étoile la relit."""
    assert favs("alice") == []
    call("alice", "PUT", "/api/me/favorites/harvester/harv1")
    assert favs("alice") == [("harvester", "harv1")]


def test_a_star_does_not_empty_the_other_shared_reads(world):
    key = ("harv1", "api_status", (), (("cluster", "harv1"),), "/staged/harv1.yaml", "operator", None)
    wapp.READ_SHARE.get(key, 3, lambda: (200, b"{}", "application/json", [], None, []))
    call("alice", "PUT", "/api/me/favorites/harvester/harv1")
    assert wapp.READ_SHARE.peek(key, 60) is not None


def test_a_session_write_from_another_site_is_refused(world, monkeypatch):
    monkeypatch.setattr(wapp, "_local_session", lambda: {"user": "alice", "sid": "s"})
    monkeypatch.setattr(wapp, "_same_origin", lambda: False)
    with wapp.app.test_client() as c:
        r = c.put("/api/me/favorites/harvester/harv1")
    assert r.status_code == 403


def test_without_identity_it_is_a_401(world):
    with wapp.app.test_client() as c:
        r = c.put("/api/me/favorites/harvester/harv1")
    assert r.status_code == 401


def test_the_route_is_rate_limited_with_a_valid_limit():
    src = (ROOT / "web" / "app.py").read_text()
    i = src.index('@app.route("/api/me/favorites/<kind>/<host>"')
    head = src[i:src.index("def api_my_favorite", i)]
    assert "@requires_auth" in head and '@_rate_limit("60/minute")' in head
    from limits import parse_many
    assert parse_many("60/minute")
