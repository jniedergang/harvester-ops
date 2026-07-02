# Changelog

All notable changes to this project will be documented here.
Format inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This file summarises each minor release; per-patch detail lives in `git log`.

## [1.6.6] — 2026-07-02 — Dock buttons fully i18n

### Fixed
- The dock's Details/Hide toggle rendered hardcoded French labels
  («Détails», «Masquer») regardless of the UI language — the dock
  re-renders every 3s outside the data-i18n scan, so static translation
  passes never reached it. Labels and tooltips now resolve through i18n
  (new keys `dock.details` / `dock.hideDetails` / `dock.showDetails`,
  EN + FR — the two title keys were referenced but never defined).

### Tests
- `test_dock_button_labels_are_i18n` — no hardcoded French literals left
  in dock.js, keys present in both EN and FR dicts. The referenced-key
  scanner now also parses the dock's `tr('key', fallback)` helper so
  such keys can't silently rot. Suite: 315 passing.

## [1.6.5] — 2026-07-02 — Failed actions explain themselves; durable history

Born from a live incident: after a Harvester upgrade left the cluster's
`virt-api` webhook without endpoints, every VM start from the UI failed
with a bare "exit 1" — the actual kubectl error ("no endpoints available
for service virt-api") was captured but never surfaced, and the failed
runs later vanished from the Activity tab.

### Fixed
- **VM start/stop errors now carry the kubectl explanation.**
  `_vm_action_runner` used `check_call(stderr=PIPE)` — with `check_call`
  the pipe is never read, so `CalledProcessError.stderr` is `None` and the
  emitted message was `str(e)`: the full command line (kubeconfig path
  included — a path we never want in UI events) without the actual error.
  The runner now captures stderr and surfaces its last line in the step
  event, the dock card, the Activity row tooltip and the details panel.
- **Action history no longer evaporates.** Runs were persisted to SQLite
  on completion, but nothing ever read the DB after startup: the 1h
  in-memory GC (and any Flask restart) silently emptied the Activity tab.
  `/api/activity` now merges the persisted history (last 500 runs,
  `?limit=` up to 500), `/api/action/<id>` falls back to the DB, and
  `/api/stream/<id>` replays persisted events so the details panel works
  for any historical run.

### Added
- `error_summary` on actions (API + SQLite, additive `ALTER TABLE`
  migration — hot-applicable, no downtime): last meaningful stderr line
  of the failing `kubectl` call or engine script.

### Tests
- `tests/api/test_actions_persistence.py` (11 tests) — stderr surfacing
  (incl. no-kubeconfig-path-leak regression), timeout wording, script
  last-stderr capture, DB read-back, `/api/activity` merge + dedup +
  limit, `/api/action` DB fallback, `/api/stream` replay. Suite: 314
  passing.

## [1.6.4] — 2026-06-17 — Overview/topology: live re-grouping + full VM actions

### Fixed
- A VM started (or stopped) from the Overview/Cluster topology now moves
  to the correct group on the next refresh. The auto-refresh decided
  between an in-place data merge and a full re-render by comparing only
  the element-id set; a started VM keeps its id but changes compound
  parent (host node vs. the "Stopped / unscheduled" bucket), so it was
  recoloured in place and stayed in the wrong group. The comparison now
  folds in each node's parent, so a grouping change triggers the
  re-render (which already preserves zoom/pan and selection).

### Changed
- The VM detail panel (select a VM in the topology) now exposes the full
  action set: notes, edit, **console**, snapshot, **migrate**, and a
  contextual start/stop — instead of only notes/edit/snapshot. Start and
  stop are available with a confirm but without the destructive unlock,
  matching the Virtual machines tab; delete stays behind the unlock.

### Tests
- `tests/api/test_topology_vm_actions.py` (8 tests) — full action set,
  console/migrate wiring, start/stop ungated, delete still gated,
  parent-aware refresh comparison. Suite: 303 passing.

## [1.6.3] — 2026-06-17 — Documentation overhaul

### Changed
- Repositioned the project from "graceful shutdown/startup tooling" to an
  **operations console** for Harvester, reflecting the actual surface:
  power sequencing, VM lifecycle, cluster observability, Cluster API /
  CAPHV provisioning, Terraform IaC, and bare-metal discovery.
- `README.md` (EN + FR): new capability map; quick-start split into the
  CLI power-sequencing core and the optional web console.
- `docs/{en,fr}/architecture.md`: corrected the execution model — the
  bash scripts are the sole path for **power sequencing only**; VM /
  Cluster API / Terraform / bare-metal surfaces are the console calling
  `kubectl` / providers / Redfish directly. Updated diagram + trust model.

### Added
- `docs/en/capabilities.md` + `docs/fr/capabilites.md`: a full tour of
  every capability area (CLI vs. console, what-needs-what matrix).

## [1.6.2] — 2026-06-02 — UX sweep

### Added
- Reconnecting `EventSource` helper (`web/static/js/sse-reconnect.js`)
  with exponential backoff (1, 2, 4, 8, 16 s; cap 30 s; 5 attempts) and
  a lifecycle callback. All SSE consumers (terraform, support, dock,
  shutdown/startup follow, CAPI install) migrated through it.
- Settings modal accessibility: `role="dialog"`, `aria-modal="true"`,
  `aria-labelledby`, focus trap on Tab / Shift+Tab, restoration of the
  previously focused element on close.

### Tests
- 295 API tests (+17 vs 1.6.1), still ~13 s on a developer laptop.

## [1.6.1] — 2026-06-12 — Quality sweep

### Added
- `web/requirements-lock.txt` generated by `pip-compile --generate-hashes`
  — use `pip install --require-hashes -r web/requirements-lock.txt` in
  production to block supply-chain hijacking.
- `CONTRIBUTING.md` with repo layout, test policy, release flow.
- `.pre-commit-config.yaml`: `bash -n`, `py_compile`, `pytest --collect-only`.

### Fixed
- XSS hardening in `clusters.js`: every `e.message`, `d.error`,
  `n.hostname`, `file.name` interpolation now wrapped in `escapeHtml(...)`.

## [1.6.0] — 2026-06-12 — Observability + performance

### Added
- Prometheus metrics endpoint (`/metrics`) and readiness probe
  (`/healthz/ready`) returning 503 on broken config / DB / clusters.
- SQLite WAL mode + busy_timeout + compound index on `actions(cluster,
  started_at DESC)` for the action history.

### Changed
- Multi-node SSH probes parallelised via `ThreadPoolExecutor`
  (`max_workers=min(8, len(nodes))`).

## [1.5.0 – 1.5.7] — 2026-06 — Terraform declarations + memory and security

Two major capability changes in the 1.5.x line:

- **Declarations** (1.5.0 – 1.5.5): multi-resource bundles in the
  Terraform tab, saved in `localStorage`, applied as a single
  declaration. Each resource (VM, image, SSH key, raw HCL) edits via
  section buttons (Specs, Disks, Networks, Cloud-init) opening a
  `FloatingPanel`. Sidecar JSON written next to each `.tf` to allow
  editing already-deployed resources from the Live sub-tab. Confirm-by-
  typing modal on every destroy entry point.
- **Hardening** (1.5.6 – 1.5.7): security headers (CSP, XFO, XCTO,
  Referrer-Policy), RFC 1123 validation on every `<namespace>` and
  `<name>` path param, rate-limiter on `POST /api/action` and
  `/api/terraform/*/apply*`, kubeconfig chmod 0600 + isolation per
  Terraform workspace, in-memory GC for the `ACTIONS{}` and
  `_notes_docs{}` dictionaries (1 h TTL), invalidation of topology /
  list caches on mutation, frontend timer cleanup on `beforeunload`.

## [1.4.0] — 2026-05-29 → 2026-06-02 — Topology view, themes, BMC discovery, Terraform schemas

Highlights of the 1.4 line:

- **Cluster topology** (Cytoscape.js): live node / network / volume
  graph with click-to-detail panels, incremental refresh, search.
- **Theming**: 5 colour themes × dark/light = 10 palettes; defaults to
  Tokyo Night Day (light). All component colours go through CSS vars.
- **Bare-metal / BMC**: Redfish discovery + power actions, integrated
  with the action tracking dock.
- **Terraform integration**: schema-driven forms with dropdowns
  populated from live cluster data (namespaces, networks, images, SSH
  keys, storage classes, cloud-init configs); HCL renderer on the
  backend; full apply / destroy / state / clean-stale endpoints; first
  e2e tests in Playwright.
- **CAPHV deployment**: install / uninstall stack from active bundle
  with step-by-step progress in the dock; airgap-friendly bundle
  upload/download with timestamp + active marker.
- **Per-cluster notes** (Yjs + Tiptap) live-syncing across browser
  tabs, with backend `/api/notes` WebSocket sync and SQLite
  persistence.
- **Accessibility**: keyboard-visible focus on every interactive
  element, tooltip reveal on Tab.

## [1.3.0] — 2026-05-27 — CAPHV integration (stage 1 + 2)

- CAPI / CAPHV bundle build, diag endpoint and UI status; install / uninstall flow.
- Wizard to create downstream RKE2 / CAPI clusters from the UI.
- Single-pane `/review` dashboard with KPIs auto-refreshing every 30 s.

## [1.2.0] — 2026-05-27 — Multi-cluster + VM tab

- VM tab with bulk actions, runStrategy edit, snapshots, live migration.
- Floating-panel system for the VM Console, Snapshots, Migrate, Edit.
- Bottom dock that shows in-progress and recent actions across tabs.

## [1.1.0] — 2026-05-27 — Web UI polish + multi-cluster CRUD

- Cluster CRUD: create, edit, delete clusters from the UI; upload
  kubeconfig + SSH key; persisted in `/etc/harvester-ops/`.
- Sidebar collapsible to icon-only mode.
- Per-namespace VM actions in the UI (matching the `-N` CLI flag).
- Support bundle with anonymization + de-anonymization tool.

## [1.0.0] — 2026-05-27 — Initial release

- CLI: `harvester-shutdown.sh`, `harvester-startup.sh`,
  `harvester-status.sh` (multi-cluster, dry-run, interactive,
  step-by-step events).
- Optional Flask web UI with SSE-streamed step progress, basic auth,
  self-signed TLS, podman-packaged for airgap installs.
- `package.sh` produces a self-contained `tar.gz` + `sha256` with
  scripts, web vendor wheels, OCI image, docs (EN + FR), example
  config.
- Bilingual docs: install, architecture, operating procedure,
  troubleshooting.
