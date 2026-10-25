/**
 * harvester-ops : mettre à jour Harvester (v1.69.0)
 *
 * Une fenêtre par cluster, ouverte depuis l'aperçu ou le réglage
 * server-version (comme le bouton Upgrade de Harvester) :
 * - une mise à jour en cours ou finie : étapes horodatées, téléchargement de
 *   l'ISO, état de chaque hôte (reprise d'un hôte en pause), cause d'un
 *   échec, journaux, « Dismiss », abandon tant que Harvester l'accepte ;
 * - sinon, le formulaire : une version (éligibilité dite avant, alors que
 *   Harvester ne la vérifie qu'après avoir téléchargé l'ISO) ou un ISO du
 *   magasin de la console servi au cluster (chemin airgap), options,
 *   pré-contrôles, notes de version et case « j'ai lu ».
 * Le lancement est une action suivie jusqu'au bout dans le dock.
 */
const Upgrade = (() => {
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const enc = encodeURIComponent;
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 13) => (window.Icons ? Icons.svg(n, { size }) : '');
  const WINS = new Map();
  const when = (iso) => { try { return iso ? new Date(iso).toLocaleString() : ''; } catch { return iso || ''; } };
  const gib = (n) => `${(Number(n || 0) / 2 ** 30).toFixed(1)} GiB`;

  async function call(method, url, body) {
    const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' },
                                 body: body ? JSON.stringify(body) : undefined });
    let d = {};
    try { d = await r.json(); } catch { /* sans corps */ }
    if (!r.ok) throw new Error(d.hint || d.error || `HTTP ${r.status}`);
    return d;
  }

  function follow(id, into, text, onDone) {
    if (window.Dock && Dock.poll) Dock.poll();
    if (window.VMActions && VMActions.follow) VMActions.follow(id, into, text, onDone);
    else if (into) into.textContent = tr('bk.started', { id });
  }

  // clés littérales (contrôle de parité i18n)
  const STEP = () => ({
    LogReady: tr('upg.step.log'), ImageReady: tr('upg.step.image'), RepoReady: tr('upg.step.repo'),
    NodesPrepared: tr('upg.step.prepare'), SystemServicesUpgraded: tr('upg.step.services'),
    NodesUpgraded: tr('upg.step.nodes'), Completed: tr('upg.step.done'),
  });
  const CHECK = () => ({
    running: tr('upg.chk.running'), cleanup: tr('upg.chk.cleanup'), 'nodes-ready': tr('upg.chk.nodesReady'),
    'nodes-schedulable': tr('upg.chk.nodesSched'), volumes: tr('upg.chk.volumes'), backups: tr('upg.chk.backups'),
    schedules: tr('upg.chk.schedules'), addons: tr('upg.chk.addons'), charts: tr('upg.chk.charts'),
  });

  const BODY = () => `<div class="upg-win">
        <div class="upg-head" data-upg="head"></div>
        <div class="res-feedback" data-upg="feedback"></div>
        <div data-upg="body"><p class="form-hint">${esc(tr('common.loading'))}</p></div></div>`;

  /** Branche le contenu (même rendu dans la fenêtre et dans l'onglet). */
  function attach(id, cluster, root) {
    const w = { id, cluster, root, data: null, source: 'version', dirty: false };
    WINS.set(id, w);
    root.addEventListener('click', (e) => onClick(w, e));
    root.addEventListener('change', (e) => onChange(w, e));
    root.addEventListener('input', () => { w.dirty = true; });
    w.timer = setInterval(() => {
      if (document.hidden || !document.body.contains(root)) return;
      if ((w.data && w.data.running) || !w.dirty) load(w);
    }, 8000);
    load(w);
    return w;
  }

  function open(cluster) {
    if (!cluster) return;
    const id = `upgrade-${cluster}`;
    const panel = FloatingPanels.open({
      id, icon: 'upload', width: 820, height: 680, title: `${tr('upg.title')} · ${cluster}`,
      restoreSpec: { type: 'harvester-upgrade', args: { cluster } },
      onClose: () => { const w = WINS.get(id); if (w && w.timer) clearInterval(w.timer); WINS.delete(id); },
      bodyHtml: BODY(),
    });
    const root = panel.el;
    if (root.dataset.upgReady) return;
    root.dataset.upgReady = '1';
    attach(id, cluster, root);
  }

  // 2.0.0 : Paramètres > Mise à jour d'un hôte, le même contenu dans la page
  const INLINE = 'upgrade-inline';
  /** Monte le contenu dans `host` pour ce cluster (remplace le précédent). */
  function mount(cluster, host) {
    unmount();
    if (!cluster || !host) return null;
    host.innerHTML = BODY();
    return attach(INLINE, cluster, host.firstElementChild);
  }
  /** Quitte l'onglet : la minuterie s'arrête, rien ne lit plus en fond. */
  function unmount() {
    const w = WINS.get(INLINE);
    if (!w) return;
    clearInterval(w.timer);
    WINS.delete(INLINE);
  }

  async function load(w) {
    let d = null;
    try { d = await call('GET', `/api/upgrade/${enc(w.cluster)}`); } catch (e) { d = { error: e.message }; }
    w.data = d;
    render(w);
  }

  function say(w, html) { w.root.querySelector('[data-upg="feedback"]').innerHTML = html; }

  function render(w) {
    const d = w.data;
    const head = w.root.querySelector('[data-upg="head"]');
    const body = w.root.querySelector('[data-upg="body"]');
    if (!d || d.error || d.unreachable) {
      head.innerHTML = '';
      body.innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc((d && d.error) || tr('fabric.unreachable'))}</div></div>`;
      return;
    }
    head.innerHTML = `${icon('info')} ${esc(tr('upg.current'))} <strong>${esc(d.current || '?')}</strong>
      ${d.running ? `<span class="badge info">${esc(tr('upg.inProgress'))}</span>` : ''}`;
    const u = d.upgrade;
    const showProgress = u && (d.running || !u.dismissed);
    const keepForm = w.dirty && body.querySelector('[data-upg="form"]');
    let html = showProgress ? progress(w, u) : '';
    if (!d.running) html += keepForm ? '' : form(w, d);
    if (keepForm) {
      // une saisie en cours n'est jamais effacée : seule la progression se redessine
      const prog = body.querySelector('[data-upg="progress"]');
      if (prog && showProgress) prog.outerHTML = progress(w, u);
      return;
    }
    body.innerHTML = html;
    syncForm(w);
  }

  function stepRow(c) {
    const st = c.status === 'True' ? ['ok', 'ok'] : c.status === 'False' ? (c.reason === 'Disabled' ? ['dim', 'info'] : ['fail', 'fail'])
      : c.status === 'Unknown' ? ['info', 'running'] : ['dim', 'pending'];
    return `<li class="upg-step upg-${st[0]}">${icon(st[1])} <strong>${esc(STEP()[c.type] || c.type)}</strong>
      ${c.time ? `<span class="res-dim">${esc(when(c.time))}</span>` : ''}
      ${c.status === 'False' && c.reason !== 'Disabled' && (c.message || c.reason) ? `<div class="res-error">${esc(c.message || c.reason)}</div>` : ''}
      ${c.reason === 'Disabled' ? `<span class="res-dim">${esc(tr('upg.logsOff'))}</span>` : ''}</li>`;
  }

  function progress(w, u) {
    const img = u.image_progress != null && !u.conditions.some(c => c.type === 'ImageReady' && c.status === 'True')
      ? `<div class="upg-bar tip" data-tip="${esc(tr('upg.t.iso'))}"><span>${esc(tr('upg.isoDownload'))} ${esc(u.image_progress)} %</span>
          <div class="hs-gauge-bar"><i style="width:${Math.min(100, Number(u.image_progress) || 0)}%"></i></div></div>` : '';
    const result = u.completed === true ? `<div class="sto-finding sev-info"><div class="sto-finding-title">${icon('ok')} ${esc(tr('upg.succeeded', { to: u.target || u.version }))}</div></div>`
      : u.completed === false ? `<div class="sto-finding sev-critical"><div class="sto-finding-title">${icon('fail')} ${esc(tr('upg.failed'))}</div>
          <div class="sto-finding-facts">${esc(u.failure || '')}</div></div>` : '';
    const nodes = u.nodes.length ? `<table class="data-table"><thead><tr><th>${esc(tr('upg.host'))}</th><th>${esc(tr('res.col.state'))}</th><th></th></tr></thead><tbody>
        ${u.nodes.map(n => `<tr data-node="${esc(n.name)}"><td><strong>${esc(n.name)}</strong></td>
          <td><span class="badge ${n.state === 'Succeeded' ? 'ok' : n.state === 'Failed' ? 'fail' : n.state === 'Pending' ? 'dim' : 'info'}">${esc(n.state)}</span>
            ${n.message ? `<div class="res-dim">${esc(n.message)}</div>` : ''}</td>
          <td class="tpl-acts">${n.paused && u.completed === null ? `<button type="button" class="btn btn-sm btn-primary tip needs-admin" data-upg="resume"
            data-tip="${esc(tr('upg.t.resume'))}">${icon('play')} ${esc(tr('upg.resume'))}</button>` : ''}</td></tr>`).join('')}</tbody></table>` : '';
    const repo = u.repo && u.repo.harvester ? `<p class="res-dim">${esc(tr('upg.repo', { os: u.repo.os, k8s: u.repo.kubernetes, rancher: u.repo.rancher }))}</p>` : '';
    return `<div class="card upg-card" data-upg="progress">
        <div class="tpl-head">${icon('upload')} <span class="tpl-title"><strong>${esc(u.name)}</strong>
          <span class="res-dim">${esc(tr('upg.fromTo', { from: u.previous || '?', to: u.target || u.version || u.image }))} · ${esc(when(u.created))}</span></span>
          <span class="tpl-acts">
            ${u.notes ? `<a class="btn btn-sm btn-secondary tip" href="${esc(u.notes)}" target="_blank" rel="noopener noreferrer" data-tip="${esc(tr('upg.t.notes'))}">${icon('doc')} ${esc(tr('upg.notes'))}</a>` : ''}
            ${u.log_name ? `<button type="button" class="btn btn-sm btn-secondary tip needs-admin" data-upg="logs" data-tip="${esc(tr('upg.t.logs'))}">${icon('download')} ${esc(tr('upg.logs'))}</button>` : ''}
            ${u.completed === null ? `<button type="button" class="btn btn-sm btn-secondary tip needs-admin" data-upg="follow" data-tip="${esc(tr('upg.t.follow'))}">${icon('activity')} ${esc(tr('upg.follow'))}</button>` : ''}
            ${u.can_abort ? `<button type="button" class="btn btn-sm btn-danger tip needs-admin" data-upg="abort" data-tip="${esc(tr('upg.t.abort'))}">${icon('stop')} ${esc(tr('upg.abort'))}</button>` : ''}
            ${u.can_dismiss ? `<button type="button" class="btn btn-sm btn-secondary tip needs-admin" data-upg="dismiss" data-tip="${esc(tr('upg.t.dismiss'))}">${icon('ok')} ${esc(tr('upg.dismiss'))}</button>` : ''}
          </span></div>
        ${result}${img}
        <ol class="upg-steps">${u.conditions.map(stepRow).join('')}</ol>
        ${nodes}${repo}
        ${u.completed === null ? `<p class="form-hint">${esc(u.single_node ? tr('upg.hintSingle') : tr('upg.hintMulti'))}</p>` : ''}
      </div>`;
  }

  function form(w, d) {
    const vers = d.versions.map(v => [v.name, `${v.name}${v.release_date ? ` (${v.release_date})` : ''}${v.eligible === false ? ` · ${v.reason}` : ''}`, v.eligible === false]);
    const isos = d.isos.map(i => [i.name, `${i.name} · ${i.release.harvester} · ${gib(i.size)}${i.eligible === false ? ` · ${i.reason}` : ''}`, i.eligible === false]);
    const opts = (list) => list.map(([v, l, dis]) => `<option value="${esc(v)}" ${dis ? 'disabled' : ''}>${esc(l)}</option>`).join('');
    const checks = d.prechecks.map(c => `<li class="${c.ok ? 'upg-ok' : 'upg-fail'}">${icon(c.ok ? 'ok' : 'warn')} ${esc(CHECK()[c.key] || c.key)}
        ${!c.ok && c.text ? `<span class="res-dim">${esc(c.text)}</span>` : ''}</li>`).join('');
    const blocked = d.prechecks.some(c => !c.ok);
    return `<form class="of-form card upg-card" data-upg="form" autocomplete="off">
        <h4 class="hs-sub">${esc(tr('upg.new'))}</h4>
        <p class="form-hint">${esc(tr('upg.hint'))}</p>
        <div class="upg-sources">
          <label class="bk-radio tip" data-tip="${esc(tr('upg.t.srcVersion'))}"><input type="radio" name="source" value="version" ${w.source === 'version' ? 'checked' : ''}> <span>${esc(tr('upg.srcVersion'))}</span></label>
          <label class="bk-radio tip" data-tip="${esc(tr('upg.t.srcIso'))}"><input type="radio" name="source" value="iso" ${w.source === 'iso' ? 'checked' : ''}> <span>${esc(tr('upg.srcIso'))}</span></label>
        </div>
        <div data-when="version">
          ${vers.length ? `<label class="bk-field"><span>${esc(tr('upg.version'))}</span><select name="version" class="tip" data-tip="${esc(tr('upg.t.version'))}">
              <option value="">${esc(tr('upg.pick'))}</option>${opts(vers)}</select></label>`
            : `<p class="form-hint">${esc(tr('upg.noVersion'))}</p>`}
          <details class="upg-versions"><summary class="tip" data-tip="${esc(tr('upg.t.manage'))}">${esc(tr('upg.manage'))}</summary>
            ${d.versions.length ? `<ul class="upg-vlist">${d.versions.map(v => `<li data-version="${esc(v.name)}"><code>${esc(v.name)}</code>
                <span class="res-dim">${esc(v.iso_url)}</span>
                <button type="button" class="btn btn-sm btn-danger tip needs-admin" data-upg="vdel" data-tip="${esc(tr('upg.t.vdel'))}">${icon('trash')}</button></li>`).join('')}</ul>` : ''}
            <label class="bk-field"><span>${esc(tr('upg.vurl'))}</span>
              <input name="vurl" type="url" placeholder="https://releases.rancher.com/harvester/v1.9.0/version.yaml" class="tip" data-tip="${esc(tr('upg.t.vurl'))}"></label>
            <button type="button" class="btn btn-sm btn-secondary tip needs-admin" data-upg="vadd" data-tip="${esc(tr('upg.t.vadd'))}">${icon('add')} ${esc(tr('upg.vadd'))}</button>
            ${d.checker ? `<p class="form-hint">${icon('info')} ${esc(tr('upg.checkerHint'))}</p>` : ''}
          </details>
        </div>
        <div data-when="iso" hidden>
          ${isos.length ? `<label class="bk-field"><span>${esc(tr('upg.iso'))}</span><select name="iso" class="tip" data-tip="${esc(tr('upg.t.iso2'))}">
              <option value="">${esc(tr('upg.pick'))}</option>${opts(isos)}</select></label>
            <label class="bk-field"><span>SHA-512</span><input name="checksum" class="tip" maxlength="128" data-tip="${esc(tr('upg.t.checksum'))}"></label>`
            : `<p class="form-hint">${esc(tr('upg.noIso'))}</p>`}
        </div>
        <label class="bk-check tip" data-tip="${esc(tr('upg.t.log'))}"><input type="checkbox" name="log" checked> <span>${esc(tr('upg.log'))}</span></label>
        <label class="bk-check tip" data-tip="${esc(tr('upg.t.skip'))}"><input type="checkbox" name="skip"> <span>${esc(tr('upg.skip'))}</span></label>
        <h4 class="hs-sub">${esc(tr('upg.checks'))}</h4>
        <ul class="upg-checks">${checks}</ul>
        ${blocked ? `<p class="form-hint">${esc(tr('upg.blocked'))}</p>` : ''}
        <div class="sto-finding sev-action"><div class="sto-finding-title">${icon('warn')} ${esc(tr('upg.warning'))}</div>
          <p class="upg-notes" data-upg="notes"></p>
          <label class="bk-check tip" data-tip="${esc(tr('upg.t.read'))}"><input type="checkbox" name="read"> <span>${esc(tr('upg.read'))}</span></label></div>
        <div class="bk-form-actions"><button type="button" class="btn btn-sm btn-primary tip needs-admin" data-upg="start" disabled
          data-tip="${esc(tr('upg.t.start'))}">${icon('upload')} ${esc(tr('upg.start'))}</button></div>
        <div class="of-msg" role="status"></div></form>`;
  }

  function val(f, n) { return f.querySelector(`[name="${n}"]`)?.value || ''; }

  function syncForm(w) {
    const f = w.root.querySelector('[data-upg="form"]');
    if (!f) return;
    const src = f.querySelector('[name="source"]:checked')?.value || 'version';
    w.source = src;
    f.querySelectorAll('[data-when]').forEach(el => { el.hidden = el.dataset.when !== src; });
    const d = w.data;
    let target = '';
    if (src === 'version') target = val(f, 'version');
    else {
      const i = d.isos.find(x => x.name === val(f, 'iso'));
      target = i ? i.release.harvester : '';
      const sum = f.querySelector('[name="checksum"]');
      if (sum && i && !sum.value && i.sha512) sum.value = i.sha512;
    }
    const notes = f.querySelector('[data-upg="notes"]');
    notes.innerHTML = target && /^v\d/.test(target)
      ? `<a href="https://github.com/harvester/harvester/releases/tag/${esc(target)}" target="_blank" rel="noopener noreferrer" class="tip"
          data-tip="${esc(tr('upg.t.notes'))}">${icon('doc')} ${esc(tr('upg.notesFor', { v: target }))}</a>` : '';
    const read = f.querySelector('[name="read"]');
    f.querySelector('[data-upg="start"]').disabled = !target || !read.checked;
  }

  function onChange(w, e) {
    const f = w.root.querySelector('[data-upg="form"]');
    if (!f || !f.contains(e.target)) return;
    // la case « j'ai lu » se décoche à chaque changement de cible, comme dans Harvester
    if (['source', 'version', 'iso'].includes(e.target.name)) f.querySelector('[name="read"]').checked = false;
    syncForm(w);
  }

  async function run(w, action, body, doneText, into) {
    const msg = into || w.root.querySelector('[data-upg="feedback"]');
    try {
      const out = await call('POST', `/api/upgrade/${enc(w.cluster)}/do/${action}`, body);
      follow(out.action_id, msg, doneText, () => { w.dirty = false; setTimeout(() => load(w), 1500); });
      return out;
    } catch (err) {
      msg.innerHTML = `<span class="res-error">${esc(err.message)}</span>`;
      return null;
    }
  }

  async function onClick(w, e) {
    const b = e.target.closest('[data-upg]');
    if (!b || b.tagName === 'FORM' || b.tagName === 'DIV' || b.tagName === 'P') return;
    const act = b.dataset.upg;
    const u = w.data && w.data.upgrade;
    const f = w.root.querySelector('[data-upg="form"]');
    if (act === 'start' && f) {
      const src = w.source;
      const body = { log: f.querySelector('[name="log"]').checked, skip_single_replica: f.querySelector('[name="skip"]').checked };
      if (src === 'version') body.version = val(f, 'version');
      else { body.iso = val(f, 'iso'); body.checksum = val(f, 'checksum').trim() || undefined; }
      if (!confirm(tr('upg.confirm', { cluster: w.cluster }))) return;
      b.disabled = true;
      await run(w, 'start', body, tr('upg.done.start'), f.querySelector('.of-msg'));
      return;
    }
    if (act === 'vadd' && f) {
      const url = val(f, 'vurl').trim();
      if (!url) return;
      return run(w, 'version-add', { url }, tr('upg.done.vadd'), f.querySelector('.of-msg'));
    }
    if (act === 'vdel') {
      const name = b.closest('[data-version]')?.dataset.version;
      if (!name || !confirm(tr('upg.confirm.vdel', { name }))) return;
      return run(w, 'version-delete', { name }, tr('upg.done.vdel', { name }));
    }
    if (!u) return;
    if (act === 'dismiss') return run(w, 'dismiss', { name: u.name }, tr('upg.done.dismiss'));
    if (act === 'follow') return run(w, 'follow', { name: u.name }, tr('upg.done.follow'));
    if (act === 'abort') {
      if (!confirm(tr('upg.confirm.abort', { name: u.name }))) return;
      return run(w, 'abort', { name: u.name }, tr('upg.done.abort'));
    }
    if (act === 'resume') {
      const node = b.closest('[data-node]')?.dataset.node;
      return run(w, 'resume-node', { name: u.name, node }, tr('upg.done.resume', { node }));
    }
    if (act === 'logs') {
      const msg = w.root.querySelector('[data-upg="feedback"]');
      try {
        const out = await call('POST', `/api/upgrade/${enc(w.cluster)}/do/logs`, { name: u.name });
        follow(out.action_id, msg, tr('upg.done.logs'), async (ok) => {
          if (!ok || !out.download) return;
          const r = await fetch(`/api/upgrade/${enc(w.cluster)}/logs/${enc(out.download)}`);
          if (!r.ok) return;
          const blob = await r.blob();
          const a = document.createElement('a');
          a.href = URL.createObjectURL(blob);
          a.download = `${w.cluster}-${u.name}-logs.zip`;
          document.body.appendChild(a);
          a.click();
          setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
        });
      } catch (err) {
        msg.innerHTML = `<span class="res-error">${esc(err.message)}</span>`;
      }
    }
  }

  function init() {
    const btn = document.getElementById('btn-upgrade');
    // 2.0.0 : le bouton de l'aperçu mène à Paramètres > Mise à jour de l'hôte
    if (btn) btn.addEventListener('click', () => {
      const c = window.App && App.getCurrentCluster();
      if (c && window.App.openHost) App.openHost('harvester', c, 'params', 'upgrade');
      else open(c);
    });
  }
  document.addEventListener('DOMContentLoaded', init);
  if (window.FloatingPanels && FloatingPanels.registerType) {
    FloatingPanels.registerType('harvester-upgrade', (a) => open(a.cluster));
  }
  return { open, mount, unmount };
})();
window.Upgrade = Upgrade;
