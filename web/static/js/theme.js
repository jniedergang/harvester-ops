/**
 * harvester-ops — theme switcher
 *
 * Applies and persists 1-of-5 themes × 2 modes (dark / light) on the
 * <html> element via data-theme / data-mode attributes. The CSS in
 * style.css defines the matching :root-style palette blocks.
 *
 * The boot-time apply runs from an inline <script> in <head> (see
 * templates/index.html) so the attributes are set BEFORE style.css
 * parses — this avoids the FOUC of the SUSE-Slate-dark default
 * flashing for one frame before the user's saved theme kicks in.
 *
 * Module exports:
 *   Theme.apply(theme, mode)     — set both attrs + persist
 *   Theme.setTheme(theme)        — switch palette only
 *   Theme.setMode(mode)          — switch dark/light only
 *   Theme.current()              — { theme, mode } currently applied
 *   Theme.bindControls(rootEl)   — wire #set-theme / #set-mode selects
 */

const THEME_KEY = 'harvester_ops_theme';
const MODE_KEY  = 'harvester_ops_mode';

const VALID_THEMES = ['suse', 'nord', 'solarized', 'catppuccin', 'tokyo'];
const VALID_MODES  = ['dark', 'light'];

const Theme = (() => {
  function getSaved() {
    let theme = 'tokyo', mode = 'light';
    try {
      const t = localStorage.getItem(THEME_KEY);
      const m = localStorage.getItem(MODE_KEY);
      if (t && VALID_THEMES.includes(t)) theme = t;
      if (m && VALID_MODES.includes(m))  mode  = m;
    } catch {}
    return { theme, mode };
  }

  function apply(theme, mode) {
    if (!VALID_THEMES.includes(theme)) theme = 'tokyo';
    if (!VALID_MODES.includes(mode))   mode  = 'light';
    const html = document.documentElement;
    html.setAttribute('data-theme', theme);
    html.setAttribute('data-mode', mode);
    try {
      localStorage.setItem(THEME_KEY, theme);
      localStorage.setItem(MODE_KEY, mode);
    } catch {}
  }

  function setTheme(theme) {
    const { mode } = current();
    apply(theme, mode);
  }

  function setMode(mode) {
    const { theme } = current();
    apply(theme, mode);
  }

  function current() {
    const html = document.documentElement;
    return {
      theme: html.getAttribute('data-theme') || 'tokyo',
      mode:  html.getAttribute('data-mode')  || 'light',
    };
  }

  function bindControls() {
    const sel = document.getElementById('set-theme');
    const mod = document.getElementById('set-mode');
    const { theme, mode } = current();
    if (sel) {
      sel.value = theme;
      sel.addEventListener('change', (e) => setTheme(e.target.value));
    }
    if (mod) {
      mod.value = mode;
      mod.addEventListener('change', (e) => setMode(e.target.value));
    }
  }

  // Boot apply — the inline <script> calls this immediately to avoid
  // FOUC. Safe to call before DOMContentLoaded.
  function bootApply() {
    const { theme, mode } = getSaved();
    apply(theme, mode);
  }

  return { apply, setTheme, setMode, current, bindControls, bootApply };
})();

// Expose globally so the inline <head> script + settings.js can reach it
window.Theme = Theme;
