"""v1.80.0 : les dates qu'il faut à la vue en couloirs des vagues.

wave_state rend désormais chaque copie (début, fin, durée), les débuts et
fins de la VM et de la migration, et la fenêtre de bascule réellement
vécue. Les objets viennent des fixtures relevées sur le banc (1.76.0)."""

import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin" / "lib"))
import hv_forklift as hf  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
PLANS = {p["metadata"]["name"]: p for p in json.loads((FIX / "forklift_b2_plans_176.json").read_text())["items"]}
MIGRATIONS = {m["metadata"]["name"]: m
              for m in json.loads((FIX / "forklift_b2_migrations_176.json").read_text())["items"]}
AFTER = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)


def test_every_copy_is_listed_with_its_dates():
    st = hf.wave_state(PLANS["vague-1"], list(MIGRATIONS.values()), now=AFTER)
    vm = st["vms"][0]
    assert vm["precopies"] == 3
    assert [c["start"] for c in vm["copies"]] == ["2026-09-29T19:23:48Z", "2026-09-29T19:31:02Z",
                                                  "2026-09-29T19:32:53Z"]
    assert vm["copies"][0] == {"start": "2026-09-29T19:23:48Z", "end": "2026-09-29T19:25:37Z", "seconds": 109}
    # la copie finale de la bascule n'a pas de fin relevée : gardée, ouverte
    assert vm["copies"][-1]["end"] == "" and vm["copies"][-1]["seconds"] is None
    # la dernière copie finie reste celle de la fenêtre de suivi
    assert vm["last_precopy"]["seconds"] == 62


def test_wave_and_vm_start_end_and_cutover_window():
    st = hf.wave_state(PLANS["vague-2"], list(MIGRATIONS.values()), now=AFTER)
    assert st["created"] == "2026-09-29T19:45:47Z"
    assert st["started"] == "2026-09-29T19:45:56Z"
    assert st["completed"] == "2026-09-29T19:48:22Z"
    vm = st["vms"][0]
    assert vm["started"] == "2026-09-29T19:45:55Z"
    assert vm["completed"] == "2026-09-29T19:48:22Z"
    assert vm["cutover_window"] == {"start": "2026-09-29T19:47:54Z", "end": "2026-09-29T19:48:22Z"}


def test_cutover_window_open_while_it_runs_and_absent_before():
    m = copy.deepcopy(MIGRATIONS["vague-2-m1"])
    vm = m["status"]["vms"][0]
    vm.pop("completed", None)
    m["status"].pop("completed", None)
    m["status"]["conditions"] = [c for c in m["status"]["conditions"] if c["type"] == "Ready"]
    plan = copy.deepcopy(PLANS["vague-2"])
    plan["status"].pop("migration", None)
    st = hf.wave_state(plan, [m], now=AFTER)
    assert st["completed"] is None
    assert st["vms"][0]["cutover_window"] == {"start": "2026-09-29T19:47:54Z", "end": None}
    # étape Cutover pas encore commencée : aucune fenêtre
    for s in vm["pipeline"]:
        if s["name"] == "Cutover":
            s.pop("started", None)
    st = hf.wave_state(plan, [m], now=AFTER)
    assert st["vms"][0]["cutover_window"] is None


def test_global_view_without_migrations_reads_the_plan_summary():
    st = hf.wave_state(PLANS["vague-1"], [], now=AFTER)
    assert st["started"] == "2026-09-29T19:23:45Z"
    assert st["completed"] == "2026-09-29T19:38:21Z"
    assert len(st["vms"][0]["copies"]) == 3


def test_wave_without_any_run_has_no_dates():
    plan = copy.deepcopy(PLANS["vague-2"])
    plan["status"].pop("migration", None)
    st = hf.wave_state(plan, [], now=AFTER)
    assert st["started"] is None and st["completed"] is None
    assert st["vms"][0]["copies"] == [] and st["vms"][0]["cutover_window"] is None
