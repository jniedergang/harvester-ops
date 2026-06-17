/**
 * harvester-ops — VM live migration panel.
 * Shows the current node, lists target nodes available, triggers
 * a VirtualMachineInstanceMigration, and shows recent migration history.
 */
const VMMigrate = (() => {

  async function open(cluster, namespace, name) {
    const panelId = `vm-migrate-${cluster}-${namespace}-${name}`;
    const title = `🔄 Live migrate — ${namespace}/${name}`;
    const body = `
      <div class="migrate-panel">
        <div class="migrate-status" id="migrate-status">Loading…</div>
        <div class="apply-bar" style="margin: 14px 0; padding: 0; border: 0;">
          <button class="btn btn-primary btn-sm" id="migrate-trigger"
                  title="Migrate this VM live to another node. KubeVirt picks the target automatically based on scheduling constraints.">
            🔄 <span>Migrate now</span>
          </button>
          <button class="btn btn-secondary btn-sm" id="migrate-refresh" title="Refresh status and history">Refresh</button>
          <span class="apply-result" id="migrate-feedback"></span>
        </div>
        <h4 style="margin-top:20px;">Available nodes</h4>
        <table class="data-table" id="migrate-nodes">
          <thead><tr><th>Node</th><th>Ready</th><th>Schedulable</th><th></th></tr></thead>
          <tbody></tbody>
        </table>
        <h4 style="margin-top:20px;">Recent migrations</h4>
        <table class="data-table" id="migrate-history">
          <thead><tr><th>Migration</th><th>From → To</th><th>Phase</th><th>Created</th></tr></thead>
          <tbody><tr><td colspan="4" class="empty-state">—</td></tr></tbody>
        </table>
      </div>`;

    const panel = FloatingPanels.open({
      id: panelId,
      title,
      bodyHtml: body,
      width: 780,
      height: 540,
      restoreSpec: { type: 'vm-migrate', args: { cluster, namespace, name } },
    });

    const fb = panel.el.querySelector('#migrate-feedback');
    const statusEl = panel.el.querySelector('#migrate-status');
    const nodesBody = panel.el.querySelector('#migrate-nodes tbody');
    const histBody  = panel.el.querySelector('#migrate-history tbody');

    async function refresh() {
      try {
        const d = await fetch(`/api/vm/${encodeURIComponent(cluster)}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/migrate-info`).then(r => r.json());
        statusEl.innerHTML = `
          <div class="kv-strip">
            <span><strong>Current node:</strong> <code>${d.current_node || '—'}</code></span>
            <span><strong>VMI phase:</strong> <span class="phase ${d.phase || 'Unknown'}">${d.phase || 'Unknown'}</span></span>
          </div>`;
        // Nodes
        nodesBody.innerHTML = '';
        (d.nodes || []).forEach(n => {
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td><code>${n.name}</code> ${n.current ? '<span class="badge ok">current</span>' : ''}</td>
            <td>${n.ready === 'True' ? '<span class="badge ok">✓</span>' : '<span class="badge fail">✗</span>'}</td>
            <td>${n.schedulable ? '<span class="badge ok">✓</span>' : '<span class="badge warn">cordoned</span>'}</td>
            <td></td>`;
          nodesBody.appendChild(tr);
        });
        if (nodesBody.children.length === 0)
          nodesBody.innerHTML = '<tr><td colspan="4" class="empty-state">—</td></tr>';
        // History
        histBody.innerHTML = '';
        (d.migrations || []).slice(0, 10).forEach(m => {
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td><code>${m.name}</code></td>
            <td><code>${m.sourceNode || '?'}</code> → <code>${m.targetNode || '?'}</code></td>
            <td><span class="phase ${m.phase === 'Succeeded' ? 'Running' : m.phase === 'Failed' ? 'Failed' : 'Pending'}">${m.phase}</span></td>
            <td>${m.creationTimestamp ? new Date(m.creationTimestamp).toLocaleString() : '—'}</td>`;
          histBody.appendChild(tr);
        });
        if (histBody.children.length === 0)
          histBody.innerHTML = '<tr><td colspan="4" class="empty-state">No migration yet.</td></tr>';
        // Disable trigger if VM not running
        panel.el.querySelector('#migrate-trigger').disabled = d.phase !== 'Running';
      } catch (e) {
        statusEl.innerHTML = `<span style="color:var(--danger)">${e.message}</span>`;
      }
    }

    async function doMigrate() {
      if (!confirm(`Live-migrate VM "${name}" to another node?\nThe VM keeps running; brief network interruption possible.`)) return;
      fb.textContent = 'triggering…';
      try {
        const r = await fetch(`/api/vm/${encodeURIComponent(cluster)}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/migrate`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
        });
        const d = await r.json();
        if (r.ok) {
          fb.innerHTML = `<span style="color:var(--accent)">✓ ${d.migration} started</span>`;
          setTimeout(refresh, 800);
        } else {
          fb.innerHTML = `<span style="color:var(--danger)">✗ ${d.detail || d.error}</span>`;
        }
      } catch (e) {
        fb.innerHTML = `<span style="color:var(--danger)">✗ ${e.message}</span>`;
      }
    }

    panel.el.querySelector('#migrate-trigger').addEventListener('click', doMigrate);
    panel.el.querySelector('#migrate-refresh').addEventListener('click', refresh);
    refresh();
  }

  return { open };
})();

window.VMMigrate = VMMigrate;
if (window.FloatingPanels) {
  FloatingPanels.registerType('vm-migrate', (args) =>
    VMMigrate.open(args.cluster, args.namespace, args.name));
}
