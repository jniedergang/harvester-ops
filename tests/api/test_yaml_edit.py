"""v1.60.0 : « Edit YAML » et « Download YAML », comme dans Harvester.

Une liste fermée de types ; un Secret, un réglage et un add-on ne se lisent
qu'en administrateur (leur YAML porte des valeurs) ; un YAML modifié ne peut
pas viser un autre objet que celui ouvert ; l'écriture est une action suivie
par bin/harvester-resources.py yaml, qui garde le resourceVersion.
"""

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(ROOT / "web"))
import hv_yaml as hy  # noqa: E402

_spec = importlib.util.spec_from_file_location("hres_y", ROOT / "bin" / "harvester-resources.py")
hres = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hres)

VM = {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
      "metadata": {"name": "web", "namespace": "default", "resourceVersion": "42",
                   "managedFields": [{"manager": "x"}],
                   "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{}",
                                   "harvesterhci.io/vmRunStrategy": "RerunOnFailure"}},
      "spec": {"runStrategy": "RerunOnFailure"}, "status": {"ready": True}}


# -- la bibliothèque pure ---------------------------------------------------------

def test_clean_drops_status_and_history_but_keeps_the_version():
    c = hy.clean(VM)
    assert "status" not in c and "managedFields" not in c["metadata"]
    assert c["metadata"]["resourceVersion"] == "42"
    assert c["metadata"]["annotations"] == {"harvesterhci.io/vmRunStrategy": "RerunOnFailure"}
    assert VM["status"] == {"ready": True}               # l'original n'est pas touché


def test_an_edit_cannot_retarget_another_object():
    obj = json.loads(json.dumps(hy.clean(VM)))
    assert hy.check_target("vm", obj, "default", "web")["metadata"]["namespace"] == "default"
    for bad, msg in (({"name": "other"}, "renaming"), ({"namespace": "prod"}, "opened is in")):
        o = json.loads(json.dumps(obj))
        o["metadata"].update(bad)
        with pytest.raises(ValueError, match=msg):
            hy.check_target("vm", o, "default", "web")
    o = dict(obj, kind="VirtualMachineInstance")
    with pytest.raises(ValueError, match="VirtualMachine is expected"):
        hy.check_target("vm", o, "default", "web")
    o = dict(obj, apiVersion="harvesterhci.io/v1beta1")
    with pytest.raises(ValueError, match="apiVersion"):
        hy.check_target("vm", o, "default", "web")
    with pytest.raises(ValueError, match="List"):
        hy.check_target("vm", {"kind": "List", "items": []}, "default", "web")


def test_a_cluster_wide_object_has_no_namespace():
    sc = {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass", "metadata": {"name": "fast", "namespace": "x"}}
    with pytest.raises(ValueError, match="no namespace"):
        hy.check_target("storageclass", sc, "", "fast")


def test_creation_takes_the_namespace_from_the_yaml():
    img = {"apiVersion": "harvesterhci.io/v1beta1", "kind": "VirtualMachineImage",
           "metadata": {"generateName": "image-", "namespace": "lab"}, "spec": {}}
    assert hy.check_target("image", img, "", None, creating=True)["metadata"]["namespace"] == "lab"
    with pytest.raises(ValueError, match="namespace is required"):
        hy.check_target("image", {**img, "metadata": {"name": "a"}}, "", None, creating=True)


def test_a_cloud_template_is_a_labelled_configmap_only():
    cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "a", "namespace": "default"}}
    with pytest.raises(ValueError, match="cloud-init-template"):
        hy.check_target("cloudtemplate", cm, "default", "a")
    with pytest.raises(ValueError, match="not a cloud configuration template"):
        hy.check_readable("cloudtemplate", cm)


def test_who_reads_and_writes_what():
    assert hy.allowed("vm", "viewer") and not hy.allowed("vm", "viewer", write=True)
    assert hy.allowed("vm", "operator", write=True)
    for kind in ("secret", "setting", "addon"):             # leur YAML porte des valeurs
        assert not hy.allowed(kind, "operator") and hy.allowed(kind, "admin")
    assert hy.allowed("storageclass", "operator") and not hy.allowed("storageclass", "operator", write=True)
    for kind in hy.CONTAINER_KINDS:                         # v1.91.0 : lecture seule, même en admin
        assert hy.allowed(kind, "viewer") and not hy.allowed(kind, "admin", write=True)
    with pytest.raises(ValueError):
        hy.spec_of("widget")


# -- l'outil ------------------------------------------------------------------------

class FakeKube:
    def __init__(self, fail_real=None):
        self.calls = []
        self.fail_real = fail_real

    def run(self, *args, input=None, timeout=None):
        self.calls.append((args, json.loads(input) if input else None))
        if "--dry-run=server" not in args and self.fail_real:
            raise hres.KubeError(self.fail_real)
        return json.dumps({"metadata": {"resourceVersion": "43"}})


def run_tool(tmp_path, monkeypatch, obj, *extra, kube=None):
    f = tmp_path / "o.json"
    f.write_text(json.dumps(obj))
    kube = kube or FakeKube()
    monkeypatch.setattr(hres, "kube_from", lambda args: kube)
    code = hres.main(["yaml", "--kubeconfig", "/kc", "--file", str(f), *extra])
    return code, kube


def test_the_tool_checks_then_replaces(tmp_path, monkeypatch, capsys):
    code, kube = run_tool(tmp_path, monkeypatch, hy.clean(VM), "--kind", "vm", "--namespace", "default", "--name", "web")
    assert code == hres.EXIT_OK
    verbs = [(a[0], "--dry-run=server" in a) for a, _ in kube.calls]
    assert verbs == [("replace", True), ("replace", False)]
    assert kube.calls[1][1]["metadata"]["resourceVersion"] == "42"      # garde contre l'écrasement
    assert "STEP_EVENT|replace|done|default/web saved (version 43)" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["deployment", "pod", "cronjob"])
def test_the_tool_refuses_to_write_a_container_workload(tmp_path, monkeypatch, kind):
    """v1.91.0 : les charges de conteneurs sont en lecture seule, en ligne de
    commande comme dans la console."""
    obj = {"apiVersion": "apps/v1", "kind": kind.capitalize(), "metadata": {"name": "web", "namespace": "apps"}}
    code, kube = run_tool(tmp_path, monkeypatch, obj, "--kind", kind, "--namespace", "apps", "--name", "web")
    assert code != hres.EXIT_OK and kube.calls == []


def test_the_tool_refuses_another_target(tmp_path, monkeypatch, capsys):
    code, kube = run_tool(tmp_path, monkeypatch, hy.clean(VM), "--kind", "vm", "--namespace", "default", "--name", "api")
    assert code == hres.EXIT_BLOCKED and kube.calls == []
    assert "renaming is not an edit" in capsys.readouterr().err


def test_a_concurrent_change_is_said_plainly(tmp_path, monkeypatch, capsys):
    kube = FakeKube(fail_real='Operation cannot be fulfilled on virtualmachines.kubevirt.io "web": '
                              'the object has been modified; please apply your changes to the latest version')
    code, _ = run_tool(tmp_path, monkeypatch, hy.clean(VM), "--kind", "vm", "--namespace", "default", "--name", "web",
                       kube=kube)
    assert code == hres.EXIT_BLOCKED
    assert "someone changed this object since it was opened" in capsys.readouterr().err


def test_a_conflict_seen_at_the_check_is_said_plainly_too(tmp_path, monkeypatch, capsys):
    """Vu sur harv1 : le conflit sort dès l'essai à blanc (--dry-run=server)."""
    class Stale(FakeKube):
        def run(self, *args, input=None, timeout=None):
            self.calls.append((args, None))
            raise hres.KubeError('Error from server (Conflict): Operation cannot be fulfilled on '
                                 'virtualmachines.kubevirt.io "web": the object has been modified')
    code, kube = run_tool(tmp_path, monkeypatch, hy.clean(VM), "--kind", "vm", "--namespace", "default", "--name", "web",
                          kube=Stale())
    assert code == hres.EXIT_BLOCKED and len(kube.calls) == 1              # rien n'est tenté pour de vrai
    assert "someone changed this object since it was opened" in capsys.readouterr().err


def test_the_tool_creates_with_create(tmp_path, monkeypatch):
    img = {"apiVersion": "harvesterhci.io/v1beta1", "kind": "VirtualMachineImage",
           "metadata": {"generateName": "image-", "namespace": "default"}, "spec": {"url": "https://x"}}
    code, kube = run_tool(tmp_path, monkeypatch, img, "--kind", "image", "--create")
    assert code == hres.EXIT_OK and [a[0] for a, _ in kube.calls] == ["create", "create"]


def test_the_tool_reads_yaml_files(tmp_path, monkeypatch):
    f = tmp_path / "o.yaml"
    f.write_text("apiVersion: v1\nkind: Namespace\nmetadata:\n  name: lab\n")
    kube = FakeKube()
    monkeypatch.setattr(hres, "kube_from", lambda args: kube)
    assert hres.main(["yaml", "--kubeconfig", "/kc", "--file", str(f), "--kind", "namespace", "--name", "lab"]) == 0
    assert kube.calls[0][1] == {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "lab"}}


# -- les routes ---------------------------------------------------------------------

import accounts as acc  # noqa: E402
import app as wapp  # noqa: E402

PW = "a long enough password"


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(wapp, "AUTH_OPEN_ALLOWED", False)
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "none")
    monkeypatch.setattr(wapp, "ROLES_PATH", tmp_path / "none.yaml")
    monkeypatch.setattr(wapp, "_roles_cache", {"mtime": None, "data": None})
    monkeypatch.setattr(wapp, "ACCOUNTS_PATH", tmp_path / "accounts.json")
    monkeypatch.setattr(wapp, "_ACCOUNTS", {"store": None})
    monkeypatch.setattr(wapp, "_LOCAL_SESSIONS", acc.LocalSessions())
    monkeypatch.setattr(wapp, "load_config", lambda: {"clusters": [{"name": "harv1", "kubeconfig": "/kc"}]})
    for user, role in (("boss", "admin"), ("ops", "operator"), ("eye", "viewer")):
        wapp._accounts().create(user, PW, role)
    w = {"actions": [], "objects": {}}

    def fake_action(cluster_, label, cmd, tool, spec=None, dry_run=False, after=None):
        w["actions"].append((label, cmd, json.loads(Path(cmd[cmd.index("--file") + 1]).read_text())))

        class Run:
            id = "yaml00000001"
        return Run(), None
    monkeypatch.setattr(wapp, "_cli_action", fake_action)

    def fake_json(kc, *args, **k):
        return json.loads(json.dumps(w["objects"].get(args[2])))
    monkeypatch.setattr(wapp, "_kubectl_json", fake_json)
    w["objects"]["web"] = VM
    w["objects"]["creds"] = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "creds", "namespace": "default"},
                             "data": {"password": "c2VjcmV0"}}
    return w


def auth(user):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{PW}".encode()).decode()}


def test_reading_and_downloading_a_vm(world):
    with wapp.app.test_client() as c:
        d = c.get("/api/yaml/harv1/vm/web?namespace=default", headers=auth("eye")).get_json()
        assert "resourceVersion: '42'" in d["yaml"] and "status" not in d["yaml"] and "managedFields" not in d["yaml"]
        assert d["writable"] is False
        assert c.get("/api/yaml/harv1/vm/web?namespace=default", headers=auth("ops")).get_json()["writable"] is True
        r = c.get("/api/yaml/harv1/vm/web?namespace=default&download=1", headers=auth("eye"))
        assert r.status_code == 200 and 'filename="web.yaml"' in r.headers["Content-Disposition"]
        assert c.get("/api/yaml/harv1/vm/nope?namespace=default", headers=auth("eye")).status_code == 404
        assert c.get("/api/yaml/harv1/widget/web?namespace=default", headers=auth("eye")).status_code == 400
        assert c.get("/api/yaml/harv1/vm/web", headers=auth("eye")).status_code == 400      # sans namespace


def test_a_secret_yaml_is_for_administrators(world):
    with wapp.app.test_client() as c:
        assert c.get("/api/yaml/harv1/secret/creds?namespace=default", headers=auth("ops")).status_code == 403
        d = c.get("/api/yaml/harv1/secret/creds?namespace=default", headers=auth("boss")).get_json()
        assert "c2VjcmV0" in d["yaml"]


def test_saving_is_a_followed_action_on_the_same_object(world):
    import yaml
    with wapp.app.test_client() as c:
        text = c.get("/api/yaml/harv1/vm/web?namespace=default", headers=auth("ops")).get_json()["yaml"]
        edited = text.replace("RerunOnFailure", "Halted")
        assert c.put("/api/yaml/harv1/vm/web?namespace=default", json={"yaml": edited},
                     headers=auth("eye")).status_code == 403
        r = c.put("/api/yaml/harv1/vm/web?namespace=default", json={"yaml": edited}, headers=auth("ops"))
        assert r.status_code == 202, r.get_json()
        label, cmd, sent = world["actions"][-1]
        assert label == "yaml:replace:vm:default/web" and cmd[2] == "yaml"
        assert sent["spec"]["runStrategy"] == "Halted" and sent["metadata"]["resourceVersion"] == "42"
        # un autre nom, deux objets, du YAML cassé : refusés avant tout envoi
        other = yaml.safe_load(edited)
        other["metadata"]["name"] = "api"
        for body in (yaml.safe_dump(other), edited + "\n---\n" + edited, "a: [", ""):
            assert c.put("/api/yaml/harv1/vm/web?namespace=default", json={"yaml": body},
                         headers=auth("ops")).status_code == 400
    assert len(world["actions"]) == 1


def test_creating_from_yaml(world):
    img = "apiVersion: harvesterhci.io/v1beta1\nkind: VirtualMachineImage\nmetadata:\n  generateName: image-\n  namespace: default\nspec:\n  url: https://x\n"
    with wapp.app.test_client() as c:
        r = c.post("/api/yaml/harv1/image", json={"yaml": img}, headers=auth("ops"))
        assert r.status_code == 202, r.get_json()
        assert c.post("/api/yaml/harv1/storageclass", json={"yaml": "apiVersion: storage.k8s.io/v1\nkind: StorageClass\nmetadata:\n  name: f\n"},
                      headers=auth("ops")).status_code == 403
    assert "--create" in world["actions"][-1][1]


def test_a_long_object_name_is_accepted(world):
    """Vu sur harv1 : un PVC système de 110 caractères (sous-domaine DNS, pas
    une étiquette de 63) était refusé par le contrôle des chemins."""
    long = "harv1-to-v180-upgradelog-infra-log-archive-harv1-to-v180-upgradelog-infra-fluentd-0"
    world["objects"][long] = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                              "metadata": {"name": long, "namespace": "harvester-system"}, "spec": {}}
    with wapp.app.test_client() as c:
        r = c.get(f"/api/yaml/harv1/volume/{long}?namespace=harvester-system", headers=auth("eye"))
        assert r.status_code == 200 and long in r.get_json()["yaml"]
        assert c.get("/api/yaml/harv1/volume/Bad_Name?namespace=default", headers=auth("eye")).status_code == 400
        assert c.get("/api/yaml/harv1/volume/" + "a" * 254 + "?namespace=default", headers=auth("eye")).status_code == 400
