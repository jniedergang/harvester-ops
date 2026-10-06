/**
 * harvester-ops : fausse API de la démo du site (v1.86.0)
 *
 * La démo est la vraie interface de la console, servie en fichiers
 * statiques. Ce script, chargé avant elle, répond à sa place à tout ce
 * qu'elle demande à /api/ :
 *  - les lectures (GET) viennent d'un enregistrement de la console réelle
 *    sur des clusters de test, anonymisé (window.__HARVOPS_DEMO__, data.js) ;
 *  - les gestes (POST, PUT, PATCH, DELETE) ne touchent rien : chacun devient
 *    une action simulée, visible dans le dock et l'onglet Activity, qui
 *    avance par étapes puis se termine ;
 *  - démarrer, arrêter ou redémarrer une VM change son état dans la démo
 *    (en démarrage, puis en marche), pour que l'interface vive.
 * Les flux d'une action (/api/stream/<id>) sont rejoués par un faux
 * EventSource. Les consoles VNC et série demandent une vraie VM : la démo le
 * dit.
 */
(() => {
  // Un lien du site « essayer dans la démo » porte l'onglet dans l'ancre
  // (#allvms) : la console, elle, reprend l'onglet mémorisé par le navigateur.
  try {
    const tab = decodeURIComponent((location.hash || '').slice(1));
    if (/^[a-z]+$/.test(tab)) localStorage.setItem('harvester_ops_current_tab', tab);
    const lang = new URLSearchParams(location.search).get('lang');
    if (/^(en|fr|es|it|de)$/.test(lang || '')) localStorage.setItem('harvester_ops_language', lang);
  } catch { /* stockage indisponible */ }
  const DEMO = window.__HARVOPS_DEMO__ || { responses: {} };
  const R = DEMO.responses || {};
  const META = DEMO.meta || {};
  try {
    const cur = localStorage.getItem('harvester_ops_current_cluster');
    if (META.clusters && META.clusters.length && !META.clusters.includes(cur)) {
      localStorage.setItem('harvester_ops_current_cluster', META.clusters[0]);
    }
  } catch { /* stockage indisponible */ }

  // Les dates de l'enregistrement glissent jusqu'à maintenant : « prochaine
  // copie dans 33 s » reste vrai le jour où l'on ouvre la démo.
  const SHIFT = META.recorded_at ? Date.now() / 1000 - META.recorded_at : 0;
  const ISO = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?Z$/;
  const EPOCH_KEY = /(_at|^ts|^started|^ended|^created|^time|^last_seen)$/;
  function shift(o) {
    if (Array.isArray(o)) { o.forEach((v, i) => { o[i] = shiftVal(v, ''); }); return o; }
    if (o && typeof o === 'object') { for (const k of Object.keys(o)) o[k] = shiftVal(o[k], k); }
    return o;
  }
  function shiftVal(v, k) {
    if (typeof v === 'string' && ISO.test(v)) return new Date(Date.parse(v) + SHIFT * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z');
    if (typeof v === 'number' && EPOCH_KEY.test(k) && v > 1.6e9 && v < 2.2e9) return v + SHIFT;
    if (v && typeof v === 'object') return shift(v);
    return v;
  }
  if (SHIFT) Object.values(R).forEach((r) => shift(r.body));
  const realFetch = window.fetch.bind(window);
  const now = () => Date.now() / 1000;
  const clone = (o) => (o == null ? o : JSON.parse(JSON.stringify(o)));

  // -- état vivant ------------------------------------------------------------
  // Une VM démarrée ou arrêtée dans la démo garde son nouvel état.
  const vmOverlay = new Map();            // `${cluster}|${ns}|${name}` -> {runStrategy, phase, until, next}
  const actions = new Map();              // id -> action (forme de ActionRun.to_dict)
  let seq = 0;

  function overlayFor(cluster, ns, name) {
    const o = vmOverlay.get(`${cluster}|${ns}|${name}`);
    if (!o) return null;
    if (o.next && now() >= o.until) { o.phase = o.next; o.next = null; }
    return o;
  }
  function applyVm(v, cluster) {
    const o = overlayFor(cluster || v.cluster, v.namespace, v.name);
    if (!o) return v;
    v.runStrategy = o.runStrategy;
    v.phase = o.phase;
    if (o.phase !== 'Running') { v.node = null; v.ips = []; v.agent_connected = 'False'; }
    else if (o.node) { v.node = o.node; v.ips = o.ips || v.ips; v.agent_connected = 'True'; }
    return v;
  }
  function setVm(cluster, ns, name, runStrategy) {
    const key = `${cluster}|${ns}|${name}`;
    const before = vmOverlay.get(key) || {};
    const base = findVm(cluster, ns, name) || {};
    const on = runStrategy !== 'Halted';
    vmOverlay.set(key, {
      runStrategy, phase: on ? 'Starting' : 'Stopped', next: on ? 'Running' : null, until: now() + 6,
      node: before.node || base.node || 'node-1', ips: (before.ips && before.ips.length ? before.ips : base.ips) || [],
    });
  }
  function findVm(cluster, ns, name) {
    const r = R[`/api/vms/${cluster}`];
    return r && r.body && (r.body.vms || []).find((v) => v.namespace === ns && v.name === name);
  }

  // -- actions simulées -------------------------------------------------------
  const STEPS = {
    default: ['Checking the request', 'Applying on the cluster', 'Waiting for the cluster to settle'],
    runStrategy: ['Updating the run strategy', 'Waiting for the VM to change state'],
  };
  function newAction(label, cluster, steps) {
    const id = `demo${Date.now().toString(36)}${(seq++).toString(36)}`;
    const a = {
      id, action: label, cluster: cluster || '', status: 'running', exit_code: null, started_at: now(),
      ended_at: null, dry_run: false, error_summary: null, cluster_user: null, progress: {},
      progress_current: null, result: { demo: true }, steps: steps || STEPS.default,
    };
    actions.set(id, a);
    setTimeout(() => { a.status = 'done'; a.exit_code = 0; a.ended_at = now(); }, 1200 * a.steps.length + 600);
    return a;
  }
  const publicAction = (a) => { const { steps, ...rest } = a; return rest; };

  // Un historique de démonstration : ce qu'une équipe fait en une semaine.
  const SEED = [
    ['vm-start:default/web-01', 'lyon-lab', 'done', 18, 9],
    ['wave-start:wave-erp', 'nantes-dr', 'done', 95, 41],
    ['wave-start:wave-crm', 'nantes-dr', 'done', 92, 38],
    ['vm-snapshot:default/db-01', 'lyon-lab', 'done', 160, 22],
    ['vm-migrate:default/api-01', 'lyon-lab', 'done', 240, 74],
    ['terraform-apply:edge-network', 'lille-edge', 'done', 420, 63],
    ['capi-cluster-create:rke2-apps', 'paris-prod', 'done', 610, 1180],
    ['backup:default/db-01', 'lyon-lab', 'done', 900, 210],
    ['node-maintenance:lyon-n2', 'lyon-lab', 'done', 1300, 380],
    ['upgrade:v1.9.0', 'lyon-lab', 'done', 2900, 3480],
    ['baremetal-batch:bm-staging', 'bm-staging', 'done', 4300, 2640],
    ['shutdown', 'lille-edge', 'done', 5800, 412],
    ['startup', 'lille-edge', 'done', 5600, 655],
    ['vm-import:ova/erp-legacy', 'paris-prod', 'failed', 7000, 96, 'the OVA archive is not reachable from the cluster'],
    ['vm-transfer:default/report-01', 'paris-prod', 'done', 8200, 520],
  ].map(([action, cluster, status, agoMin, dur, err], i) => {
    const started = now() - agoMin * 60;
    return { id: `seed${i}`, action, cluster, status, exit_code: status === 'done' ? 0 : 1, started_at: started,
             ended_at: started + dur, dry_run: false, error_summary: err || null, cluster_user: null,
             progress: {}, progress_current: null, result: {} };
  });

  // -- correspondance d'une requête et d'un enregistrement ----------------------
  function lookup(path, query) {
    const full = path + (query ? `?${query}` : '');
    if (R[full]) return R[full];
    if (R[path]) return R[path];
    // même chemin, autre requête : la plus proche suffit à la démo
    const pref = `${path}?`;
    for (const k of Object.keys(R)) if (k.startsWith(pref)) return R[k];
    return null;
  }

  function json(body, status = 200, headers = {}) {
    return new Response(JSON.stringify(body), {
      status, headers: { 'Content-Type': 'application/json', ...headers },
    });
  }

  function onGet(path, query) {
    let m;
    if (path === '/api/activity') {
      const rec = clone((lookup(path, query) || {}).body) || { in_progress: [], actions_done: [] };
      const mine = [...actions.values()].map(publicAction);
      rec.in_progress = [...mine.filter((a) => a.status === 'running'), ...(rec.in_progress || [])];
      rec.actions_done = [...mine.filter((a) => a.status !== 'running'), ...(rec.actions_done || []), ...SEED]
        .sort((a, b) => b.started_at - a.started_at);
      const f = rec.facets || {};
      const all = [...rec.in_progress, ...rec.actions_done];
      rec.facets = {
        actions: [...new Set([...(f.actions || []), ...all.map((a) => a.action.split(':')[0])])].sort(),
        clusters: [...new Set([...(f.clusters || []), ...all.map((a) => a.cluster).filter(Boolean)])].sort(),
        statuses: [...new Set([...(f.statuses || []), ...all.map((a) => a.status)])].sort(),
      };
      // les filtres de l'onglet, comme la console les applique côté serveur
      const q = new URLSearchParams(query || '');
      const keep = (a) => (!q.get('cluster') || a.cluster === q.get('cluster'))
        && (!q.get('status') || a.status === q.get('status'))
        && (!q.get('action') || a.action.split(':')[0] === q.get('action'))
        && (!q.get('q') || `${a.action} ${a.cluster}`.toLowerCase().includes(q.get('q').toLowerCase()));
      rec.total = all.length;
      rec.actions_done = rec.actions_done.filter(keep);
      rec.matched = rec.actions_done.length + rec.in_progress.length;
      return json(rec);
    }
    if ((m = /^\/api\/action\/([^/]+)$/.exec(path)) && actions.has(m[1])) return json(publicAction(actions.get(m[1])));
    const rec = lookup(path, query);
    let body = rec ? clone(rec.body) : null;
    if ((m = /^\/api\/vms\/([^/]+)$/.exec(path)) && body && body.vms) body.vms.forEach((v) => applyVm(v, m[1]));
    if (path === '/api/vms-all' && body && body.vms) body.vms.forEach((v) => applyVm(v));
    if ((m = /^\/api\/vm\/([^/]+)\/([^/]+)\/([^/]+)\/state$/.exec(path)) && body) {
      const o = overlayFor(m[1], m[2], m[3]);
      if (o) { body.runStrategy = o.runStrategy; body.phase = o.phase; }
    }
    if (body == null) body = path.endsWith('s') ? [] : {};
    return json(body, rec ? rec.status : 200);
  }

  function onWrite(method, path, bodyText) {
    let body = {};
    try { body = bodyText ? JSON.parse(bodyText) : {}; } catch { /* corps non JSON (dépôt de fichier) */ }
    let m;
    if ((m = /^\/api\/vm\/([^/]+)\/([^/]+)\/([^/]+)\/runStrategy$/.exec(path)) && body.runStrategy) {
      setVm(m[1], m[2], m[3], body.runStrategy);
      const a = newAction(`vm:runStrategy:${m[3]}=${body.runStrategy}`, m[1], STEPS.runStrategy);
      return json({ ok: true, action_id: a.id, runStrategy: body.runStrategy });
    }
    if ((m = /^\/api\/vm\/([^/]+)\/([^/]+)\/([^/]+)\/do\/([a-z-]+)$/.exec(path))) {
      const [, c, ns, n, act] = m;
      if (act === 'start') setVm(c, ns, n, 'Always');
      if (act === 'stop' || act === 'force-stop') setVm(c, ns, n, 'Halted');
      if (act === 'restart' || act === 'soft-reboot') {
        const v = findVm(c, ns, n) || {};
        vmOverlay.set(`${c}|${ns}|${n}`, { runStrategy: v.runStrategy || 'Always', phase: 'Starting', next: 'Running',
                                           until: now() + 5, node: v.node, ips: v.ips });
      }
      const a = newAction(`vm:${act}:${n}`, c);
      return json({ ok: true, action_id: a.id }, 202);
    }
    const cluster = (path.split('/')[3] || '');
    const a = newAction(`${method.toLowerCase()} ${path.replace(/^\/api\//, '')}`, cluster);
    return json({ ok: true, action_id: a.id, demo: true }, 202);
  }

  function apiPath(url) {
    const i = url.pathname.indexOf('/api/');
    return i < 0 ? null : url.pathname.slice(i);
  }

  window.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    const path = apiPath(url);
    if (!path) return realFetch(input, init);
    const method = (init.method || (typeof input !== 'string' && input.method) || 'GET').toUpperCase();
    await new Promise((r) => setTimeout(r, 60 + Math.random() * 140));   // un peu de vraie latence
    if (method === 'GET' || method === 'HEAD') return onGet(path, url.search.slice(1));
    const text = typeof init.body === 'string' ? init.body : '';
    return onWrite(method, path, text);
  };

  // Dépôts de fichiers par XMLHttpRequest (archives, images) : acceptés, rien n'est gardé.
  const RealXHR = window.XMLHttpRequest;
  window.XMLHttpRequest = function DemoXHR() {
    const x = new RealXHR();
    let target = null;
    const open = x.open.bind(x);
    x.open = (method, u, ...rest) => {
      const url = new URL(u, location.href);
      target = apiPath(url) ? { method, path: apiPath(url) } : null;
      return target ? undefined : open(method, u, ...rest);
    };
    const send = x.send.bind(x);
    x.send = (b) => {
      if (!target) return send(b);
      const a = newAction(`upload ${target.path.replace(/^\/api\//, '')}`, '');
      const fire = (n) => { const f = x[`on${n}`]; if (f) f.call(x, new Event(n)); x.dispatchEvent(new Event(n)); };
      Object.defineProperty(x, 'readyState', { value: 4 });
      Object.defineProperty(x, 'status', { value: 200 });
      Object.defineProperty(x, 'responseText', { value: JSON.stringify({ ok: true, action_id: a.id, demo: true }) });
      setTimeout(() => { fire('load'); fire('loadend'); fire('readystatechange'); }, 400);
    };
    x.setRequestHeader = target ? () => {} : x.setRequestHeader.bind(x);
    return x;
  };

  // Flux d'une action : étapes rejouées, puis la fin.
  const RealES = window.EventSource;
  window.EventSource = function DemoEventSource(u) {
    const url = new URL(u, location.href);
    const path = apiPath(url);
    if (!path) return new RealES(u);
    const es = { readyState: 1, url: url.href, onopen: null, onerror: null, onmessage: null, _l: {} };
    es.addEventListener = (type, fn) => { (es._l[type] = es._l[type] || []).push(fn); };
    es.removeEventListener = (type, fn) => { es._l[type] = (es._l[type] || []).filter((f) => f !== fn); };
    es.close = () => { es.readyState = 2; };
    const emit = (type, data) => {
      if (es.readyState === 2) return;
      const ev = new MessageEvent(type, { data: JSON.stringify(data) });
      (es._l[type] || []).forEach((f) => f(ev));
      if (type === 'message' && es.onmessage) es.onmessage(ev);
    };
    const id = (/\/api\/stream\/([^/?]+)/.exec(path) || [])[1];
    const a = id && actions.get(id);
    const steps = a ? a.steps : STEPS.default;
    setTimeout(() => { if (es.onopen) es.onopen(new Event('open')); (es._l.open || []).forEach((f) => f(new Event('open'))); }, 50);
    steps.forEach((msg, i) => setTimeout(() => {
      emit('step', { id: `s${i}`, status: 'running', message: msg });
      setTimeout(() => emit('step', { id: `s${i}`, status: 'done', message: msg }), 700);
    }, 1200 * i + 200));
    setTimeout(() => emit('end', { status: 'done', exit_code: 0 }), 1200 * steps.length + 600);
    return es;
  };
  window.EventSource.CONNECTING = 0; window.EventSource.OPEN = 1; window.EventSource.CLOSED = 2;

  // Consoles VNC et série : pas de vraie VM derrière la démo.
  const RealWS = window.WebSocket;
  window.WebSocket = function DemoWebSocket(u, p) {
    if (!/\/api\//.test(String(u)) && !/ticket=/.test(String(u))) return new RealWS(u, p);
    const ws = { readyState: 3, send() {}, close() {}, addEventListener() {}, removeEventListener() {} };
    setTimeout(() => {
      if (ws.onerror) ws.onerror(new Event('error'));
      if (ws.onclose) ws.onclose(new CloseEvent('close', { code: 1000, reason: 'demo' }));
    }, 300);
    return ws;
  };

  window.HarvopsDemo = { actions, vmOverlay };
})();
