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
  const RETRY_MS = 2000;
  const MAX_RETRIES = 45;             // ~90 s of boot-waiting
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
          // VM gets its boot captured. Other errors are terminal.
          if (r.status === 409) {
            setStatus('retrying', tr('console.vmNotRunning',
              'The VM is not running — start it to catch the boot.'));
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
        rfb.scaleViewport = state.scaled;
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
      state.retryTimer = setTimeout(connectOnce, RETRY_MS);
    }

    reconnectBtn.addEventListener('click', () => {
      state.retries = 0;
      connectOnce();
    });
    wrap.querySelector('.vm-console-cad').addEventListener('click', () => {
      if (state.rfb) state.rfb.sendCtrlAltDel();
    });
    wrap.querySelector('.vm-console-scale').addEventListener('click', (e) => {
      state.scaled = !state.scaled;
      e.currentTarget.textContent = state.scaled ? '1:1' : 'Fit';
      if (state.rfb) state.rfb.scaleViewport = state.scaled;
    });

    const panel = FloatingPanels.open({
      id: panelId,
      title: `🖥 Console — ${namespace}/${name}`,
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
