"""2.0.0, tâche 15 : la vue des VMs avec le panneau Dossiers / Hôtes, la vue
« Dossiers » et les gestes de rangement (folders.js, all-vms.js).

API simulée : arborescence, VMs Harvester et KVM, conteneurs, hôtes,
contrôles avant geste, rangement, annulation (le faux serveur remet les
dossiers d'avant), flux d'actions et Activité pour le dock. On vérifie le
glisser-déposer de trois lignes de deux familles, la fenêtre de contrôle
avec une ligne qui ne sera rangée que dans la console, l'action dans le
dock puis « Annuler » qui remet les dossiers d'origine ; créer, renommer,
supprimer (vide, non vide avec « déplacer le contenu vers ») ; les
adresses #vms/folder/unfiled et #vms/host/kvm/node2 ; le lecteur sans
geste ; un nom de dossier hostile qui reste du texte ; les cinq langues."""

import json
import re
import time

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import goto  # noqa: E402

KUUID = "49960a5e-a239-42da-95f6-f55383ea5c62"
XSS = '<img src=x onerror="window.__xss=1">'


def tree():
    return [
        {"id": 1, "parent_id": None, "name": "Prod", "position": 0, "path": "/Prod", "origin": "console",
         "counts": {"vms": None, "containers": None, "hosts": None}},
        {"id": 2, "parent_id": 1, "name": "ERP", "position": 0, "path": "/Prod/ERP", "origin": "console",
         "counts": {"vms": None, "containers": None, "hosts": None}},
        {"id": 3, "parent_id": None, "name": "Lab", "position": 0, "path": "/Lab", "origin": "objects",
         "counts": {"vms": None, "containers": None, "hosts": None}},
        {"id": 4, "parent_id": None, "name": XSS, "position": 0, "path": "/" + XSS, "origin": "console",
         "counts": {"vms": None, "containers": None, "hosts": None}},
        {"id": 5, "parent_id": None, "name": "Empty", "position": 0, "path": "/Empty", "origin": "console",
         "counts": {"vms": None, "containers": None, "hosts": None}},
    ]


def hvm(name, ns="default", cluster="harv-fake", folder=None, src=None, phase="Running", uid=None):
    return {"kind": "harvester", "source": cluster, "cluster": cluster, "namespace": ns, "name": name,
            "phase": phase, "runStrategy": "Always" if phase == "Running" else "Halted", "cpu": 2,
            "memory": "4Gi", "memory_bytes": 4 * 2 ** 30, "node": "n1", "ips": [], "labels": {},
            "uuid": uid or f"uid-{name}", "key": f"harvester/{cluster}/{ns}/{name}",
            "folder": folder, "folder_source": src or ("vm" if folder else None)}


def kvm(name, uuid, host="node2", folder=None):
    return {"kind": "kvm", "source": host, "host": host, "namespace": None, "name": name, "state": "running",
            "phase": "Running", "cpu": 1, "memory_bytes": 2 ** 30, "node": host, "ips": [], "labels": {},
            "uuid": uuid, "key": f"kvm/{uuid}", "folder": folder, "folder_source": "vm" if folder else None,
            "persistent": True}


def fleet():
    return [hvm("web-1", folder="/Prod"), hvm("db", folder="/Prod/ERP", uid="uid-db"),
            hvm("cirros", ns="lab"), hvm("mine", cluster="gpudemo", folder="/Lab", src="console"),
            kvm("kvmtest-1", KUUID), kvm("kvmtest-2", "6b128cd4-f440-482d-8e90-1eb32a2c2094", folder="/Lab")]


@pytest.fixture
def world(context, flask_server):
    state = {"tree": tree(), "vms": fleet(), "sent": [], "role": "operator", "actions": [], "lang": "en",
             "prev": {}}
    context.add_init_script("localStorage.setItem('harvester_ops_sidebar_pinned','1');"
                            "localStorage.removeItem('harvester_ops_allvms');"
                            "localStorage.removeItem('harvester_ops_vms_panel');")
    page = context.new_page()

    def ok(route, body, status=200):
        return route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def action(req, label, undo=True):
        aid = f"fa{len(state['actions']):010d}"
        state["actions"].append({"id": aid, "action": f"folders:{label}", "cluster": "folders", "status": "done",
                                 "exit_code": 0, "started_at": time.time(), "ended_at": time.time(),
                                 "dry_run": False, "error_summary": None,
                                 **({"undo": {"path": f"/api/folders/actions/{aid}/undo"}} if undo else {})})
        return aid

    def folders(route, req):
        url = req.url.split("/api/folders", 1)[1].split("?", 1)[0]
        body = req.post_data_json if req.post_data else None
        if req.method == "GET" and url == "":
            return ok(route, {"tree": state["tree"], "role": state["role"]})
        state["sent"].append((req.method, "/api/folders" + url, body))
        if url == "/check":
            outs = []
            for o in body["objects"]:
                k = o["key"]
                if k.startswith("harvester/gpudemo/"):
                    outs.append({"key": k, "write": False, "outcome": "console", "check": "ssar",
                                 "reason": "you cannot write this object on its cluster: only the console's filing changes"})
                else:
                    outs.append({"key": k, "write": True, "outcome": "object", "check": "virsh" if k.startswith("kvm/") else "ssar",
                                 "reason": "the folder is written on it"})
            return ok(route, {"objects": outs})
        if url == "/assign":
            to = body["to"]
            path = None if to == "unfiled" else next(f["path"] for f in state["tree"] if f["id"] == to)
            aid = action(req, f"assign:{path or 'unfiled'}")
            state["prev"][aid] = {}
            for v in state["vms"]:
                if v["key"] in [o["key"] for o in body["objects"]]:
                    state["prev"][aid][v["key"]] = v["folder"]
                    v["folder"] = path
            return ok(route, {"action_id": aid}, 202)
        m = re.match(r"^/actions/([^/]+)/undo$", url)
        if m:
            for v in state["vms"]:
                if v["key"] in state["prev"].get(m.group(1), {}):
                    v["folder"] = state["prev"][m.group(1)][v["key"]]
            return ok(route, {"action_id": action(req, f"undo:{m.group(1)}")}, 202)
        if req.method == "DELETE":
            fid = int(url.strip("/"))
            if fid != 5:
                return ok(route, {"error": "folder is not empty: move its content to another folder first",
                                  "nonempty": True, "count_visible": 1, "subfolders": 1, "hidden": False}, 409)
            state["tree"] = [f for f in state["tree"] if f["id"] != fid]
            return ok(route, {"action_id": action(req, "delete", undo=False)}, 202)
        if req.method == "POST" and url == "":
            nid = max(f["id"] for f in state["tree"]) + 1
            parent = body.get("parent_id")
            ppath = next((f["path"] for f in state["tree"] if f["id"] == parent), "")
            state["tree"].append({"id": nid, "parent_id": parent, "name": body["name"], "position": 0,
                                  "path": f"{ppath}/{body['name']}", "origin": "console", "counts": {}})
            return ok(route, {"action_id": action(req, "create", undo=False)}, 202)
        if req.method == "PATCH":
            fid = int(url.strip("/"))
            for f in state["tree"]:
                if f["id"] == fid:
                    f["name"] = body["name"]
                    f["path"] = "/" + body["name"] if f["parent_id"] is None else f["path"]
            return ok(route, {"action_id": action(req, "rename")}, 202)
        return ok(route, {"action_id": action(req, "other")}, 202)

    def activity(route, req):
        return ok(route, {"in_progress": [], "actions_done": state["actions"][-5:], "log_files": [],
                          "filters": {}, "matched": 0, "total": 0, "facets": {}})

    page.route(re.compile(r".*/api/folders.*"), folders)
    page.route(re.compile(r".*/api/vms-all(\?.*)?$"), lambda r, q: ok(r, {
        "sources": [{"kind": "harvester", "source": "harv-fake", "state": "ok", "count": 3},
                    {"kind": "harvester", "source": "gpudemo", "state": "ok", "count": 1},
                    {"kind": "kvm", "source": "node2", "state": "ok", "count": 2}],
        "vms": state["vms"], "clusters": [], "folder_warnings": []}))
    page.route(re.compile(r".*/api/replication/plans(\?.*)?$"), lambda r, q: ok(r, {
        "plans": [{"id": "rp-0123456789ab", "resources": [{"resource_uid": "uid-db"}]}]}))
    page.route(re.compile(r".*/api/containers-all(\?.*)?$"), lambda r, q: ok(r, {
        "clusters": [{"cluster": "harv-fake", "state": "ok", "count": 2}],
        "workloads": [{"kind": "Deployment", "namespace": "web", "name": "front", "cluster": "harv-fake",
                       "state": "Running", "ready": 1, "desired": 1, "images": [], "gpus": 0, "created": None,
                       "labels": {}, "pods": [], "key": "container/harv-fake/web/deployment/front",
                       "folder": "/Prod", "folder_source": "vm"},
                      {"kind": "Job", "namespace": "web", "name": "once", "cluster": "harv-fake",
                       "state": "Completed", "ready": 1, "desired": 1, "images": [], "gpus": 0, "created": None,
                       "labels": {}, "pods": []}]}))
    page.route(re.compile(r".*/api/hosts$"), lambda r, q: ok(r, {"families": {}, "hosts": [
        {"kind": "harvester", "name": "harv-fake", "state": "ok", "error": None, "vm_count": 3, "labels": {},
         "version": None, "favorite": False, "folder": "/Prod", "folder_source": "console", "origin": "config",
         "key": "host/harvester/harv-fake"},
        {"kind": "kvm", "name": "node2", "state": "ok", "error": None, "vm_count": 2, "labels": {},
         "version": None, "favorite": False, "folder": None, "folder_source": None, "origin": "console",
         "key": "host/kvm/node2"}]}))
    page.route(re.compile(r".*/api/kvm/hosts\?declared=1$"), lambda r, q: ok(r, {"hosts": [{"name": "node2"}]}))
    page.route(re.compile(r".*/api/whoami$"), lambda r, q: ok(r, {"role": state["role"], "roles_active": True,
                                                                  "user": "u", "auth": "local"}))
    page.route(re.compile(r".*/api/activity(\?.*)?$"), activity)
    page.route(re.compile(r".*/api/stream/fa\d+$"), lambda r, q: r.fulfill(
        status=200, content_type="text/event-stream", body='event: end\ndata: {"status": "done"}\n\n'))
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    state["errors"] = errors

    def go(hash, lang="en", tree=True):
        page.add_init_script(f"localStorage.setItem('harvester_ops_language', '{lang}');")
        goto(page, flask_server["base_url"], hash)
        if tree:
            page.wait_for_function("() => !!(window.Folders && Folders.get() && Folders.get().length > 0)")
        return page
    return go, page, state


def rows(page):
    return page.locator("#all-vms-host tbody tr[data-avm-row]")


def row_names(page):
    return rows(page).evaluate_all("rs => rs.map(r => r.querySelector('[data-avm-col=name]').childNodes[0].textContent.trim())")


def sent(state, path, method="POST"):
    return [b for m, p, b in state["sent"] if p == path and m == method]


def test_panel_shows_the_tree_with_counts_and_the_two_virtual_folders(world):
    go, page, state = world
    go("#vms")
    panel = page.locator("#all-vms-host [data-avm=panel]")
    expect(panel.locator(".fld-node.virtual")).to_have_count(2)
    expect(panel.locator('[data-fld-id="1"] .fld-count')).to_have_text("2")      # /Prod + /Prod/ERP
    expect(panel.locator('[data-fld-drop="unfiled"] .fld-count')).to_have_text("2")
    # dossier recréé depuis les objets : pastille et info-bulle
    expect(panel.locator('[data-fld-id="3"] .fld-origin')).to_have_attribute("title", re.compile("objects that name it"))
    # pastilles famille, console, DR
    expect(rows(page)).to_have_count(6)
    expect(page.locator('tr[data-avm-row="kvm/' + KUUID + '"] .vmf-family')).to_have_text("KVM")
    expect(page.locator('tr[data-avm-row="harvester/gpudemo/default/mine"] .vmf-console')).to_be_visible()
    expect(page.locator('tr[data-avm-row="harvester/harv-fake/default/db"] .vmf-dr')).to_have_attribute(
        "href", "#dr/rp-0123456789ab")
    assert state["errors"] == []


def test_drag_three_rows_of_two_families_checks_then_undo_restores(world):
    go, page, state = world
    go("#vms")
    for key in ("harvester/harv-fake/lab/cirros", "kvm/" + KUUID, "harvester/gpudemo/default/mine"):
        page.locator(f'tr[data-avm-row="{key}"] input[type=checkbox]').check()
    src = page.locator('tr[data-avm-row="kvm/' + KUUID + '"]')
    src.drag_to(page.locator('#all-vms-host [data-fld-id="5"]'))
    win = page.locator("#fp-fld-move")
    expect(win).to_be_visible()
    checks = win.locator("tr[data-fld-check]")
    expect(checks).to_have_count(3)
    # le contrôle est fait AVANT le bouton : une ligne seulement dans la console
    expect(win.locator('tr[data-outcome="console"]')).to_have_count(1)
    expect(win.locator('tr[data-outcome="console"]')).to_contain_text("only the console's filing changes")
    check_body = sent(state, "/api/folders/check")[-1]
    assert {o["key"] for o in check_body["objects"]} == {
        "harvester/harv-fake/lab/cirros", "kvm/" + KUUID, "harvester/gpudemo/default/mine"}
    assert {"key": "kvm/" + KUUID, "host": "node2"} in check_body["objects"] and check_body["to"] == 5
    win.locator("[data-fld-apply]").click()
    expect(win).to_be_hidden()
    assert sent(state, "/api/folders/assign")[-1]["to"] == 5
    expect(page.locator('tr[data-avm-row="kvm/' + KUUID + '"] .vmf-folder')).to_contain_text("/Empty", timeout=8000)
    # l'action suivie dans le dock, avec « Annuler »
    card = page.locator(".dock-action-card", has_text="folders:assign:/Empty")
    expect(card).to_be_visible(timeout=8000)
    card.locator("[data-undo]").click()
    expect(page.locator('tr[data-avm-row="harvester/gpudemo/default/mine"] .vmf-folder')).to_contain_text("/Lab", timeout=8000)
    expect(page.locator('tr[data-avm-row="kvm/' + KUUID + '"] .vmf-folder')).to_contain_text("unfiled")
    assert any(p.endswith("/undo") for m, p, b in state["sent"])
    assert state["errors"] == []


def test_move_menu_on_a_row_with_folder_search(world):
    go, page, state = world
    go("#vms")
    page.locator('tr[data-avm-row="harvester/harv-fake/lab/cirros"] [data-avm-act="folder"]').click()
    win = page.locator("#fp-fld-move")
    # pas de bouton actif avant les contrôles du serveur
    expect(win.locator("[data-fld-apply]")).to_be_disabled()
    assert sent(state, "/api/folders/check") == []
    win.locator("[data-fld-search]").fill("erp")
    expect(win.locator(".fld-target")).to_have_count(2)                  # Non rangées + /Prod/ERP
    win.locator('input[value="2"]').check()
    expect(win.locator("tr[data-fld-check]")).to_have_count(1)
    win.locator("[data-fld-apply]").click()
    assert sent(state, "/api/folders/assign")[-1] == {"objects": [{"key": "harvester/harv-fake/lab/cirros"}], "to": 2}


def test_changing_the_target_asks_the_server_once(world):
    """Revue M-2 : le contrôle ne dépend pas de la cible ; changer trois fois
    de cible ne relance pas la revue d'accès des objets déjà contrôlés."""
    go, page, state = world
    go("#vms")
    page.locator('tr[data-avm-row="harvester/harv-fake/lab/cirros"] [data-avm-act="folder"]').click()
    win = page.locator("#fp-fld-move")
    for fid in ("2", "3", "5"):
        win.locator(f'input[value="{fid}"]').check()
    expect(win.locator("tr[data-fld-check]")).to_have_count(1)
    expect(win.locator("[data-fld-apply]")).to_be_enabled()
    win.locator('input[value="1"]').check()
    page.wait_for_timeout(400)
    expect(win.locator("[data-fld-apply]")).to_be_enabled()
    assert len(sent(state, "/api/folders/check")) == 1
    win.locator("[data-fld-apply]").click()
    assert sent(state, "/api/folders/assign")[-1]["to"] == 1


def test_create_rename_delete_inline_without_browser_prompt(world):
    go, page, state = world
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.type), d.dismiss()))
    go("#vms/folder/1")
    panel = page.locator("#all-vms-host [data-avm=panel]")
    panel.locator('[data-fld-tool="new"]').click()
    panel.locator("[data-fld-new]").fill("Ops")
    panel.locator("[data-fld-new]").press("Enter")
    expect(page).to_have_url(re.compile(r"#vms/folder/6$"), timeout=8000)
    assert sent(state, "/api/folders")[-1] == {"parent_id": 1, "name": "Ops"}
    panel.locator('[data-fld-tool="rename"]').click()
    panel.locator("[data-fld-edit]").fill("Run")
    panel.locator("[data-fld-edit]").press("Enter")
    expect(panel.locator('[data-fld-id="6"] .fld-name')).to_have_text("Run", timeout=8000)
    assert sent(state, "/api/folders/6", "PATCH")[-1] == {"name": "Run"}
    # Échap abandonne une saisie
    panel.locator('[data-fld-tool="new"]').click()
    panel.locator("[data-fld-new]").press("Escape")
    expect(panel.locator("[data-fld-new]")).to_have_count(0)
    page.goto(page.url.split("#")[0] + "#vms/folder/5")
    page.wait_for_function("() => !!(window.Folders && Folders.get())")
    panel.locator('[data-fld-tool="delete"]').click()
    panel.locator('[data-fld-tool="delete-yes"]').click()
    expect(page).to_have_url(re.compile(r"#vms$"), timeout=8000)
    assert ("DELETE", "/api/folders/5", None) in state["sent"]
    assert dialogs == []


def test_delete_non_empty_is_refused_and_offers_move_content_then_delete(world):
    go, page, state = world
    go("#vms/folder/1")
    page.locator('#all-vms-host [data-fld-tool="delete"]').click()
    win = page.locator("#fp-fld-empty")
    expect(win).to_be_visible()
    # ni le dossier lui-même ni ses sous-dossiers ne sont des cibles
    labels = win.locator(".fld-target span").all_inner_texts()
    assert "/Prod" not in labels and "/Prod/ERP" not in labels and "/Lab" in labels
    win.locator('input[value="3"]').check()
    win.locator("[data-fld-empty-go]").click()
    assert sent(state, "/api/folders/1/empty-into")[-1] == {"target_id": 3}


def test_unfiled_and_host_addresses(world):
    go, page, state = world
    go("#vms/folder/unfiled")
    expect(rows(page)).to_have_count(2)
    assert sorted(row_names(page)) == ["cirros", "kvmtest-1"]
    go("#vms/host/kvm/node2")
    expect(page.locator('#all-vms-host [data-fld-mode="hosts"]')).to_have_class(re.compile("active"))
    expect(rows(page)).to_have_count(2)
    assert sorted(row_names(page)) == ["kvmtest-1", "kvmtest-2"]
    expect(page.locator('#all-vms-host .fld-node.active')).to_contain_text("node2")


def test_viewer_has_no_filing_gesture(world):
    go, page, state = world
    state["role"] = "viewer"
    go("#vms/folder/1")
    page.wait_for_function("() => App.role && App.role() === 'viewer'")
    panel = page.locator("#all-vms-host [data-avm=panel]")
    for tool in ("new", "rename", "delete"):
        expect(panel.locator(f'[data-fld-tool="{tool}"]')).to_be_disabled()
        expect(panel.locator(f'[data-fld-tool="{tool}"]')).to_have_attribute("title", re.compile("operator role"))
    expect(page.locator('#all-vms-host tbody [data-avm-act="folder"]').first).to_be_disabled()
    assert page.locator("#all-vms-host tbody tr[draggable=true]").count() == 0
    expect(page.locator('#all-vms-host [data-avm="viewer"]')).to_be_visible()
    # le dock montre l'annulation d'un geste d'un autre, grisée avec la raison
    state["actions"].append({"id": "fa0000000099", "action": "folders:assign:/Prod", "cluster": "folders",
                             "status": "done", "exit_code": 0, "started_at": time.time(), "ended_at": time.time(),
                             "dry_run": False, "error_summary": None,
                             "undo": {"path": "/api/folders/actions/fa0000000099/undo"}})
    undo = page.locator("#dock-card-fa0000000099 .dock-undo")
    expect(undo).to_be_disabled(timeout=8000)
    expect(undo).to_have_attribute("title", re.compile("operator role"))
    assert [s for s in state["sent"] if s[0] != "GET"] == []


def test_a_hostile_folder_name_stays_text(world):
    go, page, state = world
    go("#vms/folder/4")
    expect(page.locator('#all-vms-host [data-fld-id="4"] .fld-name')).to_have_text(XSS)
    expect(page.locator("#all-vms-host .vmf-scope-title")).to_contain_text(XSS)
    assert page.evaluate("window.__xss") is None
    assert page.locator("#all-vms-host img[src=x]").count() == 0


def test_folders_view_three_groups_and_filters_on_containers_and_hosts(world):
    go, page, state = world
    go("#folders/1")
    view = page.locator("#folders-view-host")
    expect(view.locator('[data-fview-group="vms"] .fview-item')).to_have_count(2)
    expect(view.locator('[data-fview-group="containers"] .fview-item')).to_have_count(1)
    expect(view.locator('[data-fview-group="hosts"] .fview-item')).to_have_count(1)
    expect(view.locator('[data-fview-group="containers"] a')).to_have_attribute("href", "#containers/folder/1")
    go("#containers/folder/1")
    expect(page.locator("#all-containers-host tbody tr[data-ct-row]")).to_have_count(1)
    expect(page.locator("#all-containers-host [data-ct=scope]")).to_contain_text("/Prod")
    go("#hosts/folder/1")
    expect(page.locator("#hosts-index-host tr[data-hix-row]")).to_have_count(1)
    # ranger un hôte propose ses VMs, case décochée
    page.locator('#hosts-index-host [data-hix="folder"]').first.click()
    win = page.locator("#fp-fld-move")
    expect(win.locator("[data-fld-extra]")).not_to_be_checked()
    expect(win.locator(".fld-extra")).to_contain_text("Also file its 3 VM(s)")
    win.locator('input[value="3"]').check()
    expect(win.locator("tr[data-fld-check]")).to_have_count(1)
    win.locator("[data-fld-extra]").check()
    expect(win.locator("tr[data-fld-check]")).to_have_count(4)


@pytest.mark.parametrize("lang,move,unfiled", [
    ("en", "Move to folder", "Unfiled"), ("fr", "Déplacer vers un dossier", "Non rangées"),
    ("de", "In Ordner verschieben", "Nicht eingeordnet"), ("es", "Mover a una carpeta", "Sin carpeta"),
    ("it", "Sposta in una cartella", "Non archiviate"),
])
def test_new_labels_in_five_languages(world, lang, move, unfiled):
    go, page, state = world
    go("#vms", lang=lang)
    page.wait_for_function(f"() => i18n.currentLang === '{lang}'")
    page.locator('#all-vms-host tr[data-avm-row] input[type=checkbox]').first.check()
    expect(page.locator('#all-vms-host [data-avm-move]')).to_contain_text(move)
    expect(page.locator('#all-vms-host [data-fld-drop="unfiled"] .fld-name')).to_have_text(unfiled)


def test_panel_with_2000_folders_recreated_from_objects_stays_fast(world):
    """Décision 90 : aucun plafond aux dossiers recréés depuis les objets ;
    2 000 dossiers (et 2 000 VMs) restent rapides à dessiner (temps mesurés,
    rapport de l'étape 3b) et gardent leur pastille « depuis les objets »."""
    go, page, state = world
    big = [{"id": 100 + g, "parent_id": None, "name": f"G{g}", "position": 0, "path": f"/G{g}",
            "origin": "objects", "counts": {}} for g in range(40)]
    big += [{"id": 1000 + i, "parent_id": 100 + i % 40, "name": f"F{i}", "position": 0,
             "path": f"/G{i % 40}/F{i}", "origin": "objects", "counts": {}} for i in range(1960)]
    state["tree"] = big
    state["vms"] = [hvm(f"vm{i}", folder=f"/G{i % 40}/F{i % 1960}") for i in range(2000)]
    t0 = time.time()
    go("#vms")
    expect(page.locator("#all-vms-host tbody tr[data-avm-row]")).to_have_count(300, timeout=30000)   # revue I-3
    expect(page.locator("#all-vms-host .fld-node[data-fld-id]")).to_have_count(2000)
    load = time.time() - t0
    ms = page.evaluate("""() => {
      const el = document.createElement('div'); document.body.appendChild(el);
      const rows = AllVMs.cached().vms;
      const t = performance.now();
      for (let i = 0; i < 5; i++) Folders.panel(el, { base: 'vms', current: null, mode: 'folders', rows,
        allLabel: 'All', allTip: '', unfiledLabel: 'Unfiled', unfiledTip: '' });
      const per = (performance.now() - t) / 5;
      el.remove();
      return per;
    }""")
    view_ms = page.evaluate("""() => new Promise((res) => { const t = performance.now();
      document.dispatchEvent(new CustomEvent('hops:folders'));
      requestAnimationFrame(() => requestAnimationFrame(() => res(performance.now() - t))); })""")
    print(f"whole VMs view (2000 rows + panel) one render: {view_ms:.0f} ms")
    expect(page.locator('#all-vms-host [data-fld-id="1000"] .fld-origin')).to_be_visible()
    expect(page.locator('#all-vms-host [data-fld-id="100"] .fld-count')).to_have_text("50")
    print(f"panel 2000 folders + 2000 VMs: one render {ms:.0f} ms, page ready in {load:.1f}s")
    assert ms < 1500


def _old_action(aid, label, undo=True):
    # finie il y a une heure : le dock ne la montre plus, l'Activité si
    return {"id": aid, "action": f"folders:{label}", "cluster": "folders", "status": "done", "exit_code": 0,
            "started_at": time.time() - 3600, "ended_at": time.time() - 3590, "dry_run": False,
            "error_summary": None, **({"undo": {"path": f"/api/folders/actions/{aid}/undo"}} if undo else {})}


def test_undo_from_activity_after_the_dock_card_is_gone(world):
    """Revue I-4 : Annuler depuis l'onglet Activité, bien après la carte du dock."""
    go, page, state = world
    state["actions"] += [_old_action("fa0000000050", "rename:/Prod"), _old_action("fa0000000051", "create:/X", False)]
    go("#activity", tree=False)
    row = page.locator("#activity-history tbody tr", has_text="folders:rename:/Prod")
    btn = row.locator("[data-act-undo]")
    expect(btn).to_be_enabled(timeout=8000)
    expect(btn).to_have_attribute("title", re.compile("folders from before"))
    expect(page.locator("#activity-history tbody tr", has_text="folders:create:/X").locator("[data-act-undo]")).to_have_count(0)
    assert page.locator("#dock-card-fa0000000050").count() == 0
    btn.click()
    expect(page.locator(".dock-action-card", has_text="folders:undo:fa0000000050")).to_be_visible(timeout=8000)
    assert ("POST", "/api/folders/actions/fa0000000050/undo", None) in state["sent"]
    # le clic n'ouvre pas le détail de la ligne
    expect(page.locator("#fp-activity-detail")).to_have_count(0)
    assert state["errors"] == []


def test_undo_in_activity_is_greyed_for_a_viewer(world):
    go, page, state = world
    state["role"] = "viewer"
    state["actions"].append(_old_action("fa0000000060", "assign:/Prod"))
    go("#activity", tree=False)
    page.wait_for_function("() => App.role && App.role() === 'viewer'")
    btn = page.locator("#activity-history tbody tr", has_text="folders:assign:/Prod").locator("[data-act-undo]")
    expect(btn).to_be_disabled(timeout=8000)
    expect(btn).to_have_attribute("title", re.compile("operator role"))
    assert [s for s in state["sent"] if s[0] != "GET"] == []


RENDER_MS = """() => new Promise((res) => { const t = performance.now();
  document.dispatchEvent(new CustomEvent('hops:folders'));
  requestAnimationFrame(() => requestAnimationFrame(() => res(performance.now() - t))); })"""
FULL_MS = """(col) => new Promise((res) => { const t = performance.now();
  document.querySelector(`#all-vms-host [data-avm-sort="${col}"]`).click();
  requestAnimationFrame(() => requestAnimationFrame(() => res(performance.now() - t))); })"""
REFRESH_MS = """async () => { const t = performance.now(); await AllVMs.load();
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  return performance.now() - t; }"""


def test_5000_vms_show_the_first_300_then_more_and_act_on_the_filtered_set(world):
    """Revue I-3 : la vue d'accueil ne dessine que 300 lignes, « Show 300
    more » en ajoute, la sélection et « tout cocher » portent sur l'ensemble
    filtré (des clés, pas des lignes du DOM), la recherche attend 150 ms et
    une relecture sans changement ne redessine rien."""
    go, page, state = world
    state["vms"] = [hvm(f"vm{i:05d}", ns=f"ns{i % 7}") for i in range(5000)]
    go("#vms")
    expect(rows(page)).to_have_count(300, timeout=30000)
    ms = page.evaluate(RENDER_MS)
    full = page.evaluate(FULL_MS, "namespace")
    print(f"5000 VMs: render without change {ms:.0f} ms, full redraw (new sort) {full:.0f} ms")
    # bornes larges (suite en parallèle) : la garde est le nombre de lignes
    # dessinées ; les temps mesurés seul sont au rapport de la revue
    assert ms < 800 and full < 1500
    page.locator('#all-vms-host [data-avm-sort="name"]').click()
    more = page.locator('#all-vms-host [data-avm="more"]')
    expect(more).to_be_visible()
    expect(more).to_contain_text("300 more")
    expect(more).to_have_attribute("title", re.compile("."))
    first = rows(page).first.get_attribute("data-avm-row")
    more.click()
    expect(rows(page)).to_have_count(600)
    assert rows(page).first.get_attribute("data-avm-row") == first
    # « tout cocher » coche l'ensemble filtré, pas les 600 lignes dessinées
    page.locator('#all-vms-host [data-avm="all"]').check()
    expect(page.locator('#all-vms-host [data-avm="selcount"]')).to_contain_text("5000")
    page.locator('#all-vms-host [data-avm-bulk="clear"]').click()
    # recherche : une seule mise à jour après la frappe
    page.evaluate("""() => { window.__mut = 0; new MutationObserver(() => { window.__mut += 1; })
      .observe(document.querySelector('#all-vms-host tbody'), { childList: true }); }""")
    page.locator('#all-vms-host [data-avm="text"]').press_sequentially("vm0001", delay=20)
    expect(rows(page)).to_have_count(10)
    page.wait_for_timeout(300)
    assert page.evaluate("window.__mut") == 1
    expect(more).to_be_hidden()
    page.locator('#all-vms-host [data-avm="text"]').fill("")
    expect(rows(page)).to_have_count(300)
    # relecture identique : rien n'est redessiné ; une VM changée : redessin
    page.evaluate("document.querySelector('#all-vms-host tbody tr').dataset.mark = '1'")
    page.evaluate(REFRESH_MS)
    assert page.evaluate("!!document.querySelector('#all-vms-host tbody tr[data-mark]')")
    state["vms"][0]["phase"] = "Stopped"
    page.evaluate(REFRESH_MS)
    assert not page.evaluate("!!document.querySelector('#all-vms-host tbody tr[data-mark]')")
    assert state["errors"] == []


@pytest.mark.parametrize("n", [2000, 10000])
def test_vms_view_scale_measured(world, n):
    """Mesures du rapport (revue I-3) : rendu, relecture identique, relecture
    changée, à 2 000 et 10 000 VMs."""
    go, page, state = world
    state["vms"] = [hvm(f"vm{i:05d}", ns=f"ns{i % 7}", folder="/Prod" if i % 3 else None) for i in range(n)]
    t0 = time.time()
    go("#vms")
    expect(rows(page)).to_have_count(300, timeout=60000)
    ready = time.time() - t0
    render = page.evaluate(RENDER_MS)
    full = page.evaluate(FULL_MS, "namespace")
    same = page.evaluate(REFRESH_MS)
    state["vms"][0]["phase"] = "Stopped"
    changed = page.evaluate(REFRESH_MS)
    print(f"{n} VMs: page ready {ready:.1f}s, render without change {render:.0f} ms, full redraw {full:.0f} ms, "
          f"unchanged refresh {same:.0f} ms, changed refresh {changed:.0f} ms")
    assert render < 800 and full < 1500 and changed < 2500
