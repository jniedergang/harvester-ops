#!/usr/bin/env python3
"""Filme une scène de démonstration dans la vraie console.

Le navigateur joue la scène sur le serveur de développement (ou sur
n'importe quelle instance passée en --base) et Playwright enregistre la
fenêtre. La scène ne dit pas QUAND couper : elle pose des repères
(`cam.say`, `cam.fast`, `cam.preview`) que le montage (build.py) exploite.

    ./record.py cluster_maintenance --lang en

Sortie : out/<scene>-<lang>/raw.webm + cues.json
"""

import argparse
import importlib
import json
import shutil
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
# On filme plus petit que la vidéo finale : l'interface occupe alors 25 % de
# plus dans l'image, et le texte reste lisible quand la vidéo est réduite
# (dans un README, par exemple). Le montage remonte l'image en 1080p.
# Agrandir la page côté navigateur ne marche pas : Chromium redimensionne
# aussi la fenêtre de rendu, l'effet est nul.
WIDTH, HEIGHT = 1536, 864

# Halo qui suit la souris et onde au clic : sans lui, les clics de
# Playwright sont invisibles à l'écran et la vidéo paraît magique.
POINTER = """
(() => {
  const ring = document.createElement('div');
  ring.style.cssText = `position:fixed;z-index:2147483647;width:26px;height:26px;
    margin:-13px 0 0 -13px;border:3px solid rgba(48,186,120,.95);border-radius:50%;
    pointer-events:none;transition:transform .08s linear;box-shadow:0 0 0 2px rgba(0,0,0,.25);
    left:-100px;top:-100px`;
  const add = () => document.body && document.body.appendChild(ring);
  document.readyState === 'loading' ? addEventListener('DOMContentLoaded', add) : add();
  addEventListener('mousemove', e => { ring.style.left = e.clientX + 'px';
                                       ring.style.top = e.clientY + 'px'; }, true);
  addEventListener('mousedown', e => {
    const w = document.createElement('div');
    w.style.cssText = `position:fixed;z-index:2147483646;left:${e.clientX}px;top:${e.clientY}px;
      width:10px;height:10px;margin:-5px 0 0 -5px;border-radius:50%;
      border:3px solid rgba(48,186,120,.9);pointer-events:none`;
    document.body.appendChild(w);
    w.animate([{transform:'scale(1)',opacity:1},{transform:'scale(7)',opacity:0}],
              {duration:520,easing:'ease-out'}).onfinish = () => w.remove();
  }, true);
})();
"""


class Cam:
    """Le carnet de bord de la scène : repères datés + gestes lisibles."""

    def __init__(self, page, t0):
        self.page = page
        self.t0 = t0
        self.cues = []

    def _at(self):
        return round(time.time() - self.t0, 3)

    def say(self, key):
        """Pose un sous-titre (le texte vit dans captions/<scene>.<lang>.yaml)."""
        self.cues.append({"t": self._at(), "kind": "say", "key": key})

    def preview(self):
        """Marque le début de l'extrait animé du README."""
        self.cues.append({"t": self._at(), "kind": "preview"})

    def fast(self, factor=8):
        return _Fast(self, factor)

    # -- gestes ----------------------------------------------------------
    def pause(self, seconds):
        self.page.wait_for_timeout(int(seconds * 1000))

    def point(self, selector, settle=0.6):
        """Amène la souris sur un élément sans cliquer (le halo suit)."""
        loc = self.page.locator(selector).first
        loc.scroll_into_view_if_needed()
        loc.hover()
        self.pause(settle)
        return loc

    def click(self, selector, settle=0.9, park=True):
        loc = self.point(selector, settle=0.35)
        loc.click()
        # On éloigne le pointeur : sinon l'infobulle du bouton reste ouverte
        # et masque le panneau qu'on vient d'ouvrir.
        if park:
            self.park()
        self.pause(settle)
        return loc

    def until(self, js, timeout=900, poll=5, arg=None):
        """Attend une condition côté cluster, en interrogeant l'API.

        ⚠️ `page.wait_for_function` NE SAIT PAS attendre une fonction
        asynchrone : elle reçoit une promesse, la trouve « vraie » et rend
        la main au premier tour (mesuré le 22/09/2026 : 0,1 s au lieu
        d'attendre). D'où cette boucle, qui évalue pour de bon — et qui
        laisse le film tourner pendant l'attente.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.page.evaluate(js, arg):
                return True
            self.pause(poll)
        raise TimeoutError(f"condition jamais remplie en {timeout}s")

    def park(self):
        """Pointeur sur une zone neutre, et on retire le focus du bouton.

        Les infobulles de la console s'affichent aussi au focus : après un
        clic, celle du bouton restait ouverte sur le panneau pendant vingt
        secondes de film."""
        self.page.mouse.move(940, 620)
        self.page.evaluate("() => document.activeElement && document.activeElement.blur()")


class _Fast:
    def __init__(self, cam, factor):
        self.cam, self.factor = cam, factor

    def __enter__(self):
        self.cam.cues.append({"t": self.cam._at(), "kind": "fast-start",
                              "factor": self.factor})
        return self.cam

    def __exit__(self, *exc):
        self.cam.cues.append({"t": self.cam._at(), "kind": "fast-end"})
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("scene", help="module de scenes/ (sans .py)")
    ap.add_argument("--lang", default="en", choices=("en", "fr"))
    ap.add_argument("--base", default="http://127.0.0.1:8095")
    ap.add_argument("--cluster", default=None, help="remplace le cluster de la scène")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    mod = importlib.import_module(f"scenes.{args.scene}")
    out = Path(args.out or HERE / "out" / f"{mod.NAME}-{args.lang}")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--force-device-scale-factor=1"])
        t0 = time.time()
        ctx = browser.new_context(
            viewport={"width": WIDTH, "height": HEIGHT},
            record_video_dir=str(out), record_video_size={"width": WIDTH, "height": HEIGHT},
            locale="fr-FR" if args.lang == "fr" else "en-US",
        )
        ctx.add_init_script(POINTER)
        # La console garde langue et cluster dans le navigateur : on les pose
        # avant le premier rendu pour ne pas filmer un changement d'état.
        ctx.add_init_script(
            "localStorage.setItem('harvester_ops_language','%s');"
            "localStorage.setItem('harvester_ops_current_cluster','%s');"
            % (args.lang, args.cluster or mod.CLUSTER))
        page = ctx.new_page()
        # Les actions de la console demandent confirmation : la scène les a
        # décidées, on répond oui (les boîtes natives ne sont pas filmées).
        page.on("dialog", lambda d: d.accept())
        cam = Cam(page, t0)
        page.goto(args.base + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        try:
            mod.scene(cam)
        finally:
            cam.pause(1.2)
            video = page.video
            wall = round(time.time() - t0, 3)
            ctx.close()
            raw = Path(video.path())
            raw.rename(out / "raw.webm")
            browser.close()

    (out / "cues.json").write_text(json.dumps(
        {"scene": mod.NAME, "lang": args.lang, "cluster": args.cluster or mod.CLUSTER,
         "wall_duration": wall, "cues": cam.cues}, indent=2, ensure_ascii=False))
    print(f"prise : {out}/raw.webm ({wall:.1f}s réelles, {len(cam.cues)} repères)")


if __name__ == "__main__":
    main()
