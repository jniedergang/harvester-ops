#!/usr/bin/env python3
"""Banc de charge : des clusters Kubernetes simulés par kwok, peuplés comme un
Harvester (VMs, VMIs, images, volumes, réseaux, objets Longhorn), pour mesurer
ce que coûte la console avec beaucoup de clusters et de VMs.

Rien ne tourne vraiment (pas de VM, pas de nœud) : seuls le serveur d'API et
etcd sont réels. C'est ce que la console interroge ; les gestes qui agissent
sur des machines ne sont pas couverts.

  kwok_bench.py templates --from ~/.kube/harvester.yaml   # CRD et objets modèles, lus sur un vrai cluster
  kwok_bench.py up 30 --vms 200                         # 30 clusters de 200 VMs, config.yaml de la console
  kwok_bench.py down                                    # tout supprimer

kwok et kwokctl : https://github.com/kubernetes-sigs/kwok (binaires), posés dans
$KWOK_BENCH_DIR/bin. Environ 500 Mo de mémoire par cluster simulé.
"""
import argparse
import copy
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BENCH = Path(os.environ.get("KWOK_BENCH_DIR", str(Path.home() / ".local/share/kwok-bench")))
KWOKCTL = BENCH / "bin" / "kwokctl"
TPL = BENCH / "templates"
ENV = {**os.environ, "KWOK_WORKDIR": str(BENCH / "work")}
CRDS = ["virtualmachines.kubevirt.io", "virtualmachineinstances.kubevirt.io",
        "virtualmachineimages.harvesterhci.io", "network-attachment-definitions.k8s.cni.cncf.io",
        "nodes.longhorn.io", "settings.longhorn.io", "volumes.longhorn.io", "settings.harvesterhci.io"]
# un modèle de chaque, pris sur le vrai cluster
SAMPLES = {"vm": "virtualmachines.kubevirt.io", "vmi": "virtualmachineinstances.kubevirt.io",
           "image": "virtualmachineimages.harvesterhci.io", "pvc": "persistentvolumeclaims",
           "nad": "network-attachment-definitions.k8s.cni.cncf.io", "lhnode": "nodes.longhorn.io",
           "lhvol": "volumes.longhorn.io"}
NAMESPACES = 10


def sh(argv, **kw):
    return subprocess.run(argv, check=True, capture_output=True, text=True, env=ENV, **kw)


def clean(obj):
    md = obj.get("metadata", {})
    for k in ("uid", "resourceVersion", "creationTimestamp", "generation", "managedFields",
              "ownerReferences", "finalizers", "selfLink"):
        md.pop(k, None)
    (md.get("annotations") or {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
    return obj


def cmd_templates(a):
    TPL.mkdir(parents=True, exist_ok=True)
    kc = ["kubectl", "--kubeconfig", os.path.expanduser(a.source)]
    crds = []
    for c in CRDS:
        d = clean(json.loads(sh(kc + ["get", "crd", c, "-o", "json"]).stdout))
        d.pop("status", None)
        d["spec"].pop("conversion", None)
        crds.append(d)
    (TPL / "crds.json").write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": crds}))
    out = {}
    for key, kind in SAMPLES.items():
        items = json.loads(sh(kc + ["get", kind, "-A", "-o", "json"]).stdout)["items"]
        if not items:
            sys.exit(f"no {kind} on the source cluster to use as a template")
        out[key] = clean(items[0])
    # les données utilisateur (cloud-init) ne quittent pas le cluster source
    for v in out["vm"]["spec"]["template"]["spec"].get("volumes", []):
        for k in ("cloudInitNoCloud", "cloudInitConfigDrive"):
            if k in v:
                v[k] = {"userData": "#cloud-config\n"}
    (TPL / "objects.json").write_text(json.dumps(out))
    print(f"templates written to {TPL}")


def objects_for(cluster, vms):
    t = json.loads((TPL / "objects.json").read_text())
    items = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": f"ns-{i}"}}
             for i in range(NAMESPACES)]
    items.append({"apiVersion": "v1", "kind": "Node", "metadata": {
        "name": f"{cluster}-node1", "annotations": {"kwok.x-k8s.io/node": "fake"},
        "labels": {"node-role.kubernetes.io/control-plane": "true", "type": "kwok"}},
        "status": {"allocatable": {"cpu": "32", "memory": "256Gi", "pods": "250"},
                   "capacity": {"cpu": "32", "memory": "256Gi", "pods": "250"}}})
    items.append({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "longhorn-system"}})
    ln = copy.deepcopy(t["lhnode"])
    ln["metadata"].update(name=f"{cluster}-node1", namespace="longhorn-system")
    items.append(ln)
    for i in range(NAMESPACES):
        nad = copy.deepcopy(t["nad"])
        nad["metadata"].update(name="vlan-net", namespace=f"ns-{i}")
        items.append(nad)
        img = copy.deepcopy(t["image"])
        img["metadata"].update(name="image-base", namespace=f"ns-{i}")
        items.append(img)
    for n in range(vms):
        ns = f"ns-{n % NAMESPACES}"
        name = f"vm-{n:04d}"
        vm = copy.deepcopy(t["vm"])
        vm["metadata"].update(name=name, namespace=ns)
        vm["metadata"].setdefault("labels", {})["harvesterhci.io/vmName"] = name
        vm.pop("status", None)
        items.append(vm)
        vmi = copy.deepcopy(t["vmi"])
        vmi["metadata"].update(name=name, namespace=ns)
        items.append(vmi)
        pvc = copy.deepcopy(t["pvc"])
        pvc["metadata"].update(name=f"{name}-disk-0", namespace=ns)
        pvc["spec"].pop("volumeName", None)
        items.append(pvc)
        vol = copy.deepcopy(t["lhvol"])
        vol["metadata"].update(name=f"pvc-{cluster}-{n:04d}", namespace="longhorn-system")
        items.append(vol)
    return items


def create_one(name):
    """Une création à la fois : kwokctl en parallèle se trompe d'autorité de
    certification (vu : certificat signé par une autre `kwok-ca`)."""
    if not (BENCH / "work" / "clusters" / name).exists():
        sh([str(KWOKCTL), "create", "cluster", "--name", name, "--runtime", "binary",
            "--disable", "kube-scheduler", "--wait", "3m"])


def up_one(name, vms):
    kc = BENCH / f"kc-{name}.yaml"
    kc.write_text(sh([str(KWOKCTL), "get", "kubeconfig", "--name", name]).stdout)
    kubectl = ["kubectl", "--kubeconfig", str(kc)]
    subprocess.run(kubectl + ["apply", "--server-side", "-f", str(TPL / "crds.json")],
                   check=True, capture_output=True, env=ENV)
    sh(kubectl + ["wait", "--for=condition=Established", "crd", "--all", "--timeout=120s"])
    data = json.dumps({"apiVersion": "v1", "kind": "List", "items": objects_for(name, vms)})
    r = subprocess.run(kubectl + ["apply", "--server-side", "-f", "-"], input=data,
                       capture_output=True, text=True, env=ENV)
    if r.returncode != 0:
        raise RuntimeError(f"{name}: {r.stderr.strip()[-400:]}")
    return name, kc


def cmd_up(a):
    names = [f"kb{i:03d}" for i in range(1, a.clusters + 1)]
    for n in names:
        create_one(n)
    with ThreadPoolExecutor(max_workers=a.parallel) as pool:
        done = list(pool.map(lambda n: up_one(n, a.vms), names))
    cfg = {"web": {"bind_host": "127.0.0.1", "bind_port": a.port},
           "settings": {"log_dir": str(BENCH / "console-logs")},
           "clusters": [{"name": n, "description": f"kwok {a.vms} VMs", "kubeconfig": str(kc),
                         "ssh": {"user": "rancher", "port": 22},
                         "nodes": [{"hostname": f"{n}-node1", "ip": "127.0.0.1", "role": "control-plane"}]}
                        for n, kc in done]}
    import yaml
    (BENCH / "config.yaml").write_text(yaml.safe_dump(cfg))
    print(f"{len(done)} clusters of {a.vms} VMs; console config: {BENCH / 'config.yaml'}")


def cmd_down(a):
    out = sh([str(KWOKCTL), "get", "clusters"]).stdout.split()
    for n in out:
        if n.startswith("kb"):
            sh([str(KWOKCTL), "delete", "cluster", "--name", n])
    print(f"{len([n for n in out if n.startswith('kb')])} clusters deleted")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    t = sp.add_parser("templates")
    t.add_argument("--from", dest="source", default="~/.kube/harvester.yaml")
    u = sp.add_parser("up")
    u.add_argument("clusters", type=int)
    u.add_argument("--vms", type=int, default=100)
    u.add_argument("--parallel", type=int, default=6)
    u.add_argument("--port", type=int, default=8135)
    sp.add_parser("down")
    a = p.parse_args()
    {"templates": cmd_templates, "up": cmd_up, "down": cmd_down}[a.cmd](a)


if __name__ == "__main__":
    main()
