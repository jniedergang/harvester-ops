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


# 2.0.0 : adresse d'un écran de l'ancien menu (nom d'onglet de la 1.x), pour
# les tests qui posaient `harvester_ops_current_tab` ou cliquaient l'ancien
# menu. `cluster` = cluster Harvester, `kvm` = hôte KVM (écrans d'un hôte).
_HOST_PARAMS = ("storage", "network", "addons", "security", "monlog", "advanced")


def address(tab, cluster="harv-fake", kvm="node2", sub=None):
    c = f"#harvester/{cluster}"
    if tab == "overview":
        return c + ({"cluster": "/params/nodes", "events": "/overview/events"}.get(sub, ""))
    if tab in _HOST_PARAMS:
        return f"{c}/params/{tab}" + (f"/{sub}" if sub else "")
    if tab in ("startup", "shutdown"):
        return f"{c}/params/power/{tab}"
    if tab in ("vmimport", "forklift"):
        return f"{c}/imports/{tab}" + (f"/{sub}" if sub else "")
    fixed = {
        "namespaces": f"{c}/vms", "containers": f"{c}/containers", "clusterdr": f"{c}/dr",
        "hupgrade": f"{c}/params/upgrade", "hconn": f"{c}/params/conn",
        "allvms": "#vms", "allcontainers": "#containers", "forkliftglobal": "#migrations",
        "drplans": "#dr", "drlinks": "#dr/links", "kvmhosts": "#hosts/kvm", "kvmvms": "#vms/host/kvm",
        "kvmstorage": f"#kvm/{kvm}/params/pools", "kvmnetworks": f"#kvm/{kvm}/params/networks",
        "hosts": "#hosts",
    }
    if tab in fixed:
        return fixed[tab]
    if tab == "automation":
        return f"#automation/{sub or 'capi'}"
    return f"#{tab}"                       # gpus, bookings, usage, activity
