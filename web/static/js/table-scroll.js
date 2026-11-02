/* harvester-ops : tableaux qui défilent à l'horizontale (2.0.4).
 *
 * Un tableau à bouton « ⋮ » par ligne est posé dans un `.tbl-scroll` ; sa
 * colonne d'actions reste collée au bord droit (position: sticky, CSS). Ce
 * module ne fait que poser `tbl-more-r` quand du contenu passe sous cette
 * colonne (le tableau déborde et n'est pas défilé jusqu'au bout), pour
 * dessiner une ombre légère à sa gauche.
 *
 * TableScroll.watch(racine) : à appeler après avoir inséré un `.tbl-scroll`
 * (idempotent). Un ResizeObserver suit le conteneur et le tableau : un
 * redessin des lignes ou un changement de largeur de la fenêtre remet
 * l'état à jour sans autre appel. */
(function () {
  'use strict';
  const seen = new WeakSet();
  const ro = typeof ResizeObserver === 'function'
    ? new ResizeObserver((entries) => entries.forEach((e) => update(e.target.closest('.tbl-scroll'))))
    : null;

  function update(w) {
    if (!w) return;
    const more = w.scrollLeft + w.clientWidth < w.scrollWidth - 1;
    if (w.classList.contains('tbl-more-r') !== more) w.classList.toggle('tbl-more-r', more);
  }

  function watch(root) {
    const scope = root || document;
    const list = scope.matches && scope.matches('.tbl-scroll') ? [scope] : scope.querySelectorAll('.tbl-scroll');
    list.forEach((w) => {
      if (!seen.has(w)) {
        seen.add(w);
        w.addEventListener('scroll', () => update(w), { passive: true });
        if (ro) ro.observe(w);
      }
      const t = w.firstElementChild;
      if (t && ro && !seen.has(t)) { seen.add(t); ro.observe(t); }
      update(w);
    });
  }

  window.addEventListener('resize', () => document.querySelectorAll('.tbl-scroll').forEach(update));
  window.TableScroll = { watch, update };
})();
