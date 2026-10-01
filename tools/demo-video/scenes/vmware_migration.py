"""Migration VMware -> Harvester à chaud, par Forklift, dans la console.

Commence sur la console VMware de la VM source, OS vivant (un service de
l'invité écrit sur l'écran son nom, l'heure, sa durée de fonctionnement et un
compteur d'écritures : `vmwlab-alive.sh`), passe par la source vCenter,
l'inventaire, la vague en cours de copies incrémentales et sa vue en couloirs,
fait la VRAIE bascule, et finit sur la console Harvester de la VM migrée : le
même système, le compteur reprend où il en était.

Tourné sur le banc harvlab2 + vmwlab, avec une vague déjà lancée (première
copie faite). La connexion à l'ESXi se fait hors caméra (`prepare`), avec le
mot de passe de l'environnement (VMWLAB_ESX_PASSWORD, tiré de Vault par
l'appelant). Variables : DEMO_WAVE, DEMO_SOURCE (vague et VM source)."""
import os

NAME = "vmware-migration"
CLUSTER = "harvlab2"
WAVE = os.environ.get("DEMO_WAVE", "couloirs")
SOURCE = os.environ.get("DEMO_SOURCE", "vmwlab-src-1")
TARGET_NS = os.environ.get("DEMO_TARGET_NS", "mig-lanes")
ESX = "https://172.16.2.80"


def prepare(browser):
    """Connexion au client web de l'ESXi, hors caméra."""
    ctx = browser.new_context(ignore_https_errors=True)
    pg = ctx.new_page()
    pg.goto(ESX + "/ui/", wait_until="domcontentloaded")
    pg.wait_for_selector("#username", timeout=60000)
    pg.fill("#username", "root")
    pg.fill("#password", os.environ["VMWLAB_ESX_PASSWORD"])
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(8000)
    state = ctx.storage_state()
    ctx.close()
    return state


SCREEN_LIT = """() => {
  const c = document.querySelector('.vm-console-screen canvas');
  if (!c || !c.width) return false;
  const d = c.getContext('2d').getImageData(0, 0, c.width, Math.min(c.height, 200)).data;
  for (let i = 0; i < d.length; i += 4) if (d[i] + d[i + 1] + d[i + 2] > 120) return true;
  return false;
}"""

WAVE_DONE = """async ([wave]) => {
  const d = await (await fetch('/api/forklift/harvlab2')).json();
  const w = (d.waves || []).find(x => x.name === wave);
  return !!w && ['succeeded', 'failed'].includes(w.state);
}"""

VM_RUNNING = """async ([ns, vm]) => {
  const d = await (await fetch('/api/vms/harvlab2?fresh=1')).json();
  const v = (d.vms || []).find(x => x.namespace === ns && x.name === vm);
  return !!v && v.phase === 'Running';
}"""


def scene(cam):
    p = cam.page
    base = p.url.split("#")[0].rstrip("/")

    # -- la source : la console VMware, OS vivant ------------------------------
    p.goto(ESX + "/ui/#/host/vms", wait_until="domcontentloaded")
    p.wait_for_timeout(5000)
    cam.click(f"a:has-text('{SOURCE}') >> nth=0")
    p.wait_for_timeout(4000)
    cam.click("text=/^Console$/ >> nth=0")
    cam.click("text=/Open browser console|Ouvrir une console de navigateur/ >> nth=0")
    p.wait_for_timeout(5000)
    cam.say("source")
    cam.pause(9)

    # -- la console harvops -----------------------------------------------------
    p.goto(base + "/", wait_until="domcontentloaded")
    p.wait_for_timeout(3000)
    cam.click('.tab-group-head[data-group="vmio"]')
    cam.pause(1)
    cam.click('.tab[data-tab="forklift"]')
    p.wait_for_timeout(3000)
    cam.click('[data-section="forklift"] [data-section-tab="sources"]')
    p.wait_for_timeout(2500)
    cam.say("intro")
    cam.pause(6)

    cam.click('[data-section="forklift"] [data-section-tab="inventory"]')
    p.wait_for_timeout(4000)
    cam.say("inventory")
    cam.pause(6)

    cam.click('[data-section="forklift"] [data-section-tab="waves"]')
    p.wait_for_timeout(3000)
    cam.click('#tab-forklift [data-fk="waves-mode"][data-mode="lanes"]')
    p.wait_for_timeout(1500)
    cam.say("lanes")
    cam.preview()
    cam.pause(8)

    cam.click(f'#tab-forklift [data-fkl-lane][data-wave="{WAVE}"] .fkl-label')
    p.wait_for_timeout(2500)
    cam.say("follow")
    cam.pause(8)
    cam.click(f'#fp-fk-wave-follow-{CLUSTER}-{WAVE} [data-action="close"]')
    cam.pause(1)

    # -- la bascule ---------------------------------------------------------------
    cam.click('#tab-forklift [data-fk="waves-mode"][data-mode="blocks"]')
    p.wait_for_timeout(1500)
    cam.say("cutover")
    cam.click(f'#tab-forklift [data-fk="wave-cutover"][data-wave="{WAVE}"]')
    cam.pause(4)
    with cam.fast(24):
        cam.until(WAVE_DONE, timeout=2400, poll=10, arg=[WAVE])
        cam.until(VM_RUNNING, timeout=900, poll=10, arg=[TARGET_NS, SOURCE])
        # l'invité attend sa carte réseau VMware, absente sur Harvester, avant
        # de finir de démarrer : ~3 min (vu au premier tournage, écran noir)
        cam.pause(200)
    cam.say("cutoverDone")
    cam.pause(6)

    # -- la cible : la VM dans Harvester, OS vivant -------------------------------
    cam.click('.tab[data-tab="namespaces"]')
    p.wait_for_timeout(3000)
    cam.say("target")
    cam.pause(5)
    p.evaluate("([c, ns, vm]) => window.VMConsole.open(c, ns, vm)", [CLUSTER, TARGET_NS, SOURCE])
    p.wait_for_selector(".vm-console-screen", timeout=60000)
    # une grande fenêtre : le texte de l'invité doit se lire à l'écran
    p.evaluate("""() => { const el = document.querySelector('.floating-panel:last-of-type');
      if (el) Object.assign(el.style, {left: '90px', top: '30px', width: '1360px', height: '780px'});
      window.dispatchEvent(new Event('resize')); }""")
    p.wait_for_timeout(3000)
    cam.click(".vm-console-screen", park=False)
    # la console ne reçoit une image qu'au premier changement d'écran de
    # l'invité (vu au tournage : écran noir sur une invite immobile) ; on
    # attend des pixels, en pressant une touche neutre s'il le faut
    with cam.fast(8):
        for _ in range(30):
            if p.evaluate(SCREEN_LIT):
                break
            p.keyboard.press("Shift")
            cam.pause(4)
    p.wait_for_timeout(3000)
    cam.say("targetAlive")
    cam.pause(10)
    p.evaluate("() => document.querySelectorAll('.floating-panel').forEach(el => "
               "window.FloatingPanels.close(el.id.replace(/^fp-/, '')))")
    p.wait_for_timeout(1000)

    if not p.locator('.tab[data-tab="forkliftglobal"]').is_visible():
        cam.click('.tab-group-head[data-group="vmio"]')
    cam.click('.tab[data-tab="forkliftglobal"]')
    p.wait_for_timeout(3500)
    cam.click('#tab-forkliftglobal [data-fkg="view"][data-mode="lanes"]')
    p.wait_for_timeout(1500)
    cam.say("global")
    cam.pause(6)
    cam.say("outro")
    cam.pause(4)
