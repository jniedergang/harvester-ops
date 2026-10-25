/**
 * harvester-ops : page d'un hôte Harvester (navigation 2.0, étape 2)
 *
 * Un bandeau posé au-dessus des sections existantes : famille, nom, état,
 * le sous-menu de l'hôte (Vue d'ensemble, Machines virtuelles, Conteneurs,
 * Imports et migrations, Paramètres, Disaster Recovery) et, sur une page de
 * Paramètres ou d'Imports, la barre de ses onglets. Aucune section n'est
 * réécrite : le routeur montre la section que l'adresse désigne
 * (Sections.show, setTab), le bandeau ne fait que l'encadrer.
 *
 * Onglets de Paramètres (Routes.HARVESTER_PARAMS) : Nœuds (vue de blocs du
 * cluster, maintenance, « Joindre un nœud »), Stockage, Réseau, Add-ons,
 * Sécurité (avec les comptes du cluster), Monitoring et journaux, Avancé,
 * Mise à jour (contenu de la fenêtre Upgrade), Alimentation (Démarrage,
 * Arrêt), Connexion (pane léger, ici).
 *
 * Le bandeau ne lance aucune lecture : l'état vient de la liste des hôtes
 * (HostNav). Seul le pane Connexion lit /api/clusters (déjà lu par l'App).
 */
const HostPage = (() => {
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 14) => (window.Icons ? Icons.svg(n, { size }) : '');
  const enc = encodeURIComponent;
  const $ = (s) => document.querySelector(s);

  let route = null;

  // clés littérales (contrôle de parité i18n)
  const PARAM_TABS = () => ({
    nodes: [tr('hp.tab.nodes'), tr('hp.tab.nodesTip'), 'node'],
    storage: [tr('tab.storage'), tr('tab.storageTip'), 'storage'],
    network: [tr('tab.network'), tr('tab.networkTip'), 'network'],
    addons: [tr('tab.addons'), tr('tab.addonsTip'), 'plug'],
    security: [tr('tab.security'), tr('hp.tab.securityTip'), 'shield'],
    monlog: [tr('tab.monlog'), tr('tab.monlogTip'), 'activity'],
    advanced: [tr('tab.advanced'), tr('tab.advancedTip'), 'settings'],
    upgrade: [tr('upg.open'), tr('upg.t.open'), 'upload'],
    power: [tr('hp.tab.power'), tr('hp.tab.powerTip'), 'power'],
    conn: [tr('hp.tab.conn'), tr('hp.tab.connTip'), 'key'],
  });
  const IMPORT_TABS = () => ({
    vmimport: [tr('tab.vmimport'), tr('tab.vmimportTip'), 'download'],
    forklift: [tr('tab.forklift'), tr('tab.forkliftTip'), 'migrate'],
  });
  const POWER_TABS = () => ({
    startup: [tr('tab.startup'), tr('hp.power.startupTip'), 'play'],
    shutdown: [tr('tab.shutdown'), tr('hp.power.shutdownTip'), 'stop'],
  });
  const SUBS = () => [
    ['overview', tr('hnav.sub.overview'), tr('hnav.sub.overviewTip')],
    ['vms', tr('hnav.sub.vms'), tr('hnav.sub.vmsTip')],
    ['containers', tr('hnav.sub.containers'), tr('hnav.sub.containersTip')],
    ['imports', tr('hnav.sub.imports'), tr('hnav.sub.importsTip')],
    ['params', tr('hnav.sub.params'), tr('hnav.sub.paramsTip')],
    ['dr', tr('hnav.sub.dr'), tr('hnav.sub.drTip')],
  ];
  const STATE_BADGE = { ok: 'ok', unreachable: 'fail', error: 'fail', denied: 'warn', unknown: 'dim' };

  const href = (r) => window.Routes.format(Object.assign({ name: 'host', kind: 'harvester', host: route.host,
                                                            sub: null, tab: null, pane: null, id: null }, r));

  function hostInfo() {
    const d = window.HostNav && HostNav._data ? HostNav._data() : null;
    return ((d && d.hosts) || []).find((h) => h.kind === 'harvester' && h.name === route.host) || null;
  }

  function subHref(sub) {
    if (sub === 'overview') return href({});
    if (sub === 'imports') return href({ sub, tab: 'vmimport' });
    if (sub === 'params') return href({ sub, tab: 'nodes' });
    return href({ sub });
  }

  function tabBar(items, current, mk, label) {
    return `<div class="sub-tabs sub-tabs-inline hp-tabs" role="tablist" aria-label="${esc(label)}">${Object.entries(items).map(([id, [text, tip, ic]]) => {
      const on = id === current;
      return `<a class="sub-tab tip${on ? ' active' : ''}" role="tab" data-hp-tab="${esc(id)}" href="${esc(mk(id))}"
          aria-selected="${on ? 'true' : 'false'}" title="${esc(tip)}" data-tip="${esc(tip)}">
        <span class="tab-icon">${icon(ic)}</span> <span>${esc(text)}</span></a>`;
    }).join('')}</div>`;
  }

  function render() {
    const head = $('#host-page-head');
    if (!head) return;
    const on = !!(route && route.name === 'host' && route.kind === 'harvester' && route.host);
    head.hidden = !on;
    document.body.classList.toggle('host-page', on);
    if (!on) { head.innerHTML = ''; return; }
    const sub = route.sub || 'overview';
    const h = hostInfo();
    const st = (h && h.state) || 'unknown';
    const stateText = { ok: tr('hnav.state.ok'), unreachable: tr('hnav.state.unreachable'), denied: tr('hnav.state.denied'),
                        error: tr('hnav.state.error'), unknown: tr('hnav.state.unknown') }[st] || st;
    const meta = [];
    if (h && h.version) meta.push(esc(h.version));
    if (h && typeof h.vm_count === 'number') {
      // nombre repris d'une lecture déjà faite par un écran : dire son âge
      const age = typeof h.vm_count_age === 'number' ? h.vm_count_age : null;
      const tip = age === null ? '' : (age < 60 ? tr('hp.countAgeNow') : tr('hp.countAge', { n: Math.round(age / 60) }));
      meta.push(`<span class="hp-count${tip ? ' tip' : ''}"${tip ? ` title="${esc(tip)}" data-tip="${esc(tip)}"` : ''}>${esc(tr('hnav.tip.vms', { n: h.vm_count }))}</span>`);
    }
    let bar = '';
    if (sub === 'params') {
      bar = tabBar(PARAM_TABS(), route.tab, (id) => href({ sub: 'params', tab: id }), tr('hnav.sub.params'));
      if (route.tab === 'power') {
        bar += tabBar(POWER_TABS(), route.pane || 'startup', (id) => href({ sub: 'params', tab: 'power', pane: id }),
                      tr('hp.tab.power'));
      }
    } else if (sub === 'imports') {
      bar = tabBar(IMPORT_TABS(), route.tab, (id) => href({ sub: 'imports', tab: id }), tr('hnav.sub.imports'));
    }
    head.innerHTML = `
      <div class="hp-title">
        <span class="badge hp-family tip" data-tip="${esc(tr('hp.familyTip'))}" title="${esc(tr('hp.familyTip'))}">Harvester</span>
        <span class="hp-name">${esc(route.host)}</span>
        <span class="badge ${STATE_BADGE[st] || 'dim'} hp-state" title="${esc(h && h.error ? `${stateText}: ${h.error}` : stateText)}">${esc(stateText)}</span>
        ${meta.length ? `<span class="hp-meta">${meta.join(' &middot; ')}</span>` : ''}
        <nav class="hp-sub" aria-label="${esc(route.host)}">${SUBS().map(([id, text, tip]) =>
          `<a class="hp-sub-link${id === sub ? ' active' : ''}" data-hp-sub="${esc(id)}" href="${esc(subHref(id))}"
              title="${esc(tip)}"${id === sub ? ' aria-current="page"' : ''}>${esc(text)}</a>`).join('')}</nav>
        <a class="btn btn-sm btn-secondary hp-activity tip" href="${esc(window.Routes.format({ name: 'activity', kind: 'harvester', host: route.host }))}"
           title="${esc(tr('hp.activityTip'))}" data-tip="${esc(tr('hp.activityTip'))}">${icon('activity')} <span>${esc(tr('hp.activity'))}</span></a>
      </div>
      ${bar ? `<div class="hp-bars">${bar}</div>` : ''}`;
  }

  /** Le routeur a appliqué une adresse (ou l'écran a suivi un clic). */
  function show(r) {
    route = r || null;
    render();
  }

  // ---------------------------------------------------------------------
  // Paramètres > Connexion : origine, nœuds déclarés, tests, réglages
  // ---------------------------------------------------------------------
  async function renderConn(cluster) {
    const box = $('#hconn-host');
    if (!box || !cluster) return;
    box.innerHTML = `<p class="form-hint">${esc(tr('common.loading'))}</p>`;
    let entry = null;
    try {
      const d = await fetch('/api/clusters').then((r) => r.json());
      entry = (d.clusters || []).find((c) => c.name === cluster) || null;
    } catch (e) { /* lu plus bas */ }
    if (window.App && App.getCurrentCluster() !== cluster) return;     // un autre hôte depuis
    if (!entry) {
      box.innerHTML = `<p class="form-hint">${esc(tr('hp.conn.missing', { name: cluster }))}</p>`;
      return;
    }
    const origin = entry.origin === 'config' ? 'config' : 'console';
    const originLabel = origin === 'config' ? tr('clusters.origin.config') : tr('clusters.origin.console');
    const originTip = origin === 'config' ? tr('clusters.origin.configTip') : tr('clusters.origin.consoleTip');
    const nodes = entry.nodes || [];
    box.innerHTML = `
      <div class="card hp-conn">
        <div class="card-header"><h2>${esc(cluster)}
          <span class="badge cluster-origin tip" data-origin="${esc(origin)}" data-tip="${esc(originTip)}" title="${esc(originTip)}">${esc(originLabel)}</span></h2>
          <button type="button" class="btn btn-sm btn-secondary tip needs-admin" data-hp-conn="settings"
                  title="${esc(tr('hp.conn.settingsTip'))}" data-tip="${esc(tr('hp.conn.settingsTip'))}">${icon('settings')} ${esc(tr('hp.conn.settings'))}</button>
        </div>
        <div class="card-body">
          ${entry.description ? `<p class="form-hint">${esc(entry.description)}</p>` : ''}
          <h4>${esc(tr('hp.conn.nodes', { n: nodes.length }))}</h4>
          ${nodes.length ? `<table class="data-table hp-conn-nodes"><thead><tr>
              <th>${esc(tr('col.name'))}</th><th>IP</th><th>${esc(tr('col.roles'))}</th></tr></thead><tbody>
              ${nodes.map((n) => `<tr><td>${esc(n.hostname)}</td><td><code>${esc(n.ip)}</code></td><td>${esc(n.role)}</td></tr>`).join('')}
            </tbody></table>` : `<p class="form-hint">${esc(tr('hp.conn.noNodes'))}</p>`}
          <div class="apply-bar hp-conn-tests">
            <button type="button" class="btn btn-sm btn-secondary tip needs-admin" data-hp-conn="test-kc"
                    title="${esc(tr('hp.conn.testKcTip'))}" data-tip="${esc(tr('hp.conn.testKcTip'))}">${icon('test')} ${esc(tr('clusters.testKc'))}</button>
            <button type="button" class="btn btn-sm btn-secondary tip needs-admin" data-hp-conn="test-ssh"
                    title="${esc(tr('hp.conn.testSshTip'))}" data-tip="${esc(tr('hp.conn.testSshTip'))}">${icon('console')} ${esc(tr('clusters.testSsh'))}</button>
          </div>
          <div class="hp-conn-result" role="status" aria-live="polite"></div>
        </div>
      </div>`;
    box.dataset.cluster = cluster;
  }

  async function connAction(act, box) {
    const cluster = box.dataset.cluster;
    const out = box.querySelector('.hp-conn-result');
    if (act === 'settings') {
      // `Settings` est une constante globale, pas une propriété de window
      if (typeof Settings !== 'undefined') Settings.openTab('clusters');
      return;
    }
    if (!cluster || !out) return;
    out.innerHTML = `<p class="form-hint">${esc(tr('common.loading'))}</p>`;
    try {
      const r = await fetch(`/api/clusters/${enc(cluster)}/${act === 'test-kc' ? 'test-kubeconfig' : 'test-ssh'}`, { method: 'POST' });
      const d = await r.json().catch(() => ({}));
      if (!r.ok && !('ok' in d) && !d.results) throw new Error(d.hint || d.error || `HTTP ${r.status}`);
      if (act === 'test-kc') {
        out.innerHTML = d.ok
          ? `<div class="summary-bar ok">${icon('ok')} ${esc(tr('hp.conn.kcOk', { version: d.server_version || '?' }))}</div>`
          : `<div class="summary-bar bad">${icon('fail')} ${esc(d.error || 'failed')}</div>`;
        return;
      }
      const rows = (d.results || []).map((n) => `<tr><td>${esc(n.hostname)}</td><td><code>${esc(n.ip)}</code></td>
          <td>${n.ok ? `<span class="badge ok">${esc(tr('hp.conn.sshOk'))}</span>`
                     : `<span class="badge fail">${esc(String(n.detail || 'failed').slice(0, 120))}</span>`}</td></tr>`).join('');
      out.innerHTML = rows ? `<table class="data-table hp-conn-ssh"><tbody>${rows}</tbody></table>`
                           : `<p class="form-hint">${esc(tr('hp.conn.noNodes'))}</p>`;
    } catch (e) {
      out.innerHTML = `<div class="summary-bar bad">${icon('fail')} ${esc(e.message)}</div>`;
    }
  }

  function init() {
    document.addEventListener('click', (e) => {
      const b = e.target.closest('[data-hp-conn]');
      if (b) { e.preventDefault(); connAction(b.dataset.hpConn, b.closest('#hconn-host')); return; }
      const join = e.target.closest('#btn-join-node');
      if (join) {
        e.preventDefault();
        const c = window.App && App.getCurrentCluster();
        if (c && window.BMC && BMC.openJoin) BMC.openJoin(c);
      }
    });
  }

  document.addEventListener('DOMContentLoaded', init);
  return { show, render, renderConn };
})();
window.HostPage = HostPage;
