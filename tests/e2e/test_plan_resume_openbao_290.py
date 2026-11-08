"""2.9.0 tâche 12 : reprise d'un déroulé quand OpenBao garde ses secrets
(décision 141), sur routes simulées.

- OpenBao actif : la fenêtre de reprise dit « secrets gardés dans OpenBao
  jusqu'à la fin du déroulé », ne montre pas le formulaire des secrets, et
  la reprise part sans secret ; la réponse dit qu'ils ont été lus ;
- OpenBao muet à la reprise : 503 `secrets_prompt`, le formulaire revient
  avec le code ;
- « Taper de nouveaux secrets » rouvre le formulaire ; « Oublier les
  secrets gardés » (administrateur) lance l'action suivie, caché à un
  opérateur.
"""

import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_bm_plans_240 as t240  # noqa: E402

fulfill = t240.fulfill
RUN = t240.DEPLOY_RUN
ACT = {"deploy": {"in_progress": [], "actions_done": [RUN]}}


def open_resume(context, flask_server, role="admin", resume=None, active=True):
    page, st = t240.open_page(context, flask_server, "#automation/pxe/history", role=role, activity=ACT,
                              resume=resume)
    page.route(re.compile(r".*/api/secrets/banner$"), lambda r, q: fulfill(r, {
        "active": active, "mode": "b1", "state": "ok", "down_since": None, "cache_until": None, "error": None}))
    st["forget"] = []

    def forget(route, req):
        st["forget"].append(req.post_data_json)
        fulfill(route, {"action_id": "f00000000001"}, 202)
    page.route(re.compile(r".*/api/deploy/[^/]+/forget-secrets$"), forget)
    page.locator(".bm-deploy-resume").click()
    win = page.locator(f"#fp-deploy-resume-{RUN['id']}")
    expect(win).to_be_visible()
    return page, st, win


def test_resume_without_the_secrets_form_when_openbao_keeps_them(context, flask_server):
    ok = ({"action_id": "fedcba987654", "resumes": RUN["id"], "findings": [], "secrets_source": "openbao"}, 202)
    page, st, win = open_resume(context, flask_server, resume=[ok])
    expect(win.locator(".dpw-kept")).to_be_visible()
    expect(win.locator(".dpw-kept")).to_contain_text("kept in OpenBao until the end of the run")
    expect(win.locator(".dpw-secrets")).to_be_hidden()
    win.locator(".dpw-launch").click()
    expect(win.locator('[data-dpw-source="openbao"]')).to_contain_text("Secrets read in OpenBao")
    assert st["resume"] == [{"action_id": RUN["id"]}]
    assert not st["errors"]


def test_openbao_down_at_resume_asks_for_the_secrets(context, flask_server):
    down = ({"error": "OpenBao does not answer (openbao-sealed): type the secrets to resume",
             "code": "openbao-sealed", "fields": ["token", "bmc_password"], "secrets_prompt": True}, 503)
    page, st, win = open_resume(context, flask_server, resume=[down])
    expect(win.locator(".dpw-secrets")).to_be_hidden()
    win.locator(".dpw-launch").click()
    expect(win.locator('[data-dpw-prompt="install"]')).to_contain_text("openbao-sealed")
    expect(win.locator(".dpw-secrets")).to_be_visible()
    expect(win.locator(".dpw-kept")).to_be_hidden()
    win.locator('[data-dpw-secret="token"]').fill("TOK-290")
    win.locator('[data-dpw-secret="bmc_password"]').fill("BMC-290")
    win.locator(".dpw-launch").click()
    expect(win.locator(".dpw-follow")).to_be_visible()
    assert st["resume"][1]["secrets"] == {"token": "TOK-290", "bmc_password": "BMC-290"}


def test_type_new_secrets_and_forget_the_kept_ones(context, flask_server):
    page, st, win = open_resume(context, flask_server)
    expect(win.locator(".dpw-kept")).to_be_visible()
    win.locator(".dpw-type-secrets").click()
    expect(win.locator(".dpw-secrets")).to_be_visible()
    forget = win.locator(".dpw-forget")
    assert forget.get_attribute("data-tip")
    forget.click()
    expect(win.locator(".dpw-msg")).to_contain_text("being erased")
    assert st["forget"] == [{"action_id": RUN["id"]}]


def test_forget_hidden_for_an_operator_and_nothing_when_openbao_is_inactive(context, flask_server):
    page, st, win = open_resume(context, flask_server, role="operator")
    expect(win.locator(".dpw-kept")).to_be_visible()
    expect(win.locator(".dpw-forget")).to_be_hidden()
    page2, st2, win2 = open_resume(context, flask_server, active=False)
    page2.wait_for_timeout(300)
    expect(win2.locator(".dpw-kept")).to_be_hidden()
    expect(win2.locator(".dpw-secrets")).to_be_visible()                 # comportement 2.4.0
