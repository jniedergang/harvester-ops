"""2.10.0 (tâche 9) : `harvester-secrets eso` (status, listener, assistant,
add, keys, remove). B1 : demandes exactes à l'agent (écouteur allumé au
premier cluster, éteint au dernier) ; mode A : commandes `bao` exactes ;
réglages sans valeur ; JWKS du cluster enfant par le fichier de
spécification de l'appelant (`harvester-capi`) ou par son kubeconfig."""

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_harvester_secrets_cli_290 import CID, PREFIX, cli, creds, env, hsc, read_settings, settings  # noqa: E402,F401

import openbao_jwt as oj  # noqa: E402
import openbao_policy as pol  # noqa: E402

ISS = "https://kubernetes.default.svc.cluster.local"
KEY = "1d905023"
REF = "harvlab2/c210/c210"
LISTEN = "172.16.1.11:8212"
ESO_CA = "-----BEGIN CERTIFICATE-----\nRVNPQ0E=\n-----END CERTIFICATE-----\n"


def _b64(i):
    return base64.urlsafe_b64encode(i.to_bytes((i.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()


JWKS = {"keys": [{"kty": "RSA", "kid": "k8s-1", "use": "sig", "alg": "RS256", "n": _b64((1 << 2047) + 99), "e": "AQAB"}]}
JWKS2 = {"keys": JWKS["keys"] + [{"kty": "RSA", "kid": "k8s-2", "use": "sig", "n": _b64((1 << 2047) + 77), "e": "AQAB"}]}


@pytest.fixture
def spec(tmp_path):
    def make(jwks=JWKS, issuer=ISS):
        f = tmp_path / "spec.json"
        f.write_text(json.dumps({"eso_jwks": json.dumps(jwks), "eso_issuer": issuer}))
        f.chmod(0o600)
        return str(f)
    return make


@pytest.fixture
def agent(monkeypatch):
    sent = []

    def run(req, timeout=None):
        sent.append(req)
        info = {}
        if req["op"] == "b1-eso-listener":
            info = {"eso_listener": req["address"], "eso_ca_pem": ESO_CA if req["address"] else None}
        return {"state": "done", "info": info}
    monkeypatch.setattr(hsc, "run_agent", run)
    return sent


def b1(env, **kw):
    creds(env)
    return settings(env, mode="b1", **kw)


def add(capsys, specfile, *more, ns=("team-a", "team-b")):
    argv = ["eso", "add", "--cluster", REF, "--spec", specfile, "--node-cidr", "172.16.2.0/24"]
    for n in ns:
        argv += ["--namespace", n]
    return cli(argv + list(more), capsys=capsys)


# ---------------------------------------------------------------------------
# Écouteur
# ---------------------------------------------------------------------------
def test_listener_is_b1_only(env, capsys, agent):
    creds(env)
    settings(env)
    rc, doc, _ = cli(["eso", "listener", "--addr", LISTEN], capsys=capsys)
    assert rc == 2 and doc["code"] == "eso-listener-b1-only" and agent == []


@pytest.mark.parametrize("args", [[], ["--addr", LISTEN, "--off"], ["--addr", "127.0.0.1:8212"],
                                  ["--addr", "0.0.0.0:8212"], ["--addr", "172.16.1.11:8210"]])
def test_listener_bad_input(env, capsys, agent, args):
    b1(env)
    rc, doc, _ = cli(["eso", "listener", *args], capsys=capsys)
    assert rc == 2 and doc["code"] == "bad-input" and agent == []


def test_listener_address_kept_off_until_the_first_cluster(env, capsys, agent, spec):
    """Point 298 : éteint tant qu'aucun cluster n'a ESO."""
    b1(env)
    rc, doc, _ = cli(["eso", "listener", "--addr", LISTEN], capsys=capsys)
    assert rc == 0 and doc["listener"] is None and agent == []
    assert read_settings(env)["eso"]["address"] == LISTEN
    assert add(capsys, spec())[0] == 0
    assert [r["op"] for r in agent] == ["b1-eso-listener", "b1-eso-cluster"]
    assert agent[0]["address"] == LISTEN


# ---------------------------------------------------------------------------
# Pose d'un cluster enfant
# ---------------------------------------------------------------------------
def test_add_b1_without_listener_address_is_refused(env, capsys, agent, spec):
    b1(env)
    rc, doc, _ = add(capsys, spec())
    assert rc == 2 and doc["code"] == "eso-listener-needed" and agent == []
    assert not read_settings(env).get("eso", {}).get("clusters")


def test_add_b1_asks_the_agent_and_returns_the_store_parameters(env, capsys, agent, spec):
    b1(env)
    rc, doc, err = add(capsys, spec(), "--listen", LISTEN)
    assert rc == 0, doc
    assert agent[0] == {"op": "b1-eso-listener", "address": LISTEN}
    assert agent[1] == {"op": "b1-eso-cluster", "key": KEY, "harvester": "harvlab2", "namespace": "c210",
                        "cluster": "c210", "issuer": ISS, "pubkeys": oj.pubkeys_of(JWKS),
                        "namespaces": ["team-a", "team-b"], "node_cidr": "172.16.2.0/24"}
    assert doc["store"] == {"server": f"https://{LISTEN}", "ca_pem": ESO_CA, "kv_mount": "harvester-ops",
                            "prefix": PREFIX, "jwt_mount": f"jwt-hops-{CID}-eso-{KEY}",
                            "audience": f"hops-openbao-{CID}"}
    assert doc["applied"] is True and doc["commands"] is None
    e = read_settings(env)["eso"]
    assert e["listener"] == LISTEN and e["ca_pem"] == ESO_CA
    c = e["clusters"][KEY]
    assert (c["harvester"], c["namespace"], c["cluster"]) == ("harvlab2", "c210", "c210")
    assert c["keys_sha256"] == oj.keys_sha256(JWKS) and c["namespaces"] == ["team-a", "team-b"]
    assert "BEGIN PUBLIC KEY" not in json.dumps(c)                       # clés dans l'état de root seulement
    rc, doc, _ = add(capsys, spec(), ns=("team-a",))                      # namespace retiré : rôle réécrit
    assert rc == 0 and [r["op"] for r in agent] == ["b1-eso-listener", "b1-eso-cluster", "b1-eso-cluster"]
    assert agent[2]["namespaces"] == ["team-a"]


def test_add_mode_a_renders_the_commands(env, capsys, agent, spec):
    creds(env)
    settings(env)
    rc, doc, _ = add(capsys, spec())
    assert rc == 0, doc
    assert agent == [] and doc["applied"] is False
    assert doc["commands"] == pol.eso_cluster_commands(env["srv"].addr, "harvester-ops", PREFIX, CID, KEY, ISS,
                                                       oj.pubkeys_of(JWKS), ["team-a", "team-b"], "172.16.2.0/24")
    assert read_settings(env)["eso"]["clusters"][KEY]["applied"] is False
    assert doc["store"]["server"] == env["srv"].addr


@pytest.mark.parametrize("ns,code", [(("kube-system",), "eso-system-namespace"), ((), "eso-no-namespace"),
                                     (("Team_A",), "eso-namespace-invalid")])
def test_add_refuses_system_or_missing_namespaces(env, capsys, agent, spec, ns, code):
    b1(env)
    rc, doc, _ = add(capsys, spec(), "--listen", LISTEN, ns=ns)
    assert rc == 2 and doc["code"] == code and agent == []


@pytest.mark.parametrize("cluster", ["c210", "harvlab2/c210", "harvlab2/C210/c210", "../x/y/z"])
def test_add_bad_cluster(env, capsys, agent, spec, cluster):
    b1(env)
    rc, doc, _ = cli(["eso", "add", "--cluster", cluster, "--namespace", "team-a", "--spec", spec()],
                     capsys=capsys)
    assert rc == 2 and doc["code"] == "bad-input"


def test_add_bad_jwks(env, capsys, agent, spec):
    b1(env)
    rc, doc, _ = add(capsys, spec(jwks={"keys": []}), "--listen", LISTEN)
    assert rc == 1 and doc["code"] == "eso-child-keys-unreadable" and agent == []


def test_add_needs_openbao(env, capsys, agent, spec):
    settings(env, mode=None)
    rc, doc, _ = add(capsys, spec())
    assert rc == 2 and doc["code"] == "openbao-inactive"


def test_assistant_writes_nothing(env, capsys, agent, spec):
    creds(env)
    settings(env)
    rc, doc, _ = cli(["eso", "assistant", "--cluster", REF, "--namespace", "team-a", "--spec", spec()], capsys=capsys)
    assert rc == 0 and doc["commands"] and agent == []
    assert not read_settings(env).get("eso")


def test_child_keys_read_with_the_harvester_kubeconfig(env, capsys, agent, monkeypatch, tmp_path):
    """Sans fichier de spécification : le kubeconfig du cluster enfant lu
    avec celui du cluster Harvester, puis `get --raw` (accès anonyme refusé
    par RKE2)."""
    import child_access
    import contextlib
    import kube
    b1(env)
    calls = []

    class K:
        def __init__(self, path):
            self.path = path

        def run(self, *a, **kw):
            calls.append((self.path, a))
            return json.dumps({"issuer": ISS} if a[-1].endswith("openid-configuration") else JWKS)

    @contextlib.contextmanager
    def session(kube_, ns, c):
        calls.append(("session", ns, c))
        yield "/work/child.kubeconfig"
    monkeypatch.setattr(kube, "Kube", K)
    monkeypatch.setattr(child_access, "session", session)
    rc, doc, _ = cli(["eso", "add", "--cluster", REF, "--namespace", "team-a", "--kubeconfig", "/work/h.kubeconfig",
                      "--listen", LISTEN], capsys=capsys)
    assert rc == 0, doc
    assert calls[0] == ("session", "c210", "c210")
    assert [a for _p, a in calls[1:]] == [("get", "--raw", "/.well-known/openid-configuration"),
                                          ("get", "--raw", "/openid/v1/jwks")]


# ---------------------------------------------------------------------------
# Clés, retrait, état
# ---------------------------------------------------------------------------
def test_keys_compare_then_refresh(env, capsys, agent, spec):
    b1(env)
    assert add(capsys, spec(), "--listen", LISTEN)[0] == 0
    rc, doc, _ = cli(["eso", "keys", "--cluster", REF, "--spec", spec()], capsys=capsys)
    assert rc == 0 and doc["changed"] is False
    rc, doc, _ = cli(["eso", "keys", "--cluster", REF, "--spec", spec(JWKS2)], capsys=capsys)
    assert rc == 0 and doc["changed"] is True and doc["code"] == "eso-keys-changed"
    n = len(agent)
    rc, doc, _ = cli(["eso", "keys", "--cluster", REF, "--refresh", "--spec", spec(JWKS2)], capsys=capsys)
    assert rc == 0, doc
    assert agent[n]["op"] == "b1-eso-cluster" and agent[n]["pubkeys"] == oj.pubkeys_of(JWKS2)
    assert agent[n]["namespaces"] == ["team-a", "team-b"]
    assert read_settings(env)["eso"]["clusters"][KEY]["keys_sha256"] == oj.keys_sha256(JWKS2)


def test_keys_unknown_cluster(env, capsys, agent, spec):
    b1(env)
    rc, doc, _ = cli(["eso", "keys", "--cluster", REF, "--spec", spec()], capsys=capsys)
    assert rc == 2 and doc["code"] == "eso-cluster-unknown"


def test_remove_last_cluster_turns_the_listener_off(env, capsys, agent, spec):
    b1(env)
    assert add(capsys, spec(), "--listen", LISTEN)[0] == 0
    rc, doc, _ = cli(["eso", "listener", "--off"], capsys=capsys)
    assert rc == 2 and doc["code"] == "eso-listener-in-use"
    rc, doc, _ = cli(["eso", "listener", "--addr", "172.16.1.12:8212"], capsys=capsys)
    assert rc == 2 and doc["code"] == "eso-listener-in-use"
    n = len(agent)
    rc, doc, _ = cli(["eso", "remove", "--cluster", REF], capsys=capsys)
    assert rc == 0, doc
    assert agent[n:] == [{"op": "b1-eso-cluster-remove", "key": KEY}, {"op": "b1-eso-listener", "address": None}]
    e = read_settings(env)["eso"]
    assert e["clusters"] == {} and e["listener"] is None and e["address"] == LISTEN and e["ca_pem"] is None


def test_remove_all_mode_a_commands(env, capsys, agent, spec):
    creds(env)
    settings(env)
    assert add(capsys, spec())[0] == 0
    rc, doc, _ = cli(["eso", "remove", "--all"], capsys=capsys)
    assert rc == 0 and doc["removed"] == [KEY]
    assert doc["commands"] == pol.eso_remove_commands(env["srv"].addr, CID, KEY)
    assert agent == []


def test_status_without_values(env, capsys, agent, spec):
    b1(env)
    assert add(capsys, spec(), "--listen", LISTEN)[0] == 0
    rc, doc, _ = cli(["eso", "status"], capsys=capsys)
    assert rc == 0 and doc["listener"] == LISTEN and list(doc["clusters"]) == [KEY]
    assert "BEGIN" not in json.dumps(doc)


def test_add_binds_the_role_to_the_account_uids(env, capsys, agent, spec):
    """Revue 8-10 I-3 : `--sa-uid` passe à l'agent, est gardé dans les
    réglages et repris par `eso keys --refresh`."""
    b1(env)
    u = "11111111-2222-3333-4444-555555555555"
    rc, doc, _ = add(capsys, spec(), "--listen", LISTEN, "--sa-uid", f"team-a={u}")
    assert rc == 0, doc
    assert agent[-1]["sa_uids"] == {"team-a": u}
    assert read_settings(env)["eso"]["clusters"][KEY]["sa_uids"] == {"team-a": u}
    rc, doc, _ = add(capsys, spec(), "--sa-uid", "team-c=" + u)
    assert rc == 2 and doc["code"] == "bad-input"
    rc, doc, _ = add(capsys, spec(), "--sa-uid", "team-a")
    assert rc == 2 and doc["code"] == "bad-input"


# ---------------------------------------------------------------------------
# Revue 8-10 M-3 : après une restauration, les réglages suivent ce que
# l'instantané porte (l'agent le dit)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("info,left", [
    ({"person_jwt": ["rmgr"], "person_groups": ["ops"], "eso_clusters": [KEY], "eso_listener": LISTEN},
     {"servers": ["rmgr"], "pending": [], "groups": ["ops"], "eso": [KEY], "listener": LISTEN, "enabled": True}),
    ({"person_jwt": [], "person_groups": [], "eso_clusters": [], "eso_listener": None},
     {"servers": [], "pending": [], "groups": [], "eso": [], "listener": None, "enabled": False}),
    (None, {"servers": ["rmgr"], "pending": ["lab"], "groups": ["ops"], "eso": [KEY], "listener": LISTEN,
            "enabled": True}),                                                   # agent d'avant : rien rangé
])
def test_restore_reconciles_the_settings(env, capsys, monkeypatch, info, left):
    settings(env, mode="b1", person_token={"enabled": True, "servers": {"rmgr": {"mount": "m"}},
                                           "pending": {"lab": {"mount": "m2"}}, "groups": {"ops": {}}},
             eso={"listener": LISTEN, "clusters": {KEY: {"namespaces": ["team-a"]}}})
    monkeypatch.setattr(hsc, "run_agent", lambda req, timeout=None: {"state": "done", "message": "restored",
                                                                     **({"info": info} if info else {})})
    rc, doc, _ = cli(["restore", "openbao-20261108T020000Z.snap"], capsys=capsys)
    assert rc == 0, doc
    s = read_settings(env)
    pt, e = s["person_token"], s["eso"]
    got = {"servers": sorted(pt["servers"]), "pending": sorted(pt.get("pending") or {}),
           "groups": sorted(pt.get("groups") or {}), "eso": sorted(e["clusters"]), "listener": e["listener"],
           "enabled": pt["enabled"]}
    assert got == left


# ---------------------------------------------------------------------------
# Tâche 13 (hops-upd) : une restauration lancée depuis la console arrête la
# console, donc l'outil qui l'a lancée, avant qu'il range les réglages.
# `restore-settle` les range d'après l'état final de l'agent (status.json),
# une seule fois par restauration (marque `restore_settled`).
# ---------------------------------------------------------------------------
def _agent_status(op="restore-snapshot", state="done", started=1794195841.5, info=None):
    d = hsc.state() / "secrets-agent"
    d.mkdir(parents=True, exist_ok=True)
    doc = {"op": op, "state": state, "started": started}
    if info is not None:
        doc["info"] = info
    (d / "status.json").write_text(json.dumps(doc))


EMPTY = {"person_jwt": [], "person_groups": [], "eso_clusters": [], "eso_listener": None}


def _seed_restored(env):
    settings(env, mode="b1", person_token={"enabled": True, "servers": {"rmgr": {"mount": "m"}}, "pending": {},
                                           "groups": {}}, eso={"listener": LISTEN, "clusters": {KEY: {}}})


@pytest.mark.parametrize("status,pruned", [
    ({"info": EMPTY}, True),
    ({"info": EMPTY, "state": "running"}, False),
    ({"info": EMPTY, "state": "failed"}, False),
    ({"info": EMPTY, "op": "b1-eso-listener"}, False),
    ({}, False),                                                              # agent d'avant : pas d'info
])
def test_restore_settle_prunes_from_the_final_agent_status(env, capsys, status, pruned):
    _seed_restored(env)
    _agent_status(**status)
    rc, doc, _ = cli(["restore-settle"], capsys=capsys)
    assert rc == 0, doc
    s = read_settings(env)
    assert (s["person_token"]["servers"] == {} and s["eso"]["clusters"] == {} and s["eso"]["listener"] is None
            and s["person_token"]["enabled"] is False) is pruned
    assert bool(doc.get("settings_pruned")) is pruned
    assert (s.get("restore_settled") == 1794195841.5) is pruned


def test_restore_settle_runs_once_per_restore(env, capsys):
    _seed_restored(env)
    _agent_status(info=EMPTY)
    assert cli(["restore-settle"], capsys=capsys)[0] == 0
    _seed_restored(env)                                                        # posé de nouveau depuis
    s = read_settings(env)
    s["restore_settled"] = 1794195841.5
    (hsc.state() / "secrets-settings.json").write_text(json.dumps(s))
    rc, doc, _ = cli(["restore-settle"], capsys=capsys)
    assert rc == 0 and not doc.get("settings_pruned")
    assert read_settings(env)["person_token"]["servers"] == {"rmgr": {"mount": "m"}}


def test_restore_by_the_tool_records_its_settle_mark(env, capsys, monkeypatch):
    _seed_restored(env)
    monkeypatch.setattr(hsc, "run_agent", lambda req, timeout=None: {"state": "done", "started": 42.0,
                                                                     "info": EMPTY})
    rc, doc, _ = cli(["restore", "openbao-20261108T020000Z.snap"], capsys=capsys)
    assert rc == 0 and doc["settings_pruned"]
    assert read_settings(env)["restore_settled"] == 42.0
