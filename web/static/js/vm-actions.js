/**
 * harvester-ops — le menu d'actions d'une VM, comme celui de Harvester (v1.60.0)
 *
 * Un bouton « ⋮ » sur chaque VM ouvre un menu rangé par familles
 * (alimentation, protection, disques, migration, copie, YAML, suppression).
 * Le menu lit l'état réel de la VM (/state) à l'ouverture : un geste qui ne
 * s'applique pas est grisé et dit pourquoi, au lieu d'échouer après coup.
 * Les gestes qui demandent un choix (clone, template, suppression, disque,
 * migration) ouvrent une petite fenêtre. Chaque geste est une action suivie
 * dans le dock, par bin/harvester-resources.py vm.
 */
const VMActions = (() => {
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const enc = encodeURIComponent;
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 14) => (window.Icons ? Icons.svg(n, { size }) : '');
  const base = (c, ns, n) => `/api/vm/${enc(c)}/${enc(ns)}/${enc(n)}`;

  let menu = null;

  async function call(method, url, body) {
    const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' },
                                 body: body ? JSON.stringify(body) : undefined });
    let d = {};
    try { d = await r.json(); } catch { /* sans corps */ }
    if (!r.ok) throw new Error(d.hint || d.error || `HTTP ${r.status}`);
    return d;
  }

  function toast(text, bad) {
    const el = document.createElement('div');
    el.className = `vma-toast${bad ? ' bad' : ''}`;
    el.setAttribute('role', 'status');
    el.innerHTML = `${icon(bad ? 'fail' : 'ok')} ${esc(text)}`;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), bad ? 7000 : 4000);
  }

  /** Suit une action jusqu'à sa fin ; `into` reçoit les étapes. */
  function follow(actionId, into, doneText, onDone) {
    if (window.Dock && Dock.poll) Dock.poll();
    const say = (html) => { if (into) into.innerHTML = html; };
    say(esc(tr('bk.started', { id: actionId })));
    if (!window.SSEReconnect) { if (onDone) onDone(true); return; }
    let last = '';
    const es = SSEReconnect.connect(`/api/stream/${enc(actionId)}`, {
      on: {
        step: (e) => { try { const s = JSON.parse(e.data); if (s.message) { last = s.message; say(esc(s.message)); } } catch { /* ligne illisible */ } },
        end: (e) => {
          let d = {};
          try { d = JSON.parse(e.data); } catch { /* fin sans détail */ }
          es.close();
          const ok = d.status === 'done';
          const msg = ok ? doneText : tr('res.error', { msg: last || d.error_summary || d.status || '?' });
          say(`<span class="${ok ? '' : 'res-error'}">${icon(ok ? 'ok' : 'fail')} ${esc(msg)}</span>`);
          if (!into) toast(msg, !ok);
          if (onDone) onDone(ok);
        },
      },
    });
  }

  // -- le menu ------------------------------------------------------------------
  function close() {
    if (!menu) return;
    menu.el.remove();
    document.removeEventListener('pointerdown', menu.outside, true);
    document.removeEventListener('keydown', menu.keys, true);
    if (menu.anchor) { menu.anchor.setAttribute('aria-expanded', 'false'); menu.anchor.focus(); }
    menu = null;
  }

  // Clés en toutes lettres : le contrôle de parité i18n ne lit que des littéraux.
  const TIP = {
    restart: () => tr('vma.tip.restart'), softreboot: () => tr('vma.tip.softreboot'),
    pause: () => tr('vma.tip.pause'), unpause: () => tr('vma.tip.unpause'), 'force-stop': () => tr('vma.tip.forceStop'),
    backup: () => tr('vma.tip.backup'), snapshot: () => tr('vma.tip.snapshot'), 'add-volume': () => tr('vma.tip.addVolume'),
    'remove-volume': () => tr('vma.tip.removeVolume'), eject: () => tr('vma.tip.eject'),
    'abort-migration': () => tr('vma.tip.abortMigration'), migrate: () => tr('vma.tip.migrate'),
    clone: () => tr('vma.tip.clone'), template: () => tr('vma.tip.template'), yaml: () => tr('vma.tip.yaml'),
    download: () => tr('vma.tip.download'), delete: () => tr('vma.tip.delete'),
    logs: () => tr('vma.tip.logs'), serial: () => tr('vma.tip.serial'),
    cpumem: () => tr('vma.tip.cpumem'), 'insert-cdrom': () => tr('vma.tip.insert'), 'eject-image': () => tr('vma.tip.ejectImage'),
    'add-nic': () => tr('vma.tip.addNic'), 'remove-nic': () => tr('vma.tip.removeNic'),
    'storage-migrate': () => tr('vma.tip.storageMigrate'), 'cancel-storage-migration': () => tr('vma.tip.cancelStorage'),
    schedule: () => tr('vma.tip.schedule'), quota: () => tr('vma.tip.quota'), access: () => tr('vma.tip.access'),
  };

  function item(act, label, ico, why, extra = '') {
    return `<button type="button" role="menuitem" class="vma-item tip${act === 'delete' ? ' danger' : ''}"
      data-vma="${esc(act)}" ${extra} data-tip="${esc(why || TIP[act]())}" ${why ? 'aria-disabled="true"' : ''}>
      <span class="vma-ic">${icon(ico)}</span><span>${esc(label)}</span></button>`;
  }
  const group = (title, body) => (body ? `<div class="vma-group" role="group" aria-label="${esc(title)}">
      <div class="vma-title">${esc(title)}</div>${body}</div>` : '');

  function build(st) {
    const on = st.running, paused = st.paused;
    const off = !on ? tr('vma.why.stopped') : '';
    const cm = st.cpumem || {};
    const power = [
      ...(cm.enabled ? [item('cpumem', tr('vma.cpumem'), 'compute', off || (st.restart_required ? tr('vma.why.restartRequired') : ''))] : []),
      item('restart', tr('vma.restart'), 'restart', off),
      item('softreboot', tr('vma.softreboot'), 'refresh', off || (!st.agent ? tr('vma.why.noAgent') : '')),
      paused ? item('unpause', tr('vma.unpause'), 'play', '') : item('pause', tr('vma.pause'), 'pause', off),
      item('force-stop', tr('vma.forceStop'), 'power', st.run_strategy === 'Halted' && !on ? tr('vma.why.alreadyStopped') : ''),
    ].join('');
    const protect = [
      item('backup', tr('vma.backup'), 'bundle', ''),
      item('snapshot', tr('vma.snapshot'), 'snapshot', ''),
      item('schedule', tr('vma.schedule'), 'timer', ''),
      item('quota', st.quota ? tr('vma.quotaSet', { size: `${Math.round(st.quota / 2 ** 30)} Gi` }) : tr('vma.quota'), 'storage', ''),
    ].join('');
    // v1.61.0 : un lecteur SATA se vide et se remplit à chaud (Insert / Eject
    // Image de Harvester) ; les autres CD-ROM gardent l'éjection à froid
    const sata = st.sata_cdroms || [];
    const sataNames = new Set(sata.map(d => d.name));
    const hot = (st.volumes || []).filter(v => v.hotpluggable && !sataNames.has(v.volume));
    const cds = (st.volumes || []).filter(v => v.kind === 'cdrom' && !sataNames.has(v.volume));
    const disks = [
      item('add-volume', tr('vma.addVolume'), 'hotplug', off),
      ...hot.map(v => item('remove-volume', tr('vma.removeVolume', { name: v.volume }), 'eject', '', `data-volume="${esc(v.volume)}"`)),
      ...sata.map(d => (d.empty
        ? item('insert-cdrom', tr('vma.insert', { name: d.name }), 'cdrom', '', `data-volume="${esc(d.name)}"`)
        : item('eject-image', tr('vma.ejectImage', { name: d.name }), 'eject', '', `data-volume="${esc(d.name)}"`))),
      ...cds.map(v => item('eject', tr('vma.eject', { name: v.volume }), 'eject', '', `data-volume="${esc(v.volume)}"`)),
    ].join('');
    const ifaces = st.interfaces || [];
    const net = [
      item('add-nic', tr('vma.addNic'), 'network', ''),
      ...ifaces.filter(i => i.unpluggable).map(i => item('remove-nic', tr('vma.removeNic', { name: i.name }), 'close', '', `data-volume="${esc(i.name)}"`)),
    ].join('');
    const mig = [
      st.migrating ? item('abort-migration', tr('vma.abortMigration'), 'close', '')
        : item('migrate', tr('vma.migrate'), 'migrate', off || (!st.node ? tr('vma.why.noNode')
          : (!(st.targets || []).length ? tr('vma.why.noTarget') : ''))),
      st.storage_migration ? item('cancel-storage-migration', tr('vma.cancelStorage', { name: st.storage_migration }), 'close', '')
        : item('storage-migrate', tr('vma.storageMigrate'), 'volume', off || (!(st.targets || []).length ? tr('vma.why.noTarget') : '')),
    ].join('');
    const copyG = [
      item('clone', tr('vma.clone'), 'clone', ''),
      item('template', tr('vma.template'), 'doc', ''),
    ].join('');
    const yaml = [
      item('yaml', tr('vma.yaml'), 'code', ''),
      item('download', tr('vma.download'), 'download', ''),
    ].join('');
    const observe = [
      item('serial', tr('vma.serial'), 'console', off),
      item('logs', tr('vma.logs'), 'doc', off),
      item('access', tr('vma.access'), 'key', ''),
    ].join('');
    return group(tr('vma.g.power'), power) + group(tr('vma.g.protect'), protect) + group(tr('vma.g.disks'), disks)
      + group(tr('vma.g.network'), net) + group(tr('vma.g.migration'), mig) + group(tr('vma.g.copy'), copyG)
      + group(tr('vma.g.observe'), observe) + group(tr('vma.g.yaml'), yaml)
      + `<div class="vma-group">${item('delete', tr('vma.delete'), 'trash', '')}</div>`;
  }

  async function open(anchor, cluster, ns, name, opts = {}) {
    if (menu && menu.anchor === anchor) { close(); return; }
    close();
    const el = document.createElement('div');
    el.className = 'vma-menu';
    el.setAttribute('role', 'menu');
    el.setAttribute('aria-label', tr('vma.label', { name: `${ns}/${name}` }));
    el.innerHTML = `<div class="vma-head">${icon('vm', 13)} <strong>${esc(name)}</strong> <span class="res-dim">${esc(ns)}</span></div>
      <div class="vma-body"><p class="form-hint">${esc(tr('common.loading'))}</p></div>`;
    document.body.appendChild(el);
    place(el, anchor);
    anchor.setAttribute('aria-expanded', 'true');
    menu = {
      el, anchor, cluster, ns, name, opts, st: null,
      outside: (e) => { if (!el.contains(e.target) && e.target !== anchor && !anchor.contains(e.target)) close(); },
      keys: (e) => {
        if (e.key === 'Escape') { e.preventDefault(); close(); return; }
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
    const mine = menu;
    try {
      const st = await call('GET', `${base(cluster, ns, name)}/state`);
      if (menu !== mine) return;
      mine.st = st;
      el.querySelector('.vma-body').innerHTML = build(st);
      place(el, anchor);
      el.querySelector('.vma-item:not([aria-disabled])')?.focus();
    } catch (err) {
      if (menu !== mine) return;
      el.querySelector('.vma-body').innerHTML = `<p class="res-error">${esc(err.message)}</p>`;
    }
    el.addEventListener('click', (e) => {
      const b = e.target.closest('.vma-item');
      if (!b || b.getAttribute('aria-disabled')) return;
      const act = b.dataset.vma;
      const vol = b.dataset.volume;
      const ctx = { cluster, ns, name, st: mine.st, onDone: opts.onDone };
      close();
      run(act, ctx, vol);
    });
  }

  function place(el, anchor) {
    const r = anchor.getBoundingClientRect();
    const w = el.offsetWidth || 260, h = el.offsetHeight || 300;
    let left = r.right - w, top = r.bottom + 4;
    if (left < 8) left = 8;
    if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 4);
    el.style.left = `${left + window.scrollX}px`;
    el.style.top = `${top + window.scrollY}px`;
  }

  // -- les gestes ---------------------------------------------------------------
  const DIRECT = {
    restart: (p) => tr('vma.confirm.restart', p), softreboot: (p) => tr('vma.confirm.softreboot', p),
    pause: (p) => tr('vma.confirm.pause', p), unpause: null,
    'force-stop': (p) => tr('vma.confirm.forceStop', p), 'abort-migration': (p) => tr('vma.confirm.abort', p),
  };
  const DONE = {
    restart: (p) => tr('vma.done.restart', p), softreboot: (p) => tr('vma.done.softreboot', p),
    pause: (p) => tr('vma.done.pause', p), unpause: (p) => tr('vma.done.unpause', p),
    'force-stop': (p) => tr('vma.done.forceStop', p), 'abort-migration': (p) => tr('vma.done.abort', p),
    'remove-volume': (p) => tr('vma.done.removeVolume', p), eject: (p) => tr('vma.done.eject', p),
    cpumem: (p) => tr('vma.done.cpumem', p), 'insert-cdrom': (p) => tr('vma.done.insert', p),
    'eject-image': (p) => tr('vma.done.ejectImage', p), 'add-nic': (p) => tr('vma.done.addNic', p),
    'remove-nic': (p) => tr('vma.done.removeNic', p), 'storage-migrate': (p) => tr('vma.done.storage', p),
    'cancel-storage-migration': (p) => tr('vma.done.cancelStorage', p), quota: (p) => tr('vma.done.quota', p),
    access: (p) => tr('vma.done.access', p),
    clone: (p) => tr('vma.done.clone', p), template: (p) => tr('vma.done.template', p),
    'add-volume': (p) => tr('vma.done.addVolume', p), migrate: (p) => tr('vma.done.migrate', p),
  };

  async function doIt(ctx, action, body, into) {
    const out = await call('POST', `${base(ctx.cluster, ctx.ns, ctx.name)}/do/${action}`, body || {});
    const p = { name: `${ctx.ns}/${ctx.name}` };
    follow(out.action_id, into, (DONE[action] || ((q) => tr('vma.done.generic', q)))(p),
      (ok) => { if (ok && ctx.onDone) ctx.onDone(action); });
    return out.action_id;
  }

  async function run(act, ctx, vol) {
    const ref = `${ctx.ns}/${ctx.name}`;
    try {
      if (act in DIRECT) {
        if (DIRECT[act] && !confirm(DIRECT[act]({ name: ref }))) return;
        await doIt(ctx, act, {});
        return;
      }
      if (act === 'remove-volume') {
        if (!confirm(tr('vma.confirm.removeVolume', { name: ref, disk: vol }))) return;
        await doIt(ctx, act, { volume: vol });
        return;
      }
      if (act === 'eject') { ejectDialog(ctx, vol); return; }
      if (act === 'yaml') { if (window.YamlWindow) YamlWindow.open(ctx.cluster, 'vm', ctx.ns, ctx.name, { onDone: ctx.onDone }); return; }
      if (act === 'download') { if (window.YamlWindow) YamlWindow.download(ctx.cluster, 'vm', ctx.ns, ctx.name); return; }
      if (act === 'snapshot') { if (window.VMSnapshots) VMSnapshots.open(ctx.cluster, ctx.ns, ctx.name); return; }
      if (act === 'backup') { backupDialog(ctx); return; }
      if (act === 'logs') { logsWindow(ctx); return; }
      if (act === 'serial') { serialWindow(ctx); return; }
      if (act === 'eject-image') {
        if (!confirm(tr('vma.confirm.ejectImage', { name: ref, drive: vol }))) return;
        await doIt(ctx, act, { volume: vol });
        return;
      }
      if (act === 'remove-nic') {
        if (!confirm(tr('vma.confirm.removeNic', { name: ref, iface: vol }))) return;
        await doIt(ctx, act, { iface: vol });
        return;
      }
      if (act === 'cancel-storage-migration') {
        if (!confirm(tr('vma.confirm.cancelStorage', { name: ref }))) return;
        await doIt(ctx, act, {});
        return;
      }
      if (act === 'schedule') { if (window.Backups && Backups.scheduleFor) Backups.scheduleFor(ctx.cluster, ctx.ns, ctx.name); return; }
      if (act === 'cpumem') { cpumemDialog(ctx); return; }
      if (act === 'insert-cdrom') { insertDialog(ctx, vol); return; }
      if (act === 'add-nic') { nicDialog(ctx); return; }
      if (act === 'storage-migrate') { storageDialog(ctx); return; }
      if (act === 'quota') { quotaDialog(ctx); return; }
      if (act === 'access') { accessDialog(ctx); return; }
      if (act === 'clone') { cloneDialog(ctx); return; }
      if (act === 'template') { templateDialog(ctx); return; }
      if (act === 'delete') { deleteDialog(ctx); return; }
      if (act === 'add-volume') { addVolumeDialog(ctx); return; }
      if (act === 'migrate') { migrateDialog(ctx); return; }
    } catch (err) {
      toast(err.message, true);
    }
  }

  // Une petite fenêtre de formulaire, au même patron que les autres (.of-form)
  function dialog(ctx, kind, title, ico, fieldsHtml, submitLabel, onSubmit, opts = {}) {
    const panel = FloatingPanels.open({
      id: `vma-${kind}-${ctx.cluster}-${ctx.ns}-${ctx.name}`, icon: ico, width: opts.width || 480, height: opts.height || 420,
      title: `${title} · ${ctx.ns}/${ctx.name}`,
      bodyHtml: `<form class="of-form vma-form" autocomplete="off">${fieldsHtml}
        <div class="bk-form-actions"><button type="submit" class="btn btn-sm ${opts.danger ? 'btn-danger' : 'btn-primary'} tip"
          data-tip="${esc(tr('bk.submitTip'))}">${icon(opts.submitIcon || 'ok')} ${esc(submitLabel)}</button></div>
        <div class="of-msg" role="status"></div></form>`,
    });
    const root = panel.el;
    if (root.dataset.vmaReady) return root;
    root.dataset.vmaReady = '1';
    const form = root.querySelector('form');
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = form.querySelector('button[type="submit"]');
      btn.disabled = true;
      try {
        await onSubmit(form, root.querySelector('.of-msg'), () => { btn.disabled = false; });
      } catch (err) {
        btn.disabled = false;
        root.querySelector('.of-msg').innerHTML = `<span class="res-error">${esc(err.message)}</span>`;
      }
    });
    form.querySelector('input:not([type=hidden]):not([type=checkbox]), select')?.focus();
    return root;
  }
  const check = (name, label, tip, checked) => `<label class="bk-check tip" data-tip="${esc(tip)}">
      <input type="checkbox" name="${name}" ${checked ? 'checked' : ''}> <span>${esc(label)}</span></label>`;
  const field = (name, label, input, tip) => `<label class="bk-field"><span>${esc(label)}</span>${
    input.replace(/^<(input|select|textarea)/, `<$1 class="tip" data-tip="${esc(tip)}"`)}</label>`;
  const followForm = (ctx, action, body, msg, reenable) =>
    doIt(ctx, action, body, msg).then(() => reenable && setTimeout(reenable, 1500));

  function cloneDialog(ctx) {
    dialog(ctx, 'clone', tr('vma.clone'), 'clone',
      `<p class="form-hint">${esc(tr('vma.hint.clone'))}</p>`
      + field('new_name', tr('vma.f.newName'), `<input name="new_name" required pattern="[a-z0-9]([-a-z0-9]*[a-z0-9])?" maxlength="63" value="${esc(`${ctx.name}-clone`)}">`, tr('vma.t.newName'))
      + check('with_data', tr('vma.f.withData'), tr('vma.t.withData'), true)
      + check('start', tr('vma.f.startClone'), tr('vma.t.startClone'), false),
      tr('vma.clone'), (form, msg, re) => followForm(ctx, 'clone', {
        new_name: form.new_name.value.trim(), with_data: form.with_data.checked, start: form.start.checked,
      }, msg, re), { submitIcon: 'clone' });
  }

  function templateDialog(ctx) {
    dialog(ctx, 'template', tr('vma.template'), 'doc',
      `<p class="form-hint">${esc(tr('vma.hint.template'))}</p>`
      + field('template_name', tr('vma.f.templateName'), `<input name="template_name" required pattern="[a-z0-9]([-a-z0-9]*[a-z0-9])?" maxlength="63" value="${esc(`${ctx.name}-template`)}">`, tr('vma.t.templateName'))
      + field('description', tr('of.f.description'), '<input name="description" maxlength="200">', tr('of.t.description'))
      + check('with_data', tr('vma.f.withData'), tr('vma.t.templateData'), false)
      + check('set_default', tr('vma.f.setDefault'), tr('vma.t.setDefault'), true),
      tr('vma.template'), (form, msg, re) => followForm(ctx, 'template', {
        template_name: form.template_name.value.trim(), description: form.description.value,
        with_data: form.with_data.checked, set_default: form.set_default.checked,
      }, msg, re));
  }

  function deleteDialog(ctx) {
    const vols = ((ctx.st && ctx.st.volumes) || []).filter(v => v.claim);
    // comme Harvester : le premier volume (disque système) est coché
    const rows = vols.map((v, i) => check(`vol:${v.claim}`, `${v.volume} (${v.claim})${v.kind === 'cdrom' ? ' · CD-ROM' : ''}`,
      tr('vma.t.deleteVolume'), i === 0)).join('');
    dialog(ctx, 'delete', tr('vma.delete'), 'trash',
      `<p class="form-hint">${esc(tr('vma.hint.delete'))}</p>`
      + (vols.length ? `<fieldset class="vma-vols"><legend>${esc(tr('vma.f.deleteVolumes'))}</legend>${rows}</fieldset>` : '')
      + ((ctx.st && ctx.st.cloudinit_secrets || []).length ? check('keep_cloudinit', tr('vma.f.keepCloudinit'), tr('vma.t.keepCloudinit'), false) : ''),
      tr('vma.delete'), async (form, msg) => {
        const remove = [...form.querySelectorAll('input[type=checkbox][name^="vol:"]')].filter(c => c.checked).map(c => c.name.slice(4));
        if (!confirm(tr('vma.confirm.delete', { name: `${ctx.ns}/${ctx.name}`, n: remove.length }))) {
          form.querySelector('button[type="submit"]').disabled = false;
          return;
        }
        const out = await call('DELETE', base(ctx.cluster, ctx.ns, ctx.name),
          { remove_volumes: remove, keep_cloudinit: !!form.keep_cloudinit?.checked });
        follow(out.action_id, msg, tr('vma.done.delete', { name: `${ctx.ns}/${ctx.name}` }),
          (ok) => { if (ok && ctx.onDone) ctx.onDone('delete'); });
      }, { danger: true, submitIcon: 'trash', height: 360 + vols.length * 28 });
  }

  function ejectDialog(ctx, vol) {
    const claim = (((ctx.st && ctx.st.volumes) || []).find(v => v.volume === vol) || {}).claim;
    dialog(ctx, `eject-${vol}`, tr('vma.eject', { name: vol }), 'eject',
      `<p class="form-hint">${esc(tr('vma.hint.eject'))}</p>`
      + (claim ? check('delete_volume', tr('vma.f.deleteCdrom', { name: claim }), tr('vma.t.deleteCdrom'), true) : ''),
      tr('vma.ejectGo'), (form, msg, re) => followForm(ctx, 'eject',
        { volume: vol, delete_volume: !!form.delete_volume?.checked }, msg, re), { height: 300 });
  }

  async function addVolumeDialog(ctx) {
    const root = dialog(ctx, 'add-volume', tr('vma.addVolume'), 'hotplug',
      `<p class="form-hint">${esc(tr('vma.hint.addVolume'))}</p>`
      + field('claim', tr('bk.col.volume'), '<select name="claim" required></select>', tr('vma.t.claim'))
      + field('volume', tr('vma.f.diskName'), '<input name="volume" pattern="[a-z0-9]([-a-z0-9]*[a-z0-9])?" maxlength="63">', tr('vma.t.diskName'))
      + field('bus', tr('vma.f.bus'), '<select name="bus"><option value="scsi">scsi</option><option value="virtio">virtio</option><option value="sata">sata</option></select>', tr('vma.t.bus')),
      tr('vma.addVolumeGo'), (form, msg, re) => followForm(ctx, 'add-volume', {
        claim: form.claim.value, volume: form.volume.value.trim() || undefined, bus: form.bus.value,
      }, msg, re), { submitIcon: 'hotplug' });
    const sel = root.querySelector('[name="claim"]');
    try {
      const d = await call('GET', `/api/pvcs/${enc(ctx.cluster)}?namespace=${enc(ctx.ns)}`);
      // un volume libre : du namespace de la VM, lié, et qu'aucune VM ne porte
      // (annotation harvesterhci.io/owned-by posée par Harvester)
      const mine = new Set(((ctx.st && ctx.st.volumes) || []).map(v => v.claim));
      const list = (Array.isArray(d) ? d : d.items || [])
        .filter(p => (p.namespace || ctx.ns) === ctx.ns && p.phase === 'Bound' && !mine.has(p.name) && !ownedBy(p.owned_by));
      sel.innerHTML = list.length
        ? list.map(p => `<option value="${esc(p.name)}">${esc(p.name)}${p.capacity ? ` · ${esc(p.capacity)}` : ''}</option>`).join('')
        : `<option value="" disabled selected>${esc(tr('vma.noFreeVolume'))}</option>`;
    } catch (err) {
      sel.innerHTML = `<option value="" disabled selected>${esc(err.message)}</option>`;
    }
  }

  function ownedBy(raw) {
    if (!raw) return false;
    try {
      const refs = JSON.parse(raw);
      return (Array.isArray(refs) ? refs : []).some(r => (r.refs || []).length);
    } catch { return true; }
  }

  function migrateDialog(ctx) {
    const targets = (ctx.st && ctx.st.targets) || [];
    dialog(ctx, 'migrate', tr('vma.migrate'), 'migrate',
      `<p class="form-hint">${esc(tr('vma.hint.migrate', { node: (ctx.st && ctx.st.node) || '?' }))}</p>`
      + field('node', tr('vma.f.targetNode'), `<select name="node"><option value="">${esc(tr('vma.anyNode'))}</option>${
        targets.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join('')}</select>`, tr('vma.t.targetNode')),
      tr('vma.migrateGo'), (form, msg, re) => followForm(ctx, 'migrate', { node: form.node.value || undefined },
        msg, re), { submitIcon: 'migrate', height: 300 });
  }

  function backupDialog(ctx) {
    dialog(ctx, 'backup', tr('vma.backup'), 'bundle',
      `<p class="form-hint">${esc(tr('vma.hint.backup'))}</p>`
      + field('name', tr('bk.f.name'), `<input name="name" pattern="[a-z0-9]([-a-z0-9]*[a-z0-9])?" maxlength="63" placeholder="${esc(tr('vma.f.autoName'))}">`, tr('vma.t.backupName')),
      tr('vma.backupGo'), async (form, msg, re) => {
        const body = { vm: ctx.name, type: 'backup' };
        if (form.name.value.trim()) body.name = form.name.value.trim();
        const out = await call('POST', `/api/backups/${enc(ctx.cluster)}/${enc(ctx.ns)}`, body);
        follow(out.action_id, msg, tr('vma.done.backup', { name: `${ctx.ns}/${ctx.name}` }), () => re());
      }, { submitIcon: 'bundle', height: 300 });
  }

  // -- v1.61.0 : les fenêtres des gestes à chaud ------------------------------------
  const gi = (q) => { const m = /^([0-9.]+)\s*(Mi|Gi|Ti)?$/.exec(String(q || '')); if (!m) return 0;
    return parseFloat(m[1]) * ({ Mi: 1 / 1024, Gi: 1, Ti: 1024 }[m[2] || 'Gi']); };

  function cpumemDialog(ctx) {
    const c = (ctx.st && ctx.st.cpumem) || {};
    const maxMem = Math.floor(gi(c.max_memory)) || '';
    dialog(ctx, 'cpumem', tr('vma.cpumem'), 'compute',
      `<p class="form-hint">${esc(tr('vma.hint.cpumem', { cpu: c.max_sockets || '?', mem: c.max_memory || '?' }))}</p>`
      + field('cpu', tr('vma.f.cpu'), `<input name="cpu" type="number" min="1" max="${esc(c.max_sockets || '')}" value="${esc(c.sockets || 1)}" required>`, tr('vma.t.cpu'))
      + field('memory', tr('vma.f.memoryGi'), `<input name="memory" type="number" min="1" max="${esc(maxMem)}" step="1" value="${esc(Math.round(gi(c.memory)) || 1)}" required>`, tr('vma.t.memory')),
      tr('of.save'), (form, msg, re) => followForm(ctx, 'cpumem', { cpu: Number(form.cpu.value), memory: `${form.memory.value}Gi` }, msg, re),
      { submitIcon: 'compute', height: 320 });
  }

  async function insertDialog(ctx, drive) {
    const root = dialog(ctx, `insert-${drive}`, tr('vma.insert', { name: drive }), 'cdrom',
      `<p class="form-hint">${esc(tr('vma.hint.insert'))}</p>`
      + field('image', tr('section.images'), '<select name="image" required></select>', tr('vma.t.isoImage')),
      tr('vma.insertGo'), (form, msg, re) => followForm(ctx, 'insert-cdrom', { volume: drive, image: form.image.value }, msg, re),
      { submitIcon: 'cdrom', height: 300 });
    const sel = root.querySelector('[name="image"]');
    try {
      const d = await call('GET', `/api/cluster-objects/${enc(ctx.cluster)}/images`);
      const ready = (d.items || []).filter(i => i.state === 'ready');
      const iso = ready.filter(i => /\.iso$/i.test(i.display_name || ''));
      const list = iso.length ? iso : ready;
      sel.innerHTML = list.map(i => `<option value="${esc(`${i.namespace}/${i.name}`)}">${esc(i.display_name)}</option>`).join('')
        || `<option value="" disabled selected>${esc(tr('vma.noImage'))}</option>`;
    } catch (err) { sel.innerHTML = `<option value="" disabled selected>${esc(err.message)}</option>`; }
  }

  async function nicDialog(ctx) {
    const n = ((ctx.st && ctx.st.interfaces) || []).length + 1;
    const root = dialog(ctx, 'add-nic', tr('vma.addNic'), 'network',
      `<p class="form-hint">${esc(tr('vma.hint.addNic'))}</p>`
      + field('iface', tr('vma.f.iface'), `<input name="iface" required pattern="[a-z0-9]([-a-z0-9]*[a-z0-9])?" maxlength="63" value="nic-${n}">`, tr('vma.t.iface'))
      + field('network', tr('vma.f.network'), '<select name="network" required></select>', tr('vma.t.network'))
      + field('mac', tr('vma.f.mac'), '<input name="mac" pattern="([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}" placeholder="52:54:00:…">', tr('vma.t.mac')),
      tr('vma.addNicGo'), (form, msg, re) => followForm(ctx, 'add-nic', {
        iface: form.iface.value.trim(), network: form.network.value, mac: form.mac.value.trim() || undefined }, msg, re),
      { submitIcon: 'network', height: 380 });
    const sel = root.querySelector('[name="network"]');
    try {
      const d = await call('GET', `/api/networks/${enc(ctx.cluster)}`);
      const list = (Array.isArray(d) ? d : d.items || []).filter(x => x.namespace !== 'harvester-system'
        && /"type"\s*:\s*"bridge"/.test(x.config || ''));
      sel.innerHTML = list.map(x => `<option value="${esc(`${x.namespace}/${x.name}`)}">${esc(`${x.namespace}/${x.name}`)}${x.vlan ? ` · VLAN ${esc(x.vlan)}` : ''}</option>`).join('')
        || `<option value="" disabled selected>${esc(tr('vma.noNetwork'))}</option>`;
    } catch (err) { sel.innerHTML = `<option value="" disabled selected>${esc(err.message)}</option>`; }
  }

  async function storageDialog(ctx) {
    const vols = ((ctx.st && ctx.st.volumes) || []).filter(v => v.claim && v.kind !== 'cdrom');
    const root = dialog(ctx, 'storage', tr('vma.storageMigrate'), 'volume',
      `<p class="form-hint">${esc(tr('vma.hint.storageMigrate'))}</p>`
      + field('volume', tr('vma.f.source'), `<select name="volume" required>${vols.map(v => `<option value="${esc(v.claim)}">${esc(v.volume)} (${esc(v.claim)})</option>`).join('')}</select>`, tr('vma.t.source'))
      + field('target', tr('vma.f.target'), '<select name="target" required></select>', tr('vma.t.target')),
      tr('vma.storageGo'), (form, msg, re) => followForm(ctx, 'storage-migrate', { volume: form.volume.value, target: form.target.value }, msg, re),
      { submitIcon: 'volume', height: 360 });
    const sel = root.querySelector('[name="target"]');
    try {
      const d = await call('GET', `/api/pvcs/${enc(ctx.cluster)}?namespace=${enc(ctx.ns)}`);
      const list = (Array.isArray(d) ? d : d.items || []).filter(p => p.phase === 'Bound' && !ownedBy(p.owned_by)
        && !vols.some(v => v.claim === p.name));
      sel.innerHTML = list.map(p => `<option value="${esc(p.name)}">${esc(p.name)} · ${esc(p.capacity || '?')} · ${esc(p.storage_class || '')}</option>`).join('')
        || `<option value="" disabled selected>${esc(tr('vma.noFreeVolume'))}</option>`;
    } catch (err) { sel.innerHTML = `<option value="" disabled selected>${esc(err.message)}</option>`; }
  }

  function quotaDialog(ctx) {
    const cur = ctx.st && ctx.st.quota ? Math.round(ctx.st.quota / 2 ** 30) : 0;
    dialog(ctx, 'quota', tr('vma.quota'), 'storage',
      `<p class="form-hint">${esc(tr('vma.hint.quota'))}</p>`
      + field('size', tr('vma.f.quotaGi'), `<input name="size" type="number" min="0" step="1" value="${esc(cur)}">`, tr('vma.t.quota')),
      tr('of.save'), (form, msg, re) => followForm(ctx, 'quota', { size: Number(form.size.value) > 0 ? `${form.size.value}Gi` : '0' }, msg, re),
      { submitIcon: 'save', height: 280 });
  }

  async function accessDialog(ctx) {
    const root = dialog(ctx, 'access', tr('vma.access'), 'key',
      `<p class="form-hint">${esc(tr('vma.hint.access'))}</p>`
      + field('kind', tr('vma.f.accessKind'), `<select name="kind"><option value="basic">${esc(tr('vma.accessBasic'))}</option><option value="ssh">${esc(tr('vma.accessSsh'))}</option></select>`, tr('vma.t.accessKind'))
      + field('users', tr('vma.f.users'), '<input name="users" required placeholder="root, ops">', tr('vma.t.users'))
      + `<div data-when="basic">${field('password', tr('vma.f.password'), '<input name="password" type="password" minlength="6" autocomplete="new-password">', tr('vma.t.password'))}</div>`
      + `<div data-when="ssh" hidden>${field('keys', tr('vma.f.keys'), '<select name="keys" multiple size="4"></select>', tr('vma.t.keys'))}</div>`,
      tr('vma.accessGo'), (form, msg, re) => {
        const kind = form.kind.value;
        const users = form.users.value.split(/[\s,]+/).filter(Boolean);
        const body = { kind, users };
        if (kind === 'basic') body.password = form.password.value;
        else body.keys = [...form.keys.selectedOptions].map(o => o.value);
        return followForm(ctx, 'access', body, msg, re);
      }, { submitIcon: 'key', height: 440 });
    const form = root.querySelector('form');
    const sync = () => form.querySelectorAll('[data-when]').forEach(el => { el.hidden = el.dataset.when !== form.kind.value; });
    form.kind.addEventListener('change', sync);
    sync();
    try {
      const d = await call('GET', `/api/sshkeys/${enc(ctx.cluster)}`);
      const keys = (Array.isArray(d) ? d : d.items || []).filter(k => k && k.name);
      form.keys.innerHTML = keys.map(k => `<option value="${esc(`${k.namespace}/${k.name}`)}">${esc(`${k.namespace}/${k.name}`)}</option>`).join('');
    } catch { /* la liste reste vide */ }
  }

  // v1.61.0 : « View Logs » de Harvester, les journaux du pod virt-launcher
  function logsWindow(ctx) {
    const panel = FloatingPanels.open({
      id: `vma-logs-${ctx.cluster}-${ctx.ns}-${ctx.name}`, icon: 'doc', width: 860, height: 560,
      title: `${tr('vma.logs')} · ${ctx.ns}/${ctx.name}`,
      bodyHtml: `<div class="vma-logs">
        <div class="yw-tools">
          <label class="bk-field vma-inline"><span>${esc(tr('vma.f.container'))}</span>
            <select name="container" class="tip" data-tip="${esc(tr('vma.t.container'))}"></select></label>
          <label class="bk-field vma-inline"><span>${esc(tr('vma.f.lines'))}</span>
            <select name="tail" class="tip" data-tip="${esc(tr('vma.t.lines'))}"><option>200</option><option selected>500</option><option>2000</option><option>5000</option></select></label>
          <label class="bk-check tip" data-tip="${esc(tr('vma.t.follow'))}"><input type="checkbox" name="follow" checked> <span>${esc(tr('vma.f.follow'))}</span></label>
          <button type="button" class="btn btn-sm btn-secondary tip" data-logs="refresh" data-tip="${esc(tr('res.refreshTip'))}">${icon('refresh')} ${esc(tr('overview.refresh'))}</button>
          <span class="res-dim vma-logs-pod"></span>
        </div>
        <pre class="vma-logs-text live-log" tabindex="0">${esc(tr('common.loading'))}</pre>
      </div>`,
    });
    const root = panel.el;
    if (root.dataset.vmaReady) return;
    root.dataset.vmaReady = '1';
    const pre = root.querySelector('.vma-logs-text');
    const sel = root.querySelector('[name="container"]');
    const tail = root.querySelector('[name="tail"]');
    const follow = root.querySelector('[name="follow"]');
    let timer = null;
    const load = async () => {
      if (!root.isConnected) { clearInterval(timer); return; }
      const q = new URLSearchParams({ tail: tail.value });
      if (sel.value) q.set('container', sel.value);
      try {
        const d = await call('GET', `${base(ctx.cluster, ctx.ns, ctx.name)}/logs?${q}`);
        if (!sel.options.length) {
          sel.innerHTML = d.containers.map(c => `<option value="${esc(c)}"${c === d.container ? ' selected' : ''}>${esc(c)}</option>`).join('');
        }
        root.querySelector('.vma-logs-pod').textContent = `${d.pod} · ${d.node || ''}`;
        const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
        pre.textContent = d.logs || tr('vma.logsEmpty');
        if (atEnd) pre.scrollTop = pre.scrollHeight;
      } catch (err) {
        pre.textContent = err.message;
      }
    };
    sel.addEventListener('change', load);
    tail.addEventListener('change', load);
    root.querySelector('[data-logs="refresh"]').addEventListener('click', load);
    timer = setInterval(() => { if (follow.checked && !document.hidden) load(); }, 5000);
    load();
  }

  // v1.61.0 : « Open in Serial Console » de Harvester. xterm.js (embarqué,
  // chargé à la première ouverture) relié à la sous-ressource `console` de
  // la VMI par un relais de la console, avec un ticket à usage unique.
  let xtermLoading = null;
  function loadXterm() {
    if (window.Terminal) return Promise.resolve();
    if (xtermLoading) return xtermLoading;
    xtermLoading = new Promise((resolve, reject) => {
      const css = document.createElement('link');
      css.rel = 'stylesheet';
      css.href = '/static/vendor/xterm/xterm.css';
      document.head.appendChild(css);
      const s = document.createElement('script');
      s.src = '/static/vendor/xterm/xterm.js';
      s.onload = () => resolve();
      s.onerror = () => reject(new Error('xterm.js could not be loaded'));
      document.head.appendChild(s);
    });
    return xtermLoading;
  }

  function serialWindow(ctx) {
    const panel = FloatingPanels.open({
      id: `vma-serial-${ctx.cluster}-${ctx.ns}-${ctx.name}`, icon: 'console', width: 820, height: 520,
      title: `${tr('vma.serial')} · ${ctx.ns}/${ctx.name}`,
      bodyHtml: `<div class="vma-serial">
        <div class="yw-tools">
          <span class="vma-serial-state res-dim" role="status">${esc(tr('common.loading'))}</span>
          <button type="button" class="btn btn-sm btn-secondary tip" data-serial="reconnect" data-tip="${esc(tr('vma.t.reconnect'))}" hidden>${icon('refresh')} ${esc(tr('vma.reconnect'))}</button>
        </div>
        <div class="vma-serial-term" tabindex="0"></div>
        <p class="form-hint">${esc(tr('vma.hint.serial'))}</p>
      </div>`,
    });
    const root = panel.el;
    if (root.dataset.vmaReady) return;
    root.dataset.vmaReady = '1';
    const host = root.querySelector('.vma-serial-term');
    const state = root.querySelector('.vma-serial-state');
    const again = root.querySelector('[data-serial="reconnect"]');
    let term = null, ws = null;
    const fit = () => {
      if (!term || !host.clientWidth) return;
      const cell = host.querySelector('.xterm-char-measure-element');
      const w = (cell && cell.getBoundingClientRect().width) || 8.4, h = (cell && cell.getBoundingClientRect().height) || 17;
      const cols = Math.max(40, Math.floor((host.clientWidth - 16) / w));
      const rows = Math.max(10, Math.floor((host.clientHeight - 8) / h));
      if (cols !== term.cols || rows !== term.rows) term.resize(cols, rows);
    };
    const connect = async () => {
      again.hidden = true;
      state.textContent = tr('vma.serialConnecting');
      try {
        await loadXterm();
        if (!term) {
          term = new window.Terminal({ convertEol: false, cursorBlink: true, fontSize: 13,
            fontFamily: getComputedStyle(document.documentElement).getPropertyValue('--font-mono') || 'monospace' });
          term.open(host);
          term.onData((d) => { if (ws && ws.readyState === 1) ws.send(new TextEncoder().encode(d)); });
          new ResizeObserver(fit).observe(host);
        }
        fit();
        const tk = await call('POST', `${base(ctx.cluster, ctx.ns, ctx.name)}/serial-ticket`);
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        ws = new WebSocket(`${proto}//${location.host}${tk.ws_path}?ticket=${enc(tk.ticket)}`);
        ws.binaryType = 'arraybuffer';
        ws.onopen = () => {
          state.textContent = tr('vma.serialOpen');
          ws.send(new TextEncoder().encode('\n'));     // comme Harvester : réveille l'invite
          term.focus();
        };
        ws.onmessage = (ev) => term.write(typeof ev.data === 'string' ? ev.data : new Uint8Array(ev.data));
        ws.onclose = (ev) => {
          state.textContent = ev.reason ? tr('vma.serialClosedWhy', { why: ev.reason }) : tr('vma.serialClosed');
          again.hidden = false;
        };
      } catch (err) {
        state.innerHTML = `<span class="res-error">${esc(err.message)}</span>`;
        again.hidden = false;
      }
    };
    again.addEventListener('click', connect);
    const obs = new MutationObserver(() => {
      if (!root.isConnected) { obs.disconnect(); if (ws) ws.close(); if (term) term.dispose(); }
    });
    obs.observe(document.body, { childList: true, subtree: true });
    connect();
  }

  // -- actions groupées (barre de la liste) ----------------------------------------
  async function bulk(cluster, refs, action, logEl) {
    const ask = { restart: (p) => tr('vma.confirm.bulkRestart', p), 'force-stop': (p) => tr('vma.confirm.bulkForceStop', p),
                  migrate: (p) => tr('vma.confirm.bulkMigrate', p) }[action];
    if (!ask || !confirm(ask({ n: refs.length }))) return;
    if (logEl) logEl.innerHTML = '';
    for (const ref of refs) {
      const [ns, name] = ref.split('/');
      const line = document.createElement('div');
      line.textContent = `${ref}: ${action}… `;
      if (logEl) logEl.appendChild(line);
      try {
        const out = await call('POST', `${base(cluster, ns, name)}/do/${action}`, {});
        line.append(Icons.el('ok', { cls: 'icon-ok' }), ` ${out.action_id}`);
      } catch (err) {
        line.append(Icons.el('fail', { cls: 'icon-err' }), ` ${err.message}`);
      }
    }
    if (window.Dock && Dock.poll) Dock.poll();
  }

  /** Supprimer une VM depuis un autre endroit (vue Cluster) : même fenêtre. */
  async function remove(cluster, ns, name, onDone) {
    let st = null;
    try { st = await call('GET', `${base(cluster, ns, name)}/state`); } catch { /* fenêtre sans liste de volumes */ }
    deleteDialog({ cluster, ns, name, st, onDone });
  }

  return { open, close, bulk, remove, follow, toast, _build: build };
})();
window.VMActions = VMActions;
