"""v1.27.0 — un cluster déclaré mais éteint.

Signalé à l'usage, en sélectionnant harv3 après l'avoir arrêté : l'écran
tournait dans le vide, le voile finissait par abandonner sur son garde-fou
sans rien expliquer, et revenir sur harv1 (bien vivant) affichait « not
ready ».

Deux causes, mesurées sur le vrai harv3 hors tension :

  * côté serveur, chaque appel attendait le délai de `kubectl` avant de
    renoncer — 30 s pour le statut, 15 s pour les VMs et la topologie, et
    75 s pour le diagnostic CAPI, qui enchaîne les appels ;
  * côté navigateur, rien ne vérifiait qu'une réponse concernait encore le
    cluster affiché : l'échec tardif du cluster éteint repeignait l'écran
    du cluster suivant.

Un cluster hors tension se reconnaît en deux secondes : son serveur d'API
n'accepte pas la connexion TCP.
"""

import socket
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402

JS = ROOT / "web" / "static" / "js"


@pytest.fixture(autouse=True)
def clear_reach_cache():
    with wapp._REACH_LOCK:
        wapp._REACH_CACHE.clear()
    yield
    with wapp._REACH_LOCK:
        wapp._REACH_CACHE.clear()


def write_kubeconfig(tmp_path, server):
    kc = tmp_path / "kubeconfig.yaml"
    kc.write_text(f"""apiVersion: v1
kind: Config
current-context: ctx
contexts:
- name: ctx
  context:
    cluster: c1
clusters:
- name: c1
  cluster:
    server: {server}
""")
    return str(kc)


# ---------------------------------------------------------------------------
# Lecture du point de terminaison
# ---------------------------------------------------------------------------

def test_the_api_endpoint_is_read_from_the_current_context(tmp_path):
    kc = write_kubeconfig(tmp_path, "https://172.16.3.101:6443")
    assert wapp._cluster_api_endpoint(kc) == ("172.16.3.101", 6443)


def test_a_server_without_a_port_falls_back_to_the_scheme_default(tmp_path):
    assert wapp._cluster_api_endpoint(
        write_kubeconfig(tmp_path, "https://api.example")) == ("api.example", 443)
    assert wapp._cluster_api_endpoint(
        write_kubeconfig(tmp_path, "http://api.example")) == ("api.example", 80)


def test_an_unreadable_kubeconfig_does_not_decide(tmp_path):
    """Ne pas savoir n'est pas la même chose que savoir que c'est éteint :
    dans le doute on laisse passer, sinon un kubeconfig d'une forme
    inattendue rendrait un cluster sain inutilisable."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("ceci n'est pas un kubeconfig")
    assert wapp._cluster_api_endpoint(str(bad)) is None
    assert wapp._cluster_reachable(str(bad)) is None
    assert wapp._cluster_reachable(str(tmp_path / "absent.yaml")) is None
    assert wapp._cluster_reachable("") is None


# ---------------------------------------------------------------------------
# La sonde
# ---------------------------------------------------------------------------

def test_a_listening_endpoint_is_reachable(tmp_path):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    threading.Thread(target=lambda: srv.accept(), daemon=True).start()
    try:
        kc = write_kubeconfig(tmp_path, f"https://127.0.0.1:{port}")
        assert wapp._cluster_reachable(kc) is True
    finally:
        srv.close()


def test_a_closed_port_is_unreachable_and_fast(tmp_path):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()                       # personne n'écoute plus
    kc = write_kubeconfig(tmp_path, f"https://127.0.0.1:{port}")
    import time
    t0 = time.time()
    assert wapp._cluster_reachable(kc, timeout=2.0) is False
    assert time.time() - t0 < 3, "la sonde doit renoncer vite"


def test_the_answer_is_cached_so_one_switch_probes_once(tmp_path, monkeypatch):
    """Une bascule de cluster touche plusieurs points d'entrée. Sans cache,
    chacun rouvrirait une connexion."""
    kc = write_kubeconfig(tmp_path, "https://127.0.0.1:9")
    calls = []

    def fake_connect(addr, timeout=None):
        calls.append(addr)
        raise OSError("refused")

    monkeypatch.setattr(wapp.socket, "create_connection", fake_connect)
    for _ in range(5):
        assert wapp._cluster_reachable(kc) is False
    assert len(calls) == 1, f"{len(calls)} sondes au lieu d'une"


# ---------------------------------------------------------------------------
# Les points d'entrée
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/api/status/harv-fake",
    "/api/vms/harv-fake",
    "/api/topology/harv-fake",
    "/api/capi/harv-fake/diag",
])
def test_every_slow_endpoint_answers_unreachable_instead_of_waiting(
        path, monkeypatch):
    """Ce sont les quatre qui attendaient : 30 s, 15 s, 15 s et 75 s."""
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc, **k: False)
    monkeypatch.setattr(wapp, "_cluster_api_endpoint",
                        lambda kc: ("172.16.3.101", 6443))
    with wapp.app.test_client() as c:
        r = c.get(path)
    assert r.status_code == 200, path
    d = r.get_json()
    assert d["unreachable"] is True, path
    # L'adresse est nommée : « injoignable » sans dire où n'aide personne.
    assert d["endpoint"] == "172.16.3.101:6443", path


def test_the_capi_diag_keeps_its_normal_shape_when_the_cluster_answers(
        monkeypatch):
    """La sonde est forcée : le cluster de test est injoignable par
    construction (son kubeconfig pointe un port fermé), c'est le seul moyen
    d'exercer le chemin normal. Ce test vivait dans test_endpoints.py, qui
    parle à un serveur hors process où `monkeypatch` n'a aucun effet."""
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc, **k: True)
    # Le cluster de test n'existe que dans la config du serveur hors
    # process ; en process il faut le déclarer. Son kubeconfig est absent,
    # donc kubectl échoue vite et tous les composants ressortent à False.
    monkeypatch.setattr(wapp, "load_config", lambda: {
        "clusters": [{"name": "harv-probe", "kubeconfig": "/nonexistent.yaml"}]})
    with wapp.app.test_client() as c:
        body = c.get("/api/capi/harv-probe/diag").get_json()
    for key in ("components", "capi_clusters", "have_capi_crds",
                "bundle_available"):
        assert key in body, key
    assert all(comp["installed"] is False for comp in body["components"])


def test_a_reachable_cluster_is_not_blocked(monkeypatch):
    """Garde-fou : la sonde ne doit jamais barrer la route d'un cluster
    sain, ni d'un cluster dont on n'a pas pu lire le kubeconfig."""
    for verdict in (True, None):
        monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc, **k: verdict)
        with wapp.app.test_client() as c:
            r = c.get("/api/topology/harv-fake")
        assert (r.get_json() or {}).get("unreachable") is not True, verdict


# ---------------------------------------------------------------------------
# Côté navigateur
# ---------------------------------------------------------------------------

def test_the_views_drop_a_response_that_changed_cluster():
    """La garde anti-réponse-tardive : sans elle, l'échec du cluster éteint
    arrivait après la bascule et s'affichait comme l'état du suivant."""
    src = (JS / "app.js").read_text()
    for fn in ("refreshStatusInner", "refreshNamespaces"):
        body = src.split(f"function {fn}(", 1)[1][:1200]
        assert "const asked = currentCluster" in body, fn
        assert "asked !== currentCluster) return" in body, fn


def test_an_empty_component_list_is_not_reported_as_fully_installed():
    """`[].every()` vaut TRUE en JavaScript : une réponse sans composants
    affichait « CAPI/CAPHV stack fully installed » en vert sur un cluster
    qui ne répondait même pas."""
    src = (JS / "capi.js").read_text()
    assert "comps.length > 0 && comps.every(c => c.installed)" in src, \
        "le contrôle de longueur manque : le vert serait vide de sens"
    assert "if (d.unreachable)" in src


def test_the_unreachable_message_names_the_cluster():
    i18n = (JS / "i18n.js").read_text()
    assert i18n.count("'overview.clusterUnreachable'") == 5, "5 langues attendues"
    assert "{name}" in i18n.split("'overview.clusterUnreachable':", 1)[1][:200]
