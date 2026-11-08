"""2.9.0, relecture finale I1 : revenir à une version antérieure à la 2.9.0
alors que des secrets sont dans OpenBao (mode actif, classe migrée) ou qu'un
état OpenTofu est chiffré. Une 2.8.x lirait les références `openbao:` comme
des chemins ou des valeurs (clusters perdus, hôtes KVM invalides, référence
envoyée comme secret OIDC ou jeton) et ne saurait pas lire l'état chiffré :
le retour (et l'installation avec allow_older) est refusé, avec la liste des
gestes à faire d'abord."""
import json

import pytest

from test_self_update_182 import console, keys, make_release, sign, upload  # noqa: F401

ENCRYPTED = json.dumps({"encrypted_data": "x", "encryption_version": "v0", "meta": {}})


@pytest.fixture
def ready(console, tmp_path, monkeypatch):
    wapp, state = console
    monkeypatch.setattr(wapp, "_harvester_ops_version", lambda: "2.9.0")
    tf = tmp_path / "tf"
    tf.mkdir()
    monkeypatch.setattr(wapp, "TF_WORKSPACES", tf)
    monkeypatch.setenv("HARVESTER_OPS_TF_WORKSPACES", str(tf))
    (state / "updates").mkdir(parents=True, exist_ok=True)
    (state / "updates" / "agent.json").write_text('{"installed": "2.9.0"}')
    return wapp, state, tf


def staged(c, keys, tmp_path, version):
    (tmp_path / version).mkdir()
    a = make_release(tmp_path / version, version)
    upload(c, a)
    sign(keys, a)
    upload(c, type(a)(str(a) + ".sig"))
    return a


def settings(state, **kw):
    doc = {"mode": "local", "migrated": {}}
    doc.update(kw)
    (state / "secrets-settings.json").write_text(json.dumps(doc))


def encrypt(tf, decl="d1"):
    d = tf / "c1" / "decls" / decl
    d.mkdir(parents=True)
    (d / "terraform.tfstate").write_text(ENCRYPTED)


CASES = [
    ("mode-b1", dict(mode="b1"), False, {"mode": "b1"}),
    ("class-migrated", dict(mode="a", migrated={"kubeconfig": {"at": 1}}), False,
     {"mode": "a", "migrated": ["kubeconfig"]}),
    ("state-encrypted", dict(), True, {"encrypted": ["d1"]}),
]


@pytest.mark.parametrize("label,doc,enc,blockers", CASES, ids=[c[0] for c in CASES])
def test_rollback_below_290_refused_while_secrets_are_in_openbao(ready, keys, tmp_path, label, doc, enc, blockers):
    wapp, state, tf = ready
    settings(state, **doc)
    if enc:
        encrypt(tf)
    with wapp.app.test_client() as c:
        staged(c, keys, tmp_path, "2.8.3")
        r = c.post("/api/update/rollback", json={"archive": "harvester-ops-2.8.3.tar.gz", "confirm": "2.8.3"})
        body = r.get_json()
        assert r.status_code == 409 and body["code"] == "secrets-in-openbao", body
        assert body["blockers"] == blockers
        assert any("migrate --to local" in s for s in body["steps"])
        assert any("decrypt-state" in s for s in body["steps"])
        assert any("disable" in s for s in body["steps"])
        assert "migrate --to local" in body["hint"]
        # même refus par Installer avec allow_older
        r = c.post("/api/update/apply", json={"archive": "harvester-ops-2.8.3.tar.gz", "allow_older": True})
        assert r.status_code == 409 and r.get_json()["code"] == "secrets-in-openbao"
    assert not (state / "updates" / "request.json").exists()


def test_rollback_below_290_allowed_once_back_to_local(ready, keys, tmp_path):
    wapp, state, tf = ready
    settings(state, mode="local", console_id="abc123def456")
    with wapp.app.test_client() as c:
        staged(c, keys, tmp_path, "2.8.3")
        r = c.post("/api/update/rollback", json={"archive": "harvester-ops-2.8.3.tar.gz", "confirm": "2.8.3"})
        assert r.status_code == 202, r.get_json()
    assert json.loads((state / "updates" / "request.json").read_text())["version"] == "2.8.3"


def test_rollback_within_290_not_blocked(ready, keys, tmp_path, monkeypatch):
    wapp, state, tf = ready
    monkeypatch.setattr(wapp, "_harvester_ops_version", lambda: "2.9.1")
    settings(state, mode="b1", migrated={"kubeconfig": {"at": 1}})
    encrypt(tf)
    with wapp.app.test_client() as c:
        staged(c, keys, tmp_path, "2.9.0")
        r = c.post("/api/update/rollback", json={"archive": "harvester-ops-2.9.0.tar.gz", "confirm": "2.9.0"})
        assert r.status_code == 202, r.get_json()


def test_update_to_a_newer_release_not_blocked(ready, keys, tmp_path):
    wapp, state, tf = ready
    settings(state, mode="b1", migrated={"kubeconfig": {"at": 1}})
    with wapp.app.test_client() as c:
        staged(c, keys, tmp_path, "2.10.0")
        r = c.post("/api/update/apply", json={"archive": "harvester-ops-2.10.0.tar.gz"})
        assert r.status_code == 202, r.get_json()
