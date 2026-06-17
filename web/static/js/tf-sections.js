/**
 * harvester-ops — section-button cards for Terraform declarations
 *
 * Given a declaration + a resource, render a compact card with one
 * button per section. Click a button → open a FloatingPanel containing
 * the section's form (rendered by tf-form.js with {sectionId}). On
 * Save, persist the spec back into the declarations store and update
 * the card's button colors (✓ ok / ✗ missing / · empty).
 *
 * Public API on window.TFSections:
 *   renderCard(decl, resource)        → HTML string
 *   wireCards(rootEl, decl)           → attaches click handlers
 *   refreshCard(decl, resource)       → recompute button states only
 */

const TFSections = (() => {
  const $  = (root, s) => root.querySelector(s);
  const $$ = (root, s) => Array.from(root.querySelectorAll(s));

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
    if (state === 'ok')      return '✓';
    if (state === 'missing') return '!';
    return '·';
  }

  function renderCard(decl, resource) {
    const schema = (window.TF_SCHEMA || {})[resource.kind];
    if (!schema) {
      return `<div class="tf-resource-card tf-resource-card--unknown">
        Unknown kind: ${esc(resource.kind)}</div>`;
    }
    const sections = schema.sections || [];
    const validation = window.TFForm
      ? window.TFForm.validateAll(resource.spec, resource.kind)
      : { sections: {} };

    const kindLabel = t(schema.label) || resource.kind;
    const name = (resource.spec && (resource.spec.name || ''))
      || (resource.kind === 'raw' ? '(raw HCL)' : '(unnamed)');

    const buttons = sections.map(sec => {
      const v = validation.sections[sec.id] || { valid: true, state: 'ok' };
      const state = v.state || (v.valid ? 'ok' : 'missing');
      return `<button type="button" class="tf-section-btn tf-section-btn--${esc(state)}"
                       data-section="${esc(sec.id)}"
                       data-res-id="${esc(resource.id)}"
                       title="${esc(state === 'missing'
                                  ? 'Missing required fields'
                                  : state === 'empty'
                                    ? 'No values yet'
                                    : 'All required fields filled')}">
        <span class="tf-section-btn__icon">${_stateIcon(state)}</span>
        <span class="tf-section-btn__label">${esc(t(sec.label))}</span>
      </button>`;
    }).join('');

    return `
      <div class="tf-resource-card" data-res-id="${esc(resource.id)}"
           data-kind="${esc(resource.kind)}">
        <div class="tf-resource-card__head">
          <span class="tf-resource-card__kind">${esc(kindLabel)}</span>
          <span class="tf-resource-card__name">${esc(name)}</span>
          <button type="button" class="tf-resource-card__del btn-icon-sm"
                  data-res-id="${esc(resource.id)}"
                  title="Remove this resource from the declaration">🗑</button>
        </div>
        <div class="tf-resource-card__sections">${buttons}</div>
      </div>`;
  }

  /** Re-render just one card's section button strip in place. Cheap. */
  function refreshCard(decl, resource) {
    const cardEl = document.querySelector(
      `.tf-resource-card[data-res-id="${CSS.escape(resource.id)}"]`);
    if (!cardEl) return;
    const headName = cardEl.querySelector('.tf-resource-card__name');
    const name = (resource.spec && resource.spec.name)
      || (resource.kind === 'raw' ? '(raw HCL)' : '(unnamed)');
    if (headName) headName.textContent = name;
    const validation = window.TFForm.validateAll(resource.spec, resource.kind);
    cardEl.querySelectorAll('.tf-section-btn').forEach(btn => {
      const v = validation.sections[btn.dataset.section]
                || { valid: true, state: 'ok' };
      const state = v.state || (v.valid ? 'ok' : 'missing');
      btn.classList.remove(
        'tf-section-btn--ok', 'tf-section-btn--missing', 'tf-section-btn--empty');
      btn.classList.add(`tf-section-btn--${state}`);
      const icon = btn.querySelector('.tf-section-btn__icon');
      if (icon) icon.textContent = _stateIcon(state);
    });
  }

  /** Click on a section button → open the per-section overlay. */
  function wireCards(rootEl, decl, opts) {
    opts = opts || {};
    const onAfterSave = typeof opts.onAfterSave === 'function'
      ? opts.onAfterSave : () => {};
    const onRequestDelete = typeof opts.onRequestDelete === 'function'
      ? opts.onRequestDelete : () => {};

    rootEl.addEventListener('click', (e) => {
      const sectionBtn = e.target.closest('.tf-section-btn');
      if (sectionBtn) {
        const resId = sectionBtn.dataset.resId;
        const sectionId = sectionBtn.dataset.section;
        const decl2 = window.TFDecl.get(decl.id);
        const res = decl2 && decl2.resources.find(r => r.id === resId);
        if (!res) return;
        openSectionOverlay(decl2, res, sectionId, onAfterSave);
        return;
      }
      const del = e.target.closest('.tf-resource-card__del');
      if (del) {
        const resId = del.dataset.resId;
        const decl2 = window.TFDecl.get(decl.id);
        const res = decl2 && decl2.resources.find(r => r.id === resId);
        if (res) onRequestDelete(res);
      }
    });
  }

  function openSectionOverlay(decl, resource, sectionId, onAfterSave) {
    if (!window.FloatingPanels) {
      alert('FloatingPanels module not loaded');
      return;
    }
    const schema = window.TF_SCHEMA[resource.kind];
    const sec = (schema.sections || []).find(s => s.id === sectionId);
    if (!sec) return;
    const panelId = `tf-sec-${decl.id}-${resource.id}-${sectionId}`;
    const title = `${t(schema.label) || resource.kind}: ${t(sec.label)} — `
      + ((resource.spec && resource.spec.name) || '(unnamed)');

    // Form HTML for this section only
    const cluster = decl.cluster || '';
    const bodyHtml = window.TFForm.render(
      resource.kind, cluster, resource.spec || {},
      { sectionId, hideHeader: true },
    ) + `
      <div class="tf-sec-overlay-actions">
        <button type="button" class="btn btn-primary btn-sm tf-sec-save">💾 Save section</button>
        <button type="button" class="btn btn-secondary btn-sm tf-sec-cancel">Cancel</button>
      </div>`;

    const panel = window.FloatingPanels.open({
      id: panelId,
      title,
      bodyHtml,
      width: 720,
      height: 480,
      onOpen: (api) => {
        const host = api.body || api.el;
        const root = host.querySelector('.tf-form');
        if (root) {
          window.TFForm.wire(root, resource.kind, cluster);
        }
        host.querySelector('.tf-sec-save')
          ?.addEventListener('click', () => saveSection(api, decl, resource,
                                                          sectionId, onAfterSave));
        host.querySelector('.tf-sec-cancel')
          ?.addEventListener('click', () => api.close());
      },
    });
    return panel;
  }

  function saveSection(api, decl, resource, sectionId, onAfterSave) {
    const host = api.body || api.el;
    const root = host.querySelector('.tf-form');
    if (!root) return;
    // Read the whole section as a partial spec, then merge into the
    // existing resource.spec (we don't want to wipe other sections).
    const partial = window.TFForm.read(root, resource.kind) || {};
    const merged = Object.assign({}, resource.spec, partial);
    // Nested blocks present in the read partial fully replace whatever
    // was there before (TFForm.read returns the visible array).
    Object.keys(partial).forEach(k => {
      if (Array.isArray(partial[k])) merged[k] = partial[k];
    });
    window.TFDecl.replaceResourceSpec(decl.id, resource.id, merged);
    api.close();
    const updated = window.TFDecl.get(decl.id);
    const res2 = updated && updated.resources.find(r => r.id === resource.id);
    if (res2) refreshCard(updated, res2);
    onAfterSave(updated, res2);
  }

  return { renderCard, refreshCard, wireCards, openSectionOverlay };
})();

window.TFSections = TFSections;
