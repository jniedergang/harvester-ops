/**
 * harvester-ops — voile de chargement (v1.21.0)
 *
 * Deux usages, une seule implémentation :
 *
 *   * PLEIN ÉCRAN, au changement de cluster. L'écran gardait les chiffres
 *     du cluster PRÉCÉDENT sans rien signaler — le pire des cas pour un
 *     outil d'exploitation, où lire le mauvais cluster mène à agir sur le
 *     mauvais cluster.
 *   * SUR UNE ZONE, quand une vue met du temps à se charger (la topologie
 *     d'un gros cluster, l'aperçu). Le reste de la page demeure lisible et
 *     utilisable : seule la zone concernée est floutée.
 *
 * Trois garde-fous, appris en le cassant :
 *
 *   * `delay` — n'afficher le voile que si le travail dure vraiment. Sans
 *     lui, une réponse de 80 ms produit un clignotement plus gênant que
 *     l'attente qu'il annonce ;
 *   * `MIN_VISIBLE_MS` — une fois affiché, le laisser le temps d'être lu ;
 *   * `SAFETY_MS` — le lever quoi qu'il arrive. Le voile bloque les clics :
 *     un chargement qui ne rend jamais la main ne doit pas verrouiller
 *     l'interface.
 */
const Veil = (() => {
  const MIN_VISIBLE_MS = 400;
  const SAFETY_MS = 20000;

  // Un voile par cible : la clé est l'élément hôte, `null` pour le plein
  // écran. Deux chargements sur la même zone partagent donc le même voile
  // au lieu d'en empiler deux.
  const active = new Map();

  const tr = (k, params) => (window.i18n ? i18n.t(k, params) : k);
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  function build(scoped) {
    const el = document.createElement('div');
    el.className = 'veil' + (scoped ? ' veil-scoped' : ' veil-fullscreen');
    // aria-live : un lecteur d'écran ne doit pas croire lire encore
    // l'ancien contenu.
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    el.innerHTML = `
      <div class="veil-card">
        <div class="veil-orbit" aria-hidden="true"><span></span><span></span><span></span></div>
        <p class="veil-msg"></p>
        <p class="veil-hint"></p>
      </div>`;
    return el;
  }

  function paint(el, opts) {
    const msg = opts.message || '';
    // `name` est mis en valeur : c'est l'information qu'on vient chercher
    // du regard (quel cluster, quelle vue).
    const html = opts.name
      ? esc(msg).replace('{name}', `<strong>${esc(opts.name)}</strong>`)
      : esc(msg);
    el.querySelector('.veil-msg').innerHTML = html;
    const hint = el.querySelector('.veil-hint');
    hint.textContent = opts.hint || '';
    hint.hidden = !opts.hint;
  }

  function show(target, opts = {}) {
    const key = target || null;
    let entry = active.get(key);
    if (!entry) {
      const el = build(!!target);
      const host = target || document.body;
      if (target) {
        // Le voile se positionne dans son hôte : sans contexte de
        // positionnement, il se calerait sur la fenêtre entière.
        const pos = getComputedStyle(host).position;
        if (pos === 'static') host.style.position = 'relative';
      }
      host.appendChild(el);
      entry = { el, host, shownAt: 0, safety: null, count: 0 };
      active.set(key, entry);
    }
    paint(entry.el, opts);
    entry.count += 1;
    if (!entry.shownAt) {
      void entry.el.offsetWidth;            // forcer un reflow pour la transition
      entry.el.classList.add('visible');
      entry.shownAt = Date.now();
      if (!target) document.body.classList.add('veil-blocking');
      clearTimeout(entry.safety);
      entry.safety = setTimeout(() => hide(target, true), SAFETY_MS);
    }
    return entry;
  }

  function hide(target, immediate) {
    const key = target || null;
    const entry = active.get(key);
    if (!entry) return Promise.resolve();
    entry.count = Math.max(0, entry.count - 1);
    if (entry.count > 0) return Promise.resolve();   // un autre travail continue
    clearTimeout(entry.safety);
    const wait = immediate ? 0
      : Math.max(0, MIN_VISIBLE_MS - (Date.now() - entry.shownAt));
    return new Promise((resolve) => setTimeout(() => {
      entry.el.classList.remove('visible');
      if (!target) document.body.classList.remove('veil-blocking');
      // laisser la transition de sortie se jouer avant de retirer du DOM
      setTimeout(() => {
        if (active.get(key) === entry && entry.count === 0) {
          entry.el.remove();
          active.delete(key);
        }
      }, 200);
      resolve();
    }, wait));
  }

  /**
   * Exécute `work` derrière le voile, quoi qu'il advienne de `work`.
   * `opts.delay` retarde l'apparition : au-dessous, rien ne s'affiche.
   */
  async function during(target, opts, work) {
    const delay = opts.delay || 0;
    let armed = false;
    const timer = delay
      ? setTimeout(() => { armed = true; show(target, opts); }, delay)
      : (show(target, opts), armed = true, null);
    try {
      return await work();
    } finally {
      clearTimeout(timer);
      if (armed) await hide(target);
    }
  }

  function isVisible(target) {
    const entry = active.get(target || null);
    return !!entry && entry.el.classList.contains('visible');
  }

  return { show, hide, during, isVisible, tr };
})();

window.Veil = Veil;
