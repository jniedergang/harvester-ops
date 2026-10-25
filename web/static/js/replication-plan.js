/**
 * harvester-ops : Disaster Recovery, la vue d'un contrat de réplication.
 *
 * Une fenêtre flottante par contrat, quatre onglets :
 *  - Vue d'ensemble : RPO tenu face à l'objectif (jauges), VMs et leurs
 *    raisons, débits des deux trajets (source -> cible de sauvegarde -> secours),
 *    passages en attente, dernières commandes ;
 *  - Graphiques : RPO tenu, débits et données envoyées, sur 24 h par défaut
 *    (1 h, 7 j, 30 j), séries regroupées par le serveur ;
 *  - Journal : le journal persistant du plan et de ses VMs, paginé ;
 *  - Réglages : suivi à chaud (objectif, pause, suiveur), reprise (vagues,
 *    cloud-init, MAC, CPU/mémoire, actions annexes) et zone dangereuse.
 *
 * La bascule s'ouvre dans sa propre fenêtre : les étapes du mode choisi
 * (planifiée ou non), un essai à blanc, le nom du contrat à taper, réservée
 * aux administrateurs. Chaque geste est une action suivie (dock, Activité).
 */
const ReplicationPlan = (() => {
  const U = window.ReplUtil;
  const { tr, enc, esc, icon } = U;
  const RANGES = ['1h', '24h', '7d', '30d'];
  const RANGE_S = { '1h': 3600, '24h': 86400, '7d': 604800, '30d': 2592000 };
  const RANGE_LABEL = { '1h': () => tr('rp.range.1h'), '24h': () => tr('rp.range.24h'), '7d': () => tr('rp.range.7d'), '30d': () => tr('rp.range.30d') };
  const TABS = ['overview', 'charts', 'journal', 'settings'];
  const TAB_LABEL = { overview: () => tr('rp.tab.overview'), charts: () => tr('rp.tab.charts'),
                      journal: () => tr('rp.tab.journal'), settings: () => tr('rp.tab.settings') };
  const TAB_TIP = { overview: () => tr('rp.tab.overviewTip'), charts: () => tr('rp.tab.chartsTip'),
                    journal: () => tr('rp.tab.journalTip'), settings: () => tr('rp.tab.settingsTip') };
  const TAB_ICON = { overview: 'metrics', charts: 'activity', journal: 'logs', settings: 'settings' };
  const views = new Map();   // planId -> état de la fenêtre

  const err = (e) => `<span class="res-error">${icon('fail')} ${esc(e.message || e)}</span>`;
  const getPlan = (id) => U.get(`/api/replication/plans/${enc(id)}`);
  function reloadLists() {
    if (window.Replication) { Replication.load(); Replication.refreshBadges(); }
  }

  // ===========================================================================
  // Fenêtre du contrat
  // ===========================================================================
  function open(planId, tab) {
    const panel = FloatingPanels.open({
      id: `rp-plan-${planId}`, icon: 'shield', width: 1100, height: 800, title: tr('rp.view.title', { name: planId }),
      bodyHtml: `<div class="rp-view" data-rp-view-plan="${esc(planId)}"><p class="form-hint">${esc(tr('common.loading'))}</p></div>`,
      onClose: () => {
        const v = views.get(planId); if (v && v.timer) clearInterval(v.timer); views.delete(planId);
        // 2.0.0 : #dr/<id> redevient #dr quand sa fenêtre se ferme
        if (window.Router) Router.syncFromScreen();
      },
    });
    let v = views.get(planId);
    if (!v) {
      v = { id: planId, panel, tab: TABS.includes(tab) ? tab : 'overview', range: '24h', data: null, journal: null, warnOnly: false };
      views.set(planId, v);
      v.root = panel.el.querySelector('.rp-view');
      v.root.addEventListener('click', (e) => onClick(v, e));
      v.timer = setInterval(() => { if (v.root.isConnected && !document.hidden && v.tab === 'overview') load(v); }, 20000);
    } else if (tab) { v.tab = tab; }
    load(v);
    return panel;
  }

  async function load(v) {
    const d = await getPlan(v.id);
    v.data = d;
    if (d && d.plan) v.panel.setTitle(tr('rp.view.title', { name: d.plan.name }));
    render(v);
  }

  function header(v) {
    const d = v.data;
    const row = d.row;
    const p = d.plan;
    const st = U.planState(row);
    // reprise d'une commande interrompue : le serveur dit si elle existe et qui peut la lancer
    const resumeCmd = U.action(row, 'resume-command');
    const canOp = U.roleRank(d.role) >= 2;
    return `<div class="rp-view-head">
      <div class="rp-view-title"><h2>${esc(p.name)}</h2> ${U.stateBadge(st)} ${U.flow(row)}</div>
      <p class="form-hint">${esc(tr('rp.view.sub', { obj: U.dur(p.rpo_objective_s), interval: p.interval_s ? U.dur(p.interval_s) : tr('rp.view.noInterval'),
        retain: p.retain, by: p.created_by || '?' }))}${p.selector ? ` · ${esc(tr('rp.view.selector', { sel: p.selector, ns: p.selector_namespace }))}` : ''}</p>
      ${d.lock ? `<div class="sto-finding sev-info"><div class="sto-finding-title">${icon('running')} ${esc(tr('rp.view.locked', { cmd: d.lock.command, ago: U.ago(d.lock.started_at) }))}</div></div>` : ''}
      <div class="btn-row rp-view-actions">
        <button type="button" class="btn btn-sm btn-secondary tip" data-rpv="check" data-tip="${esc(canOp ? tr('rp.act.checkTip') : tr('rp.viewerTip'))}"${canOp ? '' : ' disabled'}>${icon('test')} ${esc(tr('rp.act.check'))}</button>
        ${U.actionButton(row, 'measure-start')}${U.actionButton(row, 'measure-stop')}${U.actionButton(row, 'protect', 'btn-primary')}
        ${U.actionButton(row, 'pause')}${U.actionButton(row, 'resume')}
        ${resumeCmd ? `<button type="button" class="btn btn-sm btn-secondary tip" data-rpv="resume-cmd"
          data-tip="${esc(resumeCmd.allowed ? tr('rp.act.resumeCmdTip') : `${tr('rp.act.resumeCmdTip')} ${tr('rp.act.needsRole', { role: resumeCmd.role })}`)}"${resumeCmd.allowed ? '' : ' disabled aria-disabled="true"'}>${icon('play')} ${esc(tr('rp.act.resumeCmd'))}</button>` : ''}
        ${(U.action(row, 'failover') || U.action(row, 'failover-unplanned')) ? `<button type="button" class="btn btn-sm btn-danger tip" data-rpv="failover"
          data-tip="${esc(tr('rp.act.failoverDialogTip'))}">${icon('migrate')} ${esc(tr('rp.act.failoverDots'))}</button>` : ''}
      </div>
      <div class="of-msg" data-rpv="msg" role="status" aria-live="polite"></div>
      <div class="sub-tabs sub-tabs-inline rp-view-tabs" role="tablist">${TABS.map((t) => `<button type="button" class="sub-tab tip${v.tab === t ? ' active' : ''}"
        role="tab" aria-selected="${v.tab === t}" data-rpv-tab="${t}" data-tip="${esc(TAB_TIP[t]())}">${icon(TAB_ICON[t])} <span>${esc(TAB_LABEL[t]())}</span></button>`).join('')}</div>
    </div>`;
  }

  function render(v) {
    const d = v.data;
    if (!v.root.isConnected) return;
    if (!d || d.error) { v.root.innerHTML = `<p class="res-error">${esc(d && d.error ? d.error : tr('fabric.unreachable'))}</p>`; return; }
    const keepMsg = v.root.querySelector('[data-rpv="msg"]');
    const msgHtml = keepMsg ? keepMsg.innerHTML : '';
    // les réglages en cours de saisie ne sont pas repeints par le rafraîchissement
    if (v.tab === 'settings' && v.root.querySelector('[data-rpv-pane="settings"]')) {
      v.root.querySelector('.rp-view-head').outerHTML = header(v);
      v.root.querySelector('[data-rpv="msg"]').innerHTML = msgHtml;
      return;
    }
    v.root.innerHTML = `${header(v)}<div class="rp-view-pane" data-rpv-pane="${esc(v.tab)}"></div>`;
    v.root.querySelector('[data-rpv="msg"]').innerHTML = msgHtml;
    const pane = v.root.querySelector('.rp-view-pane');
    if (v.tab === 'overview') pane.innerHTML = overviewHtml(v);
    if (v.tab === 'charts') renderCharts(v, pane);
    if (v.tab === 'journal') renderJournal(v, pane, true);
    if (v.tab === 'settings') renderSettings(v, pane);
  }

  /** Carnet de reprise en retard : réglages changés depuis le dernier protect, ou vu par le suiveur. */
  function staleHtml(d) {
    const keys = (d.carnet_stale && d.carnet_stale.keys) || [];
    const vms = ((d.row && d.row.resources) || []).filter((r) => (r.reasons || []).includes('carnet-stale')).map((r) => `${r.namespace}/${r.name}`);
    if (!keys.length && !vms.length) return '';
    const canOp = U.roleRank(d.role) >= 2;
    // un contrat protégé ne propose pas « Protéger » dans ses actions : protect relancé ici, comme Réglages > Appliquer
    return `<div class="sto-finding sev-action" data-rpv="stale"><div class="sto-finding-title">${icon('warn')} ${esc(tr('rp.stale.title'))}
        <button type="button" class="btn btn-sm btn-primary tip" data-rpv="protect-again" data-tip="${esc(canOp ? tr('rp.stale.protectTip') : tr('rp.viewerTip'))}"${canOp ? '' : ' disabled'}>${icon('shield')} ${esc(tr('rp.stale.protect'))}</button></div>
      ${keys.length ? `<p class="sto-finding-facts">${esc(tr('rp.stale.keys', { keys: keys.join(', ') }))}</p>` : ''}
      ${vms.length ? `<p class="sto-finding-facts">${esc(tr('rp.stale.vms', { vms: vms.join(', ') }))}</p>` : ''}
      <p class="sto-finding-advice">${esc(tr('rp.stale.advice'))}</p></div>`;
  }

  // -- vue d'ensemble --------------------------------------------------------------------
  function overviewHtml(v) {
    const d = v.data;
    const s = d.status || {};
    const row = d.row;
    const t = row.transfer || {};
    const next = s.next_tick_at ? Math.max(0, (Date.parse(s.next_tick_at) - Date.now()) / 1000) : null;
    const pending = row.pending_vms || [];
    const vms = s.vms || [];
    return `<div class="kpi rp-kpi">
        <div class="rp-kpi-wide"><small>${esc(tr('rp.kpi.rpo'))}</small>${U.gauge(s.rpo_held_s, s.rpo_objective_s)}</div>
        <div><small>${esc(tr('rp.kpi.interval'))}</small><b>${esc(s.interval_s ? U.dur(s.interval_s) : '-')}</b></div>
        <div><small>${esc(tr('rp.kpi.next'))}</small><b>${esc(next == null ? '-' : U.dur(next))}</b></div>
        <div><small>${esc(tr('rp.kpi.backlog'))}</small><b class="tip" data-tip="${esc(tr('rp.kpi.backlogTip'))}">${esc(s.backlog ?? 0)}</b></div>
        <div><small>${esc(tr('rp.kpi.skew'))}</small><b class="tip" data-tip="${esc(tr('rp.kpi.skewTip'))}">${esc(U.dur(s.skew_s || 0))}</b></div>
        <div><small>${esc(tr('rp.kpi.upload'))}</small><b class="tip" data-tip="${esc(tr('rp.kpi.uploadTip'))}">${esc(U.rate(t.upload_bps))}</b></div>
        <div><small>${esc(tr('rp.kpi.apply'))}</small><b class="tip" data-tip="${esc(tr('rp.kpi.applyTip'))}">${esc(U.rate(t.apply_bps))}</b></div>
        <div><small>${esc(tr('rp.kpi.day'))}</small><b>${esc(U.bytes(t.bytes_24h))}</b></div>
      </div>
      ${staleHtml(d)}
      ${pending.length ? `<div class="sto-finding sev-action" data-rpv="pending"><div class="sto-finding-title">${icon('warn')} ${esc(tr('rp.pending', { n: pending.length, list: pending.join(', ') }))}
        ${U.actionButton(row, 'protect', 'btn-primary')}</div></div>` : ''}
      <div class="tw"><table class="data-table rp-vm-table"><thead><tr>
        <th>${esc(tr('rp.col.vm'))}</th><th>${esc(tr('rp.col.state'))}</th><th>${esc(tr('rp.col.rpo'))}</th><th>${esc(tr('rp.col.disks'))}</th>
        <th>${esc(tr('rp.col.lastPoint'))}</th><th>${esc(tr('rp.col.upload'))}</th><th>${esc(tr('rp.col.apply'))}</th>
        <th class="num">${esc(tr('rp.kpi.backlog'))}</th><th class="num">${esc(tr('rp.kpi.skew'))}</th></tr></thead>
      <tbody>${vms.length ? vms.map((x) => {
        const up = (x.throughput || {}).upload, ap = (x.throughput || {}).apply;
        return `<tr data-rp-vm="${esc(x.uid)}"><td><b>${esc(`${x.namespace}/${x.name}`)}</b></td>
          <td>${U.stateBadge(x.state, (x.reasons || []).map(U.reasonText).join(', '))}${(x.reasons || []).length ? `<div class="rp-reasons">${(x.reasons || []).map((r) => `<span class="res-dim">${esc(U.reasonText(r))}</span>`).join(' · ')}</div>` : ''}</td>
          <td>${U.gauge(x.rpo_held_s, s.rpo_objective_s)}</td><td>${(x.disks || []).length}</td>
          <td class="tip" data-tip="${esc(U.when(x.last_applied_at))}">${esc(x.last_applied_at ? U.ago(x.last_applied_at) : '-')}</td>
          <td>${up ? `${esc(U.rate(up.bps))} <span class="res-dim">(${esc(U.bytes(up.bytes))})</span>` : '-'}</td>
          <td>${ap ? `${esc(U.rate(ap.bps))} <span class="res-dim">(${esc(U.dur(ap.seconds))})</span>` : '-'}</td>
          <td class="num">${esc(x.backlog || 0)}</td><td class="num">${esc(U.dur(x.skew_s || 0))}</td></tr>`;
      }).join('') : `<tr><td colspan="9" class="hint">${esc(tr('rp.noVms'))}</td></tr>`}</tbody></table></div>
      <h4 class="rp-sub-title">${esc(tr('rp.runs.title'))}</h4>
      ${(d.runs || []).length ? `<div class="tw"><table class="data-table rp-runs"><thead><tr><th>${esc(tr('rp.runs.op'))}</th><th>${esc(tr('rp.runs.started'))}</th>
        <th>${esc(tr('rp.runs.result'))}</th><th>${esc(tr('rp.col.vms'))}</th><th>${esc(tr('rp.runs.by'))}</th></tr></thead>
        <tbody>${d.runs.slice().reverse().map((r) => `<tr><td>${esc(r.operation)}</td><td class="tip" data-tip="${esc(U.when(r.started_at))}">${esc(U.ago(r.started_at))}</td>
          <td>${r.finished_at ? `<span class="badge ${r.result === 'ok' ? 'ok' : 'warn'}">${esc(r.result || '?')}</span>` : `<span class="badge info">${esc(tr('rp.runs.open'))}</span>`}</td>
          <td>${esc((r.vms || []).join(', '))}</td><td>${esc(r.by || '')}</td></tr>`).join('')}</tbody></table></div>`
        : `<p class="hint">${esc(tr('rp.runs.none'))}</p>`}`;
  }

  // -- graphiques -----------------------------------------------------------------------------
  async function renderCharts(v, pane) {
    pane.innerHTML = `<div class="btn-row rp-range"><div class="sub-tabs sub-tabs-inline" role="tablist" aria-label="${esc(tr('rp.range.label'))}">
        ${RANGES.map((r) => `<button type="button" class="sub-tab tip${v.range === r ? ' active' : ''}" role="tab" aria-selected="${v.range === r}"
          data-rpv-range="${r}" data-tip="${esc(tr('rp.range.tip'))}">${esc(RANGE_LABEL[r]())}</button>`).join('')}</div></div>
      <div class="rp-charts" data-rpv="charts"><p class="form-hint">${esc(tr('common.loading'))}</p></div>`;
    const range = v.range;
    const d = await U.get(`/api/replication/plans/${enc(v.id)}/series?range=${enc(range)}`);
    if (v.range !== range || !pane.isConnected) return;
    const box = pane.querySelector('[data-rpv="charts"]');
    if (d.error) { box.innerHTML = `<p class="res-error">${esc(d.error)}</p>`; return; }
    const pts = d.points || [];
    if (!pts.length) { box.innerHTML = `<p class="hint" data-rpv="nodata">${esc(tr('rp.charts.none'))}</p>`; return; }
    const end = Date.now(), start = end - RANGE_S[range] * 1000;
    box.innerHTML = `
      <figure class="rp-chart-box" data-chart="rpo"><h4>${esc(tr('rp.charts.rpo'))}</h4>
        <div class="rp-legend"><span><i class="rp-sw rpo"></i>${esc(tr('rp.charts.rpoHeld'))}</span><span><i class="rp-sw ref"></i>${esc(tr('rp.charts.objective', { obj: U.dur(d.rpo_objective_s) }))}</span></div>
        ${lineChart(pts, [{ key: 'rpo_held_s', cls: 'rpo', label: tr('rp.charts.rpoHeld') }], { start, end, fmt: U.dur, ref: d.rpo_objective_s })}</figure>
      <figure class="rp-chart-box" data-chart="rate"><h4>${esc(tr('rp.charts.rate'))}</h4>
        <div class="rp-legend"><span><i class="rp-sw up"></i>${esc(tr('rp.charts.upload'))}</span><span><i class="rp-sw apply"></i>${esc(tr('rp.charts.apply'))}</span></div>
        ${lineChart(pts, [{ key: 'upload_bps', cls: 'up', label: tr('rp.charts.upload') }, { key: 'apply_bps', cls: 'apply', label: tr('rp.charts.apply') }], { start, end, fmt: U.rate })}</figure>
      <figure class="rp-chart-box" data-chart="bytes"><h4>${esc(tr('rp.charts.bytes', { bucket: U.dur(d.bucket_s) }))}</h4>
        ${barChart(pts, 'bytes', { start, end, bucket: d.bucket_s, fmt: U.bytes })}</figure>`;
    box.querySelectorAll('.rp-chart').forEach(hookTooltip);
  }

  const W = 640, H = 190, PL = 64, PR = 12, PT = 10, PB = 24;
  function axes(start, end, max, fmt) {
    const ys = [0, 0.5, 1].map((f) => max * f);
    const xt = [0, 0.25, 0.5, 0.75, 1].map((f) => start + (end - start) * f);
    const long = end - start > 2 * 86400000;
    const fx = (ms) => new Date(ms).toLocaleString(undefined, long ? { month: 'short', day: 'numeric' } : { hour: '2-digit', minute: '2-digit' });
    return `${ys.map((y) => { const py = PT + (H - PT - PB) * (1 - y / max); return `<line class="grid" x1="${PL}" x2="${W - PR}" y1="${py}" y2="${py}"/><text x="${PL - 6}" y="${py + 4}" text-anchor="end">${esc(fmt(y))}</text>`; }).join('')}
      ${xt.map((x, i) => `<text x="${PL + (W - PL - PR) * ((x - start) / (end - start))}" y="${H - 6}" text-anchor="${i === 0 ? 'start' : (i === xt.length - 1 ? 'end' : 'middle')}">${esc(fx(x))}</text>`).join('')}`;
  }
  const px = (t, start, end) => PL + (W - PL - PR) * Math.min(1, Math.max(0, (Date.parse(t) - start) / (end - start)));
  function lineChart(pts, series, o) {
    const max = Math.max(1, o.ref ? o.ref * 1.2 : 0, ...pts.flatMap((p) => series.map((s) => Number(p[s.key]) || 0))) * 1.05;
    const py = (y) => PT + (H - PT - PB) * (1 - y / max);
    const paths = series.map((s) => {
      let d = '', open = false;
      pts.forEach((p) => {
        if (p[s.key] == null) { open = false; return; }
        d += `${open ? 'L' : 'M'}${px(p.t, o.start, o.end).toFixed(1)},${py(p[s.key]).toFixed(1)} `;
        open = true;
      });
      const dots = pts.filter((p) => p[s.key] != null).length < 3
        ? pts.filter((p) => p[s.key] != null).map((p) => `<circle class="${s.cls}" cx="${px(p.t, o.start, o.end).toFixed(1)}" cy="${py(p[s.key]).toFixed(1)}" r="4"/>`).join('') : '';
      return `<path class="line ${s.cls}" d="${d}"/>${dots}`;
    }).join('');
    const ref = o.ref ? `<line class="ref" x1="${PL}" x2="${W - PR}" y1="${py(o.ref)}" y2="${py(o.ref)}"/>` : '';
    const data = pts.map((p) => ({ x: px(p.t, o.start, o.end), t: p.t, v: series.map((s) => [s.label, p[s.key] == null ? '-' : o.fmt(p[s.key])]) }));
    return `<div class="rp-chart-wrap"><svg class="rp-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(series.map((s) => s.label).join(', '))}"
      data-points="${esc(JSON.stringify(data))}">${axes(o.start, o.end, max, o.fmt)}${ref}${paths}<line class="cross" x1="0" x2="0" y1="${PT}" y2="${H - PB}" style="display:none"/></svg>
      <div class="rp-tip" hidden></div></div>`;
  }
  function barChart(pts, key, o) {
    const max = Math.max(1, ...pts.map((p) => Number(p[key]) || 0)) * 1.05;
    const bw = Math.max(2, ((W - PL - PR) * (o.bucket * 1000)) / (o.end - o.start) - 2);
    const bars = pts.map((p) => {
      const h = (H - PT - PB) * ((Number(p[key]) || 0) / max);
      return `<rect class="bar" x="${(px(p.t, o.start, o.end)).toFixed(1)}" y="${(H - PB - h).toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="2"/>`;
    }).join('');
    const data = pts.map((p) => ({ x: px(p.t, o.start, o.end) + bw / 2, t: p.t, v: [[tr('rp.charts.sent'), o.fmt(p[key] || 0)], [tr('rp.charts.passes'), String(p.passes || 0)]] }));
    return `<div class="rp-chart-wrap"><svg class="rp-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(tr('rp.charts.sent'))}"
      data-points="${esc(JSON.stringify(data))}">${axes(o.start, o.end, max, o.fmt)}${bars}<line class="cross" x1="0" x2="0" y1="${PT}" y2="${H - PB}" style="display:none"/></svg>
      <div class="rp-tip" hidden></div></div>`;
  }
  /** Infobulle au survol : le point le plus proche, trait vertical. */
  function hookTooltip(svg) {
    const data = JSON.parse(svg.dataset.points || '[]');
    const tip = svg.parentElement.querySelector('.rp-tip');
    const cross = svg.querySelector('.cross');
    svg.addEventListener('mousemove', (e) => {
      const r = svg.getBoundingClientRect();
      const x = ((e.clientX - r.left) / r.width) * W;
      let best = null;
      data.forEach((p) => { if (!best || Math.abs(p.x - x) < Math.abs(best.x - x)) best = p; });
      if (!best) return;
      cross.setAttribute('x1', best.x); cross.setAttribute('x2', best.x); cross.style.display = '';
      tip.hidden = false;
      tip.innerHTML = `<b>${esc(U.when(best.t))}</b>${best.v.map(([k, val]) => `<div>${esc(k)}: ${esc(val)}</div>`).join('')}`;
      tip.style.left = `${Math.min(r.width - 160, Math.max(0, (best.x / W) * r.width + 10))}px`;
    });
    svg.addEventListener('mouseleave', () => { tip.hidden = true; cross.style.display = 'none'; });
  }

  // -- journal -------------------------------------------------------------------------------
  async function renderJournal(v, pane, fresh) {
    if (fresh) {
      v.journal = { events: [], next: null };
      pane.innerHTML = `<div class="btn-row"><label class="bk-check tip" data-tip="${esc(tr('rp.journal.warnTip'))}">
          <input type="checkbox" data-rpv="warn"${v.warnOnly ? ' checked' : ''}> <span>${esc(tr('rp.journal.warn'))}</span></label></div>
        <div data-rpv="journal"><p class="form-hint">${esc(tr('common.loading'))}</p></div>`;
      pane.querySelector('[data-rpv="warn"]').addEventListener('change', (e) => { v.warnOnly = e.target.checked; renderJournal(v, pane, true); });
    }
    const q = `limit=100${v.warnOnly ? '&level=warn' : ''}${v.journal.next ? `&before=${enc(v.journal.next)}` : ''}`;
    const d = await U.get(`/api/replication/plans/${enc(v.id)}/events?${q}`);
    const box = pane.querySelector('[data-rpv="journal"]');
    if (!box) return;
    if (d.error) { box.innerHTML = `<p class="res-error">${esc(d.error)}</p>`; return; }
    v.journal.events = v.journal.events.concat(d.events || []);
    v.journal.next = d.next_before;
    const names = {};
    ((v.data.status || {}).vms || []).forEach((x) => { names[x.uid] = `${x.namespace}/${x.name}`; });
    const evs = v.journal.events;
    box.innerHTML = evs.length ? `<div class="tw"><table class="data-table rp-journal"><thead><tr><th>${esc(tr('ev.col.when'))}</th><th>${esc(tr('rp.journal.level'))}</th>
        <th>${esc(tr('rp.journal.op'))}</th><th>${esc(tr('rp.col.vm'))}</th><th>${esc(tr('rp.journal.code'))}</th><th>${esc(tr('ev.col.message'))}</th><th>${esc(tr('rp.journal.by'))}</th></tr></thead>
      <tbody>${evs.map((e) => `<tr class="${e.level === 'error' ? 'ev-row-warn' : ''}" data-rp-ev="${esc(e.id)}">
        <td class="tip" data-tip="${esc(U.when(e.ts))}">${esc(U.ago(e.ts))}</td>
        <td><span class="badge ${e.level === 'error' ? 'fail' : (e.level === 'warn' ? 'warn' : '')}">${esc(e.level)}</span></td>
        <td>${esc(e.operation)}${e.step ? ` <span class="res-dim">${esc(e.step)}</span>` : ''}</td>
        <td>${esc(e.resource_uid ? (names[e.resource_uid] || e.resource_uid) : tr('rp.journal.plan'))}</td><td><code>${esc(e.code)}</code></td>
        <td class="ev-msg">${esc(e.message)}</td>
        <td>${esc(e.by || '')}${e.action_id ? ` <a href="#activity/action/${enc(e.action_id)}" data-rp-ev-action="${esc(e.action_id)}" class="tip" data-tip="${esc(tr('rp.journal.actionTip', { id: e.action_id }))}">${icon('activity', 11)}</a>` : ''}</td></tr>`).join('')}</tbody></table></div>
      ${v.journal.next ? `<button type="button" class="btn btn-sm btn-secondary tip" data-rpv="more" data-tip="${esc(tr('rp.journal.moreTip'))}">${esc(tr('rp.journal.more'))}</button>` : ''}`
      : `<p class="hint">${esc(tr('rp.journal.none'))}</p>`;
  }

  // -- réglages ------------------------------------------------------------------------------
  const field = (label, input, tip) => `<label class="bk-field"><span>${esc(label)}</span>${
    input.replace(/^<(input|select|textarea)/, `<$1 class="tip" data-tip="${esc(tip)}"`)}</label>`;
  const STEPS = [60, 120, 180, 240, 300, 360, 600, 720, 900, 1200, 1800, 3600, 7200, 10800, 14400, 21600, 28800, 43200, 86400];
  function renderSettings(v, pane) {
    const d = v.data;
    const p = d.plan;
    const o = p.options || {};
    const canOp = U.roleRank(d.role) >= 2;
    const dis = canOp ? '' : ' disabled';
    const recoveryOk = p.state !== 'failing_over';
    pane.innerHTML = `<form class="of-form rp-settings" autocomplete="off" novalidate>
      ${canOp ? '' : `<div class="sto-finding sev-info"><div class="sto-finding-title">${icon('info')} ${esc(tr('rp.viewerTip'))}</div></div>`}
      <section class="fk-step"><div class="fk-step-head"><b>${esc(tr('rp.set.follow'))}</b><span class="res-dim">${esc(tr('rp.set.followHint'))}</span></div>
        <div class="ct-create-row">
          ${field(tr('rp.f.rpo'), `<input name="rpo" type="number" min="1" max="10080" value="${Math.round((p.rpo_objective_s || 900) / 60)}"${dis}>`, tr('rp.f.rpoTip'))}
          ${field(tr('rp.set.skew'), `<input name="skew" type="number" min="0" max="3600" value="${o.skew_tolerance_s ?? 10}"${dis}>`, tr('rp.set.skewTip'))}
          ${field(tr('rp.set.follower'), `<select name="follow"${dis}>${['console', 'cli', 'off'].map((x) => `<option value="${x}"${(o.follow || 'console') === x ? ' selected' : ''}>${esc(x === 'console' ? tr('rp.set.followConsole') : (x === 'cli' ? tr('rp.set.followCli') : tr('rp.set.followOff')))}</option>`).join('')}</select>`, tr('rp.set.followerTip'))}
        </div>
        <label class="bk-check tip" data-tip="${esc(tr('rp.set.pausedTip'))}"><input type="checkbox" name="paused"${o.paused ? ' checked' : ''}${dis}> <span>${esc(tr('rp.set.paused'))}</span></label>
        <div class="btn-row"><button type="button" class="btn btn-sm btn-primary tip" data-rpv="save-follow" data-tip="${esc(tr('rp.set.saveTip'))}"${dis}>${icon('save')} ${esc(tr('rp.set.save'))}</button></div>
      </section>
      <section class="fk-step"><div class="fk-step-head"><b>${esc(tr('rp.set.cadence'))}</b><span class="res-dim">${esc(tr('rp.set.cadenceHint'))}</span></div>
        <div class="ct-create-row">
          ${field(tr('rp.set.interval'), `<select name="interval"${dis}><option value="">${esc(tr('rp.set.intervalKeep'))}</option>${STEPS.map((s) => `<option value="${s}"${p.interval_s === s ? ' selected' : ''}>${esc(U.dur(s))}</option>`).join('')}</select>`, tr('rp.set.intervalTip'))}
          ${field(tr('rp.f.retain'), `<input name="retain" type="number" min="1" max="100" value="${p.retain || 6}"${dis}>`, tr('rp.f.retainTip'))}
        </div>
        <div class="btn-row"><button type="button" class="btn btn-sm btn-secondary tip" data-rpv="save-cadence" data-tip="${esc(tr('rp.set.cadenceTip'))}"${dis}>${icon('save')} ${esc(tr('rp.set.applyCadence'))}</button></div>
      </section>
      <section class="fk-step"><div class="fk-step-head"><b>${esc(tr('rp.set.recovery'))}</b><span class="res-dim">${esc(tr('rp.set.recoveryHint'))}</span></div>
        <div data-rpv="recovery"></div>
        <div class="btn-row"><button type="button" class="btn btn-sm btn-primary tip" data-rpv="save-recovery" data-tip="${esc(tr('rp.set.saveRecoveryTip'))}"${canOp && recoveryOk ? '' : ' disabled'}>${icon('save')} ${esc(tr('rp.set.saveRecovery'))}</button></div>
      </section>
      <section class="fk-step rp-danger"><div class="fk-step-head"><b>${esc(tr('rp.set.danger'))}</b></div>
        <p class="form-hint">${esc(tr('rp.set.dangerHint'))}</p>
        <div class="btn-row">${U.actionButton(d.row, 'unprotect', 'btn-danger')}${U.actionButton(d.row, 'unprotect-delete-backups', 'btn-danger')}${U.actionButton(d.row, 'delete', 'btn-danger')}</div>
      </section>
      <div class="of-msg" data-rpv="set-msg" role="status"></div></form>`;
    if (window.ReplicationCreate) {
      v.recovery = ReplicationCreate.recoveryEditor(pane.querySelector('[data-rpv="recovery"]'), {
        plan: p, vms: ((d.status || {}).vms || []).map((x) => ({ namespace: x.namespace, name: x.name })),
        readOnly: !canOp, planId: p.id,
      });
    }
  }

  async function saveFollow(v, btn) {
    const f = v.root.querySelector('.rp-settings');
    const p = v.data.plan, o = p.options || {};
    const body = {};
    // le champ montre des minutes arrondies : seul un champ changé envoie l'objectif
    if (Number(f.rpo.value) !== Math.round((p.rpo_objective_s || 900) / 60)) body.rpo = `${Number(f.rpo.value)}m`;
    if (f.paused.checked !== !!o.paused) body.paused = f.paused.checked;
    if (f.follow.value !== (o.follow || 'console')) body.follow = f.follow.value;
    if (Number(f.skew.value) !== Number(o.skew_tolerance_s ?? 10)) body.skew_tolerance = `${Number(f.skew.value)}s`;
    const msg = v.root.querySelector('[data-rpv="set-msg"]');
    if (!Object.keys(body).length) { msg.textContent = tr('rp.set.nothing'); return; }
    // valeurs d'avant, pour « Annuler »
    const prev = {};
    if ('rpo' in body) prev.rpo = `${p.rpo_objective_s}s`;           // valeur exacte, en secondes
    if ('paused' in body) prev.paused = !!o.paused;
    if ('follow' in body) prev.follow = o.follow || 'console';
    if ('skew_tolerance' in body) prev.skew_tolerance = `${Number(o.skew_tolerance_s ?? 10)}s`;
    await send(v, btn, msg, 'PATCH', `/api/replication/plans/${enc(v.id)}/settings`, body, tr('rp.set.saved'), prev);
  }
  async function saveCadence(v, btn) {
    const f = v.root.querySelector('.rp-settings');
    const body = {};
    if (f.interval.value && Number(f.interval.value) !== v.data.plan.interval_s) body.interval = `${f.interval.value}s`;
    if (Number(f.retain.value) !== v.data.plan.retain) body.retain = Number(f.retain.value);
    const msg = v.root.querySelector('[data-rpv="set-msg"]');
    if (!Object.keys(body).length) { msg.textContent = tr('rp.set.nothing'); return; }
    await send(v, btn, msg, 'POST', `/api/replication/plans/${enc(v.id)}/protect`, body, tr('rp.set.cadenceDone'));
  }
  async function saveRecovery(v, btn) {
    const msg = v.root.querySelector('[data-rpv="set-msg"]');
    if (!v.recovery) return;
    const r = v.recovery.read();
    if (r.errors.length) { msg.innerHTML = `<span class="res-error">${esc(r.errors.join(' · '))}</span>`; return; }
    const body = { recovery: r.recovery, cloud_init: r.cloud_init, mac: r.mac, start: r.start };
    const o = v.data.plan.options || {};
    // « Annuler » renvoie exactement les clés changées ; pas d'annulation quand un secret
    // est touché (une valeur masquée ne se renvoie pas, une nouvelle resterait)
    const prev = { recovery: { waves: o.waves || [], hooks: o.hooks || [], network_data: o.network_data || {}, resize: o.resize || {} },
                   cloud_init: o.cloud_init || 'keep', mac: o.mac || 'keep', start: o.start || 'started' };
    Object.keys(r.recovery).forEach((k) => { if (!(k in prev.recovery)) prev.recovery[k] = k in o ? o[k] : false; });
    const secretOf = (hs) => JSON.stringify((hs || []).filter((h) => Object.keys(h.secret_headers || {}).length || String(h.url || '').includes('********'))
      .map((h) => [h.name, h.url, h.secret_headers]));
    const noUndo = secretOf(o.hooks) !== secretOf(r.recovery.hooks) ? tr('rp.undoNoSecret') : '';
    await send(v, btn, msg, 'PATCH', `/api/replication/plans/${enc(v.id)}/settings`, body,
      ['protected', 'lagging'].includes(v.data.plan.state) ? tr('rp.set.recoverySavedProtect') : tr('rp.set.saved'),
      noUndo ? null : prev, noUndo);
  }
  /** Bouton « Annuler » : renvoie les valeurs d'avant par la même route (action suivie). */
  function offerUndo(msg, method, url, prev, onDone) {
    if (!msg || !prev) return;
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn btn-sm btn-secondary tip rp-undo';
    b.dataset.rpUndo = '1';
    b.dataset.tip = tr('rp.undoTip');
    b.innerHTML = `${icon('undo')} ${esc(tr('rp.undo'))}`;
    b.addEventListener('click', async () => {
      b.disabled = true;
      try {
        const out = await U.api(method, url, prev);
        U.follow(out.action_id, msg, tr('rp.undone'), () => { if (onDone) onDone(); reloadLists(); });
      } catch (e) { msg.innerHTML = err(e); }
    });
    msg.appendChild(document.createTextNode(' '));
    msg.appendChild(b);
  }
  async function send(v, btn, msg, method, url, body, doneText, prev, noUndo) {
    if (btn) btn.disabled = true;
    try {
      const out = await U.api(method, url, body);
      U.follow(out.action_id, msg, doneText, (ok) => {
        if (btn) btn.disabled = false;
        const loaded = load(v); reloadLists();
        // après le nouveau rendu de la vue, qui recopie le message
        if (!ok) Promise.resolve(loaded).then(() => explainRefusal(out.action_id, v.root.querySelector('[data-rpv="msg"]') || msg));
        if (ok && prev) offerUndo(msg, method, url, prev, () => load(v));
        if (ok && noUndo) msg.insertAdjacentHTML('beforeend', ` <span class="res-dim" data-rp-noundo="1">${esc(noUndo)}</span>`);
      });
      return out;
    } catch (e) {
      if (btn) btn.disabled = false;
      msg.innerHTML = err(e);
      return null;
    }
  }

  async function onClick(v, e) {
    const tab = e.target.closest('[data-rpv-tab]');
    if (tab) { v.tab = tab.dataset.rpvTab; if (v.root.querySelector('[data-rpv-pane]')) v.root.querySelector('[data-rpv-pane]').remove(); render(v); return; }
    const rg = e.target.closest('[data-rpv-range]');
    if (rg) { v.range = rg.dataset.rpvRange; renderCharts(v, v.root.querySelector('.rp-view-pane')); return; }
    const act = e.target.closest('[data-rp-act]');
    if (act && !act.disabled) {
      const name = act.dataset.rpAct;
      if (name === 'failover' || name === 'failover-unplanned') { failover(v.id); return; }
      actOn(v.data, name, v.root.querySelector('[data-rpv="msg"]'), () => load(v));
      return;
    }
    const b = e.target.closest('[data-rpv]');
    if (!b || b.disabled || ['SELECT', 'INPUT', 'FORM'].includes(b.tagName)) return;
    const msg = v.root.querySelector('[data-rpv="msg"]');
    const k = b.dataset.rpv;
    if (k === 'failover') failover(v.id);
    else if (k === 'check') checkPlan(v, b, msg);
    else if (k === 'protect-again') send(v, b, msg, 'POST', `/api/replication/plans/${enc(v.id)}/protect`, {}, tr('rp.fo.protectFirstDone'));
    else if (k === 'resume-cmd') send(v, b, msg, 'POST', `/api/replication/plans/${enc(v.id)}/resume`, {}, tr('rp.act.resumeCmdDone'));
    else if (k === 'more') renderJournal(v, v.root.querySelector('.rp-view-pane'), false);
    else if (k === 'save-follow') saveFollow(v, b);
    else if (k === 'save-cadence') saveCadence(v, b);
    else if (k === 'save-recovery') saveRecovery(v, b);
  }

  /** Contrôle complet sans écriture ; les constats de la ligne de commande s'affichent. */
  async function checkPlan(v, btn, msg) {
    btn.disabled = true;
    try {
      const out = await U.api('POST', `/api/replication/plans/${enc(v.id)}/check`, { phase: 'protect' });
      U.follow(out.action_id, msg, tr('rp.act.checkDone'), async () => {
        btn.disabled = false;
        const o = await U.actionOutput(out.action_id);
        if (o) msg.innerHTML += findingsHtml(o.findings || (o.facts && o.facts.findings) || []);
      });
    } catch (e) { btn.disabled = false; msg.innerHTML = err(e); }
  }
  function findingsHtml(list) {
    if (!list.length) return '';
    return `<ul class="rp-findings">${list.map((f) => `<li class="${f.level === 'block' ? 'fail' : (f.level === 'warn' ? 'warn' : 'ok')}"><code>${esc(f.code)}</code>
      ${f.level === 'block' && findingSentence(f) ? `<span data-rp-hint="${esc(f.code)}">${esc(findingSentence(f))}</span>`
    : esc(Object.entries(f).filter(([k]) => !['code', 'level'].includes(k)).map(([k, x]) => `${k}=${typeof x === 'object' ? JSON.stringify(x) : x}`).join(' '))}</li>`).join('')}</ul>`;
  }
  /** Un refus connu dit en clair (quoi, pourquoi, comment l'accepter) ; '' pour les autres. */
  function findingSentence(f) {
    const x = f.facts || {};
    if (f.code === 'standby-replicas-exceed-nodes') {
      return tr('rp.hint.replicas', { sc: x.storage_class, replicas: x.replicas, nodes: x.nodes,
        suggest: (x.suggest || []).join(', ') || '-', box: tr('rp.c.ackDegraded') });
    }
    return '';
  }
  /** Après un échec : les refus connus de la sortie de l'outil, en clair. */
  async function explainRefusal(actionId, msg) {
    const o = await U.actionOutput(actionId);
    const lines = ((o && o.findings) || []).filter((f) => f.level === 'block').map(findingSentence).filter(Boolean);
    if (!lines.length) return;
    if (msg) msg.insertAdjacentHTML('beforeend', lines.map((t) => `<p class="form-hint res-error" data-rp-hint="1">${esc(t)}</p>`).join(''));
    else if (window.VMActions && VMActions.toast) VMActions.toast(lines.join(' '), true);
  }

  // ===========================================================================
  // Gestes d'un plan, partagés avec la liste des contrats
  // ===========================================================================
  async function act(planId, name, btn, onDone) {
    const d = await getPlan(planId);
    if (d.error) { if (window.VMActions && VMActions.toast) VMActions.toast(d.error, true); return; }
    // depuis la liste : la liste se repeint, le résultat se dit par une notification (et reste au dock)
    actOn(d, name, null, onDone);
  }
  async function actOn(d, name, msg, onDone) {
    const id = d.plan.id;
    const url = (s) => `/api/replication/plans/${enc(id)}${s}`;
    const go = async (method, path, body, done, prev) => {
      try {
        const out = await U.api(method, url(path), body);
        U.follow(out.action_id, msg, done, (ok) => {
          const loaded = onDone ? onDone() : null; reloadLists();
          // message remplacé par un nouveau rendu : la phrase part en notification
          if (!ok) Promise.resolve(loaded).then(() => explainRefusal(out.action_id, msg && msg.isConnected ? msg : null));
          if (ok && prev) offerUndo(msg, method, url(path), prev, onDone);
        });
      } catch (e) { if (msg) msg.innerHTML = err(e); else if (window.VMActions && VMActions.toast) VMActions.toast(e.message, true); }
    };
    if (name === 'measure-start') return go('POST', '/measure', { op: 'start' }, tr('rp.done.measureStart'));
    if (name === 'measure-stop') return go('POST', '/measure', { op: 'stop' }, tr('rp.done.measureStop'));
    if (name === 'protect') return go('POST', '/protect', {}, tr('rp.done.protect'));
    if (name === 'pause') return go('PATCH', '/settings', { paused: true }, tr('rp.done.pause'), { paused: false });
    if (name === 'resume') return go('PATCH', '/settings', { paused: false }, tr('rp.done.resume'), { paused: true });
    if (name === 'unprotect' || name === 'unprotect-delete-backups' || name === 'delete') return confirmDanger(d, name, onDone);
    return null;
  }

  /** Déprotéger, supprimer les sauvegardes, supprimer le plan : nom tapé. */
  function confirmDanger(d, name, onDone) {
    const p = d.plan;
    const title = name === 'delete' ? tr('rp.danger.deleteTitle', { name: p.name })
      : (name === 'unprotect' ? tr('rp.danger.unprotectTitle', { name: p.name }) : tr('rp.danger.unprotectDeleteTitle', { name: p.name }));
    const text = name === 'delete' ? tr('rp.danger.deleteText') : (name === 'unprotect' ? tr('rp.danger.unprotectText', { target: p.target_cluster })
      : tr('rp.danger.unprotectDeleteText'));
    const panel = FloatingPanels.open({ id: `rp-danger-${p.id}`, icon: 'warn', width: 560, height: 380, title,
      bodyHtml: `<form class="of-form rp-confirm" autocomplete="off"><div class="sto-finding sev-critical"><div class="sto-finding-title">${icon('warn')} ${esc(title)}</div>
        <p class="sto-finding-facts">${esc(text)}</p></div>
        ${field(tr('rp.fo.typeName', { name: p.name }), '<input name="confirm" autocomplete="off" spellcheck="false">', tr('rp.fo.typeNameTip'))}
        <div class="bk-form-actions"><button type="submit" class="btn btn-sm btn-danger tip" data-rp-confirm disabled data-tip="${esc(tr('rp.danger.goTip'))}">${esc(U.ACTION_LABEL[name]())}</button></div>
        <div class="of-msg" role="status"></div></form>` });
    const f = panel.el.querySelector('form');
    const btn = f.querySelector('[data-rp-confirm]');
    f.confirm.addEventListener('input', () => { btn.disabled = f.confirm.value !== p.name; });
    f.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (f.confirm.value !== p.name) return;
      btn.disabled = true;
      const msg = f.querySelector('.of-msg');
      try {
        const out = name === 'delete' ? await U.api('DELETE', `/api/replication/plans/${enc(p.id)}`)
          : await U.api('POST', `/api/replication/plans/${enc(p.id)}/unprotect`, { confirm: p.name, delete_backups: name === 'unprotect-delete-backups' });
        U.follow(out.action_id, msg, name === 'delete' ? tr('rp.done.delete') : tr('rp.done.unprotect'), (ok) => {
          if (onDone) onDone(); reloadLists();
          if (ok && name === 'delete') FloatingPanels.close(`rp-plan-${p.id}`);
        });
      } catch (er) { btn.disabled = false; msg.innerHTML = err(er); }
    });
    f.confirm.focus();
    return panel;
  }

  // ===========================================================================
  // Bascule : étapes, essai à blanc, nom tapé, administrateur seulement
  // ===========================================================================
  const STEP_KEYS = {
    planned: ['rp.fo.step.pause', 'rp.fo.step.stop', 'rp.fo.step.fence', 'rp.fo.step.final', 'rp.fo.step.apply',
              'rp.fo.step.activate', 'rp.fo.step.objects', 'rp.fo.step.waves', 'rp.fo.step.hooks'],
    unplanned: ['rp.fo.step.uSync', 'rp.fo.step.uCatchup', 'rp.fo.step.uPoints', 'rp.fo.step.activate',
                'rp.fo.step.objects', 'rp.fo.step.waves', 'rp.fo.step.hooks', 'rp.fo.step.uFence'],
  };
  // littéraux pour le contrôle de parité i18n
  const STEP_TEXT = {
    'rp.fo.step.pause': () => tr('rp.fo.step.pause'), 'rp.fo.step.stop': () => tr('rp.fo.step.stop'),
    'rp.fo.step.fence': () => tr('rp.fo.step.fence'), 'rp.fo.step.final': () => tr('rp.fo.step.final'),
    'rp.fo.step.apply': () => tr('rp.fo.step.apply'), 'rp.fo.step.activate': () => tr('rp.fo.step.activate'),
    'rp.fo.step.objects': () => tr('rp.fo.step.objects'), 'rp.fo.step.waves': () => tr('rp.fo.step.waves'),
    'rp.fo.step.hooks': () => tr('rp.fo.step.hooks'), 'rp.fo.step.uSync': () => tr('rp.fo.step.uSync'),
    'rp.fo.step.uCatchup': () => tr('rp.fo.step.uCatchup'), 'rp.fo.step.uPoints': () => tr('rp.fo.step.uPoints'),
    'rp.fo.step.uFence': () => tr('rp.fo.step.uFence'),
  };
  const ELIGIBLE = ['protected', 'lagging', 'drifting', 'degraded', 'source-unreachable', 'failing_over', 'error'];

  async function failover(planId) {
    const panel = FloatingPanels.open({ id: `rp-failover-${planId}`, icon: 'migrate', width: 720, height: 780, title: tr('rp.fo.titleLoading'),
      bodyHtml: `<form class="of-form rp-failover" autocomplete="off" novalidate><p class="form-hint">${esc(tr('common.loading'))}</p></form>` });
    const f = panel.el.querySelector('form');
    if (f.dataset.ready) return panel;
    const d = await getPlan(planId);
    if (d.error) { f.innerHTML = `<p class="res-error">${esc(d.error)}</p>`; return panel; }
    f.dataset.ready = '1';
    const p = d.plan, row = d.row;
    panel.setTitle(tr('rp.fo.title', { name: p.name, target: p.target_cluster }));
    const isAdmin = d.role === 'admin';
    const planned = U.action(row, 'failover'), unplanned = U.action(row, 'failover-unplanned');
    const mode0 = planned ? 'planned' : 'unplanned';
    const opts = p.options || {};
    const waves = opts.waves || [];
    const hooks = opts.hooks || [];
    const res = (row.resources || []).filter((r) => ELIGIBLE.includes(r.state));
    const maxRpo = row.rpo_held_s;
    f.innerHTML = `
      <div class="sto-finding sev-critical"><div class="sto-finding-title">${icon('warn')} ${esc(tr('rp.fo.definitive'))}</div>
        <p class="sto-finding-facts">${esc(tr('rp.fo.definitiveText', { source: p.source_cluster, target: p.target_cluster }))}</p></div>
      ${isAdmin ? '' : `<div class="sto-finding sev-action" data-rp-fo="notadmin"><div class="sto-finding-title">${icon('lock')} ${esc(tr('rp.fo.adminOnly', { role: d.role }))}</div></div>`}
      <fieldset class="rp-fo-modes"><legend>${esc(tr('rp.fo.mode'))}</legend>
        <label class="bk-check tip" data-tip="${esc(tr('rp.fo.plannedTip'))}"><input type="radio" name="mode" value="planned"${mode0 === 'planned' ? ' checked' : ''}${planned ? '' : ' disabled'}> <span>${esc(tr('rp.fo.planned'))}</span></label>
        <label class="bk-check tip" data-tip="${esc(tr('rp.fo.unplannedTip'))}"><input type="radio" name="mode" value="unplanned"${mode0 === 'unplanned' ? ' checked' : ''}${unplanned ? '' : ' disabled'}> <span>${esc(tr('rp.fo.unplanned'))}</span></label>
        <div class="rp-fo-unplanned" data-rp-fo="uopts">
          <label class="bk-check tip" data-tip="${esc(tr('rp.fo.forceTip'))}"><input type="checkbox" name="force_unplanned"> <span>${esc(tr('rp.fo.force'))}</span></label>
          <label class="bk-check tip" data-tip="${esc(tr('rp.fo.skewTip'))}"><input type="checkbox" name="accept_skew"> <span>${esc(tr('rp.fo.skew'))}</span></label>
        </div></fieldset>
      <fieldset><legend>${esc(tr('rp.fo.vms'))}</legend>
        <label class="bk-check tip" data-tip="${esc(tr('rp.fo.allTip'))}"><input type="checkbox" name="all" checked> <span>${esc(tr('rp.fo.all', { n: res.length }))}</span></label>
        <div class="rp-fo-vms" data-rp-fo="vms" hidden>${res.map((r) => `<label class="bk-check"><input type="checkbox" name="vm" value="${esc(`${r.namespace}/${r.name}`)}" checked>
          <span>${esc(`${r.namespace}/${r.name}`)} ${U.stateBadge(r.state)} <span class="res-dim">${esc(r.rpo_held_s == null ? '' : tr('rp.fo.vmRpo', { rpo: U.dur(r.rpo_held_s) }))}</span></span></label>`).join('')}</div>
      </fieldset>
      <div class="rp-fo-loss" data-rp-fo="loss"></div>
      <h4>${esc(tr('rp.chk.title'))}</h4><ul class="rp-issues rp-checks" data-rp-fo="checks"></ul>
      <h4>${esc(tr('rp.fo.steps'))}</h4><ol class="rp-steps" data-rp-fo="steps"></ol>
      ${waves.length ? `<p class="form-hint">${esc(tr('rp.fo.wavesLine', { list: waves.map((w, i) => `${i + 1}. ${w.name || tr('rp.wave.n', { n: i + 1 })} (${(w.vms || []).length})`).join(' · ') }))}</p>` : ''}
      ${hooks.length ? `<p class="form-hint">${esc(tr('rp.fo.hooksLine', { list: hooks.map((h) => `${h.name} (${h.at})`).join(', ') }))}</p>` : ''}
      <div class="btn-row"><button type="button" class="btn btn-sm btn-secondary tip" data-rp-fo="dry" data-tip="${esc(tr('rp.fo.dryTip'))}"${isAdmin ? '' : ' disabled'}>${icon('test')} ${esc(tr('rp.fo.dry'))}</button></div>
      <div class="rp-fo-dry" data-rp-fo="dryout"></div>
      <div data-rp-fo="stale" hidden></div>
      ${field(tr('rp.fo.typeName', { name: p.name }), `<input name="confirm" autocomplete="off" spellcheck="false"${isAdmin ? '' : ' disabled'}>`, tr('rp.fo.typeNameTip'))}
      <div class="bk-form-actions"><button type="submit" class="btn btn-sm btn-danger tip" data-rp-fo="go" disabled
        data-tip="${esc(isAdmin ? tr('rp.fo.goTip') : tr('rp.fo.adminOnly', { role: d.role }))}">${icon('migrate')} ${esc(tr('rp.fo.go'))}</button></div>
      <div class="of-msg" data-rp-fo="msg" role="status" aria-live="polite"></div>`;
    const $ = (s) => f.querySelector(s);
    const mode = () => (f.querySelector('[name="mode"]:checked') || {}).value || mode0;
    const dry = { state: 'none', why: '', stale: null };
    const li = (ok, text) => `<li class="${ok === true ? 'ok' : (ok === false ? 'fail' : 'warn')}" data-rp-fo-check="${ok === true ? 'ok' : (ok === false ? 'fail' : 'warn')}">${icon(ok === true ? 'ok' : (ok === false ? 'fail' : 'info'), 11)} ${esc(text)}</li>`;
    const reachLi = (v, cluster) => li(v === true ? true : (v === false ? false : null),
      v === true ? tr('rp.chk.reach', { cluster }) : (v === false ? tr('rp.chk.reachNo', { cluster }) : tr('rp.chk.reachUnknown', { cluster })));
    /** Droits, état du plan, joignabilité des deux sites, essai à blanc : avant le bouton. */
    const checks = () => {
      const m = mode();
      const a = m === 'planned' ? planned : unplanned;
      const reach = row.reach || {};
      const modeText = m === 'planned' ? tr('rp.act.failover') : tr('rp.act.failoverUnplanned');
      const out = [li(isAdmin, isAdmin ? tr('rp.chk.admin') : tr('rp.chk.adminNo', { role: d.role })),
        li(!!a, a ? tr('rp.chk.allowed', { mode: modeText }) : tr('rp.chk.notAllowed', { mode: modeText })),
        reachLi(reach.source, p.source_cluster), reachLi(reach.target, p.target_cluster)];
      if (dry.state === 'running') out.push(li(null, tr('rp.chk.dryRunning')));
      if (dry.state === 'ok') out.push(li(true, tr('rp.chk.dry')));
      if (dry.state === 'fail') out.push(li(false, tr('rp.chk.dryNo', { why: dry.why })));
      if (dry.stale) out.push(li(staleOk() ? null : false, tr('rp.chk.stale')));
      $('[data-rp-fo="checks"]').innerHTML = out.join('');
    };
    const sync = () => {
      checks();
      const m = mode();
      $('[data-rp-fo="uopts"]').hidden = m !== 'unplanned';
      $('[data-rp-fo="vms"]').hidden = $('[name="all"]').checked;
      $('[data-rp-fo="steps"]').innerHTML = STEP_KEYS[m].map((k, i) => `<li data-step="${esc(k)}"${k === 'rp.fo.step.activate' ? ' class="rp-noreturn"' : ''}>
        <span class="rp-step-n">${i + 1}</span> ${esc(STEP_TEXT[k]())}${k === 'rp.fo.step.activate' ? ` <span class="badge fail">${esc(tr('rp.fo.noReturn'))}</span>` : ''}</li>`).join('');
      $('[data-rp-fo="loss"]').innerHTML = m === 'planned' ? `<p class="hint ok-t">${icon('ok')} ${esc(tr('rp.fo.lossPlanned'))}</p>`
        : `<p class="hint warn-t">${icon('warn')} ${esc(tr('rp.fo.lossUnplanned', { rpo: maxRpo == null ? '?' : U.dur(maxRpo) }))}</p>`;
      const sel = $('[name="all"]').checked || f.querySelectorAll('[name="vm"]:checked').length;
      $('[data-rp-fo="go"]').disabled = !isAdmin || f.confirm.value !== p.name || !sel || (dry.stale && !staleOk());
    };
    const body = (dry) => {
      const b = { mode: mode(), dry_run: !!dry };
      if ($('[name="all"]').checked) b.all_vms = true;
      else b.vms = [...f.querySelectorAll('[name="vm"]:checked')].map((x) => x.value);
      if (b.mode === 'unplanned') { b.force_unplanned = f.force_unplanned.checked; b.accept_skew = f.accept_skew.checked; }
      if (staleOk()) b.accept_stale_carnet = true;
      if (!dry) b.confirm = f.confirm.value;
      return b;
    };
    const staleOk = () => !!(f.querySelector('[name="accept_stale"]') || {}).checked;
    /** Réglages de reprise d'avant le dernier changement : protéger d'abord, ou basculer avec eux (accord explicite). */
    const renderStale = () => {
      const el = $('[data-rp-fo="stale"]');
      const s = dry.stale;
      el.hidden = !s;
      if (!s) { el.innerHTML = ''; return; }
      const fx = s.facts || {};
      const was = staleOk();
      el.innerHTML = `<div class="sto-finding sev-action"><div class="sto-finding-title">${icon('warn')} ${esc(tr('rp.stale.title'))}</div>
        ${(fx.keys || []).length ? `<p class="sto-finding-facts">${esc(tr('rp.stale.keys', { keys: fx.keys.join(', ') }))}</p>` : ''}
        ${(fx.vms || []).length ? `<p class="sto-finding-facts">${esc(tr('rp.stale.vms', { vms: fx.vms.join(', ') }))}</p>` : ''}
        <div class="btn-row"><button type="button" class="btn btn-sm btn-primary tip" data-rp-fo="protect-first" data-tip="${esc(tr('rp.fo.protectFirstTip'))}"${isAdmin ? '' : ' disabled'}>${icon('shield')} ${esc(tr('rp.fo.protectFirst'))}</button></div>
        <label class="bk-check tip" data-tip="${esc(tr('rp.fo.acceptStaleTip'))}"><input type="checkbox" name="accept_stale"${was ? ' checked' : ''}${isAdmin ? '' : ' disabled'}> <span>${esc(tr('rp.fo.acceptStale'))}</span></label>
        <div class="of-msg" data-rp-fo="stalemsg" role="status"></div></div>`;
    };
    f.addEventListener('change', (e) => { if (e.target.name === 'accept_stale') { runDry(); return; } sync(); });
    f.addEventListener('input', sync);
    f.addEventListener('click', async (e) => {
      const b = e.target.closest('[data-rp-fo="protect-first"]');
      if (!b || b.disabled) return;
      b.disabled = true;
      const msg = $('[data-rp-fo="stalemsg"]');
      try {
        const r = await U.api('POST', `/api/replication/plans/${enc(p.id)}/protect`, {});
        U.follow(r.action_id, msg, tr('rp.fo.protectFirstDone'), (ok) => { b.disabled = false; if (ok) runDry(); });
      } catch (er) { b.disabled = false; msg.innerHTML = err(er); }
    });
    sync();
    const runDry = async () => {
      const out = $('[data-rp-fo="dryout"]');
      dry.state = 'running'; checks();
      try {
        const r = await U.api('POST', `/api/replication/plans/${enc(p.id)}/failover`, body(true));
        U.follow(r.action_id, out, tr('rp.fo.dryDone'), async (ok) => {
          const o = await U.actionOutput(r.action_id);
          dry.state = ok ? 'ok' : 'fail';
          dry.why = (o && (o.message || o.error || o.code)) || '';
          dry.stale = ((o && o.findings) || []).find((x) => x.code === 'carnet-stale') || null;
          renderStale();
          sync();
          if (o) out.innerHTML += dryHtml(o);
        });
      } catch (e) { dry.state = 'fail'; dry.why = e.message; checks(); out.innerHTML = err(e); }
    };
    $('[data-rp-fo="dry"]').addEventListener('click', runDry);
    // essai à blanc d'office pour un administrateur : les contrôles avant le bouton
    if (isAdmin && (planned || unplanned)) runDry();
    f.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!isAdmin || f.confirm.value !== p.name) return;
      const go = $('[data-rp-fo="go"]');
      go.disabled = true;
      const msg = $('[data-rp-fo="msg"]');
      try {
        const r = await U.api('POST', `/api/replication/plans/${enc(p.id)}/failover`, body(false));
        U.follow(r.action_id, msg, tr('rp.fo.done'), () => { reloadLists(); const v = views.get(p.id); if (v) load(v); });
      } catch (er) { msg.innerHTML = err(er); sync(); }
    });
    return panel;
  }
  function dryHtml(o) {
    const pa = o.point_at || {}, sk = o.skew_s || {}, lost = o.lost_after || {};
    const keys = Object.keys(Object.assign({}, pa, sk));
    return `${keys.length ? `<table class="data-table rp-dry"><thead><tr><th>${esc(tr('rp.col.vm'))}</th><th>${esc(tr('rp.fo.point'))}</th>
      <th>${esc(tr('rp.kpi.skew'))}</th><th>${esc(tr('rp.fo.lostAfter'))}</th></tr></thead><tbody>${keys.map((k) => `<tr><td>${esc(k)}</td>
      <td>${esc(U.when(pa[k]))}</td><td>${esc(sk[k] == null ? '-' : U.dur(sk[k]))}</td><td>${esc(lost[k] ? U.when(lost[k]) : '-')}</td></tr>`).join('')}</tbody></table>` : ''}
      ${(o.waves || []).length ? `<p class="form-hint">${esc(tr('rp.fo.wavesLine', { list: o.waves.map((w) => `${w.index}. ${(w.vms || []).join(', ')}`).join(' · ') }))}</p>` : ''}
      ${o.message ? `<p class="form-hint">${esc(o.message)}</p>` : ''}${findingsHtml(o.findings || [])}`;
  }

  /** La fenêtre de ce contrat est-elle ouverte ? (adresse #dr/<id>) */
  const isOpen = (planId) => views.has(planId);
  return { open, isOpen, act, failover, confirmDanger };
})();
window.ReplicationPlan = ReplicationPlan;
