"""v1.78.0 : pools de disques de données après une installation bare-metal.

L'installeur de Harvester ne connaît qu'un disque de données ; les pools sont
créés après coup avec ce que Harvester offre déjà : BlockDevices de
node-disk-manager provisionnés dans Longhorn avec une étiquette, et une classe
`longhorn-<tag>` par pool. Faits vus sur le banc (NDM 1.8) : un BlockDevice
porte un UUID, se retrouve par série ou WWN ; `engineVersion` n'est pas mis
par défaut ; NDM recopie spec.tags dans les tags du disque Longhorn.

Séries et WWN génériques : aucune donnée de matériel réel.
"""

import copy
import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(ROOT / "web"))
import hv_host as hh  # noqa: E402

_spec = importlib.util.spec_from_file_location("hres_pools", ROOT / "bin" / "harvester-resources.py")
hres = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hres)

NS = "longhorn-system"
SC = "storageclasses.storage.k8s.io"


def a_bd(name, serial="", wwn="", node="n1", dtype="disk", provision=False, tags=None):
    return {"apiVersion": "harvesterhci.io/v1beta1", "kind": "BlockDevice",
            "metadata": {"name": name, "namespace": NS, "resourceVersion": "3"},
            "spec": {"nodeName": node, "devPath": f"/dev/{name[:3]}", "provision": provision,
                     "fileSystem": {}, **({"tags": list(tags)} if tags is not None else {})},
            "status": {"state": "Active", "provisionPhase": "Provisioned" if provision else "Unprovisioned",
                       "deviceStatus": {"devPath": f"/dev/{name[:3]}",
                                        "details": {"deviceType": dtype, "serialNumber": serial, "wwn": wwn},
                                        "fileSystem": {"mountPoint": "", "type": ""}},
                       "conditions": []}}


class FakeKube:
    """Cluster simulé : NDM provisionne un BlockDevice remplacé avec
    `provision: true` et recopie ses tags sur le disque Longhorn."""

    def __init__(self, bds=(), classes=(), appear_after=0, copy_tags=True):
        self.objs = {}
        for bd in bds:
            self.objs[(hh.K_BD, NS, bd["metadata"]["name"])] = bd
        for sc in classes:
            self.objs[(SC, None, sc["metadata"]["name"])] = sc
        self.objs[(hh.K_LHNODE, NS, "n1")] = {"metadata": {"name": "n1", "namespace": NS},
                                              "spec": {"tags": [], "disks": {"default-disk-1": {"tags": []}}}}
        self.appear_after = appear_after
        self.copy_tags = copy_tags
        self.lists = 0
        self.replaced, self.created, self.patched = [], [], []

    def list(self, kind, ns=None, selector=None):
        if kind == hh.K_BD:
            self.lists += 1
            if self.lists <= self.appear_after:
                return []                       # NDM pas encore là
        return [copy.deepcopy(o) for (k, n, _), o in self.objs.items() if k == kind and (ns is None or n == ns)]

    def get(self, kind, ns, name):
        o = self.objs.get((kind, ns, name))
        return copy.deepcopy(o) if o is not None else None

    def replace(self, obj):
        name = obj["metadata"]["name"]
        self.replaced.append(copy.deepcopy(obj))
        obj = copy.deepcopy(obj)
        if obj["spec"].get("provision"):
            obj["status"]["provisionPhase"] = "Provisioned"
            lh = self.objs[(hh.K_LHNODE, NS, "n1")]
            lh["spec"]["disks"][name] = {"tags": list(obj["spec"].get("tags") or []) if self.copy_tags else []}
        self.objs[(hh.K_BD, NS, name)] = obj
        return obj

    def patch(self, kind, ns, name, patch):
        self.patched.append((kind, name, patch))
        o = self.objs[(kind, ns, name)]
        for d, v in ((patch.get("spec") or {}).get("disks") or {}).items():
            o["spec"]["disks"].setdefault(d, {}).update(v)

    def create(self, obj):
        self.created.append(obj)
        self.objs[(SC, None, obj["metadata"]["name"])] = obj
        return obj


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture()
def steps(monkeypatch):
    seen = []
    monkeypatch.setattr(hres, "step", lambda sid, status, msg="": seen.append((sid, status, msg)))
    return seen


def run(kube, pools, timeout=600, classes=True):
    c = Clock()
    return hres.pools_apply(kube, "n1", hh.check_pools(pools), timeout, classes=classes,
                            sleep=c.sleep, now=c.now)


POOLS = [{"tag": "ssd", "replicas": 1, "disks": [{"serial": "SER-A1", "path": "/dev/disk/by-path/pci-a"}]},
         {"tag": "hdd", "replicas": 2, "disks": [{"wwn": "0x5000000000000b01"},
                                                  {"serial": "SER-C3"}]}]


def bench():
    return [a_bd("uuid-a", serial="SER-A1"), a_bd("uuid-b", wwn="0x5000000000000b01"),
            a_bd("uuid-c", serial="ser-c3"), a_bd("uuid-c-p1", serial="SER-C3", dtype="part"),
            a_bd("uuid-x", serial="SER-A1", node="n2")]


# -- description des pools ----------------------------------------------------------

def test_check_pools_normalizes_and_refuses_each_defect():
    out = hh.check_pools([{"tag": "ssd", "disks": [{"serial": " S1 "}]}])
    assert out == [{"tag": "ssd", "replicas": 1, "disks": [{"serial": "S1", "wwn": "", "path": ""}]}]
    assert hh.check_pools(None) == [] and hh.check_pools([]) == []
    assert hh.check_pools([{"tag": "a", "replicas": "3", "disks": [{"wwn": "w"}]}])[0]["replicas"] == 3
    bad = [
        ([{"tag": "SSD", "disks": [{"serial": "s"}]}], "tag"),
        ([{"tag": "-x", "disks": [{"serial": "s"}]}], "tag"),
        ([{"tag": "a" * 33, "disks": [{"serial": "s"}]}], "tag"),
        ([{"tag": "a", "replicas": 0, "disks": [{"serial": "s"}]}], "replicas"),
        ([{"tag": "a", "replicas": 4, "disks": [{"serial": "s"}]}], "replicas"),
        ([{"tag": "a", "replicas": 1.5, "disks": [{"serial": "s"}]}], "replicas"),
        ([{"tag": "a", "replicas": True, "disks": [{"serial": "s"}]}], "replicas"),
        ([{"tag": "a", "disks": []}], "at least one disk"),
        ([{"tag": "a", "disks": [{"path": "/dev/sdb"}]}], "serial number or a WWN"),
        ([{"tag": "a", "disks": [{"serial": "s"}]}, {"tag": "a", "disks": [{"serial": "t"}]}], "twice"),
        ([{"tag": "a", "disks": [{"serial": "s"}]}, {"tag": "b", "disks": [{"serial": "S"}]}], "already in the pool a"),
        ([{"tag": "a", "disks": [{"wwn": "0x50"}]}, {"tag": "b", "disks": [{"wwn": "50"}]}], "already in the pool a"),
        ({"tag": "a"}, "a list"),
    ]
    for pools, why in bad:
        with pytest.raises(ValueError, match=why):
            hh.check_pools(pools)


# -- correspondance des BlockDevices ------------------------------------------------

def test_match_by_serial_or_wwn_never_by_kernel_name():
    bds = bench()
    assert hh.match_block_device(bds, "n1", {"serial": "SER-A1"})["metadata"]["name"] == "uuid-a"
    # WWN avec ou sans « 0x », casse ignorée
    assert hh.match_block_device(bds, "n1", {"wwn": "5000000000000B01"})["metadata"]["name"] == "uuid-b"
    # le disque entier l'emporte sur sa partition de même série
    assert hh.match_block_device(bds, "n1", {"serial": "SER-C3"})["metadata"]["name"] == "uuid-c"
    # le nom noyau seul ne suffit jamais
    assert hh.match_block_device(bds, "n1", {"serial": "", "wwn": "", "path": "/dev/uui"}) is None
    # un autre nœud n'est pas regardé
    assert hh.match_block_device(bds, "n2", {"serial": "SER-A1"})["metadata"]["name"] == "uuid-x"
    assert hh.match_block_device(bds, "n1", {"serial": "NOPE"}) is None


def test_two_whole_disks_with_the_same_identity_are_refused():
    bds = [a_bd("u1", serial="DUP"), a_bd("u2", serial="DUP")]
    with pytest.raises(ValueError, match="several block devices"):
        hh.match_block_device(bds, "n1", {"serial": "DUP"})


def test_disk_add_carries_tags_and_the_longhorn_v1_engine():
    out = hh.disk_add(a_bd("u1", serial="S"), force_format=True, tags=["ssd"])
    assert out["spec"]["tags"] == ["ssd"]
    assert out["spec"]["provisioner"] == {"longhorn": {"engineVersion": "LonghornV1"}}
    assert out["spec"]["fileSystem"]["forceFormatted"] is True
    assert "tags" not in hh.disk_add(a_bd("u1", serial="S"))["spec"]


# -- pools-apply ---------------------------------------------------------------------

def test_pools_apply_provisions_tags_and_creates_one_class_per_pool(steps):
    k = FakeKube(bench())
    assert run(k, POOLS) == hres.EXIT_OK
    by = {o["metadata"]["name"]: o["spec"] for o in k.replaced}
    assert set(by) == {"uuid-a", "uuid-b", "uuid-c"}
    assert by["uuid-a"]["tags"] == ["ssd"] and by["uuid-b"]["tags"] == ["hdd"]
    for s in by.values():
        assert s["provision"] is True and s["fileSystem"]["forceFormatted"] is True
        assert s["provisioner"] == {"longhorn": {"engineVersion": "LonghornV1"}}
    classes = {c["metadata"]["name"]: c for c in k.created}
    assert set(classes) == {"longhorn-ssd", "longhorn-hdd"}
    p = classes["longhorn-hdd"]["parameters"]
    assert classes["longhorn-hdd"]["provisioner"] == "driver.longhorn.io"
    assert p["diskSelector"] == "hdd" and p["numberOfReplicas"] == "2"
    assert p["migratable"] == "true" and p["staleReplicaTimeout"] == "30"
    assert classes["longhorn-ssd"]["parameters"]["numberOfReplicas"] == "1"
    lh = k.objs[(hh.K_LHNODE, NS, "n1")]["spec"]["disks"]
    assert lh["uuid-a"]["tags"] == ["ssd"] and lh["uuid-c"]["tags"] == ["hdd"]
    assert ("classes", "done", "longhorn-ssd, longhorn-hdd") in steps


def test_pools_apply_waits_for_node_disk_manager_after_the_install(steps):
    k = FakeKube(bench(), appear_after=3)
    assert run(k, POOLS) == hres.EXIT_OK
    assert k.lists >= 4
    assert any(s[0] == "find" and "waiting for" in s[2] for s in steps)


def test_a_disk_never_found_is_a_clear_refusal_and_nothing_is_touched(steps):
    k = FakeKube([a_bd("uuid-a", serial="SER-A1")])
    assert run(k, POOLS, timeout=60) == hres.EXIT_BLOCKED
    err = [s for s in steps if s[1] == "error"]
    assert err and err[-1][0] == "find"
    assert "WWN 0x5000000000000b01" in err[-1][2] and "serial SER-C3" in err[-1][2]
    assert not k.replaced and not k.created


def test_pools_apply_is_idempotent(steps):
    k = FakeKube(bench())
    assert run(k, POOLS) == hres.EXIT_OK
    k.replaced.clear(), k.created.clear(), k.patched.clear()
    assert run(k, POOLS) == hres.EXIT_OK
    assert not k.replaced and not k.created and not k.patched
    assert any(s[0] == "disk" and "already in the pool ssd" in s[2] for s in steps)


def test_a_disk_already_used_elsewhere_is_left_as_is(steps):
    k = FakeKube([a_bd("uuid-a", serial="SER-A1", provision=True, tags=["fast"])])
    with pytest.raises(ValueError, match="already a storage disk"):
        run(k, POOLS[:1])
    assert not k.replaced


def test_an_existing_class_with_another_selector_is_refused_before_formatting(steps):
    other = {"metadata": {"name": "longhorn-ssd"}, "provisioner": "driver.longhorn.io",
             "parameters": {"diskSelector": "nvme", "numberOfReplicas": "3"}}
    k = FakeKube(bench(), classes=[other])
    with pytest.raises(ValueError, match="another disk selector"):
        run(k, POOLS)
    assert not k.replaced and not k.created
    assert k.objs[(SC, None, "longhorn-ssd")]["parameters"]["diskSelector"] == "nvme"
    # la même classe déjà là : gardée, pas recréée
    same = {"metadata": {"name": "longhorn-ssd"}, "provisioner": "driver.longhorn.io",
            "parameters": {"diskSelector": "ssd"}}
    k = FakeKube(bench(), classes=[same])
    assert run(k, POOLS) == hres.EXIT_OK
    assert [c["metadata"]["name"] for c in k.created] == ["longhorn-hdd"]


def test_join_mode_provisions_only(steps):
    k = FakeKube(bench())
    assert run(k, POOLS, classes=False) == hres.EXIT_OK
    assert len(k.replaced) == 3 and not k.created


def test_longhorn_tag_is_set_when_ndm_did_not_copy_it(steps):
    k = FakeKube(bench(), copy_tags=False)
    assert run(k, POOLS[:1]) == hres.EXIT_OK
    assert k.objs[(hh.K_LHNODE, NS, "n1")]["spec"]["disks"]["uuid-a"]["tags"] == ["ssd"]


def test_command_line_pools_apply_and_disk_add_tag(tmp_path, monkeypatch, steps):
    k = FakeKube(bench())
    monkeypatch.setattr(hres, "kube_from", lambda a: k)
    spec = tmp_path / "pools.json"
    spec.write_text(json.dumps({"pools": POOLS}))
    monkeypatch.setattr(hres, "pools_apply",
                        lambda kube, node, pools, timeout, classes=True: (node, pools, timeout, classes))
    assert hres.main(["pools-apply", "--kubeconfig", "x", "--node", "n1", "--spec", str(spec),
                      "--no-classes", "--timeout", "60"]) == ("n1", hh.check_pools(POOLS), 60, False)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"pools": [{"tag": "Bad", "disks": [{"serial": "s"}]}]}))
    assert hres.main(["pools-apply", "--kubeconfig", "x", "--node", "n1", "--spec", str(bad)]) == hres.EXIT_BLOCKED
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    assert hres.main(["pools-apply", "--kubeconfig", "x", "--node", "n1", "--spec", str(empty)]) == hres.EXIT_BLOCKED
    # host disk-add --tag
    monkeypatch.setattr(hres.time, "sleep", lambda s: None)
    assert hres.main(["host", "disk-add", "--kubeconfig", "x", "--node", "n1", "--disk", "uuid-b",
                      "--format", "--tag", "hdd", "--timeout", "30"]) == hres.EXIT_OK
    assert k.replaced[-1]["spec"]["tags"] == ["hdd"]


# -- la console : route et runner ----------------------------------------------------

import app as wapp  # noqa: E402
import bm_discover as bmd  # noqa: E402

BASE = {"bmc_host": "192.0.2.10", "bmc_user": "u", "bmc_password": "p", "iso": "h.iso",
        "hostname": "n1", "device": "/dev/sda", "mgmt_interface": "eno1", "vip": "192.0.2.50",
        "token": "t"}
INVENTORY = (ROOT / "tests" / "api" / "fixtures" / "bm_disks_178" / "discovery.txt").read_text()


@pytest.fixture()
def client(monkeypatch, tmp_path):
    wapp.app.config["TESTING"] = True
    monkeypatch.setattr(wapp, "current_user", lambda: "alice")
    monkeypatch.setattr(wapp, "current_cluster_identity", lambda: None)
    monkeypatch.setattr(wapp, "INVENTORY_DIR", tmp_path / "inventory")
    monkeypatch.setattr(wapp, "load_config", lambda: {"clusters": []})
    return wapp.app.test_client()


@pytest.fixture()
def runs(monkeypatch):
    seen = []
    monkeypatch.setattr(wapp, "track_action", lambda label, cluster, worker, *a: seen.append(a) or "a1")
    return seen


def test_route_validates_pools_before_the_action_run(client, runs):
    for pools in ([{"tag": "SSD", "disks": [{"serial": "s"}]}],
                  [{"tag": "a", "replicas": 9, "disks": [{"serial": "s"}]}],
                  [{"tag": "a", "disks": []}],
                  [{"tag": "a", "disks": [{"serial": "s"}]}, {"tag": "b", "disks": [{"serial": "s"}]}]):
        r = client.post("/api/baremetal/install", json=dict(BASE, pools=pools))
        assert r.status_code == 400 and r.get_json()["fields"] == ["pools"], pools
    assert not runs
    r = client.post("/api/baremetal/install", json=dict(BASE, pools=POOLS))
    assert r.status_code == 202
    assert runs[0][0]["pools"] == hh.check_pools(POOLS)
    r = client.post("/api/baremetal/install", json=dict(BASE, bmc_host="192.0.2.11"))
    assert r.status_code == 202 and runs[1][0]["pools"] == []


def test_replicas_null_is_one():
    assert hh.check_pools([{"tag": "a", "replicas": None, "disks": [{"serial": "s"}]}])[0]["replicas"] == 1
    assert hh.check_pools([{"tag": "a", "replicas": "", "disks": [{"serial": "s"}]}])[0]["replicas"] == 1


def test_a_pool_disk_is_never_the_system_or_data_disk_without_inventory(client, runs):
    by_path = [{"tag": "a", "disks": [{"serial": "S-1", "path": "/dev/sda"}]}]
    by_wwn = [{"tag": "a", "disks": [{"wwn": "0x5000c500a0000009"}]}]
    by_serial = [{"tag": "a", "disks": [{"serial": "TEST-DATA-0042"}]}]
    for pools, extra in ((by_path, {}),
                         (by_wwn, {"device": "/dev/disk/by-id/wwn-0x5000c500a0000009"}),
                         (by_serial, {"data_disk": "/dev/disk/by-id/ata-MODEL_TEST-DATA-0042"})):
        r = client.post("/api/baremetal/install", json=dict(BASE, pools=pools, **extra))
        assert r.status_code == 400, extra
        assert r.get_json()["fields"] == ["pools"] and r.get_json()["reasons"][0][1] == "role-twice"
    assert not runs


def test_a_pool_disk_is_resolved_in_the_inventory_and_checked_for_roles(client, runs, tmp_path):
    bmd.store_inventory(tmp_path / "inventory", "SYS-0001", BASE["bmc_host"], INVENTORY)
    ssd = "/dev/disk/by-path/pci-0000:00:17.0-ata-2.0"
    # le disque système désigné par son chemin stable, le pool par sa série : même disque
    r = client.post("/api/baremetal/install", json=dict(
        BASE, device=ssd, pools=[{"tag": "a", "disks": [{"serial": "TEST-SSD-0001"}]}]))
    assert r.status_code == 400
    assert any(reason == "role-twice" for _, reason in r.get_json()["reasons"])
    # le disque de données, par WWN
    r = client.post("/api/baremetal/install", json=dict(
        BASE, device="/dev/sdb", data_disk="/dev/sdc", pools=[{"tag": "a", "disks": [{"wwn": "5000C500A0000001"}]}]))
    assert r.status_code == 400
    # un disque absent de l'inventaire
    r = client.post("/api/baremetal/install", json=dict(
        BASE, pools=[{"tag": "a", "disks": [{"serial": "NOT-THERE"}]}]))
    assert r.status_code == 400 and r.get_json()["reasons"] == [["serial NOT-THERE", "unknown-disk"]]
    assert not runs
    # 1.78.0 : le disque système est aussi contrôlé (taille) ; /dev/sdc fait 480 Gio
    r = client.post("/api/baremetal/install", json=dict(
        BASE, device="/dev/sdc", pools=[{"tag": "a", "disks": [{"serial": "TEST-NVME-0001"}]}]))
    assert r.status_code == 202


def test_the_new_cluster_name_is_checked_before_the_action_run(client, runs, monkeypatch):
    r = client.post("/api/baremetal/install", json=dict(BASE, cluster_name="Bad_Name"))
    assert r.status_code == 400 and r.get_json()["fields"] == ["cluster_name"]
    monkeypatch.setattr(wapp, "load_config", lambda: {"clusters": [{"name": "n1"}]})
    r = client.post("/api/baremetal/install", json=BASE)                 # nom d'hôte déjà déclaré
    assert r.status_code == 400 and r.get_json()["fields"] == ["cluster_name"]
    assert not runs
    r = client.post("/api/baremetal/install", json=dict(BASE, cluster_name="lab.home"))
    assert r.status_code == 202 and runs[0][0]["cluster_name"] == "lab.home"


def test_runner_declares_then_runs_pools_under_one_budget():
    src = Path(wapp.__file__).read_text()
    body = src.split("def _baremetal_install_runner(", 1)[1].split("\ndef ", 1)[0]
    assert body.index('step("wait-api", "done"') < body.index('step("declare", "running"')
    assert body.index('step("cleanup", "done"') < body.index('step("declare", "running"')
    assert body.index("_bm_declare_cluster(") < body.index('step("pools", "running"')
    assert "        if pools:\n" in body                          # rien d'affiché sans pools
    assert "_bm_apply_pools(opts, kubeconfig, run, step, post_deadline - time.time())" in body
    assert body.count("HARVESTER_POOLS_TIMEOUT") == 1
    # la clé part dans la configuration AVANT qu'elle soit rendue
    assert body.index("_bm_with_ssh_key(") < body.index("cfg_yaml = _harvester_install_config")
    assert "_bm_join_pubkey(opts[\"cluster\"])" in body
    # tout ce qui suit l'installation finit par fail() en cas d'exception
    post = body.split('step("cleanup", "done"', 1)[1]
    assert "except Exception as e:" in post and "return fail(current[0]" in post
    # les clés sont gardées une fois la machine repartie sur l'installeur
    assert body.index("keep_creds[0] = True") < body.index('step("power", "done"')


def test_the_pools_step_goes_through_the_command_line_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(wapp, "ISO_DIR", tmp_path)
    seen = {}

    class P:
        def __init__(self, cmd, **kw):
            seen["cmd"] = cmd
            spec = Path(cmd[cmd.index("--spec") + 1])
            seen["mode"] = stat.S_IMODE(spec.stat().st_mode)
            seen["spec"] = json.loads(spec.read_text())
            self.stderr = iter(["STEP_EVENT|find|running|2 disk(s)\n",
                                "STEP_EVENT|find|error|not found on n1: serial S\n"])

        def poll(self):
            return 2

        def wait(self):
            return 2
    monkeypatch.setattr(wapp.subprocess, "Popen", P)
    steps = []
    run = type("R", (), {"id": "r1"})()
    opts = {"hostname": "n1", "mode": "join", "pools": hh.check_pools(POOLS)}
    code, why = wapp._bm_apply_pools(opts, "/kc", run, lambda *a: steps.append(a), 123.7)
    assert code == 2 and why == "not found on n1: serial S"
    cmd = seen["cmd"]
    assert cmd[1].endswith("harvester-resources.py") and cmd[2] == "pools-apply"
    assert cmd[cmd.index("--node") + 1] == "n1" and "--no-classes" in cmd
    assert cmd[cmd.index("--timeout") + 1] == "123"                  # le temps restant, pas 1800
    assert seen["spec"] == {"pools": opts["pools"]} and seen["mode"] == 0o600
    assert not list(tmp_path.rglob("pools-*.json"))
    assert all(s[0] == "pools" and s[1] == "progress" for s in steps)
    wapp._bm_apply_pools(dict(opts, mode="create"), "/kc", run, lambda *a: None, 60)
    assert "--no-classes" not in seen["cmd"]


def test_cancel_from_the_dock_stops_pools_apply(monkeypatch, tmp_path):
    import threading
    import time as _t
    monkeypatch.setattr(wapp, "ISO_DIR", tmp_path)
    done = threading.Event()

    class P:
        terminated = False

        def __init__(self, cmd, **kw):
            self.stderr = self._lines()

        def _lines(self):
            done.wait(5)            # stderr ne rend rien tant que le processus vit
            return
            yield

        def poll(self):
            return 143 if P.terminated else None

        def terminate(self):
            P.terminated = True
            done.set()

        def wait(self):
            return 143
    monkeypatch.setattr(wapp.subprocess, "Popen", P)
    run = type("R", (), {"id": "r1", "_cancel": True})()
    t0 = _t.time()
    code, _ = wapp._bm_apply_pools({"hostname": "n1", "pools": []}, "/kc", run, lambda *a: None, 60)
    assert P.terminated and code == 143 and _t.time() - t0 < 5


def test_the_cluster_key_joins_the_keys_where_they_already_are():
    out = wapp._bm_with_ssh_key({"ssh_keys": "ssh-ed25519 AAAA one"}, "ssh-ed25519 BBBB pool")
    assert out["ssh_keys"].splitlines() == ["ssh-ed25519 AAAA one", "ssh-ed25519 BBBB pool"]
    assert wapp._bm_with_ssh_key({}, "ssh-ed25519 BBBB pool")["ssh_keys"] == "ssh-ed25519 BBBB pool"
    adv = "os:\n  ssh_authorized_keys:\n  - ssh-ed25519 AAAA adv\n"
    out = wapp._bm_with_ssh_key({"advanced_yaml": adv}, "ssh-ed25519 BBBB pool")
    assert not out.get("ssh_keys")
    import yaml
    assert yaml.safe_load(out["advanced_yaml"])["os"]["ssh_authorized_keys"] == [
        "ssh-ed25519 AAAA adv", "ssh-ed25519 BBBB pool"]
    wapp._his.render_install_config(dict(BASE, iso_url="http://192.0.2.1/x.iso", method="dhcp", **out))


def test_the_kubeconfig_is_read_over_ssh_with_the_cluster_known_hosts(monkeypatch, tmp_path):
    rke2 = ("apiVersion: v1\nkind: Config\nclusters:\n- name: default\n  cluster:\n"
            "    certificate-authority-data: Q0E=\n    server: https://127.0.0.1:6443\n"
            "users:\n- name: default\n  user: {client-certificate-data: Qw==}\n")
    answers = iter([type("P", (), {"returncode": 255, "stdout": ""})(),
                    type("P", (), {"returncode": 0, "stdout": rke2})()])
    calls = []
    monkeypatch.setattr(wapp.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or next(answers))
    monkeypatch.setattr(wapp.time, "sleep", lambda s: None)
    out = tmp_path / "kc"
    out.write_text("stale")
    out.chmod(0o644)
    steps = []
    ok = wapp._bm_fetch_kubeconfig("192.0.2.50", tmp_path / "k", tmp_path / "kh", out,
                                   wapp.time.time() + 60, type("R", (), {})(), lambda *a: steps.append(a))
    assert ok is True
    import yaml
    doc = yaml.safe_load(out.read_text())
    assert doc["clusters"][0]["cluster"]["server"] == "https://192.0.2.50:6443"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    c = calls[0]
    assert c[c.index("-i") + 1] == str(tmp_path / "k")
    assert f"UserKnownHostsFile={tmp_path / 'kh'}" in c and "StrictHostKeyChecking=accept-new" in c
    assert "rancher@192.0.2.50" in c and "BatchMode=yes" in c
    assert all("Qw==" not in str(s) for s in steps)


@pytest.fixture()
def console(monkeypatch, tmp_path):
    """Configuration de la console dans un répertoire jetable."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("clusters: []\n")
    monkeypatch.setattr(wapp, "CONFIG_PATH", cfg)
    monkeypatch.setattr(wapp, "load_config", lambda: __import__("yaml").safe_load(cfg.read_text()))
    return tmp_path


@pytest.mark.skipif(not __import__("shutil").which("ssh-keygen"), reason="ssh-keygen absent")
def test_the_new_cluster_gets_its_own_key_and_is_declared(console):
    f = wapp._bm_cluster_keypair("lab1")
    assert f["key"] == console / "ssh" / "lab1_id"
    assert stat.S_IMODE(f["key"].stat().st_mode) == 0o600
    assert f["pub"].read_text().startswith("ssh-ed25519 ")
    kc = f["kubeconfig"]
    wapp._bm_write_private(kc, "apiVersion: v1\n")
    c = wapp._bm_declare_cluster("lab1", kc, f["key"], "n1", "192.0.2.50")
    assert c["kubeconfig"] == str(console / "kubeconfigs" / "lab1.yaml")
    assert c["ssh"] == {"user": "rancher", "port": 22, "key": str(f["key"]), "generated": True}
    assert c["nodes"] == [{"hostname": "n1", "ip": "192.0.2.50", "role": "control-plane"}]
    assert [x["name"] for x in wapp.load_config()["clusters"]] == ["lab1"]
    with pytest.raises(ValueError, match="already exists"):
        wapp._bm_declare_cluster("lab1", kc, f["key"], "n1", "192.0.2.50")
    # un nœud qui rejoint reçoit la même clé publique
    assert wapp._bm_join_pubkey("lab1").split()[:2] == f["pub"].read_text().split()[:2]
    assert wapp._bm_join_pubkey("nope") is None


def test_join_keeps_todays_keys_when_the_cluster_key_is_not_the_console_s(console):
    (console / "id").write_text("x")
    wapp._atomic_write_config({"clusters": [{"name": "c", "ssh": {"key": str(console / "id")}}]})
    assert wapp._bm_join_pubkey("c") is None


def test_settings_keep_the_generated_mark_only_for_the_same_key(console, monkeypatch):
    import io
    wapp.app.config["TESTING"] = True
    monkeypatch.setattr(wapp, "current_user", lambda: "root")
    ssh = console / "ssh"
    ssh.mkdir(exist_ok=True)
    for n in ("lab1_id", "lab1_id.pub", "lab1_known_hosts"):
        (ssh / n).write_text("x")
    node = {"hostname": "n1", "ip": "192.0.2.50", "role": "control-plane"}
    wapp._atomic_write_config({"clusters": [{"name": "lab1", "kubeconfig": "", "description": "",
                                             "ssh": {"user": "rancher", "port": 22, "key": str(ssh / "lab1_id"),
                                                     "generated": True}, "nodes": [node]}]})
    c = wapp.app.test_client()
    r = c.put("/api/clusters/lab1", json={"name": "lab1", "description": "d", "nodes": [node]})
    assert r.status_code == 200 and wapp.load_config()["clusters"][0]["ssh"]["generated"] is True
    # renommé : la clé, sa publique et les clés d'hôte suivent
    r = c.put("/api/clusters/lab1", json={"name": "lab2", "nodes": [node]})
    assert r.status_code == 200
    got = wapp.load_config()["clusters"][0]
    assert got["ssh"]["key"] == str(ssh / "lab2_id") and got["ssh"]["generated"] is True
    assert (ssh / "lab2_id.pub").exists() and (ssh / "lab2_known_hosts").exists()
    # une clé téléversée n'est plus celle de la console
    key = b"-----BEGIN OPENSSH PRIVATE KEY-----\nx\n"
    r = c.post("/api/clusters/lab2/sshkey", data={"file": (io.BytesIO(key), "id")},
               content_type="multipart/form-data")
    assert r.status_code == 200 and "generated" not in wapp.load_config()["clusters"][0]["ssh"]
    # supprimé : publique et clés d'hôte partent avec la clé
    assert c.delete("/api/clusters/lab2").status_code == 200
    assert not list(ssh.iterdir())


# -- disque système, disque de données, disque de Longhorn ----------------------------

def test_a_block_device_used_by_the_system_is_never_a_pool_disk(steps):
    def with_child(label="", mount="", parent_name="uuid-a"):
        child = a_bd("uuid-a-p1", serial="SER-A1", dtype="part")
        child["status"]["deviceStatus"]["parentDevice"] = "/dev/uui"
        child["status"]["deviceStatus"]["details"]["label"] = label
        child["status"]["deviceStatus"]["fileSystem"]["mountPoint"] = mount
        return child
    for child, why in ((with_child(label="COS_STATE"), "labelled COS_STATE"),
                       (with_child(label="HARV_LH_DEFAULT"), "labelled HARV_LH_DEFAULT"),
                       (with_child(mount="/var/lib/harvester/defaultdisk"), "mounted on")):
        k = FakeKube([a_bd("uuid-a", serial="SER-A1"), child])
        with pytest.raises(ValueError, match=why):
            run(k, POOLS[:1])
        assert not k.replaced
    # une partition d'un autre nœud ne compte pas
    other = with_child(label="COS_STATE")
    other["spec"]["nodeName"] = "n2"
    k = FakeKube([a_bd("uuid-a", serial="SER-A1"), other])
    assert run(k, POOLS[:1]) == hres.EXIT_OK
    # le disque par défaut de Longhorn, déclaré sous le nom du BlockDevice
    k = FakeKube([a_bd("uuid-a", serial="SER-A1")])
    k.objs[(hh.K_LHNODE, NS, "n1")]["spec"]["disks"]["uuid-a"] = {"path": "/var/lib/harvester/defaultdisk"}
    with pytest.raises(ValueError, match="Longhorn's default disk"):
        run(k, POOLS[:1])
    assert not k.replaced
