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
import shutil
import subprocess
import sys
import tempfile
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
# repères lus par le faux ssh-keygen -y dans la copie de la clé
ENCRYPTED_KEY = PRIVATE_KEY.replace("AAAAfake", "AAAAfake-ENCRYPTED")
BROKEN_KEY = PRIVATE_KEY.replace("AAAAfake", "AAAAfake-BROKEN")
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
    ("domstats", "--raw", "--nowait", "--state", "--cpu-total", "--balloon", "--vcpu"):
        ok(fx("domstats-raw.txt")),
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
        self.closed_paths = []                # et le ControlPath utilisé
        self.key_checks = []                  # contenus vus par ssh-keygen -y

    def __call__(self, argv, capture_output=False, text=False, timeout=None, input=None,
                 stdin=None):
        assert capture_output and text and timeout
        # ssh, ssh-keyscan et ssh-keygen -y ne lisent jamais l'entrée de la console
        assert argv[:2] == ["ssh-keygen", "-l"] or stdin is subprocess.DEVNULL
        self.argvs.append(argv)
        if argv[0] == "ssh-keyscan":
            sim = self.sims.get(argv[-1])
            # forme réelle du relevé : `[adresse]:port` hors du port 22
            port = argv[argv.index("-p") + 1]
            field = argv[-1] if port == "22" else f"[{argv[-1]}]:{port}"
            out = "".join(f"{field} {t} {k}\n" for t, k, _ in sim.keys) if sim else ""
            return SimpleNamespace(returncode=0, stdout="# banner\n" + out, stderr="")
        if argv[:2] == ["ssh-keygen", "-y"]:
            assert argv[2:5] == ["-P", "", "-f"]
            data = Path(argv[5]).read_text()
            assert Path(argv[5]).stat().st_mode & 0o077 == 0     # copie privée
            self.key_checks.append(data)
            if "ENCRYPTED" in data:
                return SimpleNamespace(returncode=255, stdout="", stderr=(
                    f'Load key "{argv[5]}": incorrect passphrase supplied to decrypt private key\n'))
            if "BROKEN" in data:
                return SimpleNamespace(returncode=255, stdout="", stderr=f'Load key "{argv[5]}": invalid format\n')
            return SimpleNamespace(returncode=0, stdout="ssh-ed25519 AAAApublicPART\n", stderr="")
        if argv[0] == "ssh-keygen":
            fp = next((f for sim in self.sims.values() for t, k, f in sim.keys if k in input), None)
            return SimpleNamespace(returncode=0 if fp else 1,
                                   stdout=f"256 {fp} host ({'X'})\n" if fp else "", stderr="")
        assert argv[0] == "ssh"
        if argv[1:3] == ["-O", "exit"]:
            self.closed.append(argv[-1])
            self.closed_paths.append(next(a for a in argv if a.startswith("ControlPath=")))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        assert argv[-3] == "--"
        sim = self.sims[argv[-2]]
        fail = sim.ssh_fail
        if isinstance(fail, list):             # échecs rejoués dans l'ordre, None = passe
            fail = fail.pop(0) if fail else None
        if fail:
            return SimpleNamespace(returncode=255, stdout="", stderr=fail)
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
        # même protocole que kvm_host.VirshHost.batch_script : repère de
        # départ, puis `_kvm_run <i> virsh ...` une commande par appel
        assert script.split("\n", 1)[0] == "export LC_ALL=C"
        token = re.search(r"^T=(@@kvm-[0-9a-f]+)$", script, re.M).group(1)
        stdout, stderr = f"{token} start 1\n", f"{token} start\n"
        for chunk in re.split(r"\n_kvm_run (?=\d+ )", script)[1:]:
            words = shlex.split(chunk)
            i, words = int(words[0]), words[1:]
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
    """Répertoire d'état, réseau simulé et horloge injectés dans le script.
    L'état est court : le socket ControlMaster doit tenir en 90 octets."""
    net, clock = FakeNet({}), Clock()
    short = Path(tempfile.mkdtemp(prefix="kvm-", dir="/tmp"))
    monkeypatch.setattr(cli, "RUNNER", net)
    monkeypatch.setattr(cli, "CLOCK", clock)
    monkeypatch.setattr(cli, "SLEEP", clock.sleep)
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(short / "state"))
    yield SimpleNamespace(state=short / "state", tmp=tmp_path, net=net, clock=clock)
    shutil.rmtree(short, ignore_errors=True)


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

# geste, états successifs de domstate (le premier = avant), état final,
# secondes, commande virsh envoyée
GESTURE_CASES = [
    ("start", ["shut off", "running"], "running", 0, "start"),
    ("shutdown", ["running", "running", "in shutdown", "shut off"], "shut off", 4, "shutdown"),
    ("reboot", ["running", "running"], "running", 2, "reboot"),             # attend un tour d'abord
    ("reboot", ["running", "shut off", "running"], "running", 4, "reboot"),
    ("destroy", ["running", "shut off"], "shut off", 0, "destroy"),
    ("destroy", ["paused", "shut off"], "shut off", 0, "destroy"),
    ("destroy", ["crashed", "shut off"], "shut off", 0, "destroy"),        # on_crash=preserve
    ("suspend", ["running", "paused"], "paused", 0, "suspend"),
    ("resume", ["paused", "running"], "running", 0, "resume"),
    # suspendue par l'invité (S3) : réveil par dompmwakeup, pas par resume
    ("resume", ["pmsuspended", "running"], "running", 0, "dompmwakeup"),
]


@pytest.mark.parametrize("action,states,final,seconds,command", GESTURE_CASES)
def test_gesture_follows_the_real_state(env, capsys, action, states, final, seconds, command):
    sim = declare(env, virsh={("domstate", "--domain", "web"): [ok(s + "\n\n") for s in states],
                              (command, "--domain", "web"): ok(f"Domain 'web' {action}\n")})
    rc, data, steps = run_json(capsys, "vm", action, "--host", "lab", "--name", "web")
    assert rc == 0
    assert data == {"host": "lab", "name": "web", "action": action, "ok": True,
                    "from": states[0], "state": final, "elapsed_s": float(seconds),
                    "gone": False, "confirmed": action != "reboot"}
    # le geste part une fois, avec l'argv construit par ssh_argv
    assert sum(1 for a in env.net.argvs if a == virsh_argv(env, "lab", command, "--domain", "web")) == 1
    assert [c[5:] for c in sim.calls if c[5:6] != ["domstate"]] == [[command, "--domain", "web"]]
    assert set(env.clock.sleeps) <= {cli.POLL_INTERVAL}
    done = "web: reboot requested; the domain is running" if action == "reboot" else f"web: {final}"
    assert ["wait", "done", f"{done} after {seconds}s"] in steps
    # chaque changement d'état est dit
    seen = [s[2] for s in steps if s[:2] == ["wait", "running"]]
    assert seen[-1] == f"web: {final}" and len(seen) == len(set(seen))


GESTURE_FAIL_KEYS = {"host", "name", "action", "ok", "from", "state", "suggest", "error", "kind"}


@pytest.mark.parametrize("action,timeout,stuck,hint,suggest", [
    ("shutdown", 120, "running", "use destroy to force it off", ["destroy"]),
    ("reboot", 120, "in shutdown", "use destroy then start to force a restart", ["destroy", "start"]),
    ("reboot", 120, "shut off", "did not come back to running; use start to boot it", ["start"]),
    ("start", 30, "shut off", "expected running", []),
    ("suspend", 30, "running", "expected paused", []),
    ("destroy", 30, "running", "expected shut off", []),
])
def test_gesture_default_timeouts(env, capsys, action, timeout, stuck, hint, suggest):
    before = "running" if action in ("shutdown", "reboot", "suspend", "destroy") else "shut off"
    seq = [ok(before)] + [ok(stuck)]
    declare(env, virsh={("domstate", "--domain", "web"): seq, (action, "--domain", "web"): ok()})
    rc, data, steps = run_json(capsys, "vm", action, "--host", "lab", "--name", "web")
    assert rc == 1
    assert set(data) == GESTURE_FAIL_KEYS
    assert data["ok"] is False and data["kind"] == "timeout" and data["state"] == stuck
    assert data["suggest"] == suggest and data["from"] == before
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
    ("suspend", "pmsuspended", "already suspended by the guest"),
    ("start", "crashed", "use destroy, then start"),
    ("start", "pmsuspended", "resume it instead"),
]


@pytest.mark.parametrize("action,state,why", INVALID)
def test_gesture_refuses_invalid_transition_without_calling_libvirt(env, capsys, action, state, why):
    sim = declare(env, virsh={("domstate", "--domain", "web"): ok(state)})
    rc, data, steps = run_json(capsys, "vm", action, "--host", "lab", "--name", "web")
    assert rc == 1 and data["kind"] == "invalid" and why in data["error"]
    assert set(data) == GESTURE_FAIL_KEYS and data["from"] == state and data["state"] is None
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
    host = SimpleNamespace(virsh=lambda *a, **kw: next(states) + "\n")
    clock = Clock()
    assert cli.follow_state(host, "web", {"shut off"}, 120, clock=clock, sleep=clock.sleep) == ("shut off", 4)
    with pytest.raises(cli.StateTimeout) as e:
        cli.follow_state(SimpleNamespace(virsh=lambda *a, **kw: "running"), "web", {"shut off"}, 5,
                         clock=clock, sleep=clock.sleep)
    # la dernière attente est raccourcie au temps restant : pas de dépassement
    assert e.value.state == "running" and e.value.elapsed == 5 and clock.sleeps[-3:] == [2, 2, 1]


class FlakyHost:
    """Hôte de suivi : `script` = états ou KvmError rejoués dans l'ordre (le
    dernier répété). Un appel qui échoue en `timeout` consomme le délai
    qu'on lui a donné, comme ssh qui attend."""

    def __init__(self, clock, script, timeout=30):
        self.clock, self.script, self.timeout, self.budgets = clock, list(script), timeout, []

    def virsh(self, *args, timeout=None, idempotent=False):
        assert args[0] == "domstate"
        self.budgets.append((timeout, self.clock()))
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, kh.KvmError):
            if item.kind == "timeout":
                self.clock.t += timeout
            raise item
        return item + "\n"


def kerr(kind):
    return kh.KvmError(kind, f"lab: {kind}: simulated", host="lab")


@pytest.mark.parametrize("script,result,warns", [
    # un trou réseau, puis l'état attendu
    ([kerr("unreachable"), "shut off"], ("shut off", 2), 1),
    # deux séries séparées par une lecture réussie : un avertissement chacune
    ([kerr("unreachable"), kerr("timeout"), "running", kerr("unreachable"), "shut off"], None, 2),
    ([kerr("timeout"), "shut off"], None, 1),
])
def test_follow_state_retries_transient_errors(capsys, script, result, warns):
    clock = Clock()
    host = FlakyHost(clock, script)
    got = cli.follow_state(host, "web", {"shut off"}, 120, clock=clock, sleep=clock.sleep)
    assert got[0] == "shut off" and (result is None or got == result)
    err = capsys.readouterr().err
    assert err.count("STEP_EVENT|wait|warn|web: cannot read the state") == warns


@pytest.mark.parametrize("kind", ["denied", "auth", "hostkey", "failed", "notfound"])
def test_follow_state_other_errors_fail_at_once(kind):
    clock = Clock()
    host = FlakyHost(clock, [kerr(kind), "shut off"])
    with pytest.raises(kh.KvmError) as e:
        cli.follow_state(host, "web", {"shut off"}, 120, clock=clock, sleep=clock.sleep)
    assert e.value.kind == kind and len(host.budgets) == 1 and clock.sleeps == []


@pytest.mark.parametrize("kind,timeout", [("timeout", 120), ("timeout", 10), ("unreachable", 7)])
def test_follow_state_respects_the_deadline(kind, timeout):
    clock = Clock()
    start = clock()
    host = FlakyHost(clock, [kerr(kind)])
    with pytest.raises(kh.KvmError) as e:
        cli.follow_state(host, "web", {"shut off"}, timeout, clock=clock, sleep=clock.sleep)
    assert e.value.kind == kind
    # jamais plus que l'échéance, au minimum d'un appel près
    assert clock() - start <= timeout + cli.MIN_CALL_TIMEOUT
    for budget, at in host.budgets:
        # chaque appel reçoit au plus le temps restant (minimum MIN_CALL_TIMEOUT)
        assert budget <= max(cli.MIN_CALL_TIMEOUT, start + timeout - at + 1)
        assert budget <= host.timeout
    assert all(s <= cli.POLL_INTERVAL for s in clock.sleeps)


def test_gesture_survives_a_polling_hiccup(env, capsys):
    declare(env, virsh={("domstate", "--domain", "web"): [
        ok("running"), (255, "", UNREACHABLE), ok("shut off")],
        ("shutdown", "--domain", "web"): ok()})
    # le faux réseau rend 255 comme un ssh qui n'aboutit pas : classé unreachable
    rc, data, steps = run_json(capsys, "vm", "shutdown", "--host", "lab", "--name", "web")
    assert rc == 0 and data["state"] == "shut off"
    assert [s[:2] for s in steps].count(["wait", "warn"]) == 1


@pytest.mark.parametrize("action", ["destroy", "shutdown"])
def test_gesture_transient_domain_gone_is_success(env, capsys, action):
    gone = (1, "", "error: failed to get domain 'web'\n")
    declare(env, virsh={("domstate", "--domain", "web"): [ok("running"), gone],
                        (action, "--domain", "web"): ok()})
    rc, data, steps = run_json(capsys, "vm", action, "--host", "lab", "--name", "web")
    assert rc == 0
    assert data == {"host": "lab", "name": "web", "action": action, "ok": True, "from": "running",
                    "state": None, "elapsed_s": 0.0, "gone": True, "confirmed": True}
    assert steps[-1][:2] == ["wait", "done"] and "no longer exists" in steps[-1][2]


@pytest.mark.parametrize("action", ["suspend", "start", "reboot", "resume"])
def test_gesture_domain_vanishing_otherwise_is_a_failure(env, capsys, action):
    before = {"start": "shut off", "resume": "paused"}.get(action, "running")
    declare(env, virsh={("domstate", "--domain", "web"): [ok(before), (1, "", "error: failed to get domain 'web'\n")],
                        (action, "--domain", "web"): ok()})
    rc, data, steps = run_json(capsys, "vm", action, "--host", "lab", "--name", "web")
    assert rc == 1 and data["kind"] == "notfound" and "not confirmed" in data["error"]


def test_reboot_success_does_not_claim_the_guest_restarted(env, capsys):
    declare(env, virsh={("domstate", "--domain", "web"): [ok("running")], ("reboot", "--domain", "web"): ok()})
    rc, out, err, steps = run(capsys, "vm", "reboot", "--host", "lab", "--name", "web")
    assert rc == 0 and out.strip() == "web: reboot requested; the domain is running"
    rc, data, _ = run_json(capsys, "vm", "reboot", "--host", "lab", "--name", "web")
    assert data["confirmed"] is False and data["ok"] is True


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


@pytest.mark.parametrize("exc,msg", [
    (subprocess.TimeoutExpired("ssh-keyscan", 15), "timed out"),
    (FileNotFoundError(2, "No such file or directory", "ssh-keyscan"), "cannot run ssh-keyscan"),
])
def test_host_scan_failure_is_an_error_not_a_crash(env, capsys, monkeypatch, exc, msg):
    def runner(argv, **kw):
        raise exc
    monkeypatch.setattr(cli, "RUNNER", runner)
    rc, out, err, steps = run(capsys, "host", "scan", "--address", "10.0.0.5")
    assert rc == 1 and msg in err and steps[-1][:2] == ["scan", "error"]


def add_argv(env, fingerprint=HOST_KEYS[1][2], name="lab", *extra):
    key = env.tmp / "upload.key"
    if not key.exists():
        key.write_text(PRIVATE_KEY)
    # forme --option=valeur : une valeur qui commence par un tiret reste une valeur
    return ["host", "add", f"--name={name}", "--address=10.0.0.5", "--user=virtops",
            f"--ssh-key={key}", f"--fingerprint={fingerprint}", *extra]


def state_files(env):
    # le fichier de verrou reste (vide) : ce n'est pas une donnée d'hôte
    return sorted(str(p.relative_to(env.state)) for p in env.state.rglob("*")
                  if p.is_file() and p.name != ".lock") if env.state.exists() else []


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
    # le socket fermé est celui de CETTE déclaration (nom dans le chemin)
    cpath = kh.control_path(kvm_decl.load(env.state, "lab"), env.state)
    assert env.net.closed_paths == [f"ControlPath={cpath}"] and cpath.name.startswith("cm-lab-")
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


def boom(*a, **kw):
    raise ZeroDivisionError("/secret/path/inside")


@pytest.mark.parametrize("target", ["run_checks", "close_master"])
@pytest.mark.parametrize("keep", [False, True])
def test_host_add_unexpected_error_rolls_back(env, capsys, monkeypatch, target, keep):
    env.net.sims["10.0.0.5"] = Sim()
    if target == "close_master":
        # la première fermeture (avant le test) échoue ; celle du retrait passe
        real, calls = cli.close_master, []

        def flaky(decl, state):
            calls.append(1)
            if len(calls) == 1:
                boom()
            return real(decl, state)
        monkeypatch.setattr(cli, "close_master", flaky)
    else:
        monkeypatch.setattr(cli, target, boom)
    rc, data, steps = run_json(capsys, *add_argv(env), *(["--keep-on-failure"] if keep else []))
    assert rc == 1
    # erreur imprévue : message générique, ni trace ni chemin
    assert data == {"ok": False, "kind": "failed", "error": "unexpected internal error (ZeroDivisionError)"}
    assert state_files(env) == (["kvm.d/lab.yaml", "ssh/lab_id", "ssh/lab_known"] if keep else [])
    assert steps[-1][:2] == ["internal", "error"]


def test_host_add_rollback_closes_the_master(env, capsys):
    env.net.sims["10.0.0.5"] = Sim(virsh={("version",): (1, "", DENIED)})
    rc, data, _ = run_json(capsys, *add_argv(env))
    assert rc == 1 and data["kept"] is False
    assert data["error"].startswith("connection test failed at uri:") and data["kind"] == "denied"
    # avant le test, puis au retrait : même socket, celui de la déclaration
    assert env.net.closed == ["10.0.0.5", "10.0.0.5"]
    assert len(set(env.net.closed_paths)) == 1 and "cm-lab-" in env.net.closed_paths[0]


@pytest.mark.parametrize("err,reason", [
    (PermissionError(13, "Permission denied", "/tmp/x/state/ssh/lab_id"), "EACCES"),
    (OSError(28, "No space left on device", "/tmp/x/state/kvm.d/lab.yaml"), "ENOSPC"),
    (kvm_decl.DeclError("path /tmp/x/state escapes"), "invalid data"),
])
def test_host_add_save_error_shows_no_path(env, capsys, monkeypatch, err, reason):
    env.net.sims["10.0.0.5"] = Sim()

    def failing(state, decl):
        raise err
    monkeypatch.setattr(cli.kvm_decl, "write", failing)
    rc, out, errout, steps = run(capsys, *add_argv(env), "--json")
    data = json.loads(out)
    assert rc == 1 and data == {"ok": False, "kind": "failed",
                                "error": f"cannot save the host declaration ({reason})"}
    assert "/tmp/x" not in out + errout and str(env.state) not in out + errout
    assert steps[-1] == ["save", "error", f"cannot save the host declaration ({reason})"]
    assert state_files(env) == []


def test_host_add_pins_the_bracketed_line_on_another_port(env, capsys):
    env.net.sims["10.0.0.5"] = Sim()
    rc, data, _ = run_json(capsys, *add_argv(env), "--port=2222")
    assert rc == 0 and data["host"]["port"] == 2222
    assert (env.state / "ssh/lab_known").read_text() == "[10.0.0.5]:2222 ssh-ed25519 AAAAed\n"


@pytest.mark.parametrize("key,rc,kind,msg", [
    (ENCRYPTED_KEY, 2, "auth", "the key is protected by a passphrase; provide an unencrypted key"),
    (BROKEN_KEY, 2, "usage", "the SSH key file is not a valid private key"),
])
def test_host_add_checks_the_key_before_the_network(env, capsys, key, rc, kind, msg):
    env.net.sims["10.0.0.5"] = Sim()
    path = env.tmp / "upload.key"
    path.write_text(key)
    path.chmod(0o644)                          # ssh-keygen lit une copie 0600
    code, out, err, steps = run(capsys, *add_argv(env), "--json")
    data = json.loads(out)
    assert code == rc and data["kind"] == kind and data["error"].startswith(msg)
    # seul ssh-keygen -y a tourné, sur une copie supprimée ensuite
    assert [a[:2] for a in env.net.argvs] == [["ssh-keygen", "-y"]]
    assert not Path(env.net.argvs[0][5]).exists() and env.net.key_checks == [key]
    assert "AAAAfake" not in out + err and str(env.tmp) not in out + err and state_files(env) == []


def test_host_add_never_shows_the_key(env, capsys):
    env.net.sims["10.0.0.5"] = Sim()
    rc, out, err, _ = run(capsys, *add_argv(env), "--json")
    assert rc == 0 and env.net.argvs[0][:2] == ["ssh-keygen", "-y"]
    assert "AAAAfake" not in out + err and "AAAApublicPART" not in out + err


@pytest.mark.parametrize("exc,kind", [
    (subprocess.TimeoutExpired("ssh-keygen", 10), "timeout"),
    (FileNotFoundError(2, "No such file or directory", "ssh-keygen"), "failed"),
])
def test_host_add_key_check_cannot_run(env, capsys, monkeypatch, exc, kind):
    def runner(argv, **kw):
        raise exc
    monkeypatch.setattr(cli, "RUNNER", runner)
    rc, data, _ = run_json(capsys, *add_argv(env))
    assert rc == 1 and data["kind"] == kind and state_files(env) == []


def test_host_add_and_remove_take_the_lock(env, capsys):
    import fcntl
    import os
    env.net.sims["10.0.0.5"] = Sim()
    d = kvm_decl.decl_dir(env.state)
    d.mkdir(parents=True, mode=0o700)
    fd = os.open(d / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        rc, data, _ = run_json(capsys, *add_argv(env))
        assert rc == 2 and data["kind"] == "busy" and "in progress" in data["error"]
        assert env.net.sims["10.0.0.5"].calls == [] and state_files(env) == []
    finally:
        os.close(fd)
    assert run(capsys, *add_argv(env))[0] == 0
    fd = os.open(d / ".lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        rc, data, _ = run_json(capsys, "host", "remove", "lab")
        assert rc == 2 and data["kind"] == "busy" and (env.state / "ssh/lab_id").exists()
    finally:
        os.close(fd)
    assert run(capsys, "host", "remove", "lab")[0] == 0
    # le verrou n'est pas pris pour une déclaration (glob *.yaml)
    assert run_json(capsys, "host", "list")[1]["warnings"] == []


@pytest.mark.parametrize("opt,value,msg", [
    ("--fingerprint", "MD5:aa", "invalid fingerprint"),
    ("--name", "../x", "invalid or missing name"),
    ("--address", "-oProxyCommand=x", "invalid address"),
    ("--user", "-root", "invalid or missing user"),
    ("--ssh-key", "/nonexistent/key", "does not exist"),
    ("--label", "novalue", "invalid label"),
    ("--uri", "xen:///", "uri must be"),
    ("--uri", "qemu+ext:///system?command=/tmp/x", "uri must be"),
])
def test_host_add_refused_before_the_host(env, capsys, opt, value, msg):
    env.net.sims["10.0.0.5"] = Sim()
    argv = [a for a in add_argv(env) if not a.startswith(opt + "=")] + [f"{opt}={value}"]
    rc, out, err, steps = run(capsys, *argv)
    assert rc == 2 and msg in err and env.net.argvs == [] and state_files(env) == []


@pytest.mark.parametrize("state_dir,msg", [
    ("/tmp/" + "s" * 80, "state directory takes 85 bytes"),
    ("/tmp/kvm%Cstate", "must not contain '%'"),
])
def test_host_add_refuses_a_state_dir_ssh_cannot_use(env, capsys, monkeypatch, state_dir, msg):
    env.net.sims["10.0.0.5"] = Sim()
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", state_dir)
    rc, out, err, steps = run(capsys, *add_argv(env))
    assert rc == 2 and msg in err and env.net.argvs == [] and not Path(state_dir).exists()


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


BASE_CHECK = {"check", "status", "message", "kind"}
CHECK_KEYS = {"ssh": BASE_CHECK, "virsh": BASE_CHECK | {"version"},
              "uri": BASE_CHECK | {"libvirt", "qemu"}, "qemu-img": BASE_CHECK,
              "vnc": BASE_CHECK | {"domains", "exposed"}}


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
    assert set(data) == {"host", "ok", "error", "kind", "checks"}
    assert (data["error"] is None) is (rc == 0) and (data["kind"] is None) is (rc == 0)
    for c in data["checks"]:
        # chaque point a toujours les mêmes clés, quel que soit son statut
        assert set(c) == CHECK_KEYS[c["check"]]
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


def vnc_xml_sub(name, listen_el):
    return (f"<domain type='kvm'><name>{name}</name><devices><graphics type='vnc' port='5900'>"
            f"{listen_el}</graphics></devices></domain>")


@pytest.mark.parametrize("xml,exposed", [
    (vnc_xml("d", "127.0.0.1"), False),
    (vnc_xml("d", "127.0.1.1"), False),                   # 127.0.0.0/8
    (vnc_xml("d", "::1"), False),
    (vnc_xml("d", "[::1]"), False),
    (vnc_xml("d", "localhost"), False),
    (vnc_xml_sub("d", "<listen type='socket' socket='/run/libvirt/qemu/d.sock'/>"), False),
    (vnc_xml_sub("d", "<listen type='none'/>"), False),
    (vnc_xml("d", "0.0.0.0"), True),
    (vnc_xml("d", "::"), True),
    (vnc_xml("d", "172.16.1.12"), True),                  # adresse du LAN : exposé
    (vnc_xml("d", "fd00::12"), True),
    (vnc_xml("d", "node2.home.lo"), True),                # nom d'hôte : exposé
    (vnc_xml("d", "172.16.1.12", "s3cret"), False),       # mot de passe : pas d'alerte
])
def test_vnc_check_flags_any_non_loopback_listen(env, capsys, xml, exposed):
    declare(env, sim=Sim(virsh={("list", "--name"): running("d"),
                                ("dumpxml", "--security-info", "--domain", "d"): ok(xml)}))
    rc, data, _ = run_json(capsys, "host", "test", "lab")
    vnc = data["checks"][-1]
    assert rc == 0 and vnc["exposed"] == int(exposed)
    assert vnc["status"] == ("warn" if exposed else "ok")
    assert ("security:" in vnc["message"]) is exposed and "s3cret" not in json.dumps(data)


@pytest.mark.parametrize("sim_kw,status", [
    ({"virsh": {("list", "--name"): running()}}, "ok"),
    ({"virsh": {("list", "--name"): (1, "", "error: unexpected\n")}}, "warn"),
    ({"ssh_fail": UNREACHABLE}, "skipped"),
])
def test_vnc_check_always_has_its_keys(env, capsys, sim_kw, status):
    declare(env, sim=Sim(**sim_kw))
    vnc = run_json(capsys, "host", "test", "lab")[1]["checks"][-1]
    assert vnc["status"] == status and vnc["exposed"] == 0 and vnc["domains"] == []


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
    cpath = kh.control_path(kvm_decl.load(env.state, "a"), env.state)
    rc, data, steps = run_json(capsys, "host", "remove", "a")
    assert rc == 0 and data == {"host": "a", "removed": True} and env.net.closed == ["10.0.0.5"]
    assert env.net.closed_paths == [f"ControlPath={cpath}"]
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


# --- protocole : STEP_EVENT, JSON à chaque sortie, options -------------------

def test_step_event_message_with_pipe_and_newline(capsys):
    cli.step("wait", "warn", "a|b\nc | d")
    err = capsys.readouterr().err
    assert err == "STEP_EVENT|wait|warn|a|b c | d\n"
    # découpe du consommateur : maxsplit=3, le message garde ses `|`
    assert err.rstrip("\n").split("|", 3) == ["STEP_EVENT", "wait", "warn", "a|b c | d"]


@pytest.mark.parametrize("argv,rc,kind,msg", [
    (["vm", "start", "--host", "lab"], 2, "usage", "--name"),                     # option requise
    (["vm", "start", "--host", "lab", "--name", "web", "--timeout", "x"], 2, "usage", "invalid int"),
    (["vm", "frobnicate", "--host", "lab", "--name", "web"], 2, "usage", "invalid choice"),
    (["vm", "start", "--host", "lab", "--name", "web", "--bogus"], 2, "usage", "unrecognized"),
    (["vm", "start", "--host", "nope", "--name", "web"], 2, "usage", "not declared"),
    (["host", "test", "nope"], 2, "usage", "not declared"),
    (["host", "remove", "nope"], 2, "usage", "not declared"),
    (["inventory", "--host", "nope"], 2, "usage", "not declared"),
    (["vm", "show", "--host", "lab", "--name", "ghost"], 1, "notfound", "ghost"),
    (["host", "scan", "--address", "10.0.0.77"], 1, "failed", "no host key received on port 22; "
     "if the host runs OpenSSH 9.8 or later, repeated scans are penalised"),
    (["host", "scan", "--address=-x"], 2, "usage", "invalid address"),
])
def test_json_on_every_failure_path(env, capsys, argv, rc, kind, msg):
    declare(env, virsh={("dumpxml", "--domain", "ghost"): (1, "", "error: failed to get domain 'ghost'\n")})
    code, out, err, steps = run(capsys, *argv, "--json")
    data = json.loads(out)                     # exactement un objet JSON
    assert code == rc and data["ok"] is False and data["kind"] == kind and msg in data["error"]
    assert set(data) == {"ok", "error", "kind"}
    assert "Traceback" not in err


def test_json_on_fingerprint_mismatch(env, capsys):
    env.net.sims["10.0.0.5"] = Sim()
    rc, data, _ = run_json(capsys, *add_argv(env, "SHA256:someoneElse000000000000000000000000000000"))
    assert rc == 1 and data["kind"] == "hostkey" and "matches none" in data["error"]


def test_json_on_unexpected_error_hides_details(env, capsys, monkeypatch):
    declare(env)
    monkeypatch.setattr(cli, "read_all", boom)
    rc, out, err, _ = run(capsys, "inventory", "--json")
    assert rc == 1 and json.loads(out) == {"ok": False, "kind": "failed",
                                           "error": "unexpected internal error (ZeroDivisionError)"}
    assert "/secret/path" not in out + err and "Traceback" not in err


def test_usage_error_without_json_writes_nothing_on_stdout(env, capsys):
    rc, out, err, steps = run(capsys, "vm", "start", "--host", "lab")
    assert rc == 2 and out == "" and "--name" in err and steps[-1][:2] == ["validate", "error"]


@pytest.mark.parametrize("opt,value,msg", [
    # forme --option=valeur : la valeur arrive à NOTRE contrôle, pas à argparse
    ("--label", "-x=y", "label"),
    ("--uri", "-qemu:///system", "uri must be"),
    ("--name", "-lab", "name"),
    ("--user", "-root", "user"),
    ("--fingerprint", "-SHA256:x", "invalid fingerprint"),
])
def test_dash_values_reach_validation(env, capsys, opt, value, msg):
    env.net.sims["10.0.0.5"] = Sim()
    argv = [a for a in add_argv(env) if not a.startswith(opt + "=")] + [f"{opt}={value}"]
    rc, data, _ = run_json(capsys, *argv)
    assert rc == 2 and msg in data["error"] and "expected one argument" not in data["error"]
    assert env.net.argvs == []


def test_dash_label_value_is_kept(env, capsys):
    env.net.sims["10.0.0.5"] = Sim()
    rc, data, _ = run_json(capsys, *add_argv(env), "--label=site=-paris", "--uri=qemu:///system")
    assert rc == 0 and data["host"]["labels"] == {"site": "-paris"}


PENALTY = ("kex_exchange_identification: read: Connection reset by peer\n"
           "Connection reset by 10.0.0.5 port 22\n")


@pytest.mark.parametrize("fails,rc,sleeps", [
    ([PENALTY], 0, [cli.SSH_RETRY_AFTER_SCAN]),                 # pénalité levée : ajouté
    ([PENALTY, PENALTY], 1, [cli.SSH_RETRY_AFTER_SCAN]),        # une seule nouvelle tentative
    (["ju@10.0.0.5: Permission denied (publickey).\n"], 1, []),  # pas d'attente sur un refus de clé
])
def test_host_add_retries_ssh_once_after_the_scan(env, capsys, fails, rc, sleeps):
    # OpenSSH >= 9.8 pénalise les connexions du relevé (PerSourcePenalties)
    env.net.sims["10.0.0.5"] = Sim(ssh_fail=list(fails))
    code, data, steps = run_json(capsys, *add_argv(env))
    assert code == rc and env.clock.sleeps == sleeps
    assert (["check-ssh", "warn"] in [s[:2] for s in steps]) is bool(sleeps)


def test_host_test_does_not_wait_on_unreachable(env, capsys):
    declare(env, sim=Sim(ssh_fail=[PENALTY]))
    rc, data, _ = run_json(capsys, "host", "test", "lab")
    assert rc == 1 and data["kind"] == "unreachable" and env.clock.sleeps == []


# --- annulation (SIGTERM / SIGHUP), copie de contrôle de la clé -------------

def test_host_add_key_check_copy_lives_in_the_private_state_dir(env, capsys):
    env.net.sims["10.0.0.5"] = Sim()
    rc, _, _ = run_json(capsys, *add_argv(env))
    copy = Path(env.net.argvs[0][5])
    assert rc == 0 and copy.parent == env.state / "ssh" and not copy.exists()
    assert (env.state / "ssh").stat().st_mode & 0o777 == 0o700


def test_host_add_key_check_copy_error_is_specific(env, capsys, monkeypatch):
    def refuse(*a, **kw):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(cli.tempfile, "mkstemp", refuse)
    rc, data, _ = run_json(capsys, *add_argv(env))
    assert rc == 1 and data["error"] == "cannot check the SSH key (EACCES)"
    assert env.net.argvs == [] and state_files(env) == []


def test_rollback_survives_a_failing_close_master(env, capsys, monkeypatch):
    sim = Sim(ssh_fail=UNREACHABLE)
    env.net.sims["10.0.0.5"] = sim
    calls = []

    def boom(decl, state):
        calls.append(decl["name"])
        raise OSError("socket vanished")
    monkeypatch.setattr(cli, "close_master", boom)
    monkeypatch.setattr(cli, "run_checks", lambda h, ssh_retry=0: [
        {"check": "ssh", "status": "error", "message": "unreachable", "kind": "unreachable"}])
    rc, data, _ = run_json(capsys, *add_argv(env))
    # close_master échoue deux fois (avant le test, au retour arrière) : la
    # première fait échouer l'ajout, le retour arrière va quand même au bout
    assert rc == 1 and state_files(env) == [] and calls


@pytest.mark.parametrize("cmd,vcmd,want", [
    ("host", "add", "host add cancelled (SIGTERM): nothing saved"),
    ("vm", "shutdown", "shutdown cancelled (SIGTERM): it may already have been requested; "
                       "its result was not followed"),
    ("host", "test", "cancelled (SIGTERM)"),
])
def test_cancel_message_never_claims_an_undo_for_a_gesture(cmd, vcmd, want):
    args = SimpleNamespace(cmd=cmd, hcmd=vcmd, vcmd=vcmd)
    assert cli.cancel_message(args, "SIGTERM") == want


SHIM = {
    # relevé : la clé d'hôte générée pour le test
    "ssh-keyscan": '#!/bin/sh\necho "# banner"\necho "$KVMT_ADDR $(cat "$KVMT_HOSTPUB")"\n',
    # ssh -O exit répond, une vraie connexion reste pendue (hôte qui ne répond plus)
    "ssh": '#!/bin/sh\nfor a in "$@"; do [ "$a" = "-O" ] && exit 0; done\n'
           'touch "$KVMT_MARK"\nexec sleep 30\n',
}


@pytest.mark.parametrize("sig", ["SIGTERM", "SIGHUP"])
def test_a_signal_during_host_add_rolls_back_everything(tmp_path, sig):
    """Vrai processus : `host add` est arrêté par le bouton « Annuler » de la
    console (SIGTERM) pendant le test de connexion, déclaration déjà écrite.
    Rien ne reste : ni déclaration, ni clé, ni empreinte ; code 3 et un objet
    JSON {"ok": false, "kind": "cancelled"}."""
    import os
    import signal
    import time
    if not shutil.which("ssh-keygen"):
        pytest.skip("ssh-keygen absent")
    shim = tmp_path / "bin"
    shim.mkdir()
    for name, body in SHIM.items():
        (shim / name).write_text(body)
        (shim / name).chmod(0o755)
    keys = tmp_path / "keys"
    keys.mkdir()
    for k in ("host", "client"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(keys / k)],
                       check=True, stdin=subprocess.DEVNULL)
    host_pub = keys / "host.pub"
    host_pub.write_text(" ".join(host_pub.read_text().split()[:2]) + "\n")
    fp = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(host_pub)],
                        capture_output=True, text=True, check=True).stdout.split()[1]
    short = Path(tempfile.mkdtemp(prefix="kvmc-", dir="/tmp"))
    state, mark = short / "state", tmp_path / "connected"
    env = dict(os.environ, PATH=f"{shim}:{os.environ['PATH']}", KVMT_ADDR="10.0.0.5",
               KVMT_HOSTPUB=str(host_pub), KVMT_MARK=str(mark))
    try:
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "bin" / "harvester-kvm.py"), "host", "add", "--json",
             f"--state-dir={state}", "--name=lab", "--address=10.0.0.5", "--user=virtops",
             f"--ssh-key={keys / 'client'}", f"--fingerprint={fp}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        deadline = time.monotonic() + 20
        while not mark.exists() and time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.05)
        assert mark.exists(), proc.communicate(timeout=5)
        # le test de connexion tourne : la déclaration et la clé sont écrites
        assert (state / "kvm.d" / "lab.yaml").exists() and (state / "ssh" / "lab_id").exists()
        proc.send_signal(getattr(signal, sig))
        out, err = proc.communicate(timeout=15)
        assert proc.returncode == 3, err
        data = json.loads(out)
        assert data["ok"] is False and data["kind"] == "cancelled"
        assert data["error"] == f"host add cancelled ({sig}): nothing saved"
        left = sorted(p.name for p in state.rglob("*") if p.is_file() and p.name != ".lock")
        assert left == []
        assert "BEGIN" not in out + err and str(state) not in out
    finally:
        if proc.poll() is None:
            proc.kill()
        shutil.rmtree(short, ignore_errors=True)


def test_host_add_refuses_a_name_taken_by_a_console_route(env, capsys):
    env.net.sims["10.0.0.5"] = Sim()
    rc, data, _ = run_json(capsys, *add_argv(env, HOST_KEYS[1][2], "scan"))
    assert rc == 2 and "reserved" in data["error"] and env.net.argvs == []
    assert kvm_decl.check_decl({"name": "scanner", "address": "h", "user": "u",
                                "ssh_key": kvm_decl.key_rel("scanner"),
                                "known_hosts": kvm_decl.known_hosts_rel("scanner")}) is None
