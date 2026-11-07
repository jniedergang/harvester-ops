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
