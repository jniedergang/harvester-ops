"""2.4.0 : moteur de chaîne d'un plan de déploiement (bin/lib/deploy_chain.py).

Toutes les entrées-sorties sont simulées. Tableaux : chaîne complète, phase
désactivée, phase 2.5+, échec en phase 2 (phase 3 jamais appelée),
Terraform partiel (modèle indépendant poursuivi, modèle à depends_on en
échec sauté), arrêt avant une phase et entre deux modèles, reprise (phase
faite revérifiée et sautée, phase dont la vérification échoue rejouée,
partiel repris au premier élément non fait), exception sans fuite de texte,
délai de la disponibilité, secrets effacés quelle que soit l'issue.

Mutations jouées (rapport de la tâche 5) : reprise sans `verify`, `partial`
qui continue la chaîne, arrêt non consulté entre éléments.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import deploy_chain as dc     # noqa: E402
import secret_store as ss     # noqa: E402

SECRET = "TOKEN-VALUE-240-chain"


class Ctx:
    def __init__(self, cancel_after=None, run_id="a1b2c3d4e5f6"):
        self.run_id = run_id
        self.steps, self.results, self.calls = [], [], []
        self.cancel_after = cancel_after       # nombre d'appels à cancelled() avant l'arrêt
        self.cancel_calls = 0
        self.cancel_now = False

    def emit_step(self, sid, status, msg):
        self.steps.append((sid, status, msg))

    def set_result(self, r):
        self.results.append(r)

    def cancelled(self):
        self.cancel_calls += 1
        if self.cancel_after is not None and self.cancel_calls > self.cancel_after:
            return True
        return self.cancel_now

    def final(self, sid):
        return [s for s in self.steps if s[0] == sid][-1][1:]


def simple(ctx, pid, status="done", data=None, verify=None, raises=None, **kw):
    def execute(c, state):
        ctx.calls.append(pid)
        if raises:
            raise raises
        return dc.PhaseOutcome(status, message=f"{pid} {status}", data=data or {})
    return dc.Phase(pid, execute=execute, verify=verify, **kw)


def tf_phase(ctx, outcomes, deps=None, verify_item=None, cancel_after_item=None):
    """Phase Terraform : un élément par modèle ; `outcomes` {modèle: statut}."""
    def run_item(c, state, item):
        ctx.calls.append(f"tf:{item.id}")
        if cancel_after_item == item.id:
            ctx.cancel_now = True
        st = outcomes[item.id]
        if isinstance(st, Exception):
            raise st
        return dc.ItemOutcome(st, message=f"{item.id} {st}",
                              data={"declaration": f"site-{item.id}", "revision_applied": 1})
    items = [dc.Item(t, tuple((deps or {}).get(t, ()))) for t in outcomes]
    return dc.Phase("terraform", items=items, run_item=run_item, verify_item=verify_item,
                    item_key="template", item_step="tf-")


def run(ctx, phases, previous=None, store=None, **kw):
    store = store or ss.LocalStore()
    store.put("plan-run", f"{ctx.run_id}/install", "token", SECRET)
    store.put("plan-run", "other0000000/install", "token", "keep")
    out = dc.run(ctx, phases, previous=previous, sleep=kw.pop("sleep", lambda s: None),
                 secrets=store, **kw)
    assert store.refs("plan-run", ctx.run_id) == []              # effacés
    assert store.refs("plan-run", "other0000000")                # pas ceux d'un autre déroulé
    return out


def statuses(out):
    return [(p["id"], p["status"]) for p in out["phases"]]


# --- chaînes -------------------------------------------------------------------

def test_full_chain():
    ctx = Ctx()
    out = run(ctx, [simple(ctx, "install", data={"actions": ["c1"]}), simple(ctx, "readiness"),
                    tf_phase(ctx, {"a": "done", "b": "done"})])
    assert out["status"] == "done" and "error_summary" not in out
    assert statuses(out) == [("install", "done"), ("readiness", "done"), ("terraform", "done")]
    assert out["phases"][0]["actions"] == ["c1"]
    assert out["phases"][2]["items"][0] == {"template": "a", "status": "done", "message": "a done",
                                           "declaration": "site-a", "revision_applied": 1}
    assert ctx.calls == ["install", "readiness", "tf:a", "tf:b"]
    assert [s[0] for s in ctx.steps if s[1] == "running"] == ["phase-install", "phase-readiness",
                                                              "phase-terraform", "tf-a", "tf-b"]
    assert ctx.results[-1] == out


@pytest.mark.parametrize("phases_ids,expected", [
    # ordre fixe, quel que soit l'ordre de la liste
    (["terraform", "readiness"], [("readiness", "done"), ("terraform", "done")]),
    # phase désactivée : sautée, la suivante tourne
    ([("readiness", False), "terraform"], [("readiness", "disabled"), ("terraform", "done")]),
    # artefacts : sautée « coming in 2.7 », même activée par erreur ; capi
    # tourne depuis la 2.5.0, registry depuis la 2.6.0, AVANT capi (point 189)
    (["readiness", "capi", "artefacts", "registry"],
     [("readiness", "done"), ("registry", "done"), ("capi", "done"), ("artefacts", "disabled")]),
])
def test_order_disabled_and_coming(phases_ids, expected):
    ctx = Ctx()
    phases = []
    for p in phases_ids:
        pid, en = (p, True) if isinstance(p, str) else p
        phases.append(simple(ctx, pid, enabled=en))
    out = run(ctx, phases)
    assert statuses(out) == expected and out["status"] == "done"
    for pid in ("artefacts",):
        if any(p.id == pid for p in phases):
            assert ctx.final(f"phase-{pid}") == ("skipped", "coming in 2.7")
            assert pid not in ctx.calls
    if ("readiness", False) in phases_ids:
        assert ctx.final("phase-readiness") == ("skipped", "disabled in the plan")


def test_absent_phase_emits_nothing():
    ctx = Ctx()
    run(ctx, [simple(ctx, "terraform")])
    assert {s[0] for s in ctx.steps} == {"phase-terraform"}


@pytest.mark.parametrize("failing,status", [("readiness", "error"), ("install", "error")])
def test_failure_stops_the_chain(failing, status):
    ctx = Ctx()
    phases = [simple(ctx, "install", "error" if failing == "install" else "done"),
              simple(ctx, "readiness", "error" if failing == "readiness" else "done"),
              simple(ctx, "terraform"), simple(ctx, "capi", enabled=False)]
    out = run(ctx, phases)
    assert out["status"] == status and out["failed_phase"] == failing
    assert "terraform" not in ctx.calls
    assert dict(statuses(out))["terraform"] == "skipped"
    assert dict(statuses(out))["capi"] == "disabled"
    assert out["error_summary"] == f"{failing} error"


def test_terraform_partial_independent_continues_dependent_skipped():
    ctx = Ctx()
    out = run(ctx, [tf_phase(ctx, {"net": "error", "img": "done", "vm": "done", "keys": "done"},
                             deps={"vm": ["net"], "keys": ["img"]}),
                    simple(ctx, "capi", enabled=False)])
    items = {i["template"]: i for i in out["phases"][0]["items"]}
    assert items["net"]["status"] == "error" and items["img"]["status"] == "done"
    assert items["vm"]["status"] == "skipped" and items["vm"]["message"] == "depends on net, not done"
    assert items["keys"]["status"] == "done"
    assert "tf:vm" not in ctx.calls
    assert out["phases"][0]["status"] == "partial" and out["status"] == "partial"
    assert out["phases"][0]["message"] == "2/4 done, 1 failed"
    assert ctx.final("tf-vm") == ("skipped", "depends on net, not done")


def test_partial_does_not_continue_to_later_phase(monkeypatch):
    """Une phase suivante (capi, activable depuis la 2.5.0) n'est pas lancée."""
    ctx = Ctx()
    out = run(ctx, [tf_phase(ctx, {"a": "error", "b": "done"}), simple(ctx, "capi")])
    assert "capi" not in ctx.calls and dict(statuses(out))["capi"] == "skipped"
    assert out["status"] == "partial"


def test_all_items_failed_is_error():
    ctx = Ctx()
    out = run(ctx, [tf_phase(ctx, {"a": "error", "b": "error"})])
    assert out["phases"][0]["status"] == "error" and out["status"] == "error"


# --- arrêt -----------------------------------------------------------------------

def test_cancel_before_a_phase():
    ctx = Ctx(cancel_after=1)               # la première phase passe, pas la seconde
    out = run(ctx, [simple(ctx, "install"), simple(ctx, "readiness"), simple(ctx, "terraform"),
                    simple(ctx, "capi", enabled=False)])
    assert ctx.calls == ["install"]
    assert statuses(out) == [("install", "done"), ("readiness", "skipped"), ("terraform", "skipped"),
                             ("capi", "disabled")]
    assert out["status"] == "cancelled" and "failed_phase" not in out
    assert ctx.final("phase-readiness") == ("skipped", "cancelled")
    assert dc.EXIT_CODES[out["status"]] == 3


def test_cancel_between_two_templates():
    ctx = Ctx()
    out = run(ctx, [tf_phase(ctx, {"a": "done", "b": "done", "c": "done"}, cancel_after_item="a")])
    items = out["phases"][0]["items"]
    assert [(i["template"], i["status"]) for i in items] == [("a", "done"), ("b", "skipped"), ("c", "skipped")]
    assert ctx.calls == ["tf:a"]                # l'élément en cours est allé à son terme
    assert out["status"] == "cancelled"


def test_cancel_during_readiness_wait():
    ctx = Ctx()
    n = {"i": 0}

    def execute(c, state):
        n["i"] += 1
        if n["i"] == 2:
            ctx.cancel_now = True
        return dc.PhaseOutcome("retry", message="nodes-not-ready", data={"unmet": [{"code": "x"}]})
    out = run(ctx, [dc.Phase("readiness", execute=execute, timeout=600), simple(ctx, "terraform")])
    assert out["status"] == "cancelled" and "terraform" not in ctx.calls


# --- disponibilité : relecture jusqu'au délai --------------------------------------

def test_readiness_polls_until_ready():
    clock = {"t": 0.0}
    slept = []
    left = {"n": 3}

    def execute(c, state):
        left["n"] -= 1
        return dc.PhaseOutcome("retry" if left["n"] else "done", data={"unmet": [] if not left["n"] else [1]})
    ctx = Ctx()
    out = run(ctx, [dc.Phase("readiness", execute=execute, timeout=1200)],
              sleep=lambda s: (slept.append(s), clock.__setitem__("t", clock["t"] + s)), now=lambda: clock["t"])
    assert out["status"] == "done" and slept == [15, 15]
    assert out["phases"][0]["seconds"] == 30


def test_readiness_times_out_with_last_findings():
    clock = {"t": 0.0}

    def execute(c, state):
        return dc.PhaseOutcome("retry", message="longhorn-not-schedulable",
                               data={"unmet": [{"code": "longhorn-not-schedulable", "detail": "n1"}]})
    ctx = Ctx()
    out = run(ctx, [dc.Phase("readiness", execute=execute, timeout=60), simple(ctx, "terraform")],
              sleep=lambda s: clock.__setitem__("t", clock["t"] + s), now=lambda: clock["t"])
    ph = out["phases"][0]
    assert ph["status"] == "error" and ph["timed_out"] is True
    assert ph["unmet"] == [{"code": "longhorn-not-schedulable", "detail": "n1"}]
    assert out["error_summary"] == "longhorn-not-schedulable" and "terraform" not in ctx.calls


# --- reprise ------------------------------------------------------------------------

PREV = {"phases": [{"id": "install", "status": "done", "actions": ["c1"]},
                   {"id": "readiness", "status": "done", "unmet": []},
                   {"id": "terraform", "status": "partial", "items": [
                       {"template": "a", "status": "done", "declaration": "site-a", "revision_applied": 2},
                       {"template": "b", "status": "error"},
                       {"template": "c", "status": "skipped"}]}]}


@pytest.mark.parametrize("install_ok,expected_calls", [
    (True, ["verify:install", "readiness", "tf:b", "tf:c"]),          # faite, revérifiée, sautée
    (False, ["verify:install", "install", "readiness", "tf:b", "tf:c"]),  # vérification en défaut : rejouée
])
def test_resume(install_ok, expected_calls):
    ctx = Ctx()
    verified = []

    def verify_install(c, state):
        ctx.calls.append("verify:install")
        assert state["previous"]["actions"] == ["c1"]
        return install_ok

    def verify_item(c, state, prev):
        verified.append(prev["template"])
        return prev["revision_applied"] == 2
    phases = [simple(ctx, "install", verify=verify_install),
              simple(ctx, "readiness", verify=None),            # sans vérification : rejouée
              tf_phase(ctx, {"a": "done", "b": "done", "c": "done"}, verify_item=verify_item)]
    out = run(ctx, phases, previous=PREV)
    assert ctx.calls == expected_calls
    assert verified == ["a"]                     # seuls les éléments faits sont revérifiés
    items = out["phases"][2]["items"]
    assert items[0]["checked"] is True and items[0]["declaration"] == "site-a"
    assert out["status"] == "done"
    if install_ok:
        assert out["phases"][0]["checked"] is True and out["phases"][0]["actions"] == ["c1"]
        assert ctx.final("phase-install") == ("skipped", "already done (checked)")
    assert ctx.final("tf-a") == ("skipped", "already done (checked)")


def test_resume_item_check_fails_replays_item():
    ctx = Ctx()
    out = run(ctx, [tf_phase(ctx, {"a": "done", "b": "done", "c": "done"},
                             verify_item=lambda c, s, p: False)], previous=PREV)
    assert ctx.calls == ["tf:a", "tf:b", "tf:c"] and out["status"] == "done"


def test_resume_verify_exception_replays():
    ctx = Ctx()
    out = run(ctx, [simple(ctx, "install", verify=lambda c, s: 1 / 0)], previous=PREV)
    assert ctx.calls == ["install"] and out["status"] == "done"


def test_resume_done_terraform_runs_new_templates():
    """Revue I3 : une phase Terraform `done` dont le plan a gagné un modèle
    depuis n'est pas sautée d'un bloc ; seuls les modèles faits et revérifiés
    sont sautés, le nouveau est appliqué. Une vérification de phase vraie
    n'y change rien."""
    prev = {"phases": [{"id": "terraform", "status": "done", "items": [
        {"template": "a", "status": "done", "revision_applied": 2},
        {"template": "b", "status": "done", "revision_applied": 2}]}]}
    ctx = Ctx()
    ph = tf_phase(ctx, {"a": "done", "b": "done", "c": "done"},
                  verify_item=lambda c, s, p: p["revision_applied"] == 2)
    ph.verify = lambda c, s: True
    out = run(ctx, [ph], previous=prev)
    assert ctx.calls == ["tf:c"]
    assert [i.get("checked", False) for i in out["phases"][0]["items"]] == [True, True, False]
    assert out["status"] == "done"


def test_phase_data_never_overrides_status():
    """Revue M1 : `data` d'une phase ne remplace ni l'id, ni le statut, ni le message."""
    ctx = Ctx()
    out = run(ctx, [simple(ctx, "readiness", status="error",
                           data={"status": "done", "id": "x", "message": "m", "unmet": []})])
    ph = out["phases"][0]
    assert (ph["id"], ph["status"], ph["message"]) == ("readiness", "error", "readiness error")
    assert ph["unmet"] == [] and out["status"] == "error"


def test_item_progress_reaches_result():
    """Revue M2 : l'avancée d'une phase à éléments est consignée après
    chaque élément, avant la fin de la phase."""
    ctx = Ctx()
    run(ctx, [tf_phase(ctx, {"a": "done", "b": "done"})])
    live = [r for r in ctx.results if r["phases"] and r["phases"][-1].get("status") == "running"]
    assert [[i["template"] for i in r["phases"][-1]["items"]] for r in live] == [["a"], ["a", "b"]]


@pytest.mark.parametrize("run_id", ["", "../x", "A1B2C3D4E5F6", "a1b2c3", None])
def test_bad_run_id_refused_before_anything(run_id):
    """Revue M3 : un identifiant d'action invalide est refusé avant toute
    phase (l'effacement des secrets ne pourrait pas le désigner)."""
    ctx = Ctx(run_id=run_id)
    with pytest.raises(ValueError):
        dc.run(ctx, [simple(ctx, "readiness")], secrets=ss.LocalStore())
    assert ctx.calls == [] and ctx.steps == []


# --- exceptions -------------------------------------------------------------------

def test_phase_exception_without_text_leak():
    ctx = Ctx()
    out = run(ctx, [simple(ctx, "install", raises=RuntimeError(f"bmc said {SECRET}")),
                    simple(ctx, "readiness")])
    assert out["status"] == "error" and out["error_summary"] == "RuntimeError"
    assert out["phases"][0]["message"] == "RuntimeError"
    blob = repr(out) + repr(ctx.steps) + repr(ctx.results)
    assert SECRET not in blob and "bmc said" not in blob


def test_item_exception_without_text_leak():
    ctx = Ctx()
    out = run(ctx, [tf_phase(ctx, {"a": ValueError(f"leak {SECRET}"), "b": "done"})])
    a = out["phases"][0]["items"][0]
    assert a["status"] == "error" and a["message"] == "ValueError"
    assert SECRET not in repr(out) + repr(ctx.steps)
    assert out["status"] == "partial"


@pytest.mark.parametrize("bad", [None, "done", dc.PhaseOutcome("weird")])
def test_phase_returning_garbage_is_error(bad):
    ctx = Ctx()
    out = run(ctx, [dc.Phase("install", execute=lambda c, s: bad)])
    assert out["status"] == "error"


def test_secrets_erased_when_the_engine_itself_fails():
    ctx = Ctx()
    store = ss.LocalStore()
    store.put("plan-run", f"{ctx.run_id}/install", "token", SECRET)

    def boom(*a):
        raise KeyboardInterrupt
    ctx.set_result = boom
    with pytest.raises(KeyboardInterrupt):
        dc.run(ctx, [simple(ctx, "install")], secrets=store)
    assert store.refs("plan-run", ctx.run_id) == []


def test_default_store_is_the_process_store():
    ctx = Ctx(run_id="feedbeef0001")
    ss.store().put("plan-run", "feedbeef0001/install", "token", SECRET)
    dc.run(ctx, [simple(ctx, "install")])
    assert ss.store().refs("plan-run", "feedbeef0001") == []
