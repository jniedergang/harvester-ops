"""v1.23.0 — filtres de l'onglet Activité.

Avec plusieurs clusters déclarés, l'historique mélange tout. Le piège de ce
genre de fonctionnalité est de filtrer la page DÉJÀ CHARGÉE : « aucun échec
sur harv3 » serait alors une réponse fausse dès que le cluster est peu
actif, ses échecs étant simplement plus loin que les 50 derniers runs. Le
filtre descend donc jusqu'au SQL, et ces tests le vérifient sur un
historique volontairement plus grand que la fenêtre d'affichage.
"""

import importlib
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
app_module = importlib.import_module("app")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    d = tmp_path / "actions.db"
    monkeypatch.setattr(app_module, "ACTIONS_DB", d)
    app_module._actions_init_db()
    return d


def _row(db, **kw):
    """Écrit un run terminé directement en base."""
    now = time.time()
    rec = {"id": kw.get("id") or uuid.uuid4().hex[:12],
           "action": kw.get("action", "shutdown"),
           "cluster": kw.get("cluster", "harv1"),
           "status": kw.get("status", "done"),
           "exit_code": 0, "started_at": kw.get("started_at", now),
           "ended_at": now, "dry_run": 0, "cmd": "",
           "events": "[]", "error_summary": kw.get("error_summary")}
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO actions (id, action, cluster, status, exit_code,"
        " started_at, ended_at, dry_run, cmd, events, error_summary)"
        " VALUES (:id,:action,:cluster,:status,:exit_code,:started_at,"
        ":ended_at,:dry_run,:cmd,:events,:error_summary)", rec)
    conn.commit()
    conn.close()
    return rec["id"]


def test_the_filter_searches_the_whole_history_not_the_visible_page(db):
    """LE piège : 200 runs sur harv1 poussent le seul run harv3 hors de la
    fenêtre par défaut. Filtrer après coup ne le trouverait jamais."""
    base = time.time() - 10000
    for i in range(200):
        _row(db, cluster="harv1", started_at=base + i)
    wanted = _row(db, cluster="harv3", action="startup", started_at=base - 1)

    unfiltered = app_module._actions_db_recent(50)
    assert wanted not in [r["id"] for r in unfiltered], (
        "le run visé doit être hors de la fenêtre, sinon le test ne prouve rien")

    found = app_module._actions_db_recent(50, cluster="harv3")
    assert [r["id"] for r in found] == [wanted]


def test_each_dimension_filters(db):
    a = _row(db, cluster="harv1", action="shutdown", status="done")
    b = _row(db, cluster="harv3", action="vm-start:default/x", status="error",
             error_summary="boom sur le volume")
    ids = lambda **kw: {r["id"] for r in app_module._actions_db_recent(100, **kw)}

    assert ids(cluster="harv3") == {b}
    assert ids(status="error") == {b}
    assert ids(action="shutdown") == {a}
    assert ids(action="vm-start") == {b}, "l'action se filtre par sous-chaîne"
    assert ids(q="boom") == {b}, "la recherche libre couvre le message d'erreur"
    assert ids(q=a[:6]) == {a}, "la recherche libre couvre l'identifiant"


def test_filters_combine_as_and(db):
    _row(db, cluster="harv1", status="error")
    keep = _row(db, cluster="harv3", status="error")
    _row(db, cluster="harv3", status="done")
    got = app_module._actions_db_recent(100, cluster="harv3", status="error")
    assert [r["id"] for r in got] == [keep]


def test_a_quote_in_the_search_does_not_break_the_query(db):
    """Le filtre part en SQL : il doit être paramétré, pas concaténé."""
    _row(db, cluster="harv1", action="shutdown")
    assert app_module._actions_db_recent(10, q="' OR 1=1 --") == []
    assert app_module._actions_db_recent(10, q='"; DROP TABLE actions; --') == []
    # la table est toujours là
    assert len(app_module._actions_db_recent(10)) == 1


# ---------------------------------------------------------------------------
# Fichiers de log CLI
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,cluster,action", [
    ("20260909-104755-harv1-shutdown.log", "harv1", "shutdown"),
    ("20260910-114424-harv3-startup.log", "harv3", "startup"),
    # un nom de cluster peut contenir un tiret, une action aussi : c'est la
    # liste des actions connues qui lève l'ambiguïté.
    ("20260910-114424-harv-second-ns-stop.log", "harv-second", "ns-stop"),
])
def test_log_file_names_are_parsed_server_side(tmp_path, name, cluster, action):
    p = tmp_path / name
    p.write_text("x")
    entry = app_module._log_file_entry(p, p.stat())
    assert entry["cluster"] == cluster
    assert entry["action"] == action


def test_an_unparseable_log_name_is_not_lost(tmp_path):
    p = tmp_path / "rogue.log"
    p.write_text("x")
    entry = app_module._log_file_entry(p, p.stat())
    assert entry["cluster"] == "?" and entry["action"] == "rogue"


def test_the_shared_matcher_treats_log_files_as_done():
    """Un fichier de log n'a pas de statut : le filtre « done » doit
    quand même le retenir, sinon filtrer par statut ferait disparaître
    toute l'activité CLI."""
    entry = {"filename": "x.log", "cluster": "harv1", "action": "shutdown"}
    assert app_module._activity_matches(entry, "", "done", "", "")
    assert not app_module._activity_matches(entry, "", "error", "", "")
    assert app_module._activity_matches(entry, "harv1", "", "", "")
    assert not app_module._activity_matches(entry, "harv3", "", "", "")


# ---------------------------------------------------------------------------
# Contrat de l'endpoint
# ---------------------------------------------------------------------------

def test_activity_endpoint_reports_filters_and_totals(api):
    status, payload = api("GET", "/api/activity?cluster=nowhere")
    assert status == 200
    assert payload["filters"]["cluster"] == "nowhere"
    assert payload["filters"]["active"] is True
    assert payload["actions_done"] == [] and payload["in_progress"] == []
    # le total reste celui de TOUT l'historique : c'est le dénominateur du
    # « N sur M », il ne doit pas être compté après filtrage.
    assert payload["total"] >= payload["matched"]

    status, payload = api("GET", "/api/activity")
    assert payload["filters"]["active"] is False


def test_facets_offer_only_values_that_exist(api):
    _, payload = api("GET", "/api/activity")
    facets = payload["facets"]
    for key in ("clusters", "statuses", "actions"):
        assert key in facets and isinstance(facets[key], list)
    assert all(f for f in facets["clusters"]), "pas de valeur vide dans le menu"


def test_action_facet_keeps_the_verb_not_the_target(db, monkeypatch):
    """`vm-start:default/x` par VM ferait un menu déroulant d'une entrée
    par machine : on ne propose que le verbe."""
    _row(db, action="vm-start:default/a")
    _row(db, action="vm-start:default/b")
    _row(db, action="shutdown")
    monkeypatch.setattr(app_module, "LOG_DIR", Path("/nonexistent"))
    with app_module.ACTIONS_LOCK:
        app_module.ACTIONS.clear()
    facets = app_module._activity_facets()
    assert "vm-start" in facets["actions"]
    assert not any(":" in a for a in facets["actions"])
