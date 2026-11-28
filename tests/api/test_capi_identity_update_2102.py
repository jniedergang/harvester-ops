"""2.10.2 (point 331 B) : la console met à jour les identités CAPHV qu'elle a
posées avant la 2.10.1.

Harvester v1.9.1 exige `get` sur l'image de chaque VM créée ; les identités
de la 2.10.0 n'ont que `list` et leurs nouvelles machines sont refusées. La
console relève les identités périmées (constat `identity-outdated`), montre
ce qu'elle changerait (dry-run du serveur) et ajoute la seule règle
manquante au Role du namespace de l'image : objet de l'image, jamais le nom
affiché ; jamais un Role qu'elle n'a pas posé ; idempotent ; patch qui porte
la version de l'objet lu.
"""

import argparse
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(ROOT / "tests" / "api"))
import capi_identity as ci  # noqa: E402
from test_capi_identity_250 import Kube, demand  # noqa: E402

V = "v0.10.1"
ROLE = ci.K_ROLE


@pytest.fixture(scope="module")
def cli():
    s = importlib.util.spec_from_file_location("harvester_capi_2102_ident", ROOT / "bin" / "harvester-capi.py")
    mod = importlib.util.module_from_spec(s)
    s.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Monde simulé : un cluster posé par la 2.10.0 (identité sans `get` image)
# ---------------------------------------------------------------------------

def old_docs(spec):
    """Les objets de l'identité tels que la 2.10.0 les posait : sans la
    règle `get` sur l'image (ajoutée en 2.10.1)."""
    out = []
    for d in ci.docs(spec, V):
        d = copy.deepcopy(d)
        if d["kind"] == "Role":
            d["rules"] = [r for r in d["rules"] if not (r["resources"] == ["virtualmachineimages"]
                                                         and "get" in r["verbs"])]
        out.append(d)
    return out


def capi_cluster(ns, name, images, overrides=None):
    vols = [{"volumeType": "image", "imageName": i, "volumeSize": "40Gi", "bootOrder": 1} for i in images]
    vols.append({"volumeType": "storageClass", "storageClass": "longhorn", "volumeSize": "10Gi"})
    topo = {"variables": [{"name": "volumes", "value": vols}, {"name": "cpuCount", "value": 2}]}
    if overrides:
        topo["workers"] = {"machineDeployments": [{"class": "default-worker", "variables": {"overrides": [
            {"name": "volumes", "value": [{"volumeType": "image", "imageName": o}]} for o in overrides]}}]}
    return {"apiVersion": "cluster.x-k8s.io/v1beta2", "kind": "Cluster",
            "metadata": {"namespace": ns, "name": name}, "spec": {"topology": topo}}


def image(ns, name, display):
    return {"metadata": {"namespace": ns, "name": name}, "spec": {"displayName": display},
            "status": {"progress": 100}}


PROVIDER = {"metadata": {"namespace": "caphv-system", "name": "harvester"},
            "spec": {"name": "harvester", "type": "infrastructure", "version": V},
            "status": {"installedVersion": V, "phase": "Ready"}}


class IK(Kube):
    """Faux kubectl de la mise à jour : patch de fusion avec contrôle de
    `resourceVersion` (Conflict), dry-run du serveur, cluster éteint."""

    def __init__(self, *a, down=False, race=0, refuse=None, **kw):
        super().__init__(*a, **kw)
        self.down, self.race, self.refuse = down, race, refuse
        self.patches = []

    def run(self, *args, input=None, timeout=None):
        if self.down:
            raise self.err("Unable to connect to the server")
        return ""

    def get(self, kind, ns, name, managed_fields=False):
        o = super().get(kind, ns, name)
        return copy.deepcopy(o)

    def list(self, kind, ns=None, selector=None, missing_ok=True):
        return copy.deepcopy(super().list(kind, ns, selector))

    def patch(self, kind, ns, name, patch, dry_run=False, secret=False):
        self.patches.append((kind, ns, name, copy.deepcopy(patch), dry_run))
        if self.refuse:
            raise self.err(self.refuse)
        o = self.objs[(kind, ns, name)]
        md = o.setdefault("metadata", {})
        if self.race:
            self.race -= 1
            md["resourceVersion"] = str(int(md.get("resourceVersion") or "1") + 1)
        if patch["metadata"]["resourceVersion"] != md.get("resourceVersion"):
            raise self.err(f'Operation cannot be fulfilled on roles "{name}": the object has been '
                           'modified; please apply your changes to the latest version (Conflict)')
        if dry_run:
            return
        o["rules"] = patch["rules"]
        md["resourceVersion"] = str(int(md.get("resourceVersion") or "1") + 1)

    err = None


@pytest.fixture
def world(cli):
    IK.err = cli.KubeError

    def make(clusters=(("web", "web", "default/image-abc"),), images=None, down=False, race=0, refuse=None,
             posed=None):
        k = IK(down=down, race=race, refuse=refuse)
        k.objs[("capiproviders.turtles-capi.cattle.io", "caphv-system", "harvester")] = PROVIDER
        for img in images or [image("default", "image-abc", "sles15-sp7.qcow2")]:
            k.objs[("virtualmachineimages.harvesterhci.io", img["metadata"]["namespace"],
                    img["metadata"]["name"])] = img
        for ns, name, img in clusters:
            k.objs[("clusters.cluster.x-k8s.io", ns, name)] = capi_cluster(ns, name, [img])
            docs = (posed or old_docs)(demand(name=name, namespace=ns, image=img
                                              if img.partition("/")[2].startswith("image-") else "default/image-abc"))
            for d in docs:
                d["metadata"]["resourceVersion"] = "1"
            k.apply(docs)
        k.calls.clear()
        return k
    return make


def args(**kw):
    a = dict(cluster=None, kubeconfig="/x", name="", dry_run=False, json=True, state_dir=None)
    a.update(kw)
    return argparse.Namespace(**a)


def image_rules(k, ns, role):
    return [r for r in k.objs[(ROLE, ns, role)]["rules"] if r["resources"] == ["virtualmachineimages"]]


def steps(capsys):
    return [ln.split("|", 3)[1:] for ln in capsys.readouterr().err.splitlines() if ln.startswith("STEP_EVENT|")]


# ---------------------------------------------------------------------------
# Bibliothèque (pure)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,cluster,images", [
    ("variable", capi_cluster("a", "b", ["default/image-abc"]), ["default/image-abc"]),
    ("worker override", capi_cluster("a", "b", ["default/image-abc"], overrides=["other/image-x"]),
     ["default/image-abc", "other/image-x"]),
    ("storage class volume only", {"spec": {"topology": {"variables": [{"name": "volumes", "value": [
        {"volumeType": "storageClass", "storageClass": "x"}]}]}}}, []),
    ("volumeType missing means image", {"spec": {"topology": {"variables": [{"name": "volumes", "value": [
        {"imageName": "default/image-q"}]}]}}}, ["default/image-q"]),
    ("no topology", {"spec": {}}, []),
    ("value not a list", {"spec": {"topology": {"variables": [{"name": "volumes", "value": "x"}]}}}, []),
])
def test_images_of_a_cluster(label, cluster, images):
    assert ci.cluster_images(cluster) == images, label


@pytest.mark.parametrize("label,rule,ok", [
    ("named get", {"apiGroups": ["harvesterhci.io"], "resources": ["virtualmachineimages"], "verbs": ["get"],
                   "resourceNames": ["image-abc"]}, True),
    ("get on every image", {"apiGroups": ["harvesterhci.io"], "resources": ["virtualmachineimages"],
                            "verbs": ["get", "list"]}, True),
    ("wildcards", {"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}, True),
    ("list only", {"apiGroups": ["harvesterhci.io"], "resources": ["virtualmachineimages"], "verbs": ["list"]}, False),
    ("another image", {"apiGroups": ["harvesterhci.io"], "resources": ["virtualmachineimages"], "verbs": ["get"],
                       "resourceNames": ["image-zzz"]}, False),
    ("another group", {"apiGroups": ["kubevirt.io"], "resources": ["virtualmachineimages"], "verbs": ["get"]}, False),
])
def test_what_grants_get_on_the_image(label, rule, ok):
    assert ci.grants_image_get({"rules": [rule]}, "image-abc") is ok, label


@pytest.mark.parametrize("ns,image_ns,role", [
    ("web", "web", "caphv-identity"),
    ("web", "default", "caphv-identity-web"),
    ("web", "web-vms", "caphv-identity-web"),
])
def test_the_role_of_the_image_namespace_follows_docs(ns, image_ns, role):
    assert ci.image_role_name(ns, image_ns) == role
    spec = demand(name="c", namespace=ns, image=f"{image_ns}/image-abc")
    names = {(d["metadata"]["namespace"], d["metadata"]["name"]) for d in ci.docs(spec, V) if d["kind"] == "Role"
             and any("virtualmachineimages" in r["resources"] for r in d["rules"])}
    assert names == {(image_ns, role)}


# ---------------------------------------------------------------------------
# Relevé (identity-check)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,setup,state,codes", [
    ("2.10.0 identity", {}, "outdated", ["identity-outdated"]),
    ("2.10.1 identity", {"posed": lambda s: ci.docs(s, V)}, "current", []),
    ("console identity (no account)", {"posed": lambda s: []}, "not-managed", []),
    ("account of someone else", {"posed": lambda s: [dict(d, metadata=dict(d["metadata"], labels={}))
                                                     for d in old_docs(s)]}, "not-managed", []),
    ("image deleted", {"images": [image("default", "image-zzz", "z")]}, "blocked", ["identity-image-unresolved"]),
    ("image role deleted", {"posed": lambda s: [d for d in old_docs(s) if not (
        d["kind"] == "Role" and d["metadata"]["namespace"] == "default")]}, "blocked", ["identity-role-missing"]),
    ("image role of someone else", {"posed": lambda s: [dict(d, metadata=dict(d["metadata"], labels={}))
                                                        if d["kind"] == "Role" and d["metadata"]["namespace"]
                                                        == "default" else d for d in old_docs(s)]},
     "blocked", ["identity-role-foreign"]),
])
def test_the_report_says_the_state_of_each_identity(cli, world, label, setup, state, codes):
    k = world(**setup)
    rep = cli.identity_report(k)
    (c,) = rep["clusters"]
    assert rep["caphv_version"] == V and c["cluster"] == "web/web", label
    assert c["state"] == state, (label, c)
    assert [f["code"] for f in c["findings"]] == codes, label
    assert not [x for x in k.calls if x[0] in ("apply", "delete")] and not k.patches, label   # lecture seule


def test_the_report_names_the_exact_change(cli, world):
    rep = cli.identity_report(world())
    (c,) = rep["clusters"]
    assert [(x["namespace"], x["role"], x["image"], x["rule"]) for x in c["changes"]] == [
        ("default", "caphv-identity-web", "image-abc",
         {"apiGroups": ["harvesterhci.io"], "resources": ["virtualmachineimages"], "verbs": ["get"],
          "resourceNames": ["image-abc"]})]
    f = c["findings"][0]
    assert f["level"] == "warn" and f["facts"] == {"cluster": "web/web", "roles": "default/caphv-identity-web",
                                                   "images": "image-abc"}


def test_a_display_name_is_brought_back_to_the_object(cli, world):
    k = world(clusters=(("web", "web", "default/sles15-sp7.qcow2"),))
    (c,) = cli.identity_report(k)["clusters"]
    assert [x["image"] for x in c["changes"]] == ["image-abc"]          # jamais le nom affiché


def test_a_display_name_shared_by_two_images_is_not_guessed(cli, world):
    k = world(clusters=(("web", "web", "default/sles15-sp7.qcow2"),),
              images=[image("default", "image-abc", "sles15-sp7.qcow2"), image("default", "image-twin", "sles15-sp7.qcow2")])
    (c,) = cli.identity_report(k)["clusters"]
    assert c["state"] == "blocked" and c["changes"] == []
    f = c["findings"][0]
    assert f["code"] == "identity-image-unresolved" and f["facts"]["candidates"] == "default/image-abc, default/image-twin"


def test_unknown_caphv_rules_block(cli, world):
    k = world()
    k.objs[("capiproviders.turtles-capi.cattle.io", "caphv-system", "harvester")] = dict(
        PROVIDER, spec=dict(PROVIDER["spec"], version="v0.11.0"), status=dict(PROVIDER["status"], installedVersion="v0.11.0"))
    (c,) = cli.identity_report(k)["clusters"]
    assert c["state"] == "blocked" and c["findings"][0]["code"] == "identity-rules-unknown"


def test_an_unreachable_cluster_is_said_not_guessed(cli, world):
    rep = cli.identity_report(world(down=True))
    assert rep == {"unreachable": True, "caphv_version": None, "clusters": []}


def test_several_clusters_and_one_selected(cli, world):
    k = world(clusters=(("web", "web", "default/image-abc"), ("api", "api", "default/image-abc")))
    rep = cli.identity_report(k)
    assert sorted((c["cluster"], c["state"]) for c in rep["clusters"]) == [("api/api", "outdated"),
                                                                          ("web/web", "outdated")]
    only = cli.identity_report(k, ("api", "api"))
    assert [c["cluster"] for c in only["clusters"]] == ["api/api"]


def test_check_cli_prints_json(cli, world, monkeypatch, capsys):
    k = world()
    monkeypatch.setattr(cli, "kube_from", lambda a: (k, "/x", None))
    assert cli.main(["identity-check", "--kubeconfig", "/x", "--json"]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["clusters"][0]["state"] == "outdated"


# ---------------------------------------------------------------------------
# Mise à jour (identity-update)
# ---------------------------------------------------------------------------

def test_the_update_adds_only_the_missing_rule_and_is_idempotent(cli, world, monkeypatch, capsys):
    k = world()
    monkeypatch.setattr(cli, "kube_from", lambda a: (k, "/x", None))
    monkeypatch.setattr(cli, "_refuse_during_uninstall", lambda a, sid: False)
    before = copy.deepcopy(k.objs[(ROLE, "default", "caphv-identity-web")]["rules"])
    assert cli.cmd_identity_update(args()) == cli.EXIT_OK
    after = k.objs[(ROLE, "default", "caphv-identity-web")]["rules"]
    assert after[:len(before)] == before and len(after) == len(before) + 1     # rien retiré, rien d'autre
    assert after[-1] == {"apiGroups": ["harvesterhci.io"], "resources": ["virtualmachineimages"],
                         "verbs": ["get"], "resourceNames": ["image-abc"]}
    out = steps(capsys)
    assert ["identity-web/web", "done", "added get on harvesterhci.io/virtualmachineimages image-abc to Role "
            "default/caphv-identity-web"] in out and out[-1][1] == "done"
    # seconde passe : rien à faire
    n = len(k.patches)
    assert cli.cmd_identity_update(args()) == cli.EXIT_OK
    assert len(k.patches) == n and ["identity-web/web", "done", "identity up to date"] in steps(capsys)
    assert cli.identity_report(k)["clusters"][0]["state"] == "current"
    # les autres Roles de l'identité n'ont pas bougé
    assert {(p[1], p[2]) for p in k.patches} == {("default", "caphv-identity-web")}


def test_the_dry_run_goes_to_the_server_and_writes_nothing(cli, world, monkeypatch, capsys):
    k = world()
    monkeypatch.setattr(cli, "kube_from", lambda a: (k, "/x", None))
    before = copy.deepcopy(k.objs)
    assert cli.cmd_identity_update(args(dry_run=True)) == cli.EXIT_OK
    assert k.objs == before
    assert [p[4] for p in k.patches] == [True]                                 # dry-run du serveur
    assert any("would add get on harvesterhci.io/virtualmachineimages image-abc to Role default/caphv-identity-web"
               in s[2] for s in steps(capsys))


def test_one_cluster_only(cli, world, monkeypatch):
    k = world(clusters=(("web", "web", "default/image-abc"), ("api", "api", "default/image-abc")))
    monkeypatch.setattr(cli, "kube_from", lambda a: (k, "/x", None))
    monkeypatch.setattr(cli, "_refuse_during_uninstall", lambda a, sid: False)
    assert cli.cmd_identity_update(args(name="api/api")) == cli.EXIT_OK
    assert {(p[1], p[2]) for p in k.patches} == {("default", "caphv-identity-api")}
    assert len(image_rules(k, "default", "caphv-identity-web")) == 1            # web non touché


def test_a_role_changed_meanwhile_is_read_again(cli, world, monkeypatch):
    k = world(race=1)
    monkeypatch.setattr(cli, "kube_from", lambda a: (k, "/x", None))
    monkeypatch.setattr(cli, "_refuse_during_uninstall", lambda a, sid: False)
    assert cli.cmd_identity_update(args()) == cli.EXIT_OK
    assert len(k.patches) == 2 and len(image_rules(k, "default", "caphv-identity-web")) == 2


@pytest.mark.parametrize("label,setup,kw,rc,last", [
    ("image role of someone else", {"posed": lambda s: [dict(d, metadata=dict(d["metadata"], labels={}))
                                                        if d["kind"] == "Role" and d["metadata"]["namespace"]
                                                        == "default" else d for d in old_docs(s)]}, {},
     "EXIT_BLOCKED", "updated 0 rule(s); 0 failed; 1 need a manual fix"),
    ("image deleted", {"images": [image("default", "image-zzz", "z")]}, {},
     "EXIT_BLOCKED", "updated 0 rule(s); 0 failed; 1 need a manual fix"),
    ("escalation refused by the server", {"refuse": 'roles.rbac.authorization.k8s.io "caphv-identity-web" is '
                                                    'forbidden: user "u" is attempting to grant RBAC permissions '
                                                    'not currently held'}, {},
     "EXIT_FAIL", "updated 0 rule(s); 1 failed; 0 need a manual fix"),
    ("unknown cluster", {}, {"name": "nope/nope"}, "EXIT_FAIL", "cluster nope/nope not found"),
    ("unreachable", {"down": True}, {}, "EXIT_FAIL", "the Harvester cluster does not answer"),
])
def test_refusals(cli, world, monkeypatch, capsys, label, setup, kw, rc, last):
    k = world(**setup)
    monkeypatch.setattr(cli, "kube_from", lambda a: (k, "/x", None))
    monkeypatch.setattr(cli, "_refuse_during_uninstall", lambda a, sid: False)
    assert cli.cmd_identity_update(args(**kw)) == getattr(cli, rc), label
    out = steps(capsys)
    assert out[-1][2] == last, (label, out)
    if label != "escalation refused by the server":
        assert not k.patches, label
    assert not [x for x in k.calls if x[0] in ("apply", "delete")], label


def test_an_identity_the_console_did_not_create_is_left_alone(cli, world, monkeypatch, capsys):
    k = world(posed=lambda s: [dict(d, metadata=dict(d["metadata"], labels={})) for d in old_docs(s)])
    monkeypatch.setattr(cli, "kube_from", lambda a: (k, "/x", None))
    monkeypatch.setattr(cli, "_refuse_during_uninstall", lambda a, sid: False)
    assert cli.cmd_identity_update(args(name="web/web")) == cli.EXIT_OK
    assert not k.patches
    assert ["identity-web/web", "done", "identity not created by the console: left alone"] in steps(capsys)


def test_refused_during_an_uninstall(cli, world, monkeypatch):
    k = world()
    monkeypatch.setattr(cli, "kube_from", lambda a: (k, "/x", None))
    monkeypatch.setattr(cli, "_refuse_during_uninstall", lambda a, sid: True)
    assert cli.cmd_identity_update(args()) == cli.EXIT_BLOCKED and not k.patches


def test_the_updated_identity_is_the_one_2101_creates(cli, world, monkeypatch):
    """La règle ajoutée est celle que la 2.10.1 pose à la création : mêmes
    Roles, mêmes règles (ordre mis à part)."""
    k = world()
    monkeypatch.setattr(cli, "kube_from", lambda a: (k, "/x", None))
    monkeypatch.setattr(cli, "_refuse_during_uninstall", lambda a, sid: False)
    cli.cmd_identity_update(args())
    want = {(d["metadata"]["namespace"], d["metadata"]["name"]): d["rules"]
            for d in ci.docs(demand(), V) if d["kind"] == "Role"}
    for key, rules in want.items():
        got = k.objs[(ROLE, *key)]["rules"]
        assert sorted(map(json.dumps, got)) == sorted(map(json.dumps, rules)), key


# ---------------------------------------------------------------------------
# Console : routes et contrôle au démarrage
# ---------------------------------------------------------------------------

@pytest.fixture
def wapp(monkeypatch):
    sys.path.insert(0, str(ROOT / "web"))
    import app as w
    w.app.config["TESTING"] = True
    calls = []
    monkeypatch.setattr(w, "_capi_base", lambda cl, sub: (["harvester-capi", sub, "--cluster", cl], None))
    monkeypatch.setattr(w, "_capi_action", lambda cl, label, cmd, **kw: calls.append((label, cmd, kw))
                        or (type("R", (), {"id": "abc123"})(), None))
    w._test_calls = calls
    return w


def test_the_update_route_is_admin_only(wapp):
    assert wapp.required_role_for("/api/capi/h1/identity-update", "POST") == "admin"
    assert wapp.required_role_for("/api/capi/h1/identities", "GET") != "admin"


@pytest.mark.parametrize("label,body,code,label_cmd", [
    ("all clusters", {}, 201, ("capi-identity-update:h1", ["harvester-capi", "identity-update", "--cluster", "h1"],
                               {"dry_run": False})),
    ("one cluster", {"name": "web/web"}, 201,
     ("capi-identity-update:web/web", ["harvester-capi", "identity-update", "--cluster", "h1", "--name", "web/web"],
      {"dry_run": False})),
    ("dry run", {"name": "web/web", "dry_run": True}, 201,
     ("capi-identity-update:web/web", ["harvester-capi", "identity-update", "--cluster", "h1", "--name", "web/web",
                                       "--dry-run"], {"dry_run": True})),
    ("bad name", {"name": "web"}, 400, None),
    ("name with a slash too many", {"name": "a/b/c"}, 400, None),
])
def test_the_update_route_starts_a_tracked_action(wapp, label, body, code, label_cmd):
    r = wapp.app.test_client().post("/api/capi/h1/identity-update", json=body)
    assert r.status_code == code, (label, r.get_json())
    if label_cmd:
        assert wapp._test_calls == [label_cmd], label
        assert r.get_json()["action_id"] == "abc123"
    else:
        assert wapp._test_calls == [] and r.get_json()["code"] == "invalid-name", label


@pytest.mark.parametrize("query,argv,code", [
    ("", ["harvester-capi", "identity-check", "--cluster", "h1", "--json"], 200),
    ("?name=web/web", ["harvester-capi", "identity-check", "--cluster", "h1", "--json", "--name", "web/web"], 200),
    ("?name=bad", None, 400),
])
def test_the_report_route_reads_only(wapp, monkeypatch, query, argv, code):
    seen = []
    monkeypatch.setattr(wapp, "_capi_run_json", lambda cmd, spec=None, timeout=180: seen.append(cmd)
                        or (({"unreachable": False, "clusters": []}, 0), None))
    monkeypatch.setitem(wapp.CAPI_IDENTITY_STARTUP, "h1", {"outdated": ["web/web"]})
    r = wapp.app.test_client().get(f"/api/capi/h1/identities{query}")
    assert r.status_code == code
    if argv:
        assert seen == [argv] and r.get_json()["startup"] == {"outdated": ["web/web"]}
    assert wapp._test_calls == []                       # aucune action


def test_the_startup_check_reports_and_never_updates(wapp, monkeypatch, caplog):
    rep = {"unreachable": False, "clusters": [
        {"cluster": "web/web", "state": "outdated"}, {"cluster": "api/api", "state": "current"},
        {"cluster": "x/x", "state": "blocked"}]}
    ran = []

    class P:
        def __init__(self, out):
            self.stdout, self.returncode = out, 0

    def fake_run(cmd, **kw):
        ran.append(cmd)
        if "--cluster=off" in " ".join(cmd) or cmd[-2:] == ["--harvester-name=off", "--json"]:
            return P(json.dumps({"unreachable": True, "caphv_version": None, "clusters": []}))
        return P(json.dumps(rep))
    monkeypatch.setattr(wapp, "_capi_script", lambda: Path("/bin/harvester-capi.py"))
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {"clusters": [{"name": "h1"}, {"name": "off"}, {"name": "nokc"}]})
    monkeypatch.setattr(wapp, "_cluster_kubeconfig_path", lambda c, purpose="", missing_ok=False:
                        None if c["name"] == "nokc" else f"/kc/{c['name']}")
    monkeypatch.setattr(wapp.subprocess, "run", fake_run)
    monkeypatch.setattr(wapp, "CAPI_IDENTITY_STARTUP", {})
    with caplog.at_level("WARNING", logger="harvester-ops"):
        wapp._capi_identity_startup_check(sleep=lambda s: None)
    assert [c[2] for c in ran] == ["identity-check", "identity-check"]          # jamais identity-update
    assert all("identity-update" not in c for c in ran)
    got = wapp.CAPI_IDENTITY_STARTUP
    assert got["h1"]["outdated"] == ["web/web"] and got["h1"]["blocked"] == ["x/x"]
    assert got["off"]["unreachable"] is True and got["off"]["outdated"] == []
    assert "nokc" not in got
    text = caplog.text
    assert "h1: CAPHV identities created before 2.10.1 lack get on their image" in text and "web/web" in text
    assert "off:" not in text
    assert wapp._test_calls == []
