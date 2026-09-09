"""v1.17.0 — socle du déploiement bare-metal : Redfish avancé + serveur
d'artefacts.

Tout ce qui est verrouillé ici a d'abord été constaté EN RÉEL sur node3
(iLO 4, licence Advanced) — conformément à la règle projet « tester en
réel ». Les surprises que ces tests figent :

* l'iLO 4 publie `BootSourceOverrideSupported` là où les Redfish récents
  publient `...@Redfish.AllowableValues` : lire un seul des deux donnait
  une liste de cibles vide ;
* l'action OEM `#HpiLOVirtualMedia.InsertVirtualMedia` **rejette**
  `Inserted` et `WriteProtected` (`ActionParameterUnknown`) alors que
  l'action standard `#VirtualMedia.InsertMedia` les attend ;
* le chemin du System était codé en dur (`/redfish/v1/Systems/1`), ce qui
  cassait les actions d'alimentation sur iDRAC.
"""

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
WEB = ROOT / "web"
sys.path.insert(0, str(WEB))

import app as wapp          # noqa: E402
import pxe_server as px     # noqa: E402


@pytest.fixture()
def client():
    wapp.app.config["TESTING"] = True
    return wapp.app.test_client()


# ---------------------------------------------------------------------------
# Redfish : résolution dynamique et dialectes
# ---------------------------------------------------------------------------

def test_action_target_prefers_standard_and_flags_oem():
    """Le dialecte doit être identifié : la charge utile en dépend."""
    standard = {"Actions": {"#VirtualMedia.InsertMedia": {"target": "/std"}}}
    assert wapp._redfish_action_target(standard, "InsertMedia") == ("/std", False)

    oem = {"Oem": {"Hp": {"Actions": {
        "#HpiLOVirtualMedia.InsertVirtualMedia": {"target": "/oem"}}}}}
    assert wapp._redfish_action_target(
        oem, "InsertVirtualMedia", "InsertMedia") == ("/oem", True)

    assert wapp._redfish_action_target({}, "InsertMedia") == (None, False)


def test_insert_payload_matches_the_dialect(client, monkeypatch):
    """Constaté sur node3 : envoyer Inserted/WriteProtected à l'action OEM
    fait échouer l'insertion avec ActionParameterUnknown."""
    sent = {}

    def fake_vm(host, user, pwd, manager_path=None):
        return "/redfish/v1/Managers/1/VirtualMedia/2/", {
            "MediaTypes": ["CD", "DVD"],
            "Oem": {"Hp": {"Actions": {
                "#HpiLOVirtualMedia.InsertVirtualMedia": {"target": "/oem/insert"}}}},
        }

    def fake_send(host, path, user, pwd, method, payload=None, timeout=20):
        sent["path"], sent["payload"] = path, payload
        return True, 200, ""

    monkeypatch.setattr(wapp, "_redfish_virtualmedia_cd", fake_vm)
    monkeypatch.setattr(wapp, "_redfish_send", fake_send)
    r = client.post("/api/bmc/1.2.3.4/virtualmedia",
                    json={"action": "insert", "image": "http://x/y.iso",
                          "user": "u", "password": "p"})
    assert r.status_code == 200
    assert sent["path"] == "/oem/insert"
    assert sent["payload"] == {"Image": "http://x/y.iso"}, (
        "l'action OEM n'accepte que Image")


def test_insert_refuses_when_no_cd_media(client, monkeypatch):
    """Sans licence iLO Advanced, aucun lecteur CD n'est exposé : le dire
    clairement plutôt que de laisser un 500 obscur."""
    monkeypatch.setattr(wapp, "_redfish_virtualmedia_cd",
                        lambda *a, **k: (None, None))
    r = client.post("/api/bmc/1.2.3.4/virtualmedia",
                    json={"action": "insert", "image": "http://x/y.iso"})
    assert r.status_code == 412
    assert "Advanced" in r.get_json()["detail"]


def test_boot_once_uses_the_resolved_system_path(client, monkeypatch):
    sent = {}

    def fake_send(host, path, user, pwd, method, payload=None, timeout=20):
        sent["path"], sent["payload"], sent["method"] = path, payload, method
        return True, 200, ""

    monkeypatch.setattr(wapp, "_redfish_system_path",
                        lambda *a: "/redfish/v1/Systems/System.Embedded.1")
    monkeypatch.setattr(wapp, "_redfish_send", fake_send)
    r = client.post("/api/bmc/1.2.3.4/boot-once", json={"target": "Cd"})
    assert r.status_code == 200
    assert sent["method"] == "PATCH"
    assert sent["path"] == "/redfish/v1/Systems/System.Embedded.1"
    assert sent["payload"]["Boot"] == {"BootSourceOverrideTarget": "Cd",
                                       "BootSourceOverrideEnabled": "Once"}


def test_power_action_no_longer_hardcodes_systems_1():
    """Le chemin codé en dur cassait l'alimentation sur iDRAC alors que la
    découverte, elle, résolvait déjà dynamiquement."""
    src = (WEB / "app.py").read_text()
    assert '"https://{host}/redfish/v1/Systems/1/Actions' not in src
    assert "_redfish_system_path(host, user, pwd)" in src


def test_discovery_reads_both_boot_target_schemas():
    src = (WEB / "app.py").read_text()
    assert "BootSourceOverrideTarget@Redfish.AllowableValues" in src
    assert "BootSourceOverrideSupported" in src, (
        "l'iLO 4 n'expose que cette clé — la manquer vide la liste")


# ---------------------------------------------------------------------------
# Serveur d'artefacts
# ---------------------------------------------------------------------------

@pytest.fixture()
def served():
    d = Path(tempfile.mkdtemp())
    iso = d / "x.iso"
    iso.write_bytes(bytes(range(256)) * 40)          # 10240 octets
    cfg = d / "c.yaml"
    cfg.write_text("scheme_version: 1\n")
    port = px.start(port=0, bind="127.0.0.1")
    yield port, iso, cfg
    px.stop()


def _get(port, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, b"", {}


def test_artifact_server_serves_only_issued_tokens(served):
    port, iso, cfg = served
    t_iso = px.issue(iso, "iso")
    t_cfg = px.issue(cfg, "config")

    assert _get(port, f"/pxe/iso/{t_iso}.iso")[0] == 200
    assert _get(port, f"/pxe/config/{t_cfg}.yaml")[0] == 200
    # un jeton ne vaut que pour son type
    assert _get(port, f"/pxe/config/{t_iso}.yaml")[0] == 404
    assert _get(port, "/pxe/iso/inconnu.iso")[0] == 404
    # ce n'est pas un serveur de fichiers
    assert _get(port, "/etc/passwd")[0] == 404
    assert _get(port, "/pxe/iso/..%2f..%2fetc%2fpasswd.iso")[0] == 404


def test_artifact_server_supports_ranges(served):
    """Un BMC télécharge un ISO par plages : sans 206 correct, l'insertion
    de média échoue ou repart sans fin."""
    port, iso, _ = served
    t = px.issue(iso, "iso")
    status, body, headers = _get(port, f"/pxe/iso/{t}.iso",
                                 {"Range": "bytes=100-199"})
    assert status == 206
    assert len(body) == 100
    assert headers["Content-Range"] == "bytes 100-199/10240"
    assert headers["Accept-Ranges"] == "bytes"
    # plage hors bornes
    assert _get(port, f"/pxe/iso/{t}.iso", {"Range": "bytes=99999-"})[0] == 416


def test_artifact_tokens_expire_and_can_be_revoked(served):
    port, iso, _ = served
    t = px.issue(iso, "iso")
    assert _get(port, f"/pxe/iso/{t}.iso")[0] == 200
    px.revoke(t)
    assert _get(port, f"/pxe/iso/{t}.iso")[0] == 404

    expired = px.issue(iso, "iso", ttl=-1)
    assert _get(port, f"/pxe/iso/{expired}.iso")[0] == 404


def test_artifact_server_is_not_the_authenticated_flask_app():
    """Le BMC ne sait ni s'authentifier ni faire confiance au certificat
    auto-signé : les artefacts doivent sortir par un listener séparé."""
    src = (WEB / "pxe_server.py").read_text()
    assert "ThreadingHTTPServer" in src
    assert "requires_auth" not in src
    # pas de dépendance nouvelle
    assert "import requests" not in src
