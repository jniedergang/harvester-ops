# Bare-metal: installing Harvester on a blank machine

The Bare-metal tab turns an empty server into a running Harvester node
without anyone walking to the rack, plugging in a USB stick, or clicking
through the installer. The console drives the machine through its BMC.

Nothing here is Harvester-specific magic: it is the vendor's own
zero-touch installation, driven end to end by the console.

---

## What has to be true before you start

| Requirement | Why | How to check |
|---|---|---|
| The BMC speaks Redfish | Everything is driven through it | Discovery lists the node |
| The BMC exposes a **CD virtual media** device | The ISO is mounted over the network | The node card offers **Install Harvester**; otherwise it shows *no virtual media* |
| The BMC can boot from `Cd` | The machine must come up on that ISO once | Same badge as above |
| The console host is reachable **from the BMC** | The BMC downloads the ISO from it | `curl` the console host from another machine on the BMC network |

On HPE iLO 4/5, virtual media is an **iLO Advanced** feature. Without
that licence the BMC exposes no CD device and the console will say so
rather than fail thirty seconds into an installation.

The node card also reports the number of disks the firmware can boot
from. If it reports none, the machine has no usable install target and
there is nothing to install onto.

---

## The flow

```
console (your server)                          target machine
  official Harvester ISO ──remaster──► patched ISO
        │                                      │
        │  plain HTTP, one-off token
        ▼                                      ▼
  /pxe/iso/<token>.iso     ◄──── BMC: insert virtual media (URL)
  /pxe/config/<token>.yaml ◄──── installer fetches its answers
        │
        └─ Redfish: boot once on Cd, power on, install runs unattended
```

### 1. Get an ISO into the store

**Installation images** downloads an ISO **server-side, as a stream**.
The file never passes through your browser: a Harvester ISO is around
7.6 GB, and an upload of that size through a browser would be spooled in
memory on the way in.

Paste the vendor URL, press Download, and follow the percentage in the
dock. The store shows what is on disk and how much room is left.

The ISO is deliberately **not** part of the release tarball: the whole
toolkit is 135 MB, the ISO alone is fifty times that.

### 2. Discover the BMC

Enter one or more BMC addresses, a username and a password. The
credentials are **never stored on the server**: they stay in the page for
the length of your session, and every subsequent action re-sends them.

Each reachable BMC comes back as a card: model, serial, BIOS, memory,
NICs with their MAC addresses, current power state, and whether the
machine can be installed.

> **Careful with a powered-off machine.** A BMC does not re-read hardware
> while the server is off: it replays the inventory of the **last POST**.
> On a machine that has not booted in months, that inventory can be
> months stale. The installation runs a preflight that powers the machine
> on and reads the real thing before deciding anything.

### 3. Fill in the node

**Install Harvester** opens a form:

- **Image**: which ISO from the store.
- **Node**: hostname, addressing (static or DHCP), IP / mask / gateway,
  cluster VIP and its mode.
- **Management network**: one or several NICs, ticked among the ones just
  discovered (several make a bond), the bond mode and miimon, the LACP
  rate in 802.3ad, the transmit hash policy in the modes that use it, an
  optional VLAN.
- **Disks** (1.78.0): once the machine has an inventory (discovery boot,
  below), a table of its disks: size, model, serial, media, bus,
  partitions already there, and the stable path the console will write
  (`by-path`, else a `by-id` link to the disk itself, never a multipath
  `dm-*`; a bare `sdX` only when nothing else exists, and flagged). Each
  disk gets a role: **system**, **data** (Longhorn's default disk),
  **pool** with a tag, **wipe only**, or **ignore**, and its own **Wipe**
  box (`install.wipe_disks_list`), next to "wipe all disks". Without an
  inventory, the install disk is typed in (a stable
  `/dev/disk/by-path/...` is best). **Read the disks** shows what the BMC
  publishes over Redfish (controllers, RAID or pass-through, volumes,
  drives), read only.
- **Cluster name** (create mode): the name under which the console
  declares the new cluster, the hostname by default.
- **System**: DNS, NTP, node labels (one `key=value` per line), kernel
  modules.
- **Access**: the cluster token, the OS password, and optionally SSH
  public keys.
- **Advanced YAML**: anything else the installer accepts, merged with the
  form: `os.write_files` (NetworkManager connections of the storage or
  other networks, systemd drop-ins, sshd settings),
  `os.persistent_state_paths`, `os.sysctls`, `os.environment`,
  `system_settings`...

**Import a configuration** takes an existing installer file and splits it:
what the form shows fills it, the rest goes to the advanced YAML. The
file's token and password stay on the server for 15 minutes (the fields
say "taken from the file"), its `iso_url` is replaced by the image the
console serves (the window says so), `install.automatic` is dropped (the
console always installs unattended), and a file without `bond_options`
gets `active-backup`, as the installer would. **Preview** shows the exact
YAML the installer will read, secrets masked.

Every key is checked against the schema of the Harvester v1.9 installer
before anything is powered on. A key is refused, with its path (for
example `os.write_files[2].contnt`), when it is unknown, of the wrong type,
set both by the form and the advanced YAML, or kept by the console
(`install.iso_url`, `install.automatic`, `install.mode`, `server_url`,
`token`, `os.password`, `install.power_off`). A `system_settings.ntp-servers` is refused while the NTP
field is filled: the installer rewrites that setting from the NTP servers
and the other value would be lost without a word.

With an inventory, the server also checks the disks before anything is
powered on, and refuses with the disk and the reason: a role given twice,
no system disk, a disk too small (installer v1.9: 250 GiB for a system
disk alone, 180 GiB with a data disk, 50 GiB for a data or pool disk;
`harvester.install.skipchecks=true` lifts sizes), a disk that holds
partitions or a filesystem without its Wipe ticked (or wipe all), a pool
tag that is not lowercase letters, digits and `-`, a disk the inventory
does not know. The system and data disks are formatted by the installer:
ticking their Wipe clears the check, and they are left out of the list
sent to the installer.

The token and the password never appear in a response, in an action
label, or in a log line.

### 4. Watch it install

The installation is a tracked action like any other: it appears in the
dock the moment you confirm, and streams its steps.

| Step | What happens |
|---|---|
| `preflight` | Powers the machine on if needed, waits for POST where the BMC publishes it (HPE), checks there is a disk: the discovery inventory first, then the HPE UEFI boot targets, then the Redfish drives; with none of them, a warning (the installer checks its disk itself) |
| `remaster` | Patches the ISO so it installs unattended (below) |
| `serve` | Publishes the ISO and the config behind one-off tokens |
| `bmc-insert` | Mounts the ISO as virtual media, and waits for the BMC's answer (up to 15 min, `HARVESTER_OPS_BM_INSERT_WAIT`) |
| `bmc-boot` | Sets a **one-shot** boot on `Cd` |
| `power` | Powers the machine off, then on, onto the ISO: a cold boot, since some BMCs attach the virtual media or apply the one-shot boot only at power-on |
| `wait-install` | The installer powers the machine **off** when it is done (`install.power_off`, set by the console) |
| `boot-disk` | Ejects the media, sets a one-shot boot on the disk, powers the machine on |
| `wait-api` | Waits for the Harvester API on the VIP |
| `declare` | Reads the new cluster's kubeconfig over SSH and declares the cluster in the console |
| `pools` | Creates the disk pools (only when some are asked) |

Why the power-off (1.78.0): some BMCs do not honour a one-shot boot and
keep the virtual CD first; the machine then came back to the installer
after every install, in a loop (seen on a Redfish test bench). With the
installer powering off, the console knows the install is over and boots
the disk itself, whatever the BMC does with one-shot boots.

If the run fails before the install starts, a machine the preflight
powered on is powered off again; a machine found running is left as it
was.

**The new cluster is declared automatically** (create mode). The console
creates an ed25519 key pair for it (`ssh/<name>_id`, 0600, in the
console's state directory, `/var/lib/harvester-ops` in the packaged
service, where Settings > Clusters keeps the clusters it declares),
puts the public key in the install config, reads
`/etc/rancher/rke2/rke2.yaml` over SSH as `rancher` through the VIP
(host key recorded on first contact in `ssh/<name>_known_hosts`), writes
it 0600 with its server set to `https://<VIP>:6443`, and declares the
cluster with that kubeconfig (`kubeconfigs/<name>.yaml`), that key and the
node in `clusters.d/<name>.yaml` of that same directory, never in the
read-only `config.yaml`. The paths in the declaration are relative to the
state directory, so copying it moves the cluster with the console (see
"Moving harvops to another host" in the install guide). The key stays: the
console uses it for the graceful shutdown and startup. A node that joins
this cluster later gets the same public key.

**Disk pools** are created right after the API answers, with what
Harvester already offers after an install: each pool disk is found among
node-disk-manager's BlockDevices by its serial or WWN (never by its kernel
name, which can change between boots), provisioned into Longhorn,
formatted and tagged with the pool, and each pool gets a StorageClass
`longhorn-<tag>` (`diskSelector: <tag>`, replicas as asked, 1 by default,
a warning when more than one is asked on a single node). An existing class
with another selector is refused before any disk is formatted. A disk that
is the system or data disk, or that carries the system partitions, is
never taken. In join mode the disks are provisioned and the classes are
left as they are. The same thing on the command line:
`harvester-resources pools-apply --cluster <c> --node <n> --spec <json>`
(and `host disk-add --tag`).

### Adding a node to an existing cluster (API only)

The tab creates a new cluster. `POST /api/baremetal/install` also takes
`"mode": "join"` with the address of the cluster to join
(`"server_url": "https://<VIP>:443"`) instead of a VIP. That cluster must be
declared in the console: the action ends when the new node is **Ready** in
it (step `wait-node`), not when an API answers, since the cluster's API was
already up. The configuration generated for a join was checked by
installing two nodes into a three-node test cluster; the Redfish-driven
flow in join mode has not been run on real hardware yet.

### Reading the disks first: the discovery boot (window, API and CLI)

Some BMCs publish no disks at all (HPE iLO 4 publishes none), so the console
cannot tell which disk to install on. `POST /api/baremetal/discover`
(`bmc_host`, `bmc_user`, `bmc_password`, `iso`, optional `extra_args`) boots
the Harvester ISO once, with a small script of the console, and waits for
the machine to send back what Linux sees: disks with their stable
`by-path`/`by-id` links, partitions, NICs. The machine then powers itself
off. `GET /api/baremetal/inventory/<bmc_host>` returns the parsed inventory
(`source`, `at`, `system_serial`, `disks`, `nics`), or 404. In the install
window, **Discovery boot** runs it as a tracked action and fills the disk
table when the inventory arrives; the management NICs then show their
Linux names.

- The script sits on the ISO itself (`/discover.sh`, mounted at
  `/run/initramfs/live`); the kernel line carries only its path
  (`systemd.run=`) and never `harvester.install.*`: nothing is installed.
  It carries no secret, only the upload address, whose token accepts a
  single upload of 1 MiB at most, and only while a discovery runs.
- Each discovery remasters its own ISO (about a minute) in the console's
  work directory, with a fresh upload token, serves it for that run only
  and deletes it at the end: allow the size of the ISO in free space.
- The script also sends the machine's DMI serial and UUID. The console
  compares them with the serial (and UUID) the BMC gives: an inventory
  from another machine is refused and not stored, both values in the
  message. When the machine reports only filler values (common on VMs:
  empty, "Not Specified"), the UUID is used; if nothing can be compared,
  the inventory is stored with a warning step. The install checks this
  again before powering anything on: if the BMC now leads to another
  machine (blade swapped, BMC readdressed), it stops and asks for a new
  discovery, rather than choosing disks from another machine's inventory.
- One action at a time per BMC: a discovery or an install on a BMC that
  another discovery or install is driving is refused (409).
- Waits: 15 min for the inventory, then 5 min for the machine to power
  itself off; after that it is forced off and the step says so. The
  virtual media is ejected in every case (a failed eject is reported as a
  warning step: eject it from the BMC).
- Inventories are kept per system serial (read over Redfish), mode 0600,
  under `~/.local/share/harvester-ops/inventory`
  (`HARVESTER_OPS_INVENTORY_DIR`).
- Same flow on the command line, password never on argv:
  `harvester-baremetal discover --bmc <host> --user <user> --password-file <0600 file> --iso <path>`
  (or `--password-stdin`).

### Many identical nodes: install profiles and batches (1.80.0)

A **profile** is a named install configuration: the fields of the install
window, written in YAML, plus the advanced YAML, where any value may hold a
variable `{{name}}`. Built-in variables: `{{hostname}}`, `{{ip}}`
(management address), `{{mgmt_mac}}` and `{{vip}}`; a profile declares its
own (`storage_ip`, `admin_ip`...). Variables work anywhere, including inside
the NetworkManager keyfiles and `sshd` snippets of `os.write_files`:

```yaml
os:
  write_files:
  - path: /etc/NetworkManager/system-connections/storage.nmconnection
    permissions: '0600'
    content: |
      [ipv4]
      method=manual
      address1={{storage_ip}}/24
```

The advanced YAML is read **before** the variables are replaced, then each
value goes into its string as is: a value holding `: ` or `#` cannot break
the document, and a variable alone on an integer field of the installer
schema (`vlan_id: {{vlan}}`) becomes an integer. Values are one line, at
most 512 characters, without control characters.

- **Profiles** sub-section of the Bare-metal tab: list, **New profile**,
  edit, delete. The editor takes the fields in YAML (a template with the
  built-in variables is offered), the advanced YAML, and can start from an
  install configuration file (its token and password are dropped). Saving
  checks the profile against the installer schema with sample values: an
  unknown or reserved key, or an undeclared variable, is refused then, not
  when a batch starts.
- Profiles are stored in the console state directory,
  `<state>/profiles.d/<name>.yaml` (mode 0600, RFC 1123 name), and move
  with it like the clusters it declares. **No secret is ever stored**: the
  cluster token, the OS password and the BMC passwords are refused in a
  profile.
- **Install a batch**: cluster name, VIP, ISO (the profile's by default),
  joins at once (2 by default, 4 at most), the secrets, and the machines
  table: one row per machine (BMC host, BMC user, optional BMC password of
  that row, one column per variable). **Fill the table** reads a pasted CSV
  whose first line names the columns (`bmc_host,bmc_user,hostname,ip,...`,
  separator comma, semicolon or tab); a `bmc_password` column is ignored:
  type the passwords in the table or once for all rows. The token and OS
  password can also come from an install configuration file (same
  15-minute server-side cache as the install window).
- **Check and preview** substitutes every row and runs the same checks as
  an install (schema, disk checks against the discovery inventory of that
  BMC when there is one), without any secret; each row shows its mode or
  why it is refused, and its exact YAML (token masked). A missing variable
  is named with its row.
- **Start the batch** checks every row again, then nothing is powered on
  unless all pass. Row 1 creates the cluster through the usual install
  action; the other rows join `https://<vip>:443` once row 1 is **done**
  (cluster declared in the console, its generated SSH key given to the
  joining nodes), two at a time. A failed create stops the batch: the
  other rows are skipped. A parent action `baremetal-batch:<cluster>`
  shows each machine as a step (hostname, BMC, mode, install action id);
  each machine is its own `baremetal-install:<hostname>` action in the
  dock. Cancelling the batch cancels the install in progress and skips
  the rows not started.
- API: `GET|POST /api/baremetal/profiles`, `GET|PUT|DELETE
  /api/baremetal/profiles/<name>`, `POST .../from-config`,
  `POST .../<name>/csv`, `POST .../<name>/render`, `POST .../<name>/batch`
  (`cluster_name`, `vip`, `iso`, `rows`, `token`, `password`,
  `bmc_password`, `import_id`, `concurrency`). Reads are open to every
  role, writes need **admin**.
- Command line, same code (the batch runs inside the command), secrets
  never on argv:

  ```
  harvester-baremetal profile list
  harvester-baremetal profile show rack-a
  harvester-baremetal profile apply rack-a --nodes nodes.csv \
      --cluster-name rack-a --vip 10.0.0.100 --secrets-file secrets.yaml
  ```

  `secrets.yaml` (mode 0600, or the same YAML on stdin with
  `--secrets-stdin`): `token`, `password` (optional), `bmc_password`
  (common) and `bmc_passwords` (per BMC host). The CSV must not hold
  passwords. `--state-dir` points to the console state directory,
  `--port` sets the artifact server port when a console already listens
  on 8091.
- Verified by automated tests (substitution, store, ordering, refusals,
  secrets) and on the window with mocked routes. **Run for real (1.83.2)**
  on two machines behind two Redfish BMCs (the bmcfg bench): row 1 created
  the cluster, row 2 joined it, both nodes `Ready`, the cluster declared by
  itself with both nodes. That run found three defects, fixed in 1.83.2:
  `skipchecks: true` did not reach the installer, a failed row did not say
  why, and the joined node was missing from the declaration.

### Redfish: what 1.78.0 changed

Checked against a Redfish emulator driving a nested machine, beyond the
HPE iLO 4 of the first installs:

- the virtual media is the one of the **manager of the target system**
  (`Links.ManagedBy`), not the first manager the BMC lists; a BMC that
  manages several systems would otherwise mount the ISO of another
  machine;
- the `VirtualMedia` links the BMC publishes are followed (Redfish 2020.4
  and later put the virtual media under the system); the historical
  `<manager>/VirtualMedia/` path is only a fallback;
- a BMC written `host:port` works;
- a media insert that answers only once the whole image is downloaded is
  waited for; the drive saying "inserted" is not trusted, since a BMC can
  say so before the image is there;
- the preflight no longer refuses a BMC that is not an iLO.

---

## Why the ISO gets remastered

Harvester's unattended mode is switched on from the **kernel command
line**:

```
harvester.install.automatic=true
harvester.install.config_url=<url>
ip=dhcp rd.neednet=1
```

Virtual media mounts an image as-is; there is no way to add kernel
arguments to it. Hence the remastering.

**Why the network arguments matter.** The installer fetches its
configuration from `config_url` *before* it configures any network. A
PXE install already has networking, handed to it by the kernel's `ip=`
parameter. The same ISO booted from virtual media has none: without
`ip=dhcp rd.neednet=1` the installer sits there forever and never emits
a single request, with nothing on the console to say why. These two
arguments are always injected.

**How the injection works.** The official ISO has a single grub config
carrying the menu entries; the EFI one only chainloads it. Its entries
already end in `${extra_iso_cmdline}`, a variable it sources from
`/boot/grub2/harvester.cfg`. Setting that one variable is all it takes,
and it applies to every entry and both firmware paths at once. On an
older ISO without that variable, the arguments are appended to the
kernel lines directly.

**How the image is rebuilt.** It is not rebuilt: it is copied with
`xorriso -boot_image any replay`, which replays the original boot setup.
That matters because this ISO's El Torito boot record is **UEFI-only and
hidden** (it is not a file in the ISO tree), so an image reconstructed
with `mkisofs` cannot boot at all. Copying also avoids unpacking 7.7 GB:
the whole step takes about two minutes.

Three things are verified on the produced image, each of which fails
silently otherwise:

- the volume label is still **`COS_LIVE`** (the kernel mounts its root
  filesystem through `root=live:CDLABEL=COS_LIVE`, so losing the label
  produces an ISO that boots and then cannot find itself);
- the El Torito boot record survived;
- the install arguments really are in the image.

If the source ISO carries no grub entries at all, the remastering
refuses rather than producing a silently inert image.

## Watching an install that will not talk

The installer runs on the machine's first console. Nothing of it reaches
the console that started it, so a stuck install looks identical to a slow
one. Two extra kernel arguments help, both available in the
**Advanced** section of the install form:

- `console=ttyS1,115200` mirrors the install onto the BMC's virtual
  serial port, which you can then attach to from the BMC. Check which
  COM port your BMC exposes: on HPE iLO 4 it is `Com2`, hence `ttyS1`,
  and the BIOS setting `SerialConsolePort` has to be set to `Virtual`
  first.
- `harvester.install.skipchecks=true` skips the hardware preflight.
  In an install profile, `skipchecks: true` adds it (1.83.2).
  Without it, a machine that fails a minimum-requirement check aborts the
  install and says so only on that console you cannot see.

---

## Why a second HTTP listener

The console serves **HTTPS with a self-signed certificate** and requires
authentication on everything. A BMC fetching an ISO can do neither: it
will not authenticate, and it will not trust that certificate.

So the artifact server is a separate, minimal listener in plain HTTP
(default port 8091). It is not a file server: it exposes exactly two
paths, each addressed by a random one-off token with a limited lifetime,
revoked as soon as the installation ends.

```
GET /pxe/iso/<token>.iso
GET /pxe/config/<token>.yaml
```

It supports HTTP `Range` requests, because BMCs download multi-gigabyte
images in chunks.

The port has to be reachable **from the BMC network**. On a host with a
firewall, open it there.

---

## Troubleshooting

**The installer read its configuration, then nothing (1.80.0).** Seen on a
real blade with 1 Gbit/s NICs: the Harvester installer's hardware checks
refuse an unattended install on a management NIC under 10 Gbit/s (and on
too little memory or disk), show it on the machine console only, and never
fetch the install image. The console now notices this ten minutes after the
configuration was read (no image request from anyone but the BMC) and stops
with that explanation, instead of waiting an hour; the preview and the
preflight already warn when the discovery inventory shows a management NIC
under 10 Gbit/s. If the machine is acceptable for you, add
`harvester.install.skipchecks=true` to the extra kernel arguments.

**"no virtual media" on the node card.** The BMC exposes no CD device or
cannot boot from it. On iLO, check for the Advanced licence. There is no
workaround from the console: without virtual media the machine has to be
installed another way.

**Discovery returns an empty inventory.** The machine is powered off and
has never POSTed since the BMC was reset. Power it on, wait for POST,
discover again.

**The BMC never fetches the ISO.** It cannot reach the artifact server:
check the route from the BMC network to the console host, and the
firewall on that host.

**The machine boots the ISO but waits for an operator.** The kernel
arguments did not land in the boot loader that was actually used
(typically: EFI patched, legacy not, or the reverse). The remastering
step reports how many `grub.cfg` files it patched; a healthy Harvester
ISO yields at least two.

**The install runs but the API never answers on the VIP.** The VIP is on
a different subnet from the management interface, or it is already taken.
Check it is free before starting.

---

See [capabilities.md](capabilities.md) for the rest of the toolkit and
[architecture.md](architecture.md) for how these surfaces fit together.
