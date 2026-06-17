/**
 * harvester-ops — Declaration overlay panel (v1.5.5)
 *
 * Each declaration opens in its own FloatingPanel. The panel's body
 * is a 2-column layout:
 *
 *   ┌─ tf-dp-resources ───┬─ tf-dp-detail ───────────────┐
 *   │ Resource A    ✓     │ <kind> <name>            🗑  │
 *   │ Resource B    ⚠     │ [Specs] [Disks] [Networks]  │
 *   │ Resource C    ·     │                              │
 *   │ + Add resource      │ (or empty state if none)     │
 *   └─────────────────────┴──────────────────────────────┘
 *                          [Dry-run] [Apply ▶]
 *
 * Public API on window.TFDeclPanel:
 *   open(declId)        → opens (or brings to front) the panel
 *   refresh(declId)     → re-render an already-open panel after a
 *                         store mutation (Apply / section save / …)
 *   close(declId)       → close it
 */

const TFDeclPanel = (() => {
  // The set of currently-open panel ids, kept here so the orchestrator
  // can refresh all of them on store changes if it wants.
  const openPanels = new Map();   // declId → api

  function esc(v) {
    if (v === null || v === undefined) return '';
    return String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function lang() {
    try { return localStorage.getItem('harvester_ops_lang') || 'en'; }
    catch { return 'en'; }
  }
  function t(o) {
    if (!o) return '';
    if (typeof o === 'string') return o;
    return o[lang()] || o.en || '';
  }
  function _stateIcon(state) {
    if (state === 'ok') return '✓';
    if (state === 'missing') return '!';
    return '·';
  }

  function _resourceListItem(res, isActive) {
    const schema = (window.TF_SCHEMA || {})[res.kind];
    const kindLabel = schema ? (t(schema.label) || res.kind) : res.kind;
    const name = (res.spec && res.spec.name)
      || (res.kind === 'raw' ? '(raw HCL)' : '(unnamed)');
    let summary = '';
    if (window.TFForm && schema) {
      const v = window.TFForm.validateAll(res.spec, res.kind);
      const states = Object.values(v.sections || {}).map(s => s.state || 'ok');
      let cls = 'ok', icon = '✓';
      if (states.some(s => s === 'missing')) { cls = 'missing'; icon = '!'; }
      else if (states.every(s => s === 'empty')) { cls = 'empty'; icon = '·'; }
      summary = `<span class="tf-dp-resitem__state tf-dp-resitem__state--${cls}">${icon}</span>`;
    }
    return `
      <button type="button"
              class="tf-dp-resitem ${isActive ? 'tf-dp-resitem--active' : ''}"
              data-res-id="${esc(res.id)}">
        <span class="tf-dp-resitem__kind">${esc(kindLabel)}</span>
        <span class="tf-dp-resitem__name">${esc(name)}</span>
        ${summary}
      </button>`;
  }

  function _addResourcePicker(declId) {
    const kinds = Object.keys(window.TF_SCHEMA || {});
    const opts = kinds.map(k => {
      const lbl = t(window.TF_SCHEMA[k].label) || k;
      return `<option value="${esc(k)}">${esc(lbl)}</option>`;
    }).join('');
    return `
      <div class="tf-dp-add-resource">
        <select class="tf-dp-add-kind" data-decl="${esc(declId)}">${opts}</select>
        <button type="button" class="btn btn-sm btn-primary tf-dp-add-btn"
                data-decl="${esc(declId)}">+ Add</button>
      </div>`;
  }

  function _renderDetail(decl, res) {
    if (!res) {
      return `<div class="tf-dp-empty">
        <p>No resource selected. Pick one on the left, or add a new one.</p>
      </div>`;
    }
    // Reuse the same card markup as the old in-tab cards — minus the
    // head (kind/name) which we put in the detail header below.
    const cardHtml = window.TFSections.renderCard(decl, res);
    return cardHtml;
  }

  function _renderBody(decl) {
    const decl2 = window.TFDecl.get(decl.id) || decl;
    const activeId = openPanels.get(decl.id)?.activeResId
                     || (decl2.resources[0]?.id || null);
    const resList = decl2.resources.length
      ? decl2.resources.map(r => _resourceListItem(r, r.id === activeId)).join('')
      : '<p class="tf-dp-empty-list">No resources yet.</p>';
    const activeRes = decl2.resources.find(r => r.id === activeId)
                     || decl2.resources[0] || null;
    const detail = _renderDetail(decl2, activeRes);
    return `
      <div class="tf-dp-toolbar">
        <span class="form-hint">Cluster: <code>${esc(decl2.cluster || '—')}</code> ·
                                ${decl2.resources.length} resource(s)</span>
      </div>
      <div class="tf-dp-split">
        <aside class="tf-dp-resources">
          <div class="tf-dp-resources__list">${resList}</div>
          ${_addResourcePicker(decl.id)}
        </aside>
        <section class="tf-dp-detail">
          ${detail}
        </section>
      </div>
      <div class="tf-dp-actions">
        <button class="btn btn-sm tf-dp-dryrun"
                data-decl="${esc(decl.id)}">Dry-run</button>
        <button class="btn btn-sm btn-primary tf-dp-apply"
                data-decl="${esc(decl.id)}">Apply ▶</button>
        <button class="btn btn-sm btn-danger tf-dp-destroy"
                data-decl="${esc(decl.id)}">🧨 Destroy</button>
      </div>`;
  }

  function _wire(host, declId, api) {
    // Switch the active resource when the user clicks a list entry
    host.addEventListener('click', (e) => {
      const item = e.target.closest('.tf-dp-resitem');
      if (item) {
        const entry = openPanels.get(declId);
        if (entry) entry.activeResId = item.dataset.resId;
        _rerender(declId);
        return;
      }
      const addBtn = e.target.closest('.tf-dp-add-btn');
      if (addBtn) {
        const select = host.querySelector(
          `.tf-dp-add-kind[data-decl="${CSS.escape(declId)}"]`);
        const kind = select?.value || 'vm';
        const res = window.TFDecl.addResource(declId, kind);
        const entry = openPanels.get(declId);
        if (entry && res) entry.activeResId = res.id;
        _rerender(declId);
        if (window.TF) window.TF.renderDeclarations();
        return;
      }
      const sec = e.target.closest('.tf-section-btn');
      if (sec) {
        const decl = window.TFDecl.get(declId);
        const res = decl?.resources.find(r => r.id === sec.dataset.resId);
        if (res) {
          window.TFSections.openSectionOverlay(decl, res, sec.dataset.section,
            () => {
              _rerender(declId);
              if (window.TF) window.TF.renderDeclarations();
            });
        }
        return;
      }
      const del = e.target.closest('.tf-resource-card__del');
      if (del) {
        const decl = window.TFDecl.get(declId);
        const res = decl?.resources.find(r => r.id === del.dataset.resId);
        if (!res) return;
        const name = res.spec?.name || '';
        if (!confirm(`Remove ${res.kind} "${name}" from the declaration?`)) return;
        window.TFDecl.removeResource(declId, res.id);
        const entry = openPanels.get(declId);
        if (entry && entry.activeResId === res.id) entry.activeResId = null;
        _rerender(declId);
        if (window.TF) window.TF.renderDeclarations();
        return;
      }
      const apply  = e.target.closest('.tf-dp-apply');
      const dry    = e.target.closest('.tf-dp-dryrun');
      const destroy = e.target.closest('.tf-dp-destroy');
      if (apply || dry) {
        if (window.TF) window.TF.applyDeclaration(declId, !!dry);
        return;
      }
      if (destroy) {
        if (window.TF) window.TF.destroyDeclaration(declId);
        return;
      }
    });
  }

  function _rerender(declId) {
    const entry = openPanels.get(declId);
    if (!entry) return;
    const decl = window.TFDecl.get(declId);
    if (!decl) { close(declId); return; }
    entry.api.setTitle(`📦 ${decl.name}`);
    entry.api.setBody(_renderBody(decl));
    _wire(entry.api.body, declId, entry.api);
  }

  function open(declId) {
    const decl = window.TFDecl.get(declId);
    if (!decl) return null;
    const panelId = `tf-decl-${decl.id}`;
    // Existing panel? bring it to front, refresh.
    if (openPanels.has(declId)) {
      const entry = openPanels.get(declId);
      window.FloatingPanels.open({ id: panelId, title: `📦 ${decl.name}` }); // brings front
      _rerender(declId);
      return entry.api;
    }
    const api = window.FloatingPanels.open({
      id: panelId,
      title: `📦 ${decl.name}`,
      bodyHtml: _renderBody(decl),
      width: 880, height: 540,
      restoreSpec: { type: 'tf-decl-panel', args: { declId } },
      onOpen: (a) => {
        openPanels.set(declId, { api: a, activeResId:
          (decl.resources[0]?.id || null) });
        _wire(a.body, declId, a);
      },
      onClose: () => {
        openPanels.delete(declId);
      },
    });
    return api;
  }

  function refresh(declId) {
    if (openPanels.has(declId)) _rerender(declId);
  }

  function close(declId) {
    const entry = openPanels.get(declId);
    if (entry) entry.api.close();
  }

  // Restore-on-reload: FloatingPanels remembers which IDs were open;
  // we register our type so the same set of decl panels comes back.
  function _registerRestore() {
    if (!window.FloatingPanels || !window.FloatingPanels.registerType) return;
    window.FloatingPanels.registerType('tf-decl-panel', (args) => {
      if (!args || !args.declId) return null;
      return open(args.declId);
    });
  }

  // Subscribe to store changes so any change (rename, spec edit, add/
  // remove resource) flows into every open panel without manual
  // calls from terraform.js or test stubs.
  function _subscribeStoreChanges() {
    if (!window.TFDecl || !window.TFDecl.onChange) return;
    window.TFDecl.onChange(() => {
      // Snapshot the keys because _rerender may close (and so mutate)
      // the underlying Map while we iterate.
      const keys = Array.from(openPanels.keys());
      keys.forEach(declId => _rerender(declId));
    });
  }
  document.addEventListener('DOMContentLoaded', () => {
    _registerRestore();
    _subscribeStoreChanges();
  });

  return { open, refresh, close, openPanels };
})();

window.TFDeclPanel = TFDeclPanel;
