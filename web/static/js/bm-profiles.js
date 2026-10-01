/**
 * harvester-ops : profils d'installation multi-nœuds (v1.80.0).
 *
 * Dans l'onglet Bare-metal, sous la découverte :
 *   - la liste des profils (créer, modifier, supprimer) ;
 *   - l'éditeur d'un profil : champs de la fenêtre d'installation en YAML,
 *     variables `{{nom}}` permises partout, YAML avancé, départ possible
 *     d'un fichier de configuration ;
 *   - la fenêtre « Installer une série » : un tableau de machines (collage
 *     CSV possible), l'aperçu de chaque ligne et le lancement.
 *
 * Aucun secret n'est rangé : jeton, mot de passe de l'OS et mots de passe
 * des BMC sont saisis au lancement et ne vivent que dans la fenêtre.
 */
const BMProfiles = (() => {
  const tr = (k, params) => (window.i18n ? i18n.t(k, params) : k);
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const icon = (n, o) => (window.Icons ? Icons.svg(n, o) : '');
  const BUILTINS = ['hostname', 'ip', 'mgmt_mac'];
  const NEW_FIELDS = [
    'hostname: "{{hostname}}"',
    'device: /dev/sda',
    'mgmt_interfaces:',
    '- "{{mgmt_mac}}"',
    'bond_mode: active-backup',
    'method: static',
    'ip: "{{ip}}"',
    'subnet_mask: 255.255.255.0',
    'gateway: 192.0.2.1',
    'dns: 192.0.2.53',
    'ntp: pool.ntp.org',
    '',
  ].join('\n');

  async function api(method, url, body) {
    const r = await fetch(url, {
      method, headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    const d = await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, d };
  }

  // Refus du serveur : [où, raison] par ligne, jamais une valeur
  function refusal(d, status) {
    const pairs = Array.isArray(d.errors) ? d.errors
      : (d.fields || []).map(f => [f, (d.reasons || {})[f] || '']);
    const items = pairs.map(([w, r]) => `<li><code>${esc(w)}</code>${r ? ' : ' + esc(r) : ''}</li>`).join('');
    return `<div class="bm-refusal">${icon('fail', { size: 14 })} ${esc(d.error || status)}
      ${items ? `<ul class="bm-paths">${items}</ul>` : ''}</div>`;
  }

  // -------------------------------------------------------------------------
  // Liste
  // -------------------------------------------------------------------------
  async function render() {
    const box = document.querySelector('#bm-profiles');
    if (!box) return;
    let list = [];
    try {
      const r = await api('GET', '/api/baremetal/profiles');
      list = r.d.profiles || [];
    } catch (e) { /* liste vide */ }
    const rows = list.map(p => `
      <tr data-profile="${esc(p.name)}">
        <td><code>${esc(p.name)}</code>${p.invalid ? ` <span class="bm-refusal">${esc(tr('bmp.invalid'))}</span>` : ''}</td>
        <td>${esc(p.description || '')}</td>
        <td>${(p.uses || []).map(v => `<code>{{${esc(v)}}}</code>`).join(' ')}</td>
        <td class="bm-profile-actions">
          <button class="btn btn-sm btn-primary btn-ico bmp-batch" data-name="${esc(p.name)}"
                  data-tip="${esc(tr('bmp.batchTip'))}"${p.invalid ? ' disabled' : ''}>${icon('install')} ${esc(tr('bmp.batch'))}</button>
          <button class="btn btn-sm btn-secondary bmp-edit" data-name="${esc(p.name)}"
                  data-tip="${esc(tr('bmp.editTip'))}">${icon('edit')}</button>
          <button class="btn btn-sm btn-danger bmp-del" data-name="${esc(p.name)}"
                  data-tip="${esc(tr('bmp.deleteTip'))}">${icon('trash')}</button>
        </td>
      </tr>`).join('') ||
      `<tr><td colspan="4" class="empty-state">${esc(tr('bmp.empty'))}</td></tr>`;
    box.innerHTML = `
      <div class="card">
        <div class="card-header"><h2>${icon('bundle', { size: 18 })} ${esc(tr('bmp.title'))}</h2>
          <button class="btn btn-sm btn-primary btn-ico bmp-new" data-tip="${esc(tr('bmp.newTip'))}">${icon('add')} ${esc(tr('bmp.new'))}</button>
        </div>
        <div class="card-body">
          <p class="form-hint">${esc(tr('bmp.hint', { a: '{{hostname}}', b: '{{storage_ip}}' }))}</p>
          <table class="data-table" id="bmp-table">
            <thead><tr><th>${esc(tr('bmp.col.name'))}</th><th>${esc(tr('bmp.col.description'))}</th>
              <th>${esc(tr('bmp.col.variables'))}</th><th>${esc(tr('col.actions'))}</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </div>`;
  }

  async function remove(name) {
    if (!confirm(tr('bmp.confirmDelete', { name }))) return;
    await api('DELETE', `/api/baremetal/profiles/${encodeURIComponent(name)}`);
    render();
  }

  // -------------------------------------------------------------------------
  // Éditeur d'un profil
  // -------------------------------------------------------------------------
  async function openEditor(name) {
    let prof = { name: '', description: '', variables: [], fields_yaml: NEW_FIELDS, advanced_yaml: '' };
    if (name) {
      const r = await api('GET', `/api/baremetal/profiles/${encodeURIComponent(name)}`);
      if (!r.ok) { alert(r.d.error || r.status); return; }
      prof = r.d;
    }
    const tip = (text) => `class="tip" data-tip="${esc(text)}"`;
    const panel = FloatingPanels.open({
      id: `bm-profile-${name || 'new'}`,
      title: name ? `${tr('bmp.editTitle')} · ${name}` : tr('bmp.newTitle'),
      width: Math.min(820, window.innerWidth - 160), height: Math.max(480, Math.min(820, window.innerHeight - 80)),
      bodyHtml: `
        <form class="capi-form bmp-form" style="padding:14px;" autocomplete="off">
          <p class="form-hint">${esc(tr('bmp.editorHint', { v: '{{name}}' }))}</p>
          <div class="apply-bar">
            <button type="button" class="btn btn-sm btn-secondary btn-ico" data-bmp="import"
                    data-tip="${esc(tr('bmp.fromFileTip'))}">${icon('upload')} ${esc(tr('bmp.fromFile'))}</button>
            <input type="file" data-bmp="file" accept=".yaml,.yml,.txt,text/yaml,text/plain" hidden>
          </div>
          <div class="bmp-msg" role="status"></div>
          <fieldset>
            <legend>${esc(tr('bmp.fs.profile'))}</legend>
            <label ${tip(tr('bmp.tip.name'))}>${esc(tr('bmp.col.name'))} *
              <input name="name" required pattern="[a-z0-9]([-a-z0-9]*[a-z0-9])?" maxlength="63"
                     value="${esc(prof.name)}"${name ? ' readonly' : ''}></label>
            <label ${tip(tr('bmp.tip.description'))}>${esc(tr('bmp.col.description'))}
              <input name="description" maxlength="500" value="${esc(prof.description)}"></label>
            <label style="grid-column:1/-1;" ${tip(tr('bmp.tip.variables'))}>${esc(tr('bmp.f.variables'))}
              <input name="variables" placeholder="admin_ip, storage_ip"
                     value="${esc((prof.variables || []).join(', '))}"></label>
            <p class="form-hint" style="grid-column:1/-1;">${esc(tr('bmp.builtins'))}
              ${['hostname', 'ip', 'mgmt_mac', 'vip'].map(v => `<code>{{${v}}}</code>`).join(' ')}</p>
          </fieldset>
          <fieldset>
            <legend>${esc(tr('bmp.fs.fields'))}</legend>
            <label style="grid-column:1/-1;" ${tip(tr('bmp.tip.fields'))}>${esc(tr('bmp.f.fields'))}
              <textarea name="fields_yaml" class="adv-code" rows="14" spellcheck="false"></textarea></label>
          </fieldset>
          <fieldset>
            <legend>${esc(tr('bmp.fs.advanced'))}</legend>
            <label style="grid-column:1/-1;" ${tip(tr('bmp.tip.advanced'))}>${esc(tr('bmp.f.advanced'))}
              <textarea name="advanced_yaml" class="adv-code" rows="14" spellcheck="false"></textarea></label>
          </fieldset>
          <div class="apply-bar">
            <button type="submit" class="btn btn-primary btn-sm btn-ico"
                    data-tip="${esc(tr('bmp.saveTip'))}">${icon('save')} ${esc(tr('bmp.save'))}</button>
          </div>
        </form>`,
    });
    const form = panel.el.querySelector('.bmp-form');
    const msg = panel.el.querySelector('.bmp-msg');
    // .value, jamais innerHTML : le YAML reste du texte
    form.fields_yaml.value = prof.fields_yaml || '';
    form.advanced_yaml.value = prof.advanced_yaml || '';
    const file = panel.el.querySelector('[data-bmp="file"]');
    panel.el.querySelector('[data-bmp="import"]').addEventListener('click', () => file.click());
    file.addEventListener('change', async () => {
      const f = file.files && file.files[0];
      if (!f) return;
      const r = await api('POST', '/api/baremetal/profiles/from-config', { text: await f.text() });
      file.value = '';
      if (!r.ok) { msg.innerHTML = refusal(r.d, r.status); return; }
      form.fields_yaml.value = r.d.fields_yaml || '';
      form.advanced_yaml.value = r.d.advanced_yaml || '';
      msg.innerHTML = `<div class="bm-imported">${icon('ok', { size: 14 })} ${esc(tr('bmp.imported'))}
        ${r.d.secrets_dropped ? esc(tr('bmp.secretsDropped')) : ''}</div>`;
    });
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const body = {
        name: form.name.value.trim(), description: form.description.value,
        variables: form.variables.value, fields_yaml: form.fields_yaml.value,
        advanced_yaml: form.advanced_yaml.value,
      };
      const r = name
        ? await api('PUT', `/api/baremetal/profiles/${encodeURIComponent(name)}`, body)
        : await api('POST', '/api/baremetal/profiles', body);
      if (!r.ok) { msg.innerHTML = refusal(r.d, r.status); msg.scrollIntoView({ block: 'nearest' }); return; }
      msg.innerHTML = `<div class="bm-imported">${icon('ok', { size: 14 })} ${esc(tr('bmp.saved'))}</div>`;
      render();
      if (!name) { panel.close(); openEditor(body.name); }
    });
  }

  // -------------------------------------------------------------------------
  // Série
  // -------------------------------------------------------------------------
  async function openBatch(name) {
    const r = await api('GET', `/api/baremetal/profiles/${encodeURIComponent(name)}`);
    if (!r.ok) { alert(r.d.error || r.status); return; }
    const prof = r.d;
    let isos = [];
    try { isos = (await fetch('/api/isos').then(x => x.json())).isos || []; } catch (e) { /* vide */ }
    // colonnes : les variables intégrées que le profil emploie, puis les siennes
    const uses = new Set();
    const scan = (t) => String(t || '').replace(/\{\{\s*([A-Za-z0-9_.-]*)\s*\}\}/g, (_, n) => { uses.add(n); return ''; });
    scan(prof.fields_yaml); scan(prof.advanced_yaml);
    const vars = BUILTINS.filter(v => uses.has(v)).concat(prof.variables || []);
    const tip = (text) => `class="tip" data-tip="${esc(text)}"`;
    const defaultIso = (prof.fields || {}).iso || '';
    const isoOpts = [`<option value="">${esc(tr('bmp.isoProfile'))}${defaultIso ? ' (' + esc(defaultIso) + ')' : ''}</option>`]
      .concat(isos.map(i => `<option value="${esc(i.name)}">${esc(i.name)}</option>`)).join('');
    const panel = FloatingPanels.open({
      id: `bm-batch-${name}`,
      title: `${tr('bmp.batchTitle')} · ${name}`,
      width: Math.max(760, Math.min(1100, window.innerWidth - 160)), height: Math.max(480, Math.min(860, window.innerHeight - 80)),
      bodyHtml: `
        <form class="capi-form bmp-batch-form" style="padding:14px;" autocomplete="off">
          <p class="form-hint vm-edit-unverified">${esc(tr('bmp.batchWarn'))}</p>
          <fieldset>
            <legend>${esc(tr('bmp.fs.cluster'))}</legend>
            <label ${tip(tr('bmp.tip.clusterName'))}>${esc(tr('bmc.f.clusterName'))} *
              <input name="cluster_name" required pattern="[a-z0-9]([-a-z0-9.]{0,59}[a-z0-9])?" maxlength="61"></label>
            <label ${tip(tr('bmp.tip.vip', { v: '{{vip}}' }))}>${esc(tr('bmc.f.vip'))} *
              <input name="vip" required placeholder="192.0.2.100"></label>
            <label ${tip(tr('bmp.tip.iso'))}>${esc(tr('bmc.f.iso'))}
              <select name="iso">${isoOpts}</select></label>
            <label ${tip(tr('bmp.tip.concurrency'))}>${esc(tr('bmp.f.concurrency'))}
              <input name="concurrency" type="number" min="1" max="4" value="2"></label>
          </fieldset>
          <fieldset>
            <legend>${esc(tr('bmp.fs.secrets'))}</legend>
            <label ${tip(tr('bmp.tip.token'))}>${esc(tr('bmp.f.token'))} *
              <input name="token" type="password" autocomplete="new-password"></label>
            <label ${tip(tr('bmp.tip.osPassword'))}>${esc(tr('bmp.f.osPassword'))}
              <input name="password" type="password" autocomplete="new-password"></label>
            <label ${tip(tr('bmp.tip.bmcPassword'))}>${esc(tr('bmp.f.bmcPassword'))}
              <input name="bmc_password" type="password" autocomplete="new-password"></label>
            <div class="apply-bar" style="grid-column:1/-1;">
              <button type="button" class="btn btn-sm btn-secondary btn-ico" data-bmp="secrets-file"
                      data-tip="${esc(tr('bmp.secretsFromFileTip'))}">${icon('upload')} ${esc(tr('bmp.secretsFromFile'))}</button>
              <input type="file" data-bmp="sfile" accept=".yaml,.yml,.txt,text/yaml,text/plain" hidden>
              <span class="form-hint bmp-secrets-state"></span>
            </div>
          </fieldset>
          <fieldset>
            <legend>${esc(tr('bmp.fs.nodes'))}</legend>
            <p class="form-hint" style="grid-column:1/-1;">${esc(tr('bmp.nodesHint'))}</p>
            <div style="grid-column:1/-1; overflow-x:auto;">
              <table class="data-table bmp-nodes">
                <thead><tr><th>#</th><th>bmc_host</th><th>bmc_user</th><th>bmc_password</th>
                  ${vars.map(v => `<th><code>{{${esc(v)}}}</code></th>`).join('')}
                  <th>${esc(tr('bmp.col.check'))}</th><th></th></tr></thead>
                <tbody></tbody>
              </table>
            </div>
            <div class="apply-bar" style="grid-column:1/-1;">
              <button type="button" class="btn btn-sm btn-secondary btn-ico" data-bmp="add-row"
                      data-tip="${esc(tr('bmp.addRowTip'))}">${icon('add')} ${esc(tr('bmp.addRow'))}</button>
            </div>
            <label style="grid-column:1/-1;" ${tip(tr('bmp.tip.csv'))}>${esc(tr('bmp.f.csv'))}
              <textarea name="csv" rows="4" class="adv-code" spellcheck="false"
                        placeholder="bmc_host,bmc_user,${esc(vars.join(','))}"></textarea></label>
            <div class="apply-bar" style="grid-column:1/-1;">
              <button type="button" class="btn btn-sm btn-secondary btn-ico" data-bmp="load-csv"
                      data-tip="${esc(tr('bmp.loadCsvTip'))}">${icon('upload')} ${esc(tr('bmp.loadCsv'))}</button>
            </div>
          </fieldset>
          <div class="bmp-msg" role="status"></div>
          <div class="apply-bar">
            <button type="button" class="btn btn-sm btn-secondary btn-ico" data-bmp="preview"
                    data-tip="${esc(tr('bmp.previewTip'))}">${icon('preview')} ${esc(tr('bmp.preview'))}</button>
            <button type="submit" class="btn btn-primary btn-sm btn-ico"
                    data-tip="${esc(tr('bmp.startTip'))}">${icon('play')} ${esc(tr('bmp.start'))}</button>
            <span class="bmp-result"></span>
          </div>
        </form>`,
    });
    const form = panel.el.querySelector('.bmp-batch-form');
    const tbody = panel.el.querySelector('.bmp-nodes tbody');
    const msg = panel.el.querySelector('.bmp-msg');
    let importId = null;
    let previews = [];

    function renumber() {
      tbody.querySelectorAll('tr').forEach((tr_, k) => { tr_.querySelector('.bmp-n').textContent = k + 1; });
    }
    function addRow(rec) {
      rec = rec || {};
      const values = rec.values || {};
      const tr_ = document.createElement('tr');
      tr_.innerHTML = `<td class="bmp-n"></td>
        <td><input data-col="bmc_host" value="${esc(rec.bmc_host || '')}" size="14"></td>
        <td><input data-col="bmc_user" value="${esc(rec.bmc_user || 'admin')}" size="8"></td>
        <td><input data-col="bmc_password" type="password" autocomplete="new-password" size="8"
                   class="tip" data-tip="${esc(tr('bmp.tip.rowPassword'))}"></td>
        ${vars.map(v => `<td><input data-var="${esc(v)}" value="${esc(values[v] || '')}" size="12"></td>`).join('')}
        <td class="bmp-check"></td>
        <td><button type="button" class="btn btn-sm btn-danger bmp-row-del"
                    data-tip="${esc(tr('bmp.delRowTip'))}">${icon('close')}</button></td>`;
      tbody.appendChild(tr_);
      renumber();
    }
    function rows() {
      return [...tbody.querySelectorAll('tr')].map(tr_ => {
        const values = {};
        tr_.querySelectorAll('[data-var]').forEach(i => { if (i.value.trim()) values[i.dataset.var] = i.value.trim(); });
        const row = { values };
        tr_.querySelectorAll('[data-col]').forEach(i => { row[i.dataset.col] = i.value.trim(); });
        return row;
      });
    }
    function common() {
      return {
        cluster_name: form.cluster_name.value.trim(), vip: form.vip.value.trim(),
        iso: form.iso.value, rows: rows().map(({ bmc_password, ...r }) => r),
      };
    }
    function markRows(out) {
      previews = out || [];
      const cells = tbody.querySelectorAll('.bmp-check');
      cells.forEach(c => { c.innerHTML = ''; });
      previews.forEach(p => {
        const c = cells[p.row - 1];
        if (!c) return;
        c.innerHTML = p.yaml
          ? `<button type="button" class="btn btn-sm btn-secondary bmp-row-preview" data-row="${p.row}"
               data-tip="${esc(tr('bmp.rowPreviewTip'))}">${icon('ok', { size: 12 })} ${esc(p.mode)}</button>`
          : `<span class="bm-refusal tip" data-tip="${esc((p.errors || []).map(e => e.join(' : ')).join(' ; '))}">${icon('fail', { size: 12 })} ${esc(tr('bmp.rowRefused'))}</span>`;
      });
    }

    addRow();
    panel.el.querySelector('[data-bmp="add-row"]').addEventListener('click', () => addRow());
    tbody.addEventListener('click', (e) => {
      const del = e.target.closest('.bmp-row-del');
      if (del) { del.closest('tr').remove(); renumber(); return; }
      const pv = e.target.closest('.bmp-row-preview');
      if (pv) {
        const p = previews.find(x => String(x.row) === pv.dataset.row);
        const w = FloatingPanels.open({
          id: `bm-batch-preview-${name}-${pv.dataset.row}`,
          title: `${tr('bmc.previewTitle')} · ${p.hostname}`,
          width: 640, height: Math.max(360, Math.min(720, window.innerHeight - 120)), bodyHtml: '',
        });
        w.setBody(`<textarea class="adv-code bm-preview-text tip" readonly spellcheck="false"
          data-tip="${esc(tr('bmc.tip.previewText'))}"></textarea>`);
        w.body.querySelector('.bm-preview-text').value = p.yaml || '';
      }
    });
    panel.el.querySelector('[data-bmp="load-csv"]').addEventListener('click', async () => {
      const res = await api('POST', `/api/baremetal/profiles/${encodeURIComponent(name)}/csv`, { text: form.csv.value });
      if (!res.ok) { msg.innerHTML = refusal(res.d, res.status); return; }
      tbody.innerHTML = '';
      (res.d.rows || []).forEach(addRow);
      form.csv.value = '';
      msg.innerHTML = `<div class="bm-imported">${icon('ok', { size: 14 })} ${esc(tr('bmp.csvLoaded', { n: (res.d.rows || []).length }))}
        ${res.d.passwords_dropped ? ' ' + esc(tr('bmp.csvPasswordsDropped')) : ''}</div>`;
    });
    const sfile = panel.el.querySelector('[data-bmp="sfile"]');
    panel.el.querySelector('[data-bmp="secrets-file"]').addEventListener('click', () => sfile.click());
    sfile.addEventListener('change', async () => {
      const f = sfile.files && sfile.files[0];
      if (!f) return;
      // même découpage et même cache de 15 min que la fenêtre d'installation
      const res = await api('POST', '/api/baremetal/config/parse', { text: await f.text() });
      sfile.value = '';
      if (!res.ok) { msg.innerHTML = refusal(res.d, res.status); return; }
      importId = res.d.import_id;
      panel.el.querySelector('.bmp-secrets-state').textContent =
        [res.d.has_token ? tr('bmp.tokenFromFile') : '', res.d.has_password ? tr('bmp.passwordFromFile') : '']
          .filter(Boolean).join(' · ');
    });
    panel.el.querySelector('[data-bmp="preview"]').addEventListener('click', async () => {
      msg.innerHTML = '…';
      const res = await api('POST', `/api/baremetal/profiles/${encodeURIComponent(name)}/render`, common());
      if (!res.ok) { markRows([]); msg.innerHTML = refusal(res.d, res.status); return; }
      markRows(res.d.rows);
      msg.innerHTML = res.d.ok
        ? `<div class="bm-imported">${icon('ok', { size: 14 })} ${esc(tr('bmp.previewOk'))}</div>`
        : `<div class="bm-refusal">${icon('fail', { size: 14 })} ${esc(tr('bmp.previewRefused'))}</div>`;
    });
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const res = panel.el.querySelector('.bmp-result');
      const body = {
        ...common(), rows: rows(), concurrency: Number(form.concurrency.value) || 2,
        token: form.token.value, password: form.password.value, bmc_password: form.bmc_password.value,
      };
      if (importId) body.import_id = importId;
      if (!confirm(tr('bmp.confirmStart', { n: body.rows.length, cluster: body.cluster_name }))) return;
      res.textContent = '…';
      const r2 = await api('POST', `/api/baremetal/profiles/${encodeURIComponent(name)}/batch`, body);
      if (r2.ok) {
        res.innerHTML = `<span style="color:var(--accent)">${icon('ok', { size: 14 })} ${esc(tr('bmc.started'))} ${esc(r2.d.action_id)}</span>`;
        msg.innerHTML = '';
      } else {
        res.innerHTML = '';
        msg.innerHTML = refusal(r2.d, r2.status);
        msg.scrollIntoView({ block: 'nearest' });
      }
    });
  }

  function init() {
    document.addEventListener('click', (e) => {
      const n = e.target.closest('.bmp-new');
      if (n) { e.preventDefault(); openEditor(null); return; }
      const ed = e.target.closest('.bmp-edit');
      if (ed) { e.preventDefault(); openEditor(ed.dataset.name); return; }
      const b = e.target.closest('.bmp-batch');
      if (b) { e.preventDefault(); openBatch(b.dataset.name); return; }
      const d = e.target.closest('.bmp-del');
      if (d) { e.preventDefault(); remove(d.dataset.name); }
    });
  }

  return { init, render, openEditor, openBatch };
})();

document.addEventListener('DOMContentLoaded', BMProfiles.init);
window.BMProfiles = BMProfiles;
