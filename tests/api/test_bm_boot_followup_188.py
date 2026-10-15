"""1.88.0 : reprise du PR #5 (bond étiqueté, identité des cartes).

Ce que la reprise change et que ces tests tiennent :
- les noms hvbootN et les ifname= ne vont que dans l'ISO remasterisée ;
  un appelant qui démarre l'installeur lui-même (banc harvlab, PXE) garde
  la configuration d'avant ;
- la surcouche de l'initrd n'est posée qu'avec deux membres ou plus ;
- l'extinction ne renonce plus sur un état transitoire ou illisible.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp  # noqa: E402

BASE = {"token": "t" * 16, "password": "p" * 12, "mode": "create", "hostname": "n1",
        "device": "/dev/vda", "method": "dhcp", "vip": "192.0.2.60",
        "server_url": "https://192.0.2.60:443", "iso_url": "http://192.0.2.9/i.iso"}


@pytest.mark.parametrize("macs", [
    ["52:54:00:aa:bb:01"],
    ["52:54:00:aa:bb:01", "52:54:00:aa:bb:02"],
])
def test_names_only_go_to_the_remastered_iso(macs):
    opts = dict(BASE, mgmt_interfaces=macs)
    plain = yaml.safe_load(wapp._harvester_install_config(opts))
    assert plain["install"]["management_interface"]["interfaces"] == [{"hwAddr": m} for m in macs]
    assert "ifname=" not in str(plain["os"].get("additional_kernel_arguments") or "")
    assert not plain["os"].get("write_files")
    boot = yaml.safe_load(wapp._harvester_install_config(opts, boot_identity=True))
    assert boot["install"]["management_interface"]["interfaces"] == [
        {"hwAddr": m, "name": f"hvboot{i}"} for i, m in enumerate(macs)]
    args = boot["os"]["additional_kernel_arguments"].split()
    assert args == [f"ifname=hvboot{i}:{m}" for i, m in enumerate(macs)]


SCRIPT = (ROOT / "bin" / "harvester-iso-remaster.sh").read_text()


def overlay_applied(network_args):
    """Rejoue le bloc de la surcouche du script, outils remplacés."""
    start = SCRIPT.index("# Bond slaves share the current MAC")
    end = SCRIPT.index("# --- 3. recopier", start)
    stub = ('emit_event() { printf "EVENT %s %s\\n" "$1" "$2"; }\n'
            'log_error() { :; }\nxorriso() { :; }\npython3() { :; }\n'
            'MAPS=()\nWORK=/nonexistent\nSRC=/nonexistent\nSCRIPT_DIR=/nonexistent\n')
    r = subprocess.run(["bash", "-e", "-c", stub + SCRIPT[start:end]],
                       env=dict(os.environ, NETWORK_ARGS=network_args),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return "EVENT iso-identity running" in r.stdout


@pytest.mark.parametrize("network,expected", [
    ("rd.neednet=1 ifname=hvboot0:52:54:00:aa:bb:01 bond=hvbond0:hvboot0:mode=balance-tlb "
     "ip=hvbond0:dhcp bootdev=hvbond0", False),
    ("rd.neednet=1 ifname=hvboot0:52:54:00:aa:bb:01 ifname=hvboot1:52:54:00:aa:bb:02 "
     "bond=hvbond0:hvboot0,hvboot1:mode=802.3ad,miimon=100:9000 ip=hvbond0:dhcp", True),
    ("rd.neednet=1 bond=hvbond0:eth0,eth1:mode=802.3ad ip=hvbond0:dhcp", False),
    ("", False),
])
def test_initrd_overlay_only_with_two_members_selected_by_mac(network, expected):
    assert overlay_applied(network) is expected


class _Run:
    _cancel = False


@pytest.mark.parametrize("states,resets,ok", [
    (["Off"], [], True),
    (["On", "Off"], ["ForceOff"], True),
    (["PoweringOn", "Off"], ["ForceOff"], True),
    ([None, "Off"], ["ForceOff"], True),
    (["On", "On"], ["ForceOff"], False),
])
def test_power_off_forces_any_state_but_off(monkeypatch, states, resets, ok):
    seq = iter(states)
    last = [states[-1]]
    calls = []
    monkeypatch.setattr(wapp, "_redfish_get",
                        lambda *a, **k: {"PowerState": next(seq, last[0])})
    monkeypatch.setattr(wapp, "_bm_reset", lambda h, u, p, sp, t: calls.append(t) or (True, ""))
    t = [0.0]
    got, _ = wapp._bm_power_off("bmc", "u", "p", "/s", _Run(), off_wait=20,
                                sleep=lambda s: t.__setitem__(0, t[0] + s), now=lambda: t[0])
    assert (got, calls) == (ok, resets)
