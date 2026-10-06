#!/usr/bin/env python3
"""Construit le site de présentation et sa démo vivante (v1.86.0).

    python3 tools/demo-site/build.py --out dist/site

Sortie, en fichiers statiques (hébergeables n'importe où, sous un chemin
quelconque : tout est relatif) :
  index.html               redirige vers la langue du navigateur
  <lang>/index.html        le site, une page par langue (en fr es it de)
  assets/                  styles, images, captures
  demo/                    la vraie interface de la console, sur la fausse API

La démo : le modèle de page de la console rendu avec des clusters fictifs,
ses ressources recopiées en chemins relatifs, puis la fausse API
(site/demo/demo-api.js) et les données anonymisées (site/demo/data.json)
chargées AVANT l'interface. Rien n'est appelé sur le réseau.
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

import jinja2

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"
sys.path.insert(0, str(Path(__file__).resolve().parent))


def version():
    return (ROOT / "VERSION").read_text().strip()


def build_demo(out):
    demo = out / "demo"
    if demo.exists():
        shutil.rmtree(demo)
    static = demo / "static"
    shutil.copytree(ROOT / "web" / "static", static)
    # chemins absolus -> relatifs : le site peut vivre sous /harvester-ops/
    for f in static.rglob("*.js"):
        t = f.read_text(errors="replace")
        if "'/static/" in t:
            # import() se résout depuis le module (static/js/), une balise
            # <script> créée à la volée depuis la page (demo/)
            t = re.sub(r"import\(\s*'/static/", "import('../", t)
            t = re.sub(r"from\s+'/static/", "from '../", t)
            f.write_text(t.replace("'/static/", "'static/"))
    for f in static.rglob("*.css"):
        t = f.read_text(errors="replace")
        if "/static/" in t:
            f.write_text(t.replace("/static/", "../"))
    data = json.loads((SITE / "demo" / "data.json").read_text())
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "web" / "templates")), autoescape=True)
    html = env.get_template("index.html").render(
        version=version(), clusters=[{"name": c} for c in data["meta"]["clusters"]])
    html = html.replace('"/static/', '"static/')
    head = ('<script src="demo-data.js"></script>\n<script src="demo-api.js"></script>\n'
            '<link rel="stylesheet" href="demo.css">\n')
    html = html.replace("<head>\n", "<head>\n" + head, 1)
    html = html.replace("</body>", '<script src="demo-banner.js"></script>\n</body>', 1)
    (demo / "index.html").write_text(html)
    (demo / "demo-data.js").write_text("window.__HARVOPS_DEMO__ = " + json.dumps(data, separators=(",", ":")) + ";\n")
    for name in ("demo-api.js", "demo-banner.js", "demo.css"):
        shutil.copy(SITE / "demo" / name, demo / name)
    return demo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dist" / "site"))
    ap.add_argument("--demo-only", action="store_true")
    ap.add_argument("--no-shots", action="store_true", help="sans captures (pas de navigateur)")
    ap.add_argument("--shot-langs", default="en,fr,es,it,de")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    build_demo(out)
    if not a.demo_only and not a.no_shots:
        import shots
        errs = shots.capture(out, langs=a.shot_langs.split(","))
        if errs:
            print("erreurs de page pendant les captures :", errs[:5], file=sys.stderr)
            sys.exit(1)
    if not a.demo_only:
        import pages
        pages.build(out, version())
    print(f"site -> {out}")


if __name__ == "__main__":
    main()
