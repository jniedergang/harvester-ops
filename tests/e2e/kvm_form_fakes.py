"""API KVM simulée pour les fenêtres Créer une VM et Modifier (prochaine
version KVM). Les réponses ont la forme EXACTE de celles de la console : les
capacités sont calculées par kvm_caps.form_options sur les relevés de node2,
la vue d'une VM par kvm_xml.managed_view et la table HOTNESS est celle de
kvm_edit ; seules les actions (création, modification, gestes) sont simulées."""

import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent.parent
FIX = ROOT / "tests" / "fixtures" / "kvm"
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import kvm_caps as kc  # noqa: E402
import kvm_edit as ke  # noqa: E402
import kvm_xml as kx  # noqa: E402

EVIL = "<img src=x onerror=window.__xss=1>"
EVIL_POOL = "p<b>ool\"'"
EVIL_NET = "n<i>et"
EVIL_IMG = "i<svg onload=window.__xss=2>.qcow2"
DIGEST0, DIGEST1 = "a" * 64, "b" * 64
USER_SECRET = "Sup3rS3cretPw"


def caps_options():
    caps = kc.parse_capabilities((FIX / "capabilities-libvirt-12.8.xml").read_text())
    q35 = kc.parse_domcapabilities((FIX / "domcapabilities-q35.xml").read_text())
    pc = kc.parse_domcapabilities((FIX / "domcapabilities-pc.xml").read_text())
    return kc.form_options(caps, {"q35": q35, "pc": pc})


def caps_no_tpm():
    caps = kc.parse_capabilities((FIX / "capabilities-libvirt-12.8.xml").read_text())
    q35 = kc.parse_domcapabilities((FIX / "domcapabilities-q35-no-tpm.xml").read_text())
    pc = kc.parse_domcapabilities((FIX / "domcapabilities-pc.xml").read_text())
    return kc.form_options(caps, {"q35": q35, "pc": pc})


POOLS = [
    {"name": "images", "state": "running", "type": "dir", "target": "/var/lib/libvirt/images",
     "autostart": True, "capacity": 900 * 2 ** 30, "allocation": 100 * 2 ** 30, "available": 800 * 2 ** 30},
    {"name": "small", "state": "running", "type": "dir", "target": "/srv/small", "autostart": True,
     "capacity": 10 * 2 ** 30, "allocation": 9 * 2 ** 30, "available": 2 ** 30},
    {"name": EVIL_POOL, "state": "running", "type": "dir", "target": "/srv/evil", "autostart": False,
     "capacity": 2 ** 30, "allocation": 0, "available": 2 ** 30},
    {"name": "off", "state": "inactive", "type": "dir", "target": "/srv/off", "autostart": False},
]
NETWORKS = [{"name": "default", "active": True, "forward": "nat", "bridge": "virbr0"},
            {"name": EVIL_NET, "active": True, "forward": "isolated", "bridge": "virbr9"}]
LINKS = {"bridges": [{"name": "br0", "state": "UP"}], "physical": [{"name": "eno1", "state": "UP", "master": "br0"}],
         "links": ["lo", "eno1", "br0"], "networks": []}
IMAGES = [
    {"pool": "images", "volume": "cirros-0.6.3.qcow2", "format": "qcow2", "iso": False, "capacity": 117440512},
    {"pool": "images", "volume": "netinst.iso", "format": "raw", "iso": True, "capacity": 650 * 2 ** 20},
    {"pool": "images", "volume": EVIL_IMG, "format": "qcow2", "iso": False, "capacity": 2 ** 30},
]


def vm_form(state="running", persistent=True, pending=None, digest=DIGEST0, name="kvmtest-1"):
    view = kx.managed_view((FIX / "dumpxml-session-defined-q35.xml").read_text())
    view["name"] = name
    return {"name": name, "view": view, "unmanaged": ["cputune", "devices/hostdev", "metadata:urn:other"],
            "pending": pending or [], "restart_required": bool(pending), "hotness": list(ke.HOTNESS),
            "config_digest": digest, "state": state, "persistent": persistent, "autostart": False,
            "snapshots": 0, "host": "node2", "role": "operator"}


def sse(steps, status="done", output=None, error=None):
    body = "".join(f"event: step\ndata: {json.dumps(s)}\n\n" for s in steps)
    end = {"status": status, "error_summary": error, "result": {"output": output or {}}}
    return body + f"event: end\ndata: {json.dumps(end)}\n\n"


VMS = [{"host": "node2", "name": "kvmtest-1", "state": "running", "persistent": True, "autostart": False,
        "vcpus": 2, "memory_kib": 2097152},
       {"host": "node2", "name": "temp", "state": "running", "persistent": False, "autostart": False,
        "vcpus": 1, "memory_kib": 262144},
       {"host": "node2", "name": EVIL, "state": "shut off", "persistent": True, "autostart": False,
        "vcpus": 1, "memory_kib": 262144}]
GESTURES = {"destroy": ["shut off"], "reboot": ["paused", "shut off"], "resume": ["running", "shut off"],
            "shutdown": ["paused", "shut off"], "start": ["running"], "suspend": ["shut off"]}
OPERATIONS = {"snapshots": {"role": "operator", "states": None, "persistent": False},
              "clone": {"role": "operator", "states": ["shut off"], "persistent": True},
              "delete": {"role": "admin", "states": ["shut off"], "persistent": True}}
EDIT_OPS = {"edit": {"role": "operator", "states": ["running", "idle", "blocked", "shut off"], "persistent": False},
            "hot": {"role": "operator", "states": ["running", "idle", "blocked", "shut off"], "persistent": False}}


def install(page, state):
    """Route /api/kvm/*, /api/clusters, /api/sshkeys/* et les flux d'actions."""
    state.setdefault("role", "operator")
    state.setdefault("sent", [])
    state.setdefault("streams", {})
    state.setdefault("caps", caps_options())
    state.setdefault("form", vm_form())
    state.setdefault("gets", [])

    def ok(route, body, status=200):
        return route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def api(route, req):
        parts = urlsplit(req.url)
        path = unquote(parts.path.split("/api/kvm/", 1)[1])
        q = parse_qs(parts.query)
        m = req.method
        if m == "GET":
            state["gets"].append(path)
            if path == "hosts":
                return ok(route, {"hosts": [{"name": "node2"}, {"name": "node9"}], "warnings": [],
                                  "role": state["role"], "gestures": GESTURES})
            if path == "vms":
                return ok(route, {"vms": VMS, "role": state["role"], "gestures": GESTURES,
                                  "operations": OPERATIONS, "edit_operations": EDIT_OPS, "warnings": [],
                                  "hosts": [{"name": "node2", "state": "ok", "count": len(VMS)}]})
            mm = re.fullmatch(r"hosts/(node2|node9)/(caps|pools|networks|links|images)", path)
            if mm:
                what = mm.group(2)
                if what == "caps":
                    return ok(route, {"host": mm.group(1), "options": state["caps"], "role": state["role"]})
                if what == "pools":
                    return ok(route, {"pools": POOLS, "role": state["role"]})
                if what == "networks":
                    return ok(route, {"networks": NETWORKS, "role": state["role"]})
                if what == "links":
                    return ok(route, dict(LINKS, role=state["role"]))
                return ok(route, {"images": IMAGES, "pools": ["images"], "role": state["role"]})
            if path == "templates":
                return ok(route, {"templates": [{"name": "web-small", "owner": "ju"}], "role": state["role"]})
            if path == "templates/web-small":
                return ok(route, {"name": "web-small", "removed": ["cloudinit.user_data"], "owner": "ju",
                                  "spec": {"os": "linux", "machine": "q35", "cpu": {"vcpus": 2, "max_vcpus": 2,
                                           "sockets": 2, "cores": 1, "threads": 1, "mode": "host-model"},
                                           "memory": {"current_mib": 512, "max_mib": 512},
                                           "disks": [{"device": "disk", "source": {"kind": "blank", "pool": "images",
                                                                                   "size_bytes": 2 * 2 ** 30}}],
                                           "nics": [{"kind": "network", "source": "default", "model": "virtio"}]}})
            mm = re.fullmatch(r"vms/node2/(.+)/(form|cloudinit)", path)
            if mm:
                if mm.group(2) == "form":
                    return ok(route, dict(state["form"], name=mm.group(1)))
                return ok(route, {"host": "node2", "name": mm.group(1), "seed": {"pool": "images", "volume": "kvmtest-1-cidata.iso"},
                                  "user_data": f"#cloud-config\nchpasswd:\n  list: |\n    root:{USER_SECRET}\n",
                                  "network_data": None})
            mm = re.fullmatch(r"vms/node2/([^/]+)", path)
            if mm:
                return ok(route, {"host": "node2", "name": mm.group(1), "state": "running",
                                  "disks": [{"target": "vda", "device": "disk", "source": "images/kvmtest-1-vda.qcow2"},
                                            {"target": "sdb", "device": "cdrom", "source": None},
                                            {"target": "sdc", "device": "cdrom", "source": "images/kvmtest-1-cidata.iso"}],
                                  "nics": [{"mac": "52:54:00:81:e2:fb", "type": "network", "source": "default"}]})
            return ok(route, {"error": "not found"}, 404)
        body = json.loads(req.post_data or "{}")
        state["sent"].append((m, path, body))
        aid = f"kvm{len(state['sent']):09d}"
        if path == "hosts/node2/vm-plan":
            spec = body["spec"]
            refusals = [] if spec.get("name") != "taken" else ["a VM named taken already exists"]
            return ok(route, {"host": "node2", "dry_run": True, "possible": not refusals, "role": state["role"],
                              "plan": {"possible": not refusals, "refusals": refusals,
                                       "warnings": ["the network n<i>et is inactive"],
                                       "instances": [{"name": spec["name"], "seed": {"pool": "images", "volume": f"{spec['name']}-cidata.iso"},
                                                      "volumes": [{"target": "vda", "pool": "images", "volume": f"{spec['name']}-vda.qcow2",
                                                                   "kind": "image-linked", "capacity": 2 ** 30}],
                                                      "xml_preview": "<domain><graphics type='vnc' passwd='***'/></domain>"}]}})
        if path.endswith("/edit-plan"):
            if body.get("expect_config") != state["form"]["config_digest"]:
                return ok(route, {"error": "the VM was changed since you opened it: reload", "kind": "changed"}, 409)
            # comme le serveur : un spec vide ne change rien
            ch = [{"field": "cpu.vcpus", "applied": "now", "reason": None},
                  {"field": "cpu.max_vcpus", "applied": "next-boot", "reason": "applied at the next start"}] \
                if body.get("spec") else []
            return ok(route, {"host": "node2", "dry_run": True, "possible": True, "role": state["role"],
                              "plan": {"changes": ch, "refusals": [], "empty": False}})
        if m == "PATCH":
            if body.get("expect_config") != state["form"]["config_digest"]:
                return ok(route, {"error": "the VM was changed since you opened it: reload", "kind": "changed"}, 409)
            state["form"] = dict(state["form"], pending=["cpu.max_vcpus"], config_digest=DIGEST1)
            state["streams"][aid] = sse(
                [{"step_id": "plan", "status": "done", "message": "2 change(s)"},
                 {"step_id": "setvcpus", "status": "done", "message": "3 vCPUs"},
                 {"step_id": "define", "status": "done", "message": "defined"}],
                output={"ok": True, "name": "kvmtest-1", "changes": [
                    {"field": "cpu.vcpus", "applied": "now", "reason": None},
                    {"field": "cpu.max_vcpus", "applied": "next-boot", "reason": "applied at the next start"}],
                    "restart_required": True, "after": {"pending": ["cpu.max_vcpus"]}})
            return ok(route, {"action_id": aid}, 202)
        if path == "vms/node2":
            names = [body["spec"]["name"]] if not body["spec"].get("instances") else [
                f"{body['spec']['name']}-{i:02d}" for i in range(1, body["spec"]["instances"] + 1)]
            if state.get("create_fails"):
                state["streams"][aid] = sse([{"step_id": "define", "status": "error", "message": "define refused"}],
                                            status="error", error="define refused",
                                            output={"ok": False, "error": "define refused", "created": [names[0]] if len(names) > 1 else [],
                                                    "leftovers": [{"pool": "images", "volume": f"{names[-1]}-vda.qcow2"}]})
            else:
                state["streams"][aid] = sse(
                    [{"step_id": "plan", "status": "done", "message": "1 VM"},
                     {"step_id": "volume-vda", "status": "done", "message": "created"},
                     {"step_id": "seed", "status": "done", "message": "uploaded"},
                     {"step_id": "define", "status": "done", "message": "defined"},
                     {"step_id": "verify", "status": "done", "message": "running"}],
                    output={"ok": True, "created": [{"name": n, "state": "running", "uuid": "u", "volumes": [], "seed": None,
                                                     "vnc_password": True} for n in names], "warnings": [], "leftovers": []})
            return ok(route, {"action_id": aid, "names": names}, 202)
        if "/hot/" in path:
            state["streams"][aid] = sse([{"step_id": "plan", "status": "done", "message": "cpumem"}],
                                        output={"ok": True, "changes": [
                                            {"field": "cpu.vcpus", "applied": "now", "reason": None},
                                            {"field": "memory.current_mib", "applied": "next-boot",
                                             "reason": "above the current maximum (4096 MiB)"}],
                                            "restart_required": True, "after": {"pending": ["memory.max_mib"]}})
            return ok(route, {"action_id": aid}, 202)
        if path == "templates":
            state["streams"][aid] = sse([{"step_id": "save", "status": "done", "message": "saved"}],
                                        output={"ok": True, "name": body.get("name"), "removed": ["cloudinit.user_data"]})
            return ok(route, {"action_id": aid}, 202)
        state["streams"][aid] = sse([{"step_id": "g", "status": "done", "message": "ok"}],
                                    output={"ok": True, "state": "shut off" if path.endswith("shutdown") else "running"})
        return ok(route, {"action_id": aid}, 202)

    page.route(re.compile(r".*/api/kvm/.*"), api)
    page.route(re.compile(r".*/api/clusters$"), lambda r, q: ok(r, {"clusters": [{"name": "harv1"}]}))
    page.route(re.compile(r".*/api/sshkeys/harv1$"), lambda r, q: ok(r, {"items": [
        {"name": "ju-key", "namespace": "default", "public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOk ju@node1"}]}))
    page.route(re.compile(r".*/api/stream/kvm\d+$"), lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body=state["streams"].get(q.url.rsplit("/", 1)[1], sse([]))))
    return state
