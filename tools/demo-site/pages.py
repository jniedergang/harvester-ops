"""Pages du site de présentation, une par langue (v1.86.0).

Le texte vient de site/content/<lang>.json (même structure dans les cinq
langues, contrôlée par les tests), les versions techniques de techinfo.py,
les icônes du jeu Lucide de la console (web/static/js/icons.js), les
captures de <sortie>/assets/shots/<lang>/ (prises dans la démo par shots.py).
Aucune dépendance : du HTML et du CSS écrits ici, un peu de JavaScript pour
le menu sur téléphone.
"""
import html
import json
import re
import shutil
from pathlib import Path

import techinfo

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"
LANGS = ["en", "fr", "es", "it", "de"]
GITHUB = "https://github.com/jniedergang/harvester-ops"
FAMILY_ICON = {"multi": "cloud", "vms": "vm", "views": "metrics", "storage": "storage", "network": "network",
               "vmware": "migrate", "lifecycle": "power", "automation": "robot", "activity": "activity",
               "security": "shield", "delivery": "bundle"}
PILLAR_ICON = {"layers": "cloud", "zap": "robot", "activity": "activity"}
e = html.escape


def icons():
    t = (ROOT / "web" / "static" / "js" / "icons.js").read_text()
    return {m.group(1): m.group(2) for m in re.finditer(r"^\s+'?([A-Za-z]+)'?\s*:\s*'(<[^']*)'", t, re.M)}


ICONS = None


def svg(name, size=24, cls=""):
    global ICONS
    if ICONS is None:
        ICONS = icons()
    body = ICONS.get(name, ICONS.get("cloud", ""))
    return (f'<svg class="ico {cls}" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" '
            f'aria-hidden="true">{body}</svg>')


def content(lang):
    return json.loads((SITE / "content" / f"{lang}.json").read_text())


def tech_roles(lang):
    p = SITE / "content" / f"tech-{lang}.json"
    return json.loads(p.read_text()) if p.exists() else {}


OUT = None          # répertoire de sortie, où shots.py a posé les captures


def shot(lang, name):
    """Chemin de la capture dans la langue de la page, sinon en anglais."""
    for lg in (lang, "en"):
        if OUT and (OUT / "assets" / "shots" / lg / f"{name}.jpg").exists():
            return f"../assets/shots/{lg}/{name}.jpg"
    return ""


def frame(src, alt, wide=False):
    if not src:
        return ""
    return (f'<figure class="frame{" wide" if wide else ""}"><div class="frame-bar"><i></i><i></i><i></i></div>'
            f'<img src="{e(src)}" alt="{e(alt)}" loading="lazy" width="1600" height="950"></figure>')


def page(lang, ver, tech):
    c = content(lang)
    roles = tech_roles(lang)
    cur = ' aria-current="page"'
    langs = "".join(
        f'<a href="../{lg}/" hreflang="{lg}" lang="{lg}"{cur if lg == lang else ""}>{lg.upper()}</a>'
        for lg in LANGS)
    pillars = "".join(
        f'<div class="pillar">{svg(PILLAR_ICON.get(p["icon"], "cloud"), 30)}<h3>{e(p["title"])}</h3>'
        f'<p>{e(p["text"])}</p></div>' for p in c["pillars"])
    fams = []
    for i, f in enumerate(c["families"]):
        items = "".join(f"<li>{e(x)}</li>" for x in f["items"])
        alt = c["shots_alt"].get(f["shot"], f["title"])
        fams.append(
            f'<section class="family{" flip" if i % 2 else ""}" id="{e(f["id"])}">'
            f'<div class="family-text"><div class="family-icon">{svg(FAMILY_ICON.get(f["id"], "cloud"), 26)}</div>'
            f'<h3>{e(f["title"])}</h3><p class="lead">{e(f["lead"])}</p><ul class="ticks">{items}</ul>'
            f'<a class="link-demo" href="../demo/?lang={lang}#{e(f["demo"])}">'
            f'{e(c["try"])} {svg("play", 16)}</a></div>'
            f'<div class="family-shot">{frame(shot(lang, f["shot"]), alt)}</div></section>')
    arch = "".join(
        f'<div class="arch-box"><h4>{e(a["title"])}</h4><p>{e(a["text"])}</p></div>'
        + ('<div class="arch-arrow" aria-hidden="true"></div>' if k < len(c["tech"]["arch"]) - 1 else "")
        for k, a in enumerate(c["tech"]["arch"]))
    principles = "".join(f"<li>{e(x)}</li>" for x in c["tech"]["principles"])
    groups = []
    for gid, rows in tech["groups"]:
        if not rows:
            continue
        trs = "".join(
            f'<tr><td>{e(r["name"])}</td><td><code>{e(r["version"])}</code></td>'
            f'<td>{e(roles.get(r["role"], r["role"]))}</td></tr>' for r in rows)
        groups.append(f'<div class="stack-group"><h4>{e(c["tech"]["groups"][gid])}</h4>'
                      f'<table><thead><tr><th>{e(c["tech"]["col_component"])}</th><th>{e(c["tech"]["col_version"])}</th>'
                      f'<th>{e(c["tech"]["col_role"])}</th></tr></thead><tbody>{trs}</tbody></table></div>')
    steps = "".join(f"<li><span>{k + 1}</span>{e(s)}</li>" for k, s in enumerate(c["install"]["steps"]))
    hero_shot = frame(shot(lang, "hero"), c["shots_alt"]["allvms"], wide=True)
    gh_parity = f"{GITHUB}/blob/master/docs/{'fr/parite-harvester.md' if lang == 'fr' else 'en/harvester-parity.md'}"
    gh_videos = f"{GITHUB}/blob/master/{'README.fr.md' if lang == 'fr' else 'README.md'}#{'à-voir' if lang == 'fr' else 'watch-it-work'}"
    return f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(c["meta"]["title"])}</title>
<meta name="description" content="{e(c["meta"]["description"])}">
<meta property="og:title" content="{e(c["meta"]["title"])}">
<meta property="og:description" content="{e(c["meta"]["description"])}">
<meta property="og:type" content="website">
{"".join(f'<link rel="alternate" hreflang="{lg}" href="../{lg}/">' for lg in LANGS)}
<link rel="icon" type="image/svg+xml" href="../assets/favicon.svg">
<link rel="stylesheet" href="../assets/site.css">
</head>
<body>
<header class="top">
  <a class="brand" href="./">{svg("cloud", 26)}<span>harvester-ops</span></a>
  <button class="menu-btn" type="button" aria-expanded="false" aria-controls="topnav" aria-label="Menu">{svg("more", 22)}</button>
  <nav id="topnav">
    <a href="#features">{e(c["nav"]["features"])}</a>
    <a href="../demo/?lang={lang}">{e(c["nav"]["demo"])}</a>
    <a href="#tech">{e(c["nav"]["tech"])}</a>
    <a href="#install">{e(c["nav"]["install"])}</a>
    <a href="{GITHUB}">{e(c["nav"]["github"])}</a>
    <span class="langs">{langs}</span>
  </nav>
</header>
<main>
<section class="hero">
  <div class="hero-text">
    <p class="kicker">{e(c["hero"]["kicker"])}</p>
    <h1>{e(c["hero"]["title"])}</h1>
    <p class="lead">{e(c["hero"]["lead"])}</p>
    <div class="ctas">
      <a class="btn primary" href="../demo/?lang={lang}">{svg("play", 18)} {e(c["hero"]["cta_demo"])}</a>
      <a class="btn" href="{GITHUB}">{svg("code", 18)} {e(c["hero"]["cta_github"])}</a>
      <a class="btn ghost" href="#install">{svg("download", 18)} {e(c["hero"]["cta_install"])}</a>
    </div>
    <p class="note">{e(c["hero"]["note"])} <span class="ver">v{e(ver)}</span></p>
  </div>
  <div class="hero-shot">{hero_shot}</div>
</section>
<section class="pillars">{pillars}</section>
<section class="demo-band">
  <div><h2>{e(c["demo_band"]["title"])}</h2><p>{e(c["demo_band"]["text"])}</p></div>
  <a class="btn primary big" href="../demo/?lang={lang}">{svg("play", 20)} {e(c["demo_band"]["cta"])}</a>
</section>
<section class="features" id="features">
  <h2>{e(c["features_title"])}</h2>
  <p class="section-lead">{e(c["features_lead"])}</p>
  <nav class="family-index">{"".join(f'<a href="#{e(f["id"])}">{svg(FAMILY_ICON.get(f["id"], "cloud"), 18)}<span>{e(f["title"])}</span></a>' for f in c["families"])}</nav>
  {"".join(fams)}
</section>
<section class="parity">
  <h2>{e(c["parity"]["title"])}</h2><p>{e(c["parity"]["text"])}</p>
  <a class="btn" href="{gh_parity}">{e(c["parity"]["link"])}</a>
</section>
<section class="tech" id="tech">
  <h2>{e(c["tech"]["title"])}</h2>
  <p class="section-lead">{e(c["tech"]["lead"])}</p>
  <h3>{e(c["tech"]["arch_title"])}</h3>
  <div class="arch">{arch}</div>
  <h3>{e(c["tech"]["principles_title"])}</h3>
  <ul class="ticks principles">{principles}</ul>
  <h3>{e(c["tech"]["stack_title"])}</h3>
  <p class="section-lead small">{e(c["tech"]["stack_lead"])} (v{e(ver)})</p>
  <div class="stack">{"".join(groups)}</div>
</section>
<section class="install" id="install">
  <h2>{e(c["install"]["title"])}</h2>
  <p class="section-lead">{e(c["install"]["lead"])}</p>
  <ol class="steps">{steps}</ol>
  <pre class="code"><code>V={e(ver)}
curl -LO {GITHUB}/releases/download/v$V/harvester-ops-$V.tar.gz
curl -LO {GITHUB}/releases/download/v$V/harvester-ops-$V.tar.gz.sha256
curl -LO {GITHUB}/releases/download/v$V/harvester-ops-$V.tar.gz.sig
sha256sum -c harvester-ops-$V.tar.gz.sha256
tar xzf harvester-ops-$V.tar.gz &amp;&amp; cd harvester-ops-$V
sudo ./install.sh</code></pre>
  <p class="note">{e(c["install"]["need"])}</p>
</section>
<section class="videos">
  <h2>{e(c["videos"]["title"])}</h2><p>{e(c["videos"]["text"])}</p>
  <a class="btn" href="{gh_videos}">{svg("play", 18)} {e(c["videos"]["link"])}</a>
</section>
</main>
<footer>
  <p>{e(c["footer"]["license"])} {e(c["footer"]["version"])} v{e(ver)} · <a href="{GITHUB}">GitHub</a></p>
  <p class="small">{e(c["footer"]["made"])}</p>
</footer>
<script src="../assets/site.js"></script>
</body>
</html>
"""


def build(out, ver):
    global OUT
    OUT = out
    tech = techinfo.collect()
    assets = out / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for f in (SITE / "assets").iterdir():
        shutil.copy(f, assets / f.name)
    shutil.copy(ROOT / "web" / "static" / "favicon.svg", assets / "favicon.svg")
    for lang in LANGS:
        d = out / lang
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(page(lang, ver, tech))
    (out / "index.html").write_text(
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>harvester-ops</title>"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<link rel=\"icon\" type=\"image/svg+xml\" href=\"assets/favicon.svg\">"
        "<script>var l=(navigator.language||'en').slice(0,2);"
        f"if({json.dumps(LANGS)}.indexOf(l)<0)l='en';location.replace(l+'/');</script>"
        "</head><body><noscript>"
        + " ".join(f'<a href="{lg}/">{lg.upper()}</a>' for lg in LANGS)
        + "</noscript></body></html>\n")
    (out / ".nojekyll").write_text("")
