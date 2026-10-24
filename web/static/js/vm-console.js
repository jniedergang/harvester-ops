/**
 * harvester-ops — in-browser VNC console (noVNC + Flask WebSocket relay).
 *
 * Flow: POST /api/vm/<c>/<ns>/<n>/console-ticket (authenticated pre-flight —
 * the only place readable errors can come from, a rejected WS handshake is
 * opaque to the browser) → open wss://…/ws/vnc/…?ticket=… → RFB on a canvas.
 *
 * noVNC (~300 KB of ES modules) is loaded lazily via dynamic import() on the
 * first console open, so the page boot cost is zero. The auto-retry loop
 * (2 s, up to ~90 s) re-requests a ticket on every attempt: open the console
 * right after starting a VM and the firmware/GRUB output is caught as soon
 * as qemu exposes the display.
 *
 * v1.97.0 : la même fenêtre sert une VM KVM (`openKvm(host, domain)`). Tout
 * ce qui dépend de la plateforme passe par une CIBLE : `harvesterTarget`
 * (URLs /api/vm/..., boutons de marche par runStrategy, instantanés et
 * réglages) ou `kvmTarget` (URLs /api/kvm/..., `ws_path` lu dans la réponse
 * du ticket, gestes KVM suivis par VMActions.follow, arrêt forcé confirmé
 * dans la page, pas de réglages avant 1.98). Un 422 du ticket KVM (affichage
 * SPICE seul, socket, pas d'écoute...) est dit sans nouvel essai.
 */
const VMConsole = (() => {
  // Two-phase retry: a fast phase right after a disconnect (a KubeVirt
  // hard reset destroys qemu — reattaching within ~1 s is what makes the
  // SeaBIOS/firmware splash catchable), then a relaxed phase while waiting
  // for a VM that is simply off.
  const RETRY_FAST_MS = 400;
  const RETRY_FAST_COUNT = 20;        // ~8 s of eager reattach
  const RETRY_MS = 2000;
  const MAX_RETRIES = 60;             // fast phase + ~80 s relaxed
  let rfbModule = null;               // memoised dynamic import

  function loadRFB() {
    if (!rfbModule) {
      rfbModule = import('/static/vendor/novnc/core/rfb.js').then(m => m.default);
    }
    return rfbModule;
  }

  function tr(key, fallback, vars) {
    return (window.i18n && typeof i18n.t === 'function') ? i18n.t(key, vars) : fallback;
  }

  // --- cibles ---------------------------------------------------------------
  // Ce qui change d'une plateforme à l'autre ; le reste de la fenêtre (boucle
  // de connexion, partage, échelle) est commun.
  function harvesterTarget(cluster, namespace, name) {
    const base = `/api/vm/${cluster}/${namespace}/${name}`;
    return {
      panelId: `vm-console-${cluster}-${namespace}-${name}`,
      title: `Console — ${namespace}/${name}`,
      restoreSpec: { type: 'vm-console', args: { cluster, namespace, name } },
      entity: null,
      ticketUrl: `${base}/console-ticket`,
      statusUrl: `${base}/console-status`,
      wsPath: () => `/ws/vnc/${cluster}/${namespace}/${name}`,
      kind: 'harvester',
      wire(wrap, ctx) {
        wrap.querySelector('.vm-console-power-start')
            .addEventListener('click', () => powerAction('start'));
        wrap.querySelector('.vm-console-power-stop')
            .addEventListener('click', () => powerAction('stop'));
        wrap.querySelector('.vm-console-power-reset')
            .addEventListener('click', () => powerAction('reset'));
        wrap.querySelector('.vm-console-snapshots').addEventListener('click', () => {
          if (window.VMSnapshots) VMSnapshots.open(cluster, namespace, name);
        });
        wrap.querySelector('.vm-console-settings').addEventListener('click', () => {
          if (window.VMEdit) VMEdit.open(cluster, namespace, name);
        });

        async function powerAction(kind) {
          try {
            if (kind === 'start') {
              await fetch(`${base}/runStrategy`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ runStrategy: 'Always' }),
              });
              ctx.kickRetry();
            } else if (kind === 'stop') {
              if (!confirm(tr('console.confirmStop', 'Stop VM {name}?', { name }))) return;
              await fetch(`${base}/runStrategy`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ runStrategy: 'Halted' }),
              });
            } else if (kind === 'reset') {
              if (!confirm(tr('console.confirmReset',
                'Hard reset VM {name}? Unsaved guest state is lost.', { name }))) return;
              await fetch(`${base}/restart`, { method: 'POST' });
              ctx.kickRetry();      // reattach fast enough to catch the firmware
            }
          } catch (e) { /* the dock tracks the action and carries the error */ }
        }
      },
      // 409 : la VM ne tourne pas (encore) ; tout autre refus est final
      refusal: () => null,
      lostReason: () => null,
    };
  }

  function kvmTarget(host, domain) {
    const enc = encodeURIComponent;
    const base = `/api/kvm/vms/${enc(host)}/${enc(domain)}`;
    let wsPath = null;
    const reasonText = (reason, fallback, detail) => ({
      notrunning: tr('console.vmNotRunning', fallback),
      spice: tr('console.kvm.spice', fallback),
      socket: tr('console.kvm.socket', fallback),
      nodisplay: tr('console.kvm.nodisplay', fallback),
      nolisten: tr('console.kvm.nolisten', fallback),
      listen: tr('console.kvm.listen', fallback),
      auth: tr('console.kvm.auth', fallback),
      'unsupported-security': tr('console.kvm.unsupportedSecurity', fallback, { detail: detail || '' }),
      'host-removed': tr('console.kvm.hostRemoved', fallback),
      unreachable: tr('console.kvm.unreachable', fallback, { detail: detail || '' }),
    }[reason] || fallback);
    return {
      panelId: `kvm-console-${host}-${enc(domain)}`,
      title: tr('console.kvm.title', `Console: ${domain} on ${host}`, { name: domain, host }),
      restoreSpec: { type: 'kvm-console', args: { host, domain } },
      entity: { key: `kvm:${host}|${domain}`, label: `${host}/${domain}` },
      ticketUrl: `${base}/console-ticket`,
      statusUrl: `${base}/console-status`,
      takeTicket(body) { wsPath = body.ws_path; },
      wsPath: () => wsPath,
      kind: 'kvm',
      wire(wrap, ctx) {
        const confirmBox = wrap.querySelector('.vm-console-confirm');
        async function gesture(g) {
          try {
            const r = await fetch(`${base}/${g}`, {
              method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
            });
            const out = await r.json().catch(() => ({}));
            if (!r.ok) { ctx.say('failed', out.hint || out.error || `HTTP ${r.status}`); return; }
            if (window.VMActions && VMActions.follow) {
              VMActions.follow(out.action_id, null,
                tr('console.kvm.gestureDone', `${g}: done`, { name: domain }),
                () => { if (g === 'start') ctx.kickRetry(); });
            }
            if (g === 'start') ctx.kickRetry();
          } catch (e) { ctx.say('failed', e.message); }
        }
        function askDestroy() {
          const q = tr('kvm.destroyConfirm', `Force off ${domain} on ${host}?`, { name: domain, host });
          confirmBox.innerHTML = `<div class="bkg-confirm" role="group" aria-label="${escapeHtml(q)}">
            <span>${escapeHtml(q)}</span>
            <button type="button" class="btn btn-sm btn-danger tip vm-console-destroy-yes"
                    data-tip="${escapeHtml(tr('kvm.destroyYesTip', 'Cut the power of this VM now'))}"
                    >${Icons.svg('stop')} ${escapeHtml(tr('kvm.destroyYes', 'Force off'))}</button>
            <button type="button" class="btn btn-sm btn-secondary tip vm-console-destroy-no"
                    data-tip="${escapeHtml(tr('kvm.confirmNoTip', 'Keep the VM as it is'))}"
                    >${escapeHtml(tr('kvm.confirmNo', 'Cancel'))}</button></div>`;
          confirmBox.hidden = false;
          confirmBox.querySelector('.vm-console-destroy-no').focus();
        }
        confirmBox.addEventListener('click', (e) => {
          if (e.target.closest('.vm-console-destroy-yes')) {
            confirmBox.hidden = true; confirmBox.innerHTML = ''; gesture('destroy');
          } else if (e.target.closest('.vm-console-destroy-no')) {
            confirmBox.hidden = true; confirmBox.innerHTML = '';
          }
        });
        wrap.querySelector('.vm-console-kvm-start').addEventListener('click', () => gesture('start'));
        wrap.querySelector('.vm-console-kvm-shutdown').addEventListener('click', () => gesture('shutdown'));
        wrap.querySelector('.vm-console-kvm-destroy').addEventListener('click', askDestroy);
        // Les instantanés KVM arrivent avec leur propre module : tant qu'il
        // n'est pas chargé, le bouton n'apparaît pas (il ne ferait rien).
        const snapBtn = wrap.querySelector('.vm-console-kvm-snapshots');
        if (!window.KVMSnapshots) {
          snapBtn.hidden = true;
          const sep = snapBtn.previousElementSibling;
          if (sep && sep.classList.contains('vm-console-sep')) sep.hidden = true;
        }
        snapBtn.addEventListener('click', () => {
          if (window.KVMSnapshots) KVMSnapshots.open(host, domain);
        });
      },
      // Refus d'un ticket : texte à afficher et nouvel essai ou non. Le 422
      // est un affichage qu'aucun essai ne rendra joignable.
      refusal(status, body) {
        if (status === 409) return { retry: true, text: reasonText('notrunning', body.error) };
        if (status === 422) return { retry: false, text: reasonText(body.reason, body.error || '?') };
        return null;
      },
      // Pourquoi la dernière connexion est tombée : les raisons finales
      // arrêtent la boucle (mot de passe refusé, hôte retiré, affichage
      // devenu injoignable par nature).
      lostReason(last) {
        if (!last) return null;
        const final = ['auth', 'unsupported-security', 'host-removed', 'spice', 'socket', 'nodisplay', 'nolisten', 'listen'];
        if (final.includes(last.reason)) return { retry: false, text: reasonText(last.reason, last.detail || last.reason, last.detail) };
        if (last.reason === 'unreachable') return { retry: true, text: reasonText('unreachable', last.detail || '', last.detail) };
        return null;
      },
    };
  }

  function open(cluster, namespace, name) {
    return openWith(harvesterTarget(cluster, namespace, name));
  }

  function openKvm(host, domain) {
    return openWith(kvmTarget(host, domain));
  }

  function openWith(t) {
    const panelId = t.panelId;
    // Déjà ouverte : la ramener, sans ouvrir une seconde connexion cachée.
    if (document.getElementById('fp-' + panelId)) return FloatingPanels.open({ id: panelId });
    const state = {
      rfb: null, closed: false, retries: 0, retryTimer: null, scaled: true,
      statusTimer: null,
    };

    // --- panel body -------------------------------------------------------
    const wrap = document.createElement('div');
    wrap.className = 'vm-console-wrap';
    wrap.innerHTML = `
      <div class="vm-console-bar">
        <span class="vm-console-dot" data-state="connecting"></span>
        <span class="vm-console-status"></span>
        <span class="vm-console-spacer"></span>
        <button class="btn-mini vm-console-reconnect tip" hidden
                data-tip="${escapeHtml(tr('console.reconnectTip', 'Retry the console connection'))}"
                >${escapeHtml(tr('console.reconnect', 'Reconnect'))}</button>
        ${t.kind === 'kvm' ? `
            <button class="btn-mini vm-console-kvm-start tip"
                    data-tip="${escapeHtml(tr('kvm.g.startTip', 'Start the VM'))}"
                    ><span class="icon-green">${Icons.svg('play')}</span></button>
            <button class="btn-mini vm-console-kvm-shutdown tip"
                    data-tip="${escapeHtml(tr('kvm.g.shutdownTip', 'Shut the VM down'))}"
                    >${Icons.svg('power')}</button>
            <button class="btn-mini vm-console-kvm-destroy tip"
                    data-tip="${escapeHtml(tr('kvm.g.destroyTip', 'Force off'))}"
                    ><span class="icon-red">${Icons.svg('stop')}</span></button>
            <span class="vm-console-sep"></span>
            <button class="btn-mini vm-console-kvm-snapshots tip"
                    data-tip="${escapeHtml(tr('console.kvm.snapshotsTip', 'Open the snapshots of this VM'))}"
                    >${Icons.svg('snapshot')}</button>` : `
            <button class="btn-mini vm-console-power-start tip"
                    data-tip="${escapeHtml(tr('console.startTip', 'Start the VM'))}"
                    ><span class="icon-green">${Icons.svg('play')}</span></button>
            <button class="btn-mini vm-console-power-stop tip"
                    data-tip="${escapeHtml(tr('console.stopTip', 'Stop the VM (graceful)'))}"
                    ><span class="icon-red">${Icons.svg('stop')}</span></button>
            <button class="btn-mini vm-console-power-reset tip"
                    data-tip="${escapeHtml(tr('console.resetTip', 'Hard reset: destroy and respawn the VM instance'))}"
                    >${Icons.svg('restart')}</button>
            <span class="vm-console-sep"></span>
            <button class="btn-mini vm-console-snapshots tip"
                    data-tip="${escapeHtml(tr('console.snapshotsTip', 'Open VM snapshots'))}"
                    >${Icons.svg('snapshot')}</button>
            <button class="btn-mini vm-console-settings tip"
                    data-tip="${escapeHtml(tr('console.settingsTip', 'Open VM settings (CPU, memory, disks, cloud-init)'))}"
                    >${Icons.svg('settings')}</button>`}
        <span class="vm-console-sep"></span>
        <button class="btn-mini vm-console-cad tip"
                data-tip="${escapeHtml(tr('console.ctrlAltDelTip', 'Send Ctrl-Alt-Del to the VM'))}"
                >Ctrl-Alt-Del</button>
        <button class="btn-mini vm-console-scale tip"
                data-tip="${escapeHtml(tr('console.scaleTip', 'Toggle fit-to-window / 1:1 pixel size'))}"
                >1:1</button>
      </div>
      <div class="vm-console-confirm" hidden></div>
      <div class="vm-console-screen" tabindex="0"></div>`;
    const screen = wrap.querySelector('.vm-console-screen');
    const statusEl = wrap.querySelector('.vm-console-status');
    const dotEl = wrap.querySelector('.vm-console-dot');
    const reconnectBtn = wrap.querySelector('.vm-console-reconnect');

    function setStatus(stateName, text) {
      dotEl.dataset.state = stateName;
      statusEl.textContent = text;
      reconnectBtn.hidden = !(stateName === 'failed' || stateName === 'taken');
      // « Reprendre la main » quand un autre client a pris l'écran : le mot
      // dit ce que le clic va faire à l'autre.
      reconnectBtn.textContent = stateName === 'taken'
        ? tr('console.takeBack', 'Take it back')
        : tr('console.reconnect', 'Reconnect');
      reconnectBtn.dataset.tip = stateName === 'taken'
        ? tr('console.takeBackTip', 'Reconnect: the other client loses the display, KubeVirt allows only one')
        : tr('console.reconnectTip', 'Retry the console connection');
    }

    // La console est PARTAGÉE : tous les navigateurs d'une VM passent par une
    // seule connexion côté serveur (KubeVirt n'en accepte qu'une). On dit
    // avec qui, pour qu'on sache qu'un autre peut taper en même temps.
    async function fetchStatus() {
      try {
        const r = await fetch(t.statusUrl);
        return r.ok ? await r.json() : null;
      } catch { return null; }
    }

    async function showViewers() {
      if (state.closed || !state.rfb || document.hidden) return;
      const st = await fetchStatus();
      if (!st || !state.rfb || state.closed) return;
      const names = (st.viewers || []).filter(Boolean);
      let text = tr('console.connected', 'Connected');
      if (st.count > 1) {
        text += ' · ' + tr('console.sharedWith', 'shared by {n} viewers', { n: st.count })
          + (names.length ? ' (' + [...new Set(names)].join(', ') + ')' : '');
      }
      setStatus('connected', text);
    }

    // Quand un collègue a repris la main depuis cette console, la connexion
    // partagée est de nouveau là : la rejoindre n'éjecte personne, on le
    // fait donc sans attendre de clic. Tant que personne ne l'a reprise, on
    // ne touche à rien, pour ne pas voler l'écran au client extérieur.
    function watchForReturn() {
      clearInterval(state.statusTimer);
      state.statusTimer = setInterval(async () => {
        if (state.closed || state.rfb || document.hidden) return;
        const st = await fetchStatus();
        if (st && st.count > 0 && !state.rfb && !state.closed) {
          clearInterval(state.statusTimer);
          state.retries = 0;
          connectOnce();
        }
      }, 5000);
    }

    // --- connection loop --------------------------------------------------
    async function connectOnce() {
      if (state.closed) return;
      let ticket;
      try {
        const r = await fetch(t.ticketUrl, { method: 'POST' });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) {
          // 409 = VM not running (yet): keep retrying so a freshly started
          // VM gets its boot captured. 429 = session cap, usually transient
          // reconnect churn: retry too. Other errors are terminal.
          const refused = t.refusal(r.status, body);
          if (refused) {
            setStatus(refused.retry ? 'retrying' : 'failed', refused.text);
            if (refused.retry) scheduleRetry();
          } else if (r.status === 409) {
            setStatus('retrying', tr('console.vmNotRunning',
              'The VM is not running — start it to catch the boot.'));
            scheduleRetry();
          } else if (r.status === 429) {
            setStatus('retrying', body.error || 'busy');
            scheduleRetry();
          } else {
            setStatus('failed', body.error || `HTTP ${r.status}`);
          }
          return;
        }
        ticket = body.ticket;
        if (t.takeTicket) t.takeTicket(body);
      } catch (e) {
        setStatus('retrying', tr('console.connecting', 'Connecting…'));
        scheduleRetry();
        return;
      }

      const RFB = await loadRFB();
      if (state.closed) return;
      const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
      const url = `${proto}${location.host}${t.wsPath()}`
                + `?ticket=${encodeURIComponent(ticket)}`;
      setStatus('connecting', tr('console.connecting', 'Connecting…'));
      const rfb = new RFB(screen, url, { wsProtocols: [] });
      rfb.scaleViewport = state.scaled;
      rfb.background = '#000';
      state.rfb = rfb;

      rfb.addEventListener('connect', () => {
        state.retries = 0;
        setStatus('connected', tr('console.connected', 'Connected'));
        showViewers();
        clearInterval(state.statusTimer);
        state.statusTimer = setInterval(showViewers, 10000);
        // Re-apply after layout: set before the panel had dimensions, the
        // initial scale computation can run against a 0-sized container.
        applyScaleMode();
        rfb.focus();
      });
      rfb.addEventListener('disconnect', async () => {
        state.rfb = null;
        clearInterval(state.statusTimer);
        if (state.closed) return;
        // Un AUTRE client a pris l'écran (la console d'Harvester, une autre
        // instance) : se reconnecter aussitôt l'éjecterait à son tour, et
        // les deux se reprendraient l'écran en boucle. On le dit, et on
        // laisse l'exploitant choisir.
        const st = await fetchStatus();
        if (state.closed) return;
        const lost = t.lostReason(st && st.last_close);
        if (lost) {
          setStatus(lost.retry ? 'retrying' : 'failed', lost.text);
          if (lost.retry) scheduleRetry();
          return;
        }
        if (st && st.last_close && st.last_close.reason === 'taken') {
          setStatus('taken', tr('console.taken',
            'Another console took this display (for example the Harvester UI): KubeVirt allows only one.'));
          watchForReturn();
          return;
        }
        setStatus('retrying', tr('console.retrying',
          'Waiting for the VM display… (attempt {n})',
          { n: state.retries + 1 }));
        scheduleRetry();
      });
    }

    function scheduleRetry() {
      if (state.closed) return;
      if (state.retries >= MAX_RETRIES) {
        setStatus('failed', tr('console.failed', 'Connection failed'));
        return;
      }
      state.retries += 1;
      clearTimeout(state.retryTimer);
      const delay = state.retries <= RETRY_FAST_COUNT ? RETRY_FAST_MS : RETRY_MS;
      state.retryTimer = setTimeout(connectOnce, delay);
    }

    // Restart the loop eagerly (fresh fast phase) after a power action.
    function kickRetry() {
      state.retries = 0;
      clearTimeout(state.retryTimer);
      if (!state.rfb) state.retryTimer = setTimeout(connectOnce, RETRY_FAST_MS);
    }

    reconnectBtn.addEventListener('click', () => {
      clearInterval(state.statusTimer);
      state.retries = 0;
      connectOnce();
    });
    t.wire(wrap, { kickRetry, say: (st, text) => setStatus(st, text) });
    wrap.querySelector('.vm-console-cad').addEventListener('click', () => {
      if (state.rfb) state.rfb.sendCtrlAltDel();
    });
    function applyScaleMode() {
      // fit: noVNC scales into the container. 1:1: native pixels, the
      // container scrolls natively (the mouse keeps driving the guest).
      screen.style.overflow = state.scaled ? 'hidden' : 'auto';
      if (state.rfb) state.rfb.scaleViewport = state.scaled;
    }
    wrap.querySelector('.vm-console-scale').addEventListener('click', (e) => {
      state.scaled = !state.scaled;
      e.currentTarget.textContent = state.scaled ? '1:1' : 'Fit';
      applyScaleMode();
    });

    const panel = FloatingPanels.open({
      id: panelId,
      title: t.title,
      icon: 'console',
      bodyNode: wrap,
      width: 900, height: 620,
      restoreSpec: t.restoreSpec,
      ...(t.entity ? { entity: t.entity } : {}),
      onClose: () => {
        state.closed = true;
        clearTimeout(state.retryTimer);
        clearInterval(state.statusTimer);
        try { if (state.rfb) state.rfb.disconnect(); } catch (e) { /* down */ }
      },
    });

    connectOnce();
    return panel;     // restoreAll() re-applies saved dims from this api
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  return { open, openKvm };
})();

window.VMConsole = VMConsole;
FloatingPanels.registerType('vm-console', (args) =>
  VMConsole.open(args.cluster, args.namespace, args.name));
FloatingPanels.registerType('kvm-console', (args) =>
  VMConsole.openKvm(args.host, args.domain));
