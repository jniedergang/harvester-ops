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

## 3. Startup sequence (the 5 steps)

| # | Step | What it does |
|---|---|---|
| 1 | Power on first CP | Operator powers on the **last CP shut down** via iLO/iDRAC/IPMI. The script then waits for `/readyz` on the API |
| 2 | Power on remaining nodes | Other CPs, then workers, one at a time |
| 3 | Wait Nodes Ready | Poll until every node reports `Ready` |
| 4 | Restore cluster state | Re-enable Longhorn rebuild, `uncordon` all nodes |
| 5 | Restart VMs | Set every `runStrategy: Halted` VM back to `Always` |

## 4. Running the procedure

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

- Settings → **Clusters** tab → *Add cluster*, OR the **+** button next to the cluster selector in the sidebar.
- Provide: a unique name, a description, the SSH user (defaults to `rancher`), the list of nodes (hostname + IP + role: `control-plane` or `worker`), and upload the kubeconfig + the SSH private key as files.
- The new cluster is persisted in `/etc/harvester-ops/config.yaml` (atomic write, `.bak` of previous version kept).
- The kubeconfig is stored at `/etc/harvester-ops/kubeconfigs/<name>.yaml` (mode 0600); the SSH key at `/etc/harvester-ops/ssh/<name>_id` (mode 0600).
- From the Clusters tab you can also: replace the kubeconfig, replace the SSH key, run **Test kubeconfig** (kubectl version ping) or **Test SSH** (per-node SSH probe), edit metadata, or delete the cluster (which also removes its files).

## 11. Safety design

- **Two-factor consent**: destructive operations require both a non-interactive flag (`--yes`) AND a final confirmation prompt unless `--yes` is explicitly set.
- **Idempotency**: re-running the script after a partial failure picks up where it left off (e.g. VMs already stopped → skip step 2's wait).
- **No silent failure**: every step exits non-zero on error unless the operator explicitly chooses to skip.
- **Auditable**: every command is logged with timestamp and exit code.
