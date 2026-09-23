/**
 * harvester-ops : la vue Cluster, un bloc par hôte (v1.43.0).
 *
 * Remplace le dernier graphe Cytoscape de la console. Chaque hôte dit ce
 * qu'il peut donner et ce qu'il a déjà donné (vCPU, mémoire) ; chaque VM est
 * une carte qui dit ce qu'elle consomme : vCPU, mémoire, disques, réseaux,
 * adresse. Ce qui ne tient pas sur la carte s'affiche en calque au survol.
 *
 * Au clic, les mêmes actions qu'avant sur une VM, et sur un hôte : isoler,
 * réintégrer, et le mode maintenance de Harvester, dont le contrôle
 * préalable est affiché AVANT de confirmer (ce qui migrera, ce qui ne le
 * pourra pas et pourquoi, ce qui s'arrêtera).
 */
const ClusterMap = (() => {
  const REFRESH_MS = 8000;
  const { tr, esc, val, applyTips, bytes, kv } = window.Board;
  const fill = (text, vars) => String(text).replace(/\{(\w+)\}/g,
    (m, k) => (vars && vars[k] != null ? vars[k] : m));
  let cluster = null;
  let host = null;
  let timer = null;
  let lastData = null;
  let unlocked = false;
  let filter = '';
  let selected = null;               // {type: 'vm'|'node', key}
  let pop = null;                    // calque de détail au survol

  // « 4 GiB » plutôt que « 4.0 GiB » : sur une carte, les tailles rondes
  // sont la règle (mémoire et disques se déclarent en Gi entiers).
  const size = (n) => bytes(n).replace(/\.0 /, ' ');
  const vmKey = (v) => v.namespace + '/' + v.name;
  const running = (v) => v.phase === 'Running';

  // -------------------------------------------------------------------------
  // Textes courts d'une carte
  // -------------------------------------------------------------------------
  function sizing(v) {
    const parts = [];
    if (v.vcpu) parts.push(`${v.vcpu} vCPU`);
    if (v.memory) parts.push(size(v.memory));
    return parts.join(' · ');
  }

  function disksLine(v) {
    const real = (v.disks || []).filter(d => d.source === 'pvc');
    if (!real.length) return tr('cluster.noDisk', 'no disk');
    if (real.length === 1) return size(real[0].size);
    return fill(tr('cluster.disksN', '{n} disks · {size}'),
      { n: real.length, size: size(v.disk_total) });
  }

  function netsLine(v) {
    const names = (v.nics || []).map(n => n.pod ? tr('cluster.pod', 'pod network')
      : (n.network || '').replace(/^default\//, ''));
    return names.join(', ') || tr('cluster.noNetwork', 'no network');
  }

  const firstIp = (v) => ((v.nics || []).find(n => (n.ips || []).length) || {}).ips?.[0];

  function matches(v) {
    if (!filter) return true;
    const hay = [v.name, v.namespace, v.node, v.guest_os,
      ...(v.nics || []).flatMap(n => [n.network, n.mac, ...(n.ips || [])])]
      .filter(Boolean).join(' ').toLowerCase();
    return hay.includes(filter);
  }

  // -------------------------------------------------------------------------
  // Rendu
  // -------------------------------------------------------------------------
  function gauge(label, used, total, fmt) {
    const pct = total ? Math.round(100 * used / total) : 0;
    const over = pct > 100;
    return `<div class="cm-gauge${over ? ' over' : ''} tip" data-tip-i18n="cluster.gaugeTip">
      <span class="cm-gauge-label">${esc(label)}</span>
      <span class="sto-bar"><span class="sto-bar-used" style="width:${Math.min(100, pct)}%"></span></span>
      <span class="cm-gauge-figs">${esc(fmt(used))} / ${esc(fmt(total))} (${pct} %)${over
        ? ' · ' + esc(tr('cluster.overcommit', 'overcommitted')) : ''}</span>
    </div>`;
  }

  function vmCard(v) {
    const k = vmKey(v);
    const isSel = selected && selected.type === 'vm' && selected.key === k;
    const ip = firstIp(v);
    return `<div class="cm-vm ${running(v) ? 'running' : 'stopped'}${isSel ? ' selected' : ''}"
                 data-vm="${esc(k)}" tabindex="0" role="button"
                 aria-label="${esc(k + ' · ' + tr('cluster.cardTip', 'hover for the detail, click for the actions'))}">
      <div class="cm-vm-head"><span class="vsw-dot" aria-hidden="true"></span>
        <b class="cm-vm-name">${esc(v.name)}</b>
        ${v.namespace !== 'default' ? `<small>${esc(v.namespace)}</small>` : ''}</div>
      <div class="cm-vm-line">${esc(sizing(v) || '-')}</div>
      <div class="cm-vm-line">${esc(disksLine(v))}</div>
      <div class="cm-vm-line cm-dim">${esc(netsLine(v))}</div>
      ${ip ? `<div class="cm-vm-line cm-ip">${esc(ip)}</div>` : ''}
    </div>`;
  }

  function hostState(n) {
    const bits = [];
    bits.push(n.ready ? tr('cluster.ready', 'ready') : tr('cluster.notReady', 'not ready'));
    if (n.maintenance === 'completed') bits.push(tr('cluster.maint.completed', 'in maintenance'));
    else if (n.maintenance) bits.push(tr('cluster.maint.running', 'entering maintenance'));
    else if (!n.schedulable) bits.push(tr('cluster.cordoned', 'cordoned'));
    return bits;
  }

  function hostBlock(n, vms) {
    const shown = vms.filter(matches);
    const isSel = selected && selected.type === 'node' && selected.key === n.name;
    const warn = !n.ready || !n.schedulable || n.maintenance;
    return `<section class="vsw cm-host${warn ? ' warn' : ''}" data-host="${esc(n.name)}">
      <header class="vsw-head cm-host-head${isSel ? ' selected' : ''}" data-node="${esc(n.name)}"
              tabindex="0" role="button" aria-label="${esc(n.name)}">
        <span class="vsw-kind">${esc(tr('cluster.host', 'Host'))}</span>
        ${val(n.name, 'vsw-title')}
        ${hostState(n).map(b => `<span class="vsw-sub">${esc(b)}</span>`).join('')}
        ${(n.roles || []).length ? `<span class="vsw-sub">${esc(n.roles.join(', '))}</span>` : ''}
        ${n.addresses && n.addresses.InternalIP ? `<span class="vsw-sub">${val(n.addresses.InternalIP)}</span>` : ''}
        <span class="vsw-sub vsw-ports">${esc(fill(tr('cluster.shown', '{shown} of {total} VM'),
          { shown: shown.length, total: vms.length }))}</span>
      </header>
      <div class="cm-gauges">
        ${gauge('vCPU', n.vcpu_allocated || 0, n.cpu_allocatable || 0, x => String(Math.round(x * 100) / 100))}
        ${gauge(tr('cluster.memory', 'Memory'), n.memory_allocated || 0, n.memory_allocatable || 0, size)}
      </div>
      <div class="cm-grid">${shown.map(vmCard).join('')
        || `<div class="vsw-empty">${esc(vms.length ? tr('cluster.noMatch', 'no VM matches the filter')
                                                    : tr('cluster.noVm', 'no VM on this host'))}</div>`}</div>
    </section>`;
  }

  function render(d) {
    const body = host && host.querySelector('.fabric-body');
    if (!body) return;
    const nodes = [...(d.nodes || [])].sort((a, b) => a.name.localeCompare(b.name));
    const vms = [...(d.vms || [])].sort((a, b) => ((running(b) - running(a)) || a.name.localeCompare(b.name)));
    const onHost = (n) => vms.filter(v => running(v) && v.node === n.name);
    const idle = vms.filter(v => !running(v) || !nodes.some(n => n.name === v.node));
    const idleShown = idle.filter(matches);
    const scroll = body.scrollTop;
    body.innerHTML = nodes.map(n => hostBlock(n, onHost(n))).join('')
      + (idle.length ? `<section class="vsw cm-host cm-idle">
          <header class="vsw-head"><span class="vsw-kind">${esc(tr('cluster.stopped', 'Stopped / unscheduled'))}</span>
            <span class="vsw-sub vsw-ports">${esc(fill(tr('cluster.shown', '{shown} of {total} VM'),
              { shown: idleShown.length, total: idle.length }))}</span></header>
          <div class="cm-grid">${idleShown.map(vmCard).join('')
            || `<div class="vsw-empty">${esc(tr('cluster.noMatch', 'no VM matches the filter'))}</div>`}</div>
        </section>` : '');
    body.scrollTop = scroll;
    applyTips(body);
    const meta = host.querySelector('.fabric-meta');
    if (meta) {
      meta.textContent = `${nodes.length} ${tr('cluster.hosts', 'host(s)')} · `
        + `${vms.filter(running).length}/${vms.length} VM`;
    }
  }

  // -------------------------------------------------------------------------
  // Le calque de détail, au survol ou au focus clavier
  // -------------------------------------------------------------------------
  function popHtml(v) {
    const disks = (v.disks || []).map(d => {
      // Un lecteur CD-ROM sans volume est un lecteur VIDE (ISO retiré après
      // l'installation), pas une inconnue.
      const what = d.source === 'cloudinit' ? 'cloud-init'
        : d.source === 'pvc' ? `${size(d.size)}${d.storage_class ? ' · ' + d.storage_class : ''}`
        : d.source ? d.source
        : d.device === 'cdrom' ? tr('cluster.cdEmpty', 'empty, no medium')
        : tr('cluster.noVolume', 'no volume behind this disk');
      return `<li><b>${esc(d.disk)}</b> ${d.device === 'cdrom' ? '<small>CD-ROM</small>' : ''}
        ${d.boot_order ? `<small>${esc(fill(tr('cluster.bootN', 'boot {n}'), { n: d.boot_order }))}</small>` : ''}
        <span>${esc(what)}</span>${d.pvc ? `<small class="cm-dim">${esc(d.pvc)}</small>` : ''}</li>`;
    }).join('');
    const nics = (v.nics || []).map(n => `<li><b>${esc(n.nic)}</b>
        <span>${esc(n.pod ? tr('cluster.pod', 'pod network') : n.network || '?')}</span>
        ${n.mac ? `<small>${esc(n.mac)}</small>` : ''}
        ${(n.ips || []).map(ip => `<small>${esc(ip)}</small>`).join('')}
        ${n.guest_iface ? `<small class="cm-dim">${esc(n.guest_iface)}</small>` : ''}</li>`).join('');
    const guest = (v.guest_only || []).filter(g => (g.ips || []).length)
      .map(g => `${g.iface || '?'} ${(g.ips || []).join(' ')}`).join(' · ');
    return `<div class="cm-pop-head"><span class="vsw-dot ${running(v) ? 'on' : ''}"></span>
        <b>${esc(vmKey(v))}</b> <small>${esc(v.phase)}</small></div>
      <dl class="kv">
        <dt>vCPU</dt><dd>${esc(v.vcpu || '-')}</dd>
        <dt>${esc(tr('cluster.memory', 'Memory'))}</dt><dd>${esc(v.memory ? size(v.memory) : '-')}</dd>
        <dt>${esc(tr('topology.detail.node', 'Hosted on'))}</dt><dd>${esc(v.node || '-')}</dd>
        <dt>${esc(tr('topology.detail.runStrategy', 'Run strategy'))}</dt><dd>${esc(v.run_strategy || '-')}</dd>
        ${v.guest_os ? `<dt>${esc(tr('cluster.os', 'Guest OS'))}</dt><dd>${esc(v.guest_os)}</dd>` : ''}
      </dl>
      <div class="cm-pop-title">${esc(tr('cluster.disksTitle', 'Disks'))}</div>
      <ul class="cm-pop-list">${disks || `<li class="cm-dim">-</li>`}</ul>
      <div class="cm-pop-title">${esc(tr('cluster.nics', 'Network cards'))}</div>
      <ul class="cm-pop-list">${nics || `<li class="cm-dim">-</li>`}</ul>
      ${guest ? `<div class="cm-dim cm-pop-guest">${esc(tr('netmap.guestOnly', 'Also inside the guest'))} : ${esc(guest)}</div>` : ''}`;
  }

  function showPop(card) {
    const v = (lastData && lastData.vms || []).find(x => vmKey(x) === card.dataset.vm);
    if (!v) return;
    if (!pop) {
      pop = document.createElement('div');
      pop.className = 'cm-pop';
      pop.setAttribute('role', 'tooltip');
      document.body.appendChild(pop);
    }
    pop.innerHTML = popHtml(v);
    pop.hidden = false;
    // À droite de la carte s'il y a la place, sinon à gauche ; jamais hors
    // de la fenêtre, ni sur le panneau de détail, qui doit rester lisible.
    const r = card.getBoundingClientRect();
    const w = pop.offsetWidth, h = pop.offsetHeight;
    const body = host.querySelector('.fabric-body');
    const limit = Math.min(window.innerWidth - 8,
      body ? body.getBoundingClientRect().right : window.innerWidth);
    let left = r.right + 8;
    if (left + w > limit) left = Math.max(8, r.left - w - 8);
    const top = Math.max(8, Math.min(r.top, window.innerHeight - h - 8));
    pop.style.left = left + 'px';
    pop.style.top = top + 'px';
  }

  function hidePop() { if (pop) pop.hidden = true; }

  // -------------------------------------------------------------------------
  // Panneaux d'actions
  // -------------------------------------------------------------------------
  // `tip` est déjà traduit, par un appel `tr(...)` à clé littérale, que le
  // contrôle de parité des traductions sait voir.
  function btn(act, label, tip, cls = '', icon = '', attrs = '') {
    const svg = icon && window.Icons ? Icons.svg(icon) + ' ' : '';
    return `<button type="button" class="btn btn-sm tip ${cls}" data-cm-act="${esc(act)}"
                    data-tip="${esc(tip)}" ${attrs}>${svg}${esc(label)}</button>`;
  }

  function showVm(k) {
    const side = host.querySelector('.fabric-detail');
    const v = (lastData.vms || []).find(x => vmKey(x) === k);
    if (!side || !v) return;
    selected = { type: 'vm', key: k };
    markSelected();
    side.innerHTML = popHtml(v) + `<div class="actions cm-actions">`
      + btn('vm-notes', tr('topology.action.notes', 'Notes'), tr('cluster.notesTip', 'Private notes attached to this object'), '', 'notes')
      + btn('vm-edit', tr('topology.action.edit', 'Edit'), tr('cluster.editTip', 'Edit the VM definition'), '', 'edit')
      + btn('vm-console', tr('topology.action.console', 'Console'), tr('cluster.consoleTip', 'Open the VM console'), '', 'console')
      + btn('vm-snap', tr('topology.action.snapshot', 'Snapshot'), tr('cluster.snapshotTip', 'Snapshots of this VM'), '', 'snapshot')
      + btn('vm-migrate', tr('topology.action.migrate', 'Migrate'), tr('cluster.migrateTip', 'Live-migrate the VM to another host'), '', 'migrate')
      + (v.run_strategy === 'Halted'
        ? btn('vm-start', tr('topology.action.start', 'Start'), tr('cluster.startTip', 'Start the VM (you are asked to confirm)'), '', 'play')
        : btn('vm-stop', tr('topology.action.stop', 'Stop'), tr('cluster.stopTip', 'Stop the VM (you are asked to confirm)'), 'btn-warn', 'stop'))
      + (unlocked ? btn('vm-delete', tr('topology.action.delete', 'Delete'), tr('cluster.deleteTip', 'Delete the VM and its definition for good (you are asked to confirm)'), 'btn-danger', 'delete') : '')
      + `</div><div class="cm-out"></div>`;
    applyTips(side);
    if (window.CopyTo) CopyTo.wire(side);
  }

  function showNode(name) {
    const side = host.querySelector('.fabric-detail');
    const n = (lastData.nodes || []).find(x => x.name === name);
    if (!side || !n) return;
    selected = { type: 'node', key: name };
    markSelected();
    const inMaint = !!n.maintenance;
    side.innerHTML = `<h3>${esc(n.name)}</h3><dl class="kv">`
      + kv(tr('topology.detail.ready', 'Ready'), n.ready ? tr('storage.yes', 'yes') : tr('storage.no', 'no'))
      + kv(tr('topology.detail.schedulable', 'Schedulable'), n.schedulable ? tr('storage.yes', 'yes') : tr('storage.no', 'no'))
      + kv(tr('cluster.maintenance', 'Maintenance'), n.maintenance || '-')
      + kv(tr('topology.detail.roles', 'Roles'), (n.roles || []).join(', '))
      + kv(tr('topology.detail.ip', 'IP'), n.addresses && n.addresses.InternalIP)
      + kv('vCPU', `${n.vcpu_allocated} / ${n.cpu_allocatable}`)
      + kv(tr('cluster.memory', 'Memory'), `${size(n.memory_allocated)} / ${size(n.memory_allocatable)}`)
      + `</dl><div class="actions cm-actions">`
      + btn('node-notes', tr('topology.action.notes', 'Notes'), tr('cluster.notesTip', 'Private notes attached to this object'), '', 'notes')
      // Harvester refuse d'isoler le dernier nœud disponible (son webhook) :
      // le bouton reste visible, désactivé, et la raison est écrite.
      + (!inMaint && n.schedulable ? btn('node-cordon', tr('cluster.cordon', 'Cordon'),
          n.last_available
            ? tr('cluster.lastNodeHint', 'Harvester refuses to cordon, or put in maintenance, the last node still available: another node must be able to take the VMs.')
            : tr('cluster.cordonTip', 'No new VM or pod will be placed on this host; those already there keep running'),
          '', 'construction', n.last_available ? 'disabled' : '') : '')
      + (!inMaint && !n.schedulable ? btn('node-uncordon', tr('cluster.uncordon', 'Uncordon'), tr('cluster.uncordonTip', 'Make the host available again for new VMs and pods'), '', 'ok') : '')
      + (!inMaint ? btn('node-maint-check', tr('cluster.maintEnter', 'Enter maintenance...'), tr('cluster.maintEnterTip', 'First shows what maintenance would do; nothing changes before you confirm'), 'btn-warn', 'warn') : '')
      + (inMaint ? btn('node-maint-leave', tr('cluster.maintLeave', 'Leave maintenance'), tr('cluster.maintLeaveTip', 'Take the host out of maintenance and restart the VMs it shut down'), '', 'play') : '')
      + `</div>`
      + (!inMaint && n.schedulable && n.last_available
        ? `<p class="form-hint cm-last-node">${esc(tr('cluster.lastNodeHint', 'Harvester refuses to cordon, or put in maintenance, the last node still available: another node must be able to take the VMs.'))}</p>`
        : '')
      + `<div class="cm-maint"></div><div class="cm-out"></div>`;
    applyTips(side);
    if (window.CopyTo) CopyTo.wire(side);
  }

  // Le panneau n'est redessiné au rafraîchissement que si l'état de l'objet
  // choisi a changé : le redessiner toutes les 8 s effacerait le contrôle de
  // maintenance en cours de lecture et le compte rendu de la dernière action.
  function signature(s) {
    if (!s || !lastData) return null;
    if (s.type === 'vm') {
      const v = (lastData.vms || []).find(x => vmKey(x) === s.key);
      return v ? [v.phase, v.run_strategy, v.node].join('|') : 'gone';
    }
    const n = (lastData.nodes || []).find(x => x.name === s.key);
    return n ? [n.ready, n.schedulable, n.maintenance, n.last_available].join('|') : 'gone';
  }

  function refreshPanel(before) {
    if (!selected) return;
    const now = signature(selected);
    if (now === before) return;
    if (now === 'gone') {
      selected = null;
      const side = host.querySelector('.fabric-detail');
      if (side) side.innerHTML = `<p class="hint">${esc(tr('cluster.detailHint',
        'Click a VM or a host for its actions.'))}</p>`;
      return;
    }
    const keep = (host.querySelector('.fabric-detail .cm-out') || {}).textContent || '';
    if (selected.type === 'vm') showVm(selected.key); else showNode(selected.key);
    if (keep) out(keep);
  }

  function markSelected() {
    host.querySelectorAll('[data-vm]').forEach(el => el.classList.toggle('selected',
      selected && selected.type === 'vm' && el.dataset.vm === selected.key));
    host.querySelectorAll('.cm-host-head').forEach(el => el.classList.toggle('selected',
      selected && selected.type === 'node' && el.dataset.node === selected.key));
  }

  function out(text) {
    const o = host.querySelector('.fabric-detail .cm-out');
    if (o) o.textContent = text;
  }

  async function call(method, url, body) {
    const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' },
                                 body: body ? JSON.stringify(body) : undefined });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || j.hint || j.error || 'HTTP ' + r.status);
    return j;
  }

  // Le contrôle préalable de maintenance, affiché avant toute confirmation.
  const REFUSALS = {
    'single-control-plane': () => tr('cluster.refusal.single',
      'This node is the only control plane of the cluster: Harvester refuses to put it in maintenance.'),
    'control-plane-busy': () => tr('cluster.refusal.busy',
      'Another control plane node is already in maintenance: Harvester needs all three available.'),
    'already': () => tr('cluster.refusal.already', 'This node is already in maintenance.'),
    'last-available-node': () => tr('cluster.refusal.lastNode',
      'No other node is available (all are cordoned or in maintenance): Harvester refuses to put the last one in maintenance.'),
  };
  const REASONS = {
    LastHealthyReplica: () => tr('cluster.reason.lastReplica',
      'the last healthy replica of one of its volumes is on this node'),
    NodeSchedulingRequirementsNotMet: () => tr('cluster.reason.placement',
      'no other node satisfies its placement rules'),
    // Raisons de la condition LiveMigratable=False de KubeVirt ; une raison
    // inconnue est montrée telle quelle.
    DisksNotLiveMigratable: () => tr('cluster.reason.disks',
      'one of its disks does not allow live migration (access mode or local storage)'),
    InterfaceNotLiveMigratable: () => tr('cluster.reason.interface',
      'one of its network cards is bound in a way that cannot migrate'),
    HostDeviceNotLiveMigratable: () => tr('cluster.reason.hostDevice',
      'a host device (GPU, PCI) is passed through to it'),
  };

  async function maintenanceCheck(name, force) {
    const box = host.querySelector('.fabric-detail .cm-maint');
    if (!box) return;
    box.innerHTML = `<p class="hint">${esc(tr('common.loadingNamed', 'Loading...', { name }))}</p>`;
    let p;
    try {
      p = await call('GET', `/api/node/${encodeURIComponent(cluster)}/${encodeURIComponent(name)}`
        + `/maintenance-check${force ? '?force=1' : ''}`);
    } catch (e) { box.innerHTML = `<p class="hint warn">${esc(e.message)}</p>`; return; }
    const list = (items) => items.length
      ? `<ul class="cm-pop-list">${items.map(x => `<li>${esc(x)}</li>`).join('')}</ul>`
      : `<p class="cm-dim">-</p>`;
    const blocked = Object.entries(p.non_migratable || {}).map(([reason, vms]) =>
      `${vms.join(', ')} : ${(REASONS[reason] || (() => reason))()}`);
    const refusal = p.refusal ? (REFUSALS[p.refusal] || (() => p.refusal))() : null;
    const can = !refusal && (!p.blocked || force);
    box.innerHTML = `<section class="sto-health cm-maint-box">
        <div class="sto-health-head"><b>${esc(tr('cluster.maintCheckTitle', 'Maintenance: what would happen'))}</b></div>
        ${refusal ? `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(refusal)}</div></div>` : `
        <div class="cm-pop-title">${esc(tr('cluster.maintMigrate', 'Will migrate'))}</div>${list(p.migrate || [])}
        <div class="cm-pop-title">${esc(tr('cluster.maintStop', 'Will be shut down'))}</div>${list(p.will_stop || [])}
        ${p.force && (p.will_stop || []).length ? `<p class="form-hint">${esc(tr('cluster.maintStopStays',
          'Shut down by forcing, they stay stopped after the maintenance: restart them by hand.'))}</p>` : ''}
        ${(p.stuck_volumes || []).length ? `
        <div class="cm-pop-title warn">${esc(tr('cluster.maintStuck', 'The maintenance will not finish'))}</div>
        ${list(p.stuck_volumes.map(v => `${v.claim || v.volume}${(v.pods || []).length ? ' (' + v.pods.join(', ') + ')' : ''}`))}
        <p class="form-hint">${esc(tr('cluster.stuckHint',
          'These attached volumes have their only healthy replica on this node: Longhorn will not let it go, and the drain waits for ever. Add a replica elsewhere, or stop what uses them, before the maintenance.'))}</p>` : ''}
        ${(p.volume_waits || []).length ? `
        <div class="cm-pop-title warn">${esc(tr('cluster.maintVolumeWaits', 'Migration will wait for a volume'))}</div>
        ${list(p.volume_waits.map(w => `${w.vm} : ${w.volume}`))}
        <p class="form-hint">${esc(tr('cluster.volumeWaitsHint',
          'These volumes are not healthy: Longhorn does not migrate a volume while a replica waits to be rebuilt, and the maintenance can take much longer. Wait until they are healthy (Storage view).'))}</p>` : ''}
        ${(p.drain_stops || []).length ? `
        <div class="cm-pop-title warn">${esc(tr('cluster.maintDrainStops', 'Shut down by the drain'))}</div>
        ${list(p.drain_stops.map(d => `${d.vm} : ${d.restarts
          ? tr('cluster.drainRestarts', 'restarted on another node (not live-migrated)')
          : tr('cluster.drainStays', 'stays stopped')}`))}
        <p class="form-hint">${esc(tr('cluster.drainHint',
          'These VMs have no live-migration eviction strategy. To have them migrate, set Eviction strategy to LiveMigrateIfPossible in the VM editor (Lifecycle).'))}</p>` : ''}
        ${blocked.length ? `<div class="cm-pop-title warn">${esc(tr('cluster.maintBlocked', 'Cannot migrate'))}</div>${list(blocked)}` : ''}
        ${blocked.length ? `<label class="cm-force tip" data-tip-i18n="cluster.maintForceTip">
            <input type="checkbox" data-cm-force ${force ? 'checked' : ''} ${unlocked ? '' : 'disabled'}>
            ${esc(tr('cluster.maintForce', 'Shut down the VMs that cannot migrate'))}</label>
            ${unlocked ? '' : `<p class="form-hint">${esc(tr('topology.lockedHint', 'Enable "Allow destructive actions" in the toolbar to use this command.'))}</p>`}` : ''}`}
        ${btn('node-maint-enter', tr('cluster.maintConfirmBtn', 'Enter maintenance'), tr('cluster.maintConfirmTip', 'Ask Harvester to migrate the VMs away and put the host in maintenance'),
              'btn-warn', 'warn', `data-force="${force ? '1' : '0'}" ${can ? '' : 'disabled'}`)}
      </section>`;
    applyTips(box);
  }

  async function act(action, el) {
    const s = selected;
    if (!s) return;
    const base = (ns, n) => `/api/vm/${encodeURIComponent(cluster)}/${encodeURIComponent(ns)}/${encodeURIComponent(n)}`;
    const nodeBase = (n) => `/api/node/${encodeURIComponent(cluster)}/${encodeURIComponent(n)}`;
    try {
      if (s.type === 'vm') {
        const [ns, name] = s.key.split('/');
        if (action === 'vm-notes') return window.Notes?.open('vm', cluster, ns, name);
        if (action === 'vm-edit') return window.VMEdit?.open?.(cluster, ns, name);
        if (action === 'vm-snap') return window.VMSnapshots?.open?.(cluster, ns, name);
        if (action === 'vm-console') return window.VMConsole?.open?.(cluster, ns, name);
        if (action === 'vm-migrate') return window.VMMigrate?.open?.(cluster, ns, name);
        if (action === 'vm-start' || action === 'vm-stop') {
          const msg = action === 'vm-start'
            ? fill(tr('topology.confirm.vm-start', 'Start VM "{name}"?'), { name })
            : fill(tr('topology.confirm.vm-stop', 'Halt VM "{name}"? It will be force-stopped.'), { name });
          if (!window.confirm(msg)) return;
          await call('PATCH', base(ns, name) + '/runStrategy',
            { runStrategy: action === 'vm-start' ? 'Always' : 'Halted' });
        } else if (action === 'vm-delete') {
          if (!unlocked) { out(tr('topology.lockedHint', 'Enable "Allow destructive actions" in the toolbar to use this command.')); return; }
          if (!window.confirm(fill(tr('topology.confirm.vm-delete', 'DELETE VM "{name}"? This is permanent.'), { name }))) return;
          await call('DELETE', base(ns, name));
        }
      } else {
        const name = s.key;
        if (action === 'node-notes') return window.Notes?.open('node', cluster, name);
        if (action === 'node-maint-check') return maintenanceCheck(name, false);
        if (action === 'node-cordon') {
          if (!window.confirm(fill(tr('cluster.confirm.cordon', 'Cordon node "{name}"? No new VM or pod will be placed on it.'), { name }))) return;
          await call('POST', nodeBase(name) + '/cordon');
        } else if (action === 'node-uncordon') {
          if (!window.confirm(fill(tr('cluster.confirm.uncordon', 'Make node "{name}" schedulable again?'), { name }))) return;
          await call('POST', nodeBase(name) + '/uncordon');
        } else if (action === 'node-maint-enter') {
          const force = el && el.dataset.force === '1';
          if (!window.confirm(fill(force
            ? tr('cluster.confirm.maintForce', 'Put node "{name}" in maintenance, shutting down the VMs that cannot migrate?')
            : tr('cluster.confirm.maint', 'Put node "{name}" in maintenance? Its VMs migrate to the other nodes.'), { name }))) return;
          await call('POST', nodeBase(name) + '/maintenance', { force });
        } else if (action === 'node-maint-leave') {
          if (!window.confirm(fill(tr('cluster.confirm.maintLeave', 'Take node "{name}" out of maintenance?'), { name }))) return;
          await call('DELETE', nodeBase(name) + '/maintenance');
        } else {
          return;
        }
      }
      out(tr('cluster.requested', 'Requested; followed in the actions dock.'));
      setTimeout(() => refresh(true), 2500);
    } catch (e) {
      out(tr('topology.actionFailed', 'Action failed') + ' : ' + (e.message || e));
    }
  }

  // -------------------------------------------------------------------------
  // Cycle de vie
  // -------------------------------------------------------------------------
  async function refresh(fresh) {
    if (!cluster || !host || document.hidden) return;
    const asked = cluster;
    try {
      const r = await fetch(`/api/topology/${encodeURIComponent(asked)}${fresh === true ? '?fresh=1' : ''}`);
      const d = await r.json();
      if (asked !== cluster) return;
      if (!r.ok || d.unreachable || d.error) {
        const b = host.querySelector('.fabric-body');
        if (b) b.innerHTML = `<p class="hint warn">${esc(d.unreachable
          ? tr('fabric.unreachable', 'Cluster unreachable') : (d.error || 'HTTP ' + r.status))}</p>`;
        return;
      }
      const before = signature(selected);
      lastData = d;
      render(d);
      refreshPanel(before);
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
          <input type="search" class="cm-filter tip" data-tip-i18n="cluster.filterTip"
                 placeholder="${esc(tr('cluster.filter', 'Filter: name, address, network'))}"
                 aria-label="${esc(tr('cluster.filter', 'Filter: name, address, network'))}">
          <label class="topology-unlock tip" data-tip-i18n="topology.unlockTip">
            <input type="checkbox" class="cm-unlock"> ${lock} ${esc(tr('topology.unlockDestructive', 'Allow destructive actions'))}
          </label>
          <button type="button" class="btn btn-sm fabric-refresh tip"
                  data-tip-i18n="topology.refreshTip">${icon} ${esc(tr('topology.refresh', 'Refresh'))}</button>
        </span>
      </div>
      <div class="fabric-layout">
        <div class="fabric-body"></div>
        <aside class="fabric-detail"><p class="hint">${esc(tr('cluster.detailHint',
          'Click a VM or a host for its actions.'))}</p></aside>
      </div>`;
    applyTips(host);
    if (window.CopyTo) CopyTo.wire(host);
    let t;
    host.querySelector('.cm-filter').addEventListener('input', (e) => {
      clearTimeout(t);
      t = setTimeout(() => { filter = e.target.value.trim().toLowerCase(); if (lastData) render(lastData); }, 150);
    });
    host.querySelector('.cm-unlock').addEventListener('change', (e) => {
      unlocked = e.target.checked;
      if (selected && selected.type === 'vm') showVm(selected.key);
      if (selected && selected.type === 'node') showNode(selected.key);
    });
    host.addEventListener('click', (e) => {
      if (e.target.closest('[data-copy]')) return;
      if (e.target.closest('.fabric-refresh')) { refresh(true); return; }
      const force = e.target.closest('[data-cm-force]');
      if (force && selected) { maintenanceCheck(selected.key, force.checked); return; }
      const a = e.target.closest('[data-cm-act]');
      if (a) { act(a.dataset.cmAct, a); return; }
      const card = e.target.closest('[data-vm]');
      if (card) { hidePop(); showVm(card.dataset.vm); return; }
      const head = e.target.closest('[data-node]');
      if (head) showNode(head.dataset.node);
    });
    host.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      const card = e.target.closest('[data-vm]');
      const head = e.target.closest('[data-node]');
      if (card) { e.preventDefault(); showVm(card.dataset.vm); }
      else if (head) { e.preventDefault(); showNode(head.dataset.node); }
    });
    host.addEventListener('mouseover', (e) => {
      const card = e.target.closest('[data-vm]');
      if (card) showPop(card);
    });
    host.addEventListener('mouseout', (e) => {
      const card = e.target.closest('[data-vm]');
      if (card && !card.contains(e.relatedTarget)) hidePop();
    });
    host.addEventListener('focusin', (e) => {
      const card = e.target.closest('[data-vm]');
      if (card) showPop(card);
    });
    host.addEventListener('focusout', hidePop);
    host.querySelector('.fabric-body').addEventListener('scroll', hidePop);
  }

  function start(clusterName) {
    const h = document.querySelector('.overview-subtab[data-subtab="cluster"] .topology-host');
    if (!h) return Promise.resolve();
    if (cluster !== clusterName) { lastData = null; selected = null; }
    cluster = clusterName;
    if (host !== h || !h.querySelector('.fabric-body')) { host = h; unlocked = false; shell(); }
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
    hidePop();
  }

  return { start, stop, refresh };
})();

if (typeof window !== 'undefined') window.ClusterMap = ClusterMap;
