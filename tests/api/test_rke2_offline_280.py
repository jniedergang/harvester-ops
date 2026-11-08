"""2.8.0 (tâche 8, point 237) : cluster enfant amorcé entièrement sans
Internet (rke2_offline), rendu dans les gabarits (capi_cluster) et
configuration lue par harvester-capi (`offline_config`, constats).

Mutations jouées (rapport des tâches 5 à 8) : « mot de passe dans
preRKE2Commands », « install.sh exécuté avant la vérification », « repli
vers Internet admis en mode sans Internet ».
"""

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import capi_cluster as cc  # noqa: E402
import registries as rg  # noqa: E402
import rke2_offline as ro  # noqa: E402

V = "v1.34.11+rke2r1"
PW = "PULL-PW-OFFLINE-2718"
DIGESTS = {f: "sha256:" + hashlib.sha256(f.encode()).hexdigest() for f in ro.FILES}


def item(**over):
    it = {"name": "rke2", "kind": "rke2", "version": V, "files": ro.files_of(V),
          "images": ["docker.io/rancher/rke2-runtime:v1.34.11-rke2r1"]}
    it.update(over)
    return it


@pytest.mark.parametrize("it,needle", [
    (item(), None),
    (item(version="v1.34.11"), "exact RKE2 version"),
    (item(version="1.34.11+rke2r1"), "exact RKE2 version"),
    (item(files=ro.files_of(V)[:2]), "these only"),
    (item(files=ro.files_of(V) + ["rke2/x/other"]), "these only"),
    (item(files=ro.files_of("v1.33.5+rke2r1")), "these only"),
    (item(images=[]), "one at least"),
])
def test_check_item(it, needle):
    errs = ro.check_item(it)
    assert (errs == []) if needle is None else any(needle in e for e in errs)


LISTING = """docker.io/rancher/rke2-runtime:v1.34.11-rke2r1
docker.io/rancher/hardened-calico:v3.30.3-build20250827
docker.io/rancher/hardened-cilium:v1.18.1-build20250818
docker.io/rancher/hardened-flannel:v0.27.2-build20250805
docker.io/rancher/mirrored-calico-operator:v1.38.6
docker.io/rancher/hardened-coredns:v1.12.3-build20250812
docker.io/rancher/rke2-runtime:v1.34.11-rke2r1
"""


def test_images_for_keep_the_chosen_cni_only():
    assert ro.images_for(LISTING, "calico") == [
        "docker.io/rancher/rke2-runtime:v1.34.11-rke2r1", "docker.io/rancher/hardened-calico:v3.30.3-build20250827",
        "docker.io/rancher/mirrored-calico-operator:v1.38.6", "docker.io/rancher/hardened-coredns:v1.12.3-build20250812"]
    cil = ro.images_for(LISTING, "cilium")
    assert "docker.io/rancher/hardened-cilium:v1.18.1-build20250818" in cil and not [x for x in cil if "calico" in x]
    canal = ro.images_for(LISTING, "canal")
    assert any("flannel" in x for x in canal) and any("calico" in x for x in canal)
    with pytest.raises(ValueError):
        ro.images_for(LISTING, "weave")


def test_pre_commands_carry_no_credential_and_verify_before_install():
    """Mutations « mot de passe dans preRKE2Commands », « install.sh exécuté
    avant la vérification »."""
    cmds = ro.pre_commands("172.16.2.76", ro.REPO, DIGESTS)
    text = "\n".join(cmds)
    assert PW not in text and "password" not in text and "login" not in text
    fetches = [c for c in cmds if c.startswith("curl ")]
    assert len(fetches) == 3 and all("--fail" in c and f"--netrc-file {ro.NETRC}" in c and
                                     f"--cacert {ro.CA_FILE}" in c for c in fetches)
    assert all(f"https://172.16.2.76/v2/{ro.REPO}/blobs/{DIGESTS[f]}" in text for f in ro.FILES)
    i_check = next(i for i, c in enumerate(cmds) if "sha256sum -c" in c)
    i_install = next(i for i, c in enumerate(cmds) if ro.INSTALL in c and c.startswith("install "))
    assert i_check < i_install == len(cmds) - 1
    # le script n'est jamais téléchargé à sa place ni lancé par ces commandes
    assert not [c for c in cmds[:i_install] if f"-o {ro.INSTALL}" in c]
    assert not [c for c in cmds if c.startswith("sh ") or f"bash {ro.INSTALL}" in c or c.strip() == ro.INSTALL]
    for f in ro.FILES:
        assert DIGESTS[f].split(":")[1] in cmds[i_check]
    assert "rm -rf" in cmds[i_check] and "exit 1" in cmds[i_check]      # empreinte fausse : tout part


@pytest.mark.skipif(not __import__("shutil").which("sha256sum"), reason="sha256sum needed")
def test_the_verification_really_refuses_a_wrong_file(tmp_path):
    """Les commandes jouées par un vrai shell : un fichier altéré efface tout
    et `/opt/install.sh` n'apparaît pas."""
    art = tmp_path / "art"
    blobs = {f: f"content of {f}\n".encode() for f in ro.FILES}
    digests = {f: "sha256:" + hashlib.sha256(b).hexdigest() for f, b in blobs.items()}
    sums = "".join(f"{hashlib.sha256(b).hexdigest()}  {f}\n" for f, b in blobs.items() if f != "sha256sum-amd64.txt")
    blobs["sha256sum-amd64.txt"] = sums.encode()
    digests["sha256sum-amd64.txt"] = "sha256:" + hashlib.sha256(blobs["sha256sum-amd64.txt"]).hexdigest()
    install = tmp_path / "install.sh"

    def run(alter):
        cmds = ro.pre_commands("r", ro.REPO, digests)
        script = []
        for c in cmds:
            c = c.replace(ro.ARTIFACTS_DIR, str(art)).replace(ro.INSTALL, str(install))
            if c.startswith("curl "):
                f = next(f for f in ro.FILES if digests[f] in c)
                dest = c.split(" -o ")[1].split()[0]
                data = blobs[f] + (b"x" if alter == f else b"")
                Path(dest).parent.mkdir(parents=True, exist_ok=True)
                Path(dest).write_bytes(data)
                continue
            script.append(c)
        return subprocess.run(["bash", "-c", "\n".join(script)], capture_output=True, text=True)
    bad = run("rke2.linux-amd64.tar.gz")
    assert bad.returncode != 0 and not install.exists() and not art.exists()
    good = run(None)
    assert good.returncode == 0, good.stderr
    assert install.read_bytes() == blobs["install.sh"] and oct(install.stat().st_mode & 0o777) == "0o700"


def test_pre_commands_without_ca_use_the_system_cas():
    assert not [c for c in ro.pre_commands("r.client", ro.REPO, DIGESTS, ca_path=None) if "--cacert" in c]


@pytest.mark.parametrize("host,machine", [("172.16.2.76", "172.16.2.76"), ("172.16.2.76:5000", "172.16.2.76"),
                                          ("registry.client", "registry.client")])
def test_netrc(host, machine):
    assert ro.netrc(host, "pull", PW) == f"machine {machine} login pull password {PW}\n"


def test_netrc_refuses_spaces():
    with pytest.raises(ValueError):
        ro.netrc("r", "pull", "a b")


def test_tags_and_versions():
    assert ro.tag_of(V) == "v1.34.11_rke2r1" and ro.k8s_of(V) == "v1.34.11" and ro.k8s_of("v1.34.11") is None
    m = json.loads(ro.manifest(DIGESTS, {f: 1 for f in ro.FILES}))
    assert m["artifactType"] == ro.ARTIFACT_TYPE and [x["annotations"][ro.A_TITLE] for x in m["layers"]] == list(ro.FILES)
    assert m["config"]["mediaType"] == "application/vnd.oci.empty.v1+json"


DECL = {"name": "amorcage", "endpoint": "172.16.2.76", "usage": ["images"], "mirrors": ["docker.io"]}
PUB = {"amorcage": [{"version": V, "package": "rke2", "pkg_version": "1.34.11-rke2r1", "item": "rke2"}]}


@pytest.mark.parametrize("spec,entries,published,codes", [
    ({"offline": False}, [], {}, []),
    ({"offline": True, "k8s_version": "v1.34.11"}, [{"decl": DECL, "no_fallback": True}], PUB, []),
    ({"offline": True, "k8s_version": "v1.34.11"}, [{"decl": DECL, "no_fallback": False}], PUB,
     ["offline-needs-no-fallback"]),
    ({"offline": True, "k8s_version": "v1.34.11"}, [{"decl": dict(DECL, mirrors=["ghcr.io"]), "no_fallback": True}],
     PUB, ["offline-needs-no-fallback"]),
    ({"offline": True, "k8s_version": "v1.34.11"}, [{"decl": DECL, "no_fallback": True}], {"amorcage": []},
     ["offline-rke2-missing"]),
    ({"offline": True, "k8s_version": "v1.33.5"}, [{"decl": DECL, "no_fallback": True}], PUB,
     ["offline-rke2-version-mismatch"]),
    ({"offline": True, "offline_artifacts": "image", "k8s_version": "v1.33.5"}, [{"decl": DECL, "no_fallback": True}],
     {}, []),
    ({"offline": True, "offline_artifacts": "image"}, [], {}, ["offline-needs-no-fallback"]),
])
def test_findings(spec, entries, published, codes):
    """Mutation « repli vers Internet admis en mode sans Internet »."""
    got = ro.findings(spec, published, entries)
    assert [f["code"] for f in got] == codes and all(f["level"] == "block" for f in got)


def test_standard_library_only():
    code = "import sys; sys.path.insert(0, %r); import rke2_offline" % str(ROOT / "bin" / "lib")
    p = subprocess.run([sys.executable, "-I", "-S", "-c", code], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


# --- gabarits ---------------------------------------------------------------------

def templates():
    return [{"kind": "Cluster", "metadata": {"name": "web", "namespace": "web"}, "spec": {"topology": {}}},
            {"kind": "RKE2ControlPlaneTemplate", "metadata": {"name": "cp"},
             "spec": {"template": {"spec": {"serverConfig": {"cni": "calico"}, "preRKE2Commands": ["echo own"]}}}},
            {"kind": "RKE2ConfigTemplate", "metadata": {"name": "w"}, "spec": {"template": {"spec": {}}}}]


def test_both_templates_receive_the_fetch_and_air_gap():
    patch = ro.template_patch("172.16.2.76", DIGESTS, "web-rke2-fetch")
    secret = {"kind": "Secret", "metadata": {"name": "web-rke2-fetch"}}
    out = cc.postprocess(templates(), cc.normalize({"name": "web", "offline": True}), offline=dict(patch, secrets=[secret]))
    for kind in cc.RKE2_TEMPLATE_KINDS:
        inner = next(d for d in out if d["kind"] == kind)["spec"]["template"]["spec"]
        assert inner["agentConfig"]["airGapped"] is True
        assert [f["path"] for f in inner["files"]] == [ro.NETRC, ro.CA_FILE]
        assert inner["files"][0]["permissions"] == "0600" and "content" not in inner["files"][0]
        assert inner["preRKE2Commands"][-1].startswith("install ")
    cp = next(d for d in out if d["kind"] == "RKE2ControlPlaneTemplate")["spec"]["template"]["spec"]
    assert cp["preRKE2Commands"][0] == "echo own"                                  # les siennes d'abord
    assert secret in out


def test_image_mode_only_sets_air_gap_and_no_option_changes_nothing():
    out = cc.postprocess(templates(), cc.normalize({"name": "web", "offline": True, "offline_artifacts": "image"}),
                         offline={})
    inner = next(d for d in out if d["kind"] == "RKE2ConfigTemplate")["spec"]["template"]["spec"]
    # 2.8.1 (décision 241) : plus le cloud-init sans paquets et la remise
    # en place des dépôts (tests/api/test_offline_bootstrap_281.py)
    assert inner == {"agentConfig": {"airGapped": True, "additionalUserData": {"config": ro.user_data()}},
                     "preRKE2Commands": [ro.repos_back()]}
    plain = cc.postprocess(templates(), cc.normalize({"name": "web"}))
    assert "agentConfig" not in json.dumps([d for d in plain if d["kind"] in cc.RKE2_TEMPLATE_KINDS])


# --- harvester-capi -----------------------------------------------------------------

@pytest.fixture(scope="module")
def cli():
    s = importlib.util.spec_from_file_location("harvester_capi_offline", ROOT / "bin" / "harvester-capi.py")
    mod = importlib.util.module_from_spec(s)
    s.loader.exec_module(mod)
    return mod


@pytest.fixture
def state(tmp_path, monkeypatch, cli):
    st = tmp_path / "state"
    pkg = st / "artefacts.d" / "rke2-1.34.11-rke2r1"
    for rel in ro.files_of(V):
        (pkg / rel).parent.mkdir(parents=True, exist_ok=True)
        (pkg / rel).write_text(f"content of {rel}\n")
    (pkg / "artefacts.yaml").write_text(json.dumps({"name": "rke2", "version": "1.34.11-rke2r1", "items": [item()]}))
    (st / "registries.d").mkdir()
    rg.save(st, "amorcage", {"kind": "external", "endpoint": "172.16.2.76", "ca_pem": None, "usage": ["images"],
                             "mirrors": ["docker.io"], "clusters": ["harvlab"],
                             "published": [{"package": "rke2", "version": "1.34.11-rke2r1"}]})
    monkeypatch.setattr(cli, "_state", lambda: st)
    monkeypatch.setattr(cli.rg, "read_creds", lambda k, n, role: {"username": "pull", "password": PW})
    return st


def spec(**over):
    s = cc.normalize(dict({"name": "web", "k8s_version": "v1.34.11", "offline": True,
                           "registries": [{"registry": "amorcage", "no_fallback": True}]}, **over))
    return s


def test_offline_config_uses_the_package_digests_and_a_netrc_secret(cli, state):
    got = cli.offline_config(None, spec())
    [secret] = got["secrets"]
    assert secret["metadata"]["name"] == "web-rke2-fetch" and PW in secret["stringData"]["netrc"]
    assert PW not in json.dumps(got["preRKE2Commands"]) and PW not in json.dumps(got["files"])
    pkg = state / "artefacts.d" / "rke2-1.34.11-rke2r1"
    want = "sha256:" + hashlib.sha256((pkg / ro.files_of(V)[0]).read_bytes()).hexdigest()
    assert want in "\n".join(got["preRKE2Commands"])
    assert cli.offline_config(None, spec(offline_artifacts="image")) == {}
    assert cli.offline_config(None, spec(offline=False)) is None
    hidden = cli.offline_config(None, spec(), hidden=True)
    assert PW not in json.dumps(hidden)


@pytest.mark.parametrize("over,codes", [
    ({}, []),
    ({"registries": [{"registry": "amorcage"}]}, ["offline-needs-no-fallback"]),
    ({"k8s_version": "v1.33.5"}, ["offline-rke2-version-mismatch"]),
])
def test_offline_findings_of_a_request(cli, state, over, codes):
    assert [f["code"] for f in cli.offline_findings(spec(**over))] == codes


def test_the_secret_leaves_with_its_cluster_only(cli):
    gen = [{"metadata": {"name": n, "labels": {cc.GENERATED: "true"}}}
           for n in ("web-rke2-fetch", "web2-rke2-fetch", "web-registries")]
    assert sorted(s["metadata"]["name"] for s in cli._registry_secrets_of(gen, "web", "web")) == [
        "web-registries", "web-rke2-fetch"]
