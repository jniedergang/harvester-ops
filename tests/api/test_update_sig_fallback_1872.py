"""v1.87.2 : vérifier la signature d'une mise à jour sur un hôte dont
l'OpenSSH ne connaît pas `ssh-keygen -Y` (RHEL 8 : OpenSSH 8.0, vu par un
contributeur, PR #3).

L'agent fait alors faire la vérification par le ssh-keygen de l'image de la
console installée : conteneur jetable, sans réseau, en lecture seule, qui ne
voit que la signature et les clés de confiance. Vérifié en réel sur un hôte
Podman (image OpenSSH 9.6) : archive signée acceptée, archive altérée
refusée."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_self_update_182 import agent_env, keys, make_release, sign, su  # noqa: E402,F401  (fixtures)

OLD = subprocess.CompletedProcess(["ssh-keygen"], 255, "", "ssh-keygen: illegal option -- Y\n"
                                  "usage: ssh-keygen [-q] [-b bits] [-C comment] [-f output_keyfile]\n")


def old_host(argv, **kw):
    """Le ssh-keygen d'OpenSSH 8.0 : il répond par son mode d'emploi."""
    assert argv[:2] == ["ssh-keygen", "-Y"]
    return OLD


def real_image(calls):
    """La vérification « dans l'image », jouée ici par le vrai ssh-keygen."""
    def run(archive, signature, signers):
        calls.append((archive, signature, signers))
        with open(archive, "rb") as data:
            return subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(signers), "-I", su.SIG_IDENTITY,
                                   "-n", su.SIG_NAMESPACE, "-s", str(signature)],
                                  stdin=data, capture_output=True, text=True)
    return run


def test_an_old_host_ssh_keygen_falls_back_to_the_console_image(tmp_path, keys):
    a = make_release(tmp_path, "9.9.9")
    sig = sign(keys, a)
    calls = []
    assert su.verify_signature(a, sig, keys / "signers", runner=old_host, in_image=real_image(calls))
    assert len(calls) == 1
    with open(a, "ab") as f:
        f.write(b"tampered")
    with pytest.raises(su.UpdateError, match="signature refused"):
        su.verify_signature(a, sig, keys / "signers", runner=old_host, in_image=real_image(calls))


def test_a_host_without_ssh_keygen_falls_back_too(tmp_path, keys):
    a = make_release(tmp_path, "9.9.9")
    sig = sign(keys, a)

    def missing(argv, **kw):
        raise FileNotFoundError("ssh-keygen")
    assert su.verify_signature(a, sig, keys / "signers", runner=missing, in_image=real_image([]))


def test_without_an_image_the_error_says_what_is_needed(tmp_path, keys):
    a = make_release(tmp_path, "9.9.9")
    sig = sign(keys, a)
    with pytest.raises(su.UpdateError, match=r"OpenSSH 8\.2 or newer"):
        su.verify_signature(a, sig, keys / "signers", runner=old_host)
    with pytest.raises(su.UpdateError, match="neither this host's ssh-keygen nor the console image"):
        su.verify_signature(a, sig, keys / "signers", runner=old_host, in_image=lambda *x: OLD)


def test_a_recent_host_never_starts_a_container(tmp_path, keys):
    a = make_release(tmp_path, "9.9.9")
    sig = sign(keys, a)
    calls = []
    assert su.verify_signature(a, sig, keys / "signers", in_image=real_image(calls))
    assert calls == []
    # une vraie signature refusée par l'hôte n'est pas « rattrapée » par l'image
    other = sign(keys, a, key="other")
    with pytest.raises(su.UpdateError, match="signature refused"):
        su.verify_signature(a, other, keys / "signers", in_image=real_image(calls))
    assert calls == []


def test_the_agent_verifies_in_a_locked_down_container(agent_env, tmp_path, keys):
    ag, dirs = agent_env
    a = make_release(tmp_path, "9.9.9")
    sig = sign(keys, a)
    agent = ag.Agent.__new__(ag.Agent)
    seen = {}

    def run_cmd(argv, **kw):
        seen["argv"] = argv
        seen["stdin"] = kw["stdin"].read(4)
        vdir = Path(argv[argv.index("-v") + 1].split(":")[0])
        seen["files"] = sorted(p.name for p in vdir.iterdir())
        seen["signers"] = (vdir / "signers").read_text()
        return subprocess.CompletedProcess(argv, 0, "Good signature", "")
    agent.run_cmd = run_cmd
    r = agent.verify_in_image(a, sig, keys / "signers")
    assert r.returncode == 0
    argv = seen["argv"]
    assert argv[1:4] == ["run", "--rm", "-i"]
    for flag in (["--network", "none"], ["--read-only"], ["--entrypoint", "ssh-keygen"]):
        i = argv.index(flag[0])
        assert argv[i:i + len(flag)] == flag
    assert argv[argv.index("-v") + 1].endswith(":/verify:ro,Z")
    assert ag.IMAGE in argv and argv[argv.index(ag.IMAGE) + 1:argv.index(ag.IMAGE) + 3] == ["-Y", "verify"]
    assert seen["files"] == ["release.sig", "signers"] and seen["signers"] == (keys / "signers").read_text()
    assert seen["stdin"] == Path(a).read_bytes()[:4]          # l'archive passe par l'entrée standard
    assert not (dirs["work"] / "verify").exists()              # rien ne traîne
