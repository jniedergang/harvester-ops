"""Activité : tout ce qui a été fait, avec son journal, et le paquet de
support qu'on envoie quand quelque chose ne va pas."""

NAME = "activity"
CLUSTER = "harvlab"

BUNDLE_DONE = "() => !!document.querySelector('#support-result .summary-bar')"


def scene(cam):
    p = cam.page

    cam.click('.tab[data-tab="activity"]')
    p.wait_for_selector("#activity-history tbody tr", timeout=60000)
    cam.pause(1.5)
    cam.say("intro")
    cam.preview()
    cam.pause(5)

    cam.say("history")
    cam.point("#activity-history tbody tr >> nth=0")
    cam.pause(5)

    # -- filtrer, puis ouvrir une action --------------------------------------
    cam.say("filter")
    cam.click("#act-f-q")
    p.keyboard.type("startup", delay=90)
    cam.park()
    cam.pause(4)
    cam.say("details")
    cam.click("#activity-history tbody tr >> nth=0")
    p.wait_for_timeout(1500)
    cam.pause(8)
    cam.say("dock")
    cam.pause(6)
    try:
        cam.click("#btn-activity-log-close")
    except Exception:                                              # noqa: BLE001
        pass
    cam.click("#btn-act-filter-reset")
    cam.pause(1)

    # -- le paquet de support -------------------------------------------------
    cam.click("#btn-settings")
    cam.click('.settings-tab[data-stab="support"]')
    cam.say("support")
    cam.point("#sup-anonymize")
    cam.pause(6)
    cam.click("#btn-build-bundle")
    cam.say("bundleBuilding")
    with cam.fast(6):
        cam.until(BUNDLE_DONE, timeout=600, poll=2)
        cam.pause(2)
    cam.say("bundleReady")
    cam.pause(7)
