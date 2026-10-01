"""v1.83.0 : lectures Kubernetes directes contre l'API (bin/lib/kube_rest.py),
contre un faux serveur d'API : même résultat que kubectl, repli sur kubectl
pour ce qui n'est pas traité."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import kube_rest as kr  # noqa: E402

CORE = {"kind": "APIResourceList", "groupVersion": "v1", "resources": [
    {"name": "namespaces", "singularName": "namespace", "namespaced": False, "kind": "Namespace", "shortNames": ["ns"]},
    {"name": "nodes", "singularName": "node", "namespaced": False, "kind": "Node", "shortNames": ["no"]},
    {"name": "persistentvolumeclaims", "singularName": "persistentvolumeclaim", "namespaced": True,
     "kind": "PersistentVolumeClaim", "shortNames": ["pvc"]},
    {"name": "pods/log", "namespaced": True, "kind": "Pod"}]}
GROUPS = {"kind": "APIGroupList", "groups": [
    {"name": "kubevirt.io", "preferredVersion": {"groupVersion": "kubevirt.io/v1"}},
    {"name": "longhorn.io", "preferredVersion": {"groupVersion": "longhorn.io/v1beta2"}},
    {"name": "metrics.k8s.io", "preferredVersion": {"groupVersion": "metrics.k8s.io/v1beta1"}}]}
GV = {"kubevirt.io/v1": {"resources": [
    {"name": "virtualmachines", "singularName": "virtualmachine", "namespaced": True, "kind": "VirtualMachine", "shortNames": ["vm", "vms"]},
    {"name": "virtualmachineinstances", "singularName": "virtualmachineinstance", "namespaced": True,
     "kind": "VirtualMachineInstance", "shortNames": ["vmi"]}]},
    "longhorn.io/v1beta2": {"resources": [
        {"name": "nodes", "singularName": "node", "namespaced": True, "kind": "Node", "shortNames": ["lhn"]}]}}
_SPEC = {"runStrategy": "Always", "template": {"spec": {"domain": {"cpu": {"cores": 1}}}}}
VMS = [{"metadata": {"name": "a", "namespace": "ns1", "labels": {"app": "x"}}, "spec": _SPEC},
       {"metadata": {"name": "b", "namespace": "ns2", "labels": {}}, "spec": _SPEC}]


def make_handler(seen):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            seen.append((self.path, dict(self.headers)))
            p = self.path.split("?")[0]
            q = self.path.split("?")[1] if "?" in self.path else ""
            if p == "/api/v1":
                return self.send(200, CORE)
            if p == "/apis":
                return self.send(200, GROUPS)
            if p.startswith("/apis/metrics.k8s.io"):
                return self.send(503, {"kind": "Status", "reason": "ServiceUnavailable", "message": "unavailable"})
            for gv, body in GV.items():
                if p == f"/apis/{gv}":
                    return self.send(200, body)
            if p == "/apis/kubevirt.io/v1/virtualmachines":
                items = [v for v in VMS if "labelSelector=app%3Dx" not in q or v["metadata"]["labels"].get("app") == "x"]
                return self.send(200, {"kind": "VirtualMachineList", "apiVersion": "kubevirt.io/v1", "items": items})
            if p == "/apis/kubevirt.io/v1/namespaces/ns1/virtualmachines/a":
                return self.send(200, VMS[0])
            if p.startswith("/apis/kubevirt.io/v1/namespaces/ns1/virtualmachines/"):
                return self.send(404, {"kind": "Status", "reason": "NotFound",
                                       "message": 'virtualmachines.kubevirt.io "zz" not found'})
            if p == "/apis/kubevirt.io/v1/virtualmachineinstances":
                return self.send(403, {"kind": "Status", "reason": "Forbidden", "message":
                                       'virtualmachineinstances.kubevirt.io is forbidden: User "eve" cannot list resource '
                                       '"virtualmachineinstances" in API group "kubevirt.io" at the cluster scope'})
            if p == "/api/v1/nodes":
                return self.send(200, {"kind": "NodeList", "apiVersion": "v1", "items": [{"metadata": {"name": "n1"}}]})
            if p == "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes":
                return self.send(200, {"kind": "NodeList", "items": [{"metadata": {"name": "n1", "namespace": "longhorn-system"}}]})
            if p == "/api/v1/namespaces/default/persistentvolumeclaims":
                return self.send(200, {"kind": "PersistentVolumeClaimList", "items": []})
            return self.send(404, {"kind": "Status", "reason": "NotFound", "message": "no route " + p})
    return H


@pytest.fixture
def api(tmp_path, monkeypatch):
    seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(seen))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(kr, "ENABLED", True)
    kr._configs.clear()
    kr._discovery.clear()

    def kubeconfig(user=None, name="kc.yaml"):
        u = user if user is not None else {"token": "tok-123"}
        kc = tmp_path / name
        kc.write_text(json.dumps({
            "apiVersion": "v1", "kind": "Config", "current-context": "c",
            "clusters": [{"name": "c", "cluster": {"server": f"http://127.0.0.1:{srv.server_port}"}}],
            "users": [{"name": "u", "user": u}],
            "contexts": [{"name": "c", "context": {"cluster": "c", "user": "u"}}]}))
        return str(kc)
    yield kubeconfig, seen
    srv.shutdown()


def kubectl(kc, *args):
    return ["kubectl", "--kubeconfig", kc, *args, "-o", "json"]


def test_list_all_namespaces_carries_kind_and_api_version(api):
    kc, seen = api
    r = kr.run(kubectl(kc(), "get", "vm", "-A"))
    assert r.returncode == 0 and r.data["kind"] == "List"
    assert [(i["kind"], i["apiVersion"], i["metadata"]["name"]) for i in r.data["items"]] == \
        [("VirtualMachine", "kubevirt.io/v1", "a"), ("VirtualMachine", "kubevirt.io/v1", "b")]
    assert any(h.get("Authorization") == "Bearer tok-123" for _, h in seen)
    assert json.loads(kr.stdout_of(r)) == r.data


def test_grouped_get_mixes_scopes_and_resolves_core_first(api):
    kc, _ = api
    r = kr.run(kubectl(kc(), "get", "-A", "nodes,virtualmachines.kubevirt.io"))
    kinds = [i["kind"] for i in r.data["items"]]
    assert kinds.count("Node") == 1 and kinds.count("VirtualMachine") == 2
    assert next(i for i in r.data["items"] if i["kind"] == "Node")["apiVersion"] == "v1"
    r = kr.run(kubectl(kc(), "get", "nodes.longhorn.io", "-n", "longhorn-system"))
    assert r.data["items"][0]["apiVersion"] == "longhorn.io/v1beta2"


def test_named_object_label_selector_and_default_namespace(api):
    kc, seen = api
    k = kc()
    r = kr.run(kubectl(k, "get", "vm", "a", "-n", "ns1"))
    assert r.data["kind"] == "VirtualMachine" and r.data["metadata"]["name"] == "a"
    r = kr.run(kubectl(k, "get", "vm", "-A", "-l", "app=x"))
    assert [i["metadata"]["name"] for i in r.data["items"]] == ["a"]
    r = kr.run(kubectl(k, "get", "pvc"))                 # namespace du contexte : default
    assert r.returncode == 0 and r.data["items"] == []


def test_errors_read_like_kubectl(api):
    kc, _ = api
    k = kc()
    r = kr.run(kubectl(k, "get", "vm", "zz", "-n", "ns1"))
    assert r.returncode == 1 and r.stderr == 'Error from server (NotFound): virtualmachines.kubevirt.io "zz" not found'
    r = kr.run(kubectl(k, "get", "vmi", "-A"))
    assert r.returncode == 1 and r.stderr.startswith("Error from server (Forbidden): virtualmachineinstances.kubevirt.io is forbidden")
    r = kr.run(kubectl(k, "get", "nosuch", "-A"))
    assert r.stderr == 'error: the server doesn\'t have a resource type "nosuch"'


def test_impersonation_from_the_kubeconfig(api):
    kc, seen = api
    kr.run(kubectl(kc({"token": "t", "as": "user-x", "as-groups": ["grp"]}), "get", "nodes"))
    hdr = seen[-1][1]
    assert hdr.get("Impersonate-User") == "user-x" and hdr.get("Impersonate-Group") == "grp"


@pytest.mark.parametrize("args,user", [
    (("get", "vm", "-A", "-o", "yaml"), None),            # autre sortie
    (("get", "vm", "-A", "--sort-by", "x"), None),        # option inconnue
    (("get", "vm/a", "-n", "ns1"), None),                 # forme type/nom
    (("delete", "vm", "a", "-n", "ns1"), None),           # pas une lecture
    (("get", "vm", "-A"), {"exec": {"command": "x"}}),    # plugin d'authentification
    (("get", "vm", "-A"), {"token": "t", "as-groups": ["a", "b"]}),
])
def test_left_to_kubectl(api, args, user):
    kc, _ = api
    argv = ["kubectl", "--kubeconfig", kc(user, name=f"k{abs(hash(str(user)))}.yaml"), *args]
    if "-o" not in args:
        argv += ["-o", "json"]
    assert kr.run(argv) is None


def test_missing_kubeconfig_left_to_kubectl(tmp_path, monkeypatch):
    monkeypatch.setattr(kr, "ENABLED", True)
    assert kr.run(kubectl(str(tmp_path / "absent.yaml"), "get", "nodes")) is None


def test_unreachable_server_answers_like_kubectl_without_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(kr, "ENABLED", True)
    kc = tmp_path / "down.yaml"
    kc.write_text(json.dumps({"current-context": "c", "contexts": [{"name": "c", "context": {"cluster": "c", "user": "u"}}],
                              "clusters": [{"name": "c", "cluster": {"server": "http://127.0.0.1:1"}}],
                              "users": [{"name": "u", "user": {"token": "t"}}]}))
    r = kr.run(kubectl(str(kc), "get", "nodes"), timeout=2)
    assert r.returncode == 1 and r.stderr.startswith("Unable to connect to the server")


def test_disabled_switch(api, monkeypatch):
    kc, _ = api
    monkeypatch.setattr(kr, "ENABLED", False)
    assert kr.run(kubectl(kc(), "get", "nodes")) is None


def test_console_reads_through_the_api_and_notes_refusals(api, monkeypatch):
    """La console : _kubectl_json rend l'objet sans kubectl, et un refus de la
    RBAC est retenu comme avec kubectl (en-tête X-Cluster-Denied)."""
    kc, _ = api
    sys.path.insert(0, str(ROOT / "web"))
    import app as wapp

    def no_kubectl(*a, **k):
        raise AssertionError("kubectl must not run")
    monkeypatch.setattr(wapp.subprocess, "run", no_kubectl)
    k = kc()
    with wapp.app.test_request_context("/api/x"):
        d = wapp._kubectl_json(k, "get", "vm", "-A", cluster="t")
        assert [i["metadata"]["name"] for i in d["items"]] == ["a", "b"]
        assert wapp._kubectl_json(k, "get", "vmi", "-A", cluster="t") is None
        assert wapp.g.cluster_denials and wapp.g.cluster_denials[0]["resource"] == "virtualmachineinstances"
        r = wapp._kubectl_run(kubectl(k, "get", "nodes"), capture_output=True)
        assert json.loads(r.stdout)["items"][0]["metadata"]["name"] == "n1"     # octets, comme subprocess


def test_stdout_text_and_bytes_like_subprocess(api):
    """Vu sur le banc : en mode texte, `stdout` restait vide et la liste des
    VMs plantait en relisant stdout."""
    kc, _ = api
    sys.path.insert(0, str(ROOT / "web"))
    import app as wapp
    k = kc()
    with wapp.app.test_request_context("/api/x"):
        t = wapp._kubectl_run(kubectl(k, "get", "nodes"), capture_output=True, text=True)
        assert isinstance(t.stdout, str) and json.loads(t.stdout)["items"][0]["metadata"]["name"] == "n1"
        b = wapp._kubectl_run(kubectl(k, "get", "nodes"), capture_output=True)
        assert isinstance(b.stdout, bytes) and isinstance(b.stderr, bytes)
        e = wapp._kubectl_run(kubectl(k, "get", "vmi", "-A"), capture_output=True, text=True)
        assert e.returncode == 1 and e.stdout == "" and "Forbidden" in e.stderr


def test_a_closed_kept_connection_is_retried_on_a_new_one(api):
    """Vu sous charge sur le banc : le serveur ferme les connexions gardées ;
    le second essai reprenait une autre connexion périmée du réservoir."""
    kc, _ = api
    k = kc()
    conn = kr.conn_for(k)
    assert kr.run(kubectl(k, "get", "nodes")).returncode == 0

    class Stale:
        sock = None
        timeout = 0

        def request(self, *a, **kw):
            import http.client
            raise http.client.RemoteDisconnected("Remote end closed connection without response")

        def close(self):
            pass
    conn._idle[:] = [Stale(), Stale(), Stale()]
    r = kr.run(kubectl(k, "get", "nodes"))
    assert r.returncode == 0 and r.data["items"][0]["metadata"]["name"] == "n1"
