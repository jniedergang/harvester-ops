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

### The actions menu of a VM, as in Harvester (1.60.0)

Each VM row has a **⋮** button. It opens a menu grouped the way Harvester's
is, built from the VM's actual state (read when the menu opens): an action
that does not apply is greyed out and says why, instead of failing after the
click (a soft reboot without a guest agent, a migration with no other ready
node).

- **Power**: Restart (within the grace period), Soft reboot (the guest
  reboots itself, through the guest agent), Pause / Unpause, Force stop
  (the instance is cut without waiting for the guest).
- **Protection**: Take backup (to the backup target), Take snapshot.
- **Disks**: Add a volume while the VM runs (an existing volume of the same
  namespace that no VM uses, on scsi, virtio or sata), detach a volume that
  was plugged that way, eject a CD-ROM (and delete its volume once the VM
  is stopped).
- **Migration**: Migrate, to a node you choose or any ready one; Abort the
  migration in progress.
- **Copy**: Clone, with or without the data (with the data, each volume is
  a Longhorn clone of the original; without, volumes start again from
  their image, or empty); the MAC and IP addresses are not copied and the
  cloud-init is copied into a secret of its own. Generate a template, or a
  new version of an existing one, with or without the data (with the data,
  each disk is first exported to an image).
- **YAML**: Edit YAML, Download YAML (below).
- **Delete**: choose which volumes go with the VM (the system disk is
  checked, as in Harvester); its cloud-init secret goes too unless another
  VM uses it.

The list also offers **Restart**, **Force stop** and **Migrate** on the
selected VMs. Every action is a tracked action (dock, Activity), run by
`harvester-resources vm <action>`, which can be used alone.

### Changing a running VM, as in Harvester (1.61.0)

The VM menu also carries Harvester's live actions. Each one reads the VM's
state first and is greyed out, with the reason, when it cannot apply.

- **Edit CPU and memory** while the VM runs, up to the maximums set at
  creation ("Enable CPU and memory hotplug" in the compute settings: one
  core per socket, maximums four times the start values unless given,
  limits equal to the maximums, 1 GiB of memory at least). KubeVirt applies
  the change by moving the VM live; the console waits until the guest has
  the new CPUs and has taken the memory in (its kernel must support
  virtio-mem, as recent distributions do).
- **Insert an image** into an empty SATA CD-ROM drive, and **eject** it,
  while the VM runs; the drive stays, the image's volume is deleted.
- **Add a network interface** on a bridge VM network, and **unplug** it; the
  change is applied by a live migration, or at the next restart when there
  is no other node.
- **Migrate a volume** of the running VM to an existing, unused volume of the
  namespace (another class, a larger size); **cancel** while the copy runs.
  Once KubeVirt has switched to the target, the cancel is refused: going back
  would restart the VM on the old copy.
- **Create a schedule** for this VM (the Backups window opens on it) and set
  its **snapshot quota**.
- **Add an access**: a password for an account, or SSH keys for accounts,
  set in the guest by its agent at the next restart. The password goes
  through a private file, never on a command line or in a log.
- **Serial console** in a terminal, and **View logs** of the VM's
  virt-launcher pod (operators).

The VM list shows the CPUs, the memory, the IP addresses and the node, can
be sorted by each, and a filter keeps the VMs matching a name, an IP, a node
or a label (`key=value`). The settings window writes Harvester's own fields:
display name, description (the key Harvester reads), operating system,
maintenance strategy, reserved memory.

### More when creating and editing a VM (1.62.0)

- **Static IP** per network interface, written where Harvester reads it
  (`static-ip.harvesterhci.io/<interface>`); the VM list shows it first. On
  an overlay (kube-ovn) network Harvester turns it into the interface's
  kube-ovn address, and the guest receives it by DHCP when the subnet serves
  it (verified on harv1: 10.62.0.50 in the guest 41 s after the start); on a
  VLAN network it is only shown and must be set in the guest.
- **Labels, instance labels and annotations** in the settings window, each
  as key and value rows; the keys Harvester, KubeVirt and Kubernetes manage
  are hidden and left untouched.
- **Windows answer file** (autounattend.xml) at creation: kept in a Secret
  and given to Windows Setup on a SATA CD-ROM named `sysprep`, as Harvester
  does.
- **Filesystem volumes (virtiofs)** at creation: a ConfigMap, a Secret or a
  ServiceAccount of the namespace shared with the guest (one of each at
  most), mounted with `mount -t virtiofs <name> /mnt/<name>`. The guest
  kernel needs virtiofs (verified with a Tumbleweed image; a minimal Leap
  image lacks it).

### Edit YAML, Download YAML (1.60.0)

Every object Harvester lets you edit as YAML can be, from its row: VMs,
images, volumes, storage classes, SSH keys, secrets, VM networks, templates,
add-ons, schedules, backups and snapshots. The text leaves out the status
and the field history; **Check** lets the cluster judge it without changing
anything (Harvester's own checks answer); **Save** replaces the object. The
text keeps the object's version: if someone changed it since it was opened,
the cluster refuses instead of overwriting their change, and the window says
so. Secrets, settings and add-on configurations are read in YAML by
administrators only, since their text carries values. Command line:
`harvester-resources yaml`.

### Cloud-init when creating a VM (fixed in 1.60.0)

The Create window's Cloud-init section was lost: the VM was created without
it. The user-data and network-data now go into a secret per VM, referenced
by the VM as Harvester does, with two fields from Harvester's form: **SSH
keys** (their public key is added to `ssh_authorized_keys`, and the VM lists
them) and **Install the guest agent** (checked by default). In a VM's
settings, saving the cloud-init is a tracked action, and it now also works on
a VM that has none yet (a secret is created and attached) or an inline one
(moved into a secret).

### Backups, snapshots and their schedules (1.58.0)

The **Backups** button, right of the namespace selector in Virtual machines,
opens a window with the four tabs of Harvester's "Backup and Snapshots"
menu, for the chosen namespace or all of them. It stays open next to the
VMs, and comes back after a reload. The backup target is shown at the top
(type and endpoint; the keys of an S3 target never reach the page).

- **VM Schedules**: a VM backed up or snapshotted every hour, day or week
  (or a cron), how many copies are kept, and after how many failures in a
  row Harvester suspends it; the schedule is said in plain words ("every
  Sunday at 03:30"). Suspend, resume, delete (the copies stay). A schedule
  more frequent than once an hour is refused before Harvester refuses it.
- **VM Backups** (on the backup target) and **VM Snapshots** (in the
  cluster): state, size, target, the schedule that made them. Take one now,
  restore one into a new VM (optionally keeping the MAC addresses, which is
  refused on a network where the original still runs) or over the original
  VM (stopped; confirmation), optionally left stopped; delete.
- **Volume Snapshots**: restore one into a new volume; a volume snapshot
  taken with a VM snapshot is deleted with it, not alone.

Every gesture is an action followed in the dock, through
`harvester-resources backup|schedule|volsnap` on the command line. Verified
on harv1: a snapshot of a stopped VM (12 s), restored into a new stopped VM
(9 s), its volume snapshot restored into a new volume (bound), a backup to
the NAS (NFS), a weekly schedule created, suspended, resumed and deleted,
and everything deleted again.

Added in 1.68.0, what the window still lacked against Harvester:

- **Edit** a schedule: its frequency, the copies kept and the failures
  tolerated (the VM and the type stay, as in Harvester). The window starts
  from the current values; Harvester's own trigger follows the new rhythm.
- **File system freeze** when taking a backup or a snapshot: with the guest
  agent, Harvester freezes the VM's file systems during the copy, at most
  this long (5 s to 5 min; Harvester's default is 1 s). "0 s", no limit, is
  not offered: Harvester never calls the thaw itself, the deadline does.
  Harvester 1.9 and later; older versions ignore it, which the dock says.
- **Previous volumes** when replacing a VM from a backup: keep them, or
  delete them (confirmed separately: they cannot be recovered). A snapshot
  restore always keeps them, as Harvester requires.

Verified: on harv1 a snapshot with a 5 s freeze of a VM whose agent is
connected (Harvester froze its file systems, the VM did not stay frozen),
and a weekly schedule edited then deleted; on the three-node bench a VM
replaced from an NFS backup with its previous volumes deleted.

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

### The Harvester sections, one cluster at a time (1.57.0)

The sidebar groups, under Cluster, the menus operators know from Harvester,
for whichever cluster is selected:

- **Storage**: Volumes (the storage board: replicas, health, fixes),
  Images (source, size, state, storage class, the VMs using them) and
  Storage Classes (replicas, reclaim, binding, expansion, the image a class
  was made for, how many volumes use it).
- **Network**: VM Networks (one block per network), Cluster Networks,
  Load Balancers, IP Pools and Host Networks (1.65.0, see below), Overlay
  Networks (the kube-ovn VPCs and subnets, with their forms) and Underlay
  Networks (the fabric: physical cards, virtual switches, LLDP). These views left the
  Overview, which keeps Metrics and the Cluster board.
- **Add-ons**: every Harvester add-on, its chart and version, its state, and
  Enable / Disable for administrators, followed in the dock
  (`harvester-resources addon` on the command line). A refusal of Harvester
  is said plainly: on a single-node cluster, the descheduler "cannot be
  enabled as not enough nodes exist in the cluster". Verified on harv1:
  harvester-seeder enabled (deployed in 21 s), then disabled.
- **Security**: Secrets (type, key names, the VMs using them; values are
  never read into the page; the several hundred secrets the cluster uses
  for itself are hidden until asked for) and SSH Keys (fingerprint,
  validation, the VMs that received them).
- **Advanced** (1.67.0, see below): Settings (every Harvester setting,
  the backup target included), PCI Devices, USB Devices and SR-IOV Networks
  (1.68.0) and Support (Harvester's support bundles and kubeconfigs limited
  to a role).

Every list filters by words, sorts by any column (click the header, again to
reverse; the order is remembered), opens a row's details, and opens a VM
from its name. Lists refresh every 10 s while shown.

**Creating, changing and deleting (1.59.0)**, as Harvester's own menus
allow, each in a window that can stay open or be minimised, each gesture an
action followed in the dock (`harvester-resources create|delete|sc-default|
volume-expand|addon-values` on the command line):

- **Images**: a new image from an http(s) address, with its storage class
  (the default one proposed) and followed until imported; delete an image
  no volume uses.
- **Storage Classes** (administrators): a new class (replicas, stale replica
  timeout, data locality, disk and node tags, reclaim policy, binding,
  migration, expansion); make a class the default one (Harvester refuses a
  second default: the old one is unset first, and set back if the new one is
  refused); delete a class no volume or image uses.
- **Volumes**: a new volume, empty or from an image (its `lh-` class read
  from the image, the size at least the image's virtual size); expand a
  volume from its detail; the existing deletion of orphaned volumes stays.
- **VM Networks** (administrators): a new VLAN or untagged network on a
  cluster network, route automatic (DHCP) or manual; delete a network no VM
  uses, from the list of networks with no VM.
- **SSH Keys**: a new key, pasted or read from a .pub file, followed until
  Harvester validates it; delete.
- **Secrets**: a new secret with several keys; delete a secret no VM uses
  (the cluster's own secrets are never offered).
- **Add-ons** (administrators): the configuration (Helm values, YAML) in a
  window; saving redeploys an enabled add-on. Reading it is reserved to
  administrators too, since it can carry passwords.

Verified on harv1, each gesture checked with kubectl then undone: an image
imported by URL (28 s), a volume made from it and expanded from 1 to 2 Gi,
the image refused for deletion while used, then both deleted; a storage
class created, made default and back, deleted; an SSH key validated and
deleted; a secret with two keys (their names listed, never their values),
deleted; a VLAN 3999 network created and deleted; an add-on configuration
changed and restored.

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

### Hosts, as in Harvester (1.62.0)

In the Cluster view, a host's detail panel has **Configure...**, which opens
Harvester's host settings in a window, one tab per topic. Every change is a
tracked action run by `harvester-resources host <action>` (administrators).

Since 1.68.1 the window also has the tabs of a host's page in Harvester,
read only: **Basics** (IP, role, state, OS, kernel, container runtime,
kubelet, clock sync with a warning when the host is not in sync, UUID,
manufacturer, model and serial number when known, and three gauges: CPU and
memory in use against what VMs and pods can get, Longhorn space promised on
the host against its disks), **Instances** (the VMs running there, opened
from their name), **Network** (the cluster network configurations applied
to the host with their VLANs and state, and its network cards with the bond
or bridge they belong to) and **Events** (what Kubernetes reported about the
host).

- **General**: the name shown for the host, its console address (a link
  opens it), its labels (the system ones, including `cpumanager`, Rancher's
  and Longhorn's, are hidden and never touched) and its **host tags**, which
  storage classes use to place replicas.
- **Disks**: the disks Harvester's disk manager found. A whole, active,
  unmounted disk can be **added** to the storage (formatted unless it already
  holds ext4 or XFS; Longhorn V1, V2 or an LVM volume group); a storage disk
  shows its free, maximum and promised space, takes **disk tags** and can
  stop accepting replicas; **removing** one lets Longhorn move its replicas
  away first (Harvester refuses when it holds the only healthy copy).
- **Huge pages**: transparent huge pages of the host kernel (enabled, shared
  memory, defragmentation).
- **KSM**: merging of identical memory pages between VMs (stop, run, prune;
  standard, high or customized parameters; free memory threshold; merge
  across NUMA nodes). ksmtuned starts merging only when free memory falls
  under the threshold.
- **Out-of-band**: the host's BMC through the harvester-seeder add-on
  (address, port, user and password kept in a Secret, certificate check,
  hardware events). As in Harvester, **power off, power on and reboot**
  through the BMC are offered only for a host in maintenance.
- **Actions**: **enable or disable the CPU manager** (the static policy,
  needed for dedicated CPUs; Harvester restarts the node's Kubernetes agent,
  the VMs keep running; refused while a VM with dedicated CPUs runs there),
  and **delete the host** after typing its name (never the last node).

The seeder reaches a BMC's **Redfish on port 443** whatever the port given
(that port is IPMI's, 623), and **IPMI accepts passwords of 20 bytes at
most**; the window says both, and shows the seeder's connection error when
it cannot reach the BMC.

Verified on the three-node test cluster: names, labels and tags; a virtual
disk added, tagged, taken out of scheduling and removed; huge pages; KSM
running; the CPU manager enabled then disabled; the out-of-band access
through an IPMI emulator (virtualbmc), the host powered off and on through
it while in maintenance; a host deleted, then installed again into the
cluster. A physical BMC (iLO, iDRAC) has not been tried through the seeder
yet.

### Namespaces (1.62.0)

**Namespaces**, next to the namespace selector of the Virtual machines tab,
opens a window with the namespaces of the cluster: description, VMs,
volumes, snapshot quota, age. System namespaces are hidden by default and
cannot be deleted. **New namespace** (name, description, labels); **edit**
(description, labels, annotations, and the **snapshot quota** of the whole
namespace, kept in Harvester's `default-resource-quota`); **YAML**;
**delete** after typing the name, the window saying what goes with it.

### Rancher projects and quotas (1.72.0)

The **Namespaces** window has a **Project** column and a **Projects** tab,
as Harvester's Projects/Namespaces page under Rancher.

- A namespace's project is read in its `field.cattle.io/projectId`
  annotation, and compared with the cluster: a namespace whose annotation
  names a project of another cluster (an earlier import of the cluster into
  Rancher: 17 of them on harv1 at the time of writing) is flagged, since
  Rancher treats it as not in a project and applies no quota to it.
- Signed in through Rancher, the console reads the projects with your token:
  their names, their quotas and how much is used, the namespace default,
  the VM default limit. **New project** and **edit**: name, description,
  resource quotas (each line a project limit and the share of each
  namespace; CPU in cores or millicores, memory and storage in Gi or Mi),
  and the default limit given to VMs that set none. Rancher's own rules are
  checked before writing (the project limit and the namespace default go
  together, on the same resources, the default within the limit; requests
  within limits). Rancher's Default and System projects, and a project that
  still holds namespaces, cannot be deleted.
- **Move** puts a namespace in a project, or takes it out of any project
  (Rancher then removes its quota). A new namespace can be created directly
  in a project. In a project with quotas, a namespace's **edit** shows its
  own quota, only for the resources the project limits and within its
  limit: above it, Rancher would set the quota to zero and no VM could
  start. The console waits for Rancher to apply it.
- Everything goes through Rancher with your token, so Rancher applies your
  rights there. Signed in with a console account, the window shows the
  grouping from the annotations but cannot change it.

On the command line, with the kubeconfig of a Rancher session:
`harvester-resources project create|update|delete|move|ns-quota`.

### Rancher members of the cluster and its projects (1.73.0)

Under the cluster accounts (Settings, **Cluster accounts**), **Members in
Rancher** lists who Rancher gives rights on the cluster, with the role and
the provider (local, Keycloak...); in the Namespaces window, **Members** on a
project does the same for the project. A member is added by searching the
users and groups Rancher knows, then choosing a role of the context (cluster
owner, member, read-only or a finer role). Removing a binding is refused for
the last owner. Rancher's API does not list the bindings of its system
accounts, so they are never offered. Everything goes through Rancher with
your token: Rancher checks that you may grant the role. Signed in with a
console account, the block says to sign in through Rancher.

On the command line, with the kubeconfig of a Rancher session:
`harvester-resources member add|remove [--scope project --project p-xxxxx]`.

### Dashboard events and usage (1.62.0)

The Overview has an **Events** tab: the cluster events, grouped as on
Harvester's dashboard (hosts, VMs, volumes, images), with counts, warnings
marked, a "warnings only" filter and a search; refreshed every 20 seconds.
The Metrics tab shows **usage** gauges: CPU and memory measured now
(metrics.k8s.io) against the hosts' capacity, with what the pods and VMs
reserved; the Longhorn storage written, and what is promised to volumes
against what may be promised (over-provisioning included).

### Volume and image actions (1.63.0)

In the Storage section, a volume's panel has **Actions**, and each image row
has its own **Actions** button, with Harvester's menus. An action that does
not apply is greyed out and says why (a volume a VM uses, a cluster without
CDI, an image not ready or not on Longhorn v1). Each one is a tracked action
run by `harvester-resources volume|image <action>`.

- **Volume**: **clone** (with its data, Longhorn copies it; or an empty
  volume with the same size and class); **export to an image** (the volume
  of a running VM on Longhorn v1, otherwise stop it first); **take a
  snapshot** (with the class Harvester's `csi-driver-config` setting names,
  owned by the volume); **copy to another class** (Harvester's data
  migration: a CDI DataVolume, the original stays); **cancel an expansion**
  that cannot finish (the claim is recreated at its real size on the same
  volume, data kept); **description**; **delete** when no VM uses it.
- **Image**: **edit** its description and labels (Harvester fixes the rest);
  **clone** an image downloaded from a URL; **encrypt** into an encrypted
  storage class, or **decrypt**; **download** the file (gzip, as Harvester
  serves it; Longhorn v1 images); **create a VM** from it (the creation
  window opens with a disk made of the image, at the image's class and
  size).
- **Upload** an image file from the browser: the console keeps it, then
  offers it once to the cluster over HTTP (port 8092 by default, see the
  installation guide), whose nodes download it; the file is deleted
  afterwards. A **SHA512 checksum** can be given, here and when creating an
  image from a URL: Longhorn checks the file against it.

Verified on harv1: each volume action, an image edited, downloaded, cloned,
encrypted then decrypted with a throwaway encrypted class, a CirrOS image
uploaded from the browser (22 s), a VM creation opened from an image; the
cancel of a frozen expansion on the three-node bench.

### Templates, cloud configurations, storage classes, secrets, SSH keys (1.64.0)

- **Templates** (next to the namespace selector of the Virtual machines tab):
  each VM template with its versions, newest first, marked ready or not (its
  images imported) and default. **Launch** opens the VM creation from a
  version (its cloud-init is copied into a Secret of the new VM, never
  shared with the template, as Harvester does); **make default**; **delete
  a version** (not the default one) or the whole template; YAML. A version
  never changes: a new one is made from a VM's menu (Generate template).
- **Cloud configs**: Harvester's cloud-init templates (user-data and
  network-data, kept as labelled ConfigMaps): create, edit the text and the
  description, delete, YAML.
- **Storage classes**: the creation form offers the engine (Longhorn v1;
  Longhorn v2 when its data engine is enabled; LVM when its add-on is
  installed, with the node, the volume group and the type), **encryption**
  with the secret holding the passphrase (and online expansion of encrypted
  volumes), an allowed topology, the binding mode, the reclaim policy and a
  description. Encrypted Longhorn v1 classes were tried for real (a volume
  written and read by a pod); the LVM and Longhorn v2 choices were not, as
  neither is enabled on the test clusters.
- **Secrets**: created by type (Opaque, basic authentication, SSH key, TLS
  certificate, registry, and the **encryption** secret of the storage
  classes); **new values** for an existing secret (values are never shown;
  an empty field keeps the current one; an Opaque secret can lose a key).
  Values travel through a private file, never on a command line.
- **SSH keys**: **edit** the public key and the description; Harvester
  computes the new fingerprint.

### Networks, as in Harvester (1.65.0)

The Network section follows Harvester's Networks menu, in tabs: VM Networks,
Cluster Networks, Load Balancers, IP Pools, Host Networks, then the
kube-ovn Overlay and Underlay tabs. Every change is for administrators,
checked before it runs, and followed in the dock.

- **Cluster Networks**: one block per cluster network (its bridge, MTU,
  readiness) with its **configurations**: which NICs of which hosts form its
  bond (all hosts, one host, or hosts by labels), the bond mode, miimon and
  MTU, and the state on each host as Harvester's agent reports it. Only the
  NICs present and free on every chosen host are offered; the management
  NIC never is. A configuration can be edited, **moved to another cluster
  network** (Harvester's Migrate), or deleted; a cluster network is deleted
  once it has no configuration and no VM network. A running VM on the
  network blocks what Harvester would refuse (a new uplink, a host leaving),
  not a description change. The agent of a host may report a first error
  and then succeed: the console waits a minute before calling it a failure.
- **Storage, VM migration and RWX networks**: three tiles at the top of the
  tab, each on the management network or on a dedicated VLAN of a cluster
  network (range, excluded addresses; for storage, a VLAN kept for it; for
  RWX, shared with the storage network). The storage network is refused
  while a VM runs, as Harvester requires; the console then waits until
  Harvester says it is applied (it stops monitoring, waits for every volume
  to detach, and gives Longhorn the new network).
- **VM networks**: VLAN, untagged, and **trunk** (VLAN ranges like
  `100-199, 300`); a route (DHCP, or a manual network and gateway, with an
  optional DHCP server) and a description. **Edit** on a network's block
  changes its description and route, and its VLAN or trunk ranges while
  none of its VMs runs. Harvester probes the gateway of a known route; the
  result is shown.
- **Load Balancers**: in front of the running VMs of a namespace chosen by
  labels (`key=value[,value]` per line), with an address from **DHCP** or
  from an **IP pool** (fixed once created), listeners (port and port on the
  VMs, TCP or UDP), and an optional TCP health check. The list shows the
  address, the VMs behind and the state.
- **IP Pools**: ranges (subnet, first and last address, gateway), an
  optional VM network, priority and namespace (`*` makes the pool global).
  An address still held by a vanished load balancer is **released** from
  the pool's window.
- **Host Networks**: an interface `<network>-br.<VLAN>` on each chosen host,
  with DHCP or one static address per host (same subnet), optionally the
  underlay of the overlay networks. Harvester sets no route.

On the command line: `harvester-resources clusternetwork`, `netconfig`,
`vmnet`, `lb`, `ippool`, `hostnet` and `netsetting`.

Tried for real on the three-node bench: cluster network and configuration
created, MTU changed and restored, the configuration moved to another
cluster network and back, VLAN, trunk and untagged networks created and
changed (gateway probe answered), a static host network on the three hosts,
the migration network proven by a live migration, the storage and RWX
networks applied with the VMs stopped and restored, load balancers by DHCP
and by pool reached over SSH from outside, an orphan address released.

### Overlay and underlay networks: NAT, provider networks, policies (1.66.0)

Three windows complete the kube-ovn part of Harvester's Networks menu. Each
starts with the **health of kube-ovn**: the OVN database, the controller, and
the hosts where kube-ovn's network agent is not ready. (A host removed and
joined again once left the OVN database without quorum for hours, silently;
the windows now say so first.)

- **Provider networks** (Underlay tab, button of the same name): provider
  networks (kube-ovn takes a NIC of every host into a bridge `br-<name>`, with
  its addresses and routes; a NIC already bonded by Harvester, the management
  one included, is refused), their VLANs (0 is untagged), and **external
  networks**: the LAN side of NAT gateways, an underlay subnet at the real
  LAN prefix and gateway with a range of addresses nobody else uses. Only the
  first of the range can be picked by kube-ovn (the gateway's own address);
  external IPs take the next ones.
- **NAT & Internet** (Overlay tab): **NAT gateways** of a VPC (a pod in a VPC
  subnet at a LAN IP, going out through an external network; the console
  adds the VPC default route through it, which kube-ovn never does, and
  removes it with the gateway), **external IPs** (the next reserved address
  is proposed), **SNAT** and **DNAT** rules. kube-ovn freezes these objects
  once ready: they are deleted and created again, rules first, then the
  external IP, then the gateway, an order the console enforces.
  With kube-ovn before 1.16.1 (Harvester 1.8), a gateway's pod loses its
  own network to the tenant network and nothing comes back through it
  (kube-ovn issue 6632): the console repairs it when it creates the gateway,
  flags a gateway that broke again (kube-ovn rewrites it when its container
  restarts), and offers **Repair**. Harvester 1.9 ships a kube-ovn without
  the problem.
- **Policies** (Overlay tab): network policies aimed at VMs by name, with
  incoming and outgoing rules (anyone, a network, a namespace or some VMs,
  and ports), and a lax mode, on by default, that keeps kube-ovn's DHCP
  working. kube-ovn applies a policy to every kube-ovn interface of the VMs
  it targets, never to a NIC on a Harvester VLAN bridge. A policy written
  with selectors the form does not handle is changed in YAML.

On the command line: `harvester-network apply|delete --kind
provider|vlan|external|gateway|eip|snat|dnat|policy` and `harvester-network
state`.

Tried for real on the three-node bench: a provider network on each host's
second NIC, an untagged VLAN, an external network on the LAN keeping six
addresses; a VM in a VPC subnet reached over SSH from outside through an
external IP and a DNAT rule; a policy that cut it and, once changed, let it
through again; everything removed in kube-ovn's order.

### Harvester settings, support bundles and kubeconfigs (1.67.0)

The **Advanced** section, under Cluster, holds the rest of Harvester's
Advanced and Support menus.

- **Settings**: the forty-odd settings Harvester's UI shows, grouped
  (general, network, security, performance, backup, upgrade, support, UI),
  filtered by words or to the modified ones. Each row says whether the value
  differs from the default and whether Harvester **applied** it (for a
  setting with a controller: its hash annotation is up to date and its
  `configured` condition carries no error, which is shown). Edit opens a
  typed window (list, number with its bounds, yes/no, text, JSON, PEM); the
  value is checked beforehand as Harvester's webhook would (log level, hotplug
  ratio 1 to 20, overcommit of 100 % or more, NTP servers without `http://`
  and without duplicates, grace period, memory overhead ratio, Longhorn v2
  memory, JSON and PEM forms), then written and followed in the dock until
  Harvester applies it. **Reset** goes back to the default (value removed).
  Settings that can cut an access (automatic disk provisioning, which
  formats disks; proxy, registry and RKE2 certificate rotation, which
  re-apply the RKE2 plan on every node; the Rancher registration URL, whose
  removal deletes the Rancher agent; TLS certificate and options; external
  UI source) ask to tick "I understand the risk" first. The storage,
  migration and RWX network settings open Network > Cluster Networks, where
  their checks live; `server-version` is read-only; `ssl-parameters` is
  marked as having no effect in Harvester 1.9 (replaced by
  `traefik-default-tls-options`, which is shown).
- **Secrets never reach the page**: the TLS private key, S3 keys, registry
  passwords, proxy credentials and the token of the Rancher import URL (it
  gives the cluster agent's credentials) are shown as `•••`. Left as they are, the
  server puts the real ones back from the cluster when saving; S3 keys and
  registry passwords, which Harvester itself removes from the setting once
  applied, must be typed again.
- **Backup target**: an NFS or S3 form (endpoint, bucket, region, keys,
  virtual-hosted style, refresh interval) and **Test**, which asks Harvester
  whether it reaches the saved target (its own health check). Harvester
  connects to a new target before accepting it and refuses a change while a
  backup or a restore runs.
- **Support bundles** (Support tab): Harvester's own diagnostic archive.
  New asks for a description (required by Harvester), an issue link, extra
  namespaces and the timeouts; the collection is followed to its end, then
  the archive downloads through the API server and stays on the cluster
  until it expires or is deleted. The console's own anonymised bundle
  remains in the top bar.
- **Kubeconfigs** (Support tab): safer than Harvester's download, which
  hands out an administrator's credentials. Each kubeconfig is a service
  account bound to one chosen role (view, edit, admin, cluster-admin or
  Harvester's own roles), on the whole cluster or one namespace, with a
  token that expires (1 hour to 90 days). The form warns when the role
  reads Secrets: on Harvester, `view` does, because Harvester adds its own
  view rules to it, cloud-init data included. The file is downloaded once;
  the console keeps no copy and never logs the token. **Revoke** deletes
  the account: the token stops working at once.

Administrators only, for every change and for the whole Support tab (a
bundle holds the cluster's logs, a kubeconfig a token). On the command line:
`harvester-resources setting set|reset|test-backup-target`,
`harvester-resources supportbundle create|delete` and
`harvester-resources kubeconfig create|revoke --out <file>`.

Tried for real on harv1: the log level, the hotplug ratio (copied into
KubeVirt by Harvester) and the grace period changed and reset; the backup
target tested; a support bundle collected in under six minutes, downloaded
and deleted; a kubeconfig with `view` on `default` that listed the VMs, was
refused elsewhere, and stopped working as soon as it was revoked. On the
three-node bench: an NFS backup target set, reached by Longhorn, tested and
removed.

### PCI, USB and SR-IOV devices (1.68.0)

Three more tabs in **Advanced**, as in Harvester (they need the
`pcidevices-controller` add-on; without it the tabs say so and open
Add-ons):

- **PCI Devices**: every PCI device of every host, filtered by words, host
  or the ones in passthrough: address, description, vendor and device IDs,
  the driver in use (and the host driver it had), its **IOMMU group** (the
  whole group goes with it, said before enabling), the VMs using it.
  **Enable passthrough** detaches it from the host for VMs, **Disable**
  gives it back; one at a time or by selection. A device without an IOMMU
  group cannot be passed; one a VM uses cannot be given back.
- **USB Devices**: the same for USB devices (vendor, product, path).
- **SR-IOV Networks**: the SR-IOV network cards not taken by a cluster
  network, with the number of **virtual functions** they expose: enable
  with N, disable (refused while a virtual function is in passthrough); to
  change N, disable first. Each virtual function becomes a PCI device,
  marked as such, to pass like any other.

The VM editor offers PCI and USB devices in the same list, with their
passthrough state. A USB device takes the name of its USBDevice, which is
how Harvester knows a VM uses it.

A trap of Harvester 1.8, met on the bench: a device removed from a stopped
VM stays listed in the VM's allocation annotation, and Harvester then
refuses to give it back ("already in use with vm"). Harvester 1.9 rebuilds
that annotation from the VM; the console does the same before disabling,
and says so in the dock. A running VM's allocation always counts.

GPU pages (vGPU, SR-IOV GPU, MIG) are not shipped: no GPU Harvester
supports is on the benches, so nothing could be tried for real.

On the command line: `harvester-resources device pci-enable|pci-disable|
usb-enable|usb-disable --name <device> [--name ...]` and
`harvester-resources device sriov --name <card> --vfs N`.

Tried for real on the three-node bench (emulated devices: an e1000e card,
an igb card with SR-IOV, a QEMU tablet): the e1000e passed to a VM on its
host (the device is in the VM's libvirt domain), refused back while used,
given back to its driver after; two virtual functions created on the igb,
one passed then given back, SR-IOV refused off while it was taken, then
disabled; the tablet passed to a VM on another host and given back.

### Upgrading Harvester (1.69.0)

**Upgrade**, in the Overview header (and on the `server-version` setting, as
in Harvester), opens a window per cluster.

- **Before**: the current version, and two ways to go:
  - **a version**: a Harvester `Version` object. The list says which ones
    this cluster can reach, with the reason Harvester would give; Harvester
    itself only checks that after downloading the 8 GB ISO. A version is
    added from its published `version.yaml` (the console reads it) or
    deleted. When Harvester's version checker is on, it deletes the versions
    it does not offer itself at its next hourly check: start soon after
    adding one;
  - **an ISO of the console** (the Bare-metal store): the air-gapped path.
    The console reads the Harvester version inside the ISO, checks its
    SHA-512 (Harvester does not; the published `.sha512` file is used when it
    sits next to the ISO), serves it to the cluster through its token
    counter, waits for Harvester to import it, then starts the upgrade.

  Options: collect the upgrade logs (Harvester keeps them in a 1 GiB volume
  until the upgrade is dismissed; without rancher-logging it deploys its own
  collector), skip the check of detached single-replica volumes. The checks
  Harvester's webhook would refuse on are listed beforehand (another upgrade
  running or cleaning up, hosts not ready or cordoned, degraded volumes on
  three hosts or more, backups running, schedules not suspended, add-ons
  changing state, Harvester's charts not ready). The release notes of the
  target are linked, and "I have read and understood the upgrade
  instructions" is required, as in Harvester.
- **During**: each step with its time (logs, ISO downloaded with its
  percentage, repository, images preloaded, system services, hosts,
  completed), the state of every host (a host paused by the manual mode of
  `upgrade-config` can be resumed), what the new version brings. The start
  is an action followed to the end in the dock; the cluster API disappears
  while Kubernetes and the hosts restart, and the console keeps following
  (up to 45 minutes without an answer). **Follow** attaches a new action to
  a running upgrade, after a console restart for instance.
- **After**: success, or the cause of the failure (Harvester puts it in the
  reason of the Completed condition, or in the first failed step, or in a
  host); **Logs** packages and downloads the upgrade logs; **Dismiss** lets
  Harvester remove the log collector and its volume. **Abort** deletes the
  upgrade while Harvester still accepts it, before its hosts are touched
  (Harvester then cleans up, its logs and ISO image included).

Administrators only. On the command line: `harvester-resources upgrade
version-add|version-delete|start|follow|logs|dismiss|abort|resume-node`.

### Monitoring and logging (1.70.0)

**Monitoring & Logging**, in the Cluster menu, gathers what Harvester spreads
over its Monitoring and Logging pages, in four tabs.

- **Metrics**: without rancher-monitoring, what the cluster knows right now
  (metrics-server): each host's CPU and memory, and each running VM's CPU in
  cores and as a share of its vCPUs, and its memory. With rancher-monitoring,
  Prometheus adds the cluster's CPU, memory, disk and network, and per VM its
  CPU share, memory used, network and disk traffic. The VM CPU from Prometheus
  is the time the guest spends on its vCPUs (Harvester's own VM dashboards
  divide it by 1000 and show near zero); the current view counts the VM's
  container, QEMU included, and reads higher (on harvlab, 0.2 % against 3.8 %
  for idle VMs). The add-ons are one click away when monitoring is off.
- **Alerts**: the AlertmanagerConfig objects of the namespaces, with their
  receivers (webhook, Slack, email, PagerDuty, Opsgenie, Microsoft Teams) and
  their route (grouping, delays, matchers). A receiver's secret values are
  typed in the form and become a Secret of the namespace; only the reference
  is kept. The events Kubernetes records against a configuration (Alertmanager
  refusing it, for instance) are shown with it.
- **Flows**: Flow (one namespace) and ClusterFlow (whole cluster) objects of
  the logging operator, of three kinds as in Harvester: logging, audit (the
  API server's audit log, through Harvester's `harvester-kube-audit-log-ref`)
  and event (the Kubernetes events collected by Harvester's event tailer).
  The form only offers the outputs a flow may use (its namespace's outputs,
  the cluster outputs, audit outputs for an audit flow), with selection rules
  (labels, hosts, namespaces, include or exclude) and filters in YAML.
- **Outputs**: Output and ClusterOutput objects, with a form per target
  (Elasticsearch, OpenSearch, Loki, Splunk HEC, syslog, Kafka, forward, S3,
  HTTP, file, null). Secret fields work as for receivers. An output used by a
  flow cannot be deleted until the flow changes. A file output's path must
  contain `${tag}` (fluentd splits its buffer by tag); the form proposes one.

Every object shows the state the logging operator gives it: applied,
inactive (no flow uses it yet), problems (with the operator's words), pending.
Saving waits for the operator, then for fluentd's configuration check: when
fluentd refuses the new configuration, it keeps the previous one and nothing
changes in the log flow, so the console says it with fluentd's own error
instead of "saved". A failed check of a Logging is also shown above the
lists. Saving an unchanged object writes nothing.

Reading is open to every role; changing is for administrators. On the command
line: `harvester-resources monlog output-apply|output-delete|flow-apply|
flow-delete|amc-apply|amc-delete`.

### Importing VMs from VMware, OpenStack or an OVA archive (1.71.0)

**VM Import**, in the VM Import / Export section of the menu (1.77.0;
under Cluster before), drives Harvester's vm-import-controller
add-on (enable it in Add-ons first; the section says when it is off).

- **Sources**, one tab per provider:
  - **VMware**: the vCenter SDK address (`https://vcenter/sdk`), the exact
    datacenter name and an account allowed to export VMs;
  - **OpenStack**: the Keystone address, the region, the project's user,
    password, project and domain, and the upload retries;
  - **OVA**: the address of an `.ova` file over HTTP or HTTPS, with an
    optional user, password or CA certificate, and the download timeout
    (600 s by default, it covers the whole download: raise it for a large
    archive, 0 for no limit).

  Typed credentials become a Secret of the source's namespace (named
  `<source>-creds` unless you name it), with the key the controller expects
  for each type (the CA certificate key differs between the three); an
  existing Secret can be used instead. Each source shows whether the
  controller reached it (ready), could not (not ready), or never checked
  it (missing Secret, refused login). Harvester keeps the reason only in
  the controller's log: **Why** shows its error lines for that source. A
  ready source is never checked again by Harvester: **Check again**
  recreates it. A source used by an import in progress cannot be changed
  or deleted; deleting a source also deletes the Secret the console
  created for it.
- **Imports**: the VM to import (its name in vCenter, the server's name or
  ID in OpenStack, the name to give it for an OVA), the target namespace,
  the storage class of its disks, and the mapping of the source's network
  cards to VM networks. A card without a line is dropped; with no line at
  all the VM gets a single card on the pod network. Advanced: default card
  model and disk bus, skipping the checks on the source, and for VMware the
  folder, the guest shutdown timeout and a forced power-off (needed without
  VMware Tools). The form shows the name the VM will get in Harvester.

  What the controller would loop on without saying anything is refused
  before writing: a VM name that is not valid in Harvester, image names
  over 63 characters (`vm-import-<import>-<disk>`), a network mapped twice
  or to a network that does not exist, a VM of the same name not created
  by an import. Each import shows its step (checks, export from the
  source, images, VM created, VM running), the progress of each disk's
  image and a link to the VM once created. An import is followed to the
  running VM in the dock; one blocked without changing state for five
  minutes is ended with the controller's reason. Deleting an import once
  done leaves the VM and its images; before that, its images go with it
  and a VM already created stays.

Verified for real: an OVA imported end to end on harv1, and VMware sources
against vcsim (the vCenter simulator of the controller's own tests). The
export of a VM from a real vCenter or OpenStack is not verified: none is
available on the test benches.

Reading is open to every role; changing, and the controller's log, are for
administrators. On the command line: `harvester-resources vmimport
source-apply|source-recheck|source-delete|import-create|import-follow|
import-delete`.

### VMware migrations with Forklift: installation, VDDK image, vCenter provider (1.75.0)

Harvester 1.9 does not ship Forklift. `harvester-forklift install` puts
cert-manager (from the console's own Cluster API bundle), the experimental
forklift-operator add-on (chart 1.9.0, images `v1.8.2` by default,
`--image-tag` for another) and the ForkliftController on the cluster, in
that order; images are pulled from `registry.rancher.com/harvester` (plan a
mirror in airgap). If Harvester itself already carries a forklift-operator
add-on (expected from 1.9.1), the console enables it as it ships and never
rewrites its chart or its values. That path is not verified for real yet:
no bench runs a Harvester that ships this add-on.

**VMware migrations**, in the VM Import / Export section of the menu,
right after VM Import, opens
three tabs:

- **Preparation**, three steps in the order they must be done, each with
  its state and its button: Forklift (install or resume, with the parts
  read back: cert-manager, add-on, operator, controller, components, and
  the inventory access, the service account `harvester-ops-inventory`
  through which the console reads Forklift's inventory; it is set at the
  end of the installation, so an installation stopped earlier, or a
  Forklift installed another way, shows it missing and offers Resume); the
  VDDK image (VMware's archive is given once to the console's own store and
  serves every cluster; the target image is proposed from Harvester's
  `containerd-registry` setting, and Harvester's own credentials for that
  registry are reusable without being shown, Harvester 1.9 keeping them in
  a fleet-local secret, or credentials can be typed instead; the cluster
  remembers the last image pushed and the form offers it again); and
  vCenter sources, with a count of how many are ready and a link to the
  next tab. What is being typed in the VDDK step (image, registry account,
  archive choice) and an upload in progress are kept while the tab refreshes
  itself; Refresh reads everything again. A link from VM Import > VMware
  opens this tab.
- **vCenter sources**: one block per vSphere provider, its state
  (checking, ready, or refused with Forklift's message), its VDDK image if
  any, and how many migration plans use it. Adding a source can take a
  vCenter already declared in VM Import without retyping its password,
  read on the server; typing one instead asks for the address, a user, a
  password, and a certificate (CA certificate, or insecure). Changing a
  source never asks for the password again unless one is typed, and keeps
  the TLS setting unless it is changed. Deleting is refused while a
  migration plan uses the source. A new source never replaces an existing
  provider of the same name: the window says the name is taken. Every
  provider made by `harvester-forklift`, from the tab or on the command
  line, carries the console's label `harvester-ops.io/managed` and can be
  changed; a vSphere provider made by another tool is never changed (its
  Change button is disabled and `provider-apply` refuses its name), it can
  only be read and deleted. A window stays tied to the cluster it was
  opened for, even if the tab moves to another cluster before Save.
- **Inventory**, read-only: pick a source, then VMs, networks or
  datastores, search by name, and for VMs a filter on the ones that can
  move warm (Changed Block Tracking on). Each VM shows its CPUs, memory,
  disks, whether CBT is on, and Forklift's concerns translated to plain
  text (critical, warning, information).

On the command line: `harvester-forklift status|install|vddk-image|
provider-apply|provider-delete|inventory`. `install` takes
`--chart-version` and `--image-tag`, and either `--cert-manager-manifest`
or `--cert-manager-from-bundle` (the console's own Cluster API bundle).
`vddk-image` builds the init image from VMware's archive and pushes it,
registry credentials on stdin or in a private file (`--spec`); with
`--cluster` or `--kubeconfig` it also records the pushed image on the
cluster (ConfigMap `forklift/harvester-ops-vddk`), so a later form offers
it again. `provider-apply` reads its request as JSON (`url`, `user`,
`password`, `insecure` or `cacert`, `vddk_image`) from stdin or `--spec`,
sets the inventory access if it is missing, and refuses a name already
taken by a provider it did not make (another tool's vSphere provider, or
Forklift's own `host` provider); `provider-delete` refuses a provider that
is not a vCenter, and one a plan uses; `inventory` reads `vms`, `networks`
or `datastores` as Forklift sees them.

**Airgap.** The console pulls the VDDK base image
(`registry.suse.com/bci/bci-busybox:16.0`) itself, from its own host: in
airgap, set `HARVESTER_OPS_VDDK_BASE` in `/etc/harvester-ops/env` to a
mirror of it (`--base` on the command line). That base image, or its
mirror, is always pulled anonymously over HTTPS, trusted through the
system certificate authorities of the console's image: a mirror served
in plain HTTP, or one that needs credentials, is not supported yet. The
target registry (where the built VDDK image is pushed) is trusted the
same way through those authorities: a registry signed by an internal one
is not supported yet, but plain HTTP and credentials both work there.
Only the cert-manager manifest comes from the console's Cluster API
bundle; its images, like Forklift's, are pulled by the cluster: mirror
them. The cluster must also be able to pull from the registry the VDDK
image is pushed to (Advanced > containerd-registry).

Checked for real on the harvlab2 bench, against the nested vCenter of
vmwlab, from the tab: this was also the first real check of a VM Import
VMware source against a real vCenter, not only vcsim. Warm migration
waves, cutover, rollback and the global view came in 1.76.0 (next
section).

Reading is open to every role; changing is for administrators, and every
write (Forklift installed, the VDDK image pushed, a source added, changed
or deleted) is a tracked action, in the dock and in Activity.

### Warm VMware migrations: waves, cutover, rollback, global view (1.76.0)

A **wave** is a batch of VMs from one vCenter source migrated warm
together: Forklift copies their disks while they run, then takes
incremental copies (Changed Block Tracking) until the **cutover**, where
each source is shut down, the last copy made and the VM started on
Harvester. The downtime is limited to that last step.

**Preparation**, two more steps:

- **Disk importer (CDI)**, as step 2. The CDI importer shipped with
  Harvester (`registry.suse.com/suse/sles/16.0/cdi-importer:1.65.0`) lacks
  nbdkit's VDDK plugin: every VMware disk copy fails
  (harvester/harvester#11773, still there in 1.9.1-rc1 and rc2). The step
  says which image the cluster uses (SUSE without VDDK, upstream, or
  other) and offers to switch to the upstream importer of the same
  version (`quay.io/kubevirt/cdi-importer:v1.65.0`, or its mirror in
  airgap). The setting lives on the `harvester-system/cdi-operator`
  deployment (`IMPORTER_IMAGE`, `OVIRT_POPULATOR_IMAGE`); the original
  image is recorded once, as an annotation, and "Back to the original
  image" puts it back. A Harvester upgrade may bring its own importer
  back: check this step again after every upgrade.
- **Interval between incremental copies**, below the steps: in minutes,
  from 5 to 1440 (60 by default in Forklift). It applies to the whole
  cluster and every open wave (`controller_precopy_interval` of the
  ForkliftController); saving it restarts the Forklift controller, and a
  copy already scheduled is not rescheduled.

**Inventory**: a VMware Tools column (running or stopped) and a "Warm
migration" column saying whether the VM is eligible and, if not, why (CBT
off, VMware Tools stopped: without them the cutover cannot shut the
source down, VM already in another open wave). Eligible VMs can be
ticked, then **Compose a wave** opens the compose window.

**Compose a wave**: a name (lowercase, digits and `-`, 40 characters at
most), the target namespace, for each vCenter network these VMs use a
Harvester VM network or the pod network, for each datastore a storage
class, and two options: keep static IPs, and **raw copy** (no guest
conversion). Conversion is on by default; a raw copy shortens the
downtime but the guest must already have virtio drivers (most Linux
guests, not Windows): it is ticked by default when every VM runs Linux,
and a ticked Windows guest is flagged. A VM already in an open wave, on
this cluster or on another cluster declared in the console, is refused
(a VM is identified by its vCenter and its `vm-NN` id); an unreachable
cluster is named in the answer without blocking the compose.

**Waves**, a new tab: one block per wave, with its state (ready to start,
validating, refused by Forklift, copying, cutover scheduled, cutting
over, migrated, failed, back on the source, closed), its target
namespace, its VMs, the next copy and the scheduled cutover. The
actions:

- **Start**: a first full copy of the disks, then incremental copies at
  the set interval.
- **Cut over now** or **Schedule the cutover** (browser time); copies go
  on until then, and a scheduled cutover can be brought forward.
- **Back to the source**, for the whole wave or one VM, only once that
  VM's cutover has started: the console first checks it can reach the
  vCenter, stops the Harvester VM (found by its name, or by the `vmID` and
  `plan` labels Forklift sets), then powers the source on through the
  vCenter. Run again, it does not redo what is already done.
- **Close**: ends the wave, so its VMs can join another one; neither the
  Harvester VMs nor the sources are touched. When ticked, it also removes
  the `forklift-migration-precopy` snapshots Forklift leaves on the
  sources after a failed attempt (also possible later on a closed wave).
- **Delete**: removes the plan, its migrations and its maps; migrated
  VMs stay.
- **Follow** opens one window per wave: for each VM, the step
  (translated), the disk progress, the number of copies, how long the
  last one took and the time until the next, and the error with a hint
  when it is known (importer without VDDK, wrong VDDK image, VMware Tools
  missing).

A wave's objects (NetworkMap, StorageMap, Plan with `warm: true`,
Migration) live in the `forklift` namespace, labelled
`harvester-ops.io/managed` and `harvester-ops.io/wave`.

**Migrations (all clusters)**, the third entry of the VM Import / Export
section (1.77.0; next to Activity before): every VM of
every wave of every declared cluster, in one table (VMware VM, vCenter,
target cluster, wave, step, last copy, cutover), filtered by state and by
vCenter, with one block per cluster (Forklift installed or not, importer
image, sources, waves). An unreachable cluster is shown as such without
blocking the others; the view is read at most every 15 seconds per
person.

**Lanes** (1.80.0): the Waves tab switches between **Blocks** and
**Lanes** (the choice is remembered in the browser, Blocks by default).
Lanes put every wave of the cluster on one time axis, one lane per wave:
a line for now, a mark for each copy already made (the first full copy,
then the incremental ones; hovering a mark gives its start, end and
duration), the next copy, the scheduled cutover with its countdown, the
cutover window once it happened, and the wave state. The axis runs from
the start of the earliest wave to two hours from now (or 30 minutes
after the last scheduled cutover); the 6 h, 24 h, 7 days and Fit buttons
change the span. A maintenance window can be drawn on the axis (start
and end, kept in this browser only, per cluster): a visual aid, nothing
is sent to the cluster. Clicking a lane opens the wave's follow window.
Migrations (all clusters) offers the same lanes, one per cluster and
wave.

Before a wave is composed, started or switched over, the console checks that
no MAC address of its VMs is already carried by a VM of the destination
cluster (1.83.2). Harvester refuses a duplicate MAC even on a stopped VM, and
Forklift finds out only when it creates the VM, after the switchover has
stopped the source: the gesture is refused with the VM holding the address.
When the inventory cannot be read, the check is skipped and said so.

On the command line: `harvester-forklift wave-apply` (the wave as JSON on
stdin or `--spec`: `name`, `target_namespace`, `provider`, `vms`,
`networks`, `storages`, `skip_conversion`, `preserve_static_ips`),
`wave-start`, `wave-cutover [--at <RFC 3339>]`, `wave-status`, `waves`,
`wave-rollback [--vm vm-NN]`, `wave-close [--clean-snapshots]`,
`wave-delete`, `cdi-importer [--show|--upstream [--image <mirror>]|--original]`
and `precopy-interval <minutes>`. Rollback and snapshot removal talk to
the vCenter with the source's credentials (REST for power, SOAP for
snapshots, which the vCenter 8.0 REST API does not expose).

**Measured for real** on harvlab2, against the nested vCenter 8.0.1 of
vmwlab, Debian 11 with a 10 GiB disk: downtime of **6 min 24 s** with
guest conversion (about 4 min 30 of which is conversion), **1 min 44 s**
with a raw copy; rollback: 17 s to stop the Harvester VM, then the
source boot (about 5 min on this nested bench). Two waves run from the
console: immediate and scheduled cutover, raw copy and conversion, a VM
without VMware Tools and a VM already taken refused, rollback run again
with no effect, a close that removed three Forklift snapshots from the
real vCenter, the importer switched then recorded, the interval changed.

**Limits.**

- Without VMware Tools running in the guest, the cutover cannot shut the
  source down and fails; the inventory says so beforehand.
- The upstream CDI importer replaces a SUSE image: mirror it in airgap,
  and check again after every Harvester upgrade.
- The inventory reads Forklift at detail level 4 (tools, snapshots,
  UUID): heavier than before on a large vCenter.
- Refusing a VM already taken on **another** cluster is tested but not
  checked for real: only one bench runs Forklift.
- The cutover depends on the vCenter: on the nested bench a VM power-on
  took more than a minute to answer, hence a 5 minute wait before
  concluding it failed.

### LVM storage and downloading CDI images (1.74.0)

- **Images on a class outside Longhorn v1** (LVM, Longhorn v2, third-party
  storage) are now created as Harvester creates them: with the `cdi`
  backend. Until then the console asked for a Longhorn backing image
  whatever the class, and Harvester quietly put the image in a Longhorn
  class instead of the one chosen (seen for real on harvlab2 with an LVM
  class). Uploads from the browser follow the same rule.
- **Downloading a CDI image**: Harvester first copies the volume into a
  compressed qcow2 file, through a temporary downloader. **Download** says
  so, follows that preparation as an action, then fetches the qcow2 file;
  Harvester removes the downloader afterwards. A Longhorn v1 image is still
  downloaded straight away, gzip-compressed.
- **Verified for real** on harvlab2 (Harvester 1.9.0, Harvester's
  experimental LVM add-on, a spare virtual disk): a disk given to LVM from
  the host window (volume group active), an LVM storage class created by the
  form, an image imported on it as a CDI image with its LVM volume, and
  downloaded as qcow2.

On the command line: `harvester-resources image prepare-download|download`.

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

### Creating a cluster (1.48.0, window with menus in 1.53.0)

The **Create a cluster** button of the K8S Clusters tab opens a window that
can be minimised to the window bar, to check a setting elsewhere, and
reopened as it was. It is organised in menus, like the creation of a VM,
and filled from the cluster itself:

- **Essentials**, the opening menu, enough to create: the name, the
  Kubernetes version (versions created for real with the bundle are marked
  "tested"), 1, 3 or 5 control plane nodes, the number of workers, a small /
  medium / large preset, the image (ISOs and images still downloading are
  left out, SUSE images first, the last one used is remembered), the key
  pair, the VM network with its VLAN and the IP pool with its ranges and
  free addresses.
- **Nodes**: custom CPU, memory and disk, the SSH user suggested from the
  image's system.
- **Network**: the gateway and mask taken from the pool (and marked as
  such until changed), the DNS server (remembered), extra IP pools and
  networks.
- **Storage**: a data disk and its storage class.
- **Kubernetes**: namespaces of the cluster objects and of the VMs, the
  CNI, pod and service CIDRs.
- **Integrations**: import into Rancher, Fleet add-ons with MTU,
  encapsulation and BGP.
- **Pre-check**: the details of the pre-check; each menu shows the number
  of findings that concern it, and the action bar sums them up whatever the
  open menu.

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

### Services on the created clusters (1.52.0)

The Services tab deploys Helm charts on the clusters created by Cluster
API, through the Cluster API add-on provider for Helm (CAAPH v0.6.4):

- **The provider** is in the airgap bundle and installed from the
  Installation tab with the others. It is optional: without it clusters can
  still be created, and the Services tab says what is missing.
- **A catalog ready to use**: a DNS server (CoreDNS with upstream resolvers
  and local records, reachable on a load balancer address), a test
  application (podinfo, a page that tells which cluster serves it), and any
  Helm chart (repository, chart, version, values).
- **A form** fills in the settings, proposes the tested chart version and
  shows the values the chart will receive. The pre-check runs as you type:
  provider missing, cluster unknown, invalid name, namespace, repository or
  YAML, a version left floating, a service that exists and will be updated.
- **Deploying** declares a `HelmChartProxy` next to the cluster and labels
  the `Cluster` so that the service selects it; CAAPH installs the chart,
  and the action follows the release (one `HelmReleaseProxy` per cluster)
  until it is ready. **Removing** a service deletes it: CAAPH uninstalls the
  release from each cluster, and the label goes away.
- **The service address** is given to the load balancer of the created
  cluster by the DHCP of the VM network, or taken from a Harvester IP pool.
  It is announced by **kube-vip**, which CAPHV does not install: the
  console adds it to every cluster it creates (from the Harvester cloud
  provider chart, on the control plane nodes), and to an older cluster
  with its first service; the pre-check says so.
- **Not yet**: charts and their images come from the Internet (the
  management cluster fetches the chart, the created cluster pulls the
  images); serving them from the bundle comes later. DHCP and NTP servers
  are not in the catalog.

### Bundles and CLI

- Timestamped airgap bundles with an active marker, inspect, upload,
  download, and a Harvester version compatibility check.
- `harvester-capi status | install | cleanup-legacy | inventory | check |
  render | create | delete | services | service-check | service-deploy |
  service-remove`, each with `--cluster` or `--kubeconfig`; `create` and
  `service-deploy` exit 2 when the pre-check blocks. The manifests are rendered by
  `caphv-generate`, shipped with the console (taken from CAPHV at a fixed
  commit, see `bin/caphv-generate.PROVENANCE`).

## 5. Terraform — infrastructure as code (console)

Drive the Terraform provider for Harvester from saved declarations.

- **Declarations**: named groups of resources (VMs, VM images, SSH keys,
  Terraform code written by hand) applied together, each with its own
  Terraform state (1.54.0). They are kept by the console (shared between
  operators, backed up with it); those a browser still held are imported at
  the first visit.
- **One view (1.55.0)**: the list of declarations on the left, each with its
  state (never applied, up to date, N to apply, changed, error); the chosen
  declaration on the right, renamed in place (the name is unique in the
  cluster, nothing deployed moves) and described. Three tabs:
  - **Resources**: one card per resource with its summary, its Terraform
    address and its state (to create, deployed, N settings to change, to
    replace, removed and destroyed at the next apply, incomplete);
  - **Code**: the Terraform code the declaration produces, file by file,
    and an export as `.tf`;
  - **History**: its plans, applies and destructions, who ran them and
    what changed.
- **Forms in the view**: a resource opens section by section (General,
  Disks, Networks, First boot for a VM) with readable labels in the five
  languages, the Terraform name in small print and a tooltip on every
  field, and a check as you type. Saving does not touch the cluster.
- **A plan read before applying**: "Preview the plan" computes, on this
  declaration only, what would be created, changed (setting by setting,
  before and after), replaced (and why) or destroyed, sensitive values
  masked. "Apply this plan" applies exactly that plan; if the declaration
  changed since, the console refuses and asks for a new plan. The last
  plan stays available from the "Apply" button.
- A resource removed from a declaration is destroyed by its next apply
  (the plan says so); a resource destroyed on its own from the cluster
  resources tab leaves its declaration. Destroying a declaration asks for
  its name to be typed; a declaration with deployed resources cannot be
  deleted. Two operators editing the same declaration do not overwrite
  each other: the later one is told and sees the current version.
- A destroyed VM takes its disks with it unless "delete with the VM" is
  unchecked on the disk (1.52.1).
- **Cluster resources**: everything Terraform manages on the cluster,
  declaration by declaration; a resource of the shared workspace (applied
  before 1.54) can be adopted into a declaration, which takes it over at
  its next plan without recreating it.
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
  install disk, management NIC, addressing, VIP, DNS, NTP, token, OS
  password; the browser's password manager never fills these fields), and the console remasters the ISO for zero-touch install,
  publishes it behind a one-off token, mounts it as virtual media, sets a
  one-shot `Cd` boot and powers the machine on. Tracked step by step in
  the dock, from preflight to the Harvester API answering on the VIP.
- **The complete installer configuration (1.77.0).** Beyond the basic
  fields, the window sets:
  - a management interface bonded over several NICs (discovered cards
    ticked by MAC, or names), the bond mode, miimon, and for 802.3ad the
    LACP rate; the transmit hash policy in 802.3ad, balance-xor,
    balance-tlb and balance-alb; an optional VLAN;
  - a data disk and "wipe all disks";
  - node labels (one `key=value` per line) and kernel modules;
  - an **advanced YAML** section for everything else the installer
    accepts: `os.write_files` (NetworkManager connections of the other
    networks, systemd drop-ins, sshd settings), `os.persistent_state_paths`,
    `os.sysctls`, `os.environment`, `system_settings`, and so on.

  **Import a configuration** reads an existing installer file: the fields
  the form knows fill it, the rest goes to the advanced YAML, the file's
  token and password stay on the server (the fields say "taken from the
  file"), and its `iso_url` is replaced by the image the console serves
  to the BMC. **Preview** shows the exact YAML the installer will get,
  secrets masked. Every key is checked against the installer's schema
  (Harvester v1.9) before anything is powered on, and refused with its
  path when it is unknown, badly typed, set twice (by the form and the
  advanced YAML), or owned by the console (`install.iso_url`,
  `install.automatic`, `install.mode`, `server_url`, `token`, `os.password`). A
  `system_settings.ntp-servers` is refused while the NTP field is set:
  the installer would replace it with the field without a word (seen on
  a real install).

  Checked for real on a nested node of Harvester v1.9.0 installed with a
  configuration shaped like an operator's: management bond of two NICs
  (active-backup), storage bond in MTU 9000 with a VLAN and a static route
  written by `write_files`, data disk taken as Longhorn's default disk,
  node labels, modules, a persistent path, a sysctl, all still in place
  after a reboot. Not checked for real: LACP (802.3ad, no switch on the
  bench negotiates it) and a tagged management VLAN.
- **Disks, chosen and checked (1.78.0).** A discovery boot reads what
  Linux sees on the machine (disks, stable links, partitions, NICs), even
  when the BMC publishes nothing (iLO 4); the window then shows a table of
  disks with a role each (system, data, pool, wipe, ignore), writes stable
  paths, and the server refuses a disk too small, holding data without its
  wipe, or used twice. Several **disk pools** (storage tiers) are created
  right after the install, each with its StorageClass, on any Harvester.
  The new cluster is **declared in the console automatically**, with its
  own SSH key. The installer powers off at the end and the console boots
  the disk itself, so a BMC that ignores one-shot boots no longer loops.
  Checked end to end on a nested node driven through a Redfish emulator
  (five disks on virtio, SATA, SCSI and NVMe); on a physical blade, the
  install path was last checked in 1.19 (iLO 4).
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

### Signing in through Rancher (1.50.0)

When the console is declared in Rancher Manager (2.12 or later, see the
install guide), its sign-in page offers **"Sign in with Rancher"** next to
local accounts. Someone already signed in to Rancher enters without typing
anything; otherwise Rancher shows its own sign-in page, local account or
Keycloak alike.

- **Rights come from Rancher, not copied**: every action on a cluster goes
  through Rancher's proxy (`/k8s/clusters/<id>`) with that person's own
  token, so Rancher applies their user, group and project rights. Verified:
  a Rancher "cluster member" was refused by harv1 itself
  (`User "u-t286c" cannot get resource "virtualmachines"`), the
  administrator was not.
- The console finds which Rancher cluster is which by comparing the UID of
  `kube-system` on both sides, or the UIDs of the nodes for someone who
  cannot read `kube-system` (a Rancher "cluster member", 1.56.0), or
  `rancher_cluster` in the configuration. Once a cluster is known, Rancher
  is still asked, account by account, whether that person may open it.
  Clusters Rancher does not show to that person are left out, and any
  request naming one is refused.
- **Console role**: administrator for Rancher administrators and the groups
  listed in `admin_groups`, `default_role` (operator) for the others.
- **Stays with local accounts**: starting and stopping a cluster (a stopped
  cluster no longer goes through Rancher, and Rancher may run on the cluster
  being stopped), and the support bundle for non-administrators (it reads
  every cluster with the console's own account).
- **Tokens never leave the server**: the browser holds a random session id
  (HttpOnly cookie); the access token is renewed before it expires and
  written to the session's token file, which its kubeconfigs point to
  (`tokenFile`). kubectl reads it at each call and a running client (the
  Terraform provider) rereads it every minute, so an apply that outlasts a
  ten-minute token keeps working (1.56.0).
  Signing out forgets the session; Rancher does not let an OIDC token revoke
  itself, it expires at the session length. Signing out of Rancher also
  ends the console session at its next renewal.

### Rancher set in the interface, direct sign-in, Harvester RBAC chart (1.79.0)

Settings > Sign-in through Rancher (administrators) sets one or more
Rancher, at once and without a restart; the `rancher:` section of
`config.yaml` still works and shows there read-only.

- **Test** reads Rancher's version (`/rancherversion`) and its
  authentication providers (`/v3-public/authProviders`), and says which
  ones take a password.
- **Direct sign-in**: user name and password of a password provider (local,
  LDAP, OpenLDAP, Active Directory, FreeIPA), sent to Rancher, never kept.
  The Rancher token obtained is used like the single sign-on one (Rancher's
  proxy, Rancher's rights, `default_role` in the console, administrators of
  Rancher are console administrators). It lasts the session length, is not
  renewed, and is deleted in Rancher at sign-out (`POST
  /v3/tokens?action=logout`: verified on Rancher 2.14.1, a token cannot
  `DELETE` itself). A refusal does not say whether the account exists.
- **Single sign-on registration**: with a Rancher administrator's
  credentials, asked once and not kept, the console creates its
  `OIDCClient`, reads the generated secret from
  `cattle-oidc-client-secrets` and keeps it (0600), then deletes the
  administrator's token. Unregistering deletes the client in Rancher.
- **Harvester RBAC chart**: state (absent, installed, version), and
  installation in Rancher's `local` cluster as a tracked action, refused
  with the reason when the chart's Rancher or Kubernetes requirement is not
  met. The new roles are listed at the end.
- Nothing secret is ever returned to the browser: no client secret, no
  password, no token, no file path.

### Signing in is mandatory (1.57.0)

There is no open console any more: without a session, every page leads to
the sign-in page (never a browser password prompt), and every API call is
refused. Local accounts sign in with a form (an HttpOnly session cookie,
twelve hours); a write carried by a session cookie must come from the
console itself. With no account at all, the first start creates the first
administrator with a token read on the server's disk (see the install
guide). Administrators manage the console accounts in Settings > Console
accounts (create, role, reset a password, delete; the last administrator
cannot be removed); everyone changes their own password from the account
menu; resetting a password or deleting an account closes its sessions.

### Your account and signing out (1.56.0)

The account button, top right next to the settings, opens a menu that says
who is signed in and how (Rancher, local account, or an open console with
no sign-in), the console role and what it allows, what the clusters see
(Rancher rights, a delegated cluster identity, or the shared kubeconfig),
when a Rancher session ends and its groups. It leads to the cluster
accounts, the language and the version history, and signs out:

- a Rancher session is forgotten by the console;
- a local account signs out too, although HTTP Basic authentication has no
  session: the browser keeps the password and resends it, so the console
  makes it remember a placeholder account instead, accepted on that single
  path (`/logout/local`). The next page asks for the password again.
  Verified in Chromium.
- an open console has nothing to sign out of, and the menu says so.

### What the cluster refused (1.56.0)

A view that a cluster (or Rancher) only partly lets someone read no longer
shows an empty list without a word. The reads that RBAC refused travel with
the answer (`X-Cluster-Denied` header, verb, resource, API group and
namespace) and a notice above the page lists them, grouped by resource,
with what to ask for: a role on the cluster or on the project from a
Rancher administrator, or a grant for the delegated identity. A Rancher
session also learns why a cluster of the console is missing: Rancher gives
the account no access to it, or no cluster Rancher shows the account is
that one. Seen for real with a Rancher "cluster member" on harv1: the
overview showed 0 nodes and the cluster list was empty; it now shows the
node, and says that virtual machines and Longhorn volumes are refused. A
refused kind no longer hides the permitted ones in the status of a cluster
either (the grouped read is retried kind by kind).

## 9. Cross-cutting

- **Version history.** Clicking the version number (sidebar, account menu,
  Settings > About) lists what each version brought, from the release
  notes shipped with the console: newest first, the installed one marked,
  filtered by words, or reduced to additions or to fixes.
- **Update from the interface (1.82.0).** The same window has an Update tab:
  check the online source (GitHub releases or an internal mirror) and
  download, or give an archive and its signature for an air-gapped site, then
  install. The host's update agent checks the signature, installs, restarts
  the console and puts the previous version back by itself if the new one
  does not answer. See [install.md](install.md#updating-harvops-1820).

- **A sidebar that gives the screen back.** The left menu is a 56 px rail
  of icons that expands over the page while the pointer is on it, and
  collapses when it leaves. It is a layer, not a column: opening it never
  resizes the work area, so the topology detail panel, canvases and tables
  do not jump under the operator. Pin it open from its footer when you want
  the labels permanently: pinned, it becomes a column and the page, the dock
  and the window bar move over once to make room, so nothing stays hidden
  under it. The pin is remembered.
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
- **Shared reads and sizing (1.81.0)** - screens that refresh on their own are
  read once for everyone with the same identity and role, and forgotten at
  every write; measured figures and recommended resources in
  [sizing.md](sizing.md).
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
