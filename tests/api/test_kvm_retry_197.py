"""v1.97.0 : reprise d'une LECTURE coupée au niveau du transport (kvm_host).

Vu en réel sur node2 (OpenSSH 10.2p1) : deux lectures simultanées sur le
même ControlMaster ont fait tomber le sshd de l'hôte (« mm_request_receive:
read: bad msg_len »), le maître est mort, la lecture en vol a rendu 255 sans
repère de départ. Une lecture (`idempotent=True`) est rejouée sur une
connexion neuve ; une écriture (défaut) jamais : son issue est inconnue.

Le faux `runner` rejoue une suite de réponses : coupure, coupure en cours de
lot, réussite, empreinte refusée, hôte sans route.
"""

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
import kvm_host as kh  # noqa: E402
import kvm_ops as ko  # noqa: E402

DECL = {"name": "node2", "address": "172.16.1.12", "port": 22, "user": "ju",
        "ssh_key": "ssh/node2_id", "known_hosts": "ssh/node2_known", "uri": "qemu:///system"}

# sorties d'erreur de ssh relevées (ou de la même famille) pour chaque cas
LOST = "Connection to 172.16.1.12 closed by remote host.\n"
MUX = "mux_client_request_session: read from master failed: Broken pipe\n"
RESET = "Read from remote host 172.16.1.12: Connection reset by peer\n"
HOSTKEY = "Host key verification failed.\n"
AUTH = "ju@172.16.1.12: Permission denied (publickey).\n"
NOROUTE = "ssh: connect to host 172.16.1.12 port 22: No route to host\n"


class Scripted:
    """Runner ssh qui joue une réponse par appel : "ok", "lost" (255 sans rien
    lancer, stderr `LOST`), "mux", "reset", "hostkey", "auth", "noroute",
    "cut" (lot commencé puis coupé après la première commande), "late" (lot
    complet mais ssh rend 255 à la fermeture), "remote1" (commande distante
    en échec, code 1)."""

    def __init__(self, plan):
        self.plan = list(plan)
        self.calls = []

    def __call__(self, argv, stdin=None, input=None, capture_output=False, text=False,
                 timeout=None):
        self.calls.append(argv)
        what = self.plan.pop(0) if self.plan else "ok"
        err = {"lost": LOST, "mux": MUX, "reset": RESET, "hostkey": HOSTKEY, "auth": AUTH,
               "noroute": NOROUTE}
        if what in err:
            return SimpleNamespace(returncode=255, stdout="", stderr=err[what])
        if what == "remote1":
            return SimpleNamespace(returncode=1, stdout="", stderr="error: boom\n")
        remote = shlex.split(argv[-1])
        if remote[:2] != ["sh", "-c"]:
            return SimpleNamespace(returncode=0, stdout="running\n", stderr="")
        script = remote[2]
        token = next(ln[2:] for ln in script.split("\n") if ln.startswith("T="))
        n = sum(1 for ln in script.split("\n") if ln.startswith(("_kvm_run ", "_r ")))
        if "@@kvmops-" in token:                 # lot shell de kvm_ops (_sh_batch)
            out = "".join(f"ok{i}\n{token} {i} 0\n" for i in range(n))
            if what == "cut":
                return SimpleNamespace(returncode=255, stdout=out.split(f"{token} 1 ")[0],
                                       stderr=LOST)
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        out, errs = f"{token} start 1\n", f"{token} start\n"
        for i in range(n):
            if what == "cut" and i >= 1:
                return SimpleNamespace(returncode=255, stdout=out, stderr=errs + LOST)
            out += f"out{i}\n\n{token} {i} 0\n"
            errs += f"\n{token} {i}\n"
        return SimpleNamespace(returncode=255 if what == "late" else 0, stdout=out,
                               stderr=errs + (LOST if what == "late" else ""))


@pytest.fixture
def state():
    d = tempfile.mkdtemp(prefix="kvm-", dir="/tmp")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


def make(state, plan, **kw):
    runner, slept = Scripted(plan), []
    h = kh.VirshHost(DECL, state, runner=runner, sleep=slept.append, **kw)
    return h, runner, slept


def fresh(argv):
    """Vrai si l'appel ssh part hors du ControlMaster (connexion neuve)."""
    return "ControlMaster=no" in argv and "ControlPath=none" in argv


# --- run / virsh ----------------------------------------------------------------

@pytest.mark.parametrize("plan,idempotent,calls,ok", [
    # lecture coupée puis réussie : réussite, second appel sur connexion neuve
    (["lost", "ok"], True, 2, True),
    (["mux", "ok"], True, 2, True),
    (["reset", "ok"], True, 2, True),
    # deuxième coupure : erreur, pas de troisième essai (une reprise par défaut)
    (["lost", "lost"], True, 2, False),
    # écriture coupée : erreur tout de suite, jamais rejouée
    (["lost", "ok"], False, 1, False),
    (["mux", "ok"], False, 1, False),
    # pas des coupures : rejouer ne changerait rien ou ferait attendre
    (["hostkey", "ok"], True, 1, False),
    (["auth", "ok"], True, 1, False),
    (["noroute", "ok"], True, 1, False),
    # la commande distante a échoué (pas ssh) : jamais rejouée
    (["remote1", "ok"], True, 1, False),
])
def test_virsh_retries_only_a_read_cut_by_the_transport(state, plan, idempotent, calls, ok):
    h, runner, slept = make(state, plan)
    if ok:
        assert h.virsh("domstate", "--domain", "web", idempotent=idempotent) == "running\n"
    else:
        with pytest.raises(kh.KvmError):
            h.virsh("domstate", "--domain", "web", idempotent=idempotent)
    assert len(runner.calls) == calls
    assert not fresh(runner.calls[0])
    assert all(fresh(a) for a in runner.calls[1:])
    assert slept == [0.5] * (calls - 1)


def test_virsh_defaults_to_a_write(state):
    h, runner, _ = make(state, ["lost", "ok"])
    with pytest.raises(kh.KvmError) as e:
        h.virsh("snapshot-revert", "--domain", "web", "--snapshotname", "s1")
    assert len(runner.calls) == 1 and e.value.rc == 255


@pytest.mark.parametrize("retries,calls,slept", [
    (0, 1, []), (2, 3, [0.5, 1.0]),
    # borné : jamais plus de MAX_READ_RETRIES reprises
    (99, 1 + kh.MAX_READ_RETRIES, [0.5 * i for i in range(1, kh.MAX_READ_RETRIES + 1)]),
])
def test_read_retries_are_tunable_and_bounded(state, retries, calls, slept):
    h, runner, waits = make(state, ["lost"] * 10, read_retries=retries)
    with pytest.raises(kh.KvmError):
        h.run(["true"], idempotent=True)
    assert len(runner.calls) == calls and waits == slept


def test_timeout_is_never_retried(state):
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, 30)
    h = kh.VirshHost(DECL, state, runner=runner, sleep=lambda s: None)
    with pytest.raises(kh.KvmError) as e:
        h.virsh("list", idempotent=True)
    assert e.value.kind == "timeout" and len(calls) == 1


@pytest.mark.parametrize("rc,stderr,lost", [
    (255, LOST, True), (255, MUX, True), (255, RESET, True), (255, "", True),
    (255, "ssh: connect to host x port 22: Connection refused\n", True),
    (255, HOSTKEY, False), (255, AUTH, False), (255, NOROUTE, False),
    (255, "ssh: Could not resolve hostname x: Name or service not known\n", False),
    (255, "ssh: connect to host x port 22: Connection timed out\n", False),
    (1, LOST, False), (0, "", False),
])
def test_transport_lost(rc, stderr, lost):
    assert kh.transport_lost(rc, stderr) is lost


# --- batch ---------------------------------------------------------------------

@pytest.mark.parametrize("plan,idempotent,calls,complete", [
    (["lost", "ok"], True, 2, True),          # rien lancé, puis réussite
    (["cut", "ok"], True, 2, True),           # coupé après la 1re commande, rejoué entier
    (["cut", "cut"], True, 2, False),         # deuxième coupure : le manque est dit
    (["cut", "ok"], False, 1, False),         # lot qui écrit : jamais rejoué
    (["late", "ok"], True, 1, True),          # tout est arrivé : rien à rejouer
])
def test_batch_retries_only_a_read_batch(state, plan, idempotent, calls, complete):
    h, runner, _ = make(state, plan)
    out = h.batch({"a": ("list",), "b": ("dominfo", "--domain", "web")}, idempotent=idempotent)
    assert len(runner.calls) == calls
    assert all(fresh(a) for a in runner.calls[1:])
    assert out["a"] == "out0\n"
    if complete:
        assert out["b"] == "out1\n"
    else:
        assert not out["b"].ok and "connection lost" in out["b"].error.message


def test_batch_nothing_run_twice_raises(state):
    h, runner, _ = make(state, ["lost", "lost"])
    with pytest.raises(kh.KvmError) as e:
        h.batch({"a": ("list",)}, idempotent=True)
    assert len(runner.calls) == 2 and e.value.rc == 255


@pytest.mark.parametrize("plan,idempotent,calls", [
    (["cut", "ok"], True, 2), (["cut", "ok"], False, 1)])
def test_sh_batch_passes_the_mode(state, plan, idempotent, calls):
    h, runner, _ = make(state, plan)
    if idempotent:
        assert ko._sh_batch(h, [["stat", "a"], ["stat", "b"]], idempotent=True)[1] == (0, "ok1")
    else:
        with pytest.raises(kh.KvmError):
            ko._sh_batch(h, [["stat", "a"], ["stat", "b"]])
    assert len(runner.calls) == calls


# --- lectures de la bibliothèque : rejouées ; écritures : non -------------------

def test_list_snapshots_survives_a_lost_master(state):
    """Le cas vu sur node2 : la relecture des instantanés coupée net."""
    h, runner, _ = make(state, ["lost"])
    calls = []
    real = h.batch

    def spy(commands, timeout=None, cmd_timeout=None, idempotent=False):
        calls.append(idempotent)
        return real(commands, timeout, cmd_timeout, idempotent)
    h.batch = spy
    with pytest.raises(Exception):
        # le faux hôte ne rend pas de vraies sorties virsh : seule compte la reprise
        ko.list_snapshots(h, "web")
    assert calls == [True] and len(runner.calls) == 2 and fresh(runner.calls[1])


@pytest.mark.parametrize("call", [
    lambda h: kh.vm_detail(h, "web"),
    lambda h: kh.host_inventory(h),
])
def test_inventory_and_detail_are_retried_reads(state, call):
    h, runner, _ = make(state, ["lost"])
    try:
        call(h)
    except Exception:                    # noqa: BLE001 : sorties factices, seule la reprise compte
        pass
    assert len(runner.calls) >= 2 and fresh(runner.calls[1])


@pytest.mark.parametrize("call", [
    lambda h: h.virsh("snapshot-create-as", "--domain", "web", "--name", "s1", "--atomic"),
    lambda h: h.run_input(["env", "LC_ALL=C", "virsh", "define", "/dev/stdin"], "<domain/>"),
    lambda h: h.run(["rm", "-f", "--", "/x"]),
])
def test_mutations_are_never_retried(state, call):
    h, runner, _ = make(state, ["lost", "ok"])
    with pytest.raises(kh.KvmError):
        call(h)
    assert len(runner.calls) == 1
