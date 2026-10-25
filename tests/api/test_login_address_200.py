"""2.0.0 : une adresse ouverte avant la connexion est rouverte après.

`Routes.acceptable` décide ce qui peut être gardé le temps de la connexion :
une adresse que la grammaire comprend, ou un ancien jeton connu, sous une
forme courte et sans caractère actif. Le reste est oublié."""

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent

RUNNER = r"""
const R = require('./web/static/js/routes.js');
const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));
console.log(JSON.stringify(cases.map((h) => R.acceptable(h))));
"""

CASES = [
    ("#harvester/harv1/params/network/overlay", True),
    ("#harvester/Lab_1/vms", True),
    ("#kvm/kvm-paris.01/params/pools", True),
    ("#vms", True),
    ("#dr/rp-0123456789ab", True),
    ("#storage", True),                      # ancien jeton
    ("#kvmhosts", True),
    ("#automation/capi", True),
    ("", False),
    ("#", False),
    ("#nowhere", False),
    ("#harvester/<img src=x onerror=alert(1)>", False),
    ("#harvester/%3Cimg%20src%3Dx%3E", False),
    ("#harvester/harv1/params/nope", False),
    ("javascript:alert(1)", False),
    ("#harvester/harv1\"onmouseover=x", False),
    ("#" + "a/" * 300, False),
]


@pytest.mark.parametrize("hash,ok", CASES)
def test_acceptable(hash, ok):
    try:
        r = subprocess.run(["node", "-e", RUNNER], input=json.dumps([hash]), capture_output=True,
                           text=True, cwd=str(ROOT), timeout=30)
    except FileNotFoundError:
        pytest.skip("node absent")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == [ok]


def test_the_login_page_keeps_only_an_acceptable_address_and_never_writes_it():
    html = (ROOT / "web" / "templates" / "login.html").read_text()
    i = html.index("harvester_ops_after_login")
    block = html[html.rindex("<script>", 0, i):html.index("</script>", i)]
    assert "Routes.acceptable(h)" in block and "sessionStorage.setItem" in block
    assert "innerHTML" not in block and "document.write" not in block
    assert '<script src="/static/js/routes.js"></script>' in html


def test_the_router_validates_again_before_using_it():
    src = (ROOT / "web" / "static" / "js" / "router.js").read_text()
    i = src.index("function restoreAfterLogin")
    body = src[i:src.index("\n  }\n", i)]
    assert "R().acceptable(h)" in body and "removeItem(AFTER_LOGIN)" in body
    assert "location.hash" in body                     # une adresse déjà demandée l'emporte
    assert src.index("restoreAfterLogin();") > src.index("function start()")
