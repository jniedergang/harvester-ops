"""v1.77.0 : routes de la configuration d'installation complète.

* `POST /api/baremetal/config/parse` découpe un fichier importé ; jeton et
  mot de passe restent côté serveur, par personne, 15 minutes ;
* `POST /api/baremetal/config/preview` rend le YAML final, secrets masqués ;
* `POST /api/baremetal/install` valide la configuration AVANT de créer
  l'ActionRun : le runner ne la rend qu'après avoir allumé la machine.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
WEB = ROOT / "web"
sys.path.insert(0, str(WEB))

import app as wapp          # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "bm_config_177" / "operator.yaml"
TOKEN = "TOKSECRET-4f1c9a"
PASSWORD = "PWSECRET-77b2e0"


def _operator_text():
    text = FIXTURE.read_text()
    assert "token: example-token" in text and "password: example-password" in text
    return (text.replace("token: example-token", f"token: {TOKEN}")
                .replace("password: example-password", f"password: {PASSWORD}"))


def _no_secret(resp):
    body = resp.get_data(as_text=True)
    assert TOKEN not in body and PASSWORD not in body, body[:300]


@pytest.fixture()
def client(monkeypatch):
    wapp.app.config["TESTING"] = True
    monkeypatch.setattr(wapp, "_BM_IMPORT_CACHE", {})
    monkeypatch.setattr(wapp, "current_user", lambda: "alice")
    monkeypatch.setattr(wapp, "current_cluster_identity", lambda: None)
    return wapp.app.test_client()


@pytest.fixture()
def runs(monkeypatch):
    seen = []
    monkeypatch.setattr(wapp, "track_action",
                        lambda label, cluster, worker, *a: seen.append((label, a)) or "a1")
    return seen


BMC = {"bmc_host": "192.0.2.10", "bmc_user": "u", "bmc_password": "BMCSECRET",
       "iso": "harvester.iso"}


def _parse(client, text=None):
    r = client.post("/api/baremetal/config/parse", json={"text": text or _operator_text()})
    _no_secret(r)
    return r


def _install_body(parsed, **extra):
    body = dict(parsed["form"], **BMC)
    body["advanced_yaml"] = parsed["advanced"]
    body["import_id"] = parsed["import_id"]
    body.update(extra)
    return body


# --- parse -------------------------------------------------------------------

def test_parse_returns_form_and_keeps_secrets_server_side(client):
    r = _parse(client)
    assert r.status_code == 200
    j = r.get_json()
    assert set(j) == {"form", "advanced", "notes", "import_id", "has_token", "has_password"}
    assert j["has_token"] is True and j["has_password"] is True
    assert len(j["import_id"]) >= 22            # 128 bits en base64 url
    assert j["form"]["hostname"]
    assert "token" not in j["form"] and "password" not in j["form"]


def test_parse_refusal_lists_paths_without_values(client):
    text = _operator_text() + "\nbogus_key: " + TOKEN + "\n"
    r = _parse(client, text)
    assert r.status_code == 400
    j = r.get_json()
    assert "bogus_key" in j["errors"] and j["fields"] == j["errors"]
    assert "import_id" not in j


def test_parse_invalid_yaml_does_not_echo_the_text(client):
    r = _parse(client, f"token: {TOKEN}\nos: [unclosed\n")
    assert r.status_code == 400


def test_parse_size_limit(client):
    ok = "# " + "x" * (wapp._BM_IMPORT_MAX - 10) + "\n"
    assert client.post("/api/baremetal/config/parse", json={"text": ok}).status_code == 200
    big = "# " + "x" * wapp._BM_IMPORT_MAX + "\n"
    r = client.post("/api/baremetal/config/parse", json={"text": big})
    assert r.status_code == 413
    assert r.get_json()["max_bytes"] == 256 * 1024


def test_parse_requires_text(client):
    r = client.post("/api/baremetal/config/parse", json={"text": 3})
    assert r.status_code == 400 and r.get_json()["fields"] == ["text"]


# --- cache par personne --------------------------------------------------------

def test_import_id_is_bound_to_its_person(client, monkeypatch, runs):
    parsed = _parse(client).get_json()
    monkeypatch.setattr(wapp, "current_user", lambda: "bob")
    r = client.post("/api/baremetal/install", json=_install_body(parsed))
    assert r.status_code == 400
    assert r.get_json() == {"error": "import expired", "fields": ["import_id"]}
    r = client.post("/api/baremetal/config/preview", json=_install_body(parsed))
    assert r.status_code == 400 and r.get_json()["fields"] == ["import_id"]
    assert runs == []


def test_import_id_is_bound_to_the_delegated_identity(client, monkeypatch, runs):
    parsed = _parse(client).get_json()
    monkeypatch.setattr(wapp, "current_cluster_identity", lambda: {"user": "other"})
    r = client.post("/api/baremetal/install", json=_install_body(parsed))
    assert r.status_code == 400 and runs == []


def test_import_expires_after_15_minutes(client, monkeypatch, runs):
    now = [1_000_000.0]
    monkeypatch.setattr(wapp.time, "time", lambda: now[0])
    parsed = _parse(client).get_json()
    now[0] += wapp._BM_IMPORT_TTL - 1
    assert client.post("/api/baremetal/config/preview",
                       json=_install_body(parsed)).status_code == 200
    now[0] += 2
    r = client.post("/api/baremetal/install", json=_install_body(parsed))
    assert r.status_code == 400 and r.get_json()["error"] == "import expired"
    assert parsed["import_id"] not in wapp._BM_IMPORT_CACHE      # purgé
    assert runs == []
    assert wapp._BM_IMPORT_TTL == 15 * 60


def test_unknown_import_id_is_refused(client, runs):
    parsed = _parse(client).get_json()
    r = client.post("/api/baremetal/install", json=_install_body(parsed, import_id="nope"))
    assert r.status_code == 400 and runs == []


# --- aperçu --------------------------------------------------------------------

def test_preview_masks_secrets_and_needs_no_bmc(client):
    parsed = _parse(client).get_json()
    body = _install_body(parsed)
    for k in BMC:
        body.pop(k)
    r = client.post("/api/baremetal/config/preview", json=body)
    _no_secret(r)
    assert r.status_code == 200, r.get_json()
    cfg = wapp.yaml.safe_load(r.get_json()["yaml"])
    assert cfg["token"] == "•••" and cfg["os"]["password"] == "•••"
    assert cfg["install"]["iso_url"] == wapp._BM_ISO_URL_PLACEHOLDER
    assert "BMCSECRET" not in r.get_data(as_text=True)


def test_preview_masks_form_secrets_too(client):
    r = client.post("/api/baremetal/config/preview", json={
        "hostname": "n1", "device": "/dev/sda", "mgmt_interface": "eno1",
        "vip": "192.0.2.5", "token": TOKEN, "password": PASSWORD})
    _no_secret(r)
    assert r.status_code == 200
    assert "•••" in r.get_json()["yaml"]


def test_preview_refusal_body(client):
    r = client.post("/api/baremetal/config/preview", json={
        "hostname": "n1", "device": "/dev/sda", "mgmt_interface": "eno1",
        "vip": "192.0.2.5", "token": TOKEN,
        "advanced_yaml": f"os:\n  write_files:\n  - contnt: {PASSWORD}\n"})
    _no_secret(r)
    assert r.status_code == 400
    j = r.get_json()
    assert j["error"] == "invalid configuration"
    assert "os.write_files[0].contnt" in j["fields"]
    assert j["reasons"]["os.write_files[0].contnt"] == "unknown"


# --- installation --------------------------------------------------------------

def test_install_takes_secrets_from_the_import(client, runs):
    parsed = _parse(client).get_json()
    r = client.post("/api/baremetal/install", json=_install_body(parsed))
    _no_secret(r)
    assert r.status_code == 202, r.get_json()
    label, args = runs[0]
    opts = args[0]
    assert label == f"baremetal-install:{parsed['form']['hostname']}"
    assert TOKEN not in label and PASSWORD not in label
    assert opts["token"] == TOKEN and opts["password"] == PASSWORD
    assert "import_id" not in opts
    # le runner rendra la même configuration, avec la vraie adresse d'ISO
    cfg = wapp.yaml.safe_load(wapp._harvester_install_config(dict(opts, iso_url="http://x/i.iso")))
    assert cfg["token"] == TOKEN and cfg["os"]["password"] == PASSWORD


def test_form_values_win_over_the_import(client, runs):
    parsed = _parse(client).get_json()
    r = client.post("/api/baremetal/install",
                    json=_install_body(parsed, token="form-token", password="form-pass"))
    assert r.status_code == 202
    opts = runs[0][1][0]
    assert opts["token"] == "form-token" and opts["password"] == "form-pass"


def test_install_refuses_a_bad_config_before_any_action(client, runs):
    parsed = _parse(client).get_json()
    body = _install_body(parsed, advanced_yaml=parsed["advanced"] + "\nserver_url: https://x\n")
    r = client.post("/api/baremetal/install", json=body)
    _no_secret(r)
    assert r.status_code == 400
    j = r.get_json()
    assert j["error"] == "invalid configuration" and "server_url" in j["fields"]
    assert j["reasons"]["server_url"] == "reserved"
    assert runs == []


def test_install_refuses_a_bad_form_field_before_any_action(client, runs):
    r = client.post("/api/baremetal/install", json=dict(
        BMC, hostname="n1", device="/dev/sda", mgmt_interface="eno1",
        vip="192.0.2.5", token=TOKEN, vlan_id="5000"))
    _no_secret(r)
    assert r.status_code == 400
    assert "install.management_interface.vlan_id" in r.get_json()["fields"]
    assert runs == []


def test_install_accepts_several_interfaces_without_the_old_field(client, runs):
    r = client.post("/api/baremetal/install", json=dict(
        BMC, hostname="n1", device="/dev/sda", vip="192.0.2.5", token=TOKEN,
        mgmt_interfaces=["52:54:00:aa:bb:01", "52:54:00:aa:bb:02"],
        bond_mode="802.3ad", vlan_id="120"))
    assert r.status_code == 202, r.get_json()
    r = client.post("/api/baremetal/install", json=dict(
        BMC, hostname="n1", device="/dev/sda", vip="192.0.2.5", token=TOKEN))
    assert r.status_code == 400 and r.get_json()["fields"] == ["mgmt_interface"]


def test_install_ignores_a_client_iso_url(client, runs):
    r = client.post("/api/baremetal/install", json=dict(
        BMC, hostname="n1", device="/dev/sda", mgmt_interface="eno1",
        vip="192.0.2.5", token=TOKEN, iso_url="http://evil/x.iso"))
    assert r.status_code == 202
    assert "iso_url" not in runs[0][1][0]


def test_import_without_token_still_requires_one(client, runs):
    text = _operator_text().replace(f"token: {TOKEN}", "")
    parsed = _parse(client, text).get_json()
    assert parsed["has_token"] is False
    r = client.post("/api/baremetal/install", json=_install_body(parsed))
    assert r.status_code == 400 and "token" in r.get_json()["fields"]
    assert runs == []


# --- garde-fous ----------------------------------------------------------------

def test_new_routes_are_authenticated_and_rate_limited():
    from limits import parse_many
    src = (WEB / "app.py").read_text()
    for route in ("/api/baremetal/config/parse", "/api/baremetal/config/preview",
                  "/api/baremetal/install"):
        head = src.split(f'@app.route("{route}"', 1)[1].split("\ndef ", 1)[0]
        assert "@requires_auth" in head
        spec = re.search(r'@_rate_limit\("([^"]+)"\)', head).group(1)
        assert parse_many(spec)
