/**
 * harvester-ops — Bare-metal (BMC / Redfish) sub-tab.
 *
 * MVP: form-driven discovery of one or more BMCs (IP + creds), shows the
 * system summary + NIC MAC table. Per-row power actions (On/Off/Reset).
 * Full PXE/DHCP/HTTP boot story comes in a follow-up (task #56 part 2).
 */
const BMC = (() => {
  const $ = (s) => document.querySelector(s);

  function render() {
    const out = $('#bmc-body');
    if (!out) return;
    out.innerHTML = `
      <h4 style="margin-top:0;">Discovery</h4>
      <form id="bmc-discover-form" class="capi-form">
        <fieldset>
          <legend>Cible(s)</legend>
          <label style="grid-column:1/-1;">IPs ou hostnames (csv ou un par ligne) *
            <textarea name="hosts" rows="3" required
                      placeholder="192.0.2.10, 192.0.2.11"></textarea></label>
          <label>Username *
            <input name="user" required value="${guessUser()}"></label>
          <label>Password *
            <input name="password" type="password" required></label>
        </fieldset>
        <div class="apply-bar">
          <button type="submit" class="btn btn-primary btn-sm">🔍 Discover</button>
        </div>
      </form>
      <div id="bmc-discover-result" style="margin-top:12px;"></div>
      <p class="form-hint" style="margin-top:8px;">
        ℹ️ Supporte iLO 4/5, iDRAC 9 et tout BMC parlant le Redfish standard.
        Les credentials ne sont jamais persistés. Pour la suite (PXE/DHCP/HTTP +
        config par nœud), voir le wireframe de la tâche #56.
      </p>`;
  }

  function guessUser() {
    // Reasonable default for HP iLO; the field is editable.
    return 'admin';
  }

  async function discover(ev) {
    ev.preventDefault();
    const out = $('#bmc-discover-result');
    const fd = new FormData(ev.target);
    const hostsRaw = (fd.get('hosts') || '').toString();
    const hosts = hostsRaw.split(/[\s,;]+/).map(s => s.trim()).filter(Boolean);
    if (hosts.length === 0) { alert('Provide at least one BMC IP.'); return; }
    if (out) out.innerHTML = '<p class="form-hint">Discovering…</p>';
    try {
      const r = await fetch('/api/bmc/discover', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          hosts, user: fd.get('user'), password: fd.get('password'),
        }),
      });
      const d = await r.json();
      if (!r.ok) { if (out) out.innerHTML = `<div class="summary-bar bad">${d.error}</div>`; return; }
      if (out) out.innerHTML = renderResults(d.nodes || []);
    } catch (e) {
      if (out) out.innerHTML = `<div class="summary-bar bad">${e.message}</div>`;
    }
  }

  function renderResults(nodes) {
    const okCount  = nodes.filter(n => n.ok).length;
    const errCount = nodes.length - okCount;
    let html = `<div class="summary-bar ${errCount ? 'warn' : 'ok'}">
        ${okCount} / ${nodes.length} reachable BMC(s)${errCount ? ` · ${errCount} unreachable` : ''}
      </div>`;
    nodes.forEach(n => {
      if (!n.ok) {
        html += `<div class="card" style="margin-top:10px;">
          <div class="card-header"><h2><code>${n.host}</code> — <span class="err">${n.error || 'unreachable'}</span></h2></div>
        </div>`;
        return;
      }
      const nics = (n.nics || []).map(x => `
        <tr><td>${x.name}</td><td><code>${x.mac || '—'}</code></td>
            <td>${x.status || '—'}</td><td>${x.speed_mbps ? x.speed_mbps + ' Mbps' : '—'}</td></tr>`).join('');
      const powerCls = n.power_state === 'On' ? 'ok' : 'warn';
      html += `<div class="card" style="margin-top:10px;">
        <div class="card-header">
          <h2><code>${n.host}</code> — ${n.model || ''} <span class="badge ${powerCls}">${n.power_state || '?'}</span></h2>
          <span class="form-hint">SN: <code>${n.serial || '—'}</code> · BIOS: <code>${n.bios_version || '—'}</code> · ${n.memory_gib || '?'} GiB</span>
        </div>
        <div class="card-body">
          <div class="apply-bar" style="margin-bottom:8px;">
            <button class="btn btn-sm btn-secondary bmc-power" data-host="${n.host}" data-action="On"
                    title="Press the virtual power button (no-op if already On)">⚡ On</button>
            <button class="btn btn-sm btn-secondary bmc-power" data-host="${n.host}" data-action="GracefulShutdown"
                    title="Send ACPI shutdown to the OS">🛑 Graceful off</button>
            <button class="btn btn-sm btn-secondary bmc-power" data-host="${n.host}" data-action="ForceOff"
                    title="Cut power immediately (destructive)">💥 Force off</button>
            <button class="btn btn-sm btn-secondary bmc-power" data-host="${n.host}" data-action="GracefulRestart"
                    title="Send ACPI reboot to the OS">🔄 Restart</button>
          </div>
          <table class="data-table">
            <thead><tr><th>NIC</th><th>MAC</th><th>State</th><th>Speed</th></tr></thead>
            <tbody>${nics}</tbody>
          </table>
          <p class="form-hint" style="margin-top:8px;">UUID: <code>${n.uuid || '—'}</code></p>
        </div>
      </div>`;
    });
    return html;
  }

  async function power(host, action) {
    const fd = new FormData($('#bmc-discover-form'));
    if (!confirm(`Send "${action}" to ${host}?`)) return;
    try {
      const r = await fetch(`/api/bmc/${encodeURIComponent(host)}/power`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          action, user: fd.get('user'), password: fd.get('password'),
        }),
      });
      const d = await r.json();
      if (!r.ok) { alert('Power action failed: ' + (d.error || 'unknown')); return; }
      alert(`Action dispatched: ${d.action_id}. See the dock for progress.`);
    } catch (e) { alert(e.message); }
  }

  function init() {
    document.addEventListener('click', (e) => {
      if (e.target.closest('#btn-bmc-refresh')) render();
      const p = e.target.closest('.bmc-power');
      if (p) { e.preventDefault(); power(p.dataset.host, p.dataset.action); }
      if (e.target.closest('#tab-automation .sub-tab[data-subtab="pxe"]')) {
        setTimeout(render, 50);
      }
    });
    document.addEventListener('submit', (e) => {
      if (e.target?.id === 'bmc-discover-form') discover(e);
    });
  }

  return { init, render };
})();

document.addEventListener('DOMContentLoaded', BMC.init);
window.BMC = BMC;
