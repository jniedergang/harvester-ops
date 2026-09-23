"""Instantané et restauration, avec une preuve lisible à l'écran.

On crée un fichier « before-snapshot » dans la VM, on photographie la VM,
on le remplace par « after-snapshot », on restaure, et `ls` montre de
nouveau « before-snapshot ». Pas de `>` ni de `|` : la console envoie des
codes de touches physiques (c'est ce qui fait marcher l'AZERTY), et
Playwright tape ces caractères SANS appuyer sur Majuscule, comme aucun
clavier réel ne le fait. « > » arrivait « . » et le fichier n'était
jamais créé (constaté le 23/09/2026). La
restauration laisse la VM arrêtée (comme dans Harvester) : on la relance
depuis la console.
"""

NAME = "snapshots"
CLUSTER = "harvlab"
VM = "db-01"

# Un instantané NOUVEAU (absent au début de la scène) et prêt : un reste
# de la prise précédente, en cours de suppression, faisait croire que le
# nôtre était déjà là.
SNAP_READY = """async (vm) => {
  const d = await (await fetch(`/api/vm/harvlab/default/${vm}/snapshots`)).json();
  const before = window.__snapBefore || [];
  return (d.snapshots || []).some(s => s.ready && !before.includes(s.name)
                                     && !String(s.name).includes('pre'));
}"""

SNAP_LIST = """async (vm) => {
  const d = await (await fetch(`/api/vm/harvlab/default/${vm}/snapshots`)).json();
  window.__snapBefore = (d.snapshots || []).map(s => s.name);
  return window.__snapBefore.length;
}"""

RESTORE_DONE = """async (vm) => {
  const d = await (await fetch('/api/actions')).json();
  const a = (d.actions || [])
    .filter(x => x.action === `snapshot-restore:default/${vm}`)
    .sort((x, y) => y.started_at - x.started_at)[0];
  return !!(a && (a.status === 'done' || a.status === 'error'));
}"""

VM_RUNNING = """async (vm) => {
  const d = await (await fetch('/api/topology/harvlab')).json();
  const v = (d.vms || []).find(x => x.name === vm);
  return !!(v && v.phase === 'Running');
}"""


ARRANGE = """() => {
  const all = [...document.querySelectorAll('.floating-panel')];
  const con = all.find(e => e.id.startsWith('fp-vm-console'));
  const snap = all.find(e => e.id.startsWith('fp-vm-snapshots'));
  if (con) Object.assign(con.style, {left: '70px', top: '70px', width: '780px'});
  if (snap) Object.assign(snap.style, {left: (innerWidth - 650) + 'px', top: '70px', width: '630px'});
}"""


def arrange(cam):
    """Console à gauche, instantanés à droite : les deux fenêtres se
    recouvraient et le clic dans la console tombait sur l'autre."""
    cam.page.evaluate(ARRANGE)
    cam.pause(0.8)


def type_in_console(cam, text, pause=2.5):
    # Le clavier va à la fenêtre qui a le focus : on clique d'abord dans
    # l'écran de la console (le panneau des instantanés le lui prend).
    cam.click(".vm-console-screen", park=False)
    cam.page.keyboard.type(text, delay=70)
    cam.pause(pause)


def login(cam):
    type_in_console(cam, "\n", 1.5)
    type_in_console(cam, "cirros\n", 2)
    type_in_console(cam, "gocubsgo\n", 3)


def scene(cam):
    p = cam.page

    cam.click('.tab[data-tab="namespaces"]')
    p.wait_for_selector("#ns-vms-table tbody tr", timeout=60000)
    cam.say("intro")
    cam.preview()
    cam.pause(4)

    # -- une trace dans la VM -----------------------------------------------
    p.evaluate("(vm) => window.VMConsole.open('harvlab', 'default', vm)", VM)
    p.wait_for_selector(".vm-console-screen", timeout=60000)
    cam.pause(4)
    login(cam)
    cam.say("write")
    type_in_console(cam, "touch before-snapshot; sync; ls\n", 4)

    # -- l'instantané --------------------------------------------------------
    cam.say("snapshot")
    p.evaluate("(vm) => window.VMSnapshots.open('harvlab', 'default', vm)", VM)
    p.wait_for_selector("#snap-create", timeout=30000)
    arrange(cam)
    cam.pause(2)
    p.evaluate(SNAP_LIST, VM)
    cam.click("#snap-create")
    with cam.fast(6):
        cam.until(SNAP_READY, arg=VM, timeout=600, poll=3)
        cam.pause(3)
    cam.say("snapReady")
    cam.pause(4)

    # -- on change la VM -----------------------------------------------------
    cam.say("change")
    type_in_console(cam, "rm before-snapshot; touch after-snapshot; sync; ls\n", 4)

    # -- restauration --------------------------------------------------------
    cam.say("restore")
    # On restaure NOTRE instantané, pas le premier de la liste.
    mine = p.evaluate("""async (vm) => {
      const d = await (await fetch(`/api/vm/harvlab/default/${vm}/snapshots`)).json();
      const s = (d.snapshots || []).find(x => x.ready && !(window.__snapBefore || []).includes(x.name)
                                             && !String(x.name).includes('pre'));
      return s && s.name;
    }""", VM)
    # La liste du panneau ne se rafraîchit pas seule : on appuie sur
    # « Rafraîchir », comme le ferait l'opérateur, et on attend la ligne.
    cam.click("#snap-refresh")
    p.wait_for_selector(f'#snap-table [data-restore="{mine}"]:not([disabled])', timeout=30000)
    cam.click(f'#snap-table [data-restore="{mine}"]')
    p.wait_for_selector("#snap-restore-go", timeout=15000)
    cam.say("restoreOpts")
    cam.point("#snap-opt-pre")
    cam.pause(5)
    cam.click("#snap-restore-go")
    with cam.fast(8):
        cam.until(RESTORE_DONE, arg=VM, timeout=900)
        cam.pause(3)

    # -- relance et preuve ---------------------------------------------------
    cam.say("start")
    cam.click(".vm-console-power-start")
    with cam.fast(6):
        cam.until(VM_RUNNING, arg=VM, timeout=600)
        cam.pause(35)      # le temps que cirros démarre et affiche l'invite
    login(cam)
    type_in_console(cam, "ls\n", 3)
    cam.say("proof")
    cam.pause(7)
    type_in_console(cam, "exit\n", 1.5)
