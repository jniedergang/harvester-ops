/**
 * harvester-ops : outils communs aux vues « à la manière d'ESXi »
 * (Fabrique, Réseau, Stockage).
 *
 * Les trois vues se lisent de la même façon, un bloc par objet et de gauche
 * à droite, et partagent ces quelques gestes. Les recopier dans chacune les
 * aurait fait diverger au premier correctif.
 */
const Board = (() => {
  /** Traduction avec repli lisible si la clé manque. */
  const tr = (k, f) => {
    const v = window.i18n ? i18n.t(k) : null;
    return v && v !== k ? v : f;
  };

  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  /** Une valeur copiable : le bouton est un calque, son porteur un repère. */
  const val = (v, cls = '') => {
    const b = window.CopyTo ? CopyTo.button(v) : '';
    return `<span class="vsw-val ${cls}">${esc(v)}${b}</span>`;
  };

  /**
   * Les bulles d'aide lisent `data-tip`, que i18n ne résout qu'à son
   * passage sur tout le document. Une vue réécrite toutes les 8 s doit le
   * faire elle-même, sur sa seule surface.
   */
  function applyTips(root) {
    if (!root) return;
    root.querySelectorAll('[data-tip-i18n]').forEach(el => {
      const t = window.i18n ? i18n.t(el.getAttribute('data-tip-i18n')) : null;
      if (t) el.setAttribute('data-tip', t);
    });
  }

  /** Octets en unités binaires, comme Longhorn et Kubernetes les comptent. */
  function bytes(n) {
    if (n == null || n === '' || !Number.isFinite(Number(n))) return '-';
    let v = Number(n);
    const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
    let i = 0;
    while (Math.abs(v) >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i === 0 || v >= 10 ? Math.round(v) : v.toFixed(1)) + ' ' + u[i];
  }

  /** Ligne de détail `dt`/`dd`, avec copie quand la valeur le mérite. */
  function kv(label, value) {
    const t = value == null || value === '' ? '-' : String(value);
    return `<dt>${esc(label)}</dt><dd>${esc(t)}${window.CopyTo ? CopyTo.button(t) : ''}</dd>`;
  }

  return { tr, esc, val, applyTips, bytes, kv };
})();

if (typeof window !== 'undefined') window.Board = Board;
