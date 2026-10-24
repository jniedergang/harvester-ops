/**
 * harvester-ops : instantanés, clonage et suppression d'une VM KVM (v1.97.0).
 *
 * Trois fenêtres (FloatingPanels), ouvertes depuis le menu d'une VM KVM
 * (kvm.js) ou, pour les instantanés, depuis la console de la VM :
 *   - Instantanés : liste (actuel, date, état à la prise, emplacement,
 *     parent), prise d'un instantané (nom, description ; une VM en marche est
 *     mise en pause le temps d'enregistrer sa mémoire), retour et
 *     suppression confirmés dans la fenêtre. Les instantanés externes sont
 *     montrés, sans action.
 *   - Cloner : nouveau nom, aperçu du serveur (disques copiés et leur
 *     destination, place, disques partagés, ce qui est régénéré, refus),
 *     puis la copie suivie étape par étape.
 *   - Supprimer : aperçu d'abord (disques supprimés et gardés avec leur
 *     raison, fichiers illisibles à accepter un par un, notes), nom de la VM
 *     à saisir, puis la suppression avec l'empreinte de l'aperçu montré : si
 *     le serveur répond que l'aperçu a changé, le nouvel aperçu s'affiche.
 *
 * Toute écriture est une action suivie (dock, Activité) que le serveur lance
 * par bin/harvester-kvm.py ; on la suit par SSEReconnect et la fin porte le
 * résultat du script (`result.output`). Les règles (états, rôle) viennent du
 * serveur ; aucun nom n'est inséré sans échappement.
 */
const KVMVm = (() => {
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const enc = encodeURIComponent;
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 14) => (window.Icons ? Icons.svg(n, { size }) : '');
  const ROLE_RANK = { viewer: 1, operator: 2, admin: 3 };
  const SNAPSHOT_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
  const STEP_BADGE = { done: 'ok', ok: 'ok', running: 'info', warn: 'warn', error: 'fail', skipped: 'dim' };

  const base = (host, domain) => `/api/kvm/vms/${enc(host)}/${enc(domain)}`;
  const badge = (cls, text, tip) =>
    `<span class="badge ${cls}${tip ? ' tip' : ''}"${tip ? ` data-tip="${esc(tip)}"` : ''}>${esc(text)}</span>`;
  const dim = (t) => `<span class="res-dim">${esc(t)}</span>`;
  const errHtml = (t) => `<span class="res-error">${icon('fail')} ${esc(t)}</span>`;
  const okHtml = (t) => `<span>${icon('ok')} ${esc(t)}</span>`;
  const stateText = (s) => (window.KVM && KVM.stateLabel ? KVM.stateLabel(s) : (s || '?'));
  const canOperate = (role) => (ROLE_RANK[role] || 0) >= ROLE_RANK.operator;

  function bytes(n) {
    if (typeof n !== 'number' || !Number.isFinite(n)) return '–';
    const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
    let i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i += 1; }
    return `${v.toFixed(i ? 1 : 0)} ${u[i]}`;
  }

  async function getJson(url) {
    try {
      const r = await fetch(url);
      const d = await r.json().catch(() => null);
      if (!d) return { error: `HTTP ${r.status}`, status: r.status };
      if (!r.ok) return Object.assign({ status: r.status }, d, { error: d.error || `HTTP ${r.status}` });
      return d;
    } catch (e) { return { error: e.message }; }
  }

  async function send(method, url, body) {
    const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    let d = {};
    try { d = await r.json(); } catch { /* sans corps */ }
    if (!r.ok) {
      const err = new Error(d.hint || d.error || `HTTP ${r.status}`);
      err.data = d;
      throw err;
    }
    return d;
  }

  /** Suit une action : celui de kvm.js (`KVM.follow`), chargé avant. */
  const follow = (actionId, onStep, onEnd) => KVM.follow(actionId, onStep, onEnd);

  /** Une étape de l'action dans la liste de la fenêtre (une ligne par étape). */
  function stepLine(list, s) {
    let li = [...list.children].find((x) => x.dataset.step === (s.step_id || ''));
    if (!li) { li = document.createElement('li'); li.dataset.step = s.step_id || ''; list.appendChild(li); }
    const st = s.status || 'running';
    li.className = `kvm-step is-${st}`;
    li.innerHTML = `${badge(STEP_BADGE[st] || 'info', s.step_id || st)} ${esc(s.message || '')}`;
  }

  // rend la promesse de la relecture : les fenêtres l'enchaînent après leur
  // propre lecture, jamais en même temps (voir `load` des instantanés)
  const refreshList = () => (window.KVM && KVM.load ? KVM.load(true) : Promise.resolve());
  const listHtml = (items) => (items && items.length ? `<ul class="kvm-op-list">${items.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : '');
  const failText = (run, o) => o.error || run.error_summary || run.status || '?';

  function panel(kind, host, domain, title, ic, bodyHtml, size) {
    if (!window.FloatingPanels) return null;
    const p = FloatingPanels.open({
      id: `kvm-${kind}-${host}-${enc(domain)}`, icon: ic, title,
      width: size[0], height: size[1], bodyHtml,
    });
    return p && p.el;
  }

  // -- Instantanés --------------------------------------------------------------------
  function defaultSnapshotName(domain, now = new Date()) {
    const p = (n) => String(n).padStart(2, '0');
    const stamp = `${now.getFullYear()}${p(now.getMonth() + 1)}${p(now.getDate())}-${p(now.getHours())}${p(now.getMinutes())}`;
    let stem = String(domain).replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^[^A-Za-z0-9]+/, '');
    stem = stem.slice(0, 64 - stamp.length - 1);
    return stem ? `${stem}-${stamp}` : `snap-${stamp}`;
  }

  const takenLabel = (s) => (s === 'running' ? tr('kvm.snap.withMemory')
    : (s === 'shutoff' || s === 'shut off') ? tr('kvm.snap.disksOnly') : (s || '–'));

  function openSnapshots(host, domain) {
    const root = panel('snapshots', host, domain, tr('kvm.snap.title', { name: domain, host }), 'snapshot', `
      <div class="kvm-vmw kvm-snapshots">
        <div class="res-tools"><span class="res-dim" data-ks="state"></span>
          <button type="button" class="btn btn-sm btn-secondary tip" data-ks="refresh" data-tip="${esc(tr('kvm.snap.refreshTip'))}">${icon('refresh')} ${esc(tr('overview.refresh'))}</button></div>
        <div class="kvm-op-support" data-ks="support"></div>
        <div data-ks="list"><p class="form-hint">${esc(tr('common.loading'))}</p></div>
        <div class="kvm-op-confirm" data-ks="confirm" hidden></div>
        <form class="of-form kvm-snap-create" data-ks="create" autocomplete="off" novalidate>
          <h4>${esc(tr('kvm.snap.create'))}</h4>
          <div class="ct-create-row">
            <label class="bk-field"><span>${esc(tr('kvm.snap.name'))}</span><input name="name" maxlength="64" class="tip" data-tip="${esc(tr('kvm.snap.nameTip'))}"></label>
          </div>
          <label class="bk-field"><span>${esc(tr('kvm.snap.description'))}</span><textarea name="description" rows="2" maxlength="1000" class="tip" data-tip="${esc(tr('kvm.snap.descriptionTip'))}"></textarea></label>
          <p class="form-hint" data-ks="pause" hidden></p>
          <div class="bk-form-actions"><button type="submit" class="btn btn-sm btn-primary tip" data-ks="go" data-tip="${esc(tr('kvm.snap.createTip'))}">${icon('snapshot')} ${esc(tr('kvm.snap.createGo'))}</button></div>
        </form>
        <div class="of-msg" data-ks="msg" role="status" aria-live="polite"></div>
        <ul class="kvm-steps" data-ks="steps"></ul>
      </div>`, [760, 620]);
    if (!root) return null;
    if (root.dataset.ksReady) { root._ksLoad && root._ksLoad(true); return root; }
    root.dataset.ksReady = '1';
    const $ = (k) => root.querySelector(`[data-ks="${k}"]`);
    const form = $('create');
    form.elements.name.value = defaultSnapshotName(domain);
    const st = { data: null, busy: false, pending: null };
    const say = (html) => { $('msg').innerHTML = html; };

    function controls() {
      const d = st.data || {};
      const op = canOperate(d.role);
      const createWhy = !op ? tr('kvm.operatorOnly')
        : (d.support || []).length ? (d.support || []).join('; ')
          : d.persistent === false ? tr('kvm.why.transient') : '';
      const go = $('go');
      go.disabled = st.busy || !st.data || !!st.data.error || !!createWhy;
      go.dataset.tip = createWhy || tr('kvm.snap.createTip');
      root.querySelectorAll('[data-ks-act]').forEach((b) => {
        b.disabled = st.busy || !op;
        if (!op) b.dataset.tip = tr('kvm.operatorOnly');
      });
    }

    function render() {
      const d = st.data;
      if (!d) return;
      if (d.error) {
        $('list').innerHTML = `<p>${errHtml(d.error)}</p>`;
        $('support').innerHTML = '';
        controls();
        return;
      }
      $('state').textContent = tr('kvm.snap.vmState', { name: domain, host, state: stateText(d.state) });
      $('support').innerHTML = (d.support || []).length
        ? `<div class="kvm-op-refusals">${icon('warn')} ${esc(tr('kvm.snap.cannotTake'))}${listHtml(d.support)}</div>` : '';
      const pause = $('pause');
      if (d.running) {
        pause.hidden = false;
        pause.textContent = tr('kvm.snap.pause', { mem: bytes((d.memory_kib || 0) * 1024) });
      } else { pause.hidden = true; pause.textContent = ''; }
      const snaps = d.snapshots || [];
      $('list').innerHTML = snaps.length ? `<table class="data-table kvm-snap-table"><thead><tr>
          <th>${esc(tr('kvm.snap.col.name'))}</th><th>${esc(tr('kvm.snap.col.created'))}</th><th>${esc(tr('kvm.snap.col.state'))}</th>
          <th>${esc(tr('kvm.snap.col.location'))}</th><th>${esc(tr('kvm.snap.col.parent'))}</th><th></th></tr></thead><tbody>
        ${snaps.map((s) => {
    const external = s.location === 'external';
    const acts = external ? dim(tr('kvm.snap.externalNoAction'))
      : `<button type="button" class="btn btn-sm btn-secondary tip" data-ks-act="revert" data-snap="${esc(s.name)}" data-tip="${esc(tr('kvm.snap.revertTip'))}">${icon('undo')} ${esc(tr('kvm.snap.revert'))}</button>
         <button type="button" class="btn btn-sm btn-danger tip" data-ks-act="delete" data-snap="${esc(s.name)}" data-tip="${esc(tr('kvm.snap.deleteTip'))}">${icon('trash')} ${esc(tr('kvm.snap.delete'))}</button>`;
    return `<tr data-ks-row="${esc(s.name)}"><td class="kvm-snap-name">${esc(s.name)} ${s.current ? badge('ok', tr('kvm.snap.current'), tr('kvm.snap.currentTip')) : ''}</td>
          <td>${esc(s.created || '–')}</td><td>${esc(takenLabel(s.state))}</td>
          <td>${esc(external ? tr('kvm.snap.external') : s.location === 'internal' ? tr('kvm.snap.internal') : (s.location || '–'))}</td>
          <td>${esc(s.parent || '–')}</td><td class="kvm-snap-acts">${acts}</td></tr>`;
  }).join('')}</tbody></table>` : `<p class="form-hint">${esc(tr('kvm.snap.none'))}</p>`;
      controls();
    }

    // `retry` : relecture juste après une action, filet de dernier recours.
    // Cause vue sur node2 (sshd 10.2p1) : deux lectures lancées ensemble sur
    // la même connexion SSH partagée ont fait tomber le sshd de l'hôte
    // (moniteur privsep désynchronisé) et la lecture en vol a rendu 502.
    // Depuis, le serveur rejoue une lecture coupée sur une connexion neuve
    // et la fenêtre enchaîne ses lectures ; ce second essai 2 s plus tard ne
    // sert plus que si les deux essais du serveur échouent.
    async function load(fresh, retry) {
      let d = await getJson(`${base(host, domain)}/snapshots${fresh ? '?fresh=1' : ''}`);
      if (retry && d.error && d.status === 502 && root.isConnected) {
        await new Promise((r) => setTimeout(r, 2000));
        d = await getJson(`${base(host, domain)}/snapshots?fresh=1`);
      }
      if (!root.isConnected) return;
      st.data = d;
      render();
    }
    root._ksLoad = load;

    function closeConfirm() {
      const c = $('confirm');
      c.hidden = true; c.innerHTML = ''; st.pending = null;
    }

    async function ask(act, snap) {
      const before = ((st.data || {}).snapshots || []).find((x) => x.name === snap) || {};
      // revenir à un instantané pris arrêté éteint une VM en marche : l'état
      // affiché peut dater (VM démarrée depuis), on le relit avant de parler
      if (act === 'revert') {
        await load(true);
        if (!root.isConnected) return;
      }
      const d = st.data || {};
      const s = (d.snapshots || []).find((x) => x.name === snap) || before;
      let text;
      if (act === 'revert') {
        text = tr('kvm.snap.revertConfirm', { name: domain, snapshot: snap });
        const takenOff = s.state === 'shutoff' || s.state === 'shut off';
        // état illisible : on prévient quand même
        if (takenOff && d.state !== 'shut off') {
          text += ` ${tr('kvm.snap.revertPowerOff', { name: domain })}`;
        }
      } else {
        text = tr('kvm.snap.deleteConfirm', { snapshot: snap });
        const children = (d.snapshots || []).filter((x) => x.parent === snap).map((x) => x.name);
        if (children.length) text += ` ${tr('kvm.snap.deleteChildren', { children: children.join(', ') })}`;
      }
      st.pending = { act, snap };
      const c = $('confirm');
      c.innerHTML = `<div class="bkg-confirm kvm-confirm" role="group" aria-label="${esc(text)}">
          <span>${icon('warn')} ${esc(text)}</span>
          <button type="button" class="btn btn-sm ${act === 'delete' ? 'btn-danger' : 'btn-primary'} tip" data-ks="yes" data-tip="${esc(act === 'revert' ? tr('kvm.snap.revertTip') : tr('kvm.snap.deleteTip'))}">${esc(act === 'revert' ? tr('kvm.snap.revertYes') : tr('kvm.snap.deleteYes'))}</button>
          <button type="button" class="btn btn-sm btn-secondary tip" data-ks="no" data-tip="${esc(tr('kvm.confirmNoTip'))}">${esc(tr('kvm.confirmNo'))}</button></div>`;
      c.hidden = false;
      c.querySelector('[data-ks="no"]').focus();
    }

    function doneText(act, snap, o) {
      if (act === 'create') return o.memory ? tr('kvm.snap.takenMemory', { snapshot: snap, s: o.elapsed_s }) : tr('kvm.snap.taken', { snapshot: snap, s: o.elapsed_s });
      if (act === 'revert') return tr('kvm.snap.reverted', { name: domain, snapshot: snap, state: stateText(o.state) });
      return tr('kvm.snap.deleted', { snapshot: snap });
    }

    async function run(act, snap, request) {
      st.busy = true;
      controls();
      const steps = $('steps');
      steps.innerHTML = '';
      say(esc(tr('kvm.snap.sending', { snapshot: snap })));
      try {
        const out = await request();
        say(esc(tr('bk.started', { id: out.action_id })));
        follow(out.action_id, (s) => stepLine(steps, s), (ok, r) => {
          const o = (r.result && r.result.output) || {};
          st.busy = false;
          if (ok) say(okHtml(doneText(act, snap, o)));
          else say(`${errHtml(failText(r, o))}${listHtml(o.refusals)}`);
          if (ok && act === 'create') form.elements.name.value = defaultSnapshotName(domain);
          // l'une après l'autre : pas de lectures simultanées de notre fait
          load(true, true).then(refreshList, refreshList);
        });
      } catch (e) {
        st.busy = false;
        say(errHtml(e.message));
        controls();
      }
    }

    root.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b || b.disabled || !root.contains(b)) return;
      const k = b.dataset.ks;
      if (k === 'refresh') { load(true); return; }
      if (k === 'no') { closeConfirm(); return; }
      if (k === 'yes') {
        const p = st.pending;
        closeConfirm();
        if (!p) return;
        const url = `${base(host, domain)}/snapshots/${enc(p.snap)}`;
        run(p.act, p.snap, () => (p.act === 'revert' ? send('POST', `${url}/revert`, {}) : send('DELETE', url, {})));
        return;
      }
      if (b.dataset.ksAct) ask(b.dataset.ksAct, b.dataset.snap);
    });
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      if ($('go').disabled) return;
      const name = form.elements.name.value.trim();
      if (!SNAPSHOT_RE.test(name)) { say(errHtml(tr('kvm.snap.badName'))); return; }
      const description = form.elements.description.value;
      run('create', name, () => send('POST', `${base(host, domain)}/snapshots`, { name, description }));
    });
    controls();
    load(true);
    return root;
  }

  // -- Cloner -------------------------------------------------------------------------
  function openClone(host, domain) {
    const root = panel('clone', host, domain, tr('kvm.clone.title', { name: domain, host }), 'clone', `
      <form class="of-form kvm-vmw kvm-clone" autocomplete="off" novalidate>
        <p class="form-hint">${esc(tr('kvm.clone.hint'))}</p>
        <div class="ct-create-row">
          <label class="bk-field"><span>${esc(tr('kvm.clone.newName'))}</span><input name="name" maxlength="255" class="tip" data-tip="${esc(tr('kvm.clone.newNameTip'))}"></label>
        </div>
        <div class="bk-form-actions">
          <button type="button" class="btn btn-sm btn-secondary tip" data-kc="preview" data-tip="${esc(tr('kvm.clone.previewTip'))}">${icon('preview')} ${esc(tr('kvm.clone.preview'))}</button>
        </div>
        <div data-kc="plan"></div>
        <div class="bk-form-actions"><button type="submit" class="btn btn-sm btn-primary tip" data-kc="go" disabled data-tip="${esc(tr('kvm.clone.goTip'))}">${icon('clone')} ${esc(tr('kvm.clone.go'))}</button></div>
        <div class="of-msg" data-kc="msg" role="status" aria-live="polite"></div>
        <ul class="kvm-steps" data-kc="steps"></ul>
        <div data-kc="result"></div>
      </form>`, [780, 700]);
    if (!root) return null;
    if (root.dataset.kcReady) return root;
    root.dataset.kcReady = '1';
    const $ = (k) => root.querySelector(`[data-kc="${k}"]`);
    const form = root.querySelector('form');
    form.elements.name.value = `${domain}-copy`.slice(0, 255);
    const st = { plan: null, planName: null, busy: false, done: false, seq: 0 };
    const say = (html) => { $('msg').innerHTML = html; };

    function controls() {
      const go = $('go');
      const name = form.elements.name.value.trim();
      let why = '';
      if (!st.plan || st.planName !== name) why = tr('kvm.clone.needPreview');
      else if (st.plan.error) why = st.plan.error;
      else if (!canOperate(st.plan.role)) why = tr('kvm.operatorOnly');
      else if (!st.plan.possible) why = tr('kvm.clone.refusedTip');
      go.disabled = st.busy || st.done || !!why;
      go.dataset.tip = why || tr('kvm.clone.goTip');
      $('preview').disabled = st.busy || st.done;
    }

    function planHtml(d) {
      if (d.error) return `<p>${errHtml(d.error)}</p>`;
      const p = d.plan || {};
      const disks = (p.disks || []).map((x) => `<tr><td>${esc(x.target)}</td><td class="kvm-path">${esc(x.source || '–')}</td>
          <td class="kvm-path">${esc(x.destination || '–')}</td><td>${esc(x.mode === 'pool' ? tr('kvm.clone.inPool', { pool: x.pool || '?' }) : tr('kvm.clone.inDir'))}</td>
          <td>${esc(bytes(x.needed))}</td></tr>`).join('');
      const shared = (p.shared || []).map((x) => `<li>${esc(x.target)}: <span class="kvm-path">${esc(x.source || '–')}</span> ${dim(x.reason || '')}</li>`).join('');
      const space = (p.space || []).map((x) => `<li><span class="kvm-path">${esc(x.where)}</span>: ${esc(tr('kvm.clone.spaceLine', { needed: bytes(x.needed), available: bytes(x.available) }))}</li>`).join('');
      const regen = [...(p.regenerated || [])];
      regen.unshift(tr('kvm.clone.regenUuid'));
      if ((p.nics || []).length) regen.push(tr('kvm.clone.regenMacs', { n: p.nics.length }));
      if (p.nvram) regen.push(tr('kvm.clone.regenNvram'));
      const warns = [];
      if (p.uefi_entries_lost) warns.push(tr('kvm.clone.uefiLost'));
      if (p.tpm_state_lost) warns.push(tr('kvm.clone.tpmLost'));
      return `<div class="kvm-op-plan">
        ${(p.refusals || []).length ? `<div class="kvm-op-refusals">${icon('fail')} ${esc(tr('kvm.clone.refused'))}${listHtml(p.refusals)}</div>` : `<p>${okHtml(tr('kvm.clone.possible', { name: p.new_name || '' }))}</p>`}
        <h4>${esc(tr('kvm.clone.disks'))}</h4>
        ${disks ? `<table class="data-table kvm-clone-disks"><thead><tr><th>${esc(tr('kvm.clone.col.target'))}</th><th>${esc(tr('kvm.clone.col.source'))}</th>
          <th>${esc(tr('kvm.clone.col.destination'))}</th><th>${esc(tr('kvm.clone.col.where'))}</th><th>${esc(tr('kvm.clone.col.size'))}</th></tr></thead><tbody>${disks}</tbody></table>`
    : `<p class="form-hint">${esc(tr('kvm.clone.noDisk'))}</p>`}
        ${shared ? `<h4>${esc(tr('kvm.clone.shared'))}</h4><ul class="kvm-op-list">${shared}</ul>` : ''}
        ${space ? `<h4>${esc(tr('kvm.clone.space'))}</h4><ul class="kvm-op-list">${space}</ul>` : ''}
        <h4>${esc(tr('kvm.clone.regenerated'))}</h4>${listHtml(regen)}
        ${warns.length ? `<div class="kvm-op-warn">${icon('warn')} ${esc(tr('kvm.clone.warnings'))}${listHtml(warns)}</div>` : ''}
      </div>`;
    }

    async function preview() {
      const name = form.elements.name.value.trim();
      if (!name) { say(errHtml(tr('kvm.clone.needName'))); return; }
      const seq = ++st.seq;
      say(esc(tr('kvm.clone.previewing')));
      $('plan').innerHTML = '';
      st.plan = null;
      controls();
      const d = await getJson(`${base(host, domain)}/clone-plan?name=${enc(name)}&fresh=1`);
      if (!root.isConnected || seq !== st.seq) return;
      st.plan = d; st.planName = name;
      say('');
      $('plan').innerHTML = planHtml(d);
      controls();
    }

    function resultHtml(o) {
      const disks = (o.disks || []).map((x) => `${x.target}: ${x.destination}`);
      const notes = [...(o.regenerated || [])];
      if (o.uefi_entries_lost) notes.push(tr('kvm.clone.uefiLost'));
      if (o.tpm_state_lost) notes.push(tr('kvm.clone.tpmLost'));
      return `<div class="kvm-op-plan">
        <p>${esc(tr('kvm.clone.resultLine', { name: o.name || '', uuid: o.uuid || '?', state: stateText(o.state) }))}</p>
        ${disks.length ? `<h4>${esc(tr('kvm.clone.disks'))}</h4>${listHtml(disks)}` : ''}
        ${(o.macs || []).length ? `<h4>${esc(tr('kvm.clone.macs'))}</h4>${listHtml(o.macs)}` : ''}
        ${notes.length ? `<h4>${esc(tr('kvm.clone.regenerated'))}</h4>${listHtml(notes)}` : ''}
        ${(o.warnings || []).length ? `<div class="kvm-op-warn">${icon('warn')} ${esc(tr('kvm.clone.warnings'))}${listHtml(o.warnings)}</div>` : ''}
      </div>`;
    }

    form.elements.name.addEventListener('input', controls);
    $('preview').addEventListener('click', preview);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      controls();
      if ($('go').disabled) return;
      const name = st.planName;
      st.busy = true;
      controls();
      const steps = $('steps');
      steps.innerHTML = '';
      $('result').innerHTML = '';
      say(esc(tr('kvm.clone.sending', { name })));
      try {
        const out = await send('POST', `${base(host, domain)}/clone`, { name });
        say(esc(tr('bk.started', { id: out.action_id })));
        follow(out.action_id, (s) => stepLine(steps, s), (ok, r) => {
          const o = (r.result && r.result.output) || {};
          st.busy = false;
          if (ok) {
            st.done = true;
            say(okHtml(tr('kvm.clone.done', { name, source: domain, s: o.elapsed_s })));
            $('result').innerHTML = resultHtml(o);
          } else {
            const left = (o.leftovers || []).map((x) => (typeof x === 'string' ? x : (x.path || x.name || JSON.stringify(x))));
            let extra = '';
            if (o.created) extra += `<p>${esc(tr('kvm.clone.createdAnyway', { name: o.created }))}</p>`;
            if (left.length) extra += `<div class="kvm-op-warn">${icon('warn')} ${esc(tr('kvm.clone.leftovers'))}${listHtml(left)}</div>`;
            say(`${errHtml(failText(r, o))}${listHtml(o.refusals)}${extra}`);
          }
          controls();
          refreshList();
        });
      } catch (err) {
        st.busy = false;
        say(errHtml(err.message));
        controls();
      }
    });
    controls();
    preview();
    return root;
  }

  // -- Supprimer ----------------------------------------------------------------------
  function openDelete(host, domain) {
    const root = panel('delete', host, domain, tr('kvm.del.title', { name: domain, host }), 'trash', `
      <form class="of-form kvm-vmw kvm-delete" autocomplete="off" novalidate>
        <p class="form-hint">${esc(tr('kvm.del.hint'))}</p>
        <label class="kvm-check"><input type="checkbox" name="storage" class="tip" data-tip="${esc(tr('kvm.del.storageTip'))}"> ${esc(tr('kvm.del.storage'))}</label>
        <div data-kd="plan"><p class="form-hint">${esc(tr('common.loading'))}</p></div>
        <label class="bk-field"><span data-kd="typeLabel"></span><input name="confirm" maxlength="255" class="tip" data-tip="${esc(tr('kvm.del.typeTip'))}"></label>
        <div class="bk-form-actions"><button type="submit" class="btn btn-sm btn-danger tip" data-kd="go" disabled data-tip="${esc(tr('kvm.del.goTip'))}">${icon('trash')} ${esc(tr('kvm.del.go'))}</button></div>
        <div class="of-msg" data-kd="msg" role="status" aria-live="polite"></div>
        <ul class="kvm-steps" data-kd="steps"></ul>
        <div data-kd="result"></div>
      </form>`, [780, 720]);
    if (!root) return null;
    if (root.dataset.kdReady) return root;
    root.dataset.kdReady = '1';
    const $ = (k) => root.querySelector(`[data-kd="${k}"]`);
    const form = root.querySelector('form');
    $('typeLabel').textContent = tr('kvm.del.type', { name: domain });
    // `ack` : fichiers illisibles cochés, envoyés tels quels à l'aperçu ET à
    // la suppression (la même liste que celle de l'aperçu montré)
    const st = { plan: null, ack: [], loading: false, busy: false, done: false, seq: 0 };
    const say = (html) => { $('msg').innerHTML = html; };

    function controls() {
      const go = $('go');
      const p = st.plan;
      let why = '';
      if (st.loading || !p) why = tr('kvm.del.needPreview');
      else if (p.error) why = p.error;
      else if ((p.role || 'viewer') !== 'admin') why = tr('kvm.adminOnly');
      else if (!p.possible) why = tr('kvm.del.refusedTip');
      else if (form.elements.confirm.value !== domain) why = tr('kvm.del.typeTip');
      go.disabled = st.busy || st.done || !!why;
      go.dataset.tip = why || tr('kvm.del.goTip');
      form.elements.storage.disabled = st.busy || st.done;
      root.querySelectorAll('[data-kd-ack]').forEach((c) => { c.disabled = st.busy || st.done || st.loading; });
    }

    const diskPath = (x) => x.path || x.source || x.volume || x.target || '?';

    function planHtml(d) {
      if (d.error) return `<p>${errHtml(d.error)}</p>`;
      const p = d.plan || {};
      const del = (p.delete || []).map((x) => `<li><strong>${esc(x.target || '?')}</strong> <span class="kvm-path">${esc(diskPath(x))}</span></li>`).join('');
      const keep = (p.keep || []).map((x) => `<li><strong>${esc(x.target || '?')}</strong> <span class="kvm-path">${esc(diskPath(x))}</span> ${dim(x.reason || '')}</li>`).join('');
      const acked = new Set(st.ack);
      const unread = (p.unreadable || []).map((f) => `<li><label class="kvm-check"><input type="checkbox" data-kd-ack="${esc(f)}"${acked.has(f) ? ' checked' : ''} class="tip"
          data-tip="${esc(tr('kvm.del.ackTip'))}"> <span class="kvm-path">${esc(f)}</span></label></li>`).join('');
      const notes = [];
      if (p.internal_snapshots) notes.push(tr('kvm.del.snapshots', { n: p.internal_snapshots }));
      if (p.managed_save_lost) notes.push(tr('kvm.del.managedSave'));
      if (p.tpm_state === 'removed') notes.push(tr('kvm.del.tpmRemoved'));
      if (p.tpm_state === 'kept') notes.push(tr('kvm.del.tpmKept'));
      if (p.nvram) notes.push(tr('kvm.del.nvram', { path: typeof p.nvram === 'string' ? p.nvram : '?' }));
      notes.push(...(p.notes || []));
      return `<div class="kvm-op-plan">
        <p>${esc(tr('kvm.del.vmLine', { name: p.name || domain, host, state: stateText(p.state) }))}</p>
        ${(p.refusals || []).length ? `<div class="kvm-op-refusals">${icon('fail')} ${esc(tr('kvm.del.refused'))}${listHtml(p.refusals)}</div>` : ''}
        ${p.remove_storage ? `<h4>${esc(tr('kvm.del.deleted'))}</h4>${del ? `<ul class="kvm-op-list" data-kd-list="delete">${del}</ul>` : `<p class="form-hint">${esc(tr('kvm.del.noneDeleted'))}</p>`}` : ''}
        ${keep ? `<h4>${esc(tr('kvm.del.kept'))}</h4><ul class="kvm-op-list" data-kd-list="keep">${keep}</ul>` : ''}
        ${unread ? `<div class="kvm-op-warn" data-kd-list="unreadable">${icon('warn')} ${esc(tr('kvm.del.unreadable'))}<ul class="kvm-op-list">${unread}</ul></div>` : ''}
        ${notes.length ? `<h4>${esc(tr('kvm.del.notes'))}</h4>${listHtml(notes)}` : ''}
      </div>`;
    }

    async function preview(fresh) {
      const seq = ++st.seq;
      st.loading = true;
      controls();
      const q = new URLSearchParams();
      if (form.elements.storage.checked) {
        q.set('remove_storage', '1');
        st.ack.forEach((f) => q.append('acknowledge', f));
      }
      if (fresh) q.set('fresh', '1');
      const d = await getJson(`${base(host, domain)}/delete-plan?${q.toString()}`);
      if (!root.isConnected || seq !== st.seq) return;
      st.loading = false;
      st.plan = d.error ? { error: d.error } : Object.assign({}, d, { ack: [...st.ack] });
      $('plan').innerHTML = planHtml(d);
      controls();
    }

    function resultHtml(o) {
      const lines = (arr, f) => (arr || []).map(f);
      const parts = [];
      const del = lines(o.deleted, (x) => `${x.target || '?'}: ${diskPath(x)}`);
      const kept = lines(o.kept, (x) => `${x.target || '?'}: ${diskPath(x)} (${x.reason || ''})`);
      const skipped = lines(o.skipped, (x) => `${x.target || '?'}: ${x.path || ''} (${x.reason || ''})`);
      const left = lines(o.leftovers, (x) => `${x.target || '?'}: ${diskPath(x)} (${x.error || ''})`);
      if (del.length) parts.push(`<h4>${esc(tr('kvm.del.resDeleted'))}</h4>${listHtml(del)}`);
      if (kept.length) parts.push(`<h4>${esc(tr('kvm.del.kept'))}</h4>${listHtml(kept)}`);
      if (skipped.length) parts.push(`<h4>${esc(tr('kvm.del.skipped'))}</h4>${listHtml(skipped)}`);
      if (left.length) parts.push(`<div class="kvm-op-warn">${icon('warn')} ${esc(tr('kvm.del.leftovers'))}${listHtml(left)}</div>`);
      if ((o.notes || []).length) parts.push(`<h4>${esc(tr('kvm.del.notes'))}</h4>${listHtml(o.notes)}`);
      return `<div class="kvm-op-plan">${parts.join('')}</div>`;
    }

    form.elements.storage.addEventListener('change', () => { st.ack = []; preview(true); });
    form.elements.confirm.addEventListener('input', controls);
    root.addEventListener('change', (e) => {
      const c = e.target.closest('[data-kd-ack]');
      if (!c) return;
      const f = c.dataset.kdAck;
      st.ack = st.ack.filter((x) => x !== f);
      if (c.checked) st.ack.push(f);
      preview(true);
    });
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      controls();
      if ($('go').disabled) return;
      const p = st.plan;
      const body = {
        confirm: form.elements.confirm.value,
        expect_delete: p.plan.digest,
        remove_storage: !!p.plan.remove_storage,
        acknowledge_unreadable: p.plan.remove_storage ? p.ack : [],
      };
      st.busy = true;
      controls();
      const steps = $('steps');
      steps.innerHTML = '';
      $('result').innerHTML = '';
      say(esc(tr('kvm.del.sending', { name: domain })));
      try {
        const out = await send('DELETE', base(host, domain), body);
        say(esc(tr('bk.started', { id: out.action_id })));
        follow(out.action_id, (s) => stepLine(steps, s), (ok, r) => {
          const o = (r.result && r.result.output) || {};
          st.busy = false;
          if (ok) {
            st.done = true;
            say(okHtml(tr('kvm.del.done', { name: domain, host })));
            $('result').innerHTML = resultHtml(o);
          } else if (o.kind === 'plan-changed') {
            say(`${errHtml(tr('kvm.del.planChanged'))}`);
            form.elements.confirm.value = '';
            // aperçu d'abord, liste ensuite (lectures enchaînées)
            Promise.resolve(preview(true)).finally(() => { controls(); refreshList(); });
            return;
          } else {
            const pending = (o.pending || []).length
              ? `<div class="kvm-op-warn">${icon('warn')} ${esc(tr('kvm.del.pending'))}${listHtml(o.pending)}</div>` : '';
            say(`${errHtml(failText(r, o))}${listHtml(o.refusals)}${pending}`);
          }
          controls();
          refreshList();
        });
      } catch (err) {
        st.busy = false;
        say(errHtml(err.message));
        controls();
      }
    });
    controls();
    preview(true);
    return root;
  }

  return { openSnapshots, openClone, openDelete, defaultSnapshotName };
})();

if (typeof window !== 'undefined') {
  window.KVMVm = KVMVm;
  // la console d'une VM KVM (vm-console.js) ouvre ses instantanés par ce nom
  window.KVMSnapshots = { open: (host, domain) => KVMVm.openSnapshots(host, domain) };
}
