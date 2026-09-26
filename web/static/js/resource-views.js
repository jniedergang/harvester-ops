/**
 * harvester-ops — les listes des sections de Harvester (v1.57.0)
 *
 * Storage > Images et Storage Classes, Security > Secrets et SSH Keys,
 * Add-ons : une liste lue par /api/cluster-objects/<cluster>/<type>, avec qui
 * s'en sert (les VMs, en lien vers leur fenêtre de réglages). Une seule liste
 * vit à la fois ; elle se relit toutes les 10 s tant qu'elle est à l'écran.
 * Un Secret n'arrive jamais avec ses valeurs : seulement le nom de ses clés.
 */
const ResourceViews = (() => {
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const enc = encodeURIComponent;
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 13) => (window.Icons ? Icons.svg(n, { size }) : '');
  const REFRESH_MS = 10000;

  let cur = null;      // { kind, cluster, host, timer, data, filter, showSystem, open:Set }

  // -- mise en forme --------------------------------------------------------
  function bytes(n) {
    if (n == null || n === '' || isNaN(n)) return '–';
    const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let v = Number(n), i = 0;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return `${v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)} ${u[i]}`;
  }
  function age(ts) {
    if (!ts) return '–';
    const d = (Date.now() - Date.parse(ts)) / 1000;
    if (isNaN(d)) return esc(ts);
    if (d < 3600) return tr('res.age.min', { n: Math.max(1, Math.round(d / 60)) });
    if (d < 86400) return tr('res.age.hours', { n: Math.round(d / 3600) });
    return tr('res.age.days', { n: Math.round(d / 86400) });
  }
  function vms(list) {
    if (!list || !list.length) return `<span class="res-dim">${esc(tr('res.unused'))}</span>`;
    const shown = list.slice(0, 3).map(ref => `<button type="button" class="res-chip tip" data-vm="${esc(ref)}"
        data-tip="${esc(tr('res.openVmTip'))}">${icon('vm', 11)} ${esc(ref.split('/')[1] || ref)}</button>`).join('');
    const more = list.length > 3 ? ` <span class="res-dim tip" data-tip="${esc(list.slice(3).join(', '))}">+${list.length - 3}</span>` : '';
    return shown + more;
  }
  const badge = (cls, text, tip) =>
    `<span class="badge ${cls}${tip ? ' tip' : ''}"${tip ? ` data-tip="${esc(tip)}"` : ''}>${esc(text)}</span>`;

  // -- colonnes et détails par type -----------------------------------------
  // Clés i18n en toutes lettres : le contrôle de parité ne lit que des littéraux.
  const IMG_STATE = {
    ready: () => badge('ok', tr('res.img.ready')),
    failed: (r) => badge('fail', tr('res.img.failed'), r.message),
    importing: (r) => badge('info', tr('res.img.importing', { pct: r.progress == null ? 0 : r.progress })),
  };
  const ADDON_STATE = {
    AddonDeploySuccessful: () => badge('ok', tr('res.addon.deployed')),
    AddonDisabled: () => badge('dim', tr('res.addon.disabled')),
    AddonEnabling: () => badge('info', tr('res.addon.enabling')),
    AddonDeploying: () => badge('info', tr('res.addon.enabling')),
    AddonDisabling: () => badge('info', tr('res.addon.disabling')),
    AddonUpdating: () => badge('info', tr('res.addon.updating')),
  };
  const ADDON_DESC = {
    'rancher-logging': () => tr('res.addonDesc.logging'),
    'rancher-monitoring': () => tr('res.addonDesc.monitoring'),
    'harvester-seeder': () => tr('res.addonDesc.seeder'),
    'nvidia-driver-toolkit': () => tr('res.addonDesc.nvidia'),
    'pcidevices-controller': () => tr('res.addonDesc.pcidevices'),
    'vm-import-controller': () => tr('res.addonDesc.vmimport'),
    'descheduler': () => tr('res.addonDesc.descheduler'),
    'kubeovn-operator': () => tr('res.addonDesc.kubeovn'),
  };
  const addonBusy = (r) => /ing$/.test(r.status || '');

  const VIEWS = {
    images: {
      cols: () => [tr('res.col.name'), tr('res.col.source'), tr('res.col.size'), tr('res.col.state'),
                   tr('res.col.class'), tr('res.col.usedBy'), tr('res.col.age')],
      row: (r) => [
        `<strong>${esc(r.display_name)}</strong><div class="res-dim">${esc(r.namespace)}/${esc(r.name)}</div>`,
        esc(r.source_type || '–'),
        `${bytes(r.virtual_size)}<div class="res-dim">${esc(tr('res.img.file', { size: bytes(r.size) }))}</div>`,
        (IMG_STATE[r.state] || IMG_STATE.importing)(r),
        `<code>${esc(r.storage_class || '–')}</code>`, vms(r.used_by), age(r.created)],
      details: (r) => [[tr('res.d.url'), r.url ? `<code>${esc(r.url)}</code>` : '–'],
                       [tr('res.d.backend'), esc(r.backend || '–')],
                       [tr('res.d.volumes'), esc(r.volumes)],
                       ...(r.message ? [[tr('res.d.message'), esc(r.message)]] : [])],
      text: (r) => `${r.display_name} ${r.namespace}/${r.name} ${r.source_type} ${r.url || ''}`,
      sort: [(r) => r.display_name, (r) => r.source_type, (r) => r.virtual_size || 0, (r) => r.state,
             (r) => r.storage_class, (r) => (r.used_by || []).length, (r) => r.created]
    },
    storageclasses: {
      cols: () => [tr('res.col.name'), tr('res.col.replicas'), tr('res.col.reclaim'), tr('res.col.binding'),
                   tr('res.col.expansion'), tr('res.col.volumes'), tr('res.col.age')],
      row: (r) => [
        `<strong>${esc(r.name)}</strong> ${r.is_default ? badge('ok', tr('res.sc.default'), tr('res.sc.defaultTip')) : ''}
         ${r.image ? `<div class="res-dim tip" data-tip="${esc(tr('res.sc.imageTip'))}">${icon('cdrom', 11)} ${esc(r.image.display_name || r.image.ref)}</div>` : ''}`,
        esc(r.replicas || '–'), esc(r.reclaim_policy || '–'), esc(r.binding || '–'),
        r.expansion ? icon('ok') : icon('fail'), esc(r.volumes), age(r.created)],
      details: (r) => [[tr('res.d.provisioner'), `<code>${esc(r.provisioner)}</code>`],
                       ...Object.entries(r.parameters || {}).map(([k, v]) => [k, `<code>${esc(v)}</code>`])],
      text: (r) => `${r.name} ${(r.image || {}).display_name || ''}`,
      sort: [(r) => r.name, (r) => Number(r.replicas) || 0, (r) => r.reclaim_policy, (r) => r.binding,
             (r) => (r.expansion ? 1 : 0), (r) => r.volumes, (r) => r.created]
    },
    sshkeys: {
      cols: () => [tr('res.col.name'), tr('res.col.fingerprint'), tr('res.col.state'), tr('res.col.usedBy'), tr('res.col.age')],
      row: (r) => [
        `<strong>${esc(r.name)}</strong><div class="res-dim">${esc(r.namespace)}</div>`,
        `<code>${esc(r.fingerprint || '–')}</code>`,
        r.validated ? badge('ok', tr('res.key.valid')) : badge('warn', tr('res.key.pending')),
        vms(r.used_by), age(r.created)],
      details: (r) => [[tr('res.d.publicKey'), `<code class="res-wrap">${esc(r.public_key || '')}</code>`]],
      text: (r) => `${r.namespace}/${r.name} ${r.fingerprint || ''}`,
      sort: [(r) => r.name, (r) => r.fingerprint, (r) => (r.validated ? 1 : 0), (r) => (r.used_by || []).length,
             (r) => r.created]
    },
    secrets: {
      cols: () => [tr('res.col.name'), tr('res.col.type'), tr('res.col.keys'), tr('res.col.usedBy'), tr('res.col.age')],
      row: (r) => [
        `<strong>${esc(r.name)}</strong><div class="res-dim">${esc(r.namespace)}</div>
         ${r.cloud_init ? badge('info', tr('res.sec.cloudinit'), tr('res.sec.cloudinitTip')) : ''}`,
        `<code>${esc(r.type)}</code>`,
        (r.keys || []).map(k => `<code class="res-key">${esc(k)}</code>`).join(' ') || '–',
        vms(r.used_by), age(r.created)],
      details: null,
      text: (r) => `${r.namespace}/${r.name} ${r.type} ${(r.keys || []).join(' ')}`,
      sort: [(r) => `${r.name} ${r.namespace}`, (r) => r.type, (r) => (r.keys || []).length,
             (r) => (r.used_by || []).length, (r) => r.created]
    },
    addons: {
      cols: () => [tr('res.col.name'), tr('res.col.chart'), tr('res.col.state'), ''],
      row: (r) => [
        `<strong>${esc(r.name)}</strong><div class="res-dim">${esc(r.namespace)}</div>
         <div class="res-desc">${esc(ADDON_DESC[r.name] ? ADDON_DESC[r.name]() : '')}</div>`,
        `<code>${esc(r.chart)}</code><div class="res-dim">${esc(r.version || '')}</div>`,
        (/Failed/.test(r.status) || r.message ? badge('fail', tr('res.addon.failed'), r.message)
          : (ADDON_STATE[r.status] || (() => badge('dim', r.status || '?')))()),
        `<button type="button" class="btn btn-sm ${r.enabled ? 'btn-secondary' : 'btn-primary'} tip needs-admin"
            data-addon="${esc(r.namespace)}/${esc(r.name)}" data-enable="${r.enabled ? '0' : '1'}"
            data-tip="${esc(r.enabled ? tr('res.addon.disableTip') : tr('res.addon.enableTip'))}"
            ${addonBusy(r) ? 'disabled' : ''}>${esc(r.enabled ? tr('res.addon.disable') : tr('res.addon.enable'))}</button>`],
      details: null,
      text: (r) => `${r.namespace}/${r.name} ${r.chart}`,
      sort: [(r) => r.name, (r) => r.chart, (r) => r.status, null]
    },
  };

  // -- rendu ------------------------------------------------------------------
  function shell() {
    const k = cur.kind;
    cur.host.innerHTML = `
      <div class="card res-card" data-kind="${esc(k)}">
        <div class="res-tools">
          <input type="search" class="res-filter tip" data-tip="${esc(tr('res.filterTip'))}"
                 placeholder="${esc(tr('res.filter'))}" value="${esc(cur.filter)}">
          <span class="res-count"></span>
          ${k === 'secrets' ? `<label class="res-system tip" data-tip="${esc(tr('res.sec.systemTip'))}">
              <input type="checkbox" class="res-system-box" ${cur.showSystem ? 'checked' : ''}> <span>${esc(tr('res.sec.system'))}</span></label>` : ''}
          <button type="button" class="btn btn-sm btn-secondary res-refresh tip" data-tip="${esc(tr('res.refreshTip'))}">${icon('refresh')} ${esc(tr('overview.refresh'))}</button>
        </div>
        <div class="res-feedback"></div>
        <div class="res-body"><p class="form-hint">${esc(tr('common.loading'))}</p></div>
      </div>`;
    const card = cur.host.querySelector('.res-card');
    card.querySelector('.res-filter').addEventListener('input', (e) => { cur.filter = e.target.value; render(); });
    card.querySelector('.res-refresh').addEventListener('click', () => load());
    card.querySelector('.res-system-box')?.addEventListener('change', (e) => { cur.showSystem = e.target.checked; load(); });
    card.addEventListener('click', onClick);
    card.addEventListener('keydown', (e) => {
      const head = e.target.closest('th[data-sort]');
      if (head && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); sortBy(Number(head.dataset.sort)); }
    });
  }

  function render() {
    if (!cur || !cur.host.isConnected) return;
    const body = cur.host.querySelector('.res-body');
    const count = cur.host.querySelector('.res-count');
    const d = cur.data;
    if (!d) return;
    if (d.unreachable) {
      body.innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(tr('fabric.unreachable'))}</div></div>`;
      count.textContent = '';
      return;
    }
    if (d.error) {
      body.innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(d.error)}</div>
        ${d.hint ? `<div class="res-dim">${esc(d.hint)}</div>` : ''}</div>`;
      count.textContent = '';
      return;
    }
    const v = VIEWS[cur.kind];
    const words = cur.filter.toLowerCase().split(/\s+/).filter(Boolean);
    const rows = (d.items || []).filter(r => !words.length || words.every(w => v.text(r).toLowerCase().includes(w)));
    count.textContent = cur.kind === 'secrets' && d.system_hidden
      ? tr('res.countHidden', { n: rows.length, hidden: d.system_hidden })
      : tr('res.count', { n: rows.length });
    if (!rows.length) {
      body.innerHTML = `<p class="form-hint">${esc((d.items || []).length ? tr('res.noMatch') : tr('res.empty'))}</p>`;
      return;
    }
    const cols = v.cols();
    // v1.57.0 : tri par colonne (clic sur l'en-tête, second clic : sens inverse)
    const srt = cur.sort;
    if (srt && v.sort[srt.col]) {
      const key = v.sort[srt.col];
      const val = (r) => { const x = key(r); return x == null ? '' : x; };
      rows.sort((a, b) => {
        const x = val(a), y = val(b);
        const c = (typeof x === 'number' && typeof y === 'number') ? x - y
          : String(x).localeCompare(String(y), undefined, { numeric: true, sensitivity: 'base' });
        return c * srt.dir;
      });
    }
    const th = (c, i) => {
      if (!c || !v.sort[i]) return `<th>${esc(c)}</th>`;
      const on = srt && srt.col === i;
      const arrow = on ? icon(srt.dir > 0 ? 'arrowUp' : 'arrowDown', 11) : '';
      return `<th class="res-sortable tip${on ? ' is-sorted' : ''}" data-sort="${i}" role="button" tabindex="0"
        aria-sort="${on ? (srt.dir > 0 ? 'ascending' : 'descending') : 'none'}"
        data-tip="${esc(tr('res.sortTip'))}">${esc(c)} ${arrow}</th>`;
    };
    body.innerHTML = `<table class="data-table res-table"><thead><tr>${cols.map(th).join('')}</tr></thead><tbody>
      ${rows.map(r => {
        const id = `${r.namespace || ''}/${r.name}`;
        const open = v.details && cur.open.has(id);
        return `<tr class="${v.details ? 'res-row' : ''}${open ? ' is-open' : ''}" data-id="${esc(id)}"
            ${v.details ? `title="${esc(tr('res.detailsTip'))}"` : ''}>${v.row(r).map(c => `<td>${c}</td>`).join('')}</tr>
          ${open ? `<tr class="res-details"><td colspan="${cols.length}"><dl>${v.details(r).map(([k, val]) =>
            `<dt>${esc(k)}</dt><dd>${val}</dd>`).join('')}</dl></td></tr>` : ''}`;
      }).join('')}</tbody></table>`;
  }

  const SORT_KEY = (kind) => `harvester_ops_res_sort_${kind}`;

  function sortBy(col) {
    const same = cur.sort && cur.sort.col === col;
    cur.sort = { col, dir: same ? -cur.sort.dir : 1 };
    try { localStorage.setItem(SORT_KEY(cur.kind), JSON.stringify(cur.sort)); } catch {}
    render();
  }

  function onClick(e) {
    const head = e.target.closest('th[data-sort]');
    if (head) { sortBy(Number(head.dataset.sort)); return; }
    const vm = e.target.closest('[data-vm]');
    if (vm) {
      const [ns, name] = vm.dataset.vm.split('/');
      if (window.VMEdit) window.VMEdit.open(cur.cluster, ns, name);
      return;
    }
    const add = e.target.closest('[data-addon]');
    if (add) { toggleAddon(add); return; }
    const row = e.target.closest('tr.res-row');
    if (row && !e.target.closest('button, a, input')) {
      const id = row.dataset.id;
      if (cur.open.has(id)) cur.open.delete(id); else cur.open.add(id);
      render();
    }
  }

  function say(html) {
    const fb = cur && cur.host.querySelector('.res-feedback');
    if (fb) fb.innerHTML = html;
  }

  async function toggleAddon(btn) {
    const ref = btn.dataset.addon;
    const enable = btn.dataset.enable === '1';
    const [ns, name] = ref.split('/');
    if (!confirm(enable ? tr('res.addon.confirmEnable', { name }) : tr('res.addon.confirmDisable', { name }))) return;
    btn.disabled = true;
    try {
      const r = await fetch(`/api/addons/${enc(cur.cluster)}/${enc(ns)}/${enc(name)}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: enable }) });
      const out = await r.json();
      if (!r.ok) throw new Error(out.hint || out.error || `HTTP ${r.status}`);
      say(esc(tr('res.addon.started', { name, id: out.action_id })));
      if (window.Dock && Dock.poll) Dock.poll();
      follow(out.action_id, name, enable);
    } catch (e) {
      btn.disabled = false;
      say(`<span class="res-error">${esc(tr('res.error', { msg: e.message }))}</span>`);
    }
  }

  function follow(actionId, name, enable) {
    load();
    if (!window.SSEReconnect) return;
    const es = SSEReconnect.connect(`/api/stream/${enc(actionId)}`, {
      on: {
        step: (e) => {
          try { const s = JSON.parse(e.data); if (s.message) say(esc(`${name}: ${s.message}`)); } catch { /* ligne illisible */ }
        },
        end: (e) => {
          let d = {};
          try { d = JSON.parse(e.data); } catch { /* fin sans détail */ }
          es.close();
          say(d.status === 'done'
            ? `${icon('ok')} ${esc(enable ? tr('res.addon.enabled', { name }) : tr('res.addon.disabledDone', { name }))}`
            : `<span class="res-error">${icon('fail')} ${esc(tr('res.error', { msg: d.error_summary || d.status || '?' }))}</span>`);
          load();
        },
      },
    });
  }

  // -- cycle de vie ---------------------------------------------------------------
  async function load() {
    if (!cur) return;
    const me = cur;
    const q = me.kind === 'secrets' && me.showSystem ? '?all=1' : '';
    try {
      const r = await fetch(`/api/cluster-objects/${enc(me.cluster)}/${enc(me.kind)}${q}`);
      const d = await r.json();
      if (me !== cur) return;
      cur.data = r.ok ? d : { error: d.error === 'cluster refused' ? tr('res.refused') : (d.error || `HTTP ${r.status}`), hint: d.hint };
    } catch (e) {
      if (me !== cur) return;
      cur.data = { error: e.message };
    }
    render();
  }

  function start(kind, cluster, host) {
    if (!VIEWS[kind] || !host) return Promise.resolve();
    const same = cur && cur.kind === kind && cur.cluster === cluster && cur.host === host;
    if (!same) {
      stop();
      let sort = null;
      try { sort = JSON.parse(localStorage.getItem(SORT_KEY(kind)) || 'null'); } catch {}
      cur = { kind, cluster, host, timer: null, data: null, filter: '', showSystem: false, open: new Set(), sort };
      shell();
    } else if (cur.timer) {
      clearInterval(cur.timer);
    }
    cur.timer = setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
    return load();
  }

  function stop() {
    if (cur && cur.timer) clearInterval(cur.timer);
    cur = null;
  }

  return { start, stop, refresh: () => load(), _bytes: bytes };
})();
window.ResourceViews = ResourceViews;
