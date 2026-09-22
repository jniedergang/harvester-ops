# harvester-ops

**Shut a SUSE Harvester cluster down cleanly, bring it back in the right
order, and run it day to day from one console.**

English · [Français](README.fr.md)

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/jniedergang/harvester-ops)](https://github.com/jniedergang/harvester-ops/releases/latest)
[![Tests](https://img.shields.io/badge/tests-1230%2B_passing-green.svg)](tests/)

> Independent open source project. Not affiliated with, endorsed by, or
> supported by SUSE.

[![A tour of harvester-ops](docs/assets/video/tour-en.webp)](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/tour-en.mp4)

*A tour of the console, going down the left-hand menu. Click the picture
for the full video.*

## Why

A Harvester cluster does not like being switched off at the wall. Stopping
it properly means taking an etcd snapshot, stopping the virtual machines in
a sensible order, waiting until Longhorn has let go of every volume,
cordoning the nodes, then powering them off with the control plane last.
Starting it again is the same list backwards, waiting at each stage for
the cluster to be ready for the next.

harvester-ops does both. From a command line that works on an airgapped
site, or from a web console that shows each step as it happens and stops
if a check fails. Around that core it has grown into a day-2 console for
Harvester: cluster and node maintenance, virtual machines, storage,
network, Cluster API, Terraform and bare-metal installation, for several
clusters at once.

## Watch it work

Short clips filmed on a real three-node cluster, with captions. Waits are
sped up, with the factor shown on screen; nothing is cut.

| Clip | What you see | Watch |
|---|---|---|
| **Tour** | Every entry of the menu, top to bottom, and the five interface languages | [English](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/tour-en.mp4) · [Français](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/tour-fr.mp4) |
| **Shutdown and startup** | A whole cluster powered off in order, then brought back | [English](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/shutdown-startup-en.mp4) · [Français](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/shutdown-startup-fr.mp4) |
| **Node maintenance** | The pre-check, a host drained while its VMs live-migrate, then a VM moved by hand | [English](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/cluster-maintenance-en.mp4) · [Français](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/cluster-maintenance-fr.mp4) |
| **Virtual machines** | The VM list, guided creation, and the built-in console | [English](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/vms-console-en.mp4) · [Français](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/vms-console-fr.mp4) |
| **Storage** | Space really left, and a degraded volume explained while it rebuilds | [English](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/storage-en.mp4) · [Français](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/storage-fr.mp4) |
| **Network** | Networks, the physical fabric, and the path of a VM | [English](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/network-en.mp4) · [Français](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/network-fr.mp4) |
| **Snapshots** | A VM snapshot taken, the VM changed, and restored | [English](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/snapshots-en.mp4) · [Français](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/snapshots-fr.mp4) |
| **Activity** | Every action on record with its log, and a support bundle | [English](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/activity-en.mp4) · [Français](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/activity-fr.mp4) |
| **Several clusters** | Switching clusters, one that is powered off, roles and accounts | [English](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/multi-cluster-en.mp4) · [Français](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/multi-cluster-fr.mp4) |

## What it does

### Power sequencing, the core (command line and console)

- **Graceful shutdown in eight steps**: pre-flight checks, etcd snapshot,
  optional VM snapshots, VMs stopped in configurable groups, Longhorn
  volumes detached, nodes cordoned, workers then control plane powered off.
- **Startup in five steps**: first control-plane node, then the others
  (by Wake-on-LAN when a MAC address is declared), nodes Ready, cluster
  state restored, VMs restarted in reverse order.
- **Safety nets**: a failed check (etcd snapshot, a volume still attached)
  stops the sequence unless you force it; only the VMs the shutdown
  stopped come back, with their original run strategy; one shutdown or
  startup at a time per cluster, from the console or the command line.

### Day-2 console

- **Cluster view**: one block per host with its CPU and memory gauges and
  the VMs it runs. Node maintenance with a pre-check that says which VMs
  will migrate, which would stop and what would hold the drain, then the
  drain followed to the end.
- **Virtual machines**: create with the space actually left checked as you
  type, edit every section, templates, snapshots and restore, live
  migration, bulk actions, and a VNC console several people can watch at
  once.
- **Storage**: classes, volumes and node disks, allocatable space as
  Longhorn computes it, and degraded volumes diagnosed with the fixes that
  are safe.
- **Network**: the networks and what is attached to them, the physical
  fabric (cards, bonds, virtual switches), a VM's path end to end, LLDP.
- **Activity**: every operation that changes something is recorded with
  its live log and kept in the history.

### Automation

- **Cluster API (CAPHV)**: install the stack from an airgap bundle, list,
  scale and delete downstream RKE2 clusters, fetch their kubeconfig.
  Creating a cluster needs the `caphv-generate` tool on the host.
- **Terraform**: saved declarations (VMs, images, SSH keys, raw HCL), plan
  shown before apply, destroy, and the Harvester provider installed or
  updated from the console.
- **Bare-metal**: find machines by their management board over Redfish,
  power them, keep an ISO store, and install Harvester on a blank machine
  unattended, over virtual media.

### Made for production sites

- **Airgap**: one tarball with its SHA-256 checksum, the Python wheels and
  the container image inside; nothing is fetched at run time.
- **Several clusters**, including clusters that are powered off.
- **Roles and identities**: viewer, operator and admin, deny by default,
  and actions carried out on the cluster under the operator's own identity.
- **Five languages** (English, French, German, Spanish, Italian), a tooltip
  on every control, light and dark themes.
- Prometheus metrics, a readiness probe, and an anonymised support bundle.

## Screenshots

| | |
|---|---|
| [![Cluster view](docs/assets/cluster.png)](docs/assets/cluster.png) | [![Virtual machines](docs/assets/vms.png)](docs/assets/vms.png) |
| Cluster view, with a node maintenance pre-check | Virtual machines of a namespace |
| [![Graceful shutdown](docs/assets/shutdown.png)](docs/assets/shutdown.png) | [![Storage](docs/assets/storage.png)](docs/assets/storage.png) |
| The eight shutdown steps | Storage, with a volume being rebuilt |
| [![Network fabric](docs/assets/fabric.png)](docs/assets/fabric.png) | [![VNC console](docs/assets/console.png)](docs/assets/console.png) |
| Physical fabric of the cluster | Built-in VNC console |
| [![Activity](docs/assets/activity.png)](docs/assets/activity.png) | [![Bare-metal](docs/assets/baremetal.png)](docs/assets/baremetal.png) |
| Activity: every action and its log | Bare-metal: Redfish discovery and unattended install |

## Quick start

```bash
# On the operations workstation
tar xzf harvester-ops-<version>.tar.gz
cd harvester-ops-<version>
sudo ./install.sh                      # interactive installer
sudo $EDITOR /etc/harvester-ops/config.yaml

# Command line: the power-sequencing core, fine on an airgapped site
harvester-status   --cluster prod
harvester-shutdown --cluster prod --interactive
harvester-startup  --cluster prod

# Web console, if you installed it
xdg-open https://localhost:8090
```

Every command takes `--dry-run`, which shows what would happen and touches
nothing. See [install](docs/en/install.md) and the
[operating procedure](docs/en/operating-procedure.md).

## How it is built

- **Two surfaces, one engine.** The sequencing lives in auditable bash
  scripts (`bin/`), needing only `kubectl` and `ssh`. The console (Flask,
  plain JavaScript, no framework) runs those same scripts and never goes
  around them.
- **Every change is an action**: it gets an id the moment it starts, its
  steps stream live to the browser, and its log is kept.
- **Delivered as one tarball** built on SUSE BCI, installed as a systemd
  service that runs a read-only container under an unprivileged account.

## Tested on real clusters

Unit and browser tests run on every change (over 1,000 backend tests and
200 browser tests). That is not enough on its own: each feature is also
exercised on a real cluster before release, on a single-node production
cluster and on a three-node test cluster for what needs several nodes
(maintenance, migration, a full shutdown and startup). The clips above
were filmed on that test cluster, and filming them found and fixed
defects in node maintenance, the shutdown and the startup that no
single-node cluster could show; the [changelog](CHANGELOG.md) tells each
story.

## Documentation

[Capabilities](docs/en/capabilities.md) ·
[Operating procedure](docs/en/operating-procedure.md) ·
[Architecture](docs/en/architecture.md) ·
[Install](docs/en/install.md) ·
[Bare-metal](docs/en/bare-metal.md) ·
[Troubleshooting](docs/en/troubleshooting.md) ·
[Changelog](CHANGELOG.md)

<details>
<summary>What the tarball contains</summary>

```
harvester-ops/
├── README.md, README.fr.md, VERSION, CHANGELOG.md, LICENSE
├── install.sh, uninstall.sh        interactive installer
├── bin/
│   ├── harvester-shutdown.sh       shutdown engine (8 steps)
│   ├── harvester-startup.sh        startup engine (5 steps)
│   ├── harvester-status.sh         cluster state (text or JSON)
│   └── lib/common.sh               logs, dry run, live step events
├── web/                            the console (optional)
│   ├── app.py
│   ├── templates/, static/         plain JavaScript interface
│   └── vendor/                     Python wheels, for airgapped sites
├── container/Containerfile         FROM registry.suse.com/bci/python:3.11
├── images/harvester-ops-ui.tar     OCI image (podman load)
├── config/                         example config, systemd unit
└── docs/en/, docs/fr/
```

</details>

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the layout, the setup and the
release flow. Every change comes with a test, a `VERSION` bump and a
`CHANGELOG.md` entry.

## License

Apache License 2.0, see [LICENSE](LICENSE).
