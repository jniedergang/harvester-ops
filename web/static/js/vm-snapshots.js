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
          <button class="btn btn-primary btn-sm" id="snap-create" title="Create a new snapshot of the current VM disk state">
            ➕ <span>Create snapshot now</span>
          </button>
          <button class="btn btn-secondary btn-sm" id="snap-refresh" title="Refresh the list of snapshots">Refresh</button>
          <span class="apply-result" id="snap-feedback"></span>
        </div>
        <p class="form-hint">
          Snapshots use Longhorn under the hood (VirtualMachineBackup with type=snapshot).
          They are in-cluster and instant. Use Restore to bring back the disk state.
        </p>
        <table class="data-table" id="snap-table">
          <thead><tr>
            <th>Name</th>
            <th>Created</th>
            <th>Ready</th>
            <th>Progress</th>
            <th>Actions</th>
          </tr></thead>
          <tbody><tr><td colspan="5" class="empty-state">Loading…</td></tr></tbody>
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
      if (!silent) tbody.innerHTML = '<tr><td colspan="5" class="empty-state">Loading…</td></tr>';
      try {
        const d = await fetch(`/api/vm/${encodeURIComponent(cluster)}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/snapshots`).then(r => r.json());
        const snaps = d.snapshots || [];
        // Schedule auto-refresh if any snapshot is not yet ready
        if (pollTimer) clearTimeout(pollTimer);
        if (snaps.some(s => !s.ready)) {
          pollTimer = setTimeout(() => refresh(true), 3000);
        }
        if (snaps.length === 0) {
          tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No snapshot yet.</td></tr>';
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
            <td>${s.progress || 0}%</td>
            <td>
              <button class="btn-icon-action" title="Restore this snapshot in place (replaces current VM disk)" data-restore="${s.name}" ${s.ready ? '' : 'disabled'}>↩</button>
              <button class="btn-icon-action" title="Delete this snapshot (cannot be undone)" data-delete="${s.name}">🗑</button>
              ${s.error ? '<span class="badge fail" title="' + s.error + '">error</span>' : ''}
            </td>`;
          tbody.appendChild(tr);
        });
        tbody.querySelectorAll('[data-restore]').forEach(b =>
          b.addEventListener('click', () => doRestore(b.dataset.restore)));
        tbody.querySelectorAll('[data-delete]').forEach(b =>
          b.addEventListener('click', () => doDelete(b.dataset.delete)));
      } catch (e) {
        tbody.innerHTML = `<tr><td colspan="5" class="empty-state" style="color:var(--danger)">${e.message}</td></tr>`;
      }
    }

    async function doCreate() {
      fb.textContent = 'creating…';
      try {
        const r = await fetch(`/api/vm/${encodeURIComponent(cluster)}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/snapshots`, { method: 'POST' });
        const d = await r.json();
        if (r.ok) {
          fb.innerHTML = `<span style="color:var(--accent)">✓ created ${d.name}</span>`;
          refresh();
        } else {
          fb.innerHTML = `<span style="color:var(--danger)">✗ ${d.detail || d.error}</span>`;
        }
      } catch (e) {
        fb.innerHTML = `<span style="color:var(--danger)">✗ ${e.message}</span>`;
      }
    }

    async function doDelete(snap) {
      if (!confirm(`Delete snapshot "${snap}"? This cannot be undone.`)) return;
      fb.textContent = 'deleting…';
      const r = await fetch(`/api/vm/${encodeURIComponent(cluster)}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/snapshots/${encodeURIComponent(snap)}`, { method: 'DELETE' });
      const d = await r.json();
      fb.innerHTML = r.ok
        ? `<span style="color:var(--accent)">✓ deleted</span>`
        : `<span style="color:var(--danger)">✗ ${d.error}</span>`;
      refresh();
    }

    async function doRestore(snap) {
      if (!confirm(`Restore VM "${name}" from snapshot "${snap}"?\n\nThe VM must be stopped before restore. Current disk state will be replaced.`)) return;
      fb.textContent = 'restoring…';
      const r = await fetch(`/api/vm/${encodeURIComponent(cluster)}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/restore`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ snapshot: snap, new_vm: false }),
      });
      const d = await r.json();
      fb.innerHTML = r.ok
        ? `<span style="color:var(--accent)">✓ restore "${d.restore}" started</span>`
        : `<span style="color:var(--danger)">✗ ${d.detail || d.error}</span>`;
    }

    panel.el.querySelector('#snap-create').addEventListener('click', doCreate);
    panel.el.querySelector('#snap-refresh').addEventListener('click', refresh);
    refresh();
  }

  return { open };
})();

window.VMSnapshots = VMSnapshots;
if (window.FloatingPanels) {
  FloatingPanels.registerType('vm-snapshots', (args) =>
    VMSnapshots.open(args.cluster, args.namespace, args.name));
}
