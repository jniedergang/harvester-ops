"""2.0.0 : /api/clusters donne les nœuds déclarés (Paramètres > Connexion)
et l'adresse de jonction (« Joindre un nœud ») de chaque cluster.

L'adresse vient de l'hôte du serveur d'API du kubeconfig (port 443), la règle
de _cluster_for_server qui reconnaît le cluster visé par une jonction."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402

KC = """apiVersion: v1
clusters:
- cluster: {{server: "{server}"}}
  name: c
contexts:
- context: {{cluster: c, user: u}}
  name: c
current-context: c
kind: Config
users:
- name: u
  user: {{token: x}}
"""


@pytest.mark.parametrize("server,url", [
    ("https://172.16.3.100:6443", "https://172.16.3.100:443"),
    ("https://harv1.home.lo:6443", "https://harv1.home.lo:443"),
    ("https://[fd00::10]:6443", "https://[fd00::10]:443"),
    ("https://10.0.0.1", "https://10.0.0.1:443"),
])
def test_join_url_from_the_kubeconfig_host(tmp_path, server, url):
    kc = tmp_path / "kc.yaml"
    kc.write_text(KC.format(server=server))
    assert wapp._cluster_join_url({"kubeconfig": str(kc)}) == url
    # la même règle reconnaît le cluster visé
    assert wapp._cluster_api_endpoint(str(kc))[0] in url


@pytest.mark.parametrize("kubeconfig", ["", "/nonexistent/kc.yaml"])
def test_join_url_is_none_when_unreadable(kubeconfig):
    assert wapp._cluster_join_url({"kubeconfig": kubeconfig}) is None


def test_clusters_list_carries_nodes_and_join_url(tmp_path, monkeypatch):
    kc = tmp_path / "kc.yaml"
    kc.write_text(KC.format(server="https://172.16.3.100:6443"))
    cfg = {"clusters": [{"name": "harv1", "kubeconfig": str(kc), "description": "lab",
                         "nodes": [{"hostname": "n1", "ip": "172.16.3.11", "role": "control-plane"}, "bad"]},
                        {"name": "bare", "kubeconfig": "/none.yaml"}]}
    monkeypatch.setattr(wapp, "load_config", lambda: cfg)
    monkeypatch.setattr(wapp, "_config_cluster_names", lambda: {"harv1"})
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")
    with wapp.app.test_client() as c:
        d = c.get("/api/clusters").get_json()
    harv1, bare = d["clusters"]
    assert harv1["nodes"] == [{"hostname": "n1", "ip": "172.16.3.11", "role": "control-plane"}]
    assert harv1["join_url"] == "https://172.16.3.100:443"
    assert (bare["nodes"], bare["join_url"], bare["origin"]) == ([], None, "console")
    # rien qui ressemble à un chemin ou à un secret
    assert "kubeconfig" not in harv1 and "ssh" not in harv1
