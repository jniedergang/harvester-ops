"""Réseau : les réseaux du cluster, la fabrique physique, le chemin d'une VM."""

NAME = "network"
CLUSTER = "harvlab"


def scene(cam):
    p = cam.page

    cam.click('.tab[data-tab="overview"]')
    cam.click('button[data-overview-tab="network"]')
    p.wait_for_timeout(3000)
    cam.say("intro")
    cam.preview()
    cam.pause(6)

    cam.say("attachments")
    cam.point(".fabric-body")
    cam.pause(6)

    cam.say("leaves")
    cam.pause(6)

    # -- la fabrique --------------------------------------------------------
    cam.click('button[data-overview-tab="fabric"]')
    p.wait_for_timeout(3000)
    cam.say("fabric")
    cam.pause(6)

    cam.say("fabricDetail")
    cam.pause(7)

    # -- le chemin réseau d'une machine -------------------------------------
    cam.say("path")
    p.evaluate("() => window.VMEdit.open('harvlab', 'default', 'web-01')")
    p.wait_for_timeout(3500)
    # Le chemin réseau est un onglet DANS la section réseau de l'éditeur.
    cam.click('.vm-edit-nav button[data-section="network"]')
    cam.pause(1)
    cam.click('[data-net-tab="path"]')
    p.wait_for_selector(".netpath-box", timeout=60000)
    cam.pause(6)
    cam.say("pathDetail")
    cam.pause(7)
