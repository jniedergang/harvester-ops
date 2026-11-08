"""2.9.0 tâche 11 : rotation de la clé SSH d'un hôte KVM (point 260).

Faux hôte : chaque connexion ssh n'est acceptée que si la clé `-i` est dans
`authorized_keys` du faux $HOME ; les scripts distants tournent pour de vrai
(sh) dans ce $HOME. Une étape en échec rend l'état d'avant : ancienne clé
toujours acceptée, nouvelle absente de `authorized_keys`, ancienne valeur
rangée. Aucune commande distante ne touche aux domaines (pas de virsh).

Mutations jouées (rapport de la tâche 11) : « ancienne clé retirée avant la
connexion d'essai », « nouvelle clé laissée dans authorized_keys après un
échec », « ancienne valeur non reposée quand le retrait échoue ».
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import kvm_decl  # noqa: E402
import kvm_host  # noqa: E402


def keypair(d, name):
    k = Path(d) / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(k)], check=True)
    return k, (Path(str(k) + ".pub")).read_text().strip()


def blob(pub):
    return pub.split()[1]


class FakeHost:
    """ssh simulé : clé acceptée si son corps est dans authorized_keys."""

    def __init__(self, home):
        self.home = Path(home)
        self.ak = self.home / ".ssh" / "authorized_keys"
        self.calls = []
        self.fail = None              # (indice de l'appel ou motif du script, code)

    def __call__(self, argv, input=None, **kw):
        if "-O" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        key = argv[argv.index("-i") + 1]
        pub = subprocess.run(["ssh-keygen", "-y", "-f", key], capture_output=True, text=True).stdout.strip()
        remote = argv[-1]
        self.calls.append({"remote": remote, "blob": blob(pub), "fresh": "ControlMaster=no" in argv,
                           "stdin": input})
        if self.fail and self.fail(len(self.calls), remote):
            return subprocess.CompletedProcess(argv, 255, "", "Connection reset by peer")
        if not (self.ak.exists() and blob(pub) in self.ak.read_text()):
            return subprocess.CompletedProcess(argv, 255, "", "Permission denied (publickey).")
        return subprocess.run(["sh", "-c", remote], input=input, capture_output=True, text=True,
                              env={"HOME": str(self.home), "PATH": os.environ["PATH"]})


@pytest.fixture
def host(tmp_path):
    state = tmp_path / "state"
    (state / "ssh").mkdir(parents=True, mode=0o700)
    (state / "ssh" / "node2_known").write_text("10.0.0.2 ssh-ed25519 AAAA\n")
    old, old_pub = keypair(state / "ssh", "node2_id")
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "authorized_keys").write_text(f"ssh-rsa AAAAother admin@x\n{old_pub}\n")
    os.chmod(home / ".ssh" / "authorized_keys", 0o600)
    new, new_pub = keypair(tmp_path, "new")
    decl = {"name": "node2", "address": "10.0.0.2", "user": "ju", "port": 22,
            "ssh_key": "ssh/node2_id", "known_hosts": "ssh/node2_known", "uri": "qemu:///system"}
    stored = {"value": old.read_text()}
    fake = FakeHost(home)
    x = type("X", (), {})()
    x.state, x.decl, x.old, x.old_pub, x.new, x.new_pub, x.fake, x.stored = state, decl, old, old_pub, new, new_pub, fake, stored
    x.steps = []

    def rotate(**kw):
        return kvm_host.rotate_key(decl, state, old, new, old_pub, new_pub,
                                   lambda: stored.__setitem__("value", new.read_text()),
                                   lambda: stored.__setitem__("value", old.read_text()),
                                   runner=fake, step=lambda *a: x.steps.append(a), **kw)
    x.rotate = rotate
    return x


def ak(x):
    return x.fake.ak.read_text()


def test_rotation_replaces_the_key_on_the_host(host):
    mode0 = os.stat(host.fake.ak).st_mode
    res = host.rotate()
    assert res == {"old_refused": True}
    assert blob(host.new_pub) in ak(host) and blob(host.old_pub) not in ak(host)
    assert "ssh-rsa AAAAother admin@x" in ak(host)                  # les autres entrées gardées
    assert os.stat(host.fake.ak).st_mode == mode0
    assert host.stored["value"] == host.new.read_text()
    # ordre : ajout (ancienne), essai (nouvelle), retrait (nouvelle), contrôle (ancienne)
    seq = [(("authorized_keys" in c["remote"] and "printf" in c["remote"]) and "add"
            or ("grep -vF" in c["remote"]) and "remove" or "true", c["blob"]) for c in host.fake.calls]
    assert seq == [("add", blob(host.old_pub)), ("true", blob(host.new_pub)),
                   ("remove", blob(host.new_pub)), ("true", blob(host.old_pub))]
    assert all(c["fresh"] for c in host.fake.calls)                  # jamais la connexion partagée
    assert not any("virsh" in c["remote"] for c in host.fake.calls)  # domaines jamais touchés
    # clés publiques par l'entrée standard, jamais dans la ligne de commande
    assert not any(blob(host.new_pub) in c["remote"] or blob(host.old_pub) in c["remote"] for c in host.fake.calls)


def _failing(step_kind):
    def match(i, remote):
        if step_kind == "add":
            return "printf" in remote
        if step_kind == "test":
            return i == 2
        if step_kind == "remove":
            return "grep -vF" in remote and i == 3
        return False
    return match


@pytest.mark.parametrize("where", ["add", "test", "store", "remove"])
def test_any_failed_step_gives_back_the_state_before(host, where):
    if where == "store":
        def boom():
            raise OSError("disk full")
        with pytest.raises(OSError):
            kvm_host.rotate_key(host.decl, host.state, host.old, host.new, host.old_pub, host.new_pub, boom,
                                lambda: None, runner=host.fake)
    else:
        host.fake.fail = _failing(where)
        with pytest.raises(kvm_host.KvmError):
            host.rotate()
    assert blob(host.old_pub) in ak(host)                    # ancienne toujours acceptée
    assert blob(host.new_pub) not in ak(host)                # nouvelle retirée
    if where != "store":
        assert host.stored["value"] == host.old.read_text()  # ancienne valeur rangée
    # l'ancienne clé ouvre toujours une session
    host.fake.fail = None
    r = host.fake(kvm_host.ssh_argv(host.decl, host.state, ["true"], fresh=True), input="")
    assert r.returncode == 0


def test_old_key_still_accepted_elsewhere_is_reported(host):
    """L'ancienne clé encore acceptée (autre entrée, autre fichier) : signalé,
    pas une erreur."""
    real = host.fake.__call__

    class Sticky(FakeHost):
        pass
    host.fake.ak.write_text(ak(host))
    orig_call = FakeHost.__call__

    def call(self, argv, input=None, **kw):
        r = orig_call(self, argv, input, **kw)
        if "grep -vF" in argv[-1]:
            with open(self.ak, "a") as f:
                f.write(host.old_pub.replace(host.old_pub.split()[-1], "copy") + "\n")
        return r
    FakeHost.__call__ = call
    try:
        res = host.rotate()
    finally:
        FakeHost.__call__ = orig_call
    assert res == {"old_refused": False}
    assert any(s[0] == "old-refused" and s[1] == "warn" for s in host.steps)


def test_same_key_refused(host):
    with pytest.raises(kvm_host.KvmError):
        kvm_host.rotate_key(host.decl, host.state, host.old, host.old, host.old_pub, host.old_pub,
                            lambda: None, lambda: None, runner=host.fake)
    assert host.fake.calls == []


# ---------------------------------------------------------------------------
# harvester-secrets rotate ssh-kvm HOST (clé locale, clé dans OpenBao)
# ---------------------------------------------------------------------------
import test_harvester_secrets_cli_290 as tcli  # noqa: E402
from test_harvester_secrets_cli_290 import cli, creds, env, settings  # noqa: E402,F401


def _declare(env, ref=False):
    sd = env["state"]
    (sd / "ssh").mkdir(exist_ok=True, mode=0o700)
    old, old_pub = keypair(sd / "ssh", "node2_id")
    (sd / "ssh" / "node2_known").write_text("10.0.0.2 ssh-ed25519 AAAA\n")
    decl = {"name": "node2", "address": "10.0.0.2", "user": "ju", "port": 22,
            "ssh_key": kvm_decl.key_ref("node2") if ref else "ssh/node2_id", "known_hosts": "ssh/node2_known",
            "uri": "qemu:///system"}
    kvm_decl.write(sd, decl)
    home = env["state"].parent / "home"
    (home / ".ssh").mkdir(parents=True, exist_ok=True)
    (home / ".ssh" / "authorized_keys").write_text(old_pub + "\n")
    return old, old_pub, FakeHost(home)


def test_cli_rotates_a_local_key(env, capsys, monkeypatch):
    settings(env, mode="local")
    old, old_pub, fake = _declare(env)
    before = old.read_text()
    monkeypatch.setattr(tcli.hsc, "ROTATE_RUNNER", fake)
    rc, doc, err = cli(["--actor", "alice", "rotate", "ssh-kvm", "node2"], capsys=capsys)
    assert rc == 0, (doc, err)
    assert doc["old_refused"] is True and doc["store"] == "local"
    after = (env["state"] / "ssh" / "node2_id").read_text()
    assert after != before and oct(os.stat(env["state"] / "ssh" / "node2_id").st_mode & 0o777) == "0o600"
    assert blob(old_pub) not in fake.ak.read_text()
    assert "PRIVATE KEY" not in err + json.dumps(doc)
    assert not list((env["state"] / "ssh").glob("rotate-*"))       # copies de travail effacées


def test_cli_rotates_a_key_kept_in_openbao(env, capsys, monkeypatch):
    creds(env)
    settings(env, migrated={"ssh-kvm": {"at": 1}})
    old, old_pub, fake = _declare(env, ref=True)
    path = f"{tcli.PREFIX}/ssh-kvm/node2"
    env["srv"].put_raw("harvester-ops", path, {"private_key": old.read_text()})
    old.unlink()
    monkeypatch.setattr(tcli.hsc, "ROTATE_RUNNER", fake)
    rc, doc, err = cli(["--actor", "alice", "rotate", "ssh-kvm", "node2"], capsys=capsys)
    assert rc == 0, (doc, err)
    e = env["srv"].kv[("harvester-ops", path)]
    assert e["current"] == 2 and e["versions"][2]["private_key"] != e["versions"][1]["private_key"]
    assert (e["custom"] or {}).get("hops_v2_actor") == "alice"
    assert not (env["state"] / "ssh" / "node2_id").exists()          # jamais sur disque
    assert "PRIVATE KEY" not in err + json.dumps(doc)


def test_cli_failed_rotation_keeps_the_old_key(env, capsys, monkeypatch):
    settings(env, mode="local")
    old, old_pub, fake = _declare(env)
    before = old.read_text()
    seen = []

    def first_remove(i, remote):
        if "grep -vF" in remote and not seen:
            seen.append(i)
            return True
        return False
    fake.fail = first_remove
    monkeypatch.setattr(tcli.hsc, "ROTATE_RUNNER", fake)
    rc, doc, err = cli(["rotate", "ssh-kvm", "node2"], capsys=capsys)
    assert rc == 1 and doc["code"].startswith("kvm-")
    assert (env["state"] / "ssh" / "node2_id").read_text() == before
    assert fake.ak.read_text().strip() == old_pub


@pytest.mark.parametrize("target,code", [(None, "bad-input"), ("../x", "bad-input"), ("ghost", "not-found")])
def test_cli_rotation_refusals(env, capsys, target, code):
    settings(env, mode="local")
    rc, doc, _ = cli(["rotate", "ssh-kvm"] + ([target] if target else []), capsys=capsys)
    assert rc == 2 and doc["code"] == code
