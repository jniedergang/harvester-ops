# Changelog

All notable changes to this project will be documented here.
Format inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This file summarises each minor release; per-patch detail lives in `git log`.

## [1.19.0] - 2026-09-10 - Bare-metal: the tab that drives an install

### Added
- **Bare-metal tab, filled in.** Until now it could discover a BMC and
  send a power action; it now carries the whole chain: the ISO store
  (download with progress, disk headroom, deletion), the discovery, and
  a per-node install form (image, hostname, install disk, management
  NIC, static or DHCP addressing, VIP, DNS, cluster token, OS password,
  SSH keys) posting to `/api/baremetal/install` and tracked in the dock.
- **Install is offered only when the machine can actually do it.** The
  button appears when the BMC exposes a CD virtual media device *and*
  publishes a `Cd` boot target. Otherwise the card shows a badge
  explaining why, which on iLO usually means no Advanced licence.
  Offering the action anyway would walk the operator into a wall thirty
  seconds into an installation.
- **Extra kernel arguments** on the install form (filtered, since they
  land on a grub command line): `harvester.install.skipchecks=true` to
  bypass the hardware preflight, `console=ttyS1,115200` to mirror the
  install onto the BMC serial console, which is the only way to watch an
  unattended install unfold.
- **`docs/en/bare-metal.md` / `docs/fr/bare-metal.md`**: prerequisites,
  the flow step by step, why the ISO has to be remastered, why the
  artifact server is a second listener rather than a Flask route, and a
  troubleshooting section.

### Fixed
Everything in this section came out of driving the tab against two real
iLO 4s rather than out of the unit tests.

- **BMC credentials no longer evaporate.** Actions used to re-read the
  username and password from the discovery form, which is emptied by any
  re-render: the request went out with an empty password and failed as a
  silent 401. They are now kept in the module for the session and never
  sent anywhere but the BMC endpoint they belong to.
- **A click inside the tab wiped the tab.** The content pane carries the
  same `data-subtab="pxe"` attribute as the navigation link, so an
  unscoped selector re-rendered everything 50 ms after *any* click in the
  pane, erasing a discovery that takes a minute to produce. The trigger
  now targets the nav link, and a non-forced re-render keeps what is
  already on screen.
- **The management interface is designated by MAC address.** Redfish
  names both NICs of an XL170r "System Ethernet Interface" and says
  nothing about the name Linux will give them; writing that name into
  the config produced an install with an unfindable management
  interface. The config now emits `hwAddr` when given a MAC, and a real
  interface name still works.
- **Confirmations showed raw `{host}` / `{device}` placeholders**: the
  module's translation helper swallowed the interpolation parameters.
- **The remastering was rewritten around what the ISO actually is.** It
  had been built on assumptions, and every one of them was wrong: the
  official image has a *single* grub config carrying the menu entries
  (the EFI one only chainloads it), its entries already end in
  `${extra_iso_cmdline}`, sourced from `/boot/grub2/harvester.cfg`, and
  its El Torito record is **UEFI-only and hidden** (not a file in the
  tree), so rebuilding the image with `mkisofs` produced an ISO that
  could not boot at all. The step now sets that one variable and copies
  the image with `-boot_image any replay`, which keeps the original boot
  setup by construction instead of trying to recreate it. It also
  verifies the boot record survived, not just the volume label, and it
  never extracts the 7.7 GB payload: two minutes instead of the full
  extract-and-rebuild, and a quarter of the disk space.
- **Unattended installs get `ip=dhcp rd.neednet=1`.** The vendor's
  installer fetches its configuration from `config_url` *before*
  configuring any network. A PXE install has networking already, from
  the kernel's `ip=` parameter; booting the same ISO from virtual media
  has none, so the installer sat silent forever, never emitting a single
  request. This is the difference between an install that happens and
  one that hangs with nothing to show for it.
- **Remastering no longer works in `/tmp`**, which is a tmpfs on many
  hosts. It now works beside the produced image and refuses upfront when
  the space is not there.
- **Generated artifacts left the ISO store.** A run killed mid-flight
  (a server restart during the 30-minute wait) left its 7.7 GB remastered
  image in the store, where the next install picked it as the source
  instead of the official one. Run artifacts now live in a private
  `work/` directory, swept of anything older than a day.
- **`port=0` no longer means "the default port"** in the artifact
  server: an `or` folded the ephemeral-port request into the fixed
  port, which was already taken.
- **The install form shipped the development lab's addresses** as default
  values. Examples now use the documentation range (RFC 5737), and
  switching to DHCP hides the static fields *and* drops their `required`,
  which otherwise blocked submission on invisible fields.

### Changed
- Power and install buttons moved from emoji to the SVG icon set, three
  new icons (`download`, `search`, `install`), and Force off is now
  styled as the destructive action it is.
- 60 new interface strings, complete in the five languages.
- README and capabilities pages: the bare-metal row no longer advertises
  "PXE / DHCP / HTTP groundwork", and the i18n line no longer claims
  IT/ES/DE fall back to English (they have been complete since 1.12.0).

### Tests
- Every `Icons.svg()` reference resolves to a drawn icon (an unknown name
  silently returns an empty string, so a typo produced a blank button).
- Bare-metal front-end, at source level: credentials survive a re-render,
  install gated on virtual media, no BMC-provided field reaches
  `innerHTML` unescaped, every button carries a tooltip.
- The remastering tests now build a synthetic ISO shaped like the real
  one, El Torito record included, and assert what actually breaks a boot:
  the volume label, the boot record, the injected arguments, and the
  network parameters without which the install never starts.
- 466 tests green.

## [1.18.0] — 2026-09-10 — Bare-metal: ISO store, remastering, install orchestration

### Added
- **ISO store** (`GET /api/isos`, `POST /api/iso/fetch`, `DELETE
  /api/iso/<name>`): the image is downloaded **server-side in a stream**
  with progress and a checksum computed on the fly. A multipart upload
  would transit through Werkzeug's temporary spooling, which is a tmpfs
  in the containerised deployment — a 7.6 GB image would land in RAM.
  The ISO is never shipped in the tarball (7.6 GB against 135 MB for the
  whole deliverable).
- **`bin/harvester-iso-remaster.sh`**: injects
  `harvester.install.automatic` and `config_url` into the kernel command
  line of **every** grub config found — BIOS and EFI both, since patching
  one leaves a machine that installs itself in one mode and waits for an
  operator in the other. It refuses an ISO carrying no Harvester install
  entry rather than producing a silently inert image, and verifies the
  `COS_LIVE` volume label survived (the kernel mounts its rootfs by that
  label).
- **Install orchestration** (`POST /api/baremetal/install`): preflight →
  remaster → serve → insert media → one-shot boot → power → wait for the
  new cluster's API → cleanup. The preflight powers the machine on and
  waits for POST before trusting any inventory, then refuses to go
  further without a CD-capable virtual media, a `Cd` boot target and at
  least one disk. Secrets never reach the action label, and the config
  file is written 0600.
- The runner addresses the three known limits of long actions: events
  aggregated (the buffer is bounded), state persisted periodically (a
  Flask restart used to lose a 30-minute run) and a cooperative cancel
  flag (a pure-Python worker cannot be killed).

### Tests
- Config generation parsed by a real YAML parser, including hostile
  values and the DHCP/static split; endpoint validation before any
  hardware is touched; the advertised URL must be routable by the BMC,
  never 127.0.0.1; remastering exercised on a synthetic Harvester-like
  ISO (both grubs patched, label preserved, refusal of a non-installer
  image).

## [1.17.0] — 2026-09-09 — Bare-metal: Redfish virtual media and artifact server

First slice of the zero-touch Harvester install. Everything here was
exercised for real against node3 (ProLiant XL170r Gen9, iLO 4 Advanced).

### Added
- **Redfish virtual media and boot override**: `POST /api/bmc/<host>/
  virtualmedia` (insert/eject an image the BMC fetches over HTTP) and
  `POST /api/bmc/<host>/boot-once` (one-shot boot target). Discovery now
  reports the resolved system/manager paths, the virtual media device,
  the allowed boot targets, the UEFI disk targets and the POST state.
- **Artifact server** (`web/pxe_server.py`): a small plain-HTTP listener
  on its own port, serving only two token-addressed paths (the ISO and
  the node config). It exists because the console serves HTTPS with a
  self-signed certificate behind Basic auth — a BMC can neither
  authenticate nor trust that — and because the dev WSGI server handles
  neither multi-gigabyte files nor Range requests well. Tokens are
  random, time-limited and revoked when the install ends; no directory
  listing, no other path.

### Fixed
- Power actions hardcoded `/redfish/v1/Systems/1`, which broke them on
  iDRAC (`System.Embedded.1`) even though discovery already resolved the
  path dynamically. The resolved path is now reused everywhere.

### What the live test corrected
- iLO 4 publishes `BootSourceOverrideSupported` where recent Redfish
  publishes `...@Redfish.AllowableValues` — reading only one showed an
  empty list of boot targets.
- The HP OEM `InsertVirtualMedia` action **rejects** `Inserted` and
  `WriteProtected` (`ActionParameterUnknown`), which the standard
  `InsertMedia` action expects. The payload now follows the dialect.
- Verified end to end on node3: a test ISO served by the artifact server
  was inserted (`Inserted: true`, `ConnectedVia: URI`), the boot target
  set to `Cd`/`Once`, then reset and ejected.

## [1.16.0] — 2026-09-09 — Delete orphan volumes, SSH key annotation, CPU pinning

### Added
- **Delete an orphaned volume from the Storage view** — the view has
  surfaced them since 1.8.6, but the cleanup still meant kubectl. The
  action sits behind the destructive lock plus a confirm naming the
  claim, and the endpoint independently re-checks that no VM claims the
  PVC (a stale page must not become data loss). Tracked as an action.
- **CPU pinning** in Compute: dedicated placement, isolated emulator
  thread, NUMA passthrough — each written only when enabled.

### Fixed
- Saving cloud-init now syncs the **`harvesterhci.io/sshNames`
  annotation**: the assistant injected the key material into user-data,
  but Harvester lists a VM's keys from that annotation, so its own UI
  showed the VM as having no SSH key. The KeyPair name is sent without
  the "(namespace)" suffix the picker label carries.
- The i18n orphan scanner learned topology.js's `confirmI18n()` alias.

### Verified live on harv1
- A deliberate orphan PVC was created, refused while the destructive
  lock was closed, then deleted through the UI and confirmed gone;
  deleting a PVC still attached to leap156 was refused with 409.
- Pinning and the sshNames annotation applied to leap156, read back
  with kubectl (`["capi-ssh-key"]`), then reverted.

## [1.15.0] — 2026-09-09 — Devices, tolerations, passthrough picker

### Added
- **Devices** in the Firmware tab: serial console, graphics device,
  memory balloon, USB tablet pointer and watchdog (i6300esb with its
  action). KubeVirt treats a missing `autoattach*` as enabled, so the
  patch writes `false` only to disable and removes the key to go back
  to the default — it never freezes a choice the operator did not make.
- **Tolerations** in the Placement tab, to let a VM schedule onto a
  tainted (dedicated or drained) node.
- **PCI / GPU passthrough picker** backed by a new
  `/api/pcidevices/<cluster>` endpoint listing what Harvester
  discovered on the nodes, with the `resourceName` a VM spec must
  reference. Read-only by design: claiming a device unbinds it from its
  host driver, which a console should never do behind the operator's
  back.
- **macvtap / SR-IOV** NIC bindings.

### Verified live on harv1
- Memory balloon disabled, watchdog set to poweroff, toleration added:
  applied, read back with kubectl, then reverted — leap156 came back to
  its exact original spec (USB tablet included).
- **Anti-affinity proven observable on a single node**: a strict rule
  against a tag carried by a running VM left the new VM unschedulable
  with `0/1 nodes are available: 1 node(s) didn't match pod
  anti-affinity rules`; removing the rule and recreating the VMI let it
  start in 40 s. It also confirmed the "applies at next restart"
  banner: the pending pod kept the old spec until the VMI was recreated.
- The passthrough picker lists the 17 real PCI devices of the cluster.

### Not verified (stated in the UI and here)
- Actual PCI/GPU passthrough to a guest: no device could be handed over
  on a single-node production cluster.
- macvtap / SR-IOV at runtime: no such hardware on the test cluster.

## [1.14.0] — 2026-09-09 — Consistent icon set

### Changed
- **The emoji action icons are replaced by an inline SVG set** (user
  report on the VM actions row): the emoji mixed full-colour images
  (📸 📝) with thin glyphs (■ ⚙), rendered at different sizes per
  platform and read poorly at 16 px. The new set is hand-drawn with one
  stroke weight and inherits `currentColor`, so a single set serves the
  five themes and both light and dark — with start staying green and
  stop red across the VM row and the console toolbar alike.
- Applied to the VM actions row, the console toolbar, the snapshot
  panel (restore / delete) and the edit panel's console shortcut.

### Tests
- `tests/api/test_icons.py`: the set is monochrome and theme-aware, no
  emoji left in the two action surfaces, and icons.js loads before its
  users.

## [1.13.0] — 2026-09-09 — Placement, fine disk/NIC options, console shortcut

### Added
- **Placement tab** — a node selector to pin the VM, and rules relative
  to other VMs (keep away from an HA twin, or co-locate with a VM it
  talks to a lot) expressed on Harvester tags. Only `nodeSelector` and
  the pod rules are written: the `nodeAffinity` Harvester derives from
  the VM networks is displayed read-only and left untouched by the
  merge patch (verified live — it survived every apply).
- **Fine disk options** — serial (visible as /dev/disk/by-id inside the
  guest), cache mode, shareable, read-only, dedicated I/O thread. They
  stay absent from the spec when left at their default.
- **NIC boot order** — the field that makes a VM PXE-boot.
- **Console shortcut in the edit panel header**, mirroring the settings
  shortcut the console already offers (generic `headerActions` on
  floating panels).

### Fixed
- Boot order is a single sequence shared by disks AND NICs — a clash
  used to surface as a raw webhook refusal ("already set for a
  different device", hit for real while testing). Each editor now
  checks the other half of the spec and names the offending device
  before anything is sent.

### Tests
- Round-trips for the disk options and the NIC boot order, placement
  mappers (including "never republish nodeAffinity"), console-shortcut
  wiring. Live on harv1: options and placement applied to leap156,
  verified with kubectl, clash refused client-side, then everything
  reverted through the UI back to the original spec.

## [1.12.0] — 2026-09-09 — Firmware tab, compute ceilings, Harvester tags

### Added
- **Firmware tab** — boot mode (BIOS / UEFI / UEFI + Secure Boot), TPM
  2.0 with optional persistent state, machine type and firmware serial.
  This is what modern guests need: Windows 11 and recent SLE/openSUSE
  images will not boot in legacy BIOS, and Windows 11 also wants a TPM.
  Secure Boot automatically pulls in the SMM feature KubeVirt requires
  (and drops it again when leaving Secure Boot).
- **Compute** — hot-plug ceilings (`cpu.maxSockets`, `memory.maxGuest`)
  so CPU/RAM can be added later without a reboot, the CPU model
  (host-model / host-passthrough, which decides live-migration
  compatibility), and the scheduling reservations (limits/requests)
  behind an advanced fold.
- **General** — the guest hostname, and an editor for the **Harvester
  tags** (`tag.harvesterhci.io/*`) shown in the Harvester UI. Removing
  a tag sends an explicit null, since a merge patch otherwise just
  merges the remaining ones and the tag survives.

### Changed
- **The dashed underline announcing tooltips is gone** (user report):
  it decorated every label of the visual editors and turned the panels
  into a grid of dashes. The help cursor and the hover bubble remain.
- The Compute / General / Lifecycle labels inherited from earlier
  versions were still hardcoded English — now translated in all five
  languages like the rest.

### Tests
- Tag mapper (only `tag.harvesterhci.io/*`, internal labels untouched),
  explicit null on tag removal, firmware patch shape (BIOS clears the
  bootloader, Secure Boot implies SMM, TPM detach), compute optional
  keys. Verified live on harv1: tag added then removed on onit-repro,
  General dry-run green, Firmware/Compute reading real VM specs.

## [1.11.0] — 2026-09-09 — Guided snapshot restore

### Added
- **Restoring a snapshot is now a guided sequence** (user request):
  the restore button opens options instead of a bare confirm — take a
  safety snapshot of the CURRENT state first (before the stop, so it
  captures the live state you might want back) and stop the VM
  automatically (Harvester requires a stopped VM for an in-place
  restore). Both are on by default, and the whole snapshot -> stop ->
  restore runs as one tracked action with live steps. If the rollback
  turns out wrong, the safety snapshot is right there to return to.

### Fixed
- A restore action stayed "running" for up to 20 minutes after the
  restore had actually finished: the completion poll only recognised a
  `Complete` condition, but Harvester 1.8 signals a finished restore
  with `Ready=True` / `InProgress=False`. Both schemes are accepted now,
  so the action reports done within a minute (seen live: two restores
  stuck at "InProgress=False,Ready=True" before the fix).

### Tests
- Guided-flow ordering (safety snapshot before the stop, then restore),
  the Harvester 1.8 completion condition, and the UI options. Verified
  on the live cluster: full snapshot -> stop -> restore -> VM running,
  action done in ~1 min.

## [1.10.1] — 2026-09-09 — Snapshot panel: honest columns, actionable restore error

### Fixed
- The snapshot panel showed a permanent "0%" Progress column: a
  VirtualMachineBackup of type snapshot never carries status.progress
  (verified live, including during creation — the field belongs to
  export backups). The dead column is gone.
- Restoring a snapshot while the VM runs surfaced the raw webhook
  error ("The request is invalid: ... Please stop the VM"). The
  endpoint now pre-checks the VMI and answers with an actionable
  message before spawning anything: stop the VM first — Harvester only
  restores onto a stopped VM (localised in all five languages).
- The remaining hardcoded English strings of the panel (create button,
  hint, confirm dialogs, progress verbs) joined the i18n dictionaries
  in all five languages.

## [1.10.0] — 2026-09-09 — Full localisation: German, Spanish, Italian

### Added
- **The console is now fully localised in five languages**: German,
  Spanish and Italian join English and French with complete coverage
  (374 strings each — every tab, tooltip, confirm, editor and hint;
  they previously fell back to English on all but ~150 basics). Seven
  missing French bare-metal strings were filled too, and two ghost
  keys from a pre-1.5 UI were purged from the partial dictionaries.

### Tests
- The translation-parity baseline drops from 1129 accepted holes to
  **zero**: any new string must now ship in all five languages or the
  suite fails.

## [1.9.1] — 2026-09-09 — README refresh

### Changed
- README capability map updated for everything shipped since 1.6.7
  (visual editors, cloud-init assistant, safety nets, state-aware
  restart, Wake-on-LAN, storage map); screenshot gallery reshot in the
  SUSE default theme against a live cluster, with two new tiles
  (storage topology, VNC console).

## [1.9.0] — 2026-09-09 — State-aware restart, Wake-on-LAN power-on

### Added
- **The startup now restores the pre-shutdown state instead of booting
  everything**: the shutdown annotates only the VMs it actually stops
  (with their original run strategy) and purges stale annotations;
  the startup restarts exactly that set, restores each VM's original
  strategy, and consumes the annotation. Before, every Halted VM —
  including ones deliberately stopped weeks earlier — was rebooted
  with runStrategy Always.
- **Wake-on-LAN power-on**: a node with `wol_mac` in the config is
  powered on by magic packet during the startup steps (python3, then
  wakeonlan/ether-wake as fallbacks) instead of prompting the operator
  to press the button; nodes without it keep the prompt. The packet is
  **re-sent while the node stays unpingable**: the OS kills networking
  before the actual power-off, so a single packet fired on "ping died"
  can land during shutdown and be ignored (race seen live).

### Fixed
- Reading the restart annotation via jsonpath bracket syntax
  (`annotations['a.b/c']`) silently returns empty — the whole
  state-restore read path now goes through a go-template helper
  (verified against the live cluster).
- The VM restart step now **waits for the KubeVirt virt-api webhook**
  (server-side dry-run probe, then per-patch retries): nodes Ready
  does not mean KubeVirt ready, and every runStrategy patch was being
  rejected minutes after boot ("no endpoints available for virt-api").

### Internal
- package.sh removes a stale images/harvester-ops-ui.tar before podman
  save (repacking failed with "docker-archive doesn't support modifying
  existing images").

### Verified live (full real cycle on harv1)
- Shutdown: etcd snapshot in 2 s, Longhorn wait instant (2 pod volumes
  listed as ignored), node powered off — 51 s end to end (was ~4 min
  with a red step and a false warning).
- Startup: WoL boot (~40 s), API wait, virt-api wait, then exactly the
  3 VMs the shutdown had stopped came back with their original run
  strategies — the 11 deliberately-stopped VMs stayed down, and the
  restart annotations were consumed.

## [1.8.9] — 2026-09-09 — Shutdown audit: etcd snapshot fixed, real safety nets

Audit of a real full-cluster shutdown run (2026-09-09 00:36) that
exposed three defects, all reproduced from the live log.

### Fixed
- **The etcd snapshot never worked**: it called an etcdctl binary at a
  path RKE2 does not ship (etcd runs as a static pod), failing
  instantly on every real run. It now uses the native
  `rke2 etcd-snapshot save` (verified on the live cluster: snapshot
  saved).
- **A failed safety check no longer sails through --yes**: the failure
  branch used confirm(), which auto-approves in batch mode — the
  destructive sequence completed with a red step and exit 0. A failed
  etcd snapshot (or VM volumes still attached) now ABORTS the run in
  non-interactive mode, with a clear error; `--force` (CLI) or the new
  Force checkbox (console) continues past it, and interactive mode
  still asks.
- **The Longhorn detach wait only tracks VM volumes**: it used to count
  every attached volume, including pod-held ones (monitoring, upgrade
  log archives) that can never detach by stopping VMs — burning the
  whole 3-minute timeout and ending on a false consistency warning.
  Pod volumes are now listed once as ignored (they stop with the
  node); the wait completes as soon as VM volumes are free, and names
  the offenders if they are not.

### Tests
- `tests/api/test_shutdown_audit.py` locks all three fixes (script
  source + CLI flag plumbing + console checkbox and i18n); the live
  VNC banner test now skips cleanly when the cluster is powered off.

## [1.8.8] — 2026-09-08 — ISO vs disk representation, source images

### Added
- The Storage view now distinguishes **content**, not just the guest
  device: a volume created from an ISO image renders as a round disc
  (💿) like a CD-ROM drive, and every image-backed volume shows its
  **source image** (display name) in the detail panel. Resolution is a
  server-side two-hop join: volume.spec.backingImage -> Longhorn
  BackingImage (harvesterhci.io/imageId annotation) -> VMImage, with
  ISO-ness read from the display name or source url extension. Search
  matches "iso" and image names.

## [1.8.7] — 2026-09-08 — CD-ROMs identified in the Storage view

### Added
- CD-ROM devices are now identifiable in the Storage topology: the
  device type (disk/cdrom/lun) travels from the VM spec through the
  topology snapshot, a CD-ROM renders as a round disc (💿) instead of
  the cylinder, an **empty drive** (cdrom device with no media) stays
  visible as a grey disc marked "empty", and the volume detail panel
  gains a Device row. Verified live on both cases (empty drive, and
  media inserted from an ISO-image PVC).

## [1.8.6] — 2026-09-08 — Storage view: which volume is attached to what

### Changed
- The Storage topology is now **VM-centric** (user ask): VM → volume
  groups flowing left-to-right, volumes labelled with their real PVC
  claim name (kubernetesStatus join to the Longhorn volume, instead of
  the unreadable pvc-<uid>), guest disk name on the edge, boot disk
  first. Volumes claimed by no VM gather in an **Unattached volumes**
  section — on the test cluster this immediately surfaced 20 orphaned
  PVCs left behind by deleted VMs. Node attachment and replica
  placement moved from edge spaghetti to the volume detail panel
  (PVC, consuming VM, guest disk, state, size, replicas).
- A red volume border now means a real fault (degraded/faulted while
  attached); detached volumes with unknown robustness show a neutral
  border instead of a false alarm.

### Tests
- Reducer test for the kubernetesStatus PVC join; storage-view wiring
  and EN+FR key assertions; the i18n orphan-key scanner learned the
  `i.t()` alias used by topology.js (baseline debt shrank 132 → 105).

## [1.8.5] — 2026-09-08 — Tooltip debt cleared: every surface localised

### Fixed
- The hardcoded-title debt tracked since the v1.7.1 audit is CLEARED:
  the advanced surfaces (BMC power controls, the whole Cluster API tab,
  the notes editor toolbar, Terraform declarations) now use styled i18n
  tooltips (EN + FR) instead of ~53 hardcoded native titles — several
  of which were French-only (notes toolbar) or English-only. Inputs
  with a validation pattern keep a native title (it doubles as the
  browser's validation hint), now localised too.
- The 22 literal titles of the page template (sidebar tabs, VM table
  sort headers, sub-tabs, refresh buttons) are wired to the existing
  data-i18n-title mechanism, so they translate at startup.

### Tests
- Audit baselines dropped to zero and locked (any new hardcoded title
  fails the suite); new template-title audit; i18n parity baseline
  recomputed (EN+FR complete, IT/ES/DE still fall back to EN).

## [1.8.4] — 2026-09-08 — Network topology: per-network band layout

### Fixed
- The Network topology view piled every VM into a tiny overlapping ring
  around the network hub (cose force layout with an ideal edge length
  far too short for 14 VM boxes). It now reads like a rack diagram:
  one horizontal band per network, the switch on the left, its member
  VMs in a grid on the right (running VMs first, then by name; big
  networks on top). Multi-NIC VMs sit in the band of their first
  interface and keep cross-band edges to the others; VMs with no
  network get a bottom band. Deterministic and overlap-free (verified:
  0 box overlaps on a 14-VM cluster, was unreadable before).

### Tests
- Band-layout wiring assertions; a `_cy()` test hook on the Topology
  module lets e2e checks measure real node positions.

## [1.8.3] — 2026-09-08 — SUSE brand theme, list-card ergonomics

### Added
- **SUSE brand theme, now the default**, in dark and light: the real
  suse.com identity — pine `#0c322c` background family with jungle
  green `#30ba78` accents in dark; white with pine text and a jade
  darkened to `#1d8653` for AA contrast in light; persimmon warnings,
  waterhole-blue info. The official **SUSE variable typeface** is
  vendored (~80 KB of woff2 subsets + OFL license — airgap-safe,
  no CDN) and wired through a per-theme `--font-ui` variable; the
  four other themes keep the system font stack.
- Text fields with common values (interface names, disk sizes,
  timezone, locale, keyboard, groups, shell, NTP servers, mount
  points, devices, permissions) now offer a **suggestion dropdown
  that never blocks free input** (native datalist combos).

### Fixed
- "+ Add" on a repeatable list (interfaces, disks, extra disks) no
  longer duplicates the name of an existing sibling: the new card is
  auto-named with the first free name (eth0 taken, next is eth1;
  /dev/vdb taken, next is /dev/vdc).
- The per-card summary header now follows the fields live instead of
  staying frozen at render time ("eth0 — dhcp" over an eth1 field).
- The boot-time theme fallback in the page head and the theme module
  were out of sync with the intended default.

### Tests
- Vendored-font assets and default-theme assertions; newItem
  collision-avoidance evaluated in node; datalist/live-header
  source-level checks (377 API tests green).

## [1.8.2] — 2026-09-08 — Cloud-init assistant: full module coverage

### Added
- User report: "many parameters missing" — the assistant now covers the
  breadth of common cloud-config modules, organised in titled sections:
  identity (hostname, FQDN + manage_etc_hosts, timezone, locale,
  keyboard layout), SSH access policy (ssh_pwauth, disable_root,
  expire-passwords-at-first-login), richer users (groups, shell),
  packages (+ reboot-if-required), storage (grow root partition, and
  repeatable **extra disks formatted & mounted** via fs_setup + mounts —
  pairs with the visual disk editor), repeatable **write_files**
  (path/permissions/content as YAML literal blocks), NTP servers,
  **trusted CA certificates** (PEM — e.g. an internal CA), bootcmd, and
  runcmd. Network-data now supports **multiple interfaces** (per-NIC
  dhcp/static + MTU) and global DNS/search domains (type: nameserver).
  All generator output remains parser-validated in tests.

## [1.8.1] — 2026-09-08 — Cloud-init assistant

### Added
- **Cloud-init assistant** in the VM edit panel: a fold above the
  user-data / network-data editors with two generator forms — system
  (hostname, timezone, package refresh/upgrade, packages, run commands,
  repeatable users with optional password, passwordless sudo, a
  Harvester SSH KeyPair picker fed by `/api/sshkeys`, and raw public
  keys) and network (DHCP or static with CIDR address, gateway, DNS).
  "Generate" writes clean `#cloud-config` / network-data v1 YAML into
  the editors (with a confirm when overwriting) — the editors remain
  the saved truth, and the assistant is deliberately one-way: it never
  pretends to parse arbitrary existing YAML back into form fields. The
  generated YAML is covered by parser-validated tests, including
  quoting of hostile strings (quotes, colons, pipes).

### Tests
- 6 more in `tests/api/test_vm_edit.py` (generator output parsed with a
  real YAML parser, minimal output, static-requires-address, wiring and
  EN/FR keys). Suite: 370 passing.

## [1.8.0] — 2026-09-08 — Visual disk & network editors

The Disks and Network tabs of the VM edit panel were raw JSON textareas
(labelled YAML, actually JSON). They are now card-based visual editors.

### Added
- **Disk editor** — one card per disk (summary header, name, disk/cdrom,
  virtio/sata/scsi bus, boot order with 0 = excluded) and a three-mode
  volume source: attach an **existing PVC** (new `/api/pvcs/<cluster>`
  list endpoint), create a **new disk from a Harvester image** (inherits
  the image's storage class), or a **new blank disk** (size + storage
  class). New disks go through Harvester's `volumeClaimTemplates`
  annotation — the same mechanism as the Harvester UI — so the PVC is
  provisioned automatically; removing a disk drops its template entry
  and keeps the PVC (stated in the UI). Cloud-init volumes show as a
  locked card; anything the form does not model (containerDisk, lun,
  volume-less devices) is carried through untouched.
- **Network editor** — one card per interface: bridge (multus network
  dropdown) or masquerade (pod network), NIC model, optional MAC with
  validation. Client-side checks (RFC 1123 names, duplicates, namespace
  of PVCs, MAC format) + the existing server dry-run for real apiserver
  validation. A raw-JSON fold preserves the old editing path.
- The TFForm engine now accepts caller-supplied schema objects (no fake
  TF_SCHEMA kinds), optional bilingual labels per field, per-item card
  headers, and initial values for ref dropdowns (edit flows).

### Fixed
- `PATCH /api/vm/...` dropped the WHOLE `metadata` object (a v1.6.x
  sanitisation ternary) — annotation patches, including the General
  tab's description, silently never applied. Identity fields are still
  stripped; annotations now pass (required by volumeClaimTemplates).
- VM edits are now tracked in the dock/Activity (`vm-edit:<ns>/<name>`
  actions with error_summary), per the everything-is-tracked rule.
- The edit panel's Reset button and post-Apply refresh were dead (the
  refresh callback was stored on the panel API but looked up on the DOM
  node); reopening the panel stacked duplicate nav listeners; `esc()`
  did not escape quotes inside value attributes.
- TFForm: mid-list removals silently dropped fields on read (index-based
  lookup over a sparse DOM) and could collide input names on the next
  add; an emptied repeatable list now CAN be emitted (`emitEmptyLists`)
  for full-state consumers.
- The FR translation of the whole Terraform UI never worked: four files
  read `localStorage['harvester_ops_lang']` while i18n persists under
  `harvester_ops_language`. All TF_SCHEMA bilingual labels now resolve.
- kubectl missing on the server returns a clean 502 on VM get/patch
  instead of an unhandled 500.

### Tests
- `tests/api/test_vm_edit.py` (16 tests) — mapper round-trips through
  node on a faithful VM fixture (identity, passthrough protection, VCT
  add/drop, image storage-class inheritance, validation errors,
  masquerade), `/api/pvcs` contract, metadata-patch fix, action
  tracking, source-level wiring and EN/FR keys. `/api/pvcs` joined the
  list-endpoint contract suite. Suite: 364 passing.

## [1.7.1] — 2026-09-08 — Console toolbar, tooltip audit, panel-resize overhaul

### Added
- **Console toolbar** — VM power controls right in the console title bar:
  start, graceful stop and hard reset (confirm-gated), plus shortcuts to
  the snapshot manager and the VM settings overlay. Hard reset is a new
  tracked action (`POST …/restart`: deletes the VMI and waits for a
  fresh Running one — what `virtctl restart` does).
- **Two-phase reconnect** (eager 400 ms phase after a disconnect) so a
  hard reset reattaches fast enough to catch the firmware splash.
- **Panels resize from every edge and corner** (8 handles).

### Fixed
- Topology detail panel: Edit and Snapshot did nothing — the dispatcher
  called `window.VmEdit`/`VmSnapshots` (wrong case) and the optional
  chaining swallowed the miss. Locked by a regression test.
- Snapshot restore failed with "delete policy with backup type snapshot
  for replacing VM is not supported": the VirtualMachineRestore manifest
  now pins `deletionPolicy: retain`, and restore failures carry
  `error_summary` like every other runner (note: Harvester requires the
  VM to be stopped before an in-place restore — that message now reaches
  the dock verbatim).
- Panel drag/resize moved to pointer capture: the old
  document-level mouseup pattern lost the button release over the noVNC
  canvas (which captures pointer events) and the panel stayed glued to
  the cursor.
- 1:1 scale mode scrolls natively instead of clipping the framebuffer.
- The console-session cap (429) is treated as transient by the client
  and retried instead of ending the session.
- **Tooltip audit** (user report: dashed underline + help cursor on
  toolbar buttons): the `.tip` text ornament no longer applies to
  buttons, and the daily surfaces (console toolbar, VM row actions,
  dock, floating panels, snapshots, migrate, clusters, activity nav) now
  use styled EN+FR i18n tooltips instead of hardcoded English `title=`.
  The remaining hardcoded-title debt (advanced surfaces: capi, notes,
  terraform, bmc) is frozen by a per-file baseline test.

### Tests
- +13 across `test_vm_console.py`, `test_snapshot_restore.py`,
  `test_tooltips_audit.py` and `test_topology_vm_actions.py`. Suite: 348
  passing.

## [1.7.0] — 2026-09-08 — In-browser VNC console

The "VM console — coming soon" placeholder kept its promise: the console
button now opens a real graphical console in the browser.

### Added
- **In-browser VNC console** — noVNC 1.7.0 vendored under
  `web/static/vendor/novnc/` (MPL-2.0, license shipped; ~300 KB loaded
  lazily on first open). The Flask side relays RFC 6143 bytes between the
  browser and the KubeVirt `vnc` subresource over WebSockets using
  `flask-sock` + `simple-websocket`, both already shipped — zero new
  dependency, airgap posture intact. Upstream auth comes from the cluster
  kubeconfig (client cert or bearer token); cert material is staged 0600
  under the tmpfs and deleted before any network I/O.
- **Full-boot capture** — the console auto-retries (2 s, up to ~90 s) and
  attaches as soon as qemu exposes the display: open the console on a
  stopped VM, start it, and the firmware/GRUB/kernel output appears.
- **Ticket-gated access** — a WebSocket handshake cannot carry
  credentials (and flask-sock sends the 101 before any decorator runs),
  so an authenticated, rate-limited endpoint issues single-use 30 s
  tickets bound to one VM, and doubles as the pre-flight that returns
  readable errors (VM stopped, unknown cluster, session cap).
- Toolbar: connection status, Ctrl-Alt-Del, fit-to-window / 1:1 scaling.
- `harvester_ops_vnc_sessions` gauge; a global session cap (8) keeps
  console relays from exhausting the request-thread pool;
  `get virtualmachineinstances/vnc` joined the permissions matrix.

### Fixed
- Restored console panels lost their saved geometry: the panel opener
  never returned the panel api to the layout-restore machinery. It does.

### Tests
- `tests/api/test_vm_console.py` (20 tests) — vendored tree + license,
  lazy-import and teardown wiring, kubeconfig parsing (token auth,
  https-only, no temp-file leak), ticket single-use/expiry/VM binding,
  pre-flight HTTP semantics (404/409/429/400), plus a `--live` test that
  dials the real cluster and reads the RFB banner. Suite: 335 passing.

## [1.6.8] — 2026-07-02 — Packaging works from any host Python

### Fixed
- `package.sh` bundled wheels with the host interpreter's tags: on a
  rolling host (Python 3.13+) the C-extension wheels (PyYAML,
  MarkupSafe…) came down as `cp313` and the BCI Python 3.11 image build
  failed with `No matching distribution found` (`from versions: none`).
  Wheels are now cross-downloaded explicitly for `cp311` /
  `manylinux2014_x86_64` + `manylinux_2_28_x86_64` (`--only-binary`),
  independent of whatever Python the build host runs.
- The tarball shipped a nested duplicate of every wheel
  (`web/vendor/vendor/`, ~35 MB dead weight): `cp -r web …` already
  carries `web/vendor`, and a second `cp -r web/vendor` into the
  existing target nested it. It also embedded the build host's
  `__pycache__/*.pyc`. Both are gone; host artefacts are pruned from
  the assembled tree.

## [1.6.7] — 2026-07-02 — README screenshots

### Added
- Screenshot gallery in `README.md` (assets under `docs/assets/`):
  adaptive light/dark cluster-topology hero (`<picture>` +
  `prefers-color-scheme`), plus overview, VM lifecycle, shutdown
  sequencer and Activity views. Captured in English against a live
  single-node Harvester v1.8.0 cluster (18 VMs), 1600×900 @2x.

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
