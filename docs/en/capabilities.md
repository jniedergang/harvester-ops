# Capabilities

harvester-ops is an operations console for SUSE Harvester clusters. It
started as power-sequencing tooling and now covers most of the day-2
surface. This page tours each capability area, what it does, and where it
lives (CLI vs. web console).

Everything is **multi-cluster** (one `config.yaml` declares N clusters)
and every mutating operation is **tracked as an action** with live logs
and a retained history.

---

## 1. Power sequencing (CLI + console)

The original core, and the only part needed for a pure shutdown/startup
deployment. Available as auditable bash and mirrored in the console.

- **Graceful shutdown** — 8 ordered steps: pre-flight → etcd snapshot →
  optional VM snapshot → ordered VM stop → Longhorn maintenance → cordon
  → shutdown workers → shutdown control-plane.
- **Startup** — 5 steps: power on first control-plane → power on the rest
  → wait for nodes Ready → restore cluster state → restart VMs in
  reverse-group order (parallel within a group).
- **VM ordering groups** — VMs stop/restart in configurable groups:
  sequential *between* groups, parallel *within* a group, with per-group
  priority.
- **Invariants enforced**: no Longhorn data loss, no etcd quorum loss
  mid-shutdown, graceful ACPI stop before storage detaches.

→ Full step-by-step reference: [operating-procedure.md](operating-procedure.md).

```bash
harvester-status   --cluster prod
harvester-shutdown --cluster prod --interactive
harvester-startup  --cluster prod
```

## 2. VM lifecycle (CLI partial, console full)

Manage KubeVirt virtual machines without leaving the console.

- Per-namespace VM list with `runStrategy` and VMI phase.
- Bulk start/stop via `runStrategy` (single VM or whole namespace).
- **Snapshots** — create a `VirtualMachineBackup` (type=snapshot) per VM;
  restore from a snapshot.
- **Live migration** — move a running VM between nodes, with pre-flight
  migration-info checks.
- **Serial console** — in-browser access to a VM.
- **Inline edit** — change CPU / memory / disks / networks and the
  cloud-init payload, then apply.

CLI exposes the start/stop subset via `harvester-status`/`-shutdown -N <ns>`.

## 3. Cluster observability (console)

- **Live topology** rendered with Cytoscape across three views: Cluster
  (nodes), Network, and Storage (Longhorn volumes), with click-to-detail.
- **Overview metrics**: nodes, VMs running, Longhorn volume count and
  rebuild limit, node table.
- **Prometheus `/metrics`** — action counters/durations, in-flight gauge,
  kubectl call outcomes.
- **`/healthz/ready`** — readiness probe returning 503 when config,
  clusters, or the action DB are unhealthy (`/healthz` for liveness).

## 4. Cluster API — downstream RKE2 clusters (console)

Provision and operate downstream Kubernetes clusters on Harvester through
the Cluster API Provider Harvester (CAPHV).

- **Install the stack from an airgap bundle** — cert-manager, CAPI core,
  the RKE2 bootstrap/control-plane providers, CAPHV, and a ClusterClass —
  with step-by-step progress in the dock.
- **Create clusters** via a guided wizard (sizing, image, SSH, network,
  CNI), with a YAML preview (dry-run) before apply.
- **Operate** managed clusters: scale (patches the topology), download
  kubeconfig, view spec/conditions/machines, roll K8s upgrades, delete.
- **Bundle management** — timestamped airgap bundles with active marker,
  inspect, upload, download, and a Harvester-version compatibility check.

## 5. Terraform — infrastructure as code (console)

Drive the Terraform provider for Harvester from saved declarations.

- **Declarations** — named, persisted bundles of N heterogeneous
  resources (VMs, VM images, SSH keys, raw HCL), edited section by section
  (Specs / Disks / Networks / Cloud-init) and applied in one shot.
- **Apply / destroy** with live plan and apply streaming; typed-confirm
  modal on every destroy entry point.
- **Edit deployed resources** — each applied resource writes a sidecar
  JSON so its original spec can be reloaded and edited from the Live
  sub-tab.

## 6. Bare-metal (console)

- **BMC / Redfish discovery** — point at one or many BMC endpoints and
  read back node profiles (model, NICs, power state).
- **Power actions** over Redfish.
- **PXE / DHCP / HTTP** provisioning groundwork (work in progress).

## 7. Operations support (CLI + console)

- **Multi-cluster config** — declare clusters in `config.yaml`; add /
  edit / delete and upload kubeconfig + SSH key from the console;
  connection tests for kubeconfig and SSH.
- **Collaborative notes** — live-synced rich-text notes (Yjs + Tiptap)
  attached per cluster and per node, syncing across browser tabs and
  operators.
- **Support bundles** — collect logs and cluster state into a tarball
  with **anonymisation** (stable placeholders like `<<NODE-1>>`,
  `<<IP-NODE-1>>`); a separate de-anonymisation tool reverses it from the
  mapping for support hand-off.

## 8. Cross-cutting

- **Action tracking + dock** — a persistent bottom dock shows in-progress
  and recent actions on every tab, with live step/log streaming over SSE
  (auto-reconnecting).
- **Internationalisation** — EN + FR complete; IT / ES / DE fall back to
  EN.
- **Theming** — 5 colour themes × dark/light.
- **Accessibility** — keyboard-visible focus, dialog focus trap, tooltips
  on every control (globally toggleable).

---

## What needs what

| You want… | You need |
|---|---|
| Just safe shutdown/startup | CLI scripts only (no UI, no podman) |
| A dashboard + VM management | Web console |
| Provision downstream clusters | Web console + a CAPHV airgap bundle |
| Terraform-managed VMs | Web console + the Terraform provider binary |
| Fully offline operation | The tarball: bundled wheels + OCI image |

See [architecture.md](architecture.md) for how the surfaces sit on top of
the shared engine, and [install.md](install.md) to get started.
