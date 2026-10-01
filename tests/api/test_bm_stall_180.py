"""Leçons de l'installation réelle sur node4 (1.80.0) : une carte de gestion
à 1 Gbit/s fait refuser l'installation automatique par les contrôles
matériels de l'installeur, qui lit sa configuration puis ne demande jamais
l'image, sans rien dire ; la console attendait 60 minutes pour rien."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp  # noqa: E402
import pxe_server as px  # noqa: E402

FIXTURE = ROOT / "tests" / "api" / "fixtures" / "bm_disks_178" / "discovery.txt"


def _tokens(tmp_path):
    iso, cfg = tmp_path / "i.iso", tmp_path / "c.yaml"
    iso.write_bytes(b"x"); cfg.write_text("a: 1\n")
    return px.issue(iso, "iso"), px.issue(cfg, "config")


def test_stats_record_clients_and_first_hit(tmp_path):
    t_iso, _ = _tokens(tmp_path)
    assert px._resolve(t_iso, "iso", "192.0.2.10")
    assert px._resolve(t_iso, "iso", "192.0.2.20")
    st = px.stats(t_iso)
    assert st["clients"] == ["192.0.2.10", "192.0.2.20"] and st["first_hit"] > 0


def test_stalled_when_only_the_bmc_read_the_image(tmp_path, monkeypatch):
    t_iso, t_cfg = _tokens(tmp_path)
    px._resolve(t_cfg, "config", "192.0.2.50")          # l'installeur lit sa configuration
    px._resolve(t_iso, "iso", "192.0.2.34")             # le BMC lit l'ISO du média virtuel
    later = lambda: px.stats(t_cfg)["first_hit"] + wapp.BM_STALL_AFTER + 1
    why = wapp._bm_install_stalled(t_cfg, t_iso, "192.0.2.34", now=later)
    assert why and "skipchecks" in why


def test_not_stalled_once_the_installer_reads_the_image(tmp_path):
    t_iso, t_cfg = _tokens(tmp_path)
    px._resolve(t_cfg, "config", "192.0.2.50")
    px._resolve(t_iso, "iso", "192.0.2.34")
    px._resolve(t_iso, "iso", "192.0.2.50")             # l'installeur demande l'image
    later = lambda: px.stats(t_cfg)["first_hit"] + wapp.BM_STALL_AFTER + 1
    assert wapp._bm_install_stalled(t_cfg, t_iso, "192.0.2.34:443", now=later) is None


def test_not_stalled_before_the_delay(tmp_path):
    t_iso, t_cfg = _tokens(tmp_path)
    px._resolve(t_cfg, "config", "192.0.2.50")
    assert wapp._bm_install_stalled(t_cfg, t_iso, "192.0.2.34") is None


def test_wait_power_off_stops_on_a_stall(monkeypatch):
    run = wapp.ActionRun("st000000test", "baremetal-install:t", "(local)", [])
    monkeypatch.setattr(wapp, "_redfish_get", lambda *a, **k: {"PowerState": "On"})
    ok = wapp._bm_wait_power_off("bmc", "u", "p", "/s", 1e12, run, lambda *a: None,
                                 sleep=lambda s: None, now=lambda: 0.0, stalled=lambda: "bloqué")
    assert ok is False and run._stall == "bloqué"


def _inventory(speed):
    raw = FIXTURE.read_text()
    return {"raw": raw + f"\n== speeds\neth0 {speed}\n"}


def test_nic_warning_for_a_slow_management_nic(monkeypatch):
    import baremetal_disks as bd
    nics = bd.parse_discovery(FIXTURE.read_text())["nics"]
    mac = nics[0]["mac"]
    monkeypatch.setattr(wapp._bmd, "load_inventory", lambda *a, **k: {"raw": FIXTURE.read_text()})
    monkeypatch.setattr(wapp._bmdisks, "parse_discovery",
                        lambda raw: {"disks": [], "nics": [dict(nics[0], speed=1000)]})
    w = wapp._bm_nic_warning({"bmc_host": "192.0.2.9", "mgmt_interfaces": [mac]})
    assert w and "10 Gbit/s" in w and "1000 Mbit/s" in w
    assert wapp._bm_nic_warning({"bmc_host": "192.0.2.9", "mgmt_interfaces": [mac],
                                 "extra_args": "harvester.install.skipchecks=true"}) is None


def test_no_nic_warning_at_10_gbit(monkeypatch):
    monkeypatch.setattr(wapp._bmd, "load_inventory", lambda *a, **k: {"raw": ""})
    monkeypatch.setattr(wapp._bmdisks, "parse_discovery",
                        lambda raw: {"disks": [], "nics": [{"name": "eno1", "mac": "52:54:00:00:00:01", "speed": 10000}]})
    assert wapp._bm_nic_warning({"bmc_host": "192.0.2.9", "mgmt_interfaces": ["52:54:00:00:00:01"]}) is None


def test_runner_passes_the_stall_check():
    src = (ROOT / "web" / "app.py").read_text()
    runner = src.split("def _baremetal_install_runner", 1)[1].split("\ndef ", 1)[0]
    assert "stalled=lambda: _bm_install_stalled(cfg_token, iso_token, host)" in runner
    assert 'return fail("wait-install", run._stall)' in runner
