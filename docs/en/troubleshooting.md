# Troubleshooting

## During shutdown

### "Pre-flight failed: API unreachable"

The `KUBECONFIG` declared in `config.yaml` is wrong, or the API server is already down.

- Check `kubectl --kubeconfig=<path> get nodes` manually.
- Verify TLS certs are valid (`openssl s_client -connect <api>:6443`).
- If the API is genuinely down, the cluster is already in a bad state — investigate before shutting down further.

### VMs don't stop within `--vm-timeout`

A guest is hung on its own ACPI handler. Options:

1. Extend the timeout: `--vm-timeout 600`.
2. Force-stop in the web UI (kills the `VirtualMachineInstance` directly — guest state may be lost).
3. Skip the wait: `--skip-vm-stop` then deal with the leftover VMIs by hand.

### Longhorn volumes don't detach

Usually because a Pod still references a PVC bound to the volume.

```bash
kubectl --kubeconfig=<path> -n longhorn-system get volumes.longhorn.io \
  -o custom-columns=NAME:.metadata.name,STATE:.status.state,ATTACHED:.spec.nodeID
```

For each `attached` volume, find what's keeping it attached:

```bash
kubectl get pod -A -o json | jq -r \
  '.items[] | select(.spec.volumes[]?.persistentVolumeClaim) |
   "\(.metadata.namespace)/\(.metadata.name)"'
```

Delete the offending pod (or its parent workload) before continuing.

### "SSH ... connection refused"

The node OS is already down or unreachable. The script logs a warning but continues — manual verification recommended.

## During startup

### API doesn't come up

Check the first CP node:

```bash
ssh rancher@<cp-ip> sudo systemctl status rke2-server
sudo journalctl -u rke2-server -n 100 --no-pager
```

Common causes:

- **etcd quorum lost** — happens when multiple CPs were shut down too close together at the previous shutdown.
  → Restore from the snapshot taken at step 1 of the previous shutdown:
  ```bash
  sudo systemctl stop rke2-server
  sudo rke2 server --cluster-reset \
    --cluster-reset-restore-path=/var/lib/rancher/rke2/server/db/snapshots/<snapshot>.db
  ```
- **Time drift** — etcd is very sensitive. Verify chronyd/timesyncd is running.
- **Disk full** on `/var/lib/rancher` — Longhorn replicas can fill it.

### Some nodes stay NotReady

```bash
kubectl describe node <node-name>
ssh rancher@<node-ip> sudo journalctl -u rke2-agent -n 100 --no-pager
```

Common causes:

- CNI plugin not ready (Calico/Multus). Check `kubectl -n kube-system get pods | grep -E 'calico|multus'`.
- Container runtime (containerd) didn't start. Restart `rke2-agent`.

### VMs stay in `Halted` after startup

The script auto-restarts only VMs that had `runStrategy: Halted` set **by it** during the previous shutdown. If you have VMs that should also start, set them to `Always` manually or use the web UI's "Restart all VMs" button on the Namespaces tab.

## Web UI issues

### Login loop

Check `/etc/harvester-ops/htpasswd` exists and the format is bcrypt (`htpasswd -B -c`).

### "Cluster X not reachable" in the dashboard

Means the kubeconfig for that cluster is invalid or the API is down. The web UI does not exit — other clusters remain usable.

### SSE stream stops mid-operation

The browser tab was inactive too long, or the proxy in front of Flask has a low timeout. The operation continues server-side; refresh the page to re-attach the stream.

## Recovering from a half-done shutdown

If the script was killed (Ctrl-C, lost SSH, ...) and some nodes are off while others are still up:

1. Run `harvester-status --cluster <name>` from the operator workstation.
2. If the API still responds: re-run `harvester-shutdown` — it is idempotent and will pick up from where it stopped.
3. If the API is unreachable but some nodes are up: SSH to a still-running CP, manually shut down remaining workers/CPs in the right order.

## Getting more diagnostics

```bash
# Full bash trace
bash -x /usr/local/bin/harvester-shutdown.sh --cluster prod --dry-run -y 2>&1 | tee debug.log

# Cluster snapshot (read-only)
harvester-status --cluster prod --output json > status.json
```

Attach the script log (`/var/log/harvester-ops/*.log`) plus the JSON status when filing a support case.
