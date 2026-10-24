"""Prochaine version KVM, CLI : corrections après la revue de la tâche 3.

- I1 : toute suppression réelle (vm, vol, image, pool, net) exige
  l'empreinte de son aperçu ; sans elle, erreur d'usage avant l'hôte.
- I2 : une création tient en exclusif chaque volume qu'elle va créer (et en
  partage chaque réseau cité) ; les suppressions, arrêts et envois du même
  objet se heurtent à ces verrous (`busy`), joués en vrai entrelacement :
  la commande concurrente tourne PENDANT l'écriture de la création.
- I3 : un volume partiel encore « in use » est réessayé avant d'être nommé.
- Mineures : pool ou réseau recréé (nouvel uuid), --remove-dir ajouté après
  l'aperçu, robustesse des specs et des options, mot de passe VNC jamais
  recopié depuis une erreur, politique d'adresses du téléchargement.
"""

import json
import shlex

import pytest

import test_kvm_cli_197 as c197
import test_kvm_cli_create as t
env = t.env                                 # fixture du module voisin

import kvm_host as kh
import kvm_storage as ks

cli = t.cli
run = t.run
ANY = t.ANY_DIGEST
BLANK = [{"source": {"kind": "blank", "pool": "images", "size_bytes": 1 << 20}}]


def no_remote(env):
    return env.net.remotes == [] and env.net.fresh == []


# --- I1 : empreinte obligatoire ----------------------------------------------------------------

def _i1_world():
    s = t.vm_world()
    s.add_pool("p2", "/srv/p2")
    s.add_net("n2", "10.1.2.0/24", bridge="virbr9", forward="nat")
    return s


I1_CASES = [
    ("vm delete", ["vm", "delete", "--host=lab", "--name=vm1"], "a VM"),
    ("vm delete storage", ["vm", "delete", "--host=lab", "--name=vm1", "--remove-storage"],
     "a VM"),
    ("vol delete", ["vol", "delete", "--host=lab", "--pool=images", "--name=free.qcow2"],
     "a volume"),
    ("image delete", ["image", "delete", "--host=lab", "--pool=images",
                      "--volume=free.qcow2"], "an image"),
    ("pool delete", ["pool", "delete", "--host=lab", "--pool=p2"], "a pool"),
    ("pool delete dir", ["pool", "delete", "--host=lab", "--pool=p2", "--remove-dir"],
     "a pool"),
    ("net delete", ["net", "delete", "--host=lab", "--network=n2"], "a network"),
]


@pytest.mark.parametrize("label,argv,what", I1_CASES, ids=[c[0] for c in I1_CASES])
def test_a_real_delete_without_its_preview_digest_is_a_usage_error(env, capsys, label, argv,
                                                                    what):
    env.use(_i1_world())
    res = run(capsys, *argv)
    assert res.rc == 2 and set(res.data) == {"ok", "error", "kind"}
    assert res.data["kind"] == "usage"
    assert res.data["error"] == (f"--expect-delete is required to delete {what}: run the same "
                                 "command with --dry-run first, then pass the digest it shows")
    assert no_remote(env) and t.writes(env) == []
    # le texte lisible donne la même indication
    text = run(capsys, *argv, as_json=False)
    assert text.rc == 2 and "--dry-run first" in text.err
    # l'aperçu, lui, n'en a pas besoin et montre l'empreinte à passer
    prev = run(capsys, *argv, "--dry-run")
    assert prev.rc == 0 and len(prev.data["plan"]["digest"]) == 64
    res = run(capsys, *argv, f"--expect-delete={prev.data['plan']['digest']}")
    assert res.rc == 0, res.data


def test_the_console_always_passes_the_vm_delete_digest():
    """La route de la 1.98 refuse sans empreinte et la passe toujours au
    script : rendre l'option obligatoire ne casse pas la console."""
    src = (t.ROOT / "web" / "app.py").read_text()
    body = src[src.index("def api_kvm_vm_delete("):]
    body = body[:body.index("\n@app.route")]
    assert 'raise _ko.OpError("invalid", "expect_delete is required' in body
    assert 'cmd.append(f"--expect-delete={digest}")' in body


# --- I2 : verrous, joués en entrelacement réel ----------------------------------------------------

def _during_first_write(sim, verb, action):
    """Lance `action()` une fois, au moment où la création écrit `verb`
    (la création tient alors ses verrous)."""
    orig = sim._answer
    box = {"done": False, "result": None}

    def hooked(args, data=None):
        if not box["done"] and args and args[0] == verb:
            box["done"] = True
            box["result"] = action()
        return orig(args, data)
    sim._answer = hooked
    return box


def _inner(capsys, argv):
    """Commande concurrente, dans le même processus, pendant la création :
    son objet JSON est lu tout de suite (la création n'a encore rien écrit
    sur stdout)."""
    rc = cli.main(list(argv) + ["--json"])
    out = capsys.readouterr().out
    return rc, json.loads(out)


INTERLEAVE = [
    # libellé, commande concurrente, spec avec seed
    ("vol delete of the disk being created",
     ["vol", "delete", "--host=lab", "--pool=images", "--name=n1-vda.qcow2", ANY], False),
    ("image delete of the disk being created",
     ["image", "delete", "--host=lab", "--pool=images", "--volume=n1-vda.qcow2", ANY], False),
    ("vol delete of the seed still to come",
     ["vol", "delete", "--host=lab", "--pool=images", "--name=n1-cidata.iso", ANY], True),
    ("upload under the name of the disk",
     ["vol", "upload", "--host=lab", "--pool=images", "--name=n1-vda.qcow2", "FILE"], False),
    ("net delete of the cited network",
     ["net", "delete", "--host=lab", "--network=default", ANY], False),
    ("net stop of the cited network", ["net", "stop", "--host=lab", "--network=default"], False),
    ("pool delete of the written pool",
     ["pool", "delete", "--host=lab", "--pool=images", ANY], False),
    ("pool stop of the written pool", ["pool", "stop", "--host=lab", "--pool=images"], False),
]


@pytest.mark.parametrize("label,inner,seed", INTERLEAVE, ids=[c[0] for c in INTERLEAVE])
def test_a_concurrent_command_during_a_creation_is_busy(env, capsys, label, inner, seed):
    sim = t.world()
    env.use(sim)
    src = env.short / "up.raw"
    src.write_bytes(b"z" * 4096)
    inner = [f"--file={src}" if a == "FILE" else a for a in inner]
    spec = {"name": "n1", "disks": BLANK,
            "nics": [{"kind": "network", "source": "default"}]}
    if seed:
        spec["cloudinit"] = {"user_data": t.UD, "seed_pool": "images"}
    before = {}
    box = _during_first_write(sim, "vol-create-as", lambda: (
        before.setdefault("writes", len(sim.writes)), _inner(capsys, inner))[1])
    res = run(capsys, "vm", "create", "--host=lab", env.spec(spec))
    assert box["done"]
    rc, data = box["result"]
    assert rc == 2 and data["kind"] == "busy", data
    assert "is in progress" in data["error"]
    # la commande concurrente n'a rien écrit ; la création, elle, aboutit
    assert res.rc == 0, res.data
    assert [w[0] for w in sim.writes].count("vol-delete") == 0
    assert "n1" in sim.domains and "default" in sim.networks
    assert sim.pools["images"].get("state", "running") == "running"


HELD = [
    # libellé, verrou tenu (genre, nom, partagé), commande, champ du message
    ("create while its disk is being deleted", ("volume", "images/n1-vda.qcow2", False),
     ["vm", "create", "--host=lab", "SPEC"], "volume images/n1-vda.qcow2"),
    ("create while its seed name is being uploaded", ("volume", "images/n1-cidata.iso", False),
     ["vm", "create", "--host=lab", "SEEDSPEC"], "volume images/n1-cidata.iso"),
    ("create while its network is being deleted", ("network", "default", False),
     ["vm", "create", "--host=lab", "SPEC"], "network default"),
    ("cloudinit while its pool is being deleted", ("pool", "images", False),
     ["vm", "cloudinit", "--host=lab", "--name=vm1", "CISPEC"], "pool images"),
    ("cloudinit while its new seed name is taken", ("volume", "images/vm1-cidata-1.iso", False),
     ["vm", "cloudinit", "--host=lab", "--name=vm1", "CISPEC"], "volume images/vm1-cidata-1.iso"),
    ("add-volume while its pool is being deleted", ("pool", "images", False),
     ["vm", "add-volume", "--host=lab", "--name=vm1", "DISKSPEC"], "pool images"),
]


@pytest.mark.parametrize("label,held,argv,what", HELD, ids=[c[0] for c in HELD])
def test_a_writer_meets_the_lock_of_a_destructive_operation(env, capsys, label, held, argv,
                                                             what):
    env.use(t.vm_world(cloudinit=True))
    sub = {"SPEC": env.spec({"name": "n1", "disks": BLANK,
                             "nics": [{"kind": "network", "source": "default"}]}, "a.json"),
           "SEEDSPEC": env.spec({"name": "n1", "disks": BLANK,
                                 "cloudinit": {"user_data": t.UD, "seed_pool": "images"}},
                                "b.json"),
           "CISPEC": env.spec({"user_data": t.UD}, "c.json"),
           "DISKSPEC": env.spec({"source": {"kind": "blank", "pool": "images",
                                            "size_bytes": 1 << 20}}, "d.json")}
    kind, name, shared = held
    with cli.object_lock(env.state, "lab", kind, name, shared=shared):
        res = run(capsys, *[sub.get(a, a) for a in argv])
    assert res.rc == 2 and res.data["kind"] == "busy", res.data
    assert f"another operation on {what} is in progress" in res.data["error"]
    assert t.writes(env) == []                    # refusé avant toute écriture
    assert t.UD_MARK not in res.out + res.err


def test_creations_share_pools_and_networks(env, capsys):
    """Deux créations dans le même pool et sur le même réseau ne se
    gênent pas (verrous partagés) ; seuls leurs volumes sont exclusifs."""
    env.use(t.world())
    with cli.object_lock(env.state, "lab", "pool", "images", shared=True), \
            cli.object_lock(env.state, "lab", "network", "default", shared=True):
        res = run(capsys, "vm", "create", "--host=lab", env.spec({
            "name": "n1", "disks": BLANK, "nics": [{"kind": "network", "source": "default"}]}))
    assert res.rc == 0, res.data


def test_net_start_does_not_wait_for_a_creation(env, capsys):
    s = t.world()
    s.add_net("off", "10.9.9.0/24", bridge="virbr7", forward="nat", active=False)
    env.use(s)
    with cli.object_lock(env.state, "lab", "network", "off", shared=True):
        assert run(capsys, "net", "start", "--host=lab", "--network=off").rc == 0
        res = run(capsys, "net", "stop", "--host=lab", "--network=off")
        assert res.rc == 2 and res.data["kind"] == "busy"


# --- I3 : volume partiel encore tenu par libvirt ------------------------------------------------

IN_USE = "error: Requested operation is not valid: volume 'p.raw' is still in use."


class DelHost:
    """Hôte minimal : `vol-delete` rend la suite d'erreurs donnée, puis
    réussit."""
    name = "lab"

    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = 0

    def virsh(self, *args, **kw):
        assert args[:1] == ("vol-delete",)
        self.calls += 1
        if self.errors:
            raise kh.make_error("lab", self.errors.pop(0), 1)
        return ""


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


PARTIAL = [
    # libellé, erreurs successives, reste attendu, attentes, appels
    ("deleted at once", [], None, [], 1),
    ("already gone", ["error: failed to get vol 'p.raw'\n"], None, [], 1),
    ("in use twice then deleted", [IN_USE, IN_USE], None, [0.5, 1.0], 3),
    ("still being allocated then deleted",
     ["error: Requested operation is not valid: volume 'p.raw' is still being allocated."],
     None, [0.5], 2),
    ("in use until the deadline", [IN_USE] * 50, "still in use",
     [0.5, 1.0, 2.0] + [4.0] * 6, 10),
    ("another error is said at once", ["error: internal error: disk on fire\n"], "disk on fire",
     [], 1),
]


@pytest.mark.parametrize("label,errors,left,sleeps,calls", PARTIAL, ids=[c[0] for c in PARTIAL])
def test_a_partial_volume_in_use_is_retried_with_a_bounded_backoff(label, errors, left, sleeps,
                                                                   calls):
    host, clock = DelHost(errors), Clock()
    res = ks._delete_partial(host, "images", "p.raw", retry_for=30, sleep=clock.sleep,
                             clock=clock)
    assert clock.sleeps == sleeps and host.calls == calls
    assert sum(clock.sleeps) <= 30
    if left is None:
        assert res is None
    else:
        assert res["kind"] == "volume" and res["volume"] == "p.raw" and left in res["error"]


def test_the_partial_delete_retry_is_tunable(monkeypatch):
    host, clock = DelHost([IN_USE] * 50), Clock()
    monkeypatch.setattr(ks, "PARTIAL_DELETE_RETRY", 2.0)
    res = ks._delete_partial(host, "images", "p.raw", sleep=clock.sleep, clock=clock)
    assert res is not None and clock.sleeps == [0.5, 1.0]


# --- mineures : objets recréés, --remove-dir après l'aperçu -------------------------------------

def _with_uuids(sim):
    """Le Sim n'écrit pas d'uuid : on l'ajoute aux XML de pool et de réseau,
    comme libvirt."""
    uuids = {}
    pd, nd = sim.v_pool_dumpxml, sim.v_net_dumpxml

    def pool_xml(o, p, data):
        rc, out, err = pd(o, p, data)
        u = uuids.get(("pool", o["pool"]))
        return rc, out.replace("</name>", f"</name><uuid>{u}</uuid>", 1) if u else out, err

    def net_xml(o, p, data):
        rc, out, err = nd(o, p, data)
        u = uuids.get(("net", o["network"]))
        return rc, out.replace("</name>", f"</name><uuid>{u}</uuid>", 1) if u else out, err
    sim.v_pool_dumpxml, sim.v_net_dumpxml = pool_xml, net_xml
    return uuids


RECREATED = [
    ("pool", "p2", ["pool", "delete", "--host=lab", "--pool=p2"]),
    ("pool", "p2", ["pool", "delete", "--host=lab", "--pool=p2", "--remove-dir"]),
    ("net", "n2", ["net", "delete", "--host=lab", "--network=n2"]),
]


@pytest.mark.parametrize("kind,name,argv", RECREATED, ids=[" ".join(c[2][:1] + c[2][4:])
                                                           for c in RECREATED])
def test_an_object_recreated_with_the_same_name_and_path_is_plan_changed(env, capsys, kind,
                                                                         name, argv):
    s = t.world()
    s.add_pool("p2", "/srv/p2")
    s.add_net("n2", "10.1.2.0/24", bridge="virbr9", forward="nat")
    uuids = _with_uuids(s)
    uuids[(kind, name)] = "11111111-1111-1111-1111-111111111111"
    env.use(s)
    prev = run(capsys, *argv, "--dry-run")
    assert prev.rc == 0 and prev.data["possible"] is True, prev.data
    # supprimé puis recréé à l'identique (même nom, même chemin ou même pont)
    uuids[(kind, name)] = "22222222-2222-2222-2222-222222222222"
    res = run(capsys, *argv, f"--expect-delete={prev.data['plan']['digest']}")
    assert res.rc == 1 and res.data["kind"] == "plan-changed", res.data
    assert "preview again" in res.data["error"]
    assert not [w for w in t.writes(env) if w[0] in ("pool-destroy", "pool-delete",
                                                     "pool-undefine", "net-destroy",
                                                     "net-undefine")]


def test_remove_dir_added_after_the_preview_is_refused(env, capsys):
    s = t.world()
    s.add_pool("p2", "/srv/p2")
    env.use(s)
    prev = run(capsys, "pool", "delete", "--host=lab", "--pool=p2", "--dry-run")
    res = run(capsys, "pool", "delete", "--host=lab", "--pool=p2", "--remove-dir",
              f"--expect-delete={prev.data['plan']['digest']}")
    assert res.rc == 1 and res.data["kind"] == "plan-changed"
    assert not [w for w in t.writes(env) if w[0].startswith("pool-")]
    assert "p2" in s.pools


@pytest.mark.parametrize("flag,want", [
    ("--remove-dir", "the directory is removed only if it is empty: the deletion is refused "
                     "while the pool holds volumes"),
    (None, "the directory and its files are kept"),
])
def test_pool_delete_preview_says_what_happens_to_the_directory(env, capsys, flag, want):
    s = t.world()
    s.add_pool("p2", "/srv/p2")
    env.use(s)
    text = run(capsys, "pool", "delete", "--host=lab", "--pool=p2", "--dry-run",
               *([flag] if flag else []), as_json=False)
    assert text.rc == 0 and want in text.out
    assert "its files are removed" not in text.out


# --- mineures : robustesse des specs et des options ---------------------------------------------

DEEP = '{"name": "x", "a": ' + "[" * 40 + "]" * 40 + "}"
VERY_DEEP = "[" * 200_000 + "]" * 200_000
ROBUST = [
    (["vm", "create", "--host=lab", ("deep.json", DEEP)], "nests deeper than 32 levels"),
    (["vm", "create", "--host=lab", ("vdeep.json", VERY_DEEP[:900_000])], "nests deeper"),
    (["vm", "create", "--host=lab", ("nan.json", '{"name": "x", "vcpus": NaN}')],
     "not finite"),
    (["vm", "create", "--host=lab", ("inf.json", '{"name": "x", "memory": -Infinity}')],
     "not finite"),
    (["vm", "create", "--host=lab", ("big.json", '{"name": "x", "memory": 1e400}')],
     "not finite"),
    (["vm", "add-volume", "--host=lab", "--name=vm1", ("d.json", "{" + '"a":{' * 40 + "}" * 41)],
     "nests deeper"),
    (["vm", "cloudinit", "--host=lab", "--name=vm1", ("c.json", '{"hostname": NaN}')],
     "not finite"),
    (["vol", "upload", "--host=lab", "--pool=images", "--name=v", "--file=/etc/hostname",
      "--limit-mbps=nan"], "positive finite"),
    (["vol", "upload", "--host=lab", "--pool=images", "--name=v", "--file=/etc/hostname",
      "--limit-mbps=inf"], "positive finite"),
    (["vol", "create", "--host=lab", "--pool=images", "--name=v", "--capacity=²G"],
     "invalid --capacity"),
    (["image", "download-url", "--host=lab", "--pool=images", "--name=v",
      "--url=http://example.org:99999/x"], "valid port"),
    (["image", "download-url", "--host=lab", "--pool=images", "--name=v",
      "--url=http://example.org:abc/x"], "valid port"),
    (["image", "download-url", "--host=lab", "--pool=images", "--name=v",
      "--url=https://example.org:0/x"], "valid port"),
    (["image", "download-url", "--host=lab", "--pool=images", "--name=v",
      "--url=ftp://example.org/x"], "http or https"),
    (["image", "download-url", "--host=lab", "--pool=images", "--name=v",
      "--url=http://example.org/x", "--allow-private=bogus"], "--allow-private"),
    (["image", "download-url", "--host=lab", "--pool=images", "--name=v",
      "--url=http://example.org/x", "--max-size=nan"], "invalid --max-size"),
]


@pytest.mark.parametrize("argv,msg", ROBUST, ids=[f"{i}" for i in range(len(ROBUST))])
def test_hostile_specs_and_options_are_usage_errors_before_the_host(env, capsys, argv, msg):
    env.use(t.vm_world())
    out = []
    for a in argv:
        if isinstance(a, tuple):
            p = env.short / a[0]
            p.write_text(a[1])
            out.append(f"--spec-file={p}")
        else:
            out.append(a)
    res = run(capsys, *out)
    assert res.rc == 2, res.err
    assert set(res.data) == {"ok", "error", "kind"} and res.data["kind"] == "usage"
    assert msg in res.data["error"]
    assert no_remote(env) and "Traceback" not in res.err


# --- mineures : mot de passe VNC dans une erreur qui recopie le XML -----------------------------

@pytest.mark.parametrize("verb", ["define", "start"])
def test_an_error_echoing_the_xml_never_prints_the_vnc_password(env, capsys, monkeypatch, verb):
    env.use(t.world())
    real = cli.RUNNER
    seen = {}

    def echo(argv, **kw):
        rem = shlex.split(argv[-1]) if argv and argv[0] == "ssh" and len(argv) > 3 else []
        if kw.get("input") and "define" in rem:
            seen["xml"] = kw["input"]
            seen["pw"] = kw["input"].split("passwd=\"")[1].split("\"")[0]
            if verb == "define":
                return c197.ns(1, "", "error: XML error: invalid domain\n" + kw["input"])
        if verb == "start" and rem[5:6] == ["start"]:
            return c197.ns(1, "", "error: internal error: qemu failed with "
                                  f"<graphics type='vnc' passwd='{seen['pw']}'/> and "
                                  f"passwd=\"{seen['pw']}\"\n")
        return real(argv, **kw)
    monkeypatch.setattr(cli, "RUNNER", echo)
    for as_json in (True, False):
        res = run(capsys, "vm", "create", "--host=lab", env.spec({
            "name": "pw1", "disks": BLANK, "start": True,
            "display": {"vnc": True, "password": True}}), as_json=as_json)
        assert res.rc == 1, res.err
        assert len(seen["pw"]) == 8
        assert seen["pw"] not in res.out + res.err
        assert "pw1" not in env.net.sim.domains            # retour arrière


# --- mineures : adresses du téléchargement ------------------------------------------------------

ADDRESSES = [
    ("8.8.8.8", (), True),
    ("2001:4860:4860::8888", (), True),
    ("127.0.0.1", (), False),
    ("::1", (), False),
    ("169.254.169.254", (), False),                # métadonnées des clouds
    ("fd00:ec2::254", (), False),
    ("10.1.2.3", (), False),
    ("172.16.3.6", (), False),
    ("192.168.1.1", (), False),
    ("100.100.100.200", (), False),                # partagée (100.64/10)
    ("0.0.0.0", (), False),
    ("224.0.0.1", (), False),
    ("::ffff:127.0.0.1", (), False),
    ("fe80::1%eth0", (), False),
    ("172.16.3.6", ("172.16.0.0/16",), True),
    ("::ffff:172.16.3.6", ("172.16.0.0/16",), True),
    ("10.1.2.3", ("172.16.0.0/16",), False),
    # toujours refusées, même couvertes par la liste d'autorisation (1.99.0, f7bb19c)
    ("169.254.169.254", ("0.0.0.0/0",), False),
    ("169.254.169.254", ("169.254.0.0/16",), False),
    ("::ffff:169.254.169.254", ("0.0.0.0/0",), False),
    ("fd00:ec2::254", ("::/0",), False),
    ("0.0.0.0", ("0.0.0.0/0",), False),
    ("::", ("::/0",), False),
    ("224.0.0.1", ("224.0.0.0/4",), False),
    ("ff02::1", ("::/0",), False),
    ("255.255.255.255", ("0.0.0.0/0",), False),
    # la liste n'ouvre que bouclage, lien local hors métadonnées, privées
    ("127.0.0.1", ("127.0.0.0/8",), True),
    ("169.254.1.1", ("169.254.0.0/16",), True),
    ("fe80::1", ("fe80::/10",), True),
]


@pytest.mark.parametrize("addr,allow,ok", ADDRESSES, ids=[f"{a} {b}" for a, b, _ in ADDRESSES])
def test_download_address_policy(addr, allow, ok):
    assert cli.address_allowed(addr, cli.parse_cidrs(allow)) is ok


def test_download_from_a_loopback_server_is_refused_without_allow_private(env, capsys,
                                                                          files_server):
    root, base = files_server
    (root / "c.qcow2").write_bytes(t.qcow2_bytes())
    env.use(t.world())
    res = run(capsys, "image", "download-url", "--host=lab", "--pool=images", "--name=c.qcow2",
              f"--url={base}/c.qcow2", "--sha256=" + "e" * 64)
    assert res.rc == 1 and res.data["kind"] == "refused", res.data
    assert "127.0.0.1" in res.data["error"] and "--allow-private=CIDR" in res.data["error"]
    assert t.writes(env) == [] and no_remote(env)
    assert list((env.state / "kvm-images" / "staging").iterdir()) == []


@pytest.mark.parametrize("target", ["http://169.254.169.254/latest/meta-data/",
                                    "http://10.255.255.1/x", "http://[::1]:9/x",
                                    "http://localhost:9/x"])
def test_every_redirect_hop_is_checked(env, capsys, files_server, target):
    """Le premier serveur est autorisé (127.0.0.1/32 seulement) ; sa
    redirection vers une adresse interne est refusée avant toute connexion."""
    root, base = files_server
    files_server_redirect[0] = target
    env.use(t.world())
    res = run(capsys, "image", "download-url", "--host=lab", "--pool=images", "--name=c.qcow2",
              f"--url={base}/hop", "--allow-private=127.0.0.1/32", "--sha256=" + "e" * 64)
    assert res.rc == 1 and res.data["kind"] == "refused", res.data
    if "169.254.169.254" in target:                # métadonnées : jamais, pas de piste
        assert "never reachable" in res.data["error"]
    else:
        assert "--allow-private=CIDR" in res.data["error"]
    assert t.writes(env) == [] and no_remote(env)


def test_download_size_cap(env, capsys, files_server):
    root, base = files_server
    (root / "c.qcow2").write_bytes(t.qcow2_bytes())
    env.use(t.world())
    res = run(capsys, "image", "download-url", "--host=lab", "--pool=images", "--name=c.qcow2",
              f"--url={base}/c.qcow2", "--allow-private=127.0.0.1/32", "--max-size=1K",
              "--sha256=" + "e" * 64)
    assert res.rc == 1 and res.data["kind"] == "refused" and "--max-size" in res.data["error"]
    assert t.writes(env) == []
    assert list((env.state / "kvm-images" / "staging").iterdir()) == []


files_server_redirect = [None]


class _Hop(t._Files):
    def do_GET(self):
        if self.path == "/hop":
            self.send_response(302)
            self.send_header("Location", files_server_redirect[0])
            self.end_headers()
            return
        return super().do_GET()


@pytest.fixture
def files_server(tmp_path):
    import http.server
    import threading
    root = tmp_path / "www"
    root.mkdir()

    def handler(*a, **kw):
        return _Hop(*a, directory=str(root), **kw)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield root, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


# --- après la revue de la tâche 4 : règles du téléchargement alignées sur 1.99.0 ----------------

REASONS = [
    ("169.254.169.254", ("0.0.0.0/0",), "metadata"),
    ("0.0.0.0", ("0.0.0.0/0",), "unspecified"),
    ("::", ("::/0",), "unspecified"),
    ("239.1.2.3", ("0.0.0.0/0",), "multicast"),
    ("255.255.255.255", ("0.0.0.0/0",), "broadcast"),
    ("127.0.0.1", (), "loopback"),
    ("169.254.1.1", (), "link-local"),
    ("10.1.2.3", (), "private"),
    ("100.64.0.1", (), "private"),
    ("240.0.0.1", (), "private"),               # 240/4 est « privée » pour ipaddress
    ("8.8.8.8", (), None),
]


@pytest.mark.parametrize("addr,allow,why", REASONS, ids=[r[0] for r in REASONS])
def test_address_refusal_reasons_are_those_of_the_shared_guard(addr, allow, why):
    assert cli.address_refusal(addr, cli.parse_cidrs(allow)) == why


@pytest.mark.parametrize("url", ["http://user:s3cr3tpw@example.org/x.img",
                                 "https://s3cr3tpw@example.org/x.img",
                                 "https://:s3cr3tpw@example.org/x.img",
                                 "https://@example.org/x.img"])
def test_urls_with_credentials_are_refused_before_any_network_call(env, capsys, monkeypatch, url):
    env.use(t.world())
    monkeypatch.setattr(cli.socket, "getaddrinfo", lambda *a, **k: pytest.fail("network call"))
    assert "user name or password" in cli.check_url(url)
    res = run(capsys, "image", "download-url", "--host=lab", "--pool=images", "--name=c.qcow2",
              f"--url={url}", "--sha256=" + "e" * 64)
    assert res.rc == 2 and res.data["kind"] == "usage", res.data
    assert "s3cr3tpw" not in res.out + res.err and "user:" not in res.out + res.err
    assert no_remote(env)


def test_plain_http_needs_the_sha256(env, capsys):
    env.use(t.world())
    res = run(capsys, "image", "download-url", "--host=lab", "--pool=images", "--name=c.qcow2",
              "--url=http://example.org/x.img")
    assert res.rc == 2 and "--sha256 is required" in res.data["error"]
    assert no_remote(env)


@pytest.mark.parametrize("old,new,refused", [
    ("https://a.example/x", "http://b.example/y", True),
    ("https://a.example/x", "https://b.example/y", False),
    ("http://a.example/x", "https://b.example/y", False),
    ("http://a.example/x", "http://b.example/y", False),
    ("https://a.example/x", "http://u:pw@b.example/y", True),
])
def test_redirects_never_go_from_https_to_http(old, new, refused):
    import urllib.request
    h = cli._Redirects()
    req = urllib.request.Request(old)
    if refused:
        with pytest.raises(cli.Failure) as e:
            h.redirect_request(req, None, 302, "Found", {}, new)
        assert e.value.kind == "refused" and "pw" not in e.value.message
        assert "b.example" not in e.value.message
    else:
        assert h.redirect_request(req, None, 302, "Found", {}, new).full_url == new


def test_a_redirect_to_the_metadata_address_is_refused_even_with_a_broad_allowlist(env, capsys,
                                                                                 files_server):
    files_server_redirect[0] = "http://169.254.169.254/latest/meta-data/"
    root, base = files_server
    env.use(t.world())
    res = run(capsys, "image", "download-url", "--host=lab", "--pool=images", "--name=c.qcow2",
              f"--url={base}/hop", "--allow-private=0.0.0.0/0", "--sha256=" + "e" * 64)
    assert res.rc == 1 and res.data["kind"] == "refused", res.data
    assert "metadata" in res.data["error"] and "never reachable" in res.data["error"]
    assert "--allow-private" not in res.data["error"]
    assert t.writes(env) == [] and list((env.state / "kvm-images" / "staging").iterdir()) == []


# --- après la revue de la tâche 4 : verrous des objets cités --------------------------------

DIGEST64 = "b" * 64


@pytest.mark.parametrize("held,argv,what", [
    (("pool", "images"), ["vol", "delete", "--host=lab", "--pool=images", "--name=free.qcow2",
                          f"--expect-delete={DIGEST64}"], "pool images"),
    (("pool", "images"), ["image", "delete", "--host=lab", "--pool=images", "--volume=free.qcow2",
                          f"--expect-delete={DIGEST64}"], "pool images"),
    (("pool", "images"), ["image", "verify", "--host=lab", "--pool=images", "--volume=free.qcow2"],
     "pool images"),
    (("volume", "images/free.qcow2"), ["image", "verify", "--host=lab", "--pool=images",
                                       "--volume=free.qcow2"], "volume images/free.qcow2"),
    (("pool", "images"), ["vm", "edit", "--host=lab", "--name=vm1", "DISKEDIT",
                          f"--expect-config={DIGEST64}"], "pool images"),
    (("network", "default"), ["vm", "edit", "--host=lab", "--name=vm1", "NICEDIT",
                              f"--expect-config={DIGEST64}"], "network default"),
    (("network", "default"), ["vm", "add-nic", "--host=lab", "--name=vm1", "NIC"], "network default"),
])
def test_commands_hold_the_pool_or_network_they_use(env, capsys, held, argv, what):
    env.use(t.vm_world())
    sub = {"DISKEDIT": env.spec({"disks": BLANK}, "e1.json"),
           "NICEDIT": env.spec({"nics": [{"kind": "network", "source": "default"}]}, "e2.json"),
           "NIC": env.spec({"kind": "network", "source": "default"}, "n.json")}
    with cli.object_lock(env.state, "lab", *held):          # arrêt ou retrait en cours
        res = run(capsys, *[sub.get(a, a) for a in argv])
    assert res.rc == 2 and res.data["kind"] == "busy", res.data
    assert f"another operation on {what} is in progress" in res.data["error"]
    assert t.writes(env) == []


# --- après la revue de la tâche 4 : modèles et propriétaire ----------------------------------

def test_template_of_another_person_is_kept_unless_replace_other(env, capsys):
    spec = env.spec({"vcpus": 1})
    assert run(capsys, "template", "save", "--name=t1", spec, "--owner=alice").rc == 0
    res = run(capsys, "template", "save", "--name=t1", spec, "--owner=bob")
    assert res.rc == 1 and res.data["kind"] == "refused" and "belongs to alice" in res.data["error"]
    res = run(capsys, "template", "delete", "--name=t1", "--owner=bob")
    assert res.rc == 1 and res.data["kind"] == "refused"
    assert run(capsys, "template", "save", "--name=t1", spec, "--owner=alice").rc == 0
    assert run(capsys, "template", "save", "--name=t1", spec, "--owner=bob",
               "--replace-other").rc == 0
    assert run(capsys, "template", "delete", "--name=t1", "--owner=bob").rc == 0
