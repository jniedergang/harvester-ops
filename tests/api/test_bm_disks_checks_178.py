"""1.78.0 : contrôles des disques côté serveur quand un inventaire de
découverte existe pour le BMC (installation et aperçu), et lecture du
stockage publié par le BMC (`POST /api/bmc/storage`)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp                         # noqa: E402
import bm_discover as bmd                  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "bm_disks_178" / "discovery.txt"
HOST = "192.0.2.51"
BASE = {"bmc_host": HOST, "bmc_user": "u", "bmc_password": "BMCSECRET", "iso": "h.iso",
        "hostname": "n1", "mgmt_interface": "eno1", "vip": "192.0.2.50", "token": "t",
        "password": "pw", "method": "dhcp"}
NVME = "/dev/disk/by-id/nvme-eui.0025388a00000001"      # 960 Gio
SAS = "/dev/disk/by-id/scsi-SSEAGATE_ST480FM0003_TEST-SAS-0001"   # 480 Gio
SSD = "/dev/disk/by-path/pci-0000:00:17.0-ata-2.0"      # 1 Tio, partitionné
SATA = "/dev/disk/by-path/pci-0000:00:1f.2-ata-1.0"     # 30 Gio


@pytest.fixture()
def client(monkeypatch, tmp_path):
    wapp.app.config["TESTING"] = True
    monkeypatch.setattr(wapp, "_BM_IMPORT_CACHE", {})
    monkeypatch.setattr(wapp, "current_user", lambda: "alice")
    monkeypatch.setattr(wapp, "current_cluster_identity", lambda: None)
    monkeypatch.setattr(wapp, "load_config", lambda: {"clusters": []})
    monkeypatch.setattr(wapp, "INVENTORY_DIR", tmp_path / "inventory")
    bmd.store_inventory(tmp_path / "inventory", "SYS-0051", HOST, FIXTURE.read_text())
    return wapp.app.test_client()


@pytest.fixture()
def runs(monkeypatch):
    seen = []
    monkeypatch.setattr(wapp, "track_action", lambda label, cluster, worker, *a: seen.append(a) or "a1")
    return seen


def _install(client, **kw):
    return client.post("/api/baremetal/install", json=dict(BASE, **kw))


def test_the_install_refuses_what_check_disk_roles_refuses(client, runs):
    r = _install(client, device=SATA)
    assert r.status_code == 400
    j = r.get_json()
    assert j == {"error": "invalid disks", "fields": [SATA], "reasons": {SATA: "too-small:250"}}
    r = _install(client, device=SSD)
    assert r.get_json()["reasons"] == {SSD: "has-data"}
    r = _install(client, device=NVME, data_disk="/dev/nvme0n1")
    assert r.get_json()["reasons"] == {"/dev/nvme0n1": "role-twice"}
    r = _install(client, device="/dev/disk/by-id/wwn-0xnothere")
    assert r.get_json()["reasons"] == {"/dev/disk/by-id/wwn-0xnothere": "unknown-disk"}
    r = _install(client, device=NVME, wipe_disks_list=["/dev/sdz"])
    assert r.get_json()["reasons"] == {"/dev/sdz": "unknown-disk"}
    r = _install(client, device=NVME, pools=[{"tag": "a", "disks": [{"serial": "TEST-SATA-0001"}]}])
    assert r.get_json()["reasons"] == {SATA: "too-small:50"}
    r = _install(client, device=NVME, pools=[{"tag": "a", "disks": [{"serial": "TEST-SSD-0001"}]}])
    assert r.get_json()["reasons"] == {SSD: "has-data"}
    assert not runs
    # effacer le disque, ou tout effacer, lève `has-data`
    assert _install(client, device=NVME, wipe_disks_list=[SSD],
                    pools=[{"tag": "a", "disks": [{"serial": "TEST-SSD-0001"}]}]).status_code == 202
    assert _install(client, device=SSD, wipe_all_disks=True).status_code == 202
    # skipchecks lève les tailles, par les arguments noyau ou le champ
    assert _install(client, device=SATA, extra_args="harvester.install.skipchecks=true").status_code == 202
    assert _install(client, device=SATA, skipchecks=True).status_code == 202
    assert _install(client, device=NVME, data_disk=SAS).status_code == 202
    assert len(runs) == 5


def test_no_inventory_keeps_free_text(client, runs):
    r = client.post("/api/baremetal/install", json=dict(BASE, bmc_host="192.0.2.52", device="/dev/sda"))
    assert r.status_code == 202


def test_the_preview_runs_the_same_checks(client):
    r = client.post("/api/baremetal/config/preview", json=dict(BASE, device=SATA))
    assert r.status_code == 400
    assert r.get_json()["reasons"] == {SATA: "too-small:250"}
    assert "BMCSECRET" not in r.get_data(as_text=True)
    assert client.post("/api/baremetal/config/preview", json=dict(BASE, device=NVME)).status_code == 200
    # sans hôte : aperçu comme avant
    body = {k: v for k, v in BASE.items() if k != "bmc_host"}
    assert client.post("/api/baremetal/config/preview", json=dict(body, device=SATA)).status_code == 200


def test_bmc_storage_route(client, monkeypatch):
    seen = []
    ctrl = {"controllers": [{"id": "RAID.1", "model": "PERC H730", "raid_types": ["RAID1"],
                             "can_create_volume": True, "drives": [], "volumes": []}],
            "source": "redfish", "supported": True}
    monkeypatch.setattr(wapp, "_bmc_storage", lambda h, u, p, *a: seen.append((h, u, p)) or ctrl)
    r = client.post("/api/bmc/storage", json={"host": "192.0.2.9", "user": "root", "password": "BMCSECRET"})
    assert r.status_code == 200
    j = r.get_json()
    assert j["controllers"][0]["model"] == "PERC H730" and j["supported"] is True and j["host"] == "192.0.2.9"
    assert seen == [("192.0.2.9", "root", "BMCSECRET")]
    assert "BMCSECRET" not in r.get_data(as_text=True)
    for bad in ({}, {"host": "a/b"}, {"host": 3}):
        assert client.post("/api/bmc/storage", json=bad).status_code == 400

    def boom(*a):
        raise RuntimeError("BMCSECRET")
    monkeypatch.setattr(wapp, "_bmc_storage", boom)
    r = client.post("/api/bmc/storage", json={"host": "192.0.2.9", "password": "BMCSECRET"})
    assert r.status_code == 502 and "BMCSECRET" not in r.get_data(as_text=True)


def test_bmc_storage_route_is_protected():
    import limits
    src = (ROOT / "web" / "app.py").read_text()
    block = src.split('@app.route("/api/bmc/storage", methods=["POST"])', 1)[1].split("def ", 1)[0]
    assert "@requires_auth" in block
    spec = block.split('@_rate_limit("', 1)[1].split('"', 1)[0]
    assert limits.parse_many(spec)
    assert any(p for p in wapp.ADMIN_ONLY_PREFIXES if "/api/bmc/storage".startswith(p))


# --- round 3 : « effacer » coché sur le disque système ou de données --------

VDA = "/dev/disk/by-path/pci-0000:05:00.0"


def test_wiping_the_system_disk_passes_and_is_stripped_from_the_config(client, runs):
    # disque système partitionné, son « effacer » coché : accepté
    r = _install(client, device=SSD, wipe_disks_list=[SSD, VDA])
    assert r.status_code == 202
    opts = runs[-1][0]
    cfg = wapp._bm_render_checked(opts)
    assert cfg["install"]["device"] == SSD
    assert cfg["install"]["wipe_disks_list"] == [VDA]
    # désigné par un autre nom (/dev/sdd) : retiré aussi, grâce à l'inventaire
    r = _install(client, device=SSD, wipe_disks_list=["/dev/sdd"])
    assert r.status_code == 202
    cfg = wapp._bm_render_checked(runs[-1][0])
    assert "wipe_disks_list" not in cfg["install"]
    # le disque de données de même
    r = _install(client, device=NVME, data_disk=SSD, wipe_disks_list=[SSD])
    assert r.status_code == 202
    assert "wipe_disks_list" not in wapp._bm_render_checked(runs[-1][0])["install"]
    # une valeur du client n'est jamais reprise
    r = _install(client, device=NVME, wipe_disks_list=[VDA], own_disk_names=[VDA])
    assert wapp._bm_render_checked(runs[-1][0])["install"]["wipe_disks_list"] == [VDA]


def test_the_preview_shows_the_list_without_the_system_disk(client):
    import yaml
    r = client.post("/api/baremetal/config/preview",
                    json=dict(BASE, device=SSD, wipe_disks_list=[SSD, VDA]))
    assert r.status_code == 200
    cfg = yaml.safe_load(r.get_json()["yaml"])
    assert cfg["install"]["wipe_disks_list"] == [VDA]


def test_an_imported_list_naming_the_system_disk_is_stripped_too():
    import harvester_install_schema as his
    base = {"mode": "create", "hostname": "n1", "device": "/dev/sda", "mgmt_interface": "eno1",
            "vip": "192.0.2.50", "token": "t", "password": "pw", "method": "dhcp",
            "iso_url": "http://192.0.2.9/h.iso"}
    cfg = his.render_install_config(dict(
        base, advanced_yaml="install:\n  wipe_disks_list:\n  - /dev/sda\n  - /dev/sdb\n"))
    assert cfg["install"]["wipe_disks_list"] == ["/dev/sdb"]
    cfg = his.render_install_config(dict(base, advanced_yaml="install:\n  wipe_disks_list:\n  - /dev/sda\n"))
    assert "wipe_disks_list" not in cfg["install"]
    cfg = his.render_install_config(dict(base, data_disk="/dev/sdc", wipe_disks_list="/dev/sdc\n/dev/sdd"))
    assert cfg["install"]["wipe_disks_list"] == ["/dev/sdd"]
