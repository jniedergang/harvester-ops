#!/usr/bin/env python3
"""Cluster de démonstration simulé avec des GPU (kwok), pour évaluer
l'inventaire GPU et la réservation dans la vraie interface, sans matériel.

    gpu_demo.py prepare --from ~/.kube/harvester.yaml   # CRD lues sur un vrai Harvester
    gpu_demo.py up                                      # crée le cluster « gpudemo » et le déclare
    gpu_demo.py down                                    # le supprime et retire sa déclaration

Le cluster porte les CRD de pcidevices (PCIDevice, SRIOVGPUDevice,
VGPUDevice, MigConfiguration), l'add-on pcidevices-controller activé, et :

- gpu-n1 : deux H100 ; l'un passé en entier à la VM prod/train, l'autre
  découpé en quatre vGPU NVIDIA_H100-20C (deux configurés, dont un tenu par
  la VM ml/notebook, deux désactivés) ;
- gpu-n2 : un A100 partitionné en MIG (deux 3g.40gb créés, un tenu par la VM
  ml/big par son vGPU adossé à MIG) ;
- gpu-n3 : un nœud qui sert quatre GPU aux conteneurs
  (`harvesterhci.io/gpu-baremetal-workloads=true`), dont deux pris par le
  Deployment ml/trainer.

Les formes des objets suivent harvester/pcidevices (types relevés le
18/10/2026, voir docs/design/2026-10-18-conteneurs-et-inventaire-gpu.md).
La déclaration va dans l'état de la console de dev (`clusters.d/`), jamais
dans le config.yaml de l'exploitant.
"""
import argparse
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

BENCH = Path(os.environ.get("KWOK_BENCH", Path.home() / ".local/share/kwok-bench"))
KWOKCTL = BENCH / "bin" / "kwokctl"
TPL = BENCH / "templates"
STATE = Path(os.environ.get("HARVESTER_OPS_STATE_DIR", Path.home() / ".local/share/harvester-ops"))
NAME = "gpudemo"
ENV = dict(os.environ, PATH=f"{BENCH / 'bin'}:{os.environ.get('PATH', '')}",
           KWOK_WORKDIR=str(BENCH / "work"))
DEVICE_CRDS = ("pcidevices.devices.harvesterhci.io", "pcideviceclaims.devices.harvesterhci.io",
               "sriovgpudevices.devices.harvesterhci.io", "vgpudevices.devices.harvesterhci.io",
               "migconfigurations.devices.harvesterhci.io", "addons.harvesterhci.io")
H100 = ("10de", "2330", "0302", "NVIDIA Corporation GH100 [H100 PCIe]", "nvidia.com/GH100_H100_PCIE")
A100 = ("10de", "20b5", "0302", "NVIDIA Corporation GA100 [A100 PCIe 80GB]", "nvidia.com/GA100_A100_PCIE_80GB")
ALLOC = "harvesterhci.io/deviceAllocationDetails"


def sh(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, env=ENV, **kw)
    if r.returncode != 0:
        raise SystemExit(f"{' '.join(map(str, cmd[:3]))}: {r.stderr.strip()[-400:]}")
    return r.stdout


def clean(obj):
    m = obj["metadata"]
    obj["metadata"] = {k: m[k] for k in ("name", "labels", "annotations") if k in m}
    obj.pop("status", None)
    return obj


def cmd_prepare(a):
    TPL.mkdir(parents=True, exist_ok=True)
    out = []
    for crd in DEVICE_CRDS:
        out.append(clean(json.loads(sh(["kubectl", "--kubeconfig", a.src, "get", "crd", crd, "-o", "json"]))))
    (TPL / "gpu-crds.json").write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": out}))
    print(f"{len(out)} CRDs written to {TPL / 'gpu-crds.json'}")


def pci(node, addr, card):
    vendor, device, cls, desc, res = card
    return {"apiVersion": "devices.harvesterhci.io/v1beta1", "kind": "PCIDevice",
            "metadata": {"name": f"{node}-{addr.replace(':', '').replace('.', '')}"},
            "status": {"address": addr, "vendorId": vendor, "deviceId": device, "classId": cls,
                       "iommuGroup": "40", "nodeName": node, "resourceName": res, "description": desc,
                       "kernelDriverInUse": "vfio-pci"}}


def claim(name, node, addr):
    return {"apiVersion": "devices.harvesterhci.io/v1beta1", "kind": "PCIDeviceClaim",
            "metadata": {"name": name},
            "spec": {"address": addr, "nodeName": node, "userName": "admin"},
            "status": {"kernelDriverToUnbind": "nvidia", "passthroughEnabled": True}}


def vgpu(node, addr, parent_addr, vtype, configured):
    parent = f"{node}-{parent_addr.replace(':', '').replace('.', '')}"
    return {"apiVersion": "devices.harvesterhci.io/v1beta1", "kind": "VGPUDevice",
            "metadata": {"name": f"{node}-{addr.replace(':', '').replace('.', '')}",
                         "labels": {"harvesterhci.io/parentSRIOVGPUDevice": parent}},
            "spec": {"vGPUTypeName": vtype if configured else "", "address": addr, "enabled": configured,
                     "nodeName": node, "parentGPUDeviceAddress": parent_addr},
            "status": {"vGPUStatus": "vGPUConfigured" if configured else "",
                       "configureVGPUTypeName": vtype if configured else "",
                       "availableTypes": {vtype: "nvidia-1"}}}


def node(name, gpus=None, label=False):
    labels = {"kubernetes.io/hostname": name, "type": "kwok"}
    if label:
        labels["harvesterhci.io/gpu-baremetal-workloads"] = "true"
    res = {"cpu": "64", "memory": "512Gi", "pods": "250"}
    res.update(gpus or {})
    return {"apiVersion": "v1", "kind": "Node",
            "metadata": {"name": name, "labels": labels, "annotations": {"kwok.x-k8s.io/node": "fake"}},
            "status": {"allocatable": res, "capacity": res}}


def vm_pair(t, ns, name, devices, alloc):
    vm = copy.deepcopy(t["vm"])
    vm["metadata"] = {"name": name, "namespace": ns, "labels": {"harvesterhci.io/vmName": name},
                      "annotations": {ALLOC: json.dumps(alloc)}}
    vm.pop("status", None)
    dom = vm["spec"]["template"]["spec"]["domain"]
    dom.setdefault("devices", {}).update(devices)
    vmi = copy.deepcopy(t["vmi"])
    vmi["metadata"] = {"name": name, "namespace": ns}
    return [vm, vmi]


def objects():
    t = json.loads((TPL / "objects.json").read_text())
    items = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": n}}
             for n in ("harvester-system", "ml", "prod")]
    items.append({"apiVersion": "harvesterhci.io/v1beta1", "kind": "Addon",
                  "metadata": {"name": "pcidevices-controller", "namespace": "harvester-system"},
                  "spec": {"enabled": True, "chart": "harvester-pcidevices-controller", "repo": "", "version": "",
                           "valuesContent": ""}})
    items += [node("gpu-n1", {H100[4]: "1", "nvidia.com/NVIDIA_H100-20C": "2"}),
              node("gpu-n2", {"nvidia.com/NVIDIA_A100-3-40C": "2"}),
              node("gpu-n3", {"nvidia.com/gpu": "4"}, label=True)]
    # gpu-n1 : un H100 en passthrough, un découpé en vGPU
    items += [pci("gpu-n1", "0000:3b:00.0", H100), claim("gpu-n1-00003b000", "gpu-n1", "0000:3b:00.0"),
              pci("gpu-n1", "0000:af:00.0", H100)]
    items.append({"apiVersion": "devices.harvesterhci.io/v1beta1", "kind": "SRIOVGPUDevice",
                  "metadata": {"name": "gpu-n1-0000af000"},
                  "spec": {"address": "0000:af:00.0", "nodeName": "gpu-n1", "enabled": True},
                  "status": {"vfAddresses": [f"0000:af:00.{i}" for i in range(4, 8)],
                             "vGPUDevices": [f"gpu-n1-0000af00{i}" for i in range(4, 8)]}})
    for i, conf in zip(range(4, 8), (True, True, False, False)):
        items.append(vgpu("gpu-n1", f"0000:af:00.{i}", "0000:af:00.0", "NVIDIA H100-20C", conf))
    # gpu-n2 : un A100 partitionné en MIG, deux vGPU adossés
    items.append(pci("gpu-n2", "0000:17:00.0", A100))
    items.append({"apiVersion": "devices.harvesterhci.io/v1beta1", "kind": "SRIOVGPUDevice",
                  "metadata": {"name": "gpu-n2-000017000"},
                  "spec": {"address": "0000:17:00.0", "nodeName": "gpu-n2", "enabled": True},
                  "status": {"vfAddresses": ["0000:17:00.4", "0000:17:00.5"],
                             "vGPUDevices": ["gpu-n2-000017004", "gpu-n2-000017005"]}})
    for i in (4, 5):
        items.append(vgpu("gpu-n2", f"0000:17:00.{i}", "0000:17:00.0", "NVIDIA A100-3-40C", True))
    items.append({"apiVersion": "devices.harvesterhci.io/v1beta1", "kind": "MigConfiguration",
                  "metadata": {"name": "gpu-n2-000017000"},
                  "spec": {"enabled": True, "gpuAddress": "0000:17:00.0", "nodeName": "gpu-n2",
                           "profileSpec": [{"name": "MIG 3g.40gb", "id": 9, "requested": 2},
                                           {"name": "MIG 1g.10gb", "id": 19, "requested": 0}]},
                  "status": {"status": "synced",
                             "profileStatus": [{"name": "MIG 3g.40gb", "id": 9, "available": 0, "total": 2,
                                                "vGPUID": ["1", "2"]},
                                               {"name": "MIG 1g.10gb", "id": 19, "available": 7, "total": 7}]}})
    # VM consommatrices (spec + annotation d'allocation, VMI en marche)
    items += vm_pair(t, "prod", "train", {"hostDevices": [{"name": "h100", "deviceName": H100[4]}]},
                     {"hostdevices": {H100[4]: ["gpu-n1-00003b000"]}})
    items += vm_pair(t, "ml", "notebook", {"gpus": [{"name": "vgpu1", "deviceName": "nvidia.com/NVIDIA_H100-20C"}]},
                     {"gpus": {"nvidia.com/NVIDIA_H100-20C": ["gpu-n1-0000af004"]}})
    items += vm_pair(t, "ml", "big", {"gpus": [{"name": "mig1", "deviceName": "nvidia.com/NVIDIA_A100-3-40C"}]},
                     {"gpus": {"nvidia.com/NVIDIA_A100-3-40C": ["gpu-n2-000017004"]}})
    # conteneurs : deux GPU pris sur gpu-n3
    items.append({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "trainer", "namespace": "ml"},
                  "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "trainer"}},
                           "template": {"metadata": {"labels": {"app": "trainer"}},
                                        "spec": {"nodeName": "gpu-n3", "containers": [{
                                            "name": "trainer", "image": "registry.suse.com/bci/python:3.11",
                                            "resources": {"limits": {"nvidia.com/gpu": "2", "memory": "8Gi"},
                                                          "requests": {"nvidia.com/gpu": "2", "memory": "8Gi"}}}]}}}})
    return items


def cmd_up(a):
    if not (TPL / "gpu-crds.json").exists():
        raise SystemExit("run `gpu_demo.py prepare --from <kubeconfig of a Harvester>` first")
    if not (BENCH / "work" / "clusters" / NAME).exists():
        sh([str(KWOKCTL), "create", "cluster", "--name", NAME, "--runtime", "binary", "--wait", "3m"])
    kc = STATE / "kubeconfigs" / f"{NAME}.yaml"
    kc.parent.mkdir(parents=True, exist_ok=True)
    kc.write_text(sh([str(KWOKCTL), "get", "kubeconfig", "--name", NAME]))
    kc.chmod(0o600)
    kubectl = ["kubectl", "--kubeconfig", str(kc)]
    for f in ("crds.json", "gpu-crds.json"):
        sh(kubectl + ["apply", "--server-side", "-f", str(TPL / f)])
    sh(kubectl + ["wait", "--for=condition=Established", "crd", "--all", "--timeout=120s"])
    items = objects()
    statuses = [o for o in items if "status" in o and o["kind"] not in ("Node",)]
    sh(kubectl + ["apply", "--server-side", "-f", "-"],
       input=json.dumps({"apiVersion": "v1", "kind": "List", "items": items}))
    for o in statuses:                       # le statut passe par la sous-ressource quand elle existe
        ns = ["-n", o["metadata"]["namespace"]] if o["metadata"].get("namespace") else []
        kind = o["kind"].lower()
        subprocess.run(kubectl + ["patch", kind, o["metadata"]["name"], *ns, "--subresource=status",
                                  "--type=merge", "-p", json.dumps({"status": o["status"]})],
                       capture_output=True, text=True, env=ENV)
    decl = STATE / "clusters.d" / f"{NAME}.yaml"
    decl.parent.mkdir(parents=True, exist_ok=True)
    decl.write_text(f"name: {NAME}\ndescription: simulated GPU cluster (kwok bench, no real hardware)\n"
                    f"kubeconfig: kubeconfigs/{NAME}.yaml\nnodes:\n"
                    + "".join(f"- hostname: gpu-n{i}\n  ip: 127.0.0.1\n  role: control-plane\n" for i in (1, 2, 3)))
    print(f"{NAME} up and declared ({decl})")


def cmd_down(a):
    subprocess.run([str(KWOKCTL), "delete", "cluster", "--name", NAME], capture_output=True, env=ENV)
    for p in (STATE / "clusters.d" / f"{NAME}.yaml", STATE / "kubeconfigs" / f"{NAME}.yaml"):
        p.unlink(missing_ok=True)
    print(f"{NAME} removed")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("prepare")
    p.add_argument("--from", dest="src", required=True)
    sp.add_parser("up")
    sp.add_parser("down")
    a = ap.parse_args()
    {"prepare": cmd_prepare, "up": cmd_up, "down": cmd_down}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
