"""v1.56.0 : dire ce que le cluster a refusé, au lieu d'une vue vide.

Vu en réel avec un compte « membre du cluster » de Rancher : l'aperçu
montrait « 0 nœud » et la topologie aucune VM, sans un mot. Les refus de la
RBAC sont maintenant rangés (verbe, ressource, groupe, espace de noms) et
partent avec la réponse, en-tête `X-Cluster-Denied`, que l'écran affiche.
"""

import json
import sys
from pathlib import Path

from flask import Response, g

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(Path(__file__).parent))
import app as wapp  # noqa: E402

VM_DENIED = ('Error from server (Forbidden): virtualmachines.kubevirt.io is forbidden: '
             'User "u-7df4t" cannot list resource "virtualmachines" in API group '
             '"kubevirt.io" in the namespace "default"')
NODES_DENIED = ('Error from server (Forbidden): nodes.longhorn.io is forbidden: User "u-7df4t" '
                'cannot list resource "nodes" in API group "longhorn.io" in the namespace '
                '"longhorn-system"')


class Done:
    def __init__(self, rc=0, stderr=""):
        self.returncode, self.stdout, self.stderr = rc, "", stderr


def test_a_refusal_is_parsed_and_travels_with_a_successful_response(monkeypatch):
    results = iter([Done(1, VM_DENIED), Done(1, NODES_DENIED), Done(1, VM_DENIED), Done(0)])
    monkeypatch.setattr(wapp.subprocess, "run", lambda *a, **k: next(results))
    with wapp.app.test_request_context("/api/topology/harv1"):
        for _ in range(4):
            wapp._kubectl_run(["kubectl", "get", "vm"], capture_output=True, text=True)
        assert g.cluster_denials == [
            {"verb": "list", "resource": "virtualmachines", "group": "kubevirt.io", "namespace": "default"},
            {"verb": "list", "resource": "nodes", "group": "longhorn.io", "namespace": "longhorn-system"},
        ]
        resp = wapp._surface_cluster_denial(Response("{}", 200, mimetype="application/json"))
        assert resp.status_code == 200
        assert json.loads(resp.headers["X-Cluster-Denied"]) == g.cluster_denials


def test_a_failure_that_is_not_a_refusal_says_nothing(monkeypatch):
    monkeypatch.setattr(wapp.subprocess, "run", lambda *a, **k: Done(1, "connection refused"))
    with wapp.app.test_request_context("/api/topology/harv1"):
        wapp._kubectl_run(["kubectl", "get", "vm"], capture_output=True, text=True)
        resp = wapp._surface_cluster_denial(Response("{}", 200))
        assert "X-Cluster-Denied" not in resp.headers


def test_a_refusal_at_the_cluster_scope_has_no_namespace():
    with wapp.app.test_request_context("/api/x"):
        wapp._note_cluster_denial('Error from server (Forbidden): namespaces is forbidden: User "u" '
                                  'cannot list resource "namespaces" in API group "" at the cluster scope')
        assert g.cluster_denials == [{"verb": "list", "resource": "namespaces"}]


def test_the_overview_says_what_the_status_script_was_refused(monkeypatch, tmp_path):
    htpasswd = tmp_path / "htpasswd"
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", htpasswd)          # absent : mode ouvert
    monkeypatch.setattr(wapp, "load_config", lambda: {
        "clusters": [{"name": "harv1", "kubeconfig": str(tmp_path / "kc")}]})
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc, **k: True)
    out = {"nodes": [{"name": "n1"}], "vms_by_namespace": {}, "longhorn": {}, "summary": {},
           "denied": [VM_DENIED]}
    monkeypatch.setattr(wapp.subprocess, "check_output", lambda *a, **k: json.dumps(out).encode())
    with wapp.app.test_client() as c:
        r = c.get("/api/status/harv1")
        assert r.status_code == 200 and r.get_json()["nodes"] == [{"name": "n1"}]
        assert json.loads(r.headers["X-Cluster-Denied"])[0]["resource"] == "virtualmachines"


def test_a_rancher_session_is_told_to_ask_rancher(monkeypatch, tmp_path):
    from test_rancher_login import sign_in
    import rancher_sso as rs
    from test_rancher_sso import URL, FakeRancher
    secret = tmp_path / "secret"
    secret.write_text("s3cret")
    htpasswd = tmp_path / "htpasswd"
    htpasswd.write_text("")
    cfg = {"clusters": [{"name": "harv1", "kubeconfig": str(tmp_path / "kc")}],
           "rancher": {"url": URL, "client_id": "client-abc", "client_secret_file": str(secret)}}
    monkeypatch.setattr(wapp, "load_config", lambda: cfg)
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", htpasswd)
    ids = tmp_path / "ids"
    ids.mkdir()
    monkeypatch.setattr(wapp, "_SSO_SESSIONS", rs.Sessions(lambda: ids))
    monkeypatch.setattr(wapp, "_SSO_PENDING", rs.PendingLogins())
    monkeypatch.setattr(wapp, "_SSO_CLUSTER_IDS", {})
    monkeypatch.setattr(wapp, "_kube_system_uid", lambda entry: "uid-harv1")
    monkeypatch.setattr(wapp, "_node_uids", lambda entry: frozenset({"node-uid-1"}))
    fake = FakeRancher()
    monkeypatch.setattr(wapp, "_sso_http", lambda s: fake)
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc, **k: True)
    with wapp.app.test_client() as c:
        sign_in(c, fake, roles=("user",), username="membre")
        monkeypatch.setattr(wapp.subprocess, "run", lambda *a, **k: Done(1, VM_DENIED))
        r = c.get("/api/vms/harv1")
        d = r.get_json()
        assert r.status_code == 403 and d["error"] == "cluster refused"
        assert d["via"] == "rancher" and d["cluster_user"] == "membre@rancher"
        assert "Rancher" in d["hint"] and "roles.yaml" not in d["hint"]
        assert d["denied"][0]["resource"] == "virtualmachines"
        assert "X-Cluster-Denied" in r.headers
