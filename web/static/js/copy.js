/**
 * harvester-ops : copier une valeur d'un coup d'oeil.
 *
 * Une IP, une MAC, un identifiant ou un nom de ressource servent à être
 * COLLÉS ailleurs : un ping, un ticket, la table d'un switch, une commande
 * kubectl. Les retaper depuis un écran est le moyen le plus sûr de se
 * tromper d'un caractère, et personne ne s'en aperçoit avant d'avoir
 * diagnostiqué la mauvaise machine.
 *
 * Deux règles de forme, apprises en le posant une première fois :
 *
 *   * le bouton est un CALQUE (`position: absolute`). Posé dans le flux, il
 *     élargissait chaque ligne et décalait toutes les valeurs, ce qui est
 *     exactement ce qu'on ne veut pas d'une commodité ;
 *   * il ne se montre qu'au survol de sa ligne, pour ne pas transformer un
 *     tableau de faits en grille de boutons.
 *
 * Le presse-papier du navigateur exige un contexte sûr (HTTPS ou
 * localhost). Une console servie en HTTP simple n'y a pas droit : on
 * retombe alors sur une sélection, plutôt que d'échouer sans rien dire.
 */
const CopyTo = (() => {
  // Ce qui mérite un bouton : adresses IPv4 et IPv6, MAC, identifiants
  // Kubernetes, chemins `namespace/nom`. Un mot isolé comme « up » ou
  // « bridge » n'a aucun intérêt à être copié.
  const WORTH_COPYING = [
    /^\d{1,3}(\.\d{1,3}){3}(\/\d{1,2})?$/,          // IPv4, avec ou sans masque
    /^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$/,        // MAC
    /^[0-9a-fA-F]{0,4}(:[0-9a-fA-F]{0,4}){2,7}$/,    // IPv6
    /^[a-z0-9][a-z0-9.-]{5,}$/,                      // nom de ressource, hôte
    /^[^/\s]+\/[^/\s]+$/,                            // namespace/nom
  ];

  function worth(value) {
    const v = String(value == null ? '' : value).trim();
    if (v.length < 3 || v === '-' || v.includes(' ')) return false;
    return WORTH_COPYING.some(re => re.test(v));
  }

  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  /** Bouton de copie, ou chaîne vide si la valeur ne le mérite pas. */
  function button(value, opts = {}) {
    if (!opts.force && !worth(value)) return '';
    const label = (window.i18n ? i18n.t('copy.title') : 'Copy this value');
    const icon = window.Icons ? Icons.svg('copy', { size: 11 }) : '⧉';
    return `<button type="button" class="copy-btn tip" data-copy="${esc(value)}"`
      + ` data-tip-i18n="copy.title" aria-label="${esc(label)}">${icon}</button>`;
  }

  async function write(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Pas de presse-papier (contexte non sûr) : repli par sélection.
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch { ok = false; }
      ta.remove();
      return ok;
    }
  }

  /**
   * Écoute déléguée sur un conteneur. Déléguée parce que les panneaux sont
   * réécrits à chaque rafraîchissement : un écouteur par bouton se perdrait
   * au rendu suivant, et la copie cesserait de marcher sans rien dire.
   */
  function wire(root) {
    if (!root || root.dataset.copyWired === '1') return;
    root.dataset.copyWired = '1';
    root.addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-copy]');
      if (!btn || !root.contains(btn)) return;
      e.preventDefault();
      e.stopPropagation();
      const ok = await write(btn.dataset.copy);
      btn.classList.add(ok ? 'copied' : 'copy-failed');
      setTimeout(() => btn.classList.remove('copied', 'copy-failed'), 1200);
    });
  }

  return { button, wire, worth, write };
})();

if (typeof window !== 'undefined') window.CopyTo = CopyTo;
