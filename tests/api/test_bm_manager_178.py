"""Redfish (1.78.0) : le média virtuel est celui du gestionnaire de la
machine visée (`Links.ManagedBy`), pas le premier de la collection Managers.
Vu en réel sur le banc : l'émulateur Redfish de node2 liste un gestionnaire
par VM de l'hôte ; prendre le premier aurait monté l'ISO, et lancé
l'installation, sur une autre machine."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp  # noqa: E402
import bm_discover  # noqa: E402

SYS = "/redfish/v1/Systems/target"
DOCS = {
    "/redfish/v1/Systems/": {"Members": [{"@odata.id": SYS}]},
    SYS: {"Id": "target", "Links": {"ManagedBy": [{"@odata.id": "/redfish/v1/Managers/mine"}]}},
    "/redfish/v1/Managers/": {"Members": [{"@odata.id": "/redfish/v1/Managers/other"},
                                          {"@odata.id": "/redfish/v1/Managers/mine"}]},
    "/redfish/v1/Managers/mine/VirtualMedia/": {"Members": [{"@odata.id": "/redfish/v1/Managers/mine/VirtualMedia/Cd"}]},
    "/redfish/v1/Managers/other/VirtualMedia/": {"Members": [{"@odata.id": "/redfish/v1/Managers/other/VirtualMedia/Cd"}]},
    "/redfish/v1/Managers/mine/VirtualMedia/Cd": {"MediaTypes": ["CD", "DVD"]},
    "/redfish/v1/Managers/other/VirtualMedia/Cd": {"MediaTypes": ["CD", "DVD"]},
}


def test_console_uses_the_manager_of_the_system(monkeypatch):
    monkeypatch.setattr(wapp, "_redfish_get", lambda host, path, u, p, timeout=8: DOCS.get(path))
    assert wapp._redfish_manager_path("bmc", "u", "p") == "/redfish/v1/Managers/mine"
    path, _ = wapp._redfish_virtualmedia_cd("bmc", "u", "p")
    assert path == "/redfish/v1/Managers/mine/VirtualMedia/Cd"


def test_console_falls_back_to_the_first_manager_without_links(monkeypatch):
    docs = dict(DOCS)
    docs[SYS] = {"Id": "target"}
    monkeypatch.setattr(wapp, "_redfish_get", lambda host, path, u, p, timeout=8: docs.get(path))
    assert wapp._redfish_manager_path("bmc", "u", "p") == "/redfish/v1/Managers/other"


def test_cli_uses_the_manager_of_the_system(monkeypatch):
    bmc = bm_discover.RedfishBmc("bmc", "u", "p")
    monkeypatch.setattr(bmc, "_get", lambda path: DOCS.get(path))
    bmc._system = SYS
    path, _ = bmc._virtual_cd()
    assert path == "/redfish/v1/Managers/mine/VirtualMedia/Cd"


def test_local_ip_ignores_the_bmc_port():
    """Un BMC écrit `hôte:port` publiait 127.0.0.1 dans l'adresse de l'ISO."""
    plain = wapp._bm_local_ip_for("192.0.2.10")
    assert wapp._bm_local_ip_for("192.0.2.10:8446") == plain
    assert plain != "127.0.0.1"


# Agencement Redfish 2020.4+ (vu sur l'émulateur Redfish du banc) : le lien
# `VirtualMedia` du gestionnaire mène sous le System, et
# `<gestionnaire>/VirtualMedia/` répond 404.
MODERN = {
    "/redfish/v1/Systems/": {"Members": [{"@odata.id": SYS}]},
    SYS: {"Id": "target", "Links": {"ManagedBy": [{"@odata.id": "/redfish/v1/Managers/mine"}]},
          "VirtualMedia": {"@odata.id": SYS + "/VirtualMedia"}},
    "/redfish/v1/Managers/": {"Members": [{"@odata.id": "/redfish/v1/Managers/mine"}]},
    "/redfish/v1/Managers/mine": {"VirtualMedia": {"@odata.id": SYS + "/VirtualMedia"}},
    SYS + "/VirtualMedia": {"Members": [{"@odata.id": SYS + "/VirtualMedia/Cd"}]},
    SYS + "/VirtualMedia/Cd": {"MediaTypes": ["CD", "DVD"]},
}


def test_console_follows_the_virtual_media_link_under_the_system(monkeypatch):
    monkeypatch.setattr(wapp, "_redfish_get", lambda host, path, u, p, timeout=8: MODERN.get(path))
    path, vm = wapp._redfish_virtualmedia_cd("bmc", "u", "p")
    assert path == SYS + "/VirtualMedia/Cd" and vm["MediaTypes"]


def test_cli_follows_the_virtual_media_link_under_the_system(monkeypatch):
    bmc = bm_discover.RedfishBmc("bmc", "u", "p")
    monkeypatch.setattr(bmc, "_get", lambda path: MODERN.get(path))
    bmc._system = SYS
    path, _ = bmc._virtual_cd()
    assert path == SYS + "/VirtualMedia/Cd"


def test_insert_waits_for_the_answer_not_the_inserted_flag(monkeypatch):
    """Vu en réel : l'émulateur Redfish ne répond à l'insertion qu'après
    avoir téléchargé l'ISO, et dit le lecteur monté AVANT : la console doit
    attendre la réponse (délai long), pas l'état du lecteur."""
    seen = {}
    monkeypatch.setattr(wapp, "_redfish_virtualmedia_cd", lambda *a, **k: ("/vm", {
        "MediaTypes": ["CD"], "Inserted": False,
        "Actions": {"#VirtualMedia.InsertMedia": {"target": "/vm/insert"}}}))
    def send(host, path, u, p, method, payload=None, timeout=20):
        seen["timeout"] = timeout
        return True, 204, ""
    monkeypatch.setattr(wapp, "_redfish_send", send)
    ok, _, _ = wapp._bm_media_insert("bmc", "u", "p", "http://x/i.iso")
    assert ok and seen["timeout"] >= 600


def test_cli_insert_waits_for_the_answer(monkeypatch):
    bmc = bm_discover.RedfishBmc("bmc", "u", "p")
    seen = {}
    monkeypatch.setattr(bmc, "_virtual_cd", lambda: ("/vm", {
        "MediaTypes": ["CD"], "Actions": {"#VirtualMedia.InsertMedia": {"target": "/vm/insert"}}}))
    def req(path, method="GET", payload=None, timeout=None):
        seen["timeout"] = timeout
        return True, None
    monkeypatch.setattr(bmc, "_req", req)
    ok, _ = bmc.insert("http://x/i.iso")
    assert ok and seen["timeout"] >= 600


def test_refused_insert_is_not_waited_for(monkeypatch):
    monkeypatch.setattr(wapp, "_redfish_virtualmedia_cd", lambda *a, **k: ("/vm", {
        "MediaTypes": ["CD"], "Actions": {"#VirtualMedia.InsertMedia": {"target": "/vm/insert"}}}))
    monkeypatch.setattr(wapp, "_redfish_send", lambda *a, **k: (False, 400, "bad image"))
    ok, detail, _ = wapp._bm_media_insert("bmc", "u", "p", "http://x/i.iso")
    assert not ok and detail == "bad image"
