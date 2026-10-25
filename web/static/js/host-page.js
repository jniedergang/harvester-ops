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
 *
 * Hôte KVM (tâche 7) : même bandeau, sous-menu Vue d'ensemble, Machines
 * virtuelles, Paramètres (Système, Pools, Réseaux, Étiquettes, Connexion) et
 * Disaster Recovery (état vide : la réplication des domaines KVM n'existe pas
 * encore). Les vues KVM existantes sont montées avec l'hôte imposé ;
 * Système lit /api/kvm/hosts/<h>, Étiquettes et Connexion la déclaration
 * seule (lisibles quand l'hôte ne répond pas). Les étiquettes se modifient
 * (administrateur) en action suivie, `harvester-kvm.py host label`.
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
  const KVM_TABS = () => ({
    system: [tr('hp.kvm.tab.system'), tr('hp.kvm.tab.systemTip'), 'compute'],
    pools: [tr('hp.kvm.tab.pools'), tr('hp.kvm.tab.poolsTip'), 'storage'],
    networks: [tr('hp.kvm.tab.networks'), tr('hp.kvm.tab.networksTip'), 'network'],
    labels: [tr('hp.kvm.tab.labels'), tr('hp.kvm.tab.labelsTip'), 'doc'],
    conn: [tr('hp.tab.conn'), tr('hp.kvm.tab.connTip'), 'key'],
  });
  // 2.0.0 : onglets de la famille KVM (#hosts/kvm) : le tableau des hôtes et
  // les vues de tous les hôtes KVM de la 1.96-1.100 (tuiles de capacité côte
  // à côte, pools et images, réseaux), chaque bloc menant à la page d'hôte
  const KVM_FLEET_TABS = () => ({
    hosts: [tr('hp.kvmFleet.hosts'), tr('hp.kvmFleet.hostsTip'), 'node'],
    capacity: [tr('hp.kvmFleet.capacity'), tr('hp.kvmFleet.capacityTip'), 'compute'],
    pools: [tr('hp.kvm.tab.pools'), tr('hp.kvmFleet.poolsTip'), 'storage'],
    networks: [tr('hp.kvm.tab.networks'), tr('hp.kvmFleet.networksTip'), 'network'],
  });
  const fleetHref = (id) => window.Routes.format({ name: 'hosts', kind: 'kvm', tab: id === 'hosts' ? null : id });
  const KVM_SUBS = () => [
    ['overview', tr('hnav.sub.overview'), tr('hnav.sub.overviewKvmTip')],
    ['vms', tr('hnav.sub.vms'), tr('hnav.sub.vmsKvmTip')],
    ['params', tr('hnav.sub.params'), tr('hnav.sub.paramsKvmTip')],
    ['dr', tr('hnav.sub.dr'), tr('hnav.sub.drKvmTip')],
  ];
  const STATE_BADGE = { ok: 'ok', unreachable: 'fail', error: 'fail', denied: 'warn', unknown: 'dim' };

  const kindOf = () => (route && route.kind === 'kvm' ? 'kvm' : 'harvester');
  const href = (r) => window.Routes.format(Object.assign({ name: 'host', kind: kindOf(), host: route.host,
                                                            sub: null, tab: null, pane: null, id: null }, r));

  function hostInfo() {
    const d = window.HostNav && HostNav._data ? HostNav._data() : null;
    return ((d && d.hosts) || []).find((h) => h.kind === kindOf() && h.name === route.host) || null;
  }

  function subHref(sub) {
    if (sub === 'overview') return href({});
    if (sub === 'imports') return href({ sub, tab: 'vmimport' });
    if (sub === 'params') return href({ sub, tab: kindOf() === 'kvm' ? 'system' : 'nodes' });
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
    const on = !!(route && route.name === 'host' && route.host);
    const fleet = !!(route && route.name === 'hosts' && route.kind === 'kvm');
    head.hidden = !on && !fleet;
    document.body.classList.toggle('host-page', on);
    document.body.classList.toggle('host-page-kvm', on && kindOf() === 'kvm');
    if (fleet) {
      head.innerHTML = `<div class="hp-bars hp-kvm-fleet">${tabBar(KVM_FLEET_TABS(), route.tab || 'hosts', fleetHref, tr('hp.kvmFleet.label'))}</div>`;
      return;
    }
    if (!on) { head.innerHTML = ''; return; }
    const kvm = kindOf() === 'kvm';
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
    if (kvm && sub === 'params') {
      bar = tabBar(KVM_TABS(), route.tab, (id) => href({ sub: 'params', tab: id }), tr('hnav.sub.params'));
    } else if (kvm) {
      bar = '';
    } else if (sub === 'params') {
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
        <span class="badge hp-family tip" data-tip="${esc(kvm ? tr('hp.kvm.familyTip') : tr('hp.familyTip'))}" title="${esc(kvm ? tr('hp.kvm.familyTip') : tr('hp.familyTip'))}">${kvm ? 'KVM' : 'Harvester'}</span>
        <span class="hp-name">${esc(route.host)}</span>
        <span class="badge ${STATE_BADGE[st] || 'dim'} hp-state" title="${esc(h && h.error ? `${stateText}: ${h.error}` : stateText)}">${esc(stateText)}</span>
        ${meta.length ? `<span class="hp-meta">${meta.join(' &middot; ')}</span>` : ''}
        <nav class="hp-sub" aria-label="${esc(route.host)}">${(kvm ? KVM_SUBS() : SUBS()).map(([id, text, tip]) =>
          `<a class="hp-sub-link${id === sub ? ' active' : ''}" data-hp-sub="${esc(id)}" href="${esc(subHref(id))}"
              title="${esc(tip)}"${id === sub ? ' aria-current="page"' : ''}>${esc(text)}</a>`).join('')}</nav>
        <a class="btn btn-sm btn-secondary hp-activity tip" href="${esc(window.Routes.format({ name: 'activity', kind: kindOf(), host: route.host }))}"
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

  // ---------------------------------------------------------------------
  // Hôte KVM : Paramètres > Système, Étiquettes, Connexion
  // ---------------------------------------------------------------------
  const kib = (k) => {
    if (typeof k !== 'number' || !Number.isFinite(k)) return '–';
    const u = ['KiB', 'MiB', 'GiB', 'TiB'];
    let i = 0, v = k;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i += 1; }
    return `${v.toFixed(i ? 1 : 0)} ${u[i]}`;
  };
  const row = (label, value, tip) => `<tr><th scope="row"${tip ? ` class="tip" data-tip="${esc(tip)}" title="${esc(tip)}"` : ''}>${esc(label)}</th><td>${value}</td></tr>`;
  const errLine = (e) => `<div class="summary-bar bad hp-kvm-error" data-kind="${esc(e.kind || 'failed')}">${icon('fail')}
      <strong>${esc(window.KVM && KVM.kindLabel ? KVM.kindLabel(e.kind) : (e.kind || 'failed'))}</strong> ${esc(e.message || '')}</div>`;
  let kvmSeq = 0;

  async function getJson(url) {
    try {
      const r = await fetch(url);
      const d = await r.json().catch(() => null);
      if (!d) return { error: { kind: 'failed', message: `HTTP ${r.status}` }, status: r.status };
      return Object.assign({ status: r.status }, d);
    } catch (e) { return { error: { kind: 'failed', message: e.message } }; }
  }

  /** Monte un onglet de Paramètres d'un hôte KVM (system, labels, conn). */
  async function renderKvmParams(host, tab) {
    const box = $('#kvmparams-host');
    if (!box || !host) return;
    const seq = ++kvmSeq;
    box.dataset.host = host;
    box.dataset.tab = tab;
    box.innerHTML = `<p class="form-hint">${esc(tr('common.loading'))}</p>`;
    const url = tab === 'system' ? `/api/kvm/hosts/${enc(host)}` : `/api/kvm/hosts/${enc(host)}/declaration`;
    const d = await getJson(url);
    if (seq !== kvmSeq) return;                         // un autre onglet depuis
    if (tab === 'system') box.innerHTML = systemHtml(host, d);
    else if (d.status === 404 || (d.error && !d.name)) {
      box.innerHTML = errLine(typeof d.error === 'object' ? d.error : { kind: 'failed', message: d.error || '' });
    } else if (tab === 'labels') box.innerHTML = labelsHtml(host, d);
    else box.innerHTML = connHtml(host, d);
    box._decl = d;
  }

  function systemHtml(host, d) {
    const cpu = d.cpu || {}, mem = d.memory || {};
    const err = d.error ? (typeof d.error === 'object' ? d.error : { kind: 'failed', message: String(d.error) }) : null;
    const facts = [
      row(tr('hp.kvm.sys.uri'), `<code>${esc(d.uri || '–')}</code>`, tr('hp.kvm.sys.uriTip')),
    ];
    if (!err) {
      facts.unshift(row(tr('hp.kvm.sys.hostname'), esc(d.hostname || host)));
      facts.push(
        row('libvirt', esc(d.libvirt_version || '?')),
        row('QEMU', esc(d.qemu_version || '?')),
        row(tr('hp.kvm.sys.cpu'), `${esc(tr('kvm.cpus', { n: cpu.cpus == null ? '?' : cpu.cpus }))}${cpu.cpu_model ? ` <span class="res-dim">${esc(cpu.cpu_model)}${cpu.mhz ? ` · ${esc(cpu.mhz)} MHz` : ''}${cpu.sockets ? ` · ${esc(tr('kvm.sockets', { n: cpu.sockets }))}` : ''}</span>` : ''}`),
        row(tr('hp.kvm.sys.memory'), esc(tr('kvm.memory', { total: kib(mem.total_kib), free: kib(mem.free_kib), available: kib(mem.available_kib) }))),
        row(tr('hp.kvm.sys.vms'), esc(tr('kvm.vmCount', { n: (d.vms || []).length }))));
    }
    const partly = (d.errors || []).map((e) => `<li><code>${esc(e.command)}</code> ${esc(e.message)}</li>`).join('');
    return `<div class="card hp-kvm-system">
        <div class="card-header"><h2>${esc(tr('hp.kvm.tab.system'))} · ${esc(host)}</h2></div>
        <div class="card-body">
          ${err ? `${errLine(err)}<p class="form-hint">${esc(tr('hp.kvm.sys.declOnly'))}</p>` : ''}
          <table class="data-table hp-kvm-facts"><tbody>${facts.join('')}</tbody></table>
          ${partly ? `<h4>${esc(tr('hp.kvm.sys.partly'))}</h4><ul class="hp-kvm-partly">${partly}</ul>` : ''}
        </div></div>`;
  }

  const ROLE_RANK = { viewer: 1, operator: 2, admin: 3 };
  const hasRole = (d, need) => (ROLE_RANK[d.role] || 0) >= ROLE_RANK[need];

  function labelsHtml(host, d) {
    const labels = Object.entries(d.labels || {}).sort((a, b) => a[0].localeCompare(b[0]));
    const admin = hasRole(d, 'admin');
    const tip = admin ? tr('hp.kvm.labels.editTip') : tr('kvm.adminOnly');
    return `<div class="card hp-kvm-labels">
        <div class="card-header"><h2>${esc(tr('hp.kvm.tab.labels'))} · ${esc(host)}</h2>
          <button type="button" class="btn btn-sm btn-secondary tip" data-hp-kvm="edit-labels" ${admin ? '' : 'disabled aria-disabled="true"'}
                  title="${esc(tip)}" data-tip="${esc(tip)}">${icon('edit')} ${esc(tr('hp.kvm.labels.edit'))}</button></div>
        <div class="card-body">
          <p class="form-hint">${esc(tr('hp.kvm.labels.hint'))}</p>
          ${labels.length ? `<table class="data-table hp-kvm-labels-table"><thead><tr><th>${esc(tr('hp.kvm.labels.key'))}</th><th>${esc(tr('hp.kvm.labels.value'))}</th></tr></thead>
            <tbody>${labels.map(([k, v]) => `<tr><td><code>${esc(k)}</code></td><td>${esc(v)}</td></tr>`).join('')}</tbody></table>`
            : `<p class="form-hint hp-kvm-nolabel">${esc(tr('hp.kvm.labels.none'))}</p>`}
          <div class="hp-kvm-result" role="status" aria-live="polite"></div>
        </div></div>`;
  }

  function connHtml(host, d) {
    const op = hasRole(d, 'operator');
    const admin = hasRole(d, 'admin');
    const fps = (d.fingerprints || []).map((f) => `<li><strong>${esc(f.type)}</strong> <code>${esc(f.fingerprint)}</code></li>`).join('');
    const testTip = op ? tr('kvm.testTip') : tr('kvm.operatorOnly');
    const rmTip = admin ? tr('kvm.removeTip') : tr('kvm.adminOnly');
    return `<div class="card hp-kvm-conn">
        <div class="card-header"><h2>${esc(tr('hp.tab.conn'))} · ${esc(host)}</h2></div>
        <div class="card-body">
          <table class="data-table hp-kvm-facts"><tbody>
            ${row(tr('kvm.dc.address'), `<code>${esc(d.address || '–')}</code>`)}
            ${row(tr('kvm.dc.port'), esc(d.port || 22))}
            ${row(tr('kvm.dc.user'), `<code>${esc(d.user || '–')}</code>`)}
            ${row(tr('hp.kvm.sys.uri'), `<code>${esc(d.uri || '–')}</code>`, tr('hp.kvm.sys.uriTip'))}
            ${row(tr('hp.kvm.conn.fps'), fps ? `<ul class="hp-kvm-fps">${fps}</ul>` : `<span class="res-dim">${esc(tr('hp.kvm.conn.noFp'))}</span>`, tr('hp.kvm.conn.fpsTip'))}
          </tbody></table>
          <div class="apply-bar hp-kvm-actions">
            <button type="button" class="btn btn-sm btn-secondary tip" data-hp-kvm="test" ${op ? '' : 'disabled aria-disabled="true"'}
                    title="${esc(testTip)}" data-tip="${esc(testTip)}">${icon('test')} ${esc(tr('kvm.test'))}</button>
            <button type="button" class="btn btn-sm btn-secondary tip" data-hp-kvm="remove" ${admin ? '' : 'disabled aria-disabled="true"'}
                    title="${esc(rmTip)}" data-tip="${esc(rmTip)}">${icon('trash')} ${esc(tr('kvm.remove'))}</button>
          </div>
          <div class="hp-kvm-result" role="status" aria-live="polite"></div>
        </div></div>`;
  }

  function kvmAction(act, btn) {
    const box = btn.closest('#kvmparams-host');
    if (!box) return;
    const host = box.dataset.host;
    const out = box.querySelector('.hp-kvm-result');
    if (act === 'test') { if (window.KVM) KVM.testHost(host, out); return; }
    if (act === 'edit-labels') { openLabels(host, box._decl || {}); return; }
    if (act === 'remove') {
      // confirmation dans la page, jamais confirm()
      out.innerHTML = `<div class="bkg-confirm kvm-confirm" role="group" aria-label="${esc(tr('kvm.removeConfirm', { name: host }))}">
          <span>${esc(tr('kvm.removeConfirm', { name: host }))}</span>
          <button type="button" class="btn btn-sm btn-danger tip" data-hp-kvm="remove-yes" title="${esc(tr('kvm.removeYesTip'))}" data-tip="${esc(tr('kvm.removeYesTip'))}">${icon('trash')} ${esc(tr('kvm.removeYes'))}</button>
          <button type="button" class="btn btn-sm btn-secondary tip" data-hp-kvm="remove-no" title="${esc(tr('kvm.confirmNoTip'))}" data-tip="${esc(tr('kvm.confirmNoTip'))}">${esc(tr('kvm.confirmNo'))}</button></div>`;
      out.querySelector('[data-hp-kvm="remove-no"]').focus();
      return;
    }
    if (act === 'remove-no') { out.innerHTML = ''; return; }
    if (act === 'remove-yes') {
      out.querySelectorAll('button').forEach((b) => { b.disabled = true; });
      if (window.KVM) {
        KVM.removeHost(host, out, (ok) => {
          if (!ok) return;
          if (window.HostNav && HostNav.load) HostNav.load();
          // l'hôte n'existe plus : l'index des hôtes KVM
          if (window.Router) setTimeout(() => Router.go('#hosts/kvm'), 900);
        });
      }
    }
  }

  // -- fenêtre « Modifier les étiquettes » (administrateur) ---------------------
  const LABEL_KEY_RE = /^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,62}$/;
  const LABELS_MAX = 32;
  const LABEL_VALUE_MAX = 253;

  /** Contrôles avant d'agir (purs) : [{ ok, text }] et les changements. */
  function labelChecks(rows, before) {
    const checks = [];
    const keys = rows.map((r) => r.k);
    const bad = rows.filter((r) => !LABEL_KEY_RE.test(r.k));
    checks.push({ ok: !bad.length, text: bad.length ? tr('hp.kvm.labels.chkBadKey', { keys: bad.map((r) => r.k || '""').join(', ') }) : tr('hp.kvm.labels.chkKeys') });
    const dup = keys.filter((k, i) => keys.indexOf(k) !== i);
    checks.push({ ok: !dup.length, text: dup.length ? tr('hp.kvm.labels.chkDup', { keys: [...new Set(dup)].join(', ') }) : tr('hp.kvm.labels.chkNoDup') });
    // eslint-disable-next-line no-control-regex
    const badV = rows.filter((r) => r.v.length > LABEL_VALUE_MAX || /[\u0000-\u001f\u007f]/.test(r.v));
    checks.push({ ok: !badV.length, text: badV.length ? tr('hp.kvm.labels.chkBadValue', { keys: badV.map((r) => r.k).join(', '), max: LABEL_VALUE_MAX }) : tr('hp.kvm.labels.chkValues') });
    checks.push({ ok: rows.length <= LABELS_MAX, text: tr('hp.kvm.labels.chkCount', { n: rows.length, max: LABELS_MAX }) });
    const after = {};
    rows.forEach((r) => { after[r.k] = r.v; });
    const added = Object.keys(after).filter((k) => !(k in before));
    const changed = Object.keys(after).filter((k) => k in before && before[k] !== after[k]);
    const removed = Object.keys(before).filter((k) => !(k in after));
    return { checks, labels: after, added, changed, removed, ok: checks.every((c) => c.ok) };
  }

  let labelsModal = null;
  function closeLabels() {
    if (!labelsModal) return;
    const { el, prev } = labelsModal;
    el.remove();
    labelsModal = null;
    if (prev && prev.focus) { try { prev.focus(); } catch (e) { /* parti */ } }
  }

  function openLabels(host, decl) {
    closeLabels();
    const before = Object.assign({}, decl.labels || {});
    const el = document.createElement('div');
    el.className = 'modal-overlay active hp-labels-modal';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-modal', 'true');
    el.setAttribute('aria-labelledby', 'hp-labels-title');
    el.innerHTML = `<div class="modal">
        <div class="modal-header"><div><h3 id="hp-labels-title">${esc(tr('hp.kvm.labels.title', { name: host }))}</h3>
          <div class="modal-subtitle">${esc(tr('hp.kvm.labels.subtitle'))}</div></div>
          <button type="button" class="btn-close tip" data-lbl="close" title="${esc(tr('common.close'))}" data-tip="${esc(tr('common.close'))}" aria-label="${esc(tr('common.close'))}">×</button></div>
        <div class="modal-body">
          <table class="data-table hp-labels-edit"><thead><tr><th>${esc(tr('hp.kvm.labels.key'))}</th><th>${esc(tr('hp.kvm.labels.value'))}</th><th></th></tr></thead><tbody></tbody></table>
          <button type="button" class="btn btn-sm btn-secondary tip" data-lbl="add" title="${esc(tr('hp.kvm.labels.addTip'))}" data-tip="${esc(tr('hp.kvm.labels.addTip'))}">${icon('add')} ${esc(tr('hp.kvm.labels.add'))}</button>
          <h4>${esc(tr('hp.kvm.labels.checks'))}</h4>
          <ul class="kvm-checks hp-labels-checks"></ul>
          <p class="form-hint hp-labels-diff"></p>
          <div class="of-msg hp-labels-msg" role="status" aria-live="polite"></div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-sm btn-secondary tip" data-lbl="close" title="${esc(tr('hp.kvm.labels.cancelTip'))}" data-tip="${esc(tr('hp.kvm.labels.cancelTip'))}">${esc(tr('common.cancel'))}</button>
          <button type="button" class="btn btn-sm btn-primary tip" data-lbl="apply" title="${esc(tr('hp.kvm.labels.applyTip'))}" data-tip="${esc(tr('hp.kvm.labels.applyTip'))}">${icon('ok')} ${esc(tr('common.apply'))}</button>
        </div></div>`;
    document.body.appendChild(el);
    labelsModal = { el, prev: document.activeElement };
    // même piège à focus que la fenêtre des réglages : Tab reste dedans
    if (typeof Settings !== 'undefined' && Settings.trapFocus) Settings.trapFocus(el);
    const body = el.querySelector('tbody');
    const addRow = (k, v) => {
      const tr0 = document.createElement('tr');
      tr0.innerHTML = `<td><input type="text" class="tip" data-lbl-k maxlength="63" value="${esc(k)}" title="${esc(tr('hp.kvm.labels.keyTip'))}" aria-label="${esc(tr('hp.kvm.labels.key'))}"></td>
        <td><input type="text" class="tip" data-lbl-v maxlength="${LABEL_VALUE_MAX + 1}" value="${esc(v)}" title="${esc(tr('hp.kvm.labels.valueTip'))}" aria-label="${esc(tr('hp.kvm.labels.value'))}"></td>
        <td><button type="button" class="btn-icon-action tip" data-lbl="del" title="${esc(tr('hp.kvm.labels.delTip'))}" aria-label="${esc(tr('hp.kvm.labels.delTip'))}">${icon('trash', 13)}</button></td>`;
      body.appendChild(tr0);
    };
    Object.entries(before).sort((a, b) => a[0].localeCompare(b[0])).forEach(([k, v]) => addRow(k, v));
    const rows = () => [...body.querySelectorAll('tr')].map((r) => ({ k: r.querySelector('[data-lbl-k]').value.trim(), v: r.querySelector('[data-lbl-v]').value }));
    const apply = el.querySelector('[data-lbl="apply"]');
    const paint = () => {
      const res = labelChecks(rows(), before);
      el.querySelector('.hp-labels-checks').innerHTML = res.checks.map((c) =>
        `<li class="${c.ok ? 'ok' : 'bad'}"><span class="badge ${c.ok ? 'ok' : 'fail'}">${icon(c.ok ? 'ok' : 'fail', 12)}</span> ${esc(c.text)}</li>`).join('');
      const nothing = !res.added.length && !res.changed.length && !res.removed.length;
      el.querySelector('.hp-labels-diff').textContent = nothing ? tr('hp.kvm.labels.noChange')
        : tr('hp.kvm.labels.diff', { added: res.added.join(', ') || '–', changed: res.changed.join(', ') || '–', removed: res.removed.join(', ') || '–' });
      apply.disabled = !res.ok || nothing;
      const why = !res.ok ? tr('hp.kvm.labels.fixFirst') : nothing ? tr('hp.kvm.labels.noChange') : tr('hp.kvm.labels.applyTip');
      apply.title = why;
      apply.dataset.tip = why;
      return res;
    };
    el.addEventListener('input', paint);
    el.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeLabels(); });
    el.addEventListener('click', async (e) => {
      if (e.target === el) { closeLabels(); return; }
      const b = e.target.closest('[data-lbl]');
      if (!b) return;
      const act = b.dataset.lbl;
      if (act === 'close') { closeLabels(); return; }
      if (act === 'add') { addRow('', ''); paint(); body.querySelector('tr:last-child [data-lbl-k]').focus(); return; }
      if (act === 'del') { b.closest('tr').remove(); paint(); return; }
      if (act !== 'apply') return;
      const res = paint();
      if (!res.ok || apply.disabled) return;
      apply.disabled = true;
      const msg = el.querySelector('.hp-labels-msg');
      msg.textContent = tr('hp.kvm.labels.sending');
      try {
        const r = await fetch(`/api/kvm/hosts/${enc(host)}/labels`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ labels: res.labels }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.hint || d.error || `HTTP ${r.status}`);
        const follow = window.KVM && KVM.follow;
        if (!follow) return;
        follow(d.action_id, (st) => { if (st.message) msg.textContent = st.message; }, (ok, run) => {
          if (!ok) {
            msg.innerHTML = `<span class="res-error">${icon('fail')} ${esc(run.error_summary || tr('hp.kvm.labels.failed'))}</span>`;
            apply.disabled = false;
            return;
          }
          closeLabels();
          if (window.HostNav && HostNav.load) HostNav.load();
          if (route && route.kind === 'kvm' && route.host === host && route.tab === 'labels') {
            renderKvmParams(host, 'labels').then(() => {
              const out = $('#kvmparams-host .hp-kvm-result');
              if (out) out.innerHTML = `<span>${icon('ok')} ${esc(tr('hp.kvm.labels.done', { id: d.action_id }))}</span>`;
            });
          }
        });
      } catch (err) {
        msg.innerHTML = `<span class="res-error">${icon('fail')} ${esc(err.message)}</span>`;
        apply.disabled = false;
      }
    });
    paint();
    (body.querySelector('[data-lbl-k]') || el.querySelector('[data-lbl="add"]')).focus();
  }

  function init() {
    document.addEventListener('click', (e) => {
      const k = e.target.closest('[data-hp-kvm]');
      if (k && !k.disabled && k.closest('#kvmparams-host')) { e.preventDefault(); kvmAction(k.dataset.hpKvm, k); return; }
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
  return { show, render, renderConn, renderKvmParams, openLabels, labelChecks };
})();
window.HostPage = HostPage;
