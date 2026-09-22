"""Instantané et restauration, avec une preuve lisible à l'écran.

On écrit « v1 » dans un fichier de la VM, on photographie la VM, on
écrit « v2 », on restaure, et la VM relue dit de nouveau « v1 ». La
restauration laisse la VM arrêtée (comme dans Harvester) : on la relance
depuis la console.
"""

NAME = "snapshots"
CLUSTER = "harvlab"
VM = "db-01"

SNAP_READY = """async (vm) => {
  const d = await (await fetch(`/api/vm/harvlab/default/${vm}/snapshots`)).json();
  return (d.snapshots || []).some(s => s.ready && !String(s.name).includes('pre'));
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
    type_in_console(cam, "echo v1 > state.txt; sync; cat state.txt\n", 4)

    # -- l'instantané --------------------------------------------------------
    cam.say("snapshot")
    p.evaluate("(vm) => window.VMSnapshots.open('harvlab', 'default', vm)", VM)
    p.wait_for_selector("#snap-create", timeout=30000)
    cam.pause(2)
    cam.click("#snap-create")
    with cam.fast(6):
        cam.until(SNAP_READY, arg=VM, timeout=600, poll=3)
        cam.pause(3)
    cam.say("snapReady")
    cam.pause(4)

    # -- on change la VM -----------------------------------------------------
    cam.say("change")
    type_in_console(cam, "echo v2 > state.txt; sync; cat state.txt\n", 4)

    # -- restauration --------------------------------------------------------
    cam.say("restore")
    cam.click("#snap-table [data-restore]:not([disabled])")
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
    type_in_console(cam, "cat state.txt\n", 3)
    cam.say("proof")
    cam.pause(7)
    type_in_console(cam, "exit\n", 1.5)
