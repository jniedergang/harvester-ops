# Artefact packages and registries (2.6.0)

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

The full Artefacts phase of the deployment plans (charts and manifests
applied, clean removal) comes in 2.7. The Registry phase of the plans is
described in [bare-metal.md](bare-metal.md), section "Deployment plans:
Registry phase (2.6.0)".

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

Item kinds: `chart`, `images`, `manifest`. Two more kinds, `policy` and
`gitops`, are kept in the package and shown "coming (2.7)"; they are never
applied nor published.

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
registry from the console, with each account, without writing. Findings:
`registry-unknown`, `registry-not-attached`, `registry-unreachable`,
`registry-ca-invalid`, `registry-auth-refused`, `registry-no-usage`
(blocking); `registry-pull-is-push`, `registry-insecure`,
`registry-vm-network-unchecked` (warnings). `registry-insecure` comes from
`insecure_skip_verify: true` in a declaration file: the nodes then do not
check the certificate.

**Registry reachability is checked from the Harvester cluster only, not
from the VM network.** The console and the Harvester cluster probe the
address; no check runs from the network of the child VMs. Every check says
so with a warning: `registry-vm-network-unchecked` (Check, bootstrap
registry window and plan checks) and `cluster-registry-vm-network-unchecked`
(creation check of a child cluster, one per registry that serves images).
A VM network that cannot route to the registry (VLAN without a route,
firewall) is not detected: the nodes then fall back to the Internet, or
fail to pull when `no_fallback` is set. Make sure the child VMs reach the
address before you rely on it. A probe pod attached to the VM network
comes in 2.6.1.

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

The child clusters read on 443. The console publishes on 5443. Port 5443
is open to every machine that reaches the address, the VM networks
included: a write still needs the `push` account, and the `pull` account
is refused there.

- **TLS always.** The console generates a CA (10 years) and a server
  certificate (2 years), or takes your certificate (`--cert`, `--key`,
  `--ca`). **The generated CA key is erased after the deployment: the
  certificate cannot be renewed.** Deploying again gives a new CA, and the
  child clusters keep the old one on their nodes (registries are set at
  creation only, see below). Plan for it: with the generated CA, the
  registry has two years.
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
- Images first, then charts; manifests and the "coming" kinds are skipped.
  The progress shows `images 3/14` and the MiB sent.
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

- **Set at creation only.** RKE2 reads its registry configuration when it
  starts. Changing the registries of an existing cluster is not offered in
  2.6.0.
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
  per registry, read by the nodes at their first boot),
  `<cluster>-registry-<registry>-auth` (the read account) and
  `<cluster>-registry-<registry>-ca` (`Opaque`, `ca.crt` only), the last
  two labelled with the registry name (they tell which clusters use a
  registry). All are labelled to be removed with the cluster. **Only the
  read account reaches the nodes**; the write account never does.

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

Writes on the Harvester cluster are made with the identity of the person,
as in 2.5.0.

## Command line: `harvester-registry`

```
harvester-registry declare NAME --file r.yaml|- [--revision N]
harvester-registry attach  NAME --cluster C          (accounts as JSON on stdin)
harvester-registry check   NAME --cluster C
harvester-registry list | show NAME | forget NAME [--kubeconfig-for C=FILE ...]
harvester-registry deploy  NAME --cluster C --address IP --size-gib N --storage-class S
                           [--dns-name N] [--mirrors h1,h2] [--cert F --key F --ca F]
                           [--timeout S] [--dry-run]
harvester-registry status  NAME --cluster C
harvester-registry leftovers   --cluster C
harvester-registry delete  NAME --cluster C [--delete-data]
```

`attach` reads `{"push": {"username", "password"}, "pull": {...}}` on
standard input. `deploy --dry-run` runs the checks only. Exit codes: 0
success, 1 failure, 2 refused (a blocking finding, a registry in use...).

## Not in 2.6.0

- The **Artefacts phase** of the plans (charts, manifests and policies
  applied, clean removal): 2.7.
- A child cluster bootstrapped **entirely without Internet** (RKE2 itself
  and its system images from the registry, a prepared VM image): later.
- **Changing the registries of an existing cluster**: later, by renewing
  the machines.
- A Harbor registry posed by the console. A client Harbor works as a client
  registry; it was not tried for real.
- Pointing the `containerd-registry` setting of the Harvester nodes at the
  declared registries: the setting stays a manual gesture (Settings).
- Garbage collection and resizing of a bootstrap registry.
- Probing the registry from the VM network: the checks warn that
  reachability is checked from the Harvester cluster only (see Check
  above). The probe pod comes in 2.6.1; its image (`bci-busybox`) is
  already in the registry bundle of 2.6.0, imported on the nodes with the
  registry image, and used by nothing else yet.
- Restricting the write port 5443 to the address of the console. Tried
  on the bench: a second LoadBalancer Service on the same address, with
  the write port only and `loadBalancerSourceRanges` set to the console
  address, shares the address through the kube-vip of Harvester, and the
  range is enforced (the console got 401 and then 200 with `push`, another
  host of the LAN timed out, port 443 stayed open to all). Not delivered:
  it needs the source address of the console as the cluster sees it,
  which is a setting to design (NAT, container network).

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

Not verified for real: a client Harbor, a child cluster bootstrapped
entirely without the Internet (out of 2.6.0), a bootstrap registry on a
cluster of several nodes, more than one child cluster on a registry at a
time, packages of several gigabytes, and the resume of a publication
stopped halfway (tests only: the bench publication took 3 s).
