"""v1.95.0 : le décompte des GPU-heures et les quotas (bin/lib/hv_usage.py).

Fonctions pures : heures réservées coupées aux bornes de la période et règle
des annulations, heures consommées depuis les relevés (trous de plus de deux
intervalles non comblés), rattachement par la cible d'une réservation sinon
par le namespace, gaspillage et hors règle, série par jour, quotas mensuels
et d'unités simultanées."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import hv_gpu as hg  # noqa: E402
import hv_usage as hu  # noqa: E402

P0, P1 = "2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z"


def bk(bid, start, end, count=1, holder=("user", "alice"), cluster="c1", typ="vgpu", model="M",
       target=None, cancelled_at=""):
    t = None
    if target:
        kind, ref = target.split(":")
        ns, name = ref.split("/")
        t = {"kind": kind, "namespace": ns, "name": name}
    return {"id": bid, "cluster": cluster, "type": typ, "unit": "", "model": model, "count": count,
            "holder": {"kind": holder[0], "name": holder[1]}, "start": start, "end": end, "target": t,
            "cancelled_at": cancelled_at}


def smp(ts, name, ns="ml", kind="vm", count=1, cluster="c1", unit="u1", typ="vgpu", workload="", interval=300):
    return {"ts": ts, "cluster": cluster, "unit": unit, "type": typ, "model": "M", "kind": kind,
            "namespace": ns, "name": name, "workload": workload, "count": count, "interval": interval}


def at(h, m=0, day=10):
    return f"2026-10-{day:02d}T{h:02d}:{m:02d}:00Z"


# -- réservé : bornes de la période, annulations ---------------------------------------

@pytest.mark.parametrize("case,b,expected", [
    ("inside", bk("a", at(9), at(13), count=2), 8.0),
    ("starts before the period", bk("a", "2026-09-30T22:00:00Z", "2026-10-01T02:00:00Z"), 2.0),
    ("ends after the period", bk("a", "2026-10-31T23:00:00Z", "2026-11-01T03:00:00Z"), 1.0),
    ("spans the whole period", bk("a", "2026-09-20T00:00:00Z", "2026-11-10T00:00:00Z"), 31 * 24.0),
    ("ends exactly at the start", bk("a", "2026-09-30T20:00:00Z", P0), 0.0),
    ("starts exactly at the end", bk("a", P1, "2026-11-01T05:00:00Z"), 0.0),
    ("cancelled after its start", bk("a", at(9), at(13), cancelled_at=at(10, 30)), 1.5),
    ("cancelled before its start", bk("a", at(9), at(13), cancelled_at=at(8)), 0.0),
    ("cancelled exactly at its start", bk("a", at(9), at(13), cancelled_at=at(9)), 0.0),
    ("cancelled after its end", bk("a", at(9), at(13), cancelled_at=at(15)), 4.0),
])
def test_booked_hours_are_clipped_to_the_period(case, b, expected):
    assert hu.booking_hours(b, P0, P1) == pytest.approx(expected), case


# -- consommé : un relevé vaut l'écart avec le précédent, au plus deux intervalles -----

@pytest.mark.parametrize("case,stamps,expected_minutes", [
    ("one sample = one interval", [at(10)], 5),
    ("regular samples", [at(10), at(10, 5), at(10, 10)], 15),
    ("one missed run (2 intervals) is filled", [at(10), at(10, 10)], 15),
    ("a longer gap is not filled", [at(10), at(10, 20)], 10),
    ("jitter is followed", [at(10), at(10, 6)], 11),
    ("duplicate at the same instant counts once", [at(10), at(10)], 5),
])
def test_consumed_hours_from_samples(case, stamps, expected_minutes):
    rep = hu.report([], [smp(t, "nb") for t in stamps], P0, P1)
    [r] = rep["holders"]
    assert r["consumed"] == pytest.approx(round(expected_minutes / 60, 3), abs=1e-3), case


def test_series_are_kept_apart_per_unit_and_consumer():
    # deux unités tenues par la même VM : deux séries, jamais l'écart de l'une pour l'autre
    s = [smp(at(10), "nb", unit="u1"), smp(at(10, 5), "nb", unit="u2"), smp(at(10, 10), "nb", unit="u1")]
    segs = hu.segments(s, P0, P1)
    assert sorted(round((e - b).total_seconds() / 60) for _x, b, e in segs) == [5, 5, 10]


def test_a_sample_is_clipped_at_the_period_edge():
    rep = hu.report([], [smp("2026-10-01T00:02:00Z", "nb")], P0, P1)
    assert rep["holders"][0]["consumed"] == pytest.approx(round(2 / 60, 3), abs=1e-3)


def test_the_interval_of_each_sample_is_bounded():
    assert hu.interval_from("5") == 30 and hu.interval_from("99999") == 3600 and hu.interval_from("x") == 300


# -- rattachement : par la cible, sinon par le namespace ---------------------------------

@pytest.mark.parametrize("case,target,sample,holder", [
    ("vm target", "vm:ml/nb", smp(at(10), "nb"), ("user", "alice")),
    ("workload target, pod of it", "workload:ml/trainer",
     smp(at(10), "trainer-7b-x", kind="pod", workload="trainer"), ("user", "alice")),
    ("workload target naming the pod", "workload:ml/solo", smp(at(10), "solo", kind="pod", workload="solo"),
     ("user", "alice")),
    ("other vm", "vm:ml/other", smp(at(10), "nb"), ("namespace", "ml")),
    ("other namespace", "vm:prod/nb", smp(at(10), "nb"), ("namespace", "ml")),
    ("vm target never matches a pod", "vm:ml/trainer", smp(at(10), "trainer-x", kind="pod", workload="trainer"),
     ("namespace", "ml")),
    ("other cluster", "vm:ml/nb", smp(at(10), "nb", cluster="c2"), ("namespace", "ml")),
    ("no target", None, smp(at(10), "nb"), ("namespace", "ml")),
])
def test_attribution(case, target, sample, holder):
    rep = hu.report([bk("b1", at(9), at(13), target=target)], [sample], P0, P1)
    got = {(r["holder"]["kind"], r["holder"]["name"]): r for r in rep["holders"]}
    assert got[holder]["consumed"] > 0, case


def test_attribution_needs_the_booking_active_at_that_time():
    b = bk("b1", at(9), at(13), target="vm:ml/nb", cancelled_at=at(11))
    s = [smp(at(10), "nb"), smp(at(12), "nb"), smp(at(14), "nb")]
    rep = hu.report([b], s, P0, P1)
    got = {r["holder"]["name"]: r for r in rep["holders"]}
    assert got["alice"]["consumed"] == pytest.approx(round(5 / 60, 3), abs=1e-3)     # 10:00 seulement
    assert got["ml"]["consumed"] == pytest.approx(round(10 / 60, 3), abs=1e-3)       # après l'annulation, puis après la fin


def test_attribution_prefers_the_same_unit_type_then_the_oldest():
    s = smp(at(10), "nb", typ="gpu")
    b1 = bk("b1", at(8), at(13), holder=("group", "ml-team"), typ="vgpu", target="vm:ml/nb")
    b2 = bk("b2", at(9), at(13), holder=("user", "bob"), typ="gpu", target="vm:ml/nb")
    b3 = bk("b3", at(7), at(13), holder=("user", "carol"), typ="gpu", target="vm:ml/nb")
    assert hu.attribute(s, hu._t(at(10)), [b1, b2])["id"] == "b2"
    assert hu.attribute(s, hu._t(at(10)), [b1, b2, b3])["id"] == "b3"


# -- gaspillage et hors règle ---------------------------------------------------------------

def test_waste_and_out_of_rule_per_holder():
    # alice réserve 2 unités 10:00-11:00 pour ml/nb, la VM en tient 1 pendant 30 min ;
    # bob réserve 1 unité pour sa charge, qui en tient 3 pendant 10 min ;
    # prod/train tient un GPU sans réservation pendant 5 min
    bookings = [bk("a", at(10), at(11), count=2, target="vm:ml/nb"),
                bk("b", at(10), at(11), count=1, holder=("user", "bob"), typ="node",
                   target="workload:ml/trainer")]
    samples = [smp(at(10, m), "nb") for m in (5, 10, 15, 20, 25, 30)]
    samples += [smp(at(10, m), "trainer-1", kind="pod", workload="trainer", count=3, unit="n1/gpu", typ="node")
                for m in (5, 10)]
    samples += [smp(at(10, 5), "train", ns="prod", unit="g9", typ="gpu")]
    rep = hu.report(bookings, samples, P0, P1, now=at(12))
    got = {r["holder"]["name"]: r for r in rep["holders"]}
    assert (got["alice"]["booked"], got["alice"]["consumed"], got["alice"]["waste"], got["alice"]["out_of_rule"]) == \
        (2.0, 0.5, 1.5, 0.0)
    assert (got["bob"]["booked"], got["bob"]["consumed"]) == (1.0, 0.5)
    assert got["bob"]["out_of_rule"] == pytest.approx(round(2 * 10 / 60, 3), abs=1e-3)     # 2 unités de trop
    assert got["bob"]["waste"] == pytest.approx(round(1 - 10 / 60, 3), abs=1e-3)
    assert (got["prod"]["holder"]["kind"], got["prod"]["out_of_rule"], got["prod"]["booked"]) == \
        ("namespace", round(5 / 60, 3), 0.0)
    assert rep["totals"]["booked"] == 3.0
    # les titulaires d'abord, puis les namespaces
    assert [r["holder"]["kind"] for r in rep["holders"]] == ["user", "user", "namespace"]


def test_waste_counts_only_the_past_part():
    rep = hu.report([bk("a", at(10), at(14))], [], P0, P1, now=at(11))
    [r] = rep["holders"]
    assert (r["booked"], r["waste"]) == (4.0, 1.0)


def test_several_units_of_one_booking_at_the_same_run_are_capped_together():
    # 2 vGPU réservés pour la VM, qui en tient 2 (deux unités, deux relevés) : rien hors règle
    b = bk("a", at(10), at(11), count=2, target="vm:ml/nb")
    s = [smp(at(10, 5), "nb", unit="u1"), smp(at(10, 5), "nb", unit="u2")]
    [r] = hu.report([b], s, P0, P1, now=at(12))["holders"]
    assert r["out_of_rule"] == 0.0 and r["consumed"] == pytest.approx(round(10 / 60, 3), abs=1e-3)


# -- détail par cluster et type, série par jour ----------------------------------------------

def test_breakdown_and_days_multi_holder_multi_cluster():
    bookings = [bk("a", at(22, day=10), at(2, day=11), target="vm:ml/nb"),
                bk("g", at(9, day=12), at(10, day=12), count=3, holder=("group", "ml-team"), cluster="c2",
                   typ="node", model="nvidia.com/gpu")]
    samples = [smp(at(23, 0, day=10), "nb"), smp(at(10, day=12), "x", cluster="c2", kind="pod", typ="node")]
    rep = hu.report(bookings, samples, at(0, day=10), at(0, day=13), now=at(0, day=13))
    got = {r["holder"]["name"]: r for r in rep["holders"]}
    assert got["alice"]["breakdown"] == [{"cluster": "c1", "type": "vgpu", "booked": 4.0,
                                          "consumed": round(5 / 60, 3)}]
    assert got["ml-team"]["breakdown"] == [{"cluster": "c2", "type": "node", "booked": 3.0, "consumed": 0.0}]
    assert got["ml"]["breakdown"][0]["cluster"] == "c2"
    days = {d["day"]: d for d in rep["days"]}
    assert list(days) == ["2026-10-10", "2026-10-11", "2026-10-12"]
    assert (days["2026-10-10"]["booked"], days["2026-10-11"]["booked"], days["2026-10-12"]["booked"]) == (2.0, 2.0, 3.0)


def test_days_follow_the_time_zone_of_the_person():
    rep = hu.report([bk("a", at(22, day=10), at(23, day=10))], [], at(0, day=10), at(0, day=12), tz=120)
    days = {d["day"]: d["booked"] for d in rep["days"]}
    assert days["2026-10-11"] == 1.0 and days["2026-10-10"] == 0.0           # 22:00 UTC = minuit à UTC+2


def test_period_defaults_and_limits():
    s, e = hu.period(None, None, "2026-10-19T08:00:00Z")
    assert (hu.hb.iso(s), hu.hb.iso(e)) == (P0, P1)
    with pytest.raises(ValueError, match="after its start"):
        hu.period(P1, P0, P0)
    with pytest.raises(ValueError, match="400 days"):
        hu.period("2025-01-01T00:00:00Z", "2026-03-01T00:00:00Z", P0)
    assert hu.tz_from("120") == 120 and hu.tz_from("9999") == 0 and hu.tz_from(None) == 0
    assert hu.hb.iso(hu.next_month("2026-12-15T00:00:00Z")) == "2027-01-01T00:00:00Z"


# -- quotas -------------------------------------------------------------------------------

Q = {"holder": {"kind": "user", "name": "alice"}, "monthly_hours": 10, "max_units": None}


@pytest.mark.parametrize("case,existing,new,quota,codes", [
    ("under the cap", [bk("a", at(9), at(13), count=1)], bk("n", at(14), at(18)), Q, []),
    ("exactly the cap", [bk("a", at(9), at(15))], bk("n", at(16), at(20)), Q, []),
    ("over the cap", [bk("a", at(9), at(15))], bk("n", at(16), at(21)), Q, ["quota-hours"]),
    ("another holder's bookings do not count", [bk("a", at(9), at(19), holder=("user", "bob"))],
     bk("n", at(20), at(23)), Q, []),
    ("a cancelled booking not started does not count", [bk("a", at(9), at(19), cancelled_at=at(8))],
     bk("n", at(20), at(23)), Q, []),
    ("a cancelled booking counts until its cancellation",
     [bk("a", at(9), at(19), cancelled_at=at(17))], bk("n", at(20), at(23)), Q, ["quota-hours"]),
    ("another month does not count", [bk("a", "2026-09-10T00:00:00Z", "2026-09-10T09:00:00Z")],
     bk("n", at(9), at(13)), Q, []),
    ("count multiplies the hours", [], bk("n", at(9), at(12), count=4), Q, ["quota-hours"]),
    ("no quota", [bk("a", at(9), at(23))], bk("n", at(9, day=11), at(23, day=11)), None, []),
    ("max units reached", [bk("a", at(9), at(13), count=2)], bk("n", at(12), at(14)),
     {"holder": Q["holder"], "monthly_hours": None, "max_units": 2}, ["quota-units"]),
    ("max units, back to back passes", [bk("a", at(9), at(13), count=2)], bk("n", at(13), at(14)),
     {"holder": Q["holder"], "monthly_hours": None, "max_units": 2}, []),
    ("max units across clusters", [bk("a", at(9), at(13), cluster="c2")], bk("n", at(10), at(11)),
     {"holder": Q["holder"], "monthly_hours": None, "max_units": 1}, ["quota-units"]),
    ("max units ignores cancelled", [bk("a", at(9), at(13), cancelled_at=at(9, 30))], bk("n", at(10), at(11)),
     {"holder": Q["holder"], "monthly_hours": None, "max_units": 1}, []),
    ("both limits", [bk("a", at(9), at(15), count=1)], bk("n", at(10), at(16)),
     {"holder": Q["holder"], "monthly_hours": 10, "max_units": 1}, ["quota-hours", "quota-units"]),
])
def test_quota_reasons(case, existing, new, quota, codes):
    assert [r["code"] for r in hu.quota_reasons(new, existing, quota)] == codes, case


def test_quota_hours_over_a_month_boundary_checks_each_month():
    q = dict(Q, monthly_hours=5)
    new = bk("n", "2026-10-31T20:00:00Z", "2026-11-01T06:00:00Z")          # 4 h en octobre, 6 h en novembre
    [r] = hu.quota_reasons(new, [], q)
    assert (r["month"], r["adding"], r["used"], r["cap"]) == ("2026-11", 6.0, 0.0, 5)


def test_quota_reason_texts():
    [r] = hu.quota_reasons(bk("n", at(9), at(21)), [], Q)
    assert hu.hb.describe(r) == ("over the quota of user alice: 0.0 GPU-hours already booked in 2026-10, "
                                 "this booking adds 12.0, the monthly cap is 10")


@pytest.mark.parametrize("req,err", [
    ({"holder": {"kind": "namespace", "name": "ml"}, "monthly_hours": 1}, r"person \(user\) or a group"),
    ({"holder": {"kind": "user", "name": ""}, "monthly_hours": 1}, "holder is required"),
    ({"holder": {"kind": "user", "name": "a"}}, "monthly GPU-hours cap"),
    ({"holder": {"kind": "user", "name": "a"}, "monthly_hours": -1}, "from 0 to"),
    ({"holder": {"kind": "user", "name": "a"}, "monthly_hours": "x"}, "must be a number"),
    ({"holder": {"kind": "user", "name": "a"}, "monthly_hours": True}, "must be a number"),
    ({"holder": {"kind": "user", "name": "a"}, "max_units": 1.5}, "whole number"),
    ({"holder": {"kind": "user", "name": "a"}, "max_units": 0}, "whole number"),
    ("nope", "must be an object"),
])
def test_quota_validation(req, err):
    with pytest.raises(ValueError, match=err):
        hu.normalize_quota(req)


def test_quota_normalized():
    assert hu.normalize_quota({"holder": {"kind": "group", "name": " ml "}, "monthly_hours": "12.5",
                               "max_units": "3"}) == \
        {"holder": {"kind": "group", "name": "ml"}, "monthly_hours": 12.5, "max_units": 3}


def test_quota_rows_show_the_month_and_what_is_left():
    q = [{"holder": {"kind": "user", "name": "alice"}, "monthly_hours": 10, "max_units": 2},
         {"holder": {"kind": "group", "name": "idle"}, "monthly_hours": 5, "max_units": None}]
    rep = hu.report([bk("a", at(9), at(21))], [], at(0, day=10), at(0, day=11), quotas=q, now=at(23))
    got = {r["holder"]["name"]: r for r in rep["holders"]}
    assert got["alice"]["quota"] == {"monthly_hours": 10, "max_units": 2, "month_booked": 12.0,
                                     "remaining": 0.0, "over": True}
    assert got["idle"]["booked"] == 0 and got["idle"]["quota"]["remaining"] == 5.0          # quota sans activité
    assert rep["quota_month"] == "2026-10"


def test_quota_month_follows_the_period():
    assert hu.quota_month("2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z", "2026-10-19T00:00:00Z").month == 9
    assert hu.quota_month(P0, P1, "2026-10-19T00:00:00Z").month == 10


# -- relevés depuis l'inventaire, CSV -------------------------------------------------------

def test_samples_from_units_skip_mig_and_unknown_consumers():
    units = [{"id": "g1", "type": "gpu", "model": "H100", "consumers": [
                 {"kind": "vm", "cluster": "c", "namespace": "prod", "name": "train"}]},
             {"id": "m/mig", "type": "mig", "model": "MIG", "consumers": [
                 {"kind": "vm", "cluster": "c", "namespace": "ml", "name": "nb"}]},
             {"id": "n3/nvidia.com/gpu", "type": "node", "model": "nvidia.com/gpu", "consumers": [
                 {"kind": "pod", "namespace": "ml", "name": "trainer-7b-x", "workload": "trainer", "count": 2},
                 {"kind": "pod", "namespace": "ml", "name": ""}, "bad"]},
             {"id": "v", "type": "vgpu", "model": "A2-4Q", "consumers": []}, "junk"]
    assert hu.samples_from_units(units, "c") == [
        {"cluster": "c", "unit": "g1", "type": "gpu", "model": "H100", "kind": "vm", "namespace": "prod",
         "name": "train", "workload": "", "count": 1},
        {"cluster": "c", "unit": "n3/nvidia.com/gpu", "type": "node", "model": "nvidia.com/gpu", "kind": "pod",
         "namespace": "ml", "name": "trainer-7b-x", "workload": "trainer", "count": 2}]


@pytest.mark.parametrize("case,pod,workload", [
    ("deployment by template hash", {"metadata": {"name": "trainer-7b9-x", "labels": {"pod-template-hash": "7b9"},
                                                  "ownerReferences": [{"kind": "ReplicaSet", "name": "trainer-7b9",
                                                                       "controller": True}]}}, "trainer"),
    ("statefulset", {"metadata": {"name": "db-0", "ownerReferences": [{"kind": "StatefulSet", "name": "db"}]}}, "db"),
    ("job", {"metadata": {"name": "j-abc", "ownerReferences": [{"kind": "Job", "name": "j"}]}}, "j"),
    ("bare pod", {"metadata": {"name": "solo"}}, "solo"),
    ("replicaset without hash", {"metadata": {"name": "rs-x", "ownerReferences": [{"kind": "ReplicaSet",
                                                                                   "name": "rs"}]}}, "rs"),
])
def test_pod_workload(case, pod, workload):
    assert hg.pod_workload(pod) == workload, case


def test_csv_rows_and_no_formula():
    rep = hu.report([bk("a", at(9), at(10), holder=("group", "=cmd"), target="vm:ml/nb")],
                    [smp(at(9, 30), "nb")], P0, P1)
    lines = hu.to_csv(rep).splitlines()
    assert lines[0].startswith("holder_kind,holder,cluster,type,booked_gpu_hours")
    assert lines[1].startswith("group,'=cmd,,,1.0,0.083")
    assert lines[2].startswith("group,'=cmd,c1,vgpu,1.0,0.083")
