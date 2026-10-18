/**
 * harvester-ops : section « Containers » (v1.91.0)
 *
 * Les charges de conteneurs (Deployments, StatefulSets, DaemonSets, Jobs,
 * CronJobs, pods seuls) déployées sur les clusters Harvester, à côté des VM.
 * Deux vues sur le modèle des VM :
 *   - Containers.current : le cluster choisi en haut (/api/containers/<c>) ;
 *   - Containers.all : tous les clusters déclarés (/api/containers-all),
 *     avec les puces de clusters de « All clusters VMs ».
 *
 * Lecture seule pour l'instant : filtres (texte, cluster, namespace, type,
 * état), tri sur chaque colonne, une ligne dépliée montre ses pods, et le menu
 * de la ligne ouvre le YAML (fenêtre YAML, en lecture) et les journaux d'un
 * pod (500 lignes au plus, conteneur au choix). Les namespaces système, les
 * pods des VM et ceux de CDI sont écartés côté serveur (hv_containers.py).
 */
const Containers = (() => {
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const enc = encodeURIComponent;
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 14) => (window.Icons ? Icons.svg(n, { size }) : '');
  const REFRESH_MS = 15000;
  const LOG_TAILS = [100, 200, 500];

  const KINDS = ['Deployment', 'StatefulSet', 'DaemonSet', 'Job', 'CronJob', 'ReplicaSet', 'Pod'];
  const STATES = ['Running', 'Progressing', 'Degraded', 'Failed', 'Completed', 'Suspended'];
  // Clés en toutes lettres : le contrôle de parité i18n ne lit que des littéraux.
  const STATE_LABEL = {
    Running: () => tr('cw.state.Running'), Progressing: () => tr('cw.state.Progressing'),
    Degraded: () => tr('cw.state.Degraded'), Failed: () => tr('cw.state.Failed'),
    Completed: () => tr('cw.state.Completed'), Suspended: () => tr('cw.state.Suspended'),
  };
  const STATE_BADGE = { Running: 'ok', Progressing: 'info', Degraded: 'warn', Failed: 'fail', Completed: 'dim', Suspended: 'dim' };
  const stateLabel = (s) => (STATE_LABEL[s] ? STATE_LABEL[s]() : s);

  const CL_STATE = {
    unreachable: ['warn', () => tr('avm.cl.unreachable'), () => tr('cw.cl.unreachableTip')],
    denied: ['fail', () => tr('avm.cl.denied'), () => tr('cw.cl.deniedTip')],
    error: ['fail', () => tr('avm.cl.error'), () => tr('cw.cl.errorTip')],
    unknown: ['warn', () => tr('avm.cl.unknown'), () => tr('avm.cl.unknownTip')],
  };

  /** « 5d », « 3h », « 12m », « 40s » depuis un horodatage RFC 3339. */
  function age(ts) {
    const t = Date.parse(ts || '');
    if (Number.isNaN(t)) return '?';
    const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (s >= 86400) return `${Math.floor(s / 86400)}d`;
    if (s >= 3600) return `${Math.floor(s / 3600)}h`;
    if (s >= 60) return `${Math.floor(s / 60)}m`;
    return `${s}s`;
  }
  const createdKey = (w) => -(Date.parse(w.created || '') || 0);     // le plus jeune d'abord en croissant
  const readyKey = (w) => (w.desired ? w.ready / w.desired : (w.desired === 0 ? 1 : w.ready));
  const SORT = {
    cluster: (w) => w.cluster, namespace: (w) => w.namespace, name: (w) => w.name,
    kind: (w) => KINDS.indexOf(w.kind), ready: readyKey, state: (w) => STATES.indexOf(w.state),
    gpus: (w) => w.gpus || 0, age: createdKey, images: (w) => (w.images || []).join(' '),
  };

  // -- le menu d'une ligne (même allure que celui des VMs) ----------------------
  let menu = null;
  function closeMenu() {
    if (!menu) return;
    menu.el.remove();
    document.removeEventListener('pointerdown', menu.outside, true);
    document.removeEventListener('keydown', menu.keys, true);
    if (menu.anchor) { menu.anchor.setAttribute('aria-expanded', 'false'); menu.anchor.focus(); }
    menu = null;
  }

  function openMenu(anchor, w) {
    if (menu && menu.anchor === anchor) { closeMenu(); return; }
    closeMenu();
    const el = document.createElement('div');
    el.className = 'vma-menu ct-menu';
    el.setAttribute('role', 'menu');
    el.setAttribute('aria-label', tr('cw.menuLabel', { name: `${w.namespace}/${w.name}` }));
    const item = (act, ic, label, tip, disabled) => `<button type="button" class="vma-item tip" role="menuitem"
        data-ct-menu="${act}" data-tip="${esc(tip)}"${disabled ? ' aria-disabled="true"' : ''}>
        <span class="vma-ic">${icon(ic)}</span> ${esc(label)}</button>`;
    const noPod = !(w.pods || []).length;
    el.innerHTML = `<div class="vma-head">${icon('container', 13)} <strong>${esc(w.name)}</strong>
        <span class="res-dim">${esc(w.kind)} · ${esc(w.namespace)}</span></div>
      <div class="vma-group">
        ${item('yaml', 'code', tr('cw.menu.yaml'), tr('cw.menu.yamlTip'))}
        ${item('download', 'download', tr('cw.menu.download'), tr('cw.menu.downloadTip'))}
        ${item('logs', 'logs', tr('cw.menu.logs'), noPod ? tr('cw.menu.logsNone') : tr('cw.menu.logsTip'), noPod)}
      </div>`;
    document.body.appendChild(el);
    const r = anchor.getBoundingClientRect();
    const wd = el.offsetWidth || 260, h = el.offsetHeight || 160;
    let left = r.right - wd, top = r.bottom + 4;
    if (left < 8) left = 8;
    if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 4);
    el.style.left = `${left + window.scrollX}px`;
    el.style.top = `${top + window.scrollY}px`;
    anchor.setAttribute('aria-expanded', 'true');
    menu = {
      el, anchor,
      outside: (e) => { if (!el.contains(e.target) && e.target !== anchor && !anchor.contains(e.target)) closeMenu(); },
      keys: (e) => {
        if (e.key === 'Escape') { e.preventDefault(); closeMenu(); return; }
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          const items = [...el.querySelectorAll('.vma-item')];
          const i = items.indexOf(document.activeElement);
          const n = e.key === 'ArrowDown' ? (i + 1) % items.length : (i - 1 + items.length) % items.length;
          e.preventDefault();
          items[n]?.focus();
        }
      },
    };
    document.addEventListener('pointerdown', menu.outside, true);
    document.addEventListener('keydown', menu.keys, true);
    el.querySelector('.vma-item:not([aria-disabled])')?.focus();
    el.addEventListener('click', (e) => {
      const b = e.target.closest('[data-ct-menu]');
      if (!b || b.getAttribute('aria-disabled')) return;
      const act = b.dataset.ctMenu;
      closeMenu();
      const kind = w.kind.toLowerCase();
      if (act === 'yaml' && window.YamlWindow) YamlWindow.open(w.cluster, kind, w.namespace, w.name);
      else if (act === 'download' && window.YamlWindow) YamlWindow.download(w.cluster, kind, w.namespace, w.name);
      else if (act === 'logs') logsWindow(w);
    });
  }

  // -- journaux d'un pod --------------------------------------------------------
  function logsWindow(w, podName) {
    const pods = w.pods || [];
    if (!pods.length || !window.FloatingPanels) return null;
    const first = pods.find((p) => p.name === podName) || pods[0];
    const panel = FloatingPanels.open({
      id: `ct-logs-${w.cluster}-${w.namespace}-${w.kind}-${w.name}`, icon: 'logs', width: 860, height: 560,
      title: `${tr('cw.logs.title')} · ${w.namespace}/${w.name} · ${w.cluster}`,
      bodyHtml: `<div class="vma-logs ct-logs">
        <div class="yw-tools">
          <label class="bk-field vma-inline"><span>${esc(tr('cw.logs.pod'))}</span>
            <select name="pod" class="tip" data-tip="${esc(tr('cw.logs.podTip'))}">
              ${pods.map((p) => `<option value="${esc(p.name)}"${p.name === first.name ? ' selected' : ''}>${esc(p.name)}</option>`).join('')}
            </select></label>
          <label class="bk-field vma-inline"><span>${esc(tr('vma.f.container'))}</span>
            <select name="container" class="tip" data-tip="${esc(tr('cw.logs.containerTip'))}"></select></label>
          <label class="bk-field vma-inline"><span>${esc(tr('vma.f.lines'))}</span>
            <select name="tail" class="tip" data-tip="${esc(tr('cw.logs.linesTip'))}">
              ${LOG_TAILS.map((n) => `<option${n === 500 ? ' selected' : ''}>${n}</option>`).join('')}
            </select></label>
          <button type="button" class="btn btn-sm btn-secondary tip" data-ct-logs="refresh" data-tip="${esc(tr('cw.logs.refreshTip'))}">${icon('refresh')} ${esc(tr('overview.refresh'))}</button>
          <span class="res-dim ct-logs-node"></span>
        </div>
        <pre class="vma-logs-text live-log" tabindex="0">${esc(tr('common.loading'))}</pre>
      </div>`,
    });
    const root = panel.el;
    const podSel = root.querySelector('[name="pod"]');
    const ctSel = root.querySelector('[name="container"]');
    const tail = root.querySelector('[name="tail"]');
    const pre = root.querySelector('.vma-logs-text');
    const fillContainers = () => {
      const p = pods.find((x) => x.name === podSel.value) || first;
      const names = [...(p.containers || []), ...(p.init_containers || [])];
      ctSel.innerHTML = names.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
      root.querySelector('.ct-logs-node').textContent = p.node ? `${tr('col.node')}: ${p.node}` : '';
    };
    const load = async () => {
      const q = new URLSearchParams({ tail: tail.value });
      if (ctSel.value) q.set('container', ctSel.value);
      pre.textContent = tr('common.loading');
      try {
        const r = await fetch(`/api/containers/${enc(w.cluster)}/${enc(w.namespace)}/pod/${enc(podSel.value)}/logs?${q}`);
        let d = {};
        try { d = await r.json(); } catch { /* sans corps */ }
        if (!r.ok) throw new Error(d.hint || d.detail || d.error || `HTTP ${r.status}`);
        pre.textContent = d.logs || tr('cw.logs.empty');
        pre.scrollTop = pre.scrollHeight;
      } catch (err) {
        pre.textContent = err.message;
      }
    };
    if (!root.dataset.ctReady) {
      root.dataset.ctReady = '1';
      podSel.addEventListener('change', () => { fillContainers(); load(); });
      ctSel.addEventListener('change', load);
      tail.addEventListener('change', load);
      root.querySelector('[data-ct-logs="refresh"]').addEventListener('click', load);
    }
    podSel.value = first.name;
    fillContainers();
    load();
    return panel;
  }

  // -- une vue (cluster choisi, ou tous les clusters) ---------------------------
  function makeView(mode) {
    const ALL = mode === 'all';
    const STORE = ALL ? 'harvester_ops_allcontainers' : 'harvester_ops_containers';
    let host = null;
    let timer = null;
    let data = null;
    let loading = false;
    let builtLang = '';
    let shownCluster = null;
    const expanded = new Set();
    const prefs = { clusters: [], state: '', kind: '', ns: '', text: '', sort: 'name', dir: 'asc' };

    function loadPrefs() {
      try { Object.assign(prefs, JSON.parse(localStorage.getItem(STORE) || '{}')); } catch { /* réglages illisibles */ }
      if (!Array.isArray(prefs.clusters)) prefs.clusters = [];
    }
    function savePrefs() {
      try { localStorage.setItem(STORE, JSON.stringify(prefs)); } catch { /* stockage indisponible */ }
    }
    const keyOf = (w) => `${w.cluster}|${w.namespace}|${w.kind}|${w.name}`;

    function visible() {
      if (!data) return [];
      const words = prefs.text.toLowerCase().split(/\s+/).filter(Boolean);
      const rows = data.workloads.filter((w) => {
        if (ALL && prefs.clusters.length && !prefs.clusters.includes(w.cluster)) return false;
        if (prefs.state && w.state !== prefs.state) return false;
        if (prefs.kind && w.kind !== prefs.kind) return false;
        if (prefs.ns && w.namespace !== prefs.ns) return false;
        if (!words.length) return true;
        const hay = [w.name, w.namespace, w.cluster, w.kind, ...(w.images || []),
          ...(w.pods || []).flatMap((p) => [p.name, p.node || '']),
          ...Object.entries(w.labels || {}).map(([k, v]) => `${k}=${v}`)].join(' ').toLowerCase();
        return words.every((x) => hay.includes(x));
      });
      const get = SORT[prefs.sort] || SORT.name;
      const dir = prefs.dir === 'desc' ? -1 : 1;
      return rows.sort((a, b) => {
        const x = get(a); const y = get(b);
        const c = typeof x === 'number' && typeof y === 'number' ? x - y
          : String(x).localeCompare(String(y), undefined, { numeric: true, sensitivity: 'base' });
        return (c || a.name.localeCompare(b.name) || a.cluster.localeCompare(b.cluster)) * dir;
      });
    }

    const columns = () => (ALL ? 11 : 10);

    function skeleton() {
      const th = (k, label, tip, cls = '') => `<th class="sortable tip ${cls}" data-ct-sort="${k}" data-tip="${esc(tip)}"
          title="${esc(tip)}"><span>${esc(label)}</span> <span class="sort-arrow"></span></th>`;
      host.innerHTML = `
        <div class="ct-banner" role="note">${icon('info')} <span>${esc(tr('cw.banner'))}</span></div>
        ${ALL ? `<div class="avm-clusters" data-ct="clusters" role="group" aria-label="${esc(tr('cw.filter.clusterTip'))}"></div>` : ''}
        <div class="avm-toolbar">
          <label class="avm-field tip" data-tip="${esc(tr('cw.filter.stateTip'))}">
            <span>${esc(tr('cw.filter.state'))}</span>
            <select data-ct="state">
              <option value="">${esc(tr('avm.phase.all'))}</option>
              ${STATES.map((s) => `<option value="${s}">${esc(stateLabel(s))}</option>`).join('')}
            </select>
          </label>
          <label class="avm-field tip" data-tip="${esc(tr('cw.filter.kindTip'))}">
            <span>${esc(tr('cw.col.kind'))}</span>
            <select data-ct="kind">
              <option value="">${esc(tr('cw.filter.kindAll'))}</option>
              ${KINDS.map((k) => `<option value="${k}">${esc(k)}</option>`).join('')}
            </select>
          </label>
          <label class="avm-field tip" data-tip="${esc(tr('cw.filter.nsTip'))}">
            <span>${esc(tr('col.namespace'))}</span>
            <select data-ct="ns"><option value="">${esc(tr('avm.filter.nsAll'))}</option></select>
          </label>
          <input type="search" class="avm-search tip" data-ct="text" autocomplete="off"
                 placeholder="${esc(tr('cw.filter.textPh'))}" data-tip="${esc(tr('cw.filter.textTip'))}">
          <button type="button" class="btn btn-sm tip" data-ct="refresh" data-tip="${esc(tr('cw.refreshTip'))}"
                  aria-label="${esc(tr('cw.refreshTip'))}">${icon('refresh')}</button>
          <span class="avm-count" data-ct="count"></span>
        </div>
        <p class="hint warn ct-notice" data-ct="notice" hidden></p>
        <table class="data-table ns-vm-table avm-table ct-table">
          <thead><tr>
            <th class="check-col" aria-label="${esc(tr('cw.col.expand'))}"></th>
            ${ALL ? th('cluster', tr('col.cluster'), tr('avm.tip.sortCluster')) : ''}
            ${th('namespace', tr('col.namespace'), tr('avm.tip.sortNs'))}
            ${th('name', tr('cw.col.name'), tr('cw.tip.sortName'))}
            ${th('kind', tr('cw.col.kind'), tr('cw.tip.sortKind'))}
            ${th('ready', tr('cw.col.ready'), tr('cw.tip.sortReady'), 'num')}
            ${th('state', tr('cw.filter.state'), tr('cw.tip.sortState'))}
            ${th('images', tr('cw.col.images'), tr('cw.tip.sortImages'))}
            ${th('gpus', tr('cw.col.gpus'), tr('cw.tip.sortGpus'), 'num')}
            ${th('age', tr('cw.col.age'), tr('cw.tip.sortAge'), 'num')}
            <th>${esc(tr('col.actions'))}</th>
          </tr></thead>
          <tbody></tbody>
        </table>
        <p class="hint avm-empty" data-ct="empty" hidden></p>`;
      const q = (s) => host.querySelector(s);
      q('[data-ct="state"]').value = prefs.state;
      q('[data-ct="kind"]').value = prefs.kind;
      q('[data-ct="text"]').value = prefs.text;
      q('[data-ct="state"]').addEventListener('change', (e) => { prefs.state = e.target.value; savePrefs(); render(); });
      q('[data-ct="kind"]').addEventListener('change', (e) => { prefs.kind = e.target.value; savePrefs(); render(); });
      q('[data-ct="ns"]').addEventListener('change', (e) => { prefs.ns = e.target.value; savePrefs(); render(); });
      q('[data-ct="text"]').addEventListener('input', (e) => { prefs.text = e.target.value; savePrefs(); render(); });
      q('[data-ct="refresh"]').addEventListener('click', () => load());
      host.querySelectorAll('[data-ct-sort]').forEach((h) => h.addEventListener('click', () => {
        const k = h.dataset.ctSort;
        if (prefs.sort === k) prefs.dir = prefs.dir === 'asc' ? 'desc' : 'asc';
        else { prefs.sort = k; prefs.dir = 'asc'; }
        savePrefs(); render();
      }));
      if (ALL) {
        q('[data-ct="clusters"]').addEventListener('click', (e) => {
          const chip = e.target.closest('[data-ct-cluster]');
          if (!chip) return;
          const c = chip.dataset.ctCluster;
          if (c === '*') prefs.clusters = [];
          else if (prefs.clusters.includes(c)) prefs.clusters = prefs.clusters.filter((x) => x !== c);
          else prefs.clusters = [...prefs.clusters, c];
          savePrefs(); render();
        });
      }
      host.querySelector('tbody').addEventListener('click', onRowAction);
    }

    function renderClusters() {
      if (!ALL) return;
      const box = host.querySelector('[data-ct="clusters"]');
      const all = !prefs.clusters.length;
      const chips = (data ? data.clusters : []).map((c) => {
        const on = prefs.clusters.includes(c.cluster);
        const st = CL_STATE[c.state];
        const tip = st ? `${st[2]()}${c.error ? ` (${c.error})` : ''}` : tr('cw.cl.tip', { n: c.count || 0 });
        return `<button type="button" class="avm-chip tip${on ? ' active' : ''}${st ? ` avm-chip-${st[0]}` : ''}"
            data-ct-cluster="${esc(c.cluster)}" aria-pressed="${on}" data-tip="${esc(tip)}">
            ${icon('cloud', 13)} <span>${esc(c.cluster)}</span>
            ${st ? `<span class="badge ${st[0]}">${esc(st[1]())}</span>` : `<span class="avm-chip-n">${c.count || 0}</span>`}
          </button>`;
      }).join('');
      box.innerHTML = `<button type="button" class="avm-chip tip${all ? ' active' : ''}" data-ct-cluster="*"
          aria-pressed="${all}" data-tip="${esc(tr('cw.filter.allClustersTip'))}">${esc(tr('avm.filter.allClusters'))}</button>${chips}`;
    }

    function renderNotice() {
      // un cluster refusé, éteint, ou lu en partie : dit, jamais une liste vide muette
      const box = host.querySelector('[data-ct="notice"]');
      const lines = [];
      for (const c of (data ? data.clusters : [])) {
        const st = CL_STATE[c.state];
        if (!ALL && st) lines.push(`${c.cluster}: ${st[2]()}${c.error ? ` (${c.error})` : ''}`);
        if (c.denials && c.denials.length) lines.push(tr('cw.partial', { c: c.cluster, msg: c.denials[0] }));
      }
      box.hidden = !lines.length;
      box.innerHTML = lines.map((l) => `${icon('warn', 13)} ${esc(l)}`).join('<br>');
    }

    function renderNamespaces() {
      const sel = host.querySelector('[data-ct="ns"]');
      const names = [...new Set((data ? data.workloads : [])
        .filter((w) => !ALL || !prefs.clusters.length || prefs.clusters.includes(w.cluster)).map((w) => w.namespace))].sort();
      if (prefs.ns && !names.includes(prefs.ns)) names.unshift(prefs.ns);
      sel.innerHTML = `<option value="">${esc(tr('avm.filter.nsAll'))}</option>`
        + names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
      sel.value = prefs.ns;
    }

    function podsRow(w) {
      const pods = w.pods || [];
      const body = pods.length ? `<table class="data-table ct-pods">
          <thead><tr><th>${esc(tr('cw.pod.name'))}</th><th>${esc(tr('col.node'))}</th><th>${esc(tr('cw.filter.state'))}</th>
            <th class="num">${esc(tr('cw.pod.restarts'))}</th><th class="num">${esc(tr('cw.col.age'))}</th><th>${esc(tr('col.actions'))}</th></tr></thead>
          <tbody>${pods.map((p) => `<tr data-ct-pod="${esc(p.name)}">
            <td><code>${esc(p.name)}</code></td>
            <td>${p.node ? esc(p.node) : '<span class="res-dim">–</span>'}</td>
            <td><span class="badge ${p.ready ? 'ok' : (p.phase === 'Succeeded' ? 'dim' : 'warn')}">${esc(p.state)}</span></td>
            <td class="num">${esc(p.restarts || 0)}</td>
            <td class="num">${esc(age(p.created))}</td>
            <td><button type="button" class="btn-icon-action tip" data-ct-act="podlogs" data-tip="${esc(tr('cw.pod.logsTip'))}"
                aria-label="${esc(tr('cw.pod.logsTip'))}">${icon('logs')}</button></td></tr>`).join('')}</tbody>
        </table>` : `<p class="hint">${esc(tr('cw.pod.none'))}</p>`;
      return `<tr class="ct-pods-row" data-ct-pods-of="${esc(keyOf(w))}"><td colspan="${columns()}">${body}</td></tr>`;
    }

    function row(w) {
      const k = keyOf(w);
      const open = expanded.has(k);
      const imgs = w.images || [];
      const ready = w.desired == null ? `${w.ready}` : `${w.ready}/${w.desired}`;
      const b = (attr, ic, tip) => `<button type="button" class="btn-icon-action tip" data-ct-act="${attr}"
          data-tip="${esc(tip)}" aria-label="${esc(tip)}">${ic}</button>`;
      return `<tr data-ct-row="${esc(k)}" class="${open ? 'ct-open' : ''}">
        <td class="check-col"><button type="button" class="btn-icon-action ct-expand tip" data-ct-act="expand"
            aria-expanded="${open}" data-tip="${esc(open ? tr('cw.tip.collapse') : tr('cw.tip.expand'))}"
            aria-label="${esc(open ? tr('cw.tip.collapse') : tr('cw.tip.expand'))}">${icon(open ? 'chevronDown' : 'chevronRight', 13)}</button></td>
        ${ALL ? `<td><span class="avm-cluster">${esc(w.cluster)}</span></td>` : ''}
        <td>${esc(w.namespace)}</td>
        <td>${esc(w.name)}</td>
        <td>${esc(w.kind)}</td>
        <td class="num">${esc(ready)}</td>
        <td><span class="badge ${STATE_BADGE[w.state] || 'dim'}">${esc(stateLabel(w.state))}</span></td>
        <td class="ct-images">${imgs.length ? `<code>${esc(imgs[0])}</code>${imgs.length > 1 ? ` <span class="res-dim tip" data-tip="${esc(imgs.slice(1).join(', '))}">+${imgs.length - 1}</span>` : ''}` : '<span class="res-dim">–</span>'}</td>
        <td class="num">${w.gpus ? `<span class="tip" data-tip="${esc(tr('cw.tip.gpus'))}">${esc(w.gpus)}</span>` : '<span class="res-dim">–</span>'}</td>
        <td class="num">${esc(age(w.created))}</td>
        <td class="vm-actions-cell">
          ${b('logs', icon('logs'), (w.pods || []).length ? tr('cw.menu.logsTip') : tr('cw.menu.logsNone'))}
          ${b('more', icon('more'), tr('cw.moreTip'))}
        </td></tr>${open ? podsRow(w) : ''}`;
    }

    function render() {
      if (!host) return;
      renderClusters();
      renderNotice();
      renderNamespaces();
      const rows = visible();
      host.querySelector('tbody').innerHTML = rows.map(row).join('');
      host.querySelectorAll('[data-ct-sort]').forEach((h) => {
        const on = h.dataset.ctSort === prefs.sort;
        h.classList.toggle('active-sort', on);
        h.querySelector('.sort-arrow').innerHTML = on ? icon(prefs.dir === 'asc' ? 'arrowUp' : 'arrowDown', 12) : '';
        h.setAttribute('aria-sort', on ? (prefs.dir === 'asc' ? 'ascending' : 'descending') : 'none');
      });
      const total = data ? data.workloads.length : 0;
      host.querySelector('[data-ct="count"]').textContent = data ? tr('cw.count', { n: rows.length, total }) : tr('common.loading');
      const empty = host.querySelector('[data-ct="empty"]');
      // cluster choisi non lu : l'avis le dit, « aucune charge » serait faux
      const unread = !ALL && data && data.clusters.length && data.clusters[0].state !== 'ok';
      empty.hidden = !data || rows.length > 0 || unread;
      empty.textContent = total ? tr('cw.emptyFiltered') : tr('cw.empty');
    }

    function findWorkload(el) {
      const tr_ = el.closest('[data-ct-row]') || el.closest('[data-ct-pods-of]')?.previousElementSibling;
      const k = tr_ && tr_.dataset.ctRow;
      return k && data ? data.workloads.find((w) => keyOf(w) === k) : null;
    }

    function onRowAction(e) {
      const btn = e.target.closest('[data-ct-act]');
      if (!btn) return;
      const w = findWorkload(btn);
      if (!w) return;
      const act = btn.dataset.ctAct;
      if (act === 'expand') {
        const k = keyOf(w);
        if (expanded.has(k)) expanded.delete(k); else expanded.add(k);
        render();
      } else if (act === 'more') openMenu(btn, w);
      else if (act === 'logs') logsWindow(w);
      else if (act === 'podlogs') logsWindow(w, btn.closest('[data-ct-pod]')?.dataset.ctPod);
    }

    async function fetchCluster(cluster) {
      const r = await fetch(`/api/containers/${enc(cluster)}`);
      let d = {};
      try { d = await r.json(); } catch { /* sans corps */ }
      if (r.ok && Array.isArray(d.workloads)) {
        const info = { cluster, state: 'ok', count: d.workloads.length };
        if (d.denials && d.denials.length) info.denials = d.denials;
        return { clusters: [info], workloads: d.workloads.map((w) => ({ ...w, cluster })) };
      }
      const state = d.unreachable ? 'unreachable' : (r.status === 403 ? 'denied' : (r.status === 404 ? 'unknown' : 'error'));
      return { clusters: [{ cluster, state, error: d.unreachable ? d.endpoint : (d.detail || d.error || `HTTP ${r.status}`) }], workloads: [] };
    }

    async function load() {
      if (!host || loading) return;
      const cluster = ALL ? null : (window.App && App.getCurrentCluster());
      if (!ALL && !cluster) { render(); return; }
      if (!ALL && cluster !== shownCluster) { data = null; expanded.clear(); render(); }
      loading = true;
      try {
        if (ALL) {
          const r = await fetch('/api/containers-all');
          if (r.ok) data = await r.json();
        } else {
          const d = await fetchCluster(cluster);
          if (cluster === (window.App && App.getCurrentCluster())) { data = d; shownCluster = cluster; }
        }
      } catch { /* console injoignable : on garde la dernière liste */ }
      loading = false;
      render();
    }

    function start(el) {
      if (!el) return;
      const lang = window.i18n ? i18n.currentLang : '';
      if (host !== el || builtLang !== lang) {
        host = el;
        builtLang = lang;
        loadPrefs();
        skeleton();
      }
      render();
      load();
      clearInterval(timer);
      timer = setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
    }

    function stop() {
      clearInterval(timer);
      timer = null;
    }

    return { start, stop, load, _visible: visible };
  }

  return { current: makeView('cluster'), all: makeView('all'), logsWindow, _age: age };
})();

window.Containers = Containers;
