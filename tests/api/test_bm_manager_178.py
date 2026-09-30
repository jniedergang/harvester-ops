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


# --- contrôle préalable de l'installation (vu en réel, banc Redfish) --------

def _run():
    run = wapp.ActionRun("pf000000test", "baremetal-install:t", "(local)", [])
    run.close = lambda: None
    return run


def _profile(uefi):
    return {"ok": True, "power_state": "On", "post_state": None, "model": "M",
            "system_path": "/redfish/v1/Systems/1", "virtualmedia_path": "/vm",
            "boot_targets": ["Cd", "Hdd"], "uefi_targets": uefi}


def _preflight(monkeypatch, uefi, inventory=None, drives=0):
    monkeypatch.setattr(wapp, "_bmc_discover_one", lambda *a, **k: _profile(uefi))
    monkeypatch.setattr(wapp._bmd, "load_inventory", lambda *a, **k: inventory)
    monkeypatch.setattr(wapp, "_bmc_storage", lambda *a, **k: {
        "controllers": [{"drives": [{}] * drives}] if drives else []})
    monkeypatch.setattr(wapp.pxe_server, "start", lambda *a, **k: 18091)
    monkeypatch.setattr(wapp.pxe_server, "stop", lambda *a, **k: None)
    run = _run()
    # l'ISO n'existe pas : le déroulé s'arrête juste après le contrôle préalable
    wapp._baremetal_install_runner(run, {"bmc_user": "u", "bmc_password": "p",
                                         "bmc_host": "192.0.2.1", "iso": "absent.iso"})
    return [(e.get("step_id"), e.get("status"), e.get("message", "")) for e in run.events
            if e.get("type") == "step" and e.get("step_id") == "preflight"]


def test_a_bmc_without_hpe_targets_is_not_refused(monkeypatch):
    """Avant 1.78.0 : tout BMC hors iLO était refusé (« no disk visible »)."""
    ev = _preflight(monkeypatch, uefi=[])
    assert any(s == "warn" for _, s, _ in ev)
    assert ev[-1][1] == "done"


def test_the_discovery_inventory_proves_the_disks(monkeypatch):
    raw = (Path(__file__).parent / "fixtures" / "bm_disks_178" / "discovery.txt").read_text()
    ev = _preflight(monkeypatch, uefi=[], inventory={"raw": raw})
    assert ev[-1][1] == "done" and "inventaire" in ev[-1][2]
    assert not any(s == "warn" for _, s, _ in ev)


def test_redfish_drives_prove_the_disks(monkeypatch):
    ev = _preflight(monkeypatch, uefi=[], drives=2)
    assert ev[-1][1] == "done" and "Redfish" in ev[-1][2]


def test_an_ilo_listing_no_disk_is_still_refused(monkeypatch):
    ev = _preflight(monkeypatch, uefi=["Cd.Emb.1-1", "NIC.LOM.1-1"])
    assert ev[-1][1] == "error" and "no disk visible" in ev[-1][2]


# --- fin d'installation : l'installeur s'éteint, la console démarre sur le
# disque (vu en réel : un BMC qui n'applique pas l'amorce « Once » ramenait
# la machine sur le CD, l'installeur tournait en boucle) ---------------------

def test_the_installer_config_powers_off_at_the_end():
    import harvester_install_schema as his
    cfg = his.render_install_config({"token": "t", "hostname": "h", "device": "/dev/sda",
                                     "mgmt_interface": "eno1", "method": "dhcp",
                                     "vip": "192.0.2.100", "power_off": True})
    assert cfg["install"]["power_off"] is True


def test_power_off_is_reserved_to_the_console():
    import harvester_install_schema as his
    try:
        his.render_install_config({"token": "t", "hostname": "h", "device": "/dev/sda",
                                   "mgmt_interface": "eno1", "method": "dhcp", "vip": "192.0.2.100",
                                   "advanced_yaml": "install:\n  power_off: false\n"})
    except his.InstallConfigError as e:
        assert e.reasons.get("install.power_off") == "reserved"
    else:
        raise AssertionError("install.power_off accepted in the advanced YAML")
    split = his.split_imported_config("install:\n  power_off: true\n  device: /dev/sda\n")
    assert "power-off-ignored" in split["notes"]


def test_wait_power_off_sees_the_end_of_the_install(monkeypatch):
    states = iter(["On", "On", "Off"])
    monkeypatch.setattr(wapp, "_redfish_get", lambda *a, **k: {"PowerState": next(states)})
    run = _run()
    t = [0.0]
    assert wapp._bm_wait_power_off("bmc", "u", "p", "/s", 1e9, run, lambda *a: None,
                                   sleep=lambda s: t.__setitem__(0, t[0] + s), now=lambda: t[0])


def test_wait_power_off_stops_on_cancel(monkeypatch):
    monkeypatch.setattr(wapp, "_redfish_get", lambda *a, **k: {"PowerState": "On"})
    run = _run()
    run._cancel = True
    assert not wapp._bm_wait_power_off("bmc", "u", "p", "/s", 1e9, run, lambda *a: None,
                                       sleep=lambda s: None, now=lambda: 0.0)


def test_runner_boots_the_disk_after_the_installer_powers_off():
    src = (ROOT / "web" / "app.py").read_text()
    runner = src.split("def _baremetal_install_runner", 1)[1].split("\ndef ", 1)[0]
    i_wait = runner.index("_bm_wait_power_off(")
    i_eject = runner.index("_bm_media_eject(", i_wait)
    i_hdd = runner.index('_bm_boot_once_target(host, kc_user, kc_pwd, sys_path, "Hdd")')
    i_on = runner.index('_bm_reset(host, kc_user, kc_pwd, sys_path, "On")')
    i_api = runner.index("_bm_wait_api(")
    assert i_wait < i_eject < i_hdd < i_on < i_api


def test_a_machine_powered_on_by_the_preflight_is_powered_off_on_failure(monkeypatch):
    """Le préflight allume une machine éteinte pour lire son inventaire ; si
    le run échoue avant l'installation, elle est éteinte à nouveau."""
    calls = []
    profile = _profile([])
    off = dict(profile, power_state="Off")
    seq = iter([off, profile, profile, profile])
    monkeypatch.setattr(wapp, "_bmc_discover_one", lambda *a, **k: next(seq))
    monkeypatch.setattr(wapp, "_redfish_send", lambda *a, **k: (True, 204, ""))
    monkeypatch.setattr(wapp, "_bm_reset", lambda h, u, p, sp, t: calls.append(t) or (True, ""))
    monkeypatch.setattr(wapp._bmd, "load_inventory", lambda *a, **k: None)
    monkeypatch.setattr(wapp, "_bmc_storage", lambda *a, **k: {})
    monkeypatch.setattr(wapp.pxe_server, "start", lambda *a, **k: 18091)
    monkeypatch.setattr(wapp.pxe_server, "stop", lambda *a, **k: None)
    monkeypatch.setattr(wapp.time, "sleep", lambda s: None)
    run = _run()
    wapp._baremetal_install_runner(run, {"bmc_user": "u", "bmc_password": "p",
                                         "bmc_host": "192.0.2.1", "iso": "absent.iso"})
    assert run.status == "error" and calls == ["ForceOff"]


def test_a_machine_found_on_is_left_on_after_a_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(wapp, "_bmc_discover_one", lambda *a, **k: _profile([]))
    monkeypatch.setattr(wapp, "_bm_reset", lambda h, u, p, sp, t: calls.append(t) or (True, ""))
    monkeypatch.setattr(wapp._bmd, "load_inventory", lambda *a, **k: None)
    monkeypatch.setattr(wapp, "_bmc_storage", lambda *a, **k: {})
    monkeypatch.setattr(wapp.pxe_server, "start", lambda *a, **k: 18091)
    monkeypatch.setattr(wapp.pxe_server, "stop", lambda *a, **k: None)
    run = _run()
    wapp._baremetal_install_runner(run, {"bmc_user": "u", "bmc_password": "p",
                                         "bmc_host": "192.0.2.1", "iso": "absent.iso"})
    assert run.status == "error" and calls == []
