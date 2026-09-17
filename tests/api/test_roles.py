"""v1.30.0 — rôles dans la console.

L'authentification htpasswd ne vérifiait qu'un mot de passe. Tout compte
authentifié pouvait ensuite éteindre un cluster, supprimer une VM ou
détruire un workspace Terraform : il n'existait ni utilisateur ni rôle.

Le garde est CENTRAL et en refus par défaut. C'est le point important de ces
tests : un point d'entrée ajouté demain doit être protégé sans que personne
ait à y penser. La leçon vient des `@_rate_limit` qui décoraient sans rien
limiter, trouvés six versions trop tard.

Ce que ce modèle ne prétend PAS faire, et que les tests consignent : la
console agit sur le cluster avec un kubeconfig partagé, administrateur. Le
cluster ne voit qu'une identité. C'est un garde-fou contre l'erreur et
l'abus, pas une frontière que la RBAC du cluster ferait respecter.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402


@pytest.fixture
def roles(tmp_path, monkeypatch):
    """Installe une identité et un fichier de rôles, et rend un client qui
    parle au nom de l'utilisateur demandé."""
    htpasswd = tmp_path / "htpasswd"
    htpasswd.write_text("")            # son EXISTENCE suffit au garde
    roles_file = tmp_path / "roles.yaml"
    roles_file.write_text("""
default_role: viewer
users:
  patronne: admin
  operatrice: operator
  curieux: viewer
""")
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", htpasswd)
    monkeypatch.setattr(wapp, "ROLES_PATH", roles_file)
    monkeypatch.setattr(wapp, "_roles_cache", {"mtime": None, "data": None})
    # L'identité vient de l'en-tête ; le mot de passe est vérifié ailleurs.
    monkeypatch.setattr(wapp, "check_auth", lambda u, p: True)
    import base64

    def call(user, method, path, body=None):
        token = base64.b64encode(f"{user}:x".encode()).decode()
        with wapp.app.test_client() as c:
            return c.open(path, method=method, json=body if body is not None else {},
                          headers={"Authorization": f"Basic {token}"})

    return call


# ---------------------------------------------------------------------------
# Le classement des gestes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path,expected", [
    ("GET", "/api/clusters", "viewer"),
    ("GET", "/api/vms/harv1", "viewer"),
    ("PATCH", "/api/vm/harv1/default/web/runStrategy", "operator"),
    ("POST", "/api/vms/harv1/create", "operator"),
    ("POST", "/api/terraform/harv1/apply", "operator"),
    # Les gestes qui coupent un service, changent la configuration de
    # l'outil, ou touchent au matériel.
    ("POST", "/api/action", "admin"),
    ("POST", "/api/clusters", "admin"),
    ("DELETE", "/api/clusters/harv1", "admin"),
    ("POST", "/api/bmc/1.2.3.4/power", "admin"),
    ("POST", "/api/iso/fetch", "admin"),
    ("POST", "/api/terraform/provider/install", "admin"),
    ("POST", "/api/terraform/harv1/destroy", "admin"),
    ("POST", "/api/capi/harv1/install", "admin"),
])
def test_each_path_asks_for_the_right_role(method, path, expected):
    assert wapp.required_role_for(path, method) == expected


def test_reading_is_always_allowed_to_a_viewer():
    for method in ("GET", "HEAD", "OPTIONS"):
        assert wapp.required_role_for("/api/anything/at/all", method) == "viewer"


def test_an_unknown_mutating_path_defaults_to_operator():
    """Refus par défaut : un point d'entrée ajouté demain est protégé sans
    qu'on ait à y penser."""
    assert wapp.required_role_for("/api/something-invented-tomorrow", "POST") \
        == "operator"
    assert wapp.required_role_for("/api/x", "DELETE") == "operator"
    assert wapp.required_role_for("/api/x", "PUT") == "operator"


# ---------------------------------------------------------------------------
# L'application du garde
# ---------------------------------------------------------------------------

def test_a_viewer_cannot_change_anything(roles):
    r = roles("curieux", "POST", "/api/vms/harv1/create")
    assert r.status_code == 403
    d = r.get_json()
    assert d["role"] == "viewer" and d["required"] == "operator"
    # Le message nomme le rôle qu'il faudrait : « forbidden » seul laisse
    # l'opérateur sans recours.
    assert "operator" in d["hint"]


def test_an_operator_can_act_but_not_shut_down_a_cluster(roles):
    assert roles("operatrice", "POST", "/api/vms/harv1/create").status_code != 403
    r = roles("operatrice", "POST", "/api/action")
    assert r.status_code == 403
    assert r.get_json()["required"] == "admin"


def test_an_admin_passes_everywhere(roles):
    for method, path in (("POST", "/api/action"),
                         ("POST", "/api/iso/fetch"),
                         ("POST", "/api/vms/harv1/create")):
        assert roles("patronne", method, path).status_code != 403, path


def test_an_unlisted_account_falls_back_to_the_default_role(roles):
    r = roles("inconnu", "POST", "/api/vms/harv1/create")
    assert r.status_code == 403
    assert r.get_json()["role"] == "viewer"


def test_reading_stays_open_to_everyone(roles):
    for user in ("curieux", "operatrice", "patronne", "inconnu"):
        assert roles(user, "GET", "/api/clusters").status_code != 403, user


# ---------------------------------------------------------------------------
# Sans identité, pas de rôle
# ---------------------------------------------------------------------------

def test_without_htpasswd_nobody_is_restricted(tmp_path, monkeypatch):
    """Sans authentification, personne n'est identifiable. Brider sur une
    identité vide mettrait TOUT LE MONDE en lecture seule, exploitant
    compris — ce qui est arrivé au premier jet."""
    roles_file = tmp_path / "roles.yaml"
    roles_file.write_text("default_role: viewer\nusers: {}\n")
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "absent")
    monkeypatch.setattr(wapp, "ROLES_PATH", roles_file)
    monkeypatch.setattr(wapp, "_roles_cache", {"mtime": None, "data": None})
    assert wapp.roles_active() is False
    with wapp.app.test_client() as c:
        assert c.get("/api/whoami").get_json()["role"] == "admin"


def test_without_a_roles_file_nobody_is_restricted(tmp_path, monkeypatch):
    """Mettre à jour la console ne doit pas verrouiller une installation qui
    n'a pas encore de fichier de rôles."""
    htpasswd = tmp_path / "htpasswd"
    htpasswd.write_text("")
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", htpasswd)
    monkeypatch.setattr(wapp, "ROLES_PATH", tmp_path / "absent.yaml")
    monkeypatch.setattr(wapp, "_roles_cache", {"mtime": None, "data": None})
    assert wapp.roles_active() is False


def test_a_broken_roles_file_does_not_lock_everyone_out(tmp_path, monkeypatch,
                                                         caplog):
    bad = tmp_path / "roles.yaml"
    bad.write_text("ceci: n'est pas: un yaml valide: [")
    monkeypatch.setattr(wapp, "ROLES_PATH", bad)
    monkeypatch.setattr(wapp, "_roles_cache", {"mtime": None, "data": None})
    data = wapp.load_roles()
    assert data["users"] == {}
    assert data["default_role"] == "viewer"


def test_an_invalid_role_name_is_ignored(tmp_path, monkeypatch):
    """Une faute de frappe (« opeator ») ne doit pas accorder de droits par
    accident : le compte retombe sur le rôle par défaut."""
    f = tmp_path / "roles.yaml"
    f.write_text("default_role: viewer\nusers:\n  bob: opeator\n")
    monkeypatch.setattr(wapp, "ROLES_PATH", f)
    monkeypatch.setattr(wapp, "_roles_cache", {"mtime": None, "data": None})
    assert "bob" not in wapp.load_roles()["users"]


def test_an_invalid_default_role_falls_back_to_viewer(tmp_path, monkeypatch):
    f = tmp_path / "roles.yaml"
    f.write_text("default_role: superuser\nusers: {}\n")
    monkeypatch.setattr(wapp, "ROLES_PATH", f)
    monkeypatch.setattr(wapp, "_roles_cache", {"mtime": None, "data": None})
    assert wapp.load_roles()["default_role"] == "viewer"


def test_the_file_is_reread_when_it_changes(tmp_path, monkeypatch):
    """Changer un rôle ne doit pas demander de redémarrer la console."""
    import os
    f = tmp_path / "roles.yaml"
    f.write_text("default_role: viewer\nusers:\n  bob: viewer\n")
    monkeypatch.setattr(wapp, "ROLES_PATH", f)
    monkeypatch.setattr(wapp, "_roles_cache", {"mtime": None, "data": None})
    assert wapp.load_roles()["users"]["bob"] == "viewer"
    f.write_text("default_role: viewer\nusers:\n  bob: admin\n")
    os.utime(f, (0, 0))                # force un mtime différent
    assert wapp.load_roles()["users"]["bob"] == "admin"


# ---------------------------------------------------------------------------
# Couverture : aucun point d'entrée mutatif ne doit échapper au garde
# ---------------------------------------------------------------------------

def test_every_mutating_route_is_covered_by_the_gate():
    """Le garde est central, donc la couverture est totale par
    construction. Ce test le VÉRIFIE plutôt que de le supposer : c'est ce
    qui manquait aux limites de débit, décorées une par une et oubliées six
    fois."""
    uncovered = []
    for rule in wapp.app.url_map.iter_rules():
        path = str(rule)
        if not path.startswith("/api/"):
            continue
        mutating = {"POST", "PUT", "PATCH", "DELETE"} & set(rule.methods or ())
        if not mutating:
            continue
        for method in sorted(mutating):
            if wapp.required_role_for(path, method) == "viewer":
                uncovered.append(f"{method} {path}")
    assert not uncovered, (
        "ces points d'entrée modifient quelque chose sans exiger de rôle : "
        + ", ".join(uncovered))


def test_the_gate_runs_before_the_view():
    """Un garde posé après coup laisserait la vue s'exécuter."""
    src = (ROOT / "web" / "app.py").read_text()
    assert "@app.before_request\ndef _enforce_role():" in src


def test_the_limitation_is_written_down():
    """Le modèle ne fait pas respecter la RBAC du cluster, et le code doit
    le dire : la console agit avec un kubeconfig partagé, administrateur."""
    src = (ROOT / "web" / "app.py").read_text()
    block = src.split("# Rôles\n", 1)[1][:2000]
    assert "kubeconfig partagé" in block
    assert "pas une frontière" in block
