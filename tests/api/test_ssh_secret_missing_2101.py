"""2.10.1 tâche 2 : test SSH d'un cluster dont la clé référencée manque dans
OpenBao (restauration qui a perdu le secret) : 409 `secret-missing` avec la
classe et la référence, jamais une valeur, au lieu d'une erreur serveur.

Tableau : référence absente (409), référence présente (200, aucun
changement), clé locale (200). Mutation rapportée (rapport 2.10.1, tâche 2) :
« conversion retirée » -> le premier cas rend 503 `openbao-missing`.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).parent))
import app as wapp  # noqa: E402
from test_sso_person_token_2100 import KV, PREFIX, console  # noqa: E402,F401
from test_secret_identity_routes_2100 import REF_KEY, world  # noqa: E402,F401


@pytest.fixture
def client(world, monkeypatch):
    wapp.app.config["TESTING"] = True
    monkeypatch.setattr(wapp.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "ok",
                                                                               "stderr": ""})())
    world["cfg"]["clusters"][0]["nodes"] = [{"hostname": "n1", "ip": "10.0.0.1", "role": "cp"}]
    return wapp.app.test_client()


def test_missing_reference_is_409_secret_missing(world, client):
    world["bao"].kv.pop((KV, f"{PREFIX}/ssh-cluster/h1"), None)        # perdu à la restauration
    wapp._sst._STORE.clear_cache()
    r = client.post("/api/clusters/h1/test-ssh")
    assert r.status_code == 409, r.get_data(as_text=True)
    d = r.get_json()
    assert d["code"] == "secret-missing"
    assert d["class"] == "ssh-cluster" and d["reference"] == REF_KEY
    assert "hint" in d


def test_present_reference_probes(world, client):
    world["bao"].put_raw(KV, f"{PREFIX}/ssh-cluster/h1", {"private_key": "KEY-h1"})
    r = client.post("/api/clusters/h1/test-ssh")
    assert r.status_code == 200
    assert r.get_json()["results"][0]["ok"] is True
    assert "KEY-h1" not in r.get_data(as_text=True)


def test_local_key_unchanged(world, client, tmp_path):
    key = tmp_path / "id"
    key.write_text("x")
    world["cfg"]["clusters"][0]["ssh"]["key"] = str(key)
    r = client.post("/api/clusters/h1/test-ssh")
    assert r.status_code == 200 and json.dumps(r.get_json()).count("10.0.0.1") == 1
