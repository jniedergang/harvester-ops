/**
 * harvester-ops : mise à jour de la console depuis l'interface (v1.82.0)
 *
 * Onglet « Mise à jour » de la fenêtre des versions. En ligne : la console lit
 * release.json à la source réglée, télécharge l'archive et sa signature. Hors
 * ligne : on lui dépose l'archive et sa .sig. Installer remet la main à
 * l'agent de l'hôte, qui vérifie la signature, installe, redémarre la console
 * et revient en arrière seul ; la page suit ce redémarrage puis se recharge.
 * Conception : docs/design/2026-10-01-mise-a-jour-console.md.
 */
const ConsoleUpdate = (() => {
  const $ = (s) => document.querySelector(s);
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const fmtSize = (n) => (n >= 1 << 30 ? (n / (1 << 30)).toFixed(1) + ' GiB'
    : n >= 1 << 20 ? (n / (1 << 20)).toFixed(0) + ' MiB' : Math.ceil(n / 1024) + ' KiB');
  const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : '');
  const STATE_KEYS = { done: 'upd.state.done', 'rolled-back': 'upd.state.rolled-back',
    failed: 'upd.state.failed', running: 'upd.state.running' };
  let st = null;
  let checking = false;
  let following = null;      // version en cours d'installation
  let sending = '';
  let sourceDraft = null;   // saisie en cours : un rafraîchissement ne l'efface pas
  let loadError = null;     // état illisible : dit, au lieu d'un « chargement » sans fin

  async function api(method, url, body) {
    const r = await fetch(url, {
      method, headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    let d = {};
    try { d = await r.json(); } catch (_) { /* corps vide */ }
    return { ok: r.ok, status: r.status, d };
  }

  async function load() {
    try {
      const r = await api('GET', '/api/update/status');
      if (r.ok) { st = r.d; loadError = null; }
      // 404 : serveur plus ancien que la page (console non redémarrée)
      else loadError = r.status === 404 ? tr('upd.load.missing')
        : tr('upd.load.failed', { code: (r.d && r.d.error) || r.status });
    } catch (_) { loadError = tr('upd.load.unreachable'); }
    render();
    badge();
  }

  function badge() {
    const b = $('#btn-version');
    if (!b || !st) return;
    const newer = st.check && st.check.ok && st.check.newer;
    b.classList.toggle('has-update', !!newer);
    if (newer) b.setAttribute('data-update', tr('upd.badge', { v: st.check.release.version }));
    else b.removeAttribute('data-update');
  }

  function stepLine(s) {
    const icon = s.status === 'done' ? 'ok' : s.status === 'error' ? 'fail' : 'clock';
    return `<li>${window.Icons ? Icons.svg(icon, { size: 12 }) : ''} <strong>${esc(s.id)}</strong> ${esc(s.message || '')}</li>`;
  }

  function lastBlock() {
    const l = st.last;
    if (!l || !l.started) return '';
    return `<div class="card upd-card">
      <h4 data-i18n="upd.last.title">${esc(tr('upd.last.title'))}</h4>
      <p>${esc(tr('upd.last.line', { state: tr(STATE_KEYS[l.state] || STATE_KEYS.running), from: l.from || '?',
        to: l.to || '?', when: when(l.ended || l.ts || l.started) }))}</p>
      ${l.message ? `<p class="form-hint">${esc(l.message)}</p>` : ''}
      <ul class="upd-steps">${(l.steps || []).map(stepLine).join('')}</ul>
    </div>`;
  }

  function notesBlock(rel) {
    const notes = (rel.notes || []).slice(0, 10);
    if (!notes.length) return '';
    return `<details class="upd-notes"><summary>${esc(tr('upd.notes.title'))}</summary>
      ${notes.map(n => `<div class="version-rel"><strong>v${esc(n.version)}</strong> ${esc(n.title || '')}
        <ul>${(n.sections || []).flatMap(s => s.items.slice(0, 8).map(i => `<li>${esc(i)}</li>`)).join('')}</ul></div>`).join('')}
    </details>`;
  }

  function checkBlock() {
    const c = st.check;
    if (checking) return `<p class="form-hint">${esc(tr('upd.checking'))}</p>`;
    if (!c) return '';
    if (!c.ok) return `<p class="upd-err">${esc(tr('upd.check.failed', { err: c.error || '?' }))}</p>`;
    const rel = c.release;
    const when = c.ts ? ' ' + tr('upd.check.at', { t: new Date(c.ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) }) : '';
    if (!c.newer) {
      // une source plus ancienne que la console n'est pas « à jour » : on le dit
      const msg = rel.version === st.current ? tr('upd.check.uptodate', { v: rel.version })
        : tr('upd.check.ahead', { v: rel.version, cur: st.current });
      return `<p>${esc(msg + when)}</p>`;
    }
    return `<p class="upd-ok">${esc(tr('upd.check.newer', { v: rel.version, date: rel.date || '' }))}
        ${rel.title ? ` : ${esc(rel.title)}` : ''}</p>
      ${notesBlock(rel)}
      ${st.can_apply ? `<button type="button" class="btn btn-primary tip" data-upd="download"
        data-tip="${esc(tr('upd.downloadTip'))}" title="${esc(tr('upd.downloadTip'))}">${esc(tr('upd.download'))}</button>` : ''}`;
  }

  function stagedBlock() {
    if (!st.staged.length) return `<p class="form-hint">${esc(tr('upd.staged.none'))}</p>`;
    return `<table class="data-table upd-staged"><tbody>${st.staged.map(s => {
      const state = s.error ? `<span class="upd-err">${esc(s.error)}</span>`
        : s.signature === 'valid' ? (s.newer ? esc(tr('upd.staged.sigValid')) : `<span class="upd-err">${esc(tr('upd.staged.older'))}</span>`)
        : `<span class="upd-err">${esc(tr('upd.staged.sigMissing'))}</span>`;
      const canInstall = st.can_apply && s.ok && s.newer && st.agent && !st.pending;
      return `<tr data-name="${esc(s.name)}">
        <td><strong>${s.version ? 'v' + esc(s.version) : esc(s.name)}</strong></td>
        <td>${esc(fmtSize(s.size || 0))}</td><td>${state}</td>
        <td class="upd-actions">
          ${st.can_apply ? `<button type="button" class="btn btn-primary btn-sm tip" data-upd="install" ${canInstall ? '' : 'disabled'}
            data-tip="${esc(tr('upd.installTip'))}" title="${esc(tr('upd.installTip'))}">${esc(tr('upd.install'))}</button>
          <button type="button" class="btn btn-sm tip" data-upd="delete"
            data-tip="${esc(tr('upd.deleteTip'))}" title="${esc(tr('upd.deleteTip'))}">${esc(tr('upd.delete'))}</button>` : ''}
        </td></tr>`;
    }).join('')}</tbody></table>`;
  }

  function render() {
    const pane = $('#versions-pane-update');
    if (!pane) return;
    if (!st && loadError) {
      pane.innerHTML = `<p class="upd-err">${esc(loadError)}</p>
        <button type="button" class="btn btn-sm tip" data-upd="reload"
          data-tip="${esc(tr('upd.load.retryTip'))}" title="${esc(tr('upd.load.retryTip'))}">${esc(tr('upd.load.retry'))}</button>`;
      return;
    }
    if (!st) { pane.innerHTML = `<p class="form-hint">${esc(tr('common.loading'))}</p>`; return; }
    if (following) { renderFollow(pane); return; }
    const admin = st.can_apply;
    pane.innerHTML = `
      <div class="upd-head">
        <p><strong>${esc(tr('upd.current', { v: st.current }))}</strong></p>
        ${st.agent ? `<p class="form-hint">${esc(tr('upd.agent.ok', { v: st.agent.installed || '?' }))}</p>`
          : st.from_sources ? `<p class="form-hint">${esc(tr('upd.agent.sources'))}</p>`
          : `<p class="upd-err">${esc(tr('upd.agent.missing'))}</p>`}
        ${st.trusted_keys ? '' : `<p class="upd-err">${esc(tr('upd.keys.missing'))}</p>`}
        ${admin ? '' : `<p class="form-hint">${esc(tr('upd.adminOnly'))}</p>`}
      </div>
      <div class="card upd-card">
        <h4>${esc(tr('upd.online.title'))}</h4>
        <p class="form-hint">${esc(tr('upd.online.hint'))}</p>
        <div class="upd-row">
          <label for="upd-source">${esc(tr('upd.source.label'))}</label>
          <input type="url" id="upd-source" value="${esc(sourceDraft ?? st.source)}" ${admin ? '' : 'disabled'}
            class="tip" data-tip="${esc(tr('upd.source.tip'))}" title="${esc(tr('upd.source.tip'))}">
          ${admin ? `<button type="button" class="btn btn-sm tip" data-upd="save-source" data-tip="${esc(tr('upd.source.saveTip'))}"
              title="${esc(tr('upd.source.saveTip'))}">${esc(tr('upd.source.save'))}</button>
            <button type="button" class="btn btn-sm tip" data-upd="reset-source" data-tip="${esc(tr('upd.source.resetTip'))}"
              title="${esc(tr('upd.source.resetTip'))}">${esc(tr('upd.source.reset'))}</button>
            <button type="button" class="btn btn-sm tip" data-upd="check" data-tip="${esc(tr('upd.checkTip'))}"
              title="${esc(tr('upd.checkTip'))}" ${checking ? 'disabled' : ''}>${esc(tr('upd.check'))}</button>` : ''}
        </div>
        <div class="upd-check">${checkBlock()}</div>
      </div>
      ${admin ? `<div class="card upd-card">
        <h4>${esc(tr('upd.offline.title'))}</h4>
        <p class="form-hint">${esc(tr('upd.offline.hint'))}</p>
        <label class="btn btn-sm tip" data-tip="${esc(tr('upd.offline.pickTip'))}" title="${esc(tr('upd.offline.pickTip'))}">
          ${esc(tr('upd.offline.pick'))}
          <input type="file" id="upd-files" multiple accept=".gz,.sig" hidden></label>
        <span class="form-hint" id="upd-sending">${esc(sending)}</span>
      </div>` : ''}
      <div class="card upd-card">
        <h4>${esc(tr('upd.staged.title'))}</h4>
        ${stagedBlock()}
      </div>
      ${lastBlock()}`;
  }

  // -- suivi d'une installation ---------------------------------------------
  function renderFollow(pane) {
    const l = (st && st.last) || {};
    const mine = l.to === following.version && l.started >= following.since - 5;
    let head = tr('upd.progress.waiting');
    if (following.down) head = tr('upd.progress.restarting');
    if (mine && l.state === 'rolled-back') head = tr('upd.progress.rolledBack', { v: l.from, msg: l.message || '' });
    if (mine && l.state === 'failed') head = tr('upd.progress.failed', { msg: l.message || '' });
    if (st && st.current === following.version) head = tr('upd.progress.done', { v: following.version });
    pane.innerHTML = `<div class="card upd-card upd-follow">
      <h4>${esc(head)}</h4>
      <ul class="upd-steps">${(mine ? l.steps || [] : []).map(stepLine).join('')}</ul></div>`;
  }

  async function follow(version) {
    following = { version, since: Date.now() / 1000, down: false };
    render();
    for (let i = 0; i < 900; i++) {          // jusqu'à 45 min (installation longue)
      await new Promise(r => setTimeout(r, 2000));
      try {
        const r = await api('GET', '/api/update/status');
        if (!r.ok) throw new Error(String(r.status));
        st = r.d;
        const l = st.last || {};
        if (st.current === version) {
          renderFollow($('#versions-pane-update'));
          setTimeout(() => location.reload(), 2500);
          return;
        }
        if (l.to === version && ['rolled-back', 'failed'].includes(l.state) && l.started >= following.since - 5
            && !st.pending) {
          renderFollow($('#versions-pane-update'));
          following = null;
          setTimeout(render, 8000);
          return;
        }
        following.down = false;
      } catch (_) {
        following.down = true;
      }
      renderFollow($('#versions-pane-update'));
    }
  }

  // -- gestes ---------------------------------------------------------------
  async function check() {
    checking = true; render();
    try { await api('POST', '/api/update/check'); } finally { checking = false; }
    await load();
  }

  async function saveSource(url) {
    const r = await api('PUT', '/api/update/source', { url });
    if (!r.ok) { alert(r.d.error || r.status); return; }
    sourceDraft = null;
    await load();
  }

  async function download() {
    const r = await api('POST', '/api/update/download');
    if (!r.ok) { alert(r.d.error || r.status); return; }
    if (window.Dock && Dock.poll) Dock.poll();
    watchAction(r.d.action_id);
  }

  function watchAction(id) {
    if (!id || !window.SSEReconnect) { setTimeout(load, 3000); return; }
    const es = SSEReconnect.connect(`/api/stream/${encodeURIComponent(id)}`, {
      on: { end: () => { es.close(); load(); } },
    });
    // filet : l'état est relu même si le flux ne dit rien
    setTimeout(load, 15000);
  }

  function uploadOne(file) {
    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', `/api/update/upload?name=${encodeURIComponent(file.name)}`);
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        sending = tr('upd.offline.sending', { name: file.name, pct: Math.floor(e.loaded * 100 / e.total) });
        const el = $('#upd-sending');
        if (el) el.textContent = sending;
      };
      xhr.onload = () => {
        let d = {};
        try { d = JSON.parse(xhr.responseText); } catch (_) { /* rien */ }
        sending = xhr.status < 300 ? tr('upd.offline.sent', { name: file.name }) : (d.error || String(xhr.status));
        resolve();
      };
      xhr.onerror = () => { sending = file.name + ': network error'; resolve(); };
      xhr.send(file);
    });
  }

  async function uploadFiles(files) {
    // l'archive d'abord : sa signature la vérifie en arrivant
    const list = [...files].sort((a, b) => (a.name.endsWith('.sig') ? 1 : 0) - (b.name.endsWith('.sig') ? 1 : 0));
    for (const f of list) await uploadOne(f);
    await load();
  }

  async function install(name) {
    const s = st.staged.find(x => x.name === name);
    if (!s || !confirm(tr('upd.confirm', { v: s.version }))) return;
    let r = await api('POST', '/api/update/apply', { archive: name });
    if (r.status === 409 && r.d.busy) {
      if (!confirm(tr('upd.confirmBusy', { n: r.d.busy.length }))) return;
      r = await api('POST', '/api/update/apply', { archive: name, force: true });
    }
    if (!r.ok) { alert(r.d.error || r.status); return; }
    follow(s.version);
  }

  async function remove(name) {
    await api('DELETE', `/api/update/staged/${encodeURIComponent(name)}`);
    await load();
  }

  function onClick(e) {
    const b = e.target.closest('[data-upd]');
    if (!b) return;
    const name = b.closest('tr')?.dataset.name;
    switch (b.dataset.upd) {
      case 'check': check(); break;
      case 'save-source': saveSource($('#upd-source').value.trim()); break;
      case 'reset-source': saveSource(''); break;
      case 'download': download(); break;
      case 'install': install(name); break;
      case 'delete': remove(name); break;
      case 'reload': loadError = null; render(); load(); break;
      default: break;
    }
  }

  function showPane(which) {
    document.querySelectorAll('#versions-modal [data-vpane]').forEach(x =>
      x.classList.toggle('active', x.dataset.vpane === which));
    $('#versions-pane-history').hidden = which !== 'history';
    $('#versions-pane-update').hidden = which !== 'update';
    if (which === 'update') load();
  }

  function init() {
    document.querySelectorAll('#versions-modal [data-vpane]').forEach(b =>
      b.addEventListener('click', () => showPane(b.dataset.vpane)));
    const pane = $('#versions-pane-update');
    if (pane) {
      pane.addEventListener('click', onClick);
      pane.addEventListener('change', (e) => { if (e.target.id === 'upd-files') uploadFiles(e.target.files); });
      pane.addEventListener('input', (e) => { if (e.target.id === 'upd-source') sourceDraft = e.target.value; });
    }
    // l'état (et donc la pastille « mise à jour disponible ») au chargement
    load();
  }

  return { init, showPane, load };
})();
window.ConsoleUpdate = ConsoleUpdate;
