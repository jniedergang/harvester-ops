/**
 * harvester-ops : magasin de secrets OpenBao (2.9.0, tâche 12).
 *
 * Paramètres > Secrets (administrateur), en sous-onglets :
 *  - État : mode (local, A, B1), adresse, santé, cache, agent de l'hôte,
 *    avertissements, copie hors ligne de la clé de descellement (B1),
 *    réglages sans secret, activation et désactivation ;
 *  - Classes : où est chaque classe, migrer, confirmer, revenir en local,
 *    barrière de la classe kubeconfig (autre classe + panne simulée) ;
 *  - Versions : historique d'un secret (date, auteur, action), retour à une
 *    version ; jamais la valeur ;
 *  - Rotation : secret_id, clé de descellement, clés SSH des hôtes KVM, clés
 *    transit, âge des secrets que la console ne possède pas, clés transit
 *    gardées 30 jours, entités à retirer en mode A ;
 *  - Jetons personnels (2.10.0) : méthode jwt de chaque Rancher, assistant
 *    à onglets (Serveur, Vérifications, Confirmation), activation et
 *    désactivation à chaud, clés de Rancher, groupes internes et entités vues ;
 *  - Instantanés (B1) : réglages, liste, prendre, restaurer.
 * L'assistant d'activation est une fenêtre à onglets (Serveur,
 * Vérifications, Confirmation) : « Activer » reste grisé tant qu'une
 * vérification est rouge. Le bandeau global dit une panne d'OpenBao et
 * jusqu'à quand le cache tient.
 *
 * Toute écriture passe par /api/secrets/... qui lance `harvester-secrets` en
 * action suivie (dock, Activité) ; le suivi passe par SSEReconnect.
 */
const Secrets = (() => {
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const enc = encodeURIComponent;
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 13) => (window.Icons ? Icons.svg(n, { size }) : '');
  const tipAttr = (t) => `data-tip="${esc(t)}" title="${esc(t)}"`;
  const TABS = ['status', 'classes', 'persons', 'versions', 'rotation', 'snapshots'];
  const cur = { tab: 'status', status: null, eso: null, classes: null, rotation: null, snaps: null, tfkeys: null,
                persons: null, versionsClass: null, busy: false };

  // Textes par code (clés en toutes lettres : le contrôle de parité ne lit que des littéraux)
  const CLASS = {
    'replication-notify': () => tr('sx.cls.replication-notify'), 'replication-hook': () => tr('sx.cls.replication-hook'),
    'rancher-oidc': () => tr('sx.cls.rancher-oidc'), 'tf-kvm': () => tr('sx.cls.tf-kvm'),
    'ssh-kvm': () => tr('sx.cls.ssh-kvm'), 'ssh-cluster': () => tr('sx.cls.ssh-cluster'),
    kubeconfig: () => tr('sx.cls.kubeconfig'),
  };
  const WARN = {
    'openbao-down': () => tr('sx.w.down'), 'openbao-degraded': () => tr('sx.w.degraded'),
    'agent-missing': () => tr('sx.w.agentMissing'), 'secret-id-overdue': () => tr('sx.w.secretIdOverdue'),
    'offline-copy-pending': () => tr('sx.w.offlineCopy'), 'snapshot-dir-unset': () => tr('sx.w.snapDirUnset'),
    'snapshot-stale': () => tr('sx.w.snapStale'), 'snapshot-failed': () => tr('sx.w.snapFailed'),
    'audit-space-low': () => tr('sx.w.auditLow'), 'tf-entities-pending': () => tr('sx.w.entities'),
    'openbao-jwt-keys-changed': () => tr('sx.w.jwtKeysChanged'), 'openbao-jwt-keys-unreadable': () => tr('sx.w.jwtKeysUnreadable'),
    'eso-keys-changed': () => tr('sx.w.esoKeysChanged'),
  };
  const GATE = {
    'migrate-another-first': () => tr('sx.gate.another'), 'outage-test-first': () => tr('sx.gate.outage'),
    'outage-test-again': () => tr('sx.gate.outageAgain'),
  };
  const CHECK = {
    address: () => tr('sx.chk.address'), tls: () => tr('sx.chk.tls'), seal: () => tr('sx.chk.seal'),
    wrap: () => tr('sx.chk.wrap'), login: () => tr('sx.chk.login'), kv2: () => tr('sx.chk.kv2'),
    capabilities: () => tr('sx.chk.capabilities'), roundtrip: () => tr('sx.chk.roundtrip'),
    transit: () => tr('sx.chk.transit'), 'tf-role': () => tr('sx.chk.tfRole'), openbao: () => tr('sx.chk.openbao'),
    agent: () => tr('sx.chk.agent'), bundle: () => tr('sx.chk.bundle'), after: () => tr('sx.chk.after'),
  };
  const TAB = {
    status: [() => tr('sx.tab.status'), () => tr('sx.tab.statusTip')],
    classes: [() => tr('sx.tab.classes'), () => tr('sx.tab.classesTip')],
    persons: [() => tr('sx.tab.persons'), () => tr('sx.tab.personsTip')],
    versions: [() => tr('sx.tab.versions'), () => tr('sx.tab.versionsTip')],
    rotation: [() => tr('sx.tab.rotation'), () => tr('sx.tab.rotationTip')],
    snapshots: [() => tr('sx.tab.snapshots'), () => tr('sx.tab.snapshotsTip')],
  };
  const STATE = { ok: () => tr('sx.state.ok'), degraded: () => tr('sx.state.degraded'), down: () => tr('sx.state.down'),
                  inactive: () => tr('sx.state.inactive') };
  const WIZ = {
    server: [() => tr('sx.wiz.server'), () => tr('sx.wiz.serverTip')],
    checks: [() => tr('sx.wiz.checks'), () => tr('sx.wiz.checksTip')],
    confirm: [() => tr('sx.wiz.confirm'), () => tr('sx.wiz.confirmTip')],
  };
  const REPLACE = {
    'rancher-oidc': () => tr('sx.replace.rancher-oidc'), 'replication-notify': () => tr('sx.replace.replication-notify'),
    kubeconfig: () => tr('sx.replace.kubeconfig'), 'ssh-cluster': () => tr('sx.replace.ssh-cluster'),
  };
  const text = (map, k) => (map[k] ? map[k]() : k);
  const MODE = { local: () => tr('sx.mode.local'), a: () => tr('sx.mode.a'), b1: () => tr('sx.mode.b1') };

  // -- outils -------------------------------------------------------------------
  async function api(method, url, body) {
    try {
      const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' },
                                   body: body ? JSON.stringify(body) : undefined });
      let d = {};
      try { d = await r.json(); } catch { /* sans corps */ }
      return { ok: r.ok, status: r.status, d };
    } catch (e) {
      return { ok: false, status: 0, d: { error: String(e && e.message || e) } };
    }
  }
  /** Suit une action jusqu'à sa fin : promesse de { ok, run, output }. */
  function follow(actionId) {
    if (window.Dock && Dock.poll) Dock.poll();
    return new Promise((resolve) => {
      const done = async (ok) => {
        const r = await api('GET', `/api/action/${enc(actionId)}`);
        const run = r.ok ? r.d : {};
        resolve({ ok: ok && run.status !== 'error', run, output: ((run.result || {}).output) || null });
      };
      if (!window.SSEReconnect) { setTimeout(() => done(true), 1500); return; }
      const es = SSEReconnect.connect(`/api/stream/${enc(actionId)}`, {
        on: {
          end: (e) => {
            let d = {};
            try { d = JSON.parse(e.data); } catch { /* fin sans détail */ }
            es.close();
            done(d.status === 'done');
          },
        },
        onStatus: (s) => { if (s.state === 'dead') done(false); },
      });
    });
  }
  function when(ts) {
    if (!ts) return '–';
    const d = new Date(typeof ts === 'number' ? ts * 1000 : ts);
    return Number.isNaN(d.getTime()) ? '–' : d.toLocaleString();
  }
  function age(s) {
    if (s == null) return '–';
    const d = Math.floor(s / 86400);
    if (d >= 1) return tr('sx.age.days', { n: d });
    const h = Math.floor(s / 3600);
    return h >= 1 ? tr('sx.age.hours', { n: h }) : tr('sx.age.minutes', { n: Math.max(1, Math.round(s / 60)) });
  }
  function bytes(n) {
    if (n == null) return '–';
    const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let v = Number(n), i = 0;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i += 1; }
    return `${v.toFixed(v < 10 && i ? 1 : 0)} ${u[i]}`;
  }
  function btn(attrs, cls, ic, label, tip, disabled) {
    return `<button type="button" class="btn btn-sm ${cls} tip" ${attrs} ${tipAttr(tip)}${disabled ? ' disabled' : ''}>${ic ? icon(ic) + ' ' : ''}${esc(label)}</button>`;
  }
  function say(html, bad) {
    const el = document.getElementById('sx-feedback');
    if (el) el.innerHTML = bad ? `<span class="res-error">${html}</span>` : html;
  }
  const errOf = (r) => esc((r.d && (r.d.error || r.d.code)) || `HTTP ${r.status}`);

  /** Un geste : POST, suivi de l'action, message, rechargement. */
  async function gesture(method, url, body, doneText) {
    if (cur.busy) return null;
    cur.busy = true;
    say(`<span class="spinner-inline"></span> ${esc(tr('sx.running'))}`);
    try {
      const r = await api(method, url, body);
      if (!r.ok || !r.d.action_id) { say(errOf(r), true); return null; }
      const f = await follow(r.d.action_id);
      const out = f.output || {};
      if (f.ok) say(`${icon('ok')} ${esc(doneText || tr('sx.done'))}`);
      else say(esc(tr('sx.failed', { msg: out.error || out.code || (f.run || {}).error_summary || '?' })), true);
      return f;
    } finally {
      cur.busy = false;
      load();
    }
  }

  /** Fenêtre par-dessus les Paramètres (modèle des fenêtres de la console). */
  function modal(id, title, bodyHtml, footHtml) {
    document.getElementById(id)?.remove();
    const m = document.createElement('div');
    m.className = 'modal-overlay active sx-modal';
    m.id = id;
    m.setAttribute('role', 'dialog');
    m.setAttribute('aria-modal', 'true');
    m.setAttribute('aria-label', title);
    m.innerHTML = `<div class="modal sx-window">
        <div class="modal-header"><h3>${esc(title)}</h3>
          <button type="button" class="btn-close tip" data-x="close" ${tipAttr(tr('common.close'))} aria-label="${esc(tr('common.close'))}">×</button></div>
        <div class="modal-body">${bodyHtml}</div>
        ${footHtml ? `<div class="apply-bar sx-foot">${footHtml}</div>` : ''}
      </div>`;
    document.body.appendChild(m);
    const close = () => m.remove();
    m.addEventListener('click', (e) => { if (e.target === m || e.target.closest('[data-x="close"]')) close(); });
    m.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } });
    if (typeof Settings !== 'undefined' && Settings.trapFocus) Settings.trapFocus(m);
    setTimeout(() => (m.querySelector('input, select, textarea, button:not([data-x])') || m).focus(), 0);
    return { el: m, close, q: (s) => m.querySelector(s) };
  }

  /** Confirmation à saisie (identifiant de la console, d'une déclaration) :
   *  le bouton n'est actif que quand la saisie est exacte. */
  function typedConfirm({ title, intro, expect, label, action, danger = true, details = [] }) {
    return new Promise((resolve) => {
      const w = modal('sx-confirm', title, `
        <p>${esc(intro)}</p>
        ${details.length ? `<ul class="sx-preflight" data-sx-preflight>${details.map((d) => `<li class="fail">${esc(d)}</li>`).join('')}</ul>` : ''}
        <label class="login-field"><span>${esc(label)}</span>
          <input type="text" class="sx-typed tip" autocomplete="off" spellcheck="false" ${tipAttr(tr('sx.t.typed', { value: expect }))}
                 placeholder="${esc(expect)}"></label>`,
        `${btn('data-x="close"', 'btn-secondary', '', tr('common.cancel'), tr('sx.t.cancel'))}
         ${btn('data-x="ok"', danger ? 'btn-danger' : 'btn-primary', '', action, tr('sx.t.typedOk'), true)}`);
      const inp = w.q('.sx-typed');
      const ok = w.q('[data-x="ok"]');
      inp.addEventListener('input', () => { ok.disabled = inp.value.trim() !== expect; });
      ok.addEventListener('click', () => { const v = inp.value.trim(); w.close(); resolve(v); });
      w.el.addEventListener('click', (e) => { if (e.target.closest('[data-x="close"]')) resolve(null); });
    });
  }

  // -- coquille de l'onglet --------------------------------------------------------
  function shell() {
    const box = document.getElementById('secrets-settings-body');
    if (!box) return null;
    if (!box.querySelector('.sx-tabs')) {
      box.innerHTML = `
        <div class="sub-tabs sub-tabs-inline sx-tabs" role="tablist" aria-label="${esc(tr('sx.title'))}">
          ${TABS.map((t) => `<button type="button" class="sub-tab tip${t === cur.tab ? ' active' : ''}" role="tab"
              aria-selected="${t === cur.tab}" data-sx-tab="${t}" ${tipAttr(TAB[t][1]())}>${esc(TAB[t][0]())}</button>`).join('')}
        </div>
        <div class="res-feedback" id="sx-feedback" role="status" aria-live="polite"></div>
        <div class="sx-pane" data-sx-pane></div>`;
    }
    box.querySelectorAll('[data-sx-tab]').forEach((b) => {
      const on = b.dataset.sxTab === cur.tab;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', String(on));
    });
    return box.querySelector('[data-sx-pane]');
  }

  async function load() {
    const pane = shell();
    if (!pane) return;
    const r = await api('GET', '/api/secrets/status');
    if (!r.ok) { pane.innerHTML = `<p class="form-hint">${esc(tr('sx.loadError'))} ${errOf(r)}</p>`; return; }
    cur.status = r.d;
    if (cur.tab === 'status') {
      // 2.10.0 : carte External Secrets (clusters enfants, écouteur de B1)
      const e = r.d.active ? await api('GET', '/api/secrets/eso') : null;
      cur.eso = e && e.ok ? e.d : null;
    } else if (cur.tab === 'classes') {
      const c = await api('GET', '/api/secrets/classes');
      cur.classes = c.ok ? c.d : null;
    } else if (cur.tab === 'rotation') {
      const [a, b, e] = await Promise.all([api('GET', '/api/secrets/rotation'), api('GET', '/api/secrets/tf-keys'),
        api('GET', '/api/secrets/tf-entity-commands')]);
      cur.rotation = a.ok ? a.d : null;
      cur.tfkeys = b.ok ? b.d : null;
      cur.entities = e.ok ? e.d.entities : [];
    } else if (cur.tab === 'persons') {
      const p = r.d.active ? await api('GET', '/api/secrets/person-token') : null;
      cur.persons = p && p.ok ? p.d : null;
    } else if (cur.tab === 'snapshots') {
      const s = await api('GET', '/api/secrets/snapshots');
      cur.snaps = s.ok ? s.d : null;
    } else if (cur.tab === 'versions') {
      const c = await api('GET', '/api/secrets/classes');
      cur.classes = c.ok ? c.d : null;
    }
    render();
  }

  function render() {
    const pane = shell();
    if (!pane || !cur.status) return;
    const html = { status: statusHtml, classes: classesHtml, persons: personsHtml, versions: versionsHtml,
                   rotation: rotationHtml, snapshots: snapshotsHtml }[cur.tab]();
    pane.innerHTML = html;
    if (cur.tab === 'versions' && cur.versionsClass) loadNames(cur.versionsClass);
  }

  // -- État --------------------------------------------------------------------------
  function stateBadge(st) {
    const s = (st || {}).state;
    const cls = s === 'ok' ? 'ok' : s === 'degraded' ? 'warn' : s === 'down' ? 'fail' : '';
    return `<span class="badge ${cls} tip" data-sx-state="${esc(s || 'inactive')}" ${tipAttr(tr('sx.t.state'))}>${esc(text(STATE, s || 'inactive'))}</span>`;
  }
  function warningsHtml(list) {
    if (!(list || []).length) return '';
    return `<div class="sx-warnings">${list.map((w) => {
      const sev = w.level === 'error' ? 'sev-critical' : w.level === 'info' ? 'sev-info' : 'sev-action';
      const facts = [];
      if (w.since) facts.push(tr('sx.f.since', { when: when(w.since) }));
      if (w.error) facts.push(w.error);
      if (w.age_s) facts.push(tr('sx.f.age', { age: age(w.age_s) }));
      if (w.free != null) facts.push(tr('sx.f.free', { free: bytes(w.free), total: bytes(w.total) }));
      if (w.last_ok_at) facts.push(tr('sx.f.lastOk', { when: when(w.last_ok_at) }));
      if (w.count) facts.push(String(w.count));
      if ((w.servers || []).length) facts.push(w.servers.join(', '));
      return `<div class="sto-finding ${sev}" data-sx-warning="${esc(w.code)}"><div class="sto-finding-title">${icon(w.level === 'info' ? 'info' : 'warn')} ${esc(text(WARN, w.code))}</div>
        ${facts.length ? `<div class="sto-finding-facts">${esc(facts.join(' · '))}</div>` : ''}</div>`;
    }).join('')}</div>`;
  }
  function kv(rows) {
    return `<dl class="kv sx-kv">${rows.filter(Boolean).map(([k, v, tip]) => `<dt>${esc(k)}</dt><dd${tip ? ` class="tip" ${tipAttr(tip)}` : ''}>${v}</dd>`).join('')}</dl>`;
  }

  function statusHtml() {
    const s = cur.status;
    const st = s.store || {};
    const agent = s.agent || {};
    const active = s.active;
    const head = `<div class="sx-head">
        <span class="badge info" data-sx-mode="${esc(s.mode)}">${esc(text(MODE, s.mode))}</span> ${active ? stateBadge(st) : ''}
        ${active ? '' : `<p class="form-hint">${esc(tr('sx.localIntro'))}</p>`}
      </div>`;
    const facts = active ? kv([
      [tr('sx.k.addr'), esc(s.addr || '–')],
      [tr('sx.k.mount'), `${esc(s.mount || '')} · ${esc(s.prefix || '')}`, tr('sx.t.mount')],
      [tr('sx.k.lastCheck'), esc(when(st.last_probe_at)), tr('sx.t.lastCheck')],
      [tr('sx.k.cache'), esc(tr('sx.cacheFacts', { ttl: (s.settings || {}).cache_ttl_min, n: st.cached || 0 }))
        + (st.cache_until ? ` · ${esc(tr('sx.cacheUntil', { when: when(st.cache_until) }))}` : ''), tr('sx.t.cache')],
      st.down_since ? [tr('sx.k.downSince'), esc(when(st.down_since)) + (st.error ? ` (${esc(st.error)})` : '')] : null,
      [tr('sx.k.agent'), esc(agent.present ? tr('sx.agentPresent', { v: agent.version || '?' }) : tr('sx.agentAbsent')), tr('sx.t.agent')],
      [tr('sx.k.secretId'), esc(age(agent.secret_id_age_s)), tr('sx.t.secretId')],
      s.mode === 'b1' ? [tr('sx.k.unsealKey'), esc(agent.current_key || '–'), tr('sx.t.unsealKey')] : null,
      s.mode === 'b1' && agent.audit ? [tr('sx.k.audit'), esc(tr('sx.f.free', { free: bytes(agent.audit.free), total: bytes(agent.audit.total) })), tr('sx.t.audit')] : null,
    ]) : '';
    const actions = active ? `<div class="apply-bar sx-actions">
        ${btn('data-sx="cache"', 'btn-secondary', 'clean', tr('sx.clearCache'), tr('sx.t.clearCache'))}
        ${btn('data-sx="outage"', 'btn-secondary', 'test', tr('sx.outageTest'), tr('sx.t.outageTest'))}
        ${btn('data-sx="disable"', 'btn-danger', 'unlock', tr('sx.disable'), tr('sx.t.disable'), (Object.keys(s.migrated || {}).length > 0))}
      </div>${Object.keys(s.migrated || {}).length ? `<p class="form-hint">${esc(tr('sx.disableNeedsLocal'))}</p>` : ''}
      ${s.outage_test && s.outage_test.ok_at ? `<p class="form-hint">${esc(tr('sx.outageOk', { when: when(s.outage_test.ok_at), by: s.outage_test.by || '' }))} ${esc(tr('sx.outageScope'))}</p>` : `<p class="form-hint">${esc(tr('sx.outageScope'))}</p>`}`
      : `<div class="apply-bar sx-actions">${btn('data-sx="enable"', 'btn-primary', 'lock', tr('sx.enable'), tr('sx.t.enable'))}</div>`;
    return `${head}${warningsHtml(s.warnings)}${facts}${actions}${offlineCopyHtml(s)}${vaultAckHtml(s)}${esoHtml(cur.eso)}${settingsHtml(s)}`;
  }

  function offlineCopyHtml(s) {
    if (s.mode !== 'b1') return '';
    const oc = s.offline_copy || {};
    const key = (s.agent || {}).current_key || '';
    if (!oc.pending) {
      return oc.key_id ? `<p class="form-hint sx-offline-ok">${icon('ok')} ${esc(tr('sx.offlineAcked', { key: oc.key_id, when: when(oc.acked_at) }))}</p>` : '';
    }
    return `<div class="card sx-card sx-offline" data-sx-offline>
        <h5>${icon('key')} ${esc(tr('sx.offlineTitle'))}</h5>
        <p class="form-hint">${esc(tr('sx.offlineIntro'))}</p>
        <ol class="sx-steps">
          <li>${btn('data-sx="export"', 'btn-secondary', 'download', tr('sx.offlineExport'), tr('sx.t.offlineExport'))}</li>
          <li>${esc(tr('sx.offlineKeep'))}</li>
          <li>${btn('data-sx="ack"', 'btn-primary', 'ok', tr('sx.offlineAck'), tr('sx.t.offlineAck'))}
            <span class="res-dim">${esc(key ? tr('sx.offlineKeyIs', { key }) : '')}</span></li>
        </ol></div>`;
  }

  function vaultAckHtml(s) {
    const v = s.tf_server || {};
    if (!s.active || v.warning !== 'tf-encryption-vault-unverified' || v.acknowledged) return '';
    return `<div class="sto-finding sev-action" data-sx-vault><div class="sto-finding-title">${icon('warn')} ${esc(tr('sx.vaultTitle', { version: v.version || '?' }))}</div>
      <div class="sto-finding-facts">${esc(tr('sx.vaultIntro'))}</div>
      ${btn('data-sx="vault-ack"', 'btn-secondary', 'ok', tr('sx.vaultAck'), tr('sx.t.vaultAck'))}</div>`;
  }

  /** 2.10.0 : carte « External Secrets » (mode, écouteur de B1, clusters
   *  enfants, clés changées). Rien de secret : adresses et empreintes. */
  function esoHtml(e) {
    if (!e) return '';
    const b1 = e.mode === 'b1';
    const clusters = e.clusters || [];
    const listener = b1 ? `
        ${kv([
          [tr('sx.eso.k.listener'), e.listener ? `<span class="badge ok" data-sx-eso-listener="on">${esc(tr('sx.eso.on'))}</span> <code>${esc(e.listener)}</code>`
            : `<span class="badge dim" data-sx-eso-listener="off">${esc(tr('sx.eso.off'))}</span>`, tr('sx.eso.t.listener')],
          e.address ? [tr('sx.eso.k.address'), `<code>${esc(e.address)}</code>`, tr('sx.eso.t.address')] : null,
          [tr('sx.eso.k.port'), esc(e.port), tr('sx.eso.t.port')],
        ])}
        <div class="sx-inline-row">
          <label class="login-field"><span>${esc(tr('sx.eso.addr'))}</span>
            <input type="text" data-sx-eso-addr spellcheck="false" autocomplete="off" placeholder="${esc(`172.16.1.11:${e.port || 8212}`)}"
                   value="${esc(e.listener || '')}" class="tip" ${tipAttr(tr('sx.eso.t.addr'))}></label>
          ${btn('data-sx="eso-listener-save"', 'btn-primary', 'save', tr('sx.eso.save'), tr('sx.eso.t.save'))}
          ${btn('data-sx="eso-listener-off"', 'btn-secondary', 'stop', tr('sx.eso.turnOff'),
            clusters.length ? tr('sx.eso.t.turnOffBusy') : tr('sx.eso.t.turnOff'), clusters.length > 0 || !e.listener)}
        </div>
        <p class="form-hint sx-eso-firewall">${icon('warn')} ${esc(tr('sx.eso.firewall', { port: e.port || 8212 }))}</p>`
      : `<p class="form-hint sx-eso-mode-a">${esc(tr('sx.eso.modeA'))}</p>`;
    const rows = clusters.map((c) => {
      const ref = `${c.harvester}/${c.namespace}/${c.cluster}`;
      const fp = String(c.keys_sha256 || '');
      return `<tr class="sx-eso-cluster" data-ref="${esc(ref)}"><td><code>${esc(ref)}</code>
          ${c.keys_changed ? ` <span class="badge warn tip" data-sx-eso-changed ${tipAttr(tr('sx.eso.t.changed'))}>${esc(tr('sx.eso.changed'))}</span>` : ''}</td>
        <td>${fp ? `<code class="tip" ${tipAttr(fp)}>${esc(fp.slice(0, 12))}</code>` : '–'}</td>
        <td>${esc((c.namespaces || []).join(', ') || '–')}</td>
        <td>${c.applied ? `<span class="badge ok">${esc(tr('sx.eso.applied'))}</span>` : `<span class="badge warn tip" ${tipAttr(tr('sx.eso.t.waiting'))}>${esc(tr('sx.eso.waiting'))}</span>`}</td>
        <td>${esc(c.by || '–')}</td><td>${esc(when(c.at))}</td>
        <td class="sx-row-actions">${btn(`data-sx-eso-keys="${esc(ref)}"`, c.keys_changed ? 'btn-primary' : 'btn-secondary', 'refresh',
          tr('sx.eso.updateKeys'), tr('sx.eso.t.updateKeys'))}</td></tr>`;
    }).join('');
    return `<div class="card sx-card sx-eso" data-sx-eso="${esc(e.mode || '')}">
        <h5>${icon('key')} ${esc(tr('sx.eso.title'))}</h5>
        <p class="form-hint">${esc(tr('sx.eso.intro'))}</p>
        ${listener}
        ${rows ? `<div class="sx-scroll"><table class="data-table sx-eso-clusters"><thead><tr>
          <th>${esc(tr('sx.eso.col.cluster'))}</th><th>${esc(tr('sx.eso.col.key'))}</th><th>${esc(tr('sx.eso.col.namespaces'))}</th>
          <th>${esc(tr('sx.eso.col.applied'))}</th><th>${esc(tr('sx.eso.col.by'))}</th><th>${esc(tr('sx.eso.col.at'))}</th><th></th></tr></thead>
          <tbody>${rows}</tbody></table></div>` : `<p class="form-hint">${esc(tr('sx.eso.none'))}</p>`}
      </div>`;
  }

  function settingsHtml(s) {
    const v = s.settings || {};
    const field = (k, min, max, label, tip) => `<label class="login-field"><span>${esc(label)}</span>
        <input type="number" min="${min}" max="${max}" data-sx-set="${k}" value="${esc(v[k])}" class="tip" ${tipAttr(tip)}></label>`;
    return `<details class="sx-settings"><summary class="tip" ${tipAttr(tr('sx.t.settings'))}>${esc(tr('sx.settings'))}</summary>
      <div class="sx-grid">
        ${field('cache_ttl_min', 1, 120, tr('sx.set.cache'), tr('sx.t.setCache'))}
        ${field('originals_keep_days', 1, 30, tr('sx.set.originals'), tr('sx.t.setOriginals'))}
        ${field('plan_secrets_keep_days', 1, 30, tr('sx.set.plans'), tr('sx.t.setPlans'))}
        ${field('timeout', 5, 60, tr('sx.set.timeout'), tr('sx.t.setTimeout'))}
      </div>
      <div class="apply-bar">${btn('data-sx="save-settings"', 'btn-primary', 'save', tr('sx.save'), tr('sx.t.save'))}</div>
    </details>`;
  }

  // -- Classes -------------------------------------------------------------------------
  function classesHtml() {
    const s = cur.status;
    if (!s.active) return `<p class="form-hint">${esc(tr('sx.needActive'))}</p>`;
    const rows = ((cur.classes || {}).classes || []);
    const down = ['down'].includes((s.store || {}).state);
    const body = rows.map((r) => {
      const where = r.openbao && r.local ? tr('sx.where.mixed') : r.openbao || r.migrated ? tr('sx.where.openbao') : tr('sx.where.local');
      let migrateWhy = '';
      if (r.migrated && r.migrated.complete !== false && !r.local) migrateWhy = tr('sx.why.already');
      else if (down) migrateWhy = tr('sx.why.down');
      else if (r.offline_copy_pending) migrateWhy = tr('sx.why.offline');
      else if (r.gate) migrateWhy = text(GATE, r.gate);
      const backWhy = down ? tr('sx.why.down') : (!r.openbao ? tr('sx.why.nothingThere') : '');
      const confirmWhy = !r.originals ? tr('sx.why.noOriginals') : '';
      const due = r.originals_oldest ? r.originals_oldest + (s.settings.originals_keep_days || 7) * 86400 : null;
      return `<tr data-sx-class="${esc(r.class)}">
        <td><strong>${esc(text(CLASS, r.class))}</strong><div class="res-dim"><code>${esc(r.class)}</code></div></td>
        <td>${esc(where)}</td>
        <td class="tip" ${tipAttr(tr('sx.t.counts'))}>${esc(tr('sx.counts', { local: r.local, bao: r.openbao }))}</td>
        <td>${r.migrated ? esc(tr('sx.migratedBy', { when: when(r.migrated.at), by: r.migrated.by || '?' })) + (r.migrated.complete === false ? ` <span class="badge warn">${esc(tr('sx.partial'))}</span>` : '') : '–'}</td>
        <td>${r.originals ? esc(tr('sx.originals', { n: r.originals, when: when(due) })) : '–'}</td>
        <td class="sx-row-actions">
          ${btn(`data-sx-migrate="${esc(r.class)}"`, 'btn-primary', 'lock', tr('sx.migrate'), migrateWhy || tr('sx.t.migrate'), !!migrateWhy)}
          ${btn(`data-sx-confirm="${esc(r.class)}"`, 'btn-secondary', 'ok', tr('sx.confirm'), confirmWhy || tr('sx.t.confirm'), !!confirmWhy)}
          ${btn(`data-sx-back="${esc(r.class)}"`, 'btn-secondary', 'undo', tr('sx.back'), backWhy || tr('sx.t.back'), !!backWhy)}
        </td></tr>`;
    }).join('');
    return `<p class="form-hint">${esc(tr('sx.classesIntro'))}</p>
      <div class="table-scroll"><table class="data-table sx-classes"><thead><tr>
        <th>${esc(tr('sx.col.class'))}</th><th>${esc(tr('sx.col.where'))}</th><th>${esc(tr('sx.col.count'))}</th>
        <th>${esc(tr('sx.col.migrated'))}</th><th>${esc(tr('sx.col.originals'))}</th><th></th></tr></thead>
        <tbody>${body}</tbody></table></div>
      <p class="form-hint">${esc(tr('sx.localForever'))}</p>
      <div class="apply-bar">${btn('data-sx="outage"', 'btn-secondary', 'test', tr('sx.outageTest'), tr('sx.t.outageTest'))}</div>`;
  }

  // -- Jetons personnels (2.10.0) ---------------------------------------------------------
  const PT_CHECK = {
    discovery: () => tr('sx.pt.chk.discovery'), login: () => tr('sx.pt.chk.login'), token: () => tr('sx.pt.chk.token'),
    capabilities: () => tr('sx.pt.chk.capabilities'), revoke: () => tr('sx.pt.chk.revoke'),
  };
  const PT_CHECK_IDS = ['discovery', 'login', 'token', 'capabilities', 'revoke'];
  const PT_CLASS = {
    kubeconfig: () => tr('sx.cls.kubeconfig'), 'ssh-cluster': () => tr('sx.cls.ssh-cluster'),
    'ssh-kvm': () => tr('sx.cls.ssh-kvm'), 'tf-kvm': () => tr('sx.cls.tf-kvm'), eso: () => tr('sx.pt.cls.eso'),
  };
  const ptRancher = (key) => (((cur.persons || {}).ranchers) || []).find((r) => r.key === key) || null;

  /** Ce qui empêche l'administrateur qui regarde de lancer les
   *  vérifications d'un Rancher (connexion réelle avec SON jeton
   *  d'identité), ou '' quand il le peut. */
  function ptCheckBlock(p, r) {
    const me = p.me || {};
    if (!me.sso) return tr('sx.pt.needRancher', { rancher: r.label });
    if (!me.id_token) return tr('sx.pt.needNewSignIn');
    if (me.rancher !== r.key) return tr('sx.pt.otherRancher', { rancher: r.label });
    return '';
  }

  function personsHtml() {
    const s = cur.status;
    if (!s.active) return `<p class="form-hint">${esc(tr('sx.needActive'))}</p>`;
    const p = cur.persons;
    if (!p) return `<p class="form-hint">${esc(tr('sx.loadError'))}</p>`;
    const rs = p.ranchers || [];
    const anyMethod = rs.some((r) => r.configured || r.pending) || Object.keys(p.groups || {}).length > 0;
    const head = `<div class="sx-head">
        <span class="badge ${p.enabled ? 'ok' : 'dim'} tip" data-sx-pt-state="${p.enabled ? 'on' : 'off'}" ${tipAttr(tr('sx.pt.t.state'))}>${esc(p.enabled ? tr('sx.pt.on') : tr('sx.pt.off'))}</span>
        ${p.enabled ? `<span class="res-dim tip" ${tipAttr(tr('sx.pt.t.tokens'))}>${esc(tr('sx.pt.tokens', { n: p.tokens || 0 }))}</span>` : ''}
        <p class="form-hint">${esc(tr('sx.pt.intro'))}</p>
      </div>`;
    const me = p.me || {};
    const meHint = !me.sso ? `<p class="form-hint sx-pt-me" data-sx-pt-me="local">${icon('info')} ${esc(tr('sx.pt.meLocal'))}</p>`
      : (!me.id_token ? `<p class="form-hint sx-pt-me" data-sx-pt-me="no-token">${icon('info')} ${esc(tr('sx.pt.needNewSignIn'))}</p>` : '');
    const rows = rs.map((r) => {
      const e = r.configured || r.pending;
      const state = r.configured ? (p.enabled ? 'active' : 'set') : r.pending ? 'pending' : 'none';
      const stateTxt = { active: tr('sx.pt.st.active'), set: tr('sx.pt.st.set'), pending: tr('sx.pt.st.pending'), none: tr('sx.pt.st.none') }[state];
      const stateCls = { active: 'ok', set: 'info', pending: 'warn', none: 'dim' }[state];
      const keyBadges = e ? `${esc(e.keys === 'static' ? tr('sx.pt.keys.static') : tr('sx.pt.keys.discovery'))}
          ${e.keys_changed ? ` <span class="badge warn tip" data-sx-pt-changed ${tipAttr(tr('sx.pt.t.changed'))}>${esc(tr('sx.pt.changed'))}</span>` : ''}
          ${e.keys_unreadable ? ` <span class="badge warn tip" data-sx-pt-unreadable ${tipAttr(tr('sx.pt.t.unreadable'))}>${esc(tr('sx.pt.unreadable'))}</span>` : ''}
          ${e.keys_pending_confirm ? ` <span class="badge info tip" ${tipAttr(tr('sx.pt.t.pendingConfirm'))}>${esc(tr('sx.pt.pendingConfirm'))}</span>` : ''}` : '–';
      const gone = r.origin === 'gone';
      const setupWhy = gone ? tr('sx.pt.t.gone') : (!r.sso ? tr('sx.pt.t.noSso') : '');
      const actions = [
        btn(`data-sx-pt-setup="${esc(r.key)}"`, r.configured ? 'btn-secondary' : 'btn-primary', 'key',
          r.configured ? tr('sx.pt.redo') : tr('sx.pt.setup'), setupWhy || tr('sx.pt.t.setup'), !!setupWhy),
        e && e.keys === 'static' ? btn(`data-sx-pt-keys="${esc(r.key)}"`, e.keys_changed ? 'btn-primary' : 'btn-secondary', 'refresh',
          tr('sx.pt.updateKeys'), tr('sx.pt.t.updateKeys')) : '',
        e && e.keys_pending_confirm ? btn(`data-sx-pt-confirm-keys="${esc(r.key)}"`, 'btn-secondary', 'ok',
          tr('sx.pt.confirmKeys'), tr('sx.pt.t.confirmKeys')) : '',
        e ? btn(`data-sx-pt-remove="${esc(r.key)}"`, 'btn-secondary', 'delete', tr('sx.pt.remove'), tr('sx.pt.t.remove')) : '',
      ].join('');
      return `<tr class="sx-pt-rancher" data-key="${esc(r.key)}">
        <td><strong>${esc(r.label)}</strong>${r.url ? `<div class="res-dim">${esc(r.url)}</div>` : ''}
          ${gone ? `<div class="res-dim">${esc(tr('sx.pt.gone'))}</div>` : ''}</td>
        <td>${e ? `<code class="tip" ${tipAttr(tr('sx.pt.t.mount'))}>${esc(e.mount || '')}</code>` : '–'}</td>
        <td>${keyBadges}</td>
        <td><span class="badge ${stateCls}" data-sx-pt-rstate="${state}">${esc(stateTxt)}</span></td>
        <td>${e ? esc(`${e.configured_by || '–'} · ${when(e.checked_at || e.configured_at)}`) : '–'}</td>
        <td class="sx-row-actions">${actions}</td></tr>`;
    }).join('');
    const table = rs.length ? `<div class="sx-scroll"><table class="data-table sx-pt-ranchers"><thead><tr>
        <th>${esc(tr('sx.pt.col.rancher'))}</th><th>${esc(tr('sx.pt.col.mount'))}</th><th>${esc(tr('sx.pt.col.keys'))}</th>
        <th>${esc(tr('sx.pt.col.state'))}</th><th>${esc(tr('sx.pt.col.by'))}</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table></div>` : `<p class="form-hint">${esc(tr('sx.pt.noRancher'))}</p>`;
    const actions = `<div class="apply-bar sx-actions">
        ${btn('data-sx="pt-off"', 'btn-secondary', 'stop', tr('sx.pt.turnOff'), p.enabled ? tr('sx.pt.t.turnOff') : tr('sx.pt.t.alreadyOff'), !p.enabled)}
        ${btn('data-sx="pt-remove-all"', 'btn-danger', 'delete', tr('sx.pt.removeAll'), anyMethod ? tr('sx.pt.t.removeAll') : tr('sx.pt.t.nothing'), !anyMethod)}
      </div>`;
    return `${head}${meHint}${table}${actions}${groupsHtml(p)}${entitiesSeenHtml(p)}`;
  }

  function groupsHtml(p) {
    const gs = Object.entries(p.groups || {});
    const names = {};
    (p.entities || []).forEach((e) => { names[e.entity] = e.name || e.user || e.sub; });
    const full = gs.length >= ((p.limits || {}).groups || 32);
    const rows = gs.map(([n, g]) => `<tr class="sx-pt-group" data-name="${esc(n)}">
        <td><code>${esc(n)}</code></td>
        <td>${esc((g.classes || []).map((c) => text(PT_CLASS, c)).join(', '))}</td>
        <td>${esc((g.clusters || []).join(', '))}</td>
        <td class="tip" ${tipAttr((g.members || []).map((m) => names[m] || m).join(', ') || tr('sx.pt.noMember'))}>${esc(String((g.members || []).length))}</td>
        <td class="sx-row-actions">${btn(`data-sx-pt-group="${esc(n)}"`, 'btn-secondary', 'edit', tr('sx.pt.editGroup'), tr('sx.pt.t.editGroup'))}
          ${btn(`data-sx-pt-group-remove="${esc(n)}"`, 'btn-secondary', 'delete', tr('sx.pt.removeGroup'), tr('sx.pt.t.removeGroup'))}</td></tr>`).join('');
    return `<div class="card sx-card sx-pt-groups">
        <h5>${icon('users')} ${esc(tr('sx.pt.groups'))}</h5>
        <p class="form-hint">${esc(tr('sx.pt.groupsIntro'))}</p>
        <div class="sx-inline-row">
          <span class="badge ${p.groups_only ? 'warn' : 'dim'}" data-sx-pt-groups-only="${p.groups_only ? 'on' : 'off'}">${esc(p.groups_only ? tr('sx.pt.groupsOnlyOn') : tr('sx.pt.groupsOnlyOff'))}</span>
          ${btn('data-sx="pt-groups-only"', 'btn-secondary', 'users', p.groups_only ? tr('sx.pt.groupsOnlyDisable') : tr('sx.pt.groupsOnlyEnable'),
            p.groups_only ? tr('sx.pt.t.groupsOnlyDisable') : tr('sx.pt.t.groupsOnlyEnable'))}
          ${btn('data-sx="pt-group-new"', 'btn-primary', 'add', tr('sx.pt.newGroup'), full ? tr('sx.pt.t.groupsFull') : tr('sx.pt.t.newGroup'), full)}
        </div>
        ${rows ? `<div class="sx-scroll"><table class="data-table sx-pt-groups-table"><thead><tr>
          <th>${esc(tr('sx.pt.col.group'))}</th><th>${esc(tr('sx.pt.col.classes'))}</th><th>${esc(tr('sx.pt.col.clusters'))}</th>
          <th>${esc(tr('sx.pt.col.members'))}</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
          : `<p class="form-hint">${esc(tr('sx.pt.noGroup'))}</p>`}
      </div>`;
  }

  function entitiesSeenHtml(p) {
    const es = p.entities || [];
    const rancherLabel = (k) => (ptRancher(k) || {}).label || k;
    return `<details class="sx-settings sx-pt-entities" ${es.length ? '' : 'data-empty'}>
        <summary class="tip" ${tipAttr(tr('sx.pt.t.entities'))}>${esc(tr('sx.pt.entities', { n: es.length }))}</summary>
        <p class="form-hint">${esc(tr('sx.pt.entitiesIntro'))}</p>
        ${es.length ? `<div class="sx-scroll"><table class="data-table sx-pt-entity-table"><thead><tr>
          <th>${esc(tr('sx.pt.col.person'))}</th><th>${esc(tr('sx.pt.col.alias'))}</th><th>${esc(tr('sx.pt.col.rancher'))}</th>
          <th>${esc(tr('sx.pt.col.last'))}</th><th>${esc(tr('sx.pt.col.entity'))}</th></tr></thead><tbody>
          ${es.map((e) => `<tr class="sx-pt-entity" data-entity="${esc(e.entity)}"><td>${esc(e.name || '–')}<div class="res-dim">${esc(e.user || '')}</div></td>
            <td><code>${esc(e.sub || '')}</code></td><td>${esc(rancherLabel(e.rancher))}</td><td>${esc(when(e.last_at))}</td>
            <td><code class="tip" ${tipAttr(e.entity)}>${esc(String(e.entity).slice(0, 8))}</code></td></tr>`).join('')}
          </tbody></table></div>` : `<p class="form-hint">${esc(tr('sx.pt.noEntity'))}</p>`}
      </details>`;
  }

  /** Commandes du mode A rendues par une action (aucune valeur secrète :
   *  l'administrateur d'OpenBao les lance chez lui). */
  function showCommands(title, cmds, policy) {
    const textValue = (cmds || []).join('\n');
    const w = modal('sx-pt-commands', title, `<p class="form-hint">${esc(tr('sx.pt.commandsIntro'))}</p>
        <pre class="sx-cmds" data-sx-pt-cmds>${esc(textValue)}</pre>
        ${btn(`data-sx-copy="${esc(textValue)}"`, 'btn-secondary', 'copy', tr('sx.copy'), tr('sx.t.copy'))}
        ${policy ? `<details><summary>${esc(tr('sx.wiz.policy'))}</summary><pre class="sx-cmds">${esc(policy)}</pre></details>` : ''}`,
      btn('data-x="close"', 'btn-primary', 'ok', tr('common.close'), tr('common.close')));
    w.el.addEventListener('click', (e) => { const c = e.target.closest('[data-sx-copy]'); if (c) copy(c.dataset.sxCopy, c); });
    return w;
  }

  async function ptGesture(url, body, doneText, cmdTitle) {
    const f = await gesture('POST', url, body, doneText);
    const out = (f && f.output) || {};
    if (f && f.ok && (out.commands || []).length) showCommands(cmdTitle || tr('sx.pt.commandsTitle'), out.commands, out.policy);
    return f;
  }

  /** Assistant d'un Rancher : Serveur (méthode posée par l'agent en B1,
   *  commandes en A), Vérifications (connexion réelle de l'administrateur),
   *  Confirmation (Activer, grisé tant qu'une vérification est rouge). */
  function openPersonWizard(key) {
    const p = cur.persons || {};
    const r = ptRancher(key);
    if (!r) return null;
    const b1 = p.mode === 'b1';
    const e = r.configured || r.pending;
    const st = { tab: 'server', keys: (e && e.keys) || 'discovery', applied: !!e, checks: null, busy: false };
    const block = ptCheckBlock(p, r);
    const tabs = ['server', 'checks', 'confirm'];
    const w = modal('sx-pt-wizard', tr('sx.pt.wizTitle', { rancher: r.label }), `
      <div class="sub-tabs sub-tabs-inline" role="tablist">${tabs.map((t) => `<button type="button" class="sub-tab tip${t === 'server' ? ' active' : ''}"
          role="tab" data-pw-tab="${t}" ${tipAttr(WIZ[t][1]())}>${esc(WIZ[t][0]())}</button>`).join('')}</div>
      <div data-pw-pane="server">
        <p class="form-hint">${esc(b1 ? tr('sx.pt.wizB1') : tr('sx.pt.wizA'))}</p>
        ${kv([[tr('sx.pt.k.rancher'), esc(r.label)], [tr('sx.pt.k.issuer'), `<code>${esc(r.issuer || '')}</code>`, tr('sx.pt.t.issuer')],
              [tr('sx.pt.k.client'), `<code>${esc(r.client_id || '')}</code>`, tr('sx.pt.t.client')],
              [tr('sx.pt.k.ca'), esc(r.has_ca ? tr('sx.pt.caSet') : tr('sx.pt.caPublic')), tr('sx.pt.t.ca')]])}
        <div class="sub-tabs sub-tabs-inline sx-modes" role="radiogroup" aria-label="${esc(tr('sx.pt.k.keys'))}">
          <button type="button" class="sub-tab tip${st.keys === 'discovery' ? ' active' : ''}" data-pw-keys="discovery" ${tipAttr(tr('sx.pt.t.discovery'))}>${esc(tr('sx.pt.keys.discovery'))}</button>
          <button type="button" class="sub-tab tip${st.keys === 'static' ? ' active' : ''}" data-pw-keys="static" ${tipAttr(b1 ? tr('sx.pt.t.staticB1') : tr('sx.pt.t.static'))}${b1 ? ' disabled' : ''}>${esc(tr('sx.pt.keys.static'))}</button>
        </div>
        ${b1 ? '' : `<label class="login-field"><span>${esc(tr('sx.pt.cidr'))}</span>
          <input type="text" data-pw="cidr" spellcheck="false" placeholder="10.0.0.5/32" value="${esc((e && e.console_cidr) || '')}" class="tip" ${tipAttr(tr('sx.pt.t.cidr'))}></label>`}
        <div class="apply-bar">${btn('data-pw="apply"', 'btn-primary', 'key', b1 ? tr('sx.pt.applyB1') : tr('sx.pt.applyA'), b1 ? tr('sx.pt.t.applyB1') : tr('sx.pt.t.applyA'))}</div>
        <p class="form-hint" data-pw="applied">${esc(st.applied ? tr('sx.pt.alreadySet') : '')}</p>
      </div>
      <div data-pw-pane="checks" hidden>
        <p class="form-hint">${esc(tr('sx.pt.checksIntro'))}</p>
        ${block ? `<p class="res-error sx-pt-block" data-pw="block">${esc(block)}</p>` : ''}
        <div class="apply-bar">${btn('data-pw="run-checks"', 'btn-primary', 'test', tr('sx.wiz.runChecks'), block || (!st.applied ? tr('sx.pt.t.applyFirst') : tr('sx.pt.t.runChecks')), !!block || !st.applied)}</div>
        <ul class="sx-checks" data-pw="checks-list" aria-live="polite"></ul>
      </div>
      <div data-pw-pane="confirm" hidden><div data-pw="summary"></div></div>
      <div class="res-feedback" data-pw="msg" role="status"></div>`,
      `${btn('data-x="close"', 'btn-secondary', '', tr('common.cancel'), tr('sx.t.cancel'))}
       ${btn('data-pw="activate"', 'btn-primary', 'lock', tr('sx.pt.activate'), tr('sx.pt.t.activateBlocked'), true)}`);
    const q = w.q;
    const msg = (html, bad) => { q('[data-pw="msg"]').innerHTML = bad ? `<span class="res-error">${html}</span>` : html; };
    function setTab(t) {
      st.tab = t;
      w.el.querySelectorAll('[data-pw-tab]').forEach((b) => { b.classList.toggle('active', b.dataset.pwTab === t); b.setAttribute('aria-selected', String(b.dataset.pwTab === t)); });
      w.el.querySelectorAll('[data-pw-pane]').forEach((x) => { x.hidden = x.dataset.pwPane !== t; });
      if (t === 'confirm') paintSummary();
    }
    // les cinq vérifications, toutes vertes, dans cette fenêtre
    function green() {
      const got = st.checks || [];
      return PT_CHECK_IDS.every((id) => got.some((c) => c.id === id && c.ok === true)) && got.every((c) => c.ok === true);
    }
    function paintActivate() {
      const why = block || (!st.applied ? tr('sx.pt.t.applyFirst') : (!green() ? tr('sx.pt.t.activateBlocked') : tr('sx.pt.t.activate')));
      const b = q('[data-pw="activate"]');
      b.disabled = why !== tr('sx.pt.t.activate');
      b.dataset.tip = why;
      b.title = why;
      const rc = q('[data-pw="run-checks"]');
      const rcWhy = block || (!st.applied ? tr('sx.pt.t.applyFirst') : tr('sx.pt.t.runChecks'));
      rc.disabled = !!block || !st.applied;
      rc.dataset.tip = rcWhy;
      rc.title = rcWhy;
    }
    function paintChecks() {
      q('[data-pw="checks-list"]').innerHTML = (st.checks || []).map((c) => `<li class="${c.ok === false ? 'fail' : c.ok ? 'ok' : 'skip'}" data-pw-check="${esc(c.id)}">
        ${icon(c.ok === false ? 'fail' : c.ok ? 'ok' : 'pending')} <b>${esc(text(PT_CHECK, c.id))}</b> <span class="res-dim">${esc(c.reason || '')}</span></li>`).join('');
      paintActivate();
    }
    function paintSummary() {
      q('[data-pw="summary"]').innerHTML = kv([[tr('sx.pt.k.rancher'), esc(r.label)],
        [tr('sx.pt.k.keys'), esc(st.keys === 'static' ? tr('sx.pt.keys.static') : tr('sx.pt.keys.discovery'))],
        [tr('sx.k.checks'), esc(green() ? tr('sx.wiz.allGreen') : tr('sx.wiz.notGreen'))]])
        + `<p class="form-hint">${esc(tr('sx.pt.after'))}</p>`;
      paintActivate();
    }
    async function apply() {
      if (st.busy) return;
      st.busy = true;
      msg(`<span class="spinner-inline"></span> ${esc(tr('sx.running'))}`);
      const body = { rancher: r.key, keys: st.keys };
      if (!b1) body.console_cidr = ((q('[data-pw="cidr"]') || {}).value || '').trim();
      const res = await api('POST', '/api/secrets/person-token/assistant', body);
      if (!res.ok) { msg(errOf(res), true); st.busy = false; return; }
      const f = await follow(res.d.action_id);
      st.busy = false;
      const out = f.output || {};
      if (!f.ok) { msg(esc(tr('sx.failed', { msg: out.error || out.code || (f.run || {}).error_summary || '?' })), true); return; }
      st.applied = true;
      st.checks = null;
      q('[data-pw="applied"]').textContent = b1 ? tr('sx.pt.setByAgent') : tr('sx.pt.commandsGiven');
      msg(`${icon('ok')} ${esc(b1 ? tr('sx.pt.setByAgent') : tr('sx.pt.commandsGiven'))}`);
      if ((out.commands || []).length) {
        const box = document.createElement('div');
        box.innerHTML = `<pre class="sx-cmds" data-pw="cmds">${esc(out.commands.join('\n'))}</pre>
          ${btn(`data-sx-copy="${esc(out.commands.join('\n'))}"`, 'btn-secondary', 'copy', tr('sx.copy'), tr('sx.t.copy'))}`;
        q('[data-pw-pane="server"]').appendChild(box);
      }
      paintChecks();
    }
    async function runChecks() {
      if (st.busy || block || !st.applied) return;
      st.busy = true;
      msg(`<span class="spinner-inline"></span> ${esc(tr('sx.wiz.checking'))}`);
      const res = await api('POST', '/api/secrets/person-token/check', { rancher: r.key });
      if (!res.ok) { msg(errOf(res), true); st.busy = false; return; }
      const f = await follow(res.d.action_id);
      st.busy = false;
      st.checks = (f.output || {}).checks || [{ id: 'login', ok: false, reason: (f.output || {}).error || (f.run || {}).error_summary || '?' }];
      msg(esc(green() ? tr('sx.wiz.allGreen') : tr('sx.wiz.notGreen')));
      paintChecks();
    }
    async function activate() {
      paintActivate();
      if (q('[data-pw="activate"]').disabled || st.busy) return;
      st.busy = true;
      msg(`<span class="spinner-inline"></span> ${esc(tr('sx.wiz.activating'))}`);
      const res = await api('POST', '/api/secrets/person-token/enable', { rancher: r.key });
      if (!res.ok) { msg(errOf(res), true); st.busy = false; return; }
      const f = await follow(res.d.action_id);
      st.busy = false;
      if (f.ok) { w.close(); say(`${icon('ok')} ${esc(tr('sx.pt.activated', { rancher: r.label }))}`); load(); return; }
      st.checks = (f.output || {}).checks || st.checks;
      paintChecks();
      msg(esc(tr('sx.failed', { msg: (f.output || {}).error || (f.output || {}).code || (f.run || {}).error_summary || '?' })), true);
    }
    w.el.addEventListener('click', (ev) => {
      const t = ev.target.closest('[data-pw-tab]');
      if (t) { setTab(t.dataset.pwTab); return; }
      const k = ev.target.closest('[data-pw-keys]');
      if (k && !k.disabled) {
        st.keys = k.dataset.pwKeys;
        st.applied = false;
        st.checks = null;
        w.el.querySelectorAll('[data-pw-keys]').forEach((b) => b.classList.toggle('active', b.dataset.pwKeys === st.keys));
        q('[data-pw="applied"]').textContent = '';
        paintChecks();
        return;
      }
      if (ev.target.closest('[data-pw="apply"]')) { apply(); return; }
      if (ev.target.closest('[data-pw="run-checks"]')) { runChecks(); return; }
      if (ev.target.closest('[data-pw="activate"]')) { activate(); return; }
      const c = ev.target.closest('[data-sx-copy]');
      if (c) copy(c.dataset.sxCopy, c);
    });
    w.el.addEventListener('input', (ev) => { if (ev.target.closest('[data-pw="cidr"]')) { st.applied = false; st.checks = null; paintChecks(); } });
    paintChecks();
    return w;
  }

  /** Groupe interne : classes, clusters, membres parmi les entités vues. */
  function openGroupModal(name) {
    const p = cur.persons || {};
    const g = name ? (p.groups || {})[name] || {} : {};
    const members = new Set(g.members || []);
    const seen = (p.entities || []);
    const unknown = [...members].filter((m) => !seen.some((e) => e.entity === m));
    const limit = (p.limits || {}).members || 200;
    const w = modal('sx-pt-group', name ? tr('sx.pt.editGroupTitle', { name }) : tr('sx.pt.newGroup'), `
      <p class="form-hint">${esc(tr('sx.pt.groupHint'))}</p>
      <label class="login-field"><span>${esc(tr('sx.pt.col.group'))}</span>
        <input type="text" data-pg="name" spellcheck="false" autocomplete="off" maxlength="31" value="${esc(name || '')}" ${name ? 'disabled' : ''}
               placeholder="team-a" class="tip" ${tipAttr(tr('sx.pt.t.groupName'))}></label>
      <fieldset class="sx-pt-classes"><legend>${esc(tr('sx.pt.col.classes'))}</legend>
        ${(p.group_classes || []).map((c) => `<label class="tip" ${tipAttr(tr('sx.pt.t.class'))}><input type="checkbox" data-pg-class="${esc(c)}" ${(g.classes || []).includes(c) ? 'checked' : ''}> ${esc(text(PT_CLASS, c))}</label>`).join(' ')}
      </fieldset>
      <label class="login-field"><span>${esc(tr('sx.pt.col.clusters'))}</span>
        <input type="text" data-pg="clusters" spellcheck="false" value="${esc((g.clusters || []).join(', '))}" placeholder="harv1, node2"
               class="tip" ${tipAttr(tr('sx.pt.t.clusters'))}></label>
      <h5>${esc(tr('sx.pt.col.members'))}</h5>
      ${seen.length || unknown.length ? `<div class="sx-scroll"><table class="data-table sx-pt-member-table"><thead><tr><th></th>
          <th>${esc(tr('sx.pt.col.person'))}</th><th>${esc(tr('sx.pt.col.alias'))}</th><th>${esc(tr('sx.pt.col.last'))}</th></tr></thead><tbody>
        ${seen.map((e) => `<tr><td><input type="checkbox" data-pg-member="${esc(e.entity)}" ${members.has(e.entity) ? 'checked' : ''} class="tip" ${tipAttr(tr('sx.pt.t.member'))}
            aria-label="${esc(e.name || e.sub || e.entity)}"></td>
          <td>${esc(e.name || '–')}<div class="res-dim">${esc(e.user || '')}</div></td><td><code>${esc(e.sub || '')}</code></td><td>${esc(when(e.last_at))}</td></tr>`).join('')}
        ${unknown.map((m) => `<tr><td><input type="checkbox" data-pg-member="${esc(m)}" checked class="tip" ${tipAttr(tr('sx.pt.t.member'))} aria-label="${esc(m)}"></td>
          <td class="res-dim">${esc(tr('sx.pt.unknownEntity'))}</td><td><code>${esc(m)}</code></td><td>–</td></tr>`).join('')}
        </tbody></table></div>` : `<p class="form-hint">${esc(tr('sx.pt.noEntity'))}</p>`}
      <p class="form-hint">${esc(tr('sx.pt.membersLimit', { n: limit }))}</p>
      <div class="res-feedback" data-pg="msg" role="status"></div>`,
      `${btn('data-x="close"', 'btn-secondary', '', tr('common.cancel'), tr('sx.t.cancel'))}
       ${btn('data-pg="save"', 'btn-primary', 'save', tr('sx.save'), tr('sx.pt.t.saveGroup'))}`);
    w.q('[data-pg="save"]').addEventListener('click', async () => {
      const body = {
        name: name || (w.q('[data-pg="name"]').value || '').trim(),
        classes: [...w.el.querySelectorAll('[data-pg-class]:checked')].map((i) => i.dataset.pgClass),
        clusters: (w.q('[data-pg="clusters"]').value || '').split(/[\s,]+/).map((x) => x.trim()).filter(Boolean),
        members: [...w.el.querySelectorAll('[data-pg-member]:checked')].map((i) => i.dataset.pgMember),
      };
      if (!/^[a-z0-9][a-z0-9-]{0,30}$/.test(body.name) || !body.classes.length || !body.clusters.length) {
        w.q('[data-pg="msg"]').innerHTML = `<span class="res-error">${esc(tr('sx.pt.groupIncomplete'))}</span>`;
        return;
      }
      if (body.members.length > limit) {
        w.q('[data-pg="msg"]').innerHTML = `<span class="res-error">${esc(tr('sx.pt.membersLimit', { n: limit }))}</span>`;
        return;
      }
      w.close();
      ptGesture('/api/secrets/person-token/groups', body, tr('sx.pt.groupSaved'));
    });
    return w;
  }

  // -- Versions -------------------------------------------------------------------------
  function versionsHtml() {
    const s = cur.status;
    if (!s.active) return `<p class="form-hint">${esc(tr('sx.needActive'))}</p>`;
    const classes = ((cur.classes || {}).classes || []).filter((r) => r.openbao > 0);
    if (!classes.length) return `<p class="form-hint">${esc(tr('sx.noVersions'))}</p>`;
    if (!cur.versionsClass || !classes.some((c) => c.class === cur.versionsClass)) cur.versionsClass = classes[0].class;
    return `<p class="form-hint">${esc(tr('sx.versionsIntro'))}</p>
      <label class="login-field sx-inline"><span>${esc(tr('sx.col.class'))}</span>
        <select data-sx="versions-class" class="tip" ${tipAttr(tr('sx.t.versionsClass'))}>
          ${classes.map((c) => `<option value="${esc(c.class)}"${c.class === cur.versionsClass ? ' selected' : ''}>${esc(text(CLASS, c.class))}</option>`).join('')}
        </select></label>
      <div class="sx-names" data-sx-names><p class="form-hint">${esc(tr('common.loading'))}</p></div>
      <div class="sx-versions" data-sx-versions></div>`;
  }
  async function loadNames(cls) {
    const box = document.querySelector('[data-sx-names]');
    if (!box) return;
    const r = await api('GET', `/api/secrets/secrets?class=${enc(cls)}`);
    if (!r.ok) { box.innerHTML = `<p class="form-hint">${errOf(r)}</p>`; return; }
    box.innerHTML = (r.d.secrets || []).length ? `<ul class="sx-name-list">${r.d.secrets.map((x) => `<li>
        ${btn(`data-sx-ref="${esc(x.ref)}"`, 'btn-secondary', 'activity', `${x.name} · ${x.field}`, tr('sx.t.history'))}</li>`).join('')}</ul>`
      : `<p class="form-hint">${esc(tr('sx.noSecrets'))}</p>`;
  }
  async function loadVersions(ref) {
    const box = document.querySelector('[data-sx-versions]');
    if (!box) return;
    box.innerHTML = `<p class="form-hint">${esc(tr('common.loading'))}</p>`;
    const r = await api('GET', `/api/secrets/versions?ref=${enc(ref)}`);
    if (!r.ok) { box.innerHTML = `<p class="form-hint">${errOf(r)}</p>`; return; }
    const vs = (r.d.versions || []).slice().reverse();
    const last = vs.length ? vs[0].version : null;
    box.innerHTML = `<h5>${esc(ref)}</h5><table class="data-table sx-version-table"><thead><tr>
        <th>${esc(tr('sx.col.version'))}</th><th>${esc(tr('sx.col.date'))}</th><th>${esc(tr('sx.col.author'))}</th>
        <th>${esc(tr('sx.col.action'))}</th><th></th></tr></thead><tbody>
        ${vs.map((v) => `<tr data-sx-version="${esc(v.version)}"><td>${esc(v.version)}${v.version === last ? ` <span class="badge ok">${esc(tr('sx.current'))}</span>` : ''}</td>
          <td>${esc(when(v.created))}</td><td>${esc(v.actor || '–')}</td>
          <td>${v.action ? `<code>${esc(v.action)}</code>` : '–'}</td>
          <td>${v.version === last || v.deleted || v.destroyed ? '' : btn(`data-sx-rollback="${esc(v.version)}" data-ref="${esc(ref)}"`, 'btn-secondary', 'undo', tr('sx.rollback'), tr('sx.t.rollback'))}</td></tr>`).join('')}
      </tbody></table><p class="form-hint">${esc(tr('sx.noValueShown'))}</p>`;
  }

  // -- Rotation -------------------------------------------------------------------------
  function rotationHtml() {
    const s = cur.status;
    const r = cur.rotation;
    if (!r) return `<p class="form-hint">${esc(tr('sx.loadError'))}</p>`;
    const rows = [];
    if (r.active) {
      rows.push(`<tr data-sx-rot="secret-id"><td>${esc(tr('sx.rot.secretId'))}</td>
        <td>${esc(age(r.secret_id.age_s))}</td>
        <td class="res-dim">${esc(r.secret_id.agent ? tr('sx.rot.secretIdAuto') : tr('sx.rot.secretIdNoAgent'))}</td>
        <td>${btn('data-sx-rotate="secret-id"', 'btn-secondary', 'refresh', tr('sx.rotateNow'), tr('sx.t.rotSecretId'), !r.secret_id.agent)}</td></tr>`);
      if (r.unseal_key) {
        rows.push(`<tr data-sx-rot="unseal-key"><td>${esc(tr('sx.rot.unsealKey'))}</td>
          <td>${esc(r.unseal_key.created ? age(Date.now() / 1000 - r.unseal_key.created) : '–')}</td>
          <td class="res-dim"><code>${esc(r.unseal_key.current || '–')}</code> ${esc(tr('sx.rot.unsealHint'))}</td>
          <td>${btn('data-sx-rotate="unseal-key"', 'btn-secondary', 'key', tr('sx.rotateNow'), tr('sx.t.rotUnseal'))}</td></tr>`);
      }
    }
    (r.kvm || []).forEach((k) => rows.push(`<tr data-sx-rot="ssh-kvm" data-host="${esc(k.host)}"><td>${esc(tr('sx.rot.kvm', { host: k.host }))}</td>
      <td>${esc(age(k.age_s))}${k.old ? ` <span class="badge warn tip" ${tipAttr(tr('sx.t.kvmOld'))}>${esc(tr('sx.old'))}</span>` : ''}</td>
      <td class="res-dim">${esc(k.store === 'openbao' ? tr('sx.where.openbao') : tr('sx.where.local'))}</td>
      <td>${btn(`data-sx-rotate="ssh-kvm" data-target="${esc(k.host)}"`, 'btn-secondary', 'key', tr('sx.rotateNow'), tr('sx.t.rotKvm'))}</td></tr>`));
    (r.transit || []).forEach((t) => rows.push(`<tr data-sx-rot="transit"><td>${esc(tr('sx.rot.transit', { decl: t.decl }))}</td><td>–</td>
      <td class="res-dim"><code>${esc(t.key || '')}</code></td>
      <td>${btn(`data-sx-rotate="transit" data-target="${esc(t.decl)}"`, 'btn-secondary', 'key', tr('sx.rotateNow'), tr('sx.t.rotTransit'))}</td></tr>`));
    const owned = (r.owned || []).map((o) => `<tr><td>${esc(text(CLASS, o.class))} · ${esc(o.name)}</td><td>${esc(age(o.age_s))}</td>
      <td class="res-dim">${esc(text(REPLACE, o.class))}</td><td></td></tr>`).join('');
    return `<p class="form-hint">${esc(tr('sx.rotationIntro'))}</p>
      <div class="table-scroll"><table class="data-table sx-rotation"><thead><tr><th>${esc(tr('sx.col.secret'))}</th>
        <th>${esc(tr('sx.col.age'))}</th><th>${esc(tr('sx.col.note'))}</th><th></th></tr></thead>
        <tbody>${rows.join('')}${owned}</tbody></table></div>
      ${tfKeysHtml()}${entitiesHtml(s)}`;
  }
  function tfKeysHtml() {
    const rows = ((cur.tfkeys || {}).keys || []);
    if (!rows.length) return '';
    return `<h5>${esc(tr('sx.tfKeysTitle'))}</h5><p class="form-hint">${esc(tr('sx.tfKeysIntro'))}</p>
      <table class="data-table sx-tfkeys"><thead><tr><th>${esc(tr('sx.col.decl'))}</th><th>${esc(tr('sx.col.retired'))}</th>
        <th>${esc(tr('sx.col.deleteAfter'))}</th><th></th></tr></thead><tbody>
      ${rows.map((k) => `<tr data-sx-tfkey="${esc(k.decl)}"><td><code>${esc(k.decl)}</code>${k.trial ? ` <span class="badge info">${esc(tr('sx.trial'))}</span>` : ''}
          ${k.error ? `<div class="res-error">${esc(k.error)}</div>` : ''}</td>
        <td>${esc(when(k.retired_at))}</td><td>${esc(when(k.delete_after))}</td>
        <td>${btn(`data-sx-tfkey-delete="${esc(k.decl)}"`, 'btn-secondary', 'delete', tr('sx.deleteNow'), tr('sx.t.tfKeyDelete'))}</td></tr>`).join('')}
      </tbody></table>`;
  }
  function entitiesHtml(s) {
    const rows = cur.entities || [];
    if (s.mode !== 'a' || !rows.length) return '';
    return `<h5>${esc(tr('sx.entitiesTitle'))}</h5><p class="form-hint">${esc(tr('sx.entitiesIntro', { addr: s.addr || '' }))}</p>
      ${rows.map((e) => `<div class="card sx-card" data-sx-entity="${esc(e.decl)}"><code>${esc(e.alias)}</code>
        <pre class="sx-cmds">${esc(e.commands.join('\n'))}</pre>
        ${btn(`data-sx-copy="${esc(e.commands.join('\n'))}"`, 'btn-secondary', 'copy', tr('sx.copy'), tr('sx.t.copy'))}
        ${btn(`data-sx-entity-done="${esc(e.decl)}"`, 'btn-primary', 'ok', tr('sx.entityDone'), tr('sx.t.entityDone'))}</div>`).join('')}`;
  }

  // -- Instantanés ------------------------------------------------------------------------
  function snapshotsHtml() {
    const s = cur.status;
    if (s.mode !== 'b1') return `<p class="form-hint">${esc(s.mode === 'a' ? tr('sx.snapModeA') : tr('sx.needB1'))}</p>`;
    const d = cur.snaps || {};
    const last = d.last || {};
    const files = (d.files || []).concat(d.agent_files || []).sort((a, b) => (b.at || 0) - (a.at || 0));
    const stale = !last.last_ok_at || (Date.now() / 1000 - last.last_ok_at) > (d.stale_after_s || 172800);
    return `<p class="form-hint">${esc(tr('sx.snapIntro'))}</p>
      <div class="sx-grid">
        <label class="login-field"><span>${esc(tr('sx.snap.dir'))}</span>
          <input type="text" data-sx-snapset="snapshot_dir" value="${esc(d.dir || '')}" placeholder="/var/lib/harvester-ops-snapshots" spellcheck="false" class="tip" ${tipAttr(tr('sx.t.snapDir'))}></label>
        <label class="login-field"><span>${esc(tr('sx.snap.time'))}</span>
          <input type="time" data-sx-snapset="snapshot_time" value="${esc(d.time || '02:30')}" class="tip" ${tipAttr(tr('sx.t.snapTime'))}></label>
        <label class="login-field"><span>${esc(tr('sx.snap.keep'))}</span>
          <input type="number" min="1" max="365" data-sx-snapset="snapshot_keep" value="${esc(d.keep || 14)}" class="tip" ${tipAttr(tr('sx.t.snapKeep'))}></label>
      </div>
      <div class="apply-bar">
        ${btn('data-sx="save-snap"', 'btn-secondary', 'save', tr('sx.save'), tr('sx.t.snapSave'))}
        ${btn('data-sx="snapshot"', 'btn-primary', 'snapshot', tr('sx.snapNow'), d.dir ? tr('sx.t.snapNow') : tr('sx.why.noDir'), !d.dir)}
      </div>
      <p class="${stale ? 'res-error' : 'form-hint'}" data-sx-last>${esc(last.last_ok_at ? tr('sx.snapLast', { when: when(last.last_ok_at), file: last.last_file || '' }) : tr('sx.snapNever'))}
        ${last.last_error ? ` · ${esc(tr('sx.snapLastError', { when: when(last.last_error_at), code: last.last_error }))}` : ''}</p>
      ${files.length ? `<div class="table-scroll"><table class="data-table sx-snaps"><thead><tr><th>${esc(tr('sx.col.file'))}</th>
        <th>${esc(tr('sx.col.date'))}</th><th>${esc(tr('sx.col.size'))}</th><th>${esc(tr('sx.col.where'))}</th><th></th></tr></thead><tbody>
        ${files.map((f) => `<tr data-sx-snap="${esc(f.file)}"><td><code>${esc(f.file)}</code></td><td>${esc(when(f.at))}</td><td>${esc(bytes(f.bytes))}</td>
          <td>${esc(f.where === 'agent' ? tr('sx.snap.agent') : tr('sx.snap.dirShort'))}</td>
          <td>${btn(`data-sx-restore="${esc(f.file)}"`, 'btn-secondary', 'restore', tr('sx.restore'), tr('sx.t.restore'))}</td></tr>`).join('')}
        </tbody></table></div>` : `<p class="form-hint">${esc(tr('sx.noSnapshots'))}</p>`}
      <p class="form-hint">${esc(tr('sx.snapKeyApart'))}</p>`;
  }

  // -- assistant d'activation --------------------------------------------------------
  function openWizard() {
    const st = { tab: 'server', mode: 'b1', checks: null, checking: false, bundles: [], agent: false, commands: null };
    const tabs = ['server', 'checks', 'confirm'];
    const w = modal('sx-wizard', tr('sx.wiz.title'), `
      <div class="sub-tabs sub-tabs-inline" role="tablist">${tabs.map((t) => `<button type="button" class="sub-tab tip${t === 'server' ? ' active' : ''}"
          role="tab" data-wz-tab="${t}" ${tipAttr(WIZ[t][1]())}>${esc(WIZ[t][0]())}</button>`).join('')}</div>
      <div data-wz-pane="server">
        <div class="sub-tabs sub-tabs-inline sx-modes" role="radiogroup" aria-label="${esc(tr('sx.wiz.mode'))}">
          <button type="button" class="sub-tab active tip" data-wz-mode="b1" ${tipAttr(tr('sx.wiz.b1Tip'))}>${esc(tr('sx.mode.b1'))}</button>
          <button type="button" class="sub-tab tip" data-wz-mode="a" ${tipAttr(tr('sx.wiz.aTip'))}>${esc(tr('sx.mode.a'))}</button>
        </div>
        <div data-wz-b1>
          <p class="form-hint">${esc(tr('sx.wiz.b1Intro'))}</p>
          <label class="login-field"><span>${esc(tr('sx.wiz.bundle'))}</span>
            <select data-wz="bundle" class="tip" ${tipAttr(tr('sx.t.bundle'))}></select></label>
          <p class="form-hint" data-wz="agent"></p>
        </div>
        <div data-wz-a hidden>
          <p class="form-hint">${esc(tr('sx.wiz.aIntro'))}</p>
          <div class="sx-grid">
            <label class="login-field"><span>${esc(tr('sx.wiz.addr'))}</span><input type="url" data-wz="addr" placeholder="https://openbao.example:8200" spellcheck="false" class="tip" ${tipAttr(tr('sx.t.addr'))}></label>
            <label class="login-field"><span>${esc(tr('sx.wiz.cidr'))}</span><input type="text" data-wz="cidr" placeholder="10.0.0.5/32" spellcheck="false" class="tip" ${tipAttr(tr('sx.t.cidr'))}></label>
            <label class="login-field"><span>${esc(tr('sx.wiz.mount'))}</span><input type="text" data-wz="mount" value="harvester-ops" spellcheck="false" class="tip" ${tipAttr(tr('sx.t.wizMount'))}></label>
            <label class="login-field"><span>${esc(tr('sx.wiz.prefix'))}</span><input type="text" data-wz="prefix" placeholder="consoles/…" spellcheck="false" class="tip" ${tipAttr(tr('sx.t.prefix'))}></label>
          </div>
          <label class="login-field"><span>${esc(tr('sx.wiz.ca'))}</span><textarea data-wz="ca" rows="3" spellcheck="false" placeholder="-----BEGIN CERTIFICATE-----" class="tip" ${tipAttr(tr('sx.t.ca'))}></textarea></label>
          <div class="apply-bar">${btn('data-wz="commands"', 'btn-secondary', 'code', tr('sx.wiz.commands'), tr('sx.t.commands'))}</div>
          <div data-wz="commands-box"></div>
          <div class="sx-grid">
            <label class="login-field"><span>${esc(tr('sx.wiz.roleId'))}</span><input type="text" data-wz="role" spellcheck="false" autocomplete="off" class="tip" ${tipAttr(tr('sx.t.roleId'))}></label>
            <label class="login-field"><span>${esc(tr('sx.wiz.wrap'))}</span><input type="password" data-wz="wrap" autocomplete="new-password" class="tip" ${tipAttr(tr('sx.t.wrap'))}></label>
          </div>
        </div>
      </div>
      <div data-wz-pane="checks" hidden>
        <p class="form-hint">${esc(tr('sx.wiz.checksIntro'))}</p>
        <div class="apply-bar">${btn('data-wz="run-checks"', 'btn-primary', 'test', tr('sx.wiz.runChecks'), tr('sx.t.runChecks'))}</div>
        <ul class="sx-checks" data-wz="checks-list" aria-live="polite"></ul>
      </div>
      <div data-wz-pane="confirm" hidden><div data-wz="summary"></div></div>
      <div class="res-feedback" data-wz="msg" role="status"></div>`,
      `${btn('data-x="close"', 'btn-secondary', '', tr('common.cancel'), tr('sx.t.cancel'))}
       <span class="tip" data-wz="activate-wrap">${btn('data-wz="activate"', 'btn-primary', 'lock', tr('sx.wiz.activate'), tr('sx.t.activateBlocked'), true)}</span>`);
    const q = w.q;
    const val = (k) => (q(`[data-wz="${k}"]`) || {}).value || '';
    function setTab(t) {
      st.tab = t;
      w.el.querySelectorAll('[data-wz-tab]').forEach((b) => { b.classList.toggle('active', b.dataset.wzTab === t); b.setAttribute('aria-selected', String(b.dataset.wzTab === t)); });
      w.el.querySelectorAll('[data-wz-pane]').forEach((p) => { p.hidden = p.dataset.wzPane !== t; });
      if (t === 'confirm') paintSummary();
    }
    function green() { return !!st.checks && st.checks.length > 0 && st.checks.every((c) => c.ok !== false) && st.checks.some((c) => c.ok); }
    function paintActivate() {
      const why = st.mode === 'b1' && !val('bundle') ? tr('sx.t.needBundle') : (!green() ? tr('sx.t.activateBlocked') : tr('sx.t.activate'));
      const b = q('[data-wz="activate"]');
      b.disabled = why !== tr('sx.t.activate');
      b.dataset.tip = why;
      b.title = why;
    }
    function paintChecks() {
      const ul = q('[data-wz="checks-list"]');
      ul.innerHTML = (st.checks || []).map((c) => `<li class="${c.ok === false ? 'fail' : c.ok ? 'ok' : 'skip'}" data-wz-check="${esc(c.id)}">
        ${icon(c.ok === false ? 'fail' : c.ok ? 'ok' : 'pending')} <b>${esc(text(CHECK, c.id))}</b> <span class="res-dim">${esc(c.reason || '')}</span></li>`).join('');
      paintActivate();
    }
    function paintSummary() {
      const box = q('[data-wz="summary"]');
      box.innerHTML = kv([[tr('sx.k.mode'), esc(text(MODE, st.mode))],
        st.mode === 'a' ? [tr('sx.k.addr'), esc(val('addr'))] : [tr('sx.wiz.bundle'), esc(val('bundle') || '–')],
        [tr('sx.k.checks'), esc(green() ? tr('sx.wiz.allGreen') : tr('sx.wiz.notGreen'))]])
        + `<p class="form-hint">${esc(st.mode === 'b1' ? tr('sx.wiz.b1After') : tr('sx.wiz.aAfter'))}</p>`;
      paintActivate();
    }
    function invalidate() { st.checks = null; paintChecks(); }
    function setMode(m) {
      st.mode = m;
      w.el.querySelectorAll('[data-wz-mode]').forEach((b) => b.classList.toggle('active', b.dataset.wzMode === m));
      q('[data-wz-b1]').hidden = m !== 'b1';
      q('[data-wz-a]').hidden = m !== 'a';
      invalidate();
    }
    async function loadBundles() {
      const r = await api('GET', '/api/secrets/bundles');
      st.bundles = r.ok ? r.d.bundles || [] : [];
      st.agent = r.ok && r.d.agent;
      q('[data-wz="bundle"]').innerHTML = st.bundles.length ? st.bundles.map((b) => `<option value="${esc(b)}">${esc(b)}</option>`).join('')
        : `<option value="">${esc(tr('sx.wiz.noBundle'))}</option>`;
      q('[data-wz="agent"]').textContent = st.agent ? tr('sx.wiz.agentOk') : tr('sx.wiz.agentMissing');
      paintActivate();
    }
    async function commands() {
      const box = q('[data-wz="commands-box"]');
      const r = await api('GET', `/api/secrets/assistant?addr=${enc(val('addr'))}&cidr=${enc(val('cidr'))}&mount=${enc(val('mount'))}&prefix=${enc(val('prefix'))}`);
      if (!r.ok) { box.innerHTML = `<p class="res-error">${errOf(r)}</p>`; return; }
      const cmds = (r.d.commands || []).join('\n');
      if (!val('prefix') && r.d.prefix) q('[data-wz="prefix"]').value = r.d.prefix;
      box.innerHTML = `<p class="form-hint">${esc(tr('sx.wiz.commandsIntro'))}</p><pre class="sx-cmds" data-wz="cmds">${esc(cmds)}</pre>
        ${btn(`data-sx-copy="${esc(cmds)}"`, 'btn-secondary', 'copy', tr('sx.copy'), tr('sx.t.copy'))}
        <details><summary>${esc(tr('sx.wiz.policy'))}</summary><pre class="sx-cmds">${esc(r.d.policy || '')}</pre></details>`;
    }
    async function runChecks() {
      if (st.checking) return;
      st.checking = true;
      const msg = q('[data-wz="msg"]');
      msg.innerHTML = `<span class="spinner-inline"></span> ${esc(tr('sx.wiz.checking'))}`;
      if (st.mode === 'b1') {
        // B1 n'existe pas encore : préalables vérifiables ici (agent de
        // l'hôte, paquet déposé) ; les vérifications complètes tournent
        // après la pose, et l'activation est refusée si l'une est rouge
        await loadBundles();
        st.checks = [{ id: 'agent', ok: !!st.agent, reason: st.agent ? tr('sx.wiz.agentOk') : tr('sx.wiz.agentMissing') },
                     { id: 'bundle', ok: !!val('bundle'), reason: val('bundle') || tr('sx.wiz.noBundle') },
                     { id: 'after', ok: null, reason: tr('sx.wiz.b1ChecksAfter') }];
        st.checking = false;
        msg.textContent = green() ? tr('sx.wiz.allGreen') : tr('sx.wiz.notGreen');
        paintChecks();
        return;
      }
      const body = { mode: 'a', addr: val('addr'), mount: val('mount'), prefix: val('prefix'), ca_pem: val('ca') || undefined,
                     wrap_token: val('wrap') || undefined, transit: true };
      const r = await api('POST', '/api/secrets/check', body);
      if (!r.ok) { msg.innerHTML = `<span class="res-error">${errOf(r)}</span>`; st.checking = false; return; }
      const f = await follow(r.d.action_id);
      st.checks = (f.output || {}).checks || [{ id: 'openbao', ok: false, reason: (f.output || {}).error || (f.run || {}).error_summary || '?' }];
      st.checking = false;
      msg.textContent = green() ? tr('sx.wiz.allGreen') : tr('sx.wiz.notGreen');
      paintChecks();
    }
    async function activate() {
      paintActivate();
      if (q('[data-wz="activate"]').disabled) return;
      const msg = q('[data-wz="msg"]');
      msg.innerHTML = `<span class="spinner-inline"></span> ${esc(tr('sx.wiz.activating'))}`;
      const body = st.mode === 'b1' ? { mode: 'b1', bundle: val('bundle') }
        : { mode: 'a', addr: val('addr'), mount: val('mount'), prefix: val('prefix'), role_id: val('role'),
            wrap_token: val('wrap'), ca_pem: val('ca') || undefined };
      const r = await api('POST', '/api/secrets/enable', body);
      q('[data-wz="wrap"]').value = '';                    // jamais réaffiché
      if (!r.ok) { msg.innerHTML = `<span class="res-error">${errOf(r)}</span>`; return; }
      const f = await follow(r.d.action_id);
      if (f.ok) { w.close(); say(`${icon('ok')} ${esc(st.mode === 'b1' ? tr('sx.wiz.b1Done') : tr('sx.wiz.aDone'))}`); load(); }
      else {
        st.checks = (f.output || {}).checks || st.checks;
        paintChecks();
        msg.innerHTML = `<span class="res-error">${esc(tr('sx.failed', { msg: (f.output || {}).error || (f.output || {}).code || (f.run || {}).error_summary || '?' }))}</span>`;
      }
    }
    w.el.addEventListener('click', (e) => {
      const t = e.target.closest('[data-wz-tab]');
      if (t) { setTab(t.dataset.wzTab); return; }
      const m = e.target.closest('[data-wz-mode]');
      if (m) { setMode(m.dataset.wzMode); return; }
      if (e.target.closest('[data-wz="commands"]')) { commands(); return; }
      if (e.target.closest('[data-wz="run-checks"]')) { runChecks(); return; }
      if (e.target.closest('[data-wz="activate"]')) { activate(); return; }
      const c = e.target.closest('[data-sx-copy]');
      if (c) copy(c.dataset.sxCopy, c);
    });
    w.el.addEventListener('input', (e) => { if (e.target.closest('[data-wz]') && e.target.tagName !== 'BUTTON') invalidate(); });
    w.el.addEventListener('change', (e) => { if (e.target.closest('[data-wz="bundle"]')) invalidate(); });
    loadBundles();
    paintChecks();
    return w;
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

  // -- gestes ---------------------------------------------------------------------------
  async function onClick(e) {
    const tab = e.target.closest('[data-sx-tab]');
    if (tab) { cur.tab = tab.dataset.sxTab; shell(); load(); return; }
    const s = cur.status || {};
    const cid = s.console_id || '';
    const a = e.target.closest('[data-sx]');
    const k = a ? a.dataset.sx : null;
    if (k === 'enable') { openWizard(); return; }
    if (k === 'cache') { gesture('POST', '/api/secrets/cache/clear', {}, tr('sx.cacheCleared')); return; }
    if (k === 'outage') { gesture('POST', '/api/secrets/outage-test', {}, tr('sx.outageDone')); return; }
    if (k === 'disable') {
      const v = await typedConfirm({ title: tr('sx.disable'), intro: tr('sx.disableIntro'), expect: cid,
                                     label: tr('sx.typeConsoleId'), action: tr('sx.disable') });
      if (v) gesture('POST', '/api/secrets/disable', { confirm: v, remove_b1: false }, tr('sx.disabled'));
      return;
    }
    if (k === 'export') { exportKey(cid); return; }
    if (k === 'ack') { ackKey(); return; }
    if (k === 'vault-ack') { gesture('POST', '/api/secrets/tf-encryption/ack', {}, tr('sx.vaultAcked')); return; }
    if (k === 'save-settings') {
      const body = {};
      document.querySelectorAll('[data-sx-set]').forEach((i) => { body[i.dataset.sxSet] = i.value; });
      gesture('PUT', '/api/secrets/settings', body, tr('sx.saved'));
      return;
    }
    if (k === 'save-snap') {
      const body = {};
      document.querySelectorAll('[data-sx-snapset]').forEach((i) => { body[i.dataset.sxSnapset] = i.value; });
      gesture('PUT', '/api/secrets/settings', body, tr('sx.saved'));
      return;
    }
    if (k === 'eso-listener-save') {
      const v = (document.querySelector('[data-sx-eso-addr]') || {}).value || '';
      gesture('POST', '/api/secrets/eso/listener', { address: v.trim() }, tr('sx.eso.saved'));
      return;
    }
    if (k === 'eso-listener-off') {
      if (confirm(tr('sx.eso.offConfirm'))) gesture('POST', '/api/secrets/eso/listener', { off: true }, tr('sx.eso.turnedOff'));
      return;
    }
    if (k === 'pt-off') {
      if (confirm(tr('sx.pt.offConfirm'))) ptGesture('/api/secrets/person-token/disable', {}, tr('sx.pt.turnedOff'));
      return;
    }
    if (k === 'pt-remove-all') {
      const v = await typedConfirm({ title: tr('sx.pt.removeAll'), intro: tr('sx.pt.removeAllIntro'), expect: cid,
                                     label: tr('sx.typeConsoleId'), action: tr('sx.pt.removeAll') });
      if (v) ptGesture('/api/secrets/person-token/disable', { remove: true, confirm: v }, tr('sx.pt.removedAll'));
      return;
    }
    if (k === 'pt-groups-only') {
      const on = !((cur.persons || {}).groups_only);
      if (confirm(on ? tr('sx.pt.groupsOnlyConfirmOn') : tr('sx.pt.groupsOnlyConfirmOff'))) {
        ptGesture('/api/secrets/person-token/groups-only', { on }, tr('sx.saved'));
      }
      return;
    }
    if (k === 'pt-group-new') { openGroupModal(null); return; }
    const pts = e.target.closest('[data-sx-pt-setup]');
    if (pts) { openPersonWizard(pts.dataset.sxPtSetup); return; }
    const ptk = e.target.closest('[data-sx-pt-keys]');
    if (ptk) { ptGesture('/api/secrets/person-token/keys', { rancher: ptk.dataset.sxPtKeys }, tr('sx.pt.keysUpdated')); return; }
    const ptc = e.target.closest('[data-sx-pt-confirm-keys]');
    if (ptc) {
      if (confirm(tr('sx.pt.confirmKeysConfirm'))) ptGesture('/api/secrets/person-token/keys', { rancher: ptc.dataset.sxPtConfirmKeys, confirm: true }, tr('sx.pt.keysConfirmed'));
      return;
    }
    const ptr = e.target.closest('[data-sx-pt-remove]');
    if (ptr) {
      const key = ptr.dataset.sxPtRemove;
      const label = (ptRancher(key) || {}).label || key;
      const v = await typedConfirm({ title: tr('sx.pt.remove'), intro: tr('sx.pt.removeIntro', { rancher: label }), expect: cid,
                                     label: tr('sx.typeConsoleId'), action: tr('sx.pt.remove') });
      if (v) ptGesture('/api/secrets/person-token/disable', { rancher: key, remove: true, confirm: v }, tr('sx.pt.removed'));
      return;
    }
    const ptg = e.target.closest('[data-sx-pt-group]');
    if (ptg) { openGroupModal(ptg.dataset.sxPtGroup); return; }
    const ptgr = e.target.closest('[data-sx-pt-group-remove]');
    if (ptgr) {
      const name = ptgr.dataset.sxPtGroupRemove;
      if (confirm(tr('sx.pt.removeGroupConfirm', { name }))) ptGesture('/api/secrets/person-token/groups', { name, remove: true }, tr('sx.pt.groupRemoved'));
      return;
    }
    const ek = e.target.closest('[data-sx-eso-keys]');
    if (ek) { gesture('POST', '/api/secrets/eso/keys', { cluster: ek.dataset.sxEsoKeys }, tr('sx.eso.keysUpdated')); return; }
    if (k === 'snapshot') { gesture('POST', '/api/secrets/snapshot', {}, tr('sx.snapTaken')); return; }
    const mig = e.target.closest('[data-sx-migrate]');
    if (mig) {
      const c = mig.dataset.sxMigrate;
      if (confirm(tr('sx.migrateConfirm', { cls: text(CLASS, c) }))) gesture('POST', '/api/secrets/migrate', { class: c, to: 'openbao' }, tr('sx.migrated'));
      return;
    }
    const cf = e.target.closest('[data-sx-confirm]');
    if (cf) {
      const c = cf.dataset.sxConfirm;
      if (confirm(tr('sx.confirmConfirm', { cls: text(CLASS, c) }))) gesture('POST', '/api/secrets/confirm', { class: c }, tr('sx.confirmed'));
      return;
    }
    const bk = e.target.closest('[data-sx-back]');
    if (bk) {
      const c = bk.dataset.sxBack;
      if (confirm(tr('sx.backConfirm', { cls: text(CLASS, c) }))) gesture('POST', '/api/secrets/migrate', { class: c, to: 'local' }, tr('sx.backDone'));
      return;
    }
    const ref = e.target.closest('[data-sx-ref]');
    if (ref) { loadVersions(ref.dataset.sxRef); return; }
    const rb = e.target.closest('[data-sx-rollback]');
    if (rb) {
      if (confirm(tr('sx.rollbackConfirm', { v: rb.dataset.sxRollback }))) {
        const f = await gesture('POST', '/api/secrets/rollback', { ref: rb.dataset.ref, version: Number(rb.dataset.sxRollback) }, tr('sx.rolledBack'));
        if (f) setTimeout(() => loadVersions(rb.dataset.ref), 300);
      }
      return;
    }
    const rot = e.target.closest('[data-sx-rotate]');
    if (rot) {
      const what = rot.dataset.sxRotate;
      const target = rot.dataset.target || '';
      if (what === 'unseal-key') {
        const v = await typedConfirm({ title: tr('sx.rot.unsealKey'), intro: tr('sx.rotUnsealIntro'), expect: cid,
                                       label: tr('sx.typeConsoleId'), action: tr('sx.rotateNow') });
        if (v) gesture('POST', '/api/secrets/rotate', { what, confirm: v }, tr('sx.rotated'));
        return;
      }
      const msg = { 'secret-id': tr('sx.rotConfirmSecretId'), 'ssh-kvm': tr('sx.rotConfirmKvm', { host: target }),
                    transit: tr('sx.rotConfirmTransit', { decl: target }) }[what];
      if (confirm(msg)) gesture('POST', '/api/secrets/rotate', { what, target }, tr('sx.rotated'));
      return;
    }
    const del = e.target.closest('[data-sx-tfkey-delete]');
    if (del) {
      const decl = del.dataset.sxTfkeyDelete;
      const v = await typedConfirm({ title: tr('sx.tfKeysTitle'), intro: tr('sx.tfKeyDeleteIntro'), expect: decl,
                                     label: tr('sx.typeDecl'), action: tr('sx.deleteNow') });
      if (v) gesture('POST', '/api/secrets/tf-keys/delete', { decl, confirm: v }, tr('sx.tfKeyDeleted'));
      return;
    }
    const ed = e.target.closest('[data-sx-entity-done]');
    if (ed) { gesture('POST', '/api/secrets/tf-entity-done', { decl: ed.dataset.sxEntityDone }, tr('sx.done')); return; }
    const rs = e.target.closest('[data-sx-restore]');
    if (rs) {
      // relecture finale I2 : ce que la restauration casserait, lu avant tout
      // arrêt ; la saisie de l'identifiant vaut acquittement de cette liste
      const file = rs.dataset.sxRestore;
      const pre = await api('GET', `/api/secrets/restore/preflight?file=${encodeURIComponent(file)}`);
      if (!pre.ok) { say(errOf(pre), true); return; }
      const p = pre.d || {};
      const details = [];
      (p.busy || []).forEach((a) => details.push(tr('sx.pre.busy', { action: a.action })));
      (p.tf_declarations || []).forEach((d) => details.push(tr('sx.pre.tf', { name: d.name || d.id })));
      (p.kvm_hosts || []).forEach((h) => details.push(tr('sx.pre.kvm', { host: h.host })));
      (p.classes || []).forEach((c) => details.push(tr('sx.pre.cls', { cls: c })));
      if (p.snapshot_at == null) details.push(tr('sx.pre.undated'));
      const v = await typedConfirm({ title: tr('sx.restore'), intro: tr('sx.restoreIntro', { file }), details,
                                     expect: cid, label: tr('sx.typeConsoleId'), action: tr('sx.restore') });
      if (v) {
        gesture('POST', '/api/secrets/restore', { file, confirm: v, ack_risks: !!p.risks,
                                                  force: (p.busy || []).length > 0 }, tr('sx.restored'));
      }
      return;
    }
    const cp = e.target.closest('[data-sx-copy]');
    if (cp) copy(cp.dataset.sxCopy, cp);
  }

  async function exportKey(cid) {
    const v = await typedConfirm({ title: tr('sx.offlineTitle'), intro: tr('sx.offlineExportIntro'), expect: cid,
                                   label: tr('sx.typeConsoleId'), action: tr('sx.offlineExport'), danger: false });
    if (!v) return;
    say(`<span class="spinner-inline"></span> ${esc(tr('sx.running'))}`);
    const r = await api('POST', '/api/secrets/offline-copy/export', { confirm: v });
    if (!r.ok) { say(errOf(r), true); return; }
    const f = await follow(r.d.action_id);
    if (!f.ok) { say(esc(tr('sx.failed', { msg: (f.output || {}).error || '?' })), true); return; }
    const key = (f.output || {}).key_id || '';
    say(`${icon('download')} <a href="/api/secrets/offline-copy/download?action=${enc(r.d.action_id)}" download data-sx-download
        class="tip" ${tipAttr(tr('sx.t.downloadOnce'))}>${esc(tr('sx.offlineDownload', { key }))}</a> · ${esc(tr('sx.offlineOnce'))}`);
  }
  async function ackKey() {
    const key = ((cur.status || {}).agent || {}).current_key || '';
    const v = await typedConfirm({ title: tr('sx.offlineAck'), intro: tr('sx.offlineAckIntro'), expect: key,
                                   label: tr('sx.typeKeyId'), action: tr('sx.offlineAck'), danger: false });
    if (v) gesture('POST', '/api/secrets/offline-copy/ack', { key_id: v }, tr('sx.offlineAckDone'));
  }

  function onChange(e) {
    const sel = e.target.closest('[data-sx="versions-class"]');
    if (sel) { cur.versionsClass = sel.value; const v = document.querySelector('[data-sx-versions]'); if (v) v.innerHTML = ''; loadNames(sel.value); }
  }

  // -- bandeau global --------------------------------------------------------------------
  let bannerTimer = null;
  async function banner() {
    const el = document.getElementById('secrets-banner');
    if (!el) return;
    const r = await api('GET', '/api/secrets/banner');
    const b = r.ok ? r.d : null;
    if (!b || !b.active || !['down', 'degraded'].includes(b.state)) {
      // 2.10.0 : refus personnel de cette session, clés changées (administrateur)
      const notes = bannerNotes(b);
      if (!notes.length) { el.hidden = true; el.innerHTML = ''; return; }
      el.hidden = false;
      el.className = 'access-notice secrets-banner sx-notes';
      el.innerHTML = `<span class="an-icon">${icon('warn', 16)}</span><div class="an-body"><strong class="an-title">${esc(tr('sx.banner.notesTitle'))}</strong>
        ${notes.map((n) => `<p class="an-hint" data-sx-banner-note="${esc(n.code)}">${esc(n.text)}${n.tab
          ? ` ${btn(`data-sx-banner-open="${esc(n.tab)}"`, 'btn-secondary', 'settings', tr('sx.banner.open'), tr('sx.banner.t.open'))}` : ''}</p>`).join('')}</div>`;
      el.querySelectorAll('[data-sx-banner-open]').forEach((x) => x.addEventListener('click', () => openTab(x.dataset.sxBannerOpen)));
      return;
    }
    const msg = b.state === 'degraded'
      ? tr('sx.banner.degraded', { since: when(b.down_since), until: b.cache_until ? new Date(b.cache_until * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '–' })
      : tr('sx.banner.down', { since: when(b.down_since) });
    el.hidden = false;
    el.className = `access-notice secrets-banner sx-${esc(b.state)}`;
    el.innerHTML = `<span class="an-icon">${icon('warn', 16)}</span><div class="an-body"><strong class="an-title">${esc(tr('sx.banner.title'))}</strong>
      <span data-sx-banner-text>${esc(msg)}</span>${b.error ? ` <code>${esc(b.error)}</code>` : ''}
      <p class="an-hint">${esc(tr('sx.banner.hint'))}</p></div>`;
  }
  function bannerNotes(b) {
    if (!b || !b.active) return [];
    const out = [];
    const pn = b.person || null;
    if (pn) {
      const t = { 'openbao-person-login-failed': tr('sx.banner.personLogin'), 'openbao-person-no-token': tr('sx.banner.personNoToken'),
                  'openbao-person-expired': tr('sx.banner.personNoToken'), 'openbao-denied': tr('sx.banner.personDenied') }[pn.code];
      if (t) out.push({ code: pn.code, text: t });
    }
    const admin = window.App && App.role && App.role() === 'admin';
    if (admin && b.jwt_keys_changed) out.push({ code: 'openbao-jwt-keys-changed', text: tr('sx.banner.jwtKeys'), tab: 'persons' });
    if (admin && b.jwt_keys_unreadable) out.push({ code: 'openbao-jwt-keys-unreadable', text: tr('sx.banner.jwtUnreadable'), tab: 'persons' });
    if (admin && b.eso_keys_changed) out.push({ code: 'eso-keys-changed', text: tr('sx.banner.esoKeys'), tab: 'status' });
    return out;
  }
  function startBanner() {
    if (bannerTimer) return;
    banner();
    // les notes de l'administrateur attendent que le rôle soit connu
    document.addEventListener('hops:role', () => banner());
    bannerTimer = setInterval(() => { if (!document.hidden) banner(); }, 30000);
  }

  // -- vue d'ensemble Disaster Recovery : objet protégé « Magasin de secrets » -----------
  async function drCard(el) {
    if (!el) return;
    const r = await api('GET', '/api/secrets/status');
    const s = r.ok ? r.d : null;
    if (!s || !s.active) { el.hidden = true; el.innerHTML = ''; return; }
    el.hidden = false;
    const last = s.snapshots || {};
    const warn = (s.warnings || []).filter((w) => ['snapshot-stale', 'snapshot-failed', 'snapshot-dir-unset'].includes(w.code));
    const admin = window.App && App.role && App.role() === 'admin';
    const body = s.mode === 'b1'
      ? `${esc(last.last_ok_at ? tr('sx.dr.last', { when: when(last.last_ok_at) }) : tr('sx.snapNever'))} · ${esc(tr('sx.dr.keep', { n: (s.settings || {}).snapshot_keep || 14, time: (s.settings || {}).snapshot_time || '02:30' }))}`
      : esc(tr('sx.dr.modeA'));
    el.innerHTML = `<div class="sto-finding ${warn.length ? 'sev-action' : 'sev-info'} sx-dr" data-sx-dr="${esc(warn.length ? 'warn' : 'ok')}">
      <div class="sto-finding-title">${icon('shield')} ${esc(tr('sx.dr.title'))} ${warn.length ? `<span class="badge warn">${esc(text(WARN, warn[0].code))}</span>` : ''}</div>
      <div class="sto-finding-facts">${body}</div>
      ${s.mode === 'b1' && admin ? btn('data-sx-dr-open', 'btn-secondary', 'restore', tr('sx.dr.restore'), tr('sx.t.drRestore')) : ''}</div>`;
    el.querySelector('[data-sx-dr-open]')?.addEventListener('click', () => openTab('snapshots'));
  }

  function openTab(sub) {
    if (sub) cur.tab = sub;
    if (typeof Settings !== 'undefined' && Settings.openTab) Settings.openTab('secrets');
  }

  function init() {
    const box = document.getElementById('secrets-settings-body');
    if (box) {
      box.addEventListener('click', onClick);
      box.addEventListener('change', onChange);
    }
    document.querySelector('.settings-tab[data-stab="secrets"]')?.addEventListener('click', load);
    startBanner();
  }
  document.addEventListener('DOMContentLoaded', init);

  return { load, openTab, openWizard, banner, drCard, _state: cur };
})();
window.Secrets = Secrets;
