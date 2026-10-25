"""2.0.0 : la liste des hôtes du menu (GET /api/hosts).

Un hôte est un cluster Harvester entier ou un hyperviseur KVM. Le menu ne
lance aucune lecture par hôte : l'état d'un cluster vient de la sonde TCP en
cache, son nombre de VMs et sa version de ce que les écrans ont déjà lu
(lectures partagées), l'état d'un hôte KVM de la dernière lecture de
/api/kvm/hosts. Tout ce qui n'a pas été lu est rendu null ou « unknown »."""

import json
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import app as wapp  # noqa: E402
import kvm_decl as kd  # noqa: E402
import kvm_host as kh  # noqa: E402
import read_share as rsh  # noqa: E402

CLUSTERS = ["harv1", "Lab_1", "gpudemo", "harvlab2"]
# harvlab2 est éteint ; Lab_1 vient de la console (pas de config.yaml)
CONFIG_NAMES = {"harv1", "gpudemo", "harvlab2"}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def kvm_decl(name, labels=None):
    return {"name": name, "address": "10.0.0.9", "port": 22, "user": "ju", "ssh_key": kd.key_rel(name),
            "known_hosts": kd.known_hosts_rel(name), "labels": labels or {}}


def _trap(name):
    def boom(*a, **k):
        raise AssertionError(f"/api/hosts must not read a host: {name} called")
    return boom


@pytest.fixture
def world(monkeypatch, tmp_path):
    state = Path(tempfile.mkdtemp(prefix="hst-", dir="/tmp"))
    kd.write(state, kvm_decl("node2", {"site": "cave"}))
    kd.write(state, kvm_decl("kvm-paris.01"))
    cfg = {"clusters": [{"name": n, "kubeconfig": f"/k/{n}.yaml",
                         **({"labels": {"site": "paris"}} if n == "harv1" else {})} for n in CLUSTERS]}
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")       # mode ouvert : admin
    monkeypatch.setattr(wapp, "load_config", lambda: cfg)
    monkeypatch.setattr(wapp, "load_config_ro", lambda: cfg)
    monkeypatch.setattr(wapp, "_config_cluster_names", lambda: set(CONFIG_NAMES))
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: f"/staged/{c}.yaml" if c in CLUSTERS else None)
    reach = {n: True for n in CLUSTERS}
    reach["harvlab2"] = False
    probes = []

    def reachable(kc, **k):
        probes.append(kc)
        return reach[Path(kc).stem]
    monkeypatch.setattr(wapp, "_cluster_reachable", reachable)
    monkeypatch.setattr(wapp, "_state_dir", lambda: state)
    monkeypatch.setenv("HARVESTER_OPS_FOLDERS_DB", str(tmp_path / "folders.db"))
    clock = Clock()
    monkeypatch.setattr(wapp, "READ_SHARE", rsh.ReadShare(clock=clock))
    monkeypatch.setattr(wapp, "READ_SHARE_ENABLED", True)     # éteintes par tests/api/conftest.py
    # garde « aucune lecture par hôte » : tout appel échoue le test
    monkeypatch.setattr(wapp, "_kubectl_run", _trap("_kubectl_run"))
    monkeypatch.setattr(kh.VirshHost, "batch", _trap("VirshHost.batch"))
    monkeypatch.setattr(kh, "host_inventory", _trap("host_inventory"))
    yield {"state": state, "reach": reach, "probes": probes, "clock": clock, "cfg": cfg}
    import shutil
    shutil.rmtree(state, ignore_errors=True)


def hosts(c, **params):
    r = c.get("/api/hosts", query_string=params)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def by_key(d):
    return {(h["kind"], h["name"]): h for h in d["hosts"]}


# ---------------------------------------------------------------------------
# Forme et sources de l'état
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind,name,state,origin,labels", [
    ("harvester", "harv1", "ok", "config", {"site": "paris"}),
    ("harvester", "Lab_1", "ok", "console", {}),
    ("harvester", "gpudemo", "ok", "config", {}),
    ("harvester", "harvlab2", "unreachable", "config", {}),
    ("kvm", "node2", "unknown", "console", {"site": "cave"}),
    ("kvm", "kvm-paris.01", "unknown", "console", {}),
])
def test_every_host_with_its_state_and_origin(world, kind, name, state, origin, labels):
    with wapp.app.test_client() as c:
        h = by_key(hosts(c))[(kind, name)]
    assert (h["state"], h["origin"], h["labels"]) == (state, origin, labels)
    assert h["vm_count"] is None and h["version"] is None and h["error"] is None
    assert h["favorite"] is False and h["folder"] is None


def test_order_family_heads_and_no_cap(world):
    with wapp.app.test_client() as c:
        d = hosts(c)
    assert [(h["kind"], h["name"]) for h in d["hosts"]] == [
        ("harvester", "gpudemo"), ("harvester", "harv1"), ("harvester", "harvlab2"), ("harvester", "Lab_1"),
        ("kvm", "kvm-paris.01"), ("kvm", "node2")]
    assert d["families"] == {"harvester": {"ok": 3, "alert": 1, "unknown": 0},
                             "kvm": {"ok": 0, "alert": 0, "unknown": 2}}
    # décision 6 : aucun plafond par famille
    assert "cap" not in d


def test_an_unreachable_cluster_is_said_by_the_tcp_probe_only(world):
    with wapp.app.test_client() as c:
        h = by_key(hosts(c))[("harvester", "harvlab2")]
    assert h["state"] == "unreachable"
    assert sorted(world["probes"]) == sorted(f"/staged/{n}.yaml" for n in CLUSTERS)


def test_a_cluster_hidden_by_rancher_is_absent(world, monkeypatch):
    monkeypatch.setattr(wapp, "_sso_session", lambda: types.SimpleNamespace(login="rs", role="viewer", token="t"))
    monkeypatch.setattr(wapp, "_sso_cluster_state",
                        lambda e: (None, "no-access") if e["name"] == "gpudemo" else (f"/staged/{e['name']}.yaml", None))
    with wapp.app.test_client() as c:
        d = hosts(c)
    names = [h["name"] for h in d["hosts"] if h["kind"] == "harvester"]
    assert names == ["harv1", "harvlab2", "Lab_1"]
    assert d["families"]["harvester"]["ok"] == 2


# ---------------------------------------------------------------------------
# Nombre de VMs et version : repris des lectures déjà faites
# ---------------------------------------------------------------------------
def _fleet(monkeypatch, denied=()):
    def collect(name, kc):
        if name in denied:
            return {"error": "kubectl failed", "detail": "forbidden"}, 500, ["Error from server (Forbidden): x"]
        n = {"harv1": 3, "Lab_1": 1, "gpudemo": 0}[name]
        return {"cluster": name, "vms": [{"name": f"vm{i}", "namespace": "default"} for i in range(n)]}, 200, []
    monkeypatch.setattr(wapp, "_vms_collect", collect)


def test_vm_count_null_then_filled_after_the_fleet_view(world, monkeypatch):
    _fleet(monkeypatch)
    with wapp.app.test_client() as c:
        before = by_key(hosts(c))
        assert all(h["vm_count"] is None for h in before.values())
        assert c.get("/api/vms-all").status_code == 200
        world["clock"].t += 4            # la liste des hôtes (3 s) se relit
        after = by_key(hosts(c))
    assert after[("harvester", "harv1")]["vm_count"] == 3
    assert after[("harvester", "Lab_1")]["vm_count"] == 1
    assert after[("harvester", "gpudemo")]["vm_count"] == 0
    assert after[("harvester", "harvlab2")]["vm_count"] is None


def test_a_denied_fleet_read_is_an_alert_with_its_reason(world, monkeypatch):
    _fleet(monkeypatch, denied=("gpudemo",))
    with wapp.app.test_client() as c:
        c.get("/api/vms-all")
        h = by_key(hosts(c))[("harvester", "gpudemo")]
    assert (h["state"], h["error"], h["vm_count"]) == ("denied", "forbidden", None)


def test_a_too_old_screen_read_is_not_reused(world, monkeypatch):
    _fleet(monkeypatch)
    with wapp.app.test_client() as c:
        c.get("/api/vms-all")
        world["clock"].t += wapp.HOSTS_PEEK_MAX_AGE + 1
        h = by_key(hosts(c))[("harvester", "harv1")]
    assert h["vm_count"] is None


def test_count_and_version_from_status_and_upgrade_reads(world):
    key_status = wapp._read_share_key  # même forme que les écrans
    body_status = json.dumps({"summary": {"vms_total": 7}}).encode()
    body_upg = json.dumps({"cluster": "harv1", "current": "v1.9.0"}).encode()
    with wapp.app.test_request_context("/api/hosts"):
        k1 = key_status("api_status", {"cluster": "harv1"})
        k2 = key_status("api_upgrade", {"cluster": "harv1"})
    for k, body in ((k1, body_status), (k2, body_upg)):
        wapp.READ_SHARE.get(k, 3, lambda b=body: (200, b, "application/json", [], None, []))
    with wapp.app.test_client() as c:
        h = by_key(hosts(c))[("harvester", "harv1")]
    assert (h["vm_count"], h["version"]) == (7, "v1.9.0")


@pytest.mark.parametrize("entry,state,count,error", [
    ({"name": "node2", "vms": [{"name": "a"}, {"name": "b"}], "libvirt_version": "12.7.0"}, "ok", 2, None),
    ({"name": "node2", "error": {"kind": "unreachable", "message": "node2: unreachable"}}, "unreachable", None,
     "node2: unreachable"),
    ({"name": "node2", "error": {"kind": "timeout", "message": "t"}}, "unreachable", None, "t"),
    ({"name": "node2", "error": {"kind": "auth", "message": "a"}}, "denied", None, "a"),
    ({"name": "node2", "error": {"kind": "failed", "message": "f"}}, "error", None, "f"),
])
def test_kvm_state_from_the_shared_kvm_read(world, entry, state, count, error):
    with wapp.app.test_request_context("/api/kvm/hosts"):
        k = wapp._read_share_key("api_kvm_hosts", {}, scope=wapp.KVM_SCOPE)
    body = json.dumps({"hosts": [entry]}).encode()
    wapp.READ_SHARE.get(k, 3, lambda: (200, body, "application/json", [], None, []))
    with wapp.app.test_client() as c:
        d = hosts(c)
    h = by_key(d)[("kvm", "node2")]
    assert (h["state"], h["vm_count"], h["error"]) == (state, count, error)
    assert by_key(d)[("kvm", "kvm-paris.01")]["state"] == "unknown"


def test_the_kvm_screen_read_is_found_through_the_real_route(world, monkeypatch):
    """Les clés du menu et des écrans viennent de la même fonction : une vraie
    lecture de /api/kvm/hosts est retrouvée (mutation : une clé recopiée à la
    main, sans le rôle ou la portée, ne la retrouverait pas)."""
    def inventory(host, details=False):
        if host.name == "kvm-paris.01":
            raise kh.KvmError("unreachable", "kvm-paris.01: unreachable: no route to host")
        return {"name": host.name, "vms": [{"name": "kvmtest-1"}], "libvirt_version": "12.7.0", "errors": []}
    monkeypatch.setattr(kh, "host_inventory", inventory)
    with wapp.app.test_client() as c:
        assert c.get("/api/kvm/hosts").status_code == 200
        monkeypatch.setattr(kh, "host_inventory", _trap("host_inventory"))
        d = by_key(hosts(c))
    assert (d[("kvm", "node2")]["state"], d[("kvm", "node2")]["vm_count"]) == ("ok", 1)
    assert d[("kvm", "node2")]["version"] == "12.7.0"
    assert d[("kvm", "kvm-paris.01")]["state"] == "unreachable"


def test_a_hosts_list_never_reads_a_host_even_after_screens(world, monkeypatch):
    _fleet(monkeypatch)
    with wapp.app.test_client() as c:
        c.get("/api/vms-all")
        for _ in range(3):
            world["clock"].t += 4
            hosts(c)
    # les leurres n'ont pas été appelés (sinon AssertionError dans la réponse)


def test_favorite_flag_is_per_person(world, monkeypatch, tmp_path):
    conn = wapp._fdb.connect(wapp._folders_db_path())
    wapp._fdb.add_favorite(conn, "anonymous", "harvester", "harv1")
    wapp._fdb.add_favorite(conn, "someone-else", "kvm", "node2")
    conn.close()
    with wapp.app.test_client() as c:
        d = by_key(hosts(c))
    assert d[("harvester", "harv1")]["favorite"] is True
    assert d[("kvm", "node2")]["favorite"] is False


def test_peek_never_loads_and_respects_its_age():
    clock = Clock()
    share = rsh.ReadShare(clock=clock)
    assert share.peek(("c", "e"), 60) is None
    share.get(("c", "e"), 3, lambda: "v")
    clock.t += 30
    assert share.peek(("c", "e"), 60) == "v"          # trop vieux pour get(), pas pour peek
    clock.t += 31
    assert share.peek(("c", "e"), 60) is None
    share.get(("c", "e"), 3, lambda: "w")
    share.invalidate("c")
    assert share.peek(("c", "e"), 60) is None
    assert share.stats == {"hit": 0, "shared": 0, "miss": 2}
