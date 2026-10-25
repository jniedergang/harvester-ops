/**
 * harvester-ops : le tableau de bord de Harvester dans l'aperçu (v1.62.0)
 *
 * EventsBoard : le sous-onglet « Événements », les événements du cluster
 * rangés comme dans Harvester (hôtes, VMs, volumes, images), avec un filtre
 * « avertissements seulement » et une recherche ; relu toutes les 20 s tant
 * qu'il est affiché.
 * ClusterUsage : les jauges d'usage réel du sous-onglet Métriques (CPU et
 * mémoire lus par metrics.k8s.io, réservé par les demandes des pods, stockage
 * Longhorn utilisé et promis aux volumes).
 */
(() => {
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const enc = encodeURIComponent;
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 14) => (window.Icons ? Icons.svg(n, { size }) : '');
  const GROUPS = ['all', 'hosts', 'vms', 'volumes', 'images', 'dr'];
  const GROUP_LABEL = {
    all: () => tr('ev.all'), hosts: () => tr('ev.hosts'), vms: () => tr('ev.vms'),
    volumes: () => tr('ev.volumes'), images: () => tr('ev.images'),
    // alertes de réplication de ce cluster (Disaster Recovery)
    dr: () => tr('tab.dr'),
  };
  const GROUP_ICON = { all: 'activity', hosts: 'node', vms: 'vm', volumes: 'volume', images: 'cdrom', dr: 'shield' };

  function ago(iso) {
    const t = Date.parse(iso || '');
    if (!t) return '-';
    const s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 90) return tr('ev.ago.s', { n: s });
    if (s < 5400) return tr('ev.ago.m', { n: Math.round(s / 60) });
    if (s < 172800) return tr('ev.ago.h', { n: Math.round(s / 3600) });
    return tr('ev.ago.d', { n: Math.round(s / 86400) });
  }

  const board = { cluster: null, timer: null, data: null, group: 'all', warn: false, q: '' };

  function host() { return document.querySelector('[data-board="events"] .topology-host'); }

  function shell(h) {
    try { board.group = localStorage.getItem('harvester_ops_events_group') || 'all'; } catch { /* stockage indisponible */ }
    if (!GROUPS.includes(board.group)) board.group = 'all';
    h.innerHTML = `<div class="ev-board">
      <div class="bk-bar">
        <div class="sub-tabs sub-tabs-inline ev-tabs" role="tablist">
          ${GROUPS.map(g => `<button type="button" class="sub-tab tip" role="tab" data-ev-group="${g}"
            data-tip="${esc(tr('ev.tip.group'))}">${icon(GROUP_ICON[g])} <span>${esc(GROUP_LABEL[g]())}</span>
            <span class="ev-count" data-ev-count="${g}"></span></button>`).join('')}
        </div>
        <div class="ev-filters">
          <input type="search" class="tip" data-ev="q" placeholder="${esc(tr('ev.search'))}" data-tip="${esc(tr('ev.tip.search'))}">
          <label class="bk-check tip" data-tip="${esc(tr('ev.tip.warn'))}"><input type="checkbox" data-ev="warn"> <span>${esc(tr('ev.warnOnly'))}</span></label>
        </div>
      </div>
      <div class="ev-list" data-ev="list"><p class="hint">${esc(tr('hs.loading'))}</p></div>
    </div>`;
    h.querySelectorAll('[data-ev-group]').forEach(b => b.addEventListener('click', () => {
      board.group = b.dataset.evGroup;
      try { localStorage.setItem('harvester_ops_events_group', board.group); } catch { /* stockage indisponible */ }
      render();
    }));
    h.querySelector('[data-ev="warn"]').addEventListener('change', (e) => { board.warn = e.target.checked; render(); });
    h.querySelector('[data-ev="q"]').addEventListener('input', (e) => { board.q = e.target.value.trim().toLowerCase(); render(); });
  }

  function render() {
    const h = host();
    if (!h || !board.data) return;
    const d = board.data;
    h.querySelectorAll('[data-ev-group]').forEach(b => {
      const on = b.dataset.evGroup === board.group;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    GROUPS.forEach(g => {
      const el = h.querySelector(`[data-ev-count="${g}"]`);
      const n = g === 'all' ? d.items.length : (d.counts[g] || 0);
      const w = g === 'all' ? Object.values(d.warnings).reduce((a, b) => a + b, 0) : (d.warnings[g] || 0);
      el.innerHTML = `${n}${w ? ` <span class="ev-warn tip" data-tip="${esc(tr('ev.tip.warnCount'))}">${icon('warn', 11)}${w}</span>` : ''}`;
    });
    const rows = d.items.filter(r => (board.group === 'all' || r.group === board.group)
      && (!board.warn || r.type === 'Warning')
      && (!board.q || `${r.reason} ${r.name} ${r.namespace} ${r.message}`.toLowerCase().includes(board.q)));
    h.querySelector('[data-ev="list"]').innerHTML = rows.length ? `<table class="data-table ev-table"><thead><tr>
        <th>${esc(tr('ev.col.when'))}</th><th>${esc(tr('ev.col.type'))}</th><th>${esc(tr('ev.col.reason'))}</th>
        <th>${esc(tr('ev.col.object'))}</th><th>${esc(tr('ev.col.message'))}</th><th class="num">${esc(tr('ev.col.count'))}</th></tr></thead>
      <tbody>${rows.slice(0, 300).map(r => `<tr class="${r.type === 'Warning' ? 'ev-row-warn' : ''}">
        <td class="tip" data-tip="${esc(r.last)}">${esc(ago(r.last))}</td>
        <td>${r.type === 'Warning' ? `<span class="badge warn">${esc(tr('ev.warning'))}</span>` : `<span class="badge">${esc(tr('ev.normal'))}</span>`}</td>
        <td>${esc(r.reason)}</td>
        <td><span class="res-dim">${esc(r.kind)}</span> ${esc(r.namespace ? `${r.namespace}/${r.name}` : r.name)}</td>
        <td class="ev-msg">${esc(r.message)}</td><td class="num">${esc(r.count)}</td></tr>`).join('')}</tbody></table>`
      : `<p class="hint">${esc(tr('ev.none'))}</p>`;
  }

  async function load() {
    const cluster = board.cluster;
    try {
      const drp = window.Replication ? Replication.eventRows(cluster).catch(() => []) : Promise.resolve([]);
      const r = await fetch(`/api/events/${enc(cluster)}`);
      const d = await r.json();
      const dr = await drp;
      if (cluster !== board.cluster) return;
      if (r.ok && !d.unreachable && dr.length) {
        // les alertes de réplication rejoignent la liste, groupe « dr »
        d.items = dr.concat(d.items || []);
        d.counts = Object.assign({}, d.counts, { dr: dr.length });
        d.warnings = Object.assign({}, d.warnings, { dr: dr.length });
      }
      if ((!r.ok || d.unreachable) && dr.length) {
        // cluster injoignable : ses alertes de réplication comptent d'autant plus
        board.data = { items: dr, counts: { dr: dr.length }, warnings: { dr: dr.length } };
        render();
        const l = host() && host().querySelector('[data-ev="list"]');
        if (l) l.insertAdjacentHTML('afterbegin', `<p class="hint warn">${esc(d.unreachable ? tr('fabric.unreachable') : (d.error || `HTTP ${r.status}`))}</p>`);
        return;
      }
      if (!r.ok || d.unreachable) {
        const l = host() && host().querySelector('[data-ev="list"]');
        if (l) l.innerHTML = `<p class="hint warn">${esc(d.unreachable ? tr('fabric.unreachable') : (d.error || `HTTP ${r.status}`))}</p>`;
        return;
      }
      board.data = d;
      render();
    } catch { /* réseau : on retentera au prochain tour */ }
  }

  window.EventsBoard = {
    start(cluster) {
      const h = host();
      if (!h) return null;
      this.stop();
      board.cluster = cluster;
      board.data = null;
      shell(h);
      board.timer = setInterval(() => { if (!document.hidden) load(); }, 20000);
      return load();
    },
    stop() {
      if (board.timer) clearInterval(board.timer);
      board.timer = null;
    },
  };

  // -- les jauges d'usage ---------------------------------------------------------
  function bytes(b) {
    const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
    let i = 0, v = Number(b) || 0;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${u[i]}`;
  }
  const cores = (c) => `${Math.round((Number(c) || 0) * 10) / 10}`;

  function gauge(title, tip, used, reserved, total, fmt, extra) {
    const pct = (x) => (total ? Math.min(100, Math.round((x / total) * 100)) : 0);
    return `<div class="usage-gauge tip" data-tip="${esc(tip)}">
      <div class="usage-head"><strong>${esc(title)}</strong><span>${esc(pct(used))} %</span></div>
      <div class="usage-bar"><span class="usage-reserved" style="width:${pct(reserved)}%"></span><span class="usage-used" style="width:${pct(used)}%"></span></div>
      <div class="usage-legend"><span>${esc(tr('ev.used'))} ${esc(fmt(used))}</span>${reserved != null ? `<span>${esc(tr('ev.reserved'))} ${esc(fmt(reserved))}</span>` : ''}<span>${esc(tr('ev.total'))} ${esc(fmt(total))}</span></div>
      ${extra ? `<div class="usage-legend res-dim">${extra}</div>` : ''}</div>`;
  }

  async function renderUsage(cluster) {
    const box = document.getElementById('overview-usage');
    if (!box || !cluster) return;
    try {
      const r = await fetch(`/api/usage/${enc(cluster)}`);
      const d = await r.json();
      if (!r.ok || d.unreachable || !d.cpu) { box.hidden = true; return; }
      box.hidden = false;
      const s = d.storage;
      box.querySelector('[data-usage="body"]').innerHTML =
        gauge(tr('ev.cpu'), tr('ev.tip.cpu'), d.cpu.used, d.cpu.reserved, d.cpu.total, cores,
          d.cpu.live ? '' : esc(tr('ev.noMetrics')))
        + gauge(tr('ev.memory'), tr('ev.tip.memory'), d.memory.used, d.memory.reserved, d.memory.total, bytes,
          d.memory.live ? '' : esc(tr('ev.noMetrics')))
        + gauge(tr('ev.storage'), tr('ev.tip.storage'), s.used, null, s.total, bytes,
          esc(tr('ev.storageSched', { sched: bytes(s.scheduled), alloc: bytes(s.allocatable), op: s.over_provisioning })));
    } catch { box.hidden = true; }
  }
  window.ClusterUsage = { render: renderUsage };
})();
