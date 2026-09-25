/**
 * harvester-ops — les bulles d'aide, dans un calque au-dessus de tout (v1.46.0).
 *
 * Les bulles étaient dessinées en CSS (::after de l'élément survolé),
 * toujours au-dessus de lui : un conteneur qui défile les coupait, et la
 * barre de titre d'une fenêtre flottante passait par-dessus (signalé par
 * l'exploitant sur les onglets de la fenêtre « Migrer »). Une seule bulle,
 * posée dans <body> en position fixe et au-dessus de tous les calques, se
 * place au-dessus de l'élément s'il y a la place, sinon en dessous, et ne
 * sort jamais de l'écran. Les règles CSS restent en repli si ce script ne
 * se charge pas ; `body.no-tooltips` (Réglages, Apparence) coupe tout.
 */
(function () {
  const GAP = 8;
  const MARGIN = 4;
  let bubble = null;
  let current = null;

  function ensure() {
    if (bubble) return bubble;
    bubble = document.createElement('div');
    bubble.className = 'tip-layer';
    bubble.setAttribute('role', 'tooltip');
    bubble.hidden = true;
    document.body.appendChild(bubble);
    return bubble;
  }

  function place(el) {
    const b = ensure();
    const r = el.getBoundingClientRect();
    const t = b.getBoundingClientRect();
    let top = r.top - t.height - GAP;
    let below = false;
    if (top < MARGIN) {                 // pas la place au-dessus : en dessous
      top = r.bottom + GAP;
      below = true;
    }
    top = Math.min(top, window.innerHeight - t.height - MARGIN);
    let left = r.left + r.width / 2 - t.width / 2;
    left = Math.max(MARGIN, Math.min(left, window.innerWidth - t.width - MARGIN));
    b.style.top = `${Math.round(top)}px`;
    b.style.left = `${Math.round(left)}px`;
    b.classList.toggle('below', below);
  }

  function show(el) {
    if (document.body.classList.contains('no-tooltips')) return;
    const text = el.getAttribute('data-tip');
    if (!text) return;
    const b = ensure();
    b.textContent = text;
    b.hidden = false;
    current = el;
    place(el);
  }

  function hide() {
    if (bubble) bubble.hidden = true;
    current = null;
  }

  function target(node) {
    return node && node.closest ? node.closest('.tip[data-tip]') : null;
  }

  function init() {
    document.body.classList.add('js-tips');
    document.addEventListener('mouseover', (e) => {
      const el = target(e.target);
      if (el === current) return;
      if (el) show(el); else hide();
    });
    document.addEventListener('focusin', (e) => {
      const el = target(e.target);
      if (el) show(el);
    });
    document.addEventListener('focusout', hide);
    document.addEventListener('mousedown', hide, true);
    // la bulle est fixe : elle ne suivrait pas un défilement
    document.addEventListener('scroll', hide, true);
    window.addEventListener('blur', hide);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.Tooltips = { show, hide };
})();
