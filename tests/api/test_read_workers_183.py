"""v1.83.0 : processus lecteurs (web/read_workers.py). Un vrai lecteur est
lancé (spawn) et calcule la liste des VMs contre un faux serveur d'API, avec le
kubeconfig, le rôle et la personne imposés par la console ; les refus de la
RBAC rencontrés reviennent avec la réponse."""
import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "tests" / "api"))
from test_kube_rest_183 import make_handler  # noqa: E402


@pytest.fixture
def workers(tmp_path, monkeypatch):
    seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(seen))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    kc = tmp_path / "kc.yaml"
    kc.write_text(json.dumps({
        "current-context": "c", "contexts": [{"name": "c", "context": {"cluster": "c", "user": "u"}}],
        "clusters": [{"name": "c", "cluster": {"server": f"http://127.0.0.1:{srv.server_port}"}}],
        "users": [{"name": "u", "user": {"token": "t"}}]}))
    # le lecteur hérite de l'environnement au démarrage : lectures directes
    # actives chez lui, un seul lecteur
    monkeypatch.setenv("HARVESTER_OPS_KUBE_REST", "1")
    monkeypatch.setenv("HARVESTER_OPS_READ_WORKERS", "1")
    import read_workers as rw
    rw.shutdown()
    yield rw, str(kc), seen
    rw.shutdown()
    srv.shutdown()


def test_a_worker_renders_the_vm_list_with_the_given_identity(workers):
    rw, kc, seen = workers
    status, body, mimetype, headers, denied, denials = rw.render(
        "api_vms_list", {"cluster": "anything"}, "", kc, "viewer", "alice")
    assert status == 200 and mimetype == "application/json"
    names = sorted(v["name"] for v in json.loads(body)["vms"])
    assert names == ["a", "b"]
    # les VMIs sont refusées par le faux serveur : le refus revient
    assert denied and "forbidden" in denied
    assert denials and denials[0]["resource"] == "virtualmachineinstances"
    # le lecteur a bien interrogé l'API lui-même
    assert any(p.startswith("/apis/kubevirt.io/v1/virtualmachines") for p, _ in seen)


def test_the_console_falls_back_when_no_worker(workers, monkeypatch):
    rw, kc, _ = workers
    monkeypatch.setenv("HARVESTER_OPS_READ_WORKERS", "0")
    rw.shutdown()
    with pytest.raises(RuntimeError):
        rw.render("api_vms_list", {"cluster": "x"}, "", kc, "viewer", "a")


def test_a_worker_never_watches_nor_tracks():
    """Source : les effets de démarrage sont gardés par IS_READ_WORKER, qui
    vaut aussi pour l'import de app.py par « spawn » sous __mp_main__."""
    src = (ROOT / "web" / "app.py").read_text()
    assert 'or (__name__ == "__mp_main__"' in src
    for guarded in ("_start_cluster_watchers()", "_capi_bundle_migrate_legacy()",
                    'threading.Thread(target=_update_outcome_watch'):
        i = src.rindex(guarded)
        assert "if not IS_READ_WORKER:" in src[i - 80:i], guarded
    rws = (ROOT / "web" / "read_workers.py").read_text()
    assert "os.getppid() != ppid" in rws and "max_tasks_per_child" in rws
    # PR_SET_PDEATHSIG suit le fil créateur (une requête) : jamais
    assert "prctl" not in rws
    assert 'sys.modules["app"] = main' in rws


def test_heavy_reads_are_shared_reads():
    """Seules des vues de lecture partagées partent chez les lecteurs (la clé
    de partage porte l'identité, le lecteur la reçoit)."""
    import app as wapp
    src = (ROOT / "web" / "app.py").read_text().splitlines()
    for endpoint in wapp.HEAVY_READS:
        line = next(i for i, l in enumerate(src) if l.startswith(f"def {endpoint}("))
        assert src[line - 1].startswith("@shared_read("), endpoint


def test_a_worker_born_in_a_short_lived_thread_survives(workers):
    """Vu sur le banc : un lecteur créé depuis le fil d'une requête mourait
    avec ce fil (PR_SET_PDEATHSIG)."""
    rw, kc, _ = workers
    out = {}

    def request_thread():
        out["first"] = rw.render("api_vms_list", {"cluster": "x"}, "", kc, "viewer", "a")[0]
    t = threading.Thread(target=request_thread)
    t.start()
    t.join()
    import time
    time.sleep(1)
    assert out["first"] == 200
    assert rw.render("api_vms_list", {"cluster": "x"}, "", kc, "viewer", "a")[0] == 200
