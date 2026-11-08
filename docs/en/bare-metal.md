# Bare-metal: installing Harvester on a blank machine

The Bare-metal tab turns an empty server into a running Harvester node
without anyone walking to the rack, plugging in a USB stick, or clicking
through the installer. The console drives the machine through its BMC.

Nothing here is Harvester-specific magic: it is the vendor's own
zero-touch installation, driven end to end by the console.

Optional [storage profiles](baremetal-storage-onboarding.md) can prepare dedicated
storage NICs and provider prerequisites during a join, then leave the host cordoned
for acceptance checks. The provider-neutral network profile and the unverified Pure
example do not install CSI charts or configure array credentials.

---

## The screen: five tabs (2.1.0, Plans in 2.4.0)

Automation > Bare-metal opens on **Machines**; the other tabs sit next to it,
each with its own address:

| Tab | Address | What it holds |
|---|---|---|
| **Machines** (default) | `#automation/pxe/machines` (also `#automation/pxe`) | The machines of the last discovery, one row each: hardware, power state, disks, network cards, and the gestures **Install** (or **Join** *cluster* during a join), power on, graceful shutdown, force off, restart. **Discover...** opens the discovery window; **Re-read** reads the same BMCs again. The join banner and the "no ISO" banner sit on top. |
| **Profiles** | `#automation/pxe/profiles` | Multi-node install profiles and the batch launch. |
| **Plans** (2.4.0) | `#automation/pxe/plans` | Deployment plans, declaration templates and cluster templates (2.5.0): Terraform content, Cluster API stack and child clusters applied after an install or to an existing cluster (see below). |
| **Images** | `#automation/pxe/images` | The ISO store. |
| **History** | `#automation/pxe/history` | The bare-metal actions of Activity, running and past, each with a link to its log. |

**Refresh** in the card header reads the open tab again. Nothing is lost
when you change tab, reload the page or come back later: the console
remembers the last discovery (see below).

---

## What has to be true before you start

| Requirement | Why | How to check |
|---|---|---|
| The BMC speaks Redfish | Everything is driven through it | Discovery lists the node |
| The BMC exposes a **CD virtual media** device | The ISO is mounted over the network | The machine row offers **Install Harvester**; otherwise it shows *no virtual media* |
| The BMC can boot from `Cd` | The machine must come up on that ISO once | Same badge as above |
| The console host is reachable **from the BMC** | The BMC downloads the ISO from it | `curl` the console host from another machine on the BMC network |

On HPE iLO 4/5, virtual media is an **iLO Advanced** feature. Without
that licence the BMC exposes no CD device and the console will say so
rather than fail thirty seconds into an installation.

The machine row also reports the number of disks the firmware can boot
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

The **Images** tab downloads an ISO **server-side, as a stream**.
The file never passes through your browser: a Harvester ISO is around
7.6 GB, and an upload of that size through a browser would be spooled in
memory on the way in.

Paste the vendor URL, press Download, and follow the percentage in the
dock. The store shows what is on disk and how much room is left.

Downloading needs the **admin** role. The console checks the URL before
it connects, and again at every redirect:

- only `http` and `https`, at most 5 redirects, and no redirect from
  `https` back to `http`; TLS certificates are always verified;
- the host name is resolved once and the console connects to the address
  it checked, so a DNS answer cannot switch to an internal address;
- loopback, link-local, private (RFC 1918, ULA, CGNAT), multicast,
  broadcast, unspecified and reserved addresses are refused, and so are the cloud
  metadata addresses (`169.254.169.254`, `fd00:ec2::254`);
- the file may not exceed 20 GiB, and the download stops when the store
  falls under 1 GiB of free space.

An isolated site often serves its ISOs from an internal mirror. Allow
its network in `config.yaml`; the console reads the setting at each
download, no restart needed:

```yaml
iso_fetch:
  allow_private: ["10.20.0.0/16", "fd12:3456::/32"]   # internal mirrors
  max_bytes: 21474836480        # size cap, 20 GiB by default
  min_free_bytes: 1073741824    # free space kept in the store, 1 GiB by default
```

Every address the mirror's name resolves to must be allowed, IPv4 and
IPv6 alike. The metadata addresses, multicast and unspecified addresses
stay refused even inside an allowed network, and so do `255.255.255.255`
and the broadcast address of an allowed IPv4 network.

The ISO is deliberately **not** part of the release tarball: the whole
toolkit is 135 MB, the ISO alone is fifty times that.

### 2. Discover the BMC

In **Machines**, **Discover...** opens a window: enter one or more BMC
addresses, a username and a password. The credentials are **never stored
on the server**: they stay in the page for the length of your session, and
every subsequent action re-sends them. A BMC address is a host name or an
IP address, with an optional port; anything else (a path, a scheme, a
user name) is refused before any read.

Each reachable BMC comes back as a row: model, serial, BIOS, CPU and
memory, disks, NICs with their MAC addresses and link state, current power
state, and whether the machine can be installed. An unreachable BMC keeps
its row, with the reason.

**The console remembers the last discovery** (2.1.0): the addresses, the
date and what each BMC answered, so the list is still there after a reload
or the next day. The line above the list gives its date; **Re-read** reads
the same BMCs again with the credentials of the page, and opens the window
to ask for them when the page was reloaded. The power state shown is the
one read at that date (its tooltip says so).

- Stored in `<state>/baremetal-last-discovery.json` (mode 0600, replaced
  atomically, relative to the state directory, so it moves with it). Only
  the known fields of a machine are kept; a credential never is.
- It is a **read cache**, like the folders cache: written during the
  Redfish read itself, with no tracked action, because nothing changes on
  a machine or a cluster. A new discovery replaces it.
- Reading it needs the **admin** role (`GET /api/bmc/last-discovery`): it
  names the BMCs of the fleet and their serial numbers.

Before you act, the tab checks the store: a banner says **No ISO in the
store** when it is empty, and, during a join, **No ISO of version X in
the store** when no ISO has the cluster's version (read from the cluster,
`setting/server-version`, and matched against each ISO's
`harvester-release.yaml`, or its name). **Open Images** leads to the
store.

> **Careful with a powered-off machine.** A BMC does not re-read hardware
> while the server is off: it replays the inventory of the **last POST**.
> On a machine that has not booted in months, that inventory can be
> months stale. Installation preflight reads that inventory without booting
> the previous OS and checks any saved discovery identity. Use a discovery
> boot when fresh inventory is needed; installer checks still apply.

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

**A machine that already carries a system (2.2.0).** Before the Install
button, the window says whether a system seems present on the machine,
from what the BMC already publishes (read only, nothing is changed):

- a UEFI boot entry that starts a loader `\EFI\...` from a disk partition
  (USB keys, CDs and network entries are left out), named with the loader
  and the disk, for example `Boot entry "elemental-shim" starts
  \EFI\elemental\shim.efi from a disk partition (HD.Emb.1.3)`;
- the same entry at the head of the boot order ("boots first from");
- a logical drive (RAID volume) on a controller.

On an iLO 4 these come from `Systems/1/BIOS/Boot/` (the iLO 4 publishes no
disk nor volume), as of the machine's last boot; on an iDRAC 9 or an iLO 5,
from `Systems/<id>/BootOptions` and `Boot.BootOrder`. When a system seems
present, the evidence is listed and installing requires typing the machine
name exactly: its serial number, or the BMC address when the serial is
unknown; Install stays off until it matches. **No sign** is said only when
the boot entries could be read and none points to a system (a system
started in legacy BIOS mode would not show); when the BMC tells nothing,
the window says **Unknown**, never empty. The console checks again at
install time, on its own Redfish read, and refuses with `409` and
`fields: ["system_confirm"]` when a system seems present and the typed
name (`system_confirm`) does not match. `POST /api/bmc/system-check`
(`{host, user, password}`, administrator) returns `state` (`present`,
`absent`, `unknown`), `evidence` and `confirm_name`. A boot entry can
outlive a wiped disk: the check then asks for the name once more, which is
the safe side. Batches of a profile go through the same check, machine by
machine (2.2.1, below).

### 4. Watch it install

The installation is a tracked action like any other: it appears in the
dock the moment you confirm, and streams its steps.

| Step | What happens |
|---|---|
| `preflight` | Reads BMC inventory without powering on the old OS; validates saved discovery identity and checks there is a disk: the discovery inventory first, then the HPE UEFI boot targets, then the Redfish drives; with none of them, a warning (the installer checks its disk itself) |
| `remaster` | Patches the ISO so it installs unattended (below) |
| `serve` | Publishes the ISO and the config behind one-off tokens |
| `power-off` | Powers the machine off (any state other than `Off`, including a transient or unreadable one, gets a forced power-off) before mounting media or changing boot settings |
| `bmc-insert` | Mounts the ISO as virtual media, and waits for the BMC's answer (up to 15 min, `HARVESTER_OPS_BM_INSERT_WAIT`) |
| `bmc-boot` | Sets a **one-shot** boot on `Cd` |
| `power` | Verifies the host is still off and the boot target reads back as `Cd`, then powers on once. A BMC that does not read back `Once` (the sushy-tools emulator always says `Continuous`) gets a warning, not a refusal |
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

Preflight and ISO preparation do not change host power. Once preparation
succeeds, installation powers the host off before mounting virtual media.
If the mount fails (for example the BMC cannot reach the ISO address), a
machine that was found running is powered on again. Once the boot settings
are touched, the console never powers the host on unless the `Cd` target
is confirmed: powering on then could start the installer.

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

Since 1.88.1, waiting for the node and creating the disk pools use your own
rights on the joined cluster (delegated identity, or your Rancher session),
resolved when you start the install. Before, the install thread fell back
to the console's own kubeconfig. Someone without the rights to read nodes
or write block devices there now sees those steps refused.

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

Profiles live in the **Profiles** tab (2.1.0).

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
- **Machines that already carry a system (2.2.1).** The batch window
  runs the check of the install window (above) on every machine of the
  table: **Check the machines** reads each BMC, read only, and shows one
  banner per machine, `Row <n> · <BMC>`, with **present** and its evidence,
  **No sign**, or **Unknown** said plainly. For every machine where a
  system seems present, type its name (serial number, or BMC address when
  the serial is unknown) exactly; a wrong name is said. **Start the batch**
  reads the machines first when the table changed since the last check,
  and does not start while a name is missing or wrong. The console then
  reads every BMC again itself, after the row checks and before anything
  is powered on, and refuses the whole batch with `409`,
  `fields: ["system_confirm"]`, `refused` (BMC hosts) and `machines` (each
  machine with its row, state, evidence, name to type and `confirmed`)
  when a machine where a system seems present lacks its name; nothing
  starts, and the window shows what the console read. "Unknown" is not a
  refusal, as for one machine. The typed names go in
  `rows[].system_confirm` and never reach the install options.
- API: `GET|POST /api/baremetal/profiles`, `GET|PUT|DELETE
  /api/baremetal/profiles/<name>`, `POST .../from-config`,
  `POST .../<name>/csv`, `POST .../<name>/render`, `POST .../<name>/batch`
  (`cluster_name`, `vip`, `iso`, `rows`, `token`, `password`,
  `bmc_password`, `import_id`, `concurrency`; `rows[].system_confirm`,
  2.2.1), `POST .../<name>/system-check` (same body, read only: returns
  `machines`, 2.2.1). Reads are open to every
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
  on 8091. When a system seems present on a machine, the command prints
  each machine with its state and evidence, starts nothing and exits 2;
  `--confirm-system <BMC host>=<name>` (once per machine, the name as
  printed) confirms it (2.2.1).
- Verified by automated tests (substitution, store, ordering, refusals,
  secrets) and on the window with mocked routes. **Run for real (1.83.2)**
  on two machines behind two Redfish BMCs (the bmcfg bench): row 1 created
  the cluster, row 2 joined it, both nodes `Ready`, the cluster declared by
  itself with both nodes. That run found three defects, fixed in 1.83.2:
  `skipchecks: true` did not reach the installer, a failed row did not say
  why, and the joined node was missing from the declaration.
  **Checked for real (2.2.1), read only**: a batch window with node3
  (iLO 4, `172.16.1.33`) and node4 (iLO 4, `172.16.1.34`) showed both as
  present, node3 with `sles-secureboot` (`\EFI\sles\shim.efi` on
  `HD.Emb.5.2`) and the name `USE6236S00`, node4 with `elemental-shim` and
  `sles-secureboot` and the name `USE6236RY3`; a wrong name was said and the
  batch did not start. The batch was never submitted.

### Deployment plans: Terraform content after installation (2.4.0)

A **deployment plan** is a named, reusable list of phases that the console
runs as one tracked action, `deploy:<cluster>`. You apply it to a Harvester
cluster already declared, or you chain it after a bare-metal installation.
In 2.4.0 the phases are:

| Phase | What it does |
|---|---|
| **install** | Only when the plan starts from Bare-metal: the single-machine install or the batch, with the same BMC check (guard 132) as without a plan. |
| **readiness** | Waits until the cluster is usable: nodes Ready, `ManagedChart` objects ready, Longhorn nodes and volumes healthy, a default storage class. Read again every 15 s, 20 minutes by default (`phases.readiness.timeout_minutes`, 1 to 180). |
| **terraform** | Instantiates each **declaration template** of the plan as an ordinary Terraform declaration, `<plan>-<template>`, then plans and applies it. |
| capi, clusters | Available since 2.5.0: see "Deployment plans: Cluster API and child clusters" below. |
| registry | Available since 2.6.0: see "Deployment plans: Registry phase (2.6.0)" below. |
| artefacts | In the model, shown greyed with "coming in 2.7" in 2.4.0; available since 2.7.0. |

**Where it lives.** The **Plans** tab (`#automation/pxe/plans`, between
Profiles and Images) lists the plans on the left and opens one on the right:
one card per phase with its switch, the templates of the Terraform phase
chosen from a list (never typed), their order (up and down buttons), and an
optional "depends on" per template. The **Declaration templates** part of
the tab edits templates with the Terraform forms (VM, image, SSH key,
network, storage class, IP pool, free code). Files are kept in
`<state>/plans.d/<name>.yaml` and `<state>/tf-templates.d/<name>.yaml`,
mode 0600, written atomically, with a revision: saving with an old revision
gets 409 and keeps what you typed. **Save as template** in Terraform >
Declarations turns a Harvester declaration into a template.

**Variables.** A template writes `{{name}}` where a value changes from one
cluster to another; the plan lists the variables it asks for. `cluster` and
`vip` are built in (`vip` of an existing cluster is the address in its
kubeconfig, if it is one). A value is one line of 512 characters at most,
without control characters. A string that is only a variable takes the type
of the form field (a VLAN id becomes a number). In free code, a value is
placed according to where it stands: in a quoted string or a heredoc it is
escaped and arrives as text; outside a string only a number or
`true`/`false` is accepted; and the result is read again: a value that
would change the structure of the template (a heredoc delimiter, a `$` or
`%` followed by a `{` from the next value) is refused with its name.

**No secret in a template.** Anything passed to Terraform ends in its state.
Saving a template removes values under secret-looking keys (and says which
paths it removed), keeps public keys and the *names* of Kubernetes Secrets
(`user_data_secret_name`), drops a cloud-init that talks about passwords,
and refuses free code that assigns a secret, credentials in a URL, or a
function that hides a value (`base64decode`, `sensitive`). The same check
runs on the rendered template, so a value that looks like a secret is
refused too. The check is lexical (secret key names, password lines in
`user_data`, credentials in a URL): a token given as the value of a neutral
variable, such as `echo {{x}}` in `user_data`, is not detected. Plan values
are not meant for secrets, as they are stored in the run result. Keep the
secret in a Kubernetes Secret and reference it with the `*_secret_name`
fields.

**Deploy window.** **Apply a plan** on a Harvester host page, or **Deploy**
in the Plans tab, opens a window with three tabs: **Parameters** (cluster,
plan, variables), **Checks** (findings grouped by phase, blocking ones
first) and **Summary** (declarations created or reused, estimated time).
**Launch** stays disabled while a finding blocks, with the reason in its
tooltip. **Dry run** runs the same chain but only plans: it creates the
declarations, plans them, then removes them; nothing is written to the
cluster.

The install window of a machine and the batch window of a profile have an
optional **Plan** field, pre-filled by the `plan` of the profile, with the
variables of that plan; with a plan, the action that opens is `deploy:`
and the install is its first phase.

Findings: `plan-invalid`, `phase-not-available`, `template-missing`,
`var-missing`, `var-invalid`, `decl-name-taken`, `tf-missing`,
`cluster-unreachable`, `admin-kind`, `forbidden-construct` (blocking);
`template-newer`, `decl-differs`, `plan-changed` (warnings);
`decl-exists`, `readiness-later` (information).

**Following a run.** The action shows one step per phase (`phase-<id>`),
readiness sub-steps (`ready-<code>`) and one step per template
(`tf-<template>`), each with the id of its child action (the install, the
Terraform plan and apply) as a link. Statuses: done, error, partial (a
template failed and the independent ones went on), cancelled. A template is
independent unless it comes after a failed template that it names in its
"depends on".

**Never a destruction.** If the Terraform plan of a template counts a delete
or a replace, it is not applied: the template ends in error with
`plan-destroys` and a link to the declaration to review. A declaration that
already exists and was made by the same plan and template is only planned
and applied. If its content differs from what the values give, it is kept
and the run says so (`kept: values differ from the declaration`), unless
**Update declarations whose values differ** was ticked at launch.

**Stop and resume.** **Stop** (or the dock) stops the run: an install in
progress is stopped, a Terraform run goes to its end, templates not started
are skipped (`cancelled`). A stop during the last template has nothing left
to skip: the run ends `done`, with `cancel_requested` in its result.
**Resume** (History tab, on an error, partial, cancelled or
interrupted run) starts a new `deploy:` action that reads the result of the
first one, checks each finished phase again cheaply (cluster declared and
nodes present, readiness replayed, declaration present with the applied
revision) and starts from the first phase that is not done. A run whose
console was restarted is marked interrupted and can be resumed. A batch
whose nodes all joined is not reinstalled; otherwise only the rows whose
host name is not a node are installed again, with the BMC check and the
secrets asked again. A single-machine install is not resumed: start it
again.

**Secrets.** Only the install phase uses secrets in 2.4.0 (cluster token, OS
password, BMC passwords). They are typed at launch, kept in memory for the
run only, cleared at the end whatever the outcome, and asked again on
resume. They never appear in the action label, its steps, its result, the
plan or template files. A plan applied to an existing cluster asks for no
secret.

**Roles.**

| Gesture | Role |
|---|---|
| Read plans and templates (a viewer sees `(hidden)` in place of cloud-init) | viewer |
| Write or delete a plan or a template | admin |
| Apply a plan to an existing cluster, stop, resume | operator |
| A chain with an install, or its resume | admin |
| A plan whose capi phase must install the Cluster API stack (an operator may run it when the stack is already installed at the bundle versions: the phase then only checks it) | admin |
| A plan whose templates create VM networks, storage classes, IP pools (or, in free code, cluster networks, VLAN configurations, load balancers, settings, PCI or SR-IOV devices) | admin |

An operator may launch a plan whose templates hold free code: an admin wrote
that code, and the operator only gives values, which are neutralised. The
declaration produced records who wrote the template revision
(`origin.template_by`). Outside the chain, that declaration stays free code
and only an admin plans, applies or destroys it from Terraform >
Declarations. The server decides; the window shows the `admin-kind`
finding and keeps Launch disabled.

Installing the Cluster API stack is reserved to administrators, in a plan as
in Cluster API > Installation. When the stack must be installed, an operator
gets the `capi-install-admin` finding and Launch stays disabled; the capi
phase checks the role again when it starts, so a resume or a stack removed in
the meantime is refused the same way.

**One cluster at a time.** One `deploy:` action runs per cluster (409
`deploy-running`); a shutdown or startup of the cluster blocks it, and a
running plan blocks a shutdown. The lock is a file
(`<state>/deploy.d/<cluster>.lock`) shared by the console and the command
line.

**Seen for real (2.4.0).** On harvlab: network, storage class and IP pool
templates, a failure left `partial`, a fix in Terraform > Declarations, a
resume that skipped the finished templates, a stop and its resume, dry run
with nothing written, operator and admin roles. On a Redfish bench
(sushy-tools on a KVM machine): a one-node Harvester installed by a plan,
then readiness and one template, 36 minutes in all (install 35 min,
readiness 76 s, Terraform 4 s). Not seen yet: a batch with a plan, the BMC
guard inside a plan, the resume of an install phase.

**Command line.** `bin/harvester-plans.py` loads the console and runs the
same chain: `plan list|show|save|delete`, `template list|show|save|delete`,
`check --cluster C [--vars FILE] PLAN`, `apply --cluster C [--vars FILE]
[--dry-run] [--update-declarations] PLAN`, `resume [--secrets-file F |
--secrets-stdin] [--confirm-system HOST=NAME] ACTION`, and `install
--profile P --nodes FILE --cluster-name N --vip IP PLAN` for a batch with a
plan. `--json` prints one document at every exit. Exit codes: 0 done, 1
error or partial, 3 cancelled. The command line runs with the rights of the
state directory (it does not check roles).

**Rolling back to 2.3.x.** The upgrade to 2.4.0 is additive and leaves
existing declarations as they are. A rollback to 2.3.x (Update tab) works
without a crash, with three limits: 2.3.x lists the VM network, storage
class and IP pool declarations but cannot plan, apply or destroy them
(their objects stay on the cluster, unmanaged); a profile saved in 2.3.x
loses its `plan` field; plans, templates and deployment runs are ignored
until 2.4.0 is back. Before rolling back, destroy the declarations of these
three kinds, or accept them as unmanaged.

### Deployment plans: Cluster API and child clusters (2.5.0)

After the Terraform content, a plan can declare the Cluster API stack and
create child RKE2 clusters, on Harvester 1.9 or later only.

| Phase | What it does |
|---|---|
| **capi** | Declares the stack (RKE2 bootstrap and control plane, CAPHV, CAAPH) from the active bundle, with the same command as the **Install the providers** button (`harvester-capi install`, images pushed to the nodes). A stack already ready at the bundle versions is left as it is (nothing declared, no child action). 10 minutes by default (`phases.capi.timeout_minutes`, 1 to 60). |
| **clusters** | Creates the child clusters of the plan **one after another**, each from a named **cluster template**, with the same command as the creation window (`harvester-capi create`). One child action `capi-cluster-create:<ns>/<name>` per cluster. 30 minutes per cluster by default (`phases.clusters.timeout_minutes`, 5 to 180). 16 clusters at most per plan. |

**Cluster templates.** A cluster template holds a cluster request of the
Cluster API creation form, without name nor namespaces (the plan gives
them). Values may use `{{variables}}`, like declaration templates. They are
kept in `<state>/capi-templates.d/<name>.yaml`, mode 0600, with a revision.
The **Cluster templates** part of the Plans tab lists and edits them in the
creation form (template mode, **Variable...** in each reference field), and
**Save as template** in the creation window of Cluster API > Clusters
stores the request you filled. A template used by a saved plan cannot be
deleted (409 `template-in-use`). Reading needs the viewer role, writing the
admin role.

**Clusters of a plan.** The Clusters card lists the entries in order:
cluster name (also its namespace, 40 characters at most once rendered,
`{{cluster}}` and plan variables allowed), template, and "depends on". The
**Several** shortcut adds `count` clusters from one template, named by a
pattern holding `{{n}}` (1, 2, 3...); they are appended to the list when the
plan is saved. In a plan file:

```yaml
phases:
  capi: {enabled: true, timeout_minutes: 20}
  clusters:
    enabled: true
    timeout_minutes: 45
    items:
      - {name: site1-apps, template: rke2-small}
    template: rke2-small        # shortcut, expanded at save
    count: 3
    name_pattern: "site1-k{{n}}"
```

**One restricted identity per child cluster.** Up to 2.4.0, CAPHV acted on
Harvester with the console kubeconfig. A cluster created by a plan, or by
the creation window, now gets its own service account with only the rights
CAPHV v0.10.1 uses, read call by call in its source (see Capabilities,
section 4). Its VMs go to a namespace of their own, `<name>-vms`, created
and deleted with the cluster (a template that sets another VM namespace is
refused). The console creates the identity, the
cloud provider account and its binding with the identity of the person who
launched the run; the check verifies beforehand, in one batch of
`SelfSubjectAccessReview`, that this person may create, update and delete
each of these objects and hold each right they grant.

**Checks before acting.** On top of the 2.4.0 findings, for all the
clusters of the plan together and in one read of the cluster: free CPU and
memory, Longhorn space for every disk (replicas on distinct nodes), free
addresses in each IP pool, the rights of the person, the Harvester version,
the stack version, and the per-cluster rules of the creation window.

- Blocking: `capi-bundle-missing`, `capi-bundle-no-turtles`,
  `capi-needs-harvester-1.9`, `capi-version-differs` (a plan never upgrades
  a stack, even for a plan with the clusters phase only: upgrade from
  Cluster API > Installation), `capi-stack-missing`,
  `capi-install-admin` (the stack must be installed and the role is not
  admin), `capi-template-missing`, `cluster-var-missing`, `cluster-name-taken`,
  `identity-rules-unknown`, `identity-name-reserved`,
  `target-namespace-forced`, `vm-namespace-taken`, `vm-namespace-too-long`,
  `cloud-provider-binding-taken`,
  `capi-check-failed`, `fleet-name-twice`, `fleet-cpu-short`,
  `fleet-memory-short`, `fleet-storage-short`, `fleet-replicas-nodes`,
  `fleet-pool-short`, `rights-missing`.
- Warnings: `rights-unchecked`, `cluster-failed`, `fleet-storage-unknown`
  (an image still importing, its root disks not counted), and the warnings of the
  creation window (one control plane node, version not tested...).
- Information: `cluster-exists`, `capi-later`, `refs-later`,
  `terraform-ref-later`, `fleet-dhcp-leases`, `fleet-sizes`.

`terraform-ref-later` (2.8.2): the image, key pair or IP pool a cluster
template names is missing, but a declaration of the Terraform phase of
this plan creates it. The console reads it in the rendered resources of
the plan templates (`image`, `ssh_key`, `ippool`, or free code whose
`resource` block has a literal name, without `count` or `for_each`).
An image matches by `namespace/name` or `namespace/display name`; a
missing namespace means `default`. A declaration kept with other values
counts with its own resources. The Clusters phase checks again before
any creation and blocks if the reference is still missing.

A shortage that the creation window only reports as a warning blocks a
plan: the chain runs unattended.

**Following a run.** Steps `phase-capi` (sub-step `capi-providers`) and
`phase-clusters` (one `k8s-<name>` per cluster). The child actions are
nested under the `deploy:` action in Activity (unfold the row) and in the
dock (the parent and its current child). The result records, per cluster,
whether this run created it, the template revision and its child actions.

**Stop, resume, failure.** A stop lets the cluster being created go to its
end and skips the next ones. A stop while the run waits on a cluster it
resumed ends that wait at once. Cancelling the child action itself deletes
that cluster. **Resume** checks the stack again, then takes each cluster in
turn: one already made by this plan and not failed is waited for; one in
failure stays in error (`cluster-failed`): delete it, then resume. A plan
never deletes a cluster on its own.

**Deleting a child cluster.** From Cluster API > Clusters, as before. The
`Cluster` goes first; once it is gone, the identity objects are removed one
by one by name, then what the console generated and the namespace it
created. If an object of the identity cannot be read or deleted (a right
missing, for example), the deletion ends in error and keeps the namespace:
fix the cause and delete again, the deletion resumes there.

**Command line.** `harvester-plans capi-template list|show|save|delete`;
`check` and `apply` cover the two phases. `harvester-capi create
--identity restricted|console` (restricted by default; console only for a
diagnosis), `--origin plan=P,template=M,run=ID`, `harvester-capi check
--many --spec FILE` (a JSON list, checked together), `harvester-capi state
--name ns/name`.

**Seen for real (2.5.0).** See the CHANGELOG of 2.5.0 for the benches,
timings and what is not verified.

### Deployment plans: Registry phase (2.6.0)

Before the child clusters, a plan can pose or check their registries and
publish artefact packages into them. Packages, registries and the
behaviour of child clusters are described in
[artefacts.md](artefacts.md).

| Phase | What it does |
|---|---|
| **registry** | One item per registry of the plan: a **bootstrap** registry is posed on the Harvester cluster (`harvester-registry deploy`, child action `registry-deploy:<cluster>/<name>`), or adopted if this plan posed it; an **external** registry, declared and attached beforehand, is only checked. Then one item per publication (`harvester-artefacts publish`, child action `artefacts-publish:<name>-<version>@<registry>`), after its registry when the registry belongs to the plan. |
| artefacts | Since 2.7.0: see "Deployment plans: Artefacts phase (2.7.0)" below. |

The order of the chain is install, readiness, terraform, **registry**,
capi, clusters: the clusters need their registry when they are created, and
the registry may use a storage class made by Terraform.

In a plan file:

```yaml
phases:
  registry:
    enabled: true
    timeout_minutes: 10            # a bootstrap registry ready within, 1 to 60
    publish_timeout_minutes: 60    # each publication, 5 to 600
    registries:
      - name: seed
        mode: bootstrap
        address: "{{registry_ip}}"
        size_gib: 20               # 5 to 2048
        storage_class: harv-rep1
        mirrors: [docker.io, ghcr.io]
        dns_name: registry.site1.example   # optional
      - name: client-harbor
        mode: external             # declared and attached beforehand
    publish:
      - {package: socle, version: "2026.11", registry: seed}
  clusters:
    enabled: true
    items:
      - name: site1-apps
        template: rke2-small
        registries: [seed, client-harbor]
```

- Registry names: 30 characters at most, `bootstrap` reserved; 8 registries
  and 32 publications at most per plan.
- A bootstrap registry of the plan carries no account: the console
  generates them. Plan variables are allowed in the address, the storage
  class, the mirrors and the DNS name.
- An external registry takes only `name` and `mode`.
- A publication names an imported package and a registry of the plan or a
  declared one.
- **Registries of the child clusters.** A cluster template and a plan entry
  take `registries`: names, or `{registry, mirrors, no_fallback}`, in
  order, with `{{variables}}`. The list of a plan entry replaces the one of
  its template. An entry names a registry of the Registry phase by its
  name.

**Checks before acting.** The checks of `harvester-registry deploy
--dry-run` for each registry to pose (free address, Longhorn room, free
name, embedded images, tools), `check` for each external registry, and
`publish --dry-run` for each publication whose registry already exists.
Their findings appear with their own codes (`registry-address-answers`,
`registry-room-short`, `publish-tag-differs`...). On top of them:

- Blocking: `registry-name-taken` (a registry of the same name exists and
  was not posed by this plan), `registry-address-differs` (the registry of
  this plan answers at another address: a plan never moves a registry),
  `registry-unknown`, `registry-admin`, `registry-check-failed`,
  `registry-package-too-big` (the packages to publish pass 90 % of the
  volume), `artefact-package-missing`, `package-untrusted-strict`,
  `registry-var-missing`.
- Information: `registry-exists` (posed by this plan, adopted),
  `registry-later` (checked when the phase starts, after an install),
  `publish-later` (the registry is posed by this run: the publication is
  checked once it is there), `cluster-registry-later` (a child cluster
  uses a registry of the Registry phase: checked again at the start of the
  Clusters phase).
- Warning: `registry-vm-network-unchecked` (the bootstrap registry is
  probed from the VM network when the clusters are checked),
  `cluster-registry-unreachable-from-vm-network` (blocking when the
  cluster cuts the fallback to the Internet) and
  `cluster-registry-vm-network-unchecked` (the probe pod could not judge,
  with the reason); see [artefacts.md](artefacts.md).

**Adoption.** A bootstrap registry posed by a plan carries its origin (the
label `harvester-ops.io/plan`, the annotation `harvester-ops.io/plan-run`
on its objects). A new run of the same plan adopts it, if it answers at the
same address. A registry of the same name without this origin blocks.
A deployment of this plan that failed before the registry was ready
leaves objects without a declaration: they carry the origin too, so the
checks (replayed with the origin of the plan) do not count them as a taken
name or an answering address, and the resume deploys over them.

**Following a run.** Step `phase-registry`, then one sub-step per item:
`reg-<registry>` and `pub-<package>-<version>-<registry>`. The child
actions are nested under `deploy:` in Activity. A publication that passes
`publish_timeout_minutes` is stopped. **Resume** checks each finished item
again: registry ready at the same address, every image and chart present at
the right digest (one `HEAD` each); a publication that resumes skips the
layers already sent. **Dry run** poses and publishes nothing.

**Never a deletion.** The chain never deletes a registry, even after a
failure. A run records `undo: {kind: registry, ref: <cluster>/<name>}` for
a registry it created; the undo of the deployment (2.7.0) uses it.
Remove a registry from Cluster API > Registries.

**Roles.**

| Gesture | Role |
|---|---|
| A plan that poses a bootstrap registry (`registry-admin` otherwise, checked at launch and again when the phase starts) | admin |
| A plan whose registries already exist, publications, child clusters with registries | operator |

**Command line.** `harvester-plans check` and `apply` cover the phase;
`harvester-registry deploy --origin plan=P,run=ID` sets the origin.

**Seen for real (2.6.0).** On harvlab2 (Harvester v1.9.0, one nested
node), a plan with the Registry phase (bootstrap registry of 8 GiB at
172.16.2.76, client registry, publication of a 31 MiB package) and one
1+1 child cluster: check in 12 s; registry posed and ready in 37 s,
publication in 3 s, cluster available in about 10 minutes. A resumed run
checked the registry and the publication again and skipped them
(`already done (checked)`); the next runs adopted the registry
(`registry-exists`). Details: [artefacts.md](artefacts.md), "Seen for
real".

### Deployment plans: Artefacts phase (2.7.0)

The last phase of the chain applies package items to the child clusters:
the base of policies and the automation accounts, manifests and policies,
then charts. What each item writes and how it is removed is described in
[artefacts.md](artefacts.md), "Artefacts on child clusters (2.7.0)".

```yaml
phases:
  artefacts:
    enabled: true
    timeout_minutes: 15            # per item, 1 to 60
    policies:                      # base and access, per cluster
      - clusters: ["{{cluster}}-apps"]
        base: false                # unticked by default
        teams: [{namespace: team-a, quota: {pods: "20"}}]
        roles: [{namespace: team-a, group: devs, role: view}]
        accounts: [{namespace: team-a, name: ci, role: edit}]
    items:
      - {package: socle, version: "2026.11", item: podinfo,
         registry: "{{registry}}", values: {replicaCount: 2},
         clusters: ["{{cluster}}-apps"]}
      - {package: socle, version: "2026.11", item: ingress-rules,
         clusters: ["{{cluster}}-apps", "tools/tools"], depends_on: []}
```

- `clusters`: a cluster of the Clusters phase of the same plan (rendered
  name), or an existing child cluster `<ns>/<name>`
  (`artefact-cluster-unknown` otherwise).
- A chart needs `registry` (a registry where the package is published, or
  published by the Registry phase of the plan); a manifest refuses
  `registry` and `values`. `values` follow the secret rules of the plans.
  The kinds `images` and `rke2` ("published by the Registry phase") and
  `gitops` ("applied by a GitOps entry", 2.8.0) are refused.
- 32 entries and 128 items once the clusters are unfolded, at most.
- In each cluster: `pol-<cluster>` (base, roles, accounts) first, then the
  manifests and policies, then the charts, in the order of the entries;
  `depends_on` between entries is honoured. The items of a cluster wait for
  its creation; when a cluster fails in the Clusters phase, its items are
  skipped and the other clusters get theirs (phase `partial`).
- Before the phase, the console replays the checks of each item whose
  cluster exists: package and trust (strict mode), CAAPH, chart published
  (`artefact-not-published`), images published in a registry of the
  cluster (`artefact-images-not-published`: a warning, blocking when the
  cluster cuts the fallback), objects (`manifest-check` in the child),
  applicable name (42 characters), right of the person on
  `<c>-kubeconfig` (`child-admin-required`), item laid by another origin
  (`artefact-taken`).
- An item is marked `created` only when it carries the run of this chain;
  an item already laid by a run of this plan is adopted, and keeps the run
  that created it. A resumed run checks the items done (manifests: every
  object read again in the child, since an equal checksum does not prove
  they are all there) and skips them.
- 2.7.0 also marks the Terraform declarations a run creates (`origin.run`,
  `created`, `undo`), so that the undo can prove what it destroys.

### Deployment plans: GitOps phase (2.8.0)

The last phase of the chain, after Artefacts, poses GitOps entries on the
child clusters. What an entry writes, Flux and Fleet, the sources and the
lockdown are described in [gitops.md](gitops.md).

```yaml
phases:
  gitops:
    enabled: true
    timeout_minutes: 15            # per item, 1 to 60
    entries:
      - name: apps                 # RFC 1123, 34 characters at most
        tool: auto                 # auto | flux | fleet
        clusters: ["{{cluster}}-apps", "tools/tools"]
        target_namespace: team-a
        source: {type: oci, package: socle, version: "2026.11",
                 item: apps, registry: "{{registry}}"}
        privileges: namespace      # namespace | cluster (admin)
        interval_minutes: 10
        prune: true
      - name: infra
        clusters: ["{{cluster}}-apps"]
        target_namespace: team-a
        source: {type: git, url: "ssh://git@git.example/infra.git",
                 branch: main, path: ./deploy, secret: infra-key}
```

- 16 entries and 64 items once the clusters are unfolded, at most.
- `clusters`: as in the Artefacts phase (a cluster of the Clusters phase,
  or an existing `<ns>/<name>`; `gitops-cluster-unknown` otherwise).
  Variables are allowed in `clusters` and in `source.registry`.
- `source.secret` names a secret of the plan. It is entered at launch in
  the Deploy window (or `gitops_secrets` in the secrets file of
  `harvester-plans`, `--secrets-file` or `--secrets-stdin`), never stored,
  and asked again on resume. A missing one blocks
  (`gitops-secret-missing`).
- An item depends on its cluster (`clusters:<c>`). A Flux item also waits
  for the chart item `flux` of the same cluster in the Artefacts phase of
  the plan (`gitops-flux-in-plan`, information). When neither the plan nor
  the cluster has Flux: `gitops-flux-missing`.
- Before the phase, the console replays the checks of each item: tool and
  source, Fleet and `privileges: cluster` for administrators only
  (`gitops-fleet-admin-only`, `gitops-privileges-admin-only`), artefact or
  chart published (`gitops-package-not-published`), entry laid by another
  origin (`gitops-taken`), and `gitops-check` on an existing child cluster.
- One child action per item: `capi-gitops:<ns>/<c>:<entry>`. An entry is
  marked `created` only when it carries the run of this chain; an entry
  laid by this plan is adopted and keeps the run that created it. A
  resumed run checks the items done with `gitops-status`.
- A disabled phase with entries only warns: the plan was written before.

**A chain that installs Harvester.** A plan chained to a Harvester install
(single install or batch) refuses at launch an external Rancher import
(`rancher_external` in a cluster template) and every Fleet entry:
`rancher-session-in-chain`, blocking. A freshly installed Harvester is not
known to your Rancher session. The install and batch windows ask for the
Git secrets of the chosen plan, and clear them once sent.

**Access options of cluster templates (2.8.0).** A cluster template
accepts the access options of the creation window (see
[operating-procedure.md](operating-procedure.md), Cluster API):

- `rancher_external: true`: the child cluster is imported into the
  Rancher of the session that runs the plan. With `rancher_import`
  (embedded Turtles) it is refused (`rancher-import-conflict`). An adopted
  cluster without the import is imported by the run.
- `oidc`: an object of strings (`issuer_url`, `audience`, claims,
  prefixes, `ca_pem`, `admin_group`), checked when its values are fixed.
- `offline: true` and `offline_artifacts` (`fetch` or `image`): a child
  cluster bootstrapped without the Internet. `offline_packages` (`skip`,
  the default, or `keep`, 2.8.1): repositories of the image set aside at
  the first boot, or kept for a mirror of the local network.
  When the Registry phase of the same plan lays the registry (mirror of
  `docker.io`, `no_fallback` on the cluster) or publishes the `rke2` item
  of the version, the launch check shows `offline-later` (information)
  instead of `offline-needs-no-fallback` or `offline-rke2-missing`. The
  Clusters phase checks them again before any creation (2.8.1).

### Undoing a deployment (2.7.0)

**History > Undo** on a finished run (not a dry run) removes what the run
and its resumes created, in reverse order: artefacts, child clusters, the
bootstrap registry the plan laid, the Terraform content. Never the
Harvester installation, never the Cluster API stack. Administrator only.

The window has three tabs, like Deploy:

- **Content**: what the chain created, by kind, with boxes. Unticked by
  default, with the reason in the tooltip: the bootstrap registry (it and
  its data outlive the plan, other clusters may pull from it), a
  declaration whose creation is not proven (a run older than 2.7.0), a
  declaration changed since it was applied. An item adopted without the
  mark of the chain is never listed.
- **Destruction plan**: what will go, read by the console without writing
  anything: objects of the child cluster (deletions judged by the server),
  the chart release, the machines, VMs, volumes and load balancers of a
  cluster (found by name: approximate) and the workloads inside it, the
  resources of the Terraform state, the registry and its volume. Limits
  said: volumes still claimed, load balancers whose address came from DHCP
  (harvester/harvester#11767), objects named after the cloud provider name
  `kubernetes` (CAPHV #277), the namespace of a chart release (removed only
  if empty and created for the chart). An object found under the name of
  an item but **without the run mark of this chain** (recreated since by
  another run, or by hand) is named here and left untouched.
- **Confirmation**: the name of the Harvester cluster typed; the data of a
  registry goes only on a second typed name (its volume is kept
  otherwise).

The Undo button stays greyed until the destruction plan of the current
boxes is back and the name matches, and sends one request only.

The undo is an action `deploy-undo:<cluster>` under the same cluster lock
as `deploy:` (a deployment and an undo never run together on a cluster),
with child actions `capi-service-remove:`, `capi-manifest-remove:`,
`capi-cluster-delete:`, `registry-delete:` and `tf-destroy:`, and since
2.8.0 `capi-gitops-remove:` and `capi-rancher-unimport:`.

**2.8.0: GitOps entries and imports.** Two more kinds: `gitops` (an entry
the run posed) and `rancher-import` (an import into the external Rancher
made by the run). The order becomes: gitops, artefacts, rancher-import,
clusters, registry, terraform.

- The artefacts and the import of a cluster wait for its GitOps entries;
  the cluster waits for its GitOps entries, its artefacts and its import.
  The Flux chart goes after the entries of its cluster.
- When the cluster of an import is undone too, the import step ends
  "left to the removal of its cluster": the deletion drops the import
  without reading the child cluster.
- A Fleet entry needs a Rancher session. Without one, its item fails
  with `rancher-session-needed`, never "already gone".
- Undo is refused while a machine renewal runs on the Harvester cluster
  (`renew-running`).

- **Order and dependencies.** A child cluster waits for the removal of its
  artefacts; the registry waits for every chosen cluster; each Terraform
  declaration waits for the chosen clusters and registry, because a
  network, a storage class or an image of the content can carry them. A
  failed item only stops what depends on it: a cluster whose chart could
  not be removed stays, and so does the Terraform content (phase
  `partial`).
- **Only what the chain created.** Each target is read again before it is
  removed: it must carry the `harvester-ops.io/plan-run` mark of a run of
  the chain. Anything else is `undo-not-ours`, left untouched. The
  removal tools read the mark again just before they write
  (`--expect-run`), so an object recreated in between is refused too. A
  declaration is designated by its identifier, never reused: it counts as
  ours by its id alone, so a declaration a later run of the same plan
  adopted is still destroyed by the undo of the run that created it.
- **Terraform content under a cluster that stays.** A declaration is never
  destroyed while a cluster that the Clusters phase of the chain names
  (created or adopted) is still present, ticked or not, ours or not
  (`undo-tf-carries-cluster`). The destruction plan names these clusters
  on the declaration. Remove the cluster first, or untick the declaration.
- **Registry delete stopped halfway.** The registry Deployment, which
  carries the run mark, goes last: a delete stopped halfway leaves it, and
  the undo finishes the removal when run again.
- **Resume.** Run the undo again on the same run: the items already gone
  are checked absent and skipped.
- **History.** The original run gets `undone_by`, `undone_at` and
  `undone_status`, shown as a badge: **Undone** once everything the chain
  created is gone (over successive undos), **Partially undone** otherwise
  (for example when the registry was left, the default). A run fully undone
  is refused (`undo-already-done`); an undo is never undone
  (`undo-of-undo`): apply the plan again to recreate what it made.
  Since 2.7.1, a deployment whose Clusters phase is partial and whose
  Artefacts phase then fails ends `partial`, not `error`: clusters were
  created, and Undo or a resume takes them as such.

Command line:

```
harvester-plans undo ACTION_ID --dry-run [--include registry,unproven,modified]
harvester-plans undo ACTION_ID --confirm CLUSTER [--include ...]
                     [--delete-registry-data NAME]
```

`--include` adds the categories unticked by default, each on its own.

**Seen for real (2.7.0).** On harvlab2, a plan with Terraform, a bootstrap
registry, one 1+1 child cluster and the Artefacts phase (base, account,
manifest, policy, chart) ran in about 13 minutes (cluster 623 s); stopped
twice in the Artefacts phase and resumed, the items done were checked and
skipped. Undo: a first pass left an item recreated outside the plan
(`undo-not-ours`) and so kept its cluster and the Terraform content; the
relaunch deleted the cluster (265 s) and the declaration; the registry
went in a third pass with its volume kept, then the run was refused as
`undo-already-done`. An undo whose cluster deletion was cancelled from
the dock finished when relaunched (2 min 11 s). On harv4, two child
clusters with a manifest and a chart each: deployment 19 min 20 s, undo
of everything 6 min 15 s. Decoys without the run mark survived every
undo. Details: [artefacts.md](artefacts.md), "Seen for real (2.7.0)".

### History of the bare-metal actions (2.1.0)

The **History** tab lists the bare-metal actions of Activity: installs and
joins (`baremetal-install`), batches (`baremetal-batch`), discovery boots
(`baremetal-discover`) and power gestures (`bmc-power`), the running ones
first. It uses the action filter of Activity, server side, so older actions
are found too. Each row links to the action in Activity
(`#activity/action/<id>`), where its log opens. While an action runs, the
tab reads it again every five seconds.

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

The console sets the port with `HARVESTER_OPS_PXE_PORT` (default 8091).
It never picks another port by itself: the address given to the BMC and
the installer must be the one you opened. If the port is taken, the run
fails at its `serve` step with a message naming the port, before any
power action. The listener starts with the first installation or
discovery and stops when the last one ends.

Any failure of a run, expected or not, ends the action as failed. The
served configuration (it holds the cluster token), the remastered ISO and
the tokens are removed; the step message gives the error type only, never
a path or a secret.

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

**"no virtual media" on the machine row.** The BMC exposes no CD device or
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

### Static management networking and bonded NIC identity

The installer downloads its configuration before applying the network in
that file. The console therefore derives the initial dracut network from
the rendered management settings: interface mappings, bond options, VLAN,
static IPv4 address or DHCP, gateway and DNS. Preview includes
`boot_network_args`; conflicting network arguments are rejected before
creating an installation action.

On a multihomed console, virtual media uses the address routed toward the
BMC, while configuration and installation-image downloads use the address
routed toward the planned static management IP. API callers can override
these: `advertise_host` sets the address for both sides, as before, and
`installer_advertise_host` sets the installer side only. The installer
address is derived from the route to the planned static IP only when
neither is set; for DHCP installations it defaults to the media address.
Both networks must be able to reach the corresponding HTTP address/port.

Bond slaves can share a current MAC address. Matching names against that
address during normal-system udev coldplug can rename the wrong slave
(`rename4`, then `interface not found`). For MAC-selected interfaces the
console gives the boot and installer the same names, matches existing bond
slaves by `bonding_slave/perm_hwaddr`, and writes equivalent final naming
rules for installed boots. It never changes the selected original MACs.
These names (`hvbootN`) and their `ifname=` arguments are only written into
the configuration served by the remastered ISO, which carries the matching
boot arguments. A configuration rendered for another boot path (your own
PXE, a nested test bench) keeps the selected MACs without these names.

The initrd overlay is only added when the bond has two or more
MAC-selected members: a single member shares its MAC with nobody. It is
verified against the known dracut ifname generator, identical in Harvester
1.8.2 and 1.9.0. An unknown generator is refused during ISO preparation,
before any power change; another ISO version needs verification first.
Hardware validation is not bypassed. The generic no-image-download timeout
is not proof of a hardware-check failure: inspect the actual console error.

## Optional VM/customer uplinks when joining a cluster

Import the join YAML as usual. In **VM/customer network**, enable **Configure VM
uplinks after joining**, choose an existing network configuration as the template,
and tick the separate customer NICs by MAC address. Discovery inventory, when
available, also shows their Linux names. Management and VM NICs must be distinct.
The template supplies the cluster network and all bond/link attributes, including
mode and MTU; its NIC names and node selector are replaced for the new host.

**Preview** shows the installer YAML followed by a separate post-join plan. This
plan is console orchestration, not a supported key in Harvester's installer YAML.
It is currently offered for joins to declared clusters, not cluster creation or
migration of existing hosts. Unchecked, the feature makes no extra API calls.

A catch-all configuration such as `vm-bond` must first be scoped to its existing
hosts under **Networks**. The install is refused if that configuration would
also select the new host. The wizard never rewrites existing network configurations.
Do not change active hosts' coverage when preparing that selector.

After the node joins, the console rechecks the actual node labels, template and
cluster network. The network-controller LinkMonitor must report each selected MAC
as exactly one free, physical NIC. Down links produce a warning and are allowed;
the network agent brings up the bond. The template bond MAC is not copied.
Disk pools run before this optional network step.
The console server-dry-runs and then creates one host-specific VlanConfig, using
`kubernetes.io/hostname`, and reports success only after its VlanStatus is ready.
No existing bond, management configuration, storage configuration or maintenance
state is changed. Missing inventory, changed templates and overlapping selectors
stop this step with an explicit error. The Harvester installation remains in place.

On a create timeout or readiness failure, inspect the named configuration under
**Networks**. There is no automatic reinstall, retry of an uncertain create,
or teardown of the possibly active VM bond. Complete the network configuration
there before using the host for VM workloads.
