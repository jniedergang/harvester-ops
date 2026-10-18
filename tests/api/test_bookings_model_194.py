"""v1.94.0 : le modèle des réservations (bin/lib/hv_bookings.py), sans I/O.

Créneaux semi-ouverts (bout à bout autorisé), même unité refusée sur des
créneaux qui se chevauchent, capacité dépassée à un seul instant d'un
créneau, capacité MIG = instances créées du profil, annulées ignorées,
créneaux invalides, plafond de 90 jours."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import hv_bookings as hb  # noqa: E402

NOW = datetime(2026, 10, 18, 8, 0, tzinfo=timezone.utc)


def unit(uid, typ, model, state="free", total=1, used=0, **extra):
    return dict({"id": uid, "type": typ, "cluster": "c", "node": "n1", "name": uid, "model": model,
                 "address": "", "state": state, "total": total, "used": used, "free": total - used,
                 "resource": extra.pop("resource", "")}, **extra)


# l'inventaire de gpudemo (relevé le 18/10/2026), en abrégé
UNITS = [
    unit("g1", "gpu", "H100", "used", used=1),
    unit("g2", "gpu", "H100", "disabled"),                 # découpé en vGPU
    unit("v1", "vgpu", "NVIDIA H100-20C", "used", used=1),
    unit("v2", "vgpu", "NVIDIA H100-20C"),
    unit("v3", "vgpu", "", "disabled"),
    unit("v4", "vgpu", "NVIDIA A100-3-40C"),
    unit("m1/mig", "mig", "MIG 3g.40gb x2", total=2, used=1, profiles=[
        {"name": "MIG 3g.40gb", "id": 9, "created": 2, "available": 0, "total": 2},
        {"name": "MIG 1g.10gb", "id": 19, "created": 0, "available": 7, "total": 7}]),
    unit("m2/mig", "mig", "MIG 3g.40gb x1", total=1, profiles=[
        {"name": "MIG 3g.40gb", "id": 9, "created": 1, "available": 1, "total": 2}]),
    unit("m3/mig", "mig", "", "disabled", total=0, profiles=[
        {"name": "MIG 3g.40gb", "id": 9, "created": 3, "available": 0, "total": 3}]),
    unit("n3/nvidia.com/gpu", "node", "nvidia.com/gpu", total=4, used=2, resource="nvidia.com/gpu"),
    unit("n4/nvidia.com/gpu", "node", "nvidia.com/gpu", "disabled", total=8, resource="nvidia.com/gpu"),
]


def bk(start, end, count=1, typ="vgpu", model="NVIDIA H100-20C", unit="", bid=None, cancelled=False, cluster="c"):
    return {"id": bid or f"bk-{start[8:10]}{start[11:13]}{end[11:13]}{count}{unit or 'x'}",
            "cluster": cluster, "type": typ, "unit": unit, "model": model, "count": count,
            "holder": {"kind": "user", "name": "alice"}, "start": start, "end": end,
            "cancelled_at": "2026-10-18T07:00:00Z" if cancelled else ""}


def T(h, day=20):
    return f"2026-10-{day:02d}T{h:02d}:00:00Z"


# -- capacité --------------------------------------------------------------------

@pytest.mark.parametrize("typ,model,cap", [
    ("gpu", "H100", 1),                         # la carte découpée en vGPU ne compte pas
    ("vgpu", "NVIDIA H100-20C", 2),             # le vGPU désactivé (sans type) non plus
    ("vgpu", "NVIDIA A100-3-40C", 1),
    ("mig", "MIG 3g.40gb", 3),                  # instances CRÉÉES, sur deux GPU ; le GPU MIG coupé ne compte pas
    ("mig", "MIG 1g.10gb", 0),                  # possible (total 7) mais aucune créée
    ("node", "nvidia.com/gpu", 4),              # le nœud cordonné ne compte pas
    ("vgpu", "NVIDIA L4-2Q", 0),
])
def test_capacity_from_the_inventory(typ, model, cap):
    assert hb.capacity(UNITS, typ, model) == cap


def test_catalog_lists_bookable_pools_with_units_for_unit_types():
    cat = {(e["type"], e["model"]): e for e in hb.catalog(UNITS)}
    assert set(cat) == {("gpu", "H100"), ("vgpu", "NVIDIA H100-20C"), ("vgpu", "NVIDIA A100-3-40C"),
                        ("mig", "MIG 3g.40gb"), ("node", "nvidia.com/gpu")}
    assert [u["id"] for u in cat[("vgpu", "NVIDIA H100-20C")]["units"]] == ["v1", "v2"]
    assert cat[("vgpu", "NVIDIA H100-20C")]["used"] == 1
    assert cat[("mig", "MIG 3g.40gb")]["used"] == 1 and cat[("mig", "MIG 3g.40gb")]["units"] == []
    assert cat[("node", "nvidia.com/gpu")]["used"] == 2
    assert [e["type"] for e in hb.catalog(UNITS)] == ["gpu", "vgpu", "vgpu", "mig", "node"]


@pytest.mark.parametrize("uid,typ,err", [
    ("v2", "vgpu", None),
    ("v3", "vgpu", "disabled"),
    ("g2", "gpu", "disabled"),
    ("g1", "vgpu", "is a gpu"),
    ("zz", "gpu", "not in the GPU inventory"),
])
def test_resolve_takes_the_model_of_the_unit(uid, typ, err):
    b = {"cluster": "c", "type": typ, "unit": uid, "model": "", "count": 1}
    if err:
        with pytest.raises(ValueError, match=err):
            hb.resolve(b, UNITS)
    else:
        assert hb.resolve(b, UNITS)["model"] == "NVIDIA H100-20C"


# -- chevauchements et conflits ----------------------------------------------------

@pytest.mark.parametrize("a,b,overlap", [
    ((9, 13), (13, 15), False),       # bout à bout
    ((13, 15), (9, 13), False),
    ((9, 13), (12, 14), True),        # chevauche l'extrémité
    ((9, 13), (8, 9), False),         # finit quand l'autre commence
    ((9, 13), (10, 11), True),        # contenu
    ((10, 11), (9, 13), True),        # contenant
    ((9, 13), (9, 13), True),         # identique
])
def test_overlaps_are_half_open(a, b, overlap):
    assert hb.overlaps(bk(T(a[0]), T(a[1])), bk(T(b[0]), T(b[1]))) is overlap


@pytest.mark.parametrize("existing,new,codes", [
    # même unité
    ([bk(T(9), T(13), unit="v2")], bk(T(12), T(14), unit="v2"), ["unit-taken"]),
    ([bk(T(9), T(13), unit="v2")], bk(T(13), T(14), unit="v2"), []),                       # bout à bout
    ([bk(T(9), T(13), unit="v2")], bk(T(8), T(9), unit="v2"), []),
    ([bk(T(9), T(13), unit="v1")], bk(T(9), T(13), unit="v2"), []),                        # autre unité, capacité 2
    ([bk(T(9), T(13), unit="v2", cancelled=True)], bk(T(10), T(11), unit="v2"), []),       # annulée ignorée
    ([bk(T(9), T(13), unit="v2", cluster="other")], bk(T(10), T(11), unit="v2"), []),      # autre cluster
    # par nombre : capacité 2
    ([bk(T(9), T(13), 2)], bk(T(12), T(14), 1), ["capacity"]),
    ([bk(T(9), T(13), 2)], bk(T(13), T(14), 2), []),
    ([bk(T(9), T(11), 1), bk(T(12), T(14), 1)], bk(T(9), T(14), 1), []),                   # jamais 3 en même temps
    ([bk(T(9), T(11), 1), bk(T(10), T(14), 1)], bk(T(8), T(15), 1), ["capacity"]),        # 3 entre 10 et 11 seulement
    ([bk(T(9), T(11), 1), bk(T(11), T(14), 1)], bk(T(8), T(15), 1), []),                   # bout à bout : jamais 3
    ([bk(T(9), T(13), 1, unit="v1")], bk(T(10), T(12), 2), ["capacity"]),                 # l'unité précise compte dans son modèle
    ([bk(T(9), T(13), 2, cancelled=True)], bk(T(9), T(13), 2), []),
    ([bk(T(9), T(13), 2, model="NVIDIA A100-3-40C")], bk(T(9), T(13), 2), []),            # autre modèle
    ([], bk(T(9), T(13), 3), ["capacity"]),                                                # plus que le cluster
    # les deux à la fois
    ([bk(T(9), T(13), 1, unit="v2"), bk(T(9), T(13), 1)], bk(T(10), T(11), unit="v2"), ["unit-taken", "capacity"]),
])
def test_conflicts(existing, new, codes):
    assert [r["code"] for r in hb.conflicts(new, existing, 2)] == codes


def test_capacity_conflict_names_the_peak_and_the_instant():
    existing = [bk(T(9), T(11), 1, bid="bk-aaaaaaaa"), bk(T(10), T(14), 1, bid="bk-bbbbbbbb")]
    [r] = hb.conflicts(bk(T(8), T(15), 1), existing, 2)
    assert (r["peak"], r["capacity"], r["at"]) == (3, 2, T(10))
    assert "3 x vgpu NVIDIA H100-20C would be booked at 2026-10-20T10:00:00Z, the cluster has 2" == hb.describe(r)


def test_mig_profile_capacity():
    cap = hb.capacity(UNITS, "mig", "MIG 3g.40gb")
    existing = [bk(T(9), T(13), 2, typ="mig", model="MIG 3g.40gb")]
    assert hb.conflicts(bk(T(10), T(12), 1, typ="mig", model="MIG 3g.40gb"), existing, cap) == []
    assert [r["code"] for r in hb.conflicts(bk(T(10), T(12), 2, typ="mig", model="MIG 3g.40gb"), existing, cap)] == ["capacity"]
    assert [r["code"] for r in hb.conflicts(bk(T(10), T(12), 1, typ="mig", model="MIG 1g.10gb"), [],
                                            hb.capacity(UNITS, "mig", "MIG 1g.10gb"))] == ["capacity"]


def test_a_booking_does_not_conflict_with_itself():
    b = bk(T(9), T(13), 2, bid="bk-cccccccc")
    assert hb.conflicts(b, [b], 2) == []


def test_peak_counts_ends_before_starts():
    assert hb.peak([bk(T(9), T(11), 2), bk(T(11), T(13), 2)], T(8), T(14)) == (2, T(9))
    assert hb.peak([], T(8), T(14)) == (0, None)


# -- validation --------------------------------------------------------------------

def req(**over):
    r = {"cluster": "gpudemo", "type": "vgpu", "model": "NVIDIA H100-20C", "count": 2,
         "holder": {"kind": "user", "name": "alice"}, "start": T(9), "end": T(13), "note": " hello "}
    r.update(over)
    return r


def test_normalize_keeps_a_clean_booking():
    b = hb.normalize(req(target={"kind": "vm", "namespace": "ml", "name": "notebook"},
                         start="2026-10-20T11:00:00+02:00"), NOW)
    assert b == {"cluster": "gpudemo", "type": "vgpu", "unit": "", "model": "NVIDIA H100-20C", "count": 2,
                 "holder": {"kind": "user", "name": "alice"}, "start": T(9), "end": T(13),
                 "target": {"kind": "vm", "namespace": "ml", "name": "notebook"}, "note": "hello"}


@pytest.mark.parametrize("over,msg", [
    ({"end": T(9)}, "end must come after"),
    ({"end": T(8)}, "end must come after"),
    ({"start": T(9, 1), "end": T(10, 1)}, "already over"),              # le 1er octobre, passé
    ({"end": "2027-01-18T09:00:01Z"}, "90 days at most"),
    ({"start": "lundi"}, "unreadable date"),
    ({"start": None}, "ISO 8601"),
    ({"type": "tpu"}, "type must be"),
    ({"cluster": "bad name!"}, "invalid cluster"),
    ({"count": 0}, "count must be"),
    ({"count": 65}, "count must be"),
    ({"count": True}, "count must be"),
    ({"count": "2"}, "count must be"),
    ({"model": ""}, "pick a specific unit"),
    ({"unit": "n3/nvidia.com/gpu", "type": "node", "count": 1}, "whole GPUs and vGPUs only"),
    ({"unit": "v2", "count": 2}, "one at a time"),
    ({"holder": {"kind": "team", "name": "x"}}, "must be a person"),
    ({"holder": {"kind": "group", "name": "  "}}, "holder is required"),
    ({"target": {"kind": "vm", "namespace": "ML", "name": "x"}}, "RFC 1123"),
    ({"target": {"kind": "disk", "namespace": "a", "name": "b"}}, "VM or a container workload"),
    ({"note": "x" * 501}, "500 characters"),
    ({"note": "a\x00b"}, "control characters"),
])
def test_normalize_refuses(over, msg):
    with pytest.raises(ValueError, match=msg):
        hb.normalize(req(**over), NOW)


def test_ninety_days_exactly_is_allowed():
    assert hb.normalize(req(end="2027-01-18T09:00:00Z"), NOW)["end"] == "2027-01-18T09:00:00Z"


def test_a_slot_that_started_but_is_not_over_is_allowed():
    assert hb.normalize(req(start=T(7, 18), end=T(10, 18)), NOW)["start"] == T(7, 18)


# -- états et droits ---------------------------------------------------------------

@pytest.mark.parametrize("now,cancelled,st", [
    (datetime(2026, 10, 20, 8, 59, tzinfo=timezone.utc), False, "upcoming"),
    (datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc), False, "active"),
    (datetime(2026, 10, 20, 12, 59, tzinfo=timezone.utc), False, "active"),
    (datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc), False, "ended"),
    (datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc), True, "cancelled"),
])
def test_state(now, cancelled, st):
    assert hb.state(bk(T(9), T(13), cancelled=cancelled), now) == st


@pytest.mark.parametrize("b,user,role,ok", [
    ({"created_by": "alice", "holder": {"kind": "user", "name": "bob"}}, "alice", "operator", True),
    ({"created_by": "bob", "holder": {"kind": "user", "name": "alice"}}, "alice", "operator", True),
    ({"created_by": "bob", "holder": {"kind": "group", "name": "alice"}}, "alice", "operator", False),
    ({"created_by": "bob", "holder": {"kind": "user", "name": "bob"}}, "alice", "operator", False),
    ({"created_by": "bob", "holder": {"kind": "user", "name": "bob"}}, "alice", "admin", True),
    ({"created_by": "alice", "holder": {"kind": "user", "name": "alice"}}, "alice", "viewer", False),
    ({"created_by": "", "holder": {"kind": "user", "name": ""}}, "", "operator", False),
])
def test_can_cancel(b, user, role, ok):
    assert hb.can_cancel(b, user, role) is ok


def test_booked_at_counts_live_bookings_at_an_instant():
    bs = [bk(T(9), T(13), 2), bk(T(10), T(11), 1, unit="v1"), bk(T(9), T(13), 1, cancelled=True),
          bk(T(9), T(13), 1, model="other")]
    assert hb.booked_at(bs, "vgpu", "NVIDIA H100-20C", T(10)) == 3
    assert hb.booked_at(bs, "vgpu", "NVIDIA H100-20C", T(11)) == 2
    assert hb.booked_at(bs, "vgpu", "NVIDIA H100-20C", T(13)) == 0
