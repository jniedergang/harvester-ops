/**
 * harvester-ops — la fenêtre « Migrer » d'une VM.
 *
 * Un seul geste, trois destinations (v1.45.0, décision de l'exploitant) :
 *   - un autre nœud de ce cluster : la migration à chaud, qui déclenche une
 *     VirtualMachineInstanceMigration et montre les nœuds et l'historique ;
 *   - un autre cluster déclaré : le transfert (vm-transfer.js) ;
 *   - un fichier : l'export dans le magasin (vm-transfer.js).
 */
const VMMigrate = (() => {
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const tr = (k, vars) => i18n.t(k, vars);
  const DESTS = ['node', 'cluster', 'file'];

  function open(cluster, namespace, name, dest) {
    const panelId = `vm-migrate-${cluster}-${namespace}-${name}`;
    const vm = `${namespace}/${name}`;
    const body = `
      <div class="migrate-panel">
        <div class="sub-tabs sub-tabs-inline migrate-dests" role="tablist">
          <button type="button" class="sub-tab tip" role="tab" data-dest="node"
                  data-tip="${esc(tr('migrate.dest.nodeTip'))}">${Icons.svg('node')} <span>${esc(tr('migrate.dest.node'))}</span></button>
          <button type="button" class="sub-tab tip" role="tab" data-dest="cluster"
                  data-tip="${esc(tr('migrate.dest.clusterTip'))}">${Icons.svg('cloud')} <span>${esc(tr('migrate.dest.cluster'))}</span></button>
          <button type="button" class="sub-tab tip" role="tab" data-dest="file"
                  data-tip="${esc(tr('migrate.dest.fileTip'))}">${Icons.svg('download')} <span>${esc(tr('migrate.dest.file'))}</span></button>
        </div>

        <div class="migrate-pane" data-pane="node" role="tabpanel">
          <div class="migrate-status" data-x="status">${esc(tr('common.loading'))}</div>
          <div class="apply-bar" style="margin: 14px 0; padding: 0; border: 0;">
            <button type="button" class="btn btn-primary btn-sm tip" data-x="trigger"
                    data-tip="${esc(tr('migrate.actionTip'))}">${Icons.svg('migrate')} <span>${esc(tr('migrate.now'))}</span></button>
            <button type="button" class="btn btn-secondary btn-sm tip" data-x="refresh"
                    data-tip="${esc(tr('migrate.refreshTip'))}">${esc(tr('migrate.refresh'))}</button>
            <span class="apply-result" data-x="feedback"></span>
          </div>
          <h4 style="margin-top:20px;">${esc(tr('migrate.nodes'))}</h4>
          <table class="data-table" data-x="nodes">
            <thead><tr><th>${esc(tr('migrate.col.node'))}</th><th>${esc(tr('migrate.col.ready'))}</th><th>${esc(tr('migrate.col.schedulable'))}</th></tr></thead>
            <tbody></tbody>
          </table>
          <h4 style="margin-top:20px;">${esc(tr('migrate.history'))}</h4>
          <table class="data-table" data-x="history">
            <thead><tr><th>${esc(tr('migrate.col.migration'))}</th><th>${esc(tr('migrate.col.fromTo'))}</th><th>${esc(tr('migrate.col.phase'))}</th><th>${esc(tr('migrate.col.created'))}</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
        <div class="migrate-pane" data-pane="cluster" role="tabpanel" hidden></div>
        <div class="migrate-pane" data-pane="file" role="tabpanel" hidden></div>
      </div>`;

    const panel = FloatingPanels.open({
      id: panelId,
      title: tr('migrate.title', { vm }),
      icon: 'migrate',
      bodyHtml: body,
      width: 820,
      height: 660,
      restoreSpec: { type: 'vm-migrate', args: { cluster, namespace, name, dest } },
    });
    const root = panel.el;
    const q = (x) => root.querySelector(`[data-pane="node"] [data-x="${x}"]`);
    const rendered = {};

    function show(d) {
      if (!DESTS.includes(d)) d = 'node';
      root.querySelectorAll('[data-dest]').forEach(b => {
        const on = b.dataset.dest === d;
        b.classList.toggle('active', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      root.querySelectorAll('[data-pane]').forEach(p => { p.hidden = p.dataset.pane !== d; });
      if (d !== 'node' && !rendered[d] && window.VMTransfer) {
        rendered[d] = true;
        VMTransfer.render(root.querySelector(`[data-pane="${d}"]`),
                          { kind: d === 'cluster' ? 'migrate' : 'export', cluster, namespace, name });
      }
    }
    root.querySelectorAll('[data-dest]').forEach(b =>
      b.addEventListener('click', () => show(b.dataset.dest)));

    async function refresh() {
      const statusEl = q('status');
      try {
        const d = await fetch(`/api/vm/${encodeURIComponent(cluster)}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/migrate-info`).then(r => r.json());
        statusEl.innerHTML = `
          <div class="kv-strip">
            <span><strong>${esc(tr('migrate.currentNode'))} :</strong> <code>${esc(d.current_node || '?')}</code></span>
            <span><strong>${esc(tr('migrate.phase'))} :</strong> <span class="phase ${esc(d.phase || 'Unknown')}">${esc(d.phase || 'Unknown')}</span></span>
          </div>`;
        const nodesBody = q('nodes').querySelector('tbody');
        nodesBody.innerHTML = (d.nodes || []).map(n => `<tr>
            <td><code>${esc(n.name)}</code> ${n.current ? `<span class="badge ok">${esc(tr('migrate.current'))}</span>` : ''}</td>
            <td>${n.ready === 'True' ? '<span class="badge ok">' + Icons.svg('ok', { size: 14 }) + '</span>' : '<span class="badge fail">' + Icons.svg('fail', { size: 14 }) + '</span>'}</td>
            <td>${n.schedulable ? '<span class="badge ok">' + Icons.svg('ok', { size: 14 }) + '</span>' : `<span class="badge warn">${esc(tr('migrate.cordoned'))}</span>`}</td>
          </tr>`).join('') || '<tr><td colspan="3" class="empty-state">?</td></tr>';
        const histBody = q('history').querySelector('tbody');
        histBody.innerHTML = (d.migrations || []).slice(0, 10).map(m => `<tr>
            <td><code>${esc(m.name)}</code></td>
            <td><code>${esc(m.sourceNode || '?')}</code> / <code>${esc(m.targetNode || '?')}</code></td>
            <td><span class="phase ${m.phase === 'Succeeded' ? 'Running' : m.phase === 'Failed' ? 'Failed' : 'Pending'}">${esc(m.phase)}</span></td>
            <td>${m.creationTimestamp ? esc(new Date(m.creationTimestamp).toLocaleString()) : '?'}</td>
          </tr>`).join('') || `<tr><td colspan="4" class="empty-state">${esc(tr('migrate.none'))}</td></tr>`;
        q('trigger').disabled = d.phase !== 'Running';
      } catch (e) {
        statusEl.innerHTML = `<span style="color:var(--danger)">${esc(e.message)}</span>`;
      }
    }

    async function doMigrate() {
      if (!confirm(tr('migrate.confirm', { name }))) return;
      const fb = q('feedback');
      fb.textContent = tr('migrate.triggering');
      try {
        const r = await fetch(`/api/vm/${encodeURIComponent(cluster)}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/migrate`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
        });
        const d = await r.json();
        if (r.ok) {
          fb.innerHTML = `<span style="color:var(--accent)">${Icons.svg('ok', { size: 14 })} ${esc(tr('migrate.started', { migration: d.migration }))}</span>`;
          setTimeout(refresh, 800);
        } else {
          fb.innerHTML = `<span style="color:var(--danger)">${Icons.svg('fail', { size: 14 })} ${esc(d.detail || d.error)}</span>`;
        }
      } catch (e) {
        fb.innerHTML = `<span style="color:var(--danger)">${Icons.svg('fail', { size: 14 })} ${esc(e.message)}</span>`;
      }
    }

    q('trigger').addEventListener('click', doMigrate);
    q('refresh').addEventListener('click', refresh);
    refresh();
    show(dest || 'node');
  }

  return { open };
})();

window.VMMigrate = VMMigrate;
if (window.FloatingPanels) {
  FloatingPanels.registerType('vm-migrate', (args) =>
    VMMigrate.open(args.cluster, args.namespace, args.name, args.dest));
}
