/**
 * harvester-ops : Disaster Recovery, réplication asynchrone des VMs entre
 * clusters Harvester (écrans du moteur livré en 1.99.0).
 *
 * Ce module porte :
 *  - les outils communs aux trois fichiers de la réplication (ReplUtil) ;
 *  - Disaster Recovery > Contrats : cartes OU tableau (bascule gardée), avec
 *    pour chaque contrat la jauge RPO tenu / objectif, le sens source -> secours
 *    et la joignabilité, les VMs et leurs états, le dernier événement, le
 *    dernier transfert (débit, quantité) ; les alertes actives en tête ;
 *  - Disaster Recovery > Liens : débit passif continu (les vraies sauvegardes)
 *    et test actif à la demande (pods temporaires) ;
 *  - le sous-onglet Disaster Recovery de chaque cluster : contrats où il est
 *    source ET secours, badge de rôle, filtre ;
 *  - les pastilles d'alerte du menu (racine et cluster) ;
 *  - la fenêtre des notifications externes (Matrix, Slack, mail, webhook).
 *
 * Toute écriture passe par les routes /api/replication/... qui lancent la
 * ligne de commande en action suivie (dock, Activité). Les fenêtres de
 * création et de vue d'un contrat sont dans replication-create.js et
 * replication-plan.js.
 */
const ReplUtil = (() => {
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const enc = encodeURIComponent;
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 13) => (window.Icons ? Icons.svg(n, { size }) : '');
  const lang = () => (window.i18n && i18n.currentLang) || undefined;
  const num = (n, d = 1) => Number(n).toLocaleString(lang(), { maximumFractionDigits: d });

  /** Une durée en secondes, lisible. */
  function dur(s) {
    if (s == null || Number.isNaN(Number(s))) return '-';
    s = Math.max(0, Math.round(Number(s)));
    if (s < 90) return tr('rp.dur.s', { n: s });
    if (s < 5400) return tr('rp.dur.min', { n: num(s / 60, s < 600 ? 1 : 0) });
    if (s < 172800) return tr('rp.dur.h', { h: Math.floor(s / 3600), m: String(Math.round((s % 3600) / 60)).padStart(2, '0') });
    return tr('rp.dur.d', { n: num(s / 86400, 1) });
  }
  const UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  function bytes(n) {
    if (n == null || Number.isNaN(Number(n))) return '-';
    let v = Number(n), i = 0;
    while (v >= 1024 && i < UNITS.length - 1) { v /= 1024; i += 1; }
    return `${num(v, v < 10 && i ? 1 : 0)} ${UNITS[i]}`;
  }
  const rate = (bps) => (bps == null ? '-' : `${bytes(bps)}/s`);
  function ago(iso) {
    const t = Date.parse(iso || '');
    if (!t) return '-';
    const s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 90) return tr('ev.ago.s', { n: s });
    if (s < 5400) return tr('ev.ago.m', { n: Math.round(s / 60) });
    if (s < 172800) return tr('ev.ago.h', { n: Math.round(s / 3600) });
    return tr('ev.ago.d', { n: Math.round(s / 86400) });
  }
  const when = (iso) => {
    const d = new Date(iso || '');
    return Number.isNaN(d.getTime()) ? '' : d.toLocaleString(lang(), { dateStyle: 'short', timeStyle: 'medium' });
  };

  /** Appel JSON ; une erreur porte `status`, `code` et le corps. */
  async function api(method, url, body) {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const r = await fetch(url, opts);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      let msg = d.hint || d.error || `HTTP ${r.status}`;
      if (r.status === 403 && d.required) msg = tr('role.denied', { required: d.required, role: d.role });
      const e = new Error(msg);
      e.status = r.status; e.code = d.code; e.data = d;
      throw e;
    }
    return d;
  }
  async function get(url) {
    try { return await api('GET', url); } catch (e) { return { error: e.message, status: e.status, code: e.code }; }
  }
  /** Suit une action dans le dock (SSE), comme les autres écrans. */
  function follow(actionId, into, doneText, onDone) {
    if (window.VMActions && VMActions.follow) VMActions.follow(actionId, into, doneText, onDone);
    else { if (into) into.textContent = tr('bk.started', { id: actionId }); if (onDone) onDone(true); }
  }
  /** Résultat JSON de la ligne de commande d'une action terminée. */
  async function actionOutput(actionId) {
    const d = await get(`/api/action/${enc(actionId)}`);
    return (d && d.result && d.result.output) || null;
  }

  // -- états -----------------------------------------------------------------------
  // Clés en toutes lettres : le contrôle de parité i18n ne lit que des littéraux.
  const STATE = {
    draft: ['dim', () => tr('rp.state.draft')], measuring: ['info', () => tr('rp.state.measuring')],
    protecting: ['info', () => tr('rp.state.protecting')], protected: ['ok', () => tr('rp.state.protected')],
    lagging: ['warn', () => tr('rp.state.lagging')], drifting: ['warn', () => tr('rp.state.drifting')],
    degraded: ['warn', () => tr('rp.state.degraded')],
    'source-unreachable': ['fail', () => tr('rp.state.sourceUnreachable')],
    failing_over: ['info', () => tr('rp.state.failingOver')], failed_over: ['info', () => tr('rp.state.failedOver')],
    unprotecting: ['info', () => tr('rp.state.unprotecting')], unprotected: ['dim', () => tr('rp.state.unprotected')],
    error: ['fail', () => tr('rp.state.error')],
  };
  const STATE_TIP = {
    draft: () => tr('rp.stateTip.draft'), measuring: () => tr('rp.stateTip.measuring'),
    protecting: () => tr('rp.stateTip.protecting'), protected: () => tr('rp.stateTip.protected'),
    lagging: () => tr('rp.stateTip.lagging'), drifting: () => tr('rp.stateTip.drifting'),
    degraded: () => tr('rp.stateTip.degraded'), 'source-unreachable': () => tr('rp.stateTip.sourceUnreachable'),
    failing_over: () => tr('rp.stateTip.failingOver'), failed_over: () => tr('rp.stateTip.failedOver'),
    unprotecting: () => tr('rp.stateTip.unprotecting'), unprotected: () => tr('rp.stateTip.unprotected'),
    error: () => tr('rp.stateTip.error'),
  };
  const PRIORITY = ['error', 'source-unreachable', 'degraded', 'drifting', 'lagging'];
  /** L'état montré d'un contrat : celui du plan, affiné par la pire de ses VMs. */
  function planState(row) {
    const by = row.resources_by_state || {};
    if (row.state === 'protected') return PRIORITY.find((s) => by[s]) || 'protected';
    return row.state;
  }
  function stateBadge(state, extraTip) {
    const [cls, label] = STATE[state] || ['warn', () => state];
    const tip = [STATE_TIP[state] ? STATE_TIP[state]() : '', extraTip || ''].filter(Boolean).join(' ');
    return `<span class="badge ${cls} tip" data-rp-state="${esc(state)}" data-tip="${esc(tip)}">${esc(label())}</span>`;
  }
  const REASON = {
    'source-unreachable': () => tr('rp.reason.sourceUnreachable'), 'backup-error': () => tr('rp.reason.backupError'),
    'standby-degraded': () => tr('rp.reason.standbyDegraded'), 'rpo-drift': () => tr('rp.reason.rpoDrift'),
    'plan-drift': () => tr('rp.reason.planDrift'), 'rpo-exceeded': () => tr('rp.reason.rpoExceeded'),
    'no-point': () => tr('rp.reason.noPoint'), 'disk-never-backs-up': () => tr('rp.reason.neverBacksUp'),
  };
  const reasonText = (r) => (REASON[r] ? REASON[r]() : r);

  /** Jauge RPO tenu / objectif : barre jusqu'à 1,5 fois l'objectif, repère à l'objectif. */
  function gauge(held, objective, opts = {}) {
    // plus rien n'est répliqué (basculé, déprotégé) : pas de RPO à montrer
    if (opts.state && ['failed_over', 'unprotecting', 'unprotected'].includes(opts.state)) {
      return `<span class="rp-gauge-wrap"><span class="res-dim tip" data-rp-gauge="off" data-tip="${esc(tr('rp.gauge.offTip'))}">${esc(tr('rp.gauge.off'))}</span></span>`;
    }
    const max = Math.max(1, (objective || 1) * 1.5);
    const pct = held == null ? 0 : Math.min(100, (held / max) * 100);
    const mark = Math.min(100, ((objective || 0) / max) * 100);
    const cls = held == null ? 'none' : (held > objective ? 'over' : (held > 0.8 * objective ? 'near' : 'ok'));
    const tip = held == null ? tr('rp.gauge.none', { obj: dur(objective) })
      : tr('rp.gauge.tip', { held: dur(held), obj: dur(objective), pct: Math.round((held / (objective || 1)) * 100) });
    return `<span class="rp-gauge-wrap${opts.wide ? ' wide' : ''}"><span class="rp-gauge tip ${cls}" role="meter" aria-valuemin="0"
      aria-valuemax="${Math.round(max)}" aria-valuenow="${held == null ? 0 : Math.round(held)}" aria-label="${esc(tr('rp.col.rpo'))}"
      data-rp-gauge="${cls}" data-tip="${esc(tip)}"><i style="width:${pct.toFixed(1)}%"></i><em style="left:${mark.toFixed(1)}%"></em></span>
      <span class="rp-gauge-text">${held == null ? esc(tr('rp.gauge.noneShort')) : esc(dur(held))} <span class="res-dim">/ ${esc(dur(objective))}</span></span></span>`;
  }

  /** Point de joignabilité d'un cluster, lu par le suiveur. */
  function reachDot(v, cluster) {
    const cls = v === true ? 'ok' : (v === false ? 'fail' : 'dim');
    const tip = v === true ? tr('rp.reach.ok', { cluster }) : (v === false ? tr('rp.reach.down', { cluster }) : tr('rp.reach.unknown', { cluster }));
    return `<span class="rp-dot ${cls} tip" data-rp-reach="${cls}" data-tip="${esc(tip)}" aria-label="${esc(tip)}"></span>`;
  }
  function flow(row) {
    const reach = row.reach || {};
    return `<span class="rp-flow">${reachDot(reach.source, row.source_host)}<b>${esc(row.source_host)}</b>
      <span class="rp-arrow" aria-hidden="true">-&gt;</span>${reachDot(reach.target, row.target_host)}<b>${esc(row.target_host)}</b></span>`;
  }
  function vmStates(by) {
    const keys = Object.keys(by || {}).sort((a, b) => (PRIORITY.indexOf(a) + 1 || 99) - (PRIORITY.indexOf(b) + 1 || 99));
    return keys.map((s) => {
      const [cls, label] = STATE[s] || ['', () => s];
      return `<span class="badge ${cls}" data-rp-vmstate="${esc(s)}">${by[s]} ${esc(label())}</span>`;
    }).join(' ');
  }
  function transferText(t) {
    if (!t || !t.last_at) return `<span class="res-dim">${esc(tr('rp.transfer.none'))}</span>`;
    // dernier passage sans aucun bloc changé : un débit de 0 ne dirait rien
    if (!t.last_bytes) {
      return `<span class="tip" data-tip="${esc(tr('rp.transfer.tip', { at: when(t.last_at) }))}">${esc(tr('rp.transfer.idle'))}</span>
        <span class="res-dim">· ${esc(tr('rp.transfer.day', { bytes: bytes(t.bytes_24h), n: t.passes_24h || 0 }))}</span>`;
    }
    return `<span class="tip" data-tip="${esc(tr('rp.transfer.tip', { at: when(t.last_at) }))}">${esc(tr('rp.transfer.last', {
      bytes: bytes(t.last_bytes), up: rate(t.upload_bps), apply: rate(t.apply_bps) }))}</span>
      <span class="res-dim">· ${esc(tr('rp.transfer.day', { bytes: bytes(t.bytes_24h), n: t.passes_24h || 0 }))}</span>`;
  }
  function lastEvent(evs) {
    const e = (evs || [])[0];
    if (!e) return `<span class="res-dim">${esc(tr('rp.event.none'))}</span>`;
    const cls = e.level === 'error' ? 'fail' : (e.level === 'warn' ? 'warn' : '');
    return `<span class="rp-event ${cls} tip" data-tip="${esc(when(e.ts))}">${e.level !== 'info' ? icon(e.level === 'error' ? 'fail' : 'warn', 11) : ''}
      ${esc(ago(e.ts))} · ${esc(e.message || e.code || e.step)}</span>`;
  }
  /** L'action d'un plan si elle est listée : {allowed, reason, role}. */
  const action = (row, name) => (row.actions || []).find((a) => a.name === name) || null;

  const ACTION_TIP = {
    'measure-start': () => tr('rp.act.measureStartTip'), 'measure-stop': () => tr('rp.act.measureStopTip'),
    protect: () => tr('rp.act.protectTip'), failover: () => tr('rp.act.failoverTip'),
    'failover-unplanned': () => tr('rp.act.failoverUnplannedTip'), pause: () => tr('rp.act.pauseTip'),
    resume: () => tr('rp.act.resumeTip'), unprotect: () => tr('rp.act.unprotectTip'),
    'unprotect-delete-backups': () => tr('rp.act.unprotectDeleteTip'), delete: () => tr('rp.act.deleteTip'),
  };
  const ACTION_LABEL = {
    'measure-start': () => tr('rp.act.measureStart'), 'measure-stop': () => tr('rp.act.measureStop'),
    protect: () => tr('rp.act.protect'), failover: () => tr('rp.act.failover'),
    'failover-unplanned': () => tr('rp.act.failoverUnplanned'), pause: () => tr('rp.act.pause'),
    resume: () => tr('rp.act.resume'), unprotect: () => tr('rp.act.unprotect'),
    'unprotect-delete-backups': () => tr('rp.act.unprotectDelete'), delete: () => tr('rp.act.delete'),
  };
  /** Pourquoi une action est grisée (rôle ou raison du moteur). */
  function deniedTip(a) {
    if (!a) return '';
    if (a.reason === 'backups-in-use') return tr('rp.act.backupsInUse');
    if (!a.allowed) return tr('rp.act.needsRole', { role: a.role });
    return '';
  }
  function actionButton(row, name, cls = 'btn-secondary', extra = '') {
    const a = action(row, name);
    if (!a) return '';
    const deny = deniedTip(a);
    const tip = deny ? `${ACTION_TIP[name] ? ACTION_TIP[name]() : ''} ${deny}` : (ACTION_TIP[name] ? ACTION_TIP[name]() : '');
    return `<button type="button" class="btn btn-sm ${cls} tip" data-rp-act="${esc(name)}" data-plan="${esc(row.id)}"
      data-tip="${esc(tip)}"${a.allowed ? '' : ' disabled aria-disabled="true"'}${extra}>${esc(ACTION_LABEL[name] ? ACTION_LABEL[name]() : name)}</button>`;
  }
  const roleRank = (r) => ({ viewer: 1, operator: 2, admin: 3 }[r] || 0);

  function clusterNames() {
    return window.App && App.listClusters ? App.listClusters() : [];
  }

  return { tr, enc, esc, icon, num, dur, bytes, rate, ago, when, api, get, follow, actionOutput, STATE, planState,
           stateBadge, reasonText, gauge, reachDot, flow, vmStates, transferText, lastEvent, action, actionButton,
           ACTION_LABEL, ACTION_TIP, deniedTip, roleRank, clusterNames };
})();
window.ReplUtil = ReplUtil;

const Replication = (() => {
  const U = ReplUtil;
  const { tr, enc, esc, icon } = U;
  const REFRESH_MS = 20000;
  const BADGE_MS = 60000;
  const STORE = 'harvester_ops_replication';

  function prefs() {
    const p = { view: 'cards', state: '', q: '', side: 'any' };
    try { Object.assign(p, JSON.parse(localStorage.getItem(STORE) || '{}')); } catch { /* réglages illisibles */ }
    if (!['cards', 'table'].includes(p.view)) p.view = 'cards';
    if (!['any', 'source', 'target'].includes(p.side)) p.side = 'any';
    return p;
  }
  function savePrefs(p) {
    try { localStorage.setItem(STORE, JSON.stringify({ view: p.view, side: p.side })); } catch { /* stockage indisponible */ }
  }

  // ===========================================================================
  // Contrats (menu racine) et sous-onglet d'un cluster : une même vue
  // ===========================================================================
  let cur = null;   // { host, mode: 'all' | 'cluster', cluster, data, alerts, prefs, timer }

  function start(host, mode = 'all', cluster = null) {
    stop();
    if (!host) return Promise.resolve();
    cur = { host, mode, cluster, data: null, alerts: null, hostRows: null, prefs: prefs(), timer: null };
    shell();
    cur.timer = setInterval(() => {
      if (cur && cur.host.isConnected && !cur.host.closest('[hidden]') && !document.hidden) load();
    }, REFRESH_MS);
    return load();
  }
  function stop() {
    if (cur && cur.timer) clearInterval(cur.timer);
    cur = null;
  }

  function shell() {
    const p = cur.prefs;
    const views = ['cards', 'table'].map((v) => `<button type="button" class="sub-tab tip${p.view === v ? ' active' : ''}" role="tab"
        aria-selected="${p.view === v}" data-rp-view="${v}" data-tip="${esc(v === 'cards' ? tr('rp.view.cardsTip') : tr('rp.view.tableTip'))}">
        ${icon(v === 'cards' ? 'placement' : 'doc')} <span>${esc(v === 'cards' ? tr('rp.view.cards') : tr('rp.view.table'))}</span></button>`).join('');
    const sides = cur.mode === 'cluster' ? `<div class="sub-tabs sub-tabs-inline rp-sides" role="tablist" aria-label="${esc(tr('rp.side.label'))}">
        ${['any', 'source', 'target'].map((s) => `<button type="button" class="sub-tab tip${p.side === s ? ' active' : ''}" role="tab"
          aria-selected="${p.side === s}" data-rp-side="${s}" data-tip="${esc(s === 'any' ? tr('rp.side.anyTip') : (s === 'source' ? tr('rp.side.sourceTip') : tr('rp.side.targetTip')))}">
          <span>${esc(s === 'any' ? tr('rp.side.any') : (s === 'source' ? tr('rp.side.source') : tr('rp.side.target')))}</span></button>`).join('')}</div>` : '';
    cur.host.innerHTML = `<div class="card na-card rp-card" data-rp-mode="${esc(cur.mode)}">
        <div class="res-tools">
          ${sides}
          <input type="search" class="res-filter tip" data-rp="q" placeholder="${esc(tr('rp.search'))}" data-tip="${esc(tr('rp.searchTip'))}" aria-label="${esc(tr('rp.search'))}">
          <select class="tip" data-rp="state" data-tip="${esc(tr('rp.filter.stateTip'))}" aria-label="${esc(tr('rp.filter.stateTip'))}"></select>
          <span class="res-count"></span>
          <div class="sub-tabs sub-tabs-inline rp-view-toggle" role="tablist">${views}</div>
          <button type="button" class="btn btn-sm btn-secondary tip" data-rp="refresh" data-tip="${esc(tr('res.refreshTip'))}">${icon('refresh')} ${esc(tr('overview.refresh'))}</button>
          ${cur.mode === 'all' ? `<button type="button" class="btn btn-sm btn-secondary tip" data-rp="notify" data-tip="${esc(tr('rp.notify.openTip'))}">${icon('news')} ${esc(tr('rp.notify.open'))}</button>` : ''}
          <button type="button" class="btn btn-sm btn-primary tip rp-new" data-rp="new" data-tip="${esc(tr('rp.newTip'))}">${icon('add')} ${esc(tr('rp.new'))}</button>
        </div>
        <div class="rp-follower" data-rp="follower" hidden></div>
        <div class="rp-alerts" data-rp="alerts" hidden></div>
        <div data-rp="body"><p class="form-hint">${esc(tr('common.loading'))}</p></div>
        ${cur.mode === 'cluster' ? `<div data-rp="vms"></div>` : ''}
      </div>`;
    const card = cur.host.querySelector('.rp-card');
    card.addEventListener('click', onClick);
    card.querySelector('[data-rp="q"]').addEventListener('input', (e) => { cur.prefs.q = e.target.value.trim().toLowerCase(); render(); });
    card.querySelector('[data-rp="state"]').addEventListener('change', (e) => { cur.prefs.state = e.target.value; render(); });
  }

  async function load() {
    if (!cur) return;
    const c = cur;
    const q = c.mode === 'cluster' ? `?host=${enc(c.cluster)}` : '';
    const reqs = [U.get(`/api/replication/plans${q}`), U.get(`/api/replication/alerts${q}`)];
    if (c.mode === 'cluster') reqs.push(U.get(`/api/replication/hosts/harvester/${enc(c.cluster)}/resources?side=any`));
    const [d, a, h] = await Promise.all(reqs);
    if (c !== cur) return;
    c.data = d; c.alerts = a; c.hostRows = h || null;
    render();
  }

  function filtered() {
    const p = cur.prefs;
    return (cur.data.plans || []).filter((r) => {
      if (p.state && U.planState(r) !== p.state) return false;
      if (cur.mode === 'cluster' && p.side !== 'any') {
        const role = r.source_host === cur.cluster ? 'source' : 'target';
        if (role !== p.side) return false;
      }
      if (!p.q) return true;
      const hay = [r.name, r.source_host, r.target_host, ...(r.resources || []).map((x) => `${x.namespace}/${x.name}`)].join(' ').toLowerCase();
      return hay.includes(p.q);
    });
  }

  function render() {
    if (!cur || !cur.host.isConnected) return;
    const body = cur.host.querySelector('[data-rp="body"]');
    const d = cur.data;
    cur.host.querySelectorAll('[data-rp-view]').forEach((b) => {
      const on = b.dataset.rpView === cur.prefs.view;
      b.classList.toggle('active', on); b.setAttribute('aria-selected', String(on));
    });
    cur.host.querySelectorAll('[data-rp-side]').forEach((b) => {
      const on = b.dataset.rpSide === cur.prefs.side;
      b.classList.toggle('active', on); b.setAttribute('aria-selected', String(on));
    });
    if (!d || d.error) {
      body.innerHTML = `<p class="res-error">${esc(d && d.error ? d.error : tr('fabric.unreachable'))}</p>`;
      return;
    }
    const role = d.role || 'viewer';
    const nb = cur.host.querySelector('[data-rp="new"]');
    nb.disabled = U.roleRank(role) < 2;
    nb.dataset.tip = nb.disabled ? tr('rp.viewerTip') : tr('rp.newTip');
    renderFollower(d.follower);
    renderAlerts(cur.host.querySelector('[data-rp="alerts"]'), cur.alerts);
    fillStates(d.plans || []);
    const rows = filtered();
    cur.host.querySelector('.res-count').textContent = tr('rp.count', { n: rows.length });
    if (!(d.plans || []).length) {
      body.innerHTML = `<div class="rp-empty"><p class="hint">${esc(cur.mode === 'cluster' ? tr('rp.emptyCluster', { cluster: cur.cluster }) : tr('rp.empty'))}</p></div>`;
    } else if (!rows.length) {
      body.innerHTML = `<p class="hint">${esc(tr('rp.noMatch'))}</p>`;
    } else if (cur.prefs.view === 'table') {
      body.innerHTML = tableHtml(rows, role);
    } else {
      body.innerHTML = `<div class="rp-cards">${rows.map((r) => cardHtml(r, role)).join('')}</div>`;
    }
    if (cur.mode === 'cluster') renderHostVms();
  }

  function fillStates(plans) {
    const sel = cur.host.querySelector('[data-rp="state"]');
    const states = [...new Set(plans.map(U.planState))].sort();
    const want = [''].concat(states).join('|');
    if ([...sel.options].map((o) => o.value).join('|') !== want) {
      sel.innerHTML = `<option value="">${esc(tr('rp.filter.allStates'))}</option>`
        + states.map((s) => `<option value="${esc(s)}">${esc(U.STATE[s] ? U.STATE[s][1]() : s)}</option>`).join('');
    }
    if (!states.includes(cur.prefs.state)) cur.prefs.state = '';
    sel.value = cur.prefs.state;
  }

  function renderFollower(f) {
    const el = cur.host.querySelector('[data-rp="follower"]');
    if (!f || f.running || f.state === 'running') { el.hidden = true; el.innerHTML = ''; return; }
    el.hidden = false;
    const text = f.state === 'elsewhere' ? tr('rp.follower.elsewhere') : (f.enabled === false || f.state === 'off' || f.state === 'disabled'
      ? tr('rp.follower.off') : tr('rp.follower.down', { state: f.state || '?' }));
    el.innerHTML = `<div class="sto-finding sev-action"><div class="sto-finding-title">${icon('warn')} ${esc(text)}</div>
      ${f.last_error ? `<div class="sto-finding-facts">${esc(f.last_error)}</div>` : ''}</div>`;
  }

  // -- alertes -------------------------------------------------------------------------
  const ALERT = {
    'rpo-exceeded': () => tr('rp.alert.rpoExceeded'), 'disk-never-backs-up': () => tr('rp.alert.neverBacksUp'),
    'source-vm-running-after-failover': () => tr('rp.alert.sourceRunning'), 'backup-error': () => tr('rp.alert.backupError'),
    'standby-degraded': () => tr('rp.alert.standbyDegraded'), 'source-unreachable': () => tr('rp.alert.sourceUnreachable'),
    'plan-drift': () => tr('rp.alert.planDrift'), 'plan-error': () => tr('rp.alert.planError'),
    'selector-new-vm': () => tr('rp.alert.selectorNewVm'), 'carnet-stale': () => tr('rp.alert.carnetStale'),
    'auto-protect-failed': () => tr('rp.alert.autoProtectFailed'),
  };
  const alertText = (a) => (ALERT[a.code] ? ALERT[a.code]() : a.code);
  function renderAlerts(el, a) {
    if (!el) return;
    const list = (a && a.alerts) || [];
    if (!list.length) { el.hidden = true; el.innerHTML = ''; return; }
    el.hidden = false;
    el.innerHTML = `<details class="rp-alert-box" open><summary class="tip" data-tip="${esc(tr('rp.alerts.tip'))}">${icon('warn')}
        ${esc(tr('rp.alerts.title', { n: list.length }))}</summary>
      <ul class="rp-alert-list">${list.slice(0, 50).map((x) => `<li class="${x.level === 'error' ? 'fail' : 'warn'}" data-rp-alert="${esc(x.code)}">
        <b>${esc(alertText(x))}</b> · <a href="#" data-rp-open="${esc(x.plan)}" class="tip" data-tip="${esc(tr('rp.openTip'))}">${esc(x.plan_name)}</a>
        ${x.name ? ` · ${esc(`${x.namespace}/${x.name}`)}` : ''}
        <span class="res-dim">${x.since ? ` · ${esc(U.ago(x.since))}` : ''}${x.message ? ` · ${esc(x.message)}` : ''}</span></li>`).join('')}</ul></details>`;
  }

  // -- cartes et tableau --------------------------------------------------------------------
  function roleBadge(r) {
    if (cur.mode !== 'cluster') return '';
    const src = r.source_host === cur.cluster;
    return `<span class="badge ${src ? 'info' : 'ok'} tip" data-rp-role="${src ? 'source' : 'target'}"
      data-tip="${esc(src ? tr('rp.role.sourceTip', { cluster: cur.cluster }) : tr('rp.role.targetTip', { cluster: cur.cluster }))}">${esc(src ? tr('rp.role.source') : tr('rp.role.target'))}</span>`;
  }
  function failoverButton(r) {
    const a = U.action(r, 'failover') || U.action(r, 'failover-unplanned');
    if (!a) return '';
    const deny = U.deniedTip(a);
    return `<button type="button" class="btn btn-sm btn-danger tip" data-rp-act="failover" data-plan="${esc(r.id)}"
      data-tip="${esc(`${tr('rp.act.failoverDialogTip')}${deny ? ` ${deny}` : ''}`)}">${icon('migrate')} ${esc(tr('rp.act.failoverDots'))}</button>`;
  }
  function vmCount(r) {
    const n = (r.resources || []).length;
    return `${esc(tr('rp.vms', { n }))}${(r.pending_vms || []).length ? ` <span class="badge warn tip" data-tip="${esc(tr('rp.pendingTip', { list: r.pending_vms.join(', ') }))}">+${r.pending_vms.length}</span>` : ''}`;
  }
  function cardHtml(r, role) {
    const st = U.planState(r);
    return `<article class="rp-plan-card" data-rp-plan="${esc(r.id)}" data-state="${esc(st)}">
      <header><h3><a href="#" data-rp-open="${esc(r.id)}" class="tip" data-tip="${esc(tr('rp.openTip'))}">${esc(r.name)}</a></h3>
        <span class="rp-badges">${roleBadge(r)} ${U.stateBadge(st)}${r.alerts ? ` <span class="badge fail tip" data-tip="${esc(tr('rp.alertsCountTip'))}">${icon('warn', 11)} ${r.alerts}</span>` : ''}</span></header>
      <div class="rp-card-flow">${U.flow(r)}</div>
      <div class="rp-card-gauge"><small>${esc(tr('rp.col.rpo'))}</small>${U.gauge(r.rpo_held_s, r.rpo_objective_s, { wide: true, state: st })}</div>
      <dl class="rp-meta">
        <div><dt>${esc(tr('rp.col.vms'))}</dt><dd>${vmCount(r)} ${U.vmStates(r.resources_by_state)}</dd></div>
        <div><dt>${esc(tr('rp.col.transfer'))}</dt><dd>${U.transferText(r.transfer)}</dd></div>
        <div class="rp-meta-wide"><dt>${esc(tr('rp.col.lastEvent'))}</dt><dd>${U.lastEvent(r.last_events)}</dd></div>
      </dl>
      <footer class="btn-row">
        <button type="button" class="btn btn-sm btn-secondary tip" data-rp-open="${esc(r.id)}" data-tip="${esc(tr('rp.openTip'))}">${icon('preview')} ${esc(tr('rp.open'))}</button>
        ${U.actionButton(r, 'measure-start')}${U.actionButton(r, 'protect')}${failoverButton(r)}
      </footer></article>`;
  }
  function tableHtml(rows, role) {
    const roleCol = cur.mode === 'cluster';
    return `<div class="tw"><table class="data-table rp-table"><thead><tr>
        <th>${esc(tr('rp.col.contract'))}</th>${roleCol ? `<th>${esc(tr('rp.col.role'))}</th>` : ''}<th>${esc(tr('rp.col.state'))}</th>
        <th>${esc(tr('rp.col.direction'))}</th><th>${esc(tr('rp.col.vms'))}</th><th>${esc(tr('rp.col.rpo'))}</th>
        <th>${esc(tr('rp.col.transfer'))}</th><th>${esc(tr('rp.col.lastEvent'))}</th><th></th></tr></thead>
      <tbody>${rows.map((r) => `<tr data-rp-plan="${esc(r.id)}">
        <td><a href="#" data-rp-open="${esc(r.id)}" class="tip" data-tip="${esc(tr('rp.openTip'))}"><b>${esc(r.name)}</b></a></td>
        ${roleCol ? `<td>${roleBadge(r)}</td>` : ''}
        <td>${U.stateBadge(U.planState(r))}</td><td>${U.flow(r)}</td>
        <td>${vmCount(r)}<div class="rp-chips">${U.vmStates(r.resources_by_state)}</div></td>
        <td>${U.gauge(r.rpo_held_s, r.rpo_objective_s, { state: U.planState(r) })}</td><td class="rp-xfer">${U.transferText(r.transfer)}</td>
        <td class="rp-ev">${U.lastEvent(r.last_events)}</td>
        <td class="btn-row rp-row-act"><button type="button" class="btn btn-sm btn-secondary tip" data-rp-open="${esc(r.id)}" data-tip="${esc(tr('rp.openTip'))}">${esc(tr('rp.open'))}</button>${failoverButton(r)}</td>
      </tr>`).join('')}</tbody></table></div>`;
  }

  /** Sous-onglet d'un cluster : ses VMs protégées, vues comme source ou secours. */
  function renderHostVms() {
    const el = cur.host.querySelector('[data-rp="vms"]');
    const h = cur.hostRows;
    if (!el) return;
    if (!h || h.error) { el.innerHTML = h && h.error ? `<p class="res-error">${esc(h.error)}</p>` : ''; return; }
    const rows = (h.resources || []).filter((r) => cur.prefs.side === 'any' || r.side === cur.prefs.side)
      .filter((r) => !cur.prefs.q || `${r.namespace}/${r.name} ${r.plan_name}`.toLowerCase().includes(cur.prefs.q));
    el.innerHTML = `<h4 class="rp-sub-title">${esc(tr('rp.hostVms', { cluster: cur.cluster }))}</h4>${rows.length ? `<div class="tw"><table class="data-table rp-host-table"><thead><tr>
        <th>${esc(tr('rp.col.vm'))}</th><th>${esc(tr('rp.col.role'))}</th><th>${esc(tr('rp.col.peer'))}</th><th>${esc(tr('rp.col.contract'))}</th>
        <th>${esc(tr('rp.col.state'))}</th><th>${esc(tr('rp.col.rpo'))}</th><th>${esc(tr('rp.col.lastEvent'))}</th></tr></thead>
      <tbody>${rows.map((r) => `<tr data-rp-res="${esc(r.resource_uid)}" data-side="${esc(r.side)}">
        <td><b>${esc(`${r.namespace}/${r.name}`)}</b></td>
        <td><span class="badge ${r.side === 'source' ? 'info' : 'ok'}">${esc(r.side === 'source' ? tr('rp.role.source') : tr('rp.role.target'))}</span></td>
        <td>${esc(r.peer_host || '')}</td>
        <td><a href="#" data-rp-open="${esc(r.plan)}" class="tip" data-tip="${esc(tr('rp.openTip'))}">${esc(r.plan_name)}</a></td>
        <td>${U.stateBadge(r.state, (r.reasons || []).map(U.reasonText).join(', '))}</td>
        <td>${U.gauge(r.rpo_held_s, r.rpo_objective_s, { state: r.state })}</td><td>${U.lastEvent(r.last_events)}</td></tr>`).join('')}</tbody></table></div>`
      : `<p class="hint">${esc(tr('rp.hostVmsNone'))}</p>`}`;
  }

  function onClick(e) {
    const open = e.target.closest('[data-rp-open]');
    if (open) { e.preventDefault(); if (window.ReplicationPlan) ReplicationPlan.open(open.dataset.rpOpen); return; }
    const v = e.target.closest('[data-rp-view]');
    if (v) { cur.prefs.view = v.dataset.rpView; savePrefs(cur.prefs); render(); return; }
    const s = e.target.closest('[data-rp-side]');
    if (s) { cur.prefs.side = s.dataset.rpSide; savePrefs(cur.prefs); render(); return; }
    const act = e.target.closest('[data-rp-act]');
    if (act && !act.disabled) { runAction(act.dataset.rpAct, act.dataset.plan, act); return; }
    const b = e.target.closest('[data-rp]');
    if (!b || b.tagName === 'SELECT' || b.tagName === 'INPUT') return;
    if (b.dataset.rp === 'refresh') load();
    if (b.dataset.rp === 'new' && window.ReplicationCreate) {
      ReplicationCreate.open({ from: cur.mode === 'cluster' ? cur.cluster : '', onCreated: () => { if (cur) load(); } });
    }
    if (b.dataset.rp === 'notify') openNotify();
  }

  /** Les gestes simples d'une carte ; la bascule ouvre sa fenêtre. */
  async function runAction(name, planId, btn) {
    if (name === 'failover' || name === 'failover-unplanned') {
      if (window.ReplicationPlan) ReplicationPlan.failover(planId);
      return;
    }
    if (window.ReplicationPlan) ReplicationPlan.act(planId, name, btn, () => { if (cur) load(); refreshBadges(); });
  }

  // ===========================================================================
  // Pastilles d'alerte du menu (racine et cluster), relues toutes les minutes
  // ===========================================================================
  let badgeTimer = null;
  let lastAlerts = null;
  async function refreshBadges() {
    const d = await U.get('/api/replication/alerts');
    if (!d || d.error) return;
    lastAlerts = d;
    paintBadges();
  }
  function paintBadges() {
    const d = lastAlerts;
    if (!d) return;
    const list = d.alerts || [];
    const cluster = window.App && App.getCurrentCluster ? App.getCurrentCluster() : null;
    const mine = list.filter((a) => a.source_host === cluster || a.target_host === cluster);
    const paint = (el, items) => {
      if (!el) return;
      const n = items.length;
      el.hidden = !n;
      el.textContent = n > 99 ? '99+' : String(n);
      el.classList.toggle('fail', items.some((a) => a.level === 'error'));
      el.dataset.tip = tr('rp.badgeTip', { n });
      el.setAttribute('aria-label', tr('rp.badgeTip', { n }));
    };
    document.querySelectorAll('[data-rp-badge="all"]').forEach((el) => paint(el, list));
    document.querySelectorAll('[data-rp-badge="cluster"]').forEach((el) => paint(el, mine));
  }
  function startBadges() {
    if (badgeTimer) return;
    refreshBadges();
    badgeTimer = setInterval(() => { if (!document.hidden) refreshBadges(); }, BADGE_MS);
  }

  /** Alertes d'un cluster pour l'onglet Événements (même forme que ses lignes). */
  async function eventRows(cluster) {
    const d = await U.get(`/api/replication/alerts?host=${enc(cluster)}`);
    if (!d || d.error) return [];
    return (d.alerts || []).map((a) => ({
      group: 'dr', type: 'Warning', reason: alertText(a), kind: tr('rp.ev.kind'),
      namespace: a.namespace || '', name: a.name ? a.name : a.plan_name,
      message: `${a.plan_name}: ${a.message || alertText(a)}`, last: a.since || '', count: 1, plan: a.plan,
    }));
  }

  // ===========================================================================
  // Liens entre clusters
  // ===========================================================================
  let links = null;   // { host, data, timer }
  function startLinks(host) {
    stopLinks();
    if (!host) return Promise.resolve();
    links = { host, data: null, timer: null, busy: 0, flash: null };
    host.innerHTML = `<div class="card na-card rp-links-card">
      <div class="res-tools"><span class="res-count"></span>
        <button type="button" class="btn btn-sm btn-secondary tip" data-rpl="refresh" data-tip="${esc(tr('res.refreshTip'))}">${icon('refresh')} ${esc(tr('overview.refresh'))}</button>
      </div>
      <div data-rpl="body"><p class="form-hint">${esc(tr('common.loading'))}</p></div>
      <div class="fk-step rp-linktest-new" data-rpl="new"></div></div>`;
    host.querySelector('.rp-links-card').addEventListener('click', onLinksClick);
    // pas de repeinte pendant un essai : le message de suivi serait remplacé
    links.timer = setInterval(() => { if (links && !links.busy && !links.host.closest('[hidden]') && !document.hidden) loadLinks(); }, 60000);
    return loadLinks();
  }
  function stopLinks() { if (links && links.timer) clearInterval(links.timer); links = null; }

  async function loadLinks() {
    if (!links) return;
    const l = links;
    const [d, p] = await Promise.all([U.get('/api/replication/links'), U.get('/api/replication/plans')]);
    if (l !== links) return;
    l.data = d; l.role = (p && p.role) || 'viewer';
    renderLinks();
  }

  function spark(series, key, cls) {
    const pts = (series || []).filter((x) => x[key] != null);
    if (pts.length < 2) return `<span class="res-dim">${esc(tr('rp.links.noSeries'))}</span>`;
    const W = 220, H = 40;
    const t0 = Date.parse(pts[0].t), t1 = Date.parse(pts[pts.length - 1].t) || t0 + 1;
    const max = Math.max(...pts.map((x) => x[key])) || 1;
    const d = pts.map((x, i) => `${i ? 'L' : 'M'}${(((Date.parse(x.t) - t0) / (t1 - t0 || 1)) * W).toFixed(1)},${(H - 2 - (x[key] / max) * (H - 6)).toFixed(1)}`).join(' ');
    return `<svg class="rp-spark ${cls}" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(tr('rp.links.sparkAria', { max: U.rate(max) }))}">
      <path d="${d}" fill="none" stroke-width="2"/></svg>`;
  }
  function linkHtml(k) {
    const p = k.passive || {};
    const a = k.active;
    const slow = (x, y) => (x != null && y != null && x < y * 0.5 ? ' slow' : '');
    const canTest = U.roleRank(links.role) >= 2;
    return `<article class="rp-link fk-step" data-rp-link="${esc(`${k.source}|${k.target}`)}">
      <div class="fk-step-head"><b>${esc(k.source)} -&gt; ${esc(k.target)}</b>
        <span class="res-dim">${esc(tr('rp.links.plans', { list: (k.plans || []).map((x) => x.name).join(', ') || '-' }))}</span></div>
      <div class="rp-leg-flow">
        <span class="rp-leg-node">${esc(k.source)}</span>
        <span class="rp-leg-line${slow(p.upload_bps_p50_24h, p.apply_bps_p50_24h)} tip" data-tip="${esc(tr('rp.links.uploadTip'))}"><span>${esc(U.rate(p.upload_bps_p50_24h))}</span></span>
        <span class="rp-leg-node tip" data-tip="${esc(tr('rp.links.targetTip'))}">${icon('storage', 12)} ${esc(k.backup_target || tr('rp.links.backupTarget'))}</span>
        <span class="rp-leg-line${slow(p.apply_bps_p50_24h, p.upload_bps_p50_24h)} tip" data-tip="${esc(tr('rp.links.applyTip'))}"><span>${esc(U.rate(p.apply_bps_p50_24h))}</span></span>
        <span class="rp-leg-node">${esc(k.target)}</span>
      </div>
      <div class="grid2 rp-link-grid">
        <div class="rp-link-box"><h4>${esc(tr('rp.links.passive'))}</h4>
          <p class="form-hint">${esc(tr('rp.links.passiveHint'))}</p>
          <div class="kpi rp-kpi">
            <div><small>${esc(tr('rp.links.upLast'))}</small><b>${esc(U.rate(p.upload_bps))}</b></div>
            <div><small>${esc(tr('rp.links.applyLast'))}</small><b>${esc(U.rate(p.apply_bps))}</b></div>
            <div><small>${esc(tr('rp.links.day'))}</small><b>${esc(U.bytes(p.bytes_24h))}</b></div>
          </div>
          <div class="rp-sparks"><span>${esc(tr('rp.links.upload'))}</span>${spark(p.series, 'upload_bps', 'up')}
            <span>${esc(tr('rp.links.apply'))}</span>${spark(p.series, 'apply_bps', 'apply')}</div>
          <p class="form-hint">${p.last_at ? esc(tr('rp.links.lastPass', { ago: U.ago(p.last_at) })) : esc(tr('rp.links.noPass'))}</p></div>
        <div class="rp-link-box"><h4>${esc(tr('rp.links.active'))}</h4>
          <p class="form-hint">${esc(tr('rp.links.activeHint'))}</p>
          ${a ? (a.error ? `<p class="res-error">${icon('fail')} ${esc(a.error)}</p>` : `<div class="kpi rp-kpi">
            <div><small>${esc(tr('rp.links.write'))}</small><b data-rpl-write>${esc(U.rate(a.write_bps))}</b></div>
            <div><small>${esc(tr('rp.links.read'))}</small><b data-rpl-read>${esc(U.rate(a.read_bps))}</b></div>
            <div><small>${esc(tr('rp.links.size'))}</small><b>${esc(U.bytes(a.size_bytes))}</b></div></div>`)
            + `<p class="form-hint">${esc(tr('rp.links.testedAt', { ago: U.ago(a.at), by: a.by || '?' }))}</p>`
            : `<p class="hint">${esc(tr('rp.links.never'))}</p>`}
          <div class="btn-row">${sizeSelect()}
            <button type="button" class="btn btn-sm btn-primary tip" data-rpl="test" data-from="${esc(k.source)}" data-to="${esc(k.target)}"
              data-tip="${esc(canTest ? tr('rp.links.testTip') : tr('rp.viewerTip'))}"${canTest ? '' : ' disabled'}>${icon('test')} ${esc(tr('rp.links.test'))}</button></div>
          <div class="of-msg" data-rpl="msg" role="status"></div></div>
      </div></article>`;
  }
  const sizeSelect = () => `<select data-rpl="size" class="tip" data-tip="${esc(tr('rp.links.sizeTip'))}" aria-label="${esc(tr('rp.links.size'))}">
      ${[256, 512, 1024, 2048].map((n) => `<option value="${n}"${n === 512 ? ' selected' : ''}>${n} MiB</option>`).join('')}</select>`;

  function renderLinks() {
    const l = links;
    if (!l || !l.host.isConnected) return;
    const body = l.host.querySelector('[data-rpl="body"]');
    const d = l.data;
    if (!d || d.error) { body.innerHTML = `<p class="res-error">${esc(d && d.error ? d.error : tr('fabric.unreachable'))}</p>`; return; }
    const list = d.links || [];
    l.host.querySelector('.res-count').textContent = tr('rp.links.count', { n: list.length });
    body.innerHTML = `<p class="hint rp-links-hint">${esc(tr('rp.links.hint'))}</p>${list.length ? list.map(linkHtml).join('')
      : `<p class="hint">${esc(tr('rp.links.none'))}</p>`}`;
    // un test entre deux clusters sans contrat (avant d'en créer un)
    const names = U.clusterNames();
    const canTest = U.roleRank(l.role) >= 2;
    const opts = (sel) => names.map((n, i) => `<option value="${esc(n)}"${i === sel ? ' selected' : ''}>${esc(n)}</option>`).join('');
    l.host.querySelector('[data-rpl="new"]').innerHTML = `<div class="fk-step-head"><b>${esc(tr('rp.links.newTitle'))}</b></div>
      <p class="form-hint">${esc(tr('rp.links.newHint'))}</p>
      <div class="btn-row"><select data-rpl="from" class="tip" data-tip="${esc(tr('rp.f.fromTip'))}" aria-label="${esc(tr('rp.f.from'))}">${opts(0)}</select>
        <span aria-hidden="true">-&gt;</span>
        <select data-rpl="to" class="tip" data-tip="${esc(tr('rp.f.toTip'))}" aria-label="${esc(tr('rp.f.to'))}">${opts(1)}</select>${sizeSelect()}
        <button type="button" class="btn btn-sm btn-primary tip" data-rpl="test-new" data-tip="${esc(canTest ? tr('rp.links.testTip') : tr('rp.viewerTip'))}"${canTest ? '' : ' disabled'}>${icon('test')} ${esc(tr('rp.links.test'))}</button></div>
      <div class="of-msg" data-rpl="msg" role="status"></div>`;
    showFlash();
  }
  /** Le résultat du dernier essai reste affiché après la relecture qui suit. */
  function showFlash() {
    const f = links && links.flash;
    if (!f) return;
    const box = f.key === 'new' ? links.host.querySelector('.rp-linktest-new')
      : links.host.querySelector(`[data-rp-link="${CSS.escape(f.key)}"]`);
    const msg = box && box.querySelector('[data-rpl="msg"]');
    if (msg) msg.innerHTML = f.html;
  }

  async function onLinksClick(e) {
    const b = e.target.closest('[data-rpl]');
    if (!b || b.disabled || b.tagName === 'SELECT') return;
    if (b.dataset.rpl === 'refresh') { loadLinks(); return; }
    if (b.dataset.rpl !== 'test' && b.dataset.rpl !== 'test-new') return;
    const box = b.closest('.rp-link-box, .rp-linktest-new');
    const from = b.dataset.from || box.querySelector('[data-rpl="from"]').value;
    const to = b.dataset.to || box.querySelector('[data-rpl="to"]').value;
    const msg = box.querySelector('[data-rpl="msg"]');
    if (!from || !to || from === to) { msg.innerHTML = `<span class="res-error">${esc(tr('rp.links.pickTwo'))}</span>`; return; }
    b.disabled = true;
    const key = b.dataset.rpl === 'test' ? `${from}|${to}` : 'new';
    const l = links;
    try {
      const out = await U.api('POST', '/api/replication/links/test', { from, to, size_mib: Number(box.querySelector('[data-rpl="size"]').value) });
      l.busy += 1;
      U.follow(out.action_id, msg, tr('rp.links.testDone'), () => {
        l.busy -= 1;
        l.flash = { key, html: msg.innerHTML };
        b.disabled = false;
        loadLinks();
      });
    } catch (err) {
      b.disabled = false;
      msg.innerHTML = `<span class="res-error">${icon('fail')} ${esc(err.message)}</span>`;
    }
  }

  // ===========================================================================
  // Notifications externes (Matrix, Slack, mail, webhook)
  // ===========================================================================
  const MASK = '********';
  const KINDS = ['matrix', 'slack', 'email', 'webhook'];
  const KIND_LABEL = { matrix: () => tr('rp.notify.kind.matrix'), slack: () => tr('rp.notify.kind.slack'),
                       email: () => tr('rp.notify.kind.email'), webhook: () => tr('rp.notify.kind.webhook') };
  const nfield = (label, input, tip) => `<label class="bk-field"><span>${esc(label)}</span>${
    input.replace(/^<(input|select|textarea)/, `<$1 class="tip" data-tip="${esc(tip)}"`)}</label>`;
  function channelHtml(c, i, canEdit) {
    const v = (k) => esc(c[k] == null ? '' : c[k]);
    const secret = (name, label, tip) => nfield(label, `<input type="password" name="${name}" autocomplete="new-password"
      value="${c[name] ? MASK : ''}" placeholder="${esc(tr('rp.notify.secretPh'))}">`, tip);
    let f = '';
    if (c.kind === 'matrix') {
      f = nfield(tr('rp.notify.f.homeserver'), `<input name="homeserver" value="${v('homeserver')}" placeholder="https://matrix.example.org">`, tr('rp.notify.t.homeserver'))
        + nfield(tr('rp.notify.f.room'), `<input name="room" value="${v('room')}" placeholder="!abc:example.org">`, tr('rp.notify.t.room'))
        + secret('token', tr('rp.notify.f.token'), tr('rp.notify.t.token'));
    } else if (c.kind === 'slack') {
      f = secret('url', tr('rp.notify.f.slackUrl'), tr('rp.notify.t.slackUrl'));
    } else if (c.kind === 'email') {
      f = nfield(tr('rp.notify.f.smtpHost'), `<input name="smtp_host" value="${v('smtp_host')}">`, tr('rp.notify.t.smtpHost'))
        + nfield(tr('rp.notify.f.smtpPort'), `<input name="smtp_port" type="number" min="1" max="65535" value="${v('smtp_port') || 587}">`, tr('rp.notify.t.smtpPort'))
        + `<label class="bk-check tip" data-tip="${esc(tr('rp.notify.t.starttls'))}"><input type="checkbox" name="starttls"${c.starttls !== false ? ' checked' : ''}> <span>STARTTLS</span></label>`
        + `<label class="bk-check tip" data-tip="${esc(tr('rp.notify.t.implicitTls'))}"><input type="checkbox" name="implicit_tls"${c.implicit_tls ? ' checked' : ''}> <span>${esc(tr('rp.notify.f.implicitTls'))}</span></label>`
        + nfield(tr('rp.notify.f.username'), `<input name="username" value="${v('username')}" autocomplete="off">`, tr('rp.notify.t.username'))
        + secret('password', tr('rp.notify.f.password'), tr('rp.notify.t.password'))
        + nfield(tr('rp.notify.f.from'), `<input name="from" value="${v('from')}">`, tr('rp.notify.t.from'))
        + nfield(tr('rp.notify.f.to'), `<input name="to" value="${esc((c.to || []).join(', '))}">`, tr('rp.notify.t.to'));
    } else {
      f = nfield(tr('rp.notify.f.url'), `<input name="url" value="${v('url')}" placeholder="https://...">`, tr('rp.notify.t.url'))
        + nfield(tr('rp.notify.f.secretHeader'), `<input name="secret_header_name" value="${esc(Object.keys(c.secret_headers || {})[0] || '')}" placeholder="Authorization">`, tr('rp.notify.t.secretHeaderName'))
        + nfield(tr('rp.notify.f.secretValue'), `<input type="password" name="secret_header_value" autocomplete="new-password" value="${Object.keys(c.secret_headers || {}).length ? MASK : ''}">`, tr('rp.notify.t.secretHeaderValue'));
    }
    return `<fieldset class="fk-step rp-channel" data-rp-channel="${i}" data-kind="${esc(c.kind)}" data-id="${esc(c.id || '')}"${canEdit ? '' : ' disabled'}>
      <div class="fk-step-head"><b>${esc(KIND_LABEL[c.kind] ? KIND_LABEL[c.kind]() : c.kind)}</b>
        ${nfield(tr('rp.notify.f.name'), `<input name="name" value="${v('name')}" maxlength="60">`, tr('rp.notify.t.name'))}
        <button type="button" class="btn btn-sm btn-secondary tip" data-rpn="test" data-tip="${esc(c.id ? tr('rp.notify.testTip') : tr('rp.notify.saveFirst'))}"${c.id ? '' : ' disabled'}>${icon('test')} ${esc(tr('rp.notify.test'))}</button>
        <button type="button" class="btn-icon-sm tip" data-rpn="del" data-tip="${esc(tr('rp.notify.delTip'))}" aria-label="${esc(tr('rp.notify.delTip'))}">×</button></div>
      <div class="rp-channel-fields">${f}</div></fieldset>`;
  }
  /** Lit un canal du formulaire ; un secret inchangé repart masqué (gardé par le serveur). */
  function readChannel(fs) {
    const val = (n) => { const el = fs.querySelector(`[name="${n}"]`); return el ? el.value.trim() : ''; };
    const c = { id: fs.dataset.id || undefined, kind: fs.dataset.kind, name: val('name') };
    if (c.kind === 'matrix') Object.assign(c, { homeserver: val('homeserver'), room: val('room'), token: val('token') });
    else if (c.kind === 'slack') c.url = val('url');
    else if (c.kind === 'email') {
      Object.assign(c, { smtp_host: val('smtp_host'), smtp_port: Number(val('smtp_port') || 587), // TLS implicite l'emporte : les deux ensemble sont refusés par le serveur
        starttls: fs.querySelector('[name="starttls"]').checked && !fs.querySelector('[name="implicit_tls"]').checked,
        implicit_tls: fs.querySelector('[name="implicit_tls"]').checked,
        username: val('username'), password: val('password'), from: val('from'), to: val('to').split(',').map((x) => x.trim()).filter(Boolean) });
    } else {
      c.url = val('url');
      const hn = val('secret_header_name');
      c.headers = {};
      c.secret_headers = hn ? { [hn]: val('secret_header_value') } : {};
    }
    // un canal neuf reçoit son identifiant ici (le serveur l'exige, stable ensuite)
    if (!c.id) c.id = `c-${Date.now().toString(36)}-${Math.floor(Math.random() * 1296).toString(36)}`;
    return c;
  }
  async function openNotify() {
    const panel = FloatingPanels.open({ id: 'rp-notify', icon: 'news', width: 720, height: 680, title: tr('rp.notify.title'),
      bodyHtml: `<div class="of-form rp-notify"><p class="form-hint">${esc(tr('common.loading'))}</p></div>` });
    const root = panel.el.querySelector('.rp-notify');
    const [d, p] = await Promise.all([U.get('/api/replication/notify'), U.get('/api/replication/plans')]);
    const role = (p && p.role) || 'viewer';
    if (d.error) { root.innerHTML = `<p class="res-error">${esc(d.error)}</p>`; return; }
    const canEdit = role === 'admin';
    // GET /api/replication/notify : {notify: <réglage masqué>, codes, kinds}
    const st = { cfg: JSON.parse(JSON.stringify(d.notify || d)) };
    st.cfg.channels = st.cfg.channels || [];
    const draw = () => {
      root.innerHTML = `<p class="hint">${esc(tr('rp.notify.hint'))}</p>
        ${canEdit ? '' : `<div class="sto-finding sev-info"><div class="sto-finding-title">${icon('info')} ${esc(tr('rp.notify.adminOnly'))}</div></div>`}
        <div class="btn-row">
          <label class="bk-check tip" data-tip="${esc(tr('rp.notify.t.enabled'))}"><input type="checkbox" name="enabled"${st.cfg.enabled ? ' checked' : ''}${canEdit ? '' : ' disabled'}> <span>${esc(tr('rp.notify.enabled'))}</span></label>
          ${nfield(tr('rp.notify.f.level'), `<select name="min_level"${canEdit ? '' : ' disabled'}><option value="warn"${st.cfg.min_level !== 'error' ? ' selected' : ''}>${esc(tr('rp.notify.level.warn'))}</option>
            <option value="error"${st.cfg.min_level === 'error' ? ' selected' : ''}>${esc(tr('rp.notify.level.error'))}</option></select>`, tr('rp.notify.t.level'))}
        </div>
        <div data-rpn="channels">${st.cfg.channels.map((c, i) => channelHtml(c, i, canEdit)).join('') || `<p class="hint">${esc(tr('rp.notify.none'))}</p>`}</div>
        <div class="btn-row">${nfield(tr('rp.notify.f.kind'), `<select data-rpn="kind"${canEdit ? '' : ' disabled'}>${KINDS.map((k) => `<option value="${k}">${esc(KIND_LABEL[k]())}</option>`).join('')}</select>`, tr('rp.notify.t.kind'))}
          <button type="button" class="btn btn-sm btn-secondary tip" data-rpn="add" data-tip="${esc(tr('rp.notify.addTip'))}"${canEdit ? '' : ' disabled'}>${icon('add')} ${esc(tr('rp.notify.add'))}</button></div>
        <div class="bk-form-actions"><button type="button" class="btn btn-sm btn-primary tip" data-rpn="save" data-tip="${esc(canEdit ? tr('rp.notify.saveTip') : tr('rp.notify.adminOnly'))}"${canEdit ? '' : ' disabled'}>${icon('save')} ${esc(tr('rp.notify.save'))}</button></div>
        <div class="of-msg" data-rpn="msg" role="status"></div>`;
    };
    const collect = () => {
      st.cfg.enabled = root.querySelector('[name="enabled"]').checked;
      st.cfg.min_level = root.querySelector('[name="min_level"]').value;
      st.cfg.channels = [...root.querySelectorAll('[data-rp-channel]')].map(readChannel);
    };
    draw();
    root.addEventListener('click', async (e) => {
      const b = e.target.closest('[data-rpn]');
      if (!b || b.disabled || b.tagName === 'SELECT') return;
      const msg = root.querySelector('[data-rpn="msg"]');
      if (b.dataset.rpn === 'add') { collect(); st.cfg.channels.push({ kind: root.querySelector('[data-rpn="kind"]').value }); draw(); return; }
      if (b.dataset.rpn === 'del') { collect(); st.cfg.channels.splice(Number(b.closest('[data-rp-channel]').dataset.rpChannel), 1); draw(); return; }
      if (b.dataset.rpn === 'save') {
        collect();
        b.disabled = true;
        try {
          const out = await U.api('PUT', '/api/replication/notify', st.cfg);
          U.follow(out.action_id, msg, tr('rp.notify.saved'), () => { b.disabled = false; });
        } catch (err) { b.disabled = false; msg.innerHTML = `<span class="res-error">${icon('fail')} ${esc(err.message)}</span>`; }
        return;
      }
      if (b.dataset.rpn === 'test') {
        const id = b.closest('[data-rp-channel]').dataset.id;
        try {
          const out = await U.api('POST', '/api/replication/notify/test', { channel: id });
          U.follow(out.action_id, msg, tr('rp.notify.tested'));
        } catch (err) { msg.innerHTML = `<span class="res-error">${icon('fail')} ${esc(err.message)}</span>`; }
      }
    });
    return root;
  }

  return { start, stop, load, startLinks, stopLinks, loadLinks, startBadges, refreshBadges, paintBadges,
           eventRows, openNotify, alertText };
})();
window.Replication = Replication;
