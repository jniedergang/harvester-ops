"""v1.57.0 : les listes des sections Storage, Security et Add-ons.

Les objets viennent de ce que harv1 (Harvester v1.9.0) a rendu : une image
`backingimage` et sa classe `lh-<uuid>`, les PVC portant l'annotation
`harvesterhci.io/imageId`, une VM qui porte `harvesterhci.io/sshNames` et un
secret cloud-init, des add-ons avec leurs statuts réels.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402
import cluster_objects as co  # noqa: E402

VM = {"kind": "VirtualMachine", "metadata": {"name": "web", "namespace": "default",
      "annotations": {"harvesterhci.io/sshNames": '["default/ops-key"]'}},
      "spec": {"template": {"spec": {"volumes": [
          {"name": "rootdisk", "persistentVolumeClaim": {"claimName": "web-rootdisk"}},
          {"name": "cloudinit", "cloudInitNoCloud": {"secretRef": {"name": "web-ci"},
                                                     "networkDataSecretRef": {"name": "web-ci"}}}]}}}}
PVC = {"kind": "PersistentVolumeClaim", "metadata": {"name": "web-rootdisk", "namespace": "default",
       "annotations": {"harvesterhci.io/imageId": "default/image-abc"}},
       "spec": {"storageClassName": "lh-1234"}}
IMAGE = {"kind": "VirtualMachineImage", "metadata": {"name": "image-abc", "namespace": "default",
         "creationTimestamp": "2026-07-20T07:40:11Z"},
         "spec": {"displayName": "leap.qcow2", "sourceType": "download", "url": "https://x/leap.qcow2",
                  "backend": "backingimage"},
         "status": {"progress": 100, "size": 723648512, "virtualSize": 10737418240,
                    "storageClassName": "lh-1234",
                    "conditions": [{"type": "Imported", "status": "True"},
                                   {"type": "RetryLimitExceeded", "status": "False"}]}}
SC = {"kind": "StorageClass", "metadata": {"name": "lh-1234"}, "provisioner": "driver.longhorn.io",
      "reclaimPolicy": "Delete", "volumeBindingMode": "Immediate", "allowVolumeExpansion": True,
      "parameters": {"numberOfReplicas": "1", "migratable": "true"}}
SC_DEFAULT = {"kind": "StorageClass", "metadata": {"name": "harv-rep1", "annotations": {
    "storageclass.kubernetes.io/is-default-class": "true"}}, "provisioner": "driver.longhorn.io",
    "parameters": {"numberOfReplicas": "1"}}
KEY = {"kind": "KeyPair", "metadata": {"name": "ops-key", "namespace": "default"},
       "spec": {"publicKey": "ssh-ed25519 AAAA ops"},
       "status": {"fingerPrint": "41:d0", "conditions": [{"type": "validated", "status": "True"}]}}
SECRET = {"kind": "Secret", "metadata": {"name": "web-ci", "namespace": "default"}, "type": "Opaque",
          "data": {"userdata": "c2VjcmV0LXZhbHVl", "networkdata": "bmV0"}}
SA_TOKEN = {"kind": "Secret", "metadata": {"name": "sa-token", "namespace": "default"},
            "type": "kubernetes.io/service-account-token", "data": {"token": "dG9r"}}
SYS_TLS = {"kind": "Secret", "metadata": {"name": "tls", "namespace": "cattle-system"},
           "type": "kubernetes.io/tls", "data": {"tls.crt": "eA=="}}
ADDON_ON = {"kind": "Addon", "metadata": {"name": "vm-import-controller", "namespace": "harvester-system"},
            "spec": {"chart": "harvester-vm-import-controller", "version": "1.9.0", "enabled": True},
            "status": {"status": "AddonDeploySuccessful"}}
ADDON_FAILED = {"kind": "Addon", "metadata": {"name": "descheduler", "namespace": "kube-system"},
                "spec": {"chart": "descheduler", "version": "0.36.0", "enabled": True},
                "status": {"status": "AddonDeployFailed", "conditions": [
                    {"type": "OperationFailed", "status": "True", "message": "chart not found"}]}}


# -- réductions pures -------------------------------------------------------------

def test_images_say_who_uses_them():
    [r] = co.images([IMAGE], [VM], [PVC])
    assert r["display_name"] == "leap.qcow2" and r["state"] == "ready"
    assert r["volumes"] == 1 and r["used_by"] == ["default/web"]
    assert r["virtual_size"] == 10737418240 and r["storage_class"] == "lh-1234"


def test_an_image_that_gave_up_says_why():
    bad = json.loads(json.dumps(IMAGE))
    bad["status"]["conditions"] = [{"type": "RetryLimitExceeded", "status": "True", "message": "404"}]
    assert co.images([bad])[0]["state"] == "failed" and co.images([bad])[0]["message"] == "404"
    bad["status"] = {"progress": 42}
    assert co.images([bad])[0]["state"] == "importing"


def test_storage_classes_count_volumes_and_name_their_image():
    rows = {r["name"]: r for r in co.storage_classes([SC, SC_DEFAULT], [PVC], [IMAGE])}
    assert rows["lh-1234"]["volumes"] == 1 and rows["lh-1234"]["image"]["display_name"] == "leap.qcow2"
    assert rows["lh-1234"]["replicas"] == "1" and rows["lh-1234"]["expansion"] is True
    assert rows["harv-rep1"]["is_default"] and rows["harv-rep1"]["image"] is None


def test_ssh_keys_say_which_vms_received_them():
    [r] = co.ssh_keys([KEY], [VM])
    assert r["validated"] and r["used_by"] == ["default/web"] and r["fingerprint"] == "41:d0"


def test_secrets_never_carry_their_values():
    rows, hidden = co.secrets([SECRET, SA_TOKEN, SYS_TLS], [VM])
    assert hidden == 2 and [r["name"] for r in rows] == ["web-ci"]
    assert rows[0]["keys"] == ["networkdata", "userdata"] and rows[0]["used_by"] == ["default/web"]
    blob = json.dumps(rows)
    assert "c2VjcmV0" not in blob and "bmV0" not in blob and "data" not in rows[0]
    rows, hidden = co.secrets([SECRET, SA_TOKEN, SYS_TLS], [VM], include_system=True)
    assert hidden == 0 and len(rows) == 3 and "dG9r" not in json.dumps(rows)


def test_addons_report_failures():
    rows = {r["name"]: r for r in co.addons([ADDON_ON, ADDON_FAILED])}
    assert rows["vm-import-controller"]["enabled"] and rows["vm-import-controller"]["message"] == ""
    assert rows["descheduler"]["message"] == "chart not found"


# -- routes -----------------------------------------------------------------------------

@pytest.fixture
def cluster(monkeypatch, tmp_path):
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "absent")
    monkeypatch.setattr(wapp, "load_config", lambda: {"clusters": [{"name": "harv1", "kubeconfig": "/kc"}]})
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc, **k: True)
    world = {"items": [IMAGE, VM, PVC, SC, SC_DEFAULT, KEY, SECRET, SA_TOKEN, ADDON_ON], "calls": [],
             "refuse": set()}

    def fake(kc, *args, timeout=15, cluster=None):
        kinds = args[1].split(",")
        world["calls"].append(kinds)
        refused = [k for k in kinds if k in world["refuse"]]
        if refused:
            wapp._note_cluster_denial(f'Error from server (Forbidden): x is forbidden: User "u" cannot list '
                                      f'resource "{refused[0]}" in API group "" at the cluster scope')
            return None
        wanted = {wapp._CO_KIND_OF[k] for k in kinds}
        return {"items": [i for i in world["items"] if i["kind"] in wanted]}
    monkeypatch.setattr(wapp, "_kubectl_json", fake)
    return world


def test_one_grouped_read_per_list(cluster):
    with wapp.app.test_client() as c:
        d = c.get("/api/cluster-objects/harv1/images").get_json()
    assert d["items"][0]["used_by"] == ["default/web"]
    assert cluster["calls"] == [["virtualmachineimages.harvesterhci.io", "virtualmachines.kubevirt.io",
                                 "persistentvolumeclaims"]]


def test_the_secrets_route_hides_system_secrets_and_values(cluster):
    with wapp.app.test_client() as c:
        d = c.get("/api/cluster-objects/harv1/secrets").get_json()
        assert [r["name"] for r in d["items"]] == ["web-ci"] and d["system_hidden"] == 1
        assert "c2VjcmV0" not in json.dumps(d)
        d = c.get("/api/cluster-objects/harv1/secrets?all=1").get_json()
        assert len(d["items"]) == 2 and "dG9r" not in json.dumps(d)


def test_a_refused_usage_read_keeps_the_list(cluster):
    """Un compte qui lit les images mais pas les VMs voit la liste, sans les
    VMs, et l'en-tête dit ce qui a été refusé."""
    cluster["refuse"].add("virtualmachines.kubevirt.io")
    with wapp.app.test_client() as c:
        r = c.get("/api/cluster-objects/harv1/images")
        d = r.get_json()
    assert r.status_code == 200 and d["items"][0]["display_name"] == "leap.qcow2"
    assert d["items"][0]["used_by"] == []
    assert json.loads(r.headers["X-Cluster-Denied"])[0]["resource"] == "virtualmachines.kubevirt.io"


def test_an_unknown_kind_is_refused(cluster):
    with wapp.app.test_client() as c:
        assert c.get("/api/cluster-objects/harv1/pods").status_code == 400
        assert c.get("/api/cluster-objects/nope/images").status_code == 404


def test_toggling_an_addon_goes_through_the_cli(cluster, monkeypatch):
    seen = {}

    def fake_action(cluster_, label, cmd, tool, spec=None, dry_run=False, after=None):
        seen.update(label=label, cmd=cmd, tool=tool)

        class Run:
            id = "abc123"
        return Run(), None
    monkeypatch.setattr(wapp, "_cli_action", fake_action)
    with wapp.app.test_client() as c:
        r = c.post("/api/addons/harv1/kube-system/descheduler", json={"enabled": True})
        assert r.status_code == 202 and r.get_json()["action_id"] == "abc123"
        assert c.post("/api/addons/harv1/kube-system/descheduler", json={"enabled": "yes"}).status_code == 400
    assert seen["tool"] == "harvester-resources" and seen["label"] == "addon:enable:kube-system/descheduler"
    assert seen["cmd"][1].endswith("harvester-resources.py")
    assert seen["cmd"][2:] == ["addon", "--kubeconfig", "/kc", "--namespace", "kube-system",
                               "--name", "descheduler", "--enable"]


def test_toggling_an_addon_needs_an_administrator():
    assert wapp.required_role_for("/api/addons/harv1/kube-system/descheduler", "POST") == "admin"
    assert wapp.required_role_for("/api/cluster-objects/harv1/addons", "GET") == "viewer"
