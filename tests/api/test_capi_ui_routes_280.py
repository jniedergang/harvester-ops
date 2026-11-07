"""2.8.0 (tâche 9) : ce que l'interface demande au serveur en plus.

* vérification GitOps de la fenêtre (`dry_run`) : `gitops-check` en lecture,
  aucune action, aucune valeur de secret envoyée à l'outil, droit sur le
  cluster enfant rendu en constat ;
* détail d'un cluster enfant : réglages OIDC publics (annotation) ;
* membres d'un cluster enfant importé : identifiant Rancher lu par
  `rancher-status`, refusé si l'import vise un autre Rancher ;
* `harvester-resources member --cid` : scope cluster seul, forme vérifiée.
"""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app as wapp  # noqa: E402
from test_capi_artefact_routes_270 import calls, env  # noqa: E402,F401

ENTRY = {"name": "demo", "namespace": "equipe-a",
         "source": {"type": "git", "url": "ssh://git@172.16.1.11:2222/x/app.git", "secret": "depot"}}
KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nUI-KEY-2809\n-----END OPENSSH PRIVATE KEY-----\n"


def check(env, **over):
    b = {"cluster": "web/web", "entry": dict(ENTRY), "secrets": {"depot": {"identity": KEY}}, "dry_run": True}
    b.update(over)
    return env.client.post("/api/capi/harvlab2/gitops", json=b)


def actions_now(env):
    with wapp.ACTIONS_LOCK:
        return set(wapp.ACTIONS) - env.before


@pytest.mark.parametrize("answer,rc,out,blocked,codes", [
    ("yes", 0, {"blocked": False, "tool": "flux", "findings": [], "entry": "demo"}, False, []),
    ("yes", 2, {"blocked": True, "tool": "fleet", "entry": "demo",
                "findings": [{"code": "gitops-fleet-admin-only", "level": "block", "facts": {}}]},
     True, ["gitops-fleet-admin-only"]),
    ("no", 0, {"blocked": False, "tool": "flux", "findings": [], "entry": "demo"}, True, ["child-admin-required"]),
])
def test_gitops_check_reads_only_and_never_sends_secret_values(env, answer, rc, out, blocked, codes):
    env.right["answer"] = answer
    (env.fake / "gitops-check.json").write_text(json.dumps(out))
    (env.fake / "gitops-check.rc").write_text(str(rc))
    r = check(env)
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d["blocked"] is blocked and [f["code"] for f in d["findings"]] == codes
    assert d["tool"] == out["tool"]
    c = calls(env)[-1]
    assert c["argv"][0] == "gitops-check" and "--spec" in c["argv"]
    assert c["spec"]["secrets"] == {"depot": {}}                       # seuls les noms
    assert "UI-KEY-2809" not in json.dumps(calls(env))
    assert actions_now(env) == set()                                   # lecture : aucune action


def test_gitops_check_refuses_a_bad_entry_before_the_tool(env):
    r = check(env, entry=dict(ENTRY, name="Bad Name"))
    assert r.status_code == 400 and calls(env) == []


def test_gitops_check_lets_an_operator_see_the_cluster_privileges_refusal(env):
    """Un essai n'est pas refusé en 403 : la fenêtre montre le constat de
    l'outil (`gitops-privileges-admin-only`) avant le bouton."""
    (env.fake / "gitops-check.json").write_text(json.dumps({"blocked": True, "tool": "flux", "entry": "demo", "findings": [
        {"code": "gitops-privileges-admin-only", "level": "block", "facts": {}}]}))
    (env.fake / "gitops-check.rc").write_text("2")
    r = check(env, entry=dict(ENTRY, privileges="cluster"))
    assert r.status_code == 200 and r.get_json()["blocked"] is True
    assert calls(env)[-1]["spec"]["role"] == "operator"


def test_details_give_the_public_oidc_settings(env, monkeypatch):
    cl = {"metadata": {"name": "web", "namespace": "web", "annotations": {
        "harvester-ops.io/oidc": json.dumps({"issuer_url": "https://idp.example", "audience": "k8s",
                                              "username_prefix": "oidc:", "groups_prefix": "oidc:"})}},
          "spec": {"topology": {}}, "status": {"phase": "Provisioned"}}

    def kubectl(argv, **kw):
        if "cluster.cluster.x-k8s.io" in argv:
            return SimpleNamespace(returncode=0, stdout=json.dumps(cl), stderr="")
        if "machines.cluster.x-k8s.io" in argv:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"items": []}), stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="not found")
    monkeypatch.setattr(wapp, "_kubectl_run", kubectl)
    d = env.client.get("/api/capi/harvlab2/cluster/web/web/details").get_json()
    assert d["oidc"] == {"issuer_url": "https://idp.example", "audience": "k8s", "username_prefix": "oidc:",
                         "groups_prefix": "oidc:"}
    cl["metadata"]["annotations"] = {}
    assert env.client.get("/api/capi/harvlab2/cluster/web/web/details").get_json()["oidc"] is None


# --- membres d'un cluster enfant importé ----------------------------------------------

SESSION = {"url": "https://rancher.example", "cid": "c-sg2q6", "token": "t"}


@pytest.fixture
def members(env, monkeypatch):
    got = {"paths": [], "cli": []}
    monkeypatch.setattr(wapp, "_session_rancher", lambda c: ("/kc", dict(SESSION)) if c == "harvlab2" else (None, None))

    def rget(r, path):
        got["paths"].append(path)
        return 200, {"data": []}
    monkeypatch.setattr(wapp, "_rancher_get", rget)

    def res_cli(cluster, kc, label, args):
        got["cli"].append((label, args))
        return SimpleNamespace(id="a1"), None
    monkeypatch.setattr(wapp, "_res_cli", res_cli)
    monkeypatch.setattr(wapp, "_res_reply", lambda run, err, action=None: (wapp.jsonify({"action_id": run.id}), 202))

    def status(rancher="https://rancher.example", present=True, cid="c-m-abc123", imported=True):
        (env.fake / "rancher-status.json").write_text(json.dumps({"session": "https://rancher.example", "clusters": [
            {"namespace": "web", "cluster": "web", "imported": imported, "rancher": rancher, "name": "harvlab2-web",
             "cid": cid, "present": present}]}))
    got["status"] = status
    return got


def test_members_of_an_imported_child_use_its_rancher_id(env, members):
    members["status"]()
    d = env.client.get("/api/rancher-members/harvlab2?scope=cluster&child=web/web").get_json()
    assert d["managed"] is True
    assert members["paths"][0] == "/v3/clusterroletemplatebindings?clusterId=c-m-abc123"
    argv = calls(env)[-1]["argv"]
    assert argv[0] == "rancher-status" and argv[argv.index("--name") + 1] == "web/web"
    r = env.client.post("/api/rancher-members/harvlab2/do/add",
                        json={"scope": "cluster", "child": "web/web", "principal": "local://u-abc", "role": "cluster-member"})
    assert r.status_code == 202
    label, args = members["cli"][-1]
    assert args[args.index("--cid") + 1] == "c-m-abc123" and label.endswith(":child")


@pytest.mark.parametrize("case", ["other-rancher", "gone", "not-imported", "bad-cid", "project-scope", "bad-ref"])
def test_members_of_a_child_are_refused_outside_the_session_rancher(env, members, case):
    if case == "other-rancher":
        members["status"](rancher="https://other.example")
    elif case == "gone":
        members["status"](present=False)
    elif case == "not-imported":
        members["status"](imported=False)
    elif case == "bad-cid":
        members["status"](cid="local")
    else:
        members["status"]()
    q = "scope=project&project=p-abc&child=web/web" if case == "project-scope" else \
        ("scope=cluster&child=web" if case == "bad-ref" else "scope=cluster&child=web/web")
    r = env.client.get(f"/api/rancher-members/harvlab2?{q}")
    assert r.status_code in (400, 409)
    if case not in ("project-scope", "bad-ref"):
        assert r.get_json()["code"] == "child-not-imported"
    assert members["paths"] == []
    body = {"scope": "project", "project": "p-abc"} if case == "project-scope" else {"scope": "cluster"}
    body.update(child="web" if case == "bad-ref" else "web/web", principal="local://u-abc", role="cluster-member")
    assert env.client.post("/api/rancher-members/harvlab2/do/add", json=body).status_code in (400, 409)
    assert members["cli"] == []


def test_members_without_child_keep_the_harvester_cluster(env, members):
    env.client.get("/api/rancher-members/harvlab2?scope=cluster")
    assert members["paths"][0] == "/v3/clusterroletemplatebindings?clusterId=c-sg2q6"


_spec = importlib.util.spec_from_file_location("hres_members_280", ROOT / "bin" / "harvester-resources.py")
hres = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hres)


@pytest.mark.parametrize("scope,cid,want", [
    ("cluster", None, "c-sg2q6"),
    ("cluster", "c-m-abc123", "c-m-abc123"),
    ("cluster", "c-m-ABC", ValueError),
    ("cluster", "local", ValueError),
    ("cluster", "c-m-abc/../x", ValueError),
    ("project", "c-m-abc123", ValueError),
])
def test_resources_member_target_takes_a_child_cluster_id(scope, cid, want):
    args = SimpleNamespace(scope=scope, cid=cid, project="p-abc")
    if want is ValueError:
        with pytest.raises(ValueError):
            hres._members_target({"cid": "c-sg2q6"}, args)
        return
    target, path, qs = hres._members_target({"cid": "c-sg2q6"}, args)
    assert target == want and qs == f"?clusterId={want}"
