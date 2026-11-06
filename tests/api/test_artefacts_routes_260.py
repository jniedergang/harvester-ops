"""v2.6.0 : paquets d'artefacts dans la console (tâche 5) : téléversement en
flux, pré-vérification, acquittement, import par l'outil en action suivie
(journal), mode strict et verrou de config.yaml, clés de confiance ; aucun
chemin absolu dans les réponses."""

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp  # noqa: E402
import artefacts as art  # noqa: E402
import test_deploy_routes_240 as tr  # noqa: E402
import test_harvester_artefacts_cli_260 as tcli  # noqa: E402

env = tr.env
keys = tcli.keys
needs_keygen = pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="ssh-keygen needed")


def upload(c, path, name=None):
    data = Path(path).read_bytes()
    return c.post(f"/api/artefacts/upload?name={name or Path(path).name}", data=data,
                  headers={"Content-Type": "application/octet-stream", "Content-Length": str(len(data))})


def wait(aid, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        run = wapp.ACTIONS.get(aid)
        if run is not None and run.status in ("done", "error", "cancelled"):
            return run
        time.sleep(0.05)
    raise AssertionError("action did not finish")


def journal(run):
    return [e.get("message", "") for e in run.events if e.get("type") == "step"]


@pytest.fixture
def pkg(tmp_path):
    d = tmp_path / "build"
    d.mkdir()
    return tcli.small_package(d)


def no_paths(resp, tmp_path):
    assert str(tmp_path) not in json.dumps(resp.get_json())


def test_unsigned_package_needs_its_acknowledgement_then_is_imported(env, pkg, tmp_path):
    c = env["client"]
    r = upload(c, pkg)
    assert r.status_code == 200
    staged = r.get_json()["staged"]
    assert staged["acknowledge_needed"] == "unsigned" and staged["trust"]["state"] == "unsigned"
    no_paths(r, tmp_path)
    up = wait(r.get_json()["action_id"])
    assert up.action == "artefacts-import:socle-2026.11" and up.status == "done"
    r = c.post("/api/artefacts/socle/2026.11/accept", json={})
    assert r.status_code == 409 and r.get_json()["code"] == "acknowledge-required"
    assert r.get_json()["state"] == "unsigned"
    r = c.post("/api/artefacts/socle/2026.11/accept", json={"acknowledge": "unknown-key"})   # faux acquittement
    assert r.status_code == 409 and r.get_json()["code"] == "acknowledge-required"
    r = c.post("/api/artefacts/socle/2026.11/accept", json={"acknowledge": "unsigned"})
    assert r.status_code == 202, r.get_json()
    run = wait(r.get_json()["action_id"])
    assert run.status == "done", journal(run)
    assert "accepted UNSIGNED package socle-2026.11 after acknowledgement by alice" in journal(run)
    lst = c.get("/api/artefacts").get_json()
    assert [(p["name"], p["trust"], p["imported_by"]) for p in lst["packages"]] == [("socle", "unsigned", "alice")]
    detail = c.get("/api/artefacts/socle/2026.11")
    assert detail.status_code == 200 and detail.get_json()["trust"]["person"] == "alice"
    assert detail.get_json()["index"]["items"][0]["name"] == "ns" and "path" not in detail.get_json()
    no_paths(detail, tmp_path)
    assert not list((env["state"] / "artefacts" / "incoming").glob("socle*"))      # archive retirée
    r = c.post("/api/artefacts/socle/2026.11/accept", json={"acknowledge": "unsigned"})
    assert r.status_code == 404                                                    # plus rien à accepter


@needs_keygen
@pytest.mark.parametrize("state,ack,status,code", [
    ("valid", None, 202, None),
    ("unknown-key", None, 409, "acknowledge-required"),
    ("unknown-key", "unknown-key", 202, None),
    ("invalid", "unsigned", 422, "invalid-signature"),
])
def test_signed_packages_follow_the_decision_table(env, keys, pkg, state, ack, status, code):
    c = env["client"]
    art.save_signers(env["state"], (keys / "signers").read_text())
    if state == "invalid":
        other = pkg.parent / "other"
        other.write_bytes(b"x")
        subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(keys / "client"), "-n", art.SIG_NAMESPACE,
                        str(other)], check=True)
        sig = Path(str(other) + ".sig")
    else:
        tcli.run("sign", pkg, "--key", keys / ("client" if state == "valid" else "stranger"), check=0)
        sig = Path(str(pkg) + ".sig")
    upload(c, pkg)
    r = upload(c, sig, name=pkg.name + ".sig")
    assert r.get_json()["staged"]["trust"]["state"] == state
    r = c.post("/api/artefacts/socle/2026.11/accept", json={"acknowledge": ack} if ack else {})
    assert r.status_code == status, r.get_json()
    if code:
        assert r.get_json()["code"] == code
    else:
        run = wait(r.get_json()["action_id"])
        assert run.status == "done"
        line = [m for m in journal(run) if m.startswith("accepted")][0]
        assert ("signed by ops@client" in line) if state == "valid" else ("UNKNOWN-KEY" in line and "alice" in line)


def test_strict_mode_refuses_and_its_lock_comes_from_config(env, pkg, monkeypatch):
    c = env["client"]
    r = c.put("/api/artefacts/settings", json={"strict": True})
    assert r.status_code == 200 and r.get_json()["strict"] is True and r.get_json()["action_id"]
    upload(c, pkg)
    r = c.post("/api/artefacts/socle/2026.11/accept", json={"acknowledge": "unsigned"})
    assert r.status_code == 409 and r.get_json()["code"] == "strict-mode"
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {"clusters": [], "artefacts": {"strict": True}})
    s = c.get("/api/artefacts/settings").get_json()
    assert s["locked"] is True and s["strict"] is True
    r = c.put("/api/artefacts/settings", json={"strict": False})
    assert r.status_code == 409 and r.get_json()["code"] == "strict-locked"


@needs_keygen
def test_trusted_keys_are_added_and_removed_by_fingerprint(env, keys):
    c = env["client"]
    pub = (keys / "client.pub").read_text().split()
    r = c.put("/api/artefacts/settings", json={"add_signer": f"ops@client {pub[0]} {pub[1]}"})
    assert r.status_code == 200
    signers = r.get_json()["signers"]
    assert [(s["principal"], s["key_type"]) for s in signers] == [("ops@client", "ssh-ed25519")]
    assert pub[1] not in json.dumps(r.get_json())                         # la matière de la clé n'est pas renvoyée
    act = wait(r.get_json()["action_id"])
    assert any("trusted key added: ops@client SHA256:" in m for m in journal(act))
    r = c.put("/api/artefacts/settings", json={"add_signer": "ops ssh-rsa AAAA"})
    assert r.status_code == 400 and r.get_json()["code"] == "signers-invalid"
    r = c.put("/api/artefacts/settings", json={"remove_signer": signers[0]["fingerprint"]})
    assert r.status_code == 200 and r.get_json()["signers"] == []
    assert c.put("/api/artefacts/settings", json={"remove_signer": "SHA256:x"}).status_code == 404
    assert c.put("/api/artefacts/settings", json={"colour": 1}).status_code == 400


@pytest.mark.parametrize("name,status", [
    ("socle.tar.gz", 400),
    ("socle-latest.tar.gz", 400),
    ("socle-1x.tar.gz", 400),
    ("../socle-2026.11.tar.gz", 400),
])
def test_bad_archive_names_are_refused(env, name, status):
    r = env["client"].post(f"/api/artefacts/upload?name={name}", data=b"x",
                           headers={"Content-Length": "1"})
    assert r.status_code == status


def test_no_room_is_507_before_receiving(env, pkg, monkeypatch):
    class St:
        f_bavail, f_frsize = 10, 4096
    monkeypatch.setattr(wapp.os, "statvfs", lambda p: St())
    r = upload(env["client"], pkg)
    assert r.status_code == 507 and r.get_json()["need"] > 1 << 30


def test_a_broken_archive_is_said_at_upload(env, tmp_path):
    bad = tmp_path / "socle-2026.11.tar.gz"
    bad.write_bytes(b"not a tar")
    r = upload(env["client"], bad)
    staged = r.get_json()["staged"]
    assert staged["ok"] is False and staged["code"] == "not-a-package"
    assert wait(r.get_json()["action_id"]).status == "error"


def test_delete_is_tracked_and_versions_are_checked(env, pkg):
    c = env["client"]
    upload(c, pkg)
    wait(c.post("/api/artefacts/socle/2026.11/accept", json={"acknowledge": "unsigned"}).get_json()["action_id"])
    r = c.delete("/api/artefacts/socle/2026.11")
    assert r.status_code == 200 and wait(r.get_json()["action_id"]).action == "artefacts-remove:socle-2026.11"
    assert c.delete("/api/artefacts/socle/2026.11").status_code == 404
    assert c.get("/api/artefacts/socle/latest").status_code == 400
    assert c.get("/api/artefacts/socle/2026.12").status_code == 404


def test_viewers_and_operators_cannot_import(env, pkg, monkeypatch):
    monkeypatch.setattr(wapp, "open_mode", lambda: True)
    for role in ("viewer", "operator"):
        monkeypatch.setattr(wapp, "current_role", lambda role=role: role)
        assert upload(env["client"], pkg).status_code == 403
        assert env["client"].put("/api/artefacts/settings", json={"strict": True}).status_code == 403
        assert env["client"].get("/api/artefacts").status_code == 200


def test_limits_of_the_package_routes_are_readable():
    from limits import parse_many
    src = (ROOT / "web" / "app.py").read_text()
    for fn in ("api_artefacts_upload", "api_artefacts_accept", "api_artefacts_delete", "api_artefacts_settings_put"):
        i = src.index(f"def {fn}(")
        parse_many(re.findall(r'@_rate_limit\("([^"]+)"\)', src[i - 160:i])[-1])
