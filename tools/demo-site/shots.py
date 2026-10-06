"""Captures du site, prises dans la démo, dans chaque langue (v1.86.0).

Prises à la construction (build.py), jamais gardées dans le dépôt : elles
suivent l'interface de la version construite et ne montrent que les données
anonymisées de la démo. JPEG : une capture d'écran de 1600 px pèse quatre
fois moins qu'en PNG pour une différence invisible sur le site.
"""
import functools
import http.server
import threading
from pathlib import Path

LANGS = ["en", "fr", "es", "it", "de"]
VIEW = {"width": 1600, "height": 950}

# nom -> (onglet, préparation dans la page avant la capture)
SHOTS = {
    "hero": ("allvms", None),
    "allvms": ("allvms", None),
    "vms": ("namespaces", None),
    "overview": ("overview", "() => document.querySelector('[data-overview-tab=cluster]')?.click()"),
    "storage": ("storage", None),
    "network": ("network", "() => document.querySelector('[data-section-tab=underlay]')?.click()"),
    "lanes": ("forkliftglobal", "() => document.querySelector('[data-fkg=view][data-mode=lanes]')?.click()"),
    "shutdown": ("shutdown", None),
    "automation": ("automation", None),
    "activity": ("activity", None),
    "security": ("security", None),
    "update": ("overview", "() => { window.Versions && Versions.open(); "
                           "setTimeout(() => document.querySelector('#versions-modal [data-vpane=update]')?.click(), 400); }"),
}


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(root):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def capture(out, langs=LANGS, names=None):
    """Prend les captures de out/demo dans out/assets/shots/<lang>/<nom>.jpg."""
    from playwright.sync_api import sync_playwright
    srv = serve(out)
    base = f"http://127.0.0.1:{srv.server_address[1]}/demo/"
    errors = []
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            for lang in langs:
                dest = out / "assets" / "shots" / lang
                dest.mkdir(parents=True, exist_ok=True)
                for name, (tab, prep) in SHOTS.items():
                    if names and name not in names:
                        continue
                    ctx = b.new_context(viewport=VIEW)
                    mode = "dark" if name == "hero" else "light"     # l'en-tête du site est sombre
                    ctx.add_init_script(
                        f"localStorage.setItem('harvester_ops_language','{lang}');"
                        f"localStorage.setItem('harvester_ops_mode','{mode}');"
                        "localStorage.setItem('harvester_ops_theme','suse');"
                        "localStorage.setItem('harvester_ops_fkg_view','table');")
                    pg = ctx.new_page()
                    pg.on("pageerror", lambda e: errors.append(str(e)[:160]))
                    pg.goto(f"{base}?lang={lang}#{tab}", wait_until="domcontentloaded")
                    pg.wait_for_function("window.App && App.getCurrentCluster && App.getCurrentCluster()", timeout=20000)
                    pg.mouse.move(1500, 900)
                    pg.wait_for_timeout(2200)
                    if prep:
                        pg.evaluate(prep)
                        pg.mouse.move(1500, 900)
                        pg.wait_for_timeout(1800)
                    # le bandeau de démo n'a rien à faire dans une capture du site
                    pg.evaluate("() => document.getElementById('demo-banner')?.remove()")
                    pg.screenshot(path=str(dest / f"{name}.jpg"), type="jpeg", quality=86)
                    ctx.close()
            b.close()
    finally:
        srv.shutdown()
    return errors
