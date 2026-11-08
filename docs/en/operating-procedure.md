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
Every command requires `--cluster <name>`. Since 2.0.0 the web UI lists every host (Harvester clusters and KVM hypervisors) in its side menu; see section 13.

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

In the web UI, **Virtual machines** of a Harvester host (`#harvester/<c>/vms`, the former Namespaces tab) lists each namespace with its VMs and lets you stop/start them as a group.

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
  browser) and says its state, for example `58 ok, 2 in alert`. A host not
  read yet is counted too (`0 ok, 0 in alert, 1 unknown`): the numbers
  always add up to the family count. The tooltip of the summary explains the
  three states with the colours of the dots. A folded family shows no host
  line at all, not even a host in alert or the open host: the summary, in
  the alert colour, is enough.
- **Host row.** A dot gives the state (green reachable, red unreachable or
  in error, orange access refused, grey not read yet), then the name and the
  number of VMs when a screen has already read them. The list never reads a
  host itself.
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
  Bare-metal is in four tabs (2.1.0): **Machines** (default), **Profiles**,
  **Images** and **History**, at `#automation/pxe/<tab>`; the old
  `#automation/pxe` opens Machines. Cluster API (2.2.0): **Clusters**
  (default), **Services**, **History** and **Installation**, at
  `#automation/capi/<tab>`; `#automation/capi` opens Clusters. Terraform
  (2.2.0): **Declarations** (default), **Resources**, **History** and
  **Installation**, at `#automation/terraform/<tab>`; `#automation/terraform`
  opens Declarations. Each History is Activity filtered on the actions of its
  screen, with a link to each log. Clusters warns when the Cluster API stack
  is not installed on the management cluster, Declarations when the
  Harvester provider or OpenTofu is missing; both link to Installation.

Every entry is a real link: middle click opens it in a new tab, and the
address can be copied. A link opened before signing in is opened again once
signed in.

The menu of 1.x (cluster selector, per-cluster groups) is gone. Old links
still open their screen: `#storage` opens Parameters > Storage of the last
Harvester host visited, `#allvms` opens `#vms`, `#kvmhosts` the KVM hosts index,
`#forkliftglobal` Migrations; the CHANGELOG of 2.0.0 lists every old place
and its new address. Opening the console without an address shows
`#vms`; the first time after the upgrade, it shows the last screen you had
open instead.

### Host page (Harvester)

A Harvester host opens on a page with a band on top, on one line: the
family, the name, the state, the version and the VM count, the host's
sub-menu (Overview, Virtual machines, Containers, Imports and migrations,
Parameters, Disaster Recovery) and the **Activity of this host** icon
button. When the window is narrow, the sub-menu scrolls sideways instead of
wrapping, and a fade shows the side with more entries. Every view already
built keeps its place; the band only frames it.

- **Overview** (`#harvester/<c>`): metrics, usage gauges, the nodes table,
  and the **Events** block below them (the Cluster and Events sub-tabs are
  gone). `#harvester/<c>/overview/events` scrolls to the events. The
  **Upgrade** button opens Parameters > Upgrade.
- **Parameters** (`#harvester/<c>/params/<tab>`), in tabs:
  - **Nodes**: the block view of the nodes, with maintenance and host
    settings, and **Join a node** (administrator), which opens Automation >
    Bare-metal > Machines (`#automation/pxe/machines`) with this cluster
    already chosen: the install window opens in join mode towards
    `https://<VIP>:443`, and the tab says before you act when the store has
    no ISO of the cluster's version. The VIP is read from the cluster
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
  - **Power**: Startup and Shutdown (with the VM order). The two sit in a
    selector next to the panel title, so the page has one tab bar only.
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
**Remove** (confirmed in the row) give back what KVM > Hosts offered, and
the tabs **Capacity**, **Pools** and **Networks** show those of every KVM
host on one screen (`#hosts/kvm/capacity`, `/pools`, `/networks`), each
host name leading to its page. A link
to a host you cannot see (removed, or outside your rights) opens this index
with a notice. The index only reads the host list, never a host.

### Find a 1.x screen

Old links and bookmarks keep working: the console translates the 1.x token
to its new address and rewrites the address bar. A token that belonged to
one cluster opens that screen on the last Harvester host you visited (the
first visible one otherwise); a KVM token opens the KVM family. The most
used ones:

| You looked for | Now |
|---|---|
| Cluster selector | **Hosts** in the side menu, or the hosts index `#hosts` |
| Cluster > Overview, Events | the host's **Overview** (Events below the metrics) |
| Cluster > Storage, Network, Add-ons, Security, Monitoring & Logging, Advanced | the host's **Parameters**, same tab names |
| Cluster > Startup, Shutdown | **Parameters > Power** |
| Settings > Cluster accounts | **Parameters > Security > Cluster accounts** |
| Current cluster's VMs | the host's **Virtual machines** |
| All clusters VMs, KVM > Virtual machines | **Virtual machines** (`#vms`), Hosts panel for one host or family |
| `#containers` | the containers of the whole fleet; one cluster's: the host's **Containers** |
| KVM > Hosts, Storage, Networks | the KVM family (`#hosts/kvm`) and its Capacity, Pools, Networks tabs, or the host's **Parameters** |
| VM Import / Export | the host's **Imports and migrations**; all clusters: **Migrations** |

The CHANGELOG of 2.0.0 has the complete table, every old token included.

### File VMs, containers and hosts in folders

A folder is shared by everyone who uses the console. An object (VM,
container workload, host) is in one folder only, and nothing is inherited:
filing a host does not file its VMs (the window offers **Also file its N
VMs**, unchecked).

1. Open **Virtual machines** (`#vms`), **Containers** or the hosts index.
   The Folders panel on the left lists the tree, **All VMs** and **Unfiled**,
   with the counts of what you can see.
2. Select rows and drag them onto a folder, or use **Move to folder** on a
   row or a selection, then pick the folder (search by name).
3. Read the checks the window shows, object by object, before its button:
   **written on the object** (annotation on a Harvester VM or workload,
   libvirt metadata on a KVM domain, `folder` field of a host declaration),
   **console only** (you may not write the object, or the cluster is
   declared in `config.yaml`: the filing is kept by the console, with a
   badge), or **refused** with the reason and what to do.
4. Confirm. The action appears in the dock, one line per object, and stays
   in Activity.

Create, rename and delete folders from the panel. A folder that is not
empty cannot be deleted: the window offers **Move the content of...** to
another folder, then deletes it, in one action. A name already taken by a
sibling is refused before anything starts.

Since 2.0.2, the entry of an object that no longer exists (a KVM host that
was removed, a deleted VM, a removed cluster) does not block the deletion.
**Delete** checks each entry and purges it when the object is proven gone:
one step and one journal line each, in the same action. An entry that
cannot be checked (host unreachable, object you cannot see, read error)
still blocks, and the action says why. Nothing is purged then.

### Undo and resume a folder action

- **Undo**: a finished assign, rename, move, empty-into or undo offers
  **Undo** in its dock card, then in Activity (also after a console
  restart). Undo puts back the exact folder each object had just before the
  action. Any operator can undo another person's action; the undo names
  both people.
- **Resume**: an action that ended in error (a host unreachable in the
  middle, a webhook refusal) or was stopped by the console shutdown offers
  **Resume**. It writes only the objects the action did not reach; an
  object done is never written again, and one filed elsewhere since is
  left alone. On the command line: `harvester-folders.py resume --action
  <id>` and `undo --action <id>`.
- The objects you cannot see (a cluster Rancher hides from you, a namespace
  you cannot read) are never named in the steps: they are counted.

### Roles (2.0.0)

| Gesture | viewer | operator | admin |
|---|---|---|---|
| Read every screen, star a host | yes | yes | yes |
| Create, rename, delete an empty folder, empty a folder, file and move objects | no (greyed, with the reason) | yes | yes |
| Undo or resume a folder action, anyone's | no | yes | yes |
| Test a KVM host | no | yes | yes |
| Disaster Recovery: measure, protect, pause, resume, unprotect | no | yes | yes |
| Disaster Recovery: failover, unprotect deleting the backups, resuming either | no | no | yes |
| Add a host, remove a KVM host, edit KVM labels, Join a node, Test kubeconfig and SSH | no | no | yes |

A button you may not use stays visible, greyed, with the role it needs in
its tooltip; the server applies the same rule.

## 14. Applying a deployment plan (2.4.0)

A plan puts the same Terraform content (networks, storage classes, IP pools,
images, keys, VMs) on a cluster, after a bare-metal install or on a cluster
already declared. Full reference: [bare-metal.md](bare-metal.md), section
"Deployment plans".

1. **Admin, once:** in Automation > Bare-metal > Plans, write the
   declaration templates (Terraform forms, `{{variables}}` where a value
   changes per site), then the plan: readiness timeout, templates in order,
   "depends on" where one needs another. Templates hold no secret; a VM that
   needs one points at an existing Kubernetes Secret by name.
2. **Check before acting:** open the host page of the cluster, **Apply a
   plan**, choose the plan, fill the variables, read **Checks**. Launch stays
   disabled while a finding blocks (missing variable, declaration name taken
   by something else, cluster unreachable, admin-only object for an
   operator, refused Terraform construct).
3. **Dry run** first on a production cluster: it plans every template and
   removes its trial declarations; nothing is written to the cluster.
4. **Launch.** Follow the `deploy:<cluster>` action in the dock or in the
   History tab: one step per phase and per template, each with a link to
   its Terraform plan and apply.
5. **When a template fails**, the independent templates go on and the run
   ends `partial`. Fix the cause (often the declaration itself, in
   Terraform > Declarations, or the cluster), then **Resume** from the
   History tab: finished templates are checked again and skipped.
6. **Stop** lets a running Terraform apply finish and skips the rest;
   **Resume** goes on from there.
7. A plan never destroys: a plan that would delete or replace a resource
   stops on that template (`plan-destroys`). Remove resources from the
   declaration in Terraform > Declarations, on purpose.

With an install (admin): set the **Plan** field in the install window of a
machine or the batch window of a profile. The install becomes the first
phase; its secrets are asked at launch and again on resume, never stored.

## 15. Child clusters from a deployment plan (2.5.0)

A plan can also declare the Cluster API stack and create child RKE2
clusters, each with its own restricted CAPHV identity, on Harvester 1.9 or
later. Full reference: [bare-metal.md](bare-metal.md), section "Deployment
plans: Cluster API and child clusters"; the identity: Capabilities,
section 4.

1. **Admin, once:** fill the creation window of Cluster API > Clusters on a
   cluster that has the images, networks, keys and pools, then **Save as
   template**; or write the template in Plans > Cluster templates, with
   **Variable...** for a reference that changes per site. Give each child
   cluster an image storage class that its nodes can place (one replica on
   a one-node cluster).
2. In the plan, switch on **Cluster API** and **Clusters**, add the
   clusters (or **Several** with a name pattern holding `{{n}}`), their
   order and "depends on".
3. **Check before acting:** **Apply a plan** on the host page. The checks
   count every cluster of the plan together: CPU, memory, Longhorn space,
   pool addresses, your rights on Harvester, versions. Launch stays
   disabled while one blocks.
4. **Launch** and follow `deploy:<cluster>`: unfold the row in Activity to
   see `capi-install` and one `capi-cluster-create` per cluster.
5. **On a failure** the next independent clusters go on. A cluster left in
   failure is never deleted by the plan: delete it from Cluster API >
   Clusters, then **Resume**. A stop lets the creation in progress finish.
6. **Delete a child cluster** from Cluster API > Clusters. If the deletion
   ends with "object(s) kept", fix the right that is missing and delete
   again: it resumes where it stopped.

## 16. Deliver content to child clusters without Internet (2.6.0)

Give child clusters their images and charts from a registry instead of the
Internet. Full reference: [artefacts.md](artefacts.md); the plan phase:
[bare-metal.md](bare-metal.md), section "Deployment plans: Registry phase".

1. **On a connected machine:** write the description (charts, images,
   manifests) and run `harvester-artefacts pack DESCRIPTION --sign-key
   KEY`. Credentials of the sources go in a private file (`--creds`), never
   in an argument. Carry the `.tar.gz` and its `.sig` to the console.
2. **Admin, once:** in Settings > Artefacts, add the public key of your
   organisation to the trusted keys; switch strict mode on if only signed
   packages may enter.
3. **Admin: import.** Cluster API > Artefacts > **Import**: send the archive
   and the signature, read the trust state, **Accept**. An unsigned
   package, or one of an unknown key, needs the acknowledgement checkbox
   (refused in strict mode).
4. **A registry.** Either (admin) **Bootstrap registry** in Cluster API >
   Registries: a free address outside the IP pools, a volume, a storage
   class, the origin registries it mirrors; read **Checks**, then
   **Launch**. Or (admin) **Declare a client registry**, then (operator)
   **Attach** it to the Harvester cluster with its write and read accounts,
   and **Check**.
5. **Operator: publish.** On the package, **Publish** to the registry.
   Read the checks first: a tag that exists with another content blocks
   everything (`publish-tag-differs`). Publishing again resumes and skips
   what is there.
6. **Operator: create the child cluster** with the registries, in the
   **Registries** menu of the creation window (or in a cluster template).
   They are set at creation only. Leave **No fallback to Internet** off
   unless the registry carries the RKE2 system images.
7. **Operator: services.** Cluster API > Services, **From a registry**:
   the chart published in the registry, pulled by CAAPH with the read
   account and the CA.

A plan does steps 4 and 5 before its clusters (Registry phase); posing a
bootstrap registry from a plan needs the admin role. Roles: reading is open
to viewers; importing, keys, strict mode, declaring and posing registries
are admin; attaching, checking, publishing and creating clusters with
registries are operator.

## 17. Apply artefacts to child clusters, issue tokens, undo a deployment (2.7.0)

Full reference: [artefacts.md](artefacts.md), "Artefacts on child
clusters"; the plan phase and the undo: [bare-metal.md](bare-metal.md).

1. **Operator with the right to read `<c>-kubeconfig`: apply.** Cluster API
   > Artefacts, open the package, **Apply** on a chart, manifest or policy
   item: choose the child cluster (and the registry for a chart), read
   **Checks**, then **Apply**. Follow the action in the dock. A refusal
   `child-admin-required` means you cannot read the admin kubeconfig of the
   child cluster.
2. **Check what is applied.** Cluster API > Services: column **Kind**,
   state of the binding ("applied", "not applied yet"). **Remove** takes an
   item away; objects changed by hand are kept and named.
3. **Base of policies and accounts.** In a plan (Artefacts card) or with
   `harvester-capi manifest-deploy` and a `policies` spec. The base is
   unticked by default: tick it only for team namespaces the console
   creates.
4. **Token for a pipeline.** Cluster API > Clusters, card of the child
   cluster, **Issue a token** on an automation account: duration 600 to
   86 400 s; copy it at once, it is never shown again. It works about one
   minute past its expiry.
5. **Admin: undo a deployment.** Bare-metal > History, **Undo** on the run:
   read **Content** (registry and unproven or changed declarations
   unticked), read the **Destruction plan** (an item marked as recreated
   since stays; a declaration kept by a cluster that stays is said), type the cluster name in **Confirmation** (and the
   registry name to delete its data), **Undo**. A partial result says what
   stayed and why: fix it and run **Undo** again on the same run; what is
   gone is checked and skipped. To recreate, apply the plan again.
6. **Admin: uninstall the Cluster API stack.** Remove every child cluster
   first. Cluster API > Installation, **Uninstall**: read what goes and what
   stays, type the cluster name. **Install** lays it again from the bundle.
   Do not start a deployment while the uninstall runs: the deployment lock
   is checked at the start but not held during the removal.
7. **Test benches: own addresses.** Each test bench has its own registry
   address and its own IP pool, reserved in the IP management and the DNS
   before use. Never borrow the address of another bench: with both benches
   up, two kube-vip LoadBalancers would answer ARP for the same address.

## 18. Child cluster access, GitOps, renewal and offline bootstrap (2.8.0)

Full references: GitOps in [gitops.md](gitops.md); package kinds and
registries in [artefacts.md](artefacts.md); the plan phase and the undo in
[bare-metal.md](bare-metal.md). Each child cluster of Cluster API >
Clusters gets two more buttons: **Access** and **Renew**.

### Import into the external Rancher of your session

The child cluster is imported into the Rancher that manages your session,
by the generic import of Rancher. The Rancher embedded in Harvester is
never aimed at by this path.

1. **Sign in through Rancher.** A local account of the console is refused
   (`rancher-session-needed`, 409), as for Projects and Members.
2. **Access window, Rancher tab.** It shows the import state, the Rancher
   of your session and the run of a plan that imported it. **Import into Rancher**
   (operator **and** the right to administer the child cluster) is greyed
   with the reason when it cannot run. At creation, the **Access** menu
   offers the same as "External Rancher of the session"; a cluster
   template takes `rancher_external: true`.
3. What the console does, as a tracked action
   `capi-rancher-import:<ns>/<c>`:
   - creates an empty `provisioning.cattle.io` cluster in `fleet-default`
     of Rancher with the token of your session, named `<harvester>-<c>`
     (`<harvester>-<ns>-<c>` when the namespace differs from the name);
   - reads the registration manifest in that Rancher (10 MiB at most),
     applies it in the child cluster, then erases it: it carries a token
     and is never stored, never in a step nor a log;
   - probes the image of the Rancher agent in the registries of the child
     cluster first. Missing while the fallback to the Internet is cut:
     `rancher-agent-image-missing`, blocking. Mirror `registry.rancher.com`
     in a registry of the cluster;
   - waits until the cluster is active (900 s by default, 60 to 3 600),
     then marks the `Cluster` (`harvester-ops.io/rancher-external`).
4. **Exclusive of the embedded Turtles import** (`rancher_import`): both
   together are refused (`rancher-import-conflict`). Two agents would
   fight over `cattle-system`.
5. **Members tab.** The members of the imported child cluster in Rancher,
   cluster scope only, as in the Members window of a Harvester cluster
   (`child-not-imported` when it is not imported in your Rancher).
6. **Remove the import** (`capi-rancher-unimport:`): the cluster is deleted
   in Rancher and waited for (600 s). Then the leftovers Rancher keeps in
   the child cluster (cluster roles and bindings, CRDs of
   `management.cattle.io` and others, the namespace `local`) are removed,
   each judged by the server first. Only an object that carries a mark of
   Rancher goes (a `cattle.io` label, annotation or owner). Any other is
   kept and named (`kept-not-rancher`). Since 2.8.2 the agent Bundle
   `fleet-agent-<name in Rancher>` that Rancher 2.14 leaves in
   `fleet-default` is deleted too, here and when an imported child cluster
   is deleted or undone. Only when its name is exact, each of its targets
   names this cluster alone, it carries a mark of Rancher or Fleet and no
   label of the console, and once the Fleet cluster of that name is gone
   (waited for up to 60 s: Rancher removes it after its own cluster; while
   it is there Fleet would recreate the Bundle, or it is the agent of a
   live Fleet cluster). Otherwise it is kept and named in the steps
   (`kept-not-rancher`; `kept-fleet-cluster`; `kept-not-matched` when the
   name is over 63 characters, since Fleet shortens it). A refusal of Rancher on the
   Bundle is said and does not undo the removal. Verified by tests only
   so far.
7. **Delete an imported child cluster.** The cluster is removed from
   Rancher first, without reading the child cluster (a dead child stays
   deletable). If Rancher is unreachable or you have no session, the
   deletion is refused. When that Rancher is gone for good, an
   administrator drops the import mark with `harvester-capi delete
   --forget-rancher` (API: `?forget_rancher=1`, `rancher-forget-admin`
   otherwise). The cluster may then stay listed in that Rancher.

### OIDC of the child API

Without Rancher, people sign in to the API of the child cluster through
the identity provider of your organisation. The `oidc` option is set at
creation (Access menu, OIDC block) or by a renewal:

| Key | Rule |
|---|---|
| `issuer_url` | `https://` only, no credentials, no query |
| `audience` | the client id at the provider, 1 to 256 characters |
| `username_claim`, `groups_claim` | `email` and `groups` by default |
| `username_prefix`, `groups_prefix` | `oidc:` by default; never empty, never starting with `system:` |
| `ca_pem` | optional CA of the provider |
| `admin_group` | optional group bound to `cluster-admin` |

- The console writes an `AuthenticationConfiguration` into the control
  plane template only: the file
  `/etc/rancher/rke2/hops-auth/authentication-config.yaml` (root, 0600)
  and the argument `authentication-config` of the API server. Version
  `apiserver.config.k8s.io/v1` on Kubernetes 1.34 and later, `v1beta1`
  from 1.30 to 1.33, refused before (`oidc-version-too-old`). The
  `--oidc-*` flags are never used.
- No secret: the API needs only the audience.
- **`admin_group`** is bound to `cluster-admin` by the
  `ClusterRoleBinding` `hops-oidc-admins`, subject `<groups_prefix><admin_group>`,
  in the base of policies of the cluster (`hops-policies`).
- **Download the OIDC kubeconfig** (Access window, OIDC tab; operator and
  the right to administer the child cluster): server and CA of the child
  cluster, user `oidc` through the kubelogin exec plugin (`kubectl
  oidc-login get-token`). It holds no credential.
- Rancher import and OIDC together are allowed, with the warning
  `oidc-with-rancher`.

### Renew the machines of a child cluster

The registries, the OIDC settings and the registry CA live in the RKE2
templates and are read at the first boot of a node. **Renew** replaces the
machines that need it:

```
harvester-capi renew --name NS/C --what registries,oidc,ca [--spec F]
                     [--timeout 60-10800] [--dry-run]
harvester-capi renew --name NS/C --finish
harvester-capi renew --name NS/C --abandon
```

1. **Renew window, Parameters**: what to renew (registries: read the
   declarations again or replace the list; OIDC: the current settings are
   filled in, untick to remove OIDC; new registry CA), the time limit.
2. **Difference**: the dry run, read on the cluster without writing. Renew
   stays greyed until a fresh difference with changes, no blocking
   finding, and the confirmation ticked.
3. What happens:
   - only the templates that change are copied to `<name>-r<N>` with the
     new content (annotation `harvester-ops.io/template-revision`), and the
     ClusterClass is repointed;
   - only the machines of those templates are replaced, and only they are
     waited for. One more address of the IP pool is taken during the
     rollout (`renew-pool-short` blocks when there is none). With a
     single control plane, the API is cut for about a minute;
   - a mark `harvester-ops.io/renewal` on the `Cluster` names the
     revision, the replaced templates, the old machines and the Secrets to
     remove;
   - once every machine of these roles is new: old templates and Secrets
     removed, mark removed. Past the time limit nothing old is removed.
4. **An unfinished renewal** (time limit, console stopped) leaves its mark.
   A new renewal is refused (`renew-busy`). **Finish the renewal**
   (`--finish`, operator) waits again and cleans up. **Abandon**
   (`--abandon`, administrators) removes the mark only and lists what is
   kept.
5. **Administrator group.** A change of `admin_group` alone replaces no
   machine: it changes the annotation and delivers the binding again.
   After each OIDC renewal, the binding `hops-oidc-admins` follows the new
   settings (changed, added or removed).
6. **Lock.** The renewal holds the lock of the Harvester cluster, the same
   as plans. Meanwhile plans, undo, the Turtles uninstall and other
   renewals on the same Harvester cluster are refused (`renew-running`,
   `renew-busy`), up to the time limit: 1 h by default, 3 h at most. A
   renewal is refused while a plan runs (`renew-plan-running`).

`--what ca` brings the next CA of a registry to the cluster during a CA
change (see [artefacts.md](artefacts.md), "Registries finished (2.8.0)").
It also refreshes the CA of `<c>-rke2-fetch` of a cluster without
Internet.

### Child cluster without Internet

A child cluster can be bootstrapped with RKE2 itself coming from a
registry: option `offline: true` (creation window, Registries menu,
**Without Internet**; or a cluster template).

1. **On a connected machine:** `harvester-artefacts pack --rke2
   v1.34.11+rke2r1 --cni calico`, import the package, publish it to a
   registry that serves images. Since 2.8.1 the package also carries the
   images of the cloud provider, the CSI driver and kube-vip that the
   console lays in every child cluster: rebuild a package made by 2.8.0.
2. **Create the cluster** with that registry. Required:
   - a registry that mirrors `docker.io` with **No fallback to Internet**
     (`offline-needs-no-fallback`);
   - the `rke2` item of the Kubernetes version of the template published
     in a registry of the cluster (`offline-rke2-missing`,
     `offline-rke2-version-mismatch`). The `rke2rN` release is not
     compared.
3. What the nodes do, in both templates:
   - a file `/etc/rancher/rke2/hops-fetch.netrc` (0600) with the read
     account, from the Secret `<c>-rke2-fetch`, never in a command;
   - `curl` with this netrc fetches the three files by digest into
     `/opt/rke2-artifacts`. Each sha256 is checked, then
     `sha256sum-amd64.txt`. On a mismatch everything is removed and the
     boot fails;
   - `/opt/install.sh` is placed only after these checks, and CAPRKE2
     runs it (`agentConfig.airGapped: true`).
4. `offline_artifacts: image`: the VM image already carries these files.
   Nothing is fetched.
5. cloud-init without network (2.8.1, both modes). CAPHV asks cloud-init
   for a package update and for `qemu-guest-agent` and `iptables` on every
   machine. Without Internet, zypper waited about 5 minutes per repository
   before RKE2. The console now sets `agentConfig.additionalUserData` in
   both templates:
   - `package_update`, `package_upgrade` and
     `package_reboot_if_required` are false;
   - at the first boot, a `bootcmd` moves `/etc/zypp/repos.d` and
     `/etc/zypp/services.d` aside (once per instance). The package list of
     CAPHV cannot be removed, and installing it would refresh the
     repositories. Without repositories, it fails at once;
   - the first command without Internet puts them back, before the fetch.

   The image must therefore already carry `iptables` and
   `qemu-guest-agent`: they are not installed. Without `iptables`, pods
   that publish a host port (ingress-nginx) do not start. Without
   `qemu-guest-agent`, the machines report no address. Every offline
   creation and plan check shows the warning `offline-node-packages`
   (not blocking); `image-sles-repos` is not shown in this mode.

   `cloud-init status` reports an error on these nodes, by design: the
   package module fails at once without repositories, and the boot goes
   on. `repos_back` writes one line per directory in
   `/var/log/cloud-init-output.log` (`harvester-ops: zypper repositories
   restored in ...`).
6. `offline_packages: keep` (creation window, Registries menu,
   **Repositories of the image**; or a cluster template; default `skip`):
   for an image whose repositories point at a mirror of the local network
   (RMT, MLM). The repositories stay in place and cloud-init installs
   `iptables` and `qemu-guest-agent` from the mirror. `package_update` and
   `package_upgrade` stay false in both modes. With `keep`, a repository
   that the machines cannot reach brings back the wait of several minutes
   per repository.

Limits: the fetch uses Basic authentication. A registry that only accepts
token authentication on its blobs (Harbor, ghcr style) is not checked.
A disk under 20 GiB is a warning (`offline-disk-small`): at 10 GiB the
bench reached 78 % with the agent and Flux, and the image garbage
collection failed.

### Roles (2.8.0)

| Gesture | Role |
|---|---|
| Read imports, OIDC settings, GitOps entries | viewer |
| Import, remove the import, pose or remove a Flux entry, renew, finish a renewal, download the OIDC kubeconfig | operator **and** the right to administer the child cluster (and a Rancher session for Rancher) |
| Fleet entries, `privileges: cluster`, abandon a renewal, `delete --forget-rancher`, `renew-cert`, `harvester-nodes` | admin |

### Seen for real (2.8.0)

On harvlab2 with a bench Dex (OIDC) and the external Rancher 2.14.1:

- OIDC: a member of the administrator group is `cluster-admin`
  (`kubectl auth whoami`, `auth can-i '*' '*'`); another person has only
  what the team role gives (`view` in `equipe-a`); a token of another
  audience is refused (401). The OIDC kubeconfig holds no credential.
- Import at creation: `Active` in 142 s, Members shows the importer
  (`cluster-owner`); Remove import: the cluster leaves Rancher, 36
  leftovers deleted in the child, 11 kept without a mark of Rancher
  (`kept-not-rancher`), the Rancher cluster list identical to before.
- Renew `--what oidc` (username prefix and administrator group changed):
  only the control plane machine replaced, workers kept; the binding
  `hops-oidc-admins` moved to the new group. Stopped with `--timeout 60`,
  then `--finish` cleaned up. `--abandon`, then the next renewal removed
  the templates and the nodes Secret left behind.
- The lock: `harvester-plans apply` refused during a renewal
  (`renew-running`), and a renewal refused during a plan
  (`renew-plan-running`).

Not verified for real: a client identity provider other than Dex
(Keycloak, Entra ID), Rancher 2.15, a Rancher in airgap with
`system-default-registry`, the child cluster without Internet end to end
(stopped in 2.8.0 by the package update CAPHV asks of cloud-init; 2.8.1
skips it, the bench run is still to do), a plan chained to a real Harvester install with a Git
secret, more than two child clusters, harv4 (two clusters in one run).
