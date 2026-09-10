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
- **Node**: hostname, install disk (`/dev/sda`…), management interface
  (picked from the NICs just discovered), addressing (static or DHCP),
  IP / mask / gateway, cluster VIP, DNS.
- **Access**: the cluster token, the OS password, and optionally SSH
  public keys.

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
