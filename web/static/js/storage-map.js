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
    const ill = HEALTH_ISSUES.includes(v.health) ? ' health-' + v.health : '';
    return `<div class="sto-vol ${esc(state)}${isSel ? ' selected' : ''}${v.orphan ? ' orphan' : ''}${ill} tip"
                 data-vol="${esc(k)}" data-tip="${esc(tip)}" aria-label="${esc(tip)}"
                 tabindex="0" role="button">
      <span class="vsw-dot" aria-hidden="true"></span>
      <span class="sto-vol-name">${esc(label)}${cd ? ` <small class="sto-cd">${esc(tr('storage.cdrom', 'CD-ROM'))}</small>` : ''}</span>
      ${ill ? `<small class="sto-health-tag">${esc(healthLabel(v.health))}</small>` : ''}
      ${v.claim_missing ? `<small class="sto-claim-gone">${esc(tr('storage.claimGone', 'claim deleted'))}</small>` : ''}
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
    body.innerHTML = bannerHtml(d) + m.blocks.map(b => blockHtml(b, d)).join('') + stray + cds
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
  // Santé des volumes (v1.42.0)
  //
  // Le serveur rend des codes et des faits (volume_health.py) ; le texte est
  // écrit ici, dans la langue de l'exploitant. Chaque cause dit ce qu'on
  // observe, quoi faire, et propose la correction quand elle est sûre, avec
  // la commande kubectl équivalente pour qui préfère la ligne de commande.
  // -------------------------------------------------------------------------
  const HEALTH_ISSUES = ['faulted', 'degraded', 'at-risk'];
  const fill = (text, vars) => String(text).replace(/\{(\w+)\}/g,
    (m, k) => (vars && vars[k] != null ? vars[k] : m));

  function healthLabel(h) {
    if (h === 'faulted') return tr('health.faulted', 'faulted');
    if (h === 'degraded') return tr('health.degraded', 'degraded');
    return tr('health.atRisk', 'at risk');
  }

  function findingTitle(f) {
    switch (f.cause) {
      case 'faulted': return tr('health.t.faulted', 'No healthy replica left');
      case 'rebuild-disabled': return tr('health.t.rebuildDisabled', 'Rebuilding is switched off');
      case 'rebuilding': return tr('health.t.rebuilding', 'Rebuilding in progress');
      case 'rebuild-pending': return tr('health.t.rebuildPending', 'A new replica is being prepared');
      case 'replica-failed': return tr('health.t.replicaFailed', 'A replica failed');
      case 'not-enough-nodes': return f.severity === 'watch'
        ? tr('health.t.willDegrade', 'Will start degraded: not enough nodes')
        : fill(tr('health.t.notEnoughNodes', 'Not enough nodes for {wanted} replicas'), f.facts);
      case 'no-room': return tr('health.t.noRoom', 'No disk has room for a new replica');
      case 'node-unavailable': return tr('health.t.nodeUnavailable', 'A replica sits on a node or disk that is down');
      default: return tr('health.t.unexplained', 'Degraded, cause not identified');
    }
  }

  function findingFacts(f) {
    const x = f.facts || {};
    switch (f.cause) {
      case 'faulted': return fill(tr('health.f.faulted', '{failed} of {replicas} replica(s) failed.'), x);
      case 'rebuild-disabled': return `${x.setting} = ${x.value}`;
      case 'replica-failed': return fill(tr('health.f.replicaFailed',
        '{failed} failed, {healthy} healthy. Longhorn waits up to {wait} s to reuse a failed replica.'),
        { failed: (x.replicas || []).length, healthy: x.healthy, wait: x.wait_seconds });
      case 'not-enough-nodes': return fill(tr('health.f.notEnoughNodes',
        '{wanted} replicas wanted, {nodes} schedulable node(s): Longhorn keeps replicas on distinct nodes.'), x);
      case 'no-room': return fill(tr('health.f.noRoom', '{size} needed, {n} node(s) with room.'),
        { size: bytes(x.size), n: x.nodes_with_room });
      case 'node-unavailable': return (x.replicas || []).map(r => fill(r.why === 'disk'
        ? tr('health.f.diskDown', '{replica} on {node}: disk {disk} not ready')
        : tr('health.f.nodeDown', '{replica} on {node}: node not ready'), r)).join(' ; ');
      case 'unexplained': return fill(tr('health.f.unexplained',
        '{healthy} healthy replica(s) of {wanted}. Longhorn says: {reason} {message}'),
        { healthy: x.healthy, wanted: x.wanted, reason: x.reason || '', message: x.message || '' });
      default: return '';
    }
  }

  function findingAdvice(f) {
    switch (f.cause) {
      case 'faulted': return tr('health.a.faulted',
        'Do not delete anything. Bring back the node or disk that held the data: Longhorn recovers the volume when a replica returns.');
      case 'rebuild-disabled': return tr('health.a.rebuildDisabled',
        'A graceful shutdown switches it off and the startup switches it back on. Here it stayed off, so no degraded volume is repaired.');
      case 'rebuilding': return tr('health.a.rebuilding',
        'Nothing to do: the volume repairs itself and stays degraded until the copy is complete.');
      case 'rebuild-pending': return tr('health.a.rebuildPending',
        'Nothing to do: Longhorn is starting the new replica, and the copy begins within seconds.');
      case 'replica-failed': return tr('health.a.replicaFailed',
        'Rebuilding now starts a fresh copy immediately instead of waiting, at the cost of disk and network load.');
      case 'not-enough-nodes': return tr('health.a.notEnoughNodes',
        'Lower the replica count to what the cluster can hold, or add nodes. Letting replicas share a node would hide the warning without protecting against the loss of that node.');
      case 'no-room': return tr('health.a.noRoom',
        'Free space (the orphaned volumes of this view can be deleted), add a disk, or review the over-provisioning setting.');
      case 'node-unavailable': return tr('health.a.nodeUnavailable',
        'Bring the node or disk back and Longhorn resumes the replica. If the node is gone for good, remove it from Longhorn so the replica is rebuilt elsewhere.');
      default: return tr('health.a.unexplained', 'Check the volume in the Longhorn UI.');
    }
  }

  // La même commande que celle que le serveur exécutera.
  function fixCommand(fix, v) {
    const p = fix.params || {};
    if (fix.kind === 'set-replicas') {
      return `kubectl -n longhorn-system patch volumes.longhorn.io ${v.longhorn} --type merge -p '{"spec":{"numberOfReplicas":${p.replicas}}}'`;
    }
    if (fix.kind === 'enable-rebuild') {
      return `kubectl -n longhorn-system patch settings.longhorn.io concurrent-replica-rebuild-per-node-limit --type merge -p '{"value":"${p.value}"}'`;
    }
    return `kubectl -n longhorn-system delete replicas.longhorn.io ${p.replica}`;
  }

  function fixLabel(fix) {
    if (fix.kind === 'set-replicas') {
      return fill(tr('health.fix.setReplicas', 'Set {replicas} replica(s)'), fix.params);
    }
    if (fix.kind === 'enable-rebuild') return tr('health.fix.enableRebuild', 'Switch rebuilding back on');
    return tr('health.fix.rebuildNow', 'Rebuild now');
  }

  function fixConfirm(fix, v) {
    if (fix.kind === 'set-replicas') {
      return fill(tr('health.confirm.setReplicas',
        'Lower {volume} to {replicas} replica(s)? Its data is kept, but it loses redundancy until the cluster has more nodes.'),
        { volume: v.pvc_name || v.longhorn, replicas: fix.params.replicas });
    }
    if (fix.kind === 'enable-rebuild') {
      return tr('health.confirm.enableRebuild',
        'Switch Longhorn rebuilding back on for the whole cluster? Every degraded volume starts repairing.');
    }
    return fill(tr('health.confirm.rebuildNow',
      'Delete the failed replica {replica} so that Longhorn rebuilds a fresh copy now? A healthy replica remains.'),
      fix.params);
  }

  function findingHtml(f, v) {
    const progress = f.cause === 'rebuilding'
      ? (f.facts.replicas || []).map(r => `<div class="sto-rebuild">
          <span>${esc(r.node || r.replica)}</span>
          <span class="sto-bar"><span class="sto-bar-used" style="width:${Math.max(0, Math.min(100, Number(r.progress) || 0))}%"></span></span>
          <span>${r.progress == null ? '?' : esc(r.progress) + ' %'}</span></div>`).join('')
      : '';
    const fix = f.fix
      ? `<button type="button" class="btn btn-sm tip" data-vol-fix="${esc(f.fix.kind)}"
                 data-tip-i18n="health.fixTip">${esc(fixLabel(f.fix))}</button>
         <details class="sto-kubectl"><summary>${esc(tr('health.command', 'Equivalent command'))}</summary>
           <span class="vsw-val"><code>${esc(fixCommand(f.fix, v))}</code>${window.CopyTo ? CopyTo.button(fixCommand(f.fix, v), { force: true }) : ''}</span>
         </details>`
      : '';
    const facts = findingFacts(f);
    return `<div class="sto-finding sev-${esc(f.severity)}" data-cause="${esc(f.cause)}">
      <div class="sto-finding-title">${esc(findingTitle(f))}</div>
      ${facts ? `<div class="sto-finding-facts">${esc(facts)}</div>` : ''}
      <div class="sto-finding-advice">${esc(findingAdvice(f))}</div>
      ${progress}${fix}
    </div>`;
  }

  function healthBoxHtml(v) {
    if (!(v.findings || []).length) return '';
    return `<section class="sto-health">
      <div class="sto-health-head">${esc(tr('health.title', 'Health'))} :
        <b>${esc(healthLabel(v.health))}</b></div>
      ${v.findings.map(f => findingHtml(f, v)).join('')}
      <div class="sto-fix-out"></div>
    </section>`;
  }

  function bannerHtml(d) {
    const s = d.health_summary || {};
    const parts = [];
    if (s.faulted) parts.push(fill(tr('health.banner.faulted', '{n} faulted'), { n: s.faulted }));
    if (s.degraded) parts.push(fill(tr('health.banner.degraded', '{n} degraded'), { n: s.degraded }));
    if (s.at_risk) parts.push(fill(tr('health.banner.atRisk', '{n} at risk'), { n: s.at_risk }));
    if (!parts.length) return '';
    // Le premier volume à ouvrir : le plus grave d'abord.
    const vols = d.volumes || [];
    const first = ['faulted', 'degraded', 'at-risk']
      .map(h => vols.find(v => v.health === h)).find(Boolean);
    const top = s.top_cause
      ? ' · ' + tr('health.banner.cause', 'main cause') + ' : '
        + findingTitle({ cause: s.top_cause, severity: 'action',
                         facts: (first && (first.findings || []).find(f => f.cause === s.top_cause) || {}).facts || {} })
      : '';
    return `<div class="sto-health-banner ${s.faulted ? 'critical' : 'warn'} tip" role="button" tabindex="0"
                 data-health-first="${first ? esc(volKey(first)) : ''}" data-tip-i18n="health.bannerTip">
      <b>${esc(tr('health.banner.title', 'Volumes need attention'))}</b>
      <span>${esc(parts.join(', '))}${esc(top)}</span>
    </div>`;
  }

  async function applyFix(kind, btn) {
    const v = (lastData.volumes || []).find(x => selected && volKey(x) === selected.key);
    const f = v && (v.findings || []).find(x => x.fix && x.fix.kind === kind);
    if (!f) return;
    if (!window.confirm(fixConfirm(f.fix, v))) return;
    btn.disabled = true;
    const out = host.querySelector('.sto-fix-out');
    try {
      const r = await fetch(`/api/volume-health/${encodeURIComponent(cluster)}/${encodeURIComponent(v.longhorn)}/fix`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ kind }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || j.error || 'HTTP ' + r.status);
      if (out) out.textContent = fill(tr('health.fixStarted',
        'Requested ({summary}); the action checks the result for a minute, in the actions dock.'),
        { summary: j.summary || kind });
      setTimeout(() => refresh(true), 3000);
    } catch (e) {
      btn.disabled = false;
      if (out) out.textContent = String(e.message || e);
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
      + healthBoxHtml(v)
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
      const fixBtn = e.target.closest('[data-vol-fix]');
      if (fixBtn) { applyFix(fixBtn.dataset.volFix, fixBtn); return; }
      const banner = e.target.closest('[data-health-first]');
      if (banner && banner.dataset.healthFirst) { showVol(banner.dataset.healthFirst); return; }
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
      const banner = e.target.closest('[data-health-first]');
      if (banner && banner.dataset.healthFirst) {
        e.preventDefault(); showVol(banner.dataset.healthFirst); return;
      }
      const v = e.target.closest('[data-vol]');
      const dk = e.target.closest('[data-disk]');
      if (v) { e.preventDefault(); showVol(v.dataset.vol); }
      else if (dk) { e.preventDefault(); showDisk(dk.dataset.disk); }
    });
  }

  function start(clusterName) {
    const h = document.querySelector('[data-board="storage"] .topology-host');
    if (!h) return Promise.resolve();
    if (cluster !== clusterName) { lastData = null; selected = null; }
    cluster = clusterName;
    if (host !== h || !h.querySelector('.fabric-body')) {
      host = h; unlocked = false; shell();
    }
    if (!lastData) {
      host.querySelector('.fabric-body').innerHTML =
        `<p class="hint">${esc(tr('topology.loading', 'Loading...', { name: cluster }))}</p>`;
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
