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

### "The target cluster does not answer" through a Rancher proxy

A Rancher proxy can return 404 for `/readyz` even when authenticated
Kubernetes resource reads succeed. Earlier transfer checks used that
endpoint and could report the target as unreachable.

Since 1.87.3 the pre-check first reads a single custom resource definition
(`?limit=1`, at most 15 seconds), which goes through the proxy like any
resource. It then reads the full list it needs with the usual 60 seconds:
that list weighs about 37 MB on a Harvester 1.9 cluster. A target whose
list takes longer than that is reported as not answering.

- A target that does not answer, or whose certificate is refused, is still
  reported as "The target cluster does not answer".
- A target that answers but refuses the reader (403, or 401 for expired
  credentials) is reported as such: check the rights or the credentials of
  its kubeconfig. It must permit reading custom resource definitions.

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

### A VMware wave that stays "deleting"

Before 1.84.0, deleting a wave left its plan and migrations in deletion for
good (finalizer `orphan`): the Kubernetes garbage collector must update each
migration, and Forklift's webhook refuses any change to a migration from an
account that cannot create VMs in the target namespace. Delete the wave again
from 1.84.0: the console removes the finalizer itself and the VMs stay.

### A Windows VM fails at "converting guest"

The pod `<wave>-<vm>-…` of the namespace target shows `inspect_os: unhandled
exception thrown: Hivex.Error`: the guest conversion of the Forklift shipped
for Harvester 1.9 (virt-v2v 2.7.7) cannot read the registry of a recent
Windows (seen on Windows Server 2025). The source was stopped by the
switchover: roll the wave back (the source powers on), close and delete it,
then compose a new wave with **raw copy** and **compatibility mode** (the
default for Windows from 1.87.0).

### A VMware wave says "Disk copy waiting for storage"

The target storage class cannot place a disk of the wave: Longhorn leaves the
volume detached ("insufficient storage", "un-schedulable replicas") and the
copy cannot start. Free space on the cluster, add a disk, map the datastore to
another class, or raise Longhorn's `storage-over-provisioning-percentage`
(volumes are thin: a 40 GiB Windows disk often holds far less). The copy
starts by itself once the volume attaches. From 1.87.0 such a wave is refused
at composition when the figures show it cannot fit.

### A rollback says "VMware waits for an answer"

VMware stopped the source on a question at power-on (often a serial port
file that already exists). The message gives the question and its choices;
answer it in vCenter (the VM's "Answer question"), then run the rollback
again: that VM is not marked rolled back until it runs.

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

## kube-ovn networks (VPC tab)

### A VM attached to an overlay network gets no address

Either no subnet serves that network (the VPC tab says so above the
blocks, with a button that opens the subnet form on it), or the subnet has
DHCP turned off and the VM expects DHCP. A subnet serves a network when its
`provider` is `<network>.<namespace>.ovn`.

### A VM on a subnet of another VPC cannot reach the Internet

kube-ovn does outgoing NAT in its default VPC only. A VPC of its own is
isolated until it gets a route out (a peering with the default VPC and a
static route, or a VPC NAT gateway, which a later version of the console
will offer).

## Signing in through Rancher

### The sign-in page says "Rancher's answer could not be verified"

The code under the message says which check failed. `token-issuer`: the
`url` in `config.yaml` differs from the address Rancher answers as.
`token-audience`: `client_id` is not this console's OIDC client. On the
console host, the log line "Rancher sign-in refused" carries Rancher's own
message (a status and a sentence, never a token).

### A Rancher user sees no cluster, or empty views

The console only offers the clusters Rancher shows that person, found by
comparing the `kube-system` UID; a cluster that is off cannot be matched
(retried after a minute). Empty views mean Rancher gives no read right
there: check their cluster and project roles in Rancher. On harv1 the
`default` namespace was linked to a project of another Rancher
(`field.cattle.io/projectId`), so project roles did not cover its VMs.

### Direct sign-in says the user name or password is wrong (1.79.0)

The console says the same thing for a wrong password and an unknown account.
Check the account type: an LDAP or Active Directory user must pick that
provider, not "Local". A user of an OIDC provider (Keycloak, for example)
has no password Rancher can check: use single sign-on. On the console host,
the log line "Rancher direct sign-in refused" gives the code (never the
password).

### A Rancher shows as unavailable on the sign-in page (1.79.0)

The console did not get its providers within three seconds (retried after
thirty seconds). Use Settings > Sign-in through Rancher > Test: the error
says whether the address does not answer or the certificate is refused
(give the certificate authority in PEM).

### The Harvester RBAC chart is refused (1.79.0)

The chart declares the Rancher and Kubernetes versions it supports
(`catalog.cattle.io/rancher-version`, `catalog.cattle.io/kube-version`);
the console shows both requirements and the versions it read. Upgrade
Rancher, or wait for the chart version that matches it in `rancher-charts`.

### "starting or stopping a cluster needs a local account"

By design: use a local account for power sequencing.

## Cluster API clusters

### Machines stay "Provisioning" although their VMs run

CAPHV v0.10.1 reports a HarvesterMachine ready in `status.ready` only,
while the Cluster API core of Harvester v1.9 (v1.13) reads
`status.initialization.provisioned` because of a contract label on the
HarvesterMachine CRD. The install removes that label through a patch kept
in the Harvester `CAPIProvider` (an edit by hand is reverted by Turtles).
Check that it is gone:

```bash
kubectl get crd harvestermachines.infrastructure.cluster.x-k8s.io \
  -o jsonpath='{.metadata.labels}'     # no "cluster.x-k8s.io/v1beta2"
```

The Installation tab reports it (`compatibility/contract-label`) and the
install removes it: Turtles re-applies the provider without taking off a
label that is already there.

### "unable to compute the Harvester Endpoint: problem in getting the ingress-expose service"

Harvester v1.9 no longer has the `kube-system/ingress-expose` Service that
CAPHV v0.10.1 reads the VIP from. The install creates a stand-in labelled
`harvester-ops.io/shim=caphv-ingress-expose`; the Installation tab says
whether it is there. Run the install again to recreate it.

### "spec.template.metadata in body should have at least 1 properties"

The Harvester templates were created at `v1alpha1` (by the upstream
generator run by hand, for instance): the conversion stores an empty
`metadata` that the schema then refuses when the topology copies the
template. Clusters created by the console use `v1beta1`. Delete the
cluster and its templates, and create it again from the console.

### The pre-check says the namespace already holds a cluster

The generated objects (ClusterClass, identity secret, add-ons) carry fixed
names, so a second cluster in the same namespace would overwrite the
first one's. Leave the namespace empty in the advanced options (it then
takes the cluster name) or pick another one.

### The IP pool shows more free addresses than it has

Harvester's `available` counter on an IP pool drifts upwards after
clusters are deleted (24 free out of 16 seen on a test cluster). The
console counts from the table of allocated addresses instead.

### ingress-nginx stays in ContainerCreating: "iptables: executable file not found"

The node image has no `iptables`: cloud-init installs it at first boot, and
on an unregistered SLES image (no repository) the installation fails
(`cloud-init status --long` on the node shows it). Register the image, give
it a local repository, prepare an image that already has `iptables` and
`qemu-guest-agent`, or use the openSUSE Leap 15.6 cloud image.

### The API address answers with the Harvester certificate

While the first control plane node is not up, the load balancer address
of the new cluster has no backend, and the connection ends on the
Harvester API server. This is normal until the control plane counts 1.

### Deleting a cluster ends with "identity of ns/name: N object(s) kept" (2.5.0)

The `Cluster` is gone, but some objects of its restricted identity could
not be read or deleted, most often because the Harvester identity of the
person who deleted it lacks `get` or `delete` on roles, bindings, service
accounts or secrets in one of the namespaces listed. The namespace and the
identity account are kept on purpose: the account lists the side
namespaces. Give the missing right (or ask someone who has it), then delete
the cluster again from Cluster API > Clusters or with `harvester-capi delete
--name ns/name`: the deletion resumes on the identity and finishes.

### A cluster stays "Deleting" and the CAPHV log says "forbidden" (2.5.0)

CAPHV deletes the pool `<namespace>-<name>-ippool` unconditionally when a
cluster goes, and a refusal is not "not found": without that right it
retries forever and the cluster never disappears. The restricted identity
holds this right; it is missing only if someone edited the ClusterRole
`harvester-ops-caphv-<namespace>`. Compare it with
`kubectl auth can-i --list --as=system:serviceaccount:<ns>:caphv-identity`
and the table of Capabilities, section 4, and put the rule back. The
deletion goes on by itself at the next attempt of CAPHV, which spaces its
attempts out after repeated errors: on the bench it came 8 minutes after
the fix (up to about 17 minutes). Seen for real: with the right removed,
the `Cluster` stayed `Deleting` for 10 minutes with
`cannot delete resource "ippools"` in the CAPHV log.

### "the Harvester address is unknown" when creating a cluster (2.5.0)

The restricted identity needs the Harvester address. It is read from
`kube-system/rke2-traefik` (fresh 1.9 install), then from
`kube-system/ingress-expose` (cluster upgraded from 1.8). If neither has an
address, check those services on the cluster (`kubectl -n kube-system get
svc rke2-traefik ingress-expose`).

## Artefact packages and registries (2.6.0)

### A child cluster does not pull from its registry

Look on a node of the child cluster (control plane and worker):

- `/etc/rancher/rke2/registries.yaml`: the mirrors and the registry
  account written at creation (read account only).
- `/var/lib/rancher/rke2/agent/etc/containerd/certs.d/<origin>/hosts.toml`:
  one block per mirror, in order. A `server` line means the fallback to the
  origin registry is on (the default).
- `/var/lib/rancher/rke2/agent/containerd/containerd.log`: each pull names
  the endpoint it tried and why it failed (certificate, 401, not found).

Usual causes:

- The registries were chosen after the cluster was created. They are read
  when RKE2 starts and set at creation only: an existing cluster does not
  change.
- The registry does not serve images (usage charts only), or the origin is
  not among the mirrors chosen for this cluster.
- The nodes cannot reach the registry address (routing from the VM
  network, firewall). The console checks the registry from itself only.
- The image was never published, or under another name: images live at
  `<registry>/<origin host>/<path>:<tag>`; list them with the read account
  (`GET /v2/_catalog`).
- `containerd.log` says `x509: certificate signed by unknown authority`
  for a registry of the list: its CA file is missing on the node. The
  console writes one CA file per registry under
  `/etc/rancher/rke2/tls/harvester-ops/<registry>/ca.crt`. Check that the
  CA of each registry was declared.

### A child cluster VM stays in "Scheduling" (2.6.0)

The PVC of its root disk stays `Pending` and the Longhorn event says
`volume size should be larger than the backing image size`: the root disk
is smaller than the virtual size of the image. The creation check now
blocks it (`disk-smaller-than-image`) and gives the size to ask for. The
published openSUSE Leap 15.6 cloud image announces 10 GiB.

### Publish says "publish-tag-differs"

A tag of the package already exists in the registry with another content.
Nothing was written, for any image. Publish the content under a new tag (a
new package version, or `as` in the description). A bootstrap registry has
deletes disabled: its tags cannot be removed. In a client registry, remove
the tag with the tools of that registry, on purpose.

### The bootstrap registry is full

2.6.0 has no garbage collection and no resize gesture, and what is
published stays. The details of the registry show the volume use. Deploy a
second registry with a larger volume, or, once nothing uses this one,
delete it with its data (typed name) and deploy it again. In a plan,
`registry-package-too-big` blocks packages that pass 90 % of the volume.

### A bootstrap registry deployment failed and a new attempt says "registry-name-taken"

A deployment that does not become ready in time leaves its objects and its
volume on the cluster, and saves no declaration. The Registries tab shows
it as a **Leftover** row. Run **Delete** on that row (or
`harvester-registry delete NAME --cluster C`, with `--delete-data` to
remove the volume too), fix the cause shown in the `ready` step, then
deploy again. A deployment plan that posed it needs nothing of this: its
resume takes the leftover over and deploys again.

### The registry pod stays in "ErrImageNeverPull"

The registry image is imported on the nodes listed in the console
configuration at deployment. A node added to the cluster later does not
have it, and the pod was scheduled there. Import the image on that node by
hand, from the console host:

```bash
ssh rancher@<node> 'sudo /var/lib/rancher/rke2/bin/ctr --address /run/k3s/containerd/containerd.sock \
  --namespace k8s.io images import -' < /var/lib/harvester-ops/registry-bundle/registry-images.tar
```

Add the node to the console configuration as well.

### Import says an acknowledgement is required, or strict mode refuses

- `acknowledge-required` (exit code 3 on the command line): the package is
  unsigned or signed by an unknown key. Tick **I accept this package
  knowingly**, or add the key to Settings > Artefacts, or pass
  `--accept-unsigned` / `--accept-unknown-key`.
- `strict-mode`: strict mode refuses anything but a trusted key. If it is
  locked by `config.yaml` (`strict-locked` when you try to turn it off),
  only an administrator of the host can change it.
- `invalid-signature`, `sums-mismatch`: always refused. Build or download
  the package again.
- `archive-changed`: another upload replaced the archive after it was
  checked. Check it again.
- `package-untrusted-strict` at publication: strict mode was turned on
  after the package was accepted by acknowledgement.

### `harvester-artefacts remove` fails on a machine without PyYAML

`remove` reads the saved plans (a package that a plan publishes cannot be
removed), and plans need PyYAML. Without it, the step says "remove reads
the saved plans; run it where the console is installed (PyYAML)" and the
command exits with 1, removing nothing. Run it where the console is
installed.
`pack`, `sign`, `verify` and `inspect` stay on the Python standard library.

## Artefacts on child clusters and undo (2.7.0)

### A removed item left an object in the child cluster ("kept-relabelled")

The console deletes only the objects that still carry its labels
(`harvester-ops.io/managed`, `artefact-item`, `artefact-cluster`). An
object whose labels were removed by hand is kept and named in the step.
Delete it yourself if it is no longer wanted. A delivered namespace that
holds a workload or a volume of someone else is kept too
(`kept-in-use`).

### The binding of a manifest stays "not applied yet"

Cluster API applies the ConfigMap only when the `Cluster` carries the
label `harvester-ops.io/art-<item>: "on"` and the child is reachable. Read
`kubectl get clusterresourcesetbinding -n <ns> <cluster> -o yaml` on the
Harvester cluster: an object the API of the child refuses (a quota, a
missing namespace, a LimitRange out of order) is named there. The deploy
action fails after its timeout (900 s by default) with the binding state.

### A removal or an undo waits on a finalizer

The console never removes a finalizer. A `HarvesterCluster` that stays
after Delete is named with its finalizer in the step (wait 120 s); a
`Cluster` stuck in Deleting usually means CAPHV lacks a right (see the 2.5.0
entry above). Fix the cause, then run Delete or the undo again.

### Creating a cluster says "harvester-cluster-leftover"

A `HarvesterCluster` of that name remains from an older `Cluster`. Run
**Delete** on the cluster (or `harvester-capi delete --name ns/name`): it
is removed when no `Cluster` owns it. An orphan without any owner is only
a warning; the creation adopts it.

### The undo says "undo-not-ours" for an item

Something with the name of the item exists but does not carry the
`harvester-ops.io/plan-run` mark of a run of the undone chain: it was
recreated since by another run, or by hand. The undo leaves it, and keeps
what depends on it (its cluster, the Terraform content). Either remove it
from where it was made, then run the undo again; or keep it, and untick
the item **and** the Terraform declarations. A Terraform declaration is
never destroyed while a cluster of the chain is still present (see
"undo-tf-carries-cluster" below).

A Terraform declaration counts as "ours" by its id alone: ids are never
reused. So a declaration that a later run of the same plan adopted is
still destroyed by the undo of the run that created it, once no cluster of
the chain is left.

### The undo says "undo-tf-carries-cluster"

A cluster that the Clusters phase of the chain names (created or adopted)
is still present: unticked, recreated by another run, or adopted. It may
use the network, storage class or image of the declaration, so the
declaration is kept. The destruction plan names these clusters on the
declaration. Remove the cluster first (tick it, or delete it where it was
made), then run the undo again; or untick the declaration to keep it.

### A registry delete stopped halfway

`harvester-registry delete` removes the Deployment, which carries the run
mark, last, after the console record. A delete that stops halfway (a
refused right, an API error, a cancel) leaves the Deployment, so the undo
still recognises the registry and finishes the removal when run again.

### The undo says "undo-already-done"

Everything the run created is gone. Apply the plan again to recreate it;
an undo is never undone.

## Child cluster access, GitOps, renewal and registries (2.8.0)

### The import into Rancher says "rancher-agent-image-missing"

The registration manifest pulls the Rancher agent (for example
`registry.rancher.com/rancher/rancher-agent:<version>` with Rancher
Prime). The console
probes it in the registries of the child cluster first. When the cluster
cuts the fallback to the Internet and no registry holds the image, the
import is refused before anything is applied. Mirror
`registry.rancher.com` in a registry of the cluster, publish a package
that carries the agent image, then import again. With the fallback open,
the same case is only a warning (`rancher-agent-image-fallback`).

### A GitOps entry stays not ready: the Kustomization fails

Read the state in the child cluster:

```
kubectl -n hops-gitops get kustomization,ocirepository,gitrepository
kubectl -n hops-gitops describe kustomization <entry>
```

- A source not ready: the registry, its CA or the Git credentials. An
  `oci` source whose artefact is missing times out (the tag is not checked
  beforehand); publish the package again.
- **A `ClusterRole` (or any cluster scoped object) in a `namespace`
  entry is refused by the impersonated account.** That is the lockdown at
  work, expected: the account `hops-gitops-<entry>` writes only in its team
  namespace. Move the object to a manifest item, or ask an administrator
  for `privileges: cluster`.
- `gitops-flux-not-locked`: the `flux` service was installed without the
  lockdown values. Apply the chart item `flux` again from the console.

### Every Flux object of a child cluster disappeared at once

The Flux CRDs come with the `flux2` chart. When CAAPH installs the
release again (seen on the bench when its namespace was changed), the
CRDs are removed and recreated, and every `OCIRepository`,
`GitRepository` and `Kustomization` goes with them. The
`ClusterResourceSet` of an entry does not apply its content again while
its binding is at the same checksum, so the entry then times out.

- Since 2.8.0 a `flux` release already posed keeps its namespace: the
  console never asks CAAPH to move it.
- To recover, have Cluster API apply the entries again: delete the
  `ClusterResourceSetBinding` of the child cluster on the Harvester
  cluster (`kubectl -n <ns> delete clusterresourcesetbinding <c>`), then
  touch one of its `ClusterResourceSet` objects (an annotation is
  enough). The objects come back within seconds.

### A renewal is refused: "renew-busy" or "renew-plan-running"

- `renew-busy`: an earlier renewal of this child cluster did not finish
  (time limit, console stopped), or another renewal holds the Harvester
  cluster. Its mark stays on the `Cluster`. **Finish the renewal**
  (`harvester-capi renew --name NS/C --finish`) waits again and cleans up.
  **Abandon** (`--abandon`, administrators) removes the mark only: the old
  templates and Secrets are kept and named.
- `renew-plan-running`: a plan (console or `harvester-plans`) holds the
  Harvester cluster. Wait for it to end.
- The reverse, `renew-running`: a renewal holds the Harvester cluster, so
  plans, undo and the Turtles uninstall are refused until it ends (up to
  its time limit, 3 h at most).

### Renewing a certificate says "registry-ca-transition-pending"

A new CA is in transition and some copies do not trust it yet. The
refusal lists each copy with its reason. Run `harvester-capi renew --name
NS/C --what ca` on each listed child cluster (`nodes`, `rke2-fetch`,
`service-ca`), `renew --finish` for `renewing`, deploy again the Flux
entries listed as `flux:<entry>`, and run `harvester-nodes` again for
`harvester-nodes`. Then run `renew-cert` again. Fleet entries are not in
the list (warning `registry-ca-fleet-unchecked`): deploy them again before
the switch.

### Building a package fails with HTTP 403

Some download sites refuse the default agent of Python (`get.rke2.io` for
`pack --rke2`). Since 2.8.0 the tools send a `User-Agent`. With an older
`harvester-artefacts`, use the 2.8.0 one on the connected machine.

### Harvester nodes is refused: "harvester-nodes-other-auth"

Another registry of the Harvester setting `containerd-registry` keeps a
password in Harvester (a Secret `rke.cattle.io/auth-config` of
`fleet-local`). When the mirrors change, Harvester 1.9 rewrites those
Secrets from the setting with the host only, and would erase that
password. Nothing was written. Remove those credentials in Harvester
first (Settings > containerd-registry), run Harvester nodes, then enter
them again in Harvester afterwards.

### A child cluster without Internet stays at 10 %

CAPHV (v0.10.1) writes `package_update: true` and `packages:
[qemu-guest-agent, iptables]` in the cloud-init of every machine. Without
Internet, zypper waits for each repository of the image to time out
(about 5 minutes each, seen on harvlab2 with Leap 15.6) before the RKE2
commands run.

Since 2.8.1, a cluster created with `offline: true` turns the package
update off and moves the zypper repositories aside during the first
boot (see the operating procedure, child cluster without Internet). If
the lines `Timeout exceeded when accessing ...` still show in the console
of the machine (`kubectl -n <ns>-vms logs <virt-launcher pod> -c
guest-console-log`):

- the cluster was created before 2.8.1: its templates do not carry
  `agentConfig.additionalUserData`. Delete it and create it again;
- read the cloud-init Secret of the machine on the Harvester cluster
  (`<machine>-cloud-init` in the VM namespace, key `userdata`). It should
  hold `package_update: false` and the `bootcmd` line
  `harvester-ops-repos-aside`. If `package_update` is still true, the
  CAPHV version merges cloud-init differently: use an image whose
  repositories point at a mirror the machines reach, or that has none;
- the cluster uses `offline_packages: keep` and a repository of the image
  is not reachable: use `skip`, or fix the repositories of the image.

`cloud-init status` reports an error on these nodes with
`offline_packages: skip`: the package module fails without repositories,
by design. It is not the cause of a stuck cluster.

The end-to-end offline creation is not verified for real yet (2.8.1
still has to be run on the bench).

### A node of an offline cluster has no zypper repository

With `offline_packages: skip`, the repositories are set aside during the
first boot and put back by the first command without Internet. They stay
aside when cloud-init stops before that command, or when something wrote
into `/etc/zypp/repos.d` in the meantime. Read
`/var/log/cloud-init-output.log`: `harvester-ops: ... is not empty,
repositories left in ...` or no `harvester-ops:` line at all. Restore by
hand, as root:

```
for d in /etc/zypp/repos.d /etc/zypp/services.d; do
  [ -d "$d.harvester-ops-offline" ] || continue
  mkdir -p "$d" && mv "$d.harvester-ops-offline"/* "$d"/ && rmdir "$d.harvester-ops-offline"
done
```

Check first that no file of the same name exists in both directories.
For an image whose repositories reach a mirror of the local network, use
`offline_packages: keep`.

### Harvester nodes merged, but images still come from the origin

The nodes of the Harvester cluster that hosts a bootstrap registry may
not reach the registry address themselves. Since 2.8.1, Harvester nodes
warns about this pairing (`harvester-nodes-self-hosted`, not blocking). Seen on harvlab2 (one node):
the address is held on the node, and a connection from the node to it
times out. containerd then falls back to the origin registry. Wire the
Harvester nodes to a registry they can reach: a client registry, or the
bootstrap registry of another Harvester cluster. Check from a node with
`curl -k https://<registry>/v2/` (401 expected).

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
