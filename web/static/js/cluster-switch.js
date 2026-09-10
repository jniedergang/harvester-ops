/**
 * harvester-ops — voile de changement de cluster (v1.20.0)
 *
 * Changer de cluster ne coûte pas le même temps partout : quelques
 * centaines de millisecondes sur un cluster local, plusieurs secondes sur
 * un cluster distant dont l'API répond mal. Entre les deux, l'écran
 * gardait les chiffres du cluster PRÉCÉDENT sans rien signaler — le pire
 * des cas pour un outil d'exploitation, où lire le mauvais cluster mène à
 * agir sur le mauvais cluster.
 *
 * D'où ce voile : le fond est flouté, un message animé au centre nomme le
 * cluster en cours de chargement, et la page redevient utilisable quand
 * les données sont vraiment celles du nouveau cluster.
 *
 * Deux garde-fous :
 *   * une durée minimale d'affichage, sinon un cluster rapide produit un
 *     flash désagréable au lieu d'une transition ;
 *   * un délai de sécurité qui lève le voile quoi qu'il arrive : un
 *     rafraîchissement qui ne rend jamais la main ne doit pas laisser
 *     l'interface verrouillée.
 */
const ClusterSwitch = (() => {
  const MIN_VISIBLE_MS = 400;
  const SAFETY_MS = 20000;

  let el = null;
  let shownAt = 0;
  let safetyTimer = null;

  const tr = (k, params) => (window.i18n ? i18n.t(k, params) : k);
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  function ensure() {
    if (el) return el;
    el = document.createElement('div');
    el.id = 'cluster-switch-overlay';
    el.className = 'cluster-switch-overlay';
    // aria-live pour que le changement soit annoncé aux lecteurs d'écran :
    // eux non plus ne doivent pas croire lire encore l'ancien cluster.
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    el.hidden = true;
    el.innerHTML = `
      <div class="cluster-switch-card">
        <div class="cluster-switch-orbit" aria-hidden="true">
          <span></span><span></span><span></span>
        </div>
        <p class="cluster-switch-msg"></p>
        <p class="cluster-switch-hint"></p>
      </div>`;
    document.body.appendChild(el);
    return el;
  }

  function show(cluster) {
    const node = ensure();
    node.querySelector('.cluster-switch-msg').innerHTML =
      tr('cluster.switching', { name: `<strong>${esc(cluster)}</strong>` });
    node.querySelector('.cluster-switch-hint').textContent = tr('cluster.switchingHint');
    node.hidden = false;
    // reflow avant d'ajouter la classe : sans lui la transition d'opacité
    // ne joue pas, l'élément passant de hidden à visible dans la même frame.
    void node.offsetWidth;
    node.classList.add('visible');
    document.body.classList.add('cluster-switching');
    shownAt = Date.now();
    clearTimeout(safetyTimer);
    safetyTimer = setTimeout(() => hide(true), SAFETY_MS);
    return node;
  }

  function hide(immediate) {
    if (!el || el.hidden) return Promise.resolve();
    clearTimeout(safetyTimer);
    const wait = immediate ? 0 : Math.max(0, MIN_VISIBLE_MS - (Date.now() - shownAt));
    return new Promise((resolve) => setTimeout(() => {
      el.classList.remove('visible');
      document.body.classList.remove('cluster-switching');
      // laisser la transition de sortie se jouer avant de retirer du flux
      setTimeout(() => { if (el && !el.classList.contains('visible')) el.hidden = true; }, 200);
      resolve();
    }, wait));
  }

  /** Exécute `work` derrière le voile, quoi qu'il advienne de `work`. */
  async function during(cluster, work) {
    show(cluster);
    try {
      return await work();
    } finally {
      await hide();
    }
  }

  function isVisible() { return !!el && !el.hidden; }

  return { show, hide, during, isVisible };
})();

window.ClusterSwitch = ClusterSwitch;
