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
 * Filtres (texte, cluster, namespace, type, état), tri sur chaque colonne,
 * une ligne dépliée montre ses pods, et le menu de la ligne ouvre le YAML
 * (fenêtre YAML, en lecture) et les journaux d'un pod (500 lignes au plus,
 * conteneur au choix). v1.93.0 : créer (formulaire ou YAML), mettre à
 * l'échelle, redémarrer, supprimer, chacun une action suivie. Les namespaces système, les
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
    folder: (w) => w.folder || '\uffff',
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
    const canScale = SCALABLE.includes(w.kind);
    const canRestart = RESTARTABLE.includes(w.kind);
    el.innerHTML = `<div class="vma-head">${icon('container', 13)} <strong>${esc(w.name)}</strong>
        <span class="res-dim">${esc(w.kind)} · ${esc(w.namespace)}</span></div>
      <div class="vma-group">
        ${item('yaml', 'code', tr('cw.menu.yaml'), tr('cw.menu.yamlTip'))}
        ${item('download', 'download', tr('cw.menu.download'), tr('cw.menu.downloadTip'))}
        ${item('logs', 'logs', tr('cw.menu.logs'), noPod ? tr('cw.menu.logsNone') : tr('cw.menu.logsTip'), noPod)}
      </div>
      ${FILEABLE.includes(w.kind) && window.Folders ? `<div class="vma-group">
        ${item('folder', 'folder', tr('fld.move'), Folders.canOperate() ? tr('fld.moveOneTip') : tr('fld.operatorOnly'), !Folders.canOperate())}
      </div>` : ''}
      <div class="vma-group">
        ${item('scale', 'moveVertical', tr('cw.menu.scale'), canScale ? tr('cw.menu.scaleTip') : tr('cw.menu.scaleNo', { kind: w.kind }), !canScale)}
        ${item('restart', 'restart', tr('cw.menu.restart'), canRestart ? tr('cw.menu.restartTip') : tr('cw.menu.restartNo', { kind: w.kind }), !canRestart)}
        ${item('delete', 'trash', tr('cw.menu.delete'), tr('cw.menu.deleteTip'))}
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
      else if (act === 'scale') scaleDialog(w);
      else if (act === 'restart') restartDialog(w);
      else if (act === 'delete') deleteDialog(w);
      else if (act === 'folder' && window.Folders) {
        Folders.openMove([{ key: w.key, label: `${w.name} (${w.kind} · ${w.cluster}/${w.namespace})` }],
          { onDone: () => refreshViews() });
      }
    });
  }

  // -- v1.93.0 : les gestes, chacun une action suivie ---------------------------
  // Le serveur lance bin/harvester-containers.py ; on suit l'action (dock,
  // Activity) et on relit les listes à la fin.
  const SCALABLE = ['Deployment', 'StatefulSet', 'ReplicaSet'];
  // 2.0.0 : charges rangeables dans un dossier (décision 11)
  const FILEABLE = ['Deployment', 'StatefulSet', 'DaemonSet', 'CronJob'];
  const RESTARTABLE = ['Deployment', 'StatefulSet', 'DaemonSet'];
  const NAME_RE = /^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$/;
  const QTY_MEM_RE = /^[0-9]+(\.[0-9]+)?([kMGTPE]i?)?$/;
  const QTY_CPU_RE = /^([0-9]+(\.[0-9]+)?|[0-9]+m)$/;
  const ENV_RE = /^[A-Za-z_][A-Za-z0-9_.-]*$/;
  const views = [];

  function refreshViews() {
    views.forEach((v) => v.load({ fresh: true }));
  }

  async function send(method, url, body) {
    const r = await fetch(url, {
      method, headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let d = {};
    try { d = await r.json(); } catch { /* sans corps */ }
    if (!r.ok) throw new Error(d.hint || d.error || d.detail || `HTTP ${r.status}`);
    return d;
  }

  function follow(actionId, msg, doneText, onDone) {
    const end = (ok) => { refreshViews(); if (onDone) onDone(ok); };
    if (window.VMActions && VMActions.follow) VMActions.follow(actionId, msg, doneText, end);
    else { if (msg) msg.textContent = tr('bk.started', { id: actionId }); end(true); }
  }

  /** Une fenêtre à formulaire, sur le modèle des gestes des VM. */
  function dialog(id, title, ico, bodyHtml, submitLabel, onSubmit, opts = {}) {
    const panel = FloatingPanels.open({
      id, icon: ico, width: opts.width || 480, height: opts.height || 320, title,
      bodyHtml: `<form class="of-form ct-form" autocomplete="off" novalidate>${bodyHtml}
        <div class="bk-form-actions"><button type="submit" class="btn btn-sm ${opts.danger ? 'btn-danger' : 'btn-primary'} tip"
          data-ct-submit data-tip="${esc(opts.submitTip || tr('bk.submitTip'))}">${icon(opts.submitIcon || 'ok')} ${esc(submitLabel)}</button></div>
        <div class="of-msg" role="status"></div></form>`,
    });
    const root = panel.el;
    if (root.dataset.ctReady) return root;
    root.dataset.ctReady = '1';
    const form = root.querySelector('form');
    const msg = root.querySelector('.of-msg');
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = form.querySelector('[data-ct-submit]');
      btn.disabled = true;
      try {
        const out = await onSubmit(form, msg);
        if (!out) { btn.disabled = false; return; }
        // v1.94.1 : la demande est acceptée, la fenêtre se ferme ; l'action
        // reste dans le dock et sa fin s'annonce par une notification. Un
        // refus garde la fenêtre ouverte avec l'erreur (catch ci-dessous).
        FloatingPanels.close(id);
        follow(out.action_id, null, out.doneText);
      } catch (err) {
        btn.disabled = false;
        msg.innerHTML = `<span class="res-error">${icon('fail')} ${esc(err.message)}</span>`;
      }
    });
    if (opts.setup) opts.setup(root);
    form.querySelector('input:not([type=hidden]):not([type=checkbox]), select, textarea')?.focus();
    return root;
  }

  const field = (label, input, tip) => `<label class="bk-field"><span>${esc(label)}</span>${
    input.replace(/^<(input|select|textarea)/, `<$1 class="tip" data-tip="${esc(tip)}"`)}</label>`;
  const check = (name, label, tip, checked) => `<label class="bk-check tip" data-tip="${esc(tip)}">
      <input type="checkbox" name="${name}"${checked ? ' checked' : ''}> <span>${esc(label)}</span></label>`;
  const target = (w) => `/api/containers/${enc(w.cluster)}/${enc(w.namespace)}/${enc(w.kind.toLowerCase())}/${enc(w.name)}`;

  function scaleDialog(w) {
    return dialog(`ct-scale-${w.cluster}-${w.namespace}-${w.kind}-${w.name}`,
      `${tr('cw.menu.scale')} · ${w.namespace}/${w.name}`, 'moveVertical',
      `<p class="form-hint">${esc(tr('cw.scale.hint', { kind: w.kind, ready: w.ready, desired: w.desired == null ? '?' : w.desired }))}</p>`
      + field(tr('cw.scale.replicas'), `<input type="number" name="replicas" min="0" max="1000" step="1" required value="${esc(w.desired == null ? 1 : w.desired)}">`, tr('cw.scale.replicasTip')),
      tr('cw.menu.scale'), async (form, msg) => {
        const n = Number(form.replicas.value);
        if (!Number.isInteger(n) || n < 0 || n > 1000 || form.replicas.value === '') {
          msg.innerHTML = `<span class="res-error">${esc(tr('cw.err.replicas'))}</span>`;
          return null;
        }
        const out = await send('POST', `${target(w)}/scale`, { replicas: n });
        return { ...out, doneText: tr('cw.done.scale', { name: `${w.namespace}/${w.name}`, n }) };
      }, { submitIcon: 'moveVertical', height: 260 });
  }

  function restartDialog(w) {
    return dialog(`ct-restart-${w.cluster}-${w.namespace}-${w.kind}-${w.name}`,
      `${tr('cw.menu.restart')} · ${w.namespace}/${w.name}`, 'restart',
      `<p class="form-hint">${esc(tr('cw.restart.hint', { kind: w.kind, name: `${w.namespace}/${w.name}`, cluster: w.cluster }))}</p>`,
      tr('cw.menu.restart'), async () => {
        const out = await send('POST', `${target(w)}/restart`, {});
        return { ...out, doneText: tr('cw.done.restart', { name: `${w.namespace}/${w.name}` }) };
      }, { submitIcon: 'restart', height: 230, submitTip: tr('cw.restart.goTip') });
  }

  function deleteDialog(w) {
    const pods = (w.pods || []).length;
    return dialog(`ct-delete-${w.cluster}-${w.namespace}-${w.kind}-${w.name}`,
      `${tr('cw.menu.delete')} · ${w.namespace}/${w.name}`, 'trash',
      `<p class="form-hint ct-delete-what">${esc(tr('cw.delete.hint', { kind: w.kind, name: `${w.namespace}/${w.name}`, cluster: w.cluster, n: pods }))}</p>`,
      tr('cw.delete.go'), async () => {
        const out = await send('DELETE', target(w));
        return { ...out, doneText: tr('cw.done.delete', { name: `${w.namespace}/${w.name}` }) };
      }, { danger: true, submitIcon: 'trash', height: 240, submitTip: tr('cw.delete.goTip') });
  }

  // -- créer une charge : formulaire, ou YAML collé ----------------------------
  function clusterNames() {
    return window.App && App.listClusters ? App.listClusters() : [];
  }

  async function gpuHint(root, cluster) {
    const el = root.querySelector('[data-ct-gpu-hint]');
    if (!el) return;
    el.textContent = tr('common.loading');
    let d = null;
    try {
      const r = await fetch(`/api/gpus/${enc(cluster)}`);
      d = r.ok ? await r.json() : null;
    } catch { d = null; }
    if (root.querySelector('[name="cluster"]')?.value !== cluster) return;
    const s = d && d.summary && d.summary.node;
    if (!d || d.unreachable || !s) { el.textContent = tr('cw.create.gpuUnknown'); return; }
    el.textContent = s.total ? tr('cw.create.gpuFree', { free: s.free, total: s.total, cluster })
      : tr('cw.create.gpuNone', { cluster });
    el.dataset.free = String(s.free);
  }

  /** Les défauts du formulaire : un message par défaut, rien d'envoyé. */
  function formErrors(f) {
    const errs = [];
    const bad = (name, text) => { errs.push(text); f[name]?.setAttribute('aria-invalid', 'true'); };
    f.querySelectorAll('[aria-invalid]').forEach((x) => x.removeAttribute('aria-invalid'));
    if (!NAME_RE.test(f.name.value.trim())) bad('name', tr('cw.err.name'));
    if (!NAME_RE.test(f.namespace.value.trim())) bad('namespace', tr('cw.err.namespace'));
    if (!f.image.value.trim() || /\s/.test(f.image.value.trim())) bad('image', tr('cw.err.image'));
    const rep = Number(f.replicas.value);
    const lo = f.kind.value === 'Job' ? 1 : 0;
    if (f.replicas.value === '' || !Number.isInteger(rep) || rep < lo || rep > 1000) bad('replicas', tr('cw.err.replicas'));
    const g = Number(f.gpus.value || 0);
    if (!Number.isInteger(g) || g < 0 || g > 64) bad('gpus', tr('cw.err.gpus'));
    if (f.cpu.value.trim() && !QTY_CPU_RE.test(f.cpu.value.trim())) bad('cpu', tr('cw.err.cpu'));
    if (!QTY_MEM_RE.test(f.memory.value.trim())) bad('memory', tr('cw.err.memory'));
    const env = f.env.value.split('\n').map((l) => l.trim()).filter(Boolean);
    if (env.some((l) => !l.includes('=') || !ENV_RE.test(l.split('=')[0].trim()))) bad('env', tr('cw.err.env'));
    return errs;
  }

  function createDialog(cluster, namespace) {
    const names = clusterNames();
    const cl = cluster || (window.App && App.getCurrentCluster()) || names[0] || '';
    const nss = [...new Set(views.flatMap((v) => v.namespaces(cl)))].sort();
    const body = `
      <div class="sub-tabs sub-tabs-inline ct-create-tabs" role="tablist">
        <button type="button" class="sub-tab tip active" role="tab" aria-selected="true" data-ct-mode="form" data-tip="${esc(tr('cw.create.tabFormTip'))}">${icon('edit')} <span>${esc(tr('cw.create.tabForm'))}</span></button>
        <button type="button" class="sub-tab tip" role="tab" aria-selected="false" data-ct-mode="yaml" data-tip="${esc(tr('cw.create.tabYamlTip'))}">${icon('code')} <span>${esc(tr('cw.create.tabYaml'))}</span></button>
      </div>
      ${field(tr('col.cluster'), `<select name="cluster">${(names.length ? names : [cl]).map((c) => `<option value="${esc(c)}"${c === cl ? ' selected' : ''}>${esc(c)}</option>`).join('')}</select>`, tr('cw.create.clusterTip'))}
      <div data-ct-pane="form">
        ${field(tr('cw.col.name'), '<input name="name" maxlength="63" placeholder="my-app">', tr('cw.create.nameTip'))}
        ${field(tr('col.namespace'), `<input name="namespace" maxlength="63" list="ct-ns-list" value="${esc(namespace || '')}"><datalist id="ct-ns-list">${nss.map((n) => `<option value="${esc(n)}"></option>`).join('')}</datalist>`, tr('cw.create.namespaceTip'))}
        ${field(tr('cw.col.kind'), '<select name="kind"><option value="Deployment">Deployment</option><option value="Job">Job</option></select>', tr('cw.create.kindTip'))}
        ${field(tr('cw.create.image'), '<input name="image" placeholder="registry.suse.com/bci/bci-base:15.7">', tr('cw.create.imageTip'))}
        ${field(tr('cw.create.replicas'), '<input type="number" name="replicas" min="0" max="1000" step="1" value="1">', tr('cw.create.replicasTip'))}
        ${field(tr('cw.create.command'), '<input name="command" placeholder="sleep infinity">', tr('cw.create.commandTip'))}
        ${field(tr('cw.create.env'), '<textarea name="env" rows="3" placeholder="NAME=value"></textarea>', tr('cw.create.envTip'))}
        <div class="ct-create-row">
          ${field(tr('cw.create.gpus'), '<input type="number" name="gpus" min="0" max="64" step="1" value="0">', tr('cw.create.gpusTip'))}
          ${field(tr('cw.create.gpuResource'), '<input name="gpu_resource" value="nvidia.com/gpu">', tr('cw.create.gpuResourceTip'))}
        </div>
        <p class="form-hint ct-gpu-hint" data-ct-gpu-hint></p>
        <div class="form-hint bkg-hint" data-bkg-hint></div>
        <div class="ct-create-row">
          ${field(tr('cw.create.cpu'), '<input name="cpu" placeholder="500m">', tr('cw.create.cpuTip'))}
          ${field(tr('cw.create.memory'), '<input name="memory" value="256Mi">', tr('cw.create.memoryTip'))}
        </div>
      </div>
      <div data-ct-pane="yaml" hidden>
        ${field(tr('cw.create.yaml'), '<textarea name="yaml" rows="14" spellcheck="false" placeholder="apiVersion: apps/v1&#10;kind: Deployment&#10;..."></textarea>', tr('cw.create.yamlTip'))}
      </div>
      ${check('create_namespace', tr('cw.create.createNs'), tr('cw.create.createNsTip'), false)}
      <p class="form-hint">${esc(tr('cw.create.hint'))}</p>`;
    return dialog(`ct-create-${cl}`, tr('cw.create.title'), 'add', body, tr('cw.create.go'), async (f, msg) => {
      const mode = f.querySelector('[data-ct-mode].active').dataset.ctMode;
      const c = f.cluster.value;
      let payload;
      if (mode === 'yaml') {
        if (!f.yaml.value.trim()) { msg.innerHTML = `<span class="res-error">${esc(tr('cw.err.yaml'))}</span>`; return null; }
        payload = { yaml: f.yaml.value };
      } else {
        const errs = formErrors(f);
        if (errs.length) {
          msg.innerHTML = `<span class="res-error">${errs.map(esc).join('<br>')}</span>`;
          f.querySelector('[aria-invalid]')?.focus();
          return null;
        }
        payload = { spec: {
          name: f.name.value.trim(), namespace: f.namespace.value.trim(), kind: f.kind.value,
          image: f.image.value.trim(), replicas: Number(f.replicas.value), command: f.command.value.trim(),
          env: f.env.value.split('\n').map((l) => l.trim()).filter(Boolean),
          gpus: Number(f.gpus.value || 0), gpu_resource: f.gpu_resource.value.trim() || 'nvidia.com/gpu',
          cpu: f.cpu.value.trim(), memory: f.memory.value.trim(),
        } };
      }
      payload.create_namespace = f.create_namespace.checked;
      const out = await send('POST', `/api/containers/${enc(c)}/create`, payload);
      return { ...out, doneText: tr('cw.done.create', { name: `${out.namespace}/${out.name}`, cluster: c }) };
    }, {
      width: 560, height: 720, submitIcon: 'add', submitTip: tr('cw.create.goTip'),
      setup: (root) => {
        const f = root.querySelector('form');
        root.querySelectorAll('[data-ct-mode]').forEach((b) => b.addEventListener('click', () => {
          root.querySelectorAll('[data-ct-mode]').forEach((x) => {
            const on = x === b;
            x.classList.toggle('active', on);
            x.setAttribute('aria-selected', String(on));
          });
          root.querySelectorAll('[data-ct-pane]').forEach((p) => { p.hidden = p.dataset.ctPane !== b.dataset.ctMode; });
          root.querySelector('.of-msg').innerHTML = '';
        }));
        // v1.94.0 : GPU de conteneurs libres, réservations en cours (lecture seule)
        const bookingHint = () => { if (window.Bookings) Bookings.hint(root.querySelector('[data-bkg-hint]'), f.cluster.value, ['node']); };
        f.cluster.addEventListener('change', () => { gpuHint(root, f.cluster.value); bookingHint(); });
        bookingHint();
        f.kind.addEventListener('change', () => {
          if (f.kind.value === 'Job' && Number(f.replicas.value) < 1) f.replicas.value = '1';
        });
        gpuHint(root, f.cluster.value);
      },
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
    let scopeFolder = null;          // 2.0.0 : dossier de l'adresse (#containers/folder/<id>)
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
      const fpath = scopeFolder == null ? null : (scopeFolder === 'unfiled' ? 'unfiled'
        : (window.Folders ? Folders.pathOf(scopeFolder) : null) || '\u0000');
      const rows = data.workloads.filter((w) => {
        if (fpath === 'unfiled' && (!w.key || w.folder)) return false;
        if (fpath && fpath !== 'unfiled' && !(window.Folders && Folders.within(w.folder, fpath))) return false;
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

    const columns = () => (ALL ? 12 : 11);

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
          <button type="button" class="btn btn-sm btn-primary tip ct-create-btn" data-ct="create"
                  data-tip="${esc(tr('cw.create.btnTip'))}">${icon('add')} ${esc(tr('cw.create.btn'))}</button>
        </div>
        <p class="hint warn ct-notice" data-ct="notice" hidden></p>
        <p class="hint ct-folder-scope" data-ct="scope" hidden></p>
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
            ${th('folder', tr('vmf.col.folder'), tr('vmf.tip.sortFolder'))}
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
      q('[data-ct="create"]').addEventListener('click', () => createDialog(
        ALL ? (prefs.clusters.length === 1 ? prefs.clusters[0] : null) : (window.App && App.getCurrentCluster()),
        prefs.ns || ''));
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
        <td class="vmf-folder">${folderCell(w)}</td>
        <td class="vm-actions-cell">
          ${b('logs', icon('logs'), (w.pods || []).length ? tr('cw.menu.logsTip') : tr('cw.menu.logsNone'))}
          ${b('more', icon('more'), tr('cw.moreTip'))}
        </td></tr>${open ? podsRow(w) : ''}`;
    }

    function folderCell(w) {
      if (!w.key) return '<span class="res-dim">–</span>';
      if (!w.folder) return `<span class="res-dim">${esc(tr('fld.unfiledShort'))}</span>`;
      const consoleOnly = w.folder_source === 'console'
        ? ` <span class="badge warn tip" data-tip="${esc(tr('vmf.consoleOnlyTip', { host: w.cluster }))}" title="${esc(tr('vmf.consoleOnlyTip', { host: w.cluster }))}">${esc(tr('vmf.consoleOnly'))}</span>` : '';
      return esc(w.folder) + consoleOnly;
    }

    function renderScope() {
      const box = host.querySelector('[data-ct="scope"]');
      if (!box) return;
      box.hidden = scopeFolder == null;
      if (scopeFolder == null) return;
      const label = scopeFolder === 'unfiled' ? tr('fld.unfiled') : ((window.Folders && Folders.pathOf(scopeFolder)) || tr('vmf.folderGone'));
      box.innerHTML = `${icon('folder', 13)} ${esc(tr('cw.folderScope', { path: label }))}
        <a href="#containers" class="tip" title="${esc(tr('cw.folderScopeAllTip'))}">${esc(tr('cw.folderScopeAll'))}</a>`;
    }

    function render() {
      if (!host) return;
      renderScope();
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

    async function fetchCluster(cluster, fresh = '') {
      const r = await fetch(`/api/containers/${enc(cluster)}${fresh}`);
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

    async function load(opts = {}) {
      if (!host || loading) return;
      const fresh = opts.fresh ? '?fresh=1' : '';
      const cluster = ALL ? null : (window.App && App.getCurrentCluster());
      if (!ALL && !cluster) { render(); return; }
      if (!ALL && cluster !== shownCluster) { data = null; expanded.clear(); render(); }
      loading = true;
      try {
        if (ALL) {
          const r = await fetch(`/api/containers-all${fresh}`);
          if (r.ok) data = await r.json();
        } else {
          const d = await fetchCluster(cluster, fresh);
          if (cluster === (window.App && App.getCurrentCluster())) { data = d; shownCluster = cluster; }
        }
      } catch { /* console injoignable : on garde la dernière liste */ }
      loading = false;
      render();
    }

    function start(el, opts) {
      if (!el) return;
      scopeFolder = opts && opts.folder != null ? String(opts.folder) : null;
      if (window.Folders && scopeFolder != null) Folders.load();
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

    /** v1.92.0 : la vue s'ouvre sur une charge (lien depuis l'inventaire GPU) :
     *  filtres remis à zéro, texte et cluster posés, appliqués au prochain start. */
    function focus(p) {
      loadPrefs();
      Object.assign(prefs, { state: '', kind: '', ns: p.namespace || '', text: p.text || '',
                             clusters: ALL && p.cluster ? [p.cluster] : [] });
      savePrefs();
      host = null;
    }

    /** Les namespaces connus d'un cluster (propositions du formulaire de création). */
    function namespaces(cluster) {
      return [...new Set((data ? data.workloads : []).filter((w) => (w.cluster || shownCluster) === cluster)
        .map((w) => w.namespace))];
    }

    // 2.0.0 : l'arborescence des dossiers relue (chemin du filtre) ou changée
    document.addEventListener('hops:folders', () => { if (host && timer) render(); });
    document.addEventListener('hops:folders-changed', () => { if (host && timer) load({ fresh: true }); });

    const view = { start, stop, load, focus, namespaces, _visible: visible, folder: () => scopeFolder };
    views.push(view);
    return view;
  }

  return { current: makeView('cluster'), all: makeView('all'), logsWindow, createDialog, _age: age };
})();

window.Containers = Containers;
