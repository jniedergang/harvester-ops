"""2.6.0 (tâche 9) : les clusters enfants branchés sur leurs registres à la
création (point 190) : demande, constats, configuration de registres privés
de RKE2 posée dans les deux gabarits rendus par le vrai générateur, comptes
de lecture seulement (point 188), repli vers Internet jamais coupé par
défaut (point 198), Secrets partis avec le cluster, modèles à variable."""

import argparse
import base64
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import capi_cluster as cc  # noqa: E402
import deploy_plans as dp  # noqa: E402
import registries as rg  # noqa: E402

PULL_PW, PUSH_PW = "pull-Pw-31415", "push-Pw-27182"
CA = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("harvester_capi_260", ROOT / "bin" / "harvester-capi.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def demand(**kw):
    base = {"name": "web", "k8s_version": "v1.34.11", "image": "default/image-abc", "ssh_keypair": "default/ju",
            "network": "default/production", "gateway": "172.16.0.1", "subnet_mask": "255.255.0.0",
            "ip_pool": "capi-vm-pool", "dns": ["172.16.3.6"]}
    base.update(kw)
    return cc.normalize(base)


def decl(name, kind="external", mirrors=("ghcr.io",), usage=("images", "charts"), clusters=("harvlab2",), ca=True,
         published=()):
    d = {"name": name, "kind": kind, "endpoint": f"{name}.example:443", "usage": list(usage),
         "mirrors": list(mirrors), "clusters": list(clusters)}
    if ca:
        d["ca_pem"] = CA
    if published:
        d["published"] = list(published)
    return d


# ---------------------------------------------------------------------------
# La demande
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("given,expected", [
    (None, None),
    ([], None),
    ("a, b", [{"registry": "a"}, {"registry": "b"}]),
    (["a", {"registry": "b", "mirrors": ["docker.io", "docker.io"], "no_fallback": "true"}],
     [{"registry": "a"}, {"registry": "b", "mirrors": ["docker.io"], "no_fallback": True}]),
    ([{"registry": "a", "no_fallback": False}], [{"registry": "a", "no_fallback": False}]),
])
def test_registries_are_normalized_in_order(given, expected):
    spec = demand(registries=given) if given is not None else demand()
    assert spec.get("registries") == expected


@pytest.mark.parametrize("given", [
    {"registry": "a"},                                   # pas une liste
    [{"registry": "a", "push": "x"}],                    # clé inconnue (aucun compte dans la demande)
    [{"mirrors": ["docker.io"]}],                        # nom absent
    [{"registry": "a", "mirrors": "docker.io"}],         # miroirs pas en liste
    [{"registry": "a", "no_fallback": "maybe"}],
    [3],
])
def test_a_malformed_registry_list_is_refused(given):
    with pytest.raises(ValueError, match="registries"):
        demand(registries=given)


@pytest.mark.parametrize("given,message", [
    (["Bad_Name"], "registry name"),
    (["a" * 31], "registry name"),
    (["a", "a"], "listed twice"),
    ([{"registry": "a", "mirrors": ["https://docker.io"]}], "registry host"),
])
def test_bad_names_and_hosts_are_named_by_validate(given, message):
    errs = dict(cc.validate(demand(registries=given)))
    assert message in errs.get("registries", "")


def test_registries_never_reach_the_generator():
    argv = cc.to_argv(demand(registries=["a"]), "/k")
    assert not [a for a in argv if "regist" in a]


# ---------------------------------------------------------------------------
# Constats (tableau : un cas par chemin)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("entries,decls,attached,ready,codes", [
    (["a"], {"a": decl("a")}, {"a": True}, (), []),
    (["x"], {}, {}, (), [("cluster-registry-unknown", "block")]),
    (["a"], {"a": decl("a", clusters=["harv4"])}, {"a": True}, (),
     [("cluster-registry-not-attached", "block")]),
    (["a"], {"a": decl("a")}, {"a": False}, (), [("cluster-registry-not-attached", "block")]),
    (["a"], {"a": decl("a", usage=["charts"])}, {"a": True}, (), [("cluster-registry-no-images", "warn")]),
    ([{"registry": "a", "mirrors": ["quay.io"]}], {"a": decl("a")}, {"a": True}, (),
     [("cluster-registry-mirror-unknown", "block")]),
    ([{"registry": "a", "no_fallback": True}], {"a": decl("a", mirrors=["docker.io"])}, {"a": True}, (),
     [("cluster-registry-no-fallback-system", "block")]),
    ([{"registry": "a", "no_fallback": True}], {"a": decl("a", mirrors=["docker.io"])}, {"a": True}, ("a",), []),
    # miroirs de l'entrée : docker.io laissé de côté, la coupure est permise
    ([{"registry": "a", "mirrors": ["ghcr.io"], "no_fallback": True}],
     {"a": decl("a", mirrors=["docker.io", "ghcr.io"])}, {"a": True}, (), []),
    # par défaut, rien n'est coupé : aucun constat, même avec docker.io
    (["a"], {"a": decl("a", kind="bootstrap", mirrors=["docker.io"])}, {"a": True}, (), []),
])
def test_child_registry_findings(entries, decls, attached, ready, codes):
    got = rg.check_child(entries, decls, "harvlab2", attached, ready)
    assert [(f["code"], f["level"]) for f in got] == codes


def test_system_images_are_found_in_a_published_package(tmp_path):
    root = tmp_path / "artefacts.d" / "rke2-1.0.0"
    root.mkdir(parents=True)
    (root / "artefacts.yaml").write_text(json.dumps({"name": "rke2", "version": "1.0.0", "items": [
        {"name": "sys", "kind": "images", "images": ["docker.io/rancher/rke2-runtime:v1.34.11-rke2r1"]}]}))
    decls = {"a": decl("a", published=[{"package": "rke2", "version": "1.0.0"}]),
             "b": decl("b", published=[{"package": "absent", "version": "1.0.0"}]), "c": decl("c")}
    assert rg.system_ready_registries(tmp_path, decls) == {"a"}


# ---------------------------------------------------------------------------
# Rendu réel par le générateur embarqué
# ---------------------------------------------------------------------------
@pytest.fixture
def generated(cli, tmp_path):
    if not shutil.which("yq"):
        pytest.skip("yq needed")
    kc = tmp_path / "kc.yaml"
    kc.write_text("apiVersion: v1\nkind: Config\n")

    def run(entries, decls):
        spec = demand(registries=entries)
        creds = {n: {"username": f"pull-{n}", "password": PULL_PW} for n in decls}
        _, files, secrets = rg.rke2_config(spec["registries"], decls, {"name": "web", "namespace": "web"}, creds)
        return cli.render(spec, str(kc), registries={"files": files, "secrets": secrets})
    return run


def _templates(docs):
    return {d["kind"]: d["spec"]["template"]["spec"] for d in docs if d["kind"] in cc.RKE2_TEMPLATE_KINDS}


@pytest.mark.parametrize("entries,decls,mirrors,files", [
    (["a"], {"a": decl("a")}, {"ghcr.io": ["https://a.example:443"]}, 0),
    ([{"registry": "a", "no_fallback": True}], {"a": decl("a")}, {"ghcr.io": ["https://a.example:443"]}, 1),
    (["b", "a"], {"a": decl("a", mirrors=["ghcr.io", "quay.io"]), "b": decl("b", ca=False)},
     {"ghcr.io": ["https://b.example:443", "https://a.example:443"], "quay.io": ["https://a.example:443"]}, 0),
])
def test_the_real_templates_carry_the_registries_on_both_roles(generated, entries, decls, mirrors, files):
    docs = generated(entries, decls)
    tpl = _templates(docs)
    assert set(tpl) == set(cc.RKE2_TEMPLATE_KINDS), "control plane AND workers (M-cp-only)"
    nodes = next(d for d in docs if d["kind"] == "Secret" and d["metadata"]["name"] == "web-registries")
    prc = json.loads(nodes["stringData"]["registries.yaml"])
    assert {o: m["endpoint"] for o, m in prc["mirrors"].items()} == mirrors
    assert all(m["rewrite"] == {"^(.*)$": f"{o}/$1"} for o, m in prc["mirrors"].items())
    assert "insecure" not in json.dumps(prc)
    for kind, inner in tpl.items():
        # CAPRKE2 v0.25.2 ne garde qu'un registre de privateRegistriesConfig
        assert "privateRegistriesConfig" not in inner, kind
        paths = [f["path"] for f in inner.get("files") or []]
        assert rg.REGISTRIES_FILE in paths, kind
        assert sum(p.startswith(rg.CA_DIR) for p in paths) == sum(1 for d in decls.values() if d.get("ca_pem"))
        got = [p for p in paths if p == rg.NO_FALLBACK_FILE]
        assert len(got) == files, kind              # M-files-always : jamais sans le demander
    serverconf = next(d for d in docs if d["kind"] == "RKE2ControlPlaneTemplate")["spec"]["template"]["spec"]
    assert serverconf["serverConfig"]["cni"] == "calico", "the generator's own fields are kept"
    secrets = [d for d in docs if d["kind"] == "Secret" and "-registry-" in d["metadata"]["name"]]
    assert len(secrets) == len(decls) + sum(1 for d in decls.values() if d.get("ca_pem"))
    for s in secrets:
        assert s["metadata"]["labels"][cc.GENERATED] == "true"
        assert s["metadata"]["namespace"] == "web" and s["type"] == "Opaque"
    blob = json.dumps(docs)
    # compte de lecture : un Secret -auth par registre, plus registries.yaml
    # (JSON dans le Secret des nœuds, guillemets échappés)
    assert PUSH_PW not in blob and blob.count(PULL_PW) == 2 * len(decls)


def test_existing_files_of_a_template_are_kept():
    docs = [{"kind": "RKE2ConfigTemplate", "metadata": {"name": "w"},
             "spec": {"template": {"spec": {"files": [{"path": "/etc/x", "content": "y"}]}}}}]
    out = cc.postprocess(docs, demand(), registries={"config": {"mirrors": {}},
                                                     "files": [{"path": rg.NO_FALLBACK_FILE}], "secrets": []})
    assert [f["path"] for f in out[0]["spec"]["template"]["spec"]["files"]] == ["/etc/x", rg.NO_FALLBACK_FILE]
    assert docs[0]["spec"]["template"]["spec"]["files"] == [{"path": "/etc/x", "content": "y"}], "input untouched"


def test_without_registries_nothing_changes():
    docs = [{"kind": "RKE2ConfigTemplate", "metadata": {"name": "w"}, "spec": {"template": {"spec": {}}}}]
    assert cc.postprocess(docs, demand())[0]["spec"]["template"]["spec"] == {}


# ---------------------------------------------------------------------------
# harvester-capi : comptes lus à la création, masqués à l'aperçu
# ---------------------------------------------------------------------------
class Kube:
    def __init__(self, objs=None):
        self.objs = dict(objs or {})
        self.calls = []

    def get(self, kind, ns, name):
        self.calls.append(("get", kind, ns, name))
        return self.objs.get((kind, ns, name))

    def list(self, kind, ns=None, selector=None):
        want = dict(p.split("=", 1) for p in (selector or "").split(",") if p)
        return [o for (k, s, n), o in self.objs.items() if k == kind and (ns is None or s == ns)
                and all(((o.get("metadata") or {}).get("labels") or {}).get(a) == b for a, b in want.items())]

    def delete(self, kind, ns, name, cascade=None):
        self.calls.append(("delete", kind, ns, name))
        self.objs.pop((kind, ns, name), None)

    def apply(self, docs, timeout=None):
        self.calls.append(("apply", len(docs)))


def _secret(user, pw):
    return {"data": {"username": base64.b64encode(user.encode()).decode(),
                     "password": base64.b64encode(pw.encode()).decode()}}


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(tmp_path))
    rg.save(tmp_path, "amorcage", {"kind": "external", "endpoint": "172.16.2.76", "usage": ["images", "charts"],
                                   "mirrors": ["ghcr.io"], "clusters": ["harvlab2"]})
    return tmp_path


def test_create_reads_only_the_read_account(cli, state):
    kube = Kube({("secrets", rg.NAMESPACE, "amorcage-pull"): _secret("pull", PULL_PW),
                 ("secrets", rg.NAMESPACE, "amorcage-push"): _secret("push", PUSH_PW)})
    regs = cli.registry_config(kube, demand(registries=["amorcage"]), "harvlab2")
    assert [c[3] for c in kube.calls if c[1] == "secrets"] == ["amorcage-pull"]
    assert PULL_PW in json.dumps(regs) and PUSH_PW not in json.dumps(regs)


@pytest.mark.parametrize("entry,tail", [
    ("amorcage", "mirrors ghcr.io"),
    ({"registry": "amorcage", "no_fallback": True}, "mirrors ghcr.io, no fallback to Internet"),
])
def test_the_create_step_names_the_mirrors_and_the_fallback(cli, state, entry, tail):
    """2.6.0, vu sur harvlab2 : la ligne de l'étape lisait une clé retirée
    et la création finissait en exit 1 sans un mot."""
    kube = Kube({("secrets", rg.NAMESPACE, "amorcage-pull"): _secret("pull", PULL_PW)})
    spec = demand(registries=[entry])
    regs = cli.registry_config(kube, spec, "harvlab2")
    assert cli.registries_step(spec, regs) == f"1 registry(ies): {tail}"


def test_preview_reads_no_account_and_shows_none(cli, state):
    kube = Kube({("secrets", rg.NAMESPACE, "amorcage-pull"): _secret("pull", PULL_PW)})
    regs = cli.registry_config(kube, demand(registries=["amorcage"]), "harvlab2", hidden=True)
    assert not [c for c in kube.calls if c[1] == "secrets"]
    shown = json.dumps(cc.masked(regs["secrets"]))
    assert PULL_PW not in shown and "<hidden>" in shown


@pytest.mark.parametrize("objs,regs,error", [
    ({}, ["amorcage"], "cluster-registry-not-attached"),
    ({("secrets", rg.NAMESPACE, "amorcage-pull"): _secret("pull", PULL_PW)}, ["absent"], "cluster-registry-unknown"),
])
def test_create_stops_before_any_write_when_a_registry_is_missing(cli, state, objs, regs, error):
    with pytest.raises(ValueError, match=error):
        cli.registry_config(Kube(objs), demand(registries=regs), "harvlab2")


def test_check_adds_the_registry_findings(cli, state):
    kube = Kube()                                 # compte de lecture absent sur ce cluster
    got = cli.registry_findings(kube, demand(registries=["amorcage", "absent"]), "harvlab2")
    assert [f["code"] for f in got] == ["cluster-registry-not-attached", "cluster-registry-unknown"]
    assert cli.registry_findings(kube, demand(), "harvlab2") == []


def test_cli_render_masks_the_registry_secrets(cli, state, monkeypatch, capsys):
    if not shutil.which("yq"):
        pytest.skip("yq needed")
    kc = state / "kc.yaml"
    kc.write_text("apiVersion: v1\nkind: Config\n")
    kube = Kube({("secrets", rg.NAMESPACE, "amorcage-pull"): _secret("pull", PULL_PW)})
    monkeypatch.setattr(cli, "kube_from", lambda a: (kube, str(kc), None))
    monkeypatch.setattr(cli, "spec_from", lambda p: demand(registries=["amorcage"]))
    args = argparse.Namespace(spec="-", identity="console", show_secrets=False, cluster="harvlab2",
                              kubeconfig=str(kc), harvester_name="harvlab2")
    assert cli.cmd_render(args) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "web-registries" in out and "web-registry-amorcage-auth" in out
    assert PULL_PW not in out


# ---------------------------------------------------------------------------
# Suppression : les Secrets des registres partent avec le cluster
# ---------------------------------------------------------------------------
def test_deleting_the_cluster_removes_its_registry_secrets(cli):
    creds = {"a": {"username": "pull", "password": PULL_PW}}
    _, _, secrets = rg.rke2_config(["a"], {"a": decl("a")}, {"name": "web", "namespace": "web"}, creds)
    kube = Kube({("namespaces", None, "web"): {"metadata": {"name": "web", "labels": {"harvester-ops.io/managed": "true"}}}})
    for s in secrets:
        kube.objs[("secrets", "web", s["metadata"]["name"])] = s
    assert cli.remove_cluster(kube, "web", "web", 60, sleep=lambda s: None) == cli.EXIT_OK
    assert not [k for k in kube.objs if k[0] == "secrets"], "M-not-generated: registry secrets left behind"


@pytest.mark.parametrize("deleted,kept", [("web", "web2"), ("web2", "web"), ("web", "web-api")])
def test_deleting_one_of_two_clusters_in_a_namespace_removes_its_registry_secrets(cli, deleted, kept):
    """Revue finale 2.6.0 (I-1) : deux clusters dans un namespace ; la
    suppression de l'un retire SES Secrets de registre (registries.yaml,
    -auth, -ca, -chart-), par nom ; ceux de l'autre restent, et le
    registre n'est plus compté pour le cluster parti."""
    import capi_services as sv
    creds = {"a": {"username": "pull", "password": PULL_PW}}
    kube = Kube({("namespaces", None, "shared"): {"metadata": {"name": "shared"}},
                 (cli.cs.K_CLUSTER, "shared", kept): {"metadata": {"name": kept}}})
    for c in (deleted, kept):
        for s in rg.rke2_config(["a"], {"a": decl("a")}, {"name": c, "namespace": "shared"}, creds)[2]:
            kube.objs[("secrets", "shared", s["metadata"]["name"])] = s
        for s in sv.registry_secrets({"cluster": f"shared/{c}", "name": "podinfo"}, "a", "a.example:443", creds["a"],
                                     labels={cc.GENERATED: "true", rg.LABEL: "a"}):
            kube.objs[("secrets", "shared", s["metadata"]["name"])] = s
    # un Secret rendu partagé (sans nom de cluster) reste : règle de 2.5.0
    kube.objs[("secrets", "shared", "capi-shared")] = {"metadata": {"name": "capi-shared",
                                                                   "labels": {cc.GENERATED: "true"}}}
    assert cli.remove_cluster(kube, "shared", deleted, 60, sleep=lambda s: None) == cli.EXIT_OK
    left = sorted(n for (k, _, n) in kube.objs if k == "secrets")
    assert left == sorted(["capi-shared", f"{kept}-registries", f"{kept}-registry-a-auth",
                           f"{kept}-registry-a-ca", f"{kept}-chart-a"])
    assert ("namespaces", None, "shared") in kube.objs


def test_registry_users_are_read_from_the_labelled_secrets():
    creds = {"a": {"username": "pull", "password": PULL_PW}}
    kube = Kube()
    for ns in ("web", "api"):
        for s in rg.rke2_config(["a"], {"a": decl("a")}, {"name": ns, "namespace": ns}, creds)[2]:
            kube.objs[("secrets", ns, s["metadata"]["name"])] = s
    kube.objs[("secrets", rg.NAMESPACE, "a-pull")] = {"metadata": {"namespace": rg.NAMESPACE,
                                                                 "labels": {rg.LABEL: "a", cc.GENERATED: "true"}}}
    assert rg.child_users(kube, "a") == ["api", "web"]
    assert rg.child_users(kube, "b") == []


# ---------------------------------------------------------------------------
# Modèles de cluster et entrées de plan
# ---------------------------------------------------------------------------
def tmpl(**spec):
    base = {"k8s_version": "v1.34.11", "image": "default/leap", "ssh_keypair": "default/ju",
            "network": "default/production", "gateway": "172.16.0.1", "subnet_mask": "255.255.0.0",
            "ip_pool": "capi-vm-pool", "dns": ["172.16.3.6"]}
    base.update(spec)
    return {"description": "t", "variables": ["registre"], "spec": base}


def test_a_template_takes_a_registry_variable_rendered_at_launch():
    t = dp.check_capi_template(tmpl(registries=["{{registre}}", {"registry": "client", "no_fallback": False}]))
    t["name"] = "rke2"
    spec = dp.render_capi_template(t, {"registre": "amorcage"}, "web")
    assert spec["registries"] == [{"registry": "amorcage"}, {"registry": "client", "no_fallback": False}]


@pytest.mark.parametrize("regs,where", [
    ([{"registry": "a", "password": "x"}], "spec.registries"),
    ("amorcage", "spec.registries"),
    (["{{autre}}"], "{{autre}}"),
    (["Bad_Name"], "spec.registries"),
])
def test_a_bad_template_registry_list_is_refused(regs, where):
    with pytest.raises(dp.ProfileError) as e:
        dp.check_capi_template(tmpl(registries=regs))
    assert where in [w for w, _ in e.value.errors]


def test_a_plan_entry_carries_its_own_registries():
    plan = {"variables": ["registre"], "phases": {"clusters": {"enabled": True, "items": [
        {"template": "rke2", "name": "{{cluster}}-a", "registries": ["{{registre}}"]},
        {"template": "rke2", "name": "{{cluster}}-b"}]}}}
    out = dp.check_plan(plan)
    items = out["phases"]["clusters"]["items"]
    assert items[0]["registries"] == ["{{registre}}"] and "registries" not in items[1]
    assert dp.render_registries(items[0]["registries"], {"registre": "amorcage"}, "w") == [{"registry": "amorcage"}]
    with pytest.raises(dp.ProfileError, match="missing variable"):
        dp.render_registries(items[0]["registries"], {}, "w")


@pytest.mark.parametrize("regs,why", [
    (["{{inconnue}}"], "undeclared variable"),
    ([{"registry": "a", "extra": 1}], "a list of registry names"),
])
def test_a_bad_plan_entry_registry_list_is_refused(regs, why):
    plan = {"variables": [], "phases": {"clusters": {"enabled": True, "items": [
        {"template": "rke2", "name": "a", "registries": regs}]}}}
    with pytest.raises(dp.ProfileError) as e:
        dp.check_plan(plan)
    assert any(why in r for _, r in e.value.errors)
