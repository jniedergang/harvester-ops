# Artefact packages and registries (2.6.0), artefacts on child clusters (2.7.0), registries finished (2.8.0)

Child clusters made by Cluster API often run where the Internet is not
reachable. 2.6.0 brings what is needed to give them their images and
charts anyway:

- a signed **artefact package**, built on a connected machine and imported
  into the console;
- **registries** declared in the console: the registry of your
  organisation, or a **bootstrap registry** that the console poses on the
  Harvester cluster;
- **publishing** a package into a registry;
- **child clusters** that pull from these registries, set at creation;
- **services** (CAAPH) that install a chart published in a registry.

2.7.0 applies what a package brings to the child clusters: charts through
CAAPH, manifests and policies through a `ClusterResourceSet` whose objects
the console removes itself, a base of policies and automation accounts with
short tokens. See "Artefacts on child clusters (2.7.0)" below. The Registry
and Artefacts phases of the plans, and undoing a deployment, are described
in [bare-metal.md](bare-metal.md).

2.8.0 adds two package kinds: `gitops` (a Flux artefact, see
[gitops.md](gitops.md)) and `rke2` (RKE2 itself, for a child cluster
bootstrapped without the Internet). It finishes the registries: the read
password leaves its separate Secret, the CA of a bootstrap registry is
kept and its certificate can be renewed, and a registry can be merged into
the Harvester nodes. See "Registries finished (2.8.0)" below.

## The package

A package is one file, `<name>-<version>.tar.gz`, with an optional detached
signature `<name>-<version>.tar.gz.sig`.

- `name`: lower case letters, digits and dashes, a letter first, 40
  characters at most.
- `version`: `x.y.z` (with an optional suffix) or a date
  `YYYY.MM[.DD][-n]`.

Inside, one root directory:

```
<name>-<version>/
  SHA256SUMS            first member of the archive
  artefacts.yaml        the index (written in JSON, valid YAML)
  charts/<chart>-<version>.tgz
  images/oci-layout     OCI image layout
  images/index.json     one entry per image
  images/blobs/sha256/  layers, each stored once
  manifests/<item>/<file>.yaml
```

`SHA256SUMS` comes first, so the import checks every file while it
extracts the archive. A file missing from the list, or with another
checksum, refuses the whole package.

Item kinds: `chart`, `images`, `manifest`, `policy` (a manifest of
policies, delivered like a manifest since 2.7.0), and since 2.8.0
`gitops` and `rke2`. Manifests and policies are not published in a
registry.

- **`gitops`**: YAML files (`files`, required), published as a **Flux
  artefact**: one layer `application/vnd.cncf.flux.content.v1.tar+gzip`
  (deterministic tar.gz), repository
  `<registry>/harvester-ops/gitops/<package>/<item>`, tag = the version of
  the package. It is posed by a GitOps entry, never by Apply
  (`artefact-kind-gitops`). Its content is judged by the rules of the
  manifests at publication (`publish-gitops-refused`): each namespaced
  object must carry `metadata.namespace` (found on the bench). See
  [gitops.md](gitops.md).
- **`rke2`**: an exact RKE2 version (`v1.34.11+rke2r1`), its three files
  `rke2/<version>/{rke2.linux-amd64.tar.gz, sha256sum-amd64.txt,
  install.sh}`, and the system images of this version for one CNI. It
  serves a child cluster created with `offline: true` (see
  [operating-procedure.md](operating-procedure.md), Cluster API).

### Building a package: `harvester-artefacts pack`

`pack` runs on any connected machine with Python 3. It uses the Python
standard library only: no PyYAML, no podman, no skopeo, no helm. It reads a
description in JSON (in YAML only if PyYAML is installed; otherwise it says
so).

```json
{"name": "socle", "version": "2026.11",
 "description": "podinfo and its image",
 "items": [
   {"name": "podinfo", "kind": "chart",
    "chart": {"repo": "oci://ghcr.io/stefanprodan/charts", "name": "podinfo",
              "version": "6.9.2"},
    "images": ["ghcr.io/stefanprodan/podinfo:6.9.2"]},
   {"name": "proof", "kind": "images",
    "images": [{"ref": "ghcr.io/stefanprodan/podinfo:6.9.2",
                "as": "docker.io/harvops-proof/podinfo:2.6.0"}]},
   {"name": "quotas", "kind": "manifest", "files": ["quotas.yaml"]}]}
```

```bash
harvester-artefacts pack socle.json --out dist/ --sign-key ~/.ssh/artefacts_ed25519
```

- **Charts** come from an OCI registry (`oci://`) or a Helm repository
  (`https://`, its `index.yaml`, the chart checksum of the index checked).
  A chart item may carry `values`: services installed from the registry
  start from them.
- **Images** are copied layer by layer, one platform, `linux/amd64` by
  default (`--platform os/arch` for another). A layer shared by several
  images is stored once. The images a chart needs are listed by hand: `pack`
  does not render the chart.
- **`as`** stores an image under another name, with a tag. It serves a
  name that does not exist on the Internet, for instance to prove that a
  cluster pulls from its registry.
- **Manifests** are YAML files, read next to the description.
- **`--creds FILE`**: credentials of the source registries and Helm
  repositories, a JSON file `{"host": {"username": "...", "password":
  "..."}}`. The file must be private (`chmod 600`), or `pack` refuses it.
  Credentials are never given in an argument and never written to the
  package. They are sent only to their own host: a redirection to another
  host drops them.
- **`--plain-http HOST`** (repeatable): a registry or a Helm repository
  spoken to in clear (test setups). Any `http://` source needs it, even
  without credentials.
- **`--sign-key FILE`** signs the package at the end (see below).
- **`--rke2 VERSION [--cni calico]`** (2.8.0) builds a package
  `rke2-<x.y.z-rke2rN>` with one `rke2` item, without a description; given
  with a description, the item joins it. The archive is checked against
  `sha256sum-amd64.txt`. The system images are the list
  `rke2-images-all.linux-amd64.txt` of the version, filtered by the CNI
  (`calico`, `canal`, `cilium`, `flannel`, `none`), copied like any image.
  Downloads send a `User-Agent` (`get.rke2.io` refuses the default one of
  Python with HTTP 403).

```bash
harvester-artefacts pack --rke2 v1.34.11+rke2r1 --cni calico --out dist/ --sign-key ~/.ssh/artefacts_ed25519
```

Exit codes: 0 built, 1 failure (source unreachable, chart not found,
checksum wrong), 2 description refused (unknown key, invalid name,
duplicate image name...).

### Signing and checking

| Command | What it does | Exit code |
|---|---|---|
| `sign PACKAGE --key FILE` | `ssh-keygen -Y sign` with the namespace `harvester-artefacts`; writes `PACKAGE.sig` | 0, 1 |
| `verify PACKAGE [--signers FILE]` | trust state of the signature | 0 only for `valid`, 1 otherwise |
| `inspect PACKAGE [--signers FILE]` | index, files, size and trust state, without extracting | 0 readable, 2 not a package |
| `import PACKAGE [--accept-unsigned \| --accept-unknown-key] [--person NAME]` | import into the console (state directory) | 0 accepted, 2 refused, 3 acknowledgement needed |
| `list` | packages of the console | 0 |
| `remove NAME VERSION` | remove a package from the console | 0, 2 refused |
| `publish NAME VERSION --cluster C --registry R [--items a,b] [--dry-run]` | copy into a registry (see Publishing) | 0, 1 failure, 2 refused |

Signing and checking need `ssh-keygen` from OpenSSH 8.2 or later. The
namespace `harvester-artefacts` differs from the one of the console
releases: a release signature is not valid for a package, and the reverse.

`pack`, `sign`, `verify` and `inspect` stay on the standard library.
`remove` reads the saved plans, so it needs PyYAML, as `import` and
`publish` do: run those three where the console is installed.

## Trust

Each package gets a trust state:

| State | Meaning |
|---|---|
| `valid` | signature right, made by a trusted key |
| `unknown-key` | signature right, the key is not among the trusted keys |
| `unsigned` | no `.sig` |
| `invalid` | wrong signature, signature of another file, or unreadable `.sig` |
| `sums-mismatch` | a file differs from `SHA256SUMS`, or is not listed (seen at extraction) |

The console decides:

| State | Permissive mode (default) | Strict mode |
|---|---|---|
| `valid` | accepted | accepted |
| `unknown-key` | accepted after the acknowledgement `unknown-key` | refused |
| `unsigned` | accepted after the acknowledgement `unsigned` | refused |
| `invalid`, `sums-mismatch` | refused | refused |

The acknowledgement must name the state found; it is never guessed. In the
**Import** window, an unsigned package or a package of an unknown key shows
what it means ("This package is not signed. Its origin cannot be
checked."), the key fingerprint when there is one, and a checkbox **I
accept this package knowingly**. **Accept** stays greyed until the box is
ticked. The acknowledgement is bound to the archive checked: if another
upload replaced it in the meantime, the console refuses (`archive-changed`)
and asks to check again. Through the API, `POST
/api/artefacts/<name>/<version>/accept` with an `acknowledge` must carry
the `sha256` returned by the pre-check (`sha256-required`, 400,
otherwise).

**Journal.** The `trust` step of the import action writes, for example,
`accepted UNSIGNED package socle-2026.11 after acknowledgement by alice`
(or `UNKNOWN-KEY`, or `accepted package ... signed by <principal>
(<fingerprint>)`). The same facts go to `trust.json` next to the package:
state, principal and key fingerprint if any, person, date, mode. A package
accepted after an acknowledgement keeps its badge (**Unknown key**,
**Unsigned**) in the Artefacts tab.

**Strict mode and trusted keys.** Settings > Artefacts (administrators):

- **Strict mode** switch, saved in `<state>/artefacts/settings.json`.
- **Trusted keys**, the lines of an OpenSSH `allowed_signers` file, kept in
  `<state>/artefacts/signers` (0600). Paste a line (principal, then the
  public key) to add a key; `namespaces="harvester-artefacts"` is added
  when it is missing. Accepted keys: `ssh-ed25519`, `ecdsa-sha2-*`,
  `ssh-rsa` of 3072 bits or more. A key is removed by its fingerprint.

`artefacts: {strict: true}` in `config.yaml` locks strict mode on: the
switch is greyed with the reason, and turning it off is refused
(`strict-locked`). The lock covers **the mode, not the keys**: an
administrator can still add a trusted key, and so make a signature of
their own `valid`. Restrict who is administrator if that matters.

Strict mode turned on after an import also applies to publishing: a package
that was accepted by acknowledgement can no longer be published
(`package-untrusted-strict`).

**Importing in the console.** Cluster API > Artefacts > **Import**: send
the archive and its `.sig` (**Send and check**), read the trust state, then
**Accept**. The console checks there is room for twice the archive plus
1 GiB before receiving it (507 otherwise). The package is extracted and
checked into `<state>/artefacts.d/<name>-<version>/` (0700), with
`trust.json` and its `.sig`; the uploaded archive is then deleted. A
package already imported must be removed before it is imported again.
**Remove** is refused while a saved plan publishes the package
(`package-in-use`); the registries keep what was published.

## Registries

A registry is declared in the console without any secret
(`<state>/registries.d/<name>.yaml`, revision, 409 on a stale revision).
Its accounts live only in Secrets of the Harvester clusters that use it.
Cluster API > **Registries** lists them, attached or not to the management
cluster shown in the header, with their address, usage, mirrors, published
packages, the clusters and plans that use them, and their live state.

### Client registry

**Declare a client registry** (administrators): an existing registry of
your organisation.

- **Name**: lower case letters, digits and dashes, 30 characters at most.
- **Address**: `host[:port]`, without `https://` nor path.
- **CA**: the public CA certificate (PEM); empty means the system CAs.
- **Usage**: images (the nodes pull images from it), charts (CAAPH pulls
  charts from it), or both.
- **Mirrors**: the origin registries it serves (`docker.io`, `ghcr.io`...).
  A child cluster pulls the images of these origins from it.

**Attach** (operators) gives the write account (`push`) and the read
account (`pull`) for one Harvester cluster. They are posed as Secrets in
the namespace `harvester-ops-registry` of that cluster (`<registry>-push`,
`<registry>-pull`, and `<registry>-ca` for the CA), with the identity of
the person. They are sent once and never shown again. On the command line,
they arrive on standard input, never in an argument.

- Without a separate read account, the write account is used for both, with
  the warning `registry-pull-is-push`: the read account ends in a file on
  every node of the child clusters.
- **Whoever can read Secrets in `harvester-ops-registry` on the Harvester
  cluster holds the write account of your registry.** The write account
  never leaves that namespace: child clusters get the read account only.

**Check** (operators) reads the accounts on the cluster and probes the
registry from the console, with each account, without writing. Pick a
**VM network** next to the button to probe it also from that network (see
below). Findings: `registry-unknown`, `registry-not-attached`,
`registry-unreachable`, `registry-ca-invalid`, `registry-auth-refused`,
`registry-no-usage` (blocking); `registry-pull-is-push`,
`registry-insecure`, `registry-write-port-open` (bootstrap registry whose
write port accepts every address, see Settings > Registries),
`registry-unreachable-from-vm-network`, `registry-vm-network-unchecked`
(warnings); `registry-vm-network-ok` (information). `registry-insecure` comes from
`insecure_skip_verify: true` in a declaration file: the nodes then do not
check the certificate.

**Reachability from the VM network (2.6.1).** The console and the
Harvester cluster reaching a registry does not mean the child VMs do: a
VM network can lack a route to it (VLAN without a route, firewall). So the
checks run a short-lived **probe pod** attached to the VM network, as a VM
would be:

- It is a Job in its own namespace, `harvester-ops-probe`, attached by
  Multus to the network (NetworkAttachmentDefinition), on the nodes that
  carry its cluster network. The image is `bci-busybox`, shipped in the
  registry bundle and pinned by digest; it is imported on the nodes with a
  bootstrap registry (on a cluster without one, the node pulls it from
  `registry.suse.com`, or the probe says the image is missing).
- It takes an address the way a VM does: the one the IPAM of the network
  gives, otherwise a DHCP lease, given back when the probe ends.
- For each address it tests, in order: the name with the DNS server the
  network gives (when the registry is a name), a route through the VM
  network, a TCP connection, then a TLS handshake with the name sent as
  SNI (an HTTP answer, 401 included). The CA itself is checked by the
  console, not by the probe. The TLS client of the probe (BusyBox) reads
  RSA and ECDSA certificates only.
- An address given by the IPAM of the network without a gateway (for a
  registry outside its subnet) or without a DNS server (for a registry
  given by name) is not judged: a VM may receive them otherwise. The
  finding then says so (`ipam-incomplete`) instead of "unreachable".
- Least privilege: capabilities `NET_ADMIN` (address and routes) and
  `NET_RAW` (DHCP socket) only, all others dropped, no service account
  token, read-only filesystem but a 1 MiB `/tmp` in memory, 32 MiB of
  memory. These are the capabilities the spike of 2.6.0 proved enough.
- It is removed at the end of every check, success or failure; the Job
  also has a deadline and `ttlSecondsAfterFinished`, so it goes away even
  if the console stops in the middle.
- It needs the right to create a Job in `harvester-ops-probe` (and the
  namespace the first time). Without it, the check says so.

Where it runs: **Check** of a registry with a VM network chosen (a fresh
probe each time), and the creation check of a child cluster and the plan
checks, from the main network of the cluster (`network`), once for all
its registries that serve images. These checks are run again as you type
in the creation window: a result is kept 5 minutes per Harvester cluster,
network and addresses, so typing does not start a pod each time. A probe
that could not judge is not kept (it often depends on the rights of the
person). A
bootstrap registry posed by a plan is probed when the Clusters phase
starts (it does not answer before).

Findings: `cluster-registry-vm-network-ok` (information, with the probe
address), `cluster-registry-unreachable-from-vm-network` (with the step
that failed: name, route, TCP or TLS; a warning, **blocking when the
cluster cuts the fallback to the Internet**, since its nodes would then
pull nothing), and `cluster-registry-vm-network-unchecked` when the probe
cannot judge, with the reason: no VM network, network not found, rights
missing, probe image missing, no address (no DHCP lease within 20 s and no
IPAM), address of the IPAM without gateway or DNS, pod not started (the
Multus or scheduling message), no result in time. Same codes without the `cluster-` prefix for Check. The bootstrap
registry window says `registry-vm-network-unchecked`: the address is not
served yet, it is probed when a child cluster or a plan uses it.

Limits: the probe is a pod on a Harvester node, not a VM; it uses the main
network of the cluster only; a firewall that treats pods and VMs
differently (by MAC address, for instance) is not seen.

**Forget** removes the Secrets of the registry (both accounts and the CA)
from every attached Harvester cluster, with your identity, then the
declaration. It is refused while a plan, a cluster template or a child
cluster uses the registry; the window then names what uses it. Forget is
an action of `harvester-registry forget`, tracked in Activity
(`registry-forget:<name>`) from the start: each Secret removed is a step.
When a cluster refuses the removal, the action ends in error with the
steps already done, and the declaration stays so that you can try again.
An attached cluster that is no longer in the console is named in a step:
its Secrets stay there, remove them by hand.

### Bootstrap registry

**Bootstrap registry** (administrators) poses a registry on the Harvester
cluster: CNCF Distribution 3.1 in the image maintained by SUSE
(`registry.suse.com/suse/registry:3.1`), pinned by digest and shipped in
the release (see [install.md](install.md)). Nothing is pulled from the
Internet.

The window has three tabs, **Parameters**, **Checks**, **Summary**;
**Launch** stays greyed while a check blocks, with the reason.

- **Address**: a free IPv4 address of the management network, outside the
  IP pools, served by the kube-vip of Harvester. The nodes of the child
  clusters must reach it.
- **Volume (GiB)**: 20 by default, from 5 to 2048, checked against the room
  of Longhorn (over-provisioning and real space, replicas of the class on
  distinct nodes).
- **Storage class**: a Longhorn class.
- **DNS name** (optional): added to the certificate next to the address.
- **Mirrors**: the origin registries it serves. They are fixed at
  deployment.

Checks: `registry-address-invalid`, `registry-address-in-pool`,
`registry-address-vip`, `registry-address-answers`, `registry-name-taken`,
`registry-images-missing`, `registry-tools-missing`,
`registry-storage-class-unknown`, `registry-replicas-nodes`,
`registry-room-short`.

**What is posed.** In the namespace `harvester-ops-registry`: one
Deployment (one replica, `Recreate`), one Longhorn PVC `<name>-data`, one
Service `<name>` of type LoadBalancer annotated
`kube-vip.io/loadbalancerIPs` (no NodePort), the Secrets of the
certificate, of the htpasswd entries and of the accounts, and a ConfigMap.
The pod has **two containers on the same volume**, because the htpasswd
authentication of Distribution has no rights (any account could write):

| Port | Instance | Accounts |
|---|---|---|
| 443 | read only (any write gets 405) | `push` and `pull` |
| 5443 | read and write | `push` only |

The child clusters read on 443. The console publishes on 5443. Since
2.6.1 the two ports are **two LoadBalancer Services at the same address**
(`<name>` for 443, `<name>-write` for 5443), both served by the kube-vip of
Harvester, so that the write port can be reserved to the console.

**Write port reserved to the console (2.6.1).** Settings > Registries
(administrators) holds the networks of the console allowed on 5443
(IPv4 addresses or networks, comma separated). The window suggests the
address the console leaves from toward each cluster that carries a
bootstrap registry. The setting is read at each gesture, without restart.

- It becomes `loadBalancerSourceRanges` on the `<name>-write` Service:
  another host gets no answer on 5443 (the connection times out), port 443
  stays open to every address. A single Service with source ranges would
  cut 443 for the VMs too, hence the second Service.
- Saving the setting brings every bootstrap registry the console declares
  to it: one tracked action per Harvester cluster
  (`registry-write-access:<cluster>`, `harvester-registry write-access`),
  with your identity. This is also how a registry posed by 2.6.0 (5443 on
  its main Service) is migrated: the port leaves the main Service first,
  then the write Service is posed, so writes stop for a few seconds and
  reads never. A new bootstrap registry is posed with the setting.
- Empty, the write port accepts every address, as in 2.6.0, and Check
  says `registry-write-port-open`. A damaged settings file is never read
  as "open": deploying and `write-access` are refused until the setting is
  saved again.
- A bootstrap registry name cannot end with `-write` (the name of the
  write Service of another registry), and Delete removes only the objects
  labelled with the registry name.
- What it protects: the write port from the other hosts of the networks
  that reach the address (the child VMs included). What it does not: it is
  not authentication (a write still needs the `push` account, and `pull`
  is refused there); it does not see through a NAT or a proxy (the cluster
  must see the real address of the console: behind a NAT, give the
  translated address); and it filters on the address that reaches the
  Harvester node, so a host that can borrow an allowed address gets
  through.

- **TLS always.** The console generates a CA (10 years) and a server
  certificate (2 years), or takes your certificate (`--cert`, `--key`,
  `--ca`). Up to 2.7.x the generated CA key was erased after the
  deployment. **Since 2.8.0 it is kept** in the Secret `<name>-ca-key`, and
  the CA carries a critical `nameConstraints` limited to the address of the
  registry (and its DNS name when given). The certificate can then be
  renewed: see "Registries finished (2.8.0)". Deploying again still gives
  a new CA.
- **Two accounts**, `push` and `pull`, with random passwords (32 bytes).
  They exist only in Secrets of the Harvester cluster. There is never an
  anonymous access.
- The pod runs as uid 499, read-only root filesystem, no capability,
  512 MiB memory limit, probes every 60 s.
- **The image is imported on the nodes** listed for this cluster in the
  console configuration (`ctr images import` over SSH), as Cluster API
  images were. **A node added to the cluster later does not get it**: if
  the pod is moved there, it stays in `ErrImageNeverPull` (see
  [troubleshooting.md](troubleshooting.md)).
- The deployment waits until `/v2/` answers 401 without an account, then
  200 with the read account. Then the declaration is saved.

**Delete** removes the registry from the cluster. Its volume is kept, so a
new deployment finds its images again, unless you tick **Delete its data
too** and type its name. Delete is refused while a child cluster, a
service, a plan or a cluster template uses it (`registry-in-use`, with the
names).

**A failed deployment leaves its objects.** If the registry does not become
ready in time, the objects and the volume stay on the cluster and no
declaration is saved; a new attempt from the window is refused
(`registry-name-taken`, often with `registry-address-answers`). The
Registries tab lists such objects as a **Leftover** row (found by the
console labels in `harvester-ops-registry`), with the objects left and the
plan that posed them. **Delete** on that row removes them; the volume is
kept unless you type the name, as for a declared registry. A deployment
plan that posed them is not blocked: its resume takes them over (the
checks are replayed with the origin of the plan) and deploys again. From
the command line: `harvester-registry leftovers --cluster C`, then
`harvester-registry delete NAME --cluster C`.

**No cleanup in 2.6.0.** Distribution needs a stopped or read-only registry
for its garbage collection, and deletes are disabled. What is published
stays. Watch the volume use in the details of the registry.

### Where things go in a registry

An image `<host>/<path>:<tag>` is published as
`<registry>/<host>/<path>:<tag>`:

- `docker.io/library/nginx:1.27` becomes `<registry>/docker.io/library/nginx:1.27`;
- a port in the host is written `_<port>` (`localhost:5000/x` becomes
  `<registry>/localhost_5000/x`), since `:` is not allowed in a repository
  name.

Two images with the same path on two hosts never mix. Charts go under
`<registry>/charts/<chart>:<version>` (a `+` in the version becomes `_`).

Since 2.8.0:

- a `gitops` item goes to
  `<registry>/harvester-ops/gitops/<package>/<item>:<version>`, a Flux
  artefact (configuration `application/vnd.cncf.flux.config.v1+json`,
  layer `application/vnd.cncf.flux.content.v1.tar+gzip`);
- an `rke2` item goes to `<registry>/harvester-ops/rke2`: each of the
  three files as a blob, plus an OCI manifest of artifact type
  `application/vnd.harvester-ops.rke2.v1+json` (empty configuration, one
  titled layer per file). The tag is the version with `+` replaced by `_`
  (OCI tags refuse `+`), for example `v1.34.11_rke2r1`.

## Publishing

**Publish** on a package of the Artefacts tab (operators): choose a
registry attached to the Harvester cluster, the items, read the checks,
then publish.

- **Checks come before any write**: `registry-unknown`,
  `registry-not-attached`, `registry-no-usage` (an image to a registry of
  charts only, or the reverse), `package-untrusted-strict`,
  `publish-item-unknown`, and `publish-tag-differs`.
- **`publish-tag-differs`**: a tag already present with another content.
  Nothing is overwritten, and nothing is written at all.
- **Idempotent**: a tag present with the same digest is skipped. A layer
  already in the registry is not sent again (or is mounted from another
  repository of the same registry). An interrupted publication is resumed
  by publishing again.
- Images first, then charts, then the `gitops` and `rke2` artefacts
  (2.8.0); manifests are skipped. The progress shows `images 3/14` and the
  MiB sent.
- A `gitops` item needs a registry that serves charts (a Flux artefact is
  an OCI artefact, like a chart); an `rke2` item needs one that serves
  images (`registry-no-usage` otherwise).
- The declaration of the registry keeps the list of packages published
  (names, versions, date, person).

The write account is read from the Secret `<registry>-push` of the Harvester
cluster and never shown. On the command line: `harvester-artefacts publish
NAME VERSION --cluster C --registry R [--items a,b] [--dry-run]`;
`--dry-run` lists what would be sent and the findings.

## Child clusters on their registries

In the creation window of Cluster API > Clusters, the **Registries** menu
holds an ordered list of registries, declared and attached to this Harvester
cluster (a registry not attached is shown but cannot be chosen). Cluster
templates and plan entries take the same list, with `{{variables}}`.

- **Set at creation, changed by a renewal.** RKE2 reads its registry
  configuration when it starts. Since 2.8.0, the registries of an existing
  cluster change by renewing its machines (`harvester-capi renew --what
  registries`, see [operating-procedure.md](operating-procedure.md),
  Cluster API).
- **Order**: for each mirrored origin, the registries that serve it are
  tried in the order of the list; the first is preferred, the next ones are
  its fallbacks.
- **Mirrors**: per registry, the origins it serves for this cluster, chosen
  among those of its declaration.
- The console writes `/etc/rancher/rke2/registries.yaml` itself, with
  files of the control plane and worker templates: mirrors with a
  `rewrite` `^(.*)$` to `<origin>/$1` (the layout above), and per registry
  its account and its CA, in `/etc/rancher/rke2/tls/harvester-ops/<registry>/ca.crt`.
  Only registries that serve images are used. It does not use the
  `privateRegistriesConfig` field of CAPRKE2: in v0.25.2 that field keeps
  the account and the CA of one registry only, and writes every CA to the
  same file (seen on the bench, see "Seen for real").
- **Secrets** in the namespace of the cluster on Harvester:
  `<cluster>-registries` (`registries.yaml` and one `ca-<registry>.crt`
  per registry, read by the nodes at their first boot) and
  `<cluster>-registry-<registry>-ca` (`Opaque`, `ca.crt` only). Since
  2.8.0 the read password is stored once, in `<cluster>-registries`; the
  label `harvester-ops.io/registry-<registry>: used` on that Secret tells
  which clusters use a registry. Up to 2.7.x a third Secret,
  `<cluster>-registry-<registry>-auth`, held the read account again (see
  "Registries finished (2.8.0)"). All are labelled to be removed with the
  cluster. **Only the read account reaches the nodes**; the write account
  never does.
- **The read password ends up in the cloud-init of the VMs.** CAPRKE2
  inlines `registries.yaml`, read password included, into the bootstrap
  Secret of each Machine, and CAPHV copies it into the cloud-init Secret
  of the VM in its namespace on Harvester (which may be `default`).
  Anyone who can read Secrets there, or see the user data of a VM in
  Harvester, can read the read account. `privateRegistriesConfig` does the
  same; only the read account travels this way.

**Fallback to the Internet.** containerd tries the mirrors, then the origin
registry. 2.6.0 **never cuts this fallback by default**, bootstrap registry
included.

- **No fallback to Internet** (`no_fallback: true` on an entry) cuts it.
  RKE2 cuts it for **every** mirrored registry of the cluster at once: one
  decision per cluster. An image absent from the registries then fails
  instead of coming from the Internet.
- It is refused (`cluster-registry-no-fallback-system`, blocking) while
  `docker.io` or `registry.rancher.com` is mirrored and no registry of the
  list carries the RKE2 system images (`rancher/rke2-runtime` in a package
  published to it): the nodes could not start. The creation form greys the
  box and says why.
- This replaces the earlier design, where a bootstrap registry cut the
  fallback by default.

Findings of the creation: `cluster-registry-unknown`,
`cluster-registry-not-attached`, `cluster-registry-mirror-unknown`,
`cluster-registry-no-fallback-system` (blocking),
`cluster-registry-no-images` (warning).

## Charts from a registry (Services)

In Cluster API > Services, **From a registry** offers the charts published
in the registries attached to this cluster (greyed with the reason when
there is none).

- CAAPH reads the chart at `oci://<registry>/charts`, with the chart name
  and version of the package.
- Values: those of the package item first, yours laid over them.
- Credentials: the read account, in a Docker `config.json` Secret
  `<cluster>-chart-<registry>`; CA: the Secret
  `<cluster>-registry-<registry>-ca` of the cluster (posed if it is
  absent). `insecureSkipTLSVerify` is never set.
- The images of the chart are pulled by the child cluster under their
  origin names, through its mirrors. If the cluster does not pull from this
  registry, the warning `service-registry-not-mirrored` says the images
  will come from their origin.
- Removing the service removes its credentials Secret when no other service
  uses it; never the CA of the cluster.

Findings: `service-registry-unknown`, `service-registry-no-charts`,
`service-registry-not-attached`, `service-package-missing`,
`service-item-not-chart` (blocking); `service-chart-not-published`,
`service-registry-not-mirrored` (warnings).

## Artefacts on child clusters (2.7.0)

A package item reaches a child cluster `<ns>/<c>` made by Cluster API. Every
object the console writes for it lives on the Harvester cluster, in `<ns>`:

| Item kind | Written on Harvester | Applied by |
|---|---|---|
| `chart` | a `HelmChartProxy` named after the item, label `harvester-ops.io/svc-<item>: "on"` on the `Cluster` | CAAPH, from a registry where the package is published (as "From a registry" in Services) |
| `manifest`, `policy` | a ConfigMap and a `ClusterResourceSet` named `art-<item>` (`strategy: Reconcile`), label `harvester-ops.io/art-<item>: "on"` on the `Cluster` | Cluster API, in the child cluster |

Each of them carries the labels `harvester-ops.io/managed`,
`harvester-ops.io/artefact-item` and `harvester-ops.io/artefact-package`,
the annotations `harvester-ops.io/artefact-version` and
`harvester-ops.io/applied-by`, and, from a plan, the origin of the plan
(`harvester-ops.io/plan` label, `harvester-ops.io/plan-run` annotation).

### Manifests and policies: removal followed by the console

Cluster API never removes what a `ClusterResourceSet` applied, and a
`Reconcile` set leaves in place an object taken out of its ConfigMap (seen
for real, see below). So the console follows the removal itself:

- Each object of the content gets the labels `harvester-ops.io/managed`,
  `harvester-ops.io/artefact-item` and `harvester-ops.io/artefact-cluster`.
- **Update** (a new version of the item): the objects that leave the
  content are written in an annotation of the ConfigMap before anything
  else, the new content is applied, the console waits for the binding to
  carry the new checksum, then deletes those objects in the child cluster.
  A run stopped halfway finishes the job at the next update or removal.
- **Removal**: the `ClusterResourceSet` first (no more reapplication), the
  label of the `Cluster`, then the objects one by one, namespaced first,
  `Namespace` and cluster scoped kinds last, then the ConfigMap. Every
  deletion is judged by the server (`--dry-run=server`) before the first
  one is made.
- **Only the objects that still carry the labels go.** An object changed by
  hand (labels removed) is kept and named (`kept-relabelled`). A delivered
  `Namespace` is deleted only if it holds nothing foreign (a workload or a
  volume that is not the item's): `kept-in-use`. A kind no longer served
  counts as gone. A ConfigMap deleted by hand loses the list of objects:
  the removal says so, and the labelled objects stay.
- A child cluster already deleted: only the `ClusterResourceSet` and its
  ConfigMap are removed ("its objects left with it").

Objects accepted in a manifest: any namespaced kind outside the system
namespaces of the child (`kube-system`, `*-system`, `cattle-*`,
`tigera-operator`...); cluster scoped, only `Namespace`, `ClusterRole`,
`ClusterRoleBinding` (never to `cluster-admin`, never to a `system:*`
subject), `StorageClass`, `PriorityClass` and `IngressClass`. Refused:
CRDs, webhooks, admission policies, `APIService`, `Node`,
`PersistentVolume`, `CSIDriver`, `RuntimeClass` (an operator comes with a
chart). A Secret of type `kubernetes.io/service-account-token` is refused.
500 objects and 900 KiB at most per item. A `List` is unfolded, and so are
the `<Kind>List` of Kubernetes groups; a custom kind ending in `List` stays
an object. A `ClusterRole` with wildcard rules bound by a
`ClusterRoleBinding` is accepted: the person applying it can read the
admin kubeconfig of the child cluster anyway (rule below).

### Base of policies, roles and automation accounts

The console brings a base of policies, **unticked by default**. Ticked, it
lays in each team namespace (1 to 16 per cluster, never a system
namespace, label `harvester-ops.io/team: "true"`):

- `ResourceQuota` `hops-quota`: `requests.cpu: "4"`, `requests.memory:
  8Gi`, `limits.cpu: "8"`, `limits.memory: 16Gi`, `pods: "50"`,
  `persistentvolumeclaims: "10"`, `requests.storage: 200Gi`,
  `services.loadbalancers: "2"`;
- `LimitRange` `hops-limits`: default 500m and 512Mi, default request 100m
  and 128Mi, max 2 CPU and 4Gi (each value can be changed; a request above
  its default, or a default above its max, is refused);
- `NetworkPolicy`: `hops-default-deny` (all pods, in and out),
  `hops-allow-same-namespace`, `hops-allow-dns` (to `kube-dns` in
  `kube-system`, UDP and TCP 53), `hops-allow-ingress` (from the RKE2
  ingress controller, `kube-system`, pods
  `app.kubernetes.io/name: rke2-ingress-nginx`).

A team namespace that already exists without the labels of the console
blocks (`team-namespace-taken`); the base never writes into `default` nor
into a namespace it did not create.

- **Roles by group**: a `RoleBinding` in a team namespace to the built-in
  `admin`, `edit` or `view`, subject `Group`. No `ClusterRoleBinding`.
- **Automation accounts**: 16 per cluster at most, a `ServiceAccount` in a
  team namespace (`automountServiceAccountToken: false`) and a
  `RoleBinding` `hops-sa-<name>` to `edit` or `view`. No token Secret is
  ever created.

With the base unticked, roles and accounts go into team namespaces that
must exist already (`team-namespace-missing` otherwise). The whole is
rendered as one item named `hops-policies` (package `harvester-ops`,
version `builtin`), delivered and removed like a manifest. Policy items of
a package (kind `policy`) are ordinary manifests, checked the same way.

### Tokens

**Issue a token** (Cluster API > Clusters, card of the child cluster, or
`harvester-capi token`) asks the child cluster for a `TokenRequest` of an
automation account: 600 to 86 400 seconds, 3 600 by default. The token is
shown once, with a Copy button, then removed from the page when the window
closes (a click beside the window does not close it once the token is
shown). It is never stored: not in an action, a step, a log, the event
stream nor `actions.db`; the answer is sent with `Cache-Control:
no-store`. The API of the child cluster accepts it for about one minute
after its expiry (clock skew allowance): "valid N s, plus up to one
minute". An account without the labels of the console is refused
(`account-not-managed`).

### Gestures outside a plan

- **Artefacts tab, Apply** on a chart, manifest or policy item: a window
  with Parameters (child cluster, registry for a chart, values), Checks and
  Summary. Apply stays greyed until the checks of the current values are
  back, and stays greyed once launched.
- **Cluster API > Services**: charts, manifests and policies in one table,
  column **Kind**, state of the binding for a manifest, **Remove**.
- **Cluster API > Clusters**: automation accounts of a child cluster and
  **Issue a token**.

### Roles

| Gesture | Role |
|---|---|
| Read applied items | viewer |
| Apply or remove an item, base of policies and accounts, issue a token | operator **and** the right to read the Secret `<c>-kubeconfig` of the child cluster |
| Undo a deployment, uninstall the Cluster API stack | admin |

Whoever can read `<c>-kubeconfig` is administrator of the child cluster:
the server checks this right with a `SelfSubjectAccessReview` made with the
identity of the person before anything is launched
(`child-admin-required`, 403).

### Command line

```
harvester-capi manifest-check  --spec F
harvester-capi manifest-deploy --spec F [--timeout 900]
harvester-capi manifest-remove --name NS/C/ITEM [--dry-run]
harvester-capi manifest-verify --name NS/C/ITEM
harvester-capi manifests [--json]
harvester-capi policy-render   --spec F
harvester-capi token --name NS/C --account NS/SA [--seconds N]
```

A spec: `{cluster: "ns/c", package, version, item}` or `{cluster,
policies: {base, teams, roles, accounts}}`. `token` writes the token on
standard output only. The working files of the tools (and the child
kubeconfig, 0600, erased at the end) go to `HARVESTER_OPS_WORK_DIR`, or
`<state>/work` (0700); never the bare `/tmp` (a sub-directory given by the
variable is accepted, owner and mode checked).

## Registries finished (2.8.0)

### Read password stored once (m-14)

Up to 2.7.x a child cluster kept its read account twice: in
`<c>-registries` (read by the nodes) and in `<c>-registry-<r>-auth`.

- Since 2.8.0 the password is stored once, in `<c>-registries`. The label
  `harvester-ops.io/registry-<r>: used` on that Secret is the mark of use.
- The old form is still read (which clusters use a registry, the mirror
  check of a service, the CA switch, the deletion).
- It is removed at the first registries renewal of the cluster, once the
  machines are replaced, or with the cluster.

### Kept CA key and certificate renewal

A bootstrap registry posed from 2.8.0 keeps its CA key in the Secret
`harvester-ops-registry/<r>-ca-key`. Its CA carries a critical
`nameConstraints`: the address of the registry, and its DNS name when
given. The Registries tab shows when the certificate expires.

**Renew the certificate** (administrators), or on the command line:

```
harvester-registry renew-cert NAME --cluster C [--days 730] [--dry-run]
```

- A new server certificate signed by the same CA, valid `--days` (1 to
  3 650, 730 by default).
- The TLS Secret is replaced and the pod restarted. The console waits for
  `/v2/` and says how long reads were cut (a few seconds).
- No file of the nodes changes: the child clusters keep trusting the
  same CA.

A registry posed before 2.8.0 has no kept key: `status` says
`registry-ca-key-missing` (information), and `renew-cert` refuses without
`--new-ca`.

### New CA for a registry posed before 2.8.0

`--new-ca` changes the CA in three steps, so that no child cluster ever
loses trust in its registry:

1. `renew-cert NAME --cluster C --new-ca` makes a new CA, kept in
   `<r>-ca-next`. From then on, what the console writes for this registry
   trusts both CAs (old and new).
2. Bring the new CA to every copy. Run `harvester-capi renew --name NS/C
   --what ca` on each child cluster that uses the registry. Deploy again
   the Flux entries that pull from it, and run `harvester-nodes` again if
   it is merged into the Harvester nodes.
3. Run `renew-cert NAME --cluster C` again. While a copy still lacks the
   new CA, it is refused (`registry-ca-transition-pending`) with the list
   of copies and their reason. Once the list is empty, the server
   certificate is signed by the new CA, which becomes `<r>-ca-key`.

The reasons in the list:

| Reason | Copy | What to do |
|---|---|---|
| `renewing` | a renewal of the child cluster is not finished | `renew --finish` |
| `nodes` | the nodes Secret named by the current templates | `renew --what ca` |
| `rke2-fetch` | `<c>-rke2-fetch` of a cluster without Internet | `renew --what ca` |
| `service-ca` | `<c>-registry-<r>-ca` read by the services | `renew --what ca` |
| `flux:<entry>` | a Flux entry whose source is in the registry | deploy the entry again |
| `harvester-nodes` | the `additional-ca` setting of the Harvester nodes | `harvester-nodes` again |

- Fleet entries live in Rancher and are not read by `harvester-registry`.
  The switch only emits the warning step `registry-ca-fleet-unchecked`:
  deploy the Fleet entries that pull from the registry again before.
- From the command line, run one `--new-ca` at a time. The console blocks
  a second one on the same registry.
- A transition in progress resumes without `--new-ca`. It never signs
  with the old key meanwhile. If the next CA was lost halfway,
  `registry-ca-signer-missing` refuses cleanly.

### Harvester nodes

**Harvester nodes** (administrators) lets the Harvester nodes themselves
pull from a registry:

```
harvester-registry harvester-nodes NAME --cluster C [--remove] [--dry-run] [--wait 60]
```

- The mirrors and the read account of the registry are merged into the
  Harvester setting `containerd-registry`. The other entries are kept. An
  origin already mirrored through another registry is refused
  (`harvester-nodes-mirror-conflict`).
- Its CA (and the next CA during a transition) is merged into the setting
  `additional-ca`.
- **Harvester restarts RKE2 on every node** to apply it
  (`harvester-rke2-restart`). The window asks to tick this restart.
- The write goes through a private file, never through the command line.
  Harvester then moves the read password into a Secret of type
  `rke.cattle.io/auth-config` in `fleet-local`, and removes it from the
  setting. The console checks this within `--wait` seconds (60 by
  default): `harvester-nodes-auth-inline` otherwise (exit 1).
- **Refused when another registry of the setting keeps a password in
  Harvester** (`harvester-nodes-other-auth`). When the mirrors change,
  Harvester 1.9 rewrites those Secrets from the setting with the host
  only: it would erase that password. Remove those credentials in
  Harvester first, then enter them again afterwards.
- The previous value is returned masked, in the result of the action.
  It cannot give the passwords back.
- `--remove` takes out the entry of this registry only.
- **Warning when the registry is hosted by this cluster** (2.8.1,
  `harvester-nodes-self-hosted`, not blocking): a bootstrap registry
  declared on this cluster, or an endpoint address equal to the address
  of a LoadBalancer Service of this cluster (kube-vip). The node that
  holds the address may not reach the registry through it: pulls on the
  Harvester nodes may fall back to the origin or fail (seen on harvlab2).
  The finding is in the JSON (`findings`), in the log of the action and in
  the window. A host name is not resolved, so it is not compared.

## Roles

| Gesture | Role |
|---|---|
| Read packages, registries and the trust settings | viewer |
| Import or remove a package; strict mode and trusted keys | admin |
| Declare, change or forget a client registry | admin |
| Deploy or delete a bootstrap registry | admin |
| Attach a client registry to a Harvester cluster, check a registry | operator |
| Publish a package | operator |
| Choose registries when creating a child cluster, deploy a service from a registry | operator |
| Renew the certificate of a bootstrap registry, merge a registry into the Harvester nodes (2.8.0) | admin |

Writes on the Harvester cluster are made with the identity of the person,
as in 2.5.0.

## Command line: `harvester-registry`

```
harvester-registry declare NAME --file r.yaml|- [--revision N]
harvester-registry attach  NAME --cluster C          (accounts as JSON on stdin)
harvester-registry check   NAME --cluster C [--vm-network NS/NAME]
harvester-registry list | show NAME | forget NAME [--kubeconfig-for C=FILE ...]
harvester-registry deploy  NAME --cluster C --address IP --size-gib N --storage-class S
                           [--dns-name N] [--mirrors h1,h2] [--cert F --key F --ca F]
                           [--timeout S] [--dry-run]
harvester-registry status  NAME --cluster C
harvester-registry leftovers   --cluster C
harvester-registry delete  NAME --cluster C [--delete-data]
harvester-registry write-access [NAME] --cluster C
harvester-registry settings [--write-sources A,B | --clear]
harvester-registry renew-cert NAME --cluster C [--days 730] [--new-ca] [--timeout S] [--dry-run]
harvester-registry harvester-nodes NAME --cluster C [--remove] [--dry-run] [--wait 60]
```

`attach` reads `{"push": {"username", "password"}, "pull": {...}}` on
standard input. `deploy --dry-run` runs the checks only. `check
--vm-network` adds the probe from that VM network. `settings` shows or
sets the networks of the console allowed on the write port; `write-access`
brings the bootstrap registries of a cluster (or the one named) to it. Exit codes: 0
success, 1 failure, 2 refused (a blocking finding, a registry in use...).

## Not in 2.8.0

- A Harbor registry posed by the console. A client Harbor works as a client
  registry; it was not tried for real.
- Argo CD as a GitOps tool: Flux and Fleet only.
- The Kubernetes upgrade of the child clusters.
- Restricting the port 5443 to the address of the console beyond 2.6.1.
- Garbage collection and resizing of a bootstrap registry.

**Seen for real (2.8.0).** On harvlab2, with a bootstrap registry laid by 2.7.1 and a child cluster
created by 2.7.1:

- A registries renewal by 2.8.0 (mirror without fallback added) replaced
  both machines in 15 min (API cut about 85 s, single control plane),
  removed the old `-auth` Secret and labelled the new nodes Secret; the new
  nodes pull through the registry, no fallback in `hosts.toml`.
- `renew-cert --new-ca`: refused with the copies listed (`nodes`,
  `service-ca`), `renew --what ca` on the child, then the switch (reads cut
  5.3 s); an image pulled after it. Then `renew-cert` with the kept key
  (reads cut 6.3 s, nodes untouched, image still pulled).
- `pack --rke2 v1.34.11+rke2r1` (57 images, 2.6 GB) and its publication.
- `harvester-nodes`: refused while another registry keeps a password in
  Harvester; with the guard bypassed on the bench, Harvester did erase
  that password, as the source says. Merge and removal otherwise as
  documented (setting and `additional-ca` back to their value, auth Secret
  pruned by Harvester).

Not verified for real: images pulled through the merged registry by the
Harvester nodes (the node that holds the registry address could not reach
it, containerd fell back to the origin), a Harbor or token-only registry,
the CA switch with an offline child and a Flux entry at once.

**Seen for real (2.6.0).** Bench harvlab2 (Harvester v1.9.0, one nested
node, 32 GiB), console of this release, 7 November 2026.

- **Package** `banc-260` 1.0.0: the podinfo chart 6.9.2, the image
  `ghcr.io/stefanprodan/podinfo:6.9.2`, the same stored as
  `docker.io/harvops-proof/podinfo:2.6.0` (a name that does not exist on
  the Internet), one manifest. `pack` took 6.5 s for 31.3 MiB; the layers
  shared by the two names are stored once.
- **Import.** One byte changed with its signature: refused
  (`invalid-signature`). The same without signature: accepted at the
  acknowledgement, then refused at extraction (`sums-mismatch`). Unsigned
  and unknown key: 409 until the acknowledgement, then imported, with the
  journal line and `trust.json`. Strict mode refused both. With the key
  trusted, the signed package was `valid` and imported in about 1 s.
  `harvester-artefacts import` answered 3 without the acknowledgement.
- **Registries.** Bootstrap registry of 8 GiB on a one-replica class,
  posed and ready in 37 s (anonymous 401, read account 200); publication
  in 3 s. Client registry simulated by Distribution 3.1 on another host,
  with its own CA and two accounts: attach 2 s, publication 2 s.
- **Child cluster** 1+1 created with both registries (client first) in
  about 10 minutes. Each node holds both registries with the read
  account only, and one CA file per registry.
- **No Internet.** A temporary nftables rule on the bench host dropped
  every packet from the child VMs to outside the LAN (Internet reachable
  before, timed out after). An image absent from the registries failed
  (`not found` once the fallback timed out, as `containerd.log` shows).
  The renamed image and the podinfo image were pulled from the
  registries in 3 s and less, the registry logs showing the `pull`
  account; pods using the renamed image ran on both nodes. The client
  registry served first; once it was stopped, the bootstrap registry
  served the same pull (`connection refused`, then the next host).
- **Chart by CAAPH** from the bootstrap registry (Services, "From a
  registry"), Internet still cut for the child: HelmReleaseProxy ready,
  release deployed in 15 s, pod running. CAAPH runs on the Harvester
  cluster; the chart was read from the registry (its log shows the
  Helm client).
- **Robustness.** Registry pod deleted: back in seconds, content intact.
  Deleted keeping its data, then deployed again over the volume in 26 s:
  every image was still there, with a new CA, as said above. Deleted with
  its data after the typed name (a wrong name was refused). Delete,
  forget and remove were refused while the cluster and the plan used
  them.
- **Found and fixed on the bench:** with two registries, CAPRKE2 v0.25.2
  kept one registry only on the nodes (the client registry had neither
  its CA nor its account); a root disk smaller than the image (the
  published openSUSE Leap 15.6 image now announces 10 GiB) left the VM in
  Scheduling; forgetting a client registry left its accounts on the
  Harvester cluster.
- **Bench setting.** The console room check refused two 10 GiB disks on
  the 48 GiB Longhorn disk of harvlab2. The Longhorn setting
  `storage-minimal-available-percentage` was lowered from 25 to 15 during
  each creation, then set back.

**Seen for real (2.6.1).** Bench harvlab2, console of this release,
7 November 2026. Bootstrap registry of 8 GiB at 172.16.2.76.

- **Probe from `default/lab-net`** (untagged, Pi-hole DHCP): lease
  obtained and given back (absent from the DHCP leases after), TLS
  handshake done, `registry-vm-network-ok` in 11 to 14 s; the creation
  check of a child cluster on this network gave
  `cluster-registry-vm-network-ok`. No Job nor pod left in
  `harvester-ops-probe` after any check.
- **Networks that cannot reach the registry**, made for the test on
  VLAN 998 and 999: without DHCP nor IPAM, `registry-vm-network-unchecked`
  with the reason `no-address` (28 s, the DHCP wait); a static IPAM
  without gateway, the same with `ipam-incomplete` (7 s); a static IPAM
  with a route through a gateway that does not exist,
  `registry-unreachable-from-vm-network` (`tcp`), and in the creation
  check of a child cluster that cuts the fallback,
  `cluster-registry-unreachable-from-vm-network` blocking.
- **Write port.** A registry shaped as in 2.6.0 (5443 on the main
  Service, the port added by another field manager) was migrated by
  saving `172.16.1.11/32`; a new registry was then posed with the setting.
  Both times: two Services at the same address, 443 answering from node1
  and from node2, 5443 answering from node1 (the console) and timing out
  from node2. Check probed both accounts through it without finding.
- The TLS step of the probe was also run against a TLS server behind a
  reverse proxy on a local network: answer with the name in SNI, refused
  without it.

**Seen for real (2.7.0).** Bench harvlab2 (Harvester v1.9.0, Turtles
with Cluster API v1.13.3, CAAPH v0.6.4, CAPHV v0.10.1, RKE2 provider
v0.25.2), console of this release, 7 November 2026. Package `banc-270`
1.0.0 (podinfo chart and image, a manifest of a ConfigMap and a Deployment,
a policy item with one NetworkPolicy, a manifest `extra`) and 1.0.1 (the
manifest without the Deployment), signed by a key unknown to the console.

- **Plan** `banc270` on harvlab2: Terraform (a VM network and an SSH key),
  bootstrap registry at 172.16.2.76 with the publication, the Cluster API
  stack (already there, checked), one 1+1 child cluster, then the
  Artefacts phase (base of two team namespaces, one with a quota of 4 pods,
  a `view` role for a group, an automation account `ci`, the manifest, the
  policy, the podinfo chart). Terraform 8 s, registry 45 s, publication
  2 s, cluster 623 s; stopped at the start of the Artefacts phase, resumed
  and stopped after the base, resumed again: every earlier item `already
  done (checked)`, the last two applied in 1 min 17 s. In the child: both
  team namespaces with their labels, `hops-quota`, `hops-limits`, the four
  NetworkPolicies, the account and its `RoleBinding`, the objects of the
  manifest carrying the three labels, the podinfo release running; on
  harvlab2 the three `ClusterResourceSet` applied, their binding at the
  checksum, the `HelmChartProxy` ready, each carrying the run that created
  it (three different runs of the same chain).
- **Policies.** A fourth pod in the team namespace was refused
  (`exceeded quota: hops-quota`); a container without resources got
  100m/128Mi requested and 500m/512Mi limits; from the deny by default
  namespace, `wget` to another namespace timed out while the DNS answered,
  the same from the namespace without deny passed; an Ingress of the team
  namespace answered from node1 on both nodes through the RKE2 controller.
- **Token** of 600 s for `ci`: lists the service accounts of its
  namespace, `Forbidden` in `kube-system`, `Cache-Control: no-store`, and
  its value found nowhere in `actions.db`, the logs or the state (search of
  the value). A token of 600 s issued on harv4 expired at 18:27:11: still accepted 35 s later, `Unauthorized` 81 s later.
- **Update and removal.** The plan moved to 1.0.1: the Deployment left by
  the content was deleted from the child (run of 1 min 28 s). The labels of
  the ConfigMap removed by hand, then the item removed (1.7 s): the
  ConfigMap kept and named `kept-relabelled`, the `ClusterResourceSet` and
  its ConfigMap gone.
- **Undo, and what it must not touch.** Before the undo, three decoys
  without the run mark: the same item `app` applied again outside the plan
  (same `ClusterResourceSet` name `art-app`), an item `extra` applied
  outside the plan, and a ConfigMap `art-app` labelled like the console in
  `default` of harvlab2. The destruction plan (15 s) named `app` as left
  untouched. First undo (20 s): chart, policy and base removed (team
  namespaces kept: `kept-in-use`, they held hand-made pods); `app`
  `undo-not-ours`, so the cluster and the Terraform content stayed
  (skipped). All three decoys survived, with their objects in the child.
  The two decoy items removed outside the plan, the undo relaunched: what
  was gone checked and skipped, cluster deleted (265 s), declaration
  destroyed and removed (2 s); the run showed Partially undone (registry
  left). The registry undo was refused on the bench (`registry-in-use`: the
  plan being undone names its own registry), fixed (`--ignore-plan`), then
  removed in 2 s with its volume kept; the run turned Undone and a new undo
  was refused (`undo-already-done`). The `default/art-app` decoy was still
  there at the end. Harvester and the Cluster API stack untouched.
- **Interrupted undo.** Another run (one cluster, one manifest, 608 s):
  the deletion of the cluster cancelled from the dock 30 s in, the undo
  ended partial with the cluster in Deleting; relaunched, it checked the
  manifest and finished the deletion in 2 min 11 s, Undone.
- **Turtles uninstall.** Refused while the child cluster and the chart
  existed (`capi-clusters-present`, `caaph-services-present`), in the
  tracked action, nothing touched. After the undo: dry run, then removal in
  26 s of the four `CAPIProvider` of the console, their deployments and
  bundle ConfigMaps; `core/cluster-api` and Turtles running, CRDs and
  namespaces kept, the `ingress-expose` Service (not laid by the console on
  this bench) kept. **Install** laid the stack again in 52 s, ready at the
  same versions.
- **Leftover HarvesterCluster.** With a dead owner and a finalizer:
  `check` blocking (`harvester-cluster-leftover`, finalizer named),
  `delete` waited 120 s and named the finalizer, which the console left.
  Without owner: a warning, then removed by `delete` in 6 s.
- **Two child clusters on harv4** (bare metal, Harvester v1.9.0): a plan
  with a bootstrap registry, two 1+1 clusters, the manifest `extra` and
  the podinfo chart on both: registry 41 s, publication 3 s, clusters
  477 s and 499 s, manifests 7 s, charts 14 s, 19 min 20 s in all. The
  harv4 registry used an address borrowed from another bench (that bench
  was down); harv4 has its own reserved address since.
  Then one undo of the whole run, registry and its data included (two typed names): both charts and manifests removed in 1 to 2 s each, clusters deleted in 169 s and 177 s, registry and volume removed in 2 s, 6 min 15 s in all, run Undone; a decoy ConfigMap `art-extra` labelled like the console in `default` survived. Bench objects removed, the cluster shut down gracefully and node4 powered off.
- **Found and fixed on the bench:** the registry step of an undo always
  refused by the plan being undone; an undo relaunched at once refused by
  the lock it was releasing; a failed undo after a partial one turned the
  run from partial to error; the undo window named the last resume as the
  creator.
- **Bench setting.** Longhorn `storage-minimal-available-percentage` 25 ->
  15 during the creations, set back; bench image, key pair and IP pools
  created for the essay and deleted.

Not verified for real (2.7.0): more than two child clusters, a Rancher
outside the bench importing child clusters, a restricted Rancher person
issuing a token (tried by impersonation only), `--remove-images` of the
uninstall.

Not verified for real (2.6.0): a client Harbor, a child cluster bootstrapped
entirely without the Internet (out of 2.6.0), a bootstrap registry on a
cluster of several nodes, more than one child cluster on a registry at a
time, packages of several gigabytes, and the resume of a publication
stopped halfway (tests only: the bench publication took 3 s).
