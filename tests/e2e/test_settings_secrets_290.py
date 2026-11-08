"""2.9.0 tâche 12 : Paramètres > Magasin de secrets, assistant d'activation,
bandeau de panne, objet protégé de la vue Disaster Recovery, sur routes
simulées (aucun OpenBao interrogé).

Cas du plan : activation bloquée par une vérification rouge, commandes `bao`
sans valeur, migration et retour d'une classe, bandeau « servi depuis la
mémoire jusqu'à HH:MM », noms hostiles échappés ; plus : historique des
versions sans valeur, rotation, clés transit gardées (saisie exacte),
instantanés et restauration (identifiant tapé), copie hors ligne.

Mutations jouées (rapport des tâches 11-12) : « Activer actif avec une
vérification rouge », « valeur affichée dans l'historique », « échappement
retiré ».
"""

import json
import re
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_auto_tabs_220 as t220  # noqa: E402

fulfill = t220.fulfill
HOSTILE = '<img src=x onerror="window.__pwned=1">'
CID = "abc123def456"
SHOTS = Path("/tmp/bmpost/290-shots")


def sse(events):
    return "".join(f"event: {k}\ndata: {json.dumps(v)}\n\n" for k, v in events)


def status_doc(**kw):
    d = {"mode": "b1", "active": True, "addr": "https://127.0.0.1:8210", "mount": "harvester-ops",
         "prefix": f"consoles/{CID}", "console_id": CID,
         "store": {"state": "ok", "cached": 3, "last_probe_at": time.time() - 20},
         "settings": {"cache_ttl_min": 15, "timeout": 10, "originals_keep_days": 7, "plan_secrets_keep_days": 7,
                      "snapshot_dir": "/mnt/backup/openbao", "snapshot_time": "02:30", "snapshot_keep": 14},
         "migrated": {"rancher-oidc": {"at": time.time() - 86400, "by": "alice", "complete": True}},
         "outage_test": None, "tf_vault_ack": None, "tf_entities_pending": [],
         "offline_copy": {"pending": False, "key_id": "hops-20261108-1", "acked_at": time.time() - 3600},
         "snapshots": {"last_ok_at": time.time() - 3600, "last_file": "openbao-20261108T023000Z-hops-20261108-1.snap"},
         "agent": {"present": True, "version": "2.9.0", "b1_installed": True, "current_key": "hops-20261108-1",
                   "audit": {"free": 40 << 30, "total": 50 << 30}, "secret_id_age_s": 5 * 86400, "last": None},
         "credentials": True, "tf_flavor": "opentofu", "warnings": []}
    d.update(kw)
    return d


CLASSES = {"order": ["replication-notify", "replication-hook", "rancher-oidc", "tf-kvm", "ssh-kvm", "ssh-cluster",
                     "kubeconfig"],
           "classes": [
               {"class": "replication-notify", "local": 2, "openbao": 0, "migrated": None, "originals": 0,
                "originals_oldest": None, "gate": None, "offline_copy_pending": False},
               {"class": "rancher-oidc", "local": 0, "openbao": 1,
                "migrated": {"at": 1794100000, "by": HOSTILE, "complete": True}, "originals": 1,
                "originals_oldest": 1794100000, "gate": None, "offline_copy_pending": False},
               {"class": "kubeconfig", "local": 3, "openbao": 0, "migrated": None, "originals": 0,
                "originals_oldest": None, "gate": "outage-test-first", "offline_copy_pending": False}]}


def open_secrets(context, flask_server, state, viewport=None):
    """Console à froid, routes /api/secrets/* simulées (`state` modifiable)."""
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');")
    page = context.new_page()
    if viewport:
        page.set_viewport_size(viewport)
    state.setdefault("calls", [])
    state.setdefault("outputs", {})
    state.setdefault("n", 0)

    def api(route, req):
        url = req.url.split("/api/secrets/", 1)[1]
        path = url.split("?")[0]
        body = json.loads(req.post_data or "{}") if req.method in ("POST", "PUT") else None
        if req.method in ("POST", "PUT"):
            state["calls"].append((path, body))
            state["n"] += 1
            aid = f"sx{state['n']:04d}"
            state["outputs"][aid] = state.get("next_output", {}).get(path, {"ok": True})
            return fulfill(route, {"action_id": aid}, 202)
        doc = {"status": lambda: state["status"], "banner": lambda: state.get("banner", {"active": False}),
               "classes": lambda: CLASSES, "rotation": lambda: state.get("rotation", {"active": False}),
               "tf-keys": lambda: state.get("tfkeys", {"keys": []}),
               "tf-entity-commands": lambda: {"entities": []},
               "snapshots": lambda: state.get("snaps", {}), "bundles": lambda: state.get("bundles", {"bundles": []}),
               "secrets": lambda: state.get("names", {"secrets": []}),
               "versions": lambda: state.get("versions", {"versions": []}),
               "assistant": lambda: state.get("assistant", {"commands": []}),
               "restore/preflight": lambda: state.get("preflight", {"busy": [], "tf_declarations": [],
                                                                    "kvm_hosts": [], "classes": [],
                                                                    "snapshot_at": 1794105000, "risks": False})}.get(path)
        return fulfill(route, doc() if doc else {"error": "x"}, 200 if doc else 404)
    page.route(re.compile(r".*/api/secrets/.*"), api)

    def action(route, req):
        aid = req.url.rsplit("/", 1)[1]
        out = state["outputs"].get(aid, {})
        fulfill(route, {"id": aid, "status": "done" if out.get("ok", True) else "error",
                        "result": {"output": out}, "error_summary": None})
    page.route(re.compile(r".*/api/action/sx\d+$"), action)

    def stream(route, req):
        aid = req.url.rsplit("/", 1)[1]
        out = state["outputs"].get(aid, {})
        route.fulfill(status=200, content_type="text/event-stream",
                      body=sse([("end", {"status": "done" if out.get("ok", True) else "error"})]))
    page.route(re.compile(r".*/api/stream/sx\d+$"), stream)
    page.on("dialog", lambda d: d.accept())
    page.goto(flask_server["base_url"])
    page.wait_for_load_state("networkidle")
    return page


def to_tab(page, sub=None):
    page.locator("#btn-settings").click()
    page.locator('.settings-tab[data-stab="secrets"]').click()
    body = page.locator("#secrets-settings-body")
    expect(body.locator(".sx-tabs")).to_be_visible()
    if sub:
        body.locator(f'[data-sx-tab="{sub}"]').click()
    return body


def shot(page, name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / name), full_page=False)


# ---------------------------------------------------------------------------
def test_status_tab_shows_mode_health_and_no_value(context, flask_server):
    st = {"status": status_doc(warnings=[{"code": "snapshot-stale", "level": "warn", "last_ok_at": 1794000000}])}
    page = open_secrets(context, flask_server, st)
    body = to_tab(page)
    expect(body.locator('[data-sx-mode="b1"]')).to_contain_text("Mode B1")
    expect(body.locator('[data-sx-state="ok"]')).to_contain_text("Reachable")
    expect(body.locator('[data-sx-warning="snapshot-stale"]')).to_contain_text("older than 2 days")
    expect(body).to_contain_text("hops-20261108-1")
    for b in body.locator("button").all():
        assert b.get_attribute("data-tip"), b.inner_text()                # info-bulle sur chaque contrôle
    shot(page, "secrets-status-desktop.png")


def test_local_mode_offers_the_assistant_only(context, flask_server):
    st = {"status": status_doc(mode="local", active=False, migrated={}, warnings=[])}
    page = open_secrets(context, flask_server, st)
    body = to_tab(page)
    expect(body.locator('[data-sx="enable"]')).to_be_visible()
    assert body.locator('[data-sx="disable"]').count() == 0
    body.locator('[data-sx="classes"]').count()
    body.locator('[data-sx-tab="classes"]').click()
    expect(body).to_contain_text("Activate OpenBao first")


def test_activation_blocked_by_a_red_check_then_allowed(context, flask_server):
    st = {"status": status_doc(mode="local", active=False, migrated={}),
          "bundles": {"bundles": ["harvester-ops-openbao-2.7.2-2.7.2-5.4.tar.gz"], "agent": True},
          "assistant": {"commands": ["bao secrets enable -path=harvester-ops kv-v2",
                                     "bao write -wrap-ttl=15m -f auth/approle/role/harvester-ops-x/secret-id"],
                        "policy": 'path "harvester-ops/data/consoles/x/*" {}', "prefix": "consoles/x"},
          "next_output": {"check": {"ok": False, "checks": [
              {"id": "address", "ok": True, "reason": "https://bao:8200"},
              {"id": "capabilities", "ok": False, "reason": "missing: transit/keys/" + HOSTILE}]}}}
    page = open_secrets(context, flask_server, st)
    body = to_tab(page)
    body.locator('[data-sx="enable"]').click()
    w = page.locator("#sx-wizard")
    expect(w).to_be_visible()
    act = w.locator('[data-wz="activate"]')
    expect(act).to_be_disabled()
    # mode A : commandes bao, sans valeur
    w.locator('[data-wz-mode="a"]').click()
    w.locator('[data-wz="addr"]').fill("https://bao.example:8200")
    w.locator('[data-wz="cidr"]').fill("10.0.0.5/32")
    w.locator('[data-wz="commands"]').click()
    expect(w.locator('[data-wz="cmds"]')).to_contain_text("bao secrets enable")
    w.locator('[data-wz="role"]').fill("rid-12345678")
    w.locator('[data-wz="wrap"]').fill("s.WRAPPEDTOKEN0123456789")
    shot(page, "secrets-wizard-server.png")
    w.locator('[data-wz-tab="checks"]').click()
    w.locator('[data-wz="run-checks"]').click()
    expect(w.locator('[data-wz-check="capabilities"]')).to_have_class(re.compile("fail"))
    expect(act).to_be_disabled()                                         # rouge : Activer grisé
    assert page.evaluate("() => window.__pwned") is None
    shot(page, "secrets-wizard-checks-red.png")
    check_call = [c for c in st["calls"] if c[0] == "check"][-1][1]
    assert check_call["wrap_token"] == "s.WRAPPEDTOKEN0123456789" and check_call["mode"] == "a"
    # tout vert : Activer permis
    st["next_output"]["check"] = {"ok": True, "checks": [{"id": "address", "ok": True, "reason": "ok"},
                                                         {"id": "roundtrip", "ok": True, "reason": "ok"}]}
    w.locator('[data-wz="run-checks"]').click()
    expect(w.locator('[data-wz-check="roundtrip"]')).to_have_class(re.compile("ok"))
    expect(act).to_be_enabled()
    act.click()
    expect(w).to_be_hidden()
    enable = [c for c in st["calls"] if c[0] == "enable"][-1][1]
    assert enable["mode"] == "a" and enable["role_id"] == "rid-12345678"


def test_mode_a_activation_allowed_with_the_real_check_output_before_deposit(context, flask_server):
    """Relecture finale I7 : la sortie réelle de `check` sur une console
    jamais activée (vecteur partagé avec test_harvester_secrets_cli_290 :
    la connexion est « après », pas rouge) laisse Activer permis."""
    real = json.loads((Path(__file__).resolve().parents[1] / "api" / "fixtures"
                       / "secrets-check-a-before-deposit.json").read_text())
    st = {"status": status_doc(mode="local", active=False, migrated={}),
          "assistant": {"commands": ["bao secrets enable -path=harvester-ops kv-v2"],
                        "policy": 'path "harvester-ops/data/consoles/x/*" {}', "prefix": "consoles/x"},
          "next_output": {"check": real}}
    page = open_secrets(context, flask_server, st)
    body = to_tab(page)
    body.locator('[data-sx="enable"]').click()
    w = page.locator("#sx-wizard")
    w.locator('[data-wz-mode="a"]').click()
    w.locator('[data-wz="addr"]').fill("https://bao.example:8200")
    w.locator('[data-wz="role"]').fill("rid-12345678")
    w.locator('[data-wz="wrap"]').fill("s.WRAPPEDTOKEN0123456789")
    w.locator('[data-wz-tab="checks"]').click()
    w.locator('[data-wz="run-checks"]').click()
    expect(w.locator('[data-wz-check="login"]')).to_have_class(re.compile("skip"))
    expect(w.locator('[data-wz-check="wrap"]')).to_have_class(re.compile("ok"))
    expect(w.locator('[data-wz="activate"]')).to_be_enabled()


def test_classes_migrate_and_back_with_hostile_names_escaped(context, flask_server):
    st = {"status": status_doc()}
    page = open_secrets(context, flask_server, st)
    body = to_tab(page, "classes")
    row = body.locator('[data-sx-class="replication-notify"]')
    expect(row).to_contain_text("2 local · 0 in OpenBao")
    row.locator("[data-sx-migrate]").click()
    expect(page.locator("#sx-feedback")).to_contain_text("Class moved to OpenBao")
    assert ("migrate", {"class": "replication-notify", "to": "openbao"}) in st["calls"]
    body.locator('[data-sx-class="rancher-oidc"] [data-sx-back]').click()
    expect(page.locator("#sx-feedback")).to_contain_text("Class back in local files")
    assert ("migrate", {"class": "rancher-oidc", "to": "local"}) in st["calls"]
    # barrière kubeconfig : bouton grisé avec sa raison
    kb = body.locator('[data-sx-class="kubeconfig"] [data-sx-migrate]')
    expect(kb).to_be_disabled()
    assert "simulated outage" in kb.get_attribute("data-tip")
    assert page.evaluate("() => window.__pwned") is None
    expect(body.locator('[data-sx-class="rancher-oidc"]')).to_contain_text("onerror")    # texte, pas balise
    shot(page, "secrets-classes-desktop.png")


def test_versions_show_authors_never_values(context, flask_server):
    st = {"status": status_doc(), "names": {"secrets": [{"ref": "openbao:rancher-oidc/r1#client_secret",
                                                         "name": "r1", "field": "client_secret"}]},
          "versions": {"versions": [{"version": 1, "created": "2026-11-01T10:00:00Z", "actor": "alice", "action": "a1",
                                     "value": "SECRET-VALUE-NEVER-SHOWN"},
                                    {"version": 2, "created": "2026-11-08T10:00:00Z", "actor": HOSTILE, "action": "a2"}]}}
    page = open_secrets(context, flask_server, st)
    body = to_tab(page, "versions")
    body.locator('[data-sx-ref="openbao:rancher-oidc/r1#client_secret"]').click()
    table = body.locator(".sx-version-table")
    expect(table.locator('[data-sx-version="2"]')).to_contain_text("current")
    expect(table.locator('[data-sx-version="1"]')).to_contain_text("alice")
    assert page.evaluate("() => window.__pwned") is None
    assert "SECRET-VALUE" not in page.content()                         # une valeur reçue par erreur n'est pas rendue
    table.locator('[data-sx-rollback="1"]').click()
    expect(page.locator("#sx-feedback")).to_contain_text("Previous value written again")
    assert ("rollback", {"ref": "openbao:rancher-oidc/r1#client_secret", "version": 1}) in st["calls"]
    expect(body).to_contain_text("Values are never shown")


def test_rotation_and_kept_transit_keys_need_the_exact_identifier(context, flask_server):
    st = {"status": status_doc(),
          "rotation": {"mode": "b1", "active": True, "secret_id": {"age_s": 86400, "agent": True},
                       "unseal_key": {"current": "hops-20261108-1", "created": time.time() - 86400 * 3},
                       "kvm": [{"host": "node2", "age_s": 400 * 86400, "store": "openbao", "old": True}],
                       "transit": [{"decl": "d1d1d1d1d1d1", "key": f"hops-{CID}-tf-d1d1d1d1d1d1"}], "owned": []},
          "tfkeys": {"keys": [{"decl": "e2e2e2e2e2e2", "key": "k", "retired_at": time.time() - 86400,
                               "delete_after": time.time() + 29 * 86400, "due": False, "trial": False}]}}
    page = open_secrets(context, flask_server, st)
    body = to_tab(page, "rotation")
    expect(body.locator('[data-sx-rot="ssh-kvm"]')).to_contain_text("old")
    body.locator('[data-sx-rotate="ssh-kvm"]').click()
    expect(page.locator("#sx-feedback")).to_contain_text("Rotated")
    assert ("rotate", {"what": "ssh-kvm", "target": "node2"}) in st["calls"]
    body.locator('[data-sx-tfkey-delete="e2e2e2e2e2e2"]').click()
    m = page.locator("#sx-confirm")
    ok = m.locator('[data-x="ok"]')
    expect(ok).to_be_disabled()
    m.locator(".sx-typed").fill("e2e2e2e2e2e")
    expect(ok).to_be_disabled()
    m.locator(".sx-typed").fill("e2e2e2e2e2e2")
    expect(ok).to_be_enabled()
    ok.click()
    expect(page.locator("#sx-feedback")).to_contain_text("Transit key removed")
    assert ("tf-keys/delete", {"decl": "e2e2e2e2e2e2", "confirm": "e2e2e2e2e2e2"}) in st["calls"]
    shot(page, "secrets-rotation-desktop.png")


def test_snapshots_restore_with_the_typed_console_id(context, flask_server):
    st = {"status": status_doc(),
          "snaps": {"dir": "/mnt/backup/openbao", "time": "02:30", "keep": 14, "stale_after_s": 172800,
                    "last": {"last_ok_at": time.time() - 3600, "last_file": "openbao-a.snap"},
                    "files": [{"file": "openbao-20261108T023000Z-hops-20261108-1.snap", "bytes": 40000,
                               "at": time.time() - 3600, "where": "dir"}],
                    "agent_files": [{"file": "openbao-20261101T000000Z-hops-20261101-1-pre-upgrade.snap",
                                     "bytes": 38000, "at": time.time() - 7 * 86400, "where": "agent"}]}}
    page = open_secrets(context, flask_server, st)
    body = to_tab(page, "snapshots")
    expect(body.locator("[data-sx-snap]")).to_have_count(2)
    shot(page, "secrets-snapshots-desktop.png")
    body.locator('[data-sx-restore="openbao-20261108T023000Z-hops-20261108-1.snap"]').click()
    m = page.locator("#sx-confirm")
    expect(m).to_contain_text("Secrets written since this snapshot are lost")
    m.locator(".sx-typed").fill(CID)
    m.locator('[data-x="ok"]').click()
    expect(page.locator("#sx-feedback")).to_contain_text("Snapshot restored")
    assert ("restore", {"file": "openbao-20261108T023000Z-hops-20261108-1.snap", "confirm": CID,
                        "ack_risks": False, "force": False}) in st["calls"]


def test_restore_shows_what_it_would_break_before_anything_stops(context, flask_server):
    """Relecture finale I2 : contrôle avant restauration montré dans la
    fenêtre (actions en cours, déclaration chiffrée plus récente, clé KVM
    tournée, classe migrée depuis), noms échappés ; la saisie de
    l'identifiant acquitte la liste."""
    snap = "openbao-20261108T023000Z-hops-20261108-1.snap"
    st = {"status": status_doc(),
          "snaps": {"dir": "/mnt/backup/openbao", "time": "02:30", "keep": 14, "stale_after_s": 172800,
                    "files": [{"file": snap, "bytes": 40000, "at": time.time() - 3600, "where": "dir"}],
                    "agent_files": []},
          "preflight": {"busy": [{"id": "a1", "action": "tf-apply:c1/web", "cluster": "c1"}],
                        "tf_declarations": [{"id": "d1", "name": HOSTILE, "reason": "created after the snapshot"}],
                        "kvm_hosts": [{"host": "node2", "rotated_at": 1}], "classes": ["kubeconfig"],
                        "snapshot_at": 1794105000, "risks": True}}
    page = open_secrets(context, flask_server, st)
    body = to_tab(page, "snapshots")
    body.locator(f'[data-sx-restore="{snap}"]').click()
    m = page.locator("#sx-confirm")
    items = m.locator("[data-sx-preflight] li")
    expect(items).to_have_count(4)
    expect(m).to_contain_text("tf-apply:c1/web")
    expect(m).to_contain_text("KVM host node2")
    expect(m).to_contain_text("Class kubeconfig")
    expect(m).to_contain_text("onerror")                     # texte, pas balise
    assert page.evaluate("() => window.__pwned") is None
    m.locator(".sx-typed").fill(CID)
    m.locator('[data-x="ok"]').click()
    expect(page.locator("#sx-feedback")).to_contain_text("Snapshot restored")
    assert ("restore", {"file": snap, "confirm": CID, "ack_risks": True, "force": True}) in st["calls"]


def test_offline_copy_steps(context, flask_server):
    st = {"status": status_doc(offline_copy={"pending": True}),
          "next_output": {"offline-copy/export": {"ok": True, "key_id": "hops-20261108-1"}}}
    page = open_secrets(context, flask_server, st)
    body = to_tab(page)
    box = body.locator("[data-sx-offline]")
    expect(box).to_contain_text("Offline copy of the unseal key")
    box.locator('[data-sx="export"]').click()
    page.locator("#sx-confirm .sx-typed").fill(CID)
    page.locator('#sx-confirm [data-x="ok"]').click()
    link = page.locator("[data-sx-download]")
    expect(link).to_contain_text("hops-20261108-1")
    assert "/api/secrets/offline-copy/download?action=sx" in link.get_attribute("href")
    box.locator('[data-sx="ack"]').click()
    page.locator("#sx-confirm .sx-typed").fill("hops-20261108-1")
    page.locator('#sx-confirm [data-x="ok"]').click()
    expect(page.locator("#sx-feedback")).to_contain_text("Offline copy acknowledged")
    assert ("offline-copy/ack", {"key_id": "hops-20261108-1"}) in st["calls"]


def test_narrow_screen(context, flask_server):
    st = {"status": status_doc()}
    page = open_secrets(context, flask_server, st, viewport={"width": 420, "height": 860})
    body = to_tab(page)
    expect(body.locator('[data-sx-mode="b1"]')).to_be_visible()
    shot(page, "secrets-status-narrow.png")
    body.locator('[data-sx-tab="classes"]').click()
    expect(body.locator(".sx-classes")).to_be_visible()
    shot(page, "secrets-classes-narrow.png")
