"""Fenêtre d'installation bare-metal (1.76.1) : le gestionnaire de mots de
passe du navigateur ne doit plus remplir le jeton du cluster, le mot de passe
de l'OS ni le champ DNS (pris pour un identifiant, vu en réel : « ju » dans
DNS), et les serveurs NTP, que le serveur savait déjà écrire, ont leur champ."""
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "web"))

import app as app_module  # noqa: E402

BMC_JS = (ROOT / "web/static/js/bmc.js").read_text()


def _install_form():
    start = BMC_JS.index('<form id="bm-install-form"')
    return BMC_JS[start:BMC_JS.index("</form>", start)]


def test_form_opts_out_of_autofill():
    assert re.search(r'<form id="bm-install-form"[^>]*autocomplete="off"', BMC_JS)


def test_secrets_are_new_passwords():
    form = _install_form()
    for name in ("token", "password"):
        tag = re.search(rf'<input name="{name}"[^>]*>', form).group(0)
        assert 'autocomplete="new-password"' in tag, name


def test_dns_and_ntp_fields_refuse_autofill():
    form = _install_form()
    for name in ("dns", "ntp"):
        tag = re.search(rf'<input name="{name}"[^>]*>', form)
        assert tag, name
        assert 'autocomplete="off"' in tag.group(0), name


def test_ntp_field_reaches_the_config():
    cfg = yaml.safe_load(app_module._harvester_install_config({
        "token": "t", "hostname": "n1", "device": "/dev/sda", "mgmt_interface": "eth0",
        "method": "dhcp", "vip": "192.0.2.100", "ntp": "192.0.2.1, 192.0.2.2", "dns": "192.0.2.3"}))
    assert cfg["os"]["ntp_servers"] == ["192.0.2.1", "192.0.2.2"]
    assert cfg["os"]["dns_nameservers"] == ["192.0.2.3"]
