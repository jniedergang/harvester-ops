/**
 * harvester-ops — les sections de Harvester sous « Cluster » (v1.57.0)
 *
 * Storage, Network, Add-ons et Security, rangées comme dans l'interface de
 * Harvester, un cluster à la fois. Chaque section a ses onglets ; un onglet
 * est soit une vue de blocs existante (volumes, réseaux, VPC, fabrique),
 * montée par App.mountTopology, soit une liste (ResourceViews).
 */
const Sections = (() => {
  const $$ = (s) => document.querySelectorAll(s);
  const DEF = {
    storage: { first: 'volumes', panes: { volumes: { board: 'storage' },
                                          images: { list: 'images' },
                                          classes: { list: 'storageclasses' } } },
    network: { first: 'vmnets', panes: { vmnets: { board: 'network' },
                                         overlay: { board: 'vpc' },
                                         underlay: { board: 'fabric' } } },
    addons: { first: 'list', panes: { list: { list: 'addons' } } },
    security: { first: 'secrets', panes: { secrets: { list: 'secrets' },
                                           sshkeys: { list: 'sshkeys' } } },
  };
  const KEY = (sec) => `harvester_ops_section_${sec}`;

  function isSection(name) { return Object.prototype.hasOwnProperty.call(DEF, name); }

  function current(sec) {
    let p = null;
    try { p = localStorage.getItem(KEY(sec)); } catch {}
    return DEF[sec].panes[p] ? p : DEF[sec].first;
  }

  function paint(sec, pane) {
    const root = document.getElementById(`tab-${sec}`);
    if (!root) return;
    root.querySelectorAll('[data-section-tab]').forEach(b => {
      const on = b.dataset.sectionTab === pane;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    root.querySelectorAll('.section-pane').forEach(p => { p.hidden = p.dataset.pane !== pane; });
  }

  /** Montre l'onglet courant de la section et le fait vivre ; rend la
   *  promesse du premier chargement (pour le voile d'une bascule). */
  function activate(sec) {
    if (!isSection(sec)) return Promise.resolve();
    const pane = current(sec);
    paint(sec, pane);
    const spec = DEF[sec].panes[pane];
    const cluster = window.App && App.getCurrentCluster();
    if (!cluster) return Promise.resolve();
    if (spec.board) {
      if (window.ResourceViews) ResourceViews.stop();
      return App.mountTopology(spec.board) || Promise.resolve();
    }
    if (window.App) App.stopBoards(null);
    const host = document.querySelector(`#tab-${sec} .section-pane[data-pane="${pane}"] .resource-host`);
    return window.ResourceViews ? ResourceViews.start(spec.list, cluster, host) : Promise.resolve();
  }

  function show(sec, pane) {
    if (!isSection(sec) || !DEF[sec].panes[pane]) return;
    try { localStorage.setItem(KEY(sec), pane); } catch {}
    return activate(sec);
  }

  /** Ouvre une section sur un onglet (liens d'une vue vers une autre). */
  function open(sec, pane) {
    if (pane) { try { localStorage.setItem(KEY(sec), pane); } catch {} }
    if (window.App && App.setTab) App.setTab(sec);
  }

  function stopLists() { if (window.ResourceViews) ResourceViews.stop(); }

  function init() {
    $$('[data-section-tab]').forEach(b => b.addEventListener('click', () => {
      const sec = b.closest('[data-section]')?.dataset.section;
      if (sec) show(sec, b.dataset.sectionTab);
    }));
    Object.keys(DEF).forEach(sec => paint(sec, current(sec)));
  }

  document.addEventListener('DOMContentLoaded', init);
  return { isSection, activate, show, open, stopLists, current };
})();
window.Sections = Sections;
