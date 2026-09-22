"""Arrêt gracieux puis redémarrage d'un cluster entier, pour de vrai.

C'est la raison d'être de l'outil : éteindre un cluster Harvester dans le
bon ordre, et le remonter de même. Le banc n'a pas de carte
d'administration : la scène rallume elle-même les machines (`harvlab.sh
start`), à la place d'un BMC ou d'une main sur le bouton, et le sous-titre
le dit.
"""

import pathlib
import subprocess

NAME = "shutdown-startup"
CLUSTER = "harvlab"

HARVLAB = (pathlib.Path(__file__).resolve().parents[3]
           / "tests" / "bench" / "harvlab" / "harvlab.sh")

ACTION_DONE = """async (kind) => {
  const d = await (await fetch('/api/actions')).json();
  const a = (d.actions || [])
    .filter(x => x.action === kind && !x.dry_run)
    .sort((x, y) => y.started_at - x.started_at)[0];
  return !!(a && (a.status === 'done' || a.status === 'error'));
}"""


def scene(cam):
    p = cam.page

    cam.click('.tab[data-tab="shutdown"]')
    p.wait_for_timeout(2000)
    cam.say("intro")
    cam.preview()
    cam.pause(5)

    cam.say("steps")
    cam.point("#shutdown-steps")
    cam.pause(7)

    cam.say("options")
    cam.point("#opt-snapshot")
    cam.pause(6)

    cam.say("launch")
    cam.click("#btn-shutdown")
    cam.pause(8)

    cam.say("running")
    cam.pause(7)

    with cam.fast(20):
        cam.until(ACTION_DONE, arg="shutdown", timeout=1800)
        cam.pause(4)
    cam.say("down")
    cam.pause(6)

    # -- on rallume les machines (ce que ferait un BMC) ---------------------
    cam.say("powerOn")
    subprocess.run([str(HARVLAB), "start"], capture_output=True, text=True, check=False)
    cam.pause(6)

    cam.click('.tab[data-tab="startup"]')
    p.wait_for_timeout(2000)
    cam.say("startup")
    cam.pause(6)
    cam.click("#btn-startup")
    cam.pause(8)
    cam.say("startupRunning")
    cam.pause(6)

    with cam.fast(25):
        cam.until(ACTION_DONE, arg="startup", timeout=2400)
        cam.pause(4)

    cam.say("back")
    cam.click('.tab[data-tab="overview"]')
    cam.click('button[data-overview-tab="cluster"]')
    p.wait_for_selector(".cm-host", timeout=120000)
    cam.pause(7)
    cam.say("outro")
    cam.pause(6)
