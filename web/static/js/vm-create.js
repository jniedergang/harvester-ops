/**
 * harvester-ops — création de machines virtuelles (v1.28.0)
 *
 * La console savait tout éditer d'une VM mais pas en créer une.
 *
 * Le parti pris : ce panneau ne réécrit AUCUN formulaire. Il rejoue les huit
 * sections de l'éditeur (`VMEdit.renderSectionHtml` / `wireSection` /
 * `buildPatch`) sur un SQUELETTE de VM au lieu d'une VM existante, puis
 * fusionne les fragments produits par chaque section pour obtenir un
 * manifeste complet. Conséquence directe : tout ce qui est éditable est
 * réglable à la création, par construction, et le restera sans effort le
 * jour où une section gagnera un champ.
 *
 * S'ajoutent ici les trois choses qui n'existent qu'à la création :
 *   - le nom, le namespace et le nombre d'instances ;
 *   - le choix de démarrer ou non la VM une fois créée ;
 *   - l'enregistrement de la configuration comme template Harvester.
 */
const VMCreate = (() => {
  const PANEL_ID = 'vm-create';

  function tr(key, fallback) {
    return (window.i18n && i18n.t(key) !== key) ? i18n.t(key) : fallback;
  }
  function esc(v) {
    if (v === null || v === undefined) return '';
    return String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // ---------------------------------------------------------------------
  // Le squelette : une VM minimale mais VALIDE, de la même forme que celles
  // que renvoie le cluster. Les sections de l'éditeur savent la lire sans
  // rien savoir du fait qu'elle n'existe pas encore.
  // ---------------------------------------------------------------------
  function skeleton(name, namespace) {
    return {
      apiVersion: 'kubevirt.io/v1',
      kind: 'VirtualMachine',
      // Le namespace compte dès le squelette : sans lui, les références de
      // PVC des sections s'affichent « undefined/pvc-xxx ».
      metadata: { name, namespace: namespace || 'default',
                  annotations: {}, labels: {} },
      spec: {
        runStrategy: 'Halted',
        template: {
          metadata: { labels: { 'harvesterhci.io/vmName': name } },
          spec: {
            hostname: name,
            // Comme l'interface de Harvester : sans stratégie d'éviction, la
            // VM est ARRÊTÉE par la mise en maintenance de son nœud au lieu
            // de migrer (constaté sur harvlab, v1.44.3).
            evictionStrategy: 'LiveMigrateIfPossible',
            domain: {
              cpu: { cores: 1, sockets: 1, threads: 1 },
              memory: { guest: '2Gi' },
              resources: { limits: { cpu: '1', memory: '2Gi' } },
              devices: {
                disks: [],
                interfaces: [{ name: 'default', masquerade: {}, model: 'virtio' }],
              },
            },
            networks: [{ name: 'default', pod: {} }],
            volumes: [],
          },
        },
      },
    };
  }

  /** Fusion profonde des fragments de section dans le squelette.
   *  Les tableaux sont REMPLACÉS et non concaténés : la section disques
   *  décrit l'ensemble des disques, pas un ajout. */
  function merge(target, patch) {
    if (!patch || typeof patch !== 'object') return target;
    Object.keys(patch).forEach(k => {
      const v = patch[k];
      if (v === null || v === undefined) { delete target[k]; return; }
      if (Array.isArray(v)) { target[k] = v; return; }
      if (typeof v === 'object') {
        if (typeof target[k] !== 'object' || target[k] === null
            || Array.isArray(target[k])) target[k] = {};
        merge(target[k], v);
        return;
      }
      target[k] = v;
    });
    return target;
  }

  /** Retire les `null` laissés par les fragments d'édition : un merge patch
   *  s'en sert pour SUPPRIMER une clé, mais un manifeste de création qui en
   *  contient est refusé par l'apiserver. */
  function stripNulls(o) {
    if (Array.isArray(o)) { o.forEach(stripNulls); return o; }
    if (o && typeof o === 'object') {
      Object.keys(o).forEach(k => {
        if (o[k] === null) delete o[k];
        else stripNulls(o[k]);
      });
    }
    return o;
  }

  function open(cluster, namespace) {
    if (!window.VMEdit || !VMEdit.renderSectionHtml) {
      alert('VMEdit indisponible');
      return;
    }
    const existing = document.getElementById('fp-' + PANEL_ID);
    if (existing) {
      return FloatingPanels.open({ id: PANEL_ID, icon: 'vm',
        title: tr('vm.create.title', 'Create a virtual machine') });
    }

    const sections = VMEdit.SECTIONS;
    const html = `
      <div class="vm-create">
        <form class="capi-form vm-create-head">
          <fieldset>
            <legend>${esc(tr('vm.create.identity', 'Identity'))}</legend>
            <label>${esc(tr('vm.create.name', 'Name'))} *
              <input name="name" required value="vm-01"
                     pattern="[a-z0-9]([-a-z0-9]*[a-z0-9])?">
              <span class="form-hint">${esc(tr('vm.create.nameHint',
                'Lower case, digits and dashes (RFC 1123).'))}</span></label>
            <label>${esc(tr('vm.create.namespace', 'Namespace'))} *
              <select name="namespace" required></select></label>
            <label>${esc(tr('vm.create.count', 'How many'))}
              <input name="count" type="number" min="1" max="50" value="1">
              <span class="form-hint">${esc(tr('vm.create.countHint',
                'Above one, names are suffixed: web-01, web-02...'))}</span></label>
            <label class="vm-create-start">
              <input name="start" type="checkbox" checked>
              <span>${esc(tr('vm.create.start', 'Start once created'))}</span></label>
            <label style="grid-column:1/-1;">${esc(tr('vm.create.template', 'Start from a template'))}
              <select name="template"><option value="">${
                esc(tr('vm.create.noTemplate', '(from scratch)'))}</option></select>
              <span class="form-hint">${esc(tr('vm.create.templateHint',
                'Loads the template settings as a starting point. Everything stays editable.'))}</span></label>
          </fieldset>
        </form>

        <div class="vm-edit-layout vm-create-layout">
          <aside class="vm-edit-nav">
            ${sections.map(s => `
              <button type="button" data-section="${s.id}">
                <span class="ic">${Icons.svg(s.icon, { size: 14 })}</span>
                <span>${esc(s.label())}</span>
              </button>`).join('')}
          </aside>
          <main class="vm-edit-content"></main>
        </div>

        <div class="apply-bar vm-create-actions">
          <button type="button" class="btn btn-primary btn-sm btn-ico tip"
                  data-action="create" data-tip="${esc(tr('vm.create.tip.create',
                  'Create the virtual machine(s) on the selected cluster'))}">
            ${Icons.svg('add')} ${esc(tr('vm.create.go', 'Create'))}
          </button>
          <button type="button" class="btn btn-secondary btn-sm btn-ico tip"
                  data-action="dry-run" data-tip="${esc(tr('vm.create.tip.dryRun',
                  'Ask the cluster to validate the manifest without creating anything'))}">
            ${Icons.svg('preview')} ${esc(tr('vm.create.dryRun', 'Validate only'))}
          </button>
          <button type="button" class="btn btn-secondary btn-sm btn-ico tip"
                  data-action="save-template" data-tip="${esc(tr('vm.create.tip.template',
                  'Save this configuration as a reusable Harvester template'))}">
            ${Icons.svg('bundle')} ${esc(tr('vm.create.saveTemplate', 'Save as template'))}
          </button>
          <span class="apply-result" data-result></span>
        </div>
      </div>`;

    const panel = FloatingPanels.open({
      id: PANEL_ID,
      title: tr('vm.create.title', 'Create a virtual machine'),
      icon: 'vm',
      bodyHtml: html,
      width: 1040,
      height: 680,
      restoreSpec: { type: 'vm-create', args: { cluster, namespace } },
    });

    const root = panel.el;
    const head = root.querySelector('.vm-create-head');
    const content = root.querySelector('.vm-edit-content');
    const navBtns = root.querySelectorAll('.vm-edit-nav button');
    const result = root.querySelector('[data-result]');

    // Une seule instance de squelette pour toute la vie du panneau : les
    // sections déjà visitées y ont écrit, on ne doit pas la recréer.
    let vm = skeleton('vm-01', namespace);
    const rendered = new Map();          // id -> élément de section

    head.querySelector('[name="namespace"]').addEventListener('change', (e) => {
      vm.metadata.namespace = e.target.value;
    });

    const nameInput = head.querySelector('[name="name"]');
    nameInput.addEventListener('input', () => {
      const n = nameInput.value.trim();
      if (n) {
        vm.metadata.name = n;
        vm.spec.template.spec.hostname = n;
        vm.spec.template.metadata.labels['harvesterhci.io/vmName'] = n;
      }
    });

    // Namespaces : ceux du cluster, avec `default` en tête.
    (async () => {
      const sel = head.querySelector('[name="namespace"]');
      try {
        const list = await fetch(`/api/namespaces/${encodeURIComponent(cluster)}`)
          .then(r => r.json());
        const names = (Array.isArray(list) ? list : list.namespaces || [])
          .map(n => (typeof n === 'string' ? n : n.name)).filter(Boolean);
        sel.innerHTML = names.map(n =>
          `<option value="${esc(n)}"${n === namespace ? ' selected' : ''}>${esc(n)}</option>`).join('');
      } catch {
        sel.innerHTML = `<option value="${esc(namespace || 'default')}">${
          esc(namespace || 'default')}</option>`;
      }
    })();

    // --- templates : liste, puis application comme point de départ ---
    (async () => {
      const sel = head.querySelector('[name="template"]');
      try {
        const d = await fetch(`/api/vmtemplates/${encodeURIComponent(cluster)}`)
          .then(r => r.json());
        (d.templates || []).forEach(tp => {
          const id = `${tp.namespace}/${tp.name}`;
          const o = document.createElement('option');
          o.value = id;
          o.textContent = tp.description ? `${id} — ${tp.description}` : id;
          sel.appendChild(o);
        });
      } catch { /* pas de template : le choix « depuis zéro » suffit */ }
      sel.addEventListener('change', () => applyTemplate(sel.value));
    })();

    async function applyTemplate(id) {
      const name = nameInput.value.trim() || 'vm-01';
      const ns = head.querySelector('[name="namespace"]').value || namespace;
      if (!id) { vm = skeleton(name, ns); resetSections(); return; }
      say(esc(tr('vm.create.loadingTemplate', 'Loading the template…')));
      try {
        const [ns, tpl] = id.split('/');
        const d = await fetch(`/api/vmtemplates/${encodeURIComponent(cluster)}`
          + `/${encodeURIComponent(ns)}/${encodeURIComponent(tpl)}`).then(r => r.json());
        if (d.error) { say(esc(d.error), true); return; }
        // On repart du squelette et on y fusionne la spec du template : le
        // template ne porte ni nom ni namespace, et il peut lui manquer des
        // champs que les sections attendent.
        const base = skeleton(name, ns);
        merge(base, { metadata: (d.vm && d.vm.metadata) || {},
                      spec: (d.vm && d.vm.spec) || {} });
        // Le template porte SON namespace d'origine ; la VM ira dans celui
        // choisi ici. Sans ce rappel après la fusion, les références de PVC
        // pointent le namespace du template.
        base.metadata.name = name;
        base.metadata.namespace = ns;
        vm = base;
        resetSections();
        say(esc(tr('vm.create.templateLoaded', 'Template loaded')) + ` <code>${esc(id)}</code>`);
      } catch (e) { say(esc(e.message), true); }
    }

    /** Le squelette a changé sous les sections : elles doivent être
     *  reconstruites, sinon on éditerait encore l'ancienne VM. */
    function resetSections() {
      rendered.forEach(el => el.remove());
      rendered.clear();
      const active = root.querySelector('.vm-edit-nav button.active');
      showSection(active ? active.dataset.section : 'general');
    }

    function showSection(id) {
      navBtns.forEach(b => b.classList.toggle('active', b.dataset.section === id));
      rendered.forEach((el, key) => { el.hidden = key !== id; });
      if (rendered.has(id)) return;
      const wrap = document.createElement('div');
      wrap.className = 'vm-create-section';
      // Rien n'existe encore : les PVC déclarés par un template sont des
      // recettes, pas des volumes à sélectionner.
      wrap.innerHTML = VMEdit.renderSectionHtml(id, vm, cluster,
                                                { claimsAreToCreate: true });
      content.appendChild(wrap);
      rendered.set(id, wrap);
      try {
        VMEdit.wireSection(wrap, id, cluster, namespace, vm.metadata.name,
                           () => vm, { createMode: true });
      } catch (e) {
        console.warn('wireSection', id, e);
      }
      rendered.forEach((el, key) => { el.hidden = key !== id; });
    }

    navBtns.forEach(b =>
      b.addEventListener('click', () => showSection(b.dataset.section)));
    showSection('general');

    /** Assemble le manifeste à partir des sections VISITÉES. Une section
     *  jamais ouverte garde les valeurs du squelette, ce qui est le
     *  comportement voulu : on ne force personne à parcourir les huit
     *  onglets pour créer une VM. */
    function buildManifest() {
      const out = JSON.parse(JSON.stringify(vm));
      rendered.forEach((el, id) => {
        let fragment;
        try {
          fragment = VMEdit.buildPatch(el, id, out);
        } catch (e) {
          throw new Error(`${id}: ${e.message || e}`);
        }
        if (fragment) merge(out, fragment);
      });
      return stripNulls(out);
    }

    function say(html, bad) {
      result.innerHTML = `<span style="color:var(--${bad ? 'danger' : 'accent'})">${
        Icons.svg(bad ? 'fail' : 'ok', { size: 14 })} ${html}</span>`;
    }

    async function submit(dryRun) {
      const name = nameInput.value.trim();
      const ns = head.querySelector('[name="namespace"]').value;
      const count = parseInt(head.querySelector('[name="count"]').value, 10) || 1;
      const start = head.querySelector('[name="start"]').checked;
      if (!name) { say(esc(tr('vm.create.errName', 'A name is required')), true); return; }
      let manifest;
      try { manifest = buildManifest(); }
      catch (e) { say(esc(String(e.message || e)), true); return; }

      say(esc(tr('vm.create.sending', 'Sending…')));
      try {
        const r = await fetch(`/api/vms/${encodeURIComponent(cluster)}/create`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ namespace: ns, name, count, start,
                                 manifest, dry_run: dryRun }),
        });
        const d = await r.json();
        if (!r.ok) { say(esc(d.error || 'error'), true); return; }
        say(`${esc(dryRun ? tr('vm.create.validating', 'Validation started')
                          : tr('vm.create.started', 'Creation started'))} `
            + `<code>${esc(d.action_id)}</code> — ${esc((d.names || []).join(', '))}`);
        if (!dryRun) setTimeout(() => window.App && App.refreshNamespaces
                                && App.refreshNamespaces(false), 4000);
      } catch (e) { say(esc(e.message), true); }
    }

    async function saveTemplate() {
      const name = nameInput.value.trim();
      const ns = head.querySelector('[name="namespace"]').value;
      let manifest;
      try { manifest = buildManifest(); }
      catch (e) { say(esc(String(e.message || e)), true); return; }
      const tplName = prompt(tr('vm.create.templateName',
                                'Name for the template:'), `${name}-template`);
      if (!tplName) return;
      say(esc(tr('vm.create.sending', 'Sending…')));
      try {
        const r = await fetch(`/api/vmtemplates/${encodeURIComponent(cluster)}`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ namespace: ns, name: tplName, manifest }),
        });
        const d = await r.json();
        if (!r.ok) { say(esc(d.error || 'error'), true); return; }
        say(esc(tr('vm.create.templateSaved', 'Template saved')) + ` <code>${esc(d.name || tplName)}</code>`);
      } catch (e) { say(esc(e.message), true); }
    }

    root.querySelector('[data-action="create"]')
        .addEventListener('click', () => submit(false));
    root.querySelector('[data-action="dry-run"]')
        .addEventListener('click', () => submit(true));
    root.querySelector('[data-action="save-template"]')
        .addEventListener('click', saveTemplate);

    return panel;
  }

  return { open, _internals: { skeleton, merge, stripNulls } };
})();

if (typeof window !== 'undefined') window.VMCreate = VMCreate;
if (typeof FloatingPanels !== 'undefined') {
  FloatingPanels.registerType('vm-create', (args) =>
    VMCreate.open(args.cluster, args.namespace));
}
