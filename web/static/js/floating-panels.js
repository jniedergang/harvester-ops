/**
 * harvester-ops — Floating panels system
 *
 * Reusable component for draggable + resizable + minimizable overlay panels.
 * Minimized panels dock on a horizontal bar ABOVE the running-actions dock
 * (or at the bottom of the screen if the dock is hidden).
 *
 * Usage:
 *   const panel = FloatingPanels.open({
 *     id: 'unique-id',
 *     title: '⚙ Edit VM — default/foo',
 *     bodyHtml: '<div>...</div>',
 *     width: 760, height: 540,
 *     onClose: () => {...},
 *   });
 *   panel.setBody('<new html>');
 *   panel.close();
 */
const FloatingPanels = (() => {
  const $  = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);

  const panels = new Map();   // id -> { el, state }
  let zCounter = 920;

  const STORAGE_KEY = 'harvester_ops_open_panels';
  const typeRegistry = new Map();   // type → opener(args)

  function registerType(type, opener) {
    typeRegistry.set(type, opener);
  }

  function persistOpen() {
    const list = [];
    panels.forEach((p, id) => {
      if (p.opts.restoreSpec) {
        list.push({
          id,
          ...p.opts.restoreSpec,
          minimized: !!p.minimized,
          dims: {
            width: p.el.style.width,
            height: p.el.style.height,
            top: p.el.style.top,
            left: p.el.style.left,
          },
        });
      }
    });
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(list));
    } catch {}
  }

  function loadPersisted() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
    } catch {
      return [];
    }
  }

  function restoreAll() {
    const list = loadPersisted();
    list.forEach(item => {
      const opener = typeRegistry.get(item.type);
      if (!opener) return;
      try {
        const api = opener(item.args || {});
        if (api && api.el && item.dims) {
          // Restore geometry
          for (const k of ['width', 'height', 'top', 'left']) {
            if (item.dims[k]) api.el.style[k] = item.dims[k];
          }
        }
        if (api && item.minimized) {
          setTimeout(() => minimize(api.id), 50);
        }
      } catch (e) {
        console.warn('restore panel failed', item, e);
      }
    });
  }

  function ensureMinBar() {
    let bar = $('#min-bar');
    if (bar) return bar;
    bar = document.createElement('div');
    bar.id = 'min-bar';
    bar.className = 'min-bar';
    document.body.appendChild(bar);
    positionMinBar();
    window.addEventListener('resize', positionMinBar);
    return bar;
  }

  function positionMinBar() {
    const bar = $('#min-bar');
    if (!bar) return;
    const dock = $('#bottom-dock');
    const dockVisible = dock && dock.style.display !== 'none';
    if (dockVisible) {
      const rect = dock.getBoundingClientRect();
      bar.style.bottom = (window.innerHeight - rect.top) + 'px';
    } else {
      bar.style.bottom = '0';
    }
    bar.style.display = bar.children.length === 0 ? 'none' : 'flex';
  }

  function open(opts) {
    if (!opts || !opts.id) throw new Error('FloatingPanels.open: id required');
    const existing = panels.get(opts.id);
    if (existing) {
      restore(opts.id);
      bringToFront(opts.id);
      return existing.api;
    }

    const w = opts.width  ?? 720;
    const h = opts.height ?? 480;
    const offset = (panels.size * 30) % 200;

    const el = document.createElement('div');
    el.className = 'floating-panel';
    el.id = 'fp-' + opts.id;
    el.style.width  = w + 'px';
    el.style.height = h + 'px';
    el.style.top    = (60 + offset) + 'px';
    el.style.left   = (120 + offset) + 'px';
    el.style.zIndex = ++zCounter;
    el.innerHTML = `
      <div class="floating-panel-resize-handle"></div>
      <header class="floating-panel-header" data-handle>
        <span class="floating-panel-title">${escapeHtml(opts.title || 'Panel')}</span>
        <div class="floating-panel-actions">
          <button class="btn-icon-sm" data-action="min" title="Minimize">_</button>
          <button class="btn-icon-sm" data-action="close" title="Close">×</button>
        </div>
      </header>
      <div class="floating-panel-body"></div>`;
    document.body.appendChild(el);
    const body = el.querySelector('.floating-panel-body');
    if (opts.bodyHtml) body.innerHTML = opts.bodyHtml;
    if (opts.bodyNode) body.appendChild(opts.bodyNode);

    const api = {
      id: opts.id,
      el,
      setBody: (html) => { body.innerHTML = html; },
      setTitle: (t)   => { el.querySelector('.floating-panel-title').textContent = t; },
      close: () => closePanel(opts.id),
      minimize: () => minimize(opts.id),
      restore: () => restore(opts.id),
      body,
    };

    panels.set(opts.id, { el, opts, api, minimized: false });
    persistOpen();

    // Drag
    const header = el.querySelector('.floating-panel-header');
    header.addEventListener('mousedown', (e) => {
      if (e.target.closest('button, input, select, textarea, .floating-panel-actions')) return;
      const rect = el.getBoundingClientRect();
      const off = { x: e.clientX - rect.left, y: e.clientY - rect.top };
      const onMove = (ev) => {
        el.style.left = Math.max(0, Math.min(window.innerWidth - 200, ev.clientX - off.x)) + 'px';
        el.style.top  = Math.max(0, Math.min(window.innerHeight - 50, ev.clientY - off.y)) + 'px';
      };
      const onUp = () => {
        document.removeEventListener('mousemove', onMove);
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp, { once: true });
      e.preventDefault();
    });
    bringToFront(opts.id);
    el.addEventListener('mousedown', () => bringToFront(opts.id));

    // Resize
    const resizer = el.querySelector('.floating-panel-resize-handle');
    resizer.addEventListener('mousedown', (e) => {
      const start = { x: e.clientX, y: e.clientY, w: el.offsetWidth, h: el.offsetHeight };
      const onMove = (ev) => {
        el.style.width  = Math.max(360, start.w + (ev.clientX - start.x)) + 'px';
        el.style.height = Math.max(220, start.h + (ev.clientY - start.y)) + 'px';
      };
      const onUp = () => {
        document.removeEventListener('mousemove', onMove);
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp, { once: true });
      e.preventDefault();
      e.stopPropagation();
    });

    // Buttons
    el.querySelector('[data-action="min"]').addEventListener('click', (e) => {
      e.stopPropagation();
      minimize(opts.id);
    });
    el.querySelector('[data-action="close"]').addEventListener('click', (e) => {
      e.stopPropagation();
      closePanel(opts.id);
    });

    if (opts.onOpen) opts.onOpen(api);
    return api;
  }

  function bringToFront(id) {
    const p = panels.get(id);
    if (!p) return;
    p.el.style.zIndex = ++zCounter;
  }

  function minimize(id) {
    const p = panels.get(id);
    if (!p || p.minimized) return;
    // Save dimensions to restore later
    p.savedDims = {
      width:  p.el.style.width,
      height: p.el.style.height,
      top:    p.el.style.top,
      left:   p.el.style.left,
    };
    p.el.style.display = 'none';
    p.minimized = true;

    const bar = ensureMinBar();
    const chip = document.createElement('div');
    chip.className = 'min-chip';
    chip.dataset.fpId = id;
    const title = p.opts.title || id;
    chip.innerHTML = `
      <span class="title" title="${escapeHtml(title)}">${escapeHtml(title)}</span>
      <button class="btn-icon-sm" data-action="close" title="Close">×</button>`;
    chip.addEventListener('click', (e) => {
      if (e.target.closest('[data-action="close"]')) return;
      restore(id);
    });
    chip.querySelector('[data-action="close"]').addEventListener('click', (e) => {
      e.stopPropagation();
      closePanel(id);
    });
    bar.appendChild(chip);
    positionMinBar();
    persistOpen();
  }

  function restore(id) {
    const p = panels.get(id);
    if (!p) return;
    if (p.minimized) {
      p.el.style.display = 'flex';
      if (p.savedDims) {
        p.el.style.width  = p.savedDims.width;
        p.el.style.height = p.savedDims.height;
        p.el.style.top    = p.savedDims.top;
        p.el.style.left   = p.savedDims.left;
      }
      p.minimized = false;
      const chip = document.querySelector(`#min-bar .min-chip[data-fp-id="${id}"]`);
      if (chip) chip.remove();
      positionMinBar();
      persistOpen();
    }
    bringToFront(id);
  }

  function closePanel(id) {
    const p = panels.get(id);
    if (!p) return;
    if (p.opts.onClose) {
      try { p.opts.onClose(); } catch (e) { console.warn(e); }
    }
    p.el.remove();
    const chip = document.querySelector(`#min-bar .min-chip[data-fp-id="${id}"]`);
    if (chip) chip.remove();
    panels.delete(id);
    positionMinBar();
    persistOpen();
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // Re-position the min-bar whenever the dock toggles/resizes
  document.addEventListener('DOMContentLoaded', () => {
    const obs = new MutationObserver(positionMinBar);
    const dock = $('#bottom-dock');
    if (dock) obs.observe(dock, { attributes: true, attributeFilter: ['style', 'class'] });
    window.addEventListener('resize', positionMinBar);
  });

  return { open, close: closePanel, minimize, restore, bringToFront, registerType, restoreAll };
})();

window.FloatingPanels = FloatingPanels;
