#!/usr/bin/env python3
"""Pose (ou retire) les VMs de démonstration du banc harvlab.

Les vidéos doivent montrer des machines aux noms parlants, pas les restes
d'un essai. Ce script les crée PAR L'API DE LA CONSOLE, comme le ferait un
utilisateur : même chemin de code, donc la démonstration ne peut pas
diverger du produit.

    ./seed.py up      # web-01, db-01, cache-01, api-01
    ./seed.py down    # les efface
    ./seed.py status
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8095"
CLUSTER = "harvlab"
NAMESPACE = "default"
IMAGE = "default/cirros"
LABEL = "demo-video"
VMS = [("web-01", 1, "1Gi"), ("db-01", 1, "2Gi"), ("cache-01", 1, "1Gi"), ("api-01", 1, "1Gi")]

CLOUD_INIT = """#cloud-config
password: demo
chpasswd: { expire: False }
ssh_pwauth: True
"""


def api(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def image_storage_class():
    """Jamais fabriquer `longhorn-<image>` : les images récentes portent un
    nom en `lh-<uuid>`. On lit celui que l'image déclare."""
    out = subprocess.run(
        ["kubectl", "--kubeconfig", f"{__import__('os').path.expanduser('~')}/.kube/harvlab.yaml",
         "get", "virtualmachineimage", "-n", "default", "cirros",
         "-o", "jsonpath={.status.storageClassName}"],
        capture_output=True, text=True, check=True).stdout.strip()
    if not out:
        sys.exit("l'image cirros n'a pas encore de storage class")
    return out


def manifest(name, cores, memory, sc):
    claim = f"{name}-disk-0"
    return {
        "apiVersion": "kubevirt.io/v1",
        "kind": "VirtualMachine",
        "metadata": {
            "name": name, "namespace": NAMESPACE,
            "labels": {"app.kubernetes.io/managed-by": LABEL},
            "annotations": {
                "harvesterhci.io/volumeClaimTemplates": json.dumps([{
                    "metadata": {"name": claim,
                                 "annotations": {"harvesterhci.io/imageId": IMAGE}},
                    "spec": {"accessModes": ["ReadWriteMany"],
                             "resources": {"requests": {"storage": "1Gi"}},
                             "volumeMode": "Block", "storageClassName": sc},
                }]),
            },
        },
        "spec": {
            "runStrategy": "Always",
            "template": {
                "metadata": {"labels": {"harvesterhci.io/vmName": name,
                                        "app.kubernetes.io/managed-by": LABEL}},
                "spec": {
                    "hostname": name,
                    # Sans elle, la maintenance ARRÊTE la VM au lieu de la
                    # migrer : c'est tout le sujet de la vidéo.
                    "evictionStrategy": "LiveMigrateIfPossible",
                    "domain": {
                        "cpu": {"cores": cores, "sockets": 1, "threads": 1},
                        "memory": {"guest": memory},
                        "resources": {"limits": {"cpu": str(cores), "memory": memory}},
                        "devices": {
                            "disks": [
                                {"name": "disk-0", "disk": {"bus": "virtio"}, "bootOrder": 1},
                                {"name": "cloudinit", "disk": {"bus": "virtio"}},
                            ],
                            "interfaces": [{"name": "default", "masquerade": {}, "model": "virtio"}],
                        },
                    },
                    "networks": [{"name": "default", "pod": {}}],
                    "volumes": [
                        {"name": "disk-0", "persistentVolumeClaim": {"claimName": claim}},
                        {"name": "cloudinit", "cloudInitNoCloud": {"userData": CLOUD_INIT}},
                    ],
                },
            },
        },
    }


def up():
    sc = image_storage_class()
    for name, cores, memory in VMS:
        r = api("POST", f"/api/vms/{CLUSTER}/create",
                {"namespace": NAMESPACE, "name": name, "count": 1, "start": True,
                 "manifest": manifest(name, cores, memory, sc)})
        print(f"{name} : action {r.get('action_id')}")
    print("créées ; elles démarrent (cirros, quelques dizaines de secondes)")


def down():
    for name, _, _ in VMS:
        try:
            api("DELETE", f"/api/vm/{CLUSTER}/{NAMESPACE}/{name}")
            print(f"{name} : suppression demandée")
        except Exception as e:                                  # noqa: BLE001
            print(f"{name} : {e}")


def status():
    d = api("GET", f"/api/topology/{CLUSTER}")
    by_node = {}
    for v in d.get("vms", []):
        by_node.setdefault(v.get("node") or "-", []).append(f"{v['name']}({v.get('phase')})")
    for node in sorted(by_node):
        print(f"{node} : {', '.join(sorted(by_node[node]))}")


def wait_running(timeout=600):
    names = {n for n, _, _ in VMS}
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = api("GET", f"/api/topology/{CLUSTER}")
        running = {v["name"] for v in d.get("vms", [])
                   if v["name"] in names and v.get("phase") == "Running" and v.get("node")}
        if running >= names:
            print("toutes en marche")
            return True
        time.sleep(10)
    print("toujours pas toutes en marche")
    return False


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("what", choices=("up", "down", "status", "wait"))
    what = ap.parse_args().what
    {"up": up, "down": down, "status": status, "wait": wait_running}[what]()
