"""2.8.0 (tâche 6) : `harvester-capi renew`.

Cluster de gestion simulé (gabarits, ClusterClass, machines, Secrets) ;
l'ordre des écritures est consigné. Les machines anciennes partent quand la
ClusterClass a été repointée, les nouvelles arrivent après la bascule.

Mutations jouées (rapport des tâches 5 à 8) : « gabarit modifié en place »,
« anciens gabarits supprimés avant la fin », « Secrets anciens retirés
avant la bascule ».
"""

import base64
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import capi_renew as cr  # noqa: E402
import capi_stack as cs  # noqa: E402
import child_oidc as co  # noqa: E402
import registries as rg  # noqa: E402
import test_capi_renew_280 as t  # noqa: E402

NS = CL = "web"
PULL_PW = "PULL-PW-RENEW-77"
DECLS = {"amorcage": {"name": "amorcage", "endpoint": "172.16.2.76", "ca_pem": "CA-A", "usage": ["images"],
                      "mirrors": ["docker.io"], "clusters": ["harvlab"]},
         "client": {"name": "client", "endpoint": "registry.client", "usage": ["images"],
                    "mirrors": ["docker.io", "ghcr.io"], "clusters": ["harvlab"]}}


@pytest.fixture(scope="module")
def cli():
    s = importlib.util.spec_from_file_location("harvester_capi_renew", ROOT / "bin" / "harvester-capi.py")
    mod = importlib.util.module_from_spec(s)
    s.loader.exec_module(mod)
    return mod


def b64(d):
    return {k: base64.b64encode(v.encode()).decode() for k, v in d.items()}


class Mgmt:
    def __init__(self, objs, machines):
        self.objs = dict(objs)
        self.machines = machines           # fonction : n-ième lecture -> liste
        self.reads = 0
        self.ops = []
        self.kubeconfig = "/x"

    def get(self, kind, ns, name):
        return self.objs.get((kind, ns, name))

    def list(self, kind, ns=None, selector=None):
        if kind == cr.K_MACHINE:
            self.reads += 1
            return self.machines(self.reads)
        out = [o for (k, s, _), o in self.objs.items() if k == kind and (ns is None or s == ns)]
        if selector:
            for cond in selector.split(","):
                key, _, val = cond.partition("=")
                out = [o for o in out if ((o.get("metadata") or {}).get("labels") or {}).get(key) == val]
        return out

    def apply(self, docs, timeout=None):
        for d in docs:
            kind = {"Secret": "secrets", "RKE2ControlPlaneTemplate": cr.K_CP_TEMPLATE,
                    "RKE2ConfigTemplate": cr.K_CONFIG_TEMPLATE}[d["kind"]]
            self.ops.append(("apply", kind, d["metadata"]["name"]))
            self.objs[(kind, d["metadata"].get("namespace"), d["metadata"]["name"])] = json.loads(json.dumps(d))

    def patch(self, kind, ns, name, body, dry_run=False):
        self.ops.append(("patch", kind, name, json.loads(json.dumps(body))))

        def merge(dst, src):
            for k, v in src.items():
                if v is None:
                    dst.pop(k, None)
                elif isinstance(v, dict) and isinstance(dst.get(k), dict):
                    merge(dst[k], v)
                else:
                    dst[k] = json.loads(json.dumps(v))
        if (kind, ns, name) in self.objs:
            merge(self.objs[(kind, ns, name)], body)

    def delete(self, kind, ns, name, **kw):
        self.ops.append(("delete", kind, name))
        self.objs.pop((kind, ns, name), None)


def world(registries=("amorcage",), oidc=None, machines=None, pool_available=4, old_form=False):
    doc, files, secs = rg.rke2_config(list(registries), DECLS, {"name": CL, "namespace": NS},
                                      {r: {"username": "pull", "password": PULL_PW} for r in registries})
    cp = t.cp_template(files=json.loads(json.dumps(files)) if registries else None)
    cfg = t.cfg_template(files=json.loads(json.dumps(files)) if registries else None)
    ann = {co.ANNOTATION: co.public(co.check(oidc, "v1.34.11")[0])} if oidc else {}
    if oidc:
        cp = t.cc.renew_templates([cp], {"k8s_version": "v1.34.11", "oidc": oidc}, ["oidc"])[0]
    cluster = {"metadata": {"name": CL, "namespace": NS, "annotations": ann},
               "spec": {"topology": {"classRef": {"name": "harvester-rke2"}, "version": "v1.34.11+rke2r1",
                                     "variables": [{"name": "ipPoolRef", "value": "capi-vm-pool"},
                                                   {"name": "cpuCount", "value": 2},
                                                   {"name": "memorySize", "value": "4Gi"}]}}}
    labels = {} if old_form else {rg.used_label(r): "used" for r in registries}
    nodes = next((s for s in secs if s["metadata"]["name"] == "web-registries"), None)
    objs = {(cs.K_CLUSTER, NS, CL): cluster,
            (cr.K_CLASS, NS, "harvester-rke2"): t.cluster_class(),
            (cr.K_CP_TEMPLATE, NS, cp["metadata"]["name"]): cp,
            (cr.K_CONFIG_TEMPLATE, NS, cfg["metadata"]["name"]): cfg,
            ("ippools.loadbalancer.harvesterhci.io", None, "capi-vm-pool"): {
                "metadata": {"name": "capi-vm-pool"}, "spec": {"ranges": [{"subnet": "172.16.0.0/16",
                                                                         "rangeStart": "172.16.2.200",
                                                                         "rangeEnd": "172.16.2.203"}]},
                "status": {"total": 4, "allocated": {f"172.16.2.20{i}": "x" for i in range(4 - pool_available)}}}}
    if nodes:
        nodes = dict(nodes, data=b64(nodes.pop("stringData")))
        nodes["metadata"]["labels"].update(labels)
        objs[("secrets", NS, "web-registries")] = nodes
    for r in registries if old_form else ():
        objs[("secrets", NS, f"web-registry-{r}-auth")] = {"metadata": {
            "name": f"web-registry-{r}-auth", "namespace": NS, "labels": {rg.LABEL: r, t.cc.GENERATED: "true"}}}
    for r in registries:
        if DECLS[r].get("ca_pem"):
            objs[("secrets", NS, f"web-registry-{r}-ca")] = {"metadata": {
                "name": f"web-registry-{r}-ca", "namespace": NS, "labels": {rg.LABEL: r, t.cc.GENERATED: "true"}}}
    return Mgmt(objs, machines or rolling())


def rolling(polls_until_done=2):
    """Lecture 1 : contrôle (anciennes) ; puis anciennes et nouvelles ;
    enfin nouvelles prêtes seulement."""
    def machines(n):
        if n == 1:
            return [t.machine("cp-a", t.OLD), t.machine("w-a", t.OLD)]
        if n <= polls_until_done:
            return [t.machine("cp-a", t.OLD), t.machine("cp-b", t.NEW, ready=False)]
        return [t.machine("cp-b", t.NEW), t.machine("w-b", t.NEW)]
    return machines


@pytest.fixture
def run(cli, tmp_path, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(cli, "time", types.SimpleNamespace(time=lambda: clock["t"],
                                                           sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))
    monkeypatch.setattr(cli, "_registry_decls", lambda spec: {e["registry"]: DECLS[e["registry"]]
                                                               for e in spec.get("registries") or []})
    monkeypatch.setattr(cli.rg, "pull_creds_for", lambda kube, names: {n: {"username": "pull", "password": PULL_PW}
                                                                       for n in names})
    monkeypatch.setattr(cli.rg, "load", lambda st, n: DECLS.get(n))
    monkeypatch.setattr(cli, "registry_findings", lambda kube, spec, h: [])
    monkeypatch.setattr(cli, "_free_capacity", lambda kube: (None, None))
    monkeypatch.setattr(cli, "_overcommit", lambda kube: {})

    def go(mgmt, what, spec=None, extra=()):
        argv = ["renew", "--kubeconfig", "/x", "--harvester-name", "harvlab", "--name", f"{NS}/{CL}",
                "--what", what, "--timeout", "600"] + list(extra)
        if spec is not None:
            p = tmp_path / "renew.json"
            p.write_text(json.dumps(spec))
            argv += ["--spec", str(p)]
        monkeypatch.setattr(cli, "kube_from", lambda a: (mgmt, "/x", None))
        return cli.main(argv)
    return go


def events(capsys):
    cap = capsys.readouterr()
    return [ln.split("|", 3)[1:] for ln in cap.err.splitlines() if ln.startswith("STEP_EVENT|")], cap


def test_registries_renewal_order_new_secrets_templates_class_then_cleanup(run, capsys):
    """Mutations « gabarit modifié en place », « Secrets anciens retirés avant
    la bascule », « anciens gabarits supprimés avant la fin »."""
    mgmt = world(registries=("amorcage",))
    assert run(mgmt, "registries", {"registries": ["amorcage", "client"]}) == 0
    ops = [o[:3] for o in mgmt.ops]
    i_sec = ops.index(("apply", "secrets", "web-registries-r1"))
    i_cp = ops.index(("apply", cr.K_CP_TEMPLATE, "harvester-rke2-control-plane-r1"))
    i_cfg = ops.index(("apply", cr.K_CONFIG_TEMPLATE, "harvester-rke2-worker-r1"))
    i_cls = next(i for i, o in enumerate(ops) if o[0] == "patch" and o[1] == cr.K_CLASS)
    i_del = [i for i, o in enumerate(ops) if o[0] == "delete"]
    assert i_sec < i_cp < i_cfg < i_cls < min(i_del)
    # jamais d'écriture sur les gabarits d'origine (modifiés en place : non)
    assert ("apply", cr.K_CP_TEMPLATE, "harvester-rke2-control-plane") not in ops
    assert {o[2] for o in mgmt.ops if o[0] == "delete"} == {"harvester-rke2-control-plane", "harvester-rke2-worker",
                                                            "web-registries"}
    patch = next(o[3] for o in mgmt.ops if o[0] == "patch" and o[1] == cr.K_CLASS)
    assert [x["name"] for _r, x in cr.template_refs({"spec": patch["spec"]})] == [
        "harvester-rke2-control-plane-r1", "harvester-rke2-worker-r1"]
    new = mgmt.objs[(cr.K_CP_TEMPLATE, NS, "harvester-rke2-control-plane-r1")]
    files = new["spec"]["template"]["spec"]["files"]
    assert files[0]["contentFrom"]["secret"]["name"] == "web-registries-r1"
    evs, cap = events(capsys)
    assert PULL_PW not in cap.err + cap.out
    assert [e[0] for e in evs if e[1] == "done"] == ["check", "secrets", "templates", "class", "wait", "done"]


def test_a_second_renewal_takes_revision_two(run, capsys):
    mgmt = world()
    assert run(mgmt, "registries", {"registries": ["amorcage", "client"]}) == 0
    mgmt.reads = 0
    # machines de la révision 1 : créées après sa bascule, pas « occupé »
    mgmt.machines = lambda n: [t.machine("cp-b", t.NEW), t.machine("w-b", t.NEW)]
    assert run(mgmt, "registries", {"registries": ["client"]}) == 0
    names = {o[2] for o in mgmt.ops if o[0] == "apply"}
    assert "harvester-rke2-control-plane-r2" in names and "web-registries-r2" in names
    assert ("delete", cr.K_CP_TEMPLATE, "harvester-rke2-control-plane-r1") in [o[:3] for o in mgmt.ops]
    # la CA du registre sorti de la liste part avec la bascule
    assert ("delete", "secrets", "web-registry-amorcage-ca") in [o[:3] for o in mgmt.ops]


def test_timeout_removes_nothing(run, capsys):
    mgmt = world(machines=rolling(polls_until_done=10 ** 6))
    assert run(mgmt, "registries", {"registries": ["amorcage", "client"]}) == 1
    assert not [o for o in mgmt.ops if o[0] == "delete"]
    assert "the old templates and secrets are kept" in capsys.readouterr().err


def test_nothing_to_do_is_refused(run, capsys):
    mgmt = world()
    assert run(mgmt, "registries", {"registries": ["amorcage"]}) == 2
    assert "renew-nothing" in capsys.readouterr().err and mgmt.ops == []


def test_ca_renewal_follows_the_secret_content(run, capsys, monkeypatch):
    """CA en transition (ancienne et nouvelle) : seul le contenu du Secret
    des nœuds change, le renouvellement a lieu."""
    mgmt = world()
    monkeypatch.setitem(DECLS, "amorcage", dict(DECLS["amorcage"], ca_pem="CA-A\nCA-B"))
    assert run(mgmt, "ca", extra=["--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["secrets_changed"] is True and out["revision"] == 1 and mgmt.ops == []


def test_dry_run_writes_nothing(run, capsys):
    mgmt = world()
    assert run(mgmt, "registries", {"registries": ["amorcage", "client"]}, ["--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    # un registre sans CA de plus : seul le Secret des nœuds change
    assert out["changes"] == [] and out["secrets_changed"] is True and mgmt.ops == []


def test_a_renewal_in_progress_is_refused(run, capsys):
    mgmt = world()
    tpl = cr.rotated([mgmt.objs[(cr.K_CP_TEMPLATE, NS, "harvester-rke2-control-plane")]], 1, now=1700000000)[0]
    mgmt.objs[(cr.K_CP_TEMPLATE, NS, tpl["metadata"]["name"])] = tpl
    cls = mgmt.objs[(cr.K_CLASS, NS, "harvester-rke2")]
    cls["spec"]["controlPlane"]["templateRef"]["name"] = tpl["metadata"]["name"]
    assert run(mgmt, "registries", {"registries": ["client"]}) == 2
    assert "renew-busy" in capsys.readouterr().err and mgmt.ops == []


def test_no_address_for_the_extra_machine_is_refused(run, capsys):
    mgmt = world(pool_available=0)
    assert run(mgmt, "registries", {"registries": ["amorcage", "client"]}) == 2
    assert "renew-pool-short" in capsys.readouterr().err and mgmt.ops == []


def test_oidc_renewal_changes_the_control_plane_and_the_annotation(run, capsys):
    mgmt = world(registries=(), oidc=t.OIDC)
    assert run(mgmt, "oidc", {"oidc": dict(t.OIDC, audience="k8s-2")}) == 0
    names = {o[2] for o in mgmt.ops if o[0] == "apply"}
    assert names == {"harvester-rke2-control-plane-r1", "harvester-rke2-worker-r1"}
    ann = next(o[3] for o in mgmt.ops if o[0] == "patch" and o[1] == cs.K_CLUSTER)
    assert json.loads(ann["metadata"]["annotations"][co.ANNOTATION])["audience"] == "k8s-2"


def test_the_old_auth_form_is_read_then_removed(run, capsys):
    """Revue finale 2.6.0 (m-14) : cluster d'avant 2.8.0, registres lus par
    les Secrets `-auth`, retirés au renouvellement."""
    mgmt = world(old_form=True)
    assert run(mgmt, "registries", {"registries": ["amorcage", "client"]}) == 0
    assert ("delete", "secrets", "web-registry-amorcage-auth") in [o[:3] for o in mgmt.ops]
    assert run(world(old_form=True), "ca", extra=["--dry-run"]) == 2          # même liste lue : rien à faire
    assert "renew-nothing" in capsys.readouterr().err
