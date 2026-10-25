"""2.0.0 (revue 3b, I-2) : une lecture partagée qui ne nomme pas de cluster
(vue de flotte : /api/vms-all, /api/containers-all, /api/gpus-all, menu des
hôtes, comptes des dossiers) porte l'identité présentée aux clusters.

Avant : la clé ne portait le kubeconfig que si la route nommait un cluster.
Deux personnes du même rôle, connectées par Rancher (ou sous délégation
d'identité), partageaient donc la même réponse de flotte : la seconde voyait
les VMs des clusters que Rancher ne montre qu'à la première. Préexistant
depuis la 1.85 ; la vue des VMs est l'écran d'accueil de la 2.0.0.

Le cas nominal (comptes locaux, pas de délégation) partage toujours : tous
lisent avec le même kubeconfig, la même vue."""

import base64
import json
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
os.environ.setdefault("HARVESTER_OPS_DISABLE_RATELIMIT", "1")
import app as wapp  # noqa: E402
from flask import request  # noqa: E402

SESSIONS = {
    "ann": types.SimpleNamespace(login="ann@rancher", role="operator", token="ta",
                                 server_id="r1", ident={"id": "u-ann", "username": "ann"}),
    "bob": types.SimpleNamespace(login="bob@rancher", role="operator", token="tb",
                                 server_id="r1", ident={"id": "u-bob", "username": "bob"}),
    # même nom d'utilisateur sur un autre Rancher : une autre personne
    "ann2": types.SimpleNamespace(login="ann@rancher", role="operator", token="tc",
                                  server_id="r2", ident={"id": "u-ann", "username": "ann"}),
}
# ce que Rancher laisse voir à chacun
VISIBLE = {"ann": {"harv1"}, "bob": {"harv1", "secret"}, "ann2": {"harv1"}}


def _who():
    try:
        return request.headers.get("X-Who")
    except RuntimeError:
        return None


@pytest.fixture
def sso(monkeypatch):
    monkeypatch.setattr(wapp, "READ_SHARE", wapp._rsh.ReadShare())
    monkeypatch.setattr(wapp, "READ_SHARE_ENABLED", True)
    monkeypatch.setattr(wapp, "_sso_session", lambda: SESSIONS.get(_who()))
    cfg = {"clusters": [{"name": "harv1", "kubeconfig": "/k/harv1.yaml"},
                        {"name": "secret", "kubeconfig": "/k/secret.yaml"}]}
    monkeypatch.setattr(wapp, "load_config", lambda: cfg)
    monkeypatch.setattr(wapp, "load_config_ro", lambda: cfg)

    def kc(cluster):
        who = _who()
        return f"/sso/{who}/{cluster}.yaml" if cluster in VISIBLE.get(who, ()) else None
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", kc)
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda k: True)
    calls = []

    def collect(name, kc_path):
        calls.append(kc_path)
        return {"vms": [{"name": f"vm-{name}", "namespace": "ns", "key": f"harvester/{name}/ns/vm-{name}"}]}, 200, []
    monkeypatch.setattr(wapp, "_vms_collect", collect)
    return calls


def get(who, path):
    with wapp.app.test_client() as c:
        return c.get(path, headers={"X-Who": who})


def key_for(who, endpoint="api_vms_all", **kw):
    with wapp.app.test_request_context("/api/x", headers={"X-Who": who}):
        return wapp._read_share_key(endpoint, {}, (("kinds", "harvester"),), **kw)


# ---------------------------------------------------------------------------
# clé
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("a,b,same", [
    ("ann", "bob", False),      # même rôle, autre personne Rancher
    ("ann", "ann2", False),     # même nom, autre Rancher
    ("ann", "ann", True),       # même personne : on partage
])
def test_fleet_key_carries_the_rancher_identity(sso, a, b, same):
    assert (key_for(a) == key_for(b)) is same


@pytest.mark.parametrize("scope", [None, "_hosts", "_replication"])
def test_every_fleet_key_without_a_cluster_carries_the_identity(sso, scope):
    assert key_for("ann", scope=scope) != key_for("bob", scope=scope)


def test_kvm_keys_are_shared_across_identities(sso):
    """Revue finale I-3 : un hôte KVM est un objet de la console, lu avec la
    clé SSH de la console quelle que soit la personne ; ses lectures sont
    partagées entre identités (le rôle reste dans la clé)."""
    assert key_for("ann", scope="_kvm") == key_for("bob", scope="_kvm") == key_for("ann2", scope="_kvm")


def test_cluster_keys_keep_the_kubeconfig_only(sso):
    """Une route qui nomme un cluster porte déjà le kubeconfig de la personne."""
    def k(who):
        with wapp.app.test_request_context("/api/x", headers={"X-Who": who}):
            return wapp._read_share_key("api_vms_list", {"cluster": "harv1"})
    assert k("ann") != k("bob")
    assert k("ann")[4] == "/sso/ann/harv1.yaml"


@pytest.mark.parametrize("ident_a,ident_b,same", [
    ({"user": "u1", "groups": ["ops"]}, {"user": "u2", "groups": ["ops"]}, False),
    ({"user": "u1", "groups": ["ops"]}, {"user": "u1", "groups": ["dev"]}, False),
    ({"user": "u1", "groups": ["a", "b"]}, {"user": "u1", "groups": ["b", "a"]}, True),
    (None, None, True),         # comptes locaux sans délégation : on partage
])
def test_fleet_key_carries_the_delegated_identity(monkeypatch, ident_a, ident_b, same):
    monkeypatch.setattr(wapp, "_sso_session", lambda: None)
    ids = {"a": ident_a, "b": ident_b}
    monkeypatch.setattr(wapp, "current_cluster_identity", lambda: ids[_who()])
    monkeypatch.setattr(wapp, "current_role", lambda: "operator")
    assert (key_for("a") == key_for("b")) is same


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
def test_vms_all_is_not_served_across_rancher_identities(sso):
    a = get("ann", "/api/vms-all?kinds=harvester").get_json()
    b = get("bob", "/api/vms-all?kinds=harvester").get_json()
    assert {v["cluster"] for v in a["vms"]} == {"harv1"}
    assert {v["cluster"] for v in b["vms"]} == {"harv1", "secret"}
    # ann relit : servie depuis SA lecture, jamais celle de bob
    again = get("ann", "/api/vms-all?kinds=harvester").get_json()
    assert {v["cluster"] for v in again["vms"]} == {"harv1"}


def test_same_identity_still_shares_one_read(sso):
    get("ann", "/api/vms-all?kinds=harvester")
    n = len(sso)
    get("ann", "/api/vms-all?kinds=harvester")
    assert len(sso) == n


def test_folder_counts_of_one_person_never_include_the_keys_of_another(sso, monkeypatch):
    get("bob", "/api/vms-all?kinds=harvester,kvm")
    get("ann", "/api/vms-all?kinds=harvester,kvm")
    monkeypatch.setattr(wapp._kd, "load_all", lambda d: [])
    monkeypatch.setattr(wapp, "_visible_cluster_names", lambda: VISIBLE[_who()])
    for who in ("ann", "bob"):
        with wapp.app.test_request_context("/api/folders", headers={"X-Who": who}):
            vms = wapp._folder_visible_keys()["vms"]
        clusters = {k.split("/")[1] for k in vms}
        assert clusters == VISIBLE[who], who


def test_containers_all_and_gpus_all_are_keyed_by_identity(sso, monkeypatch):
    for ep in ("api_containers_all", "api_gpus_all"):
        with wapp.app.test_request_context("/api/x", headers={"X-Who": "ann"}):
            ka = wapp._read_share_key(ep, {})
        with wapp.app.test_request_context("/api/x", headers={"X-Who": "bob"}):
            kb = wapp._read_share_key(ep, {})
        assert ka != kb, ep


def test_the_identity_part_is_frozen_in_the_single_key_builder():
    """Mutation de garde : retirer la part d'identité fait échouer les tests
    ci-dessus ; ce test verrouille qu'elle passe par le constructeur unique."""
    src = (ROOT / "web" / "app.py").read_text()
    body = src.split("def _read_share_key(", 1)[1].split("\ndef ", 1)[0]
    assert "_read_share_identity()" in body
    assert src.count("_read_share_key(") >= 4


# ---------------------------------------------------------------------------
# Revue finale I-3 : la part KVM de /api/vms-all est lue une fois pour tous
# ---------------------------------------------------------------------------
@pytest.fixture
def kvm_survey(sso, monkeypatch, tmp_path):
    monkeypatch.setenv("HARVESTER_OPS_FOLDERS_DB", str(tmp_path / "folders.db"))
    decls = [{"name": "node2"}, {"name": "kvm2"}]
    monkeypatch.setattr(wapp._kd, "load_all", lambda d: decls)
    monkeypatch.setattr(wapp._kh, "VirshHost", lambda d, state, timeout=None: d["name"])
    surveys = []

    def read_all(hosts, deadline=None, folders=False):
        surveys.extend(hosts)
        return [{"name": h, "vms": [{"name": f"dom-{h}", "uuid": f"0000000{i}-0000-0000-0000-000000000000",
                                     "state": "running", "host": h}], "errors": []}
                for i, h in enumerate(hosts)]
    monkeypatch.setattr(wapp._kvm_cli, "read_all", read_all)
    return surveys


def test_two_identities_cause_one_ssh_survey_per_host(kvm_survey):
    a = get("ann", "/api/vms-all?kinds=harvester,kvm").get_json()
    b = get("bob", "/api/vms-all?kinds=harvester,kvm").get_json()
    assert sorted(kvm_survey) == ["kvm2", "node2"]          # une seule tournée SSH
    for body in (a, b):
        assert {v["source"] for v in body["vms"] if v["kind"] == "kvm"} == {"node2", "kvm2"}
    # la part Harvester reste propre à chacun
    assert {v["cluster"] for v in a["vms"] if v["kind"] == "harvester"} == {"harv1"}
    assert {v["cluster"] for v in b["vms"] if v["kind"] == "harvester"} == {"harv1", "secret"}


def test_i3_mutation_identity_in_the_kvm_key(kvm_survey, monkeypatch):
    """Mutation volontaire : l'identité remise dans la clé KVM = une tournée
    SSH par personne."""
    real = wapp._kvm_part_key
    monkeypatch.setattr(wapp, "_kvm_part_key", lambda: real() + (wapp._read_share_identity(),))
    get("ann", "/api/vms-all?kinds=harvester,kvm")
    get("bob", "/api/vms-all?kinds=harvester,kvm")
    assert len(kvm_survey) == 4


def test_a_kvm_write_drops_the_shared_kvm_part(kvm_survey):
    get("ann", "/api/vms-all?kinds=harvester,kvm")
    wapp.READ_SHARE.invalidate(wapp.KVM_SCOPE)
    get("bob", "/api/vms-all?kinds=harvester,kvm")
    assert len(kvm_survey) == 4


# ---------------------------------------------------------------------------
# Revue finale D-4 : une lecture partielle n'est pas une lecture complète
# ---------------------------------------------------------------------------
OLD = "2026-01-01T00:00:00Z"                 # bien plus de 30 jours


def _seed_old_console_filing(path):
    sys.path.insert(0, str(ROOT / "bin" / "lib"))
    import folders_db as fdb
    conn = fdb.connect(path)
    try:
        fdb.ensure_path(conn, "/Prod", "console")
        # rangement « console » d'une VM d'un namespace que la personne ne lit pas
        fdb.upsert_membership(conn, "harvester/harv1/hidden/vm-x", "/Prod", "console", seen_at=OLD,
                              scope="harvester/harv1")
        return set(fdb.memberships(conn))
    finally:
        conn.close()


def _kept(path):
    import folders_db as fdb
    conn = fdb.connect(path)
    try:
        return "harvester/harv1/hidden/vm-x" in fdb.memberships(conn)
    finally:
        conn.close()


@pytest.mark.parametrize("who,denials,kept", [
    ("ann", [], True),              # session Rancher : lecture limitée à ses droits
    (None, ["forbidden: cannot list virtualmachines in the namespace hidden"], True),   # refus RBAC
    (None, [], False),              # kubeconfig de la console, tout lu : purge
])
def test_a_partial_read_never_purges_filings_it_cannot_see(sso, monkeypatch, tmp_path, who, denials, kept):
    db = tmp_path / "folders.db"
    monkeypatch.setenv("HARVESTER_OPS_FOLDERS_DB", str(db))
    _seed_old_console_filing(db)
    if who is None:
        monkeypatch.setattr(wapp, "_sso_session", lambda: None)
        monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: f"/k/{c}.yaml")
        monkeypatch.setattr(wapp, "current_cluster_identity", lambda: None)

    def collect(name, kc_path):
        return {"vms": [{"name": f"vm-{name}", "namespace": "ns", "key": f"harvester/{name}/ns/vm-{name}"}]}, \
            200, denials
    monkeypatch.setattr(wapp, "_vms_collect", collect)
    monkeypatch.setattr(wapp._kd, "load_all", lambda d: [])
    assert get(who or "local", "/api/vms-all?kinds=harvester,kvm").status_code == 200
    assert _kept(db) is kept
