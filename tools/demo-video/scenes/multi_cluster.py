"""Plusieurs clusters dans une console, dont un éteint, et les comptes
du cluster. Rien n'est modifié : c'est une visite."""

NAME = "multi-cluster"
CLUSTER = "harvlab"
OFF = "harv3"          # cluster déclaré mais éteint sur le banc


def scene(cam):
    p = cam.page

    cam.click('.tab[data-tab="overview"]')
    cam.click('button[data-overview-tab="metrics"]')
    p.wait_for_timeout(2500)
    cam.say("intro")
    cam.preview()
    cam.pause(5)

    cam.say("switch")
    cam.point("#cluster-select")
    cam.pause(2)
    p.select_option("#cluster-select", OFF)
    cam.park()
    cam.pause(5)
    cam.say("poweredOff")
    cam.pause(7)

    p.select_option("#cluster-select", CLUSTER)
    cam.park()
    cam.pause(3)
    cam.say("back")
    cam.pause(4)

    cam.click("#btn-settings")
    cam.click('.settings-tab[data-stab="clusters"]')
    p.wait_for_timeout(2000)
    cam.say("declarations")
    cam.pause(7)

    cam.click('.settings-tab[data-stab="husers"]')
    p.wait_for_timeout(3000)
    cam.say("accounts")
    cam.pause(8)
    cam.say("roles")
    cam.pause(7)
    cam.click("#btn-close-settings")
    cam.pause(2)
