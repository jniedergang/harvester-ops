"""2.8.0 (tâche 7, point 235) : CA du registre d'amorçage gardée et
contrainte, `harvester-registry renew-cert` (même CA, ou nouvelle CA en
transition pour un registre d'avant 2.8.0).

openssl réel, cluster simulé (fixture de test_registry_seed_cli_260).

Mutations jouées (rapport des tâches 5 à 8) : « CA sans contraintes de
nom », « bascule sans transition ».
"""

import base64
import json
import subprocess
from pathlib import Path

import pytest

import test_registry_seed_cli_260 as tcli
from test_registry_seed_cli_260 import cli, deploy, world  # noqa: F401
import registries as rg

pytestmark = pytest.mark.skipif(not __import__("shutil").which("openssl"), reason="openssl needed")


def x509_text(pem, tmp_path, name="c.crt"):
    p = Path(tmp_path) / name
    p.write_text(pem)
    return subprocess.run(["openssl", "x509", "-in", str(p), "-noout", "-text"], capture_output=True, text=True,
                          check=True).stdout


def verify(ca_pem, crt_pem, tmp_path):
    (Path(tmp_path) / "v-ca.crt").write_text(ca_pem)
    (Path(tmp_path) / "v.crt").write_text(crt_pem)
    return subprocess.run(["openssl", "verify", "-CAfile", str(Path(tmp_path) / "v-ca.crt"),
                           str(Path(tmp_path) / "v.crt")], capture_output=True, text=True).returncode == 0


def applied(kube, name):
    return [d for d in kube.applied if d["metadata"]["name"] == name]


class PatchKube(tcli.FakeKube):
    def __init__(self, objects=None):
        super().__init__(objects)
        self.patched = []

    def patch(self, kind, ns, name, body, dry_run=False):
        self.patched.append((kind, name, body))


@pytest.fixture
def seeded(world, capsys, monkeypatch):
    kube = PatchKube()
    world["kube"] = kube
    monkeypatch.setattr(cli, "kube_from", lambda args, need_entry=False: (kube, {"nodes": [{"ip": "10.0.0.1"}]}))
    code, out, err = deploy(capsys, "--dns-name", "harvlab2-registry.home.lo")
    assert code == 0, err
    for d in kube.applied:                         # Secrets relus comme le serveur les rend (data)
        if d["kind"] == "Secret":
            kube.objects[("secrets", d["metadata"]["name"])] = dict(d, data={
                k: base64.b64encode(v.encode()).decode() for k, v in d["stringData"].items()})
    world.update(out=out, err=err)
    return world


def test_the_ca_key_is_kept_and_the_ca_constrained(seeded, tmp_path):
    """Mutation « CA sans contraintes de nom »."""
    kube = seeded["kube"]
    [key] = applied(kube, "amorcage-ca-key")
    assert key["type"] == "Opaque" and key["metadata"]["namespace"] == rg.NAMESPACE
    assert key["metadata"]["labels"] == {rg.LABEL: "amorcage", rg.MANAGED: "true"}
    assert "PRIVATE KEY" in key["stringData"]["ca.key"]
    txt = x509_text(key["stringData"]["ca.crt"], tmp_path)
    assert "X509v3 Name Constraints: critical" in txt
    assert "IP:172.16.2.76/255.255.255.255" in txt and "DNS:harvlab2-registry.home.lo" in txt
    assert key["stringData"]["ca.key"] not in seeded["out"] + seeded["err"]
    assert all(key["stringData"]["ca.key"] not in " ".join(a) for a, _i in seeded["calls"])
    # la CA contrainte signe bien le certificat du registre (vérification stricte)
    [tls] = applied(kube, "amorcage-tls")
    assert verify(key["stringData"]["ca.crt"], tls["stringData"]["tls.crt"], tmp_path)
    assert list(Path(seeded["work"]).iterdir()) == []


def renew(capsys, *extra):
    code = cli.main(["renew-cert", "amorcage", "--cluster", "harvlab2", "--kubeconfig", "/kc", *extra])
    o = capsys.readouterr()
    return code, o.out, o.err


def test_renew_cert_with_the_kept_key_keeps_the_ca(seeded, capsys, tmp_path):
    kube = seeded["kube"]
    [old] = applied(kube, "amorcage-tls")
    kube.applied.clear()
    code, out, err = renew(capsys, "--days", "400")
    assert code == 0, err
    [new] = applied(kube, "amorcage-tls")
    ca = applied(kube, "amorcage-ca-key") or [kube.objects[("secrets", "amorcage-ca-key")]]
    assert new["stringData"]["ca.crt"] == old["stringData"]["ca.crt"]                    # même CA
    assert new["stringData"]["tls.crt"] != old["stringData"]["tls.crt"]
    assert verify(old["stringData"]["ca.crt"], new["stringData"]["tls.crt"], tmp_path)
    assert "days" not in out and json.loads(out)["not_after"] > 0 and json.loads(out)["read_cut_seconds"] >= 0
    assert [p[:2] for p in kube.patched] == [("deployments", "amorcage")]                   # pod redémarré
    assert rg.load(seeded["state"], "amorcage")["ca_pem"] == old["stringData"]["ca.crt"]
    assert "PRIVATE KEY" not in out + err and list(Path(seeded["work"]).iterdir()) == []
    assert not [d for d in kube.applied if d["kind"] != "Secret"]                           # aucun gabarit


def test_renew_cert_without_a_kept_key_needs_new_ca(seeded, capsys):
    kube = seeded["kube"]
    kube.objects.pop(("secrets", "amorcage-ca-key"))
    kube.applied.clear()
    code, out, err = renew(capsys)
    assert code == 2 and "registry-ca-key-missing" in err and json.loads(out)["code"] == "registry-ca-key-missing"
    assert kube.applied == [] and kube.patched == []


def child_cluster(kube, nodes_secret="web-registries", mark=None):
    """Cluster enfant `web/web` : Cluster, ClusterClass et gabarit du plan
    de contrôle qui désigne le Secret des nœuds (revue 5-8 I-6 : c'est le
    Secret DÉSIGNÉ qui compte, pas n'importe lequel)."""
    import capi_renew as cr
    ann = {cr.A_RENEWAL: mark} if mark else {}
    kube.objects[("clusters.cluster.x-k8s.io", "web")] = {
        "metadata": {"name": "web", "namespace": "web", "annotations": ann},
        "spec": {"topology": {"classRef": {"name": "harvester-rke2"}}}}
    kube.objects[(cr.K_CLASS, "harvester-rke2")] = {"metadata": {"name": "harvester-rke2"}, "spec": {
        "controlPlane": {"templateRef": {"kind": "RKE2ControlPlaneTemplate", "name": "cp"}}}}
    kube.objects[(cr.K_CP_TEMPLATE, "cp")] = {"kind": "RKE2ControlPlaneTemplate", "metadata": {"name": "cp"},
                                              "spec": {"template": {"spec": {"files": [
                                                  {"path": rg.REGISTRIES_FILE, "contentFrom": {"secret": {
                                                      "name": nodes_secret, "key": "registries.yaml"}}}]}}}}


def child_nodes_secret(kube, ca_text, name="web-registries"):
    if ("clusters.cluster.x-k8s.io", "web") not in kube.objects:
        child_cluster(kube)
    kube.objects[("secrets", name)] = {
        "kind": "Secret", "metadata": {"name": name, "namespace": "web",
                                       "labels": {rg.used_label("amorcage"): "used", "harvester-ops.io/generated": "true"}},
        "data": {"ca-amorcage.crt": base64.b64encode(ca_text.encode()).decode()}}


def test_new_ca_in_three_steps(seeded, capsys, tmp_path):
    """Mutation « bascule sans transition » : tant qu'un cluster enfant ne
    croit pas la nouvelle CA, le certificat ne bascule pas."""
    kube = seeded["kube"]
    old_ca = kube.objects[("secrets", "amorcage-ca-key")]["stringData"]["ca.crt"]
    kube.objects.pop(("secrets", "amorcage-ca-key"))                     # registre d'avant 2.8.0
    child_nodes_secret(kube, old_ca)
    kube.applied.clear()
    # 1) nouvelle CA gardée en transition ; un cluster ne la croit pas : refus
    code, out, err = renew(capsys, "--new-ca")
    assert code == 2 and "registry-ca-transition-pending" in err and json.loads(out)["clusters"] == ["web/web"]
    [nxt] = applied(kube, "amorcage-ca-next")
    d = rg.load(seeded["state"], "amorcage")
    assert d["ca_next_pem"] == nxt["stringData"]["ca.crt"] and d["ca_pem"] == old_ca
    assert not applied(kube, "amorcage-tls") and kube.patched == []
    assert rg.ca_bundle(d) == old_ca + nxt["stringData"]["ca.crt"]       # ce que `renew --what ca` pose
    kube.objects[("secrets", "amorcage-ca-next")] = dict(nxt, data={
        k: base64.b64encode(v.encode()).decode() for k, v in nxt["stringData"].items()})
    # 2) le cluster enfant croit les deux ; 3) bascule
    child_nodes_secret(kube, rg.ca_bundle(d))
    code, out, err = renew(capsys, "--new-ca")
    assert code == 0, err
    [tls] = applied(kube, "amorcage-tls")
    assert verify(nxt["stringData"]["ca.crt"], tls["stringData"]["tls.crt"], tmp_path)
    assert not verify(old_ca, tls["stringData"]["tls.crt"], tmp_path)
    d = rg.load(seeded["state"], "amorcage")
    assert d["ca_pem"] == nxt["stringData"]["ca.crt"] and "ca_next_pem" not in d
    assert ("secrets", "amorcage-ca-next") in kube.deleted
    assert applied(kube, "amorcage-ca-key")[-1]["stringData"]["ca.crt"] == nxt["stringData"]["ca.crt"]


def test_a_transition_in_progress_is_resumed_without_new_ca_flag(seeded, capsys):
    kube = seeded["kube"]
    kube.objects.pop(("secrets", "amorcage-ca-key"))
    child_nodes_secret(kube, "nothing")
    renew(capsys, "--new-ca")
    kube.applied.clear()
    code, _out, err = renew(capsys)                     # transition en cours : jamais l'ancienne clé
    assert code == 2 and "registry-ca-transition-pending" in err and not applied(kube, "amorcage-tls")


def test_status_says_the_missing_key_and_the_pending_transition(seeded, capsys, monkeypatch):
    kube = seeded["kube"]
    kube.objects[("deployments", "amorcage")] = {"metadata": {"name": "amorcage"}, "status": {}}
    kube.objects.pop(("secrets", "amorcage-ca-key"))
    rg.update(seeded["state"], "amorcage", lambda x: x.update(ca_next_pem=x["ca_pem"]))
    child_nodes_secret(kube, "nothing")
    monkeypatch.setattr(cli.rs, "cert_not_after", lambda pem, runner=None: None)
    assert cli.main(["status", "amorcage", "--cluster", "harvlab2", "--kubeconfig", "/kc"]) == 0
    got = {f["code"]: f for f in json.loads(capsys.readouterr().out)["findings"]}
    assert got["registry-ca-key-missing"]["level"] == "ok"
    assert got["registry-ca-transition-pending"]["facts"]["clusters"] == "web/web (nodes)"


def test_a_switch_stopped_after_the_next_ca_went_is_refused_cleanly(seeded, capsys):
    """Revue 5-8 m-6 : la CA suivante retirée mais la déclaration pas encore
    mise à jour : refus nommé, plus de KeyError."""
    kube = seeded["kube"]
    kube.objects.pop(("secrets", "amorcage-ca-key"))
    rg.update(seeded["state"], "amorcage", lambda x: x.update(ca_next_pem=x["ca_pem"]))
    kube.applied.clear()
    code, out, err = renew(capsys)
    assert code == 2 and "registry-ca-signer-missing" in err
    assert json.loads(out)["blocked"] is True and not applied(kube, "amorcage-tls")
