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
  → shutdown workers → shutdown control-plane. A failed safety check
  (etcd snapshot failure, VM volumes still attached) **aborts** the
  sequence in non-interactive mode; pass `--force` (or tick Force in
  the console) to continue past it. The Longhorn wait only tracks
  VM-attached volumes — volumes held by pods (monitoring, upgrade
  logs) stop with the node and are listed as ignored.
- **Startup** — 5 steps: power on first control-plane → power on the rest
  → wait for nodes Ready → restore cluster state → restart VMs in
  reverse-group order (parallel within a group). Nodes with a
  `wol_mac` in the config are powered on by **Wake-on-LAN** (no
  operator prompt); and only the VMs the shutdown actually stopped are
  restarted, each with its original run strategy — VMs deliberately
  stopped before the shutdown stay stopped.
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
- **Snapshots** — create a `VirtualMachineBackup` (type=snapshot) per VM.
  **Guided restore**: restoring offers to take a safety snapshot of the
  current state first and to stop the VM automatically (both on by
  default), so the whole "snapshot the live state, stop, roll back" is
  one tracked action — and if the rollback was a mistake, the safety
  snapshot brings back exactly where you were.
- **Live migration** — move a running VM between nodes, with pre-flight
  migration-info checks.
- **VNC console** — full graphical console in the browser (noVNC over a
  WebSocket relay to the KubeVirt `vnc` subresource). Shows the whole
  boot — firmware, GRUB, kernel — thanks to an auto-retry loop that
  attaches as soon as qemu exposes the display; keyboard/mouse,
  Ctrl-Alt-Del and fit-to-window/1:1 scaling included; the console
  title bar also carries VM power controls (start / graceful stop /
  hard reset) and shortcuts to the snapshot manager and VM settings. Access is
  gated by short-lived single-use tickets issued by an authenticated
  endpoint; the kubeconfig needs `get virtualmachineinstances/vnc`
  (checked by the permissions matrix).
- **Inline edit** — change CPU / memory and the cloud-init payload,
  then apply. **Disks and network interfaces get visual editors**
  (v1.8.0): one card per disk/NIC with dropdowns fed by the live
  cluster (existing PVCs, Harvester images, storage classes, multus
  networks). New disks — blank or from an image — are created through
  Harvester's `volumeClaimTemplates` mechanism; bus, boot order,
  cdrom, bridge/masquerade binding, NIC model and MAC are all
  editable, with client-side validation plus server dry-run. A raw
  JSON fold remains for exotic specs. A **Firmware** tab covers boot
  mode (BIOS / UEFI / UEFI + Secure Boot, pulling in the SMM feature
  Secure Boot requires), TPM 2.0 with persistent state, machine type
  and firmware serial — what modern guests such as Windows 11 or
  SLE 16 refuse to boot without. **Compute** adds the hot-plug ceilings
  (max CPU sockets, max guest memory), the CPU model
  (host-model / host-passthrough) and, behind a fold, the scheduling
  reservations. **General** edits the guest hostname and the Harvester
  tags (`tag.harvesterhci.io/*`). Disks carry their fine options
  (serial, cache mode, shareable, read-only, dedicated I/O thread) and
  NICs a boot order for **PXE booting** — the boot sequence is shared
  between disks and NICs, and a clash is caught before it reaches the
  apiserver. A **Placement** tab pins the VM with a node selector,
  adds tolerations, and keeps it away from (or next to) VMs carrying a
  given tag; the node affinity Harvester manages for networking is shown
  read-only and left untouched, and CPU pinning (dedicated placement,
  isolated emulator thread, NUMA passthrough) sits in Compute. The
  Firmware tab also carries the
  **devices**: serial console, graphics, memory balloon, USB tablet
  pointer and watchdog — each written only when it differs from the
  KubeVirt default.

**Shipped but not verified on the test cluster** (stated in the UI, in
the warning style): PCI/GPU passthrough (the picker lists the devices
Harvester discovered, but no device could be claimed on a single-node
production cluster) and the macvtap / SR-IOV NIC bindings (no such
hardware). Everything else on this page was exercised for real. The Cloud-init tab gains an
  **assistant** (v1.8.1) that generates clean cloud-config and
  network-data v1 YAML into the editors — hostname, users (password,
  passwordless sudo, Harvester SSH keys or raw public keys), packages,
  run commands, DHCP or static addressing — review, then save.
  Common-value fields offer a dropdown of suggestions that never
  blocks free input, and added list items are auto-named to avoid
  duplicating a sibling (eth0 taken, next is eth1).

CLI exposes the start/stop subset via `harvester-status`/`-shutdown -N <ns>`.

## 3. Cluster observability (console)

- **Live topology** rendered with Cytoscape across three views: Cluster
  (nodes), Network, and Storage (Longhorn volumes), with click-to-detail.
  The Network view reads like a rack diagram: one band per network,
  switch on the left, member VMs in a grid (running first) — no
  force-layout pile-ups. The Storage view answers "which volume is
  attached to what": VM → volume groups labelled with the real PVC
  claim names (joined to Longhorn volumes), guest disk name on the
  edge, CD-ROM devices and ISO-backed volumes drawn as round discs (💿,
  media or empty drive), every image-backed volume showing its source
  image in the detail panel, and an **Unattached volumes** section that surfaces orphaned
  PVCs — deleted-VM leftovers an operator wants to reclaim, and each of
  them can be **deleted right there** (destructive lock plus a confirm;
  the endpoint re-checks that no VM still claims it). The detail panel
  shows PVC, consuming VM, state, size and replica placement.
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
  (auto-reconnecting). Failed actions carry the underlying error (last
  `kubectl` / script stderr line) in the dock, the Activity table and the
  details panel — never a bare `exit 1`.
- **Durable action history** — the last 500 runs (with their step/log
  events) are persisted in SQLite and served back by the Activity tab and
  its details replay, across UI restarts. In-memory eviction only affects
  live SSE attachment, never the visible history.
- **Internationalisation** — EN + FR complete; IT / ES / DE fall back to
  EN.
- **Icons** — a hand-drawn monochrome SVG set inheriting `currentColor`,
  so one set serves every theme and both modes. It replaced the emoji
  icons, which mixed full-colour images with thin glyphs and read poorly
  at button size.
- **Theming** — 5 colour themes × dark/light. The default SUSE theme
  follows the suse.com identity — pine/jade palette and the official
  SUSE typeface, vendored in the tarball (airgap-safe, OFL licensed).
- **Accessibility** — keyboard-visible focus, dialog focus trap, tooltips
  on every control (globally toggleable), fully localised in **five
  languages** (EN, FR, DE, ES, IT) across every surface, advanced tabs
  included.

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
