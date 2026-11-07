"""2.7.0 (point 213) : reliquats de la revue finale 2.6.0 et de la revue 4-8.

Un test par mineur, chacun en échec avant sa correction :
m-12 (heuristique documentée), m-13 (`registries: []` d'une entrée de plan),
m-15 (nom de service pris par un autre cluster du namespace), m-16
(Chart.yaml borné, charts des registres gardés en cache), m-17 (les deux
constats d'image), M6 (`sums-too-large`), m-1 de 4-8 (descripteur sans
taille), m-12 de 4-8 (image absente, registre injoignable), constat
`registry-cert-expiring`, espace de travail (m11) des outils de registre et
d'artefacts. m-9 (contrôle d'usage de la route) est tenu par
test_registry_routes_260::test_bootstrap_delete_in_use_is_refused_with_the_users.
"""

import importlib.util
import io
import json
import os
import socket
import sys
import tarfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "tests" / "api"))
import app as wapp  # noqa: E402
import artefacts as art  # noqa: E402
import capi_cluster as cc  # noqa: E402
import capi_services as sv  # noqa: E402
import deploy_plans as dp  # noqa: E402
import oci_layout as ol  # noqa: E402
import oci_push as op  # noqa: E402
import registries as rg  # noqa: E402
import registry_seed as rs  # noqa: E402
import test_deploy_capi_250 as tc  # noqa: E402
import test_deploy_registry_260 as tdr  # noqa: E402

env, world, capi, reg = tdr.env, tdr.world, tdr.capi, tdr.reg


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "bin" / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- m-12 -------------------------------------------------------------------------
def test_m12_system_ready_registries_says_it_is_a_heuristic():
    doc = rg.system_ready_registries.__doc__
    assert "Heuristique" in doc and "no_fallback" in doc


# --- m-13 -------------------------------------------------------------------------
@pytest.mark.parametrize("regs,kept", [([], []), (["amorcage"], ["amorcage"]), (None, None)])
def test_m13_an_explicit_empty_list_is_kept_by_the_plan(regs, kept):
    entry = {"template": "rke2-petit", "name": "apps"}
    if regs is not None:
        entry["registries"] = regs
    doc = {"phases": {"clusters": {"enabled": True, "items": [entry]}}}
    plan = dp.check_plan(doc)
    item = plan["phases"]["clusters"]["items"][0]
    assert item.get("registries") == kept and (("registries" in item) == (kept is not None))


def test_m13_an_explicit_empty_list_replaces_the_template_registries(reg):
    body = tdr.plan(publish=False, cluster_regs=())
    body["phases"]["clusters"]["items"][0]["registries"] = []
    tmpl = tc.template()
    tmpl["spec"]["registries"] = ["amorcage"]
    tc.seed(reg["client"], body, tmpl=tmpl)
    run = tdr.start(reg)
    assert run.status == "done", run.result
    [spec] = reg["reg"].specs
    assert spec.get("registries") in (None, [])


# --- m-15 -------------------------------------------------------------------------
def service(cluster="web/web", name="podinfo"):
    return sv.normalize({"service": "podinfo", "name": name, "cluster": cluster})


@pytest.mark.parametrize("owners,code,level", [
    ({"web/podinfo": ["web/autre"]}, "service-name-taken", "block"),
    ({"web/podinfo": ["web/web", "web/autre"]}, "service-name-taken", "block"),
    ({"web/podinfo": ["web/web"]}, "service-exists", "warn"),
    ({"web/grafana": ["web/autre"]}, None, None),
])
def test_m15_the_service_name_of_another_cluster_of_the_namespace_is_refused(owners, code, level):
    facts = {"caaph": True, "clusters": ["web/web", "web/autre"], "services": ["web/podinfo"],
             "service_clusters": owners}
    got = [(f["code"], f["level"]) for f in sv.check(service(), facts) if f["code"].startswith("service-")]
    if code is None:
        got = [g for g in got if g[0] == "service-name-taken"]
        assert got == []
    else:
        assert got == [(code, level)]


def test_m15_service_facts_read_the_cluster_labels():
    cli = load("harvester_capi_m15", "harvester-capi.py")

    class K:
        def list(self, kind, ns=None, selector=None):
            if kind == cli.cs.K_CLUSTER:
                return [{"metadata": {"namespace": "web", "name": "web",
                                      "labels": {sv.label_of("podinfo"): "on", sv.label_of("old"): "off"}}},
                        {"metadata": {"namespace": "fleet-local", "name": "local",
                                      "labels": {sv.label_of("podinfo"): "on"}}}]
            return []
    cli._caaph_ready = lambda kube: True
    cli._clusters_without_kube_vip = lambda kube: []
    f = cli._service_facts(K())
    assert f["service_clusters"] == {"web/podinfo": ["web/web"]}


# --- m-16 -------------------------------------------------------------------------
def chart(tmp_path, size):
    p = tmp_path / "c.tgz"
    with tarfile.open(p, "w:gz") as t:
        data = b"name: c\nversion: 1.0.0\n" + b"#" * size
        ti = tarfile.TarInfo("c/Chart.yaml")
        ti.size = len(data)
        t.addfile(ti, io.BytesIO(data))
    return p


def test_m16_chart_meta_refuses_a_chart_yaml_over_1_mib(tmp_path):
    assert ol.chart_meta(chart(tmp_path, 100))["name"] == "c"
    with pytest.raises(ValueError, match="larger than 1 MiB"):
        ol.chart_meta(chart(tmp_path, 1 << 20))


def test_m16_registry_charts_are_cached_by_state_mtime(tmp_path, monkeypatch):
    state = tmp_path / "state"
    (state / rg.DIR).mkdir(parents=True)
    art.packages_dir(state).mkdir(parents=True)
    monkeypatch.setattr(wapp, "_bm_profiles_state", lambda: str(state))
    wapp._CAPI_REGISTRY_CHARTS_CACHE.clear()
    reads = []
    monkeypatch.setattr(wapp, "_capi_registry_charts_read",
                        lambda st, c: reads.append(c) or [{"registry": "r", "item": "podinfo"}])
    first = wapp._capi_registry_charts("harvlab")
    first[0]["item"] = "changed by the caller"
    assert wapp._capi_registry_charts("harvlab") == [{"registry": "r", "item": "podinfo"}]
    assert reads == ["harvlab"]
    f = state / rg.DIR / "r.yaml"
    f.write_text("x")
    os.utime(f, ns=(time.time_ns() + 10 ** 9,) * 2)
    wapp._capi_registry_charts("harvlab")
    assert reads == ["harvlab", "harvlab"]


# --- m-17 -------------------------------------------------------------------------
def test_m17_disk_smaller_than_image_keeps_the_sles_warning():
    spec = cc.normalize({"namespace": "web", "name": "web", "image": "default/sles", "network": "default/net",
                         "ssh_keypair": "default/key", "ip_pool": "pool", "disk_size": "10Gi"})
    facts = {"images": {"default/sles": {"ready": True, "iso": False, "os": "sles",
                                         "virtual_size": 30 * (1 << 30)}}}
    codes = [f["code"] for f in cc.check(spec, facts)]
    assert "disk-smaller-than-image" in codes and "image-sles-repos" in codes
    facts["images"]["default/sles"]["ready"] = False
    codes = [f["code"] for f in cc.check(spec, facts)]
    assert "image-not-ready" in codes and "image-sles-repos" not in codes


# --- M6 ---------------------------------------------------------------------------
def package_with_sums(tmp_path, size):
    p = tmp_path / "socle-1.0.0.tar.gz"
    with tarfile.open(p, "w:gz") as t:
        data = b"0" * size
        ti = tarfile.TarInfo(f"socle-1.0.0/{art.SUMS}")
        ti.size = len(data)
        t.addfile(ti, io.BytesIO(data))
    return p


@pytest.mark.parametrize("fn", ["inspect", "extract"])
def test_M6_sums_over_4_mib_have_their_own_code(tmp_path, fn):
    p = package_with_sums(tmp_path, art.SUMS_MAX + 1)
    with pytest.raises(art.ArtefactError) as e:
        art.inspect_package(p) if fn == "inspect" else art.extract_verified(p, tmp_path / "out")
    assert e.value.code == "sums-too-large"
    assert not (tmp_path / "out").exists()


# --- m-1 de la revue 4-8 ------------------------------------------------------------
class Src:
    def __init__(self, man):
        self.man, self.blobs = man, []

    def manifest(self, path, ref):
        return json.dumps(self.man).encode(), "application/vnd.oci.image.manifest.v1+json"

    def blob_to_file(self, *a, **kw):
        self.blobs.append(a)


@pytest.mark.parametrize("config,layer", [
    ({"digest": "sha256:" + "a" * 64}, {"digest": "sha256:" + "b" * 64, "size": 3}),
    ({"digest": "sha256:" + "a" * 64, "size": 2}, {"digest": "sha256:" + "b" * 64}),
    ({"digest": "sha256:" + "a" * 64, "size": "2"}, {"digest": "sha256:" + "b" * 64, "size": 3}),
    ({"digest": "sha256:" + "a" * 64, "size": True}, {"digest": "sha256:" + "b" * 64, "size": 3}),
    ({"digest": "sha256:" + "a" * 64, "size": 2}, {"digest": "sha256:" + "b" * 64, "size": -1}),
])
def test_m1_a_descriptor_without_size_is_refused_before_any_byte(tmp_path, config, layer):
    config = dict(config, mediaType="application/x-other")       # pas de lecture de plateforme
    src = Src({"schemaVersion": 2, "config": config, "layers": [layer]})
    with pytest.raises(op.RegistryError) as e:
        ol.copy_image(src, "reg.example/a/b:1", ol.Layout(tmp_path / "lay"))
    assert e.value.code == "descriptor-no-size" and src.blobs == []


def test_m1_a_chart_layer_without_size_is_refused():
    arts = load("harvester_artefacts_m1", "harvester-artefacts.py")

    class Reg:
        blobs = []

        def manifest(self, repo, tag):
            return json.dumps({"layers": [{"mediaType": ol.M_HELM_CHART, "digest": "sha256:" + "c" * 64}]}).encode(), ""

        def blob_to_file(self, *a, **kw):
            self.blobs.append(a)

    class Sources:
        def registry(self, host):
            return Reg()
    with pytest.raises(ValueError, match="has no size"):
        arts.fetch_chart(Sources(), {"repo": "oci://reg.example/charts", "name": "c", "version": "1.0.0"}, "x.tgz")
    assert Reg.blobs == []


# --- m-12 de la revue 4-8 -----------------------------------------------------------
def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_m12_an_unreachable_registry_is_named():
    r = op.Registry(f"127.0.0.1:{closed_port()}", None, timeout=5)
    with pytest.raises(op.RegistryError) as e:
        r.manifest_digest("a/b", "1")
    assert e.value.code == "registry-unreachable"


def test_m12_publication_refuses_instead_of_a_typeerror(tmp_path, monkeypatch):
    arts = load("harvester_artefacts_m12", "harvester-artefacts.py")
    pkg = {"path": str(tmp_path), "index": {"items": []}}

    class Lay:
        def __init__(self, *a):
            pass

        def find(self, n):
            return None
    monkeypatch.setattr(arts.ol, "Layout", Lay)
    with pytest.raises(arts.Refused) as e:
        arts.plan_publication(pkg, {"name": "r", "usage": ["images"]}, None, ["a/b:1"], [], tmp_path)
    assert [f["code"] for f in e.value.findings] == ["publish-image-missing"]

    class Lay2(Lay):
        def find(self, n):
            return {"digest": "sha256:" + "d" * 64}

    class Dst:
        host = "reg.example"

        def manifest_digest(self, repo, tag):
            raise op.RegistryError("HEAD reg.example: refused", code="registry-unreachable")
    monkeypatch.setattr(arts.ol, "Layout", Lay2)
    with pytest.raises(arts.Refused) as e:
        arts.plan_publication(pkg, {"name": "r", "usage": ["images"]}, Dst(), ["a/b:1"], [], tmp_path)
    assert [f["code"] for f in e.value.findings] == ["registry-unreachable"]


# --- registry-cert-expiring -----------------------------------------------------------
DAY = 86400


@pytest.mark.parametrize("left,days", [(59 * DAY + 10, 59), (60 * DAY, None), (-3 * DAY, 0), (None, None)])
def test_cert_expiring_at_60_days(left, days):
    now = 1_800_000_000
    got = rs.cert_findings("amorcage", None if left is None else now + left, now)
    if days is None:
        assert got == []
    else:
        assert [(f["code"], f["level"], f["facts"]["days"]) for f in got] == [("registry-cert-expiring", "warn", days)]


def test_cert_not_after_reads_openssl(monkeypatch):
    class P:
        returncode, stdout = 0, "notAfter=Nov  7 12:00:00 2028 GMT\n"
    seen = []
    t = rs.cert_not_after("PEM", runner=lambda argv, **kw: seen.append((argv, kw.get("input"))) or P())
    import calendar
    assert t == calendar.timegm((2028, 11, 7, 12, 0, 0)) and seen[0] == (["openssl", "x509", "-noout", "-enddate"], "PEM")

    class Bad:
        returncode, stdout = 1, ""
    assert rs.cert_not_after("PEM", runner=lambda argv, **kw: Bad()) is None


def test_cert_expiring_is_a_plan_warning(reg, monkeypatch):
    fake = reg["reg"]
    rg.save(reg["state"], "amorcage", {"kind": "bootstrap", "endpoint": "172.16.2.76", "usage": ["images", "charts"],
                                       "clusters": ["harvlab"], "cluster": "harvlab", "address": "172.16.2.76",
                                       "size_gib": 8, "storage_class": "harv-rep1"})
    fake.present["amorcage"] = {"origin": {"plan": "site", "run": "aaaaaaaaaaaa"}, "ready": True}
    real = fake.json

    def json_with_cert(kc, sub, args, timeout=120):
        out, rc, err = real(kc, sub, args, timeout)
        if sub == "registry:status":
            out["findings"] = [cc.finding("registry-cert-expiring", "warn", registry="amorcage", days=12,
                                          not_after="2026-11-19")]
        return out, rc, err
    monkeypatch.setattr(wapp, "_deploy_registry_json", json_with_cert)
    tdr.seed(reg)
    out = tdr.check(reg)
    got = [f for f in out["findings"] if f["code"] == "registry-cert-expiring"]
    assert len(got) == 1 and got[0]["level"] == "warning" and got[0]["phase"] == "registry"
    assert "amorcage: " in got[0]["detail"] and "days=12" in got[0]["detail"]


def test_registry_status_reports_the_certificate(monkeypatch, capsys, tmp_path):
    import base64
    reg_tool = load("harvester_registry_cert", "harvester-registry.py")

    class K:
        def get(self, kind, ns, name):
            if kind == "secrets" and name == "amorcage-tls":
                return {"data": {"tls.crt": base64.b64encode(b"CERT").decode(), "tls.key": "S0VZ"}}
            return None
    monkeypatch.setattr(reg_tool, "kube_from", lambda args: (K(), "kc"))
    monkeypatch.setattr(reg_tool, "state", lambda: str(tmp_path))
    seen = []
    monkeypatch.setattr(reg_tool.rs, "cert_not_after", lambda pem, runner=None: seen.append(pem) or time.time() + DAY)
    assert reg_tool.main(["status", "amorcage", "--cluster", "harvlab", "--kubeconfig", "kc"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert seen == ["CERT"] and out["findings"][0]["code"] == "registry-cert-expiring"
    assert "S0VZ" not in json.dumps(out) and "KEY" not in json.dumps(out)


# --- m11 : espace de travail des outils de registre et d'artefacts -----------------------
@pytest.mark.parametrize("tool,anchor", [
    ("harvester-registry.py", "work = work_dir.mkdtemp(\"hops-registry-\")"),
    ("harvester-artefacts.py", "work = work_dir.mkdtemp(\"hops-publish-\")"),
])
def test_m11_tools_use_the_private_work_dir(tool, anchor):
    src = (ROOT / "bin" / tool).read_text()
    assert anchor in src
    assert "HARVESTER_OPS_WORK_DIR\")))" not in src


def test_m11_publish_refuses_a_shared_work_dir(monkeypatch, tmp_path, capsys):
    arts = load("harvester_artefacts_m11", "harvester-artefacts.py")
    monkeypatch.setenv("HARVESTER_OPS_WORK_DIR", "/tmp")
    monkeypatch.setattr(arts.art, "load_package", lambda *a: {"trust": {"state": "valid"}, "path": str(tmp_path),
                                                             "index": {"items": []}})
    import kube
    monkeypatch.setattr(rg, "load", lambda st, n: {"name": "r", "clusters": ["harvlab"], "endpoint": "r.example"})
    monkeypatch.setattr(rg, "read_creds", lambda k, n, role: {"username": "u", "password": "p"})
    monkeypatch.setattr(kube, "cluster_config", lambda c: {"kubeconfig": "kc"})
    monkeypatch.setattr(arts.art, "settings", lambda st, cfg: {"strict": False})
    rc = arts.main(["--state-dir", str(tmp_path), "publish", "socle", "1.0.0", "--cluster", "harvlab",
                    "--registry", "r", "--kubeconfig", "kc"])
    assert rc == arts.EXIT_REFUSED and "shared by every account" in capsys.readouterr().err
