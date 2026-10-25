# Architecture

```
Operator workstation
└─ harvester-ops container  (registry.suse.com/bci/python:3.11)
   │
   └─ Flask console  (sidebar tabs · action dock · SSE live feed · multi-cluster)
        │
        ├──▶ bin/harvester-{shutdown,startup,status}.sh  — power-sequencing engine (SSE)
        ├──▶ kubectl  — VM lifecycle, topology, status
        ├──▶ CAPHV / clusterctl  — downstream RKE2 clusters
        ├──▶ terraform  — declarations (IaC)
        └──▶ Redfish  — BMC discovery + power

   auth     ▶ Basic auth + TLS (browser) · kubectl + ssh (clusters) · redfish (BMCs)
   reaches  ▶ Harvester clusters (prod, staging, …): control-plane + workers,
              Harvester + Longhorn + KubeVirt
```

## Two kinds of operation

harvester-ops drives clusters through **two paths**, and the distinction
matters for trust and for airgap:

1. **Power sequencing** goes through the **bash scripts only**. The
   console spawns them; it never reimplements shutdown/startup logic. This
   is the auditable, UI-independent core.
2. **Everything else** (VM lifecycle, topology, Cluster API, Terraform,
   bare-metal) is the Flask console talking to **`kubectl` / providers /
   Redfish directly**. These surfaces exist only in the console.

So the strong guarantee — *"the UI never bypasses the scripts"* — applies
to **power sequencing**, the one place where getting the order wrong can
lose data. The higher-level surfaces are conventional API clients.

## Component responsibilities

### Bash scripts (`bin/`)

- The **sole execution path for power sequencing**. The console never
  bypasses them for shutdown/startup.
- Self-contained: depend only on `bash`, `kubectl`, `ssh`, `yq`,
  `python3` (a few jq-style transforms).
- Emit structured `STEP_EVENT|<id>|<status>|<msg>` lines on stderr for the
  console to parse into SSE events.
- Read configuration from `/etc/harvester-ops/config.yaml`.

### Helper tools (`bin/`, Python)

- `harvester-provider-install.py` installs or updates the Terraform
  provider from a version, a URL or a local file. Standard library only,
  so it runs on an airgapped host where nothing but `python3` and the
  extracted tarball exist.
- `harvester-vm-transfer.py` (1.45.0) checks, moves, exports and imports a
  VM between clusters. Its decisions live in `bin/lib/vm_transfer.py`
  (pure, tested without a cluster), its two engines in
  `bin/lib/vm_transfer_run.py` (driving the clusters through an injectable
  client), the HTTP counter that serves disks to CDI in
  `bin/lib/vm_transfer_serve.py`, and the Longhorn allocatable space it
  shares with the console in `bin/lib/longhorn_room.py`.
- Same `STEP_EVENT` contract as the bash scripts, so the console streams
  them through the identical SSE path. The console spawns this script
  rather than reimplementing the install, which is what keeps the CLI and
  the UI at parity.

### Common library (`bin/lib/common.sh`)

- Logging (colored TTY + plain log file).
- `confirm()` / `interactive_pause()` for human-in-the-loop mode.
- `run` wrapper honouring `--dry-run`.
- Cluster loader (parses YAML, sets `KUBECONFIG`, SSH options, node list).
- VM helpers: `get_ordered_vms`, `snapshot_vm`, `wait_for_vm_ready`,
  `set_vm_priority`.
- `emit_event()` for SSE-compatible step events.

### Flask console (`web/`)

- **One Flask app per container**, serving all configured clusters.
- **Power-sequencing surface**: spawns the bash scripts via
  `subprocess.Popen` and streams their stderr through SSE.
- **Direct-API surfaces**: VM lifecycle, cluster topology and status,
  Cluster API / CAPHV install + cluster CRUD, Terraform declarations,
  BMC / Redfish — implemented as `kubectl` / provider / Redfish calls.
- **Action model**: every mutating call becomes an `ActionRun` with a
  live event log, surfaced in the persistent dock and retained in a
  SQLite history (WAL mode).
- **Collaborative notes**: a WebSocket sync endpoint backs the Yjs notes,
  persisted in SQLite.
- **Observability**: Prometheus `/metrics`, `/healthz` (liveness),
  `/healthz/ready` (readiness).
- **State**: only the action history and notes are persisted; the app is
  restartable at any time. Auth is HTTP Basic (htpasswd) over TLS; OIDC
  is a candidate for a future version.

### Container image

- Base: `registry.suse.com/bci/python:3.11`.
- Includes `kubectl`, `yq`, `openssh-clients`, `xorriso`, OpenTofu (the
  open, compatible equivalent of Terraform, MPL-2.0), the Python wheels
  (offline), the bash scripts, and the console.
- Built via `container/Containerfile`, saved as an OCI tar
  (`images/harvester-ops-ui.tar`) for airgap delivery.
- **Ready to use (1.51.0)**: the deliverable also carries the Cluster API
  bundle of the moment (providers and their images, airgap) and the
  Terraform provider for Harvester, pinned in
  `scripts/embedded-providers.env`. `install.sh` places them in the
  service's persistent state (`/var/lib/harvester-ops`), active at first
  start; a bundle or provider the operator chooses later is never
  overwritten by a new installation.

## Multi-cluster model

A single `config.yaml` declares N clusters. Each CLI command
(`--cluster <name>`) loads exactly one cluster's context. The console
sidebar lists every cluster; switching only changes the active context —
there is no cross-cluster fan-out operation.

## Navigation and addresses

Every screen of the console has an address after `#`, so a link opens the
exact screen and a reload (F5) gives it back. Two browser modules carry it:

- `web/static/js/routes.js` is pure (no DOM, no network, tested in node). It
  reads an address into a route `{name, kind, host, sub, tab, pane, id}`,
  writes it back (`encodeURIComponent` on every segment), and translates the
  old tokens (`#storage`, `#allvms`, `#kvmhosts`...), which stay understood.
  Host names are checked with the server's declaration rule (letters,
  digits, `.`, `_`, `-`), the same for a Harvester cluster and a KVM host.
- `web/static/js/router.js` applies an address: it sets the cluster first,
  then the tab, then the pane. A click on a menu entry, a section tab, an
  overview tab or the cluster selector rewrites the address without adding
  a history entry; a link
  from one view to another (`App.openHost`) adds one, so Back returns to the
  previous screen. An unknown host, a host hidden by Rancher, or an address
  that matches no screen opens the default screen with a notice, never an
  empty page. An old token that needs a host while none is visible opens
  its old page with a notice. An empty address restores the last screen.

Address grammar (`<c>` = Harvester cluster, `<h>` = KVM host):

| Address | Screen |
|---|---|
| `#vms`, `#vms/folder/<id>`, `#vms/host/<harvester or kvm>[/<name>]` | virtual machines |
| `#folders[/<id>]`, `#containers[/folder/<id>]`, `#hosts[/harvester, /kvm or /folder/<id>]` | folders, containers, hosts |
| `#harvester/<c>`, `#harvester/<c>/overview/events` | overview of a cluster |
| `#harvester/<c>/vms`, `#harvester/<c>/containers`, `#harvester/<c>/dr` | VMs, containers, disaster recovery of a cluster |
| `#harvester/<c>/imports/<vmimport or forklift>[/<pane>]` | imports and VMware migrations |
| `#harvester/<c>/params/<tab>[/<pane>]` | settings: `nodes`, `storage`, `network`, `addons`, `security`, `monlog`, `advanced`, `upgrade`, `power`, `conn` |
| `#kvm/<h>`, `#kvm/<h>/vms`, `#kvm/<h>/dr`, `#kvm/<h>/params/<system, pools, networks, labels or conn>` | KVM host |
| `#dr`, `#dr/<plan>`, `#dr/links` | replication contracts, links |
| `#migrations`, `#gpus`, `#bookings`, `#usage`, `#automation/<capi, terraform or pxe>`, `#activity[/host/<kind>/<name>]` | fleet screens |

Two more modules carry the 2.0 menu: `web/static/js/host-nav.js` (the side
menu of hosts, fed by `GET /api/hosts`, pure ordering and search tested in
node) and `web/static/js/host-page.js` (the band of a host page, Harvester
or KVM, on top of the section the address selects). The router calls
`HostNav.sync(route)` and `HostPage.show(route)` after each screen. On a KVM
host page the KVM views get the host from the route (`KVM.start(view, el,
{host})`): their host filter is fixed and hidden. Its Parameters > System
reads `GET /api/kvm/hosts/<h>`; Labels and Connection read `GET
/api/kvm/hosts/<h>/declaration` (no SSH, pinned fingerprints, never a key
path); labels are written by `PUT /api/kvm/hosts/<h>/labels` (admin), a
tracked action of `harvester-kvm.py host label`.

Modules never read the cluster selector directly: they use
`App.getCurrentCluster()`, `App.listClusters()`, `App.managementCluster()`
(cluster that Cluster API and Terraform manage from, the last cluster
visited unless chosen) and `App.openHost(kind, name, sub, tab, pane)`.

### Hosts list and favourites

`GET /api/hosts` gives the side menu its hosts: every Harvester cluster the
person sees (a Rancher session does not get the clusters Rancher hides) and
every declared KVM host, with a state (`ok`, `unreachable`, `denied`,
`error`, `unknown`), the number of VMs, the version, the labels, the origin
(`config` or `console`) and whether the person starred it. Each family gets a
summary (`ok`, `alert`, `unknown`). There is no cap: every host is listed.

The list never reads a host. A cluster's state comes from the cached TCP
probe of its API server; its VM count and version come from the shared reads
the screens already made (`/api/vms-all`, `/api/status/<c>`,
`/api/upgrade/<c>`, at most 10 minutes old, `HARVESTER_OPS_HOSTS_PEEK_AGE`);
a KVM host's state and VM count come from the last `/api/kvm/hosts` read.
What has not been read yet is `null` or `unknown`.

Favourites are a personal preference: `PUT` and `DELETE
/api/me/favorites/<kind>/<host>` (any role, like changing one's own
password), stored per person in the console database `folders.db` (next to
`notes.db`, or `HARVESTER_OPS_FOLDERS_DB`), found again from another
computer. They are not tracked actions and have no command line: nothing is
written on a cluster or a hypervisor.

## SSE event stream

When the console triggers any action (a shutdown here):

```
client                          Flask                       bash script
  │ POST /api/action              │                              │
  │ ───────────────────────────▶  │                              │
  │                               │ subprocess.Popen             │
  │                               │ ──────────────────────────▶  │
  │ GET /api/stream/<id> (SSE)    │                              │
  │ ───────────────────────────▶  │                              │
  │                               │ read stderr line-by-line     │
  │                               │ ◀──── "STEP_EVENT|vm-stop|   │
  │                               │        running|..."          │
  │ event: step                   │                              │
  │ data: {"id":"vm-stop",..}     │                              │
  │ ◀───────────────────────────  │                              │
```

The frontend (vanilla JS + an auto-reconnecting `EventSource` wrapper)
updates step indicators in real time without polling. Direct-API actions
(VM ops, Terraform apply, CAPHV install) use the same `ActionRun` + SSE
mechanism, streaming step/log events as they progress.

## Trust model

- The container holds full `cluster-admin` access via mounted kubeconfigs
  and SSH keys, and can reach BMCs over Redfish.
- It should run on a **trusted operator workstation** or a dedicated jump
  host.
- Path params that map to Kubernetes names are validated (RFC 1123) at the
  HTTP boundary; mutating endpoints carry auth + rate-limiting; kubeconfigs
  are staged with `0600` permissions per Terraform workspace.
- Basic auth + TLS protect the console. For higher trust, place it behind
  a reverse proxy with mTLS or OIDC.
