#!/usr/bin/env python3
"""Refait les captures d'écran du README sur le banc harvlab.

Mêmes dimensions qu'avant (1600x900 à l'échelle 2, soit 3200x1800),
puis réduction à 256 couleurs : une interface se quantifie sans perte
visible, et le dépôt garde des images de 200 à 400 Ko.

    ./seed.py degrade        # pour montrer un volume en reconstruction
    ./screenshots.py         # écrit dans docs/assets/
"""

import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parents[1] / "docs" / "assets"
BASE = "http://127.0.0.1:8095"
CLUSTER = "harvlab"


def shrink(path):
    img = Image.open(path).convert("RGB")
    img.quantize(colors=256, method=Image.Quantize.MEDIANCUT).save(path, optimize=True)
    return path.stat().st_size // 1024


def main(only=None):
    shots = {}
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx = b.new_context(viewport={"width": 1600, "height": 900}, device_scale_factor=2)
        ctx.add_init_script("localStorage.setItem('harvester_ops_language','en');"
                            f"localStorage.setItem('harvester_ops_current_cluster','{CLUSTER}');")
        p = ctx.new_page()
        p.on("dialog", lambda d: d.dismiss())     # une capture ne confirme rien
        p.goto(BASE + "/", wait_until="domcontentloaded")
        p.wait_for_timeout(3000)

        def close_panels():
            p.evaluate("() => document.querySelectorAll('.floating-panel').forEach("
                       "el => window.FloatingPanels.close(el.id.replace(/^fp-/, '')))")
            p.wait_for_timeout(800)

        def snap(name):
            if only and name not in only:
                return
            p.mouse.move(1590, 890)
            p.evaluate("() => document.activeElement && document.activeElement.blur()")
            p.wait_for_timeout(600)
            path = ASSETS / f"{name}.png"
            p.screenshot(path=str(path))
            shots[name] = shrink(path)

        # Vue Cluster avec le pré-contrôle de maintenance ouvert (lecture seule).
        p.click('.tab[data-tab="overview"]')
        p.click('button[data-overview-tab="cluster"]')
        p.wait_for_selector(".cm-host", timeout=60000)
        p.wait_for_timeout(2000)
        busiest = p.evaluate("""async () => {
          const d = await (await fetch('/api/topology/harvlab')).json();
          const c = {}; (d.vms || []).forEach(v => v.node && (c[v.node] = (c[v.node] || 0) + 1));
          return Object.keys(c).sort((a, b) => c[b] - c[a])[0];
        }""")
        p.click(f'.cm-host-head[data-node="{busiest}"]')
        p.wait_for_timeout(800)
        p.click('[data-cm-act="node-maint-check"]')
        p.wait_for_selector(".cm-maint-box", timeout=30000)
        p.wait_for_timeout(1500)
        snap("cluster")

        p.click('button[data-overview-tab="storage"]')
        p.wait_for_timeout(3000)
        vol = p.locator(".sto-vol.health-degraded").first
        if vol.count() == 0:
            vol = p.locator(".sto-vol").first
        vol.click()
        p.wait_for_timeout(1500)
        snap("storage")

        p.click('button[data-overview-tab="fabric"]')
        p.wait_for_timeout(3500)
        snap("fabric")

        p.click('.tab[data-tab="shutdown"]')
        p.wait_for_timeout(2000)
        snap("shutdown")

        p.click('.tab[data-tab="namespaces"]')
        p.wait_for_selector("#ns-vms-table tbody tr", timeout=60000)
        p.wait_for_timeout(1500)
        snap("vms")

        p.evaluate("() => window.VMConsole.open('harvlab', 'default', 'web-01')")
        p.wait_for_selector(".vm-console-screen", timeout=60000)
        p.wait_for_timeout(6000)
        snap("console")
        close_panels()

        # Une action parlante : le dernier démarrage complet du cluster, avec
        # son journal (la première ligne venue était un simple événement).
        p.click('.tab[data-tab="activity"]')
        p.wait_for_selector("#activity-history tbody tr", timeout=60000)
        p.fill("#act-f-q", "startup")
        p.wait_for_timeout(1500)
        p.click("#activity-history tbody tr >> nth=0")
        p.wait_for_timeout(2000)
        snap("activity")
        close_panels()

        p.click('.tab-group-head[data-group="automation"]')
        p.wait_for_timeout(600)
        p.click('.tab[data-subtab="pxe"]')
        p.wait_for_timeout(3000)
        snap("baremetal")
        b.close()
    for name, kb in shots.items():
        print(f"{name}.png : {kb} Ko")


# v1.83.2 : captures qui ne demandent pas harvlab (banc à 3 nœuds), prises sur
# harvlab2 avec de vraies vagues de migration VMware : la vue en couloirs et
# l'onglet Mise à jour. `screenshots.py --base http://127.0.0.1:8125 lanes update`
EXTRA = {"lanes", "update"}


def extra(only, base):
    shots = {}
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx = b.new_context(viewport={"width": 1600, "height": 900}, device_scale_factor=2)
        ctx.add_init_script("localStorage.setItem('harvester_ops_language','en');"
                            "localStorage.setItem('harvester_ops_current_cluster','harvlab2');"
                            "localStorage.setItem('harvester_ops_section_forklift','waves');"
                            "localStorage.setItem('harvester_ops_fk_waves_view','lanes');")
        p = ctx.new_page()
        p.on("dialog", lambda d: d.dismiss())
        p.goto(base + "/", wait_until="domcontentloaded")
        p.wait_for_timeout(3000)

        def snap(name):
            p.mouse.move(1590, 890)
            p.evaluate("() => document.activeElement && document.activeElement.blur()")
            p.wait_for_timeout(600)
            path = ASSETS / f"{name}.png"
            p.screenshot(path=str(path))
            shots[name] = shrink(path)

        if "lanes" in only:
            p.click('.tab-group-head[data-group="vmio"]')
            p.click('.tab[data-tab="forklift"]')
            p.wait_for_selector("#tab-forklift [data-fkl-lane]", timeout=90000)
            p.wait_for_timeout(2000)
            snap("lanes")
        if "update" in only:
            p.evaluate("Versions.open()")
            p.click('#versions-modal [data-vpane="update"]')
            p.wait_for_timeout(2000)
            p.click('#versions-pane-update [data-upd="check"]')
            p.wait_for_timeout(4000)
            snap("update")
        b.close()
    for name, kb in shots.items():
        print(f"{name}.png : {kb} Ko")


if __name__ == "__main__":
    args = sys.argv[1:]
    base = BASE
    if "--base" in args:
        i = args.index("--base")
        base = args[i + 1]
        del args[i:i + 2]
    wanted = set(args)
    if wanted and wanted <= EXTRA:
        extra(wanted, base)
    else:
        BASE = base
        main(wanted or None)
