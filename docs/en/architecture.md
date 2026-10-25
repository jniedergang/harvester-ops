# Architecture

```
Operator workstation
└─ harvester-ops container  (registry.suse.com/bci/python:3.11)
   │
   └─ Flask console  (host menu · action dock · SSE live feed · multi-host)
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

## Multi-host model

A host is a whole Harvester cluster or a KVM hypervisor, never a Harvester
node. Clusters are declared in `config.yaml` and in `clusters.d/` (console
declarations), KVM hosts in `kvm.d/`. Each CLI command (`--cluster <name>`,
`--host <name>`) loads exactly one host's context, and every mutation acts
on named objects of named hosts.

The console reads the fleet at once where a screen needs it: the side menu
(`GET /api/hosts`), Virtual machines (`/api/vms-all?kinds=harvester,kvm`),
Containers (`/api/containers-all`), GPU (`/api/gpus-all`) and the folder
counts. These reads are shared between people who would get the same
answer (`shared_read`): the key carries the kubeconfig rendered for the
person when the route names a cluster, and since 2.0.0 the identity the
person presents to the clusters (Rancher session, or delegated user and
groups, `_read_share_identity`) when it does not. Local accounts without
identity delegation all use the console's kubeconfig and still share one
read per role. KVM reads are the exception: the console reaches every KVM
host with its own SSH key, so the answer does not depend on who reads. The
KVM routes (`/api/kvm/...`) carry no identity in their key, and the KVM
part of `/api/vms-all` is read once for everybody and kept 10 seconds
(`HARVESTER_OPS_KVM_VMS_SHARE_TTL`); only its Harvester part is read per
identity. Thirty people on Virtual machines make at most one SSH survey per
KVM host every 10 seconds.

These fleet reads (`/api/vms-all?kinds=...`, `/api/containers-all`,
`/api/gpus-all`, `/api/forklift-global`) list only the clusters the person
sees, with the same rule as the hosts menu (`_fleet_clusters`): a cluster
Rancher hides from a Rancher session is left out, and its name appears
nowhere in the answer. `hidden_count` gives their number. A cluster that is
visible but refuses the read (RBAC, identity delegation) stays listed as
`denied`, as in the menu.

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
  then the tab, then the pane. It is the only way a screen opens: the side
  menu of hosts is made of real links, and the menu of 1.x (cluster selector
  and per-cluster groups) is gone since 2.0.0. A click inside a screen (a
  section tab) rewrites the address without adding a history entry; a menu
  link or a link from one view to another (`App.openHost`) adds one, so Back
  returns to the previous screen. An unknown host or a host hidden by Rancher opens the
  hosts index (`#hosts`) with a notice, and so does an old token that needs
  a host while none is visible; an address that matches no screen opens the
  default screen (`#vms`) with a notice. Never an empty page. An empty
  address opens `#vms`; the first time 2.0.0 opens in a browser, it reopens
  the last screen of the 1.x menu once instead (the tab 1.x kept), then
  forgets it.

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
| `#migrations`, `#gpus`, `#bookings`, `#usage`, `#automation/<capi, terraform or pxe>`, `#activity[/host/<kind>/<name>]`, `#activity/action/<id>` | fleet screens (an action: Activity filtered on it, its detail open) |

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

Modules never read a cluster selector (there is none since 2.0.0): they use
`App.getCurrentCluster()`, `App.listClusters()`, `App.managementCluster()`
(cluster that Cluster API and Terraform manage from, the last cluster
visited unless chosen) and `App.openHost(kind, name, sub, tab, pane)`.

### Pages of a host

A host page is a band (family, name, state, version, VM count, sub-menu,
Activity of this host) on top of the existing section that the address
selects; the sections themselves are not rewritten. On a Harvester host,
Parameters are tabs over the former Cluster sections (`Sections.DEF` gives
their panes); the Overview shows the metrics and, below, the Events block,
which polls `/api/events/<c>` every 20 s while the page is visible (one more
poller per viewer than in 1.x, where Events was a tab).

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

## Folders (2.0.0)

A folder files VMs (Harvester and KVM), container workloads (Deployment,
StatefulSet, DaemonSet, CronJob) and hosts. An object is in one folder only,
and nothing is inherited: filing a host does not file its VMs.

The folder of an object is written **on the object**, which is the source
of truth:

| Object | Where | Key in the cache |
|---|---|---|
| Harvester VM, container workload | annotation `harvester-ops.io/folder` (merge patch, `null` to remove) | `harvester/<cluster>/<ns>/<name>`, `container/<cluster>/<ns>/<kind>/<name>` |
| KVM domain | libvirt metadata `<hops:folder xmlns:hops="https://harvester-ops.io/xmlns/folder/1" path="..."/>` (`--config`, and `--live` when it runs) | `kvm/<uuid>` (a renamed domain keeps it) |
| Host | `folder` field of `clusters.d/<name>.yaml` or `kvm.d/<name>.yaml` | `host/harvester/<name>`, `host/kvm/<name>` |

The console database `folders.db` keeps the tree (`folders`: id, parent,
name, position, creator, `origin` = `console` or `objects` when a folder was
rebuilt from the objects that name it), a cache of the memberships
(`memberships`: key, path, `source` = `vm` read on the object or `console`
when the person may not write the object, `seen_at`, `scope` = the read that
saw it) and the journal of each folder action (`moves`, one line per object:
resume of an interrupted action, Undo). A path is absolute, 8 levels and 253
characters at most; a name has 1 to 63 characters, no `/`, no control
character, and is neither `.` nor `..`. Two siblings never share a name.

Every fleet read realigns the cache: a value on the object wins and creates
the folders it names; an object without a value keeps a `console` filing and
loses a `vm` one (someone removed it on the object). This write happens
during a read, without a tracked action: it is the documented exception, as
it only mirrors what the objects say. An entry not seen for 30 days is
removed, but only after a successful and complete read of its cluster or
host: an unreachable host keeps its entries, and so does a cluster read with
a refusal or by a person signed in through Rancher or under identity
delegation (such a read sees only part of the cluster, so the filings it
cannot see are not aged out).

KVM folders are read with one `virsh` invocation per host that chains the
`metadata` reads of every domain, in a second SSH call over the same
connection. Measured on a host with 15 domains (libvirt 12.8.0): the
inventory alone takes 1.7 s, plus 0.3 s with this read, against plus 2.7 s
for one `virsh metadata` command per domain and plus 2.9 s for one
`dumpxml` per domain. A virsh error other than "no metadata" makes the host
read again domain by domain, so a failure is never taken for "no folder".

A fleet read creates every folder named by objects, without a cap. A read that started before a rename or a
move ended (kubectl and SSH take seconds) still returns the former path: for
two minutes after the change, or while objects remain to rewrite, that path
is mapped to the new one instead of creating the former folder again.

Every change goes through `bin/harvester-folders.py` (see Capabilities); the
console launches it from the routes under `/api/folders`. In the browser,
`web/static/js/folders.js` (`window.Folders`) draws the panel of the
Virtual machines view and of the Folders view, and the Move to folder and
Move the content windows; `all-vms.js` is the Virtual machines view
(`#vms`, `#vms/folder/<id>`, `#vms/folder/unfiled`,
`#vms/host/<family>/<host>`); `FoldersView` (in `folders.js`) is `#folders`.

### Folder actions: checks, journal, undo, resume

Before a gesture, `POST /api/folders/check` tells object by object what will
happen: written on the object (`kubectl auth can-i` with the person's
identity, one review per cluster, namespace and resource; KVM host reached
and domain persistent; host declaration writable), filed in the console
only, or refused and why. The window shows these lines before its button.

Each folder action is an `ActionRun` (dock, Activity) that runs
`harvester-folders.py` with the person's role and kubeconfig. The tool
writes one `moves` line per object (`pending`, then `done`, `failed` or
`skipped`) with the value read on the object just before writing it, so:

- **Undo** (`POST /api/folders/actions/<id>/undo`) puts back that exact
  value; what the button needs is stored in the additive `undo` column of
  `actions.db`, so it survives a console restart, and Activity offers it
  after the dock card is gone.
- **Resume** (`POST /api/folders/actions/<id>/resume`) finishes only the
  `pending` and `failed` lines of an action that ended in error or was
  stopped by the console shutdown; a `done` line is never rewritten, and an
  object filed elsewhere since is left alone. The server adds a `resume`
  path to such an action while lines remain (`_folder_resume`), which the
  dock and Activity turn into a button. A folder action is recorded in
  `actions.db` when it starts, so one cut by a console stop comes back
  `interrupted` after the restart, with Resume and Undo.
- An object the person cannot see (hidden cluster, unreadable namespace) is
  never named in the steps, the output or `--json`: it is counted ("2 other
  objects not shown to you"), and its real key stays in the journal only.

## Disaster Recovery screens (2.0.0)

The replication screens of 1.101.0 (`web/static/js/replication.js`,
`replication-plan.js`, `replication-create.js`...) are placed in the
navigation, not rebuilt: `#dr` lists the contracts, `#dr/<id>` opens one
contract's window on top of the list (closing it gives back `#dr`), and
`#dr/links` lists the links between sites. A Harvester host's Disaster
Recovery (`#harvester/<c>/dr`) reads `GET
/api/replication/hosts/harvester/<c>/resources` and shows the contracts
where the host is the source or the recovery site; `#kvm/<h>/dr` says that
KVM replication is not available yet and reads nothing. The DR badge of a
host line in the side menu counts its alerts as source and as recovery
site.

The browser holds no state rule: every button follows the `actions` of the
contract row (`plan_row()` and `resource_row()` in
`bin/lib/replication.py`), each with its `role`, `allowed` and `reason`.
The contract window's **Resume command** follows a `resume-command` action
that the console adds to the row when `replication_run.resumable` says the
`resume` command has something to finish, with the role of the gesture it
finishes (admin for a failover).

## Console data stores

| Store | Content | Written by |
|---|---|---|
| `actions.db` (SQLite WAL) | every `ActionRun`, its events and result, `undo` (2.0.0, additive column) | the console |
| `folders.db` (SQLite WAL, 0600 with its `-wal` and `-shm`) | `favorites`, `folders`, `memberships` (cache), `moves` (journal) | the console (favourites, cache realignment during a read), `harvester-folders.py` (everything else) |
| `replication.db` (SQLite WAL) | contracts, their VMs and volumes, passes, measures, runs, events | `harvester-replication.py`, its follower |
| objects | annotation `harvester-ops.io/folder`, libvirt metadata, `folder` field of a host declaration | `harvester-folders.py` |

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
