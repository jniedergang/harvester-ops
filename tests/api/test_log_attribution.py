"""v1.22.0 — sur quel cluster cet évènement est-il survenu ?

Avec un seul cluster déclaré, la question ne se posait pas. Avec plusieurs,
l'interface répondait (colonne Cluster dans Activité, `action → cluster`
dans le dock) mais les journaux, non :

  * côté CLI, le nom du cluster n'était QUE dans le nom du fichier. Une
    ligne recopiée dans un ticket ne disait plus rien ;
  * côté serveur, « kubectl get nodes failed » ne disait pas sur quoi —
    précisément la ligne qu'on lit quand ça va mal.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
COMMON = ROOT / "bin" / "lib" / "common.sh"

sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402


def _run_logging(tmp_path, script, env=None):
    """Source la bibliothèque et exécute `script`, journal dans tmp_path."""
    full = f'''
      set -euo pipefail
      export HARVESTER_OPS_LOG_DIR="{tmp_path}"
      export NO_COLOR=1
      source "{COMMON}"
      {script}
    '''
    r = subprocess.run(["bash", "-c", full], capture_output=True, text=True,
                       env={**dict(__import__("os").environ), **(env or {})})
    assert r.returncode == 0, r.stderr[-2000:]
    logs = sorted(tmp_path.glob("*.log"))
    return r, (logs[0].read_text() if logs else ""), logs


def test_cli_log_carries_a_self_sufficient_header(tmp_path):
    _, content, logs = _run_logging(tmp_path, '''
        CLUSTER_NAME=prod
        KUBECONFIG_PATH=/home/someone/.kube/whatever.yaml
        init_logging shutdown
    ''', env={"HARVESTER_OPS_VERSION": "9.9.9"})
    head = content.splitlines()[:3]
    assert head[0].startswith("# harvester-ops v9.9.9 |")
    assert "action=shutdown" in head[0] and "cluster=prod" in head[0]
    # Une étiquette `started=` vide passait le test : c'est ainsi que
    # `date -Is`, extension GNU absente de BSD/macOS, a pu casser l'en-tête
    # sans que rien ne l'attrape.
    assert re.search(r"# started=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} \| ",
                     head[1]), head[1]
    assert re.search(r"host=\S+ \| user=\S+", head[1]), head[1]
    # le nom du fichier porte toujours le cluster, l'en-tête ne le remplace pas
    assert "-prod-shutdown.log" in logs[0].name


def test_the_header_names_the_kubeconfig_without_the_path(tmp_path):
    """Savoir QUEL kubeconfig a servi lève l'ambiguïté entre deux clusters ;
    étaler l'arborescence de la machine dans un fichier destiné au support,
    non."""
    _, content, _ = _run_logging(tmp_path, '''
        CLUSTER_NAME=prod
        KUBECONFIG_PATH=/home/someone/.kube/whatever.yaml
        init_logging shutdown
    ''')
    assert "kubeconfig=whatever.yaml" in content
    assert "/home/someone" not in content


def test_every_cli_log_line_names_its_cluster(tmp_path):
    """Une ligne extraite d'un journal doit dire d'elle-même sur quel
    cluster elle est survenue."""
    _, content, _ = _run_logging(tmp_path, '''
        CLUSTER_NAME=prod
        init_logging shutdown
        log_info "arrêt de la VM x"
        log_warn "volume encore attaché"
        log_error "échec"
        log_step "étape 3"
    ''')
    body = [l for l in content.splitlines()
            if l and not l.startswith("#") and not l.startswith("STEP_EVENT|")]
    assert body, "aucune ligne de journal produite"
    for line in body:
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[\w+\s*\] \[prod\] ",
                        line), f"ligne sans cluster : {line!r}"


def test_lines_emitted_before_the_cluster_is_known_still_work(tmp_path):
    """L'analyse des arguments journalise avant de connaître le cluster :
    ces lignes n'ont simplement pas de préfixe, et rien ne casse."""
    _, content, _ = _run_logging(tmp_path, '''
        init_logging boot
        log_info "avant de savoir"
        CLUSTER_NAME=prod
        log_info "après"
    ''')
    assert "] avant de savoir" in content
    assert "] [prod] après" in content
    assert "cluster=<none>" in content.splitlines()[0]


def test_machine_readable_events_are_left_untouched(tmp_path):
    """Le format `STEP_EVENT|...` est analysé par l'UI : y glisser le
    cluster casserait le suivi SSE."""
    _, content, _ = _run_logging(tmp_path, '''
        CLUSTER_NAME=prod
        init_logging shutdown
        emit_event preflight running "checks"
    ''')
    assert "STEP_EVENT|preflight|running|checks" in content


# ---------------------------------------------------------------------------
# Côté serveur
# ---------------------------------------------------------------------------

def test_a_kubeconfig_resolves_back_to_its_cluster_name(monkeypatch):
    """Le chemin ne suffit pas : rien n'oblige à nommer un kubeconfig
    d'après son cluster (celui de harv1 s'appelle `harvester.yaml`)."""
    monkeypatch.setattr(wapp, "load_config", lambda: {"clusters": [
        {"name": "harv1", "kubeconfig": "/home/ju/.kube/harvester.yaml"},
        {"name": "harv3", "kubeconfig": "/home/ju/.kube/harv3.yaml"},
    ]})
    assert wapp._cluster_of_kubeconfig("/home/ju/.kube/harvester.yaml") == "harv1"
    assert wapp._cluster_of_kubeconfig("/home/ju/.kube/harv3.yaml") == "harv3"
    assert wapp._cluster_of_kubeconfig("/tmp/unknown.yaml") == "?"
    assert wapp._cluster_of_kubeconfig(None) == "?"


def test_a_failed_kubectl_call_names_the_cluster(monkeypatch, caplog):
    """LA ligne qu'on lit quand quelque chose ne va pas."""
    monkeypatch.setattr(wapp, "load_config", lambda: {"clusters": [
        {"name": "harv3", "kubeconfig": "/nonexistent/harv3.yaml"},
    ]})
    with caplog.at_level("WARNING"):
        out = wapp._kubectl_json("/nonexistent/harv3.yaml", "get", "nodes")
    assert out is None
    assert "[harv3] kubectl get nodes failed" in caplog.text, caplog.text


def test_an_explicit_cluster_wins_over_the_lookup(monkeypatch, caplog):
    """Les appelants qui connaissent le cluster le passent : pas de
    relecture de la configuration à chaque appel de kubectl."""
    monkeypatch.setattr(wapp, "load_config",
                        lambda: (_ for _ in ()).throw(AssertionError("relu")))
    with caplog.at_level("WARNING"):
        wapp._kubectl_json("/nonexistent/x.yaml", "get", "vm", cluster="harv9")
    assert "[harv9]" in caplog.text


def test_kubectl_metric_is_broken_down_by_cluster():
    """Même question dans le plan des métriques : quel cluster échoue ?"""
    if isinstance(wapp.metric_kubectl_calls, object) and \
            not hasattr(wapp.metric_kubectl_calls, "_labelnames"):
        pytest.skip("prometheus_client absent : métrique neutralisée")
    assert "cluster" in wapp.metric_kubectl_calls._labelnames
    assert "status" in wapp.metric_kubectl_calls._labelnames


def test_the_watcher_already_named_its_cluster():
    """Régression : le veilleur d'évènements le faisait déjà, il doit
    continuer."""
    src = (ROOT / "web" / "app.py").read_text()
    assert 'log_watch.info("starting cluster watcher for %s", cluster)' in src
    assert 'log_watch.warning("%s: %s", cluster, e)' in src


def test_a_missing_kubectl_is_logged_not_raised(monkeypatch, caplog):
    """Signalé par un contributeur dont la machine n'a pas kubectl : l'appel
    remontait en FileNotFoundError non rattrapée, donc en 500 opaque. Le
    contrat est « None sur toute erreur, journalisée »."""
    monkeypatch.setattr(wapp, "load_config", lambda: {"clusters": [
        {"name": "harv3", "kubeconfig": "/tmp/harv3.yaml"},
    ]})

    def boom(*a, **kw):
        raise FileNotFoundError(2, "No such file or directory", "kubectl")
    monkeypatch.setattr(wapp.subprocess, "run", boom)

    with caplog.at_level("WARNING"):
        out = wapp._kubectl_json("/tmp/harv3.yaml", "get", "nodes")
    assert out is None, "un binaire absent doit dégrader, pas lever"
    assert "[harv3] kubectl get nodes unavailable" in caplog.text, caplog.text
