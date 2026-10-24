"""Prochaine version KVM : la CLI harvester-kvm.py pour créer et modifier des
VMs, les gestes à chaud, les images, volumes, pools et réseaux.

Faux hôte : le `Sim2` de kvm_next_sim derrière le `SimNet` de la CLI 1.97
(chaque ligne distante est redécoupée par shlex puis jouée), et un faux
`Popen` pour les envois en flux (`vol-upload` sur une connexion propre).
Les vrais processus (annulation par SIGTERM) tournent contre de faux ssh et
virsh posés sur le PATH, ou contre le Sim chargé par un petit lanceur."""

import hashlib
import http.server
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_kvm_cli_197 as c197
from kvm_next_sim import Sim2
import kvm_decl
import kvm_host as kh
import kvm_storage as ks

cli = c197.cli
ROOT = Path(__file__).resolve().parent.parent.parent
ADDR = c197.ADDR
IMG = "/var/lib/libvirt/images"
UD_MARK = "KVMCLI-USERDATA-SECRET-7731"
UD = f"#cloud-config\npassword: {UD_MARK}\n"
HOSTILE_VM = ["a;rm -rf x", "$(id)", "x y", "-x", "'q'", "a\\b"]


# --- faux hôte ------------------------------------------------------------------------

class FakePopen:
    """Processus ssh d'un envoi en flux : lit l'entrée standard par un vrai
    tuyau (run_stream y écrit sans bloquer), puis rejoue la ligne distante
    sur le Sim à la fin. `delay` : pause par lecture (envoi lent)."""

    def __init__(self, net, argv, stdin=None, stdout=None, stderr=None, bufsize=0):
        assert argv[0] == "ssh" and "ControlMaster=no" in argv and "ControlPath=none" in argv
        assert argv[-3] == "--" and argv[-2] == ADDR
        self.net, self.out, self.err = net, stdout, stderr
        self.rem = shlex.split(argv[-1])
        net.fresh.append(self.rem)
        r, w = os.pipe()
        self._r = r
        self.stdin = os.fdopen(w, "wb", buffering=0)
        self.buf = bytearray()
        self.rc = None
        self.killed = False
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        while True:
            chunk = os.read(self._r, 65536)
            if not chunk:
                break
            self.buf += chunk
            if self.net.on_chunk:
                self.net.on_chunk(len(self.buf))
            if self.net.delay:
                time.sleep(self.net.delay)
        os.close(self._r)

    def poll(self):
        return self.rc

    def wait(self, timeout=None):
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise subprocess.TimeoutExpired("ssh", timeout)
        if self.rc is None:
            if self.killed:
                self.rc = -15
            else:
                try:
                    self.net.sim.run_stream(self.rem, io.BytesIO(bytes(self.buf)))
                    self.rc = 0
                except kh.KvmError as e:
                    self.err.write(e.message.split(": ", 2)[-1].encode())
                    self.rc = e.rc or 1
        return self.rc

    def terminate(self):
        self.killed = True

    kill = terminate


class Net(c197.SimNet):
    def __init__(self, sim):
        super().__init__(sim)
        self.fresh = []
        self.delay = 0
        self.on_chunk = None

    def popen(self, argv, **kw):
        return FakePopen(self, argv, **kw)


@pytest.fixture
def env(monkeypatch):
    short = Path(tempfile.mkdtemp(prefix="kvmc-", dir="/tmp"))
    state = short / "state"
    kvm_decl.write_key(state, "lab", c197.PRIVATE_KEY)
    kvm_decl.write_known_hosts(state, "lab", [f"{ADDR} ssh-ed25519 AAAAed"])
    kvm_decl.write(state, {"name": "lab", "address": ADDR, "user": "virtops",
                           "ssh_key": kvm_decl.key_rel("lab"),
                           "known_hosts": kvm_decl.known_hosts_rel("lab")})
    box = SimpleNamespace(state=state, short=short, net=None)

    def use(sim):
        box.net = Net(sim)
        monkeypatch.setattr(cli, "RUNNER", box.net)
        monkeypatch.setattr(cli, "POPEN", box.net.popen)
        return box.net
    box.use = use

    def spec(data, name="spec.json"):
        p = short / name
        p.write_text(json.dumps(data) if not isinstance(data, str) else data)
        return f"--spec-file={p}"
    box.spec = spec
    monkeypatch.setattr(cli, "SLEEP", lambda s: None)
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(state))
    yield box
    shutil.rmtree(short, ignore_errors=True)


run = c197.run
# empreinte valide quelconque : un refus d'usage ou un verrou occupé vient avant
ANY_DIGEST = f"--expect-delete={c197.ANY_DIGEST}"


def world(running=False, cidata=False):
    """Hôte d'essai : pool `images` (image de base, ISO, volume libre), réseau
    `default`, et la VM `vm1` (créée par la bibliothèque, comme en vrai)."""
    s = Sim2()
    s.add_pool("images", f"{IMG}/images")
    s.add_vol("images", "base.qcow2", cap=1 << 30, fmt="qcow2")
    s.add_vol("images", "boot.iso", fmt="iso")
    s.add_vol("images", "free.qcow2", fmt="qcow2")
    s.add_net("default", "192.168.122.0/24", bridge="virbr0", forward="nat")
    return s


VM_SPEC = {"name": "vm1", "cpu": {"vcpus": 1, "max_vcpus": 4, "hotplug": True},
           "memory": {"current_mib": 512, "max_mib": 1024},
           "disks": [{"source": {"kind": "blank", "pool": "images", "size_bytes": 1 << 20}},
                     {"source": {"kind": "empty"}}],
           "nics": [{"kind": "network", "source": "default", "mac": "52:54:00:00:00:aa"}]}


def vm_world(state="shut off", cloudinit=False):
    import kvm_create as kc
    s = world()
    spec = dict(VM_SPEC)
    if cloudinit:
        spec["cloudinit"] = {"user_data": UD, "seed_pool": "images"}
    kc.create_vm(s, spec, uuid_gen=lambda: "u-vm1", start=state == "running")
    s.writes.clear()
    return s


def writes(env):
    return list(env.net.sim.writes)


def verbs(env):
    return [w[0] for w in env.net.sim.writes]


def digest_of(env, name="vm1"):
    import kvm_xml as kx
    return kx.canonical_digest(env.net.sim.domains[name]["xml"])


# --- argv exact de chaque sous-commande ---------------------------------------------------

def _argv_cases():
    blank = {"name": "n1", "disks": [{"source": {"kind": "blank", "pool": "images",
                                                 "size_bytes": 1 << 20}}]}
    linked = {"name": "n2", "disks": [{"source": {"kind": "image", "pool": "images",
                                                  "image": "base.qcow2"}}]}
    seeded = {"name": "n3", "disks": blank["disks"],
              "cloudinit": {"user_data": UD, "seed_pool": "images"}}
    return [
        ("create blank", world, ["vm", "create", "--host=lab", blank],
         [("vol-create-as", "--pool", "images", "--name", "n1-vda.qcow2", "--capacity",
           "1048576", "--format", "qcow2"), ("define", "--validate", "/dev/stdin")]),
        ("create linked", world, ["vm", "create", "--host=lab", linked],
         [("vol-create-as", "--pool", "images", "--name", "n2-vda.qcow2", "--capacity",
           str(1 << 30), "--format", "qcow2", "--backing-vol", "base.qcow2",
           "--backing-vol-format", "qcow2"), ("define", "--validate", "/dev/stdin")]),
        ("create seeded", world,
         ["vm", "create", "--host=lab", seeded],
         [("vol-create-as", "--pool", "images", "--name", "n3-vda.qcow2"),
          ("vol-create-as", "--pool", "images", "--name", "n3-cidata.iso"),
          ("vol-upload", "images", "n3-cidata.iso"), ("define", "--validate", "/dev/stdin")]),
        ("create from options", world,
         ["vm", "create", "--host=lab", "--name=n4", "--disk=blank:images:2M",
          "--nic=network:default", "--start", "--autostart"],
         [("vol-create-as", "--pool", "images", "--name", "n4-vda.qcow2", "--capacity",
           str(2 << 20), "--format", "qcow2"), ("define", "--validate", "/dev/stdin"),
          ("autostart", "--domain", "n4"), ("start", "--domain", "n4")]),
        ("cpumem running", lambda: vm_world("running"),
         ["vm", "cpumem", "--host=lab", "--name=vm1", "--vcpus=2", "--memory-mib=768"],
         [("setvcpus", "--domain", "vm1", "--count", "2", "--live", "--config"),
          ("setmem", "--domain", "vm1", "--size", str(768 * 1024), "--live", "--config")]),
        ("add-volume running", lambda: vm_world("running"),
         ["vm", "add-volume", "--host=lab", "--name=vm1",
          {"bus": "virtio", "source": {"kind": "blank", "pool": "images",
                                       "size_bytes": 1 << 20}}],
         [("vol-create-as", "--pool", "images", "--name", "vm1-vdb.qcow2", "--capacity",
           "1048576", "--format", "qcow2"),
          ("attach-device", "--domain", "vm1", "--file", "/dev/stdin", "--live", "--config")]),
        ("remove-volume running", lambda: vm_world("running"),
         ["vm", "remove-volume", "--host=lab", "--name=vm1", "--target=vda"],
         [("detach-device", "--domain", "vm1", "--file", "/dev/stdin", "--live", "--config")]),
        ("insert-cdrom running", lambda: vm_world("running"),
         ["vm", "insert-cdrom", "--host=lab", "--name=vm1", "--target=sda", "--pool=images",
          "--volume=boot.iso"],
         [("update-device", "--domain", "vm1", "--file", "/dev/stdin", "--live", "--config")]),
        ("add-nic running", lambda: vm_world("running"),
         ["vm", "add-nic", "--host=lab", "--name=vm1", {"kind": "network",
                                                         "source": "default"}],
         [("attach-device", "--domain", "vm1", "--file", "/dev/stdin", "--live", "--config")]),
        ("remove-nic running", lambda: vm_world("running"),
         ["vm", "remove-nic", "--host=lab", "--name=vm1", "--mac=52:54:00:00:00:AA"],
         [("detach-interface", "--domain", "vm1", "--type", "network", "--mac",
           "52:54:00:00:00:aa", "--live", "--config")]),
        ("edit stopped", vm_world,
         ["vm", "edit", "--host=lab", "--name=vm1", {"title": "T", "cpu": {"vcpus": 2}},
          "DIGEST"],
         [("define", "--validate", "/dev/stdin")]),
        ("cloudinit", lambda: vm_world(cloudinit=True),
         ["vm", "cloudinit", "--host=lab", "--name=vm1", {"user_data": UD + "# v2\n"}],
         [("vol-create-as", "--pool", "images", "--name", "vm1-cidata-1.iso"),
          ("vol-upload", "images", "vm1-cidata-1.iso"),
          ("update-device", "--domain", "vm1", "--file", "/dev/stdin", "--config"),
          ("define", "--validate", "/dev/stdin"),
          ("vol-delete", "--pool", "images", "--vol", "vm1-cidata.iso")]),
        ("vol create linked", world,
         ["vol", "create", "--host=lab", "--pool=images", "--name=v.qcow2", "--capacity=1G",
          "--backing=base.qcow2"],
         [("vol-create-as", "--pool", "images", "--name", "v.qcow2", "--capacity",
           str(1 << 30), "--format", "qcow2", "--backing-vol", "base.qcow2",
           "--backing-vol-format", "qcow2")]),
        ("vol resize", world,
         ["vol", "resize", "--host=lab", "--pool=images", "--name=free.qcow2",
          "--capacity=128M"],
         [("vol-resize", "--pool", "images", "--vol", "free.qcow2", "--capacity",
           str(128 << 20))]),
        ("vol delete", world,
         ["vol", "delete", "--host=lab", "--pool=images", "--name=free.qcow2", "PREVIEW"],
         [("vol-delete", "--pool", "images", "--vol", "free.qcow2")]),
        ("image delete", world,
         ["image", "delete", "--host=lab", "--pool=images", "--volume=free.qcow2", "PREVIEW"],
         [("vol-delete", "--pool", "images", "--vol", "free.qcow2")]),
        ("pool define", world,
         ["pool", "define", "--host=lab", "--name=lab-pool", "--path=/srv/lab-pool",
          "--autostart"],
         [("pool-define", "--validate", "--file", "/dev/stdin"),
          ("pool-build", "--pool", "lab-pool"), ("pool-start", "--pool", "lab-pool"),
          ("pool-autostart", "--pool", "lab-pool")]),
        ("pool stop", world, ["pool", "stop", "--host=lab", "--pool=images"],
         [("pool-destroy", "--pool", "images")]),
        ("pool refresh", world, ["pool", "refresh", "--host=lab", "--pool=images"],
         [("pool-refresh", "--pool", "images")]),
        ("pool autostart off", world,
         ["pool", "autostart", "--host=lab", "--pool=images", "--disable"],
         [("pool-autostart", "--pool", "images", "--disable")]),
        ("net define", world,
         ["net", "define", "--host=lab", "--name=lab-net", "--mode=isolated",
          "--subnet=10.213.77.0/24"],
         [("net-define", "--validate", "--file", "/dev/stdin"),
          ("net-start", "--network", "lab-net")]),
        ("net autostart", world, ["net", "autostart", "--host=lab", "--network=default"],
         [("net-autostart", "--network", "default")]),
    ]


ARGV_CASES = _argv_cases()


def _argv(env, items, capsys=None):
    """argv d'un cas : dict = fichier de spec, DIGEST = empreinte de la VM,
    PREVIEW = empreinte de l'aperçu des arguments qui précèdent (obligatoire
    pour une suppression réelle)."""
    out = []
    for it in items:
        if isinstance(it, dict):
            out.append(env.spec(it))
        elif it == "DIGEST":
            out.append(f"--expect-config={digest_of(env)}")
        elif it == "PREVIEW":
            out = c197.previewed(capsys, *out)
        else:
            out.append(it)
    return out


@pytest.mark.parametrize("label,make,argv,want", ARGV_CASES, ids=[c[0] for c in ARGV_CASES])
def test_exact_argv_of_each_write(env, capsys, label, make, argv, want):
    env.use(make())
    res = run(capsys, *_argv(env, argv, capsys))
    assert res.rc == 0, res.err
    got = writes(env)
    # chaque écriture attendue, dans l'ordre, avec ses premiers arguments exacts
    assert len(got) == len(want), got
    for g, w in zip(got, want):
        assert g[:len(w)] == w, (g, w)
    # les XML ne partent que par l'entrée standard, jamais sur une ligne de commande
    for rem in env.net.remotes:
        assert not any("<domain" in a or "<pool" in a or "<network" in a for a in rem)


def test_create_seed_goes_on_a_fresh_connection_and_its_content_never_in_output(env, capsys):
    env.use(world())
    res = run(capsys, "vm", "create", "--host=lab", env.spec({
        "name": "ci", "disks": [{"source": {"kind": "blank", "pool": "images",
                                            "size_bytes": 1 << 20}}],
        "display": {"vnc": True, "password": True},
        "cloudinit": {"user_data": UD, "seed_pool": "images"}}))
    assert res.rc == 0, res.err
    (rem,) = env.net.fresh
    assert rem[:6] == ["env", "LC_ALL=C", "virsh", "-q", "-c", "qemu:///system"]
    assert rem[6] == "vol-upload" and "--sparse" not in rem
    define = [i for r, i in env.net.inputs if "define" in r]
    password = define[0].split("passwd=\"")[1].split("\"")[0]
    assert len(password) == 8
    for text in (res.out, res.err):
        assert UD_MARK not in text and password not in text
    (created,) = res.data["created"]
    assert created["vnc_password"] is True and created["seed"] == "images/ci-cidata.iso"


# --- erreurs d'usage, refus avant l'hôte -------------------------------------------------

USAGE = [
    (["vm", "create", "--host=lab"], "--spec-file"),
    (["vm", "create", "--host=lab", "SPEC", "--name=x"], "not both"),
    (["vm", "create", "--host=lab", "--name=x", "--disk=floppy:a"], "--disk"),
    (["vm", "create", "--host=lab", "--name=x", "--disk=blank:images:-1"], "invalid size"),
    (["vm", "create", "--host=lab", "--name=x", "--nic=vde:x"], "--nic"),
    (["vm", "create", "--host=lab", "--name=x/y"], "no '/'"),
    (["vm", "create", "--host=lab", "SPEC", "--timeout=0"], "--timeout"),
    (["vm", "create", "--host=lab", "BADJSON"], "not valid JSON"),
    (["vm", "create", "--host=lab", "LIST"], "JSON object"),
    (["vm", "edit", "--host=lab", "--name=vm1", "SPEC"], "--expect-config"),
    (["vm", "edit", "--host=lab", "--name=vm1", "SPEC", "--expect-config=abc"], "digest"),
    (["vm", "cpumem", "--host=lab", "--name=vm1"], "--vcpus"),
    (["vm", "cpumem", "--host=lab", "--name=vm1", "--vcpus=0"], "positive"),
    (["vm", "remove-volume", "--host=lab", "--name=vm1", "--target=../x"], "--target"),
    (["vm", "remove-nic", "--host=lab", "--name=vm1", "--mac=zz"], "--mac"),
    (["vm", "cloudinit", "--host=lab", "--name=vm1", "SPEC"], "unknown field"),
    (["vm", "insert-cdrom", "--host=lab", "--name=vm1", "--target=sda", "--pool=a\nb",
      "--volume=v"], "pool"),
    (["vol", "create", "--host=lab", "--pool=images", "--name=../etc", "--capacity=1G"],
     "volume"),
    (["vol", "create", "--host=lab", "--pool=images", "--name=v", "--capacity=1G",
      "--format=vmdk"], "--format"),
    (["vol", "upload", "--host=lab", "--pool=images", "--name=v", "--file=/nonexistent"],
     "--file"),
    (["vol", "upload", "--host=lab", "--pool=images", "--name=v", "--file=/etc/hostname",
      "--sha256=xyz"], "--sha256"),
    (["vol", "upload", "--host=lab", "--pool=images", "--name=v", "--file=/etc/hostname",
      "--limit-mbps=0"], "--limit-mbps"),
    (["vol", "delete", "--host=lab", "--pool=images", "--name=v",
      "--acknowledge-unreadable=rel/path"], "absolute"),
    (["image", "download-url", "--host=lab", "--pool=images", "--name=v",
      "--url=file:///etc/passwd"], "--url"),
    (["pool", "define", "--host=lab", "--name=p"], "--path"),
    (["pool", "define", "--host=lab", "--name=-p", "--path=/srv/p"], "pool"),
    (["pool", "delete", "--host=lab", "--pool=images", "--expect-delete=nope"], "digest"),
    (["net", "define", "--host=lab", "--name=n"], "--mode"),
    (["net", "delete", "--host=lab", "--network=a\tb"], "network"),
    (["host", "caps", "--host=lab", "--machine=virt"], "--machine"),
    (["vm", "create", "--host=ghost", "SPEC"], "not declared"),
]


@pytest.mark.parametrize("argv,msg", USAGE, ids=[" ".join(a[:2]) + f" {i}"
                                                 for i, (a, _) in enumerate(USAGE)])
def test_usage_errors_reach_no_host(env, capsys, argv, msg):
    env.use(vm_world())
    sub = {"SPEC": env.spec({"name": "x", "user_data": UD}),
           "BADJSON": env.spec("{nope", "bad.json"), "LIST": env.spec("[1]", "list.json")}
    res = run(capsys, *[sub.get(a, a) for a in argv])
    assert res.rc == 2, res.err
    assert set(res.data) == {"ok", "error", "kind"} and res.data["kind"] == "usage"
    assert msg in res.data["error"]
    assert env.net.remotes == [] and env.net.fresh == []
    assert UD_MARK not in res.out + res.err and str(env.short) not in res.out + res.err


def test_spec_file_from_stdin(env, capsys, monkeypatch):
    env.use(world())
    spec = {"name": "si", "disks": [{"source": {"kind": "blank", "pool": "images",
                                                "size_bytes": 1 << 20}}]}
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(spec)
                                                                         .encode())))
    res = run(capsys, "vm", "create", "--host=lab", "--spec-file=-")
    assert res.rc == 0 and res.data["created"][0]["name"] == "si"


def test_spec_file_too_large_is_refused(env, capsys):
    env.use(world())
    res = run(capsys, "vm", "create", "--host=lab",
              env.spec('{"name": "' + "x" * cli.SPEC_MAX + '"}', "big.json"))
    assert res.rc == 2 and "larger than" in res.data["error"]


# --- noms piégés --------------------------------------------------------------------------

@pytest.mark.parametrize("name", HOSTILE_VM)
def test_hostile_vm_names_stay_one_remote_argument(env, capsys, name):
    import kvm_create as kc
    s = world()
    spec = dict(VM_SPEC, name=name, disks=[{"source": {"kind": "volume", "pool": "images",
                                                       "volume": "free.qcow2"}}])
    kc.create_vm(s, spec, uuid_gen=lambda: "u-h", start=True)
    s.writes.clear()
    env.use(s)
    res = run(capsys, "vm", "cpumem", "--host=lab", f"--name={name}", "--vcpus=2")
    assert res.rc == 0, res.err
    assert ("setvcpus", "--domain", name, "--count", "2", "--live", "--config") in writes(env)
    res = run(capsys, "vm", "edit", "--host=lab", f"--name={name}", env.spec({"title": "t"}),
              f"--expect-config={digest_of(env, name)}")
    assert res.rc == 0, res.err
    for rem in env.net.remotes:
        if "--domain" in rem:
            assert rem[rem.index("--domain") + 1] == name


@pytest.mark.parametrize("argv", [
    ["vol", "create", "--host=lab", "--pool=images", "--name=a;rm -rf x", "--capacity=1M"],
    ["vol", "upload", "--host=lab", "--pool=images", "--name=$(id)", "--file=/etc/hostname"],
    ["pool", "define", "--host=lab", "--name=x y", "--path=/srv/x"],
    ["net", "define", "--host=lab", "--name=-x", "--mode=isolated", "--subnet=10.0.9.0/24"],
    ["pool", "start", "--host=lab", "--pool=a\nb"],
    ["image", "verify", "--host=lab", "--pool=images", "--volume=a\nb"],
])
def test_hostile_new_names_are_refused_before_the_host(env, capsys, argv):
    """Un nom créé par la console suit une règle stricte (lettres, chiffres,
    . _ + -) ; un caractère de contrôle est refusé partout."""
    env.use(world())
    res = run(capsys, *argv)
    assert res.rc == 2 and res.data["kind"] == "usage" and env.net.remotes == []


HOSTILE_OBJ = ["$(id) x", "a;rm -rf /", "-x", "'q'"]


@pytest.mark.parametrize("name", HOSTILE_OBJ)
def test_hostile_existing_object_names_stay_one_remote_argument(env, capsys, name):
    """Un pool, un réseau ou un volume existant peut porter tout nom libvirt :
    il part comme un seul argument distant, jamais interprété."""
    s = world()
    s.add_pool(name, "/srv/hostile")
    s.add_vol("images", name, fmt="qcow2")
    s.add_net(name, "10.66.0.0/24", bridge="virbr7", forward="nat")
    env.use(s)
    for argv in (["pool", "refresh", "--host=lab", f"--pool={name}"],
                 ["net", "autostart", "--host=lab", f"--network={name}"],
                 ["vol", "resize", "--host=lab", "--pool=images", f"--name={name}",
                  "--capacity=1G"]):
        res = run(capsys, *argv)
        assert res.rc == 0, res.err
    assert ("pool-refresh", "--pool", name) in writes(env)
    assert ("net-autostart", "--network", name) in writes(env)
    assert ("vol-resize", "--pool", "images", "--vol", name, "--capacity", str(1 << 30)) \
        in writes(env)


def test_hostile_bridge_name_in_a_network_spec_is_refused(env, capsys):
    env.use(world())
    res = run(capsys, "net", "define", "--host=lab", env.spec(
        {"name": "n", "mode": "bridge", "bridge": "br0;reboot"}))
    assert res.rc == 1 and res.data["kind"] == "refused"
    assert not [w for w in writes(env) if w[0].startswith("net-")]


# --- aperçus : aucune écriture ------------------------------------------------------------

class Refuser(Sim2):
    """Hôte qui refuse toute écriture : un aperçu qui écrirait échoue."""

    def __init__(self, base):
        self.__dict__.update(base.__dict__)

    def _answer(self, args, data=None):
        from kvm_next_sim import WRITE_VERBS
        assert str(args[0]) not in WRITE_VERBS, f"write during a dry run: {args}"
        return super()._answer(args, data)


DRY = [
    ("vm create", world, ["vm", "create", "--host=lab", {"name": "d1", "disks": [
        {"source": {"kind": "blank", "pool": "images", "size_bytes": 1 << 20}}],
        "cloudinit": {"user_data": UD, "seed_pool": "images"}}, "--dry-run"]),
    ("vm edit", vm_world, ["vm", "edit", "--host=lab", "--name=vm1", {"title": "x"},
                           "--dry-run"]),
    ("vol delete", world, ["vol", "delete", "--host=lab", "--pool=images",
                           "--name=free.qcow2", "--dry-run"]),
    ("pool define", world, ["pool", "define", "--host=lab", "--name=p2", "--path=/srv/p2",
                            "--dry-run"]),
    ("pool delete", world, ["pool", "delete", "--host=lab", "--pool=images", "--dry-run"]),
    ("net define", world, ["net", "define", "--host=lab", "--name=n2", "--mode=nat",
                           "--subnet=10.9.8.0/24", "--dry-run"]),
    ("net delete", world, ["net", "delete", "--host=lab", "--network=default", "--dry-run"]),
]


@pytest.mark.parametrize("label,make,argv", DRY, ids=[d[0] for d in DRY])
def test_dry_run_never_writes(env, capsys, label, make, argv):
    env.use(Refuser(make()))
    res = run(capsys, *_argv(env, argv, capsys))
    assert res.rc == 0, res.err
    assert res.data["dry_run"] is True and env.net.fresh == []
    assert UD_MARK not in res.out + res.err


# --- modification, empreinte, gestes --------------------------------------------------------

def test_edit_running_mixed_result_and_restart_needed(env, capsys):
    env.use(vm_world("running"))
    res = run(capsys, "vm", "edit", "--host=lab", "--name=vm1",
              env.spec({"cpu": {"vcpus": 2}, "memory": {"max_mib": 2048}}),
              f"--expect-config={digest_of(env)}")
    assert res.rc == 0, res.err
    applied = {c["field"]: c["applied"] for c in res.data["changes"]}
    assert applied == {"cpu.vcpus": "now", "memory.max_mib": "next-boot"}
    assert res.data["restart_required"] is True
    assert "memory.max_mib" in res.data["after"]["pending"]
    assert res.data["after"]["config_digest"] != res.data["config_digest_before"]


def test_edit_with_a_stale_digest_is_refused_as_changed(env, capsys):
    env.use(vm_world())
    stale = digest_of(env)
    env.net.sim.domains["vm1"]["xml"] = env.net.sim.domains["vm1"]["xml"].replace(
        "<on_crash>destroy</on_crash>", "<on_crash>restart</on_crash>")
    res = run(capsys, "vm", "edit", "--host=lab", "--name=vm1", env.spec({"title": "x"}),
              f"--expect-config={stale}")
    assert res.rc == 1 and res.data["kind"] == "changed" and writes(env) == []


def test_empty_edit_writes_nothing(env, capsys):
    env.use(vm_world())
    res = run(capsys, "vm", "edit", "--host=lab", "--name=vm1", env.spec({}),
              f"--expect-config={digest_of(env)}")
    assert res.rc == 0 and res.data["empty"] is True and writes(env) == []


def test_gesture_refused_names_the_reason(env, capsys):
    env.use(vm_world("running"))
    res = run(capsys, "vm", "cpumem", "--host=lab", "--name=vm1", "--vcpus=8")
    assert res.rc == 1 and res.data["kind"] == "refused" and res.data["refusals"]
    assert writes(env) == []


def test_gesture_failure_lists_what_was_done(env, capsys):
    env.use(vm_world("running"))
    env.net.sim.fail_next["setmem"] = (1, "error: internal error: balloon broke\n")
    res = run(capsys, "vm", "cpumem", "--host=lab", "--name=vm1", "--vcpus=2",
              "--memory-mib=768")
    assert res.rc == 1, res.err
    status = {c["field"]: c["applied"] for c in res.data["changes"]}
    assert status == {"cpu.vcpus": "now", "memory.current_mib": "failed"}


def test_cloudinit_result_says_presence_and_size_only(env, capsys):
    env.use(vm_world(cloudinit=True))
    res = run(capsys, "vm", "cloudinit", "--host=lab", "--name=vm1",
              env.spec({"user_data": UD, "hostname": "h1"}), "--rerun")
    assert res.rc == 0, res.err
    assert res.data["user_data"] is True and res.data["user_data_bytes"] == len(UD)
    assert res.data["seed"]["volume"] == "vm1-cidata-1.iso" and res.data["removed"] is True
    assert UD_MARK not in res.out + res.err


# --- verrous ---------------------------------------------------------------------------

def test_create_holds_every_instance_name(env, capsys):
    env.use(world())
    with cli.vm_lock(env.state, "lab", "m-02"):
        res = run(capsys, "vm", "create", "--host=lab", env.spec({
            "name": "m", "instances": 3, "disks": [{"source": {"kind": "blank", "pool": "images",
                                                                "size_bytes": 1 << 20}}]}))
    assert res.rc == 2 and res.data["kind"] == "busy" and "m-02" in res.data["error"]
    assert writes(env) == []


def test_pool_delete_waits_for_a_creation_writing_into_it(env, capsys):
    env.use(world())
    with cli.object_lock(env.state, "lab", "pool", "images", shared=True):
        res = run(capsys, "pool", "delete", "--host=lab", "--pool=images", ANY_DIGEST)
        assert res.rc == 2 and res.data["kind"] == "busy"
        # deux créations peuvent écrire dans le même pool
        with cli.object_lock(env.state, "lab", "pool", "images", shared=True):
            pass
    assert writes(env) == []


def test_edit_and_gesture_share_the_vm_lock(env, capsys):
    env.use(vm_world("running"))
    with cli.vm_lock(env.state, "lab", "vm1"):
        res = run(capsys, "vm", "cpumem", "--host=lab", "--name=vm1", "--vcpus=2")
        assert res.rc == 2 and res.data["kind"] == "busy"
        res = run(capsys, "vm", "delete", "--host=lab", "--name=vm1", ANY_DIGEST)
        assert res.rc == 2 and res.data["kind"] == "busy"


# --- création : échec, échéance, plusieurs instances --------------------------------------

def test_create_failure_rolls_back_and_says_so(env, capsys):
    env.use(world())
    env.net.sim.fail_next["define"] = (1, "error: XML error: boom\n")
    res = run(capsys, "vm", "create", "--host=lab", env.spec({
        "name": "f1", "disks": [{"source": {"kind": "blank", "pool": "images",
                                            "size_bytes": 1 << 20}}],
        "cloudinit": {"user_data": UD, "seed_pool": "images"}}))
    assert res.rc == 1 and res.data["kind"] == "failed"
    assert res.data["leftovers"] == [] and res.data["failed"] == "f1"
    assert ("rollback", "done") in c197.step_ids(res)
    vols = env.net.sim.pools["images"]["vols"]
    assert "f1-vda.qcow2" not in vols and "f1-cidata.iso" not in vols


def test_create_timeout_rolls_back(env, capsys, monkeypatch):
    env.use(world())
    clock = iter([0.0] * 3 + [10_000.0] * 1000)      # échéance après le premier disque
    monkeypatch.setattr(cli, "CLOCK", lambda: next(clock))
    res = run(capsys, "vm", "create", "--host=lab", env.spec({
        "name": "t1", "disks": [{"source": {"kind": "blank", "pool": "images",
                                            "size_bytes": 1 << 20}}]}), "--timeout=60")
    assert res.rc == 1 and res.data["kind"] == "timeout"
    assert ("vol-create-as", "--pool", "images", "--name", "t1-vda.qcow2") == writes(env)[0][:5]
    assert ("vol-delete", "--pool", "images", "--vol", "t1-vda.qcow2") in writes(env)
    assert "t1-vda.qcow2" not in env.net.sim.pools["images"]["vols"]
    assert "t1" not in env.net.sim.domains


def test_create_three_instances_second_fails(env, capsys):
    env.use(world())
    sim = env.net.sim
    calls = {"n": 0}
    orig = sim.v_define

    def define(o, p, data):
        calls["n"] += 1
        if calls["n"] == 2:
            return 1, "", "error: internal error: second define broke\n"
        return orig(o, p, data)
    sim.v_define = define
    res = run(capsys, "vm", "create", "--host=lab", env.spec({
        "name": "m", "instances": 3, "disks": [{"source": {"kind": "blank", "pool": "images",
                                                            "size_bytes": 1 << 20}}]}))
    assert res.rc == 1
    assert [c["name"] for c in res.data["created"]] == ["m-01"]
    assert res.data["failed"] == "m-02" and res.data["leftovers"] == []
    assert "m-01" in sim.domains and "m-02" not in sim.domains and "m-03" not in sim.domains


def test_cancel_during_the_full_copy_in_process_rolls_back(env, capsys, monkeypatch):
    import kvm_create as kc
    env.use(world())
    monkeypatch.setattr(kc, "COPY_POLL", 0.01)
    sim = env.net.sim
    orig = sim._answer

    def slow(args, data=None):
        if args and args[0] == "vol-create-from":
            time.sleep(0.3)
        return orig(args, data)
    sim._answer = slow
    real_alloc = kc._vol_allocation

    def poll(host, pool, vol):
        raise cli.Cancelled("SIGTERM")
    monkeypatch.setattr(kc, "_vol_allocation", poll)
    res = run(capsys, "vm", "create", "--host=lab", env.spec({
        "name": "cp", "disks": [{"source": {"kind": "image", "pool": "images",
                                            "image": "base.qcow2", "linked": False}}]}))
    monkeypatch.setattr(kc, "_vol_allocation", real_alloc)
    assert res.rc == 3, res.err
    assert res.data["kind"] == "cancelled"
    assert res.data["error"].startswith("creation cancelled (SIGTERM): ")
    assert "cp" not in sim.domains


# --- images : téléversement, téléchargement, vérification ----------------------------------

def qcow2_bytes(size=1 << 20, backing=False):
    import struct
    hdr = bytearray(512)
    hdr[0:4] = b"QFI\xfb"
    struct.pack_into(">I", hdr, 4, 3)
    if backing:
        struct.pack_into(">QI", hdr, 8, 400, 8)
        hdr[400:408] = b"/etc/sha"
    struct.pack_into(">I", hdr, 20, 16)
    struct.pack_into(">Q", hdr, 24, size)
    struct.pack_into(">I", hdr, 100, 104)
    return bytes(hdr) + b"\0" * 4096


def test_upload_streams_verifies_and_notes(env, capsys):
    env.use(world())
    f = env.short / "img.qcow2"
    f.write_bytes(qcow2_bytes())
    sha = hashlib.sha256(f.read_bytes()).hexdigest()
    res = run(capsys, "image", "upload", "--host=lab", "--pool=images", "--name=up.qcow2",
              f"--file={f}", f"--sha256={sha}", "--author=ops")
    assert res.rc == 0, res.err
    assert res.data["verified"] is True and res.data["format"] == "qcow2"
    assert env.net.sim.contents[("images", "up.qcow2")] == f.read_bytes()
    note = ks.load_notes(env.state, "lab")["images/up.qcow2"]
    assert note["sha256"] == sha and note["author"] == "ops"
    assert [s[0] for s in res.steps if s[1] == "done"][:3] == ["check", "upload", "verify"]
    assert any(ln.startswith("PROGRESS_EVENT|upload|") for ln in res.err.splitlines())
    assert str(f) not in res.out + res.err


def test_upload_of_a_qcow2_with_a_backing_file_is_refused_before_any_write(env, capsys):
    env.use(world())
    f = env.short / "evil.qcow2"
    f.write_bytes(qcow2_bytes(backing=True))
    res = run(capsys, "vol", "upload", "--host=lab", "--pool=images", "--name=e.qcow2",
              f"--file={f}")
    assert res.rc == 1 and res.data["kind"] == "refused" and writes(env) == []


def test_upload_wrong_checksum_on_the_host_deletes_the_volume(env, capsys):
    env.use(world())
    f = env.short / "img.raw"
    f.write_bytes(b"x" * 5000)
    sim = env.net.sim
    orig = sim.run_stream

    def corrupt(argv, source, **kw):
        out = orig(argv, io.BytesIO(source.read()[:-1] + b"y"), **kw)
        return out
    sim.run_stream = corrupt
    res = run(capsys, "vol", "upload", "--host=lab", "--pool=images", "--name=r.raw",
              f"--file={f}")
    assert res.rc == 1 and "does not match" in res.data["error"]
    assert "r.raw" not in sim.pools["images"]["vols"] and res.data["leftovers"] == []


class _Files(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/redir"):
            self.send_response(302)
            self.send_header("Location", "file:///etc/passwd")
            self.end_headers()
            return
        return super().do_GET()


@pytest.fixture
def files_server(tmp_path):
    root = tmp_path / "www"
    root.mkdir()

    def handler(*a, **kw):
        return _Files(*a, directory=str(root), **kw)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield root, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_download_url_on_this_machine_then_upload(env, capsys, files_server):
    root, base = files_server
    data = qcow2_bytes()
    (root / "cirros.qcow2").write_bytes(data)
    env.use(world())
    sha = hashlib.sha256(data).hexdigest()
    res = run(capsys, "image", "download-url", "--host=lab", "--pool=images",
              "--name=cirros.qcow2", f"--url={base}/cirros.qcow2?token=SECRETTOKEN",
              f"--sha256={sha}", "--allow-private=127.0.0.1/32")
    assert res.rc == 0, res.err
    assert env.net.sim.contents[("images", "cirros.qcow2")] == data
    assert res.data["url"] == f"{base}/cirros.qcow2"
    assert "SECRETTOKEN" not in res.out + res.err
    note = ks.load_notes(env.state, "lab")["images/cirros.qcow2"]
    assert note["url"] == f"{base}/cirros.qcow2" and "SECRETTOKEN" not in json.dumps(note)
    assert list((env.state / "kvm-images" / "staging").iterdir()) == []


@pytest.mark.parametrize("path,sha,kind,msg", [
    ("/cirros.qcow2", "0" * 64, "refused", "does not match"),
    ("/missing.qcow2", None, "failed", "HTTP 404"),
    ("/redir", None, "refused", "outside http/https"),
])
def test_download_url_refusals_send_nothing(env, capsys, files_server, path, sha, kind, msg):
    root, base = files_server
    (root / "cirros.qcow2").write_bytes(qcow2_bytes())
    env.use(world())
    argv = ["image", "download-url", "--host=lab", "--pool=images", "--name=c.qcow2",
            f"--url={base}{path}", "--allow-private=127.0.0.0/8"] + (
                [f"--sha256={sha}"] if sha else [])
    res = run(capsys, *argv)
    assert res.rc in (1, 2) and res.data["kind"] == kind and msg in res.data["error"]
    assert writes(env) == [] and list((env.state / "kvm-images" / "staging").iterdir()) == []


def test_image_list_and_verify(env, capsys):
    s = world()
    s.contents[("images", "free.qcow2")] = b"abc"
    env.use(s)
    ks.save_note(env.state, "lab", "images", "free.qcow2",
                 {"sha256": hashlib.sha256(b"abc").hexdigest()})
    res = run(capsys, "image", "list", "--host=lab", "--fresh")
    assert res.rc == 0
    names = {(i["pool"], i["volume"]) for i in res.data["images"]}
    assert ("images", "free.qcow2") in names
    res = run(capsys, "image", "verify", "--host=lab", "--pool=images", "--volume=free.qcow2")
    assert res.rc == 0 and res.data["match"] is True
    ks.save_note(env.state, "lab", "images", "free.qcow2", {"sha256": "1" * 64})
    res = run(capsys, "image", "verify", "--host=lab", "--pool=images", "--volume=free.qcow2")
    assert res.rc == 1 and res.data["kind"] == "mismatch" and res.data["match"] is False


# --- suppressions avec empreinte -------------------------------------------------------------

@pytest.mark.parametrize("kind", ["vol", "pool", "net"])
def test_delete_with_a_digest_refuses_a_changed_object(env, capsys, kind):
    s = world()
    if kind == "pool":
        s.add_pool("p2", "/srv/p2")
        argv = ["pool", "delete", "--host=lab", "--pool=p2"]
    elif kind == "net":
        s.add_net("n2", "10.1.2.0/24", bridge="virbr9", forward="nat")
        argv = ["net", "delete", "--host=lab", "--network=n2"]
    else:
        argv = ["vol", "delete", "--host=lab", "--pool=images", "--name=free.qcow2"]
    env.use(s)
    res = run(capsys, *argv, "--dry-run")
    assert res.rc == 0 and res.data["possible"] is True
    good = res.data["plan"]["digest"]
    # changé entre l'aperçu et l'exécution
    if kind == "pool":
        s.pools["p2"]["path"] = "/srv/p2-other"          # pool redéfini ailleurs
    elif kind == "net":
        s.networks["n2"]["bridge"] = "virbr8"
    else:
        s.pools["images"]["vols"]["free.qcow2"]["key"] = "other-key"
        s.pools["images"]["vols"]["free.qcow2"]["path"] = f"{IMG}/images/free-new.qcow2"
    res = run(capsys, *argv, f"--expect-delete={good}")
    assert res.rc == 1 and res.data["kind"] == "plan-changed", res.data
    assert not [w for w in writes(env) if w[0] in ("vol-delete", "pool-undefine",
                                                   "net-undefine")]
    res = run(capsys, *argv, "--dry-run")
    res = run(capsys, *argv, f"--expect-delete={res.data['plan']['digest']}")
    assert res.rc == 0 and res.data["gone"] is True, res.data


def test_pool_and_network_refusals_for_real_reasons(env, capsys):
    s = vm_world("running")
    env.use(s)
    res = run(capsys, "pool", "define", "--host=lab", "--name=etcp", "--path=/etc/x")
    assert res.rc == 1 and "system directory" in res.data["error"]
    res = run(capsys, "net", "stop", "--host=lab", "--network=default")
    assert res.rc == 1 and res.data["kind"] == "refused" and "vm1" in res.data["error"]
    res = run(capsys, "net", "define", "--host=lab", "--name=lan", "--mode=nat",
              "--subnet=172.16.0.0/16")
    assert res.rc == 1 and res.data["kind"] == "refused"
    assert not [w for w in writes(env) if w[0] in ("net-define", "pool-define",
                                                   "net-destroy")]


# --- schéma JSON stable --------------------------------------------------------------------

FAIL = {"ok", "error", "kind"}
SCHEMAS = [
    ("caps", world, ["host", "caps", "--host=lab"], 0, {"ok", "host", "machine", "options"}),
    ("create", world, ["vm", "create", "--host=lab", {"name": "s1", "disks": [
        {"source": {"kind": "blank", "pool": "images", "size_bytes": 1 << 20}}]}], 0,
     {"ok", "host", "dry_run", "elapsed_s", "created", "warnings", "leftovers"}),
    ("create dry", world, ["vm", "create", "--host=lab", {"name": "s1"}, "--dry-run"], 0,
     {"ok", "host", "dry_run", "possible", "plan"}),
    ("create refused", world, ["vm", "create", "--host=lab", {"name": "s1", "disks": [
        {"source": {"kind": "volume", "pool": "images", "volume": "nope"}}]}], 1,
     FAIL | {"host", "refusals", "created", "failed", "leftovers"}),
    ("edit", vm_world, ["vm", "edit", "--host=lab", "--name=vm1", {"title": "t"}, "DIGEST"], 0,
     {"ok", "host", "dry_run", "name", "previous_name", "changes", "empty", "defined",
      "renamed", "leftovers", "config_digest_before", "after", "restart_required"}),
    ("edit dry", vm_world, ["vm", "edit", "--host=lab", "--name=vm1", {"title": "t"},
                            "--dry-run"], 0, {"ok", "host", "name", "dry_run", "possible",
                                              "plan"}),
    ("edit changed", vm_world, ["vm", "edit", "--host=lab", "--name=vm1", {"title": "t"},
                                "--expect-config=" + "0" * 64], 1,
     FAIL | {"host", "name", "refusals", "changes", "leftovers"}),
    ("gesture", lambda: vm_world("running"), ["vm", "cpumem", "--host=lab", "--name=vm1",
                                              "--vcpus=2"], 0,
     {"ok", "host", "name", "gesture", "changes", "defined", "leftovers", "after",
      "restart_required"}),
    ("gesture refused", lambda: vm_world("running"),
     ["vm", "cpumem", "--host=lab", "--name=vm1", "--vcpus=9"], 1,
     FAIL | {"host", "name", "gesture", "refusals", "changes", "leftovers"}),
    ("cloudinit", lambda: vm_world(cloudinit=True),
     ["vm", "cloudinit", "--host=lab", "--name=vm1", {"user_data": UD}], 0,
     {"ok", "host", "name", "seed", "previous", "removed", "kept_reason", "next_boot", "rerun",
      "leftovers", "after", "user_data", "user_data_bytes", "network_data"}),
    ("image list", world, ["image", "list", "--host=lab", "--fresh"], 0,
     {"ok", "host", "images", "pools", "missing_pools", "unknown", "survey_age_s"}),
    ("vol create", world, ["vol", "create", "--host=lab", "--pool=images", "--name=n.qcow2",
                           "--capacity=1M"], 0, {"ok", "host", "pool", "name", "volume"}),
    ("vol delete dry", world, ["vol", "delete", "--host=lab", "--pool=images",
                               "--name=free.qcow2", "--dry-run"], 0,
     {"ok", "host", "dry_run", "possible", "plan"}),
    ("vol delete", world, ["vol", "delete", "--host=lab", "--pool=images",
                           "--name=free.qcow2", "PREVIEW"], 0,
     {"ok", "host", "dry_run", "pool", "volume", "key", "path", "identity", "refusals",
      "unreadable", "acknowledged_unreadable", "digest", "deleted", "gone"}),
    ("pool define", world, ["pool", "define", "--host=lab", "--name=p3", "--path=/srv/p3"], 0,
     {"ok", "host", "name", "dry_run", "state", "existing_directory", "nested_with"}),
    ("pool stop", world, ["pool", "stop", "--host=lab", "--pool=images"], 0,
     {"ok", "host", "pool", "action", "state"}),
    ("pool delete", world, ["pool", "delete", "--host=lab", "--pool=images", "PREVIEW"], 0,
     {"ok", "host", "dry_run", "name", "deleted", "directory_removed", "gone"}),
    ("net define", world, ["net", "define", "--host=lab", "--name=n3", "--mode=isolated",
                           "--subnet=10.4.4.0/24"], 0,
     {"ok", "host", "name", "dry_run", "network"}),
    ("net delete refused", lambda: vm_world("running"),
     ["net", "delete", "--host=lab", "--network=default", "PREVIEW"], 1, FAIL | {"host", "network",
                                                                     "refusals"}),
    ("net links", world, ["net", "links", "--host=lab"], 0,
     {"ok", "host", "bridges", "physical", "links", "networks"}),
]


@pytest.mark.parametrize("label,make,argv,rc,keys", SCHEMAS, ids=[s[0] for s in SCHEMAS])
def test_json_schema_is_stable(env, capsys, label, make, argv, rc, keys):
    env.use(make())
    res = run(capsys, *_argv(env, argv, capsys))
    assert res.rc == rc, res.err
    assert set(res.data) == keys
    assert "Traceback" not in res.err and str(env.state) not in res.out + res.err


# --- annulation ---------------------------------------------------------------------------

class Sig(BaseException):
    def __init__(self, name, **attrs):
        super().__init__(name)
        for k, v in attrs.items():
            setattr(self, k, v)

    def __str__(self):
        return self.args[0]


VOL = {"kind": "volume", "pool": "images", "volume": "x.qcow2", "error": "busy"}
CANCEL_CASES = [
    (SimpleNamespace(cmd="vm", vcmd="create", dry_run=False), Sig("SIGTERM"),
     "creation cancelled (SIGTERM) before anything was created", None),
    (SimpleNamespace(cmd="vm", vcmd="create", dry_run=False),
     Sig("SIGTERM", leftovers=[], created=[], failed="a"),
     "creation cancelled (SIGTERM): rolled back",
     {"created": [], "failed": "a", "leftovers": []}),
    (SimpleNamespace(cmd="vm", vcmd="create", dry_run=False),
     Sig("SIGTERM", leftovers=[VOL], created=[{"name": "m-01"}], failed="m-02"),
     "creation cancelled (SIGTERM): rollback incomplete, left on the host: volume "
     "images/x.qcow2 (busy); already created and kept: m-01",
     {"created": [{"name": "m-01"}], "failed": "m-02", "leftovers": [VOL]}),
    (SimpleNamespace(cmd="vm", vcmd="create", dry_run=True), Sig("SIGTERM"),
     "cancelled (SIGTERM)", None),
    (SimpleNamespace(cmd="vm", vcmd="edit", dry_run=False),
     Sig("SIGHUP", changes=[{"field": "title", "applied": "now"}], leftovers=[]),
     "edit cancelled (SIGHUP): some changes may already be applied: reload the VM",
     {"changes": [{"field": "title", "applied": "now"}], "leftovers": []}),
    (SimpleNamespace(cmd="vm", vcmd="add-volume"), Sig("SIGTERM"),
     "add-volume cancelled (SIGTERM): some changes may already be applied: reload the VM",
     None),
    (SimpleNamespace(cmd="vol", ocmd="upload", pool="images", name="v"),
     Sig("SIGTERM", leftovers=[]),
     "upload cancelled (SIGTERM): the partial volume was deleted", {"leftovers": []}),
    (SimpleNamespace(cmd="image", ocmd="upload", pool="images", name="v"),
     Sig("SIGTERM", leftovers=[VOL]),
     "upload cancelled (SIGTERM): the partial volume could not be deleted: volume "
     "images/x.qcow2 (busy)", {"leftovers": [VOL]}),
    (SimpleNamespace(cmd="image", ocmd="download-url", pool="images", name="v"),
     Sig("SIGTERM", stage="download"),
     "download cancelled (SIGTERM): nothing was sent to the host", None),
    (SimpleNamespace(cmd="vol", ocmd="upload", pool="images", name="v"), Sig("SIGTERM"),
     "upload cancelled (SIGTERM) before any volume was created", None),
    (SimpleNamespace(cmd="pool", ocmd="delete", dry_run=False), Sig("SIGTERM"),
     "pool delete cancelled (SIGTERM): it may already have been deleted: check the list", None),
    (SimpleNamespace(cmd="net", ocmd="stop"), Sig("SIGTERM"),
     "network stop cancelled (SIGTERM): it may already have been stopped: check the list",
     None),
]


@pytest.mark.parametrize("args,sig,msg,data", CANCEL_CASES)
def test_cancel_messages(args, sig, msg, data):
    assert cli.cancel_message(args, sig) == msg
    assert cli.cancel_data(args, sig) == data


FAKE_VIRSH = r'''#!{python}
# faux virsh d'un téléversement : pool `images` dans $KVMT_DIR, volumes = fichiers
import os, sys, time
d = os.environ["KVMT_DIR"]
args = sys.argv[1:]
while args and args[0] in ("-q", "-c"):
    args = args[2:] if args[0] == "-c" else args[1:]
cmd, rest = args[0], args[1:]
opt = lambda k: rest[rest.index(k) + 1]
vols = os.path.join(d, "vols")
if cmd == "pool-dumpxml":
    print(f"<pool type='dir'><name>images</name><available unit='bytes'>{10**12}</available>"
          f"<target><path>{vols}</path></target></pool>")
elif cmd == "pool-info":
    print("Name:           images\nState:          running\nPersistent:     yes\n"
          "Autostart:      yes")
elif cmd == "vol-dumpxml":
    p = os.path.join(vols, opt("--vol"))
    if not os.path.exists(p):
        sys.stderr.write(f"error: failed to get vol '{opt('--vol')}'\n"); sys.exit(1)
    n = os.path.getsize(p)
    print(f"<volume type='file'><name>{opt('--vol')}</name><key>{p}</key>"
          f"<capacity unit='bytes'>{n}</capacity><allocation unit='bytes'>{n}</allocation>"
          f"<target><path>{p}</path><format type='raw'/></target></volume>")
elif cmd == "vol-create-as":
    open(os.path.join(vols, opt("--name")), "wb").close()
elif cmd == "vol-delete":
    os.unlink(os.path.join(vols, opt("--vol")))
elif cmd == "vol-upload":
    data = b""
    while True:
        b = sys.stdin.buffer.read(65536)
        if not b:
            break
        data += b
        time.sleep(0.05)
    # écrit à la fin, comme un envoi qui aboutit : un ssh laissé en vie
    # recréerait donc le volume après le retour arrière
    with open(os.path.join(vols, opt("--vol")), "wb") as f:
        f.write(data)
else:
    sys.stderr.write(f"error: unexpected command {cmd}\n"); sys.exit(1)
'''


def _declare(state):
    kvm_decl.write_key(state, "lab", c197.PRIVATE_KEY)
    kvm_decl.write_known_hosts(state, "lab", [f"{ADDR} ssh-ed25519 AAAAed"])
    kvm_decl.write(state, {"name": "lab", "address": ADDR, "user": "virtops",
                           "ssh_key": kvm_decl.key_rel("lab"),
                           "known_hosts": kvm_decl.known_hosts_rel("lab")})


@pytest.mark.parametrize("sig", ["SIGTERM", "SIGHUP"])
def test_a_signal_during_the_upload_deletes_the_partial_volume_for_real(tmp_path, sig):
    """Vrai processus contre de faux ssh et virsh : le téléversement est
    arrêté pendant l'envoi. Code 3, volume partiel supprimé, aucun ssh ni
    virsh restant, rien ne réapparaît ensuite."""
    shim = tmp_path / "bin"
    shim.mkdir()
    for name, body in {"ssh": c197.FAKE_SSH,
                       "virsh": FAKE_VIRSH.replace("{python}", sys.executable)}.items():
        (shim / name).write_text(body)
        (shim / name).chmod(0o755)
    (tmp_path / "vols").mkdir()
    src = tmp_path / "big.raw"
    src.write_bytes(os.urandom(8 << 20))
    short = Path(tempfile.mkdtemp(prefix="kvmcu-", dir="/tmp"))
    state = short / "state"
    _declare(state)
    env = dict(os.environ, PATH=f"{shim}:{os.environ['PATH']}", KVMT_DIR=str(tmp_path))
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "bin" / "harvester-kvm.py"), "vol", "upload", "--json",
         f"--state-dir={state}", "--host=lab", "--pool=images", "--name=part.raw",
         f"--file={src}", "--no-verify"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (tmp_path / "vols" / "part.raw").exists():
            time.sleep(0.02)
        time.sleep(0.5)                        # l'envoi est en cours
        assert proc.poll() is None
        proc.send_signal(getattr(signal, sig))
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 3, err
        data = json.loads(out)
        assert data == {"ok": False, "kind": "cancelled", "leftovers": [],
                        "error": f"upload cancelled ({sig}): the partial volume was deleted"}
        assert "STEP_EVENT|upload|running|" in err
        time.sleep(1.0)                        # un ssh resté en vie finirait son envoi
        assert list((tmp_path / "vols").iterdir()) == []
        left = subprocess.run(["pgrep", "-f", str(tmp_path)], capture_output=True, text=True)
        assert left.stdout.strip() == ""
    finally:
        if proc.poll() is None:
            proc.kill()
        shutil.rmtree(short, ignore_errors=True)


LAUNCHER = r'''
import json, sys, time
sys.path.insert(0, {api!r}); sys.path.insert(0, {lib!r})
import test_kvm_cli_create as t
import kvm_create as kc
s = t.world()
net = t.Net(s)
net.delay = 0.2                                 # seed envoyé lentement
t.cli.RUNNER = net
t.cli.POPEN = net.popen
started = open({flag!r}, "w")
net.on_chunk = lambda n: (started.write("x"), started.flush())
try:
    rc = t.cli.main(sys.argv[1:])
finally:
    json.dump({{"domains": sorted(s.domains), "vols": sorted(s.pools["images"]["vols"]),
               "writes": [list(w) for w in s.writes]}}, open({dump!r}, "w"))
sys.exit(rc)
'''


def test_a_signal_during_the_seed_upload_rolls_the_creation_back_for_real(tmp_path):
    """Vrai processus (le Sim chargé par un lanceur) : SIGTERM pendant
    l'envoi du seed. Code 3, disque et seed créés par la création supprimés,
    aucune VM définie."""
    short = Path(tempfile.mkdtemp(prefix="kvmcc-", dir="/tmp"))
    state = short / "state"
    _declare(state)
    flag, dump = tmp_path / "started", tmp_path / "dump.json"
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"name": "cc", "disks": [
        {"source": {"kind": "blank", "pool": "images", "size_bytes": 1 << 20}}],
        "cloudinit": {"user_data": UD + "x" * (800 << 10), "seed_pool": "images"}}))
    launcher = tmp_path / "launch.py"
    launcher.write_text(LAUNCHER.format(api=str(ROOT / "tests" / "api"),
                                        lib=str(ROOT / "bin" / "lib"), flag=str(flag),
                                        dump=str(dump)))
    env = dict(os.environ, HARVESTER_OPS_KVM_SURVEY_CACHE="0")
    proc = subprocess.Popen([sys.executable, str(launcher), "vm", "create", "--json",
                             f"--state-dir={state}", "--host=lab", f"--spec-file={spec}"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and proc.poll() is None and not (
                flag.exists() and flag.stat().st_size):
            time.sleep(0.02)
        assert proc.poll() is None, proc.communicate()
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 3, err
        data = json.loads(out)
        assert data["kind"] == "cancelled" and data["leftovers"] == []
        assert data["error"] == "creation cancelled (SIGTERM): rolled back"
        world_after = json.loads(dump.read_text())
        assert "cc" not in world_after["domains"]
        assert "cc-vda.qcow2" not in world_after["vols"]
        assert "cc-cidata.iso" not in world_after["vols"]
        assert ["vol-delete", "--pool", "images", "--vol", "cc-cidata.iso"] in world_after["writes"]
        assert UD_MARK not in out + err
    finally:
        if proc.poll() is None:
            proc.kill()
        shutil.rmtree(short, ignore_errors=True)


def test_upload_interrupted_during_the_volume_creation_removes_it():
    """Annulation pendant `vol-create-as` : le nom était libre juste avant,
    le volume éventuellement créé est donc le nôtre et il est retiré."""
    s = world()
    orig = s._answer

    def interrupted(args, data=None):
        out = orig(args, data)
        if args and args[0] == "vol-create-as":
            raise cli.Cancelled("SIGTERM")
        return out
    s._answer = interrupted
    src = Path(tempfile.mkdtemp(prefix="kvmcv-")) / "f.raw"
    src.write_bytes(b"z" * 100)
    try:
        with pytest.raises(cli.Cancelled) as e:
            ks.upload_volume(s, "images", "i.raw", str(src))
    finally:
        shutil.rmtree(src.parent)
    assert e.value.leftovers == []
    assert ("vol-delete", "--pool", "images", "--vol", "i.raw") in s.writes
    assert "i.raw" not in s.pools["images"]["vols"]
