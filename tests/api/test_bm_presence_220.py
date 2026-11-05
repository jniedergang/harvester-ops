"""2.2.0 : une machine qui porte déjà un système est gardée (bare-metal).

* bin/lib/bm_presence.py : entrées UEFI d'un chargeur sur partition de
  disque (iLO 4 et Redfish standard, réponses relevées sur node3 et node4),
  supports amovibles écartés, tête de l'ordre de démarrage, volumes ;
  présent / absent / inconnu ; nom à taper ;
* la lecture Redfish (_bm_system_presence) sur des réponses simulées ;
* POST /api/bmc/system-check : adresse refusée, administrateur seul,
  authentifiée et limitée ;
* l'installation refuse sans le nom tapé quand un système semble présent,
  accepte le bon nom, et laisse passer « absent » et « inconnu » ;
* mutations volontaires.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp  # noqa: E402
import bm_presence as bp  # noqa: E402

# Relevés réels (GET Redfish en lecture seule, 02/11/2026) réduits aux champs utiles
NODE4_BOOT = {"BootSources": [
    {"BootString": "elemental-shim", "StructuredBootString": "HD.Emb.1.3",
     "UEFIDevicePath": "PciRoot(0x0)/Pci(0x1F,0x2)/Sata(0x0,0x0,0x0)/HD(1,GPT,828E2799-02E2-4291-8C8C-7A10C2661957,"
                       "0x800,0x20000)/\\EFI\\elemental\\shim.efi"},
    {"BootString": "sles-secureboot", "StructuredBootString": "Unknown.Unknown.1",
     "UEFIDevicePath": "HD(1,GPT,426A4334-7804-43A4-8FA5-1CA8C3CD856F,0x800,0x100000)/\\EFI\\sles\\shim.efi"},
    {"BootString": "Generic USB Boot", "StructuredBootString": "Generic.USB.1.1",
     "UEFIDevicePath": "UsbClass(0xFFFF,0xFFFF,0xFF,0xFF,0xFF)"},
    {"BootString": "Embedded LOM 1 Port 1 (PXE IPv4)", "StructuredBootString": "NIC.LOM.1.1.IPv4",
     "UEFIDevicePath": "PciRoot(0x0)/Pci(0x1C,0x4)/Pci(0x0,0x0)/MAC(70106FB38DBA,0x1)/IPv4(0.0.0.0)"},
    {"BootString": "Embedded SATA Port 1 HDD : Samsung SSD 860 EVO 500GB", "StructuredBootString": "HD.Emb.1.2",
     "UEFIDevicePath": "PciRoot(0x0)/Pci(0x1F,0x2)/Sata(0x0,0x0,0x0)"}],
    "PersistentBootConfigOrder": ["HD.Emb.1.3", "Unknown.Unknown.1", "Generic.USB.1.1", "NIC.LOM.1.1.IPv4",
                                  "HD.Emb.1.2"]}
BLANK_BOOT = {"BootSources": [s for s in NODE4_BOOT["BootSources"] if "EFI" not in s["UEFIDevicePath"]],
              "PersistentBootConfigOrder": ["Generic.USB.1.1", "NIC.LOM.1.1.IPv4", "HD.Emb.1.2"]}


@pytest.mark.parametrize("path,loader", [
    (NODE4_BOOT["BootSources"][0]["UEFIDevicePath"], "\\EFI\\elemental\\shim.efi"),
    (NODE4_BOOT["BootSources"][1]["UEFIDevicePath"], "\\EFI\\sles\\shim.efi"),
    ("PciRoot(0x0)/Pci(0x17,0x0)/Sata(0x0,0xFFFF,0x0)/HD(2,MBR,0x1234,0x800,0x1000)/\\EFI\\BOOT\\BOOTX64.EFI",
     "\\EFI\\BOOT\\BOOTX64.EFI"),
    ("PciRoot(0x0)/Pci(0x14,0x0)/USB(0x3,0x0)/HD(1,GPT,AA,0x800,0x1000)/\\EFI\\BOOT\\BOOTX64.EFI", None),  # clé USB
    ("PciRoot(0x0)/Pci(0x1F,0x2)/Sata(0x1,0x0,0x0)/CDROM(0x1,0x0,0x0)/\\EFI\\BOOT\\BOOTX64.EFI", None),
    ("PciRoot(0x0)/Pci(0x1C,0x4)/MAC(70106FB38DBA,0x1)/IPv4(0.0.0.0)/Uri(http://x/boot.efi)", None),
    ("PciRoot(0x0)/Pci(0x1F,0x2)/Sata(0x0,0x0,0x0)", None),                      # disque brut
    ("", None),
    (None, None),
])
def test_loader_of(path, loader):
    assert bp.loader_of(path) == loader


@pytest.mark.parametrize("boot,volumes,state,codes", [
    (NODE4_BOOT, [], "present", ["boots-from-disk", "boot-entry", "boot-entry"]),
    (BLANK_BOOT, [], "absent", []),
    (BLANK_BOOT, [{"name": "Logical Drive 1", "controller": "Smart Array P440"}], "present", ["volume"]),
    (None, None, "unknown", []),                                 # rien de lisible : jamais « vide »
    ({"Name": "no list"}, [], "unknown", []),
    (dict(NODE4_BOOT, PersistentBootConfigOrder=["NIC.LOM.1.1.IPv4"]), [], "present", ["boot-entry", "boot-entry"]),
])
def test_evaluate_hpe(boot, volumes, state, codes):
    entries, order = bp.entries_from_hpe(boot)
    got = bp.evaluate(entries, order, volumes)
    assert got["state"] == state
    assert [e["code"] for e in got["evidence"]] == codes


def test_evidence_names_what_was_seen():
    got = bp.evaluate(*bp.entries_from_hpe(NODE4_BOOT), [])
    assert got["evidence"][0] == {"code": "boots-from-disk", "name": "elemental-shim"}
    assert got["evidence"][1] == {"code": "boot-entry", "name": "elemental-shim",
                                  "loader": "\\EFI\\elemental\\shim.efi", "disk": "HD.Emb.1.3"}


def test_standard_redfish_boot_options():
    opts = [{"BootOptionReference": "Boot0003", "DisplayName": "SUSE Linux Enterprise",
             "UefiDevicePath": "PciRoot(0x0)/Pci(0x17,0x0)/Sata(0x0,0xFFFF,0x0)/HD(1,GPT,AB,0x800,0x100000)"
                               "/\\EFI\\sles\\shim.efi"},
            {"BootOptionReference": "Boot0001", "DisplayName": "PXE",
             "UefiDevicePath": "PciRoot(0x0)/Pci(0x1C,0x0)/MAC(001122334455,0x1)/IPv4(0.0.0.0)"}]
    got = bp.evaluate(*bp.entries_from_standard(opts, ["Boot0003", "Boot0001"]), None)
    assert got["state"] == "present"
    assert got["evidence"][0] == {"code": "boots-from-disk", "name": "SUSE Linux Enterprise"}
    assert bp.evaluate(*bp.entries_from_standard(opts[1:], ["Boot0001"]), None)["state"] == "absent"
    assert bp.evaluate(*bp.entries_from_standard(None, None), None)["state"] == "unknown"


@pytest.mark.parametrize("serial,host,name", [
    ("USE6236RY3", "192.0.2.34", "USE6236RY3"),
    ("  ", "192.0.2.34", "192.0.2.34"),
    (None, "192.0.2.34", "192.0.2.34"),
])
def test_confirm_name(serial, host, name):
    assert bp.confirm_name(serial, host) == name


# --- lecture Redfish simulée ------------------------------------------------------------

def fake_redfish(pages):
    def get(host, path, user, pwd, timeout=10):
        return pages.get(path)
    return get


ILO4 = {"/redfish/v1/Systems/": {"Members": [{"@odata.id": "/redfish/v1/Systems/1/"}]},
        "/redfish/v1/Systems/1/": {"SerialNumber": "USE6236RY3"},
        "/redfish/v1/Systems/1/BIOS/Boot/": NODE4_BOOT}


@pytest.mark.real_presence
@pytest.mark.parametrize("pages,state,name,source", [
    (ILO4, "present", "USE6236RY3", "hpe-oem"),
    (dict(ILO4, **{"/redfish/v1/Systems/1/BIOS/Boot/": BLANK_BOOT}), "absent", "USE6236RY3", "hpe-oem"),
    ({k: v for k, v in ILO4.items() if "BIOS" not in k}, "unknown", "USE6236RY3", None),
    ({}, "unknown", "192.0.2.34", None),                         # BMC injoignable
])
def test_system_presence_reads_the_bmc(monkeypatch, pages, state, name, source):
    monkeypatch.setattr(wapp, "_redfish_get", fake_redfish(pages))
    got = wapp._bm_system_presence("192.0.2.34", "u", "p")
    assert (got["state"], got["confirm_name"], got["source"]) == (state, name, source)


@pytest.mark.real_presence
def test_standard_dialect_and_volumes(monkeypatch):
    pages = {"/redfish/v1/Systems/": {"Members": [{"@odata.id": "/redfish/v1/Systems/System.Embedded.1"}]},
             "/redfish/v1/Systems/System.Embedded.1/": {"SerialNumber": "", "Boot": {"BootOrder": ["Boot0001"]}},
             "/redfish/v1/Systems/System.Embedded.1/BootOptions": {"Members": [
                 {"@odata.id": "/redfish/v1/Systems/System.Embedded.1/BootOptions/Boot0001"}]},
             "/redfish/v1/Systems/System.Embedded.1/BootOptions/Boot0001": {
                 "BootOptionReference": "Boot0001", "DisplayName": "PXE",
                 "UefiDevicePath": "PciRoot(0x0)/MAC(001122334455,0x1)/IPv4(0.0.0.0)"}}
    monkeypatch.setattr(wapp, "_redfish_get", fake_redfish(pages))
    monkeypatch.setattr(wapp, "_bmc_storage", lambda h, u, p, sp=None: {"controllers": [
        {"name": "PERC H740P", "volumes": [{"name": "Virtual Disk 0", "volume_type": "Mirrored"},
                                           {"name": "Disk 1", "volume_type": "RawDevice"}]}]})
    got = wapp._bm_system_presence("192.0.2.9", "u", "p")
    assert got["state"] == "present" and got["source"] == "redfish" and got["confirm_name"] == "192.0.2.9"
    assert got["evidence"] == [{"code": "volume", "name": "Virtual Disk 0", "controller": "PERC H740P"}]


# --- route de lecture --------------------------------------------------------------------

@pytest.fixture()
def client(monkeypatch):
    wapp.app.config["TESTING"] = True
    monkeypatch.setattr(wapp, "current_role", lambda: "admin")
    return wapp.app.test_client()


@pytest.mark.real_presence
def test_system_check_route(client, monkeypatch):
    monkeypatch.setattr(wapp, "_redfish_get", fake_redfish(ILO4))
    d = client.post("/api/bmc/system-check", json={"host": "192.0.2.34", "user": "u", "password": "SECRET-220"})
    assert d.status_code == 200
    body = d.get_json()
    assert body["state"] == "present" and body["host"] == "192.0.2.34"
    assert "SECRET-220" not in d.get_data(as_text=True)


@pytest.mark.parametrize("host", ["", "192.0.2.1/redfish", "u@192.0.2.1", 12, None])
def test_system_check_refuses_a_bad_address(client, host):
    r = client.post("/api/bmc/system-check", json={"host": host, "user": "u", "password": "p"})
    assert r.status_code == 400 and r.get_json()["fields"] == ["host"]


@pytest.mark.parametrize("role,status", [("viewer", 403), ("operator", 403)])
def test_system_check_is_for_administrators(client, monkeypatch, role, status):
    monkeypatch.setattr(wapp, "current_role", lambda: role)
    assert client.post("/api/bmc/system-check", json={"host": "192.0.2.1"}).status_code == status


def test_system_check_route_is_authenticated_and_rate_limited():
    from limits import parse_many
    src = (ROOT / "web" / "app.py").read_text()
    head = src.split('@app.route("/api/bmc/system-check"', 1)[1].split("\ndef ", 1)[0]
    assert "@requires_auth" in head
    assert parse_many(re.search(r'@_rate_limit\("([^"]+)"\)', head).group(1))


# --- l'installation garde la machine -------------------------------------------------------

def install_env(monkeypatch, state, name="USE6236RY3"):
    started = []
    monkeypatch.setattr(wapp, "_bm_prepare_install", lambda data, defer_join=False: (data, None))
    monkeypatch.setattr(wapp, "_bm_apply_import", lambda data: None)
    monkeypatch.setattr(wapp, "_bm_system_presence", lambda host, user, pwd, system_path=None: {
        "state": state, "evidence": [{"code": "boots-from-disk", "name": "sles"}] if state == "present" else [],
        "confirm_name": name, "source": "hpe-oem"})
    monkeypatch.setattr(wapp, "_bm_track", lambda label, host, runner, data, parent=None: (started.append(data) or "a00000000220", None))
    return started


BODY = {"mode": "create", "bmc_host": "192.0.2.34", "bmc_user": "u", "bmc_password": "p", "hostname": "n1"}


@pytest.mark.real_presence
@pytest.mark.parametrize("state,typed,status", [
    ("present", None, 409),                 # rien de tapé
    ("present", "USE6236RY", 409),          # nom faux
    ("present", "use6236ry3", 409),         # casse différente
    ("present", "USE6236RY3", 202),
    ("present", "  USE6236RY3  ", 202),     # espaces autour tolérés
    ("absent", None, 202),
    ("unknown", None, 202),                 # le BMC ne dit rien : pas de refus
])
def test_install_requires_the_typed_name_when_a_system_seems_present(client, monkeypatch, state, typed, status):
    started = install_env(monkeypatch, state)
    body = dict(BODY, **({"system_confirm": typed} if typed is not None else {}))
    r = client.post("/api/baremetal/install", json=body)
    assert r.status_code == status, r.get_json()
    if status == 409:
        d = r.get_json()
        assert d["fields"] == ["system_confirm"] and d["system"]["state"] == "present"
        assert started == []                                     # rien n'est parti
    else:
        assert len(started) == 1 and "system_confirm" not in started[0]


@pytest.mark.real_presence
def test_mutation_without_the_server_check_the_install_goes(client, monkeypatch):
    """Mutation volontaire : sans le contrôle côté serveur, une machine qui
    porte un système part à l'installation sans nom tapé."""
    started = install_env(monkeypatch, "present")
    monkeypatch.setattr(wapp, "_bm_presence_refusal", lambda data, typed: None)
    r = client.post("/api/baremetal/install", json=BODY)
    with pytest.raises(AssertionError):
        assert r.status_code == 409 and started == []


def test_mutation_a_loader_rule_that_accepts_usb_is_seen(monkeypatch):
    monkeypatch.setattr(bp, "_REMOVABLE_RE", re.compile(r"(?!x)x"))
    with pytest.raises(AssertionError):
        assert bp.loader_of("PciRoot(0x0)/USB(0x3,0x0)/HD(1,GPT,AA,0x800,0x1000)/\\EFI\\BOOT\\BOOTX64.EFI") is None


def test_mutation_unknown_reported_as_absent_is_seen(monkeypatch):
    """Mutation volontaire : une liste illisible lue comme vide affirmerait
    « absent » ; le test de l'état inconnu le voit."""
    real = bp.evaluate
    monkeypatch.setattr(bp, "evaluate", lambda entries, order=None, volumes=None: real(entries or [], order, volumes))
    with pytest.raises(AssertionError):
        assert bp.evaluate(None, None, None)["state"] == "unknown"
