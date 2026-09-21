/**
 * harvester-ops : la vue Stockage, lue comme un datastore d'ESXi.
 *
 *   storage classes et disques de VM  |  le moteur  |  disques des nœuds
 *
 * Un bloc par moteur de stockage (Longhorn, en pratique). À gauche, chaque
 * storage class avec sa politique (répliques, rétention), la place qu'elle
 * peut encore allouer, et les volumes qu'elle porte, rangés par VM. À
 * droite, les disques des nœuds avec leur jauge : ce qui est écrit, ce qui
 * est promis, ce qui reste.
 *
 * Un volume que ne réclame aucune VM et que ne monte aucun pod est dit
 * orphelin, et SEUL celui-là peut être supprimé d'ici, derrière le verrou
 * des gestes destructifs et une confirmation. Le serveur revérifie avant
 * d'agir : une page restée ouverte ne doit pas devenir une perte de données.
 */
const StorageMap = (() => {
  const REFRESH_MS = 8000;
  const { tr, esc, val, applyTips, bytes, kv } = window.Board;
  let cluster = null;
  let host = null;
  let timer = null;
  let lastData = null;
  let unlocked = false;
  let selected = null;               // {type: 'vol'|'disk', key}
  let showIdle = false;              // classes sans volume dépliées

  const volKey = (v) => v.pvc_name ? v.pvc_namespace + '/' + v.pvc_name : 'lh:' + v.longhorn;

  // -------------------------------------------------------------------------
  // Modèle : un bloc par moteur, les classes à gauche, les disques à droite
  // -------------------------------------------------------------------------
  function buildModel(d) {
    const classes = d.classes || [];
    const volumes = d.volumes || [];
    const byClass = {};
    volumes.forEach(v => {
      const k = v.storage_class || '';
      (byClass[k] = byClass[k] || []).push(v);
    });

    // Dans une classe : les volumes rangés par VM, puis les autres
    // consommateurs, puis les orphelins. C'est l'ordre de la question qu'on
    // se pose : à qui est ce disque, et qui ne l'est à personne.
    function groupsOf(vols) {
      const byVm = {};
      const pods = [], orphans = [], unknown = [];
      vols.forEach(v => {
        if (v.vm) (byVm[v.vm] = byVm[v.vm] || []).push(v);
        else if ((v.pods || []).length) pods.push(v);
        else if (v.orphan) orphans.push(v);
        else unknown.push(v);
      });
      const vmOf = (id) => (d.vms || []).find(x => x.namespace + '/' + x.name === id) || {};
      const vms = Object.keys(byVm).sort().map(id => ({
        id, vm: vmOf(id),
        vols: byVm[id].sort((a, b) => (a.boot_order || 99) - (b.boot_order || 99)),
      })).sort((a, b) => ((b.vm.status === 'Running') - (a.vm.status === 'Running'))
                          || a.id.localeCompare(b.id));
      return { vms, pods, orphans, unknown };
    }

    const providers = [...new Set(classes.map(c => c.provisioner))];
    const blocks = providers.map(p => {
      const cls = classes.filter(c => c.provisioner === p);
      const withVols = cls.filter(c => (byClass[c.name] || []).length)
        .map(c => ({ ...c, groups: groupsOf(byClass[c.name]),
                     count: byClass[c.name].length }));
      const idle = cls.filter(c => !(byClass[c.name] || []).length);
      return { provisioner: p, longhorn: p === 'driver.longhorn.io',
               classes: withVols, idle };
    }).sort((a, b) => b.longhorn - a.longhorn);

    // Volume Longhorn sans claim, ou claim d'une classe inconnue.
    const known = new Set(classes.map(c => c.name));
    const stray = volumes.filter(v => !v.storage_class || !known.has(v.storage_class));
    // Lecteurs CD-ROM sans média : pas de volume, mais une vraie ligne
    // d'inventaire (un ISO retiré après installation).
    const emptyCd = [];
    (d.vms || []).forEach(vm => (vm.disks || []).forEach(k => {
      if (k.device === 'cdrom' && !k.pvc) emptyCd.push({ vm: vm.namespace + '/' + vm.name, disk: k.disk });
    }));
    const orphans = volumes.filter(v => v.orphan);
    return { blocks, stray, emptyCd, orphans };
  }

  // -------------------------------------------------------------------------
  // Rendu
  // -------------------------------------------------------------------------
  function volRow(v, opts = {}) {
    const k = volKey(v);
    const isSel = selected && selected.type === 'vol' && selected.key === k;
    const state = v.state || '-';
    const cd = v.device === 'cdrom' || v.image_iso;
    const label = opts.byDisk && v.disk ? v.disk : (v.pvc_name || v.longhorn);
    // La bulle porte le nom COMPLET : la ligne le tronque, et deux
    // restaurations d'une même VM ne diffèrent qu'à la fin.
    const full = v.pvc_name ? v.pvc_namespace + '/' + v.pvc_name : v.longhorn;
    const tip = `${full} · ${tr('storage.volTip', 'click for the detail')}`;
    return `<div class="sto-vol ${esc(state)}${isSel ? ' selected' : ''}${v.orphan ? ' orphan' : ''} tip"
                 data-vol="${esc(k)}" data-tip="${esc(tip)}" aria-label="${esc(tip)}"
                 tabindex="0" role="button">
      <span class="vsw-dot" aria-hidden="true"></span>
      <span class="sto-vol-name">${esc(label)}${cd ? ` <small class="sto-cd">${esc(tr('storage.cdrom', 'CD-ROM'))}</small>` : ''}</span>
      ${v.boot_order ? `<small class="sto-boot">${esc(tr('storage.boot', 'boot'))} ${esc(v.boot_order)}</small>` : ''}
      <span class="sto-size">${esc(bytes(v.requested || v.size))}</span>
      <small class="sto-state">${esc(state)}</small>
    </div>`;
  }

  function classBox(c) {
    const g = c.groups;
    const reps = c.replicas == null ? null
      : (c.replicas === 1 ? tr('storage.replicaOne', '1 replica')
                          : c.replicas + ' ' + tr('storage.replicas', 'replicas'));
    const alloc = c.allocatable == null ? null
      : (c.allocatable > 0 ? bytes(c.allocatable)
         : `0 (${c.reason ? tr('storage.reasonNodes', 'not enough schedulable nodes')
                          : tr('storage.reasonFull', 'no room left')})`);
    return `<div class="vsw-pg vsw-link sto-class" data-class="${esc(c.name)}">
      <div class="vsw-pg-head">
        ${val(c.name, 'vsw-name')}
        ${c.default ? `<span class="vsw-badge ok">${esc(tr('storage.default', 'default'))}</span>` : ''}
        <small class="vsw-count">${c.count} ${esc(tr('storage.volumes', 'volumes'))}</small>
      </div>
      <div class="vsw-tags">
        ${reps ? `<span>${esc(reps)}</span>` : ''}
        ${c.reclaim_policy ? `<span>${esc(tr('storage.reclaim', 'on release'))} : ${esc(c.reclaim_policy)}</span>` : ''}
        ${c.image ? `<span class="sto-img">${esc(tr('storage.image', 'image'))} ${esc(c.image)}</span>` : ''}
      </div>
      ${alloc != null ? `<div class="vsw-kv"><span>${esc(tr('storage.allocatable', 'Allocatable'))}</span>
        <span class="${c.allocatable > 0 ? '' : 'warn'}">${esc(alloc)}</span></div>` : ''}
      ${g.vms.map(x => `<div class="sto-group">
          <div class="sto-group-head ${x.vm.status === 'Running' ? 'running' : 'stopped'}">
            <span class="vsw-dot" aria-hidden="true"></span>${val(x.id)}
            <small>${esc(x.vm.status || '')}</small></div>
          ${x.vols.map(v => volRow(v, { byDisk: true })).join('')}
        </div>`).join('')}
      ${g.pods.length ? `<div class="sto-group">
          <div class="sto-group-head pods">${esc(tr('storage.byPods', 'Mounted by pods'))}</div>
          ${g.pods.map(v => volRow(v) + `<div class="sto-sub">${v.pods.map(p => esc(p.name)).join(', ')}</div>`).join('')}
        </div>` : ''}
      ${g.orphans.length ? `<div class="sto-group">
          <div class="sto-group-head orphan">${esc(tr('storage.orphans', 'Claimed by no VM, mounted by no pod'))}</div>
          ${g.orphans.map(v => volRow(v)).join('')}
        </div>` : ''}
      ${g.unknown.length ? `<div class="sto-group">
          <div class="sto-group-head">${esc(tr('storage.unknownUse', 'Consumer unknown'))}</div>
          ${g.unknown.map(v => volRow(v)).join('')}
        </div>` : ''}
    </div>`;
  }

  function diskBox(dk) {
    const k = dk.node + '/' + dk.disk;
    const isSel = selected && selected.type === 'disk' && selected.key === k;
    const max = dk.maximum || 0;
    const pct = (x) => max ? Math.min(100, Math.round(100 * x / max)) : 0;
    return `<div class="vsw-nic vsw-link sto-disk${dk.schedulable ? '' : ' down'}${isSel ? ' selected' : ''} tip"
                 data-disk="${esc(k)}" data-tip-i18n="storage.diskTip" tabindex="0" role="button">
      <div class="sto-disk-head">
        <span class="vsw-dot" aria-hidden="true"></span>
        ${val(dk.path || dk.disk, 'vsw-name')}
        ${dk.schedulable ? '' : `<span class="vsw-badge warn">${esc(tr('storage.unschedulable', 'not schedulable'))}</span>`}
      </div>
      <div class="sto-bar" role="img"
           aria-label="${esc(bytes(dk.used))} / ${esc(bytes(max))}">
        <span class="sto-bar-used" style="width:${pct(dk.used)}%"></span>
        <span class="sto-bar-sched" style="left:${pct(dk.scheduled)}%"></span>
      </div>
      <div class="sto-disk-facts">
        <span>${esc(tr('storage.used', 'used'))} ${esc(bytes(dk.used))} / ${esc(bytes(max))}</span>
        <span>${esc(tr('storage.promised', 'promised'))} ${esc(bytes(dk.scheduled))}</span>
        <span>${esc(tr('storage.roomShort', 'allocatable'))} ${esc(bytes(dk.room))}</span>
        <span>${dk.replicas} ${esc(tr('storage.replicasOn', 'replicas'))}</span>
      </div>
    </div>`;
  }

  function blockHtml(b, d) {
    const idle = b.idle.length ? `<div class="sto-idle">
        <button type="button" class="vsw-more tip" data-sto-idle
                data-tip-i18n="storage.idleTip">${showIdle ? esc(tr('fabric.vmsLess', 'show less'))
          : b.idle.length + ' ' + esc(tr('storage.idleClasses', 'classes hold no volume'))}</button>
        ${showIdle ? `<div class="vsw-tags">${b.idle.map(c => `<span>${esc(c.name)}${c.image ? ' · ' + esc(c.image) : ''}</span>`).join('')}</div>` : ''}
      </div>` : '';
    const disks = b.longhorn ? (d.disks || []) : [];
    const nodes = [...new Set(disks.map(x => x.node))];
    const right = b.longhorn
      // Le nom du nœud toujours : le chemin seul d'un disque ne dit pas
      // sur quelle machine il se trouve.
      ? nodes.map(n => `<div class="vsw-nodename">${val(n)}</div>
          ${disks.filter(x => x.node === n).map(diskBox).join('')}`).join('')
        || `<div class="vsw-empty warn">${esc(tr('storage.noDisk', 'no Longhorn disk reported'))}</div>`
      : `<div class="vsw-internal">${esc(tr('storage.external', 'Managed outside Longhorn: its backing is not visible from here.'))}</div>`;
    return `<section class="vsw vsw-storage" data-block="${esc(b.provisioner)}">
      <header class="vsw-head">
        <span class="vsw-kind">${esc(tr('storage.backend', 'Storage backend'))}</span>
        <b class="vsw-title">${esc(b.longhorn ? 'Longhorn' : b.provisioner)}</b>
        <span class="vsw-sub">${esc(b.provisioner)}</span>
        ${b.longhorn ? `<span class="vsw-sub">${esc(tr('storage.overProv', 'over-provisioning'))} ${esc(d.over_provisioning_pct)} %</span>
          <span class="vsw-sub">${esc(tr('storage.minAvail', 'minimal free'))} ${esc(d.minimal_available_pct)} %</span>` : ''}
      </header>
      <div class="vsw-body">
        <div class="vsw-col vsw-left">
          <div class="vsw-col-title">${esc(tr('storage.classes', 'Storage classes'))}</div>
          ${b.classes.map(classBox).join('')
            || `<div class="vsw-empty">${esc(tr('storage.noVolume', 'no volume'))}</div>`}
          ${idle}
        </div>
        <div class="vsw-spine" aria-hidden="true"></div>
        <div class="vsw-col vsw-right">
          <div class="vsw-col-title">${esc(tr('storage.nodeDisks', 'Node disks'))}</div>
          ${right}
        </div>
      </div>
    </section>`;
  }

  function render(d) {
    const body = host && host.querySelector('.fabric-body');
    if (!body) return;
    const m = buildModel(d);
    const stray = m.stray.length ? `<section class="vsw vsw-unused">
        <header class="vsw-head"><span class="vsw-kind">${esc(tr('storage.stray', 'Volumes outside any known class'))}</span></header>
        <div class="vsw-unused-list sto-list">${m.stray.map(v => volRow(v)).join('')}</div></section>` : '';
    const cds = m.emptyCd.length ? `<section class="vsw vsw-unused">
        <header class="vsw-head"><span class="vsw-kind">${esc(tr('storage.emptyCd', 'Empty CD-ROM drives'))}</span></header>
        <div class="vsw-unused-list">${m.emptyCd.map(c => `<div class="vsw-pg">${val(c.vm)} <small>${esc(c.disk)}</small></div>`).join('')}</div></section>` : '';
    const scroll = body.scrollTop;
    body.innerHTML = m.blocks.map(b => blockHtml(b, d)).join('') + stray + cds
      || `<p class="hint">${esc(tr('storage.none', 'No storage class on this cluster.'))}</p>`;
    body.scrollTop = scroll;
    applyTips(body);
    const meta = host.querySelector('.fabric-meta');
    if (meta) {
      meta.textContent = `${(d.volumes || []).length} ${tr('storage.volumes', 'volumes')} · `
        + `${m.orphans.length} ${tr('storage.orphanCount', 'orphaned')}`;
    }
  }

  // -------------------------------------------------------------------------
  // Détail
  // -------------------------------------------------------------------------
  function showVol(k) {
    const side = host.querySelector('.fabric-detail');
    const v = (lastData.volumes || []).find(x => volKey(x) === k);
    if (!side || !v) return;
    selected = { type: 'vol', key: k };
    host.querySelectorAll('[data-vol]').forEach(el =>
      el.classList.toggle('selected', el.dataset.vol === k));
    const reps = (v.replicas || []).map(r => `${r.node}${r.disk ? ' / ' + r.disk : ''}`
      + (r.running ? '' : ' (' + tr('storage.stopped', 'stopped') + ')')).join(', ');
    const last = (v.last_pods || []).map(p => `${p.workload || p.name} (${p.kind || 'pod'}, ${p.at})`).join(', ');
    const del = v.orphan ? (unlocked
      ? `<button type="button" class="btn btn-sm btn-danger tip" data-sto-delete="${esc(k)}"
                 data-tip-i18n="storage.deleteTip">${window.Icons ? Icons.svg('delete') : ''} ${esc(tr('storage.delete', 'Delete this volume'))}</button>`
      : `<p class="form-hint">${esc(tr('storage.lockedHint', 'Unlock destructive actions in the toolbar to delete this orphaned volume.'))}</p>`) : '';
    side.innerHTML = `<h3>${esc(v.pvc_name || v.longhorn)}</h3>`
      + (last ? `<p class="hint warn">${esc(tr('storage.lastUsed', 'Last used by'))} ${esc(last)}. ${esc(tr('storage.lastUsedNote', 'That workload may come back and expect its data.'))}</p>` : '')
      + `<dl class="kv">`
      + kv('PVC', v.pvc_name ? v.pvc_namespace + '/' + v.pvc_name : null)
      + kv(tr('storage.class', 'Storage class'), v.storage_class)
      + kv('VM', v.vm)
      + kv(tr('storage.disk', 'Disk'), v.disk ? `${v.disk} (${v.device || 'disk'})` : null)
      + kv(tr('storage.pods', 'Pods'), (v.pods || []).map(p => p.name).join(', ') || null)
      + kv(tr('storage.image', 'image'), v.image)
      + kv(tr('storage.requested', 'Requested'), bytes(v.requested))
      + kv(tr('storage.actual', 'Actually written'), bytes(v.actual_size))
      + kv(tr('fabric.d.state', 'State'), v.state)
      + kv(tr('storage.health', 'Health'), v.robustness)
      + kv(tr('storage.attachedTo', 'Attached to'), v.attached_to)
      + kv(tr('storage.replicasWhere', 'Replicas'), reps || null)
      + kv('Longhorn', v.longhorn)
      + `</dl>${del}<div class="sto-delete-out"></div>`;
    applyTips(side);
  }

  function showDisk(k) {
    const side = host.querySelector('.fabric-detail');
    const dk = (lastData.disks || []).find(x => x.node + '/' + x.disk === k);
    if (!side || !dk) return;
    selected = { type: 'disk', key: k };
    host.querySelectorAll('[data-disk]').forEach(el =>
      el.classList.toggle('selected', el.dataset.disk === k));
    side.innerHTML = `<h3>${esc(dk.path || dk.disk)}</h3><dl class="kv">`
      + kv(tr('fabric.d.node', 'Node'), dk.node)
      + kv(tr('storage.disk', 'Disk'), dk.disk)
      + kv(tr('storage.capacity', 'Capacity'), bytes(dk.maximum))
      + kv(tr('storage.used', 'used'), bytes(dk.used))
      + kv(tr('storage.available', 'Available'), bytes(dk.available))
      + kv(tr('storage.promised', 'promised'), bytes(dk.scheduled))
      + kv(tr('storage.reserved', 'Reserved'), bytes(dk.reserved))
      + kv(tr('storage.room', 'Room left to allocate'), bytes(dk.room))
      + kv(tr('storage.limitedBy', 'Limited by'), dk.limited_by === 'over-provisioning'
          ? tr('storage.overProv', 'over-provisioning') : tr('storage.freeSpace', 'free space'))
      + kv(tr('storage.replicasOn', 'replicas'), dk.replicas)
      + kv(tr('storage.schedulable', 'Schedulable'), dk.schedulable ? tr('storage.yes', 'yes') : tr('storage.no', 'no'))
      + `</dl>`;
  }

  async function deleteVol(k, btn) {
    const v = (lastData.volumes || []).find(x => volKey(x) === k);
    if (!v || !v.orphan || !unlocked) return;
    const claim = v.pvc_namespace + '/' + v.pvc_name;
    const msg = tr('storage.confirmDelete', 'Delete the volume {name}? Its data will be lost.')
      .replace('{name}', claim);
    if (!window.confirm(msg)) return;
    btn.disabled = true;
    const out = host.querySelector('.sto-delete-out');
    try {
      const r = await fetch(`/api/pvc/${encodeURIComponent(cluster)}/${encodeURIComponent(v.pvc_namespace)}`
        + `/${encodeURIComponent(v.pvc_name)}`, { method: 'DELETE' });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || j.error || 'HTTP ' + r.status);
      if (out) out.textContent = tr('storage.deleting', 'Deletion requested, followed in the actions dock.');
      setTimeout(() => refresh(true), 2500);
    } catch (e) {
      btn.disabled = false;
      if (out) out.textContent = String(e.message || e);
    }
  }

  // -------------------------------------------------------------------------
  // Cycle de vie
  // -------------------------------------------------------------------------
  async function refresh(fresh) {
    if (!cluster || !host || document.hidden) return;
    const asked = cluster;
    try {
      const r = await fetch(`/api/storage-map/${encodeURIComponent(asked)}${fresh === true ? '?fresh=1' : ''}`);
      const d = await r.json();
      if (asked !== cluster) return;
      if (!r.ok || d.unreachable || d.error) {
        const b = host.querySelector('.fabric-body');
        if (b) b.innerHTML = `<p class="hint warn">${esc(d.unreachable
          ? tr('fabric.unreachable', 'Cluster unreachable')
          : (d.error || 'HTTP ' + r.status))}</p>`;
        return;
      }
      lastData = d;
      render(d);
      if (selected && selected.type === 'vol') showVol(selected.key);
      if (selected && selected.type === 'disk') showDisk(selected.key);
    } catch (e) {
      const b = host.querySelector('.fabric-body');
      if (b && !lastData) b.innerHTML = `<p class="hint warn">${esc(e.message || e)}</p>`;
    }
  }

  function shell() {
    const icon = window.Icons ? Icons.svg('refresh', { size: 14 }) : '';
    const lock = window.Icons ? Icons.svg('unlock', { size: 14 }) : '';
    host.innerHTML = `
      <div class="fabric-toolbar">
        <span class="fabric-meta"></span>
        <span class="fabric-tools">
          <label class="topology-unlock tip" data-tip-i18n="storage.unlockTip">
            <input type="checkbox" class="sto-unlock"> ${lock} ${esc(tr('topology.unlockDestructive', 'Unlock destructive actions'))}
          </label>
          <button type="button" class="btn btn-sm fabric-refresh tip"
                  data-tip-i18n="topology.refreshTip">${icon} ${esc(tr('topology.refresh', 'Refresh'))}</button>
        </span>
      </div>
      <div class="fabric-layout">
        <div class="fabric-body"></div>
        <aside class="fabric-detail"><p class="hint">${esc(tr('storage.detailHint',
          'Click a volume or a disk to see its detail.'))}</p></aside>
      </div>`;
    applyTips(host);
    if (window.CopyTo) CopyTo.wire(host);
    host.querySelector('.sto-unlock').addEventListener('change', (e) => {
      unlocked = e.target.checked;
      if (selected && selected.type === 'vol') showVol(selected.key);
    });
    host.addEventListener('click', (e) => {
      if (e.target.closest('[data-copy]')) return;
      if (e.target.closest('.fabric-refresh')) { refresh(true); return; }
      const del = e.target.closest('[data-sto-delete]');
      if (del) { deleteVol(del.dataset.stoDelete, del); return; }
      if (e.target.closest('[data-sto-idle]')) {
        showIdle = !showIdle;
        if (lastData) render(lastData);
        return;
      }
      const v = e.target.closest('[data-vol]');
      if (v) { showVol(v.dataset.vol); return; }
      const dk = e.target.closest('[data-disk]');
      if (dk) showDisk(dk.dataset.disk);
    });
    host.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      const v = e.target.closest('[data-vol]');
      const dk = e.target.closest('[data-disk]');
      if (v) { e.preventDefault(); showVol(v.dataset.vol); }
      else if (dk) { e.preventDefault(); showDisk(dk.dataset.disk); }
    });
  }

  function start(clusterName) {
    const h = document.querySelector('.overview-subtab[data-subtab="storage"] .topology-host');
    if (!h) return Promise.resolve();
    if (cluster !== clusterName) { lastData = null; selected = null; }
    cluster = clusterName;
    if (host !== h || !h.querySelector('.fabric-body')) {
      host = h; unlocked = false; shell();
    }
    if (!lastData) {
      host.querySelector('.fabric-body').innerHTML =
        `<p class="hint">${esc(tr('topology.loading', 'Loading...'))}</p>`;
    }
    if (timer) clearInterval(timer);
    timer = setInterval(refresh, REFRESH_MS);
    return refresh();
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
  }

  return { start, stop, refresh, _buildModel: buildModel };
})();

if (typeof window !== 'undefined') window.StorageMap = StorageMap;
