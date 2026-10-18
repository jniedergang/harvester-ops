"""v1.93.0 : les gestes de bin/harvester-containers.py, avec un faux kubectl.

Le faux kubectl répond depuis un état JSON (le « kubeconfig » EST ce
fichier) et journalise chaque appel avec ce qu'il reçoit sur stdin. On
vérifie : contrôle à blanc du serveur AVANT chaque écriture, aucune écriture
quand la demande est refusée ou que le contrôle échoue, namespaces système
refusés avant le cluster, namespace créé seulement sur demande (et retiré si
la charge est refusée), progression en STEP_EVENT, et jamais le chemin du
kubeconfig dans ce que le script dit."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / "bin" / "harvester-containers.py"

FAKE_KUBECTL = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
i = args.index("--kubeconfig")
kc = args[i + 1]
rest = args[:i] + args[i + 2:]
stdin = sys.stdin.read() if "-f" in rest else ""
with open(os.environ["FAKE_KUBECTL_LOG"], "a") as f:
    f.write(json.dumps({"args": rest, "stdin": stdin}) + "\n")
state = json.load(open(kc))
dry = "--dry-run=server" in rest
if rest[:2] == ["get", "namespace"]:
    if state.get("ns_error"):
        sys.stderr.write(state["ns_error"] + "\n"); sys.exit(1)
    if rest[2] in state.get("namespaces", []):
        print("namespace/" + rest[2]); sys.exit(0)
    sys.stderr.write('Error from server (NotFound): namespaces "%s" not found\n' % rest[2]); sys.exit(1)
verb = rest[0]
what = "namespace" if rest[:2] == ["create", "namespace"] else verb
key = ("dry_fail" if dry else "write_fail")
fails = state.get(key, {})
if what in fails:
    sys.stderr.write(fails[what].replace("@KC@", kc) + "\n"); sys.exit(1)
print("ok"); sys.exit(0)
'''


@pytest.fixture
def sb(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    k = bindir / "kubectl"
    k.write_text(FAKE_KUBECTL)
    k.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")

    class S:
        kc = tmp_path / "secret-dir" / "harv.kubeconfig"

        @staticmethod
        def state(**st):
            S.kc.parent.mkdir(exist_ok=True)
            st.setdefault("namespaces", ["apps"])
            S.kc.write_text(json.dumps(st))

        @staticmethod
        def run(*argv):
            env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", FAKE_KUBECTL_LOG=str(log))
            r = subprocess.run([sys.executable, str(SCRIPT), argv[0], "--kubeconfig", str(S.kc), *argv[1:]],
                               capture_output=True, text=True, env=env, timeout=60)
            r.steps = [ln.split("|", 3)[1:] for ln in r.stderr.splitlines() if ln.startswith("STEP_EVENT|")]
            return r

        @staticmethod
        def calls():
            return [json.loads(x) for x in log.read_text().splitlines()]

        @staticmethod
        def writes():
            return [c for c in S.calls() if c["args"][0] in ("create", "scale", "patch", "delete")
                    and "--dry-run=server" not in c["args"]]

    S.state()
    S.tmp = tmp_path
    return S


def verbs(calls):
    return [(c["args"][0], "--dry-run=server" in c["args"]) for c in calls if c["args"][0] != "get"]


def test_create_from_options_checks_then_writes(sb):
    r = sb.run("create", "--name", "web", "--namespace", "apps", "--image", "nginx:1", "--replicas", "2",
               "--gpus", "1", "--env", "A=1", "--command", "sleep infinity")
    assert r.returncode == 0, r.stderr
    assert verbs(sb.calls()) == [("create", True), ("create", False)]       # contrôle à blanc d'abord
    dry, real = [c for c in sb.calls() if c["args"][0] == "create"]
    assert dry["stdin"] == real["stdin"]
    m = json.loads(real["stdin"])
    c = m["spec"]["template"]["spec"]["containers"][0]
    assert (m["kind"], m["spec"]["replicas"]) == ("Deployment", 2)
    assert c["resources"]["limits"] == {"memory": "256Mi", "nvidia.com/gpu": "1"}
    assert c["command"] == ["sleep", "infinity"] and c["env"] == [{"name": "A", "value": "1"}]
    ids = [(s[0], s[1]) for s in r.steps]
    assert ids[:2] == [("validate", "running"), ("validate", "done")]
    assert ("dry-run", "done") in ids and ("write", "done") in ids and ids[-1] == ("create", "done")


def test_create_from_a_file_and_from_the_console_spec(sb):
    f = sb.tmp / "job.json"
    f.write_text(json.dumps({"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "once", "namespace": "apps"},
                             "spec": {"template": {"spec": {"restartPolicy": "Never",
                                                            "containers": [{"name": "c", "image": "busybox"}]}}}}))
    assert sb.run("create", "--file", str(f)).returncode == 0
    m = json.loads(sb.writes()[-1]["stdin"])
    assert m["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"] == {"memory": "256Mi"}
    spec = sb.tmp / "spec.json"
    spec.write_text(json.dumps({"name": "j2", "namespace": "apps", "kind": "Job", "image": "busybox", "replicas": 3}))
    assert sb.run("create", "--spec", str(spec)).returncode == 0
    m = json.loads(sb.writes()[-1]["stdin"])
    assert (m["kind"], m["spec"]["completions"]) == ("Job", 3)
    spec.write_text(json.dumps({"manifest": {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "p"},
                                             "spec": {"containers": [{"name": "c", "image": "i"}]}}}))
    sb.state(namespaces=["default"])
    assert sb.run("create", "--spec", str(spec)).returncode == 0
    assert json.loads(sb.writes()[-1]["stdin"])["metadata"]["namespace"] == "default"


@pytest.mark.parametrize("argv", [
    ["create", "--name", "web", "--namespace", "kube-system", "--image", "nginx"],
    ["create", "--name", "web", "--namespace", "harvester-system", "--image", "nginx"],
    ["create", "--name", "Web", "--namespace", "apps", "--image", "nginx"],
    ["create", "--name", "web", "--namespace", "apps"],                              # sans image
    ["create", "--name", "web", "--namespace", "apps", "--image", "nginx", "--memory", "lots"],
    ["scale", "--namespace", "cattle-system", "--kind", "Deployment", "--name", "rancher", "--replicas", "0"],
    ["scale", "--namespace", "apps", "--kind", "Job", "--name", "j", "--replicas", "2"],
    ["scale", "--namespace", "apps", "--kind", "Deployment", "--name", "web", "--replicas", "-1"],
    ["restart", "--namespace", "longhorn-system", "--kind", "DaemonSet", "--name", "x"],
    ["restart", "--namespace", "apps", "--kind", "Job", "--name", "j"],
    ["delete", "--namespace", "kube-system", "--kind", "Deployment", "--name", "coredns"],
    ["delete", "--namespace", "apps", "--kind", "VirtualMachine", "--name", "vm1"],
])
def test_refusals_touch_nothing(sb, argv):
    r = sb.run(*argv)
    assert r.returncode == 2, r.stderr
    assert sb.calls() == []                                    # rien demandé au cluster
    assert r.steps and r.steps[-1][:2] == ["validate", "error"]


@pytest.mark.parametrize("argv,verb", [
    (["create", "--name", "web", "--namespace", "apps", "--image", "nginx"], "create"),
    (["scale", "--namespace", "apps", "--kind", "Deployment", "--name", "web", "--replicas", "3"], "scale"),
    (["restart", "--namespace", "apps", "--kind", "Deployment", "--name", "web"], "patch"),
    (["delete", "--namespace", "apps", "--kind", "Deployment", "--name", "web"], "delete"),
])
def test_a_failed_server_check_writes_nothing_and_leaks_no_path(sb, argv, verb):
    sb.state(dry_fail={verb: 'admission webhook "validator.harvesterhci.io" denied the request, see @KC@'})
    r = sb.run(*argv)
    assert r.returncode == 1
    assert sb.writes() == []
    err = [s for s in r.steps if s[1] == "error"]
    assert err and err[0][0] == "dry-run" and "denied the request" in err[0][2]
    assert str(sb.kc) not in r.stderr and "secret-dir" not in r.stderr and "<kubeconfig>" in r.stderr


def test_a_failed_write_is_said(sb):
    sb.state(write_fail={"scale": 'deployments.apps "web" not found'})
    r = sb.run("scale", "--namespace", "apps", "--kind", "deployment", "--name", "web", "--replicas", "1")
    assert r.returncode == 1
    assert ["write", "error", 'deployments.apps "web" not found'] in r.steps


def test_gestures_send_the_right_kubectl_commands(sb):
    assert sb.run("scale", "--namespace", "apps", "--kind", "deployment", "--name", "web", "--replicas", "0").returncode == 0
    assert sb.run("restart", "--namespace", "apps", "--kind", "statefulset", "--name", "db").returncode == 0
    assert sb.run("delete", "--namespace", "apps", "--kind", "job", "--name", "once").returncode == 0
    w = [c["args"] for c in sb.writes()]
    assert w[0] == ["scale", "deployments.apps/web", "-n", "apps", "--replicas=0"]
    assert w[1][:6] == ["patch", "statefulsets.apps", "db", "-n", "apps", "--type"]
    patch = json.loads(w[1][w[1].index("-p") + 1])
    assert list(patch["spec"]["template"]["metadata"]["annotations"]) == ["kubectl.kubernetes.io/restartedAt"]
    assert w[2] == ["delete", "jobs.batch", "once", "-n", "apps", "--wait=false"]
    # chaque écriture suit son contrôle à blanc, avec les mêmes arguments
    calls = [c["args"] for c in sb.calls()]
    for args in w:
        i = calls.index(args)
        assert calls[i - 1] == args + ["--dry-run=server"]


def test_dry_run_flag_writes_nothing(sb):
    r = sb.run("delete", "--namespace", "apps", "--kind", "Deployment", "--name", "web", "--dry-run")
    assert r.returncode == 0 and sb.writes() == []
    assert ["write", "skipped", "dry run: nothing written"] in r.steps


def test_missing_namespace_is_created_only_when_asked(sb):
    base = ["create", "--name", "web", "--namespace", "newns", "--image", "nginx"]
    r = sb.run(*base)
    assert r.returncode == 1 and sb.writes() == []
    assert r.steps[-1][:2] == ["namespace", "error"]
    r = sb.run(*base, "--create-namespace")
    assert r.returncode == 0, r.stderr
    w = [c["args"][:3] for c in sb.writes()]
    assert w == [["create", "namespace", "newns"], ["create", "-f", "-"]]


def test_a_refused_workload_takes_its_new_namespace_back(sb):
    sb.state(dry_fail={"create": "exceeded quota"})
    r = sb.run("create", "--name", "web", "--namespace", "newns", "--image", "nginx", "--create-namespace")
    assert r.returncode == 1
    w = [c["args"][:3] for c in sb.writes()]
    assert w == [["create", "namespace", "newns"], ["delete", "namespace", "newns"]]


def test_an_unreadable_namespace_lets_the_server_check_decide(sb):
    sb.state(ns_error='namespaces "apps" is forbidden: User "u" cannot get resource "namespaces"')
    r = sb.run("create", "--name", "web", "--namespace", "apps", "--image", "nginx")
    assert r.returncode == 0
    assert verbs(sb.calls()) == [("create", True), ("create", False)]


def test_a_target_is_required(tmp_path):
    r = subprocess.run([sys.executable, str(SCRIPT), "delete", "--namespace", "apps", "--kind", "Pod",
                        "--name", "p"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "--cluster or --kubeconfig" in r.stderr


@pytest.mark.parametrize("cmd", ["create", "scale", "restart", "delete"])
def test_help(cmd):
    r = subprocess.run([sys.executable, str(SCRIPT), cmd, "--help"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "--dry-run" in r.stdout
