"""2.10.2, revue de la mise à jour des identités CAPHV (M-1 à M-6).

- M-1 : `get` n'est ajouté qu'au Role qui liste déjà les images pour
  l'identité ; une image passée dans un autre namespace est un constat
  bloquant `identity-image-namespace-changed`.
- M-2 : la console n'applique que les changements de l'aperçu encore
  identiques ; un changement apparu depuis attend un nouvel aperçu.
- M-3 : le Role change entre le relevé et l'écriture (disparu, plus à la
  console, plus de `list`, écriture défaite).
- M-4 : une lecture refusée (Forbidden) est le constat du cluster concerné.
- M-5 : le contrôle du démarrage ne rend que les clusters du relevé de la
  personne.
- M-6 : relevé partagé, clé limitée à `name`, relecture forcée bornée.
"""

import copy
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(ROOT / "tests" / "api"))
import capi_identity as ci  # noqa: E402
import test_capi_identity_update_2102 as base  # noqa: E402
from test_capi_identity_update_2102 import IK, ROLE, args, capi_cluster, cli, image, steps, wapp, world  # noqa: E402,F401

ROLE_KEY = (ROLE, "default", "caphv-identity-web")
PREVIEW = "web/web=default/caphv-identity-web/image-abc"


def run_update(cli, k, monkeypatch, **kw):
    monkeypatch.setattr(cli, "kube_from", lambda a: (k, "/x", None))
    monkeypatch.setattr(cli, "_refuse_during_uninstall", lambda a, sid: False)
    return cli.cmd_identity_update(args(**kw))


def moved_to_vms_namespace(k):
    """La topologie prend une image du namespace des VMs (`web-vms`), où
    l'identité ne liste pas les images : son Role `caphv-identity-web` y
    existe et porte les étiquettes, mais n'a pas la règle `list` des images."""
    k.objs[("virtualmachineimages.harvesterhci.io", "web-vms", "image-new")] = image("web-vms", "image-new", "n.qcow2")
    k.objs[("clusters.cluster.x-k8s.io", "web", "web")] = capi_cluster("web", "web", ["web-vms/image-new"])


# ---------------------------------------------------------------------------
# M-1
# ---------------------------------------------------------------------------

def test_an_image_moved_to_a_namespace_the_identity_does_not_list_is_blocked(cli, world, monkeypatch, capsys):
    k = world()
    moved_to_vms_namespace(k)
    assert ci.owned(k.objs[(ROLE, "web-vms", "caphv-identity-web")], "web", "web")
    (c,) = cli.identity_report(k)["clusters"]
    assert c["state"] == "blocked" and c["changes"] == []
    assert c["findings"] == [{"code": "identity-image-namespace-changed", "level": "block",
                              "facts": {"cluster": "web/web", "role": "web-vms/caphv-identity-web",
                                        "image": "web-vms/image-new"}}]
    before = copy.deepcopy(k.objs)
    assert run_update(cli, k, monkeypatch) == cli.EXIT_BLOCKED
    assert not k.patches and k.objs == before
    assert steps(capsys)[-1][2] == "updated 0 rule(s); 0 failed; 1 need a manual fix"


@pytest.mark.parametrize("label,rule,ok", [
    ("list on images", {"apiGroups": ["harvesterhci.io"], "resources": ["virtualmachineimages"], "verbs": ["list"]}, True),
    ("wildcards", {"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}, True),
    ("get only", {"apiGroups": ["harvesterhci.io"], "resources": ["virtualmachineimages"], "verbs": ["get"]}, False),
    ("list on named images", {"apiGroups": ["harvesterhci.io"], "resources": ["virtualmachineimages"],
                              "verbs": ["list"], "resourceNames": ["image-abc"]}, False),
    ("list on keypairs", {"apiGroups": ["harvesterhci.io"], "resources": ["keypairs"], "verbs": ["list"]}, False),
])
def test_what_lists_images(label, rule, ok):
    assert ci.grants_image_list({"rules": [rule]}) is ok, label


# ---------------------------------------------------------------------------
# M-3 : le Role change entre le relevé et l'écriture
# ---------------------------------------------------------------------------

def _gone(k):
    del k.objs[ROLE_KEY]


def _foreign(k):
    k.objs[ROLE_KEY]["metadata"]["labels"] = {}


def _no_list(k):
    k.objs[ROLE_KEY]["rules"] = [r for r in k.objs[ROLE_KEY]["rules"] if r["resources"] != ["virtualmachineimages"]]


class Reverting(IK):
    """Écriture acceptée puis défaite (contrôleur, webhook de mutation) :
    la relecture d'après ne montre pas la règle."""

    def patch(self, kind, ns, name, patch, dry_run=False, secret=False):
        rules = copy.deepcopy(self.objs[(kind, ns, name)]["rules"])
        super().patch(kind, ns, name, patch, dry_run=dry_run, secret=secret)
        self.objs[(kind, ns, name)]["rules"] = rules


@pytest.mark.parametrize("label,between,reverting,why", [
    ("Role gone", _gone, False, "identity-role-missing: Role default/caphv-identity-web is gone"),
    ("Role no longer the console's", _foreign, False,
     "identity-role-foreign: Role default/caphv-identity-web does not carry the labels of web/web"),
    ("Role no longer lists images", _no_list, False,
     "identity-image-namespace-changed: Role default/caphv-identity-web no longer lists images"),
    ("write undone", None, True,
     "Role default/caphv-identity-web does not grant get on image image-abc after the update"),
])
def test_a_role_changed_between_report_and_apply(cli, world, monkeypatch, capsys, label, between, reverting, why):
    k = world()
    if reverting:
        k.__class__ = Reverting
    real = cli.identity_report

    def report_then_change(kube, only=None):
        rep = real(kube, only)
        if between:
            between(kube)
        return rep
    monkeypatch.setattr(cli, "identity_report", report_then_change)
    assert run_update(cli, k, monkeypatch, change=[PREVIEW]) == cli.EXIT_FAIL, label
    out = steps(capsys)
    assert ["identity-web/web", "error", why] in out, (label, out)
    assert out[-1][2] == "updated 0 rule(s); 1 failed; 0 need a manual fix", label
    assert len(k.patches) == (1 if reverting else 0), label


# ---------------------------------------------------------------------------
# M-2 : seulement les changements de l'aperçu, encore identiques
# ---------------------------------------------------------------------------

def test_the_previewed_change_is_applied(cli, world, monkeypatch, capsys):
    k = world()
    assert [ci.change_key(c["cluster"], x) for c in cli.identity_report(k)["clusters"] for x in c["changes"]] == [PREVIEW]
    assert run_update(cli, k, monkeypatch, change=[PREVIEW]) == cli.EXIT_OK
    assert [(p[1], p[2]) for p in k.patches] == [("default", "caphv-identity-web")]
    assert steps(capsys)[-1][1:] == ["done", "updated 1 rule(s); 0 failed; 0 need a manual fix"]


def test_a_change_that_appeared_after_the_preview_waits(cli, world, monkeypatch, capsys):
    """Aperçu montré pour web seul ; api est devenu périmé depuis : il n'est
    pas touché, il attend un nouvel aperçu."""
    k = world(clusters=(("web", "web", "default/image-abc"), ("api", "api", "default/image-abc")))
    assert run_update(cli, k, monkeypatch, change=[PREVIEW]) == cli.EXIT_BLOCKED
    assert {(p[1], p[2]) for p in k.patches} == {("default", "caphv-identity-web")}
    out = steps(capsys)
    assert ["identity-api/api", "error", "not in the preview: get on image image-abc in Role "
            "default/caphv-identity-api left for a new preview"] in out
    assert out[-1][2] == "updated 1 rule(s); 0 failed; 0 need a manual fix; 1 changed since the preview"


def test_an_image_changed_after_the_preview_is_not_applied(cli, world, monkeypatch, capsys):
    k = world()
    k.objs[("virtualmachineimages.harvesterhci.io", "default", "image-new")] = image("default", "image-new", "n")
    k.objs[("clusters.cluster.x-k8s.io", "web", "web")] = capi_cluster("web", "web", ["default/image-new"])
    assert run_update(cli, k, monkeypatch, change=[PREVIEW]) == cli.EXIT_BLOCKED
    assert not k.patches
    out = steps(capsys)
    assert ["identity", "error", f"changed since the preview, not applied: {PREVIEW}"] in out
    assert out[-1][2] == "updated 0 rule(s); 0 failed; 0 need a manual fix; 2 changed since the preview"


def test_a_previewed_change_already_made_is_said_done(cli, world, monkeypatch, capsys):
    """Deux envois du même aperçu (double clic, deux personnes) : le second
    trouve la règle posée et ne fait rien."""
    k = world()
    assert run_update(cli, k, monkeypatch, change=[PREVIEW]) == cli.EXIT_OK
    n = len(k.patches)
    assert run_update(cli, k, monkeypatch, change=[PREVIEW]) == cli.EXIT_OK
    assert len(k.patches) == n
    assert ["identity-web/web", "done", "Role default/caphv-identity-web already grants get on image image-abc"] \
        in steps(capsys)


@pytest.mark.parametrize("bad", ["web/web", "web/web=default/image-abc", "Web/web=default/r/i", "a/b=c/d/e/f"])
def test_a_malformed_change_is_refused(cli, world, monkeypatch, capsys, bad):
    k = world()
    monkeypatch.setattr(cli, "kube_from", lambda a: (k, "/x", None))
    monkeypatch.setattr(cli, "_refuse_during_uninstall", lambda a, sid: False)
    assert cli.main(["identity-update", "--kubeconfig", "/x", "--change", bad]) == cli.EXIT_BLOCKED
    assert not k.patches and "--change expects" in steps(capsys)[-1][2]


@pytest.mark.parametrize("label,body,code,extra", [
    ("preview list", {"changes": [PREVIEW]}, 201, ["--change", PREVIEW]),
    ("duplicates sent once", {"changes": [PREVIEW, PREVIEW]}, 201, ["--change", PREVIEW]),
    ("no list", {}, 400, None),
    ("empty list", {"changes": []}, 400, None),
    ("not a string", {"changes": [{"x": 1}]}, 400, None),
    ("option smuggled", {"changes": ["--dry-run"]}, 400, None),
])
def test_the_route_sends_only_the_previewed_changes(wapp, label, body, code, extra):
    r = wapp.app.test_client().post("/api/capi/h1/identity-update", json=body)
    assert r.status_code == code, (label, r.get_json())
    if extra:
        (call,) = wapp._test_calls
        assert call[1] == ["harvester-capi", "identity-update", "--cluster", "h1"] + extra, label
    else:
        assert wapp._test_calls == [] and r.get_json()["code"] == "invalid-changes", label


# ---------------------------------------------------------------------------
# M-4 : lecture refusée = constat du cluster concerné
# ---------------------------------------------------------------------------

class Forbidding(IK):
    deny = ()

    def get(self, kind, ns, name, managed_fields=False):
        if (kind, ns) in self.deny:
            raise self.err(f'Error from server (Forbidden): {kind} "{name}" is forbidden: User "u" cannot get')
        return super().get(kind, ns, name)

    def list(self, kind, ns=None, selector=None, missing_ok=True):
        if (kind, None) in self.deny:
            raise self.err(f'Error from server (Forbidden): {kind} is forbidden: User "u" cannot list')
        return super().list(kind, ns, selector, missing_ok)


@pytest.mark.parametrize("label,deny,states", [
    ("account of one cluster", {("serviceaccounts", "api")}, {"web/web": "outdated", "api/api": "blocked"}),
    ("images", {("virtualmachineimages.harvesterhci.io", None)}, {"web/web": "blocked", "api/api": "blocked"}),
    ("CAPI providers", {("capiproviders.turtles-capi.cattle.io", None)}, {"web/web": "blocked", "api/api": "blocked"}),
])
def test_a_forbidden_read_is_the_finding_of_its_cluster(cli, world, label, deny, states):
    k = world(clusters=(("web", "web", "default/image-abc"), ("api", "api", "default/image-abc")))
    k.__class__ = Forbidding
    k.deny = deny
    rep = cli.identity_report(k)
    got = {c["cluster"]: c for c in rep["clusters"]}
    assert {n: c["state"] for n, c in got.items()} == states, label
    for n, st in states.items():
        if st == "blocked":
            (f,) = got[n]["findings"]
            assert f["code"] == "identity-unreadable" and f["facts"]["cluster"] == n, label
            assert "forbidden" in f["facts"]["reason"], label


def test_another_read_error_still_fails_the_report(cli, world):
    k = world()
    k.__class__ = type("Broken", (IK,), {"get": lambda self, *a, **kw: (_ for _ in ()).throw(
        self.err("etcdserver: request timed out"))})
    with pytest.raises(cli.KubeError):
        cli.identity_report(k)


# ---------------------------------------------------------------------------
# M-5 / M-6 : route du relevé
# ---------------------------------------------------------------------------

def test_the_startup_result_only_names_the_clusters_of_the_callers_report(wapp, monkeypatch):
    monkeypatch.setattr(wapp, "_capi_run_json", lambda cmd, spec=None, timeout=180: (
        ({"unreachable": False, "clusters": [{"cluster": "web/web", "state": "outdated"}]}, 0), None))
    monkeypatch.setitem(wapp.CAPI_IDENTITY_STARTUP, "h1", {"at": 5.0, "unreachable": False,
                                                           "outdated": ["web/web", "secret/team-b"],
                                                           "blocked": ["hidden/x"]})
    r = wapp.app.test_client().get("/api/capi/h1/identities?fresh=1")
    assert r.get_json()["startup"] == {"at": 5.0, "unreachable": False, "outdated": ["web/web"], "blocked": []}


def test_the_report_is_shared_by_name_with_a_bounded_forced_read():
    src = (ROOT / "web" / "app.py").read_text()
    m = re.search(r'@app.route\("/api/capi/<cluster>/identities"\)\n@requires_auth\n@shared_read\((.*)\)\n', src)
    assert m and 'query_keys=("name",)' in m.group(1) and "fresh_min=CAPI_IDENTITY_FRESH_MIN" in m.group(1)
