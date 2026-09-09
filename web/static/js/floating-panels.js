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
      <div class="floating-panel-resize-handle" data-rs="se"></div>
      <div class="fp-rs fp-rs-n"  data-rs="n"></div>
      <div class="fp-rs fp-rs-s"  data-rs="s"></div>
      <div class="fp-rs fp-rs-e"  data-rs="e"></div>
      <div class="fp-rs fp-rs-w"  data-rs="w"></div>
      <div class="fp-rs fp-rs-ne" data-rs="ne"></div>
      <div class="fp-rs fp-rs-nw" data-rs="nw"></div>
      <div class="fp-rs fp-rs-sw" data-rs="sw"></div>
      <header class="floating-panel-header" data-handle>
        <span class="floating-panel-title">${escapeHtml(opts.title || 'Panel')}</span>
        <div class="floating-panel-actions">
          ${(opts.headerActions || []).map((a, i) =>
            `<button class="btn-icon-sm tip" data-header-action="${i}"
                     data-tip="${escapeHtml(a.tip || '')}">${escapeHtml(a.label || '?')}</button>`).join('')}
          <button class="btn-icon-sm tip" data-action="min" data-tip="${window.i18n ? i18n.t('panel.minimizeTip') : 'Minimize'}">_</button>
          <button class="btn-icon-sm tip" data-action="close" data-tip="${window.i18n ? i18n.t('panel.closeTip') : 'Close'}">×</button>
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

    // Drag & resize — Pointer Events with setPointerCapture. The old
    // mousemove-on-document + mouseup{once} pattern lost the mouseup when
    // it landed outside the window or inside a capturing child (the noVNC
    // canvas grabs pointer events), leaving the panel glued to the cursor.
    // Pointer capture delivers move/up to the handle no matter where the
    // pointer goes, and pointercancel is handled for free.
    function trackPointer(handleEl, onMove) {
      handleEl.addEventListener('pointerdown', (e) => {
        if (e.button !== 0) return;
        if (e.target.closest('button, input, select, textarea, .floating-panel-actions')) return;
        const rect = el.getBoundingClientRect();
        const start = { x: e.clientX, y: e.clientY,
                        w: rect.width, h: rect.height,
                        left: rect.left, top: rect.top };
        const move = (ev) => onMove(start, ev);
        const stop = () => {
          handleEl.removeEventListener('pointermove', move);
          handleEl.removeEventListener('pointerup', stop);
          handleEl.removeEventListener('pointercancel', stop);
          try { handleEl.releasePointerCapture(e.pointerId); } catch { /* gone */ }
        };
        handleEl.setPointerCapture(e.pointerId);
        handleEl.addEventListener('pointermove', move);
        handleEl.addEventListener('pointerup', stop);
        handleEl.addEventListener('pointercancel', stop);
        e.preventDefault();
        e.stopPropagation();
        bringToFront(opts.id);
      });
    }

    const header = el.querySelector('.floating-panel-header');
    trackPointer(header, (start, ev) => {
      el.style.left = Math.max(0, Math.min(window.innerWidth - 200, start.left + ev.clientX - start.x)) + 'px';
      el.style.top  = Math.max(0, Math.min(window.innerHeight - 50, start.top + ev.clientY - start.y)) + 'px';
    });
    bringToFront(opts.id);
    el.addEventListener('mousedown', () => bringToFront(opts.id));

    // Resize from every edge and corner (data-rs carries the directions).
    const MIN_W = 360, MIN_H = 220;
    el.querySelectorAll('[data-rs]').forEach((handleEl) => {
      const dirs = handleEl.dataset.rs;
      trackPointer(handleEl, (start, ev) => {
        const dx = ev.clientX - start.x, dy = ev.clientY - start.y;
        if (dirs.includes('e')) el.style.width = Math.max(MIN_W, start.w + dx) + 'px';
        if (dirs.includes('s')) el.style.height = Math.max(MIN_H, start.h + dy) + 'px';
        if (dirs.includes('w')) {
          const w2 = Math.max(MIN_W, start.w - dx);
          el.style.width = w2 + 'px';
          el.style.left = (start.left + start.w - w2) + 'px';
        }
        if (dirs.includes('n')) {
          const h2 = Math.max(MIN_H, start.h - dy);
          el.style.height = h2 + 'px';
          el.style.top = (start.top + start.h - h2) + 'px';
        }
      });
    });

    // Buttons
    // v1.13.0 : raccourcis propres au panneau, posés dans le bandeau
    // (ex. ouvrir la console depuis l'éditeur de VM).
    (opts.headerActions || []).forEach((a, i) => {
      const btn = el.querySelector(`[data-header-action="${i}"]`);
      if (btn && typeof a.onClick === 'function') {
        btn.addEventListener('click', (e) => { e.stopPropagation(); a.onClick(); });
      }
    });
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
      <button class="btn-icon-sm tip" data-action="close" data-tip="${window.i18n ? i18n.t('panel.closeTip') : 'Close'}">×</button>`;
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
