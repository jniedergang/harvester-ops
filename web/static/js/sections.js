/**
 * harvester-ops — les sections de Harvester sous « Cluster » (v1.57.0)
 *
 * Storage, Network, Add-ons, Security et Advanced, rangées comme dans l'interface de
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
                                         // v1.65.0 : le menu Networks de Harvester
                                         clusternets: { mod: 'NetAdmin' },
                                         lbs: { list: 'loadbalancers' },
                                         pools: { list: 'ippools' },
                                         hostnets: { list: 'hostnetworks' },
                                         overlay: { board: 'vpc' },
                                         underlay: { board: 'fabric' } } },
    addons: { first: 'list', panes: { list: { list: 'addons' } } },
    security: { first: 'secrets', panes: { secrets: { list: 'secrets' },
                                           sshkeys: { list: 'sshkeys' } } },
    // v1.70.0 : le menu Monitoring & Logging de Harvester
    monlog: { first: 'metrics', panes: { metrics: { mod: 'MonLog' }, alerts: { mod: 'MonLog' },
                                         flows: { mod: 'MonLog' }, outputs: { mod: 'MonLog' } } },
    // v1.71.0 : le menu Virtual Machine Imports de Harvester
    vmimport: { first: 'imports', panes: { imports: { mod: 'VMImport' }, vmware: { mod: 'VMImport' },
                                           openstack: { mod: 'VMImport' }, ova: { mod: 'VMImport' } } },
    // v1.75.0 : migrations VMware par Forklift
    forklift: { first: 'prep', panes: { prep: { mod: 'Forklift' }, sources: { mod: 'Forklift' },
                                        inventory: { mod: 'Forklift' },
                                        // v1.76.0 : vagues à chaud
                                        waves: { mod: 'Forklift' } } },
    // v1.67.0 : le menu Advanced de Harvester (réglages, paquet de support, kubeconfigs)
    advanced: { first: 'settings', panes: { settings: { mod: 'Advanced' },
                                            // v1.68.0 : les périphériques de Harvester
                                            pci: { mod: 'Devices' },
                                            usb: { mod: 'Devices' },
                                            sriov: { mod: 'Devices' },
                                            support: { mod: 'Advanced' } } },
  };
  // les modules qui tiennent leur onglet eux-mêmes : un seul vit à la fois
  const MODS = ['NetAdmin', 'Advanced', 'Devices', 'MonLog', 'VMImport', 'Forklift'];
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
    // v1.59.0 : « Nouveau volume » ou « Nouveau réseau » selon l'onglet
    root.querySelectorAll('[data-pane-only]').forEach(b => { b.hidden = b.dataset.paneOnly !== pane; });
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
    MODS.forEach(m => { if (window[m]) window[m].stop(); });
    if (spec.mod) {
      // v1.65.0 : un onglet tenu par son propre module (Cluster networks)
      if (window.ResourceViews) ResourceViews.stop();
      if (window.App) App.stopBoards(null);
      const host = document.querySelector(`#tab-${sec} .section-pane[data-pane="${pane}"] .na-host`);
      return window[spec.mod] && host ? window[spec.mod].start(cluster, host) : Promise.resolve();
    }
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

  // v1.67.0 : quitter la section coupe aussi les modules (leur minuterie
  // continuait d'interroger le cluster en arrière-plan)
  function stopLists() {
    if (window.ResourceViews) ResourceViews.stop();
    MODS.forEach(m => { if (window[m]) window[m].stop(); });
  }

  function init() {
    $$('[data-section-tab]').forEach(b => b.addEventListener('click', () => {
      const sec = b.closest('[data-section]')?.dataset.section;
      if (sec) show(sec, b.dataset.sectionTab);
    }));
    Object.keys(DEF).forEach(sec => paint(sec, current(sec)));
    $$('[data-section-new]').forEach(b => b.addEventListener('click', () => {
      const cluster = window.App && App.getCurrentCluster();
      const sec = b.closest('[data-section]')?.dataset.section;
      if (cluster && window.ObjectForms) {
        ObjectForms.openNew(b.dataset.sectionNew, cluster, { onDone: () => activate(sec) });
      }
    }));
  }

  document.addEventListener('DOMContentLoaded', init);
  return { isSection, activate, show, open, stopLists, current };
})();
window.Sections = Sections;
