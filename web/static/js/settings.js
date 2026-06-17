/**
 * harvester-ops — settings panel module
 * - Logo upload + logo/title size sliders (live preview)
 * - Interface title customization
 * - Language picker
 * - Connection diagnostic (kubeconfig, RBAC perms, SSH)
 * All settings persist in localStorage.
 */

const LOGO_KEY        = 'harvester_ops_logo';
const TITLE_KEY       = 'harvester_ops_title';
const LOGO_SIZE_KEY   = 'harvester_ops_logo_size';
const TITLE_SIZE_KEY  = 'harvester_ops_title_size';
const TOOLTIPS_KEY    = 'harvester_ops_tooltips_enabled';

const DEFAULTS = { logoSize: 24, titleSize: 16 };

const Settings = (() => {
  const $  = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);

  // Focus-trap state for the modal (a11y — v1.6.2):
  // - _prevFocus  : element to restore focus to on close.
  // - _trapKeydown: bound listener; we keep a ref so we can remove it cleanly.
  let _prevFocus = null;
  let _trapKeydown = null;

  function _focusableIn(root) {
    return Array.from(root.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]),' +
      ' select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )).filter(el => el.offsetParent !== null);
  }

  function _installFocusTrap(modal) {
    _trapKeydown = (e) => {
      if (e.key !== 'Tab') return;
      const focusables = _focusableIn(modal);
      if (focusables.length === 0) { e.preventDefault(); return; }
      const first = focusables[0];
      const last  = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault(); last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault(); first.focus();
      }
    };
    modal.addEventListener('keydown', _trapKeydown);
  }
  function _removeFocusTrap(modal) {
    if (_trapKeydown) {
      modal.removeEventListener('keydown', _trapKeydown);
      _trapKeydown = null;
    }
  }

  function openModal() {
    const m = $('#settings-modal');
    if (!m) return;
    _prevFocus = document.activeElement;
    loadIntoForm();
    renderLanguageGrid();
    populateClusterSelect();
    m.classList.add('active');
    m.setAttribute('aria-hidden', 'false');
    _installFocusTrap(m);
    // Move focus into the modal. Prefer the close button as a safe initial
    // landing — predictable and never destructive on Enter.
    const closeBtn = $('#btn-close-settings');
    if (closeBtn) closeBtn.focus();
  }
  function closeModal() {
    const m = $('#settings-modal');
    if (!m) return;
    m.classList.remove('active');
    m.setAttribute('aria-hidden', 'true');
    _removeFocusTrap(m);
    if (_prevFocus && typeof _prevFocus.focus === 'function') {
      try { _prevFocus.focus(); } catch {}
    }
    _prevFocus = null;
  }

  function setStab(name) {
    $$('.settings-tab').forEach(t => t.classList.toggle('active', t.dataset.stab === name));
    $$('.settings-tab-content').forEach(c => c.classList.toggle('active', c.id === 'stab-' + name));
  }

  function loadIntoForm() {
    // Title
    const title = localStorage.getItem(TITLE_KEY) || 'harvester-ops';
    if ($('#set-title')) $('#set-title').value = title;
    applyTitle(title);

    // Sizes
    const logoSize  = parseInt(localStorage.getItem(LOGO_SIZE_KEY)  || DEFAULTS.logoSize);
    const titleSize = parseInt(localStorage.getItem(TITLE_SIZE_KEY) || DEFAULTS.titleSize);
    if ($('#set-logo-size'))  $('#set-logo-size').value  = logoSize;
    if ($('#set-title-size')) $('#set-title-size').value = titleSize;
    if ($('#logo-size-value'))  $('#logo-size-value').textContent  = logoSize  + 'px';
    if ($('#title-size-value')) $('#title-size-value').textContent = titleSize + 'px';
    applySizes(logoSize, titleSize);

    // Logo
    const logo = localStorage.getItem(LOGO_KEY);
    applyLogo(logo);
    if (logo) {
      $('#logo-preview').src = logo;
      $('#logo-preview').style.display = 'block';
      $('#logo-preview-empty').style.display = 'none';
    } else {
      $('#logo-preview').style.display = 'none';
      $('#logo-preview-empty').style.display = 'block';
    }
  }

  function applyTitle(title) {
    const brandTitle = $('#brand-title');
    if (brandTitle) brandTitle.textContent = title;
    document.title = title;
  }

  function applySizes(logoSize, titleSize) {
    document.documentElement.style.setProperty('--brand-logo-size',  logoSize  + 'px');
    document.documentElement.style.setProperty('--brand-title-size', titleSize + 'px');
  }

  function applyLogo(dataUrl) {
    const img = $('#brand-logo');
    const fallback = $('#brand-fallback');
    if (!img || !fallback) return;
    if (dataUrl) {
      img.src = dataUrl;
      img.style.display = 'inline-block';
      fallback.style.display = 'none';
    } else {
      img.style.display = 'none';
      fallback.style.display = 'inline-block';
    }
  }

  function saveTitle() {
    const v = $('#set-title').value.trim();
    if (!v) {
      localStorage.removeItem(TITLE_KEY);
      applyTitle('harvester-ops');
    } else {
      localStorage.setItem(TITLE_KEY, v);
      applyTitle(v);
    }
  }

  function resetTitle() {
    localStorage.removeItem(TITLE_KEY);
    $('#set-title').value = 'harvester-ops';
    applyTitle('harvester-ops');
  }

  function onLogoSizeInput(e) {
    const v = parseInt(e.target.value);
    $('#logo-size-value').textContent = v + 'px';
    document.documentElement.style.setProperty('--brand-logo-size', v + 'px');
    localStorage.setItem(LOGO_SIZE_KEY, String(v));
  }

  function onTitleSizeInput(e) {
    const v = parseInt(e.target.value);
    $('#title-size-value').textContent = v + 'px';
    document.documentElement.style.setProperty('--brand-title-size', v + 'px');
    localStorage.setItem(TITLE_SIZE_KEY, String(v));
  }

  function applyTooltips(enabled) {
    // Toggle body class — CSS rule .no-tooltips * { pointer-events: ... }
    document.body.classList.toggle('no-tooltips', !enabled);
    // For native title="" attributes: stash + remove (or restore from stash).
    const root = document.body;
    if (!enabled) {
      root.querySelectorAll('[title]').forEach(el => {
        el.dataset.titleStash = el.getAttribute('title');
        el.removeAttribute('title');
      });
    } else {
      root.querySelectorAll('[data-title-stash]').forEach(el => {
        el.setAttribute('title', el.dataset.titleStash);
        delete el.dataset.titleStash;
      });
    }
  }

  function resetSizes() {
    localStorage.removeItem(LOGO_SIZE_KEY);
    localStorage.removeItem(TITLE_SIZE_KEY);
    $('#set-logo-size').value  = DEFAULTS.logoSize;
    $('#set-title-size').value = DEFAULTS.titleSize;
    $('#logo-size-value').textContent  = DEFAULTS.logoSize  + 'px';
    $('#title-size-value').textContent = DEFAULTS.titleSize + 'px';
    applySizes(DEFAULTS.logoSize, DEFAULTS.titleSize);
  }

  function onLogoChange(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    if (file.size > 256 * 1024) {
      alert('Logo > 256KB — please pick a smaller one');
      return;
    }
    const reader = new FileReader();
    reader.onload = (ev) => {
      const dataUrl = ev.target.result;
      localStorage.setItem(LOGO_KEY, dataUrl);
      $('#logo-preview').src = dataUrl;
      $('#logo-preview').style.display = 'block';
      $('#logo-preview-empty').style.display = 'none';
      applyLogo(dataUrl);
    };
    reader.readAsDataURL(file);
  }

  function removeLogo() {
    localStorage.removeItem(LOGO_KEY);
    $('#logo-preview').src = '';
    $('#logo-preview').style.display = 'none';
    $('#logo-preview-empty').style.display = 'block';
    applyLogo(null);
  }

  function renderLanguageGrid() {
    const grid = $('#lang-grid');
    if (!grid) return;
    const flags = { en: '🇬🇧', fr: '🇫🇷', it: '🇮🇹', es: '🇪🇸', de: '🇩🇪' };
    grid.innerHTML = '';
    i18n.getAvailableLanguages().forEach(({ code, label }) => {
      const btn = document.createElement('button');
      btn.dataset.lang = code;
      btn.className = code === i18n.currentLang ? 'active' : '';
      btn.innerHTML = `<span class="lang-flag">${flags[code] || '🌐'}</span><span>${label}</span>`;
      btn.addEventListener('click', () => {
        i18n.setLang(code);
        renderLanguageGrid();
        if (window.App && window.App.refreshStatus)  window.App.refreshStatus();
        if (window.App && window.App.loadVMOrder)    window.App.loadVMOrder();
      });
      grid.appendChild(btn);
    });
  }

  // -------------------------------------------------------------------------
  // Connection diagnostic
  // -------------------------------------------------------------------------
  async function populateClusterSelect() {
    const sel = $('#conn-cluster-select');
    if (!sel) return;
    try {
      const data = await fetch('/api/clusters').then(r => r.json());
      const current = $('#cluster-select').value;
      sel.innerHTML = '';
      (data.clusters || []).forEach(c => {
        const opt = document.createElement('option');
        opt.value = c.name;
        opt.textContent = `${c.name}${c.description ? ' — ' + c.description : ''}`;
        if (c.name === current) opt.selected = true;
        sel.appendChild(opt);
      });
    } catch (e) {
      console.warn('populate clusters failed', e);
    }
  }

  async function testConnection() {
    const out = $('#conn-diag');
    const sel = $('#conn-cluster-select');
    if (!out || !sel) return;
    const cluster = sel.value;
    out.style.display = 'block';
    out.innerHTML = `<div class="summary-bar">${i18n.t('settings.connection.testing')}</div>`;
    try {
      const res = await fetch(`/api/connection-test/${encodeURIComponent(cluster)}`);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      out.innerHTML = renderDiag(data);
    } catch (e) {
      out.innerHTML = `<div class="summary-bar bad">✗ ${i18n.t('common.error')}: ${e.message}</div>`;
    }
  }

  function renderDiag(d) {
    const T = (k) => i18n.t(k);
    const badge = (ok) => ok
      ? `<span class="badge ok">✓ ${T('settings.connection.allowed')}</span>`
      : `<span class="badge fail">✗ ${T('settings.connection.denied')}</span>`;
    const reach = (ok) => ok
      ? `<span class="badge ok">✓ ${T('settings.connection.reachable')}</span>`
      : `<span class="badge fail">✗ ${T('settings.connection.unreachable')}</span>`;

    // Determine overall summary
    const permsOk = d.permissions && Object.values(d.permissions).every(p => p.allowed);
    const sshOk   = d.ssh && d.ssh.length > 0 && d.ssh.every(n => n.reachable);
    const allOk   = d.api_reachable && permsOk && sshOk && d.errors.length === 0;
    const summary = allOk
      ? `<div class="summary-bar ok">✓ ${T('settings.connection.allGreen')}</div>`
      : `<div class="summary-bar bad">⚠ ${T('settings.connection.issues')}</div>`;

    const permsRows = Object.entries(d.permissions || {}).map(([key, p]) => `
      <tr>
        <td><code>${p.label}</code></td>
        <td style="text-align:right;">${badge(p.allowed)}</td>
      </tr>`).join('');

    const sshRows = (d.ssh || []).map(n => `
      <tr>
        <td><strong>${n.hostname}</strong> <small style="color:var(--text-dim)">${n.ip} (${n.role})</small></td>
        <td><code>${n.user}@</code></td>
        <td style="text-align:right;">${reach(n.reachable)}
          ${n.reachable && n.sudo_nopasswd !== undefined
            ? (n.sudo_nopasswd
                ? ` <span class="badge ok">sudo</span>`
                : ` <span class="badge warn">no sudo</span>`)
            : ''}
          ${!n.reachable && n.detail ? `<div style="color:var(--text-dim);font-size:11px;margin-top:4px;">${n.detail}</div>` : ''}
        </td>
      </tr>`).join('');

    return `
      ${summary}
      <h5>${T('settings.connection.kubeconfig')}</h5>
      <dl class="kv">
        <dt>${T('settings.connection.kubeconfig')}</dt>
        <dd>${d.kubeconfig}${d.kubeconfig_exists ? '' : ` <span class="badge fail">missing</span>`}</dd>
        <dt>${T('settings.connection.context')}</dt>
        <dd>${d.current_context || '–'}</dd>
        <dt>${T('settings.connection.user')}</dt>
        <dd>${d.current_user || '–'}</dd>
        <dt>${T('settings.connection.server')}</dt>
        <dd>${d.server || '–'}</dd>
        <dt>${T('settings.connection.apiVersion')}</dt>
        <dd>${d.api_version || '–'} ${d.api_reachable
          ? `<span class="badge ok">✓ ${T('settings.connection.reachable')}</span>`
          : `<span class="badge fail">✗ ${T('settings.connection.unreachable')}</span>`}</dd>
      </dl>

      <h5>${T('settings.connection.permissions')}</h5>
      <table class="perm-table">${permsRows}</table>

      <h5>${T('settings.connection.ssh')}</h5>
      <table class="ssh-table">${sshRows}</table>

      ${d.errors.length > 0 ? `<h5 style="color:var(--danger);">Errors</h5><ul style="margin:0;padding-left:18px;">${d.errors.map(e => `<li>${e}</li>`).join('')}</ul>` : ''}
      ${d.warnings.length > 0 ? `<h5 style="color:var(--warn);">Warnings</h5><ul style="margin:0;padding-left:18px;">${d.warnings.map(e => `<li>${e}</li>`).join('')}</ul>` : ''}
    `;
  }

  async function listAllClusters() {
    const out = $('#conn-clusters');
    if (!out) return;
    out.style.display = 'block';
    out.innerHTML = `<h5>${i18n.t('settings.connection.allClustersTitle')}</h5>`;
    try {
      const data = await fetch('/api/clusters').then(r => r.json());
      (data.clusters || []).forEach(c => {
        const card = document.createElement('div');
        card.className = 'cluster-card';
        card.innerHTML = `
          <div>
            <div class="name">${c.name}</div>
            <div class="meta">${c.description || ''} — ${c.node_count} ${i18n.t('settings.connection.nodes')}</div>
          </div>
          <button class="btn btn-sm btn-secondary" data-cluster="${c.name}">${i18n.t('settings.connection.test')}</button>`;
        card.querySelector('button').addEventListener('click', () => {
          $('#conn-cluster-select').value = c.name;
          testConnection();
        });
        out.appendChild(card);
      });
    } catch (e) {
      out.innerHTML += `<div class="summary-bar bad">${e.message}</div>`;
    }
  }

  function init() {
    if (!$('#settings-modal')) return;

    $('#btn-settings')?.addEventListener('click', openModal);
    $('#btn-close-settings')?.addEventListener('click', closeModal);
    $('#settings-modal')?.addEventListener('click', (e) => {
      if (e.target.id === 'settings-modal') closeModal();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && $('#settings-modal').classList.contains('active')) closeModal();
    });

    $$('.settings-tab').forEach(t => t.addEventListener('click', () => setStab(t.dataset.stab)));

    // Title
    $('#set-title')?.addEventListener('input', saveTitle);
    $('#btn-reset-title')?.addEventListener('click', resetTitle);

    // Logo
    $('#set-logo')?.addEventListener('change', onLogoChange);
    $('#btn-remove-logo')?.addEventListener('click', removeLogo);

    // Size sliders (live)
    $('#set-logo-size')?.addEventListener('input',  onLogoSizeInput);
    $('#set-title-size')?.addEventListener('input', onTitleSizeInput);
    $('#btn-reset-sizes')?.addEventListener('click', resetSizes);

    // Theme + mode selectors (v1.4.34). Boot apply happens via the
    // inline <head> script — here we only wire the in-modal controls
    // so changes take effect instantly without reload.
    if (window.Theme) window.Theme.bindControls();

    // Tooltips toggle
    const tooltipsEnabled = localStorage.getItem(TOOLTIPS_KEY) !== '0';
    if ($('#set-tooltips')) $('#set-tooltips').checked = tooltipsEnabled;
    applyTooltips(tooltipsEnabled);
    $('#set-tooltips')?.addEventListener('change', (e) => {
      localStorage.setItem(TOOLTIPS_KEY, e.target.checked ? '1' : '0');
      applyTooltips(e.target.checked);
    });

    // Connection diagnostic
    $('#btn-test-conn')?.addEventListener('click',  testConnection);
    $('#btn-list-clusters')?.addEventListener('click', listAllClusters);

    // Apply persisted settings on init
    const title    = localStorage.getItem(TITLE_KEY);
    if (title) applyTitle(title);
    const logo     = localStorage.getItem(LOGO_KEY);
    if (logo) applyLogo(logo);
    const logoSize  = parseInt(localStorage.getItem(LOGO_SIZE_KEY)  || DEFAULTS.logoSize);
    const titleSize = parseInt(localStorage.getItem(TITLE_SIZE_KEY) || DEFAULTS.titleSize);
    applySizes(logoSize, titleSize);
  }

  return { init, openModal, closeModal };
})();

document.addEventListener('DOMContentLoaded', () => {
  Settings.init();
  if (typeof i18n !== 'undefined') i18n.applyTranslations();
});
