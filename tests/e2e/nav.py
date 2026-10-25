"""Aide de navigation pour les tests e2e (navigation 2.0).

`goto(page, base_url, hash)` ouvre une adresse `#...` à froid et attend que
le routeur l'ait appliquée et que le voile de bascule soit retombé.
"""


def settle(page, timeout=15000):
    """Attend la fin des navigations du routeur et la chute du voile."""
    page.wait_for_function("() => !!(window.Router && window.App && Router.current())", timeout=timeout)
    page.evaluate("() => Router.idle()")
    page.wait_for_selector(".veil-fullscreen", state="detached", timeout=timeout)


def goto(page, base_url, hash="", timeout=15000):
    url = base_url.rstrip("/") + "/" + (hash if not hash or hash.startswith("#") else "#" + hash)
    page.goto(url, wait_until="domcontentloaded")
    settle(page, timeout)
    return page
