/**
 * harvester-ops : comptes d'automatisation des clusters enfants et jetons
 * courts (2.7.0, décision 125, point 208).
 *
 * Panneau de l'onglet Clusters de Cluster API : les comptes de service
 * déclarés par la console (élément hops-policies, lus sur Harvester), et
 * « Émettre un jeton » : durée de 600 à 86 400 s, jeton `TokenRequest`
 * montré UNE fois avec un bouton Copier, retiré de la page à la fermeture,
 * jamais réaffiché ni gardé dans une variable du module. Le serveur décide
 * du droit (opérateur et administrateur du cluster enfant) ; le rôle de la
 * session ne sert qu'à griser les boutons, avec la raison.
 * Voir docs/design/2026-11-07-artefacts-complets-defaire-270-plan.md (tâche 6).
 */
const CapiTokens = (() => {
  const enc = encodeURIComponent;
  const B = () => window.Board;
  const tr = (k, f, v) => B().tr(k, f, v);
  const esc = (v) => B().esc(v);
  const icon = (n, size) => (window.Icons ? Icons.svg(n, { size: size || 14 }) : '');
  const ROLE_RANK = { viewer: 1, operator: 2, admin: 3 };
  const can = (need) => (ROLE_RANK[window.App && App.role ? App.role() : null] || 0) >= ROLE_RANK[need];
  const MIN = 600;
  const MAX = 86400;
  const DEFAULT = 3600;
  let modal = null;
  let asked = 0;

  const box = () => document.getElementById('capi-tokens-body');
  const mgmt = () => (window.App && App.managementCluster ? App.managementCluster() : '');

  async function refresh(fresh = false) {
    const h = box();
    const c = mgmt();
    if (!h) return;
    if (!c) { h.innerHTML = `<p class="form-hint">${esc(tr('capi.new.pickCluster', 'Select a cluster'))}</p>`; return; }
    const n = ++asked;
    let d = null;
    try {
      const r = await fetch(`/api/capi/${enc(c)}/artefacts/applied${fresh ? '?fresh=1' : ''}`);
      d = await r.json().catch(() => null);
      if (!r.ok && d) d = { error: d.error || `HTTP ${r.status}` };
    } catch (e) { d = { error: e.message || String(e) }; }
    if (n !== asked) return;
    if (!d || d.error || d.unreachable) {
      h.innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(d && d.unreachable
        ? tr('fabric.unreachable', 'Cluster unreachable') : (d && d.error) || '?')}</div></div>`;
      return;
    }
    const accounts = d.accounts || [];
    if (!accounts.length) {
      h.innerHTML = `<p class="form-hint tok-empty">${esc(tr('tok.empty', 'No automation account yet. Declare them with the policies of a child cluster (plan phase Artefacts, or harvester-capi manifest-deploy).'))}</p>`;
      return;
    }
    const off = !can('operator');
    const tip = off ? tr('tok.t.needOperator', 'Needs the operator role, and the right to read the kubeconfig Secret of the child cluster')
      : tr('tok.t.issue', 'Issue a short token (TokenRequest) for this account; shown once');
    h.innerHTML = `<table class="data-table tok-table"><thead><tr>
        <th>${esc(tr('tok.col.cluster', 'Child cluster'))}</th><th>${esc(tr('tok.col.account', 'Account'))}</th>
        <th>${esc(tr('tok.col.role', 'Role'))}</th><th>${esc(tr('capi.v.col.actions', 'Actions'))}</th></tr></thead><tbody>${accounts.map(a => `
      <tr data-tok-row="${esc(a.cluster)}|${esc(a.namespace)}/${esc(a.name)}">
        <td><code>${esc(a.cluster)}</code></td><td><code>${esc(a.namespace)}/${esc(a.name)}</code></td>
        <td><span class="badge dim">${esc(a.role || '?')}</span></td>
        <td><span class="tip" data-tip="${esc(tip)}"><button type="button" class="btn btn-sm btn-primary" data-tok-issue
            data-cluster="${esc(a.cluster)}" data-account="${esc(a.namespace)}/${esc(a.name)}" ${off ? 'disabled' : ''}
            title="${esc(tip)}" aria-label="${esc(tip)}">${icon('key', 13)} ${esc(tr('tok.issue', 'Issue a token'))}</button></span></td>
      </tr>`).join('')}</tbody></table>`;
  }

  // Le jeton ne vit que dans le nœud de la fenêtre : effacé puis retiré à
  // la fermeture, jamais rangé ailleurs.
  function closeModal() {
    if (!modal) return;
    const t = modal.querySelector('[data-x="token"]');
    if (t) { t.value = ''; t.remove(); }
    modal.remove();
    modal = null;
  }

  function openIssue(child, account) {
    closeModal();
    const c = mgmt();
    modal = document.createElement('div');
    modal.className = 'modal-overlay active tok-modal';
    modal.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-header"><div><h3>${icon('key')} ${esc(tr('tok.title.issue', 'Issue a token'))}</h3>
          <div class="modal-subtitle">${esc(account)} · ${esc(child)}</div></div>
          <button type="button" class="btn-close tip" data-tok-close data-tip="${esc(tr('tok.t.close', 'Close: the token is removed from the page'))}"
                  title="${esc(tr('tok.t.close', 'Close: the token is removed from the page'))}">×</button></div>
        <div class="modal-body">
          <div class="form-row" data-x="ask"><label>${esc(tr('tok.duration', 'Duration (seconds)'))}</label>
            <input type="number" data-x="seconds" min="${MIN}" max="${MAX}" step="60" value="${DEFAULT}" class="tip"
                   data-tip="${esc(tr('tok.t.duration', 'From 600 s (10 min) to 86400 s (24 h); 3600 s by default'))}"
                   title="${esc(tr('tok.t.duration', 'From 600 s (10 min) to 86400 s (24 h); 3600 s by default'))}"></div>
          <p class="form-hint">${esc(tr('tok.intro', 'The token is shown once. It is never kept by the console: copy it now.'))}</p>
          <div data-x="result"></div>
        </div>
        <div class="modal-footer apply-bar" style="margin:0;">
          <button type="button" class="btn btn-secondary btn-sm tip" data-tok-close data-tip="${esc(tr('tok.t.close', 'Close: the token is removed from the page'))}"
                  title="${esc(tr('tok.t.close', 'Close: the token is removed from the page'))}">${esc(tr('tok.close', 'Close'))}</button>
          <button type="button" class="btn btn-primary btn-sm tip" data-tok-go data-tip="${esc(tr('tok.t.go', 'Ask the child cluster for a token of this duration'))}"
                  title="${esc(tr('tok.t.go', 'Ask the child cluster for a token of this duration'))}">${icon('key', 13)} ${esc(tr('tok.issue', 'Issue a token'))}</button>
          <span class="apply-result" data-x="feedback"></span>
        </div>
      </div>`;
    document.body.appendChild(modal);
    const m = modal;
    const q = (x) => m.querySelector(`[data-x="${x}"]`);
    m.addEventListener('click', (e) => { if (e.target === m || e.target.closest('[data-tok-close]')) closeModal(); });
    m.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });
    const go = m.querySelector('[data-tok-go]');
    go.addEventListener('click', async () => {
      const seconds = Number(q('seconds').value);
      if (!Number.isInteger(seconds) || seconds < MIN || seconds > MAX) {
        q('feedback').textContent = tr('tok.badDuration', 'The duration goes from 600 to 86400 seconds.');
        return;
      }
      go.disabled = true;
      q('feedback').textContent = tr('tok.asking', 'Asking the child cluster…');
      let r = null;
      let d = {};
      try {
        r = await fetch(`/api/capi/${enc(c)}/token`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, cache: 'no-store',
          body: JSON.stringify({ cluster: child, account, seconds }) });
        d = await r.json().catch(() => ({}));
      } catch (e) { d = { error: e.message || String(e) }; }
      if (modal !== m) return;                 // fermée entre-temps : rien n'est affiché
      if (!r || !r.ok || !d.token) {
        q('feedback').textContent = d.code === 'child-admin-required'
          ? tr('tok.childAdmin', 'Refused: you cannot read the kubeconfig Secret of {cluster} (administrator of the child cluster).', { cluster: child })
          : (d.error || (r ? `HTTP ${r.status}` : '?'));
        go.disabled = false;
        return;
      }
      q('feedback').textContent = '';
      go.remove();                              // un jeton par fenêtre : jamais réaffiché
      q('ask').remove();
      q('result').innerHTML = `
        <div class="ct-banner tok-once" role="status">${icon('warn')} <span>${esc(tr('tok.once', 'Shown once: it will not be displayed again.'))}</span></div>
        <textarea data-x="token" rows="4" readonly spellcheck="false" class="tok-value" aria-label="${esc(tr('tok.value', 'Token'))}"></textarea>
        <p class="form-hint">${esc(tr('tok.valid', 'Valid {n} s, plus up to one minute (expires at {at}).', { n: d.seconds || seconds, at: d.expires_at || '?' }))}</p>
        <button type="button" class="btn btn-sm btn-secondary tip" data-tok-copy data-tip="${esc(tr('tok.t.copy', 'Copy the token to the clipboard'))}"
                title="${esc(tr('tok.t.copy', 'Copy the token to the clipboard'))}">${icon('copy', 13)} ${esc(tr('tok.copy', 'Copy'))}</button>
        <span class="apply-result" data-x="copied"></span>`;
      // posé par value (jamais par innerHTML) : aucun balisage interprété
      q('token').value = d.token;
      d.token = null;
      m.querySelector('[data-tok-copy]').addEventListener('click', async () => {
        const t = q('token');
        if (!t) return;
        try {
          await navigator.clipboard.writeText(t.value);
        } catch {
          t.select();
          document.execCommand('copy');
        }
        q('copied').textContent = tr('tok.copied', 'Copied.');
      });
    });
    q('seconds').focus();
  }

  function init() {
    document.addEventListener('click', (e) => {
      if (e.target.closest('#btn-capi-tokens-refresh')) { refresh(true); return; }
      const b = e.target.closest('[data-tok-issue]');
      if (b && !b.disabled) openIssue(b.dataset.cluster, b.dataset.account);
    });
    // le rôle arrive après le premier rendu : les boutons suivent
    document.addEventListener('hops:role', () => { if (box() && box().childElementCount) refresh(); });
    // lu quand le panneau devient visible (onglet Clusters de Cluster API)
    const h = box();
    if (h && 'IntersectionObserver' in window) {
      new IntersectionObserver((entries) => {
        if (entries.some(x => x.isIntersecting)) refresh();
      }).observe(h);
    }
  }

  document.addEventListener('DOMContentLoaded', init);
  return { refresh, openIssue, close: closeModal };
})();
window.CapiTokens = CapiTokens;
