"""v1.41.0 : la console partagée, dans un vrai navigateur.

KubeVirt n'accepte qu'une connexion VNC par VM. La console se reconnectait
d'elle-même en 400 ms : face à un autre client (la console d'Harvester), les
deux se reprenaient l'écran en boucle. Ce que ces tests tiennent :

  * écran pris par un autre client : la console le DIT et s'arrête ;
  * VM redémarrée : elle se reconnecte, comme avant ;
  * un collègue reprend la main : elle rejoint d'elle-même, sans voler
    l'écran à personne.

Le websocket est remplacé par une doublure qui se ferme aussitôt ouvert :
aucun test ne touche à un cluster.
"""

import json
import time

import pytest

playwright = pytest.importorskip("playwright")

FAKE_WS = """
window.WebSocket = class {
  static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
  constructor(url) {
    // noVNC vérifie ces propriétés une à une avant d'accepter le canal.
    this.onopen = null; this.onmessage = null; this.onerror = null; this.onclose = null;
    this.url = url; this.readyState = 0; this.binaryType = 'arraybuffer';
    this.protocol = '';
    setTimeout(() => { this.readyState = 1; this.onopen && this.onopen({}); }, 30);
    setTimeout(() => { this.readyState = 3;
      this.onclose && this.onclose({code: 1006, reason: '', wasClean: false}); }, 120);
  }
  send() {} close() { this.readyState = 3; }
  addEventListener() {} removeEventListener() {}
};
"""


def open_console(page, base_url, status):
    tickets = []
    page.route("**/console-ticket", lambda r, q: (tickets.append(1), r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"ticket": "t", "ws_path": "/ws/vnc/harv-fake/default/vm1"})))[1])
    page.route("**/console-status", lambda r, q: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(status())))
    page.context.add_init_script(
        FAKE_WS + "localStorage.setItem('harvester_ops_language','en');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_current_tab','namespaces');")
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    page.evaluate("() => window.VMConsole.open('harv-fake','default','vm1')")
    return tickets


def test_a_display_taken_by_another_client_is_not_fought_over(context, flask_server):
    page = context.new_page()
    tickets = open_console(page, flask_server["base_url"], lambda: {
        "count": 0, "viewers": [],
        "last_close": {"reason": "taken", "at": time.time()}})
    page.wait_for_timeout(3000)
    status = page.locator('.vm-console-status').inner_text()
    assert "Another console took this display" in status
    btn = page.locator('.vm-console-reconnect')
    assert btn.is_visible() and btn.inner_text() == "Take it back"
    assert btn.get_attribute("data-tip")
    # Une seule tentative : pas de reprise en boucle.
    assert len(tickets) == 1, tickets


def test_a_restarted_vm_is_reattached_as_before(context, flask_server):
    page = context.new_page()
    tickets = open_console(page, flask_server["base_url"], lambda: {
        "count": 0, "viewers": [],
        "last_close": {"reason": "vm-restarted", "at": time.time()}})
    page.wait_for_timeout(2500)
    assert len(tickets) >= 3, "la console ne se reconnecte plus après un redémarrage"


def test_it_rejoins_once_a_teammate_took_the_display_back(context, flask_server):
    page = context.new_page()
    state = {"n": 0}

    def status():
        state["n"] += 1
        # D'abord pris par un autre ; puis un collègue a repris la main.
        if state["n"] == 1:
            return {"count": 0, "viewers": [],
                    "last_close": {"reason": "taken", "at": time.time()}}
        return {"count": 1, "viewers": ["alice"], "last_close": None}
    tickets = open_console(page, flask_server["base_url"], status)
    page.wait_for_timeout(1500)
    assert len(tickets) == 1
    page.wait_for_timeout(6000)
    assert len(tickets) >= 2, "la console n'a pas rejoint la session reprise"


def test_take_it_back_reconnects_on_demand(context, flask_server):
    page = context.new_page()
    tickets = open_console(page, flask_server["base_url"], lambda: {
        "count": 0, "viewers": [],
        "last_close": {"reason": "taken", "at": time.time()}})
    page.wait_for_timeout(1500)
    page.locator('.vm-console-reconnect').click()
    page.wait_for_timeout(800)
    assert len(tickets) == 2
