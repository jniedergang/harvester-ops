/**
 * harvester-ops : Disaster Recovery, la fenêtre « Nouveau contrat ».
 *
 * Cinq blocs sur une page, chacun dit s'il est prêt :
 *  1. Quoi et vers où : nom, clusters source et secours, VMs choisies par
 *     liste OU par règle d'étiquette (une VM étiquetée plus tard rejoint le
 *     contrat, protégée d'office si on le demande) ;
 *  2. Objectif : RPO, points gardés, et la phase de mesure proposée mais
 *     sautable (avertissement : RPO atteignable estimé, première copie) ;
 *  3. Correspondances : réseau absent sur le secours = « Créer sur la cible »
 *     (VLAN modifiable) ou réseau existant, BLOQUANT tant que rien n'est
 *     choisi ; colonne « Réseau d'exercice » ; classe de stockage DISQUE PAR
 *     DISQUE ; namespaces ;
 *  4. Reprise : vagues (glisser-déposer, condition de passage), cloud-init
 *     rejoué et configuration réseau du secours, MAC gardée ou changée,
 *     CPU et mémoire réduits, actions annexes HTTP (secrets masqués) ;
 *  5. Récapitulatif : ce qui bloque, la commande équivalente, Créer.
 *
 * L'éditeur de reprise (bloc 4) sert aussi l'onglet Réglages d'un contrat :
 * ReplicationCreate.recoveryEditor.
 */
const ReplicationCreate = (() => {
  const U = window.ReplUtil;
  const { tr, enc, esc, icon } = U;
  const MASK = '********';
  const NAME_RE = /^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$/;
  const LABEL_RE = /^([a-z0-9]([-a-z0-9.]*[a-z0-9])?\/)?[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?=[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$/;
  const STEPS = [60, 120, 180, 240, 300, 360, 600, 720, 900, 1200, 1800, 3600, 7200, 10800, 14400, 21600, 28800, 43200, 86400];
  const field = (label, input, tip, extra = '') => `<label class="bk-field"${extra}><span>${esc(label)}</span>${
    input.replace(/^<(input|select|textarea)/, `<$1 class="tip" data-tip="${esc(tip)}"`)}</label>`;
  const okBadge = (state, text) => `<span class="badge ${state === 'ok' ? 'ok' : (state === 'warn' ? 'warn' : 'fail')}" data-rpc-ready="${state}">${esc(text)}</span>`;
  const vmKey = (v) => `${v.namespace}/${v.name}`;
  /** Schéma, hôte et port d'une adresse (« » si illisible). */
  const originOf = (u) => { try { const x = new URL(u); return `${x.protocol}//${x.host}`; } catch (e) { return ''; } };

  // ===========================================================================
  // Éditeur de reprise (bloc 4), partagé avec les réglages d'un contrat
  // ===========================================================================
  function recoveryEditor(host, opts) {
    const plan = opts.plan || null;
    const o = (plan && plan.options) || {};
    const ro = !!opts.readOnly;
    const dis = ro ? ' disabled' : '';
    const st = {
      vms: (opts.vms || []).slice(),
      waves: JSON.parse(JSON.stringify(o.waves && o.waves.length ? o.waves : [])),
      cloud_init: o.cloud_init || 'keep', mac: o.mac || 'keep', start: o.start || 'started',
      network_data: Object.assign({}, o.network_data || {}), resize: JSON.parse(JSON.stringify(o.resize || {})),
      hooks: JSON.parse(JSON.stringify(o.hooks || [])), editing: null,
      degraded: !!o.accept_degraded_standby,
    };
    if (!st.waves.length) st.waves = [{ name: '', vms: st.vms.map(vmKey), gate: { kind: 'running' } }];
    const wavedVms = () => new Set(st.waves.flatMap((w) => w.vms || []));
    /** Les VMs sans vague vont dans la première (affichage) ; celles parties du contrat sont retirées. */
    function normalizeWaves() {
      const keys = new Set(st.vms.map(vmKey));
      st.waves.forEach((w) => { w.vms = (w.vms || []).filter((k) => keys.has(k) || opts.keepUnknown); });
      const seen = wavedVms();
      const missing = st.vms.map(vmKey).filter((k) => !seen.has(k));
      if (missing.length) st.waves[st.waves.length - 1].vms.push(...missing);
    }

    function gateHtml(w, i) {
      const g = w.gate || { kind: 'running' };
      return `<div class="rp-gate">${field(tr('rp.wave.gate'), `<select data-rpw-gate="${i}"${dis}>
          <option value="running"${g.kind === 'running' ? ' selected' : ''}>${esc(tr('rp.wave.gateRunning'))}</option>
          <option value="agent"${g.kind === 'agent' ? ' selected' : ''}>${esc(tr('rp.wave.gateAgent'))}</option>
          <option value="delay"${g.kind === 'delay' ? ' selected' : ''}>${esc(tr('rp.wave.gateDelay'))}</option></select>`, tr('rp.wave.gateTip'))}
        ${g.kind === 'running' ? '' : field(g.kind === 'agent' ? tr('rp.wave.agentMax') : tr('rp.wave.delayS'),
          `<input type="number" min="1" max="3600" data-rpw-sec="${i}" value="${g.seconds || (g.kind === 'agent' ? 300 : 60)}"${dis}>`,
          g.kind === 'agent' ? tr('rp.wave.agentMaxTip') : tr('rp.wave.delaySTip'))}</div>`;
    }
    function wavesHtml() {
      normalizeWaves();
      return `<div class="rp-waves" data-rpw="board">${st.waves.map((w, i) => `<div class="rp-wave" data-rp-wave="${i}">
          <h5><input class="rp-wave-name tip" data-rpw-name="${i}" value="${esc(w.name || '')}" placeholder="${esc(tr('rp.wave.n', { n: i + 1 }))}"
            data-tip="${esc(tr('rp.wave.nameTip'))}" maxlength="40"${dis}>
            ${st.waves.length > 1 && !ro ? `<button type="button" class="btn-icon-sm tip" data-rpw-del="${i}" data-tip="${esc(tr('rp.wave.delTip'))}" aria-label="${esc(tr('rp.wave.delTip'))}">×</button>` : ''}</h5>
          <div class="rp-wave-drop" data-rpw-drop="${i}" aria-label="${esc(tr('rp.wave.n', { n: i + 1 }))}">${(w.vms || []).map((k) => `<div class="rp-vmchip" draggable="${ro ? 'false' : 'true'}" data-rpw-vm="${esc(k)}">
            <span>${icon('vm', 11)} ${esc(k)}</span>
            <select class="tip" data-rpw-move="${esc(k)}" data-tip="${esc(tr('rp.wave.moveTip'))}" aria-label="${esc(tr('rp.wave.moveTip'))}"${dis}>${st.waves.map((_x, j) => `<option value="${j}"${j === i ? ' selected' : ''}>${esc(tr('rp.wave.short', { n: j + 1 }))}</option>`).join('')}</select></div>`).join('')
            || `<p class="res-dim rp-wave-empty">${esc(tr('rp.wave.empty'))}</p>`}</div>
          ${gateHtml(w, i)}</div>`).join('')}</div>
        ${ro ? '' : `<button type="button" class="btn btn-sm btn-secondary tip" data-rpw="add" data-tip="${esc(tr('rp.wave.addTip'))}">${icon('add')} ${esc(tr('rp.wave.add'))}</button>`}`;
    }
    function guestHtml() {
      const rerun = st.cloud_init === 'rerun';
      return `<div class="ct-create-row">
        ${field(tr('rp.rec.cloudInit'), `<select data-rpr="cloud_init"${dis}><option value="keep"${!rerun ? ' selected' : ''}>${esc(tr('rp.rec.ciKeep'))}</option>
          <option value="rerun"${rerun ? ' selected' : ''}>${esc(tr('rp.rec.ciRerun'))}</option></select>`, tr('rp.rec.cloudInitTip'))}
        ${field(tr('rp.rec.mac'), `<select data-rpr="mac"${dis}><option value="keep"${st.mac === 'keep' ? ' selected' : ''}>${esc(tr('rp.rec.macKeep'))}</option>
          <option value="new"${st.mac === 'new' ? ' selected' : ''}>${esc(tr('rp.rec.macNew'))}</option></select>`, tr('rp.rec.macTip'))}
        ${field(tr('rp.rec.start'), `<select data-rpr="start"${dis}><option value="started"${st.start === 'started' ? ' selected' : ''}>${esc(tr('rp.rec.started'))}</option>
          <option value="stopped"${st.start === 'stopped' ? ' selected' : ''}>${esc(tr('rp.rec.stopped'))}</option></select>`, tr('rp.rec.startTip'))}
        </div>
        <div class="tw"><table class="data-table rp-guest"><thead><tr><th>${esc(tr('rp.col.vm'))}</th><th>${esc(tr('rp.rec.cpu'))}</th>
          <th>${esc(tr('rp.rec.memory'))}</th><th>${esc(tr('rp.rec.netcfg'))}</th></tr></thead>
        <tbody>${st.vms.map((v) => {
          const k = vmKey(v), rz = st.resize[k] || {};
          return `<tr data-rpr-vm="${esc(k)}"><td><b>${esc(k)}</b></td>
            <td><input type="number" min="1" max="256" class="tip rp-num" data-rpr-cpu="${esc(k)}" value="${esc(rz.cpu || '')}" placeholder="${esc(v.cpu || tr('rp.rec.same'))}" data-tip="${esc(tr('rp.rec.cpuTip'))}" aria-label="${esc(tr('rp.rec.cpu'))}"${dis}></td>
            <td><input class="tip rp-num" data-rpr-mem="${esc(k)}" value="${esc(rz.memory || '')}" placeholder="${esc(v.memory || tr('rp.rec.same'))}" pattern="[0-9]+(Mi|Gi)" data-tip="${esc(tr('rp.rec.memoryTip'))}" aria-label="${esc(tr('rp.rec.memory'))}"${dis}></td>
            <td><textarea rows="${st.network_data[k] ? 4 : 1}" class="tip rp-netcfg" data-rpr-net="${esc(k)}" placeholder="${esc(rerun ? tr('rp.rec.netcfgPh') : tr('rp.rec.netcfgNeedsRerun'))}"
              data-tip="${esc(tr('rp.rec.netcfgTip'))}" aria-label="${esc(tr('rp.rec.netcfg'))}"${rerun && !ro ? '' : ' disabled'}>${esc(st.network_data[k] || '')}</textarea></td></tr>`;
        }).join('') || `<tr><td colspan="4" class="hint">${esc(tr('rp.rec.noVm'))}</td></tr>`}</tbody></table></div>`;
    }
    function atLabel(at) {
      if (at === 'start') return tr('rp.hook.atStart');
      if (at === 'end') return tr('rp.hook.atEnd');
      const n = String(at || '').split(':')[1];
      return tr('rp.hook.atWave', { n });
    }
    function atOptions(sel) {
      return `<option value="start"${sel === 'start' ? ' selected' : ''}>${esc(tr('rp.hook.atStart'))}</option>`
        + st.waves.map((_w, i) => `<option value="wave:${i + 1}"${sel === `wave:${i + 1}` ? ' selected' : ''}>${esc(tr('rp.hook.atWave', { n: i + 1 }))}</option>`).join('')
        + `<option value="end"${sel === 'end' || !sel ? ' selected' : ''}>${esc(tr('rp.hook.atEnd'))}</option>`;
    }
    function hookForm(h) {
      const sec = Object.entries(h.secret_headers || {});
      return `<div class="rp-hook-form fk-step" data-rph="form">
        <div class="ct-create-row">
          ${field(tr('rp.hook.name'), `<input name="h_name" value="${esc(h.name || '')}" maxlength="40">`, tr('rp.hook.nameTip'))}
          ${field(tr('rp.hook.at'), `<select name="h_at">${atOptions(h.at)}</select>`, tr('rp.hook.atTip'))}
          ${field(tr('rp.hook.method'), `<select name="h_method">${['POST', 'PUT', 'PATCH', 'GET', 'DELETE'].map((m) => `<option${(h.method || 'POST') === m ? ' selected' : ''}>${m}</option>`).join('')}</select>`, tr('rp.hook.methodTip'))}
          ${field(tr('rp.hook.timeout'), `<input name="h_timeout" type="number" min="1" max="60" value="${h.timeout_s || 15}">`, tr('rp.hook.timeoutTip'))}
        </div>
        ${field(tr('rp.hook.url'), `<input name="h_url" value="${esc(h.url || '')}" placeholder="https://dns-api.example/v1/records/erp">`, tr('rp.hook.urlTip'))}
        ${field(tr('rp.hook.headers'), `<textarea name="h_headers" rows="2" placeholder="Content-Type: application/json">${esc(Object.entries(h.headers || {}).map(([k, x]) => `${k}: ${x}`).join('\n'))}</textarea>`, tr('rp.hook.headersTip'))}
        <div class="ct-create-row">
          ${field(tr('rp.hook.secretName'), `<input name="h_sname" value="${esc(sec[0] ? sec[0][0] : '')}" placeholder="Authorization">`, tr('rp.hook.secretNameTip'))}
          ${field(tr('rp.hook.secretValue'), `<input name="h_svalue" type="password" autocomplete="new-password" value="${sec[0] ? esc(sec[0][1]) : ''}">`, tr('rp.hook.secretValueTip'))}
        </div>
        ${field(tr('rp.hook.body'), `<textarea name="h_body" rows="3">${esc(h.body || '')}</textarea>`, tr('rp.hook.bodyTip'))}
        <div class="btn-row"><button type="button" class="btn btn-sm btn-primary tip" data-rph="keep" data-tip="${esc(tr('rp.hook.keepTip'))}">${icon('ok')} ${esc(tr('rp.hook.keep'))}</button>
          <button type="button" class="btn btn-sm btn-secondary tip" data-rph="cancel" data-tip="${esc(tr('rp.hook.cancelTip'))}">${esc(tr('rp.hook.cancel'))}</button></div>
        <div class="of-msg" data-rph="formmsg"></div></div>`;
    }
    function hooksHtml() {
      return `<div class="tw"><table class="data-table rp-hooks"><thead><tr><th>${esc(tr('rp.hook.at'))}</th><th>${esc(tr('rp.hook.name'))}</th>
          <th>${esc(tr('rp.hook.call'))}</th><th>${esc(tr('rp.hook.secret'))}</th><th></th></tr></thead>
        <tbody>${st.hooks.map((h, i) => `<tr data-rp-hook="${esc(h.name)}"><td>${esc(atLabel(h.at))}</td><td><b>${esc(h.name)}</b></td>
          <td><code>${esc(h.method || 'POST')} ${esc(h.url)}</code></td>
          <td>${Object.keys(h.secret_headers || {}).length ? `<span class="rp-secret tip" data-tip="${esc(tr('rp.hook.maskedTip'))}">${esc(Object.keys(h.secret_headers).join(', '))}: ${MASK}</span>` : '<span class="res-dim">-</span>'}</td>
          <td class="btn-row">${opts.planId ? `<button type="button" class="btn btn-sm btn-secondary tip" data-rph-try="${esc(h.name)}" data-tip="${esc(tr('rp.hook.tryTip'))}"${dis}>${icon('play')} ${esc(tr('rp.hook.try'))}</button>`
            : `<button type="button" class="btn btn-sm btn-secondary tip" disabled data-tip="${esc(tr('rp.hook.tryLater'))}">${icon('play')} ${esc(tr('rp.hook.try'))}</button>`}
            ${ro ? '' : `<button type="button" class="btn btn-sm btn-secondary tip" data-rph-edit="${i}" data-tip="${esc(tr('rp.hook.editTip'))}">${icon('edit')}</button>
            <button type="button" class="btn-icon-sm tip" data-rph-del="${i}" data-tip="${esc(tr('rp.hook.delTip'))}" aria-label="${esc(tr('rp.hook.delTip'))}">×</button>`}</td></tr>`).join('')
          || `<tr><td colspan="5" class="hint">${esc(tr('rp.hook.none'))}</td></tr>`}</tbody></table></div>
        ${st.editing !== null ? hookForm(st.editing === -1 ? {} : st.hooks[st.editing]) : (ro ? '' : `<button type="button" class="btn btn-sm btn-secondary tip" data-rph="add" data-tip="${esc(tr('rp.hook.addTip'))}">${icon('add')} ${esc(tr('rp.hook.add'))}</button>`)}
        <div class="of-msg" data-rph="msg" role="status"></div>`;
    }
    function draw() {
      host.innerHTML = `<div class="rp-rec">
        <h4>${esc(tr('rp.rec.waves'))}</h4><p class="form-hint">${esc(tr('rp.rec.wavesHint'))}</p><div data-rpr="waves">${wavesHtml()}</div>
        <h4>${esc(tr('rp.rec.guest'))}</h4><p class="form-hint">${esc(tr('rp.rec.guestHint'))}</p><div data-rpr="guest">${guestHtml()}</div>
        <h4>${esc(tr('rp.rec.hooks'))}</h4><p class="form-hint">${esc(tr('rp.rec.hooksHint'))}</p><div data-rpr="hooks">${hooksHtml()}</div>
        ${opts.planId ? `<h4>${esc(tr('rp.rec.standby'))}</h4><label class="bk-check tip" data-tip="${esc(tr('rp.c.ackDegradedTip'))}"><input type="checkbox" data-rpr-degraded${st.degraded ? ' checked' : ''}${dis}> <span>${esc(tr('rp.c.ackDegraded'))}</span></label>` : ''}</div>`;
    }
    const redraw = (part) => {
      if (part === 'waves') host.querySelector('[data-rpr="waves"]').innerHTML = wavesHtml();
      else if (part === 'guest') host.querySelector('[data-rpr="guest"]').innerHTML = guestHtml();
      else if (part === 'hooks') host.querySelector('[data-rpr="hooks"]').innerHTML = hooksHtml();
      else draw();
      if (opts.onChange) opts.onChange();
    };
    function captureGuest() {
      host.querySelectorAll('[data-rpr-cpu]').forEach((el) => {
        const k = el.dataset.rprCpu, m = host.querySelector(`[data-rpr-mem="${CSS.escape(k)}"]`);
        const r = {};
        if (el.value) r.cpu = Number(el.value);
        if (m && m.value.trim()) r.memory = m.value.trim();
        if (Object.keys(r).length) st.resize[k] = r; else delete st.resize[k];
      });
      host.querySelectorAll('[data-rpr-net]').forEach((el) => {
        if (el.value.trim()) st.network_data[el.dataset.rprNet] = el.value; else delete st.network_data[el.dataset.rprNet];
      });
    }
    function moveVm(k, to) {
      st.waves.forEach((w) => { w.vms = (w.vms || []).filter((x) => x !== k); });
      st.waves[to].vms.push(k);
      redraw('waves');
    }
    function keepHook() {
      const f = host.querySelector('[data-rph="form"]');
      const v = (n) => f.querySelector(`[name="${n}"]`).value;
      const m = f.querySelector('[data-rph="formmsg"]');
      const h = { name: v('h_name').trim(), at: v('h_at'), method: v('h_method'), url: v('h_url').trim(),
                  timeout_s: Number(v('h_timeout') || 15), body: v('h_body'), headers: {}, secret_headers: {} };
      const errs = [];
      if (!NAME_RE.test(h.name) || h.name.length > 40) errs.push(tr('rp.hook.errName'));
      if (st.hooks.some((x, i) => x.name === h.name && i !== st.editing)) errs.push(tr('rp.hook.errDup'));
      if (!/^https?:\/\/\S+$/.test(h.url)) errs.push(tr('rp.hook.errUrl'));
      v('h_headers').split('\n').map((l) => l.trim()).filter(Boolean).forEach((l) => {
        const i = l.indexOf(':');
        if (i < 1) errs.push(tr('rp.hook.errHeader', { line: l })); else h.headers[l.slice(0, i).trim()] = l.slice(i + 1).trim();
      });
      const prev = st.editing >= 0 ? st.hooks[st.editing] : null;
      const prevSec = (prev && prev.secret_headers) || {};
      const first = Object.keys(prevSec)[0];
      // les en-têtes secrets au-delà du premier (posés en ligne de commande) restent tels quels
      Object.entries(prevSec).forEach(([k, x]) => { if (k !== first) h.secret_headers[k] = x; });
      if (v('h_sname').trim()) h.secret_headers[v('h_sname').trim()] = v('h_svalue');
      const masked = Object.values(h.secret_headers).includes(MASK);
      // même règle que le serveur : un secret masqué ne suit ni un renommage ni un autre hôte
      if (masked && prev && prev.name !== h.name) errs.push(tr('rp.hook.errRenameSecret'));
      else if (masked && prev && originOf(prev.url) !== originOf(h.url)) errs.push(tr('rp.hook.errUrlSecret'));
      if (errs.length) { m.innerHTML = `<span class="res-error">${esc(errs.join(' · '))}</span>`; return; }
      if (st.editing === -1) st.hooks.push(h); else st.hooks[st.editing] = h;
      st.editing = null;
      redraw('hooks');
    }

    draw();
    host.addEventListener('click', async (e) => {
      if (ro && !e.target.closest('[data-rph-try]')) return;
      const t = e.target.closest('[data-rpw], [data-rpw-del], [data-rph], [data-rph-edit], [data-rph-del], [data-rph-try]');
      if (!t || t.disabled) return;
      if (t.dataset.rpw === 'add') { st.waves.push({ name: '', vms: [], gate: { kind: 'running' } }); redraw('waves'); return; }
      if (t.dataset.rpwDel !== undefined) {
        const i = Number(t.dataset.rpwDel);
        const moved = st.waves[i].vms || [];
        st.waves.splice(i, 1);
        st.waves[Math.max(0, i - 1)].vms.push(...moved);
        redraw('waves'); return;
      }
      if (t.dataset.rph === 'add') { captureGuest(); st.editing = -1; redraw('hooks'); return; }
      if (t.dataset.rph === 'cancel') { st.editing = null; redraw('hooks'); return; }
      if (t.dataset.rph === 'keep') { keepHook(); return; }
      if (t.dataset.rphEdit !== undefined) { st.editing = Number(t.dataset.rphEdit); redraw('hooks'); return; }
      if (t.dataset.rphDel !== undefined) { st.hooks.splice(Number(t.dataset.rphDel), 1); redraw('hooks'); return; }
      if (t.dataset.rphTry) {
        const msg = host.querySelector('[data-rph="msg"]');
        try {
          const out = await U.api('POST', `/api/replication/plans/${enc(opts.planId)}/hooks/test`, { name: t.dataset.rphTry });
          U.follow(out.action_id, msg, tr('rp.hook.tried'), async () => {
            const o = await U.actionOutput(out.action_id);
            if (o && (o.status || o.http_status)) msg.innerHTML += ` <span class="res-dim">HTTP ${esc(o.status || o.http_status)}</span>`;
          });
        } catch (er) { msg.innerHTML = `<span class="res-error">${esc(er.message)}</span>`; }
      }
    });
    host.addEventListener('change', (e) => {
      const el = e.target;
      if (el.dataset.rpwGate !== undefined) {
        const w = st.waves[Number(el.dataset.rpwGate)];
        w.gate = el.value === 'running' ? { kind: 'running' } : { kind: el.value, seconds: el.value === 'agent' ? 300 : 60 };
        redraw('waves'); return;
      }
      if (el.dataset.rprDegraded !== undefined) { st.degraded = el.checked; if (opts.onChange) opts.onChange(); return; }
      if (el.dataset.rpwSec !== undefined) { st.waves[Number(el.dataset.rpwSec)].gate.seconds = Number(el.value); return; }
      if (el.dataset.rpwName !== undefined) { st.waves[Number(el.dataset.rpwName)].name = el.value.trim(); return; }
      if (el.dataset.rpwMove !== undefined) { moveVm(el.dataset.rpwMove, Number(el.value)); return; }
      if (el.dataset.rpr) { captureGuest(); st[el.dataset.rpr] = el.value; if (el.dataset.rpr === 'cloud_init') redraw('guest'); else if (opts.onChange) opts.onChange(); return; }
      if (el.dataset.rprCpu !== undefined || el.dataset.rprMem !== undefined || el.dataset.rprNet !== undefined) { captureGuest(); if (opts.onChange) opts.onChange(); }
    });
    // glisser-déposer des VMs entre vagues (le menu de chaque VM fait la même chose au clavier)
    host.addEventListener('dragstart', (e) => {
      const c = e.target.closest && e.target.closest('[data-rpw-vm]');
      if (!c) return;
      e.dataTransfer.setData('text/plain', c.dataset.rpwVm);
      e.dataTransfer.effectAllowed = 'move';
    });
    host.addEventListener('dragover', (e) => {
      const z = e.target.closest && e.target.closest('[data-rp-wave]');
      if (!z) return;
      e.preventDefault();
      host.querySelectorAll('.rp-wave.over').forEach((x) => x !== z && x.classList.remove('over'));
      z.classList.add('over');
    });
    host.addEventListener('dragleave', (e) => {
      const z = e.target.closest && e.target.closest('[data-rp-wave]');
      if (z && !z.contains(e.relatedTarget)) z.classList.remove('over');
    });
    host.addEventListener('drop', (e) => {
      const z = e.target.closest && e.target.closest('[data-rp-wave]');
      if (!z) return;
      e.preventDefault();
      const k = e.dataTransfer.getData('text/plain');
      if (k) moveVm(k, Number(z.dataset.rpWave));
    });

    return {
      setVms(vms) { captureGuest(); st.vms = vms.slice(); draw(); },
      read() {
        captureGuest();
        const errors = [];
        Object.entries(st.resize).forEach(([k, r]) => {
          if (r.cpu != null && !(Number.isInteger(r.cpu) && r.cpu >= 1 && r.cpu <= 256)) errors.push(tr('rp.rec.errCpu', { vm: k }));
          if (r.memory != null && !/^[1-9][0-9]*(Mi|Gi)$/.test(r.memory)) errors.push(tr('rp.rec.errMemory', { vm: k }));
        });
        if (st.cloud_init !== 'rerun' && Object.keys(st.network_data).length) errors.push(tr('rp.rec.errNetcfg'));
        const waves = st.waves.filter((w) => (w.vms || []).length).map((w) => {
          const g = w.gate && w.gate.kind !== 'running' ? { kind: w.gate.kind, seconds: Number(w.gate.seconds) } : { kind: 'running' };
          return Object.assign(w.name ? { name: w.name } : {}, { vms: w.vms.slice(), gate: g });
        });
        // une seule vague « au démarrage » = l'ordre par défaut : rien à envoyer
        const trivial = waves.length <= 1 && (!waves[0] || waves[0].gate.kind === 'running') && !(waves[0] && waves[0].name);
        return { errors, cloud_init: st.cloud_init, mac: st.mac, start: st.start,
                 recovery: Object.assign({ waves: trivial ? [] : waves, hooks: st.hooks, network_data: st.cloud_init === 'rerun' ? st.network_data : {}, resize: st.resize },
                   opts.planId ? { accept_degraded_standby: st.degraded } : {}) };
      },
      state: st,
    };
  }

  // ===========================================================================
  // La fenêtre de création
  // ===========================================================================
  function open(pre = {}) {
    const panel = FloatingPanels.open({ id: 'rp-create', icon: 'shield', width: 1080, height: 860, title: tr('rp.c.title'),
      bodyHtml: '<form class="of-form rp-create" autocomplete="off" novalidate></form>' });
    const f = panel.el.querySelector('form');
    if (f.dataset.ready) return panel;
    f.dataset.ready = '1';
    const names = U.clusterNames();
    const from0 = pre.from && names.includes(pre.from) ? pre.from : names[0] || '';
    const st = {
      from: from0, to: names.find((n) => n !== from0) || '', mode: 'list', selected: new Set(pre.vms || []),
      ruleKey: '', ns: '', auto: true, rpo: 15, retain: 6, measure: true,
      cand: null, ruleCand: null, src: {}, dst: {}, links: null,
      netMap: {}, drillMap: {}, netCreate: {}, diskClass: {}, nsMap: {}, createNs: {}, seq: 0, ackDegraded: false,
    };
    f.innerHTML = `
      <p class="form-hint">${esc(tr('rp.c.hint'))}</p>
      <section class="fk-step" data-rpc-block="1"><div class="fk-step-head"><span class="fk-step-n">1</span><b>${esc(tr('rp.c.b1'))}</b><span data-rpc-state="1"></span></div>
        <div class="ct-create-row">
          ${field(tr('rp.f.name'), '<input name="name" maxlength="63" required spellcheck="false">', tr('rp.f.nameTip'))}
          ${field(tr('rp.f.from'), `<select name="from">${names.map((n) => `<option${n === st.from ? ' selected' : ''}>${esc(n)}</option>`).join('')}</select>`, tr('rp.f.fromTip'))}
          ${field(tr('rp.f.to'), `<select name="to">${names.map((n) => `<option${n === st.to ? ' selected' : ''}>${esc(n)}</option>`).join('')}</select>`, tr('rp.f.toTip'))}
        </div>
        <div class="sub-tabs sub-tabs-inline" role="tablist" aria-label="${esc(tr('rp.c.selMode'))}">
          <button type="button" class="sub-tab tip active" role="tab" aria-selected="true" data-rpc-mode="list" data-tip="${esc(tr('rp.c.byListTip'))}">${icon('vm')} <span>${esc(tr('rp.c.byList'))}</span></button>
          <button type="button" class="sub-tab tip" role="tab" aria-selected="false" data-rpc-mode="rule" data-tip="${esc(tr('rp.c.byRuleTip'))}">${icon('tag')} <span>${esc(tr('rp.c.byRule'))}</span></button></div>
        <div class="rp-rule" data-rpc="rule" hidden>
          <div class="ct-create-row">
            ${field(tr('rp.c.label'), '<input name="rule" placeholder="dr=gold" spellcheck="false">', tr('rp.c.labelTip'))}
            ${field(tr('rp.c.ruleNs'), '<select name="rule_ns"></select>', tr('rp.c.ruleNsTip'))}
          </div>
          <label class="bk-check tip" data-tip="${esc(tr('rp.c.autoTip'))}"><input type="checkbox" name="auto" checked> <span>${esc(tr('rp.c.auto'))}</span></label>
          <p class="form-hint" data-rpc="rulecount"></p></div>
        <div data-rpc="vms"><p class="form-hint">${esc(tr('common.loading'))}</p></div></section>
      <section class="fk-step" data-rpc-block="2"><div class="fk-step-head"><span class="fk-step-n">2</span><b>${esc(tr('rp.c.b2'))}</b><span data-rpc-state="2"></span></div>
        <p class="form-hint">${esc(tr('rp.c.b2hint'))}</p>
        <div class="btn-row rp-presets">${[5, 15, 60].map((m) => `<button type="button" class="btn btn-sm btn-secondary tip" data-rpc-rpo="${m}" data-tip="${esc(tr('rp.c.presetTip', { n: m }))}">${esc(tr('rp.dur.min', { n: m }))}</button>`).join('')}
          ${field(tr('rp.f.rpo'), `<input name="rpo" type="number" min="1" max="10080" value="${st.rpo}">`, tr('rp.f.rpoTip'))}
          ${field(tr('rp.f.retain'), `<input name="retain" type="number" min="1" max="100" value="${st.retain}">`, tr('rp.f.retainTip'))}</div>
        <fieldset class="rp-measure"><legend>${esc(tr('rp.c.measure'))}</legend>
          <label class="bk-check tip" data-tip="${esc(tr('rp.c.measureYesTip'))}"><input type="radio" name="measure" value="yes" checked> <span>${esc(tr('rp.c.measureYes'))}</span></label>
          <label class="bk-check tip" data-tip="${esc(tr('rp.c.measureNoTip'))}"><input type="radio" name="measure" value="no"> <span>${esc(tr('rp.c.measureNo'))}</span></label>
        </fieldset>
        <div data-rpc="estimate"></div></section>
      <section class="fk-step" data-rpc-block="3"><div class="fk-step-head"><span class="fk-step-n">3</span><b>${esc(tr('rp.c.b3'))}</b><span data-rpc-state="3"></span></div>
        <h4>${esc(tr('rp.c.networks'))}</h4><div data-rpc="nets"></div>
        <h4>${esc(tr('rp.c.disks'))}</h4><p class="form-hint">${esc(tr('rp.c.disksHint'))}</p><div data-rpc="disks"></div>
        <h4>${esc(tr('rp.c.namespaces'))}</h4><div data-rpc="nss"></div></section>
      <section class="fk-step" data-rpc-block="4"><div class="fk-step-head"><span class="fk-step-n">4</span><b>${esc(tr('rp.c.b4'))}</b><span data-rpc-state="4"></span></div>
        <div data-rpc="recovery"></div></section>
      <section class="fk-step" data-rpc-block="5"><div class="fk-step-head"><span class="fk-step-n">5</span><b>${esc(tr('rp.c.b5'))}</b><span data-rpc-state="5"></span></div>
        <h4>${esc(tr('rp.chk.title'))}</h4><ul class="rp-issues rp-checks" data-rpc="checks"></ul>
        <ul class="rp-issues" data-rpc="issues"></ul>
        <h4>${esc(tr('rp.c.cli'))}</h4><pre class="code rp-cli" data-rpc="cli"></pre>
        <div class="bk-form-actions"><button type="submit" class="btn btn-sm btn-primary tip" data-rpc="create" data-tip="${esc(tr('rp.c.createTip'))}">${icon('add')} <span>${esc(tr('rp.c.create'))}</span></button></div>
        <div class="of-msg" data-rpc="msg" role="status" aria-live="polite"></div></section>`;
    const $ = (s) => f.querySelector(s);
    const rec = recoveryEditor($('[data-rpc="recovery"]'), { vms: [], onChange: () => refresh() });

    // rôle de la personne (contrôle affiché avant Créer)
    U.get('/api/replication/plans').then((d) => { st.role = (d && d.role) || 'viewer'; refresh(); });

    // -- données ------------------------------------------------------------------
    async function loadSource() {
      const c = st.from, seq = ++st.seq;
      st.cand = null;
      $('[data-rpc="vms"]').innerHTML = `<p class="form-hint">${esc(tr('common.loading'))}</p>`;
      const [cand, nets, nss] = await Promise.all([U.get(`/api/replication/candidates/${enc(c)}`), U.get(`/api/networks/${enc(c)}`), U.get(`/api/namespaces/${enc(c)}`)]);
      if (seq !== st.seq) return;
      st.cand = cand; st.src = { nets: Array.isArray(nets) ? nets : [], nss: Array.isArray(nss) ? nss : [] };
      const sel = f.rule_ns;
      const nsNames = [...new Set(st.src.nss.map((n) => n.name).concat((cand.vms || []).map((v) => v.namespace)))].sort();
      sel.innerHTML = nsNames.map((n) => `<option${n === (st.ns || 'default') ? ' selected' : ''}>${esc(n)}</option>`).join('');
      st.ns = sel.value;
      if (st.mode === 'rule') await loadRule(); else refresh();
    }
    async function loadTarget() {
      const c = st.to;
      const [nets, scs, nss, fab, links, cap] = await Promise.all([U.get(`/api/networks/${enc(c)}`), U.get(`/api/storageclasses/${enc(c)}`),
        U.get(`/api/namespaces/${enc(c)}`), U.get(`/api/network-fabric/${enc(c)}`), st.links ? Promise.resolve(st.links) : U.get('/api/replication/links'),
        U.get(`/api/storage-capacity/${enc(c)}`).catch(() => null)]);
      if (c !== st.to) return;
      st.dst = { nets: Array.isArray(nets) ? nets : [], scs: Array.isArray(scs) ? scs : [], nss: Array.isArray(nss) ? nss : [],
                 cap: cap && cap.classes ? cap : null,
                 cns: ((fab && fab.cluster_networks) || []).map((x) => (typeof x === 'string' ? x : x.name)).filter(Boolean),
                 error: nets && nets.error ? nets.error : null };
      st.links = links;
      refresh();
    }
    let ruleTimer = null;
    async function loadRule() {
      const rule = f.rule.value.trim();
      st.ruleKey = rule;
      if (!LABEL_RE.test(rule) || !st.ns) { st.ruleCand = null; refresh(); return; }
      const d = await U.get(`/api/replication/candidates/${enc(st.from)}?namespace=${enc(st.ns)}&selector=${enc(rule)}`);
      if (rule !== f.rule.value.trim()) return;
      st.ruleCand = d;
      refresh();
    }

    // -- calculs ------------------------------------------------------------------
    function chosenVms() {
      if (st.mode === 'rule') return ((st.ruleCand && st.ruleCand.vms) || []);
      return ((st.cand && st.cand.vms) || []).filter((v) => st.selected.has(vmKey(v)));
    }
    function srcNetworks(vms) {
      const set = new Set();
      vms.forEach((v) => (v.networks || []).forEach((n) => set.add(n)));
      return [...set].sort();
    }
    const dstNetKeys = () => (st.dst.nets || []).map((n) => `${n.namespace}/${n.name}`);
    const dstClasses = () => (st.dst.scs || []).map((s) => s.name);
    const defaultClass = () => ((st.dst.scs || []).find((s) => s.is_default) || {}).name || '';
    function netTarget(n) {
      if (st.netMap[n] !== undefined) return st.netMap[n];
      return dstNetKeys().includes(n) ? n : '';
    }
    function diskTarget(v, d) {
      const k = `${vmKey(v)}/${d.disk}`;
      if (st.diskClass[k] !== undefined) return st.diskClass[k];
      return dstClasses().includes(d.storage_class) ? d.storage_class : '';
    }
    const dstNs = () => (st.dst.nss || []).map((n) => n.name);
    /** Classes choisies qui demandent plus de répliques que la cible n'a de nœuds Longhorn planifiables
     *  (numberOfReplicas de la StorageClass, lu par /api/storage-capacity) : secours dégradé à vie. */
    function overClasses(vms) {
      const cap = st.dst.cap;
      if (!cap) return [];
      const nodes = Number(cap.schedulable_nodes) || 0;
      const used = new Set();
      vms.forEach((v) => (v.disks || []).filter((d) => d.eligible !== false).forEach((d) => { const t = diskTarget(v, d); if (t) used.add(t); }));
      const fits = Object.entries(cap.classes).filter(([n, c]) => dstClasses().includes(n) && c.replicas <= nodes)
        .sort((a, b) => b[1].replicas - a[1].replicas || a[0].localeCompare(b[0]));
      return [...used].sort().filter((sc) => nodes && cap.classes[sc] && cap.classes[sc].replicas > nodes)
        .map((sc) => ({ sc, replicas: cap.classes[sc].replicas, nodes, suggest: fits.length ? fits[0][0] : '' }));
    }
    function nsTarget(ns) { return st.nsMap[ns] !== undefined ? st.nsMap[ns] : ns; }
    function issues() {
      const out = [];
      const name = f.name.value.trim();
      if (!NAME_RE.test(name)) out.push(['1', tr('rp.c.errName')]);
      if (!st.from || !st.to || st.from === st.to) out.push(['1', tr('rp.c.errClusters')]);
      const vms = chosenVms();
      if (st.mode === 'rule' && !LABEL_RE.test(st.ruleKey)) out.push(['1', tr('rp.c.errRule')]);
      else if (!vms.length && st.mode === 'list') out.push(['1', tr('rp.c.errNoVm')]);
      vms.filter((v) => v.eligible === false).forEach((v) => out.push(['1', tr('rp.c.errIneligible', { vm: vmKey(v), why: (v.reasons || []).join(', ') })]));
      const rpo = Number(f.rpo.value);
      if (!(rpo >= 1 && rpo <= 10080)) out.push(['2', tr('rp.c.errRpo')]);
      const ret = Number(f.retain.value);
      if (!(Number.isInteger(ret) && ret >= 1 && ret <= 100)) out.push(['2', tr('rp.c.errRetain')]);
      srcNetworks(vms).forEach((n) => { if (!netTarget(n)) out.push(['3', tr('rp.c.errNet', { net: n })]); });
      vms.forEach((v) => (v.disks || []).filter((d) => d.eligible !== false).forEach((d) => {
        if (!diskTarget(v, d)) out.push(['3', tr('rp.c.errDisk', { vm: vmKey(v), disk: d.disk })]);
      }));
      [...new Set(vms.map((v) => v.namespace))].forEach((ns) => {
        if (!dstNs().includes(nsTarget(ns)) && !st.createNs[ns] && st.dst.nss) out.push(['3', tr('rp.c.errNs', { ns })]);
      });
      const over = overClasses(vms);
      if (over.length && !st.ackDegraded) out.push(['3', tr('rp.c.errReplicas', { classes: over.map((x) => x.sc).join(', ') })]);
      rec.read().errors.forEach((x) => out.push(['4', x]));
      return out;
    }

    // -- rendu --------------------------------------------------------------------
    function vmsHtml() {
      if (st.mode === 'rule') {
        const d = st.ruleCand;
        if (!d) return `<p class="hint">${esc(tr('rp.c.ruleWaiting'))}</p>`;
        if (d.error) return `<p class="res-error">${esc(d.error)}</p>`;
        return candTable(d.vms || [], false);
      }
      const d = st.cand;
      if (!d) return `<p class="form-hint">${esc(tr('common.loading'))}</p>`;
      if (d.error) return `<p class="res-error">${esc(d.error)}</p>`;
      return candTable(d.vms || [], true);
    }
    function candTable(vms, pick) {
      if (!vms.length) return `<p class="hint">${esc(tr('rp.c.noCandidates'))}</p>`;
      return `<div class="tw rp-cand-wrap"><table class="data-table rp-cand"><thead><tr>${pick ? '<th></th>' : ''}<th>${esc(tr('rp.col.vm'))}</th>
          <th>${esc(tr('rp.col.disks'))}</th><th>${esc(tr('rp.c.labels'))}</th><th>${esc(tr('rp.c.elig'))}</th></tr></thead>
        <tbody>${vms.map((v) => {
          const k = vmKey(v);
          const held = (v.plans || []).length;
          return `<tr data-rpc-vm="${esc(k)}">${pick ? `<td><input type="checkbox" class="tip" data-rpc-pick="${esc(k)}" data-tip="${esc(tr('rp.c.pickTip'))}"
              aria-label="${esc(k)}"${st.selected.has(k) ? ' checked' : ''}${v.eligible === false ? ' disabled' : ''}></td>` : ''}
            <td><b>${esc(k)}</b> ${v.running ? `<span class="badge ok">${esc(tr('rp.c.running'))}</span>` : `<span class="badge dim">${esc(tr('rp.c.stopped'))}</span>`}</td>
            <td>${(v.disks || []).map((d) => `<span class="tip" data-tip="${esc(d.storage_class || '')}">${esc(d.disk)} ${esc(U.bytes(d.size))}</span>`).join(', ')}</td>
            <td>${Object.entries(v.labels || {}).slice(0, 4).map(([a, b]) => `<code>${esc(a)}=${esc(b)}</code>`).join(' ')}</td>
            <td>${v.eligible === false ? `<span class="badge fail tip" data-tip="${esc((v.reasons || []).join(', '))}">${esc(tr('rp.c.ineligible'))}</span>`
              : (held ? `<span class="badge warn tip" data-tip="${esc(tr('rp.c.heldTip', { list: v.plans.join(', ') }))}">${esc(tr('rp.c.held'))}</span>` : `<span class="badge ok">${esc(tr('rp.c.eligible'))}</span>`)}</td></tr>`;
        }).join('')}</tbody></table></div>`;
    }
    function netsHtml(vms) {
      const nets = srcNetworks(vms);
      if (!nets.length) return `<p class="hint">${esc(tr('rp.c.noNets'))}</p>`;
      if (st.dst.error) return `<p class="res-error">${esc(st.dst.error)}</p>`;
      const srcVlan = (n) => ((st.src.nets || []).find((x) => `${x.namespace}/${x.name}` === n) || {}).vlan;
      const opt = (sel, allowEmpty, emptyLabel) => `${allowEmpty ? `<option value="">${esc(emptyLabel)}</option>` : ''}${dstNetKeys().map((k) => `<option${k === sel ? ' selected' : ''}>${esc(k)}</option>`).join('')}`;
      return `<div class="tw"><table class="data-table rp-nets"><thead><tr><th>${esc(tr('rp.c.srcNet'))}</th><th>VLAN</th><th>${esc(tr('rp.c.dstNet', { cluster: st.to }))}</th>
          <th>${esc(tr('rp.col.state'))}</th><th>${esc(tr('rp.c.drillNet'))}</th></tr></thead>
        <tbody>${nets.map((n) => {
          const t = netTarget(n);
          const c = st.netCreate[n];
          const same = t && t === n;
          return `<tr data-rpc-net="${esc(n)}"><td><b>${esc(n)}</b></td><td>${esc(srcVlan(n) || '-')}</td>
            <td><select class="tip" data-rpc-netmap="${esc(n)}" data-tip="${esc(tr('rp.c.dstNetTip'))}" aria-label="${esc(tr('rp.c.dstNet', { cluster: st.to }))}">${opt(t, true, tr('rp.c.pickNet'))}</select>
              ${t ? '' : `<button type="button" class="btn btn-sm btn-secondary tip" data-rpc-netcreate="${esc(n)}" data-tip="${esc(tr('rp.c.createNetTip', { cluster: st.to }))}">${icon('add')} ${esc(tr('rp.c.createNet'))}</button>`}
              ${c ? `<div class="rp-netcreate" data-rpc-netform="${esc(n)}"><div class="ct-create-row">
                ${field(tr('rp.c.newNetName'), `<input data-rpc-nc="name" value="${esc(c.name)}">`, tr('rp.c.newNetNameTip'))}
                ${field('VLAN', `<input type="number" min="1" max="4094" data-rpc-nc="vlan" value="${esc(c.vlan)}">`, tr('rp.c.vlanTip'))}
                ${field(tr('of.f.clusterNetwork'), `<select data-rpc-nc="cn">${(st.dst.cns.length ? st.dst.cns : ['mgmt']).map((x) => `<option${x === c.cn ? ' selected' : ''}>${esc(x)}</option>`).join('')}</select>`, tr('rp.c.cnTip'))}</div>
                <div class="btn-row"><button type="button" class="btn btn-sm btn-primary tip" data-rpc-ncgo="${esc(n)}" data-tip="${esc(tr('rp.c.createNetGoTip'))}">${esc(tr('rp.c.createNetGo'))}</button>
                  <button type="button" class="btn btn-sm btn-secondary tip" data-rpc-ncno="${esc(n)}" data-tip="${esc(tr('rp.hook.cancelTip'))}">${esc(tr('rp.hook.cancel'))}</button></div>
                <div class="of-msg" data-rpc-ncmsg="${esc(n)}"></div></div>` : ''}</td>
            <td>${t ? okBadge('ok', same ? tr('rp.c.netSame') : tr('rp.c.netMapped')) : okBadge('fail', tr('rp.c.netMissing'))}</td>
            <td><select class="tip" data-rpc-drill="${esc(n)}" data-tip="${esc(tr('rp.c.drillTip'))}" aria-label="${esc(tr('rp.c.drillNet'))}">${opt(st.drillMap[n] || '', true, tr('rp.c.drillNone'))}</select></td></tr>`;
        }).join('')}</tbody></table></div>`;
    }
    function disksHtml(vms) {
      const rows = vms.flatMap((v) => (v.disks || []).map((d) => [v, d]));
      if (!rows.length) return `<p class="hint">${esc(tr('rp.c.noDisks'))}</p>`;
      const classes = dstClasses();
      const over = overClasses(vms);
      const overSet = new Set(over.map((x) => x.sc));
      return `<div class="tw"><table class="data-table rp-disks"><thead><tr><th>${esc(tr('rp.col.vm'))}</th><th>${esc(tr('rp.c.disk'))}</th><th>${esc(tr('rp.c.size'))}</th>
          <th>${esc(tr('rp.c.srcClass'))}</th><th>${esc(tr('rp.c.dstClass', { cluster: st.to }))}</th><th>${esc(tr('rp.col.state'))}</th></tr></thead>
        <tbody>${rows.map(([v, d]) => {
          const k = `${vmKey(v)}/${d.disk}`;
          if (d.eligible === false) {
            return `<tr data-rpc-disk="${esc(k)}"><td>${esc(vmKey(v))}</td><td>${esc(d.disk)}</td><td>${esc(U.bytes(d.size))}</td><td>${esc(d.storage_class || '-')}</td>
              <td colspan="2"><span class="badge dim tip" data-tip="${esc(d.reason || '')}">${esc(tr('rp.c.diskSkipped'))}</span></td></tr>`;
          }
          const t = diskTarget(v, d);
          return `<tr data-rpc-disk="${esc(k)}"><td>${esc(vmKey(v))}</td><td><b>${esc(d.disk)}</b>${d.image ? ` <span class="res-dim tip" data-tip="${esc(d.image)}">${icon('cdrom', 11)}</span>` : ''}</td>
            <td>${esc(U.bytes(d.size))}</td><td>${esc(d.storage_class || '-')}</td>
            <td><select class="tip" data-rpc-sc="${esc(k)}" data-tip="${esc(tr('rp.c.dstClassTip'))}" aria-label="${esc(tr('rp.c.dstClass', { cluster: st.to }))}">
              <option value="">${esc(tr('rp.c.pickClass'))}</option>${classes.map((c) => `<option value="${esc(c)}"${c === t ? ' selected' : ''}>${esc(c)}${c === defaultClass() ? ` (${esc(tr('rp.c.default'))})` : ''}</option>`).join('')}</select></td>
            <td>${!t ? okBadge('fail', tr('rp.c.classMissing')) : (overSet.has(t) ? okBadge('warn', tr('rp.c.degraded'))
              : okBadge('ok', t === d.storage_class ? tr('rp.c.netSame') : tr('rp.c.netMapped')))}</td></tr>`;
        }).join('')}</tbody></table></div>${replicasHtml(over)}`;
    }
    /** Avertissement répliques > nœuds : changer de classe en un clic, ou accepter un secours dégradé. */
    function replicasHtml(over) {
      if (!over.length) return '';
      return `<div class="sto-finding sev-action" data-rpc="replicas"><div class="sto-finding-title">${icon('warn')} ${esc(tr('rp.c.replicasTitle'))}</div>
        ${over.map((x) => `<p class="sto-finding-facts" data-rpc-over="${esc(x.sc)}">${esc(tr('rp.c.replicasOver', { sc: x.sc, replicas: x.replicas, nodes: x.nodes }))}
          ${x.suggest ? `<button type="button" class="btn btn-sm btn-secondary tip" data-rpc-usesc="${esc(x.sc)}" data-to="${esc(x.suggest)}" data-tip="${esc(tr('rp.c.useClassTip', { sc: x.suggest, replicas: (st.dst.cap.classes[x.suggest] || {}).replicas }))}">${esc(tr('rp.c.useClass', { sc: x.suggest }))}</button>` : ''}</p>`).join('')}
        <label class="bk-check tip" data-tip="${esc(tr('rp.c.ackDegradedTip'))}"><input type="checkbox" data-rpc-ackdeg${st.ackDegraded ? ' checked' : ''}> <span>${esc(tr('rp.c.ackDegraded'))}</span></label></div>`;
    }
    function nssHtml(vms) {
      const nss = [...new Set(vms.map((v) => v.namespace))].sort();
      if (!nss.length) return '';
      return `<div class="tw"><table class="data-table rp-nss"><thead><tr><th>${esc(tr('rp.c.srcNs'))}</th><th>${esc(tr('rp.c.dstNs', { cluster: st.to }))}</th><th>${esc(tr('rp.col.state'))}</th></tr></thead>
        <tbody>${nss.map((ns) => {
          const t = nsTarget(ns);
          const exists = dstNs().includes(t);
          return `<tr data-rpc-ns="${esc(ns)}"><td>${esc(ns)}</td>
            <td><select class="tip" data-rpc-nsmap="${esc(ns)}" data-tip="${esc(tr('rp.c.dstNsTip'))}" aria-label="${esc(tr('rp.c.dstNs', { cluster: st.to }))}">${[...new Set([ns].concat(dstNs()))].map((x) => `<option${x === t ? ' selected' : ''}>${esc(x)}</option>`).join('')}</select>
              ${exists ? '' : `<label class="bk-check tip" data-tip="${esc(tr('rp.c.createNsTip'))}"><input type="checkbox" data-rpc-nscreate="${esc(ns)}"${st.createNs[ns] ? ' checked' : ''}> <span>${esc(tr('rp.c.createNs'))}</span></label>`}</td>
            <td>${exists ? okBadge('ok', tr('rp.c.nsExists')) : (st.createNs[ns] ? okBadge('warn', tr('rp.c.nsCreated')) : okBadge('fail', tr('rp.c.nsMissing')))}</td></tr>`;
        }).join('')}</tbody></table></div>`;
    }
    /** Intervalle sans mesure : le plus long pas de Longhorn qui tient deux fois dans l'objectif. */
    const estInterval = () => STEPS.filter((x) => x <= (Number(f.rpo.value) * 60) / 2).pop() || 60;
    /** Estimation sans mesure : intervalle déduit de l'objectif, débit du lien, première copie. */
    function estimateHtml(vms) {
      const measure = (f.querySelector('[name="measure"]:checked') || {}).value !== 'no';
      if (measure) return `<p class="hint">${icon('info')} ${esc(tr('rp.c.measureInfo'))}</p>`;
      const obj = Number(f.rpo.value) * 60;
      const interval = estInterval();
      const link = ((st.links && st.links.links) || []).find((l) => l.source === st.from && l.target === st.to);
      const p = (link && link.passive) || {}, a = (link && link.active) || {};
      const up = p.upload_bps_p50_24h || a.write_bps, ap = p.apply_bps_p50_24h || a.read_bps;
      const rate = up && ap ? Math.min(up, ap) : (up || ap || null);
      const used = vms.reduce((s, v) => s + (v.disks || []).reduce((x, d) => x + (Number(d.used || d.size) || 0), 0), 0);
      const reach = interval + 120;
      return `<div class="sto-finding sev-action" data-rpc="skipwarn"><div class="sto-finding-title">${icon('warn')} ${esc(tr('rp.c.skipWarn'))}</div>
        <p class="sto-finding-facts">${esc(tr('rp.c.estimate', { interval: U.dur(interval), rpo: U.dur(reach), obj: U.dur(obj) }))}</p>
        <p class="sto-finding-facts">${rate ? esc(tr('rp.c.firstCopy', { size: U.bytes(used), time: U.dur(used / rate), rate: U.rate(rate) }))
          : esc(tr('rp.c.noLinkData', { from: st.from, to: st.to }))}</p>
        <p class="sto-finding-advice">${esc(reach > obj ? tr('rp.c.estimateOver') : tr('rp.c.estimateAdvice'))}</p></div>`;
    }
    function body() {
      const vms = chosenVms();
      const b = { name: f.name.value.trim(), from: st.from, to: st.to, rpo: `${Number(f.rpo.value)}m`, retain: Number(f.retain.value) };
      // protéger sans mesurer : la protection prend l'intervalle estimé, rangé dans le contrat
      if ((f.querySelector('[name="measure"]:checked') || {}).value === 'no') b.interval = `${estInterval()}s`;
      if (st.mode === 'rule') { b.selector = st.ruleKey; b.namespace = st.ns; } else b.vms = vms.map(vmKey);
      const nets = srcNetworks(vms);
      const mapNet = {}, drill = {};
      nets.forEach((n) => { const t = netTarget(n); if (t && t !== n) mapNet[n] = t; if (st.drillMap[n]) drill[n] = st.drillMap[n]; });
      if (Object.keys(mapNet).length) b.map_net = mapNet;
      if (Object.keys(drill).length) b.map_drill_net = drill;
      const disk = {};
      vms.forEach((v) => (v.disks || []).filter((d) => d.eligible !== false).forEach((d) => {
        const t = diskTarget(v, d);
        if (t && t !== d.storage_class) disk[`${vmKey(v)}/${d.disk}`] = t;
      }));
      if (Object.keys(disk).length) b.map_disk_class = disk;
      const ns = {};
      [...new Set(vms.map((v) => v.namespace))].forEach((x) => { if (nsTarget(x) !== x) ns[x] = nsTarget(x); });
      if (Object.keys(ns).length) b.map_ns = ns;
      if (Object.values(st.createNs).some(Boolean)) b.create_namespace = true;
      const r = rec.read();
      b.cloud_init = r.cloud_init; b.mac = r.mac; b.start = r.start;
      const recovery = Object.assign({}, r.recovery);
      if (st.mode === 'rule') recovery.auto_protect = !!f.auto.checked;
      if (st.ackDegraded && overClasses(vms).length) recovery.accept_degraded_standby = true;
      Object.keys(recovery).forEach((k) => {
        const x = recovery[k];
        if ((Array.isArray(x) && !x.length) || (x && typeof x === 'object' && !Array.isArray(x) && !Object.keys(x).length)) delete recovery[k];
      });
      if (Object.keys(recovery).length) b.recovery = recovery;
      return b;
    }
    function cliText(b) {
      const q = (s) => (/^[\w./=:@-]+$/.test(s) ? s : `'${String(s).replace(/'/g, "'\\''")}'`);
      const parts = ['harvester-replication plan create', `--name ${q(b.name || '<name>')}`, `--from ${q(b.from)}`, `--to ${q(b.to)}`, `--rpo ${q(b.rpo)}`, `--retain ${b.retain}`];
      if (b.interval) parts.push(`--interval ${b.interval}`);
      (b.vms || []).forEach((v) => parts.push(`--vm ${q(v)}`));
      if (b.selector) parts.push(`--selector ${q(b.selector)}`, `--namespace ${q(b.namespace)}`);
      Object.entries(b.map_net || {}).forEach(([a, c]) => parts.push(`--map-net ${q(`${a}=${c}`)}`));
      Object.entries(b.map_ns || {}).forEach(([a, c]) => parts.push(`--map-ns ${q(`${a}=${c}`)}`));
      if (b.create_namespace) parts.push('--create-namespace');
      ['cloud_init', 'mac', 'start'].forEach((k) => parts.push(`--${k.replace('_', '-')} ${b[k]}`));
      if (b.map_disk_class || b.map_drill_net || b.recovery) parts.push('--spec recovery.json');
      return parts.join(' \\\n    ');
    }

    /** Les contrôles avant Créer : droits, lecture des deux clusters, correspondances, débit. */
    function checksHtml(list) {
      const li = (ok, text) => `<li class="${ok === true ? 'ok' : (ok === false ? 'fail' : 'warn')}" data-rpc-check="${ok === true ? 'ok' : (ok === false ? 'fail' : 'warn')}">${icon(ok === true ? 'ok' : (ok === false ? 'fail' : 'info'), 11)} ${esc(text)}</li>`;
      const role = st.role || 'viewer';
      const out = [li(U.roleRank(role) >= 2, U.roleRank(role) >= 2 ? tr('rp.chk.role', { role }) : tr('rp.chk.roleNo', { role }))];
      if (st.cand) out.push(st.cand.error ? li(false, tr('rp.chk.sourceNo', { cluster: st.from, why: st.cand.error })) : li(true, tr('rp.chk.source', { cluster: st.from })));
      if (st.dst.nets) out.push(st.dst.error ? li(false, tr('rp.chk.targetNo', { cluster: st.to, why: st.dst.error })) : li(true, tr('rp.chk.target', { cluster: st.to })));
      const map = list.filter((x) => x[0] === '3').length;
      out.push(li(!map, map ? tr('rp.chk.mappingNo') : tr('rp.chk.mapping')));
      const over = overClasses(chosenVms());
      if (st.dst.cap) out.push(over.length ? li(st.ackDegraded ? null : false, tr('rp.chk.replicasNo', { classes: over.map((x) => x.sc).join(', ') })) : li(true, tr('rp.chk.replicas')));
      const link = ((st.links && st.links.links) || []).find((l) => l.source === st.from && l.target === st.to);
      const p = (link && link.passive) || {}, a = (link && link.active) || {};
      const up = p.upload_bps_p50_24h || a.write_bps, ap = p.apply_bps_p50_24h || a.read_bps;
      const rate = up && ap ? Math.min(up, ap) : (up || ap || null);
      out.push(rate ? li(true, tr('rp.chk.link', { rate: U.rate(rate) })) : li(null, tr('rp.chk.linkNo')));
      out.push(li(null, tr('rp.chk.capacity')));
      return out.join('');
    }

    function refresh() {
      const vms = chosenVms();
      $('[data-rpc="vms"]').innerHTML = vmsHtml();
      if (st.mode === 'rule') {
        const n = st.ruleCand && st.ruleCand.vms ? st.ruleCand.vms.length : null;
        $('[data-rpc="rulecount"]').textContent = n == null ? tr('rp.c.ruleHint') : tr('rp.c.ruleCount', { n, ns: st.ns });
      }
      $('[data-rpc="nets"]').innerHTML = netsHtml(vms);
      $('[data-rpc="disks"]').innerHTML = disksHtml(vms);
      $('[data-rpc="nss"]').innerHTML = nssHtml(vms);
      $('[data-rpc="estimate"]').innerHTML = estimateHtml(vms);
      const keys = vms.map(vmKey).join('|');
      if (keys !== st.recKeys) { st.recKeys = keys; rec.setVms(vms); }
      const list = issues();
      if (U.roleRank(st.role || 'viewer') < 2 && st.role) list.push(['5', tr('rp.chk.roleNo', { role: st.role })]);
      $('[data-rpc="checks"]').innerHTML = checksHtml(list);
      [1, 2, 3, 4].forEach((i) => {
        const n = list.filter((x) => x[0] === String(i)).length;
        $(`[data-rpc-state="${i}"]`).innerHTML = n ? okBadge('fail', tr('rp.c.blocking', { n })) : okBadge('ok', tr('rp.c.ready'));
      });
      $('[data-rpc-state="5"]').innerHTML = list.length ? okBadge('fail', tr('rp.c.blocking', { n: list.length })) : okBadge('ok', tr('rp.c.ready'));
      $('[data-rpc="issues"]').innerHTML = list.length ? list.map(([blk, t]) => `<li class="fail" data-rpc-issue="${blk}">${icon('fail', 11)} <b>${esc(tr('rp.c.blockN', { n: blk }))}</b> ${esc(t)}</li>`).join('')
        : `<li class="ok">${icon('ok', 11)} ${esc(tr('rp.c.allReady'))}</li>`;
      $('[data-rpc="cli"]').textContent = cliText(body());
      const btn = $('[data-rpc="create"]');
      btn.disabled = !!list.length || st.busy;
      const measure = (f.querySelector('[name="measure"]:checked') || {}).value !== 'no';
      btn.querySelector('span').textContent = measure ? tr('rp.c.createMeasure') : tr('rp.c.createProtect');
    }

    // -- événements ---------------------------------------------------------------
    f.addEventListener('click', async (e) => {
      const m = e.target.closest('[data-rpc-mode]');
      if (m) {
        st.mode = m.dataset.rpcMode;
        f.querySelectorAll('[data-rpc-mode]').forEach((b) => { const on = b === m; b.classList.toggle('active', on); b.setAttribute('aria-selected', String(on)); });
        $('[data-rpc="rule"]').hidden = st.mode !== 'rule';
        if (st.mode === 'rule') loadRule(); else refresh();
        return;
      }
      const p = e.target.closest('[data-rpc-rpo]');
      if (p) { f.rpo.value = p.dataset.rpcRpo; refresh(); return; }
      const nc = e.target.closest('[data-rpc-netcreate]');
      if (nc) {
        const n = nc.dataset.rpcNetcreate;
        const src = (st.src.nets || []).find((x) => `${x.namespace}/${x.name}` === n) || {};
        st.netCreate[n] = { name: n.split('/')[1], vlan: src.vlan || '', cn: st.dst.cns.includes(src.cluster_network) ? src.cluster_network : (st.dst.cns[0] || 'mgmt') };
        refresh(); return;
      }
      const no = e.target.closest('[data-rpc-ncno]');
      if (no) { delete st.netCreate[no.dataset.rpcNcno]; refresh(); return; }
      const go = e.target.closest('[data-rpc-ncgo]');
      if (go) { createNetwork(go.dataset.rpcNcgo, go); return; }
      const use = e.target.closest('[data-rpc-usesc]');
      if (use) {
        chosenVms().forEach((v) => (v.disks || []).filter((d) => d.eligible !== false).forEach((d) => {
          if (diskTarget(v, d) === use.dataset.rpcUsesc) st.diskClass[`${vmKey(v)}/${d.disk}`] = use.dataset.to;
        }));
        refresh();
      }
    });
    async function createNetwork(n, btn) {
      const form = f.querySelector(`[data-rpc-netform="${CSS.escape(n)}"]`);
      const msg = f.querySelector(`[data-rpc-ncmsg="${CSS.escape(n)}"]`);
      const name = form.querySelector('[data-rpc-nc="name"]').value.trim();
      const vlan = Number(form.querySelector('[data-rpc-nc="vlan"]').value);
      const cn = form.querySelector('[data-rpc-nc="cn"]').value;
      if (!NAME_RE.test(name) || !(vlan >= 1 && vlan <= 4094)) { msg.innerHTML = `<span class="res-error">${esc(tr('rp.c.errNewNet'))}</span>`; return; }
      const ns = n.split('/')[0];
      btn.disabled = true;
      try {
        const out = await U.api('POST', `/api/objects/${enc(st.to)}/network`, { spec: { namespace: ns, name, type: 'vlan', vlan, cluster_network: cn, route_mode: 'auto' } });
        U.follow(out.action_id, msg, tr('rp.c.netCreated', { net: `${ns}/${name}`, cluster: st.to }), async (ok) => {
          btn.disabled = false;
          if (!ok) return;
          st.netMap[n] = `${ns}/${name}`;
          delete st.netCreate[n];
          await loadTarget();
          if (!dstNetKeys().includes(`${ns}/${name}`)) st.dst.nets.push({ namespace: ns, name, vlan: String(vlan), cluster_network: cn });
          refresh();
        });
      } catch (er) { btn.disabled = false; msg.innerHTML = `<span class="res-error">${esc(er.message)}</span>`; }
    }
    f.addEventListener('change', (e) => {
      const el = e.target;
      if (el.closest('[data-rpc="recovery"]')) return;
      if (el.name === 'from') { st.from = el.value; st.selected.clear(); st.diskClass = {}; loadSource(); return; }
      if (el.name === 'to') { st.to = el.value; st.ackDegraded = false; st.netMap = {}; st.drillMap = {}; st.diskClass = {}; st.nsMap = {}; st.netCreate = {}; loadTarget(); return; }
      if (el.name === 'rule_ns') { st.ns = el.value; loadRule(); return; }
      if (el.dataset.rpcPick) { if (el.checked) st.selected.add(el.dataset.rpcPick); else st.selected.delete(el.dataset.rpcPick); refresh(); return; }
      if (el.dataset.rpcNetmap !== undefined) { st.netMap[el.dataset.rpcNetmap] = el.value; refresh(); return; }
      if (el.dataset.rpcDrill !== undefined) { st.drillMap[el.dataset.rpcDrill] = el.value; refresh(); return; }
      if (el.dataset.rpcSc !== undefined) { st.diskClass[el.dataset.rpcSc] = el.value; refresh(); return; }
      if (el.dataset.rpcNsmap !== undefined) { st.nsMap[el.dataset.rpcNsmap] = el.value; refresh(); return; }
      if (el.dataset.rpcNscreate !== undefined) { st.createNs[el.dataset.rpcNscreate] = el.checked; refresh(); return; }
      if (el.dataset.rpcAckdeg !== undefined) { st.ackDegraded = el.checked; refresh(); return; }
      if (el.dataset.rpcNc) { const n = el.closest('[data-rpc-netform]').dataset.rpcNetform; st.netCreate[n][el.dataset.rpcNc] = el.value; return; }
      if (['measure', 'auto', 'rpo', 'retain'].includes(el.name)) refresh();
    });
    f.addEventListener('input', (e) => {
      const el = e.target;
      if (el.closest('[data-rpc="recovery"]')) return;
      if (el.name === 'rule') { clearTimeout(ruleTimer); ruleTimer = setTimeout(loadRule, 400); return; }
      if (el.dataset.rpcNc) { const n = el.closest('[data-rpc-netform]').dataset.rpcNetform; st.netCreate[n][el.dataset.rpcNc] = el.value; return; }
      if (['name', 'rpo', 'retain'].includes(el.name)) {
        // pas de repeinte des tableaux pendant la frappe : seul le récapitulatif bouge
        const list = issues();
        $('[data-rpc="create"]').disabled = !!list.length || st.busy;
        $('[data-rpc="cli"]').textContent = cliText(body());
        clearTimeout(st.typing);
        st.typing = setTimeout(refresh, 500);
      }
    });
    f.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (issues().length || st.busy) { refresh(); return; }
      const b = body();
      const measure = (f.querySelector('[name="measure"]:checked') || {}).value !== 'no';
      const msg = $('[data-rpc="msg"]');
      st.busy = true; refresh();
      try {
        const out = await U.api('POST', '/api/replication/plans', b);
        U.follow(out.action_id, msg, tr('rp.c.created', { name: b.name }), async (ok) => {
          if (!ok) { st.busy = false; refresh(); return; }
          const list = await U.get('/api/replication/plans');
          const plan = ((list && list.plans) || []).find((x) => x.name === b.name);
          if (!plan) { st.busy = false; refresh(); return; }
          try {
            const next = measure ? await U.api('POST', `/api/replication/plans/${enc(plan.id)}/measure`, { op: 'start' })
              : await U.api('POST', `/api/replication/plans/${enc(plan.id)}/protect`, {});
            U.follow(next.action_id, null, measure ? tr('rp.done.measureStart') : tr('rp.done.protect'), () => { if (window.Replication) Replication.load(); });
          } catch (er) { msg.innerHTML += ` <span class="res-error">${esc(er.message)}</span>`; }
          if (pre.onCreated) pre.onCreated(plan);
          FloatingPanels.close('rp-create');
          if (window.ReplicationPlan) ReplicationPlan.open(plan.id);
        });
      } catch (er) {
        st.busy = false; refresh();
        msg.innerHTML = `<span class="res-error">${icon('fail')} ${esc(er.message)}</span>`;
      }
    });

    loadSource();
    loadTarget();
    f.name.focus();
    return panel;
  }

  return { open, recoveryEditor };
})();
window.ReplicationCreate = ReplicationCreate;
