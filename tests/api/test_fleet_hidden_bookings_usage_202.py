"""2.0.2, décision 96 : Réservations et Usage ne nomment jamais un cluster
que Rancher cache à la personne (même règle et même aide que la décision 93,
_fleet_clusters ; le nombre seulement, `hidden_count`). L'export CSV aussi.

Deux identités, mêmes données : l'administrateur local lit tout ; une
session Rancher sans accès à `secret-lab` ne lit ni ce nom, ni le namespace
ni la personne qui n'existent que sur lui, nulle part dans la réponse.
Une identité déléguée (sans Rancher) voit le cluster, comme en 93."""

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import app as wapp  # noqa: E402
import hv_usage  # noqa: E402

CLUSTERS = ("alpha", "bravo", "secret-lab")
HIDDEN = "secret-lab"
# noms qui n'existent que sur le cluster caché : aucun ne doit sortir
SECRETS = (HIDDEN, "hidden-ns", "bob", "bk-bbbbbbbb0002")
NOW = wapp._hbk.parse_time("2026-10-19T12:00:00Z")
RANGE = "from=2026-10-19T00:00:00Z&to=2026-10-20T00:00:00Z&tz=0"

BOOKINGS = [
    # (id, cluster, titulaire, namespace)
    ("bk-aaaaaaaa0001", "alpha", "alice", "ml"),
    ("bk-bbbbbbbb0002", HIDDEN, "bob", "hidden-ns"),
    ("bk-cccccccc0003", HIDDEN, "alice", "ml"),
]


def _insert(conn, bid, cluster, holder, ns):
    conn.execute("INSERT INTO bookings (id, cluster, type, model, count, holder_kind, holder, start, \"end\","
                 " target_kind, target_namespace, target_name, created_at) VALUES"
                 " (?, ?, 'vgpu', 'M', 1, 'user', ?, '2026-10-19T06:00:00Z', '2026-10-19T10:00:00Z',"
                 " 'vm', ?, 'nb', '2026-10-18T00:00:00Z')", (bid, cluster, holder, ns))


@pytest.fixture
def world(tmp_path, monkeypatch):
    cfg = {"clusters": [{"name": n, "kubeconfig": f"/k/{n}.yaml"} for n in CLUSTERS]}
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")       # mode ouvert
    monkeypatch.setattr(wapp, "load_config", lambda: cfg)
    monkeypatch.setattr(wapp, "load_config_ro", lambda: cfg)
    monkeypatch.setenv("HARVESTER_OPS_BOOKINGS_DB", str(tmp_path / "bookings.db"))
    monkeypatch.setenv("HARVESTER_OPS_USAGE_DB", str(tmp_path / "usage.db"))
    monkeypatch.setattr(wapp, "_bookings_now", lambda: NOW)
    b = wapp._bkdb.connect(tmp_path / "bookings.db")
    for row in BOOKINGS:
        _insert(b, *row)
    wapp._bkdb.set_quota(b, {"holder": {"kind": "user", "name": "alice"}, "monthly_hours": 100, "max_units": None},
                         "patronne", "2026-10-01T00:00:00Z")
    b.close()
    u = wapp._udb.connect(tmp_path / "usage.db")
    seen = {"unit": "v1", "type": "vgpu", "model": "M", "kind": "vm", "namespace": "ml", "name": "nb", "count": 1}
    # sur le cluster caché, une charge hors réservation : titulaire « namespace hidden-ns »
    stray = dict(seen, unit="v9", namespace="hidden-ns", name="stray")
    wapp._udb.record(u, "2026-10-19T07:00:00Z", 300, "alpha", "ok", [seen])
    wapp._udb.record(u, "2026-10-19T07:00:00Z", 300, HIDDEN, "ok", [stray])
    u.close()
    return cfg


def as_admin(monkeypatch):
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: f"/staged/{c}.yaml" if c in CLUSTERS else None)


def as_rancher_member(monkeypatch):
    """Session Rancher sans accès à `secret-lab` (le vrai _kubectl_for_cluster
    passe par _sso_kubeconfig puis _sso_cluster_state, seul simulé)."""
    monkeypatch.setattr(wapp, "_sso_session",
                        lambda: types.SimpleNamespace(login="member", role="operator", token="t"))
    monkeypatch.setattr(wapp, "_session_settings", lambda sess: {"origin": "config"})
    monkeypatch.setattr(wapp, "_sso_cluster_state",
                        lambda e: (None, "no-access") if e["name"] == HIDDEN else (f"/sso/{e['name']}.yaml", None))


def as_delegated(monkeypatch):
    monkeypatch.setattr(wapp, "_kubectl_for_cluster",
                        lambda c: f"/deny/{c}.yaml" if c == HIDDEN else f"/as/{c}.yaml")


IDENTITIES = {"admin": as_admin, "rancher-member": as_rancher_member, "delegated": as_delegated}


def _get(path):
    with wapp.app.test_client() as c:
        r = c.get(path)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r


@pytest.mark.parametrize("identity,clusters,hidden", [
    ("admin", ["alpha", HIDDEN], 0),
    ("rancher-member", ["alpha"], 1),
    ("delegated", ["alpha", HIDDEN], 0),
])
def test_the_bookings_list_never_names_a_hidden_cluster(world, monkeypatch, identity, clusters, hidden):
    IDENTITIES[identity](monkeypatch)
    r = _get("/api/bookings")
    body, raw = r.get_json(), r.get_data(as_text=True)
    assert sorted({b["cluster"] for b in body["bookings"]}) == clusters
    assert body["hidden_count"] == hidden
    if hidden:
        assert not [s for s in SECRETS if s in raw]


@pytest.mark.parametrize("identity,clusters,holders,hidden", [
    ("admin", ["alpha", HIDDEN], ["alice", "bob", "hidden-ns"], 0),
    ("rancher-member", ["alpha"], ["alice"], 1),
    ("delegated", ["alpha", HIDDEN], ["alice", "bob", "hidden-ns"], 0),
])
def test_the_usage_report_never_names_a_hidden_cluster(world, monkeypatch, identity, clusters, holders, hidden):
    IDENTITIES[identity](monkeypatch)
    r = _get(f"/api/usage?{RANGE}")
    body, raw = r.get_json(), r.get_data(as_text=True)
    parts = {p["cluster"] for h in body["holders"] for p in h["breakdown"]}
    assert sorted(parts) == clusters
    assert sorted(h["holder"]["name"] for h in body["holders"]) == holders
    assert sorted(c["cluster"] for c in body["sampler"]["clusters"]) == clusters
    assert body["hidden_count"] == hidden
    # le quota applique toutes les réservations : un nombre, le même pour tous
    [alice] = [h for h in body["holders"] if h["holder"]["name"] == "alice"]
    assert alice["quota"]["month_booked"] == 8.0
    assert alice["booked"] == (4.0 if hidden else 8.0)
    if hidden:
        assert not [s for s in SECRETS if s in raw]


@pytest.mark.parametrize("identity,leak", [("admin", True), ("rancher-member", False), ("delegated", True)])
def test_the_usage_csv_never_names_a_hidden_cluster(world, monkeypatch, identity, leak):
    IDENTITIES[identity](monkeypatch)
    raw = _get(f"/api/usage.csv?{RANGE}").get_data(as_text=True)
    assert (HIDDEN in raw, "bob" in raw, "hidden-ns" in raw) == (leak, leak, leak)
    assert ",alpha," in raw


@pytest.mark.parametrize("bid,status", [("bk-bbbbbbbb0002", 404), ("bk-aaaaaaaa0001", 403)])
def test_a_booking_of_a_hidden_cluster_cannot_be_cancelled_nor_confirmed(world, monkeypatch, bid, status):
    """La session Rancher (opératrice) : la réservation du cluster caché
    n'existe pas pour elle (404, comme un identifiant inconnu) ; la visible
    est refusée pour une autre raison (pas la sienne)."""
    as_rancher_member(monkeypatch)
    with wapp.app.test_client() as c:
        r = c.post(f"/api/bookings/{bid}/cancel", json={}, headers={"Origin": "http://localhost"})
    assert r.status_code == status, r.get_data(as_text=True)
    assert HIDDEN not in r.get_data(as_text=True)


def test_mutation_without_the_filter_the_hidden_names_leak(world, monkeypatch):
    """Mutation volontaire : sans le filtre (aide neutralisée), les mêmes
    assertions voient fuir le nom. Prouve que les tests ci-dessus détectent."""
    as_rancher_member(monkeypatch)
    monkeypatch.setattr(wapp, "_fleet_cluster_allow", lambda: (None, 1))
    for path in ("/api/bookings", f"/api/usage?{RANGE}", f"/api/usage.csv?{RANGE}"):
        assert HIDDEN in _get(path).get_data(as_text=True)


def test_the_report_quota_counts_every_booking_but_shows_only_the_given_ones():
    """hv_usage.report : `quota_bookings` décompte le quota, `bookings` le détail."""
    mk = (lambda c: {"id": c, "cluster": c, "type": "vgpu", "model": "M", "count": 1, "holder": {"kind": "user", "name": "alice"}, "start": "2026-10-19T06:00:00Z", "end": "2026-10-19T10:00:00Z"})
    every = [mk("alpha"), mk(HIDDEN)]
    q = [{"holder": {"kind": "user", "name": "alice"}, "monthly_hours": 10, "max_units": None}]
    rep = hv_usage.report(every[:1], [], "2026-10-19T00:00:00Z", "2026-10-20T00:00:00Z", quotas=q,
                          now="2026-10-19T12:00:00Z", quota_bookings=every)
    [a] = rep["holders"]
    assert (a["booked"], a["quota"]["month_booked"], [p["cluster"] for p in a["breakdown"]]) == (4.0, 8.0, ["alpha"])


def test_the_screens_show_only_an_anonymous_count():
    for f, attr in (("bookings.js", 'data-bkg="hidden"'), ("usage.js", 'data-usg="hidden"')):
        js = (ROOT / "web/static/js" / f).read_text()
        assert attr in js and "tr('avm.cl.hidden', { n: d.hidden_count })" in js
