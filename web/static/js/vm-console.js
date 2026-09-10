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

  function open(cluster, namespace, name) {
    const panelId = `vm-console-${cluster}-${namespace}-${name}`;
    const state = {
      rfb: null, closed: false, retries: 0, retryTimer: null, scaled: true,
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
                >${Icons.svg('settings')}</button>
        <span class="vm-console-sep"></span>
        <button class="btn-mini vm-console-cad tip"
                data-tip="${escapeHtml(tr('console.ctrlAltDelTip', 'Send Ctrl-Alt-Del to the VM'))}"
                >Ctrl-Alt-Del</button>
        <button class="btn-mini vm-console-scale tip"
                data-tip="${escapeHtml(tr('console.scaleTip', 'Toggle fit-to-window / 1:1 pixel size'))}"
                >1:1</button>
      </div>
      <div class="vm-console-screen" tabindex="0"></div>`;
    const screen = wrap.querySelector('.vm-console-screen');
    const statusEl = wrap.querySelector('.vm-console-status');
    const dotEl = wrap.querySelector('.vm-console-dot');
    const reconnectBtn = wrap.querySelector('.vm-console-reconnect');

    function setStatus(stateName, text) {
      dotEl.dataset.state = stateName;
      statusEl.textContent = text;
      reconnectBtn.hidden = stateName !== 'failed';
    }

    // --- connection loop --------------------------------------------------
    async function connectOnce() {
      if (state.closed) return;
      let ticket;
      try {
        const r = await fetch(
          `/api/vm/${cluster}/${namespace}/${name}/console-ticket`,
          { method: 'POST' });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) {
          // 409 = VM not running (yet): keep retrying so a freshly started
          // VM gets its boot captured. 429 = session cap, usually transient
          // reconnect churn: retry too. Other errors are terminal.
          if (r.status === 409) {
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
      } catch (e) {
        setStatus('retrying', tr('console.connecting', 'Connecting…'));
        scheduleRetry();
        return;
      }

      const RFB = await loadRFB();
      if (state.closed) return;
      const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
      const url = `${proto}${location.host}/ws/vnc/${cluster}/${namespace}/${name}`
                + `?ticket=${encodeURIComponent(ticket)}`;
      setStatus('connecting', tr('console.connecting', 'Connecting…'));
      const rfb = new RFB(screen, url, { wsProtocols: [] });
      rfb.scaleViewport = state.scaled;
      rfb.background = '#000';
      state.rfb = rfb;

      rfb.addEventListener('connect', () => {
        state.retries = 0;
        setStatus('connected', tr('console.connected', 'Connected'));
        // Re-apply after layout: set before the panel had dimensions, the
        // initial scale computation can run against a 0-sized container.
        applyScaleMode();
        rfb.focus();
      });
      rfb.addEventListener('disconnect', () => {
        state.rfb = null;
        if (state.closed) return;
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

    async function powerAction(kind) {
      const base = `/api/vm/${cluster}/${namespace}/${name}`;
      try {
        if (kind === 'start') {
          await fetch(`${base}/runStrategy`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ runStrategy: 'Always' }),
          });
          kickRetry();
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
          kickRetry();      // reattach fast enough to catch the firmware
        }
      } catch (e) { /* the dock tracks the action and carries the error */ }
    }

    reconnectBtn.addEventListener('click', () => {
      state.retries = 0;
      connectOnce();
    });
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
      title: `Console — ${namespace}/${name}`,
      icon: 'console',
      bodyNode: wrap,
      width: 900, height: 620,
      restoreSpec: { type: 'vm-console', args: { cluster, namespace, name } },
      onClose: () => {
        state.closed = true;
        clearTimeout(state.retryTimer);
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

  return { open };
})();

window.VMConsole = VMConsole;
FloatingPanels.registerType('vm-console', (args) =>
  VMConsole.open(args.cluster, args.namespace, args.name));
