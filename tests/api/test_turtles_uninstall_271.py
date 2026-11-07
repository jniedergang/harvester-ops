"""2.7.1 : sûreté du retrait de la pile Turtles et restes de la 2.7.0.

Ce que ces tests tiennent (revue finale 2.7.0, m1 et m3 ii/iii) :

- m1 : une phase lancée derrière l'échec toléré d'une phase précédente
  (Artefacts derrière Clusters `partial`) qui échoue à son tour laisse le
  déroulé `partial` : des clusters ont été créés ;
- m3 ii : `--remove-images` ne vise que les images que la console a
  chargées elle-même pour la pile (annotation posée par `install
  --push-images`), et refuse, liste à l'appui, si une autre charge de
  travail les emploie ; rien n'est alors supprimé ;
- m3 iii : le retrait tient le verrou du cluster de bout en bout ; une
  création, une suppression ou un déroulé lancés pendant ce temps sont
  refusés (outil et console).
"""

import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(HERE))
import capi_stack as cs  # noqa: E402
import node_images  # noqa: E402
from test_capi_uninstall_turtles_270 import FakeKube, ORDER, PROVIDERS, steps, provider_deletions  # noqa: E402
from test_capi_stack import FakeKube as InstallKube, desc  # noqa: E402
from test_deploy_chain_240 import Ctx, dc, run  # noqa: E402
from test_deploy_chain_270 import items_phase  # noqa: E402

IMG = {key: f"reg.example/{key}:v1" for key in PROVIDERS}


# ---------------------------------------------------------------------------
# m1 : Artefacts en échec derrière une phase Clusters partielle
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("artefacts,expect", [
    ({"x": "error"}, "partial"),                     # m1 : des clusters existent
    ({"x": "done"}, "partial"),
    ({"x": "error", "y": "done"}, "partial"),
])
def test_an_error_behind_a_partial_phase_keeps_the_run_partial(artefacts, expect):
    ctx = Ctx()
    out = run(ctx, [items_phase(ctx, "clusters", {"a": "done", "b": "error"}, key="cluster"),
                    items_phase(ctx, "artefacts", artefacts, after_failure_of=("clusters",))])
    assert out["status"] == expect and out["failed_phase"] == "clusters"


def test_an_error_behind_a_partial_phase_still_stops_the_phases_after_it():
    ctx = Ctx()
    out = run(ctx, [items_phase(ctx, "clusters", {"a": "done", "b": "error"}, key="cluster"),
                    items_phase(ctx, "artefacts", {"x": "error"}, after_failure_of=("clusters",)),
                    items_phase(ctx, "later", {"z": "done"})],
              order=("clusters", "artefacts", "later"))
    assert out["status"] == "partial" and out["phases"][2]["status"] == "skipped"
    assert not any(c.startswith("later") for c in ctx.calls)


def test_a_cancel_behind_a_partial_phase_is_still_a_cancel():
    ctx = Ctx(cancel_after=3)
    out = run(ctx, [items_phase(ctx, "clusters", {"a": "done", "b": "error"}, key="cluster"),
                    items_phase(ctx, "artefacts", {"x": "done", "y": "done"}, after_failure_of=("clusters",))])
    assert out["status"] == "cancelled"


# ---------------------------------------------------------------------------
# m3 ii : les images chargées par la console, et elles seules
# ---------------------------------------------------------------------------
def test_install_records_the_images_it_loaded(tmp_path):
    d = desc(tmp_path)
    for p in d["providers"]:
        p["images"] = [f"ghcr.io/x/{p['key']}:{p['version']}"]
        p["image_files"] = [f"/b/{p['key']}.tar.gz"]
    kube = InstallKube(traefik_ip="172.16.3.100")
    cs.install(kube, d, steps()[1], sleep=lambda s: None, pushed=True)
    for p in d["providers"]:
        o = kube.get(cs.K_CAPIPROVIDER, p["namespace"], p["key"])
        assert cs.loaded_images(o) == [f"ghcr.io/x/{p['key']}:{p['version']}"]
    # relancée sans pousser : l'inscription reste (l'application côté serveur
    # retirerait une annotation absente du manifeste)
    cs.install(kube, d, steps()[1], sleep=lambda s: None)
    o = kube.get(cs.K_CAPIPROVIDER, "caphv-system", "harvester")
    assert cs.loaded_images(o) == ["ghcr.io/x/harvester:v0.10.1"]


def test_install_without_push_records_nothing(tmp_path):
    d = desc(tmp_path)
    for p in d["providers"]:
        p["images"] = ["ghcr.io/x/y:v1"]
    kube = InstallKube(traefik_ip="172.16.3.100")
    cs.install(kube, d, steps()[1], sleep=lambda s: None)
    assert all(cs.loaded_images(kube.get(cs.K_CAPIPROVIDER, p["namespace"], p["key"])) == []
               for p in d["providers"])


def test_only_the_recorded_images_are_candidates():
    k = FakeKube()
    # une autre image dans un namespace de fournisseur, non chargée par la console
    k.put("deployments", "caphv-system", "side", {"spec": {"template": {"spec": {
        "containers": [{"image": "quay.io/other/side:v2"}]}}}})
    plan = cs.uninstall_plan(k)
    assert sorted(plan["images"]) == sorted(IMG.values())
    assert "quay.io/other/side:v2" not in plan["images"]


def test_providers_installed_before_271_have_no_candidate():
    k = FakeKube()
    for key in PROVIDERS:
        ns = PROVIDERS[key][0]
        o = k.objs[(cs.K_CAPIPROVIDER, ns, key)]
        o["metadata"].pop("annotations", None)
    assert cs.uninstall_plan(k, remove_images=True)["images"] == []


def _pod(k, ns, name, image, labels=None, init=False):
    spec = {("initContainers" if init else "containers"): [{"image": image}]}
    k.put("pods", ns, name, {"metadata": {"labels": labels or {}}, "spec": spec})


@pytest.mark.parametrize("setup,used_by", [
    (lambda k: _pod(k, "web", "app", IMG["harvester"]), "pod web/app"),
    (lambda k: _pod(k, "web", "app", IMG["helm"], init=True), "pod web/app"),
    # dans le namespace du fournisseur, mais pas l'une de ses pods
    (lambda k: _pod(k, "caphv-system", "debug", IMG["harvester"]), "pod caphv-system/debug"),
    (lambda k: k.put("deployments", "web", "idle", {"spec": {"replicas": 0, "template": {"spec": {
        "containers": [{"image": IMG["rke2-bootstrap"]}]}}}}), "deployment web/idle"),
    (lambda k: k.put("cronjobs", "web", "nightly", {"spec": {"jobTemplate": {"spec": {"template": {"spec": {
        "containers": [{"image": IMG["rke2-control-plane"]}]}}}}}}), "cronjob web/nightly"),
    (lambda k: k.put("daemonsets", "kube-system", "agent", {"spec": {"template": {"spec": {
        "containers": [{"image": IMG["harvester"]}]}}}}), "daemonset kube-system/agent"),
])
def test_an_image_used_elsewhere_refuses_and_deletes_nothing(setup, used_by):
    k = FakeKube()
    setup(k)
    seen, step = steps()
    plan = cs.uninstall(k, step, remove_images=True)
    assert plan["blocked"] and [f["code"] for f in plan["refusals"]] == ["capi-images-in-use"]
    assert used_by in plan["refusals"][0]["facts"]["used_by"]
    assert k.deleted == []
    assert any(used_by in u for x in plan["images_in_use"] for u in x["used_by"])


def test_the_providers_own_pods_do_not_count():
    k = FakeKube()
    for key in PROVIDERS:
        ns = PROVIDERS[key][0]
        _pod(k, ns, f"{key}-abc", IMG[key], labels={cs.PROVIDER_LABEL: key})
    plan = cs.uninstall(k, steps()[1], remove_images=True, sleep=lambda s: None)
    assert not plan["blocked"] and plan["images_in_use"] == []
    assert provider_deletions(k) == ORDER


def test_an_image_in_use_without_remove_images_is_reported_not_refused():
    k = FakeKube()
    _pod(k, "web", "app", IMG["harvester"])
    plan = cs.uninstall_plan(k)
    assert not plan["blocked"]
    assert plan["images_in_use"] == [{"image": IMG["harvester"], "used_by": ["pod web/app"]}]


@pytest.mark.parametrize("a,b,same", [
    ("nginx", "docker.io/library/nginx:latest", True),
    ("docker.io/rancher/x:v1", "rancher/x:v1", True),
    ("ghcr.io/a/b:v1", "ghcr.io/a/b:v2", False),
    ("ghcr.io/a/b@sha256:" + "0" * 64, "ghcr.io/a/b@sha256:" + "0" * 64, True),
])
def test_image_references_are_compared_normalised(a, b, same):
    assert (cs.normalise_image(a) == cs.normalise_image(b)) is same


# ---------------------------------------------------------------------------
# La ligne de commande
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("harvester_capi_271", ROOT / "bin" / "harvester-capi.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_cli(cli, monkeypatch, capsys, kube, argv, entry=None, sub="uninstall"):
    monkeypatch.setattr(cli, "kube_from", lambda args: (kube, "kc", entry))
    rc = cli.main([sub, "--kubeconfig", "kc"] + argv)
    return rc, capsys.readouterr()


ENTRY = {"nodes": [{"ip": "172.16.3.11"}], "ssh": {"user": "rancher"}}


def test_cli_remove_images_refuses_an_image_in_use(cli, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(node_images, "remove_on_nodes", lambda entry, names, step, **kw: calls.append(names))
    k = FakeKube()
    _pod(k, "web", "app", IMG["helm"])
    rc, out = run_cli(cli, monkeypatch, capsys, k, ["--remove-images", "--json"], entry=ENTRY)
    assert rc == cli.EXIT_BLOCKED and calls == [] and k.deleted == []
    assert "capi-images-in-use" in out.err and "pod web/app" in out.err


def test_cli_remove_images_with_nothing_recorded_removes_nothing(cli, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(node_images, "remove_on_nodes", lambda entry, names, step, **kw: calls.append(names))
    k = FakeKube()
    for key in PROVIDERS:
        k.objs[(cs.K_CAPIPROVIDER, PROVIDERS[key][0], key)]["metadata"].pop("annotations", None)
    monkeypatch.setattr(cs.time, "sleep", lambda s: None)
    rc, out = run_cli(cli, monkeypatch, capsys, k, ["--remove-images"], entry=ENTRY)
    assert rc == cli.EXIT_OK and calls == [] and provider_deletions(k) == ORDER
    assert "STEP_EVENT|images|done|no image loaded by the console" in out.err


def test_cli_holds_the_cluster_lock_for_the_whole_removal(cli, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(tmp_path))
    seen = []
    real = cs.uninstall

    def spy(*a, **kw):
        seen.append(cli.deploy_holder("harvlab"))
        return real(*a, sleep=lambda s: None, **kw)
    monkeypatch.setattr(cs, "uninstall", spy)
    removing = []
    monkeypatch.setattr(node_images, "remove_on_nodes",
                        lambda entry, names, step, **kw: removing.append(cli.deploy_holder("harvlab")))
    rc, _ = run_cli(cli, monkeypatch, capsys, FakeKube(),
                    ["--harvester-name", "harvlab", "--id=abcabcabcabc", "--remove-images"], entry=ENTRY)
    assert rc == cli.EXIT_OK
    assert seen == [{"run": "abcabcabcabc", "plan": "", "action": "capi-uninstall"}]
    assert removing == seen                                   # images comprises
    assert cli.deploy_holder("harvlab") is None               # rendu à la fin


def test_cli_dry_run_does_not_take_the_lock(cli, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(tmp_path))
    seen = []
    real = cs.uninstall
    monkeypatch.setattr(cs, "uninstall", lambda *a, **kw: seen.append(cli.deploy_holder("harvlab")) or real(*a, **kw))
    rc, _ = run_cli(cli, monkeypatch, capsys, FakeKube(), ["--harvester-name", "harvlab", "--dry-run"])
    assert rc == cli.EXIT_OK and seen == [None]


def _hold(tmp_path, note):
    import fcntl
    d = tmp_path / "deploy.d"
    d.mkdir(exist_ok=True)
    lock = d / "harvlab.lock"
    lock.write_text(json.dumps(note) + "\n")
    fd = os.open(lock, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


@pytest.mark.parametrize("sub,argv", [
    ("create", ["--spec", "-"]),
    ("delete", ["--name", "web/web"]),
    ("install", ["--components", "/nonexistent"]),
])
def test_cli_gestures_refuse_while_an_uninstall_holds_the_cluster(cli, monkeypatch, capsys, tmp_path, sub, argv):
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(tmp_path))
    fd = _hold(tmp_path, {"run": "u1u1u1u1u1u1", "plan": "", "action": "capi-uninstall"})
    try:
        k = FakeKube()
        rc, out = run_cli(cli, monkeypatch, capsys, k, ["--harvester-name", "harvlab"] + argv, sub=sub)
    finally:
        os.close(fd)
    assert rc == cli.EXIT_BLOCKED and k.deleted == []
    assert "capi-uninstall-running" in out.err and "u1u1u1u1u1u1" in out.err


def test_cli_delete_under_a_plan_lock_is_not_refused_by_this_rule(cli, monkeypatch, capsys, tmp_path):
    """Un déroulé tient le verrou et lance lui-même les suppressions : seule
    la marque du retrait refuse."""
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(tmp_path))
    fd = _hold(tmp_path, {"run": "r1r1r1r1r1r1", "plan": "site-a"})
    try:
        assert cli.uninstall_refusal("harvlab") is None
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# La console
# ---------------------------------------------------------------------------
@pytest.fixture()
def wapp(monkeypatch):
    import app as w
    w.app.config["TESTING"] = True
    added = []

    def add(action, cluster="harvlab", dry=False, status="running"):
        r = types.SimpleNamespace(id=f"{len(added):012x}", action=action, cluster=cluster, dry_run=dry,
                                  status=status)
        w.ACTIONS[r.id] = r
        added.append(r.id)
        return r
    yield w, add
    for i in added:
        w.ACTIONS.pop(i, None)


@pytest.mark.parametrize("action,cluster,dry,refused", [
    ("capi-uninstall:harvlab", "harvlab", False, True),
    ("capi-uninstall:harvlab", "harvlab", True, False),          # un essai ne retire rien
    ("capi-uninstall:harvlab2", "harvlab2", False, False),       # autre cluster
])
def test_console_capi_gestures_refuse_during_an_uninstall(wapp, monkeypatch, action, cluster, dry, refused):
    w, add = wapp
    add(action, cluster, dry)
    launched = []
    monkeypatch.setattr(w, "_vm_transfer_runner", lambda run, cmd, **kw: launched.append(cmd))
    with w.app.test_request_context():
        monkeypatch.setattr(w, "current_cluster_identity", lambda: None)
        run_, err = w._capi_action("harvlab", "capi-cluster-create:web/web", ["py", "x", "create"])
    if refused:
        assert run_ is None and err[1] == 409 and err[0].get_json()["code"] == "capi-uninstall-running"
    else:
        assert run_ is not None and err is None
        w.ACTIONS.pop(run_.id, None)


def _admin_console(w, monkeypatch, tmp_path):
    kc = tmp_path / "kc"
    kc.write_text("apiVersion: v1\n")
    monkeypatch.setattr(w, "current_role", lambda: "admin")
    monkeypatch.setattr(w, "current_cluster_identity", lambda: None)
    monkeypatch.setattr(w, "load_config", lambda: {"clusters": [{"name": "harvlab", "kubeconfig": str(kc)}]})
    monkeypatch.setattr(w, "_identity_kubeconfig", lambda k, ident: k)
    monkeypatch.setattr(w, "_capi_base", lambda c, sub: (["py", "harvester-capi", sub], None))


@pytest.mark.parametrize("label", ["capi-cluster-create:web/web", "capi-cluster-delete:web/web",
                                   "capi-install:harvlab", "capi-create:harvlab", "capi-delete:harvlab"])
def test_console_uninstall_refuses_while_a_capi_gesture_runs(wapp, monkeypatch, tmp_path, label):
    w, add = wapp
    add(label)
    _admin_console(w, monkeypatch, tmp_path)
    monkeypatch.setattr(w, "_deploy_lock_holder", lambda c: None)
    monkeypatch.setattr(w, "_capi_run_json", lambda cmd, spec=None, timeout=180: (({"turtles": True}, 0), None))
    launched = []
    monkeypatch.setattr(w, "_vm_transfer_runner", lambda run, cmd, **kw: launched.append(cmd))
    r = w.app.test_client().post("/api/capi/harvlab/uninstall", json={"confirm": "harvlab"})
    assert r.status_code == 409 and launched == []


def test_console_uninstall_passes_its_action_id_for_the_lock(wapp, monkeypatch, tmp_path):
    w, _ = wapp
    _admin_console(w, monkeypatch, tmp_path)
    monkeypatch.setattr(w, "_deploy_lock_holder", lambda c: None)
    monkeypatch.setattr(w, "_capi_run_json", lambda cmd, spec=None, timeout=180: (({"turtles": True}, 0), None))
    launched = []
    monkeypatch.setattr(w, "_vm_transfer_runner", lambda run, cmd, **kw: launched.append(cmd))
    r = w.app.test_client().post("/api/capi/harvlab/uninstall", json={"confirm": "harvlab"})
    assert r.status_code == 201
    aid = r.get_json()["action_id"]
    try:
        for _ in range(200):
            if launched:
                break
            import time
            time.sleep(0.01)
        assert f"--id={aid}" in launched[0]
    finally:
        w.ACTIONS.pop(aid, None)


def test_console_deploy_launch_refuses_during_an_uninstall(wapp, monkeypatch):
    w, add = wapp
    add("capi-uninstall:harvlab")
    monkeypatch.setattr(w, "_deploy_prepare",
                        lambda *a, **kw: ([], {}, {"plan": {"name": "site-a", "revision": 1}}))
    started = []
    monkeypatch.setattr(w, "_deploy_start", lambda *a, **kw: started.append(a) or "x")
    # verrou de fichier libre : l'outil ne l'a pas encore pris (fenêtre du lancement)
    monkeypatch.setattr(w, "_deploy_cluster_lock", lambda *a: (12345, None))
    monkeypatch.setattr(w, "_flock_release", lambda fd: None)
    body, status = w._deploy_launch_checked("harvlab", "kc", "site-a", {}, False, "ju", None)
    assert status == 409 and body["code"] == "capi-uninstall-running" and started == []


def test_console_deploy_launch_names_an_uninstall_holding_the_file_lock(wapp, monkeypatch):
    w, _ = wapp
    monkeypatch.setattr(w, "_deploy_prepare",
                        lambda *a, **kw: ([], {}, {"plan": {"name": "site-a", "revision": 1}}))
    monkeypatch.setattr(w, "_deploy_start", lambda *a, **kw: "x")
    monkeypatch.setattr(w, "_deploy_cluster_lock",
                        lambda *a: (None, {"run": "u9u9u9u9u9u9", "plan": "", "action": "capi-uninstall"}))
    body, status = w._deploy_launch_checked("harvlab", "kc", "site-a", {}, False, "ju", None)
    assert status == 409 and body["code"] == "capi-uninstall-running" and body["running"] == "u9u9u9u9u9u9"


def test_console_undo_checks_the_uninstall_under_the_launch_lock():
    """Le Défaire passe par la même garde que le déroulé, sous le verrou de
    lancement (lecture de la source : le lancer demanderait une chaîne
    entière)."""
    src = (ROOT / "web" / "app.py").read_text()
    body = src[src.index("def _undo_start("):]
    body = body[:body.index("\ndef ")]
    i = body.index("with _DEPLOY_LAUNCH_LOCK:")
    j = body.index("_deploy_cluster_lock(", i)
    assert "_capi_uninstall_refusal(cluster)" in body[i:j]          # avant de prendre le verrou
    assert "_capi_uninstall_refusal(cluster, holder)" in body[body.index("lock_fd is None", j):]
