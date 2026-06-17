/**
 * harvester-ops — VM edit panel (stage 2: inline editing).
 *
 * Sections:
 *   - General   (description, runStrategy, raw annotations)
 *   - Compute   (CPU sockets/cores/threads, memory.guest, resources)
 *   - Lifecycle (terminationGracePeriodSeconds, evictionStrategy)
 *   - Disks     (YAML editor on volumes + domain.devices.disks)
 *   - Network   (YAML editor on networks + domain.devices.interfaces)
 *   - Cloud-init (separate Secret patch via /cloudinit endpoint)
 *
 * Apply: each section has its own Save button → POST /api/vm/.../? PATCH
 * with a JSON merge patch limited to that section. A "Dry-run" toggle
 * uses `kubectl --dry-run=server` to validate without writing.
 */
const VMEdit = (() => {
  const SECTIONS = [
    { id: 'general',   label: 'General',    icon: '📋' },
    { id: 'compute',   label: 'Compute',    icon: '🧠' },
    { id: 'disks',     label: 'Disks',      icon: '💾' },
    { id: 'network',   label: 'Network',    icon: '🔌' },
    { id: 'cloudinit', label: 'Cloud-init', icon: '☁️' },
    { id: 'lifecycle', label: 'Lifecycle',  icon: '🔁' },
  ];

  async function open(cluster, namespace, name) {
    const panelId = `vm-edit-${cluster}-${namespace}-${name}`;
    const title = `⚙ Edit — ${namespace}/${name}`;
    const html = `
      <div class="vm-edit-layout">
        <aside class="vm-edit-nav">
          ${SECTIONS.map(s => `
            <button data-section="${s.id}">
              <span class="ic">${s.icon}</span>
              <span>${s.label}</span>
            </button>`).join('')}
        </aside>
        <main class="vm-edit-content">
          <div class="vm-edit-loading">Loading VM spec…</div>
        </main>
      </div>`;

    const panel = FloatingPanels.open({
      id: panelId,
      title,
      bodyHtml: html,
      width: 920,
      height: 620,
      restoreSpec: { type: 'vm-edit', args: { cluster, namespace, name } },
    });

    let activeSection = 'general';
    let vmSpec = null;
    const navBtns = panel.el.querySelectorAll('.vm-edit-nav button');
    const setActive = (id) => {
      activeSection = id;
      navBtns.forEach(b => b.classList.toggle('active', b.dataset.section === id));
      renderSection();
    };
    navBtns.forEach(b => b.addEventListener('click', () => setActive(b.dataset.section)));
    navBtns[0].classList.add('active');

    const renderSection = () => {
      const content = panel.el.querySelector('.vm-edit-content');
      if (!vmSpec) { content.innerHTML = '<div class="vm-edit-loading">Loading…</div>'; return; }
      content.innerHTML = '';
      const sectionEl = document.createElement('section');
      sectionEl.className = 'vm-edit-section active';
      sectionEl.dataset.section = activeSection;
      sectionEl.innerHTML = renderSectionHtml(activeSection, vmSpec);
      content.appendChild(sectionEl);
      wireSection(sectionEl, activeSection, cluster, namespace, name);
    };

    try {
      vmSpec = await fetch(`/api/vm/${encodeURIComponent(cluster)}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`).then(r => {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      });
      renderSection();
    } catch (e) {
      panel.el.querySelector('.vm-edit-content').innerHTML =
        `<div class="vm-edit-section active"><div class="summary-bar bad">✗ ${e.message}</div></div>`;
    }

    // Expose a refresh callback for sub-handlers (after Apply)
    panel._refreshVM = async () => {
      try {
        vmSpec = await fetch(`/api/vm/${encodeURIComponent(cluster)}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`).then(r => r.json());
        renderSection();
      } catch (e) {}
    };
  }

  // ===========================================================================
  // Section renderers
  // ===========================================================================
  function renderSectionHtml(id, vm) {
    const spec     = vm.spec || {};
    const template = (spec.template || {}).spec || {};
    const domain   = template.domain || {};
    const annot    = (vm.metadata?.annotations) || {};

    switch (id) {
      case 'general': return renderGeneral(vm, spec, annot);
      case 'compute': return renderCompute(domain);
      case 'lifecycle': return renderLifecycle(template);
      case 'disks':   return renderYamlSection('disks', {
                       volumes: template.volumes || [],
                       disks: (domain.devices || {}).disks || []});
      case 'network': return renderYamlSection('network', {
                       networks: template.networks || [],
                       interfaces: (domain.devices || {}).interfaces || []});
      case 'cloudinit': return renderCloudInit();
      default: return '';
    }
  }

  function renderGeneral(vm, spec, annot) {
    const description = annot['harvesterhci.io/description'] || annot['description'] || '';
    return `
      <h3>General</h3>
      <div class="form-row">
        <label>Name</label>
        <input type="text" value="${esc(vm.metadata.name)}" readonly>
      </div>
      <div class="form-row">
        <label>Namespace</label>
        <input type="text" value="${esc(vm.metadata.namespace)}" readonly>
      </div>
      <div class="form-row">
        <label>Description (annotation harvesterhci.io/description)</label>
        <textarea data-field="annot.description" rows="2">${esc(description)}</textarea>
      </div>
      <div class="form-row">
        <label>Run strategy</label>
        <select data-field="spec.runStrategy">
          ${['Always','RerunOnFailure','Manual','Halted'].map(v =>
            `<option value="${v}" ${spec.runStrategy === v ? 'selected' : ''}>${v}</option>`).join('')}
        </select>
      </div>
      ${applyBar('general')}`;
  }

  function renderCompute(domain) {
    const cpu = domain.cpu || {};
    const mem = domain.memory || {};
    const res = (domain.resources || {}).requests || {};
    return `
      <h3>Compute resources</h3>
      <div class="grid-2">
        <div class="form-row">
          <label>CPU sockets</label>
          <input type="number" min="1" data-field="cpu.sockets" value="${cpu.sockets ?? 1}">
        </div>
        <div class="form-row">
          <label>CPU cores</label>
          <input type="number" min="1" data-field="cpu.cores" value="${cpu.cores ?? 1}">
        </div>
        <div class="form-row">
          <label>Threads per core</label>
          <input type="number" min="1" data-field="cpu.threads" value="${cpu.threads ?? 1}">
        </div>
        <div class="form-row">
          <label>Memory (guest, e.g. 4Gi / 4096Mi)</label>
          <input type="text" data-field="memory.guest" value="${esc(mem.guest || '')}" placeholder="4Gi">
        </div>
      </div>
      <h3>Current resources</h3>
      <pre>${esc(JSON.stringify({ requests: res }, null, 2))}</pre>
      <p class="form-hint">Changing CPU/memory while the VM is running may require a reboot for the guest to see the new values.</p>
      ${applyBar('compute')}`;
  }

  function renderLifecycle(template) {
    return `
      <h3>Lifecycle</h3>
      <div class="form-row">
        <label>terminationGracePeriodSeconds (seconds the guest has to ACPI shut down)</label>
        <input type="number" min="0" data-field="lifecycle.terminationGracePeriodSeconds" value="${template.terminationGracePeriodSeconds ?? 180}">
      </div>
      <div class="form-row">
        <label>evictionStrategy</label>
        <select data-field="lifecycle.evictionStrategy">
          <option value="">(none)</option>
          <option value="LiveMigrate"      ${template.evictionStrategy === 'LiveMigrate' ? 'selected' : ''}>LiveMigrate</option>
          <option value="External"         ${template.evictionStrategy === 'External' ? 'selected' : ''}>External</option>
          <option value="LiveMigrateIfPossible" ${template.evictionStrategy === 'LiveMigrateIfPossible' ? 'selected' : ''}>LiveMigrateIfPossible</option>
        </select>
      </div>
      ${applyBar('lifecycle')}`;
  }

  function renderYamlSection(id, obj) {
    return `
      <h3>${id === 'disks' ? 'Disks & Volumes' : 'Network interfaces'}</h3>
      <p class="form-hint">
        Edit the YAML fragment below. The change is applied as a JSON merge
        patch on the VM spec.template.spec.
        ${id === 'disks' ? '⚠ Adding/removing volumes typically requires a reboot.' : ''}
      </p>
      <textarea class="yaml-editor" data-yaml="${id}" spellcheck="false">${esc(toYaml(obj))}</textarea>
      ${applyBar(id)}`;
  }

  function renderCloudInit() {
    return `
      <h3>Cloud-init</h3>
      <p class="form-hint">Edit user-data and network-data. Saved to the VM's cloud-init Secret.</p>
      <div class="form-row">
        <label>user-data (YAML / shell script)</label>
        <textarea class="yaml-editor tall" data-ci="userData" spellcheck="false"></textarea>
      </div>
      <div class="form-row">
        <label>network-data (YAML)</label>
        <textarea class="yaml-editor" data-ci="networkData" spellcheck="false"></textarea>
      </div>
      <div class="form-hint" id="ci-source"></div>
      <div class="apply-bar">
        <button class="btn btn-primary btn-sm" data-action="apply-cloudinit">Save cloud-init</button>
        <button class="btn btn-secondary btn-sm" data-action="reload-cloudinit">Reload</button>
        <span class="apply-result" data-section="cloudinit"></span>
      </div>`;
  }

  function applyBar(section) {
    return `
      <div class="apply-bar">
        <label class="apply-dry">
          <input type="checkbox" data-dry-run> Dry-run (validate only)
        </label>
        <button class="btn btn-primary btn-sm" data-action="apply" data-section="${section}">Apply changes</button>
        <button class="btn btn-secondary btn-sm" data-action="reset" data-section="${section}">Reset</button>
        <span class="apply-result" data-section="${section}"></span>
      </div>`;
  }

  // ===========================================================================
  // Apply wiring
  // ===========================================================================
  function wireSection(sectionEl, sectionId, cluster, namespace, name) {
    if (sectionId === 'cloudinit') {
      loadCloudInit(sectionEl, cluster, namespace, name);
      sectionEl.querySelector('[data-action="apply-cloudinit"]').addEventListener('click', () =>
        applyCloudInit(sectionEl, cluster, namespace, name));
      sectionEl.querySelector('[data-action="reload-cloudinit"]').addEventListener('click', () =>
        loadCloudInit(sectionEl, cluster, namespace, name));
      return;
    }
    const applyBtn = sectionEl.querySelector('[data-action="apply"]');
    if (applyBtn) {
      applyBtn.addEventListener('click', async () => {
        const dryRun = !!sectionEl.querySelector('[data-dry-run]')?.checked;
        const result = sectionEl.querySelector('.apply-result');
        result.textContent = 'applying…';
        try {
          const patch = buildPatch(sectionEl, sectionId);
          const res = await fetch(`/api/vm/${encodeURIComponent(cluster)}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ patch, dry_run: dryRun }),
          });
          const d = await res.json();
          if (!res.ok) {
            result.innerHTML = `<span style="color:var(--danger)">✗ ${(d.detail || d.error || 'failed').slice(0, 200)}</span>`;
            return;
          }
          result.innerHTML = dryRun
            ? `<span style="color:var(--accent)">✓ dry-run OK</span>`
            : `<span style="color:var(--accent)">✓ applied</span>`;
          // Refresh the VM spec after apply
          if (!dryRun) {
            const panel = sectionEl.closest('.floating-panel');
            const ref = panel && panel._refreshVM;
            // Look up the open panel via FloatingPanels (panel reference is hidden inside)
            setTimeout(() => {
              const fp = document.querySelector(`#fp-vm-edit-${cluster}-${namespace}-${name}`);
              if (fp && fp._refreshVM) fp._refreshVM();
            }, 600);
          }
        } catch (e) {
          result.innerHTML = `<span style="color:var(--danger)">✗ ${e.message}</span>`;
        }
      });
    }
    const resetBtn = sectionEl.querySelector('[data-action="reset"]');
    if (resetBtn) {
      resetBtn.addEventListener('click', () => {
        // Trigger a re-render of this section from the current vmSpec
        const fp = document.querySelector(`#fp-vm-edit-${cluster}-${namespace}-${name}`);
        if (fp && fp._refreshVM) fp._refreshVM();
      });
    }
  }

  function buildPatch(sectionEl, sectionId) {
    const get = (selector) => sectionEl.querySelector(selector);
    const val = (field) => {
      const el = get(`[data-field="${field}"]`);
      return el ? (el.type === 'number' ? Number(el.value) : el.value) : undefined;
    };
    switch (sectionId) {
      case 'general':
        return {
          metadata: {
            annotations: { 'harvesterhci.io/description': val('annot.description') || '' },
          },
          spec: { runStrategy: val('spec.runStrategy') },
        };
      case 'compute':
        return {
          spec: { template: { spec: { domain: {
            cpu: {
              sockets: val('cpu.sockets'),
              cores:   val('cpu.cores'),
              threads: val('cpu.threads'),
            },
            memory: { guest: val('memory.guest') },
          } } } },
        };
      case 'lifecycle':
        return {
          spec: { template: { spec: {
            terminationGracePeriodSeconds: val('lifecycle.terminationGracePeriodSeconds'),
            evictionStrategy: val('lifecycle.evictionStrategy') || null,
          } } },
        };
      case 'disks': {
        const txt = get('[data-yaml="disks"]').value;
        const obj = fromYaml(txt);
        return {
          spec: { template: { spec: {
            volumes: obj.volumes || [],
            domain: { devices: { disks: obj.disks || [] } },
          } } },
        };
      }
      case 'network': {
        const txt = get('[data-yaml="network"]').value;
        const obj = fromYaml(txt);
        return {
          spec: { template: { spec: {
            networks: obj.networks || [],
            domain: { devices: { interfaces: obj.interfaces || [] } },
          } } },
        };
      }
      default:
        return {};
    }
  }

  async function loadCloudInit(sectionEl, cluster, namespace, name) {
    const out = sectionEl.querySelector('#ci-source');
    out.textContent = 'loading…';
    try {
      const d = await fetch(`/api/vm/${encodeURIComponent(cluster)}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/cloudinit`).then(r => r.json());
      sectionEl.querySelector('[data-ci="userData"]').value    = d.userData || '';
      sectionEl.querySelector('[data-ci="networkData"]').value = d.networkData || '';
      out.innerHTML = d.source === 'secret'
        ? `source: Secret <code>${esc(d.secretName)}</code>`
        : d.source === 'inline'
          ? `<span style="color:var(--warn)">inline cloud-init (read-only)</span>`
          : '<span style="color:var(--text-dim)">no cloud-init configured</span>';
    } catch (e) {
      out.innerHTML = `<span style="color:var(--danger)">✗ ${e.message}</span>`;
    }
  }

  async function applyCloudInit(sectionEl, cluster, namespace, name) {
    const result = sectionEl.querySelector('.apply-result');
    result.textContent = 'saving…';
    const body = {
      userData:    sectionEl.querySelector('[data-ci="userData"]').value,
      networkData: sectionEl.querySelector('[data-ci="networkData"]').value,
    };
    try {
      const res = await fetch(`/api/vm/${encodeURIComponent(cluster)}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/cloudinit`,
        { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const d = await res.json();
      if (!res.ok) {
        result.innerHTML = `<span style="color:var(--danger)">✗ ${d.error || res.status}</span>`;
      } else {
        result.innerHTML = `<span style="color:var(--accent)">✓ saved to Secret ${esc(d.secret)}</span>`;
      }
    } catch (e) {
      result.innerHTML = `<span style="color:var(--danger)">✗ ${e.message}</span>`;
    }
  }

  // Naïve YAML helpers (we don't bundle js-yaml — kept simple)
  function toYaml(obj) {
    return JSON.stringify(obj, null, 2);
  }
  function fromYaml(text) {
    return JSON.parse(text);
  }
  function esc(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  return { open };
})();

window.VMEdit = VMEdit;
FloatingPanels.registerType('vm-edit', (args) =>
  VMEdit.open(args.cluster, args.namespace, args.name));
