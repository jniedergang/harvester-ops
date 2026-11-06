"""v2.6.0 : registre d'amorçage (registry_seed) : manifestes, configuration
de Distribution, constats, certificat (vrai openssl) et comptes htpasswd
(mot de passe sur l'entrée standard)."""

import json
import shutil
import ssl
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import registry_seed as rs  # noqa: E402

SPEC = {"name": "amorcage", "address": "172.16.2.76", "size_gib": 20, "storage_class": "harv-rep1",
        "image": "registry.suse.com/suse/registry@sha256:" + "d" * 64}
TLS = {"crt": "CRT", "key": "KEY", "ca": "CA"}
HTP = {"read": "push:$2y$05$abc\npull:$2y$05$def\n", "write": "push:$2y$05$abc\n"}


def objs():
    return {(o["kind"], o["metadata"]["name"]): o for o in rs.manifests(SPEC, TLS, HTP)}


def test_every_object_is_labelled_in_the_registry_namespace():
    o = objs()
    assert set(o) == {("Namespace", "harvester-ops-registry"), ("Secret", "amorcage-tls"),
                      ("Secret", "amorcage-htpasswd"), ("ConfigMap", "amorcage-config"),
                      ("PersistentVolumeClaim", "amorcage-data"), ("Deployment", "amorcage"),
                      ("Service", "amorcage")}
    for (kind, _), x in o.items():
        if kind != "Namespace":
            assert x["metadata"]["namespace"] == "harvester-ops-registry"
            assert x["metadata"]["labels"] == {"harvester-ops.io/registry": "amorcage"}


def test_the_service_gets_its_fixed_address_from_harvester_kube_vip():
    svc = objs()[("Service", "amorcage")]
    assert svc["metadata"]["annotations"] == {"kube-vip.io/loadbalancerIPs": "172.16.2.76"}
    assert svc["spec"]["type"] == "LoadBalancer"
    assert [(p["name"], p["port"], p["targetPort"]) for p in svc["spec"]["ports"]] == \
        [("read", 443, 5000), ("write", 5443, 5001)]


def test_the_pod_runs_two_bounded_instances_of_the_imported_image():
    dep = objs()[("Deployment", "amorcage")]
    assert dep["spec"]["strategy"] == {"type": "Recreate"} and dep["spec"]["replicas"] == 1
    pod = dep["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsUser"] == 499 and pod["securityContext"]["fsGroup"] == 499
    read, write = pod["containers"]
    for c in (read, write):
        assert c["image"] == SPEC["image"] and c["imagePullPolicy"] == "Never"
        assert c["resources"] == {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}}
        assert c["readinessProbe"]["periodSeconds"] >= 60 and "tcpSocket" in c["readinessProbe"]
    auth = {v["name"]: v["secret"]["items"][0]["key"] for v in pod["volumes"] if v["name"].startswith("auth")}
    assert auth == {"auth-read": "read", "auth-write": "write"}
    assert [m["name"] for m in read["volumeMounts"]][-1] == "auth-read"
    assert [m["name"] for m in write["volumeMounts"]][-1] == "auth-write"
    pvc = objs()[("PersistentVolumeClaim", "amorcage-data")]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "20Gi"
    assert pvc["spec"]["storageClassName"] == "harv-rep1"


def test_only_bcrypt_hashes_are_in_the_htpasswd_secret():
    sec = objs()[("Secret", "amorcage-htpasswd")]["stringData"]
    for text in sec.values():
        for line in text.splitlines():
            assert line.split(":", 1)[1].startswith("$2")
    assert "pull" not in sec["write"]                        # l'instance d'écriture ne connaît que push


@pytest.mark.parametrize("which,expected", [
    ("read.yml", {"version": 0.1, "log": {"level": "info"},
                  "storage": {"filesystem": {"rootdirectory": "/var/lib/docker-registry"},
                              "delete": {"enabled": False}, "maintenance": {"readonly": {"enabled": True},
                                                                          "uploadpurging": {"enabled": False}}},
                  "http": {"addr": ":5000", "tls": {"certificate": "/etc/registry/tls/tls.crt",
                                                    "key": "/etc/registry/tls/tls.key"}},
                  "auth": {"htpasswd": {"realm": "harvester-ops", "path": "/etc/registry/auth/htpasswd"}}}),
    ("write.yml", {"version": 0.1, "log": {"level": "info"},
                   "storage": {"filesystem": {"rootdirectory": "/var/lib/docker-registry"},
                               "delete": {"enabled": False}},
                   "http": {"addr": ":5001", "tls": {"certificate": "/etc/registry/tls/tls.crt",
                                                     "key": "/etc/registry/tls/tls.key"}},
                   "auth": {"htpasswd": {"realm": "harvester-ops", "path": "/etc/registry/auth/htpasswd"}}}),
])
def test_the_distribution_configuration_field_by_field(which, expected):
    cm = objs()[("ConfigMap", "amorcage-config")]
    assert json.loads(cm["data"][which]) == expected


LH = {"nodes": [{"metadata": {"name": "n1"}, "spec": {"disks": {"d": {"allowScheduling": True}}},
                 "status": {"diskStatus": {"d": {
                     "conditions": [{"type": "Schedulable", "status": "True"}, {"type": "Ready", "status": "True"}],
                     "storageMaximum": 100 << 30, "storageScheduled": 0, "storageAvailable": 100 << 30}}}}],
      "over": 200.0, "minimal": 25.0}
GOOD = {"pools": [{"name": "capi", "ranges": [{"start": "172.16.2.200", "end": "172.16.2.205"}]}],
        "vip": "172.16.2.70", "address_answers": False, "longhorn": LH,
        "class_replicas": {"harv-rep1": 1, "harvester-longhorn": 3}, "bundle": True, "tools_missing": [],
        "name_taken": False}


@pytest.mark.parametrize("spec_over,facts_over,codes", [
    ({}, {}, []),
    ({"address": "172.16.2.300"}, {}, ["registry-address-invalid"]),
    ({"address": "fe80::1"}, {}, ["registry-address-invalid"]),
    ({"address": "172.16.2.202"}, {}, ["registry-address-in-pool"]),
    ({"address": "172.16.2.70"}, {}, ["registry-address-vip"]),
    ({}, {"address_answers": True}, ["registry-address-answers"]),
    ({}, {"name_taken": True}, ["registry-name-taken"]),
    ({}, {"bundle": False}, ["registry-images-missing"]),
    ({}, {"tools_missing": ["htpasswd"]}, ["registry-tools-missing"]),
    ({"storage_class": "harvester-longhorn"}, {}, ["registry-replicas-nodes"]),
    ({"size_gib": 2000}, {}, ["registry-room-short"]),
    ({"storage_class": "nope"}, {}, ["registry-storage-class-unknown"]),
])
def test_each_bootstrap_finding(spec_over, facts_over, codes):
    got = rs.check(dict(SPEC, **spec_over), dict(GOOD, **facts_over))
    assert [f["code"] for f in got] == codes
    assert all(f["level"] == "block" for f in got)


class Recorder:
    def __init__(self, out="push:$2y$05$hashhashhash\n", rc=0):
        self.calls, self.out, self.rc = [], out, rc

    def __call__(self, argv, input=None, **kw):
        self.calls.append((argv, input))
        return subprocess.CompletedProcess(argv, self.rc, self.out, "")


def test_htpasswd_reads_the_password_on_stdin_only():
    r = Recorder()
    line = rs.make_htpasswd("push", "Very-Secret-1", runner=r, tool="htpasswd")
    assert line == "push:$2y$05$hashhashhash\n"
    argv, stdin = r.calls[0]
    assert argv == ["htpasswd", "-niB", "push"] and stdin == "Very-Secret-1\n"
    assert all("Very-Secret-1" not in a for a in argv)


@pytest.mark.parametrize("out,rc", [("push:plain\n", 0), ("", 1)])
def test_htpasswd_failures_say_nothing_of_the_password(out, rc):
    with pytest.raises(RuntimeError) as e:
        rs.make_htpasswd("push", "Very-Secret-1", runner=Recorder(out, rc), tool="htpasswd")
    assert "Very-Secret-1" not in str(e.value)


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl needed")
def test_the_generated_ca_is_accepted_by_strict_verification(tmp_path):
    tls = rs.make_tls(tmp_path, "172.16.2.76", "harvlab2-registry.home.lo", "amorcage")
    ctx = ssl.create_default_context(cadata=tls["ca"])       # refusée sans keyUsage (Python 3.13)
    assert ctx.get_ca_certs()
    (tmp_path / "check.crt").write_text(tls["crt"])
    txt = subprocess.run(["openssl", "x509", "-in", str(tmp_path / "check.crt"), "-noout", "-text"],
                         capture_output=True, text=True, check=True).stdout
    assert "IP Address:172.16.2.76" in txt and "DNS:harvlab2-registry.home.lo" in txt
    assert "TLS Web Server Authentication" in txt and "Digital Signature" in txt
    v = subprocess.run(["openssl", "verify", "-CAfile", str(tmp_path / "ca.crt"), str(tmp_path / "check.crt")],
                       capture_output=True, text=True)
    assert v.returncode == 0
    assert oct((tmp_path / "ca.key").stat().st_mode & 0o777) == "0o600"
    assert tls["key"].startswith("-----BEGIN PRIVATE KEY-----")


def test_tool_lookup_and_the_bundle_check(tmp_path, monkeypatch):
    monkeypatch.setattr(rs.shutil, "which", lambda t: None)
    assert rs.tools_missing() == ["openssl", "htpasswd"]
    assert rs.bundle(tmp_path) is None
    d = tmp_path / "registry-bundle"
    d.mkdir()
    (d / "registry-images.tar").write_bytes(b"tar")
    (d / "registry-images.json").write_text(json.dumps({"images": {"registry": "r@sha256:x"}}))
    import hashlib
    sums = "".join(f"{hashlib.sha256((d / n).read_bytes()).hexdigest()}  {n}\n"
                   for n in ("registry-images.tar", "registry-images.json"))
    (d / "SHA256SUMS").write_text(sums)
    assert rs.bundle(tmp_path) == {"tar": str(d / "registry-images.tar"), "images": {"registry": "r@sha256:x"}}
    (d / "registry-images.tar").write_bytes(b"altered")
    assert rs.bundle(tmp_path) is None


def real_ca(tmp_path):
    """Une CA lisible (pour une déclaration), faite par make_tls."""
    d = tmp_path / "real-ca"
    d.mkdir()
    return rs.make_tls(d, "10.0.0.1")["ca"]


def test_hardening_no_node_ports_and_a_read_only_reader():
    """Revue m-6 et m-7 : kube-vip sert l'adresse sans NodePort (sinon 443 et
    5443 s'ouvrent sur chaque nœud Harvester) ; l'instance de lecture monte
    le volume en lecture seule et ne purge jamais les envois en cours de
    l'instance d'écriture."""
    o = objs()
    assert o[("Service", "amorcage")]["spec"]["allocateLoadBalancerNodePorts"] is False
    pod = o[("Deployment", "amorcage")]["spec"]["template"]["spec"]
    read, write = pod["containers"]
    data = {c["name"]: next(m for m in c["volumeMounts"] if m["name"] == "data") for c in (read, write)}
    assert data["read"].get("readOnly") is True and not data["write"].get("readOnly")
    conf = json.loads(o[("ConfigMap", "amorcage-config")]["data"]["read.yml"])
    assert conf["storage"]["maintenance"]["uploadpurging"] == {"enabled": False}
    assert "uploadpurging" not in json.dumps(json.loads(o[("ConfigMap", "amorcage-config")]["data"]["write.yml"]))
