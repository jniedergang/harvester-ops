"""v1.49.0 : les points d'entrée des réseaux kube-ovn.

Le serveur de test ne joint aucun cluster (`harv-fake`) : on vérifie la
forme des réponses, le refus des demandes mal formées avant tout appel, et
que l'écriture est réservée au rôle admin.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402


def test_the_view_says_when_the_cluster_cannot_be_reached(api):
    status, body = api("GET", "/api/kubeovn/harv-fake")
    assert status == 200, body
    assert body["unreachable"] is True and body["kubeovn"] is False


def test_malformed_requests_are_refused_before_the_cluster(api):
    for body in ({"kind": "router", "spec": {}},
                 {"kind": "subnet", "spec": {"name": "a", "cidrBlock": "10.0.0.0/24"}},
                 {"kind": "vpc", "spec": {"name": "a", "enableExternal": True}}):
        status, out = api("POST", "/api/kubeovn/harv-fake/check", json_body=body, expect_status=None)
        assert status == 400, (body, out)
    status, _ = api("DELETE", "/api/kubeovn/harv-fake/router/x", expect_status=None)
    assert status == 400


def test_a_check_on_an_unreachable_cluster_says_why(api):
    status, out = api("POST", "/api/kubeovn/harv-fake/check", expect_status=None,
                      json_body={"kind": "subnet", "spec": {"name": "lab-a", "cidr": "10.200.0.0/24",
                                                            "new_network": "default/lab-a"}})
    assert status == 502 and "unreachable" in out["error"]


def test_writes_are_for_admins_reads_for_everyone():
    assert wapp.required_role_for("/api/kubeovn/harv1", "GET") == "viewer"
    for method, path in (("POST", "/api/kubeovn/harv1/check"), ("POST", "/api/kubeovn/harv1/apply"),
                         ("DELETE", "/api/kubeovn/harv1/subnet/lab-a")):
        assert wapp.required_role_for(path, method) == "admin"


def test_the_tool_is_deployed_with_the_console():
    assert (ROOT / "bin" / "harvester-network.py").stat().st_mode & 0o111
    assert (ROOT / "bin" / "lib" / "ovn_net.py").is_file()
    assert '"$PREFIX/harvester-network"' in (ROOT / "install.sh").read_text()
    assert "/usr/local/bin/harvester-network" in (ROOT / "container" / "Containerfile").read_text()
