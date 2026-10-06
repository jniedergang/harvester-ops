"""v1.86.0 : site de présentation en cinq langues et démo vivante.

Contenus de même structure dans les cinq langues, sans typographie
interdite ; données de la démo sans aucune trace des clusters réels (le
site part en public) ; versions techniques lues dans le livrable ;
construction des pages et de la démo sans navigateur."""

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools" / "demo-site"
sys.path.insert(0, str(TOOLS))
import build  # noqa: E402
import pages  # noqa: E402
import sanitize  # noqa: E402
import techinfo  # noqa: E402

LANGS = ["en", "fr", "es", "it", "de"]
FROZEN = ("icon", "id", "shot", "demo")


def load(lang):
    return json.loads((ROOT / "site" / "content" / f"{lang}.json").read_text())


def shape(o, path=""):
    if isinstance(o, dict):
        return {k: shape(v, f"{path}.{k}") for k, v in o.items()}
    if isinstance(o, list):
        return [shape(v, path) for v in o]
    return type(o).__name__


def frozen(o, out, key=""):
    if isinstance(o, dict):
        for k, v in o.items():
            frozen(v, out, k)
    elif isinstance(o, list):
        for v in o:
            frozen(v, out, key)
    elif key in FROZEN:
        out.append((key, o))
    return out


def strings(o):
    if isinstance(o, dict):
        for v in o.values():
            yield from strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from strings(v)
    elif isinstance(o, str):
        yield o


def test_every_language_has_the_same_structure_and_identifiers():
    en = load("en")
    for lang in LANGS[1:]:
        c = load(lang)
        assert shape(c) == shape(en), lang
        # identifiants des familles et des piliers (nav.demo, lui, est un libellé)
        assert frozen([c["families"], c["pillars"]], []) == frozen([en["families"], en["pillars"]], []), lang


def test_no_typographic_trace_in_the_site():
    files = list((ROOT / "site" / "content").glob("*.json")) + list((ROOT / "site").rglob("*.css")) \
        + [ROOT / "site" / "demo" / n for n in ("demo-banner.js",)]
    for f in files:
        t = f.read_text()
        assert "—" not in t and "→" not in t, f.name


def test_every_family_points_at_a_real_tab_and_a_real_shot():
    import shots
    html = (ROOT / "web" / "templates" / "index.html").read_text()
    c = load("en")
    for f in c["families"]:
        assert f'id="tab-{f["demo"]}"' in html, f["demo"]
        assert f["shot"] in shots.SHOTS, f["shot"]
        assert f["shot"] in c["shots_alt"]
        assert f["id"] in pages.FAMILY_ICON


def test_technical_roles_are_translated_for_every_component():
    roles = {r["role"] for _, rows in techinfo.collect()["groups"] for r in rows}
    for lang in LANGS[1:]:
        t = json.loads((ROOT / "site" / "content" / f"tech-{lang}.json").read_text())
        assert roles <= set(t), (lang, roles - set(t))
        assert not any("—" in v or "→" in v for v in t.values())


def test_versions_are_read_from_the_release():
    info = techinfo.collect()
    assert info["version"] == (ROOT / "VERSION").read_text().strip()
    groups = dict((g, {r["name"]: r["version"] for r in rows}) for g, rows in info["groups"])
    lock = (ROOT / "web" / "requirements-lock.txt").read_text()
    assert f'flask=={groups["python"]["flask"]}' in lock
    cf = (ROOT / "container" / "Containerfile").read_text()
    assert f'ARG TOFU_VERSION={groups["tools"]["OpenTofu"]}' in cf
    assert f'ARG KUBECTL_VERSION={groups["tools"]["kubectl"]}' in cf
    assert groups["frontend"]["xterm.js"] == "5.5.0"


# -- données de la démo ---------------------------------------------------------

def test_the_published_demo_data_carries_no_trace_of_the_real_clusters():
    raw = (ROOT / "site" / "demo" / "data.json").read_text()
    for p in sanitize.FORBIDDEN:
        assert not re.search(p, raw), p
    data = json.loads(raw)
    assert data["meta"]["clusters"][0] == "lyon-lab"
    assert "/api/vms-all" in data["responses"]


def test_sanitize_rewrites_names_addresses_and_secrets(tmp_path):
    rec = {
        "/api/vms/harv1": {"status": 200, "body": {"vms": [{"name": "rocky9-test", "ips": ["172.16.3.41"],
                                                            "node": "harv1.home.lo"}]}},
        "/api/yaml/harvlab/vm/web": {"status": 200, "body": {"yaml": "userData: |\n  #cloud-config\n  password: s3cret\n"
                                                                     "  chpasswd: { expire: False }\n  hostname: web\n"}},
        "/api/sshkeys/harvlab": {"status": 200, "body": {"key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIRealKeyMaterial ju@node1-xl170r",
                                                         "cloud": {"userData": "#cloud-config\nusers: [ju]"},
                                                         "mac": "08:37:e7:65:04:1e", "path": "/home/ju/.kube/x"}},
    }
    src, dst = tmp_path / "rec.json", tmp_path / "data.json"
    src.write_text(json.dumps(rec))
    sanitize.main(str(src), str(dst))
    out = json.loads(dst.read_text())
    r = out["responses"]
    assert "/api/vms/paris-prod" in r and "/api/yaml/lyon-lab/vm/web" in r
    vm = r["/api/vms/paris-prod"]["body"]["vms"][0]
    assert vm["ips"] == ["10.20.3.41"] and vm["node"] == "paris-n1"
    y = r["/api/yaml/lyon-lab/vm/web"]["body"]["yaml"]
    assert "s3cret" not in y and "chpasswd" not in y and "hostname: web" in y
    k = r["/api/sshkeys/lyon-lab"]["body"]
    assert "RealKeyMaterial" not in k["key"] and k["key"].endswith("operator@admin-host")
    assert k["cloud"]["userData"] == "#cloud-config\npackage_update: true\n"
    assert k["mac"].startswith("52:54:00:") and k["path"] == "/home/operator/.kube/x"


def test_sanitize_refuses_to_write_when_a_trace_is_left(tmp_path):
    src, dst = tmp_path / "rec.json", tmp_path / "data.json"
    # un domaine du labo dans une forme qu'aucune règle ne réécrit
    src.write_text(json.dumps({"/api/x": {"status": 200, "body": {"t": "see HOME.LO/wiki"}}}).replace("HOME.LO", "home" + ".lo"))
    sanitize.DOMAINS_SAVED = sanitize.DOMAINS
    try:
        sanitize.DOMAINS = []
        with pytest.raises(SystemExit):
            sanitize.main(str(src), str(dst))
    finally:
        sanitize.DOMAINS = sanitize.DOMAINS_SAVED
    assert not dst.exists()


# -- construction ----------------------------------------------------------------

def test_the_site_builds_in_five_languages_without_a_browser(tmp_path):
    build.build_demo(tmp_path)
    pages.build(tmp_path, "9.9.9")
    for lang in LANGS:
        h = (tmp_path / lang / "index.html").read_text()
        assert f'<html lang="{lang}">' in h
        assert all(f'hreflang="{lg}"' in h for lg in LANGS)
        assert "—" not in h and "→" not in h
        assert f"../demo/?lang={lang}" in h and "v9.9.9" in h
        assert load(lang)["hero"]["title"].replace("'", "&#x27;") in h
    root = (tmp_path / "index.html").read_text()
    assert "location.replace" in root
    demo = (tmp_path / "demo" / "index.html").read_text()
    head = demo.split("</head>")[0]
    assert head.index("demo-api.js") < head.index("static/css/style.css")
    assert '"/static/' not in demo
    notes = (tmp_path / "demo" / "static" / "js" / "notes.js").read_text()
    assert "from '../vendor/tiptap/" in notes
    assert "'/static/" not in (tmp_path / "demo" / "static" / "js" / "vm-actions.js").read_text()
    assert (tmp_path / ".nojekyll").exists()
