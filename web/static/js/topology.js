/**
 * harvester-ops — interactive topology viewer (v1.4.19)
 *
 * Three perspectives over the same /api/topology/<cluster> snapshot,
 * rendered with Cytoscape.js:
 *   1. Cluster   — hypervisor nodes + their hosted VMs (parent/child)
 *   2. Network   — VMs ↔ networks (multus / pod) bipartite-ish graph
 *   3. Storage   — Longhorn volumes ↔ replicas ↔ nodes
 *
 * Default mode is read-only with safe actions on click (open Notes,
 * VM Edit, VM Snapshots). The "🔓 destructive" toggle in the toolbar
 * unlocks Stop / Migrate / Cordon etc. — guarded by a confirm() every
 * time so a misclick can't bring down a node.
 */
import cytoscape from '/static/vendor/cytoscape/cytoscape-bundle.mjs';

const Topology = (() => {
  let cy = null;
  let currentMode = 'cluster';     // 'cluster' | 'network' | 'storage'
  let lastData = null;
  let destructiveUnlocked = false;
  let refreshTimer = null;
  // v1.4.21: each overview subtab has its OWN canvas (id = topology-
  // canvas-<mode>) and detail panel. Previously all three shared a
  // single global id `#topology-canvas`, so when the user switched
  // sub-tabs document.querySelector returned the WRONG one (the
  // cluster's, since it was first in the DOM) and the network /
  // storage views silently rendered into an off-screen container.
  let currentHost = null;          // <div.topology-host> of the active mode
  const REFRESH_INTERVAL = 8000;

  function canvasId(mode) { return 'topology-canvas-' + mode; }
  function detailId(mode) { return 'topology-detail-' + mode; }
  function metaId(mode)   { return 'topology-meta-'   + mode; }

  // VM labels often exceed the box width and overlapped neighbours.
  // We truncate the displayed label and keep the full name in
  // `data.fullName` so the hover tooltip can show it untouched. The
  // truncation budget scales with the font size — wider boxes at
  // bigger fonts (tweakable from the toolbar).
  let labelFontSize = 11;
  function truncLabel(name, fontSize) {
    if (!name) return '';
    // Empirical: at font 11px and box width 130, ~14 chars fit.
    const charBudget = Math.max(6, Math.round(14 * (11 / fontSize)));
    if (name.length <= charBudget) return name;
    return name.slice(0, charBudget - 1) + '…';
  }

  // -----------------------------------------------------------------------
  // Styles — Pathway Commons SBGN inspired (https://pathwaycommons.github.io/cytoscape-sbgn-stylesheet/)
  // High-contrast, role-based shapes, dim text on light fills.
  // -----------------------------------------------------------------------
  function baseStyle() {
    return [
      {
        selector: 'node',
        style: {
          'background-color': 'data(color)',
          'border-color': 'data(border)',
          'border-width': 2,
          'label': 'data(label)',
          'color': '#fff',
          'text-valign': 'center',
          'text-halign': 'center',
          'font-size': labelFontSize,
          'font-family': 'system-ui, sans-serif',
          'text-outline-color': '#000',
          'text-outline-width': 1,
          'shape': 'data(shape)',
          // Keep labels inside their box. Cytoscape supports
          // 'wrap'/'none' for text-wrap; truncation is done JS-side
          // (see truncLabel) so the on-canvas label never overflows.
          'text-wrap': 'none',
          'text-max-width': '120',
        },
      },
      // Volume nodes: 2-line label "🛢 short-name\n10 GiB". text-wrap:
      // 'wrap' honors the explicit \n; text-max-width matches the
      // 90 px-wide vertical cylinder (minus padding) so labels never
      // spill outside the barrel silhouette.
      {
        selector: 'node[kind = "volume"]',
        style: {
          'text-wrap': 'wrap',
          'text-max-width': 75,
          'font-size': 10,
          'text-outline-width': 2,
        },
      },
      // Network = switch silhouette. The cut-rectangle gets a single-
      // line label that ellipsizes if too long.
      {
        selector: 'node[kind = "network"]',
        style: {
          'text-wrap': 'ellipsis',
          'text-max-width': 160,
          'font-size': 11,
        },
      },
      // Highlight (search hit). Bright green ring + a soft glow so
      // matches pop out across all kinds of nodes.
      {
        selector: 'node.searched',
        style: {
          'border-color': '#19c37d',
          'border-width': 5,
          'overlay-color': '#19c37d',
          'overlay-opacity': 0.18,
          'overlay-padding': '6px',
        },
      },
      // Width/height come from `size` ONLY for leaves that defined it.
      // Parents (compound nodes) auto-size from their children, so we
      // mustn't apply a hard mapping there — Cytoscape would warn 100×
      // per layout pass that the parent has no `size` data field.
      {
        selector: 'node[size]',
        style: {
          'width': 'data(size)',
          'height': 'data(size)',
        },
      },
      // Some kinds need a rectangular footprint (VMs need more width
      // than height to fit their label). Drive width/height from data
      // fields when present, falling back to the `size` rule above.
      {
        selector: 'node[width][height]',
        style: {
          'width': 'data(width)',
          'height': 'data(height)',
        },
      },
      // VM-specific label handling: native Cytoscape ellipsis is more
      // accurate than the JS char budget, and `text-max-width` ensures
      // the label stays INSIDE the box (was overflowing in v1.4.27).
      {
        selector: 'node[kind = "vm"]',
        style: {
          'text-wrap': 'ellipsis',
          'text-max-width': 110,
        },
      },
      {
        selector: 'node:parent',
        style: {
          'background-opacity': 0.18,
          'background-color': 'data(color)',
          'border-color': 'data(border)',
          'border-width': 2,
          'border-style': 'dashed',
          'color': '#fff',
          'font-size': 13,
          'text-valign': 'top',
          'text-halign': 'center',
          'padding': '14px',
          'shape': 'round-rectangle',
        },
      },
      {
        selector: 'node[?notReady]',
        style: { 'border-color': '#e0464b', 'border-width': 3 },
      },
      {
        selector: 'edge',
        style: {
          'width': 1.5,
          'curve-style': 'bezier',
          'line-color': '#778',
          'target-arrow-shape': 'none',
          'opacity': 0.75,
          'label': 'data(label)',
          'color': '#bbb',
          'font-size': 9,
          'text-rotation': 'autorotate',
        },
      },
      {
        selector: 'edge[edgeType = "replica"]',
        style: { 'line-color': '#4aa8d8', 'line-style': 'dashed' },
      },
      {
        selector: 'edge[edgeType = "attached"]',
        style: { 'line-color': '#e0464b', 'width': 2 },
      },
      {
        selector: ':selected',
        style: {
          'border-color': '#19c37d',
          'border-width': 4,
          'line-color': '#19c37d',
          'target-arrow-color': '#19c37d',
        },
      },
    ];
  }

  // -----------------------------------------------------------------------
  // Build the three perspectives
  // -----------------------------------------------------------------------
  function buildClusterElements(data) {
    const els = [];
    // Hypervisor nodes as parents
    data.nodes.forEach(n => {
      els.push({
        group: 'nodes',
        data: {
          id: 'node-' + n.name,
          label: '🖥 ' + n.name,
          color: n.ready ? '#1f4d3a' : '#5b1f22',
          border: n.ready ? '#19c37d' : '#e0464b',
          shape: 'round-rectangle',
          notReady: !n.ready,
          kind: 'node',
          raw: n,
        },
      });
    });
    // VMs as children of their hosting node (or a dangling "unscheduled" bucket)
    let unscheduled = false;
    data.vms.forEach(v => {
      const parent = v.node ? 'node-' + v.node : 'node-unscheduled';
      if (!v.node) unscheduled = true;
      const fullName = v.name;
      els.push({
        group: 'nodes',
        data: {
          id: 'vm-' + v.namespace + '-' + v.name,
          parent,
          // v1.4.28: rely on Cytoscape's text-wrap:ellipsis +
          // text-max-width to truncate visually, more accurate than the
          // JS char budget. fullName still feeds the hover tooltip +
          // search (namespace prefix included).
          label: '💻 ' + fullName,
          fullName: v.namespace + '/' + fullName,
          searchText: (v.namespace + '/' + fullName).toLowerCase(),
          color: phaseColor(v.phase),
          border: phaseBorder(v.phase),
          shape: 'round-rectangle',
          // Rectangular VMs (130×50) leave room for ~13-16 chars at
          // 11 px font without overflowing the box.
          width: 130,
          height: 50,
          kind: 'vm',
          raw: v,
        },
      });
    });
    if (unscheduled) {
      els.push({
        group: 'nodes',
        data: {
          id: 'node-unscheduled',
          label: '⏸ Stopped / unscheduled',
          color: '#3a3a3a',
          border: '#777',
          shape: 'round-rectangle',
          kind: 'node-virtual',
        },
      });
    }
    return els;
  }

  function buildNetworkElements(data) {
    const els = [];
    const seen = new Set();
    data.vms.forEach(v => {
      const fullName = v.namespace + '/' + v.name;
      els.push({
        group: 'nodes',
        data: {
          id: 'vm-' + v.namespace + '-' + v.name,
          label: '💻 ' + v.name,
          fullName,
          searchText: fullName.toLowerCase(),
          color: phaseColor(v.phase),
          border: phaseBorder(v.phase),
          shape: 'round-rectangle',
          width: 130,
          height: 50,
          kind: 'vm',
          raw: v,
        },
      });
      v.networks.forEach(net => {
        const ref = net.ref || net.name;
        const id = 'net-' + ref;
        if (!seen.has(id)) {
          seen.add(id);
          els.push({
            group: 'nodes',
            data: {
              id,
              // v1.4.30: switch-like silhouette (cut-rectangle = rack
              // device shape) + 🔀 icon to evoke a network switch.
              label: '🔀 ' + ref,
              fullName: ref,
              searchText: ref.toLowerCase(),
              color: '#1f3e5b',
              border: '#4aa8d8',
              shape: 'cut-rectangle',
              // Wide & short = 1U rack-mount silhouette.
              width: 180,
              height: 48,
              kind: 'network',
              raw: { name: ref, type: net.type },
            },
          });
        }
        els.push({
          group: 'edges',
          data: {
            id: 'e-' + v.namespace + '-' + v.name + '-' + id,
            source: 'vm-' + v.namespace + '-' + v.name,
            target: id,
            label: net.type,
            edgeType: 'network',
          },
        });
      });
    });
    return els;
  }

  function buildStorageElements(data) {
    const els = [];
    // Nodes (hosts)
    data.nodes.forEach(n => {
      els.push({
        group: 'nodes',
        data: {
          id: 'node-' + n.name,
          label: '🖥 ' + n.name,
          color: n.ready ? '#1f4d3a' : '#5b1f22',
          border: n.ready ? '#19c37d' : '#e0464b',
          shape: 'round-rectangle',
          size: 80,
          kind: 'node',
          raw: n,
        },
      });
    });
    // Volumes — color by state
    data.volumes.forEach(v => {
      const stateColor = ({
        attached: '#1f4d3a',
        detached: '#3a3a3a',
        attaching: '#7a5f1f',
        detaching: '#7a5f1f',
        creating: '#7a5f1f',
        deleting: '#5b1f22',
      })[v.state] || '#444';
      // Two-line label: truncated PVC id + human-readable size. Lets
      // operators eyeball capacity distribution without clicking each
      // volume. v1.4.29: widened the box to 140×60 (was 55×55 barrel)
      // so a 16-char truncation of UUIDs + the size line both fit
      // comfortably without spilling outside the colored shape.
      // v1.4.30: vertical cylinder (classic database icon). Cytoscape's
      // `barrel` shape draws curved sides — when the box is taller than
      // wide it reads as a vertical cylinder. The 🛢 icon reinforces.
      const shortName = v.name.length > 12
        ? v.name.slice(0, 11) + '…'
        : v.name;
      const sizeLabel = formatBytes(v.size);
      els.push({
        group: 'nodes',
        data: {
          id: 'vol-' + v.name,
          label: '🛢 ' + shortName + '\n' + sizeLabel,
          fullName: v.name + ' (' + sizeLabel + ')',
          searchText: (v.name + ' ' + sizeLabel).toLowerCase(),
          color: stateColor,
          border: v.robustness === 'healthy' ? '#19c37d' :
                  v.robustness === 'degraded' ? '#d97706' : '#e0464b',
          shape: 'barrel',
          // Vertical orientation — height > width — so the barrel
          // visually reads as a database cylinder.
          width: 90,
          height: 110,
          kind: 'volume',
          raw: v,
        },
      });
      if (v.attached_to) {
        els.push({
          group: 'edges',
          data: {
            id: 'e-attach-' + v.name,
            source: 'vol-' + v.name,
            target: 'node-' + v.attached_to,
            edgeType: 'attached',
            label: 'attached',
          },
        });
      }
    });
    // Replicas — show as small dots linking volume → host
    data.replicas.forEach(r => {
      if (!r.volume || !r.node) return;
      els.push({
        group: 'edges',
        data: {
          id: 'e-replica-' + r.name,
          source: 'vol-' + r.volume,
          target: 'node-' + r.node,
          edgeType: 'replica',
          label: r.running ? '' : '✗',
        },
      });
    });
    return els;
  }

  function phaseColor(phase) {
    return ({
      Running: '#1f4d3a',
      Stopped: '#3a3a3a',
      Halted: '#3a3a3a',
      Pending: '#7a5f1f',
      Scheduling: '#7a5f1f',
      Starting: '#7a5f1f',
      Paused: '#1f3e5b',
      Failed: '#5b1f22',
    })[phase] || '#444';
  }
  function phaseBorder(phase) {
    return ({
      Running: '#19c37d',
      Failed: '#e0464b',
    })[phase] || '#666';
  }

  // -----------------------------------------------------------------------
  // Layout per mode
  // -----------------------------------------------------------------------
  function layoutFor(mode) {
    if (mode === 'cluster') {
      return { name: 'preset', padding: 20 }; // we'll arrange children in fcose-like grid by hand
    }
    if (mode === 'network') {
      return {
        name: 'cose', animate: false, padding: 20, idealEdgeLength: 90,
        nodeRepulsion: 4000, nodeDimensionsIncludeLabels: true,
      };
    }
    if (mode === 'storage') {
      return {
        name: 'cose', animate: false, padding: 20, idealEdgeLength: 120,
        nodeRepulsion: 8000, nodeDimensionsIncludeLabels: true,
      };
    }
    return { name: 'cose' };
  }

  // Manual cluster layout (v1.4.24):
  //   Row 1 (top)    : hypervisor nodes side-by-side, each VM in a grid
  //   Row 2 (bottom) : full-width "Stopped / unscheduled" bucket
  // This matches the operator's mental model: "what's running where on
  // top, what's idle at the bottom". Halted VMs no longer get dumped
  // in the same row as live nodes.
  function applyClusterLayout(cy) {
    const allParents  = cy.nodes(':parent');
    const realParents = allParents.filter(p => p.id() !== 'node-unscheduled');
    const unsched     = allParents.filter(p => p.id() === 'node-unscheduled');
    // Cell sized to comfortably hold a 130×50 VM box with breathing
     // room on each side (the box centres on its grid coord).
    const cellW = 150, cellH = 75, gapX = 70, topY = 60;

    // Row 1: real hypervisor nodes
    let xOff = 50;
    let row1Bottom = topY;
    realParents.forEach(p => {
      const children = p.children();
      const n = children.length || 1;
      // Wider grid than √n so labels read better. Cap at 6 cols.
      const cols = Math.max(2, Math.min(n, 6));
      children.forEach((c, i) => {
        const x = xOff + (i % cols) * cellW;
        const y = topY + Math.floor(i / cols) * cellH;
        c.position({ x, y });
        row1Bottom = Math.max(row1Bottom, y);
      });
      xOff += cols * cellW + gapX;
    });
    const row1Width = Math.max(xOff - 50 - gapX, cellW * 6);

    // Row 2: full-width unscheduled bucket below row 1
    if (unsched.length) {
      const u = unsched[0];
      const children = u.children();
      // Pack across the row 1 width — typically more VMs than slots in
      // a single hypervisor row, so this naturally spans wider.
      const cols = Math.max(6, Math.floor(row1Width / cellW));
      const startY = row1Bottom + 130;
      children.forEach((c, i) => {
        c.position({
          x: 50 + (i % cols) * cellW,
          y: startY + Math.floor(i / cols) * cellH,
        });
      });
    }
    cy.fit(undefined, 40);
  }

  // -----------------------------------------------------------------------
  // Public render entry point
  // -----------------------------------------------------------------------
  function render(container, data) {
    lastData = data;
    if (cy) { try { cy.destroy(); } catch {} cy = null; }
    let elements;
    if (currentMode === 'cluster')      elements = buildClusterElements(data);
    else if (currentMode === 'network') elements = buildNetworkElements(data);
    else                                elements = buildStorageElements(data);
    // Some cose layouts crash if the container has 0 width — happens
    // when a subtab is still `hidden` (display:none) at render time.
    // Force a reflow + re-query to be safe.
    if (container.offsetWidth === 0 || container.offsetHeight === 0) {
      // The caller forgot to un-hide the host. Bail visibly instead
      // of silently producing an empty canvas.
      console.warn('topology: container has zero dimensions — subtab still hidden?',
                   container);
      return;
    }
    cy = cytoscape({
      container,
      elements,
      style: baseStyle(),
      layout: layoutFor(currentMode),
      // v1.4.27: smoother + more granular zoom. 0.2 = each wheel notch
      // moves ~5× less than the default, so the user can fine-tune.
      // Cytoscape emits an "unnatural zoom" warning — that's an
      // informational hint, not an error; we accept it.
      wheelSensitivity: 0.2,
      // Wider range gives more headroom to zoom into clusters dense
      // with VMs / out to see the whole layout.
      minZoom: 0.1,
      maxZoom: 4,
      // v1.4.22 UX: clicking a node was being interpreted as the start
      // of a drag even on a 1-pixel movement, swallowing the tap event
      // and hiding the detail panel. Lock node positions globally —
      // the user can still pan/zoom the canvas. A future "Rearrange"
      // toolbar toggle can flip this back on if needed.
      autoungrabify: true,
    });
    if (currentMode === 'cluster') applyClusterLayout(cy);
    // Belt-and-braces: ungrabify any node that managed to slip through.
    cy.nodes().ungrabify();
    // Click → details
    cy.on('tap click', 'node', evt => onNodeTap(evt.target));
    // Pointer cursor on hover
    cy.on('mouseover', 'node', evt => {
      container.style.cursor = 'pointer';
      showHoverTip(evt);
    });
    cy.on('mousemove', 'node', evt => moveHoverTip(evt));
    cy.on('mouseout', 'node', () => {
      container.style.cursor = '';
      hideHoverTip();
    });
    // Apply any pending search highlight after the new render
    applySearchHighlight();
  }

  // -----------------------------------------------------------------------
  // Hover tooltip — shows the FULL VM/node/volume name on a floating div
  // because Cytoscape's canvas can't carry per-node DOM `title` attrs.
  // -----------------------------------------------------------------------
  let _hoverTip = null;
  function showHoverTip(evt) {
    hideHoverTip();
    const d = evt.target.data();
    const text = d.fullName || d.label;
    if (!text) return;
    _hoverTip = document.createElement('div');
    _hoverTip.className = 'topology-hover-tip';
    _hoverTip.textContent = text;
    document.body.appendChild(_hoverTip);
    moveHoverTip(evt);
  }
  function moveHoverTip(evt) {
    if (!_hoverTip) return;
    const e = evt.originalEvent || evt;
    _hoverTip.style.left = (e.pageX + 14) + 'px';
    _hoverTip.style.top  = (e.pageY + 14) + 'px';
  }
  function hideHoverTip() {
    if (_hoverTip) { _hoverTip.remove(); _hoverTip = null; }
  }

  // -----------------------------------------------------------------------
  // Search highlight — sets `.searched` on matching nodes and pans/zooms
  // to the first hit. Idempotent on every render (the class is reapplied
  // from `currentSearch` whenever the graph is rebuilt).
  // -----------------------------------------------------------------------
  let currentSearch = '';
  function applySearchHighlight() {
    if (!cy) return;
    cy.nodes().removeClass('searched');
    const q = (currentSearch || '').trim().toLowerCase();
    if (!q) return;
    const matches = cy.nodes().filter(n => {
      const d = n.data();
      const hay = (d.searchText || (d.fullName || d.label || '').toLowerCase());
      return hay.includes(q);
    });
    matches.addClass('searched');
    return matches;
  }
  function search(query) {
    currentSearch = query || '';
    const matches = applySearchHighlight();
    if (matches && matches.length && cy) {
      try { cy.animate({ fit: { eles: matches, padding: 60 }, duration: 300 }); }
      catch {}
    }
    return matches ? matches.length : 0;
  }

  // -----------------------------------------------------------------------
  // Smooth zoom (v1.4.27). Animated transitions on click/button events so
  // the user feels the zoom is gradual rather than a single jump.
  // -----------------------------------------------------------------------
  function zoomBy(factor) {
    if (!cy) return;
    const target = Math.max(0.1, Math.min(4, cy.zoom() * factor));
    // `renderedPosition` is the screen-coord point that stays fixed
    // during the zoom — passing the canvas centre keeps the user's
    // bearings rather than drifting the graph to a corner.
    cy.animate(
      { zoom: { level: target,
                renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } } },
      { duration: 180, easing: 'ease-out' }
    );
  }
  function zoomFit() {
    if (!cy) return;
    cy.animate({ fit: { padding: 40 } }, { duration: 250, easing: 'ease-out' });
  }

  // v1.4.28: live re-render of label font size. Native Cytoscape
  // ellipsis re-truncates against text-max-width automatically, so we
  // only need to update the font-size style and let cyto reflow.
  function setFontSize(px) {
    labelFontSize = Math.max(8, Math.min(20, parseInt(px, 10) || 11));
    if (!cy) return;
    cy.style()
      .selector('node').style({ 'font-size': labelFontSize })
      .update();
  }

  // -----------------------------------------------------------------------
  // Click actions (safe by default, destructive when unlocked)
  // -----------------------------------------------------------------------
  function onNodeTap(node) {
    const d = node.data();
    // Scope to the active mode's host so we never write into the
    // wrong subtab's sidebar.
    const sidebar = currentHost && currentHost.querySelector('.topology-detail');
    if (!sidebar) return;
    sidebar.innerHTML = renderDetail(d);
    sidebar.scrollTop = 0;
    sidebar.querySelectorAll('[data-act]').forEach(btn => {
      btn.addEventListener('click', () => doAction(btn.dataset.act, d));
    });
  }

  function renderDetail(d) {
    // v1.4.23: defensive — i18n may not yet be on window if the module
    // somehow loads before i18n.js (race we hit in production). Fall
    // back to a no-op translator so the panel still renders and the
    // user can read the raw key — better than throwing TypeError.
    const i = window.i18n || { t: (k) => k };
    if (d.kind === 'node') {
      const n = d.raw;
      return `
        <h3>🖥 ${n.name}</h3>
        <dl class="kv">
          <dt>${i.t('topology.detail.ready')}</dt><dd>${n.ready ? '✓' : '✗ NotReady'}</dd>
          <dt>${i.t('topology.detail.schedulable')}</dt><dd>${n.schedulable ? '✓' : 'Cordoned'}</dd>
          <dt>${i.t('topology.detail.roles')}</dt><dd>${(n.roles||[]).join(', ')||'—'}</dd>
          <dt>${i.t('topology.detail.ip')}</dt><dd>${n.addresses?.InternalIP||'—'}</dd>
          <dt>CPU</dt><dd>${n.allocatable?.cpu||'—'} / ${n.capacity?.cpu||'—'}</dd>
          <dt>${i.t('topology.detail.memory')}</dt><dd>${formatMem(n.allocatable?.memory)} / ${formatMem(n.capacity?.memory)}</dd>
        </dl>
        <div class="actions">
          <button class="btn btn-sm" data-act="node-notes">📝 ${i.t('topology.action.notes')}</button>
          ${destructiveUnlocked ? `
            <button class="btn btn-sm btn-warn" data-act="node-cordon">🚧 ${i.t('topology.action.cordon')}</button>
            <button class="btn btn-sm btn-danger" data-act="node-drain">⚠️ ${i.t('topology.action.drain')}</button>
          ` : ''}
        </div>`;
    }
    if (d.kind === 'vm') {
      const v = d.raw;
      return `
        <h3>💻 ${v.namespace}/${v.name}</h3>
        <dl class="kv">
          <dt>${i.t('topology.detail.phase')}</dt><dd><span class="phase ${v.phase}">${v.phase}</span></dd>
          <dt>${i.t('topology.detail.runStrategy')}</dt><dd>${v.run_strategy}</dd>
          <dt>${i.t('topology.detail.node')}</dt><dd>${v.node || '—'}</dd>
          <dt>${i.t('topology.detail.networks')}</dt><dd>${(v.networks||[]).map(n=>n.ref||n.name).join(', ')||'—'}</dd>
          <dt>${i.t('topology.detail.volumes')}</dt><dd>${(v.volumes||[]).map(x=>x.pvc||x.disk).join(', ')||'—'}</dd>
        </dl>
        <div class="actions">
          <button class="btn btn-sm" data-act="vm-notes">📝 ${i.t('topology.action.notes')}</button>
          <button class="btn btn-sm" data-act="vm-edit">✏️ ${i.t('topology.action.edit')}</button>
          <button class="btn btn-sm" data-act="vm-console">🖥 ${i.t('topology.action.console')}</button>
          <button class="btn btn-sm" data-act="vm-snap">📸 ${i.t('topology.action.snapshot')}</button>
          <button class="btn btn-sm" data-act="vm-migrate">↔ ${i.t('topology.action.migrate')}</button>
          ${v.run_strategy === 'Halted'
            ? `<button class="btn btn-sm" data-act="vm-start">▶ ${i.t('topology.action.start')}</button>`
            : `<button class="btn btn-sm btn-warn" data-act="vm-stop">■ ${i.t('topology.action.stop')}</button>`}
          ${destructiveUnlocked ? `
            <button class="btn btn-sm btn-danger" data-act="vm-delete">🗑 ${i.t('topology.action.delete')}</button>
          ` : ''}
        </div>`;
    }
    if (d.kind === 'volume') {
      const v = d.raw;
      return `
        <h3>🗄 ${v.name}</h3>
        <dl class="kv">
          <dt>${i.t('topology.detail.state')}</dt><dd>${v.state || '—'}</dd>
          <dt>${i.t('topology.detail.health')}</dt><dd>${v.robustness || '—'}</dd>
          <dt>${i.t('topology.detail.size')}</dt><dd>${formatBytes(v.size)}</dd>
          <dt>${i.t('topology.detail.attachedTo')}</dt><dd>${v.attached_to || '—'}</dd>
        </dl>`;
    }
    if (d.kind === 'network') {
      const n = d.raw;
      return `
        <h3>🌐 ${n.name}</h3>
        <dl class="kv">
          <dt>${i.t('topology.detail.type')}</dt><dd>${n.type}</dd>
        </dl>`;
    }
    return '<p>—</p>';
  }

  function formatMem(s) {
    if (!s) return '—';
    const m = /^(\d+)([KMG]i?)$/.exec(s);
    if (!m) return s;
    const v = parseInt(m[1], 10);
    const unit = m[2];
    if (unit.startsWith('K')) return Math.round(v / 1024 / 1024) + ' GiB';
    if (unit.startsWith('M')) return Math.round(v / 1024) + ' GiB';
    return v + ' ' + unit;
  }

  // Format any byte count (Longhorn ships raw bytes as a string, e.g.
  // "42949672960" → "40 GiB") OR a K8s quantity ("10Gi" → "10 GiB").
  // Uses binary prefixes (KiB/MiB/GiB/TiB/PiB) since Longhorn and
  // Kubernetes both work in base 1024. One decimal for sizes < 10 of
  // the chosen unit, integer otherwise.
  function formatBytes(input) {
    if (input === null || input === undefined || input === '') return '—';
    let bytes;
    if (typeof input === 'number') {
      bytes = input;
    } else {
      const s = String(input).trim();
      // K8s quantity ("10Gi", "512Mi") → convert to bytes first
      const k8s = /^(\d+(?:\.\d+)?)([KMGTPE])i?$/.exec(s);
      if (k8s) {
        const v = parseFloat(k8s[1]);
        const mul = ({ K: 1024, M: 1024**2, G: 1024**3,
                       T: 1024**4, P: 1024**5, E: 1024**6 })[k8s[2]];
        bytes = v * mul;
      } else {
        bytes = Number(s);
      }
      if (!Number.isFinite(bytes)) return String(input);   // unparseable → show as-is
    }
    if (bytes === 0) return '0 B';
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
    const i = Math.min(units.length - 1, Math.floor(Math.log2(Math.abs(bytes)) / 10));
    const v = bytes / Math.pow(1024, i);
    const fmt = v >= 10 || i === 0 ? Math.round(v) : (Math.round(v * 10) / 10);
    return fmt + ' ' + units[i];
  }

  async function doAction(act, d) {
    const cluster = window.App?.getCurrentCluster?.()
                 || document.querySelector('.cluster-name')?.textContent?.trim();
    const confirmI18n = (key) => (window.i18n ? window.i18n.t(key) : key);
    // Safe actions (read / edit / open a panel) — no confirm, no lock.
    if (act === 'node-notes')      return window.Notes?.open('node', cluster, d.raw.name);
    if (act === 'vm-notes')        return window.Notes?.open('vm', cluster,
                                            d.raw.namespace, d.raw.name);
    if (act === 'vm-edit')         return window.VmEdit?.open?.(cluster,
                                            d.raw.namespace, d.raw.name);
    if (act === 'vm-snap')         return window.VmSnapshots?.open?.(cluster,
                                            d.raw.namespace, d.raw.name);
    if (act === 'vm-console')      return window.VMConsole?.open?.(cluster,
                                            d.raw.namespace, d.raw.name);
    if (act === 'vm-migrate')      return window.VMMigrate?.open?.(cluster,
                                            d.raw.namespace, d.raw.name);

    const runAction = async () => {
      const label = confirmI18n(`topology.confirm.${act}`).replace('{name}', d.raw.name);
      if (!confirm(label)) return;
      try {
        await dispatchDestructive(act, d, cluster);
        await refresh();
      } catch (e) {
        alert(confirmI18n('topology.actionFailed') + ': ' + (e.message || e));
      }
    };

    // Power actions (start / stop) — confirm only, available without the
    // destructive unlock, for parity with the Virtual machines tab.
    if (act === 'vm-start' || act === 'vm-stop') return runAction();

    // Irreversible actions (delete VM, cordon / drain node) — gated by the
    // destructive unlock on top of the confirm.
    if (act === 'vm-delete' || act === 'node-cordon' || act === 'node-drain') {
      if (!destructiveUnlocked) { alert(confirmI18n('topology.lockedHint')); return; }
      return runAction();
    }
  }

  async function dispatchDestructive(act, d, cluster) {
    const j = (path, body) => fetch(path, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    }).then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); });
    if (act === 'vm-stop') {
      return j(`/api/vm/${cluster}/${d.raw.namespace}/${d.raw.name}/runStrategy`,
               {runStrategy: 'Halted'});
    }
    if (act === 'vm-start') {
      return j(`/api/vm/${cluster}/${d.raw.namespace}/${d.raw.name}/runStrategy`,
               {runStrategy: 'Always'});
    }
    if (act === 'vm-delete') {
      return fetch(`/api/vm/${cluster}/${d.raw.namespace}/${d.raw.name}`,
                   {method: 'DELETE'})
        .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); });
    }
    // node-cordon / node-drain not yet implemented server-side (P2 backlog)
    throw new Error('not yet implemented: ' + act);
  }

  // -----------------------------------------------------------------------
  // Public API
  // -----------------------------------------------------------------------
  // v1.4.26: in-place data update — avoids cy.destroy() + relayout on
  // every 8 s tick, which was shifting elements around under the user's
  // cursor. Only the data() fields (color, label, raw, …) are merged;
  // positions stay put. Caller has already verified element IDs match.
  function applyDataUpdate(data) {
    if (!cy) return;
    let elements;
    if (currentMode === 'cluster')      elements = buildClusterElements(data);
    else if (currentMode === 'network') elements = buildNetworkElements(data);
    else                                elements = buildStorageElements(data);
    const newById = new Map(elements.map(e => [e.data.id, e.data]));
    cy.batch(() => {
      cy.elements().forEach(ele => {
        const nd = newById.get(ele.id());
        if (nd) ele.data(nd);
      });
    });
    applySearchHighlight();
  }

  // v1.6.4: "structure" = each element's id AND, for compound nodes, its
  // parent. A VM that starts/stops/migrates keeps its id but changes
  // parent (its host node vs. the "Stopped / unscheduled" bucket).
  // Comparing ids alone missed that, so applyDataUpdate() only recoloured
  // the VM in place and it stayed visually inside the wrong group. Folding
  // the parent into the comparison makes refresh() fall through to the
  // full re-render, which re-parents the VM under its new host.
  function _structureMap(data, mode) {
    let elements;
    if (mode === 'cluster')      elements = buildClusterElements(data);
    else if (mode === 'network') elements = buildNetworkElements(data);
    else                         elements = buildStorageElements(data);
    const m = new Map();
    elements.forEach(e => m.set(e.data.id, e.data.parent || ''));
    return m;
  }
  function _cyStructureMap() {
    const m = new Map();
    cy.elements().forEach(e => {
      // Read the ACTUAL compound parent (e.parent()), not e.data('parent'):
      // applyDataUpdate merges data without moving the node, so the data
      // field can be stale while the rendered parent is what we must trust.
      const p = e.isNode() && e.parent().nonempty() ? e.parent().id() : '';
      m.set(e.id(), p);
    });
    return m;
  }
  function _mapsEqual(a, b) {
    if (a.size !== b.size) return false;
    for (const [k, v] of a) if (b.get(k) !== v) return false;
    return true;
  }

  async function refresh() {
    if (!lastCluster || !currentHost) return;
    try {
      const r = await fetch(`/api/topology/${encodeURIComponent(lastCluster)}`);
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      lastData = data;
      const container = currentHost.querySelector('.topology-canvas');
      if (!cy) {
        // First render
        if (container) render(container, data);
      } else {
        // Decide between an in-place data update (no relayout, no
        // visual disruption) vs a full re-render (topology changed:
        // VM created / deleted / migrated to another node, network
        // attachment added, etc.). The user complaint that drove
        // v1.4.26 was the 8 s tick shifting everything around — so
        // we DEFAULT to the cheaper update unless we have to redo
        // the layout.
        const newStruct = _structureMap(data, currentMode);
        const oldStruct = _cyStructureMap();
        if (_mapsEqual(newStruct, oldStruct)) {
          applyDataUpdate(data);
        } else {
          // Topology changed — preserve viewport + selection so the
          // user doesn't get a flash of reset zoom/pan.
          const selectedIds = cy.$(':selected').map(e => e.id());
          const zoom = cy.zoom();
          const pan  = cy.pan();
          if (container) render(container, data);
          if (cy) {
            selectedIds.forEach(id => {
              const e = cy.getElementById(id);
              if (e.nonempty()) e.select();
            });
            try { cy.zoom(zoom); cy.pan(pan); } catch {}
          }
        }
      }
      const meta = currentHost.querySelector('.topology-meta');
      if (meta) {
        const cached = data.cached
          ? ` (cache ${Math.round(data.cache_age_s)}s)`
          : '';
        meta.textContent =
          `${data.nodes.length} nodes · ${data.vms.length} VMs · ${data.volumes.length} volumes${cached}`;
      }
    } catch (e) {
      console.warn('topology refresh failed', e);
      const meta = currentHost && currentHost.querySelector('.topology-meta');
      if (meta) meta.textContent = '⚠️ ' + (e.message || e);
    }
  }

  let lastCluster = null;
  function start(cluster, mode = 'cluster') {
    lastCluster = cluster;
    currentMode = mode;
    // Re-bind to the active subtab's host every time start() runs.
    currentHost = document.querySelector(
      `.overview-subtab[data-subtab="${mode}"] .topology-host`
    );
    refresh();
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = setInterval(refresh, REFRESH_INTERVAL);
  }

  function stop() {
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = null;
    hideHoverTip();
    if (cy) { try { cy.destroy(); } catch {} cy = null; }
  }

  function setMode(mode) {
    currentMode = mode;
    currentHost = document.querySelector(
      `.overview-subtab[data-subtab="${mode}"] .topology-host`
    );
    if (lastData && currentHost) {
      const container = currentHost.querySelector('.topology-canvas');
      if (container) render(container, lastData);
    }
  }

  function setDestructiveUnlocked(v) {
    destructiveUnlocked = !!v;
    // Re-render detail panel if a node is currently selected
    if (cy) {
      const sel = cy.$(':selected').first();
      if (sel.length) onNodeTap(sel);
    }
  }

  function isDestructiveUnlocked() { return destructiveUnlocked; }
  function getCurrentMode() { return currentMode; }

  return { start, stop, setMode, refresh,
           setDestructiveUnlocked, isDestructiveUnlocked,
           getCurrentMode, search, setFontSize,
           zoomBy, zoomFit };
})();

window.Topology = Topology;
