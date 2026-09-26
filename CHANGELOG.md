# Changelog

All notable changes to this project will be documented here.
Format inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This file summarises each minor release; per-patch detail lives in `git log`.

## [1.53.0] - 2026-09-26 - Create a cluster from a window with menus

### Changed
- **Cluster API: the "Cluster creation" sub-tab is gone.** A "Create a
  cluster" button in the K8S Clusters tab opens a window, like the creation
  of a VM: it can be minimised to the window bar to check something
  elsewhere, and comes back as it was. A saved choice of the old sub-tab
  now opens K8S Clusters.
- The window is organised in menus: **Essentials** first (name, version,
  control plane and workers, size preset, image, key pair, VM network, IP
  pool: enough to create), then Nodes, Network, Storage, Kubernetes,
  Integrations and Pre-check. Each menu shows how many findings concern
  it, and the action bar sums up the pre-check whatever the open menu
  (a click leads to the details).
- The list of clusters refreshes itself when a creation ends.

### Tests
- The window opened from the K8S Clusters button, minimised and reopened
  with its input kept; the menus; the counters per menu (a refused field
  counts on its own menu); the pre-check status in the action bar; every
  earlier behaviour of the form (values from the cluster, pool, presets,
  request, preview, follow-up).
- Real, on harv1: a cluster created from the window (minimised during the
  creation, then reopened), with kube-vip in place from the start: a
  service deployed on it got its address without any extra step.

## [1.52.1] - 2026-09-26 - Terraform tab: VMs can be created again, one click is one action

### Fixed
- **No VM could be created from the Terraform tab**: the form offered the
  cloud-init types `nocloud` and `configdrive`, and the Harvester provider
  only accepts `noCloud` and `configDrive`, so every plan was refused.
  Declarations saved before this fix are translated when rendered.
- **A click in a declaration window could fire several times**: each redraw
  of the window added its click handlers again. After a few redraws one
  "+ Add" created five resources, and a Dry-run sent 81 requests (most
  refused on the Terraform state lock or the rate limit).
- The destruction of a single resource ended on "apply completed", and its
  log window said "Apply".
- A failed provider installation now says why (for example
  "HTTP Error 404: Not Found") instead of "provider install failed".
- A Dry-run and an Apply of a declaration carried the same action label and
  could not be told apart in the Activity tab.
- **A destroyed VM left its disks behind** (a 10 Gi volume stayed Bound in
  Harvester): disks now go with the VM, unless "delete this disk when the
  VM is destroyed" is unchecked on the disk.

### Tests
- The cloud-init types offered by the form and rendered are the provider's;
  earlier values are translated.
- One click after several redraws adds one resource (fails on the previous
  code).
- The cause of a failed provider installation; the two action labels.
- A destroyed VM takes its disks unless asked otherwise.
- Real, on harv1, from the Terraform tab: a VM declared, planned (one
  request for one click), applied in 32 s and destroyed in 54 s, with no
  volume left behind; the Activity tab tells the plan and the apply
  apart.

## [1.52.0] - 2026-09-26 - Services on the clusters created by Cluster API

### Added
- **Services tab** (Automation, Cluster API): deploy Helm charts on the
  clusters created by Cluster API, through the Cluster API add-on provider
  for Helm (CAAPH v0.6.4). A catalog ready to use (a DNS server with CoreDNS,
  the podinfo test application) and any chart from a Helm repository; a
  form with the tested chart version, the settings, a pre-check as you type
  and a preview of the values the chart receives; per service, the clusters
  it runs on with the state and revision of each release; removal.
- A service is a `HelmChartProxy` next to the cluster, which selects the
  `Cluster` by a label the console sets; CAAPH installs, upgrades and
  uninstalls the chart. Deployment and removal are tracked actions.
- CLI parity: `harvester-capi services | service-check | service-deploy |
  service-remove`.
- **kube-vip in the created clusters**: CAPHV installs the Harvester cloud
  provider but not kube-vip, which announces the address of LoadBalancer
  services; without it they stayed pending forever. The console adds it to
  every cluster it creates, and to an older cluster with its first service
  (the pre-check says so).
- **CAAPH is in the Cluster API bundle** and installed from the Installation
  tab with the other providers, as an optional provider: its absence does
  not block the creation of clusters, and the tab lists it apart.

### Fixed
- `scripts/bundle-capi.sh` announced `--output` but took the path from its
  first argument only.

### Known limits
- When a service of a created cluster goes away in DHCP mode, the Harvester
  cloud provider may leave its LoadBalancer object on Harvester (seen on one
  test cluster, not on another; it holds no address). CAPHV starts the
  cloud provider without a cluster name, so these objects cannot be tied to
  their cluster. Both are to be reported upstream.

### Tests
- Catalog, pre-check, manifest and state reading (`capi_services`); the CLI
  subcommands against a fake management cluster (deploy, blocked when CAAPH
  is missing, a release that never gets ready, removal); the endpoints;
  an optional provider in the stack status.
- Real, on harv1: CAAPH installed from the Installation tab, a cluster
  created from the form, podinfo and CoreDNS deployed from the Services tab
  (kube-vip added by the console on the way), podinfo answering over HTTP
  and the DNS over UDP and TCP on their load balancer addresses, an update
  followed to its new revision, then removal and deletion of the cluster.

## [1.51.0] - 2026-09-26 - A deliverable ready to use: Cluster API bundle, Terraform provider and OpenTofu inside

### Added
- **The deliverable carries the providers of the moment**: the active
  Cluster API bundle (RKE2 providers v0.25.2 and CAPHV v0.10.1 with their
  images, plus the earlier install for Harvester 1.7/1.8) and the Terraform
  provider for Harvester v1.9.0, pinned in `scripts/embedded-providers.env`
  and checked against their published checksums.
- `install.sh` places them in the service's persistent state: the bundle is
  active and the provider installed at first start. A bundle made active or
  a provider installed later from the console is never overwritten by a new
  installation; a provider that came from an earlier deliverable is
  upgraded.
- **OpenTofu in the image** (MPL-2.0, the open and compatible equivalent of
  Terraform), pinned and checked: the Terraform tab works right after
  installation. A `terraform` binary set by the operator keeps priority
  (`HARVESTER_OPS_TF_BIN`).

### Fixed
- **Cluster API bundles could not be added or built in the packaged
  service**: their directory was inside the read-only image. It is now in
  the persistent state.
- **The image had no `xorriso`**, which the bare-metal installation needs to
  prepare the Harvester ISO.

### Tests
- The placement by `install.sh` (fresh install, the operator's choices kept,
  an embedded provider upgraded, a corrupt bundle refused), the package and
  image contents, the choice between Terraform and OpenTofu.
- Real: the package built, installed through the unit's own command, the
  bundle active and the provider found at first start, and a Terraform plan
  run against harv1 with the embedded OpenTofu and provider.

## [1.50.0] - 2026-09-26 - Sign in through Rancher, with the rights Rancher gives

### Added
- **"Sign in with Rancher"** on a new sign-in page, next to local accounts.
  The console is declared once as an OIDC client of Rancher Manager (2.12 or
  later, built-in OIDC provider); someone already signed in to Rancher
  enters without typing anything, otherwise Rancher shows its own sign-in
  page (local account or Keycloak).
- **Rights inherited from Rancher, not copied**: for a Rancher session, every
  action on a cluster goes through Rancher's proxy with that person's own
  token, so the cluster applies the user, group and project rights Rancher
  gives. The console finds which Rancher cluster is which by the UID of
  `kube-system`, leaves out the clusters Rancher does not show that person,
  and refuses any request naming one.
- **Console role from Rancher**: administrator for Rancher administrators
  and listed groups, operator (configurable) for the others. Starting and
  stopping a cluster stay with local accounts (a stopped cluster no longer
  goes through Rancher, and Rancher may run on the cluster being stopped).
- **Sign-out button** next to the session badge; a Rancher session that
  expires sends the page back to the sign-in page.

### Security
- Tokens never leave the server: the browser holds a random session id
  (HttpOnly, SameSite=Lax, Secure behind an HTTPS proxy). The code exchange
  uses PKCE, a single-use state bound to the browser and a nonce; the
  identity is read back from Rancher with the token itself.
- Writes authenticated by the session cookie must come from the console's
  own origin.
- The status script, the VNC console check and the support bundle still
  used the console's own account for a delegated session: they now act
  through Rancher, or are refused to non-administrators.

### Tests
- The sign-in logic against a Rancher simulated after the real one (16),
  the whole flow through the application (15), the sign-in page in a
  browser (6).
- Real, on Rancher Prime 2.14.1 and harv1, through `https://harvops.home.lo`:
  the Rancher administrator entered in 6 s without typing, listed 14 VMs and
  stopped one through Rancher, and was refused the cluster shutdown; a
  temporary "cluster member" account was refused by harv1 itself
  (`User "u-t286c" cannot get resource "virtualmachines"`). Three
  differences with the design, found this way and handled: Rancher refuses
  to derive a longer token from an OIDC token (the access token is renewed
  instead), gives `expires_in` in nanoseconds, and discovery waited on
  unavailable clusters.

## [1.49.0] - 2026-09-25 - kube-ovn networks: VPCs, subnets and overlay networks, with their forms

First step of the kube-ovn network settings decided on 20/09: what a
Harvester VM can use today.

### Added
- **VPC tab** (Overview), in the block layout of Fabric and Network: one
  block per VPC, its subnets (range, gateway, overlay network, NAT, DHCP,
  address usage) with the VMs holding an address in each, and how the VPC
  leaves (NAT through the nodes, static routes, peerings, or isolated).
  Findings above the blocks: an overlay network no subnet serves (seen on
  harv1: a VM attached to it would get no address), a subnet whose network
  is gone, overlapping ranges, a nearly full subnet.
- **Forms for a VPC and a subnet** (administrators): a free /24 proposed
  away from the nodes, pods and other subnets, gateway derived, overlay
  network created with the subnet or an existing one reused, outgoing NAT
  only where kube-ovn does it, DHCP for the VMs, advanced options folded;
  a pre-check while typing in the five languages; changes a subnet cannot
  take (range, VPC, network) are refused before sending.
- **Deletion guarded**: a subnet still used by a VM or a pod, or a VPC
  with subnets, is refused, the view saying which before sending
  anything; the overlay network goes with its subnet only if the console
  created it; kube-ovn's own VPC and subnets are read only.
- **`harvester-network` command line** (`inventory`, `check`, `apply`,
  `delete`): the console runs it as a followed action; a creation that
  fails half-way is undone.

### Tests
- Logic against the objects read on harv1 (34 tests), the command line
  with a simulated cluster (10), the endpoints and the admin-only writes
  (5), the view and its forms in a browser (6).
- Real, on harv1 (Harvester v1.9.0, kube-ovn v1.16.2): subnets and a VPC
  created, a VM on a new overlay subnet got its address by DHCP and reached
  the Internet through NAT, deletions refused while in use, then the same
  from the view (create, edit, refused deletion, clean-up), leaving harv1
  as it was.

## [1.48.1] - 2026-09-25 - Restart only the VMs that were running, whatever their run strategy

Since 1.9.0 the startup restarts only the VMs the shutdown stopped. The
shutdown decided "was running" from the run strategy alone, which two cases
got wrong.

### Fixed
- **A VM powered off from its own system came back at startup.** It keeps
  `RerunOnFailure`, Harvester's default, with a finished instance: the
  shutdown recorded it as running. "Running" is now read on the VM
  instance (`Always` still counts as meant to run).
- **A running `Manual` VM stayed off after startup.** Restoring its run
  strategy does not start it; the startup now calls the `start`
  subresource, as `virtctl start` does.
- The shutdown summary counted every VM as stopped ("3 VMs stopped" with
  one already off); it now says how many it stopped and how many were
  already off.

### Tests
- The running rule for every run strategy and instance phase, the `start`
  call against a stand-in `kubectl` (and nothing sent in dry run), the
  summary count.
- Real, on the harvlab2 bench: three VMs (running `RerunOnFailure`, powered
  off from the guest, running `Manual`), a complete shutdown and startup of
  the cluster: the first and the `Manual` one came back, the one powered
  off from its guest stayed off, the resume annotations were consumed.

## [1.48.0] - 2026-09-25 - Creating a Kubernetes cluster works again, from a form that helps

### Added
- **Cluster creation form** (Automation, Cluster API, Cluster creation),
  filled from the cluster itself: Kubernetes versions (the ones created for
  real with the bundle marked "tested"), control plane 1/3/5, workers and
  size presets, the cluster's images (no ISOs, SUSE first, the last choice
  remembered) with the SSH user suggested from the image's system, key
  pairs, networks with their VLAN, IP pools with their ranges and free
  addresses; gateway and mask taken from the pool, DNS remembered. The
  extended options (namespaces, extra pools and networks, data disk, CNI,
  pod and service CIDRs, Rancher import, Fleet add-ons) are folded. Every
  control has a tooltip, and a summary line gives the VMs, vCPUs, memory,
  disk and addresses needed.
- **Pre-check while typing**, in the interface language: what blocks (not
  enough addresses, overlapping CIDRs, image missing or not ready, a
  namespace that already holds a cluster, stack not installed...) and what
  deserves a look (one or an even number of control plane nodes, CPU or
  memory running short under Harvester's overcommit, a version never
  created with the bundle, an unregistered SLES image). Create stays greyed
  out while something blocks.
- **Creation followed in the page and the dock** (infrastructure, control
  plane a/b, workers c/d), ending with the kubeconfig download; cancelling
  removes what was created. Preview shows the manifests with the identity
  secret masked.
- **Installation through Rancher Turtles on Harvester v1.9**: the RKE2 and
  Harvester providers are declared as `CAPIProvider` objects fed from the
  airgap bundle, their images loaded on the nodes over SSH; the
  Installation tab lists them with their state. Leftovers of an earlier
  install next to Turtles are detected and can be removed by an
  administrator. The install checks its result on the cluster: Turtles
  re-applies a provider without removing a label already on a CRD (seen
  when reinstalling), so the contract label the patch removes is taken off
  by the console, and the status reports it while it is there.
- **`harvester-capi` command line**: `status`, `install`,
  `cleanup-legacy`, `inventory`, `check`, `render`, `create`, `delete`. The
  console runs it for every step.
- **`caphv-generate` is shipped** with the console (taken from CAPHV at a
  fixed commit, provenance recorded): without it, every creation used to
  answer that the tool was missing.

### Changed
- The Installation, bundle and Kubernetes cluster views are translated in
  the five languages (about fifty labels, messages and confirmations were
  in English, one in French only), and every value from the cluster is
  escaped.
- Bundle: Kubernetes v1.34.11 by default; Harvester v1.9.x accepted.

### Fixed
- **Creation could not work on Harvester v1.9**, for three reasons in
  CAPHV v0.10.1, worked around by the console until CAPHV ships the fixes:
  the VIP Service it reads is gone (a stand-in is created), its machines
  never looked provisioned to the Cluster API core v1.13 (a patch carried by
  the provider keeps the core on the contract CAPHV follows), and its
  generated templates at `v1alpha1` were refused once copied (moved to
  `v1beta1`).
- **The wait for a new cluster read the v1beta1 status**, which the Cluster
  API core of Harvester v1.9 no longer serves: every creation would have
  ended on a timeout.
- **A cluster was announced ready too early** (seen twice for real):
  Cluster API reports it available as soon as the control plane is up, the
  workers being created afterwards; and a machine counted as soon as its
  node registered, half a minute before the node was Ready. The console now
  waits for the counts the topology asks for, each machine's node Ready, and
  the Available condition when the core publishes one. The cluster list
  reads the same counters from the cluster status.
- **Deleting a cluster left its identity secret behind** (a kubeconfig of
  the Harvester cluster), with the ClusterClass, templates and add-ons. They
  are now removed with the last cluster of the namespace.
- **An image with a space in its name was refused**; images are now
  referenced by their object name.
- **The free addresses of an IP pool could exceed its size** (Harvester's
  counter drifts after deletions); they are counted from the allocations.
- The request keys became script options without an allow list, and the
  kubeconfig of a created cluster could be downloaded with the read-only
  role: both closed.

### Tests
- Pure logic of the form and the pre-check against objects read on harv1
  (71 tests, among them the two early "ready" cases rebuilt from what was
  seen), the stack install with a simulated cluster (13), the command line
  (13: deletion, images, versions), the form in a browser (11), the
  translated views (6).
- Real, on harv1 (Harvester v1.9.0, Turtles, Cluster API core v1.13.3):
  providers installed from the Installation tab on a cleaned cluster,
  images pushed over SSH, stand-in created, patch applied. Two clusters
  created from the form (1 control plane, 1 worker, v1.34.11), followed in
  the page to the end, reached with the downloaded kubeconfig, then deleted
  from the list with nothing left behind (namespace, VMs, addresses):
  - SLES 15 SP7: both nodes Ready, but ingress-nginx stuck, the image having
    no repository (hence the new warning);
  - openSUSE Leap 15.6: everything running, and a volume claimed inside the
    new cluster provisioned by Harvester through its CSI driver, written,
    then released.
- Real, the CAPHV fixes meant for upstream: the patched provider image on
  harv1 without any of the three workarounds (contract label present, no
  stand-in Service, templates rendered at `v1alpha1`): a cluster reached
  Available with both machines provisioned, its cloud provider pointing at
  the VIP found on `rke2-traefik`. harv1 was then put back to the shipped
  configuration from the Installation tab, which is where the leftover
  label was found.

## [1.47.2] - 2026-09-25 - Small defects seen while testing 1.47

### Fixed
- **After a console restart, the details of an action finished in the
  previous hour were empty.** The runs reloaded from the database kept
  their events but not the event counter introduced in 1.46.0, so their
  stream replayed nothing until the hourly cleanup handed them back to the
  database path.
- **The end of an export names its archive after a restart too**: the
  action's result is kept in the database (a column added in place, older
  databases keep working).
- **Start could come back to life while a transfer ran**: a pre-check
  finishing after the launch (a field left just before) enabled it again.
  It stays greyed out until the action ends; after a failure or a
  cancellation a fresh pre-check makes it available again.
- **The export pre-check said it was reading "both clusters"**; an export
  reads one cluster, an import the archive and the target cluster. The
  Check button's tooltip says the same.
- **The dock's Cancel button and its confirmation were in English** in every
  language; the button has a tooltip saying what cancelling does. The
  action and cluster names in a dock card are escaped.
- **Cancelling an upload from the dock left the store window uploading**
  (seen for real): the server stopped and deleted the partial file, but the
  browser went on sending without noticing. The window now follows the
  upload's action from the start and stops with it.
- **The first letter typed after a notes toolbar button could be lost** on
  a busy machine: the button took the focus, which the editor only gets
  back at the next frame. The button no longer takes it.

### Tests
- A reloaded run replays its events and its result; the result survives in
  the database, an older database gains the column; a late pre-check keeps
  Start greyed out (the test fails without the fix), a failed transfer can
  be started again; the export pre-check text; the dock's Cancel in five
  languages; a cancel from the dock stops the upload in the window (the test
  fails with the previous window); the notes toolbar keeps the focus in the
  editor.
- Real, on the development console: a run finished just before a restart
  replays its steps and its archive name after it; a 3 GiB upload
  cancelled with the dock's Annuler button, confirmation in French, the
  window stopping at once and nothing kept.
- The loading-text test of the network view held a request the view no
  longer makes (`/api/topology` instead of `/api/network-fabric`): it passed
  only while the real answer was slow, which is why it failed in the full
  suite. Reproduced with an instant answer, then corrected.
- The dock toggle test clicked the Activity tab, in the side menu, and left
  the pointer there: a menu that had time to unfold (120 ms after hovering)
  covered the toggle. Reproduced by waiting, then corrected by moving the
  pointer away as a user does.

## [1.47.1] - 2026-09-25 - The package says what the console is

### Fixed
- **`systemctl status` and the image metadata presented a shutdown tool.**
  The systemd unit read "Harvester HCI graceful shutdown/startup UI" and the
  image label "Graceful shutdown/startup tooling": both now describe a
  console for a set of Harvester clusters, as the README does since 1.44.11.

### Tests
- The unit description and the image label name a console for clusters and
  no longer a shutdown tool.

## [1.47.0] - 2026-09-25 - Get an export back, and bring it to another console

Asked after a first real export: "how do I get the image back? how do I
import it again?". The window said the transfer was finished without naming
the file, and an archive could only reach another console's store by hand.

### Added
- **The end of an export names its archive**, with its size and three
  buttons: Download, Import into a cluster (opens the import form for that
  archive), Export store.
- **Add an archive to the store from the browser.** The file travels as the
  request body and is written to disk as it arrives, never staged in memory:
  a disk of tens of GiB goes through. Before it is accepted the archive is
  checked (complete, readable, every member matching its SHA-256 sum); a
  copy damaged on its way, even at the right size, is refused with the
  member at fault and nothing is kept. A name already in the store is
  refused, and a lack of room is reported before receiving anything. The
  upload is an action: throughput and time left in the window and the dock,
  then the checksum pass; Cancel stops it and deletes the partial file.

### Fixed
- **Cancel in the dock did nothing for actions without a process** (ISO
  download, bare-metal install): they watch a cancel flag that nothing set.
  They now stop, and read "cancelled" rather than "error".
- **Deleting an imported VM left its cloud-init secret behind**, user data
  included (seen on harv1): the secret a console copy or an import creates
  now belongs to the VM, as Harvester does for the VMs it creates.
- The export store window, opened a second time, wired its buttons twice.
- The test server read the real export store of the development console.

### Internal
- The console names the export archive itself and passes it to the script
  as `--out <file>`; an action carries a `result` (here the archive name),
  sent with its end event.

### Tests
- Upload: kept and private (0600), refused names, never over another
  archive, two uploads of one name, no room, a truncated file, a byte
  flipped in a disk at the right size, cancel, no path in an error, crash
  leftovers removed but not a live upload; the export names its archive;
  the dock cancels an action without a process, and a cancelled ISO
  download or bare-metal install reads "cancelled"; an imported VM owns its
  cloud-init secret. Browser tests of the
  archive block and of real uploads to the test server (accepted, damaged,
  not an archive, window opened twice).
- Real, on harv1 (Harvester 1.9.0): export of a stopped VM from the Migrate
  window, archive named and downloaded (identical to the store copy),
  uploaded back through the browser under another name (identical, checked
  in 1.7 s), imported into harv1 from the store with new MACs, then again
  from the command line; the secret now disappears with its VM. An upload
  and an ISO download cancelled from the dock keep nothing.

## [1.46.0] - 2026-09-25 - Follow a transfer, and make it faster

Asked for: during a transfer, see the throughput, the time left, what there
is to transfer and what is already done; go as fast as possible, with
options when that costs resources.

### Added
- **Live progress of a transfer**, in the Migrate window and the dock: the
  phase in progress (freezing the disks, download, import, backup, images,
  restore), amount done over total, bytes actually sent, throughput, time
  left, elapsed, and a summary of each finished phase kept in Activity.
  When every byte is sent but the target is still writing, it says so.
- **The pre-check announces the amount**: disks, size, space really used.
- **A Speed choice whose tooltip says what it costs**: economical (one disk
  at a time), normal (every disk at once, the default), maximum (also
  raises Longhorn's backup and restore threads from 2 to 8 for the transfer,
  put back at the end, even on failure). A **bandwidth cap** in MiB/s for
  what the console host sends. On the command line: `--speed`,
  `--parallel`, `--bandwidth`.

### Changed
- Disks are copied at the same time: measured on the test bench, a
  quarter less time for a two-disk VM. The sync nudge runs every 20 s.

### Fixed
- **Tooltips are never hidden** behind a floating window's title bar or cut
  by a scrolling container (reported with a screenshot): one layer above
  everything, placed below the element when there is no room above.
- **A long transfer rides out a cluster API that drops for a while**: waits
  retry, a broken stream is served or written again from its start, a
  rollback retries its deletions and names what it could not remove. A
  kubectl timeout no longer quotes the command line and its kubeconfig path.
- **The live stream of any action no longer freezes past 500 events**: it
  walked the event queue by position.
- **Start was lost when clicked right after editing a field**: the re-check
  greyed it out and moved it under the pointer.
- A console restarted halfway (cached page, new scripts) left the cluster
  and file tabs of the Migrate window blank; they now ask to reload.

### Tests
- The throughput, remaining time, throttling and gzip counting; each engine
  phase publishes its progress; the CDI probe is not counted twice; disks
  in parallel or one by one; Longhorn concurrency raised and put back, even
  on failure; transient API errors and broken streams; the event stream
  past 500 events; browser tests of the progress block, the dock line, the
  speed options, the tooltip layer and the blank-tab guard.
- Real measurements on the test bench, in the design document: economical
  and normal copies, backups at normal and maximum concurrency. The KubeVirt
  raw export was evaluated and set aside: Harvester does not enable it.

## [1.45.0] - 2026-09-25 - A VM moved to another cluster, exported, imported

A VM was bound to the cluster it was born on. It can now move to another
declared cluster, or leave as a file for a site the console cannot reach.

### Added
- **One Migrate window, three destinations**: another node of the same
  cluster (the live migration, unchanged), another cluster, or a file. The
  live migration panel it replaces is now translated.
- **A pre-check before anything changes**, shown in the interface language,
  blockers first, Start disabled while one remains: target reachable, name
  free, namespace present or to be created, every network and storage class
  mapped (pre-filled when the name exists), Longhorn room (more replicas
  than nodes only warns: the volumes run degraded), MAC addresses already
  taken on the target (Harvester refuses them even for a stopped VM), host
  devices and node pinning that cannot travel, an older Harvester on the
  target.
- **Two engines, chosen for you.** Harvester's own backup when both
  clusters share a backup target and the restore can succeed (networks keep
  their names, storage classes exist); it alone offers a **short stop**, a
  first backup while the VM runs and a second, incremental one after the
  stop. Otherwise a copy through the console: each disk frozen into a
  temporary image, streamed, and imported by CDI into an ordinary volume of
  the chosen class, without an image left behind on the target.
- **Final states chosen by the operator**: the source left running (a copy,
  with new MAC addresses), stopped and marked as moved, or deleted with its
  volumes once the target is verified; the target started or left stopped.
- **Nothing left half-done**: everything a transfer creates is labelled,
  and a failure or a cancellation from the dock removes it and puts the
  source back as it was.
- **An export store**: the Exports button of the VM view lists the archives
  (origin, date, size, complete or not), downloads one, imports it into a
  declared cluster, deletes it. An archive is a plain tar with SHA-256 sums
  checked during the import; it holds cloud-init secrets and is created
  0600.
- **`harvester-vm-transfer`** on the command line: `check`, `migrate`,
  `export`, `import`, the same engine and archive as the console, for
  isolated sites.

### Fixed
- **The bulk action bar of the VM view is readable**: the strategy menu
  wrote its options in white on the browser's white list, and the main
  button blended into the bar. It is now a neutral panel edged with the
  accent colour.

### Changed
- `install.sh` deploys every file of `bin/lib`, not only `common.sh`; the
  Longhorn allocatable space moves there to be shared by the console and the
  new command.
- The service keeps exports in `/var/lib/harvester-ops/exports`.

### Tests
- The decisions, the disk counter and both engines are tested without a
  cluster, the engines against two simulated clusters that behave like the
  real ones (backup sync, image restore, CDI's first connection, a restore
  that cannot be removed while its VM lives); every guard was sabotaged to
  see its test fail.
- The command with a fake `kubectl`, the console endpoints, and a browser
  test of the Migrate window and the store, including the race that left a
  previous cluster's mappings on screen.
- A browser test measures the contrast of the bulk action bar in both
  themes; it fails on the old style.
- Real runs on two nested Harvester 1.8.2 clusters and harv1 (1.9.0): a
  direct copy and an archive import across versions, disks compared bit
  for bit; the backup engine in stop and short mode, with an image restored
  on the target and the source deleted; a transfer started from the
  interface; a cancellation and two real failures, each undone. Each defect
  they found was first reproduced by a test.

## [1.44.13] - 2026-09-23 - The videos play on the GitHub page

### Changed
- **The demonstration videos play inside the README**, in English in
  `README.md` and in French in `README.fr.md`: the tour at the top of the
  page, the eight other clips under "Watch it work", each with its download
  links in both languages. GitHub shows a player only for videos uploaded
  through its web interface, under 10 MB on a free account; the clips were
  re-encoded to fit. The animated previews they replace are removed.

### Tests
- One more test on the READMEs: nine players per language, none shared,
  each address alone on its line, which is what makes GitHub show a player.

## [1.44.12] - 2026-09-23 - Loading texts name the cluster

Spotted on the project's GitHub page: the animated preview of the README
showed "Loading {name}'s topology...", placeholder included.

### Fixed
- **The Cluster, Network, Storage and Fabric views name the cluster while
  they load**, in every language. They asked for a text that expects the
  cluster name without ever giving it, and the helper they share could not
  pass values at all. The maintenance pre-check showed that same text about
  a whole cluster while loading one node's plan; it now says "Loading
  harvlab-n1...".
- The README preview no longer includes the moment a view is loading.

### Tests
- A scan of the interface code fails when a translated text that expects a
  value is displayed without it (unless it goes through `fill`,
  `.replace` or the loading veil); checked by sabotage.
- A browser test holds each view's data back and reads its loading text in
  English, French and German; it fails when the helper drops the values.

## [1.44.11] - 2026-09-23 - The README says what the console is for

### Changed
- **The README, in English and French, presents harvester-ops for what it
  is**: a modern console to run a set of Harvester clusters, resting on
  three things: every cluster in one interface, everyday operations
  automated, and every event on record, whether made from the console or
  elsewhere. The previous version presented it as shutdown tooling that had
  grown; the ordered shutdown and startup are now one of the automations,
  and the clips are listed in that order (tour, several clusters,
  activity first). The introduction of the capabilities guide follows.
- The tour video's captions follow too: the console is introduced for all
  your clusters, and the shutdown is no longer "the reason this toolkit
  exists". The video attached to the 1.44.10 release is replaced.

### Tests
- Five tests on the two READMEs: the same videos in both languages, one
  video per filmed scene and language, every image present, each README
  linking to the other, no typographic trace.

## [1.44.10] - 2026-09-23 - A real shutdown and startup of a multi-node cluster

Filming the shutdown and startup on the three-node test cluster found two
defects in the core sequence. Neither shows on a single-node cluster, which
is where it had been exercised until now.

### Fixed
- **Every node of the cluster now receives the shutdown order.** `ssh`
  reads standard input; called inside a `while read` loop over the node
  list, it swallowed the rest of that list. The loop ended after the first
  node and the step still reported success. Both loops that power nodes
  off were affected (control-plane and workers), on every cluster with
  more than one node. The helper now passes `-n`.
- **The startup no longer waits fifteen minutes for nodes that are already
  back.** It counted a node as Ready only when `kubectl get nodes` printed
  exactly "Ready"; the shutdown cordons every node, and a cordoned node
  prints "Ready,SchedulingDisabled". The uncordon comes after that wait, so
  the startup was waiting for a condition only it could fulfil, then went on
  after its timeout. Seen on the test cluster: three nodes back, "1/3
  Ready" for fifteen minutes, virtual machines still stopped. The scripts
  now read the node's Ready condition. The shutdown pre-check had the same
  defect the other way round: it called a node under maintenance "not
  Ready".

- **A detached volume no longer delays a migration in the pre-check.**
  Restoring a snapshot leaves the previous disk detached, with the VM still
  listed among its workloads; the maintenance pre-check announced that the
  VM's migration would wait for those abandoned disks. Only attached
  volumes count now.
- **The support bundle panel speaks the interface language.** "Bundle
  ready", "Download archive", "Loading..." and the rest were hard-coded in
  English, and the build steps existed only in English and French. Found
  while preparing the French demonstration of the Activity page.

### Changed
- **The README is rewritten**, in English with a French version
  (`README.fr.md`, also in the tarball) instead of interleaved captions. It
  opens on a tour video, lists nine demonstration clips in both languages,
  and every screenshot is new: the old ones showed the topology graph
  removed in 1.43.
- The Cluster API section of the capabilities guide no longer promises
  Kubernetes version upgrades, which do not exist, and says that cluster
  creation needs the `caphv-generate` tool on the host.

### Added
- **One shutdown or startup at a time per cluster.** While filming, a
  startup stuck on the defect above was still running when a new shutdown
  was launched on the same cluster: one was halting the virtual machines
  the other was waiting to restart. Two operators, or a double click,
  would do the same. The console now refuses with a clear message in the
  shutdown or startup log (naming the action already running), and the
  scripts take a lock next to their logs, so a sequence started from the
  command line and one started from the console see each other. Dry runs
  change nothing: they neither take nor wait for the lock.

### Tests
- Six tests on the console guard (another cluster, a dry run or a finished
  sequence do not block; the endpoint answers 409 naming the running
  action), three on the script lock, and a browser test that the refusal
  shows in the log in English and French.
- A test replays the shutdown loop with a fake `ssh` and fails when a node
  is missed, plus two guards on the helper and on the shutdown step.
- One test on a detached orphan volume in the pre-check, checked by
  sabotage and against the test cluster after two snapshot restores.
- Two tests that the support panel has no English left hard-coded and that
  each of its keys exists in the five languages.
- Three tests on the Ready count with a fake `kubectl` (cordoned nodes are
  Ready, a node whose condition is False or missing is not), plus a guard
  that no script reads the status column any more.
- Both checked by sabotage: putting the old code back turns them red.
- Checked on the test cluster: with two of three nodes cordoned, the new
  count gives 3 and the startup goes through in 61 s.
- The packaged service, checked through its unit's own commands (host
  paths moved to a throwaway directory, the account created by the real
  `install.sh` function, a kubeconfig left root-only): it served the
  console as `harvester-ops`, read the three-node test cluster, ran a
  dry-run shutdown, took the script lock in its log directory and refused
  a second one, kept its state on the volume, and stopped in 0.7 s.

## [1.44.9] - 2026-09-22 - A maintenance that works no longer reports a failure

Filming the demonstration videos on the three-node test cluster showed a
node entering maintenance while the actions dock displayed, in red,
"Harvester refused the maintenance". The maintenance was going through.

### Fixed
- **A drain still running is no longer read as a refusal.** Harvester
  clears its `drain-requested` mark before the drain is over, and the
  drain itself can take minutes: on the test cluster it retried for three
  minutes against the disruption budget of the Longhorn instance managers
  before the node reached maintenance. The console now follows the signal
  that actually separates the two cases: Harvester keeps the node cordoned
  while it works, and gives it back to the cluster when it gives up. A
  withdrawal is reported only once the node is schedulable again, and
  after a 30 second delay, since the mark and the status are written
  separately.
- The dock now says "drain in progress" during that phase, instead of
  showing nothing between the request and the end.
- **When Harvester withdraws a maintenance, the console says what held
  it.** Harvester gives no reason. On a three-node cluster it is always the
  same: the Longhorn instance manager of the node cannot be evicted, its
  disruption budget allows none. The error now names the pod and its
  budget, checked against the live cluster.
- **A volume is named once in the maintenance pre-check.** Longhorn keeps
  one workload entry per pod that mounted the volume, so after three
  migrations of the same VM the panel listed the same disk three times.

### Internal
- Nine demonstration clips, English and French, filmed on the three-node
  test cluster: tour, shutdown and startup, node maintenance, virtual
  machines, storage, network, snapshots, activity, several clusters.
  `tools/demo-video/screenshots.py` retakes the README screenshots.
- `tools/demo-video/`: the chain that produces the short demonstration
  videos. A scene plays the real console against a real cluster, and the
  edit speeds up the waits, burns in the captions and adds the cards and
  the music. Captions live apart from the scenes, so rewording or
  translating one costs a rebuild, not a new take. Not shipped to
  customers: `package.sh` copies only `bin web container config docs`.

### Tests
- 5 new unit tests on the reading of disruption budgets (a blocked pod is
  named, a budget that still allows a disruption is not a blocker, another
  namespace does not count, an unsupported selector is ignored rather than
  fatal), and one on the de-duplication of volumes.
- 4 new unit tests on the follow-up of a maintenance: a drain still
  running on a cordoned node, the gap before the status, a request that
  comes back, and a real withdrawal still reported.
- 16 tests on the video chain: cutting, speed-up, the reporting of marks
  into the edited film, subtitle generation, and the parity of the caption
  files between English and French.

## [1.44.8] - 2026-09-22 - PCI passthrough and SR-IOV, checked end to end

Since 1.15.0 the VM editor's passthrough picker was marked "not verified
end to end": no device could be handed over on a single-node production
cluster. One node of the three-node test cluster was given a virtual IOMMU
and two emulated cards to try it.

### Changed
- **The passthrough picker says where each device is and whether it can be
  used**: address, node and driver. Only a device claimed in Harvester
  (driver `vfio-pci`) can be used; picking another gave a VM that never
  starts, and the 33 devices of the test cluster were listed alike.
- The editor's passthrough hint no longer says "not verified", and says
  how to read the list. The description of the interface bindings adds that
  on Harvester, SR-IOV goes through a virtual function passed through as a
  PCI device; the KubeVirt `macvtap` and `sriov` bindings stay marked as
  not verified.

### Internal
- `tests/bench/harvlab/harvlab.sh pci` adds the virtual IOMMU, an e1000e
  card and an SR-IOV capable igb card to the test cluster's node 3.

### Tests
- 1 new unit test on the picker's label; the guard on unverified
  capabilities now expects passthrough to say it was verified.
- On harvlab: with the device claimed in Harvester, an e1000e card chosen
  in the console's editor reached the guest's PCI bus (`8086:10d3`), then
  an SR-IOV virtual function enabled through Harvester (`8086:10ca`). Not
  tried: a real GPU.

## [1.44.7] - 2026-09-22 - A maintenance that would never finish is said so

### Fixed
- **A volume used by a pod could hold a node's maintenance for ever**,
  unannounced. When an attached volume's only healthy replica is on the
  node, Longhorn refuses to evict its instance manager and the drain retries
  endlessly. Harvester's check, and so the console's, looked only at the
  volumes of VMs. The pre-check now lists such volumes with their claim and
  pods, and says what to do: add a replica elsewhere, or stop what uses
  them.

### Tests
- 2 new unit tests (pod volume held on the node, VM volume left to the VM
  rule) and the browser test of the section.
- On harvlab: a node's maintenance stayed "requested" for ten minutes on a
  one-replica volume used by a pod on another node; the pre-check named
  that volume and pod; once the pod was stopped, the maintenance completed.

## [1.44.6] - 2026-09-22 - The LLDP probe, against a real frame

Since 1.36.0 the LLDP probe carried a warning: never checked against a real
frame, since nothing on the test network emitted any. The three-node test
cluster's host bridge now does (lldpd on node2), and the first real frame
showed two defects.

### Fixed
- **Three fields out of five were lost.** tcpdump writes the value of the
  chassis ID, the port ID and the system description on the line AFTER the
  TLV; only the TLV line was read. They are now decoded, and the switch's
  management address is added (the IPv4 one first).
- **The answer never showed in the Fabric view.** The probe listens up to
  35 seconds; the view refreshes every 8 and redrew the card's detail,
  dropping "Listening..." after a second and writing the answer into an
  element no longer on the page. The probe's state is now kept by the view
  and shown at every redraw.
- The answer reads one line per field with a translated label (switch,
  port, management address, chassis, description) instead of raw
  `key=value` pairs, in the Fabric view and in a VM's network path.

### Tests
- The real frame captured on harvlab is a unit test fixture; the guard
  that required the code to say "not verified" now requires it to say
  where and when it was. Two browser tests: the display, and an answer
  that survives a refresh while listening.
- On harvlab: the probe on a node's physical card, from the Fabric view,
  showed every field after 11 seconds, through the view's refreshes.

## [1.44.5] - 2026-09-22 - The reset button resets

1.41.0 left one case unverified: a hard reset of a VM with two consoles
open. Run on the test cluster, it shut the VM down instead.

### Fixed
- **The console's hard reset shut Harvester VMs down.** It deleted the VM
  instance, believing it did what `virtctl restart` does; a deleted
  instance comes back only when the VM runs `Always`, and Harvester creates
  its VMs `RerunOnFailure`. It now goes through the VM's `restart`
  subresource with no grace period, which restarts whatever the run
  strategy (18 seconds to a new instance on the test cluster).
- **A reset that failed was reported as done**: when no new instance came
  up within 180 seconds, the action still ended in success. It now ends in
  error, saying the VM did not come back.
- **Both consoles said another client had taken the display** after the
  reset, and stopped. When the connection drops, the instance is still
  "Running" with the same identity, but its deletion has started: that is
  now read as a restart, and the consoles reattach once the VM is back.

### Tests
- 3 new tests on the reset action (subresource, failure when nothing comes
  back, a refused restart reported as is) and a new classification case;
  the two older restart tests now check the subresource.
- On harvlab, two browsers on the same VM (run strategy `RerunOnFailure`),
  reset from the first: the VM restarted with a new instance, and both
  consoles reattached on their own, "shared by 2 viewers".

## [1.44.4] - 2026-09-22 - A node that is down is not a missing node

The degraded-volume cases 1.42.0 could not exercise (a replica on a node
that is down, a failed replica, a volume with no healthy copy) were run on
the three-node test cluster by cutting a node's power.

### Fixed
- **While a node was down, the Storage view offered two harmful fixes**:
  "rebuild now", which deletes the replica Longhorn takes back when the node
  returns, and "lower to 2 replicas", which turns a passing outage into a
  permanent loss of redundancy. A replica on a node that is down, or back
  without its disk ready yet (a window of about a minute seen when the node
  returned), is now reported only as unavailable, with no one-click fix;
  lacking nodes or room is no longer claimed when the gap is a node that
  will come back. A node with no disk at all is not counted as coming back.
- A detached volume with a replica on such a node is shown at risk, with
  the reason, instead of "not enough nodes".

### Tests
- 6 new unit tests on down and returning nodes; each rule was checked by
  breaking it.
- On harvlab: node 3 powered off abruptly. A volume with its only replica
  there was shown faulted with no fix, its pod kept running, and it
  recovered on its own when the node came back; the other volumes were
  shown with an unavailable replica and no fix, then rebuilding, then
  healthy. A replica failed on a healthy node (its instance manager killed)
  was offered "rebuild now"; applied from the console it deleted the failed
  replica and Longhorn rebuilt a new one within twenty seconds. When
  Longhorn had already started reusing the failed replica, the server
  redid the diagnosis and refused the fix as no longer applicable.

## [1.44.3] - 2026-09-22 - Node maintenance, checked on three real nodes

Cordon, uncordon and maintenance mode shipped in 1.43.0 marked "not
verified on a real cluster". They have now been run on `harvlab`, a
three-node test cluster, and the run corrected what the pre-check announced.

### Fixed
- **The pre-check announced "will migrate" for VMs the drain shuts down.**
  KubeVirt live-migrates a VM on eviction only if its eviction strategy asks
  for it (`LiveMigrate`, `LiveMigrateIfPossible`, or the cluster default read
  from the KubeVirt resource). A VM without one is stopped by the drain: seen
  on harvlab, a VM announced as migrating was shut down. The pre-check now
  lists these VMs apart, says for each whether it comes back on another node
  (run strategy `Always`, a restart and not a migration) or stays stopped,
  and how to make it migrate.
- **VMs created from the console had no eviction strategy**, so they were
  shut down by any node maintenance. They now get `LiveMigrateIfPossible`,
  like those of the Harvester UI. Four VMs of harv1 created with kubectl or
  Terraform have none; the pre-check shows them.
- **Forcing was described wrongly**: Harvester stops only the non-migratable
  VMs, they stay stopped after the maintenance, and the "shut down during
  maintenance" label is honoured only without forcing. The pre-check and
  the documentation say so.
- **A VM whose volume is not healthy is flagged**: Longhorn cancels the
  migration of a volume while a replica waits to be rebuilt (seen on
  harvlab: the drain went round for a quarter of an hour until the rebuild
  went through).

### Changed
- The comment of the migration endpoint claimed a target node could be
  chosen; KubeVirt always chooses it. Choosing one is not implemented.

### Tests
- 6 new unit tests (eviction strategy, cluster default, `Always`, forcing,
  unhealthy volume) and a browser test of the new sections; the VM creation
  test checks the eviction strategy sent.
- On harvlab (Harvester v1.8.2, three control-plane nodes): cordon and
  uncordon; the last available node refused by the console and by
  Harvester's webhook alike; forced maintenance; maintenance without
  forcing where each VM ended as announced (`LiveMigrateIfPossible`:
  migrated, same instance; `Always` without strategy: new instance on
  another node; `RerunOnFailure` without strategy: stopped); the busy
  control plane refusal; leaving maintenance; the unhealthy volume flagged
  while degraded and no longer once healthy. The console's live migration
  and its volume diagnosis were exercised along the way.

## [1.44.2] - 2026-09-22 - A node can join an existing cluster

Found while building a three-node test cluster (`harvlab`, nested on
node2) whose install configurations are generated by the console's own
function, the one behind the Bare-metal tab.

### Fixed
- **A join configuration put `server_url` under `install:`**, where the
  installer ignores it: it is a top-level field (`HarvesterConfig.ServerURL`
  in harvester-installer). A node could not join a cluster. It is now at the
  top level, and the VIP, which belongs to the cluster being created, is no
  longer written for a joining node.
- `POST /api/baremetal/install` checks the mode: `create` needs a VIP,
  `join` needs `server_url` (`https://host[:port]`) and a cluster declared
  in the console for that address. A join ends when the new node is Ready in
  that cluster (step `wait-node`), not when an API that was already up
  answers.

### Internal
- `tests/bench/harvlab/`: a script and its README to build, stop, start and
  destroy the three-node test cluster on node2 (Vault for its secrets,
  configurations served from an unlisted path and withdrawn after install,
  direct kernel boot of the installer).

### Tests
- 11 new tests on join configurations, the endpoint and the join wait.
- On harvlab: node 1 created the cluster, nodes 2 and 3 joined it with
  configurations from this function and were promoted to control plane
  (three etcd members). Node 3 was given a plaintext password, as the
  Bare-metal tab sends it: the installer hashed it (the `/etc/shadow` hash
  recomputed with its salt matches), so that path was already right.
- Not run on real hardware: the Redfish-driven install in join mode.

## [1.44.1] - 2026-09-22 - The packaged service starts

Checking that 1.44.0 kept its watcher snapshot once installed, we ran the
systemd unit exactly as `install.sh` sets it up, with rootful podman and a
read-only root filesystem. **The service did not start**, and had not since
the first public release (1.42.0 reproduced it): the tarball releases were
checked by starting the application directly, never through its unit.

### Fixed
- **The application crashed at startup** creating `/var/lib/harvester-ops`
  on the read-only filesystem: only `PermissionError` was expected, not
  `Read-only file system`. The action history, the notes and the Terraform
  workspaces now fall back to a temporary directory on any such error,
  with a warning, instead of crashing.
- **The container could not read its own configuration.** It runs as a
  non-root account, and `install.sh` made the TLS key, the htpasswd file and
  the kubeconfigs readable by root only. `install.sh` now creates a
  `harvester-ops` system account; the container runs as that account,
  resolved on the host (the image's uid 1001 may belong to a real person
  there); `/etc/harvester-ops/` is readable by its group and by no other
  account. The unit reapplies this at each start, so a kubeconfig copied
  later by hand is covered by a restart.
- **Nothing persisted across restarts**: there was no writable volume, so
  the action history, the notes and the watcher snapshot of 1.44.0 lived in
  memory. `/var/lib/harvester-ops/` is now a persistent volume owned by the
  account, and its home: the kubectl cache, and the SSH `known_hosts`
  without which a node's host key was accepted anew at every connection,
  never checked.
- **Stopping took 10 seconds and ended in SIGKILL**: as the container's
  first process, the application ignored SIGTERM. It now exits cleanly
  (0.6 s measured).
- `uninstall.sh --purge` also removes `/var/lib/harvester-ops/` and the
  account. The install guide no longer tells to `chmod 600` the kubeconfigs,
  which locked the service out; a troubleshooting entry covers existing
  installs.

### Tests
- 17 tests on the unit, `install.sh`, `uninstall.sh`, the install guides,
  the read-only fallback and SIGTERM. The unit passes `systemd-analyze
  verify`.
- On node1, the unit's own commands (extracted from the file, host paths
  moved to a throwaway directory) with the account created by the real
  `install.sh` function, a kubeconfig left root-only as after a manual
  copy, then the real `--purge` block to clean up: the service started as
  `harvester-ops` over HTTPS, refused requests without credentials, read
  harv1, wrote its state to the volume; after a restart the history was
  still there and a namespace deleted in between was reported; SSH
  recorded the node's host key; the stop took 0.6 s.

## [1.44.0] - 2026-09-22 - Nothing lost while the console restarts

The console reports in the dock what changes on a cluster outside it: a
VM, a volume, a network or a namespace created, deleted, started or
stopped from the Harvester UI, kubectl or Rancher. Anything that changed
while the console itself was restarting was never reported: at startup,
the watcher took a first snapshot as its reference, and that snapshot
already contained the change. Seen on harv1 with a volume created just
before a restart.

### Fixed
- **The watcher's last snapshot is kept on disk**, next to the action
  history (`watch/` beside the actions database, or
  `HARVESTER_OPS_WATCH_STATE_DIR`). The first round after a start compares
  against it, so what was created, deleted, started or stopped in the
  meantime appears in the dock and the Activity tab, marked "changed while
  the console was not watching". The same holds for a cluster that was
  unreachable when the console started. An image upload still running at
  restart is followed again, with one action and not two.
- Only what is needed to compare is written (names and a few status
  fields, not the resource versions that change at every status update),
  and only when it changed: not every 15 seconds. The file is readable by
  the service account alone and replaced atomically. A damaged type in it
  is set aside whole, so that no object passes for created; an unwritable
  directory only means the watcher forgets across restarts, as before.

### Tests
- 15 unit tests on the kept snapshot: reported changes, first round
  without a snapshot, damaged or malformed file, no rewrite without
  change, uploads across a restart, file name confined to its directory,
  unwritable directory. Each guard was checked by breaking it.
- On harv1: the console stopped, a namespace and a volume created, the
  console started; both appeared in the dock at once, with the message
  saying so. The throwaway objects were then deleted, which the dock
  reported live.
- Five browser tests that had been failing for several releases are
  aligned with the current interface: the default theme is SUSE, the VM
  stop order has its own sub-tab, shutdown groups are ordered by their
  own priority (only the default group runs in parallel, under a locked
  name), and each view has its own inline sub-tab strip. The browser
  suite is fully green again.

## [1.43.0] - 2026-09-21 - A cluster view that says what each VM consumes

The Cluster view of the Overview was the last Cytoscape graph of the
console: 130 x 50 pixel boxes carrying a VM name and nothing else. And the
Cordon and Drain buttons of a node both failed with "not yet implemented".

### Added
- **The Cluster view in blocks**, like the Fabric, Network and Storage
  views: one block per host with its state (ready, cordoned, in
  maintenance), roles and address, and two gauges, vCPU and memory given to
  its running VMs against what the host can give, flagged past 100 %. Each
  VM is a card with its vCPU, memory, disks ("20 GiB" or "2 disks ·
  60 GiB"), networks and first address. Hovering a card or focusing it
  with the keyboard shows everything else: each disk with its size,
  storage class and boot order (an empty CD-ROM drive is said empty), each
  network card with its MAC and all its addresses, the guest OS, the
  interfaces only the guest knows, the run strategy. Stopped VMs are
  grouped apart; a filter narrows the cards by name, namespace, address,
  MAC, network or guest OS. A click opens the VM's actions, as before.
- **Cordon and uncordon that work**: `POST
  /api/node/<cluster>/<node>/cordon` and `/uncordon` set
  `spec.unschedulable`, as Harvester does, as tracked actions.
- **Harvester's maintenance mode**, asked for the way the Harvester UI does
  (the `harvesterhci.io/drain-requested` annotation, plus `drain-forced`
  to force). `GET .../maintenance-check` says beforehand what would happen,
  with Harvester's own rules read in its source: the single control plane
  and the busy control plane refusals, the VMs that will migrate, the ones
  that cannot and why (last healthy replica on this node, not
  live-migratable, no other node fits their placement rules), the ones
  marked to be shut down. The view shows this check before any
  confirmation; forcing sits behind the destructive lock. `POST` and
  `DELETE .../maintenance` enter and leave it, as tracked actions that
  follow Harvester's controller for up to ten minutes and report its
  refusals; leaving restarts the VMs the maintenance shut down. Entering
  and leaving need the `admin` role.
- The topology payload carries, per VM, vCPU, guest memory, disks with
  their size and class, network cards, guest OS; per host, allocatable CPU
  and memory, what running VMs take, the maintenance state, and whether it
  is the last available node. Disk sizes come in the same grouped kubectl
  call.

### Changed
- **The console no longer ships Cytoscape** (552 KB): `topology.js`, the
  vendored bundle and the canvas styles are gone, with the i18n keys only
  they used.

### Fixed
- **Cordon and Drain did nothing** but fail with "not yet implemented".
- **Harvester refuses to cordon the last available node**, which its
  admission webhook enforces (caught on harv1, where the first real cordon
  ended in `can't enable maintenance mode or cordon on the last available
  node`). The console now applies the rule beforehand: the button is
  disabled with the reason, the server answers 409, and the maintenance
  check says so.

### Internal
- `web/node_maintenance.py`: Harvester's maintenance and cordon rules as a
  pure module. `web/static/js/cluster-map.js` replaces `topology.js`;
  `app.js` mounts the four Overview views the same way.
- `Icons.dataUri`, used only by the graph, is removed.

### Tests
- 35 unit tests on the maintenance and cordon rules and endpoints, 15 on
  the topology data, 9 source-level guards on the view (actions, locks,
  confirmations, check before request, tooltips, Cytoscape gone), 18 in
  the browser. Each guard was checked by breaking it.
- On harv1: the view with real data in French and English, console and
  editor opened from a card, the webhook refusing to cordon the only node
  (now refused beforehand with a 409, the node untouched), the single
  control plane refusing maintenance.
- **Not verified on a real cluster**: actually cordoning, uncordoning,
  entering and leaving maintenance. harv1 has a single node, which
  Harvester will not cordon; they wait for a multi-node test cluster.

## [1.42.0] - 2026-09-21 - Why a volume is degraded, and what to do

A degraded Longhorn volume has fewer healthy replicas than it asks for: it
still works, but one more failure can make it unreachable. Nothing in the
console said so, let alone why.

### Added
- **A health banner in the Storage view**: how many volumes are faulted,
  degraded or at risk of starting degraded, the main cause, and a click
  that opens the most urgent one. Rows carry a coloured dot and a tag.
- **A diagnosis per volume**, from what Longhorn already reports and in
  the same grouped kubectl call (one type added, the engines): no healthy
  replica left, rebuilding switched off, rebuild in progress with its
  percentage, a new replica being prepared, a failed replica waiting to be
  reused, not enough nodes for the replica count, no disk with room, a
  replica on a node or disk that is down. Each cause says what is observed
  and what to do.
- **Three one-click fixes**, each with its equivalent kubectl command:
  lower the replica count to what the cluster can hold, switch rebuilding
  back on, rebuild a failed replica now. `POST
  /api/volume-health/<cluster>/<volume>/fix` reads the cluster again,
  redoes the diagnosis and applies only what it offers at that moment,
  with values it computes itself: nothing on a faulted volume, never below
  one replica, never deleting the last healthy copy, never switching
  rebuilding on during a cluster shutdown or startup. The tracked action
  watches for the effect for a minute and reports an error rather than a
  success when nothing changed.

### Fixed
- **A Longhorn volume whose claim was deleted was invisible** in the
  Storage view, which was built from the claims. On harv1,
  `rancher-monitoring-grafana` was missing. Such volumes are shown,
  tagged "claim deleted", and the banner counts exactly what the view
  shows.

### Internal
- `web/volume_health.py`: a pure diagnosis returning codes and facts; the
  browser writes the text, in five languages.

### Tests
- 31 unit tests on the diagnosis, built on structures read on harv1, 8
  new ones on the storage map, 19 on the fix endpoint and its guards, 10
  in the browser. Each guard was checked by breaking it.
- Exercised on harv1 with throwaway volumes, removed afterwards. A
  3-replica volume on the single node was diagnosed "not enough nodes",
  fixed from the view and became healthy. A 2.5 GB volume with rebuilding
  switched off was diagnosed "rebuilding switched off", switched back on
  from the view, and the view followed the rebuild to healthy. The live
  runs caught two mistakes the unit tests had not: a replica Longhorn
  cannot place has an empty node, and was taken for a replica on a node
  that is down; and a new replica spends about ten seconds unknown to the
  engine, shown as "cause not identified". Both are fixed and tested.
- Not exercised live (one node, no failure to cause safely): a replica on
  a node that is down, a failed replica, a faulted volume.

## [1.41.0] - 2026-09-21 - One console, several people

Reported as a console that "blinks" when several people open it. It did,
and measurably: two browsers on the same VM each connected and dropped 15
times in 25 seconds.

### Fixed
- **Two people on the same console kicked each other out in a loop.**
  KubeVirt accepts a single VNC connection per VM and closes the previous
  one when another arrives (checked against the cluster directly, without
  the relay: the first connection is closed with 1005 the moment the
  second opens). Each console reconnects by itself within 400 ms, so each
  took the display back from the other, every second. The console now
  holds ONE connection per VM and shares it between every browser.
- **The console ignored identity delegation.** Its websocket does not go
  through kubectl, so the `as`/`as-groups` carried by the delegated
  kubeconfig never reached it, and the console opened with the toolkit's
  own rights whatever the delegation said. The connection to KubeVirt now
  carries `Impersonate-User`/`Impersonate-Group`, and every person joining
  a console, including one someone else opened, is checked by the cluster
  under their own identity (`get virtualmachineinstances/vnc`).

### Added
- **A shared console.** Everyone on a VM sees the same screen and can type
  and click; the status line says how many people share it, and who when
  accounts are configured.
- **The console says when another client took the display** (the Harvester
  UI, another console instance) instead of taking it back in a loop, and
  offers **Take it back**. The other browsers of the session rejoin by
  themselves once someone has taken it back. A VM that restarted or
  stopped is still reattached automatically, as before: the server tells
  the cases apart by checking whether the same VM instance is still
  running.
- `GET /api/vm/<cluster>/<ns>/<name>/console-status`: who is watching, and
  why the last shared connection ended.

### Internal
- `web/vnc_mux.py` speaks RFB on both sides: it answers each browser's
  handshake locally (replaying the screen size), measures every server
  message so that a newcomer always joins on a message boundary, replays
  the cursor and the QEMU keyboard extension acknowledgement to newcomers,
  and requests a full frame when someone joins. The shared connection uses
  stateless encodings (Hextile, Raw): Tight and ZRLE keep a compression
  dictionary from the first frame that a newcomer never saw. The QEMU
  keyboard extension is kept, so an AZERTY keyboard still types right.
  The browsers' SetPixelFormat and SetEncodings are absorbed; a browser too
  slow to keep up is dropped rather than buffered without limit.

### Tests
- 35 unit tests for the shared console against a fake QEMU and fake
  browsers: every server and client message measured at every split,
  stateful encodings refused, one upstream for many browsers, input from
  all of them forwarded and settings from none, replay to newcomers, join
  on a message boundary, closing when the last viewer leaves, the loss
  reason recorded before browsers are told, one dial for simultaneous
  arrivals, a slow browser dropped.
- 13 application tests: the identity check at ticket time, the
  impersonation headers, the loss classification, the status endpoint,
  the session cap across VMs. 4 browser tests for the console states.
  Each test was checked to fail when the behaviour it guards is broken.
- Exercised on harv1: two browsers on rhel9-test for 25 s went from 15
  drops each to none, with one connection to KubeVirt; text typed in one
  appeared in both and reached the VM (then erased); the joiner uses the
  QEMU keyboard extension; an external connection made both consoles stop
  and say so, **Take it back** reclaimed the display and the other console
  rejoined by itself; an impersonated identity without rights was refused
  by the cluster (403) and accepted once given an admin group.
- Not exercised live: a hard reset of the VM with two consoles open (the
  reattach path is unchanged, and the classification is unit tested).

## [1.40.0] - 2026-09-21 - Network and Storage, read the same way

The Fabric view in its vSwitch layout was judged right, and the Network and
Storage views were asked to follow. Both leave the Cytoscape canvas for the
same page of blocks read left to right. Doing so turned up a real hazard in
the old Storage view.

### Fixed
- **A volume mounted by a pod was offered for deletion.** The old Storage
  view called "unattached" any claim no VM referenced, and the delete
  endpoint only refused a claim still named by a VM. On the test cluster
  that included the Prometheus and Alertmanager databases and three upgrade
  log archives mounted by running pods. Kubernetes does not delete a claim
  a pod uses, it only postpones it: the claim goes Terminating and vanishes
  with its data at the pod's next restart. The server now lists the pods of
  the namespace before deleting and refuses a claim any of them mounts; if
  it cannot list them, it refuses too.

### Changed
- **Network view: one block per network.** On the left, each VM with what
  it really has on that network: interface, MAC, addresses, interface name
  in the guest, link state, model and binding, running VMs first.
  Interfaces known only to the guest agent (docker0, internal bridges) are
  listed apart. On the right, where the network leaves: bridge, bond and
  cards; subnet, gateway and card for a kube-ovn underlay; nothing physical
  for the overlay and the pod network. Networks with no VM are listed at
  the bottom. Same data as Fabric, so no extra call to the cluster.
- **Storage view, read like a datastore.** One block per storage engine:
  storage classes on the left with their policy, the room they can still
  allocate and their volumes grouped by VM in boot order; the node disks on
  the right with a gauge of what is written and what is promised. A volume
  mounted by pods, and one claimed by nobody, are grouped apart; only the
  latter can be deleted, from its detail, behind the destructive lock and a
  confirmation. When the workload that last used it may come back (a
  StatefulSet), the detail says so first.
- **The Cluster view costs one call instead of eight.** It fetched volumes,
  replicas, attachable networks, backing images and images on every
  refresh without showing them; it now asks for nodes and VMs only, in one
  grouped call.

### Added
- `GET /api/storage-map/<cluster>`: classes, volumes with their consumers
  (VM from its spec, pods from Longhorn), replicas placed on their disk,
  node disks with their room, in one grouped kubectl call.
- The fabric payload carries each VM's live interfaces (MAC, addresses,
  guest interface, link state) from its VMI, in the same grouped call.

### Internal
- The room-left computation is one pure function shared by the VM creation
  panel and the Storage view, so the two can never show different figures.
- The grouped fetch with its per-type fallback is shared by the three
  views (`_grouped_items`).
- `board.js` holds what the Fabric, Network and Storage views share
  (escaping, copy buttons, tooltips, sizes).
- The Cytoscape network and storage code, its reducers and 17 translation
  keys are gone.

### Tests
- 21 API tests for the storage map (consumers, orphans, the pod mounted
  claim, replicas by disk UUID, room shared with the creation panel,
  settings and nodes not confused with their Harvester namesakes) and for
  the pod check before deleting, including when pods cannot be listed.
- 19 browser tests for the two views, each checked to fail when the
  behaviour it guards is broken.
- Exercised on harv1: a throwaway claim mounted by a pod was refused by the
  server (409) and offered no button; a throwaway orphan was deleted from
  the view as a tracked action and disappeared from the cluster; both test
  objects were then removed.

## [1.39.0] - 2026-09-21 - The fabric, read like a vSwitch

The stacked graph of 1.35 to 1.38 was found hard to read and awkward to
use, and rightly so: the pieces of one switch were scattered over the
whole canvas and the edges ran diagonally. The Fabric view now copies the
ESXi "Standard Switch" layout that operators already know.

### Changed
- **One block per switch, read left to right**: networks and their VMs,
  the switch, then the physical adapters. A cluster network is a switch
  named after its bridge, with the VlanConfig policy (bond mode, MTU) in
  its header and the bond holding its cards as uplink. A kube-ovn provider
  network is a switch whose port groups are its underlay subnets (VLAN,
  CIDR, gateway, VPC, and the attachable network a VM joins them through).
- **The OVN overlay is an internal switch**: dotted, with no adapter. That
  is what it is, and it is the point the old graph got wrong.
- The view is HTML, not a canvas: text can be selected, copy buttons are
  native, and it folds to one column on a narrow screen.
- The LLDP probe moved to the adapter detail, where ESXi keeps its CDP and
  LLDP information, instead of an "unknown switch" box under every card.

### Added
- **The VMs on each network**, running ones first, the rest unfolding on
  demand (and staying unfolded across the 8 second refresh). VMs on the
  pod network are listed apart: that network has no bridge.
- **Speed and duplex on every card** ("1000 Full"), read from the node
  once per node and per session. The card detail adds MTU, carrier
  changes, traffic and error counters, and bond mode with miimon on a bond.
- Cards attached to no switch are still shown, and an overlay network
  bound to no subnet is flagged.
- Tooltips on the two probes of the VM connection path, which had none.

### Fixed
- **A cluster without kube-ovn showed "cluster unreachable".** `kubectl
  get a,b,c` fails as a whole when one type is missing, and a Harvester
  without the add-on has none of its CRDs. The fabric now falls back type
  by type, and remembers the missing ones for ten minutes so that the
  following refreshes stay a single call.

### Internal
- The Cytoscape fabric code (builder, layout, detail panel, notice) is
  gone from `topology.js`, with its 21 translation keys; the orphan
  baseline drops from 105 to 103.

### Tests
- 11 API tests: VM attachments (bare network names resolved in the VM
  namespace, pod network flagged), the per-type fallback, its memory, its
  expiry, and a dead cluster still reported as such.
- The 21 browser tests of the fabric view are rewritten for the new
  layout: reading order measured on screen, bond then card as uplink,
  internal overlay, untagged VLAN 0, VMs under their network and the
  unfolding that survives a refresh, one node read per session, a copy
  click that does not select the card, and a tooltip on every control.
  Each one was checked to fail when the behaviour it guards is broken.

## [1.38.0] - 2026-09-21 - Named bands, and a switch to end the chain

Two ideas taken from vSphere network diagrams, which an operator offered as
a reference: every band carries its name, and the physical switch closes the
picture at the bottom.

### Added
- **Each band says what it is** (physical interfaces, aggregation, virtual
  switches, networks and routing, attachable networks, workload ports).
  Without a label the level has to be inferred from the boxes it holds,
  which a hurried operator will not do.
- **The chain ends at a switch, even an unknown one.** The operator's
  question does not stop at the copper ("which port?"), so the switch is
  drawn for every card that has a carrier, marked unknown until LLDP
  answers. It also gives the LLDP probe somewhere to land. A card with no
  carrier gets none: drawing one would suggest it is plugged in.

### Fixed
- **An empty icon produced `background-image: ` and killed the whole
  view.** The base node style maps that property to `data(icon)`, and a
  label node carrying no icon made the style parser fail, so nothing
  rendered at all, with an error from inside Cytoscape rather than from our
  code. A test now fails on any invalid style warning.
- Band labels are anchored by a POINT clear of the first column, not by a
  box: a full-size anchor overlapped the first column in geometry without
  anything showing, which is how the overlap test caught it.
- Seven band keys were invisible to the i18n parity scanner because they
  went through a local `t()` alias it does not recognise. The same trap has
  now cost four times in this project; they are written as `i18n.t('...')`.
- `vm.edit.netPathCopy` removed: `copy.title` replaced it.

### Tests
- 21 tests on the fabric view, including one that fails if any two boxes
  overlap, one that checks the band labels stay out of the columns, and one
  that fails on a Cytoscape style warning.

## [1.37.0] - 2026-09-21 - The kube-ovn side, audited

An operator doubted that column and asked for a deep audit, with good
reason: three things were wrong, and one of them was the worst misreading
this view can make.

### Fixed
- **A subnet on the overlay was drawn as if it reached the wire.** A subnet
  whose provider is `ovn` is encapsulated over the node network and never
  leaves through a physical uplink. Showing it beside an underlay subnet
  suggested the opposite. Overlay subnets are now told apart.
- **The VLAN that links an underlay subnet to its uplink was not even
  queried.** Without it the chain jumped from the subnet to the card with
  nothing to explain it. A subnet now reaches the wire through its VLAN,
  which names the provider network.
- **The four kube-ovn objects sat on one row.** A subnet belongs to a VPC
  and goes through a VLAN that names a provider network: they are a
  hierarchy, and flattening them erased it. They now stack inside the band,
  from the closest to the copper to the most abstract.
- **A cluster network hung off the physical card**, an edge that skipped the
  bridge and the bond, crossed the whole diagram and suggested a second
  parallel path. It now hangs off its bridge, which a network attachment
  names in data rather than being guessed from the `<name>-br` convention.
- **Boxes overlapped** once a band held four ranks: the boxes are 44 px tall
  and the step between ranks was 26. Band heights now follow the number of
  ranks they hold.
- Provider network readiness is read from `status.ready`, which exists
  directly; reading only the conditions happened to work on one cluster.

### Added
- **Copy buttons on values worth copying** (addresses, MACs, resource
  names), in the connection path and in every topology detail panel. An
  address is meant to be pasted into a ping, a ticket or a switch table;
  retyping it from a screen is the surest way to be off by one character.
  The button is an overlay and its gutter is reserved only on rows that
  have one, so nothing shifts and nothing is covered.
- A declared edge (a cluster network realised by a bridge, a provider
  network bound to a card) is drawn dashed: it is not an observed
  attachment, and reading it as a traffic path has already caused
  confusion.

### Tests
- 28 tests on the fabric model and 11 in a real browser, including one that
  fails if any two boxes overlap and one that checks the four kube-ovn
  levels stack in the right order.
- `web/static/js/copy.js` is shared rather than duplicated: the copy
  affordance is wanted in many places.

## [1.36.0] - 2026-09-21 - Is this VM on the right network, through the right card?

That question has no answer in any form. It is a chain, from the VM down to
the copper, and the VM editor now draws it: a Connection path tab beside the
interface editor, in the Network section.

    VM -> vNIC -> attachable network -> bridge -> bond -> physical card

Each link carries what is known of it, and what is DECLARED is kept apart
from what is RUNNING, because a gap between the two is exactly what the
operator is looking for.

### Added
- **A Connection path tab** in the VM editor's Network section. Read from
  left to right, with the address, the guest interface name and the link
  state on the vNIC, then the type and state of every hop down to the card.
  A dead link or an unreported hop stands out.
- **"Which host port?"**, which resolves the exact veth carrying this VM by
  matching its MAC inside the pod network namespaces on the node. It is an
  SSH round trip, so it runs only when asked.
- **"Identify the switch (LLDP)"**, which listens briefly on the physical
  card for a switch advertisement and reports the system name, port id and
  port description.
- **`GET /api/vm-network-path/<cluster>/<ns>/<name>`**, plus `/hostport`
  and the node LLDP probe.
- The node detail now also reports **negotiated speed, duplex and carrier
  changes**, read from /sys. The Kubernetes API knows none of them, and a
  link that flaps is a link about to go down.

### Internal
- **`podInterfaceName` from the VMI is not a host port.** It names an
  interface inside the pod's network namespace, and on the test cluster the
  two running VMs carried the SAME name there, so it discriminates nothing.
  The chain starts from the bridge the attachable network names.
- **From the VM you go DOWN towards the uplink**, not up towards masters: a
  bridge has none, so walking up stopped immediately.
- **Workload ports are kept off the path.** A veth hanging from the same
  bridge is another VM's port; following it would lead to the neighbour
  instead of the outside world.
- **A stopped VM still has a declared path.** Without a VMI there is no
  node, every hop became "not reported" as though the cluster were broken.
  The declared path is shown instead, labelled as such. On a multi-node
  cluster nothing says where it will start, so no path is guessed.

### Known limitation
- **The LLDP probe is NOT verified against a real frame.** Nothing on the
  test network emits LLDP (40 seconds of listening, zero frames; the switch
  there is an unmanaged model that never does). The decoding follows the
  standard and tcpdump's documented output, but it has not been confronted
  with a live advertisement. It is written in the code so nobody has to
  rediscover it.

### Tests
- `tests/api/test_vm_network_path.py`, 18 tests: the chain walking down,
  workload ports kept out, unreported hops flagged, the stopped-VM
  fallback, a drifted MAC reported, and the shell arguments refused when
  malformed.
- `tests/e2e/test_vm_network_path_view.py`, 6 tests in a real browser.
- Each was checked against a deliberately broken implementation. One
  sabotage showed a test passing for the wrong reason (a sort was saving
  it, not the filter it claimed to check), and it was tightened.

## [1.35.0] - 2026-09-21 - The network seen from the floor up

The existing Network view looks at the network from above: which VMs sit on
which network. This one looks at it the way an operator does, from below:
which physical card carries what, and through which stack.

A Fabric sub-tab in Overview stacks the whole thing, bottom to top, in two
columns because two fabrics coexist above the cards and do not mix.

    5  workload ports (folded)
    4  attachable networks
    3  ClusterNetwork          |  ProviderNetwork / VPC / Subnet
    2  virtual switch          |  Open vSwitch
    1  bond
    0  physical interfaces

### Added
- **A Fabric view** that draws that stack, with a dead link or an
  unresolved attachment standing out by colour: that is what the operator
  opens this view to find.
- **Detail on a card**, from the cluster (type, state, MAC, master, fabric)
  and, on demand, from the node itself over SSH: MTU, bond mode and
  traffic counters. It is fetched only when asked, never on every render,
  or the view would cost an SSH round trip every eight seconds.
- **`GET /api/network-fabric/<cluster>`** assembles the whole declarative
  picture in ONE grouped kubectl call (eight resource types), as the
  1.33.0 audit requires.

### Internal
- **The bond is a floor of its own.** That is where the VlanConfig lives
  (aggregation mode, MTU), so it is where uplink redundancy is configured.
  Folding it into the switch would hide the only layer anyone actually
  sets.
- **A link's fabric is read from its chain of masters, not its name.** A
  veth called `5a9ba3611271_h` says nothing about itself, but it hangs off
  `ovs-system`. Classified by name, 81 of the 83 veth pairs on the test
  cluster landed on the wrong side, and so did a physical card.
- **Workload ports are folded** into one box per fabric with their count.
  83 veth pairs on a single node drowned the switch floor.
- The same link is reported by every monitor whose rule catches it, so the
  key is (node, index).

### Known limitation, and what is offered about it
- **Harvester does not publish its Open vSwitch bridges**: its two link
  monitors only cover `mgmt(-br|-bo)` and cards, so the kube-ovn side stops
  at the first unreported master. That is shown as such rather than
  guessed. The view OFFERS to install a permissive `LinkMonitor`, which the
  CRD documents as matching everything with an empty rule, and which only
  reads. It is never installed on its own, it goes through a tracked
  action, and it can be removed from the same banner. On the test cluster
  it takes the reported links from 5 to 94.

### Tests
- `tests/api/test_network_fabric.py`, 21 tests: the floors, the two
  fabrics, deduplication across monitors, unresolved masters being flagged
  rather than invented, and the node detail parser.
- `tests/e2e/test_fabric_view.py`, 11 tests in a real browser: the stack
  drawn bottom-up, the bond on its own floor, the two columns, folded
  ports, detail fetched only on demand, and the banner.
- Each was checked against a deliberately broken implementation.

## [1.34.0] - 2026-09-20 - A new disk is one you boot from

The VM creation panel opened with no size field, and the field looked
forgotten. It was not: a disk was born as "attach an existing volume", the
one mode where size and storage class come from the volume already created,
so showing them would be a lie. That mode is also by far the rarest thing
to want when creating a machine.

### Fixed
- **A new disk boots from an image**, and the next ones are blank data
  disks. Neither falls back to attaching an existing volume, so the size
  and the storage class are there from the start.
- **Every number on the room-left line carries its unit.** It read
  "1107 GiB allocatable ... 10 requested here": the unit was missing on the
  requested amount, and on it alone, leaving the reader to guess whether
  that 10 meant gibibytes, mebibytes or disks.
- **The unit follows the language.** "Gio" was hardcoded, so the English
  interface displayed a French abbreviation.
- **The room-left line names what is actually missing.** With a source and
  a size both filled in, it still asked for "a source and a size"; what was
  missing was the image.

### Changed
- **An image disk shows its storage class, locked.** It used to disappear,
  which left "why can I not choose it?" unanswered on screen. It is the
  image's own class, and it carries the backing image: verified on a live
  cluster, an image class holds `backingImage` where a generic one does
  not, so picking another would produce an empty disk. Before an image is
  chosen the field says the class follows the image, rather than sitting
  greyed out and empty as though it were broken.

### Tests
- `tests/e2e/test_vm_create_panel.py`, 11 new tests in a real browser: the
  default source of the first and second disk, the size field appearing,
  attaching an existing volume still hiding it, the storage class editable
  on a blank disk and locked on an image one, the unit on every number, the
  unit following the language, and the hint naming the missing image.
- Each was checked against a deliberately broken implementation.
- Two existing tests needed updating. One of them caught its own blind
  spot: its guard reported that it had stopped analysing one item title out
  of eleven because a comment had outgrown its regexp window.

## [1.33.0] - 2026-09-20 - Stop asking the cluster the same thing

An audit of every kubectl invocation, measured with a shim that logs each
real call rather than trusting the code. Starting point: one operator with
the overview open triggered 54 kubectl invocations per minute, on an idle
screen. Each invocation pays a full process start before it touches the
network, so the NUMBER of calls matters as much as what they bring back.

### Changed
- **The cluster watcher makes one call instead of five.** It polls five
  resource types; `kubectl get a,b,c,d,e` returns them all in one go, each
  object carrying its own kind. Falls back to one call per type when the
  cluster does not expose one of them, because a grouped get fails as a
  whole and losing the watch entirely would be worse than being slow.
- **The status script makes two calls instead of five**, grouped by scope
  and run concurrently. Byte-for-byte identical output, checked against the
  previous version on a live cluster.
- **A hidden browser tab stops polling the cluster.** The overview kept
  querying every 8 seconds for a page nobody was looking at. Coming back to
  the tab refreshes immediately, so the saving costs no staleness.
- **The watcher slows down when nobody uses the console** (after 5 minutes
  without a request, configurable). Monitoring scrapes of `/metrics` and
  `/healthz` do not count as a human, otherwise the console would never be
  considered idle.

### Fixed
- **The Cluster API diagnostic no longer lists CRDs.** It asked for every
  CustomResourceDefinition to answer a yes/no question. The API returns the
  whole objects, OpenAPI schemas included: 37.9 MB on the test cluster,
  whatever output format is requested, since the format only changes
  client-side rendering. `api-resources` answers the same question with
  about 27 times less work.

### Internal
- Measured: 54 kubectl invocations per minute down to 19 for one operator
  on the overview, for the same information.
- The kubectl call metric only ever counted calls made through one helper,
  while most go through `subprocess` directly. The audit used an external
  shim instead, which also revealed that a call killed by a timeout leaves
  no trace at all in a logger placed after the process returns.

### Tests
- `tests/api/test_kubectl_economy.py`, 14 tests: the grouped collector and
  its per-kind fallback, both reduction paths agreeing, the watch loop
  actually using the collector, idle detection, monitoring scrapes not
  counting, the hidden tab guard, and the status script staying grouped.
- Each was checked against a deliberately broken implementation. One
  sabotage showed a test passing while the watch loop had been degrouped,
  which is how the loop itself ended up covered.

### New settings
- `HARVESTER_OPS_WATCH_IDLE_AFTER` (default 300 s) and
  `HARVESTER_OPS_WATCH_IDLE_INTERVAL` (default 120 s).

## [1.32.0] - 2026-09-18 - The identity the cluster sees

Third and last of the three steps agreed on rights, and the one that makes
the other two mean something.

Measured on harv1 rather than assumed: the console acted with a kubeconfig
worth `system:admin`, group `system:masters`. That group is a hardcoded
superuser in the apiserver, so it bypasses RBAC entirely. Whatever roles the
console enforced on its own side, the cluster applied no rule of its own to
anything the console did.

kubectl can carry an impersonation inside the kubeconfig itself (`as`,
`as-groups`). The console now builds a copy carrying the caller's identity
and substitutes it at the single point where the kubeconfig path is
resolved, so the fifty-odd kubectl call sites inherit it untouched, and the
`bin/*.sh` scripts do the same through `HARVESTER_OPS_AS`. Verified live on
harv1: under impersonation `kubectl auth can-i list virtualmachines` answers
`no` where the shared kubeconfig answers `yes`.

### Added
- **A cluster identity per console account**, declared in `roles.yaml`.
  The short form (`login: role`) keeps working; the long form adds
  `cluster_user:` and optional `cluster_groups:`.
- **`identity.delegate`** turns it on. It stays off on upgrade, so an
  existing install behaves exactly as before until someone asks for it.
- **An account with no cluster identity is refused** once delegation is on,
  rather than quietly falling back to the shared administrator kubeconfig,
  which is what delegation exists to remove. Relaxable with
  `identity.deny_unmapped: false`.
- **CLI parity**: `HARVESTER_OPS_AS` and `HARVESTER_OPS_AS_GROUPS` make the
  scripts present the same identity, in one place in `bin/lib/common.sh`.
- **The role badge says what the CLUSTER sees**, which is not the console
  role: delegated, not delegated, or delegated with no identity.
- Each action records the identity it ran under, so the history says who the
  cluster saw, not only who clicked.

### Fixed
- **A failed kubectl no longer leaks the kubeconfig path.**
  `CalledProcessError.__str__` copies the whole argv, and it was being put
  straight into the HTTP response. Found by testing delegation for real: a
  reduced-rights account brought the file path back in the response body.
  Four endpoints were affected; this predates delegation.
- **A cluster refusal is a 403, not a 500.** The VM listing discarded
  stderr, so an RBAC denial surfaced as an opaque server error. It now
  carries the cluster's own reason and the identity that was refused.
  Nobody could hit this before delegation, precisely because the shared
  kubeconfig bypasses RBAC.

### Internal
- The impersonated copies live in a private directory (0700) with each file
  at 0600: they carry the cluster credentials. They are cached per source
  kubeconfig, identity and source mtime, so a certificate rotation produces
  a fresh copy instead of pinning the old one.
- The identity is frozen when an action is triggered, in the request thread.
  A worker thread has no request context; re-reading it there yields none,
  and the action would have gone back to the administrator kubeconfig, which
  is exactly where the heaviest gestures live.

### Tests
- `tests/api/test_identity_delegation.py`, 32 tests: configuration format
  and backward compatibility, when delegation applies, file permissions,
  copy reuse and invalidation, refusal of unmapped accounts, CLI parity,
  the worker-thread trap, the path leak and the 403.
- Each was checked against a deliberately broken implementation, so they
  fail for the right reason.
- Suite: 710 green.

### Known limitation
- The console still HOLDS the administrator kubeconfig. This is a boundary
  the cluster enforces, not a vault: a flaw in this layer would hand back
  full powers. Sourcing the identity from an OIDC provider is the next step,
  and needs the identity provider to be running.

## [1.31.0] - 2026-09-18 - The cluster's own accounts

Second of the three steps agreed on rights: seeing and changing who holds
administration of a Harvester cluster, without leaving the console.

Harvester's model was read from the cluster rather than assumed. A
`users.management.cattle.io` object carries the login and whether the
account is enabled; administration is a plain ClusterRoleBinding to
`cluster-admin`; and the password lives somewhere else entirely.

### Added
- **A Cluster accounts tab** in Settings, listing the accounts of the
  selected cluster with, in one view, whether each is enabled and whether it
  holds administration. The Harvester UI keeps those two apart.
- **Enable, disable, grant or revoke administration**, with a confirmation
  before handing someone the whole cluster.
- **Orphaned administration is surfaced.** A `User` subject holding
  cluster-admin with no user object behind it means the account was deleted
  and its delegation was not. Recreating an account with that id would
  silently give it back. harv1 had three.
- Groups holding administration are listed as such, and the seventeen
  service accounts that hold it are counted rather than listed, so the
  useful information is not drowned.

### Internal
- Revoking only removes bindings this console created. Deleting the one
  Harvester writes at installation would break the original account, and
  putting it back is not obvious; that case returns 409 and says to do it
  deliberately with kubectl.
- Listing the accounts requires the `admin` role, reading included: who
  holds administration of a cluster is not something a read-only account
  needs.
- The two kubectl calls run in parallel. Chained, the panel took 5.5 s on
  harv1.

### Not offered, and why
Creating a local account with a password. Harvester stores it as a 32-byte
derived key with its own 32-byte salt, in a separate secret, not as a bcrypt
hash. A probe account created with bcrypt was refused at login, and so was
the `$2a$` variant. Guessing the scheme would produce accounts that cannot
log in at best. The panel says so and points at the Harvester UI. The probe
account was deleted; the cluster is back to its two original users.

### Tests
- The model itself: administration read from the binding and not the user, a
  missing `enabled` meaning active, bindings for other roles ignored,
  orphans told apart from groups, service accounts counted.
- The guard on revoking, which must not touch a binding it did not create.
- **The harness stopped lying under load.** The fixture gave the server 8 s
  to start and each request 10 s. On a host at load average 27, importing
  the app alone takes 14 s, so every single test failed with "Flask did not
  start": 117 errors that read as a catastrophic regression and were a busy
  machine. Both budgets are now generous and overridable, and the failure
  message says how long it actually waited.
- 678 API tests green.

## [1.30.0] - 2026-09-18 - Roles, because a password was the only gate

The console authenticated a password and nothing more. Any account that got
in could shut down a cluster, delete a VM or destroy a Terraform workspace.
There was no notion of user and no notion of role.

### Added
- **Three roles** declared in `/etc/harvester-ops/roles.yaml`: `viewer`
  reads, `operator` does the everyday mutating work, `admin` adds what cuts
  a service or changes the tool's own configuration (cluster power
  sequencing, cluster declarations, bare-metal, the ISO store, the Terraform
  provider).
- **A central gate, deny by default.** Any request that changes something
  needs at least `operator`; an explicit list of paths needs `admin`. An
  endpoint added tomorrow is protected without anyone having to remember it.
  That shape is deliberate: the rate limits were applied decorator by
  decorator and six of them silently protected nothing for several releases.
- **A refusal that explains itself**, naming the role required and the one
  you hold, on the wire and on screen.
- `GET /api/whoami`, and a badge in the sidebar footer showing who you are
  and what you may do. It stays hidden until roles are actually in force, so
  an installation without them is not told it is running as "admin" by
  design.
- `install.sh` writes a default `roles.yaml` where everyone is a viewer
  until listed.

### Internal
- **Roles need identities.** With no htpasswd nobody can be told apart. The
  first cut restricted on an empty identity and put everyone, including the
  operator, in read-only; the dev server locked itself out within a minute.
  Now the absence of authentication disables the gate and `/api/whoami` says
  so.
- A malformed or missing roles file, an unknown role name and an invalid
  default all fall back safely rather than locking an installation out or
  granting rights by accident.

### Tests
- The classification of every kind of path, and the deny-by-default for one
  invented on the spot.
- Enforcement per role, end to end, including a viewer refused and an
  operator stopped at the cluster power switch.
- A walk over the whole route table proving no mutating endpoint escapes the
  gate.
- 662 API tests green.

### What this is not
The console reaches clusters with one shared kubeconfig that is
cluster-admin, so the cluster sees a single identity whoever is at the
keyboard. These roles are a guardrail inside the console, not a boundary the
cluster's RBAC enforces. Managing Harvester's own users from the console,
then delegating identity to OIDC and acting with the user's token, are the
agreed next steps.

## [1.29.1] - 2026-09-17 - A template's PVC is a recipe, not a volume

1.29.0 shipped this as a wrinkle in Harvester's base templates. It was not:
the templates are correct, the mapper was wrong.

A template declares its disk in the `volumeClaimTemplates` annotation
(pvc-rootdisk, 10Gi, empty imageId) and references it by claimName. That PVC
does not exist anywhere: it is a recipe to be created, with the image left
for the operator to pick. The disk mapper ignored the annotation entirely
and reported every volume with a claim as an existing PVC to select, so
loading a template showed a phantom.

### Fixed
- A claim declared in `volumeClaimTemplates` is now presented for what it
  is: an image or a blank disk, carrying the size and storage class the
  recipe declares. Loading the raw-image base template now shows a blank
  10 GiB disk instead of a PVC that cannot be found.

### Internal
- The rewrite is opt-in and the editor never asks for it. On a VM in
  service the PVC really exists, and showing its disk as "to create from an
  image" would have generated a fresh PVC on the next save, abandoning the
  old one and its contents. Checked on a live VM: its disk still maps to its
  real PVC.

### Tests
- The distinction itself, including that the editor never passes the option,
  which is the guard against that data loss.
- 633 API tests green.

## [1.29.0] - 2026-09-17 - Room, size and templates when creating a VM

Three additions to the creation panel, all answering the same question
differently: what will the cluster actually accept?

### Added
- **The room left per storage class, under the disk editor.** Not the
  "available" figure Longhorn shows: its scheduler applies two constraints at
  once and the tighter one decides. On harv1, over-provisioning leaves
  2592 GiB while real free space leaves 1107 GiB, so showing 1968 GiB would
  promise almost double what the cluster will take. The panel says which
  constraint is binding, and subtracts what the other disks of the same VM
  already request on the same class, so three disks of 600 GiB no longer all
  look like they fit. A class needing more replicas than there are
  schedulable nodes reports no room at all, which on a single-node cluster is
  the honest answer and saves an incomprehensible scheduling failure.
- **The disk size is proposed from the image**, rounded up from its virtual
  size, because a disk smaller than that is refused. A size typed by the
  operator is never overwritten.
- **Start from a template.** The panel lists the cluster's Harvester
  templates and loads the chosen one as a starting point; everything stays
  editable afterwards. The VM goes to the namespace picked in the form, not
  the template's own.
- `GET /api/storage-capacity/<cluster>` and
  `GET /api/vmtemplates/<cluster>/<namespace>/<name>`.

### Tests
- The capacity maths against harv1's real figures, both constraints in both
  directions, a class needing more nodes than exist, replicas taking the
  smallest of the nodes they need rather than the largest, and an
  unschedulable disk offering nothing.
- The subtraction between disks of one class, and that a typed size survives.
- 630 API tests green.

## [1.28.0] - 2026-09-17 - Creating virtual machines

The console could edit everything about a VM but could not create one: that
meant going to the Harvester UI or writing a Terraform declaration.

### Added
- **A Create button on the VM tab** opening an overlay that carries every
  setting a VM has. It does not offer a reduced form: it replays the eight
  sections of the editor on a skeleton VM instead of a fetched one, so
  anything you can edit you can set at creation, and it will stay that way
  the day a section gains a field. No form is duplicated.
- **Several at once**, with a count. Above one the names are numbered
  (web-01, web-02), each instance gets its own PVCs, and the guest hostname
  follows unless it was set by hand.
- **Start once created**, on by default and unticked to prepare a VM before
  running it.
- **Validate only**, which asks the cluster itself to check the manifest
  through a server-side dry run without creating anything.
- **Save as template**, writing the Harvester pair a template really is: a
  VirtualMachineTemplate and a VirtualMachineTemplateVersion pointing at it.
  Writing only the version leaves an object the Harvester UI never shows.
- `GET /api/vmtemplates/<cluster>` lists the templates of a cluster.

### Fixed
- **The VM editor was inventing the storage class of an image.** It built
  `longhorn-<image name>`, which only matches the older convention. Images
  with a backing-image backend get `lh-<uuid>`, and the invented name does
  not exist: the PVC stays Pending on "storageclass not found" and the VM is
  never schedulable. Found by creating a VM for real on harv1 and watching
  it fail. The class is now read from the image, where Harvester publishes
  it, and `/api/images` exposes it along with the virtual size, which is the
  floor for a disk built from that image.

### Tests
- Instance naming, and the trap behind it: three VMs built from one manifest
  must not share a PVC. The source manifest is also checked to be left
  untouched, since every instance is derived from it.
- Server-side fields stripped, the start checkbox driving runStrategy, and
  the refusals of the endpoint, including a name too long to survive being
  numbered.
- In a browser: the eight sections rendering on a skeleton without a single
  JS error, which is where the whole approach could have broken; the
  complete manifest reaching the request; the dry run; and a refusal landing
  under the operator's eyes.
- 621 API tests green, plus 6 browser tests on the panel.

### Verified for real
Two VMs created from the panel on harv1, from an image, with a count of two:
both reached Running with their own PVCs Bound on the right storage class,
then were deleted. The cluster is back to its 14 original VMs.

## [1.27.0] - 2026-09-15 - What a cluster that is switched off should look like

Reported by switching to a cluster that had just been powered off: the screen
span with nothing to show, then gave up without explaining, and coming back to
a healthy cluster displayed it as "not ready". Powering that cluster down for
real also exposed two safety nets that were not doing their job.

### Fixed
- **A declared but powered-off cluster froze the console.** Every call waited
  for `kubectl` to give up on its own: 30 s for the status, 15 s for the VM
  list and the topology, and 75 s for the Cluster API diagnostic, which chains
  those calls. Measured against the real harv3 with its power off. A machine
  that is off is recognised in two seconds, because its API server refuses the
  TCP connection, so that is asked first and the answer says "unreachable"
  along with the address. The four calls now answer in well under two seconds,
  and the answer is cached briefly so one cluster switch probes once.
- **A late failure repainted the cluster you had switched to.** Nothing
  checked that a response still concerned the cluster on screen, so the dead
  cluster's error, arriving 30 s later, landed on a healthy one and showed it
  as not ready. The overview and the VM list now drop a response whose cluster
  changed while it was in flight.
- **The Cluster API tab reported a stack that was "fully installed" on a
  cluster that was not answering at all.** `[].every()` is `true` in
  JavaScript, so an empty component list came out green.
- **The Longhorn detach wait was watching nothing.** It picked VM volumes by
  looking for "virt-launcher" in `workloadName`, but Longhorn puts the VM's
  own name there; the discriminator is `workloadType` being
  `VirtualMachineInstance`, and "virt-launcher" only ever appears in
  `podName`, a field that was not even read. So the filter never matched, on
  any cluster: since 1.8.9 the step returned immediately announcing "all VM
  volumes detached", and filed the disk of the VM it had just stopped under
  the pod volumes it ignores. This is the invariant the toolkit puts forward,
  and cutting power while a volume is still attached is exactly what the step
  exists to prevent. Verified on the live harv1: the old filter returned zero
  matches while two VM volumes were attached.
- **Clicking the cluster picker still left the menu open**, which 1.26.0
  claimed to have fixed. Telling a keyboard focus from a mouse focus with
  `:focus-visible` is not enough: Chromium sets it on a `<select>` focused
  with the mouse too, because a select is driven by the keyboard afterwards.
  The input modality is now tracked explicitly, so only Tab navigation holds
  the menu open. Caught by the test written for 1.26.0, which had started
  passing for the wrong reason.
- **A failed cordon was reported as a success.** `|| true` swallowed the
  error and the step emitted "done" unconditionally. It now counts what it
  actually cordoned, and tells a real failure apart from Harvester's
  legitimate refusal to cordon the last available node of a single-node
  cluster, which is harmless when the whole cluster is going down.

### Tests
- The probe: reading the endpoint from the current context, the default port
  per scheme, an unreadable kubeconfig deciding nothing rather than locking a
  healthy cluster out, a listening socket, a closed port answering in under
  three seconds, and one probe per switch thanks to the cache.
- The four endpoints answering "unreachable" with the address named, and the
  guarantee that a reachable cluster is never blocked.
- The stale-response guard, the empty-component-list green, and the message
  existing in the five languages.
- The two shutdown safety nets, including that the keep and drop filters use
  the same criterion so a volume cannot fall into both lists or neither.
- Both halves of the menu rule: a mouse click inside it closes it, Tab into
  it opens it. The second is why the mechanism exists, and the guard against
  the first must not take it away.
- 591 API tests green, 97 browser tests.

### Known limitation
While the selected cluster is unreachable, the Cluster API tab shows the
unreachable notice instead of its bundle management, which is local and would
still work. Switching to a reachable cluster brings it back.

## [1.26.1] - 2026-09-11 - Two ways in, told apart

### Fixed
- **The provider update panel did not say which button did what.** Its two
  forms, one for the network and one for a local file, were stacked with
  their action bars between them. A bar with a rule above it reads as a
  separator, so the "Install / update" button looked like it belonged to
  neither block, or to the airgap section below it. The two routes now sit
  side by side, each one a bordered block carrying its own button at its own
  foot, aligned on the same baseline. Nothing about the behaviour changed.

### Tests
- Each route's submit button is asserted to live inside its own fieldset,
  and the two blocks to share the same top and bottom.

## [1.26.0] - 2026-09-11 - The chrome gets out of the way

Four things reported by using the console, all of them about the frame
around the work rather than the work itself.

### Added
- **The sidebar is a rail that opens on hover.** 56 px of icons, expanding
  to 240 px while the pointer is on it. It expands on keyboard focus too, so
  tabbing through it no longer walks a list of invisible labels, and it can
  be pinned open from its footer if you want the labels permanently. The pin
  is remembered.
- **The window bar lists every open window, not only the minimised ones.**
  A console hidden behind another window appeared nowhere: you had to
  minimise it for it to exist somewhere, which is the opposite of what a
  taskbar is for. Every open panel now keeps a chip. Clicking it sends the
  window away, clicking again brings it back, and a window that is merely
  covered comes forward instead.
- **Windows of the same machine stack under its name.** Three windows on one
  VM used to be three chips all repeating `default/leap156`. The name is now
  written once and its windows sit behind it, each showing only what it is.
- **Slow tabs blur behind a named loading veil**, the same one the cluster
  switch and the overview already used. The Cluster API diagnostic queries
  the cluster and takes about ten seconds on a real one; blanking the card
  for that long read as a broken tab. An anti-flash threshold keeps a fast
  answer from showing anything.

### Fixed
- **Opening the menu moved the whole page.** It was a column in the flow, so
  every hover resized the work area and the topology detail panel, the
  canvases and the tables jumped. It is a layer now, and the layout reserves
  the rail width and nothing else: the geometry of the content is identical
  whether the menu is closed, open or pinned. Verified on the overview, the
  VM list and the activity tab, down to the pixel.
- **Clicking a menu entry from the rail did nothing at all.** Making the menu
  open on hover exposed three ways its own rows could move while it opened:
  the header lost 55 px when its cluster picker left the flow, icons were
  18 px in the rail against 16 elsewhere (one pixel per row, accumulating),
  and above all the labels became visible before the width had finished
  animating, so "Virtual machines" wrapped inside 56 px and pushed every row
  below it down. You pressed on one entry and released on another, and the
  browser dropped the click. Rows now hold their position from the first
  frame to the last.
- **The VM editor printed raw `<svg …>` markup** in front of every tag,
  placement rule, disk, NIC, device, toleration, cloud-init user, mount and
  file. Eleven section headers. The renderer escaped the whole summary line,
  which was right when those lines held emoji and wrong the day they held
  icon markup. The fix is not to stop escaping (those lines carry values
  from the VM spec): the contract now separates the icon NAME, taken from
  the vetted set, from the TEXT, which is still escaped.
- **The cluster picker disappeared into the rail.** Hiding the most used
  control of a multi-cluster console behind a hover was a step backwards, and
  it made the picker unreachable to the keyboard and to scripts. It now sits
  in the rail at rail width, name truncated but readable; the "add cluster"
  button, a rare gesture, waits for the menu to open.
- **Clicking inside the menu left it open over the page.** Expansion followed
  focus, so a click on the cluster picker parked a 240 px layer over the
  content and it swallowed clicks underneath until you clicked elsewhere.
  Keyboard focus still opens it, because that is what it is for, but it is
  now told apart from a mouse click.
- **A duplicate i18n key was silently overwriting a translation.**
  `common.loading` existed twice per language; the second won, so the one
  that was added had no effect and the veil showed "Loading..." instead of
  naming the cluster. A JavaScript object literal accepts duplicate keys
  without a word.

### Tests
- The audit the report asked for, as a test: content geometry is compared
  across the three menu states, and a failure names the element that moved.
- The window bar: every open window listed, same-entity stacking, the
  send-away/bring-back round trip, a covered window coming forward, closing
  from a chip, and the bar reserving space so it does not cover the content.
- The loading veil, with the response deliberately slowed so the test does
  not depend on how fast the cluster answers, plus the anti-flash threshold.
- Summary headers: no implementation may embed icon markup, the renderer
  escapes text but not the icon name, the live refresh goes through the same
  path, and every icon named by a header exists in the set.
- Duplicate i18n keys, checked against a deliberately reintroduced one.
- 568 API tests green, and 94 browser tests, 17 of them new on the menu, the
  window bar and the veil. The five browser failures that remain are the ones
  that were already there before this release (theme switching, the shutdown
  VM list and groups, and an automation selector that matches three elements
  since other inline strips were added); none of them is touched by this work.

## [1.25.0] - 2026-09-11 - Updating the Terraform provider from the console

The provider shipped in the package ages faster than the toolkit: a newer
Harvester release can require a newer provider, and replacing it meant shell
access to the host. It is now a panel in the Terraform tab, and a CLI for the
hosts that have no console.

### Added
- **Provider install and update, three ways.** A version (`1.7.3`) pulls the
  official GitHub release and verifies it against the `SHA256SUMS` published
  beside it; an http(s) URL is fetched as-is, for an internal mirror; a file
  upload covers the airgapped site with no outbound network at all. An
  expected SHA-256 can be supplied by hand, and then it wins over the
  published one.
- **`bin/harvester-provider-install.py`** does the actual work, standard
  library only, and is what the console spawns. The same command installs the
  provider on a host that has no console at all:
  `bin/harvester-provider-install.py 1.7.3 --dest <dir>`.
- **The Install sub-tab now names the active binary**, its version, its
  architecture, and where it came from: installed here, from the package, or
  a custom path. One button reverts to the provider shipped in the package.
- **Refusals that explain themselves**: a checksum mismatch, a binary built
  for another architecture (caught from the ELF header, instead of letting
  Terraform fail later on an `exec format error`), an archive with no
  provider in it, and the most common one of all, an HTML error page
  downloaded in place of the file.

### Fixed
- **Six mutative endpoints were not rate-limited at all.** flask-limiter
  silently ignores a limit string it cannot parse, and those six carried a
  decorator written as an action name (`@_rate_limit("iso-fetch")`) rather
  than a rate. No exception, no log, no limit. They now carry real rates, and
  an unparseable one raises at import instead of disappearing.
- **The reported provider version was the source repository's git tag**, even
  when the binary actually in use came from somewhere else. It is now read
  from the binary that Terraform will really execute.
- **A provider unzipped by hand was not found.** Extracting the official
  archive yields `terraform-provider-harvester_v1.7.3`, a name that was
  searched for nowhere, so the tab said "missing" with the binary sitting
  right there. It is now found, and it carries its own version.
- **Rebuilding the same version left the old plugin in the workspace
  mirror**, indefinitely, because only the version directory was compared.
- **`install.sh` deployed a hardcoded list of three scripts.**
  `harvester-iso-remaster.sh` had been missing from it since 1.19.0, so on a
  host installed that way the bare-metal install failed on a script that was
  not there. It now deploys every helper in `bin/`, which is also what keeps
  the next one from being forgotten.

### Changed
- **A provider installed from the console takes precedence over the packaged
  one** (otherwise the update would sit on disk with no effect), while an
  explicit `HARVESTER_OPS_TF_PROVIDER_PATH` still wins over both: it is the
  operator's way out when everything else guesses wrong.
- **Changing the provider re-initialises every workspace** on its next apply.
  Without it the update would have had no effect on clusters already in use:
  apply only runs `terraform init` when `.terraform/` is absent, and the
  local mirror keeps its own copy of the plugin. Terraform state, `.tf` files
  and their sidecars are left untouched.
- The workspace plugin mirror is laid out for the host architecture instead
  of assuming `linux_amd64`.

### Tests
- The installer exercised for real on archives built inside the test: local
  zip, bare binary, wrong checksum, zip-slip member, wrong architecture, HTML
  error page, and reinstallation over a previous version.
- Resolution order, version reading, and the workspace invalidation keeping
  state while dropping what is rebuildable.
- Endpoint refusals, including a local path as a source, which would
  otherwise let any authenticated account have an arbitrary host file
  installed and executed as the provider.
- Every `@_rate_limit` string in the source is checked to actually parse.
- Test isolation: the suite no longer sees the provider really installed on
  the machine running it, which used to break it as soon as the feature had
  been used once.
- Deployment: `install.sh` and the container image are checked to carry
  every helper in `bin/`, executable.
- 562 API tests green, plus 4 browser tests on the update panel.

### Verified for real
Against harv1 and the upstream release: `1.8.0` (packaged) to `1.7.3`
(installed by version, checksum verified against the published SHA256SUMS)
to `1.7.2` (uploaded as a file) and back to `1.8.0` (reverted). Terraform
re-locked the right version each time, and the `1.7.3` provider read a real
image from the live cluster.

## [1.24.0] - 2026-09-11 - Three defects found by using the thing

### Fixed
- **`network_name` was labelled optional next to a `bridge` interface.**
  That field is where the virtual NIC gets bridged. The provider's schema
  calls it optional, but its constructor *infers* the interface type from
  it (empty gives masquerade, set gives bridge), so the two are coupled.
  Showing it as optional beside the default `bridge` type let the operator
  build the one combination the provider never produces on its own: a
  bridge with no network to attach to. It is now required when the type is
  `bridge`, recomputed live when the type changes, and its description says
  what the field does rather than what it contains.
- **The Harvester provider was reported missing while sitting on the
  machine.** Only one location was searched, the packaged one, and the
  badge did not say which. Several locations are now searched, including a
  locally built provider and a `HARVESTER_OPS_TF_PROVIDER_PATH` list, a
  file that is present but not executable is not mistaken for a provider,
  and when nothing is found the tab lists every path it tried plus the
  environment variable to set.
- **The dropdown placeholder was hardcoded English** in all five languages,
  and kept saying "optional" on a field that had become required.

### Added
- The bare-metal module in the README screenshot gallery.

### Tests
- `network_name` requirement follows the interface type, live, scoped to
  its own block so `disk[0].type` cannot interfere.
- Provider discovery: several candidates, a built provider is found
  wherever it sits, a non-executable file is refused, and the endpoint
  reports where it looked.
- 525 tests green.

## [1.23.2] - 2026-09-10 - Two portability defects found by a contributor

Both surfaced by a contributor running the suite on a Mac without kubectl,
in the course of reviewing an unrelated pull request.

### Fixed
- **A missing `kubectl` raised instead of degrading.** `_kubectl_json`
  promises "None on any error, logged at WARNING", but only caught timeouts
  and parse errors: an absent binary came back as an unhandled
  `FileNotFoundError`, so an endpoint answered with an opaque 500 on the
  most likely failure of a fresh install. It is now logged like the others,
  with the cluster, under a new `unavailable` metric status.
- **`date -Is` is a GNU extension.** On BSD and macOS it fails, and the CLI
  log header came out with an empty `started=`. The format is now spelled
  out and works on both. The test accepted the empty value, which is how it
  went unnoticed; it now requires a real timestamp.

### Tests
- A missing binary is logged and returns None rather than raising.
- The header timestamp is matched against a real date, not just its label.
- 505 tests green, checked again with `kubectl` off the PATH and against a
  `date` stub that rejects `-I` the way BSD does.

## [1.23.1] - 2026-09-10 - No lab addresses in the shipped tab

### Fixed
- The BMC discovery placeholder still carried the development lab's iLO
  addresses. 1.19.0 moved the install form to the documentation range
  (RFC 5737) and added a test for it, but the test only scanned that one
  fieldset. It now covers the whole tab.

## [1.23.0] - 2026-09-10 - Filtering the Activity tab

### Added
- **Filters on Activity**: by cluster, by status, by kind of action, plus a
  free-text search over ids, action names, clusters and error messages.
  They combine, they survive a reload, and a Reset button appears only when
  something is filtered.
- **The filters run in SQL, not on the page already loaded.** That is the
  whole point: with 200 recent runs on one cluster, another cluster's
  failures sit outside the default window, and a client-side filter would
  answer "no errors on harv3" with confidence. A test builds exactly that
  history and proves the filter still finds them.
- **The counter says how much is hidden** ("61 of 514 entries"), filtered
  or not, because the table is capped either way and a short list should
  not read as a quiet history.
- The filter menus are built from the values actually present, including a
  cluster that has been removed from the configuration but still has
  history. A filtered value missing from the menu stays selectable rather
  than silently resetting itself.

### Changed
- CLI log files are parsed **server-side** into cluster + action, instead
  of the browser re-deriving them from the file name. The server is the one
  filtering on those fields; both had to read the name the same way.

### Tests
- The whole-history property, each dimension, the AND combination, and a
  quote in the search box (the query is parameterised, so `'; DROP TABLE`
  finds nothing and breaks nothing).
- File-name parsing, including a cluster whose name contains a dash and an
  action that does too (`harv-second` / `ns-stop`).
- Three browser tests: the filter reaches the server, it survives a reload,
  and the counter is rendered.
- 504 tests green.

## [1.22.0] - 2026-09-10 - Logs say which cluster they are talking about

With one declared cluster the question never came up. With several, the
interface answered it (a Cluster column in Activity, `action -> cluster` on
every dock card) but the logs did not.

### Fixed
- **CLI logs carried the cluster only in the file name.** A line copied out
  of its file — into a ticket, a support bundle, a chat — no longer said
  what it was about. Every line now carries `[cluster]`, and each log opens
  with a header giving the version, the action, the cluster, the host, the
  user and which kubeconfig was used. That last one matters because a
  kubeconfig is not obliged to be named after its cluster: harv1's is
  `harvester.yaml`. Only its basename is written, so a file meant for
  support does not spell out the machine's directory layout.
- **`kubectl ... failed` did not say on what.** That is precisely the line
  you read when something is wrong. Failures, timeouts and parse errors are
  now prefixed with the cluster, resolved from the kubeconfig when the
  caller does not pass it. Same for the one other cluster-blind warning
  (`sshNames annotation not updated`).
- `STEP_EVENT|` lines are deliberately left untouched: the console parses
  them over SSE, and slipping a field in would break the live tracking.

### Changed
- **`harvester_ops_kubectl_calls_total` gains a `cluster` label.** The same
  question in the metrics plane: which cluster is failing. Scrapers that
  aggregated this counter still work; anything pinned to the exact series
  will see the new dimension.

### Tests
- The CLI library is exercised for real (sourced, run, log file read back):
  header present, every line prefixed, lines emitted before the cluster is
  known still work and stay unprefixed, machine-readable events unchanged,
  and the full kubeconfig path absent.
- Server side: a kubeconfig resolves back to its cluster name, an explicit
  cluster wins over the lookup, a failing call names it, and the metric
  carries the label.
- 492 tests green.

## [1.21.0] - 2026-09-10 - The overview kept showing the previous cluster

### Fixed
- **The topology painted the wrong cluster.** With a cluster selected from
  another tab, coming back to Cluster / Overview showed the *previous*
  cluster's node and VMs under a heading naming the new one. The topology
  was only ever (re)mounted by a click on its own sub-tab, so arriving from
  anywhere else left the old canvas in place. Worse, its 8-second poll kept
  running in the background while you were on other tabs, repainting that
  stale cluster and querying it for nothing. Arriving on the overview now
  remounts it, leaving it stops it.
- **A response in flight during a switch is dropped.** It belongs to the
  cluster you just left; painting it would overwrite the new one's view
  moments after it loaded correctly.

### Added
- **Slow views veil their own zone.** The same blurred transition as the
  cluster switch, but scoped: the topology of a large cluster and the
  overview metrics blur only their own panel, naming what is loading,
  while the rest of the page stays sharp and clickable. It appears only
  past 250 ms, so a fast cluster shows nothing at all, and never on a
  background refresh — otherwise the panel would blink on every poll.
- The veil is now one generic module (`veil.js`), full-screen or scoped to
  an element, replacing the cluster-specific overlay.

### Tests
- Four more browser tests: returning to the overview asks for the *current*
  cluster's topology, leaving it stops the polling entirely (checked past
  the 8-second period), a slow view veils only its own zone with the rest
  of the page still usable, and a fast view veils nothing.
- 482 tests green.

## [1.20.0] - 2026-09-10 - Switching cluster actually switches cluster

### Fixed
- **The visible tab kept the previous cluster's data.** Selecting another
  cluster refreshed the overview and nothing else: staying on Cluster
  API, on the VM list or on the topology left the former cluster's
  numbers on screen, reading as if they were the new one's. That is the
  worst kind of defect for an operations console, because nothing looks
  wrong until you act on the wrong machine. The tab you are looking at is
  now reloaded; the hidden ones already rebuilt themselves on activation.

### Added
- **A loading veil while the switch happens.** The page behind it is
  blurred, a card names the cluster being loaded, and clicks are blocked
  until the data on screen really belongs to it. It has a minimum
  display time so a fast cluster does not produce a flash, and a safety
  timeout so a refresh that never returns cannot leave the interface
  locked. Reduced-motion preferences drop the animation, screen readers
  get the change announced, and browsers without `backdrop-filter` fall
  back to an opaque veil rather than to no signal at all.
- `CAPI.reactivate()`, the entry point that replays the current
  Automation sub-tab so it reloads for the new cluster through exactly
  the same path a click takes.

### Tests
- Four browser tests drive a real switch between two declared clusters:
  the veil appears and names the cluster, the page is still clickable
  afterwards, and the VM list and Cluster API tabs both re-query the new
  cluster with no residual call to the old one.
- Source-level guards for what would regress silently: script load
  order, the safety timeout, the `[hidden]` rule that keeps the veil out
  of the flow, the reduced-motion block, and a check that every tab
  declared in the page is covered by the reload.
- 476 tests green.

## [1.19.1] - 2026-09-10 - Cluster declarations keep what you give them

### Fixed
Both found while adopting the cluster the bare-metal tab had just
installed, which is the first time a declaration was created through the
API rather than written by hand.

- **The Wake-on-LAN MAC address was accepted and thrown away.** The
  declaration came back looking complete, but `harvester-startup.sh` had
  nothing left to power the node on with, which is half of what this
  toolkit does. It is now kept and validated as a MAC.
- **An SSH key given as a path was blanked**, so every SSH action on the
  declared cluster failed. Uploading a key file still overrides the path,
  as before.

### Tests
- Both fields survive a declaration, and a malformed MAC is refused.
- 468 tests green.

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
