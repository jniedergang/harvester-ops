/* harvester-ops — frontend application
   Vanilla JS, no framework. Multi-cluster, tabs, SSE-driven step indicators. */

const App = (() => {
  let currentCluster = null;
  let currentNamespace = null;
  let currentSSE = null;
  let statusRefreshTimer = null;
  let nsRefreshTimer = null;

  // -------------------------------------------------------------------------
  // DOM helpers
  // -------------------------------------------------------------------------
  const $  = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  const TAB_STORAGE = 'harvester_ops_current_tab';

  function setTab(name) {
    // Highlight every `.tab` whose data-tab matches the current name. The
    // Automation group keeps "broad-highlight" off by *not* carrying
    // data-tab on its children — they share routing with the head but get
    // their own .sub-active class via the group's switcher. Cluster
    // children have UNIQUE data-tab values per child, so this selector
    // highlights exactly one of them at a time. (Don't filter out
    // .tab-child here or Cluster's Démarrage/Arrêt children stop
    // lighting up when selected.)
    $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
    $$('.tab-content').forEach(c => c.classList.toggle('active', c.id === 'tab-' + name));
    try { localStorage.setItem(TAB_STORAGE, name); } catch {}
    if (name === 'overview')   refreshStatus();
    if (name === 'namespaces') { refreshNamespaces(true); }
    if (name === 'activity')   refreshActivity();
    if (name === 'shutdown')   loadVMOrder();
  }

  function setStepStatus(panelId, stepId, status, msg) {
    const li = $(`#${panelId} li[data-step="${stepId}"]`);
    if (!li) return;
    li.classList.remove('running', 'done', 'error', 'skipped', 'progress', 'warn');
    if (status) li.classList.add(status);
    if (msg !== undefined) li.querySelector('.msg').textContent = msg;
  }

  function resetSteps(panelId) {
    $$(`#${panelId} li.step`).forEach(li => {
      li.classList.remove('running', 'done', 'error', 'skipped', 'progress', 'warn');
      li.querySelector('.msg').textContent = '';
    });
  }

  function appendLog(targetSel, message, level) {
    const el = $(targetSel);
    if (!el) return;
    const span = document.createElement('span');
    if (level) span.className = level;
    span.textContent = message + '\n';
    el.appendChild(span);
    el.scrollTop = el.scrollHeight;
  }

  // -------------------------------------------------------------------------
  // API helpers
  // -------------------------------------------------------------------------
  async function api(path, opts = {}) {
    const res = await fetch(path, { ...opts, headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) } });
    if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
    return res.json();
  }

  // -------------------------------------------------------------------------
  // Cluster selection
  // -------------------------------------------------------------------------
  function setCluster(name) {
    currentCluster = name;
    $$('.cluster-name').forEach(el => el.textContent = name);
    // v1.4.32: persist the selection so F5 restores the same cluster
    // (was always defaulting to the first <select> option).
    try { localStorage.setItem('harvester_ops_current_cluster', name); } catch {}
    refreshStatus();
  }

  // -------------------------------------------------------------------------
  // Overview tab
  // -------------------------------------------------------------------------
  async function refreshStatus() {
    if (!currentCluster) return;
    try {
      const data = await api(`/api/status/${encodeURIComponent(currentCluster)}`);
      // Clear any previous error banner on a successful refresh
      const old = $('#status-error-banner');
      if (old) old.remove();
      const s = data.summary || {};
      $('#m-nodes').textContent = `${s.nodes_ready || 0} / ${s.nodes_total || 0}`;
      $('#m-vms').textContent = `${s.vms_running || 0} / ${s.vms_total || 0}`;
      const vol = data.longhorn?.volumes_by_state || {};
      $('#m-volumes').textContent = Object.entries(vol).map(([k, v]) => `${v} ${k}`).join(', ') || '–';
      $('#m-rebuild').textContent = data.longhorn?.concurrent_rebuild_limit ?? '–';

      const tbody = $('#nodes-table tbody');
      tbody.innerHTML = '';
      (data.nodes || []).forEach(n => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td>${n.name}</td>
          <td>${n.ready === 'True' ? '<span class="phase Running">Ready</span>' : '<span class="phase Failed">NotReady</span>'}</td>
          <td>${n.schedulable ? '✓' : '<span class="phase Stopped">Cordoned</span>'}</td>
          <td>${(n.roles || []).join(', ')}</td>
          <td><button class="btn-node-notes tip" data-tip-i18n="overview.nodeNotesTip" data-node="${n.name}" title="${i18n.t('overview.nodeNotesTip')}">📝</button></td>`;
        tr.querySelector('.btn-node-notes').addEventListener('click', () => {
          window.Notes && Notes.open('node', currentCluster, n.name);
        });
        tbody.appendChild(tr);
      });
    } catch (e) {
      console.warn('status refresh failed', e);
      // Surface a banner on the Overview tab so the user sees WHY the
      // metrics are blank, instead of staring at "–" placeholders.
      const banner = $('#status-error-banner') || (() => {
        const b = document.createElement('div');
        b.id = 'status-error-banner';
        b.className = 'status-error-banner';
        const overview = $('#tab-overview');
        if (overview) overview.insertBefore(b, overview.firstChild);
        return b;
      })();
      let detail = e.message || String(e);
      try {
        const parsed = JSON.parse(detail.split('\n').slice(-1)[0] || '{}');
        if (parsed.hint) detail = parsed.hint;
        else if (parsed.stderr) detail = parsed.stderr;
      } catch {}
      banner.textContent = `⚠️ ${i18n.t('overview.statusError')}: ${detail}`;
    }
  }

  // -------------------------------------------------------------------------
  // Namespaces tab
  // -------------------------------------------------------------------------
  // -------------------------------------------------------------------------
  // Namespaces tab — with bulk actions, per-row runStrategy, auto-select first
  // -------------------------------------------------------------------------
  const nsSelection = new Set();   // "ns/name" strings
  let nsSortKey = 'name';
  let nsSortDir = 'asc';            // 'asc' | 'desc'

  function sortVMs(vms) {
    const cmp = (a, b) => {
      let av, bv;
      switch (nsSortKey) {
        case 'phase':
          av = (a.phase || ''); bv = (b.phase || ''); break;
        case 'runStrategy':
          av = (a.runStrategy || ''); bv = (b.runStrategy || ''); break;
        default:
          av = a.name; bv = b.name;
      }
      const r = av.localeCompare(bv);
      return nsSortDir === 'asc' ? r : -r;
    };
    return [...vms].sort(cmp);
  }

  async function refreshNamespaces(autoSelectFirst = false) {
    if (!currentCluster) return;
    try {
      const data = await api(`/api/vms/${encodeURIComponent(currentCluster)}`);
      const groups = {};
      (data.vms || []).forEach(vm => {
        (groups[vm.namespace] = groups[vm.namespace] || []).push(vm);
      });

      // Populate the namespace dropdown
      const sortedNs = Object.keys(groups).sort();
      const sel = $('#ns-dropdown');
      if (sel) {
        const prev = sel.value;
        sel.innerHTML = sortedNs.map(ns =>
          `<option value="${ns}">${ns} (${groups[ns].length})</option>`).join('');
        // Restore selection or auto-pick first
        if (currentNamespace && groups[currentNamespace]) {
          sel.value = currentNamespace;
        } else if (autoSelectFirst && sortedNs.length > 0) {
          sel.value = sortedNs[0];
          currentNamespace = sortedNs[0];
        } else if (prev && groups[prev]) {
          sel.value = prev;
          currentNamespace = prev;
        }
      }

      // Update count badge
      if (currentNamespace) {
        const list = groups[currentNamespace] || [];
        const badge = $('#ns-vm-count');
        if (badge) badge.textContent = i18n.t('vms.count').replace('{n}', list.length);
        renderNamespaceDetail(list);
      }
    } catch (e) {
      console.warn('vms refresh failed', e);
    }
  }

  async function selectNamespace(ns, preloaded) {
    currentNamespace = ns;
    nsSelection.clear();
    const sel = $('#ns-dropdown');
    if (sel && sel.value !== ns) sel.value = ns;

    let vms;
    if (preloaded && preloaded[ns]) {
      vms = preloaded[ns];
    } else {
      const data = await api(`/api/vms/${encodeURIComponent(currentCluster)}`);
      vms = (data.vms || []).filter(v => v.namespace === ns);
    }
    const badge = $('#ns-vm-count');
    if (badge) badge.textContent = i18n.t('vms.count').replace('{n}', vms.length);
    renderNamespaceDetail(vms);
  }

  function renderNamespaceDetail(vms) {
    const tbody = $('#ns-vms-table tbody');
    tbody.innerHTML = '';
    // Apply current sort
    vms = sortVMs(vms);
    // Update header sort indicators
    $$('#ns-vms-table th.sortable').forEach(th => {
      const arrow = th.querySelector('.sort-arrow');
      const isActive = th.dataset.sortBy === nsSortKey;
      arrow.textContent = isActive ? (nsSortDir === 'asc' ? '↑' : '↓') : '';
      th.classList.toggle('active-sort', isActive);
    });
    vms.forEach(vm => {
      const key = `${vm.namespace}/${vm.name}`;
      const tr = document.createElement('tr');
      tr.dataset.key = key;
      if (nsSelection.has(key)) tr.classList.add('selected');

      const phase = vm.phase || 'Stopped';
      const phaseTip = i18n.t(`vm.tooltip.${phase}`);
      const rsTip    = i18n.t(`vm.tooltip.${vm.runStrategy}`);

      tr.innerHTML = `
        <td class="check-col"><input type="checkbox" data-key="${key}" ${nsSelection.has(key) ? 'checked' : ''}></td>
        <td>${vm.name}
          ${vm.agent_connected === 'True' ? '<span class="agent-dot ok" title="qemu-guest-agent connected">●</span>' : ''}
        </td>
        <td><span class="phase ${phase} tip" data-tip="${phaseTip}">${i18n.t('vm.state.' + phase)}</span></td>
        <td>
          <select class="strategy-select tip" data-tip="${rsTip}" data-ns="${vm.namespace}" data-name="${vm.name}">
            <option value="Always"          ${vm.runStrategy === 'Always' ? 'selected' : ''}>Always</option>
            <option value="RerunOnFailure"  ${vm.runStrategy === 'RerunOnFailure' ? 'selected' : ''}>RerunOnFailure</option>
            <option value="Manual"          ${vm.runStrategy === 'Manual' ? 'selected' : ''}>Manual</option>
            <option value="Halted"          ${vm.runStrategy === 'Halted' ? 'selected' : ''}>Halted</option>
          </select>
        </td>
        <td class="vm-actions-cell">
          ${vm.runStrategy === 'Halted'
            ? `<button class="btn-icon-action start" title="Start the VM (set runStrategy=Always and wait for Running phase)" data-action="start" data-ns="${vm.namespace}" data-name="${vm.name}"><span class="icon-green">▶</span></button>`
            : `<button class="btn-icon-action stop"  title="Stop the VM gracefully (set runStrategy=Halted, ACPI shutdown)"  data-action="stop"  data-ns="${vm.namespace}" data-name="${vm.name}"><span class="icon-red">■</span></button>`}
          <button class="btn-icon-action snapshot" title="Manage VM snapshots (create, restore, delete VirtualMachineBackup)"
                  data-vm-snapshot data-ns="${vm.namespace}" data-name="${vm.name}">📸</button>
          <button class="btn-icon-action migrate"  title="Live-migrate this VM to another node (only when Running)"
                  data-vm-migrate data-ns="${vm.namespace}" data-name="${vm.name}">🔄</button>
          <button class="btn-icon-action edit"    title="Edit VM settings (CPU, memory, disks, network, cloud-init)"
                  data-vm-edit data-ns="${vm.namespace}" data-name="${vm.name}">⚙</button>
          <button class="btn-icon-action console" title="Open VM console (VNC / serial)"
                  data-vm-console data-ns="${vm.namespace}" data-name="${vm.name}">🖥</button>
          <button class="btn-icon-action notes"   title="Open collaborative notes for this VM (real-time multi-user)"
                  data-vm-notes data-ns="${vm.namespace}" data-name="${vm.name}">📝</button>
        </td>`;

      tr.querySelector('input[type="checkbox"]').addEventListener('change', (e) => {
        if (e.target.checked) nsSelection.add(key); else nsSelection.delete(key);
        tr.classList.toggle('selected', e.target.checked);
        updateBulkToolbar();
      });
      tr.querySelector('select').addEventListener('change', (e) => {
        changeRunStrategy(vm.namespace, vm.name, e.target.value);
      });
      tr.querySelector('.btn-icon-action.start, .btn-icon-action.stop')?.addEventListener('click', (e) => {
        const action = e.currentTarget.dataset.action;
        changeRunStrategy(vm.namespace, vm.name, action === 'start' ? 'Always' : 'Halted');
      });
      tr.querySelector('[data-vm-edit]')?.addEventListener('click', () => {
        if (window.VMEdit) window.VMEdit.open(currentCluster, vm.namespace, vm.name);
      });
      tr.querySelector('[data-vm-console]')?.addEventListener('click', () => {
        if (window.VMConsole) window.VMConsole.open(currentCluster, vm.namespace, vm.name);
      });
      tr.querySelector('[data-vm-notes]')?.addEventListener('click', () => {
        if (window.Notes) window.Notes.open('vm', currentCluster, vm.namespace, vm.name);
      });
      tr.querySelector('[data-vm-snapshot]')?.addEventListener('click', () => {
        if (window.VMSnapshots) window.VMSnapshots.open(currentCluster, vm.namespace, vm.name);
      });
      tr.querySelector('[data-vm-migrate]')?.addEventListener('click', () => {
        if (window.VMMigrate) window.VMMigrate.open(currentCluster, vm.namespace, vm.name);
      });
      tbody.appendChild(tr);
    });
    updateBulkToolbar();
  }

  function updateBulkToolbar() {
    const bar = $('#bulk-toolbar');
    if (!bar) return;
    if (nsSelection.size > 0) {
      bar.style.display = 'flex';
      $('#bulk-count-num').textContent = nsSelection.size;
    } else {
      bar.style.display = 'none';
    }
  }

  async function changeRunStrategy(ns, name, target) {
    try {
      await fetch(`/api/vm/${encodeURIComponent(currentCluster)}/${encodeURIComponent(ns)}/${encodeURIComponent(name)}/runStrategy`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ runStrategy: target }),
      });
      // Brief refresh
      setTimeout(() => selectNamespace(currentNamespace), 600);
    } catch (e) {
      alert('PATCH failed: ' + e.message);
    }
  }

  async function bulkAction(target) {
    if (nsSelection.size === 0) return;
    if (!confirm(`Apply runStrategy=${target} to ${nsSelection.size} VMs?`)) return;
    const log = $('#ns-action-log');
    log.innerHTML = '';
    for (const key of nsSelection) {
      const [ns, name] = key.split('/');
      const line = document.createElement('div');
      line.textContent = `→ ${ns}/${name}: ${target}...`;
      log.appendChild(line);
      try {
        await fetch(`/api/vm/${encodeURIComponent(currentCluster)}/${encodeURIComponent(ns)}/${encodeURIComponent(name)}/runStrategy`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ runStrategy: target }),
        });
        line.textContent += ' ✓';
      } catch (e) {
        line.textContent += ' ✗ ' + e.message;
      }
    }
    setTimeout(() => selectNamespace(currentNamespace), 800);
  }

  // -------------------------------------------------------------------------
  // VM order — drag-and-drop sortable list + save + filter/sort
  // -------------------------------------------------------------------------
  let currentVMs = [];   // [{namespace, name, priority, snapshot, runStrategy, phase}, ...]
  let dragSrc = null;
  let nsFilter = '';
  let sortBy   = 'priority';
  // Empty groups created by the user that don't have VMs yet — survives
  // re-renders so the section doesn't disappear while user is configuring.
  let pendingEmptyGroups = [];
  // Collapse state per group name, persisted in memory across re-renders.
  let groupCollapsed = {};
  // Set when the user has unsaved local edits (drag, rename, snapshot
  // toggle). While dirty, the 8s periodic refresh skips loadVMOrder() so
  // we don't clobber the user's intent with server state. Cleared on save
  // and on explicit Reload.
  let vmOrderDirty = false;
  const markVmOrderDirty = () => {
    vmOrderDirty = true;
    const sv = $('#btn-vms-save');
    if (sv) sv.classList.add('has-changes');
  };
  const clearVmOrderDirty = () => {
    vmOrderDirty = false;
    const sv = $('#btn-vms-save');
    if (sv) sv.classList.remove('has-changes');
  };

  async function loadVMOrder() {
    if (!currentCluster) return;
    try {
      const data = await api(`/api/vms/${encodeURIComponent(currentCluster)}`);
      currentVMs = data.vms || [];
      pendingEmptyGroups = [];   // server is the source of truth on a fresh load
      clearVmOrderDirty();
      // Build namespace filter dropdown
      const nsSet = new Set(currentVMs.map(v => v.namespace));
      const sel = $('#vm-order-filter-ns');
      if (sel) {
        const prev = sel.value;
        sel.innerHTML = `<option value="">— ${i18n.t('shutdown.allNamespaces')} —</option>` +
          [...nsSet].sort().map(ns => `<option value="${ns}" ${ns === prev ? 'selected' : ''}>${ns}</option>`).join('');
        nsFilter = sel.value;
      }
      renderVMOrder();
    } catch (e) { console.warn('vms load failed', e); }
  }

  function getDisplayedVMs() {
    let list = nsFilter ? currentVMs.filter(v => v.namespace === nsFilter) : [...currentVMs];
    switch (sortBy) {
      case 'namespace':
        list.sort((a, b) => (a.namespace + '/' + a.name).localeCompare(b.namespace + '/' + b.name));
        break;
      case 'name':
        list.sort((a, b) => a.name.localeCompare(b.name));
        break;
      case 'phase':
        list.sort((a, b) => (a.phase || '').localeCompare(b.phase || ''));
        break;
      default: // priority
        list.sort((a, b) => (a.priority - b.priority) || a.name.localeCompare(b.name));
    }
    return list;
  }

  // -------------------------------------------------------------------------
  // Groups model (v1.4.14)
  //   - Each group has its own `group_priority`. Groups sharing the same
  //     group_priority execute IN PARALLEL. Default 100 for all groups
  //     ⇒ everything parallel between groups unless explicitly changed.
  //   - Within a NORMAL group: VMs ordered by `priority` (intra-group).
  //     The first VM in the list stops first, then the next, sequentially.
  //   - Within "default": all VMs in parallel. intra-priority ignored.
  //   - "default" group is special: locked name, no delete, always shown
  //     even if empty.
  // -------------------------------------------------------------------------
  const DEFAULT_GROUP_PRIO = 100;
  function buildGroupsFromVMs() {
    const byGroup = new Map();   // gName → {name, group_priority, vms[]}
    getDisplayedVMs().forEach(vm => {
      const g = vm.group || 'default';
      let entry = byGroup.get(g);
      if (!entry) {
        entry = {
          name: g,
          group_priority: (vm.group_priority ?? DEFAULT_GROUP_PRIO),
          vms: [],
        };
        byGroup.set(g, entry);
      }
      // If a group's VMs disagree on group_priority (shouldn't happen
      // after a clean save, but defensive), take the min — pessimistic
      // assumption that the group runs earlier than expected.
      if ((vm.group_priority ?? DEFAULT_GROUP_PRIO) < entry.group_priority) {
        entry.group_priority = vm.group_priority;
      }
      entry.vms.push(vm);
    });
    // Always show the default group, even if empty.
    if (!byGroup.has('default')) {
      byGroup.set('default', { name: 'default', group_priority: DEFAULT_GROUP_PRIO, vms: [] });
    }
    // User-created empty groups
    pendingEmptyGroups.forEach(g => {
      if (!byGroup.has(g.name)) {
        byGroup.set(g.name, {
          name: g.name,
          group_priority: g.group_priority ?? DEFAULT_GROUP_PRIO,
          vms: [],
        });
      }
    });
    // For each NORMAL group, sort VMs by intra-priority then name.
    // Default: keep alphabetical (parallel — order irrelevant for execution).
    for (const g of byGroup.values()) {
      if (g.name === 'default') {
        g.vms.sort((a, b) => a.name.localeCompare(b.name));
      } else {
        g.vms.sort((a, b) => (a.priority - b.priority) || a.name.localeCompare(b.name));
      }
    }
    return [...byGroup.values()].sort((a, b) =>
      (a.group_priority - b.group_priority) || a.name.localeCompare(b.name)
    );
  }

  // Hash a priority value to a stable hue so same-priority groups share
  // the same color chip. 0-360 range, skip yellow-greens that clash with
  // the "ok" status badges (≈80-140).
  function priorityHue(p) {
    let h = 0;
    const s = String(p);
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    h = h % 280;             // 0..279
    if (h >= 80) h += 60;    // shift past the green zone → 0..79 + 140..339
    return h;
  }

  function _vmRow(vm, parentIsDefault) {
    const li = document.createElement('li');
    li.draggable = true;
    li.dataset.ns = vm.namespace;
    li.dataset.name = vm.name;
    if (parentIsDefault) li.classList.add('in-default');
    const snapId = `snap-${vm.namespace}-${vm.name}`.replace(/[^a-z0-9-]/gi, '_');
    const rsLabel  = i18n.t(`vm.state.${vm.runStrategy}`);
    const rsTip    = i18n.t(`vm.tooltip.${vm.runStrategy}`);
    const phaseRaw = vm.phase || 'Stopped';
    const phaseLabel = i18n.t(`vm.state.${phaseRaw}`);
    const phaseTip   = i18n.t(`vm.tooltip.${phaseRaw}`);
    const agentBadge = (vm.agent_connected === 'True')
      ? `<span class="agent-dot ok" title="qemu-guest-agent connected">●</span>`
      : (phaseRaw === 'Running'
          ? `<span class="agent-dot warn" title="qemu-guest-agent not connected">●</span>`
          : '');
    // Intra-priority badge: only meaningful in non-default groups.
    // Placed in the SECOND grid column (right after the drag handle) so
    // the row reads "⠿ #N name ... state snap" left-to-right.
    const intraBadge = parentIsDefault
      ? ''
      : `<span class="vm-intra tip" data-tip-i18n="shutdown.intraPriorityTip" title="${i18n.t('shutdown.intraPriorityTip')}">#${vm.priority ?? 10}</span>`;
    const handleTipKey = parentIsDefault ? 'shutdown.dragVmDefaultTip' : 'shutdown.dragVmTip';
    li.innerHTML = `
      <span class="drag-handle tip" data-tip-i18n="${handleTipKey}" title="${i18n.t(handleTipKey)}">⠿</span>
      ${intraBadge}
      <span class="vm-name">${agentBadge}<small>${vm.namespace}/</small>${vm.name}</span>
      <span class="vm-state-cell">
        <span class="phase ${phaseRaw} tip" data-tip="${phaseTip}">${phaseLabel}</span>
        <span class="running-state ${vm.runStrategy} tip" data-tip="${rsTip}">${rsLabel}</span>
      </span>
      <span class="vm-snap">
        <label><input type="checkbox" id="${snapId}" ${vm.snapshot ? 'checked' : ''}> <span data-i18n="vm.snap">snap</span></label>
      </span>`;
    li.addEventListener('dragstart', onVmDragStart);
    li.addEventListener('dragend',   onVmDragEnd);
    // Row-level drop only matters for intra-group reorder (non-default).
    // For default rows we still allow dropping to receive cross-group
    // drops, handled at the section level via onGroupDrop.
    if (!parentIsDefault) {
      li.addEventListener('dragover',  onVmRowDragOver);
      li.addEventListener('dragleave', onVmRowDragLeave);
      li.addEventListener('drop',      onVmRowDrop);
    }
    li.querySelector(`#${snapId}`).addEventListener('change', (e) => {
      const v = currentVMs.find(x => x.namespace === vm.namespace && x.name === vm.name);
      if (v) { v.snapshot = e.target.checked; markVmOrderDirty(); }
    });
    return li;
  }

  // Escape user-controlled values for an HTML attribute context. Group
  // names ultimately become annotation values + DOM attribute values, so
  // we keep them safe even though the backend regex narrows the surface.
  function escapeAttr(s) {
    return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;')
      .replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function renderVMOrder() {
    const host = $('#vm-order-list');
    if (!host) return;
    host.innerHTML = '';
    const help = $('.priority-help');
    if (help) help.setAttribute('data-tip', i18n.t('shutdown.priorityTip'));

    const groups = buildGroupsFromVMs();
    // Group `default` is always present (buildGroupsFromVMs guarantees it),
    // so an empty-after-default-only state only happens with zero VMs.
    const hasAnyVm = groups.some(g => g.vms.length > 0);

    // Tier index: how many distinct group_priority levels are shared?
    const tierMap = new Map();          // gprio → tier index (1..N)
    [...new Set(groups.map(g => g.group_priority))]
      .sort((a, b) => a - b)
      .forEach((p, i) => tierMap.set(p, i + 1));

    groups.forEach((g, gIdx) => {
      const section = document.createElement('section');
      const isDefault = g.name === 'default';
      const collapsed = !!groupCollapsed[g.name];
      const tier = tierMap.get(g.group_priority);
      const hue = priorityHue(g.group_priority);
      section.className = 'vm-group'
        + (isDefault ? ' is-default' : '')
        + (collapsed ? ' collapsed' : '');
      section.dataset.group = g.name;
      section.dataset.groupPriority = g.group_priority;
      section.style.setProperty('--prio-hue', String(hue));
      const tipKey = isDefault ? 'shutdown.defaultGroupTip' : 'shutdown.groupTip';
      const badge = isDefault
        ? `<span class="group-mode-badge default tip" data-tip-i18n="shutdown.defaultGroupTip">⚡ ${i18n.t('shutdown.parallelLabel')}</span>`
        : `<span class="group-mode-badge ordered tip" data-tip-i18n="shutdown.orderedLabelTip">▶ ${i18n.t('shutdown.orderedLabel')}</span>`;
      const sizeText = g.vms.length === 0
        ? i18n.t('shutdown.groupEmpty')
        : (g.vms.length + ' VM' + (g.vms.length > 1 ? 's' : ''));
      section.innerHTML = `
        <header class="vm-group-head">
          <button type="button" class="group-collapse tip" data-tip-i18n="shutdown.collapseTip"
                  title="${i18n.t('shutdown.collapseTip')}">${collapsed ? '▶' : '▼'}</button>
          <input class="group-name" value="${escapeAttr(g.name)}"
                 placeholder="${i18n.t('shutdown.groupNamePlaceholder')}"
                 ${isDefault ? 'disabled aria-readonly="true"' : ''}
                 title="${isDefault ? i18n.t('shutdown.defaultLockedTip') : i18n.t('shutdown.groupNameTip')}" />
          ${badge}
          <span class="group-size">${sizeText}</span>
          <label class="group-prio-input tip" data-tip-i18n="shutdown.groupPriorityTip"
                 title="${i18n.t('shutdown.groupPriorityTip')}">
            <span class="prio-label" data-i18n="shutdown.groupPriorityLabel">priority</span>
            <input type="number" class="group-prio-value" min="0" step="10"
                   value="${g.group_priority}" />
            <span class="tier-chip" data-tier="${tier}"
                  title="${i18n.t('shutdown.tierTip').replace('{n}', String(tier))}">T${tier}</span>
          </label>
          <span class="group-info tip" data-tip-i18n="${tipKey}">ⓘ</span>
          ${isDefault ? '' : `<button type="button" class="btn-delete-group" title="${i18n.t('shutdown.deleteGroup')}">✕</button>`}
        </header>
        <ul class="vm-group-list"></ul>`;
      const ul = section.querySelector('.vm-group-list');
      g.vms.forEach(vm => ul.appendChild(_vmRow(vm, isDefault)));
      // Drop zone (only for receiving VMs from other groups)
      section.addEventListener('dragover', onGroupDragOver);
      section.addEventListener('dragleave', onGroupDragLeave);
      section.addEventListener('drop', onGroupDrop);
      // Collapse toggle
      const chevron = section.querySelector('.group-collapse');
      chevron.addEventListener('click', (e) => {
        e.stopPropagation();
        groupCollapsed[g.name] = !groupCollapsed[g.name];
        section.classList.toggle('collapsed');
        chevron.textContent = groupCollapsed[g.name] ? '▶' : '▼';
      });
      // Group name edit — disabled for default (which is locked)
      const input = section.querySelector('.group-name');
      if (!isDefault) {
        let prevName = g.name;
        const applyRename = (newName) => {
          section.dataset.group = newName;
          const emptyIdx = pendingEmptyGroups.findIndex(x => x.name === prevName);
          if (emptyIdx >= 0) pendingEmptyGroups[emptyIdx].name = newName;
          if (groupCollapsed[prevName] !== undefined) {
            groupCollapsed[newName] = groupCollapsed[prevName];
            if (newName !== prevName) delete groupCollapsed[prevName];
          }
          g.vms.forEach(v => {
            const ref = currentVMs.find(x => x.namespace === v.namespace && x.name === v.name);
            if (ref) ref.group = newName;
          });
          g.name = newName;
          prevName = newName;
          markVmOrderDirty();
        };
        input.addEventListener('input', (e) => {
          const v = (e.target.value || '').trim();
          if (v && v !== 'default') applyRename(v);   // can't rename TO "default"
        });
        // On blur: an empty name on a non-default group is invalid; the
        // save-button validator catches it. We leave the input visible so
        // the user can fix it. Do NOT auto-coerce to "default" — that
        // would silently merge the group into the catch-all.
      }
      // Group-priority editor
      const prioInput = section.querySelector('.group-prio-value');
      prioInput.addEventListener('change', (e) => {
        const newP = parseInt(e.target.value, 10);
        if (Number.isNaN(newP) || newP < 0) {
          e.target.value = g.group_priority;
          return;
        }
        g.group_priority = newP;
        // Propagate to every VM in this group AND to the pending entry
        g.vms.forEach(v => {
          const ref = currentVMs.find(x => x.namespace === v.namespace && x.name === v.name);
          if (ref) ref.group_priority = newP;
        });
        const empty = pendingEmptyGroups.find(x => x.name === g.name);
        if (empty) empty.group_priority = newP;
        markVmOrderDirty();
        renderVMOrder();   // re-sort + re-color the tier chip
      });
      const delBtn = section.querySelector('.btn-delete-group');
      if (delBtn) delBtn.addEventListener('click', () => {
        // Reassign VMs to default (the catch-all). The default group's
        // group_priority stays at its own value (it doesn't inherit
        // from the deleted group).
        g.vms.forEach(v => {
          const ref = currentVMs.find(x => x.namespace === v.namespace && x.name === v.name);
          if (ref) { ref.group = 'default'; ref.group_priority = DEFAULT_GROUP_PRIO; }
        });
        pendingEmptyGroups = pendingEmptyGroups.filter(x => x.name !== g.name);
        markVmOrderDirty();
        renderVMOrder();
      });
      host.appendChild(section);
    });

    // "+ New group" button
    const adder = document.createElement('button');
    adder.type = 'button';
    adder.className = 'btn btn-secondary vm-group-add';
    adder.innerHTML = '➕ <span data-i18n="shutdown.newGroup">New group</span>';
    adder.addEventListener('click', addEmptyGroup);
    host.appendChild(adder);

    // Validation banner: any non-default group with empty name blocks save
    const blank = groups.some(g => g.name !== 'default' && !g.name.trim());
    const saveBtn = $('#btn-vms-save');
    if (saveBtn) saveBtn.disabled = blank;
    let nameWarn = $('#vm-order-name-warning');
    if (blank) {
      if (!nameWarn) {
        nameWarn = document.createElement('div');
        nameWarn.id = 'vm-order-name-warning';
        nameWarn.className = 'vm-order-warning';
        host.parentElement.insertBefore(nameWarn, host);
      }
      nameWarn.textContent = '⚠️ ' + i18n.t('shutdown.groupNameRequired');
    } else if (nameWarn) {
      nameWarn.remove();
    }

    if (typeof i18n !== 'undefined') i18n.applyTranslations();
  }

  function addEmptyGroup() {
    let i = 1;
    const existingNames = new Set([
      ...currentVMs.map(v => v.group || 'default'),
      ...pendingEmptyGroups.map(g => g.name),
    ]);
    while (existingNames.has(`group-${i}`)) i++;
    pendingEmptyGroups.push({ name: `group-${i}`, group_priority: DEFAULT_GROUP_PRIO });
    markVmOrderDirty();
    renderVMOrder();
    // Auto-focus + select the new group's name input
    setTimeout(() => {
      const inputs = document.querySelectorAll('#vm-order-list .vm-group:not(.is-default) .group-name');
      const newInput = inputs[inputs.length - 1];
      if (newInput) { newInput.focus(); newInput.select(); }
    }, 0);
  }

  // ---- Drag and drop ------------------------------------------------------
  // Two kinds of drag:
  //  1) Intra-group VM reordering (only in NON-default groups, since
  //     default is parallel and order is meaningless).
  //  2) Cross-group VM move: drag a VM from one group, drop on another
  //     group's section.
  // Group sections themselves are NOT draggable in v1.4.14 — the user
  // changes group order by editing the group_priority numeric input.
  // Same group_priority ⇒ groups visually share the same tier chip color.
  // ------------------------------------------------------------------------
  let dragVm = null;
  function onVmDragStart(e) {
    dragVm = {
      ns: e.currentTarget.dataset.ns,
      name: e.currentTarget.dataset.name,
      sourceGroup: e.currentTarget.closest('.vm-group')?.dataset.group,
    };
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', 'vm');
    e.currentTarget.classList.add('dragging');
    e.stopPropagation();
  }
  function onVmDragEnd(e) {
    e.currentTarget.classList.remove('dragging');
    dragVm = null;
  }

  function onGroupDragOver(e) {
    if (!dragVm) return;
    e.preventDefault();
    e.currentTarget.classList.add('drop-target');
  }
  function onGroupDragLeave(e) {
    if (e.currentTarget === e.target) e.currentTarget.classList.remove('drop-target');
  }
  function onGroupDrop(e) {
    if (!dragVm) return;
    e.preventDefault();
    const section = e.currentTarget;
    section.classList.remove('drop-target');
    const targetGroup = section.dataset.group;
    const targetGprio = parseInt(section.dataset.groupPriority, 10) || DEFAULT_GROUP_PRIO;
    const vm = currentVMs.find(v => v.namespace === dragVm.ns && v.name === dragVm.name);
    if (!vm) return;
    if (targetGroup !== vm.group) {
      vm.group = targetGroup;
      vm.group_priority = targetGprio;
      // For a brand-new VM in a target group, append at end → assign
      // intra-priority = (max existing) + 10
      const peers = currentVMs.filter(x =>
        (x.group || 'default') === targetGroup &&
        !(x.namespace === vm.namespace && x.name === vm.name)
      );
      vm.priority = peers.length
        ? Math.max(...peers.map(x => x.priority || 0)) + 10
        : 10;
      pendingEmptyGroups = pendingEmptyGroups.filter(g => g.name !== targetGroup);
      markVmOrderDirty();
      renderVMOrder();
    }
  }

  // Intra-group VM reorder: drop one VM onto another row in the same
  // non-default group → swap intra-priorities so the dropped one lands
  // before the target. Bound on each <li> in _vmRow.
  function onVmRowDragOver(e) {
    if (!dragVm) return;
    const targetRow = e.currentTarget;
    const targetGroup = targetRow.closest('.vm-group')?.dataset.group;
    if (!targetGroup || targetGroup !== dragVm.sourceGroup) return;
    if (targetGroup === 'default') return;  // ordering meaningless
    e.preventDefault();
    e.stopPropagation();
    targetRow.classList.add('row-drop-target');
  }
  function onVmRowDragLeave(e) {
    e.currentTarget.classList.remove('row-drop-target');
  }
  function onVmRowDrop(e) {
    if (!dragVm) return;
    const targetRow = e.currentTarget;
    targetRow.classList.remove('row-drop-target');
    const targetGroup = targetRow.closest('.vm-group')?.dataset.group;
    if (!targetGroup || targetGroup !== dragVm.sourceGroup) return;
    if (targetGroup === 'default') return;
    e.preventDefault();
    e.stopPropagation();
    const movedVm = currentVMs.find(v => v.namespace === dragVm.ns && v.name === dragVm.name);
    if (!movedVm) return;
    // Renumber all VMs in this group based on the new DOM order. The
    // dropped VM is inserted BEFORE the target row.
    const list = targetRow.parentElement;
    if (list && targetRow !== document.querySelector('li.dragging')) {
      const dragged = document.querySelector('li.dragging');
      if (dragged) list.insertBefore(dragged, targetRow);
    }
    // Re-read DOM order and assign priorities 10, 20, 30, ...
    [...list.querySelectorAll('li')].forEach((li, idx) => {
      const v = currentVMs.find(x =>
        x.namespace === li.dataset.ns && x.name === li.dataset.name
      );
      if (v) v.priority = (idx + 1) * 10;
    });
    markVmOrderDirty();
    renderVMOrder();
  }

  async function saveVMOrder() {
    if (!currentCluster) return;
    const groups = buildGroupsFromVMs();
    // Block save if any non-default group has an empty name
    const blank = groups.find(g => g.name !== 'default' && !g.name.trim());
    if (blank) {
      alert('⚠️ ' + i18n.t('shutdown.groupNameRequired'));
      return;
    }
    const payload = {
      groups: groups.map(g => ({
        name: g.name,
        group_priority: g.group_priority,
        vms: g.vms.map(v => ({
          namespace: v.namespace,
          name: v.name,
          snapshot: v.snapshot,
          priority: v.priority,
        })),
      })),
    };
    try {
      const res = await api(`/api/vms/${encodeURIComponent(currentCluster)}/order`, {
        method: 'PUT',
        body: JSON.stringify(payload),
      });
      pendingEmptyGroups = [];
      alert(`✓ ${i18n.t('shutdown.orderSaved')} ${res.updated}/${res.total}`);
      loadVMOrder();
    } catch (e) {
      alert(`${i18n.t('shutdown.orderSaveFailed')}: ${e.message}`);
    }
  }

  // -------------------------------------------------------------------------
  // Action launching
  // -------------------------------------------------------------------------
  async function launchAction(action, options = {}) {
    // Each action panel has its own .dry-run-sync inline checkbox — pick any
    // (they're kept in sync). Default to false if none is present.
    const dryNode = document.querySelector('.dry-run-sync');
    const body = {
      action,
      cluster: currentCluster,
      dry_run: dryNode ? !!dryNode.checked : false,
      ...options,
    };
    if (action === 'shutdown') {
      body.snapshot = $('#opt-snapshot') && $('#opt-snapshot').checked;
    }
    const msg = body.snapshot
      ? i18n.t('shutdown.confirmWithSnap', { action, cluster: currentCluster })
      : i18n.t('shutdown.confirm', { action, cluster: currentCluster });
    if (!confirm(msg)) return null;
    const run = await api('/api/action', { method: 'POST', body: JSON.stringify(body) });
    attachSSE(run.id, action);
    return run;
  }

  function attachSSE(runId, action) {
    if (currentSSE) currentSSE.close();

    const logSel = action === 'shutdown' ? '#shutdown-log'
                  : action === 'startup' ? '#startup-log'
                  : '#ns-action-log';
    const stepPanel = action === 'shutdown' ? 'shutdown-steps'
                    : action === 'startup' ? 'startup-steps'
                    : null;

    if (stepPanel) resetSteps(stepPanel);
    $(logSel).innerHTML = '';

    appendLog(logSel, `[run ${runId}] started — ${new Date().toISOString()}`, 'info');

    currentSSE = SSEReconnect.connect(`/api/stream/${runId}`, {
      on: {
        step: (e) => {
          const ev = JSON.parse(e.data);
          if (stepPanel) setStepStatus(stepPanel, ev.step_id, ev.status, ev.message);
          appendLog(logSel, `[step] ${ev.step_id} → ${ev.status} ${ev.message ? '— ' + ev.message : ''}`,
                    ev.status === 'error' ? 'err' :
                    ev.status === 'done'  ? 'ok'  :
                    ev.status === 'warn'  ? 'warn' : 'info');
        },
        log: (e) => {
          const ev = JSON.parse(e.data);
          const lvl = ev.message.includes('[ERROR]') ? 'err'
                    : ev.message.includes('[WARN')   ? 'warn'
                    : ev.message.includes('[OK')     ? 'ok'
                    : ev.message.includes('[INFO')   ? 'info'
                    : 'dim';
          appendLog(logSel, ev.message, lvl);
        },
        status: (e) => {
          const ev = JSON.parse(e.data);
          appendLog(logSel, `[status] ${ev.status}${ev.exit_code !== undefined ? ' (exit ' + ev.exit_code + ')' : ''}`,
                    ev.status === 'done' ? 'ok' : ev.status === 'error' ? 'err' : 'warn');
        },
        end: (e) => {
          const ev = JSON.parse(e.data);
          appendLog(logSel, `[end] action ${ev.action} completed in ${(ev.ended_at - ev.started_at).toFixed(1)}s`,
                    ev.status === 'done' ? 'ok' : 'err');
          currentSSE = null;
          setTimeout(refreshStatus, 1000);
        },
      },
      onStatus: (s) => {
        if (s.state === 'retry') {
          appendLog(logSel, `[stream] connection lost — retrying in ${Math.round(s.delay/1000)}s (${s.attempt}/5)`, 'warn');
        } else if (s.state === 'dead') {
          appendLog(logSel, `[stream] reconnect failed — see Activity tab for the recorded run`, 'err');
        }
      },
    });
  }

  // -------------------------------------------------------------------------
  // Activity tab (merged Actions + Logs)
  // -------------------------------------------------------------------------
  async function refreshActivity() {
    try {
      const data = await api('/api/activity');

      // In-progress section
      const runBody = $('#activity-running tbody');
      runBody.innerHTML = '';
      const running = data.in_progress || [];
      if (running.length === 0) {
        runBody.innerHTML = `<tr><td colspan="6" class="empty-state">${i18n.t('activity.noneRunning')}</td></tr>`;
      } else {
        running.forEach(a => {
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td><code>${a.id}</code></td>
            <td>${a.cluster}</td>
            <td>${a.action}${a.dry_run ? ' <span class="activity-type-badge">dry</span>' : ''}</td>
            <td><span class="phase Pending">${a.status}</span></td>
            <td>${new Date(a.started_at * 1000).toLocaleString()}</td>
            <td><button class="btn btn-sm btn-danger" data-cancel="${a.id}">Cancel</button></td>`;
          tr.querySelector('[data-cancel]').addEventListener('click', () => cancelAction(a.id));
          runBody.appendChild(tr);
        });
      }

      // History — merge actions_done + log_files
      const histBody = $('#activity-history tbody');
      histBody.innerHTML = '';

      // Build unified list
      const items = [];
      (data.actions_done || []).forEach(a => {
        items.push({
          kind: 'action',
          id: a.id,
          cluster: a.cluster,
          action: a.action,
          status: a.status,
          ts: a.ended_at || a.started_at,
          duration: (a.ended_at && a.started_at) ? (a.ended_at - a.started_at) : null,
          dry_run: a.dry_run,
        });
      });
      (data.log_files || []).forEach(f => {
        // Try to extract cluster + action from filename: YYYYMMDD-HHMMSS-<cluster>-<action>.log
        const m = f.filename.match(/^\d{8}-\d{6}-(.+?)-(shutdown|startup|status|ns-stop|ns-start|action)\.log$/);
        items.push({
          kind: 'log',
          filename: f.filename,
          cluster: m ? m[1] : '?',
          action: m ? m[2] : f.filename,
          status: 'done',
          ts: f.mtime,
          duration: null,
          size: f.size,
        });
      });
      // Apply the active column sort (default: ts desc — most recent first).
      const cmp = (a, b) => {
        const va = a[activitySort.col];
        const vb = b[activitySort.col];
        if (va == null && vb == null) return 0;
        if (va == null) return 1;
        if (vb == null) return -1;
        if (typeof va === 'number' && typeof vb === 'number') return va - vb;
        return String(va).localeCompare(String(vb));
      };
      items.sort((a, b) => (activitySort.dir === 'asc' ? cmp(a, b) : -cmp(a, b)));

      // Reflect current sort in the header indicators
      $$('#activity-history th[data-sort]').forEach(th => {
        th.classList.remove('sort-asc', 'sort-desc');
        if (th.dataset.sort === activitySort.col) {
          th.classList.add(activitySort.dir === 'asc' ? 'sort-asc' : 'sort-desc');
        }
      });

      // Snapshot the action items in display order for prev/next navigation.
      const visible = items.slice(0, 80);
      activityActionList = visible.filter(it => it.kind === 'action');

      if (items.length === 0) {
        histBody.innerHTML = `<tr><td colspan="7" class="empty-state">—</td></tr>`;
      } else {
        visible.forEach(it => {
          const tr = document.createElement('tr');
          tr.classList.add('clickable');
          const statusBadge = it.status === 'done' ? '<span class="phase Running">done</span>'
                            : it.status === 'error' ? '<span class="phase Failed">error</span>'
                            : `<span class="phase Stopped">${it.status}</span>`;
          const typeBadge = `<span class="activity-type-badge ${it.kind === 'action' ? 'web' : 'file'}">${it.kind === 'action' ? 'web' : 'cli'}</span>`;
          const dur = it.duration ? `${it.duration.toFixed(1)}s` : '—';
          const tsStr = new Date(it.ts * 1000).toLocaleString();
          tr.innerHTML = `
            <td>📜</td>
            <td>${typeBadge}</td>
            <td>${it.cluster}</td>
            <td>${it.action}${it.dry_run ? ' <span class="activity-type-badge">dry</span>' : ''}</td>
            <td>${statusBadge}</td>
            <td>${tsStr}</td>
            <td>${dur}</td>`;
          if (it.kind === 'log') {
            tr.addEventListener('click', () => showLogFile(it.filename));
          } else {
            tr.addEventListener('click', () => {
              const idx = activityActionList.findIndex(a => a.id === it.id);
              if (idx >= 0) showActionDetails(idx);
            });
          }
          histBody.appendChild(tr);
        });
      }
    } catch (e) { console.warn('activity refresh failed', e); }
  }

  async function showLogFile(filename) {
    try {
      const data = await api(`/api/logs/${encodeURIComponent(filename)}`);
      $('#activity-log-card').style.display = 'block';
      $('#activity-log-title').textContent = filename;
      $('#activity-log-content').textContent = data.content || '(empty)';
      $('#activity-log-content').scrollTop = $('#activity-log-content').scrollHeight;
    } catch (e) {
      alert(e.message);
    }
  }

  // Activity history list snapshotted from the latest refresh — used so
  // showActionDetails() can navigate prev/next via arrow keys & buttons.
  let activityActionList = [];
  let activityCurrentIdx = -1;
  let activitySSE = null;
  const activitySort = { col: 'ts', dir: 'desc' };

  // Header click → toggle sort column / direction. Wired once at init.
  document.addEventListener('click', (e) => {
    const th = e.target.closest('#activity-history th[data-sort]');
    if (!th) return;
    const col = th.dataset.sort;
    if (activitySort.col === col) {
      activitySort.dir = activitySort.dir === 'asc' ? 'desc' : 'asc';
    } else {
      activitySort.col = col;
      activitySort.dir = col === 'ts' || col === 'duration' ? 'desc' : 'asc';
    }
    refreshActivity();
  });

  function showActionDetails(idx) {
    const it = activityActionList[idx];
    if (!it) return;
    activityCurrentIdx = idx;
    if (activitySSE) { try { activitySSE.close(); } catch {} activitySSE = null; }

    const panelId = 'activity-detail';
    const total = activityActionList.length;
    const navBar = `
      <div class="activity-detail-nav">
        <button class="btn btn-sm btn-secondary" id="act-detail-prev"
                ${idx >= total - 1 ? 'disabled' : ''}
                title="Older action (←)">← Older</button>
        <span class="form-hint">${idx + 1} / ${total}</span>
        <button class="btn btn-sm btn-secondary" id="act-detail-next"
                ${idx <= 0 ? 'disabled' : ''}
                title="Newer action (→)">Newer →</button>
      </div>
      <div class="activity-detail-meta">
        <div><strong>Action</strong> <code>${it.action}</code></div>
        <div><strong>Cluster</strong> <code>${it.cluster}</code></div>
        <div><strong>Run ID</strong> <code>${it.id}</code></div>
        <div><strong>Status</strong> <span class="phase ${
          it.status === 'done' ? 'Running' :
          it.status === 'error' ? 'Failed' :
          it.status === 'cancelled' ? 'Stopped' :
          it.status === 'running' ? 'Pending' : 'Stopped'}">${it.status}</span></div>
        <div><strong>When</strong> ${new Date(it.ts * 1000).toLocaleString()}</div>
        ${it.duration ? `<div><strong>Duration</strong> ${it.duration.toFixed(1)}s</div>` : ''}
      </div>
      <pre id="activity-detail-log" class="live-log"></pre>`;

    const title = `${it.action} · ${it.cluster}`;
    const api = window.FloatingPanels
      ? window.FloatingPanels.open({
          id: panelId,
          title,
          bodyHtml: navBar,
          width: 800,
          height: 540,
        })
      : null;
    if (api) {
      api.setTitle(title);
      api.setBody(navBar);
    }

    const logEl = document.querySelector('#activity-detail-log');
    const prevBtn = document.querySelector('#act-detail-prev');
    const nextBtn = document.querySelector('#act-detail-next');
    if (prevBtn) prevBtn.addEventListener('click', () => showActionDetails(idx + 1));
    if (nextBtn) nextBtn.addEventListener('click', () => showActionDetails(idx - 1));

    if (!logEl) return;
    logEl.textContent = 'loading…';
    const lines = [];
    const fmt = (e) => {
      const ts = new Date((e.ts || 0) * 1000).toLocaleTimeString();
      if (e.type === 'step')
        return `[${ts}] step  ${e.step_id} → ${e.status}${e.message ? ' — ' + e.message : ''}`;
      if (e.type === 'log')
        return `[${ts}] log   ${e.message || ''}`;
      if (e.type === 'status')
        return `[${ts}] status ${e.status}${e.exit_code !== undefined ? ' (exit '+e.exit_code+')' : ''}`;
      return `[${ts}] ${e.type} ${JSON.stringify(e).slice(0, 200)}`;
    };
    const onEvent = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        lines.push(fmt(data));
        logEl.textContent = lines.join('\n');
        logEl.scrollTop = logEl.scrollHeight;
      } catch {}
    };
    activitySSE = SSEReconnect.connect(`/api/stream/${it.id}`, {
      on: {
        step: onEvent,
        log: onEvent,
        status: onEvent,
        end: () => {
          if (lines.length === 0) {
            logEl.textContent = '(no events recorded — this action has no replay)';
          }
        },
      },
      // No onStatus banner here: the activity-detail panel is a passive
      // viewer of completed (or near-complete) runs, so we silently retry.
    });
  }

  // Arrow-key navigation while the activity-detail panel is open.
  document.addEventListener('keydown', (e) => {
    const open = document.querySelector('#fp-activity-detail');
    if (!open || open.classList.contains('floating-panel-minimized')) return;
    if (e.target && /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
    if (e.key === 'ArrowLeft' && activityCurrentIdx < activityActionList.length - 1) {
      e.preventDefault();
      showActionDetails(activityCurrentIdx + 1);
    } else if (e.key === 'ArrowRight' && activityCurrentIdx > 0) {
      e.preventDefault();
      showActionDetails(activityCurrentIdx - 1);
    }
  });

  async function cancelAction(id) {
    if (!confirm(i18n.t('action.cancelConfirm'))) return;
    // v1.4.17 UX: surface a 'Cancelling…' state on the source button(s)
    // so the user knows the DELETE is in flight. Without this the click
    // looked like a no-op until the next 8s activity refresh.
    const cancelBtns = document.querySelectorAll(
      `#btn-cancel-shutdown, #btn-cancel-startup, [data-cancel="${id}"]`
    );
    cancelBtns.forEach(b => {
      b.dataset.prevText = b.textContent;
      b.textContent = i18n.t('action.cancelling');
      b.disabled = true;
      b.classList.add('cancelling');
    });
    let ok = true;
    try {
      await api(`/api/action/${id}`, { method: 'DELETE' });
    } catch (e) {
      ok = false;
      alert(i18n.t('action.cancelFailed') + ': ' + (e.message || e));
    }
    cancelBtns.forEach(b => {
      b.textContent = ok ? i18n.t('action.cancelled') : (b.dataset.prevText || '');
      b.classList.remove('cancelling');
      // Reset to original text after a short feedback window so the
      // button is reusable for the next action.
      setTimeout(() => {
        b.textContent = b.dataset.prevText || b.textContent;
        delete b.dataset.prevText;
        b.disabled = false;
      }, 1500);
    });
    refreshActivity();
  }

  // -------------------------------------------------------------------------
  // Event bindings
  // -------------------------------------------------------------------------
  function bind() {
    $$('.tab').forEach(t => t.addEventListener('click', (e) => {
      e.preventDefault();
      // Group heads without a data-tab (e.g. "cluster", "automation" —
      // purely a container) only toggle expansion. The first child of the
      // group is selected as the main tab so the content panel shows up.
      const isGroupHead = t.classList.contains('tab-group-head');
      if (isGroupHead) {
        const group = t.dataset.group;
        const grp = document.querySelector(`#tab-group-${group}`);
        if (grp) grp.classList.toggle('expanded');
        try { localStorage.setItem(`harvester_ops_group_${group}_expanded`,
          grp.classList.contains('expanded') ? '1' : '0'); } catch {}
        if (!t.dataset.tab) {
          // Pure container — pick the first child's effective main-tab
          const firstChild = grp?.querySelector('.tab-child');
          const targetTab = firstChild?.dataset.tab || `${group}`;
          setTab(targetTab);
          return;
        }
      }
      // A `.tab-child` may inherit its main routing from its group head
      // (e.g. Automation children carry data-subtab only — the matching
      // main tab is named after the group: data-group="automation"
      // ⇒ #tab-automation).
      if (t.classList.contains('tab-child') && !t.dataset.tab) {
        const groupHead = t.closest('.tab-group')?.querySelector('.tab-group-head');
        const inherited = groupHead?.dataset.tab || groupHead?.dataset.group;
        if (inherited) setTab(inherited);
        return;
      }
      setTab(t.dataset.tab);
    }));

    // Restore each group's expanded state from localStorage
    document.querySelectorAll('.tab-group').forEach(grp => {
      const id = grp.id.replace('tab-group-', '');
      try {
        const saved = localStorage.getItem(`harvester_ops_group_${id}_expanded`);
        if (saved === '0') grp.classList.remove('expanded');
        if (saved === '1') grp.classList.add('expanded');
      } catch {}
    });

    $('#cluster-select')?.addEventListener('change', (e) => setCluster(e.target.value));

    // Sidebar collapse/expand toggle (persists in localStorage)
    const SIDEBAR_STORAGE = 'harvester_ops_sidebar_collapsed';
    const applySidebar = (collapsed) => {
      document.body.classList.toggle('sidebar-collapsed', collapsed);
      try { localStorage.setItem(SIDEBAR_STORAGE, collapsed ? '1' : '0'); } catch {}
    };
    applySidebar(localStorage.getItem(SIDEBAR_STORAGE) === '1');
    $('#btn-sidebar-collapse')?.addEventListener('click', () => {
      applySidebar(!document.body.classList.contains('sidebar-collapsed'));
    });

    // Keep every inline .dry-run-sync checkbox (shutdown + startup tabs) in
    // sync with each other. The sidebar toggle was removed in 1.3.4 because
    // it duplicated these per-panel controls.
    const synced = $$('.dry-run-sync');
    if (synced.length) {
      const apply = (val) => synced.forEach(cb => cb.checked = val);
      synced.forEach(cb => cb.addEventListener('change', (e) => apply(e.target.checked)));
    }

    $('#btn-shutdown')?.addEventListener('click', async () => {
      $('#btn-shutdown').disabled = true;
      const cancelBtn = $('#btn-cancel-shutdown');
      if (cancelBtn) cancelBtn.disabled = false;
      try { await launchAction('shutdown'); }
      finally {
        $('#btn-shutdown').disabled = false;
        if (cancelBtn) cancelBtn.disabled = true;
      }
    });

    $('#btn-startup')?.addEventListener('click', async () => {
      $('#btn-startup').disabled = true;
      try { await launchAction('startup'); }
      finally { $('#btn-startup').disabled = false; }
    });

    // Namespace dropdown (replaces the old left sidebar)
    $('#ns-dropdown')?.addEventListener('change', (e) => selectNamespace(e.target.value));
    $('#btn-ns-refresh')?.addEventListener('click', () => refreshNamespaces(false));
    // Sortable column headers in VM table
    $$('#ns-vms-table th.sortable').forEach(th => {
      th.addEventListener('click', () => {
        const key = th.dataset.sortBy;
        if (nsSortKey === key) {
          nsSortDir = nsSortDir === 'asc' ? 'desc' : 'asc';
        } else {
          nsSortKey = key;
          nsSortDir = 'asc';
        }
        if (currentNamespace) selectNamespace(currentNamespace);
      });
    });
    $('#btn-ns-notes')?.addEventListener('click', () => {
      if (window.Notes && currentNamespace) {
        window.Notes.open('ns', currentCluster, currentNamespace);
      }
    });

    if ($('#btn-vms-refresh')) $('#btn-vms-refresh').addEventListener('click', loadVMOrder);
    if ($('#btn-vms-save'))    $('#btn-vms-save').addEventListener('click', saveVMOrder);
    if ($('#vm-order-filter-ns')) $('#vm-order-filter-ns').addEventListener('change', (e) => { nsFilter = e.target.value; renderVMOrder(); });
    if ($('#vm-order-sort'))      $('#vm-order-sort').addEventListener('change',     (e) => { sortBy   = e.target.value; renderVMOrder(); });
    // Shutdown sub-tabs: "Arrêt du cluster" vs "Ordre d'arrêt des VMs"
    // (Restoration is done from init() via restoreSubTabsFromStorage,
    // AFTER setCluster() — otherwise mountTopology() bails on null
    // cluster during the boot click. See v1.4.31 fix.)
    $$('[data-shutdown-tab]').forEach(btn => {
      btn.addEventListener('click', () => {
        const target = btn.dataset.shutdownTab;
        $$('[data-shutdown-tab]').forEach(b => b.classList.toggle('active', b === btn));
        $$('.shutdown-subtab').forEach(p => p.hidden = (p.dataset.subtab !== target));
        try { localStorage.setItem('harvester_ops_shutdown_subtab', target); } catch {}
      });
    });

    // Overview sub-tabs (Metrics / Cluster / Network / Storage — v1.4.19).
    // The latter three drive the Cytoscape topology viewer.
    $$('[data-overview-tab]').forEach(btn => {
      btn.addEventListener('click', () => {
        const target = btn.dataset.overviewTab;
        $$('[data-overview-tab]').forEach(b => b.classList.toggle('active', b === btn));
        $$('.overview-subtab').forEach(p => p.hidden = (p.dataset.subtab !== target));
        try { localStorage.setItem('harvester_ops_overview_subtab', target); } catch {}
        // Mount the topology viewer once per mode switch
        if (window.Topology && currentCluster) {
          if (target === 'metrics') {
            window.Topology.stop();
          } else {
            mountTopology(target);
          }
        }
      });
    });

    // Namespace bulk toolbar
    if ($('#ns-vms-check-all')) {
      $('#ns-vms-check-all').addEventListener('change', (e) => {
        const checked = e.target.checked;
        $$('#ns-vms-table tbody input[type="checkbox"]').forEach(cb => {
          cb.checked = checked;
          const key = cb.dataset.key;
          if (checked) nsSelection.add(key); else nsSelection.delete(key);
          cb.closest('tr').classList.toggle('selected', checked);
        });
        updateBulkToolbar();
      });
    }
    $('#btn-bulk-stop')?.addEventListener('click',  () => bulkAction('Halted'));
    $('#btn-bulk-start')?.addEventListener('click', () => bulkAction('Always'));
    $('#btn-bulk-strategy')?.addEventListener('click', () => {
      const target = $('#bulk-strategy-select').value;
      bulkAction(target);
    });
    $('#btn-bulk-clear')?.addEventListener('click', () => {
      nsSelection.clear();
      $$('#ns-vms-table tbody input[type="checkbox"]').forEach(cb => cb.checked = false);
      $$('#ns-vms-table tbody tr').forEach(tr => tr.classList.remove('selected'));
      $('#ns-vms-check-all').checked = false;
      updateBulkToolbar();
    });

    // Activity log close button
    $('#btn-activity-log-close')?.addEventListener('click', () => {
      $('#activity-log-card').style.display = 'none';
    });
  }

  // -------------------------------------------------------------------------
  // Init
  // -------------------------------------------------------------------------
  // v1.4.31: restore Overview + Shutdown sub-tabs from localStorage.
  // Must run AFTER setCluster() in init(), because the Overview sub-tab
  // click handler calls mountTopology(target) which early-returns when
  // currentCluster is null. Before v1.4.31, restoration happened from
  // bind() — before setCluster() — so the Cluster/Network/Storage tabs
  // came back visually selected but with an empty canvas.
  function restoreSubTabsFromStorage() {
    const restore = (storageKey, datasetAttr) => {
      try {
        const saved = localStorage.getItem(storageKey);
        if (!saved) return;
        const btn = document.querySelector(`[${datasetAttr}="${saved}"]`);
        if (btn) btn.click();
      } catch {}
    };
    restore('harvester_ops_overview_subtab', 'data-overview-tab');
    restore('harvester_ops_shutdown_subtab', 'data-shutdown-tab');
  }

  function init() {
    bind();
    // Apply translations before first render
    if (typeof i18n !== 'undefined') i18n.applyTranslations();
    const sel = $('#cluster-select');
    if (sel.options.length > 0) {
      // v1.4.32: restore the cluster the user last worked on. Only
      // honor a saved value that's still in the dropdown (a cluster
      // may have been removed from config.yaml between sessions).
      let target = sel.value;
      try {
        const saved = localStorage.getItem('harvester_ops_current_cluster');
        if (saved) {
          const opt = [...sel.options].find(o => o.value === saved);
          if (opt) {
            sel.value = saved;
            target = saved;
          }
        }
      } catch {}
      setCluster(target);
    }

    // Restore previously selected sub-tabs now that currentCluster is set.
    restoreSubTabsFromStorage();

    // Restore last active tab (persisted in localStorage).
    // v1.4.33: guard via the tab-content element (#tab-<name>) instead of
    // .tab[data-tab=<name>]. Group heads like Automation carry data-group
    // (not data-tab), so the old guard silently failed for them — F5 on
    // Automation > CAPI/Terraform/PXE always reverted to Overview.
    try {
      const savedTab = localStorage.getItem(TAB_STORAGE);
      if (savedTab && document.getElementById(`tab-${savedTab}`)) {
        setTab(savedTab);
      }
    } catch {}

    // Restore previously open floating panels
    if (window.FloatingPanels && FloatingPanels.restoreAll) {
      setTimeout(() => FloatingPanels.restoreAll(), 200);
    }

    statusRefreshTimer = setInterval(() => {
      if ($('#tab-overview').classList.contains('active')) refreshStatus();
      if ($('#tab-activity').classList.contains('active')) refreshActivity();
      if ($('#tab-namespaces').classList.contains('active')) refreshNamespaces(false);
      if ($('#tab-shutdown').classList.contains('active')) {
        // Skip the periodic refresh while the user is editing a group
        // name OR has unsaved local changes (drag, rename, snapshot
        // toggle). Otherwise the re-render replaces local intent with
        // server state and the user loses their work.
        if (vmOrderDirty) return;
        const active = document.activeElement;
        if (active && active.closest && active.closest('#vm-order-list')) return;
        loadVMOrder();
      }
    }, 8000);
    // v1.5.7: stop the long-running interval on page unload so the
    // browser doesn't keep firing it after navigation. Also stop the
    // SSE subscription if any.
    window.addEventListener('beforeunload', () => {
      if (statusRefreshTimer) clearInterval(statusRefreshTimer);
      if (currentSSE) { try { currentSSE.close(); } catch {} }
    });
  }

  // -------------------------------------------------------------------------
  // Topology viewer mounting (v1.4.19) — wraps the Topology module so other
  // app modules don't need to know about cytoscape internals.
  // -------------------------------------------------------------------------
  function mountTopology(mode) {
    if (!window.Topology || !currentCluster) return;
    // Build the canvas + detail sidebar shell once per host, then ask
    // the Topology module to render for the active mode. We use CSS
    // classes (not ids) so each of the 3 subtabs gets its own
    // independent canvas — sharing an id was the v1.4.21 bug that
    // made Network and Storage render into the Cluster's container.
    const host = document.querySelector(`.overview-subtab[data-subtab="${mode}"] .topology-host`);
    if (!host) return;
    if (!host.querySelector('.topology-canvas')) {
      host.innerHTML = `
        <div class="topology-toolbar">
          <input type="search" class="topology-search tip"
                 placeholder="${i18n.t('topology.searchPlaceholder')}"
                 data-tip-i18n="topology.searchTip"
                 aria-label="${i18n.t('topology.searchPlaceholder')}">
          <div class="topology-zoom-group" role="group"
               aria-label="${i18n.t('topology.zoomGroupAria')}">
            <button type="button" class="btn btn-sm topology-zoom-out tip"
                    data-tip-i18n="topology.zoomOutTip"
                    aria-label="${i18n.t('topology.zoomOutTip')}">−</button>
            <button type="button" class="btn btn-sm topology-zoom-fit tip"
                    data-tip-i18n="topology.zoomFitTip"
                    aria-label="${i18n.t('topology.zoomFitTip')}">⊡</button>
            <button type="button" class="btn btn-sm topology-zoom-in tip"
                    data-tip-i18n="topology.zoomInTip"
                    aria-label="${i18n.t('topology.zoomInTip')}">+</button>
          </div>
          <label class="topology-fontsize tip" data-tip-i18n="topology.fontSizeTip">
            <span aria-hidden="true">Aa</span>
            <input type="range" min="8" max="18" value="11" step="1"
                   class="topology-fontsize-input"
                   aria-label="${i18n.t('topology.fontSizeTip')}">
          </label>
          <span class="topology-meta tip" data-tip-i18n="topology.metaTip">—</span>
          <button type="button" class="btn btn-sm topology-refresh"
                  data-tip-i18n="topology.refreshTip">⟳ ${i18n.t('topology.refresh')}</button>
          <label class="topology-unlock tip" data-tip-i18n="topology.unlockTip">
            <input type="checkbox" class="topology-unlock-input">
            🔓 ${i18n.t('topology.unlockDestructive')}
          </label>
        </div>
        <div class="topology-canvas-wrap">
          <div class="topology-canvas"></div>
          <aside class="topology-detail">
            <p class="hint">${i18n.t('topology.detailEmpty')}</p>
          </aside>
        </div>`;
      host.querySelector('.topology-refresh').addEventListener('click', () => window.Topology.refresh());
      host.querySelector('.topology-unlock-input').addEventListener('change', (e) => {
        window.Topology.setDestructiveUnlocked(e.target.checked);
      });
      host.querySelector('.topology-zoom-in').addEventListener('click',
        () => window.Topology.zoomBy(1.25));
      host.querySelector('.topology-zoom-out').addEventListener('click',
        () => window.Topology.zoomBy(0.8));
      host.querySelector('.topology-zoom-fit').addEventListener('click',
        () => window.Topology.zoomFit());
      // Search field: debounced highlight + auto-fit on first match.
      const searchInput = host.querySelector('.topology-search');
      let _searchT;
      searchInput.addEventListener('input', (e) => {
        clearTimeout(_searchT);
        _searchT = setTimeout(() => window.Topology.search(e.target.value), 150);
      });
      // Font size slider: live re-render of labels with the new budget.
      host.querySelector('.topology-fontsize-input').addEventListener('input', (e) => {
        window.Topology.setFontSize(e.target.value);
      });
      if (typeof i18n !== 'undefined') i18n.applyTranslations();
    }
    window.Topology.start(currentCluster, mode);
  }

  // Expose getCurrentCluster for the topology module
  function getCurrentCluster() { return currentCluster; }

  return { init, refreshStatus, refreshNamespaces, refreshActivity, cancelAction, loadVMOrder, getCurrentCluster, mountTopology };
})();

window.App = App;
document.addEventListener('DOMContentLoaded', App.init);
