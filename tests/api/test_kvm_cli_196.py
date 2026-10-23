"""v1.96.0 : bin/harvester-kvm.py, la ligne de commande des hôtes KVM.

Le faux réseau rejoue ssh-keyscan, ssh-keygen et ssh : il redécoupe la ligne
distante avec shlex (comme le shell de l'hôte) et rejoue le protocole de
`batch()`. Horloge et attente sont injectées : les délais se jouent sans
attendre.
"""

import importlib.util
import json
import re
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
spec = importlib.util.spec_from_file_location("harvester_kvm", ROOT / "bin" / "harvester-kvm.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
import kvm_decl  # noqa: E402
import kvm_host as kh  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "kvm"
URI = "qemu:///system"
PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAfake\n-----END OPENSSH PRIVATE KEY-----\n"
HOST_KEYS = [("ssh-rsa", "AAAArsa", "SHA256:rsaFingerprint0000000000000000000000000000"),
             ("ssh-ed25519", "AAAAed", "SHA256:edFingerprint00000000000000000000000000000")]
DENIED = ("error: failed to connect to the hypervisor\nerror: authentication unavailable: "
          "no polkit agent available to authenticate action 'org.libvirt.unix.manage'\n")
UNREACHABLE = "ssh: connect to host 10.0.0.9 port 22: No route to host\n"


def fx(name):
    return (FIX / name).read_text()


def ok(out=""):
    return (0, out, "")


INVENTORY = {
    ("hostname",): ok(fx("hostname.txt")),
    ("nodeinfo",): ok(fx("nodeinfo.txt")),
    ("nodememstats",): ok(fx("nodememstats.txt")),
    ("version",): ok(fx("version.txt")),
    ("list", "--all"): ok(fx("list-all.txt")),
    ("list", "--all", "--autostart", "--name"): ok("harvlab-n1\n"),
    ("list", "--all", "--transient", "--name"): ok(""),
    ("domstats", "--raw", "--state", "--cpu-total", "--balloon", "--vcpu"): ok(fx("domstats-raw.txt")),
}
BASE_COMMANDS = {
    ("true",): ok(),
    ("env", "LC_ALL=C", "virsh", "--version"): ok("12.7.0\n"),
    ("qemu-img", "--version"): ok("qemu-img version 10.2.2\nCopyright (c) 2003-2025\n"),
}


def vnc_xml(name, listen="127.0.0.1", passwd=None, port=5900):
    pw = f" passwd='{passwd}'" if passwd else ""
    return (f"<domain type='kvm' id='1'><name>{name}</name><devices>"
            f"<graphics type='vnc' port='{port}' autoport='yes' listen='{listen}'{pw}>"
            f"<listen type='address' address='{listen}'/></graphics></devices></domain>")


class Sim:
    """Un hôte simulé : `virsh` {arguments: (rc, out, err) ou liste rejouée
    dans l'ordre, la dernière valeur répétée}, `commands` hors virsh,
    `ssh_fail` stderr d'un ssh qui n'aboutit pas."""

    def __init__(self, virsh=None, commands=None, ssh_fail=None, keys=HOST_KEYS):
        self.virsh = dict(INVENTORY)
        self.virsh.update(virsh or {})
        self.commands = dict(BASE_COMMANDS)
        self.commands.update(commands or {})
        self.ssh_fail, self.keys = ssh_fail, keys
        self.calls = []                       # commandes distantes reçues (listes)

    def answer_virsh(self, args):
        v = self.virsh.get(tuple(args), (1, "", "error: unexpected command\n"))
        if isinstance(v, list):
            return v.pop(0) if len(v) > 1 else v[0]
        return v


class FakeNet:
    def __init__(self, sims):
        self.sims = sims                      # adresse -> Sim
        self.argvs = []                       # tous les argv lancés
        self.closed = []                      # adresses dont le ControlMaster a été fermé

    def __call__(self, argv, capture_output=False, text=False, timeout=None, input=None):
        assert capture_output and text and timeout
        self.argvs.append(argv)
        if argv[0] == "ssh-keyscan":
            sim = self.sims.get(argv[-1])
            out = "".join(f"{argv[-1]} {t} {k}\n" for t, k, _ in sim.keys) if sim else ""
            return SimpleNamespace(returncode=0, stdout="# banner\n" + out, stderr="")
        if argv[0] == "ssh-keygen":
            fp = next((f for sim in self.sims.values() for t, k, f in sim.keys if k in input), None)
            return SimpleNamespace(returncode=0 if fp else 1,
                                   stdout=f"256 {fp} host ({'X'})\n" if fp else "", stderr="")
        assert argv[0] == "ssh"
        if argv[1:3] == ["-O", "exit"]:
            self.closed.append(argv[-1])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        assert argv[-3] == "--"
        sim = self.sims[argv[-2]]
        if sim.ssh_fail:
            return SimpleNamespace(returncode=255, stdout="", stderr=sim.ssh_fail)
        rem = shlex.split(argv[-1])
        sim.calls.append(rem)
        if rem[:2] == ["sh", "-c"]:
            return self.batch(sim, rem[2])
        if rem[:5] == ["env", "LC_ALL=C", "virsh", "-c", URI]:
            rc, out, err = sim.answer_virsh(rem[5:])
        else:
            rc, out, err = sim.commands.get(tuple(rem), (127, "", f"sh: {rem[0]}: command not found\n"))
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    @staticmethod
    def batch(sim, script):
        head, body = script.split("\n", 1)
        assert head == "export LC_ALL=C"
        stdout = stderr = ""
        for i, line in enumerate(ln for ln in re.split(r" >&2(?:\n|$)", body) if ln):
            cmd, rest = line.split(" </dev/null; ", 1)
            token = shlex.split(rest.split(";")[0])[2]
            words = shlex.split(cmd)
            assert words[:3] == ["virsh", "-c", URI]
            sim.calls.append(["batch", *words[3:]])
            rc, out, err = sim.answer_virsh(words[3:])
            stdout += f"{out}\n{token} {i} {rc}\n"
            stderr += f"{err}\n{token} {i}\n"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr)


class Clock:
    def __init__(self):
        self.t, self.sleeps = 1000.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Répertoire d'état, réseau simulé et horloge injectés dans le script."""
    net, clock = FakeNet({}), Clock()
    monkeypatch.setattr(cli, "RUNNER", net)
    monkeypatch.setattr(cli, "CLOCK", clock)
    monkeypatch.setattr(cli, "SLEEP", clock.sleep)
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(tmp_path / "state"))
    return SimpleNamespace(state=tmp_path / "state", tmp=tmp_path, net=net, clock=clock)


def declare(env, name="lab", address="10.0.0.5", sim=None, virsh=None, **extra):
    env.net.sims[address] = sim or Sim(virsh=virsh)
    kvm_decl.write_key(env.state, name, PRIVATE_KEY)
    kvm_decl.write_known_hosts(env.state, name, [f"{address} ssh-ed25519 AAAAed"])
    decl = {"name": name, "address": address, "user": "virtops", "ssh_key": kvm_decl.key_rel(name),
            "known_hosts": kvm_decl.known_hosts_rel(name), **extra}
    kvm_decl.write(env.state, decl)
    return env.net.sims[address]


def run(capsys, *argv):
    rc = cli.main(list(argv))
    out, err = capsys.readouterr()
    steps = [ln.split("|", 3)[1:] for ln in err.splitlines() if ln.startswith("STEP_EVENT|")]
    return rc, out, err, steps


def run_json(capsys, *argv):
    rc, out, err, steps = run(capsys, *argv, "--json")
    return rc, (json.loads(out) if out.strip() else None), steps


def virsh_argv(env, name, *args, address="10.0.0.5"):
    decl = kvm_decl.load(env.state, name)
    return kh.ssh_argv(decl, env.state, ["env", "LC_ALL=C", "virsh", "-c", URI, *args])


# --- gestes de cycle de vie ----------------------------------------------

# geste, états successifs de domstate (le premier = avant), état final, secondes
GESTURE_CASES = [
    ("start", ["shut off", "running"], "running", 0),
    ("start", ["crashed", "shut off", "running"], "running", 2),            # libvirt décide
    ("shutdown", ["running", "running", "in shutdown", "shut off"], "shut off", 4),
    ("reboot", ["running", "running"], "running", 2),                       # attend un tour d'abord
    ("reboot", ["running", "shut off", "running"], "running", 4),
    ("destroy", ["running", "shut off"], "shut off", 0),
    ("destroy", ["paused", "shut off"], "shut off", 0),
    ("suspend", ["running", "paused"], "paused", 0),
    ("resume", ["paused", "running"], "running", 0),
]


@pytest.mark.parametrize("action,states,final,seconds", GESTURE_CASES)
def test_gesture_follows_the_real_state(env, capsys, action, states, final, seconds):
    sim = declare(env, virsh={("domstate", "--domain", "web"): [ok(s + "\n\n") for s in states],
                              (action, "--domain", "web"): ok(f"Domain 'web' {action}\n")})
    rc, data, steps = run_json(capsys, "vm", action, "--host", "lab", "--name", "web")
    assert rc == 0
    assert data == {"host": "lab", "name": "web", "action": action, "ok": True,
                    "from": states[0], "state": final, "elapsed_s": float(seconds)}
    # le geste part une fois, avec l'argv construit par ssh_argv
    assert sum(1 for a in env.net.argvs if a == virsh_argv(env, "lab", action, "--domain", "web")) == 1
    assert [c for c in sim.calls if c[5:6] == [action]] == [["env", "LC_ALL=C", "virsh", "-c", URI,
                                                             action, "--domain", "web"]]
    assert set(env.clock.sleeps) <= {cli.POLL_INTERVAL}
    assert ["wait", "done", f"web: {final} after {seconds}s"] in steps
    # chaque changement d'état est dit
    seen = [s[2] for s in steps if s[:2] == ["wait", "running"]]
    assert seen[-1] == f"web: {final}" and len(seen) == len(set(seen))


@pytest.mark.parametrize("action,timeout,stuck,hint", [
    ("shutdown", 120, "running", "use destroy to force it off"),
    ("reboot", 120, "shut off", "did not come back to running"),
    ("start", 30, "shut off", "expected running"),
    ("suspend", 30, "running", "expected paused"),
])
def test_gesture_default_timeouts(env, capsys, action, timeout, stuck, hint):
    before = "running" if action in ("shutdown", "reboot", "suspend") else "shut off"
    seq = [ok(before)] + [ok(stuck)]
    declare(env, virsh={("domstate", "--domain", "web"): seq, (action, "--domain", "web"): ok()})
    rc, data, steps = run_json(capsys, "vm", action, "--host", "lab", "--name", "web")
    assert rc == 1
    assert data["ok"] is False and data["kind"] == "timeout" and data["state"] == stuck
    assert hint in data["error"] and f"after {timeout}s" in data["error"]
    assert steps[-1][:2] == ["wait", "error"]
    assert sum(env.clock.sleeps) == timeout and set(env.clock.sleeps) == {2}


def test_shutdown_timeout_is_tunable(env, capsys):
    declare(env, virsh={("domstate", "--domain", "web"): [ok("running")], ("shutdown", "--domain", "web"): ok()})
    rc, out, err, steps = run(capsys, "vm", "shutdown", "--host", "lab", "--name", "web", "--timeout", "10")
    assert rc == 1 and "still running after 10s" in err and "destroy" in err
    assert sum(env.clock.sleeps) == 10


INVALID = [
    ("resume", "running", "is not paused"),
    ("resume", "shut off", "use start"),
    ("start", "running", "already running"),
    ("start", "paused", "resume it instead"),
    ("shutdown", "shut off", "already shut off"),
    ("shutdown", "paused", "use destroy"),
    ("reboot", "shut off", "use start"),
    ("destroy", "shut off", "already shut off"),
    ("suspend", "paused", "already paused"),
    ("suspend", "shut off", "is shut off"),
]


@pytest.mark.parametrize("action,state,why", INVALID)
def test_gesture_refuses_invalid_transition_without_calling_libvirt(env, capsys, action, state, why):
    sim = declare(env, virsh={("domstate", "--domain", "web"): ok(state)})
    rc, data, steps = run_json(capsys, "vm", action, "--host", "lab", "--name", "web")
    assert rc == 1 and data["kind"] == "invalid" and why in data["error"]
    assert steps[-1][:2] == ["validate", "error"]
    assert [c[5:] for c in sim.calls] == [["domstate", "--domain", "web"]]     # rien d'autre


@pytest.mark.parametrize("where,answers,kind", [
    ("state", {}, "notfound"),
    ("action", {("domstate", "--domain", "web"): ok("running"),
                ("suspend", "--domain", "web"): (1, "", "error: Requested operation is not valid\n")},
     "failed"),
    ("wait", {("domstate", "--domain", "web"): [ok("running"), (1, "", DENIED)],
              ("suspend", "--domain", "web"): ok()}, "denied"),
])
def test_gesture_libvirt_errors(env, capsys, where, answers, kind):
    answers = answers or {("domstate", "--domain", "web"): (1, "", "error: failed to get domain 'web'\n")}
    declare(env, virsh=answers)
    rc, data, steps = run_json(capsys, "vm", "suspend", "--host", "lab", "--name", "web")
    assert rc == 1 and data["kind"] == kind and steps[-1][:2] == [where, "error"]


def test_gesture_unreachable_host(env, capsys):
    declare(env, sim=Sim(ssh_fail=UNREACHABLE))
    rc, data, steps = run_json(capsys, "vm", "start", "--host", "lab", "--name", "web")
    assert rc == 1 and data["kind"] == "unreachable"


@pytest.mark.parametrize("argv,msg", [
    (["vm", "start", "--host", "nope", "--name", "web"], "not declared"),
    (["vm", "start", "--host", "../x", "--name", "web"], "invalid host name"),
    (["vm", "start", "--host", "lab", "--name", ""], "invalid domain name"),
    (["vm", "start", "--host", "lab", "--name", "a\nb"], "invalid domain name"),
    (["vm", "start", "--host", "lab", "--name", "web", "--timeout", "0"], "--timeout"),
    (["vm", "show", "--host", "nope", "--name", "web"], "not declared"),
])
def test_gesture_usage_errors_reach_no_host(env, capsys, argv, msg):
    sim = declare(env)
    rc, out, err, steps = run(capsys, *argv)
    assert rc == 2 and msg in err and sim.calls == []


@pytest.mark.parametrize("name", ["a;rm -rf /", "$(id)", "x y", "-x", "'q'"])
def test_gesture_hostile_names_stay_one_argument(env, capsys, name):
    sim = declare(env, virsh={("domstate", "--domain", name): [ok("shut off"), ok("running")],
                              ("start", "--domain", name): ok()})
    rc, out, err, steps = run(capsys, "vm", "start", "--host", "lab", f"--name={name}")
    assert rc == 0
    assert ["env", "LC_ALL=C", "virsh", "-c", URI, "start", "--domain", name] in sim.calls


def test_follow_state_direct():
    states = iter(["running", "in shutdown", "shut off"])
    host = SimpleNamespace(virsh=lambda *a: next(states) + "\n")
    clock = Clock()
    assert cli.follow_state(host, "web", {"shut off"}, 120, clock=clock, sleep=clock.sleep) == ("shut off", 4)
    with pytest.raises(cli.StateTimeout) as e:
        cli.follow_state(SimpleNamespace(virsh=lambda *a: "running"), "web", {"shut off"}, 5,
                         clock=clock, sleep=clock.sleep)
    assert e.value.state == "running" and e.value.elapsed == 6


# --- host scan / add -----------------------------------------------------

def test_host_scan(env, capsys):
    env.net.sims["10.0.0.5"] = Sim()
    rc, data, steps = run_json(capsys, "host", "scan", "--address", "10.0.0.5")
    assert rc == 0
    assert data == {"address": "10.0.0.5", "port": 22, "keys": [
        {"type": t, "fingerprint": f, "line": f"10.0.0.5 {t} {k}"} for t, k, f in HOST_KEYS]}
    assert env.net.argvs[0] == ["ssh-keyscan", "-T", "10", "-p", "22", "--", "10.0.0.5"]


@pytest.mark.parametrize("argv,rc", [
    (["--address=-oProxyCommand=x"], 2),
    (["--address", "10.0.0.5", "--port", "70000"], 2),
    (["--address", "10.0.0.77"], 1),                        # aucune clé reçue
])
def test_host_scan_errors(env, capsys, argv, rc):
    env.net.sims["10.0.0.5"] = Sim()
    assert run(capsys, "host", "scan", *argv)[0] == rc


def add_argv(env, fingerprint=HOST_KEYS[1][2], name="lab", *extra):
    key = env.tmp / "upload.key"
    if not key.exists():
        key.write_text(PRIVATE_KEY)
    # forme --option=valeur : une valeur qui commence par un tiret reste une valeur
    return ["host", "add", f"--name={name}", "--address=10.0.0.5", "--user=virtops",
            f"--ssh-key={key}", f"--fingerprint={fingerprint}", *extra]


def state_files(env):
    return sorted(str(p.relative_to(env.state)) for p in env.state.rglob("*") if p.is_file()) \
        if env.state.exists() else []


def test_host_add_pins_only_the_matching_key_and_tests(env, capsys):
    sim = Sim()
    env.net.sims["10.0.0.5"] = sim
    rc, data, steps = run_json(capsys, *add_argv(env, HOST_KEYS[1][2], "lab", "--label", "site=paris"))
    assert rc == 0, steps
    assert data["ok"] is True and data["kept"] is True and data["fingerprint"] == HOST_KEYS[1][2]
    assert data["host"] == {"name": "lab", "address": "10.0.0.5", "port": 22, "user": "virtops",
                            "uri": URI, "labels": {"site": "paris"}}
    assert [c["check"] for c in data["checks"]] == list(cli.CHECK_IDS)
    assert state_files(env) == ["kvm.d/lab.yaml", "ssh/lab_id", "ssh/lab_known"]
    assert (env.state / "ssh/lab_known").read_text() == "10.0.0.5 ssh-ed25519 AAAAed\n"
    assert (env.state / "ssh/lab_id").read_text() == PRIVATE_KEY
    # l'ancienne connexion partagée est fermée avant le test
    assert env.net.closed == ["10.0.0.5"] and sim.calls[0] == ["true"]
    # aucun chemin dans la sortie
    assert str(env.state) not in json.dumps(data) and str(env.tmp) not in json.dumps(data)


def test_host_add_fingerprint_mismatch_saves_nothing(env, capsys):
    sim = Sim()
    env.net.sims["10.0.0.5"] = sim
    rc, out, err, steps = run(capsys, *add_argv(env, "SHA256:someoneElse000000000000000000000000000000"))
    assert rc == 1 and "matches none of the host keys" in err
    assert steps[-1][:2] == ["scan", "error"]
    assert state_files(env) == [] and sim.calls == []


@pytest.mark.parametrize("keep", [False, True])
def test_host_add_failed_test_rolls_back_unless_asked(env, capsys, keep):
    sim = Sim(virsh={("version",): (1, "", DENIED)})
    env.net.sims["10.0.0.5"] = sim
    argv = add_argv(env) + (["--keep-on-failure"] if keep else [])
    rc, data, steps = run_json(capsys, *argv)
    assert rc == 1 and data["ok"] is False and data["kept"] is keep
    by = {c["check"]: c for c in data["checks"]}
    assert by["uri"]["status"] == "error" and by["uri"]["kind"] == "denied"
    assert "libvirt group" in by["uri"]["message"]
    assert state_files(env) == (["kvm.d/lab.yaml", "ssh/lab_id", "ssh/lab_known"] if keep else [])


def test_host_add_unexpected_error_during_test_rolls_back(env, capsys, monkeypatch):
    env.net.sims["10.0.0.5"] = Sim()
    monkeypatch.setattr(cli, "run_checks", lambda host: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        cli.main(add_argv(env))
    assert state_files(env) == []


@pytest.mark.parametrize("opt,value,msg", [
    ("--fingerprint", "MD5:aa", "invalid fingerprint"),
    ("--name", "../x", "invalid or missing name"),
    ("--address", "-oProxyCommand=x", "invalid address"),
    ("--user", "-root", "invalid or missing user"),
    ("--ssh-key", "/nonexistent/key", "does not exist"),
    ("--label", "novalue", "invalid label"),
    ("--uri", "xen:///", "uri must be"),
])
def test_host_add_refused_before_the_host(env, capsys, opt, value, msg):
    env.net.sims["10.0.0.5"] = Sim()
    argv = [a for a in add_argv(env) if not a.startswith(opt + "=")] + [f"{opt}={value}"]
    rc, out, err, steps = run(capsys, *argv)
    assert rc == 2 and msg in err and env.net.argvs == [] and state_files(env) == []


def test_host_add_refuses_a_public_key_and_an_existing_host(env, capsys):
    env.net.sims["10.0.0.5"] = Sim()
    pub = env.tmp / "id.pub"
    pub.write_text("ssh-ed25519 AAAA me@x\n")
    argv = [a for a in add_argv(env) if not a.startswith("--ssh-key=")] + [f"--ssh-key={pub}"]
    rc, out, err, _ = run(capsys, *argv)
    assert rc == 2 and "not a private key" in err
    declare(env)
    before = state_files(env)
    rc, out, err, _ = run(capsys, *add_argv(env))
    assert rc == 2 and "already declared" in err and state_files(env) == before


# --- host test -----------------------------------------------------------

def running(*names):
    return ok("".join(n + "\n" for n in names) + "\n")


@pytest.mark.parametrize("sim_kw,rc,expect", [
    ({"virsh": {("list", "--name"): running()}}, 0,
     {"ssh": "ok", "virsh": "ok", "uri": "ok", "qemu-img": "ok", "vnc": "ok"}),
    ({"virsh": {("list", "--name"): running("a", "b"),
                ("dumpxml", "--security-info", "--domain", "a"): ok(vnc_xml("a", "0.0.0.0")),
                ("dumpxml", "--security-info", "--domain", "b"): ok(vnc_xml("b"))}}, 0,
     {"ssh": "ok", "virsh": "ok", "uri": "ok", "qemu-img": "ok", "vnc": "warn"}),
    ({"virsh": {("list", "--name"): running("a"),
                ("dumpxml", "--security-info", "--domain", "a"): ok(vnc_xml("a", "0.0.0.0", "s3cret"))}}, 0,
     {"ssh": "ok", "virsh": "ok", "uri": "ok", "qemu-img": "ok", "vnc": "ok"}),
    ({"ssh_fail": "ju@10.0.0.5: Permission denied (publickey).\n"}, 1,
     {"ssh": "error", "virsh": "skipped", "uri": "skipped", "qemu-img": "skipped", "vnc": "skipped"}),
    ({"commands": {("env", "LC_ALL=C", "virsh", "--version"): (127, "", "sh: virsh: command not found\n")}}, 1,
     {"ssh": "ok", "virsh": "error", "uri": "skipped", "qemu-img": "skipped", "vnc": "skipped"}),
    ({"virsh": {("version",): (1, "", DENIED)}}, 1,
     {"ssh": "ok", "virsh": "ok", "uri": "error", "qemu-img": "skipped", "vnc": "skipped"}),
    ({"commands": {("qemu-img", "--version"): (127, "", "sh: qemu-img: command not found\n")},
      "virsh": {("list", "--name"): running()}}, 0,
     {"ssh": "ok", "virsh": "ok", "uri": "ok", "qemu-img": "warn", "vnc": "ok"}),
    ({"virsh": {("list", "--name"): running("a"),
                ("dumpxml", "--security-info", "--domain", "a"): (1, "", "error: failed to get domain 'a'\n")}}, 0,
     {"ssh": "ok", "virsh": "ok", "uri": "ok", "qemu-img": "ok", "vnc": "warn"}),
])
def test_host_test_one_result_per_check(env, capsys, sim_kw, rc, expect):
    declare(env, sim=Sim(**sim_kw))
    code, data, steps = run_json(capsys, "host", "test", "lab")
    assert code == rc and data["ok"] is (rc == 0) and data["host"] == "lab"
    assert {c["check"]: c["status"] for c in data["checks"]} == expect
    assert [c["check"] for c in data["checks"]] == list(cli.CHECK_IDS)
    for c in data["checks"]:
        assert set(c) >= {"check", "status", "message"}
        assert [f"check-{c['check']}", c["status"], " ".join(c["message"].split())] in steps


def test_host_test_vnc_warning_names_the_domain_and_hides_the_password(env, capsys):
    declare(env, sim=Sim(virsh={("list", "--name"): running("open", "safe"),
                                ("dumpxml", "--security-info", "--domain", "open"): ok(vnc_xml("open", "0.0.0.0")),
                                ("dumpxml", "--security-info", "--domain", "safe"):
                                    ok(vnc_xml("safe", "0.0.0.0", "s3cret", 5901))}))
    rc, out, err, _ = run(capsys, "host", "test", "lab", "--json")
    vnc = json.loads(out)["checks"][-1]
    assert vnc["exposed"] == 1 and "open (vnc on 0.0.0.0:5900)" in vnc["message"] and "safe" not in vnc["message"]
    assert vnc["domains"] == [{"domain": "open", "type": "vnc", "listen": "0.0.0.0", "port": 5900, "password": False},
                              {"domain": "safe", "type": "vnc", "listen": "0.0.0.0", "port": 5901, "password": True}]
    assert "s3cret" not in out + err


def test_host_test_uses_ssh_argv(env, capsys):
    declare(env, sim=Sim(virsh={("list", "--name"): running()}))
    run(capsys, "host", "test", "lab")
    decl = kvm_decl.load(env.state, "lab")
    assert env.net.argvs[0] == kh.ssh_argv(decl, env.state, ["true"])
    assert kh.ssh_argv(decl, env.state, ["qemu-img", "--version"]) in env.net.argvs


# --- host list / remove --------------------------------------------------

def test_host_list_and_remove(env, capsys):
    declare(env, "a", "10.0.0.5", labels={"site": "x"})
    declare(env, "b", "10.0.0.6")
    (env.state / "kvm.d" / "broken.yaml").write_text("{")
    rc, data, _ = run_json(capsys, "host", "list")
    assert rc == 0 and [h["name"] for h in data["hosts"]] == ["a", "b"]
    assert data["hosts"][0] == {"name": "a", "address": "10.0.0.5", "port": 22, "user": "virtops",
                                "uri": URI, "labels": {"site": "x"}}
    assert len(data["warnings"]) == 1 and "broken.yaml" in data["warnings"][0]
    rc, data, steps = run_json(capsys, "host", "remove", "a")
    assert rc == 0 and data == {"host": "a", "removed": True} and env.net.closed == ["10.0.0.5"]
    assert not (env.state / "ssh/a_id").exists() and not (env.state / "ssh/a_known").exists()
    assert run(capsys, "host", "remove", "a")[0] == 2


# --- inventaire et VMs ---------------------------------------------------

INV_KEYS = {"name", "hostname", "address", "labels", "cpu", "memory", "libvirt_version",
            "qemu_version", "vms", "errors"}
VM_KEYS = {"host", "name", "id", "state", "persistent", "autostart", "vcpus", "memory_kib",
           "memory_max_kib", "cpu_time_ns"}


def test_inventory_all_hosts_one_failing(env, capsys):
    declare(env, "a", "10.0.0.5")
    declare(env, "b", "10.0.0.9", sim=Sim(ssh_fail=UNREACHABLE))
    declare(env, "c", "10.0.0.7", sim=Sim(virsh={("version",): (1, "", DENIED), **{
        k: (1, "", DENIED) for k in INVENTORY}}))
    rc, data, _ = run_json(capsys, "inventory")
    assert rc == 0
    by = {h["name"]: h for h in data["hosts"]}
    assert [h["name"] for h in data["hosts"]] == ["a", "b", "c"]
    assert set(by["a"]) == INV_KEYS and set(by["a"]["vms"][0]) == VM_KEYS
    assert len(by["a"]["vms"]) == 15 and by["a"]["libvirt_version"] == "12.7.0"
    assert by["b"] == {"name": "b", "address": "10.0.0.9",
                       "error": {"kind": "unreachable", "message": by["b"]["error"]["message"]}}
    assert by["c"]["error"]["kind"] == "denied"


def test_inventory_reads_hosts_in_parallel(env, capsys, monkeypatch):
    import threading
    barrier = threading.Barrier(2, timeout=5)
    declare(env, "a", "10.0.0.5")
    declare(env, "b", "10.0.0.6")
    real = cli.kh.host_inventory

    def waiting(host, details=False):
        barrier.wait()                           # bloquerait si les hôtes étaient lus l'un après l'autre
        return real(host, details)
    monkeypatch.setattr(cli.kh, "host_inventory", waiting)
    rc, data, _ = run_json(capsys, "inventory")
    assert rc == 0 and all("error" not in h for h in data["hosts"])


def test_inventory_single_host_and_details(env, capsys):
    declare(env, "a", "10.0.0.5", sim=Sim(virsh={
        ("pool-list", "--all", "--details"): ok(fx("pool-list-details.txt")),
        ("net-list", "--all"): ok(fx("net-list.txt")),
        **{("vol-list", "--pool", p, "--details"): ok(fx("vol-list-images.txt"))
           for p in ("bmcfg", "default", "hardpipe-test", "harvlab", "harvlab2", "images", "mlm-lab",
                     "vmware-lab", "xen-bench")}}))
    declare(env, "b", "10.0.0.9", sim=Sim(ssh_fail=UNREACHABLE))
    rc, data, _ = run_json(capsys, "inventory", "--host", "a", "--details")
    assert rc == 0 and [h["name"] for h in data["hosts"]] == ["a"]
    assert set(data["hosts"][0]) == INV_KEYS | {"pools", "networks"}
    assert all("volumes" in p for p in data["hosts"][0]["pools"])
    assert run(capsys, "inventory", "--host", "b")[0] == 1
    assert run(capsys, "inventory", "--host", "zz")[0] == 2


def test_inventory_no_host(env, capsys):
    rc, data, _ = run_json(capsys, "inventory")
    assert rc == 0 and data == {"hosts": []}


def test_vm_list_flattens_and_reports_hosts(env, capsys):
    declare(env, "a", "10.0.0.5")
    declare(env, "b", "10.0.0.9", sim=Sim(ssh_fail=UNREACHABLE))
    rc, data, _ = run_json(capsys, "vm", "list")
    assert rc == 0
    assert data["hosts"][0] == {"name": "a", "state": "ok", "count": 15}
    assert data["hosts"][1]["name"] == "b" and data["hosts"][1]["state"] == "unreachable"
    assert len(data["vms"]) == 15 and all(set(v) == VM_KEYS and v["host"] == "a" for v in data["vms"])
    assert [v["name"] for v in data["vms"]] == sorted(v["name"] for v in data["vms"])
    rc, out, err, _ = run(capsys, "vm", "list")
    assert out.splitlines()[0].split() == ["HOST", "NAME", "STATE", "VCPUS", "MEMORY"]
    assert "host b: unreachable" in out
    assert run(capsys, "vm", "list", "--host", "b")[0] == 1


def test_vm_show(env, capsys):
    declare(env, sim=Sim(virsh={("dumpxml", "--domain", "netperf"): ok(fx("dumpxml-netperf.xml")),
                                ("dominfo", "--domain", "netperf"): ok(fx("dominfo-netperf.txt")),
                                ("domstats", "--raw", *kh.DOMSTATS_GROUPS, "--domain", "netperf"): ok("")}))
    rc, data, _ = run_json(capsys, "vm", "show", "--host", "lab", "--name", "netperf")
    assert rc == 0 and data["name"] == "netperf" and data["host"] == "lab" and data["state"] == "shut off"
    assert {"disks", "nics", "graphics", "cpu_pinned", "persistent", "autostart", "stats"} <= set(data)
    rc, out, err, steps = run(capsys, "vm", "show", "--host", "lab", "--name", "ghost")
    assert rc == 1 and steps[-1][:2] == ["show", "error"]
