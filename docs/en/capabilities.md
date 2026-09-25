# Capabilities

harvester-ops is a modern console to run a set of SUSE Harvester
clusters. It rests on three things: **every cluster in one interface**,
**everyday operations automated** (VMs, node maintenance, ordered shutdown
and startup, Terraform, Cluster API, bare-metal installs), and **every event
on record**, whether it was made from the console or elsewhere. This page
tours each capability area, what it does, and where it lives (command line
or web console).

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
  stopped before the shutdown stay stopped. "Running" is read on the VM
  instance, not only on the run strategy (1.48.1): a VM powered off from
  its own system keeps `RerunOnFailure`, Harvester's default, and is no
  longer restarted; a running `Manual` VM is started again through the
  `start` subresource, restoring its strategy alone does not start it.
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
- **Create virtual machines**: a Create button on the VM tab opens an
  overlay carrying *every* setting a VM has, because it replays the eight
  sections of the editor rather than offering a reduced form: anything you
  can edit, you can set at creation. Name, namespace, how many (above one,
  names are numbered web-01, web-02, and each instance gets its own PVCs),
  and whether to start once created. "Validate only" asks the cluster to
  check the manifest without creating anything, and the configuration can be
  saved as a reusable Harvester template. The disk editor shows the room
  left per storage class, which is not the "available" figure Longhorn
  displays: its scheduler applies two constraints at once and the tighter one
  decides, replicas included, and what the other disks of the same VM already
  request is subtracted. Disk sizes are proposed from the image's virtual
  size, and creation can start from an existing Harvester template. A new
  disk starts as a bootable one from an image (the next ones as blank data
  disks), never as an existing volume to attach, which is the rare case and
  the only one with no size and no storage class to choose. For an image
  disk the storage class is shown but locked: it is the image's own class
  that carries the backing image, and picking another would give an empty
  disk. Blank disks let you choose it freely.
- **Is this VM on the right network, through the right card?** The VM
  editor's Network section has a Connection path tab beside the interface
  editor. It draws the chain from the VM down to the copper: vNIC, the
  attachable network, the bridge, the bond, the physical card, each hop
  carrying what is known of it, with what is declared kept apart from what
  is running because a gap between the two is what you are looking for. Two
  probes run only when asked, since each is an SSH round trip: one resolves
  the exact host port carrying this VM by matching its MAC inside the pod
  network namespaces, the other listens briefly on the physical card for an
  LLDP advertisement and reports which switch and which port answered. A
  stopped VM shows its declared path, labelled as such.
  The LLDP probe reports the switch name, the port (description and
  identifier), the switch's management address, its chassis and its
  description, one per line. Verified against real frames on the
  three-node test cluster, whose host bridge emits LLDP; the production
  LAN switch emits none, and the probe says so after listening.
- **Migrate** (1.45.0): one window, three destinations. **Another node**
  of the same cluster is the live migration (the VM keeps running, with
  pre-flight migration-info checks); **another cluster** and **a file** are
  described below.
- **VNC console** — full graphical console in the browser (noVNC over a
  WebSocket relay to the KubeVirt `vnc` subresource). Shows the whole
  boot — firmware, GRUB, kernel — thanks to an auto-retry loop that
  attaches as soon as qemu exposes the display; keyboard/mouse,
  Ctrl-Alt-Del and fit-to-window/1:1 scaling included; the console
  title bar also carries VM power controls (start / graceful stop /
  hard reset, through the VM's `restart` subresource like `virtctl restart`,
  so it restarts VMs whatever their run strategy) and shortcuts to the
  snapshot manager and VM settings. Consoles open on a VM that is reset
  reattach by themselves once it is back. Access is
  gated by short-lived single-use tickets issued by an authenticated
  endpoint; the kubeconfig needs `get virtualmachineinstances/vnc`
  (checked by the permissions matrix).
  **Several people can use the same console at once.** KubeVirt accepts
  a single VNC connection per VM and closes the previous one when another
  arrives, so the console holds one connection per VM and shares it
  between every browser: everyone sees the same screen and can type and
  click, and the status line says how many people share it (and who,
  when accounts are configured). To make a newcomer able to join at any
  moment, the shared connection uses stateless encodings (Hextile, Raw);
  the QEMU keyboard extension is kept, so a non-US keyboard such as AZERTY
  still types right. When another client outside the console takes the
  display (the Harvester UI, for example), the console says so and stops
  instead of taking it back in a loop; **Take it back** reconnects on
  demand, and the other consoles of the session rejoin by themselves.
  With identity delegation on, every person joining is checked by the
  cluster under their own identity (`get virtualmachineinstances/vnc`),
  and the connection to KubeVirt carries the impersonation.
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

**PCI passthrough and SR-IOV**: the picker lists the devices Harvester
discovered as address, node and driver; only a device claimed in Harvester
(driver `vfio-pci`) can be used, and the console never claims one itself.
Verified on the three-node test cluster with emulated hardware (a virtual
IOMMU): a network card, and an SR-IOV virtual function of another card,
chosen in the editor, reached the guest's PCI bus. On Harvester, SR-IOV
goes through such a virtual function passed through as a PCI device. A real
GPU has not been tried. **Shipped but not verified** (stated in the UI, in
the warning style): the macvtap and SR-IOV interface bindings of KubeVirt,
which need components Harvester does not ship. Everything else on this page
was exercised for real. The Cloud-init tab gains an
  **assistant** (v1.8.1) that generates clean cloud-config and
  network-data v1 YAML into the editors — hostname, users (password,
  passwordless sudo, Harvester SSH keys or raw public keys), packages,
  run commands, DHCP or static addressing — review, then save.
  Common-value fields offer a dropdown of suggestions that never
  blocks free input, and added list items are auto-named to avoid
  duplicating a sibling (eth0 taken, next is eth1).

CLI exposes the start/stop subset via `harvester-status`/`-shutdown -N <ns>`.

### Moving a VM to another cluster, exporting, importing (1.45.0)

The Migrate window of a VM (its migrate button, or `harvester-vm-transfer`
on the command line) moves or copies it to another declared cluster, or
exports it to an archive that can be imported later, elsewhere. Console and
command line run the same engine and write the same archive.

**The pre-check.** Before anything changes, both clusters are read and every
finding is shown in the interface language, blockers first; Start stays
disabled while one remains. It checks that the target answers and runs
KubeVirt, that the name is free and the namespace exists (or is to be
created), that every network and storage class of the VM has a counterpart
on the target, the allocatable Longhorn space (more replicas asked than the
target has nodes is a warning, not a lack of room: the volumes run
degraded), MAC addresses already used on the target (Harvester refuses a
duplicate even for a stopped VM), host devices and node pinning that cannot
travel, and an older Harvester version on the target.

**Two engines, chosen for you.**

- **Harvester backup**, when both clusters use the same backup target (NFS
  or S3) and the restore can succeed: every network keeps its name on the
  target and every storage class exists there. A `VirtualMachineBackup` is
  taken, the target is made to re-read the backup target (Harvester only
  does it when asked), and the VM is restored as a new VM; images the VM was
  born from come with it. Only this engine offers the **short stop**: a
  first backup while the VM runs, then a second one after the stop, which
  carries only what changed.
- **Copy through the console** otherwise. Each disk is frozen into a
  temporary image on the source (the VM is stopped for that, and restarted
  right away if it is to keep running), streamed, and imported on the
  target by a CDI `DataVolume` into an ordinary volume of the chosen
  storage class. The target's nodes fetch the disks from the console host
  over HTTP, on port 8094 by default: open it, or set `transfer:
  serve_address: host:port` in `config.yaml`.

**Final states, chosen by the operator.** The source is left running (a
copy, with new MAC addresses), stopped and annotated as moved, or deleted
with its volumes; the target is started or left stopped. The target is
verified (running, or its volumes bound) before the source is stopped for
good or deleted. Anything the transfer created carries the label
`harvester-ops.io/transfer`; a failure or a cancellation from the dock
removes it and puts the source back as it was. The target VM keeps an
annotation `harvester-ops.io/transferred-from`. The cloud-init secret a
console copy or an import recreates belongs to the target VM, as Harvester
does: deleting the VM deletes it (1.47.0).

**The export store.** The Exports button of the VM view lists the
archives: source cluster and version, date, size, and whether the archive
is complete. Download one to carry it to an isolated site, import it into a
declared cluster, or delete it. An archive (`.hvx`) is a plain tar: the
cleaned VM, the disks as Harvester serves them (gzip), and SHA-256 sums
checked during the import. It holds the VM's cloud-init secrets: it is
created with mode 0600 and must be kept like a secret.

**Getting an export back, and bringing it elsewhere (1.47.0).** When an
export ends, the Migrate window names its archive and its size, with three
buttons: Download (to this computer), Import into a cluster (opens the
import form for that archive), and Export store. On the site that receives
the file, the store's **Add an archive** button sends a `.hvx` from the
operator's computer into that console's store:

- the file travels as the request body and is written to disk as it
  arrives, never staged in memory, so a disk of tens of GiB goes through;
- before it is accepted, the archive is checked: complete, readable, and
  every member matching its SHA-256 sum. A copy damaged in transit, even at
  the right size, is refused with the member at fault, and nothing is kept;
- the store refuses a name that is already there, and says so before
  receiving anything when there is not enough room;
- the upload is an action like the others: throughput and time left in the
  window and the dock, then the checksum pass, and Cancel in the dock stops
  it and deletes the partial file.

Once added, the archive is imported like any other (import button of its
row). On the command line an archive is simply a file:
`harvester-vm-transfer import --in <file>.hvx`.

**Following a transfer, and its speed (1.46.0).** The pre-check announces
what there is to transfer (disks, size, space really used). Once started,
the Migrate window and the dock show the phase in progress (freezing the
disks, download, import, backup, images, restore), the amount done over the
total, the bytes actually sent, the throughput, the time left and the time
elapsed; each finished phase keeps its summary in Activity. When every byte
is sent but the target is still writing, it says so. For a backup, the
amount is the volumes' size: an incremental backup sends less.

A **Speed** choice says what it costs:

| Profile | What it does | Cost |
|---|---|---|
| Economical | one disk at a time | slowest, gentle on a busy cluster |
| Normal (default) | every disk at once (Longhorn compresses each download on one core) | CPU on the source, network |
| Maximum | also raises Longhorn's backup and restore threads from 2 to 8 for the transfer, put back at the end even on failure | CPU and network on every node of both clusters, shared with any other backup running meanwhile |

A **bandwidth cap** (MiB/s) limits what the console host sends, all disks
together, to spare a production link. A transfer rides out a cluster API
that drops for a while: waits retry, a broken download is served or written
again from its start, and a rollback retries its deletions and names what it
could not remove.

On the command line:

```bash
harvester-vm-transfer check   --from prod --vm default/web-01 --to dr
harvester-vm-transfer migrate --from prod --vm default/web-01 --to dr \
    --mode short --source stopped --target started
harvester-vm-transfer export  --from prod --vm default/web-01 --out /srv/exports/
harvester-vm-transfer import  --to edge --in /srv/exports/web-01-20260925-002842.hvx \
    --namespace apps --create-namespace --map-net default/lan=apps/vlan10
```

Exit codes: 0 done, 1 failed (undone), 2 refused by the pre-check,
3 cancelled (undone). Verified on real clusters (Harvester 1.8.2 and 1.9.0):
disks compared bit for bit after a copy, an export and an import across
versions.

## 3. Cluster observability (console)

- **The host network, one virtual switch at a time (Fabric).** The
  Network view below looks at the network from the VMs; this one reads it
  the way an ESXi operator reads a Standard Switch: one block per switch,
  left to right, with the networks and their VMs on the left, the switch
  in the middle, and the physical adapters on the right.
  - A **cluster network** is a switch named after its bridge (read from the
    attachable networks, not guessed from a naming convention). Its header
    carries the team policy from the VlanConfig (bond mode, MTU) and the
    number of workload ports; its uplink is the bond holding its cards.
  - A **kube-ovn provider network** is a switch too: its subnets sit on the
    left with their VLAN (VLAN 0 is shown as untagged), CIDR, gateway, VPC
    and the attachable network a VM uses to join them; its card on the
    right.
  - The **OVN overlay** is an internal switch: dotted, with no physical
    adapter, which is what it is. An overlay network bound to no subnet is
    flagged.
  - Every network lists the **VMs attached to it**, running ones first,
    the rest unfolding on demand. VMs on the pod network are listed apart,
    since that network has no bridge and leaves through the node routing.
  - Each card shows its state and, like ESXi, its speed and duplex
    ("1000 Full"). A card with no link is drawn red with a dashed cable.
    Cards on no switch are still shown, so nothing is silently missing.
  - Clicking a card or a bond opens its detail: MAC, master, speed,
    duplex, MTU, carrier changes, traffic and error counters, bond mode
    and miimon, plus the LLDP probe on a physical card. What only the node
    knows is read over SSH once per node and per session, not on every
    refresh.
  - Every name, address and CIDR has a copy button.
  Harvester does not publish its Open vSwitch bridges, so without help the
  workload ports on the kube-ovn side stay invisible. That is stated rather
  than guessed, and the view offers to install a read-only `LinkMonitor`
  that reveals them, through a tracked action and removable from the same
  banner. The view also works on a cluster without the kube-ovn add-on.

- **Cluster view, one block per host** (same layout as the other views).
  Each host shows its state (ready, cordoned, in maintenance), its roles,
  its address and two gauges: vCPU and memory given to its running VMs
  against what the host can give. Past 100 % the gauge says the host is
  overcommitted. Each VM is a card with what it consumes: vCPU, memory,
  disks ("20 GiB" or "2 disks · 60 GiB"), networks and first address.
  Hovering a card, or giving it the keyboard focus, shows the full detail:
  every disk with its size, storage class and boot order, every network
  card with its MAC and all its addresses, the guest OS and interfaces
  only the guest knows, the run strategy. Stopped VMs are grouped apart. A
  filter narrows the cards by name, namespace, address, MAC, network or
  guest OS. Clicking a VM opens its actions (notes, edit, console,
  snapshots, migrate, start, stop, delete behind the destructive lock).
  One grouped kubectl call per refresh, disk sizes included.
- **Cordon, uncordon and node maintenance**, from a host's panel. Cordon
  and uncordon set the node's `spec.unschedulable`, as Harvester does, as
  tracked actions with a confirmation. Harvester's admission webhook
  refuses to cordon, or put in maintenance, the last node still available
  (no other node that is neither cordoned nor in maintenance): the console
  applies the same rule beforehand, so on such a node the Cordon button is
  shown disabled with the reason, and the server refuses it with a 409
  instead of starting an action bound to fail. **Maintenance** is Harvester's own:
  the console asks for it the way the Harvester UI does (the
  `harvesterhci.io/drain-requested` annotation), and Harvester's controller
  drains the node. Before anything is asked, the console shows what would
  happen, with Harvester's own rules:
  - a node that is the only control plane is refused, as is a control plane
    node while another one is already in maintenance;
  - the VMs that **will migrate**;
  - the VMs that **cannot**, and why (the last healthy replica of one of their
    volumes is on this node, KubeVirt says they are not live-migratable, or no
    other node satisfies their placement rules). While there are any,
    maintenance is refused unless forced; forcing sits behind the destructive
    lock, shuts those VMs down, and they **stay stopped** afterwards;
  - the VMs the **drain will shut down**: KubeVirt live-migrates a VM on
    eviction only if its eviction strategy asks for it (`LiveMigrate` or
    `LiveMigrateIfPossible`, or the cluster default). The Harvester UI sets
    it, but VMs created with kubectl or Terraform often do not. For each one
    the console says whether it comes back on another node (run strategy
    `Always`: a restart, not a migration) or stays stopped, and how to make it
    migrate;
  - the VMs whose **volume is not healthy**: Longhorn does not migrate a
    volume while a replica waits to be rebuilt, and the maintenance can then
    take much longer;
  - the **attached volumes used by pods** whose only healthy replica is on
    the node: Longhorn will not let it go and the drain waits for ever (the
    Harvester check looks only at VM volumes);
  - the VMs labelled to be shut down during maintenance.
  The tracked action follows Harvester until the node is in maintenance (10
  minutes at most) and reports a refusal by its controller. Leaving
  maintenance makes the node schedulable again and restarts the VMs labelled
  to restart after it. Entering and leaving maintenance need the `admin` role.
  VMs created from the console now get `LiveMigrateIfPossible`, like those of
  the Harvester UI. **Verified on a three-node test cluster**: cordon and
  uncordon; the refusal to cordon the last available node (the console and
  Harvester's webhook refuse it alike); maintenance with and without forcing,
  where each VM ended as announced (migrated with the same instance,
  restarted elsewhere, stopped); the busy control plane refusal; leaving
  maintenance.
- **Network view, one block per network** (same layout as Fabric). On the
  left, every VM attached to that network with what it really has on it:
  interface name, MAC, addresses, the interface name inside the guest,
  link state, model and binding, running VMs first. Interfaces known only
  to the guest agent (docker0 and the like) are listed apart, since they
  leave through no cluster network. On the right, where the network goes
  out: the bridge, its bond and its cards; for a kube-ovn underlay, the
  subnet, its gateway and its card; for the overlay and the pod network,
  nothing physical, which is said. Networks with no VM are listed at the
  bottom. It reads the same data as Fabric, so it costs no extra call.
- **kube-ovn networks: VPCs, subnets and overlay networks, with their
  forms (VPC tab, 1.49.0).** One block per VPC, in the same layout: on the
  left its subnets (range, gateway, overlay network, NAT, DHCP, address
  usage) and the VMs holding an address in each; on the right how the VPC
  leaves (outgoing NAT through the nodes in the default VPC, its static
  routes and peerings, or "isolated"). What goes wrong is said above the
  blocks: an overlay network no subnet serves (a VM attached to it gets
  no address), a subnet whose network is gone, overlapping ranges, a
  nearly full subnet.
  - **Forms** (administrators): a VPC (namespaces; folded, static routes
    and peerings) and a subnet (VPC, a free /24 proposed away from the
    nodes, pods and other subnets, gateway derived from it, overlay network
    created with the subnet or an existing one without subnet, outgoing
    NAT proposed in the default VPC only, DHCP for the VMs; folded,
    excluded addresses, private subnet and allowed networks, namespaces).
    A pre-check runs while typing; Save stays greyed out while something
    blocks. Changing a subnet keeps its range, VPC and network, which
    kube-ovn cannot change.
  - **Deletion** is refused while a VM or a pod still uses the subnet (the
    view says which, before sending anything) or while a VPC still has
    subnets; the overlay network goes with its subnet only if the console
    created it. The default VPC, its `ovn-default` and `join` subnets and
    underlay subnets are read only.
  - Every change is an action followed in the dock; the command line does
    the same: `harvester-network inventory | check | apply | delete`.
- **Storage view, read like a datastore**: one block per storage engine.
  On the left, each storage class with its policy (replicas, what happens
  on release, source image), the room it can still allocate (the same
  figure as the VM creation panel), and its volumes grouped by VM in boot
  order, CD-ROM and ISO disks marked. Volumes mounted by pods and volumes
  claimed by nobody are grouped apart. On the right, the node disks with
  a gauge of what is written and what is promised, the room left and the
  replica count. Clicking a volume or a disk opens its detail (claim, VM,
  pods, image, requested and written size, health, replica placement).
  An **orphaned volume** (claimed by no VM, mounted by no pod, known to
  Longhorn and not attached) can be **deleted from its detail**, behind
  the destructive lock and a confirmation, as a tracked action. When the
  last workload that used it may come back (a StatefulSet volume), the
  detail says so first. The server checks again before deleting and
  refuses a claim that any running pod mounts. One grouped kubectl call.
- **Degraded volumes, explained and fixed.** A banner at the top of the
  Storage view counts the volumes that need attention (faulted, degraded,
  at risk of starting degraded) with their main cause, and opens the most
  urgent one. Each volume's detail says why, from what Longhorn reports:
  no healthy replica left, rebuilding switched off (a graceful shutdown
  leaves it off if the startup could not restore it), a rebuild in
  progress with its percentage, a new replica being prepared, a failed
  replica waiting to be reused, not enough nodes for the replica count,
  no disk with room, a replica on a node or disk that is down. Each cause
  comes with what to do, and three of them with a one-click fix: lower
  the replica count to what the cluster can hold, switch rebuilding back
  on, rebuild a failed replica now. Every fix shows its equivalent
  kubectl command, asks for confirmation, and runs as a tracked action
  that watches for the effect for a minute and says so when nothing
  changed. The server reads the cluster again and redoes the diagnosis
  before acting, with values it computes itself; it never touches a
  faulted volume, never goes below one replica, never deletes the last
  healthy copy, and does not switch rebuilding on while a cluster shutdown
  or startup is running. A node that is **down, or back without its disk
  ready yet**, is not a missing node: its replicas are reported as
  unavailable with no one-click fix, since Longhorn takes them back when
  the node returns, and rebuilding now or lowering the replica count would
  turn a passing outage into lost data or lost redundancy. A detached
  volume with a replica on such a node is shown at risk. Verified on a
  three-node test cluster by cutting a node's power: a volume with no
  healthy copy left (shown faulted, no fix offered) came back on its own
  with the node; the rebuild-now fix was applied from the console to a
  replica failed on a healthy node, and Longhorn rebuilt a new one. Longhorn
  volumes whose claim was deleted are shown too, marked as such.
- **Overview metrics**: nodes, VMs running, Longhorn volume count and
  rebuild limit, node table.
- **Prometheus `/metrics`** — action counters/durations, in-flight gauge,
  kubectl call outcomes **broken down by cluster**.
- **`/healthz/ready`** — readiness probe returning 503 when config,
  clusters, or the action DB are unhealthy (`/healthz` for liveness).

## 4. Cluster API: downstream RKE2 clusters (console + CLI)

Create and operate Kubernetes clusters whose nodes are Harvester VMs,
through Cluster API and the Cluster API Provider Harvester (CAPHV). The
console runs `harvester-capi` for every step, which the CLI can run too.

### Installing the stack (1.48.0)

- **Harvester v1.9 and later** embed Rancher Turtles, which already runs the
  Cluster API core. The console declares the RKE2 bootstrap, RKE2 control
  plane and Harvester providers to Turtles (`CAPIProvider` objects) from the
  airgap bundle, without Internet access, and loads their images on the
  nodes over SSH. The Installation tab lists each provider with its version
  and state.
- **Compatibility fixes for CAPHV v0.10.1 on Harvester v1.9** are applied by
  the install and shown on the same tab: a `kube-system/ingress-expose`
  Service carrying the VIP (Harvester v1.9 removed it and CAPHV still reads
  it), a patch that keeps the Cluster API core reading the HarvesterMachine
  status the way CAPHV writes it, and generated templates moved to
  `v1beta1`. They go away once CAPHV ships the fixes.
- **Leftovers of an earlier install** next to Turtles (a second Cluster API
  core, webhooks with expired certificates) are detected; an administrator
  can remove them from the Installation tab. Nothing that Turtles owns is
  touched, and the removal refuses while clusters other than the local one
  exist.
- **Older Harvester releases** keep the previous install: cert-manager,
  Cluster API core and providers, all from the bundle.

### Creating a cluster (1.48.0)

The Cluster creation tab is a form filled from the cluster itself:

- **Essentials**: the name and the Kubernetes version (versions created for
  real with the bundle are marked "tested").
- **Size**: 1, 3 or 5 control plane nodes, the number of workers, and a
  small / medium / large preset or custom CPU, memory and disk.
- **System and access**: the images of the cluster (ISOs and images still
  downloading are left out, SUSE images first, the last one used is
  remembered), the SSH user suggested from the image's system, and a key
  pair.
- **Network**: the VM network with its VLAN, the IP pool with its ranges and
  free addresses; the gateway and mask are taken from the pool and marked as
  such until changed; the DNS server is remembered.
- **Advanced options**, folded: namespaces of the cluster objects and of
  the VMs, extra IP pools and networks, a data disk and its storage class,
  the CNI, pod and service CIDRs, import into Rancher, Fleet add-ons with
  MTU, encapsulation and BGP.

Every control explains itself on hover. A **pre-check** runs as you type
and lists, in the interface language, what blocks (not enough free
addresses, overlapping CIDRs, image missing or not ready, a namespace that
already holds a cluster, stack not installed...) and what deserves a look
(one or an even number of control plane nodes, memory or CPU running short,
a version never created with the bundle, an endpoint address that comes
from DHCP). Create stays disabled while something blocks. **Preview** shows
the manifests with the identity secret masked.

Creation is an action: the page and the dock follow it (infrastructure,
control plane a/b, workers c/d), and it ends with a kubeconfig download.
Cancelling it removes what it created.

### Operating clusters

- List, details (spec, conditions, machines), scale the workers, download
  the kubeconfig, delete.
- **Deletion leaves nothing behind (1.48.0)**: the VMs go first, then the
  objects the console generated next to the cluster (ClusterClass,
  templates, add-ons, and the identity secret, which holds a kubeconfig of
  the Harvester cluster) once no other cluster in the namespace uses them,
  then the namespace if the console created it.
- Kubernetes version upgrades are not implemented.
- Volumes that the new cluster's workloads claim are Harvester volumes
  (PVCs named `pvc-<id>` in the VM namespace). Deleting their claims in the
  cluster releases them; a cluster deleted with claims still bound leaves
  them behind.
- **SLES images need a registration or a local repository**: cloud-init
  installs `iptables` and `qemu-guest-agent` on each node, and without them
  pods that publish a host port (ingress-nginx) do not start. The
  pre-check says so. The openSUSE Leap 15.6 cloud image works as is.

### Bundles and CLI

- Timestamped airgap bundles with an active marker, inspect, upload,
  download, and a Harvester version compatibility check.
- `harvester-capi status | install | cleanup-legacy | inventory | check |
  render | create | delete`, each with `--cluster` or `--kubeconfig`;
  `create` exits 2 when the pre-check blocks. The manifests are rendered by
  `caphv-generate`, shipped with the console (taken from CAPHV at a fixed
  commit, see `bin/caphv-generate.PROVENANCE`).

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
- **Provider updates from the console**: install a different build of
  `terraform-provider-harvester` without shell access to the host. Give a
  version (the official release is downloaded and checked against the
  published `SHA256SUMS`), a URL to an internal mirror, or upload the
  archive from a machine that has no outbound network at all. The
  Install sub-tab names the active binary, its version and where it came
  from, and one click reverts to the provider shipped in the package.
  Every workspace is re-initialised on its next apply; Terraform state is
  never touched. The same install runs from the CLI:
  `bin/harvester-provider-install.py 1.7.3 --dest <dir>`.

## 6. Bare-metal (console)

Turn a blank server into a running Harvester node without touching it.
Full walkthrough in **[bare-metal.md](bare-metal.md)**.

- **BMC / Redfish discovery** — point at one or many BMC endpoints and
  read back node profiles: model, serial, BIOS, memory, NICs with their
  MACs, power state, bootable disks, and whether the machine can be
  installed at all. Works on iLO 4/5, iDRAC 9 and standard Redfish; the
  system and manager paths are resolved per vendor rather than assumed.
- **Power actions** over Redfish, with the same dynamic path resolution.
- **Installation image store** — download a Harvester ISO server-side, as
  a stream, tracked with a percentage. The 7.6 GB image never passes
  through the browser and is not shipped in the tarball.
- **Unattended installation** — pick an ISO, fill in the node (hostname,
  install disk, management NIC, addressing, VIP, DNS, token, OS
  password), and the console remasters the ISO for zero-touch install,
  publishes it behind a one-off token, mounts it as virtual media, sets a
  one-shot `Cd` boot and powers the machine on. Tracked step by step in
  the dock, from preflight to the Harvester API answering on the VIP.
- **Preflight against stale inventory** — a powered-off BMC replays the
  inventory of its *last POST*, which can be months old. The install
  powers the machine on and reads the real hardware before deciding.
- **Extra kernel arguments** for the cases the defaults do not cover:
  skip the hardware preflight, or mirror the whole install onto the BMC
  serial console, which is the only way to watch an unattended install.
- **Credentials are never stored** — BMC username and password stay in
  the page for the session; the cluster token and OS password never
  appear in a response, an action label or a log line.

## 7. Operations support (CLI + console)

- **A cluster that is switched off says so.** A declared cluster whose API
  server does not answer is recognised in about two seconds, and the console
  says which address is unreachable instead of spinning for up to 75 seconds
  and then giving up silently. Responses that arrive after you have switched
  clusters are dropped, so a dead cluster's failure can never be displayed as
  the state of a healthy one.
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

## 8. Rights and roles

Until 1.30.0 the console authenticated a password and nothing more: any
account that got in could shut down a cluster, delete a VM or destroy a
Terraform workspace.

- **Three roles**, declared in `/etc/harvester-ops/roles.yaml`: `viewer`
  reads, `operator` performs the everyday mutating work (VMs, snapshots,
  Terraform applies), `admin` adds what cuts a service or changes the tool's
  own configuration: cluster power sequencing, node maintenance, cluster
  declarations, bare-metal, the ISO store and the Terraform provider.
- **Enforced centrally, deny by default.** Any request that changes
  something needs at least `operator`, and an explicit list of paths needs
  `admin`. An endpoint added tomorrow is protected without anyone having to
  remember; a test walks the whole route table to prove none escapes.
- **A refusal says what is missing**, naming the role required and the one
  you have, instead of a bare 403.
- **Roles need identities.** With no htpasswd nobody can be told apart, so
  nothing is restricted and the console says so rather than quietly putting
  everyone, including the operator, in read-only.

- **The cluster's own accounts** (Settings > Cluster accounts): the accounts
  declared on the selected Harvester cluster, whether each is enabled, and
  who holds cluster administration, in one view. Administration can be
  granted or revoked, and accounts enabled or disabled. Orphaned
  administration is surfaced: a subject holding cluster-admin with no
  account behind it means the account was deleted and its delegation was
  not, so recreating one with that id would silently give it back. Creating
  a local account with a password is deliberately not offered: Harvester
  stores it as a derived key whose scheme this console will not guess.

### The identity the cluster sees (1.32.0)

Roles above are enforced by the console. On their own they changed nothing
for the cluster: the shared kubeconfig is `system:admin`, group
`system:masters`, a hardcoded superuser in the apiserver that bypasses RBAC
entirely. Whoever was at the keyboard, the cluster applied no rule of its
own.

Each console account can now be mapped to a cluster identity, and every
kubectl call carries it, so Harvester's RBAC finally applies.

```yaml
# /etc/harvester-ops/roles.yaml
default_role: viewer
identity:
  delegate: true          # off by default: an upgrade changes nothing
  deny_unmapped: true     # an account with no mapping is refused (default)
users:
  alice: operator                     # short form, still valid
  bob:
    role: admin
    cluster_user: user-2fhwx          # the Harvester user id, not the login
    cluster_groups: [harvester-admins]
```

- `cluster_user` is the **id** of the `users.management.cattle.io` object
  (`user-2fhwx`), not the login. Settings > Cluster accounts lists them.
- **Unmapped accounts are refused** while delegation is on, rather than
  falling back to the shared administrator kubeconfig, which is what
  delegation exists to remove. Set `deny_unmapped: false` to allow it.
- **A cluster refusal reads as a refusal**: 403, with the cluster's own
  reason and the identity that was refused, not an opaque server error.
- The role badge in the sidebar says which of the three states applies:
  delegated, not delegated, or delegated with no identity.
- **CLI parity.** The scripts take the same identity from the environment:

```bash
HARVESTER_OPS_AS=user-2fhwx HARVESTER_OPS_AS_GROUPS=harvester-admins \
  ./bin/harvester-shutdown.sh --cluster harv1 --dry-run
```

Check what a cluster actually grants an identity before relying on it:

```bash
kubectl --kubeconfig <kc> auth can-i list virtualmachines.kubevirt.io -A \
  --as user-2fhwx
```

**What this is still not.** The console HOLDS the administrator kubeconfig:
this is a boundary the cluster enforces, not a vault, and a flaw in this
layer would hand back full powers. The identity is declared locally rather
than proven by an identity provider; sourcing it from OIDC is the next step.

## 9. Cross-cutting

- **A sidebar that gives the screen back.** The left menu is a 56 px rail
  of icons that expands over the page while the pointer is on it, and
  collapses when it leaves. It is a layer, not a column: opening it never
  resizes the work area, so the topology detail panel, canvases and tables
  do not jump under the operator. Pin it open from its footer when you want
  the labels permanently; the pin is remembered.
- **A window bar that lists every open window.** Consoles, VM settings,
  snapshots, migrations and notes each keep a chip above the dock for as
  long as they are open, not only once minimised. Clicking a chip sends its
  window away and brings it back; a window hidden behind another comes
  forward. Windows belonging to the same machine stack under its name,
  written once, so three windows on one VM cost one entry rather than three
  that all repeat `default/leap156`.
- **Loading states that look the same everywhere**: a slow tab (the
  Cluster API diagnostic queries the cluster and can take ten seconds)
  blurs its card behind a named loading veil instead of blanking it, with
  an anti-flash threshold so a fast answer shows nothing at all.
- **It stops asking the cluster the same thing.** Every kubectl invocation
  pays a full process start before it touches the network, so the console
  groups what it can: the cluster watcher polls its five resource types in
  a single call, and the status script makes two grouped calls instead of
  five. A hidden browser tab stops polling entirely (returning to it
  refreshes at once), and the watcher slows down after five minutes with no
  human request. Monitoring scrapes of `/metrics` and `/healthz` do not
  count as a human, or the console would never be idle. Tune with
  `HARVESTER_OPS_WATCH_IDLE_AFTER` (default 300 s) and
  `HARVESTER_OPS_WATCH_IDLE_INTERVAL` (default 120 s).
- **Action tracking + dock** — a persistent bottom dock shows in-progress
  and recent actions on every tab, with live step/log streaming over SSE
  (auto-reconnecting). Failed actions carry the underlying error (last
  `kubectl` / script stderr line) in the dock, the Activity table and the
  details panel — never a bare `exit 1`.
- **Changes made outside the console show up too.** A watcher polls the
  namespaces, VM images (with upload progress), networks, volume claims and
  VMs (with their state changes) of each cluster, and each change made
  elsewhere (Harvester UI, kubectl, Rancher) becomes a completed action in
  the dock and the Activity tab. Its last snapshot is kept on disk, next to
  the action history (`watch/` beside the actions database, or
  `HARVESTER_OPS_WATCH_STATE_DIR`), so what changed while the console was
  stopped, or while a cluster was unreachable, is reported at the first
  round after, marked as such; an image upload still running is followed
  again. Only what is needed to compare is kept (names and a few status
  fields), readable by the service account alone.
- **Filtered activity** — the Activity tab filters by cluster, status and
  kind of action, with a free-text search over ids, actions and error
  messages. The filters run against the whole history in SQL, not against
  the page already displayed, so a quiet cluster's failures are still
  found; the counter states how many entries are shown out of how many
  exist.
- **Durable action history** — the last 500 runs (with their step/log
  events) are persisted in SQLite and served back by the Activity tab and
  its details replay, across UI restarts. In-memory eviction only affects
  live SSE attachment, never the visible history.
- **Cluster switching that actually switches.** Picking another cluster
  blurs the page behind a named loading veil and reloads the view you are
  on, then hands the page back. It matters because the failure mode is
  silent: the previous cluster's numbers stay on screen and read as the
  new one's, which is how an operator acts on the wrong machine.
- **Every log says which cluster it is about.** CLI logs open with a
  header (version, action, cluster, host, user, kubeconfig) and prefix
  every line with `[cluster]`, so a line pasted into a ticket still says
  what it refers to. Server-side `kubectl` failures name the cluster too,
  and the metrics break down by it.
- **Slow views veil their own zone.** The same transition, scoped: a large
  cluster's topology blurs only its own panel while it loads, naming what
  it is fetching, with the rest of the page sharp and usable. Nothing
  shows below 250 ms, and never on a background refresh.
- **Internationalisation** — five complete languages (EN, FR, DE, ES,
  IT), with a parity test that fails the build on a missing key.
- **Icons** — one monochrome SVG set, [Lucide](https://lucide.dev)
  (ISC, vendored in the tarball, never fetched at runtime), inheriting
  `currentColor` so it serves every theme and both modes — including the
  topology canvas, where the same glyphs are drawn as node images. It
  replaced the emoji icons, which mixed full-colour images with thin
  glyphs, rendered differently per platform and read poorly at button
  size. `scripts/gen-icons.py` regenerates the set from the pinned
  release.
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
