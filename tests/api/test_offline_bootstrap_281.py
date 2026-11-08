"""2.8.1 (décision 241) : cluster enfant sans Internet, cloud-init sans
mise à jour de paquets.

Vu sur harvlab2 (R11 de 2.8.0) : CAPHV v0.10.1 fusionne dans le cloud-init
de chaque machine une base à lui (`package_update: true`, `packages:
[qemu-guest-agent, iptables]`, internal/controller/harvestermachine_controller.go,
buildVMTemplate) ; sans Internet, zypper attend chaque dépôt de l'image
(environ 5 minutes chacun) avant les commandes de RKE2. En mode sans
Internet, la console pose dans les deux gabarits RKE2
`agentConfig.additionalUserData.config` (CAPRKE2, ajouté à la fin du
cloud-config qu'il rend) :

- `package_update`, `package_upgrade`, `package_reboot_if_required` à
  false : CAPHV fusionne les clés simples « la dernière gagne », et les
  données de CAPRKE2 passent en dernier (util/cloudinit.go,
  MergeCloudInitData) ;
- un `bootcmd` qui met de côté les dépôts et services zypper une fois par
  instance (`cloud-init-per instance`) : la liste `packages` de CAPHV est
  une section concaténée que la console ne peut pas retirer, et l'install
  de zypper rafraîchit les dépôts ; sans dépôt, elle échoue tout de suite ;
- en tête des commandes sans Internet, la remise en place des dépôts, avant
  la récupération de RKE2 (une récupération qui échoue les laisse donc en
  place).

Mutations jouées (rapport 2.8.1) : « package_update laissé à true »,
« remise en place après la récupération », « bootcmd sans cloud-init-per »,
« constat du disque à 20 Gio exclu », « config posée aussi en ligne ».
"""

import copy
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import capi_cluster as cc  # noqa: E402
import rke2_offline as ro  # noqa: E402

DIGESTS = {f: "sha256:" + hashlib.sha256(f.encode()).hexdigest() for f in ro.FILES}

# Base de CAPHV v0.10.1 (harvestermachine_controller.go, cloudInitBase), et
# les sections que MergeCloudInitData concatène (util/cloudinit.go)
CAPHV_BASE = """package_update: true
packages:
  - qemu-guest-agent
  - iptables
runcmd:
  - - systemctl
    - enable
    - --now
    - qemu-guest-agent.service"""
CAPHV_LIST_SECTIONS = ("packages", "runcmd", "ssh_authorized_keys", "groups", "users", "write_files", "bootcmd")
# CAPRKE2 v0.25.2 (bootstrap/internal/cloudinit/cloudinit.go) retire ces clés
# de additionalUserData.config
CAPRKE2_IGNORED = ("write_files", "ntp", "runcmd")


def caphv_merge(*docs):
    """Copie de MergeCloudInitData (CAPHV v0.10.1) : sections listes
    concaténées, autres clés écrasées par la dernière."""
    out = {}
    for text in docs:
        for k, v in (yaml.safe_load(text) or {}).items():
            if k in CAPHV_LIST_SECTIONS:
                out[k] = list(out.get(k) or []) + list(v)
            else:
                out[k] = v
    return out


def templates():
    return [{"kind": "Cluster", "metadata": {"name": "web", "namespace": "web"}, "spec": {"topology": {}}},
            {"kind": "RKE2ControlPlaneTemplate", "metadata": {"name": "cp"},
             "spec": {"template": {"spec": {"serverConfig": {"cni": "calico"}, "preRKE2Commands": ["echo own"]}}}},
            {"kind": "RKE2ConfigTemplate", "metadata": {"name": "w"}, "spec": {"template": {"spec": {}}}}]


def inner_of(out, kind):
    return next(d for d in out if d["kind"] == kind)["spec"]["template"]["spec"]


def render(mode):
    if mode == "online":
        return cc.postprocess(templates(), cc.normalize({"name": "web"}))
    if mode == "image":
        return cc.postprocess(templates(), cc.normalize({"name": "web", "offline": True, "offline_artifacts": "image"}),
                              offline={})
    patch = ro.template_patch("172.16.2.76", DIGESTS, "web-rke2-fetch")
    return cc.postprocess(templates(), cc.normalize({"name": "web", "offline": True}), offline=patch)


@pytest.mark.parametrize("mode,user_data", [("fetch", True), ("image", True), ("online", False)])
@pytest.mark.parametrize("kind", cc.RKE2_TEMPLATE_KINDS)
def test_offline_templates_carry_the_cloud_init_without_package_update(mode, user_data, kind):
    """Mutation « config posée aussi en ligne »."""
    agent = inner_of(render(mode), kind).get("agentConfig") or {}
    if not user_data:
        assert "additionalUserData" not in agent
        return
    assert agent["additionalUserData"] == {"config": ro.user_data()}
    cfg = yaml.safe_load(agent["additionalUserData"]["config"])
    assert cfg["package_update"] is False and cfg["package_upgrade"] is False
    assert cfg["package_reboot_if_required"] is False


def test_caphv_merge_turns_package_update_off():
    """Hypothèse sur la fusion de CAPHV (à vérifier au banc) : les données
    de CAPRKE2 passent en dernier, leur `package_update` gagne."""
    caprke2 = "## template: jinja\n#cloud-config\nruncmd:\n  - \"echo rke2\"\n" + ro.user_data()
    merged = caphv_merge(CAPHV_BASE, "\nssh_authorized_keys:\n  - ssh-ed25519 AAAA\n", caprke2)
    assert merged["package_update"] is False
    assert merged["packages"] == ["qemu-guest-agent", "iptables"]       # ne se retire pas : d'où le bootcmd
    assert merged["bootcmd"] == yaml.safe_load(ro.user_data())["bootcmd"]
    assert merged["runcmd"][-1] == "echo rke2"


def test_the_config_survives_caprke2_and_has_no_template_marker():
    """CAPRKE2 retire write_files, ntp et runcmd de la config, et son en-tête
    `## template: jinja` ferait lire `{{`, `{%` ou `{#` par Jinja."""
    cfg = yaml.safe_load(ro.user_data())
    assert not set(cfg) & set(CAPRKE2_IGNORED)
    text = ro.user_data() + "\n".join(ro.pre_commands("r", ro.REPO, DIGESTS)) + ro.repos_back()
    assert not re.search(r"\{\{|\{%|\{#", text)


def test_bootcmd_runs_once_per_instance():
    """Mutation « bootcmd sans cloud-init-per » : bootcmd tourne à chaque
    démarrage ; sans garde, un redémarrage remettrait les dépôts de côté
    après leur remise en place (runcmd, lui, ne tourne qu'une fois)."""
    [entry] = yaml.safe_load(ro.user_data())["bootcmd"]
    assert entry[:3] == ["cloud-init-per", "instance", "harvester-ops-repos-aside"]
    assert entry[3:5] == ["sh", "-c"] and entry[5] == ro.repos_aside()


def test_repositories_come_back_before_the_fetch_and_after_own_commands():
    """Mutation « remise en place après la récupération »."""
    cmds = inner_of(render("fetch"), "RKE2ControlPlaneTemplate")["preRKE2Commands"]
    assert cmds[0] == "echo own" and cmds[1] == ro.repos_back()
    first_fetch = next(i for i, c in enumerate(cmds) if c.startswith("curl "))
    assert cmds.index(ro.repos_back()) < first_fetch and cmds.count(ro.repos_back()) == 1
    worker = inner_of(render("image"), "RKE2ConfigTemplate")
    assert worker["preRKE2Commands"] == [ro.repos_back()]
    assert worker["agentConfig"]["airGapped"] is True


def test_renewal_keeps_the_offline_cloud_init():
    out = render("fetch")
    again = cc.renew_templates(out, cc.normalize({"name": "web", "offline": True}), ["registries"],
                               registries={"files": []})
    for kind in cc.RKE2_TEMPLATE_KINDS:
        assert inner_of(again, kind)["agentConfig"]["additionalUserData"] == {"config": ro.user_data()}


# --- les deux lignes jouées par un vrai shell --------------------------------------

def zypp(tmp_path):
    base = tmp_path / "etc" / "zypp"
    (base / "repos.d").mkdir(parents=True)
    (base / "repos.d" / "repo-oss.repo").write_text("[repo-oss]\nenabled=1\n")
    (base / "services.d").mkdir()
    (base / "services.d" / "SLES.service").write_text("[SLES]\n")
    return base, (str(base / "repos.d"), str(base / "services.d"))


def sh(line):
    return subprocess.run(["sh", "-c", line], capture_output=True, text=True)


def test_repositories_are_set_aside_then_restored(tmp_path):
    base, dirs = zypp(tmp_path)
    assert sh(ro.repos_aside(dirs)).returncode == 0
    assert sorted(p.name for p in (base / "repos.d").iterdir()) == []
    assert (base / "repos.d.harvester-ops-offline" / "repo-oss.repo").exists()
    assert (base / "services.d.harvester-ops-offline" / "SLES.service").exists()
    assert sh(ro.repos_back(dirs)).returncode == 0
    assert (base / "repos.d" / "repo-oss.repo").read_text().startswith("[repo-oss]")
    assert (base / "services.d" / "SLES.service").exists()
    assert not list(base.glob("*.harvester-ops-offline"))


@pytest.mark.parametrize("case", ["aside twice", "back without aside", "placeholder used", "no zypper"])
def test_repositories_edge_cases(tmp_path, case):
    base, dirs = zypp(tmp_path)
    if case == "aside twice":
        sh(ro.repos_aside(dirs))
        assert sh(ro.repos_aside(dirs)).returncode == 0                 # la copie de côté n'est pas écrasée
        assert sorted(p.name for p in (base / "repos.d.harvester-ops-offline").iterdir()) == ["repo-oss.repo"]
        sh(ro.repos_back(dirs))
        assert sorted(p.name for p in (base / "repos.d").iterdir()) == ["repo-oss.repo"]
    elif case == "back without aside":
        assert sh(ro.repos_back(dirs)).returncode == 0
        assert (base / "repos.d" / "repo-oss.repo").exists()
    elif case == "placeholder used":
        sh(ro.repos_aside(dirs))
        (base / "repos.d" / "local.repo").write_text("[local]\n")       # posé entre-temps : rien n'est perdu
        sh(ro.repos_back(dirs))
        assert (base / "repos.d" / "local.repo").exists()
        assert (base / "repos.d.harvester-ops-offline" / "repo-oss.repo").exists()
    else:
        other = (str(tmp_path / "none" / "repos.d"),)
        assert sh(ro.repos_aside(other)).returncode == 0 and sh(ro.repos_back(other)).returncode == 0
        assert not (tmp_path / "none").exists()


# --- constat du disque -------------------------------------------------------------

def good(**over):
    raw = {"name": "web", "k8s_version": "v1.31.14", "image": "default/sles15-sp7.qcow2",
           "ssh_keypair": "default/capi-ssh-key", "network": "default/production",
           "gateway": "172.16.0.1", "subnet_mask": "255.255.0.0", "ip_pool": "capi-vm-pool", "dns": "172.16.3.6"}
    raw.update(over)
    return cc.normalize(raw)


@pytest.mark.parametrize("over,want", [
    ({"offline": True, "disk_size": "10Gi"}, True),
    ({"offline": True, "disk_size": "19Gi"}, True),
    ({"offline": True, "disk_size": "20479Mi"}, True),
    ({"offline": True, "disk_size": "20Gi"}, False),
    ({"offline": True, "disk_size": "20480Mi"}, False),
    ({"offline": True}, False),                                          # 40Gi par défaut
    ({"offline": True, "offline_artifacts": "image", "disk_size": "10Gi"}, True),
    ({"disk_size": "10Gi"}, False),
])
def test_offline_disk_under_20_gib_is_a_warning(over, want):
    """Mutation « constat du disque à 20 Gio exclu »."""
    found = [f for f in cc.check(good(**over), {}) if f["code"] == "offline-disk-small"]
    assert bool(found) is want
    if want:
        assert found[0]["level"] == "warn" and found[0]["facts"] == {"disk": over["disk_size"], "advised": "20Gi"}


def test_mutation_threshold_off_by_one_is_seen(monkeypatch):
    monkeypatch.setattr(cc, "OFFLINE_DISK_MIN", "19Gi")
    assert not [f for f in cc.check(good(offline=True, disk_size="19Gi"), {}) if f["code"] == "offline-disk-small"]


def test_templates_fixture_is_not_modified():
    t = templates()
    before = copy.deepcopy(t)
    cc.postprocess(t, cc.normalize({"name": "web", "offline": True}), offline={})
    assert t == before and json.dumps(t)
