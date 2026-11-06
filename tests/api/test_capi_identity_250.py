"""2.5.0 (décisions 126, 164 à 167) : une identité CAPHV restreinte par
cluster enfant, à la place du kubeconfig `system:masters` de la console.

Les règles sont relevées appel par appel dans la source de CAPHV v0.10.1 ;
le tableau attendu ci-dessous est écrit à la main, indépendamment de
`capi_identity.RULES`, pour qu'un droit retiré ou ajouté se voie.
"""

import argparse
import base64
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import capi_cluster as cc  # noqa: E402
import capi_identity as ci  # noqa: E402
from kube import KubeError  # noqa: E402

V = "v0.10.1"


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("harvester_capi_250", ROOT / "bin" / "harvester-capi.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def demand(**kw):
    base = {"name": "web", "k8s_version": "v1.34.11", "image": "default/image-abc",
            "ssh_keypair": "default/ju", "network": "default/production",
            "gateway": "172.16.0.1", "subnet_mask": "255.255.0.0", "ip_pool": "capi-vm-pool",
            "dns": ["172.16.3.6"]}
    base.update(kw)
    return ci.force_target_namespace(cc.normalize(base), "restricted")


# ---------------------------------------------------------------------------
# rfc1035_name : cas relevés de GenerateRFC1035Name (util/util.go:190),
# valeurs attendues produites par la fonction Go recopiée telle quelle
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("parts,expected", [
    (["web", "web", "ippool"], "web-web-ippool"),
    (["Web", "Prod", "ippool"], "web-prod-ippool"),            # minuscules
    (["my.ns", "c1", "ippool"], "my-ns-c1-ippool"),            # '.' remplacé
    (["ns_a", "B"], "ns-a-b"),                                 # '_' remplacé
    (["1ns", "c", "ippool"], "a-1ns-c-ippool"),                # chiffre en tête
    (["-x-", "y"], "x--y"),                                    # tirets de bord retirés seulement
    (["é", "x"], "x"),                                         # caractère non ASCII remplacé
    (["a" * 35, "b" * 35], ("a" * 35 + "-" + "b" * 35)[:63]),  # 71 caractères coupés à 63
    ([], ""),
])
def test_rfc1035_name_matches_the_caphv_source(parts, expected):
    got = ci.rfc1035_name(parts)
    assert got == expected and len(got) <= 63


# ---------------------------------------------------------------------------
# Règles : tableau attendu (source v0.10.1)
# ---------------------------------------------------------------------------

# (namespace ou None, groupe, ressource, verbes, noms)
# Le namespace des VMs est `web-vms` (pas `web`) : CAPHV y crée `web-token`
# pour le fournisseur de cloud, le nom du jeton d'amorçage de CAPRKE2 dans `web`.
EXPECTED = [
    ("web-vms", "kubevirt.io", "virtualmachines", ["get", "create", "delete"], None),
    ("web-vms", "kubevirt.io", "virtualmachineinstances", ["get"], None),
    ("web-vms", "", "secrets", ["get", "create", "update", "delete"], None),
    ("web-vms", "", "services", ["get", "create", "update", "delete"], None),
    ("web-vms", "", "serviceaccounts", ["get"], ["web"]),
    ("web-vms", "", "persistentvolumeclaims", ["list", "delete"], None),
    ("web-vms", "loadbalancer.harvesterhci.io", "loadbalancers", ["get", "create", "delete"], None),
    ("default", "harvesterhci.io", "virtualmachineimages", ["list"], None),
    ("default", "harvesterhci.io", "keypairs", ["get"], ["ju"]),
    ("harvester-system", "apps", "deployments", ["get"], ["harvester"]),
    ("kube-system", "", "services", ["get"], ["ingress-expose"]),
    (None, "", "namespaces", ["get"], ["web-vms"]),
    (None, "loadbalancer.harvesterhci.io", "ippools", ["get", "update"], ["capi-vm-pool"]),
    (None, "loadbalancer.harvesterhci.io", "ippools", ["delete"], ["web-web-ippool"]),
    (None, "", "nodes", ["list"], None),
    (None, "rbac.authorization.k8s.io", "clusterrolebindings", ["get"], ["web"]),
]


def test_every_grant_matches_the_source_table():
    got = [(n, g, r, v, names) for n, g, r, v, names, _src in ci.grants(demand(), V)]
    assert got == EXPECTED


def test_every_rule_cites_its_source_lines():
    for r in ci.RULES[V]:
        assert r["src"] and all(".go:" in s for s in r["src"]), r


def _all_role_rules(docs):
    return [(d["kind"], d["metadata"].get("namespace"), rule)
            for d in docs if d["kind"] in ("Role", "ClusterRole") for rule in d["rules"]]


@pytest.mark.parametrize("kw", [{}, {"image": "images/leap", "ssh_keypair": "keys/ju"},
                                {"ip_pool_refs": ["p1", "p2", "p3"]}])
def test_no_wildcard_no_bind_no_rolebinding_creation(kw):
    for _kind, _ns, rule in _all_role_rules(ci.docs(demand(**kw), V)):
        flat = rule["apiGroups"] + rule["resources"] + rule["verbs"] + rule.get("resourceNames", [])
        assert "*" not in flat
        assert "bind" not in rule["verbs"] and "escalate" not in rule["verbs"] \
            and "impersonate" not in rule["verbs"]
        if rule["resources"] in (["clusterrolebindings"], ["rolebindings"], ["roles"], ["clusterroles"]):
            assert rule["verbs"] == ["get"], rule


def test_the_generated_pool_delete_right_is_granted_by_name():
    rules = [r for _k, _n, r in _all_role_rules(ci.docs(demand(), V)) if r["resources"] == ["ippools"]]
    deletes = [r for r in rules if "delete" in r["verbs"]]
    assert deletes == [{"apiGroups": ["loadbalancer.harvesterhci.io"], "resources": ["ippools"],
                        "verbs": ["delete"], "resourceNames": ["web-web-ippool"]}]


# ---------------------------------------------------------------------------
# Objets posés
# ---------------------------------------------------------------------------

def _index(docs):
    return {(d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"]): d for d in docs}


def test_minimal_demand_objects_and_labels():
    docs = ci.docs(demand(), V)
    idx = _index(docs)
    assert set(idx) == {
        ("Namespace", None, "web-vms"),
        ("ServiceAccount", "web", "caphv-identity"), ("Secret", "web", "caphv-identity-token"),
        ("Role", "web-vms", "caphv-identity-web"), ("RoleBinding", "web-vms", "caphv-identity-web"),
        ("Role", "default", "caphv-identity-web"), ("RoleBinding", "default", "caphv-identity-web"),
        ("Role", "harvester-system", "caphv-identity-web"),
        ("RoleBinding", "harvester-system", "caphv-identity-web"),
        ("Role", "kube-system", "caphv-identity-web"), ("RoleBinding", "kube-system", "caphv-identity-web"),
        ("ClusterRole", None, "harvester-ops-caphv-web"), ("ClusterRoleBinding", None, "harvester-ops-caphv-web"),
        ("ServiceAccount", "web-vms", "web"), ("ClusterRoleBinding", None, "web"),
    }
    assert docs[0]["kind"] == "Namespace", "the VM namespace comes before the roles it holds"
    for d in docs:
        want = {ci.GENERATED: "true", ci.L_CLUSTER: "web", ci.L_NAMESPACE: "web"}
        if d["kind"] == "Namespace":
            want["harvester-ops.io/managed"] = "true"
        assert d["metadata"]["labels"] == want
    tok = idx[("Secret", "web", "caphv-identity-token")]
    assert tok["type"] == "kubernetes.io/service-account-token"
    assert tok["metadata"]["annotations"] == {"kubernetes.io/service-account.name": "caphv-identity"}
    cp = idx[("ClusterRoleBinding", None, "web")]
    assert cp["roleRef"]["name"] == "harvesterhci.io:cloudprovider"
    assert cp["subjects"] == [{"kind": "ServiceAccount", "name": "web", "namespace": "web-vms"}]
    for k in [k for k in idx if k[0] in ("RoleBinding", "ClusterRoleBinding") and k[2] != "web"]:
        assert idx[k]["subjects"] == [{"kind": "ServiceAccount", "name": "caphv-identity", "namespace": "web"}]
    # rien n'est accordé dans le namespace du cluster, qui porte les secrets
    # de CAPRKE2 (jeton d'amorçage, kubeconfig du cluster)
    assert not [d for d in docs if d["kind"] == "Role" and d["metadata"]["namespace"] == "web"]


@pytest.mark.parametrize("image,key,side", [
    ("default/img", "default/ju", {"default": ["virtualmachineimages", "keypairs"]}),
    ("images/img", "keys/ju", {"images": ["virtualmachineimages"], "keys": ["keypairs"]}),
    ("web-vms/img", "web-vms/ju", {}),   # dans le namespace des VMs : rôle de ce namespace
])
def test_image_and_key_namespaces(image, key, side):
    docs = ci.docs(demand(image=image, ssh_keypair=key), V)
    roles = {(d["metadata"]["namespace"]): [r["resources"][0] for r in d["rules"]]
             for d in docs if d["kind"] == "Role" and d["metadata"]["name"] == "caphv-identity-web"}
    roles.pop("harvester-system")
    roles.pop("kube-system")
    in_vms = roles.pop("web-vms")
    assert roles == side
    assert ("virtualmachineimages" in in_vms) == (image.startswith("web-vms/"))


def test_three_pools_are_named_and_the_first_is_not_doubled():
    g = [x for x in ci.grants(demand(ip_pool="p1", ip_pool_refs=["p1", "p2", "p3"]), V)
         if x[2] == "ippools" and "update" in x[3]]
    assert g[0][4] == ["p1", "p2", "p3"]


def test_unknown_version_is_refused():
    with pytest.raises(ValueError, match="identity-rules-unknown"):
        ci.docs(demand(), "v0.11.0")
    f = ci.findings(demand(), "v0.11.0", "restricted")
    assert [x["code"] for x in f] == ["identity-rules-unknown"] and f[0]["level"] == "block"
    assert ci.findings(demand(), "v0.11.0", "console") == []


# ---------------------------------------------------------------------------
# Constats
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("given,forced,blocked", [
    (None, "web-vms", False),        # valeur par défaut remplacée
    ("default", "web-vms", False),
    ("web-vms", "web-vms", False),
    ("web", "web", True),            # le namespace du cluster : refusé (jeton `web-token` de CAPRKE2)
    ("other", "other", True),        # choix explicite d'un autre namespace : refusé
])
def test_target_namespace_is_forced(given, forced, blocked):
    raw = {"name": "web", "target_namespace": given} if given else {"name": "web"}
    spec = ci.force_target_namespace(cc.normalize(raw), "restricted")
    assert spec["target_namespace"] == forced
    codes = [f["code"] for f in ci.findings(spec, V, "restricted")]
    assert ("target-namespace-forced" in codes) == blocked


def test_console_identity_keeps_the_target_namespace():
    spec = ci.force_target_namespace(cc.normalize({"name": "web"}), "console")
    assert spec["target_namespace"] == "default"


@pytest.mark.parametrize("labels,blocked", [
    (None, False),
    ({ci.L_CLUSTER: "web", ci.L_NAMESPACE: "web"}, False),        # la nôtre (reprise)
    ({ci.L_CLUSTER: "web", ci.L_NAMESPACE: "other"}, True),       # cluster voisin du même nom
    ({}, True),                                                   # posée par CAPHV pour un ancien cluster
])
def test_cloud_provider_binding_taken(labels, blocked):
    b = {} if labels is None else {"web": labels}
    codes = [f["code"] for f in ci.findings(demand(), V, "restricted", b)]
    assert ("cloud-provider-binding-taken" in codes) == blocked


@pytest.mark.parametrize("stack,desc,expected", [
    ({"providers": [{"type": "infrastructure", "name": "harvester", "version": "v0.10.1"}]}, None, "v0.10.1"),
    (None, {"providers": [{"type": "infrastructure", "name": "harvester", "version": "v0.10.1"}]}, "v0.10.1"),
    ({"providers": [{"type": "infrastructure", "name": "harvester", "version": "v0.9.0"}]},
     {"providers": [{"type": "infrastructure", "name": "harvester", "version": "v0.10.1"}]}, "v0.9.0"),
    ({"providers": [{"type": "bootstrap", "name": "rke2", "version": "v0.25.2"}]}, None, None),
])
def test_caphv_version_prefers_the_installed_controller(stack, desc, expected):
    assert ci.caphv_version(stack, desc) == expected


def test_access_reviews_cover_objects_rules_and_bind():
    rv = ci.access_reviews(demand(), V)
    assert ("web-vms", "create", "rbac.authorization.k8s.io", "roles", None) in rv
    assert (None, "delete", "", "namespaces", "web-vms") in rv
    assert (None, "create", "rbac.authorization.k8s.io", "clusterrolebindings", None) in rv
    assert (None, "delete", "loadbalancer.harvesterhci.io", "ippools", "web-web-ippool") in rv
    assert (None, "bind", "rbac.authorization.k8s.io", "clusterroles", "harvesterhci.io:cloudprovider") in rv
    assert len(rv) == len(set(rv))


# ---------------------------------------------------------------------------
# Kubeconfig
# ---------------------------------------------------------------------------

CA = base64.b64encode(b"-----BEGIN CERTIFICATE-----x").decode()


def test_kubeconfig_has_one_token_user_and_the_harvester_address():
    k = ci.kubeconfig("https://172.16.3.100:6443", CA, "tok.en-1")
    assert "server: https://172.16.3.100:6443" in k
    assert k.count("token: tok.en-1") == 1
    assert "rancher" not in k and "/k8s/clusters" not in k


@pytest.mark.parametrize("server,ca,token", [
    ("https://rancher.home.zypp.fr/k8s/clusters/c-x", CA, "t"),     # relais Rancher refusé
    ("http://172.16.3.100:6443", CA, "t"),
    ("https://172.16.3.100:6443", "not base64!", "t"),
    ("https://172.16.3.100:6443", CA, ""),
    ("https://172.16.3.100:6443", CA, "a b"),
])
def test_kubeconfig_refusals(server, ca, token):
    with pytest.raises(ValueError):
        ci.kubeconfig(server, ca, token)


# ---------------------------------------------------------------------------
# Rendu : nom du HarvesterCluster et origine
# ---------------------------------------------------------------------------

def test_postprocess_names_the_harvester_cluster_and_sets_the_origin():
    docs = [{"apiVersion": "cluster.x-k8s.io/v1beta1", "kind": "ClusterClass",
             "metadata": {"name": "harvester-rke2", "namespace": "web"}, "spec": {}},
            {"apiVersion": "cluster.x-k8s.io/v1beta1", "kind": "Cluster",
             "metadata": {"name": "web", "namespace": "web", "labels": {}}, "spec": {"topology": {}}}]
    out = cc.postprocess(docs, demand(), {"plan": "p1", "template": "rke2-petit", "run": "a" * 12})
    klass = next(d for d in out if d["kind"] == "ClusterClass")
    assert klass["spec"]["infrastructureNamingStrategy"] == {"template": "{{ .cluster.name }}"}
    c = next(d for d in out if d["kind"] == "Cluster")
    assert c["metadata"]["labels"]["harvester-ops.io/plan"] == "p1"
    assert c["metadata"]["labels"]["harvester-ops.io/plan-template"] == "rke2-petit"
    assert c["metadata"]["annotations"]["harvester-ops.io/plan-run"] == "a" * 12
    plain = next(d for d in cc.postprocess(docs, demand()) if d["kind"] == "Cluster")
    assert "harvester-ops.io/plan" not in plain["metadata"]["labels"] and "annotations" not in plain["metadata"]


@pytest.mark.parametrize("text,expected", [
    ("", {}),
    ("plan=p1,template=rke2-petit,run=0123456789ab", {"plan": "p1", "template": "rke2-petit", "run": "0123456789ab"}),
    ("plan=p1", {"plan": "p1"}),
])
def test_parse_origin(text, expected):
    assert cc.parse_origin(text) == expected


@pytest.mark.parametrize("text", ["owner=x", "plan", "plan=P1", "plan=a_b", "run=xyz", "run=0123456789AB",
                                  "plan=p,plan=q", "plan=" + "a" * 64])
def test_parse_origin_refusals(text):
    with pytest.raises(ValueError):
        cc.parse_origin(text)


# ---------------------------------------------------------------------------
# harvester-capi create / delete simulés
# ---------------------------------------------------------------------------

TOKEN = "eyJhbGciOiJSUzI1NiJ9.SECRET-TOKEN-VALUE.sig"


class Kube:
    """Faux kubectl : objets {(kind, ns, name): obj}, sélecteurs à plusieurs
    étiquettes, ns None = tous les namespaces (comme `-A`)."""

    def __init__(self, objs=None, token=True, vip="172.16.3.100"):
        self.objs = dict(objs or {})
        self.calls = []
        self.token = token
        self.vip = vip

    @staticmethod
    def _kind(d):
        return {"ServiceAccount": "serviceaccounts", "Secret": "secrets",
                "Role": "roles.rbac.authorization.k8s.io",
                "RoleBinding": "rolebindings.rbac.authorization.k8s.io",
                "ClusterRole": "clusterroles.rbac.authorization.k8s.io",
                "ClusterRoleBinding": "clusterrolebindings.rbac.authorization.k8s.io",
                "Namespace": "namespaces", "Cluster": "clusters.cluster.x-k8s.io"}.get(d["kind"], d["kind"])

    def apply(self, docs, timeout=None, field_manager=None):
        for d in docs:
            md = d["metadata"]
            self.calls.append(("apply", self._kind(d), md.get("namespace"), md["name"]))
            self.objs[(self._kind(d), md.get("namespace"), md["name"])] = json.loads(json.dumps(d))

    def get(self, kind, ns, name):
        self.calls.append(("get", kind, ns, name))
        if kind == "services" and name == "rke2-traefik":
            return {"status": {"loadBalancer": {"ingress": [{"ip": self.vip}] if self.vip else []}}}
        o = self.objs.get((kind, ns, name))
        if o is not None and kind == "secrets" and name == ci.TOKEN_SECRET and self.token:
            o = dict(o, data={"token": base64.b64encode(TOKEN.encode()).decode(), "ca.crt": CA})
        return o

    def list(self, kind, ns=None, selector=None):
        want = dict(p.split("=", 1) for p in (selector or "").split(",") if p)
        out = []
        for (k, s, n), o in self.objs.items():
            if k != kind or (ns is not None and s != ns):
                continue
            lab = (o.get("metadata") or {}).get("labels") or {}
            if all(lab.get(a) == b for a, b in want.items()):
                out.append(o)
        return out

    def delete(self, kind, ns, name, cascade=None):
        self.calls.append(("delete", kind, ns, name))
        self.objs.pop((kind, ns, name), None)


def _args(**kw):
    a = dict(spec="-", components=None, json=False, dry_run=False, timeout=60, no_wait=True,
             identity="restricted", origin="", cluster=None, kubeconfig=None)
    a.update(kw)
    return argparse.Namespace(**a)


@pytest.fixture
def sim(cli, monkeypatch, tmp_path):
    monkeypatch.setenv("HARVESTER_OPS_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "spec_from", lambda p: cc.normalize(
        {"name": "web", "k8s_version": "v1.34.11", "image": "default/image-abc", "ssh_keypair": "default/ju",
         "network": "default/production", "gateway": "172.16.0.1", "subnet_mask": "255.255.0.0",
         "ip_pool": "capi-vm-pool", "dns": ["172.16.3.6"]}))
    monkeypatch.setattr(cli, "components_from", lambda p: None)
    monkeypatch.setattr(cli, "_check", lambda kube, spec, desc, identity="console", harvester=None: {
        "findings": [], "blocked": False, "amount": {"vms": 2, "addresses": 3}, "spec": spec,
        "identity": identity, "caphv_version": V})
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    seen = {}

    def render(spec, kc, origin=None, registries=None):
        p = Path(kc)
        seen["path"] = p
        seen["mode"] = stat.S_IMODE(p.stat().st_mode)
        seen["content"] = p.read_text()
        seen["spec"] = spec
        seen["origin"] = origin
        if seen.get("fail"):
            raise RuntimeError("caphv-generate failed: boom")
        return [{"apiVersion": "cluster.x-k8s.io/v1beta1", "kind": "Cluster",
                 "metadata": {"name": "web", "namespace": "web"}}]
    monkeypatch.setattr(cli, "render", render)
    return seen


def _create(cli, kube, monkeypatch, **kw):
    monkeypatch.setattr(cli, "kube_from", lambda a: (kube, "/console/kubeconfig", None))
    return cli.cmd_create(_args(**kw))


def test_create_sets_up_the_identity_before_rendering_and_erases_the_file(cli, sim, monkeypatch, capsys, tmp_path):
    kube = Kube()
    assert _create(cli, kube, monkeypatch, origin="plan=p1,template=rke2-petit") == cli.EXIT_OK
    assert sim["mode"] == 0o600 and str(sim["path"]).startswith(str(tmp_path))
    assert not sim["path"].exists(), "kubeconfig left on disk"
    assert f"token: {TOKEN}" in sim["content"] and "https://172.16.3.100:6443" in sim["content"]
    assert "/console/kubeconfig" not in sim["content"]
    assert sim["spec"]["target_namespace"] == "web-vms"
    assert sim["origin"] == {"plan": "p1", "template": "rke2-petit"}
    applied = [c for c in kube.calls if c[0] == "apply"]
    assert applied[0] == ("apply", "namespaces", None, "web")
    first_cluster = applied.index(("apply", "clusters.cluster.x-k8s.io", "web", "web"))
    assert all(c[1] != "clusters.cluster.x-k8s.io" for c in applied[:first_cluster])
    assert ("apply", "clusterrolebindings.rbac.authorization.k8s.io", None, "web") in applied
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err and str(tmp_path) not in out.out + out.err


def test_console_identity_renders_with_the_console_kubeconfig(cli, sim, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "render", lambda spec, kc, origin=None, registries=None: seen.update(kc=kc, spec=spec) or [])
    kube = Kube()
    assert _create(cli, kube, monkeypatch, identity="console") == cli.EXIT_OK
    assert seen["kc"] == "/console/kubeconfig" and seen["spec"]["target_namespace"] == "default"
    assert not [c for c in kube.calls if c[0] == "apply" and "rbac" in c[1]]


@pytest.mark.parametrize("case", ["render-fails", "no-token", "no-vip"])
def test_a_failure_before_the_manifests_removes_the_identity_and_namespace(cli, sim, monkeypatch, capsys, case):
    sim["fail"] = case == "render-fails"
    kube = Kube(token=case != "no-token", vip=None if case == "no-vip" else "172.16.3.100")
    monkeypatch.setattr(cli, "TOKEN_WAIT", 0)
    assert _create(cli, kube, monkeypatch) == cli.EXIT_FAIL
    left = [k for k in kube.objs if k[0] != "clusters.cluster.x-k8s.io"]
    assert left == [], left
    if "path" in sim:
        assert not sim["path"].exists()
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err


def test_cancel_after_the_manifests_deletes_the_cluster_before_its_identity(cli, sim, monkeypatch):
    kube = Kube()

    def wait(*a, **kw):
        raise cli.Cancelled("signal 15")
    monkeypatch.setattr(cli, "wait_ready", wait)
    assert _create(cli, kube, monkeypatch, no_wait=False) == cli.EXIT_CANCELLED
    dels = [c for c in kube.calls if c[0] == "delete"]
    assert dels[0][1] == "clusters.cluster.x-k8s.io"
    assert {c[1] for c in dels[1:]} >= {"clusterrolebindings.rbac.authorization.k8s.io",
                                        "clusterroles.rbac.authorization.k8s.io", "serviceaccounts"}
    assert not sim["path"].exists()


def test_dry_run_creates_nothing_and_renders_without_a_token(cli, sim, monkeypatch, capsys):
    monkeypatch.setattr(cli, "as_yaml", lambda d: "yaml")
    kube = Kube()
    assert _create(cli, kube, monkeypatch, dry_run=True) == cli.EXIT_OK
    assert not [c for c in kube.calls if c[0] in ("apply", "delete")]
    assert "dry-run-no-token" in sim["content"] and TOKEN not in sim["content"]
    assert not sim["path"].exists()


def test_steps_never_carry_the_token(cli, sim, monkeypatch, capsys):
    kube = Kube()
    _create(cli, kube, monkeypatch)
    err = capsys.readouterr().err
    steps = [ln for ln in err.splitlines() if ln.startswith("STEP_EVENT|")]
    assert any("|identity|done|" in s for s in steps)
    assert all(TOKEN not in s and "token:" not in s for s in steps)


def _world():
    lab = lambda c, n: {ci.GENERATED: "true", ci.L_CLUSTER: c, ci.L_NAMESPACE: n}  # noqa: E731
    k = Kube()
    k.objs = {
        ("namespaces", None, "web"): {"metadata": {"name": "web", "labels": {"harvester-ops.io/managed": "true"}}},
        ("clusters.cluster.x-k8s.io", "web", "web"): {"metadata": {"name": "web"}},
    }
    for c, n in (("web", "web"), ("api", "api")):
        for d in ci.docs(demand(name=c), V):
            md = d["metadata"]
            assert {k: v for k, v in md["labels"].items() if k != "harvester-ops.io/managed"} == lab(c, n)
            k.objs[(Kube._kind(d), md.get("namespace"), md["name"])] = d
    return k


def test_delete_removes_the_cluster_first_then_its_identity_and_keeps_the_neighbour(cli, monkeypatch):
    kube = _world()
    cluster_gone_at = {}

    real_get = kube.get

    def get(kind, ns, name):
        # le Cluster disparaît au troisième relevé : l'identité doit rester
        # intacte jusque-là
        if kind == "clusters.cluster.x-k8s.io" and ("delete", kind, ns, name) in kube.calls:
            n = cluster_gone_at.setdefault("n", 0) + 1
            cluster_gone_at["n"] = n
            if n < 3:
                assert ("roles.rbac.authorization.k8s.io", "web-vms", "caphv-identity-web") in kube.objs
                return {"metadata": {"name": name}}
            return None
        return real_get(kind, ns, name)
    kube.get = get
    monkeypatch.setattr(cli, "kube_from", lambda a: (kube, None, None))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.cmd_delete(argparse.Namespace(name="web/web", timeout=600, cluster=None, kubeconfig=None)) \
        == cli.EXIT_OK
    dels = [c for c in kube.calls if c[0] == "delete"]
    assert dels[0] == ("delete", "clusters.cluster.x-k8s.io", "web", "web")
    gone = {(c[1], c[2], c[3]) for c in dels}
    assert ("clusterroles.rbac.authorization.k8s.io", None, "harvester-ops-caphv-web") in gone
    assert ("clusterrolebindings.rbac.authorization.k8s.io", None, "harvester-ops-caphv-web") in gone
    assert ("clusterrolebindings.rbac.authorization.k8s.io", None, "web") in gone
    assert ("roles.rbac.authorization.k8s.io", "kube-system", "caphv-identity-web") in gone
    assert ("namespaces", None, "web-vms") in gone, "the VM namespace goes with the identity"
    neighbour = [k for k in kube.objs if (kube.objs[k].get("metadata") or {}).get("labels", {})
                 .get(ci.L_CLUSTER) == "api"]
    assert len(neighbour) == len(ci.docs(demand(name="api"), V))


def test_a_cluster_still_deleting_keeps_its_identity(cli, monkeypatch):
    kube = _world()
    real_delete = kube.delete
    kube.delete = lambda kind, ns, name, cascade=None: (
        kube.calls.append(("delete", kind, ns, name)) if kind == "clusters.cluster.x-k8s.io"
        else real_delete(kind, ns, name))
    clock = iter(range(0, 10000, 100))
    assert cli.remove_cluster(kube, "web", "web", 150, sleep=lambda s: None, now=lambda: next(clock)) \
        == cli.EXIT_FAIL
    assert ("clusterroles.rbac.authorization.k8s.io", None, "harvester-ops-caphv-web") in kube.objs


def test_identity_choice_is_validated_by_the_cli(cli):
    with pytest.raises(SystemExit):
        cli.main(["create", "--kubeconfig", "/x", "--spec", "-", "--identity", "root"])


# ---------------------------------------------------------------------------
# Détails d'un cluster : identité restreinte ou console (décision 168)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sa,expected", [
    ({"metadata": {"labels": {ci.L_CLUSTER: "web", ci.L_NAMESPACE: "web"}}}, "restricted"),
    ({"metadata": {"labels": {ci.L_CLUSTER: "api", ci.L_NAMESPACE: "web"}}}, "console"),
    ({"metadata": {"labels": {}}}, "console"),
    (None, "console"),                     # cluster d'avant la 2.5.0
])
def test_details_tell_the_identity(monkeypatch, sa, expected):
    sys.path.insert(0, str(ROOT / "web"))
    wapp = importlib.import_module("app")

    class R:
        def __init__(self, rc, out):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def run(argv, **kw):
        if "serviceaccount" in argv:
            return R(0, json.dumps(sa)) if sa else R(1, "")
        if "machines.cluster.x-k8s.io" in argv:
            return R(0, json.dumps({"items": []}))
        return R(0, json.dumps({"metadata": {"name": "web"}, "status": {}, "spec": {}}))
    monkeypatch.setattr(wapp, "_kubectl_run", run)
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/kc")
    body = wapp.app.test_client().get("/api/capi/harv-fake/cluster/web/web/details").get_json()
    assert body["identity"] == expected


# ---------------------------------------------------------------------------
# Revue 2.5.0 (I1) : suppression de l'identité par les noms, sans liste à
# l'échelle du cluster ; échec franc si un objet reste
# ---------------------------------------------------------------------------

def _keys(d):
    return (Kube._kind(d), d["metadata"].get("namespace"), d["metadata"]["name"])


@pytest.mark.parametrize("image,key", [
    ("default/image-abc", "default/ju"),        # image et clé dans le même namespace
    ("images/leap", "keys/ju"),                 # deux namespaces annexes de plus
    ("web-vms/leap", "web-vms/ju"),             # tout dans le namespace des VMs
])
def test_teardown_names_every_object_the_identity_creates(image, key):
    spec = demand(image=image, ssh_keypair=key)
    created = {_keys(d) for d in ci.docs(spec, V)}
    side = next(d for d in ci.docs(spec, V) if d["kind"] == "ServiceAccount" and d["metadata"]["name"] == ci.IDENTITY
                )["metadata"]["annotations"][ci.SIDE_NAMESPACES].split(",")
    plan = ci.teardown("web", "web", side)
    assert {(k, n, o) for k, n, o in plan} == created
    assert plan[-1] == ("serviceaccounts", "web", ci.IDENTITY), "the account carrying the list goes last"
    assert len(plan) == len(set(plan))


def test_teardown_without_the_account_still_covers_the_fixed_namespaces():
    plan = ci.teardown("web", "web", None)
    assert ("roles.rbac.authorization.k8s.io", "kube-system", "caphv-identity-web") in plan
    assert ("roles.rbac.authorization.k8s.io", "harvester-system", "caphv-identity-web") in plan


class NoListKube(Kube):
    """Identité d'une personne sans droit de liste à l'échelle du cluster :
    toute lecture `-A` est refusée, comme sur un vrai serveur."""

    def list(self, kind, ns=None, selector=None):
        if ns is None and kind != "namespaces":
            raise RuntimeError(f"forbidden: cannot list {kind} at the cluster scope")
        return super().list(kind, ns, selector)


def _world_on(cls, image="images/leap", key="keys/ju"):
    k = cls()
    k.objs = {
        ("namespaces", None, "web"): {"metadata": {"name": "web", "labels": {"harvester-ops.io/managed": "true"}}},
    }
    for c in ("web", "api"):
        for d in ci.docs(demand(name=c, image=image, ssh_keypair=key), V):
            k.objs[_keys(d)] = json.loads(json.dumps(d))
    return k


def _identity_left(kube, name="web"):
    return sorted(k for k, o in kube.objs.items()
                  if ((o.get("metadata") or {}).get("labels") or {}).get(ci.L_CLUSTER) == name)


def test_delete_without_cluster_wide_list_removes_the_whole_identity(cli, capsys):
    kube = _world_on(NoListKube)
    assert cli.remove_cluster(kube, "web", "web", 60, sleep=lambda s: None) == cli.EXIT_OK
    assert _identity_left(kube) == []
    assert len(_identity_left(kube, "api")) == len(ci.docs(demand(name="api", image="images/leap",
                                                                 ssh_keypair="keys/ju"), V))
    err = capsys.readouterr().err
    assert "STEP_EVENT|delete|done|" in err


@pytest.mark.parametrize("fail_on", [
    ("delete", "clusterroles.rbac.authorization.k8s.io"),   # suppression refusée
    ("delete", "secrets"),                                  # le jeton resterait vivant
    ("get", "roles.rbac.authorization.k8s.io"),             # objet illisible
])
def test_an_object_kept_fails_loudly_and_a_new_delete_finishes(cli, capsys, monkeypatch, fail_on):
    kube = _world_on(NoListKube)
    verb, kind = fail_on
    real = getattr(kube, verb)

    def refuse(k, ns, name, *a, **kw):
        if k == kind and ns != "default":
            raise cli.KubeError(f"Error from server (Forbidden): {verb} {k} {name} is forbidden")
        return real(k, ns, name, *a, **kw)
    setattr(kube, verb, refuse)
    assert cli.remove_cluster(kube, "web", "web", 60, sleep=lambda s: None) == cli.EXIT_FAIL
    err = capsys.readouterr().err
    assert "STEP_EVENT|delete|error|" in err and "STEP_EVENT|delete|done|" not in err
    assert ("namespaces", None, "web") in kube.objs, "the namespace stays for a new delete"
    assert ("serviceaccounts", "web", ci.IDENTITY) in kube.objs, "the account listing the side namespaces stays"
    # droit rendu : `delete` reprend sur un Cluster déjà parti et finit
    setattr(kube, verb, real)
    monkeypatch.setattr(cli, "kube_from", lambda a: (kube, None, None))
    assert cli.cmd_delete(argparse.Namespace(name="web/web", timeout=60, cluster=None, kubeconfig=None)) \
        == cli.EXIT_OK
    assert _identity_left(kube) == [] and ("namespaces", None, "web") not in kube.objs


def test_delete_of_an_absent_cluster_without_identity_is_still_not_found(cli, monkeypatch, capsys):
    kube = Kube()
    monkeypatch.setattr(cli, "kube_from", lambda a: (kube, None, None))
    assert cli.cmd_delete(argparse.Namespace(name="web/web", timeout=60, cluster=None, kubeconfig=None)) \
        == cli.EXIT_FAIL
    assert "not found" in capsys.readouterr().err


def test_a_same_named_object_of_another_owner_is_left_alone(cli):
    kube = _world_on(NoListKube)
    # liaison `web` posée par CAPHV pour un cluster d'avant la 2.5.0 : sans
    # les étiquettes de ce cluster, elle n'est pas à lui
    kube.objs[("clusterrolebindings.rbac.authorization.k8s.io", None, "web")]["metadata"]["labels"] = {
        ci.L_CLUSTER: "web", ci.L_NAMESPACE: "other"}
    assert cli.remove_cluster(kube, "web", "web", 60, sleep=lambda s: None) == cli.EXIT_OK
    assert ("clusterrolebindings.rbac.authorization.k8s.io", None, "web") in kube.objs


def test_undo_before_the_manifests_fails_loudly_when_the_identity_stays(cli, capsys):
    kube = _world_on(NoListKube)
    real = kube.delete

    def refuse(k, ns, name, *a, **kw):
        if k == "clusterroles.rbac.authorization.k8s.io":
            raise cli.KubeError("Error from server (Forbidden): nope")
        return real(k, ns, name, *a, **kw)
    kube.delete = refuse
    cli._undo(kube, "web", "web", ns_created=True, applied=False)
    err = capsys.readouterr().err
    assert "STEP_EVENT|cancel|error|" in err
    assert ("namespaces", None, "web") in kube.objs


# ---------------------------------------------------------------------------
# Revue 2.5.0 (m2, m3, m4) : noms réservés, aucune liste de noms vide,
# un accès par nom et la suppression contrôlée d'avance
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,reserved", [
    ("caphv-identity", True),            # le compte du fournisseur de cloud deviendrait l'identité
    ("caphv-identity-token", True),
    ("caphv-identity-x", True),
    ("harvester-ops-caphv-web", True),   # la liaison <nom> prendrait le nom de celle de l'identité
    ("web", False),
    ("caphv", False),
    ("my-caphv-identity", False),
])
def test_reserved_cluster_names_are_refused(name, reserved):
    codes = [f["code"] for f in ci.findings(demand(name=name), V, "restricted")]
    assert ("identity-name-reserved" in codes) is reserved
    if reserved:
        f = next(f for f in ci.findings(demand(name=name), V, "restricted")
                 if f["code"] == "identity-name-reserved")
        assert f["level"] == "block"
    assert "identity-name-reserved" not in [f["code"] for f in ci.findings(demand(name=name), V, "console")]


@pytest.mark.parametrize("change", [
    {"ssh_keypair": "default/"},          # clé sans nom
    {"ip_pool": "", "ip_pool_refs": []},  # aucun pool
])
def test_an_empty_name_list_never_grants_every_name(change):
    spec = dict(demand(), **change)
    with pytest.raises(ValueError, match="every name"):
        ci.docs(spec, V)
    for doc in ci.docs(demand(), V):
        for rule in doc.get("rules") or []:
            assert rule.get("resourceNames", ["x"]), rule


def test_access_reviews_check_each_pool_and_the_teardown():
    spec = demand(ip_pool="p1", ip_pool_refs=["p2", "p3"])
    rv = ci.access_reviews(spec, V)
    for pool in ("p1", "p2", "p3"):
        for verb in ("get", "update"):
            assert (None, verb, "loadbalancer.harvesterhci.io", "ippools", pool) in rv
    for d in ci.docs(spec, V):
        group = d["apiVersion"].rpartition("/")[0]
        for verb in ("patch", "get", "delete"):
            assert any(r[0] == d["metadata"].get("namespace") and r[1] == verb and r[2] == group
                       and r[4] == d["metadata"]["name"] for r in rv), (verb, d["kind"], d["metadata"]["name"])
    rbac = {"serviceaccounts", "secrets", "roles", "rolebindings", "clusterroles", "clusterrolebindings"}
    assert not [r for r in rv if r[1] == "list" and r[3] in rbac], "the teardown no longer lists"


# ---------------------------------------------------------------------------
# Revue 2.5.0 (m5, m6) : le contrôle du formulaire compte les droits,
# l'aperçu impose le namespace des VMs comme la création
# ---------------------------------------------------------------------------

def test_single_check_with_restricted_identity_reviews_the_rights(cli, monkeypatch):
    inv = json.loads((ROOT / "tests" / "fixtures" / "capi" / "harv1-inventory-2026-11-05.json").read_text())
    monkeypatch.setattr(cli, "inventory", lambda kube, desc: json.loads(json.dumps(inv)))
    asked = []

    class K(Kube):
        def create_many(self, objs):
            asked.extend(objs)
            return [{"status": {"allowed": o["spec"]["resourceAttributes"]["verb"] != "bind"}} for o in objs]
    spec = demand(image="default/opensuse-leap-cloud", ssh_keypair="default/capi-ssh-key")
    out = cli._check(K(), spec, None, "restricted")
    codes = [f["code"] for f in out["findings"]]
    assert "rights-missing" in codes and out["blocked"]
    assert any(o["spec"]["resourceAttributes"].get("resource") == "clusters" for o in asked), "manifests too"
    asked.clear()
    out = cli._check(K(), dict(spec, target_namespace="default"), None, "console")
    assert not asked and "rights-missing" not in [f["code"] for f in out["findings"]]


def test_single_check_says_when_the_rights_could_not_be_read(cli, monkeypatch):
    inv = json.loads((ROOT / "tests" / "fixtures" / "capi" / "harv1-inventory-2026-11-05.json").read_text())
    monkeypatch.setattr(cli, "inventory", lambda kube, desc: json.loads(json.dumps(inv)))

    class K(Kube):
        def create_many(self, objs):
            raise cli.KubeError("forbidden")
    out = cli._check(K(), demand(image="default/opensuse-leap-cloud", ssh_keypair="default/capi-ssh-key"),
                     None, "restricted")
    assert "rights-unchecked" in [f["code"] for f in out["findings"]]


@pytest.mark.parametrize("identity,forced,dry", [("restricted", "web-vms", True), ("console", "default", False)])
def test_preview_renders_like_create(cli, monkeypatch, capsys, tmp_path, identity, forced, dry):
    monkeypatch.setenv("HARVESTER_OPS_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "kube_from", lambda a: (Kube(), "/console/kubeconfig", None))
    monkeypatch.setattr(cli, "spec_from", lambda p: cc.normalize(
        {"name": "web", "k8s_version": "v1.34.11", "image": "default/image-abc", "ssh_keypair": "default/ju",
         "network": "default/production", "gateway": "172.16.0.1", "subnet_mask": "255.255.0.0",
         "ip_pool": "capi-vm-pool", "dns": ["172.16.3.6"]}))
    seen = {}

    def render(spec, kc, origin=None, registries=None):
        seen["ns"] = spec["target_namespace"]
        seen["kc"] = Path(kc).read_text() if kc != "/console/kubeconfig" else kc
        seen["path"] = kc
        return []
    monkeypatch.setattr(cli, "render", render)
    monkeypatch.setattr(cli, "as_yaml", lambda d: "yaml")
    assert cli.cmd_render(argparse.Namespace(spec="-", show_secrets=False, identity=identity,
                                             cluster=None, kubeconfig=None)) == cli.EXIT_OK
    assert seen["ns"] == forced
    assert ("dry-run-no-token" in seen["kc"]) is dry
    if dry:
        assert not Path(seen["path"]).exists()


# ---------------------------------------------------------------------------
# Namespace des VMs propre au cluster (vu sur harvlab2 le 05/11/2026 : dans le
# namespace du cluster, CAPHV relisait le jeton d'amorçage de CAPRKE2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("labels,blocked", [
    (None, False),                                                # absent : la console le crée
    ({ci.L_CLUSTER: "web", ci.L_NAMESPACE: "web"}, False),        # le nôtre (reprise)
    ({ci.L_CLUSTER: "web", ci.L_NAMESPACE: "other"}, True),       # à un autre cluster
    ({}, True),                                                   # namespace de quelqu'un d'autre
])
def test_vm_namespace_taken(labels, blocked):
    codes = [f["code"] for f in ci.findings(demand(), V, "restricted", None, labels)]
    assert ("vm-namespace-taken" in codes) is blocked


def test_a_namespace_ending_like_a_vm_namespace_is_reserved():
    spec = demand(name="api-vms")
    assert spec["namespace"] == "api-vms"
    codes = [f["code"] for f in ci.findings(spec, V, "restricted")]
    assert "identity-name-reserved" in codes


def test_check_reads_the_vm_namespace(cli, monkeypatch):
    inv = json.loads((ROOT / "tests" / "fixtures" / "capi" / "harv1-inventory-2026-11-05.json").read_text())
    monkeypatch.setattr(cli, "inventory", lambda kube, desc: json.loads(json.dumps(inv)))

    class K(Kube):
        def create_many(self, objs):
            return [{"status": {"allowed": True}} for _ in objs]
    k = K()
    k.objs[("namespaces", None, "web-vms")] = {"metadata": {"name": "web-vms", "labels": {"team": "x"}}}
    out = cli._check(k, demand(image="default/opensuse-leap-cloud", ssh_keypair="default/capi-ssh-key"),
                     None, "restricted")
    assert "vm-namespace-taken" in [f["code"] for f in out["findings"]] and out["blocked"]


# ---------------------------------------------------------------------------
# Revue finale 2.5.0 (I1, I2) : un namespace qui existait n'est jamais
# supprimé ; un cluster d'avant la 2.5.0 se supprime sans lire le RBAC
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("broken", ["list", "get", None])
def test_a_namespace_that_existed_survives_a_failure_before_the_manifests(cli, sim, monkeypatch, broken):
    kube = Kube(token=False)
    kube.objs[("namespaces", None, "web")] = {"metadata": {"name": "web"}}
    real_list, real_get = kube.list, kube.get

    def lst(kind, ns=None, selector=None):
        if broken == "list" and kind == "namespaces":
            raise cli.KubeError("Unable to connect to the server: net/http: TLS handshake timeout")
        return real_list(kind, ns, selector)

    def get(kind, ns, name):
        if broken == "get" and kind == "namespaces":
            raise cli.KubeError("Unable to connect to the server: net/http: TLS handshake timeout")
        return real_get(kind, ns, name)
    kube.list, kube.get = lst, get
    monkeypatch.setattr(cli, "TOKEN_WAIT", 0)
    assert _create(cli, kube, monkeypatch) == cli.EXIT_FAIL
    assert ("delete", "namespaces", None, "web") not in kube.calls, "a namespace that existed was deleted"
    assert ("namespaces", None, "web") in kube.objs


class ForbiddenRbacKube(Kube):
    """Membre de projet : il supprime un Cluster de son namespace mais ne lit
    aucun objet RBAC hors de celui-ci (refus du serveur)."""

    def get(self, kind, ns, name):
        if "rbac" in kind and ns != "old":
            self.calls.append(("get", kind, ns, name))
            raise KubeError(f'Error from server (Forbidden): {kind} "{name}" is forbidden')
        return super().get(kind, ns, name)


def _old_world(cls=ForbiddenRbacKube):
    kube = cls()
    kube.objs = {
        ("namespaces", None, "old"): {"metadata": {"name": "old", "labels": {"harvester-ops.io/managed": "true"}}},
        ("secrets", "old", "old-identity"): {"metadata": {"name": "old-identity", "namespace": "old",
                                                          "labels": {"harvester-ops.io/generated": "true"}}},
    }
    return kube


def test_a_cluster_made_before_250_is_deleted_by_a_project_member(cli, capsys):
    kube = _old_world()
    assert cli.remove_cluster(kube, "old", "old", 60, sleep=lambda s: None) == cli.EXIT_OK
    err = capsys.readouterr().err
    assert "STEP_EVENT|delete|done|" in err and "kept" not in err
    assert any(c[0] == "get" and "rbac" in c[1] for c in kube.calls), "the refusals were exercised"
    assert ("delete", "secrets", "old", "old-identity") in kube.calls, \
        "the secret holding the console kubeconfig stays"
    assert ("namespaces", None, "old") not in kube.objs


def test_an_unlisted_generated_kind_does_not_keep_the_identity_secret(cli, capsys):
    kube = _old_world(Kube)
    real = kube.list

    def lst(kind, ns=None, selector=None):
        if kind.startswith("clusterclasses"):
            raise cli.KubeError("Error from server (Forbidden): clusterclasses is forbidden")
        return real(kind, ns, selector)
    kube.list = lst
    assert cli.remove_cluster(kube, "old", "old", 60, sleep=lambda s: None) == cli.EXIT_OK
    assert ("delete", "secrets", "old", "old-identity") in kube.calls
    assert "clusterclasses not listed in old" in capsys.readouterr().err


def test_a_refused_read_still_fails_while_the_identity_account_exists(cli, capsys):
    kube = _world_on(ForbiddenRbacKube)
    assert cli.remove_cluster(kube, "web", "web", 60, sleep=lambda s: None) == cli.EXIT_FAIL
    assert "STEP_EVENT|delete|error|" in capsys.readouterr().err
    assert ("serviceaccounts", "web", ci.IDENTITY) in kube.objs


@pytest.mark.parametrize("ns,blocked", [("a" * 59, False), ("a" * 60, True), ("a" * 63, True)])
def test_a_vm_namespace_longer_than_63_characters_is_refused(ns, blocked):
    """Revue finale n2 : `<namespace>-vms` au-delà de 63 caractères."""
    spec = dict(demand(), namespace=ns, target_namespace=ci.vm_namespace(ns))
    codes = [f["code"] for f in ci.findings(spec, V, "restricted")]
    assert ("vm-namespace-too-long" in codes) is blocked
    assert "vm-namespace-too-long" not in [f["code"] for f in ci.findings(spec, V, "console")]


@pytest.mark.parametrize("error, kept", [
    ("Error from server (NotFound): secrets \"web-registry-amorcage-auth\" not found", False),
    ("Error from server (Forbidden): secrets \"web-registry-amorcage-auth\" is forbidden", True),
])
def test_a_generated_object_already_gone_is_not_reported_kept(cli, capsys, error, kept):
    """2.6.0, vu sur harvlab2 : CAPI efface lui-même les objets dont le
    Cluster est le propriétaire (compléments, Secrets des registres) entre
    la liste et la suppression ; « kept: NotFound » laissait croire à un
    reste alors que l'objet est parti."""
    kube = _world_on(NoListKube)
    gen = {"metadata": {"name": "web-registry-amorcage-auth", "namespace": "web",
                        "labels": {cc.GENERATED: "true"}}}
    kube.objs[("secrets", "web", "web-registry-amorcage-auth")] = gen
    real = kube.delete

    def owner_gc_first(k, ns, name, *a, **kw):
        if name == "web-registry-amorcage-auth":
            kube.objs.pop((k, ns, name), None) if not kept else None
            raise cli.KubeError(error)
        return real(k, ns, name, *a, **kw)
    kube.delete = owner_gc_first
    assert cli.remove_cluster(kube, "web", "web", 60, sleep=lambda s: None) == cli.EXIT_OK
    err = capsys.readouterr().err
    assert ("web-registry-amorcage-auth kept" in err) is kept
    if not kept:
        assert "1 generated object(s) removed from web" in err
