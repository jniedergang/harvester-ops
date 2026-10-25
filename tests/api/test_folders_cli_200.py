"""2.0.0 (tâche 13) : la ligne de commande `bin/harvester-folders.py`.

`Kube` et l'hôte KVM sont remplacés par des leurres qui enregistrent les
appels : patch exact (clé, valeur, `null`), commande virsh exacte, refus du
webhook ou de la RBAC, hôte injoignable en cours de lot puis reprise,
annulation avec conflit, rôle, codes de sortie."""

import importlib.util
import json
import shlex
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import folders_db as fdb  # noqa: E402
import kvm_decl as kd  # noqa: E402
import kvm_host as kh  # noqa: E402
import kvm_ops as ko  # noqa: E402
from kube import KubeError  # noqa: E402

SPEC = importlib.util.spec_from_file_location("harvester_folders", ROOT / "bin" / "harvester-folders.py")
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)

UUID1 = "49960a5e-a239-42da-95f6-f55383ea5c62"
UUID2 = "6b128cd4-f440-482d-8e90-1eb32a2c2094"

# Refus vus en réel (kubectl 1.35, harv1) : RBAC d'un compte `view`
# (real3a/real13-harv1.txt) et webhook qui refuse sans code (403 par défaut)
RBAC_DENIAL = ('Error from server (Forbidden): virtualmachines.kubevirt.io "navtest-vm" is forbidden: '
               'User "system:serviceaccount:nav-folders-test:nav-viewer" cannot patch resource '
               '"virtualmachines" in API group "kubevirt.io" in the namespace "nav-folders-test"')
WEBHOOK_403 = ('Error from server (Forbidden): admission webhook "validator.harvesterhci.io" denied the '
               'request: the folder annotation is forbidden here')


class FakeKube:
    """Enregistre chaque patch ; `fail` {nom: "webhook" | "forbidden" | "down"}."""

    def __init__(self, fail=None):
        self.calls, self.fail = [], fail or {}

    def patch(self, kind, ns, name, patch, dry_run=False):
        self.calls.append((kind, ns, name, json.loads(json.dumps(patch)), dry_run))
        why = self.fail.get(name)
        if why == "webhook" and dry_run:
            raise KubeError('admission webhook "validator.harvesterhci.io" denied the request: no')
        if why == "forbidden":
            raise KubeError(RBAC_DENIAL)
        if why == "webhook403" and dry_run:
            raise KubeError(WEBHOOK_403)
        if why == "down":
            raise KubeError("Unable to connect to the server: dial tcp: i/o timeout")


DOMINFO = """Id:             {id}
Name:           {name}
UUID:           49960a5e-a239-42da-95f6-f55383ea5c62
OS Type:        hvm
State:          {state}
CPU(s):         1
Max memory:     65536 KiB
Used memory:    65536 KiB
Persistent:     {persistent}
Autostart:      disable
Managed save:   no
"""


class FakeVirsh:
    """Hôte KVM au niveau de `batch` : `domains` {nom: (état, persistant)} ;
    `down` : noms dont l'écriture trouve l'hôte injoignable."""

    def __init__(self, domains, down=()):
        self.domains, self.down, self.sets = domains, set(down), []
        self.name = "node2"

    def batch(self, commands, idempotent=False, **kw):
        out = {}
        for key, args in commands.items():
            name = args[args.index("--domain") + 1]
            if name in self.down:
                raise kh.KvmError("unreachable", "node2: unreachable: connection refused", host="node2")
            if args[0] == "dominfo":
                state, persistent = self.domains[name]
                out[key] = kh.CmdOutput(DOMINFO.format(id="-" if state == "shut off" else 3, name=name,
                                                       state=state, persistent="yes" if persistent else "no"))
            else:
                self.sets.append(tuple(args))
                out[key] = kh.CmdOutput("Metadata modified\n")
        return out


@pytest.fixture
def env(monkeypatch, tmp_path):
    state = tmp_path / "state"
    for n in ("node2", "kvm2"):
        kd.write(state, {"name": n, "address": "10.0.0.9", "user": "ju", "ssh_key": kd.key_rel(n),
                         "known_hosts": kd.known_hosts_rel(n), "labels": {"site": "cave"}})
    (state / "clusters.d").mkdir(parents=True)
    (state / "clusters.d" / "gpudemo.yaml").write_text(
        "name: gpudemo\ndescription: kwok bench\nkubeconfig: kubeconfigs/gpudemo.yaml\nnodes: []\n")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("clusters:\n- name: harv1\n  kubeconfig: /k/harv1.yaml\n")
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(state))
    monkeypatch.setenv("HARVESTER_OPS_CONFIG", str(cfg))
    kube = FakeKube()
    virsh = FakeVirsh({"vm-a": ("running", True), "vm-b": ("shut off", True), "vm-t": ("running", False),
                       "it's mine": ("shut off", True)})
    monkeypatch.setattr(cli, "make_kube", lambda cluster, spec: kube)
    monkeypatch.setattr(cli, "make_host", lambda name: virsh)
    monkeypatch.setattr(cli, "SLEEP", lambda s: None)
    db = tmp_path / "folders.db"
    conn = fdb.connect(db)
    for path in ("/Prod", "/Prod/ERP", "/Lab", "/Old"):
        fdb.ensure_path(conn, path, "console")
    conn.close()
    return {"state": state, "kube": kube, "virsh": virsh, "db": db, "tmp": tmp_path}


def run(env, *argv, spec=None, role=None, capsys=None):
    args = ["--db", str(env["db"]), "--json"]
    if role:
        args += ["--role", role]
    argv = list(argv)
    if spec is not None:
        f = env["tmp"] / "spec.json"
        f.write_text(json.dumps(spec))
        argv += ["--spec", str(f)]
    return cli.main(args + argv)


def vm(name, ns="nav", cluster="harv1", **kw):
    return dict({"kind": "harvester", "cluster": cluster, "namespace": ns, "name": name}, **kw)


def kvm(domain, uuid, host="node2", **kw):
    return dict({"kind": "kvm", "host": host, "domain": domain, "uuid": uuid}, **kw)


def members(env):
    conn = fdb.connect(env["db"])
    try:
        return {k: (v["folder_path"], v["source"]) for k, v in fdb.memberships(conn).items()}
    finally:
        conn.close()


def journal(env, action):
    conn = fdb.connect(env["db"])
    try:
        return [(m["object_key"], m["from_path"], m["to_path"], m["status"]) for m in fdb.moves_of(conn, action)]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Arborescence
# ---------------------------------------------------------------------------
def test_tree_create_rename_move_delete(env, capsys):
    assert run(env, "create", "--parent", "/", "--name", "Ops") == 0
    new = json.loads(capsys.readouterr().out)
    assert new["path"] == "/Ops"
    assert run(env, "rename", "--id", str(new["id"]), "--name", "Run", spec={"objects": []}) == 0
    assert run(env, "move-folder", "--id", str(new["id"]), "--parent", "1", spec={"objects": []}) == 0
    capsys.readouterr()
    assert run(env, "tree") == 0
    assert "/Prod/Run" in [f["path"] for f in json.loads(capsys.readouterr().out)["folders"]]
    assert run(env, "delete", "--id", str(new["id"])) == 0


@pytest.mark.parametrize("argv,code,message", [
    (["create", "--parent", "/", "--name", "a/b"], 2, "contains '/'"),
    (["create", "--parent", "/", "--name", "x" * 64], 2, "longer than 63"),
    (["create", "--parent", "/", "--name", "Prod"], 1, "already exists"),
    (["create", "--parent", "999", "--name", "N"], 1, "no folder 999"),
    (["create", "--parent", "abc", "--name", "N"], 2, "invalid folder id"),
    (["delete", "--id", "1"], 1, "move content to another folder first"),
    (["rename", "--id", "1", "--name", "Lab"], 1, "already exists"),
    (["move-folder", "--id", "1", "--parent", "2"], 2, "into itself"),
    (["assign", "--to", "Prod", "--spec", "/nonexistent"], 2, "start with '/'"),
    (["assign", "--to", "/Nope"], 1, "no folder /Nope"),
    (["resume", "--action", "nope"], 1, "no action nope"),
    (["undo", "--action", "bad id!"], 2, "invalid action"),
])
def test_refusals_and_exit_codes(env, capsys, argv, code, message):
    spec = {"objects": [vm("a")]} if argv[0] == "assign" and "--spec" not in argv else None
    assert run(env, *argv, spec=spec) == code
    err = capsys.readouterr().err
    assert message in err, err


@pytest.mark.parametrize("argv,code,kind", [
    (["create", "--parent", "/", "--name", "Prod"], 1, "conflict"),
    (["create", "--parent", "/", "--name", "a/b"], 2, "invalid"),
    (["create", "--parent", "999", "--name", "N"], 1, "refused"),
    (["assign", "--to", "Prod", "--spec", "/nonexistent"], 2, "usage"),
    (["undo", "--action", "bad id!"], 2, "usage"),
    (["no-such-command"], 2, "usage"),
])
def test_every_error_exit_prints_one_json_object(env, capsys, argv, code, kind):
    assert run(env, *argv) == code
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    assert out["ok"] is False and out["kind"] == kind and out["error"]
    assert "STEP_EVENT|" in cap.err and "|error|" in cap.err


def test_an_unexpected_error_hides_its_internals(env, capsys, monkeypatch):
    def broken(path=None):
        raise cli.fdb.sqlite3.OperationalError("unable to open database file /srv/secret/folders.db")
    monkeypatch.setattr(cli.fdb, "connect", broken)
    assert run(env, "tree") == 1
    cap = capsys.readouterr()
    assert json.loads(cap.out) == {"ok": False, "error": "unexpected internal error (OperationalError)",
                                   "kind": "failed"}
    assert "/srv/secret" not in cap.out + cap.err and "Traceback" not in cap.out + cap.err


def test_an_error_without_json_prints_no_json(env, capsys):
    assert cli.main(["--db", str(env["db"]), "create", "--parent", "/", "--name", "Prod"]) == 1
    cap = capsys.readouterr()
    assert cap.out == "" and "already exists" in cap.err


def test_delete_of_a_non_empty_folder_is_refused_with_code_1(env, capsys):
    assert run(env, "assign", "--to", "/Lab", spec={"objects": [vm("a")]}) == 0
    capsys.readouterr()
    assert run(env, "delete", "--id", "3", spec={"visible": []}) == 1
    err = capsys.readouterr().err
    assert "objects you cannot see" in err and "move content to another folder first" in err and "1 object" not in err


def test_a_viewer_reads_but_never_writes(env, capsys):
    assert run(env, "tree", role="viewer") == 0
    for argv in (["create", "--parent", "/", "--name", "X"], ["delete", "--id", "3"],
                 ["assign", "--to", "/Lab"]):
        spec = {"objects": [vm("a")]} if argv[0] == "assign" else None
        assert run(env, *argv, spec=spec, role="viewer") == 1
        assert "operator role is needed" in capsys.readouterr().err
    assert env["kube"].calls == []


@pytest.mark.parametrize("obj,message", [
    ({"kind": "xen", "name": "a"}, "unknown object kind"),
    (vm("a", key="harvester/harv1/other/a"), "does not match"),
    ({"kind": "container", "cluster": "harv1", "namespace": "n", "workload_kind": "Job", "name": "j"},
     "cannot be filed"),
    (kvm("vm-a", "not-a-uuid", host="../etc"), "invalid KVM host name"),
    ({"kind": "host", "family": "vmware", "name": "x"}, "unknown host family"),
])
def test_unknown_or_forged_objects_are_refused(env, capsys, obj, message):
    assert run(env, "assign", "--to", "/Lab", spec={"objects": [obj]}) == 2
    assert message in capsys.readouterr().err
    assert env["kube"].calls == [] and env["virsh"].sets == []


# ---------------------------------------------------------------------------
# Écritures sur les objets
# ---------------------------------------------------------------------------
def check_harvester_patches(env, capsys, annotation="harvester-ops.io/folder"):
    assert run(env, "--id=act-1", "assign", "--to", "/Prod/ERP",
               spec={"objects": [vm("a"), vm("pg", kind="container", workload_kind="StatefulSet")]}) == 0
    want = {"metadata": {"annotations": {annotation: "/Prod/ERP"}}}
    assert env["kube"].calls == [
        ("virtualmachines.kubevirt.io", "nav", "a", want, True),
        ("virtualmachines.kubevirt.io", "nav", "a", want, False),
        ("statefulsets.apps", "nav", "pg", want, True),
        ("statefulsets.apps", "nav", "pg", want, False)]
    env["kube"].calls.clear()
    assert run(env, "--id=act-2", "assign", "--to", "unfiled", spec={"objects": [vm("a")]}) == 0
    unset = {"metadata": {"annotations": {annotation: None}}}
    assert env["kube"].calls == [("virtualmachines.kubevirt.io", "nav", "a", unset, True),
                                 ("virtualmachines.kubevirt.io", "nav", "a", unset, False)]


def test_harvester_and_container_patches_are_exact(env, capsys):
    check_harvester_patches(env, capsys)
    assert members(env) == {"container/harv1/nav/statefulset/pg": ("/Prod/ERP", "vm")}
    assert journal(env, "act-1") == [("harvester/harv1/nav/a", None, "/Prod/ERP", "done"),
                                     ("container/harv1/nav/statefulset/pg", None, "/Prod/ERP", "done")]
    assert journal(env, "act-2") == [("harvester/harv1/nav/a", "/Prod/ERP", None, "done")]


def test_folders_cli_mutation_patch_key(env, capsys, monkeypatch):
    """Mutation volontaire : la clé d'annotation devient
    `harvester-ops.io/folders` dans le module -> les cas Harvester échouent."""
    monkeypatch.setattr(cli.fm, "ANNOTATION", "harvester-ops.io/folders")
    with pytest.raises(AssertionError):
        check_harvester_patches(env, capsys)


@pytest.mark.parametrize("wk,resource", [("Deployment", "deployments.apps"), ("DaemonSet", "daemonsets.apps"),
                                         ("CronJob", "cronjobs.batch")])
def test_every_rangeable_workload(env, wk, resource):
    assert run(env, "assign", "--to", "/Lab",
               spec={"objects": [vm("w", kind="container", workload_kind=wk)]}) == 0
    assert {c[0] for c in env["kube"].calls} == {resource}


def test_kvm_metadata_commands_are_exact(env):
    objs = [kvm("vm-a", UUID1), kvm("vm-b", UUID2)]
    assert run(env, "--id=k1", "assign", "--to", "/Lab", spec={"objects": objs}) == 0
    base = ("metadata", "--domain")
    tail = ("--uri", "https://harvester-ops.io/xmlns/folder/1", "--key", "hops", "--set", '<folder path="/Lab"/>')
    assert env["virsh"].sets == [base + ("vm-a",) + tail + ("--config", "--live"),
                                 base + ("vm-b",) + tail + ("--config",)]
    env["virsh"].sets.clear()
    assert run(env, "assign", "--to", "unfiled", spec={"objects": objs[:1]}) == 0
    assert env["virsh"].sets == [("metadata", "--domain", "vm-a", "--uri", "https://harvester-ops.io/xmlns/folder/1",
                                  "--remove", "--config", "--live")]
    assert members(env) == {f"kvm/{UUID2}": ("/Lab", "vm")}


def test_a_path_with_an_apostrophe_and_a_space_is_quoted_for_the_shell(env):
    conn = fdb.connect(env["db"])
    fdb.ensure_path(conn, "/Lab/It's a test", "console")
    conn.close()
    assert run(env, "assign", "--to", "/Lab/It's a test", spec={"objects": [kvm("it's mine", UUID1)]}) == 0
    args = env["virsh"].sets[0]
    assert args[args.index("--set") + 1] == '<folder path="/Lab/It&#x27;s a test"/>'
    # par le vrai `batch` : chaque argument passe par shlex.quote, le shell
    # distant rend exactement les mêmes mots
    h = kh.VirshHost({"name": "node2", "address": "10.0.0.9", "user": "ju", "ssh_key": "ssh/node2_id",
                      "known_hosts": "ssh/node2_known"}, str(env["state"]))
    script = h.batch_script([("set", args)], "@@tok", 20)
    line = [ln for ln in script.splitlines() if ln.startswith("_kvm_run 0 ")][0]
    assert shlex.quote("it's mine") in line and shlex.quote(args[args.index("--set") + 1]) in line
    assert shlex.split(line)[2:] == ["virsh", "-c", "qemu:///system", *args]


def test_a_transient_domain_is_written_live_and_kept_in_the_console(env, capsys):
    assert run(env, "assign", "--to", "/Lab", spec={"objects": [kvm("vm-t", UUID1)]}) == 0
    assert env["virsh"].sets[0][-1] == "--live" and "--config" not in env["virsh"].sets[0]
    assert members(env) == {f"kvm/{UUID1}": ("/Lab", "console")}
    assert "transient domain" in capsys.readouterr().err


def test_a_webhook_refusal_fails_its_line_and_the_others_go_on(env, capsys):
    env["kube"].fail = {"b": "webhook"}
    assert run(env, "--id=w1", "assign", "--to", "/Lab", spec={"objects": [vm("a"), vm("b"), vm("c")]}) == 1
    st = [j[3] for j in journal(env, "w1")]
    assert st == ["done", "failed", "done"]
    assert ("virtualmachines.kubevirt.io", "nav", "b", {"metadata": {"annotations": {
        "harvester-ops.io/folder": "/Lab"}}}, False) not in env["kube"].calls     # jamais écrite pour de vrai
    assert "denied the request" in capsys.readouterr().err


@pytest.mark.parametrize("message,rbac", [
    (RBAC_DENIAL, True),
    ('Error from server (Forbidden): deployments.apps "navtest-deploy" is forbidden: User "u" cannot patch '
     'resource "deployments" in API group "apps" in the namespace "n"', True),
    ('Error from server (Forbidden): statefulsets.apps "pg" is forbidden: User "u" cannot update resource '
     '"statefulsets" in API group "apps"', True),
    (WEBHOOK_403, False),
    ('Error from server (Forbidden): admission webhook "v.x.io" denied the request: User "u" cannot patch '
     'resource "virtualmachines"', False),
    ('Error from server (Forbidden): virtualmachines.kubevirt.io "a" is forbidden: exceeded quota: q, '
     'requested: count/virtualmachines.kubevirt.io=1', False),
    ('Error from server (BadRequest): admission webhook "validator.harvesterhci.io" denied the request: no', False),
    ("Unable to connect to the server: dial tcp: i/o timeout", False),
    ('Error from server (NotFound): virtualmachines.kubevirt.io "a" not found', False),
])
def test_only_an_rbac_denial_counts_as_no_permission(message, rbac):
    assert cli._denied(KubeError(message)) is rbac


def test_a_403_webhook_refusal_is_a_failed_line_not_a_console_filing(env, capsys):
    env["kube"].fail = {"b": "webhook403"}
    assert run(env, "--id=w4", "assign", "--to", "/Lab", spec={"objects": [vm("a"), vm("b"), vm("c")]}) == 1
    assert [j[3] for j in journal(env, "w4")] == ["done", "failed", "done"]
    assert "harvester/harv1/nav/b" not in members(env)
    err = capsys.readouterr().err
    assert "denied the request" in err and "kept in the console only" not in err


def test_rbac_denial_keeps_the_filing_in_the_console(env, capsys):
    env["kube"].fail = {"a": "forbidden"}
    assert run(env, "assign", "--to", "/Lab", spec={"objects": [vm("a"), vm("b", write=False)]}) == 0
    assert members(env) == {"harvester/harv1/nav/a": ("/Lab", "console"),
                            "harvester/harv1/nav/b": ("/Lab", "console")}
    assert [c[2] for c in env["kube"].calls] == ["a"]           # `write: false` : rien envoyé
    assert capsys.readouterr().err.count("kept in the console only") == 2


def test_a_console_filing_never_overrides_a_folder_carried_by_the_object(env, capsys):
    conn = fdb.connect(env["db"])
    fdb.upsert_membership(conn, "harvester/harv1/nav/a", "/Old", "vm")
    conn.close()
    assert run(env, "assign", "--to", "/Lab", spec={"objects": [vm("a", write=False)]}) == 1
    assert members(env)["harvester/harv1/nav/a"] == ("/Old", "vm")
    assert "keeps it" in capsys.readouterr().err


def test_hosts_get_only_their_folder_field(env, capsys):
    objs = [{"kind": "host", "family": "kvm", "name": "node2"},
            {"kind": "host", "family": "harvester", "name": "gpudemo"},
            {"kind": "host", "family": "harvester", "name": "harv1"}]
    assert run(env, "assign", "--to", "/Lab", spec={"objects": objs}) == 0
    decl = kd.load(env["state"], "node2")
    assert decl["folder"] == "/Lab" and decl["labels"] == {"site": "cave"}
    text = (env["state"] / "clusters.d" / "gpudemo.yaml").read_text()
    assert text == ("name: gpudemo\ndescription: kwok bench\nkubeconfig: kubeconfigs/gpudemo.yaml\nnodes: []\n"
                    'folder: "/Lab"\n')
    assert members(env) == {"host/kvm/node2": ("/Lab", "vm"), "host/harvester/gpudemo": ("/Lab", "vm"),
                            "host/harvester/harv1": ("/Lab", "console")}
    assert "config.yaml" in capsys.readouterr().err
    assert run(env, "assign", "--to", "unfiled", spec={"objects": objs[:2]}) == 0
    assert "folder" not in kd.load(env["state"], "node2")
    assert "folder" not in (env["state"] / "clusters.d" / "gpudemo.yaml").read_text()


@pytest.mark.parametrize("text,path,expected", [
    ("name: a\nnodes: []\n", "/X", 'name: a\nnodes: []\nfolder: "/X"\n'),
    ('name: a\nfolder: "/Old"\nnodes: []\n', "/X", 'name: a\nnodes: []\nfolder: "/X"\n'),
    ("name: a\nfolder: /a very long\n  folded value\nnodes: []\n", None, "name: a\nnodes: []\n"),
    ("name: a\nssh:\n  folder: keep\n", "/é", 'name: a\nssh:\n  folder: keep\nfolder: "/\\u00e9"\n'),
])
def test_yaml_folder_field_edit(text, path, expected):
    assert cli.set_yaml_folder(text, path) == expected


def test_decls_accept_a_valid_folder_and_refuse_an_invalid_one(env):
    import cluster_decl
    base = {"name": "node2", "address": "10.0.0.9", "user": "ju", "ssh_key": kd.key_rel("node2"),
            "known_hosts": kd.known_hosts_rel("node2")}
    assert kd.check_decl(dict(base, folder="/Lab")) is None
    assert "folder" in kd.check_decl(dict(base, folder="Lab"))
    assert cluster_decl.check_decl({"name": "a", "folder": "/A/B"}, "a.yaml") is None
    assert "deeper than 8" in cluster_decl.check_decl({"name": "a", "folder": "/" + "/".join("abcdefghi")}, "a.yaml")


# ---------------------------------------------------------------------------
# Renommage, reprise, annulation, write-back
# ---------------------------------------------------------------------------
def test_rename_rewrites_the_objects_then_undo_puts_everything_back(env, capsys):
    assert run(env, "assign", "--to", "/Prod/ERP", spec={"objects": [vm("a"), kvm("vm-b", UUID2)]}) == 0
    env["kube"].calls.clear()
    env["virsh"].sets.clear()
    spec = {"objects": [vm("a"), kvm("vm-b", UUID2)]}
    assert run(env, "--id=r1", "rename", "--id", "1", "--name", "Production", spec=spec) == 0
    assert members(env) == {"harvester/harv1/nav/a": ("/Production/ERP", "vm"),
                            f"kvm/{UUID2}": ("/Production/ERP", "vm")}
    assert journal(env, "r1")[0] == ("folder/1", "/Prod", "/Production", "done")
    assert env["kube"].calls[-1][3] == {"metadata": {"annotations": {"harvester-ops.io/folder": "/Production/ERP"}}}
    assert run(env, "--id=u1", "undo", "--action", "r1", spec=spec) == 0
    conn = fdb.connect(env["db"])
    assert fdb.path_of(conn, 1) == "/Prod"
    conn.close()
    assert members(env) == {"harvester/harv1/nav/a": ("/Prod/ERP", "vm"), f"kvm/{UUID2}": ("/Prod/ERP", "vm")}
    assert env["kube"].calls[-1][3] == {"metadata": {"annotations": {"harvester-ops.io/folder": "/Prod/ERP"}}}


def test_an_interrupted_rename_is_resumed_without_rewriting_done_lines(env, monkeypatch, capsys):
    objs = [vm("a"), vm("b"), vm("c")]
    assert run(env, "assign", "--to", "/Prod/ERP", spec={"objects": objs}) == 0
    real = cli.write_object
    seen = []

    def dies_on_second(obj, path, spec, ctx):
        seen.append(obj["name"])
        if len(seen) == 2:
            raise KeyboardInterrupt                 # console arrêtée en plein lot
        return real(obj, path, spec, ctx)
    monkeypatch.setattr(cli, "write_object", dies_on_second)
    with pytest.raises(KeyboardInterrupt):
        run(env, "--id=r2", "rename", "--id", "2", "--name", "Finance", spec={"objects": objs})
    assert [j[3] for j in journal(env, "r2")] == ["done", "done", "pending", "pending"]
    monkeypatch.setattr(cli, "write_object", real)
    env["kube"].calls.clear()
    assert run(env, "--id=r2b", "resume", "--action", "r2", spec={"objects": objs}) == 0
    assert [c[2] for c in env["kube"].calls if not c[4]] == ["b", "c"]       # `a` n'est pas réécrite
    assert [j[3] for j in journal(env, "r2")] == ["done"] * 4
    assert {v for v in members(env).values()} == {("/Prod/Finance", "vm")}


def test_an_unreachable_host_mid_batch_then_resume(env, capsys):
    env["virsh"].down = {"vm-b"}
    objs = [kvm("vm-a", UUID1), kvm("vm-b", UUID2)]
    assert run(env, "--id=h1", "assign", "--to", "/Lab", spec={"objects": objs}) == 1
    assert [j[3] for j in journal(env, "h1")] == ["done", "failed"]
    env["virsh"].down = set()
    env["virsh"].sets.clear()
    assert run(env, "--id=h2", "resume", "--action", "h1", spec={"objects": objs}) == 0
    assert [s[2] for s in env["virsh"].sets] == ["vm-b"]
    assert [j[3] for j in journal(env, "h1")] == ["done", "done"]


def test_undo_with_a_conflict_leaves_the_moved_object(env, capsys):
    objs = [vm("a"), vm("b")]
    assert run(env, "--id=m1", "assign", "--to", "/Lab", spec={"objects": objs}) == 0
    assert run(env, "--id=m2", "assign", "--to", "/Old", spec={"objects": objs[:1]}) == 0
    capsys.readouterr()
    assert run(env, "--id=u2", "undo", "--action", "m1", spec={"objects": objs}) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["conflicts"] == [{"key": "harvester/harv1/nav/a", "expected": "/Lab", "current": "/Old"}]
    assert members(env) == {"harvester/harv1/nav/a": ("/Old", "vm")}       # b revenue « non rangée »


def test_two_simultaneous_assigns_last_write_wins_with_a_coherent_journal(env):
    barrier = threading.Barrier(2)
    real = cli.write_object
    order = []
    lock = threading.Lock()

    def synced(obj, path, spec, ctx):
        barrier.wait()
        with lock:
            order.append(path)
            return real(obj, path, spec, ctx)
    cli.write_object, saved = synced, cli.write_object
    try:
        ts = [threading.Thread(target=run, args=(env, f"--id=c{i}", "assign", "--to", p),
                               kwargs={"spec": {"objects": [vm("a")]}}) for i, p in ((1, "/Lab"), (2, "/Old"))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    finally:
        cli.write_object = saved
    assert members(env)["harvester/harv1/nav/a"] == (order[-1], "vm")
    assert journal(env, "c1")[0][3] == journal(env, "c2")[0][3] == "done"


def test_write_back_writes_console_filings_the_person_may_now_write(env, capsys):
    assert run(env, "assign", "--to", "/Lab", spec={"objects": [vm("a", write=False), vm("b", write=False)]}) == 0
    assert env["kube"].calls == []
    assert run(env, "--id=wb", "write-back", "--folder", "3",
               spec={"objects": [vm("a"), vm("b", write=False)]}) == 0
    assert members(env) == {"harvester/harv1/nav/a": ("/Lab", "vm"), "harvester/harv1/nav/b": ("/Lab", "console")}
    assert [c[2] for c in env["kube"].calls] == ["a", "a"]


def test_action_id_from_the_console_at_the_end_of_the_line(env):
    """`_cli_action(id_arg=True)` ajoute `--id=<action>` en fin de ligne ; dans
    `rename`, `--id 1` (forme avec espace) reste le dossier."""
    args = cli.parse_args(["--db", "x", "rename", "--id", "1", "--name", "N", "--id=abc123"])
    assert (args.id, args.folder_id) == ("abc123", "1")


def test_a_transferred_vm_keeps_its_folder_annotation():
    """Transfert entre clusters (1.45.0) : sanitize_vm garde l'annotation de
    dossier (vérifié dans le code ; le transfert réel n'a pas été rejoué)."""
    import vm_transfer
    vm_obj = {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
              "metadata": {"name": "a", "namespace": "n", "uid": "u",
                           "annotations": {"harvester-ops.io/folder": "/Prod", "kubevirt.io/latest": "x"}},
              "spec": {"template": {"spec": {"domain": {"devices": {}}}}}}
    out = vm_transfer.sanitize_vm(vm_obj)
    assert out["metadata"]["annotations"] == {"harvester-ops.io/folder": "/Prod"}
