/**
 * harvester-ops : images, pools, volumes et réseaux d'un hôte KVM (prochaine
 * version KVM, tâche 6).
 *
 * Fenêtres (FloatingPanels) ouvertes depuis les vues Stockage et Réseaux de
 * kvm.js :
 *   - Téléverser une image (fichier du navigateur, pool, nom proposé d'après le
 *     fichier, SHA-256 facultative, hôtes cibles, plafond de débit) : barre en
 *     deux temps, réception par la console (progression du navigateur) puis
 *     envoi à chaque hôte (progression de l'action), débit, temps restant,
 *     annulation ;
 *   - Télécharger depuis une URL (même envoi, l'URL est contrôlée par la
 *     console : les refus du serveur sont montrés tels quels) ;
 *   - Pools d'images de l'hôte (administrateur) ;
 *   - Créer un pool (dir ou NFS), un volume, redimensionner un volume, créer un
 *     réseau (NAT, routé, isolé, pont) : contrôle du serveur (essai à blanc)
 *     d'abord, refus montrés avec leurs raisons, puis l'action suivie ;
 *   - Supprimer un volume, une image, un pool ou un réseau : aperçu du serveur
 *     (qui s'en sert, de quoi il est la base, fichiers illisibles à accepter un
 *     par un), puis la suppression avec l'empreinte de l'aperçu montré.
 *
 * Toute écriture est une action suivie que le serveur lance par
 * bin/harvester-kvm.py (dock, Activité) ; les règles (rôles, refus) viennent
 * du serveur ; aucun nom n'est inséré sans échappement.
 */
const KVMStorage = (() => {
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const enc = encodeURIComponent;
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 14) => (window.Icons ? Icons.svg(n, { size }) : '');
  const ROLE_RANK = { viewer: 1, operator: 2, admin: 3 };
  // mêmes formes que la bibliothèque (kvm_storage.VOLUME_RE, POOL_NAME_RE,
  // kvm_net.NET_NAME_RE) : le serveur refait le contrôle
  const VOLUME_RE = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,250}$/;
  const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
  const SHA_RE = /^[0-9a-fA-F]{64}$/;
  const STEP_BADGE = { done: 'ok', ok: 'ok', running: 'info', warn: 'warn', error: 'fail', failed: 'fail', skipped: 'dim' };
  const NET_MODES = ['nat', 'route', 'isolated', 'bridge'];
  // aperçu refusé parce que l'hôte relit déjà ses disques (429) : nouvel essai
  const BUSY_RETRIES = 10;
  const BUSY_RETRY_MS = 3000;

  const hostBase = (host) => `/api/kvm/hosts/${enc(host)}`;
  const badge = (cls, text, tip) =>
    `<span class="badge ${cls}${tip ? ' tip' : ''}"${tip ? ` data-tip="${esc(tip)}"` : ''}>${esc(text)}</span>`;
  const dim = (t) => `<span class="res-dim">${esc(t)}</span>`;
  const errHtml = (t) => `<span class="res-error">${icon('fail')} ${esc(t)}</span>`;
  const okHtml = (t) => `<span>${icon('ok')} ${esc(t)}</span>`;
  const li = (t) => `<li>${esc(t)}</li>`;
  const listHtml = (items) => (items && items.length ? `<ul class="kvm-op-list">${items.map(li).join('')}</ul>` : '');
  const opt = (value, label, selected) => `<option value="${esc(value)}"${selected ? ' selected' : ''}>${esc(label)}</option>`;
  const field = (label, input, tip) => `<label class="bk-field"><span>${esc(label)}</span>${
    input.replace(/^<(input|select|textarea)/, `<$1 class="tip" data-tip="${esc(tip)}"`)}</label>`;
  const atLeast = (role, need) => (ROLE_RANK[role] || 0) >= ROLE_RANK[need];

  function bytes(n) {
    if (typeof n !== 'number' || !Number.isFinite(n)) return '–';
    const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
    let i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i += 1; }
    return `${v.toFixed(i ? 1 : 0)} ${u[i]}`;
  }
  const parseSize = (t) => (window.KVMForm && KVMForm.parseSize ? KVMForm.parseSize(t) : null);

  async function getJson(url) {
    try {
      const r = await fetch(url);
      const d = await r.json().catch(() => null);
      if (!d) return { error: `HTTP ${r.status}`, status: r.status };
      if (!r.ok) return Object.assign({ status: r.status }, d, { error: d.error || `HTTP ${r.status}` });
      return d;
    } catch (e) { return { error: e.message }; }
  }

  async function send(method, url, body) {
    const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    let d = {};
    try { d = await r.json(); } catch { /* sans corps */ }
    if (!r.ok) {
      const err = new Error(d.hint || d.error || `HTTP ${r.status}`);
      err.data = d;
      err.status = r.status;
      throw err;
    }
    return d;
  }

  /** Suit une action, progression comprise : `on.step(s)`, `on.progress(snap)`,
   *  `on.end(ok, run)` (l'action entière, son `result.output` compris). */
  function track(actionId, on) {
    if (window.Dock && Dock.poll) Dock.poll();
    if (!window.SSEReconnect) { on.end(false, { error_summary: 'no SSE' }); return null; }
    const es = SSEReconnect.connect(`/api/stream/${enc(actionId)}`, {
      on: {
        step: (e) => { try { if (on.step) on.step(JSON.parse(e.data)); } catch { /* ligne illisible */ } },
        progress: (e) => { try { if (on.progress) on.progress(JSON.parse(e.data)); } catch { /* point illisible */ } },
        end: (e) => {
          let d = {};
          try { d = JSON.parse(e.data); } catch { /* fin sans détail */ }
          es.close();
          on.end(d.status === 'done', d);
        },
      },
    });
    return es;
  }

  function stepLine(list, s) {
    let row = [...list.children].find((x) => x.dataset.step === (s.step_id || ''));
    if (!row) { row = document.createElement('li'); row.dataset.step = s.step_id || ''; list.appendChild(row); }
    const st = s.status || 'running';
    row.className = `kvm-step is-${st}`;
    row.innerHTML = `${badge(STEP_BADGE[st] || 'info', s.step_id || st)} ${esc(s.message || '')}`;
  }

  const refreshList = () => (window.KVM && KVM.load ? KVM.load(true) : Promise.resolve());
  const failText = (run, o) => (o && o.error) || run.error_summary || run.status || '?';

  function panel(id, title, ic, bodyHtml, size) {
    if (!window.FloatingPanels) return null;
    const p = FloatingPanels.open({ id, icon: ic, title, width: size[0], height: size[1], bodyHtml });
    return p && p.el;
  }

  // -- noms et URL ---------------------------------------------------------------------
  /** Nom de volume proposé d'après un nom de fichier ou une URL : dernier
   *  segment du chemin (sans requête), caractères hors de la forme admise
   *  remplacés par « - », jamais « .. » ni un premier caractère non
   *  alphanumérique ; '' si rien ne reste. */
  function suggestName(text) {
    let s = String(text || '');
    try { if (/^[a-z][a-z0-9+.-]*:\/\//i.test(s)) s = new URL(s).pathname; } catch { /* texte brut */ }
    s = s.split(/[?#]/)[0].split(/[\\/]/).pop() || '';
    try { s = decodeURIComponent(s); } catch { /* garder tel quel */ }
    s = s.replace(/[^A-Za-z0-9._+-]+/g, '-').replace(/\.{2,}/g, '.').replace(/^[^A-Za-z0-9]+/, '');
    s = s.slice(0, 251);
    return VOLUME_RE.test(s) ? s : '';
  }

  /** URL montrable : sans identifiants, requête ni fragment (une URL signée
   *  porte son jeton dans la requête). '' si ce n'est pas une URL http(s). */
  function publicUrl(u) {
    try {
      const x = new URL(String(u));
      if (!/^https?:$/.test(x.protocol)) return '';
      return `${x.protocol}//${x.host}${x.pathname}`;
    } catch { return ''; }
  }

  // -- sous-réseaux --------------------------------------------------------------------
  function ipNum(a) {
    const p = String(a).split('.').map(Number);
    if (p.length !== 4 || p.some((x) => !Number.isInteger(x) || x < 0 || x > 255)) return null;
    return ((p[0] << 24) >>> 0) + (p[1] << 16) + (p[2] << 8) + p[3];
  }
  function cidr(text) {
    const [a, l] = String(text || '').split('/');
    const n = ipNum(a), len = Number(l);
    if (n == null || !Number.isInteger(len) || len < 0 || len > 32) return null;
    const mask = len ? (~0 << (32 - len)) >>> 0 : 0;
    const start = (n & mask) >>> 0;
    return { start, end: (start + (2 ** (32 - len)) - 1) >>> 0, len };
  }
  const overlaps = (a, b) => !!(a && b && a.start <= b.end && b.start <= a.end);
  const ipText = (n) => [n >>> 24, (n >>> 16) & 255, (n >>> 8) & 255, n & 255].join('.');

  /** Premier 192.168.X.0/24 (X de 100 à 254) qui ne chevauche ni une adresse
   *  ou une route de l'hôte (`/links`), ni un réseau libvirt : une
   *  proposition, le serveur refait le contrôle. */
  function freeSubnet(links, networks) {
    const taken = [];
    ((links && links.networks) || []).forEach((x) => { const c = cidr(x.network); if (c && x.network.indexOf('127.') !== 0) taken.push(c); });
    (networks || []).forEach((n) => (n.ips || []).forEach((ip) => {
      if (ip.address && ip.prefix != null) taken.push(cidr(`${ip.address}/${ip.prefix}`));
    }));
    for (let x = 100; x < 255; x += 1) {
      const c = cidr(`192.168.${x}.0/24`);
      if (!taken.some((t) => overlaps(c, t))) return `192.168.${x}.0/24`;
    }
    return '';
  }

  /** Plage DHCP proposée : de .2 à la dernière adresse d'hôte (kvm_net.default_dhcp). */
  function dhcpRange(subnet) {
    const c = cidr(subnet);
    if (!c || c.len > 30) return ['', ''];
    return [ipText(c.start + 2), ipText(c.end - 1)];
  }

  // -- contrôle à blanc puis action ------------------------------------------------------
  /** Fenêtre de création (pool, réseau) : essai à blanc du serveur, refus
   *  montrés avec leurs raisons ; s'il passe, la création suivie. */
  async function previewThenCreate(ctx) {
    const { root, url, spec, say, steps, go, done } = ctx;
    go.disabled = true;
    steps.innerHTML = '';
    say(esc(tr('kvms.checking')));
    let pre;
    try {
      pre = await send('POST', url, { spec, dry_run: true });
    } catch (err) {
      say(errHtml(err.message));
      go.disabled = false;
      return;
    }
    if (!root.isConnected) return;
    if (pre.possible === false) {
      say(`<div class="kvm-op-refusals" data-ks="refusals">${icon('fail')} ${esc(tr('kvms.refused'))}${listHtml(pre.refusals || [pre.error])}</div>`);
      go.disabled = false;
      return;
    }
    try {
      const out = await send('POST', url, { spec });
      say(esc(tr('bk.started', { id: out.action_id })));
      track(out.action_id, {
        step: (s) => stepLine(steps, s),
        end: (ok, run) => {
          const o = (run.result && run.result.output) || {};
          if (ok) {
            say(okHtml(done(o)));
            refreshList();
          } else {
            say(`${errHtml(failText(run, o))}${listHtml(o.refusals)}`);
            go.disabled = false;
          }
        },
      });
    } catch (err) {
      say(`${errHtml(err.message)}${listHtml(err.data && err.data.refusals)}`);
      go.disabled = false;
    }
  }

  // -- Créer un pool -------------------------------------------------------------------
  function openPoolCreate(host) {
    const root = panel(`kvm-pool-create-${enc(host)}`, tr('kvms.pool.createTitle', { host }), 'storage', `
      <form class="of-form kvm-vmw kvms-pool-create" autocomplete="off" novalidate>
        <p class="form-hint">${esc(tr('kvms.pool.hint'))}</p>
        <div class="ct-create-row">
          ${field(tr('kvms.pool.name'), '<input name="name" maxlength="64" required>', tr('kvms.pool.nameTip'))}
          ${field(tr('kvms.pool.type'), `<select name="type">${opt('dir', tr('kvms.pool.dir'), true)}${opt('netfs', tr('kvms.pool.netfs'))}</select>`, tr('kvms.pool.typeTip'))}
        </div>
        ${field(tr('kvms.pool.path'), '<input name="path" maxlength="4096" required placeholder="/var/lib/libvirt/images/pool">', tr('kvms.pool.pathTip'))}
        <div class="ct-create-row" data-ks="nfs" hidden>
          ${field(tr('kvms.pool.server'), '<input name="source_host" maxlength="253">', tr('kvms.pool.serverTip'))}
          ${field(tr('kvms.pool.export'), '<input name="source_dir" maxlength="4096" placeholder="/export/vms">', tr('kvms.pool.exportTip'))}
        </div>
        <label class="kvm-check tip" data-tip="${esc(tr('kvms.pool.autostartTip'))}"><input type="checkbox" name="autostart" checked> ${esc(tr('kvms.autostart'))}</label>
        <div class="bk-form-actions"><button type="submit" class="btn btn-sm btn-primary tip" data-ks="go"
          data-tip="${esc(tr('kvms.pool.goTip'))}">${icon('add')} ${esc(tr('kvms.pool.go'))}</button></div>
        <div class="of-msg" data-ks="msg" role="status" aria-live="polite"></div>
        <ul class="kvm-steps" data-ks="steps"></ul>
      </form>`, [600, 600]);
    if (!root || root.dataset.ksReady) return root;
    root.dataset.ksReady = '1';
    const f = root.querySelector('form');
    const $ = (k) => root.querySelector(`[data-ks="${k}"]`);
    const say = (html) => { $('msg').innerHTML = html; };
    let pathTouched = false;
    f.elements.path.addEventListener('input', () => { pathTouched = true; });
    f.elements.name.addEventListener('input', () => {
      if (!pathTouched) f.elements.path.value = NAME_RE.test(f.elements.name.value) ? `/var/lib/libvirt/images/${f.elements.name.value}` : '';
    });
    f.elements.type.addEventListener('change', () => { $('nfs').hidden = f.elements.type.value !== 'netfs'; });
    f.addEventListener('submit', (e) => {
      e.preventDefault();
      const name = f.elements.name.value.trim();
      if (!NAME_RE.test(name)) { say(errHtml(tr('kvms.pool.badName'))); return; }
      const spec = { name, type: f.elements.type.value, path: f.elements.path.value.trim(), autostart: f.elements.autostart.checked };
      if (!spec.path.startsWith('/')) { say(errHtml(tr('kvms.pool.badPath'))); return; }
      if (spec.type === 'netfs') {
        spec.source_host = f.elements.source_host.value.trim();
        spec.source_dir = f.elements.source_dir.value.trim();
        if (!spec.source_host || !spec.source_dir.startsWith('/')) { say(errHtml(tr('kvms.pool.badNfs'))); return; }
      }
      previewThenCreate({ root, url: `${hostBase(host)}/pools`, spec, say, steps: $('steps'), go: $('go'),
                          done: () => tr('kvms.pool.created', { name, host }) });
    });
    return root;
  }

  // -- Créer un volume -----------------------------------------------------------------
  function openVolumeCreate(host, ctx = {}) {
    const pools = (ctx.pools || []).filter((p) => p.state === 'running');
    const root = panel(`kvm-vol-create-${enc(host)}`, tr('kvms.vol.createTitle', { host }), 'storage', `
      <form class="of-form kvm-vmw kvms-vol-create" autocomplete="off" novalidate>
        <div class="ct-create-row">
          ${field(tr('kvms.pool.pool'), `<select name="pool">${pools.map((p) => opt(p.name, p.name, p.name === ctx.pool)).join('')}</select>`, tr('kvms.vol.poolTip'))}
          ${field(tr('kvms.vol.name'), '<input name="name" maxlength="251" required>', tr('kvms.vol.nameTip'))}
        </div>
        <div class="ct-create-row">
          ${field(tr('kvms.vol.size'), '<input name="size" maxlength="20" value="10G" required>', tr('kvms.vol.sizeTip'))}
          ${field(tr('kvms.vol.format'), `<select name="format">${opt('qcow2', 'qcow2', true)}${opt('raw', 'raw')}</select>`, tr('kvms.vol.formatTip'))}
        </div>
        ${field(tr('kvms.vol.backing'), '<select name="backing"></select>', tr('kvms.vol.backingTip'))}
        <div class="bk-form-actions"><button type="submit" class="btn btn-sm btn-primary tip" data-ks="go"
          data-tip="${esc(tr('kvms.vol.goTip'))}">${icon('add')} ${esc(tr('kvms.vol.go'))}</button></div>
        <div class="of-msg" data-ks="msg" role="status" aria-live="polite"></div>
        <ul class="kvm-steps" data-ks="steps"></ul>
      </form>`, [580, 520]);
    if (!root || root.dataset.ksReady) return root;
    root.dataset.ksReady = '1';
    const f = root.querySelector('form');
    const $ = (k) => root.querySelector(`[data-ks="${k}"]`);
    const say = (html) => { $('msg').innerHTML = html; };
    // image de base : un volume qcow2 ou raw du même pool (lié, comme un
    // disque de VM créé depuis une image)
    const backings = () => {
      const p = pools.find((x) => x.name === f.elements.pool.value);
      const vols = ((p && p.volumes) || []).filter((v) => v.type !== 'dir');
      f.elements.backing.innerHTML = opt('', tr('kvms.vol.noBacking'), true) + vols.map((v) => opt(v.name, v.name)).join('');
      f.elements.backing.disabled = f.elements.format.value !== 'qcow2';
    };
    f.elements.pool.addEventListener('change', backings);
    f.elements.format.addEventListener('change', backings);
    backings();
    f.addEventListener('submit', async (e) => {
      e.preventDefault();
      const name = f.elements.name.value.trim();
      const capacity = parseSize(f.elements.size.value);
      if (!f.elements.pool.value) { say(errHtml(tr('kvms.vol.noPool'))); return; }
      if (!VOLUME_RE.test(name) || name.includes('..')) { say(errHtml(tr('kvms.vol.badName'))); return; }
      if (!capacity) { say(errHtml(tr('kvms.vol.badSize'))); return; }
      const body = { pool: f.elements.pool.value, name, capacity, format: f.elements.format.value };
      if (!f.elements.backing.disabled && f.elements.backing.value) body.backing = f.elements.backing.value;
      $('go').disabled = true;
      $('steps').innerHTML = '';
      try {
        const out = await send('POST', `${hostBase(host)}/volumes`, body);
        say(esc(tr('bk.started', { id: out.action_id })));
        track(out.action_id, {
          step: (s) => stepLine($('steps'), s),
          end: (ok, run) => {
            const o = (run.result && run.result.output) || {};
            if (ok) { say(okHtml(tr('kvms.vol.created', { name: `${body.pool}/${name}` }))); refreshList(); } else { say(`${errHtml(failText(run, o))}${listHtml(o.refusals)}`); $('go').disabled = false; }
          },
        });
      } catch (err) { say(errHtml(err.message)); $('go').disabled = false; }
    });
    return root;
  }

  // -- Redimensionner un volume ----------------------------------------------------------
  function openVolumeResize(host, pool, vol) {
    const root = panel(`kvm-vol-resize-${enc(host)}-${enc(pool)}-${enc(vol.name)}`, tr('kvms.resize.title', { name: vol.name }), 'storage', `
      <form class="of-form kvm-vmw kvms-vol-resize" autocomplete="off" novalidate>
        <p class="form-hint">${esc(tr('kvms.resize.hint'))}</p>
        <p>${esc(tr('kvms.resize.current', { pool, size: bytes(vol.capacity) }))}</p>
        ${field(tr('kvms.resize.size'), '<input name="size" maxlength="20" required>', tr('kvms.resize.sizeTip'))}
        <div data-ks="ack"></div>
        <div class="bk-form-actions"><button type="submit" class="btn btn-sm btn-primary tip" data-ks="go"
          data-tip="${esc(tr('kvms.resize.goTip'))}">${icon('edit')} ${esc(tr('kvms.resize.go'))}</button></div>
        <div class="of-msg" data-ks="msg" role="status" aria-live="polite"></div>
        <ul class="kvm-steps" data-ks="steps"></ul>
      </form>`, [560, 460]);
    if (!root || root.dataset.ksReady) return root;
    root.dataset.ksReady = '1';
    const f = root.querySelector('form');
    const $ = (k) => root.querySelector(`[data-ks="${k}"]`);
    const say = (html) => { $('msg').innerHTML = html; };
    const acks = () => [...root.querySelectorAll('[data-ks-ack]:checked')].map((c) => c.dataset.ksAck);
    f.addEventListener('submit', async (e) => {
      e.preventDefault();
      const capacity = parseSize(f.elements.size.value);
      if (!capacity || (typeof vol.capacity === 'number' && capacity <= vol.capacity)) { say(errHtml(tr('kvms.resize.bigger', { size: bytes(vol.capacity) }))); return; }
      $('go').disabled = true;
      $('steps').innerHTML = '';
      try {
        const out = await send('POST', `${hostBase(host)}/volumes/${enc(pool)}/${enc(vol.name)}/resize`,
          { capacity, acknowledge_unreadable: acks() });
        say(esc(tr('bk.started', { id: out.action_id })));
        track(out.action_id, {
          step: (s) => stepLine($('steps'), s),
          end: (ok, run) => {
            const o = (run.result && run.result.output) || {};
            $('go').disabled = false;
            if (ok) { say(okHtml(tr('kvms.resize.done', { name: vol.name, size: bytes(capacity) }))); refreshList(); return; }
            say(`${errHtml(failText(run, o))}${listHtml(o.refusals)}`);
            // fichiers illisibles (refus de droits) : à accepter un par un, puis relancer
            const unread = o.unreadable || [];
            $('ack').innerHTML = unread.length ? `<div class="kvm-op-warn">${icon('warn')} ${esc(tr('kvms.del.unreadable'))}<ul class="kvm-op-list">${unread.map((p) => `<li><label class="kvm-check"><input type="checkbox" data-ks-ack="${esc(p)}" class="tip"
              data-tip="${esc(tr('kvm.del.ackTip'))}"> <span class="kvm-path">${esc(p)}</span></label></li>`).join('')}</ul></div>` : '';
          },
        });
      } catch (err) { say(errHtml(err.message)); $('go').disabled = false; }
    });
    return root;
  }

  // -- Supprimer (volume, image, pool, réseau) ------------------------------------------
  // clés en toutes lettres : le contrôle des traductions ne lit que des littéraux
  const DELETE_KIND = {
    volume: { title: (r) => tr('kvms.del.volumeTitle', { name: `${r.pool}/${r.name}` }), hint: () => tr('kvms.del.hint.volume'), typed: false },
    image: { title: (r) => tr('kvms.del.imageTitle', { name: `${r.pool}/${r.name}` }), hint: () => tr('kvms.del.hint.image'), typed: false },
    pool: { title: (r) => tr('kvms.del.poolTitle', { name: r.name }), hint: () => tr('kvms.del.hint.pool'), typed: true },
    network: { title: (r) => tr('kvms.del.networkTitle', { name: r.name }), hint: () => tr('kvms.del.hint.network'), typed: true },
  };
  const NET_MODE_LABEL = {
    nat: () => tr('kvms.net.mode.nat'), route: () => tr('kvms.net.mode.route'),
    isolated: () => tr('kvms.net.mode.isolated'), bridge: () => tr('kvms.net.mode.bridge'),
  };

  function deleteUrls(kind, host, r) {
    const b = hostBase(host);
    if (kind === 'volume' || kind === 'image') {
      return { plan: `${b}/volumes/${enc(r.pool)}/${enc(r.name)}/delete-plan`,
               del: kind === 'image' ? `${b}/images/${enc(r.pool)}/${enc(r.name)}` : `${b}/volumes/${enc(r.pool)}/${enc(r.name)}` };
    }
    if (kind === 'pool') return { plan: `${b}/pools/${enc(r.name)}/delete-plan`, del: `${b}/pools/${enc(r.name)}` };
    return { plan: `${b}/networks/${enc(r.name)}/delete-plan`, del: `${b}/networks/${enc(r.name)}` };
  }

  /** `kind` : volume, image, pool ou network ; `r` : {name, pool?}. */
  function openDelete(kind, host, r) {
    const K = DELETE_KIND[kind];
    if (!K) return null;
    const id = `kvm-del-${kind}-${enc(host)}-${enc(r.pool || '')}-${enc(r.name)}`;
    const root = panel(id, K.title(r), 'trash', `
      <form class="of-form kvm-vmw kvms-delete" data-ks-kind="${esc(kind)}" autocomplete="off" novalidate>
        <p class="form-hint">${esc(K.hint())}</p>
        ${kind === 'pool' ? `<label class="kvm-check"><input type="checkbox" name="remove_dir" class="tip" data-tip="${esc(tr('kvms.del.removeDirTip'))}"> ${esc(tr('kvms.del.removeDir'))}</label>` : ''}
        <div data-ks="plan"><p class="form-hint">${esc(tr('common.loading'))}</p></div>
        ${K.typed ? `<label class="bk-field"><span>${esc(tr('kvm.del.type', { name: r.name }))}</span><input name="confirm" maxlength="255" class="tip" data-tip="${esc(tr('kvm.del.typeTip'))}"></label>` : ''}
        <div class="bk-form-actions"><button type="submit" class="btn btn-sm btn-danger tip" data-ks="go" disabled
          data-tip="${esc(tr('kvms.del.goTip'))}">${icon('trash')} ${esc(tr('kvms.del.go'))}</button></div>
        <div class="of-msg" data-ks="msg" role="status" aria-live="polite"></div>
        <ul class="kvm-steps" data-ks="steps"></ul>
      </form>`, [680, 600]);
    if (!root || root.dataset.ksReady) return root;
    root.dataset.ksReady = '1';
    const f = root.querySelector('form');
    const $ = (k) => root.querySelector(`[data-ks="${k}"]`);
    const say = (html) => { $('msg').innerHTML = html; };
    const urls = deleteUrls(kind, host, r);
    // `ack` : fichiers illisibles cochés, envoyés tels quels à l'aperçu ET à la
    // suppression (la liste de l'aperçu montré)
    const st = { plan: null, ack: [], loading: false, busy: false, done: false, seq: 0, retries: 0 };

    function controls() {
      const go = $('go');
      const p = st.plan;
      let why = '';
      if (st.loading || !p) why = tr('kvm.del.needPreview');
      else if (p.error) why = p.error;
      else if ((p.role || 'viewer') !== 'admin') why = tr('kvm.adminOnly');
      else if (!p.possible) why = tr('kvm.del.refusedTip');
      else if (K.typed && f.elements.confirm.value !== r.name) why = tr('kvm.del.typeTip');
      go.disabled = st.busy || st.done || !!why;
      go.dataset.tip = why || tr('kvms.del.goTip');
      if (f.elements.remove_dir) f.elements.remove_dir.disabled = st.busy || st.done;
      root.querySelectorAll('[data-ks-ack]').forEach((c) => { c.disabled = st.busy || st.done || st.loading; });
    }

    function planHtml(d) {
      if (d.error) return `<p>${errHtml(d.error)}</p>`;
      const p = d.plan || {};
      const acked = new Set(st.ack);
      const unread = (p.unreadable || []).map((x) => `<li><label class="kvm-check"><input type="checkbox" data-ks-ack="${esc(x)}"${acked.has(x) ? ' checked' : ''} class="tip"
          data-tip="${esc(tr('kvm.del.ackTip'))}"> <span class="kvm-path">${esc(x)}</span></label></li>`).join('');
      const facts = [];
      if (p.path) facts.push(tr('kvms.del.path', { path: p.path }));
      if (kind === 'pool') {
        const s = p.state || {};
        facts.push(tr('kvms.del.poolLine', { type: s.type || '?', path: s.target_path || '?', state: s.state || '?' }));
        if (Array.isArray(p.volumes)) facts.push(tr('kvms.del.poolVolumes', { n: p.volumes.length }));
        facts.push(p.remove_dir ? tr('kvms.del.dirRemoved') : tr('kvms.del.dirKept'));
      }
      if (kind === 'network') {
        const n = p.network || {};
        facts.push(tr('kvms.del.netLine', { mode: n.forward || 'isolated', bridge: n.bridge || '–' }));
      }
      return `<div class="kvm-op-plan">
        ${listHtml(facts)}
        ${(p.refusals || []).length ? `<div class="kvm-op-refusals" data-ks="refusals">${icon('fail')} ${esc(tr('kvms.del.refused'))}${listHtml(p.refusals)}</div>`
    : `<p data-ks="free">${okHtml(tr('kvms.del.free'))}</p>`}
        ${unread ? `<div class="kvm-op-warn" data-ks="unreadable">${icon('warn')} ${esc(tr('kvms.del.unreadable'))}<ul class="kvm-op-list">${unread}</ul></div>` : ''}
      </div>`;
    }

    async function preview() {
      const seq = ++st.seq;
      st.loading = true;
      controls();
      const q = new URLSearchParams();
      if (f.elements.remove_dir && f.elements.remove_dir.checked) q.set('remove_dir', '1');
      st.ack.forEach((x) => q.append('acknowledge', x));
      q.set('fresh', '1');
      const d = await getJson(`${urls.plan}?${q.toString()}`);
      if (!root.isConnected || seq !== st.seq) return;
      // relevés de l'hôte déjà en cours (429, vu en réel sur node2) : nouvel
      // essai tout seul, quelques fois, le message reste affiché
      if (d.status === 429 && st.retries < BUSY_RETRIES) {
        st.retries += 1;
        $('plan').innerHTML = `<p class="form-hint">${esc(tr('kvms.del.busyRetry', { msg: d.error }))}</p>`;
        setTimeout(() => { if (root.isConnected && seq === st.seq) preview(); }, BUSY_RETRY_MS);
        return;
      }
      st.retries = 0;
      st.loading = false;
      st.plan = d.error ? { error: d.error } : Object.assign({}, d, { ack: [...st.ack] });
      $('plan').innerHTML = planHtml(d);
      controls();
    }

    if (f.elements.remove_dir) f.elements.remove_dir.addEventListener('change', () => preview());
    if (f.elements.confirm) f.elements.confirm.addEventListener('input', controls);
    root.addEventListener('change', (e) => {
      const c = e.target.closest('[data-ks-ack]');
      if (!c) return;
      st.ack = st.ack.filter((x) => x !== c.dataset.ksAck);
      if (c.checked) st.ack.push(c.dataset.ksAck);
      preview();
    });
    f.addEventListener('submit', async (e) => {
      e.preventDefault();
      controls();
      if ($('go').disabled) return;
      const p = st.plan;
      const body = { expect_delete: p.plan.digest };
      if (kind !== 'network') body.acknowledge_unreadable = p.ack;
      if (kind === 'pool') body.remove_dir = !!p.plan.remove_dir;
      st.busy = true;
      controls();
      $('steps').innerHTML = '';
      say(esc(tr('kvms.del.sending', { name: r.name })));
      try {
        const out = await send('DELETE', urls.del, body);
        say(esc(tr('bk.started', { id: out.action_id })));
        track(out.action_id, {
          step: (s) => stepLine($('steps'), s),
          end: (ok, run) => {
            const o = (run.result && run.result.output) || {};
            st.busy = false;
            if (ok) {
              st.done = true;
              say(okHtml(tr('kvms.del.done', { name: r.name, host })));
            } else if (o.kind === 'plan-changed') {
              say(errHtml(tr('kvm.del.planChanged')));
              if (f.elements.confirm) f.elements.confirm.value = '';
              Promise.resolve(preview()).finally(() => { controls(); refreshList(); });
              return;
            } else {
              say(`${errHtml(failText(run, o))}${listHtml(o.refusals)}`);
            }
            controls();
            refreshList();
          },
        });
      } catch (err) {
        st.busy = false;
        say(errHtml(err.message));
        controls();
      }
    });
    controls();
    preview();
    return root;
  }

  // -- Téléverser, télécharger : la barre en deux temps ----------------------------------
  const progressHtml = () => `<div class="kvms-progress" data-ks="progress" hidden>
      <div class="kvms-phase" data-ks-phase="first"><div class="kvms-phase-head"><span data-ks="first-label"></span></div>
        <div class="progress-mini xfer-live-bar"><div class="fill" data-ks="first-bar" style="width:0%"></div></div>
        <div class="xfer-live-line" data-ks="first-line"></div></div>
      <div class="kvms-phase" data-ks-phase="send"><div class="kvms-phase-head"><span>${esc(tr('kvms.up.phaseSend'))}</span></div>
        <div class="progress-mini xfer-live-bar"><div class="fill" data-ks="send-bar" style="width:0%"></div></div>
        <div class="xfer-live-line" data-ks="send-line"></div></div>
    </div>`;

  /** Champs communs du téléversement et du téléchargement (pool, nom, SHA-256,
   *  hôtes, débit, contrôle). `ctx.hosts` : hôtes joignables ; `ctx.pools` :
   *  pools actifs de l'hôte ; `ctx.imagePools` : pools d'images déclarés. */
  function sendFieldsHtml(host, ctx) {
    const pools = ctx.pools || [];
    const first = (ctx.imagePools || []).find((p) => pools.includes(p)) || pools[0] || '';
    const others = (ctx.hosts || []).filter((h) => h !== host);
    return `<div class="ct-create-row">
        ${field(tr('kvms.pool.pool'), `<select name="pool">${pools.map((p) => opt(p, p, p === first)).join('')}</select>`, tr('kvms.up.poolTip'))}
        ${field(tr('kvms.vol.name'), '<input name="name" maxlength="251" required>', tr('kvms.up.nameTip'))}
      </div>
      ${field(tr('kvms.up.sha'), '<input name="sha256" maxlength="64" spellcheck="false" placeholder="sha256">', tr('kvms.up.shaTip'))}
      <fieldset class="kvms-hosts"><legend class="tip" data-tip="${esc(tr('kvms.up.hostsTip'))}">${esc(tr('kvms.up.hosts'))}</legend>
        <label class="kvm-check tip" data-tip="${esc(tr('kvms.up.mainHostTip'))}"><input type="checkbox" checked disabled> ${esc(host)}</label>
        ${others.map((h) => `<label class="kvm-check tip" data-tip="${esc(tr('kvms.up.hostTip'))}"><input type="checkbox" data-ks-host="${esc(h)}"> ${esc(h)}</label>`).join('')}
      </fieldset>
      <div class="ct-create-row">
        ${field(tr('kvms.up.limit'), '<input name="limit_mbps" type="number" min="1" step="1" placeholder="Mbit/s">', tr('kvms.up.limitTip'))}
        <label class="kvm-check tip" data-tip="${esc(tr('kvms.up.verifyTip'))}"><input type="checkbox" name="verify" checked> ${esc(tr('kvms.up.verify'))}</label>
      </div>`;
  }

  function readSendFields(root, f) {
    const hosts = [...root.querySelectorAll('[data-ks-host]:checked')].map((c) => c.dataset.ksHost);
    return {
      pool: f.elements.pool.value, name: f.elements.name.value.trim(), sha256: f.elements.sha256.value.trim().toLowerCase(),
      hosts, limit_mbps: f.elements.limit_mbps.value ? Number(f.elements.limit_mbps.value) : null, verify: f.elements.verify.checked,
    };
  }

  function checkSendFields(v) {
    if (!v.pool) return tr('kvms.vol.noPool');
    if (!VOLUME_RE.test(v.name) || v.name.includes('..')) return tr('kvms.vol.badName');
    if (v.sha256 && !SHA_RE.test(v.sha256)) return tr('kvms.up.badSha');
    if (v.limit_mbps != null && !(v.limit_mbps > 0)) return tr('kvms.up.badLimit');
    return '';
  }

  /** Branche la barre d'envoi sur l'action : progression de chaque hôte
   *  (phases upload et verify du script), étapes, fin. */
  function followSend(st, $, actionId, host, onEnd) {
    st.action = actionId;
    st.phase = 'send';
    const steps = $('steps');
    track(actionId, {
      step: (s) => stepLine(steps, s),
      progress: (snap) => {
        if (!window.XferProgress) return;
        if (snap.phase === 'download' || snap.phase === 'receive') {
          $('first-bar').style.width = `${XferProgress.pct(snap)}%`;
          $('first-line').textContent = XferProgress.text(snap);
          return;
        }
        $('send-bar').style.width = `${XferProgress.pct(snap)}%`;
        $('send-line').textContent = XferProgress.text(snap);
      },
      end: (ok, run) => {
        st.phase = 'done';
        st.action = null;
        onEnd(ok, run, (run.result && run.result.output) || {});
      },
    });
  }

  function sendResultHtml(ok, run, o, host) {
    if (ok) {
      const hosts = (o.hosts || []).map((h) => h.host).filter(Boolean);
      return okHtml(tr('kvms.up.done', { name: `${o.pool || '?'}/${o.volume || '?'}`, hosts: (hosts.length ? hosts : [host]).join(', ') }))
        + (o.sha256 ? `<p class="kvm-path">SHA-256 ${esc(o.sha256)}</p>` : '');
    }
    if (run.status === 'cancelled') return errHtml(tr('kvms.up.cancelledSend'));
    const per = (o.hosts || []).filter((h) => h && h.ok === false).map((h) => `${h.host}: ${h.error || '?'}`);
    return `${errHtml(failText(run, o))}${listHtml(o.refusals)}${listHtml(per)}`;
  }

  async function cancelAction(st, say) {
    if (!st.action) return;
    try {
      const r = await fetch(`/api/action/${enc(st.action)}`, { method: 'DELETE' });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        say(errHtml(d.error || `HTTP ${r.status}`));
      }
    } catch (e) { say(errHtml(e.message)); }
  }

  // -- Téléverser une image -------------------------------------------------------------
  function openUpload(host, ctx = {}) {
    const root = panel(`kvm-upload-${enc(host)}`, tr('kvms.up.title', { host }), 'upload', `
      <form class="of-form kvm-vmw kvms-upload" autocomplete="off" novalidate>
        <p class="form-hint">${esc(tr('kvms.up.hint'))}</p>
        ${field(tr('kvms.up.file'), '<input type="file" name="file" required>', tr('kvms.up.fileTip'))}
        ${sendFieldsHtml(host, ctx)}
        <div class="bk-form-actions">
          <button type="submit" class="btn btn-sm btn-primary tip" data-ks="go" data-tip="${esc(tr('kvms.up.goTip'))}">${icon('upload')} ${esc(tr('kvms.up.go'))}</button>
          <button type="button" class="btn btn-sm btn-secondary tip" data-ks="cancel" hidden data-tip="${esc(tr('kvms.up.cancelTip'))}">${icon('stop')} ${esc(tr('kvms.up.cancel'))}</button>
        </div>
        ${progressHtml()}
        <div class="of-msg" data-ks="msg" role="status" aria-live="polite"></div>
        <ul class="kvm-steps" data-ks="steps"></ul>
      </form>`, [640, 720]);
    if (!root || root.dataset.ksReady) return root;
    root.dataset.ksReady = '1';
    const f = root.querySelector('form');
    const $ = (k) => root.querySelector(`[data-ks="${k}"]`);
    const say = (html) => { $('msg').innerHTML = html; };
    $('first-label').textContent = tr('kvms.up.phaseReceive');
    const st = { xhr: null, action: null, phase: 'idle' };
    let nameTouched = false;
    f.elements.name.addEventListener('input', () => { nameTouched = true; });
    f.elements.file.addEventListener('change', () => {
      const file = f.elements.file.files[0];
      if (file && !nameTouched) f.elements.name.value = suggestName(file.name);
    });
    const idle = () => { $('go').disabled = false; $('cancel').hidden = true; st.xhr = null; };

    $('cancel').addEventListener('click', () => {
      if (st.phase === 'receive' && st.xhr) {
        st.cancelled = true;
        st.xhr.abort();
        return;
      }
      if (st.phase === 'send') cancelAction(st, say);
    });

    f.addEventListener('submit', (e) => {
      e.preventDefault();
      const file = f.elements.file.files && f.elements.file.files[0];
      if (!file) { say(errHtml(tr('kvms.up.noFile'))); return; }
      const v = readSendFields(root, f);
      const bad = checkSendFields(v);
      if (bad) { say(errHtml(bad)); return; }
      const q = new URLSearchParams({ pool: v.pool, name: v.name });
      if (v.sha256) q.set('sha256', v.sha256);
      if (v.hosts.length) q.set('hosts', v.hosts.join(','));
      if (v.limit_mbps) q.set('limit_mbps', String(v.limit_mbps));
      if (!v.verify) q.set('verify', '0');
      $('go').disabled = true;
      $('cancel').hidden = false;
      $('progress').hidden = false;
      $('steps').innerHTML = '';
      $('first-bar').style.width = '0%';
      $('send-bar').style.width = '0%';
      $('first-line').textContent = '';
      $('send-line').textContent = '';
      say(esc(tr('kvms.up.receiving', { name: file.name })));
      const xhr = new XMLHttpRequest();
      st.xhr = xhr;
      st.phase = 'receive';
      st.cancelled = false;
      const t0 = Date.now();
      xhr.open('POST', `${hostBase(host)}/images/upload?${q.toString()}`);
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.upload.onprogress = (ev) => {
        if (!ev.lengthComputable || !window.XferProgress) return;
        const elapsed = (Date.now() - t0) / 1000;
        const rate = elapsed > 0 ? ev.loaded / elapsed : null;
        const snap = { phase: 'receive', done: ev.loaded, total: ev.total, rate, elapsed,
                       eta: rate ? (ev.total - ev.loaded) / rate : null };
        $('first-bar').style.width = `${XferProgress.pct(snap)}%`;
        $('first-line').textContent = XferProgress.text(snap);
      };
      xhr.onabort = () => {
        idle();
        st.phase = 'idle';
        say(errHtml(tr('kvms.up.cancelledReceive')));
      };
      xhr.onerror = () => { idle(); st.phase = 'idle'; say(errHtml(tr('kvms.up.network'))); };
      xhr.onload = () => {
        let d = {};
        try { d = JSON.parse(xhr.responseText); } catch { /* sans corps */ }
        st.xhr = null;
        if (xhr.status === 202 && d.action_id) {
          $('first-bar').style.width = '100%';
          say(esc(tr('kvms.up.sending', { name: v.name })));
          followSend(st, $, d.action_id, host, (ok, run, o) => {
            idle();
            say(sendResultHtml(ok, run, o, host));
            if (ok) { $('send-bar').style.width = '100%'; refreshList(); }
          });
          return;
        }
        idle();
        st.phase = 'idle';
        if (st.cancelled || xhr.status === 409 && d.error === 'cancelled') { say(errHtml(tr('kvms.up.cancelledReceive'))); return; }
        // 422 : somme fausse, qcow2 à image de base... refusé AVANT tout envoi
        say(`${errHtml(d.error || `HTTP ${xhr.status}`)}${(d.refusals || []).length > 1 ? listHtml(d.refusals) : ''}`);
      };
      xhr.send(file);
    });
    return root;
  }

  // -- Télécharger depuis une URL --------------------------------------------------------
  function openFetch(host, ctx = {}) {
    const root = panel(`kvm-fetch-${enc(host)}`, tr('kvms.dl.title', { host }), 'download', `
      <form class="of-form kvm-vmw kvms-fetch" autocomplete="off" novalidate>
        <p class="form-hint">${esc(tr('kvms.dl.hint'))}</p>
        ${field(tr('kvms.dl.url'), '<input name="url" maxlength="4096" required spellcheck="false" placeholder="https://">', tr('kvms.dl.urlTip'))}
        ${sendFieldsHtml(host, ctx)}
        <div class="bk-form-actions">
          <button type="submit" class="btn btn-sm btn-primary tip" data-ks="go" data-tip="${esc(tr('kvms.dl.goTip'))}">${icon('download')} ${esc(tr('kvms.dl.go'))}</button>
          <button type="button" class="btn btn-sm btn-secondary tip" data-ks="cancel" hidden data-tip="${esc(tr('kvms.up.cancelTip'))}">${icon('stop')} ${esc(tr('kvms.up.cancel'))}</button>
        </div>
        ${progressHtml()}
        <div class="of-msg" data-ks="msg" role="status" aria-live="polite"></div>
        <ul class="kvm-steps" data-ks="steps"></ul>
      </form>`, [640, 720]);
    if (!root || root.dataset.ksReady) return root;
    root.dataset.ksReady = '1';
    const f = root.querySelector('form');
    const $ = (k) => root.querySelector(`[data-ks="${k}"]`);
    const say = (html) => { $('msg').innerHTML = html; };
    $('first-label').textContent = tr('kvms.dl.phaseDownload');
    const st = { action: null, phase: 'idle' };
    let nameTouched = false;
    f.elements.name.addEventListener('input', () => { nameTouched = true; });
    f.elements.url.addEventListener('input', () => { if (!nameTouched) f.elements.name.value = suggestName(f.elements.url.value); });
    $('cancel').addEventListener('click', () => { if (st.phase === 'send') cancelAction(st, say); });

    f.addEventListener('submit', async (e) => {
      e.preventDefault();
      const url = f.elements.url.value.trim();
      const v = readSendFields(root, f);
      if (!publicUrl(url)) { say(errHtml(tr('kvms.dl.badUrl'))); return; }
      const bad = checkSendFields(v);
      if (bad) { say(errHtml(bad)); return; }
      // comme le serveur : en http simple, seule la somme prouve le fichier
      if (/^http:/i.test(url) && !v.sha256) { say(errHtml(tr('kvms.dl.needSha'))); return; }
      const body = { url, pool: v.pool, name: v.name, hosts: v.hosts, verify: v.verify };
      if (v.sha256) body.sha256 = v.sha256;
      if (v.limit_mbps) body.limit_mbps = v.limit_mbps;
      $('go').disabled = true;
      $('steps').innerHTML = '';
      say(esc(tr('kvms.dl.checking', { url: publicUrl(url) })));
      try {
        const out = await send('POST', `${hostBase(host)}/images/fetch`, body);
        $('cancel').hidden = false;
        $('progress').hidden = false;
        say(esc(tr('kvms.dl.started', { url: out.url || publicUrl(url) })));
        followSend(st, $, out.action_id, host, (ok, run, o) => {
          $('go').disabled = false;
          $('cancel').hidden = true;
          say(sendResultHtml(ok, run, o, host));
          if (ok) { $('send-bar').style.width = '100%'; refreshList(); }
        });
      } catch (err) {
        // refus de l'URL par la console (adresse interne, identifiants, http
        // sans somme...) : le message du serveur, qui ne recopie pas l'URL
        say(`<div class="kvm-op-refusals" data-ks="refusals">${errHtml(err.message)}</div>`);
        $('go').disabled = false;
      }
    });
    return root;
  }

  // -- Pools d'images de l'hôte ----------------------------------------------------------
  function openImagePools(host, ctx = {}) {
    const current = new Set(ctx.current || []);
    const root = panel(`kvm-image-pools-${enc(host)}`, tr('kvms.ip.title', { host }), 'storage', `
      <form class="of-form kvm-vmw kvms-image-pools" autocomplete="off" novalidate>
        <p class="form-hint">${esc(tr('kvms.ip.hint'))}</p>
        <fieldset><legend>${esc(tr('kvms.ip.pools'))}</legend>
          ${(ctx.pools || []).map((p) => `<label class="kvm-check tip" data-tip="${esc(tr('kvms.ip.poolTip'))}"><input type="checkbox" data-ks-pool="${esc(p)}"${current.has(p) ? ' checked' : ''}> ${esc(p)}</label>`).join('')}
        </fieldset>
        <div class="bk-form-actions"><button type="submit" class="btn btn-sm btn-primary tip" data-ks="go"
          data-tip="${esc(tr('kvms.ip.goTip'))}">${icon('save')} ${esc(tr('kvms.ip.go'))}</button></div>
        <div class="of-msg" data-ks="msg" role="status" aria-live="polite"></div>
        <ul class="kvm-steps" data-ks="steps"></ul>
      </form>`, [520, 480]);
    if (!root || root.dataset.ksReady) return root;
    root.dataset.ksReady = '1';
    const f = root.querySelector('form');
    const $ = (k) => root.querySelector(`[data-ks="${k}"]`);
    const say = (html) => { $('msg').innerHTML = html; };
    f.addEventListener('submit', async (e) => {
      e.preventDefault();
      const pools = [...root.querySelectorAll('[data-ks-pool]:checked')].map((c) => c.dataset.ksPool);
      $('go').disabled = true;
      $('steps').innerHTML = '';
      try {
        const out = await send('PUT', `${hostBase(host)}/image-pools`, { pools });
        track(out.action_id, {
          step: (s) => stepLine($('steps'), s),
          end: (ok, run) => {
            const o = (run.result && run.result.output) || {};
            $('go').disabled = false;
            if (ok) { say(okHtml(pools.length ? tr('kvms.ip.done', { pools: pools.join(', ') }) : tr('kvms.ip.cleared'))); refreshList(); } else { say(errHtml(failText(run, o))); }
          },
        });
      } catch (err) { say(errHtml(err.message)); $('go').disabled = false; }
    });
    return root;
  }

  // -- Créer un réseau -----------------------------------------------------------------
  function openNetworkCreate(host, ctx = {}) {
    const root = panel(`kvm-net-create-${enc(host)}`, tr('kvms.net.createTitle', { host }), 'network', `
      <form class="of-form kvm-vmw kvms-net-create" autocomplete="off" novalidate>
        <p class="form-hint">${esc(tr('kvms.net.hint'))}</p>
        <div class="ct-create-row">
          ${field(tr('kvms.net.name'), '<input name="name" maxlength="64" required>', tr('kvms.net.nameTip'))}
          ${field(tr('kvms.net.mode'), `<select name="mode">${NET_MODES.map((m) => opt(m, NET_MODE_LABEL[m](), m === 'nat')).join('')}</select>`, tr('kvms.net.modeTip'))}
        </div>
        <div data-ks="routed">
          <div class="ct-create-row">
            ${field(tr('kvms.net.subnet'), '<input name="subnet" maxlength="18" placeholder="192.168.150.0/24">', tr('kvms.net.subnetTip'))}
            ${field(tr('kvms.net.bridgeName'), '<input name="bridge" maxlength="15" placeholder="virbr10">', tr('kvms.net.bridgeNameTip'))}
          </div>
          <label class="kvm-check tip" data-tip="${esc(tr('kvms.net.dhcpTip'))}"><input type="checkbox" name="dhcp" checked> ${esc(tr('kvms.net.dhcp'))}</label>
          <div class="ct-create-row">
            ${field(tr('kvms.net.dhcpStart'), '<input name="dhcp_start" maxlength="15">', tr('kvms.net.dhcpStartTip'))}
            ${field(tr('kvms.net.dhcpEnd'), '<input name="dhcp_end" maxlength="15">', tr('kvms.net.dhcpEndTip'))}
          </div>
          ${field(tr('kvms.net.domain'), '<input name="domain" maxlength="253" placeholder="lab.local">', tr('kvms.net.domainTip'))}
        </div>
        <div data-ks="bridged" hidden>
          ${field(tr('kvms.net.hostBridge'), '<select name="host_bridge"></select>', tr('kvms.net.hostBridgeTip'))}
        </div>
        <label class="kvm-check tip" data-tip="${esc(tr('kvms.net.autostartTip'))}"><input type="checkbox" name="autostart" checked> ${esc(tr('kvms.autostart'))}</label>
        <div class="bk-form-actions"><button type="submit" class="btn btn-sm btn-primary tip" data-ks="go"
          data-tip="${esc(tr('kvms.net.goTip'))}">${icon('add')} ${esc(tr('kvms.net.go'))}</button></div>
        <div class="of-msg" data-ks="msg" role="status" aria-live="polite"></div>
        <ul class="kvm-steps" data-ks="steps"></ul>
      </form>`, [620, 680]);
    if (!root || root.dataset.ksReady) return root;
    root.dataset.ksReady = '1';
    const f = root.querySelector('form');
    const $ = (k) => root.querySelector(`[data-ks="${k}"]`);
    const say = (html) => { $('msg').innerHTML = html; };
    let rangeTouched = false;
    const fillRange = () => {
      if (rangeTouched) return;
      const [a, b] = dhcpRange(f.elements.subnet.value.trim());
      f.elements.dhcp_start.value = a;
      f.elements.dhcp_end.value = b;
    };
    f.elements.subnet.addEventListener('input', fillRange);
    ['dhcp_start', 'dhcp_end'].forEach((k) => f.elements[k].addEventListener('input', () => { rangeTouched = true; }));
    const showMode = () => {
      const bridged = f.elements.mode.value === 'bridge';
      $('routed').hidden = bridged;
      $('bridged').hidden = !bridged;
    };
    f.elements.mode.addEventListener('change', showMode);
    f.elements.dhcp.addEventListener('change', () => {
      f.elements.dhcp_start.disabled = !f.elements.dhcp.checked;
      f.elements.dhcp_end.disabled = !f.elements.dhcp.checked;
    });
    showMode();
    // ponts de l'hôte et sous-réseau libre proposé (le serveur refait le contrôle)
    Promise.all([getJson(`${hostBase(host)}/links`), getJson(`${hostBase(host)}/networks`)]).then(([links, nets]) => {
      if (!root.isConnected) return;
      const bridges = (links && links.bridges) || [];
      f.elements.host_bridge.innerHTML = bridges.length ? bridges.map((b) => opt(b.name, `${b.name} (${b.state || '?'})`)).join('')
        : opt('', tr('kvms.net.noBridge'), true);
      if (!f.elements.subnet.value) {
        f.elements.subnet.value = freeSubnet(links.error ? {} : links, nets.error ? [] : nets.networks);
        fillRange();
      }
    });
    f.addEventListener('submit', (e) => {
      e.preventDefault();
      const name = f.elements.name.value.trim();
      if (!NAME_RE.test(name)) { say(errHtml(tr('kvms.net.badName'))); return; }
      const mode = f.elements.mode.value;
      const spec = { name, mode, autostart: f.elements.autostart.checked };
      if (mode === 'bridge') {
        spec.bridge = f.elements.host_bridge.value;
        if (!spec.bridge) { say(errHtml(tr('kvms.net.needBridge'))); return; }
      } else {
        spec.subnet = f.elements.subnet.value.trim();
        if (!cidr(spec.subnet)) { say(errHtml(tr('kvms.net.badSubnet'))); return; }
        if (f.elements.bridge.value.trim()) spec.bridge = f.elements.bridge.value.trim();
        spec.dhcp = f.elements.dhcp.checked;
        if (spec.dhcp) {
          spec.dhcp_start = f.elements.dhcp_start.value.trim();
          spec.dhcp_end = f.elements.dhcp_end.value.trim();
        }
        if (f.elements.domain.value.trim()) spec.domain = f.elements.domain.value.trim();
      }
      previewThenCreate({ root, url: `${hostBase(host)}/networks`, spec, say, steps: $('steps'), go: $('go'),
                          done: () => tr('kvms.net.created', { name, host }) });
    });
    return root;
  }

  return { openPoolCreate, openVolumeCreate, openVolumeResize, openDelete, openUpload, openFetch,
           openImagePools, openNetworkCreate, publicUrl, atLeast,
           _internals: { suggestName, freeSubnet, dhcpRange, cidr, overlaps } };
})();

if (typeof window !== 'undefined') window.KVMStorage = KVMStorage;
