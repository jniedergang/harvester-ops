"""2.7.0 (tâche 6) : gestes d'artefacts hors plan. Appliquer un élément d'un
paquet importé à un cluster enfant (chart ou manifeste selon la sorte lue
dans le paquet), contrôler avant le bouton, retirer, poser le socle de
politiques, lire ce qui est appliqué. Chaque geste : opérateur ET droit sur
le Secret `<c>-kubeconfig`, action suivie, outil `harvester-capi` (ici un
outil simulé qui consigne sa ligne de commande et sa demande)."""

import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp  # noqa: E402

FAKE_TOOL = r'''
import json, os, sys
argv = sys.argv[1:]
sub = argv[0]
spec = None
if "--spec" in argv:
    p = argv[argv.index("--spec") + 1]
    spec = json.load(sys.stdin) if p == "-" else json.load(open(p))
d = os.environ["FAKE_DIR"]
with open(os.path.join(d, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": argv, "spec": spec}) + "\n")
if sub == "token":
    sys.stdout.write(json.dumps({"token": os.environ["FAKE_TOKEN"], "expires_at": "2026-11-07T15:00:00Z"}) + "\n")
    sys.stdout.flush()
    sys.stderr.write("STEP_EVENT|token|done|token issued for equipe-a/ci on web, 600 s, by alice\n")
    sys.exit(int(os.environ.get("FAKE_TOKEN_RC", "0")))
out = os.path.join(d, sub + ".json")
if os.path.exists(out):
    sys.stdout.write(open(out).read())
rc = os.path.join(d, sub + ".rc")
sys.stderr.write("STEP_EVENT|%s|done|ok\n" % sub)
sys.exit(int(open(rc).read()) if os.path.exists(rc) else 0)
'''

ITEMS = [{"name": "essai", "kind": "manifest", "files": ["manifests/essai/a.yaml"]},
         {"name": "regles", "kind": "policy", "files": ["manifests/regles/a.yaml"]},
         {"name": "podinfo", "kind": "chart", "chart": "charts/podinfo-6.9.2.tgz"},
         {"name": "outils", "kind": "images", "images": ["x/tool:1.0"]},
         {"name": "flux", "kind": "gitops", "files": ["gitops/a.yaml"]},
         {"name": "x" * 43, "kind": "manifest", "files": ["manifests/long/a.yaml"]}]
PUBLISHED = [{"registry": "amorcage", "package": "banc", "version": "1.0.0", "item": "podinfo",
              "chart": "podinfo", "chart_version": "6.9.2"}]


@pytest.fixture
def env(tmp_path, monkeypatch):
    wapp.app.config["TESTING"] = True
    state = tmp_path / "state"
    pkg = state / "artefacts.d" / "banc-1.0.0"
    pkg.mkdir(parents=True)
    (pkg / "artefacts.yaml").write_text(json.dumps({"name": "banc", "version": "1.0.0", "items": ITEMS}))
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "harvester-capi.py").write_text(FAKE_TOOL)
    kc = tmp_path / "kc"
    kc.write_text("apiVersion: v1\nkind: Config\n")
    right = {"answer": "yes"}

    def can_i(argv, **kw):
        right.setdefault("asked", []).append(argv)
        return SimpleNamespace(stdout=right["answer"] + "\n", stderr="", returncode=0 if right["answer"] == "yes" else 1)
    monkeypatch.setenv("FAKE_DIR", str(fake))
    monkeypatch.setattr(wapp, "_kubectl_run", can_i)
    monkeypatch.setattr(wapp, "_bm_profiles_state", lambda: state)
    monkeypatch.setattr(wapp, "_capi_script", lambda: fake / "harvester-capi.py")
    monkeypatch.setattr(wapp, "_capi_registry_charts", lambda c: list(PUBLISHED) if c == "harvlab2" else [])
    monkeypatch.setattr(wapp, "current_user", lambda: "alice")
    monkeypatch.setattr(wapp, "current_role", lambda: right.get("role", "operator"))
    monkeypatch.setattr(wapp, "open_mode", lambda: True)
    monkeypatch.setattr(wapp, "current_cluster_identity", lambda: None)
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: str(kc) if c == "harvlab2" else None)
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda k, timeout=2.0: True)
    monkeypatch.setattr(wapp, "_CAPI_SERVICES_CACHE", {})
    with wapp.ACTIONS_LOCK:
        before = dict(wapp.ACTIONS)
    yield SimpleNamespace(client=wapp.app.test_client(), right=right, fake=fake, state=state, before=set(before))
    with wapp.ACTIONS_LOCK:
        wapp.ACTIONS.clear()
        wapp.ACTIONS.update(before)


def calls(env):
    p = env.fake / "calls.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.is_file() else []


def wait(aid, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        run = wapp.ACTIONS.get(aid)
        if run is not None and run.status in ("done", "error", "cancelled"):
            return run
        time.sleep(0.05)
    raise AssertionError("action did not finish")


def new_actions(env, prefixes):
    """Actions lancées depuis le début du test (d'autres tests du même
    processus en laissent dans ACTIONS)."""
    with wapp.ACTIONS_LOCK:
        return [a for i, a in wapp.ACTIONS.items() if i not in env.before and a.action.startswith(prefixes)]


def body(**over):
    b = {"cluster": "web/web", "package": "banc", "version": "1.0.0", "item": "essai"}
    b.update(over)
    return {k: v for k, v in b.items() if v is not None}


# ---------------------------------------------------------------------------
# Rôles et limites
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method,path,role", [
    ("GET", "/api/capi/harvlab2/artefacts/applied", "viewer"),
    ("POST", "/api/capi/harvlab2/artefact", "operator"),
    ("POST", "/api/capi/harvlab2/artefact-check", "operator"),
    ("DELETE", "/api/capi/harvlab2/artefact/web/web/essai", "operator"),
    ("POST", "/api/capi/harvlab2/policies", "operator"),
    ("POST", "/api/capi/harvlab2/token", "operator"),
])
def test_roles(method, path, role):
    assert wapp.required_role_for(path, method) == role


def test_limits_of_every_new_write_route_are_readable():
    """flask-limiter ignore en silence une limite illisible."""
    from limits import parse_many
    src = (ROOT / "web" / "app.py").read_text()
    for fn in ("api_capi_artefact_check", "api_capi_artefact_apply", "api_capi_artefact_remove",
               "api_capi_policies", "api_capi_token"):
        i = src.index(f"def {fn}(")
        head = src[src.rindex("@app.route", 0, i):i]
        assert "@requires_auth" in head, fn
        m = re.findall(r'@_rate_limit\("([^"]+)"\)', head)
        assert len(m) == 1, fn
        assert parse_many(m[0]), fn


# ---------------------------------------------------------------------------
# Appliquer
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("over,sub,label,expect", [
    ({}, "manifest-deploy", "capi-manifest-deploy:web/web:essai",
     {"cluster": "web/web", "package": "banc", "version": "1.0.0", "item": "essai", "by": "alice"}),
    ({"item": "regles"}, "manifest-deploy", "capi-manifest-deploy:web/web:regles",
     {"cluster": "web/web", "package": "banc", "version": "1.0.0", "item": "regles", "by": "alice"}),
])
def test_a_manifest_or_a_policy_starts_manifest_deploy(env, over, sub, label, expect):
    r = env.client.post("/api/capi/harvlab2/artefact", json=body(**over))
    assert r.status_code == 202, r.get_json()
    run = wait(r.get_json()["action_id"])
    assert run.action == label and run.status == "done"
    c = calls(env)[-1]
    assert c["argv"][0] == sub and c["spec"] == expect
    assert not [a for a in run.cmd if a.startswith("/")]
    assert env.right["asked"][-1][-4:] == ["get", "secrets/web-kubeconfig", "-n", "web"]


def test_a_chart_starts_service_deploy_with_its_source_values_and_person(env):
    r = env.client.post("/api/capi/harvlab2/artefact", json=body(item="podinfo", registry="amorcage",
                                                                  values={"replicaCount": 2}))
    assert r.status_code == 202, r.get_json()
    run = wait(r.get_json()["action_id"])
    assert run.action == "capi-service-deploy:web/web:podinfo"
    spec = calls(env)[-1]["spec"]
    assert calls(env)[-1]["argv"][0] == "service-deploy"
    assert spec["source"] == {"registry": "amorcage", "package": "banc", "version": "1.0.0", "item": "podinfo"}
    assert spec["by"] == "alice" and spec["name"] == "podinfo" and "replicaCount: 2" in spec["values"]


@pytest.mark.parametrize("over,status,code", [
    ({"cluster": "Web/web"}, 400, None),                                   # nom invalide
    ({"cluster": "web"}, 400, None),
    ({"item": "Bad Item"}, 400, None),
    ({"extra": 1}, 400, None),
    ({"item": "absent"}, 404, "artefact-item-unknown"),
    ({"version": "9.9.9"}, 404, "artefact-package-missing"),
    ({"item": "outils"}, 400, "artefact-kind-images"),
    ({"item": "flux"}, 400, "artefact-kind-later"),
    ({"item": "x" * 43}, 400, "artefact-name-too-long"),
    ({"item": "podinfo"}, 400, None),                                      # registre exigé
    ({"item": "podinfo", "registry": "autre"}, 409, "artefact-not-published"),
    ({"item": "podinfo", "registry": "amorcage", "values": "- a\n"}, 400, None),
    ({"item": "podinfo", "registry": "amorcage", "values": "a: [\n"}, 400, None),
    ({"registry": "amorcage"}, 400, None),                                 # registre refusé pour un manifeste
    ({"values": {"a": 1}}, 400, None),
])
def test_refusals_start_nothing(env, over, status, code):
    r = env.client.post("/api/capi/harvlab2/artefact", json=body(**over))
    assert r.status_code == status, r.get_json()
    if code:
        assert r.get_json()["code"] == code
    assert calls(env) == [] and "asked" not in env.right


def test_a_viewer_is_refused(env):
    env.right["role"] = "viewer"
    for method, path, b in (("post", "/api/capi/harvlab2/artefact", body()),
                            ("post", "/api/capi/harvlab2/policies", {"cluster": "web/web", "policies": {}}),
                            ("delete", "/api/capi/harvlab2/artefact/web/web/essai?kind=manifest", None)):
        r = getattr(env.client, method)(path, json=b)
        assert r.status_code == 403 and r.get_json()["required"] == "operator"
    assert calls(env) == []


@pytest.mark.parametrize("answer,status", [("no", 403), ("", 502)])
def test_an_operator_without_the_secret_right_is_refused(env, answer, status):
    env.right["answer"] = answer
    for method, path, b in (("post", "/api/capi/harvlab2/artefact", body()),
                            ("post", "/api/capi/harvlab2/artefact", body(item="podinfo", registry="amorcage")),
                            ("post", "/api/capi/harvlab2/policies", {"cluster": "web/web", "policies": {}}),
                            ("delete", "/api/capi/harvlab2/artefact/web/web/essai?kind=manifest", None),
                            ("delete", "/api/capi/harvlab2/artefact/web/web/podinfo?kind=chart", None)):
        r = getattr(env.client, method)(path, json=b)
        assert r.status_code == status, (path, r.get_json())
        if status == 403:
            assert r.get_json()["code"] == "child-admin-required"
    assert calls(env) == []
    assert new_actions(env, ("capi-manifest", "capi-service", "capi-policies")) == []


def test_an_unknown_harvester_cluster_is_404(env):
    assert env.client.post("/api/capi/nope/artefact", json=body()).status_code == 404


# ---------------------------------------------------------------------------
# Contrôle avant le bouton
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("over,sub", [({}, "manifest-check"),
                                      ({"item": "podinfo", "registry": "amorcage"}, "service-check")])
def test_the_check_runs_the_read_only_check_of_the_same_request(env, over, sub):
    (env.fake / f"{sub}.json").write_text(json.dumps({"blocked": False, "findings": [], "objects": 2}))
    r = env.client.post("/api/capi/harvlab2/artefact-check", json=body(**over))
    assert r.status_code == 200 and r.get_json()["blocked"] is False, r.get_json()
    assert calls(env)[-1]["argv"][0] == sub and calls(env)[-1]["argv"][-2:] == ["--spec", "-"]
    assert new_actions(env, ("capi-manifest", "capi-service")) == []


@pytest.mark.parametrize("answer,code", [("no", "child-admin-required"), ("", "child-admin-unknown")])
def test_the_check_names_the_missing_right_as_a_blocker(env, answer, code):
    (env.fake / "manifest-check.json").write_text(json.dumps({"blocked": False, "findings": []}))
    env.right["answer"] = answer
    out = env.client.post("/api/capi/harvlab2/artefact-check", json=body()).get_json()
    assert out["blocked"] is True and [f["code"] for f in out["findings"]] == [code]


def test_a_blocked_check_says_blocked(env):
    (env.fake / "manifest-check.json").write_text(json.dumps({"blocked": True, "findings": [
        {"code": "manifest-kind-refused", "level": "block", "facts": {}}]}))
    (env.fake / "manifest-check.rc").write_text("2")
    out = env.client.post("/api/capi/harvlab2/artefact-check", json=body()).get_json()
    assert out["blocked"] is True and out["kind"] == "manifest"


# ---------------------------------------------------------------------------
# Retirer
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("item,kind,sub,name,label", [
    ("essai", "manifest", "manifest-remove", "web/web/essai", "capi-manifest-remove:web/web/essai"),
    ("regles", "policy", "manifest-remove", "web/web/regles", "capi-manifest-remove:web/web/regles"),
    ("hops-policies", "policies", "manifest-remove", "web/web/hops-policies",
     "capi-manifest-remove:web/web/hops-policies"),
    ("podinfo", "chart", "service-remove", "web/podinfo", "capi-service-remove:web/podinfo"),
])
def test_remove_by_kind(env, item, kind, sub, name, label):
    r = env.client.delete(f"/api/capi/harvlab2/artefact/web/web/{item}?kind={kind}")
    assert r.status_code == 202, r.get_json()
    run = wait(r.get_json()["action_id"])
    assert run.action == label
    argv = calls(env)[-1]["argv"]
    assert argv[0] == sub and argv[argv.index("--name") + 1] == name


@pytest.mark.parametrize("path,status", [
    ("/api/capi/harvlab2/artefact/web/web/essai", 400),                  # sorte non dite
    ("/api/capi/harvlab2/artefact/web/web/essai?kind=images", 400),
    ("/api/capi/harvlab2/artefact/Web/web/essai?kind=manifest", 400),
    ("/api/capi/harvlab2/artefact/web/web/" + "x" * 43 + "?kind=manifest", 400),
    ("/api/capi/nope/artefact/web/web/essai?kind=manifest", 404),
])
def test_remove_refusals(env, path, status):
    assert env.client.delete(path).status_code == status
    assert calls(env) == []


# ---------------------------------------------------------------------------
# Socle de politiques
# ---------------------------------------------------------------------------
def test_policies_start_manifest_deploy_with_the_policies_request(env):
    pol = {"base": True, "teams": [{"namespace": "equipe-a"}],
           "accounts": [{"namespace": "equipe-a", "name": "ci", "role": "edit"}]}
    r = env.client.post("/api/capi/harvlab2/policies", json={"cluster": "web/web", "policies": pol})
    assert r.status_code == 202, r.get_json()
    run = wait(r.get_json()["action_id"])
    assert run.action == "capi-policies:web/web"
    c = calls(env)[-1]
    assert c["argv"][0] == "manifest-deploy" and c["spec"] == {"cluster": "web/web", "policies": pol, "by": "alice"}


@pytest.mark.parametrize("b,needle", [
    ({"cluster": "web/web", "policies": {"base": "yes"}}, "base"),
    ({"cluster": "web/web", "policies": {"base": True}}, "teams"),
    ({"cluster": "web/web", "policies": {"teams": [{"namespace": "kube-system"}]}}, "teams[0].namespace"),
    ({"cluster": "web/web", "policies": {"nope": 1}}, "policies.nope"),
    ({"cluster": "web", "policies": {}}, "cluster"),
    ({"cluster": "web/web", "policies": {}, "x": 1}, "x"),
])
def test_policies_are_validated_naming_the_field(env, b, needle):
    r = env.client.post("/api/capi/harvlab2/policies", json=b)
    assert r.status_code == 400 and needle in r.get_json()["error"]
    assert calls(env) == []


def test_the_base_stays_unchecked_by_default():
    import child_policies as cp
    assert cp.normalize({})["base"] is False


# ---------------------------------------------------------------------------
# Ce qui est appliqué
# ---------------------------------------------------------------------------
def test_applied_lists_charts_manifests_kinds_and_accounts(env):
    art = {"item": "podinfo", "package": "banc", "version": "1.0.0", "cluster": "web", "by": "alice", "origin": {}}
    (env.fake / "services.json").write_text(json.dumps({"caaph": True, "clusters": ["web/web"], "catalog": [],
        "services": [{"namespace": "web", "name": "podinfo", "artefact": art},
                     {"namespace": "web", "name": "coredns", "artefact": None}]}))
    st = {"applied": True, "hash": "sha256:x", "clusters": [], "message": "applied"}
    (env.fake / "manifests.json").write_text(json.dumps([
        {"namespace": "web", "cluster": "web", "item": "essai", "package": "banc", "version": "1.0.0", "state": st},
        {"namespace": "web", "cluster": "web", "item": "regles", "package": "banc", "version": "1.0.0", "state": st},
        {"namespace": "web", "cluster": "web", "item": "hops-policies", "package": "harvester-ops",
         "version": "builtin", "state": st, "accounts": [{"namespace": "equipe-a", "name": "ci", "role": "edit"}]}]))
    env.right["role"] = "viewer"
    out = env.client.get("/api/capi/harvlab2/artefacts/applied").get_json()
    assert [c["name"] for c in out["charts"]] == ["podinfo"] and out["charts"][0]["kind"] == "chart"
    assert [(m["item"], m["kind"]) for m in out["manifests"]] == [
        ("essai", "manifest"), ("regles", "policy"), ("hops-policies", "policies")]
    assert out["accounts"] == [{"namespace": "equipe-a", "name": "ci", "role": "edit", "cluster": "web/web"}]
    assert "accounts" not in out["manifests"][2]


def test_applied_says_unreachable(env, monkeypatch):
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda k, timeout=2.0: False)
    out = env.client.get("/api/capi/harvlab2/artefacts/applied").get_json()
    assert out["unreachable"] is True and calls(env) == []


# ---------------------------------------------------------------------------
# `harvester-capi manifests` : comptes d'automatisation du socle
# ---------------------------------------------------------------------------
def test_manifests_lists_the_automation_accounts_of_the_policies(monkeypatch, capsys):
    import importlib.util
    import capi_manifests as mf
    import child_policies as cp
    spec = importlib.util.spec_from_file_location("harvester_capi_t6", ROOT / "bin" / "harvester-capi.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    pol = cp.normalize({"teams": [{"namespace": "equipe-a"}],
                        "accounts": [{"namespace": "equipe-a", "name": "ci", "role": "edit"},
                                     {"namespace": "equipe-a", "name": "lecture", "role": "view"}]})
    cm, crs = mf.docs("web", "web", cp.ITEM, "harvester-ops", "builtin", cp.objects(pol), by="alice")
    other_cm, other_crs = mf.docs("web", "web", "essai", "banc", "1.0.0",
                                  [{"apiVersion": "v1", "kind": "ServiceAccount",
                                    "metadata": {"name": "pas-un-compte", "namespace": "x"}}])

    class K:
        def list(self, kind, ns=None, selector=None):
            return [crs, other_crs] if kind == mf.K_CRS else []

        def get(self, kind, ns, name):
            return {cm["metadata"]["name"]: cm, other_cm["metadata"]["name"]: other_cm}.get(name)
    monkeypatch.setattr(cli, "kube_from", lambda a: (K(), None, None))
    assert cli.main(["manifests", "--json"]) == cli.EXIT_OK
    out = {m["item"]: m for m in json.loads(capsys.readouterr().out)}
    assert out[cp.ITEM]["accounts"] == [{"namespace": "equipe-a", "name": "ci", "role": "edit"},
                                        {"namespace": "equipe-a", "name": "lecture", "role": "view"}]
    assert "accounts" not in out["essai"]
