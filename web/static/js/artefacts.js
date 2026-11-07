/**
 * harvester-ops : paquets d'artefacts signés (v2.6.0).
 *
 * Onglet Artefacts de Cluster API : la liste des paquets importés (badge de
 * confiance), leur détail dans le panneau de droite, et la fenêtre
 * d'import (archive puis signature, pré-vérification, avertissement et case
 * à cocher pour un paquet non signé ou signé par une clé inconnue).
 * Paramètres > Artefacts : mode strict et clés de confiance.
 * Le serveur reçoit l'archive et lance `harvester-artefacts import` (parité
 * CLI) ; la progression passe par le dock.
 * Voir docs/design/2026-11-05-artefacts-registre-260-plan.md (tâche 5).
 */
const Artefacts = (() => {
  const enc = encodeURIComponent;
  const B = () => window.Board;
  const tr = (k, f, v) => B().tr(k, f, v);
  const esc = (v) => B().esc(v);
  const icon = (n, size) => (window.Icons ? Icons.svg(n, { size: size || 14 }) : '');
  let data = null;
  let selected = null;
  let modal = null;


  // Un appel littéral par état : le contrôle de parité des traductions ne
  // voit que ces formes.
  function trustBadge(state, principal) {
    if (state === 'valid') {
      return `<span class="badge ok tip art-trust" data-tip="${esc(tr('art.t.valid', 'Signed by a trusted key'))}">${icon('ok', 12)} ${esc(tr('art.trust.valid', 'Signed'))}${principal ? ` (${esc(principal)})` : ''}</span>`;
    }
    if (state === 'unknown-key') {
      return `<span class="badge warn tip art-trust" data-tip="${esc(tr('art.t.unknownKey', 'Signed by a key that is not among the trusted keys; accepted after an acknowledgement'))}">${icon('warn', 12)} ${esc(tr('art.trust.unknownKey', 'Unknown key'))}</span>`;
    }
    if (state === 'unsigned') {
      return `<span class="badge warn tip art-trust" data-tip="${esc(tr('art.t.unsigned', 'Not signed; accepted after an acknowledgement'))}">${icon('warn', 12)} ${esc(tr('art.trust.unsigned', 'Unsigned'))}</span>`;
    }
    return `<span class="badge fail art-trust">${esc(tr('art.trust.invalid', 'Invalid signature'))}</span>`;
  }

  const bytes = (n) => (B().bytes ? B().bytes(n) : `${n} B`);

  // -------------------------------------------------------------------------
  // Liste et détail
  // -------------------------------------------------------------------------
  async function refresh() {
    const box = document.getElementById('artefacts-body');
    if (!box) return;
    try {
      const r = await fetch('/api/artefacts');
      data = r.ok ? await r.json() : null;
    } catch (e) { data = null; }
    if (!data) {
      box.innerHTML = `<p class="form-hint">${esc(tr('art.loadError', 'The packages could not be read.'))}</p>`;
      return;
    }
    const strict = data.strict
      ? `<div class="ct-banner art-strict" role="status">${icon('lock')} <span>${esc(tr('art.strictOn', 'Strict mode: only packages signed by a trusted key are accepted.'))}</span></div>` : '';
    const rows = (data.packages || []).map(p => `
      <tr class="art-row${selected && selected.name === p.name && selected.version === p.version ? ' selected' : ''}"
          data-art-name="${esc(p.name)}" data-art-version="${esc(p.version)}" tabindex="0">
        <td>${esc(p.name)}</td><td>${esc(p.version)}</td><td>${esc(p.items)}</td>
        <td>${trustBadge(p.trust, p.principal)}</td>
        <td>${esc(p.imported_by || '')}<div class="form-hint">${esc(p.imported_at || '')}</div></td></tr>`).join('');
    box.innerHTML = `${strict}
      <div class="art-layout">
        <div class="art-list">
          ${rows ? `<table class="data-table art-table"><thead><tr>
            <th>${esc(tr('art.col.name', 'Package'))}</th><th>${esc(tr('art.col.version', 'Version'))}</th>
            <th>${esc(tr('art.col.items', 'Items'))}</th><th>${esc(tr('art.col.trust', 'Trust'))}</th>
            <th>${esc(tr('art.col.imported', 'Imported by'))}</th></tr></thead><tbody>${rows}</tbody></table>`
            : `<p class="form-hint art-empty">${esc(tr('art.empty', 'No package yet. Build one with harvester-artefacts pack, then import it.'))}</p>`}
        </div>
        <aside class="art-detail card" id="artefacts-detail" aria-live="polite">${selected ? '' : `<p class="form-hint">${esc(tr('art.pick', 'Select a package to see its items, images and trust.'))}</p>`}</aside>
      </div>`;
    if (selected) showDetail(selected.name, selected.version);
  }

  async function showDetail(name, version) {
    selected = { name, version };
    document.querySelectorAll('.art-row').forEach(r => r.classList.toggle('selected',
      r.dataset.artName === name && r.dataset.artVersion === version));
    const box = document.getElementById('artefacts-detail');
    if (!box) return;
    let d = null;
    try {
      const r = await fetch(`/api/artefacts/${enc(name)}/${enc(version)}`);
      d = r.ok ? await r.json() : null;
    } catch (e) { d = null; }
    if (!d) { box.innerHTML = `<p class="form-hint">${esc(tr('art.loadError', 'The packages could not be read.'))}</p>`; return; }
    const t = d.trust || {};
    const items = (d.index.items || []).map(it => `<li><b>${esc(it.name)}</b> <span class="badge dim">${esc(it.kind)}</span>
      ${it.later ? `<span class="badge info tip" data-tip="${esc(tr('art.t.later', 'Kept in the package; applied by a later version'))}">${esc(tr('art.later', 'coming (2.8)'))}</span>` : ''}
      ${it.version ? `<span class="form-hint">${esc(it.version)}</span>` : ''}</li>`).join('');
    const images = (d.images || []).map(im => `<li><code>${esc(im.name)}</code>
      <span class="form-hint">${im.size != null ? esc(bytes(im.size)) : ''} ${esc(im.platform || '')} ${esc(String(im.digest || '').slice(7, 19))}</span></li>`).join('');
    const pubs = (d.published || []).map(p => `<li>${esc(p.registry)} <span class="form-hint">${esc(p.at || '')}</span></li>`).join('');
    box.innerHTML = `
      <div class="card-header"><h3>${esc(d.name)} ${esc(d.version)}</h3>
        <button type="button" class="btn btn-sm btn-primary tip needs-operator" data-art-publish
                data-tip="${esc(tr('art.t.publish', 'Copy the images and charts of this package into a registry attached to the cluster'))}"
                title="${esc(tr('art.t.publish', 'Copy the images and charts of this package into a registry attached to the cluster'))}">${icon('upload')} ${esc(tr('art.publish', 'Publish'))}</button>
        <button type="button" class="btn btn-sm btn-danger tip needs-admin" data-art-remove
                data-tip="${esc(tr('art.t.remove', 'Remove this package from the console (the registries keep what was published)'))}"
                title="${esc(tr('art.t.remove', 'Remove this package from the console (the registries keep what was published)'))}">${icon('trash')} ${esc(tr('art.remove', 'Remove'))}</button></div>
      <div class="card-body">
        ${d.index.description ? `<p>${esc(d.index.description)}</p>` : ''}
        <h4>${esc(tr('art.trustTitle', 'Trust'))}</h4>
        <div class="art-trust-box">${trustBadge(t.state, t.principal)}
          ${t.key_fingerprint ? `<div class="form-hint">${esc(tr('art.fingerprint', 'Key'))} <code>${esc(t.key_fingerprint)}</code></div>` : ''}
          <div class="form-hint">${esc(tr('art.importedBy', 'Imported by {person} on {date} ({mode} mode)', { person: t.person || '?', date: t.date || '?', mode: t.mode || '?' }))}</div></div>
        <h4>${esc(tr('art.items', 'Items'))}</h4><ul class="art-items">${items}</ul>
        ${images ? `<h4>${esc(tr('art.images', 'Images'))}</h4><ul class="art-images">${images}</ul>` : ''}
        <h4>${esc(tr('art.published', 'Published in'))}</h4>
        ${pubs ? `<ul>${pubs}</ul>` : `<p class="form-hint">${esc(tr('art.notPublished', 'Not published in a registry yet.'))}</p>`}
      </div>`;
  }

  // -------------------------------------------------------------------------
  // Publication (2.6.0, tâche 12) : constats avant le bouton, progression
  // -------------------------------------------------------------------------
  const PUB_FINDINGS = {
    'registry-unknown': (v) => tr('art.pfd.registry-unknown', 'The registry {registry} is not declared.', v),
    'registry-not-attached': (v) => tr('art.pfd.registry-not-attached', 'The registry {registry} is not attached to {cluster}.', v),
    'registry-no-usage': (v) => tr('art.pfd.registry-no-usage', 'The registry {registry} does not serve {usage}.', v),
    'publish-tag-differs': (v) => tr('art.pfd.publish-tag-differs', '{reference} already exists with another content: nothing is overwritten.', v),
    'package-untrusted-strict': (v) => tr('art.pfd.package-untrusted-strict', 'Strict mode refuses {package} ({state}).', v),
    'publish-item-unknown': (v) => tr('art.pfd.publish-item-unknown', 'Unknown item(s): {items}.', v),
    'cluster-unreachable': (v) => tr('art.pfd.cluster-unreachable', 'The Harvester cluster cannot be read.', v),
  };
  const pubText = (f) => (PUB_FINDINGS[f.code] ? PUB_FINDINGS[f.code](Object.assign({}, f.facts || {})) : f.code);

  async function openPublish() {
    if (!selected) return;
    const pkg = Object.assign({}, selected);
    const cluster = window.App && App.managementCluster ? App.managementCluster() : '';
    const [dr, rr] = await Promise.all([
      fetch(`/api/artefacts/${enc(pkg.name)}/${enc(pkg.version)}`).then(r => r.json()).catch(() => ({})),
      fetch('/api/registries').then(r => r.json()).catch(() => ({})),
    ]);
    const regs = (rr.registries || []).filter(x => (x.clusters || []).includes(cluster));
    const items = ((dr.index || {}).items || []);
    const publishable = (it) => !it.later && it.kind !== 'manifest' && it.kind !== 'policy';
    closeModal();
    modal = document.createElement('div');
    modal.className = 'modal-overlay active art-modal art-publish';
    modal.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-header"><div><h3>${icon('upload')} ${esc(tr('art.publishTitle', 'Publish a package'))}</h3>
          <div class="modal-subtitle">${esc(`${pkg.name} ${pkg.version}`)} · ${esc(cluster)}</div></div>
          <button type="button" class="btn-close tip" data-art-close data-tip="${esc(tr('common.cancel', 'Cancel'))}" title="${esc(tr('common.cancel', 'Cancel'))}">×</button></div>
        <div class="modal-body">
          <div class="form-row"><label>${esc(tr('art.registry', 'Registry'))}</label>
            <select data-x="registry" class="tip" data-tip="${esc(tr('art.t.registry', 'Registries attached to this Harvester cluster (Registries tab)'))}"
                    title="${esc(tr('art.t.registry', 'Registries attached to this Harvester cluster (Registries tab)'))}">${regs.map(x =>
              `<option value="${esc(x.name)}">${esc(`${x.name} (${x.endpoint || ''})`)}</option>`).join('')}</select></div>
          ${regs.length ? '' : `<p class="form-hint">${esc(tr('art.noRegistry', 'No registry attached to this cluster: declare or pose one in the Registries tab.'))}</p>`}
          <fieldset class="tf-block"><legend>${esc(tr('art.items', 'Items'))}</legend>${items.map(it => `
            <label class="bm-check tip" data-tip="${esc(publishable(it) ? tr('art.t.item', 'Published with the others when checked')
              : it.later ? tr('art.t.later', 'Kept in the package; applied by a later version') : tr('art.t.manifest', 'Manifests are not published in a registry'))}">
              <input type="checkbox" data-item="${esc(it.name)}" ${publishable(it) ? 'checked' : 'disabled'}> ${esc(it.name)}
              <span class="badge dim">${esc(it.kind)}</span></label>`).join('')}</fieldset>
          <h4>${esc(tr('art.checks', 'Checks'))}</h4><div class="xfer-report" data-x="report"></div>
          <ul class="art-todo" data-x="todo"></ul>
          <div class="xfer-live-line" data-x="progress" hidden></div>
        </div>
        <div class="modal-footer apply-bar" style="margin:0;">
          <button type="button" class="btn btn-secondary btn-sm" data-art-close>${esc(tr('common.cancel', 'Cancel'))}</button>
          <span class="tip" data-x="go-wrap"><button type="button" class="btn btn-primary btn-sm" data-art-go disabled>${icon('upload', 13)} ${esc(tr('art.publish', 'Publish'))}</button></span>
          <span class="apply-result" data-x="feedback"></span>
        </div>
      </div>`;
    document.body.appendChild(modal);
    const m = modal;
    const q = (x) => m.querySelector(`[data-x="${x}"]`);
    const body = () => {
      const all = [...m.querySelectorAll('[data-item]:not([disabled])')];
      const chosen = all.filter(c => c.checked).map(c => c.dataset.item);
      return { cluster, registry: q('registry').value, items: chosen.length === all.length ? null : chosen };
    };
    const go = m.querySelector('[data-art-go]');
    const setGo = (ok, why) => {
      go.disabled = !ok;
      q('go-wrap').dataset.tip = why;
      go.title = why;
    };
    let seq = 0;
    async function check() {
      const n = ++seq;
      const b = body();
      if (!b.registry) { setGo(false, tr('art.noRegistry', 'No registry attached to this cluster: declare or pose one in the Registries tab.')); return; }
      if (b.items && !b.items.length) { setGo(false, tr('art.noItem', 'Choose at least one item.')); return; }
      q('report').innerHTML = `<p class="form-hint">${esc(tr('art.checking', 'Checking…'))}</p>`;
      const r = await fetch(`/api/artefacts/${enc(pkg.name)}/${enc(pkg.version)}/publish`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(Object.assign({ dry_run: true }, b)) });
      const d = await r.json().catch(() => ({}));
      if (n !== seq || modal !== m) return;
      const fs = d.findings || [];
      q('report').innerHTML = !r.ok ? `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(d.error || `HTTP ${r.status}`)}</div></div>`
        : fs.map(f => `<div class="sto-finding ${f.level === 'block' ? 'sev-critical' : 'sev-watch'}" data-code="${esc(f.code)}"><div class="sto-finding-title">${esc(pubText(f))}</div></div>`).join('')
          || `<div class="sto-finding sev-info"><div class="sto-finding-title">${esc(tr('art.noBlocker', 'No blocker.'))}</div></div>`;
      q('todo').innerHTML = (d.todo || []).map(t => `<li><span class="badge ${t.state === 'present' ? 'ok' : 'info'}">${esc(t.state === 'present'
        ? tr('art.todo.present', 'already there') : tr('art.todo.new', 'to send'))}</span> <code>${esc(t.name)}</code></li>`).join('');
      const first = fs.find(f => f.level === 'block');
      if (!r.ok || d.blocked) setGo(false, tr('art.t.publishBlocked', 'Blocked: {reason}', { reason: first ? pubText(first) : (d.error || '?') }));
      else setGo(true, tr('art.t.publishGo', 'Send the missing layers; what is already there is skipped'));
    }
    m.addEventListener('click', (e) => { if (e.target === m || e.target.closest('[data-art-close]')) closeModal(); });
    m.addEventListener('change', check);
    go.addEventListener('click', async () => {
      if (go.disabled) return;
      go.disabled = true;
      const r = await fetch(`/api/artefacts/${enc(pkg.name)}/${enc(pkg.version)}/publish`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body()) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) { q('feedback').textContent = d.error || `HTTP ${r.status}`; go.disabled = false; return; }
      const line = q('progress');
      line.hidden = false;
      line.textContent = tr('art.publishStarted', 'Publishing (action {id}), see the dock.', { id: d.action_id });
      if (!window.SSEReconnect) return;
      const es = SSEReconnect.connect(`/api/stream/${enc(d.action_id)}`, {
        on: {
          step: (ev) => { try { const st = JSON.parse(ev.data); if (st.message) line.textContent = st.message; } catch { /* ligne illisible */ } },
          end: (ev) => {
            let x = {};
            try { x = JSON.parse(ev.data); } catch { /* fin sans détail */ }
            es.close();
            line.textContent = x.status === 'done' ? tr('art.publishDone', 'Published.') : tr('art.publishFailed', 'The publication failed: see the dock.');
            setTimeout(() => showDetail(pkg.name, pkg.version), 500);
          },
        },
      });
    });
    check();
  }

  async function remove() {
    if (!selected) return;
    const ok = window.confirm(tr('art.confirmRemove', 'Remove package {name}?', { name: `${selected.name}-${selected.version}` }));
    if (!ok) return;
    const r = await fetch(`/api/artefacts/${enc(selected.name)}/${enc(selected.version)}`, { method: 'DELETE' });
    if (!r.ok) {
      const b = await r.json().catch(() => ({}));
      window.alert(b.error || tr('art.removeFailed', 'The package could not be removed.'));
      return;
    }
    selected = null;
    refresh();
  }

  // -------------------------------------------------------------------------
  // Fenêtre d'import
  // -------------------------------------------------------------------------
  function closeModal() {
    if (modal) modal.remove();
    modal = null;
  }

  function verdictHtml(st) {
    if (!st) return '';
    if (!st.trust) {
      return `<div class="summary-bar bad" data-x="refused">${esc(tr('art.notPackage', 'This file is not a valid package: {error}', { error: st.error || st.code }))}</div>`;
    }
    const t = st.trust;
    const head = `<div class="art-staged"><b>${esc(st.name)} ${esc(st.version)}</b> ${trustBadge(t.state, t.principal)}
      <span class="form-hint">${esc(tr('art.stagedSize', '{items} item(s), {size}', { items: st.items, size: bytes(st.size) }))}</span></div>`;
    if (t.state === 'invalid') {
      return `${head}<div class="summary-bar bad" data-x="refused">${esc(tr('art.invalid', 'The signature does not match this package. It is refused.'))}</div>`;
    }
    if (st.code && st.code.startsWith('strict-refuses-')) {
      const why = st.locked ? tr('art.strictLocked', 'Strict mode is imposed by the console configuration (config.yaml).')
        : tr('art.strictSetting', 'Strict mode is set in Settings > Artefacts.');
      return `${head}<div class="summary-bar bad" data-x="refused">${esc(tr('art.strictRefuses', 'Strict mode refuses this package: it is not signed by a trusted key.'))} ${esc(why)}</div>`;
    }
    if (!st.acknowledge_needed) return head;
    const what = t.state === 'unsigned'
      ? tr('art.warnUnsigned', 'This package is not signed. Its origin cannot be checked.')
      : tr('art.warnUnknownKey', 'This package is signed by a key that is not trusted. Its origin cannot be checked.');
    return `${head}<div class="ct-banner auto-banner-warn art-warning" role="alert">${icon('warn')}
        <div><p>${esc(what)}</p>
        <p>${esc(tr('art.warnJournal', 'Accepting it is written to the journal with your name.'))}</p>
        ${t.key_fingerprint ? `<p class="form-hint">${esc(tr('art.fingerprint', 'Key'))} <code>${esc(t.key_fingerprint)}</code></p>` : ''}
        <label class="tip" data-tip="${esc(tr('art.t.ack', 'Required to accept a package whose origin cannot be checked'))}">
          <input type="checkbox" data-x="ack"> ${esc(tr('art.ack', 'I accept this package knowingly'))}</label></div></div>`;
  }

  /** Accepter : actif seulement pour un paquet accepté d'office ou dont
   *  l'acquittement est coché. */
  function syncAccept(st) {
    if (!modal) return;
    const btn = modal.querySelector('[data-art-accept]');
    const ack = modal.querySelector('[data-x="ack"]');
    const acceptable = !!(st && st.trust && st.ok && (!st.acknowledge_needed || (ack && ack.checked)));
    btn.disabled = !acceptable;
  }

  async function sendFile(file, name) {
    const r = await fetch(`/api/artefacts/upload?name=${enc(name)}`, {
      method: 'POST', body: file, headers: { 'Content-Type': 'application/octet-stream' } });
    const b = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(b.error || `HTTP ${r.status}`);
    return b;
  }

  function openImport() {
    closeModal();
    modal = document.createElement('div');
    modal.className = 'modal-overlay active art-modal';
    modal.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="art-import-title">
        <div class="modal-header"><div><h3 id="art-import-title">${icon('bundle')} ${esc(tr('art.importTitle', 'Import a package'))}</h3>
          <div class="modal-subtitle">${esc(tr('art.importSub', 'The archive <name>-<version>.tar.gz, and its .sig if it is signed'))}</div></div>
          <button type="button" class="btn-close tip" data-art-close data-tip="${esc(tr('common.cancel', 'Cancel'))}" title="${esc(tr('common.cancel', 'Cancel'))}">×</button></div>
        <div class="modal-body">
          <div class="form-row"><label>${esc(tr('art.archive', 'Package archive'))}</label>
            <input type="file" accept=".gz" data-x="archive" class="tip" data-tip="${esc(tr('art.t.archive', 'The .tar.gz built by harvester-artefacts pack'))}" title="${esc(tr('art.t.archive', 'The .tar.gz built by harvester-artefacts pack'))}"></div>
          <div class="form-row"><label>${esc(tr('art.signature', 'Signature (optional)'))}</label>
            <input type="file" accept=".sig" data-x="sig" class="tip" data-tip="${esc(tr('art.t.signature', 'The .sig written by harvester-artefacts sign'))}" title="${esc(tr('art.t.signature', 'The .sig written by harvester-artefacts sign'))}"></div>
          <div data-x="verdict"></div>
        </div>
        <div class="modal-footer apply-bar" style="margin:0;">
          <button type="button" class="btn btn-secondary btn-sm" data-art-close>${esc(tr('common.cancel', 'Cancel'))}</button>
          <button type="button" class="btn btn-secondary btn-sm tip" data-art-upload disabled
                  data-tip="${esc(tr('art.t.upload', 'Send the files and check the package (nothing is imported yet)'))}" title="${esc(tr('art.t.upload', 'Send the files and check the package (nothing is imported yet)'))}">${icon('upload')} ${esc(tr('art.upload', 'Send and check'))}</button>
          <button type="button" class="btn btn-primary btn-sm tip" data-art-accept disabled
                  data-tip="${esc(tr('art.t.accept', 'Import the package into the console'))}" title="${esc(tr('art.t.accept', 'Import the package into the console'))}">${icon('ok', 13)} ${esc(tr('art.accept', 'Accept'))}</button>
          <span class="apply-result" data-x="feedback"></span>
        </div>
      </div>`;
    document.body.appendChild(modal);
    const q = (x) => modal.querySelector(`[data-x="${x}"]`);
    let staged = null;
    modal.addEventListener('click', (e) => {
      if (e.target === modal || e.target.closest('[data-art-close]')) closeModal();
    });
    q('archive').addEventListener('change', () => {
      modal.querySelector('[data-art-upload]').disabled = !q('archive').files.length;
    });
    modal.addEventListener('change', (e) => { if (e.target.matches('[data-x="ack"]')) syncAccept(staged); });
    modal.querySelector('[data-art-upload]').addEventListener('click', async (e) => {
      const f = q('archive').files[0];
      if (!f) return;
      e.target.disabled = true;
      q('feedback').textContent = tr('art.sending', 'Sending…');
      try {
        let res = await sendFile(f, f.name);
        const s = q('sig').files[0];
        if (s) res = await sendFile(s, `${f.name}.sig`);
        staged = res.staged;
        q('verdict').innerHTML = verdictHtml(staged);
        q('feedback').textContent = '';
      } catch (err) {
        q('feedback').textContent = err.message;
      }
      e.target.disabled = false;
      syncAccept(staged);
    });
    modal.querySelector('[data-art-accept]').addEventListener('click', async (e) => {
      if (!staged || e.target.disabled) return;
      e.target.disabled = true;
      // revue I-2 : l'acquittement vaut pour l'archive vérifiée, pas une autre
      const body = { acknowledge: staged.acknowledge_needed || null, sha256: staged.sha256 || null };
      const r = await fetch(`/api/artefacts/${enc(staged.name)}/${enc(staged.version)}/accept`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const b = await r.json().catch(() => ({}));
      if (!r.ok) {
        q('feedback').textContent = b.error || `HTTP ${r.status}`;
        return;
      }
      closeModal();
      setTimeout(refresh, 1500);
    });
  }

  // -------------------------------------------------------------------------
  // Paramètres > Artefacts
  // -------------------------------------------------------------------------
  async function loadSettings() {
    const box = document.getElementById('artefacts-settings-body');
    if (!box) return;
    const r = await fetch('/api/artefacts/settings').catch(() => null);
    const s = r && r.ok ? await r.json() : null;
    if (!s) { box.innerHTML = `<p class="form-hint">${esc(tr('art.loadError', 'The packages could not be read.'))}</p>`; return; }
    const lockTip = s.locked ? tr('art.t.strictLocked', 'Locked by the console configuration (artefacts.strict in config.yaml)')
      : tr('art.t.strict', 'Refuse every package that is not signed by a trusted key');
    const keys = (s.signers || []).map(k => `<tr><td>${esc(k.principal)}</td><td><code>${esc(k.fingerprint)}</code></td>
      <td>${esc(k.key_type)}</td><td>${esc(k.comment || '')}</td>
      <td><button type="button" class="btn btn-sm btn-danger tip" data-art-unsign="${esc(k.fingerprint)}"
           data-tip="${esc(tr('art.t.removeKey', 'Stop trusting this key'))}" title="${esc(tr('art.t.removeKey', 'Stop trusting this key'))}">${icon('trash')}</button></td></tr>`).join('');
    box.innerHTML = `
      <div class="form-row"><label class="tip" data-tip="${esc(lockTip)}" title="${esc(lockTip)}">
        <input type="checkbox" id="art-strict" ${s.strict ? 'checked' : ''} ${s.locked ? 'disabled' : ''}>
        ${esc(tr('art.strict', 'Strict mode'))}</label>
        <div class="form-hint">${esc(s.locked ? tr('art.strictLocked', 'Strict mode is imposed by the console configuration (config.yaml).')
          : tr('art.strictHint', 'Off: an unsigned package, or one signed by an unknown key, is accepted after an explicit acknowledgement written to the journal. A wrong signature is always refused.'))}</div></div>
      <h4>${esc(tr('art.keys', 'Trusted keys'))}</h4>
      ${keys ? `<table class="data-table"><thead><tr><th>${esc(tr('art.principal', 'Principal'))}</th><th>${esc(tr('art.fingerprint', 'Key'))}</th><th>${esc(tr('art.keyType', 'Type'))}</th><th>${esc(tr('art.comment', 'Comment'))}</th><th></th></tr></thead><tbody>${keys}</tbody></table>`
        : `<p class="form-hint">${esc(tr('art.noKeys', 'No trusted key: every signed package is "unknown key".'))}</p>`}
      <div class="form-row"><label for="art-add-key">${esc(tr('art.addKey', 'Add a key (allowed_signers line)'))}</label>
        <input type="text" id="art-add-key" class="form-input tip" placeholder="ops@client ssh-ed25519 AAAA…"
               data-tip="${esc(tr('art.t.addKey', 'principal, then the public key: the line of an allowed_signers file'))}" title="${esc(tr('art.t.addKey', 'principal, then the public key: the line of an allowed_signers file'))}">
        <button type="button" class="btn btn-sm btn-primary tip" id="art-add-key-btn"
                data-tip="${esc(tr('art.t.addKeyBtn', 'Trust packages signed by this key'))}" title="${esc(tr('art.t.addKeyBtn', 'Trust packages signed by this key'))}">${icon('add')} ${esc(tr('art.add', 'Add'))}</button></div>
      <div class="res-feedback" id="art-settings-feedback"></div>`;
  }

  async function putSettings(body) {
    const fb = document.getElementById('art-settings-feedback');
    const r = await fetch('/api/artefacts/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const b = await r.json().catch(() => ({}));
    await loadSettings();
    const fb2 = document.getElementById('art-settings-feedback');
    if (!r.ok && fb2) fb2.textContent = b.error || `HTTP ${r.status}`;
    else if (fb) fb.textContent = '';
  }

  function init() {
    document.addEventListener('click', (e) => {
      if (e.target.closest('#btn-artefacts-import')) { openImport(); return; }
      if (e.target.closest('#btn-artefacts-refresh')) { refresh(); return; }
      const row = e.target.closest('.art-row');
      if (row) { showDetail(row.dataset.artName, row.dataset.artVersion); return; }
      if (e.target.closest('[data-art-remove]')) { remove(); return; }
      if (e.target.closest('[data-art-publish]')) { openPublish(); return; }
      const un = e.target.closest('[data-art-unsign]');
      if (un) { putSettings({ remove_signer: un.dataset.artUnsign }); return; }
      if (e.target.closest('#art-add-key-btn')) {
        const v = (document.getElementById('art-add-key') || {}).value || '';
        if (v.trim()) putSettings({ add_signer: v.trim() });
      }
    });
    document.addEventListener('change', (e) => {
      if (e.target.id === 'art-strict') putSettings({ strict: e.target.checked });
    });
    document.addEventListener('keydown', (e) => {
      const row = e.target.closest && e.target.closest('.art-row');
      if (row && e.key === 'Enter') showDetail(row.dataset.artName, row.dataset.artVersion);
    });
    document.querySelector('.settings-tab[data-stab="artefacts"]')?.addEventListener('click', loadSettings);
  }

  document.addEventListener('DOMContentLoaded', init);
  return { start: refresh, refresh, openImport, openPublish, _verdictHtml: verdictHtml };
})();
window.Artefacts = Artefacts;
