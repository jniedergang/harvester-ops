/**
 * harvester-ops — Terraform declarations store (v1.5.0)
 *
 * A *declaration* is a named bundle of N resources of various kinds
 * (vm, image, ssh_key, raw) applied together to one cluster. This module
 * is the single source of truth for the localStorage-backed list of
 * declarations and the currently-active one.
 *
 * Public API on window.TFDecl:
 *   list()                              → declaration[]
 *   get(id)                             → declaration | null
 *   create(name, cluster)               → declaration   (added + active)
 *   rename(id, newName)                 → declaration
 *   remove(id)                          → boolean
 *   setActive(id)                       → declaration | null
 *   getActive()                         → declaration | null
 *
 *   addResource(declId, kind)           → resource     (spec=defaults)
 *   updateResource(declId, resId, spec) → resource
 *   removeResource(declId, resId)       → boolean
 *
 *   markApplied(declId, status)         → declaration   (writes timestamp)
 *   onChange(cb)                        → unsub fn      (event fan-out)
 *
 * Storage shape (localStorage.harvester_ops_tf_declarations):
 *   {
 *     schema_version: 1,
 *     declarations: [Declaration, …],
 *     active_declaration_id: string | null,
 *   }
 */

const TFDecl = (() => {
  const STORAGE_KEY = 'harvester_ops_tf_declarations';
  const SCHEMA_VERSION = 1;

  const listeners = new Set();

  function _empty() {
    return {
      schema_version: SCHEMA_VERSION,
      declarations: [],
      active_declaration_id: null,
    };
  }

  function _load() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return _empty();
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object') return _empty();
      // Forward-compat: future schema versions could be migrated here
      if (parsed.schema_version !== SCHEMA_VERSION) {
        console.warn(
          `tf-declarations: storage schema_version ${parsed.schema_version} `
          + `differs from current ${SCHEMA_VERSION} — using as-is`);
      }
      parsed.declarations = Array.isArray(parsed.declarations)
        ? parsed.declarations : [];
      return parsed;
    } catch (e) {
      console.warn('tf-declarations: failed to load, starting fresh:', e);
      return _empty();
    }
  }

  function _save(state) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); }
    catch (e) { console.warn('tf-declarations: save failed:', e); }
    listeners.forEach(cb => { try { cb(); } catch {} });
  }

  function _uuid() {
    // 12 hex chars — collision-safe for the tens-of-declarations scale
    // we expect and fits well in URLs / DOM ids.
    if (window.crypto && crypto.getRandomValues) {
      const buf = new Uint8Array(6);
      crypto.getRandomValues(buf);
      return Array.from(buf, b => b.toString(16).padStart(2, '0')).join('');
    }
    return Math.random().toString(16).slice(2, 14);
  }

  function _defaultsForKind(kind) {
    const schema = (window.TF_SCHEMA || {})[kind];
    if (!schema) return {};
    const spec = {};
    (schema.args || []).forEach(arg => {
      if (arg.default !== undefined) spec[arg.name] = arg.default;
    });
    // Materialise the required minimum of each nested block so the UI
    // can render them immediately (and so `cloudinit { min:1 }` is set
    // even before the user touches the section).
    Object.entries(schema.nested || {}).forEach(([nkey, ndef]) => {
      const min = ndef.min || 0;
      if (min <= 0) return;
      spec[nkey] = [];
      for (let i = 0; i < min; i++) {
        const obj = {};
        (ndef.args || []).forEach(arg => {
          if (arg.default !== undefined) obj[arg.name] = arg.default;
        });
        spec[nkey].push(obj);
      }
    });
    return spec;
  }

  // -------------------------------------------------------------------------
  // Public API
  // -------------------------------------------------------------------------
  function list() { return _load().declarations.slice(); }

  function get(id) {
    return _load().declarations.find(d => d.id === id) || null;
  }

  function create(name, cluster) {
    const state = _load();
    const now = new Date().toISOString();
    const decl = {
      id: _uuid(),
      name: name || `untitled-${state.declarations.length + 1}`,
      cluster: cluster || '',
      resources: [],
      created_at: now,
      updated_at: now,
      last_applied_at: null,
      last_applied_status: null,
    };
    state.declarations.push(decl);
    state.active_declaration_id = decl.id;
    _save(state);
    return decl;
  }

  function rename(id, newName) {
    const state = _load();
    const d = state.declarations.find(x => x.id === id);
    if (!d) return null;
    d.name = newName;
    d.updated_at = new Date().toISOString();
    _save(state);
    return d;
  }

  function remove(id) {
    const state = _load();
    const before = state.declarations.length;
    state.declarations = state.declarations.filter(d => d.id !== id);
    if (state.active_declaration_id === id) {
      state.active_declaration_id =
        state.declarations.length ? state.declarations[0].id : null;
    }
    _save(state);
    return state.declarations.length < before;
  }

  function setActive(id) {
    const state = _load();
    if (id && !state.declarations.find(d => d.id === id)) return null;
    state.active_declaration_id = id || null;
    _save(state);
    return state.declarations.find(d => d.id === state.active_declaration_id)
           || null;
  }

  function getActive() {
    const state = _load();
    if (!state.active_declaration_id) return null;
    return state.declarations.find(d => d.id === state.active_declaration_id)
           || null;
  }

  function addResource(declId, kind) {
    const state = _load();
    const d = state.declarations.find(x => x.id === declId);
    if (!d) return null;
    const res = {
      id: _uuid(),
      kind,
      spec: _defaultsForKind(kind),
    };
    d.resources.push(res);
    d.updated_at = new Date().toISOString();
    _save(state);
    return res;
  }

  /** v1.5.3: add a resource carrying an already-known spec (e.g.
   *  reloaded from the `<safe>.json` sidecar of a deployed resource).
   *  Bypasses _defaultsForKind() so the imported spec is preserved
   *  byte-for-byte. */
  function addResourceWithSpec(declId, kind, spec) {
    const state = _load();
    const d = state.declarations.find(x => x.id === declId);
    if (!d) return null;
    const res = { id: _uuid(), kind, spec: spec || {} };
    d.resources.push(res);
    d.updated_at = new Date().toISOString();
    _save(state);
    return res;
  }

  function updateResource(declId, resId, spec) {
    const state = _load();
    const d = state.declarations.find(x => x.id === declId);
    if (!d) return null;
    const r = d.resources.find(x => x.id === resId);
    if (!r) return null;
    r.spec = Object.assign({}, r.spec, spec || {});
    d.updated_at = new Date().toISOString();
    _save(state);
    return r;
  }

  /** Replace a resource's spec wholesale — used when the section
   *  overlay saves the entire form back to the store. */
  function replaceResourceSpec(declId, resId, fullSpec) {
    const state = _load();
    const d = state.declarations.find(x => x.id === declId);
    if (!d) return null;
    const r = d.resources.find(x => x.id === resId);
    if (!r) return null;
    r.spec = fullSpec || {};
    d.updated_at = new Date().toISOString();
    _save(state);
    return r;
  }

  function removeResource(declId, resId) {
    const state = _load();
    const d = state.declarations.find(x => x.id === declId);
    if (!d) return false;
    const before = d.resources.length;
    d.resources = d.resources.filter(r => r.id !== resId);
    d.updated_at = new Date().toISOString();
    _save(state);
    return d.resources.length < before;
  }

  function markApplied(declId, status) {
    const state = _load();
    const d = state.declarations.find(x => x.id === declId);
    if (!d) return null;
    d.last_applied_at = new Date().toISOString();
    d.last_applied_status = status || null;
    _save(state);
    return d;
  }

  function onChange(cb) {
    if (typeof cb !== 'function') return () => {};
    listeners.add(cb);
    return () => listeners.delete(cb);
  }

  /** Test/debug helper — purge every declaration. Never called by the UI. */
  function _clearAll() { _save(_empty()); }

  return {
    list, get, create, rename, remove,
    setActive, getActive,
    addResource, addResourceWithSpec,
    updateResource, replaceResourceSpec, removeResource,
    markApplied, onChange, _clearAll,
    STORAGE_KEY, SCHEMA_VERSION,
  };
})();

window.TFDecl = TFDecl;
