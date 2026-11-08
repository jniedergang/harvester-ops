"""v2.6.0 : registres déclarés (registries), configuration RKE2 des clusters
enfants, rattachement (Secrets), constats, et la CLI harvester-registry
(kubectl simulé qui note sa ligne de commande et son entrée standard)."""

import base64
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import capi_cluster as cc  # noqa: E402
import deploy_plans as dp  # noqa: E402
import registries as rg  # noqa: E402

CLI = ROOT / "bin" / "harvester-registry.py"
PULL_PW, PUSH_PW = "pull-Pw-31415", "push-Pw-27182"


@pytest.fixture(scope="module")
def ca_pem(tmp_path_factory):
    import shutil
    if not shutil.which("openssl"):
        pytest.skip("openssl needed")
    d = tmp_path_factory.mktemp("ca")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                    "-keyout", str(d / "k"), "-out", str(d / "c"), "-days", "2", "-subj", "/CN=t",
                    "-addext", "basicConstraints=critical,CA:TRUE",
                    "-addext", "keyUsage=critical,keyCertSign,cRLSign"], check=True, capture_output=True)
    return (d / "c").read_text()


def decl(name, kind="external", endpoint=None, mirrors=("docker.io",), usage=("images", "charts"), ca=None, **kw):
    d = {"name": name, "kind": kind, "endpoint": endpoint or f"{name}.example:443", "usage": list(usage),
         "mirrors": list(mirrors), "clusters": ["harvlab2"]}
    if ca:
        d["ca_pem"] = ca
    if kind == "bootstrap":
        d.update(cluster="harvlab2", address="172.16.2.76", size_gib=20, storage_class="harv-rep1",
                 push_endpoint="172.16.2.76:5443")
    d.update(kw)
    return d


CLUSTER = {"name": "web", "namespace": "web"}


def creds(*names):
    return {n: {"username": f"pull-{n}", "password": PULL_PW} for n in names}


# ---------------------------------------------------------------------------
# Configuration RKE2 des clusters enfants
# ---------------------------------------------------------------------------
def test_one_bootstrap_registry_mirrors_its_origins_and_cuts_the_fallback_when_asked(ca_pem):
    d = decl("amorcage", kind="bootstrap", endpoint="172.16.2.76", mirrors=["ghcr.io", "docker.io"], ca=ca_pem)
    prc, files, secrets = rg.rke2_config(["amorcage"], {"amorcage": d}, CLUSTER, creds("amorcage"),
                                         no_fallback=True)
    assert prc == {"mirrors": {
        "ghcr.io": {"endpoint": ["https://172.16.2.76"], "rewrite": {"^(.*)$": "ghcr.io/$1"}},
        "docker.io": {"endpoint": ["https://172.16.2.76"], "rewrite": {"^(.*)$": "docker.io/$1"}}},
        "configs": {"172.16.2.76": {"auth": {"username": "pull-amorcage", "password": PULL_PW},
                                    "tls": {"ca_file": "/etc/rancher/rke2/tls/harvester-ops/amorcage/ca.crt"}}}}
    assert [f["path"] for f in files] == ["/etc/rancher/rke2/registries.yaml",
                                          "/etc/rancher/rke2/tls/harvester-ops/amorcage/ca.crt",
                                          "/etc/rancher/rke2/config.yaml.d/50-harvester-ops-registry.yaml"]
    assert files[-1] == {"path": "/etc/rancher/rke2/config.yaml.d/50-harvester-ops-registry.yaml",
                         "owner": "root:root", "permissions": "0600",
                         "content": "disable-default-registry-endpoint: true\n"}
    # 2.8.0 (m-14) : plus de `-auth` ; marque d'usage sur le Secret des nœuds
    ca, nodes = secrets
    assert nodes["metadata"]["name"] == "web-registries"
    assert nodes["metadata"]["labels"] == {cc.GENERATED: "true", rg.used_label("amorcage"): "used"}
    assert ca["type"] == "Opaque" and ca["stringData"] == {"ca.crt": ca_pem}       # jamais kubernetes.io/tls
    assert "insecure" not in json.dumps(prc)


@pytest.mark.parametrize("order", [["a", "b"], ["b", "a"]])
def test_two_registries_for_the_same_origin_keep_the_cluster_order(order):
    decls = {n: decl(n, mirrors=["docker.io", "quay.io"] if n == "a" else ["docker.io"]) for n in "ab"}
    prc, files, _ = rg.rke2_config(order, decls, CLUSTER, creds("a", "b"))
    assert prc["mirrors"]["docker.io"]["endpoint"] == [f"https://{n}.example:443" for n in order]
    assert prc["mirrors"]["quay.io"]["endpoint"] == ["https://a.example:443"]
    assert rg.NO_FALLBACK_FILE not in [f["path"] for f in files]    # registres du client seulement : repli gardé


@pytest.mark.parametrize("kind", ["external", "bootstrap"])
@pytest.mark.parametrize("no_fallback,files", [(None, 0), (True, 1), (False, 0)])
def test_fallback_is_cut_only_when_asked_never_by_default(kind, no_fallback, files):
    """Revue I-4, point 198 : `disable-default-registry-endpoint` vaut pour
    tous les registres miroités ; jamais coupé par défaut, même pour un
    registre d'amorçage (il couperait les images système de RKE2)."""
    prc, f, _ = rg.rke2_config(["z"], {"z": decl("z", kind=kind, mirrors=["ghcr.io"])}, CLUSTER, creds("z"),
                               no_fallback=no_fallback)
    assert [x["path"] for x in f].count(rg.NO_FALLBACK_FILE) == files


@pytest.mark.parametrize("mirrors_a,mirrors_b,ready,codes", [
    (["ghcr.io"], [], (), []),                                           # rien des images système
    (["docker.io"], [], (), ["cluster-registry-no-fallback-system"]),
    (["ghcr.io"], ["docker.io"], (), ["cluster-registry-no-fallback-system"]),   # miroité par l'autre registre
    (["registry.rancher.com"], [], (), ["cluster-registry-no-fallback-system"]),
    (["docker.io"], [], ("a",), []),                                     # le registre porte les images système
    (["docker.io"], ["docker.io"], ("b",), []),                          # l'un des deux suffit
    (["ghcr.io"], ["docker.io"], ("a",), ["cluster-registry-no-fallback-system"]),   # porté par le mauvais
])
def test_no_fallback_for_the_rke2_system_origins_needs_their_images(mirrors_a, mirrors_b, ready, codes):
    decls = {"a": decl("a", kind="bootstrap", mirrors=mirrors_a), "b": decl("b", mirrors=mirrors_b)}
    got = rg.fallback_findings(True, ["a", "b"], decls, ready)
    assert [f["code"] for f in got] == codes and all(f["level"] == "block" for f in got)
    assert rg.fallback_findings(False, ["a", "b"], decls, ()) == []        # rien à dire sans coupure


def test_a_charts_only_registry_stays_out_of_the_node_configuration():
    decls = {"img": decl("img"), "ch": decl("ch", usage=["charts"])}
    prc, _, secrets = rg.rke2_config(["ch", "img"], decls, CLUSTER, creds("img"))
    assert list(prc["configs"]) == ["img.example:443"]
    assert all("ch." not in e for m in prc["mirrors"].values() for e in m["endpoint"])
    assert [s["metadata"]["name"] for s in secrets] == ["web-registries"]
    assert secrets[0]["metadata"]["labels"] == {cc.GENERATED: "true", rg.used_label("img"): "used"}


def test_a_registry_without_ca_uses_the_system_cas_and_insecure_only_when_declared():
    prc, _, secrets = rg.rke2_config(["a"], {"a": decl("a")}, CLUSTER, creds("a"))
    assert prc["configs"]["a.example:443"] == {"auth": {"username": "pull-a", "password": PULL_PW}}
    assert [s["metadata"]["name"] for s in secrets] == ["web-registries"]
    prc, _, _ = rg.rke2_config(["a"], {"a": decl("a", insecure_skip_verify=True)}, CLUSTER, creds("a"))
    assert prc["configs"]["a.example:443"]["tls"] == {"insecure_skip_verify": True}


def test_origins_keep_their_host_so_two_paths_never_mix():
    decls = {"a": decl("a", mirrors=["docker.io", "ghcr.io", "localhost:5000"])}
    prc, _, _ = rg.rke2_config(["a"], decls, CLUSTER, creds("a"))
    rewrites = {o: m["rewrite"]["^(.*)$"] for o, m in prc["mirrors"].items()}
    assert rewrites == {"docker.io": "docker.io/$1", "ghcr.io": "ghcr.io/$1", "localhost:5000": "localhost_5000/$1"}


def test_only_secrets_carry_a_password_and_never_the_write_account():
    decls = {"a": decl("a"), "b": decl("b", kind="bootstrap")}
    prc, files, secrets = rg.rke2_config(["a", "b"], decls, CLUSTER, creds("a", "b"))
    assert PULL_PW not in json.dumps(files)                 # les nœuds le lisent dans un Secret
    assert PUSH_PW not in json.dumps([prc, files, secrets])


class FakeKube:
    def __init__(self, secrets=None):
        self.secrets, self.applied = dict(secrets or {}), []

    def get(self, kind, ns, name):
        assert kind == "secrets" and ns == rg.NAMESPACE
        return self.secrets.get(name)

    def apply(self, docs):
        self.applied.append(docs)


def secret(user, pw):
    return {"data": {"username": base64.b64encode(user.encode()).decode(),
                     "password": base64.b64encode(pw.encode()).decode()}}


def test_child_clusters_get_the_read_account_from_the_harvester_cluster():
    kube = FakeKube({"a-pull": secret("pull", PULL_PW), "a-push": secret("push", PUSH_PW)})
    pc = rg.pull_creds_for(kube, ["a"])
    doc, _, secrets = rg.rke2_config(["a"], {"a": decl("a")}, CLUSTER, pc)
    assert doc["configs"]["a.example:443"]["auth"] == {"username": "pull", "password": PULL_PW}
    assert json.loads(secrets[0]["stringData"]["registries.yaml"]) == doc
    with pytest.raises(KeyError):
        rg.pull_creds_for(FakeKube({"b-push": secret("push", PUSH_PW)}), ["b"])


# ---------------------------------------------------------------------------
# Déclarations
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("change,where", [
    (lambda d: d.update(colour="red"), "colour"),
    (lambda d: d.update(endpoint="https://h.example"), "endpoint"),
    (lambda d: d.update(endpoint="h.example/path"), "endpoint"),
    (lambda d: d.update(ca_pem="-----BEGIN CERTIFICATE-----\nzz\n-----END CERTIFICATE-----\n"), "ca_pem"),
    (lambda d: d.update(ca_pem="-----BEGIN PRIVATE KEY-----\nzz\n"), "ca_pem"),
    (lambda d: d.update(usage=["images", "blobs"]), "usage"),
    (lambda d: d.update(mirrors=["https://docker.io"]), "mirrors"),
    (lambda d: d.update(size_gib=20), "size_gib"),
    (lambda d: d.update(kind="harbor"), "kind"),
    (lambda d: d.update(software="distribution"), "software"),
    (lambda d: d.update(name="r" * 31), "name"),
])
def test_a_bad_declaration_is_refused_with_its_field(change, where):
    d = decl("harbor-client")
    change(d)
    with pytest.raises(dp.ProfileError) as e:
        rg.check_decl(d)
    assert where in [w for w, _ in e.value.errors], e.value.errors


@pytest.mark.parametrize("size", [4, 2049, "20", True])
def test_a_bootstrap_registry_size_is_an_integer_from_5_to_2048(size):
    with pytest.raises(dp.ProfileError) as e:
        rg.check_decl(decl("z", kind="bootstrap", size_gib=size))
    assert [w for w, _ in e.value.errors] == ["size_gib"]


def test_declarations_are_saved_with_revisions_and_forgotten(tmp_path, ca_pem):
    d = decl("harbor-client", ca=ca_pem)
    d.pop("name")
    saved = rg.save(tmp_path, "harbor-client", d)
    assert saved["revision"] == 1 and saved["ca_pem"] == ca_pem
    assert stat.S_IMODE((tmp_path / "registries.d" / "harbor-client.yaml").stat().st_mode) == 0o600
    with pytest.raises(dp.PlanConflict):
        rg.save(tmp_path, "harbor-client", d)
    rg.update(tmp_path, "harbor-client", lambda x: x.update(mirrors=["ghcr.io"]))
    assert rg.load(tmp_path, "harbor-client")["revision"] == 2
    assert [x["name"] for x in rg.list_decls(tmp_path)] == ["harbor-client"]
    with pytest.raises(ValueError, match="registry-in-use: plan-a, web"):
        rg.forget(tmp_path, "harbor-client", used_by=["web", "plan-a"])
    assert rg.forget(tmp_path, "harbor-client")
    with pytest.raises(dp.ProfileError):
        rg.save(tmp_path, "bootstrap", d)
    z = decl("z", kind="bootstrap")
    z.pop("name")
    rg.save(tmp_path, "z", z)
    with pytest.raises(ValueError, match="delete"):
        rg.forget(tmp_path, "z")


# ---------------------------------------------------------------------------
# Rattachement et constats
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("given,error", [
    ({"push": {"username": "u", "password": PUSH_PW}}, None),
    ({"push": {"username": "u", "password": PUSH_PW}, "pull": {"username": "r", "password": PULL_PW}}, None),
    ({"pull": {"username": "r", "password": PULL_PW}}, "credentials.push"),
    ({"push": {"username": "u:x", "password": PUSH_PW}}, "credentials.push"),
    ({"push": {"username": "u", "password": ""}}, "credentials.push"),
    ({"push": {"username": "u", "password": PUSH_PW}, "admin": {}}, "credentials:"),
    (None, "credentials:"),
])
def test_credentials_are_checked_without_echoing_them(given, error):
    if error:
        with pytest.raises(ValueError) as e:
            rg.check_creds(given)
        assert error in str(e.value) and PUSH_PW not in str(e.value) and PULL_PW not in str(e.value)
        return
    c = rg.check_creds(given)
    assert c["pull"] == given.get("pull", given["push"])


def test_attach_poses_three_labelled_secrets_and_warns_when_pull_is_push(ca_pem):
    kube = FakeKube()
    res = rg.attach(kube, decl("harbor-client", ca=ca_pem), {"push": {"username": "u", "password": PUSH_PW}})
    ns, push, pull, ca = kube.applied[0]
    assert ns["metadata"] == {"name": "harvester-ops-registry", "labels": {"harvester-ops.io/managed": "true"}}
    assert [push["metadata"]["name"], pull["metadata"]["name"], ca["metadata"]["name"]] == \
        ["harbor-client-push", "harbor-client-pull", "harbor-client-ca"]
    assert push["type"] == pull["type"] == "kubernetes.io/basic-auth" and ca["type"] == "Opaque"
    assert all(d["metadata"]["labels"] == {rg.LABEL: "harbor-client"} for d in (push, pull, ca))
    assert [f["code"] for f in res["findings"]] == ["registry-pull-is-push"]
    assert PUSH_PW not in json.dumps(res)


@pytest.mark.parametrize("d,facts,codes", [
    (None, {}, ["registry-unknown"]),
    ("ok", {"attached": False}, ["registry-not-attached"]),
    ("ok", {"attached": True, "reachable": False}, ["registry-unreachable"]),
    ("ok", {"attached": True, "ca_ok": False}, ["registry-ca-invalid"]),
    ("ok", {"attached": True, "auth": {"pull": False, "push": True}}, ["registry-auth-refused"]),
    ("ok", {"attached": True, "auth": {"pull": True, "push": True}, "pull_is_push": True}, ["registry-pull-is-push"]),
    ("no-usage", {"attached": True, "auth": {"pull": True, "push": True}}, ["registry-no-usage"]),
    ("insecure", {"attached": True, "auth": {"pull": True, "push": True}}, ["registry-insecure"]),
    ("ok", {"attached": True, "auth": {"pull": True, "push": True}}, []),
])
def test_each_registry_finding(d, facts, codes):
    dd = {"ok": decl("a"), "no-usage": decl("a", usage=[]), "insecure": decl("a", insecure_skip_verify=True),
          None: None}[d]
    got = rg.check(dd, facts, "a")
    assert [f["code"] for f in got if f["code"] != rg.VM_NETWORK_WARNING] == codes
    # revue finale I-4 : toute sonde faite le dit, vue de la console seulement
    probed = dd is not None and facts.get("attached") is not False
    assert [f["code"] for f in got if f["code"] == rg.VM_NETWORK_WARNING] == ([rg.VM_NETWORK_WARNING] if probed else [])
    levels = {"registry-pull-is-push": "warn", "registry-insecure": "warn", rg.VM_NETWORK_WARNING: "warn"}
    assert all(f["level"] == levels.get(f["code"], "block") for f in got)


def test_facts_probe_read_then_write_from_the_console():
    kube = FakeKube({"a-pull": secret("pull", PULL_PW), "a-push": secret("push", PUSH_PW)})
    calls = []

    def probe(d, c, role):
        calls.append((role, c["username"]))
        return True, True, role == "pull"
    f = rg.facts(kube, decl("a"), "harvlab2", probe_fn=probe)
    assert f == {"attached": True, "auth": {"pull": True, "push": False}, "pull_is_push": False}
    assert calls == [("pull", "pull"), ("push", "push")]
    assert rg.facts(kube, decl("a"), "harv4", probe_fn=probe) == {"attached": False}


def test_probe_reads_the_registry_without_writing(tmp_path):
    import test_oci_push_175 as t175
    r = t175.FakeRegistry(user="pull", password=PULL_PW, basic=True)
    try:
        class Plain:
            def __new__(cls, host, creds, ca_pem=None, timeout=15):
                import oci_push as op
                return op.Registry(host, creds, plain_http=True, timeout=timeout)
        d = decl("a", endpoint=r.host)
        assert rg.probe(d, {"username": "pull", "password": PULL_PW}, "pull", registry_cls=Plain) == (True, True, True)
        assert rg.probe(d, {"username": "pull", "password": "bad"}, "pull", registry_cls=Plain) == (True, True, False)
        assert {m for m, *_ in r.log} == {"GET"}
    finally:
        r.close()
    assert rg.probe(decl("a", endpoint="127.0.0.1:1"), {"username": "u", "password": "p"}, "pull",
                    registry_cls=Plain)[0] is False


# ---------------------------------------------------------------------------
# CLI (kubectl simulé)
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_kubectl(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    log = tmp_path / "kubectl.log"
    (d / "kubectl").write_text(f"""#!/bin/sh
printf '%s\\n' "$*" >> {log}
case "$*" in
  *apply*) cat >> {log}.stdin; echo applied ;;
  *"get secrets"*) echo 'Error from server (NotFound): secrets not found' >&2; exit 1 ;;
  *) echo '{{}}' ;;
esac
""")
    (d / "kubectl").chmod(0o755)
    kc = tmp_path / "kc"
    kc.write_text("apiVersion: v1\n")
    return {"path": str(d), "log": log, "kc": kc}


def cli(tmp_path, *args, stdin=None, env_extra=None):
    env = dict(os.environ, HARVESTER_OPS_CONFIG=str(tmp_path / "none.yaml"), **(env_extra or {}))
    return subprocess.run([sys.executable, str(CLI), "--state-dir", str(tmp_path / "state"), *args],
                          input=stdin, capture_output=True, text=True, env=env, timeout=60)


def test_cli_declare_attach_and_check(tmp_path, fake_kubectl, ca_pem):
    f = tmp_path / "r.json"
    f.write_text(json.dumps(dict(decl("harbor-client", ca=ca_pem), clusters=[])))
    p = cli(tmp_path, "declare", "harbor-client", "--file", f)
    assert p.returncode == 0, p.stderr
    p = cli(tmp_path, "declare", "harbor-client", "--file", f)
    assert p.returncode == 2 and json.loads(p.stdout)["current_revision"] == 1
    env = {"PATH": fake_kubectl["path"] + os.pathsep + os.environ["PATH"]}
    body = json.dumps({"push": {"username": "push", "password": PUSH_PW},
                       "pull": {"username": "pull", "password": PULL_PW}})
    p = cli(tmp_path, "attach", "harbor-client", "--cluster", "harvlab2", "--kubeconfig", fake_kubectl["kc"],
            stdin=body, env_extra=env)
    assert p.returncode == 0, p.stderr
    argv = fake_kubectl["log"].read_text()
    stdin = Path(str(fake_kubectl["log"]) + ".stdin").read_text()
    assert PUSH_PW not in argv and PULL_PW not in argv                      # jamais en argument
    assert PUSH_PW in stdin and PULL_PW in stdin                            # dans le corps de kubectl
    assert PUSH_PW not in p.stdout + p.stderr and PULL_PW not in p.stdout + p.stderr
    shown = json.loads(cli(tmp_path, "show", "harbor-client").stdout)
    assert shown["clusters"] == ["harvlab2"] and shown["revision"] == 2
    p = cli(tmp_path, "check", "harbor-client", "--cluster", "harvlab2", "--kubeconfig", fake_kubectl["kc"],
            env_extra=env)
    assert p.returncode == 2 and [x["code"] for x in json.loads(p.stdout)["findings"]] == ["registry-not-attached"]
    p = cli(tmp_path, "check", "nope", "--cluster", "harvlab2", "--kubeconfig", fake_kubectl["kc"], env_extra=env)
    assert [x["code"] for x in json.loads(p.stdout)["findings"]] == ["registry-unknown"]


def test_cli_refuses_a_hand_made_bootstrap_registry_and_bad_credentials(tmp_path, fake_kubectl):
    f = tmp_path / "r.json"
    f.write_text(json.dumps(decl("z", kind="bootstrap")))
    p = cli(tmp_path, "declare", "z", "--file", f)
    assert p.returncode == 2 and "declared by deploy" in p.stderr
    f.write_text(json.dumps(decl("a")))
    assert cli(tmp_path, "declare", "a", "--file", f).returncode == 0
    p = cli(tmp_path, "attach", "a", "--cluster", "harvlab2", "--kubeconfig", fake_kubectl["kc"],
            stdin='{"push": {"username": "u"}}')
    assert p.returncode == 2 and "credentials.push" in p.stderr
    assert not fake_kubectl["log"].exists()
    assert cli(tmp_path, "forget", "a").returncode == 0
    assert cli(tmp_path, "forget", "a").returncode == 2


def test_attach_refuses_a_bootstrap_registry():
    """Revue I-3 : les comptes d'un registre d'amorçage sont générés par la
    console ; un opérateur ne doit pas pouvoir les remplacer."""
    kube = FakeKube()
    with pytest.raises(ValueError, match="registry-bootstrap"):
        rg.attach(kube, decl("amorcage", kind="bootstrap"), {"push": {"username": "u", "password": PUSH_PW}})
    assert kube.applied == []


@pytest.mark.parametrize("user,password,ok", [
    ("pull", "pass word:with colon", True),          # revue m-3 : un mot de passe peut porter espace et ':'
    ("pull", "x\n", False),                          # jamais de fin de ligne (fichier htpasswd, config.json)
    ("pu ll", "x", False),
    ("pu:ll", "x", False),
    ("pull\n", "x", False),                          # `$` laissait passer une fin de ligne
])
def test_credentials_rules_apply_the_colon_rule_to_the_username_only(user, password, ok):
    creds = {"push": {"username": user, "password": password}}
    if ok:
        assert rg.check_creds(creds)["push"]["password"] == password
    else:
        with pytest.raises(ValueError) as e:
            rg.check_creds(creds)
        assert password not in str(e.value) or password == "x"


def test_a_host_with_a_trailing_newline_is_refused():
    with pytest.raises(dp.ProfileError):
        rg.check_decl(decl("a", endpoint="h.example:443\n"))


def test_two_registries_with_a_ca_each_keep_both_configs_on_the_nodes(ca_pem):
    """2.6.0, vu sur harvlab2 : CAPRKE2 v0.25.2 (`pkg/rke2/registries.go`,
    encore sur main) recrée `Configs` à chaque registre et écrit toute CA
    dans le même `/etc/rancher/rke2/tls/ca.crt` : avec deux registres, un
    seul gardait son compte et sa CA sur les nœuds. La console écrit donc
    elle-même `registries.yaml` (fichier tiré d'un Secret) et une CA par
    registre, sans `privateRegistriesConfig`."""
    decls = {"client": decl("client", endpoint="172.16.1.11:5443", ca=ca_pem),
             "amorcage": decl("amorcage", kind="bootstrap", endpoint="172.16.2.76", ca=ca_pem + "\n")}
    doc, files, secrets = rg.rke2_config(["client", "amorcage"], decls, CLUSTER, creds("client", "amorcage"))
    assert doc["configs"] == {
        "172.16.1.11:5443": {"auth": {"username": "pull-client", "password": PULL_PW},
                             "tls": {"ca_file": "/etc/rancher/rke2/tls/harvester-ops/client/ca.crt"}},
        "172.16.2.76": {"auth": {"username": "pull-amorcage", "password": PULL_PW},
                        "tls": {"ca_file": "/etc/rancher/rke2/tls/harvester-ops/amorcage/ca.crt"}}}
    nodes = next(s for s in secrets if s["metadata"]["name"] == "web-registries")
    assert nodes["type"] == "Opaque" and nodes["metadata"]["labels"] == {
        cc.GENERATED: "true", rg.used_label("client"): "used", rg.used_label("amorcage"): "used"}
    assert json.loads(nodes["stringData"]["registries.yaml"]) == doc
    assert nodes["stringData"]["ca-client.crt"] == ca_pem and nodes["stringData"]["ca-amorcage.crt"] == ca_pem + "\n"
    by_path = {f["path"]: f for f in files}
    assert by_path["/etc/rancher/rke2/registries.yaml"] == {
        "path": "/etc/rancher/rke2/registries.yaml", "owner": "root:root", "permissions": "0600",
        "contentFrom": {"secret": {"name": "web-registries", "key": "registries.yaml"}}}
    assert by_path["/etc/rancher/rke2/tls/harvester-ops/client/ca.crt"]["contentFrom"] == {
        "secret": {"name": "web-registries", "key": "ca-client.crt"}}
    assert "/etc/rancher/rke2/tls/harvester-ops/amorcage/ca.crt" in by_path
    assert PULL_PW not in json.dumps(files)
