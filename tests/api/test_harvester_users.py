"""v1.31.0 — comptes du cluster Harvester.

Harvester (v1.8) modélise ses comptes très simplement, ce qui a été établi
en lisant le cluster plutôt qu'en le supposant :

  * un objet `users.management.cattle.io` porte le login et l'activation ;
  * l'administration est un `ClusterRoleBinding` ordinaire vers
    `cluster-admin`, sujet `User: <nom de l'objet>` ;
  * le mot de passe vit dans un secret séparé, sous forme de clé dérivée de
    32 octets avec un sel de 32 octets — PAS une empreinte bcrypt.

Ce dernier point borne la surface. Une sonde créée sur harv1 avec une
empreinte bcrypt (puis avec la variante `$2a$`) s'est fait refuser la
connexion : le secret suit un autre schéma, dont deviner les paramètres
poserait au mieux des comptes inutilisables. La création d'un compte local
avec mot de passe n'est donc pas offerte, et `password_management: false` le
dit dans la réponse plutôt que de laisser l'appelant l'apprendre à ses
dépens.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402


def crb(name, kind, subject_name, role="cluster-admin"):
    return {"metadata": {"name": name},
            "roleRef": {"kind": "ClusterRole", "name": role},
            "subjects": [{"kind": kind, "name": subject_name}]}


def user(uid, username, *, enabled=None, principals=None, bootstrap=False):
    meta = {"name": uid}
    if bootstrap:
        meta["labels"] = {"authz.management.cattle.io/bootstrapping": "admin-user"}
    u = {"metadata": meta, "username": username, "displayName": username}
    if enabled is not None:
        u["enabled"] = enabled
    if principals is not None:
        u["principalIds"] = principals
    return u


@pytest.fixture
def cluster(monkeypatch):
    """Un cluster joignable dont on contrôle ce que kubectl renvoie."""
    state = {"users": [], "bindings": [], "ran": []}
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc, **k: True)
    monkeypatch.setattr(wapp, "load_config", lambda: {
        "clusters": [{"name": "c", "kubeconfig": "/nonexistent.yaml"}]})

    def fake_json(kc, *args, **kw):
        if "users.management.cattle.io" in args:
            return {"items": state["users"]}
        if "clusterrolebindings" in args:
            return {"items": state["bindings"]}
        return {}

    class Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        state["ran"].append(cmd)
        return Done()

    monkeypatch.setattr(wapp, "_kubectl_json", fake_json)
    monkeypatch.setattr(wapp.subprocess, "run", fake_run)
    return state


def get_users(state):
    with wapp.app.test_client() as c:
        return c.get("/api/harvester-users/c").get_json()


def patch_user(uid, body):
    with wapp.app.test_client() as c:
        r = c.patch(f"/api/harvester-users/c/{uid}", json=body)
    return r.status_code, r.get_json()


# ---------------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------------

def test_admin_status_comes_from_the_binding_not_the_user(cluster):
    """C'est un ClusterRoleBinding ordinaire qui fait l'administrateur ;
    l'objet utilisateur n'en sait rien."""
    cluster["users"] = [user("u-1", "alice"), user("u-2", "bob")]
    cluster["bindings"] = [crb("globaladmin-u-1", "User", "u-1")]
    d = get_users(cluster)
    by_name = {u["username"]: u for u in d["users"]}
    assert by_name["alice"]["is_admin"] is True
    assert by_name["alice"]["admin_bindings"] == ["globaladmin-u-1"]
    assert by_name["bob"]["is_admin"] is False


def test_a_missing_enabled_field_means_active(cluster):
    """Rancher ne pose `enabled` qu'en cas de désactivation explicite : le
    lire comme « faux » afficherait tous les comptes comme désactivés."""
    cluster["users"] = [user("u-1", "alice"),
                        user("u-2", "bob", enabled=False),
                        user("u-3", "carol", enabled=True)]
    states = {u["username"]: u["enabled"] for u in get_users(cluster)["users"]}
    assert states == {"alice": True, "bob": False, "carol": True}


def test_bindings_for_other_roles_are_ignored(cluster):
    cluster["users"] = [user("u-1", "alice")]
    cluster["bindings"] = [crb("lecture", "User", "u-1", role="view")]
    assert get_users(cluster)["users"][0]["is_admin"] is False


def test_a_user_subject_without_a_user_object_is_an_orphan(cluster):
    """Trouvé sur harv1 : trois comptes supprimés avaient laissé leur
    délégation d'administration derrière eux. Recréer un compte portant cet
    identifiant lui rendrait cluster-admin en silence."""
    cluster["users"] = [user("u-1", "alice")]
    cluster["bindings"] = [crb("globaladmin-u-1", "User", "u-1"),
                           crb("globaladmin-u-parti", "User", "u-parti")]
    d = get_users(cluster)
    orphans = [o for o in d["other_admins"] if o["orphan"]]
    assert [o["name"] for o in orphans] == ["u-parti"]


def test_groups_are_listed_but_not_called_orphans(cluster):
    """Un groupe venu d'un fournisseur externe détient légitimement
    l'administration : le signaler comme orphelin serait une fausse alerte."""
    cluster["bindings"] = [crb("g", "Group", "keycloakoidc_group://rancher-admins")]
    other = get_users(cluster)["other_admins"][0]
    assert other["kind"] == "Group" and other["orphan"] is False


def test_service_accounts_are_counted_not_listed(cluster):
    """Il y en a dix-sept sur harv1, tous d'infrastructure : les lister
    noierait l'information utile."""
    cluster["bindings"] = [crb(f"sa-{i}", "ServiceAccount", f"svc-{i}")
                           for i in range(17)]
    d = get_users(cluster)
    assert d["service_account_admins"] == 17
    assert d["other_admins"] == []


def test_system_accounts_are_flagged(cluster):
    cluster["users"] = [user("u-1", "alice"),
                        user("boot", "admin", bootstrap=True),
                        user("u-sys", None, principals=["system://provisioning/x"])]
    flags = {u["id"]: u["system"] for u in get_users(cluster)["users"]}
    assert flags == {"u-1": False, "boot": True, "u-sys": True}


def test_the_payload_says_passwords_are_not_managed(cluster):
    """Le contrat explicite : cette console ne pose pas de mot de passe
    local, et le dit."""
    assert get_users(cluster)["password_management"] is False


# ---------------------------------------------------------------------------
# Écriture
# ---------------------------------------------------------------------------

def test_enabling_and_disabling_patches_the_user(cluster):
    cluster["users"] = [user("u-1", "alice")]
    status, d = patch_user("u-1", {"enabled": False})
    assert status == 200 and d["changed"] == ["disabled"]
    patched = [c for c in cluster["ran"] if "patch" in c]
    assert patched and json.loads(patched[-1][-1]) == {"enabled": False}


def test_granting_admin_creates_a_binding_of_our_own(cluster):
    cluster["users"] = [user("u-1", "alice")]
    status, d = patch_user("u-1", {"admin": True})
    assert status == 200 and d["changed"] == ["admin granted"]
    created = [c for c in cluster["ran"] if "create" in c]
    assert created, cluster["ran"]


def test_revoking_only_touches_bindings_this_console_created(cluster):
    """Garde essentielle : supprimer le binding que Harvester a posé à
    l'installation casserait le compte d'origine, et le rétablir n'aurait
    rien d'évident."""
    cluster["users"] = [user("u-1", "alice")]
    cluster["bindings"] = [crb("globaladmin-u-1", "User", "u-1")]
    status, d = patch_user("u-1", {"admin": False})
    assert status == 409
    assert d["bindings"] == ["globaladmin-u-1"]
    assert "kubectl" in d["hint"]
    assert not [c for c in cluster["ran"] if "delete" in c], \
        "rien ne doit avoir été supprimé"


def test_revoking_works_on_a_binding_we_created(cluster):
    cluster["users"] = [user("u-1", "alice")]
    cluster["bindings"] = [crb(wapp._admin_binding_name("u-1"), "User", "u-1")]
    status, d = patch_user("u-1", {"admin": False})
    assert status == 200 and d["changed"] == ["admin revoked"]
    assert [c for c in cluster["ran"] if "delete" in c]


def test_granting_twice_does_nothing(cluster):
    cluster["users"] = [user("u-1", "alice")]
    cluster["bindings"] = [crb("globaladmin-u-1", "User", "u-1")]
    status, d = patch_user("u-1", {"admin": True})
    assert status == 400, "rien à changer"
    assert not [c for c in cluster["ran"] if "create" in c]


def test_an_empty_request_is_refused(cluster):
    status, d = patch_user("u-1", {})
    assert status == 400
    assert "enabled" in d["hint"] and "admin" in d["hint"]


# ---------------------------------------------------------------------------
# Droits d'accès à cette surface
# ---------------------------------------------------------------------------

def test_even_reading_the_account_list_needs_admin():
    """Savoir qui détient l'administration d'un cluster n'a pas à être
    lisible par un compte en lecture seule."""
    assert wapp.required_role_for("/api/harvester-users/c", "GET") == "admin"
    assert wapp.required_role_for("/api/harvester-users/c/u-1", "PATCH") == "admin"


def test_the_password_limitation_is_written_in_the_code():
    src = (ROOT / "web" / "app.py").read_text()
    block = src.split("# Comptes du cluster Harvester", 1)[1][:2000]
    assert "clé dérivée" in block
    assert "n'est donc pas offerte" in block
