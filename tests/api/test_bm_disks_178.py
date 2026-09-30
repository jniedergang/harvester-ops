"""Disques d'une installation bare-metal (1.78.0) : lecture de l'inventaire
de découverte, chemin stable proposé, contrôles des rôles et champs de
configuration. La fixture part d'un inventaire réel du banc (VM imbriquée,
ISO live v1.9.0, multipathd actif), anonymisé, complété de disques
synthétiques (SAS derrière multipath sans by-path, NVMe, disque partitionné,
disque sans lien)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "web"))

import baremetal_disks as bd  # noqa: E402
import harvester_install_schema as his  # noqa: E402

FIXTURE = ROOT / "tests/api/fixtures/bm_disks_178/discovery.txt"
GIB = 1 << 30


@pytest.fixture(scope="module")
def inv():
    return bd.parse_discovery(FIXTURE.read_text())


@pytest.fixture(scope="module")
def by_name(inv):
    return {d["name"]: d for d in inv["disks"]}


def _disk(name, size_gib, by_id=(), by_path=(), has_data=False):
    d = {"name": name, "size_bytes": size_gib * GIB, "has_data": has_data,
         "links": {"by_id": list(by_id), "by_path": list(by_path)}}
    d["stable_path"], d["stable_kind"] = bd.stable_path(d)
    return d


# ---------------------------------------------------------------- inventaire

def test_fixture_is_anonymized():
    text = FIXTURE.read_text()
    assert "SPIKE" not in text and "4c:af" not in text.lower()
    assert chr(0x2014) not in text and chr(0x2192) not in text


def test_parse_keeps_only_target_disks(by_name):
    # loop0 (squashfs), sr0 (média live) et les dm-* ne sont pas des disques
    assert sorted(by_name) == ["nvme0n1", "sda", "sdb", "sdc", "sdd", "sde", "vda"]


def test_parse_disk_fields(by_name):
    sdb = by_name["sdb"]
    assert sdb["size_bytes"] == 32212254720
    assert sdb["model"] == "QEMU HARDDISK"
    assert sdb["serial"] == "TEST-SATA-0001"
    assert sdb["transport"] == "sata"
    assert sdb["rotational"] is True
    assert sdb["type"] == "disk"
    assert sdb["multipath"] is True
    # `mpath_member` est posé par multipathd du système live, pas une donnée
    assert sdb["fstype"] is None and sdb["has_data"] is False
    nvme = by_name["nvme0n1"]
    assert nvme["rotational"] is False and nvme["transport"] == "nvme"
    assert nvme["wwn"] == "eui.0025388a00000001"


def test_virtio_disk_by_path(by_name):
    vda = by_name["vda"]
    assert vda["links"]["by_id"] == []
    # `pci-...` préféré à l'ancienne forme `virtio-pci-...`
    assert (vda["stable_path"], vda["stable_kind"]) == (
        "/dev/disk/by-path/pci-0000:05:00.0", "by-path")


def test_sata_by_path_prefers_current_ata_form(by_name):
    assert by_name["sdb"]["stable_path"] == "/dev/disk/by-path/pci-0000:00:1f.2-ata-1.0"


def test_multipath_links_never_attached_to_disk(inv, by_name):
    for d in inv["disks"]:
        for link in d["links"]["by_id"] + d["links"]["by_path"]:
            assert "dm-" not in link.rsplit("/", 1)[-1]
    # wwn- de sdb mène à dm-0 dans l'inventaire réel : pas à sdb
    assert not any(x.rsplit("/", 1)[-1].startswith("wwn-")
                   for x in by_name["sdb"]["links"]["by_id"])


def test_multipath_disk_without_by_path_uses_own_by_id(by_name):
    # sdc : wwn- et scsi-3 vont au dm-2, seul scsi-S mène au disque
    sdc = by_name["sdc"]
    assert (sdc["stable_path"], sdc["stable_kind"]) == (
        "/dev/disk/by-id/scsi-SSEAGATE_ST480FM0003_TEST-SAS-0001", "by-id")


def test_nvme_prefers_eui(by_name):
    nvme = by_name["nvme0n1"]
    assert (nvme["stable_path"], nvme["stable_kind"]) == (
        "/dev/disk/by-id/nvme-eui.0025388a00000001", "by-id")


def test_partitioned_disk(by_name):
    sdd = by_name["sdd"]
    assert sdd["has_data"] is True
    assert [(p["name"], p["fstype"]) for p in sdd["partitions"]] == [
        ("sdd1", "vfat"), ("sdd2", "ext4"), ("sdd3", "LVM2_member")]
    assert sdd["partitions"][1]["size_bytes"] == 100 * GIB
    # les liens -partN visent les partitions, pas le disque
    assert not any("-part" in x for x in sdd["links"]["by_id"] + sdd["links"]["by_path"])
    assert sdd["stable_path"] == "/dev/disk/by-path/pci-0000:00:17.0-ata-2.0"


def test_disk_without_links_falls_back_to_kernel(by_name):
    assert (by_name["sde"]["stable_path"], by_name["sde"]["stable_kind"]) == (
        "/dev/sde", "kernel")


def test_nics(inv):
    assert inv["nics"] == [
        {"name": "enp3s0", "mac": "52:54:00:00:00:01", "state": "UP", "speed": 1000},
        {"name": "enp4s0", "mac": "52:54:00:00:00:02", "state": "UP", "speed": None},
    ]


def test_parse_tolerates_empty_and_broken_sections():
    assert bd.parse_discovery("") == {"disks": [], "nics": []}
    assert bd.parse_discovery("== lsblk\n{oops\n== nics\n[\n") == {"disks": [], "nics": []}


def test_parse_size_as_string_and_unknown_section():
    text = ('== junk\nabc\n== lsblk\n{"blockdevices": [{"name": "sdz", "size": "'
            + str(300 * GIB) + '", "type": "disk", "rota": "0"}]}\n'
            '== links\n/dev/disk/by-id/ata-X_1 /dev/sdz\nbroken line here\n')
    d = bd.parse_discovery(text)["disks"][0]
    assert d["size_bytes"] == 300 * GIB and d["rotational"] is False
    assert d["stable_path"] == "/dev/disk/by-id/ata-X_1"


# ------------------------------------------------------------- chemin stable

@pytest.mark.parametrize("by_id,expected", [
    (["/dev/disk/by-id/scsi-SX", "/dev/disk/by-id/ata-X", "/dev/disk/by-id/wwn-0x1",
      "/dev/disk/by-id/nvme-M_S", "/dev/disk/by-id/nvme-eui.01"], "/dev/disk/by-id/nvme-eui.01"),
    (["/dev/disk/by-id/scsi-SX", "/dev/disk/by-id/ata-X", "/dev/disk/by-id/wwn-0x1",
      "/dev/disk/by-id/nvme-M_S_1", "/dev/disk/by-id/nvme-M_S"], "/dev/disk/by-id/nvme-M_S"),
    (["/dev/disk/by-id/scsi-SX", "/dev/disk/by-id/ata-X", "/dev/disk/by-id/wwn-0x1"],
     "/dev/disk/by-id/wwn-0x1"),
    (["/dev/disk/by-id/scsi-SX", "/dev/disk/by-id/ata-X"], "/dev/disk/by-id/ata-X"),
    (["/dev/disk/by-id/scsi-SX", "/dev/disk/by-id/dm-name-X",
      "/dev/disk/by-id/usb-X"], "/dev/disk/by-id/scsi-SX"),
])
def test_by_id_order(by_id, expected):
    assert bd.stable_path({"name": "sdx", "links": {"by_id": by_id, "by_path": []}}) == (
        expected, "by-id")


def test_by_path_wins_over_by_id():
    d = {"name": "sdx", "links": {"by_id": ["/dev/disk/by-id/nvme-eui.01"],
                                  "by_path": ["/dev/disk/by-path/pci-0000:01:00.0-nvme-1"]}}
    assert bd.stable_path(d) == ("/dev/disk/by-path/pci-0000:01:00.0-nvme-1", "by-path")


def test_nvme_nguid_form_after_model_serial():
    d = {"name": "nvme0n1", "links": {"by_id": [
        "/dev/disk/by-id/nvme-nvme.1af4-31-51-1", "/dev/disk/by-id/nvme-LONGER_MODEL_SERIAL"],
        "by_path": []}}
    assert bd.stable_path(d)[0] == "/dev/disk/by-id/nvme-LONGER_MODEL_SERIAL"


def test_only_unknown_links_is_kernel():
    d = {"name": "sdq", "links": {"by_id": ["/dev/disk/by-id/dm-name-foo",
                                            "/dev/disk/by-id/lvm-pv-uuid-x"], "by_path": []}}
    assert bd.stable_path(d) == ("/dev/sdq", "kernel")


# ----------------------------------------------------------- contrôle des rôles

BIG = _disk("sda", 500, by_path=["/dev/disk/by-path/pci-a"])
MID = _disk("sdb", 200, by_id=["/dev/disk/by-id/wwn-0xb"])
SMALL = _disk("sdc", 40, by_id=["/dev/disk/by-id/ata-c"])
USED = _disk("sdd", 100, by_id=["/dev/disk/by-id/ata-d"], has_data=True)
DISKS = [BIG, MID, SMALL, USED]


def _roles(os=None, data=None, pools=None, wipe=None):
    return {"os": os, "data": data, "pools": pools or {}, "wipe": wipe or []}


def test_valid_roles():
    roles = _roles(os=BIG["stable_path"], data=MID["stable_path"],
                   pools={"fast": [USED["stable_path"]]}, wipe=[USED["stable_path"]])
    assert bd.check_disk_roles(DISKS, roles) == []


def test_no_os():
    assert bd.check_disk_roles(DISKS, _roles()) == [("", "no-os")]
    assert ("", "no-os") in bd.check_disk_roles(DISKS, _roles(os="  "))


def test_unknown_disk():
    errs = bd.check_disk_roles(DISKS, _roles(os="/dev/disk/by-id/nope",
                                             wipe=["/dev/disk/by-id/gone"]))
    assert ("/dev/disk/by-id/nope", "unknown-disk") in errs
    assert ("/dev/disk/by-id/gone", "unknown-disk") in errs


def test_role_twice_detected_through_aliases():
    # même disque désigné par son chemin stable puis par /dev/<nom>
    errs = bd.check_disk_roles(DISKS, _roles(os=BIG["stable_path"], data="/dev/sda"))
    assert ("/dev/sda", "role-twice") in errs
    errs = bd.check_disk_roles(DISKS, _roles(
        os=BIG["stable_path"], pools={"a": [MID["stable_path"]], "b": [MID["stable_path"]]}))
    assert (MID["stable_path"], "role-twice") in errs


def test_os_size_single_and_with_data():
    # 200 Gio : trop petit seul (250), suffisant avec un disque de données (180)
    assert bd.check_disk_roles(DISKS, _roles(os=MID["stable_path"])) == [
        (MID["stable_path"], "too-small:250")]
    assert bd.check_disk_roles(DISKS, _roles(os=MID["stable_path"],
                                             data=BIG["stable_path"])) == []


def test_data_and_pool_minimum():
    errs = bd.check_disk_roles(DISKS, _roles(os=BIG["stable_path"], data=SMALL["stable_path"]))
    assert (SMALL["stable_path"], "too-small:50") in errs
    errs = bd.check_disk_roles(DISKS, _roles(os=BIG["stable_path"],
                                             pools={"p1": [SMALL["stable_path"]]}))
    assert errs == [(SMALL["stable_path"], "too-small:50")]


def test_size_boundary_is_floor_gib():
    # l'installeur compare octets >> 30 : 250 Gio moins un octet est refusé
    d = {"name": "sdx", "size_bytes": 250 * GIB - 1, "has_data": False,
         "stable_path": "/dev/sdx", "links": {}}
    assert bd.check_disk_roles([d], _roles(os="/dev/sdx")) == [("/dev/sdx", "too-small:250")]
    d["size_bytes"] = 250 * GIB
    assert bd.check_disk_roles([d], _roles(os="/dev/sdx")) == []


def test_skipchecks_lifts_sizes_only():
    roles = _roles(os=SMALL["stable_path"], data=USED["stable_path"])
    errs = bd.check_disk_roles(DISKS, roles, skipchecks=True)
    assert errs == [(USED["stable_path"], "has-data")]


def test_has_data_needs_wipe():
    roles = _roles(os=BIG["stable_path"], data=USED["stable_path"])
    assert bd.check_disk_roles(DISKS, roles) == [(USED["stable_path"], "has-data")]
    # effacement demandé pour ce disque (par un autre de ses noms), ou tout effacer
    assert bd.check_disk_roles(DISKS, dict(roles, wipe=["/dev/sdd"])) == []
    assert bd.check_disk_roles(DISKS, roles, wipe_all=True) == []


@pytest.mark.parametrize("tag,ok", [
    ("fast", True), ("a", True), ("ssd-01", True), ("a" * 32, True),
    ("a" * 33, False), ("Fast", False), ("-a", False), ("a-", False),
    ("a_b", False), ("", False), ("a.b", False),
])
def test_pool_tag(tag, ok):
    errs = bd.check_disk_roles(DISKS, _roles(os=BIG["stable_path"],
                                             pools={tag: [MID["stable_path"]]}))
    assert ((tag, "pool-tag") not in errs) is ok


def test_roles_on_fixture(by_name):
    disks = list(by_name.values())
    roles = _roles(os=by_name["sdd"]["stable_path"], data=by_name["nvme0n1"]["stable_path"],
                   pools={"hdd": [by_name["sdc"]["stable_path"], by_name["sde"]["stable_path"]]})
    assert bd.check_disk_roles(disks, roles) == [(by_name["sdd"]["stable_path"], "has-data")]
    roles["pools"]["hdd"].append(by_name["vda"]["stable_path"])  # 20 Gio
    errs = bd.check_disk_roles(disks, dict(roles, wipe=[by_name["sdd"]["stable_path"]]))
    assert errs == [(by_name["vda"]["stable_path"], "too-small:50")]


# ------------------------------------------------- champs de la configuration

def test_install_fields_use_stable_paths():
    roles = _roles(os="/dev/sda", data="/dev/sdb",
                   wipe=["/dev/sdd", USED["stable_path"], "/dev/sda", MID["stable_path"],
                         "/dev/disk/by-id/free-text"])
    assert bd.install_fields(DISKS, roles) == {
        "device": BIG["stable_path"],
        "data_disk": MID["stable_path"],
        # système et données retirés (l'installeur les formate), doublons fusionnés
        "wipe_disks_list": [USED["stable_path"], "/dev/disk/by-id/free-text"],
    }


def test_install_fields_without_data():
    assert bd.install_fields(DISKS, _roles(os=BIG["stable_path"])) == {
        "device": BIG["stable_path"], "data_disk": "", "wipe_disks_list": []}


def _form(**kw):
    base = {"mode": "create", "token": "t", "hostname": "h1", "device": "/dev/sda"}
    base.update(kw)
    return base


def test_build_form_config_wipe_disks_list_forms():
    for value in (["/dev/a", "/dev/b"], "/dev/a\n/dev/b", "/dev/a, /dev/b\n"):
        errors = []
        cfg = his.build_form_config(_form(wipe_disks_list=value), errors)
        assert cfg["install"]["wipe_disks_list"] == ["/dev/a", "/dev/b"]
        assert errors == []
        assert his.validate_install_config(cfg) == []


def test_build_form_config_without_wipe_list_unchanged():
    errors = []
    for value in (None, "", [], "  \n"):
        cfg = his.build_form_config(_form(wipe_disks_list=value), errors)
        assert "wipe_disks_list" not in cfg["install"]
    assert his.build_form_config(_form(), []) == his.build_form_config(
        _form(wipe_disks_list=""), [])


def test_install_fields_feed_the_form():
    roles = _roles(os=BIG["stable_path"], data=MID["stable_path"], wipe=[USED["stable_path"]])
    cfg = his.build_form_config(_form(**bd.install_fields(DISKS, roles)), [])
    assert cfg["install"]["device"] == BIG["stable_path"]
    assert cfg["install"]["data_disk"] == MID["stable_path"]
    assert cfg["install"]["wipe_disks_list"] == [USED["stable_path"]]
    assert "  wipe_disks_list:\n  - " + USED["stable_path"] in his.dump_install_config(cfg)
