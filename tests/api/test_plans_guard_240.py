"""2.4.0, revue du bloc 3 et décisions 160-161 : ce qu'un plan peut écrire.

* B3-I1 : une valeur de variable ne change jamais la structure d'un modèle
  en HCL brut relu par un admin (saut de ligne puis marqueur de heredoc,
  `$` ou `%` d'une valeur suivi d'un `{`, valeur qui neutralise le `${` du
  modèle) : refus, le résultat étant relu par la lecture de tf_guard ;
* B3-I3 : un seul contrôle de structure, celui de web/tf_guard.py (2.3.1),
  pour les déclarations comme pour les modèles et la chaîne d'un plan
  (contrôlé dans `_tf_decl_launch`, que la chaîne appelle) ;
* 160 : réseaux, classes de stockage et pools d'adresses (et ce qu'un code
  libre en crée) réservés à l'admin, côté serveur, chaîne comprise ;
* 161 : un opérateur lance un plan dont le code libre a été écrit par un
  admin (valeurs neutralisées) ; l'auteur du modèle est consigné.

Mutations jouées en fin de fichier : relecture du rendu retirée, contrôle
de rôle retiré, contrôle tf_guard retiré de `_tf_decl_launch`.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp  # noqa: E402
import deploy_plans as dp  # noqa: E402
import hcl_guard as hg  # noqa: E402
import tf_guard as g  # noqa: E402
import test_deploy_routes_240 as tr  # noqa: E402
import test_deploy_runner_240 as trun  # noqa: E402

env = tr.env
world = trun.world
KEY = tr.KEY
ABS = "abspath(path.root)"
STRUCTURE = hg.STRUCTURE_CHANGED


# ---------------------------------------------------------------------------
# B3-I1 : les sondes de la revue, toutes refusées
# ---------------------------------------------------------------------------
PROBES = [
    # (modèle, valeurs, variable en cause, raison)
    ('d = <<EOT\n{{x}}\nEOT\n', {"x": "a\nEOT\n  extra = " + ABS + "\n  ud2 = <<EOT\nb"}, "x",
     "control character or line break"),
    ('d = <<-EOT\n  {{x}}\n  EOT\n', {"x": "hi\n  EOT\nfoo = 1\ny = <<EOT\n"}, "x",
     "control character or line break"),
    ('d = <<EOT\n{{x}}\nb = 1\nEOT\n', {"x": "EOT"}, "x", STRUCTURE),               # marqueur seul
    ('d = <<-EOT\n  {{x}}\n  EOT\n', {"x": "EOT"}, "x", STRUCTURE),                  # marqueur indenté
    ('n = "{{a}}{{b}}"', {"a": "$", "b": "{" + ABS + "}"}, "a", STRUCTURE),          # $ + {
    ('n = "{{a}}{{b}}"', {"a": "%", "b": "{ if true }x%{ endif }"}, "a", STRUCTURE),  # % + {
    ('n = "{{a}}{{b}}"', {"a": "$", "b": '{file("/etc/hosts")}'}, "a, b", STRUCTURE),
    ('n = "{{a}}{x}"', {"a": "%"}, "a", STRUCTURE),
    ('n = "{{a}}${local.x}"', {"a": "$"}, "a", STRUCTURE),                          # neutralise le modèle
    ('d = <<EOT\n{{a}}{{b}}\nEOT\n', {"a": "$", "b": "{timestamp()}"}, "a", STRUCTURE),
    ('n = "{{x}}"', {"x": 'a"\n description = "b'}, "x", "control character or line break"),
    ('n = "{{x}}"', {"x": "a\tb"}, "x", "control character or line break"),
]


@pytest.mark.parametrize("tf,values,name,why", PROBES)
def test_a_value_never_changes_the_template_structure(tf, values, name, why):
    _out, refused = hg.render(tf, values)
    assert refused == [(name, why)]


@pytest.mark.parametrize("tf,values,expected", [
    ('n = "{{x}}"', {"x": "a$"}, 'n = "a$"'),
    ('n = "{{x}}"', {"x": "50%"}, 'n = "50%"'),
    ('n = "{{a}}-{{b}}"', {"a": "$", "b": "{x}"}, 'n = "$-{x}"'),
    ('n = "{{x}}"', {"x": "${x}"}, 'n = "$${x}"'),
    ('n = "{{x}}"', {"x": "{"}, 'n = "{"'),
    ('d = <<EOT\nhost: {{x}}\nEOT\n', {"x": "EOT2"}, 'd = <<EOT\nhost: EOT2\nEOT\n'),
    ('d = <<EOT\n{{x}}\nEOT\n', {"x": "EOTX"}, 'd = <<EOT\nEOTX\nEOT\n'),
    ('v = {{x}}', {"x": "42"}, 'v = 42'),
])
def test_ordinary_values_still_render(tf, values, expected):
    assert hg.render(tf, values) == (expected, [])


def test_the_probe_files_of_the_review_are_refused():
    """Les sondes laissées par la revue dans /tmp/b3probe (rejouées si
    présentes) : chaque injection de p.py et p2.py est refusée."""
    p2 = Path("/tmp/b3probe/p2.py")
    if not p2.exists():
        pytest.skip("review probes not present")
    t = 'locals {\n  ud = <<EOT\n{{x}}\nEOT\n}\noutput "a" { value = local.ud }\n'
    x = "a\nEOT\n  extra = abspath(path.root)\n  ud2 = <<EOT\nb"
    assert hg.render(t, {"x": x})[1]


def test_a_refused_value_refuses_the_whole_render_of_a_plan():
    t, _ = dp.check_template({"variables": ["a", "b"], "resources": [
        {"id": "a" * 12, "kind": "raw", "spec": {"tf": 'resource "harvester_ssh_key" "k" {\n'
                                                    '  name       = "{{a}}{{b}}"\n'
                                                    '  public_key = "' + KEY + '"\n}\n'}}]})
    plan = {"name": "p", "phases": {"terraform": {"enabled": True, "templates": [{"template": "t"}]}}}
    with pytest.raises(dp.ProfileError) as e:
        dp.render_templates(plan, {"t": t}, {"a": "$", "b": "{" + ABS + "}"})
    assert any(STRUCTURE in why for _w, why in e.value.errors)


# ---------------------------------------------------------------------------
# B3-I3 : une seule règle de structure, celle de tf_guard
# ---------------------------------------------------------------------------
def test_hcl_guard_keeps_no_copy_of_the_tf_guard_rules():
    src = (ROOT / "bin/lib/hcl_guard.py").read_text()
    for gone in ("BLOCKS_RE", "templatefile", "fileexists", "def _lex(", "provisioner|"):
        assert gone not in src, gone
    assert "import tf_guard" in src and "_tfg.char_kinds" in src


@pytest.mark.parametrize("tf", [
    'resource "harvester_ssh_key" "y" { name = templatestring("a", {}) }',
    'resource "harvester_bootstrap" "b" { api_url = "x" }',
    'resource "null_resource" "x" {}',
    'resource "harvester_image" "i" {\n name = "i"\n file_path = "/k"\n}',
    'resource "harvester_ssh_key" "y" {\n  provisioner "local-exec" { command = "id" }\n}',
])
def test_template_structure_is_refused_by_tf_guard(tf):
    assert g.check_resources(tf, "harvester")                     # la règle de toute déclaration
    with pytest.raises(dp.ProfileError):
        dp.check_template({"resources": [{"id": "a" * 12, "kind": "raw", "spec": {"tf": tf}}]})


def test_char_kinds_matches_the_reader():
    t = 'a = "x${b}y" # c\nd = <<EOT\nhi ${e} $${f}\nEOT\n'
    kinds, strings = g.char_kinds(t)
    k = "".join(kinds)
    assert k[t.index('"x')] == g.STRING and k[t.index("b}")] == g.CODE and k[t.index("# c")] == g.COMMENT
    assert k[t.index("hi")] == g.HEREDOC and k[t.index("e}")] == g.CODE and k[t.index("$${f")] == g.HEREDOC
    assert strings == [(4, 12, True)]
    assert g.char_kinds('a = "unterminated') is None


def test_resource_types():
    assert g.resource_types('resource "harvester_network" "n" {}\ndata "harvester_image" "i" {}\n') \
        == ["harvester_network"]
    assert g.resource_types("resource {") == []


# ---------------------------------------------------------------------------
# 160 : genres réservés à l'admin, routes de déclaration
# ---------------------------------------------------------------------------
ADMIN_SPECS = {
    "network": {"name": "plan-test-net", "namespace": "default", "cluster_network_name": "mgmt", "vlan_id": 0},
    "storageclass": {"name": "plan-test-sc", "parameters": {"numberOfReplicas": "1"}},
    "ippool": {"name": "plan-test-pool", "range": [{"start": "10.9.0.10", "end": "10.9.0.20",
                                                    "subnet": "10.9.0.0/24", "gateway": "10.9.0.1"}]},
}


@pytest.fixture
def decl_world(tmp_path, monkeypatch):
    store = wapp._tfs.DeclStore(tmp_path / "tf.db")
    monkeypatch.setattr(wapp, "TF_DECLS", store)
    monkeypatch.setattr(wapp, "TF_WORKSPACES", tmp_path / "ws")
    kc = tmp_path / "kc"
    kc.write_text("apiVersion: v1\n")
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: str(kc) if c == "harv1" else None)
    started = []
    for f in ("_tf_decl_runner", "_tf_apply_runner", "_tf_apply_declaration_runner"):
        monkeypatch.setattr(wapp, f, lambda *a, **kw: started.append(a))
    wapp.app.config["TESTING"] = True
    role = {"v": "admin"}
    monkeypatch.setattr(wapp, "current_role", lambda: role["v"])
    return {"store": store, "started": started, "role": role, "client": wapp.app.test_client(), "kc": str(kc)}


def _res(kind, rid="7" * 12):
    return {"id": rid, "kind": kind, "spec": dict(ADMIN_SPECS[kind])}


@pytest.mark.parametrize("kind", sorted(ADMIN_SPECS))
@pytest.mark.parametrize("role,status", [("operator", 403), ("viewer", 403), ("admin", 201)])
def test_admin_kinds_on_create(decl_world, kind, role, status):
    decl_world["role"]["v"] = role
    r = decl_world["client"].post("/api/tf-declarations", json={"cluster": "harv1", "name": "d",
                                                               "resources": [_res(kind)]})
    assert r.status_code == status, r.get_json()
    if status == 403 and role == "operator":
        body = r.get_json()
        assert body["code"] == "admin-kind" and body["kinds"] == [kind] and body["required"] == "admin"


@pytest.mark.parametrize("kind", sorted(ADMIN_SPECS))
def test_admin_kinds_on_update_plan_apply_and_older_routes(decl_world, kind):
    c, w = decl_world["client"], decl_world
    d = c.post("/api/tf-declarations", json={"cluster": "harv1", "name": "d",
                                            "resources": [_res(kind)]}).get_json()
    w["role"]["v"] = "operator"
    assert c.put(f"/api/tf-declarations/{d['id']}", json={"revision": d["revision"], "name": "x"}).status_code == 403
    for dry in (True, False):
        r = c.post("/api/terraform/harv1/apply_declaration", json={"declaration": {"id": d["id"]}, "dry_run": dry})
        assert r.status_code == 403 and r.get_json()["code"] == "admin-kind"
    r = c.post("/api/terraform/harv1/apply", json={"kind": kind, "spec": ADMIN_SPECS[kind], "dry_run": True})
    assert r.status_code == 403
    r = c.post("/api/terraform/harv1/apply_declaration",
               json={"declaration": {"name": "y", "resources": [_res(kind)]}, "dry_run": True})
    assert r.status_code == 403
    assert w["started"] == []
    # un opérateur ne glisse pas un réseau dans sa déclaration de clés
    f = c.post("/api/tf-declarations", json={"cluster": "harv1", "name": "f", "resources": [
        {"id": "8" * 12, "kind": "ssh_key", "spec": {"name": "k", "public_key": KEY}}]}).get_json()
    r = c.put(f"/api/tf-declarations/{f['id']}", json={"revision": f["revision"], "resources": [
        {"id": "8" * 12, "kind": "ssh_key", "spec": {"name": "k", "public_key": KEY}}, _res(kind)]})
    assert r.status_code == 403
    w["role"]["v"] = "admin"
    r = c.post("/api/terraform/harv1/apply_declaration", json={"declaration": {"id": d["id"]}, "dry_run": True})
    assert r.status_code == 201


def test_the_view_carries_the_admin_kinds(decl_world):
    c = decl_world["client"]
    d = c.post("/api/tf-declarations", json={"cluster": "harv1", "name": "d",
                                            "resources": [_res("network")]}).get_json()
    assert c.get(f"/api/tf-declarations/{d['id']}").get_json()["admin_kinds"] == ["network"]


@pytest.mark.parametrize("tf,kinds", [
    ('resource "harvester_network" "n" {\n name = "n"\n cluster_network_name = "mgmt"\n vlan_id = 0\n}\n',
     ["harvester_network"]),
    ('resource "harvester_setting" "s" {\n name = "x"\n value = "y"\n}\n', ["harvester_setting"]),
    ('resource "harvester_ssh_key" "k" {\n name = "k"\n public_key = "' + KEY + '"\n}\n', []),
])
def test_admin_types_in_free_code(tf, kinds):
    assert wapp._tf_admin_kinds([{"kind": "raw", "spec": {"tf": tf}}]) == kinds


def test_role_rule_table():
    raw = [{"kind": "raw", "spec": {"tf": 'resource "harvester_ssh_key" "k" {}\n'}}]
    net = [{"kind": "network", "spec": {}}]
    rule = wapp._tf_role_refusal
    assert rule(raw, None) is None and rule(net, None) is None                   # ligne de commande
    assert rule(raw, "admin") is None and rule(net, "admin") is None
    assert rule(raw, "operator")["code"] == "raw-admin-only"
    assert rule(raw, "operator", via_plan=True) is None                          # 161
    assert rule(net, "operator", via_plan=True)["code"] == "admin-kind"          # 160, chaîne comprise
    assert rule([{"kind": "raw", "spec": "not a dict"}], "operator", via_plan=True) is None


def test_launch_checks_role_and_guard_for_any_caller(decl_world):
    """La chaîne appelle _tf_decl_launch sans requête : rôle (160/161) et
    tf_guard y sont contrôlés, pas seulement dans la route (revue B3-I3)."""
    st = decl_world["store"]
    kc = decl_world["kc"]
    origin = {"plan": "p", "template": "t", "template_revision": 1, "plan_revision": 1}
    raw_ok = {"id": "9" * 12, "kind": "raw", "spec": {"tf": 'resource "harvester_ssh_key" "k" {\n'
                                                         ' name = "k"\n public_key = "' + KEY + '"\n}\n'}}
    from_plan = st.create("a" * 12, "harv1", "p-t", resources=[raw_ok], origin=origin)
    by_hand = st.create("b" * 12, "harv1", "hand", resources=[raw_ok])
    net = st.create("c" * 12, "harv1", "p-n", resources=[_res("network")], origin=dict(origin, template="n"))
    # 161 : chaîne d'un plan (détenteur + origine), opérateur : permis
    rid, _h = wapp._tf_decl_launch("harv1", kc, from_plan["id"], "plan", holder="d" * 12, role="operator")
    assert rid
    for decl_id, holder, code in ((by_hand["id"], "d" * 12, "raw-admin-only"),      # pas d'origine
                                  (from_plan["id"], None, "raw-admin-only"),       # hors chaîne
                                  (net["id"], "d" * 12, "admin-kind")):            # 160
        with pytest.raises(wapp.TfLaunchError) as e:
            wapp._tf_decl_launch("harv1", kc, decl_id, "plan", holder=holder, role="operator")
        assert e.value.status == 403 and e.value.payload["code"] == code
    bad = st.create("e" * 12, "harv1", "bad", resources=[{"id": "9" * 12, "kind": "raw", "spec": {
        "tf": 'resource "harvester_ssh_key" "k" {\n provisioner "local-exec" { command = "id" }\n}\n'}}])
    with pytest.raises(wapp.TfLaunchError) as e:
        wapp._tf_decl_launch("harv1", kc, bad["id"], "destroy", role="admin")
    assert e.value.status == 422 and e.value.payload["code"] == "forbidden-construct"


# ---------------------------------------------------------------------------
# 160/161 dans un plan : constats, chaîne, auteur du modèle
# ---------------------------------------------------------------------------
NET_TEMPLATE = {"variables": ["vlan"], "resources": [
    {"kind": "network", "spec": {"name": "plan-test-net", "namespace": "default",
                                 "cluster_network_name": "mgmt", "vlan_id": "{{vlan}}"}}]}
RAW_TEMPLATE = {"variables": ["equipe"], "resources": [
    {"kind": "raw", "spec": {"tf": 'resource "harvester_ssh_key" "k" {\n  name       = "{{equipe}}-k"\n'
                                   '  namespace  = "default"\n  public_key = "' + KEY + '"\n}\n'}}]}


def _seed(c, templates):
    for name, body in templates.items():
        assert c.put(f"/api/baremetal/tf-templates/{name}", json=body).status_code == 201, name
    plan = tr.plan_body(templates=tuple(templates), variables=("equipe", "vlan"))
    assert c.put("/api/baremetal/plans/site", json=plan).status_code == 201


def _as(monkeypatch, role):
    monkeypatch.setattr(wapp, "current_role", lambda: role)


def test_check_says_admin_kinds_block_an_operator(env, monkeypatch):
    c = env["client"]
    _seed(c, {"reseau": NET_TEMPLATE})
    body = {"plan": "site", "variables": {"equipe": "ops", "vlan": "0"}}
    codes = {f["code"]: f for f in c.post("/api/deploy/harvlab/check", json=body).get_json()["findings"]}
    assert "admin-kind" not in codes                                              # admin
    _as(monkeypatch, "operator")
    r = c.post("/api/deploy/harvlab/check", json=body).get_json()
    f = next(f for f in r["findings"] if f["code"] == "admin-kind")
    assert f["level"] == "blocking" and "reseau" in f["detail"] and "network" in f["detail"] and r["blocking"]
    r = c.post("/api/deploy/harvlab", json=body)
    assert r.status_code == 400 and env["started"] == []


def test_operator_runs_an_admin_raw_template_and_its_author_is_kept(world, monkeypatch):
    c = world["client"]
    _seed(c, {"cles": RAW_TEMPLATE})                    # écrit par alice (admin)
    _as(monkeypatch, "operator")
    monkeypatch.setattr(wapp, "current_user", lambda: "bob")
    r = c.post("/api/deploy/harvlab", json={"plan": "site", "variables": {"equipe": "ops", "vlan": "0"}})
    assert r.status_code == 202, r.get_json()
    run = trun.wait(r.get_json()["action_id"])
    assert run.status == "done", run.result
    assert run.launch_role == "operator" and run.result["by"] == "bob"
    [item] = next(p for p in run.result["phases"] if p["id"] == "terraform")["items"]
    assert item["template_by"] == "alice"
    d = world["store"].find_by_name("harvlab", "site-cles")
    assert d["origin"]["template_by"] == "alice"
    assert world["fake"].count("apply") == 1
    # la déclaration produite reste du code libre : hors chaîne, réservée à l'admin
    r = c.post("/api/terraform/harvlab/apply_declaration", json={"declaration": {"id": d["id"]}, "dry_run": True})
    assert r.status_code == 403 and r.get_json()["code"] == "raw-admin-only"


def test_template_author_is_never_taken_from_the_document(env):
    c = env["client"]
    body = dict(RAW_TEMPLATE, updated_by="mallory")
    assert c.put("/api/baremetal/tf-templates/cles", json=body).status_code == 201
    assert dp.load_template(env["state"], "cles")["updated_by"] == "alice"


def test_origin_template_by_is_checked():
    base = {"plan": "p", "template": "t", "template_revision": 1, "plan_revision": 1}
    assert wapp._tfs.clean_origin(base) == base
    assert wapp._tfs.clean_origin(dict(base, template_by="alice"))["template_by"] == "alice"
    for bad in (dict(base, template_by="a\nb"), dict(base, template_by=3), dict(base, other="x")):
        with pytest.raises(wapp._tfs.StoreError):
            wapp._tfs.clean_origin(bad)


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------
def test_mutation_without_the_relexing_a_value_opens_an_interpolation(monkeypatch):
    monkeypatch.setattr(hg, "_structure_changed", lambda *a: None)
    out, refused = hg.render('n = "{{a}}{{b}}"', {"a": "$", "b": "{" + ABS + "}"})
    assert refused == [] and out == 'n = "${' + ABS + '}"'          # la sonde passerait


def test_mutation_without_the_role_rule_an_operator_creates_a_network(decl_world, monkeypatch):
    monkeypatch.setattr(wapp, "_tf_role_refusal", lambda resources, role, via_plan=False: None)
    decl_world["role"]["v"] = "operator"
    r = decl_world["client"].post("/api/tf-declarations", json={"cluster": "harv1", "name": "d",
                                                               "resources": [_res("network")]})
    assert r.status_code == 201


def test_mutation_without_the_guard_in_launch_a_chain_would_run_a_provisioner(decl_world, monkeypatch):
    monkeypatch.setattr(wapp, "_tf_guard_rendered", lambda rendered, target_kind="harvester": [])
    bad = decl_world["store"].create("e" * 12, "harv1", "bad", resources=[{"id": "9" * 12, "kind": "raw", "spec": {
        "tf": 'resource "harvester_ssh_key" "k" {\n provisioner "local-exec" { command = "id" }\n}\n'}}])
    rid, _h = wapp._tf_decl_launch("harv1", decl_world["kc"], bad["id"], "plan", role="admin")
    assert rid                                                     # plus rien ne l'arrête avant la commande


def test_the_screen_greys_admin_kinds_with_the_reason():
    """Décision 160 à l'écran : boutons d'ajout et ressources des genres
    réservés grisés pour un non-admin, bulle qui dit pourquoi ; constats du
    déroulé nommés (le serveur décide, l'écran le montre)."""
    js = (ROOT / "web/static/js/tf-decl-view.js").read_text()
    assert "const ADMIN_KINDS = ['network', 'storageclass', 'ippool'];" in js
    assert "kindLocked(k) || declLocked(d) ? 'disabled' : ''" in js
    assert "(d.admin_kinds || []).length" in js and "tr('tfd.t.adminKind')" in js
    plans = (ROOT / "web/static/js/bm-plans.js").read_text()
    assert "'admin-kind': () => tr('dpw.f.adminKind')" in plans
    assert "'forbidden-construct': () => tr('dpw.f.forbiddenConstruct')" in plans
    i18n = (ROOT / "web/static/js/i18n.js").read_text()
    for key in ("tfd.t.adminKind", "dpw.f.adminKind", "dpw.f.forbiddenConstruct"):
        assert i18n.count(f"'{key}':") + i18n.count(f'"{key}":') == 5, key


# ---------------------------------------------------------------------------
# Revue du bloc 3, remarques m1 à m3
# ---------------------------------------------------------------------------
def test_m1_a_probe_holding_the_cluster_lock_for_an_instant_is_waited_for(env, monkeypatch):
    tr.seed(env["client"])
    real, calls = wapp._deploy_cluster_lock, []

    def lock(cluster, run_id, plan_name):
        calls.append(run_id)
        if len(calls) <= 2:
            return None, {"run": "", "plan": ""}          # sonde de _deploy_lock_holder
        return real(cluster, run_id, plan_name)
    monkeypatch.setattr(wapp, "_deploy_cluster_lock", lock)
    r = env["client"].post("/api/deploy/harvlab", json={"plan": "site", "variables": {"equipe": "ops"}})
    assert r.status_code == 202, r.get_json()
    assert len(calls) == 3


def test_m3_a_declaration_not_released_in_time_is_said(world, monkeypatch):
    tr.seed(world["client"])
    monkeypatch.setattr(wapp, "_tf_decl_wait_idle", lambda decl_id, **kw: False)
    run = trun.start(world)
    item = next(p for p in run.result["phases"] if p["id"] == "terraform")["items"][0]
    assert item["status"] == "error" and item["message"] == "declaration-busy"


def test_m2_an_updated_declaration_whose_plan_fails_says_it_is_not_applied(world):
    c = world["client"]
    tr.seed(c)
    assert trun.start(world, variables={"equipe": "ops"}).status == "done"
    world["fake"].fail.add("dev-ops")
    r = c.post("/api/deploy/harvlab", json={"plan": "site", "variables": {"equipe": "dev"},
                                            "update_differing": True})
    run = trun.wait(r.get_json()["action_id"])
    item = next(p for p in run.result["phases"] if p["id"] == "terraform")["items"][0]
    assert item["status"] == "error" and item["updated"] is True
    assert item["message"].endswith("; declaration updated, not applied")


@pytest.mark.parametrize("kind,spec,error", [
    ("ippool", dict(ADMIN_SPECS["ippool"], range=[dict(ADMIN_SPECS["ippool"]["range"][0], subnet="10.9.0.5/24")]),
     "has host bits set: write 10.9.0.0/24"),
    ("network", dict(ADMIN_SPECS["network"], route_mode="manual", route_cidr="10.9.0.1/24",
                     route_gateway="10.9.0.1"), "has host bits set"),
    ("network", dict(ADMIN_SPECS["network"], labels={"bad key": "x"}), "not a Kubernetes label key"),
    ("network", dict(ADMIN_SPECS["network"], labels={"-a": "x"}), "not a Kubernetes label key"),
])
def test_m5_subnets_and_label_keys_are_refused_before_the_webhook(kind, spec, error):
    with pytest.raises(wapp._tfk.SpecError) as e:
        wapp._render_tf_for_kind(kind, spec)
    assert error in str(e.value)


@pytest.mark.parametrize("labels", [{"app": "x"}, {"example.com/team": "ops"}, {"a.b_c-d": ""}])
def test_m5_ordinary_label_keys_pass(labels):
    assert "labels" in wapp._render_tf_for_kind("network", dict(ADMIN_SPECS["network"], labels=labels))
