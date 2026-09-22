"""Vue Cluster et maintenance d'un nœud, jouée pour de vrai.

La scène choisit elle-même le nœud à vider (celui qui porte le plus de VMs
au moment du tournage) : le banc ne place pas les VMs deux fois de la même
façon, et une vidéo qui dépend d'un nom de nœud serait à refaire à chaque
prise.
"""

NAME = "cluster-maintenance"
CLUSTER = "harvlab"


def busiest_node(cam):
    """Nœud portant le plus de VMs en marche, d'après l'API de la console."""
    return cam.page.evaluate("""async (cluster) => {
      const r = await fetch(`/api/topology/${cluster}`);
      const d = await r.json();
      const count = {};
      (d.vms || []).filter(v => v.node).forEach(v => { count[v.node] = (count[v.node] || 0) + 1; });
      const names = (d.nodes || []).map(n => n.name);
      names.sort((a, b) => (count[b] || 0) - (count[a] || 0));
      return { node: names[0], vms: (d.vms || []).filter(v => v.node === names[0]).map(v => v.name) };
    }""", CLUSTER)


def scene(cam):
    page = cam.page

    # -- la vue d'ensemble ------------------------------------------------
    cam.click('.tab[data-tab="overview"]')
    cam.click('button[data-overview-tab="cluster"]')
    page.wait_for_selector(".cm-host", timeout=60000)
    cam.pause(1.5)
    cam.say("intro")
    cam.preview()
    cam.pause(4)

    cam.say("hosts")
    cam.point(".cm-host >> nth=1 >> .cm-host-head")
    cam.pause(3.5)

    # -- une VM en détail --------------------------------------------------
    target = busiest_node(cam)
    node = target["node"]
    # Le nœud choisi sert aussi aux attentes côté navigateur.
    cam.page.evaluate("n => { window.__demoNode = n; }", node)
    cam.say("vmCards")
    vm = page.locator(f'.cm-host[data-host="{node}"] .cm-vm').first
    vm.scroll_into_view_if_needed()
    vm.hover()
    cam.pause(4.5)

    # -- le nœud et son panneau -------------------------------------------
    cam.say("nodePanel")
    cam.click(f'.cm-host-head[data-node="{node}"]')
    cam.pause(3)

    # -- le pré-contrôle de maintenance -----------------------------------
    cam.say("precheck")
    cam.click('[data-cm-act="node-maint-check"]')
    page.wait_for_selector(".cm-maint-box", timeout=30000)
    cam.pause(4)
    cam.say("precheckDetail")
    cam.point(".cm-maint-box")
    cam.pause(5)

    # -- on y va (action réelle) ------------------------------------------
    cam.say("enter")
    cam.click('[data-cm-act="node-maint-enter"]')
    cam.pause(6)          # le dock des actions prend la main
    cam.say("dock")
    cam.pause(5)

    cam.say("waiting")
    # Sur le banc, vider un nœud prend de deux à huit minutes : Longhorn ne
    # laisse partir son gestionnaire d'instances qu'une fois les répliques
    # reconstruites ailleurs. On montre l'attente accélérée, pas coupée.
    with cam.fast(20):
        # Harvester marque le nœud « en maintenance » AVANT d'avoir vidé
        # quoi que ce soit : attendre cette marque ne montrerait aucune
        # migration. On attend que le nœud soit réellement vide.
        cam.until("""async (cluster) => {
          const d = await (await fetch(`/api/topology/${cluster}`)).json();
          const n = (d.nodes || []).find(x => x.name === window.__demoNode);
          const left = (d.vms || []).filter(v => v.node === window.__demoNode).length;
          // « requested » veut dire « demande posée », pas « nœud vidé » :
          // seul `completed` dit que Harvester a fini.
          return !!(n && n.maintenance === 'completed' && left === 0);
        }""", arg=CLUSTER)
        cam.pause(4)

    cam.say("moved")
    cam.pause(6)

    # -- retour à la normale ----------------------------------------------
    cam.say("leave")
    cam.click(f'.cm-host-head[data-node="{node}"]')
    cam.pause(1.5)
    cam.click('[data-cm-act="node-maint-leave"]')
    cam.pause(4)
    with cam.fast(8):
        cam.until("""async (cluster) => {
          const d = await (await fetch(`/api/topology/${cluster}`)).json();
          const n = (d.nodes || []).find(x => x.name === window.__demoNode);
          return !!(n && !n.maintenance && n.schedulable);
        }""", arg=CLUSTER, timeout=600)
        cam.pause(3)
    cam.say("back")
    cam.pause(4)

    # -- une machine déplacée à la main ------------------------------------
    cam.say("migrate")
    vm_card = page.locator(".cm-vm", has_text="web-01").first
    vm_card.scroll_into_view_if_needed()
    vm_card.click()
    cam.park()
    cam.pause(1.5)
    before = page.evaluate("""async (cluster) => {
      const d = await (await fetch(`/api/topology/${cluster}`)).json();
      return (d.vms || []).find(v => v.name === 'web-01').node;
    }""", CLUSTER)
    page.evaluate("n => { window.__demoVmNode = n; }", before)
    cam.click('[data-cm-act="vm-migrate"]')
    page.wait_for_selector("#migrate-trigger", timeout=30000)
    cam.pause(3)
    cam.click("#migrate-trigger")
    cam.pause(4)
    with cam.fast(6):
        cam.until("""async (cluster) => {
          const d = await (await fetch(`/api/topology/${cluster}`)).json();
          const v = (d.vms || []).find(x => x.name === 'web-01');
          return !!(v && v.phase === 'Running' && v.node && v.node !== window.__demoVmNode);
        }""", arg=CLUSTER, timeout=600)
        cam.pause(3)
    cam.say("migrateDone")
    # On referme le panneau pour finir sur la vue d'ensemble.
    page.keyboard.press("Escape")
    cam.pause(6)
