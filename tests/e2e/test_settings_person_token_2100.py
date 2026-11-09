"""2.10.0 (tâche 11) : Paramètres > Secrets > Jetons personnels, marque de
l'identité des secrets dans Activité, bandeau des refus personnels, sur
routes simulées (aucun OpenBao interrogé).

Cas du plan : activation bloquée tant qu'une vérification est rouge, compte
local renvoyé vers une connexion par Rancher, commandes du mode A sans
valeur, marque dans Activité, noms hostiles échappés ; plus : désactiver,
tout retirer (identifiant retapé), groupes avec les entités vues, captures
large et étroite.

Mutations jouées (rapport des tâches 11 et 12) : « Activer actif avec une
vérification rouge », « échappement retiré », « vérifications permises à un
compte local », « marque d'Activité absente ».
"""

import re
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_auto_tabs_220 as t220  # noqa: E402
import test_settings_secrets_290 as t290  # noqa: E402
from nav import goto  # noqa: E402

fulfill = t220.fulfill
HOSTILE = t290.HOSTILE
CID = t290.CID
SHOTS = Path("/tmp/bmpost/shots-2100")
UUID_A = "11111111-2222-3333-4444-555555555555"
UUID_B = "22222222-3333-4444-5555-666666666666"
GREEN = [{"id": i, "ok": True, "reason": f"{i} fine"} for i in ("discovery", "login", "token", "capabilities", "revoke")]


def doc(**kw):
    d = {"mode": "b1", "active": True, "enabled": False, "groups_only": False, "tokens": 0,
         "group_classes": ["kubeconfig", "ssh-cluster", "ssh-kvm", "tf-kvm", "eso"],
         "limits": {"ranchers": 16, "groups": 32, "members": 200},
         "me": {"sso": True, "rancher": "config", "id_token": True, "kind": "sso"},
         "ranchers": [
             {"key": "config", "id": "rancher-home", "label": "Rancher", "url": "https://rancher.example",
              "origin": "config", "issuer": "https://rancher.example/oidc", "client_id": "client-7zjw5jtpx6",
              "sso": True, "has_ca": False, "configured": None, "pending": None},
             {"key": "lab", "id": "lab", "label": HOSTILE, "url": "https://lab.example", "origin": "console",
              "issuer": "https://lab.example/oidc", "client_id": "client-lab", "sso": True, "has_ca": True,
              "configured": {"mount": f"jwt-hops-{CID}-p-lab", "keys": "static", "keys_sha256": "ab" * 32,
                             "configured_by": HOSTILE, "configured_at": 1794100000, "checked_at": 1794100100,
                             "keys_changed": True, "keys_unreadable": False, "keys_pending_confirm": False},
              "pending": None}],
         "groups": {"ops": {"classes": ["ssh-kvm"], "clusters": ["node2"], "members": [UUID_A], "at": 1, "by": "a"}},
         "entities": [{"entity": UUID_A, "sub": "user-kk67j", "name": HOSTILE, "user": "alice@rancher",
                       "rancher": "config", "last_at": 1794100000},
                      {"entity": UUID_B, "sub": "u-lwpz9", "name": "Bob", "user": "bob@rancher", "rancher": "lab",
                       "last_at": 1794000000}]}
    d.update(kw)
    return d


def open_tab(context, flask_server, pt, mode="b1", viewport=None):
    st = {"status": t290.status_doc(mode=mode)}
    page = t290.open_secrets(context, flask_server, st, viewport=viewport)
    page.route(re.compile(r".*/api/secrets/person-token$"), lambda r, q: fulfill(r, pt))
    body = t290.to_tab(page, "persons")
    expect(body.locator(".sx-pt-ranchers")).to_be_visible(timeout=5000)
    return page, body, st


def test_tab_lists_ranchers_groups_and_entities_escaped(context, flask_server):
    page, body, _ = open_tab(context, flask_server, doc())
    expect(body.locator('[data-sx-pt-state="off"]')).to_be_visible()
    rows = body.locator(".sx-pt-rancher")
    expect(rows).to_have_count(2)
    expect(rows.nth(0).locator('[data-sx-pt-rstate="none"]')).to_be_visible()
    expect(rows.nth(1)).to_contain_text(HOSTILE)                        # texte, pas balise
    expect(rows.nth(1).locator("[data-sx-pt-changed]")).to_be_visible()
    expect(rows.nth(1).locator("[data-sx-pt-keys]")).to_be_visible()     # clés statiques : mise à jour
    expect(body.locator(".sx-pt-group")).to_have_count(1)
    body.locator(".sx-pt-entities summary").click()
    expect(body.locator(".sx-pt-entity")).to_have_count(2)
    expect(body.locator(".sx-pt-entity").first).to_contain_text(HOSTILE)
    assert page.evaluate("window.__pwned") is None and body.locator("img").count() == 0
    for b in body.locator("button").all():
        assert b.get_attribute("data-tip"), b.inner_text()               # info-bulle sur chaque contrôle
    expect(body.locator('[data-sx="pt-off"]')).to_be_disabled()          # déjà désactivés


def test_activation_blocked_until_all_five_checks_are_green(context, flask_server):
    page, body, st = open_tab(context, flask_server, doc())
    body.locator('[data-sx-pt-setup="config"]').click()
    w = page.locator("#sx-pt-wizard")
    expect(w).to_be_visible()
    act = w.locator('[data-pw="activate"]')
    expect(act).to_be_disabled()
    expect(w.locator('[data-pw-keys="static"]')).to_be_disabled()        # B1 : découverte seulement
    w.locator('[data-pw-tab="checks"]').click()
    expect(w.locator('[data-pw="run-checks"]')).to_be_disabled()         # méthode à poser d'abord
    w.locator('[data-pw-tab="server"]').click()
    w.locator('[data-pw="apply"]').click()
    expect(w.locator('[data-pw="applied"]')).to_contain_text("Method set by the agent", timeout=5000)
    assert ("person-token/assistant", {"rancher": "config", "keys": "discovery"}) in st["calls"]
    # une vérification rouge : Activer reste grisé
    st["next_output"] = {"person-token/check": {"ok": False, "checks": GREEN[:2] + [
        {"id": "token", "ok": False, "reason": "policies default, root"}] + GREEN[3:]}}
    w.locator('[data-pw-tab="checks"]').click()
    w.locator('[data-pw="run-checks"]').click()
    expect(w.locator('[data-pw-check="token"].fail')).to_be_visible(timeout=5000)
    expect(act).to_be_disabled()
    expect(act).to_have_attribute("data-tip", re.compile("All five checks"))
    act.click(force=True)
    assert not any(c[0] == "person-token/enable" for c in st["calls"])
    # toutes vertes : Activer s'ouvre et active
    st["next_output"] = {"person-token/check": {"ok": True, "checks": GREEN}}
    w.locator('[data-pw="run-checks"]').click()
    expect(w.locator('[data-pw-check="revoke"].ok')).to_be_visible(timeout=5000)
    w.locator('[data-pw-tab="confirm"]').click()
    expect(w.locator('[data-pw="summary"]')).to_contain_text("Every check is green")
    expect(act).to_be_enabled()
    act.click()
    expect(page.locator("#sx-feedback")).to_contain_text("Personal tokens on for Rancher", timeout=5000)
    assert ("person-token/enable", {"rancher": "config"}) in st["calls"]


@pytest.mark.parametrize("me,text", [
    ({"sso": False, "rancher": None, "id_token": False, "kind": "local"}, "Connect through Rancher (Rancher)"),
    ({"sso": True, "rancher": "config", "id_token": False, "kind": "sso"}, "no identity token"),
    ({"sso": True, "rancher": "lab", "id_token": True, "kind": "sso"}, "connect through Rancher to check"),
])
def test_checks_say_who_can_run_them(context, flask_server, me, text):
    pt = doc(me=me)
    pt["ranchers"][0]["pending"] = {"mount": f"jwt-hops-{CID}-p-config", "keys": "discovery"}
    page, body, st = open_tab(context, flask_server, pt)
    if not me["sso"]:
        expect(body.locator('[data-sx-pt-me="local"]')).to_be_visible()
    body.locator('[data-sx-pt-setup="config"]').click()
    w = page.locator("#sx-pt-wizard")
    w.locator('[data-pw-tab="checks"]').click()
    expect(w.locator('[data-pw="block"]')).to_contain_text(text, ignore_case=True)
    expect(w.locator('[data-pw="run-checks"]')).to_be_disabled()
    expect(w.locator('[data-pw="activate"]')).to_be_disabled()
    assert not any(c[0] == "person-token/check" for c in st["calls"])


def test_mode_a_gives_commands_without_value(context, flask_server):
    cmds = ["export BAO_ADDR=https://bao.example:8200", f"bao auth enable -path=jwt-hops-{CID}-p-config jwt",
            f"bao policy write hops-person-{CID} - <<'EOF'\npath \"x\" {{}}\nEOF"]
    page, body, st = open_tab(context, flask_server, doc(mode="a"), mode="a")
    body.locator('[data-sx-pt-setup="config"]').click()
    w = page.locator("#sx-pt-wizard")
    expect(w.locator('[data-pw-keys="static"]')).to_be_enabled()
    w.locator('[data-pw-keys="static"]').click()
    w.locator('[data-pw="cidr"]').fill("10.0.0.5/32")
    st["next_output"] = {"person-token/assistant": {"ok": True, "commands": cmds}}
    w.locator('[data-pw="apply"]').click()
    expect(w.locator('[data-pw="cmds"]')).to_contain_text("bao auth enable", timeout=5000)
    assert ("person-token/assistant", {"rancher": "config", "keys": "static", "console_cidr": "10.0.0.5/32"}) in st["calls"]
    text = w.locator('[data-pw="cmds"]').inner_text()
    assert "eyJ" not in text and "s." not in text.replace("hops-person", "")


def test_turn_off_and_remove_everything(context, flask_server):
    page, body, st = open_tab(context, flask_server, doc(enabled=True, tokens=2))
    expect(body.locator('[data-sx-pt-state="on"]')).to_be_visible()
    body.locator('[data-sx="pt-off"]').click()                          # confirm() accepté par le banc
    expect(page.locator("#sx-feedback")).to_contain_text("tokens in use revoked", timeout=5000)
    assert ("person-token/disable", {}) in st["calls"]
    page.locator('#secrets-settings-body [data-sx-tab="persons"]').click()
    page.locator('#secrets-settings-body [data-sx="pt-remove-all"]').click()
    c = page.locator("#sx-confirm")
    ok = c.locator('[data-x="ok"]')
    expect(ok).to_be_disabled()
    c.locator(".sx-typed").fill(CID)
    ok.click()
    expect(page.locator("#sx-feedback")).to_contain_text("Everything removed", timeout=5000)
    assert ("person-token/disable", {"remove": True, "confirm": CID}) in st["calls"]


def test_group_window_offers_the_entities_seen(context, flask_server):
    page, body, st = open_tab(context, flask_server, doc())
    body.locator('[data-sx-pt-group="ops"]').click()
    g = page.locator("#sx-pt-group")
    expect(g).to_be_visible()
    expect(g.locator('[data-pg="name"]')).to_be_disabled()
    expect(g.locator(f'[data-pg-member="{UUID_A}"]')).to_be_checked()
    expect(g.locator(f'[data-pg-member="{UUID_B}"]')).not_to_be_checked()
    expect(g).to_contain_text(HOSTILE)
    g.locator(f'[data-pg-member="{UUID_B}"]').check()
    g.locator('[data-pg-class="tf-kvm"]').check()
    g.locator('[data-pg="save"]').click()
    expect(page.locator("#sx-feedback")).to_contain_text("Group set", timeout=5000)
    assert ("person-token/groups", {"name": "ops", "classes": ["ssh-kvm", "tf-kvm"], "clusters": ["node2"],
                                    "members": [UUID_A, UUID_B]}) in st["calls"]
    assert page.evaluate("window.__pwned") is None


def test_banner_says_a_personal_refusal_and_changed_keys(context, flask_server):
    st = {"status": t290.status_doc(), "banner": {"active": True, "mode": "b1", "state": "ok",
                                                  "person": {"code": "openbao-person-login-failed", "at": time.time()},
                                                  "jwt_keys_changed": True, "eso_keys_changed": False,
                                                  "jwt_keys_unreadable": False}}
    page = t290.open_secrets(context, flask_server, st)
    el = page.locator("#secrets-banner")
    expect(el).to_be_visible(timeout=8000)
    expect(el.locator('[data-sx-banner-note="openbao-person-login-failed"]')).to_contain_text("refused your personal")
    expect(el.locator('[data-sx-banner-note="openbao-jwt-keys-changed"]')).to_be_visible()
    el.locator("[data-sx-banner-open]").click()
    expect(page.locator('#secrets-settings-body [data-sx-tab="persons"].active')).to_be_visible(timeout=5000)


def _activity_page(context, flask_server, identity):
    context.add_init_script("localStorage.setItem('harvester_ops_language','en');"
                            "localStorage.setItem('harvester_ops_sidebar_pinned','1');")
    page = context.new_page()
    done = {"id": "sidbadge0001", "action": f"kvm:vm-start:node2/{HOSTILE}", "cluster": "kvm:node2", "status": "done",
            "exit_code": 0, "started_at": time.time() - 9, "ended_at": time.time() - 2, "dry_run": False,
            "error_summary": None, "secret_identity": identity, "result": {}}
    page.route("**/api/activity*", lambda r, q: fulfill(r, {
        "in_progress": [], "actions_done": [done], "log_files": [], "total": 1,
        "facets": {"clusters": ["kvm:node2"], "statuses": ["done"], "actions": [done["action"]]}}))
    page.route("**/api/stream/*", lambda r, q: r.fulfill(status=200, content_type="text/event-stream",
                                                         body=t290.sse([("end", {"status": "done"})])))
    goto(page, flask_server["base_url"], "#gpus")
    page.wait_for_function("window.App && window.i18n")
    page.click('#host-nav [data-hnav-root="activity"]', timeout=5000)
    return page


@pytest.mark.parametrize("identity,label", [("person", "personal token"), ("service", "service"),
                                            ("late-service", "late service"), (None, None)])
def test_activity_marks_the_identity_of_the_secrets(context, flask_server, identity, label):
    page = _activity_page(context, flask_server, identity)
    row = page.locator("#activity-history tbody tr").first
    expect(row).to_contain_text("vm-start", timeout=10000)
    badge = row.locator("[data-secret-identity]")
    if label is None:
        expect(badge).to_have_count(0)
        return
    expect(badge).to_have_text(label)
    assert badge.get_attribute("title")
    row.click()
    expect(page.locator(".activity-detail-meta [data-secret-identity]")).to_have_text(label, timeout=5000)
    assert page.locator("#activity-history img").count() == 0


@pytest.mark.parametrize("size", [(1440, 900), (390, 844)])
def test_screenshots_desktop_and_narrow(context, flask_server, size):
    pt = doc(enabled=True, tokens=3)
    pt["ranchers"][0]["configured"] = {"mount": f"jwt-hops-{CID}-p-config", "keys": "discovery",
                                       "keys_sha256": "cd" * 32, "configured_by": "alice",
                                       "configured_at": 1794100000, "checked_at": 1794100100,
                                       "keys_changed": False, "keys_unreadable": False, "keys_pending_confirm": False}
    pt["ranchers"][1]["label"] = "Lab Rancher"
    pt["ranchers"][1]["configured"]["configured_by"] = "bob"
    for e in pt["entities"]:
        e["name"] = e["name"] if e["name"] != HOSTILE else "Alice Doe"
    page, body, st = open_tab(context, flask_server, pt, viewport={"width": size[0], "height": size[1]})
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.add_style_tag(content="#bottom-dock { display: none !important; }")
    page.screenshot(path=str(SHOTS / f"settings-person-token-{size[0]}.png"))
    body.locator(".sx-pt-groups").screenshot(path=str(SHOTS / f"settings-person-token-groups-{size[0]}.png"))
    body.locator('[data-sx-pt-setup="config"]').click()
    w = page.locator("#sx-pt-wizard")
    st["next_output"] = {"person-token/check": {"ok": False, "checks": GREEN[:3] + [
        {"id": "capabilities", "ok": False, "reason": "unexpected: kubeconfig/harv1 (read, want deny)"}] + GREEN[4:]}}
    w.locator('[data-pw-tab="checks"]').click()
    w.locator('[data-pw="run-checks"]').click()
    expect(w.locator('[data-pw-check="capabilities"].fail')).to_be_visible(timeout=5000)
    w.locator(".modal").screenshot(path=str(SHOTS / f"settings-person-token-wizard-{size[0]}.png"))
    # rien ne déborde horizontalement de la fenêtre
    assert page.evaluate("document.querySelector('#sx-pt-wizard .modal').scrollWidth <= window.innerWidth + 1")
