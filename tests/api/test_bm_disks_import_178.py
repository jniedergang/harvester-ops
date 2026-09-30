"""1.78.0 : la liste des disques à effacer d'un fichier importé rejoint les
cases du tableau des disques quand chacun de ses chemins est dans
l'inventaire de la machine ; sinon elle reste dans le YAML avancé, avec une
remarque."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp                         # noqa: E402
import bm_discover as bmd                  # noqa: E402
import harvester_install_schema as his     # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "bm_disks_178" / "discovery.txt"
HOST = "192.0.2.41"
SSD = "/dev/disk/by-path/pci-0000:00:17.0-ata-2.0"
NVME_ALIAS = "/dev/nvme0n1"


def _file(wipe):
    lines = "".join(f"    - {w}\n" for w in wipe)
    return ("token: t0k3n\nos:\n  hostname: hv-a\n  password: pw\n"
            "install:\n  mode: create\n  device: /dev/sda\n  vip: 192.0.2.100\n"
            "  vip_mode: static\n  management_interface:\n    method: dhcp\n"
            "    interfaces:\n    - name: eth0\n"
            f"  wipe_disks_list:\n{lines}")


@pytest.fixture()
def client(monkeypatch, tmp_path):
    wapp.app.config["TESTING"] = True
    monkeypatch.setattr(wapp, "_BM_IMPORT_CACHE", {})
    monkeypatch.setattr(wapp, "current_user", lambda: "alice")
    monkeypatch.setattr(wapp, "current_cluster_identity", lambda: None)
    monkeypatch.setattr(wapp, "INVENTORY_DIR", tmp_path)
    bmd.store_inventory(tmp_path, "SERIAL-0001", HOST, FIXTURE.read_text(), now=100)
    return wapp.app.test_client()


def test_split_moves_the_wipe_list_only_when_every_path_is_known():
    known = {SSD, NVME_ALIAS}
    out = his.split_imported_config(_file([SSD, NVME_ALIAS]), known)
    assert out["form"]["wipe_disks_list"] == [SSD, NVME_ALIAS]
    assert "wipe_disks_list" not in out["advanced"]
    assert "wipe-list-advanced" not in out["notes"]
    out = his.split_imported_config(_file([SSD, "/dev/disk/by-id/elsewhere"]), known)
    assert "wipe_disks_list" not in out["form"]
    assert "wipe_disks_list" in out["advanced"] and "elsewhere" in out["advanced"]
    assert "wipe-list-advanced" in out["notes"]
    # sans inventaire : comportement d'avant, dans le YAML avancé
    out = his.split_imported_config(_file([SSD]))
    assert "wipe_disks_list" not in out["form"] and "wipe_disks_list" in out["advanced"]


def test_parse_route_reads_the_inventory_of_the_machine(client):
    r = client.post("/api/baremetal/config/parse",
                    json={"text": _file([SSD, NVME_ALIAS]), "bmc_host": HOST})
    assert r.status_code == 200
    j = r.get_json()
    assert j["form"]["wipe_disks_list"] == [SSD, NVME_ALIAS]
    # autre machine, ou hôte absent : pas d'inventaire, liste gardée en YAML
    for body in ({"bmc_host": "192.0.2.99"}, {}, {"bmc_host": "bad host/.."}):
        j = client.post("/api/baremetal/config/parse",
                        json=dict(body, text=_file([SSD]))).get_json()
        assert "wipe_disks_list" not in j["form"]
        assert "wipe-list-advanced" in j["notes"]
