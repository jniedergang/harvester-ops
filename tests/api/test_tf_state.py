"""v1.54.0 : états par déclaration, reprise de l'état partagé, résumé de plan."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import tf_state as st  # noqa: E402


def res(t, n, module=None):
    r = {"mode": "managed", "type": t, "name": n, "provider": "p", "instances": [{"attributes": {"id": n}}]}
    if module:
        r["module"] = module
    return r


def state(*resources, serial=7):
    return {"version": 4, "terraform_version": "1.12.6", "serial": serial,
            "lineage": "old", "outputs": {}, "resources": list(resources)}


def write(ws, data):
    ws.mkdir(parents=True, exist_ok=True)
    (ws / st.STATE_FILE).write_text(json.dumps(data))


def test_addresses_are_the_managed_root_resources(tmp_path):
    write(tmp_path, state(res("harvester_virtualmachine", "web"), res("harvester_ssh_key", "k"),
                          res("x", "in_module", module="module.m"),
                          {"mode": "data", "type": "harvester_image", "name": "img"}))
    assert st.addresses(tmp_path) == ["harvester_ssh_key.k", "harvester_virtualmachine.web"]
    assert st.addresses(tmp_path / "missing") == []


def test_resources_move_to_the_declaration_state_with_backups(tmp_path):
    shared, decl = tmp_path / "harv1", tmp_path / "harv1" / "decls" / ("a" * 12)
    write(shared, state(res("harvester_virtualmachine", "web"), res("harvester_ssh_key", "k"),
                        res("harvester_image", "other")))
    moved = st.move(shared, decl, ["harvester_virtualmachine.web", "harvester_ssh_key.k", "harvester_image.absent"])
    assert moved == ["harvester_virtualmachine.web", "harvester_ssh_key.k"]
    assert st.addresses(shared) == ["harvester_image.other"]
    assert st.addresses(decl) == ["harvester_ssh_key.k", "harvester_virtualmachine.web"]
    new = json.loads((decl / st.STATE_FILE).read_text())
    assert new["version"] == 4 and new["serial"] == 1 and new["lineage"] != "old"
    assert json.loads((shared / st.STATE_FILE).read_text())["serial"] == 8
    assert list(shared.glob("terraform.tfstate.pre-move-*"))            # copie avant écriture
    # une seconde fois : plus rien à reprendre, rien ne bouge
    assert st.move(shared, decl, ["harvester_virtualmachine.web"]) == []


def test_an_address_already_in_the_destination_is_left_alone(tmp_path):
    shared, decl = tmp_path / "s", tmp_path / "d"
    write(shared, state(res("harvester_virtualmachine", "web")))
    write(decl, state(res("harvester_virtualmachine", "web"), serial=3))
    assert st.move(shared, decl, ["harvester_virtualmachine.web"]) == []
    assert st.addresses(shared) == ["harvester_virtualmachine.web"]


def show(*changes):
    return {"resource_changes": [dict(mode="managed", **c) for c in changes]}


def test_plan_summary_reads_actions_and_changed_settings():
    s = st.plan_summary(show(
        {"address": "harvester_image.leap", "type": "harvester_image", "name": "leap",
         "change": {"actions": ["create"], "before": None,
                    "after": {"name": "leap", "url": "https://x/leap.qcow2", "tags": None}}},
        {"address": "harvester_virtualmachine.web", "type": "harvester_virtualmachine", "name": "web",
         "change": {"actions": ["update"],
                    "before": {"memory": "4Gi", "cpu": 2, "disk": [{"size": "10Gi"}], "ssh": "k1"},
                    "after": {"memory": "8Gi", "cpu": 2, "disk": [{"size": "20Gi"}], "ssh": "k2"},
                    "before_sensitive": {"ssh": True}, "after_sensitive": {"ssh": True}}},
        {"address": "harvester_ssh_key.k", "type": "harvester_ssh_key", "name": "k",
         "change": {"actions": ["no-op"], "before": {}, "after": {}}},
        {"address": "harvester_virtualmachine.old", "type": "harvester_virtualmachine", "name": "old",
         "change": {"actions": ["delete", "create"], "before": {"image": "a"}, "after": {"image": "b"},
                    "replace_paths": [["disk", 0, "image"]]}},
        {"address": "harvester_ssh_key.gone", "type": "harvester_ssh_key", "name": "gone",
         "change": {"actions": ["delete"], "before": {"name": "gone"}, "after": None}}))
    assert s["counts"] == {"create": 1, "update": 1, "replace": 1, "delete": 1, "noop": 1}
    by = {c["address"]: c for c in s["changes"]}
    assert "harvester_ssh_key.k" not in by
    assert {"path": "url", "before": None, "after": "https://x/leap.qcow2"} in by["harvester_image.leap"]["fields"]
    upd = {f["path"]: f for f in by["harvester_virtualmachine.web"]["fields"]}
    assert upd["memory"] == {"path": "memory", "before": "4Gi", "after": "8Gi"}
    assert upd["disk[0].size"]["after"] == "20Gi"
    assert "ssh" not in upd                                  # masquée des deux côtés : égale
    assert by["harvester_virtualmachine.old"]["action"] == "replace"
    assert by["harvester_virtualmachine.old"]["replace_paths"] == ["disk/0/image"]


def test_sensitive_values_never_leave_the_summary():
    s = st.plan_summary(show(
        {"address": "a.b", "type": "a", "name": "b",
         "change": {"actions": ["update"], "before": {"password": "old"}, "after": {"password": "new"},
                    "before_sensitive": {"password": True}, "after_sensitive": {"password": True}}},
        {"address": "a.c", "type": "a", "name": "c",
         "change": {"actions": ["create"], "before": None, "after": {"token": "secret", "name": "c"},
                    "after_sensitive": {"token": True}}}))
    text = json.dumps(s)
    assert "old" not in text and "new" not in text and "secret" not in text
