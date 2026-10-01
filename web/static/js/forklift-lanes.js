/**
 * harvester-ops : vue en couloirs des vagues de migration VMware (v1.80.0)
 *
 * Toutes les vagues sur un même axe du temps, un couloir par vague : la
 * ligne de maintenant, chaque copie déjà faite (la complète, puis les
 * incrémentales), la prochaine copie, la bascule prévue (avec son compte à
 * rebours), la fenêtre de bascule une fois vécue, l'état de la vague. Un
 * clic sur un couloir ouvre la fenêtre de suivi (fournie par l'appelant).
 *
 * Partagée par l'onglet Vagues d'un cluster (Forklift.js) et par la vue
 * « Migrations (tous clusters) » (ForkliftGlobal.js). Rendu en HTML/CSS
 * positionné en pourcentages de l'axe : aucune mesure de largeur, la vue
 * suit la taille de la carte. Le zoom et la fenêtre de maintenance sont
 * des aides visuelles gardées dans ce navigateur seulement (par portée :
 * un cluster, ou la vue globale) ; rien n'est envoyé au serveur.
 *
 * La relecture des données est celle de l'onglet appelant : `paint` ne
 * redessine que les couloirs, jamais la barre d'outils (une saisie de
 * fenêtre de maintenance n'est pas effacée par la relecture de fond).
 */
const ForkliftLanes = (() => {
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 13) => (window.Icons ? Icons.svg(n, { size }) : '');
  const MIN = 60e3, HOUR = 3600e3, DAY = 86400e3;
  const ZOOMS = { '6h': 6 * HOUR, '24h': DAY, '7d': 7 * DAY };
  const RUNNING = ['copying', 'cutover-scheduled', 'cutting-over'];
  // couleur du bord d'un couloir, comme le badge d'état des deux vues
  const STATE_CLS = {
    ready: 'info', pending: 'warn', invalid: 'fail', copying: 'info', 'cutover-scheduled': 'warn',
    'cutting-over': 'warn', succeeded: 'ok', failed: 'fail', 'rolled-back': 'warn', closed: '',
  };
  const TICK_STEPS = [5 * MIN, 10 * MIN, 15 * MIN, 30 * MIN, HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR,
                      12 * HOUR, DAY, 2 * DAY, 7 * DAY];

  const ts = (iso) => { const t = Date.parse(iso || ''); return Number.isFinite(t) ? t : null; };
  const fmtWhen = (t) => new Date(t).toLocaleString();
  const fmtDur = (sec) => {
    const s = Math.max(0, Math.round(sec));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    if (h) return `${h} h ${String(m).padStart(2, '0')} min`;
    if (m) return `${m} min ${String(r).padStart(2, '0')} s`;
    return `${r} s`;
  };
  // même texte que le compte à rebours de l'onglet Vagues (data-fk-at,
  // tenu à jour chaque seconde par Forklift.tick)
  const left = (t) => { const ms = t - Date.now(); return ms > 0 ? tr('fk.w.in', { left: fmtDur(ms / 1000) }) : tr('fk.w.due'); };

  // -- mémoire du navigateur (jamais indispensable) -------------------------------
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch { /* navigation privée */ } },
  };
  const zoomKey = (scope) => `harvester_ops_fk_lanes_zoom:${scope}`;
  const maintKey = (scope) => `harvester_ops_fk_maint:${scope}`;
  function getZoom(scope) {
    const z = store.get(zoomKey(scope));
    return z === 'fit' || ZOOMS[z] ? z : 'fit';
  }
  function getMaint(scope) {
    try {
      const m = JSON.parse(store.get(maintKey(scope)) || 'null');
      return m && typeof m === 'object' ? { start: String(m.start || ''), end: String(m.end || '') } : { start: '', end: '' };
    } catch { return { start: '', end: '' }; }
  }
  function setMaint(scope, m) {
    store.set(maintKey(scope), m.start || m.end ? JSON.stringify(m) : null);
  }
  /** La fenêtre de maintenance en instants, seulement si elle est complète et dans l'ordre. */
  function maintRange(scope) {
    const m = getMaint(scope);
    const a = ts(m.start), b = ts(m.end);   // datetime-local : heure locale du navigateur
    return a != null && b != null && b > a ? [a, b] : null;
  }

  // -- axe du temps --------------------------------------------------------------
  const waveStart = (w) => {
    let t = ts(w.started) ?? ts(w.created);
    (w.vms || []).forEach((v) => (v.copies || []).forEach((c) => {
      const s = ts(c.start);
      if (s != null && (t == null || s < t)) t = s;
    }));
    return t;
  };

  /** [début, fin] de l'axe. Ajusté : de la plus ancienne vague à
   *  max(maintenant + 2 h, dernière bascule prévue + 30 min). Zoomé : la
   *  durée choisie autour de maintenant, deux tiers passés, un tiers à venir. */
  function domain(rows, zoom, now) {
    if (ZOOMS[zoom]) return [now - ZOOMS[zoom] * 2 / 3, now + ZOOMS[zoom] / 3];
    let start = null, cut = null;
    rows.forEach(({ wave: w }) => {
      const s = waveStart(w);
      if (s != null && (start == null || s < start)) start = s;
      const c = ts(w.cutover);
      if (c != null && (cut == null || c > cut)) cut = c;
    });
    const end = Math.max(now + 2 * HOUR, cut != null ? cut + 30 * MIN : 0);
    if (start == null || start > now) start = Math.min(now, end) - HOUR;
    return [start - (end - start) * 0.02, end];
  }

  function ticks(d0, d1) {
    const span = d1 - d0;
    const step = TICK_STEPS.find((s) => span / s <= 8) || TICK_STEPS[TICK_STEPS.length - 1];
    // graduations calées sur l'heure locale (minuit local pour les jours)
    const off = new Date(d0).getTimezoneOffset() * MIN;
    const out = [];
    for (let t = Math.ceil((d0 - off) / step) * step + off; t <= d1; t += step) {
      const dt = new Date(t);
      const midnight = dt.getHours() === 0 && dt.getMinutes() === 0;
      const label = step >= DAY || midnight
        ? dt.toLocaleDateString(undefined, { day: '2-digit', month: 'short' })
          + (step < DAY ? ` ${dt.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}` : '')
        : dt.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
      out.push([t, label]);
    }
    return out;
  }

  // -- rendu ------------------------------------------------------------------
  function toolsHtml(scope) {
    const zoom = getZoom(scope);
    const m = getMaint(scope);
    const chip = (z, label, tip) => `<button type="button" class="res-chip fkl-chip tip${zoom === z ? ' is-on' : ''}"
        data-fkl-zoom="${z}" aria-pressed="${zoom === z}" data-tip="${esc(tip)}">${esc(label)}</button>`;
    return `<div class="fkl-tools" data-fkl="tools">
        <span class="fkl-zooms" role="group" aria-label="${esc(tr('fkl.zoom'))}">${icon('search', 12)}
          ${chip('6h', tr('fkl.zoom.6h'), tr('fkl.t.zoom6h'))}${chip('24h', tr('fkl.zoom.24h'), tr('fkl.t.zoom24h'))}
          ${chip('7d', tr('fkl.zoom.7d'), tr('fkl.t.zoom7d'))}${chip('fit', tr('fkl.zoom.fit'), tr('fkl.t.zoomFit'))}</span>
        <span class="fkl-maint-form"><span class="form-hint">${esc(tr('fkl.maint'))}</span>
          <input type="datetime-local" class="tip" data-fkl-maint="start" value="${esc(m.start)}"
            aria-label="${esc(tr('fkl.maintStart'))}" data-tip="${esc(tr('fkl.t.maintStart'))}">
          <span class="form-hint">-</span>
          <input type="datetime-local" class="tip" data-fkl-maint="end" value="${esc(m.end)}"
            aria-label="${esc(tr('fkl.maintEnd'))}" data-tip="${esc(tr('fkl.t.maintEnd'))}">
          <button type="button" class="btn btn-sm btn-secondary tip" data-fkl="maint-clear" data-tip="${esc(tr('fkl.t.maintClear'))}">${icon('clean', 12)} ${esc(tr('fkl.maintClear'))}</button></span>
        <span class="fkl-legend">
          <span class="fkl-key tip" data-tip="${esc(tr('fkl.t.legendFull'))}"><i class="fkl-sw fkl-sw-full"></i>${esc(tr('fkl.legend.full'))}</span>
          <span class="fkl-key tip" data-tip="${esc(tr('fkl.t.legendIncr'))}"><i class="fkl-sw fkl-sw-incr"></i>${esc(tr('fkl.legend.incr'))}</span>
          <span class="fkl-key tip" data-tip="${esc(tr('fkl.t.legendNext'))}"><i class="fkl-sw fkl-sw-next"></i>${esc(tr('fkl.legend.next'))}</span>
          <span class="fkl-key tip" data-tip="${esc(tr('fkl.t.legendCutover'))}"><i class="fkl-sw fkl-sw-cut"></i>${esc(tr('fkl.legend.cutover'))}</span>
          <span class="fkl-key tip" data-tip="${esc(tr('fkl.t.legendWindow'))}"><i class="fkl-sw fkl-sw-win"></i>${esc(tr('fkl.legend.window'))}</span>
          <span class="fkl-key tip" data-tip="${esc(tr('fkl.t.legendMaint'))}"><i class="fkl-sw fkl-sw-maint"></i>${esc(tr('fkl.legend.maint'))}</span>
        </span>
      </div>`;
  }

  function bodyHtml(rows, opts) {
    const now = Date.now();
    const [d0, d1] = domain(rows, getZoom(opts.scope), now);
    const pct = (t) => ((t - d0) / (d1 - d0)) * 100;
    const clamp = (p) => Math.max(0, Math.min(100, p));
    const band = (a, b, cls, tip, attrs = '') => {
      if (b < d0 || a > d1) return '';
      const l = clamp(pct(a)), r = clamp(pct(b));
      return `<span class="${cls} tip" style="left:${l.toFixed(3)}%;width:${Math.max(0, r - l).toFixed(3)}%" data-tip="${esc(tip)}"${attrs}></span>`;
    };
    const point = (t, cls, tip, attrs = '') => (t < d0 || t > d1 ? ''
      : `<span class="${cls} tip" style="left:${pct(t).toFixed(3)}%" data-tip="${esc(tip)}"${attrs}></span>`);
    const maint = maintRange(opts.scope);
    const maintBand = maint ? band(maint[0], maint[1], 'fkl-maint',
      tr('fkl.maintTip', { from: fmtWhen(maint[0]), to: fmtWhen(maint[1]) }), ' data-fkl="maint"') : '';
    const nowLine = point(now, 'fkl-now', tr('fkl.t.now', { when: fmtWhen(now) }), ' data-fkl="now"');

    const axis = `<div class="fkl-row fkl-axis-row"><div class="fkl-label form-hint">${esc(tr('fkl.axis'))}</div>
        <div class="fkl-axis tip" data-fkl="axis" data-tip="${esc(tr('fkl.t.axis', { from: fmtWhen(d0), to: fmtWhen(d1) }))}">
          ${maintBand}
          ${ticks(d0, d1).map(([t, label]) => `<span class="fkl-tick" style="left:${pct(t).toFixed(3)}%"><span>${esc(label)}</span></span>`).join('')}
          ${point(now, 'fkl-now fkl-now-label', tr('fkl.t.now', { when: fmtWhen(now) }), ' data-fkl="now-label"').replace('></span>', `><b>${esc(tr('fkl.now'))}</b></span>`)}
        </div></div>`;

    const lanes = rows.map(({ cluster, wave: w }) => {
      const running = RUNNING.includes(w.state);
      const marks = [];
      // fenêtre de bascule vécue : du premier début d'étape Cutover à la
      // dernière fin des VMs (maintenant tant qu'une VM n'a pas fini)
      let ws = null, we = null, open = false;
      (w.vms || []).forEach((v) => {
        const cw = v.cutover_window;
        if (!cw) return;
        const a = ts(cw.start), b = ts(cw.end);
        if (a == null) return;
        if (ws == null || a < ws) ws = a;
        if (b == null) open = true; else if (we == null || b > we) we = b;
      });
      if (ws != null) {
        const end = open ? now : (we ?? ws);
        marks.push(band(ws, end, 'fkl-window', tr('fkl.windowTip', {
          from: fmtWhen(ws), to: open ? tr('fkl.inProgress') : fmtWhen(end), dur: fmtDur((end - ws) / 1000) }),
          ' data-fkl="window"'));
      }
      (w.vms || []).forEach((v) => {
        const vm = v.name || v.id;
        (v.copies || []).forEach((c, i) => {
          const a = ts(c.start);
          if (a == null) return;
          let b = ts(c.end);
          const kind = i === 0 ? tr('fkl.copyFull') : tr('fkl.copyIncr', { n: i });
          const cls = `fkl-copy ${i === 0 ? 'fkl-copy-full' : 'fkl-copy-incr'}`;
          let to;
          if (b != null) to = `${fmtWhen(b)} (${fmtDur(c.seconds != null ? c.seconds : (b - a) / 1000)})`;
          else if (running) { b = now; to = tr('fkl.inProgress'); }
          else { b = a; to = tr('fkl.noEnd'); }
          marks.push(band(a, b, `${cls}${b === now ? ' fkl-copy-run' : ''}`,
            tr('fkl.copyTip', { kind, vm, from: fmtWhen(a), to }), ` data-fkl="copy" data-fkl-copy="${i === 0 ? 'full' : 'incr'}"`));
        });
      });
      const next = ts(w.next_precopy);
      if (next != null) marks.push(point(next, 'fkl-next', tr('fkl.nextTip', { when: fmtWhen(next), left: left(next) }), ' data-fkl="next"'));
      const cut = ts(w.cutover);
      let cutLabel = '';
      if (cut != null) {
        const scheduled = w.state === 'cutover-scheduled' && cut > now;
        marks.push(point(cut, `fkl-cut${scheduled ? ' fkl-cut-planned' : ''}`,
          scheduled ? tr('fkl.cutoverTip', { when: fmtWhen(cut), left: left(cut) }) : tr('fkl.cutoverPastTip', { when: fmtWhen(cut) }),
          ' data-fkl="cutover"'));
        if (scheduled) {
          cutLabel = `<div class="form-hint fkl-countdown" data-fkl="countdown">${icon('timer', 11)} ${esc(tr('fkl.legend.cutover'))}
            <span data-fk-at="${esc(w.cutover)}">${esc(left(cut))}</span></div>`;
        }
      }
      const name = opts.showCluster ? `${cluster} / ${w.name}` : w.name;
      return `<div class="fkl-row fkl-lane fkl-st-${esc(STATE_CLS[w.state] ?? 'warn') || 'none'} tip" role="button" tabindex="0"
          data-fkl-lane data-cluster="${esc(cluster)}" data-wave="${esc(w.name)}" data-state="${esc(w.state)}"
          data-tip="${esc(tr('fkl.t.lane', { wave: w.name }))}">
        <div class="fkl-label"><div class="fkl-name"><b>${esc(name)}</b></div>
          <div>${opts.stateBadge ? opts.stateBadge(w) : esc(w.state)} <span class="form-hint">${esc(tr('fk.w.vms', { n: (w.vms || []).length }))}</span></div>
          ${cutLabel}</div>
        <div class="fkl-track">${maintBand}${marks.join('')}${nowLine}</div>
      </div>`;
    }).join('');
    return `${axis}${lanes}`;
  }

  // -- cycle de vie -----------------------------------------------------------
  const state = new WeakMap();   // conteneur -> { rows, opts }

  /** Dessine (ou redessine) les couloirs dans `container`. `rows` :
   *  [{ cluster, wave }] ; `opts` : { scope, showCluster, stateBadge,
   *  onOpen(cluster, wave) }. La barre d'outils n'est posée qu'une fois. */
  function paint(container, rows, opts) {
    if (!container) return;
    state.set(container, { rows, opts });
    let root = container.querySelector(':scope > .fkl');
    if (!root || root.dataset.fklScope !== opts.scope) {
      container.innerHTML = `<div class="fkl" data-fkl-scope="${esc(opts.scope)}">${toolsHtml(opts.scope)}
          <div class="fkl-body" data-fkl="body"></div></div>`;
      root = container.querySelector(':scope > .fkl');
    }
    if (!container.dataset.fklBound) {
      container.dataset.fklBound = '1';
      container.addEventListener('click', onClick);
      container.addEventListener('keydown', onKey);
      container.addEventListener('change', onChange);
    }
    repaint(container);
  }

  function repaint(container) {
    const s = state.get(container);
    const root = container.querySelector(':scope > .fkl');
    if (!s || !root) return;
    const zoom = getZoom(s.opts.scope);
    root.querySelectorAll('[data-fkl-zoom]').forEach((b) => {
      const on = b.dataset.fklZoom === zoom;
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-pressed', String(on));
    });
    root.querySelector('[data-fkl="body"]').innerHTML = s.rows.length
      ? bodyHtml(s.rows, s.opts) : `<p class="form-hint">${esc(tr('fkl.none'))}</p>`;
  }

  function onClick(e) {
    const container = e.currentTarget;
    const s = state.get(container);
    if (!s) return;
    const z = e.target.closest('[data-fkl-zoom]');
    if (z) { store.set(zoomKey(s.opts.scope), z.dataset.fklZoom); return repaint(container); }
    if (e.target.closest('[data-fkl="maint-clear"]')) {
      setMaint(s.opts.scope, { start: '', end: '' });
      container.querySelectorAll('[data-fkl-maint]').forEach((i) => { i.value = ''; });
      return repaint(container);
    }
    const lane = e.target.closest('[data-fkl-lane]');
    if (lane && s.opts.onOpen) s.opts.onOpen(lane.dataset.cluster, lane.dataset.wave);
  }

  function onKey(e) {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const lane = e.target.closest && e.target.closest('[data-fkl-lane]');
    if (!lane || e.target !== lane) return;
    e.preventDefault();
    lane.click();
  }

  function onChange(e) {
    const t = e.target;
    if (!t.dataset || !t.dataset.fklMaint) return;
    const container = e.currentTarget;
    const s = state.get(container);
    if (!s) return;
    const m = getMaint(s.opts.scope);
    m[t.dataset.fklMaint] = t.value;
    setMaint(s.opts.scope, m);
    repaint(container);
  }

  return { paint, domain, ticks };
})();
window.ForkliftLanes = ForkliftLanes;
