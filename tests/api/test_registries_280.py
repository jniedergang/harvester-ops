"""2.8.0 (tâche 7) : registres achevés, partie pure.

* m-14 (revue finale 2.6.0) : le mot de passe de lecture n'est rangé que
  dans `<c>-registries` ; marque d'usage par étiquette ; l'ancienne forme
  (`<c>-registry-<r>-auth`) est encore lue ;
* CA en transition (ancienne et nouvelle) ;
* fusion dans `containerd-registry` et `additional-ca` des nœuds Harvester.

Mutations jouées (rapport des tâches 5 à 8) : « mot de passe encore copié
dans -auth », « réglage écrasé au lieu d'être fusionné ».
"""

import base64
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import capi_cluster as cc  # noqa: E402
import hv_settings as hs  # noqa: E402
import registries as rg  # noqa: E402

PULL_PW = "PULL-PW-280-31415"
CLUSTER = {"name": "web", "namespace": "web"}
DECLS = {"a": {"name": "a", "endpoint": "172.16.2.76", "ca_pem": "CA-A\n", "usage": ["images"],
               "mirrors": ["docker.io", "ghcr.io"]},
         "b": {"name": "b", "endpoint": "registry.client", "usage": ["images"], "mirrors": ["docker.io"]}}


def creds(*names):
    return {n: {"username": "pull", "password": PULL_PW} for n in names}


def test_a_28_cluster_keeps_the_read_password_in_its_nodes_secret_only():
    """Mutation « mot de passe encore copié dans -auth »."""
    _, _, secrets = rg.rke2_config(["a", "b"], DECLS, CLUSTER, creds("a", "b"))
    holders = [s["metadata"]["name"] for s in secrets if PULL_PW in json.dumps(s)]
    assert holders == ["web-registries"]
    assert not [s for s in secrets if s["metadata"]["name"].endswith("-auth")]
    nodes = next(s for s in secrets if s["metadata"]["name"] == "web-registries")
    assert nodes["metadata"]["labels"] == {cc.GENERATED: "true", "harvester-ops.io/registry-a": "used",
                                           "harvester-ops.io/registry-b": "used"}


def as_server(secret):
    out = json.loads(json.dumps(secret))
    out["data"] = {k: base64.b64encode(v.encode()).decode() for k, v in out.pop("stringData").items()}
    return out


def old_form(names):
    """Secrets d'un cluster créé en 2.7 : `-auth` étiquetés, Secret des
    nœuds sans marque d'usage."""
    _, _, secrets = rg.rke2_config(names, DECLS, CLUSTER, creds(*names))
    nodes = as_server(next(s for s in secrets if s["metadata"]["name"] == "web-registries"))
    nodes["metadata"]["labels"] = {cc.GENERATED: "true"}
    auth = [{"metadata": {"name": f"web-registry-{n}-auth", "namespace": "web",
                          "labels": {cc.GENERATED: "true", rg.LABEL: n}},
             "data": {"password": base64.b64encode(PULL_PW.encode()).decode()}} for n in names]
    return nodes, auth


class Kube:
    def __init__(self, secrets):
        self.secrets = secrets

    def list(self, kind, ns=None, selector=None):
        out = [s for s in self.secrets if ns is None or s["metadata"]["namespace"] == ns]
        for cond in (selector or "").split(","):
            if cond:
                k, _, v = cond.partition("=")
                out = [s for s in out if (s["metadata"].get("labels") or {}).get(k) == v]
        return out

    def get(self, kind, ns, name):
        return next((s for s in self.secrets if s["metadata"]["namespace"] == ns and s["metadata"]["name"] == name),
                    None)


@pytest.mark.parametrize("names", [["a"], ["b", "a"]])
def test_both_forms_read_the_same(names):
    nodes_old, auth = old_form(names)
    _, _, secrets = rg.rke2_config(names, DECLS, CLUSTER, creds(*names))
    nodes_new = as_server(next(s for s in secrets if s["metadata"]["name"] == "web-registries"))
    assert rg.cluster_registry_names(nodes_old, auth, DECLS) == rg.cluster_registry_names(nodes_new, [], DECLS) == names
    for n in names:
        assert rg.child_users(Kube([nodes_old] + auth), n) == rg.child_users(Kube([nodes_new]), n) == ["web"]
        assert rg.cluster_uses(Kube([nodes_old] + auth), "web", "web", n)
        assert rg.cluster_uses(Kube([nodes_new]), "web", "web", n)
    assert not rg.cluster_uses(Kube([nodes_new]), "web", "web2", names[0])     # pas un autre cluster
    assert rg.child_users(Kube([nodes_new]), "c") == []


def test_a_renewed_nodes_secret_still_marks_the_usage():
    _, _, secrets = rg.rke2_config(["a"], DECLS, CLUSTER, creds("a"), nodes_secret="web-registries-r2")
    nodes = as_server(next(s for s in secrets if s["metadata"]["name"] == "web-registries-r2"))
    assert rg.cluster_uses(Kube([nodes]), "web", "web", "a")


@pytest.mark.parametrize("cur,nxt,want", [
    ("CA-A\n", None, "CA-A\n"),
    ("CA-A", None, "CA-A"),                          # tel quel : rien à renouveler
    ("CA-A\n", "CA-B\n", "CA-A\nCA-B\n"),
    ("CA-A", "CA-B\n", "CA-A\nCA-B\n"),
    ("CA-A\nCA-B\n", "CA-B\n", "CA-A\nCA-B\n"),      # déjà là
])
def test_ca_bundle(cur, nxt, want):
    assert rg.ca_bundle({"ca_pem": cur, "ca_next_pem": nxt}) == want


def test_the_nodes_trust_both_cas_during_a_transition():
    d = dict(DECLS["a"], ca_next_pem="CA-B\n")
    _, _, secrets = rg.rke2_config(["a"], {"a": d}, CLUSTER, creds("a"))
    nodes = next(s for s in secrets if s["metadata"]["name"] == "web-registries")
    ca = next(s for s in secrets if s["metadata"]["name"] == "web-registry-a-ca")
    assert nodes["stringData"]["ca-a.crt"] == "CA-A\nCA-B\n" == ca["stringData"]["ca.crt"]


def test_ca_next_is_for_a_bootstrap_registry_only():
    with pytest.raises(rg.dp.ProfileError) as e:
        rg.check_decl({"kind": "external", "endpoint": "r.client", "ca_next_pem": "x"})
    assert any(w == "ca_next_pem" for w, _r in e.value.errors)


# --- containerd-registry ------------------------------------------------------------

CURRENT = {"Mirrors": {"quay.io": {"Endpoints": ["https://172.16.1.11:5005"], "Rewrites": None}},
           "Configs": {"172.16.1.11:5005": {"Auth": None, "TLS": {"CAFile": "", "InsecureSkipVerify": False}}},
           "Auths": None}


def test_merge_keeps_the_other_entries_and_remove_takes_only_ours():
    """Mutation « réglage écrasé au lieu d'être fusionné »."""
    value, found = rg.containerd_merge(json.dumps(CURRENT), DECLS["a"], {"username": "pull", "password": PULL_PW})
    got = json.loads(value)
    assert found == []
    assert got["Mirrors"]["quay.io"] == CURRENT["Mirrors"]["quay.io"]
    assert got["Configs"]["172.16.1.11:5005"] == CURRENT["Configs"]["172.16.1.11:5005"]
    assert got["Mirrors"]["docker.io"] == {"Endpoints": ["https://172.16.2.76"], "Rewrites": {"^(.*)$": "docker.io/$1"}}
    assert got["Configs"]["172.16.2.76"]["Auth"]["Password"] == PULL_PW
    assert PULL_PW not in hs.redact("containerd-registry", value)               # masqué dans le résultat
    back, _ = rg.containerd_merge(value, DECLS["a"], remove=True)
    assert json.loads(back) == CURRENT


def test_merge_into_an_empty_setting_and_twice_is_stable():
    once, _ = rg.containerd_merge("", DECLS["b"], {"username": "pull", "password": PULL_PW})
    twice, _ = rg.containerd_merge(once, DECLS["b"], {"username": "pull", "password": PULL_PW})
    assert once == twice and json.loads(once)["Auths"] is None


def test_a_mirror_rewritten_otherwise_is_a_conflict():
    cur = {"Mirrors": {"docker.io": {"Endpoints": ["https://other"], "Rewrites": {"^(.*)$": "x/$1"}}}, "Configs": {}}
    value, found = rg.containerd_merge(json.dumps(cur), DECLS["a"], {"username": "u", "password": "p"})
    assert [f["code"] for f in found] == ["harvester-nodes-mirror-conflict"]
    assert json.loads(value)["Mirrors"]["docker.io"] == cur["Mirrors"]["docker.io"]


@pytest.mark.parametrize("bad", ["not json", "[1]"])
def test_an_unreadable_setting_is_refused(bad):
    with pytest.raises(ValueError):
        rg.containerd_merge(bad, DECLS["a"], {"username": "u", "password": "p"})


@pytest.mark.parametrize("cur,pem,remove,want", [
    ("", "CA-A", False, "CA-A\n"),
    ("CA-X\n", "CA-A", False, "CA-X\nCA-A\n"),
    ("CA-X\nCA-A\n", "CA-A", False, "CA-X\nCA-A\n"),
    ("CA-X\nCA-A\n", "CA-A", True, "CA-X\n"),
    ("CA-X\n", "CA-A", True, "CA-X\n"),
    ("CA-X\n", "", False, "CA-X\n"),
])
def test_additional_ca_merge(cur, pem, remove, want):
    assert rg.additional_ca_merge(cur, pem, remove) == want
