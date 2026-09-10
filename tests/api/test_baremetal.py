"""v1.17.0 — socle du déploiement bare-metal : Redfish avancé + serveur
d'artefacts.

Tout ce qui est verrouillé ici a d'abord été constaté EN RÉEL sur node3
(iLO 4, licence Advanced) — conformément à la règle projet « tester en
réel ». Les surprises que ces tests figent :

* l'iLO 4 publie `BootSourceOverrideSupported` là où les Redfish récents
  publient `...@Redfish.AllowableValues` : lire un seul des deux donnait
  une liste de cibles vide ;
* l'action OEM `#HpiLOVirtualMedia.InsertVirtualMedia` **rejette**
  `Inserted` et `WriteProtected` (`ActionParameterUnknown`) alors que
  l'action standard `#VirtualMedia.InsertMedia` les attend ;
* le chemin du System était codé en dur (`/redfish/v1/Systems/1`), ce qui
  cassait les actions d'alimentation sur iDRAC.
"""

import json
import re
import sys
import tempfile
import time
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
WEB = ROOT / "web"
sys.path.insert(0, str(WEB))

import app as wapp          # noqa: E402
import pxe_server as px     # noqa: E402


@pytest.fixture()
def client():
    wapp.app.config["TESTING"] = True
    return wapp.app.test_client()


# ---------------------------------------------------------------------------
# Redfish : résolution dynamique et dialectes
# ---------------------------------------------------------------------------

def test_action_target_prefers_standard_and_flags_oem():
    """Le dialecte doit être identifié : la charge utile en dépend."""
    standard = {"Actions": {"#VirtualMedia.InsertMedia": {"target": "/std"}}}
    assert wapp._redfish_action_target(standard, "InsertMedia") == ("/std", False)

    oem = {"Oem": {"Hp": {"Actions": {
        "#HpiLOVirtualMedia.InsertVirtualMedia": {"target": "/oem"}}}}}
    assert wapp._redfish_action_target(
        oem, "InsertVirtualMedia", "InsertMedia") == ("/oem", True)

    assert wapp._redfish_action_target({}, "InsertMedia") == (None, False)


def test_insert_payload_matches_the_dialect(client, monkeypatch):
    """Constaté sur node3 : envoyer Inserted/WriteProtected à l'action OEM
    fait échouer l'insertion avec ActionParameterUnknown."""
    sent = {}

    def fake_vm(host, user, pwd, manager_path=None):
        return "/redfish/v1/Managers/1/VirtualMedia/2/", {
            "MediaTypes": ["CD", "DVD"],
            "Oem": {"Hp": {"Actions": {
                "#HpiLOVirtualMedia.InsertVirtualMedia": {"target": "/oem/insert"}}}},
        }

    def fake_send(host, path, user, pwd, method, payload=None, timeout=20):
        sent["path"], sent["payload"] = path, payload
        return True, 200, ""

    monkeypatch.setattr(wapp, "_redfish_virtualmedia_cd", fake_vm)
    monkeypatch.setattr(wapp, "_redfish_send", fake_send)
    r = client.post("/api/bmc/1.2.3.4/virtualmedia",
                    json={"action": "insert", "image": "http://x/y.iso",
                          "user": "u", "password": "p"})
    assert r.status_code == 200
    assert sent["path"] == "/oem/insert"
    assert sent["payload"] == {"Image": "http://x/y.iso"}, (
        "l'action OEM n'accepte que Image")


def test_insert_refuses_when_no_cd_media(client, monkeypatch):
    """Sans licence iLO Advanced, aucun lecteur CD n'est exposé : le dire
    clairement plutôt que de laisser un 500 obscur."""
    monkeypatch.setattr(wapp, "_redfish_virtualmedia_cd",
                        lambda *a, **k: (None, None))
    r = client.post("/api/bmc/1.2.3.4/virtualmedia",
                    json={"action": "insert", "image": "http://x/y.iso"})
    assert r.status_code == 412
    assert "Advanced" in r.get_json()["detail"]


def test_boot_once_uses_the_resolved_system_path(client, monkeypatch):
    sent = {}

    def fake_send(host, path, user, pwd, method, payload=None, timeout=20):
        sent["path"], sent["payload"], sent["method"] = path, payload, method
        return True, 200, ""

    monkeypatch.setattr(wapp, "_redfish_system_path",
                        lambda *a: "/redfish/v1/Systems/System.Embedded.1")
    monkeypatch.setattr(wapp, "_redfish_send", fake_send)
    r = client.post("/api/bmc/1.2.3.4/boot-once", json={"target": "Cd"})
    assert r.status_code == 200
    assert sent["method"] == "PATCH"
    assert sent["path"] == "/redfish/v1/Systems/System.Embedded.1"
    assert sent["payload"]["Boot"] == {"BootSourceOverrideTarget": "Cd",
                                       "BootSourceOverrideEnabled": "Once"}


def test_power_action_no_longer_hardcodes_systems_1():
    """Le chemin codé en dur cassait l'alimentation sur iDRAC alors que la
    découverte, elle, résolvait déjà dynamiquement."""
    src = (WEB / "app.py").read_text()
    assert '"https://{host}/redfish/v1/Systems/1/Actions' not in src
    assert "_redfish_system_path(host, user, pwd)" in src


def test_discovery_reads_both_boot_target_schemas():
    src = (WEB / "app.py").read_text()
    assert "BootSourceOverrideTarget@Redfish.AllowableValues" in src
    assert "BootSourceOverrideSupported" in src, (
        "l'iLO 4 n'expose que cette clé — la manquer vide la liste")


# ---------------------------------------------------------------------------
# Serveur d'artefacts
# ---------------------------------------------------------------------------

@pytest.fixture()
def served():
    d = Path(tempfile.mkdtemp())
    iso = d / "x.iso"
    iso.write_bytes(bytes(range(256)) * 40)          # 10240 octets
    cfg = d / "c.yaml"
    cfg.write_text("scheme_version: 1\n")
    port = px.start(port=0, bind="127.0.0.1")
    yield port, iso, cfg
    px.stop()


def _get(port, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, b"", {}


def test_artifact_server_serves_only_issued_tokens(served):
    port, iso, cfg = served
    t_iso = px.issue(iso, "iso")
    t_cfg = px.issue(cfg, "config")

    assert _get(port, f"/pxe/iso/{t_iso}.iso")[0] == 200
    assert _get(port, f"/pxe/config/{t_cfg}.yaml")[0] == 200
    # un jeton ne vaut que pour son type
    assert _get(port, f"/pxe/config/{t_iso}.yaml")[0] == 404
    assert _get(port, "/pxe/iso/inconnu.iso")[0] == 404
    # ce n'est pas un serveur de fichiers
    assert _get(port, "/etc/passwd")[0] == 404
    assert _get(port, "/pxe/iso/..%2f..%2fetc%2fpasswd.iso")[0] == 404


def test_artifact_server_supports_ranges(served):
    """Un BMC télécharge un ISO par plages : sans 206 correct, l'insertion
    de média échoue ou repart sans fin."""
    port, iso, _ = served
    t = px.issue(iso, "iso")
    status, body, headers = _get(port, f"/pxe/iso/{t}.iso",
                                 {"Range": "bytes=100-199"})
    assert status == 206
    assert len(body) == 100
    assert headers["Content-Range"] == "bytes 100-199/10240"
    assert headers["Accept-Ranges"] == "bytes"
    # plage hors bornes
    assert _get(port, f"/pxe/iso/{t}.iso", {"Range": "bytes=99999-"})[0] == 416


def test_artifact_tokens_expire_and_can_be_revoked(served):
    port, iso, _ = served
    t = px.issue(iso, "iso")
    assert _get(port, f"/pxe/iso/{t}.iso")[0] == 200
    px.revoke(t)
    assert _get(port, f"/pxe/iso/{t}.iso")[0] == 404

    expired = px.issue(iso, "iso", ttl=-1)
    assert _get(port, f"/pxe/iso/{expired}.iso")[0] == 404


def test_artifact_server_is_not_the_authenticated_flask_app():
    """Le BMC ne sait ni s'authentifier ni faire confiance au certificat
    auto-signé : les artefacts doivent sortir par un listener séparé."""
    src = (WEB / "pxe_server.py").read_text()
    assert "ThreadingHTTPServer" in src
    assert "requires_auth" not in src
    # pas de dépendance nouvelle
    assert "import requests" not in src


# ---------------------------------------------------------------------------
# Configuration d'installation et orchestration (v1.18.0)
# ---------------------------------------------------------------------------

def test_install_config_is_valid_yaml_and_well_shaped():
    yaml = pytest.importorskip("yaml")
    cfg = wapp._harvester_install_config({
        "token": "tok", "hostname": "harv3-node1", "password": "rancher",
        "ssh_keys": "ssh-ed25519 AAA ju@node1\n", "ntp": "0.pool.ntp.org",
        "dns": "172.16.3.6", "mode": "create", "device": "/dev/sda",
        "mgmt_interface": "eno1", "method": "static", "ip": "172.16.3.13",
        "subnet_mask": "255.255.0.0", "gateway": "172.16.0.1",
        "vip": "172.16.3.101",
    })
    d = yaml.safe_load(cfg)
    assert d["scheme_version"] == 1
    assert d["install"]["mode"] == "create"
    assert d["install"]["device"] == "/dev/sda"
    assert d["install"]["vip"] == "172.16.3.101"
    mi = d["install"]["management_interface"]
    assert mi["interfaces"] == [{"name": "eno1"}]
    assert mi["method"] == "static" and mi["ip"] == "172.16.3.13"
    assert d["os"]["ssh_authorized_keys"] == ["ssh-ed25519 AAA ju@node1"]


def test_install_config_omits_static_fields_in_dhcp():
    yaml = pytest.importorskip("yaml")
    d = yaml.safe_load(wapp._harvester_install_config({
        "token": "t", "hostname": "h", "device": "/dev/sda",
        "mgmt_interface": "eno1", "method": "dhcp", "vip": "1.2.3.4",
    }))
    mi = d["install"]["management_interface"]
    assert mi["method"] == "dhcp"
    for k in ("ip", "subnet_mask", "gateway"):
        assert k not in mi, f"{k} n'a aucun sens en DHCP"


def test_install_config_quotes_hostile_values():
    """Un mot de passe avec deux-points ou dièse casserait le YAML."""
    yaml = pytest.importorskip("yaml")
    d = yaml.safe_load(wapp._harvester_install_config({
        "token": "a: b #c", "hostname": "h", "password": "p@ss: word #1",
        "device": "/dev/sda", "mgmt_interface": "eno1", "vip": "1.2.3.4",
    }))
    assert d["token"] == "a: b #c"
    assert d["os"]["password"] == "p@ss: word #1"


def test_install_endpoint_validates_before_touching_hardware(client):
    r = client.post("/api/baremetal/install", json={})
    assert r.status_code == 400
    assert "bmc_host" in r.get_json()["fields"]

    base = {"bmc_host": "1.2.3.4", "bmc_user": "u", "bmc_password": "p",
            "iso": "h.iso", "hostname": "h", "device": "/dev/sda",
            "mgmt_interface": "eno1", "vip": "1.2.3.4", "token": "t"}
    # static sans adresse : refusé avant tout allumage
    r = client.post("/api/baremetal/install", json={**base, "method": "static"})
    assert r.status_code == 400 and r.get_json()["fields"] == ["ip"]
    # nom d'ISO piégé
    r = client.post("/api/baremetal/install",
                    json={**base, "iso": "../../etc/passwd.iso"})
    assert r.status_code == 400


def test_install_action_label_carries_no_secret(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(wapp, "track_action",
                        lambda label, cluster, worker, *a: seen.setdefault("label", label) or "id1")
    r = client.post("/api/baremetal/install", json={
        "bmc_host": "1.2.3.4", "bmc_user": "u", "bmc_password": "SECRET",
        "iso": "h.iso", "hostname": "harv3", "device": "/dev/sda",
        "mgmt_interface": "eno1", "vip": "1.2.3.4", "token": "TOKENSECRET"})
    assert r.status_code == 202
    assert "SECRET" not in seen["label"] and "TOKENSECRET" not in seen["label"]


def test_advertise_ip_is_routable_towards_the_bmc():
    """L'URL de l'ISO est consommée PAR LE BMC : publier 127.0.0.1 rendrait
    l'insertion impossible."""
    ip = wapp._bm_local_ip_for("172.16.1.33")
    assert ip and not ip.startswith("127."), ip


# ---------------------------------------------------------------------------
# Remasterisation de l'ISO
# ---------------------------------------------------------------------------

def _build_fake_harvester_iso(d, legacy=False):
    """Mini-ISO reproduisant la structure du VRAI ISO Harvester, telle que
    constatée sur harvester-v1.8.2-amd64.iso :

    * un seul grub porteur des entrées de menu, `/boot/grub2/grub.cfg` ;
    * celui de l'EFI ne fait que le charger (`configfile`) ;
    * les entrées se terminent par `${extra_iso_cmdline}`, et le grub source
      `/boot/grub2/harvester.cfg` avant de les définir ;
    * une amorce El Torito UEFI — sans elle, `-boot_image any replay` n'a
      rien à rejouer et l'image produite ne démarrerait pas.

    `legacy=True` produit la variante sans `extra_iso_cmdline`, pour la voie
    de repli qui patche directement les lignes de noyau.
    """
    import subprocess
    src = d / "src"
    (src / "boot/grub2").mkdir(parents=True)
    (src / "EFI/BOOT").mkdir(parents=True)
    (src / "boot/x86_64/loader").mkdir(parents=True)
    tail = "" if legacy else " ${extra_iso_cmdline}"
    source_line = "" if legacy else "source (${root})/boot/grub2/harvester.cfg\n"
    (src / "boot/grub2/grub.cfg").write_text(
        "set default=0\n" + source_line +
        'menuentry "Harvester Installer" {\n'
        "  $linux ($root)/boot/x86_64/loader/linux cdroot "
        "root=live:CDLABEL=COS_LIVE" + tail + "\n}\n")
    if not legacy:
        (src / "boot/grub2/harvester.cfg").write_text("set harvester_version=v1.8.2\n")
    # l'EFI ne fait que charger l'autre : il ne doit PAS être choisi
    (src / "EFI/BOOT/grub.cfg").write_text(
        "set prefix=($root)/boot/grub2\nconfigfile $prefix/grub.cfg\n")
    (src / "boot/x86_64/loader/linux").write_text("k")
    efi = src / "boot/efiboot.img"
    efi.write_bytes(b"\0" * 4096)
    iso = d / ("legacy.iso" if legacy else "src.iso")
    subprocess.run(["xorriso", "-as", "mkisofs", "-V", "COS_LIVE",
                    "-e", "boot/efiboot.img", "-no-emul-boot",
                    "-o", str(iso), str(src)],
                   capture_output=True, check=True)
    return iso


def _iso_file(iso, path, dest):
    import subprocess
    subprocess.run(["xorriso", "-osirrox", "on", "-indev", str(iso),
                    "-extract", path, str(dest)], capture_output=True)
    return dest.read_text() if dest.exists() else ""


@pytest.mark.skipif(not Path("/usr/bin/xorriso").exists(),
                    reason="xorriso absent")
def test_remaster_injects_through_extra_iso_cmdline():
    """Le vrai ISO offre `${extra_iso_cmdline}`, concaténé par toutes les
    entrées de menu : y poser les arguments vaut mieux que de réécrire
    chaque ligne de noyau à coups d'expressions régulières."""
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        src = _build_fake_harvester_iso(d)
        out = d / "out.iso"
        url = "http://10.0.0.1:8091/pxe/config/TOK.yaml"
        r = subprocess.run(
            ["bash", str(ROOT / "bin" / "harvester-iso-remaster.sh"),
             "--src", str(src), "--out", str(out), "--config-url", url],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-1500:]
        assert "extra_iso_cmdline via /boot/grub2/harvester.cfg" in r.stderr

        cfg = _iso_file(out, "/boot/grub2/harvester.cfg", d / "h.cfg")
        assert "set extra_iso_cmdline=" in cfg
        assert "harvester.install.automatic=true" in cfg
        assert url in cfg
        # le grub porteur des entrées n'a pas eu à être touché
        assert "set harvester_version=v1.8.2" in cfg


@pytest.mark.skipif(not Path("/usr/bin/xorriso").exists(),
                    reason="xorriso absent")
def test_remaster_keeps_the_label_and_the_boot_record():
    """Les deux façons silencieuses de produire un ISO mort : perdre le
    label `COS_LIVE` (le noyau monte son rootfs par `CDLABEL=`) et perdre
    l'amorce El Torito. Celle du vrai ISO est CACHÉE — ce n'est pas un
    fichier de l'arborescence — donc un `mkisofs` reconstruit ne peut pas la
    reproduire : d'où la recopie avec `-boot_image any replay`."""
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        src = _build_fake_harvester_iso(d)
        out = d / "out.iso"
        r = subprocess.run(
            ["bash", str(ROOT / "bin" / "harvester-iso-remaster.sh"),
             "--src", str(src), "--out", str(out),
             "--config-url", "http://10.0.0.1:8091/pxe/config/T.yaml"],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-1500:]
        pvd = subprocess.run(["xorriso", "-indev", str(out), "-pvd_info"],
                             capture_output=True, text=True)
        assert "COS_LIVE" in pvd.stdout + pvd.stderr
        elt = subprocess.run(["xorriso", "-indev", str(out),
                              "-report_el_torito", "plain"],
                             capture_output=True, text=True)
        assert "El Torito boot img" in elt.stdout + elt.stderr, (
            "amorce perdue : l'image produite ne démarrerait pas")
        assert (out.parent / (out.name + ".sha256")).read_text().split()[1] == out.name
        assert "-boot_image any replay" in (
            ROOT / "bin" / "harvester-iso-remaster.sh").read_text()


@pytest.mark.skipif(not Path("/usr/bin/xorriso").exists(),
                    reason="xorriso absent")
def test_remaster_falls_back_to_the_kernel_line():
    """Un ISO sans `extra_iso_cmdline` (version plus ancienne) doit encore
    pouvoir être préparé."""
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        src = _build_fake_harvester_iso(d, legacy=True)
        out = d / "out.iso"
        url = "http://10.0.0.1:8091/pxe/config/OLD.yaml"
        r = subprocess.run(
            ["bash", str(ROOT / "bin" / "harvester-iso-remaster.sh"),
             "--src", str(src), "--out", str(out), "--config-url", url],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-1500:]
        cfg = _iso_file(out, "/boot/grub2/grub.cfg", d / "g.cfg")
        assert url in cfg and "harvester.install.automatic=true" in cfg


@pytest.mark.skipif(not Path("/usr/bin/xorriso").exists(),
                    reason="xorriso absent")
def test_remaster_refuses_an_iso_without_install_grub():
    """Un ISO qui n'est pas un installeur Harvester doit être refusé, pas
    produire une image silencieusement inerte."""
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "empty").mkdir()
        (d / "empty/readme.txt").write_text("nothing here")
        iso = d / "e.iso"
        subprocess.run(["xorriso", "-as", "mkisofs", "-V", "COS_LIVE",
                        "-o", str(iso), str(d / "empty")],
                       capture_output=True, check=True)
        r = subprocess.run(
            ["bash", str(ROOT / "bin" / "harvester-iso-remaster.sh"),
             "--src", str(iso), "--out", str(d / "o.iso"),
             "--config-url", "http://x/y.yaml"],
            capture_output=True, text=True)
        assert r.returncode != 0
        assert "iso-patch|error" in r.stderr


# ---------------------------------------------------------------------------
# UI de l'onglet Bare-metal (v1.19.0) — vérifications au niveau source, le DOM
# de cet onglet dépendant d'un vrai BMC pour produire quoi que ce soit.
# ---------------------------------------------------------------------------

BMC_JS = (ROOT / "web" / "static" / "js" / "bmc.js").read_text()


@pytest.mark.skipif(not Path("/usr/bin/xorriso").exists(),
                    reason="xorriso absent")
def test_remaster_works_beside_the_output_not_in_tmp():
    """Vécu : /tmp est un tmpfs sur beaucoup d'hôtes (126 Gio de RAM sur le
    nôtre). Y extraire un ISO Harvester y poserait ~8 Go en mémoire ; sur
    une machine d'exploitation modeste c'est l'OOM. Le travail se fait donc
    par défaut à côté de l'ISO produit."""
    sh = (ROOT / "bin" / "harvester-iso-remaster.sh").read_text()
    assert 'WORK_DIR="$(dirname "$OUT")"' in sh
    assert 'mktemp -d "$WORK_DIR/remaster-XXXXXX"' in sh
    assert "mktemp -d)" not in sh, "un mktemp nu retomberait dans /tmp"
    # et l'appelant pointe explicitement le magasin d'ISO
    assert '"--work-dir", str(_iso_work_dir())' in (WEB / "app.py").read_text()


@pytest.mark.skipif(not Path("/usr/bin/xorriso").exists(),
                    reason="xorriso absent")
def test_remaster_refuses_without_room():
    """Mieux vaut un message clair qu'un xorriso qui meurt à mi-course en
    laissant un ISO tronqué dans le magasin."""
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "src").mkdir()
        (d / "src/f").write_bytes(b"x" * 4096)
        iso = d / "s.iso"
        subprocess.run(["xorriso", "-as", "mkisofs", "-V", "COS_LIVE",
                        "-o", str(iso), str(d / "src")],
                       capture_output=True, check=True)
        r = subprocess.run(
            ["bash", str(ROOT / "bin" / "harvester-iso-remaster.sh"),
             "--src", str(iso), "--out", str(d / "o.iso"),
             "--config-url", "http://x/y.yaml", "--work-dir", "/dev/full"],
            capture_output=True, text=True)
        assert r.returncode != 0


def test_bmc_tab_rerender_targets_the_nav_link_only():
    """Trouvé en testant l'UI : le panneau de contenu porte le même
    `data-subtab="pxe"` que le lien de navigation. Un sélecteur non scopé
    faisait reconstruire l'onglet à CHAQUE clic dedans, effaçant 50 ms plus
    tard le résultat d'une découverte qui prend une minute."""
    html = (ROOT / "web" / "templates" / "index.html").read_text()
    assert 'class="sub-tab-content" data-subtab="pxe"' in html, (
        "le panneau porte bien l'attribut : c'est ce qui rend le piège réel")
    assert "e.target.closest('[data-subtab=\"pxe\"]')" not in BMC_JS
    assert "closest('.tab-child[data-subtab=\"pxe\"]')" in BMC_JS
    # et un re-render non forcé préserve ce qui est déjà affiché
    assert "if (!force && out.querySelector('#bmc-discover-form'))" in BMC_JS


def test_management_interface_is_designated_by_mac():
    """Redfish nomme les deux cartes d'un XL170r « System Ethernet
    Interface » et ignore le nom que Linux leur donnera. Écrire ce nom dans
    la configuration produirait une interface de management introuvable ;
    la MAC, elle, désigne sans ambiguïté."""
    import yaml
    base = {"token": "t", "hostname": "h", "device": "/dev/sda",
            "vip": "10.0.0.1", "method": "dhcp"}
    cfg = yaml.safe_load(wapp._harvester_install_config(
        dict(base, mgmt_interface="D0:67:26:D5:4A:F8")))
    iface = cfg["install"]["management_interface"]["interfaces"][0]
    assert iface == {"hwAddr": "d0:67:26:d5:4a:f8"}, iface

    # un vrai nom d'interface reste accepté tel quel
    cfg = yaml.safe_load(wapp._harvester_install_config(
        dict(base, mgmt_interface="eno1")))
    assert cfg["install"]["management_interface"]["interfaces"][0] == {"name": "eno1"}


def test_install_form_offers_macs_not_redfish_names():
    assert 'value="${esc(n.mac)}"' in BMC_JS


def test_install_form_carries_no_homelab_address():
    """Le formulaire est livré à des clients : aucune adresse du réseau de
    développement ne doit s'y trouver comme valeur par défaut. Les exemples
    utilisent les plages de documentation (RFC 5737)."""
    form = BMC_JS.split("bmc.fs.node", 1)[1].split("</fieldset>", 1)[0]
    assert "172.16." not in form, "adresse du lab en dur dans le formulaire"
    assert "192.0.2." in form, "utiliser 192.0.2.0/24 (RFC 5737) en exemple"


def test_dhcp_hides_and_unrequires_the_static_fields():
    """Un champ `required` caché bloque l'envoi sans expliquer pourquoi."""
    assert "l.querySelector('input').required = stat;" in BMC_JS
    assert "l.hidden = !stat;" in BMC_JS


def test_translation_helper_forwards_interpolation_params():
    """Constaté en lançant une installation : la confirmation affichait
    « Installer Harvester sur {host} ? » — le helper local avalait le second
    argument au lieu de le passer à i18n.t()."""
    assert "const tr = (k, params) => (window.i18n ? i18n.t(k, params) : k);" in BMC_JS
    for key in ("bmc.confirmInstall", "bmc.confirmPower", "bmc.iso.confirmDelete"):
        assert f"tr('{key}', " in BMC_JS, f"{key} doit recevoir ses paramètres"


def test_failed_install_leaves_no_secret_on_disk():
    """La configuration générée porte le token du cluster et le mot de passe
    OS. Le premier essai réel a échoué à la remasterisation en la laissant
    derrière lui : tout chemin d'échec doit maintenant l'effacer."""
    src = (WEB / "app.py").read_text()
    runner = src.split("def _baremetal_install_runner", 1)[1].split("\ndef ", 1)[0]
    assert "scratch = []" in runner
    assert "scratch.append(cfg_path)" in runner
    fail = runner.split("def fail(", 1)[1].split("run.close()", 1)[0]
    assert "for p in scratch:" in fail, "fail() doit effacer les artefacts"


def test_ephemeral_port_is_not_confused_with_the_default():
    """`port=0` demande un port libre. Un `or` le confondait avec « non
    précisé » et renvoyait sur le port fixe, déjà pris."""
    p1 = px.start(port=0, bind="127.0.0.1")
    try:
        assert p1 not in (0, 8091)
    finally:
        px.stop()


def test_extra_kernel_arguments_are_validated_and_forwarded(client, monkeypatch):
    """Les arguments supplémentaires finissent sur une ligne de commande
    grub : guillemets et retours à la ligne y ouvriraient une injection.
    Ils sont indispensables pour observer une installation (`console=`),
    donc on les accepte, mais filtrés."""
    seen = {}
    monkeypatch.setattr(wapp, "track_action",
                        lambda *a, **k: seen.update(opts=a[3]) or "id1")
    base = {"bmc_host": "1.2.3.4", "bmc_user": "u", "bmc_password": "p",
            "iso": "x.iso", "hostname": "h", "device": "/dev/sda",
            "mgmt_interface": "aa:bb:cc:dd:ee:ff", "vip": "10.0.0.1",
            "token": "t", "method": "dhcp"}
    r = client.post("/api/baremetal/install",
                    json=dict(base, extra_args=' console=ttyS1,115200  x=1 '))
    assert r.status_code == 202
    assert seen["opts"]["extra_args"] == "console=ttyS1,115200 x=1"

    r = client.post("/api/baremetal/install",
                    json=dict(base, extra_args='a" ; reboot #'))
    assert r.status_code == 400

    src = (WEB / "app.py").read_text()
    assert '"--extra-args", opts["extra_args"]' in src


def test_generated_artifacts_stay_out_of_the_iso_store(tmp_path, monkeypatch):
    """Vécu : un run interrompu par un redémarrage du serveur a laissé son
    ISO remasterisé de 7,7 Go dans le magasin, où l'installation suivante
    l'a resélectionné à la place de l'image officielle."""
    monkeypatch.setattr(wapp, "ISO_DIR", tmp_path)
    work = wapp._iso_work_dir()
    assert work.parent == tmp_path and work.name == "work"
    assert oct(work.stat().st_mode)[-3:] == "700"

    (tmp_path / "harvester-v1.8.2-amd64.iso").write_bytes(b"x")
    (work / "install-abc.iso").write_bytes(b"y")
    listed = [p.name for p in wapp._iso_dir().glob("*.iso")]
    assert listed == ["harvester-v1.8.2-amd64.iso"]

    src = (WEB / "app.py").read_text()
    for line in ('cfg_path = _iso_work_dir()', 'out_iso = _iso_work_dir()'):
        assert line in src


def test_work_dir_sweeps_stale_artifacts(tmp_path, monkeypatch):
    import os as _os
    monkeypatch.setattr(wapp, "ISO_DIR", tmp_path)
    work = wapp._iso_work_dir()
    old, fresh = work / "install-old.iso", work / "install-new.iso"
    old.write_bytes(b"x"); fresh.write_bytes(b"y")
    _os.utime(old, (time.time() - 200000, time.time() - 200000))
    wapp._iso_work_dir()
    assert not old.exists() and fresh.exists()


@pytest.mark.skipif(not Path("/usr/bin/xorriso").exists(),
                    reason="xorriso absent")
def test_remaster_brings_the_network_up_in_the_initrd():
    """LE piège de l'installation par média virtuel, payé en direct sur
    node3 : l'installeur Harvester va chercher sa configuration à
    `config_url` AVANT de configurer le réseau (le fetch précède
    applyNetworks dans install_panels.go). En PXE le réseau est déjà monté
    par le paramètre `ip=` du noyau ; depuis un média virtuel, personne ne
    l'a monté et l'installeur reste muet, sans jamais émettre une requête.
    """
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        src = _build_fake_harvester_iso(d)
        out = d / "out.iso"
        r = subprocess.run(
            ["bash", str(ROOT / "bin" / "harvester-iso-remaster.sh"),
             "--src", str(src), "--out", str(out),
             "--config-url", "http://10.0.0.1:8091/pxe/config/T.yaml"],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-1500:]
        cfg = _iso_file(out, "/boot/grub2/harvester.cfg", d / "h.cfg")
        assert "ip=dhcp" in cfg, "sans réseau, l'installation n'a jamais lieu"
        assert "rd.neednet=1" in cfg


def test_automatic_install_config_carries_the_iso_url():
    """Constaté sur node3, dans le journal de l'installeur : « iso_url is
    required in automatic installation ». Le validateur l'exige en mode
    automatique même quand l'image est déjà montée en média virtuel."""
    import yaml
    cfg = yaml.safe_load(wapp._harvester_install_config({
        "token": "t", "hostname": "h", "device": "/dev/sda",
        "vip": "10.0.0.1", "method": "dhcp", "mgmt_interface": "eno1",
        "iso_url": "http://10.0.0.9:8091/pxe/iso/TOK.iso"}))
    assert cfg["install"]["iso_url"] == "http://10.0.0.9:8091/pxe/iso/TOK.iso"


def test_iso_token_is_issued_before_the_config_is_written():
    """La configuration doit porter l'URL de l'ISO : son jeton est donc émis
    avant l'écriture, alors que le fichier n'existe pas encore (il sera là
    bien avant la première requête du BMC)."""
    src = (WEB / "app.py").read_text()
    runner = src.split("def _baremetal_install_runner", 1)[1].split("\ndef ", 1)[0]
    assert runner.index('iso_token = pxe_server.issue(out_iso') < \
        runner.index("cfg_yaml = _harvester_install_config"), (
            "l'URL de l'ISO doit exister avant que la configuration soit rendue")
    assert runner.count("pxe_server.issue(out_iso") == 1, "un seul jeton d'ISO"
    assert 'dict(opts, iso_url=iso_url)' in runner
