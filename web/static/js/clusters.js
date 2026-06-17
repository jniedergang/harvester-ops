/**
 * harvester-ops — cluster management UI
 * - "+" button next to the sidebar cluster selector opens a floating
 *   form panel to declare a new cluster.
 * - Settings → Clusters tab lists all declared clusters with edit/test/delete actions.
 *
 * Backend: /api/clusters (GET, POST, PUT, DELETE) + /api/clusters/<n>/{kubeconfig,sshkey,test-kubeconfig,test-ssh}.
 */
const Clusters = (() => {
  const $  = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);

  // ---------------------------------------------------------------------------
  // Settings → Clusters tab
  // ---------------------------------------------------------------------------
  async function refreshList() {
    const out = $('#clusters-list');
    if (!out) return;
    out.innerHTML = '<p class="form-hint">' + (window.i18n ? i18n.t('common.loading') : 'Loading...') + '</p>';
    try {
      const data = await fetch('/api/clusters').then(r => r.json());
      const list = data.clusters || [];
      if (list.length === 0) {
        out.innerHTML = `<p class="form-hint">No cluster declared yet. Click "+ Add cluster" to declare your first one.</p>`;
        return;
      }
      out.innerHTML = '';
      for (const c of list) {
        // Fetch full detail (kubeconfig path, ssh info) — list endpoint only returns name+description
        const card = document.createElement('div');
        card.className = 'cluster-card';
        card.innerHTML = `
          <div class="cluster-card-head">
            <div>
              <h5>${escapeHtml(c.name)}</h5>
              <div class="form-hint">${escapeHtml(c.description || '')} — ${c.node_count} ${i18n.t('settings.connection.nodes')}</div>
            </div>
            <div class="cluster-card-actions">
              <button class="btn btn-sm btn-secondary" data-act="test-kc" data-name="${c.name}" data-i18n="clusters.testKc">${i18n.t('clusters.testKc')}</button>
              <button class="btn btn-sm btn-secondary" data-act="test-ssh" data-name="${c.name}" data-i18n="clusters.testSsh">${i18n.t('clusters.testSsh')}</button>
              <label class="btn btn-sm btn-secondary" title="Replace kubeconfig">
                📄 <input type="file" accept=".yaml,.yml,.kubeconfig" data-act="upload-kc" data-name="${c.name}" style="display:none;">
              </label>
              <label class="btn btn-sm btn-secondary" title="Replace SSH key">
                🔑 <input type="file" data-act="upload-ssh" data-name="${c.name}" style="display:none;">
              </label>
              <button class="btn btn-sm btn-danger" data-act="delete" data-name="${c.name}" data-i18n="clusters.delete">${i18n.t('clusters.delete')}</button>
            </div>
          </div>
          <div class="cluster-card-result" id="cluster-result-${escapeId(c.name)}" style="display:none;"></div>`;
        out.appendChild(card);
      }

      // Wire actions
      out.querySelectorAll('[data-act]').forEach(btn => {
        const act = btn.dataset.act;
        const name = btn.dataset.name;
        if (btn.tagName === 'INPUT') {
          btn.addEventListener('change', () => uploadFile(act, name, btn.files[0]));
        } else {
          btn.addEventListener('click', () => handleAction(act, name));
        }
      });
    } catch (e) {
      out.innerHTML = `<div class="summary-bar bad">${escapeHtml(e.message)}</div>`;
    }
  }

  async function handleAction(act, name) {
    const resEl = $(`#cluster-result-${escapeId(name)}`);
    resEl.style.display = 'block';
    resEl.innerHTML = i18n.t('common.loading');

    try {
      if (act === 'test-kc') {
        const r = await fetch(`/api/clusters/${encodeURIComponent(name)}/test-kubeconfig`, { method: 'POST' });
        const d = await r.json();
        resEl.innerHTML = d.ok
          ? `<div class="summary-bar ok">✓ ${escapeHtml(i18n.t('settings.connection.reachable'))} — server ${escapeHtml(d.server_version || '')}</div>`
          : `<div class="summary-bar bad">✗ ${escapeHtml(d.error || 'failed')}</div>`;
      } else if (act === 'test-ssh') {
        const r = await fetch(`/api/clusters/${encodeURIComponent(name)}/test-ssh`, { method: 'POST' });
        const d = await r.json();
        const rows = (d.results || []).map(n => `
          <tr>
            <td>${escapeHtml(n.hostname)}</td>
            <td><code>${escapeHtml(n.ip)}</code></td>
            <td>${n.ok
              ? '<span class="badge ok">✓ reachable</span>'
              : '<span class="badge fail">✗ ' + escapeHtml((n.detail || 'failed').slice(0, 80)) + '</span>'}
              ${n.sudo_nopasswd ? '<span class="badge ok">sudo</span>' : ''}</td>
          </tr>`).join('');
        resEl.innerHTML = `<table class="perm-table">${rows}</table>`;
      } else if (act === 'delete') {
        if (!confirm(i18n.t('clusters.deleteConfirm').replace('{name}', name))) {
          resEl.style.display = 'none';
          return;
        }
        const r = await fetch(`/api/clusters/${encodeURIComponent(name)}`, { method: 'DELETE' });
        if (r.ok) {
          resEl.innerHTML = `<div class="summary-bar ok">✓ Deleted</div>`;
          setTimeout(refreshList, 600);
        } else {
          const d = await r.json();
          resEl.innerHTML = `<div class="summary-bar bad">${escapeHtml(d.error || 'error')}</div>`;
        }
      }
    } catch (e) {
      resEl.innerHTML = `<div class="summary-bar bad">${escapeHtml(e.message)}</div>`;
    }
  }

  async function uploadFile(act, name, file) {
    if (!file) return;
    const resEl = $(`#cluster-result-${escapeId(name)}`);
    resEl.style.display = 'block';
    resEl.innerHTML = i18n.t('common.loading');
    const endpoint = act === 'upload-kc'
      ? `/api/clusters/${encodeURIComponent(name)}/kubeconfig`
      : `/api/clusters/${encodeURIComponent(name)}/sshkey`;
    const fd = new FormData();
    fd.append('file', file);
    try {
      const r = await fetch(endpoint, { method: 'POST', body: fd });
      const d = await r.json();
      if (r.ok) {
        resEl.innerHTML = `<div class="summary-bar ok">✓ ${escapeHtml(file.name)} uploaded</div>`;
      } else {
        resEl.innerHTML = `<div class="summary-bar bad">${escapeHtml(d.error || 'HTTP ' + r.status)}</div>`;
      }
    } catch (e) {
      resEl.innerHTML = `<div class="summary-bar bad">${escapeHtml(e.message)}</div>`;
    }
  }

  // ---------------------------------------------------------------------------
  // "Add cluster" floating form
  // ---------------------------------------------------------------------------
  function openAddForm() {
    const T = (k) => i18n.t(k);
    const body = `
      <form class="cluster-form" id="cluster-add-form">
        <div class="form-row">
          <label>${T('clusters.form.name')}</label>
          <input type="text" name="name" required pattern="[a-zA-Z0-9][a-zA-Z0-9._-]{0,60}">
        </div>
        <div class="form-row">
          <label>${T('clusters.form.description')}</label>
          <input type="text" name="description">
        </div>
        <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 12px;">
          <div class="form-row">
            <label>${T('clusters.form.sshUser')}</label>
            <input type="text" name="ssh_user" value="rancher">
          </div>
          <div class="form-row">
            <label>${T('clusters.form.sshPort')}</label>
            <input type="number" name="ssh_port" value="22" min="1" max="65535">
          </div>
        </div>
        <div class="form-row">
          <label>${T('clusters.form.kubeconfig')}</label>
          <input type="file" name="kubeconfig" accept=".yaml,.yml,.kubeconfig" required>
        </div>
        <div class="form-row">
          <label>${T('clusters.form.sshKey')}</label>
          <input type="file" name="ssh_key">
        </div>
        <div class="form-row">
          <label>${T('clusters.form.nodes')}</label>
          <div class="nodes-builder" id="nodes-builder">
            <div class="node-row"><input placeholder="${T('clusters.form.hostname')}" data-f="hostname" required><input placeholder="${T('clusters.form.ip')}" data-f="ip" required>
              <select data-f="role"><option value="control-plane">control-plane</option><option value="worker">worker</option></select>
              <button type="button" class="btn-icon-sm" data-remove>×</button></div>
          </div>
          <button type="button" class="btn btn-sm btn-secondary" id="btn-add-node">${T('clusters.form.addNode')}</button>
        </div>
        <div class="form-err" id="form-err" style="color:var(--danger); display:none;"></div>
        <div style="display:flex; gap:8px; margin-top:14px;">
          <button type="submit" class="btn btn-primary btn-sm">${T('clusters.form.submit')}</button>
          <button type="button" class="btn btn-secondary btn-sm" id="btn-cancel-form">${T('clusters.form.cancel')}</button>
        </div>
      </form>`;

    const panel = FloatingPanels.open({
      id: 'cluster-add',
      title: '+ ' + T('clusters.add'),
      bodyHtml: body,
      width: 640,
      height: 600,
    });

    const form = panel.el.querySelector('#cluster-add-form');
    const nodesEl = panel.el.querySelector('#nodes-builder');

    panel.el.querySelector('#btn-add-node').addEventListener('click', () => {
      const row = document.createElement('div');
      row.className = 'node-row';
      row.innerHTML = `<input placeholder="${T('clusters.form.hostname')}" data-f="hostname" required>
                       <input placeholder="${T('clusters.form.ip')}" data-f="ip" required>
                       <select data-f="role"><option value="control-plane">control-plane</option><option value="worker">worker</option></select>
                       <button type="button" class="btn-icon-sm" data-remove>×</button>`;
      nodesEl.appendChild(row);
      row.querySelector('[data-remove]').addEventListener('click', () => row.remove());
    });
    nodesEl.querySelectorAll('[data-remove]').forEach(b =>
      b.addEventListener('click', (e) => e.currentTarget.closest('.node-row').remove()));
    panel.el.querySelector('#btn-cancel-form').addEventListener('click', () => panel.close());

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const err = panel.el.querySelector('#form-err');
      err.style.display = 'none';
      const f = new FormData(form);
      const nodes = Array.from(nodesEl.querySelectorAll('.node-row')).map(r => ({
        hostname: r.querySelector('[data-f="hostname"]').value.trim(),
        ip:       r.querySelector('[data-f="ip"]').value.trim(),
        role:     r.querySelector('[data-f="role"]').value,
      })).filter(n => n.hostname && n.ip);
      if (!f.get('name') || !f.get('kubeconfig') || nodes.length === 0) {
        err.textContent = T('clusters.form.required');
        err.style.display = 'block';
        return;
      }
      const payload = {
        name: f.get('name'),
        description: f.get('description') || '',
        ssh: { user: f.get('ssh_user') || 'rancher', port: parseInt(f.get('ssh_port') || '22') },
        nodes,
      };
      const fd = new FormData();
      fd.append('payload', JSON.stringify(payload));
      fd.append('kubeconfig', f.get('kubeconfig'));
      const sshKey = f.get('ssh_key');
      if (sshKey && sshKey.size > 0) fd.append('ssh_key', sshKey);
      try {
        const res = await fetch('/api/clusters', { method: 'POST', body: fd });
        const data = await res.json();
        if (res.ok) {
          panel.close();
          refreshList();
          // Repopulate the sidebar cluster selector
          if (window.App && window.App.reloadClusters) window.App.reloadClusters();
          else location.reload();
        } else {
          err.textContent = data.error || 'HTTP ' + res.status;
          err.style.display = 'block';
        }
      } catch (e2) {
        err.textContent = e2.message;
        err.style.display = 'block';
      }
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function escapeId(s) {
    return String(s).replace(/[^a-zA-Z0-9_-]/g, '_');
  }

  function init() {
    $('#btn-add-cluster')?.addEventListener('click', openAddForm);
    $('#btn-clusters-add')?.addEventListener('click', openAddForm);
    $('#btn-clusters-refresh')?.addEventListener('click', refreshList);
    // Refresh list when settings → Clusters tab is opened
    document.addEventListener('click', (e) => {
      const tab = e.target.closest('.settings-tab[data-stab="clusters"]');
      if (tab) setTimeout(refreshList, 50);
    });
  }

  return { init, refreshList, openAddForm };
})();

document.addEventListener('DOMContentLoaded', Clusters.init);
window.Clusters = Clusters;
