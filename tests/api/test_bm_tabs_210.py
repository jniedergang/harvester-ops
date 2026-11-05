"""2.1.0 : Bare-metal en onglets, côté serveur et adresses.

* la dernière découverte des BMC est gardée par la console : cache de
  lecture écrit pendant la lecture Redfish (pas d'action suivie), relatif au
  répertoire d'état, 0600, remplacement atomique, JAMAIS d'identifiant ;
* adresses de BMC refusées avant toute lecture (une par ligne) ;
* la dernière découverte ne se lit qu'en administrateur ;
* `join-url` dit la version du cluster et les ISO du magasin de cette
  version (bandeau « aucun ISO de la version X » de Machines) ;
* adresses `#automation/pxe/<onglet>` (routes.js) ;
* mutations volontaires : chaque garde est prouvée par une copie cassée.
"""

import json
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
WEB = ROOT / "web"
sys.path.insert(0, str(WEB))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp  # noqa: E402

USER = "bmcadmin-210"
PASSWORD = "BMCSECRET-210x"


def node_of(host, **extra):
    """Machine comme la rend _bmc_discover_one (champs réels de la 1.17+)."""
    n = {"ok": True, "host": host, "system_path": "/redfish/v1/Systems/1/",
         "manager_path": "/redfish/v1/Managers/1/", "virtualmedia_path": "/redfish/v1/Managers/1/VirtualMedia/2/",
         "virtualmedia_inserted": False, "boot_targets": ["Cd", "Hdd"], "boot_override": "None",
         "uefi_targets": ["HD.Emb.1.1", "HD.Emb.1.2"], "post_state": "PowerOff", "manufacturer": "HPE",
         "model": "ProLiant XL170r Gen9", "serial": "USE0000000", "asset": "", "bios_version": "U14",
         "power_state": "Off", "cpu_count": 1, "memory_gib": 128, "indicator_led": "Off", "uuid": "u-1",
         "nics": [{"name": "System Ethernet Interface", "mac": "94:18:82:00:00:01", "status": "Enabled",
                   "speed_mbps": 1000}]}
    n.update(extra)
    return n


def assert_no_secret(path, *secrets):
    raw = Path(path).read_text()
    leaked = [s for s in secrets if s in raw]
    assert leaked == [], f"identifiant écrit dans {Path(path).name}"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    wapp.app.config["TESTING"] = True
    state = tmp_path / "state"
    monkeypatch.setattr(wapp, "_state_dir", lambda: state)
    monkeypatch.setattr(wapp, "current_role", lambda: "admin")
    seen = []

    def fake_one(host, user, pwd):
        seen.append((host, user, pwd))
        if host.startswith("10.0.0.9"):
            return {"ok": False, "host": host, "error": "Redfish root unreachable"}
        return node_of(host)
    monkeypatch.setattr(wapp, "_bmc_discover_one", fake_one)
    return {"state": state, "seen": seen, "client": wapp.app.test_client()}


def discover(c, hosts, user=USER, password=PASSWORD):
    return c.post("/api/bmc/discover", json={"hosts": hosts, "user": user, "password": password})


# --- mémoire de la découverte ---------------------------------------------------

def test_discovery_is_remembered_without_credentials(env):
    c = env["client"]
    r = discover(c, ["192.0.2.31", "10.0.0.9"])
    assert r.status_code == 200
    d = r.get_json()
    assert d["remembered"] is True and d["count"] == 2 and d["at"]
    f = env["state"] / wapp.BM_LAST_DISCOVERY
    assert f.is_file()
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert_no_secret(f, PASSWORD, USER)
    got = c.get("/api/bmc/last-discovery").get_json()["discovery"]
    assert got["hosts"] == ["192.0.2.31", "10.0.0.9"]
    assert got["at"] == d["at"]
    assert [n["host"] for n in got["nodes"]] == ["192.0.2.31", "10.0.0.9"]
    assert got["nodes"][0]["model"] == "ProLiant XL170r Gen9"
    assert got["nodes"][0]["nics"][0]["mac"] == "94:18:82:00:00:01"
    assert got["nodes"][1] == {"ok": False, "host": "10.0.0.9", "error": "Redfish root unreachable", "nics": []}
    # les identifiants sont arrivés à la lecture Redfish, et seulement là
    assert env["seen"][0] == ("192.0.2.31", USER, PASSWORD)


def test_a_new_discovery_replaces_the_previous_one(env):
    c = env["client"]
    discover(c, ["192.0.2.31"])
    discover(c, ["192.0.2.32"])
    got = c.get("/api/bmc/last-discovery").get_json()["discovery"]
    assert got["hosts"] == ["192.0.2.32"]
    assert [p.name for p in env["state"].iterdir()] == [wapp.BM_LAST_DISCOVERY]   # aucun fichier temporaire laissé


def test_the_file_moves_with_the_state_dir(env, tmp_path, monkeypatch):
    discover(env["client"], ["192.0.2.31"])
    moved = tmp_path / "elsewhere"
    env["state"].rename(moved)
    monkeypatch.setattr(wapp, "_state_dir", lambda: moved)
    got = env["client"].get("/api/bmc/last-discovery").get_json()["discovery"]
    assert got["hosts"] == ["192.0.2.31"]
    # aucun chemin absolu n'est rangé dans le fichier
    assert str(tmp_path) not in (moved / wapp.BM_LAST_DISCOVERY).read_text()


@pytest.mark.parametrize("content,why", [
    (None, "jamais de découverte"),
    ("{not json", "fichier illisible"),
    ('["a list"]', "pas un objet"),
    ('{"hosts": ["192.0.2.1"]}', "objet sans machines"),
    ('{"nodes": "x"}', "machines qui ne sont pas une liste"),
])
def test_absent_or_unreadable_memory_reads_as_null(env, content, why):
    if content is not None:
        env["state"].mkdir(parents=True, exist_ok=True)
        (env["state"] / wapp.BM_LAST_DISCOVERY).write_text(content)
    r = env["client"].get("/api/bmc/last-discovery")
    assert r.status_code == 200, why
    assert r.get_json() == {"discovery": None}, why


def test_only_known_fields_are_kept(env, monkeypatch):
    """Liste blanche : un champ inconnu (qui porterait un jour un secret) ne
    passe pas, même rendu par la lecture Redfish."""
    monkeypatch.setattr(wapp, "_bmc_discover_one",
                        lambda h, u, p: node_of(h, password=p, user=u, token="TOK-210",
                                                nics=[{"mac": "aa:bb", "secret": p}]))
    discover(env["client"], ["192.0.2.31"])
    f = env["state"] / wapp.BM_LAST_DISCOVERY
    assert_no_secret(f, PASSWORD, USER, "TOK-210")
    doc = json.loads(f.read_text())
    assert set(doc["nodes"][0]) <= set(wapp._BM_NODE_KEYS) | {"nics"}
    assert doc["nodes"][0]["nics"] == [{"name": None, "mac": "aa:bb", "status": None, "speed_mbps": None}]


def test_known_fields_are_bounded(env, monkeypatch):
    monkeypatch.setattr(wapp, "_bmc_discover_one",
                        lambda h, u, p: node_of(h, model="M" * 5000, boot_targets=["Cd"] * 500,
                                                nics=[{"mac": str(i)} for i in range(100)]))
    discover(env["client"], ["192.0.2.31"])
    n = env["client"].get("/api/bmc/last-discovery").get_json()["discovery"]["nodes"][0]
    assert len(n["model"]) == 512 and len(n["boot_targets"]) == 64 and len(n["nics"]) == 32


def test_a_storage_failure_keeps_the_discovery_answer(env, monkeypatch):
    def boom(doc):
        raise OSError("read-only")
    monkeypatch.setattr(wapp, "_bm_last_discovery_save", boom)
    r = discover(env["client"], ["192.0.2.31"])
    assert r.status_code == 200
    assert r.get_json()["remembered"] is False and r.get_json()["nodes"][0]["ok"] is True


def test_hosts_are_deduplicated_and_capped(env):
    hosts = ["192.0.2.31", "192.0.2.31"] + [f"198.51.100.{i}" for i in range(80)]
    d = discover(env["client"], hosts).get_json()
    assert d["count"] == wapp.BMC_DISCOVER_MAX
    assert [h for h, _u, _p in env["seen"]].count("192.0.2.31") == 1


@pytest.mark.parametrize("hosts,why", [
    ([], "liste vide"),
    ("192.0.2.31", "pas une liste"),
    ([""], "adresse vide"),
    ([12], "pas une chaîne"),
    (["192.0.2.31/redfish"], "chemin dans l'adresse"),
    (["user@192.0.2.31"], "identifiant dans l'adresse"),
    (["192.0.2.31 192.0.2.32"], "espace"),
    (["https://192.0.2.31"], "schéma"),
    (["192.0.2.31", "a" * 254], "adresse trop longue"),
])
def test_bad_bmc_addresses_are_refused_before_any_read(env, hosts, why):
    r = discover(env["client"], hosts)
    assert r.status_code == 400, why
    assert env["seen"] == [], why
    assert not (env["state"] / wapp.BM_LAST_DISCOVERY).exists(), why


@pytest.mark.parametrize("role,method,path,status", [
    ("operator", "get", "/api/bmc/last-discovery", 403),
    ("viewer", "get", "/api/bmc/last-discovery", 403),
    ("operator", "post", "/api/bmc/discover", 403),
    ("admin", "get", "/api/bmc/last-discovery", 200),
])
def test_last_discovery_is_for_administrators(env, monkeypatch, role, method, path, status):
    monkeypatch.setattr(wapp, "current_role", lambda: role)
    r = getattr(env["client"], method)(path, json={"hosts": ["192.0.2.31"]} if method == "post" else None)
    assert r.status_code == status


def test_routes_are_authenticated_and_rate_limited():
    from limits import parse_many
    src = (WEB / "app.py").read_text()
    head = src.split('@app.route("/api/bmc/last-discovery"', 1)[1].split("\ndef ", 1)[0]
    assert "@requires_auth" in head
    assert parse_many(re.search(r'@_rate_limit\("([^"]+)"\)', head).group(1))


def test_mutation_a_whitelisted_secret_is_seen(env, monkeypatch):
    """Mutation volontaire : une liste blanche qui laisse passer `password`
    est vue par le contrôle du fichier."""
    monkeypatch.setattr(wapp, "_BM_NODE_KEYS", wapp._BM_NODE_KEYS + ("password",))
    monkeypatch.setattr(wapp, "_bmc_discover_one", lambda h, u, p: node_of(h, password=p))
    discover(env["client"], ["192.0.2.31"])
    with pytest.raises(AssertionError):
        assert_no_secret(env["state"] / wapp.BM_LAST_DISCOVERY, PASSWORD)


def test_mutation_an_unchecked_address_is_seen(env, monkeypatch):
    """Mutation volontaire : sans le contrôle des adresses, la lecture part."""
    monkeypatch.setattr(wapp, "BMC_HOST_RE", re.compile(r".+"))
    discover(env["client"], ["192.0.2.31/redfish"])
    with pytest.raises(AssertionError):
        assert env["seen"] == []


# --- version du cluster à joindre et ISO du magasin -------------------------------

@pytest.fixture()
def iso_store(tmp_path, monkeypatch):
    d = tmp_path / "iso"
    d.mkdir()
    for name in ("harvester-v1.9.0-amd64.iso", "harvester-v1.8.2-amd64.iso", "custom.iso", "notes.txt"):
        (d / name).write_bytes(b"x")
    releases = {"custom.iso": {"harvester": "v1.9.0"}}
    monkeypatch.setattr(wapp, "_iso_dir", lambda: d)
    monkeypatch.setattr(wapp, "_iso_release_info", lambda p: releases.get(p.name))
    return d


@pytest.mark.parametrize("version,expected", [
    ("v1.9.0", ["custom.iso", "harvester-v1.9.0-amd64.iso"]),
    ("1.9.0", ["custom.iso", "harvester-v1.9.0-amd64.iso"]),
    ("v1.8.2", ["harvester-v1.8.2-amd64.iso"]),
    ("v1.10.0", []),
    ("", []),
])
def test_isos_of_a_version(iso_store, version, expected):
    assert wapp._bm_isos_of_version(version) == expected


def test_the_release_file_wins_over_the_name(iso_store, monkeypatch):
    """Un ISO renommé (nom en 1.8.2, contenu 1.9.0) compte pour sa vraie version."""
    monkeypatch.setattr(wapp, "_iso_release_info",
                        lambda p: {"harvester": "v1.9.0"} if p.name == "harvester-v1.8.2-amd64.iso" else None)
    assert wapp._bm_isos_of_version("v1.8.2") == []
    assert "harvester-v1.8.2-amd64.iso" in wapp._bm_isos_of_version("v1.9.0")


@pytest.mark.parametrize("reason,version,want_version,want_isos", [
    (None, "v1.9.0", "v1.9.0", ["custom.iso", "harvester-v1.9.0-amd64.iso"]),
    ("proxied", "v1.8.2", "v1.8.2", ["harvester-v1.8.2-amd64.iso"]),
    ("unreachable", "v1.9.0", None, []),
    (None, "", None, []),
])
def test_join_url_says_the_cluster_version_and_its_isos(iso_store, monkeypatch, reason, version,
                                                       want_version, want_isos):
    wapp.app.config["TESTING"] = True
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {"clusters": [{"name": "harv9"}]})
    monkeypatch.setattr(wapp, "_cluster_join_url",
                        lambda c: (None if reason else "https://192.0.2.100:443", reason))
    asked = []
    monkeypatch.setattr(wapp, "_cluster_harvester_version", lambda c: asked.append(c) or version)
    d = wapp.app.test_client().get("/api/clusters/harv9/join-url").get_json()
    assert d["harvester_version"] == want_version
    assert d["isos_of_version"] == want_isos
    # cluster éteint : aucune seconde lecture qui attendrait kubectl
    assert asked == ([] if reason == "unreachable" else ["harv9"])


# --- adresses #automation/pxe/<onglet> ----------------------------------------------

RUNNER = r"""
global.window = global;
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const m = { exports: {} };
new Function('module', 'globalThis', src)(m, globalThis);
const Routes = m.exports;
const inp = JSON.parse(require('fs').readFileSync(0, 'utf8'));
console.log(JSON.stringify(inp.map((h) => { const r = Routes.parse(h); return [r.name, r.tab || null, r.pane || null, r.name === 'unknown' ? null : Routes.format(r)]; })));
"""

ADDRESSES = [
    ("#automation/pxe", ["automation", "pxe", None, "#automation/pxe"]),
    ("#automation/pxe/machines", ["automation", "pxe", "machines", "#automation/pxe/machines"]),
    ("#automation/pxe/profiles", ["automation", "pxe", "profiles", "#automation/pxe/profiles"]),
    ("#automation/pxe/plans", ["automation", "pxe", "plans", "#automation/pxe/plans"]),
    ("#automation/pxe/Plans", ["unknown", None, None, None]),
    ("#automation/pxe/images", ["automation", "pxe", "images", "#automation/pxe/images"]),
    ("#automation/pxe/history", ["automation", "pxe", "history", "#automation/pxe/history"]),
    ("#automation/pxe/disks", ["unknown", None, None, None]),
    ("#automation/terraform/images", ["unknown", None, None, None]),
]


def run_routes(src_path, hashes):
    try:
        r = subprocess.run(["node", "-e", RUNNER, str(src_path)], input=json.dumps(hashes),
                           capture_output=True, text=True, cwd=str(ROOT), timeout=30)
    except FileNotFoundError:
        pytest.skip("node absent")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_bare_metal_tab_addresses():
    got = run_routes(WEB / "static/js/routes.js", [h for h, _ in ADDRESSES])
    assert got == [want for _, want in ADDRESSES]


def test_mutation_without_the_tab_list_the_addresses_are_unknown(tmp_path):
    """Mutation volontaire : routes.js sans la liste des onglets refuse les adresses."""
    src = (WEB / "static/js/routes.js").read_text()
    broken = src.replace("pxe: ['machines', 'profiles', 'plans', 'images', 'history']", "pxe: []")
    assert broken != src
    p = tmp_path / "routes.js"
    p.write_text(broken)
    got = run_routes(p, ["#automation/pxe/profiles"])
    assert got[0][0] == "unknown"
