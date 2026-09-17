"""v1.28.0 — création de machines virtuelles.

La console savait tout éditer d'une VM mais pas en créer une : il fallait
passer par l'UI Harvester ou par une déclaration Terraform.

Le panneau de création ne réécrit AUCUN formulaire : il rejoue les huit
sections de l'éditeur sur un squelette. Ces tests couvrent ce que cette
réutilisation ne garantit pas toute seule, c'est-à-dire tout ce qui n'existe
qu'à la création : les noms d'instances, la déclinaison du manifeste, et les
refus du point d'entrée.

Le piège payé en réel, et verrouillé ici : les noms de PVC portent le nom de
la VM. Créer trois VMs à partir du même manifeste sans les réécrire ferait
échouer les deux dernières sur des PVC déjà pris, ou pire les ferait
partager un disque.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402

JS = ROOT / "web" / "static" / "js"


def manifest(name="web"):
    vct = [{"metadata": {"name": f"{name}-disk-0-abcde",
                         "annotations": {"harvesterhci.io/imageId": "default/image-x"}},
            "spec": {"storageClassName": "lh-1234", "volumeMode": "Block",
                     "resources": {"requests": {"storage": "10Gi"}}}}]
    return {
        "apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
        "metadata": {"name": name, "namespace": "default",
                     "resourceVersion": "4242", "uid": "u-1",
                     "annotations": {
                         "harvesterhci.io/volumeClaimTemplates": json.dumps(vct)}},
        "spec": {"runStrategy": "Halted", "template": {"spec": {
            "hostname": name,
            "volumes": [{"name": "disk-0",
                         "persistentVolumeClaim": {"claimName": f"{name}-disk-0-abcde"}}],
        }}},
        "status": {"printableStatus": "Running"},
    }


# ---------------------------------------------------------------------------
# Noms d'instances
# ---------------------------------------------------------------------------

def test_a_single_instance_keeps_the_name_asked_for():
    """Suffixer « web » en « web-01 » quand on n'en demande qu'une
    surprendrait."""
    assert wapp._instance_names("web", 1) == ["web"]


def test_several_instances_are_numbered():
    assert wapp._instance_names("web", 3) == ["web-01", "web-02", "web-03"]


def test_numbering_pads_to_two_digits():
    names = wapp._instance_names("web", 12)
    assert names[0] == "web-01" and names[-1] == "web-12"


# ---------------------------------------------------------------------------
# Déclinaison du manifeste
# ---------------------------------------------------------------------------

def test_each_instance_gets_its_own_pvc():
    """LE piège du multi-VM : sans réécriture, les trois VMs réclameraient
    le même PVC."""
    base = manifest("web")
    pvcs, claims = set(), set()
    for name in wapp._instance_names("web", 3):
        vm = wapp._vm_manifest_for_instance(base, name, "default", False)
        vct = json.loads(vm["metadata"]["annotations"]["harvesterhci.io/volumeClaimTemplates"])
        pvc = vct[0]["metadata"]["name"]
        claim = vm["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"]
        assert pvc.startswith(name + "-"), pvc
        assert claim == pvc, "le volume doit suivre le renommage du PVC"
        pvcs.add(pvc)
        claims.add(claim)
    assert len(pvcs) == 3, f"PVC en collision : {pvcs}"


def test_the_source_manifest_is_never_mutated():
    """Trois déclinaisons partent du MÊME objet : le modifier en place
    ferait dériver les suivantes."""
    base = manifest("web")
    before = json.dumps(base, sort_keys=True)
    for name in ("web-01", "web-02"):
        wapp._vm_manifest_for_instance(base, name, "default", True)
    assert json.dumps(base, sort_keys=True) == before


def test_the_hostname_follows_the_vm_name():
    vm = wapp._vm_manifest_for_instance(manifest("web"), "web-02", "default", True)
    assert vm["spec"]["template"]["spec"]["hostname"] == "web-02"


def test_a_hostname_chosen_by_hand_is_kept():
    base = manifest("web")
    base["spec"]["template"]["spec"]["hostname"] = "mon-hote"
    vm = wapp._vm_manifest_for_instance(base, "web-02", "default", True)
    assert vm["spec"]["template"]["spec"]["hostname"] == "mon-hote"


def test_the_start_checkbox_drives_the_run_strategy():
    on = wapp._vm_manifest_for_instance(manifest(), "a", "default", True)
    off = wapp._vm_manifest_for_instance(manifest(), "a", "default", False)
    assert on["spec"]["runStrategy"] == "Always"
    assert off["spec"]["runStrategy"] == "Halted"


def test_server_side_fields_are_stripped():
    """`resourceVersion`, `uid` et `status` viennent d'un objet lu sur le
    cluster ; les renvoyer en création fait échouer l'apiserver."""
    vm = wapp._vm_manifest_for_instance(manifest(), "a", "default", True)
    assert "resourceVersion" not in vm["metadata"]
    assert "uid" not in vm["metadata"]
    assert "status" not in vm
    assert vm["metadata"]["namespace"] == "default"


# ---------------------------------------------------------------------------
# Le point d'entrée
# ---------------------------------------------------------------------------

@pytest.fixture
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc, **k: True)
    monkeypatch.setattr(wapp, "load_config", lambda: {
        "clusters": [{"name": "harv-probe", "kubeconfig": "/nonexistent.yaml"}]})

    def fake_track(label, cluster, worker, *args):
        calls.append({"label": label, "args": args})
        return "act-0001"

    monkeypatch.setattr(wapp, "track_action", fake_track)
    return calls


def post(body):
    with wapp.app.test_client() as c:
        r = c.post("/api/vms/harv-probe/create", json=body)
    return r.status_code, r.get_json()


def test_a_valid_request_is_accepted_and_tracked(spawned):
    status, d = post({"namespace": "default", "name": "web", "count": 3,
                      "manifest": manifest()})
    assert status == 202
    assert d["names"] == ["web-01", "web-02", "web-03"]
    assert d["action_id"] == "act-0001"


@pytest.mark.parametrize("body,reason", [
    ({"namespace": "", "name": "web", "manifest": {"spec": {}}}, "namespace"),
    ({"namespace": "default", "name": "Web_1", "manifest": {"spec": {}}}, "nom majuscule"),
    ({"namespace": "default", "name": "web"}, "manifeste absent"),
    ({"namespace": "default", "name": "web", "manifest": {}}, "manifeste sans spec"),
    ({"namespace": "default", "name": "web", "count": 0,
      "manifest": {"spec": {}}}, "compte nul"),
    ({"namespace": "default", "name": "web", "count": 999,
      "manifest": {"spec": {}}}, "compte hors borne"),
    ({"namespace": "default", "name": "web", "count": "trois",
      "manifest": {"spec": {}}}, "compte non numerique"),
])
def test_bad_requests_are_refused(body, reason, spawned):
    status, _ = post(body)
    assert status == 400, reason
    assert spawned == [], f"rien ne doit partir pour : {reason}"


def test_a_name_too_long_to_be_numbered_is_refused(spawned):
    """« mon-app » x12 donne « mon-app-12 », mais un nom déjà à la limite
    des 63 caractères ne passerait plus une fois suffixé."""
    status, d = post({"namespace": "default", "name": "a" * 62, "count": 5,
                      "manifest": manifest()})
    assert status == 400
    assert "RFC 1123" in d["error"]
    assert spawned == []


def test_an_unreachable_cluster_says_so_instead_of_trying(monkeypatch):
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc, **k: False)
    monkeypatch.setattr(wapp, "load_config", lambda: {
        "clusters": [{"name": "harv-probe", "kubeconfig": "/nonexistent.yaml"}]})
    status, d = post({"namespace": "default", "name": "web",
                      "manifest": manifest()})
    assert status == 200 and d["unreachable"] is True


# ---------------------------------------------------------------------------
# La storage class d'une image : lue, jamais fabriquée
# ---------------------------------------------------------------------------

def test_the_image_endpoint_exposes_its_storage_class():
    """Sans elle, le formulaire devait deviner le nom."""
    src = (ROOT / "web" / "app.py").read_text()
    reducer = src.split("def _reduce_image", 1)[1].split("\n\n\n", 1)[0]
    assert '"storage_class"' in reducer
    assert '"virtual_size"' in reducer


def test_the_editor_reads_the_storage_class_instead_of_building_it():
    """Harvester crée une storage class par image, sous DEUX conventions :
    `longhorn-image-xxxx` pour les anciennes, `lh-<uuid>` pour celles à
    backend backingimage. Fabriquer le nom donnait un PVC bloqué en Pending
    sur « storageclass not found » — vécu sur harv1 avec image-fvkzr."""
    src = (JS / "vm-edit.js").read_text()
    assert "imageStorageClass.get(item.image)" in src, \
        "la classe doit venir de l'image, pas d'une concaténation"
    assert "primeImageStorageClasses" in src


# ---------------------------------------------------------------------------
# Le panneau
# ---------------------------------------------------------------------------

def test_the_create_panel_replays_the_editor_sections():
    """Le cœur du parti pris : aucun formulaire dupliqué. Si ce test casse,
    c'est que la création a commencé à diverger de l'édition."""
    src = (JS / "vm-create.js").read_text()
    for fn in ("VMEdit.renderSectionHtml", "VMEdit.wireSection",
               "VMEdit.buildPatch", "VMEdit.SECTIONS"):
        assert fn in src, fn


def test_the_editor_exports_what_creation_needs():
    src = (JS / "vm-edit.js").read_text()
    exports = src.split("  return {\n    open,", 1)[1][:400]
    for name in ("SECTIONS", "renderSectionHtml", "buildPatch", "wireSection"):
        assert name in exports, name


def test_create_mode_does_not_patch_a_vm_that_does_not_exist():
    src = (JS / "vm-edit.js").read_text()
    assert "createMode" in src
    wire = src.split("function wireSection", 1)[1][:9000]
    assert "if (createMode) return;" in wire, \
        "le bouton Appliquer d'une section n'a rien à patcher en création"


def test_nulls_are_stripped_from_the_manifest():
    """Un merge patch met une clé à `null` pour la SUPPRIMER ; un manifeste
    de création qui en contient est refusé par l'apiserver."""
    src = (JS / "vm-create.js").read_text()
    assert "function stripNulls" in src
    assert "stripNulls(out)" in src
