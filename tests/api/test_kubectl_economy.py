"""v1.33.0 : économie des appels kubectl.

Audit mené sur harv1 avec un shim qui journalise chaque invocation réelle.
Point de départ mesuré : un seul utilisateur posé sur la page d'aperçu
déclenchait **54 appels kubectl et 70 s de temps kubectl cumulé par minute**,
soit 117 % d'un cœur, pour un écran que personne ne touchait.

Trois faits que l'audit a établis et que ces tests figent :

  * chaque invocation de kubectl paie ~0,7 s de démarrage de processus AVANT
    de toucher au réseau. Le nombre d'appels compte donc autant que ce qu'ils
    rapportent, et `kubectl get a,b,c` en un tour vaut mieux que trois tours ;
  * `kubectl get crd` rapatrie les objets ENTIERS, schémas OpenAPI compris :
    37,9 Mo et ~24 s sur harv1, quel que soit le format de sortie demandé,
    qui n'agit que sur le rendu côté client. Le diagnostic CAPI ne lui
    accordait que 8 s, donc il expirait À TOUS LES COUPS et concluait que la
    pile CAPI était absente alors qu'elle était installée ;
  * du travail périodique sans destinataire reste du travail : onglet de
    navigateur caché, console que personne n'utilise.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402


# ---------------------------------------------------------------------------
# Diagnostic CAPI : ne jamais redemander `get crd`
# ---------------------------------------------------------------------------

def test_the_capi_diagnostic_does_not_list_crds():
    """`get crd` prenait 24 s pour 8 s accordées : il expirait toujours, et
    le diagnostic déclarait la pile absente. C'est la régression à empêcher,
    pas seulement une lenteur."""
    src = (ROOT / "web" / "app.py").read_text()
    block = src.split("def api_capi_diag(", 1)[1][:4000]
    assert '"get", "crd"' not in block
    assert '"api-resources"' in block


def test_api_resource_names_reads_the_columns_from_the_end():
    """SHORTNAMES manque sur une ligne sur deux : compter depuis le début
    décalerait la version d'API et produirait des noms faux."""
    sortie = (
        "bindings                v1        true   Binding\n"
        "componentstatuses  cs   v1        false  ComponentStatus\n"
        "clusters           cl   cluster.x-k8s.io/v1beta1  true  Cluster\n"
        "harvesterclusters       infrastructure.cluster.x-k8s.io/v1beta1  true  HarvesterCluster\n"
    )
    noms = wapp._api_resource_names(sortie)
    assert "bindings" in noms                      # groupe vide : pas de point
    assert "componentstatuses" in noms             # avec raccourci
    assert "clusters.cluster.x-k8s.io" in noms     # avec raccourci ET groupe
    assert "harvesterclusters.infrastructure.cluster.x-k8s.io" in noms


def test_the_names_answer_the_question_the_diagnostic_asks():
    """Les deux tests que fait le diagnostic doivent tomber juste sur ces
    noms, sinon l'optimisation casse le résultat qu'elle accélère."""
    noms = wapp._api_resource_names(
        "clusters  cl  cluster.x-k8s.io/v1beta1  true  Cluster\n"
        "harvestermachines  infrastructure.cluster.x-k8s.io/v1beta1  true  HarvesterMachine\n")
    assert any(c.startswith("clusters.cluster.x-k8s.io") for c in noms)
    assert any(c.endswith(".infrastructure.cluster.x-k8s.io") and "harvester" in c
               for c in noms)


def test_garbage_lines_are_skipped_rather_than_crashing():
    assert wapp._api_resource_names("") == set()
    assert wapp._api_resource_names("bruit\n\n  \n") == set()


# ---------------------------------------------------------------------------
# Surveillance de cluster : un appel au lieu de cinq
# ---------------------------------------------------------------------------

def test_every_watched_resource_declares_the_kind_it_returns():
    """C'est ce champ qui permet de démultiplexer un appel groupé. Un type
    ajouté sans lui ferait disparaître sa surveillance en silence."""
    for entry in wapp.CLUSTER_WATCH_RESOURCES:
        assert len(entry) == 4, entry
        label, kind, scope, api_kind = entry
        assert scope in ("cluster", "namespaced")
        assert api_kind and api_kind[0].isupper(), api_kind


def test_the_watcher_makes_one_call_for_all_resources(monkeypatch):
    calls = []

    class Done:
        returncode = 0
        stderr = ""
        stdout = ('{"items": ['
                  '{"kind": "Namespace", "metadata": {"uid": "u1", "name": "default"}},'
                  '{"kind": "VirtualMachine", "metadata": {"uid": "u2", "namespace": "d",'
                  ' "name": "vm1"}, "spec": {}, "status": {}}'
                  ']}')

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return Done()

    monkeypatch.setattr(wapp.subprocess, "run", fake_run)
    snaps = wapp._cluster_snapshot_all("/kc", wapp.CLUSTER_WATCH_RESOURCES)
    assert len(calls) == 1, f"{len(calls)} appels au lieu d'un seul"
    assert "," in calls[0][-2] or "," in " ".join(calls[0])
    assert snaps["namespaces"]["u1"]["name"] == "default"
    assert snaps["virtualmachines.kubevirt.io"]["u2"]["name"] == "d/vm1"


def test_the_watch_loop_actually_uses_the_grouped_collector():
    """Vérifier que le collecteur groupe NE SUFFIT PAS : la boucle pourrait
    très bien continuer à appeler un type après l'autre à côté de lui. C'est
    ce qu'un sabotage a montré, ce test passant alors sans rien voir."""
    src = (ROOT / "web" / "app.py").read_text()
    bloc = src.split("def _cluster_watch_iteration(", 1)[1][:1500]
    assert "_cluster_snapshot_all(" in bloc
    assert "_cluster_snapshot(kc," not in bloc, \
        "la boucle rappelle kubectl type par type"


def test_an_unknown_resource_type_falls_back_per_kind(monkeypatch):
    """`kubectl get a,b,c` échoue EN BLOC si le cluster n'expose pas l'un des
    types. Sans repli, un cluster sans Harvester perdrait aussi la
    surveillance des namespaces et des PVC, ce qui serait pire que lent."""
    calls = []

    class Fail:
        returncode = 1
        stdout = ""
        stderr = 'error: the server doesn\'t have a resource type "x"'

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return Fail()

    monkeypatch.setattr(wapp.subprocess, "run", fake_run)
    snaps = wapp._cluster_snapshot_all("/kc", wapp.CLUSTER_WATCH_RESOURCES)
    # 1 tentative groupée + 1 par type
    assert len(calls) == 1 + len(wapp.CLUSTER_WATCH_RESOURCES)
    assert set(snaps) == {k for _, k, _, _ in wapp.CLUSTER_WATCH_RESOURCES}


def test_both_paths_reduce_an_object_identically():
    """Si le chemin groupé et le chemin par type ne réduisaient pas de la
    même façon, le premier repli ferait voir de faux changements et
    inonderait l'activité d'événements imaginaires."""
    item = {"kind": "VirtualMachine",
            "metadata": {"uid": "u", "namespace": "ns", "name": "vm",
                         "resourceVersion": "42"},
            "spec": {"runStrategy": "Always"},
            "status": {"ready": True, "printableStatus": "Running"}}
    reduit = wapp._snapshot_items("virtualmachines.kubevirt.io", [item])
    assert reduit["u"]["name"] == "ns/vm"
    assert reduit["u"]["rv"] == "42"
    assert reduit["u"]["extra"]["run_strategy"] == "Always"


# ---------------------------------------------------------------------------
# Ne pas travailler pour personne
# ---------------------------------------------------------------------------

def test_the_watcher_slows_down_when_nobody_uses_the_console(monkeypatch):
    monkeypatch.setattr(wapp, "_last_request_ts", wapp.time.time())
    assert wapp._console_is_idle() is False
    monkeypatch.setattr(wapp, "_last_request_ts",
                        wapp.time.time() - wapp.CLUSTER_WATCH_IDLE_AFTER - 1)
    assert wapp._console_is_idle() is True


def test_monitoring_scrapes_do_not_count_as_a_human(monkeypatch):
    """Prometheus interroge `/metrics` sans relâche. Le compter garderait la
    console éveillée pour toujours et le ralentissement ne servirait jamais."""
    monkeypatch.setattr(wapp, "_last_request_ts", 0.0)
    with wapp.app.test_client() as c:
        c.get("/metrics")
        c.get("/healthz")
    assert wapp._last_request_ts == 0.0, "une sonde a réveillé la console"
    with wapp.app.test_client() as c:
        c.get("/api/clusters")
    assert wapp._last_request_ts > 0.0, "une vraie requête n'a pas compté"


def test_a_hidden_browser_tab_stops_polling_the_cluster():
    """L'aperçu interrogeait le cluster toutes les 8 s même sur un onglet que
    personne ne regarde. dock.js faisait déjà ce test ; app.js l'ignorait."""
    src = (ROOT / "web" / "static" / "js" / "app.js").read_text()
    bloc = src.split("statusRefreshTimer = setInterval(", 1)[1][:900]
    assert "document.hidden" in bloc
    # Et le retour sur l'onglet doit rafraîchir tout de suite, sinon on
    # échange l'économie contre huit secondes d'écran périmé.
    assert "visibilitychange" in src


# ---------------------------------------------------------------------------
# Le script de statut
# ---------------------------------------------------------------------------

def test_the_status_script_groups_and_parallelises():
    """Cinq `kubectl get` coûtaient 9,9 s mesurés de bout en bout ; deux
    appels groupés lancés de front en coûtent 5,6 s, pour une sortie
    strictement identique (vérifiée sur harv1)."""
    src = (ROOT / "bin" / "harvester-status.sh").read_text()
    assert "nodes,vm,vmi" in src
    assert "volumes.longhorn.io,settings.longhorn.io" in src
    assert "ThreadPoolExecutor" in src
    # Les appels un par un ne doivent pas revenir par la bande.
    assert '"get", "vmi", "-A"' not in src
    assert '"get", "nodes", "-o", "json"' not in src


def test_the_status_script_still_parses():
    r = subprocess.run(["bash", "-n", str(ROOT / "bin" / "harvester-status.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
