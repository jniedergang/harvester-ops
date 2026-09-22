"""Machines virtuelles : la liste, la création guidée, la console.

La VM créée ici l'est pour de vrai ; le ménage se fait après le tournage
(`seed.py down` la laisse, elle porte un nom à part).
"""

NAME = "vms-console"
CLUSTER = "harvlab"
CONSOLE_VM = "api-01"      # la scène se déconnecte en partant : la prise
                           # suivante retrouve l'invite de connexion


def scene(cam):
    p = cam.page

    cam.click('.tab[data-tab="namespaces"]')
    p.wait_for_selector("#ns-vms-table tbody tr", timeout=60000)
    cam.pause(1.5)
    cam.say("intro")
    cam.preview()
    cam.pause(5)

    cam.say("table")
    cam.point("#ns-vms-table tbody tr >> nth=0")
    cam.pause(5)

    # -- sélection multiple -------------------------------------------------
    cam.say("bulk")
    cam.click("#ns-vms-table tbody tr >> nth=0 >> input[type=checkbox]")
    cam.click("#ns-vms-table tbody tr >> nth=1 >> input[type=checkbox]")
    cam.pause(5)
    cam.click("#btn-bulk-clear")
    cam.pause(1)

    # -- création guidée ----------------------------------------------------
    cam.say("create")
    cam.click("#btn-vm-create")
    p.wait_for_timeout(2500)
    cam.pause(4)
    cam.say("createForm")
    cam.pause(6)
    cam.pause(1)

    # -- la console ---------------------------------------------------------
    cam.say("console")
    # On referme la fenêtre de création : deux fenêtres l'une sur l'autre
    # brouillent l'image (la console s'ouvre par-dessus).
    # Le bouton de fermeture est recouvert par le sommaire de l'éditeur :
    # on passe par l'API des fenêtres, c'est le même chemin que le clic.
    p.evaluate("() => window.FloatingPanels.close('vm-create')")
    p.wait_for_timeout(1200)
    p.evaluate("(vm) => window.VMConsole.open('harvlab', 'default', vm)", CONSOLE_VM)
    p.wait_for_selector(".vm-console-screen", timeout=60000)
    cam.pause(6)
    cam.say("consoleLive")
    # Il faut cliquer dans l'écran pour que le clavier y aille : `park()`
    # retire le focus après chaque clic, la console n'en recevrait rien.
    cam.click(".vm-console-screen", park=False)
    cam.pause(1)
    # cirros : on se connecte pour montrer que le clavier va bien jusqu'à la VM
    p.keyboard.type("cirros\n", delay=90)
    cam.pause(2)
    p.keyboard.type("gocubsgo\n", delay=90)
    cam.pause(3)
    p.keyboard.type("uname -a\n", delay=90)
    cam.pause(4)
    cam.say("consoleShared")
    cam.pause(6)
    # On rend la machine telle qu'on l'a trouvée.
    p.keyboard.type("exit\n", delay=60)
    cam.pause(1.5)
