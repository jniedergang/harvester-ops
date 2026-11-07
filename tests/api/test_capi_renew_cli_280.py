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
import os
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
    entries = list(registries)
    registries = [e["registry"] if isinstance(e, dict) else e for e in entries]
    doc, files, secs = rg.rke2_config(entries, DECLS, {"name": CL, "namespace": NS},
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
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "time", types.SimpleNamespace(time=lambda: clock["t"], monotonic=lambda: clock["t"],
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
                "--timeout", "600"] + (["--what", what] if what else []) + list(extra)
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
    # la marque du renouvellement est posée avant la bascule, retirée à la fin
    i_mark = next(i for i, o in enumerate(mgmt.ops) if o[0] == "patch" and o[1] == cs.K_CLUSTER
                  and (o[3]["metadata"]["annotations"].get(cr.A_RENEWAL) or "").startswith("{"))
    assert i_mark < i_cls
    assert cr.A_RENEWAL not in mgmt.objs[(cs.K_CLUSTER, NS, CL)]["metadata"]["annotations"]


def test_a_second_renewal_takes_revision_two(run, capsys):
    mgmt = world()
    assert run(mgmt, "registries", {"registries": ["amorcage", "client"]}) == 0
    mgmt.reads = 0
    # machines de la révision 1, remplacées à leur tour
    mgmt.machines = lambda n: ([t.machine("cp-b", t.NEW), t.machine("w-b", t.NEW)] if n == 1
                               else [t.machine("cp-c", t.NEW), t.machine("w-c", t.NEW)])
    assert run(mgmt, "registries", {"registries": ["client"]}) == 0
    names = {o[2] for o in mgmt.ops if o[0] == "apply"}
    assert "harvester-rke2-control-plane-r2" in names and "web-registries-r2" in names
    assert ("delete", cr.K_CP_TEMPLATE, "harvester-rke2-control-plane-r1") in [o[:3] for o in mgmt.ops]
    # la CA du registre sorti de la liste part avec la bascule
    assert ("delete", "secrets", "web-registry-amorcage-ca") in [o[:3] for o in mgmt.ops]


def test_timeout_removes_nothing_and_keeps_the_mark(run, capsys):
    mgmt = world(machines=rolling(polls_until_done=10 ** 6))
    assert run(mgmt, "registries", {"registries": ["amorcage", "client"]}) == 1
    assert not [o for o in mgmt.ops if o[0] == "delete"]
    err = capsys.readouterr().err
    assert "the old templates and secrets are kept" in err and "renew --finish" in err
    assert cr.marker_of(mgmt.objs[(cs.K_CLUSTER, NS, CL)])["revision"] == 1


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
    mgmt.objs[(cs.K_CLUSTER, NS, CL)]["metadata"]["annotations"][cr.A_RENEWAL] = cr.marker_value(
        cr.marker(1, {}, ["controlPlane"], ["uid-cp-a"], []))
    assert run(mgmt, "registries", {"registries": ["client"]}) == 2
    err = capsys.readouterr().err
    assert "renew-busy" in err and "--finish" in err and "--abandon" in err and mgmt.ops == []


def test_no_address_for_the_extra_machine_is_refused(run, capsys):
    mgmt = world(pool_available=0)
    assert run(mgmt, "registries", {"registries": ["amorcage", "client"]}) == 2
    assert "renew-pool-short" in capsys.readouterr().err and mgmt.ops == []


def workers_stay():
    """Cluster API ne remplace pas un worker dont le gabarit ne change pas
    (revue 5-8 I-1) : seul le plan de contrôle roule."""
    def machines(n):
        if n == 1:
            return [t.machine("cp-a", t.OLD), t.machine("w-a", t.OLD)]
        return [t.machine("cp-b", t.NEW), t.machine("w-a", t.OLD)]
    return machines


def test_oidc_renewal_changes_the_control_plane_and_the_annotation(run, capsys):
    """Revue 5-8 I-1 : seul le gabarit du plan de contrôle est copié, et
    seules ses machines sont attendues ; les workers restent."""
    mgmt = world(registries=(), oidc=t.OIDC, machines=workers_stay())
    assert run(mgmt, "oidc", {"oidc": dict(t.OIDC, audience="k8s-2")}) == 0
    names = {o[2] for o in mgmt.ops if o[0] == "apply"}
    assert names == {"harvester-rke2-control-plane-r1"}
    patch = next(o[3] for o in mgmt.ops if o[0] == "patch" and o[1] == cr.K_CLASS)
    assert "workers" not in patch["spec"]
    assert ("delete", cr.K_CP_TEMPLATE, "harvester-rke2-control-plane") in [o[:3] for o in mgmt.ops]
    assert ("delete", cr.K_CONFIG_TEMPLATE, "harvester-rke2-worker") not in [o[:3] for o in mgmt.ops]
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


# --- revue des tâches 5 à 8 : I-1 à I-6, I-9, m-2 --------------------------------

from test_child_oidc_280 import CA as OIDC_CA  # noqa: E402

FULL_OIDC = dict(t.OIDC, username_claim="preferred_username", groups_claim="roles", username_prefix="idp:",
                 groups_prefix="idp-g:", ca_pem=OIDC_CA, admin_group="ops")


def cp_files(mgmt):
    name = next(n for (k, _s, n) in mgmt.objs if k == cr.K_CP_TEMPLATE and n.endswith("-r1"))
    return mgmt.objs[(cr.K_CP_TEMPLATE, NS, name)]["spec"]["template"]["spec"]["files"]


def test_oidc_without_request_keeps_the_current_settings(run, capsys):
    """I-3 : relu dans le fichier du gabarit, rien ne change (l'annotation
    publique seule remettait les revendications par défaut et perdait la
    CA de l'émetteur)."""
    mgmt = world(registries=(), oidc=FULL_OIDC)
    assert run(mgmt, "oidc") == 2
    assert "renew-nothing" in capsys.readouterr().err and mgmt.ops == []


def test_oidc_kept_when_only_the_registries_are_requested(run, cli, capsys, monkeypatch):
    """I-3 : `--what oidc,registries` avec seulement les registres dans la
    demande : le fichier OIDC de la copie est celui d'avant (revue finale
    I-2 : la liaison absente du groupe administrateur est livrée)."""
    got = delivered(monkeypatch, cli)
    mgmt = world(registries=("amorcage",), oidc=FULL_OIDC)
    before = next(f for f in mgmt.objs[(cr.K_CP_TEMPLATE, NS, "harvester-rke2-control-plane")]["spec"]["template"]
                  ["spec"]["files"] if f["path"] == co.FILE)
    assert run(mgmt, "oidc,registries", {"registries": ["amorcage", "client"]}) == 0
    after = next(f for f in cp_files(mgmt) if f["path"] == co.FILE)
    assert after["content"] == before["content"]
    ann = json.loads(next(o[3] for o in mgmt.ops if o[0] == "patch" and o[1] == cs.K_CLUSTER)
                     ["metadata"]["annotations"][co.ANNOTATION])
    assert ann["admin_group"] == "ops"
    assert [subjects(d["objs"]) for d in got] == [[f"{FULL_OIDC.get('groups_prefix', 'oidc:')}ops"]]


@pytest.mark.parametrize("cp,expect", [
    (None, None),
    (t.cp_template(), None),
    (t.cp_template(files=[{"path": co.FILE, "content": "{broken"}]), None),
])
def test_of_template_without_oidc(cp, expect):
    assert co.of_template(cp) is expect


def test_of_template_round_trip():
    cp = t.cc.renew_templates([t.cp_template()], {"k8s_version": "v1.34.11", "oidc": FULL_OIDC}, ["oidc"])[0]
    cl = {"metadata": {"annotations": {co.ANNOTATION: co.public(co.check(FULL_OIDC, "v1.34.11")[0])}}}
    got = co.of_template(cp, cl)
    assert co.check(got, "v1.34.11")[0] == co.check(FULL_OIDC, "v1.34.11")[0]


def test_a_mirror_subset_survives_a_registries_reread(run, capsys):
    """I-4 : une entrée qui ne miroite que docker.io ; relue, rien ne change
    (avant : toutes les origines de la déclaration, ghcr.io en plus)."""
    mgmt = world(registries=({"registry": "client", "mirrors": ["docker.io"]},))
    assert run(mgmt, "registries") == 2
    assert "renew-nothing" in capsys.readouterr().err and mgmt.ops == []


def test_a_mirror_subset_survives_a_ca_renewal(run, capsys, monkeypatch):
    """I-4 : `renew --what ca` d'un registre en transition de CA garde le
    sous-ensemble de miroirs."""
    monkeypatch.setitem(DECLS, "client", dict(DECLS["client"], ca_pem="CA-C"))
    mgmt = world(registries=({"registry": "client", "mirrors": ["docker.io"]},))
    monkeypatch.setitem(DECLS, "client", dict(DECLS["client"], ca_pem="CA-C\nCA-D"))
    assert run(mgmt, "ca") == 0
    new = mgmt.objs[("secrets", NS, "web-registries-r1")]
    doc = json.loads(new["stringData"]["registries.yaml"])
    assert sorted(doc["mirrors"]) == ["docker.io"] and new["stringData"]["ca-client.crt"] == "CA-C\nCA-D"


@pytest.mark.parametrize("names,yaml_mirrors,expect", [
    (["client"], {"docker.io": {"endpoint": ["https://registry.client"]}},
     [{"registry": "client", "mirrors": ["docker.io"]}]),
    (["client"], {"docker.io": {"endpoint": ["https://registry.client"]},
                  "ghcr.io": {"endpoint": ["https://registry.client"]}}, [{"registry": "client"}]),
    (["amorcage", "client"], {"docker.io": {"endpoint": ["https://172.16.2.76", "https://registry.client"]},
                              "ghcr.io": {"endpoint": ["https://registry.client"]}},
     [{"registry": "amorcage"}, {"registry": "client"}]),
    (["client"], None, [{"registry": "client"}]),                     # Secret illisible : la déclaration
])
def test_entries_from_nodes_secret(names, yaml_mirrors, expect):
    sec = None if yaml_mirrors is None else {"data": b64({"registries.yaml": json.dumps({"mirrors": yaml_mirrors})})}
    assert rg.entries_from_nodes_secret(sec, names, DECLS) == expect


def test_a_registry_ca_used_by_a_service_is_kept(run, capsys):
    """I-5 : la CA d'un registre sorti de la liste des nœuds reste tant
    qu'un service (HelmChartProxy) la désigne."""
    mgmt = world(registries=("amorcage", "client"))
    mgmt.objs[("helmchartproxies.addons.cluster.x-k8s.io", NS, "web-app")] = {
        "metadata": {"name": "web-app", "namespace": NS},
        "spec": {"tlsConfig": {"caSecret": {"name": "web-registry-amorcage-ca", "namespace": NS}}}}
    assert run(mgmt, "registries", {"registries": ["client"]}) == 0
    assert ("delete", "secrets", "web-registry-amorcage-ca") not in [o[:3] for o in mgmt.ops]
    assert "web-registry-amorcage-ca kept: used by the service(s) web-app" in capsys.readouterr().err


def test_the_offline_fetch_secret_follows_the_ca(run, capsys, monkeypatch):
    """I-6 : `<c>-rke2-fetch` d'un cluster sans Internet reçoit la CA en
    transition (sinon toute nouvelle machine échoue au curl, après la
    bascule)."""
    mgmt = world()
    mgmt.objs[("secrets", NS, "web-rke2-fetch")] = {"metadata": {"name": "web-rke2-fetch", "namespace": NS,
                                                                "labels": {rg.LABEL: "amorcage"}},
                                                   "data": b64({"netrc": f"machine x login pull password {PULL_PW}\n",
                                                                "ca.crt": "CA-A"})}
    monkeypatch.setitem(DECLS, "amorcage", dict(DECLS["amorcage"], ca_next_pem="CA-B"))
    assert run(mgmt, "ca") == 0
    p = next(o for o in mgmt.ops if o[0] == "patch" and o[2] == "web-rke2-fetch")
    assert p[3] == {"stringData": {"ca.crt": "CA-A\nCA-B"}}                   # jamais le netrc
    assert PULL_PW not in json.dumps(mgmt.ops)


def test_new_machines_dated_before_the_switch_still_count(run, capsys):
    """I-2 : horloge du serveur en retard sur la console (ou l'inverse) :
    les machines se reconnaissent à leur UID, pas à leur date."""
    def machines(n):
        if n == 1:
            return [t.machine("cp-a", t.NEW), t.machine("w-a", t.NEW)]
        return [t.machine("cp-b", t.OLD), t.machine("w-b", t.OLD)]
    mgmt = world(machines=machines)
    assert run(mgmt, "registries", {"registries": ["amorcage", "client"]}) == 0


@pytest.mark.parametrize("holder,code", [
    ({"run": "r1", "plan": "p1"}, "renew-plan-running"),
    ({"run": "a9", "plan": "", "action": "capi-renew", "child": "web/other"}, "renew-busy"),
    ({"run": "u1", "plan": "", "action": "capi-uninstall"}, "capi-uninstall-running"),
])
@pytest.mark.parametrize("dry", [False, True])
def test_refused_while_the_cluster_lock_is_held(cli, run, capsys, holder, code, dry):
    """I-9 : le verrou de fichier du cluster Harvester (déroulés de plan de
    la console ou de la ligne de commande, retrait de la pile, autre
    renouvellement) refuse le renouvellement, dans l'outil lui-même."""
    mgmt = world()
    run(world(), "registries", {"registries": ["amorcage"]})                 # crée le répertoire d'état
    capsys.readouterr()
    fd, _h = cli.hold_cluster_lock("harvlab", holder)
    try:
        assert run(mgmt, "registries", {"registries": ["amorcage", "client"]}, ["--dry-run"] if dry else []) == 2
    finally:
        os.close(fd)
    assert code in capsys.readouterr().err and mgmt.ops == []


def test_the_lock_is_held_during_the_renewal(cli, run, capsys):
    """I-9 : pendant l'attente, le verrou porte ce renouvellement."""
    seen = []

    def machines(n):
        seen.append(cli.deploy_holder("harvlab"))
        return rolling()(n)
    mgmt = world(machines=machines)
    assert run(mgmt, "registries", {"registries": ["amorcage", "client"]}, ["--id", "act1"]) == 0
    assert seen and all(h == {"run": "act1", "plan": "", "action": "capi-renew", "child": "web/web"} for h in seen)
    assert cli.deploy_holder("harvlab") is None                               # rendu à la fin


def marked(names=None, secrets=("web-registries",), old=("uid-cp-a", "uid-w-a")):
    mgmt = world(registries=("amorcage",))
    mgmt.objs[(cs.K_CLUSTER, NS, CL)]["metadata"]["annotations"][cr.A_RENEWAL] = cr.marker_value(cr.marker(
        1, names if names is not None else {"harvester-rke2-control-plane": "harvester-rke2-control-plane-r1"},
        ["controlPlane"], list(old), list(secrets)))
    cp = mgmt.objs[(cr.K_CP_TEMPLATE, NS, "harvester-rke2-control-plane")]
    r1 = cr.rotated([cp], 1)[0]
    mgmt.objs[(cr.K_CP_TEMPLATE, NS, r1["metadata"]["name"])] = r1
    return mgmt


def test_finish_resumes_and_cleans_up(run, capsys):
    """m-2 / I-1 : `--finish` repointe la ClusterClass si besoin, attend,
    retire l'ancien gabarit, l'ancien Secret des nœuds et la marque."""
    mgmt = marked()
    mgmt.machines = lambda n: [t.machine("cp-b", t.NEW), t.machine("w-a", t.OLD)]
    assert run(mgmt, None, extra=["--finish"]) == 0
    ops = [o[:3] for o in mgmt.ops]
    assert ("patch", cr.K_CLASS, "harvester-rke2") in ops                     # bascule finie (idempotent)
    assert ("delete", cr.K_CP_TEMPLATE, "harvester-rke2-control-plane") in ops
    assert ("delete", "secrets", "web-registries") in ops
    assert cr.A_RENEWAL not in mgmt.objs[(cs.K_CLUSTER, NS, CL)]["metadata"]["annotations"]


def test_finish_times_out_and_keeps_everything(run, capsys):
    mgmt = marked()
    mgmt.machines = lambda n: [t.machine("cp-a", t.OLD)]
    assert run(mgmt, None, extra=["--finish"]) == 1
    assert not [o for o in mgmt.ops if o[0] == "delete"]
    assert cr.marker_of(mgmt.objs[(cs.K_CLUSTER, NS, CL)]) is not None


def test_abandon_clears_the_mark_and_removes_nothing(run, capsys):
    mgmt = marked()
    assert run(mgmt, None, extra=["--abandon"]) == 0
    assert not [o for o in mgmt.ops if o[0] == "delete"]
    assert cr.A_RENEWAL not in mgmt.objs[(cs.K_CLUSTER, NS, CL)]["metadata"]["annotations"]
    assert "nothing removed" in capsys.readouterr().err
    # ensuite un nouveau renouvellement passe
    mgmt.machines = rolling()
    mgmt.reads = 0
    assert run(mgmt, "registries", {"registries": ["amorcage", "client"]}) == 0


@pytest.mark.parametrize("extra,code", [
    (["--finish"], "renew-not-running"),
    (["--abandon"], "renew-not-running"),
])
def test_finish_or_abandon_without_a_renewal(run, capsys, extra, code):
    mgmt = world()
    assert run(mgmt, None, extra=extra) == 2
    assert code in capsys.readouterr().err and mgmt.ops == []


@pytest.mark.parametrize("what,extra", [
    ("registries", ["--finish"]),
    (None, ["--finish", "--abandon"]),
    (None, ["--abandon", "--dry-run"]),
])
def test_finish_and_abandon_take_no_other_option(run, capsys, what, extra):
    mgmt = world()
    assert run(mgmt, what, extra=extra) == 2 and mgmt.ops == []


def test_an_unreadable_mark_is_never_finished(run, capsys):
    mgmt = world()
    mgmt.objs[(cs.K_CLUSTER, NS, CL)]["metadata"]["annotations"][cr.A_RENEWAL] = "{garbage"
    assert run(mgmt, "registries", {"registries": ["client"]}) == 2
    assert run(mgmt, None, extra=["--finish"]) == 2
    assert "renew-marker-unreadable" in capsys.readouterr().err
    assert run(mgmt, None, extra=["--abandon"]) == 0


# --- revue finale 2.8.0 I-2 : la liaison du groupe administrateur suit l'OIDC ---

TEAM_NS = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "equipe-a"}}


def with_socle(mgmt, cli, group):
    """Socle `hops-policies` déjà livré : un namespace d'équipe et, si
    `group`, la liaison du groupe administrateur."""
    import capi_manifests as mf
    import child_policies as cpol
    objs = [TEAM_NS] + ([cpol.admin_binding("oidc:", group)] if group else [])
    cm = mf.docs(NS, CL, cpol.ITEM, "harvester-ops", "builtin", objs, {"plan": "site", "run": "r1"}, "alice")[0]
    mgmt.objs[("configmaps", NS, mf.name_of(cpol.ITEM))] = cm
    return mgmt


def delivered(monkeypatch, cli, rc=0):
    got = []

    def deliver(kube, ns, cluster, item, package, version, objs, origin, by, timeout, secrets=()):
        got.append({"item": item, "package": package, "origin": origin, "by": by, "objs": objs})
        return rc
    monkeypatch.setattr(cli, "deliver", deliver)
    return got


def subjects(objs):
    return [s["name"] for o in objs if o["kind"] == "ClusterRoleBinding" for s in o["subjects"]]


@pytest.mark.parametrize("before,after,expect", [
    ({"admin_group": "contractors"}, {"admin_group": "ops"}, ["oidc:ops"]),            # groupe changé
    ({}, {"admin_group": "ops"}, ["oidc:ops"]),                                         # groupe ajouté
    ({"admin_group": "contractors"}, {}, []),                                           # groupe retiré
    ({"admin_group": "ops"}, {"admin_group": "ops", "groups_prefix": "idp:"}, ["idp:ops"]),  # préfixe changé
])
def test_the_admin_binding_follows_an_oidc_renewal(run, cli, capsys, monkeypatch, before, after, expect):
    got = delivered(monkeypatch, cli)
    oidc = dict(t.OIDC, groups_prefix="oidc:", **before)
    mgmt = with_socle(world(registries=(), oidc=oidc, machines=workers_stay()), cli, before.get("admin_group"))
    assert run(mgmt, "oidc", {"oidc": dict(t.OIDC, groups_prefix=after.get("groups_prefix", "oidc:"),
                                           **{k: v for k, v in after.items() if k != "groups_prefix"})}) == 0
    [d] = got
    assert d["item"] == "hops-policies" and d["origin"] == {"plan": "site", "run": "r1"} and d["by"] == "alice"
    assert subjects(d["objs"]) == expect
    assert any(o["kind"] == "Namespace" for o in d["objs"])                         # le reste du socle reste
    assert cr.A_RENEWAL not in mgmt.objs[(cs.K_CLUSTER, NS, CL)]["metadata"]["annotations"]


def test_an_unchanged_admin_group_delivers_nothing(run, cli, capsys, monkeypatch):
    got = delivered(monkeypatch, cli)
    oidc = dict(t.OIDC, groups_prefix="oidc:", admin_group="ops")
    mgmt = with_socle(world(registries=(), oidc=oidc, machines=workers_stay()), cli, "ops")
    assert run(mgmt, "oidc", {"oidc": dict(oidc, audience="k8s-2")}) == 0
    assert got == []


def test_a_failed_admin_delivery_keeps_the_mark_for_finish(run, cli, capsys, monkeypatch):
    delivered(monkeypatch, cli, rc=1)
    oidc = dict(t.OIDC, groups_prefix="oidc:", admin_group="contractors")
    mgmt = with_socle(world(registries=(), oidc=oidc, machines=workers_stay()), cli, "contractors")
    assert run(mgmt, "oidc", {"oidc": dict(oidc, admin_group="ops")}) == 1
    ev, _cap = events(capsys)
    assert ["access", "error"] == ev[-1][:2]
    assert cr.A_RENEWAL in mgmt.objs[(cs.K_CLUSTER, NS, CL)]["metadata"]["annotations"]


def test_a_group_change_alone_is_a_renewal_without_machines(run, cli, capsys, monkeypatch):
    """Revue finale I-2 : le groupe administrateur n'est pas dans le fichier
    de l'API ; aucun gabarit ne bouge, l'essai le dit (`access_changed`)."""
    oidc = dict(t.OIDC, groups_prefix="oidc:", admin_group="contractors")
    mgmt = with_socle(world(registries=(), oidc=oidc, machines=workers_stay()), cli, "contractors")
    assert run(mgmt, "oidc", {"oidc": dict(oidc, admin_group="ops")}, extra=["--dry-run"]) == 0
    _ev, cap = events(capsys)
    out = json.loads(cap.out)
    assert out["changes"] == [] and out["access_changed"] is True and out["secrets_changed"] is False
