"""v1.32.0 : identité présentée au cluster.

Jusqu'ici la console agissait avec UN kubeconfig partagé. Mesuré sur harv1 :
il vaut `system:admin`, groupe `system:masters`, c'est-à-dire un
superutilisateur câblé dans l'apiserver qui COURT-CIRCUITE la RBAC. Les rôles
de la v1.30.0 étaient donc un garde-fou côté console et rien de plus : aucune
règle Harvester ne s'appliquait à ce que la console faisait.

kubectl sait porter une usurpation DANS le kubeconfig (`as`, `as-groups`).
On substitue donc une copie porteuse de l'identité au point UNIQUE où le
chemin est résolu, ce qui évite de toucher la cinquantaine de sites d'appel.
Vérifié en réel sur harv1 : sous usurpation, `can-i list virtualmachines`
répond « no » là où le kubeconfig partagé répond « yes ».

Deux pièges que ces tests verrouillent :

  * un thread de travail n'a pas de contexte de requête ; y relire l'identité
    rend None et l'action repart avec le kubeconfig administrateur, soit
    exactement là où les gestes sont les plus lourds (extinction de cluster) ;
  * un compte sans correspondance ne doit pas « retomber » sur le kubeconfig
    partagé, sinon activer la délégation ne protège de rien.
"""

import base64
import os
import stat
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402


KUBECONFIG = {
    "apiVersion": "v1",
    "kind": "Config",
    "current-context": "ctx",
    "clusters": [{"name": "c", "cluster": {"server": "https://10.0.0.1:6443"}}],
    "contexts": [{"name": "ctx", "context": {"cluster": "c", "user": "default"}}],
    "users": [{"name": "default", "user": {"client-certificate-data": "Zm9v"}}],
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Une console authentifiée, un fichier de rôles pilotable, un kubeconfig."""
    htpasswd = tmp_path / "htpasswd"
    htpasswd.write_text("")
    roles_file = tmp_path / "roles.yaml"
    kc = tmp_path / "kubeconfig.yaml"
    kc.write_text(yaml.safe_dump(KUBECONFIG))

    monkeypatch.setattr(wapp, "HTPASSWD_PATH", htpasswd)
    monkeypatch.setattr(wapp, "ROLES_PATH", roles_file)
    monkeypatch.setattr(wapp, "IDENTITY_DIR", tmp_path / "identities")
    monkeypatch.setattr(wapp, "check_auth", lambda u, p: True)
    monkeypatch.setattr(wapp, "load_config", lambda: {
        "clusters": [{"name": "harv1", "kubeconfig": str(kc)}]})

    def write_roles(text):
        roles_file.write_text(text)
        wapp._roles_cache.update({"mtime": None, "data": None})
        wapp._identity_cache.clear()

    def call(user, method, path, body=None):
        token = base64.b64encode(f"{user}:x".encode()).decode()
        with wapp.app.test_client() as c:
            return c.open(path, method=method,
                          json=body if body is not None else {},
                          headers={"Authorization": f"Basic {token}"})

    def as_user(user):
        """Entre dans un contexte de requête au nom de `user`."""
        token = base64.b64encode(f"{user}:x".encode()).decode()
        return wapp.app.test_request_context(
            "/api/x", headers={"Authorization": f"Basic {token}"})

    write_roles("default_role: viewer\nusers: {}\n")
    return type("Env", (), {"write_roles": staticmethod(write_roles),
                            "call": staticmethod(call),
                            "as_user": staticmethod(as_user),
                            "kc": kc, "tmp": tmp_path})


DELEGATED = """
default_role: viewer
identity:
  delegate: true
users:
  patronne:
    role: admin
    cluster_user: u-p5oguyiwv6
    cluster_groups: [harvester-admins, lecture]
  simple: operator
"""


# ---------------------------------------------------------------------------
# Le format de configuration
# ---------------------------------------------------------------------------

def test_the_short_form_still_means_a_role(env):
    """Des milliers d'installations écrivent `login: role`. La faire tomber
    en panne au profit d'une table serait une régression silencieuse."""
    env.write_roles("default_role: viewer\nusers:\n  bob: operator\n")
    assert wapp.load_roles()["users"]["bob"] == "operator"
    assert wapp.load_roles()["identities"] == {}


def test_the_long_form_carries_both_role_and_cluster_identity(env):
    env.write_roles(DELEGATED)
    roles = wapp.load_roles()
    assert roles["users"]["patronne"] == "admin"
    assert roles["identities"]["patronne"] == {
        "user": "u-p5oguyiwv6", "groups": ["harvester-admins", "lecture"]}


def test_a_long_form_without_cluster_user_grants_no_identity(env):
    env.write_roles(
        "identity:\n  delegate: true\nusers:\n  bob:\n    role: admin\n")
    assert wapp.load_roles()["users"]["bob"] == "admin"
    assert "bob" not in wapp.load_roles()["identities"]


def test_an_invalid_role_in_long_form_is_still_ignored(env):
    """La garde de la v1.30.0 (une faute de frappe n'accorde rien) doit
    survivre au nouveau format."""
    env.write_roles("identity:\n  delegate: true\n"
                    "users:\n  bob:\n    role: opeator\n    cluster_user: u-1\n")
    assert "bob" not in wapp.load_roles()["users"]


# ---------------------------------------------------------------------------
# Quand la délégation s'applique
# ---------------------------------------------------------------------------

def test_delegation_is_off_unless_asked(env):
    """Une mise à jour ne doit rien changer à une installation en place."""
    env.write_roles("default_role: viewer\nusers:\n  patronne: admin\n")
    assert wapp.identity_delegation_active() is False


def test_delegation_needs_an_identity_to_delegate(env, monkeypatch):
    """Sans htpasswd personne n'est identifiable : déléguer une identité vide
    n'aurait aucun sens."""
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", env.tmp / "absent")
    env.write_roles(DELEGATED)
    assert wapp.identity_delegation_active() is False


def test_with_delegation_off_the_shared_kubeconfig_is_used_unchanged(env):
    env.write_roles("default_role: viewer\nusers:\n  patronne: admin\n")
    with env.as_user("patronne"):
        assert wapp._kubectl_for_cluster("harv1") == str(env.kc)


def test_with_delegation_on_the_kubeconfig_carries_the_identity(env):
    env.write_roles(DELEGATED)
    with env.as_user("patronne"):
        path = wapp._kubectl_for_cluster("harv1")
    assert path != str(env.kc), "le kubeconfig partagé a été rendu tel quel"
    doc = yaml.safe_load(Path(path).read_text())
    user = doc["users"][0]["user"]
    assert user["as"] == "u-p5oguyiwv6"
    assert user["as-groups"] == ["harvester-admins", "lecture"]
    # Le certificat d'origine doit survivre : c'est lui qui authentifie.
    assert user["client-certificate-data"] == "Zm9v"


def test_an_identity_without_groups_writes_no_group_key(env):
    """`as-groups: []` n'est pas neutre côté apiserver : mieux vaut ne rien
    écrire que d'écrire une liste vide."""
    env.write_roles("identity:\n  delegate: true\n"
                    "users:\n  bob:\n    role: admin\n    cluster_user: u-b\n")
    with env.as_user("bob"):
        path = wapp._kubectl_for_cluster("harv1")
    user = yaml.safe_load(Path(path).read_text())["users"][0]["user"]
    assert user["as"] == "u-b"
    assert "as-groups" not in user


def test_the_copy_is_not_readable_by_anyone_else(env):
    """Elle contient les identifiants du cluster."""
    env.write_roles(DELEGATED)
    with env.as_user("patronne"):
        path = Path(wapp._kubectl_for_cluster("harv1"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_the_copy_is_reused_rather_than_rewritten(env):
    env.write_roles(DELEGATED)
    with env.as_user("patronne"):
        first = wapp._kubectl_for_cluster("harv1")
        second = wapp._kubectl_for_cluster("harv1")
    assert first == second


def test_a_changed_source_kubeconfig_produces_a_fresh_copy(env):
    """Sinon une rotation de certificat laisserait la console sur l'ancien."""
    env.write_roles(DELEGATED)
    with env.as_user("patronne"):
        first = wapp._kubectl_for_cluster("harv1")
        doc = dict(KUBECONFIG)
        doc["users"] = [{"name": "default",
                         "user": {"client-certificate-data": "YmFy"}}]
        env.kc.write_text(yaml.safe_dump(doc))
        os.utime(env.kc, (0, 0))
        second = wapp._kubectl_for_cluster("harv1")
    assert first != second
    user = yaml.safe_load(Path(second).read_text())["users"][0]["user"]
    assert user["client-certificate-data"] == "YmFy"


def test_two_users_do_not_share_a_copy(env):
    env.write_roles(DELEGATED + "  autre:\n    role: admin\n"
                                "    cluster_user: u-autre\n")
    with env.as_user("patronne"):
        a = wapp._kubectl_for_cluster("harv1")
    with env.as_user("autre"):
        b = wapp._kubectl_for_cluster("harv1")
    assert a != b


def test_an_unreadable_kubeconfig_falls_back_instead_of_breaking(env):
    """Un kubeconfig illisible ne doit pas mettre la console par terre : on
    journalise et on repart sur le chemin nominal."""
    env.kc.write_text("ceci: n'est pas: un yaml: [")
    env.write_roles(DELEGATED)
    with env.as_user("patronne"):
        assert wapp._kubectl_for_cluster("harv1") == str(env.kc)


# ---------------------------------------------------------------------------
# Un compte sans correspondance
# ---------------------------------------------------------------------------

def test_an_unmapped_account_is_refused_when_delegation_is_on(env):
    """C'est LE point : le laisser passer le ferait retomber sur le
    kubeconfig administrateur, et activer la délégation ne protégerait rien."""
    env.write_roles(DELEGATED)
    r = env.call("simple", "GET", "/api/clusters")
    assert r.status_code == 403
    d = r.get_json()
    assert d["error"] == "no cluster identity"
    assert "roles.yaml" in d["hint"]


def test_a_mapped_account_passes(env):
    env.write_roles(DELEGATED)
    assert env.call("patronne", "GET", "/api/clusters").status_code != 403


def test_the_refusal_can_be_relaxed_explicitly(env):
    env.write_roles(DELEGATED.replace(
        "  delegate: true", "  delegate: true\n  deny_unmapped: false"))
    assert env.call("simple", "GET", "/api/clusters").status_code != 403


def test_nobody_is_refused_when_delegation_is_off(env):
    env.write_roles("default_role: viewer\nusers:\n  simple: operator\n")
    assert env.call("simple", "GET", "/api/clusters").status_code != 403


def test_whoami_still_answers_an_unmapped_account(env):
    """Sinon l'écran ne peut pas expliquer POURQUOI tout est refusé."""
    env.write_roles(DELEGATED)
    r = env.call("simple", "GET", "/api/whoami")
    assert r.status_code == 200
    d = r.get_json()
    assert d["delegation_active"] is True
    assert d["cluster_user"] is None


def test_whoami_reports_the_cluster_identity(env):
    env.write_roles(DELEGATED)
    d = env.call("patronne", "GET", "/api/whoami").get_json()
    assert d["cluster_user"] == "u-p5oguyiwv6"
    assert d["cluster_groups"] == ["harvester-admins", "lecture"]


# ---------------------------------------------------------------------------
# Parité CLI : les scripts doivent présenter la même identité
# ---------------------------------------------------------------------------

def test_the_scripts_receive_the_identity_through_the_environment(env):
    env.write_roles(DELEGATED)
    with env.as_user("patronne"):
        e = wapp.identity_env()
    assert e["HARVESTER_OPS_AS"] == "u-p5oguyiwv6"
    assert e["HARVESTER_OPS_AS_GROUPS"] == "harvester-admins,lecture"


def test_no_environment_variables_when_delegation_is_off(env):
    env.write_roles("default_role: viewer\nusers:\n  patronne: admin\n")
    with env.as_user("patronne"):
        assert wapp.identity_env() == {}


def test_common_sh_applies_the_identity_in_one_place():
    """La substitution doit rester centrale côté bash aussi : 13 appels
    kubectl directs y vivent, les rattraper un par un serait la même erreur
    que les `@_rate_limit` posés à la main et oubliés six fois."""
    src = (ROOT / "bin" / "lib" / "common.sh").read_text()
    assert "apply_cluster_identity() {" in src
    assert "apply_cluster_identity\n" in src.split("load_cluster() {")[0] \
        or "    apply_cluster_identity" in src.split("load_cluster() {")[1]
    assert "HARVESTER_OPS_AS" in src
    assert 'chmod 0600' in src


# ---------------------------------------------------------------------------
# Le piège du thread de travail
# ---------------------------------------------------------------------------

def test_an_action_freezes_its_identity_at_trigger_time(env, monkeypatch):
    """Le thread de travail n'a pas de contexte de requête. Si l'identité s'y
    relit, elle vaut None et l'extinction du cluster repart en administrateur.
    Elle doit donc être capturée AU DÉCLENCHEMENT."""
    env.write_roles(DELEGATED)
    started = {}

    class FakeRun:
        pass

    monkeypatch.setattr(wapp.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda s: None})())
    with env.as_user("patronne"):
        run = wapp.start_action("status", "harv1", dry_run=True)
        started["env"] = run.identity_env
        started["user"] = run.cluster_user
    assert started["env"]["HARVESTER_OPS_AS"] == "u-p5oguyiwv6"
    assert started["user"] == "u-p5oguyiwv6"
    # Et hors contexte de requête, la relecture rendrait bien None : c'est
    # précisément ce que la capture évite.
    assert wapp.current_cluster_identity() is None


def test_current_identity_outside_a_request_does_not_explode(env):
    """Appelée depuis un thread, elle doit rendre None, pas lever."""
    env.write_roles(DELEGATED)
    assert wapp.current_cluster_identity() is None


def test_an_action_reports_the_identity_it_used(env, monkeypatch):
    """L'historique doit dire sous quelle identité le cluster a vu le geste."""
    env.write_roles(DELEGATED)
    monkeypatch.setattr(wapp.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda s: None})())
    with env.as_user("patronne"):
        run = wapp.start_action("status", "harv1", dry_run=True)
    assert run.to_dict()["cluster_user"] == "u-p5oguyiwv6"


# ---------------------------------------------------------------------------
# Ce que le modèle ne prétend pas faire
# ---------------------------------------------------------------------------

def test_the_remaining_limitation_is_written_down():
    """La console DÉTIENT le kubeconfig administrateur : c'est une frontière
    que le cluster applique, pas un coffre. Le code doit le dire."""
    src = (ROOT / "web" / "app.py").read_text()
    block = src.split("# Identité présentée au cluster", 1)[1][:3000]
    assert "system:masters" in block
    assert "pas un coffre" in block


# ---------------------------------------------------------------------------
# Ce que le test EN RÉEL a attrapé et que l'unitaire ne voyait pas
# ---------------------------------------------------------------------------

def test_a_cluster_refusal_is_a_403_not_a_500(env, monkeypatch):
    """Avant la délégation, personne ne pouvait se faire refuser : le
    kubeconfig partagé est `system:masters`, qui court-circuite la RBAC. Avec
    elle, un compte aux droits réduits en reçoit, et cela remontait en 500,
    donc « erreur serveur » pour un refus parfaitement normal, sans dire qui
    refusait ni pourquoi."""
    env.write_roles(DELEGATED)

    class Denied:
        returncode = 1
        stdout = ""
        stderr = ('Error from server (Forbidden): virtualmachines.kubevirt.io '
                  'is forbidden: User "u-p5oguyiwv6" cannot list resource')

    monkeypatch.setattr(wapp.subprocess, "run", lambda *a, **k: Denied())
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc, **k: True)
    r = env.call("patronne", "GET", "/api/vms/harv1")
    assert r.status_code == 403
    d = r.get_json()
    assert d["error"] == "cluster refused"
    assert d["cluster_user"] == "u-p5oguyiwv6"
    assert "forbidden" in d["detail"].lower()


def test_a_failing_kubectl_never_leaks_the_kubeconfig_path(env, monkeypatch):
    """`CalledProcessError.__str__` recopie l'argv complet. Le renvoyer dans
    une réponse HTTP divulguait le chemin du kubeconfig : constaté en testant
    la délégation en réel, avec un compte aux droits réduits."""
    env.write_roles(DELEGATED)

    class Broken:
        returncode = 1
        stdout = ""
        stderr = "quelque chose a cassé"      # pas un refus RBAC

    monkeypatch.setattr(wapp.subprocess, "run", lambda *a, **k: Broken())
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc, **k: True)
    body = str(env.call("patronne", "GET", "/api/vms/harv1").get_json())
    assert "kubeconfig" not in body.lower()
    assert str(env.kc) not in body
    assert "/identities/" not in body


def test_the_safe_error_helper_drops_the_command_line():
    e = wapp.subprocess.CalledProcessError(
        1, ["kubectl", "--kubeconfig", "/secret/path.yaml", "get", "vm"])
    msg = wapp._safe_proc_error(e)
    assert "/secret/path.yaml" not in msg
    assert "exit code 1" in msg
    t = wapp.subprocess.TimeoutExpired(["kubectl", "--kubeconfig", "/secret"], 5)
    assert "/secret" not in wapp._safe_proc_error(t)


def test_the_denial_handler_returns_a_response_not_a_tuple(env, monkeypatch):
    """Un after_request qui rend un tuple fait exploser le handler suivant
    sur `.headers`, et la réponse repart en 500 sans rien expliquer."""
    env.write_roles(DELEGATED)

    class Denied:
        returncode = 1
        stdout = ""
        stderr = "Error from server (Forbidden): cannot list resource"

    monkeypatch.setattr(wapp.subprocess, "run", lambda *a, **k: Denied())
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc, **k: True)
    r = env.call("patronne", "GET", "/api/vms/harv1")
    # Les en-têtes de sécurité sont posés par un after_request POSTÉRIEUR :
    # leur présence prouve que la chaîne n'a pas été rompue.
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.status_code == 403


def test_a_denial_outside_a_request_does_not_explode():
    """Les threads de travail appellent kubectl sans contexte de requête."""
    wapp._note_cluster_denial("Error from server (Forbidden): cannot list")
