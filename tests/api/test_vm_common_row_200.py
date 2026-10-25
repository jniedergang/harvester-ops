"""2.0.0 (tâche 11) : la ligne commune des VMs Harvester et KVM.

`/api/vms-all?kinds=harvester,kvm` rend une liste unique : champs communs
(kvm_ops.COMMON_FIELDS), clé stable (`harvester/<c>/<ns>/<nom>`,
`kvm/<uuid>`), dossier lu sur l'objet (annotation, métadonnées libvirt) et
repli « console » du cache. Les dossiers KVM sont lus en UNE invocation de
virsh par hôte (mesure sur node2 dans le rapport de l'étape 3a)."""

import json
import re
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import app as wapp  # noqa: E402
import folders_db as fdb  # noqa: E402
import kvm_host as kh  # noqa: E402
import kvm_ops as ko  # noqa: E402
import read_share as rsh  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "kvm"


def fx(name):
    return (FIX / name).read_text()


# ---------------------------------------------------------------------------
# États libvirt -> phases
# ---------------------------------------------------------------------------
PHASE_TABLE = [
    ("running", "Running"), ("idle", "Running"), ("blocked", "Running"),
    ("shut off", "Stopped"), ("paused", "Paused"), ("pmsuspended", "Paused"),
    ("crashed", "Failed"), ("in shutdown", "Stopping"), ("dying", "Unknown"), ("", "Unknown"),
    ("Running", "Running"),
]


def check_phases(phase_of):
    for state, phase in PHASE_TABLE:
        assert phase_of(state) == phase, state


@pytest.mark.parametrize("state,phase", PHASE_TABLE)
def test_every_libvirt_state_has_its_phase(state, phase):
    assert ko.phase_of(state) == phase
    assert ko.common_row({"host": "node2", "name": "a", "state": state})["phase"] == phase


def test_common_row_mutation_phase(monkeypatch):
    """Mutation volontaire : `paused` et `running` inversés dans la table ->
    le tableau des phases échoue."""
    swapped = dict(ko.PHASES, paused="Running", running="Paused")
    monkeypatch.setattr(ko, "PHASES", swapped)
    with pytest.raises(AssertionError):
        check_phases(ko.phase_of)


# ---------------------------------------------------------------------------
# Mémoire
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("q,expected", [
    ("4Gi", 4 * 2 ** 30), ("512Mi", 512 * 2 ** 20), ("1G", 10 ** 9), ("1073741824", 2 ** 30),
    ("1.5Gi", int(1.5 * 2 ** 30)), ("", None), (None, None), ("lots", None), ("4Xi", None),
])
def test_harvester_memory_in_bytes(q, expected):
    assert wapp._quantity_bytes(q) == expected


@pytest.mark.parametrize("vm,expected", [
    ({"memory_kib": 1024, "memory_max_kib": 2048}, 1024 * 1024),
    ({"memory_kib": None, "memory_max_kib": 2048}, 2048 * 1024),      # domaine arrêté : domstats sans balloon
    ({}, None),
])
def test_kvm_memory_in_bytes(vm, expected):
    assert ko.common_row(dict(vm, host="h", name="n"))["memory_bytes"] == expected


# ---------------------------------------------------------------------------
# Clés et champs figés
# ---------------------------------------------------------------------------
EXPECTED_FIELDS = ("kind", "source", "namespace", "name", "phase", "cpu", "memory_bytes", "node",
                   "ips", "labels", "uuid", "key", "folder", "folder_source")

HARV_VM = {"namespace": "prod", "name": "erp-1", "phase": "Running", "cpu": 4, "memory": "8Gi",
           "node": "harv1-node1", "ips": ["172.16.3.40"], "labels": {"app": "erp"},
           "uid": "0b5e5c1e-2b4f-4a8b-9a43-111111111111", "folder": "/Production/ERP"}


def test_the_common_fields_are_frozen():
    assert ko.COMMON_FIELDS == EXPECTED_FIELDS
    assert tuple(wapp.harvester_common_row(HARV_VM, "harv1")) == EXPECTED_FIELDS
    assert tuple(ko.common_row({"host": "node2", "name": "x"})) == EXPECTED_FIELDS


def test_harvester_row_and_key():
    row = wapp.harvester_common_row(HARV_VM, "harv1")
    assert row == {"kind": "harvester", "source": "harv1", "namespace": "prod", "name": "erp-1",
                   "phase": "Running", "cpu": 4, "memory_bytes": 8 * 2 ** 30, "node": "harv1-node1",
                   "ips": ["172.16.3.40"], "labels": {"app": "erp"},
                   "uuid": "0b5e5c1e-2b4f-4a8b-9a43-111111111111", "key": "harvester/harv1/prod/erp-1",
                   "folder": "/Production/ERP", "folder_source": "vm"}


def test_kvm_row_key_is_the_uuid_and_survives_a_rename():
    vm = {"host": "node2", "name": "sles15sp7", "state": "running", "vcpus": 2, "memory_kib": 2097152,
          "uuid": "851F88C6-88B0-4E6A-85A6-ACF4BB595771"}
    row = ko.common_row(vm)
    assert row["key"] == "kvm/851f88c6-88b0-4e6a-85a6-acf4bb595771"
    assert (row["kind"], row["source"], row["node"], row["namespace"]) == ("kvm", "node2", "node2", None)
    assert ko.common_row(dict(vm, name="sles15sp7-renamed"))["key"] == row["key"]
    assert ko.common_row(dict(vm, uuid=None))["key"] is None


def test_uuid_list_of_node2():
    """Relevé réel de node2 (libvirt 12.8.0, `virsh list --all --uuid --name`)."""
    got = kh.parse_uuid_names(fx("list-uuid-name.txt"))
    assert len(got) == 15
    assert got["harvlab-n1"] == "be02dfd3-538b-4ee2-80de-a0e7f0b12548"
    assert all(kh.UUID_RE.match(u) for u in got.values())


# ---------------------------------------------------------------------------
# Métadonnées de dossier (KVM)
# ---------------------------------------------------------------------------
def test_folder_metadata_present_absent_unreadable():
    assert kh.parse_folder_metadata(fx("metadata-folder.xml")) == "/Lab/It's a test"
    assert kh.parse_folder_metadata("\n\n") is None
    for bad in ("<folder path='/A'", "<other path='/A'/>", "<folder/>",
                "<!DOCTYPE x [<!ENTITY a 'b'>]><folder path='&a;'/>"):
        with pytest.raises(ValueError):
            kh.parse_folder_metadata(bad)


class VirshSim:
    """Hôte simulé au niveau de `batch` : interprète une chaîne de commandes
    virsh (« echo X; metadata --domain 'n' --uri U; ... ») comme virsh le
    fait (sortie concaténée, code de la dernière commande, erreurs sur
    stderr), et une commande `metadata` seule."""

    def __init__(self, domains, absent_rc=0):
        # domains : {nom: chemin | None | "!xml" (illisible) | "!err" (échec)}
        self.domains, self.absent_rc, self.calls = domains, absent_rc, []
        self.name = "node2"

    def one(self, name):
        v = self.domains.get(name, "!gone")
        if v == "!gone":
            return 1, "", f"error: failed to get domain '{name}'\n"
        if v == "!err":
            return 1, "", "error: internal error: client socket is closed\n"
        if v is None:
            return (self.absent_rc, "\n", "error: metadata not found: Requested metadata element is not present\n"
                    if self.absent_rc else "")
        if v == "!xml":
            return 0, "<folder path='/A'\n", ""
        return 0, f'<folder path="{v}"/>\n', ""

    def batch(self, commands, idempotent=False, **kw):
        self.calls.append(dict(commands))
        out = {}
        for key, args in commands.items():
            if len(args) == 1:                       # chaîne de commandes
                stdout, stderr, rc = "", "", 0
                for part in args[0].split("; "):
                    if part.startswith("echo "):
                        stdout += part[5:] + "\n"
                        rc = 0
                        continue
                    m = re.match(r"metadata --domain '(.*)' --uri (\S+)$", part)
                    assert m and m.group(2) == kh.FOLDER_NS, part
                    rc, o, e = self.one(m.group(1))
                    stdout, stderr = stdout + o, stderr + e
                out[key] = kh.CmdOutput(stdout, rc=0 if rc == 0 else rc, stderr=stderr)
            else:
                assert args[:2] == ("metadata", "--domain") and args[3:] == ("--uri", kh.FOLDER_NS)
                rc, o, e = self.one(args[2])
                err = None if rc == 0 else kh.make_error("node2", e, rc, [])
                out[key] = kh.CmdOutput(o if rc == 0 else e, rc=rc, error=err)
        return out


def test_folders_of_a_host_in_one_virsh_invocation():
    sim = VirshSim({"a": "/Prod/ERP", "b": None, "c b": "/Lab"})
    found, errors = kh.read_folders(sim, ["a", "b", "c b"])
    assert found == {"a": "/Prod/ERP", "b": None, "c b": "/Lab"} and errors == []
    assert len(sim.calls) == 1 and list(sim.calls[0]) == ["folders-0"]


@pytest.mark.parametrize("domains,found,error_on", [
    # XML illisible : dossier non lu, erreur listée, les autres lus
    ({"a": "!xml", "b": "/X"}, {"b": "/X"}, ["a"]),
    # libvirt plus ancien : « metadata not found » en code 1 = pas de dossier
    ({"a": None, "b": "/X"}, {"a": None, "b": "/X"}, []),
    # erreur réelle d'un domaine : tranche relue domaine par domaine
    ({"a": "!err", "b": "/X"}, {"b": "/X"}, ["a"]),
])
def test_folder_read_errors_are_listed_not_guessed(domains, found, error_on):
    sim = VirshSim(domains, absent_rc=1 if domains.get("a", 0) is None else 0)
    got, errors = kh.read_folders(sim, list(domains))
    assert got == found
    assert [e["command"] for e in errors] == [f"metadata {n}" for n in error_on]


def test_odd_names_are_read_on_their_own():
    sim = VirshSim({"it's": "/A", "plain": None})
    found, _ = kh.read_folders(sim, ["it's", "plain"])
    assert found == {"it's": "/A", "plain": None}
    assert any("it's" in c for c in sim.calls[-1])


def test_inventory_sets_folder_and_unread_flag():
    """host_inventory(folders=True) : un second appel, dossiers posés, une
    lecture en échec laisse folder None et folder_unread True."""
    class Inv(VirshSim):
        def batch(self, commands, idempotent=False, **kw):
            if "list" in commands:
                self.calls.append(dict(commands))
                ans = {k: kh.CmdOutput("") for k in commands}
                ans["list"] = kh.CmdOutput(" Id   Name   State\n----------------------\n"
                                           " 1    a      running\n -    b      shut off\n\n")
                ans["uuids"] = kh.CmdOutput("49960a5e-a239-42da-95f6-f55383ea5c62 a\n"
                                            "6b128cd4-f440-482d-8e90-1eb32a2c2094 b\n\n")
                return ans
            return super().batch(commands, idempotent)
    sim = Inv({"a": "/P", "b": "!xml"})
    sim.decl = {"address": "10.0.0.1", "labels": {}}
    inv = kh.host_inventory(sim, folders=True)
    a, b = inv["vms"]
    assert (a["folder"], a["folder_unread"], a["uuid"]) == ("/P", False, "49960a5e-a239-42da-95f6-f55383ea5c62")
    assert (b["folder"], b["folder_unread"]) == (None, True)
    assert [e["command"] for e in inv["errors"]] == ["metadata b"]


def test_the_multi_command_goes_as_one_quoted_virsh_argument():
    """Par le vrai `batch` : la chaîne part en UN argument de virsh, citée
    pour le shell distant."""
    calls = []

    class R:
        def __call__(self, argv, **kw):
            calls.append(argv)
            from types import SimpleNamespace
            return SimpleNamespace(returncode=255, stdout="", stderr="ssh: connect to host: Connection refused")
    h = kh.VirshHost({"name": "node2", "address": "10.0.0.1", "user": "ju", "ssh_key": "ssh/node2_id",
                      "known_hosts": "ssh/node2_known"}, "/tmp", runner=R(), read_retries=0)
    with pytest.raises(kh.KvmError):
        kh.read_folders(h, ["a b"])
    import shlex
    rem = shlex.split(calls[0][-1])
    assert rem[:2] == ["sh", "-c"]
    # lecture : lot parallèle depuis la 1.101.2 (`_kvm_bg`), même argv
    run = [ln for ln in rem[2].splitlines() if ln.startswith(("_kvm_run 0 ", "_kvm_bg 0 "))][0]
    words = shlex.split(run)[2:]
    assert words[:3] == ["virsh", "-c", "qemu:///system"] and len(words) == 4
    assert re.fullmatch(r"echo (@@hops-[0-9a-f]+)-0; metadata --domain 'a b' --uri "
                        + re.escape(kh.FOLDER_NS) + r"; echo \1-end", words[3])


# ---------------------------------------------------------------------------
# /api/vms-all?kinds=harvester,kvm
# ---------------------------------------------------------------------------
def _vm(name, ns="default", folder=None, mem="2Gi"):
    v = {"namespace": ns, "name": name, "phase": "Stopped", "cpu": 1, "memory": mem, "node": None,
         "ips": [], "labels": {}, "uid": f"uid-{name}", "priority": 10, "group": "default",
         "group_priority": 100, "snapshot": True, "ready_timeout": 300, "runStrategy": "Halted",
         "agent_connected": "False"}
    v.update(wapp._folder_from_annotation({"harvester-ops.io/folder": folder} if folder is not None else {}))
    return v


@pytest.fixture
def world(monkeypatch, tmp_path):
    cfg = {"clusters": [{"name": "harv1", "kubeconfig": "/k/harv1.yaml"},
                        {"name": "off", "kubeconfig": "/k/off.yaml"}]}
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")
    monkeypatch.setattr(wapp, "load_config_ro", lambda: cfg)
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: f"/staged/{c}.yaml")
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc, **k: "off" not in kc)
    db = tmp_path / "folders.db"
    monkeypatch.setenv("HARVESTER_OPS_FOLDERS_DB", str(db))
    monkeypatch.setattr(wapp, "READ_SHARE", rsh.ReadShare())
    vms = [_vm("erp-1", "prod", "/Production/ERP"), _vm("web", folder="not-absolute"), _vm("db")]
    monkeypatch.setattr(wapp, "_vms_collect", lambda c, kc: ({"cluster": c, "vms": [dict(v) for v in vms]}, 200, []))
    kvm = {"name": "node2", "vms": [
        {"host": "node2", "name": "sles15sp7", "id": None, "state": "shut off", "persistent": True,
         "autostart": False, "vcpus": 2, "memory_kib": None, "memory_max_kib": 2097152,
         "cpu_time_ns": None, "uuid": "851f88c6-88b0-4e6a-85a6-acf4bb595771", "folder": "/Lab",
         "folder_unread": False},
        {"host": "node2", "name": "mlm-src", "id": None, "state": "shut off", "persistent": True,
         "autostart": False, "vcpus": 1, "memory_kib": None, "memory_max_kib": 1048576,
         "cpu_time_ns": None, "uuid": "db540746-4fb3-4d1a-9d61-eb283bd776e0", "folder": None,
         "folder_unread": True}],
        "errors": [{"command": "metadata mlm-src", "kind": "failed", "message": "node2: mlm-src: unreadable"}]}
    seen = {}

    class Cli:
        @staticmethod
        def read_all(hosts, details=False, deadline=None, folders=False):
            seen["folders"] = folders
            return [dict(kvm), {"name": "kvm-off", "error": {"kind": "unreachable", "message": "kvm-off: down"}}]
    monkeypatch.setattr(wapp, "_kvm_cli", Cli)
    state = Path(tempfile.mkdtemp(prefix="vcr-", dir="/tmp"))
    import kvm_decl as kd
    for n in ("node2", "kvm-off"):
        kd.write(state, {"name": n, "address": "10.0.0.9", "user": "ju", "ssh_key": kd.key_rel(n),
                         "known_hosts": kd.known_hosts_rel(n)})
    monkeypatch.setattr(wapp, "_state_dir", lambda: state)
    yield {"db": db, "seen": seen}
    import shutil
    shutil.rmtree(state, ignore_errors=True)


def get(c, qs):
    r = c.get("/api/vms-all" + qs)
    return r.status_code, r.get_json()


def test_without_kinds_the_answer_is_unchanged(world):
    with wapp.app.test_client() as c:
        st, d = get(c, "")
    assert st == 200 and set(d) == {"clusters", "vms"}
    assert all("folder_invalid" not in v and "kind" not in v for v in d["vms"])


@pytest.mark.parametrize("qs", ["?kinds=", "?kinds=vmware", "?kinds=harvester,xen"])
def test_unknown_kinds_are_refused(world, qs):
    with wapp.app.test_client() as c:
        st, d = get(c, qs)
    assert st == 400 and "kinds" in d["error"]


def test_one_list_for_harvester_and_kvm(world):
    with fdb.connect(world["db"]) as _:
        pass
    conn = fdb.connect(world["db"])
    fdb.upsert_membership(conn, "harvester/harv1/default/db", "/Ops", "console", scope="harvester/harv1")
    fdb.upsert_membership(conn, "kvm/db540746-4fb3-4d1a-9d61-eb283bd776e0", "/Kept", "vm", scope="kvm/node2")
    conn.close()
    with wapp.app.test_client() as c:
        st, d = get(c, "?kinds=harvester,kvm")
    assert st == 200 and world["seen"]["folders"] is True
    assert {(s["kind"], s["source"], s["state"]) for s in d["sources"]} == {
        ("harvester", "harv1", "ok"), ("harvester", "off", "unreachable"),
        ("kvm", "node2", "ok"), ("kvm", "kvm-off", "unreachable")}
    node2 = next(s for s in d["sources"] if s["source"] == "node2")
    assert node2["count"] == 2 and "unreadable" in node2["error"]
    rows = {r["key"]: r for r in d["vms"]}
    assert all(set(EXPECTED_FIELDS) <= set(r) for r in d["vms"])
    assert (rows["harvester/harv1/prod/erp-1"]["folder"], rows["harvester/harv1/prod/erp-1"]["folder_source"]) == (
        "/Production/ERP", "vm")
    # objet sans valeur + cache « console » : le repli reste
    assert (rows["harvester/harv1/default/db"]["folder"], rows["harvester/harv1/default/db"]["folder_source"]) == (
        "/Ops", "console")
    # chemin invalide dans l'annotation : ignoré, signalé
    assert rows["harvester/harv1/default/web"]["folder"] is None
    assert d["folder_warnings"] == [{"key": "harvester/harv1/default/web",
                                     "reason": "the folder path must start with '/'"}]
    kv = rows["kvm/851f88c6-88b0-4e6a-85a6-acf4bb595771"]
    assert (kv["phase"], kv["memory_bytes"], kv["folder"], kv["folder_source"]) == ("Stopped", 2 * 2 ** 30, "/Lab", "vm")
    # dossier illisible : le cache reste tel quel (ni retiré ni rafraîchi)
    unread = rows["kvm/db540746-4fb3-4d1a-9d61-eb283bd776e0"]
    assert (unread["folder"], unread["folder_source"]) == ("/Kept", "vm")
    assert "_scope" not in unread and "_unread" not in unread
    # les dossiers nommés par les objets existent maintenant dans la base
    conn = fdb.connect(world["db"])
    paths = {f["path"]: f["origin"] for f in fdb.list_tree(conn)}
    assert paths["/Production/ERP"] == "objects" and paths["/Lab"] == "objects"
    assert fdb.memberships(conn)["kvm/db540746-4fb3-4d1a-9d61-eb283bd776e0"]["folder_path"] == "/Kept"
    conn.close()
    assert d["clusters"] and json.dumps(d)          # `clusters` gardé pour all-vms.js


def test_an_object_that_lost_its_folder_drops_the_cached_vm_membership(world):
    conn = fdb.connect(world["db"])
    fdb.upsert_membership(conn, "harvester/harv1/default/db", "/Old", "vm", scope="harvester/harv1")
    conn.close()
    with wapp.app.test_client() as c:
        st, d = get(c, "?kinds=harvester")
    row = next(r for r in d["vms"] if r["key"] == "harvester/harv1/default/db")
    assert (row["folder"], row["folder_source"]) == (None, None)
    assert all(s["kind"] == "harvester" for s in d["sources"])


def test_uuid_list_keeps_spaces_around_a_domain_name():
    text = (f"{'49960a5e-a239-42da-95f6-f55383ea5c62'}  lead\n"
            f"{'6b128cd4-f440-482d-8e90-1eb32a2c2094'} trail \n"
            f"{'d516b457-b2a1-440a-8df3-0657d2523ba7'} two  words\n\n")
    assert kh.parse_uuid_names(text) == {" lead": "49960a5e-a239-42da-95f6-f55383ea5c62",
                                         "trail ": "6b128cd4-f440-482d-8e90-1eb32a2c2094",
                                         "two  words": "d516b457-b2a1-440a-8df3-0657d2523ba7"}
