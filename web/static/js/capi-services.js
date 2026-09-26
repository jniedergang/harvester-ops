/**
 * harvester-ops : des services sur les clusters créés par Cluster API
 * (v1.52.0, B2).
 *
 * Un service est un HelmChartProxy de CAAPH (le fournisseur d'add-ons Helm
 * de Cluster API) qui vise un cluster par une étiquette. La vue propose un
 * catalogue prêt à l'emploi (DNS, application témoin) et un chart libre,
 * contrôle la demande avant l'envoi, et suit l'installation dans le dock.
 * Le serveur ne fait que lancer `harvester-capi` (parité CLI).
 * Voir docs/design/2026-09-26-services-capi.md.
 */
const CapiServices = (() => {
  const REFRESH_MS = 10000;
  const enc = encodeURIComponent;
  const B = () => window.Board;
  const tr = (k, f, v) => B().tr(k, f, v);
  const esc = (v) => B().esc(v);
  let cluster = null;
  let host = null;
  let timer = null;
  let lastData = null;
  let modal = null;

  // Un appel littéral par constat : le contrôle de parité des traductions
  // ne voit que ces formes.
  const FINDINGS = {
    'caaph-missing': (v) => tr('svc.fd.caaph-missing', 'The Helm add-on provider (CAAPH) is not installed: install it from the Installation tab.', v),
    'service-unknown': (v) => tr('svc.fd.service-unknown', 'Unknown service {service}.', v),
    'invalid-name': (v) => tr('svc.fd.invalid-name', 'Invalid name "{name}": lower case letters, digits and dashes, 42 characters at most.', v),
    'cluster-missing': (v) => tr('svc.fd.cluster-missing', 'Cluster {cluster} does not exist.', v),
    'service-exists': (v) => tr('svc.fd.service-exists', 'Service {name} already exists next to this cluster: it will be updated.', v),
    'invalid-repo': (v) => tr('svc.fd.invalid-repo', '"{repo}" is not a chart repository address (https://, http:// or oci://).', v),
    'invalid-chart': (v) => tr('svc.fd.invalid-chart', 'Invalid chart name "{chart}".', v),
    'invalid-namespace': (v) => tr('svc.fd.invalid-namespace', 'Invalid namespace "{namespace}".', v),
    'version-floating': (v) => tr('svc.fd.version-floating', 'No version given: the latest one is installed, and may change later.', v),
    'invalid-ipam': (v) => tr('svc.fd.invalid-ipam', 'Unknown address mode "{ipam}" (dhcp or pool).', v),
    'invalid-values': (v) => tr('svc.fd.invalid-values', 'The values are not valid YAML: {message}', v),
    'lb-added': (v) => tr('svc.fd.lb-added', 'kube-vip will be added to {cluster}: without it, no LoadBalancer service of the cluster gets an address.', v),
  };
  const LEVEL_CLASS = { block: 'sev-critical', warn: 'sev-watch', ok: 'sev-info' };
  const LEVEL_RANK = { block: 0, warn: 1, ok: 2 };

  const CATALOG_TEXT = {
    coredns: {
      title: () => tr('svc.cat.coredns', 'DNS server (CoreDNS)'),
      desc: () => tr('svc.cat.coredns.desc', 'Answers name queries for your network: local records, the rest forwarded to upstream resolvers. Reachable on an address of the Harvester pool.'),
    },
    podinfo: {
      title: () => tr('svc.cat.podinfo', 'Test application (podinfo)'),
      desc: () => tr('svc.cat.podinfo.desc', 'A small web page that says which cluster it runs on: checks the whole chain, from the chart to the load balancer address.'),
    },
    custom: {
      title: () => tr('svc.cat.custom', 'Any Helm chart'),
      desc: () => tr('svc.cat.custom.desc', 'A chart from a Helm repository of your choice, with its version and values.'),
    },
  };
  const titleOf = (key) => (CATALOG_TEXT[key] ? CATALOG_TEXT[key].title() : key);

  function findingText(f) {
    const fn = FINDINGS[f.code];
    return fn ? fn(Object.assign({}, f.facts || {})) : f.code;
  }

  // La bulle arrive déjà traduite (appel `tr` littéral, vu par le contrôle
  // de parité), comme dans les formulaires de la vue VPC.
  const field = (label, input, tip, extra = '') => {
    const attrs = `class="tip" data-tip="${esc(tip)}"`;
    return `<label class="tf-field"><span class="tf-label">${esc(label)} ${extra}</span>${input
      .replace('<input', `<input ${attrs}`).replace('<select', `<select ${attrs}`)
      .replace('<textarea', `<textarea ${attrs}`)}</label>`;
  };

  // -------------------------------------------------------------------------
  // Rendu du panneau
  // -------------------------------------------------------------------------
  function releaseBadge(r) {
    // Une release que CAAPH n'a pas encore refaite après un changement
    // n'est pas « prête », même si l'ancienne l'était.
    const ready = r.ready && r.current !== false;
    const failed = /fail/i.test(r.status || '') || (!r.ready && /error|fail/i.test(r.message || ''));
    const cls = ready ? 'ok' : failed ? 'fail' : 'warn';
    const icon = ready ? 'ok' : failed ? 'fail' : 'pending';
    const tip = r.current === false ? tr('svc.t.releaseUpdating', 'Being updated with the new settings')
      : r.message || (ready ? tr('svc.t.releaseReady', 'Installed and ready')
      : tr('svc.t.releasePending', 'Being installed'));
    return `<span class="badge ${cls} tip" data-tip="${esc(tip)}">${Icons.svg(icon, { size: 12 })} ${esc(r.cluster || '?')}`
      + ` · ${esc(r.status || tr('svc.pending', 'pending'))}${r.revision ? ` · r${esc(r.revision)}` : ''}</span>`;
  }

  function render(d) {
    const body = host.querySelector('.svc-body');
    if (!body) return;
    const parts = [];
    if (!d.caaph) {
      parts.push(`<div class="sto-finding sev-watch" data-code="caaph-missing"><div class="sto-finding-title">${esc(FINDINGS['caaph-missing']({}))}
        <button type="button" class="btn btn-sm btn-secondary tip" data-svc-goto="install"
                data-tip="${esc(tr('svc.t.gotoInstall', 'Open the Installation tab'))}">${esc(tr('capi.tab.install', 'Installation'))}</button></div></div>`);
    }
    if (!(d.clusters || []).length) {
      parts.push(`<p class="empty-state">${esc(tr('svc.noCluster', 'No cluster created by Cluster API on {cluster} yet: create one with "{tab}" in the K8S Clusters tab, then deploy services on it.', { cluster, tab: tr('capi.k8s.create', 'Create a cluster') }))}</p>`);
    }
    const cat = (d.catalog || []).map(c => c.key).concat(['custom']);
    const canDeploy = d.caaph && (d.clusters || []).length;
    parts.push(`<h4>${esc(tr('svc.catalog', 'Catalog'))}</h4>
      <div class="svc-catalog">${cat.map(k => {
        const c = (d.catalog || []).find(x => x.key === k);
        return `<div class="svc-card">
          <div class="svc-card-title">${esc(titleOf(k))}</div>
          <div class="tf-desc">${esc(CATALOG_TEXT[k] ? CATALOG_TEXT[k].desc() : '')}</div>
          <div class="svc-card-foot">
            <code>${c ? esc(`${c.chart} ${c.version}`) : 'helm'}</code>
            <button type="button" class="btn btn-sm btn-primary tip" data-svc-deploy="${esc(k)}" ${canDeploy ? '' : 'disabled'}
                    data-tip="${esc(tr('svc.t.deploy', 'Choose the cluster and the settings, check, then deploy'))}">${Icons.svg('add', { size: 13 })} ${esc(tr('svc.deploy', 'Deploy...'))}</button>
          </div></div>`;
      }).join('')}</div>`);
    const services = d.services || [];
    parts.push(`<h4 style="margin-top:16px;">${esc(tr('svc.deployed', 'Deployed services'))}</h4>`);
    if (!services.length) {
      parts.push(`<p class="empty-state">${esc(tr('svc.none', 'No service deployed yet.'))}</p>`);
    } else {
      parts.push(`<table class="data-table svc-table">
        <thead><tr>
          <th>${esc(tr('svc.col.service', 'Service'))}</th>
          <th>${esc(tr('svc.col.chart', 'Chart'))}</th>
          <th>${esc(tr('svc.col.namespace', 'Namespace in the cluster'))}</th>
          <th>${esc(tr('svc.col.clusters', 'Clusters'))}</th>
          <th>${esc(tr('capi.v.col.actions', 'Actions'))}</th>
        </tr></thead><tbody>${services.map(s => `
          <tr data-svc="${esc(s.namespace)}/${esc(s.name)}">
            <td><strong>${esc(s.name)}</strong><div class="form-hint">${esc(titleOf(s.service))} · ${esc(s.namespace)}</div></td>
            <td><code>${esc(s.chart || '')} ${esc(s.version || tr('svc.latest', 'latest'))}</code></td>
            <td><code>${esc(s.release_namespace || 'default')}</code></td>
            <td class="svc-releases">${(s.releases || []).length ? s.releases.map(releaseBadge).join(' ')
              : `<span class="badge warn tip" data-tip="${esc(s.message || tr('svc.t.noRelease', 'CAAPH has not selected any cluster yet'))}">${Icons.svg('pending', { size: 12 })} ${esc(tr('svc.waiting', 'waiting'))}</span>`}</td>
            <td><button type="button" class="btn btn-sm btn-danger tip" data-svc-remove="${esc(s.namespace)}/${esc(s.name)}"
                        data-tip="${esc(tr('svc.t.remove', 'Uninstall the service from its clusters and delete it'))}">${Icons.svg('delete', { size: 13 })} ${esc(tr('svc.remove', 'Remove'))}</button></td>
          </tr>`).join('')}</tbody></table>`);
    }
    body.innerHTML = parts.join('');
  }

  // -------------------------------------------------------------------------
  // Formulaire
  // -------------------------------------------------------------------------
  function paramFields(key, c) {
    const p = (c && c.params) || {};
    const out = [];
    if ('upstream' in p) {
      out.push(field(tr('svc.f.upstream', 'Upstream resolvers'), `<input data-p="upstream" type="text" value="${esc(p.upstream)}" spellcheck="false">`,
        tr('svc.t.upstream', 'Where names that are not local go, separated by spaces (your router, 1.1.1.1...).')));
    }
    if ('hosts' in p) {
      out.push(field(tr('svc.f.hosts', 'Local records, one per line'), `<textarea data-p="hosts" rows="3" placeholder="172.16.3.60 web.lab" spellcheck="false">${esc(p.hosts)}</textarea>`,
        tr('svc.t.hosts', 'address name, as in /etc/hosts. Answered by this server without asking upstream.')));
    }
    if ('ipam' in p) {
      out.push(field(tr('svc.f.ipam', 'Service address'), `<select data-p="ipam">
          <option value="dhcp" ${p.ipam === 'dhcp' ? 'selected' : ''}>${esc(tr('svc.ipam.dhcp', 'DHCP of the VM network'))}</option>
          <option value="pool" ${p.ipam === 'pool' ? 'selected' : ''}>${esc(tr('svc.ipam.pool', 'Harvester IP pool'))}</option></select>`,
        tr('svc.t.ipam', 'The load balancer of the created cluster takes this address from the network DHCP or from a Harvester IP pool.')));
    }
    if (key === 'custom') {
      out.push(field(tr('svc.f.repo', 'Chart repository'), `<input data-x="repo" type="text" placeholder="https://charts.example.org" spellcheck="false">`,
        tr('svc.t.repo', 'Address of the Helm repository (https://, or oci:// for a registry).')));
      out.push(field('Chart', `<input data-x="chart" type="text" placeholder="nginx" spellcheck="false">`,
        tr('svc.t.chart', 'Name of the chart in that repository.')));
    }
    return out.join('');
  }

  function formHtml(d, key, preset) {
    const c = (d.catalog || []).find(x => x.key === key);
    const clusters = d.clusters || [];
    const c0 = preset.cluster && clusters.includes(preset.cluster) ? preset.cluster : clusters[0];
    const keys = (d.catalog || []).map(x => x.key).concat(['custom']);
    return `
      <div class="tf-form svc-form">
        <p class="tf-desc" data-x="desc">${esc(CATALOG_TEXT[key] ? CATALOG_TEXT[key].desc() : '')}</p>
        <fieldset class="tf-block"><legend>${esc(tr('capi.new.sec.essentials', 'Essentials'))}</legend><div class="tf-args">
          ${field(tr('svc.f.service', 'Service'), `<select data-x="service">${keys.map(k => `<option value="${esc(k)}" ${k === key ? 'selected' : ''}>${esc(titleOf(k))}</option>`).join('')}</select>`,
            tr('svc.t.service', 'What to install: a service of the catalog, or any Helm chart.'))}
          ${field(tr('svc.f.cluster', 'Target cluster'), `<select data-x="cluster">${clusters.map(x => `<option ${x === c0 ? 'selected' : ''}>${esc(x)}</option>`).join('')}</select>`,
            tr('svc.t.cluster', 'A cluster created by Cluster API on this Harvester (namespace/name).'))}
          ${field(tr('svc.f.name', 'Name'), `<input data-x="name" type="text" value="${esc(key === 'custom' ? '' : key)}" autocomplete="off" spellcheck="false">`,
            tr('svc.t.name', 'Name of the service and of its Helm release. Lower case letters, digits and dashes.'))}
          ${field(tr('svc.f.namespace', 'Namespace in the cluster'), `<input data-x="namespace" type="text" value="${esc(c ? c.namespace : 'default')}" spellcheck="false">`,
            tr('svc.t.namespace', 'Where the chart is installed in the created cluster; created if needed.'))}
        </div></fieldset>
        <fieldset class="tf-block"><legend>${esc(tr('svc.sec.settings', 'Settings'))}</legend>
          <div class="tf-args" data-x="params">${paramFields(key, c)}</div></fieldset>
        <details class="tf-block capi-advanced" ${key === 'custom' ? 'open' : ''}>
          <summary class="tip" data-tip="${esc(tr('svc.t.advanced', 'Chart version and values'))}">${esc(tr('capi.new.sec.advanced', 'Advanced options'))}</summary>
          <div class="tf-args">
            ${field(tr('svc.f.version', 'Chart version'), `<input data-x="version" type="text" value="${esc(c ? c.version : '')}" spellcheck="false">`,
              tr('svc.t.version', 'The tested version is proposed. Empty: the latest, which can change without notice.'))}
            ${field(tr('svc.f.values', 'Values (YAML)'), `<textarea data-x="values" rows="8" class="svc-values" spellcheck="false">${esc(c ? c.values : '')}</textarea>`,
              tr('svc.t.values', 'Chart values. __UPSTREAM__, __HOSTS__ and __IPAM__ take the settings above; {{ .Cluster.metadata.name }} becomes the cluster name.'))}
          </div>
        </details>
        <fieldset class="tf-block"><legend>${esc(tr('capi.new.sec.check', 'Pre-check'))}</legend>
          <div class="xfer-report" data-x="report"></div>
          <details class="svc-preview"><summary class="tip" data-tip="${esc(tr('svc.t.preview', 'The values the chart will receive, settings applied'))}">${esc(tr('svc.preview', 'Values sent to the chart'))}</summary>
            <pre class="capi-yaml" data-x="preview"></pre></details>
        </fieldset>
      </div>`;
  }

  function readForm(root) {
    const q = (x) => root.querySelector(`[data-x="${x}"]`);
    const body = {
      service: q('service').value, cluster: q('cluster').value,
      name: q('name').value.trim(), namespace: q('namespace').value.trim(),
      version: q('version').value.trim(), params: {},
    };
    root.querySelectorAll('[data-p]').forEach(el => { body.params[el.dataset.p] = el.value; });
    // Les valeurs du catalogue ne partent que si on les a changées : le
    // serveur garde sinon celles de sa version.
    if (body.service === 'custom' || q('values').dataset.touched) body.values = q('values').value;
    if (body.service === 'custom') {
      body.repo = q('repo').value.trim();
      body.chart = q('chart').value.trim();
    }
    return body;
  }

  function openForm(key, preset = {}) {
    const d = lastData;
    if (!d) return;
    closeModal();
    modal = document.createElement('div');
    modal.className = 'modal-overlay active svc-modal';
    modal.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-header"><div><h3>${Icons.svg('bundle')} ${esc(tr('svc.newTitle', 'Deploy a service'))}</h3>
          <div class="modal-subtitle">${esc(cluster)}</div></div>
          <button type="button" class="btn-close tip" data-svc-close data-tip="${esc(tr('common.cancel', 'Cancel'))}">×</button></div>
        <div class="modal-body" data-x="form-host">${formHtml(d, key, preset)}</div>
        <div class="modal-footer apply-bar" style="margin:0;">
          <button type="button" class="btn btn-secondary btn-sm" data-svc-close>${esc(tr('common.cancel', 'Cancel'))}</button>
          <button type="button" class="btn btn-primary btn-sm tip" data-svc-save disabled
                  data-tip="${esc(tr('svc.t.save', 'Declare the service; CAAPH installs it on the cluster'))}">${Icons.svg('ok', { size: 13 })} ${esc(tr('svc.deployNow', 'Deploy'))}</button>
          <span class="apply-result" data-x="feedback"></span>
        </div>
      </div>`;
    document.body.appendChild(modal);
    const seq = { n: 0, t: null };
    let root = modal.querySelector('.svc-form');
    const later = () => { clearTimeout(seq.t); seq.t = setTimeout(check, 500); };

    function wire() {
      const q = (x) => root.querySelector(`[data-x="${x}"]`);
      q('name').addEventListener('input', () => {
        q('name').dataset.touched = '1';
        q('name').value = q('name').value.toLowerCase().replace(/[^a-z0-9-]/g, '-');
      });
      q('values').addEventListener('input', () => { q('values').dataset.touched = '1'; });
      q('service').addEventListener('change', () => {
        // Un autre service : le formulaire repart de ses valeurs à lui, en
        // gardant le cluster choisi.
        const keep = { cluster: q('cluster').value };
        modal.querySelector('[data-x="form-host"]').innerHTML = formHtml(d, q('service').value, keep);
        root = modal.querySelector('.svc-form');
        wire();
        check();
      });
      root.querySelectorAll('input, select, textarea').forEach(el => {
        if (el.dataset.x === 'service') return;
        el.addEventListener('input', later);
        el.addEventListener('change', later);
      });
    }

    async function check() {
      const n = ++seq.n;
      const report = root.querySelector('[data-x="report"]');
      const body = readForm(root);
      report.classList.add('is-checking');
      try {
        const r = await fetch(`/api/capi/${enc(cluster)}/service-check`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        const out = await r.json();
        if (n !== seq.n || !modal) return;
        if (!r.ok) throw new Error(out.error || `HTTP ${r.status}`);
        const items = (out.findings || []).slice().sort((a, b) => LEVEL_RANK[a.level] - LEVEL_RANK[b.level]);
        report.innerHTML = items.map(f => `<div class="sto-finding ${LEVEL_CLASS[f.level] || ''}" data-code="${esc(f.code)}" data-level="${esc(f.level)}">
            <div class="sto-finding-title">${esc(findingText(f))}</div></div>`).join('')
          + (out.blocked ? '' : `<div class="sto-finding sev-info" data-code="ok"><div class="sto-finding-title">${esc(tr('svc.noBlocker', 'No blocker.'))}</div></div>`);
        root.querySelector('[data-x="preview"]').textContent = out.values || '';
        modal.querySelector('[data-svc-save]').disabled = !!out.blocked;
      } catch (e) {
        if (n !== seq.n || !modal) return;
        report.innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(tr('svc.error', 'Error: {msg}', { msg: e.message }))}</div></div>`;
        modal.querySelector('[data-svc-save]').disabled = true;
      } finally {
        report.classList.remove('is-checking');
      }
    }

    modal.addEventListener('click', async (e) => {
      if (e.target.closest('[data-svc-close]') || e.target === modal) { closeModal(); return; }
      const btn = e.target.closest('[data-svc-save]');
      if (!btn) return;
      btn.disabled = true;
      const body = readForm(root);
      try {
        const r = await fetch(`/api/capi/${enc(cluster)}/service-deploy`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        const out = await r.json();
        if (!r.ok) throw new Error(out.error || out.hint || `HTTP ${r.status}`);
        closeModal();
        follow(out.action_id, tr('svc.done', '{name} installed on {cluster}.', { name: body.name, cluster: body.cluster }));
      } catch (err) {
        modal.querySelector('[data-x="feedback"]').innerHTML = `<span style="color:var(--danger)">${esc(tr('svc.error', 'Error: {msg}', { msg: err.message }))}</span>`;
        btn.disabled = false;
      }
    });
    wire();
    check();
  }

  function closeModal() {
    if (modal) modal.remove();
    modal = null;
  }

  // -------------------------------------------------------------------------
  // Suivi d'une action, retrait
  // -------------------------------------------------------------------------
  function say(html) {
    const fb = host && host.querySelector('.svc-feedback');
    if (fb) fb.innerHTML = html;
  }

  function follow(actionId, doneText) {
    say(esc(tr('svc.started', 'Running (action {id}), see the dock.', { id: actionId })));
    refresh(true);
    if (!window.SSEReconnect) return;
    let lastError = '';
    const es = SSEReconnect.connect(`/api/stream/${enc(actionId)}`, {
      on: {
        step: (e) => {
          try {
            const s = JSON.parse(e.data);
            if (s.status === 'running') { say(esc(s.message || '')); return; }
            if (s.status !== 'error') return;
            // Un refus s'écrit « code {faits} » : la page le redit dans la
            // langue de l'interface.
            const m = /^([a-z0-9-]+) (\{.*\})$/.exec(s.message || '');
            let facts = null;
            try { facts = m ? JSON.parse(m[2]) : null; } catch { facts = null; }
            lastError = facts && FINDINGS[m[1]] ? findingText({ code: m[1], facts }) : (s.message || lastError);
          } catch { /* ligne illisible */ }
        },
        end: (e) => {
          let d = {};
          try { d = JSON.parse(e.data); } catch { /* fin sans détail */ }
          es.close();
          say(d.status === 'done'
            ? `<span style="color:var(--accent)">${Icons.svg('ok', { size: 13 })} ${esc(doneText)}</span>`
            : `<span style="color:var(--danger)">${Icons.svg('fail', { size: 13 })} ${esc(tr('svc.failed', 'Failed: {msg}', { msg: lastError || d.error_summary || d.status || '?' }))}</span>`);
          refresh(true);
        },
      },
    });
  }

  async function remove(ref) {
    const [ns, name] = ref.split('/');
    if (!confirm(tr('svc.confirmRemove', 'Remove service {name}? CAAPH uninstalls it from every cluster it runs on.', { name }))) return;
    try {
      const r = await fetch(`/api/capi/${enc(cluster)}/service/${enc(ns)}/${enc(name)}`, { method: 'DELETE' });
      const out = await r.json();
      if (!r.ok) throw new Error(out.error || out.hint || `HTTP ${r.status}`);
      follow(out.action_id, tr('svc.removed', '{name} removed.', { name }));
    } catch (e) {
      say(`<span style="color:var(--danger)">${esc(tr('svc.error', 'Error: {msg}', { msg: e.message }))}</span>`);
    }
  }

  // -------------------------------------------------------------------------
  // Cycle de vie
  // -------------------------------------------------------------------------
  async function refresh(fresh = false) {
    if (!cluster || !host || !host.isConnected) return;
    const asked = cluster;
    try {
      const r = await fetch(`/api/capi/${enc(asked)}/services${fresh ? '?fresh=1' : ''}`);
      const d = await r.json();
      if (asked !== cluster) return;
      if (!r.ok || d.unreachable || d.error) {
        host.querySelector('.svc-body').innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(d.unreachable
          ? tr('fabric.unreachable', 'Cluster unreachable') : (d.error || 'HTTP ' + r.status))}</div></div>`;
        return;
      }
      lastData = d;
      render(d);
    } catch (e) {
      if (!lastData) host.querySelector('.svc-body').innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(e.message || e)}</div></div>`;
    }
  }

  function shell() {
    host.innerHTML = `<p class="apply-result svc-feedback"></p><div class="svc-body"></div>`;
    host.addEventListener('click', (e) => {
      let el;
      if ((el = e.target.closest('[data-svc-deploy]'))) { openForm(el.dataset.svcDeploy); return; }
      if ((el = e.target.closest('[data-svc-remove]'))) { remove(el.dataset.svcRemove); return; }
      if ((el = e.target.closest('[data-svc-goto]')) && window.CAPI && CAPI.selectCapiTab) CAPI.selectCapiTab(el.dataset.svcGoto);
    });
  }

  function visible() {
    return host && host.isConnected && host.offsetParent !== null && !document.hidden;
  }

  function start() {
    const h = document.querySelector('#capi-services-body');
    const c = document.querySelector('#cluster-select')?.value;
    if (!h) return Promise.resolve();
    if (!c) { h.innerHTML = `<p class="form-hint">${esc(tr('capi.new.pickCluster', 'Select a cluster'))}</p>`; return Promise.resolve(); }
    if (cluster !== c) lastData = null;
    cluster = c;
    if (host !== h || !h.querySelector('.svc-body')) { host = h; shell(); }
    if (!lastData) host.querySelector('.svc-body').innerHTML = `<p class="hint">${esc(tr('common.loading', 'Loading...'))}</p>`;
    if (!timer) timer = setInterval(() => { if (!modal && visible()) refresh(); }, REFRESH_MS);
    return refresh(true);
  }

  return { start, refresh, openForm, _findingText: findingText };
})();

if (typeof window !== 'undefined') window.CapiServices = CapiServices;
