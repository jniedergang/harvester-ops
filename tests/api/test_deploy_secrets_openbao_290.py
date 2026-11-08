"""2.9.0, tâche 10 : secrets d'un plan gardés dans OpenBao (décision 141,
point 262).

* OpenBao actif : `plan-run` écrit dans OpenBao, relu par une référence
  `local:` (les lecteurs de la 2.4.0 ne changent pas), effacé par
  `metadata delete` de chaque feuille, versions comprises ;
* un déroulé arrêté reprenable garde ses secrets, un déroulé fini (ou un
  essai, ou une exception) les efface ;
* la reprise les lit au lieu de les redemander, puis efface l'arbre du
  déroulé repris ; OpenBao coupé : `openbao-unreachable` et proposition de
  saisir les secrets (comportement 2.4.0) ; secrets tapés prioritaires ;
* balayage (démarrage et tâche de fond) : seuls les déroulés finis, les
  essais, les reprenables plus vieux que le délai (7 jours) et les orphelins
  anciens perdent leurs secrets ; un déroulé interrompu par une console
  arrêtée garde les siens ;
* OpenBao inactif : la mémoire de la 2.4.0.
"""

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import deploy_chain as dch  # noqa: E402
import openbao_client as oc  # noqa: E402
import openbao_policy as pol  # noqa: E402
import secret_store as ss  # noqa: E402
from fake_openbao import FakeOpenBao  # noqa: E402
from test_deploy_routes_240 import env  # noqa: E402,F401

import app as wapp  # noqa: E402

CID = "abc123def456"
PREFIX = f"consoles/{CID}"
TOKEN, PASSWORD, BMC1, BMC2, GITPW = "K10-tok-290", "pw-290", "bmc-a-290", "bmc-b-290", "git-pw-290"
PREV, NEW = "a1a1a1a1a1a1", "b2b2b2b2b2b2"


@pytest.fixture
def bao(tmp_path, monkeypatch):
    srv = FakeOpenBao().start()
    admin = srv.add_token(policies=[pol.console_role(CID)])
    state, etc = tmp_path / "bstate", tmp_path / "betc"
    state.mkdir()
    etc.mkdir()
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(state))
    monkeypatch.setenv("HARVESTER_OPS_OPENBAO_DIR", str(etc))
    doc = {"mode": "a", "addr": srv.addr, "console_id": CID, "timeout": 5}
    (state / ss.SETTINGS_FILE).write_text(json.dumps(doc))

    def factory(actor=None, action=None):
        c = oc.Client(srv.addr, actor=actor or "service", action=action, retry_delays=(0.01,))
        c.set_token(admin)
        return c
    st = ss.MultiStore(state, client_factory=factory)
    st.SETTINGS_RECHECK_S = 0
    monkeypatch.setattr(ss, "_STORE", st)
    monkeypatch.setattr(wapp, "ACTIONS_DB", tmp_path / "actions.db")
    wapp._actions_init_db()
    yield {"srv": srv, "store": st, "state": state, "doc": doc}
    srv.stop()


def kv_paths(srv, run_id):
    return sorted(p for (_m, p) in srv.kv if p.startswith(f"{PREFIX}/plan-run/{run_id}/"))


def rows():
    return [{"hostname": "n1", "bmc_host": "10.0.0.1", "bmc_password": BMC1, "token": TOKEN, "password": PASSWORD},
            {"hostname": "n2", "bmc_host": "10.0.0.2", "bmc_password": BMC2, "token": TOKEN, "password": PASSWORD}]


def keep(store, rid=PREV, gitops=True):
    wapp._deploy_install_secrets_put(store, rid, rows())
    if gitops:
        wapp._deploy_gitops_secrets_put(store, rid, {"repo1": {"username": "git", "password": GITPW}})


# ---------------------------------------------------------------------------
# Magasin
# ---------------------------------------------------------------------------
def test_plan_run_goes_to_openbao_and_reads_back_by_local_ref(bao):
    st, srv = bao["store"], bao["srv"]
    r = rows()
    wapp._deploy_install_secrets_put(st, PREV, r)
    assert all(k not in o for o in r for k in ("bmc_password", "token", "password"))
    assert kv_paths(srv, PREV) == [f"{PREFIX}/plan-run/{PREV}/bmc/0", f"{PREFIX}/plan-run/{PREV}/bmc/1",
                                   f"{PREFIX}/plan-run/{PREV}/install/0", f"{PREFIX}/plan-run/{PREV}/install/1"]
    st.clear_cache()
    back = wapp._deploy_install_secrets_get(st, PREV, [{"hostname": "n1"}, {"hostname": "n2"}])
    assert [o["bmc_password"] for o in back] == [BMC1, BMC2] and back[0]["token"] == TOKEN
    meta = srv.kv[("harvester-ops", f"{PREFIX}/plan-run/{PREV}/bmc/0")]["custom"]
    assert meta.get("hops_class") == "plan-run" and BMC1 not in json.dumps(meta)


def test_delete_tree_removes_every_version(bao):
    st, srv = bao["store"], bao["srv"]
    keep(st)
    wapp._deploy_install_secrets_put(st, PREV, rows())          # seconde version des mêmes clés
    assert srv.kv[("harvester-ops", f"{PREFIX}/plan-run/{PREV}/bmc/0")]["current"] > 2
    n0 = len(srv.log)
    st.delete_tree(dch.SECRET_CLASS, PREV)
    assert kv_paths(srv, PREV) == []
    deletes = [r for r in srv.log[n0:] if r["method"] == "DELETE"]
    assert deletes and all("/metadata/" in r["path"] for r in deletes)
    assert st.refs(dch.SECRET_CLASS, PREV, backend="openbao") == []          # LIST vide


def test_inactive_openbao_keeps_the_240_memory(bao):
    st, srv = bao["store"], bao["srv"]
    (bao["state"] / ss.SETTINGS_FILE).write_text(json.dumps(dict(bao["doc"], mode="local")))
    keep(st)
    assert kv_paths(srv, PREV) == []
    assert wapp._deploy_install_secrets_get(st, PREV, [{}])[0]["token"] == TOKEN
    st.delete_tree(dch.SECRET_CLASS, PREV)
    assert wapp._deploy_install_secrets_get(st, PREV, [{}])[0].get("token") is None


def test_secrets_kept_in_memory_while_openbao_is_down(bao):
    """OpenBao coupé à l'écriture : gardés en mémoire (2.4.0) ; un champ
    absent du même secret ne fait pas appel à OpenBao."""
    st, srv = bao["store"], bao["srv"]
    srv.sealed = True
    wapp._deploy_gitops_secrets_put(st, PREV, {"repo1": {"username": "git", "password": GITPW}})
    assert wapp._deploy_gitops_secret_get(st, PREV, "repo1") == {"username": "git", "password": GITPW}
    with pytest.raises(ss.SecretError):                  # rien en mémoire : OpenBao, coupé, est consulté
        st.get(ss.make_ref(dch.SECRET_CLASS, f"{PREV}/gitops/other", "password"))


# ---------------------------------------------------------------------------
# Fin d'un déroulé
# ---------------------------------------------------------------------------
class Ctx:
    def __init__(self, rid):
        self.run_id, self.steps, self.result = rid, [], None

    def emit_step(self, *a):
        self.steps.append(a)

    def set_result(self, r):
        self.result = r

    def cancelled(self):
        return False


def one_phase(status):
    def execute(ctx, state):
        if status == "raise":
            raise RuntimeError("boom")
        return dch.PhaseOutcome(status)
    return [dch.Phase("readiness", execute=execute)]


@pytest.mark.parametrize("outcome,dry,kept", [
    ("done", False, False),
    ("error", False, True),
    ("partial", False, True),
    ("error", True, False),
])
def test_end_of_run_keeps_only_a_resumable_run(bao, outcome, dry, kept):
    st, srv = bao["store"], bao["srv"]
    keep(st)
    run = wapp.ActionRun(PREV, "deploy:harvlab", "harvlab", [], dry_run=dry)
    dch.run(Ctx(PREV), one_phase(outcome), secrets=st, sleep=lambda s: None,
            keep_secrets=lambda status: wapp._deploy_keep_secrets(st, run, status))
    assert bool(kv_paths(srv, PREV)) is kept


def test_an_exception_erases_the_secrets(bao):
    st, srv = bao["store"], bao["srv"]
    keep(st)
    run = wapp.ActionRun(PREV, "deploy:harvlab", "harvlab", [])
    res = dch.run(Ctx(PREV), one_phase("raise"), secrets=st, sleep=lambda s: None,
                  keep_secrets=lambda status: wapp._deploy_keep_secrets(st, run, status))
    assert res["status"] == "error"
    # phase en exception = `error` reprenable : gardé ; exception hors phase : effacé
    assert kv_paths(srv, PREV)
    with pytest.raises(ValueError):
        dch.run(type("C", (), {"run_id": "bad"})(), [], secrets=st, keep_secrets=lambda s: True)


@pytest.mark.parametrize("dry,status,active,expected", [
    (False, "error", True, True), (False, "partial", True, True), (False, "cancelled", True, True),
    (False, "done", True, False), (True, "error", True, False), (False, "error", False, False),
])
def test_keep_rule(bao, dry, status, active, expected):
    if not active:
        (bao["state"] / ss.SETTINGS_FILE).write_text(json.dumps(dict(bao["doc"], mode="local")))
    run = wapp.ActionRun(PREV, "deploy:x", "x", [], dry_run=dry)
    assert wapp._deploy_keep_secrets(bao["store"], run, status) is expected


# ---------------------------------------------------------------------------
# Reprise
# ---------------------------------------------------------------------------
def test_fill_reads_kept_secrets(bao):
    keep(bao["store"])
    s, g, src, err = wapp._deploy_resume_fill(bao["store"], PREV, {}, {})
    assert err is None and src == "openbao"
    assert s["token"] == TOKEN and s["password"] == PASSWORD
    assert s["bmc_passwords"] == {"10.0.0.1": BMC1, "10.0.0.2": BMC2}
    assert g == {"repo1": {"username": "git", "password": GITPW}}


def test_typed_secrets_win(bao):
    keep(bao["store"])
    s, g, src, _ = wapp._deploy_resume_fill(bao["store"], PREV, {"token": "typed", "bmc_password": "typed-bmc"},
                                            {"repo1": {"identity": "typed-key"}})
    assert s == {"token": "typed", "bmc_password": "typed-bmc"} and g == {"repo1": {"identity": "typed-key"}}
    assert src is None


def test_fill_when_openbao_is_down(bao):
    keep(bao["store"])
    bao["store"].clear_cache()
    bao["srv"].sealed = True
    s, g, src, err = wapp._deploy_resume_fill(bao["store"], PREV, {}, {})
    assert (s, g, src, err) == ({}, {}, None, "openbao-sealed")


def prev_run(rid=PREV, status="error", ended=None, dry=False, install=True):
    run = wapp.ActionRun(rid, "deploy:rack-a", "rack-a", [], dry_run=dry)
    run.status = status
    run.ended_at = ended if ended is not None else time.time()
    run.result = {"plan": "site", "phases": [{"id": "install", "status": "error"}]}
    if install:
        run.result["install"] = {"kind": "batch", "profile": "rack-a", "hostnames": ["n1", "n2"],
                                 "batch": {"rows": [{"bmc_host": "10.0.0.1"}, {"bmc_host": "10.0.0.2"}]}}
    with wapp.ACTIONS_LOCK:
        wapp.ACTIONS[rid] = run
    return run


@pytest.fixture
def resume(env, bao, monkeypatch):
    seen = {}

    def fake_install(result, cluster, secrets_, confirms):
        seen["secrets"] = dict(secrets_)
        if not (secrets_.get("token") and (secrets_.get("bmc_password") or secrets_.get("bmc_passwords"))):
            return None, ({"error": "the installation phase is not done: its secrets are asked again",
                           "code": "secrets-required", "fields": ["token", "bmc_password"]}, 400)
        return {"kind": "batch", "rows": [dict(r) for r in rows()], "cluster": cluster, "hostnames": ["n1", "n2"],
                "record": result.get("install")}, None

    def fake_launch(cluster, kc, plan, variables, dry, user, cu, **kw):
        seen["launch"] = kw
        return {"action_id": NEW}, 202
    monkeypatch.setattr(wapp, "_deploy_resume_install", fake_install)
    monkeypatch.setattr(wapp, "_deploy_launch_checked", fake_launch)
    return seen


def test_resume_reads_secrets_instead_of_asking(env, bao, resume):
    keep(bao["store"])
    prev_run()
    r = env["client"].post("/api/deploy/rack-a/resume", json={"action_id": PREV})
    body = r.get_json()
    assert r.status_code == 202 and body["secrets_source"] == "openbao", body
    assert resume["secrets"]["token"] == TOKEN and resume["secrets"]["bmc_passwords"]["10.0.0.2"] == BMC2
    assert resume["launch"]["gitops_secrets"] == {"repo1": {"username": "git", "password": GITPW}}
    new_id = resume["launch"]["run_id"]
    assert kv_paths(bao["srv"], new_id)                  # secrets rangés sous le nouveau déroulé
    assert kv_paths(bao["srv"], PREV) == []              # l'arbre repris effacé
    text = r.get_data(as_text=True)
    for v in (TOKEN, PASSWORD, BMC1, BMC2, GITPW):
        assert v not in text


def test_resume_when_openbao_is_down_proposes_typing_the_secrets(env, bao, resume):
    keep(bao["store"])
    prev_run()
    bao["store"].clear_cache()
    bao["srv"].sealed = True
    r = env["client"].post("/api/deploy/rack-a/resume", json={"action_id": PREV})
    body = r.get_json()
    assert r.status_code == 503 and body["code"] == "openbao-sealed" and body["secrets_prompt"] is True
    assert body["fields"] == ["token", "bmc_password"]
    assert "launch" not in resume
    # secrets tapés : la reprise part comme en 2.4.0 (OpenBao toujours coupé)
    r = env["client"].post("/api/deploy/rack-a/resume", json={
        "action_id": PREV, "secrets": {"token": "typed", "bmc_password": "typed-bmc"}})
    assert r.status_code == 202, r.get_json()
    assert resume["secrets"]["token"] == "typed"
    # gardés en mémoire (2.4.0) : la reprise relit les secrets tapés
    st = bao["store"]
    back = wapp._deploy_install_secrets_get(st, resume["launch"]["run_id"], [{}])
    assert back[0]["token"] == TOKEN                    # lignes préparées par le faux (_deploy_resume_install)
    st.delete_tree(dch.SECRET_CLASS, resume["launch"]["run_id"])        # OpenBao coupé : pas d'exception
    assert st.refs(dch.SECRET_CLASS, resume["launch"]["run_id"]) == []      # mémoire vidée


def test_resume_after_the_sweep_asks_again(env, bao, resume):
    keep(bao["store"])
    prev_run(ended=time.time() - 8 * 86400)
    run = wapp._plan_secrets_sweep(store=bao["store"], wait=True)
    assert run is not None and run.result["removed"] == [PREV]
    r = env["client"].post("/api/deploy/rack-a/resume", json={"action_id": PREV})
    assert r.status_code == 400 and r.get_json()["code"] == "secrets-required"


# ---------------------------------------------------------------------------
# Balayage
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status,dry,age_days,expected", [
    ("done", False, 0, "ended"),
    ("error", True, 0, "ended"),
    ("error", False, 1, "keep"),
    ("partial", False, 6, "keep"),
    ("cancelled", False, 8, "expired"),
    ("running", False, 0, "interrupted-keep"),    # sans détenteur du verrou : interrompu, reprenable
])
def test_sweep_verdicts(bao, status, dry, age_days, expected):
    keep(bao["store"])
    prev_run(status=status, dry=dry, ended=time.time() - age_days * 86400)
    due = dict(wapp._plan_secrets_due(store=bao["store"]))
    if expected in ("keep", "interrupted-keep"):
        assert PREV not in due
    else:
        assert due[PREV] == expected


def test_restart_sweeps_ended_runs_only(bao):
    """Console tuée au milieu : le déroulé fini dont le `finally` n'a pas
    effacé les secrets est balayé ; le déroulé interrompu (ligne `running`
    sans détenteur) garde les siens pour sa reprise."""
    keep(bao["store"], PREV)
    keep(bao["store"], NEW)
    prev_run(PREV, status="done")
    prev_run(NEW, status="running")
    run = wapp._plan_secrets_sweep(store=bao["store"], wait=True)
    assert run.status == "done" and run.result["removed"] == [PREV] and run.result["failed"] == []
    assert kv_paths(bao["srv"], PREV) == [] and kv_paths(bao["srv"], NEW)
    events = json.dumps(list(run.events))
    for v in (TOKEN, PASSWORD, BMC1, GITPW):
        assert v not in events
    assert wapp._plan_secrets_sweep(store=bao["store"], wait=True) is None      # plus rien à balayer


@pytest.mark.parametrize("age_s,swept", [(60, False), (2 * 3600, True)])
def test_orphan_secrets_are_swept_once_old(bao, monkeypatch, age_s, swept):
    keep(bao["store"], "c3c3c3c3c3c3", gitops=False)
    monkeypatch.setattr(wapp, "_plan_secrets_age_s", lambda store, rid, now: age_s)
    due = dict(wapp._plan_secrets_due(store=bao["store"]))
    assert ("c3c3c3c3c3c3" in due) is swept


def test_age_reads_metadata_only(bao):
    keep(bao["store"], gitops=False)
    age = wapp._plan_secrets_age_s(bao["store"], PREV, time.time() + 100)
    assert age is not None and age >= 99


def test_keep_days_setting_is_bounded(bao):
    for raw, want in ((0, 1), (90, 30), ("x", 7), (3, 3)):
        (bao["state"] / ss.SETTINGS_FILE).write_text(json.dumps(dict(bao["doc"], plan_secrets_keep_days=raw)))
        assert ss.load_settings(bao["state"])["plan_secrets_keep_days"] == want


def test_sweep_does_nothing_without_openbao(bao):
    (bao["state"] / ss.SETTINGS_FILE).write_text(json.dumps(dict(bao["doc"], mode="local")))
    assert wapp._plan_secrets_due(store=bao["store"]) == []
    assert wapp._plan_secrets_sweep(store=bao["store"], wait=True) is None


def test_sweeper_starts_with_the_console():
    src = (ROOT / "web" / "app.py").read_text()
    main = src.split('if __name__ == "__main__":', 1)[1]
    assert "_start_plan_secrets_sweeper()" in main


# ---------------------------------------------------------------------------
# Parité en ligne de commande (bin/harvester-plans.py)
# ---------------------------------------------------------------------------
@pytest.fixture
def plans_cli(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("harvester_plans_290", ROOT / "bin" / "harvester-plans.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, "_console", lambda state: wapp)
    return cli


def test_cli_resume_without_secrets_file_reads_openbao(env, bao, resume, plans_cli, monkeypatch, capsys):
    keep(bao["store"])
    prev_run()
    launched = []
    monkeypatch.setattr(plans_cli, "_launched", lambda app, body, status: launched.append((body, status)) or 0)
    rc = plans_cli.main(["resume", PREV])
    assert rc == 0 and launched == [({"action_id": NEW}, 202)]
    assert resume["secrets"]["token"] == TOKEN and resume["launch"]["gitops_secrets"]["repo1"]["password"] == GITPW
    assert kv_paths(bao["srv"], PREV) == []
    err = capsys.readouterr().err
    assert "secrets read in OpenBao" in err and TOKEN not in err


def test_cli_resume_when_openbao_is_down_asks_for_the_file(env, bao, resume, plans_cli, capsys):
    keep(bao["store"])
    prev_run()
    bao["store"].clear_cache()
    bao["srv"].sealed = True
    rc = plans_cli.main(["--json", "resume", PREV])
    out = capsys.readouterr().out
    assert rc == 2 and json.loads(out)["code"] == "openbao-sealed" and "--secrets-file" in out


def test_cli_sweep_secrets(bao, plans_cli, capsys):
    keep(bao["store"])
    prev_run(status="done")
    rc = plans_cli.main(["sweep-secrets", "--dry-run"])
    assert rc == 0 and json.loads(capsys.readouterr().out)["due"] == [{"run": PREV, "reason": "ended"}]
    assert kv_paths(bao["srv"], PREV)
    rc = plans_cli.main(["sweep-secrets"])
    d = json.loads(capsys.readouterr().out)
    assert rc == 0 and d["removed"] == [PREV] and kv_paths(bao["srv"], PREV) == []


# ---------------------------------------------------------------------------
# Revue des tâches 9-10 (I3) : un échec de l'effacement des secrets ne change
# pas l'issue d'un déroulé ; il est écrit en étape et laissé au balayage
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code", ["openbao-login-failed", "openbao-denied", "openbao-unreachable"])
def test_a_failed_secret_cleanup_keeps_a_done_run_done(bao, code):
    st, srv = bao["store"], bao["srv"]
    keep(st)

    def refuse(*a, **kw):
        raise ss.SecretError(code, dch.SECRET_CLASS, PREV, "refused")
    st.openbao.delete_tree = refuse
    ctx = Ctx(PREV)
    res = dch.run(ctx, one_phase("done"), secrets=st, sleep=lambda s: None,
                  keep_secrets=lambda status: False)
    assert res["status"] == "done"
    if code == "openbao-unreachable":
        return                                   # déjà toléré : laissé au balayage sans bruit
    cleanup = [s for s in ctx.steps if s[0] == "secrets-cleanup"]
    assert cleanup and cleanup[-1][1] == "error" and code in cleanup[-1][2]
    assert "refused" not in cleanup[-1][2] or code in cleanup[-1][2]


# Revue m6 des tâches 9-10 : gardés pendant une panne, en mémoire seulement ;
# une copie en mémoire n'est jamais relue par la reprise, elle est purgée
def test_memory_copies_of_a_kept_run_are_purged(bao):
    st, srv = bao["store"], bao["srv"]
    srv.sealed = True
    keep(st)                                     # OpenBao coupé à la saisie : mémoire
    assert st.refs(dch.SECRET_CLASS, PREV)
    run = wapp.ActionRun(PREV, "deploy:harvlab", "harvlab", [])
    dch.run(Ctx(PREV), one_phase("error"), secrets=st, sleep=lambda s: None,
            keep_secrets=lambda status: wapp._deploy_keep_secrets(st, run, status))
    assert st.refs(dch.SECRET_CLASS, PREV) == []


def test_resume_forget_purges_memory_copies(bao):
    st, srv = bao["store"], bao["srv"]
    srv.sealed = True
    keep(st)
    wapp._deploy_resume_forget(PREV)
    assert st.refs(dch.SECRET_CLASS, PREV) == []


# Revue m7 : oublier tout de suite les secrets d'un déroulé arrêté (admin)
def test_forget_the_secrets_of_a_stopped_run(env, bao):
    keep(bao["store"])
    prev_run(status="cancelled")
    r = env["client"].post("/api/deploy/rack-a/forget-secrets", json={"action_id": PREV})
    assert r.status_code == 202, r.get_json()
    aid = r.get_json()["action_id"]
    for _ in range(100):
        with wapp.ACTIONS_LOCK:
            run = wapp.ACTIONS.get(aid)
        if run and run.status in ("done", "error"):
            break
        time.sleep(0.05)
    assert run.status == "done" and run.action == f"secrets:plan-run-forget:{PREV}"
    assert kv_paths(bao["srv"], PREV) == []
    for v in (TOKEN, PASSWORD, BMC1, BMC2, GITPW):
        assert v not in json.dumps(list(run.events))


@pytest.mark.parametrize("prep,body,code", [
    ("running", {"action_id": PREV}, 409),
    ("none", {"action_id": PREV}, 404),
    ("other-cluster", {"action_id": PREV}, 404),
    ("none", {"action_id": "../x"}, 400),
])
def test_forget_refusals(env, bao, prep, body, code, monkeypatch):
    keep(bao["store"])
    with wapp.ACTIONS_LOCK:
        wapp.ACTIONS.pop(PREV, None)
    if prep == "running":
        prev_run(status="running")
        monkeypatch.setattr(wapp, "_deploy_lock_holder", lambda c: {"run": PREV})
    elif prep == "other-cluster":
        run = prev_run()
        run.cluster = "rack-b"
    r = env["client"].post("/api/deploy/rack-a/forget-secrets", json=body)
    assert r.status_code == code, r.get_json()
    assert kv_paths(bao["srv"], PREV)


def test_forget_is_admin_only():
    assert wapp.required_role_for("/api/deploy/rack-a/forget-secrets", "POST") == "admin"


def test_cli_forget_secrets(bao, plans_cli, capsys):
    keep(bao["store"])
    prev_run(status="cancelled")
    rc = plans_cli.main(["forget-secrets", PREV])
    d = json.loads(capsys.readouterr().out)
    assert rc == 0 and d["removed"] == [PREV] and kv_paths(bao["srv"], PREV) == []
