"""v1.79.0 : les Rancher réglés dans l'interface, le magasin (`web/rancher_servers.py`).

Lecture, priorité de config.yaml, écriture 0600, chemins relatifs qui
survivent au déplacement du répertoire d'état, secret jamais rendu, relecture
à chaud, contraintes de version des charts.
"""

import os
import shutil
import stat
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import rancher_servers as srv  # noqa: E402

PEM = "-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIUQ==\n-----END CERTIFICATE-----"


@pytest.fixture
def store(tmp_path):
    box = {"state": tmp_path / "state"}
    return srv.Store(lambda: box["state"]), box


def test_slug_and_id():
    assert srv.slug("Rancher de prod !") == "rancher-de-prod"
    assert srv.slug("") == "rancher"
    assert srv.ID_RE.match(srv.slug("x" * 200))


def test_create_writes_a_private_file_with_defaults(store):
    st, box = store
    x = st.create({}, {"label": "Prod", "url": "https://rancher.example.com/"})
    assert x["id"] == "prod" and x["url"] == "https://rancher.example.com" and x["origin"] == "console"
    assert x["default_role"] == "operator" and x["session_hours"] == 12 and x["direct_enabled"] is True
    f = box["state"] / "rancher.d" / "prod.yaml"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(f.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("data, msg", [
    ({"label": "a", "url": "http://rancher.example.com"}, "https"),
    ({"label": "a", "url": "https://u:p@rancher.example.com"}, "credentials"),
    ({"label": "", "url": "https://rancher.example.com"}, "label"),
    ({"label": "a", "url": "https://r.example.com", "default_role": "root"}, "default_role"),
    ({"label": "a", "url": "https://r.example.com", "session_hours": 25}, "session_hours"),
    ({"label": "a", "url": "https://r.example.com", "ca": "-----BEGIN PRIVATE KEY-----"}, "private key"),
    ({"label": "a", "url": "https://r.example.com", "ca": "hello"}, "PEM"),
])
def test_bad_settings_are_refused(store, data, msg):
    st, _ = store
    with pytest.raises(srv.ServerError) as e:
        st.create({}, data)
    assert msg in str(e.value)


def test_ids_are_unique_and_urls_too(store):
    st, _ = store
    a = st.create({}, {"label": "Lab", "url": "https://a.example.com"})
    b = st.create({}, {"label": "Lab", "url": "https://b.example.com"})
    assert (a["id"], b["id"]) == ("lab", "lab-2")
    with pytest.raises(srv.ServerError) as e:
        st.create({}, {"label": "Other", "url": "https://A.example.com/"})
    assert e.value.status == 409


def test_ca_is_kept_beside_with_a_relative_path_that_survives_a_move(store, tmp_path):
    st, box = store
    st.create({}, {"label": "Lab", "url": "https://a.example.com", "ca": PEM})
    raw = (box["state"] / "rancher.d" / "lab.yaml").read_text()
    assert "ca_file: rancher.d/lab.ca.pem" in raw and str(tmp_path) not in raw
    ca = box["state"] / "rancher.d" / "lab.ca.pem"
    assert stat.S_IMODE(ca.stat().st_mode) == 0o600
    # la console déplacée : copier l'état suffit
    shutil.move(str(box["state"]), str(tmp_path / "moved"))
    box["state"] = tmp_path / "moved"
    x = st.get({}, "lab")
    assert x["ca_file"] == str(tmp_path / "moved" / "rancher.d" / "lab.ca.pem")
    assert Path(x["ca_file"]).read_text().startswith("-----BEGIN CERTIFICATE-----")
    # retirer l'autorité supprime le fichier
    st.update({}, "lab", {"ca": ""})
    assert st.get({}, "lab")["ca_file"] is None and not (tmp_path / "moved" / "rancher.d" / "lab.ca.pem").exists()


def test_sso_secret_is_stored_0600_and_never_public(store):
    st, box = store
    st.create({}, {"label": "Lab", "url": "https://a.example.com"})
    x = st.set_sso({}, "lab", "client-xyz", "very-secret", "https://c/auth/rancher/callback", "harvester-ops-c")
    sec = box["state"] / "rancher.d" / "lab.oidc-secret"
    assert stat.S_IMODE(sec.stat().st_mode) == 0o600 and sec.read_text().strip() == "very-secret"
    assert "secret_file: rancher.d/lab.oidc-secret" in (box["state"] / "rancher.d" / "lab.yaml").read_text()
    pub = srv.public(x)
    assert pub["sso"] == {"enabled": True, "client_id": "client-xyz", "registered_at": pub["sso"]["registered_at"]}
    assert "very-secret" not in repr(pub) and "secret_file" not in repr(pub) and "ca_file" not in repr(pub)
    s = srv.settings_of(x)
    assert s["client_secret"] == "very-secret" and s["sso"] and s["issuer"] == "https://a.example.com/oidc"
    st.clear_sso({}, "lab")
    assert not sec.exists() and srv.public(st.get({}, "lab"))["sso"]["enabled"] is False


def test_config_server_is_read_only_and_wins_on_the_same_url(store, tmp_path):
    st, _ = store
    st.create({}, {"label": "Mine", "url": "https://rancher.example.com"})
    secret = tmp_path / "sec"
    secret.write_text("s")
    cfg = {"rancher": {"url": "https://rancher.example.com/", "client_id": "c1",
                       "client_secret_file": str(secret), "label": "Corp"}}
    servers = st.servers(cfg)
    assert [(x["id"], x["origin"]) for x in servers] == [("rancher-example-com", "config")]
    assert servers[0]["direct_enabled"] is False and srv.sso_enabled(servers[0])
    every = st.servers(cfg, include_shadowed=True)
    assert [x["shadowed"] for x in every] == [False, True]
    with pytest.raises(srv.ServerError) as e:
        st.delete(cfg, "rancher-example-com")
    assert e.value.status == 409 and "read-only" in str(e.value)
    with pytest.raises(srv.ServerError):
        st.update(cfg, "rancher-example-com", {"label": "x"})
    # le réglage masqué reste supprimable
    st.delete(cfg, "mine")
    assert len(st.servers(cfg, include_shadowed=True)) == 1


def test_changes_are_seen_without_restart(store):
    st, box = store
    assert st.servers({}) == []
    st.create({}, {"label": "Lab", "url": "https://a.example.com"})
    assert [x["id"] for x in st.servers({})] == ["lab"]
    # un fichier changé à côté (copie, autre processus) est relu
    f = box["state"] / "rancher.d" / "lab.yaml"
    f.write_text(f.read_text().replace("label: Lab", "label: Lab B"))
    os.utime(f, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    assert st.get({}, "lab")["label"] == "Lab B"
    f.unlink()
    assert st.servers({}) == []


def test_providers_and_login_paths():
    out = srv.providers_of({"data": [{"id": "local", "type": "localProvider"},
                                     {"id": "openldap", "type": "openLdapProvider"},
                                     {"id": "keycloakoidc", "type": "keyCloakOIDCProvider"}]})
    assert [(p["id"], p["password"], p["enabled"]) for p in out] == [
        ("local", True, True), ("openldap", True, True), ("keycloakoidc", False, True)]
    assert srv.login_path("local") == "/v3-public/localProviders/local?action=login"
    assert srv.login_path({"id": "activedirectory", "type": "activeDirectoryProvider"}) == \
        "/v3-public/activeDirectoryProviders/activedirectory?action=login"
    with pytest.raises(srv.ServerError):
        srv.login_path("../x")


@pytest.mark.parametrize("version, constraint, ok", [
    ("v2.14.1", ">= 2.14.0-0 < 2.15.0-0", True),
    ("v2.13.4", ">= 2.14.0-0 < 2.15.0-0", False),
    ("v2.15.0", ">= 2.14.0-0 < 2.15.0-0", False),
    ("v1.33.7+rke2r1", ">= 1.23.0-0 < 1.36.0-0", True),
    ("v1.36.3+rke2r1", ">= 1.23.0-0 < 1.36.0-0", False),
    ("v2.14.1", "", True),
    ("v1.20.0", "< 1.21.0 || >= 1.30.0", True),
    (None, ">= 1.0.0", None),
])
def test_chart_version_constraints(version, constraint, ok):
    assert srv.satisfies(version, constraint) is ok
