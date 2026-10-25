"""Revue finale I2 : le network-config de l'assistant cloud-init des VMs KVM
désigne chaque carte par sa MAC (network-config v2, `match: macaddress`).

Avant, une entrée v1 `name: eth0` ne correspondait à aucune carte sur une
image récente (`enp1s0`, Tumbleweed) : l'invité retombait en DHCP sans le
dire. Le générateur est joué par node (sauté sans node), cas en tableau ;
la version v1 de Harvester ne change pas."""

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
JS = ROOT / "web" / "static" / "js" / "cloudinit-form.js"
M1, M2 = "52:54:00:aa:00:01", "52:54:00:aa:00:02"


def run(calls, src=None):
    """Joue `calls` ([fonction, spec, macs]) ; rend [{yaml} | {error}]."""
    js = """
      global.window = global;
      const src = require('fs').readFileSync(process.argv[1], 'utf8');
      eval(src.replace('const CloudInitForm =', 'global.CloudInitForm ='));
      const out = JSON.parse(process.argv[2]).map(([fn, spec, macs]) => {
        try { return { yaml: CloudInitForm[fn](spec, macs) }; }
        catch (e) { return { error: e.message }; }
      });
      console.log(JSON.stringify(out));"""
    path = src or JS
    r = subprocess.run(["node", "-e", js, str(path), json.dumps(calls)], capture_output=True,
                       text=True, cwd=str(ROOT))
    if r.returncode != 0:
        if "node" in r.stderr and "not found" in r.stderr:
            pytest.skip("node unavailable")
        raise AssertionError(r.stderr[:500])
    return json.loads(r.stdout)


CASES = [
    # (libellé, spec de l'assistant, MAC des cartes, sortie attendue ou erreur)
    ("no entry: first card on DHCP", {}, [M1],
     "version: 2\nethernets:\n  nic1:\n    match:\n      macaddress: '52:54:00:aa:00:01'\n"
     "    dhcp4: true\n"),
    ("static on the first card, default route as 0.0.0.0/0",
     {"nic": [{"card": 1, "mode": "static", "address": "10.213.77.10/24",
               "gateway": "10.213.77.1"}]}, [M1, M2],
     "version: 2\nethernets:\n  nic1:\n    match:\n      macaddress: '52:54:00:aa:00:01'\n"
     "    dhcp4: false\n    addresses:\n      - 10.213.77.10/24\n    routes:\n"
     "      - to: 0.0.0.0/0\n        via: 10.213.77.1\n"),
    ("second card renamed only when asked, upper-case MAC lowered",
     {"nic": [{"card": 2, "iface": "lan1", "mtu": 9000}]}, [M1, M2.upper()],
     "version: 2\nethernets:\n  nic2:\n    match:\n      macaddress: '52:54:00:aa:00:02'\n"
     "    set-name: lan1\n    mtu: 9000\n    dhcp4: true\n"),
    ("DNS and search on each entry",
     {"dns": "10.0.0.1, 10.0.0.2", "search": "home.lo", "nic": [{"card": 1}]}, [M1],
     "version: 2\nethernets:\n  nic1:\n    match:\n      macaddress: '52:54:00:aa:00:01'\n"
     "    dhcp4: true\n    nameservers:\n      addresses:\n        - 10.0.0.1\n        - 10.0.0.2\n"
     "      search:\n        - home.lo\n"),
    ("two cards", {"nic": [{"card": 1}, {"card": 2}]}, [M1, M2],
     "version: 2\nethernets:\n  nic1:\n    match:\n      macaddress: '52:54:00:aa:00:01'\n"
     "    dhcp4: true\n  nic2:\n    match:\n      macaddress: '52:54:00:aa:00:02'\n"
     "    dhcp4: true\n"),
    ("no card", {}, [], {"error": "the VM has no network card to configure"}),
    ("card out of range", {"nic": [{"card": 3}]}, [M1, M2],
     {"error": "card 3 does not exist: the VM has 2 card(s)"}),
    ("card twice", {"nic": [{"card": 1}, {"card": 1}]}, [M1],
     {"error": "card 1 is configured twice"}),
    ("static without address", {"nic": [{"card": 1, "mode": "static"}]}, [M1],
     {"error": "a CIDR address is required for static addressing (card 1)"}),
]


@pytest.fixture(scope="module")
def results():
    return run([["genNetworkDataByMac", c[1], c[2]] for c in CASES])


@pytest.mark.parametrize("i", range(len(CASES)), ids=[c[0] for c in CASES])
def test_network_config_by_mac(results, i):
    want = CASES[i][3]
    got = results[i]
    if isinstance(want, dict):
        assert got == want
    else:
        assert got == {"yaml": want}
        assert "eth0" not in got["yaml"]


def test_harvester_keeps_its_v1_network_data():
    (got,) = run([["genNetworkData", {"nic": [{"iface": "eth0", "mode": "dhcp"}]}, None]])
    assert got["yaml"].startswith("version: 1\nconfig:\n  - type: physical\n    name: eth0\n")


@pytest.mark.parametrize("label,old,new", [
    ("match by name instead of MAC", "L.push('    match:');\n      L.push(`      macaddress: ${yamlStr(macs[card - 1])}`);",
     "L.push(`    match:\\n      name: eth0`);"),
    ("set-name always written", "if (n.iface) L.push(`    set-name:", "L.push(`    set-name:"),
    ("first card always", "macs[card - 1]", "macs[0]"),
])
def test_mutations_are_detected(tmp_path, label, old, new):
    src = JS.read_text()
    assert old in src
    mutant = tmp_path / "cloudinit-form.js"
    mutant.write_text(src.replace(old, new, 1))
    got = run([["genNetworkDataByMac", c[1], c[2]] for c in CASES], mutant)
    assert any(g != (c[3] if isinstance(c[3], dict) else {"yaml": c[3]})
               for g, c in zip(got, CASES))
