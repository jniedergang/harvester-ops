"""Ouvrir une vue de blocs dans un test navigateur.

v1.57.0 : Réseau, Fabrique, Stockage et VPC ont quitté les sous-onglets de
l'Aperçu pour les sections de Harvester (Storage > Volumes, Network > VM
Networks, Overlay, Underlay) ; seule la vue Cluster reste dans l'Aperçu.
"""

WHERE = {"storage": ("storage", "volumes"), "network": ("network", "vmnets"),
         "fabric": ("network", "underlay"), "vpc": ("network", "overlay")}


def goto_board(page, board):
    """Montre la vue `board` comme le ferait un clic de l'exploitant."""
    page.wait_for_function("window.App && window.Sections")
    if board in ("metrics", "cluster"):
        # 2.0.0 : plus de sous-onglets d'aperçu : l'aperçu (métriques et
        # événements) ou Paramètres > Nœuds, par l'adresse de l'hôte
        sub = "/params/nodes" if board == "cluster" else ""
        page.evaluate(f"Router.go('#harvester/' + encodeURIComponent(App.getCurrentCluster()) + '{sub}')")
        page.evaluate("Router.idle()")
        return
    sec, pane = WHERE[board]
    # comme un lien d'une vue à l'autre : la section s'ouvre directement sur
    # l'onglet voulu, sans monter d'abord la vue de son premier onglet
    page.evaluate(f"Sections.open('{sec}', '{pane}')")
    page.wait_for_selector(f'#tab-{sec} [data-section-tab="{pane}"].active')
