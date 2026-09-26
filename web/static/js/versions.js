/**
 * harvester-ops — historique des versions (v1.56.0)
 *
 * Un clic sur le numéro de version (menu latéral, menu du compte, À propos)
 * ouvre la liste de ce que chaque version a apporté, lue dans CHANGELOG.md
 * par /api/changelog. Les notes restent en anglais, comme le fichier ; le
 * reste de la fenêtre suit la langue de l'interface.
 */
const Versions = (() => {
  const $ = (s) => document.querySelector(s);
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const escapeHtml = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  let data = null;
  let kind = 'all';
  let prevFocus = null;

  // Markdown léger du CHANGELOG, APRÈS échappement : gras, code, liens
  // (réduits à leur texte : la console peut être hors ligne).
  function md(text) {
    return escapeHtml(text)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1');
  }

  function sectionWanted(name) {
    const n = (name || '').toLowerCase();
    if (kind === 'added') return n.startsWith('added') || n.startsWith('new');
    if (kind === 'fixed') return n.startsWith('fixed') || n.startsWith('security');
    return true;
  }

  function render() {
    const body = $('#versions-body');
    if (!body) return;
    if (!data) { body.innerHTML = `<p class="form-hint">${escapeHtml(tr('common.loading'))}</p>`; return; }
    if (!data.releases.length) {
      body.innerHTML = `<p class="form-hint">${escapeHtml(tr('versions.none'))}</p>`;
      return;
    }
    const words = ($('#versions-filter')?.value || '').toLowerCase().split(/\s+/).filter(Boolean);
    const out = [];
    let first = true;
    for (const r of data.releases) {
      const secs = r.sections.filter(s => sectionWanted(s.name) && s.items.length);
      if (!secs.length) continue;
      const hay = (r.version + ' ' + r.title + ' ' + secs.map(s => s.items.join(' ')).join(' ')).toLowerCase();
      if (words.some(w => !hay.includes(w))) continue;
      const current = r.version === data.current;
      out.push(`
        <details class="version-rel${current ? ' is-current' : ''}" data-version="${escapeHtml(r.version)}"${first || words.length ? ' open' : ''}>
          <summary>
            <span class="version-num">v${escapeHtml(r.version)}</span>
            <span class="version-title">${md(r.title)}</span>
            <span class="version-date">${escapeHtml(r.date)}</span>
            ${current ? `<span class="badge info tip" data-tip="${escapeHtml(tr('versions.currentTip'))}">${escapeHtml(tr('versions.current'))}</span>` : ''}
          </summary>
          ${secs.map(s => `
            <div class="version-sec">
              ${s.name ? `<h5>${escapeHtml(s.name)}</h5>` : ''}
              <ul>${s.items.map(i => `<li>${md(i)}</li>`).join('')}</ul>
            </div>`).join('')}
        </details>`);
      first = false;
    }
    body.innerHTML = out.length ? out.join('')
      : `<p class="form-hint">${escapeHtml(tr('versions.noMatch'))}</p>`;
  }

  async function load() {
    try {
      const r = await fetch('/api/changelog');
      data = await r.json();
      data.releases = data.releases || [];
    } catch {
      data = { current: '', releases: [] };
    }
    render();
  }

  function open() {
    const m = $('#versions-modal');
    if (!m) return;
    prevFocus = document.activeElement;
    m.classList.add('active');
    m.setAttribute('aria-hidden', 'false');
    $('#btn-close-versions')?.focus();
    if (!data) { render(); load(); } else render();
  }

  function close() {
    const m = $('#versions-modal');
    if (!m) return;
    m.classList.remove('active');
    m.setAttribute('aria-hidden', 'true');
    if (prevFocus && typeof prevFocus.focus === 'function') { try { prevFocus.focus(); } catch {} }
    prevFocus = null;
  }

  function init() {
    $('#btn-version')?.addEventListener('click', open);
    $('#btn-about-versions')?.addEventListener('click', () => {
      if (typeof Settings !== 'undefined') Settings.closeModal();
      open();
    });
    $('#btn-close-versions')?.addEventListener('click', close);
    $('#versions-modal')?.addEventListener('click', (e) => { if (e.target.id === 'versions-modal') close(); });
    $('#versions-modal')?.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
    $('#versions-filter')?.addEventListener('input', render);
    document.querySelectorAll('#versions-modal [data-vkind]').forEach(b => b.addEventListener('click', () => {
      kind = b.dataset.vkind;
      document.querySelectorAll('#versions-modal [data-vkind]').forEach(x => x.classList.toggle('active', x === b));
      render();
    }));
  }

  return { init, open, close };
})();
window.Versions = Versions;
