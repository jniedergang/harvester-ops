"""2.9.0 tâche 3 : copies de travail des secrets sur tmpfs
(`secret_store.secret_files_dir`, `write_private_copy`).

Un secret lu dans OpenBao n'est jamais écrit sur un disque persistant : la
copie remise à kubectl, ssh ou tofu vit dans un répertoire 0700 sur tmpfs
(`$XDG_RUNTIME_DIR/hops-secrets` en développement, `/tmp/hops-secrets` dans
le conteneur), sinon `secret-files-not-tmpfs` sauf réglage de développement.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import secret_store as ss   # noqa: E402

EXT4 = "22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"


def mi(*tmpfs):
    out = EXT4
    for i, p in enumerate(tmpfs):
        out += f"{30 + i} 22 0:{40 + i} / {p.replace(' ', chr(92) + '040')} rw shared:{9 + i} - tmpfs tmpfs rw\n"
    return out


@pytest.mark.parametrize("mounts,path,kind", [
    (mi("/run/user/1000"), "/run/user/1000/hops-secrets", "tmpfs"),
    (mi("/run/user/1000"), "/run/user/10000/x", "ext4"),          # préfixe sans séparateur
    (mi("/tmp"), "/tmp", "tmpfs"),
    (mi(), "/tmp/x", "ext4"),
    (mi("/a", "/a/b"), "/a/b/c", "tmpfs"),
    (mi("/my dir"), "/my dir/x", "tmpfs"),                        # espace échappé dans mountinfo
])
def test_fs_type(mounts, path, kind):
    assert ss.fs_type(path, mounts) == kind


def test_xdg_runtime_dir_first(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    d = ss.secret_files_dir({}, mi(str(run), str(tmp_path / "t")), env={"XDG_RUNTIME_DIR": str(run)})
    assert d == run / "hops-secrets"
    assert (d.stat().st_mode & 0o777) == 0o700


def test_not_tmpfs_refused(tmp_path):
    with pytest.raises(ss.SecretError) as e:
        ss.secret_files_dir({}, EXT4, env={"XDG_RUNTIME_DIR": str(tmp_path)})
    assert e.value.code == "secret-files-not-tmpfs"
    assert not (tmp_path / "hops-secrets").exists()


def test_dev_disk_override_needs_both_settings(tmp_path):
    with pytest.raises(ss.SecretError):
        ss.secret_files_dir({"dev_disk_secret_files": True}, EXT4, env={})
    with pytest.raises(ss.SecretError):
        ss.secret_files_dir({"secret_files_dir": str(tmp_path / "d")}, EXT4, env={})
    d = ss.secret_files_dir({"dev_disk_secret_files": True, "secret_files_dir": str(tmp_path / "d")}, EXT4, env={})
    assert d == tmp_path / "d" and (d.stat().st_mode & 0o777) == 0o700


def test_existing_dir_tightened(tmp_path):
    run = tmp_path / "run"
    (run / "hops-secrets").mkdir(parents=True, mode=0o755)
    os.chmod(run / "hops-secrets", 0o755)
    d = ss.secret_files_dir({}, mi(str(run)), env={"XDG_RUNTIME_DIR": str(run)})
    assert (d.stat().st_mode & 0o777) == 0o700


def test_symlink_dir_refused(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (run / "hops-secrets").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ss.SecretError):
        ss.secret_files_dir({}, mi(str(run)), env={"XDG_RUNTIME_DIR": str(run)})


def test_private_copy(tmp_path):
    p = ss.write_private_copy(tmp_path, "Kube Ctl/../x", "VALUE")
    assert p.parent == tmp_path and p.read_text() == "VALUE"
    assert (p.stat().st_mode & 0o777) == 0o600
    assert "/" not in p.name and p.name.startswith("kube-ctl")
    assert ss.write_private_copy(tmp_path, "kubectl", "V") != ss.write_private_copy(tmp_path, "kubectl", "V")


def test_memory_secret_file_on_tmpfs(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    st = ss.MultiStore(tmp_path / "state", mountinfo=mi(str(run)))
    st.settings = dict(ss.DEFAULTS, secret_files_dir=str(run / "hops-secrets"))
    st._sig = ("pinned",)
    st._checked = 1e18
    st._files = ss.FileStore(tmp_path / "state")
    ref = st.put("plan-run", "a1", "token", "VAL")
    with st.file(ref, "ssh") as p:
        assert p.parent == run / "hops-secrets" and p.read_text() == "VAL"
    assert not p.exists()
    with pytest.raises(KeyError):
        with st.file(ref, "ssh") as p2:
            raise KeyError("boom")
    assert not p2.exists()
