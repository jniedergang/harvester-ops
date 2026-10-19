/**
 * harvester-ops : Shared resources > Usage (v1.95.0).
 *
 * Le décompte des GPU-heures sur une période (cette semaine, ce mois, le mois
 * précédent, une plage au choix) : par titulaire, les heures réservées
 * (bookings.db) et les heures consommées (relevées par l'échantillonneur de
 * la console dans usage.db), le gaspillage (réservé et pas utilisé) et le
 * hors règle (utilisé sans réservation), le quota du mois et ce qu'il en
 * reste. Une ligne se déplie sur son détail par cluster et type d'unité ; un
 * graphique (SVG simple, couleurs du thème) montre les GPU-heures par jour.
 * Export CSV par la console. Les administrateurs règlent les quotas dans une
 * fenêtre ; chaque changement est une action suivie (dock, Activity).
 */
const Usage = (() => {
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 13) => (window.Icons ? Icons.svg(n, { size }) : '');
  const REFRESH_MS = 60000;
  const STORE = 'harvester_ops_usage';
  const DAY_MS = 86400000;
  const PERIODS = ['week', 'month', 'last', 'custom'];
  // Clés en toutes lettres : le contrôle de parité i18n ne lit que des littéraux.
  const PERIOD_LABEL = { week: () => tr('usg.p.week'), month: () => tr('usg.p.month'),
                         last: () => tr('usg.p.last'), custom: () => tr('usg.p.custom') };
  const PERIOD_TIP = { week: () => tr('usg.p.weekTip'), month: () => tr('usg.p.monthTip'),
                       last: () => tr('usg.p.lastTip'), custom: () => tr('usg.p.customTip') };
  const TYPE_LABEL = { gpu: () => tr('gpu.type.gpu'), vgpu: () => tr('gpu.type.vgpu'),
                       mig: () => tr('gpu.type.mig'), node: () => tr('gpu.type.node') };
  const typeLabel = (t) => (TYPE_LABEL[t] ? TYPE_LABEL[t]() : t);
  const lang = () => (window.i18n && i18n.currentLang) || undefined;
  const hours = (v) => (Number(v) || 0).toLocaleString(lang(), { maximumFractionDigits: 2, minimumFractionDigits: 0 });
  const when = (iso) => {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? String(iso || '') : d.toLocaleString(lang(), { dateStyle: 'short', timeStyle: 'short' });
  };
  const pad = (n) => String(n).padStart(2, '0');
  const dayInput = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const keyOf = (h) => `${h.kind}|${h.name}`;
  function holderText(h) {
    if (h.kind === 'group') return tr('bkg.holderGroup', { name: h.name });
    if (h.kind === 'namespace') return tr('usg.noBooking', { ns: h.name });
    return h.name;
  }

  let cur = null;   // { host, prefs, data, open: Set, timer }

  // -- la période ----------------------------------------------------------------
  /** {from, to} (Date, heure locale) de la période choisie. */
  function range(prefs, today) {
    const now = today || new Date();
    const d0 = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    if (prefs.period === 'week') {
      const from = new Date(d0); from.setDate(from.getDate() - ((from.getDay() + 6) % 7));
      const to = new Date(from); to.setDate(to.getDate() + 7);
      return { from, to };
    }
    if (prefs.period === 'last') {
      return { from: new Date(now.getFullYear(), now.getMonth() - 1, 1), to: new Date(now.getFullYear(), now.getMonth(), 1) };
    }
    if (prefs.period === 'custom') {
      const f = prefs.from ? new Date(`${prefs.from}T00:00`) : new Date(now.getFullYear(), now.getMonth(), 1);
      const t = prefs.to ? new Date(`${prefs.to}T00:00`) : new Date(d0);
      const to = new Date(t); to.setDate(to.getDate() + 1);          // la fin est incluse
      return { from: f, to };
    }
    return { from: new Date(now.getFullYear(), now.getMonth(), 1), to: new Date(now.getFullYear(), now.getMonth() + 1, 1) };
  }
  function query(prefs) {
    const r = range(prefs);
    return `from=${encodeURIComponent(r.from.toISOString())}&to=${encodeURIComponent(r.to.toISOString())}&tz=${-new Date().getTimezoneOffset()}`;
  }

  function loadPrefs() {
    const p = { period: 'month', from: '', to: '' };
    try { Object.assign(p, JSON.parse(localStorage.getItem(STORE) || '{}')); } catch { /* réglages illisibles */ }
    if (!PERIODS.includes(p.period)) p.period = 'month';
    return p;
  }
  function savePrefs() {
    try { localStorage.setItem(STORE, JSON.stringify(cur.prefs)); } catch { /* stockage indisponible */ }
  }

  // -- cycle de vie (appelé par App.setTab) --------------------------------------
  function start(host) {
    stop();
    if (!host) return Promise.resolve();
    cur = { host, prefs: loadPrefs(), data: null, open: new Set() };
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

  async function load() {
    if (!cur) return;
    const c = cur;
    const q = query(c.prefs);
    c.host.querySelector('[data-usg="csv"]').setAttribute('href', `/api/usage.csv?${q}`);
    let d;
    try {
      const r = await fetch(`/api/usage?${q}`);
      d = await r.json().catch(() => null);
      if (!r.ok || !d) d = { error: (d && (d.hint || d.error)) || `HTTP ${r.status}` };
    } catch (e) { d = { error: e.message }; }
    if (c !== cur) return;
    c.data = d;
    render();
  }

  function shell() {
    const p = cur.prefs;
    const r = range(p);
    const toIncl = new Date(r.to.getTime() - DAY_MS);
    cur.host.innerHTML = `<div class="card na-card usg-card">
        <div class="res-tools">
          <div class="sub-tabs sub-tabs-inline usg-periods" role="tablist">
            ${PERIODS.map((v) => `<button type="button" class="sub-tab tip${v === p.period ? ' active' : ''}" role="tab" aria-selected="${v === p.period}"
              data-usg-period="${v}" data-tip="${esc(PERIOD_TIP[v]())}">${esc(PERIOD_LABEL[v]())}</button>`).join('')}
          </div>
          <span class="usg-custom"${p.period === 'custom' ? '' : ' hidden'}>
            <input type="date" class="tip" data-usg="from" value="${esc(p.from || dayInput(r.from))}" data-tip="${esc(tr('usg.fromTip'))}" aria-label="${esc(tr('usg.from'))}">
            <span>–</span>
            <input type="date" class="tip" data-usg="to" value="${esc(p.to || dayInput(toIncl))}" data-tip="${esc(tr('usg.toTip'))}" aria-label="${esc(tr('usg.to'))}">
          </span>
          <strong class="usg-range"></strong>
          <span class="usg-spacer"></span>
          <button type="button" class="btn btn-sm btn-secondary tip" data-usg="refresh" data-tip="${esc(tr('res.refreshTip'))}">${icon('refresh')} ${esc(tr('overview.refresh'))}</button>
          <a class="btn btn-sm btn-secondary tip" data-usg="csv" href="#" download data-tip="${esc(tr('usg.csvTip'))}">${icon('download')} ${esc(tr('usg.csv'))}</a>
          <button type="button" class="btn btn-sm btn-primary tip" data-usg="quotas" hidden data-tip="${esc(tr('usg.quotasTip'))}">${icon('settings')} ${esc(tr('usg.quotas'))}</button>
        </div>
        <div data-usg="body"><p class="form-hint">${esc(tr('common.loading'))}</p></div></div>`;
    const card = cur.host.querySelector('.usg-card');
    card.addEventListener('click', onClick);
    card.querySelectorAll('[data-usg="from"], [data-usg="to"]').forEach((el) => el.addEventListener('change', () => {
      cur.prefs.from = card.querySelector('[data-usg="from"]').value;
      cur.prefs.to = card.querySelector('[data-usg="to"]').value;
      savePrefs();
      load();
    }));
  }

  function render() {
    if (!cur || !cur.host.isConnected) return;
    const body = cur.host.querySelector('[data-usg="body"]');
    const d = cur.data;
    cur.host.querySelectorAll('[data-usg-period]').forEach((b) => {
      const on = b.dataset.usgPeriod === cur.prefs.period;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', String(on));
    });
    cur.host.querySelector('.usg-custom').hidden = cur.prefs.period !== 'custom';
    const r = range(cur.prefs);
    const last = new Date(r.to.getTime() - 1);
    cur.host.querySelector('.usg-range').textContent = `${r.from.toLocaleDateString(lang(), { day: 'numeric', month: 'short' })} – ${last.toLocaleDateString(lang(), { day: 'numeric', month: 'short', year: 'numeric' })}`;
    if (!d || d.error) {
      body.innerHTML = `<p class="res-error">${esc(d && d.error ? d.error : tr('fabric.unreachable'))}</p>`;
      return;
    }
    const admin = d.role === 'admin';
    cur.host.querySelector('[data-usg="quotas"]').hidden = !admin;
    body.innerHTML = `${samplerHtml(d)}${tilesHtml(d)}${chartHtml(d.days || [])}${tableHtml(d, admin)}`;
  }

  // -- l'état de l'échantillonneur, les totaux -----------------------------------
  function samplerHtml(d) {
    const s = d.sampler || {};
    const mins = Math.round((d.interval || 300) / 60);
    if (!s.first) {
      const text = d.sampling ? tr('usg.neverSampled', { min: mins }) : tr('usg.samplingOff');
      return `<div class="sto-finding sev-info usg-sampler" data-usg-sampler="never"><div class="sto-finding-title">${icon('info')} ${esc(text)}</div></div>`;
    }
    const clusters = s.clusters || [];
    const lastTs = clusters.reduce((m, c) => (c.ts > m ? c.ts : m), '');
    const bad = clusters.filter((c) => c.state !== 'ok');
    const badText = bad.length ? ` ${tr('usg.notRead', { list: bad.map((c) => `${String(c.cluster)} (${c.state})`).join(', ') })}` : '';
    return `<p class="form-hint usg-sampler" data-usg-sampler="ok">${icon('timer', 12)} ${esc(tr('usg.sampled', { at: when(lastTs), min: mins, since: when(s.first) }))}${esc(badText)}</p>`;
  }

  function tilesHtml(d) {
    const t = d.totals || {};
    const tile = (key, label, tip, v) => `<div class="usg-tile tip" data-usg-total="${key}" data-tip="${esc(tip)}">
        <div class="usg-tile-value">${esc(hours(v))}</div><div class="usg-tile-label">${esc(label)}</div></div>`;
    return `<div class="usg-tiles">${tile('booked', tr('usg.col.booked'), tr('usg.t.booked'), t.booked)}
      ${tile('consumed', tr('usg.col.consumed'), tr('usg.t.consumed'), t.consumed)}
      ${tile('waste', tr('usg.col.waste'), tr('usg.t.waste'), t.waste)}
      ${tile('out_of_rule', tr('usg.col.outOfRule'), tr('usg.t.outOfRule'), t.out_of_rule)}</div>`;
  }

  // -- le graphique par jour (SVG simple, couleurs du thème) ---------------------
  function niceMax(v) {
    if (v <= 0) return 1;
    const p = 10 ** Math.floor(Math.log10(v));
    return [1, 2, 2.5, 5, 10].map((m) => m * p).find((m) => m >= v);
  }
  function chartHtml(days) {
    const top = Math.max(0, ...days.map((x) => Math.max(x.booked, x.consumed)));
    const legend = `<div class="usg-legend"><span class="usg-key usg-key-booked"></span>${esc(tr('usg.col.booked'))}
      <span class="usg-key usg-key-consumed"></span>${esc(tr('usg.col.consumed'))}</div>`;
    if (!days.length || top <= 0) {
      return `<div class="usg-chart-wrap"><h3 class="usg-h">${esc(tr('usg.chart'))}</h3><p class="form-hint usg-chart-empty">${esc(tr('usg.chartEmpty'))}</p></div>`;
    }
    // la largeur réelle de la vue : le texte n'est jamais étiré
    const host = cur && cur.host.querySelector('[data-usg="body"]');
    const W = Math.max(320, Math.round((host && host.clientWidth) || 760)), H = 190, L = 44, R = 8, T = 10, B = 26;
    const pw = W - L - R, ph = H - T - B;
    const max = niceMax(top);
    const slot = pw / days.length;
    const bw = Math.max(1.5, Math.min(18, slot * 0.36));
    const y = (v) => T + ph - (v / max) * ph;
    const every = Math.max(1, Math.ceil(days.length / Math.max(2, Math.floor(pw / 70))));
    const fmtDay = (s) => new Date(`${s}T00:00`).toLocaleDateString(lang(), { day: 'numeric', month: 'numeric' });
    const grid = [0, 0.5, 1].map((f) => `<line class="usg-grid" x1="${L}" x2="${W - R}" y1="${y(max * f).toFixed(1)}" y2="${y(max * f).toFixed(1)}"/>
        <text class="usg-axis" x="${L - 6}" y="${(y(max * f) + 4).toFixed(1)}" text-anchor="end">${esc(hours(max * f))}</text>`).join('');
    const bars = days.map((x, i) => {
      const cx = L + slot * i + slot / 2;
      const label = tr('usg.t.day', { day: fmtDay(x.day), booked: hours(x.booked), consumed: hours(x.consumed) });
      const rect = (cls, v, dx) => (v > 0 ? `<rect class="${cls}" x="${(cx + dx).toFixed(1)}" y="${y(v).toFixed(1)}" width="${bw.toFixed(1)}" height="${(T + ph - y(v)).toFixed(1)}" rx="1.5"/>` : '');
      return `<g class="usg-day" data-day="${esc(x.day)}"><title>${esc(label)}</title>
          <rect class="usg-hit" x="${(L + slot * i).toFixed(1)}" y="${T}" width="${slot.toFixed(1)}" height="${ph}"/>
          ${rect('usg-bar-booked', x.booked, -bw - 1)}${rect('usg-bar-consumed', x.consumed, 1)}
          ${i % every === 0 ? `<text class="usg-axis" x="${cx.toFixed(1)}" y="${H - 8}" text-anchor="middle">${esc(fmtDay(x.day))}</text>` : ''}</g>`;
    }).join('');
    const total = days.reduce((a, x) => ({ b: a.b + x.booked, c: a.c + x.consumed }), { b: 0, c: 0 });
    const aria = tr('usg.chartAria', { n: days.length, booked: hours(total.b), consumed: hours(total.c) });
    return `<div class="usg-chart-wrap"><h3 class="usg-h">${esc(tr('usg.chart'))}</h3>
      <svg class="usg-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(aria)}" preserveAspectRatio="xMinYMin meet">${grid}
        <line class="usg-baseline" x1="${L}" x2="${W - R}" y1="${T + ph}" y2="${T + ph}"/>${bars}</svg>${legend}</div>`;
  }

  // -- le tableau par titulaire ---------------------------------------------------
  function quotaHtml(q) {
    if (!q) return '<span class="res-dim">–</span>';
    const parts = [];
    if (q.monthly_hours != null) {
      const pct = q.monthly_hours > 0 ? Math.min(100, (q.month_booked / q.monthly_hours) * 100) : 100;
      parts.push(`<div class="usg-quota tip${q.over ? ' is-over' : ''}" data-tip="${esc(tr('usg.t.quota', { used: hours(q.month_booked), cap: hours(q.monthly_hours), left: hours(q.remaining) }))}">
          <div class="usg-quota-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(pct)}"><div class="usg-quota-fill" style="width:${pct.toFixed(1)}%"></div></div>
          <span class="usg-quota-text">${esc(tr('usg.quotaLine', { used: hours(q.month_booked), cap: hours(q.monthly_hours), left: hours(q.remaining) }))}</span></div>`);
    }
    if (q.max_units) parts.push(`<span class="res-dim usg-maxunits">${esc(tr('usg.maxUnits', { n: q.max_units }))}</span>`);
    return parts.join('');
  }

  function tableHtml(d, admin) {
    const rows = d.holders || [];
    if (!rows.length) return `<p class="form-hint usg-empty">${esc(tr('usg.none'))}</p>`;
    const th = (t, tip) => `<th${tip ? ` class="tip" data-tip="${esc(tip)}"` : ''}>${esc(t)}</th>`;
    const num = (v, cls = '') => `<td class="usg-num${cls}">${esc(hours(v))}</td>`;
    const body = rows.map((r) => {
      const k = keyOf(r.holder);
      const open = cur.open.has(k);
      const canQuota = admin && r.holder.kind !== 'namespace';
      const main = `<tr data-holder="${esc(k)}" class="usg-row${r.holder.kind === 'namespace' ? ' is-unbooked' : ''}">
        <td><button type="button" class="btn-icon-action tip usg-toggle" data-usg="toggle" data-key="${esc(k)}" aria-expanded="${open}"
            data-tip="${esc(tr('usg.toggleTip'))}" aria-label="${esc(tr('usg.toggleTip'))}">${icon(open ? 'chevronDown' : 'chevronRight', 12)}</button>
          <span class="tip" data-tip="${esc(r.holder.kind === 'namespace' ? tr('usg.t.unbooked') : tr('usg.t.holder', { n: r.bookings }))}">${esc(holderText(r.holder))}</span></td>
        ${num(r.booked)}${num(r.consumed)}<td>${quotaHtml(r.quota)}</td>${num(r.waste, r.waste > 0 ? ' is-waste' : '')}${num(r.out_of_rule, r.out_of_rule > 0 ? ' is-out' : '')}
        ${admin ? `<td class="usg-actions">${canQuota ? `<button type="button" class="btn btn-sm btn-secondary tip" data-usg="quota-edit" data-kind="${esc(r.holder.kind)}" data-name="${esc(r.holder.name)}"
            data-tip="${esc(tr('usg.setQuotaTip'))}">${esc(tr('usg.setQuota'))}</button>` : '<span class="res-dim">–</span>'}</td>` : ''}</tr>`;
      const sub = (r.breakdown || []).map((p) => `<tr class="usg-sub" data-sub-of="${esc(k)}"${open ? '' : ' hidden'}>
          <td class="usg-sub-label">${esc(p.cluster)} · ${esc(typeLabel(p.type))}</td>${num(p.booked)}${num(p.consumed)}<td></td><td></td><td></td>${admin ? '<td></td>' : ''}</tr>`).join('');
      const none = (r.breakdown || []).length ? '' : `<tr class="usg-sub" data-sub-of="${esc(k)}"${open ? '' : ' hidden'}><td colspan="${admin ? 7 : 6}" class="res-dim">${esc(tr('usg.noBreakdown'))}</td></tr>`;
      return main + sub + none;
    }).join('');
    const t = d.totals || {};
    return `<h3 class="usg-h">${esc(tr('usg.table', { month: d.quota_month }))}</h3>
      <table class="data-table usg-table"><thead><tr>${th(tr('bkg.col.holder'))}${th(tr('usg.col.booked'), tr('usg.t.booked'))}${th(tr('usg.col.consumed'), tr('usg.t.consumed'))}
        ${th(tr('usg.col.quota', { month: d.quota_month }), tr('usg.t.quotaCol'))}${th(tr('usg.col.waste'), tr('usg.t.waste'))}${th(tr('usg.col.outOfRule'), tr('usg.t.outOfRule'))}${admin ? th(tr('bkg.col.actions')) : ''}</tr></thead>
      <tbody>${body}</tbody>
      <tfoot><tr class="usg-total"><td>${esc(tr('usg.total'))}</td>${num(t.booked)}${num(t.consumed)}<td></td>${num(t.waste)}${num(t.out_of_rule)}${admin ? '<td></td>' : ''}</tr></tfoot></table>`;
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
      err.status = r.status;
      throw err;
    }
    return d;
  }
  function follow(actionId, msg, doneText, onDone) {
    if (window.VMActions && VMActions.follow) VMActions.follow(actionId, msg, doneText, onDone);
    else { if (msg) msg.textContent = tr('bk.started', { id: actionId }); if (onDone) onDone(true); }
  }

  function onClick(e) {
    const b = e.target.closest('[data-usg], [data-usg-period]');
    if (!b || !cur || b.tagName === 'INPUT') return;
    if (b.dataset.usgPeriod) {
      cur.prefs.period = b.dataset.usgPeriod;
      savePrefs();
      render();
      load();
      return;
    }
    const act = b.dataset.usg;
    if (act === 'refresh') { load(); return; }
    if (act === 'quotas') { openQuotas(); return; }
    if (act === 'quota-edit') { openQuotas({ kind: b.dataset.kind, name: b.dataset.name }); return; }
    if (act === 'toggle') {
      const k = b.dataset.key;
      if (cur.open.has(k)) cur.open.delete(k); else cur.open.add(k);
      const open = cur.open.has(k);
      b.setAttribute('aria-expanded', String(open));
      b.innerHTML = icon(open ? 'chevronDown' : 'chevronRight', 12);
      cur.host.querySelectorAll('tr.usg-sub').forEach((tr2) => { if (tr2.dataset.subOf === k) tr2.hidden = !open; });
    }
  }

  // -- la fenêtre des quotas (administrateurs) ---------------------------------------
  const field = (label, input, tip) => `<label class="bk-field"><span>${esc(label)}</span>${
    input.replace(/^<(input|select)/, `<$1 class="tip" data-tip="${esc(tip)}"`)}</label>`;

  function quotaRows(list) {
    if (!list.length) return `<p class="form-hint usg-q-empty">${esc(tr('usg.q.none'))}</p>`;
    return `<table class="data-table usg-q-table"><thead><tr><th>${esc(tr('bkg.col.holder'))}</th><th>${esc(tr('usg.q.hours'))}</th>
      <th>${esc(tr('usg.q.units'))}</th><th>${esc(tr('bkg.col.actions'))}</th></tr></thead><tbody>
      ${list.map((q) => `<tr data-quota="${esc(keyOf(q.holder))}"><td><span class="tip" data-tip="${esc(tr('usg.q.setBy', { who: q.updated_by || '?', at: when(q.updated_at) }))}">${esc(holderText(q.holder))}</span></td>
        <td>${q.monthly_hours == null ? '–' : esc(hours(q.monthly_hours))}</td><td>${q.max_units ? esc(q.max_units) : '–'}</td>
        <td class="usg-actions"><button type="button" class="btn btn-sm btn-secondary tip" data-usgq="edit" data-kind="${esc(q.holder.kind)}" data-name="${esc(q.holder.name)}"
            data-hours="${q.monthly_hours == null ? '' : esc(q.monthly_hours)}" data-units="${q.max_units ? esc(q.max_units) : ''}" data-tip="${esc(tr('usg.q.editTip'))}">${esc(tr('usg.q.edit'))}</button>
          <button type="button" class="btn btn-sm btn-secondary tip" data-usgq="delete" data-kind="${esc(q.holder.kind)}" data-name="${esc(q.holder.name)}"
            data-tip="${esc(tr('usg.q.deleteTip'))}">${esc(tr('usg.q.delete'))}</button></td></tr>`).join('')}</tbody></table>`;
  }

  function openQuotas(pre) {
    if (!window.FloatingPanels) return null;
    const panel = FloatingPanels.open({
      id: 'usg-quotas', icon: 'settings', width: 620, height: 640, title: tr('usg.quotas'),
      bodyHtml: `<div class="usg-quota-panel">
        <p class="form-hint">${esc(tr('usg.q.hint'))}</p>
        <div data-usgq="list"><p class="form-hint">${esc(tr('common.loading'))}</p></div>
        <form class="of-form usg-q-form" autocomplete="off" novalidate>
          <h3 class="usg-h">${esc(tr('usg.q.formTitle'))}</h3>
          <div class="ct-create-row">
            ${field(tr('bkg.f.holderKind'), `<select name="kind"><option value="user">${esc(tr('bkg.f.person'))}</option><option value="group">${esc(tr('bkg.f.group'))}</option></select>`, tr('usg.q.kindTip'))}
            ${field(tr('bkg.f.holder'), '<input name="name" maxlength="128">', tr('usg.q.nameTip'))}
          </div>
          <div class="ct-create-row">
            ${field(tr('usg.q.hours'), '<input type="number" name="hours" min="0" step="0.5">', tr('usg.q.hoursTip'))}
            ${field(tr('usg.q.units'), '<input type="number" name="units" min="1" step="1">', tr('usg.q.unitsTip'))}
          </div>
          <div class="bk-form-actions"><button type="submit" class="btn btn-sm btn-primary tip" data-usgq="save" data-tip="${esc(tr('usg.q.saveTip'))}">${icon('ok')} ${esc(tr('usg.q.save'))}</button></div>
          <div class="of-msg" role="status"></div>
        </form></div>`,
    });
    const root = panel.el;
    const f = root.querySelector('form');
    const msg = f.querySelector('.of-msg');
    const fill = (q) => {
      f.kind.value = q.kind === 'group' ? 'group' : 'user';
      f.name.value = q.name || '';
      f.hours.value = q.hours != null ? q.hours : '';
      f.units.value = q.units != null ? q.units : '';
      f.hours.focus();
    };
    const refreshList = async () => {
      let d;
      try { d = await (await fetch('/api/usage/quotas')).json(); } catch (e) { d = { error: e.message }; }
      const box = root.querySelector('[data-usgq="list"]');
      if (!box) return;
      box.innerHTML = d && d.quotas ? quotaRows(d.quotas) : `<p class="res-error">${esc((d && d.error) || '?')}</p>`;
      if (pre && d && d.quotas) {
        const q = d.quotas.find((x) => x.holder.kind === pre.kind && x.holder.name === pre.name);
        fill(q ? { kind: pre.kind, name: pre.name, hours: q.monthly_hours, units: q.max_units } : pre);
        pre = null;
      }
    };
    if (root.dataset.usgReady) { if (pre) fill(pre); refreshList(); return root; }
    root.dataset.usgReady = '1';
    const done = () => { refreshList(); if (cur) load(); };
    root.addEventListener('click', async (e) => {
      const b = e.target.closest('[data-usgq]');
      if (!b || b.tagName === 'FORM') return;
      const act = b.dataset.usgq;
      if (act === 'edit') { fill({ kind: b.dataset.kind, name: b.dataset.name, hours: b.dataset.hours, units: b.dataset.units }); return; }
      if (act === 'delete') {
        const cell = b.closest('td');
        cell.innerHTML = `<div class="bkg-confirm" role="group"><span>${esc(tr('usg.q.confirm'))}</span>
          <button type="button" class="btn btn-sm btn-danger tip" data-usgq="delete-yes" data-kind="${esc(b.dataset.kind)}" data-name="${esc(b.dataset.name)}" data-tip="${esc(tr('usg.q.deleteTip'))}">${esc(tr('usg.q.confirmYes'))}</button>
          <button type="button" class="btn btn-sm btn-secondary tip" data-usgq="delete-no" data-tip="${esc(tr('bkg.confirmNoTip'))}">${esc(tr('bkg.confirmNo'))}</button></div>`;
        return;
      }
      if (act === 'delete-no') { refreshList(); return; }
      if (act === 'delete-yes') {
        b.disabled = true;
        try {
          const out = await send('DELETE', '/api/usage/quotas', { holder: { kind: b.dataset.kind, name: b.dataset.name } });
          follow(out.action_id, msg, tr('usg.q.deleted'), done);
        } catch (err) { msg.innerHTML = `<span class="res-error">${icon('fail')} ${esc(err.message)}</span>`; refreshList(); }
      }
    });
    f.addEventListener('submit', async (e) => {
      e.preventDefault();
      const name = f.name.value.trim();
      const hrs = f.hours.value === '' ? null : Number(f.hours.value);
      const units = f.units.value === '' ? null : Number(f.units.value);
      if (!name) { msg.innerHTML = `<span class="res-error">${esc(tr('bkg.err.holder'))}</span>`; return; }
      if (hrs == null && units == null) { msg.innerHTML = `<span class="res-error">${esc(tr('usg.q.errEmpty'))}</span>`; return; }
      const btn = f.querySelector('[data-usgq="save"]');
      btn.disabled = true;
      try {
        const out = await send('POST', '/api/usage/quotas', { holder: { kind: f.kind.value, name }, monthly_hours: hrs, max_units: units });
        follow(out.action_id, msg, tr('usg.q.saved'), () => { btn.disabled = false; done(); });
      } catch (err) {
        btn.disabled = false;
        msg.innerHTML = `<span class="res-error">${icon('fail')} ${esc(err.message)}</span>`;
      }
    });
    if (pre) fill(pre);
    refreshList();
    return root;
  }

  return { start, stop, load, range, openQuotas };
})();
window.Usage = Usage;
