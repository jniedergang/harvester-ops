/**
 * harvester-ops — Support bundle UI
 * Triggers /api/support-bundle, streams progress via SSE, allows minimization to background bubble.
 */
const Support = (() => {
  const $  = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);

  let currentSSE = null;
  let currentJobId = null;
  let dragOffset = null;

  const STEP_LABELS = {
    metadata: { en: 'Capturing metadata',           fr: 'Capture des métadonnées' },
    config:   { en: 'Sanitizing configuration',     fr: 'Assainissement de la configuration' },
    logs:     { en: 'Collecting logs',              fr: 'Collecte des logs' },
    status:   { en: 'Capturing cluster status',     fr: 'Capture du statut cluster' },
    system:   { en: 'Capturing system info',        fr: 'Capture infos système' },
    archive:  { en: 'Creating tar.gz archive',      fr: 'Création de l\'archive tar.gz' },
    error:    { en: 'Error',                        fr: 'Erreur' },
  };

  function labelFor(stepId) {
    const lang = typeof i18n !== 'undefined' ? i18n.currentLang : 'en';
    const entry = STEP_LABELS[stepId];
    return entry ? (entry[lang] || entry.en) : stepId;
  }

  function open() {
    $('#support-panel').style.display = 'block';
    $('#support-bubble').style.display = 'none';
  }
  function minimize() {
    $('#support-panel').style.display = 'none';
    $('#support-bubble').style.display = 'block';
  }
  function close() {
    if (currentSSE) currentSSE.close();
    currentSSE = null;
    currentJobId = null;
    $('#support-panel').style.display = 'none';
    $('#support-bubble').style.display = 'none';
  }

  async function startBuild() {
    const anonymize = $('#sup-anonymize')?.checked ?? true;
    // Close existing
    if (currentSSE) currentSSE.close();
    // Reset UI
    $('#support-steps').innerHTML = '';
    $('#support-progress-fill').style.width = '0%';
    $('#support-progress-pct').textContent = '0%';
    $('#support-result').style.display = 'none';
    open();

    try {
      const res = await fetch('/api/support-bundle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ anonymize }),
      });
      const job = await res.json();
      currentJobId = job.id;
      attachSSE(job.id);
    } catch (e) {
      $('#support-result').style.display = 'block';
      $('#support-result').innerHTML = `<div class="summary-bar bad">${e.message}</div>`;
    }
  }

  function attachSSE(jobId) {
    // The backend support-bundle stream replays from index 0 on each
    // connect (see api_support_bundle_stream in web/app.py), and step
    // rendering is idempotent (we update the existing <li> by data-step),
    // so SSEReconnect can transparently retry on connection loss.
    currentSSE = SSEReconnect.connect(`/api/support-bundle/${jobId}/stream`, {
      on: {
        step: (e) => {
          const ev = JSON.parse(e.data);
          let li = document.querySelector(`#support-steps li[data-step="${ev.step_id}"]`);
          if (!li) {
            li = document.createElement('li');
            li.dataset.step = ev.step_id;
            li.innerHTML = `<span class="ico"></span><span class="text"></span>`;
            $('#support-steps').appendChild(li);
          }
          li.classList.remove('running', 'done', 'error');
          li.classList.add(ev.status);
          const ico = li.querySelector('.ico');
          ico.textContent = ev.status === 'done' ? '✓' : ev.status === 'error' ? '✗' : '⏳';
          li.querySelector('.text').textContent = `${labelFor(ev.step_id)}${ev.message ? ' — ' + ev.message : ''}`;

          if (typeof ev.percent === 'number') {
            $('#support-progress-fill').style.width = ev.percent + '%';
            $('#support-progress-pct').textContent = ev.percent + '%';
            $('#support-bubble-pct').textContent = ev.percent + '%';
          }
        },
        end: async (e) => {
          const ev = JSON.parse(e.data);
          currentSSE = null;
          $('#support-result').style.display = 'block';
          if (ev.status === 'done') {
            let hasMapping = false;
            try {
              const job = await fetch(`/api/support-bundle/${currentJobId}`).then(r => r.json());
              hasMapping = !!job.has_mapping;
            } catch {}
            const mapBtn = hasMapping
              ? `<a class="btn btn-secondary btn-sm" href="/api/support-bundle/${currentJobId}/mapping" download
                    style="margin-left:6px;">🗝 ${i18n.t('settings.support.downloadMapping')}</a>`
              : '';
            $('#support-result').innerHTML = `
              <div class="summary-bar ok">✓ Bundle ready</div>
              <a class="btn btn-primary btn-sm" href="/api/support-bundle/${currentJobId}/download" download>
                ⬇ Download archive
              </a>${mapBtn}`;
          } else {
            $('#support-result').innerHTML = `<div class="summary-bar bad">✗ ${ev.error || 'Failed'}</div>`;
          }
        },
      },
      onStatus: (s) => {
        if (s.state === 'dead') {
          $('#support-result').style.display = 'block';
          $('#support-result').innerHTML = `<div class="summary-bar bad">✗ Stream lost — refresh and check past bundles</div>`;
        }
      },
    });
  }

  async function listPast() {
    const container = $('#bundle-history');
    if (!container) return;
    container.style.display = 'block';
    container.innerHTML = '<p class="hint">Loading...</p>';
    try {
      const data = await fetch('/api/support-bundle').then(r => r.json());
      const items = data.bundles || [];
      if (items.length === 0) {
        container.innerHTML = '<p class="hint">(no past bundles)</p>';
        return;
      }
      container.innerHTML = '';
      items.forEach(b => {
        const row = document.createElement('div');
        row.className = 'bundle-row';
        const sizeKB = (b.size / 1024).toFixed(1);
        const date = new Date(b.mtime * 1000).toLocaleString();
        row.innerHTML = `
          <div>
            <code>${b.filename}</code>
            <div style="color:var(--text-dim);font-size:10px;margin-top:2px;">${date} — ${sizeKB} KB ${b.anonymized ? '— anonymized' : ''}</div>
          </div>
          <a class="btn btn-sm btn-secondary" href="/api/support-bundle/${b.filename.replace(/^harvester-ops-bundle-[0-9]+-[0-9]+-/, '').replace(/-anon$|\.tar\.gz$/g, '')}/download" download>⬇</a>`;
        container.appendChild(row);
      });
    } catch (e) {
      container.innerHTML = `<p style="color:var(--danger)">${e.message}</p>`;
    }
  }

  // Drag
  function onDragStart(e) {
    if (e.target.closest('select, button, input, .support-header-actions')) return;
    const panel = $('#support-panel');
    const rect = panel.getBoundingClientRect();
    dragOffset = { x: e.clientX - rect.left, y: e.clientY - rect.top };
    panel.style.right = 'auto';
    document.addEventListener('mousemove', onDragMove);
    document.addEventListener('mouseup', onDragEnd, { once: true });
    e.preventDefault();
  }
  function onDragMove(e) {
    if (!dragOffset) return;
    const panel = $('#support-panel');
    panel.style.left = (e.clientX - dragOffset.x) + 'px';
    panel.style.top  = (e.clientY - dragOffset.y) + 'px';
  }
  function onDragEnd() {
    dragOffset = null;
    document.removeEventListener('mousemove', onDragMove);
  }

  // -------------------------------------------------------------------------
  // De-anonymize a log file (upload log + mapping → download restored file)
  // -------------------------------------------------------------------------
  async function deanonymize() {
    const logF = $('#deanon-log')?.files?.[0];
    const mapF = $('#deanon-mapping')?.files?.[0];
    const out  = $('#deanon-result');
    if (!logF || !mapF) {
      out.innerHTML = `<span style="color:var(--danger)">${i18n.t('settings.support.deanonNoFiles')}</span>`;
      return;
    }
    out.textContent = i18n.t('common.loading');
    const fd = new FormData();
    fd.append('log', logF);
    fd.append('mapping', mapF);
    try {
      const res = await fetch('/api/deanonymize', { method: 'POST', body: fd });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        out.innerHTML = `<span style="color:var(--danger)">✗ ${err.error || 'HTTP ' + res.status}</span>`;
        return;
      }
      const replaced = res.headers.get('X-Replaced-Entries') || '?';
      const blob = await res.blob();
      // Browser download
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = (logF.name.replace(/\.[^.]+$/, '') || 'log') + '_deanonymized.log';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      out.innerHTML = `<span style="color:var(--accent)">✓ ${i18n.t('settings.support.deanonOk').replace('{n}', replaced)}</span>`;
    } catch (e) {
      out.innerHTML = `<span style="color:var(--danger)">✗ ${e.message}</span>`;
    }
  }

  function init() {
    if (!$('#support-panel')) return;
    $('#btn-build-bundle')?.addEventListener('click', startBuild);
    $('#btn-show-bundles')?.addEventListener('click', listPast);
    $('#btn-support-min')?.addEventListener('click', minimize);
    $('#btn-support-close')?.addEventListener('click', close);
    $('#support-bubble')?.addEventListener('click', open);
    $('#support-drag-handle')?.addEventListener('mousedown', onDragStart);
    $('#btn-deanonymize')?.addEventListener('click', deanonymize);
  }

  return { init };
})();

document.addEventListener('DOMContentLoaded', Support.init);
