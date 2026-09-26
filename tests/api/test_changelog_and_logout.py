"""v1.56.0 : l'historique des versions (clic sur le numéro de version) et la
déconnexion d'un compte local.
"""

import base64
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402


def basic(user, pw="x"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


# -- historique des versions ------------------------------------------------

def test_the_changelog_is_parsed_into_releases():
    text = """# Changelog

Intro text.

## [2.1.0] - 2026-10-01 - Something new

### Added
- **A feature**: the first line
  and its continuation.
- Another one.

### Fixed
- A bug.

## [1.18.0] — 2026-09-10 — Old heading style

### Added
- Old item.
"""
    rel = wapp._parse_changelog(text)
    assert [r["version"] for r in rel] == ["2.1.0", "1.18.0"]
    assert rel[0]["date"] == "2026-10-01" and rel[0]["title"] == "Something new"
    assert rel[0]["sections"][0] == {"name": "Added", "items": [
        "**A feature**: the first line and its continuation.", "Another one."]}
    assert rel[0]["sections"][1] == {"name": "Fixed", "items": ["A bug."]}
    assert rel[1]["title"] == "Old heading style" and rel[1]["sections"][0]["items"] == ["Old item."]


def test_the_real_changelog_reads_back_completely():
    rel = wapp._parse_changelog((ROOT / "CHANGELOG.md").read_text())
    version = (ROOT / "VERSION").read_text().strip()
    assert rel[0]["version"] == version
    assert all(r["date"] and r["title"] and r["sections"] for r in rel), \
        [r["version"] for r in rel if not (r["date"] and r["title"] and r["sections"])]
    assert rel[-1]["version"] == "1.0.0"


def test_the_changelog_route(monkeypatch, tmp_path):
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "absent")
    with wapp.app.test_client() as c:
        d = c.get("/api/changelog").get_json()
        assert d["current"] == wapp._harvester_ops_version()
        assert d["releases"][0]["version"] == (ROOT / "VERSION").read_text().strip()
    monkeypatch.setattr(wapp, "_changelog_path", lambda: tmp_path / "none.md")
    with wapp.app.test_client() as c:
        d = c.get("/api/changelog").get_json()
        assert d["releases"] == [] and d["error"] == "changelog not shipped"


def test_the_image_ships_its_version_and_changelog():
    """L'image packagée affichait « v1.0.0 » (valeur figée dans l'image) et
    n'avait pas d'historique : VERSION et CHANGELOG.md y sont maintenant, à
    l'endroit où la console les cherche."""
    src = (ROOT / "container" / "Containerfile").read_text()
    assert "COPY VERSION CHANGELOG.md /opt/harvester-ops/" in src
    assert "HARVESTER_OPS_VERSION=1.0.0" not in src
    # la console cherche VERSION et CHANGELOG.md au-dessus de web/
    assert wapp._changelog_path() == ROOT / "CHANGELOG.md"


# -- déconnexion d'un compte local ---------------------------------------------

def test_a_local_account_signs_out(monkeypatch, tmp_path):
    htpasswd = tmp_path / "htpasswd"
    htpasswd.write_text("")
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", htpasswd)
    monkeypatch.setattr(wapp, "check_auth", lambda u, p: u == "alice")
    with wapp.app.test_client() as c:
        # le vrai compte ne « déconnecte » rien : le navigateur doit retenir
        # le compte factice, et ne le fait que sur une réponse 200
        r = c.get("/logout/local", headers=basic("alice"))
        assert r.status_code == 401 and "Basic" in r.headers["WWW-Authenticate"]
        r = c.get("/logout/local", headers=basic(wapp.LOGOUT_PSEUDO_USER, "123"))
        assert r.status_code == 200 and r.get_json()["login"] == "/login?signed_out=1"
        # le compte factice n'ouvre rien d'autre
        assert c.get("/api/whoami", headers=basic(wapp.LOGOUT_PSEUDO_USER)).status_code == 401
        page = c.get("/login?signed_out=1").get_data(as_text=True)
        assert "login-signed-out" in page
        assert "login-signed-out" not in c.get("/login").get_data(as_text=True)


def test_an_open_console_has_nothing_to_forget(monkeypatch, tmp_path):
    monkeypatch.setattr(wapp, "HTPASSWD_PATH", tmp_path / "absent")
    with wapp.app.test_client() as c:
        r = c.get("/logout/local")
        assert r.status_code == 200 and r.get_json()["login"] == "/"
