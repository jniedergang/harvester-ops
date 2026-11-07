"""2.7.0 (tâche 5) : moteur pur du « Défaire le déploiement » (deploy_undo).

Chaîne de reprises remontée (32 maillons, boucle refusée), collecte de ce
que la chaîne a créé (dernier état gagnant, adopté sans marque jamais
touché), exceptions décochées (registre, création non prouvée, déclaration
modifiée), ordre de retrait et dépendances, phases données au moteur.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import deploy_chain as dch  # noqa: E402
import deploy_undo as du  # noqa: E402

A, B, C = "aaaaaaaaaaa1", "aaaaaaaaaaa2", "aaaaaaaaaaa3"


def item(kind, ref, created=True, **kw):
    out = {"status": "done", "created": created, "undo": {"kind": kind, "ref": ref}}
    out.update(kw)
    return out


def result(phases, resumes=None, plan="site"):
    return {"plan": plan, "resumes": resumes, "phases": [{"id": p, "items": its} for p, its in phases.items()]}


def reader(runs):
    return lambda i: runs.get(i)


def test_chain_of_three_resumes_cluster_made_by_the_first():
    runs = {A: result({"clusters": [item("capi-cluster", "web/web")]}),
            B: result({"clusters": [dict(item("capi-cluster", "web/web"), message="already done (checked)")]},
                      resumes=A),
            C: result({"clusters": [dict(item("capi-cluster", "web/web"), checked=True)]}, resumes=B)}
    chain = du.chain(C, reader(runs))
    assert [r["_run"] for r in chain] == [A, B, C]
    got = du.collect(chain)
    # vu en réel : la fenêtre dit « créé par » ; c'est le premier maillon
    # qui l'a créé, les reprises n'ont fait que recopier la marque
    assert [(u["kind"], u["ref"], u["run"]) for u in got] == [("capi-cluster", "web/web", A)]
    assert got[0]["proven"] is True and got[0]["default"] is True


@pytest.mark.parametrize("runs,code", [
    ({A: {"resumes": B}, B: {"resumes": A}}, "undo-chain-loop"),
    ({A: {"resumes": A}}, "undo-chain-loop"),
])
def test_chain_refusals(runs, code):
    with pytest.raises(du.UndoError) as e:
        du.chain(A, reader(runs))
    assert e.value.code == code


def test_chain_is_bounded():
    ids = [f"{i:012x}" for i in range(40)]
    runs = {ids[i]: {"resumes": ids[i + 1] if i + 1 < len(ids) else None} for i in range(len(ids))}
    with pytest.raises(du.UndoError) as e:
        du.chain(ids[0], reader(runs))
    assert e.value.code == "undo-chain-too-long"
    assert len(du.chain(ids[8], reader(runs))) == 32


def test_a_missing_link_stops_the_chain():
    assert [r["_run"] for r in du.chain(B, reader({B: {"resumes": A}}))] == [B]


@pytest.mark.parametrize("it,expected", [
    (item("capi-cluster", "web/web", created=False), []),                    # adopté : jamais touché
    ({"status": "done", "message": "already present, adopted"}, []),
    ({"status": "done", "created": True}, []),                               # sans undo : rien
    (item("unknown", "x"), []),
    (item("registry", "harvlab/amorcage"), [("registry", False, du.REGISTRY_NOTE)]),
    (item("chart", "web/web/podinfo"), [("chart", True, "")]),
])
def test_collect(it, expected):
    got = du.collect([dict(result({"x": [it]}), _run=A)])
    assert [(u["kind"], u["default"], u["note"]) for u in got] == expected


def tf_lookup(decls):
    return lambda i: decls.get(i)


def test_declarations_before_2_7_and_modified_since():
    res = dict(result({"terraform": [
        {"template": "cles", "status": "done", "declaration_id": "d1", "revision_applied": 1},  # d'avant 2.7.0
        {"template": "net", "status": "done", "declaration_id": "d2", "revision_applied": 1},   # d'un autre plan
        dict(item("tf-declaration", "d3"), template="disk", revision_applied=2),               # modifiée depuis
        dict(item("tf-declaration", "d4"), template="vm", revision_applied=1),
    ]}), _run=A)
    decls = {"d1": {"origin": {"plan": "site"}, "revision": 1}, "d2": {"origin": {"plan": "autre"}, "revision": 1},
             "d3": {"origin": {"plan": "site", "run": A}, "revision": 3},
             "d4": {"origin": {"plan": "site", "run": A}, "revision": 1}}
    got = {u["ref"]: u for u in du.collect([res], tf_lookup(decls), plan="site")}
    assert set(got) == {"d1", "d3", "d4"}
    assert (got["d1"]["proven"], got["d1"]["default"], got["d1"]["note"]) == (False, False, du.NOT_PROVEN)
    assert (got["d3"]["proven"], got["d3"]["default"]) == (True, False) and du.MODIFIED in got["d3"]["note"]
    assert (got["d4"]["proven"], got["d4"]["default"], got["d4"]["note"]) == (True, True, "")


def test_a_declaration_with_a_run_but_no_mark_is_not_guessed():
    res = dict(result({"terraform": [{"template": "cles", "declaration_id": "d1"}]}), _run=A)
    decls = {"d1": {"origin": {"plan": "site", "run": B}, "revision": 1}}
    assert du.collect([res], tf_lookup(decls), plan="site") == []


UNDOS = du.collect([dict(result({
    "terraform": [item("tf-declaration", "d1")],
    "registry": [item("registry", "harvlab/amorcage")],
    "clusters": [item("capi-cluster", "web/web"), item("capi-cluster", "api/api")],
    "artefacts": [item("policies", "web/web/hops-policies"), item("manifest", "web/web/regles"),
                  item("chart", "web/web/podinfo"), item("chart", "outils/outils/podinfo")],
}), _run=A)])


def test_plan_order_and_dependencies():
    steps = du.plan(UNDOS)
    assert [s["kind"] for s in steps] == ["chart", "chart", "manifest", "policies", "capi-cluster", "capi-cluster",
                                          "tf-declaration"]                       # registre décoché
    web = next(s for s in steps if s["ref"] == "web/web")
    assert web["depends_on"] == ["artefacts:chart-web/web/podinfo", "artefacts:manifest-web/web/regles",
                                 "artefacts:policies-web/web/hops-policies"]
    assert next(s for s in steps if s["ref"] == "api/api")["depends_on"] == []
    # revue 4-8 I2 (décision du propriétaire du produit) : le contenu Terraform
    # peut porter les clusters et le registre ; il attend leur retrait
    assert next(s for s in steps if s["kind"] == "tf-declaration")["depends_on"] == [
        "clusters:capi-cluster-web/web", "clusters:capi-cluster-api/api"]


def test_registry_waits_for_the_clusters_and_terraform_for_both():
    """Revue 4-8 I2 : registre après les clusters, Terraform après les deux."""
    steps = du.plan(UNDOS, include={u["id"] for u in UNDOS})
    reg = next(s for s in steps if s["kind"] == "registry")
    assert reg["depends_on"] == ["clusters:capi-cluster-web/web", "clusters:capi-cluster-api/api"]
    tf = next(s for s in steps if s["kind"] == "tf-declaration")
    assert tf["depends_on"] == ["clusters:capi-cluster-web/web", "clusters:capi-cluster-api/api",
                                "registry:registry-harvlab/amorcage"]


def test_plan_with_an_explicit_choice():
    steps = du.plan(UNDOS, include={"registry-harvlab/amorcage", "capi-cluster-web/web"})
    assert [s["id"] for s in steps] == ["capi-cluster-web/web", "registry-harvlab/amorcage"]
    assert steps[0]["depends_on"] == []                          # artefacts non choisis : pas d'attente


def test_phases_go_on_after_a_failure_and_skip_dependents():
    steps = du.plan(UNDOS)
    calls = []

    def run_item(ctx, state, it):
        calls.append(it.id)
        return dch.ItemOutcome("error" if it.id == "chart-web/web/podinfo" else "done")
    phases = du.phases(steps, run_item, lambda *a: False)
    assert [p.id for p in phases] == ["artefacts", "clusters", "terraform"]

    class Ctx:
        run_id = A

        def emit_step(self, *a):
            pass

        def set_result(self, r):
            pass

        def cancelled(self):
            return False

    import secret_store
    out = dch.run(Ctx(), phases, order=du.ORDER, secrets=secret_store.LocalStore())
    st = {i["id"]: i["status"] for p in out["phases"] for i in p.get("items") or []}
    assert st["capi-cluster-web/web"] == "skipped" and st["capi-cluster-api/api"] == "done"
    # revue 4-8 I2 : un cluster resté là garde le contenu Terraform qui peut le porter
    assert st["tf-declaration-d1"] == "skipped"
    # la dernière phase n'a rien fait : `error` pour le moteur, que le
    # lanceur du défaire rend `partial` (une partie est défaite)
    # 2.7.1 (m1) : une phase déjà partielle laisse le Défaire `partial`
    assert out["status"] == "partial" and "capi-cluster-web/web" not in calls and "tf-declaration-d1" not in calls


def test_declarations_name_every_cluster_of_the_chain_created_or_adopted():
    """Revue finale 2.7.0 (I1) : le contenu Terraform peut porter tout cluster
    que la phase Clusters de la chaîne nomme, créé par elle ou repris ; la
    collecte les donne à chaque déclaration (`carries`)."""
    runs = [dict(result({"terraform": [item("tf-declaration", "d1")],
                         "clusters": [item("capi-cluster", "web/web", cluster="web"),
                                      {"status": "done", "cluster": "api", "ref": "api/api", "created": False,
                                       "message": "already present, adopted"}]}), _run=A),
            dict(result({"clusters": [{"status": "error", "cluster": "outils", "created": False}]}, resumes=A),
                 _run=B)]
    got = {u["kind"]: u for u in du.collect(runs)}
    assert got["tf-declaration"]["carries"] == ["api/api", "outils/outils", "web/web"]
    assert "carries" not in got["capi-cluster"]
    steps = du.plan(list(got.values()), include={got["tf-declaration"]["id"]})
    assert steps[0]["carries"] == ["api/api", "outils/outils", "web/web"]    # le lanceur la relit
