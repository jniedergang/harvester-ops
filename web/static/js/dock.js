/**
 * harvester-ops — persistent bottom dock for live running actions.
 * - Always visible across all tabs when toggled ON (default).
 * - Polls /api/activity every 3s; shows each in-progress action with mini progress.
 * - Toggle button lives in the Activity tab.
 * - Collapsible (header click) and dismissible (× — equivalent to toggle OFF).
 * State persisted in localStorage.
 */

const Dock = (() => {
  const $  = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);

  const STORAGE_VISIBLE  = 'harvester_ops_dock_visible';
  const STORAGE_COLLAPSE = 'harvester_ops_dock_collapsed';
  const STORAGE_HEIGHT   = 'harvester_ops_dock_height';
  const DEFAULT_HEIGHT   = 160;

  let timer = null;
  let tickTimer = null;
  let activeSSE = {};   // run_id -> EventSource (for step messages per action)
  let resizeStart = null;

  const KEEP_DONE_FOR_SECONDS = 30;

  // Compute approximate progress from the last STEP event.
  const STEP_PROGRESS = {
    shutdown: {
      'preflight':         5,
      'etcd-snapshot':     15,
      'vm-snapshot':       25,
      'vm-stop':           45,
      'longhorn-maint':    65,
      'cordon':            75,
      'shutdown-workers':  85,
      'shutdown-cp':       95,
      'complete':         100,
    },
    startup: {
      'power-cp1':         15,
      'power-rest':        30,
      'wait-ready':        50,
      'restore':           70,
      'vm-restart':        90,
      'complete':         100,
    },
    'vm-start':    { 'patch': 30, 'wait': 100 },
    'vm-stop':     { 'patch': 30, 'wait': 100 },
    'vm-manual':   { 'patch': 100 },
    'vm-snapshot':       { 'create': 20, 'progress': null /* use message pct */ },
    'snapshot-delete':   { 'delete': 100 },
    'snapshot-restore':  { 'apply': 30, 'wait': 100 },
    'vm-migrate':        { 'create': 30, 'wait': 100 },
    'capi-bundle-build': {
      'start': 5, 'manifests': 15, 'images': 70, 'clusterclass': 90, 'archive': 100,
    },
    'capi-install': {
      'preflight':         3,
      'extract':           6,
      'images':           35,
      'cert-manager-apply': 40, 'cert-manager-wait': 48,
      'cluster-api-apply':  53, 'cluster-api-wait':  60,
      'cabp-rke2-apply':    65, 'cabp-rke2-wait':    72,
      'cacp-rke2-apply':    76, 'cacp-rke2-wait':    82,
      'caphv-apply':        86, 'caphv-wait':        94,
      'clusterclass':      100,
    },
    'capi-uninstall': {
      'preflight':    5,
      'clusterclass': 20,
      'namespaces':   60,
      'crds':         80,
      'ctr-images':  100,
    },
  };

  // Some action steps carry their progress as a trailing "N%" in the message
  // (vm-snapshot, vm-image-upload, capi-bundle-build images …). Extract it
  // whenever we see one — cheaper than maintaining a per-step allowlist.
  function pctFromMessage(_stepId, message) {
    if (!message) return null;
    const m = message.match(/(\d+)\s*%/);
    return m ? parseInt(m[1]) : null;
  }

  function pctForAction(actionFull, stepId) {
    // Action labels can be "vm-stop:default/foo" — take the prefix
    const baseAction = actionFull.split(':')[0];
    const map = STEP_PROGRESS[baseAction] || {};
    return map[stepId];
  }

  const liveSteps = {};   // run_id -> {step_id, status, message, pct}
  const liveLogs  = {};   // run_id -> [recent events as strings]
  const expanded  = new Set();   // run_ids currently showing details
  const LOG_BUFFER_SIZE = 80;

  function isVisible() {
    return localStorage.getItem(STORAGE_VISIBLE) !== 'false';   // default true
  }
  function setVisible(v) {
    localStorage.setItem(STORAGE_VISIBLE, v ? 'true' : 'false');
    applyVisibility();
  }
  function isCollapsed() {
    return localStorage.getItem(STORAGE_COLLAPSE) === 'true';
  }
  function setCollapsed(v) {
    localStorage.setItem(STORAGE_COLLAPSE, v ? 'true' : 'false');
    applyVisibility();
  }

  function applyVisibility() {
    const visible = isVisible();
    const collapsed = isCollapsed();
    const dock = $('#bottom-dock');
    if (!dock) return;
    dock.style.display = visible ? 'flex' : 'none';
    dock.classList.toggle('collapsed', collapsed);
    document.body.classList.toggle('has-dock', visible);
    document.body.classList.toggle('dock-collapsed', visible && collapsed);
    // Apply persisted height
    const h = parseInt(localStorage.getItem(STORAGE_HEIGHT) || DEFAULT_HEIGHT);
    document.documentElement.style.setProperty('--dock-height', h + 'px');
    document.body.style.setProperty('--dock-padding', (h + 20) + 'px');
    applyContentPadding(visible, collapsed, h);
    // Sync toggle UI in Activity tab
    const toggle = $('#dock-toggle');
    if (toggle) toggle.checked = visible;
  }

  function applyContentPadding(visible, collapsed, height) {
    const content = $('#content');
    if (!content) return;
    if (!visible)        content.style.paddingBottom = '';
    else if (collapsed)  content.style.paddingBottom = '56px';
    else                 content.style.paddingBottom = (height + 24) + 'px';
  }

  // -------------------------------------------------------------------------
  // Vertical resize (handle at top edge of dock)
  // -------------------------------------------------------------------------
  function onResizeStart(e) {
    const dock = $('#bottom-dock');
    resizeStart = { y: e.clientY, h: dock.getBoundingClientRect().height };
    $('#dock-resize-handle')?.classList.add('dragging');
    dock.classList.remove('transitioning');
    document.body.style.userSelect = 'none';
    document.addEventListener('mousemove', onResizeMove);
    document.addEventListener('mouseup', onResizeEnd, { once: true });
    e.preventDefault();
  }
  function onResizeMove(e) {
    if (!resizeStart) return;
    const delta = resizeStart.y - e.clientY;   // moving UP increases height
    const newH = Math.max(80, Math.min(window.innerHeight * 0.7, resizeStart.h + delta));
    document.documentElement.style.setProperty('--dock-height', newH + 'px');
    applyContentPadding(true, isCollapsed(), newH);
  }
  function onResizeEnd() {
    if (!resizeStart) return;
    const dock = $('#bottom-dock');
    const newH = Math.round(dock.getBoundingClientRect().height);
    localStorage.setItem(STORAGE_HEIGHT, String(newH));
    resizeStart = null;
    $('#dock-resize-handle')?.classList.remove('dragging');
    document.body.style.userSelect = '';
    document.removeEventListener('mousemove', onResizeMove);
  }

  async function poll() {
    if (!isVisible()) return;
    try {
      const data = await fetch('/api/activity').then(r => r.json());
      const now = Date.now() / 1000;
      const inProgress = (data.in_progress || []);
      // Keep recently completed actions visible for KEEP_DONE_FOR_SECONDS
      const recentlyDone = (data.actions_done || [])
        .filter(a => a.ended_at && (now - a.ended_at) <= KEEP_DONE_FOR_SECONDS);
      // Merge: in-progress first (newest first), then recent done (newest first)
      const items = [
        ...inProgress.sort((a, b) => b.started_at - a.started_at),
        ...recentlyDone.sort((a, b) => b.ended_at - a.ended_at),
      ];
      render(items);
    } catch (e) {
      console.warn('dock poll failed', e);
    }
  }

  function render(actions) {
    const list   = $('#dock-list');
    const empty  = $('#dock-empty');
    const count  = $('#dock-count');
    if (!list || !empty || !count) return;

    count.textContent = actions.length;
    count.classList.toggle('zero', actions.length === 0);

    if (actions.length === 0) {
      empty.style.display = 'block';
      list.innerHTML = '';
      // Close any lingering SSE
      Object.values(activeSSE).forEach(es => es.close());
      activeSSE = {};
      return;
    }
    empty.style.display = 'none';

    // Diff: track current IDs
    const current = new Set(actions.map(a => a.id));
    Object.keys(activeSSE).forEach(id => {
      if (!current.has(id)) {
        activeSSE[id].close();
        delete activeSSE[id];
        delete liveSteps[id];
      }
    });

    list.innerHTML = '';
    actions.forEach(a => {
      const isDone    = a.status === 'done';
      const isError   = a.status === 'error' || a.status === 'cancelled';
      const isRunning = !a.ended_at && !isDone && !isError;
      // Subscribe to SSE for live step + log updates if not already.
      // Skip if the action is finished — we already have all events in liveLogs.
      if (isRunning && !activeSSE[a.id]) {
        const buf = liveLogs[a.id] = liveLogs[a.id] || [];
        const push = (line, cls) => {
          buf.push({ line, cls, ts: Date.now() });
          if (buf.length > LOG_BUFFER_SIZE) buf.shift();
          updateLogTail(a.id);
        };
        activeSSE[a.id] = SSEReconnect.connect(`/api/stream/${a.id}`, {
          on: {
            step: (e) => {
              const ev = JSON.parse(e.data);
              let pct = pctForAction(a.action, ev.step_id);
              const msgPct = pctFromMessage(ev.step_id, ev.message);
              if (msgPct !== null) pct = msgPct;
              if (pct == null) pct = (liveSteps[a.id]?.pct || 0);
              liveSteps[a.id] = { step_id: ev.step_id, status: ev.status, message: ev.message, pct };
              push(`[step] ${ev.step_id} → ${ev.status}${ev.message ? ' — ' + ev.message : ''}`,
                   ev.status === 'done' ? 'ok' : ev.status === 'error' ? 'err' : 'info');
              updateCard(a.id);
            },
            log: (e) => {
              const ev = JSON.parse(e.data);
              const msg = (ev.message || '').replace(/\u001b\[[0-9;]*m/g, '');
              if (!msg.trim()) return;
              const cls = msg.includes('[ERROR]') ? 'err'
                        : msg.includes('[WARN')  ? 'warn'
                        : msg.includes('[OK')    ? 'ok'
                        : msg.includes('[INFO')  ? 'info'
                        : msg.includes('DRY-RUN') ? 'dim'
                        : '';
              push(msg, cls);
            },
            status: (e) => {
              const ev = JSON.parse(e.data);
              push(`[status] ${ev.status}${ev.exit_code !== undefined ? ' (exit ' + ev.exit_code + ')' : ''}`,
                   ev.status === 'done' ? 'ok' : 'warn');
            },
            end: () => {
              push(`[end] action complete`, 'ok');
              delete activeSSE[a.id];
            },
          },
          onStatus: (s) => {
            if (s.state === 'retry') {
              push(`[stream] reconnecting in ${Math.round(s.delay/1000)}s (${s.attempt}/5)`, 'warn');
            } else if (s.state === 'dead') {
              push(`[stream] reconnect failed — action may still be running`, 'err');
              delete activeSSE[a.id];
            }
          },
        });
      }

      const live = liveSteps[a.id] || { step_id: '–', status: 'running', message: '', pct: 5 };
      const card = document.createElement('div');
      card.className = 'dock-action-card'
                     + (expanded.has(a.id) ? ' expanded' : '')
                     + (isDone  ? ' state-done'  : '')
                     + (isError ? ' state-error' : '');
      card.id = 'dock-card-' + a.id;
      card.dataset.id = a.id;
      card.dataset.startedAt = a.started_at;
      if (a.ended_at) card.dataset.endedAt = a.ended_at;

      const dryBadge = a.dry_run ? ' <span class="activity-type-badge" style="background:var(--warn);color:#000;">dry-run</span>' : '';
      const startedHuman = new Date(a.started_at * 1000).toLocaleTimeString();
      const endedHuman   = a.ended_at ? new Date(a.ended_at * 1000).toLocaleTimeString() : '';

      // Final progress on done state
      const pctValue = isRunning ? live.pct : 100;
      // v1.6.5 : une erreur montre POURQUOI (stderr kubectl/script), pas
      // seulement "exit 1" — le message complet reste lisible au survol.
      const errSuffix = (!isRunning && !isDone && a.error_summary)
        ? ' — ' + escapeHtml(a.error_summary) : '';
      const stepText = isRunning
        ? `${live.step_id}${live.message ? ' — ' + live.message : ''}`
        : (isDone ? '✓ completed'
                  : '✗ ' + (a.exit_code !== null ? 'exit ' + a.exit_code : (a.status || 'failed')) + errSuffix);
      const stepTitle = (!isRunning && !isDone && a.error_summary)
        ? escapeHtml(a.error_summary) : live.step_id;

      const icon = isRunning ? '⚡' : (isDone ? '✓' : '✗');

      card.innerHTML = `
        <div class="dock-action-summary">
          <span class="icon">${icon}</span>
          <div class="info">
            <div class="name">${a.action} → ${a.cluster}${dryBadge}</div>
            <div class="meta">
              <code class="aid">${a.id}</code> ·
              <span class="started" title="started">${startedHuman}</span>
              <span class="elapsed"></span>
            </div>
          </div>
          <div class="step-msg" title="${stepTitle}">${stepText}</div>
          <div class="progress-mini"><div class="fill" style="width:${pctValue}%"></div></div>
          <button class="btn-mini toggle-details" data-id="${a.id}"
                  title="${expanded.has(a.id) ? 'Masquer les logs' : 'Afficher les logs en temps réel'}"
                  data-i18n-title="${expanded.has(a.id) ? 'dock.hideDetails' : 'dock.showDetails'}">${expanded.has(a.id) ? 'Masquer' : 'Détails'}</button>
          ${isRunning
            ? `<button class="btn-mini" data-cancel="${a.id}">Cancel</button>`
            : `<span class="ended-badge" title="ended ${endedHuman}">${endedHuman}</span>`}
        </div>
        <pre class="dock-action-log" id="dock-log-${a.id}"></pre>`;
      const cancelBtn = card.querySelector('[data-cancel]');
      if (cancelBtn) cancelBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (!confirm('Cancel ' + a.action + ' on ' + a.cluster + '?')) return;
        try {
          await fetch(`/api/action/${a.id}`, { method: 'DELETE' });
          poll();
        } catch (err) { alert(err.message); }
      });
      card.querySelector('.toggle-details').addEventListener('click', (e) => {
        e.stopPropagation();
        if (expanded.has(a.id)) expanded.delete(a.id); else expanded.add(a.id);
        card.classList.toggle('expanded');
        const open = expanded.has(a.id);
        e.currentTarget.textContent = open ? 'Masquer' : 'Détails';
        e.currentTarget.title = open ? 'Masquer les logs' : 'Afficher les logs en temps réel';
        e.currentTarget.setAttribute('data-i18n-title', open ? 'dock.hideDetails' : 'dock.showDetails');
        updateLogTail(a.id);
      });
      list.appendChild(card);
      updateLogTail(a.id);
    });
    // Tick once to populate the .elapsed text immediately
    tickElapsed();
  }

  function fmtDuration(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    if (seconds < 60)   return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds/60)}m ${seconds % 60}s`;
    return `${Math.floor(seconds/3600)}h ${Math.floor((seconds%3600)/60)}m`;
  }

  function tickElapsed() {
    const now = Date.now() / 1000;
    document.querySelectorAll('.dock-action-card').forEach(card => {
      const startedAt = parseFloat(card.dataset.startedAt);
      const endedAt   = card.dataset.endedAt ? parseFloat(card.dataset.endedAt) : null;
      const el = card.querySelector('.elapsed');
      if (!el || !startedAt) return;
      if (endedAt) {
        const duration = endedAt - startedAt;
        const sinceDone = now - endedAt;
        el.innerHTML = ` · <span class="dur">⏱ ${fmtDuration(duration)}</span>` +
                       ` · <span class="ago">done ${fmtDuration(sinceDone)} ago</span>`;
      } else {
        const elapsed = now - startedAt;
        el.innerHTML = ` · <span class="dur running">⏱ ${fmtDuration(elapsed)}</span>`;
      }
    });
  }

  function updateLogTail(runId) {
    const pre = document.getElementById('dock-log-' + runId);
    if (!pre) return;
    const buf = liveLogs[runId] || [];
    pre.innerHTML = buf.map(e => `<span class="${e.cls || ''}">${escapeHtml(e.line)}</span>`).join('\n');
    pre.scrollTop = pre.scrollHeight;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function updateCard(runId) {
    const card = $(`#dock-card-${runId}`);
    if (!card) return;
    // Freeze the summary line once the action is finished: the next poll() will
    // rebuild the card with the proper "✓ completed" / "✗ exit N" text.
    if (card.classList.contains('state-done') || card.classList.contains('state-error')) return;
    const live = liveSteps[runId];
    if (!live) return;
    const msgEl = card.querySelector('.step-msg');
    const fillEl = card.querySelector('.progress-mini .fill');
    if (msgEl) {
      msgEl.textContent = live.step_id + (live.message ? ' — ' + live.message : '');
      msgEl.title = live.step_id;
    }
    if (fillEl) fillEl.style.width = live.pct + '%';
  }

  function init() {
    if (!$('#bottom-dock')) return;
    applyVisibility();

    // Header click → toggle collapsed
    $('.bottom-dock-header')?.addEventListener('click', (e) => {
      if (e.target.closest('.btn-icon-sm')) return;   // ignore button clicks
      const dock = $('#bottom-dock');
      dock.classList.add('transitioning');
      setCollapsed(!isCollapsed());
      setTimeout(() => dock.classList.remove('transitioning'), 250);
    });
    // Vertical resize handle
    $('#dock-resize-handle')?.addEventListener('mousedown', onResizeStart);
    $('#btn-dock-collapse')?.addEventListener('click', (e) => {
      e.stopPropagation();
      setCollapsed(!isCollapsed());
    });
    $('#btn-dock-hide')?.addEventListener('click', (e) => {
      e.stopPropagation();
      setVisible(false);
    });
    // Toggle in Activity tab
    $('#dock-toggle')?.addEventListener('change', (e) => {
      setVisible(e.target.checked);
    });

    // Poll every 1s for snappy "actions en cours" updates
    poll();
    timer = setInterval(poll, 1000);
    // Independent tick to update the live elapsed counter every second
    // (decoupled from poll so chrono updates even if API is slow)
    tickTimer = setInterval(tickElapsed, 1000);

    // v1.5.7: pause the poll loop when the tab is hidden (saves API
    // calls + CPU) and stop everything on page unload so we don't
    // leak intervals on the way out.
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        if (timer) { clearInterval(timer); timer = null; }
        if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
      } else {
        if (!timer) { poll(); timer = setInterval(poll, 1000); }
        if (!tickTimer) { tickTimer = setInterval(tickElapsed, 1000); }
      }
    });
    window.addEventListener('beforeunload', () => {
      if (timer) clearInterval(timer);
      if (tickTimer) clearInterval(tickTimer);
      Object.values(activeSSE || {}).forEach(es => {
        try { es.close(); } catch {}
      });
    });
  }

  return { init, poll, isVisible, setVisible };
})();

document.addEventListener('DOMContentLoaded', Dock.init);
