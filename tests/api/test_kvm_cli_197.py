"""v1.97.0 : bin/harvester-kvm.py, affichage, instantanés, clonage, suppression.

Le faux réseau remplace ssh au niveau de `subprocess.run` : il redécoupe la
ligne distante avec shlex (comme le shell de l'hôte) et la rejoue contre le
libvirt simulé des tests de kvm_ops (`Sim` de test_kvm_ops_197.py) : un nom
mal cité n'y retrouverait pas son domaine. L'annulation réelle se joue dans
un vrai processus, avec de faux `ssh`, `virsh` et `qemu-img` sur le PATH.
"""

import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
spec = importlib.util.spec_from_file_location("harvester_kvm_197", ROOT / "bin" / "harvester-kvm.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
_ops_spec = importlib.util.spec_from_file_location("kvm_ops_world_197",
                                                   ROOT / "tests" / "api" / "test_kvm_ops_197.py")
W = importlib.util.module_from_spec(_ops_spec)
_ops_spec.loader.exec_module(W)
import kvm_decl  # noqa: E402
import kvm_host as kh  # noqa: E402

URI = "qemu:///system"
ADDR = "10.0.0.5"
PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAfake\n-----END OPENSSH PRIVATE KEY-----\n"
PASSWORD = W.FAKE_PASSWORD
POOL, OUT = W.POOL, W.OUT
IP_ADDR = ("1: lo    inet 127.0.0.1/8 scope host lo\n"
           "1: lo    inet6 ::1/128 scope host noprefixroute\n"
           f"2: eno1    inet {ADDR}/16 brd 10.0.255.255 scope global eno1\n")
VIRSH_PREFIX = ["env", "LC_ALL=C", "virsh", "-c", URI]


def ns(rc=0, out="", err=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


class SimNet:
    """ssh simulé devant un `Sim` : chaque ligne distante est redécoupée
    par shlex, puis jouée (virsh seul, lot virsh, lot shell, copie)."""

    def __init__(self, sim):
        self.sim = sim
        self.remotes = []                     # lignes distantes redécoupées
        self.raw = []                         # lignes distantes telles qu'envoyées
        self.inputs = []                      # (ligne, entrée standard)

    def __call__(self, argv, capture_output=False, text=False, timeout=None, input=None,
                 stdin=None):
        assert capture_output and text and timeout and argv[0] == "ssh"
        if argv[1:3] == ["-O", "exit"]:
            return ns()
        assert argv[-3] == "--" and argv[-2] == ADDR
        # l'entrée de la console ne part jamais, sauf les données de run_input
        assert (input is None) == (stdin is subprocess.DEVNULL)
        rem = shlex.split(argv[-1])
        self.raw.append(argv[-1])
        self.remotes.append(rem)
        sim = self.sim
        if input is not None:
            self.inputs.append((rem, input))
        if rem[:5] == VIRSH_PREFIX:
            if input is None:
                sim.calls.append(("virsh", tuple(rem[5:])))
            else:
                sim.calls.append(("input", tuple(rem), input))
            return ns(*sim._answer(rem[5:], input))
        if rem[:2] == ["sh", "-c"] and re.search(r"^T=@@kvm-[0-9a-f]+$", rem[2], re.M):
            return self.batch(rem[2])
        if rem == ["env", "LC_ALL=C", "ip", "-o", "addr", "show"]:
            return ns(0, IP_ADDR)
        try:
            return ns(0, sim.run(rem))
        except kh.KvmError as e:
            return ns(e.rc or 1, "", e.message.split(": ", 2)[-1])

    def batch(self, script):
        token = re.search(r"^T=(@@kvm-[0-9a-f]+)$", script, re.M).group(1)
        stdout, stderr = f"{token} start 1\n", f"{token} start\n"
        for chunk in re.split(r"\n_kvm_run (?=\d+ )", script)[1:]:
            words = shlex.split(chunk)
            i, words = int(words[0]), words[1:]
            assert words[:3] == ["virsh", "-c", URI]
            self.sim.calls.append(("batch", tuple(words[3:])))
            rc, out, err = self.sim._answer(words[3:])
            stdout += f"{out}\n{token} {i} {rc}\n"
            stderr += f"{err}\n{token} {i}\n"
        return ns(0, stdout, stderr)

    def virsh_commands(self, first=None):
        """Commandes virsh distantes, en argv exact (seules, en lot ou avec
        une entrée), après redécoupage shlex."""
        out = []
        for c in self.sim.calls:
            args = c[1][5:] if c[0] == "input" else c[1]
            if c[0] in ("virsh", "batch", "input") and (first is None or args[0] == first):
                out.append(tuple(args))
        return out


@pytest.fixture
def env(monkeypatch):
    short = Path(tempfile.mkdtemp(prefix="kvm7-", dir="/tmp"))
    state = short / "state"
    kvm_decl.write_key(state, "lab", PRIVATE_KEY)
    kvm_decl.write_known_hosts(state, "lab", [f"{ADDR} ssh-ed25519 AAAAed"])
    kvm_decl.write(state, {"name": "lab", "address": ADDR, "user": "virtops",
                           "ssh_key": kvm_decl.key_rel("lab"),
                           "known_hosts": kvm_decl.known_hosts_rel("lab")})
    box = SimpleNamespace(state=state, net=None)

    def use(sim):
        box.net = SimNet(sim)
        monkeypatch.setattr(cli, "RUNNER", box.net)
        return box.net
    box.use = use
    monkeypatch.setattr(cli, "SLEEP", lambda s: None)
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(state))
    yield box
    shutil.rmtree(short, ignore_errors=True)


def run(capsys, *argv, as_json=True):
    rc = cli.main(list(argv) + (["--json"] if as_json else []))
    out, err = capsys.readouterr()
    steps = [ln.split("|", 3)[1:] for ln in err.splitlines() if ln.startswith("STEP_EVENT|")]
    data = json.loads(out) if as_json and out.strip() else out
    return SimpleNamespace(rc=rc, data=data, steps=steps, out=out, err=err)


def step_ids(res):
    return [(s[0], s[1]) for s in res.steps]


# --- vm display -----------------------------------------------------------------------

def vnc_dom(name, listen, port=5901, passwd=True):
    pw = f" passwd='{PASSWORD}'" if passwd else ""
    return W.dom(name, [W.disk("vda", "/srv/vm/x.qcow2")],
                 graphics=(f"<graphics type='vnc' port='{port}' autoport='yes' listen='{listen}'"
                           f"{pw}><listen type='address' address='{listen}'/></graphics>"))


TUNNEL = "ssh -N -L {p}:{a}:{p} -p 22 -l virtops 10.0.0.5"
DISPLAY_CASES = [
    # libellé, XML, état, attendu (available, address, port, password, reason), adresses lues
    ("loopback with password", W.fx("dumpxml-vnc-password.xml"), "running",
     (True, "127.0.0.1", 5900, True, None), False),
    ("stopped", W.fx("dumpxml-vnc-password.xml"), "shut off",
     (False, None, None, False, "notrunning"), False),
    ("spice only", W.fx("dumpxml-spice-only.xml"), "running",
     (False, None, None, False, "spice"), False),
    ("unix socket", W.fx("dumpxml-vnc-socket.xml"), "running",
     (False, None, None, False, "socket"), False),
    ("wildcard", vnc_dom("w", "0.0.0.0"), "running", (True, "127.0.0.1", 5901, True, None), False),
    ("ip of the host", vnc_dom("h", ADDR, passwd=False), "running",
     (True, ADDR, 5901, False, None), True),
    ("ip of another machine", vnc_dom("o", "10.9.9.9"), "running",
     (False, None, None, False, "listen"), True),
    ("ipv6 loopback", vnc_dom("v6", "::1"), "running", (True, "::1", 5901, True, None), False),
    ("no display", W.dom("n", [W.disk("vda", "/srv/vm/x.qcow2")]), "running",
     (False, None, None, False, "nodisplay"), False),
]


@pytest.mark.parametrize("label,xml,state,expected,reads_addresses", DISPLAY_CASES,
                         ids=[c[0] for c in DISPLAY_CASES])
def test_vm_display(env, capsys, label, xml, state, expected, reads_addresses):
    sim = W.Sim()
    name = re.search(r"<name>([^<]+)</name>", xml).group(1)
    sim.add_domain(name, xml, state=state, live=xml)
    net = env.use(sim)
    res = run(capsys, "vm", "display", "--host=lab", f"--name={name}")
    d = res.data
    assert res.rc == 0 and d["ok"] is True
    assert (d["available"], d["address"], d["port"], d["password"], d["reason"]) == expected
    if expected[0]:
        a = f"[{expected[1]}]" if ":" in expected[1] else expected[1]
        assert d["tunnel"] == TUNNEL.format(p=expected[2], a=a) and d["type"] == "vnc"
    else:
        assert d["tunnel"] is None and d["message"] == cli.DISPLAY_REASONS[expected[4]]
    # le mot de passe : lu par --security-info pour sa présence, jamais montré
    assert ("dumpxml", "--security-info", "--domain", name) in net.virsh_commands("dumpxml")
    assert PASSWORD not in res.out + res.err
    assert (["env", "LC_ALL=C", "ip", "-o", "addr", "show"] in net.remotes) == reads_addresses
    assert res.steps[-1][:2] == ["display", "done" if expected[0] else "warn"]


def test_vm_display_text_gives_the_tunnel_without_password_or_key(env, capsys):
    sim = W.Sim()
    xml = W.fx("dumpxml-vnc-password.xml")
    sim.add_domain("kvmtest-1", xml, state="running", live=xml)
    env.use(sim)
    res = run(capsys, "vm", "display", "--host=lab", "--name=kvmtest-1", as_json=False)
    assert res.rc == 0
    assert "ssh -N -L 5900:127.0.0.1:5900 -p 22 -l virtops 10.0.0.5" in res.out
    assert "protected by a password" in res.out
    assert PASSWORD not in res.out + res.err and str(env.state) not in res.out + res.err


def test_vm_display_unknown_domain(env, capsys):
    env.use(W.Sim())
    res = run(capsys, "vm", "display", "--host=lab", "--name=ghost")
    assert res.rc == 1 and set(res.data) == {"ok", "error", "kind"}
    assert res.data["kind"] == "notfound"


# --- instantanés ----------------------------------------------------------------------

def snap_world(**kw):
    sim = W.snap_sim(**kw)
    xml = sim.domains["kvmtest-1"]["xml"]
    sim.domains["kvmtest-1"]["live"] = xml
    return sim


def test_snapshot_list(env, capsys):
    env.use(snap_world())
    res = run(capsys, "snapshot", "list", "--host=lab", "--name=kvmtest-1")
    assert res.rc == 0
    assert [s["name"] for s in res.data["snapshots"]] == ["s1", "s2", "-z w"]
    assert res.data["current"] == "-z w" and res.data["support"] == []
    text = run(capsys, "snapshot", "list", "--host=lab", "--name=kvmtest-1", as_json=False).out
    assert "-z w" in text and "current" in text and PASSWORD not in text


@pytest.mark.parametrize("state,description,memory,announce", [
    ("shut off", None, False, "the snapshot holds the disks"),
    ("running", "-dash; $(id)", True, "the guest is paused while its memory (0.2 GiB) is saved"),
])
def test_snapshot_create(env, capsys, state, description, memory, announce):
    net = env.use(snap_world(state=state))
    argv = ["snapshot", "create", "--host=lab", "--name=kvmtest-1", "--snapshot=new-1"]
    if description:
        argv.append(f"--description={description}")
    res = run(capsys, *argv)
    assert res.rc == 0 and res.data["memory"] is memory and res.data["current"] == "new-1"
    expected = ["snapshot-create-as", "--domain", "kvmtest-1", "--name", "new-1"]
    if description:
        expected += ["--description", description]
    assert net.virsh_commands("snapshot-create-as") == [tuple(expected + ["--atomic"])]
    assert step_ids(res) == [("plan", "running"), ("plan", "done"), ("create", "running"),
                             ("create", "done"), ("verify", "done")]
    assert announce in res.steps[1][2]


SNAPSHOT_REFUSALS = [
    ("raw disk", dict(xml=W.dom("kvmtest-1", [W.disk("vda", "/i/r.img", fmt="raw")])),
     ["raw disk vda: internal snapshots need qcow2"]),
    ("paused", dict(state="paused"),
     ["the VM is paused: wait until it is running or shut off"]),
    ("existing name", dict(snaps=W.SNAPS + [("new-1", "shutoff", None, "internal")]),
     ["a snapshot named new-1 already exists"]),
]


@pytest.mark.parametrize("label,kw,refusals", SNAPSHOT_REFUSALS,
                         ids=[c[0] for c in SNAPSHOT_REFUSALS])
def test_snapshot_create_refused(env, capsys, label, kw, refusals):
    net = env.use(snap_world(**kw))
    res = run(capsys, "snapshot", "create", "--host=lab", "--name=kvmtest-1",
              "--snapshot=new-1")
    assert res.rc == 1 and res.data["kind"] == "refused"
    assert res.data["refusals"] == refusals
    assert net.virsh_commands("snapshot-create-as") == []
    assert res.steps[-1][:2] == ["create", "error"]


@pytest.mark.parametrize("snap,start,end", [("s1", "shut off", "running"),
                                            ("s2", "running", "shut off")])
def test_snapshot_revert_rereads_the_state(env, capsys, snap, start, end):
    net = env.use(snap_world(state=start))
    res = run(capsys, "snapshot", "revert", "--host=lab", "--name=kvmtest-1",
              f"--snapshot={snap}")
    assert res.rc == 0 and res.data["state"] == end
    assert net.virsh_commands("snapshot-revert") == [
        ("snapshot-revert", "--domain", "kvmtest-1", "--snapshotname", snap)]
    assert "the current state of kvmtest-1 that was not saved is lost" in res.steps[1][2]
    assert res.steps[-1] == ["verify", "done", f"kvmtest-1: {end}"]


def test_snapshot_revert_needing_force_is_named_never_forced(env, capsys):
    sim = snap_world()
    sim.fail["snapshot-revert"] = (1, "error: revert requires force: snapshot 's1' lacks "
                                      "domain 'kvmtest-1' rollback info\n")
    net = env.use(sim)
    res = run(capsys, "snapshot", "revert", "--host=lab", "--name=kvmtest-1", "--snapshot=s1")
    assert res.rc == 1 and res.data["kind"] == "needs-force"
    assert "revert requires force" in res.data["error"]
    assert not any("--force" in c for c in net.virsh_commands())


def test_snapshot_delete_reattaches_children(env, capsys):
    net = env.use(snap_world())
    res = run(capsys, "snapshot", "delete", "--host=lab", "--name=kvmtest-1", "--snapshot=s2")
    assert res.rc == 0
    assert net.virsh_commands("snapshot-delete") == [
        ("snapshot-delete", "--domain", "kvmtest-1", "--snapshotname", "s2")]
    assert [(s["name"], s["parent"]) for s in res.data["snapshots"]] == [("s1", None),
                                                                         ("-z w", "s1")]
    assert "its children (-z w) are attached to its parent" in res.steps[1][2]


@pytest.mark.parametrize("verb", ["revert", "delete"])
def test_snapshot_unknown_is_notfound(env, capsys, verb):
    net = env.use(snap_world())
    res = run(capsys, "snapshot", verb, "--host=lab", "--name=kvmtest-1", "--snapshot=nope")
    assert res.rc == 1 and res.data["kind"] == "notfound"
    assert net.virsh_commands("snapshot-" + verb) == []


# --- noms piégés ----------------------------------------------------------------------

HOSTILE = ["a;rm -rf /", "$(id)", "x y", "-x", "'q'"]


@pytest.mark.parametrize("name", HOSTILE)
def test_hostile_vm_and_snapshot_names_stay_one_argument(env, capsys, name):
    sim = W.Sim()
    xml = W.dom(name, [W.disk("vda", f"{OUT}/h.qcow2")])
    sim.add_domain(name, xml, snaps=[(name, "shutoff", None, "internal")], current=name)
    net = env.use(sim)
    for verb in ("revert", "delete"):
        res = run(capsys, "snapshot", verb, "--host=lab", f"--name={name}",
                  f"--snapshot={name}")
        assert res.rc == 0, res.data
    assert net.virsh_commands("snapshot-revert") == [
        ("snapshot-revert", "--domain", name, "--snapshotname", name)]
    assert net.virsh_commands("snapshot-delete") == [
        ("snapshot-delete", "--domain", name, "--snapshotname", name)]
    # la ligne distante est citée : le shell n'y voit jamais `;`, `$(` nus
    for raw in net.raw:
        if "snapshot-revert" in raw:
            assert shlex.quote(name) in raw


@pytest.mark.parametrize("new", ["$(id) x;y", "-x", "a b"])
def test_hostile_clone_name_stays_one_argument(env, capsys, new):
    net = env.use(W.clone_sim())
    res = run(capsys, "clone", "--host=lab", "--name=src", f"--new-name={new}")
    assert res.rc == 0, res.data
    assert res.data["name"] == new
    assert ("dominfo", "--domain", new) in net.virsh_commands("dominfo")
    defined = ET_name(net.inputs[-1][1])
    assert defined == new and new in net.sim.domains


def ET_name(xml):
    import xml.etree.ElementTree as ET
    return ET.fromstring(xml).findtext("name")


@pytest.mark.parametrize("argv,msg", [
    (["snapshot", "create", "--host=lab", "--name=v", "--snapshot=a;b"], "invalid snapshot name"),
    (["snapshot", "create", "--host=lab", "--name=v", "--snapshot=-x"], "invalid snapshot name"),
    (["snapshot", "create", "--host=lab", "--name=v", "--snapshot=ok",
      "--description=" + "x" * 1001], "invalid description"),
    (["snapshot", "revert", "--host=lab", "--name=v", "--snapshot="], "invalid snapshot name"),
    (["snapshot", "delete", "--host=lab", "--name=v", "--snapshot=s", "--timeout=0"],
     "--timeout must be positive"),
    (["clone", "--host=lab", "--name=v", "--new-name=a/b"], "no '/'"),
    (["clone", "--host=lab", "--name=v", "--new-name=.hidden"], "no '/'"),
    (["clone", "--host=lab", "--name=", "--new-name=c"], "invalid domain name"),
    (["vm", "delete", "--host=lab", "--name=v", "--acknowledge-unreadable=/x"],
     "only applies with --remove-storage"),
    (["vm", "delete", "--host=lab", "--name=v", "--remove-storage",
      "--acknowledge-unreadable=relative"], ""),
    (["vm", "display", "--host=ghost", "--name=v"], "host ghost is not declared"),
    (["snapshot", "create", "--host=lab", "--name=v"], "--snapshot"),
])
def test_usage_errors_reach_no_host(env, capsys, argv, msg):
    net = env.use(W.Sim())
    res = run(capsys, *argv)
    assert res.rc == 2 and set(res.data) == {"ok", "error", "kind"}
    assert res.data["kind"] == "usage" and msg in res.data["error"]
    assert net.remotes == []


# --- clonage --------------------------------------------------------------------------

def test_clone_pool_mode_argv_and_stdin(env, capsys):
    net = env.use(W.clone_sim())
    res = run(capsys, "clone", "--host=lab", "--name=src", "--new-name=src-2")
    assert res.rc == 0, res.data
    # volume créé par libvirt depuis la source, XML du volume sur l'entrée standard
    (create,) = net.virsh_commands("vol-create-from")
    assert create == ("vol-create-from", "--pool", "kvmtest", "--file", "/dev/stdin",
                      "--vol", "mine.qcow2", "--inputpool", "kvmtest")
    assert net.virsh_commands("vol-clone") == []
    # define : XML sur l'entrée standard, jamais en argument
    assert net.virsh_commands("define") == [("define", "/dev/stdin")]
    (define_input,) = [d for r, d in net.inputs if "define" in r]
    assert "<domain" in define_input and PASSWORD in define_input   # la copie garde l'accès
    assert all("<domain" not in a for rem in net.remotes for a in rem)
    assert PASSWORD not in res.out + res.err
    assert step_ids(res) == [("plan", "running"), ("plan", "done"), ("copy-vda", "running"),
                             ("copy-vda", "done"), ("define", "running"), ("define", "done"),
                             ("verify", "running"), ("verify", "done")]
    d = res.data
    assert (d["source"], d["name"], d["leftovers"], d["dry_run"]) == ("src", "src-2", [], False)
    assert d["disks"] == [{"target": "vda", "destination": f"{POOL}/src-2-vda.qcow2",
                           "pool": "kvmtest"}]
    assert "src-2" in net.sim.domains


def test_clone_file_mode_reports_copied_bytes(env, capsys):
    net = env.use(W.clone_sim("file"))
    res = run(capsys, "clone", "--host=lab", "--name=src", "--new-name=src-2")
    assert res.rc == 0, res.data
    copy_steps = [s for s in res.steps if s[0] == "copy-vda"]
    assert ["copy-vda", "running", f"50% ({(64 << 20) // 2} of {64 << 20} bytes)"] in copy_steps
    assert res.data["disks"] == [{"target": "vda", "destination": f"{OUT}/src-2-vda.qcow2",
                                  "pool": None}]
    assert f"{OUT}/src-2-vda.qcow2" in net.sim.exists


def test_clone_dry_run_changes_nothing(env, capsys):
    net = env.use(W.clone_sim())
    res = run(capsys, "clone", "--host=lab", "--name=src", "--new-name=src-2", "--dry-run")
    assert res.rc == 0 and res.data["dry_run"] is True and res.data["possible"] is True
    assert res.data["plan"]["disks"][0]["destination"] == f"{POOL}/src-2-vda.qcow2"
    assert net.inputs == [] and "src-2" not in net.sim.domains
    assert net.virsh_commands("vol-create-from") == [] and net.virsh_commands("define") == []


def test_clone_dry_run_shows_refusals_with_exit_0(env, capsys):
    env.use(W.clone_sim(state="running"))
    res = run(capsys, "clone", "--host=lab", "--name=src", "--new-name=src-2", "--dry-run")
    assert res.rc == 0 and res.data["possible"] is False
    assert res.data["plan"]["refusals"] == [
        "the VM must be shut off to be copied (state: running)"]
    assert res.steps[-1][:2] == ["plan", "warn"]


def test_clone_refused(env, capsys):
    net = env.use(W.clone_sim(state="running"))
    res = run(capsys, "clone", "--host=lab", "--name=src", "--new-name=src-2")
    assert res.rc == 1 and res.data["kind"] == "refused"
    assert res.data["refusals"] == ["the VM must be shut off to be copied (state: running)"]
    assert res.data["leftovers"] == [] and net.inputs == []
    assert step_ids(res) == [("plan", "running"), ("plan", "error")]


def test_clone_failure_names_what_the_rollback_left(env, capsys):
    sim = W.clone_sim()
    sim.fail["define"] = (1, "error: boom\n")
    real = sim._answer

    def answer(args, data=None):
        if str(args[0]) == "vol-delete":
            return 1, "", "error: cannot delete volume: busy\n"
        return real(args, data)
    sim._answer = answer
    env.use(sim)
    res = run(capsys, "clone", "--host=lab", "--name=src", "--new-name=src-2")
    assert res.rc == 1 and res.data["kind"] == "failed"
    (left,) = res.data["leftovers"]
    assert (left["kind"], left["pool"], left["volume"]) == ("volume", "kvmtest",
                                                            "src-2-vda.qcow2")
    assert step_ids(res)[-2:] == [("define", "error"), ("rollback", "warn")]
    assert "volume kvmtest/src-2-vda.qcow2" in res.steps[-1][2]


def test_clone_failure_rolled_back_says_so(env, capsys):
    sim = W.clone_sim()
    sim.fail["define"] = (1, "error: boom\n")
    env.use(sim)
    res = run(capsys, "clone", "--host=lab", "--name=src", "--new-name=src-2")
    assert res.rc == 1 and res.data["leftovers"] == []
    assert res.steps[-1] == ["rollback", "done", "everything created was removed"]
    assert "src-2-vda.qcow2" not in sim.pools["kvmtest"]["vols"]


def test_clone_unexpected_error_keeps_leftovers_without_details(env, capsys, monkeypatch):
    sim = W.clone_sim()
    env.use(sim)
    real = cli.ko.clone_xml

    def broken(*a, **k):
        raise RuntimeError("/secret/path in a message")
    monkeypatch.setattr(cli.ko, "clone_xml", broken)
    res = run(capsys, "clone", "--host=lab", "--name=src", "--new-name=src-2")
    monkeypatch.setattr(cli.ko, "clone_xml", real)
    assert res.rc == 1 and res.data["error"] == "unexpected internal error (RuntimeError)"
    assert res.data["leftovers"] == [] and "/secret/path" not in res.out + res.err
    assert "src-2-vda.qcow2" not in sim.pools["kvmtest"]["vols"]


def test_operations_on_the_same_vm_never_overlap(env, capsys):
    net = env.use(W.clone_sim())
    with cli.vm_lock(env.state, "lab", "src"):
        res = run(capsys, "clone", "--host=lab", "--name=src", "--new-name=src-2")
        assert res.rc == 2 and res.data["kind"] == "busy"
        res = run(capsys, "vm", "delete", "--host=lab", "--name=src")
        assert res.rc == 2 and res.data["kind"] == "busy"
    with cli.vm_lock(env.state, "lab", "src-2"):           # le nom du clone aussi
        res = run(capsys, "clone", "--host=lab", "--name=src", "--new-name=src-2")
        assert res.rc == 2 and "src-2" in res.data["error"]
    assert net.remotes == []
    # un autre nom, une autre VM : libre
    res = run(capsys, "clone", "--host=lab", "--name=src", "--new-name=src-3")
    assert res.rc == 0


# --- suppression ----------------------------------------------------------------------

@pytest.mark.parametrize("nvram,flag", [(None, False), ("qcow2", True)])
def test_vm_delete_storage_only_owned_volumes(env, capsys, nvram, flag):
    net = env.use(W.delete_sim(nvram=nvram))
    res = run(capsys, "vm", "delete", "--host=lab", "--name=victim", "--remove-storage")
    assert res.rc == 0, res.data
    (undef,) = net.virsh_commands("undefine")
    assert undef == tuple(["undefine", "--domain", "victim", "--managed-save",
                           "--snapshots-metadata", "--checkpoints-metadata"]
                          + ["--nvram"] * flag + ["--storage", "vda"])
    assert "--remove-all-storage" not in undef
    d = res.data
    assert [x["target"] for x in d["deleted"]] == ["vdc", "vda"]
    assert [(x["target"], x["reason"]) for x in d["kept"]] == [
        ("vdb", "shared with other"), ("sda", "read-only or CD-ROM")]
    assert d["undefined"] is True and d["leftovers"] == [] and bool(d["nvram"]) is flag
    assert step_ids(res) == [("plan", "running"), ("plan", "done"), ("undefine", "running"),
                             ("undefine", "done"), ("storage", "running"), ("storage", "done"),
                             ("verify", "done")]
    assert "victim" not in net.sim.domains


def test_vm_delete_without_storage_keeps_disks(env, capsys):
    net = env.use(W.delete_sim(snaps=[("s1", "shutoff", None, "internal")]))
    res = run(capsys, "vm", "delete", "--host=lab", "--name=victim")
    assert res.rc == 0
    (undef,) = net.virsh_commands("undefine")
    assert "--storage" not in undef and res.data["deleted"] == []
    assert res.data["internal_snapshots"] == 1
    assert "internal snapshot data stays in the kept disk files (qemu-img snapshot -l)" in \
        res.data["notes"]


def test_vm_delete_refused_when_running(env, capsys):
    net = env.use(W.delete_sim(state="running"))
    res = run(capsys, "vm", "delete", "--host=lab", "--name=victim", "--remove-storage")
    assert res.rc == 1 and res.data["kind"] == "refused"
    assert res.data["refusals"] == ["the VM is running: shut it down or force it off first"]
    assert net.virsh_commands("undefine") == []
    assert step_ids(res) == [("plan", "running"), ("plan", "error")]


def test_vm_delete_unreadable_neighbour_keeps_disks_unless_acknowledged(env, capsys):
    net = env.use(W.d46_sim())
    res = run(capsys, "vm", "delete", "--host=lab", "--name=target", "--remove-storage",
              "--dry-run")
    assert res.rc == 0 and res.data["plan"]["unreadable"] == [W.SECRET]
    assert res.data["plan"]["delete"] == []
    text = run(capsys, "vm", "delete", "--host=lab", "--name=target", "--remove-storage",
               "--dry-run", as_json=False).out
    assert f"unreadable by the SSH user: {W.SECRET}" in text
    assert "--acknowledge-unreadable=PATH" in text
    assert net.virsh_commands("undefine") == []
    res = run(capsys, "vm", "delete", "--host=lab", "--name=target", "--remove-storage",
              f"--acknowledge-unreadable={W.SECRET}")
    assert res.rc == 0, res.data
    assert sorted(x["target"] for x in res.data["deleted"]) == ["vda", "vdb"]
    assert res.data["acknowledged_unreadable"] == [W.SECRET]
    (undef,) = net.virsh_commands("undefine")
    assert undef[-2:] == ("--storage", "vda")


def test_vm_delete_dry_run_changes_nothing(env, capsys):
    net = env.use(W.delete_sim())
    res = run(capsys, "vm", "delete", "--host=lab", "--name=victim", "--remove-storage",
              "--dry-run")
    assert res.rc == 0 and res.data["possible"] is True and res.data["dry_run"] is True
    assert [x["target"] for x in res.data["plan"]["delete"]] == ["vda", "vdc"]
    assert net.virsh_commands("undefine") == [] and "victim" in net.sim.domains


# --- schéma JSON (la console le lit) ----------------------------------------------------

SNAP_FAIL = {"ok", "error", "kind", "host", "name", "snapshot", "refusals"}
SCHEMAS = [
    ("display", lambda: W.snap_sim(state="running"),
     ["vm", "display", "--name=kvmtest-1"], 0,
     {"ok", "host", "name", "state", "available", "type", "address", "port", "password",
      "reason", "message", "tunnel"}),
    ("snapshot list", lambda: W.snap_sim(), ["snapshot", "list", "--name=kvmtest-1"], 0,
     {"ok", "host", "name", "snapshots", "current", "support", "running", "state",
      "persistent", "memory_kib"}),
    ("snapshot create", lambda: W.snap_sim(),
     ["snapshot", "create", "--name=kvmtest-1", "--snapshot=n"], 0,
     {"ok", "host", "name", "snapshot", "memory", "state", "current", "snapshots",
      "elapsed_s"}),
    ("snapshot create refused", lambda: W.snap_sim(state="paused"),
     ["snapshot", "create", "--name=kvmtest-1", "--snapshot=n"], 1, SNAP_FAIL),
    ("snapshot revert", lambda: W.snap_sim(),
     ["snapshot", "revert", "--name=kvmtest-1", "--snapshot=s1"], 0,
     {"ok", "host", "name", "snapshot", "taken_state", "state", "elapsed_s"}),
    ("snapshot revert notfound", lambda: W.snap_sim(),
     ["snapshot", "revert", "--name=kvmtest-1", "--snapshot=zz"], 1, SNAP_FAIL),
    ("snapshot delete", lambda: W.snap_sim(),
     ["snapshot", "delete", "--name=kvmtest-1", "--snapshot=s1"], 0,
     {"ok", "host", "name", "snapshot", "current", "snapshots"}),
    ("clone", lambda: W.clone_sim(), ["clone", "--name=src", "--new-name=c"], 0,
     {"ok", "host", "dry_run", "elapsed_s", "source", "name", "uuid", "state", "macs",
      "disks", "shared", "nvram", "uefi_entries_lost", "tpm_state_lost", "regenerated",
      "warnings", "leftovers"}),
    ("clone dry run", lambda: W.clone_sim(), ["clone", "--name=src", "--new-name=c",
                                              "--dry-run"], 0,
     {"ok", "host", "source", "new_name", "dry_run", "possible", "plan"}),
    ("clone refused", lambda: W.clone_sim(state="running"),
     ["clone", "--name=src", "--new-name=c"], 1,
     {"ok", "error", "kind", "host", "source", "new_name", "refusals", "leftovers"}),
    ("vm delete", lambda: W.delete_sim(), ["vm", "delete", "--name=victim"], 0,
     {"ok", "host", "dry_run", "name", "undefined", "deleted", "kept", "leftovers",
      "unreadable", "notes", "remove_storage", "internal_snapshots", "managed_save_lost",
      "nvram", "tpm_state", "acknowledged_unreadable"}),
    ("vm delete dry run", lambda: W.delete_sim(), ["vm", "delete", "--name=victim",
                                                   "--dry-run"], 0,
     {"ok", "host", "name", "dry_run", "possible", "plan"}),
    ("vm delete refused", lambda: W.delete_sim(state="running"),
     ["vm", "delete", "--name=victim"], 1,
     {"ok", "error", "kind", "host", "name", "refusals", "leftovers", "unreadable"}),
]
PLAN_KEYS = {
    "clone": {"source", "new_name", "refusals", "disks", "shared", "space", "nics", "nvram",
              "uefi_entries_lost", "tpm_state_lost", "regenerated"},
    "vm": {"name", "state", "persistent", "refusals", "internal_snapshots",
           "external_snapshots", "managed_save", "managed_save_lost", "nvram", "tpm_state",
           "remove_storage", "delete", "keep", "unreadable", "acknowledged_unreadable",
           "notes"},
}


@pytest.mark.parametrize("label,world,argv,rc,keys", SCHEMAS, ids=[c[0] for c in SCHEMAS])
def test_json_schema_is_stable(env, capsys, label, world, argv, rc, keys):
    sim = world()
    for d in sim.domains.values():
        d["live"] = d["live"] or d["xml"]
    env.use(sim)
    res = run(capsys, *argv, "--host=lab")
    assert res.rc == rc, res.data
    assert set(res.data) == keys
    assert res.data["ok"] is (rc == 0)
    if "plan" in res.data:
        assert set(res.data["plan"]) == PLAN_KEYS[argv[0]]
    for s in res.data.get("snapshots") or []:
        assert set(s) == {"name", "created", "state", "parent", "location", "current"}
    # une seule ligne d'objet JSON, pas de trace, pas de chemin d'état
    assert "Traceback" not in res.err and str(env.state) not in res.out + res.err
    assert PASSWORD not in res.out + res.err


# --- annulation ------------------------------------------------------------------------

@pytest.mark.parametrize("args,sig,want", [
    (SimpleNamespace(cmd="snapshot", scmd="create"), "SIGTERM",
     "snapshot create cancelled (SIGTERM): it may already have been taken: list the snapshots"),
    (SimpleNamespace(cmd="snapshot", scmd="revert"), "SIGHUP",
     "snapshot revert cancelled (SIGHUP): it may already have been reverted: list the snapshots"),
    (SimpleNamespace(cmd="snapshot", scmd="delete"), "SIGTERM",
     "snapshot delete cancelled (SIGTERM): it may already have been deleted: list the snapshots"),
    (SimpleNamespace(cmd="snapshot", scmd="list"), "SIGTERM", "cancelled (SIGTERM)"),
    (SimpleNamespace(cmd="vm", vcmd="delete", dry_run=False), "SIGTERM",
     "vm delete cancelled (SIGTERM): it may already have been undefined: check the VM list"),
    (SimpleNamespace(cmd="vm", vcmd="delete", dry_run=True), "SIGTERM", "cancelled (SIGTERM)"),
    (SimpleNamespace(cmd="clone", dry_run=False), "SIGTERM",
     "clone cancelled (SIGTERM) before any copy: nothing created"),
])
def test_cancel_messages(args, sig, want):
    assert cli.cancel_message(args, sig) == want


class Sig(BaseException):
    def __init__(self, name, leftovers):
        super().__init__(name)
        self.leftovers = leftovers


def test_cancel_message_of_a_clone_names_what_remains():
    args = SimpleNamespace(cmd="clone", dry_run=False)
    assert cli.cancel_message(args, Sig("SIGTERM", [])) == "clone cancelled (SIGTERM): rolled back"
    left = [{"kind": "volume", "pool": "p", "volume": "v", "error": "busy"},
            {"kind": "file", "path": "/srv/vm/c-vda.qcow2"}]
    assert cli.cancel_message(args, Sig("SIGTERM", left)) == (
        "clone cancelled (SIGTERM): rollback incomplete, left on the host: volume p/v (busy), "
        "/srv/vm/c-vda.qcow2")


def test_cancel_during_the_copy_in_process_rolls_back(env, capsys, monkeypatch):
    """Annulation levée pendant le suivi de la copie (comme le ferait le
    gestionnaire de signal) : code 3, retour arrière fait, JSON avec les
    restes."""
    sim = W.clone_sim("file")
    sim.copy_polls = 99
    env.use(sim)

    def on_poll(c):
        if c["polls"] == 2:
            raise cli.Cancelled("SIGTERM")
    sim.on_copy_poll = on_poll
    res = run(capsys, "clone", "--host=lab", "--name=src", "--new-name=src-2")
    assert res.rc == 3 and res.data == {"ok": False, "kind": "cancelled", "leftovers": [],
                                        "error": "clone cancelled (SIGTERM): rolled back"}
    (copy,) = sim.copies.values()
    assert copy["killed"] and "src-2" not in sim.domains


FAKE_SSH = '#!/bin/sh\nfor a in "$@"; do [ "$a" = "-O" ] && exit 0; last=$a; done\nexec /bin/sh -c "$last"\n'
FAKE_VIRSH = r'''#!{python}
# faux virsh : un monde lu dans $KVMT_WORLD (domaines, aucun pool)
import json, os, sys
world = json.load(open(os.environ["KVMT_WORLD"]))
args = sys.argv[1:]
if args[:1] == ["-c"]:
    args = args[2:]
cmd, rest = args[0], args[1:]
dom = rest[rest.index("--domain") + 1] if "--domain" in rest else None
doms = world["domains"]
if dom is not None and dom not in doms:
    sys.stderr.write(f"error: failed to get domain '{dom}'\n"); sys.exit(1)
if cmd == "list":
    for n, d in doms.items():
        if "--all" in rest or d["state"] != "shut off":
            print(n)
    print()
elif cmd == "pool-list":
    print()
elif cmd == "dominfo":
    print(f"Id:             -\nName:           {dom}\nUUID:           u\n"
          f"State:          {doms[dom]['state']}\nPersistent:     yes\nManaged save:   no")
elif cmd == "domstate":
    print(doms[dom]["state"]); print()
elif cmd == "dumpxml":
    print(doms[dom]["xml"])
else:
    sys.stderr.write(f"error: unexpected command {cmd}\n"); sys.exit(1)
'''


@pytest.mark.parametrize("sig", ["SIGTERM", "SIGHUP"])
def test_a_signal_during_the_copy_rolls_back_for_real(tmp_path, sig):
    """Vrai processus contre de faux ssh, virsh et qemu-img : le clonage hors
    pool est arrêté (bouton « Annuler » de la console) pendant la copie.
    Code 3, retour arrière fait, aucun fichier temporaire ni qemu-img restant."""
    shim = tmp_path / "bin"
    shim.mkdir()
    for name, body in {"ssh": FAKE_SSH, "qemu-img": W.FAKE_QEMU_IMG,
                       "virsh": FAKE_VIRSH.replace("{python}", sys.executable)}.items():
        (shim / name).write_text(body)
        (shim / name).chmod(0o755)
    work = tmp_path / "vm dir"
    work.mkdir()
    (work / "src.qcow2").write_text("disk-data")
    world = tmp_path / "world.json"
    world.write_text(json.dumps({"domains": {"src": {"state": "shut off", "xml": W.dom(
        "src", [W.disk("vda", str(work / "src.qcow2"))])}}}))
    short = Path(tempfile.mkdtemp(prefix="kvm7c-", dir="/tmp"))
    state = short / "state"
    kvm_decl.write_key(state, "lab", PRIVATE_KEY)
    kvm_decl.write_known_hosts(state, "lab", [f"{ADDR} ssh-ed25519 AAAAed"])
    kvm_decl.write(state, {"name": "lab", "address": ADDR, "user": "virtops",
                           "ssh_key": kvm_decl.key_rel("lab"),
                           "known_hosts": kvm_decl.known_hosts_rel("lab")})
    env = dict(os.environ, PATH=f"{shim}:{os.environ['PATH']}", KVMT_WORLD=str(world),
               QDELAY="20")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "bin" / "harvester-kvm.py"), "clone", "--json",
         f"--state-dir={state}", "--host=lab", "--name=src", "--new-name=src-2"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and proc.poll() is None:
            if any(p.name.endswith(".pid") for p in work.iterdir()):
                break
            time.sleep(0.05)
        assert any(".hops-" in p.name for p in work.iterdir()), proc.communicate(timeout=5)
        time.sleep(0.5)                     # le script suit la copie
        proc.send_signal(getattr(signal, sig))
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 3, err
        data = json.loads(out)
        assert data == {"ok": False, "kind": "cancelled", "leftovers": [],
                        "error": f"clone cancelled ({sig}): rolled back"}
        assert sorted(p.name for p in work.iterdir()) == ["src.qcow2"]
        assert "STEP_EVENT|copy-vda|running|" in err
        left = subprocess.run(["pgrep", "-f", str(work)], capture_output=True, text=True)
        assert left.stdout.strip() == ""
    finally:
        if proc.poll() is None:
            proc.kill()
        shutil.rmtree(short, ignore_errors=True)
