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
