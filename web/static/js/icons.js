/**
 * harvester-ops — jeu d'icônes SVG inline (v1.14.0)
 *
 * Les boutons d'action utilisaient des emoji : rendu incohérent (certains
 * en couleur pleine façon photo, d'autres en glyphe fin), taille variable
 * d'une plateforme à l'autre, et illisibles à 16 px. Ce jeu est dessiné
 * d'un seul trait : viewBox 24, stroke currentColor, épaisseur constante.
 *
 * `currentColor` fait que chaque icône hérite de la couleur du bouton —
 * elle suit donc les 5 thèmes et les modes clair/sombre sans variante.
 *
 * Usage :  Icons.svg('console')            -> chaîne HTML
 *          Icons.svg('play', {size: 18})   -> taille personnalisée
 */
const Icons = (() => {
  // Tracés seuls : l'enveloppe <svg> est ajoutée par svg().
  const PATHS = {
    // Alimentation
    play:    '<path d="M8 5.2l11 6.8-11 6.8z" fill="currentColor" stroke="none"/>',
    stop:    '<rect x="6.5" y="6.5" width="11" height="11" rx="1.6" fill="currentColor" stroke="none"/>',
    restart: '<path d="M20 12a8 8 0 11-2.6-5.9"/><path d="M20 4v4.5h-4.5"/>',
    power:   '<path d="M12 4v8"/><path d="M7.1 7.1a7 7 0 109.8 0"/>',

    // Objets
    snapshot: '<path d="M3.2 8.6A1.6 1.6 0 014.8 7h2.3l1.2-1.9h7.4L16.9 7h2.3A1.6 1.6 0 0120.8 8.6v8.8a1.6 1.6 0 01-1.6 1.6H4.8a1.6 1.6 0 01-1.6-1.6z"/>'
              + '<circle cx="12" cy="13" r="3.1"/>',
    migrate: '<path d="M4 9.2h12.5"/><path d="M13.4 6.1l3.1 3.1-3.1 3.1"/>'
             + '<path d="M20 15h-12.5"/><path d="M10.6 11.9L7.5 15l3.1 3.1"/>',
    settings: '<path d="M4.5 8h8"/><path d="M17.5 8H19.5"/><path d="M4.5 16h2.5"/><path d="M12 16h7.5"/>'
              + '<circle cx="15" cy="8" r="2.2"/><circle cx="9.5" cy="16" r="2.2"/>',
    console: '<rect x="3" y="5" width="18" height="11.5" rx="1.6"/>'
             + '<path d="M9 20h6"/><path d="M12 16.5V20"/>',
    notes:   '<path d="M6.2 3.6h7.6L18.8 8.4v11.2a1 1 0 01-1 1H6.2a1 1 0 01-1-1V4.6a1 1 0 011-1z"/>'
             + '<path d="M13.6 3.6V8.6h5"/><path d="M8.4 13h7"/><path d="M8.4 16.4h4.4"/>',
    trash:   '<path d="M4.8 6.8h14.4"/><path d="M9.5 6.8V4.9h5V6.8"/>'
             + '<path d="M7 6.8l.9 12.1a1 1 0 001 .9h6.2a1 1 0 001-.9l.9-12.1"/>',
    restore: '<path d="M4 12a8 8 0 102.6-5.9"/><path d="M4 4v4.5h4.5"/>',
    refresh: '<path d="M20 12a8 8 0 11-2.6-5.9"/><path d="M20 4v4.5h-4.5"/>',

    // Bare-metal (v1.19.0)
    download: '<path d="M12 4v10.5"/><path d="M8.2 10.8L12 14.6l3.8-3.8"/>'
              + '<path d="M4.8 18.6h14.4"/>',
    search:  '<circle cx="10.8" cy="10.8" r="6.1"/><path d="M15.3 15.3L20 20"/>',
    install: '<path d="M12 3.4l7.6 4v8.6L12 20.6 4.4 16V7.4z"/>'
             + '<path d="M4.4 7.4L12 11.7l7.6-4.3"/><path d="M12 11.7v8.9"/>',
  };

  function svg(name, opts) {
    const d = PATHS[name];
    if (!d) return '';
    const size = (opts && opts.size) || 16;
    return `<svg class="icon icon-${name}" viewBox="0 0 24 24" width="${size}" height="${size}"`
      + ' fill="none" stroke="currentColor" stroke-width="1.7"'
      + ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"'
      + ` focusable="false">${d}</svg>`;
  }

  function has(name) { return Object.prototype.hasOwnProperty.call(PATHS, name); }

  return { svg, has, names: () => Object.keys(PATHS) };
})();

window.Icons = Icons;
