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

### The service does not start, or cannot read a kubeconfig

Before 1.44.1 the packaged service could not start: the application
crashed with `Read-only file system: '/var/lib/harvester-ops'`, and the
container, which runs as a non-root account, could not read the TLS key,
the htpasswd file or the kubeconfigs that `install.sh` made root-only.
Re-run `sudo ./install.sh` from the 1.44.1 package or later: it creates the
`harvester-ops` account, the persistent `/var/lib/harvester-ops/` volume,
and installs the unit that gives the account read access to
`/etc/harvester-ops/` at each start.

If a kubeconfig or an SSH key added later is reported unreadable, restart
the service (`sudo systemctl restart harvester-ops`): the unit makes every
file under `/etc/harvester-ops/` readable by the `harvester-ops` group, and
by no other account, before starting the container.

### Login loop

Check `/etc/harvester-ops/htpasswd` exists and the format is bcrypt (`htpasswd -B -c`).

### "Cluster X not reachable" in the dashboard

Means the kubeconfig for that cluster is invalid or the API is down. The web UI does not exit — other clusters remain usable.

### SSE stream stops mid-operation

The browser tab was inactive too long, or the proxy in front of Flask has a low timeout. The operation continues server-side; refresh the page to re-attach the stream.

### A maintenance ends in error but the node is in maintenance

Before 1.44.9, the actions dock could show "Harvester refused the
maintenance and withdrew the request" while the node did enter maintenance.
Harvester clears its `drain-requested` mark before the drain is over, and
the drain can take minutes (it waits on the disruption budget of the
Longhorn instance managers). Check the node itself:

```sh
kubectl get node <name> -o jsonpath='{.metadata.annotations}'
```

`harvesterhci.io/maintain-status: completed` means the maintenance is
done, whatever the action said. On 1.44.9 and later the console waits
before concluding, so this no longer happens.

## Moving a VM between clusters

### "The target cluster never fetched the disk"

The copy through the console needs the target's nodes to reach the console
host over HTTP, on port 8094 by default. Open that port on the host
(`firewall-cmd --add-port=8094/tcp`), or set `transfer: serve_address:
host:port` in `config.yaml` to an address and port they can reach. The
transfer stops after five minutes without a request and undoes what it
created.

### The backup never shows on the target

Both clusters must have the same backup target, in the `configured` state.
Harvester re-reads the backup target only when asked (with a zero
`refreshIntervalInSeconds`, never on its own), and its first pass only
brings back the images the VM was born from: the backup appears on a later
pass. The transfer asks every minute, and also asks Longhorn to re-read the
target at once. If the images are large, the wait follows their restore.

### "duplicate mac address present for vm"

Harvester refuses two VMs with the same MAC address on a cluster network,
even when one of them is stopped (for example the original VM, left
stopped, next to its copy coming back). The pre-check shows the addresses
and the VM holding them: untick "Keep the MAC addresses", or remove that
VM.

### The import shows every byte sent, then waits

"The target is still writing": CDI has received the disk and is flushing it
to the volume. On slow storage this can take minutes (measured on a nested
test cluster: five minutes for 4 GiB). The transfer goes on by itself.

### "could not undo, remove by hand"

A rollback retries its deletions for five minutes while a cluster API is
unreachable. What remains is listed, and carries the label
`harvester-ops.io/transfer=<id>`: `kubectl get vm,pvc,virtualmachineimages -A
-l harvester-ops.io/transfer=<id>`. Harvester refuses to delete a volume
while an image is exported from it: delete the images first.

### "Upload refused" when adding an archive to the store

The message says why:

- `checksum mismatch in disks/<volume>.raw.gz`: the file was damaged on its
  way (copy, USB key, transfer tool), even though its size is right.
  Download it again from the source store and compare
  `sha256sum` on both sides;
- `incomplete archive`: the export was interrupted, or the copy was cut
  short;
- `already in the store`: rename the file (the name must end in `.hvx`)
  or delete the older archive first;
- `not enough room in the store`: the size needed and the space free are
  given; free space in `/var/lib/harvester-ops/exports`.

Behind a reverse proxy, raise its request body limit (nginx:
`client_max_body_size 0;`) and its timeouts for the console's URL: an
archive is one request as large as the file.

### A VirtualMachineRestore stays on the target

Normal: Harvester ties it to the restored VM and refuses to delete it
while that VM exists. Empty `BackupVolume` objects also stay in Longhorn
after the transfer backups are deleted; that is how Longhorn works.

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
