/**
 * harvester-ops : l'inventaire des GPU (v1.92.0), onglet Advanced > GPU.
 *
 * Lecture seule. Une unité est ce qu'on pourra réserver : un GPU entier
 * (passthrough), un vGPU, les instances MIG d'un GPU, ou la ressource GPU
 * d'un nœud pour les conteneurs. Un sélecteur « Ce cluster / Tous les
 * clusters » (/api/gpus/<c> ou /api/gpus-all), une carte par type (total,
 * utilisées, libres), puis le tableau filtrable par cluster, nœud, type,
 * état ; le consommateur ouvre la VM, ou la charge dans la vue Containers.
 */
const Gpus = (() => {
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const enc = encodeURIComponent;
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 13) => (window.Icons ? Icons.svg(n, { size }) : '');
  const REFRESH_MS = 20000;
  const STORE = 'harvester_ops_gpus';
  const TYPES = ['gpu', 'vgpu', 'mig', 'node'];
  const STATES = ['free', 'used', 'disabled'];
  // Clés en toutes lettres : le contrôle de parité i18n ne lit que des littéraux.
  const TYPE_LABEL = { gpu: () => tr('gpu.type.gpu'), vgpu: () => tr('gpu.type.vgpu'),
                       mig: () => tr('gpu.type.mig'), node: () => tr('gpu.type.node') };
  const TYPE_TIP = { gpu: () => tr('gpu.type.gpuTip'), vgpu: () => tr('gpu.type.vgpuTip'),
                     mig: () => tr('gpu.type.migTip'), node: () => tr('gpu.type.nodeTip') };
  const STATE_LABEL = { free: () => tr('gpu.state.free'), used: () => tr('gpu.state.used'),
                        disabled: () => tr('gpu.state.disabled') };
  const STATE_BADGE = { free: 'ok', used: 'info', disabled: 'dim' };
  const REASON = { vgpu: () => tr('gpu.reason.vgpu'), mig: () => tr('gpu.reason.mig'),
                   containers: () => tr('gpu.reason.containers'), 'passthrough-off': () => tr('gpu.reason.passthroughOff') };
  const CL_STATE = {
    unreachable: ['warn', () => tr('avm.cl.unreachable'), () => tr('gpu.cl.unreachableTip')],
    denied: ['fail', () => tr('avm.cl.denied'), () => tr('gpu.cl.deniedTip')],
    error: ['fail', () => tr('avm.cl.error'), () => tr('gpu.cl.errorTip')],
    unknown: ['warn', () => tr('avm.cl.unknown'), () => tr('avm.cl.unknownTip')],
    'no-addon': ['dim', () => tr('gpu.cl.noAddon'), () => tr('gpu.noAddon')],
  };

  let cur = null;   // { cluster, host, timer, data, prefs }

  const badge = (cls, text, tip) =>
    `<span class="badge ${cls}${tip ? ' tip' : ''}"${tip ? ` data-tip="${esc(tip)}"` : ''}>${esc(text)}</span>`;

  function loadPrefs() {
    const p = { scope: 'cluster', cluster: '', node: '', type: '', state: '', text: '' };
    try { Object.assign(p, JSON.parse(localStorage.getItem(STORE) || '{}')); } catch { /* réglages illisibles */ }
    if (p.scope !== 'all') p.scope = 'cluster';
    return p;
  }
  function savePrefs() {
    try { localStorage.setItem(STORE, JSON.stringify(cur.prefs)); } catch { /* stockage indisponible */ }
  }

  // -- cycle de vie (appelé par Sections) -------------------------------------
  function start(cluster, host) {
    stop();
    cur = { cluster, host, data: null, prefs: loadPrefs() };
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
    const all = c.prefs.scope === 'all';
    let d = null;
    try {
      const r = await fetch(all ? '/api/gpus-all' : `/api/gpus/${enc(c.cluster)}`);
      d = await r.json().catch(() => null);
      if (d && !r.ok && !d.error) d.error = `HTTP ${r.status}`;
    } catch { d = null; }
    if (c !== cur || all !== (c.prefs.scope === 'all')) return;
    c.data = d;
    render();
  }

  function shell() {
    const p = cur.prefs;
    const scope = (v, label, tip) => `<button type="button" class="res-chip adv-chip tip${p.scope === v ? ' is-on' : ''}"
        data-gpu="scope" data-scope="${v}" aria-pressed="${p.scope === v}" data-tip="${esc(tip)}">${esc(label)}</button>`;
    const opts = (list, label, all) => `<option value="">${esc(all)}</option>`
      + list.map(v => `<option value="${esc(v)}">${esc(label(v))}</option>`).join('');
    cur.host.innerHTML = `<div class="card na-card gpu-card">
        <div class="res-tools">
          <span class="gpu-scope" role="group" aria-label="${esc(tr('gpu.t.scope'))}">
            ${scope('cluster', tr('gpu.scope.cluster'), tr('gpu.t.scopeCluster'))}
            ${scope('all', tr('gpu.scope.all'), tr('gpu.t.scopeAll'))}</span>
          <select class="tip" data-gpu="cluster" data-tip="${esc(tr('gpu.t.cluster'))}" ${p.scope === 'all' ? '' : 'hidden'}><option value="">${esc(tr('avm.filter.allClusters'))}</option></select>
          <select class="tip" data-gpu="node" data-tip="${esc(tr('gpu.t.node'))}"><option value="">${esc(tr('dev.allNodes'))}</option></select>
          <select class="tip" data-gpu="type" data-tip="${esc(tr('gpu.t.type'))}">${opts(TYPES, (t) => TYPE_LABEL[t](), tr('gpu.allTypes'))}</select>
          <select class="tip" data-gpu="state" data-tip="${esc(tr('gpu.t.state'))}">${opts(STATES, (s) => STATE_LABEL[s](), tr('gpu.allStates'))}</select>
          <input type="search" class="res-filter tip" data-gpu="text" data-tip="${esc(tr('gpu.t.text'))}" placeholder="${esc(tr('gpu.textPh'))}">
          <span class="res-count"></span>
          <button type="button" class="btn btn-sm btn-secondary tip" data-gpu="refresh" data-tip="${esc(tr('res.refreshTip'))}">${icon('refresh')} ${esc(tr('overview.refresh'))}</button>
        </div>
        <div class="res-feedback" data-gpu="notice"></div>
        <div class="metrics gpu-summary" data-gpu="summary"></div>
        <div data-gpu="body"><p class="form-hint">${esc(tr('common.loading'))}</p></div></div>`;
    const card = cur.host.querySelector('.gpu-card');
    card.querySelector('[data-gpu="type"]').value = p.type;
    card.querySelector('[data-gpu="state"]').value = p.state;
    card.querySelector('[data-gpu="text"]').value = p.text;
    card.addEventListener('click', onClick);
    card.addEventListener('change', (e) => {
      const k = e.target.dataset.gpu;
      if (!cur || !['cluster', 'node', 'type', 'state'].includes(k)) return;
      cur.prefs[k] = e.target.value;
      savePrefs();
      render();
    });
    card.querySelector('[data-gpu="text"]').addEventListener('input', (e) => {
      cur.prefs.text = e.target.value; savePrefs(); render();
    });
  }

  function fillSelect(key, values, allLabel) {
    const sel = cur.host.querySelector(`[data-gpu="${key}"]`);
    if (!sel) return;
    const want = [''].concat(values).join('|');
    if ([...sel.options].map(o => o.value).join('|') !== want) {
      sel.innerHTML = `<option value="">${esc(allLabel)}</option>` + values.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
    }
    if (!values.includes(cur.prefs[key])) cur.prefs[key] = '';
    sel.value = cur.prefs[key];
  }

  function consumerHtml(c) {
    const count = c.count > 1 ? ` <span class="res-dim">x${esc(c.count)}</span>` : '';
    if (c.kind === 'vm') {
      return `<a href="#" class="res-chip tip" data-gpu="open-vm" data-cluster="${esc(c.cluster)}" data-ns="${esc(c.namespace)}"
          data-name="${esc(c.name)}" data-tip="${esc(tr('gpu.t.openVm'))}">${icon('vm', 11)} ${esc(c.name)}</a>`;
    }
    if (c.system) {
      return `<span class="res-chip tip" data-tip="${esc(tr('gpu.t.systemPod', { ns: c.namespace }))}">${icon('container', 11)} ${esc(c.name)}${count}</span>`;
    }
    return `<a href="#" class="res-chip tip" data-gpu="open-pod" data-cluster="${esc(c.cluster)}" data-ns="${esc(c.namespace)}"
        data-name="${esc(c.name)}" data-tip="${esc(tr('gpu.t.openPod'))}">${icon('container', 11)} ${esc(c.name)}${count}</a>`;
  }

  function stateHtml(u) {
    let tip = '';
    if (u.reason && REASON[u.reason]) tip = REASON[u.reason]();
    else if (u.type === 'node' && u.cordoned) tip = tr('gpu.reason.cordoned');
    else if (u.type === 'vgpu' && u.state === 'disabled') tip = tr('gpu.reason.vgpuOff');
    else if (u.type === 'mig' && u.state === 'disabled') tip = tr('gpu.reason.migOff');
    let out = badge(STATE_BADGE[u.state] || 'dim', STATE_LABEL[u.state] ? STATE_LABEL[u.state]() : u.state, tip);
    if (u.type === 'mig' && u.sync === 'out-of-sync') {
      out += ` ${badge('warn', tr('gpu.outOfSync'), u.message || tr('gpu.t.outOfSync'))}`;
    }
    return out;
  }

  function modelHtml(u) {
    if (u.type === 'mig' && (u.profiles || []).length) {
      const tip = u.profiles.map(p => tr('gpu.migProfile', { name: p.name, created: p.created, available: p.available, total: p.total })).join(' · ');
      return `<span class="tip" data-tip="${esc(tip)}">${esc(u.model || tr('gpu.migNone'))}</span>`;
    }
    const sub = u.type === 'gpu' && u.vgpus ? `<div class="res-dim">${esc(tr('gpu.vgpusOf', { n: u.vgpus }))}</div>` : '';
    return `${esc(u.model || '–')}${u.resource && u.resource !== u.model ? `<div class="res-dim"><code>${esc(u.resource)}</code></div>` : ''}${sub}`;
  }

  function emptyText(d, all) {
    if (all) return tr('gpu.noneAll');
    if (!d.addon) return tr('gpu.noneNoAddon');
    return tr('gpu.none');
  }

  function render() {
    if (!cur || !cur.host.isConnected) return;
    const p = cur.prefs;
    const all = p.scope === 'all';
    const body = cur.host.querySelector('[data-gpu="body"]');
    const notice = cur.host.querySelector('[data-gpu="notice"]');
    const sumEl = cur.host.querySelector('[data-gpu="summary"]');
    cur.host.querySelector('[data-gpu="cluster"]').hidden = !all;
    cur.host.querySelectorAll('[data-gpu="scope"]').forEach(b => {
      const on = b.dataset.scope === p.scope;
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-pressed', String(on));
    });
    const d = cur.data;
    if (!d) { sumEl.innerHTML = ''; notice.innerHTML = ''; body.innerHTML = `<p class="res-error">${esc(tr('fabric.unreachable'))}</p>`; return; }
    if (!all && (d.unreachable || d.error)) {
      sumEl.innerHTML = '';
      notice.innerHTML = '';
      const msg = d.unreachable ? tr('fabric.unreachable') : (d.denied ? tr('gpu.cl.deniedTip') : (d.detail || d.error));
      body.innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(msg)}</div></div>`;
      return;
    }
    // ce qui n'a pas été lu : add-on coupé, CRD absentes, clusters muets, refus
    const notes = [];
    if (all) {
      const bad = (d.clusters || []).filter(c => c.state !== 'ok');
      if (bad.length) {
        notes.push(`<div class="avm-clusters" data-gpu="clusters">${bad.map(c => {
          const st = CL_STATE[c.state] || CL_STATE.error;
          return `<span class="avm-chip tip" data-tip="${esc(st[2]() + (c.error ? ` (${c.error})` : ''))}">${esc(c.cluster)} ${badge(st[0], st[1]())}</span>`;
        }).join('')}</div>`);
      }
    } else {
      if (!d.addon) {
        notes.push(`<div class="sto-finding sev-action"><div class="sto-finding-title">${icon('warn')} ${esc(tr('gpu.noAddon'))}</div>
          <button type="button" class="btn btn-sm btn-secondary tip" data-gpu="open-addons" data-tip="${esc(tr('dev.t.openAddons'))}">${icon('plug')} ${esc(tr('tab.addons'))}</button></div>`);
      } else {
        const off = ['vgpu', 'mig'].filter(t => d.available && d.available[t] === false);
        if (off.length) notes.push(`<p class="form-hint">${esc(tr('gpu.notAvailable', { list: off.map(t => TYPE_LABEL[t]()).join(', ') }))}</p>`);
      }
      if ((d.denials || []).length) notes.push(`<p class="form-hint">${icon('lock')} ${esc(tr('gpu.partial'))}</p>`);
    }
    notice.innerHTML = notes.join('');

    const units = d.units || [];
    if (all) fillSelect('cluster', [...new Set(units.map(u => u.cluster))].sort(), tr('avm.filter.allClusters'));
    const inCluster = units.filter(u => !all || !p.cluster || u.cluster === p.cluster);
    fillSelect('node', [...new Set(inCluster.map(u => u.node).filter(Boolean))].sort(), tr('dev.allNodes'));
    const words = p.text.toLowerCase().split(/\s+/).filter(Boolean);
    const rows = inCluster.filter(u => (!p.node || u.node === p.node) && (!p.type || u.type === p.type)
      && (!p.state || u.state === p.state)
      && (!words.length || words.every(w => [u.name, u.model, u.node, u.cluster, u.parent, u.resource,
        ...(u.consumers || []).map(c => `${c.namespace}/${c.name}`)].join(' ').toLowerCase().includes(w))));

    // une carte par type : la somme de ce qui est affiché par cluster et nœud
    const sum = {};
    TYPES.forEach(t => { sum[t] = { total: 0, used: 0, free: 0 }; });
    inCluster.filter(u => !p.node || u.node === p.node).forEach(u => {
      const s = sum[u.type];
      if (!s) return;
      s.total += u.total; s.used += u.used; s.free += u.state === 'disabled' ? 0 : u.free;
    });
    sumEl.innerHTML = TYPES.map(t => `<div class="metric gpu-metric tip" data-gpu-type="${t}" data-tip="${esc(TYPE_TIP[t]())}">
        <div class="label">${esc(TYPE_LABEL[t]())}</div>
        <div class="value">${esc(sum[t].total)}</div>
        <div class="res-dim">${esc(tr('gpu.sumLine', { used: sum[t].used, free: sum[t].free }))}</div></div>`).join('');

    cur.host.querySelector('.res-count').textContent = tr('gpu.count', { n: rows.length });
    if (!units.length) {
      body.innerHTML = `<p class="form-hint gpu-empty">${esc(emptyText(d, all))}</p>`;
      return;
    }
    if (!rows.length) { body.innerHTML = `<p class="form-hint gpu-empty">${esc(tr('gpu.noMatch'))}</p>`; return; }
    const th = (text) => `<th>${esc(text)}</th>`;
    body.innerHTML = `<table class="data-table dev-table gpu-table"><thead><tr>
        ${all ? th(tr('gpu.col.cluster')) : ''}${th(tr('gpu.col.node'))}${th(tr('gpu.col.type'))}${th(tr('gpu.col.unit'))}${th(tr('gpu.col.model'))}
        ${th(tr('gpu.col.parent'))}${th(tr('gpu.col.state'))}${th(tr('gpu.col.used'))}${th(tr('gpu.col.consumer'))}</tr></thead><tbody>
      ${rows.map(u => `<tr data-unit="${esc(u.cluster)}|${esc(u.id)}" data-type="${esc(u.type)}">
        ${all ? `<td>${esc(u.cluster)}</td>` : ''}<td>${esc(u.node || '–')}</td>
        <td><span class="tip" data-tip="${esc(TYPE_TIP[u.type] ? TYPE_TIP[u.type]() : '')}">${esc(TYPE_LABEL[u.type] ? TYPE_LABEL[u.type]() : u.type)}</span></td>
        <td><strong>${esc(u.type === 'node' ? u.name : (u.address || u.name))}</strong>${u.type !== 'node' && u.address ? `<div class="res-dim">${esc(u.name)}</div>` : ''}</td>
        <td>${modelHtml(u)}</td>
        <td>${u.parent ? `<code class="res-key">${esc(u.parent)}</code>` : '–'}</td>
        <td>${stateHtml(u)}</td>
        <td class="tip" data-tip="${esc(tr('gpu.t.used', { used: u.used, total: u.total }))}">${esc(u.used)}/${esc(u.total)}</td>
        <td>${(u.consumers || []).length ? u.consumers.map(consumerHtml).join(' ') : '–'}</td></tr>`).join('')}
      </tbody></table>`;
  }

  function onClick(e) {
    const b = e.target.closest('[data-gpu]');
    if (!b || !cur || b.tagName === 'INPUT' || b.tagName === 'SELECT') return;
    const act = b.dataset.gpu;
    if (act === 'refresh') return load();
    if (act === 'scope') {
      if (cur.prefs.scope === b.dataset.scope) return;
      cur.prefs.scope = b.dataset.scope;
      savePrefs();
      cur.data = null;
      render();
      return load();
    }
    if (act === 'open-addons' && window.Sections) return Sections.open('addons', 'list');
    if (act === 'open-vm') {
      e.preventDefault();
      if (window.VMEdit && VMEdit.open) VMEdit.open(b.dataset.cluster, b.dataset.ns, b.dataset.name);
      return;
    }
    if (act === 'open-pod') {
      e.preventDefault();
      if (window.Containers && Containers.all.focus) {
        Containers.all.focus({ cluster: b.dataset.cluster, namespace: b.dataset.ns, text: b.dataset.name });
      }
      if (window.App && App.setTab) App.setTab('allcontainers');
    }
  }

  return { start, stop };
})();
window.Gpus = Gpus;
