"""2.8.1, suites de la relecture (I-1, I-2, M-2, M-3, M-6).

- I-1 : en mode sans Internet `skip`, la console sait qu'elle coupe
  l'installation des paquets de CAPHV ; l'image doit donc déjà porter
  `iptables` et `qemu-guest-agent`. Constat non bloquant
  `offline-node-packages` sur chaque création et chaque contrôle de plan.
  `image-sles-repos` n'est plus rendu dans ce mode (il conseillerait une
  image Leap, qui n'aurait pas plus ces paquets).
- I-2 : option `offline_packages: skip|keep` (défaut `skip`). `keep` laisse
  les dépôts de l'image en place (miroir du réseau local, RMT ou MLM) ;
  `package_update` et `package_upgrade` restent à false dans les deux modes.
- M-2 : `repos_back` écrit une ligne sur stderr (dépôts remis, rien de côté,
  copie laissée de côté).
- M-3 : `additionalUserData` fusionné, jamais écrasé.
- M-6 : la version de CAPHV (et de CAPRKE2) livrée est épinglée : la fusion
  de cloud-init est à relire quand elle change.

Mutations jouées (rapport 2.8.1, « Review fixes ») : constat aussi en
`keep`, constat absent en `skip`, `image-sles-repos` gardé en `skip`, bootcmd
posé en `keep`, remise en place posée en `keep`, valeur inconnue admise,
`additionalUserData` écrasé, message de remise en place retiré.
"""

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import capi_cluster as cc  # noqa: E402
import deploy_plans as dp  # noqa: E402
import rke2_offline as ro  # noqa: E402

DIGESTS = {f: "sha256:" + hashlib.sha256(f.encode()).hexdigest() for f in ro.FILES}
CODE = "offline-node-packages"
IMAGE = "default/sles15-sp7.qcow2"


def good(**over):
    raw = {"name": "web", "k8s_version": "v1.31.14", "image": IMAGE,
           "ssh_keypair": "default/capi-ssh-key", "network": "default/production",
           "gateway": "172.16.0.1", "subnet_mask": "255.255.0.0", "ip_pool": "capi-vm-pool", "dns": "172.16.3.6"}
    raw.update(over)
    return cc.normalize(raw)


SLES = {"images": {IMAGE: {"ready": True, "iso": False, "os": "sles"}}}
LEAP = {"images": {IMAGE: {"ready": True, "iso": False, "os": "opensuse"}}}


# --- I-1 / I-2 : constats ------------------------------------------------------------

@pytest.mark.parametrize("over,facts,want_packages,want_sles", [
    ({"offline": True}, SLES, True, False),                                       # skip par défaut
    ({"offline": True, "offline_packages": "skip"}, SLES, True, False),
    ({"offline": True, "offline_artifacts": "image"}, SLES, True, False),
    ({"offline": True, "offline_packages": "keep"}, SLES, False, True),          # miroir local : avis SLES gardé
    ({"offline": True}, LEAP, True, False),
    ({"offline": True, "offline_packages": "keep"}, LEAP, False, False),
    ({}, SLES, False, True),                                                      # en ligne : inchangé
    ({}, LEAP, False, False),
    ({"offline": True}, {}, True, False),                                         # image non lue : constat quand même
])
def test_offline_node_packages_warning(over, facts, want_packages, want_sles):
    """Mutations « constat aussi en keep », « constat absent en skip »,
    « image-sles-repos gardé en skip »."""
    found = cc.check(good(**over), facts)
    pk = [f for f in found if f["code"] == CODE]
    assert bool(pk) is want_packages
    if want_packages:
        assert pk == [{"code": CODE, "level": "warn",
                       "facts": {"packages": "iptables, qemu-guest-agent", "image": IMAGE}}]
    assert bool([f for f in found if f["code"] == "image-sles-repos"]) is want_sles
    assert not cc.blocking(pk)


# --- I-2 : option offline_packages ---------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    ({"offline": True}, "skip"),
    ({"offline": True, "offline_packages": "keep"}, "keep"),
    ({"offline": True, "offline_packages": ""}, "skip"),
    ({"offline_packages": "keep"}, None),                                         # en ligne : retirée
])
def test_offline_packages_default_and_online(raw, want):
    spec = good(**raw)
    assert spec.get("offline_packages") == want
    assert not [e for e in cc.validate(spec) if e[0] == "offline_packages"]


@pytest.mark.parametrize("value", ["none", "KEEP", "true", 1])
def test_offline_packages_unknown_value_is_refused(value):
    """Mutation « valeur inconnue admise »."""
    errs = cc.validate(good(offline=True, offline_packages=value))
    assert ("offline_packages", "skip or keep") in errs
    assert cc.blocking([f for f in cc.check(good(offline=True, offline_packages=value), {})
                        if f["code"] == "invalid" and f["facts"]["option"] == "offline_packages"])


@pytest.mark.parametrize("value,ok", [("keep", True), ("skip", True), ("never", False)])
def test_cluster_template_takes_offline_packages(value, ok):
    doc = {"spec": {"k8s_version": "v1.34.11", "offline": True, "offline_packages": value}}
    if ok:
        assert dp.check_capi_template(doc)["spec"]["offline_packages"] == value
        spec = dp.render_capi_template(dict(dp.check_capi_template(doc), name="m"), {"cluster": "h"}, "web")
        assert spec["offline_packages"] == value
    else:
        with pytest.raises(dp.ProfileError) as e:
            dp.check_capi_template(doc)
        assert any(w == "spec.offline_packages" and "skip or keep" in r for w, r in e.value.errors)


def templates():
    return [{"kind": "Cluster", "metadata": {"name": "web", "namespace": "web"}, "spec": {"topology": {}}},
            {"kind": "RKE2ControlPlaneTemplate", "metadata": {"name": "cp"},
             "spec": {"template": {"spec": {"preRKE2Commands": ["echo own"]}}}},
            {"kind": "RKE2ConfigTemplate", "metadata": {"name": "w"}, "spec": {"template": {"spec": {}}}}]


def inner_of(out, kind):
    return next(d for d in out if d["kind"] == kind)["spec"]["template"]["spec"]


def render(packages, artifacts="fetch", docs=None):
    spec = cc.normalize({"name": "web", "offline": True, "offline_artifacts": artifacts,
                         "offline_packages": packages})
    patch = ro.template_patch("172.16.2.76", DIGESTS, "web-rke2-fetch") if artifacts == "fetch" else {}
    return cc.postprocess(docs or templates(), spec, offline=patch)


@pytest.mark.parametrize("packages,bootcmd,restore", [("skip", True, True), ("keep", False, False)])
@pytest.mark.parametrize("artifacts", ["fetch", "image"])
@pytest.mark.parametrize("kind", cc.RKE2_TEMPLATE_KINDS)
def test_keep_leaves_repositories_and_install_alone(packages, bootcmd, restore, artifacts, kind):
    """Mutations « bootcmd posé en keep », « remise en place posée en keep »."""
    inner = inner_of(render(packages, artifacts), kind)
    cfg = yaml.safe_load(inner["agentConfig"]["additionalUserData"]["config"])
    assert cfg["package_update"] is False and cfg["package_upgrade"] is False      # les deux modes
    assert ("bootcmd" in cfg) is bootcmd
    assert inner["agentConfig"]["additionalUserData"]["config"] == ro.user_data(packages)
    cmds = inner.get("preRKE2Commands") or []
    assert (ro.repos_back() in cmds) is restore
    if artifacts == "fetch":
        assert any(c.startswith("curl ") for c in cmds)
    assert inner["agentConfig"]["airGapped"] is True


def test_user_data_rejects_an_unknown_mode():
    with pytest.raises(ValueError):
        ro.user_data("maybe")


# --- M-3 : additionalUserData fusionné ----------------------------------------------

OURS = object()                       # la config de la console, rendue au moment du test


def with_user_data(aud):
    docs = templates()
    for d in docs[1:]:
        d["spec"]["template"]["spec"]["agentConfig"] = {"additionalUserData": dict(aud)}
    return docs


@pytest.mark.parametrize("existing,want", [
    ({"strict": True}, {"strict": True, "config": OURS}),
    ({"data": {"a": "b"}}, {"data": {"a": "b"}, "config": OURS}),
    ({"config": OURS}, {"config": OURS}),                                         # déjà posé : idempotent
])
def test_additional_user_data_is_merged(existing, want):
    """Mutation « additionalUserData écrasé »."""
    ours = ro.user_data("skip")
    existing = {k: ours if v is OURS else v for k, v in existing.items()}
    want = {k: ours if v is OURS else v for k, v in want.items()}
    out = render("skip", docs=with_user_data(existing))
    for kind in cc.RKE2_TEMPLATE_KINDS:
        assert inner_of(out, kind)["agentConfig"]["additionalUserData"] == want


def test_another_additional_config_is_refused_not_overwritten():
    with pytest.raises(ValueError, match="additionalUserData.config"):
        render("skip", docs=with_user_data({"config": "ntp:\n  enabled: true\n"}))


# --- M-2 : remise en place visible dans cloud-init-output.log ------------------------

def zypp(tmp_path):
    base = tmp_path / "etc" / "zypp"
    (base / "repos.d").mkdir(parents=True)
    (base / "repos.d" / "repo-oss.repo").write_text("[repo-oss]\n")
    (base / "services.d").mkdir()
    return base, (str(base / "repos.d"), str(base / "services.d"))


def sh(line):
    return subprocess.run(["sh", "-c", line], capture_output=True, text=True)


@pytest.mark.parametrize("case,lines", [
    ("restored", ["harvester-ops: zypper repositories restored in {d}"] * 2),
    ("nothing aside", ["harvester-ops: nothing set aside for {d}"] * 2),
    ("placeholder used", ["harvester-ops: {d} is not empty, repositories left in {d}.harvester-ops-offline",
                          "harvester-ops: zypper repositories restored in {d}"]),
])
def test_repos_back_says_what_it_did_on_stderr(tmp_path, case, lines):
    """Mutation « message de remise en place retiré »."""
    base, dirs = zypp(tmp_path)
    if case != "nothing aside":
        sh(ro.repos_aside(dirs))
    if case == "placeholder used":
        (base / "repos.d" / "local.repo").write_text("[local]\n")
    r = sh(ro.repos_back(dirs))
    assert r.returncode == 0 and r.stdout == ""
    assert r.stderr.splitlines() == [line.format(d=d) for line, d in zip(lines, dirs)]


# --- M-6 : versions de CAPHV et de CAPRKE2 épinglées --------------------------------

def test_bundled_caphv_and_caprke2_are_the_versions_the_merge_was_read_in():
    """La coupure des paquets repose sur MergeCloudInitData et cloudInitBase
    de CAPHV v0.10.1 (util/cloudinit.go, harvestermachine_controller.go) et
    sur additionalUserData de CAPRKE2 v0.25.2 (cleanupAdditionalCloudInit).
    Une autre version livrée : relire ces deux sources, rejouer le banc R11
    (secret `<machine>-cloud-init`), puis mettre ce test à jour."""
    comps = yaml.safe_load((ROOT / "scripts" / "capi-components.yaml").read_text())
    turtles = {(b["turtles"]["type"], b["turtles"]["name"]): b["version"]
               for b in comps.values() if isinstance(b, dict) and b.get("turtles")}
    msg = "bundled {} changed: re-check MergeCloudInitData and cloudInitBase (CAPHV), additionalUserData (CAPRKE2)"
    assert turtles[("infrastructure", "harvester")] == "v0.10.1", msg.format("CAPHV")
    assert turtles[("bootstrap", "rke2")] == "v0.25.2", msg.format("CAPRKE2 bootstrap")
    assert turtles[("controlPlane", "rke2")] == "v0.25.2", msg.format("CAPRKE2 control plane")
    prov = (ROOT / "bin" / "caphv-generate.PROVENANCE").read_text()
    assert "5373a21672008ad15240d3e3bfb6660d9331e935" in prov and "v0.10.1" in prov, msg.format("caphv-generate")


# --- I-2 : ligne de commande (demande lue par --spec) --------------------------------

def test_cli_spec_file_carries_offline_packages(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("harvester_capi_281r", ROOT / "bin" / "harvester-capi.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    f = tmp_path / "spec.json"
    f.write_text('{"name": "web", "offline": true, "offline_packages": "keep"}')
    assert cli.spec_from(str(f))["offline_packages"] == "keep"
    f.write_text('{"name": "web", "offline": true, "offline_packages": "maybe"}')
    assert ("offline_packages", "skip or keep") in cc.validate(cli.spec_from(str(f)))
