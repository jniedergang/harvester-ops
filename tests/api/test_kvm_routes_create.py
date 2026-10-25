"""Prochaine version KVM, tâche 4 : routes de la console pour créer et
modifier des VMs, les gestes à chaud, le catalogue d'images (téléverser,
télécharger, vérifier, supprimer), les volumes, pools, réseaux, pools
d'images et modèles.

Lectures par la bibliothèque (simulée ici), mutations par
bin/harvester-kvm.py en action suivie, valeurs en `--option=valeur`, specs
par un fichier 0600 supprimé à la fin. Toute suppression exige l'empreinte
de l'aperçu ; rôles d'après la table de `_kvm_required_role` ; 409 quand la
VM ou l'objet est déjà pris ; aucun secret (user-data, mot de passe VNC) ni
chemin d'état dans une réponse. Le relevé gardé du catalogue est oublié
d'un processus à l'autre (génération partagée dans le répertoire d'état)."""

import http.server
import json
import os
import re
import socketserver
import stat
import subprocess
import sys
import types
from pathlib import Path
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_kvm_routes_196 import (  # noqa: E402,F401 : fixtures partagées
    HOSTILE, ROOT, SRC, _NoThread, _RealThread, as_user, client, env, only_run, script_cmd, wapp)

ko, kh, ks, kn, ke, kc = wapp._ko, wapp._kh, wapp._ks, wapp._kn, wapp._ke, wapp._kc
DIGEST = "a" * 64
CONFIG = "c" * 64
UNREAD = "/var/lib/machines/moon/netperf.qcow2"
MARKER = "USERDATA-SECRET-MARKER-42"
VNC_PW = "Vn9pw7Qx"
CLI = str(ROOT / "bin" / "harvester-kvm.py")


def base(env, *sub):
    return [sys.executable, CLI, *sub, "--json", f"--state-dir={env['state']}"]


def spec_path(cmd):
    return next(a.split("=", 1)[1] for a in cmd if a.startswith("--spec-file="))


SPEC = {"name": "kvmtest-1", "vcpus": 2, "memory": {"current_mib": 512},
        "disks": [{"source": {"kind": "blank", "pool": "images", "size_bytes": 1 << 30}}],
        "nics": [{"kind": "network", "source": "kvmtest-net"}],
        "cloudinit": {"user_data": f"#cloud-config\npassword: {MARKER}\n", "seed_pool": "images"}}


@pytest.fixture
def lib(monkeypatch):
    """Bibliothèque simulée : appels notés, réponses sans secret (comme la
    vraie), rien ne touche un hôte."""
    calls = []

    def create_plan(host, spec, options=None, world=None):
        calls.append(("create-plan", host.name, spec.get("name")))
        return {"possible": True, "refusals": [], "warnings": [],
                "instances": [{"name": spec["name"], "volumes": [], "seed": None,
                               "xml_preview": "<graphics passwd='***'/>"}],
                "_instances": ["internal"], "_options": {"x": 1}}

    def edit_plan(host, name, spec, expect_digest=None, options=None, gesture=False, vm=None):
        calls.append(("edit-plan", host.name, name, expect_digest))
        if expect_digest and expect_digest != CONFIG:
            raise ko.OpError("changed", "the VM was changed since you opened it: reload")
        return {"name": name, "state": "running", "persistent": True, "config_digest": CONFIG,
                "changes": [{"field": "memory.current_mib", "old": 512, "new": 1024,
                             "applied": "now", "reason": None}], "refusals": [], "empty": False,
                "_vm": {"inactive": "x"}}

    def read_vm(host, name):
        calls.append(("read-vm", host.name, name))
        if name == "nosuch":
            raise kh.KvmError("notfound", f"{host.name}: notfound: no domain")
        return {"inactive": f"<domain><name>{name}</name><devices><graphics type='vnc' "
                            f"passwd='{VNC_PW}'/><disk device='cdrom'/></devices><metadata>"
                            "<hops:vm xmlns:hops='https://harvester-ops.io/xmlns/vm/1'>"
                            "<hops:seed pool='images' volume='web-cidata.iso'/></hops:vm>"
                            "</metadata></domain>",
                "live": None, "info": {"state": "shut off", "persistent": True}, "snapshots": []}

    def vm_form(host, name):
        calls.append(("form", host.name, name))
        return {"name": name, "view": {"display": {"vnc": True, "password": True}},
                "unmanaged": ["cputune"], "pending": ["memory.max_mib"], "restart_required": True,
                "hotness": [{"field": "vcpus", "running": "now"}], "config_digest": CONFIG,
                "state": "running", "persistent": True, "autostart": False, "snapshots": 0}

    def read_seed(host, pool, volume):
        calls.append(("seed", host.name, pool, volume))
        return {"user-data": f"#cloud-config\npassword: {MARKER}\n".encode(),
                "network-config": b"version: 1\n", "meta-data": b"{}"}

    def list_images(host, decl=None, notes=None, fresh=False):
        calls.append(("images", host.name, fresh))
        return {"images": [{"pool": "images", "volume": "cirros.qcow2", "format": "qcow2"}],
                "pools": ["images"], "missing_pools": [], "unknown": [], "survey_age_s": 3.0}

    def delete_volume_plan(host, pool, name, acknowledged_unreadable=None, survey_timeout=None):
        calls.append(("vol-plan", host.name, pool, name, acknowledged_unreadable))
        return {"pool": pool, "volume": name, "refusals": [], "unreadable": [UNREAD],
                "acknowledged_unreadable": acknowledged_unreadable or [], "digest": DIGEST}

    def delete_pool_plan(host, name, remove_dir=False, busy=(), acknowledged_unreadable=None,
                         **kw):
        calls.append(("pool-plan", host.name, name, remove_dir, acknowledged_unreadable))
        return {"name": name, "remove_dir": remove_dir, "volumes": [], "refusals": [],
                "unreadable": [], "digest": DIGEST}

    def delete_network_plan(host, name):
        calls.append(("net-plan", host.name, name))
        return {"name": name, "network": {}, "refusals": [], "digest": DIGEST}

    def define_pool(host, spec, step=None, dry_run=False):
        calls.append(("define-pool", host.name, spec.get("path"), dry_run))
        if spec.get("path", "").startswith("/etc"):
            raise ko.OpError("refused", "pool path /etc is or contains a system directory",
                             data={"refusals": ["pool path /etc is or contains a system directory"]})
        return {"name": spec["name"], "dry_run": True, "xml": "<pool/>", "existing_directory": False,
                "nested_with": []}

    def define_network(host, spec, step=None, dry_run=False):
        calls.append(("define-net", host.name, spec.get("subnet"), dry_run))
        if spec.get("subnet") == "172.16.200.0/24":
            raise ko.OpError("refused", "overlaps 172.16.0.0/16 (address of br0)")
        return {"name": spec["name"], "dry_run": True, "xml": "<network/>"}

    for mod, name, fn in ((kc, "create_plan", create_plan), (ke, "edit_plan", edit_plan),
                          (ke, "read_vm", read_vm), (ke, "vm_form", vm_form),
                          (ko, "read_seed", read_seed), (ks, "list_images", list_images),
                          (ks, "delete_volume_plan", delete_volume_plan),
                          (ks, "delete_pool_plan", delete_pool_plan),
                          (kn, "delete_network_plan", delete_network_plan),
                          (ks, "define_pool", define_pool), (kn, "define_network", define_network),
                          (ks, "list_pools", lambda h: [{"name": "images", "state": "running"}]),
                          (kn, "list_networks", lambda h: [{"name": "default"}]),
                          (kn, "host_links", lambda h: {"bridges": ["br0"], "physical": ["eno1"],
                                                        "links": [], "networks": []})):
        monkeypatch.setattr(mod, name, fn)
    # l'empreinte du XML inactif lu par la modification : celle de la fenêtre
    monkeypatch.setattr(wapp._kx, "canonical_digest", lambda xml: CONFIG)
    return calls


@pytest.fixture
def surveys():
    with wapp._KVM_SURVEYS_LOCK:
        wapp._KVM_SURVEYS.clear()
    yield
    with wapp._KVM_SURVEYS_LOCK:
        wapp._KVM_SURVEYS.clear()


def holder(action, holds=(), vms=(), status="running"):
    """Une action KVM en cours qui tient des objets (ou des noms de VM)."""
    r = wapp.ActionRun(action.replace(":", "-")[:12], action, "kvm:node2", ["x"])
    r.status = status
    r.kvm_holds = tuple(holds)
    r.kvm_vms = tuple(vms)
    wapp.ACTIONS[r.id] = r
    return r


# --- lectures ------------------------------------------------------------------------

def test_form_is_the_library_view_without_password_value(client, lib):
    r = client.get("/api/kvm/vms/node2/web/form")
    assert r.status_code == 200
    d = r.get_json()
    assert d["config_digest"] == CONFIG and d["view"]["display"]["password"] is True
    assert d["pending"] == ["memory.max_mib"] and d["hotness"][0]["field"] == "vcpus"
    assert VNC_PW not in r.get_data(as_text=True)
    assert wapp.ACTIONS == {} and _NoThread.started == []


def test_cloudinit_is_read_for_an_operator_only(as_user, lib):
    r = as_user("curieux", "GET", "/api/kvm/vms/node2/web/cloudinit")
    assert r.status_code == 403 and r.get_json()["required"] == "operator"
    assert not [c for c in lib if c[0] == "seed"]
    r = as_user("alice", "GET", "/api/kvm/vms/node2/web/cloudinit")
    assert r.status_code == 200
    d = r.get_json()
    assert MARKER in d["user_data"] and d["network_data"] == "version: 1\n"
    assert d["seed"] == {"pool": "images", "volume": "web-cidata.iso"}
    assert VNC_PW not in r.get_data(as_text=True)


def test_cloudinit_route_is_not_a_shared_read():
    assert re.search(r'@app\.route\("/api/kvm/vms/<host>/<domain>/cloudinit"\)\n'
                     r'@_rate_limit\(KVM_READ_RATE\)\n@requires_auth\n'
                     r'def api_kvm_vm_cloudinit', SRC)


def test_images_catalogue_reads_the_cached_survey_unless_fresh(client, lib, surveys):
    d = client.get("/api/kvm/hosts/node2/images").get_json()
    assert d["images"][0]["volume"] == "cirros.qcow2" and d["survey_age_s"] == 3.0
    client.get("/api/kvm/hosts/node2/images?fresh=1")
    assert [c for c in lib if c[0] == "images"] == [("images", "node2", False),
                                                    ("images", "node2", True)]


def test_images_catalogue_is_capped_per_host(client, lib, surveys):
    with wapp._KVM_SURVEYS_LOCK:
        wapp._KVM_SURVEYS["node2"] = wapp.KVM_SURVEYS_PER_HOST
    r = client.get("/api/kvm/hosts/node2/images")
    assert r.status_code == 429 and r.get_json()["kind"] == "busy"
    assert not [c for c in lib if c[0] == "images"]


@pytest.mark.parametrize("url,key", [("/api/kvm/hosts/node2/links", "bridges"),
                                     ("/api/kvm/hosts/node2/pools", "pools"),
                                     ("/api/kvm/hosts/node2/networks", "networks")])
def test_host_reads(client, lib, url, key):
    r = client.get(url)
    assert r.status_code == 200 and key in r.get_json() and r.get_json()["host"] == "node2"


def test_caps_are_cached_by_host_and_version(client, monkeypatch):
    seen, version = [], {"v": "Compiled against library: libvirt 12.8.0"}
    monkeypatch.setattr(kh.VirshHost, "virsh", lambda self, *a, **k: version["v"])

    def host_options(host):
        seen.append(host.name)
        return {"arch": "x86_64", "machines": [], "default_machine": "q35",
                "by_machine": {"q35": {"tpm": True}, "pc": {"tpm": True}},
                "disabled": {"q35": {}, "pc": {"secure_boot": "Secure Boot needs q35"}}}
    monkeypatch.setattr(kc, "host_options", host_options)
    monkeypatch.setattr(wapp, "READ_SHARE_ENABLED", False)
    wapp._KVM_CAPS.clear()
    d = client.get("/api/kvm/hosts/node2/caps?machine=pc").get_json()
    assert list(d["options"]["by_machine"]) == ["pc"] and d["options"]["disabled"]["pc"]
    client.get("/api/kvm/hosts/node2/caps")
    assert seen == ["node2"]                                     # gardé
    version["v"] = "Compiled against library: libvirt 12.9.0"    # mise à jour de l'hôte
    client.get("/api/kvm/hosts/node2/caps")
    assert seen == ["node2", "node2"]
    client.get("/api/kvm/hosts/node2/caps?fresh=1")              # relu sur demande
    assert seen == ["node2"] * 3
    assert client.get("/api/kvm/hosts/node2/caps?machine=arm").status_code == 400
    wapp._KVM_CAPS.clear()


def test_host_test_forgets_the_caps(client):
    wapp._KVM_CAPS[(("node2", "172.16.1.12", "qemu:///system"), "v")] = ({}, 0)
    client.post("/api/kvm/hosts/node2/test")
    assert not [k for k in wapp._KVM_CAPS if k[0][0] == "node2"]


@pytest.mark.parametrize("url,call", [
    (f"/api/kvm/hosts/node2/volumes/images/a.qcow2/delete-plan?acknowledge={quote(UNREAD, safe='')}",
     ("vol-plan", "node2", "images", "a.qcow2", [UNREAD])),
    ("/api/kvm/hosts/node2/pools/kvmtest-pool/delete-plan?remove_dir=1",
     ("pool-plan", "node2", "kvmtest-pool", True, None)),
    ("/api/kvm/hosts/node2/networks/kvmtest-net/delete-plan", ("net-plan", "node2", "kvmtest-net")),
])
def test_delete_previews_are_read_only_and_give_the_digest(client, lib, surveys, url, call):
    r = client.get(url)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["plan"]["digest"] == DIGEST and r.get_json()["possible"] is True
    assert lib == [call]
    assert wapp.ACTIONS == {} and _NoThread.started == []


def test_create_and_edit_previews_are_read_only(client, lib):
    r = client.post("/api/kvm/hosts/node2/vm-plan", json={"spec": SPEC})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d["possible"] is True and "_instances" not in d["plan"] and "_options" not in d["plan"]
    assert "passwd='***'" in d["plan"]["instances"][0]["xml_preview"]
    assert MARKER not in r.get_data(as_text=True)
    r = client.post("/api/kvm/vms/node2/web/edit-plan", json={"spec": {"memory": {"current_mib": 1024}}})
    assert r.status_code == 200 and r.get_json()["plan"]["config_digest"] == CONFIG
    assert "_vm" not in r.get_json()["plan"]
    r = client.post("/api/kvm/vms/node2/web/edit-plan", json={"spec": {}, "expect_config": "d" * 64})
    assert r.status_code == 409 and r.get_json()["kind"] == "changed"
    assert wapp.ACTIONS == {} and _NoThread.started == []


def test_previews_do_not_invalidate_shared_reads(client, lib, monkeypatch):
    seen = []
    monkeypatch.setattr(wapp.READ_SHARE, "invalidate", lambda scope: seen.append(scope))
    client.post("/api/kvm/hosts/node2/vm-plan", json={"spec": SPEC})
    client.post("/api/kvm/vms/node2/web/edit-plan", json={"spec": {}})
    assert seen == []


# --- mutations : ligne de commande exacte ------------------------------------------------

def run_route(client, method, path, body):
    return client.open(path, method=method, json=body if body is not None else {})


@pytest.mark.parametrize("method,path,body,expected", [
    ("POST", "/api/kvm/vms/node2/web/hot/cpumem", {"vcpus": 4, "memory_mib": 2048},
     ["vm", "cpumem", "--host=node2", "--name=web", "--vcpus=4", "--memory-mib=2048"]),
    ("POST", "/api/kvm/vms/node2/web/hot/remove-volume", {"target": "vdb"},
     ["vm", "remove-volume", "--host=node2", "--name=web", "--target=vdb"]),
    ("POST", "/api/kvm/vms/node2/web/hot/eject-image", {"target": "sda"},
     ["vm", "eject-image", "--host=node2", "--name=web", "--target=sda"]),
    ("POST", "/api/kvm/vms/node2/web/hot/insert-cdrom", {"target": "sda", "pool": "-iso", "volume": "-x.iso"},
     ["vm", "insert-cdrom", "--host=node2", "--name=web", "--target=sda", "--pool=-iso", "--volume=-x.iso"]),
    ("POST", "/api/kvm/vms/node2/web/hot/remove-nic", {"mac": "52:54:00:12:34:56"},
     ["vm", "remove-nic", "--host=node2", "--name=web", "--mac=52:54:00:12:34:56"]),
    ("POST", "/api/kvm/hosts/node2/images/images/cirros.qcow2/verify", {"sha256": "f" * 64},
     ["image", "verify", "--host=node2", "--pool=images", "--volume=cirros.qcow2", f"--sha256={'f' * 64}"]),
    ("DELETE", "/api/kvm/hosts/node2/images/images/cirros.qcow2", {"expect_delete": DIGEST},
     ["image", "delete", "--host=node2", "--pool=images", "--volume=cirros.qcow2", f"--expect-delete={DIGEST}"]),
    ("POST", "/api/kvm/hosts/node2/volumes",
     {"pool": "images", "name": "d.qcow2", "capacity": 1073741824, "backing": "cirros.qcow2"},
     ["vol", "create", "--host=node2", "--pool=images", "--name=d.qcow2", "--capacity=1073741824",
      "--format=qcow2", "--backing=cirros.qcow2"]),
    ("POST", "/api/kvm/hosts/node2/volumes", {"pool": "images", "name": "r.raw", "capacity": 4096,
                                              "format": "raw", "prealloc_metadata": True},
     ["vol", "create", "--host=node2", "--pool=images", "--name=r.raw", "--capacity=4096",
      "--format=raw", "--prealloc-metadata"]),
    ("POST", "/api/kvm/hosts/node2/volumes/images/d.qcow2/resize",
     {"capacity": 2147483648, "acknowledge_unreadable": [UNREAD]},
     ["vol", "resize", "--host=node2", "--pool=images", "--name=d.qcow2", "--capacity=2147483648",
      f"--acknowledge-unreadable={UNREAD}"]),
    ("DELETE", "/api/kvm/hosts/node2/volumes/images/d.qcow2",
     {"expect_delete": DIGEST, "acknowledge_unreadable": [UNREAD, UNREAD]},
     ["vol", "delete", "--host=node2", "--pool=images", "--name=d.qcow2",
      f"--acknowledge-unreadable={UNREAD}", f"--expect-delete={DIGEST}"]),
    ("POST", "/api/kvm/hosts/node2/pools/images/start", None, ["pool", "start", "--host=node2", "--pool=images"]),
    ("POST", "/api/kvm/hosts/node2/pools/images/stop", None, ["pool", "stop", "--host=node2", "--pool=images"]),
    ("POST", "/api/kvm/hosts/node2/pools/images/refresh", None, ["pool", "refresh", "--host=node2", "--pool=images"]),
    ("POST", "/api/kvm/hosts/node2/pools/images/autostart", {"enable": False},
     ["pool", "autostart", "--host=node2", "--pool=images", "--disable"]),
    ("POST", "/api/kvm/hosts/node2/pools/images/autostart", {"enable": True},
     ["pool", "autostart", "--host=node2", "--pool=images"]),
    ("DELETE", "/api/kvm/hosts/node2/pools/kvmtest-pool",
     {"expect_delete": DIGEST, "remove_dir": True},
     ["pool", "delete", "--host=node2", "--pool=kvmtest-pool", "--remove-dir", f"--expect-delete={DIGEST}"]),
    ("POST", "/api/kvm/hosts/node2/networks/kvmtest-net/start", None,
     ["net", "start", "--host=node2", "--network=kvmtest-net"]),
    ("POST", "/api/kvm/hosts/node2/networks/kvmtest-net/stop", None,
     ["net", "stop", "--host=node2", "--network=kvmtest-net"]),
    ("POST", "/api/kvm/hosts/node2/networks/kvmtest-net/autostart", {"enable": False},
     ["net", "autostart", "--host=node2", "--network=kvmtest-net", "--disable"]),
    ("DELETE", "/api/kvm/hosts/node2/networks/kvmtest-net", {"expect_delete": DIGEST},
     ["net", "delete", "--host=node2", "--network=kvmtest-net", f"--expect-delete={DIGEST}"]),
    ("PUT", "/api/kvm/hosts/node2/image-pools", {"pools": ["images", "-iso"]},
     ["host", "image-pools", "--host=node2", "--pool=images", "--pool=-iso"]),
    ("PUT", "/api/kvm/hosts/node2/image-pools", {"pools": []}, ["host", "image-pools", "--host=node2"]),
])
def test_mutation_argv(client, env, lib, method, path, body, expected):
    r = run_route(client, method, path, body)
    assert r.status_code == 202, r.get_json()
    cmd = script_cmd()
    sub, opts = expected[:2], expected[2:]
    assert cmd == base(env, *sub) + opts
    run = only_run()
    assert str(env["state"]) not in " ".join(run.cmd)          # commande publique sans chemin


SPEC_ROUTES = [
    ("POST", "/api/kvm/vms/node2", {"spec": SPEC}, ["vm", "create", "--host=node2"], SPEC),
    ("PATCH", "/api/kvm/vms/node2/web", {"spec": {"memory": {"current_mib": 1024}}, "expect_config": CONFIG},
     ["vm", "edit", "--host=node2", "--name=web"], {"memory": {"current_mib": 1024}}),
    ("POST", "/api/kvm/vms/node2/web/hot/add-volume",
     {"disk": {"source": {"kind": "blank", "pool": "images", "size_bytes": 1 << 20}}},
     ["vm", "add-volume", "--host=node2", "--name=web"],
     {"source": {"kind": "blank", "pool": "images", "size_bytes": 1 << 20}}),
    ("POST", "/api/kvm/vms/node2/web/hot/add-nic", {"nic": {"kind": "bridge", "source": "br0"}},
     ["vm", "add-nic", "--host=node2", "--name=web"], {"kind": "bridge", "source": "br0"}),
    ("POST", "/api/kvm/vms/node2/web/hot/cloudinit",
     {"user_data": f"#cloud-config\n{MARKER}", "hostname": "h1", "rerun": True},
     ["vm", "cloudinit", "--host=node2", "--name=web", "--rerun"],
     {"user_data": f"#cloud-config\n{MARKER}", "hostname": "h1"}),
    ("POST", "/api/kvm/vms/node2/web/hot/cloudinit", {"user_data": "", "clear": True},
     ["vm", "cloudinit", "--host=node2", "--name=web"], {"user_data": "", "clear": True}),
    ("POST", "/api/kvm/hosts/node2/pools", {"spec": {"name": "kvmtest-pool", "type": "dir",
                                                     "path": "/var/lib/libvirt/images/kvmtest"}},
     ["pool", "define", "--host=node2"], {"name": "kvmtest-pool", "type": "dir",
                                          "path": "/var/lib/libvirt/images/kvmtest"}),
    ("POST", "/api/kvm/hosts/node2/networks", {"spec": {"name": "kvmtest-net", "mode": "isolated",
                                                        "subnet": "10.213.77.0/24"}},
     ["net", "define", "--host=node2"], {"name": "kvmtest-net", "mode": "isolated",
                                         "subnet": "10.213.77.0/24"}),
    ("POST", "/api/kvm/templates", {"name": "small", "spec": {"vcpus": 1}},
     ["template", "save", "--name=small"], {"vcpus": 1}),
]


@pytest.mark.parametrize("method,path,body,expected,spec", SPEC_ROUTES,
                         ids=[r[3][0] + "-" + r[3][1] for r in SPEC_ROUTES])
def test_spec_routes_pass_a_private_file_removed_after_the_action(client, env, lib, monkeypatch,
                                                                  method, path, body, expected, spec):
    r = client.open(path, method=method, json=body)
    assert r.status_code == 202, r.get_json()
    cmd = script_cmd()
    sub, opts = expected[:2], expected[2:]
    f = spec_path(cmd)
    tail = [f"--spec-file={f}"] + ([f"--expect-config={CONFIG}"] if sub == ["vm", "edit"] else [])
    rest = [a for a in cmd[len(base(env, *sub)):] if not a.startswith("--spec-file=")
            and not a.startswith("--expect-config=")]
    assert cmd[:len(base(env, *sub))] == base(env, *sub)
    assert sorted(rest) == sorted(opts) and all(t in cmd for t in tail)
    assert Path(f).parent == env["work"]
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert json.loads(Path(f).read_text()) == spec
    # jamais de spec, de user-data ni de chemin dans la réponse ou la commande publique
    run = only_run()
    shown = r.get_data(as_text=True) + json.dumps(run.to_dict())
    assert MARKER not in shown and str(env["state"]) not in shown
    # fin de l'action (réussite, échec ou annulation) : le fichier est supprimé
    monkeypatch.setattr(wapp, "_vm_transfer_runner", lambda run, cmd, capture=False: None)
    _NoThread.started[-1].target()
    assert not Path(f).exists()


@pytest.mark.parametrize("outcome", ["error", "cancelled", "raise"])
def test_spec_file_is_removed_after_a_failed_or_cancelled_action(client, env, lib, monkeypatch, outcome):
    assert client.post("/api/kvm/vms/node2", json={"spec": SPEC}).status_code == 202
    f = spec_path(script_cmd())

    def runner(run, cmd, capture=False):
        if outcome == "raise":
            raise RuntimeError("boom")
        run.status = outcome
    monkeypatch.setattr(wapp, "_vm_transfer_runner", runner)
    try:
        _NoThread.started[-1].target()
    except RuntimeError:
        pass
    assert not Path(f).exists()


def test_create_holds_every_instance_name_and_its_pools(client, lib):
    spec = dict(SPEC, instances=3)
    r = client.post("/api/kvm/vms/node2", json={"spec": spec})
    assert r.status_code == 202 and r.get_json()["names"] == ["kvmtest-1-01", "kvmtest-1-02", "kvmtest-1-03"]
    run = only_run()
    assert run.action == "kvm:vm-create:node2/kvmtest-1-01"
    assert run.kvm_vms == ("kvmtest-1-01", "kvmtest-1-02", "kvmtest-1-03")
    assert set(run.kvm_holds) == {("node2", "pool", "images", "s"), ("node2", "network", "kvmtest-net", "s")}
    # une seconde opération sur l'une des instances : 409, rien lancé
    r = client.post("/api/kvm/vms/node2/kvmtest-1-02/start")
    assert r.status_code == 409 and r.get_json()["running_action"] == "kvm:vm-create:node2/kvmtest-1-01"
    assert len(_NoThread.started) == 1


def test_create_into_a_name_held_by_a_clone_is_busy(client, lib, env):
    holder("kvm:vm-clone:node2/src", vms=("src", "kvmtest-1"))
    r = client.post("/api/kvm/vms/node2", json={"spec": SPEC})
    assert r.status_code == 409 and r.get_json()["running_action"] == "kvm:vm-clone:node2/src"
    assert _NoThread.started == [] and list(env["work"].iterdir()) == []      # spec retiré


@pytest.mark.parametrize("held,method,path,body", [
    (("node2", "pool", "images", "s"), "DELETE", "/api/kvm/hosts/node2/pools/images", {"expect_delete": DIGEST}),
    (("node2", "pool", "images", "s"), "POST", "/api/kvm/hosts/node2/pools/images/stop", None),
    (("node2", "pool", "images", "s"), "POST", "/api/kvm/hosts/node2/pools/images/autostart", None),
    (("node2", "volume", "images/d.qcow2", "x"), "DELETE", "/api/kvm/hosts/node2/volumes/images/d.qcow2",
     {"expect_delete": DIGEST}),
    (("node2", "volume", "images/d.qcow2", "x"), "POST", "/api/kvm/hosts/node2/volumes/images/d.qcow2/resize",
     {"capacity": 1 << 30}),
    (("node2", "network", "kvmtest-net", "s"), "DELETE", "/api/kvm/hosts/node2/networks/kvmtest-net",
     {"expect_delete": DIGEST}),
    (("node2", "network", "kvmtest-net", "s"), "POST", "/api/kvm/hosts/node2/networks/kvmtest-net/stop", None),
    (("node2", "pool", "images", "x"), "POST", "/api/kvm/vms/node2", {"spec": SPEC}),
    (("node2", "pool", "images", "x"), "POST", "/api/kvm/vms/node2/web/hot/add-volume",
     {"disk": {"source": {"kind": "blank", "pool": "images", "size_bytes": 1}}}),
])
def test_objects_held_by_a_running_action_are_busy(client, lib, held, method, path, body):
    holder("kvm:vm-create:node2/other", holds=[held])
    r = run_route(client, method, path, body)
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["running_action"] == "kvm:vm-create:node2/other"
    assert _NoThread.started == []


@pytest.mark.parametrize("held,method,path", [
    (("node2", "pool", "images", "s"), "POST", "/api/kvm/hosts/node2/pools/images/start"),
    (("node2", "pool", "images", "s"), "POST", "/api/kvm/hosts/node2/pools/images/refresh"),
    (("node2", "network", "kvmtest-net", "s"), "POST", "/api/kvm/hosts/node2/networks/kvmtest-net/start"),
    (("ghost", "pool", "images", "x"), "POST", "/api/kvm/hosts/node2/pools/images/stop"),    # autre hôte
])
def test_shared_holds_and_other_hosts_pass(client, lib, held, method, path):
    holder("kvm:vm-create:node2/other", holds=[held])
    assert client.post(path, json={}).status_code == 202


def test_a_finished_action_holds_nothing(client, lib):
    holder("kvm:pool-delete:node2/images", holds=[("node2", "pool", "images", "x")], status="done")
    assert client.post("/api/kvm/hosts/node2/pools/images/stop", json={}).status_code == 202


def test_object_labels(client, lib):
    client.post("/api/kvm/hosts/node2/pools/iso/stop", json={})
    client.delete("/api/kvm/hosts/node2/volumes/images/d.qcow2", json={"expect_delete": DIGEST})
    client.post("/api/kvm/vms/node2/web/hot/cpumem", json={"vcpus": 2})
    assert sorted(r.action for r in wapp.ACTIONS.values()) == [
        "kvm:pool-stop:node2/iso", "kvm:vm-cpumem:node2/web", "kvm:vol-delete:node2/images/d.qcow2"]


# --- empreintes obligatoires ---------------------------------------------------------------

DELETE_ROUTES = {
    "/api/kvm/vms/<host>/<domain>": ("/api/kvm/vms/node2/web", {"confirm": "web"}),
    "/api/kvm/hosts/<host>/volumes/<pool>/<volume>": ("/api/kvm/hosts/node2/volumes/images/d.qcow2", {}),
    "/api/kvm/hosts/<host>/images/<pool>/<volume>": ("/api/kvm/hosts/node2/images/images/c.qcow2", {}),
    "/api/kvm/hosts/<host>/pools/<pool>": ("/api/kvm/hosts/node2/pools/kvmtest-pool", {"remove_dir": True}),
    "/api/kvm/hosts/<host>/networks/<net>": ("/api/kvm/hosts/node2/networks/kvmtest-net", {}),
}
# DELETE sans aperçu, par nature : retrait d'un hôte (déclaration seulement),
# instantané (re-parenté, pas de stockage partagé), modèle de la console
NOT_PREVIEWED = {"/api/kvm/hosts/<host>", "/api/kvm/vms/<host>/<domain>/snapshots/<snapshot>",
                 "/api/kvm/templates/<template>"}


def test_every_kvm_delete_route_is_classified():
    found = {str(r) for r in wapp.app.url_map.iter_rules()
             if str(r).startswith("/api/kvm/") and "DELETE" in r.methods}
    assert found == set(DELETE_ROUTES) | NOT_PREVIEWED


@pytest.mark.parametrize("rule", sorted(DELETE_ROUTES))
@pytest.mark.parametrize("digest", [None, "", "ABC", "g" * 64, 12, ["a" * 64]])
def test_no_delete_route_spawns_the_cli_without_the_preview_digest(client, lib, rule, digest):
    path, body = DELETE_ROUTES[rule]
    body = dict(body)
    if digest is not None:
        body["expect_delete"] = digest
    r = client.delete(path, json=body)
    assert r.status_code == 400, (rule, r.get_json())
    assert "expect_delete" in r.get_json()["error"] or "digest" in r.get_json()["error"]
    assert _NoThread.started == [] and wapp.ACTIONS == {}


@pytest.mark.parametrize("rule", sorted(DELETE_ROUTES))
def test_every_delete_route_passes_the_digest_to_the_cli(client, lib, rule):
    path, body = DELETE_ROUTES[rule]
    r = client.delete(path, json=dict(body, expect_delete=DIGEST))
    assert r.status_code == 202, r.get_json()
    cmd = script_cmd()
    assert cmd[-1] == f"--expect-delete={DIGEST}" and cmd.count(f"--expect-delete={DIGEST}") == 1


def test_edit_needs_the_config_digest_and_refuses_a_stale_one(client, lib, monkeypatch):
    r = client.patch("/api/kvm/vms/node2/web", json={"spec": {"vcpus": 2}})
    assert r.status_code == 400 and "expect_config" in r.get_json()["error"]
    monkeypatch.setattr(wapp._kx, "canonical_digest", lambda xml: "e" * 64)
    r = client.patch("/api/kvm/vms/node2/web", json={"spec": {"vcpus": 2}, "expect_config": CONFIG})
    assert r.status_code == 409 and r.get_json()["kind"] == "changed"
    assert _NoThread.started == []
    monkeypatch.setattr(wapp._kx, "canonical_digest", lambda xml: CONFIG)
    r = client.patch("/api/kvm/vms/node2/web", json={"spec": {"vcpus": 2}, "expect_config": CONFIG})
    assert r.status_code == 202
    assert f"--expect-config={CONFIG}" in script_cmd()


def test_edit_dry_run_is_the_preview(client, lib):
    r = client.patch("/api/kvm/vms/node2/web", json={"spec": {"vcpus": 2}, "dry_run": True})
    assert r.status_code == 200 and r.get_json()["dry_run"] is True and wapp.ACTIONS == {}


def test_edit_rename_holds_the_new_name(client, lib, monkeypatch):
    monkeypatch.setattr(wapp._kx, "canonical_digest", lambda xml: CONFIG)
    holder("kvm:vm-clone:node2/src", vms=("src", "web2"))
    r = client.patch("/api/kvm/vms/node2/web", json={"spec": {"name": "web2"}, "expect_config": CONFIG})
    assert r.status_code == 409


# --- refus avant toute action ----------------------------------------------------------------

@pytest.mark.parametrize("method,path,body", [
    ("POST", "/api/kvm/vms/node2", {"spec": "x"}),
    ("POST", "/api/kvm/vms/node2", {"spec": dict(SPEC, name="a/b")}),
    ("POST", "/api/kvm/vms/node2", {"spec": dict(SPEC, instances=99)}),
    ("POST", "/api/kvm/vms/node2", {"spec": SPEC, "dry_run": "yes"}),
    ("PATCH", "/api/kvm/vms/node2/web", {"spec": {}, "expect_config": "short"}),
    ("PATCH", "/api/kvm/vms/node2/web", {"spec": {"name": ".hidden"}, "expect_config": CONFIG}),
    ("POST", "/api/kvm/vms/node2/web/hot/cpumem", {}),
    ("POST", "/api/kvm/vms/node2/web/hot/cpumem", {"vcpus": 0}),
    ("POST", "/api/kvm/vms/node2/web/hot/cpumem", {"vcpus": True}),
    ("POST", "/api/kvm/vms/node2/web/hot/remove-volume", {"target": "vd b"}),
    ("POST", "/api/kvm/vms/node2/web/hot/remove-volume", {"target": "--json"}),
    ("POST", "/api/kvm/vms/node2/web/hot/insert-cdrom", {"target": "sda", "pool": "p\nq", "volume": "v"}),
    ("POST", "/api/kvm/vms/node2/web/hot/remove-nic", {"mac": "52:54:00"}),
    ("POST", "/api/kvm/vms/node2/web/hot/add-volume", {"disk": []}),
    ("POST", "/api/kvm/vms/node2/web/hot/cloudinit", {"user_data": 3}),
    ("POST", "/api/kvm/vms/node2/web/hot/cloudinit", {"rerun": "1"}),
    ("POST", "/api/kvm/vms/node2/web/hot/cloudinit", {"user_data": "", "clear": "yes"}),
    ("POST", "/api/kvm/vms/node2/web/hot/cloudinit", {"user_data": "#cloud-config\n", "clear": True}),
    ("POST", "/api/kvm/vms/node2/web/hot/cloudinit", {"acknowledge_unreadable": ["rel/path"]}),
    ("POST", "/api/kvm/hosts/node2/volumes", {"pool": "images", "name": "../x", "capacity": 1}),
    ("POST", "/api/kvm/hosts/node2/volumes", {"pool": "images", "name": "x", "capacity": -1}),
    ("POST", "/api/kvm/hosts/node2/volumes", {"pool": "images", "name": "x", "capacity": 1, "format": "vmdk"}),
    ("POST", "/api/kvm/hosts/node2/volumes/images/x/resize", {"capacity": "1G"}),
    ("POST", "/api/kvm/hosts/node2/pools", {"spec": {"name": "bad name", "path": "/x"}}),
    ("POST", "/api/kvm/hosts/node2/networks", {"spec": {"name": "-net", "mode": "nat"}}),
    ("POST", "/api/kvm/hosts/node2/pools/images/autostart", {"enable": "no"}),
    ("DELETE", "/api/kvm/hosts/node2/pools/images", {"expect_delete": DIGEST, "remove_dir": "yes"}),
    ("PUT", "/api/kvm/hosts/node2/image-pools", {"pools": "images"}),
    ("PUT", "/api/kvm/hosts/node2/image-pools", {"pools": ["a", "a"]}),
    ("POST", "/api/kvm/templates", {"name": "../x", "spec": {}}),
    ("POST", "/api/kvm/templates", {"name": "t", "spec": []}),
    ("POST", "/api/kvm/hosts/node2/images/images/c.qcow2/verify", {"sha256": "xyz"}),
])
def test_bad_requests_are_refused_before_any_action(client, env, lib, method, path, body):
    r = client.open(path, method=method, json=body)
    assert r.status_code == 400, r.get_json()
    assert _NoThread.started == [] and wapp.ACTIONS == {} and list(env["work"].iterdir()) == []


@pytest.mark.parametrize("path", ["/api/kvm/vms/node2", "/api/kvm/hosts/node2/vm-plan",
                                  "/api/kvm/vms/node2/web/edit-plan"])
def test_a_spec_body_over_one_mebibyte_is_refused_before_reading(client, path):
    r = client.post(path, data=b"{" + b" " * (wapp.KVM_SPEC_MAX_BODY + 1) + b"}",
                    content_type="application/json")
    assert r.status_code == 413 and wapp.ACTIONS == {}


def test_unknown_hot_gesture_and_verbs(client, lib):
    assert client.post("/api/kvm/vms/node2/web/hot/format-disk", json={}).status_code == 404
    assert client.post("/api/kvm/hosts/node2/pools/images/build", json={}).status_code == 404
    assert client.post("/api/kvm/hosts/node2/networks/n/update", json={}).status_code == 404
    assert wapp.ACTIONS == {}


def test_undeclared_host_is_404(client, lib):
    assert client.post("/api/kvm/vms/nowhere", json={"spec": SPEC}).status_code == 404
    assert client.post("/api/kvm/hosts/nowhere/pools/images/start", json={}).status_code == 404
    assert client.get("/api/kvm/hosts/nowhere/images").status_code == 404


@pytest.mark.parametrize("name", HOSTILE)
def test_hostile_names_stay_one_argument(client, env, lib, name):
    q = quote(name, safe="")
    assert client.post(f"/api/kvm/vms/node2/{q}/hot/cpumem", json={"vcpus": 2}).status_code == 202
    assert f"--name={name}" in script_cmd()
    assert client.post(f"/api/kvm/hosts/node2/pools/{q}/refresh", json={}).status_code == 202
    assert f"--pool={name}" in script_cmd()
    assert client.delete(f"/api/kvm/hosts/node2/networks/{q}", json={"expect_delete": DIGEST}).status_code == 202
    assert f"--network={name}" in script_cmd()
    assert client.delete(f"/api/kvm/hosts/node2/volumes/{q}/{q}", json={"expect_delete": DIGEST}).status_code == 202
    cmd = script_cmd()
    assert f"--pool={name}" in cmd and f"--name={name}" in cmd
    labels = [r.action for r in wapp.ACTIONS.values()]
    assert f"kvm:vm-cpumem:node2/{name}" in labels and f"kvm:vol-delete:node2/{name}/{name}" in labels


@pytest.mark.parametrize("name", ["a\nb", "x\x7f", "z" * 256])
def test_names_with_control_characters_are_refused(client, lib, name):
    q = quote(name, safe="")
    assert client.post(f"/api/kvm/vms/node2/{q}/hot/cpumem", json={"vcpus": 2}).status_code == 400
    assert client.post(f"/api/kvm/hosts/node2/pools/{q}/start", json={}).status_code == 400
    assert wapp.ACTIONS == {}


def test_pool_and_network_dry_runs_are_previews(client, lib):
    r = client.post("/api/kvm/hosts/node2/pools", json={"spec": {"name": "p1", "path": "/etc/x"},
                                                        "dry_run": True})
    assert r.status_code == 200 and r.get_json()["possible"] is False
    assert "system directory" in r.get_json()["refusals"][0]
    r = client.post("/api/kvm/hosts/node2/networks", json={"spec": {"name": "n1", "mode": "nat",
                                                                    "subnet": "172.16.200.0/24"},
                                                           "dry_run": True})
    assert r.status_code == 200 and r.get_json()["possible"] is False
    r = client.post("/api/kvm/hosts/node2/networks", json={"spec": {"name": "n1", "mode": "nat",
                                                                    "subnet": "10.1.2.0/24"},
                                                           "dry_run": True})
    assert r.status_code == 200 and r.get_json()["possible"] is True
    assert wapp.ACTIONS == {} and _NoThread.started == []


def test_template_reads_and_delete(client, env, lib):
    wapp._ktpl.save(env["state"], "small", {"vcpus": 1, "cloudinit": {"user_data": MARKER}})
    d = client.get("/api/kvm/templates").get_json()
    assert d["templates"][0]["name"] == "small" and d["templates"][0]["removed"] == ["cloudinit.user_data"]
    t = client.get("/api/kvm/templates/small")
    assert t.status_code == 200 and MARKER not in t.get_data(as_text=True)
    assert client.get("/api/kvm/templates/nosuch").status_code == 404
    assert client.delete("/api/kvm/templates/nosuch").status_code == 404
    r = client.delete("/api/kvm/templates/small")
    assert r.status_code == 202 and script_cmd() == base(env, "template", "delete") + ["--name=small"]


# --- rôles et limites -------------------------------------------------------------------------

ROLE_TABLE = [
    ("GET", "/api/kvm/hosts/node2/caps", "viewer"),
    ("GET", "/api/kvm/hosts/node2/images", "viewer"),
    ("GET", "/api/kvm/hosts/node2/links", "viewer"),
    ("GET", "/api/kvm/hosts/node2/pools", "viewer"),
    ("GET", "/api/kvm/hosts/node2/networks", "viewer"),
    ("GET", "/api/kvm/vms/node2/web/form", "viewer"),
    ("GET", "/api/kvm/vms/node2/web/cloudinit", "operator"),
    ("GET", "/api/kvm/vms/node2/cloudinit", "viewer"),                 # VM nommée « cloudinit »
    ("GET", "/api/kvm/hosts/node2/pools/p/delete-plan", "viewer"),
    ("GET", "/api/kvm/templates", "viewer"),
    ("POST", "/api/kvm/hosts/node2/vm-plan", "operator"),
    ("POST", "/api/kvm/vms/node2/web/edit-plan", "operator"),
    ("POST", "/api/kvm/vms/node2", "operator"),
    ("PATCH", "/api/kvm/vms/node2/web", "operator"),
    ("POST", "/api/kvm/vms/node2/web/hot/cpumem", "operator"),
    ("POST", "/api/kvm/vms/node2/web/hot/cloudinit", "operator"),
    ("POST", "/api/kvm/hosts/node2/images/upload", "operator"),
    ("POST", "/api/kvm/hosts/node2/images/fetch", "operator"),
    ("POST", "/api/kvm/uploads/0123456789abcdef0123456789abcdef/cancel", "operator"),
    ("POST", "/api/kvm/hosts/node2/images/images/c.qcow2/verify", "operator"),
    ("DELETE", "/api/kvm/hosts/node2/images/images/c.qcow2", "admin"),
    ("POST", "/api/kvm/hosts/node2/volumes", "operator"),
    ("POST", "/api/kvm/hosts/node2/volumes/images/d/resize", "operator"),
    ("DELETE", "/api/kvm/hosts/node2/volumes/images/d", "admin"),
    ("POST", "/api/kvm/hosts/node2/pools", "admin"),
    ("POST", "/api/kvm/hosts/node2/pools/images/start", "admin"),
    ("POST", "/api/kvm/hosts/node2/pools/images/stop", "admin"),
    ("POST", "/api/kvm/hosts/node2/pools/images/autostart", "admin"),
    ("POST", "/api/kvm/hosts/node2/pools/images/refresh", "operator"),
    ("POST", "/api/kvm/hosts/node2/pools/refresh/stop", "admin"),      # pool nommé « refresh »
    ("DELETE", "/api/kvm/hosts/node2/pools/images", "admin"),
    ("POST", "/api/kvm/hosts/node2/networks", "admin"),
    ("POST", "/api/kvm/hosts/node2/networks/n/start", "admin"),
    ("POST", "/api/kvm/hosts/node2/networks/n/stop", "admin"),
    ("POST", "/api/kvm/hosts/node2/networks/n/autostart", "admin"),
    ("DELETE", "/api/kvm/hosts/node2/networks/n", "admin"),
    ("PUT", "/api/kvm/hosts/node2/image-pools", "admin"),
    ("POST", "/api/kvm/templates", "operator"),
    ("DELETE", "/api/kvm/templates/t", "operator"),
    # inchangés (1.96, 1.98)
    ("POST", "/api/kvm/hosts", "admin"),
    ("POST", "/api/kvm/hosts/node2/test", "operator"),
    ("DELETE", "/api/kvm/hosts/node2", "admin"),
    ("DELETE", "/api/kvm/vms/node2/web", "admin"),
    ("POST", "/api/kvm/vms/node2/web/destroy", "operator"),
]


@pytest.mark.parametrize("method,path,role", ROLE_TABLE)
def test_required_role_table(method, path, role):
    assert wapp.required_role_for(path, method) == role


@pytest.mark.parametrize("user,method,path,body,allowed", [
    ("curieux", "POST", "/api/kvm/vms/node2", {"spec": SPEC}, False),
    ("curieux", "POST", "/api/kvm/vms/node2/web/hot/cpumem", {"vcpus": 2}, False),
    ("curieux", "POST", "/api/kvm/hosts/node2/volumes", {"pool": "images", "name": "x", "capacity": 1}, False),
    ("curieux", "GET", "/api/kvm/vms/node2/web/form", None, True),
    ("curieux", "POST", "/api/kvm/hosts/node2/vm-plan", {"spec": SPEC}, False),
    ("alice", "POST", "/api/kvm/vms/node2", {"spec": SPEC}, True),
    ("alice", "POST", "/api/kvm/vms/node2/web/hot/cpumem", {"vcpus": 2}, True),
    ("alice", "POST", "/api/kvm/hosts/node2/pools/images/refresh", {}, True),
    ("alice", "POST", "/api/kvm/hosts/node2/pools/images/stop", {}, False),
    ("alice", "POST", "/api/kvm/hosts/node2/pools", {"spec": {"name": "p", "path": "/srv/p"}}, False),
    ("alice", "DELETE", "/api/kvm/hosts/node2/pools/p", {"expect_delete": DIGEST}, False),
    ("alice", "POST", "/api/kvm/hosts/node2/networks", {"spec": {"name": "n", "mode": "nat"}}, False),
    ("alice", "POST", "/api/kvm/hosts/node2/networks/n/stop", {}, False),
    ("alice", "DELETE", "/api/kvm/hosts/node2/networks/n", {"expect_delete": DIGEST}, False),
    ("alice", "DELETE", "/api/kvm/hosts/node2/volumes/images/d", {"expect_delete": DIGEST}, False),
    ("alice", "DELETE", "/api/kvm/hosts/node2/images/images/d", {"expect_delete": DIGEST}, False),
    ("alice", "PUT", "/api/kvm/hosts/node2/image-pools", {"pools": []}, False),
    ("patronne", "POST", "/api/kvm/hosts/node2/pools/images/stop", {}, True),
    ("patronne", "DELETE", "/api/kvm/hosts/node2/volumes/images/d", {"expect_delete": DIGEST}, True),
    ("patronne", "PUT", "/api/kvm/hosts/node2/image-pools", {"pools": ["images"]}, True),
])
def test_roles_are_enforced(as_user, lib, user, method, path, body, allowed):
    kw = {"json": body} if body is not None else {}
    r = as_user(user, method, path, **kw)
    if allowed:
        assert r.status_code in (200, 202), r.get_json()
    else:
        assert r.status_code == 403 and r.get_json()["required"] in ("operator", "admin")
        assert _NoThread.started == []


def test_cancel_needs_the_role_that_started_the_action(client, lib):
    client.post("/api/kvm/hosts/node2/pools/images/stop", json={})
    client.post("/api/kvm/hosts/node2/pools/iso/refresh", json={})
    roles = sorted((r.action, r.required_role) for r in wapp.ACTIONS.values())
    assert roles == [("kvm:pool-refresh:node2/iso", "operator"), ("kvm:pool-stop:node2/images", "admin")]


def test_refresh_waits_for_a_pool_being_stopped(client, lib):
    client.post("/api/kvm/hosts/node2/pools/images/stop", json={})
    assert client.post("/api/kvm/hosts/node2/pools/images/refresh", json={}).status_code == 409


MUTATIONS = [
    ('"/api/kvm/vms/<host>", methods=["POST"]', "10/minute"),
    ('"/api/kvm/vms/<host>/<domain>", methods=["PATCH"]', "10/minute"),
    ('"/api/kvm/vms/<host>/<domain>/hot/<op>", methods=["POST"]', "20/minute"),
    ('"/api/kvm/hosts/<host>/images/upload", methods=["POST"]', "10/minute"),
    ('"/api/kvm/hosts/<host>/images/fetch", methods=["POST"]', "10/minute"),
    ('"/api/kvm/uploads/<token>/cancel", methods=["POST"]', "30/minute"),
    ('"/api/kvm/hosts/<host>/images/<pool>/<volume>/verify", methods=["POST"]', "5/minute"),
    ('"/api/kvm/hosts/<host>/images/<pool>/<volume>", methods=["DELETE"]', "10/minute"),
    ('"/api/kvm/hosts/<host>/volumes", methods=["POST"]', "10/minute"),
    ('"/api/kvm/hosts/<host>/volumes/<pool>/<volume>/resize", methods=["POST"]', "10/minute"),
    ('"/api/kvm/hosts/<host>/volumes/<pool>/<volume>", methods=["DELETE"]', "10/minute"),
    ('"/api/kvm/hosts/<host>/pools", methods=["POST"]', "10/minute"),
    ('"/api/kvm/hosts/<host>/pools/<pool>/<verb>", methods=["POST"]', "20/minute"),
    ('"/api/kvm/hosts/<host>/pools/<pool>", methods=["DELETE"]', "10/minute"),
    ('"/api/kvm/hosts/<host>/networks", methods=["POST"]', "10/minute"),
    ('"/api/kvm/hosts/<host>/networks/<net>/<verb>", methods=["POST"]', "20/minute"),
    ('"/api/kvm/hosts/<host>/networks/<net>", methods=["DELETE"]', "10/minute"),
    ('"/api/kvm/hosts/<host>/image-pools", methods=["PUT"]', "10/minute"),
    ('"/api/kvm/templates", methods=["POST"]', "20/minute"),
    ('"/api/kvm/templates/<template>", methods=["DELETE"]', "20/minute"),
]


@pytest.mark.parametrize("route,limit", MUTATIONS)
def test_every_mutation_is_authenticated_and_rate_limited(route, limit):
    m = re.search(r'@app\.route\(' + re.escape(route) + r'\)\n@requires_auth\n@_rate_limit\("([^"]+)"\)', SRC)
    assert m and m.group(1) == limit, route
    wapp._validate_rate_spec(m.group(1))


@pytest.mark.parametrize("route,limit,keys", [
    ('"/api/kvm/hosts/<host>/caps"', "KVM_READ_RATE", '("machine",)'),
    ('"/api/kvm/hosts/<host>/images"', "KVM_PREVIEW_RATE", "()"),
    ('"/api/kvm/hosts/<host>/links"', "KVM_READ_RATE", "()"),
    ('"/api/kvm/hosts/<host>/pools"', "KVM_READ_RATE", "()"),
    ('"/api/kvm/hosts/<host>/networks"', "KVM_READ_RATE", "()"),
    ('"/api/kvm/vms/<host>/<domain>/form"', "KVM_READ_RATE", "()"),
    ('"/api/kvm/hosts/<host>/volumes/<pool>/<volume>/delete-plan"', "KVM_PREVIEW_RATE", None),
    ('"/api/kvm/hosts/<host>/pools/<pool>/delete-plan"', "KVM_PREVIEW_RATE", None),
    ('"/api/kvm/hosts/<host>/networks/<net>/delete-plan"', "KVM_PREVIEW_RATE", None),
    ('"/api/kvm/templates"', None, None),
    ('"/api/kvm/templates/<template>"', None, None),
])
def test_every_read_is_authenticated_and_shared(route, limit, keys):
    rate = rf"@_rate_limit\({limit}\)\n" if limit else ""
    qk = f", query_keys={re.escape(keys)}" if keys is not None else ""
    assert re.search(r'@app\.route\(' + re.escape(route) + r'\)\n' + rate + r'@requires_auth\n'
                     + r'@shared_read\(scope=KVM_SCOPE, fresh_min=KVM_FRESH_MIN' + qk + r'\)\n', SRC), route
    if limit:
        wapp._validate_rate_spec(getattr(wapp, limit))


@pytest.mark.parametrize("route", ['"/api/kvm/hosts/<host>/vm-plan", methods=["POST"]',
                                   '"/api/kvm/vms/<host>/<domain>/edit-plan", methods=["POST"]'])
def test_posted_previews_are_rate_limited(route):
    assert re.search(r'@app\.route\(' + re.escape(route) + r'\)\n@_rate_limit\(KVM_PREVIEW_RATE\)\n'
                     r'@requires_auth\n', SRC), route


def test_routing_static_segments_win():
    adapter = wapp.app.url_map.bind("localhost")
    for path, method, endpoint in [
        ("/api/kvm/vms/node2/web/hot/cpumem", "POST", "api_kvm_vm_hot"),
        ("/api/kvm/vms/node2/web/edit-plan", "POST", "api_kvm_edit_plan"),
        ("/api/kvm/vms/node2/web/start", "POST", "api_kvm_vm_gesture"),
        ("/api/kvm/vms/node2/web/form", "GET", "api_kvm_vm_form"),
        ("/api/kvm/vms/node2/web", "PATCH", "api_kvm_vm_edit"),
        ("/api/kvm/vms/node2", "POST", "api_kvm_vm_create"),
        ("/api/kvm/hosts/node2/images/upload", "POST", "api_kvm_image_upload"),
        ("/api/kvm/hosts/node2/images/fetch", "POST", "api_kvm_image_fetch"),
        ("/api/kvm/hosts/node2/pools/p/delete-plan", "GET", "api_kvm_pool_delete_plan"),
        ("/api/kvm/hosts/node2/pools/p/stop", "POST", "api_kvm_pool_verb"),
        ("/api/kvm/hosts/node2/test", "POST", "api_kvm_host_test"),
    ]:
        assert adapter.match(path, method=method)[0] == endpoint, path


# --- téléversement en flux --------------------------------------------------------------------

from test_qcow2 import make_header  # noqa: E402

QCOW2_BACKING = make_header(backing=b"/etc/shadow")


class FakePopen:
    """Le script lancé par l'envoi d'image : note la commande, vérifie que le
    fichier reçu existe (0600) à ce moment, rend l'objet JSON prévu."""
    calls, seen, plan = [], [], {}

    def __init__(self, cmd, stdout=None, stderr=None, text=None):
        FakePopen.calls.append(cmd)
        host = next(a.split("=", 1)[1] for a in cmd if a.startswith("--host="))
        f = next(a.split("=", 1)[1] for a in cmd if a.startswith("--file="))
        FakePopen.seen.append((f, os.path.exists(f) and stat.S_IMODE(os.stat(f).st_mode)))
        self.rc, obj, lines = FakePopen.plan.get(host, (0, {"ok": True, "host": host, "verified": True}, []))
        stdout.write(json.dumps(obj).encode())
        self.stderr = iter([ln + "\n" for ln in lines + [f"STEP_EVENT|upload|done|sent to {host}"]])

    def wait(self):
        return self.rc

    def poll(self):
        return self.rc


@pytest.fixture
def upload(env, monkeypatch):
    FakePopen.calls, FakePopen.seen, FakePopen.plan = [], [], {}
    monkeypatch.setattr(wapp, "_KVM_POPEN", FakePopen)
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {})
    return env["state"] / "kvm-images" / "staging"


def staged(staging):
    return sorted(p.name for p in staging.iterdir()) if staging.exists() else []


def test_upload_streams_checks_and_sends_to_each_host(client, env, upload):
    data = b"\x00" * 300000
    sha = __import__("hashlib").sha256(data).hexdigest()
    r = client.post(f"/api/kvm/hosts/node2/images/upload?pool=images&name=raw.img&sha256={sha}"
                    "&hosts=node2,ghost&limit_mbps=80&format=raw", data=data,
                    content_type="application/octet-stream")
    assert r.status_code == 202, r.get_json()
    d = r.get_json()
    assert d["sha256"] == sha and d["hosts"] == ["node2", "ghost"]
    [path] = staged(upload)
    assert stat.S_IMODE(os.stat(upload / path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(upload).st_mode) == 0o700
    run = only_run()
    assert run.action == "kvm:image-upload:node2/images/raw.img" and run.required_role == "operator"
    _NoThread.started[-1].target()
    f = str(upload / path)
    for h, cmd in zip(("node2", "ghost"), FakePopen.calls):
        assert cmd == base(env, "image", "upload") + [
            f"--host={h}", "--pool=images", "--name=raw.img", f"--file={f}", f"--sha256={sha}",
            "--format=raw", "--limit-mbps=80"]
    assert FakePopen.seen == [(f, 0o600), (f, 0o600)]
    assert staged(upload) == []                                   # supprimé à la fin
    assert run.status == "done" and run.result["output"]["ok"] is True
    assert [h["host"] for h in run.result["output"]["hosts"]] == ["node2", "ghost"]
    steps = [e["step_id"] for e in run.events if e.get("type") == "step"]
    assert "node2:upload" in steps and "ghost:upload" in steps
    assert str(env["state"]) not in json.dumps(run.to_dict()) + r.get_data(as_text=True)


def test_upload_one_host_failure_is_reported_and_the_file_removed(client, upload):
    data = b"x" * 1000
    FakePopen.plan["node2"] = (1, {"ok": False, "error": "pool images is not active", "kind": "refused"},
                               ["STEP_EVENT|check|error|pool images is not active"])
    r = client.post("/api/kvm/hosts/node2/images/upload?pool=images&name=a.img&hosts=ghost", data=data)
    assert r.status_code == 202
    _NoThread.started[-1].target()
    run = only_run()
    assert run.status == "error" and "node2: pool images is not active" in run.error_summary
    assert [h["host"] for h in run.result["output"]["hosts"]] == ["node2", "ghost"]   # le suivant reçoit
    assert staged(upload) == []


def test_upload_cancelled_while_sending_stops_the_next_hosts(client, upload):
    FakePopen.plan["node2"] = (3, {"ok": False, "kind": "cancelled",
                                   "error": "upload cancelled (SIGTERM): the partial volume was deleted"}, [])
    client.post("/api/kvm/hosts/node2/images/upload?pool=images&name=a.img&hosts=ghost", data=b"x" * 10)
    _NoThread.started[-1].target()
    run = only_run()
    assert run.status == "cancelled" and len(FakePopen.calls) == 1
    assert "partial volume was deleted" in run.error_summary and staged(upload) == []


def test_upload_wrong_sha_is_refused_before_sending(client, upload):
    r = client.post(f"/api/kvm/hosts/node2/images/upload?pool=images&name=a.img&sha256={'0' * 64}",
                    data=b"abc")
    assert r.status_code == 422 and "SHA-256" in r.get_json()["error"]
    assert _NoThread.started == [] and FakePopen.calls == [] and staged(upload) == []
    assert only_run().status == "error"


def test_upload_qcow2_with_a_backing_file_is_refused_before_sending(client, upload):
    r = client.post("/api/kvm/hosts/node2/images/upload?pool=images&name=b.qcow2",
                    data=QCOW2_BACKING + b"\0" * 1000)
    assert r.status_code == 422 and r.get_json()["kind"] == "refused"
    assert any("backing" in x for x in r.get_json()["refusals"])
    assert FakePopen.calls == [] and staged(upload) == []


def test_upload_size_limits(client, upload, monkeypatch):
    # sans longueur annoncée : 411
    r = client.post("/api/kvm/hosts/node2/images/upload?pool=images&name=a.img", data=b"")
    assert r.status_code == 411
    # au-delà du plafond (iso_fetch.max_bytes, relu à chaque demande) : 413, rien lu
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {"iso_fetch": {"max_bytes": 100}})
    r = client.post("/api/kvm/hosts/node2/images/upload?pool=images&name=a.img", data=b"x" * 101)
    assert r.status_code == 413 and "iso_fetch.max_bytes" in r.get_json()["error"]
    # place insuffisante sur la console (iso_fetch.min_free_bytes gardé libre) : 507
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {"iso_fetch": {"min_free_bytes": 1000}})
    real = wapp.shutil.disk_usage
    monkeypatch.setattr(wapp.shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(total=1, used=0, free=1009))
    r = client.post("/api/kvm/hosts/node2/images/upload?pool=images&name=a.img", data=b"x" * 10)
    assert r.status_code == 507 and r.get_json()["need"] == 10 and r.get_json()["reason"] == "no-space"
    assert "iso_fetch.min_free_bytes" in r.get_json()["error"]
    monkeypatch.setattr(wapp.shutil, "disk_usage", real)
    assert wapp.ACTIONS == {} and staged(upload) == []


class _ShortStream:
    """Corps qui s'arrête avant la longueur annoncée (navigateur fermé)."""

    def __init__(self, data):
        self.data = data

    def read(self, n):
        out, self.data = self.data[:n], self.data[n:]
        return out


def test_upload_interrupted_body_keeps_nothing(client, upload):
    # navigateur fermé (ou requête interrompue) : annulé, pas une erreur
    env_ = {"CONTENT_LENGTH": "1000", "wsgi.input": _ShortStream(b"x" * 400)}
    r = client.post("/api/kvm/hosts/node2/images/upload?pool=images&name=a.img",
                    environ_overrides=env_, content_type="application/octet-stream")
    assert r.status_code == 409 and r.get_json()["kind"] == "cancelled"
    run = only_run()
    assert run.status == "cancelled" and run.exit_code == 3
    assert staged(upload) == [] and _NoThread.started == []


def test_upload_cancelled_while_receiving(client, upload, monkeypatch):
    class Cancelling(_ShortStream):
        def read(self, n):
            for r in wapp.ACTIONS.values():
                r._cancel = True
            return super().read(min(n, 10))
    env_ = {"CONTENT_LENGTH": "100", "wsgi.input": Cancelling(b"x" * 100)}
    r = client.post("/api/kvm/hosts/node2/images/upload?pool=images&name=a.img",
                    environ_overrides=env_, content_type="application/octet-stream")
    assert r.status_code == 409 and only_run().status == "cancelled" and staged(upload) == []


@pytest.mark.parametrize("query,code", [
    ("pool=images&name=../x", 400), ("pool=images&name=a.img&sha256=zz", 400),
    ("pool=images&name=a.img&hosts=nowhere", 404), ("pool=images&name=a.img&hosts=a%3Bb", 400),
    ("pool=images&name=a.img&format=vmdk", 400), ("pool=images&name=a.img&limit_mbps=nan", 400),
    ("pool=images&name=a.img&limit_mbps=-3", 400), ("pool=p%0Aq&name=a.img", 400),
])
def test_upload_bad_parameters_are_refused_before_reading(client, upload, query, code):
    r = client.post(f"/api/kvm/hosts/node2/images/upload?{query}", data=b"abc")
    assert r.status_code == code, r.get_json()
    assert wapp.ACTIONS == {} and staged(upload) == []


def test_upload_to_a_volume_being_deleted_is_busy(client, upload):
    holder("kvm:vol-delete:node2/images/a.img", holds=[("ghost", "volume", "images/a.img", "x")])
    r = client.post("/api/kvm/hosts/node2/images/upload?pool=images&name=a.img&hosts=ghost", data=b"abc")
    assert r.status_code == 409 and staged(upload) == []


def test_upload_route_reads_the_raw_body_not_multipart():
    body = SRC[SRC.index("def api_kvm_image_upload"):SRC.index("def api_kvm_image_fetch")]
    assert "request.stream" in body and "request.files" not in body and "request.form" not in body


# --- téléchargement d'une URL (adaptateur 1.99.0) ------------------------------------------

SHA = "e" * 64


@pytest.mark.parametrize("url,reason", [
    ("http://127.0.0.1:8200/v1/secret", "address"),
    ("http://10.0.0.1/x.img", "address"),
    ("http://169.254.169.254/latest/meta-data", "address"),
    ("http://[::1]/x", "address"),
    ("http://localhost/x", "address"),
    ("http://0.0.0.0/x", "address"),
    ("http://[::]/x", "address"),
    ("http://224.0.0.1/x", "address"),
    ("http://255.255.255.255/x", "address"),
    ("ftp://example.org/x", "scheme"),
    ("file:///etc/passwd", "scheme"),
    ("http://example.org:99999/x", "bad-url"),
    ("", "bad-url"),
])
def test_fetch_refuses_internal_addresses_before_anything(client, upload, monkeypatch, url, reason):
    r = client.post("/api/kvm/hosts/node2/images/fetch",
                    json={"url": url, "pool": "images", "name": "a.img", "sha256": SHA})
    assert r.status_code == 400 and r.get_json()["reason"] == reason, r.get_json()
    assert wapp.ACTIONS == {} and _NoThread.started == []


class _Handler(http.server.BaseHTTPRequestHandler):
    payload = b"Q" * 70000
    redirect = None

    def do_GET(self):
        if self.path.startswith("/redir") and _Handler.redirect:
            self.send_response(302)
            self.send_header("Location", _Handler.redirect)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *a):
        pass


@pytest.fixture
def http_server():
    srv = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    # le fil du serveur part pour de vrai (threading.Thread est remplacé par _NoThread)
    th = _RealThread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_fetch_from_an_allowed_internal_mirror_downloads_once_and_sends(client, env, upload, monkeypatch,
                                                                       http_server):
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {"iso_fetch": {"allow_private": ["127.0.0.0/8"]}})
    sha = __import__("hashlib").sha256(_Handler.payload).hexdigest()
    url = f"{http_server}/img/cirros.img?token=SIGNED-SECRET"
    r = client.post("/api/kvm/hosts/node2/images/fetch",
                    json={"url": url, "pool": "images", "name": "c.img", "sha256": sha, "hosts": ["ghost"]})
    assert r.status_code == 202, r.get_json()
    assert "SIGNED-SECRET" not in r.get_data(as_text=True)
    _NoThread.started[-1].target()
    run = only_run()
    assert run.status == "done", run.error_summary
    shown = f"{http_server}/img/cirros.img"
    assert [c[-1] for c in FakePopen.calls] == [f"--origin-url={shown}"] * 2
    assert all(f"--sha256={sha}" in c for c in FakePopen.calls)
    assert [s[1] for s in FakePopen.seen] == [0o600, 0o600] and staged(upload) == []
    assert "SIGNED-SECRET" not in json.dumps(run.to_dict()) + " ".join(run.cmd)


def test_fetch_redirect_to_a_refused_address_sends_nothing(client, upload, monkeypatch, http_server):
    # le serveur de départ est autorisé, la redirection vise une adresse privée non autorisée
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {"iso_fetch": {"allow_private": ["127.0.0.1/32"]}})
    monkeypatch.setattr(_Handler, "redirect", "http://10.255.255.1/x")
    r = client.post("/api/kvm/hosts/node2/images/fetch",
                    json={"url": f"{http_server}/redir", "pool": "images", "name": "c.img", "sha256": SHA})
    assert r.status_code == 202
    _NoThread.started[-1].target()
    run = only_run()
    assert run.status == "error" and "iso_fetch.allow_private" in run.error_summary
    assert FakePopen.calls == [] and staged(upload) == []


def test_fetch_wrong_sha_sends_nothing(client, upload, monkeypatch, http_server):
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {"iso_fetch": {"allow_private": ["127.0.0.0/8"]}})
    client.post("/api/kvm/hosts/node2/images/fetch",
                json={"url": f"{http_server}/x", "pool": "images", "name": "c.img", "sha256": "0" * 64})
    _NoThread.started[-1].target()
    assert only_run().status == "error" and FakePopen.calls == [] and staged(upload) == []


def test_fetch_size_cap(client, upload, monkeypatch, http_server):
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {"iso_fetch": {"allow_private": ["127.0.0.0/8"],
                                                                        "max_bytes": 1000}})
    client.post("/api/kvm/hosts/node2/images/fetch", json={"url": f"{http_server}/x", "pool": "images",
                                                           "name": "c.img", "sha256": SHA})
    _NoThread.started[-1].target()
    run = only_run()
    assert run.status == "error" and "iso_fetch.max_bytes" in run.error_summary
    assert FakePopen.calls == [] and staged(upload) == []


def test_fetch_uses_the_shared_guard_once_merged(client, upload, monkeypatch):
    """Adaptateur : quand url_fetch_guard (1.99.0) est importable, la route
    passe par lui et par lui seul (même contrat)."""
    seen = []

    class FetchRefused(Exception):
        def __init__(self, reason, message):
            super().__init__(message)
            self.reason, self.message = reason, message
    fake = types.SimpleNamespace(
        FetchRefused=FetchRefused,
        parse_allowlist=lambda cfg: ["ALLOW"],
        limits=lambda cfg: (5000, 10),
        check_url=lambda url: (seen.append(("check", url)) or ("https", "mirror.example", 443, "/x")),
        resolve_checked=lambda host, port, allow: seen.append(("resolve", host, port, allow)) or "1.2.3.4")
    monkeypatch.setattr(wapp, "_ufg", fake)
    for fallback in ("address_allowed", "address_refusal", "check_url", "download_url"):
        monkeypatch.setattr(wapp._kvm_cli, fallback,
                            lambda *a, **k: pytest.fail("the fallback must not be used"))
    r = client.post("/api/kvm/hosts/node2/images/fetch",
                    json={"url": "https://mirror.example/x", "pool": "images", "name": "c.img"})
    assert r.status_code == 202
    assert seen == [("check", "https://mirror.example/x"), ("resolve", "mirror.example", 443, ["ALLOW"])]

    def refuse(host, port, allow):
        raise FetchRefused("address", "mirror.example resolves to a private address")
    fake.resolve_checked = refuse
    wapp.ACTIONS.clear()
    r = client.post("/api/kvm/hosts/node2/images/fetch",
                    json={"url": "https://mirror.example/x", "pool": "images", "name": "c.img"})
    assert r.status_code == 400 and r.get_json()["reason"] == "address"


def test_downloads_ignore_environment_proxies(upload, monkeypatch, http_server, tmp_path):
    """Comme le script et la garde 1.99.0 : l'adresse vérifiée est celle du
    serveur joint, jamais un mandataire d'environnement (ici un mandataire
    fermé : le téléchargement passe quand même)."""
    for var in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    import ipaddress
    path, size, _sha = wapp._kvm_fetch_download(f"{http_server}/x", tmp_path,
                                                [ipaddress.ip_network("127.0.0.0/8")], 1 << 20, 1,
                                                lambda d, t: None, lambda: False)
    assert size == len(_Handler.payload) and stat.S_IMODE(os.stat(path).st_mode) == 0o600


# --- relevé gardé : invalidation entre processus ------------------------------------------

_BUMP = ("import sys, types; sys.path.insert(0, sys.argv[1]); import kvm_ops as ko; "
         "ko.invalidate_survey(types.SimpleNamespace(state=sys.argv[2], name='node2', "
         "decl={'address': '172.16.1.12', 'uri': 'qemu:///system'}))")


def _other_process_writes(state):
    subprocess.run([sys.executable, "-c", _BUMP, str(ROOT / "bin" / "lib"), str(state)], check=True)


def _shared_cache(spawned=None):
    made = []

    def factory(host):
        made.append(1)
        return f"survey-{len(made)}"
    c = ko.SurveyCache(ttl=100, max_stale=1000, enabled=True, factory=factory,
                       spawn=(spawned.append if spawned is not None else (lambda fn: None)))
    return c, made


def _gen(state):
    return int((Path(state) / "kvm.d" / "survey-gen" / "node2").read_text())


def _host(state):
    return types.SimpleNamespace(state=state, name="node2",
                                 decl={"address": "172.16.1.12", "uri": "qemu:///system"})


def test_a_write_in_another_process_invalidates_the_cached_survey(tmp_path):
    c, made = _shared_cache()
    h = _host(tmp_path)
    assert c.get(h)[0] == "survey-1"
    assert c.get(h)[0] == "survey-1" and len(made) == 1          # gardé
    _other_process_writes(tmp_path)                              # écriture par la CLI
    assert c.get(h) == ("survey-2", 0.0)                         # oublié
    assert c.get(h)[0] == "survey-2" and len(made) == 2
    assert _gen(tmp_path) > 10 ** 18                             # départ pris sur l'horloge


def test_two_processes_share_one_state_dir_both_ways(tmp_path):
    """Le cache de CE processus est invalidé par l'autre, et une écriture
    d'ici est vue par le cache d'un autre processus."""
    probe = ("import sys, types; sys.path.insert(0, sys.argv[1]); import kvm_ops as ko\n"
             "h = types.SimpleNamespace(state=sys.argv[2], name='node2', decl={})\n"
             "n = [0]\n"
             "def f(host):\n    n[0] += 1\n    return n[0]\n"
             "c = ko.SurveyCache(ttl=100, enabled=True, factory=f, spawn=lambda fn: None)\n"
             "c.get(h); c.get(h)\n"
             "sys.stdout.write('ready\\n'); sys.stdout.flush(); sys.stdin.readline()\n"
             "c.get(h); print(n[0])\n")
    p = subprocess.Popen([sys.executable, "-c", probe, str(ROOT / "bin" / "lib"), str(tmp_path)],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "ready"
    ko.invalidate_survey(types.SimpleNamespace(state=tmp_path, name="node2", decl={}))   # écriture d'ici
    out, _ = p.communicate("go\n", timeout=30)
    assert out.strip() == "2"                                    # relevé refait là-bas


def test_a_refresh_started_before_a_write_elsewhere_is_discarded(tmp_path):
    spawned = []
    c, made = _shared_cache(spawned)
    c.ttl = 0                                                     # tout relevé est périmé
    h = _host(tmp_path)
    c.get(h)
    assert c.get(h)[0] == "survey-1" and len(spawned) == 1       # servi + rafraîchissement
    _other_process_writes(tmp_path)                              # écriture PENDANT le relevé
    spawned.pop()()                                              # le relevé finit après l'écriture
    survey, age = c.get(h)
    assert age == 0.0 and survey == "survey-3"                   # l'entrée rafraîchie est jetée


def test_an_unreadable_generation_disables_the_cache(tmp_path):
    c, made = _shared_cache()
    h = _host(tmp_path)
    gen = tmp_path / "kvm.d" / "survey-gen" / "node2"
    gen.parent.mkdir(parents=True)
    gen.write_text("garbage")
    c.get(h)
    c.get(h)
    assert len(made) == 2                                         # jamais servi du cache


def test_the_cli_bumps_the_generation_after_a_write_only(tmp_path):
    gen = tmp_path / "kvm.d" / "survey-gen" / "node2"

    def cli(*argv):
        return subprocess.run([sys.executable, CLI, *argv, "--json", f"--state-dir={tmp_path}"],
                              capture_output=True, text=True, timeout=60)
    cli("image", "list", "--host=node2")                         # lecture
    cli("pool", "delete", "--host=node2", "--pool=p", "--dry-run")
    assert not gen.exists()
    r = cli("pool", "start", "--host=node2", "--pool=p")         # écriture (refusée : hôte inconnu)
    first = _gen(tmp_path)
    assert r.returncode == 2 and first > 10 ** 18
    cli("vol", "delete", "--host=node2", "--pool=p", "--name=v", f"--expect-delete={DIGEST}")
    assert _gen(tmp_path) == first + 1


def test_every_console_action_bumps_the_generation(client, env, lib, monkeypatch):
    monkeypatch.setattr(wapp, "_vm_transfer_runner", lambda run, cmd, capture=False: None)
    client.post("/api/kvm/hosts/node2/pools/images/start", json={})
    _NoThread.started[-1].target()
    assert _gen(env["state"]) > 10 ** 18


def test_cli_template_commands(tmp_path):
    spec = tmp_path / "s.json"
    spec.write_text(json.dumps({"vcpus": 1, "cloudinit": {"user_data": MARKER}}))
    r = subprocess.run([sys.executable, CLI, "template", "save", "--name=-t", f"--spec-file={spec}",
                        "--json", f"--state-dir={tmp_path}"], capture_output=True, text=True)
    assert r.returncode == 2 and json.loads(r.stdout)["kind"] == "usage"     # nom refusé
    r = subprocess.run([sys.executable, CLI, "template", "save", "--name=t1", f"--spec-file={spec}",
                        "--json", f"--state-dir={tmp_path}"], capture_output=True, text=True)
    assert r.returncode == 0 and json.loads(r.stdout)["removed"] == ["cloudinit.user_data"]
    saved = tmp_path / "kvm-templates" / "t1.json"
    assert stat.S_IMODE(os.stat(saved).st_mode) == 0o600 and MARKER not in saved.read_text()
    assert MARKER not in r.stdout + r.stderr


def test_cli_image_pools(tmp_path):
    from test_kvm_routes_196 import decl
    wapp._kd.write(tmp_path, decl("node2"))

    def cli(*a):
        return subprocess.run([sys.executable, CLI, "host", "image-pools", "--host=node2", *a, "--json",
                               f"--state-dir={tmp_path}"], capture_output=True, text=True)
    r = cli("--pool=images", "--pool=-iso")
    assert r.returncode == 0 and json.loads(r.stdout)["image_pools"] == ["images", "-iso"]
    assert wapp._kd.load(tmp_path, "node2")["image_pools"] == ["images", "-iso"]
    assert cli("--pool=a", "--pool=a").returncode == 2
    r = cli()
    assert r.returncode == 0 and "image_pools" not in wapp._kd.load(tmp_path, "node2")


def test_no_secret_in_any_response_or_log(client, env, lib, surveys, caplog):
    """Ni user-data, ni mot de passe VNC (présent dans le XML lu par la
    modification), ni chemin d'état : dans aucune réponse, aucune action,
    aucune ligne du journal."""
    caplog.set_level("DEBUG")
    out = []
    for method, path, body in [
        ("GET", "/api/kvm/vms/node2/web/form", None),
        ("POST", "/api/kvm/hosts/node2/vm-plan", {"spec": SPEC}),
        ("POST", "/api/kvm/vms/node2", {"spec": SPEC}),
        ("POST", "/api/kvm/vms/node2", {"spec": SPEC, "dry_run": True}),
        ("POST", "/api/kvm/vms/node2/web/edit-plan", {"spec": {"display": {"password": True}}}),
        ("PATCH", "/api/kvm/vms/node2/web", {"spec": {"display": {"password": True}}, "expect_config": CONFIG}),
        ("POST", "/api/kvm/vms/node2/db/hot/cloudinit", {"user_data": MARKER}),
        ("GET", "/api/kvm/hosts/node2/images", None),
        ("POST", "/api/kvm/templates", {"name": "t", "spec": SPEC}),
    ]:
        r = client.open(path, method=method, json=body)
        assert r.status_code in (200, 202), (path, r.get_json())
        out.append(r.get_data(as_text=True))
    out += [json.dumps(r.to_dict()) + " ".join(r.cmd) for r in wapp.ACTIONS.values()]
    out.append(caplog.text)
    text = "\n".join(out)
    assert MARKER not in text and VNC_PW not in text and str(env["state"]) not in text


# Vu en réel (tâche 7) : annuler une création pendant une copie complète
# fermait l'action tout de suite (« cancelled », résultat vide) et relâchait
# les noms et les pools tenus pendant que l'outil défaisait encore la copie.
# L'action attend désormais la sortie de l'outil, comme les envois d'image.
def test_cancelling_a_creation_waits_for_the_tool_rollback(client, lib):
    assert client.post("/api/kvm/vms/node2", json={"spec": SPEC}).status_code == 202
    run = only_run()
    assert run.cancel_waits is True
    proc = types.SimpleNamespace(terminated=0, poll=lambda: None)
    proc.terminate = lambda: setattr(proc, "terminated", proc.terminated + 1)
    run.proc, run.status = proc, "running"
    r = client.delete(f"/api/action/{run.id}")
    assert r.status_code == 200
    assert proc.terminated == 1 and run.cancel_requested is True
    assert run.status == "running"            # noms et pools encore tenus pendant le retour arrière
    r = client.post("/api/kvm/vms/node2", json={"spec": SPEC})
    assert r.status_code == 409


def test_other_vm_gestures_still_close_at_once_on_cancel(client, lib):
    assert client.post("/api/kvm/vms/node2/kvmtest-1/start").status_code == 202
    assert only_run().cancel_waits is False


# --- MAC tirées pour les cartes de la fenêtre de création (revue finale I2) --------

MACS_CASES = [
    # (libellé, requête, statut, MAC rendues)
    ("one by default", "", 200, ["52:54:00:00:00:02"]),
    ("collision skipped, distinct", "?count=3", 200,
     ["52:54:00:00:00:02", "52:54:00:00:00:04", "52:54:00:00:00:05"]),
    ("zero refused", "?count=0", 400, None),
    ("above the cap refused", f"?count={wapp.KVM_MACS_MAX + 1}", 400, None),
    ("not a number refused", "?count=two", 400, None),
]


@pytest.mark.parametrize("label,query,status,macs", MACS_CASES, ids=[c[0] for c in MACS_CASES])
def test_mac_draw_route(client, monkeypatch, as_user, label, query, status, macs):
    used = {"domains": {"web": {"active": True, "inactive": {"nics": [{"mac": "52:54:00:00:00:01"}]},
                                "live": {"nics": [{"mac": "52:54:00:00:00:03"}]}}},
            "unknown": ["odd"]}
    monkeypatch.setattr(ks, "read_domains", lambda host: used)
    draws = iter([1, 2, 2, 3, 4, 5])        # .01 et .03 pris sur l'hôte, .02 déjà tiré
    monkeypatch.setattr(ko.secrets, "randbits", lambda bits: next(draws))
    r = as_user("curieux", "GET", f"/api/kvm/hosts/node2/macs{query}")     # lecture : lecteur
    assert r.status_code == status, r.get_json()
    if macs is not None:
        assert r.get_json() == {"host": "node2", "macs": macs, "unchecked": ["odd"]}
    assert wapp.ACTIONS == {}


def test_mac_draw_route_is_never_a_shared_cached_read():
    i = SRC.index("def api_kvm_host_macs(")
    head = SRC[SRC.rindex("@app.route", 0, i):i]
    assert "shared_read" not in head and "_rate_limit(KVM_READ_RATE)" in head
