/**
 * harvester-ops — Terraform declarations store (v1.5.0, kept by the console
 * since v1.54.0)
 *
 * A *declaration* is a named bundle of N resources of various kinds
 * (vm, image, ssh_key, raw) applied together to one cluster, in its own
 * Terraform state.
 *
 * v1.54.0 : les déclarations ne vivent plus dans le navigateur
 * (localStorage : ni partagées, ni sauvegardées) mais dans la console
 * (`/api/tf-declarations`). Ce module garde la même interface synchrone,
 * sur un cache : chaque changement part au serveur (regroupé, à révision
 * attendue) ; un conflit (quelqu'un d'autre a modifié entre-temps) ramène la
 * version du serveur et le signale par `onError`. Les déclarations laissées
 * dans le navigateur par une version précédente sont reprises une fois.
 *
 * Public API on window.TFDecl:
 *   ready                               → Promise (cache chargé)
 *   load()                              → Promise (relit le serveur)
 *   list() / get(id)                    → declaration[] / declaration | null
 *   create(name, cluster)               → declaration (enregistrée en tâche de fond)
 *   createAsync(name, cluster, desc)    → Promise<declaration>
 *   rename(id, newName)                 → declaration (renameAsync : Promise)
 *   describe(id, text)                  → declaration
 *   remove(id)                          → boolean (removeAsync : Promise, refusé si déployée)
 *   setActive(id) / getActive()
 *   addResource(declId, kind)           → resource     (spec=defaults)
 *   addResourceWithSpec(declId, kind, spec)
 *   updateResource / replaceResourceSpec / removeResource
 *   refresh(id)                         → Promise (relit une déclaration : déployé, dernier plan)
 *   flush(id)                           → Promise (attend l'enregistrement)
 *   onChange(cb) / onError(cb)          → unsub fn
 */

const TFDecl = (() => {
  const LEGACY_KEY = 'harvester_ops_tf_declarations';
  const ACTIVE_KEY = 'harvester_ops_tf_active_decl';
  const SAVE_DELAY = 400;

  const cache = new Map();
  const timers = new Map();
  const saving = new Map();
  const dirty = new Set();
  const listeners = new Set();
  const errorListeners = new Set();
  let readyResolve;
  const ready = new Promise(r => { readyResolve = r; });

  const emit = () => listeners.forEach(cb => { try { cb(); } catch {} });
  const fail = (err) => errorListeners.forEach(cb => { try { cb(err); } catch {} });
  const now = () => new Date().toISOString();

  function _uuid() {
    if (window.crypto && crypto.getRandomValues) {
      const buf = new Uint8Array(6);
      crypto.getRandomValues(buf);
      return Array.from(buf, b => b.toString(16).padStart(2, '0')).join('');
    }
    return Math.random().toString(16).slice(2, 14).padEnd(12, '0');
  }

  async function api(method, url, body) {
    const r = await fetch(url, {
      method, headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined });
    let d = {};
    try { d = await r.json(); } catch { d = {}; }
    if (!r.ok) {
      const e = new Error(d.error || `HTTP ${r.status}`);
      Object.assign(e, { status: r.status, code: d.code, data: d });
      throw e;
    }
    return d;
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
  // Serveur
  // -------------------------------------------------------------------------
  async function load() {
    try {
      const d = await api('GET', '/api/tf-declarations');
      cache.clear();
      (d.declarations || []).forEach(x => cache.set(x.id, x));
      await _importLegacy();
    } catch (e) {
      fail(e);
    } finally {
      readyResolve();
      emit();
    }
  }

  /** Les déclarations qu'une version précédente a laissées dans ce
   *  navigateur partent à la console, une fois ; une copie reste sous
   *  `<clé>_imported`. Un nom déjà pris reçoit un suffixe. */
  async function _importLegacy() {
    let raw = null;
    try { raw = localStorage.getItem(LEGACY_KEY); } catch { return; }
    if (!raw) return;
    let old = [];
    try { old = (JSON.parse(raw) || {}).declarations || []; } catch { return; }
    const fallbackCluster = document.querySelector('#cluster-select')?.value || '';
    const left = [];
    for (const d of old) {
      if (cache.has(d.id)) continue;
      const cluster = d.cluster || fallbackCluster;
      if (!cluster) { left.push(d); continue; }
      for (let n = 1; n <= 5; n++) {
        const name = n === 1 ? d.name : `${d.name} (${n})`;
        try {
          const saved = await api('POST', '/api/tf-declarations', {
            id: d.id, cluster, name, description: '',
            resources: (d.resources || []).map(r => ({ id: r.id, kind: r.kind, spec: r.spec || {} })) });
          cache.set(saved.id, saved);
          break;
        } catch (e) {
          if (e.code === 'name-taken' && n < 5) continue;
          left.push(d);
          fail(e);
          break;
        }
      }
    }
    try {
      localStorage.setItem(LEGACY_KEY + '_imported', raw);
      if (left.length) localStorage.setItem(LEGACY_KEY, JSON.stringify({ schema_version: 1, declarations: left }));
      else localStorage.removeItem(LEGACY_KEY);
    } catch { /* sans stockage */ }
  }

  function _schedule(id, delay = SAVE_DELAY) {
    dirty.add(id);
    clearTimeout(timers.get(id));
    timers.set(id, setTimeout(() => _save(id), delay));
  }

  async function _save(id) {
    timers.delete(id);
    if (saving.has(id)) return saving.get(id);        // relancé à la fin
    const d = cache.get(id);
    if (!d || d._creating) return null;
    dirty.delete(id);
    const body = {
      revision: d.revision, name: d.name, description: d.description || '',
      resources: (d.resources || []).map(r => ({ id: r.id, kind: r.kind, spec: r.spec || {} })),
    };
    const p = api('PUT', `/api/tf-declarations/${encodeURIComponent(id)}`, body)
      .then(saved => {
        const cur = cache.get(id);
        // Une saisie faite pendant l'envoi reste locale : seule la révision
        // et ce que le serveur calcule sont repris.
        if (cur && dirty.has(id)) cache.set(id, Object.assign({}, cur, {
          revision: saved.revision, updated_at: saved.updated_at,
          deployed: saved.deployed, last_plan: saved.last_plan }));
        else cache.set(id, saved);
        emit();
        return saved;
      })
      .catch(e => {
        if (e.code === 'conflict' && e.data && e.data.current) {
          cache.set(id, e.data.current);
          dirty.delete(id);
          emit();
        }
        fail(Object.assign(e, { declId: id }));
        throw e;
      })
      .finally(() => {
        saving.delete(id);
        if (dirty.has(id)) _schedule(id, 50);
      });
    saving.set(id, p);
    return p.catch(() => null);
  }

  function flush(id) {
    if (timers.has(id)) { clearTimeout(timers.get(id)); return _save(id); }
    return saving.get(id) || Promise.resolve(cache.get(id));
  }

  async function refresh(id) {
    try {
      if (!id) return load();
      const d = await api('GET', `/api/tf-declarations/${encodeURIComponent(id)}`);
      if (!dirty.has(id)) cache.set(id, d);
      else cache.set(id, Object.assign({}, cache.get(id), { deployed: d.deployed, last_plan: d.last_plan,
        last_applied_at: d.last_applied_at, last_applied_status: d.last_applied_status }));
      emit();
      return d;
    } catch (e) {
      if (e.status === 404) { cache.delete(id); emit(); }
      return null;
    }
  }

  // -------------------------------------------------------------------------
  // Public API
  // -------------------------------------------------------------------------
  function list(cluster) {
    const all = Array.from(cache.values());
    return cluster ? all.filter(d => d.cluster === cluster) : all;
  }

  function get(id) { return cache.get(id) || null; }

  function createAsync(name, cluster, description = '') {
    return _start(name, cluster, description).saved;
  }

  /** Le brouillon est dans le cache tout de suite ; `saved` dit si la
   *  console l'a accepté (un nom déjà pris est refusé). */
  function _start(name, cluster, description) {
    const id = _uuid();
    const draft = {
      id, cluster: cluster || '', name: name || 'untitled', description,
      resources: [], revision: 0, deployed: [], last_plan: null, _creating: true,
      created_at: now(), updated_at: now(), last_applied_at: null, last_applied_status: null,
    };
    cache.set(id, draft);
    try { localStorage.setItem(ACTIVE_KEY, id); } catch {}
    emit();
    const saved = api('POST', '/api/tf-declarations', { id, cluster, name, description })
      .then(saved => {
        const cur = cache.get(id) || {};
        cache.set(id, Object.assign({}, saved, cur.resources && cur.resources.length
          ? { resources: cur.resources } : {}));
        if (cur.resources && cur.resources.length) _schedule(id, 0);
        emit();
        return cache.get(id);
      })
      .catch(e => {
        cache.delete(id);
        emit();
        fail(e);
        throw e;
      });
    return { draft, saved };
  }

  function create(name, cluster) {
    const { draft, saved } = _start(name, cluster, '');
    saved.catch(() => {});
    return draft;
  }

  function renameAsync(id, newName) {
    const d = cache.get(id);
    if (!d) return Promise.reject(new Error('not found'));
    return api('PUT', `/api/tf-declarations/${encodeURIComponent(id)}`,
               { revision: d.revision, name: newName })
      .then(saved => {
        const cur = cache.get(id);
        cache.set(id, dirty.has(id) ? Object.assign({}, cur, { name: saved.name, revision: saved.revision })
                                    : saved);
        emit();
        return saved;
      });
  }

  function rename(id, newName) {
    renameAsync(id, newName).catch(fail);
    return cache.get(id) || null;
  }

  function describe(id, text) {
    const d = cache.get(id);
    if (!d) return null;
    d.description = text;
    _schedule(id);
    emit();
    return d;
  }

  function removeAsync(id) {
    return api('DELETE', `/api/tf-declarations/${encodeURIComponent(id)}`).then(() => {
      cache.delete(id);
      clearTimeout(timers.get(id));
      if (getActive()?.id === id) setActive(null);
      emit();
      return true;
    });
  }

  function remove(id) {
    removeAsync(id).catch(fail);
    return cache.has(id);
  }

  function setActive(id) {
    try {
      if (id) localStorage.setItem(ACTIVE_KEY, id); else localStorage.removeItem(ACTIVE_KEY);
    } catch {}
    emit();
    return id ? get(id) : null;
  }

  function getActive() {
    let id = null;
    try { id = localStorage.getItem(ACTIVE_KEY); } catch {}
    return id ? get(id) : null;
  }

  function _mutate(declId, fn) {
    const d = cache.get(declId);
    if (!d) return null;
    const out = fn(d);
    d.updated_at = now();
    _schedule(declId);
    emit();
    return out;
  }

  function addResource(declId, kind) {
    return _mutate(declId, d => {
      const res = { id: _uuid(), kind, spec: _defaultsForKind(kind) };
      d.resources.push(res);
      return res;
    });
  }

  /** v1.5.3: add a resource carrying an already-known spec (e.g.
   *  reloaded from the `<safe>.json` sidecar of a deployed resource). */
  function addResourceWithSpec(declId, kind, spec) {
    return _mutate(declId, d => {
      const res = { id: _uuid(), kind, spec: spec || {} };
      d.resources.push(res);
      return res;
    });
  }

  function updateResource(declId, resId, spec) {
    return _mutate(declId, d => {
      const r = d.resources.find(x => x.id === resId);
      if (r) r.spec = Object.assign({}, r.spec, spec || {});
      return r || null;
    });
  }

  function replaceResourceSpec(declId, resId, fullSpec) {
    return _mutate(declId, d => {
      const r = d.resources.find(x => x.id === resId);
      if (r) r.spec = fullSpec || {};
      return r || null;
    });
  }

  function removeResource(declId, resId) {
    return !!_mutate(declId, d => {
      const before = d.resources.length;
      d.resources = d.resources.filter(r => r.id !== resId);
      return d.resources.length < before;
    });
  }

  /** Le résultat d'un apply est enregistré par la console : on relit. */
  function markApplied(declId) {
    refresh(declId);
    return get(declId);
  }

  function onChange(cb) {
    if (typeof cb !== 'function') return () => {};
    listeners.add(cb);
    return () => listeners.delete(cb);
  }

  function onError(cb) {
    if (typeof cb !== 'function') return () => {};
    errorListeners.add(cb);
    return () => errorListeners.delete(cb);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', load);
  else load();

  return {
    ready, load, refresh, flush,
    list, get, create, createAsync, rename, renameAsync, describe, remove, removeAsync,
    setActive, getActive,
    addResource, addResourceWithSpec,
    updateResource, replaceResourceSpec, removeResource,
    markApplied, onChange, onError,
    LEGACY_KEY,
  };
})();

window.TFDecl = TFDecl;
