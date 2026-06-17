/**
 * harvester-ops — floating Docs panel
 * Draggable header + resizable corner. Loads markdown from /api/docs.
 */
const Docs = (() => {
  const $  = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);

  let currentLang = 'en';
  let currentDoc  = null;
  let dragOffset  = null;
  let resizeStart = null;

  function open() {
    const p = $('#docs-panel');
    if (!p) return;
    p.style.display = 'flex';
    p.classList.remove('minimized');
    loadIndex();
  }

  function close() {
    $('#docs-panel').style.display = 'none';
  }

  function toggleMin() {
    $('#docs-panel').classList.toggle('minimized');
  }

  async function loadIndex() {
    try {
      const data = await fetch('/api/docs').then(r => r.json());
      const list = (data.docs || {})[currentLang] || (data.docs || {}).en || [];
      const ul = $('#docs-toc-list');
      ul.innerHTML = '';
      list.forEach(item => {
        const li = document.createElement('li');
        li.textContent = item.title;
        li.dataset.path = item.path;
        if (item.path === currentDoc) li.classList.add('active');
        li.addEventListener('click', () => loadDoc(item.path));
        ul.appendChild(li);
      });
      // Auto-load first doc
      if (list.length > 0 && !currentDoc) loadDoc(list[0].path);
    } catch (e) {
      $('#docs-content').innerHTML = `<p style="color:var(--danger)">${e.message}</p>`;
    }
  }

  async function loadDoc(path) {
    currentDoc = path;
    $$('#docs-toc-list li').forEach(li => li.classList.toggle('active', li.dataset.path === path));
    $('#docs-content').innerHTML = `<p class="hint">${typeof t === 'function' ? t('common.loading') : 'Loading...'}</p>`;
    try {
      const data = await fetch(`/api/docs/${currentLang}/${encodeURIComponent(path)}`).then(r => r.json());
      $('#docs-content').innerHTML = data.html;
    } catch (e) {
      $('#docs-content').innerHTML = `<p style="color:var(--danger)">${e.message}</p>`;
    }
  }

  // -------------------------------------------------------------------------
  // Drag (whole panel by header)
  // -------------------------------------------------------------------------
  function onDragStart(e) {
    // Don't capture clicks on form controls / buttons inside the header
    if (e.target.closest('select, button, input, .docs-header-actions')) return;
    const panel = $('#docs-panel');
    const rect = panel.getBoundingClientRect();
    dragOffset = { x: e.clientX - rect.left, y: e.clientY - rect.top };
    panel.style.right = 'auto';
    panel.style.bottom = 'auto';
    document.addEventListener('mousemove', onDragMove);
    document.addEventListener('mouseup', onDragEnd, { once: true });
    e.preventDefault();
  }
  function onDragMove(e) {
    if (!dragOffset) return;
    const panel = $('#docs-panel');
    const x = Math.max(0, Math.min(window.innerWidth - 200, e.clientX - dragOffset.x));
    const y = Math.max(0, Math.min(window.innerHeight - 50, e.clientY - dragOffset.y));
    panel.style.left = x + 'px';
    panel.style.top  = y + 'px';
  }
  function onDragEnd() {
    dragOffset = null;
    document.removeEventListener('mousemove', onDragMove);
  }

  // -------------------------------------------------------------------------
  // Resize (bottom-right corner)
  // -------------------------------------------------------------------------
  function onResizeStart(e) {
    const panel = $('#docs-panel');
    const rect = panel.getBoundingClientRect();
    resizeStart = { x: e.clientX, y: e.clientY, w: rect.width, h: rect.height };
    document.addEventListener('mousemove', onResizeMove);
    document.addEventListener('mouseup', onResizeEnd, { once: true });
    e.preventDefault();
    e.stopPropagation();
  }
  function onResizeMove(e) {
    if (!resizeStart) return;
    const panel = $('#docs-panel');
    const w = Math.max(380, resizeStart.w + (e.clientX - resizeStart.x));
    const h = Math.max(280, resizeStart.h + (e.clientY - resizeStart.y));
    panel.style.width  = w + 'px';
    panel.style.height = h + 'px';
  }
  function onResizeEnd() {
    resizeStart = null;
    document.removeEventListener('mousemove', onResizeMove);
  }

  function init() {
    if (!$('#docs-panel')) return;
    $('#btn-docs')?.addEventListener('click', open);
    $('#btn-docs-close')?.addEventListener('click', close);
    $('#btn-docs-min')?.addEventListener('click', toggleMin);
    $('#docs-drag-handle')?.addEventListener('mousedown', onDragStart);
    $('#docs-resizer')?.addEventListener('mousedown', onResizeStart);
    $('#docs-lang')?.addEventListener('change', (e) => {
      currentLang = e.target.value;
      currentDoc = null;
      loadIndex();
    });
    // Sync default language with current i18n language
    if (typeof i18n !== 'undefined') {
      const supported = ['en', 'fr'];
      currentLang = supported.includes(i18n.currentLang) ? i18n.currentLang : 'en';
      if ($('#docs-lang')) $('#docs-lang').value = currentLang;
    }
  }

  return { init, open, close };
})();

document.addEventListener('DOMContentLoaded', Docs.init);
