#!/usr/bin/env python3
"""Enregistre ce que la console lit, pour la démo du site (v1.86.0).

Parcourt une console ouverte (HARVESTER_OPS_AUTH=none) avec un navigateur
sans tête : pour chaque cluster du sélecteur, chaque onglet, chaque
sous-onglet et quelques fenêtres (réglages d'une VM, snapshots, migration,
menu d'actions, YAML, notes), puis les vues de tous les clusters, la fenêtre
des versions et celle des réglages. Ne clique QUE des onglets : aucun geste
qui modifie un cluster.

Chaque réponse GET de /api/ est gardée, par méthode, chemin et requête :
`recording.json`. Ce fichier porte les noms, IP et objets réels des
clusters parcourus : il ne quitte pas la machine, `sanitize.py` en tire les
données de la démo.

    python3 tools/demo-site/record.py --base http://127.0.0.1:8105 --out /tmp/rec.json
"""
import argparse
import json
import time
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

GLOBAL_TABS = ("activity", "forkliftglobal", "allvms")


class Recorder:
    def __init__(self, page):
        self.page = page
        self.data = {}
        self.inflight = 0
        self.last = time.time()
        page.on("request", self._req)
        page.on("requestfinished", self._done)
        page.on("requestfailed", self._done)

    def _req(self, r):
        if "/api/" in r.url:
            self.inflight += 1
            self.last = time.time()

    def _done(self, r):
        if "/api/" not in r.url:
            return
        self.inflight = max(0, self.inflight - 1)
        self.last = time.time()
        if r.method != "GET":
            return
        try:
            resp = r.response()
            if resp is None or "json" not in (resp.headers.get("content-type") or ""):
                return
            body = resp.json()
        except Exception:
            return
        u = urlsplit(r.url)
        key = u.path + (("?" + u.query) if u.query else "")
        self.data[key] = {"status": resp.status, "body": body}

    def settle(self, quiet=0.7, cap=8.0):
        """Attend que plus aucune requête /api/ ne soit en vol depuis `quiet`
        secondes (la console interroge aussi en continu : plafond `cap`)."""
        t0 = time.time()
        while time.time() - t0 < cap:
            if self.inflight == 0 and time.time() - self.last > quiet:
                return
            self.page.wait_for_timeout(150)


def click_all(page, rec, scope, selector, seen):
    for el in page.locator(f"{scope} {selector}").all():
        try:
            if not el.is_visible():
                continue
            ident = el.evaluate("e => e.outerHTML.slice(0, 160)")
            if ident in seen:
                continue
            seen.add(ident)
            el.click(timeout=3000)
            page.mouse.move(1500, 900)
            rec.settle()
        except Exception:
            continue


def walk_tab(page, rec, tab):
    page.evaluate("t => { const a = document.querySelector(`.tab[data-tab='${t}'], .tab[data-subtab='${t}']`);"
                  " if (a) a.click(); }", tab)
    page.mouse.move(1500, 900)
    rec.settle()
    scope = ".tab-content.active"
    seen = set()
    for _ in range(2):                 # sous-onglets, puis ceux qu'ils font apparaître
        click_all(page, rec, scope, ".sub-tab, [data-section-tab], [data-overview-tab]", seen)


def windows_for(page, rec, cluster):
    vms = rec.data.get(f"/api/vms/{cluster}", {}).get("body", {}).get("vms") or []
    running = [v for v in vms if v.get("phase") == "Running"] or vms
    for v in running[:2]:
        args = [cluster, v["namespace"], v["name"]]
        for js in ("([c,n,v]) => window.VMEdit && VMEdit.open(c,n,v)",
                   "([c,n,v]) => window.VMSnapshots && VMSnapshots.open(c,n,v)",
                   "([c,n,v]) => window.VMMigrate && VMMigrate.open(c,n,v)",
                   "([c,n,v]) => window.YamlWindow && YamlWindow.open(c,'vm',n,v)",
                   "([c,n,v]) => window.Notes && Notes.open('vm',c,n,v)"):
            try:
                page.evaluate(js, args)
                rec.settle()
            except Exception:
                pass
        try:
            page.evaluate("([c,n,v]) => window.VMActions && VMActions.open(document.body, c, n, v)", args)
            rec.settle()
            page.keyboard.press("Escape")
        except Exception:
            pass
        page.evaluate("() => document.querySelectorAll('.floating-panel').forEach(el => "
                      "window.FloatingPanels && FloatingPanels.close(el.id.replace(/^fp-/, '')))")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8105")
    ap.add_argument("--out", required=True)
    ap.add_argument("--clusters", help="liste séparée par des virgules (défaut : tout le sélecteur)")
    a = ap.parse_args()
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1600, "height": 950})
        page.add_init_script("localStorage.setItem('harvester_ops_language','en');")
        page.on("dialog", lambda d: d.dismiss())
        rec = Recorder(page)
        page.goto(a.base + "/", wait_until="domcontentloaded")
        page.wait_for_function("window.App && App.getCurrentCluster && App.getCurrentCluster()")
        rec.settle()
        clusters = a.clusters.split(",") if a.clusters else page.locator("#cluster-select option").evaluate_all(
            "os => os.map(o => o.value)")
        tabs = page.locator("nav .tab[data-tab], nav .tab[data-subtab]").evaluate_all(
            "els => els.map(e => e.dataset.tab || e.dataset.subtab)")
        for c in clusters:
            print("cluster", c, flush=True)
            page.select_option("#cluster-select", c)
            rec.settle(cap=12)
            for t in tabs:
                if t in GLOBAL_TABS:
                    continue
                walk_tab(page, rec, t)
            windows_for(page, rec, c)
        for t in GLOBAL_TABS:
            walk_tab(page, rec, t)
        for opener, tabsel in (("#btn-version", "#versions-modal [data-vpane]"),
                               ("#btn-settings", "#settings-modal .settings-tab")):
            try:
                page.click(opener)
                rec.settle()
                for el in page.locator(tabsel).all():
                    el.click()
                    rec.settle()
                page.keyboard.press("Escape")
            except Exception:
                pass
        b.close()
    with open(a.out, "w") as f:
        json.dump(rec.data, f)
    print(f"{len(rec.data)} réponses -> {a.out}")


if __name__ == "__main__":
    main()
