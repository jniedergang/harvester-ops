"""Essai actif d'un lien entre deux clusters (`replication_link`) et sa
ligne de commande (`link test`, `link list`).

Faux clusters : la cible de sauvegarde NFS commune est un dictionnaire de
fichiers ; une pod d'écriture y pose le fichier, une pod de lecture le relit
et l'efface, une pod de nettoyage l'efface. Refus (cible différente, pas
NFS, image absente), durées lues dans le journal des pods, pods et fichier
retirés en échec, en annulation et à l'arrêt de la console."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import replication_db as db  # noqa: E402
import replication_link as rl  # noqa: E402
import replication_run as rr  # noqa: E402

_spec = importlib.util.spec_from_file_location("harvester_replication_l", ROOT / "bin" / "harvester-replication.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

NFS = json.dumps({"type": "nfs", "endpoint": "nfs://172.16.0.5:/volume1/BACKUP/harvester-lab/"})
IMAGE = "registry.suse.com/longhorn/longhorn-manager:v1.11.2"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeKube:
    """Un cluster : réglage `backup-target`, DaemonSet longhorn-manager, pods
    dont le sort est décidé par `outcome[kind]` (`ok`, `fail`, `hang`,
    `nolog`), cible NFS partagée `share`."""

    def __init__(self, name, share, clock, bt=NFS, image=IMAGE):
        self.name, self.share, self.clock = name, share, clock
        self.objs = {}
        self.calls = []
        self.outcome = {}
        self.refuse_create = set()
        if bt is not None:
            self.objs[(rl.K_SETTING, None, "backup-target")] = {"metadata": {"name": "backup-target"}, "value": bt}
        if image is not None:
            self.objs[(rl.K_DS, rl.NS, rl.MANAGER_DS)] = {"spec": {"template": {"spec": {"containers": [
                {"name": "sidecar", "image": "other:1"}, {"name": rl.MANAGER_DS, "image": image}]}}}}
        self.pods = {}

    def get(self, kind, ns, name):
        self.calls.append(("get", kind, name))
        if kind == rl.K_POD:
            p = self.pods.get(name)
            if p is None:
                return None
            if p["phase"] == "Pending":
                self._run(p)
            return {"metadata": {"name": name}, "status": {"phase": p["phase"]}}
        return self.objs.get((kind, ns, name))

    def create(self, obj):
        name = obj["metadata"]["name"]
        self.calls.append(("create", obj["kind"], name))
        if name.split("-")[2] in self.refuse_create:
            raise RuntimeError("admission webhook denied the pod")
        self.pods[name] = {"obj": obj, "phase": "Pending", "logs": ""}
        return obj

    def _run(self, p):
        name = p["obj"]["metadata"]["name"]
        kind = name.split("-")[2]                        # hops-link-<w|r|c>-<id>
        test_id = name.split("-")[3]
        script = p["obj"]["spec"]["containers"][0]["command"][2]
        out = self.outcome.get(kind, "ok")
        if out == "hang":
            return
        f = f"{test_id}.bin"
        if out == "fail":
            p["phase"] = "Failed"
            if kind == "w":
                self.share[f] = "partial"               # écriture partielle laissée
            return
        p["phase"] = "Succeeded"
        if kind == "w":
            assert f"count={self.size}" in script
            self.share[f] = "data"
            p["logs"] = "noise\nHOPS_LINK write_start=10.5 write_end=14.5 bytes=%d\n" % (self.size * rl.MIB)
        elif kind == "r":
            assert f in self.share, "the reader reads a file that exists"
            self.share.pop(f, None)
            p["logs"] = "HOPS_LINK read_start=20.0 read_end=22.0 bytes=%d\nHOPS_LINK removed=1\n" % (self.size * rl.MIB)
        else:
            self.share.pop(f, None)
            p["logs"] = "HOPS_LINK removed=1\n"
        if out == "nolog":
            p["logs"] = ""

    def logs(self, ns, name):
        return self.pods[name]["logs"]

    def delete(self, kind, ns, name, cascade=None):
        self.calls.append(("delete", kind, name))
        self.pods.pop(name, None)


@pytest.fixture
def bench():
    share, clock = {}, Clock()
    src, dst = FakeKube("harvlab", share, clock), FakeKube("harvlab2", share, clock)
    src.size = dst.size = 64
    return src, dst, share, clock


def run(bench, size=64, **kw):
    src, dst, _share, clock = bench
    return rl.run(src, dst, size, "abc123def456", sleep=clock.sleep, now=clock.now, **kw)


# ---------------------------------------------------------------------------
# Petites briques
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("a,b,expect", [
    (NFS, NFS, ("172.16.0.5", "/volume1/BACKUP/harvester-lab", "nfs://172.16.0.5:/volume1/BACKUP/harvester-lab")),
    (NFS, json.dumps({"type": "nfs", "endpoint": "nfs://172.16.0.5:/volume1/BACKUP/harvester-lab"}),
     ("172.16.0.5", "/volume1/BACKUP/harvester-lab", "nfs://172.16.0.5:/volume1/BACKUP/harvester-lab")),
    (NFS, json.dumps({"type": "nfs", "endpoint": "nfs://172.16.0.6:/volume1/BACKUP/harvester-lab"}),
     "backup-target-differs"),
    (NFS, "", "backup-target-differs"),
    ("", "", "backup-target-differs"),
    (json.dumps({"type": "s3", "endpoint": "https://s3", "bucketName": "b"}),
     json.dumps({"type": "s3", "endpoint": "https://s3", "bucketName": "b"}), "not-supported"),
])
def test_nfs_target(a, b, expect):
    if isinstance(expect, str):
        with pytest.raises(rl.LinkError) as e:
            rl.nfs_target(a, b)
        assert e.value.code == expect
    else:
        assert rl.nfs_target(a, b) == expect


def test_backup_target_falls_back_to_the_longhorn_backup_target(bench):
    src = bench[0]
    src.objs.pop((rl.K_SETTING, None, "backup-target"))
    assert rl.backup_target_raw(src) == ""
    src.objs[(rl.K_LH_TARGET, rl.NS, "default")] = {"spec": {"backupTargetURL": "nfs://172.16.0.5:/volume1/BACKUP/"
                                                                              "harvester-lab/"}}
    assert rl.nfs_target(rl.backup_target_raw(src), NFS)[0] == "172.16.0.5"


def test_manager_image(bench):
    assert rl.manager_image(bench[0]) == IMAGE
    bench[0].objs.pop((rl.K_DS, rl.NS, rl.MANAGER_DS))
    with pytest.raises(rl.LinkError) as e:
        rl.manager_image(bench[0])
    assert e.value.code == "image-unknown"


@pytest.mark.parametrize("text,expect", [
    ("", {}),
    ("HOPS_LINK write_start=1.5 write_end=3 bytes=10", {"write_start": 1.5, "write_end": 3.0, "bytes": 10.0}),
    ("dd: noise\n  HOPS_LINK removed=1  \nHOPS_LINKX a=1\nHOPS_LINK bad=x nokey a=2", {"removed": 1.0, "a": 2.0}),
])
def test_parse_logs(text, expect):
    assert rl.parse_logs(text) == expect


def test_pod_manifest_mounts_the_share_and_uses_the_cluster_image():
    p = rl.pod("hops-link-w-x", "x", IMAGE, "nas", "/b", "echo", scratch_mib=64, deadline_s=184)
    c = p["spec"]["containers"][0]
    assert c["image"] == IMAGE and c["imagePullPolicy"] == "IfNotPresent" and c["command"][:2] == ["/bin/sh", "-c"]
    assert p["metadata"]["namespace"] == rl.NS and p["metadata"]["labels"][rl.LINK_LABEL] == "x"
    assert {"name": "bt", "nfs": {"server": "nas", "path": "/b"}} in p["spec"]["volumes"]
    assert {"name": "scratch", "emptyDir": {"sizeLimit": "128Mi"}} in p["spec"]["volumes"]
    assert p["spec"]["restartPolicy"] == "Never" and p["spec"]["activeDeadlineSeconds"] == 184
    assert "scratch" not in json.dumps(rl.pod("n", "x", IMAGE, "nas", "/b", "echo")["spec"]["volumes"])


def test_scripts_time_only_the_transfer_and_remove_the_file():
    w = rl.write_script("t1", 128)
    assert "count=128" in w and w.index("s=$(date") > w.index("/dev/urandom") and "conv=fsync" in w
    assert "rm -f /mnt/bt/harvester-ops-linktest/t1.bin" in w
    r = rl.read_script("t1")
    assert "trap 'rm -f /mnt/bt/harvester-ops-linktest/t1.bin; rmdir /mnt/bt/harvester-ops-linktest" in r
    assert "removed=1" in r


# ---------------------------------------------------------------------------
# Essai complet
# ---------------------------------------------------------------------------

def test_success_measures_both_ways_and_leaves_nothing(bench):
    src, dst, share, _ = bench
    out = run(bench)
    assert out["write_s"] == 4.0 and out["read_s"] == 2.0 and out["size_bytes"] == 64 * rl.MIB
    assert out["write_bps"] == 64 * rl.MIB / 4 and out["read_bps"] == 64 * rl.MIB / 2
    assert out["cleanup"] == "ok" and out["backup_target"].startswith("nfs://172.16.0.5:")
    assert share == {} and src.pods == {} and dst.pods == {}
    # écriture par la source, lecture par la cible, aucune pod de nettoyage
    assert [c[2] for c in src.calls if c[0] == "create"] == ["hops-link-w-abc123def456"]
    assert [c[2] for c in dst.calls if c[0] == "create"] == ["hops-link-r-abc123def456"]


@pytest.mark.parametrize("who,outcome,code,cleanup_on", [
    ("src", {"w": "fail"}, "write-failed", "src"),
    ("src", {"w": "nolog"}, "write-failed", "src"),
    ("dst", {"r": "fail"}, "read-failed", "src"),
    ("dst", {"r": "nolog"}, "read-failed", "src"),
    ("src", {"w": "hang"}, "timeout", "src"),
    ("dst", {"r": "hang"}, "timeout", "src"),
])
def test_failures_always_remove_pods_and_file(bench, who, outcome, code, cleanup_on):
    src, dst, share, _ = bench
    (src if who == "src" else dst).outcome.update(outcome)
    with pytest.raises(rl.LinkError) as e:
        run(bench)
    assert e.value.code == code and e.value.facts["cleanup"] == "ok"
    assert share == {} and src.pods == {} and dst.pods == {}
    assert any(c[0] == "create" and c[2].startswith("hops-link-c-") for c in
               (src if cleanup_on == "src" else dst).calls)


def test_cleanup_falls_back_to_the_target_then_says_what_is_left(bench):
    src, dst, share, _ = bench
    src.outcome["r"] = "x"
    dst.outcome["r"] = "fail"
    src.refuse_create.add("c")
    with pytest.raises(rl.LinkError) as e:
        run(bench)
    assert e.value.facts["cleanup"] == "ok" and share == {}
    assert any(c[0] == "create" and c[2].startswith("hops-link-c-") for c in dst.calls)
    # nettoyage impossible des deux côtés : dit, avec le fichier à retirer
    share.clear()
    dst.refuse_create.add("c")
    with pytest.raises(rl.LinkError) as e:
        run(bench)
    assert e.value.facts["cleanup"].startswith("failed:") and "abc123def456.bin" in e.value.facts["cleanup"]


def test_cancel_during_a_wait_still_cleans_up(bench):
    src, dst, share, clock = bench
    dst.outcome["r"] = "hang"
    calls = {"n": 0}

    def sleep(s):
        calls["n"] += 1
        if calls["n"] == 3:
            raise rr.Cancelled("SIGTERM")           # comme le gestionnaire de signal de la ligne de commande
        clock.sleep(s)
    with pytest.raises(rr.Cancelled):
        rl.run(src, dst, 64, "abc123def456", sleep=sleep, now=clock.now)
    assert share == {} and src.pods == {} and dst.pods == {}


def test_console_shutdown_stops_and_cleans_up(bench):
    src, dst, share, _ = bench
    dst.outcome["r"] = "hang"
    flag = {"on": False}

    def stop():
        return flag["on"]
    src.outcome["w"] = "ok"
    orig = dst.create

    def create(obj):
        flag["on"] = True                            # l'arrêt arrive pendant la lecture
        return orig(obj)
    dst.create = create
    with pytest.raises(rl.LinkError) as e:
        run(bench, stop=stop)
    assert e.value.code == "stopped" and share == {} and dst.pods == {}


@pytest.mark.parametrize("change,code", [
    (lambda s, d: d.objs.update({(rl.K_SETTING, None, "backup-target"): {"value": json.dumps(
        {"type": "nfs", "endpoint": "nfs://other:/x"})}}), "backup-target-differs"),
    (lambda s, d: d.objs.pop((rl.K_DS, rl.NS, rl.MANAGER_DS)), "image-unknown"),
])
def test_refusals_create_no_pod(bench, change, code):
    src, dst, _share, _ = bench
    change(src, dst)
    with pytest.raises(rl.LinkError) as e:
        run(bench)
    assert e.value.code == code
    assert not [c for c in src.calls + dst.calls if c[0] == "create"]


@pytest.mark.parametrize("size", [63, 4097, True, "64"])
def test_size_is_bounded(bench, size):
    with pytest.raises(rl.LinkError) as e:
        run(bench, size=size)
    assert e.value.code == "bad-size"


def test_pod_timeout_grows_with_the_size():
    assert rl.pod_timeout(64) == 184 and rl.pod_timeout(4096) == 4216


# ---------------------------------------------------------------------------
# Ligne de commande
# ---------------------------------------------------------------------------

@pytest.fixture
def wired(bench, monkeypatch, tmp_path):
    src, dst, share, clock = bench
    seen = []

    def factory(name, kc):
        seen.append((name, kc))
        return {"harvlab": src, "harvlab2": dst}.get(name)
    monkeypatch.setattr(cli, "KUBE_FACTORY", factory)
    monkeypatch.setattr(cli, "NOW", clock.now)
    monkeypatch.setattr(cli, "SLEEP", clock.sleep)
    return {"db": tmp_path / "l.db", "seen": seen, "src": src, "dst": dst, "share": share}


def cli_run(capsys, *argv):
    rc = cli.main(list(argv) + ["--json"])
    out, err = capsys.readouterr()
    return rc, json.loads(out), err


def test_cli_link_test_records_the_result_and_link_list_shows_it(capsys, wired):
    rc, data, err = cli_run(capsys, "link", "test", "--from=harvlab", "--to=harvlab2", "--size-mib=64",
                            f"--db={wired['db']}", "--from-kubeconfig=/srv/kc/a.yaml",
                            "--to-kubeconfig=/srv/kc/b.yaml", "--by=alice", "--id=act9")
    assert rc == 0 and data["ok"] and data["write_s"] == 4.0 and data["read_s"] == 2.0, data
    assert wired["seen"] == [("harvlab", "/srv/kc/a.yaml"), ("harvlab2", "/srv/kc/b.yaml")]
    assert "STEP_EVENT|link|done|" in err and "/srv/kc" not in err
    rc, data, _ = cli_run(capsys, "link", "list", f"--db={wired['db']}")
    [link] = data["links"]
    assert (link["source"], link["target"], link["plans"]) == ("harvlab", "harvlab2", [])
    assert link["active"]["by"] == "alice" and link["active"]["action_id"] == "act9"
    assert link["active"]["write_bps"] == 64 * rl.MIB / 4 and link["active"]["error"] == ""
    assert link["backup_target"] == "nfs://172.16.0.5:/volume1/BACKUP/harvester-lab"
    c = db.connect(str(wired["db"]))
    [ev] = [e for e in db.events_after(c, 0) if e["step"] == "link-test"]
    assert ev["code"] == "link-tested" and ev["action_id"] == "act9"
    c.close()


@pytest.mark.parametrize("argv,rc_,kind", [
    (["--from=harvlab", "--to=harvlab"], 2, "same-cluster"),
    (["--from=harvlab", "--to=nowhere"], 2, "unknown-cluster"),
    (["--from=harvlab", "--to=harvlab2", "--size-mib=32"], 2, "bad-size"),
    (["--from=a;b", "--to=harvlab2"], 2, "usage"),
    (["--to=harvlab2"], 2, "usage"),
])
def test_cli_link_test_usage_errors(capsys, wired, argv, rc_, kind):
    rc, data, _ = cli_run(capsys, "link", "test", *argv, f"--db={wired['db']}")
    assert rc == rc_ and data["kind"] == kind
    assert not [c for c in wired["src"].calls if c[0] == "create"]


@pytest.mark.parametrize("setup,rc_,kind", [
    (lambda w: w["dst"].objs.update({(rl.K_SETTING, None, "backup-target"): {"value": ""}}), 2,
     "backup-target-differs"),
    (lambda w: w["dst"].outcome.update(r="fail"), 1, "read-failed"),
])
def test_cli_link_test_failures_are_recorded(capsys, wired, setup, rc_, kind):
    setup(wired)
    rc, data, _ = cli_run(capsys, "link", "test", "--from=harvlab", "--to=harvlab2", "--size-mib=64",
                          f"--db={wired['db']}")
    assert rc == rc_ and data["kind"] == kind and data["ok"] is False
    c = db.connect(str(wired["db"]))
    [t] = db.last_link_tests(c).values()
    assert t["error"].startswith(kind) and wired["share"] == {}
    c.close()


def test_cli_link_test_spec_file(capsys, wired, tmp_path):
    spec = tmp_path / "s.json"
    spec.write_text(json.dumps({"from": "harvlab", "to": "harvlab2", "size_mib": 64}))
    rc, data, _ = cli_run(capsys, "link", "test", f"--spec={spec}", f"--db={wired['db']}")
    assert rc == 0, data
