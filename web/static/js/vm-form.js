/**
 * harvester-ops : coquille commune des fenêtres de création et de
 * modification d'une VM (prochaine version KVM).
 *
 * Vision de ju : toutes les VMs dans une vue, deux familles (Harvester, KVM).
 * La fenêtre de création n'est donc pas recopiée par famille : une coquille
 * commune (en-tête, barre d'onglets à icônes, pied avec le résultat) et un
 * ADAPTATEUR par famille, qui dit quels onglets proposer et comment rendre,
 * câbler et lire chacun. vm-create.js (Harvester) et kvm-form.js (KVM) s'en
 * servent ; vm-edit.js reste propre à Harvester dans cette version.
 *
 * Contrat d'un adaptateur passé à `mount` :
 *   - `sections` : [{ id, icon, label(), tip? }] ;
 *   - `renderSection(id)` : HTML de l'onglet (chaque nom échappé par lui) ;
 *   - `wireSection(id, el)` (facultatif) : écouteurs de l'onglet rendu ;
 *   - `onShow(id, el)` (facultatif) : appelé à chaque affichage d'un onglet.
 * Le reste (lecture du modèle, validation, envoi, modèles) appartient à
 * l'adaptateur ; la coquille fournit les briques d'affichage partagées :
 * erreurs par onglet, lignes de résultat « appliqué maintenant / au prochain
 * démarrage / refusé », repères par champ, écran de revue.
 */
const VMForm = (() => {
  const tr = (k, fb, p) => {
    try { const v = window.i18n && i18n.t(k, p); return v && v !== k ? v : fb; }
    catch { return fb; }
  };
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 14) => (window.Icons ? Icons.svg(n, { size }) : '');

  /** Le corps de la fenêtre : en-tête (déjà rendu par l'adaptateur), barre
   *  d'onglets, zone des onglets, pied (boutons + résultat). Même balisage
   *  que la fenêtre de création Harvester depuis la 1.28.0. */
  // bulle d'un onglet : texte, ou fonction relue à l'ouverture (langue courante)
  const tipOf = (s) => (typeof s.tip === 'function' ? s.tip() : s.tip) || '';
  function shellHtml({ head = '', sections, actions = '', cls = '' }) {
    return `
      <div class="vm-create${cls ? ` ${esc(cls)}` : ''}">
        ${head}

        <div class="vm-edit-layout vm-create-layout">
          <aside class="vm-edit-nav">
            ${sections.map(s => `
              <button type="button" data-section="${esc(s.id)}"${tipOf(s) ? ` class="tip" data-tip="${esc(tipOf(s))}"` : ''}>
                <span class="ic">${icon(s.icon)}</span>
                <span>${esc(s.label())}</span>
              </button>`).join('')}
          </aside>
          <main class="vm-edit-content"></main>
        </div>

        <div class="apply-bar vm-create-actions">
          ${actions}
          <span class="apply-result" data-result></span>
        </div>
      </div>`;
  }

  /** Onglets : rendus à la première ouverture (ou tous d'un coup avec
   *  `eager`), puis gardés tels quels (la saisie survit au changement
   *  d'onglet). Rend { show, reset, rendered, current }. */
  function mount(root, adapter, opts = {}) {
    const content = root.querySelector('.vm-edit-content');
    const navBtns = [...root.querySelectorAll('.vm-edit-nav button')];
    const rendered = new Map();
    let current = null;

    function build(id) {
      const wrap = document.createElement('div');
      wrap.className = 'vm-create-section';
      wrap.dataset.vmfSection = id;
      wrap.innerHTML = adapter.renderSection(id);
      wrap.hidden = true;
      content.appendChild(wrap);
      rendered.set(id, wrap);
      try {
        if (adapter.wireSection) adapter.wireSection(id, wrap);
      } catch (e) {
        console.warn('wireSection', id, e);
      }
      return wrap;
    }

    function show(id) {
      current = id;
      navBtns.forEach(b => b.classList.toggle('active', b.dataset.section === id));
      if (!rendered.has(id)) build(id);
      rendered.forEach((el, key) => { el.hidden = key !== id; });
      if (adapter.onShow) adapter.onShow(id, rendered.get(id));
    }

    function reset() {
      rendered.forEach(el => el.remove());
      rendered.clear();
      if (opts.eager) adapter.sections.forEach(s => build(s.id));
      show(current || adapter.sections[0].id);
    }

    // un second montage sur la même fenêtre (changement d'hôte, modèle) remplace
    // le premier : ses écouteurs de la barre d'onglets sont retirés
    if (root.__vmfNav) root.__vmfNav.abort();
    const nav = new AbortController();
    root.__vmfNav = nav;
    navBtns.forEach(b => b.addEventListener('click', () => show(b.dataset.section), { signal: nav.signal }));
    if (opts.eager) adapter.sections.forEach(s => build(s.id));
    return { show, reset, rendered, current: () => current };
  }

  /** Le message du pied (même allure que la fenêtre Harvester). */
  function say(resultEl, html, bad) {
    resultEl.innerHTML = `<span style="color:var(--${bad ? 'danger' : 'accent'})">${
      icon(bad ? 'fail' : 'ok')} ${html}</span>`;
  }

  // -- validation : une erreur nomme son onglet et son champ -----------------------------
  /** `errors` : [{ section, field?, message }]. Chaque onglet fautif porte le
   *  nombre de ses erreurs, chaque champ nommé est marqué, la liste s'affiche
   *  dans `listEl`. Rend vrai s'il n'y a aucune erreur. */
  function showErrors(root, errors, listEl) {
    root.querySelectorAll('.vmf-err-count').forEach(e => e.remove());
    root.querySelectorAll('.vmf-invalid').forEach(e => e.classList.remove('vmf-invalid'));
    root.querySelectorAll('.vmf-invalid-ctl').forEach(e => {
      e.classList.remove('vmf-invalid-ctl');
      e.removeAttribute('aria-invalid');
    });
    const by = {};
    (errors || []).forEach(e => { by[e.section] = (by[e.section] || 0) + 1; });
    root.querySelectorAll('.vm-edit-nav button').forEach(b => {
      const n = by[b.dataset.section];
      if (n) {
        b.insertAdjacentHTML('beforeend', ` <span class="badge fail vmf-err-count tip" data-tip="${
          esc(tr('vmf.errCountTip', `${n} error(s) in this tab`, { n }))}">${esc(n)}</span>`);
      }
    });
    (errors || []).forEach(e => {
      if (!e.field) return;
      const el = root.querySelector(`[data-vmf-field="${CSS.escape(e.field)}"]`);
      if (!el) return;
      el.classList.add('vmf-invalid');
      // aria-invalid va sur le contrôle lui-même (le libellé qui l'entoure
      // n'est pas lu comme un champ par les lecteurs d'écran)
      const ctl = el.matches('input, select, textarea') ? el : el.querySelector('input, select, textarea');
      if (ctl) { ctl.classList.add('vmf-invalid-ctl'); ctl.setAttribute('aria-invalid', 'true'); }
    });
    if (listEl) {
      listEl.innerHTML = (errors || []).length
        ? `<ul class="vmf-errors" role="alert">${errors.map(e => `<li class="res-error">${icon('fail', 12)} ${
          esc(sectionLabel(root, e.section))} : ${esc(e.message)}</li>`).join('')}</ul>`
        : '';
    }
    return !(errors || []).length;
  }

  function sectionLabel(root, id) {
    const b = [...root.querySelectorAll('.vm-edit-nav button')].find(x => x.dataset.section === id);
    return b ? b.querySelector('span:not(.ic):not(.vmf-err-count)').textContent.trim() : id;
  }

  // -- « appliqué maintenant / au prochain démarrage / refusé » -------------------------
  const APPLIED = {
    now: ['ok', () => tr('vmf.applied.now', 'applied now'), () => tr('vmf.applied.nowTip', 'Applied to the running VM and to its configuration')],
    'next-boot': ['warn', () => tr('vmf.applied.nextBoot', 'at next boot'), () => tr('vmf.applied.nextBootTip', 'Written to the configuration; the VM takes it at its next start')],
    refused: ['fail', () => tr('vmf.applied.refused', 'refused'), () => tr('vmf.applied.refusedTip', 'Not applied: the reason is given next to it')],
    failed: ['fail', () => tr('vmf.applied.failed', 'failed'), () => tr('vmf.applied.failedTip', 'The host refused this change')],
    uncertain: ['warn', () => tr('vmf.applied.uncertain', 'uncertain'), () => tr('vmf.applied.uncertainTip', 'The connection was lost during the write: reload the VM')],
    'not-done': ['dim', () => tr('vmf.applied.notDone', 'not done'), () => tr('vmf.applied.notDoneTip', 'Not attempted: an earlier change failed')],
  };

  /** Un repère d'état : `applied` (now, next-boot, refused, failed,
   *  uncertain, not-done), bulle facultative (la raison). */
  // repère d'un champ AVANT l'envoi (prévision d'après la table servie)
  const FORECAST = {
    now: () => tr('vmf.hot.now', 'now'), 'next-boot': () => tr('vmf.hot.nextBoot', 'next boot'),
    refused: () => tr('vmf.hot.refused', 'refused'),
  };
  function appliedBadge(applied, reason, opts = {}) {
    const a = APPLIED[applied] || ['dim', () => String(applied || '?'), () => ''];
    const tip = reason || a[2]();
    const text = opts.forecast && FORECAST[applied] ? FORECAST[applied]() : a[1]();
    return `<span class="badge ${a[0]} vmf-applied tip" data-applied="${esc(applied)}" data-tip="${esc(tip)}">${esc(text)}</span>`;
  }

  /** Les lignes d'un résultat ou d'un aperçu : une par changement. */
  function changesHtml(changes) {
    if (!(changes || []).length) return `<p class="form-hint">${esc(tr('vmf.noChange', 'Nothing to change.'))}</p>`;
    return `<ul class="vmf-changes">${changes.map(c => `<li data-field="${esc(c.field)}">
        ${appliedBadge(c.applied, c.reason)} <code>${esc(c.field)}</code>${c.reason ? ` <span class="res-dim">${esc(c.reason)}</span>` : ''}</li>`).join('')}</ul>`;
  }

  /** Le bandeau « redémarrage nécessaire » : arrêt puis démarrage (un
   *  redémarrage dans l'invité ne suffit pas, le processus QEMU doit être
   *  recréé). `fields` : champs en attente. */
  function restartBanner(fields, buttonHtml = '') {
    if (!(fields || []).length) return '';
    return `<div class="vmf-restart" role="status">
        <span class="badge warn tip" data-tip="${esc(tr('vmf.restartTip', 'A reboot inside the guest is not enough: shut the VM down, then start it'))}">${
          esc(tr('vmf.restartNeeded', 'restart needed'))}</span>
        <span>${esc(tr('vmf.restartFields', 'Waiting for the next start:'))} ${fields.map(f => `<code>${esc(f)}</code>`).join(', ')}</span>
        ${buttonHtml}</div>`;
  }

  /** L'écran de revue : refus, avertissements, lignes, et un texte en
   *  lecture (le XML de l'aperçu, mot de passe déjà masqué par le serveur). */
  function reviewHtml({ refusals = [], warnings = [], body = '', xml = '' } = {}) {
    return `${refusals.length ? `<div class="vmf-refusals" role="alert"><strong>${esc(tr('vmf.refusals', 'Refused'))}</strong>
          <ul>${refusals.map(r => `<li class="res-error">${icon('fail', 12)} ${esc(r)}</li>`).join('')}</ul></div>` : ''}
      ${warnings.length ? `<div class="vmf-warnings"><strong>${esc(tr('vmf.warnings', 'Warnings'))}</strong>
          <ul>${warnings.map(w => `<li>${icon('warn', 12)} ${esc(w)}</li>`).join('')}</ul></div>` : ''}
      ${body}
      ${xml ? `<details class="vm-edit-adv vmf-xml"><summary class="tip" data-tip="${esc(tr('vmf.xmlTip', 'The definition the host will receive, read-only'))}">${
        esc(tr('vmf.xml', 'Definition (XML)'))}</summary><pre class="yaml-editor">${esc(xml)}</pre></details>` : ''}`;
  }

  return { shellHtml, mount, say, showErrors, appliedBadge, changesHtml, restartBanner, reviewHtml, esc };
})();

if (typeof window !== 'undefined') window.VMForm = VMForm;
