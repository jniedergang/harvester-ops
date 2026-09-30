"""Configuration d'installation bare-metal complète (1.77.0) : schéma de
l'installeur, construction en dictionnaire, fusion du YAML avancé, refus
(clé inconnue, réservée, en conflit, mal typée) et découpage d'un fichier
importé. La copie de fichier d'exploitant est anonymisée."""
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "web"))

import app  # noqa: E402
import harvester_install_schema as his  # noqa: E402

FIXTURE = ROOT / "tests/api/fixtures/bm_config_177/operator.yaml"


# Rendu d'avant 1.77.0, figé tel quel : référence du rendu sémantiquement
# identique exigé pour les champs d'avant (harvlab.sh en dépend).
def _legacy_render(opts):
    """Rend la configuration d'installation Harvester (YAML).

    Volontairement écrite à la main plutôt que par un moteur de gabarit :
    la structure est courte, et une clé mal placée ici coûte une
    réinstallation complète."""
    def esc(v):
        v = str(v)
        return v if re.fullmatch(r"[A-Za-z0-9._:/@-]+", v) else json.dumps(v)

    L = ["scheme_version: 1"]
    joining = opts.get("mode") == "join"
    # Champ de PREMIER niveau (HarvesterConfig.ServerURL). Placé sous
    # `install` jusqu'en 1.44.1, il y était ignoré : un nœud ne pouvait pas
    # rejoindre un cluster.
    if joining:
        L.append(f"server_url: {esc(opts['server_url'])}")
    L.append(f"token: {esc(opts['token'])}")
    L.append("os:")
    L.append(f"  hostname: {esc(opts['hostname'])}")
    if opts.get("password"):
        L.append(f"  password: {esc(opts['password'])}")
    keys = [k.strip() for k in (opts.get("ssh_keys") or "").splitlines() if k.strip()]
    if keys:
        L.append("  ssh_authorized_keys:")
        L.extend(f"    - {json.dumps(k)}" for k in keys)
    ntp = [n.strip() for n in (opts.get("ntp") or "").split(",") if n.strip()]
    if ntp:
        L.append("  ntp_servers:")
        L.extend(f"    - {esc(n)}" for n in ntp)
    dns = [d.strip() for d in (opts.get("dns") or "").split(",") if d.strip()]
    if dns:
        L.append("  dns_nameservers:")
        L.extend(f"    - {esc(d)}" for d in dns)

    L.append("install:")
    L.append(f"  mode: {esc(opts.get('mode', 'create'))}")
    L.append(f"  device: {esc(opts['device'])}")
    # Obligatoire en mode automatique, l'installeur refuse la
    # configuration sans, avec « iso_url is required in automatic
    # installation », même quand l'image est déjà montée en média virtuel.
    if opts.get("iso_url"):
        L.append(f"  iso_url: {esc(opts['iso_url'])}")
    L.append("  management_interface:")
    L.append("    interfaces:")
    # Désigner la carte par son ADRESSE MAC quand on l'a. Redfish ne publie
    # pas le nom que Linux donnera à l'interface, sur les XL170r les deux
    # NICs s'appellent toutes deux « System Ethernet Interface », alors que
    # la MAC, elle, identifie sans ambiguïté et survit au renommage.
    iface = str(opts["mgmt_interface"]).strip()
    if re.fullmatch(r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", iface):
        L.append(f"      - hwAddr: {esc(iface.lower().replace('-', ':'))}")
    else:
        L.append(f"      - name: {esc(iface)}")
    method = opts.get("method", "dhcp")
    L.append(f"    method: {esc(method)}")
    if method == "static":
        L.append(f"    ip: {esc(opts['ip'])}")
        L.append(f"    subnet_mask: {esc(opts['subnet_mask'])}")
        L.append(f"    gateway: {esc(opts['gateway'])}")
    L.append(f"    bond_options:")
    L.append(f"      mode: {esc(opts.get('bond_mode', 'balance-tlb'))}")
    L.append("      miimon: 100")
    # La VIP appartient au cluster créé, pas à un nœud qui le rejoint.
    if not joining:
        L.append(f"  vip: {esc(opts['vip'])}")
        L.append(f"  vip_mode: {esc(opts.get('vip_mode', 'static'))}")
    return "\n".join(L) + "\n"



def _render(opts):
    return yaml.safe_load(app._harvester_install_config(opts))


# ---------------------------------------------------------------------------
# Rendu identique pour les champs d'avant
# ---------------------------------------------------------------------------

_HARVLAB = {
    "token": "example-token", "hostname": "harvlab-n1", "password": "$6$salt$hash",
    "ssh_keys": "ssh-ed25519 AAAAexample ops@example\n",
    "ntp": "0.suse.pool.ntp.org", "dns": "192.0.2.53",
    "mode": "create", "device": "/dev/vda",
    "iso_url": "http://192.0.2.9:8099/x/harvester.iso",
    "mgmt_interface": "52:54:00:4c:ab:61",
    "method": "static", "ip": "192.0.2.61", "subnet_mask": "255.255.0.0",
    "gateway": "192.0.2.1", "vip": "192.0.2.60",
    "server_url": "https://192.0.2.60:443",
}

LEGACY_CASES = [
    # test_baremetal.py
    {"token": "tok", "hostname": "harv3-node1", "password": "rancher",
     "ssh_keys": "ssh-ed25519 AAA ju@node1\n", "ntp": "0.pool.ntp.org",
     "dns": "192.0.2.6", "mode": "create", "device": "/dev/sda",
     "mgmt_interface": "eno1", "method": "static", "ip": "192.0.2.13",
     "subnet_mask": "255.255.0.0", "gateway": "192.0.2.1", "vip": "192.0.2.101"},
    {"token": "t", "hostname": "h", "device": "/dev/sda",
     "mgmt_interface": "eno1", "method": "dhcp", "vip": "1.2.3.4"},
    {"token": "t", "hostname": "harvlab-n2", "mode": "join",
     "server_url": "https://192.0.2.60:443", "device": "/dev/vda",
     "mgmt_interface": "52:54:00:4c:ab:62", "method": "static",
     "ip": "192.0.2.62", "subnet_mask": "255.255.0.0", "gateway": "192.0.2.1"},
    {"token": "t", "hostname": "h", "mode": "create", "device": "/dev/sda",
     "mgmt_interface": "eno1", "vip": "1.2.3.4",
     "server_url": "https://ignored:443"},
    {"token": "a: b #c", "hostname": "h", "password": "p@ss: word #1",
     "device": "/dev/sda", "mgmt_interface": "eno1", "vip": "1.2.3.4"},
    {"token": "t", "hostname": "h", "device": "/dev/sda", "vip": "10.0.0.1",
     "method": "dhcp", "mgmt_interface": "D0-67-26-D5-4A-F8"},
    {"token": "t", "hostname": "h", "device": "/dev/sda", "vip": "10.0.0.1",
     "method": "dhcp", "mgmt_interface": "eno1",
     "iso_url": "http://10.0.0.9:8091/pxe/iso/TOK.iso", "bond_mode": "active-backup"},
    # test_bm_form_1761.py
    {"token": "t", "hostname": "n1", "device": "/dev/sda", "mgmt_interface": "eth0",
     "method": "dhcp", "vip": "192.0.2.100", "ntp": "192.0.2.1, 192.0.2.2",
     "dns": "192.0.2.3"},
    # harvlab.sh, création puis jonction
    _HARVLAB,
    dict(_HARVLAB, mode="join", hostname="harvlab-n2", ip="192.0.2.62",
         mgmt_interface="52:54:00:4c:ab:62"),
]


@pytest.mark.parametrize("opts", LEGACY_CASES)
def test_old_fields_render_the_same_dict_as_before(opts):
    """Identique là où l'ancienne sortie était bien typée : l'ancien rendu
    laissait sans guillemets une MAC tout en chiffres, que YAML 1.1 lit comme
    un entier sexagésimal ; le nouveau l'écrit en texte (voir le test
    suivant)."""
    assert _render(opts) == yaml.safe_load(_legacy_render(opts))


def test_harvlab_still_calls_the_same_function_with_the_same_fields():
    src = (ROOT / "tests/bench/harvlab/harvlab.sh").read_text()
    # depuis 1.77.0 le banc fusionne des options de plus (HARVLAB_CONFIG_EXTRA)
    # avant d'appeler la même fonction
    assert "app._harvester_install_config(opts)" in src
    for field in ("mgmt_interface", "server_url", "iso_url", "vip"):
        assert f'"{field}"' in src


# ---------------------------------------------------------------------------
# Schéma
# ---------------------------------------------------------------------------

def test_schema_records_its_upstream_origin():
    src = (ROOT / "web/harvester_install_schema.py").read_text()
    assert his.HARVESTER_INSTALLER_COMMIT in src
    assert re.fullmatch(r"[0-9a-f]{40}", his.HARVESTER_INSTALLER_COMMIT)
    assert his.HARVESTER_INSTALLER_TAG.startswith("v1.9.0")


def test_to_yaml_key_is_the_rancher_mapper_port():
    assert his.to_yaml_key("subnetMask") == "subnet_mask"
    assert his.to_yaml_key("kubeovnChartVersion") == "kubeovn_chart_version"
    assert his.to_yaml_key("skipchecks") == "skipchecks"
    # une suite de majuscules est découpée lettre par lettre
    assert his.to_yaml_key("guaranteedEngineManagerCPU") == \
        "guaranteed_engine_manager_c_p_u"


def test_every_key_the_window_writes_is_in_the_schema():
    opts = dict(_HARVLAB, mgmt_interfaces=["52:54:00:4c:ab:61", "eno2"],
                bond_mode="802.3ad", bond_miimon="100", bond_lacp_rate="fast",
                bond_xmit_hash_policy="layer3+4", vlan_id="200",
                data_disk="/dev/sdb", wipe_all_disks=True,
                labels="topology.kubernetes.io/zone=zone-a", modules="rbd, nbd")
    for mode in ("create", "join"):
        errors = []
        cfg = his.build_form_config(dict(opts, mode=mode), errors)
        assert errors == []
        assert his.validate_install_config(cfg) == [], mode
    for path in ("install.management_interface.vlan_id",
                 "install.management_interface.bond_options",
                 "install.data_disk", "install.wipe_all_disks",
                 "os.labels", "os.modules", "os.write_files",
                 "os.persistent_state_paths", "system_settings", "server_url"):
        assert his.field_at(path) is not None, path


def test_spellings_accepted_by_the_installer():
    base = {"install": {"management_interface": {"interfaces": [{"hw_addr": "aa:bb:cc:dd:ee:ff"}]}}}
    assert his.validate_install_config(base) == []
    camel = {"install": {"managementInterface": {"subnetMask": "255.255.255.0"}}}
    assert his.validate_install_config(camel) == []
    assert his.validate_install_config({"install": {"skipchecks": True}}) == []
    # `skip_checks` n'existe pas pour l'installeur : ignoré là-bas, refusé ici
    assert his.validate_install_config({"install": {"skip_checks": True}}) == \
        ["install.skip_checks"]
    # variante accidentelle de rename.go : refusée
    assert his.validate_install_config({"os": {"module": ["rbd"]}}) == ["os.module"]


def test_same_field_under_two_spellings_is_refused():
    cfg = {"install": {"vip_mode": "static", "vipMode": "dhcp"}}
    assert his.check_install_config(cfg) == [("install.vipMode", "duplicate")]


def test_unknown_system_setting_is_refused():
    assert his.validate_install_config(
        {"system_settings": {"auto-disk-provision-paths": "/dev/sd*"}}) == []
    assert his.check_install_config({"system_settings": {"no-such": "x"}}) == \
        [("system_settings[no-such]", "unknown-setting")]


def test_wrong_types_are_refused_with_their_path():
    cfg = {"os": {"modules": "rbd", "labels": {"a": ["x"]}, "hostname": 12},
           "install": {"wipe_all_disks": "yes",
                       "management_interface": {"vlan_id": "200", "mtu": True}}}
    assert dict(his.check_install_config(cfg)) == {
        "os.modules": "type:list[str]",
        "os.labels[a]": "type:str",
        "os.hostname": "type:str",
        "install.wipe_all_disks": "type:bool",
        "install.management_interface.vlan_id": "type:int",
        "install.management_interface.mtu": "type:int",
    }
    # scalaires acceptés dans un dictionnaire de textes (l'installeur les convertit)
    assert his.validate_install_config(
        {"install": {"management_interface": {"bond_options": {"miimon": 100}}}}) == []


def test_unknown_key_path_is_exact():
    cfg = yaml.safe_load(FIXTURE.read_text())
    cfg["os"]["write_files"][2]["contnt"] = cfg["os"]["write_files"][2].pop("content")
    assert his.validate_install_config(cfg) == ["os.write_files[2].contnt"]


# ---------------------------------------------------------------------------
# Nouveaux champs du formulaire, YAML avancé
# ---------------------------------------------------------------------------

_BASE = {"token": "example-token", "hostname": "hv-node-01", "device": "/dev/sda",
         "mgmt_interface": "eno1", "method": "dhcp", "vip": "192.0.2.100"}


def test_bonded_vlan_management_interface():
    d = _render(dict(_BASE, mgmt_interface=None,
                     mgmt_interfaces=["D0:67:26:D5:4A:F8", "ens15f1np1"],
                     bond_mode="802.3ad", bond_miimon="200", bond_lacp_rate="fast",
                     bond_xmit_hash_policy="layer3+4", vlan_id="200",
                     data_disk="/dev/sdb", wipe_all_disks=True,
                     labels="topology.kubernetes.io/zone=zone-a\n\nrack = r1",
                     modules="rbd,nbd"))
    mi = d["install"]["management_interface"]
    assert mi["interfaces"] == [{"hwAddr": "d0:67:26:d5:4a:f8"}, {"name": "ens15f1np1"}]
    assert mi["bond_options"] == {"mode": "802.3ad", "miimon": 200,
                                  "lacp_rate": "fast", "xmit_hash_policy": "layer3+4"}
    assert mi["vlan_id"] == 200
    assert d["install"]["data_disk"] == "/dev/sdb"
    assert d["install"]["wipe_all_disks"] is True
    assert d["os"]["labels"] == {"topology.kubernetes.io/zone": "zone-a", "rack": "r1"}
    assert d["os"]["modules"] == ["rbd", "nbd"]


def test_mgmt_interfaces_wins_over_the_single_field():
    d = _render(dict(_BASE, mgmt_interfaces="eno1, eno2", mgmt_interface="eth9"))
    assert d["install"]["management_interface"]["interfaces"] == \
        [{"name": "eno1"}, {"name": "eno2"}]


def test_bad_form_values_are_pointed():
    with pytest.raises(app.InstallConfigError) as e:
        app._harvester_install_config(dict(_BASE, vlan_id="abc"))
    assert e.value.paths == ["install.management_interface.vlan_id"]
    with pytest.raises(app.InstallConfigError) as e:
        app._harvester_install_config(dict(_BASE, labels="no-equal-sign"))
    assert e.value.paths == ["os.labels"]


ADVANCED = """
os:
  write_files:
  - path: /etc/ssh/sshd_config.d/00-custom.conf
    permissions: '0644'
    content: |
      ListenAddress 192.0.2.11
      PermitRootLogin no
  persistent_state_paths: [/var/lib/rook]
  sysctls:
    vm.max_map_count: 262144
install:
  management_interface:
    mtu: 9000
    bond_options:
      primary: eno1
system_settings:
  auto-disk-provision-paths: /dev/sd*
"""


def test_advanced_yaml_is_merged_and_keeps_literal_blocks():
    text = app._harvester_install_config(dict(_BASE, advanced_yaml=ADVANCED))
    d = yaml.safe_load(text)
    assert d["os"]["hostname"] == "hv-node-01"
    assert d["os"]["write_files"][0]["content"] == \
        "ListenAddress 192.0.2.11\nPermitRootLogin no\n"
    assert "content: |" in text
    assert d["os"]["sysctls"] == {"vm.max_map_count": 262144}
    mi = d["install"]["management_interface"]
    assert mi["mtu"] == 9000
    # fusion clé par clé dans un dictionnaire posé par le formulaire
    assert mi["bond_options"] == {"mode": "balance-tlb", "miimon": 100, "primary": "eno1"}
    assert d["system_settings"] == {"auto-disk-provision-paths": "/dev/sd*"}
    # ordre conservé : le formulaire d'abord
    assert list(d)[:4] == ["scheme_version", "token", "os", "install"]


def test_advanced_alias_is_written_under_the_output_name():
    d = _render(dict(_BASE, advanced_yaml="install:\n  dataDisk: /dev/sdc\n"))
    assert d["install"]["data_disk"] == "/dev/sdc" and "dataDisk" not in d["install"]


@pytest.mark.parametrize("adv, path", [
    ("install:\n  iso_url: http://192.0.2.9/x.iso\n", "install.iso_url"),
    ("install:\n  isoUrl: http://192.0.2.9/x.iso\n", "install.iso_url"),
    ("install:\n  automatic: true\n", "install.automatic"),
    ("install:\n  mode: join\n", "install.mode"),
    ("server_url: https://192.0.2.60:443\n", "server_url"),
    ("token: other-token\n", "token"),
    ("os:\n  password: other-password\n", "os.password"),
])
def test_reserved_keys_are_refused(adv, path):
    with pytest.raises(app.InstallConfigError) as e:
        app._harvester_install_config(dict(_BASE, advanced_yaml=adv))
    assert e.value.paths == [path]
    assert e.value.reasons == {path: "reserved"}
    assert "other-" not in str(e.value) and "other-" not in json.dumps(e.value.paths)


def test_form_and_advanced_conflict_is_refused_not_overwritten():
    adv = ("os:\n  hostname: other\ninstall:\n  management_interface:\n"
           "    bond_options:\n      mode: active-backup\n")
    with pytest.raises(app.InstallConfigError) as e:
        app._harvester_install_config(dict(_BASE, advanced_yaml=adv))
    assert e.value.paths == ["os.hostname",
                             "install.management_interface.bond_options.mode"]
    assert set(e.value.reasons.values()) == {"conflict"}
    # même conflit écrit sous l'autre orthographe acceptée
    with pytest.raises(app.InstallConfigError) as e:
        app._harvester_install_config(dict(
            _BASE, advanced_yaml="install:\n  managementInterface:\n    method: static\n"))
    assert e.value.paths == ["install.management_interface.method"]


def test_advanced_unknown_key_and_wrong_type():
    with pytest.raises(app.InstallConfigError) as e:
        app._harvester_install_config(dict(
            _BASE, advanced_yaml="os:\n  write_files:\n  - path: /x\n    contnt: y\n"
                                 "  modules: rbd\n"))
    assert e.value.paths == ["os.write_files[0].contnt", "os.modules"]


def test_advanced_yaml_must_be_a_mapping_and_errors_carry_no_text():
    with pytest.raises(app.InstallConfigError) as e:
        app._harvester_install_config(dict(_BASE, advanced_yaml="- a\n- b\n"))
    assert e.value.paths == ["advanced_yaml"]
    with pytest.raises(app.InstallConfigError) as e:
        app._harvester_install_config(dict(
            _BASE, advanced_yaml="os:\n  x: [secret-value-123\n"))
    assert e.value.paths == ["advanced_yaml"]
    assert "secret-value-123" not in str(e.value)
    assert "line" in str(e.value)


def test_blank_advanced_yaml_is_ignored():
    assert _render(dict(_BASE, advanced_yaml="  \n")) == _render(_BASE)


# ---------------------------------------------------------------------------
# Découpage d'un fichier importé
# ---------------------------------------------------------------------------

def _recompose(split, iso_url="http://192.0.2.9:8091/pxe/iso/TOK.iso"):
    opts = dict(split["form"], advanced_yaml=split["advanced"], iso_url=iso_url,
                **split["secrets"])
    return _render(opts)


@pytest.mark.parametrize("crlf", [False, True])
def test_operator_file_split_then_recomposed_is_the_same(crlf):
    text = FIXTURE.read_text()
    if crlf:
        text = text.replace("\n", "\r\n")
    original = yaml.safe_load(text)
    split = app.split_imported_config(text)
    assert split["errors"] == []
    assert split["notes"] == ["iso-url-replaced"]
    assert split["secrets"] == {"token": "example-token", "password": "example-password"}
    form = split["form"]
    assert form["mgmt_interfaces"] == ["ens1f1np1", "ens15f1np1"]
    assert form["bond_mode"] == "802.3ad" and form["vlan_id"] == "200"
    assert form["wipe_all_disks"] is True
    assert "write_files" in split["advanced"] and "content: |" in split["advanced"]
    # aucun secret hors de `secrets`
    blob = json.dumps(form) + split["advanced"]
    assert "example-token" not in blob and "example-password" not in blob
    assert "iso_url" not in split["advanced"]

    expected = dict(original)
    expected["install"] = dict(original["install"],
                               iso_url="http://192.0.2.9:8091/pxe/iso/TOK.iso")
    assert _recompose(split) == expected


def test_fixture_is_anonymised():
    text = FIXTURE.read_text()
    for addr in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text):
        if addr.startswith("255."):
            continue                      # masque de sous-réseau
        assert addr.startswith(("192.0.2.", "198.51.100.", "203.0.113.")), addr


def test_import_refuses_what_is_not_a_mapping():
    for text in ("- a\n", "just text\n", "a: [1\n"):
        r = app.split_imported_config(text)
        assert r["errors"] == ["file"]
        assert r["form"] == {} and r["advanced"] == "" and r["secrets"] == {}


def test_import_with_unknown_key_fills_nothing():
    r = app.split_imported_config("token: example-token\nos:\n  hostnam: h\n")
    assert r == {"form": {}, "advanced": "", "secrets": {}, "notes": [],
                 "errors": ["os.hostnam"]}


def test_import_detects_join_from_server_url():
    r = app.split_imported_config(
        "server_url: https://192.0.2.60:443\ntoken: example-token\n"
        "install:\n  automatic: true\n  vip: 192.0.2.60\n  management_interface:\n"
        "    method: dhcp\n    interfaces:\n    - hwAddr: 52:54:00:4c:ab:62\n")
    assert r["errors"] == []
    assert r["form"]["mode"] == "join"
    assert r["form"]["server_url"] == "https://192.0.2.60:443"
    assert r["form"]["mgmt_interfaces"] == ["52:54:00:4c:ab:62"]
    assert "join-detected" in r["notes"] and "automatic-ignored" in r["notes"]
    # la VIP n'est pas écrite par le formulaire en jonction : elle reste avancée
    assert "vip" not in r["form"] and "vip: 192.0.2.60" in r["advanced"]


def test_import_refuses_server_url_on_create():
    r = app.split_imported_config(
        "server_url: https://192.0.2.60:443\ninstall:\n  mode: create\n")
    assert r["errors"] == ["server_url"]


def test_all_digit_mac_is_now_text_where_the_old_render_made_an_int():
    opts = dict(_BASE, mgmt_interface=None, mgmt_interfaces=["52:54:00:12:34:56"])
    old = yaml.safe_load(_legacy_render(dict(_BASE, mgmt_interface="52:54:00:12:34:56")))
    assert isinstance(old["install"]["management_interface"]["interfaces"][0]["hwAddr"], int)
    assert _render(opts)["install"]["management_interface"]["interfaces"] == \
        [{"hwAddr": "52:54:00:12:34:56"}]


# ---------------------------------------------------------------------------
# Correctifs de revue (1.77.0)
# ---------------------------------------------------------------------------

_NO_BOND = """scheme_version: 1
token: example-token
os:
  hostname: hv-node-01
install:
  mode: create
  device: /dev/sda
  vip: 192.0.2.100
  vip_mode: static
  management_interface:
    method: dhcp
    interfaces:
    - name: eno1
"""


def test_import_without_bond_options_keeps_the_installer_default():
    """Sans `bond_options`, l'installeur pose active-backup (cos.go,
    updateBond) : le formulaire aurait recomposé balance-tlb en silence."""
    split = app.split_imported_config(_NO_BOND)
    assert split["errors"] == []
    assert split["form"]["bond_mode"] == "active-backup"
    assert split["form"]["bond_miimon"] == "100"
    assert "bond-default-active-backup" in split["notes"]
    d = _recompose(split)
    expected = yaml.safe_load(_NO_BOND)
    expected["install"]["iso_url"] = "http://192.0.2.9:8091/pxe/iso/TOK.iso"
    expected["install"]["management_interface"]["bond_options"] = \
        {"mode": "active-backup", "miimon": 100}
    assert d == expected


def test_import_bond_options_without_mode_stays_without_mode():
    text = _NO_BOND.replace("    method: dhcp\n",
                            "    method: dhcp\n    bond_options:\n      primary: eno1\n")
    split = app.split_imported_config(text)
    assert split["errors"] == []
    assert split["form"]["bond_mode"] == "" and split["form"]["bond_miimon"] == ""
    assert "bond-default-active-backup" not in split["notes"]
    d = _recompose(split)
    assert d["install"]["management_interface"]["bond_options"] == {"primary": "eno1"}
    expected = yaml.safe_load(text)
    expected["install"]["iso_url"] = "http://192.0.2.9:8091/pxe/iso/TOK.iso"
    assert d == expected


def test_empty_bond_fields_write_nothing_but_absent_fields_keep_the_old_default():
    assert _render(dict(_BASE, bond_mode="", bond_miimon=""))[
        "install"]["management_interface"]["bond_options"] == {}
    assert _render(_BASE)["install"]["management_interface"]["bond_options"] == \
        {"mode": "balance-tlb", "miimon": 100}


@pytest.mark.parametrize("vlan, ok", [(0, True), (1, True), (4094, True),
                                      (4095, False), (-1, False)])
def test_vlan_id_range(vlan, ok):
    cfg = {"install": {"management_interface": {"vlan_id": vlan}}}
    expected = [] if ok else [("install.management_interface.vlan_id", "range:0-4094")]
    assert his.check_install_config(cfg) == expected


def test_vlan_id_out_of_range_from_the_form_is_pointed():
    with pytest.raises(app.InstallConfigError) as e:
        app._harvester_install_config(dict(_BASE, vlan_id="5000"))
    assert e.value.reasons == {"install.management_interface.vlan_id": "range:0-4094"}


def test_uint32_fields_refuse_negative_numbers():
    for path in ("scheme_version", "install.harvester.storage_class.replica_count",
                 "install.harvester.longhorn.default_settings.guaranteedInstanceManagerCPU"):
        assert his.field_at(path).type == "uint", path
    cfg = {"scheme_version": -1,
           "install": {"harvester": {"storage_class": {"replica_count": -2}}}}
    assert dict(his.check_install_config(cfg)) == {
        "scheme_version": "type:uint",
        "install.harvester.storage_class.replica_count": "type:uint"}
    assert his.validate_install_config(
        {"install": {"harvester": {"storage_class": {"replica_count": 3}}}}) == []
    # int signé : le MTU n'est pas un uint côté Go
    assert his.field_at("install.management_interface.mtu").type == "int"


@pytest.mark.parametrize("section", [
    "os:\n  labels:\n    k: {v}\n",
    "os:\n  environment:\n    K: {v}\n",
    "os:\n  sysctls:\n    k: {v}\n",
    "system_settings:\n  log-level: {v}\n",
    "install:\n  management_interface:\n    bond_options:\n      k: {v}\n",
])
def test_text_maps_refuse_booleans_and_floats(section):
    for literal in ("yes", "true", "on", "1.10"):
        cfg = yaml.safe_load(section.format(v=literal))
        errs = his.check_install_config(cfg)
        assert len(errs) == 1 and errs[0][1] == "type:str", (section, literal, errs)
    for literal in ('"yes"', "100", "debug"):
        assert his.validate_install_config(yaml.safe_load(section.format(v=literal))) == []
