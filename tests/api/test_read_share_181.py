"""v1.81.0 : lectures partagées entre personnes connectées, YAML mémorisé,
surveillances étalées."""
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "web"))
import read_share as rsh  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_value_kept_for_ttl_then_reloaded():
    clk = Clock()
    rs = rsh.ReadShare(clock=clk)
    calls = []
    load = lambda: calls.append(1) or len(calls)  # noqa: E731
    assert rs.get(("c", "v"), 3, load) == 1
    clk.t += 2.9
    assert rs.get(("c", "v"), 3, load) == 1
    clk.t += 0.2
    assert rs.get(("c", "v"), 3, load) == 2
    assert rs.stats["hit"] == 1 and rs.stats["miss"] == 2


def test_concurrent_requests_share_one_read():
    rs = rsh.ReadShare()
    started, release = threading.Event(), threading.Event()
    calls = []

    def load():
        calls.append(1)
        started.set()
        release.wait(5)
        return "data"

    results = []
    lead = threading.Thread(target=lambda: results.append(rs.get(("c", "v"), 3, load)))
    lead.start()
    assert started.wait(5)
    followers = [threading.Thread(target=lambda: results.append(rs.get(("c", "v"), 3, load)))
                 for _ in range(10)]
    [t.start() for t in followers]
    deadline = time.time() + 5
    while rs.stats["shared"] < 10 and time.time() < deadline:
        time.sleep(0.01)
    release.set()
    [t.join(5) for t in [lead, *followers]]
    assert calls == [1]
    assert results == ["data"] * 11
    assert rs.stats["shared"] == 10


def test_error_is_shared_with_waiters_but_not_kept():
    rs = rsh.ReadShare()

    def boom():
        raise RuntimeError("down")
    with pytest.raises(RuntimeError):
        rs.get(("c", "v"), 3, boom)
    assert rs.get(("c", "v"), 3, lambda: "ok") == "ok"


def test_keep_false_is_not_kept():
    rs = rsh.ReadShare()
    n = []
    load = lambda: n.append(1) or ("err", len(n))  # noqa: E731
    rs.get(("c", "v"), 3, load, keep=lambda v: v[0] == 200)
    rs.get(("c", "v"), 3, load, keep=lambda v: v[0] == 200)
    assert len(n) == 2


def test_invalidate_cluster_only_drops_that_cluster():
    rs = rsh.ReadShare()
    rs.get(("a", "v"), 60, lambda: 1)
    rs.get(("b", "v"), 60, lambda: 1)
    rs.invalidate("a")
    assert rs.get(("a", "v"), 60, lambda: 2) == 2
    assert rs.get(("b", "v"), 60, lambda: 2) == 1
    rs.invalidate()
    assert rs.get(("b", "v"), 60, lambda: 3) == 3


def test_write_during_read_is_not_kept_and_new_requests_do_not_join():
    """Une écriture pendant une lecture : la lecture partie avant ne sert pas
    aux demandes qui arrivent après."""
    rs = rsh.ReadShare()
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(5)
        return "before"

    out = []
    t = threading.Thread(target=lambda: out.append(rs.get(("c", "v"), 60, slow)))
    t.start()
    assert started.wait(5)
    rs.invalidate("c")
    # arrive après l'écriture : nouvelle lecture, pas celle en vol
    assert rs.get(("c", "v"), 60, lambda: "after") == "after"
    release.set()
    t.join(5)
    assert out == ["before"]
    assert rs.get(("c", "v"), 60, lambda: "x") == "after"


def test_invalidate_all_during_read_is_not_kept():
    rs = rsh.ReadShare()
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(5)
        return "before"
    t = threading.Thread(target=lambda: rs.get(("never-seen", "v"), 60, slow))
    t.start()
    assert started.wait(5)
    rs.invalidate()
    release.set()
    t.join(5)
    assert rs.get(("never-seen", "v"), 60, lambda: "after") == "after"


def test_prune_bounds_memory():
    clk = Clock()
    rs = rsh.ReadShare(clock=clk, max_entries=10)
    for i in range(25):
        clk.t += 1
        rs.get(("c", i), 1000, lambda: i)
    assert len(rs._done) <= 10


def test_yaml_file_memoised_until_changed(tmp_path, monkeypatch):
    p = tmp_path / "c.yaml"
    p.write_text("a: 1\n")
    parses = []
    real = rsh._yaml.load
    monkeypatch.setattr(rsh._yaml, "load", lambda *a, **k: parses.append(1) or real(*a, **k))
    d = rsh.yaml_file(p)
    d["a"] = 99                        # l'appelant modifie sa copie
    assert rsh.yaml_file(p) == {"a": 1}
    assert len(parses) == 1
    tmp = tmp_path / "c.tmp"
    tmp.write_text("a: 2\nb: 3\n")
    os.replace(tmp, p)                 # remplacement atomique : nouvel inode
    assert rsh.yaml_file(p) == {"a": 2, "b": 3}
    assert len(parses) == 2


def test_yaml_file_raises_like_direct_read(tmp_path):
    with pytest.raises(OSError):
        rsh.yaml_file(tmp_path / "absent.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: [1\n")
    with pytest.raises(rsh._yaml.YAMLError):
        rsh.yaml_file(bad)


# ---------------------------------------------------------------------------
# Le décorateur dans la console
# ---------------------------------------------------------------------------
@pytest.fixture
def shared(monkeypatch):
    import app as wapp
    monkeypatch.setattr(wapp, "READ_SHARE_ENABLED", True)
    monkeypatch.setattr(wapp, "READ_SHARE", rsh.ReadShare())
    monkeypatch.setattr(wapp, "AUTH_OPEN_ALLOWED", True)
    monkeypatch.setattr(wapp, "auth_configured", lambda: False)
    return wapp


def _fake_vms(wapp, monkeypatch, calls):
    import subprocess as sp
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/kc/" + c)
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc: True)

    class R:
        returncode = 0
        stderr = ""
        stdout = '{"items": []}'

    def run(argv, **k):
        if "vmi" not in argv:          # une lecture = VMs + VMIs ; on compte les VMs
            calls.append(argv)
        return R()
    monkeypatch.setattr(wapp, "_kubectl_run", run)
    monkeypatch.setattr(sp, "check_output", lambda *a, **k: b'{"items": []}')


def test_two_people_share_one_cluster_read(shared, monkeypatch):
    calls = []
    _fake_vms(shared, monkeypatch, calls)
    with shared.app.test_client() as c:
        assert c.get("/api/vms/harv1").status_code == 200
        assert c.get("/api/vms/harv1").status_code == 200
        assert len(calls) == 1
        # ?fresh=1 relit
        c.get("/api/vms/harv1?fresh=1")
        assert len(calls) == 2


def test_a_write_on_the_cluster_forgets_its_reads(shared, monkeypatch):
    calls = []
    _fake_vms(shared, monkeypatch, calls)
    with shared.app.test_client() as c:
        c.get("/api/vms/harv1")
        shared.READ_SHARE.invalidate("other")
        c.get("/api/vms/harv1")
        assert len(calls) == 1
        # toute requête d'écriture sur ce cluster, même refusée
        c.post("/api/vms/harv1/create", json={})
        c.get("/api/vms/harv1")
        assert len(calls) == 2


def test_identities_do_not_share(shared, monkeypatch):
    """Le kubeconfig rendu porte l'identité présentée au cluster."""
    calls = []
    _fake_vms(shared, monkeypatch, calls)
    who = {"kc": "/kc/alice"}
    monkeypatch.setattr(shared, "_kubectl_for_cluster", lambda c: who["kc"])
    with shared.app.test_client() as c:
        c.get("/api/vms/harv1")
        who["kc"] = "/kc/bob"
        c.get("/api/vms/harv1")
        assert len(calls) == 2


def test_roles_do_not_share(shared, monkeypatch):
    calls = []
    _fake_vms(shared, monkeypatch, calls)
    role = {"r": "admin"}
    monkeypatch.setattr(shared, "current_role", lambda: role["r"])
    with shared.app.test_client() as c:
        c.get("/api/vms/harv1")
        role["r"] = "viewer"
        c.get("/api/vms/harv1")
        assert len(calls) == 2


def test_cluster_denials_replayed_for_the_followers(shared, monkeypatch):
    """Un refus RBAC vu par la lecture partagée est dit à chacun (en-tête)."""
    _fake_vms(shared, monkeypatch, [])

    class R:
        returncode = 0
        stdout = '{"items": []}'
        stderr = ""
    monkeypatch.setattr(shared, "_kubectl_run", lambda argv, **k: (
        shared._note_cluster_denial('Error from server (Forbidden): virtualmachineinstances.kubevirt.io '
                                    'is forbidden: User "eve" cannot list resource "virtualmachineinstances" '
                                    'in API group "kubevirt.io" at the cluster scope'), R())[1])
    with shared.app.test_client() as c:
        a = c.get("/api/vms/harv1")
        b = c.get("/api/vms/harv1")
    assert a.headers.get("X-Cluster-Denied")
    assert b.headers.get("X-Cluster-Denied") == a.headers.get("X-Cluster-Denied")


def test_shared_read_sits_under_requires_auth():
    """Une réponse partagée ne doit jamais sortir avant le contrôle d'accès :
    chaque @shared_read est placé juste SOUS @requires_auth."""
    src = (Path(__file__).resolve().parents[2] / "web" / "app.py").read_text().splitlines()
    n = 0
    for i, line in enumerate(src):
        if line.startswith("@shared_read("):
            n += 1
            assert src[i - 1].strip() == "@requires_auth", src[i - 1]
            j = i - 1
            while src[j].startswith("@") and not src[j].startswith("@app.route("):
                j -= 1
            assert "methods=" not in src[j] or '"GET"' in src[j], src[j]
    assert n >= 20


def test_watchers_start_spread_over_the_interval():
    import app as wapp
    off = wapp._watch_start_offsets(["a", "b", "c", "d"], 15)
    assert sorted(off.values()) == [0, 3.75, 7.5, 11.25]
    assert wapp._watch_delay(15, rnd=0) == pytest.approx(13.5)
    assert wapp._watch_delay(15, rnd=1) == pytest.approx(16.5)
