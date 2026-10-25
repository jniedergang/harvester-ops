/**
 * harvester-ops : créer et modifier une VM KVM (prochaine version KVM).
 *
 * L'adaptateur KVM de la coquille commune (vm-form.js) et ses fenêtres :
 *   - « Créer une VM » : hôte, nom, nombre d'instances, démarrage, agent
 *     invité, clés SSH, modèle ; onglets Général, Calcul, Micrologiciel,
 *     Disques, Réseau, Cloud-init (mêmes icônes et même ordre que Harvester)
 *     et Revue (aperçu du serveur : refus, avertissements, volumes, XML avec
 *     le mot de passe masqué). Les choix viennent des capacités de l'hôte
 *     (`/caps`) : types de machine, micrologiciel, modes et modèles CPU, bus,
 *     modèles vidéo ; une option grisée porte la raison servie par l'hôte,
 *     jamais une règle propre au navigateur.
 *   - « Modifier » : mêmes onglets remplis par `/form`, un repère par champ
 *     (« maintenant », « au prochain démarrage », « refusé ») d'après la table
 *     HOTNESS servie et l'état de la VM, réglages non gérés en lecture seule,
 *     bandeau « redémarrage nécessaire », résultat par changement. L'empreinte
 *     `config_digest` lue à l'ouverture part avec la modification ; un 409
 *     `changed` propose de relire sans perdre la saisie.
 *   - gestes à chaud du menu d'une VM (CPU et mémoire, disque, carte, CD-ROM,
 *     cloud-init), par les routes `/hot/<op>`.
 *
 * Toute écriture est une action suivie (dock, Activité) que le serveur lance
 * par bin/harvester-kvm.py ; on la suit par `KVM.follow` (SSEReconnect). Aucun
 * secret dans le DOM : le mot de passe VNC est une case (la console le tire et
 * ne le rend jamais), le user-data ne vit que dans la valeur des zones de
 * texte, jamais dans un attribut, un libellé ou un journal.
 */
const KVMForm = (() => {
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const enc = encodeURIComponent;
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 14) => (window.Icons ? Icons.svg(n, { size }) : '');
  const LIVE = ['running', 'idle', 'blocked'];
  const ROLE_RANK = { viewer: 1, operator: 2, admin: 3 };
  const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$/;
  const MAC_RE = /^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$/;
  const TARGET_RE = /^(vd|sd|hd)[a-z]{1,2}$/;
  const OS_TYPES = ['linux', 'windows', 'other'];
  const LIFECYCLE = ['destroy', 'restart', 'preserve', 'rename-restart'];
  const CRASH = LIFECYCLE.concat(['coredump-destroy', 'coredump-restart']);
  const WATCHDOG = ['reset', 'shutdown', 'poweroff', 'pause', 'none', 'dump', 'inject-nmi'];
  const CACHE = ['none', 'writeback', 'writethrough', 'directsync', 'unsafe', 'default'];
  const NIC_KINDS = ['network', 'bridge', 'direct'];
  const NIC_MODELS = ['virtio', 'e1000e', 'e1000', 'rtl8139'];
  const DIRECT_MODES = ['bridge', 'vepa', 'private', 'passthrough'];
  const SOURCES = ['blank', 'image', 'volume', 'iso', 'empty'];
  // réglages posés à la création seulement (refusés par la modification
  // dans cette version : « not changeable by the console in this version »)
  const CREATE_ONLY = ['memory.balloon', 'serial_console', 'watchdog', 'sysinfo_serial', 'cpu.hotplug'];
  // « redémarrage nécessaire » appris par cette page (fenêtre, résultat d'un
  // geste) avant que la liste ne le serve : "hôte/vm" -> {fields, id} ; l'Id
  // libvirt change à chaque démarrage, un repère d'un autre Id est périmé
  const pendingRestart = new Map();
  // relecture de l'état d'une fenêtre « Modifier » (réglable par les tests)
  const POLL = { ms: 20000 };
  function notePending(host, name, fields, id) {
    const key = `${host}/${name}`;
    if (fields && fields.length) pendingRestart.set(key, { fields: [...fields], id: id == null ? null : id });
    else pendingRestart.delete(key);
  }

  // -- requêtes ----------------------------------------------------------------------------
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

  const follow = (id, onStep, onEnd) => KVM.follow(id, onStep, onEnd);
  const errText = (e) => (typeof e === 'string' ? e : (e && (e.message || e.error)) || '?');

  // -- tailles -----------------------------------------------------------------------------
  const UNITS = { '': 1024 ** 3, K: 1024, M: 1024 ** 2, G: 1024 ** 3, T: 1024 ** 4 };
  /** « 10G », « 512 MiB », « 1.5T » -> octets ; sans unité = Gio ; null sinon. */
  function parseSize(text) {
    const m = /^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)(?:i?B)?\s*$/i.exec(String(text == null ? '' : text));
    if (!m) return null;
    return Math.round(parseFloat(m[1]) * UNITS[m[2].toUpperCase()]);
  }
  function bytes(n) {
    if (typeof n !== 'number' || !Number.isFinite(n)) return '–';
    const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
    let i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i += 1; }
    return `${v.toFixed(i ? 1 : 0)} ${u[i]}`;
  }
  const sizeText = (b) => (b && b % 1024 ** 3 === 0 ? `${b / 1024 ** 3}G` : (b ? `${Math.ceil(b / 1024 ** 2)}M` : ''));

  // -- chemins pointés dans un objet ------------------------------------------------------
  function getPath(o, path) {
    return path.split('.').reduce((a, k) => (a && typeof a === 'object' ? a[k] : undefined), o);
  }
  function setPath(o, path, v) {
    const ks = path.split('.');
    let cur = o;
    ks.slice(0, -1).forEach((k) => { if (!cur[k] || typeof cur[k] !== 'object') cur[k] = {}; cur = cur[k]; });
    cur[ks[ks.length - 1]] = v;
  }

  // -- champs ------------------------------------------------------------------------------
  // Une ligne par champ géré : chemin dans le spec, libellé, bulle, type.
  // Clés i18n en toutes lettres (le contrôle de parité ne lit que des littéraux).
  const FIELDS = {
    name: { s: 'general', type: 'text', label: () => tr('kvmf.f.name'), tip: () => tr('kvmf.t.name'), edit: true },
    title: { s: 'general', type: 'text', label: () => tr('kvmf.f.title'), tip: () => tr('kvmf.t.title') },
    description: { s: 'general', type: 'textarea', label: () => tr('kvmf.f.description'), tip: () => tr('kvmf.t.description') },
    os: { s: 'general', type: 'select', options: () => OS_TYPES, label: () => tr('kvmf.f.os'), tip: () => tr('kvmf.t.os') },
    autostart: { s: 'general', type: 'check', label: () => tr('kvmf.f.autostart'), tip: () => tr('kvmf.t.autostart') },
    on_poweroff: { s: 'general', type: 'select', options: () => LIFECYCLE, label: () => tr('kvmf.f.onPoweroff'), tip: () => tr('kvmf.t.onPoweroff') },
    on_reboot: { s: 'general', type: 'select', options: () => LIFECYCLE, label: () => tr('kvmf.f.onReboot'), tip: () => tr('kvmf.t.onReboot') },
    on_crash: { s: 'general', type: 'select', options: () => CRASH, label: () => tr('kvmf.f.onCrash'), tip: () => tr('kvmf.t.onCrash') },
    'cpu.vcpus': { s: 'compute', type: 'int', min: 1, label: () => tr('kvmf.f.vcpus'), tip: () => tr('kvmf.t.vcpus') },
    'cpu.max_vcpus': { s: 'compute', type: 'int', min: 1, label: () => tr('kvmf.f.maxVcpus'), tip: () => tr('kvmf.t.maxVcpus') },
    'cpu.sockets': { s: 'compute', type: 'int', min: 1, label: () => tr('kvmf.f.sockets'), tip: () => tr('kvmf.t.sockets') },
    'cpu.cores': { s: 'compute', type: 'int', min: 1, label: () => tr('kvmf.f.cores'), tip: () => tr('kvmf.t.cores') },
    'cpu.threads': { s: 'compute', type: 'int', min: 1, label: () => tr('kvmf.f.threads'), tip: () => tr('kvmf.t.threads') },
    'memory.current_mib': { s: 'compute', type: 'int', min: 16, label: () => tr('kvmf.f.memory'), tip: () => tr('kvmf.t.memory') },
    'memory.max_mib': { s: 'compute', type: 'int', min: 16, label: () => tr('kvmf.f.maxMemory'), tip: () => tr('kvmf.t.maxMemory') },
    'cpu.hotplug': { s: 'compute', type: 'check', label: () => tr('kvmf.f.hotplug'), tip: () => tr('kvmf.t.hotplug') },
    'cpu.mode': { s: 'compute', type: 'select', options: (o) => o.cpu_modes || [], label: () => tr('kvmf.f.cpuMode'), tip: () => tr('kvmf.t.cpuMode') },
    'cpu.model': { s: 'compute', type: 'select', options: (o) => [''].concat(o.cpu_models || []), label: () => tr('kvmf.f.cpuModel'), tip: () => tr('kvmf.t.cpuModel') },
    // modification : la valeur actuelle (version figée) et sa famille (la
    // dernière version au prochain démarrage) ; libvirt refuse un changement
    // de famille sur une VM existante
    machine: { s: 'firmware', type: 'select', options: (o, all, ctx) => (ctx && ctx.mode === 'edit'
      ? [...new Set([ctx.form.view.machine, ctx.family].filter(Boolean))] : all.machines || []), label: () => tr('kvmf.f.machine'), tip: () => tr('kvmf.t.machine') },
    firmware: { s: 'firmware', type: 'select', options: (o) => o.firmware || ['bios'], label: () => tr('kvmf.f.firmware'), tip: () => tr('kvmf.t.firmware') },
    secure_boot: { s: 'firmware', type: 'check', cap: 'secure_boot', label: () => tr('kvmf.f.secureBoot'), tip: () => tr('kvmf.t.secureBoot') },
    tpm: { s: 'firmware', type: 'check', cap: 'tpm', label: () => tr('kvmf.f.tpm'), tip: () => tr('kvmf.t.tpm') },
    sysinfo_serial: { s: 'firmware', type: 'text', label: () => tr('kvmf.f.sysinfoSerial'), tip: () => tr('kvmf.t.sysinfoSerial') },
    'display.vnc': { s: 'firmware', type: 'check', label: () => tr('kvmf.f.vnc'), tip: () => tr('kvmf.t.vnc') },
    'display.password': { s: 'firmware', type: 'check', label: () => tr('kvmf.f.vncPassword'), tip: () => tr('kvmf.t.vncPassword') },
    'display.video': { s: 'firmware', type: 'select', options: (o) => o.video_models || ['virtio'], label: () => tr('kvmf.f.video'), tip: () => tr('kvmf.t.video') },
    serial_console: { s: 'firmware', type: 'check', label: () => tr('kvmf.f.serialConsole'), tip: () => tr('kvmf.t.serialConsole') },
    tablet: { s: 'firmware', type: 'check', label: () => tr('kvmf.f.tablet'), tip: () => tr('kvmf.t.tablet') },
    'memory.balloon': { s: 'firmware', type: 'check', label: () => tr('kvmf.f.balloon'), tip: () => tr('kvmf.t.balloon') },
    rng: { s: 'firmware', type: 'check', label: () => tr('kvmf.f.rng'), tip: () => tr('kvmf.t.rng') },
    guest_agent: { s: 'firmware', type: 'check', label: () => tr('kvmf.f.guestAgent'), tip: () => tr('kvmf.t.guestAgent'), edit: true },
    watchdog: { s: 'firmware', type: 'select', options: () => [''].concat(WATCHDOG), label: () => tr('kvmf.f.watchdog'), tip: () => tr('kvmf.t.watchdog') },
    hotplug_slots: { s: 'firmware', type: 'int', min: 0, max: 16, label: () => tr('kvmf.f.hotplugSlots'), tip: () => tr('kvmf.t.hotplugSlots') },
  };
  const ORDER = Object.keys(FIELDS);

  const SECTIONS = [
    { id: 'general', icon: 'general', label: () => tr('kvmf.s.general'), tip: tr('kvmf.s.generalTip') },
    { id: 'compute', icon: 'compute', label: () => tr('kvmf.s.compute'), tip: tr('kvmf.s.computeTip') },
    { id: 'firmware', icon: 'firmware', label: () => tr('kvmf.s.firmware'), tip: tr('kvmf.s.firmwareTip') },
    { id: 'disks', icon: 'disk', label: () => tr('kvmf.s.disks'), tip: tr('kvmf.s.disksTip') },
    { id: 'network', icon: 'network', label: () => tr('kvmf.s.network'), tip: tr('kvmf.s.networkTip') },
    { id: 'cloudinit', icon: 'cloud', label: () => tr('kvmf.s.cloudinit'), tip: tr('kvmf.s.cloudinitTip') },
    { id: 'review', icon: 'preview', label: () => tr('kvmf.s.review'), tip: tr('kvmf.s.reviewTip') },
  ];
  // les libellés des onglets sont relus à l'ouverture (langue courante)
  const sections = () => SECTIONS.map((s) => Object.assign({}, s, { tip: s.id === 'review' ? tr('kvmf.s.reviewTip') : s.tip }));

  /** Un champ du formulaire : libellé, contrôle (bulle d'aide), repère
   *  « maintenant / prochain démarrage » (modification), raison si grisé. */
  function fieldHtml(name, ctx) {
    const f = FIELDS[name];
    const o = ctx.opts || {};
    const why = disabledWhy(name, ctx);
    const dis = why ? ' disabled aria-disabled="true"' : '';
    const tip = esc(why || f.tip());
    const hot = ctx.mode === 'edit' ? `<span class="kvmf-hot" data-hot="${esc(name)}"></span>` : '';
    const whyHtml = why ? `<span class="form-hint kvmf-why" data-why="${esc(name)}">${esc(why)}</span>` : '';
    if (f.type === 'check') {
      return `<label class="bk-check tip kvmf-field" data-vmf-field="${esc(name)}" data-tip="${tip}">
          <input type="checkbox" name="${esc(name)}"${dis}> <span>${esc(f.label())}</span> ${hot}${whyHtml}</label>`;
    }
    let control;
    if (f.type === 'select') {
      const opts = f.options(o, ctx.caps || {}, ctx) || [];
      control = `<select name="${esc(name)}" class="tip" data-tip="${tip}"${dis}>${opts.map((v) => `<option value="${esc(v)}">${esc(v === '' ? tr('kvmf.none') : v)}</option>`).join('')}</select>`;
    } else if (f.type === 'textarea') {
      control = `<textarea name="${esc(name)}" rows="2" maxlength="4000" class="tip" data-tip="${tip}"${dis}></textarea>`;
    } else if (f.type === 'int') {
      control = `<input type="number" name="${esc(name)}" class="tip" data-tip="${tip}"${f.min != null ? ` min="${f.min}"` : ''}${f.max != null ? ` max="${f.max}"` : ''}${dis}>`;
    } else {
      control = `<input type="text" name="${esc(name)}" maxlength="200" autocomplete="off" class="tip" data-tip="${tip}"${dis}>`;
    }
    return `<label class="bk-field kvmf-field" data-vmf-field="${esc(name)}"><span>${esc(f.label())} ${hot}</span>${control}${whyHtml}</label>`;
  }

  /** Raison d'un champ grisé : celle de l'hôte (`/caps`), le réglage posé à
   *  la création seulement, ou la VM transitoire ; '' s'il est libre. */
  function disabledWhy(name, ctx) {
    if (ctx.readOnly) return ctx.readOnly;
    if (ctx.mode === 'edit' && CREATE_ONLY.includes(name)) return tr('kvmf.createOnly');
    if (ctx.mode === 'create' && name === 'name') return '';
    const f = FIELDS[name];
    if (f.cap && ctx.opts && !ctx.opts[f.cap]) {
      const fam = ctx.family;
      return (ctx.caps && ctx.caps.disabled && ctx.caps.disabled[fam] && ctx.caps.disabled[fam][f.cap])
        || tr('kvmf.hostCannot');
    }
    if (name === 'cpu.model' && ctx.opts && !(ctx.opts.cpu_models || []).length) return tr('kvmf.noCpuModels');
    return '';
  }

  function readControl(el) {
    if (el.type === 'checkbox') return el.checked;
    if (el.type === 'number') return el.value === '' ? null : Number(el.value);
    return el.value === '' ? null : el.value;
  }
  function writeControl(el, v) {
    if (el.type === 'checkbox') el.checked = !!v;
    else if (el.tagName === 'SELECT') {
      const want = v == null ? '' : String(v);
      if (want && ![...el.options].some((o) => o.value === want)) {
        el.insertAdjacentHTML('beforeend', `<option value="${esc(want)}">${esc(want)}</option>`);
      }
      el.value = want;
    } else el.value = v == null ? '' : String(v);
  }

  /** Les champs gérés lus dans la fenêtre : {chemin: valeur}. */
  function readFields(root) {
    const out = {};
    ORDER.forEach((name) => {
      const el = root.querySelector(`[data-vmf-field="${CSS.escape(name)}"] [name="${CSS.escape(name)}"]`);
      if (el) out[name] = readControl(el);
    });
    return out;
  }
  function fillFields(root, src) {
    ORDER.forEach((name) => {
      const el = root.querySelector(`[data-vmf-field="${CSS.escape(name)}"] [name="${CSS.escape(name)}"]`);
      if (!el) return;
      const v = getPath(src, name);
      if (v !== undefined) writeControl(el, name === 'watchdog' && v == null ? '' : v);
    });
  }

  // -- disques et cartes : moteur TFForm, schémas KVM -------------------------------------
  /** Choix d'images des cartes de disque. Le catalogue arrive APRÈS
   *  l'ouverture de la fenêtre (il demande un relevé du stockage de l'hôte) :
   *  les mêmes tableaux sont remplis en place, un schéma déjà construit (carte
   *  ajoutée plus tard, validation) voit donc la liste à jour. */
  function imageLists(ctx) {
    if (!ctx.imgLists) ctx.imgLists = { imgs: [], isos: [], vols: [] };
    return ctx.imgLists;
  }
  /** État du catalogue dans l'onglet Disques : en cours, erreur, ou rien. */
  function imagesStateText(ctx) {
    if (ctx.imagesState === 'loading') return tr('kvmf.imagesLoading');
    if (ctx.imagesState === 'error') return tr('kvmf.imagesError', { error: errText(ctx.imagesError || '?') });
    return '';
  }
  function imagesStateHtml(ctx) {
    const text = imagesStateText(ctx);
    return `<p class="form-hint${ctx.imagesState === 'error' ? ' res-warn' : ''}" data-kvmf="images-state" role="status"${text ? '' : ' hidden'}>${esc(text)}</p>`;
  }
  function fillImageLists(ctx, images) {
    const l = imageLists(ctx);
    const put = (arr, vals) => { arr.splice(0, arr.length, ...vals); };
    const ref = (i) => `${i.pool}/${i.volume}`;
    // l'image d'ouverture (« Créer une VM » depuis une image) reste choisie
    const all = (images || []).slice();
    if (ctx.image && !all.some((i) => i.pool === ctx.image.pool && i.volume === ctx.image.volume)) all.push(ctx.image);
    put(l.imgs, all.filter((i) => !i.iso).map(ref));
    put(l.isos, all.filter((i) => i.iso).map(ref));
    put(l.vols, all.map(ref));
  }

  /** Schéma des cartes de disque, choix venus de l'hôte (pools actifs,
   *  images, bus annoncés par `domcapabilities`). */
  function diskSchema(ctx) {
    const pools = (ctx.pools || []).filter((p) => p.state === 'running').map((p) => p.name);
    const { imgs, isos, vols } = imageLists(ctx);
    const buses = (ctx.opts && ctx.opts.disk_buses) || ['virtio', 'sata', 'scsi'];
    const L = (s) => ({ en: s });
    return {
      id: 'kvm-disks',
      nested: {
        disk: {
          min: 0, max: 16,
          label: L(tr('kvmf.disks')),
          itemTitle: (v) => ({ icon: v.device === 'cdrom' ? 'cdrom' : 'disk', text:
            `${v.target || tr('kvmf.newDisk')}${v.bus ? ` · ${v.bus}` : ''}${v.source ? ` · ${v.source}` : ''}${v.size ? ` · ${v.size}` : ''}` }),
          newItem: (items) => ({ source: items.length === 0 && imgs.length ? 'image' : 'blank',
                                 pool: ctx.defaultPool || pools[0] || '' }),
          args: [
            { name: 'device', type: 'enum', required: true, default: 'disk', enum_values: ['disk', 'cdrom'], label: L(tr('kvmf.d.device')), description: L(tr('kvmf.d.deviceTip')) },
            { name: 'source', type: 'enum', required: true, default: 'blank', enum_values: SOURCES, label: L(tr('kvmf.d.source')), description: L(tr('kvmf.d.sourceTip')) },
            { name: 'pool', type: 'enum', enum_values: pools, label: L(tr('kvmf.d.pool')), description: L(tr('kvmf.d.poolTip')) },
            { name: 'image', type: 'enum', enum_values: imgs, label: L(tr('kvmf.d.image')), description: L(tr('kvmf.d.imageTip')) },
            { name: 'linked', type: 'bool', default: true, label: L(tr('kvmf.d.linked')), description: L(tr('kvmf.d.linkedTip')) },
            { name: 'iso', type: 'enum', enum_values: isos, label: L(tr('kvmf.d.iso')), description: L(tr('kvmf.d.isoTip')) },
            { name: 'volume', type: 'enum', enum_values: vols, label: L(tr('kvmf.d.volume')), description: L(tr('kvmf.d.volumeTip')) },
            { name: 'size', type: 'text', suggest: ['1G', '10G', '20G', '40G', '100G'], label: L(tr('kvmf.d.size')), description: L(tr('kvmf.d.sizeTip')) },
            { name: 'bus', type: 'enum', enum_values: buses, label: L(tr('kvmf.d.bus')), description: L(tr('kvmf.d.busTip')) },
            { name: 'target', type: 'text', validate: TARGET_RE, label: L(tr('kvmf.d.target')), description: L(tr('kvmf.d.targetTip')) },
            { name: 'boot_order', type: 'int', default: 0, min: 0, max: 64, label: L(tr('kvmf.d.bootOrder')), description: L(tr('kvmf.d.bootOrderTip')) },
            { name: 'cache', type: 'enum', enum_values: CACHE, label: L(tr('kvmf.d.cache')), description: L(tr('kvmf.d.cacheTip')) },
            { name: 'serial', type: 'text', validate: /^[A-Za-z0-9_.+-]{0,36}$/, label: L(tr('kvmf.d.serial')), description: L(tr('kvmf.d.serialTip')) },
            { name: 'readonly', type: 'bool', default: false, label: L(tr('kvmf.d.readonly')), description: L(tr('kvmf.d.readonlyTip')) },
            { name: 'shareable', type: 'bool', default: false, label: L(tr('kvmf.d.shareable')), description: L(tr('kvmf.d.shareableTip')) },
          ],
        },
      },
    };
  }

  function nicSchema(ctx) {
    const nets = (ctx.networks || []).map((n) => n.name);
    const bridges = ((ctx.links || {}).bridges || []).map((b) => b.name);
    const devs = ((ctx.links || {}).physical || []).map((b) => b.name);
    const L = (s) => ({ en: s });
    return {
      id: 'kvm-nics',
      nested: {
        nic: {
          min: 0, max: 16,
          label: L(tr('kvmf.nics')),
          itemTitle: (v) => ({ icon: 'plug', text: `${v.kind || 'network'} · ${v.network || v.bridge || v.dev || '?'}${v.model ? ` · ${v.model}` : ''}${v.mac ? ` · ${v.mac}` : ''}` }),
          newItem: () => ({ kind: nets.length ? 'network' : 'bridge', network: nets[0] || '', bridge: bridges[0] || '' }),
          args: [
            { name: 'kind', type: 'enum', required: true, default: 'network', enum_values: NIC_KINDS, label: L(tr('kvmf.n.kind')), description: L(tr('kvmf.n.kindTip')) },
            { name: 'network', type: 'enum', enum_values: nets, label: L(tr('kvmf.n.network')), description: L(tr('kvmf.n.networkTip')) },
            { name: 'bridge', type: 'enum', enum_values: bridges, label: L(tr('kvmf.n.bridge')), description: L(tr('kvmf.n.bridgeTip')) },
            { name: 'dev', type: 'enum', enum_values: devs, label: L(tr('kvmf.n.dev')), description: L(tr('kvmf.n.devTip')) },
            { name: 'mode', type: 'enum', default: 'bridge', enum_values: DIRECT_MODES, label: L(tr('kvmf.n.mode')), description: L(tr('kvmf.n.modeTip')) },
            { name: 'model', type: 'enum', default: 'virtio', enum_values: NIC_MODELS, label: L(tr('kvmf.n.model')), description: L(tr('kvmf.n.modelTip')) },
            { name: 'mac', type: 'text', validate: MAC_RE, label: L(tr('kvmf.n.mac')), description: L(tr('kvmf.n.macTip')) },
            { name: 'boot_order', type: 'int', default: 0, min: 0, max: 64, label: L(tr('kvmf.d.bootOrder')), description: L(tr('kvmf.n.bootOrderTip')) },
          ],
        },
      },
    };
  }

  /** Les champs d'une carte selon sa source ou son genre (comme
   *  syncDiskSourceFields côté Harvester). */
  function syncCards(editor, kind) {
    editor.querySelectorAll('.tf-block-item').forEach((item) => {
      const g = (n) => item.querySelector(`[name$=".${n}"]`);
      const show = (n, on) => { const el = g(n); const w = el && el.closest('.tf-field'); if (w) w.style.display = on ? '' : 'none'; };
      if (kind === 'disk') {
        const src = (g('source') || {}).value || 'blank';
        show('pool', ['blank', 'image'].includes(src));
        show('image', src === 'image');
        show('linked', src === 'image');
        show('iso', src === 'iso');
        show('volume', src === 'volume');
        show('size', ['blank', 'image'].includes(src));
        if (['iso', 'empty'].includes(src) && g('device')) g('device').value = 'cdrom';
      } else {
        const k = (g('kind') || {}).value || 'network';
        show('network', k === 'network');
        show('bridge', k === 'bridge');
        show('dev', k === 'direct');
        show('mode', k === 'direct');
      }
    });
    // + Add / − Remove de TFForm : une bulle comme tout contrôle
    editor.querySelectorAll('.tf-block-add, .tf-block-remove').forEach((b) => {
      if (!b.dataset.tip) {
        b.classList.add('tip');
        b.dataset.tip = b.classList.contains('tf-block-add')
          ? (kind === 'disk' ? tr('kvmf.addDiskTip') : tr('kvmf.addNicTip'))
          : (kind === 'disk' ? tr('kvmf.removeDiskTip') : tr('kvmf.removeNicTip'));
      }
    });
  }

  function wireCards(editor, schema, kind, onChange) {
    TFForm.wire(editor, schema, '');
    const sync = () => { syncCards(editor, kind); if (onChange) onChange(); };
    sync();
    editor.addEventListener('change', sync);
    editor.addEventListener('input', () => onChange && onChange());
    const list = editor.querySelector('.tf-block-list');
    if (list) new MutationObserver(sync).observe(list, { childList: true });
  }

  /** Une carte de disque -> un disque du spec (VmSpec de kvm_xml). */
  function diskItem(c, errors, i) {
    const src = c.source || 'blank';
    const d = { device: ['iso', 'empty'].includes(src) ? 'cdrom' : (c.device || 'disk') };
    if (c.bus) d.bus = c.bus;
    if (c.target) d.target = c.target;
    if (Number(c.boot_order) > 0) d.boot_order = Number(c.boot_order);
    if (c.cache) d.cache = c.cache;
    if (c.serial) d.serial = c.serial;
    if (c.readonly) d.readonly = true;
    if (c.shareable) d.shareable = true;
    const where = { section: 'disks', field: `disk[${i}]` };
    const size = c.size ? parseSize(c.size) : null;
    if (c.size && !size) errors.push(Object.assign({ message: tr('kvmf.e.size', { n: i + 1 }) }, where));
    if (src === 'blank') {
      if (!c.pool) errors.push(Object.assign({ message: tr('kvmf.e.pool', { n: i + 1 }) }, where));
      if (!size) errors.push(Object.assign({ message: tr('kvmf.e.blankSize', { n: i + 1 }) }, where));
      d.source = { kind: 'blank', pool: c.pool || null, size_bytes: size, format: 'qcow2' };
    } else if (src === 'image') {
      if (!c.image) errors.push(Object.assign({ message: tr('kvmf.e.image', { n: i + 1 }) }, where));
      const [ip, ...rest] = String(c.image || '').split('/');
      d.source = { kind: 'image', pool: c.pool || ip || null, image: rest.join('/') || null, image_pool: ip || null,
                   linked: c.linked !== false };
      if (size) d.source.size_bytes = size;
    } else if (src === 'volume') {
      if (!c.volume) errors.push(Object.assign({ message: tr('kvmf.e.volume', { n: i + 1 }) }, where));
      const [p, ...rest] = String(c.volume || '').split('/');
      d.source = { kind: 'volume', pool: p || null, volume: rest.join('/') || null };
    } else if (src === 'iso') {
      if (!c.iso) errors.push(Object.assign({ message: tr('kvmf.e.iso', { n: i + 1 }) }, where));
      const [p, ...rest] = String(c.iso || '').split('/');
      d.source = { kind: 'iso', pool: p || null, volume: rest.join('/') || null };
    } else d.source = { kind: 'empty' };
    return d;
  }

  function nicItem(c, errors, i) {
    const kind = c.kind || 'network';
    const n = { kind, source: kind === 'network' ? c.network : kind === 'bridge' ? c.bridge : c.dev, model: c.model || 'virtio' };
    if (!n.source) errors.push({ section: 'network', field: `nic[${i}]`, message: tr('kvmf.e.nicSource', { n: i + 1 }) });
    if (kind === 'direct') n.mode = c.mode || 'bridge';
    if (c.mac) {
      if (!MAC_RE.test(c.mac)) errors.push({ section: 'network', field: `nic[${i}]`, message: tr('kvmf.e.mac', { n: i + 1 }) });
      n.mac = c.mac.toLowerCase();
    }
    if (Number(c.boot_order) > 0) n.boot_order = Number(c.boot_order);
    return n;
  }

  /** Place restante de chaque pool sous les cartes (comme
   *  renderDiskCapacity pour Longhorn) : disques vierges et copies complètes
   *  comptés à leur taille, disques liés à zéro (ils grossissent ensuite). */
  function renderCapacity(el, ctx, cards) {
    if (!el) return;
    const need = new Map();
    (cards || []).forEach((c) => {
      const size = parseSize(c.size);
      const src = c.source || 'blank';
      if (!size || !c.pool) return;
      if (src === 'blank' || (src === 'image' && c.linked === false)) need.set(c.pool, (need.get(c.pool) || 0) + size);
    });
    const rows = [...need.entries()].map(([pool, n]) => {
      const p = (ctx.pools || []).find((x) => x.name === pool) || {};
      const over = typeof p.available === 'number' && n > p.available;
      return `<span class="${over ? 'vm-cap-over' : ''}" data-cap-pool="${esc(pool)}">${icon(over ? 'warn' : 'storage', 12)} ${esc(tr('kvmf.capacity', { pool, need: bytes(n), free: bytes(p.available) }))}</span>`;
    });
    el.innerHTML = rows.join(' · ');
  }

  // -- clés SSH : KeyPairs des clusters Harvester lisibles + clés collées -----------------
  async function loadKeys(sel, store) {
    try {
      const d = await getJson('/api/clusters');
      const clusters = (d.clusters || []).map((c) => c.name);
      const groups = await Promise.all(clusters.map(async (c) => {
        const l = await getJson(`/api/sshkeys/${enc(c)}`);
        const keys = (Array.isArray(l) ? l : l.items || []).filter((k) => k && k.name && k.public_key);
        return { cluster: c, keys };
      }));
      let html = '';
      groups.filter((g) => g.keys.length).forEach((g) => {
        html += `<optgroup label="${esc(g.cluster)}">${g.keys.map((k) => {
          const id = `${g.cluster}/${k.namespace}/${k.name}`;
          store.set(id, k.public_key);
          return `<option value="${esc(id)}">${esc(`${k.namespace}/${k.name}`)}</option>`;
        }).join('')}</optgroup>`;
      });
      sel.innerHTML = html;
      sel.disabled = !html;
    } catch { sel.disabled = true; }
  }

  function chosenKeys(head, store) {
    const picked = [...head.querySelectorAll('[name="ssh_keys"] option')].filter((o) => o.selected).map((o) => store.get(o.value)).filter(Boolean);
    const pasted = (head.querySelector('[name="ssh_extra"]').value || '').split('\n').map((l) => l.trim()).filter(Boolean);
    return { keys: picked.concat(pasted), bad: pasted.filter((k) => !CloudInitForm.SSH_KEY_RE.test(k)) };
  }

  /** Pourquoi les clés SSH ou l'agent invité n'ont pas pu être ajoutés au
   *  user-data (withKeysAndAgent n'a rien modifié) : un message par cause. */
  function ciRefusals(merged, keys, agent) {
    const key = merged.key || '';
    if (merged.reason === 'not-cloud-config') {
      const out = [];
      if (agent) out.push(tr('kvmf.e.ciAgentShell'));
      if (keys.length) out.push(tr('kvmf.e.ciKeysShell'));
      return out;
    }
    const why = {
      flow: () => tr('kvmf.e.ciFlow', { key }),
      'not-list': () => tr('kvmf.e.ciNotList', { key }),
      duplicate: () => tr('kvmf.e.ciDuplicate', { key }),
      documents: () => tr('kvmf.e.ciDocuments'),
    }[merged.reason];
    return [why ? why() : tr('kvmf.e.ciDocuments')];
  }

  /** Disque de démarrage d'après les cartes : le plus petit ordre de
   *  démarrage (> 0), sinon la première carte. */
  function bootCard(cards) {
    const ordered = (cards || []).map((c, i) => ({ c, i, o: Number(c.boot_order) || 0 }));
    const withOrder = ordered.filter((x) => x.o > 0).sort((a, b) => a.o - b.o || a.i - b.i);
    return (withOrder[0] || ordered[0] || {}).c || null;
  }

  /** L'agent invité coché par défaut ? Non pour un démarrage sur une ISO
   *  (média d'installation : pas de cloud-init) ni pour un système Windows. */
  function agentDefault(cards, os) {
    const boot = bootCard(cards);
    if (boot && boot.source === 'iso') return false;
    return os !== 'windows';
  }

  const isCloudConfig = (text) => /^#cloud-config[ \t]*(\r?\n|$)/.test(String(text || ''));

  // -- sections ----------------------------------------------------------------------------
  const unmanagedFor = (id, list) => (list || []).filter((u) => {
    const s = String(u);
    if (/^(cputune|numatune|vcpupin|iothreadids|memoryBacking|cpu|vcpus)/.test(s)) return id === 'compute';
    if (/^devices\//.test(s)) return id === 'disks';
    return id === 'general';
  });

  function unmanagedHtml(id, ctx) {
    const items = ctx.mode === 'edit' ? unmanagedFor(id, ctx.form.unmanaged) : [];
    if (!items.length) return '';
    return `<div class="vm-edit-locked kvmf-unmanaged" data-unmanaged="${esc(id)}">${icon('lock')} ${esc(tr('kvmf.unmanaged'))}
        <ul>${items.map((u) => `<li><code>${esc(u)}</code></li>`).join('')}</ul></div>`;
  }

  function renderSection(id, ctx) {
    const f = (n) => fieldHtml(n, ctx);
    const row = (...names) => `<div class="ct-create-row">${names.map(f).join('')}</div>`;
    if (id === 'general') {
      return `<h3>${esc(tr('kvmf.s.general'))}</h3>
        ${ctx.mode === 'edit' ? row('name') : ''}
        ${row('title', 'os')}
        ${f('description')}
        ${row('on_poweroff', 'on_reboot', 'on_crash')}
        ${f('autostart')}
        ${unmanagedHtml(id, ctx)}`;
    }
    if (id === 'compute') {
      return `<h3>${esc(tr('kvmf.s.compute'))}</h3>
        ${row('cpu.vcpus', 'cpu.max_vcpus')}
        ${row('cpu.sockets', 'cpu.cores', 'cpu.threads')}
        ${row('memory.current_mib', 'memory.max_mib')}
        ${ctx.mode === 'create' ? f('cpu.hotplug') : ''}
        ${row('cpu.mode', 'cpu.model')}
        <p class="form-hint">${esc(tr('kvmf.computeHint'))}</p>
        ${unmanagedHtml(id, ctx)}`;
    }
    if (id === 'firmware') {
      return `<h3>${esc(tr('kvmf.s.firmware'))}</h3>
        ${row('machine', 'firmware')}
        <div class="kvmf-checks">${f('secure_boot')}${f('tpm')}</div>
        ${row('display.video', 'hotplug_slots')}
        <div class="kvmf-checks">${f('display.vnc')}${f('display.password')}${f('tablet')}${f('rng')}${ctx.mode === 'edit' ? f('guest_agent') : ''}${f('serial_console')}${f('memory.balloon')}</div>
        ${row('watchdog', 'sysinfo_serial')}
        <p class="form-hint">${esc(tr('kvmf.vncHint'))}</p>`;
    }
    if (id === 'disks') {
      const existing = ctx.mode === 'edit' ? `<div data-existing-wrap="disks">${existingDisksHtml(ctx)}</div>` : '';
      return `<h3>${esc(tr('kvmf.s.disks'))}</h3>
        ${existing}
        ${ctx.mode === 'edit' ? `<h4>${esc(tr('kvmf.addDisks'))}</h4>` : ''}
        ${imagesStateHtml(ctx)}
        <div class="vm-edit-cards" data-editor="kvm-disks">${TFForm.render(diskSchema(ctx), '', { disk: ctx.initialDisks || [] }, { hideHeader: true })}
          <p class="form-hint vm-disk-capacity" data-disk-capacity></p></div>
        <p class="form-hint">${esc(tr('kvmf.disksHint'))}</p>
        ${unmanagedHtml(id, ctx)}`;
    }
    if (id === 'network') {
      const existing = ctx.mode === 'edit' ? `<div data-existing-wrap="nics">${existingNicsHtml(ctx)}</div>` : '';
      return `<h3>${esc(tr('kvmf.s.network'))}</h3>
        ${existing}
        ${ctx.mode === 'edit' ? `<h4>${esc(tr('kvmf.addNics'))}</h4>` : ''}
        <div class="vm-edit-cards" data-editor="kvm-nics">${TFForm.render(nicSchema(ctx), '', { nic: ctx.initialNics || [] }, { hideHeader: true })}</div>
        <p class="form-hint">${esc(tr('kvmf.nicsHint'))}</p>`;
    }
    if (id === 'cloudinit') {
      const pools = (ctx.pools || []).filter((p) => p.state === 'running').map((p) => p.name);
      const ro = ctx.readOnly ? ' disabled' : '';
      return `<h3>${esc(tr('kvmf.s.cloudinit'))}</h3>
        <p class="form-hint">${esc(ctx.mode === 'edit' ? tr('kvmf.ciEditHint') : tr('kvmf.ciHint'))}</p>
        <div class="ct-create-row">
          ${ctx.mode === 'create' ? `<label class="bk-field"><span>${esc(tr('kvmf.f.seedPool'))}</span><select name="seed_pool" class="tip" data-tip="${esc(tr('kvmf.t.seedPool'))}"><option value="">${esc(tr('kvmf.firstDiskPool'))}</option>${pools.map((p) => `<option value="${esc(p)}">${esc(p)}</option>`).join('')}</select></label>` : ''}
          <label class="bk-field"><span>${esc(tr('kvmf.f.hostname'))}</span><input name="ci_hostname" maxlength="63" autocomplete="off" class="tip" data-tip="${esc(tr('kvmf.t.hostname'))}"${ro}></label>
        </div>
        ${ctx.readOnly ? '' : CloudInitForm.renderWizard('', { schema: CloudInitForm.userSchema({ harvesterKey: false }), netSchema: CloudInitForm.netSchemaByMac(), tips: true })}
        <div class="form-row"><label>${esc(tr('kvmf.f.userData'))}</label>
          <textarea class="yaml-editor tall tip" data-ci="userData" data-vmf-field="userData" spellcheck="false" data-tip="${esc(tr('kvmf.t.userData'))}"${ro}></textarea>
          ${ctx.mode === 'create' ? `<p class="form-hint res-warn" data-ci-agent-hint hidden>${icon('warn', 12)} ${esc(tr('kvmf.ciAgentHint'))}</p>` : ''}</div>
        <div class="form-row"><label>${esc(tr('kvmf.f.networkData'))}</label>
          <textarea class="yaml-editor tip" data-ci="networkData" spellcheck="false" data-tip="${esc(tr('kvmf.t.networkData'))}"${ro}></textarea></div>
        ${ctx.mode === 'edit' ? `<div class="bk-form-actions"><button type="button" class="btn btn-sm btn-secondary tip" data-ci-clear data-tip="${esc(tr('kvmf.ciClearTip'))}"${ro}>${icon('trash')} ${esc(tr('kvmf.ciClear'))}</button>
          <span class="form-hint" data-ci-cleared hidden>${esc(tr('kvmf.ciCleared'))}</span></div>
          <label class="bk-check tip" data-tip="${esc(tr('kvmf.t.rerun'))}"><input type="checkbox" name="ci_rerun"${ro}> <span>${esc(tr('kvmf.f.rerun'))}</span> <span class="kvmf-hot" data-hot="cloudinit"></span></label>
          <p class="form-hint" data-ci-state></p>` : ''}
        <div class="kvmf-confirm" data-ci-confirm hidden></div>`;
    }
    // revue
    return `<h3>${esc(tr('kvmf.s.review'))}</h3>
      <p class="form-hint">${esc(ctx.mode === 'edit' ? tr('kvmf.reviewEditHint') : tr('kvmf.reviewHint'))}</p>
      <div class="bk-form-actions"><button type="button" class="btn btn-sm btn-secondary tip" data-kvmf="preview" data-tip="${esc(tr('kvmf.previewTip'))}">${icon('refresh')} ${esc(tr('kvmf.preview'))}</button></div>
      <div data-kvmf="review"></div>`;
  }

  function existingDisksHtml(ctx) {
    const disks = (ctx.form.view.disks || []);
    if (!disks.length) return `<p class="form-hint">${esc(tr('kvmf.noDisk'))}</p>`;
    const isos = imageLists(ctx).isos;
    const seed = ctx.form.view.seed;
    const ro = ctx.readOnly ? ' disabled' : '';
    const buses = (ctx.opts && ctx.opts.disk_buses) || ['virtio', 'sata', 'scsi'];
    const opt = (list, cur, blank) => (blank ? `<option value="">${esc(tr('kvmf.none'))}</option>` : '')
      + list.map((v) => `<option value="${esc(v)}"${v === cur ? ' selected' : ''}>${esc(v)}</option>`).join('');
    return `<table class="data-table kvmf-existing" data-existing="disks"><thead><tr>
        <th>${esc(tr('kvmf.d.target'))}</th><th>${esc(tr('kvmf.d.source'))}</th><th>${esc(tr('kvmf.d.bus'))}</th>
        <th>${esc(tr('kvmf.d.cache'))}</th><th>${esc(tr('kvmf.d.bootOrder'))}</th><th>${esc(tr('kvmf.remove'))}</th></tr></thead><tbody>
      ${disks.map((d) => {
        const isSeed = seed && d.source && d.source.pool === seed.pool && d.source.volume === seed.volume;
        const src = d.source ? (d.source.pool ? `${d.source.pool}/${d.source.volume}` : (d.source.file || d.source.dev || '')) : '';
        const media = d.device === 'cdrom' && !isSeed
          ? `<select data-disk-field="source" class="tip" data-tip="${esc(tr('kvmf.t.media'))}"${ro}><option value="">${esc(tr('kvmf.emptyDrive'))}</option>${
            (src && !isos.includes(src) ? [src] : []).concat(isos).map((v) => `<option value="${esc(v)}"${v === src ? ' selected' : ''}>${esc(v)}</option>`).join('')}</select>
            <span class="kvmf-hot" data-hot="disks[].source"></span>`
          : `<span class="res-dim">${esc(src || '–')}</span>${isSeed ? ` ${`<span class="badge dim tip" data-tip="${esc(tr('kvmf.seedTip'))}">${esc(tr('kvmf.seed'))}</span>`}` : ''}`;
        return `<tr data-disk="${esc(d.target)}" data-device="${esc(d.device)}">
          <td><code>${esc(d.target)}</code> <span class="res-dim">${esc(d.device)}</span></td>
          <td>${media}</td>
          <td><select data-disk-field="bus" class="tip" data-tip="${esc(tr('kvmf.d.busTip'))}"${ro}>${opt(buses.includes(d.bus) ? buses : buses.concat([d.bus]), d.bus)}</select> <span class="kvmf-hot" data-hot="disks[].bus"></span></td>
          <td><select data-disk-field="cache" class="tip" data-tip="${esc(tr('kvmf.d.cacheTip'))}"${ro}>${opt(CACHE, d.cache, true)}</select></td>
          <td><input type="number" min="0" max="64" data-disk-field="boot_order" value="${esc(d.boot_order || 0)}" class="tip" data-tip="${esc(tr('kvmf.d.bootOrderTip'))}"${ro}></td>
          <td><label class="bk-check tip" data-tip="${esc(tr('kvmf.t.removeDisk'))}"><input type="checkbox" data-disk-remove${ro}> <span class="kvmf-hot" data-hot="disks[-]"${
            d.device === 'cdrom' || ['sata', 'ide'].includes(d.bus) ? ' data-hot-unplug="no"' : ''}></span></label></td></tr>`;
      }).join('')}</tbody></table>`;
  }

  function existingNicsHtml(ctx) {
    const nics = ctx.form.view.nics || [];
    if (!nics.length) return `<p class="form-hint">${esc(tr('kvmf.noNic'))}</p>`;
    const ro = ctx.readOnly ? ' disabled' : '';
    return `<table class="data-table kvmf-existing" data-existing="nics"><thead><tr>
        <th>${esc(tr('kvmf.n.mac'))}</th><th>${esc(tr('kvmf.n.kind'))}</th><th>${esc(tr('kvmf.n.model'))}</th>
        <th>${esc(tr('kvmf.d.bootOrder'))}</th><th>${esc(tr('kvmf.remove'))}</th></tr></thead><tbody>
      ${nics.map((n) => `<tr data-nic="${esc(n.mac || '')}">
          <td><code>${esc(n.mac || '?')}</code></td>
          <td>${esc(n.kind || '')} <span class="res-dim">${esc(n.source || '')}${n.mode ? ` (${esc(n.mode)})` : ''}</span></td>
          <td><select data-nic-field="model" class="tip" data-tip="${esc(tr('kvmf.n.modelTip'))}"${ro}>${(NIC_MODELS.includes(n.model) ? NIC_MODELS : NIC_MODELS.concat([n.model])).map((m) => `<option value="${esc(m)}"${m === n.model ? ' selected' : ''}>${esc(m)}</option>`).join('')}</select> <span class="kvmf-hot" data-hot="nics[].model"></span></td>
          <td><input type="number" min="0" max="64" data-nic-field="boot_order" value="${esc(n.boot_order || 0)}" class="tip" data-tip="${esc(tr('kvmf.n.bootOrderTip'))}"${ro}></td>
          <td><label class="bk-check tip" data-tip="${esc(tr('kvmf.t.removeNic'))}"><input type="checkbox" data-nic-remove${ro}> <span class="kvmf-hot" data-hot="nics[-]"></span></label></td></tr>`).join('')}</tbody></table>`;
  }

  // -- règles « maintenant / prochain démarrage » (HOTNESS servie) ------------------------
  /** Ce que deviendrait un changement de `field` : now, next-boot ou refused,
   *  d'après la table servie et l'état de la VM (le serveur a le dernier mot :
   *  l'aperçu de la revue rend sa classification exacte). */
  function hotness(field, form) {
    const state = form.state;
    if (form.persistent === false) return { applied: 'refused', reason: tr('kvmf.transient') };
    if (!LIVE.includes(state) && state !== 'shut off') return { applied: 'refused', reason: tr('kvmf.notNow', { state }) };
    if (CREATE_ONLY.includes(field)) return { applied: 'refused', reason: tr('kvmf.createOnly') };
    const rule = (form.hotness || []).find((r) => r.field === field);
    if (field === 'name') {
      if (LIVE.includes(state)) return { applied: 'refused', reason: rule && rule.note };
      if (form.snapshots) return { applied: 'refused', reason: tr('kvmf.renameSnapshots') };
      return { applied: 'now', reason: '' };
    }
    if (!LIVE.includes(state)) return { applied: 'now', reason: tr('kvmf.stoppedNow') };
    if (!rule) return { applied: 'next-boot', reason: '' };
    return { applied: rule.running, reason: rule.note || rule.command || '' };
  }

  function refreshHot(root, form) {
    root.querySelectorAll('[data-hot]').forEach((el) => {
      let h = hotness(el.dataset.hot, form);
      // un lecteur SATA ou de CD-ROM ne se débranche pas à chaud (QEMU) :
      // retiré au prochain démarrage, comme le classe kvm_edit
      if (el.dataset.hotUnplug === 'no' && h.applied === 'now' && LIVE.includes(form.state)) {
        h = { applied: 'next-boot', reason: tr('kvmf.noUnplug') };
      }
      el.innerHTML = VMForm.appliedBadge(h.applied, h.reason, { forecast: true });
    });
  }

  // =========================================================================================
  // Fenêtre « Créer une VM » / « Modifier »
  // =========================================================================================
  /** Ouvre la fenêtre de création ; `host` préchoisi (bloc d'un hôte),
   *  `opts.image` = {pool, volume, iso} préremplit le premier disque (menu
   *  d'une image du catalogue, comme withImageDisk côté Harvester). */
  function openCreate(host, opts = {}) {
    return openWindow({ mode: 'create', host: host || '', image: opts.image || null });
  }

  function openEdit(host, domain, opts = {}) {
    return openWindow({ mode: 'edit', host, domain, tab: opts.tab });
  }

  function headHtml(ctx) {
    if (ctx.mode === 'edit') {
      return `<div class="kvmf-edit-head">
          <span>${icon('vm')} <strong>${esc(ctx.domain)}</strong> <span class="res-dim">${esc(ctx.host)}</span> <span data-kvmf="state"></span></span>
          <div data-kvmf="banner"></div><div data-kvmf="refreshed"></div></div>`;
    }
    return `<form class="capi-form vm-create-head kvmf-head" autocomplete="off" novalidate>
        <fieldset>
          <legend>${esc(tr('kvmf.identity'))}</legend>
          <label>${esc(tr('kvmf.f.host'))} *
            <select name="host" required class="tip" data-tip="${esc(tr('kvmf.t.host'))}"></select></label>
          <label>${esc(tr('kvmf.f.name'))} *
            <input name="name" required maxlength="200" value="kvm-01" class="tip" data-tip="${esc(tr('kvmf.t.newName'))}" data-vmf-field="name">
            <span class="form-hint">${esc(tr('kvmf.nameHint'))}</span></label>
          <label>${esc(tr('kvmf.f.count'))}
            <input name="count" type="number" min="1" max="20" value="1" class="tip" data-tip="${esc(tr('kvmf.t.count'))}" data-vmf-field="count">
            <span class="form-hint">${esc(tr('kvmf.countHint'))}</span></label>
          <label class="vm-create-start tip" data-tip="${esc(tr('kvmf.t.start'))}">
            <input name="start" type="checkbox" checked> <span>${esc(tr('kvmf.f.start'))}</span></label>
          <label class="vm-create-start tip" data-tip="${esc(tr('kvmf.t.headAgent'))}">
            <input name="head_agent" type="checkbox" checked> <span>${esc(tr('kvmf.f.guestAgent'))}</span></label>
          <label style="grid-column:span 2;">${esc(tr('kvmf.f.sshKeys'))}
            <select name="ssh_keys" multiple size="2" class="tip" data-tip="${esc(tr('kvmf.t.sshKeys'))}"></select></label>
          <label style="grid-column:span 2;">${esc(tr('kvmf.f.sshExtra'))}
            <textarea name="ssh_extra" rows="2" spellcheck="false" class="tip" data-tip="${esc(tr('kvmf.t.sshExtra'))}" data-vmf-field="ssh_extra"></textarea></label>
          <label>${esc(tr('kvmf.f.template'))}
            <select name="template" class="tip" data-tip="${esc(tr('kvmf.t.template'))}"><option value="">${esc(tr('kvmf.noTemplate'))}</option></select></label>
        </fieldset>
      </form>`;
  }

  function actionsHtml(ctx) {
    if (ctx.mode === 'edit') {
      return `<button type="button" class="btn btn-primary btn-sm btn-ico tip" data-kvmf="apply" data-tip="${esc(tr('kvmf.applyTip'))}">${icon('ok')} ${esc(tr('kvmf.apply'))}</button>
        <button type="button" class="btn btn-secondary btn-sm btn-ico tip" data-kvmf="review-go" data-tip="${esc(tr('kvmf.reviewGoTip'))}">${icon('preview')} ${esc(tr('kvmf.reviewGo'))}</button>
        <button type="button" class="btn btn-secondary btn-sm btn-ico tip" data-kvmf="reload" data-tip="${esc(tr('kvmf.reloadTip'))}">${icon('refresh')} ${esc(tr('kvmf.reload'))}</button>`;
    }
    return `<button type="button" class="btn btn-primary btn-sm btn-ico tip" data-kvmf="create" data-tip="${esc(tr('kvmf.createTip'))}">${icon('add')} ${esc(tr('kvmf.create'))}</button>
      <button type="button" class="btn btn-secondary btn-sm btn-ico tip" data-kvmf="review-go" data-tip="${esc(tr('kvmf.reviewGoTip'))}">${icon('preview')} ${esc(tr('kvmf.reviewGo'))}</button>
      <button type="button" class="btn btn-secondary btn-sm btn-ico tip" data-kvmf="save-template" data-tip="${esc(tr('kvmf.saveTemplateTip'))}">${icon('bundle')} ${esc(tr('kvmf.saveTemplate'))}</button>
      <span class="kvmf-tpl" data-kvmf="tpl" hidden><input name="tpl_name" maxlength="64" class="tip" data-tip="${esc(tr('kvmf.t.tplName'))}" placeholder="${esc(tr('kvmf.tplName'))}">
        <button type="button" class="btn btn-sm btn-primary tip" data-kvmf="tpl-yes" data-tip="${esc(tr('kvmf.tplYesTip'))}">${esc(tr('kvmf.tplYes'))}</button>
        <button type="button" class="btn btn-sm btn-secondary tip" data-kvmf="tpl-no" data-tip="${esc(tr('kvm.confirmNoTip'))}">${esc(tr('kvm.confirmNo'))}</button></span>`;
  }

  function openWindow(ctx) {
    if (!window.FloatingPanels || !window.VMForm) return null;
    const id = ctx.mode === 'edit' ? `kvm-edit-${ctx.host}-${enc(ctx.domain)}` : 'kvm-create';
    const title = ctx.mode === 'edit' ? tr('kvmf.editTitle', { name: ctx.domain, host: ctx.host }) : tr('kvmf.createTitle');
    const existing = document.getElementById(`fp-${id}`);
    if (existing) {
      const el = FloatingPanels.open({ id, icon: 'vm', title }).el;
      if (ctx.mode === 'create' && el.__kvmfRetarget) el.__kvmfRetarget({ host: ctx.host, image: ctx.image });
      return el;
    }
    const panel = FloatingPanels.open({
      id, icon: 'vm', title, width: 1040, height: 720,
      bodyHtml: `<div class="kvmf" data-kvmf-mode="${esc(ctx.mode)}">${VMForm.shellHtml({ head: headHtml(ctx), sections: sections(), actions: actionsHtml(ctx), cls: 'kvmf-shell' })}
        <div class="kvmf-confirm" data-kvmf="retarget" hidden></div>
        <div class="kvmf-errors" data-kvmf="errors"></div>
        <ul class="kvm-steps" data-kvmf="steps"></ul></div>`,
    });
    const root = panel.el;
    root.querySelector('.vm-edit-content').innerHTML = `<p class="form-hint">${esc(tr('common.loading'))}</p>`;
    const result = root.querySelector('[data-result]');
    const say = (html, bad) => VMForm.say(result, html, bad);
    const sayText = (text, bad) => say(esc(text), bad);
    const steps = root.querySelector('[data-kvmf="steps"]');
    const errorsEl = root.querySelector('[data-kvmf="errors"]');
    const st = { ctx, form: null, base: null, keys: new Map(), busy: false, timer: null, loadedCi: null };

    // -- références de l'hôte ---------------------------------------------------------
    /** Références de l'hôte. Un changement d'hôte pendant la lecture rend
     *  la réponse périmée : elle est ignorée (jeton de requête), seule la
     *  dernière demande remplit la fenêtre.
     *
     *  Le catalogue d'images n'est PAS attendu : il demande un relevé du
     *  stockage de l'hôte (plusieurs secondes sur un hôte chargé, à froid).
     *  La fenêtre s'affiche dès les autres références ; les choix d'images
     *  arrivent ensuite dans l'onglet Disques, avec leur propre indicateur
     *  de chargement et leur erreur (`onImages`). */
    async function loadRefs(host) {
      const token = (st.refToken = (st.refToken || 0) + 1);
      ctx.images = [];
      ctx.imagesState = 'loading';
      fillImageLists(ctx, []);
      const imagesP = getJson(`/api/kvm/hosts/${enc(host)}/images`);
      const [caps, pools, nets, links] = await Promise.all([
        getJson(`/api/kvm/hosts/${enc(host)}/caps`), getJson(`/api/kvm/hosts/${enc(host)}/pools`),
        getJson(`/api/kvm/hosts/${enc(host)}/networks`), getJson(`/api/kvm/hosts/${enc(host)}/links`)]);
      if (token !== st.refToken) { const e = new Error('stale'); e.stale = true; throw e; }
      if (caps.error) throw new Error(caps.error);
      ctx.caps = caps.options || {};
      ctx.role = caps.role || 'viewer';
      ctx.pools = pools.pools || [];
      ctx.networks = nets.networks || [];
      ctx.links = links.error ? {} : links;
      ctx.refWarnings = [pools, nets, links].filter((x) => x.error).map((x) => x.error);
      st.imagesP = imagesP.then((images) => { if (token === st.refToken) onImages(images); });
    }

    /** Catalogue d'images arrivé : listes remplies en place, listes
     *  déroulantes déjà affichées refaites (la valeur choisie est gardée),
     *  indicateur retiré ou remplacé par l'erreur. */
    function onImages(images) {
      const failed = !images || images.error;
      ctx.images = failed ? [] : (images.images || []);
      ctx.imagesState = failed ? 'error' : 'ready';
      ctx.imagesError = failed ? ((images && images.error) || '?') : null;
      if (failed) ctx.refWarnings = (ctx.refWarnings || []).concat([ctx.imagesError]);
      fillImageLists(ctx, ctx.images);
      const l = imageLists(ctx);
      const opts = (list, cur, blank) => blank + (cur && !list.includes(cur) ? [cur] : []).concat(list)
        .map((v) => `<option value="${esc(v)}"${v === cur ? ' selected' : ''}>${esc(v)}</option>`).join('');
      const lists = { image: l.imgs, iso: l.isos, volume: l.vols };
      root.querySelectorAll('[data-editor="kvm-disks"] select').forEach((sel) => {
        const m = /\.(image|iso|volume)$/.exec(sel.name || '');
        if (!m) return;
        const cur = sel.value;
        // une valeur gardée qui n'est plus au catalogue reste visible
        sel.innerHTML = opts(lists[m[1]], cur, sel.required ? '' : '<option value=""></option>');
      });
      root.querySelectorAll('select[data-disk-field="source"]').forEach((sel) => {
        const cur = sel.value;
        sel.innerHTML = opts(l.isos, cur, `<option value="">${esc(tr('kvmf.emptyDrive'))}</option>`);
      });
      const box = root.querySelector('[data-kvmf="images-state"]');
      if (box) box.outerHTML = imagesStateHtml(ctx);
      if (ctx.mode === 'create' && st.onImagesCreate) st.onImagesCreate();
    }

    function setFamily(fam) {
      ctx.family = fam && (ctx.caps.by_machine || {})[fam] ? fam : (ctx.caps.default_machine || 'q35');
      ctx.opts = (ctx.caps.by_machine || {})[ctx.family] || {};
    }

    /** MAC des cartes de la VM, dans l'ordre de l'onglet Réseau, pour le
     *  network-config de l'assistant (qui désigne chaque carte par sa MAC).
     *  Modification : les cartes existantes. Création : une carte sans MAC en
     *  reçoit une tirée par la console (contrôlée contre les cartes de
     *  l'hôte), écrite dans la carte et donc envoyée avec le spec. */
    async function cardMacs() {
      if (ctx.mode === 'edit') return (ctx.form.view.nics || []).map((n) => n.mac).filter(Boolean);
      const count = parseInt((root.querySelector('.kvmf-head [name="count"]') || {}).value, 10) || 1;
      if (count > 1) throw new Error(tr('kvmf.e.ciNetMulti'));
      const items = [...root.querySelectorAll('[data-editor="kvm-nics"] .tf-block[data-block="nic"] .tf-block-item')];
      const inputs = items.map((it) => it.querySelector('[name$=".mac"]')).filter(Boolean);
      const empty = inputs.filter((i) => !i.value.trim());
      if (empty.length) {
        const d = await getJson(`/api/kvm/hosts/${enc(ctx.host)}/macs?count=${empty.length}`);
        if (d.error || !Array.isArray(d.macs) || d.macs.length !== empty.length) {
          throw new Error(tr('kvmf.e.ciNetMacs', { error: errText(d.error || '?') }));
        }
        empty.forEach((i, k) => {
          i.value = d.macs[k];
          i.dispatchEvent(new Event('input', { bubbles: true }));
          i.dispatchEvent(new Event('change', { bubbles: true }));
        });
      }
      return inputs.map((i) => i.value.trim().toLowerCase());
    }

    // -- agent invité (création) : coché par défaut sauf démarrage sur ISO ou
    // Windows, tant que la personne n'a pas touché la case ----------------------------
    function diskCards() {
      const ed = root.querySelector('[data-editor="kvm-disks"] .tf-form');
      try { return ed ? (TFForm.read(ed, diskSchema(ctx), { emitEmptyLists: true }).disk || []) : []; } catch { return []; }
    }
    function syncAgent(cards) {
      const box = root.querySelector('.kvmf-head [name="head_agent"]');
      if (!box) return;
      if (!st.agentTouched) {
        const os = (root.querySelector('[data-vmf-field="os"] [name="os"]') || {}).value;
        box.checked = agentDefault(cards || diskCards(), os);
      }
      updateAgentHint();
    }
    /** Un user-data qui n'est pas un #cloud-config (script shell) passe tel
     *  quel case décochée ; case cochée, la fenêtre dit pourquoi. */
    function updateAgentHint() {
      const hint = root.querySelector('[data-ci-agent-hint]');
      const box = root.querySelector('.kvmf-head [name="head_agent"]');
      const u = (root.querySelector('[data-ci="userData"]') || {}).value || '';
      if (hint) hint.hidden = !(box && box.checked && u.trim() && !isCloudConfig(u));
    }

    // -- construction des onglets ------------------------------------------------------
    let form = null;
    function build() {
      root.querySelector('.vm-edit-content').innerHTML = '';
      const adapter = {
        id: 'kvm', sections: sections(),
        renderSection: (sid) => renderSection(sid, ctx),
        wireSection: (sid, el) => wireSection(sid, el),
        // un résultat affiché dans la revue n'est jamais remplacé par un aperçu
        onShow: (sid) => { if (sid === 'review' && !st.keepReview) preview(); },
      };
      form = VMForm.mount(root, adapter, { eager: true });
      st.mount = form;
    }

    function wireSection(sid, el) {
      if (sid === 'disks') {
        const ed = el.querySelector('[data-editor="kvm-disks"] .tf-form');
        const schema = diskSchema(ctx);
        wireCards(ed, schema, 'disk', () => {
          let cards = [];
          try { cards = TFForm.read(ed, schema, { emitEmptyLists: true }).disk || []; } catch { /* carte incomplète */ }
          renderCapacity(el.querySelector('[data-disk-capacity]'), ctx, cards);
          if (ctx.mode === 'create') syncAgent(cards);
        });
      }
      if (sid === 'network') wireCards(el.querySelector('[data-editor="kvm-nics"] .tf-form'), nicSchema(ctx), 'nic');
      if (sid === 'general' && ctx.mode === 'create') {
        el.querySelector('[name="os"]')?.addEventListener('change', () => syncAgent());
      }
      if (sid === 'cloudinit' && ctx.mode === 'create') {
        el.querySelector('[data-ci="userData"]').addEventListener('input', () => updateAgentHint());
      }
      if (sid === 'cloudinit' && ctx.mode === 'edit') {
        // « Vider » : seul moyen d'envoyer un user-data vide à la place d'un
        // user-data existant (le serveur exige clear)
        const ud = el.querySelector('[data-ci="userData"]');
        const mark = el.querySelector('[data-ci-cleared]');
        st.ciCleared = false;
        el.querySelector('[data-ci-clear]')?.addEventListener('click', () => { ud.value = ''; st.ciCleared = true; mark.hidden = false; });
        ud.addEventListener('input', () => { if (ud.value.trim()) { st.ciCleared = false; mark.hidden = true; } });
      }
      if (sid === 'cloudinit' && !ctx.readOnly) {
        CloudInitForm.wireWizard(el, '', {
          schema: CloudInitForm.userSchema({ harvesterKey: false }),
          netSchema: CloudInitForm.netSchemaByMac(),
          nicMacs: () => cardMacs(),
          tips: true,
          confirmReplace: () => confirmInPage(el.querySelector('[data-ci-confirm]'), tr('kvmf.ciReplace')),
        });
      }
      if (sid === 'review') el.querySelector('[data-kvmf="preview"]').addEventListener('click', () => preview());
      if (sid === 'firmware') {
        const m = el.querySelector('[name="machine"]');
        if (m && ctx.mode === 'create') m.addEventListener('change', () => { const keep = readSpecSafe(); setFamily(m.value); rebuildKeeping(keep); });
      }
      if (sid === 'compute') {
        const mode = el.querySelector('[name="cpu.mode"]');
        const model = el.querySelector('[data-vmf-field="cpu.model"]');
        const sync = () => { if (model) model.style.display = mode && mode.value !== 'custom' ? 'none' : ''; };
        if (mode) mode.addEventListener('change', sync);
        sync();
      }
    }

    /** Confirmation dans la page (jamais la boîte du navigateur) : rend une promesse. */
    function confirmInPage(box, text) {
      return new Promise((resolve) => {
        box.hidden = false;
        box.innerHTML = `<div class="bkg-confirm kvm-confirm" role="group" aria-label="${esc(text)}"><span>${esc(text)}</span>
            <button type="button" class="btn btn-sm btn-primary tip" data-c="yes" data-tip="${esc(tr('kvmf.replaceYesTip'))}">${esc(tr('kvmf.replaceYes'))}</button>
            <button type="button" class="btn btn-sm btn-secondary tip" data-c="no" data-tip="${esc(tr('kvm.confirmNoTip'))}">${esc(tr('kvm.confirmNo'))}</button></div>`;
        const done = (v) => { box.hidden = true; box.innerHTML = ''; resolve(v); };
        box.querySelector('[data-c="yes"]').addEventListener('click', () => done(true));
        box.querySelector('[data-c="no"]').addEventListener('click', () => done(false));
        box.querySelector('[data-c="no"]').focus();
      });
    }

    function readSpecSafe() {
      try { return collect({ lenient: true }).spec; } catch { return null; }
    }

    /** Empreinte de la saisie (spec, cartes, cloud-init, clés collées) : une
     *  fenêtre « touchée » demande avant d'être remplacée. */
    function snapshot() {
      const spec = readSpecSafe() || {};
      const q = (sel) => (root.querySelector(sel) || {}).value || '';
      return JSON.stringify([spec, spec._cards || null, q('[data-ci="userData"]'), q('[data-ci="networkData"]'),
        q('.kvmf-head [name="ssh_extra"]'), q('.kvmf-head [name="name"]'), q('.kvmf-head [name="count"]')]);
    }

    /** Changement de type de machine : les choix (micrologiciel, bus, vidéo,
     *  modèles CPU) suivent l'hôte ; la saisie est gardée. */
    function rebuildKeeping(spec) {
      const cur = form.current();
      const ci = { u: root.querySelector('[data-ci="userData"]').value, n: root.querySelector('[data-ci="networkData"]').value };
      ctx.initialDisks = spec ? spec._cards.disks : ctx.initialDisks;
      ctx.initialNics = spec ? spec._cards.nics : ctx.initialNics;
      build();
      if (spec) fillFields(root, spec);
      writeControl(root.querySelector('[name="machine"]'), ctx.family);
      root.querySelector('[data-ci="userData"]').value = ci.u;
      root.querySelector('[data-ci="networkData"]').value = ci.n;
      form.show(cur || 'general');
    }

    // -- lecture du formulaire -----------------------------------------------------------
    /** Le spec de la fenêtre et les erreurs de saisie évidentes (le serveur
     *  reste juge : refus de check_spec affichés par la revue). */
    function collect(o = {}) {
      const errors = [];
      const v = readFields(root);
      const spec = {};
      Object.entries(v).forEach(([k, val]) => {
        if (ctx.mode === 'create' && k === 'name') return;
        if (val === null || val === undefined) return;
        if (k === 'watchdog' && val === '') return;
        setPath(spec, k, val);
      });
      if (spec.cpu && spec.cpu.mode !== 'custom') delete spec.cpu.model;
      const dEd = root.querySelector('[data-editor="kvm-disks"] .tf-form');
      const nEd = root.querySelector('[data-editor="kvm-nics"] .tf-form');
      const dCards = dEd ? (TFForm.read(dEd, diskSchema(ctx), { emitEmptyLists: true }).disk || []) : [];
      const nCards = nEd ? (TFForm.read(nEd, nicSchema(ctx), { emitEmptyLists: true }).nic || []) : [];
      const disks = dCards.map((c, i) => diskItem(c, errors, i));
      const nics = nCards.map((c, i) => nicItem(c, errors, i));
      Object.defineProperty(spec, '_cards', { value: { disks: dCards, nics: nCards }, enumerable: false });
      const cpu = spec.cpu || {};
      const mem = spec.memory || {};
      if (cpu.vcpus && cpu.max_vcpus && cpu.vcpus > cpu.max_vcpus) errors.push({ section: 'compute', field: 'cpu.vcpus', message: tr('kvmf.e.vcpus') });
      if (mem.current_mib && mem.max_mib && mem.current_mib > mem.max_mib) errors.push({ section: 'compute', field: 'memory.current_mib', message: tr('kvmf.e.memory') });
      if (ctx.mode === 'create') {
        const head = root.querySelector('.kvmf-head');
        const name = head.querySelector('[name="name"]').value.trim();
        if (!name) errors.push({ section: 'general', field: 'name', message: tr('kvmf.e.name') });
        else if (!NAME_RE.test(name)) errors.push({ section: 'general', field: 'name', message: tr('kvmf.e.badName') });
        spec.name = name;
        const count = parseInt(head.querySelector('[name="count"]').value, 10) || 1;
        if (count < 1 || count > 20) errors.push({ section: 'general', field: 'count', message: tr('kvmf.e.count') });
        if (count > 1) spec.instances = count;
        spec.start = head.querySelector('[name="start"]').checked;
        if (!cpu.hotplug && cpu.sockets && cpu.cores && cpu.threads && cpu.max_vcpus
            && cpu.sockets * cpu.cores * cpu.threads !== cpu.max_vcpus) {
          errors.push({ section: 'compute', field: 'cpu.sockets', message: tr('kvmf.e.topology', { n: cpu.max_vcpus }) });
        }
        if (cpu.hotplug) { delete cpu.sockets; delete cpu.cores; delete cpu.threads; }
        if (!disks.length) errors.push({ section: 'disks', message: tr('kvmf.e.noDisk') });
        spec.disks = disks;
        spec.nics = nics;
        const agent = head.querySelector('[name="head_agent"]').checked;
        spec.guest_agent = agent || !!spec.guest_agent;
        const { keys, bad } = chosenKeys(head, st.keys);
        bad.forEach((k) => errors.push({ section: 'cloudinit', field: 'ssh_extra', message: tr('kvmf.e.sshKey', { key: k.slice(0, 24) }) }));
        const u = root.querySelector('[data-ci="userData"]').value;
        const n = root.querySelector('[data-ci="networkData"]').value;
        const merged = CloudInitForm.withKeysAndAgent(u, keys, agent);
        if (merged.skipped) ciRefusals(merged, keys, agent).forEach((m) => errors.push({ section: 'cloudinit', field: 'userData', message: m }));
        if (merged.text.trim() || n.trim()) {
          const seedPool = root.querySelector('[name="seed_pool"]').value
            || (disks.find((d) => d.source && d.source.pool) || { source: {} }).source.pool || null;
          spec.cloudinit = { user_data: merged.text, network_data: n.trim() ? n : null, seed_pool: seedPool };
          const hn = root.querySelector('[name="ci_hostname"]').value.trim();
          if (hn) spec.cloudinit.hostname = hn;
        }
      } else {
        spec.disks = disks;
        spec.nics = nics;
      }
      if (errors.length && !o.lenient) {
        const e = new Error('invalid');
        e.errors = errors;
        e.spec = spec;
        throw e;
      }
      return { spec, errors };
    }

    /** Modification : seuls les champs changés depuis l'ouverture partent
     *  (spec partiel de kvm_xml.changes), disques et cartes en listes
     *  complètes dès que l'un d'eux change. */
    function editSpec() {
      const errors = [];
      const now = readFields(root);
      const spec = {};
      Object.keys(now).forEach((k) => {
        if (JSON.stringify(now[k]) !== JSON.stringify(st.base.fields[k])) setPath(spec, k, now[k]);
      });
      // disques existants
      let disksChanged = false;
      const kept = [];
      root.querySelectorAll('[data-existing="disks"] tr[data-disk]').forEach((row) => {
        const target = row.dataset.disk;
        const old = (st.form.view.disks || []).find((d) => d.target === target) || {};
        if (row.querySelector('[data-disk-remove]').checked) { disksChanged = true; return; }
        const d = { target };
        const bus = row.querySelector('[data-disk-field="bus"]').value;
        const cache = row.querySelector('[data-disk-field="cache"]').value || null;
        const boot = Number(row.querySelector('[data-disk-field="boot_order"]').value) || null;
        if (bus !== old.bus) { d.bus = bus; disksChanged = true; }
        if (cache !== (old.cache || null)) { d.cache = cache; disksChanged = true; }
        if (boot !== (old.boot_order || null)) { d.boot_order = boot; disksChanged = true; }
        const media = row.querySelector('[data-disk-field="source"]');
        if (media) {
          const have = old.source && old.source.pool ? `${old.source.pool}/${old.source.volume}` : '';
          if (media.value !== have) {
            disksChanged = true;
            if (media.value) {
              const [p, ...rest] = media.value.split('/');
              d.source = { kind: 'iso', pool: p, volume: rest.join('/') };
            } else d.source = { kind: 'empty' };
          }
        }
        kept.push(d);
      });
      const dEd = root.querySelector('[data-editor="kvm-disks"] .tf-form');
      const added = (TFForm.read(dEd, diskSchema(ctx), { emitEmptyLists: true }).disk || []).map((c, i) => diskItem(c, errors, i));
      if (disksChanged || added.length) spec.disks = kept.concat(added);
      // cartes existantes
      let nicsChanged = false;
      const keptN = [];
      root.querySelectorAll('[data-existing="nics"] tr[data-nic]').forEach((row) => {
        const mac = row.dataset.nic;
        const old = (st.form.view.nics || []).find((n) => n.mac === mac) || {};
        if (row.querySelector('[data-nic-remove]').checked) { nicsChanged = true; return; }
        const n = { mac };
        const model = row.querySelector('[data-nic-field="model"]').value;
        const boot = Number(row.querySelector('[data-nic-field="boot_order"]').value) || null;
        if (model !== old.model) { n.model = model; nicsChanged = true; }
        if (boot !== (old.boot_order || null)) { n.boot_order = boot; nicsChanged = true; }
        keptN.push(n);
      });
      const nEd = root.querySelector('[data-editor="kvm-nics"] .tf-form');
      const addedN = (TFForm.read(nEd, nicSchema(ctx), { emitEmptyLists: true }).nic || []).map((c, i) => nicItem(c, errors, i));
      if (nicsChanged || addedN.length) spec.nics = keptN.concat(addedN);
      // cloud-init : seulement s'il a été lu puis changé, ou relancé
      const u = root.querySelector('[data-ci="userData"]').value;
      const nd = root.querySelector('[data-ci="networkData"]').value;
      const rerun = root.querySelector('[name="ci_rerun"]').checked;
      const hn = root.querySelector('[name="ci_hostname"]').value.trim();
      if (st.loadedCi && (u !== st.loadedCi.user_data || nd !== st.loadedCi.network_data || rerun || hn)) {
        spec.cloudinit = { user_data: u, network_data: nd || null, rerun };
        if (hn) spec.cloudinit.hostname = hn;
        if (!u.trim() && st.loadedCi.user_data.trim()) {
          if (st.ciCleared) { spec.cloudinit.user_data = ''; spec.cloudinit.clear = true; }
          else errors.push({ section: 'cloudinit', field: 'userData', message: tr('kvmf.e.ciEmpty') });
        }
      }
      const cpu = Object.assign({}, st.form.view.cpu, spec.cpu || {});
      const mem = Object.assign({}, st.form.view.memory, spec.memory || {});
      if (cpu.vcpus > cpu.max_vcpus) errors.push({ section: 'compute', field: 'cpu.vcpus', message: tr('kvmf.e.vcpus') });
      if (mem.current_mib > mem.max_mib) errors.push({ section: 'compute', field: 'memory.current_mib', message: tr('kvmf.e.memory') });
      if (spec.name !== undefined && !NAME_RE.test(spec.name || '')) errors.push({ section: 'general', field: 'name', message: tr('kvmf.e.badName') });
      return { spec, errors };
    }

    // -- revue (aperçu du serveur) ----------------------------------------------------
    async function preview() {
      const box = root.querySelector('[data-kvmf="review"]');
      if (!box) return null;
      let spec;
      try {
        if (ctx.mode === 'create') spec = collect().spec;
        else {
          const r = editSpec();
          if (r.errors.length) { const e = new Error('invalid'); e.errors = r.errors; throw e; }
          spec = r.spec;
        }
      } catch (e) {
        if (e.errors) { VMForm.showErrors(root, e.errors, errorsEl); box.innerHTML = `<p class="res-error">${esc(tr('kvmf.fixErrors'))}</p>`; return null; }
        throw e;
      }
      VMForm.showErrors(root, [], errorsEl);
      box.innerHTML = `<p class="form-hint">${esc(tr('kvmf.previewing'))}</p>`;
      try {
        if (ctx.mode === 'create') {
          const d = await send('POST', `/api/kvm/hosts/${enc(ctx.host)}/vm-plan`, { spec });
          const p = d.plan || {};
          box.innerHTML = VMForm.reviewHtml({
            refusals: p.refusals || [], warnings: (p.warnings || []).concat(ctx.refWarnings || []),
            body: (p.instances || []).map((i) => `<div class="kvmf-instance" data-instance="${esc(i.name)}"><strong>${icon('vm', 12)} ${esc(i.name)}</strong>
                <ul>${(i.volumes || []).map((v) => `<li>${esc(v.target)} : ${esc(v.pool)}/${esc(v.volume)} <span class="res-dim">(${esc(v.kind)}, ${esc(bytes(v.capacity))})</span></li>`).join('')}
                ${i.seed ? `<li>${esc(tr('kvmf.seed'))} : ${esc(i.seed.pool)}/${esc(i.seed.volume)}</li>` : ''}</ul></div>`).join(''),
            xml: ((p.instances || [])[0] || {}).xml_preview || '',
          });
          return d;
        }
        const d = await send('POST', `/api/kvm/vms/${enc(ctx.host)}/${enc(ctx.domain)}/edit-plan`, { spec, expect_config: st.form.config_digest });
        const p = d.plan || {};
        box.innerHTML = VMForm.reviewHtml({ refusals: p.refusals || [], body: VMForm.changesHtml(p.changes || []) });
        return d;
      } catch (e) {
        if (e.status === 409 && e.data && e.data.kind === 'changed') { onChanged(); box.innerHTML = ''; return null; }
        box.innerHTML = `<p class="res-error">${icon('fail')} ${esc(e.message)}</p>`;
        return null;
      }
    }

    function stepLine(s) {
      let li = [...steps.children].find((x) => x.dataset.step === (s.step_id || ''));
      if (!li) { li = document.createElement('li'); li.dataset.step = s.step_id || ''; steps.appendChild(li); }
      const stt = s.status || 'running';
      li.className = `kvm-step is-${stt}`;
      const cls = { done: 'ok', ok: 'ok', running: 'info', warn: 'warn', error: 'fail', skipped: 'dim' }[stt] || 'info';
      li.innerHTML = `<span class="badge ${cls}">${esc(s.step_id || stt)}</span> ${esc(s.message || '')}`;
    }

    // -- créer ------------------------------------------------------------------------
    async function create() {
      if (st.busy) return;
      let spec;
      try { spec = collect().spec; } catch (e) {
        if (!e.errors) throw e;
        VMForm.showErrors(root, e.errors, errorsEl);
        sayText(tr('kvmf.fixErrors'), true);
        form.show(e.errors[0].section);
        return;
      }
      VMForm.showErrors(root, [], errorsEl);
      st.busy = true;
      steps.innerHTML = '';
      sayText(tr('kvmf.sending'));
      try {
        const out = await send('POST', `/api/kvm/vms/${enc(ctx.host)}`, { spec });
        say(`${esc(tr('kvmf.started'))} <code>${esc(out.action_id)}</code> ${esc((out.names || []).join(', '))}`);
        follow(out.action_id, stepLine, (ok, run) => {
          st.busy = false;
          const o = (run.result && run.result.output) || {};
          const made = (o.created || []).map((c) => (typeof c === 'string' ? c : c.name));
          if (ok) say(esc(tr('kvmf.created', { names: made.join(', ') || '?' })) + ((o.warnings || []).length ? ` <span class="res-dim">${esc(o.warnings.join(' · '))}</span>` : ''));
          else {
            const left = (o.leftovers || []).map((x) => (typeof x === 'string' ? x : `${x.pool || ''}/${x.volume || x.name || ''}`));
            say(`${esc(o.error || run.error_summary || tr('kvmf.failed'))}${made.length ? ` · ${esc(tr('kvmf.createdSome', { names: made.join(', ') }))}` : ''}${
              left.length ? ` · ${esc(tr('kvmf.leftovers', { list: left.join(', ') }))}` : ''}`, true);
          }
          if (window.KVM && KVM.load) KVM.load(true);
        });
      } catch (e) {
        st.busy = false;
        sayText(e.message, true);
      }
    }

    // -- modèles ----------------------------------------------------------------------
    async function loadTemplates() {
      const sel = root.querySelector('[name="template"]');
      const d = await getJson('/api/kvm/templates');
      (d.templates || []).filter((t) => !t.error).forEach((t) => {
        sel.insertAdjacentHTML('beforeend', `<option value="${esc(t.name)}">${esc(t.name)}</option>`);
      });
      sel.addEventListener('change', () => applyTemplate(sel.value));
    }

    async function applyTemplate(name) {
      if (!name) return;
      const d = await getJson(`/api/kvm/templates/${enc(name)}`);
      if (d.error) { sayText(d.error, true); return; }
      const spec = d.spec || {};
      if (spec.machine) setFamily(spec.machine);
      ctx.initialDisks = (spec.disks || []).map(cardOf);
      ctx.initialNics = (spec.nics || []).map(nicCardOf);
      build();
      fillFields(root, spec);
      writeControl(root.querySelector('[name="machine"]'), ctx.family);
      if (spec.cloudinit && spec.cloudinit.network_data) root.querySelector('[data-ci="networkData"]').value = spec.cloudinit.network_data;
      root.querySelector('[name="cpu.mode"]')?.dispatchEvent(new Event('change'));
      syncAgent();
      form.show('general');
      say(`${esc(tr('kvmf.templateLoaded'))} <code>${esc(name)}</code>${(d.removed || []).length ? ` <span class="res-dim">${esc(tr('kvmf.templateRemoved', { list: d.removed.join(', ') }))}</span>` : ''}`);
    }

    async function saveTemplate(name) {
      let spec;
      try { spec = collect().spec; } catch (e) {
        if (!e.errors) throw e;
        VMForm.showErrors(root, e.errors, errorsEl);
        return;
      }
      try {
        const out = await send('POST', '/api/kvm/templates', { name, spec });
        follow(out.action_id, () => {}, (ok, run) => {
          const o = (run.result && run.result.output) || {};
          if (ok) say(`${esc(tr('kvmf.templateSaved'))} <code>${esc(name)}</code>${(o.removed || []).length ? ` <span class="res-dim">${esc(tr('kvmf.templateRemoved', { list: o.removed.join(', ') }))}</span>` : ''}`);
          else sayText(o.error || run.error_summary || tr('kvmf.failed'), true);
        });
      } catch (e) { sayText(e.message, true); }
    }

    // -- modifier ---------------------------------------------------------------------
    /** Valeur d'un champ telle que la fenêtre la lirait (readControl après
     *  writeControl), pour comparer base, saisie et nouvelle vue. */
    function norm(name, v) {
      const t = FIELDS[name].type;
      if (t === 'check') return !!v;
      if (v == null || v === '') return null;
      return t === 'int' ? Number(v) : String(v);
    }
    const srcOf = (f) => Object.assign({}, f.view, { autostart: !!f.autostart, name: f.name || ctx.domain });
    function fieldsOf(f) {
      const src = srcOf(f);
      const out = {};
      Object.keys(st.base.fields).forEach((name) => { out[name] = norm(name, getPath(src, name)); });
      return out;
    }
    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    /** Saisie des tableaux de disques et de cartes existants (pour savoir
     *  s'ils ont été touchés depuis l'ouverture). */
    function tablesOf() {
      const out = {};
      ['disks', 'nics'].forEach((k) => {
        out[k] = [...root.querySelectorAll(`[data-existing="${k}"] input, [data-existing="${k}"] select`)]
          .map((el) => (el.type === 'checkbox' ? el.checked : el.value));
      });
      return out;
    }
    const ciOf = () => ({ u: root.querySelector('[data-ci="userData"]').value, n: root.querySelector('[data-ci="networkData"]').value });

    /** Ce qui a changé ailleurs depuis la base de la fenêtre : {remote:
     *  [champs], conflicts: [champs aussi modifiés ici]}. */
    function remoteChanges(f) {
      const base = st.base.fields;
      const now = readFields(root);
      const next = fieldsOf(f);
      const remote = Object.keys(next).filter((k) => !same(next[k], base[k]));
      const devs = [['disks', 'disks'], ['nics', 'nics']].filter(([k]) => !same((f.view || {})[k], (st.form.view || {})[k])).map(([k]) => k);
      const tables = tablesOf();
      const conflicts = remote.filter((k) => !same(now[k], base[k]))
        .concat(devs.filter((k) => !same(tables[k], st.base.tables[k])));
      return { remote: remote.concat(devs), conflicts, next };
    }
    const fieldLabel = (k) => (k === 'disks' ? tr('kvmf.s.disks') : k === 'nics' ? tr('kvmf.s.network')
      : FIELDS[k] ? FIELDS[k].label() : k);

    /** La VM a changé ailleurs : bandeau avec les champs touchés (ceux aussi
     *  modifiés ici sont marqués) et « Relire ». L'empreinte n'est PAS
     *  remplacée ici : seule la relecture l'accepte. */
    function showChanged(f) {
      const ban = root.querySelector('[data-kvmf="banner"]');
      ban.querySelector('.kvmf-changed')?.remove();
      const rc = f && f.view ? remoteChanges(f) : { remote: [], conflicts: [] };
      const list = rc.remote.map((k) => `<li data-remote-field="${esc(k)}"${rc.conflicts.includes(k) ? ' class="res-warn"' : ''}>${esc(fieldLabel(k))}${
        rc.conflicts.includes(k) ? ` <span class="res-dim">(${esc(tr('kvmf.changedConflict'))})</span>` : ''}</li>`).join('');
      ban.insertAdjacentHTML('beforeend', `<div class="bkg-confirm kvmf-changed" role="alert">${icon('warn')} <span>${esc(tr('kvmf.changed'))}</span>
          ${list ? `<ul class="kvmf-remote">${list}</ul>` : ''}
          <button type="button" class="btn btn-sm btn-primary tip" data-kvmf="reload-now" data-tip="${esc(tr('kvmf.reloadTip'))}">${icon('refresh')} ${esc(tr('kvmf.reload'))}</button></div>`);
      sayText(tr('kvmf.changed'), true);
    }

    /** Refus 409 `changed` (aperçu ou enregistrement) : la nouvelle vue est
     *  lue pour nommer les champs changés, sans être acceptée. */
    async function onChanged() {
      const d = await getJson(`/api/kvm/vms/${enc(ctx.host)}/${enc(ctx.domain)}/form`);
      showChanged(d.error ? null : d);
    }

    /** Relecture à trois voies (base, saisie, nouvelle vue) : un champ non
     *  touché prend la nouvelle valeur, un champ modifié ici garde la saisie ;
     *  les tableaux de disques et de cartes sont refaits s'ils n'ont pas été
     *  touchés ; le cloud-init est relu s'il n'a pas été modifié. La nouvelle
     *  empreinte devient la base de la modification suivante. */
    async function reloadForm() {
      const d = await getJson(`/api/kvm/vms/${enc(ctx.host)}/${enc(ctx.domain)}/form`);
      if (d.error) { sayText(errText(d.error), true); return false; }
      const rc = remoteChanges(d);
      const now = readFields(root);
      const tables = tablesOf();
      const ci = ciOf();
      const ciUntouched = !st.loadedCi || (ci.u === st.loadedCi.user_data && ci.n === st.loadedCi.network_data);
      Object.keys(rc.next).forEach((name) => {
        if (same(now[name], st.base.fields[name]) && !same(rc.next[name], st.base.fields[name])) {
          const el = root.querySelector(`[data-vmf-field="${CSS.escape(name)}"] [name="${CSS.escape(name)}"]`);
          if (el) writeControl(el, rc.next[name]);
        }
      });
      const oldView = st.form.view;
      st.form = d;
      ctx.form = d;
      st.base.fields = rc.next;
      ['disks', 'nics'].forEach((k) => {
        const wrap = root.querySelector(`[data-existing-wrap="${k}"]`);
        if (wrap && !same((d.view || {})[k], (oldView || {})[k]) && same(tables[k], st.base.tables[k])) {
          wrap.innerHTML = k === 'disks' ? existingDisksHtml(ctx) : existingNicsHtml(ctx);
        }
      });
      st.base.tables = tablesOf();
      root.querySelector('[name="cpu.mode"]')?.dispatchEvent(new Event('change'));
      updateEditState();
      root.querySelector('[data-kvmf="banner"] .kvmf-changed')?.remove();
      if (ciUntouched) { st.loadedCi = null; loadCloudInit(); }
      const kept = rc.conflicts.map(fieldLabel);
      const refreshed = rc.remote.filter((k) => !rc.conflicts.includes(k)).map(fieldLabel);
      const box = root.querySelector('[data-kvmf="refreshed"]');
      if (box) {
        box.innerHTML = rc.remote.length ? `<div class="vmf-restart kvmf-refreshed" role="status">${icon('refresh')} ${
          esc(tr('kvmf.refreshedFields', { list: refreshed.join(', ') || '–' }))}${
          kept.length ? ` <span class="res-warn">${esc(tr('kvmf.keptFields', { list: kept.join(', ') }))}</span>` : ''}</div>` : '';
      }
      sayText(tr('kvmf.reloaded'));
      return true;
    }

    function updateEditState() {
      const f = st.form;
      root.querySelector('[data-kvmf="state"]').innerHTML = `<span class="badge ${LIVE.includes(f.state) ? 'ok' : 'dim'}">${esc(window.KVM && KVM.stateLabel ? KVM.stateLabel(f.state) : f.state)}</span>`;
      const ban = root.querySelector('[data-kvmf="banner"]');
      const changed = ban.querySelector('.kvmf-changed');
      notePending(ctx.host, ctx.domain, f.pending, f.id);
      const restart = VMForm.restartBanner(f.pending || [], `<button type="button" class="btn btn-sm btn-secondary tip" data-kvmf="cycle" data-tip="${esc(tr('kvmf.cycleTip'))}">${icon('restart')} ${esc(tr('kvmf.cycle'))}</button>`);
      const ro = ctx.readOnly ? `<div class="vmf-readonly res-error" role="status">${icon('lock')} ${esc(ctx.readOnly)}</div>` : '';
      ban.innerHTML = ro + restart;
      if (changed) ban.appendChild(changed);
      refreshHot(root, f);
    }

    async function apply() {
      if (st.busy) return;
      const { spec, errors } = editSpec();
      if (errors.length) { VMForm.showErrors(root, errors, errorsEl); sayText(tr('kvmf.fixErrors'), true); form.show(errors[0].section); return; }
      VMForm.showErrors(root, [], errorsEl);
      if (!Object.keys(spec).length) { sayText(tr('kvmf.nothing')); return; }
      st.busy = true;
      steps.innerHTML = '';
      sayText(tr('kvmf.sending'));
      try {
        const out = await send('PATCH', `/api/kvm/vms/${enc(ctx.host)}/${enc(ctx.domain)}`, { spec, expect_config: st.form.config_digest });
        follow(out.action_id, stepLine, (ok, run) => {
          st.busy = false;
          const o = (run.result && run.result.output) || {};
          const changes = o.changes || [];
          const restart = !!o.restart_required;
          const after = o.after || {};
          if (after.pending) notePending(ctx.host, ctx.domain, after.pending, after.id);
          if (ok) {
            say(`${esc(tr('kvmf.applied'))}${restart ? ` · ${esc(tr('vmf.restartNeeded'))}` : ''}`);
            const box = root.querySelector('[data-kvmf="review"]');
            if (box) box.innerHTML = VMForm.changesHtml(changes);
            showResult();
            // les champs relus deviennent la nouvelle base : un second
            // « Appliquer » ne rejoue rien
            reopen();
          } else {
            const kind = o.kind || '';
            if (kind === 'changed') onChanged();
            else say(`${esc(o.error || run.error_summary || tr('kvmf.failed'))}`, true);
            const box = root.querySelector('[data-kvmf="review"]');
            if (box && changes.length) { box.innerHTML = VMForm.changesHtml(changes); showResult(); }
          }
          if (window.KVM && KVM.load) KVM.load(true);
        });
      } catch (e) {
        st.busy = false;
        if (e.status === 409 && e.data && e.data.kind === 'changed') { onChanged(); return; }
        sayText(e.message, true);
      }
    }

    /** Après une modification réussie : relecture complète (nouvelle base). */
    async function reopen() {
      const d = await getJson(`/api/kvm/vms/${enc(ctx.host)}/${enc(ctx.domain)}/form`);
      if (d.error) {
        // la base et l'empreinte restent celles d'avant : le dire, et
        // proposer la relecture plutôt qu'un second envoi sur une base fausse
        say(`${esc(tr('kvmf.reopenFailed', { error: errText(d.error) }))}`, true);
        showChanged(null);
        return;
      }
      const review = root.querySelector('[data-kvmf="review"]').innerHTML;
      st.loadedCi = null;          // le seed a pu changer : relu
      st.form = d;
      ctx.form = d;
      initEditFields();
      root.querySelector('[data-kvmf="review"]').innerHTML = review;
      showResult();
    }

    /** Montre le résultat dans l'onglet Revue sans relancer d'aperçu. */
    function showResult() {
      st.keepReview = true;
      try { form.show('review'); } finally { st.keepReview = false; }
    }

    /** Arrêt puis démarrage (gestes de la 1.96.0) : le processus QEMU est
     *  recréé et prend les changements en attente. */
    async function cycle(btn) {
      btn.disabled = true;
      const run = (g) => new Promise((resolve) => {
        send('POST', `/api/kvm/vms/${enc(ctx.host)}/${enc(ctx.domain)}/${g}`, {}).then((out) => {
          follow(out.action_id, stepLine, (ok, r) => resolve({ ok, o: (r.result && r.result.output) || {}, r }));
        }).catch((e) => resolve({ ok: false, o: { error: e.message }, r: {} }));
      });
      sayText(tr('kvmf.cycling'));
      const a = await run('shutdown');
      if (!a.ok || (a.o.state && a.o.state !== 'shut off')) {
        sayText(a.o.error || a.r.error_summary || tr('kvmf.cycleStuck'), true);
        btn.disabled = false;
        return;
      }
      const b = await run('start');
      await reloadForm();
      if (b.ok) sayText(tr('kvmf.cycled')); else sayText(b.o.error || b.r.error_summary || tr('kvmf.failed'), true);
      if (window.KVM && KVM.load) KVM.load(true);
    }

    function initEditFields() {
      const v = st.form.view;
      ctx.readOnly = st.form.persistent === false ? tr('kvmf.transient') : '';
      setFamily(v.machine_family);
      ctx.initialDisks = [];
      ctx.initialNics = [];
      build();
      // le type de machine garde sa version : la liste propose la valeur
      // actuelle et la famille (la dernière version au prochain démarrage)
      fillFields(root, Object.assign({}, v, { autostart: !!st.form.autostart, name: st.form.name || ctx.domain }));
      root.querySelector('[name="cpu.mode"]')?.dispatchEvent(new Event('change'));
      if (ctx.readOnly) root.querySelectorAll('.vm-edit-content input, .vm-edit-content select, .vm-edit-content textarea').forEach((el) => { el.disabled = true; });
      st.base = { fields: readFields(root), tables: tablesOf() };
      root.querySelector('[data-kvmf="refreshed"]').innerHTML = '';
      updateEditState();
      if (st.loadedCi) {
        root.querySelector('[data-ci="userData"]').value = st.loadedCi.user_data;
        root.querySelector('[data-ci="networkData"]').value = st.loadedCi.network_data;
      } else loadCloudInit();
    }

    async function loadCloudInit() {
      const stEl = root.querySelector('[data-ci-state]');
      if ((ROLE_RANK[ctx.role] || 0) < ROLE_RANK.operator) { if (stEl) stEl.textContent = tr('kvm.operatorOnly'); return; }
      const d = await getJson(`/api/kvm/vms/${enc(ctx.host)}/${enc(ctx.domain)}/cloudinit`);
      if (d.error) { if (stEl) stEl.textContent = errText(d.error); return; }
      st.loadedCi = { user_data: d.user_data || '', network_data: d.network_data || '' };
      root.querySelector('[data-ci="userData"]').value = st.loadedCi.user_data;
      root.querySelector('[data-ci="networkData"]').value = st.loadedCi.network_data;
      if (stEl) stEl.textContent = d.seed ? tr('kvmf.ciLoaded', { volume: `${d.seed.pool}/${d.seed.volume}` }) : tr('kvmf.ciNone');
    }

    // -- démarrage de la fenêtre --------------------------------------------------------
    root.addEventListener('click', (e) => {
      const b = e.target.closest('[data-kvmf]');
      if (!b || b.disabled) return;
      const a = b.dataset.kvmf;
      if (a === 'create') create();
      else if (a === 'apply') apply();
      else if (a === 'review-go') form && form.show('review');
      else if (a === 'reload' || a === 'reload-now') reloadForm();
      else if (a === 'cycle') cycle(b);
      else if (a === 'save-template') {
        const box = root.querySelector('[data-kvmf="tpl"]');
        box.hidden = false;
        const inp = box.querySelector('[name="tpl_name"]');
        if (!inp.value) inp.value = `${(root.querySelector('.kvmf-head [name="name"]').value || 'vm').trim()}-template`;
        inp.focus();
      } else if (a === 'tpl-no') root.querySelector('[data-kvmf="tpl"]').hidden = true;
      else if (a === 'tpl-yes') {
        const name = root.querySelector('[name="tpl_name"]').value.trim();
        if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(name)) { sayText(tr('kvmf.e.tplName'), true); return; }
        root.querySelector('[data-kvmf="tpl"]').hidden = true;
        saveTemplate(name);
      }
    });

    (async () => {
      try {
        if (ctx.mode === 'create') {
          const head = root.querySelector('.kvmf-head');
          const sel = head.querySelector('[name="host"]');
          const d = await getJson('/api/kvm/hosts?declared=1');
          const hosts = (d.hosts || []).map((h) => h.name);
          if (!hosts.length) throw new Error(tr('kvm.noHosts'));
          if (!ctx.host || !hosts.includes(ctx.host)) ctx.host = hosts[0];
          sel.innerHTML = hosts.map((h) => `<option value="${esc(h)}"${h === ctx.host ? ' selected' : ''}>${esc(h)}</option>`).join('');
          loadKeys(head.querySelector('[name="ssh_keys"]'), st.keys);
          loadTemplates();
          const start = async () => {
            st.onImagesCreate = null;
            await loadRefs(ctx.host);
            setFamily(ctx.caps.default_machine);
            ctx.defaultPool = ((ctx.pools || []).find((p) => p.state === 'running') || {}).name || '';
            const img = ctx.image;
            ctx.initialDisks = img ? [{ source: img.iso ? 'iso' : 'image', device: img.iso ? 'cdrom' : 'disk',
              [img.iso ? 'iso' : 'image']: `${img.pool}/${img.volume}`, pool: img.pool, linked: true, boot_order: 1 }]
              : [(() => {
                // une image du catalogue (même pool : disque lié), sinon un disque vierge
                const first = (ctx.images || []).find((i) => !i.iso && (ctx.pools || []).some((p) => p.name === i.pool && p.state === 'running'));
                return first ? { source: 'image', image: `${first.pool}/${first.volume}`, pool: first.pool, linked: true, boot_order: 1 }
                  : { source: 'blank', pool: ctx.defaultPool, size: '10G', boot_order: 1 };
              })()];
            if (img && img.iso) ctx.initialDisks.push({ source: 'blank', pool: img.pool, size: '10G', boot_order: 2 });
            // catalogue encore en route : le premier disque deviendra l'image
            // du catalogue à son arrivée, si rien n'a été saisi d'ici là
            const autoImage = !img && ctx.initialDisks[0].source === 'blank';
            const net = (ctx.networks || []).find((n) => n.active) || (ctx.networks || [])[0];
            ctx.initialNics = net ? [{ kind: 'network', network: net.name, model: 'virtio' }] : [];
            build();
            const o = ctx.opts;
            fillFields(root, { os: 'linux', machine: ctx.family, firmware: 'bios', cpu: { vcpus: 1, max_vcpus: 1, sockets: 1, cores: 1, threads: 1, mode: o.default_cpu_mode || 'host-model', model: o.host_model || '' },
              memory: { current_mib: 1024, max_mib: 1024, balloon: true }, display: { vnc: true, password: true, video: o.default_video || 'virtio' },
              tablet: true, rng: true, serial_console: true, guest_agent: true, hotplug_slots: ctx.family === 'q35' ? 4 : 0,
              on_poweroff: 'destroy', on_reboot: 'restart', on_crash: 'destroy', watchdog: '' });
            root.querySelector('[name="cpu.mode"]')?.dispatchEvent(new Event('change'));
            syncAgent();
            form.show('general');
            st.pristine = snapshot();
            if (autoImage) {
              st.onImagesCreate = () => {
                st.onImagesCreate = null;
                const first = (ctx.images || []).find((i) => !i.iso && (ctx.pools || []).some((p) => p.name === i.pool && p.state === 'running'));
                if (!first || snapshot() !== st.pristine) return;
                const card = root.querySelector('[data-editor="kvm-disks"] .tf-block-item');
                const g = (n) => card && card.querySelector(`[name$=".${n}"]`);
                if (!g('source') || !g('image')) return;
                g('source').value = 'image';
                if (g('pool')) g('pool').value = first.pool;
                g('image').value = `${first.pool}/${first.volume}`;
                if (g('linked')) g('linked').checked = true;
                if (g('size')) g('size').value = '';
                g('source').dispatchEvent(new Event('change', { bubbles: true }));
                syncAgent();
                st.pristine = snapshot();
              };
              if (ctx.imagesState !== 'loading') st.onImagesCreate();
            }
            if ((ROLE_RANK[ctx.role] || 0) < ROLE_RANK.operator) {
              root.querySelector('[data-kvmf="create"]').disabled = true;
              root.querySelector('[data-kvmf="create"]').dataset.tip = tr('kvm.operatorOnly');
            }
          };
          sel.addEventListener('change', async () => {
            // même hôte : rien à relire (une relecture reconstruirait les
            // onglets et perdrait la saisie)
            if (sel.value === ctx.host) return;
            ctx.host = sel.value;
            root.querySelector('.vm-edit-content').innerHTML = `<p class="form-hint">${esc(tr('common.loading'))}</p>`;
            try { await start(); } catch (e) { if (!e.stale) sayText(e.message, true); }
          });
          head.querySelector('[name="head_agent"]').addEventListener('change', () => { st.agentTouched = true; updateAgentHint(); });
          // fenêtre déjà ouverte rouverte avec un autre hôte ou une image :
          // appliqué tout de suite, ou après accord si une saisie serait perdue
          root.__kvmfRetarget = async (next) => {
            const host = next.host && hosts.includes(next.host) ? next.host : ctx.host;
            const image = next.image || null;
            if (host === ctx.host && !image) return;
            if (st.pristine && snapshot() !== st.pristine) {
              const ok = await confirmInPage(root.querySelector('[data-kvmf="retarget"]'), tr('kvmf.discardTyped', { host }));
              if (!ok) return;
            }
            ctx.host = host;
            ctx.image = image;
            st.agentTouched = false;
            sel.value = host;
            root.querySelector('.vm-edit-content').innerHTML = `<p class="form-hint">${esc(tr('common.loading'))}</p>`;
            try { await start(); } catch (e) { if (!e.stale) sayText(e.message, true); }
          };
          try { await start(); } catch (e) { if (!e.stale) throw e; }
        } else {
          const d = await getJson(`/api/kvm/vms/${enc(ctx.host)}/${enc(ctx.domain)}/form`);
          if (d.error) throw new Error(errText(d.error));
          st.form = d;
          ctx.form = d;
          await loadRefs(ctx.host);
          ctx.role = d.role || ctx.role;
          initEditFields();
          form.show(ctx.tab || 'general');
          if ((ROLE_RANK[ctx.role] || 0) < ROLE_RANK.operator || ctx.readOnly) {
            const ap = root.querySelector('[data-kvmf="apply"]');
            ap.disabled = true;
            ap.dataset.tip = ctx.readOnly || tr('kvm.operatorOnly');
          }
          // l'état de la VM suit (repères « maintenant / prochain démarrage »)
          st.timer = setInterval(async () => {
            if (!root.isConnected) { clearInterval(st.timer); return; }
            if (document.hidden || st.busy) return;
            const f = await getJson(`/api/kvm/vms/${enc(ctx.host)}/${enc(ctx.domain)}/form`);
            if (f.error || !st.form) return;
            // l'empreinte n'est JAMAIS remplacée en silence : seul « Relire » le
            // fait ; un changement fait ailleurs est annoncé tout de suite
            const elsewhere = f.config_digest && f.config_digest !== st.form.config_digest;
            st.form = Object.assign({}, st.form, { state: f.state, pending: f.pending, snapshots: f.snapshots, id: f.id });
            ctx.form = st.form;
            updateEditState();
            if (elsewhere && st.announced !== f.config_digest) { st.announced = f.config_digest; showChanged(f); }
          }, POLL.ms);
        }
      } catch (e) {
        root.querySelector('.vm-edit-content').innerHTML = `<p class="res-error">${icon('fail')} ${esc(e.message)}</p>`;
        sayText(e.message, true);
      }
    })();
    return root;
  }

  /** Spec d'un disque -> valeurs d'une carte (modèles enregistrés). */
  function cardOf(d) {
    const s = d.source || {};
    const c = { device: d.device || 'disk', bus: d.bus, target: d.target, boot_order: d.boot_order || 0, cache: d.cache,
                serial: d.serial, readonly: !!d.readonly, shareable: !!d.shareable, source: s.kind || 'blank', pool: s.pool };
    if (s.kind === 'image') { c.image = `${s.image_pool || s.pool}/${s.image}`; c.linked = s.linked !== false; }
    if (s.kind === 'iso') c.iso = `${s.pool}/${s.volume}`;
    if (s.kind === 'volume') c.volume = `${s.pool}/${s.volume}`;
    if (s.size_bytes) c.size = sizeText(s.size_bytes);
    return c;
  }
  function nicCardOf(n) {
    const c = { kind: n.kind || 'network', model: n.model, mac: n.mac, boot_order: n.boot_order || 0, mode: n.mode };
    c[n.kind === 'bridge' ? 'bridge' : n.kind === 'direct' ? 'dev' : 'network'] = n.source;
    return c;
  }

  // =========================================================================================
  // Gestes à chaud du menu d'une VM (groupe « Disques et réseau »)
  // =========================================================================================
  const HOT_ICON = { edit: 'edit', cpumem: 'compute', 'add-volume': 'hotplug', 'remove-volume': 'trash', 'insert-cdrom': 'cdrom',
                     'eject-image': 'eject', 'add-nic': 'network', 'remove-nic': 'trash', cloudinit: 'cloud' };

  /** Pourquoi un geste est grisé, d'après la table servie (`edit_operations`). */
  function hotWhy(table, op, v, role) {
    const t = table && table[op === 'edit' ? 'edit' : 'hot'];
    if (!t) return tr('kvm.op.unavailable');
    if ((ROLE_RANK[role] || 0) < (ROLE_RANK[t.role] || 3)) return t.role === 'admin' ? tr('kvm.adminOnly') : tr('kvm.operatorOnly');
    if (t.persistent && !v.persistent) return tr('kvm.why.transient');
    if (t.states && !t.states.includes(v.state)) return tr('kvm.why.needsState', { state: KVM.stateLabel(v.state), need: t.states.map(KVM.stateLabel).join(', ') });
    return '';
  }

  /** Le groupe du menu : rendu tout de suite (Modifier, CPU et mémoire,
   *  ajouts), complété par les disques et cartes de la VM une fois lus. */
  function menuGroupHtml(v, table, role) {
    const item = (op, label, tip, extra = '') => {
      const why = hotWhy(table, op, v, role);
      return `<button type="button" class="vma-item tip" role="menuitem" data-kvm-hot="${esc(op)}"${extra}
          data-tip="${esc(why || tip)}"${why ? ' aria-disabled="true"' : ''}><span class="vma-ic">${icon(HOT_ICON[op])}</span> ${esc(label)}</button>`;
    };
    return `<div class="vma-group kvm-hot-group" data-kvm-hot-group>
        ${item('edit', tr('kvmf.m.edit'), tr('kvmf.m.editTip'))}
        ${item('cpumem', tr('kvmf.m.cpumem'), tr('kvmf.m.cpumemTip'))}
        ${item('add-volume', tr('kvmf.m.addVolume'), tr('kvmf.m.addVolumeTip'))}
        ${item('add-nic', tr('kvmf.m.addNic'), tr('kvmf.m.addNicTip'))}
        ${item('cloudinit', tr('kvmf.m.cloudinit'), tr('kvmf.m.cloudinitTip'))}
        <div data-kvm-hot-devices><span class="res-dim kvm-hot-loading">${esc(tr('common.loading'))}</span></div></div>`;
  }

  async function fillMenuDevices(groupEl, v, table, role) {
    const d = await getJson(`/api/kvm/vms/${enc(v.host)}/${enc(v.name)}`);
    const box = groupEl.querySelector('[data-kvm-hot-devices]');
    if (!box) return;
    if (d.error) { box.innerHTML = `<span class="res-error vma-item">${esc(errText(d.error))}</span>`; return; }
    const item = (op, label, tip, attrs) => {
      const why = hotWhy(table, op, v, role);
      return `<button type="button" class="vma-item tip" role="menuitem" data-kvm-hot="${esc(op)}"${attrs}
          data-tip="${esc(why || tip)}"${why ? ' aria-disabled="true"' : ''}><span class="vma-ic">${icon(HOT_ICON[op])}</span> ${esc(label)}</button>`;
    };
    const out = [];
    (d.disks || []).forEach((x) => {
      const t = x.target || '';
      if (!t) return;
      if (x.device === 'cdrom') {
        out.push(item('insert-cdrom', tr('kvmf.m.insert', { target: t }), tr('kvmf.m.insertTip'), ` data-target="${esc(t)}"`));
        if (x.source) out.push(item('eject-image', tr('kvmf.m.eject', { target: t }), tr('kvmf.m.ejectTip'), ` data-target="${esc(t)}"`));
      } else out.push(item('remove-volume', tr('kvmf.m.removeVolume', { target: t }), tr('kvmf.m.removeVolumeTip'), ` data-target="${esc(t)}"`));
    });
    (d.nics || []).forEach((x) => {
      if (x.mac) out.push(item('remove-nic', tr('kvmf.m.removeNic', { mac: x.mac }), tr('kvmf.m.removeNicTip'), ` data-mac="${esc(x.mac)}"`));
    });
    box.innerHTML = out.join('');
  }

  /** Clic dans le groupe : rend vrai s'il a été traité. */
  function onMenuClick(e, v, close) {
    const b = e.target.closest('[data-kvm-hot]');
    if (!b) return false;
    if (b.getAttribute('aria-disabled')) return true;
    close();
    const op = b.dataset.kvmHot;
    if (op === 'edit') openEdit(v.host, v.name);
    else openHot(v, op, { target: b.dataset.target, mac: b.dataset.mac });
    return true;
  }

  const hfield = (name, label, input, tip) => `<label class="bk-field"><span>${esc(label)}</span>${
    input.replace(/^<(input|select|textarea)/, `<$1 class="tip" data-tip="${esc(tip)}"`)}</label>`;

  /** La fenêtre d'un geste à chaud (même patron que vm-actions.js). */
  function openHot(v, op, arg = {}) {
    const titles = {
      cpumem: tr('kvmf.m.cpumem'), 'add-volume': tr('kvmf.m.addVolume'), 'add-nic': tr('kvmf.m.addNic'),
      'remove-volume': tr('kvmf.m.removeVolume', { target: arg.target }), 'insert-cdrom': tr('kvmf.m.insert', { target: arg.target }),
      'eject-image': tr('kvmf.m.eject', { target: arg.target }), 'remove-nic': tr('kvmf.m.removeNic', { mac: arg.mac }),
      cloudinit: tr('kvmf.m.cloudinit'),
    };
    const panel = FloatingPanels.open({
      id: `kvm-hot-${op}-${v.host}-${enc(v.name)}`, icon: HOT_ICON[op], width: op === 'cloudinit' ? 760 : 520,
      height: op === 'cloudinit' ? 680 : 460, title: `${titles[op]} · ${v.name}`,
      bodyHtml: `<form class="of-form vma-form kvm-hot" data-kvm-hot-op="${esc(op)}" autocomplete="off" novalidate>
          <p class="form-hint" data-h="hint">${esc(tr('common.loading'))}</p><div data-h="fields"></div>
          <div class="bk-form-actions"><button type="submit" class="btn btn-sm ${/^remove|eject/.test(op) ? 'btn-danger' : 'btn-primary'} tip" data-h="go"
            data-tip="${esc(tr('kvmf.hotGoTip'))}">${icon(HOT_ICON[op])} ${esc(tr('kvmf.hotGo'))}</button></div>
          <div class="of-msg" data-h="msg" role="status" aria-live="polite"></div><ul class="kvm-steps" data-h="steps"></ul><div data-h="result"></div></form>`,
    });
    const root = panel.el;
    if (root.dataset.hotReady) return root;
    root.dataset.hotReady = '1';
    const formEl = root.querySelector('form');
    const $ = (k) => root.querySelector(`[data-h="${k}"]`);
    const hint = (t) => { $('hint').textContent = t; };
    const st = { body: null };

    (async () => {
      const base = `/api/kvm/hosts/${enc(v.host)}`;
      if (op === 'cpumem') {
        const f = await getJson(`/api/kvm/vms/${enc(v.host)}/${enc(v.name)}/form`);
        const c = (f.view && f.view.cpu) || {}, m = (f.view && f.view.memory) || {};
        hint(tr('kvmf.h.cpumem', { cpu: c.max_vcpus || '?', mem: m.max_mib || '?' }));
        $('fields').innerHTML = hfield('vcpus', tr('kvmf.f.vcpus'), `<input name="vcpus" type="number" min="1" max="${esc(c.max_vcpus || '')}" value="${esc(c.vcpus || v.vcpus || 1)}">`, tr('kvmf.t.hotVcpus'))
          + hfield('memory_mib', tr('kvmf.f.memory'), `<input name="memory_mib" type="number" min="16" max="${esc(m.max_mib || '')}" value="${esc(m.current_mib || Math.round((v.memory_kib || 0) / 1024))}">`, tr('kvmf.t.hotMemory'));
        st.body = () => ({ vcpus: Number(formEl.vcpus.value) || undefined, memory_mib: Number(formEl.memory_mib.value) || undefined });
      } else if (op === 'add-volume' || op === 'insert-cdrom') {
        const [pools, images] = await Promise.all([getJson(`${base}/pools`), getJson(`${base}/images`)]);
        const act = (pools.pools || []).filter((p) => p.state === 'running');
        const imgs = (images.images || []).filter((i) => (op === 'insert-cdrom' ? i.iso : !i.iso));
        const opt = (list) => list.map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join('');
        if (op === 'insert-cdrom') {
          hint(tr('kvmf.h.insert'));
          $('fields').innerHTML = hfield('iso', tr('kvmf.d.iso'), `<select name="iso">${imgs.length ? opt(imgs.map((i) => `${i.pool}/${i.volume}`)) : `<option value="" disabled selected>${esc(tr('kvmf.noIso'))}</option>`}</select>`, tr('kvmf.d.isoTip'));
          st.body = () => { const [p, ...r] = formEl.iso.value.split('/'); return { target: arg.target, pool: p, volume: r.join('/') }; };
        } else {
          hint(tr('kvmf.h.addVolume'));
          $('fields').innerHTML = hfield('source', tr('kvmf.d.source'), `<select name="source"><option value="blank">blank</option><option value="image">image</option></select>`, tr('kvmf.d.sourceTip'))
            + hfield('pool', tr('kvmf.d.pool'), `<select name="pool">${opt(act.map((p) => p.name))}</select>`, tr('kvmf.d.poolTip'))
            + hfield('image', tr('kvmf.d.image'), `<select name="image"><option value=""></option>${opt(imgs.map((i) => `${i.pool}/${i.volume}`))}</select>`, tr('kvmf.h.linkedOnly'))
            + hfield('size', tr('kvmf.d.size'), '<input name="size" value="1G" maxlength="20">', tr('kvmf.d.sizeTip'))
            + hfield('bus', tr('kvmf.d.bus'), `<select name="bus">${opt(['virtio', 'scsi', 'sata'])}</select>`, tr('kvmf.d.busTip'));
          st.body = () => {
            const errs = [];
            const c = { source: formEl.source.value, pool: formEl.pool.value, image: formEl.image.value, size: formEl.size.value, bus: formEl.bus.value, linked: true, device: 'disk' };
            const disk = diskItem(c, errs, 0);
            if (errs.length) throw new Error(errs.map((x) => x.message).join(' · '));
            return { disk };
          };
        }
      } else if (op === 'add-nic') {
        const [nets, links] = await Promise.all([getJson(`${base}/networks`), getJson(`${base}/links`)]);
        hint(tr('kvmf.h.addNic'));
        const opt = (list) => list.map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join('');
        $('fields').innerHTML = hfield('kind', tr('kvmf.n.kind'), `<select name="kind">${opt(NIC_KINDS)}</select>`, tr('kvmf.n.kindTip'))
          + hfield('network', tr('kvmf.n.network'), `<select name="network">${opt((nets.networks || []).map((n) => n.name))}</select>`, tr('kvmf.n.networkTip'))
          + hfield('bridge', tr('kvmf.n.bridge'), `<select name="bridge">${opt(((links.bridges) || []).map((b) => b.name))}</select>`, tr('kvmf.n.bridgeTip'))
          + hfield('dev', tr('kvmf.n.dev'), `<select name="dev">${opt(((links.physical) || []).map((b) => b.name))}</select>`, tr('kvmf.n.devTip'))
          + hfield('mode', tr('kvmf.n.mode'), `<select name="mode">${opt(DIRECT_MODES)}</select>`, tr('kvmf.n.modeTip'))
          + hfield('model', tr('kvmf.n.model'), `<select name="model">${opt(NIC_MODELS)}</select>`, tr('kvmf.n.modelTip'))
          + hfield('mac', tr('kvmf.n.mac'), '<input name="mac" maxlength="17" placeholder="52:54:00:…">', tr('kvmf.n.macTip'));
        const sync = () => {
          const k = formEl.kind.value;
          ['network', 'bridge', 'dev', 'mode'].forEach((n) => {
            formEl[n].closest('label').hidden = !((n === 'network' && k === 'network') || (n === 'bridge' && k === 'bridge') || ((n === 'dev' || n === 'mode') && k === 'direct'));
          });
        };
        formEl.kind.addEventListener('change', sync);
        sync();
        st.body = () => {
          const errs = [];
          const nic = nicItem({ kind: formEl.kind.value, network: formEl.network.value, bridge: formEl.bridge.value, dev: formEl.dev.value,
                                mode: formEl.mode.value, model: formEl.model.value, mac: formEl.mac.value.trim() }, errs, 0);
          if (errs.length) throw new Error(errs.map((x) => x.message).join(' · '));
          return { nic };
        };
      } else if (op === 'remove-volume' || op === 'eject-image') {
        hint(op === 'remove-volume' ? tr('kvmf.h.removeVolume', { target: arg.target, name: v.name }) : tr('kvmf.h.eject', { target: arg.target, name: v.name }));
        st.body = () => ({ target: arg.target });
      } else if (op === 'remove-nic') {
        hint(tr('kvmf.h.removeNic', { mac: arg.mac, name: v.name }));
        st.body = () => ({ mac: arg.mac });
      } else if (op === 'cloudinit') {
        hint(tr('kvmf.h.cloudinit'));
        // « Appliquer » reste grisé tant que le seed actuel n'est pas lu : un
        // envoi sur une zone vide effacerait le cloud-init de la VM
        const go = $('go');
        go.disabled = true;
        go.dataset.tip = tr('kvmf.ciLoading');
        $('fields').innerHTML = `<div class="form-row"><label>${esc(tr('kvmf.f.userData'))}</label><textarea class="yaml-editor tall tip" data-ci="userData" spellcheck="false" data-tip="${esc(tr('kvmf.t.userData'))}" disabled></textarea>
          <button type="button" class="btn btn-sm btn-secondary tip" data-h="clear" data-tip="${esc(tr('kvmf.ciClearTip'))}" disabled>${icon('trash')} ${esc(tr('kvmf.ciClear'))}</button>
          <span class="form-hint" data-h="cleared" hidden>${esc(tr('kvmf.ciCleared'))}</span></div>
          <div class="form-row"><label>${esc(tr('kvmf.f.networkData'))}</label><textarea class="yaml-editor tip" data-ci="networkData" spellcheck="false" data-tip="${esc(tr('kvmf.t.networkData'))}"></textarea></div>
          ${hfield('hostname', tr('kvmf.f.hostname'), '<input name="hostname" maxlength="63">', tr('kvmf.t.hostname'))}
          <label class="bk-check tip" data-tip="${esc(tr('kvmf.t.rerun'))}"><input type="checkbox" name="rerun"> <span>${esc(tr('kvmf.f.rerun'))}</span></label>`;
        const ud = root.querySelector('[data-ci="userData"]');
        const d = await getJson(`/api/kvm/vms/${enc(v.host)}/${enc(v.name)}/cloudinit`);
        if (d.error) {
          // lecture impossible : rien ne part, l'erreur reste affichée
          hint(errText(d.error));
          $('msg').innerHTML = `<span class="res-error">${icon('fail')} ${esc(tr('kvmf.ciLoadFailed', { error: errText(d.error) }))}</span>`;
          go.dataset.tip = tr('kvmf.ciLoadFailed', { error: errText(d.error) });
          return;
        }
        st.loaded = d.user_data || '';
        st.cleared = false;
        ud.value = st.loaded;
        root.querySelector('[data-ci="networkData"]').value = d.network_data || '';
        ud.disabled = false;
        go.disabled = false;
        go.dataset.tip = tr('kvmf.hotGoTip');
        const clear = $('clear');
        clear.disabled = false;
        clear.addEventListener('click', () => { ud.value = ''; st.cleared = true; $('cleared').hidden = false; });
        ud.addEventListener('input', () => { if (ud.value.trim()) { st.cleared = false; $('cleared').hidden = true; } });
        st.body = () => {
          const b = { user_data: ud.value,
                      network_data: root.querySelector('[data-ci="networkData"]').value || undefined, rerun: formEl.rerun.checked };
          if (formEl.hostname.value.trim()) b.hostname = formEl.hostname.value.trim();
          if (!b.user_data.trim()) {
            // vide : seulement par « Vider » (le serveur le refuse aussi sans clear)
            if (st.cleared) { b.user_data = ''; b.clear = true; }
            else if (st.loaded.trim()) throw new Error(tr('kvmf.e.ciEmpty'));
          }
          return b;
        };
      }
    })();

    formEl.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!st.body) return;
      const go = $('go');
      let body;
      try { body = st.body(); } catch (err) { $('msg').innerHTML = `<span class="res-error">${icon('fail')} ${esc(err.message)}</span>`; return; }
      go.disabled = true;
      $('steps').innerHTML = '';
      $('result').innerHTML = '';
      $('msg').textContent = tr('kvmf.sending');
      try {
        const out = await send('POST', `/api/kvm/vms/${enc(v.host)}/${enc(v.name)}/hot/${op}`, body);
        follow(out.action_id, (s) => {
          const list = $('steps');
          let li = [...list.children].find((x) => x.dataset.step === (s.step_id || ''));
          if (!li) { li = document.createElement('li'); li.dataset.step = s.step_id || ''; list.appendChild(li); }
          li.className = `kvm-step is-${s.status || 'running'}`;
          li.innerHTML = `<span class="badge info">${esc(s.step_id || '')}</span> ${esc(s.message || '')}`;
        }, (ok, run) => {
          go.disabled = false;
          const o = (run.result && run.result.output) || {};
          const pend = (o.after && o.after.pending) || [];
          if (o.after) notePending(v.host, v.name, pend, o.after.id);
          $('msg').innerHTML = ok ? `<span>${icon('ok')} ${esc(tr('kvmf.applied'))}</span>`
            : `<span class="res-error">${icon('fail')} ${esc(o.error || run.error_summary || tr('kvmf.failed'))}</span>`;
          $('result').innerHTML = VMForm.changesHtml(o.changes || []) + VMForm.restartBanner(o.restart_required ? pend : []);
          if (window.KVM && KVM.load) KVM.load(true);
        });
      } catch (err) {
        go.disabled = false;
        $('msg').innerHTML = `<span class="res-error">${icon('fail')} ${esc(err.message)}</span>`;
      }
    });
    return root;
  }

  /** Champs en attente d'un redémarrage pour une ligne de la liste : ceux
   *  servis par `/api/kvm/vms` (`v.pending`) et ceux appris par cette page.
   *  Une VM vue arrêtée ou redémarrée (autre Id) perd le repère local. */
  const RUNNING = LIVE.concat(['paused', 'in shutdown', 'pmsuspended']);
  function restartFor(v) {
    const key = `${v.host}/${v.name}`;
    const local = pendingRestart.get(key);
    if (local && (!RUNNING.includes(v.state) || (local.id != null && v.id != null && local.id !== v.id))) {
      pendingRestart.delete(key);
    }
    const kept = pendingRestart.get(key);
    const all = [...new Set([...(RUNNING.includes(v.state) ? (v.pending || []) : []), ...(kept ? kept.fields : [])])];
    return all.length ? all : null;
  }

  // parseSize : partagé avec kvm-storage.js (volumes, redimensionnement)
  return { openCreate, openEdit, openHot, menuGroupHtml, fillMenuDevices, onMenuClick, hotWhy, restartFor, parseSize,
           _internals: { parseSize, diskItem, nicItem, hotness, cardOf, nicCardOf, notePending,
                         agentDefault, bootCard, restartFor, poll: POLL } };
})();

if (typeof window !== 'undefined') window.KVMForm = KVMForm;
