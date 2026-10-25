"""Prochaine version KVM : « redémarrage nécessaire » servi par la liste des
VMs (bin/lib/kvm_pending.py) sans relire le XML de chaque VM.

Le repère est noté par la console quand elle compare les deux XML (fenêtre
« Modifier », relecture après une modification ou un geste), avec l'Id
libvirt de la VM ; il tombe dès que la VM est vue arrêtée, redémarrée (autre
Id) ou absente d'un hôte lu."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_kvm_routes_196 import client, env, wapp  # noqa: E402,F401 : fixtures partagées

import kvm_pending as kp  # noqa: E402  (bin/lib dans le chemin par app)

kh, ke = wapp._kh, wapp._ke


def vm(name, state="running", id_=7, host="node2"):
    return {"host": host, "name": name, "state": state, "id": id_}


# (libellé, repères notés, VMs lues, hôtes lus, pending attendu par VM, repères restants)
CASES = [
    ("same id", {"node2/web": (7, ["memory.max_mib"])}, [vm("web")], ["node2"],
     {"web": ["memory.max_mib"]}, ["node2/web"]),
    ("restarted", {"node2/web": (7, ["display.video"])}, [vm("web", id_=8)], ["node2"], {"web": []}, []),
    ("shut off", {"node2/web": (7, ["display.video"])}, [vm("web", "shut off", None)], ["node2"], {"web": []}, []),
    ("paused keeps it", {"node2/web": (7, ["tpm"])}, [vm("web", "paused")], ["node2"], {"web": ["tpm"]}, ["node2/web"]),
    ("gone from a read host", {"node2/old": (3, ["tpm"])}, [vm("web")], ["node2"], {"web": []}, []),
    ("host not read keeps it", {"ghost/a": (3, ["tpm"])}, [vm("web")], ["node2"], {"web": []}, ["ghost/a"]),
    ("several", {"node2/a": (1, ["x"]), "node2/b": (2, ["y"]), "node2/c": (3, ["z"])},
     [vm("a", id_=1), vm("b", id_=5), vm("c", "shut off", None)], ["node2"], {"a": ["x"], "b": [], "c": []}, ["node2/a"]),
    ("nothing noted", {}, [vm("web")], ["node2"], {"web": []}, []),
]


@pytest.mark.parametrize("label,noted,vms,read,expect,left", CASES, ids=[c[0] for c in CASES])
def test_annotate_table(tmp_path, label, noted, vms, read, expect, left):
    for key, (i, fields) in noted.items():
        h, n = key.split("/")
        kp.record(tmp_path, h, n, i, fields)
    out = kp.annotate(tmp_path, vms, read)
    assert {v["name"]: v["pending"] for v in out} == expect
    assert sorted(kp.load(tmp_path)) == sorted(left)


@pytest.mark.parametrize("dom_id,fields,kept", [
    (7, ["b", "a", "a"], {"id": 7, "fields": ["a", "b"]}),
    (7, [], None),            # plus rien en attente : repère effacé
    (None, ["a"], None),      # VM arrêtée (pas d'Id) : rien à noter
])
def test_record_table(tmp_path, dom_id, fields, kept):
    kp.record(tmp_path, "node2", "web", 1, ["old"])
    kp.record(tmp_path, "node2", "web", dom_id, fields)
    assert kp.load(tmp_path).get("node2/web") == kept


def test_a_broken_file_reads_as_empty_and_is_rewritten(tmp_path):
    (tmp_path / "kvm.d").mkdir()
    (tmp_path / "kvm.d" / "pending.json").write_text("{not json")
    assert kp.load(tmp_path) == {}
    kp.record(tmp_path, "node2", "web", 4, ["tpm"])
    assert json.loads((tmp_path / "kvm.d" / "pending.json").read_text()) == {"node2/web": {"id": 4, "fields": ["tpm"]}}
    assert oct((tmp_path / "kvm.d" / "pending.json").stat().st_mode & 0o777) == "0o600"


def test_the_vm_list_serves_pending_without_reading_any_xml(client, env, monkeypatch):
    def inventory(host, details=False):
        if host.name == "ghost":
            raise kh.KvmError("unreachable", "ghost: unreachable")
        return {"name": host.name, "vms": [vm("web", id_=12), vm("db", "shut off", None)], "errors": []}
    monkeypatch.setattr(kh, "host_inventory", inventory)
    monkeypatch.setattr(ke, "read_vm", lambda *a, **k: pytest.fail("no dumpxml from the list"))
    kp.record(env["state"], "node2", "web", 12, ["display.video"])
    kp.record(env["state"], "node2", "db", 9, ["tpm"])
    d = client.get("/api/kvm/vms?fresh=1").get_json()
    by = {v["name"]: v for v in d["vms"]}
    assert by["web"]["pending"] == ["display.video"] and by["db"]["pending"] == []
    assert sorted(kp.load(env["state"])) == ["node2/web"]           # le repère périmé est retiré


def test_the_edit_form_records_the_marker(client, env, monkeypatch):
    form = {"name": "web", "view": {}, "unmanaged": [], "pending": ["memory.max_mib"], "restart_required": True,
            "hotness": [], "config_digest": "c" * 64, "state": "running", "persistent": True,
            "autostart": False, "snapshots": 0, "id": 31}
    monkeypatch.setattr(ke, "vm_form", lambda host, name: dict(form))
    assert client.get("/api/kvm/vms/node2/web/form").status_code == 200
    assert kp.load(env["state"]) == {"node2/web": {"id": 31, "fields": ["memory.max_mib"]}}
    monkeypatch.setattr(ke, "vm_form", lambda host, name: dict(form, pending=[]))
    client.get("/api/kvm/vms/node2/web/form?again=1")
    assert kp.load(env["state"]) == {}


def test_the_script_records_the_marker_after_an_edit(tmp_path, monkeypatch):
    cli = wapp._kvm_cli
    monkeypatch.setattr(cli.ke, "vm_form", lambda host, name: {
        "state": "running", "config_digest": "d" * 64, "pending": ["display.video"], "id": 5})

    class H:
        name = "node2"
    out = cli.after_edit(H(), "web", tmp_path)
    assert out["pending"] == ["display.video"]
    assert kp.load(tmp_path) == {"node2/web": {"id": 5, "fields": ["display.video"]}}
