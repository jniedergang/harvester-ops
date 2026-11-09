/**
 * harvester-ops : onglet « Secrets externes » de Cluster API (2.10.0, tâche 10).
 *
 * External Secrets Operator (ESO) dans un cluster enfant : choix du cluster
 * enfant, état (chart, OpenBao, authentification, empreinte des clés,
 * namespaces activés, magasins `SecretStore`, `ExternalSecret` et leur
 * dernière synchronisation), activation et retrait d'un namespace, écriture
 * d'une valeur (champs effacés dès la demande, jamais réaffichés), exemple
 * d'`ExternalSecret` à copier, retrait d'ESO (saisie du nom du cluster).
 * La fenêtre « Mettre en place » a trois onglets (Paramètres, Vérifications,
 * Résumé) : chaque saisie relance la vérification (`dry_run`, lecture seule)
 * et le bouton Appliquer reste grisé, avec la raison, tant qu'elle n'est pas
 * revenue sans bloquant. Le serveur lance `harvester-capi eso-*` (parité CLI,
 * actions suivies, dock et Activité).
 */
const CapiEso = (() => {
  const enc = encodeURIComponent;
  const B = () => window.Board;
  const tr = (k, f, v) => B().tr(k, f, v);
  const esc = (v) => B().esc(v);
  const icon = (n, size) => (window.Icons ? Icons.svg(n, { size: size || 14 }) : '');
  const cluster = () => (window.App && App.managementCluster ? App.managementCluster() : '');
  const ROLE_RANK = { viewer: 1, operator: 2, admin: 3 };
  const can = (need) => (ROLE_RANK[window.App && App.role ? App.role() : null] || 0) >= ROLE_RANK[need];
  const NS_RE = /^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$/;
  const FIELD_RE = /^[A-Za-z0-9._-]{1,253}$/;
  const FIELDS_MAX = 32;
  const cur = { child: '', doc: null, seq: 0 };
  let modal = null;

  // Un appel littéral par constat (contrôle de parité des traductions)
  const FINDINGS = {
    'eso-needs-openbao': (v) => tr('eso.fd.eso-needs-openbao', 'External Secrets need OpenBao: enable it first in Settings > Secrets.', v),
    'eso-admin-only': (v) => tr('eso.fd.eso-admin-only', 'External Secrets are set up by an administrator.', v),
    'eso-cluster-missing': (v) => tr('eso.fd.eso-cluster-missing', 'This child cluster does not exist (any more).', v),
    'eso-no-namespace': (v) => tr('eso.fd.eso-no-namespace', 'Give at least one team namespace.', v),
    'eso-chart-missing': (v) => tr('eso.fd.eso-chart-missing', 'The chart {service} is not installed on this child cluster: choose a package that carries it.', v),
    'eso-chart-not-locked': (v) => tr('eso.fd.eso-chart-not-locked', 'The chart {service} was installed without the values the console imposes: apply it again from the console.', v),
    'eso-listener-needed': (v) => tr('eso.fd.eso-listener-needed', 'OpenBao on the console host only listens locally: give the address of its listener for the child clusters in Settings > Secrets.', v),
    'eso-openbao-not-https': (v) => tr('eso.fd.eso-openbao-not-https', 'OpenBao is reached at {endpoint}, which is not HTTPS: the child clusters refuse it.', v),
    'eso-openbao-unreachable': (v) => tr('eso.fd.eso-openbao-unreachable', 'OpenBao at {endpoint} does not answer from the network of the child cluster ({network}): {failed}.', v),
    'eso-openbao-unchecked': (v) => tr('eso.fd.eso-openbao-unchecked', 'OpenBao at {endpoint} could not be checked from the child cluster: {reason}.', v),
    'eso-system-namespace': (v) => tr('eso.fd.eso-system-namespace', 'The namespace {namespace} is a system namespace: choose a team namespace.', v),
    'eso-namespace-invalid': (v) => tr('eso.fd.eso-namespace-invalid', 'The namespace {namespace} is not a valid name (lower case RFC 1123).', v),
    'child-admin-required': (v) => tr('eso.fd.child-admin-required', 'You cannot read the kubeconfig Secret of {cluster}: only an administrator of the child cluster sets External Secrets on it.', v),
    'child-admin-unknown': (v) => tr('eso.fd.child-admin-unknown', 'Your right on {cluster} could not be checked: {message}', v),
    // source du chart : constats partagés avec l'onglet Services
    'service-registry-unknown': (v) => tr('svc.fd.service-registry-unknown', 'The registry {registry} is not declared in the console.', v),
    'service-registry-no-charts': (v) => tr('svc.fd.service-registry-no-charts', 'The registry {registry} does not serve charts.', v),
    'service-registry-not-attached': (v) => tr('svc.fd.service-registry-not-attached', 'The registry {registry} is not attached to {cluster}: CAAPH would have no read account.', v),
    'service-package-missing': (v) => tr('svc.fd.service-package-missing', 'The package {package} is not imported in the console.', v),
    'service-item-not-chart': (v) => tr('svc.fd.service-item-not-chart', 'The package {package} has no chart named {item}.', v),
    'service-chart-not-published': (v) => tr('svc.fd.service-chart-not-published', 'The package {package} is not marked as published in {registry}: publish it first, or the chart may be missing.', v),
    'service-registry-not-mirrored': (v) => tr('svc.fd.service-registry-not-mirrored', 'The cluster {cluster} does not pull its images from {registry}: the images of the chart will come from their origin.', v),
    'invalid-values': (v) => tr('svc.fd.invalid-values', 'The values are not valid YAML: {message}', v),
  };
  // Codes d'erreur des routes d'écriture et de namespace
  const ERRORS = {
    'eso-namespace-not-enabled': () => tr('eso.err.eso-namespace-not-enabled', 'This namespace is not enabled for External Secrets on this child cluster.'),
    'eso-needs-openbao': () => tr('eso.err.eso-needs-openbao', 'External Secrets need OpenBao: enable it first in Settings > Secrets.'),
    'eso-not-set': () => tr('eso.err.eso-not-set', 'External Secrets are not set up on this child cluster yet.'),
    'eso-last-namespace': () => tr('eso.err.eso-last-namespace', 'This is the last enabled namespace: remove External Secrets instead.'),
    'openbao-denied': () => tr('eso.err.openbao-denied', 'OpenBao refused the write: your rights do not cover this secret.'),
    'openbao-person-login-failed': () => tr('eso.err.openbao-person-login-failed', 'Your sign-in to OpenBao failed: sign in to the console again.'),
    'openbao-person-no-token': () => tr('eso.err.openbao-person-no-token', 'Your session carries no token for OpenBao: sign in to the console again.'),
    'openbao-person-expired': () => tr('eso.err.openbao-person-expired', 'Your OpenBao token has expired: sign in to the console again.'),
  };
  const LEVEL_CLASS = { block: 'sev-critical', warn: 'sev-watch', ok: 'sev-info' };
  const findingText = (f) => {
    const fn = FINDINGS[f && f.code];
    const facts = Object.assign({}, (f && f.facts) || {});
    return fn ? fn(facts) : String((f && f.code) || '');
  };
  const findingsHtml = (fs) => (fs || []).map(f => `<div class="sto-finding ${LEVEL_CLASS[f.level] || 'sev-critical'}" data-code="${esc(f.code)}"
      data-level="${esc(f.level)}"><div class="sto-finding-title">${esc(findingText(f))}</div></div>`).join('');
  /** Texte d'une réponse refusée : code connu, sinon message du serveur. */
  const errText = (r, facts) => {
    const code = r && r.d && r.d.code;
    if (code && ERRORS[code]) return ERRORS[code]();
    if (code && FINDINGS[code]) return findingText({ code, facts: facts || {} });
    return (r && r.d && (r.d.error || r.d.code)) || `HTTP ${r ? r.status : 0}`;
  };

  async function json(method, url, body) {
    const r = await fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined });
    const d = await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, d };
  }
  const tipAttr = (t) => `data-tip="${esc(t)}" title="${esc(t)}" aria-label="${esc(t)}"`;
  const btn = (attrs, cls, ic, label, tip, disabled) => `<button type="button" class="btn btn-sm ${cls} tip" ${attrs}
      ${tipAttr(tip)}${disabled ? ' disabled' : ''}>${ic ? icon(ic, 13) : ''} ${esc(label)}</button>`;
  const field = (label, input, tip) => `<label class="tf-field"><span class="tf-label">${esc(label)}</span>${input
    .replace(/<(input|select|textarea)/, `<$1 class="tip" data-tip="${esc(tip)}" title="${esc(tip)}"`)}</label>`;
  const adminTip = () => tr('eso.t.adminOnly', 'Reserved to administrators');
  const childPath = () => {
    const [ns, name] = cur.child.split('/');
    return `/api/capi/${enc(cluster())}/eso/${enc(ns)}/${enc(name)}`;
  };
  function when(ts) {
    if (!ts) return '–';
    const d = new Date(typeof ts === 'number' ? ts * 1000 : ts);
    return Number.isNaN(d.getTime()) ? String(ts) : d.toLocaleString();
  }
  const MODE = {
    a: () => tr('eso.mode.a', 'OpenBao of your organisation (A)'),
    b1: () => tr('eso.mode.b1', 'OpenBao on the console host (B1)'),
    local: () => tr('eso.mode.local', 'No OpenBao (local store)'),
  };
  const modeText = (m) => (MODE[m] ? MODE[m]() : String(m || '–'));
  const yesNo = (ok, yes, no, tip) => `<span class="badge ${ok ? 'ok' : 'warn'} tip" data-tip="${esc(tip)}">${esc(ok ? yes : no)}</span>`;

  /** YAML de l'exemple (objets, listes, textes : ce que rend le serveur). */
  function yaml(v, ind) {
    const pad = '  '.repeat(ind || 0);
    const scalar = (x) => (typeof x === 'string' ? (/^[\w./:@-]+$/.test(x) && !/^(true|false|null|\d+)$/.test(x) ? x : JSON.stringify(x)) : JSON.stringify(x));
    if (Array.isArray(v)) {
      return v.map(x => (x && typeof x === 'object'
        ? `${pad}- ${yaml(x, (ind || 0) + 1).trimStart()}` : `${pad}- ${scalar(x)}`)).join('\n');
    }
    if (v && typeof v === 'object') {
      return Object.keys(v).map(k => {
        const x = v[k];
        if (x && typeof x === 'object' && Object.keys(x).length) return `${pad}${k}:\n${yaml(x, (ind || 0) + 1)}`;
        return `${pad}${k}: ${scalar(x)}`;
      }).join('\n');
    }
    return pad + scalar(v);
  }

  function copy(textValue, el) {
    const done = () => { if (el) { el.classList.add('copied'); setTimeout(() => el.classList.remove('copied'), 1500); } };
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(textValue).then(done, () => {});
    else {
      const ta = document.createElement('textarea');
      ta.value = textValue;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); done(); } catch { /* sélection laissée */ }
      ta.remove();
    }
  }

  function say(text, bad) {
    const el = document.getElementById('eso-feedback');
    if (!el) return;
    el.innerHTML = text ? `<span class="${bad ? 'res-error' : ''}">${esc(text)}</span>` : '';
  }

  /** Suit une action (SSE) puis relit l'état. */
  function follow(actionId, startText, doneText) {
    say(tr('eso.running', '{what} (action {id}), see the dock.', { what: startText, id: actionId }));
    if (window.Dock && Dock.poll) Dock.poll();
    if (!window.SSEReconnect) { setTimeout(load, 1500); return; }
    const es = SSEReconnect.connect(`/api/stream/${enc(actionId)}`, {
      on: {
        end: (ev) => {
          let x = {};
          try { x = JSON.parse(ev.data); } catch { /* fin sans détail */ }
          es.close();
          if (x.status === 'done') say(doneText);
          else say(tr('eso.failed', 'The action failed: see the dock and the Activity tab.'), true);
          setTimeout(load, 300);
        },
      },
    });
  }

  // -------------------------------------------------------------------------
  // Choix du cluster enfant puis état
  // -------------------------------------------------------------------------
  async function start() {
    const box = document.getElementById('capi-eso-body');
    if (!box) return;
    const c = cluster();
    if (!c) { box.innerHTML = ''; return; }
    const mine = ++cur.seq;
    box.innerHTML = `<p class="form-hint">${esc(tr('common.loading', 'Loading…'))}</p>`;
    const dg = await json('GET', `/api/capi/${enc(c)}/diag`).catch(() => null);
    if (mine !== cur.seq) return;
    const children = ((dg && dg.ok && dg.d.capi_clusters) || []).map(x => `${x.namespace}/${x.name}`).sort();
    if (!children.length) {
      box.innerHTML = `<p class="empty-state">${esc(tr('eso.noChild', 'No child cluster on {cluster} yet: create one in the Clusters tab first.', { cluster: c }))}</p>`;
      return;
    }
    if (!children.includes(cur.child)) cur.child = children[0];
    box.innerHTML = `<p class="form-hint eso-intro">${esc(tr('eso.intro', 'External Secrets Operator syncs values kept in OpenBao into Kubernetes Secrets of team namespaces of a child cluster. The console sets the operator, its access to OpenBao and a store per enabled namespace; the teams write their ExternalSecret objects.'))}</p>
      <div class="eso-picker">${field(tr('eso.f.child', 'Child cluster'), `<select data-eso="child">${children.map(x => `<option value="${esc(x)}"${x === cur.child ? ' selected' : ''}>${esc(x)}</option>`).join('')}</select>`,
        tr('eso.t.child', 'A cluster created by Cluster API on this Harvester cluster (namespace/name)'))}</div>
      <div class="res-feedback" id="eso-feedback" role="status" aria-live="polite"></div>
      <div id="capi-eso-state"></div>`;
    load();
  }

  async function load() {
    const box = document.getElementById('capi-eso-state');
    if (!box || !cur.child) return;
    const mine = ++cur.seq;
    box.classList.add('is-checking');
    const r = await json('GET', childPath()).catch((e) => ({ ok: false, status: 0, d: { error: e.message } }));
    if (mine !== cur.seq) return;
    box.classList.remove('is-checking');
    if (!r.ok) {
      cur.doc = null;
      box.innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(tr('eso.loadError', 'The External Secrets state could not be read: {error}', { error: errText(r) }))}</div></div>`;
      return;
    }
    cur.doc = r.d;
    box.innerHTML = stateHtml(r.d);
  }

  function chartBadge(ch) {
    ch = ch || {};
    const svc = 'eso';
    if (!ch.present) return `<span class="badge warn tip" data-eso-chart="absent" data-tip="${esc(tr('eso.t.chartAbsent', 'The chart {service} is not installed: the set-up window installs it from a package.', { service: svc }))}">${esc(tr('eso.chart.absent', 'not installed'))}</span>`;
    const bits = [ch.ready
      ? `<span class="badge ok tip" data-eso-chart="ready" data-tip="${esc(tr('eso.t.chartReady', 'The operator runs and its resource types are known'))}">${esc(tr('eso.chart.ready', 'ready'))}</span>`
      : `<span class="badge warn tip" data-eso-chart="pending" data-tip="${esc(tr('eso.t.chartPending', 'Installed, not ready yet'))}">${esc(tr('eso.chart.pending', 'not ready'))}</span>`];
    bits.push(ch.locked
      ? `<span class="badge dim tip" data-tip="${esc(tr('eso.t.locked', 'Installed with the values the console imposes (no cluster-wide store, no access outside the enabled namespaces)'))}">${esc(tr('eso.chart.locked', 'locked'))}</span>`
      : `<span class="badge warn tip" data-tip="${esc(tr('eso.t.notLocked', 'Installed without the values the console imposes: apply it again from the console'))}">${esc(tr('eso.chart.notLocked', 'not locked'))}</span>`);
    return bits.join(' ');
  }

  function authHtml(a) {
    if (!a) return `<span class="form-hint">${esc(tr('eso.auth.none', 'not set up'))}</span>`;
    if (!a.applied) {
      return `<span class="badge warn tip" data-eso-auth="waiting" data-tip="${esc(tr('eso.t.authWaiting', 'The commands given by the set-up must be run by the administrator of OpenBao'))}">${esc(tr('eso.auth.waiting', 'waiting for the OpenBao administrator'))}</span>`;
    }
    const by = a.by ? tr('eso.auth.by', 'by {by}, {when}', { by: a.by, when: when(a.at) }) : when(a.at);
    return `<span class="badge ok" data-eso-auth="applied">${esc(tr('eso.auth.applied', 'applied'))}</span> <span class="form-hint">${esc(by)}</span>`;
  }

  function stateHtml(d) {
    const admin = can('admin');
    const a = d.auth;
    const fp = a && a.keys_sha256 ? String(a.keys_sha256) : '';
    const kv = (k, v, tip) => `<dt>${esc(k)}</dt><dd${tip ? ` class="tip" data-tip="${esc(tip)}"` : ''}>${v}</dd>`;
    const warn = [];
    if (d.error) warn.push({ code: d.error, level: 'block', facts: { cluster: d.cluster } });
    if (!d.openbao) warn.push({ code: 'eso-needs-openbao', level: 'block', facts: {} });
    const crs = d.crs ? (d.crs.applied
      ? `<span class="badge ok">${esc(tr('eso.crs.applied', 'applied'))}</span>`
      : `<span class="badge warn">${esc(tr('eso.crs.pending', 'pending'))}</span>`) + (d.crs.message && d.crs.message !== 'applied' ? ` <span class="form-hint">${esc(d.crs.message)}</span>` : '') : '–';
    const facts = `<dl class="kv sx-kv eso-kv">
      ${kv(tr('eso.k.chart', 'Operator (chart eso)'), chartBadge(d.chart))}
      ${kv(tr('eso.k.openbao', 'OpenBao'), `${esc(modeText(d.mode))}${d.endpoint ? ` · <code>${esc(d.endpoint)}</code>` : ''}`, tr('eso.t.openbao', 'The OpenBao the child cluster reads its values from'))}
      ${kv(tr('eso.k.auth', 'Access to OpenBao'), authHtml(a), tr('eso.t.auth', 'The authentication of the child cluster in OpenBao, by the keys of its service accounts'))}
      ${kv(tr('eso.k.keys', 'Key fingerprint'), fp ? `<code class="tip" data-eso-fp data-tip="${esc(fp)}">${esc(fp.slice(0, 12))}</code>` : '–', tr('eso.t.keys', 'SHA-256 of the service account keys of the child cluster given to OpenBao'))}
      ${kv(tr('eso.k.crs', 'Store objects'), crs, tr('eso.t.crs', 'The objects the console poses in the enabled namespaces (ClusterResourceSet)'))}
    </dl>`;
    const actions = `<div class="apply-bar sx-actions eso-actions">
      ${btn('data-eso-act="setup"', 'btn-primary', 'lock', tr('eso.setup', 'Set up External Secrets'), admin
        ? tr('eso.t.setup', 'Install or update the operator, its access to OpenBao and the stores: parameters, checks, summary') : adminTip(), !admin)}
      ${btn('data-eso-act="remove"', 'btn-danger', 'trash', tr('eso.remove', 'Remove External Secrets'), admin
        ? tr('eso.t.remove', 'Remove the stores, the access to OpenBao and (unless kept) the chart; synced Secrets stay at their last value') : adminTip(), !admin || !a)}
      ${btn('data-eso-act="refresh"', 'btn-secondary', 'refresh', tr('overview.refresh', 'Refresh'), tr('eso.t.refresh', 'Read the state again'))}
    </div>`;
    return `${findingsHtml(warn)}${facts}${actions}${namespacesHtml(d)}${storesHtml(d.stores)}${esHtml(d.externalsecrets)}${writeHtml(d)}${exampleHtml(d.example)}`;
  }

  function namespacesHtml(d) {
    const admin = can('admin');
    const set = !!d.auth;
    const chips = (d.namespaces || []).map(n => `<span class="res-chip eso-ns" data-eso-ns="${esc(n)}">${esc(n)}
      <button type="button" class="btn-close tip" data-eso-ns-del="${esc(n)}" ${tipAttr(admin
        ? tr('eso.t.nsRemove', 'Disable {namespace}: its store is removed, its synced Secrets stay at their last value', { namespace: n }) : adminTip())}${admin ? '' : ' disabled'}>×</button></span>`).join('');
    const off = !admin || !set;
    const tip = !admin ? adminTip() : !set ? tr('eso.t.nsNotSet', 'Set up External Secrets first') : tr('eso.t.nsAdd', 'Enable this team namespace: the console poses a store in it');
    return `<div class="card sx-card eso-card"><h5>${icon('namespace', 14)} ${esc(tr('eso.nsTitle', 'Enabled namespaces'))}</h5>
      <div class="eso-chips">${chips || `<span class="form-hint">${esc(tr('eso.nsNone', 'No namespace enabled yet.'))}</span>`}</div>
      <div class="eso-inline">
        <input type="text" class="tip" data-eso-ns-input spellcheck="false" autocomplete="off" placeholder="equipe-a" ${tipAttr(tr('eso.t.nsInput', 'A team namespace of the child cluster (lower case RFC 1123), never a system namespace'))}${off ? ' disabled' : ''}>
        ${btn('data-eso-ns-add', 'btn-secondary', 'add', tr('eso.nsAdd', 'Enable a namespace'), tip, off)}
      </div></div>`;
  }

  function readyBadge(ok, reason) {
    return `<span class="badge ${ok ? 'ok' : 'warn'} tip" data-tip="${esc(reason || (ok ? tr('eso.ready', 'ready') : tr('eso.notReady', 'not ready')))}">${esc(ok ? tr('eso.ready', 'ready') : tr('eso.notReady', 'not ready'))}</span>`;
  }

  function storesHtml(list) {
    const rows = (list || []).map(s => `<tr class="eso-store" data-ns="${esc(s.ns)}"><td><code>${esc(s.ns)}</code></td><td>${esc(s.name)}</td>
      <td>${readyBadge(!!s.ready, s.reason)}</td><td>${esc(s.reason || '')}</td><td class="eso-msg">${esc(s.message || '')}</td></tr>`).join('');
    return `<div class="card sx-card eso-card"><h5>${icon('storage', 14)} ${esc(tr('eso.storesTitle', 'Stores (SecretStore)'))}</h5>
      ${rows ? `<div class="eso-scroll"><table class="data-table eso-table eso-stores"><thead><tr>
        <th>${esc(tr('eso.col.ns', 'Namespace'))}</th><th>${esc(tr('eso.col.name', 'Name'))}</th><th>${esc(tr('eso.col.ready', 'Ready'))}</th>
        <th>${esc(tr('eso.col.reason', 'Reason'))}</th><th>${esc(tr('eso.col.message', 'Message'))}</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : `<p class="form-hint">${esc(tr('eso.storesNone', 'No store yet.'))}</p>`}</div>`;
  }

  function esHtml(list) {
    const rows = (list || []).map(e => `<tr class="eso-es" data-ns="${esc(e.ns)}"><td><code>${esc(e.ns)}</code></td><td><strong>${esc(e.name)}</strong></td>
      <td>${readyBadge(!!e.ready, e.message || e.reason)}</td><td>${esc(e.reason || '')}</td><td>${esc(when(e.synced_at))}</td>
      <td><code>${esc(e.target || '')}</code></td></tr>`).join('');
    return `<div class="card sx-card eso-card"><h5>${icon('key', 14)} ${esc(tr('eso.esTitle', 'ExternalSecret objects'))}</h5>
      ${rows ? `<div class="eso-scroll"><table class="data-table eso-table eso-externalsecrets"><thead><tr>
        <th>${esc(tr('eso.col.ns', 'Namespace'))}</th><th>${esc(tr('eso.col.name', 'Name'))}</th><th>${esc(tr('eso.col.ready', 'Ready'))}</th>
        <th>${esc(tr('eso.col.reason', 'Reason'))}</th><th>${esc(tr('eso.col.synced', 'Last sync'))}</th><th>${esc(tr('eso.col.target', 'Target Secret'))}</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : `<p class="form-hint">${esc(tr('eso.esNone', 'No ExternalSecret yet: the teams pose them in their namespace (see the example below).'))}</p>`}</div>`;
  }

  function fieldRow() {
    return `<div class="eso-field-row">
      <input type="text" class="tip" data-eso-fk spellcheck="false" autocomplete="off" placeholder="password" ${tipAttr(tr('eso.t.fieldName', 'Field name: letters, digits, dot, dash or underscore'))}>
      <input type="password" class="tip" data-eso-fv autocomplete="new-password" ${tipAttr(tr('eso.t.fieldValue', 'Sent once, never read back nor shown again'))}>
      ${btn('data-eso-field-del', 'btn-secondary', '', '×', tr('eso.t.fieldDel', 'Remove this field'))}</div>`;
  }

  function writeHtml(d) {
    const nss = d.namespaces || [];
    const op = can('operator');
    const off = !op || !nss.length || !d.openbao;
    const tip = !op ? tr('eso.t.writeOperator', 'Writing a value needs the operator role')
      : !nss.length ? tr('eso.t.writeNoNs', 'Enable a namespace first') : !d.openbao ? findingText({ code: 'eso-needs-openbao' })
        : tr('eso.t.write', 'Write the value in OpenBao with your token; the fields are cleared at once and never shown again');
    return `<div class="card sx-card eso-card eso-write"><h5>${icon('edit', 14)} ${esc(tr('eso.writeTitle', 'Write a value'))}</h5>
      <p class="form-hint">${esc(tr('eso.writeIntro', 'Writing replaces all the fields of this secret. The values are sent once, never read back nor shown again.'))}</p>
      <div class="tf-args">
        ${field(tr('eso.f.namespace', 'Namespace'), `<select data-eso-w="namespace">${nss.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join('')}</select>`,
          tr('eso.t.wNamespace', 'An enabled namespace: only its store reads the value'))}
        ${field(tr('eso.f.secret', 'Secret name'), '<input type="text" data-eso-w="name" spellcheck="false" autocomplete="off" placeholder="db">',
          tr('eso.t.wName', 'Name of the secret in OpenBao (lower case RFC 1123), read by an ExternalSecret'))}
      </div>
      <div class="eso-field-head" aria-hidden="true"><span class="tf-label">${esc(tr('eso.f.fieldName', 'Field'))}</span><span class="tf-label">${esc(tr('eso.f.fieldValue', 'Value'))}</span><span></span></div>
      <div class="eso-fields" data-eso-fields>${fieldRow()}</div>
      <div class="apply-bar sx-actions">
        ${btn('data-eso-field-add', 'btn-secondary', 'add', tr('eso.fieldAdd', 'Add a field'), tr('eso.t.fieldAdd', 'At most {n} fields', { n: FIELDS_MAX }))}
        ${btn('data-eso-write', 'btn-primary', 'save', tr('eso.write', 'Write'), tip, off)}
        <span class="apply-result" data-eso-write-result role="status" aria-live="polite"></span>
      </div></div>`;
  }

  function exampleHtml(ex) {
    if (!ex) return '';
    const text = yaml(ex);
    return `<div class="card sx-card eso-card eso-example"><h5>${icon('notes', 14)} ${esc(tr('eso.exampleTitle', 'Example to copy'))}
        ${btn('data-eso-copy', 'btn-secondary', 'copy', tr('eso.copy', 'Copy'), tr('eso.t.copy', 'Copy this ExternalSecret to the clipboard'))}</h5>
      <p class="form-hint">${esc(tr('eso.exampleIntro', 'An ExternalSecret for the first enabled namespace; it refreshes the Secret every hour. The console never poses it: the team applies it.'))}</p>
      <pre class="sx-cmds eso-yaml" data-eso-example>${esc(text)}</pre></div>`;
  }

  // -------------------------------------------------------------------------
  // Gestes
  // -------------------------------------------------------------------------
  async function nsGesture(method, team) {
    const r = await json(method, `${childPath()}/namespaces/${enc(team)}`).catch((e) => ({ ok: false, status: 0, d: { error: e.message } }));
    if (r.ok && r.d.unchanged) { say(tr('eso.unchanged', 'Nothing to change.')); return; }
    if (!r.ok || !r.d.action_id) { say(errText(r, { namespace: team }), true); return; }
    follow(r.d.action_id, method === 'POST' ? tr('eso.nsAdding', 'Enabling {namespace}', { namespace: team })
      : tr('eso.nsRemoving', 'Disabling {namespace}', { namespace: team }), tr('eso.done', 'Done.'));
  }

  async function writeValue(box) {
    const w = (k) => box.querySelector(`[data-eso-w="${k}"]`);
    const out = box.querySelector('[data-eso-write-result]');
    const team = w('namespace').value;
    const name = w('name').value.trim();
    const rows = [...box.querySelectorAll('.eso-field-row')];
    let fields = {};
    let why = '';
    if (!NS_RE.test(name)) why = tr('eso.r.secret', 'The secret name: a lower case RFC 1123 name.');
    rows.forEach(rw => {
      const k = rw.querySelector('[data-eso-fk]').value.trim();
      const v = rw.querySelector('[data-eso-fv]').value;
      if (!k && !v) return;
      if (!FIELD_RE.test(k) || !v) why = why || tr('eso.r.fields', 'Each field: a name of letters, digits, dot, dash or underscore, and a value.');
      fields[k] = v;
    });
    if (!why && !Object.keys(fields).length) why = tr('eso.r.fieldsNone', 'Give at least one field.');
    if (why) { out.className = 'apply-result res-error'; out.textContent = why; return; }
    const pending = json('POST', `${childPath()}/secrets`, { namespace: team, name, fields })
      .catch((e) => ({ ok: false, status: 0, d: { error: e.message } }));
    // jamais réaffichés : les champs sont vidés dès la demande partie
    fields = null;
    box.querySelector('[data-eso-fields]').innerHTML = fieldRow();
    out.className = 'apply-result';
    out.textContent = tr('eso.writing', 'Writing…');
    const r = await pending;
    if (!r.ok) { out.className = 'apply-result res-error'; out.textContent = errText(r); return; }
    const who = r.d.identity === 'person' ? tr('eso.identity.person', 'person token') : tr('eso.identity.service', 'service account of the console');
    out.textContent = tr('eso.written', 'Version {version} written ({identity}).', { version: r.d.version, identity: who });
    if (window.Dock && Dock.poll) Dock.poll();
  }

  function closeModal() {
    if (modal && modal._cleanup) modal._cleanup();
    if (modal) modal.remove();
    modal = null;
  }

  function openRemove() {
    const ref = cur.child;
    closeModal();
    modal = document.createElement('div');
    modal.className = 'modal-overlay active reg-modal eso-remove-modal';
    modal.innerHTML = `<div class="modal" role="dialog" aria-modal="true">
        <div class="modal-header"><h3>${icon('trash')} ${esc(tr('eso.remove', 'Remove External Secrets'))}</h3>
          <button type="button" class="btn-close tip" data-eso-close ${tipAttr(tr('common.cancel', 'Cancel'))}>×</button></div>
        <div class="modal-body">
          <p>${esc(tr('eso.removeIntro', 'The stores of the enabled namespaces and the access of {cluster} to OpenBao are removed. The synced Secrets stay at their last value; the values stay in OpenBao.', { cluster: ref }))}</p>
          <label class="tf-field tf-type-bool tip" data-tip="${esc(tr('eso.t.keepChart', 'Leave the operator installed (other uses of it keep running)'))}"><input type="checkbox" data-eso-keep> <span class="tf-label">${esc(tr('eso.keepChart', 'Keep the eso chart'))}</span></label>
          ${field(tr('eso.typeCluster', 'Type {cluster} to confirm', { cluster: ref }), `<input type="text" data-eso-typed spellcheck="false" autocomplete="off" placeholder="${esc(ref)}">`,
            tr('eso.t.typed', 'Type the name of the child cluster exactly'))}
        </div>
        <div class="modal-footer apply-bar" style="margin:0;">
          ${btn('data-eso-close', 'btn-secondary', '', tr('common.cancel', 'Cancel'), tr('eso.t.close', 'Close this window'))}
          ${btn('data-eso-remove-go', 'btn-danger', 'trash', tr('eso.removeGo', 'Remove'), tr('eso.t.removeGo', 'Active once the name is typed exactly'), true)}
        </div></div>`;
    document.body.appendChild(modal);
    const m = modal;
    const go = m.querySelector('[data-eso-remove-go]');
    m.querySelector('[data-eso-typed]').addEventListener('input', (e) => { go.disabled = e.target.value.trim() !== ref || !can('admin'); });
    m.addEventListener('click', async (e) => {
      if (e.target === m || e.target.closest('[data-eso-close]')) { closeModal(); return; }
      if (!e.target.closest('[data-eso-remove-go]') || go.disabled) return;
      go.disabled = true;
      const keep = m.querySelector('[data-eso-keep]').checked;
      const r = await json('DELETE', childPath() + (keep ? '?keep_chart=1' : '')).catch((x) => ({ ok: false, status: 0, d: { error: x.message } }));
      closeModal();
      if (!r.ok || !r.d.action_id) { say(errText(r), true); return; }
      follow(r.d.action_id, tr('eso.removing', 'Removing External Secrets from {cluster}', { cluster: ref }), tr('eso.removed', 'External Secrets removed.'));
    });
  }

  async function openSetup() {
    const c = cluster();
    const d = cur.doc || {};
    const ref = cur.child;
    if (!c || !ref) return;
    const [ar, rg] = await Promise.all([
      json('GET', '/api/artefacts').catch(() => null),
      json('GET', '/api/registries').catch(() => null),
    ]);
    const pkgs = (ar && ar.ok && ar.d.packages) || [];
    const regs = ((rg && rg.ok && rg.d.registries) || []).filter(x => (x.clusters || []).includes(c));
    const tabs = ['params', 'checks', 'summary'];
    const TAB = { params: tr('reg.tab.params', 'Parameters'), checks: tr('reg.tab.checks', 'Checks'), summary: tr('reg.tab.summary', 'Summary') };
    const TAB_TIP = {
      params: tr('eso.t.tabParams', 'Team namespaces and, if needed, the package that carries the eso chart'),
      checks: tr('eso.t.tabChecks', 'What the console checked on the child cluster and on OpenBao for these values'),
      summary: tr('eso.t.tabSummary', 'What will be posed, before Apply'),
    };
    const opt = (v, label, sel) => `<option value="${esc(v)}"${sel ? ' selected' : ''}>${esc(label)}</option>`;
    closeModal();
    modal = document.createElement('div');
    modal.className = 'modal-overlay active reg-modal eso-modal';
    modal.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-header"><div><h3>${icon('lock')} ${esc(tr('eso.setup', 'Set up External Secrets'))}</h3>
          <div class="modal-subtitle">${esc(tr('eso.setupSub', 'On the child cluster {cluster}', { cluster: ref }))}</div></div>
          <button type="button" class="btn-close tip" data-eso-close ${tipAttr(tr('common.cancel', 'Cancel'))}>×</button></div>
        <div class="modal-body">
          <div class="sub-tabs sub-tabs-inline eso-tabs" role="tablist">${tabs.map((t, i) => `<button type="button" role="tab"
            class="sub-tab tip${i ? '' : ' active'}" data-eso-tab="${t}" data-tip="${esc(TAB_TIP[t])}" title="${esc(TAB_TIP[t])}">${esc(TAB[t])}${t === 'checks'
              ? ' <span class="badge dim" data-x="count"></span>' : ''}</button>`).join('')}</div>
          <section data-eso-pane="params"><div class="tf-args">
            ${field(tr('eso.f.namespaces', 'Team namespaces'), `<input data-f="namespaces" type="text" spellcheck="false" autocomplete="off" placeholder="equipe-a, equipe-b" value="${esc((d.namespaces || []).join(', '))}">`,
              tr('eso.t.namespaces', 'Comma separated; a store is posed in each, never in a system namespace'))}
          </div>
          <fieldset class="tf-block"><legend>${esc(tr('eso.chartLegend', 'Chart eso (optional)'))}</legend>
            <p class="form-hint">${esc(d.chart && d.chart.present ? tr('eso.chartPresent', 'The chart is already installed: choose a package only to install another version.')
              : tr('eso.chartAbsentHint', 'The chart is not installed: choose the package that carries the chart item eso.'))}</p>
            <div class="tf-args">
            ${field(tr('eso.f.package', 'Package'), `<select data-f="package">${opt('', tr('eso.noChart', 'None (chart already present)'))}${pkgs.map(p => opt(`${p.name}@${p.version}`, `${p.name} ${p.version}`)).join('')}</select>`,
              tr('eso.t.package', 'An imported package (Artefacts tab) with a chart item named eso'))}
            ${field(tr('eso.f.registry', 'Registry'), `<select data-f="registry">${regs.map(x => opt(x.name, `${x.name} (${x.endpoint || ''})`)).join('')}</select>`,
              tr('eso.t.registry', 'A registry attached to this Harvester cluster where the package is published'))}
            ${field(tr('eso.f.item', 'Item'), '<input data-f="item" type="text" value="eso" readonly>', tr('eso.t.item', 'The chart item is always named eso'))}
          </div>${regs.length ? '' : `<p class="form-hint">${esc(tr('eso.noRegistry', 'No registry attached to this cluster: declare or pose one in the Registries tab.'))}</p>`}</fieldset></section>
          <section data-eso-pane="checks" hidden><div class="xfer-report" data-x="report"><p class="form-hint">${esc(tr('eso.checking', 'Checking…'))}</p></div></section>
          <section data-eso-pane="summary" hidden><div data-x="summary"></div></section>
          <div class="xfer-live-line" data-x="progress" hidden></div>
        </div>
        <div class="modal-footer apply-bar" style="margin:0;">
          ${btn('data-eso-close', 'btn-secondary', '', tr('common.cancel', 'Cancel'), tr('eso.t.close', 'Close this window'))}
          <span class="tip" data-x="go-wrap" data-tip="${esc(tr('eso.t.checkingGo', 'Waiting for the checks of these values'))}"><button type="button" class="btn btn-primary btn-sm" data-eso-go disabled>${icon('play', 13)} ${esc(tr('eso.apply', 'Apply'))}</button></span>
          <span class="apply-result" data-x="feedback"></span></div>
      </div>`;
    document.body.appendChild(modal);
    const m = modal;
    const v = (k) => m.querySelector(`[data-f="${k}"]`);
    const q = (x) => m.querySelector(`[data-x="${x}"]`);
    const go = m.querySelector('[data-eso-go]');
    const state = { n: 0, t: null, launched: false };
    const show = (t) => {
      m.querySelectorAll('[data-eso-tab]').forEach(b => b.classList.toggle('active', b.dataset.esoTab === t));
      m.querySelectorAll('[data-eso-pane]').forEach(p => { p.hidden = p.dataset.esoPane !== t; });
    };
    m.querySelectorAll('[data-eso-tab]').forEach(b => b.addEventListener('click', () => show(b.dataset.esoTab)));

    /** Demande lue dans le formulaire, ou [null, raison]. */
    function bodyOf() {
      const nss = [...new Set(v('namespaces').value.split(/[\s,]+/).map(x => x.trim()).filter(Boolean))];
      if (!nss.length) return [null, findingText({ code: 'eso-no-namespace' })];
      const bad = nss.find(x => !NS_RE.test(x));
      if (bad) return [null, findingText({ code: 'eso-namespace-invalid', facts: { namespace: bad } })];
      const body = { namespaces: nss };
      const pk = v('package').value;
      if (pk) {
        if (!v('registry').value) return [null, tr('eso.noRegistry', 'No registry attached to this cluster: declare or pose one in the Registries tab.')];
        const at = pk.lastIndexOf('@');
        body.chart = { registry: v('registry').value, package: pk.slice(0, at), version: pk.slice(at + 1) };
      }
      return [body, ''];
    }
    function setGo(ok, why) {
      go.disabled = !ok || state.launched || !can('admin');
      const reason = !can('admin') ? findingText({ code: 'eso-admin-only' }) : why;
      q('go-wrap').dataset.tip = reason;
      go.title = reason;
      go.setAttribute('aria-label', reason);
    }
    function paintSummary(body, res) {
      const ch = body.chart;
      q('summary').innerHTML = `<ul class="reg-summary eso-summary">
        <li>${esc(tr('eso.sum.where', 'Child cluster {cluster}', { cluster: ref }))}</li>
        <li>${esc(tr('eso.sum.namespaces', 'Enabled namespaces: {namespaces}', { namespaces: body.namespaces.join(', ') }))}</li>
        <li>${esc(ch ? tr('eso.sum.chart', 'Chart eso of {package} {version}, from {registry}', ch) : tr('eso.sum.noChart', 'No chart installed by this set-up'))}</li>
        ${res && res.endpoint ? `<li>${esc(tr('eso.sum.openbao', 'OpenBao: {mode}, reached at {endpoint}', { mode: modeText(res.mode), endpoint: res.endpoint }))}</li>` : ''}
        <li>${esc(res && res.mode === 'a' ? tr('eso.sum.modeA', 'The administrator of OpenBao runs the commands given at the end of the set-up') : tr('eso.sum.modeB1', 'The console applies the access in OpenBao itself'))}</li></ul>`;
    }
    async function runChecks() {
      const n = ++state.n;
      const [body, why] = bodyOf();
      if (!body) {
        q('report').innerHTML = `<p class="form-hint">${esc(why)}</p>`;
        q('count').textContent = '';
        q('summary').innerHTML = `<p class="form-hint">${esc(why)}</p>`;
        setGo(false, why);
        return;
      }
      paintSummary(body, null);
      q('report').classList.add('is-checking');
      const r = await json('POST', `/api/capi/${enc(c)}/eso/${enc(ref.split('/')[0])}/${enc(ref.split('/')[1])}`, Object.assign({}, body, { dry_run: true }))
        .catch((e) => ({ ok: false, status: 0, d: { error: e.message } }));
      if (n !== state.n || modal !== m) return;
      q('report').classList.remove('is-checking');
      if (!r.ok) {
        const reason = errText(r);
        q('report').innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(reason)}</div></div>`;
        q('count').textContent = '';
        setGo(false, tr('eso.t.blocked', 'Blocked: {reason}', { reason }));
        return;
      }
      const fs = r.d.findings || [];
      const blocks = fs.filter(f => f.level === 'block' || !f.level);
      q('report').innerHTML = findingsHtml(fs) || `<div class="sto-finding sev-info" data-code="ok"><div class="sto-finding-title">${esc(tr('eso.noBlocker', 'No blocker.'))}</div></div>`;
      q('count').textContent = String(blocks.length);
      paintSummary(body, r.d);
      if (r.d.blocked || blocks.length) setGo(false, tr('eso.t.blocked', 'Blocked: {reason}', { reason: blocks.length ? findingText(blocks[0]) : '?' }));
      else setGo(true, tr('eso.t.go', 'Set up External Secrets on the child cluster: a tracked action, followed in the dock'));
    }
    function later(ms) {
      clearTimeout(state.t);
      state.n++;
      setGo(false, tr('eso.t.checkingGo', 'Waiting for the checks of these values'));
      state.t = setTimeout(runChecks, ms);
    }
    m._cleanup = () => { clearTimeout(state.t); state.n++; };
    m.addEventListener('click', (e) => { if (e.target === m || e.target.closest('[data-eso-close]')) closeModal(); });
    m.addEventListener('input', () => later(500));
    m.addEventListener('change', () => later(200));
    go.addEventListener('click', async () => {
      if (go.disabled) return;
      const [body] = bodyOf();
      if (!body) return;
      m._cleanup();
      state.launched = true;
      go.disabled = true;
      const r = await json('POST', childPath(), body).catch((e) => ({ ok: false, status: 0, d: { error: e.message } }));
      if (!r.ok || !r.d.action_id) {
        q('feedback').textContent = errText(r);
        state.launched = false;
        later(200);
        return;
      }
      const line = q('progress');
      line.hidden = false;
      line.textContent = tr('eso.started', 'Setting up (action {id}), see the dock.', { id: r.d.action_id });
      if (window.Dock && Dock.poll) Dock.poll();
      if (!window.SSEReconnect) return;
      const es = SSEReconnect.connect(`/api/stream/${enc(r.d.action_id)}`, {
        on: {
          step: (ev) => { try { const st = JSON.parse(ev.data); if (st.message && modal === m) line.textContent = st.message; } catch { /* ligne illisible */ } },
          end: (ev) => {
            let x = {};
            try { x = JSON.parse(ev.data); } catch { /* fin sans détail */ }
            es.close();
            line.textContent = x.status === 'done' ? tr('eso.setupDone', 'External Secrets set up.') : tr('eso.failed', 'The action failed: see the dock and the Activity tab.');
            setTimeout(load, 300);
          },
        },
      });
    });
    runChecks();
    return m;
  }

  function init() {
    document.addEventListener('click', (e) => {
      if (e.target.closest('#btn-eso-refresh')) { start(); return; }
      const box = e.target.closest('#capi-eso-body');
      if (!box) return;
      const act = e.target.closest('[data-eso-act]');
      if (act && !act.disabled) {
        const k = act.dataset.esoAct;
        if (k === 'setup') openSetup();
        else if (k === 'remove') openRemove();
        else load();
        return;
      }
      const del = e.target.closest('[data-eso-ns-del]');
      if (del && !del.disabled) {
        const team = del.dataset.esoNsDel;
        if (confirm(tr('eso.nsRemoveConfirm', 'Disable {namespace}? Its store is removed; its synced Secrets stay at their last value.', { namespace: team }))) nsGesture('DELETE', team);
        return;
      }
      const add = e.target.closest('[data-eso-ns-add]');
      if (add && !add.disabled) {
        const inp = box.querySelector('[data-eso-ns-input]');
        const team = (inp.value || '').trim();
        if (!NS_RE.test(team)) { say(findingText({ code: 'eso-namespace-invalid', facts: { namespace: team } }), true); return; }
        inp.value = '';
        nsGesture('POST', team);
        return;
      }
      if (e.target.closest('[data-eso-field-add]')) {
        const list = box.querySelector('[data-eso-fields]');
        if (list.querySelectorAll('.eso-field-row').length < FIELDS_MAX) list.insertAdjacentHTML('beforeend', fieldRow());
        return;
      }
      const fdel = e.target.closest('[data-eso-field-del]');
      if (fdel) {
        const list = box.querySelector('[data-eso-fields]');
        fdel.closest('.eso-field-row').remove();
        if (!list.querySelector('.eso-field-row')) list.innerHTML = fieldRow();
        return;
      }
      const w = e.target.closest('[data-eso-write]');
      if (w && !w.disabled) { writeValue(w.closest('.eso-write')); return; }
      const cp = e.target.closest('[data-eso-copy]');
      if (cp) { const pre = box.querySelector('[data-eso-example]'); if (pre) copy(pre.textContent, cp); }
    });
    document.addEventListener('change', (e) => {
      const sel = e.target.closest('#capi-eso-body [data-eso="child"]');
      if (sel) { cur.child = sel.value; say(''); load(); }
    });
  }
  document.addEventListener('DOMContentLoaded', init);

  return { start, refresh: load, openSetup, _findingText: findingText, _yaml: yaml };
})();
window.CapiEso = CapiEso;
