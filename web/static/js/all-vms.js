/**
 * harvester-ops : vue « Toutes les VMs » (v1.85.0)
 *
 * Les VMs de tous les clusters déclarés dans un seul tableau, lu par
 * /api/vms-all (chaque cluster en parallèle, avec l'identité de la personne).
 * Filtres : clusters (puces cliquables, plusieurs à la fois), état,
 * namespace, texte libre ; tri sur chaque colonne ; filtres et tri retenus
 * par le navigateur. Un cluster éteint, injoignable ou qui refuse la lecture
 * est dit sur sa puce au lieu de disparaître.
 *
 * Les gestes sont ceux de l'onglet Virtual machines, avec le cluster de la
 * ligne : démarrer/arrêter, console, snapshots, migration, réglages, notes,
 * et le menu d'actions complet (VMActions). La sélection peut mêler des VMs
 * de plusieurs clusters : une action groupée part cluster par cluster, et
 * chaque geste reste une action suivie dans le dock.
 */
const AllVMs = (() => {
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const enc = encodeURIComponent;
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 14) => (window.Icons ? Icons.svg(n, { size }) : '');
  const REFRESH_MS = 15000;
  const STORE = 'harvester_ops_allvms';

  let host = null;
  let timer = null;
  let data = null;
  let loading = false;
  let builtLang = '';                   // langue du squelette (il porte des libellés)
  const selection = new Set();          // `${cluster}|${namespace}|${name}`
  const prefs = { clusters: [], phase: '', ns: '', text: '', sort: 'name', dir: 'asc' };

  function loadPrefs() {
    try { Object.assign(prefs, JSON.parse(localStorage.getItem(STORE) || '{}')); } catch { /* réglages illisibles */ }
    if (!Array.isArray(prefs.clusters)) prefs.clusters = [];
  }
  function savePrefs() {
    try { localStorage.setItem(STORE, JSON.stringify(prefs)); } catch { /* stockage indisponible */ }
  }

  const keyOf = (v) => `${v.cluster}|${v.namespace}|${v.name}`;

  // Familles d'état du filtre : la phase de la VMI, ramenée à ce qu'un
  // exploitant cherche (« qu'est-ce qui tourne, qu'est-ce qui est arrêté »).
  const BUCKET = { Running: 'running', Stopped: 'stopped', Paused: 'paused', Failed: 'failed',
                   Pending: 'starting', Scheduling: 'starting', Scheduled: 'starting', Starting: 'starting' };
  const bucket = (v) => BUCKET[v.phase || 'Stopped'] || 'other';
  const PHASES = [
    ['running', () => tr('avm.phase.running')], ['stopped', () => tr('avm.phase.stopped')],
    ['starting', () => tr('avm.phase.starting')], ['paused', () => tr('avm.phase.paused')],
    ['failed', () => tr('avm.phase.failed')], ['other', () => tr('avm.phase.other')],
  ];
  const STATE_LABEL = {
    Running: () => tr('vm.state.Running'), Stopped: () => tr('vm.state.Stopped'), Paused: () => tr('vm.state.Paused'),
    Pending: () => tr('vm.state.Pending'), Starting: () => tr('vm.state.Starting'), Failed: () => tr('vm.state.Failed'),
    Unknown: () => tr('vm.state.Unknown'),
  };
  const phaseLabel = (p) => (STATE_LABEL[p] ? STATE_LABEL[p]() : p);

  // Mémoire « 4Gi », « 2048Mi », « 8G » : en octets pour le tri.
  const UNITS = { Ki: 2 ** 10, Mi: 2 ** 20, Gi: 2 ** 30, Ti: 2 ** 40, K: 1e3, M: 1e6, G: 1e9, T: 1e12 };
  function bytes(m) {
    const r = /^(\d+(?:\.\d+)?)([KMGT]i?)?$/.exec(String(m || ''));
    return r ? parseFloat(r[1]) * (UNITS[r[2]] || 1) : -1;
  }
  const ipKey = (v) => ((v.ips || [])[0] || '').split('.').map((x) => x.padStart(3, '0')).join('.');
  const SORT = {
    cluster: (v) => v.cluster, namespace: (v) => v.namespace, name: (v) => v.name,
    phase: (v) => v.phase || 'Stopped', cpu: (v) => v.cpu || 0, memory: (v) => bytes(v.memory),
    ip: ipKey, node: (v) => v.node || '',
  };

  function visible() {
    if (!data) return [];
    const words = prefs.text.toLowerCase().split(/\s+/).filter(Boolean);
    const rows = data.vms.filter((v) => {
      if (prefs.clusters.length && !prefs.clusters.includes(v.cluster)) return false;
      if (prefs.phase && bucket(v) !== prefs.phase) return false;
      if (prefs.ns && v.namespace !== prefs.ns) return false;
      if (!words.length) return true;
      const hay = [v.name, v.namespace, v.cluster, v.node || '', ...(v.ips || []),
        ...Object.entries(v.labels || {}).map(([k, val]) => `${k}=${val}`)].join(' ').toLowerCase();
      return words.every((w) => hay.includes(w));
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

  // -- squelette (monté une fois : la saisie du filtre n'est jamais perdue) --
  function skeleton() {
    const th = (k, label, tip, cls = '') => `<th class="sortable tip ${cls}" data-avm-sort="${k}" data-tip="${esc(tip)}"
        title="${esc(tip)}"><span>${esc(label)}</span> <span class="sort-arrow"></span></th>`;
    host.innerHTML = `
      <div class="avm-clusters" data-avm="clusters" role="group" aria-label="${esc(tr('avm.filter.clusterTip'))}"></div>
      <div class="avm-toolbar">
        <label class="avm-field tip" data-tip="${esc(tr('avm.filter.phaseTip'))}">
          <span>${esc(tr('avm.filter.phase'))}</span>
          <select data-avm="phase">
            <option value="">${esc(tr('avm.phase.all'))}</option>
            ${PHASES.map(([v, l]) => `<option value="${v}">${esc(l())}</option>`).join('')}
          </select>
        </label>
        <label class="avm-field tip" data-tip="${esc(tr('avm.filter.nsTip'))}">
          <span>${esc(tr('col.namespace'))}</span>
          <select data-avm="ns"><option value="">${esc(tr('avm.filter.nsAll'))}</option></select>
        </label>
        <input type="search" class="avm-search tip" data-avm="text" autocomplete="off"
               placeholder="${esc(tr('avm.filter.textPh'))}" data-tip="${esc(tr('avm.filter.textTip'))}">
        <button type="button" class="btn btn-sm tip" data-avm="refresh" data-tip="${esc(tr('avm.refreshTip'))}"
                aria-label="${esc(tr('avm.refreshTip'))}">${icon('refresh')}</button>
        <span class="avm-count" data-avm="count"></span>
      </div>
      <div class="bulk-toolbar avm-bulk" data-avm="bulk" hidden>
        <span class="bulk-count" data-avm="selcount"></span>
        <button class="btn btn-sm btn-primary tip" data-avm-bulk="start" data-tip="${esc(tr('avm.bulk.startTip'))}">
          <span class="icon-green">${icon('play')}</span> ${esc(tr('namespaces.bulkStart'))}</button>
        <button class="btn btn-sm btn-danger tip" data-avm-bulk="stop" data-tip="${esc(tr('avm.bulk.stopTip'))}">
          <span class="icon-red">${icon('stop')}</span> ${esc(tr('namespaces.bulkStop'))}</button>
        <button class="btn btn-sm btn-secondary tip" data-avm-bulk="restart" data-tip="${esc(tr('vma.bulk.restartTip'))}">
          ${icon('restart')} ${esc(tr('vma.restart'))}</button>
        <button class="btn btn-sm btn-secondary tip" data-avm-bulk="force-stop" data-tip="${esc(tr('vma.bulk.forceStopTip'))}">
          ${icon('power')} ${esc(tr('vma.forceStop'))}</button>
        <button class="btn btn-sm btn-secondary tip" data-avm-bulk="migrate" data-tip="${esc(tr('vma.bulk.migrateTip'))}">
          ${icon('migrate')} ${esc(tr('vma.migrate'))}</button>
        <button class="btn btn-sm tip" data-avm-bulk="clear" data-tip="${esc(tr('avm.bulk.clearTip'))}">
          ${esc(tr('namespaces.clearSelection'))}</button>
      </div>
      <table class="data-table ns-vm-table avm-table">
        <thead><tr>
          <th class="check-col"><input type="checkbox" data-avm="all" class="tip"
              data-tip="${esc(tr('vms.tip.checkAll'))}" title="${esc(tr('vms.tip.checkAll'))}"></th>
          ${th('cluster', tr('col.cluster'), tr('avm.tip.sortCluster'))}
          ${th('namespace', tr('col.namespace'), tr('avm.tip.sortNs'))}
          ${th('name', tr('col.vm'), tr('vms.tip.sortName'))}
          ${th('phase', tr('col.phase'), tr('vms.tip.sortPhase'))}
          ${th('cpu', tr('col.cpu'), tr('vms.tip.sortCpu'), 'num')}
          ${th('memory', tr('col.memory'), tr('vms.tip.sortMemory'), 'num')}
          ${th('ip', tr('col.ip'), tr('vms.tip.sortIp'))}
          ${th('node', tr('col.node'), tr('vms.tip.sortNode'))}
          <th>${esc(tr('col.actions'))}</th>
        </tr></thead>
        <tbody></tbody>
      </table>
      <p class="hint avm-empty" data-avm="empty" hidden></p>
      <pre class="live-log avm-log" data-avm="log" hidden></pre>`;
    const q = (s) => host.querySelector(s);
    q('[data-avm="phase"]').value = prefs.phase;
    q('[data-avm="text"]').value = prefs.text;
    q('[data-avm="phase"]').addEventListener('change', (e) => { prefs.phase = e.target.value; savePrefs(); render(); });
    q('[data-avm="ns"]').addEventListener('change', (e) => { prefs.ns = e.target.value; savePrefs(); render(); });
    q('[data-avm="text"]').addEventListener('input', (e) => { prefs.text = e.target.value; savePrefs(); render(); });
    q('[data-avm="refresh"]').addEventListener('click', () => load());
    q('[data-avm="all"]').addEventListener('change', (e) => {
      for (const v of visible()) { if (e.target.checked) selection.add(keyOf(v)); else selection.delete(keyOf(v)); }
      render();
    });
    host.querySelectorAll('[data-avm-sort]').forEach((th) => th.addEventListener('click', () => {
      const k = th.dataset.avmSort;
      if (prefs.sort === k) prefs.dir = prefs.dir === 'asc' ? 'desc' : 'asc';
      else { prefs.sort = k; prefs.dir = 'asc'; }
      savePrefs(); render();
    }));
    host.querySelectorAll('[data-avm-bulk]').forEach((b) => b.addEventListener('click', () => bulk(b.dataset.avmBulk)));
    q('[data-avm="clusters"]').addEventListener('click', (e) => {
      const chip = e.target.closest('[data-avm-cluster]');
      if (!chip) return;
      const c = chip.dataset.avmCluster;
      if (c === '*') prefs.clusters = [];
      else if (prefs.clusters.includes(c)) prefs.clusters = prefs.clusters.filter((x) => x !== c);
      else prefs.clusters = [...prefs.clusters, c];
      savePrefs(); render();
    });
    host.querySelector('tbody').addEventListener('change', (e) => {
      const cb = e.target.closest('input[data-avm-key]');
      if (!cb) return;
      if (cb.checked) selection.add(cb.dataset.avmKey); else selection.delete(cb.dataset.avmKey);
      cb.closest('tr').classList.toggle('selected', cb.checked);
      renderBulk();
    });
    host.querySelector('tbody').addEventListener('click', onRowAction);
  }

  // -- rendu ------------------------------------------------------------------
  const CL_STATE = {
    unreachable: ['warn', () => tr('avm.cl.unreachable'), () => tr('avm.cl.unreachableTip')],
    denied: ['fail', () => tr('avm.cl.denied'), () => tr('avm.cl.deniedTip')],
    error: ['fail', () => tr('avm.cl.error'), () => tr('avm.cl.errorTip')],
    unknown: ['warn', () => tr('avm.cl.unknown'), () => tr('avm.cl.unknownTip')],
  };

  function renderClusters() {
    const box = host.querySelector('[data-avm="clusters"]');
    const all = !prefs.clusters.length;
    const chips = (data ? data.clusters : []).map((c) => {
      const on = prefs.clusters.includes(c.cluster);
      const st = CL_STATE[c.state];
      const tip = st ? `${st[2]()}${c.error ? ` (${c.error})` : ''}` : tr('avm.cl.tip', { n: c.count || 0 });
      return `<button type="button" class="avm-chip tip${on ? ' active' : ''}${st ? ` avm-chip-${st[0]}` : ''}"
          data-avm-cluster="${esc(c.cluster)}" aria-pressed="${on}" data-tip="${esc(tip)}">
          ${icon('cloud', 13)} <span>${esc(c.cluster)}</span>
          ${st ? `<span class="badge ${st[0]}">${esc(st[1]())}</span>` : `<span class="avm-chip-n">${c.count || 0}</span>`}
        </button>`;
    }).join('');
    box.innerHTML = `<button type="button" class="avm-chip tip${all ? ' active' : ''}" data-avm-cluster="*"
        aria-pressed="${all}" data-tip="${esc(tr('avm.filter.allClustersTip'))}">${esc(tr('avm.filter.allClusters'))}</button>${chips}`;
  }

  function renderNamespaces() {
    const sel = host.querySelector('[data-avm="ns"]');
    const names = [...new Set((data ? data.vms : [])
      .filter((v) => !prefs.clusters.length || prefs.clusters.includes(v.cluster)).map((v) => v.namespace))].sort();
    if (prefs.ns && !names.includes(prefs.ns)) names.unshift(prefs.ns);
    sel.innerHTML = `<option value="">${esc(tr('avm.filter.nsAll'))}</option>`
      + names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
    sel.value = prefs.ns;
  }

  function row(v) {
    const k = keyOf(v);
    const phase = v.phase || 'Stopped';
    const halted = v.runStrategy === 'Halted';
    const b = (attr, ic, tip, cls = '') => `<button type="button" class="btn-icon-action ${cls} tip" data-avm-act="${attr}"
        data-tip="${esc(tip)}" aria-label="${esc(tip)}">${ic}</button>`;
    const ips = v.ips || [];
    return `<tr data-avm-row="${esc(k)}" class="${selection.has(k) ? 'selected' : ''}">
      <td class="check-col"><input type="checkbox" data-avm-key="${esc(k)}" ${selection.has(k) ? 'checked' : ''}
          aria-label="${esc(`${v.cluster}/${v.namespace}/${v.name}`)}"></td>
      <td><span class="avm-cluster">${esc(v.cluster)}</span></td>
      <td>${esc(v.namespace)}</td>
      <td>${esc(v.name)}${v.agent_connected === 'True' ? ` <span class="agent-dot ok tip" data-tip="${esc(tr('vm.tooltip.agentOk'))}">${icon('dotOn', 10)}</span>` : ''}</td>
      <td><span class="phase ${esc(phase)} tip" data-tip="${esc(tr('avm.tip.strategy', { s: v.runStrategy || '?' }))}">${esc(phaseLabel(phase))}</span></td>
      <td class="num">${v.cpu ? esc(v.cpu) : '–'}</td>
      <td class="num">${esc(v.memory || '–')}</td>
      <td>${ips.length ? `<code>${esc(ips[0])}</code>${ips.length > 1 ? ` <span class="res-dim tip" data-tip="${esc(ips.slice(1).join(', '))}">+${ips.length - 1}</span>` : ''}` : '<span class="res-dim">–</span>'}</td>
      <td>${v.node ? esc(v.node) : '<span class="res-dim">–</span>'}</td>
      <td class="vm-actions-cell">
        ${halted ? b('start', `<span class="icon-green">${icon('play')}</span>`, tr('vm.tooltip.start'), 'start')
                 : b('stop', `<span class="icon-red">${icon('stop')}</span>`, tr('vm.tooltip.stop'), 'stop')}
        ${b('console', icon('console'), tr('vm.tooltip.console'), 'console')}
        ${b('snapshot', icon('snapshot'), tr('vm.tooltip.snapshot'), 'snapshot')}
        ${b('migrate', icon('migrate'), tr('vm.tooltip.migrate'), 'migrate')}
        ${b('edit', icon('settings'), tr('vm.tooltip.edit'), 'edit')}
        ${b('notes', icon('notes'), tr('vm.tooltip.notes'), 'notes')}
        ${b('more', icon('more'), tr('vma.moreTip'), 'more')}
      </td></tr>`;
  }

  function renderBulk() {
    const bar = host.querySelector('[data-avm="bulk"]');
    // une VM sortie de la liste (supprimée, cluster éteint) quitte la sélection
    const known = new Set((data ? data.vms : []).map(keyOf));
    for (const k of [...selection]) if (!known.has(k)) selection.delete(k);
    bar.hidden = !selection.size;
    const clusters = new Set([...selection].map((k) => k.split('|')[0]));
    host.querySelector('[data-avm="selcount"]').textContent =
      tr('avm.selected', { n: selection.size, c: clusters.size });
    const rows = visible();
    const all = host.querySelector('[data-avm="all"]');
    all.checked = rows.length > 0 && rows.every((v) => selection.has(keyOf(v)));
  }

  function render() {
    if (!host) return;
    renderClusters();
    renderNamespaces();
    const rows = visible();
    host.querySelector('tbody').innerHTML = rows.map(row).join('');
    host.querySelectorAll('[data-avm-sort]').forEach((th) => {
      const on = th.dataset.avmSort === prefs.sort;
      th.classList.toggle('active-sort', on);
      th.querySelector('.sort-arrow').innerHTML = on ? icon(prefs.dir === 'asc' ? 'arrowUp' : 'arrowDown', 12) : '';
      th.setAttribute('aria-sort', on ? (prefs.dir === 'asc' ? 'ascending' : 'descending') : 'none');
    });
    const total = data ? data.vms.length : 0;
    host.querySelector('[data-avm="count"]').textContent = data ? tr('avm.count', { n: rows.length, total }) : tr('common.loading');
    const empty = host.querySelector('[data-avm="empty"]');
    empty.hidden = !data || rows.length > 0;
    empty.textContent = total ? tr('avm.emptyFiltered') : tr('avm.empty');
    renderBulk();
  }

  // -- gestes -------------------------------------------------------------------
  function findRow(el) {
    const tr_ = el.closest('[data-avm-row]');
    if (!tr_) return null;
    const [cluster, ns, name] = tr_.dataset.avmRow.split('|');
    return { cluster, ns, name };
  }

  async function setStrategy(v, target) {
    const r = await fetch(`/api/vm/${enc(v.cluster)}/${enc(v.ns)}/${enc(v.name)}/runStrategy`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ runStrategy: target }),
    });
    if (!r.ok) {
      let d = {};
      try { d = await r.json(); } catch { /* sans corps */ }
      throw new Error(d.hint || d.error || `HTTP ${r.status}`);
    }
  }

  const later = (ms = 1200) => setTimeout(() => load(), ms);

  function onRowAction(e) {
    const btn = e.target.closest('[data-avm-act]');
    if (!btn) return;
    const v = findRow(btn);
    if (!v) return;
    const act = btn.dataset.avmAct;
    if (act === 'start' || act === 'stop') {
      setStrategy(v, act === 'start' ? 'Always' : 'Halted').then(() => later(600))
        .catch((err) => alert(tr('avm.actFailed', { vm: `${v.cluster}/${v.ns}/${v.name}`, msg: err.message })));
    } else if (act === 'console' && window.VMConsole) VMConsole.open(v.cluster, v.ns, v.name);
    else if (act === 'snapshot' && window.VMSnapshots) VMSnapshots.open(v.cluster, v.ns, v.name);
    else if (act === 'migrate' && window.VMMigrate) VMMigrate.open(v.cluster, v.ns, v.name);
    else if (act === 'edit' && window.VMEdit) VMEdit.open(v.cluster, v.ns, v.name);
    else if (act === 'notes' && window.Notes) Notes.open('vm', v.cluster, v.ns, v.name);
    else if (act === 'more' && window.VMActions) VMActions.open(btn, v.cluster, v.ns, v.name, { onDone: () => later(800) });
  }

  async function bulk(action) {
    if (action === 'clear') { selection.clear(); render(); return; }
    if (!selection.size) return;
    const byCluster = new Map();
    for (const k of selection) {
      const [c, ns, name] = k.split('|');
      if (!byCluster.has(c)) byCluster.set(c, []);
      byCluster.get(c).push(`${ns}/${name}`);
    }
    const label = { start: tr('namespaces.bulkStart'), stop: tr('namespaces.bulkStop'), restart: tr('vma.restart'),
                    'force-stop': tr('vma.forceStop'), migrate: tr('vma.migrate') }[action] || action;
    if (!confirm(tr('avm.confirm', { action: label, n: selection.size, c: byCluster.size }))) return;
    const log = host.querySelector('[data-avm="log"]');
    log.hidden = false;
    log.textContent = '';
    for (const [cluster, refs] of byCluster) {
      if (action === 'start' || action === 'stop') {
        for (const ref of refs) {
          const [ns, name] = ref.split('/');
          const line = document.createElement('div');
          line.textContent = `${cluster}/${ns}/${name}: ${label}… `;
          log.appendChild(line);
          try {
            await setStrategy({ cluster, ns, name }, action === 'start' ? 'Always' : 'Halted');
            line.insertAdjacentHTML('beforeend', icon('ok'));
          } catch (err) {
            line.insertAdjacentHTML('beforeend', `${icon('fail')} ${esc(err.message)}`);
          }
        }
      } else if (window.VMActions) {
        const head = document.createElement('div');
        head.className = 'avm-log-cluster';
        head.textContent = cluster;
        log.appendChild(head);
        const part = document.createElement('div');
        log.appendChild(part);
        await VMActions.bulk(cluster, refs, action, part);
      }
    }
    later(1500);
  }

  // -- cycle ----------------------------------------------------------------------
  async function load() {
    if (!host || loading) return;
    loading = true;
    try {
      const r = await fetch('/api/vms-all');
      if (r.ok) data = await r.json();
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
})();

window.AllVMs = AllVMs;
