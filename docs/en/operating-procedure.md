# Operating procedure — Harvester graceful shutdown / startup

> Target audience: cluster operators with `cluster-admin` access and physical (iLO/iDRAC/IPMI) access to the nodes.

## 1. Theory of operation

A Harvester cluster combines:

- **RKE2 / K3s** — the Kubernetes control plane (etcd, kube-apiserver, etc.)
- **KubeVirt** — virtual machines as `VirtualMachine` (VM) / `VirtualMachineInstance` (VMI) resources
- **Longhorn** — distributed block storage with synchronous replicas

A graceful shutdown must satisfy three invariants:

1. **No data loss**: every Longhorn volume must be `detached` before its host node is powered off, so all replicas converge to the same state.
2. **No etcd corruption**: at least one control-plane node must hold a recent etcd state at restart; quorum must not be lost mid-shutdown.
3. **No VM state inconsistency**: guests should receive an ACPI shutdown (graceful) before storage detaches.

The toolkit enforces these invariants by sequencing operations and verifying state between steps.

## 2. Shutdown sequence (the 8 steps)

| # | Step | What it does | Why it matters |
|---|---|---|---|
| 0 | Pre-flight | API reachable, nodes Ready, Longhorn present | Detect a broken cluster before causing more damage |
| 1 | etcd snapshot | `etcdctl snapshot save` on a CP node | Disaster-recovery insurance |
| 2 | Stop VMs | Patch each `VirtualMachine` with `spec.runStrategy: Halted` | Triggers ACPI shutdown of the guest |
| 3 | Wait VMIs gone | Poll until no `VirtualMachineInstance` remains | Confirms guests are fully off |
| 4 | Longhorn maintenance | Set `concurrent-replica-rebuild-per-node-limit=0`, wait for `volumes.status.state == detached` | Prevents unnecessary rebuilds during/after shutdown |
| 5 | Cordon nodes | `kubectl cordon` every node | Prevents rescheduling during the shutdown window |
| 6 | Shutdown workers | `ssh <worker> 'sudo shutdown -h +0'` | Workers can go first — they hold no quorum |
| 7 | Shutdown control-plane | Same, in reverse hostname order, with `--node-shutdown-delay` between each | The last CP standing keeps the most recent etcd state |

> **Check the machines are really down.** Until 1.44.10 the sequence could
> report success having reached only the first control-plane. After a
> shutdown, confirm with the management boards, or with a ping, that every
> node is off before cutting the power.

## 3. Startup sequence (the 5 steps)

| # | Step | What it does |
|---|---|---|
| 1 | Power on first CP | Operator powers on the **last CP shut down** via iLO/iDRAC/IPMI. The script then waits for `/readyz` on the API |
| 2 | Power on remaining nodes | Other CPs, then workers, one at a time |
| 3 | Wait Nodes Ready | Poll until every node reports `Ready` |
| 4 | Restore cluster state | Re-enable Longhorn rebuild, `uncordon` all nodes |
| 5 | Restart VMs | Set every `runStrategy: Halted` VM back to `Always` |

## 4. Running the procedure

> **One sequence at a time.** A cluster runs at most one shutdown or startup
> at a time. The console refuses a second one and says which action is
> already running; the scripts do the same through a lock kept next to
> their logs, so the command line and the console see each other. Dry runs
> are never blocked.

### Recommended: interactive mode

For the first execution against a cluster, **always use `--interactive`**. The script pauses between every step so the operator can verify state and abort if anything is unexpected.

```bash
harvester-shutdown --cluster prod --interactive
```

### Dry-run

Show what would be done without touching the cluster:

```bash
harvester-shutdown --cluster prod --dry-run --yes
```

### Batch mode (e.g. UPS trigger)

For automated unattended shutdowns (UPS battery low, scheduled maintenance):

```bash
harvester-shutdown --cluster prod --yes \
                   --skip-etcd-snapshot \
                   --vm-timeout 180
```

⚠️ **Never run without `--interactive`, `--yes`, or `--dry-run`.** The script refuses to operate without an explicit non-interactive flag, by design.

## 5. Observability

Each execution produces:

- A **structured log** in `/var/log/harvester-ops/<timestamp>-<cluster>-<action>.log`
- **Stream events** in the form `STEP_EVENT|<step-id>|<status>|<msg>` (consumed by the web UI for live updates)
- **Exit codes**: `0` success, `1` aborted by operator or fatal error, `2` configuration error

The web UI re-streams the same events via Server-Sent Events (SSE) for real-time progress.

## 6. Multi-cluster

The configuration file `/etc/harvester-ops/config.yaml` declares all known clusters.
Every command requires `--cluster <name>`. The web UI lets you switch clusters from a left-hand sidebar.

To list configured clusters:

```bash
yq '.clusters[].name' /etc/harvester-ops/config.yaml
```

## 7. Per-namespace operations

When you don't want to shut down the whole cluster, but only stop all VMs of a single tenant/namespace:

```bash
harvester-status   --cluster prod --namespace tenant-a
# (no per-namespace shutdown in v1.0 — use the web UI's namespace tab)
```

In the web UI, the **Namespaces** tab lists each namespace with its VMs and lets you stop/start them as a group.

## 8. Real-time monitoring (Activity dock)

Every action you trigger from the web UI appears in the **bottom dock** — always visible across all tabs (toggleable from the Activity tab).

- Running actions show a live chronometer (`⏱ 12s elapsed`), a mini progress bar and the current step.
- Click `▸` on any card to expand a real-time log tail (step events + script output).
- Completed actions stay visible for **30 seconds** as ✓ done (or ✗ error) with their total duration and exact end time, then move to the Activity history.

Per-VM operations (Start/Stop, runStrategy change, bulk actions from the Namespaces tab) are also tracked: you can follow the kubectl patch and the VMI phase wait in real time.

## 9. Anonymized support bundles & de-anonymization

When you build a support bundle with the **Anonymize sensitive data** checkbox enabled:

- Sensitive identifiers are replaced with unambiguous placeholders: `<<NODE-1>>`, `<<IP-NODE-1>>`, `<<CLUSTER-1>>`, `<<VM-1>>`, `<<NS-1>>`. These tokens cannot be mistaken for real values, guaranteeing lossless de-anonymization later.
- The full correspondence table is included in the archive as `mapping.json` and is also downloadable separately right after the bundle is built (button **🗝 Download mapping table**).
- Keep the mapping file safe: it is the **only** way to map placeholders back to real cluster identifiers later.

To restore original names in a log file that was returned to you by a third party:

1. Settings → Support → **De-anonymize a log file**
2. Upload the modified log + the original `mapping.json`
3. Click *De-anonymize and download* → the restored file downloads automatically. The HTTP response header `X-Replaced-Entries` indicates how many placeholders were substituted.

## 10. Multi-cluster management from the UI

To add a new cluster without editing `config.yaml` by hand:

- Settings > **Clusters** tab > *Add cluster*, OR **Add a host** under Hosts in the side menu, then **Harvester cluster**.
- Provide: a unique name, a description, the SSH user (defaults to `rancher`), the list of nodes (hostname + IP + role: `control-plane` or `worker`), and upload the kubeconfig + the SSH private key as files.
- The new cluster is persisted in the console's state directory (`/var/lib/harvester-ops`, `HARVESTER_OPS_STATE_DIR`), never in `config.yaml`: `clusters.d/<name>.yaml` (mode 0600, atomic write), with paths relative to that directory so that copying it moves the console (see "Moving harvops to another host" in the install guide).
- The kubeconfig is stored at `<state>/kubeconfigs/<name>.yaml` (mode 0600); the SSH key at `<state>/ssh/<name>_id` (mode 0600).
- Each cluster shows who declared it: `config.yaml` (the operator) or `console`. When the console cannot write `config.yaml` (the packaged service mounts `/etc/harvester-ops` read-only), the clusters of `config.yaml` cannot be changed or deleted from the UI (the API answers 409): edit the file. When it can (a development setup), their changes are still written there, with a `.bak` of the previous version.
- From the Clusters tab you can also: replace the kubeconfig, replace the SSH key, run **Test kubeconfig** (kubectl version ping) or **Test SSH** (per-node SSH probe), edit metadata, or delete the cluster (which also removes its files).

## 11. Safety design

- **Two-factor consent**: destructive operations require both a non-interactive flag (`--yes`) AND a final confirmation prompt unless `--yes` is explicitly set.
- **Idempotency**: re-running the script after a partial failure picks up where it left off (e.g. VMs already stopped → skip step 2's wait).
- **No silent failure**: every step exits non-zero on error unless the operator explicitly chooses to skip.
- **Auditable**: every command is logged with timestamp and exit code.

## 12. Disaster recovery (asynchronous replication)

What it does, the objects it creates and its limits: [capabilities.md,
section 11](capabilities.md#11-disaster-recovery-asynchronous-vm-replication-cli--console-api).
Every command below is `harvester-replication` (add `--json` for scripts).

**Before anything.** Both clusters use the same backup target (Harvester
setting `backup-target`), and it lives on neither site. Run the console
that follows the plans on the backup site or outside both sites, never
only on the source site: it would be lost with it.

**From the console.** The same steps exist in Disaster Recovery >
Contracts: **New contract** (blocks 1 to 5, measure first or protect
directly), then the contract window (Check, Measure, Protect, the Charts
and Journal tabs) and **Failover...** (admin, contract name typed). See
[capabilities.md, Disaster Recovery screens](capabilities.md#disaster-recovery-screens).

**First protection.**
1. `plan create --name web --from harv1 --to harv2 --vm default/web-01
   --rpo 15m --map-net default/prod=default/prod --map-sc
   harvester-longhorn=harvester-longhorn`. Map every network and storage
   class the VMs use. An image disk without `--map-image` takes the class
   its image was built on, through `--map-sc`.
2. `check web` until it shows no `block` finding.
3. `measure start web --interval 5m`, wait three passes, `measure stop
   web`, then `measure report web` for the recommended interval.
4. `protect web --dry-run`, then `protect web` (it takes the measured
   interval unless `--interval` is given; `--retain N` sets the number of
   backups kept).
5. `status web` (exit 4 means an objective is exceeded) and `events --plan
   web`. In the console, the actions are in the dock and Activity.
6. `check web --phase failover` runs the failover as a dry run, writing
   nothing.

**While protected.** The console follower applies each backup on the
target. `plan set web --paused true` pauses it for the plan, `plan set web
--rpo 30m` changes the objective (both hot). A CPU, memory, disk or network
change of a protected VM shows as drift: run `protect web` again to
rewrite its carnet. To change the retention: `protect web --retain 10`.

**Planned failover** (both sites up, no loss): `failover web --vm
default/web-01` (or `--all-vms --yes`). The source VM is stopped, its last
writes are backed up and applied, then the VM starts on the target.

**Unplanned failover** (source lost): first `check web --phase
failover-unplanned` to see the points, the skew and the disks still
catching up, then `failover web --all-vms --unplanned --yes`. A VM whose
disks are too far apart is refused unless `--accept-skew`. Read
`lost_after` in the result: the writes after it are lost. When the source
comes back, make sure its VMs stay stopped.

**Interrupted.** A failover cut after the activation (exit 3 `interrupted`)
is finished with `resume web`, with the target alone if needed. A console
restart, a host reboot or an update does not cancel anything: a protect
(initial restore included), a measure or an unprotect in progress is left
`interrupted` with the step it reached, and `resume web` continues it
without copying again what was restored (Activity says "interrupted by the
console shutdown"). A failover not yet activated is undone at that moment.
Only a cancel by a person (dock cancel, Ctrl-C) undoes a protect; run it
again afterwards. A command reported `not-started` never began: run it
again.

**Unprotect.** `unprotect web --yes`. VMs that were failed over keep
everything on the target (listed in `left`). After a completed failover,
backups are purged by hand: see "Purging the backups after a completed
failover" in the capabilities page.

## 13. Navigation by hosts (2.0)

A **host** is a whole Harvester cluster or a KVM hypervisor, never a
Harvester node. The side menu lists every host the person can see, in two
families, Harvester and KVM, under **Hosts**. There is no cap: a fleet of 60
clusters and 40 KVM hosts is listed in full, in a part of the menu that
scrolls on its own.

- **Family head.** Each family can be folded (the choice is kept by the
  browser) and says its state in one line, for example `58 ok, 2 in alert`.
  The hosts not read yet are counted in its tooltip.
- **Host row.** A dot gives the state (green reachable, red unreachable or
  in error, orange access refused, grey not read yet), then the name and the
  number of VMs when a screen has already read them. A host in alert stays
  visible even in a folded family. The list never reads a host itself.
- **Favourites.** The star on a host keeps it at the top of its family. Stars
  are kept per person by the console, so they follow you to another
  computer. Any role can star a host.
- **Open host.** The host on screen unfolds under its name (one at a time):
  Overview, Virtual machines, Containers, Imports and migrations, Parameters,
  Disaster Recovery for a Harvester cluster; Overview, Virtual machines,
  Parameters, Disaster Recovery for a KVM host.
- **Search** (shortcut `/`). It filters the hosts by name, label (`key`,
  `value` or `key=value`) and family, and offers the VMs the screens have
  already read ("Go to the VM" opens the VMs of its host, filtered on it).
  Enter opens the first result, Escape clears the search.
- **Add a host** (administrator) opens a window with two choices: a Harvester
  cluster (the same form as Settings > Clusters) or a KVM hypervisor (the
  declaration window of the KVM screens).
- Above the hosts: **Folders**, **Virtual machines** and **Containers** of the
  whole fleet; below: **Disaster Recovery** (with **Links**), **Migrations**,
  **Shared resources** (GPU, Bookings, Usage), **Automation** (Cluster API,
  Terraform, Bare-metal) and **Activity**. These fleet screens have no
  current host: `#containers` shows the workloads of every cluster,
  `#migrations` every VMware wave.
- **Activity of a host.** `#activity/host/harvester/<c>` and
  `#activity/host/kvm/<h>` open Activity filtered on that host (a KVM host's
  actions are labelled `kvm:<h>`); the button **Activity of this host** of a
  host page leads there. Changing the cluster filter rewrites the address.
- **Management cluster.** Cluster API and Terraform (declarations included)
  show **Management cluster** in their header: the cluster their calls go
  to, the last Harvester host visited unless you pick another one in the
  menu next to it. Picking one there does not change the host you have
  open. Bare-metal has no such header: a join chooses its cluster itself.

Every entry is a real link: middle click opens it in a new tab, and the
address can be copied. A link opened before signing in is opened again once
signed in.

### Host page (Harvester)

A Harvester host opens on a page with a band on top: the family, the name,
the state, the version and the VM count, the host's sub-menu (Overview,
Virtual machines, Containers, Imports and migrations, Parameters, Disaster
Recovery) and **Activity of this host**. Every view already built keeps its
place; the band only frames it.

- **Overview** (`#harvester/<c>`): metrics, usage gauges, the nodes table,
  and the **Events** block below them (the Cluster and Events sub-tabs are
  gone). `#harvester/<c>/overview/events` scrolls to the events. The
  **Upgrade** button opens Parameters > Upgrade.
- **Parameters** (`#harvester/<c>/params/<tab>`), in tabs:
  - **Nodes**: the block view of the nodes, with maintenance and host
    settings, and **Join a node** (administrator), which opens Automation >
    Bare-metal with this cluster already chosen: the install window opens in
    join mode towards `https://<VIP>:443`. The VIP is read from the cluster
    itself (Harvester's `harvester-system/vip` ConfigMap) when the button is
    clicked. Through a Rancher kubeconfig, or when the VIP cannot be read,
    no address is guessed: the banner says so and you type it. The address
    stays editable in the window, and you must tick its confirmation before
    Install, since the install erases the machine's selected disks.
  - **Storage**, **Network**, **Add-ons**, **Monitoring & Logging**,
    **Advanced**: the same sections and sub-tabs as before.
  - **Security**: Secrets, SSH Keys, and **Cluster accounts** (moved here
    from Settings).
  - **Upgrade**: the content of the Upgrade window, in the page.
  - **Power**: Startup and Shutdown (with the VM order).
  - **Connection**: who declared the cluster (`config.yaml` or the console),
    its declared nodes, **Test kubeconfig** and **Test SSH** (administrator),
    and a shortcut to Settings > Clusters.
- **Virtual machines** (`#harvester/<c>/vms`): the namespace view and its
  buttons (Backups, Templates, Cloud configs, Namespaces, Exports, Create).
- **Imports and migrations** (`#harvester/<c>/imports/vmimport`,
  `.../imports/forklift`): VM Import and VMware migrations.

Clicking another Harvester host in the menu keeps the screen: from
Parameters > Storage > Images of one cluster you land on the same tab of the
other (with the switching veil). A viewer sees the same pages without the
write buttons.

### Host page (KVM)

A KVM host opens on the same kind of page (`#kvm/<host>`): Overview (the
host block), Virtual machines (this host only), Parameters (System, Pools,
Networks, Labels, Connection) and Disaster Recovery (not available yet for
KVM domains). Connection holds **Test** (operator) and **Remove** (admin,
confirmed in the page). Labels can be edited by an admin, after the checks
the window shows. See Capabilities, section 10.

### Hosts index

**Hosts** in the side menu (`#hosts`) opens a table of every host of both
families: family, name, state, version, labels, VMs, folder and favourite.
Click a column title to sort (again to reverse); filter by family, state (or
every host in alert), label, version or folder. `#hosts/harvester` and
`#hosts/kvm` open with the family filter set, and changing that filter
rewrites the address. A row opens its host page; its star works as in the
menu. On `#hosts/kvm`, **Declare a host** and, per KVM row, **Test** and
**Remove** (confirmed in the row) give back what KVM > Hosts offered. A link
to a host you cannot see (removed, or outside your rights) opens this index
with a notice. The index only reads the host list, never a host.
