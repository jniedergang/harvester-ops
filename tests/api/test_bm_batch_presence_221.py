"""2.2.1 : les séries de profils gardent les machines qui portent déjà un système.

* bm_presence.confirmed : la règle partagée par la fenêtre d'installation
  et les séries ;
* POST /api/baremetal/profiles/<name>/batch : chaque machine est relue par
  la console (la lecture de la fenêtre d'installation, _bm_system_presence) ;
  série mixte absent + présent confirmé + présent sans nom + inconnu ->
  409 avec la liste des machines, rien ne part ; nom faux refusé ; tout
  confirmé -> la série part, sans le nom dans les données ;
* POST /api/baremetal/profiles/<name>/system-check : lecture de chaque
  machine pour la fenêtre, mot de passe de la ligne sinon commun ;
* ligne de commande : --confirm-system HOST=NAME ;
* mutation volontaire : sans le contrôle côté serveur, la série part.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp  # noqa: E402
import bm_presence as bp  # noqa: E402
import test_bm_profiles_180 as t180  # noqa: E402  (profil, lignes et fixture `env` de la 1.80.0)

env = t180.env
TOKEN, BMCPW = t180.TOKEN, t180.BMCPW

# ligne -> état lu sur son BMC ; nom à taper = série fictive de la ligne
STATES = {"198.51.100.1": "absent", "198.51.100.2": "present", "198.51.100.3": "present",
          "198.51.100.4": "unknown"}


def serial(host):
    return "USE221" + host.rsplit(".", 1)[1]


def fake_presence(states, seen=None):
    def read(host, user, pwd, system_path=None):
        if seen is not None:
            seen.append((host, user, pwd))
        st = states[host]
        if st == "boom":
            raise RuntimeError(f"redfish {pwd}")
        return {"state": st, "confirm_name": serial(host), "source": "hpe-oem",
                "evidence": [{"code": "boots-from-disk", "name": "sles"}] if st == "present" else []}
    return read


def body(confirms=None, n=4):
    b = dict(t180.batch(n), token=TOKEN, bmc_password=BMCPW)
    for r in b["rows"]:
        if confirms and r["bmc_host"] in confirms:
            r["system_confirm"] = confirms[r["bmc_host"]]
    return b


# --- la règle partagée ---------------------------------------------------------------

@pytest.mark.parametrize("state,typed,ok", [
    ("absent", None, True),
    ("unknown", None, True),                 # le BMC ne dit rien : pas de refus
    ("present", None, False),
    ("present", "", False),
    ("present", "USE22", False),             # partiel
    ("present", "use2212", False),           # casse
    ("present", "USE2212", True),
    ("present", " USE2212 ", True),          # espaces autour tolérés
])
def test_confirmed(state, typed, ok):
    assert bp.confirmed({"state": state, "confirm_name": "USE2212"}, typed) is ok


# --- la série ------------------------------------------------------------------------

@pytest.mark.real_presence
def test_mixed_batch_is_refused_and_lists_every_machine(env, monkeypatch):
    seen = []
    monkeypatch.setattr(wapp, "_bm_system_presence", fake_presence(STATES, seen))
    t180.create(env["client"])
    r = env["client"].post("/api/baremetal/profiles/rack-a/batch",
                           json=body({"198.51.100.2": serial("198.51.100.2")}))
    d = r.get_json()
    assert r.status_code == 409, d
    assert d["fields"] == ["system_confirm"] and d["refused"] == ["198.51.100.3"]
    assert [(m["row"], m["bmc_host"], m["state"], m["confirmed"]) for m in d["machines"]] == [
        (1, "198.51.100.1", "absent", True), (2, "198.51.100.2", "present", True),
        (3, "198.51.100.3", "present", False), (4, "198.51.100.4", "unknown", True)]
    assert d["machines"][2]["confirm_name"] == "USE2213" and d["machines"][2]["evidence"]
    assert env["started"] == []                                  # rien n'est parti
    assert sorted(h for h, _, _ in seen) == sorted(STATES)       # chaque machine relue
    text = r.get_data(as_text=True)
    assert TOKEN not in text and BMCPW not in text


@pytest.mark.real_presence
@pytest.mark.parametrize("typed", ["USE2213 x", "use2213", "USE221", "198.51.100.3", ""])
def test_a_wrong_typed_name_is_refused(env, monkeypatch, typed):
    monkeypatch.setattr(wapp, "_bm_system_presence", fake_presence(STATES))
    t180.create(env["client"])
    r = env["client"].post("/api/baremetal/profiles/rack-a/batch", json=body(
        {"198.51.100.2": serial("198.51.100.2"), "198.51.100.3": typed}))
    assert r.status_code == 409 and r.get_json()["refused"] == ["198.51.100.3"]
    assert env["started"] == []


@pytest.mark.real_presence
def test_a_confirmed_batch_starts_without_the_typed_names(env, monkeypatch):
    monkeypatch.setattr(wapp, "_bm_system_presence", fake_presence(STATES))
    t180.create(env["client"])
    r = env["client"].post("/api/baremetal/profiles/rack-a/batch", json=body(
        {"198.51.100.2": serial("198.51.100.2"), "198.51.100.3": "  USE2213 "}))
    assert r.status_code == 202, r.get_json()
    [(label, _, (plan, _conc))] = env["started"]
    assert len(plan) == 4 and all("system_confirm" not in p for p in plan)


@pytest.mark.real_presence
def test_row_errors_come_before_any_bmc_read(env, monkeypatch):
    seen = []
    monkeypatch.setattr(wapp, "_bm_system_presence", fake_presence(STATES, seen))
    t180.create(env["client"])
    r = env["client"].post("/api/baremetal/profiles/rack-a/batch", json=dict(body(), token=""))
    assert r.status_code == 400 and ["token", "missing"] in r.get_json()["errors"]
    assert seen == []


@pytest.mark.real_presence
def test_mutation_without_the_server_check_the_batch_goes(env, monkeypatch):
    """Mutation volontaire : sans le contrôle côté serveur, une série qui
    compte une machine portant un système sans nom tapé part quand même."""
    monkeypatch.setattr(wapp, "_bm_system_presence", fake_presence(STATES))
    monkeypatch.setattr(wapp, "_bm_batch_presence_refusal", lambda plan, confirms: ([], []))
    t180.create(env["client"])
    r = env["client"].post("/api/baremetal/profiles/rack-a/batch", json=body())
    with pytest.raises(AssertionError):
        assert r.status_code == 409 and env["started"] == []


def test_mutation_a_rule_that_ignores_presence_is_seen(monkeypatch):
    """Mutation volontaire : une règle qui confirme tout laisserait passer
    les deux machines qui portent un système."""
    monkeypatch.setattr(bp, "confirmed", lambda check, typed: True)
    monkeypatch.setattr(wapp, "_bm_system_presence", fake_presence(STATES))
    machines, refused = wapp._bm_batch_presence_refusal(
        [{"bmc_host": h, "bmc_user": "u", "bmc_password": "p"} for h in STATES], {})
    with pytest.raises(AssertionError):
        assert refused == ["198.51.100.2", "198.51.100.3"]


@pytest.mark.real_presence
def test_a_failing_read_is_unknown_and_leaks_nothing(monkeypatch):
    monkeypatch.setattr(wapp, "_bm_system_presence", fake_presence({"192.0.2.1": "boom", "192.0.2.2": "present"}))
    got = wapp._bm_batch_presence([(1, "192.0.2.1", "u", "SECRET-221"), (2, "192.0.2.2", "u", "p")])
    assert [(m["row"], m["state"]) for m in got] == [(1, "unknown"), (2, "present")]
    assert "SECRET-221" not in repr(got)
    assert wapp._bm_batch_presence([]) == []


# --- lecture pour la fenêtre -----------------------------------------------------------

@pytest.mark.real_presence
def test_system_check_route_reads_each_machine(env, monkeypatch):
    seen = []
    monkeypatch.setattr(wapp, "_bm_system_presence", fake_presence(STATES, seen))
    t180.create(env["client"])
    b = body()
    b["rows"][1]["bmc_password"] = "ROW2-221"
    r = env["client"].post("/api/baremetal/profiles/rack-a/system-check", json=b)
    assert r.status_code == 200
    d = r.get_json()
    assert [(m["row"], m["state"]) for m in d["machines"]] == [
        (1, "absent"), (2, "present"), (3, "present"), (4, "unknown")]
    assert dict((h, p) for h, _, p in seen) == {"198.51.100.1": BMCPW, "198.51.100.2": "ROW2-221",
                                                 "198.51.100.3": BMCPW, "198.51.100.4": BMCPW}
    text = r.get_data(as_text=True)
    assert BMCPW not in text and "ROW2-221" not in text and TOKEN not in text
    assert env["started"] == []


@pytest.mark.parametrize("rows,status,fields", [
    ([], 400, ["rows"]),
    ([{"bmc_host": "u@192.0.2.1"}], 400, ["row 1 bmc_host"]),
    ([{"bmc_host": "192.0.2.1"}, {"bmc_host": "192.0.2.1/x"}], 400, ["row 2 bmc_host"]),
    (["x"], 400, ["row 1 bmc_host"]),
])
def test_system_check_route_refuses_bad_rows(env, rows, status, fields):
    t180.create(env["client"])
    r = env["client"].post("/api/baremetal/profiles/rack-a/system-check", json={"rows": rows})
    assert r.status_code == status and r.get_json()["fields"] == fields


def test_system_check_route_unknown_profile(env):
    r = env["client"].post("/api/baremetal/profiles/nope/system-check", json=body())
    assert r.status_code == 404


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_system_check_route_is_for_administrators(env, monkeypatch, role):
    monkeypatch.setattr(wapp, "current_role", lambda: role)
    assert env["client"].post("/api/baremetal/profiles/rack-a/system-check", json=body()).status_code == 403


def test_system_check_route_is_authenticated_and_rate_limited():
    from limits import parse_many
    src = (ROOT / "web" / "app.py").read_text()
    head = src.split('@app.route("/api/baremetal/profiles/<name>/system-check"', 1)[1].split("\ndef ", 1)[0]
    assert "@requires_auth" in head
    assert parse_many(re.search(r'@_rate_limit\("([^"]+)"\)', head).group(1))


# --- ligne de commande -----------------------------------------------------------------

def test_cli_passes_and_checks_the_confirmations(tmp_path, monkeypatch, capsys):
    state, nodes, sec = t180._cli_env(tmp_path)
    seen = {}

    def fake_start(prof, batch, secrets_, concurrency, confirms=None):
        seen["confirms"] = confirms
        return None, None, [
            {"row": 1, "bmc_host": "198.51.100.1", "state": "absent", "evidence": [], "confirm_name": "S1",
             "confirmed": True},
            {"row": 2, "bmc_host": "198.51.100.2", "state": "present", "confirm_name": "USE2212",
             "evidence": [{"code": "boots-from-disk", "name": "sles"}], "confirmed": False}]
    monkeypatch.setattr(wapp, "_bm_batch_start", fake_start)
    monkeypatch.setitem(sys.modules, "app", wapp)
    base = ["profile", "--state-dir", str(state), "apply", "rack-a", "--nodes", str(nodes),
            "--cluster-name", "rack-a", "--vip", "192.0.2.100", "--secrets-file", str(sec)]
    code = t180._cli().main(base + ["--confirm-system", "198.51.100.1=S1"])
    err = capsys.readouterr().err
    assert code == 2 and seen["confirms"] == {"198.51.100.1": "S1"}
    assert "--confirm-system 198.51.100.2=USE2212" in err and "nothing was started" in err
    assert "row 1 198.51.100.1: absent\n" in err
    assert t180._cli().main(base + ["--confirm-system", "no-equal-sign"]) == 2
