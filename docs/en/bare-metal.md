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
- **Disks**: install disk (`/dev/sda`, or a stable
  `/dev/disk/by-path/...`), an optional data disk, and "wipe all disks".
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
`token`, `os.password`). A `system_settings.ntp-servers` is refused while the NTP
field is filled: the installer rewrites that setting from the NTP servers
and the other value would be lost without a word.

The token and the password never appear in a response, in an action
label, or in a log line.

### 4. Watch it install

The installation is a tracked action like any other: it appears in the
dock the moment you confirm, and streams its steps.

| Step | What happens |
|---|---|
| `preflight` | Powers the machine on, waits for POST, reads the **real** inventory |
| `remaster` | Patches the ISO so it installs unattended (below) |
| `serve` | Publishes the ISO and the config behind one-off tokens |
| `bmc-insert` | Mounts the ISO as virtual media |
| `bmc-boot` | Sets a **one-shot** boot on `Cd` |
| `power-on` | Resets the machine onto the ISO |
| `wait-install` | Waits for the node to install and reboot |
| `wait-api` | Waits for the Harvester API on the VIP |

The one-shot boot matters: the machine boots the ISO **once**, then goes
back to its normal boot order and comes up on the freshly installed
system. Nothing to undo by hand afterwards.

### Adding a node to an existing cluster (API only)

The tab creates a new cluster. `POST /api/baremetal/install` also takes
`"mode": "join"` with the address of the cluster to join
(`"server_url": "https://<VIP>:443"`) instead of a VIP. That cluster must be
declared in the console: the action ends when the new node is **Ready** in
it (step `wait-node`), not when an API answers, since the cluster's API was
already up. The configuration generated for a join was checked by
installing two nodes into a three-node test cluster; the Redfish-driven
flow in join mode has not been run on real hardware yet.

### Reading the disks first: the discovery boot (API and CLI)

Some BMCs publish no disks at all (HPE iLO 4 publishes none), so the console
cannot tell which disk to install on. `POST /api/baremetal/discover`
(`bmc_host`, `bmc_user`, `bmc_password`, `iso`, optional `extra_args`) boots
the Harvester ISO once, with a small script of the console, and waits for
the machine to send back what Linux sees: disks with their stable
`by-path`/`by-id` links, partitions, NICs. The machine then powers itself
off. `GET /api/baremetal/inventory/<bmc_host>` returns the parsed inventory
(`source`, `at`, `system_serial`, `disks`, `nics`), or 404.

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
  the inventory is stored with a warning step.
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
