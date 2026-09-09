/**
 * harvester-ops — VM snapshots panel.
 * Lists VirtualMachineBackup (type=snapshot) for a VM, allows create / delete
 * / restore. Restore creates a VirtualMachineRestore.
 */
const VMSnapshots = (() => {

  async function open(cluster, namespace, name) {
    const panelId = `vm-snapshots-${cluster}-${namespace}-${name}`;
    const title = `📸 Snapshots — ${namespace}/${name}`;
    const body = `
      <div class="snapshots-panel">
        <div class="apply-bar" style="margin: 0 0 14px; padding: 0; border: 0;">
          <button class="btn btn-primary btn-sm tip" id="snap-create" data-tip="${i18n.t('snap.createTip')}">
            ➕ <span>${i18n.t('snap.create')}</span>
          </button>
          <button class="btn btn-secondary btn-sm tip" id="snap-refresh" data-tip="${i18n.t('snap.refreshTip')}">${i18n.t('snap.refresh')}</button>
          <span class="apply-result" id="snap-feedback"></span>
        </div>
        <div id="snap-restore-opts" hidden></div>
        <p class="form-hint">
          ${i18n.t('snap.hint')}
        </p>
        <table class="data-table" id="snap-table">
          <thead><tr>
            <th>Name</th>
            <th>Created</th>
            <th>Ready</th>
            <th>Actions</th>
          </tr></thead>
          <tbody><tr><td colspan="4" class="empty-state">Loading…</td></tr></tbody>
        </table>
      </div>`;

    const panel = FloatingPanels.open({
      id: panelId,
      title,
      bodyHtml: body,
      width: 760,
      height: 480,
      restoreSpec: { type: 'vm-snapshots', args: { cluster, namespace, name } },
    });

    const fb = panel.el.querySelector('#snap-feedback');
    const tbody = panel.el.querySelector('#snap-table tbody');

    let pollTimer = null;
    async function refresh(silent = false) {
      if (!silent) tbody.innerHTML = '<tr><td colspan="4" class="empty-state">Loading…</td></tr>';
      try {
        const d = await fetch(`/api/vm/${encodeURIComponent(cluster)}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/snapshots`).then(r => r.json());
        const snaps = d.snapshots || [];
        // Schedule auto-refresh if any snapshot is not yet ready
        if (pollTimer) clearTimeout(pollTimer);
        if (snaps.some(s => !s.ready)) {
          pollTimer = setTimeout(() => refresh(true), 3000);
        }
        if (snaps.length === 0) {
          tbody.innerHTML = '<tr><td colspan="4" class="empty-state">No snapshot yet.</td></tr>';
          return;
        }
        tbody.innerHTML = '';
        snaps.forEach(s => {
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td><code>${s.name}</code></td>
            <td>${s.creationTimestamp ? new Date(s.creationTimestamp).toLocaleString() : '—'}</td>
            <td>${s.ready
              ? '<span class="badge ok">✓ Ready</span>'
              : '<span class="badge warn">in-progress</span>'}</td>
            <td>
              <button class="btn-icon-action tip" data-tip="${i18n.t('snap.restoreTip')}" data-restore="${s.name}" ${s.ready ? '' : 'disabled'}>↩</button>
              <button class="btn-icon-action tip" data-tip="${i18n.t('snap.deleteTip')}" data-delete="${s.name}">🗑</button>
              ${s.error ? '<span class="badge fail" title="' + s.error + '">error</span>' : ''}
            </td>`;
          tbody.appendChild(tr);
        });
        tbody.querySelectorAll('[data-restore]').forEach(b =>
          b.addEventListener('click', () => doRestore(b.dataset.restore)));
        tbody.querySelectorAll('[data-delete]').forEach(b =>
          b.addEventListener('click', () => doDelete(b.dataset.delete)));
      } catch (e) {
        tbody.innerHTML = `<tr><td colspan="4" class="empty-state" style="color:var(--danger)">${e.message}</td></tr>`;
      }
    }

    async function doCreate() {
      fb.textContent = i18n.t('snap.creating');
      try {
        const r = await fetch(`/api/vm/${encodeURIComponent(cluster)}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/snapshots`, { method: 'POST' });
        const d = await r.json();
        if (r.ok) {
          fb.innerHTML = `<span style="color:var(--accent)">✓ ${i18n.t('snap.created')} ${d.name}</span>`;
          refresh();
        } else {
          fb.innerHTML = `<span style="color:var(--danger)">✗ ${d.detail || d.error}</span>`;
        }
      } catch (e) {
        fb.innerHTML = `<span style="color:var(--danger)">✗ ${e.message}</span>`;
      }
    }

    async function doDelete(snap) {
      if (!confirm(i18n.t('snap.confirmDelete', {snap}))) return;
      fb.textContent = i18n.t('snap.deleting');
      const r = await fetch(`/api/vm/${encodeURIComponent(cluster)}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/snapshots/${encodeURIComponent(snap)}`, { method: 'DELETE' });
      const d = await r.json();
      fb.innerHTML = r.ok
        ? `<span style="color:var(--accent)">✓ deleted</span>`
        : `<span style="color:var(--danger)">✗ ${d.error}</span>`;
      refresh();
    }

    // v1.11.0 : restore guidé — le clic ouvre une boîte d'options au lieu
    // d'un confirm() sec : snapshot de sécurité de l'état courant (coché
    // par défaut, le filet pour revenir en arrière) + arrêt automatique
    // de la VM (exigé par Harvester pour un restore in-place).
    function doRestore(snap) {
      const box = panel.el.querySelector('#snap-restore-opts');
      box.hidden = false;
      box.innerHTML = `
        <div class="snap-restore-box">
          <strong>↩ ${esc(i18n.t('snap.restoreTitle', {snap}))}</strong>
          <label class="opt-row"><input type="checkbox" id="snap-opt-pre" checked>
            <span>📸 ${esc(i18n.t('snap.optPre'))}</span></label>
          <label class="opt-row"><input type="checkbox" id="snap-opt-stop" checked>
            <span>⏻ ${esc(i18n.t('snap.optStop'))}</span></label>
          <div class="apply-bar">
            <button class="btn btn-primary btn-sm" id="snap-restore-go">↩ ${esc(i18n.t('snap.restoreGo'))}</button>
            <button class="btn btn-secondary btn-sm" id="snap-restore-cancel">${esc(i18n.t('common.cancel'))}</button>
          </div>
        </div>`;
      box.querySelector('#snap-restore-cancel').addEventListener('click', () => {
        box.hidden = true; box.innerHTML = '';
      });
      box.querySelector('#snap-restore-go').addEventListener('click', () => {
        const pre = box.querySelector('#snap-opt-pre').checked;
        const stop = box.querySelector('#snap-opt-stop').checked;
        box.hidden = true; box.innerHTML = '';
        fireRestore(snap, pre, stop);
      });
    }

    async function fireRestore(snap, pre, stop) {
      fb.textContent = i18n.t('snap.restoring');
      const r = await fetch(`/api/vm/${encodeURIComponent(cluster)}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/restore`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ snapshot: snap, new_vm: false,
                               pre_snapshot: pre, stop_vm: stop }),
      });
      const d = await r.json();
      // v1.10.1 : restore in-place refusé tant que la VM tourne — message
      // actionnable plutôt que l'erreur brute du webhook.
      fb.innerHTML = r.ok
        ? `<span style="color:var(--accent)">✓ restore "${d.restore}" started</span>`
        : (r.status === 409 && d.error === 'vm-running'
          ? `<span style="color:var(--warn)">⏻ ${i18n.t('snap.needsStopped')}</span>`
          : `<span style="color:var(--danger)">✗ ${d.detail || d.error}</span>`);
    }

    panel.el.querySelector('#snap-create').addEventListener('click', doCreate);
    panel.el.querySelector('#snap-refresh').addEventListener('click', refresh);
    refresh();
  }

  function esc(v) {
    return String(v == null ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  return { open };
})();

window.VMSnapshots = VMSnapshots;
if (window.FloatingPanels) {
  FloatingPanels.registerType('vm-snapshots', (args) =>
    VMSnapshots.open(args.cluster, args.namespace, args.name));
}
