"""Lots de lecture virsh en parallèle sur l'hôte (1.101.2).

Le vrai script de `VirshHost.batch` est joué par un `sh` local (seul ssh est
remplacé), avec un faux `virsh` en tête du PATH : vagues de processus,
fichiers temporaires, repères et `timeout` sont éprouvés pour de vrai. Un lot
qui écrit (`idempotent=False`) reste en série."""

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import kvm_host as kh  # noqa: E402

DECL = {"name": "node2", "address": "172.16.1.12", "port": 22, "user": "ju",
        "ssh_key": "ssh/node2_id", "known_hosts": "ssh/node2_known", "uri": "qemu:///system"}

# faux virsh : « virsh -c URI commande args... ». `sl <s> <x>` : marque son
# passage dans $RUN, écrit combien de commandes tournent en même temps
# qu'elle, dort <s> secondes, puis écrit <x>.
FAKE_VIRSH = r"""#!/bin/sh
shift 2
case "$1" in
  sl) : >"$RUN/run.$3.$$"; set -- "$@" "$RUN"/run.*; n=$(( $# - 3 ))
      sleep "$2"; rm -f "$RUN/run.$3.$$"; echo "$3 $n" ;;
  dominfo) echo "error: failed to get domain '$3'" >&2; exit 1 ;;
  fail) echo "boom $2" >&2; exit 3 ;;
  echo-arg) printf '%s' "$2" ;;
  hang) sleep 30 ;;
  *) echo "error: unknown command '$1'" >&2; exit 1 ;;
esac
"""


class LocalShell:
    """Joue la commande distante d'un argv ssh dans un `sh` local ; garde le
    dernier résultat brut (ordre des repères)."""

    def __init__(self, env):
        self.env, self.calls, self.last, self.scripts = env, 0, None, []
        self.mutate = None

    def __call__(self, argv, stdin=None, capture_output=False, text=False, timeout=None):
        assert argv[0] == "ssh" and argv[-3] == "--" and stdin is subprocess.DEVNULL
        self.calls += 1
        remote = argv[-1]
        if self.mutate:
            remote = self.mutate(remote)
        self.scripts.append(remote)
        self.last = subprocess.run(["/bin/sh", "-c", remote], stdin=stdin, capture_output=True,
                                   text=True, timeout=timeout, env=self.env)
        return self.last


@pytest.fixture
def box():
    """Répertoire court (le socket ControlMaster tient en 90 octets) :
    bin/ (faux virsh), tools/ (sh, cat, sleep, rm : ni mktemp ni timeout),
    run/ (commandes en cours), tmp/ (TMPDIR des répertoires du lot)."""
    d = Path(tempfile.mkdtemp(prefix="kvmp-", dir="/tmp"))
    for sub in ("bin", "tools", "run", "tmp"):
        (d / sub).mkdir()
    (d / "bin" / "virsh").write_text(FAKE_VIRSH)
    (d / "bin" / "virsh").chmod(0o755)
    for tool in ("sh", "cat", "sleep", "rm"):
        os.symlink(shutil.which(tool), d / "tools" / tool)
    yield SimpleNamespace(root=d, bin=d / "bin", tools=d / "tools", run=d / "run", tmp=d / "tmp")
    shutil.rmtree(d, ignore_errors=True)


def shell(box, path=None):
    return LocalShell({"PATH": path or f"{box.bin}:/usr/bin:/bin", "RUN": str(box.run),
                       "TMPDIR": str(box.tmp)})


def host(runner, box, **kw):
    return kh.VirshHost(DECL, str(box.root), runner=runner, timeout=30, **kw)


def marker_order(res, token_re=r"@@kvm-[0-9a-f]{24}"):
    """Indices des repères de fin, dans l'ordre où ils sont sortis
    (stdout, stderr)."""
    out = [int(m) for m in re.findall(rf"\n{token_re} (\d+) -?\d+\n", res.stdout)]
    err = [int(m) for m in re.findall(rf"\n{token_re} (\d+)\n", res.stderr)]
    return out, err


def assert_in_order(res, n):
    out, err = marker_order(res)
    assert out == list(range(n)), out
    assert err == list(range(n)), err


# --- forme du script ---------------------------------------------------------

def script_of(par, n, idempotent=True):
    seen = []

    def runner(argv, **kw):
        seen.append(shlex.split(argv[-1])[2])     # le script, tel que sh le reçoit
        return SimpleNamespace(returncode=255, stdout="", stderr="Connection refused\n")

    h = kh.VirshHost(DECL, "/var/lib/harvester-ops", runner=runner, timeout=30,
                     batch_parallel=par, read_retries=0)
    with pytest.raises(kh.KvmError):
        h.batch([("list", f"--n{i}") for i in range(n)], idempotent=idempotent)
    return seen[0]


@pytest.mark.parametrize("par,n,idem,launch,waves", [
    (8, 20, True, "_kvm_bg", ["0 1 2 3 4 5 6 7", "8 9 10 11 12 13 14 15", "16 17 18 19"]),
    (8, 3, True, "_kvm_bg", ["0 1 2"]),
    (4, 8, True, "_kvm_bg", ["0 1 2 3", "4 5 6 7"]),
    (8, 20, False, "_kvm_run", []),          # écriture : en série, toujours
    (1, 5, True, "_kvm_run", []),            # parallélisme coupé par l'hôte
])
def test_script_waves(par, n, idem, launch, waves):
    s = script_of(par, n, idem)
    lines = s.split("\n")
    launched = [ln.split()[1] for ln in lines if ln.startswith(("_kvm_bg ", "_kvm_run "))]
    assert launched == [str(i) for i in range(n)]
    assert all(ln.startswith(launch + " ") for ln in lines
               if ln.startswith(("_kvm_bg ", "_kvm_run ")))
    assert [ln[len("_kvm_wave "):] for ln in lines if ln.startswith("_kvm_wave ")] == waves
    if not waves:
        assert "mktemp" not in s and "_kvm_wave" not in s and "_kvm_bg" not in s


def test_default_parallelism_is_the_module_constant():
    h = kh.VirshHost(DECL, "/x", runner=None)
    assert kh.BATCH_PARALLEL == 8 and h.batch_parallel == 8
    assert kh.VirshHost(DECL, "/x", runner=None, batch_parallel=0).batch_parallel == 1


# --- le vrai script ----------------------------------------------------------

def test_reads_run_concurrently_and_come_back_in_order(box):
    runner = shell(box)
    n = 12
    # durées décroissantes : la dernière commande finit la première
    cmds = {f"k{i}": ("sl", f"{0.05 * (n - i):.2f}", f"x{i}") for i in range(n)}
    t0 = time.monotonic()
    out = host(runner, box, batch_parallel=4).batch(cmds, idempotent=True)
    elapsed = time.monotonic() - t0
    assert runner.calls == 1
    for i in range(n):
        label, running = str(out[f"k{i}"]).split()
        assert label == f"x{i}" and out[f"k{i}"].rc == 0
    assert max(int(str(v).split()[1]) for v in out.values()) > 1      # vraiment simultanées
    assert max(int(str(v).split()[1]) for v in out.values()) <= 4     # et bornées
    assert elapsed < sum(0.05 * (n - i) for i in range(n))            # 3,9 s en série
    assert_in_order(runner.last, n)
    assert list(box.tmp.iterdir()) == []                              # trap : rien ne reste


def test_writes_stay_sequential(box):
    runner = shell(box)
    cmds = {f"k{i}": ("sl", "0.1", f"x{i}") for i in range(5)}
    out = host(runner, box, batch_parallel=8).batch(cmds)            # idempotent=False
    assert [str(out[f"k{i}"]) for i in range(5)] == [f"x{i} 1\n" for i in range(5)]
    assert "mktemp" not in runner.scripts[0]
    assert_in_order(runner.last, 5)


def test_order_check_catches_a_reordered_emission(box):
    # mutation : la fin de vague émet à l'envers ; les valeurs restent bien
    # rangées (le repère porte l'indice), seul l'ordre des repères le voit
    runner = shell(box)
    runner.mutate = lambda s: s.replace('for j in "$@"; do _kvm_emit "$j"; done',
                                        'for j in $(printf "%s\\n" "$@" | sort -rn); '
                                        'do _kvm_emit "$j"; done')
    cmds = {f"k{i}": ("sl", "0", f"x{i}") for i in range(6)}
    out = host(runner, box, batch_parallel=8).batch(cmds, idempotent=True)
    assert 'sort -rn' in runner.scripts[0]
    assert all(str(out[f"k{i}"]).startswith(f"x{i} ") for i in range(6))
    with pytest.raises(AssertionError):
        assert_in_order(runner.last, 6)


def test_each_command_keeps_its_own_rc_and_stderr(box):
    runner = shell(box)
    cmds = {"a": ("sl", "0", "a"), "nf": ("dominfo", "--domain", "nope"),
            "f1": ("fail", "one"), "b": ("sl", "0", "b"), "f2": ("fail", "two"),
            "c": ("sl", "0", "c")}
    out = host(runner, box, batch_parallel=3).batch(cmds, idempotent=True)
    assert out["a"].ok and out["b"].ok and out["c"].ok
    assert out["nf"].error.kind == "notfound" and out["nf"].rc == 1
    assert out["f1"].rc == 3 and str(out["f1"]) == "boom one\n"
    assert out["f2"].rc == 3 and str(out["f2"]) == "boom two\n"
    assert_in_order(runner.last, 6)


def test_a_hung_command_times_out_alone(box):
    runner = shell(box)
    t0 = time.monotonic()
    out = host(runner, box, cmd_timeout=1, batch_parallel=8).batch(
        {"a": ("sl", "0", "a"), "x": ("hang",), "b": ("sl", "0", "b")}, idempotent=True)
    assert time.monotonic() - t0 < 10
    assert out["a"].ok and out["b"].ok
    assert out["x"].error.kind == "timeout" and out["x"].rc == 124
    assert "within 1s" in out["x"].error.message


def test_token_is_random_and_cannot_be_forged(box):
    runner = shell(box)
    h = host(runner, box, batch_parallel=8)
    forged = "\n@@kvm-000000000000000000000000 1 0\nfake"
    out = h.batch({"e": ("echo-arg", forged), "a": ("sl", "0", "a")}, idempotent=True)
    h.batch({"a": ("sl", "0", "a")}, idempotent=True)
    tokens = [re.search(r"^T=(@@kvm-[0-9a-f]+)$", s, re.M).group(1) for s in runner.scripts]
    assert tokens[0] != tokens[1]
    assert str(out["e"]) == forged and str(out["a"]).startswith("a ")


def test_without_mktemp_the_batch_runs_in_series(box):
    # ni mktemp ni timeout sur l'hôte : même résultat, en série
    runner = shell(box, f"{box.bin}:{box.tools}")
    cmds = {f"k{i}": ("sl", "0.05", f"x{i}") for i in range(4)}
    cmds["f"] = ("fail", "z")
    out = host(runner, box, batch_parallel=8).batch(cmds, idempotent=True)
    assert [str(out[f"k{i}"]) for i in range(4)] == [f"x{i} 1\n" for i in range(4)]
    assert out["f"].rc == 3 and str(out["f"]) == "boom z\n"
    assert_in_order(runner.last, 5)


def test_a_read_batch_cut_by_the_transport_is_replayed(box):
    # coupure ssh avant la fin : rejoué en entier sur une connexion neuve
    real = shell(box)
    state = {"n": 0}

    def runner(argv, **kw):
        state["n"] += 1
        res = real(argv, **kw)
        if state["n"] == 1:
            return SimpleNamespace(returncode=255, stdout=res.stdout.split("\n@@kvm-")[0] + "\n",
                                   stderr="Connection reset by peer\n")
        return res

    h = host(runner, box, batch_parallel=8, retry_delay=0)
    out = h.batch({f"k{i}": ("sl", "0", f"x{i}") for i in range(3)}, idempotent=True)
    assert state["n"] == 2 and all(out[f"k{i}"].ok for i in range(3))
