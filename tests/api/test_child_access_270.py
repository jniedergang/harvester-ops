"""2.7.0 (tâche 2) : accès au cluster enfant par son Secret `<c>-kubeconfig`.

Lu avec l'identité passée (usurpation portée dans le kubeconfig, vérifiée
sur harvlab2 : un opérateur sans droit sur ce Secret reçoit Forbidden), écrit
en 0600 dans l'espace de travail, effacé en sortie de `session`.
"""

import base64
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import child_access as ca  # noqa: E402
from kube import KubeError  # noqa: E402

NS, CL = "rke2-banc", "rke2-banc"


class Mgmt:
    def __init__(self, objs=None, error=None):
        self.objs, self.error = objs or {}, error

    def get(self, kind, ns, name):
        if self.error:
            raise KubeError(self.error)
        return self.objs.get((kind, ns, name))


def test_child_session_reads_the_kubeconfig_secret_and_erases_its_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVESTER_OPS_WORK_DIR", str(tmp_path / "wd"))
    sec = {"data": {"value": base64.b64encode(b"apiVersion: v1\nkind: Config\n").decode()}}
    kube = Mgmt({("secrets", NS, f"{CL}-kubeconfig"): sec})
    with ca.session(kube, NS, CL) as path:
        p = Path(path)
        assert p.parent == tmp_path / "wd" and p.read_text().startswith("apiVersion")
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert not p.exists()


@pytest.mark.parametrize("kube,code", [
    (Mgmt(), "child-kubeconfig-missing"),
    (Mgmt({("secrets", NS, f"{CL}-kubeconfig"): {"data": {}}}), "child-kubeconfig-missing"),
    (Mgmt(error='secrets "rke2-banc-kubeconfig" is forbidden: User "bob" cannot get'), "child-admin-required"),
])
def test_read_refusals(kube, code):
    with pytest.raises(ca.ChildAccessError) as e:
        ca.read(kube, NS, CL)
    assert e.value.code == code


def test_other_errors_are_not_disguised():
    with pytest.raises(KubeError):
        ca.read(Mgmt(error="connection refused"), NS, CL)


def test_the_session_file_is_erased_on_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVESTER_OPS_WORK_DIR", str(tmp_path / "wd"))
    kube = Mgmt({("secrets", NS, f"{CL}-kubeconfig"): {"data": {"value": base64.b64encode(b"kc").decode()}}})
    seen = []
    with pytest.raises(RuntimeError):
        with ca.session(kube, NS, CL) as path:
            seen.append(path)
            raise RuntimeError("boom")
    assert seen and not Path(seen[0]).exists()


# --- revue 1-3, I4 : kubeconfig effacé même à l'arrêt (SIGTERM) -------------
def test_stale_child_kubeconfigs_are_swept(tmp_path, monkeypatch):
    import os
    import time
    wd = tmp_path / "wd"
    monkeypatch.setenv("HARVESTER_OPS_WORK_DIR", str(wd))
    wd.mkdir(mode=0o700)
    old, fresh, other = wd / "hops-child-a.kubeconfig", wd / "hops-child-b.kubeconfig", wd / "keep.kubeconfig"
    for f in (old, fresh, other):
        f.write_text("x")
    past = time.time() - ca.STALE_SECONDS - 5
    os.utime(old, (past, past))
    os.utime(other, (past, past))
    assert ca.sweep() == 1
    assert not old.exists() and fresh.exists() and other.exists()


SCRIPT = r'''
import base64, importlib.util, signal, sys, time
sys.path.insert(0, sys.argv[1] + "/bin/lib")
spec = importlib.util.spec_from_file_location("hc", sys.argv[1] + "/bin/harvester-capi.py")
hc = importlib.util.module_from_spec(spec); spec.loader.exec_module(hc)
class M:
    def get(self, kind, ns, name):
        return {"data": {"value": base64.b64encode(b"apiVersion: v1").decode()}}
signal.signal(signal.SIGTERM, hc._exit_cancelled)
with hc.ca.session(M(), "a", "b") as path:
    print(path, flush=True)
    time.sleep(30)
'''


def test_sigterm_inside_a_session_erases_the_file(tmp_path):
    import os
    import signal
    import subprocess
    env = dict(os.environ, HARVESTER_OPS_WORK_DIR=str(tmp_path / "wd"))
    p = subprocess.Popen([sys.executable, "-c", SCRIPT, str(ROOT)], stdout=subprocess.PIPE, text=True, env=env)
    path = Path(p.stdout.readline().strip())
    assert path.exists()
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=20) == 3
    assert not path.exists()


def test_opening_a_session_sweeps_leftovers(tmp_path, monkeypatch):
    import os
    import time
    wd = tmp_path / "wd"
    monkeypatch.setenv("HARVESTER_OPS_WORK_DIR", str(wd))
    wd.mkdir(mode=0o700)
    left = wd / "hops-child-dead.kubeconfig"
    left.write_text("x")
    past = time.time() - ca.STALE_SECONDS - 5
    os.utime(left, (past, past))
    sec = {"data": {"value": base64.b64encode(b"apiVersion: v1\n").decode()}}
    with ca.session(Mgmt({("secrets", NS, f"{CL}-kubeconfig"): sec}), NS, CL):
        assert not left.exists()
