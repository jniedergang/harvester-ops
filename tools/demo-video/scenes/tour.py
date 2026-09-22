"""Tour du produit : on descend le menu de gauche, de haut en bas.

Une seule vidéo qui montre tout, pour quelqu'un qui découvre : chaque
entrée du menu, trois à six secondes, sans action destructrice. La seule
chose qu'on change vraiment, c'est la langue de l'interface, à la fin.
"""

NAME = "tour"
CLUSTER = "harvlab"


def scene(cam):
    p = cam.page

    cam.click('.tab[data-tab="overview"]')
    cam.click('button[data-overview-tab="metrics"]')
    p.wait_for_timeout(2500)
    cam.say("intro")
    cam.preview()
    cam.pause(5)

    cam.say("metrics")
    cam.pause(5)

    cam.click('button[data-overview-tab="cluster"]')
    p.wait_for_selector(".cm-host", timeout=60000)
    cam.say("cluster")
    cam.pause(6)

    cam.click('button[data-overview-tab="network"]')
    cam.say("network")
    cam.pause(6)

    cam.click('button[data-overview-tab="storage"]')
    cam.say("storage")
    cam.pause(6)

    cam.click('button[data-overview-tab="fabric"]')
    cam.say("fabric")
    cam.pause(6)

    cam.click('.tab[data-tab="startup"]')
    cam.say("startup")
    cam.pause(6)

    cam.click('.tab[data-tab="shutdown"]')
    cam.say("shutdown")
    cam.pause(7)

    cam.click('.tab[data-tab="namespaces"]')
    p.wait_for_timeout(1500)
    cam.say("vms")
    cam.pause(6)

    cam.click('.tab-group-head[data-group="automation"]')
    cam.pause(1)
    cam.click('.tab[data-subtab="capi"]')
    cam.say("capi")
    cam.pause(6)

    cam.click('.tab[data-subtab="terraform"]')
    cam.say("terraform")
    cam.pause(6)

    cam.click('.tab[data-subtab="pxe"]')
    cam.say("baremetal")
    cam.pause(7)

    cam.click('.tab[data-tab="activity"]')
    p.wait_for_timeout(1500)
    cam.say("activity")
    cam.pause(7)

    # -- réglages, et les cinq langues -------------------------------------
    cam.click("#btn-settings")
    cam.click('.settings-tab[data-stab="language"]')
    cam.say("settings")
    cam.pause(4)
    cam.click('#lang-grid button[data-lang="de"]')
    cam.pause(3)
    cam.say("languages")
    cam.click('#lang-grid button[data-lang="es"]')
    cam.pause(3)
    lang = "fr" if cam.lang == "fr" else "en"
    cam.click(f'#lang-grid button[data-lang="{lang}"]')
    cam.pause(2)
    cam.click("#btn-close-settings")
    cam.pause(1)
    cam.say("outro")
    cam.pause(6)
