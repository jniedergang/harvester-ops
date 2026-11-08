# GitOps of child clusters (2.8.0)

A child cluster made by Cluster API can follow a source of manifests on
its own. The console poses the GitOps tool and its source, shows its
state, and removes it. It never rewrites what the tool knows how to do.

- **Cluster API > GitOps** (new tab, between Services and Registries)
  lists the entries posed on the child clusters of the Harvester cluster
  shown in the header.
- A deployment plan carries the same entries in its last phase, **GitOps**
  (see [bare-metal.md](bare-metal.md)).
- Each gesture runs `harvester-capi gitops-*` as a tracked action.

## Flux or Fleet

An entry names its tool: `auto`, `flux` or `fleet`.

- `auto` (the default) picks **Fleet** when the child cluster is imported
  into the Rancher of your session (Access window, see
  [operating-procedure.md](operating-procedure.md)), and **Flux**
  otherwise.
- `fleet` on a cluster that is not imported is refused
  (`gitops-fleet-needs-rancher`).
- No tool is installed by default. Flux comes from a package (below).
  Fleet comes with the import into Rancher.

| Source | Flux | Fleet |
|---|---|---|
| `oci`: the Flux artefact of a `gitops` item of a published package | yes | no |
| `git`: a Git repository, `https://` or `ssh://` | yes | yes |
| `chart`: a chart item of a published package | no | yes (`HelmOp`) |

Any other pair is refused (`gitops-source-unsupported`).

## Flux on a child cluster

Flux is the community chart `flux2` (Apache-2.0). It comes in a package
like any chart, and is applied by CAAPH as a service named **`flux`**:
apply the chart item `flux` of the package from the Artefacts tab, or from
the Artefacts phase of a plan. An entry checks that this service is
ready (`gitops-flux-missing` otherwise).

**Lockdown values.** The console imposes these values on the service named
`flux`, over those of the package. A value of the package cannot remove
them.

```yaml
multitenancy:
  enabled: true
  defaultServiceAccount: hops-gitops
  privileged: false
imageAutomationController: {create: false}
imageReflectionController: {create: false}
sourceWatcher: {create: false}
```

- Flux then impersonates the account named by each `Kustomization`, and
  refuses references across namespaces.
- Image automation is not installed.
- A `flux` service posed without these values is refused
  (`gitops-flux-not-locked`): apply it again from the console.

**Chart version.** The console does not pin the version of the chart. The
package carries the right one.

- Chart `flux2` 2.20.0 (Flux 2.10) refuses Kubernetes older than 1.35. Its
  pre-install check job fails, and no value skips it.
- On a 1.34 child cluster, use chart `flux2` 2.19.1 (Flux 2.9.5). It
  installed in 30 s on the bench.

## What a Flux entry writes

The objects of an entry are delivered by a `ClusterResourceSet`
`art-gitops-<entry>`, as a manifest is (see [artefacts.md](artefacts.md)).
In the child cluster:

- the namespace **`hops-gitops`**, created by the console, shared by every
  entry;
- in `hops-gitops`: an `OCIRepository` or `GitRepository` `<entry>`, a
  `Kustomization` `<entry>`, and the account `hops-gitops-<entry>`
  (`automountServiceAccountToken: false`);
- the `Kustomization` aims at the **team namespace** of the entry
  (`targetNamespace`) and runs with the account `hops-gitops-<entry>`;
- `privileges: namespace` (the default): a `RoleBinding` of this account to
  `edit` in the team namespace;
- `privileges: cluster` (administrators only): a `ClusterRoleBinding` of
  this account to `cluster-admin`, rendered by the console;
- the read account of the registry, its CA and the Git credentials go in
  Secrets of `hops-gitops`. They travel in the Secret of the
  `ClusterResourceSet`, never in its ConfigMap.

The team namespace must exist in the child cluster
(`gitops-namespace-missing`). A system namespace, `default` or `hops-gitops` itself is refused
(`gitops-namespace-not-team`).

`prune: true` (the default) lets Flux remove what leaves the source.
`interval_minutes`: 1 to 1 440, 10 by default. The entry name: RFC 1123,
34 characters at most (`gitops-name-too-long`).

**Ready** when the source, then the `Kustomization`, are `Ready`. For an
`oci` source, the applied revision must be the version of the package.

**Lockdown at work.** The account of a `namespace` entry can write only in
its team namespace. A `ClusterRole` in such an entry is refused by the
impersonated account, and the `Kustomization` fails. That is expected.

## What a Fleet entry writes

Fleet lives in the `local` cluster of Rancher. The console writes there
with the token of your session, in `fleet-default`:

- a `GitRepo` (source `git`) or a `HelmOp` (source `chart`), named
  `hops-<harvester>-<c>-<entry>` (`hops-<harvester>-<ns>-<c>-<entry>` when
  the namespace differs from the name; past 63 characters, 54 characters,
  `-` and 8 hexadecimal digits);
- its target: the Fleet cluster of the imported child cluster;
- a Secret `<name>-auth` (Git credentials) or `<name>-pull` (read account
  of the registry, and its CA as `cacerts`).

The ownership of the object and of both Secrets is checked before any
write (`gitops-name-taken`). An entry posed by another plan, or by hand,
is never taken over by a plan (`gitops-taken`). **Ready** when
`status.summary.ready` equals `desiredReady`.

**Fleet is reserved to administrators**, to pose and to remove an entry
(`gitops-fleet-admin-only`). The reason: a `GitRepo` or a `HelmOp` without
`serviceAccount` deploys with the Fleet agent, which is `cluster-admin` of
the child cluster. `privileges: namespace` is not enforced there, so an
administrator gets the warning `gitops-fleet-not-confined`.

## Gitops items of a package

A package item of kind `gitops` holds YAML files
(`manifests/<item>/<file>.yaml`). It is published as a Flux artefact
(see [artefacts.md](artefacts.md)):

- repository `<registry>/harvester-ops/gitops/<package>/<item>`, tag = the
  version of the package;
- one layer `application/vnd.cncf.flux.content.v1.tar+gzip`, a
  deterministic tar.gz: the same files give the same digest.

The content is judged by the rules of the manifests
(`capi_manifests.check`) at publication: no CRD, no webhook, no system
namespace. A GitOps tool is not a back door. A refused item stops the
publication (`publish-gitops-refused`, with the reason).

**Each namespaced object must carry `metadata.namespace`.** This rule of
`capi_manifests.check` was found on the bench: an item without it is
refused at publication. Flux still places the objects in the team
namespace of the entry (`targetNamespace`).

A package published before 2.8.0 has no Flux artefact: an `oci` source
gets `gitops-package-not-published`. Publish the package again.

## Git credentials

A `git` source may name a secret (`source.secret`): an SSH key
`{identity, known_hosts}` or an account `{username, password}`.

- They are entered at launch, in the window or in the Deploy window of a
  plan. On the command line they arrive in the private request file.
- **They are never stored.** They go to the tool in a private request
  file, then to the child cluster (Flux) or to Rancher (Fleet). They never
  enter an action, a step, a log, `actions.db` nor the state of the plan.
- A plan keeps them in memory for its run only. A resumed run asks for
  them again.
- An `https://` URL with credentials in it is refused.

## Gestures

**Cluster API > GitOps** lists the entries: cluster, entry, tool, state,
origin (the run of a plan), **Remove**.

**Add an entry** opens a window with three tabs:

- **Parameters**: child cluster, entry name, tool (the tool deduced is
  shown), team namespace, source, privileges, interval, prune, Git
  credentials;
- **Checks**: `gitops-check` runs again at each change (read only);
- **Summary**.

**Apply** stays greyed, with the reason, until the checks of the values
shown come back without a blocking finding. The Git credentials are sent
once, then cleared from the form.

A `gitops` item of the Artefacts tab has a **GitOps** button that opens
this window with its source filled.

## Removal and Undo

**Remove** on an entry:

- **Flux**: the `Kustomization` first, waited until it is gone (its
  finalizer prunes what it applied, with the account still there; 300 s at
  most). Then the source, then the account, its bindings and its Secrets,
  then the `ClusterResourceSet` and its ConfigMap. Past the wait, the
  rest is kept and the removal says so: run it again.
- **Fleet** (administrators): the `GitRepo` or `HelmOp`, read again just
  before the deletion, then its Secrets.

**Undo** of a plan removes the GitOps entries the run created first (kind
`gitops`), before the artefacts, the import into Rancher, the clusters,
the registry and the Terraform content. The Flux chart is removed only
after the entries of its cluster. Without a Rancher session, a Fleet entry
is never judged "already gone": the undo item fails with
`rancher-session-needed`.

## Roles

| Gesture | Role |
|---|---|
| Read the entries | viewer |
| Pose or remove a Flux entry | operator **and** the right to administer the child cluster (`child-admin-required`) |
| Pose or remove a Fleet entry | admin, with a Rancher session |
| `privileges: cluster` | admin |

## Command line

```
harvester-capi gitops-check  --cluster C --spec F
harvester-capi gitops-deploy --cluster C --spec F [--timeout 900]
harvester-capi gitops-remove --cluster C --name NS/C/ENTRY [--role admin|operator|viewer]
                             [--expect-run ID] [--dry-run]
harvester-capi gitops-status --cluster C [--name NS/C] [--json]
```

A spec: `{cluster: "ns/c", entry: {...}, secrets: {name: {...}}, role,
origin, by}`. The entry takes the keys `name`, `tool`, `namespace` (the
team namespace; `target_namespace` in a plan), `source`, `privileges`,
`interval_minutes`, `prune`. `gitops-remove --role` defaults to `admin`
on the command line; the console passes the role of the person. Fleet
gestures need `--harvester-name` and a kubeconfig of a Rancher session.

## Known limits

- The tag of the Flux artefact is not read in the registry at check time
  (no HEAD). An entry whose artefact is missing times out with a clear
  message.
- The row ids of a plan can collide: `a-b/c` and `a/b-c` give the same
  id. To be fixed in 2.9.
- Fleet copies of a registry CA are not checked before a CA switch: the
  switch emits the warning step `registry-ca-fleet-unchecked` (see
  [artefacts.md](artefacts.md)). Deploy the Fleet entries again before.
- Fleet with a confined `serviceAccount` is not offered.

## Seen for real (2.8.0)

On harvlab2 (Harvester 1.9.0, child cluster 1+1, Kubernetes 1.34.11),
with the external Rancher 2.14.1 (Fleet 0.15.1) and a temporary Gitea
repository:

- Flux 2.9.5 (chart `flux2` 2.19.1) from the bootstrap registry, lockdown
  values on the release; an `oci` entry and a `git` entry over SSH (secret
  of the plan) ready in `equipe-a`. Moving the plan to the package 1.0.1
  pruned the object it no longer carries. A `ClusterRole` in a namespace
  entry is refused by the impersonated account, as expected.
- Fleet: a `GitRepo` from Gitea and a `HelmOp` of the podinfo chart
  published in the registry, `auto` choosing Fleet on the imported child;
  a second plan with the same entry name refused (`gitops-taken`).
- Resume: Git secret asked again, done entries checked and skipped. Undo
  without a Rancher session leaves the Fleet entries
  (`rancher-session-needed`); with it, GitOps then artefacts are removed in
  that order, and the deployed objects go with them.
- Four defects found there are fixed in 2.8.0: the `HelmOp` version, the
  Fleet bundles left after a removal, the plan check of an entry on an
  existing child, the namespace of the `flux` release.

Not verified for real: Fleet with a client Git server over HTTPS
(basic authentication), more than one child cluster at once, a Flux
entry posed during a CA transition.
