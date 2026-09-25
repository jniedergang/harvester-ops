/**
 * harvester-ops — mise en forme de la progression d'un transfert (v1.46.0).
 *
 * Le script publie des points `{phase, item, done, total, wire, rate, eta,
 * elapsed, items_done, items_total, final}` (octets et secondes) ; ce module
 * les rend lisibles dans la langue de l'interface, pour le dock et pour la
 * fenêtre « Migrer ». Un seul endroit, pour que les deux disent la même chose.
 */
const XferProgress = (() => {
  const tr = (k, vars) => (window.i18n ? i18n.t(k, vars) : k);

  // Un appel littéral par phase : le contrôle de parité des traductions ne
  // voit que ces formes.
  const PHASES = {
    freeze: () => tr('progress.phase.freeze'),
    download: () => tr('progress.phase.download'),
    import: () => tr('progress.phase.import'),
    backup: () => tr('progress.phase.backup'),
    images: () => tr('progress.phase.images'),
    restore: () => tr('progress.phase.restore'),
  };

  function lang() {
    return (window.i18n && i18n.currentLang) || 'en';
  }

  function num(v, digits) {
    try {
      return Number(v).toLocaleString(lang(), { minimumFractionDigits: digits,
                                                maximumFractionDigits: digits });
    } catch (_) {
      return Number(v).toFixed(digits);
    }
  }

  function bytes(n) {
    const units = tr('progress.units').split(',');
    let v = Number(n || 0), i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${i ? num(v, 1) : Math.round(v)} ${units[i]}`;
  }

  // Deux quantités dans la même unité : « 3,2 / 10,0 Gio »
  function pair(done, total) {
    const units = tr('progress.units').split(',');
    let i = 0, t = Number(total || 0);
    while (t >= 1024 && i < units.length - 1) { t /= 1024; i++; }
    const div = Math.pow(1024, i);
    return `${num((done || 0) / div, i ? 1 : 0)} / ${num((total || 0) / div, i ? 1 : 0)} ${units[i]}`;
  }

  function duration(s) {
    s = Math.max(0, Math.round(s || 0));
    if (s < 60) return `${s} s`;
    if (s < 3600) return `${Math.floor(s / 60)} min ${s % 60} s`;
    return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
  }

  function phase(snap) {
    const fn = PHASES[snap.phase];
    return fn ? fn() : snap.phase;
  }

  function pct(snap) {
    if (!snap || !snap.total) return snap && snap.final ? 100 : 0;
    return Math.max(0, Math.min(100, Math.round(100 * (snap.done || 0) / snap.total)));
  }

  // « Import disk-0 : 3,2 / 10,0 Gio (512 Mio transmis) · 85,0 Mio/s · reste 1 min 30 s »
  function text(snap) {
    if (!snap) return '';
    const head = phase(snap) + (snap.item ? ` ${snap.item}` : '');
    let s = tr('progress.head', { head, amount: pair(snap.done, snap.total) });
    if (snap.wire) s += ` (${tr('progress.sent', { wire: bytes(snap.wire) })})`;
    const parts = [s];
    if (snap.rate != null) parts.push(`${bytes(snap.rate)}/s`);
    if (snap.final) parts.push(tr('progress.took', { t: duration(snap.elapsed) }));
    // tout est parti mais la cible n'a pas fini d'écrire (vécu : CDI à 99 %
    // pendant cinq minutes) : le dire, plutôt qu'une barre pleine muette
    else if (snap.total && (snap.done || 0) >= snap.total) parts.push(tr('progress.writing'));
    else if (snap.eta != null) parts.push(tr('progress.left', { t: duration(snap.eta) }));
    return parts.join(' · ');
  }

  // Pour les phases mesurées en pourcentage d'une sauvegarde : ce qui
  // transite peut être bien moindre (sauvegardes incrémentales).
  function note(snap) {
    return snap && (snap.phase === 'backup' || snap.phase === 'restore')
      ? tr('progress.incremental') : '';
  }

  return { text, pct, bytes, duration, phase, note, pair };
})();

window.XferProgress = XferProgress;
