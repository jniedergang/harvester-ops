/**
 * harvester-ops — Terraform form renderer (v1.4.36, Phase A)
 *
 * Generic schema-driven form for Terraform resource creation. The schema
 * lives in tf-schema.js. This module turns a schema entry into HTML, fills
 * dropdowns by fetching the matching list endpoint, and converts the
 * rendered form back into a JSON spec ready for POST /api/terraform/.../apply.
 *
 * Public API (on window.TFForm):
 *   render(kind, currentCluster, values?)  → HTML string
 *   wire(rootEl, kind, currentCluster)     → populates ref dropdowns,
 *                                            attaches +Add/−Remove handlers
 *                                            for nested blocks
 *   read(rootEl, kind)                     → { …, disk: [...], … } spec
 */

const TFForm = (() => {
  const $  = (root, s) => root.querySelector(s);
  const $$ = (root, s) => Array.from(root.querySelectorAll(s));

  /** Escape user content so we can safely interpolate into HTML strings. */
  function esc(v) {
    if (v === null || v === undefined) return '';
    return String(v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function lang() {
    // v1.8.0: was 'harvester_ops_lang' — a key nobody ever writes (i18n.js
    // persists under 'harvester_ops_language'), so the {en,fr} labels of
    // TF_SCHEMA never showed in French since the feature shipped.
    try { return localStorage.getItem('harvester_ops_language') || 'en'; }
    catch { return 'en'; }
  }

  /** v1.8.0 — accept either a registered kind name (string, looked up in
   *  TF_SCHEMA as before) or a schema OBJECT supplied by the caller. Lets
   *  other panels (VM disk/network editor) reuse this engine without
   *  registering fake kinds in TF_SCHEMA (the backend test asserts every
   *  TF_SCHEMA kind has an HCL renderer branch). */
  function resolveSchema(kindOrSchema) {
    if (kindOrSchema && typeof kindOrSchema === 'object') return kindOrSchema;
    return (window.TF_SCHEMA || {})[kindOrSchema];
  }

  function t(obj) {
    if (!obj) return '';
    if (typeof obj === 'string') return obj;
    return obj[lang()] || obj.en || '';
  }

  // -------------------------------------------------------------------------
  // Field renderers — one per type. All return an HTML string for the
  // <td>/<div> that wraps the actual <input>/<select>/<textarea>.
  // -------------------------------------------------------------------------
  function renderField(arg, value, path) {
    const id = `tf-${path}`;
    const name = esc(path);
    const v = value !== undefined ? value : (arg.default !== undefined ? arg.default : '');
    const help = arg.description ? t(arg.description) : '';
    const req = arg.required ? ' <span class="tf-required" title="required">*</span>' : '';
    const tip = help ? ` data-tip="${esc(help)}"` : '';
    // v1.8.0: optional bilingual label per arg; raw name stays the fallback
    const labelText = arg.label ? t(arg.label) : arg.name;
    const labelHtml = `<label for="${id}" class="tf-label tip"${tip}>${esc(labelText)}${req}</label>`;

    let control = '';
    switch (arg.type) {
      case 'text':
        control = `<input type="text" id="${id}" name="${name}"
                          value="${esc(v)}"
                          ${arg.required ? 'required' : ''}
                          ${arg.validate ? `pattern="${esc(arg.validate.source)}"` : ''}>`;
        break;
      case 'int':
        control = `<input type="number" id="${id}" name="${name}"
                          value="${esc(v)}"
                          ${arg.min !== undefined ? `min="${arg.min}"` : ''}
                          ${arg.max !== undefined ? `max="${arg.max}"` : ''}
                          ${arg.required ? 'required' : ''}>`;
        break;
      case 'bool':
        control = `<input type="checkbox" id="${id}" name="${name}"
                          ${v ? 'checked' : ''}>`;
        break;
      case 'enum':
        control = `<select id="${id}" name="${name}"
                          ${arg.required ? 'required' : ''}>` +
          (arg.required ? '' : '<option value=""></option>') +
          arg.enum_values.map(e =>
            `<option value="${esc(e)}" ${e === v ? 'selected' : ''}>${esc(e)}</option>`
          ).join('') +
          `</select>`;
        break;
      case 'ref': {
        const placeholder = `— ${arg.required ? 'select' : 'optional'} —`;
        // Multi-select shown as a <select multiple>; renderer is the same
        const multi = arg.multiple ? ' multiple size="4"' : '';
        control =
          `<div class="tf-ref-wrap">
             <select id="${id}" name="${name}" data-ref-endpoint="${esc(arg.ref_endpoint)}"
                     data-ref-value="${esc(arg.ref_value_field || 'name')}"
                     data-ref-label="${esc(arg.ref_label_field || arg.ref_value_field || 'name')}"
                     data-ref-namespaced="${arg.ref_namespaced ? '1' : '0'}"
                     data-ref-default="${esc(arg.default || '')}"
                     data-ref-selected="${esc(v || '')}"
                     ${arg.required ? 'required' : ''}${multi}>
               <option value="">${esc(placeholder)}</option>
               ${v ? `<option value="${esc(v)}" selected>${esc(v)}</option>` : ''}
             </select>` +
          (arg.creatable
            ? `<button type="button" class="btn btn-sm tf-ref-create tip"
                       data-creates="${esc(refKindFromEndpoint(arg.ref_endpoint))}"
                       data-target="${id}"
                       data-tip="${esc({en: 'Create a new ' + arg.name, fr: 'Créer un nouveau ' + arg.name}[lang()] || '')}">+</button>`
            : '') +
          `</div>`;
        break;
      }
      case 'textarea':
        control = `<textarea id="${id}" name="${name}" rows="${arg.rows || 6}"
                             ${arg.required ? 'required' : ''}>${esc(v)}</textarea>`;
        break;
      default:
        control = `<em>unsupported type: ${esc(arg.type)}</em>`;
    }

    return `<div class="tf-field tf-type-${esc(arg.type)}">${labelHtml}${control}</div>`;
  }

  /**
   * Map a ref endpoint like '/api/namespaces' back to the schema kind
   * ('namespace') so the inline-create button (Phase B) knows which mini-form
   * to render. Conventional plural → singular mapping; falls back to the
   * pathname's last segment.
   */
  function refKindFromEndpoint(endpoint) {
    const last = (endpoint || '').replace(/^.*\//, '');
    const map = {
      namespaces: 'namespace',
      images: 'image',
      networks: 'network',
      sshkeys: 'ssh_key',
      storageclasses: 'storageclass',
      cloudinits: 'cloudinit_secret',
    };
    return map[last] || last;
  }

  // -------------------------------------------------------------------------
  // Nested block renderer — one section per nested key, with +Add / −Remove
  // -------------------------------------------------------------------------
  function renderNested(kind, nested, valuesByKey) {
    return Object.entries(nested).map(([nkey, ndef]) => {
      const min = ndef.min || 0;
      const max = ndef.max || 99;
      const existing = (valuesByKey && valuesByKey[nkey]) || [];
      // Always render at least `min` instances; extras are the user's
      // current state preserved across re-renders.
      const count = Math.max(min, existing.length);
      const sectionId = `tf-block-${nkey}`;
      const items = [];
      for (let i = 0; i < count; i++) {
        items.push(renderNestedInstance(kind, nkey, ndef, i, existing[i] || {}));
      }
      return `
        <fieldset class="tf-block" id="${sectionId}" data-block="${esc(nkey)}"
                  data-min="${min}" data-max="${max}">
          <legend>${esc(t(ndef.label))}${min > 0 ? ' <span class="tf-required">*</span>' : ''}</legend>
          <div class="tf-block-list">${items.join('')}</div>
          <button type="button" class="btn btn-sm tf-block-add"
                  data-block="${esc(nkey)}">+ Add ${esc(t(ndef.label))}</button>
        </fieldset>`;
    }).join('');
  }

  function renderNestedInstance(kind, nkey, ndef, index, values) {
    const path = `${nkey}[${index}]`;
    const min = ndef.min || 0;
    const canRemove = index >= min;
    // v1.8.0: optional per-item summary header (e.g. "💾 rootdisk — virtio · 20Gi")
    const head = typeof ndef.itemTitle === 'function'
      ? `<div class="tf-block-item__head">${esc(ndef.itemTitle(values || {}, index))}</div>`
      : '';
    return `
      <div class="tf-block-item" data-block-index="${index}">
        ${head}
        ${ndef.args.map(arg => renderField(arg, values[arg.name], `${path}.${arg.name}`)).join('')}
        ${canRemove
          ? `<button type="button" class="btn btn-sm btn-secondary tf-block-remove"
                     data-block="${esc(nkey)}">− Remove</button>`
          : ''}
      </div>`;
  }

  // -------------------------------------------------------------------------
  // Top-level render
  //
  // opts:
  //   - sectionId: render ONLY the args and/or the nested block belonging
  //     to that section (per TF_SCHEMA[kind].sections). Used by the v1.5.0
  //     declaration UI to put each section behind its own button.
  //   - hideHeader: skip the <p class="tf-desc"> blurb (for sub-panels).
  // -------------------------------------------------------------------------
  function render(kind, currentCluster, values, opts) {
    const schema = resolveSchema(kind);
    if (!schema) {
      return `<div class="tf-error">Unknown resource kind: ${esc(kind)}</div>`;
    }
    values = values || {};
    opts = opts || {};
    const head = (schema.description && !opts.hideHeader)
      ? `<p class="tf-desc">${esc(t(schema.description))}</p>` : '';

    let argsToRender = schema.args || [];   // nested-only schemas (v1.8.0)
    let nestedToRender = schema.nested;
    if (opts.sectionId && Array.isArray(schema.sections)) {
      const sec = schema.sections.find(s => s.id === opts.sectionId);
      if (sec) {
        argsToRender = sec.args
          ? (schema.args || []).filter(a => sec.args.includes(a.name))
          : [];
        nestedToRender = sec.nested && schema.nested && schema.nested[sec.nested]
          ? { [sec.nested]: schema.nested[sec.nested] }
          : null;
      }
    }
    const main = argsToRender.map(arg =>
      renderField(arg, values[arg.name], arg.name)
    ).join('');
    const blocks = nestedToRender ? renderNested(kind, nestedToRender, values) : '';
    return `
      <div class="tf-form" data-kind="${esc(typeof kind === 'string' ? kind : (schema.id || 'custom'))}" data-cluster="${esc(currentCluster || '')}"
           ${opts.sectionId ? `data-section="${esc(opts.sectionId)}"` : ''}>
        ${head}
        ${main ? `<div class="tf-args">${main}</div>` : ''}
        ${blocks}
      </div>`;
  }

  // -------------------------------------------------------------------------
  // Wire — populate ref dropdowns + nested-block buttons
  // -------------------------------------------------------------------------
  async function wire(rootEl, kind, currentCluster) {
    if (!rootEl) return;
    const schema = resolveSchema(kind) || {};

    // v1.4.39: attach +Add / −Remove handlers SYNCHRONOUSLY so the
    // form is interactive immediately, even while the ref dropdowns
    // are still being populated. Before this, a click on +Add during
    // the kubectl fetch (a few hundred ms on a real cluster, more on
    // a slow one) silently dropped because the listener wasn't yet
    // bound — the dropdown felt "stuck" for a moment then worked.

    // 1. Nested-block +Add / −Remove handlers (synchronous)
    $$(rootEl, '.tf-block-add').forEach(btn => {
      btn.addEventListener('click', () => {
        const nkey = btn.dataset.block;
        const ndef = (schema.nested || {})[nkey];
        const fs   = btn.closest('.tf-block');
        const list = fs.querySelector('.tf-block-list');
        const max  = parseInt(fs.dataset.max, 10) || 99;
        const items = list.querySelectorAll('.tf-block-item');
        if (items.length >= max) return;
        // v1.8.0: after a mid-list removal the remaining data-block-index
        // values are sparse (0,2,…) — reusing `length` as the next index
        // would collide with an existing input name. Take max+1 instead.
        const idx = 1 + Math.max(-1, ...Array.from(items)
          .map(it => parseInt(it.dataset.blockIndex, 10) || 0));
        list.insertAdjacentHTML('beforeend',
          renderNestedInstance(kind, nkey, ndef, idx, {}));
        // Re-wire ref dropdowns inside the freshly added block
        wire(list.lastElementChild, kind, currentCluster);
      });
    });

    rootEl.addEventListener('click', (e) => {
      const rem = e.target.closest('.tf-block-remove');
      if (!rem) return;
      e.preventDefault();
      const item = rem.closest('.tf-block-item');
      const fs   = rem.closest('.tf-block');
      const min  = parseInt(fs.dataset.min, 10) || 0;
      const remaining = fs.querySelectorAll('.tf-block-item').length;
      if (remaining <= min) return;
      item.remove();
    });

    // 3. Phase B placeholder: inline-create buttons
    $$(rootEl, '.tf-ref-create').forEach(btn => {
      btn.addEventListener('click', () => {
        const evt = new CustomEvent('tf-inline-create', {
          bubbles: true,
          detail: {
            kind: btn.dataset.creates,
            targetId: btn.dataset.target,
            cluster: currentCluster,
          },
        });
        rootEl.dispatchEvent(evt);
      });
    });

    // 4. Fetch options for every ref dropdown (deduped by endpoint).
    //    Runs LAST so kubectl latency never blocks the user from
    //    interacting with the form's structural controls (+Add/−Remove
    //    etc.).
    const refSelects = $$(rootEl, 'select[data-ref-endpoint]');
    const byEndpoint = new Map();
    refSelects.forEach(sel => {
      const ep = sel.dataset.refEndpoint;
      if (!byEndpoint.has(ep)) byEndpoint.set(ep, []);
      byEndpoint.get(ep).push(sel);
    });
    await Promise.all([...byEndpoint.entries()].map(async ([ep, selects]) => {
      let items = [];
      try {
        const r = await fetch(`${ep}/${encodeURIComponent(currentCluster)}`);
        if (r.ok) items = await r.json();
      } catch (e) {
        console.warn(`tf-form: ${ep} fetch failed`, e);
      }
      selects.forEach(sel => populateRef(sel, items));
    }));
  }

  function populateRef(sel, items) {
    const valueField = sel.dataset.refValue || 'name';
    const labelField = sel.dataset.refLabel || valueField;
    const namespaced = sel.dataset.refNamespaced === '1';
    const defaultVal = sel.dataset.refDefault || '';
    const prev = sel.value;
    const previouslySelected = new Set(
      sel.multiple ? Array.from(sel.selectedOptions).map(o => o.value) : [prev]
    );

    // Keep the placeholder option, replace the rest
    const placeholder = sel.options[0] && !sel.options[0].value
      ? sel.options[0].outerHTML : '';
    const opts = items.map(it => {
      const raw = it[valueField] ?? '';
      const lbl = it[labelField] ?? raw;
      const value = namespaced && it.namespace
        ? `${it.namespace}/${raw}` : raw;
      return `<option value="${esc(value)}">${esc(lbl)}${it.namespace && !namespaced ? ` (${esc(it.namespace)})` : ''}</option>`;
    }).join('');
    sel.innerHTML = placeholder + opts;

    // Restore previous selection or default
    if (sel.multiple) {
      Array.from(sel.options).forEach(o => {
        if (previouslySelected.has(o.value) && o.value) o.selected = true;
      });
    } else {
      // v1.8.0: edit flows pass the item's current value via
      // data-ref-selected — it wins over the schema default, and survives
      // even when the fetched list does not contain it (stale ref).
      const initial = sel.dataset.refSelected || '';
      if (prev && Array.from(sel.options).some(o => o.value === prev)) {
        sel.value = prev;
      } else if (initial) {
        if (!Array.from(sel.options).some(o => o.value === initial)) {
          sel.insertAdjacentHTML('beforeend',
            `<option value="${esc(initial)}">${esc(initial)}</option>`);
        }
        sel.value = initial;
      } else if (defaultVal &&
                 Array.from(sel.options).some(o => o.value === defaultVal)) {
        sel.value = defaultVal;
      }
    }
  }

  // -------------------------------------------------------------------------
  // Read — extract spec from the rendered form
  // -------------------------------------------------------------------------
  function read(rootEl, kind, opts) {
    const schema = resolveSchema(kind);
    if (!schema) return null;
    opts = opts || {};
    const spec = {};
    (schema.args || []).forEach(arg => {
      const el = rootEl.querySelector(`[name="${cssEscape(arg.name)}"]`);
      if (!el) return;
      const v = readControl(el, arg);
      if (v !== undefined && v !== '') spec[arg.name] = v;
    });
    if (schema.nested) {
      Object.entries(schema.nested).forEach(([nkey, ndef]) => {
        const items = $$(rootEl, `.tf-block[data-block="${cssEscape(nkey)}"] .tf-block-item`);
        const arr = items.map((item) => {
          // v1.8.0: look fields up INSIDE each rendered item by name
          // suffix instead of recomputing `${nkey}[${idx}]` from the loop
          // index — after a mid-list removal the DOM indices are sparse
          // and the old path-based lookup silently dropped every field.
          const obj = {};
          ndef.args.forEach(arg => {
            const el = item.querySelector(`[name$=".${cssEscape(arg.name)}"]`);
            if (!el) return;
            const v = readControl(el, arg);
            if (v !== undefined && v !== '') obj[arg.name] = v;
          });
          return obj;
        }).filter(o => Object.keys(o).length > 0);
        // v1.8.0: full-state consumers (VM editor) need an emptied list to
        // BE the state (merge patch replaces arrays wholesale). Default
        // keeps the historical TF behaviour (absent key).
        if (arr.length > 0 || opts.emitEmptyLists) spec[nkey] = arr;
      });
    }
    return spec;
  }

  function readControl(el, arg) {
    if (arg.type === 'bool')     return el.checked;
    if (arg.type === 'int') {
      const n = parseInt(el.value, 10);
      return Number.isFinite(n) ? n : undefined;
    }
    if (arg.type === 'ref' && arg.multiple) {
      return Array.from(el.selectedOptions).map(o => o.value).filter(Boolean);
    }
    return el.value;
  }

  /** CSS.escape polyfill — selectors carry user names. */
  function cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/[^a-zA-Z0-9_-]/g, c => '\\' + c);
  }

  // -------------------------------------------------------------------------
  // Validation (v1.5.0)
  //
  // Each section button on a resource card shows ✓ / ✗ / · based on whether
  // every required arg in that section has a non-empty value. We never check
  // type / regex constraints here — only "is it filled" — so the user sees a
  // green button as soon as they've answered everything the schema demands.
  // Server-side validation still catches malformed values.
  // -------------------------------------------------------------------------

  function _isFilled(v) {
    if (v === undefined || v === null) return false;
    if (Array.isArray(v)) return v.length > 0;
    if (typeof v === 'boolean') return true; // a checkbox is always "answered"
    if (typeof v === 'number')  return true;
    return String(v).trim() !== '';
  }

  function _argRequired(arg) { return !!arg.required; }

  function validateArgs(spec, args, pathPrefix) {
    const missing = [];
    (args || []).forEach(arg => {
      if (!_argRequired(arg)) return;
      const v = spec ? spec[arg.name] : undefined;
      if (!_isFilled(v)) missing.push(pathPrefix + arg.name);
    });
    return missing;
  }

  function validateNested(spec, nestedKey, nestedDef) {
    // spec[nestedKey] may be an array (typical) or undefined.
    const items = spec && spec[nestedKey];
    const arr = Array.isArray(items) ? items : (items ? [items] : []);
    const min = nestedDef.min || 0;
    const missing = [];
    if (arr.length < min) {
      for (let i = arr.length; i < min; i++) {
        // Each missing item counts every required arg as missing
        (nestedDef.args || [])
          .filter(_argRequired)
          .forEach(a => missing.push(`${nestedKey}[${i}].${a.name}`));
      }
    }
    arr.forEach((item, i) => {
      validateArgs(item, nestedDef.args, `${nestedKey}[${i}].`).forEach(m =>
        missing.push(m));
    });
    return missing;
  }

  /** Validate ONE section. Returns { valid, missing: [...] }. */
  function validateSection(spec, kind, sectionId) {
    const schema = resolveSchema(kind);
    if (!schema || !Array.isArray(schema.sections)) {
      return { valid: true, missing: [] };
    }
    const sec = schema.sections.find(s => s.id === sectionId);
    if (!sec) return { valid: true, missing: [] };
    let missing = [];
    if (sec.args) {
      const argsObjs = schema.args.filter(a => sec.args.includes(a.name));
      missing = missing.concat(validateArgs(spec, argsObjs, ''));
    }
    if (sec.nested && schema.nested && schema.nested[sec.nested]) {
      missing = missing.concat(
        validateNested(spec, sec.nested, schema.nested[sec.nested]));
    }
    return { valid: missing.length === 0, missing };
  }

  /** Section state: 'ok' (all required filled), 'missing' (any required
   *  empty AND some user input present), 'empty' (nothing filled at all). */
  function sectionState(spec, kind, sectionId) {
    const v = validateSection(spec, kind, sectionId);
    if (v.valid) return 'ok';
    // Distinguish "nothing entered yet" from "started but incomplete"
    const schema = resolveSchema(kind);
    const sec = (schema?.sections || []).find(s => s.id === sectionId);
    if (!sec) return 'ok';
    const anyInput =
      (sec.args || []).some(name => _isFilled(spec && spec[name])) ||
      (sec.nested && Array.isArray(spec && spec[sec.nested]) &&
       spec[sec.nested].some(it =>
         Object.values(it || {}).some(_isFilled)));
    return anyInput ? 'missing' : 'empty';
  }

  /** Validate every section of a kind. Returns
   *  { valid, sections: { [sectionId]: {valid, missing, state} } }. */
  function validateAll(spec, kind) {
    const schema = resolveSchema(kind);
    if (!schema || !Array.isArray(schema.sections)) {
      return { valid: true, sections: {} };
    }
    const sections = {};
    let allValid = true;
    schema.sections.forEach(sec => {
      const r = validateSection(spec, kind, sec.id);
      r.state = sectionState(spec, kind, sec.id);
      sections[sec.id] = r;
      if (!r.valid) allValid = false;
    });
    return { valid: allValid, sections };
  }

  return {
    render, wire, read, refKindFromEndpoint,
    validateSection, validateAll, sectionState,
  };
})();

window.TFForm = TFForm;
