/**
 * harvester-ops : Shared resources > Bookings (v1.94.0).
 *
 * Réserver des unités GPU d'un cluster (un GPU entier ou un vGPU précis, ou
 * un nombre de vGPU d'un type, d'instances MIG d'un profil, de GPU de
 * conteneurs) pour une personne ou un groupe, sur un créneau. La console
 * garde la réservation (bookings.db) ; rien n'est écrit sur le cluster en
 * version 1, et la vue le dit.
 *
 * Trois vues : Calendrier (une ligne par type et modèle de chaque cluster,
 * une semaine, les créneaux en barres, la marque d'aujourd'hui), Mes
 * réservations, Toutes les réservations (annuler avec une confirmation dans
 * la page). Le formulaire « Nouvelle réservation » contrôle le créneau à
 * blanc pendant la saisie (/api/bookings/check) et dit pourquoi il serait
 * refusé. Créer et annuler sont des actions suivies (dock, Activity).
 *
 * Utilisé aussi par l'inventaire GPU (badges, bouton Réserver), le
 * formulaire de création de charge et la fenêtre de modification d'une VM
 * (disponibilité, réservations en cours) : Bookings.badges, Bookings.open,
 * Bookings.hint.
 */
const Bookings = (() => {
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const enc = encodeURIComponent;
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 13) => (window.Icons ? Icons.svg(n, { size }) : '');
  const REFRESH_MS = 30000;
  const STORE = 'harvester_ops_bookings';
  const DAY_MS = 86400000;
  const SOON_MS = DAY_MS;
  const TYPES = ['gpu', 'vgpu', 'mig', 'node'];
  const VIEWS = ['calendar', 'mine', 'all'];
  // Clés en toutes lettres : le contrôle de parité i18n ne lit que des littéraux.
  const TYPE_LABEL = { gpu: () => tr('gpu.type.gpu'), vgpu: () => tr('gpu.type.vgpu'),
                       mig: () => tr('gpu.type.mig'), node: () => tr('gpu.type.node') };
  const STATE_LABEL = { upcoming: () => tr('bkg.state.upcoming'), active: () => tr('bkg.state.active'),
                        ended: () => tr('bkg.state.ended'), cancelled: () => tr('bkg.state.cancelled') };
  const STATE_BADGE = { upcoming: 'info', active: 'ok', ended: 'dim', cancelled: 'dim' };
  const VIEW_LABEL = { calendar: () => tr('bkg.view.calendar'), mine: () => tr('bkg.view.mine'), all: () => tr('bkg.view.all') };
  const VIEW_TIP = { calendar: () => tr('bkg.view.calendarTip'), mine: () => tr('bkg.view.mineTip'), all: () => tr('bkg.view.allTip') };
  const VIEW_ICON = { calendar: 'timer', mine: 'user', all: 'activity' };

  const typeLabel = (t) => (TYPE_LABEL[t] ? TYPE_LABEL[t]() : t);
  const badge = (cls, text, tip) =>
    `<span class="badge ${cls}${tip ? ' tip' : ''}"${tip ? ` data-tip="${esc(tip)}"` : ''}>${esc(text)}</span>`;
  const lang = () => (window.i18n && i18n.currentLang) || undefined;
  const when = (iso) => {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? String(iso || '') : d.toLocaleString(lang(), { dateStyle: 'short', timeStyle: 'short' });
  };
  const holderText = (h) => (h ? (h.kind === 'group' ? tr('bkg.holderGroup', { name: h.name }) : h.name) : '');
  const whatText = (b) => (b.unit ? tr('bkg.whatUnit', { unit: b.unit, model: b.model })
    : tr('bkg.whatCount', { n: b.count, model: b.model }));
  const targetText = (t) => (t ? `${t.kind === 'vm' ? tr('bkg.target.vm') : tr('bkg.target.workload')} ${t.namespace}/${t.name}` : '');

  // -- la liste partagée (vue, badges de l'inventaire, indications) -----------
  let shared = { at: 0, promise: null, data: null };
  function list(fresh) {
    if (!fresh && shared.promise && Date.now() - shared.at < 10000) return shared.promise;
    shared.at = Date.now();
    shared.promise = fetch('/api/bookings').then(async (r) => {
      const d = await r.json().catch(() => null);
      if (!r.ok || !d) throw new Error((d && (d.hint || d.error)) || `HTTP ${r.status}`);
      shared.data = d;
      return d;
    }).catch((e) => { shared.at = 0; return { error: e.message, bookings: [] }; });
    return shared.promise;
  }

  // -- lots réservables (type et modèle d'un cluster) ----------------------------
  // `pools` vient de /api/gpus-all (même calcul de capacité que le formulaire).
  const poolKey = (cluster, type, model) => `${cluster}|${type}|${model}`;
  /** Unités d'un lot réservées à l'instant `t` (ms) : réservations par nombre
   *  et réservations d'une unité précise, non annulées (comme le serveur). */
  function bookedAt(bookings, cluster, type, model, t) {
    return (bookings || []).reduce((n, b) => n + (b.cluster === cluster && b.type === type && b.model === model
      && b.state !== 'cancelled' && !b.cancelled_at && Date.parse(b.start) <= t && t < Date.parse(b.end)
      ? Number(b.count) || 1 : 0), 0);
  }
  /** Libres maintenant : la capacité moins le plus grand de « utilisées » et
   *  « réservées » (le calcul de /api/bookings/options). */
  const freeOf = (pool, booked) => Math.max(0, pool.capacity - Math.max(pool.used || 0, booked));
  function inventory() {
    return fetch('/api/gpus-all').then(async (r) => {
      const d = await r.json().catch(() => null);
      return d && Array.isArray(d.pools) ? d.pools : null;
    }).catch(() => null);
  }

  let cur = null;   // { host, timer, data, prefs, pools }

  function loadPrefs() {
    const p = { view: 'calendar', week: 0, cluster: '' };
    try { Object.assign(p, JSON.parse(localStorage.getItem(STORE) || '{}')); } catch { /* réglages illisibles */ }
    if (!VIEWS.includes(p.view)) p.view = 'calendar';
    p.week = 0;                       // toujours la semaine en cours à l'ouverture
    return p;
  }
  function savePrefs() {
    try { localStorage.setItem(STORE, JSON.stringify({ view: cur.prefs.view, cluster: cur.prefs.cluster })); } catch { /* stockage indisponible */ }
  }

  // -- cycle de vie (appelé par App.setTab) -------------------------------------
  function start(host) {
    stop();
    if (!host) return Promise.resolve();
    cur = { host, data: null, prefs: loadPrefs(), confirm: null };
    shell();
    cur.timer = setInterval(() => {
      if (cur && cur.host.isConnected && !cur.host.closest('[hidden]') && !document.hidden && !cur.confirm) load();
    }, REFRESH_MS);
    return load();
  }

  function stop() {
    if (cur && cur.timer) clearInterval(cur.timer);
    cur = null;
  }

  async function load() {
    if (!cur) return;
    const c = cur;
    // l'inventaire (plus lent : tous les clusters) arrive après la liste
    const inv = inventory();
    const d = await list(true);
    if (c !== cur) return;
    c.data = d;
    render();
    const pools = await inv;
    if (c !== cur) return;
    if (pools) { c.pools = pools; render(); }
  }

  function shell() {
    const p = cur.prefs;
    cur.host.innerHTML = `<div class="card na-card bkg-card">
        <div class="sto-finding sev-info bkg-notice"><div class="sto-finding-title">${icon('info')} ${esc(tr('bkg.notEnforced'))}</div></div>
        <div class="res-tools">
          <div class="sub-tabs sub-tabs-inline bkg-views" role="tablist">
            ${VIEWS.map((v) => `<button type="button" class="sub-tab tip${v === p.view ? ' active' : ''}" role="tab" aria-selected="${v === p.view}"
              data-bkg-view="${v}" data-tip="${esc(VIEW_TIP[v]())}">${icon(VIEW_ICON[v])} <span>${esc(VIEW_LABEL[v]())}</span></button>`).join('')}
          </div>
          <select class="tip" data-bkg="cluster" data-tip="${esc(tr('bkg.t.cluster'))}"><option value="">${esc(tr('avm.filter.allClusters'))}</option></select>
          <span class="res-count"></span>
          <button type="button" class="btn btn-sm btn-secondary tip" data-bkg="refresh" data-tip="${esc(tr('res.refreshTip'))}">${icon('refresh')} ${esc(tr('overview.refresh'))}</button>
          <button type="button" class="btn btn-sm btn-primary tip bkg-new" data-bkg="new" data-tip="${esc(tr('bkg.newTip'))}">${icon('add')} ${esc(tr('bkg.new'))}</button>
        </div>
        <div data-bkg="body"><p class="form-hint">${esc(tr('common.loading'))}</p></div></div>`;
    const card = cur.host.querySelector('.bkg-card');
    card.addEventListener('click', onClick);
    card.querySelector('[data-bkg="cluster"]').addEventListener('change', (e) => {
      cur.prefs.cluster = e.target.value; savePrefs(); render();
    });
  }

  function fillClusters(bookings) {
    const sel = cur.host.querySelector('[data-bkg="cluster"]');
    const names = [...new Set(clusterNames().concat(bookings.map((b) => b.cluster)))].sort();
    const want = [''].concat(names).join('|');
    if ([...sel.options].map((o) => o.value).join('|') !== want) {
      sel.innerHTML = `<option value="">${esc(tr('avm.filter.allClusters'))}</option>`
        + names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
    }
    if (!names.includes(cur.prefs.cluster)) cur.prefs.cluster = '';
    sel.value = cur.prefs.cluster;
  }

  function render() {
    if (!cur || !cur.host.isConnected) return;
    const body = cur.host.querySelector('[data-bkg="body"]');
    const d = cur.data;
    cur.host.querySelectorAll('[data-bkg-view]').forEach((b) => {
      const on = b.dataset.bkgView === cur.prefs.view;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', String(on));
    });
    if (!d || d.error) {
      body.innerHTML = `<p class="res-error">${esc(d && d.error ? d.error : tr('fabric.unreachable'))}</p>`;
      return;
    }
    const all = d.bookings || [];
    fillClusters(all);
    const inCluster = all.filter((b) => !cur.prefs.cluster || b.cluster === cur.prefs.cluster);
    const canBook = d.role === 'operator' || d.role === 'admin';
    const nb = cur.host.querySelector('[data-bkg="new"]');
    nb.disabled = !canBook;
    nb.dataset.tip = canBook ? tr('bkg.newTip') : tr('bkg.viewerTip');
    if (cur.prefs.view === 'calendar') {
      cur.host.querySelector('.res-count').textContent = '';
      const pools = (cur.pools || []).filter((p) => !cur.prefs.cluster || p.cluster === cur.prefs.cluster);
      body.innerHTML = calendarHtml(inCluster, d.now, pools);
      return;
    }
    const rows = cur.prefs.view === 'mine' ? inCluster.filter((b) => b.mine) : inCluster;
    // les plus utiles d'abord : en cours, à venir, puis l'historique
    const rank = { active: 0, upcoming: 1, ended: 2, cancelled: 3 };
    rows.sort((a, b) => (rank[a.state] - rank[b.state]) || a.start.localeCompare(b.start));
    cur.host.querySelector('.res-count').textContent = tr('bkg.count', { n: rows.length });
    body.innerHTML = rows.length ? tableHtml(rows) : `<p class="form-hint bkg-empty">${esc(cur.prefs.view === 'mine' ? tr('bkg.noneMine') : tr('bkg.none'))}</p>`;
  }

  // -- tableau --------------------------------------------------------------------
  function tableHtml(rows) {
    const th = (t) => `<th>${esc(t)}</th>`;
    return `<table class="data-table bkg-table"><thead><tr>${th(tr('gpu.col.cluster'))}${th(tr('gpu.col.type'))}${th(tr('bkg.col.what'))}
        ${th(tr('bkg.col.holder'))}${th(tr('bkg.col.start'))}${th(tr('bkg.col.end'))}${th(tr('gpu.col.state'))}${th(tr('bkg.col.target'))}${th(tr('bkg.col.actions'))}</tr></thead><tbody>
      ${rows.map((b) => `<tr data-booking="${esc(b.id)}" data-state="${esc(b.state)}">
        <td>${esc(b.cluster)}</td><td>${esc(typeLabel(b.type))}</td>
        <td><span class="tip" data-tip="${esc(b.note ? tr('bkg.t.note', { note: b.note }) : tr('bkg.t.what'))}">${esc(whatText(b))}</span></td>
        <td><span class="tip" data-tip="${esc(tr('bkg.t.createdBy', { who: b.created_by || '?', at: when(b.created_at) }))}">${esc(holderText(b.holder))}</span></td>
        <td>${esc(when(b.start))}</td><td>${esc(when(b.end))}</td>
        <td>${badge(STATE_BADGE[b.state] || 'dim', STATE_LABEL[b.state] ? STATE_LABEL[b.state]() : b.state,
          b.state === 'cancelled' ? tr('bkg.t.cancelledBy', { who: b.cancelled_by || '?', at: when(b.cancelled_at) }) : '')}</td>
        <td>${b.target ? esc(targetText(b.target)) : '<span class="res-dim">–</span>'}</td>
        <td class="bkg-actions">${b.can_cancel
          ? `<button type="button" class="btn btn-sm btn-secondary tip" data-bkg="cancel" data-id="${esc(b.id)}" data-tip="${esc(tr('bkg.cancelTip'))}">${icon('cancelled')} ${esc(tr('bkg.cancel'))}</button>`
          : '<span class="res-dim">–</span>'}</td></tr>`).join('')}
      </tbody></table>`;
  }

  function confirmCancel(btn) {
    const cell = btn.closest('td');
    const id = btn.dataset.id;
    const b = (cur.data.bookings || []).find((x) => x.id === id);
    cur.confirm = id;
    cell.innerHTML = `<div class="bkg-confirm" role="group" aria-label="${esc(tr('bkg.confirm'))}">
        <span>${esc(tr('bkg.confirm'))}</span>
        <button type="button" class="btn btn-sm btn-danger tip" data-bkg="cancel-yes" data-id="${esc(id)}" data-tip="${esc(tr('bkg.confirmYesTip', { what: b ? whatText(b) : id }))}">${icon('cancelled')} ${esc(tr('bkg.confirmYes'))}</button>
        <button type="button" class="btn btn-sm btn-secondary tip" data-bkg="cancel-no" data-tip="${esc(tr('bkg.confirmNoTip'))}">${esc(tr('bkg.confirmNo'))}</button>
        <div class="of-msg" role="status"></div></div>`;
    cell.querySelector('[data-bkg="cancel-no"]').focus();
  }

  async function doCancel(btn) {
    const id = btn.dataset.id;
    const box = btn.closest('.bkg-confirm');
    const msg = box.querySelector('.of-msg');
    box.querySelectorAll('button').forEach((x) => { x.disabled = true; });
    try {
      const out = await send('POST', `/api/bookings/${enc(id)}/cancel`, {});
      follow(out.action_id, msg, tr('bkg.done.cancel'), () => { if (cur) cur.confirm = null; load(); });
    } catch (err) {
      msg.innerHTML = `<span class="res-error">${icon('fail')} ${esc(err.message)}</span>`;
      box.querySelector('[data-bkg="cancel-no"]').disabled = false;
    }
  }

  // -- calendrier : une semaine, une ligne par type et modèle de chaque cluster --
  function weekStart(offset) {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    d.setDate(d.getDate() - ((d.getDay() + 6) % 7) + 7 * offset);     // lundi
    return d;
  }

  function calendarHtml(bookings, nowIso, inventoryPools) {
    const ws = weekStart(cur.prefs.week);
    const we = new Date(ws); we.setDate(we.getDate() + 7);
    const span = we - ws;
    const days = [...Array(7)].map((_, i) => { const x = new Date(ws); x.setDate(x.getDate() + i); return x; });
    const now = Date.parse(nowIso || '') || Date.now();
    const shown = bookings.filter((b) => b.state !== 'cancelled' && Date.parse(b.start) < we && Date.parse(b.end) > ws);
    // v1.94.1 : tous les lots réservables des clusters affichés, avec ou sans
    // réservation cette semaine, puis ceux qui n'ont plus que des réservations
    const pools = new Map();
    (inventoryPools || []).forEach((p) => {
      pools.set(poolKey(p.cluster, p.type, p.model), { cluster: p.cluster, type: p.type, model: p.model, inv: p, items: [] });
    });
    shown.forEach((b) => {
      const k = poolKey(b.cluster, b.type, b.model);
      if (!pools.has(k)) pools.set(k, { cluster: b.cluster, type: b.type, model: b.model, inv: null, items: [] });
      pools.get(k).items.push(b);
    });
    const fmtDay = (x) => x.toLocaleDateString(lang(), { weekday: 'short', day: 'numeric', month: 'numeric' });
    const range = `${days[0].toLocaleDateString(lang(), { day: 'numeric', month: 'short' })} – ${days[6].toLocaleDateString(lang(), { day: 'numeric', month: 'short', year: 'numeric' })}`;
    const todayPct = now >= ws && now < we ? ((now - ws) / span) * 100 : null;
    const marker = todayPct == null ? '' : `<div class="bkg-today tip" style="left:${todayPct.toFixed(3)}%" data-tip="${esc(tr('bkg.t.now'))}"></div>`;
    const head = `<div class="bkg-cal-nav">
        <button type="button" class="btn btn-sm btn-secondary tip" data-bkg="week-prev" data-tip="${esc(tr('bkg.prevTip'))}">${icon('chevronLeft')} ${esc(tr('bkg.prev'))}</button>
        <button type="button" class="btn btn-sm btn-secondary tip" data-bkg="week-today" data-tip="${esc(tr('bkg.todayTip'))}">${esc(tr('bkg.today'))}</button>
        <button type="button" class="btn btn-sm btn-secondary tip" data-bkg="week-next" data-tip="${esc(tr('bkg.nextTip'))}">${esc(tr('bkg.next'))} ${icon('chevronRight')}</button>
        <strong class="bkg-range">${esc(range)}</strong></div>`;
    const grid = `<div class="bkg-days">${days.map((x) => `<div class="bkg-day${x.getTime() <= now && now < x.getTime() + DAY_MS ? ' is-today' : ''}"></div>`).join('')}</div>`;
    const daysHead = `<div class="bkg-row bkg-row-head"><div class="bkg-label"></div><div class="bkg-track bkg-track-head">
        ${days.map((x) => `<div class="bkg-dayname${x.getTime() <= now && now < x.getTime() + DAY_MS ? ' is-today' : ''}">${esc(fmtDay(x))}</div>`).join('')}</div></div>`;
    if (!pools.size) {
      return `${head}<div class="bkg-cal">${daysHead}</div><p class="form-hint bkg-empty">${esc(tr('bkg.noneWeek'))}</p>`;
    }
    const rowsHtml = [...pools.values()].sort((a, b) => `${a.cluster}|${TYPES.indexOf(a.type)}|${a.model}`.localeCompare(`${b.cluster}|${TYPES.indexOf(b.type)}|${b.model}`)).map((p) => {
      // des voies pour les créneaux qui se chevauchent
      const lanes = [];
      const items = p.items.slice().sort((a, b) => a.start.localeCompare(b.start));
      const placed = items.map((b) => {
        let lane = lanes.findIndex((end) => end <= b.start);
        if (lane < 0) { lane = lanes.length; lanes.push(b.end); } else lanes[lane] = b.end;
        return { b, lane };
      });
      const bars = placed.map(({ b, lane }) => {
        const s = Math.max(Date.parse(b.start), ws.getTime());
        const e = Math.min(Date.parse(b.end), we.getTime());
        const left = ((s - ws) / span) * 100;
        const width = Math.max(0.6, ((e - s) / span) * 100);
        const tip = [tr('bkg.t.bar', { what: whatText(b), holder: holderText(b.holder), start: when(b.start), end: when(b.end) }),
          b.target ? targetText(b.target) : '', b.note || ''].filter(Boolean).join(' · ');
        return `<div class="bkg-bar bkg-${esc(b.state)}${b.mine ? ' is-mine' : ''} tip" data-booking="${esc(b.id)}"
            style="left:${left.toFixed(3)}%;width:${width.toFixed(3)}%;top:${4 + lane * 24}px" data-tip="${esc(tip)}">
            <span>${esc(holderText(b.holder))}${b.unit ? '' : ` ×${esc(b.count)}`}</span></div>`;
      }).join('');
      const h = 8 + Math.max(1, lanes.length) * 24;
      let free = '';
      if (p.inv) {
        const booked = bookedAt(bookings, p.cluster, p.type, p.model, now);
        const n = freeOf(p.inv, booked);
        free = `<div class="bkg-free-now tip${n ? '' : ' is-full'}" data-free="${n}" data-tip="${esc(tr('bkg.f.freeLine',
          { free: n, cap: p.inv.capacity, used: p.inv.used, booked }))}">${esc(tr('bkg.cal.free', { free: n, cap: p.inv.capacity }))}</div>`;
      }
      return `<div class="bkg-row${p.items.length ? '' : ' is-empty'}" data-pool="${esc(poolKey(p.cluster, p.type, p.model))}">
          <div class="bkg-label"><strong>${esc(p.model)}</strong><div class="res-dim">${esc(p.cluster)} · ${esc(typeLabel(p.type))}</div>${free}</div>
          <div class="bkg-track" style="min-height:${h}px">${grid}${bars}${marker}</div></div>`;
    }).join('');
    // des lots sans aucune réservation cette semaine : les lignes vides le
    // montrent, la phrase le dit
    const none = shown.length ? '' : `<p class="form-hint bkg-empty">${esc(tr('bkg.noneWeek'))}</p>`;
    return `${head}<div class="bkg-cal">${daysHead}${rowsHtml}</div>${none}`;
  }

  // -- envoi et suivi ---------------------------------------------------------------
  async function send(method, url, body) {
    const r = await fetch(url, {
      method, headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let d = {};
    try { d = await r.json(); } catch { /* sans corps */ }
    if (!r.ok) {
      const err = new Error(d.hint || d.error || d.detail || `HTTP ${r.status}`);
      err.data = d;
      err.status = r.status;
      throw err;
    }
    return d;
  }

  function follow(actionId, msg, doneText, onDone) {
    const end = (ok) => { list(true); if (onDone) onDone(ok); };
    if (window.VMActions && VMActions.follow) VMActions.follow(actionId, msg, doneText, end);
    else { if (msg) msg.textContent = tr('bk.started', { id: actionId }); end(true); }
  }

  function clusterNames() {
    return window.App && App.listClusters ? App.listClusters() : [];
  }

  function onClick(e) {
    const b = e.target.closest('[data-bkg], [data-bkg-view]');
    if (!b || !cur || b.tagName === 'SELECT') return;
    if (b.dataset.bkgView) { cur.prefs.view = b.dataset.bkgView; cur.confirm = null; savePrefs(); render(); return; }
    const act = b.dataset.bkg;
    if (act === 'refresh') { load(); return; }
    if (act === 'new') { open({ cluster: cur.prefs.cluster }); return; }
    if (act === 'week-prev') { cur.prefs.week -= 1; render(); return; }
    if (act === 'week-next') { cur.prefs.week += 1; render(); return; }
    if (act === 'week-today') { cur.prefs.week = 0; render(); return; }
    if (act === 'cancel') { confirmCancel(b); return; }
    if (act === 'cancel-no') { cur.confirm = null; render(); return; }
    if (act === 'cancel-yes') doCancel(b);
  }

  // -- le formulaire « Nouvelle réservation » ----------------------------------------
  const pad = (n) => String(n).padStart(2, '0');
  const localInput = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const field = (label, input, tip) => `<label class="bk-field"><span>${esc(label)}</span>${
    input.replace(/^<(input|select|textarea)/, `<$1 class="tip" data-tip="${esc(tip)}"`)}</label>`;

  async function getJson(url) {
    try {
      const r = await fetch(url);
      const d = await r.json().catch(() => null);
      if (!r.ok || !d) return { error: (d && (d.hint || d.error)) || `HTTP ${r.status}` };
      return d;
    } catch (e) { return { error: e.message }; }
  }

  /** Ouvre le formulaire, prérempli : {cluster, type, unit, model, count}. */
  function open(pre = {}) {
    if (!window.FloatingPanels) return null;
    const names = clusterNames();
    const cl = pre.cluster || (window.App && App.getCurrentCluster && App.getCurrentCluster()) || names[0] || '';
    const start = new Date(Date.now() + 3600000); start.setSeconds(0, 0);
    const end = new Date(start.getTime() + 4 * 3600000);
    const me = (shared.data && shared.data.user) || '';
    const bodyHtml = `
      <p class="form-hint bkg-form-notice">${icon('info')} ${esc(tr('bkg.notEnforced'))}</p>
      <div class="ct-create-row">
        ${field(tr('col.cluster'), `<select name="cluster">${(names.length ? names : [cl]).map((c) => `<option value="${esc(c)}"${c === cl ? ' selected' : ''}>${esc(c)}</option>`).join('')}</select>`, tr('bkg.f.clusterTip'))}
        ${field(tr('gpu.col.type'), `<select name="type">${TYPES.map((t) => `<option value="${t}"${t === (pre.type || 'vgpu') ? ' selected' : ''}>${esc(typeLabel(t))}</option>`).join('')}</select>`, tr('bkg.f.typeTip'))}
      </div>
      ${field(tr('bkg.f.what'), '<select name="what"><option value="">…</option></select>', tr('bkg.f.whatTip'))}
      <p class="form-hint bkg-free" data-bkg-free></p>
      ${field(tr('bkg.f.count'), '<input type="number" name="count" min="1" max="64" step="1" value="1">', tr('bkg.f.countTip'))}
      <div class="ct-create-row">
        ${field(tr('bkg.f.holderKind'), `<select name="holder_kind"><option value="me">${esc(tr('bkg.f.me', { me: me || '?' }))}</option>
          <option value="user">${esc(tr('bkg.f.person'))}</option><option value="group">${esc(tr('bkg.f.group'))}</option></select>`, tr('bkg.f.holderKindTip'))}
        ${field(tr('bkg.f.holder'), '<input name="holder" maxlength="128" disabled>', tr('bkg.f.holderTip'))}
      </div>
      <div class="ct-create-row">
        ${field(tr('bkg.col.start'), `<input type="datetime-local" name="start" value="${localInput(start)}">`, tr('bkg.f.startTip'))}
        ${field(tr('bkg.col.end'), `<input type="datetime-local" name="end" value="${localInput(end)}">`, tr('bkg.f.endTip'))}
      </div>
      ${field(tr('bkg.col.target'), `<select name="target"><option value="">${esc(tr('bkg.f.noTarget'))}</option></select>`, tr('bkg.f.targetTip'))}
      ${field(tr('bkg.f.note'), '<textarea name="note" rows="2" maxlength="500"></textarea>', tr('bkg.f.noteTip'))}
      <div class="bkg-check" data-bkg-check role="status" aria-live="polite"></div>`;
    const panel = FloatingPanels.open({
      id: 'bkg-new', icon: 'timer', width: 600, height: 760, title: tr('bkg.new'),
      bodyHtml: `<form class="of-form ct-form bkg-form" autocomplete="off" novalidate>${bodyHtml}
        <div class="bk-form-actions"><button type="submit" class="btn btn-sm btn-primary tip" data-bkg-submit
          data-tip="${esc(tr('bkg.f.goTip'))}">${icon('add')} ${esc(tr('bkg.f.go'))}</button></div>
        <div class="of-msg" role="status"></div></form>`,
    });
    const root = panel.el;
    const f = root.querySelector('form');
    if (root.dataset.bkgReady) { prefill(root, pre); return root; }
    root.dataset.bkgReady = '1';
    const st = { options: null, cluster: null, timer: null, seq: 0 };
    root._bkg = st;
    const msg = root.querySelector('.of-msg');

    const fillWhat = () => {
      const typ = f.type.value;
      const cat = ((st.options && st.options.catalog) || []).filter((e) => e.type === typ);
      const opts = [];
      cat.forEach((e) => {
        opts.push(`<option value="m:${esc(e.model)}">${esc(tr('bkg.f.anyOf', { model: e.model, free: e.free_now, cap: e.capacity }))}</option>`);
        (e.units || []).forEach((u) => {
          opts.push(`<option value="u:${esc(u.id)}">${esc(tr('bkg.f.oneUnit', { unit: u.address || u.name, node: u.node || '?', model: e.model }))}${u.booked_now ? ` · ${esc(tr('bkg.bookedNow'))}` : ''}</option>`);
        });
      });
      const keep = f.what.value;
      f.what.innerHTML = opts.length ? opts.join('') : `<option value="">${esc(st.options && st.options.error ? st.options.error : tr('bkg.f.nothing'))}</option>`;
      if ([...f.what.options].some((o) => o.value === keep)) f.what.value = keep;
      syncCount();
    };
    const syncCount = () => {
      const unit = f.what.value.startsWith('u:');
      f.count.disabled = unit;
      if (unit) f.count.value = '1';
      const typ = f.type.value;
      const e = ((st.options && st.options.catalog) || []).find((x) => x.type === typ && (`m:${x.model}` === f.what.value
        || (x.units || []).some((u) => `u:${u.id}` === f.what.value)));
      const free = root.querySelector('[data-bkg-free]');
      free.textContent = e ? tr('bkg.f.freeLine', { free: e.free_now, cap: e.capacity, used: e.used, booked: e.booked_now })
        + (e.mine_now && e.mine_now.length ? ` ${tr('bkg.f.youHold', { n: e.mine_now.length })}` : '') : '';
    };
    const loadCluster = async () => {
      const c = f.cluster.value;
      st.cluster = c;
      st.options = null;
      f.what.innerHTML = `<option value="">${esc(tr('common.loading'))}</option>`;
      const [opts, vms, cts] = await Promise.all([getJson(`/api/bookings/options/${enc(c)}`),
        getJson(`/api/vms/${enc(c)}`), getJson(`/api/containers/${enc(c)}`)]);
      if (st.cluster !== c) return;
      st.options = opts;
      fillWhat();
      const tg = [];
      (vms.vms || []).forEach((v) => tg.push(`<option value="vm:${esc(v.namespace)}/${esc(v.name)}">${esc(tr('bkg.target.vm'))} ${esc(v.namespace)}/${esc(v.name)}</option>`));
      (cts.workloads || []).forEach((w) => tg.push(`<option value="workload:${esc(w.namespace)}/${esc(w.name)}">${esc(tr('bkg.target.workload'))} ${esc(w.namespace)}/${esc(w.name)} (${esc(w.kind)})</option>`));
      f.target.innerHTML = `<option value="">${esc(tr('bkg.f.noTarget'))}</option>${tg.join('')}`;
      if (st.pending) { prefill(root, st.pending); st.pending = null; }
      scheduleCheck();
    };
    st.loadCluster = loadCluster;
    st.fillWhat = fillWhat;

    f.cluster.addEventListener('change', loadCluster);
    f.type.addEventListener('change', () => { fillWhat(); scheduleCheck(); });
    f.what.addEventListener('change', () => { syncCount(); scheduleCheck(); });
    f.holder_kind.addEventListener('change', () => {
      f.holder.disabled = f.holder_kind.value === 'me';
      if (!f.holder.disabled) f.holder.focus();
      scheduleCheck();
    });
    ['count', 'start', 'end', 'holder'].forEach((n) => f[n].addEventListener('input', scheduleCheck));

    function payload() {
      const what = f.what.value;
      const p = { cluster: f.cluster.value, type: f.type.value, count: Number(f.count.value || 1) };
      if (what.startsWith('u:')) { p.unit = what.slice(2); p.count = 1; } else p.model = what.slice(2);
      const s = new Date(f.start.value), e = new Date(f.end.value);
      p.start = Number.isNaN(s.getTime()) ? '' : s.toISOString();
      p.end = Number.isNaN(e.getTime()) ? '' : e.toISOString();
      if (f.holder_kind.value !== 'me') p.holder = { kind: f.holder_kind.value, name: f.holder.value.trim() };
      if (f.target.value) {
        const [kind, ref] = [f.target.value.split(':')[0], f.target.value.slice(f.target.value.indexOf(':') + 1)];
        const [namespace, name] = ref.split('/');
        p.target = { kind, namespace, name };
      }
      if (f.note.value.trim()) p.note = f.note.value.trim();
      return p;
    }
    /** Ce qui se voit sans le serveur : un message par défaut. */
    function localErrors(p) {
      const errs = [];
      if (!f.what.value) errs.push(tr('bkg.err.what'));
      if (!p.start || !p.end) errs.push(tr('bkg.err.dates'));
      else if (Date.parse(p.end) <= Date.parse(p.start)) errs.push(tr('bkg.err.order'));
      else if (Date.parse(p.end) - Date.parse(p.start) > 90 * DAY_MS) errs.push(tr('bkg.err.long'));
      if (!Number.isInteger(p.count) || p.count < 1 || p.count > 64) errs.push(tr('bkg.err.count'));
      if (p.holder && !p.holder.name) errs.push(tr('bkg.err.holder'));
      return errs;
    }
    function reasonText(r) {
      if (r.code === 'unit-taken') return tr('bkg.why.unit', { holder: holderText(r.holder), start: when(r.start), end: when(r.end) });
      if (r.code === 'capacity') return tr('bkg.why.capacity', { peak: r.peak, cap: r.capacity, model: r.model, at: when(r.at) });
      // v1.95.0 : le quota du titulaire (Shared resources > Usage)
      if (r.code === 'quota-hours') return tr('bkg.why.quotaHours', { holder: holderText(r.holder), used: r.used, month: r.month, adding: r.adding, cap: r.cap });
      if (r.code === 'quota-units') return tr('bkg.why.quotaUnits', { holder: holderText(r.holder), peak: r.peak, at: when(r.at), max: r.max });
      return r.message || r.code;
    }
    function showCheck(state, lines) {
      const el = root.querySelector('[data-bkg-check]');
      el.dataset.state = state;
      if (state === 'idle') { el.innerHTML = ''; return; }
      const ico = state === 'ok' ? 'ok' : (state === 'busy' ? 'pending' : 'fail');
      el.innerHTML = `<div class="sto-finding ${state === 'ok' ? 'sev-info' : 'sev-action'}"><div class="sto-finding-title">${icon(ico)} ${esc(
        state === 'ok' ? tr('bkg.check.ok') : (state === 'busy' ? tr('bkg.check.busy') : tr('bkg.check.refused')))}</div>
        ${lines.length ? `<ul class="bkg-why">${lines.map((l) => `<li>${esc(l)}</li>`).join('')}</ul>` : ''}</div>`;
    }
    function scheduleCheck() {
      clearTimeout(st.timer);
      st.timer = setTimeout(check, 350);
    }
    async function check() {
      const p = payload();
      const errs = localErrors(p);
      if (errs.length) { showCheck('refused', errs); return false; }
      const seq = ++st.seq;
      showCheck('busy', []);
      try {
        const d = await send('POST', '/api/bookings/check', p);
        if (seq !== st.seq) return null;
        showCheck(d.ok ? 'ok' : 'refused', d.ok ? [tr('bkg.check.capacity', { cap: d.capacity })] : d.conflicts.map(reasonText));
        return d.ok;
      } catch (err) {
        if (seq !== st.seq) return null;
        showCheck('refused', [err.message]);
        return false;
      }
    }
    st.check = scheduleCheck;

    f.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = f.querySelector('[data-bkg-submit]');
      const p = payload();
      const errs = localErrors(p);
      if (errs.length) { showCheck('refused', errs); return; }
      btn.disabled = true;
      try {
        const out = await send('POST', '/api/bookings', p);
        // v1.94.1 : acceptée, la fenêtre se ferme ; l'action reste dans le
        // dock et sa fin s'annonce par une notification
        clearTimeout(st.timer);
        st.seq += 1;
        FloatingPanels.close('bkg-new');
        follow(out.action_id, null, tr('bkg.done.create'), (ok) => { if (ok && cur) load(); });
      } catch (err) {
        btn.disabled = false;
        if (err.status === 409 && err.data && err.data.conflicts) showCheck('refused', err.data.conflicts.map(reasonText));
        else msg.innerHTML = `<span class="res-error">${icon('fail')} ${esc(err.message)}</span>`;
      }
    });
    st.pending = pre;
    if (!shared.data) list().then((d) => { if (d && d.user) f.holder_kind.options[0].textContent = tr('bkg.f.me', { me: d.user }); });
    loadCluster();
    f.cluster.focus();
    return root;
  }

  /** Préremplit un formulaire déjà ouvert (ou à la fin de son chargement). */
  function prefill(root, pre) {
    const f = root.querySelector('form');
    const st = root._bkg;
    if (!pre || !st) return;
    if (pre.cluster && f.cluster.value !== pre.cluster) {
      f.cluster.value = pre.cluster;
      st.pending = pre;
      st.loadCluster();
      return;
    }
    if (!st.options) { st.pending = pre; return; }
    if (pre.type) { f.type.value = pre.type; st.fillWhat(); }
    const want = pre.unit ? `u:${pre.unit}` : (pre.model ? `m:${pre.model}` : '');
    if (want && [...f.what.options].some((o) => o.value === want)) f.what.value = want;
    f.what.dispatchEvent(new Event('change'));
    if (pre.count && !pre.unit) f.count.value = String(pre.count);
    st.check();
  }

  // -- pour l'inventaire GPU : réservations d'une unité, maintenant ou bientôt ----
  /** Les réservations vivantes qui touchent une unité de l'inventaire : la
   *  sienne précise, ou le modèle de son type (vGPU, profils MIG, ressource). */
  function forUnit(u, bookings, nowMs) {
    const now = nowMs || Date.now();
    const models = u.type === 'mig' ? (u.profiles || []).map((p) => p.name)
      : [u.type === 'node' ? (u.resource || u.model) : u.model];
    return (bookings || []).filter((b) => b.cluster === u.cluster && b.type === u.type
      && (b.state === 'active' || b.state === 'upcoming')
      && Date.parse(b.start) - now < SOON_MS
      && (b.unit ? b.unit === u.id : models.includes(b.model)));
  }

  /** Badges d'une unité de l'inventaire. Une réservation de CETTE unité :
   *  « Réservée par X jusqu'au ... ». Une réservation par nombre ne tient
   *  aucune unité en particulier (v1.94.1) : chaque unité du lot porte alors
   *  « n sur N réservées maintenant », jamais le nom du titulaire. `pools` :
   *  les lots de /api/gpus-all, pour la capacité N. */
  function badges(u, bookings, nowMs, pools) {
    const hits = forUnit(u, bookings, nowMs);
    if (!hits.length) return '';
    const now = nowMs || Date.now();
    const barTip = (b) => tr('bkg.t.bar', { what: whatText(b), holder: holderText(b.holder), start: when(b.start), end: when(b.end) });
    let out = '';
    const own = hits.filter((b) => b.unit);
    if (own.length) {
      const active = own.filter((b) => Date.parse(b.start) <= now);
      const first = (active[0] || own[0]);
      const text = active.length ? tr('bkg.badgeNow', { holder: holderText(first.holder), end: when(first.end) })
        : tr('bkg.badgeSoon', { holder: holderText(first.holder), start: when(first.start) });
      out += ` <span class="badge ${active.length ? 'info' : 'dim'} tip bkg-badge" data-bkg-badge="unit" data-tip="${esc(own.map(barTip).join(' · '))}">${icon('timer', 11)} ${esc(text)}${own.length > 1 ? ` +${own.length - 1}` : ''}</span>`;
    }
    // par lot (un profil MIG par lot ; un seul lot pour les autres types)
    const byModel = new Map();
    hits.filter((b) => !b.unit).forEach((b) => {
      if (!byModel.has(b.model)) byModel.set(b.model, []);
      byModel.get(b.model).push(b);
    });
    byModel.forEach((counted, model) => {
      const pool = (pools || []).find((p) => p.cluster === u.cluster && p.type === u.type && p.model === model);
      const cap = pool ? pool.capacity : '?';
      const activeNow = counted.some((b) => Date.parse(b.start) <= now);
      const at = activeNow ? now : Math.min(...counted.map((b) => Date.parse(b.start)));
      const n = bookedAt(bookings, u.cluster, u.type, model, at);
      const full = pool && n >= pool.capacity;
      const text = activeNow ? tr('bkg.badgePoolNow', { n, cap }) : tr('bkg.badgePoolSoon', { n, cap, start: when(new Date(at).toISOString()) });
      const tip = tr('bkg.t.pool', { model, list: counted.map(barTip).join(' · ') });
      out += ` <span class="badge ${activeNow ? (full ? 'warn' : 'info') : 'dim'} tip bkg-badge" data-bkg-badge="pool" data-tip="${esc(tip)}">${icon('timer', 11)} ${esc(text)}</span>`;
    });
    return out;
  }

  /** Préremplissage du formulaire depuis une ligne de l'inventaire. */
  function prefillFor(u) {
    if (u.type === 'gpu' || u.type === 'vgpu') return { cluster: u.cluster, type: u.type, unit: u.id };
    if (u.type === 'mig') {
      const p = (u.profiles || []).find((x) => x.created > 0) || (u.profiles || [])[0];
      return { cluster: u.cluster, type: 'mig', model: p ? p.name : '' };
    }
    return { cluster: u.cluster, type: 'node', model: u.resource || u.model };
  }

  // -- indications en lecture seule dans les formulaires de VM et de charge ------
  /** Remplit `el` : unités libres maintenant, réservations en cours, celles de
   *  la personne, et un lien vers Shared resources > Bookings. `types` : les
   *  types qui comptent pour ce formulaire. */
  async function hint(el, cluster, types) {
    if (!el || !cluster) return;
    el.dataset.cluster = cluster;
    el.innerHTML = `<span class="res-dim">${esc(tr('common.loading'))}</span>`;
    const d = await getJson(`/api/bookings/options/${enc(cluster)}`);
    if (el.dataset.cluster !== cluster) return;
    const link = `<a href="#bookings" class="tip" data-bkg-open data-tip="${esc(tr('bkg.hint.linkTip'))}">${icon('timer', 11)} ${esc(tr('bkg.hint.link'))}</a>`;
    if (d.error) { el.innerHTML = `${esc(tr('bkg.hint.unknown'))} ${link}`; return; }
    const cat = (d.catalog || []).filter((e) => types.includes(e.type));
    if (!cat.length) { el.innerHTML = `${esc(tr('bkg.hint.none', { cluster }))} ${link}`; return; }
    el.innerHTML = `<span class="bkg-hint-title">${esc(tr('bkg.hint.title'))}</span><ul class="bkg-hint-list">${cat.map((e) => `<li data-pool="${esc(`${e.type}|${e.model}`)}">
        <strong>${esc(typeLabel(e.type))} ${esc(e.model)}</strong> : ${esc(tr('bkg.hint.line', { free: e.free_now, cap: e.capacity, booked: e.booked_now }))}${
        e.mine_now && e.mine_now.length ? ` ${badge('ok', tr('bkg.hint.yours', { n: e.mine_now.length }), tr('bkg.hint.yoursTip'))}` : ''}</li>`).join('')}</ul>${link}`;
  }

  // un lien « Shared resources > Bookings » où qu'il soit
  document.addEventListener('click', (e) => {
    const a = e.target.closest && e.target.closest('[data-bkg-open]');
    if (!a) return;
    e.preventDefault();
    if (window.App && App.setTab) App.setTab('bookings');
  });

  return { start, stop, load, open, list, badges, forUnit, prefillFor, hint };
})();
window.Bookings = Bookings;
