/**
 * harvester-ops : la vue des déclarations Terraform (v1.55.0).
 *
 * Demande de l'exploitant (26/09/2026) : une interface améliorée pour
 * l'onglet Terraform, où l'on puisse renommer les déclarations. Elle
 * remplace la liste et les fenêtres empilées (une par déclaration, puis une
 * par section) :
 *
 *   liste des déclarations (état de chacune)  |  la déclaration choisie :
 *                                             |  Ressources · Code · Historique
 *                                             |  Prévisualiser le plan, Appliquer...
 *
 * Chaque ressource dit si elle est déployée, à créer, à modifier ou retirée
 * (et donc détruite au prochain apply). Le plan se lit ressource par
 * ressource, réglage par réglage, avant d'appliquer ; « Appliquer ce plan »
 * applique exactement ce qui a été lu, et la console refuse si la
 * déclaration a changé depuis. Les formulaires s'ouvrent dans la vue, section
 * par section, avec un contrôle pendant la saisie.
 *
 * Données : TFDecl (déclarations gardées par la console, v1.54.0),
 * TFForm / TF_SCHEMA (formulaires), /api/tf-declarations/<id>/code|history,
 * /api/terraform/<cluster>/apply_declaration|destroy_declaration.
 */
const TFDeclView = (() => {
  const enc = encodeURIComponent;
  const tr = (k, vars) => (window.i18n ? i18n.t(k, vars) : k);
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const lang = () => { try { return localStorage.getItem('harvester_ops_language') || 'en'; } catch { return 'en'; } };
  const st = (o) => (!o ? '' : typeof o === 'string' ? o : (o[lang()] || o.en || ''));
  const TAB_KEY = 'harvester_ops_tfd_tab';

  // Un appel littéral par texte : le contrôle de parité des traductions ne
  // voit que ces formes (une clé construite par concaténation lui échappe).
  const TABS = {
    resources: () => tr('tfd.tab.resources'), code: () => tr('tfd.tab.code'), history: () => tr('tfd.tab.history'),
  };
  const TAB_TIPS = {
    resources: () => tr('tfd.t.tab.resources'), code: () => tr('tfd.t.tab.code'), history: () => tr('tfd.t.tab.history'),
  };
  const ACTIONS = {
    create: () => tr('tfd.a.create'), update: () => tr('tfd.a.update'),
    replace: () => tr('tfd.a.replace'), delete: () => tr('tfd.a.delete'),
  };
  const COUNTS = {
    create: (n) => tr('tfd.c.create', { n }), update: (n) => tr('tfd.c.update', { n }),
    replace: (n) => tr('tfd.c.replace', { n }), delete: (n) => tr('tfd.c.delete', { n }),
    noop: (n) => tr('tfd.c.noop', { n }),
  };
  // ce qui a été fait, au passé (après un apply)
  const DONE = {
    create: (n) => tr('tfd.done.create', { n }), update: (n) => tr('tfd.done.update', { n }),
    replace: (n) => tr('tfd.done.replace', { n }), delete: (n) => tr('tfd.done.delete', { n }),
  };
  const MODES = {
    plan: () => tr('tfd.mode.plan'), apply: () => tr('tfd.mode.apply'),
    destroy: () => tr('tfd.mode.destroy'), 'destroy-plan': () => tr('tfd.mode.destroyPlan'),
  };
  const STATUS = {
    done: () => tr('tfd.status.done'), error: () => tr('tfd.status.error'), running: () => tr('tfd.status.running'),
    destroyed: () => tr('tfd.status.destroyed'), cancelled: () => tr('tfd.status.cancelled'),
    interrupted: () => tr('tfd.status.interrupted'),
  };
  const STEPS = {
    preflight: () => tr('tfd.p.preflight'), adopt: () => tr('tfd.p.adopt'), init: () => tr('tfd.p.init'),
    plan: () => tr('tfd.p.plan'), apply: () => tr('tfd.p.apply'), destroy: () => tr('tfd.p.destroy'),
  };
  const text = (map, key, ...a) => (map[key] ? map[key](...a) : String(key || ''));

  let host = null;
  let cluster = '';
  let filter = '';
  let editing = null;          // { declId, resId, spec } pendant l'édition d'une ressource
  let modal = null;

  // -------------------------------------------------------------------------
  // Petits outils
  // -------------------------------------------------------------------------
  function kindLabel(kind) {
    const s = (window.TF_SCHEMA || {})[kind];
    return s ? st(s.label) : kind;
  }

  function lastPart(ref) { return String(ref || '').split('/').pop(); }

  function summaryOf(r) {
    const s = r.spec || {};
    if (r.kind === 'vm') {
      const disk = (s.disk || [])[0] || {};
      const nic = (s.network_interface || [])[0] || {};
      return [s.cpu ? tr('tfd.sum.cpu', { n: s.cpu }) : '', s.memory, lastPart(disk.image), disk.size,
              nic.network_name ? lastPart(nic.network_name) : ''].filter(Boolean).join(' · ');
    }
    if (r.kind === 'image') {
      let hostName = '';
      try { hostName = s.url ? new URL(s.url).host : ''; } catch { hostName = ''; }
      return [s.display_name, s.source_type, hostName].filter(Boolean).join(' · ');
    }
    if (r.kind === 'ssh_key') {
      const parts = String(s.public_key || '').trim().split(/\s+/);
      return parts.length > 1 ? `${parts[0]} …${parts[1].slice(-10)}${parts[2] ? ' ' + parts[2] : ''}` : '';
    }
    if (r.kind === 'raw') return r.address || '';
    return '';
  }

  function nameOf(r) {
    if (r.kind === 'raw') return (r.address || '').split('.').pop() || tr('tfd.unnamed');
    return (r.spec && r.spec.name) || tr('tfd.unnamed');
  }

  function valid(r) {
    return !window.TFForm || TFForm.validateAll(r.spec || {}, r.kind).valid;
  }

  /** Le dernier plan, s'il vaut encore pour le contenu écrit. */
  function currentPlan(d) {
    return d && d.last_plan && d.content_hash && d.last_plan.hash === d.content_hash ? d.last_plan : null;
  }

  function changedSinceApply(d) {
    return !!(d.last_applied_at && d.updated_at && d.updated_at > d.last_applied_at);
  }

  function removedAddresses(d) {
    const mine = new Set((d.resources || []).map(r => r.address).filter(Boolean));
    return (d.deployed || []).filter(a => !mine.has(a));
  }

  function declState(d) {
    if (d.last_applied_status === 'error') return { cls: 'fail', text: tr('tfd.ds.error') };
    const addrs = (d.resources || []).map(r => r.address).filter(Boolean);
    const toCreate = addrs.filter(a => !(d.deployed || []).includes(a)).length;
    const toDestroy = removedAddresses(d).length;
    if (!(d.deployed || []).length && !d.last_applied_at) return { cls: 'dim', text: tr('tfd.ds.never') };
    if (toCreate || toDestroy) return { cls: 'warn', text: tr('tfd.ds.pending', { n: toCreate + toDestroy }) };
    if (changedSinceApply(d)) return { cls: 'warn', text: tr('tfd.ds.changed') };
    return { cls: 'ok', text: tr('tfd.ds.upToDate') };
  }

  function resState(d, r) {
    if (!valid(r)) return { cls: 'fail', text: tr('tfd.st.incomplete') };
    const deployed = (d.deployed || []).includes(r.address);
    const plan = currentPlan(d);
    const ch = plan && ((plan.summary || {}).changes || []).find(c => c.address === r.address);
    if (!deployed) return { cls: 'info', text: tr('tfd.st.toCreate') };
    if (ch && ch.action === 'replace') return { cls: 'warn', text: tr('tfd.st.toReplace') };
    if (ch && ch.action === 'update') return { cls: 'warn', text: tr('tfd.st.toUpdate', { n: (ch.fields || []).length }) };
    return { cls: 'ok', text: tr('tfd.st.deployed') };
  }

  function when(iso) {
    if (!iso) return '';
    const t = typeof iso === 'number' ? new Date(iso * 1000) : new Date(iso);
    return isNaN(t) ? '' : t.toLocaleString(lang(), { dateStyle: 'short', timeStyle: 'short' });
  }

  function decls() {
    const q = filter.trim().toLowerCase();
    return (window.TFDecl ? TFDecl.list(cluster) : [])
      .filter(d => !q || d.name.toLowerCase().includes(q) || (d.description || '').toLowerCase().includes(q))
      .sort((a, b) => a.name.localeCompare(b.name));
  }

  function selected() {
    const a = window.TFDecl && TFDecl.getActive();
    return a && a.cluster === cluster ? a : null;
  }

  function getTab() { try { return localStorage.getItem(TAB_KEY) || 'resources'; } catch { return 'resources'; } }
  function setTab(t) { try { localStorage.setItem(TAB_KEY, t); } catch {} }

  // -------------------------------------------------------------------------
  // Rendu
  // -------------------------------------------------------------------------
  function render() {
    if (!host) return;
    const active = selected();
    const list = decls();
    host.innerHTML = `
      <div class="tfd">
        <aside class="tfd-list" aria-label="${esc(tr('tfd.listLabel'))}">
          <div class="tfd-list-head">
            <input type="search" class="tfd-filter tip" value="${esc(filter)}"
                   placeholder="${esc(tr('tfd.filter'))}" aria-label="${esc(tr('tfd.filter'))}"
                   data-tip="${esc(tr('tfd.t.filter'))}">
            <button type="button" class="btn btn-primary btn-sm tfd-new tip" aria-label="${esc(tr('tfd.new'))}"
                    data-tip="${esc(tr('tfd.t.new'))}">${Icons.svg('add', { size: 14 })}</button>
          </div>
          <div class="tfd-cards">${list.length ? list.map(d => cardHtml(d, active && d.id === active.id)).join('')
            : `<p class="tfd-empty">${esc(filter ? tr('tfd.noMatch') : tr('tfd.none'))}</p>`}</div>
          <p class="tfd-foot">${esc(tr('tfd.keptByConsole'))}</p>
        </aside>
        <section class="tfd-main">${active ? mainHtml(active) : `<div class="tfd-placeholder">
            <p>${esc(list.length ? tr('tfd.pick') : tr('tfd.startHint'))}</p>
            <button type="button" class="btn btn-primary btn-sm tfd-new tip" data-tip="${esc(tr('tfd.t.new'))}">${Icons.svg('add', { size: 14 })} ${esc(tr('tfd.new'))}</button>
          </div>`}</section>
      </div>`;
    applyTips(host);
    if (active && editing && editing.declId === active.id) mountEditor(active);
    else if (active && getTab() === 'code') loadCode(active);
    else if (active && getTab() === 'history') loadHistory(active);
  }

  function cardHtml(d, isActive) {
    const s = declState(d);
    const bits = [tr('tfd.nRes', { n: (d.resources || []).length })];
    if (d.last_applied_at) {
      bits.push(d.last_applied_by ? tr('tfd.appliedOn', { when: when(d.last_applied_at), by: d.last_applied_by })
                                  : tr('tfd.appliedOnAt', { when: when(d.last_applied_at) }));
    }
    return `
      <button type="button" class="tfd-card ${isActive ? 'is-active' : ''}" data-decl="${esc(d.id)}">
        <span class="tfd-card-top"><strong>${esc(d.name)}</strong><span class="badge ${esc(s.cls)}">${esc(s.text)}</span></span>
        ${d.description ? `<span class="tfd-card-desc">${esc(d.description)}</span>` : ''}
        <span class="tfd-card-meta">${esc(bits.join(' · '))}</span>
      </button>`;
  }

  function mainHtml(d) {
    const tab = editing ? 'edit' : getTab();
    const deployed = (d.deployed || []).length;
    const plan = currentPlan(d);
    const planChanges = plan ? ((plan.summary || {}).changes || []).length : 0;
    const canApply = !!plan && planChanges > 0 && !(d.incomplete || []).length;
    const applyTip = !plan ? tr('tfd.t.applyNeedsPlan') : planChanges ? tr('tfd.t.apply') : tr('tfd.t.applyNothing');
    return `
      <header class="tfd-head">
        <div class="tfd-title">
          <h3 class="tfd-name">${esc(d.name)}</h3>
          <button type="button" class="btn-icon-sm tfd-rename tip" aria-label="${esc(tr('tf.tip.renameDecl'))}"
                  data-tip="${esc(tr('tf.tip.renameDecl'))}">${Icons.svg('edit', { size: 14 })}</button>
        </div>
        <input type="text" class="tfd-desc tip" value="${esc(d.description || '')}" maxlength="500"
               placeholder="${esc(tr('tfd.descPh'))}" aria-label="${esc(tr('tfd.descPh'))}"
               data-tip="${esc(tr('tfd.t.desc'))}">
        <div class="tfd-badges">
          <span class="badge info tip" data-tip="${esc(tr('tfd.t.ownState'))}">${esc(tr('tfd.ownState'))}</span>
          ${deployed ? `<span class="badge ok tip" data-tip="${esc((d.deployed || []).join(', '))}">${esc(tr('tfd.nDeployed', { n: deployed }))}</span>` : ''}
          ${d.last_applied_at ? `<span class="tfd-meta">${esc(d.last_applied_by
            ? tr('tfd.lastApply', { status: text(STATUS, d.last_applied_status || 'done'), when: when(d.last_applied_at), by: d.last_applied_by })
            : tr('tfd.lastApplyAt', { status: text(STATUS, d.last_applied_status || 'done'), when: when(d.last_applied_at) }))}</span>` : ''}
        </div>
        ${changedSinceApply(d) && !plan ? `<p class="tfd-notice">${esc(tr('tfd.changedNotice'))}</p>` : ''}
      </header>
      ${tab === 'edit' ? '<div class="tfd-body" data-x="edit"></div>' : `
      <nav class="sub-tabs sub-tabs-inline tfd-tabs" role="tablist">
        ${['resources', 'code', 'history'].map(t => `<button type="button" role="tab" class="sub-tab tip ${t === tab ? 'active' : ''}"
             data-tfd-tab="${t}" aria-selected="${t === tab}" data-tip="${esc(text(TAB_TIPS, t))}">${esc(text(TABS, t))}</button>`).join('')}
      </nav>
      <div class="tfd-body" data-x="${esc(tab)}">${tab === 'resources' ? resourcesHtml(d) : `<p class="hint">${esc(tr('common.loading'))}</p>`}</div>`}
      <footer class="apply-bar tfd-actions">
        <button type="button" class="btn btn-primary btn-sm tfd-plan tip" ${(d.resources || []).length || deployed ? '' : 'disabled'}
                data-tip="${esc(tr('tfd.t.plan'))}">${Icons.svg('preview', { size: 14 })} ${esc(tr('tfd.plan'))}</button>
        <button type="button" class="btn btn-sm tfd-apply tip" ${canApply ? '' : 'disabled'}
                data-tip="${esc(applyTip)}">${Icons.svg('play', { size: 14 })} ${esc(tr('tfd.apply'))}</button>
        <span class="tfd-hint">${esc(plan ? tr('tfd.planReady', { when: when(plan.at), n: planChanges }) : tr('tfd.applyHint'))}</span>
        <button type="button" class="btn btn-sm tfd-export tip" data-tip="${esc(tr('tfd.t.export'))}">${Icons.svg('download', { size: 14 })} ${esc(tr('tfd.export'))}</button>
        <button type="button" class="btn btn-sm btn-danger tfd-destroy tip" ${deployed ? '' : 'disabled'}
                data-tip="${esc(tr('tfd.t.destroy'))}">${Icons.svg('destroy', { size: 14 })} ${esc(tr('tfd.destroy'))}</button>
        <button type="button" class="btn btn-sm tfd-delete tip" ${deployed ? 'disabled' : ''}
                data-tip="${esc(deployed ? tr('tfd.t.deleteDeployed') : tr('tfd.t.delete'))}">${Icons.svg('delete', { size: 14 })} ${esc(tr('tfd.delete'))}</button>
      </footer>`;
  }

  function resourcesHtml(d) {
    const cards = (d.resources || []).map(r => {
      const s = resState(d, r);
      return `
        <article class="tfd-res tfd-res--${esc(s.cls)}" data-res="${esc(r.id)}">
          <div class="tfd-res-top"><strong>${esc(kindLabel(r.kind))} ${esc(nameOf(r))}</strong><span class="badge ${esc(s.cls)}">${esc(s.text)}</span></div>
          <span class="tfd-res-sum">${esc(summaryOf(r))}</span>
          ${r.address ? `<code class="tfd-addr">${esc(r.address)}</code>` : ''}
          <div class="tfd-res-actions">
            <button type="button" class="btn btn-sm tfd-edit-res tip" data-res="${esc(r.id)}" data-tip="${esc(tr('tfd.t.edit'))}">${Icons.svg('edit', { size: 13 })} ${esc(tr('tfd.edit'))}</button>
            <button type="button" class="btn btn-sm btn-danger tfd-remove tip" data-res="${esc(r.id)}" data-tip="${esc(tr('tfd.t.remove'))}">${Icons.svg('delete', { size: 13 })} ${esc(tr('tfd.remove'))}</button>
          </div>
        </article>`;
    });
    const ghosts = removedAddresses(d).map(a => `
        <article class="tfd-res tfd-res--fail">
          <div class="tfd-res-top"><strong>${esc(a.split('.').pop())}</strong><span class="badge fail">${esc(tr('tfd.st.removed'))}</span></div>
          <span class="tfd-res-sum">${esc(tr('tfd.removedHint'))}</span>
          <code class="tfd-addr">${esc(a)}</code>
        </article>`);
    return `
      <div class="tfd-grid">${cards.concat(ghosts).join('') || `<p class="tfd-empty">${esc(tr('tfd.noResource'))}</p>`}</div>
      <div class="tfd-add">
        <span>${esc(tr('tfd.add'))}</span>
        ${['vm', 'image', 'ssh_key', 'raw'].map(k => `<button type="button" class="btn btn-sm tfd-add-kind tip" data-kind="${k}"
            data-tip="${esc(tr('tfd.t.add', { kind: kindLabel(k) }))}">${Icons.svg('add', { size: 12 })} ${esc(kindLabel(k))}</button>`).join('')}
      </div>`;
  }

  function applyTips(root) {
    root.querySelectorAll('[data-tip-i18n]').forEach(el => el.setAttribute('data-tip', tr(el.getAttribute('data-tip-i18n'))));
  }

  // -------------------------------------------------------------------------
  // Code et historique
  // -------------------------------------------------------------------------
  async function fetchCode(d) {
    const r = await fetch(`/api/tf-declarations/${enc(d.id)}/code`);
    const out = await r.json();
    if (!r.ok) throw new Error(out.error || `HTTP ${r.status}`);
    return out;
  }

  async function loadCode(d) {
    const box = host.querySelector('.tfd-body[data-x="code"]');
    if (!box) return;
    try {
      await TFDecl.flush(d.id);
      const out = await fetchCode(d);
      box.innerHTML = `${(out.errors || []).length ? `<p class="tfd-notice">${esc(tr('tfd.codeIncomplete', { n: out.errors.length }))}</p>` : ''}
        ${out.files.map(f => `<details class="tfd-file" ${f.name === '_providers.tf' ? '' : 'open'}>
          <summary><code>${esc(f.name)}</code>${f.address ? ` <span class="tfd-meta">${esc(f.address)}</span>` : ''}</summary>
          <pre class="capi-yaml">${esc(f.content)}</pre></details>`).join('')}`;
    } catch (e) {
      box.innerHTML = `<p class="tfd-notice">${esc(tr('tfd.error', { msg: e.message }))}</p>`;
    }
  }

  async function exportCode(d) {
    try {
      await TFDecl.flush(d.id);
      const out = await fetchCode(d);
      const text = out.files.map(f => `# --- ${f.name}\n${f.content.trimEnd()}\n`).join('\n');
      const a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob([text], { type: 'text/plain' }));
      a.download = `${d.name.replace(/[^A-Za-z0-9._-]+/g, '-') || 'declaration'}.tf`;
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    } catch (e) {
      alert(tr('tfd.error', { msg: e.message }));
    }
  }

  async function loadHistory(d) {
    const box = host.querySelector('.tfd-body[data-x="history"]');
    if (!box) return;
    try {
      const r = await fetch(`/api/tf-declarations/${enc(d.id)}/history`);
      const out = await r.json();
      if (!r.ok) throw new Error(out.error || `HTTP ${r.status}`);
      const runs = out.runs || [];
      box.innerHTML = runs.length ? `
        <table class="data-table tfd-history">
          <thead><tr><th>${esc(tr('tfd.h.when'))}</th><th>${esc(tr('tfd.h.what'))}</th><th>${esc(tr('tfd.h.by'))}</th><th>${esc(tr('tfd.h.result'))}</th></tr></thead>
          <tbody>${runs.map(x => `<tr>
            <td>${esc(when(x.started_at))}</td>
            <td>${esc(text(MODES, x.mode || 'apply'))}</td>
            <td>${esc(x.by || '')}</td>
            <td><span class="badge ${x.status === 'done' ? 'ok' : x.status === 'error' ? 'fail' : 'warn'}">${esc(text(STATUS, x.status))}</span>
              ${x.counts ? `<span class="tfd-meta">${esc(countsText(x.counts))}</span>` : ''}
              ${x.error_summary ? `<span class="tfd-meta">${esc(x.error_summary)}</span>` : ''}</td></tr>`).join('')}</tbody>
        </table>` : `<p class="tfd-empty">${esc(tr('tfd.noHistory'))}</p>`;
    } catch (e) {
      box.innerHTML = `<p class="tfd-notice">${esc(tr('tfd.error', { msg: e.message }))}</p>`;
    }
  }

  function doneText(c) {
    return ['create', 'update', 'replace', 'delete'].filter(k => c[k]).map(k => DONE[k](c[k])).join(', ')
      || tr('tfd.c.none');
  }

  function countsText(c) {
    return ['create', 'update', 'replace', 'delete'].filter(k => c[k]).map(k => COUNTS[k](c[k])).join(', ')
      || tr('tfd.c.none');
  }

  // -------------------------------------------------------------------------
  // Édition d'une ressource, dans la vue
  // -------------------------------------------------------------------------
  function mountEditor(d) {
    const box = host.querySelector('.tfd-body[data-x="edit"]');
    const r = (d.resources || []).find(x => x.id === editing.resId);
    if (!box || !r) { editing = null; render(); return; }
    const schema = (window.TF_SCHEMA || {})[r.kind] || {};
    const sections = schema.sections || [{ id: 'specs', label: { en: '' } }];
    const spec = editing.spec || JSON.parse(JSON.stringify(r.spec || {}));
    editing.spec = spec;
    box.innerHTML = `
      <div class="tfd-edit">
        <aside class="tfd-edit-list">
          <button type="button" class="btn btn-sm tfd-back tip" data-tip="${esc(tr('tfd.t.back'))}">${Icons.svg('arrowLeft', { size: 13 })} ${esc(tr('tfd.back'))}</button>
          ${(d.resources || []).map(x => `<button type="button" class="tfd-edit-item ${x.id === r.id ? 'is-active' : ''} tip"
              data-res="${esc(x.id)}" data-tip="${esc(tr('tfd.t.switchRes'))}">${esc(kindLabel(x.kind))} ${esc(nameOf(x))}</button>`).join('')}
        </aside>
        <div class="tfd-edit-main">
          <div class="tfd-edit-head"><strong>${esc(kindLabel(r.kind))} ${esc(nameOf(r))}</strong>
            ${r.address ? `<code class="tfd-addr">${esc(r.address)}</code>` : ''}</div>
          ${sections.map(sec => `<fieldset class="tfd-sec" data-sec="${esc(sec.id)}">
              <legend>${esc(st(sec.label) || kindLabel(r.kind))}</legend>
              ${TFForm.render(r.kind, cluster, spec, { sectionId: sec.id, hideHeader: true })}
            </fieldset>`).join('')}
        </div>
        <aside class="tfd-edit-check" aria-live="polite">
          <strong>${esc(tr('tfd.checkTitle'))}</strong>
          <div class="tfd-check" data-x="check"></div>
          <button type="button" class="btn btn-primary btn-sm tfd-save tip" data-tip="${esc(tr('tfd.t.save'))}">${Icons.svg('save', { size: 13 })} ${esc(tr('tfd.save'))}</button>
          <button type="button" class="btn btn-sm tfd-cancel tip" data-tip="${esc(tr('tfd.t.cancel'))}">${esc(tr('tfd.cancel'))}</button>
          <p class="tfd-meta">${esc(tr('tfd.saveHint'))}</p>
        </aside>
      </div>`;
    box.querySelectorAll('.tfd-sec .tf-form').forEach(root => TFForm.wire(root, r.kind, cluster));
    const recheck = () => {
      editing.spec = readEditor(box, r, editing.spec);
      const v = TFForm.validateAll(editing.spec, r.kind);
      box.querySelector('[data-x="check"]').innerHTML = sections.map(sec => {
        const s = v.sections[sec.id] || { valid: true, missing: [] };
        return `<div class="tfd-check-row ${s.valid ? 'ok' : 'fail'}">${Icons.svg(s.valid ? 'ok' : 'warn', { size: 12 })}
          ${esc(st(sec.label) || sec.id)}${s.valid ? '' : ` : ${esc(tr('tfd.missing', { fields: (s.missing || []).join(', ') }))}`}</div>`;
      }).join('') + ((d.deployed || []).includes(r.address)
        ? `<div class="tfd-check-row info">${Icons.svg('info', { size: 12 })} ${esc(tr('tfd.deployedEditHint'))}</div>` : '');
    };
    box.addEventListener('input', recheck);
    box.addEventListener('change', recheck);
    recheck();
  }

  function readEditor(box, r, base) {
    let merged = Object.assign({}, base);
    box.querySelectorAll('.tfd-sec .tf-form').forEach(root => {
      const partial = TFForm.read(root, r.kind) || {};
      merged = Object.assign(merged, partial);
      Object.keys(partial).forEach(k => { if (Array.isArray(partial[k])) merged[k] = partial[k]; });
    });
    return merged;
  }

  function saveEditor(d) {
    const box = host.querySelector('.tfd-body[data-x="edit"]');
    const r = (d.resources || []).find(x => x.id === editing.resId);
    if (!box || !r) return;
    const spec = readEditor(box, r, editing.spec || r.spec || {});
    TFDecl.replaceResourceSpec(d.id, r.id, spec);
    editing = null;
    render();
  }

  // -------------------------------------------------------------------------
  // Plan, application, destruction : une fenêtre qui se lit avant d'agir
  // -------------------------------------------------------------------------
  function closeModal() { if (modal) modal.remove(); modal = null; }

  function openModal(title, subtitle) {
    closeModal();
    modal = document.createElement('div');
    modal.className = 'modal-overlay active tfd-modal';
    modal.innerHTML = `
      <div class="modal tfd-plan-modal" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <div class="modal-header"><div><h3>${Icons.svg('terraform')} ${esc(title)}</h3>
          <div class="modal-subtitle" data-x="sub">${esc(subtitle || '')}</div></div>
          <button type="button" class="btn-close tip" data-x="close" aria-label="${esc(tr('common.close'))}" data-tip="${esc(tr('common.close'))}">×</button></div>
        <div class="modal-body" data-x="body"></div>
        <div class="modal-footer apply-bar" data-x="foot" style="margin:0;"></div>
      </div>`;
    document.body.appendChild(modal);
    modal.addEventListener('click', (e) => {
      if (e.target.closest('[data-x="close"]') || e.target === modal || e.target.closest('[data-x="dismiss"]')) closeModal();
    });
    return modal;
  }

  function q(x) { return modal && modal.querySelector(`[data-x="${x}"]`); }

  function progress(text) {
    const b = q('body');
    if (!b) return;
    let line = b.querySelector('.tfd-progress');
    if (!line) {
      b.innerHTML = `<div class="tfd-progress"><span class="spinner-inline"></span> <span data-x="line"></span></div>
        <details class="tfd-raw"><summary>${esc(tr('tfd.raw'))}</summary><pre class="capi-yaml" data-x="raw"></pre></details>`;
      line = b.querySelector('.tfd-progress');
    }
    q('line').textContent = text;
  }

  /** Suit une action jusqu'à sa fin ; rend l'action enregistrée. */
  function follow(actionId) {
    return new Promise((resolve) => {
      const raw = [];
      const finish = async () => {
        let a = {};
        try { a = await fetch(`/api/action/${enc(actionId)}`).then(r => r.json()); } catch { a = {}; }
        a._raw = raw;
        resolve(a);
      };
      if (!window.SSEReconnect) { setTimeout(finish, 1500); return; }
      const es = SSEReconnect.connect(`/api/stream/${enc(actionId)}`, {
        on: {
          step: (e) => {
            try {
              const s = JSON.parse(e.data);
              if (s.message) progress(stepText(s));
            } catch { /* ligne illisible */ }
          },
          log: (e) => {
            try {
              raw.push(JSON.parse(e.data).message || '');
              const pre = q('raw');
              if (pre) pre.textContent = raw.join('\n');
            } catch { /* ligne illisible */ }
          },
          end: () => { es.close(); finish(); },
        },
      });
    });
  }

  function stepText(s) {
    if (s.status === 'error') return tr('tfd.error', { msg: s.message });
    return STEPS[s.step_id] ? `${STEPS[s.step_id]()}${s.step_id === 'adopt' ? ' : ' + s.message : ''}` : s.message;
  }

  async function startAction(url, body) {
    const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const out = await r.json();
    if (!r.ok) {
      const detail = (out.errors || []).map(e => `#${e.index + 1} ${e.kind || ''} ${e.name || ''}: ${e.error}`).join('; ');
      throw new Error((out.hint || out.error || `HTTP ${r.status}`) + (detail ? ` (${detail})` : ''));
    }
    return out;
  }

  async function runPlan(d) {
    openModal(tr('tfd.planTitle', { name: d.name, cluster }), tr('tfd.planSub', { name: d.name }));
    progress(tr('tfd.p.start'));
    try {
      await TFDecl.flush(d.id);
      const out = await startAction(`/api/terraform/${enc(cluster)}/apply_declaration`,
                                    { declaration: { id: d.id }, dry_run: true });
      const a = await follow(out.action_id);
      await TFDecl.refresh(d.id);
      if (a.status !== 'done') return showFailure(a);
      showPlan(TFDecl.get(d.id) || d, (a.result || {}).plan, (a.result || {}).plan_hash, a._raw);
    } catch (e) {
      showFailure({ error_summary: e.message });
    }
  }

  function showFailure(a) {
    const b = q('body');
    if (!b) return;
    const raw = b.querySelector('.tfd-raw');
    b.innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(tr('tfd.failed', { msg: a.error_summary || a.status || '?' }))}</div></div>`;
    if (raw) b.appendChild(raw);
    q('foot').innerHTML = `<button type="button" class="btn btn-secondary btn-sm" data-x="dismiss">${esc(tr('common.close'))}</button>`;
  }

  function fieldValue(v) {
    if (v === null || v === undefined || v === '') return '';
    const s = typeof v === 'object' ? JSON.stringify(v) : String(v);
    return s.length > 160 ? s.slice(0, 157) + '...' : s;
  }

  function changeHtml(d, c) {
    const r = (d.resources || []).find(x => x.address === c.address);
    const title = r ? `${kindLabel(r.kind)} ${nameOf(r)}` : c.address;
    const icon = { create: 'add', update: 'edit', replace: 'refresh', delete: 'delete' }[c.action] || 'info';
    const rows = (c.fields || []).map(f => c.action === 'create'
      ? `<tr><td>${esc(f.path)}</td><td colspan="2">${esc(fieldValue(f.after))}</td></tr>`
      : `<tr><td>${esc(f.path)}</td><td class="tfd-before">${esc(fieldValue(f.before))}</td><td class="tfd-after">${esc(fieldValue(f.after))}</td></tr>`).join('');
    return `
      <section class="tfd-change tfd-change--${esc(c.action)}" data-address="${esc(c.address)}">
        <div class="tfd-change-head">${Icons.svg(icon, { size: 14 })} <strong>${esc(title)}</strong>
          <span>${esc(text(ACTIONS, c.action))}</span><code class="tfd-addr">${esc(c.address)}</code></div>
        ${rows ? `<table class="tfd-fields"><thead><tr><th>${esc(tr('tfd.f.setting'))}</th>
          ${c.action === 'create' ? `<th colspan="2">${esc(tr('tfd.f.value'))}</th>`
            : `<th>${esc(tr('tfd.f.before'))}</th><th>${esc(tr('tfd.f.after'))}</th>`}</tr></thead><tbody>${rows}</tbody></table>` : ''}
        ${c.action === 'replace' ? `<p class="tfd-warn">${esc(tr('tfd.replaceWarn', { paths: (c.replace_paths || []).join(', ') || '?' }))}</p>` : ''}
        ${c.action === 'delete' ? `<p class="tfd-warn">${esc(tr('tfd.deleteWarn'))}</p>` : ''}
      </section>`;
  }

  function showPlan(d, summary, hash, raw) {
    const b = q('body');
    if (!b) return;
    summary = summary || { counts: {}, changes: [] };
    const c = summary.counts || {};
    const changes = summary.changes || [];
    b.innerHTML = `
      <div class="tfd-counts">
        ${[['create', 'ok'], ['update', 'warn'], ['replace', 'warn'], ['delete', 'fail'], ['noop', 'dim']].map(([k, cls]) =>
          `<span class="badge ${cls}" data-count="${k}">${esc(COUNTS[k](c[k] || 0))}</span>`).join('')}
      </div>
      ${changes.length ? changes.map(ch => changeHtml(d, ch)).join('')
        : `<div class="sto-finding sev-info"><div class="sto-finding-title">${esc(tr('tfd.nothing'))}</div></div>`}
      <details class="tfd-raw"><summary>${esc(tr('tfd.raw'))}</summary><pre class="capi-yaml">${esc((raw || []).join('\n'))}</pre></details>`;
    const f = q('foot');
    f.innerHTML = `
      <button type="button" class="btn btn-primary btn-sm tip" data-x="apply" ${changes.length && hash ? '' : 'disabled'}
              data-tip="${esc(tr('tfd.t.applyPlan'))}">${Icons.svg('play', { size: 13 })} ${esc(tr('tfd.applyPlan'))}</button>
      <button type="button" class="btn btn-secondary btn-sm" data-x="dismiss">${esc(tr('common.close'))}</button>
      <span class="tfd-hint">${esc(tr('tfd.planOutdatedHint'))}</span>`;
    f.querySelector('[data-x="apply"]').addEventListener('click', () => runApply(d, hash));
  }

  async function runApply(d, hash) {
    if (q('sub')) q('sub').textContent = tr('tfd.applySub', { name: d.name });
    q('foot').innerHTML = '';
    progress(tr('tfd.p.start'));
    try {
      const out = await startAction(`/api/terraform/${enc(cluster)}/apply_declaration`,
                                    { declaration: { id: d.id }, dry_run: false, plan_hash: hash });
      const a = await follow(out.action_id);
      await TFDecl.refresh(d.id);
      if (a.status !== 'done') return showFailure(a);
      q('body').innerHTML = `<div class="sto-finding sev-info"><div class="sto-finding-title">${Icons.svg('ok', { size: 13 })}
        ${esc(tr('tfd.applied', { name: d.name, what: doneText(((a.result || {}).plan || {}).counts || {}) }))}</div></div>`;
      q('foot').innerHTML = `<button type="button" class="btn btn-secondary btn-sm" data-x="dismiss">${esc(tr('common.close'))}</button>`;
    } catch (e) {
      showFailure({ error_summary: e.message });
    }
  }

  async function reviewLastPlan(d) {
    const plan = currentPlan(d);
    if (!plan) return;
    openModal(tr('tfd.planTitle', { name: d.name, cluster }),
              plan.by ? tr('tfd.reviewSub', { when: when(plan.at), by: plan.by }) : tr('tfd.reviewSubAt', { when: when(plan.at) }));
    showPlan(d, plan.summary, plan.hash, []);
  }

  async function runDestroy(d) {
    const ok = window.TF && TF.confirmDestructive ? await TF.confirmDestructive({
      title: tr('tfd.destroyTitle', { name: d.name }),
      message: tr('tfd.destroyMsg', { cluster }),
      detail: (d.deployed || []).join('\n'),
      requiredText: d.name,
      confirmLabel: tr('tfd.destroyGo', { name: d.name }),
    }) : confirm(tr('tfd.destroyTitle', { name: d.name }));
    if (!ok) return;
    openModal(tr('tfd.destroyTitle', { name: d.name }), cluster);
    progress(tr('tfd.p.start'));
    try {
      const out = await startAction(`/api/terraform/${enc(cluster)}/destroy_declaration`,
                                    { declaration: { id: d.id }, dry_run: false });
      const a = await follow(out.action_id);
      await TFDecl.refresh(d.id);
      if (a.status !== 'done') return showFailure(a);
      q('body').innerHTML = `<div class="sto-finding sev-info"><div class="sto-finding-title">${esc(tr('tfd.destroyed', { name: d.name }))}</div></div>`;
      q('foot').innerHTML = `<button type="button" class="btn btn-secondary btn-sm" data-x="dismiss">${esc(tr('common.close'))}</button>`;
    } catch (e) {
      showFailure({ error_summary: e.message });
    }
  }

  // -------------------------------------------------------------------------
  // Création, renommage, suppression
  // -------------------------------------------------------------------------
  function openCreate() {
    openModal(tr('tfd.newTitle'), cluster);
    q('body').innerHTML = `
      <div class="tf-form">
        <label class="tf-field"><span class="tf-label">${esc(tr('tfd.f.name'))}</span>
          <input type="text" data-x="name" maxlength="80" class="tip" data-tip="${esc(tr('tfd.t.name'))}" autocomplete="off"></label>
        <label class="tf-field"><span class="tf-label">${esc(tr('tfd.f.desc'))}</span>
          <input type="text" data-x="desc" maxlength="500" class="tip" data-tip="${esc(tr('tfd.t.desc'))}"></label>
        <p class="tfd-meta" data-x="err"></p>
      </div>`;
    q('foot').innerHTML = `
      <button type="button" class="btn btn-primary btn-sm tip" data-x="create" data-tip="${esc(tr('tfd.t.create'))}">${Icons.svg('add', { size: 13 })} ${esc(tr('tfd.create'))}</button>
      <button type="button" class="btn btn-secondary btn-sm" data-x="dismiss">${esc(tr('common.cancel'))}</button>`;
    const name = q('name');
    name.focus();
    const go = async () => {
      const v = name.value.trim();
      if (!v) { q('err').textContent = tr('tfd.needName'); return; }
      try {
        const d = await TFDecl.createAsync(v, cluster, q('desc').value.trim());
        closeModal();
        TFDecl.setActive(d.id);
        setTab('resources');
        render();
      } catch (e) {
        q('err').textContent = e.code === 'name-taken' ? tr('tf.err.nameTaken', { name: v }) : tr('tfd.error', { msg: e.message });
      }
    };
    q('create').addEventListener('click', go);
    name.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
  }

  function startRename(d) {
    const h = host.querySelector('.tfd-name');
    if (!h) return;
    const input = document.createElement('input');
    input.className = 'tfd-name-input tip';
    input.value = d.name;
    input.maxLength = 80;
    input.setAttribute('aria-label', tr('tf.tip.renameDecl'));
    input.dataset.tip = tr('tf.tip.renameInput');
    h.replaceWith(input);
    input.focus();
    input.select();
    let done = false;
    const finish = (save) => {
      if (done) return;
      done = true;
      const v = input.value.trim();
      input.replaceWith(h);
      if (!save || !v || v === d.name) return;
      h.textContent = v;
      TFDecl.renameAsync(d.id, v).catch(err => {
        h.textContent = d.name;
        alert(err.code === 'name-taken' ? tr('tf.err.nameTaken', { name: v }) : tr('tfd.error', { msg: err.message }));
      });
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); finish(true); }
      if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    });
    input.addEventListener('blur', () => finish(true));
  }

  async function removeDecl(d) {
    if (!confirm(tr('tf.confirmDeleteDecl', { name: d.name }))) return;
    try {
      await TFDecl.removeAsync(d.id);
      render();
    } catch (e) {
      alert(e.code === 'deployed' ? tr('tf.err.deployed', { resources: ((e.data || {}).deployed || []).join(', ') })
                                  : tr('tfd.error', { msg: e.message }));
    }
  }

  // -------------------------------------------------------------------------
  // Événements (posés une fois sur l'hôte)
  // -------------------------------------------------------------------------
  function wire() {
    host.dataset.wired = '1';
    host.addEventListener('click', (e) => {
      const d = selected();
      let el;
      if ((el = e.target.closest('.tfd-card'))) { editing = null; TFDecl.setActive(el.dataset.decl); render(); return; }
      if (e.target.closest('.tfd-new')) { openCreate(); return; }
      if ((el = e.target.closest('[data-tfd-tab]'))) { setTab(el.dataset.tfdTab); render(); return; }
      if (!d) return;
      if (e.target.closest('.tfd-rename')) { startRename(d); return; }
      if ((el = e.target.closest('.tfd-add-kind'))) {
        const r = TFDecl.addResource(d.id, el.dataset.kind);
        if (r) { editing = { declId: d.id, resId: r.id, spec: null }; render(); }
        return;
      }
      if ((el = e.target.closest('.tfd-edit-res'))) { editing = { declId: d.id, resId: el.dataset.res, spec: null }; render(); return; }
      if ((el = e.target.closest('.tfd-edit-item'))) {
        const box = host.querySelector('.tfd-body[data-x="edit"]');
        const cur = (d.resources || []).find(x => x.id === editing.resId);
        if (box && cur) TFDecl.replaceResourceSpec(d.id, cur.id, readEditor(box, cur, editing.spec || cur.spec));
        editing = { declId: d.id, resId: el.dataset.res, spec: null };
        render();
        return;
      }
      if (e.target.closest('.tfd-back') || e.target.closest('.tfd-cancel')) { editing = null; render(); return; }
      if (e.target.closest('.tfd-save')) { saveEditor(d); return; }
      if ((el = e.target.closest('.tfd-remove'))) {
        const r = (d.resources || []).find(x => x.id === el.dataset.res);
        if (!r) return;
        const deployed = (d.deployed || []).includes(r.address);
        if (!confirm(deployed ? tr('tfd.confirmRemoveDeployed', { name: nameOf(r) }) : tr('tfd.confirmRemove', { name: nameOf(r) }))) return;
        TFDecl.removeResource(d.id, r.id);
        render();
        return;
      }
      if (e.target.closest('.tfd-plan')) { runPlan(d); return; }
      if (e.target.closest('.tfd-apply')) { reviewLastPlan(d); return; }
      if (e.target.closest('.tfd-export')) { exportCode(d); return; }
      if (e.target.closest('.tfd-destroy')) { runDestroy(d); return; }
      if (e.target.closest('.tfd-delete')) { removeDecl(d); return; }
    });
    host.addEventListener('input', (e) => {
      if (e.target.classList.contains('tfd-filter')) {
        filter = e.target.value;
        const pos = e.target.selectionStart;
        render();
        const f = host.querySelector('.tfd-filter');
        if (f) { f.focus(); f.setSelectionRange(pos, pos); }
      }
    });
    host.addEventListener('change', (e) => {
      const d = selected();
      if (d && e.target.classList.contains('tfd-desc')) TFDecl.describe(d.id, e.target.value.trim());
    });
    if (window.TFDecl) {
      // quelqu'un d'autre a modifié la déclaration entre-temps : sa version
      // est affichée, on le dit (le magasin l'a déjà rechargée)
      TFDecl.onError((err) => { if (err && err.code === 'conflict') alert(tr('tf.err.conflict')); });
      TFDecl.onChange(() => {
        if (!host || !host.isConnected) return;
        // pas pendant une saisie dans la vue : le champ disparaîtrait
        const a = document.activeElement;
        if (a && host.contains(a) && a.matches('input, textarea, select')) return;
        if (editing) return;
        render();
      });
    }
  }

  function mount(el, clusterName) {
    if (!el) return;
    if (host !== el) { host = el; if (!el.dataset.wired) wire(); }
    if (cluster !== clusterName) { editing = null; filter = ''; }
    cluster = clusterName || '';
    const sel = selected();
    if (!sel) {
      const first = decls()[0];
      if (first && window.TFDecl) TFDecl.setActive(first.id);
    }
    render();
    if (window.TFDecl) TFDecl.ready.then(() => { if (host === el) render(); });
  }

  /** Ouvre une déclaration (depuis la vue en direct), éventuellement sur
   *  l'édition d'une de ses ressources. */
  function select(declId, resId) {
    if (!window.TFDecl) return;
    TFDecl.setActive(declId);
    editing = resId ? { declId, resId, spec: null } : null;
    setTab('resources');
    render();
  }

  return { mount, select, render, _internals: { declState, resState, currentPlan, summaryOf } };
})();

if (typeof window !== 'undefined') window.TFDeclView = TFDeclView;
