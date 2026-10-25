"""2.0.0 : logique pure du menu des hôtes (`web/static/js/host-nav.js`).

Chargée dans node (comme `test_routes_js_1100.py`). Pas de plafond
(décision 6 du 25/10/2026) : tous les hôtes sont listés, favoris en tête,
puis par nom. Une famille repliée garde l'hôte ouvert et les hôtes en
alerte : une alerte n'est jamais cachée."""

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent

RUNNER = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const H = require('./web/static/js/host-nav.js');
if (input.mutate === 'alert-hidden') {
  // mutation : un repli cache aussi les alertes
  const orig = H.isAlert;
  H.visible = function (hosts, openKey, collapsed) {
    return H.order(hosts).filter((h) => !collapsed || H.keyOf(h) === openKey)
      .map((h) => ({ key: H.keyOf(h), reason: h.favorite ? 'favorite' : 'rest' }));
  };
}
const out = input.cases.map((c) => ({
  visible: H.visible(c.hosts, c.open || null, !!c.collapsed),
  matches: (c.queries || []).map((q) => c.hosts.filter((h) => H.matches(h, q)).map(H.keyOf)),
  summary: H.summaryParts(c.hosts),
}));
console.log(JSON.stringify(out));
"""


def run(cases, mutate=None):
    try:
        r = subprocess.run(["node", "-e", RUNNER], input=json.dumps({"cases": cases, "mutate": mutate}),
                           capture_output=True, text=True, cwd=str(ROOT), timeout=30)
    except FileNotFoundError:
        pytest.skip("node absent")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def h(name, kind="harvester", state="ok", favorite=False, labels=None):
    return {"kind": kind, "name": name, "state": state, "favorite": favorite, "labels": labels or {}}


def fleet(n, kind="harvester", alerts=(), favs=(), prefix="harv-"):
    return [h(f"{prefix}{i:02d}", kind, "unreachable" if i in alerts else "ok", i in favs) for i in range(1, n + 1)]


SIXTY = fleet(60, alerts=(59, 60), favs=(3, 17, 44))


# (nom, hôtes, hôte ouvert, replié, clés attendues dans l'ordre, raisons attendues par clé)
CASES = [
    ("five-all-visible", fleet(5), None, False,
     [f"harvester/harv-0{i}" for i in range(1, 6)], {}),
    ("nine-no-cap", fleet(9), None, False,
     [f"harvester/harv-0{i}" for i in range(1, 10)], {}),
    ("sixty-favourites-first-no-cap", SIXTY, "harvester/harv-40", False,
     ["harvester/harv-03", "harvester/harv-17", "harvester/harv-44"]
     + [f"harvester/harv-{i:02d}" for i in range(1, 61) if i not in (3, 17, 44)],
     {"harvester/harv-03": "favorite", "harvester/harv-40": "open",
      "harvester/harv-59": "alert", "harvester/harv-60": "alert", "harvester/harv-01": "rest"}),
    ("sixty-collapsed-keeps-alerts-and-open", SIXTY, "harvester/harv-40", True,
     ["harvester/harv-40", "harvester/harv-59", "harvester/harv-60"],
     {"harvester/harv-40": "open", "harvester/harv-59": "alert"}),
    ("collapsed-without-alert-shows-only-open", fleet(8), "harvester/harv-02", True,
     ["harvester/harv-02"], {}),
    ("collapsed-nothing-open", fleet(8), None, True, [], {}),
    ("ties-by-name-case-insensitive-then-exact",
     [h("beta"), h("Alpha"), h("alpha"), h("Beta", favorite=True), h("gamma", state="denied")], None, False,
     ["harvester/Beta", "harvester/Alpha", "harvester/alpha", "harvester/beta", "harvester/gamma"],
     {"harvester/Beta": "favorite", "harvester/gamma": "alert"}),
    ("favourite-alert-reason-is-favourite", [h("a", state="error", favorite=True), h("b")], None, True,
     ["harvester/a"], {"harvester/a": "favorite"}),
]


@pytest.mark.parametrize("name,hosts,open_,collapsed,keys,reasons", CASES, ids=[c[0] for c in CASES])
def test_visible(name, hosts, open_, collapsed, keys, reasons):
    [out] = run([{"hosts": hosts, "open": open_, "collapsed": collapsed}])
    assert [x["key"] for x in out["visible"]] == keys
    got = {x["key"]: x["reason"] for x in out["visible"]}
    for k, r in reasons.items():
        assert got[k] == r


def test_a_hundred_hosts_all_listed_alerts_included():
    hosts = fleet(60, alerts=(59, 60)) + fleet(40, kind="kvm", prefix="kvm-")
    [out] = run([{"hosts": hosts}])
    assert len(out["visible"]) == 100
    assert out["summary"] == {"total": 100, "ok": 98, "alert": 2, "unknown": 0}


@pytest.mark.parametrize("query,expected", [
    ("", ["harvester/harv-paris", "harvester/harv-lyon", "kvm/kvm-paris.01"]),
    ("paris", ["harvester/harv-paris", "kvm/kvm-paris.01"]),
    ("site=lyon", ["harvester/harv-lyon"]),
    ("env", ["harvester/harv-paris", "harvester/harv-lyon"]),
    ("kvm", ["kvm/kvm-paris.01"]),
    ("harvester prod", ["harvester/harv-paris"]),
    ("PARIS", ["harvester/harv-paris", "kvm/kvm-paris.01"]),
    ("nothing", []),
])
def test_matches_by_name_label_and_family(query, expected):
    hosts = [h("harv-paris", labels={"site": "paris", "env": "prod"}),
             h("harv-lyon", labels={"site": "lyon", "env": "dr"}),
             h("kvm-paris.01", "kvm")]
    [out] = run([{"hosts": hosts, "queries": [query]}])
    assert out["matches"][0] == expected


@pytest.mark.parametrize("state,bucket", [("ok", "ok"), ("unreachable", "alert"), ("denied", "alert"),
                                          ("error", "alert"), ("unknown", "unknown"), (None, "unknown")])
def test_summary_buckets(state, bucket):
    [out] = run([{"hosts": [h("x", state=state)]}])
    assert out["summary"][bucket] == 1 and out["summary"]["total"] == 1


def test_mutation_alert_hidden_is_caught():
    """test_hosts_mutation_alert_hidden : un repli qui cacherait les alertes
    fait échouer la ligne « sixty-collapsed-keeps-alerts-and-open »."""
    case = next(c for c in CASES if c[0] == "sixty-collapsed-keeps-alerts-and-open")
    [out] = run([{"hosts": case[1], "open": case[2], "collapsed": case[3]}], mutate="alert-hidden")
    assert [x["key"] for x in out["visible"]] != case[4]
