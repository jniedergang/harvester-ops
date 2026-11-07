"""v2.6.1 : port d'écriture 5443 des registres d'amorçage réservé à la
console (second Service LoadBalancer à la même adresse kube-vip, limité par
loadBalancerSourceRanges ; essai de la tâche 13 de la 2.6.0), réglage lu à
chaud, reprise des registres posés par la 2.6.0, et la CLI
(`settings`, `write-access`, `check --vm-network`)."""

import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(ROOT / "tests" / "api"))
import registries as rg  # noqa: E402
import registry_seed as rs  # noqa: E402
from test_registry_seed_cli_260 import FakeKube as _FakeKube, cli, deploy, world  # noqa: E402,F401


class FakeKube(_FakeKube):
    """Note aussi les patchs de fusion (`patched`), et les rejoue sur l'objet."""

    def __init__(self, objects=None):
        super().__init__(objects)
        self.patched = []

    def patch(self, kind, ns, name, patch, dry_run=False):
        self.patched.append((kind, name, patch))
        o = json.loads(json.dumps(self.objects[(kind, name)]))
        o.setdefault("spec", {}).update(patch.get("spec") or {})
        self.objects[(kind, name)] = o


# ---------------------------------------------------------------------------
# Réglage
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value,want", [
    ("172.16.1.11", ["172.16.1.11/32"]),
    ("172.16.1.11/32, 10.1.2.3/8", ["10.0.0.0/8", "172.16.1.11/32"]),
    (["172.16.1.11", "172.16.1.11/32"], ["172.16.1.11/32"]),
    ("", []),
    (None, []),
    ("0.0.0.0/0", ValueError),
    ("fe80::1", ValueError),
    ("172.16.1.300", ValueError),
    ([42], ValueError),
    (",".join(f"10.0.{i}.1" for i in range(rg.SOURCES_MAX + 1)), ValueError),
])
def test_normalize_sources(value, want):
    if want is ValueError:
        with pytest.raises(ValueError):
            rg.normalize_sources(value)
    else:
        assert rg.normalize_sources(value) == want


def test_settings_round_trip_and_a_damaged_file_reads_as_open(tmp_path):
    assert rg.settings(tmp_path) == {"write_sources": []}
    rg.save_settings(tmp_path, "172.16.1.11")
    assert rg.settings(tmp_path) == {"write_sources": ["172.16.1.11/32"]}
    assert oct((tmp_path / rg.SETTINGS_FILE).stat().st_mode & 0o777) == "0o600"
    with pytest.raises(ValueError):
        rg.save_settings(tmp_path, "nope")
    # revue 2.6.1 : un fichier abîmé n'est jamais lu comme « ouvert à tous »
    for bad in ("{not json", '{"write_sources": ["0.0.0.0/0"]}', "[]"):
        (tmp_path / rg.SETTINGS_FILE).write_text(bad)
        with pytest.raises(rg.SettingsError):
            rg.settings(tmp_path)


def test_settings_is_a_reserved_registry_name(tmp_path):
    import deploy_plans as dp
    with pytest.raises(dp.ProfileError):
        rg.save(tmp_path, "settings", {"kind": "external", "endpoint": "h.example"})
    # `<a>-write` est le Service d'écriture du registre d'amorçage `a`
    with pytest.raises(dp.ProfileError):
        rg.save(tmp_path, "a-write", {"kind": "bootstrap", "endpoint": "10.0.0.1", "cluster": "c",
                                      "address": "10.0.0.1", "size_gib": 5, "storage_class": "s"})
    rg.save(tmp_path, "a-write", {"kind": "external", "endpoint": "h.example"})


# ---------------------------------------------------------------------------
# Reprise d'un registre posé
# ---------------------------------------------------------------------------
LEGACY = {"metadata": {"name": "amorcage", "namespace": rs.NAMESPACE,
                       "labels": {rs.LABEL: "amorcage", "harvester-ops.io/plan": "p1"},
                       "annotations": {rs.VIP_ANNOTATION: "172.16.2.76"}},
          "spec": {"ports": [{"name": "read", "port": 443}, {"name": "write", "port": 5443}]}}
MIGRATED = {"metadata": LEGACY["metadata"], "spec": {"ports": [{"name": "read", "port": 443}]}}


def write_svc(sources):
    spec = {"ports": [{"name": "write", "port": 5443}]}
    if sources:
        spec["loadBalancerSourceRanges"] = sources
    return {"metadata": {"name": "amorcage-write"}, "spec": spec}


@pytest.mark.parametrize("objects,sources,applied,changed", [
    # registre de la 2.6.0 : 5443 quitte d'abord le Service principal
    ({"amorcage": LEGACY}, ["172.16.1.11/32"], ["amorcage", "amorcage-write"], True),
    ({"amorcage": LEGACY}, [], ["amorcage", "amorcage-write"], True),
    # Service d'écriture déjà là mais 5443 encore sur le principal
    ({"amorcage": LEGACY, "amorcage-write": write_svc(["172.16.1.11/32"])}, ["172.16.1.11/32"],
     ["amorcage", "amorcage-write"], True),
    # déjà porté : réglage changé, seul le Service d'écriture bouge
    ({"amorcage": MIGRATED, "amorcage-write": write_svc(["10.0.0.0/8"])}, ["172.16.1.11/32"],
     ["amorcage-write"], True),
    ({"amorcage": MIGRATED, "amorcage-write": write_svc(["172.16.1.11/32"])}, ["172.16.1.11/32"], [], False),
    ({"amorcage": MIGRATED, "amorcage-write": write_svc([])}, [], [], False),
    # Service d'écriture absent (retiré à la main) : recréé
    ({"amorcage": MIGRATED}, ["172.16.1.11/32"], ["amorcage-write"], True),
])
def test_reconcile_write_access(objects, sources, applied, changed):
    kube = FakeKube({("services", n): o for n, o in objects.items()})
    res = rs.reconcile_write_access(kube, "amorcage", sources)
    # le principal est corrigé par patch (port retiré quel que soit son
    # gestionnaire), avant que le Service d'écriture soit posé
    assert [n for _, n, _ in kube.patched] + [d["metadata"]["name"] for d in kube.applied] == applied
    assert res == {"changed": changed, "missing": False, "sources": sources}
    assert [p["port"] for p in kube.objects[("services", "amorcage")]["spec"]["ports"]] == [443]
    for _, _, patch in kube.patched:
        assert patch == {"spec": {"ports": [{"name": "read", "port": 443}]}}
    for d in kube.applied:
        # étiquettes gardées (origine d'un plan), même adresse kube-vip
        assert d["metadata"]["name"] == "amorcage-write"
        assert d["metadata"]["labels"] == LEGACY["metadata"]["labels"]
        assert d["metadata"]["annotations"] == {rs.VIP_ANNOTATION: "172.16.2.76"}
        assert d["spec"].get("loadBalancerSourceRanges", []) == sources


def test_reconcile_of_a_registry_without_service():
    kube = FakeKube()
    assert rs.reconcile_write_access(kube, "amorcage", ["172.16.1.11/32"]) == \
        {"changed": False, "missing": True, "sources": []}
    assert kube.applied == []


@pytest.mark.parametrize("main,write,want", [
    (LEGACY, None, {"migrated": False, "sources": []}),
    (LEGACY, write_svc(["172.16.1.11/32"]), {"migrated": False, "sources": []}),
    (MIGRATED, write_svc(["172.16.1.11/32"]), {"migrated": True, "sources": ["172.16.1.11/32"]}),
    (MIGRATED, write_svc([]), {"migrated": True, "sources": []}),
])
def test_write_access_reads_the_services(main, write, want):
    assert rs.write_access(main, write) == want


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_deploy_reserves_the_write_port_to_the_console_setting(world, capsys):
    rg.save_settings(world["state"], "172.16.1.11")
    code, out, err = deploy(capsys)
    assert code == 0, err
    svcs = {d["metadata"]["name"]: d for d in world["kube"].applied if d["kind"] == "Service"}
    assert svcs["amorcage-write"]["spec"]["loadBalancerSourceRanges"] == ["172.16.1.11/32"]
    assert [p["port"] for p in svcs["amorcage"]["spec"]["ports"]] == [443]
    assert "reserved to 172.16.1.11/32" in err


def test_settings_cli_sets_shows_and_clears(world, capsys):
    assert cli.main(["settings", "--write-sources", "172.16.1.11"]) == 0
    assert json.loads(capsys.readouterr().out) == {"write_sources": ["172.16.1.11/32"]}
    assert cli.main(["settings"]) == 0
    assert json.loads(capsys.readouterr().out) == {"write_sources": ["172.16.1.11/32"]}
    assert cli.main(["settings", "--write-sources", "0.0.0.0/0"]) == 2
    assert "every address" in capsys.readouterr().err
    assert rg.settings(world["state"]) == {"write_sources": ["172.16.1.11/32"]}
    assert cli.main(["settings", "--clear"]) == 0
    assert json.loads(capsys.readouterr().out) == {"write_sources": []}


def test_write_access_cli_migrates_every_bootstrap_registry_of_the_cluster(world, capsys):
    assert deploy(capsys)[0] == 0                       # réglage vide : ouvert, déjà à part
    kube = world["kube"]
    kube.patched = []
    kube.patch = types.MethodType(FakeKube.patch, kube)
    kube.objects[("services", "amorcage")] = json.loads(json.dumps(LEGACY))      # comme posé par la 2.6.0
    kube.objects.pop(("services", "amorcage-write"))
    rg.save(world["state"], "harbor", {"kind": "external", "endpoint": "h.example"})
    rg.save_settings(world["state"], "172.16.1.11")
    kube.applied.clear()
    assert cli.main(["write-access", "--cluster", "harvlab2"]) == 0
    o = capsys.readouterr()
    assert [n for _, n, _ in kube.patched] == ["amorcage"]
    assert [d["metadata"]["name"] for d in kube.applied] == ["amorcage-write"]
    res = json.loads(o.out)
    assert res["write_sources"] == ["172.16.1.11/32"] and [r["registry"] for r in res["registries"]] == ["amorcage"]
    assert "STEP_EVENT|write-access|done|amorcage brought to 172.16.1.11/32" in o.err
    kube.applied.clear()
    assert cli.main(["write-access", "--cluster", "harvlab2"]) == 0          # idempotent
    assert kube.applied == [] and "already at" in capsys.readouterr().err
    assert cli.main(["write-access", "harbor", "--cluster", "harvlab2"]) == 2


def test_delete_and_leftovers_include_the_write_service(world, capsys):
    assert deploy(capsys)[0] == 0
    assert cli.main(["delete", "amorcage", "--cluster", "harvlab2"]) == 0
    assert ("services", "amorcage-write") in world["kube"].deleted


def test_delete_never_removes_an_object_of_another_registry(world, capsys):
    assert deploy(capsys)[0] == 0
    world["kube"].objects[("services", "amorcage-write")]["metadata"]["labels"] = {rs.LABEL: "other"}
    assert cli.main(["delete", "amorcage", "--cluster", "harvlab2"]) == 0
    assert ("services", "amorcage-write") not in world["kube"].deleted
    assert "services amorcage-write kept" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["amorcage-write"])
def test_a_bootstrap_registry_name_cannot_end_with_write(world, capsys, name):
    code = cli.main(["deploy", name, "--cluster", "harvlab2", "--address", "172.16.2.76",
                     "--storage-class", "harv-rep1"])
    assert code == 2 and world["kube"].applied == []
    assert "cannot end with -write" in capsys.readouterr().err


def test_a_foreign_write_service_takes_the_name(world, capsys):
    world["kube"].objects[("services", "amorcage-write")] = {"metadata": {"labels": {rs.LABEL: "amorcage-write"}}}
    code, out, _ = deploy(capsys, "--dry-run")
    assert code == 2 and "registry-name-taken" in out


def test_a_damaged_setting_refuses_deploy_and_write_access(world, capsys):
    (world["state"] / rg.SETTINGS_FILE).write_text("{not json")
    code, out, err = deploy(capsys)
    assert code == 2 and "invalid" in err
    assert world["kube"].applied == [] and world["imports"] == []           # refusé avant tout geste
    assert cli.main(["write-access", "--cluster", "harvlab2"]) == 2
    assert cli.main(["settings"]) == 1
    assert cli.main(["settings", "--clear"]) == 0                    # le réenregistrer répare


def test_check_flags_an_open_write_port_and_probes_the_chosen_network(world, capsys, monkeypatch):
    assert deploy(capsys)[0] == 0
    # port ouvert, mais vu d'un autre cluster où il n'est que rattaché : pas de faux constat
    assert cli.write_open(world["kube"], rg.load(world["state"], "amorcage"), "harv4") is False
    assert cli.write_open(world["kube"], rg.load(world["state"], "amorcage"), "harvlab2") is True
    capsys.readouterr()
    monkeypatch.setattr(rg, "facts", lambda kube, d, c: {"attached": True, "auth": {"pull": True, "push": True}})
    seen = []

    def fake_cached(state, cluster, kube, network, endpoints, image, fresh=False):
        seen.append((cluster, network, endpoints, fresh))
        return {"network": network, "status": "probed", "address": "172.16.11.9",
                "results": {"172.16.2.76": {"ok": True, "failed": None}}}
    monkeypatch.setattr(cli.vm_probe, "cached_run", fake_cached)
    assert cli.main(["check", "amorcage", "--cluster", "harvlab2", "--vm-network", "default/lab-net"]) == 0
    codes = [f["code"] for f in json.loads(capsys.readouterr().out)["findings"]]
    assert codes == ["registry-write-port-open", rg.VM_NETWORK_OK]
    assert seen == [("harvlab2", "default/lab-net", ["172.16.2.76"], True)]       # geste explicite : sonde neuve
    rg.save_settings(world["state"], "172.16.1.11")
    assert cli.main(["write-access", "--cluster", "harvlab2"]) == 0
    capsys.readouterr()
    assert cli.main(["check", "amorcage", "--cluster", "harvlab2"]) == 0
    codes = [f["code"] for f in json.loads(capsys.readouterr().out)["findings"]]
    assert codes == [rg.VM_NETWORK_WARNING] and len(seen) == 1                    # sans réseau : non vérifié

    assert cli.main(["check", "amorcage", "--cluster", "harvlab2", "--vm-network", "lab-net"]) == 2


# ---------------------------------------------------------------------------
# Routes (console)
# ---------------------------------------------------------------------------
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402
from test_deploy_routes_240 import env  # noqa: E402,F401


@pytest.mark.parametrize("path,method,role", [
    ("/api/registries/settings", "GET", "viewer"),
    ("/api/registries/settings", "PUT", "admin"),
    ("/api/registries/amorcage/check/harvlab", "POST", "operator"),
])
def test_roles_of_the_261_routes(path, method, role):
    assert wapp.required_role_for(path, method) == role


def test_the_settings_route_has_a_readable_limit():
    import re
    from limits import parse_many
    src = (ROOT / "web" / "app.py").read_text()
    i = src.index("def api_registries_settings_put(")
    parse_many(re.findall(r'@_rate_limit\("([^"]+)"\)', src[i - 160:i])[-1])


def _bootstrap(state):
    rg.save(state, "amorcage", {"kind": "bootstrap", "software": "distribution", "endpoint": "172.16.2.76",
                                "push_endpoint": "172.16.2.76:5443", "usage": ["images"], "mirrors": [],
                                "clusters": ["harvlab"], "cluster": "harvlab", "address": "172.16.2.76",
                                "size_gib": 8, "storage_class": "harv-rep1"})


@pytest.mark.parametrize("payload,status,sources", [
    ({"write_sources": ["172.16.1.11"]}, 200, ["172.16.1.11/32"]),
    ({"write_sources": []}, 200, []),
    ({"write_sources": ["0.0.0.0/0"]}, 400, None),
    ({"write_sources": ["nope"]}, 400, None),
    ({"sources": ["172.16.1.11"]}, 400, None),
    ([], 400, None),
])
def test_saving_the_setting_brings_each_bootstrap_registry_to_it(env, monkeypatch, payload, status, sources):
    _bootstrap(env["state"])
    seen = []

    class Run:
        id = "w1w1w1w1w1w1"
    monkeypatch.setattr(wapp, "_cli_action", lambda cluster, label, cmd, tool, spec=None, **kw:
                        seen.append((cluster, label, cmd)) or (Run(), None))
    r = env["client"].put("/api/registries/settings", json=payload)
    assert r.status_code == status, r.get_json()
    if status != 200:
        assert seen == [] and rg.settings(env["state"]) == {"write_sources": []}
        return
    res = r.get_json()
    assert res["write_sources"] == sources and rg.settings(env["state"])["write_sources"] == sources
    assert res["actions"] == [{"cluster": "harvlab", "action_id": "w1w1w1w1w1w1"}] and res["skipped"] == []
    (cluster, label, cmd), = seen
    assert cluster == "harvlab" and label == "registry-write-access:harvlab"
    assert cmd[3:] == ["write-access", "--cluster", "harvlab", "--kubeconfig", env["kc"]]


def test_reading_the_setting_suggests_the_console_address(env, monkeypatch):
    _bootstrap(env["state"])
    rg.save_settings(env["state"], "10.0.0.0/8")
    monkeypatch.setattr(wapp, "_registry_console_address", lambda c: "172.16.1.11" if c == "harvlab" else None)
    r = env["client"].get("/api/registries/settings?cluster=harv4")
    assert r.get_json() == {"write_sources": ["10.0.0.0/8"], "suggested": {"harvlab": "172.16.1.11/32"},
                            "bootstrap": {"harvlab": ["amorcage"]}}


@pytest.mark.parametrize("payload,extra,status", [
    ({"vm_network": "default/lab-net"}, ["--vm-network", "default/lab-net"], 200),
    ({}, [], 200),
    ({"vm_network": "lab-net"}, None, 400),
    ({"vm_network": ["x"]}, None, 400),
])
def test_the_check_route_passes_the_vm_network(env, monkeypatch, payload, extra, status):
    _bootstrap(env["state"])
    got = []
    monkeypatch.setattr(wapp, "_capi_run_json", lambda cmd, spec=None, timeout=180:
                        got.append((cmd, timeout)) or (({"findings": [], "blocked": False}, 0), None))
    r = env["client"].post("/api/registries/amorcage/check/harvlab", json=payload)
    assert r.status_code == status
    if extra is None:
        assert got == []
    else:
        cmd, timeout = got[0]
        assert cmd[-len(extra):] == extra if extra else cmd[-1] != "--vm-network"
        assert timeout >= 150                                    # la pod sonde tient dans le délai
