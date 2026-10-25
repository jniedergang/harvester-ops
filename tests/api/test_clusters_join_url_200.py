"""2.0.0 : adresse de jonction d'un cluster (« Joindre un nœud »).

Elle prépare une installation qui efface des disques : elle n'est jamais
devinée. Elle se lit DANS le cluster, là où Harvester range sa VIP (ConfigMap
`harvester-system/vip`, clé `ip`), à l'ouverture de la fenêtre de jonction
(GET /api/clusters/<c>/join-url). Kubeconfig qui passe par un mandataire
(Rancher) ou VIP illisible : null, la console dit qu'elle n'a pas d'adresse.

/api/clusters garde les nœuds déclarés (Paramètres > Connexion) mais ne
calcule plus d'adresse (un kubeconfig lu par cluster et par appel)."""

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

VIP_CM = {"data": {"enabled": "true", "ip": "172.16.3.100", "mode": "static",
                   "serviceType": "LoadBalancer", "hwAddress": "", "loadBalancerIP": ""}}


def _kc(tmp_path, server):
    kc = tmp_path / "kc.yaml"
    kc.write_text(KC.format(server=server))
    return str(kc)


def _setup(monkeypatch, kc, reads):
    """Un cluster `harv1` déclaré ; `reads` = réponse de kubectl (ConfigMap,
    ou None quand la lecture échoue). Rend la liste des appels kubectl."""
    calls = []

    def fake_json(kc_, *args, **kw):
        calls.append(args)
        return reads
    monkeypatch.setattr(wapp, "_kubectl_json", fake_json)
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: kc if c == "harv1" else None)
    return calls


@pytest.mark.parametrize("case,server,cm,url,reason", [
    # kubeconfig direct sur la VIP : la VIP lue dans le cluster
    ("direct-vip", "https://172.16.3.100:6443", VIP_CM, "https://172.16.3.100:443", None),
    # kubeconfig recopié de rke2.yaml : il vise UN nœud ; la VIP lue l'emporte
    ("rke2-node-ip", "https://172.16.3.11:6443", VIP_CM, "https://172.16.3.100:443", None),
    # kubeconfig de l'interface Harvester (https://VIP/k8s/clusters/local)
    ("harvester-ui", "https://172.16.3.100/k8s/clusters/local", VIP_CM, "https://172.16.3.100:443", None),
    # mandataire Rancher : jamais d'adresse (ni lecture)
    ("rancher-proxy", "https://rancher.example/k8s/clusters/c-xxx", VIP_CM, None, "proxied"),
    # VIP illisible : pas de repli sur l'hôte du kubeconfig
    ("vip-unreadable", "https://172.16.3.11:6443", None, None, "vip-unreadable"),
    ("vip-empty", "https://172.16.3.11:6443", {"data": {"ip": ""}}, None, "vip-unreadable"),
    ("vip-any", "https://172.16.3.11:6443", {"data": {"ip": "0.0.0.0"}}, None, "vip-unreadable"),
    ("vip-not-ip", "https://172.16.3.11:6443", {"data": {"ip": "evil.example"}}, None, "vip-unreadable"),
    ("vip-no-data", "https://172.16.3.11:6443", {}, None, "vip-unreadable"),
    ("vip-ipv6", "https://[fd00::11]:6443", {"data": {"ip": "fd00::10"}}, "https://[fd00::10]:443", None),
])
def test_join_url_is_read_from_the_cluster(tmp_path, monkeypatch, case, server, cm, url, reason):
    calls = _setup(monkeypatch, _kc(tmp_path, server), cm)
    assert wapp._cluster_join_url("harv1") == (url, reason), case
    if reason == "proxied":
        assert calls == []
    else:
        # la ConfigMap même de Harvester, rien d'autre
        assert calls == [("get", "configmap", "vip", "-n", "harvester-system")]


@pytest.mark.parametrize("kc", [None, "/nonexistent/kc.yaml"])
def test_join_url_is_none_without_a_kubeconfig(monkeypatch, kc):
    calls = _setup(monkeypatch, kc, VIP_CM)
    url, reason = wapp._cluster_join_url("harv1")
    assert url is None and reason in ("unavailable", "proxied")
    assert calls == []


def test_join_url_route(tmp_path, monkeypatch):
    _setup(monkeypatch, _kc(tmp_path, "https://172.16.3.11:6443"), VIP_CM)
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {"clusters": [{"name": "harv1", "kubeconfig": "x"}]})
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")
    with wapp.app.test_client() as c:
        d = c.get("/api/clusters/harv1/join-url").get_json()
        assert d == {"cluster": "harv1", "join_url": "https://172.16.3.100:443", "reason": None}
        # cluster non déclaré : 404, sans lecture
        assert c.get("/api/clusters/other/join-url").status_code == 404


def test_join_url_route_says_null_for_a_rancher_kubeconfig(tmp_path, monkeypatch):
    _setup(monkeypatch, _kc(tmp_path, "https://rancher.example/k8s/clusters/c-x"), VIP_CM)
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {"clusters": [{"name": "harv1", "kubeconfig": "x"}]})
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")
    with wapp.app.test_client() as c:
        d = c.get("/api/clusters/harv1/join-url").get_json()
    assert (d["join_url"], d["reason"]) == (None, "proxied")


@pytest.mark.parametrize("case,server_url,cluster,expect", [
    # adresse lue (VIP) alors que le kubeconfig vise un nœud : le cluster choisi
    ("vip-of-chosen", "https://172.16.3.100:443", "harv1", "harv1"),
    # hôte du kubeconfig : reconnu aussi
    ("kc-host-of-chosen", "https://172.16.3.11:443", "harv1", "harv1"),
    # adresse d'un autre cluster saisie à la main : pas le cluster choisi
    ("other-address", "https://10.9.9.9:443", "harv1", None),
    # cluster choisi inconnu : jamais retenu
    ("unknown-cluster", "https://172.16.3.100:443", "nope", None),
    # sans cluster choisi : la règle d'avant (hôte du kubeconfig seulement)
    ("no-choice-kc-host", "https://172.16.3.11:443", None, "harv1"),
    ("no-choice-vip", "https://172.16.3.100:443", None, None),
])
def test_join_target_cluster(tmp_path, monkeypatch, case, server_url, cluster, expect):
    kc = _kc(tmp_path, "https://172.16.3.11:6443")
    _setup(monkeypatch, kc, VIP_CM)
    monkeypatch.setattr(wapp, "load_config", lambda: {"clusters": [{"name": "harv1", "kubeconfig": kc}]})
    assert wapp._join_cluster(server_url, cluster) == expect, case


def test_clusters_list_carries_nodes_without_reading_kubeconfigs(tmp_path, monkeypatch):
    cfg = {"clusters": [{"name": "harv1", "kubeconfig": "/none.yaml", "description": "lab",
                         "nodes": [{"hostname": "n1", "ip": "172.16.3.11", "role": "control-plane"}, "bad"]}]}
    monkeypatch.setattr(wapp, "load_config", lambda: cfg)
    monkeypatch.setattr(wapp, "_config_cluster_names", lambda: {"harv1"})
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "htpasswd")
    monkeypatch.setattr(wapp, "_cluster_api_endpoint", lambda kc: pytest.fail("kubeconfig parsed"))
    with wapp.app.test_client() as c:
        d = c.get("/api/clusters").get_json()
    (harv1,) = d["clusters"]
    assert harv1["nodes"] == [{"hostname": "n1", "ip": "172.16.3.11", "role": "control-plane"}]
    assert "join_url" not in harv1 and "kubeconfig" not in harv1 and "ssh" not in harv1


def test_install_resolves_the_chosen_cluster(monkeypatch):
    """L'installation et les cartes VM passent par _join_cluster avec le
    cluster choisi (mutation : revenir à _cluster_for_server fait échouer)."""
    seen = []
    monkeypatch.setattr(wapp, "_join_cluster", lambda url, cluster=None: seen.append((url, cluster)) or None)
    data = {"mode": "join", "server_url": "https://172.16.3.100:443", "join_cluster": "harv1",
            "hostname": "n9", "bmc_host": "192.0.2.3", "bmc_user": "u", "bmc_password": "mock-only",
            "iso": "harvester.iso", "device": "/dev/sda", "mgmt_interfaces": ["aa:bb:cc:dd:ee:ff"],
            "token": "mock-token"}
    with wapp.app.test_request_context():
        result, error = wapp._bm_prepare_install(data)
    assert result is None and error[1] == 400
    assert seen[0] == ("https://172.16.3.100:443", "harv1")
    with pytest.raises(ValueError):
        wapp._bm_vm_plan(dict(data, vm_network={"template": "t", "macs": []}), {})
    assert seen[-1] == ("https://172.16.3.100:443", "harv1")
