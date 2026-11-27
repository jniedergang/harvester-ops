"""2.10.1 tâche 6 (point 324 A) : LVM en GA, type `dm-thin` par défaut.

Harvester 1.9.1 déprécie le type LVM `striped` (notes de version v1.9.1) et
exige `dm-thin` : une classe `striped` créée par la console y serait une
impasse. `dm-thin` devient le défaut partout ; `striped` est refusé à partir
de 1.9.1 (version lue sur le cluster) et averti avant. Les volumes LVM d'un
cluster d'avant 1.9.1 sont relevés pour le contrôle de mise à jour (tâche 7).

Hypothèses à confirmer au banc (tâche 1, essai 9) : le pilote GA garde le
provisionneur `lvm.driver.harvesterhci.io`, la clé de topologie
`topology.lvm.csi/node` et les paramètres `vgName` et `type` ; tout volume au
provisionneur LVM d'un cluster en 1.9.0 est « d'avant la GA ».

Mutations rapportées (rapport 2.10.1, tâche 6) : « `striped` par défaut » ->
`test_default_type_is_dm_thin` échoue ; « refus sur le nom de version seul
1.9.1 exact » (== au lieu de >=) -> le cas v1.10.0 échoue.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import hv_advanced as ha  # noqa: E402

BASE = {"name": "local", "engine": "lvm", "node": "n1", "vg": "vg-data"}


def test_default_type_is_dm_thin():
    sc = ha.storage_class(dict(BASE))
    assert sc["parameters"] == {"vgName": "vg-data", "type": "dm-thin"}
    assert sc["provisioner"] == "lvm.driver.harvesterhci.io"
    assert sc["allowedTopologies"] == [{"matchLabelExpressions": [{"key": "topology.lvm.csi/node",
                                                                   "values": ["n1"]}]}]


@pytest.mark.parametrize("version,ok", [
    ("v1.9.0", True), (None, True), ("garbage", True),
    ("v1.9.1", False), ("v1.9.1-rc1", False), ("v1.9.2", False), ("v1.10.0", False), ("dev", False),
])
def test_striped_by_version(version, ok):
    spec = dict(BASE, lvm_type="striped")
    if ok:
        assert ha.storage_class(spec, harvester_version=version)["parameters"]["type"] == "striped"
    else:
        with pytest.raises(ValueError, match="dm-thin"):
            ha.storage_class(spec, harvester_version=version)


@pytest.mark.parametrize("version,want", [
    ("v1.9.0", {"striped": "warn", "default": "dm-thin"}),
    ("v1.9.1", {"striped": "refused", "default": "dm-thin"}),
    (None, {"striped": "warn", "default": "dm-thin"}),
])
def test_lvm_type_policy(version, want):
    assert ha.lvm_type_policy(version) == want


def sc(name, prov, lvtype=None):
    o = {"metadata": {"name": name}, "provisioner": prov}
    if lvtype:
        o["parameters"] = {"vgName": "vg", "type": lvtype}
    return o


def pv(name, driver, sc_name, claim="default/data"):
    ns, n = claim.split("/")
    return {"metadata": {"name": name}, "spec": {"csi": {"driver": driver}, "storageClassName": sc_name,
                                                 "claimRef": {"namespace": ns, "name": n}}}


@pytest.mark.parametrize("label,scs,pvs,want", [
    ("no LVM", [sc("longhorn", ha.LH)], [pv("pv1", ha.LH, "longhorn")], {"lvm": [], "striped": []}),
    ("LVM dm-thin", [sc("local", ha.LVM, "dm-thin")], [pv("pv1", ha.LVM, "local")],
     {"lvm": ["default/data"], "striped": []}),
    ("LVM striped", [sc("st", ha.LVM, "striped")], [pv("pv1", ha.LVM, "st", "prod/db")],
     {"lvm": ["prod/db"], "striped": ["prod/db"]}),
    ("class gone, driver says LVM", [], [pv("pv1", ha.LVM, "gone")], {"lvm": ["default/data"], "striped": []}),
])
def test_lvm_volumes(label, scs, pvs, want):
    assert ha.lvm_volumes(scs, pvs) == want, label


@pytest.mark.parametrize("version,rc_ok", [("v1.9.0", True), ("v1.9.1", False)])
def test_cli_refuses_striped_on_the_version_it_reads(tmp_path, monkeypatch, version, rc_ok):
    import importlib.util
    import json
    s = importlib.util.spec_from_file_location("harvester_resources_2101", ROOT / "bin" / "harvester-resources.py")
    cli = importlib.util.module_from_spec(s)
    s.loader.exec_module(cli)
    created = []

    class K:
        def get(self, kind, ns, name):
            return {"value": version} if name == "server-version" else None

        def create(self, obj):
            created.append(obj)
    monkeypatch.setattr(cli, "kube_from", lambda a: K())
    monkeypatch.setattr(cli, "step", lambda *a, **k: None)
    spec = tmp_path / "s.json"
    spec.write_text(json.dumps(dict(BASE, lvm_type="striped")))
    args = type("A", (), {"spec": str(spec)})()
    if rc_ok:
        cli.cmd_storageclass(args)
        assert created and created[0]["parameters"]["type"] == "striped"
    else:
        with pytest.raises(ValueError, match="dm-thin only"):
            cli.cmd_storageclass(args)
        assert created == []


def test_storage_options_route_gives_the_policy(monkeypatch):
    sys.path.insert(0, str(ROOT / "web"))
    import app as wapp
    wapp.app.config["TESTING"] = True
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/kc")
    monkeypatch.setattr(wapp, "_kubectl_json", lambda kc, *a, **k: {"value": "v1.9.1"}
                        if "server-version" in a else {"items": []})
    d = wapp.app.test_client().get("/api/storage-options/h1").get_json()
    assert d["lvm_types"] == {"default": "dm-thin", "striped": "refused"}
