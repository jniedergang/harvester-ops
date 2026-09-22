"""Stockage : ce qu'il reste vraiment, et un volume dégradé expliqué.

Le volume est dégradé pour de vrai avant le tournage (`seed.py degrade`
supprime une réplique) : Longhorn la reconstruit, et la vue montre le
diagnostic puis le retour à la normale.
"""

NAME = "storage"
CLUSTER = "harvlab"


def scene(cam):
    p = cam.page

    cam.click('.tab[data-tab="overview"]')
    cam.click('button[data-overview-tab="storage"]')
    p.wait_for_timeout(3000)
    cam.say("intro")
    cam.preview()
    cam.pause(5)

    cam.say("classes")
    cam.point(".fabric-body")
    cam.pause(6)

    cam.say("disks")
    cam.pause(6)

    # -- le volume dégradé --------------------------------------------------
    cam.say("degraded")
    # On ouvre le volume signalé : c'est là que vit le constat détaillé.
    # La vue pose une classe d'état sur le volume : viser le TEXTE
    # « degraded » ne marchait qu'en anglais (la prise française ouvrait un
    # volume sain pendant que le sous-titre parlait de dégradation).
    bad = p.locator(".sto-vol.health-degraded").first
    if bad.count() == 0:
        bad = p.locator(".sto-vol").first
    bad.scroll_into_view_if_needed()
    bad.hover()
    cam.pause(0.4)
    bad.click()
    cam.park()
    cam.pause(6)
    cam.say("advice")
    cam.pause(8)

    cam.say("rebuild")
    with cam.fast(10):
        cam.until("""async (cluster) => {
          const d = await (await fetch(`/api/storage-map/${cluster}`)).json();
          const vols = d.volumes || [];
          return vols.length > 0 && vols.every(
            v => v.health === 'healthy' && !(v.findings || []).length);
        }""", arg=CLUSTER, timeout=900)
        cam.pause(4)
    cam.say("healthy")
    cam.pause(6)
