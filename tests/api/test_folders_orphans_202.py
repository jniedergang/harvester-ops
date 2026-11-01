"""2.0.2, décision 94 : une entrée d'objet disparu ne bloque plus la
suppression d'un dossier.

Avant, un dossier qui ne gardait que l'entrée d'un objet qui n'existe plus
(hôte KVM retiré, VM supprimée, cluster retiré) restait « non vide » jusqu'à
la purge des 30 jours : on ne pouvait ni le supprimer ni le vider. Désormais
la suppression retire ces entrées (une étape et une ligne de journal
chacune) quand l'objet est PROUVÉ disparu ; une entrée qu'on ne peut pas
vérifier (hôte injoignable, objet caché, lecture en échec) bloque avec sa
raison, et rien n'est alors retiré.

Côté outil : leurres de kubectl et de virsh au niveau de `run` et `batch`,
réponses relevées sur kubectl 1.35 et libvirt. Côté console : la demande
porte les clusters que la personne voit et SES kubeconfigs, jamais celui de
la console pour un cluster caché."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_folders_cli_200 as base  # noqa: E402
import test_folders_routes_200 as rbase  # noqa: E402
from test_folders_cli_200 import env, run  # noqa: E402,F401
from test_folders_routes_200 import call, world  # noqa: E402,F401

cli, fdb, kh = base.cli, base.fdb, base.kh
KubeError = base.KubeError
UUID1, UUID2 = base.UUID1, base.UUID2

# réponses de kubectl 1.35 (harv1)
NOT_FOUND = 'Error from server (NotFound): virtualmachines.kubevirt.io "{n}" not found'
FORBIDDEN = ('Error from server (Forbidden): virtualmachines.kubevirt.io "{n}" is forbidden: User "u" '
             'cannot get resource "virtualmachines" in API group "kubevirt.io" in the namespace "nav"')
DOWN = "Unable to connect to the server: dial tcp 172.16.3.100:6443: i/o timeout"
# « not found » qui n'est PAS l'absence de l'objet : ne doit jamais le retirer
CONTEXT = 'error: context "harv1" not found'


class ProbeKube(base.FakeKube):
    """`run("get", ressource, nom, "-n", ns, "-o", "name")` : selon `fail`
    {nom: gone | hidden | down | ctx}, sinon l'objet existe."""

    def __init__(self, fail=None):
        super().__init__(fail)
        self.runs = []

    def run(self, *args, timeout=None, input=None):
        self.runs.append(args)
        name = args[2]
        why = self.fail.get(name)
        text = {"gone": NOT_FOUND, "hidden": FORBIDDEN, "down": DOWN, "ctx": CONTEXT}.get(why)
        if text:
            raise KubeError(text.format(n=name))
        return f"virtualmachine.kubevirt.io/{name}\n"


class ProbeVirsh:
    """`batch({"d": ("domname", uuid)})` : domaine présent, absent
    (`failed to get domain`), hôte injoignable ou erreur de libvirt."""

    def __init__(self, present=(), down=False, broken=False):
        self.present, self.down, self.broken, self.calls = set(present), down, broken, []

    def batch(self, commands, idempotent=False, **kw):
        self.calls.append(dict(commands))
        if self.down:
            raise kh.KvmError("unreachable", "node2: unreachable: connection refused", host="node2")
        out = {}
        for key, (verb, uuid_) in commands.items():
            assert verb == "domname" and idempotent
            if self.broken:
                err = kh.KvmError("failed", "node2: failed: libvirt says no", host="node2")
                out[key] = kh.CmdOutput("libvirt says no", rc=1, error=err)
            elif uuid_ in self.present:
                out[key] = kh.CmdOutput("vm-a\n")
            else:
                err = kh.KvmError("notfound", f"node2: notfound: failed to get domain '{uuid_}'", host="node2")
                out[key] = kh.CmdOutput(f"error: failed to get domain '{uuid_}'", rc=1, error=err)
        return out


@pytest.fixture
def probe(env, monkeypatch):
    kube, virsh = ProbeKube(), ProbeVirsh()
    monkeypatch.setattr(cli, "make_kube", lambda cluster, spec: kube)
    monkeypatch.setattr(cli, "make_host", lambda name: virsh)
    return dict(env, kube=kube, virsh=virsh)


def file_in_old(env, entries):
    conn = fdb.connect(env["db"])
    try:
        for key, scope in entries:
            fdb.upsert_membership(conn, key, "/Old", "console", scope=scope)
        return fdb.id_of(conn, "/Old")
    finally:
        conn.close()


def folder_exists(env, path):
    conn = fdb.connect(env["db"])
    try:
        return fdb.id_of(conn, path) is not None
    finally:
        conn.close()


VM_GONE = ("harvester/harv1/nav/gone", "harvester/harv1")
VM_HERE = ("harvester/harv1/nav/here", "harvester/harv1")

# (cas, entrées, fail du leurre kube, état de l'hôte KVM, demande, code, retirées, fragment attendu)
CASES = [
    ("undeclared KVM host", [("host/kvm/kvmfake", "hosts")], {}, {}, None, 0,
     ["host/kvm/kvmfake"], "KVM host kvmfake is no longer declared"),
    ("VM of an undeclared KVM host", [(f"kvm/{UUID1}", "kvm/ghost")], {}, {}, None, 0,
     [f"kvm/{UUID1}"], "KVM host ghost is no longer declared"),
    ("domain gone from its host", [(f"kvm/{UUID1}", "kvm/node2")], {}, {}, None, 0,
     [f"kvm/{UUID1}"], "domain no longer on KVM host node2"),
    ("domain still there", [(f"kvm/{UUID1}", "kvm/node2")], {}, {"present": [UUID1]}, None, 1,
     [], "1 object(s)"),
    ("KVM host unreachable", [(f"kvm/{UUID1}", "kvm/node2")], {}, {"down": True}, None, 1,
     [], "cannot be checked: KVM host node2 cannot be reached (unreachable)"),
    ("libvirt error", [(f"kvm/{UUID1}", "kvm/node2")], {}, {"broken": True}, None, 1,
     [], "KVM host node2 cannot be read (failed)"),
    ("KVM entry without its host", [(f"kvm/{UUID1}", "")], {}, {}, None, 1, [], "its KVM host is not known"),
    ("VM deleted", [VM_GONE], {"gone": "gone"}, {}, None, 0,
     ["harvester/harv1/nav/gone"], "not found on cluster harv1"),
    ("VM still there", [VM_HERE], {}, {}, None, 1, [], "1 object(s)"),
    ("VM the person cannot read", [VM_GONE], {"gone": "hidden"}, {}, None, 1, [], "objects you cannot see"),
    ("cluster unreachable", [VM_GONE], {"gone": "down"}, {}, None, 1, [], "cluster harv1 cannot be read"),
    ("not found that is no NotFound", [VM_GONE], {"gone": "ctx"}, {}, None, 1, [], "cluster harv1 cannot be read"),
    ("VM of a removed cluster", [("harvester/removed/nav/x", "harvester/removed")], {}, {}, None, 0,
     ["harvester/removed/nav/x"], "cluster removed is no longer declared"),
    ("VM of a cluster declared by the console", [("harvester/gpudemo/nav/gone", "harvester/gpudemo")],
     {"gone": "gone"}, {}, None, 0, ["harvester/gpudemo/nav/gone"], "not found on cluster gpudemo"),
    ("removed cluster as a host", [("host/harvester/removed", "hosts")], {}, {}, None, 0,
     ["host/harvester/removed"], "cluster removed is no longer declared"),
    ("declared cluster as a host", [("host/harvester/harv1", "hosts")], {}, {}, None, 1, [], "1 object(s)"),
    ("declared KVM host", [("host/kvm/node2", "hosts")], {}, {}, None, 1, [], "1 object(s)"),
    ("workload deleted", [("container/harv1/web/deployment/gone", "container/harv1")], {"gone": "gone"}, {},
     None, 0, ["container/harv1/web/deployment/gone"], "not found on cluster harv1"),
    ("workload kind that cannot be filed", [("container/harv1/web/job/gone", "container/harv1")], {}, {},
     None, 1, [], "unrecognised entry"),
    ("cluster hidden from the person", [VM_GONE], {"gone": "gone"}, {},
     {"visible": [], "clusters": [], "kubeconfigs": {}}, 1, [], "objects you cannot see"),
    ("visible cluster without the person's kubeconfig", [VM_GONE], {"gone": "gone"}, {},
     {"visible": [], "clusters": ["harv1"], "kubeconfigs": {}}, 1, [], "objects you cannot see"),
    ("one orphan and one live object", [("host/kvm/kvmfake", "hosts"), VM_HERE], {}, {}, None, 1,
     [], "1 object(s)"),
]


@pytest.mark.parametrize("case,entries,fail,host,spec,code,purged,fragment", CASES, ids=[c[0] for c in CASES])
def test_delete_purges_proven_orphans_and_blocks_on_the_rest(probe, capsys, monkeypatch, case, entries, fail,
                                                            host, spec, code, purged, fragment):
    probe["kube"].fail.update(fail)
    for k, v in host.items():
        setattr(probe["virsh"], k, set(v) if k == "present" else v)
    fid = file_in_old(probe, entries)
    assert run(probe, "delete", "--id", str(fid), spec=spec) == code
    cap = capsys.readouterr()
    assert fragment in cap.err, cap.err
    left = set(base.members(probe))
    if code:
        # refus : RIEN n'est retiré, pas même l'orphelin d'un lot mixte
        assert {k for k, _ in entries} <= left and folder_exists(probe, "/Old")
        assert "an entry is removed only once its object is proven gone" in cap.err
    else:
        assert not folder_exists(probe, "/Old") and not ({k for k, _ in entries} & left)
        assert json.loads(cap.out)["purged"] == len(purged)
        rows = base.journal(probe, json.loads(cap.out)["action"])
        assert [(k, f, t, s) for k, f, t, s in rows] == [(k, "/Old", None, "done") for k in sorted(purged)]


def test_purge_lines_are_journalled_under_the_action_id(probe, capsys):
    fid = file_in_old(probe, [("host/kvm/kvmfake", "hosts"), (f"kvm/{UUID2}", "kvm/node2")])
    assert cli.main(["--db", str(probe["db"]), "--json", "--role", "operator", "delete", "--id", str(fid),
                     "--id=fa-orphans01"]) == 0
    err = capsys.readouterr().err
    rows = base.journal(probe, "fa-orphans01")
    assert [(k, t, s) for k, _f, t, s in rows] == [("host/kvm/kvmfake", None, "done"), (f"kvm/{UUID2}", None, "done")]
    conn = fdb.connect(probe["db"])
    try:
        msgs = [m["message"] for m in fdb.moves_of(conn, "fa-orphans01")]
    finally:
        conn.close()
    assert msgs == ["purged: KVM host kvmfake is no longer declared", "purged: domain no longer on KVM host node2"]
    assert "STEP_EVENT|purge-0|done|host/kvm/kvmfake: entry purged" in err
    assert "STEP_EVENT|delete|done|folder /Old deleted (2 orphan entries purged)" in err


def test_an_orphan_the_person_does_not_see_is_counted_never_named(probe, capsys):
    """Un cluster retiré n'est plus dans la configuration : on ne sait pas si
    Rancher le cacherait à la personne, son nom ne sort pas (décision 93)."""
    fid = file_in_old(probe, [("harvester/removed/nav/x", "harvester/removed")])
    assert run(probe, "delete", "--id", str(fid), spec={"visible": [], "clusters": ["harv1"], "kubeconfigs": {}}) == 0
    cap = capsys.readouterr()
    assert "removed" not in cap.err + cap.out
    assert "1 other entry of objects that no longer exist purged" in cap.err


def test_a_hidden_cluster_is_never_read_and_never_with_the_console_kubeconfig(probe, capsys):
    fid = file_in_old(probe, [VM_GONE])
    probe["kube"].fail["gone"] = "gone"
    run(probe, "delete", "--id", str(fid), spec={"visible": [], "clusters": [], "kubeconfigs": {}})
    run(probe, "delete", "--id", str(fid), spec={"visible": [], "clusters": ["harv1"], "kubeconfigs": {}})
    assert probe["kube"].runs == []
    run(probe, "delete", "--id", str(fid), spec={"visible": [], "clusters": ["harv1"],
                                                  "kubeconfigs": {"harv1": "/staged/p.yaml"}})
    assert probe["kube"].runs == [("get", "virtualmachines.kubevirt.io", "gone", "-n", "nav", "-o", "name")]


def test_an_entry_moved_elsewhere_meanwhile_is_left(probe, capsys, monkeypatch):
    """Le retrait est conditionnel : rangée ailleurs entre le contrôle et le
    retrait, l'entrée reste (le dossier se supprime, il est vide)."""
    fid = file_in_old(probe, [("host/kvm/kvmfake", "hosts")])
    real = cli.classify_entry

    def moved(key, entry, *a):
        conn = fdb.connect(probe["db"])
        fdb.upsert_membership(conn, key, "/Lab", "console")
        conn.close()
        return real(key, entry, *a)
    monkeypatch.setattr(cli, "classify_entry", moved)
    assert run(probe, "delete", "--id", str(fid)) == 0
    assert base.members(probe)["host/kvm/kvmfake"] == ("/Lab", "console")
    assert json.loads(capsys.readouterr().out)["purged"] == 0


def test_subfolders_still_refuse_without_any_probe(probe, capsys):
    conn = fdb.connect(probe["db"])
    fdb.upsert_membership(conn, "host/kvm/kvmfake", "/Prod/ERP", "console", scope="hosts")
    conn.close()
    assert run(probe, "delete", "--id", "1") == 1
    assert "1 subfolder" in capsys.readouterr().err
    assert probe["kube"].runs == [] and probe["virsh"].calls == []
    assert "host/kvm/kvmfake" in base.members(probe)


# ---------------------------------------------------------------------------
# Console : ce qui part à l'outil, ce qui est refusé tout de suite
# ---------------------------------------------------------------------------
def _file(world, path, entries):
    conn = fdb.connect(world["db"])
    try:
        fdb.ensure_path(conn, path, "console")
        for key, scope in entries:
            fdb.upsert_membership(conn, key, path, "console", scope=scope)
        return fdb.id_of(conn, path)
    finally:
        conn.close()


@pytest.mark.parametrize("entries,code", [
    ([("host/kvm/kvmfake", "hosts")], 202),                          # hôte retiré : à l'outil
    ([("harvester/harv1/nav/deleted", "harvester/harv1")], 202),     # absente des lectures : à l'outil
    ([("harvester/removed/nav/x", "harvester/removed")], 202),       # cluster retiré : à l'outil
    ([("host/kvm/node2", "hosts")], 409),                            # hôte déclaré : objet vu
    ([("host/harvester/harv1", "hosts")], 409),                      # cluster vu
])
def test_the_console_sends_possible_orphans_to_the_tool(world, call, entries, code):
    f = _file(world, "/Orph", entries)
    r = call("alice", "DELETE", f"/api/folders/{f}")
    assert r.status_code == code, r.get_json()
    if code == 202:
        spec = rbase.script_spec()
        assert spec["objects"] == [] and spec["clusters"] == ["harv1", "lab"]
        assert spec["kubeconfigs"] == ({"harv1": rbase.KC} if "harv1" in entries[0][0] else {})
        cmd = rbase.script_cmd()
        assert cmd[cmd.index("delete"):cmd.index("delete") + 3] == ["delete", "--folder", str(f)]
        assert wapp_action(r)["action"] == f"folders:delete:/Orph"
    else:
        assert r.get_json()["nonempty"] is True and rbase._NoThread.started == []


def test_a_cluster_rancher_hides_refuses_at_once_and_is_never_named(world, call, monkeypatch):
    monkeypatch.setattr(rbase.wapp, "_visible_cluster_names", lambda: {"harv1"})
    f = _file(world, "/Orph", [("harvester/lab/nav/x", "harvester/lab")])
    r = call("alice", "DELETE", f"/api/folders/{f}")
    d = r.get_json()
    assert r.status_code == 409 and d["hidden"] is True and d["count_visible"] == 0
    assert "lab" not in json.dumps(d).replace("/Orph", "")


def test_mutation_without_the_console_guard_a_hidden_cluster_goes_to_the_tool(world, call, monkeypatch):
    """Mutation volontaire : si la console prenait tout cluster pour visible,
    l'entrée du cluster caché partirait à l'outil. Le test ci-dessus le voit."""
    monkeypatch.setattr(rbase.wapp, "_visible_cluster_names", lambda: {"harv1", "lab"})
    f = _file(world, "/Orph", [("harvester/lab/nav/x", "harvester/lab")])
    assert call("alice", "DELETE", f"/api/folders/{f}").status_code == 202


def wapp_action(r):
    return rbase.wapp.ACTIONS[r.get_json()["action_id"]].to_dict()
