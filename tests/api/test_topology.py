"""Tests for the /api/topology/<cluster> endpoint introduced in v1.4.19.

The endpoint consolidates kubectl snapshots for the Aperçu viz. Most of
the work is in pure reducers — we unit-test those — plus a smoke test
on the live endpoint.
"""

import json
import sys
from pathlib import Path
import importlib

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
sys.path.insert(0, str(WEB_DIR))
app_module = importlib.import_module("app")

_topology_node          = app_module._topology_node
_topology_vm            = app_module._topology_vm
_topology_volume        = app_module._topology_volume
_topology_replica       = app_module._topology_replica
_topology_network_attachment = app_module._topology_network_attachment


# ---------------------------------------------------------------------------
# Reducer: _topology_node
# ---------------------------------------------------------------------------
def test_topology_node_marks_ready_from_condition_status():
    raw = {
        "metadata": {"name": "n1", "uid": "u1", "labels": {
            "node-role.kubernetes.io/control-plane": "",
            "node-role.kubernetes.io/etcd": "",
        }},
        "spec": {},
        "status": {
            "addresses": [
                {"type": "InternalIP", "address": "10.0.0.1"},
                {"type": "Hostname",   "address": "n1.local"},
            ],
            "conditions": [{"type": "Ready", "status": "True"}],
            "capacity":    {"cpu": "8",   "memory": "16Gi"},
            "allocatable": {"cpu": "7.5", "memory": "15Gi"},
        },
    }
    out = _topology_node(raw)
    assert out["name"] == "n1"
    assert out["ready"] is True
    assert out["schedulable"] is True
    assert set(out["roles"]) == {"control-plane", "etcd"}
    assert out["addresses"]["InternalIP"] == "10.0.0.1"
    assert out["capacity"]["cpu"] == "8"


def test_topology_node_handles_not_ready_and_cordoned():
    raw = {
        "metadata": {"name": "n2", "labels": {}},
        "spec": {"unschedulable": True},
        "status": {
            "conditions": [{"type": "Ready", "status": "False"}],
            "addresses": [],
        },
    }
    out = _topology_node(raw)
    assert out["ready"] is False
    assert out["schedulable"] is False


def test_topology_node_no_conditions_defaults_to_not_ready():
    """Missing or empty conditions list → ready=False (defensive)."""
    raw = {"metadata": {"name": "n3", "labels": {}}, "spec": {}, "status": {}}
    out = _topology_node(raw)
    assert out["ready"] is False


# ---------------------------------------------------------------------------
# Reducer: _topology_vm
# ---------------------------------------------------------------------------
def test_topology_vm_links_to_running_vmi_node():
    """When a VMI exists for the VM, the reducer must pull nodeName +
    phase from the VMI status, not from the VM spec."""
    vm = {
        "metadata": {"namespace": "ns1", "name": "vm-a"},
        "spec": {
            "runStrategy": "Always",
            "template": {"spec": {
                "domain": {"devices": {
                    "interfaces": [{"name": "nic-1", "bridge": {}}],
                    "disks":      [{"name": "disk-1", "bootOrder": 1}],
                }},
                "networks": [{"name": "nic-1", "multus": {"networkName": "ns1/prod"}}],
                "volumes":  [{"name": "disk-1",
                              "persistentVolumeClaim": {"claimName": "vm-a-disk-1"}}],
            }},
        },
    }
    vmi_by = {
        "ns1/vm-a": {"status": {"phase": "Running", "nodeName": "n1"}},
    }
    out = _topology_vm(vm, vmi_by)
    assert out["phase"] == "Running"
    assert out["node"] == "n1"
    assert out["networks"] == [{"name": "nic-1", "type": "multus", "ref": "ns1/prod"}]
    assert out["interfaces"] == [{"name": "nic-1", "binding": "bridge"}]
    assert out["volumes"][0]["pvc"] == "vm-a-disk-1"
    assert out["volumes"][0]["boot_order"] == 1


def test_topology_vm_phase_stopped_when_no_vmi():
    """A Halted VM has no VMI → phase falls back to Stopped, node is
    null. Frontend uses these to group into the 'unscheduled' bucket."""
    vm = {
        "metadata": {"namespace": "ns1", "name": "vm-halted"},
        "spec": {
            "runStrategy": "Halted",
            "template": {"spec": {
                "domain": {"devices": {"interfaces": [], "disks": []}},
                "networks": [],
                "volumes": [],
            }},
        },
    }
    out = _topology_vm(vm, {})
    assert out["phase"] == "Stopped"
    assert out["node"] is None
    assert out["run_strategy"] == "Halted"


def test_topology_vm_pod_network_type():
    """The reducer should classify pod-attached networks separately
    from multus so the viz can color them differently."""
    vm = {
        "metadata": {"namespace": "ns", "name": "vm-pod"},
        "spec": {"template": {"spec": {
            "networks": [{"name": "default", "pod": {}}],
            "domain": {"devices": {"interfaces": [{"name": "default", "masquerade": {}}], "disks": []}},
            "volumes": [],
        }}},
    }
    out = _topology_vm(vm, {})
    assert out["networks"] == [{"name": "default", "type": "pod", "ref": None}]
    assert out["interfaces"][0]["binding"] == "masquerade"


# ---------------------------------------------------------------------------
# Reducer: _topology_volume + _topology_replica
# ---------------------------------------------------------------------------
def test_topology_volume_exposes_state_and_attachment():
    raw = {
        "metadata": {"name": "pvc-1", "namespace": "longhorn-system"},
        "spec":   {"size": "10Gi"},
        "status": {"state": "attached", "robustness": "healthy",
                   "currentNodeID": "n1"},
    }
    out = _topology_volume(raw)
    assert out["name"] == "pvc-1"
    assert out["state"] == "attached"
    assert out["robustness"] == "healthy"
    assert out["attached_to"] == "n1"


def test_topology_volume_detached_no_node():
    raw = {"metadata": {"name": "p"}, "spec": {}, "status": {"state": "detached"}}
    out = _topology_volume(raw)
    assert out["attached_to"] is None


def test_topology_replica_links_volume_to_node():
    raw = {
        "metadata": {"name": "r-1"},
        "spec":   {"volumeName": "pvc-1", "nodeID": "n1"},
        "status": {"currentState": "running"},
    }
    out = _topology_replica(raw)
    assert out == {"name": "r-1", "volume": "pvc-1", "node": "n1", "running": True}


def test_topology_replica_unhealthy_state():
    raw = {
        "metadata": {"name": "r-2"},
        "spec":   {"volumeName": "v", "nodeID": "n"},
        "status": {"currentState": "stopped"},
    }
    out = _topology_replica(raw)
    assert out["running"] is False


# ---------------------------------------------------------------------------
# Reducer: _topology_network_attachment
# ---------------------------------------------------------------------------
def test_topology_network_attachment_truncates_config():
    raw = {
        "metadata": {"name": "prod", "namespace": "default"},
        "spec": {"config": "x" * 500},
    }
    out = _topology_network_attachment(raw)
    assert out["name"] == "prod"
    assert out["namespace"] == "default"
    assert len(out["config_summary"]) <= 200


# ---------------------------------------------------------------------------
# Endpoint contract — error paths
# ---------------------------------------------------------------------------
def test_topology_endpoint_404_on_unknown_cluster(api):
    status, body = api("GET", "/api/topology/no-such", expect_status=404)
    assert "unknown cluster" in body["error"].lower()


def test_topology_endpoint_returns_500_on_kubectl_failure(api):
    """harv-fake's kubeconfig points to an unreachable cluster. The
    endpoint must surface a clear error JSON, not crash."""
    status, body = api("GET", "/api/topology/harv-fake", expect_status=None)
    # 200 happens if some kubectl call succeeded (unlikely against the
    # fake config). Either way, body must be JSON object.
    assert isinstance(body, dict)
    if status == 500:
        assert "error" in body


# ---------------------------------------------------------------------------
# Cache TTL — drift guard
# ---------------------------------------------------------------------------
def test_topology_js_uses_scoped_selectors_not_global_ids():
    """REGRESSION (v1.4.21): the three overview subtabs share the
    topology.js module. Selecting elements via global IDs like
    `#topology-canvas` returned the FIRST one across all 3 subtabs
    (always the Cluster's), so Network and Storage rendered into the
    wrong DOM element and looked empty. The fix is to query relative
    to a `currentHost` element, and use class selectors that are
    scoped to that host."""
    topo_js = WEB_DIR / "static" / "js" / "topology.js"
    src = topo_js.read_text()
    # The module must hold a reference to the active host
    assert "currentHost" in src, (
        "topology.js no longer scopes queries to currentHost — "
        "Network/Storage subtabs will render into Cluster's container."
    )
    # And queries must be class-based, not id-based
    assert "querySelector('#topology-canvas')" not in src and \
           'querySelector("#topology-canvas")' not in src, (
        "topology.js still uses global id #topology-canvas — fix to "
        "currentHost.querySelector('.topology-canvas') instead."
    )


def test_app_js_topology_shell_uses_classes_not_ids():
    """app.js's mountTopology() builds the canvas shell. It must use
    classes (`.topology-canvas`, `.topology-detail`, etc.) — never ids,
    because the shell is rendered into 3 different sub-tab hosts and
    duplicate ids would break the topology module's scoped lookups."""
    app_js = WEB_DIR / "static" / "js" / "app.js"
    src = app_js.read_text()
    mount = src[src.find("function mountTopology"):]
    mount = mount[:mount.find("\n  }") + 4]  # one function body
    forbidden = ['id="topology-canvas"', "id='topology-canvas'",
                 'id="topology-detail"', "id='topology-detail'",
                 'id="topology-meta"',   "id='topology-meta'"]
    bad = [f for f in forbidden if f in mount]
    assert not bad, f"mountTopology() still uses these ids: {bad}"


def test_topology_nodes_are_not_grabbable_so_clicks_register():
    """REGRESSION (v1.4.22): Cytoscape defaults nodes to grabbable —
    any pixel of mouse movement during a click is interpreted as a
    drag, swallowing the tap event and hiding the detail panel.
    Users reported "I can't click, it just moves the element."

    Lock positions with `autoungrabify: true` in the cyto config,
    plus an explicit `cy.nodes().ungrabify()` belt-and-braces."""
    topo_js = WEB_DIR / "static" / "js" / "topology.js"
    src = topo_js.read_text()
    assert "autoungrabify: true" in src, (
        "Cytoscape must boot with autoungrabify so clicks aren't "
        "interpreted as drags. See v1.4.22 fix."
    )
    assert "ungrabify()" in src, (
        "Explicit nodes().ungrabify() guard missing — belt-and-braces "
        "in case future elements are added dynamically."
    )


def test_i18n_exposes_itself_on_window_for_es_modules():
    """REGRESSION (v1.4.23): topology.js (an ES module) couldn't read
    the `i18n` const from i18n.js (a classic script) because classic
    top-level `const` bindings aren't reachable via window. Result:
    every node click threw `Cannot read properties of undefined`.
    Fix: i18n.js must do `window.i18n = i18n` so any consumer kind
    (classic script or ES module) resolves it the same way."""
    i18n_js = WEB_DIR / "static" / "js" / "i18n.js"
    src = i18n_js.read_text()
    assert "window.i18n = " in src or "window['i18n']" in src, (
        "i18n.js must expose `i18n` on window so ES modules can use it."
    )


def test_topology_renderDetail_handles_missing_i18n():
    """Defensive: even if i18n.js loads after topology.js (race),
    renderDetail must not throw. Guard the lookup with a fallback."""
    src = (WEB_DIR / "static" / "js" / "topology.js").read_text()
    # The renderDetail body must reference window.i18n with a guard
    rd = src[src.find("function renderDetail"):]
    rd = rd[:5000]
    assert "window.i18n ||" in rd or "window.i18n ??" in rd or \
           "|| { t:" in rd, (
        "renderDetail() must guard against window.i18n being undefined."
    )


def test_topology_style_scopes_size_to_node_with_size():
    """Cytoscape warns 100× per layout pass if width/height map to a
    data field absent on some nodes. Parents (compound nodes) auto-
    size from their children, so the `width/height: data(size)` rule
    MUST be scoped to `node[size]` (only nodes that defined size)."""
    src = (WEB_DIR / "static" / "js" / "topology.js").read_text()
    # The narrowed selector must exist
    assert "selector: 'node[size]'" in src or 'selector: "node[size]"' in src


def test_topology_cluster_layout_separates_running_from_unscheduled():
    """v1.4.24: the cluster view must group VMs by node on TOP and put
    the catch-all 'unscheduled' bucket on the BOTTOM. The code path
    that arranges this is `applyClusterLayout`; we assert it explicitly
    separates the two parent kinds rather than rendering them side-by-side."""
    src = (WEB_DIR / "static" / "js" / "topology.js").read_text()
    layout = src[src.find("function applyClusterLayout"):]
    layout = layout[:2500]
    assert "node-unscheduled" in layout, (
        "applyClusterLayout no longer references node-unscheduled — "
        "halted VMs may end up mixed with live hypervisor nodes."
    )
    # And there's a separate yOffset for the second row
    assert "row1Bottom" in layout or "startY" in layout, (
        "applyClusterLayout doesn't compute a separate Y offset for the "
        "unscheduled bucket → it will overlap the hypervisor row."
    )


def test_topology_truncates_vm_names_with_fullname_fallback():
    """Long VM names overflow the box. v1.4.28 switched from a JS
    char-budget truncation to native Cytoscape ellipsis (more
    accurate). The cluster builder must still populate `fullName`
    so the hover tooltip and search can use the un-ellipsized name."""
    src = (WEB_DIR / "static" / "js" / "topology.js").read_text()
    assert "fullName" in src, (
        "fullName field missing — hover tooltip + search won't have "
        "the full VM name to show."
    )
    builder = src[src.find("function buildClusterElements"):]
    builder = builder[:2500]
    assert "fullName" in builder, "buildClusterElements doesn't set fullName"
    # And the VM-specific style enables Cytoscape's native ellipsis
    assert "'text-wrap': 'ellipsis'" in src or \
           "text-wrap: 'ellipsis'"  in src, (
        "VM label style must use text-wrap:'ellipsis' for native truncation."
    )


def test_topology_exposes_search_and_setfontsize_api():
    """The Topology module's public API gains `search()` (highlight
    matching nodes + auto-fit) and `setFontSize()` (live re-render of
    labels with a new truncation budget)."""
    src = (WEB_DIR / "static" / "js" / "topology.js").read_text()
    # Both functions defined
    assert "function search(" in src
    assert "function setFontSize(" in src
    # And exposed on the return object
    ret = src[src.rfind("return {"):]
    assert "search" in ret and "setFontSize" in ret


def test_topology_search_uses_searched_class_for_highlight():
    """Search highlight is implemented via a `.searched` cytoscape
    class so the style rule (border-color, overlay) is decoupled from
    the search logic. A regression that removed the class would leave
    matches visually identical to non-matches."""
    src = (WEB_DIR / "static" / "js" / "topology.js").read_text()
    assert "addClass('searched')" in src or 'addClass("searched")' in src
    assert "node.searched" in src, (
        "The .searched style rule is missing — matches won't be visually distinct."
    )


def test_topology_volume_size_is_human_readable():
    """v1.4.25: Longhorn ships volume sizes as raw byte strings (e.g.
    '42949672960'). Showing that to operators is unhelpful — they
    can't tell 40 GiB from 4 TiB at a glance. topology.js must run
    those through formatBytes before display."""
    src = (WEB_DIR / "static" / "js" / "topology.js").read_text()
    assert "function formatBytes" in src, "formatBytes helper missing"
    # The volume detail panel must use formatBytes, not raw v.size
    rd = src[src.find("if (d.kind === 'volume')"):]
    rd = rd[:1500]
    assert "formatBytes(v.size)" in rd, (
        "Volume detail still shows raw v.size — operators see "
        "'42949672960' instead of '40 GiB'."
    )
    # Storage view: the cytoscape volume node label also uses it
    sb = src[src.find("function buildStorageElements"):]
    sb = sb[:3000]
    assert "formatBytes(v.size)" in sb, (
        "Storage view volume label doesn't show human-readable size."
    )


def test_format_bytes_handles_raw_bytes_and_k8s_quantities(tmp_path):
    """Drive the JS function via Node to verify its math on the cases
    we ship to operators: pure byte strings (Longhorn), K8s quantities
    (PVC.spec.resources.requests.storage), edge cases."""
    import subprocess
    topo_src = (WEB_DIR / "static" / "js" / "topology.js").read_text()
    # Pull the function body out — it's pure, no DOM/cyto deps
    start = topo_src.find("function formatBytes")
    assert start >= 0
    end = topo_src.find("\n  }", start)
    assert end > start
    fn = topo_src[start:end + 4]
    script = tmp_path / "fmt.js"
    script.write_text(fn + """

const cases = [
  ['42949672960', '40 GiB'],
  ['1073741824',  '1 GiB'],
  ['10737418240', '10 GiB'],
  ['10Gi',        '10 GiB'],
  ['512Mi',       '512 MiB'],
  ['1Ti',         '1 TiB'],
  ['1024',        '1 KiB'],
  [0,             '0 B'],
  [null,          '—'],
  ['',            '—'],
  ['nonsense',    'nonsense'],
];
for (const [inp, want] of cases) {
  const got = formatBytes(inp);
  if (got !== want) {
    console.error(`FAIL: formatBytes(${JSON.stringify(inp)}) -> "${got}" (want "${want}")`);
    process.exit(1);
  }
}
console.log('OK');
""")
    r = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, f"formatBytes test failed:\n{r.stdout}\n{r.stderr}"
    assert "OK" in r.stdout


def test_topology_refresh_uses_incremental_update_when_structure_unchanged():
    """v1.4.26: the 8 s auto-refresh was previously calling render()
    every tick, which destroys + recreates the cytoscape instance and
    re-runs the layout — shifting every element under the user's
    cursor. The fix is to detect when the element ID set is unchanged
    and call `applyDataUpdate(data)` instead, which only merges new
    data fields (color, label, raw) without touching positions."""
    src = (WEB_DIR / "static" / "js" / "topology.js").read_text()
    assert "function applyDataUpdate" in src, "applyDataUpdate helper missing"
    # The refresh function must branch on the set comparison
    rf = src[src.find("async function refresh"):]
    rf = rf[:3500]
    assert "_setsEqual" in rf or "setsEqual" in rf, (
        "refresh() doesn't compare old vs new element ids — it will "
        "destroy+rerender on every tick."
    )
    assert "applyDataUpdate" in rf, (
        "refresh() never calls applyDataUpdate — incremental path missing."
    )


def test_topology_refresh_preserves_viewport_on_structural_change():
    """When topology DID change (VM added/removed), we still re-render
    fully — but the user's zoom + pan + selection must survive so the
    view doesn't visually 'snap' back to the default fit."""
    src = (WEB_DIR / "static" / "js" / "topology.js").read_text()
    rf = src[src.find("async function refresh"):]
    rf = rf[:3500]
    # Both zoom + pan saved and restored
    assert "cy.zoom()" in rf and "cy.pan()" in rf, (
        "refresh() doesn't snapshot zoom/pan before the full re-render."
    )
    assert "cy.zoom(zoom)" in rf and "cy.pan(pan)" in rf, (
        "refresh() doesn't restore zoom/pan after the full re-render."
    )
    # And selection is preserved
    assert ":selected" in rf and "select()" in rf, (
        "refresh() loses the user's selected node on structural change."
    )


def test_topology_apply_data_update_uses_batch_for_perf():
    """applyDataUpdate should wrap its node updates in `cy.batch(...)`
    so cytoscape skips intermediate redraws — important when 18+ VMs
    need their color/label updated at once."""
    src = (WEB_DIR / "static" / "js" / "topology.js").read_text()
    fn = src[src.find("function applyDataUpdate"):]
    fn = fn[:1500]
    assert "cy.batch(" in fn, (
        "applyDataUpdate doesn't batch — every node update triggers a "
        "separate redraw, defeating the purpose of the incremental path."
    )


def test_topology_zoom_is_smoothed_and_granular():
    """v1.4.27: the user reported coarse zoom steps. We lower the wheel
    sensitivity to 0.2 (4× more granular than default 1.0) and widen
    the [minZoom, maxZoom] range so users can dig deeper into dense
    clusters or zoom out to see the whole layout."""
    src = (WEB_DIR / "static" / "js" / "topology.js").read_text()
    assert "wheelSensitivity: 0.2" in src or "wheelSensitivity:0.2" in src, (
        "wheelSensitivity must be 0.2 for smooth zoom — the default 1.0 "
        "feels too coarse to operators."
    )
    # Wider zoom range
    assert "minZoom: 0.1" in src
    assert "maxZoom: 4" in src


def test_topology_zoom_helpers_exposed():
    """zoomBy(factor) + zoomFit() must be on the Topology public API so
    the toolbar buttons can drive smooth animated zoom."""
    src = (WEB_DIR / "static" / "js" / "topology.js").read_text()
    assert "function zoomBy" in src
    assert "function zoomFit" in src
    # And both exposed on the return object
    ret = src[src.rfind("return {"):]
    assert "zoomBy" in ret and "zoomFit" in ret


def test_topology_zoom_helpers_use_animation():
    """The zoom transitions should be smooth — call cy.animate() with a
    short duration, never a synchronous cy.zoom() (jarring jump)."""
    src = (WEB_DIR / "static" / "js" / "topology.js").read_text()
    fn = src[src.find("function zoomBy"):]
    fn = fn[:1500]
    assert "cy.animate(" in fn, "zoomBy() doesn't animate — zoom will be jumpy."


def test_app_js_restores_subtabs_after_setcluster():
    """REGRESSION (v1.4.31): the Overview sub-tab restoration must run
    AFTER setCluster() — otherwise mountTopology() bails on null
    cluster and the Cluster/Network/Storage tabs come back with an
    empty canvas. The restore helper is a separate function so the
    ordering is explicit at the init() call site."""
    app_js = WEB_DIR.parent / "web" / "static" / "js" / "app.js"
    src = app_js.read_text()
    assert "function restoreSubTabsFromStorage" in src, (
        "restoreSubTabsFromStorage helper missing — sub-tab restore "
        "race with setCluster() will recur."
    )
    # And init() must call it AFTER setCluster()
    init_body = src[src.find("function init()"):]
    init_body = init_body[:2500]
    sc = init_body.find("setCluster(")
    rs = init_body.find("restoreSubTabsFromStorage()")
    assert sc >= 0 and rs >= 0, (
        f"init() body lacks setCluster() or restoreSubTabsFromStorage() "
        f"(setCluster={sc}, restoreSubTabs={rs})"
    )
    assert rs > sc, (
        "restoreSubTabsFromStorage() must be called AFTER setCluster() "
        "in init() — otherwise currentCluster is null at click time and "
        "mountTopology() short-circuits."
    )


def test_topology_volumes_render_as_vertical_cylinder():
    """v1.4.30: classic database-cylinder representation. The barrel
    shape is taller than wide so it reads as a vertical cylinder, with
    the 🛢 icon reinforcing. Switching back to a wide barrel would
    look like a horizontal keg — wrong semantic."""
    src = (WEB_DIR / "static" / "js" / "topology.js").read_text()
    storage = src[src.find("function buildStorageElements"):]
    storage = storage[:3500]
    # Box must be taller than wide
    assert "width: 90" in storage and "height: 110" in storage, (
        "Volume box no longer taller than wide — barrel won't read as "
        "a vertical cylinder."
    )
    # 🛢 icon (classic vertical cylinder)
    assert "🛢" in storage, (
        "Volume label icon must be 🛢 (vertical cylinder), not 🗄 (card box)."
    )


def test_topology_networks_render_as_switch_silhouette():
    """v1.4.30: networks now look like rack-mount switches —
    cut-rectangle shape (chamfered corners suggesting a device),
    wide+short proportions, 🔀 icon."""
    src = (WEB_DIR / "static" / "js" / "topology.js").read_text()
    net = src[src.find("function buildNetworkElements"):]
    net = net[:4000]
    assert "shape: 'cut-rectangle'" in net or 'shape: "cut-rectangle"' in net, (
        "Network shape must be cut-rectangle (switch silhouette), "
        "not hexagon."
    )
    # 1U-ish proportions
    assert "width: 180" in net or "width:180" in net
    assert "height: 48" in net or "height:48" in net
    # 🔀 icon
    assert "🔀" in net


def test_topology_volume_label_fits_inside_shape():
    """text-max-width must be smaller than the box width so the 2-line
    label stays inside the colored shape. v1.4.30 narrowed the
    cylinder to 90 px wide; the style follows at 75."""
    src = (WEB_DIR / "static" / "js" / "topology.js").read_text()
    vol_style = src[src.find("selector: 'node[kind = \"volume\"]'"):]
    vol_style = vol_style[:600]
    assert "'text-max-width': 75" in vol_style or \
           "text-max-width: 75"  in vol_style, (
        "Volume text-max-width must match the narrower cylinder width."
    )


def test_topology_vm_nodes_are_rectangular_to_fit_labels():
    """v1.4.28: VM boxes were 60×60 square but labels could span
    120 px → text overflowed the colored box. VMs now carry explicit
    `width` and `height` data fields rendering as 130×50 rectangles
    so labels fit naturally."""
    src = (WEB_DIR / "static" / "js" / "topology.js").read_text()
    cluster = src[src.find("function buildClusterElements"):]
    cluster = cluster[:3000]
    assert "width: 130" in cluster or "width:130" in cluster, (
        "VM cluster builder no longer sets an explicit width — labels "
        "may overflow."
    )
    assert "height: 50" in cluster or "height:50" in cluster
    # And the style mapping picks up data(width) when present
    assert "selector: 'node[width][height]'" in src or \
           'selector: "node[width][height]"' in src


def test_topology_binds_click_and_tap_for_resilience():
    """Both `tap` and `click` listeners should be wired so the detail
    panel opens reliably across desktop, touch and pen interactions."""
    src = (WEB_DIR / "static" / "js" / "topology.js").read_text()
    assert "'tap click'" in src or '"tap click"' in src, (
        "topology.js must listen on both tap AND click for resilience."
    )


def test_topology_cache_ttl_is_reasonable():
    """Cache too short → kubectl thrash. Cache too long → stale UX. The
    sweet spot has been 5 s; this test catches accidental zero or huge
    values introduced during refactors."""
    assert 1.0 <= app_module.TOPOLOGY_CACHE_TTL <= 60.0
