# Secret store (OpenBao, 2.9.0 and 2.10.0)

The console keeps its secrets (cluster kubeconfigs, SSH keys, the Rancher
OIDC client secret, replication notification and hook secrets, Terraform
variables of KVM declarations, the secrets of a running deployment plan) in
local files of its state directory by default. Since 2.9.0 it can keep them
in OpenBao instead, class by class, and come back to local files at any
time. Since 2.10.0 a person signed in through Rancher reads and writes the
personal classes with their own OpenBao token
([Per-person tokens](#per-person-tokens-2100)), and the child clusters
created by the console can sync values kept in OpenBao into their Secrets
with External Secrets Operator
([External Secrets Operator](#external-secrets-operator-in-child-clusters-2100)).

French version: [../fr/secrets.md](../fr/secrets.md).

## Two modes

| Mode | Where OpenBao runs | Who owns it | Use it when |
|---|---|---|---|
| **A** | your own OpenBao (or a compatible Vault) | you: policies, backups, unseal | you already run a secret server |
| **B1** | a container next to the console, on the same host, `127.0.0.1:8210` | the console host agent | you want the secrets out of the state directory without running a server |

Mode B1 uses the OpenBao image of the SUSE Application Collection
(`dp.apps.rancher.io/containers/openbao:2.7.2-5.4`, Rancher Prime
subscription). harvops does not redistribute it: you build a separate
**OpenBao bundle** with your own Application Collection service account
(see [Building the B1 bundle](#building-the-b1-bundle)).

Nothing changes while the store stays in local mode: 2.9.0 without OpenBao
behaves like 2.8.3, except that `install.sh` now installs the secret store
agent units (a path unit and an hourly timer that only refresh a health
file).

## What can move to OpenBao

| Class | Local place | Moved |
|---|---|---|
| `kubeconfig` | `clusters.d/<n>.yaml`, `kubeconfigs/<n>.yaml` | yes, offered only after another class moved and a simulated outage passed |
| `ssh-cluster` | `ssh/<n>_id` (private key) | yes (`.pub` and `known_hosts` stay local) |
| `ssh-kvm` | `ssh/<host>_id` | yes, with rotation |
| `rancher-oidc` | `rancher.d/<id>.oidc-secret` | yes |
| `replication-notify` | replication database, notification settings | yes, field by field |
| `replication-hook` | secret headers of replication hooks | yes |
| `tf-kvm` | `<workspace>/.secrets/<variable>` | yes |
| `plan-run` | memory (2.4.0) | kept in OpenBao while a plan runs, erased at the end (at most 7 days for a stopped run) |

Always local: console accounts, `setup-token`, `htpasswd`, the TLS key of
the console, every public key, the OpenBao credentials themselves, and the
kubeconfigs written in `config.yaml` (they belong to the installer).

Inside OpenBao a secret lives at
`<mount>/data/<prefix>/<class>/<path>`, mount `harvester-ops` and prefix
`consoles/<console id>` by default. Each write carries `custom_metadata`
(`hops_action`, `hops_actor`, `hops_class`), never a hash of the value.

## Enabling mode A

Settings > Secret store > Enable opens the assistant.

1. **Server**: address (`https://`, `http` only on the loopback), the CA if
   it is private, the console address as seen by OpenBao (CIDR), mount and
   prefix. The assistant shows the `bao` commands to run on your server and
   the exact policy text. Nothing is created by the console outside its
   prefix.
2. **Checks**: address and certificate, seal status, and the wrapped
   `secret_id` (its creation path must be the console role). Before the
   first activation the login is shown as "after": `enable` deposits the
   AppRole credentials through the host agent, then checks login, KV v2,
   capabilities of each path, a test write under `<prefix>/check/`, transit
   and the OpenTofu token role, and activates only when all are green.
   Credentials left on the host by an earlier server are never used by the
   check before activation.
3. **Confirm**: activate.

The `secret_id` is always handed over **wrapped**. The agent looks the
wrapping token up, checks its creation path, unwraps it and writes
`/etc/harvester-ops/openbao/secret-id` (`root:harvester-ops 0640`). An
already unwrapped or expired token gives `openbao-unwrap-used`: someone else
may have read the secret.

Vault: the API is the same (KV v2, AppRole, wrapping, transit). Vault 1.14.2
was checked in mode A: a subset of the checks, activation, migration and
return. OpenTofu documents its `openbao` key provider with
Vault 1.14 only: with a newer Vault the Status tab asks you to acknowledge it
before states get encrypted (Vault 1.17.6 encrypted states without error in
our tests).

## Enabling mode B1

### Building the B1 bundle

On a host that reaches the Application Collection (or your mirror):

```bash
harvester-secrets bundle --registry dp.apps.rancher.io \
    --user <service account> --password-stdin -o ./bundles/openbao/ < password-file
```

The tool pulls the image itself (standard library, no `podman login`),
checks its cosign signature with the Application Collection public key, pins
the index digest and writes `harvester-ops-openbao-<version>-<tag>.tar.gz`
(about 59 MB, image 202 MB once loaded) with its `.sha256`, provenance and
the signature as served by the registry. A tag other than the pinned
`2.7.2-5.4` is only accepted with a valid embedded signature. OpenBao older
than 2.7.2 is refused.

Put the bundle under `bundles/openbao/` of the harvops release before
`install.sh`, or keep it on the host.

### Posing B1

- From the console: Settings > Secret store > Enable, mode B1, choose the
  bundle that `install.sh` placed.
- On the host: `sudo /usr/local/bin/harvester-secrets enable --mode b1
  --bundle <file>` (on openSUSE, `sudo`'s `secure_path` does not include
  `/usr/local/bin`).

The agent creates the account `harvester-ops-openbao`, loads the image,
writes the unit `harvester-ops-openbao.service` (`--read-only`,
`--network host`, `--memory 512m`, all capabilities dropped), a CA and a
server certificate, a static unseal key
(`/etc/harvester-ops-openbao/unseal.key`, root 0400, handed to the container
as a podman secret) and starts OpenBao with a declarative initialization: no
root token is ever returned, no recovery key exists.

### Offline copy of the unseal key

After the install and after each unseal key rotation, the key in service
waits for its offline copy. Moving a class to OpenBao is refused
(`offline-copy-ack-required`) until it is acknowledged:

1. Export (type the console identifier): the agent hands one copy.
2. Download it once (`no-store`; a second download answers 410).
3. Acknowledge with the key identifier. A wrong key is refused.

Keep the file away from the snapshots: a snapshot without its key cannot be
restored, and the key without the snapshot is useless.

## Moving classes and back

Settings > Secret store > Classes, or `harvester-secrets migrate --to
openbao|local --class <c>`. Each secret is written with `cas=0`, read back,
compared by SHA-256 in memory, then the local value is replaced by a
reference (`openbao:<class>/<path>#<field>`). The original is kept as
`<file>.migrated` (0600) for 7 days (1 to 30, `originals_keep_days`), or
until **Confirm**. Back to local writes the value where it was and removes
the OpenBao copy.

`kubeconfig` is offered last: another class must have moved and **Test an
outage** must have passed.

## Cache and outages

Values read from OpenBao are kept in memory for 15 minutes (1 to 120). Never
on disk: working copies handed to `kubectl`, `ssh` or `tofu` are 0600 files
on tmpfs (`/tmp/hops-secrets` in the container), erased after the action.

| What happens | What you see |
|---|---|
| OpenBao stopped, value in cache | the action runs; banner "OpenBao does not answer, since HH:MM; served from memory until HH:MM" |
| OpenBao stopped, cache expired or emptied | `503 openbao-unreachable: <class>/<name> (<path>: ...)` |
| sealed | `openbao-sealed` |
| token revoked on the server | the console logs in again by AppRole, the action runs |
| policy narrowed | `openbao-denied ... (capabilities: deny)` for the classes outside it |
| audit log full | `openbao-internal` after two 500s, banner down; `sys/health` still says unsealed |

There is no local fallback copy. Connections to a cluster already open by
the console keep working until they are closed, even when the kubeconfig
cannot be read again. **Empty the cache** applies to the console process.

## Rotation

| Secret | How |
|---|---|
| console `secret_id` | every 30 days by the host agent (hourly timer), or **Rotate now** |
| B1 unseal key | on request (type the identifier): new key, restart with both, `sys/rotate/root`, old key retired and kept 14 days for the snapshots that need it |
| SSH key of a KVM host | on request; age shown, flagged after 365 days; new key added, tested, stored, old key removed and checked refused; a failure in the middle puts the previous state back |
| transit key of a declaration | on request; older states stay readable |
| secrets the console does not own (OIDC, Matrix token, kubeconfig) | age shown, replace them where they are set |

After a KVM key rotation, a version rollback or a restore launched from the
console, its cache is emptied.

Versions (Settings > Secret store > Versions) lists who wrote which version
(actor, action), never a value, and can bring a secret back to an older
version. Bringing an SSH key of a KVM host back to a version older than its
last rotation gives a key the host already refuses.

## Snapshots (B1)

- Directory: `/var/lib/harvester-ops-snapshots` on the host, mounted in the
  console container (created when missing). You may mount a share there
  (NFS under SELinux: `context="system_u:object_r:container_file_t:s0"`).
  A root-squashed share works: the agent reads it as the console account.
  The state directory is refused (it is backed up with the console).
- Schedule: every day at `snapshot_time` (02:30 by default, **UTC**, the
  container clock), 14 kept. A failed snapshot is retried after an hour and
  shown in Status; it is never relaunched at each pass.
- Files `openbao-<UTC stamp>-<key id>.snap` (0600). A snapshot is
  encrypted by the barrier: values and policy texts are not readable, the
  names of storage keys are.
- `sudo harvester-secrets snapshot` is refused (it would act on the console
  state as root): `sudo -u harvester-ops harvester-secrets snapshot --dest
  DIR`.

### Restoring

Settings > Secret store > Snapshots > Restore (type the identifier). Before
anything stops, the console lists what the restore would break and you
acknowledge it:

- running actions (they stop with the console);
- encrypted declarations whose state was written after the snapshot: the
  transit key is created at the first encryption (often long after the
  declaration) and a rotation adds a version, so such a state may need a key
  the snapshot does not hold and become unreadable (a state rewritten with a
  key that already existed is listed too, harmlessly);
- KVM hosts whose SSH key was rotated since (OpenBao would give back a key
  the host refuses);
- classes moved to OpenBao after the snapshot (their secrets are not in it).

The agent then stops the console and OpenBao, keeps the current volume
aside for 7 days (`/var/lib/harvester-ops-openbao.old-<UTC stamp>`), starts a
fresh instance with the key of the snapshot, restores it, and reviews the
restored configuration against an **allowlist** of what this console
expects: exact policy names and texts (the `default` policy compared with
the hash recorded at install or at the first policy refresh; if that read
failed, the agent log says so and `default` is not compared until the next
refresh), auth methods `token` and `approle` only, the
two AppRole roles with their policies, address ranges and single
`secret_id`, the OpenTofu token role, no identity group, identity entities
without policies whose aliases are the two AppRole roles or `tf-<declaration>`
on `token/`, no namespace, the expected mounts and audit device. Any
difference refuses the snapshot and puts the
previous volume back. Then every token of the snapshot is revoked; the
console and the agent log in again. The agent and the OpenTofu token
policies are rewritten before the review, so a snapshot that only widens
them is repaired, not refused.

An entity alias `tf-<declaration>` is accepted for any well-formed name, not
only for live declarations: a snapshot taken before a declaration was
removed still holds its entity, and the token role accepts `tf-*` because
the console creates the alias of a new declaration on its first action.
Such an entity carries no policy, so it gives nothing beyond the OpenTofu
token policy.

A restore launched from the console stops the console, and with it the
tool that would put the settings in line with the snapshot: the restarted
console runs `harvester-secrets restore-settle` once for that restore. If
another agent operation runs before the console comes back (for example
`install.sh` queuing `b1-policies`), the settle does nothing; run `sudo
/usr/local/bin/harvester-secrets restore-settle` by hand, or restore from
the host. A secret migrated after the snapshot is lost (the preflight lists
the class); a cluster SSH test then answers 500.

Since 2.10.0 the allowlist also accepts what the console renders for
per-person tokens and ESO, and nothing else: the auth methods
`jwt-hops-<id>-p-*` and `jwt-hops-<id>-eso-*` with their exact
configuration and roles (the `eso` role bound to the recorded namespaces
and service account UIDs), the policies `hops-person-<id>`,
`hops-group-<id>-*` and `hops-eso-<id>-*` with the text the console renders
from its settings, the groups `hops-<id>-*` carrying only their
`hops-group` policy, entities without policies whose aliases are on those
methods, and the OpenTofu token role bound or not to `127.0.0.1/32`. After
a restore, `harvester-secrets restore` puts the settings in line with what
the snapshot holds (methods, groups, ESO clusters, listener; person tokens
turned off when no method is left), and the ESO listener is turned off when
no ESO cluster is left. A snapshot taken before a namespace was enabled for
ESO is refused (its namespace and UID list differs).

Restoring by hand (`sudo harvester-secrets restore <file>`) does not check
the console's running actions. The preflight lists KVM keys rotated from the
console only: a rotation run by hand with `harvester-secrets rotate ssh-kvm`
is not listed.

## OpenTofu encrypted states

With OpenBao active and OpenTofu (not Terraform), each declaration state is
encrypted by the transit engine: one key per declaration, a child token
limited to that key and to the action. Existing clear states are migrated at
the next action.

Known limits:

- **Migration needs the provider and the cluster**: it goes through
  `apply -refresh-only`, so the first action of a declaration after
  activation (even a plan) refreshes and writes the state, drift included.
  An unreachable cluster makes that action fail at `encrypt-state` until
  the migration can run.
- **Copies before a move stay in clear in the shared workspace**: when
  resources are taken over by an encrypted declaration, the `.pre-move-*`
  copy of the shared workspace of the cluster stays in clear (the shared
  workspace is not a declaration). Remove it with the workspace clean-up.
- **A read can create a token**: a declaration view (viewer role) whose
  address cache is stale asks OpenBao for a child token and may create the
  transit key. This is not a tracked action.
- **Put back in clear, then any action encrypts again**: the button is meant
  for "before disabling OpenBao"; there is no per-declaration opt-out while
  OpenBao is active. `disable` refuses while a state is encrypted.
- State moves use `state pull` / `state push -`; `-state-out` is refused
  (it writes the destination in clear).
- Transit keys of a removed declaration are kept 30 days, then removed by
  the daily sweep, unless the declaration exists again with the same id.
  The console can remove the `hops-<console id>-*` keys of its own prefix.

## Secrets of a deployment plan

While a plan runs, the secrets typed for it stay in OpenBao
(`plan-run/<action id>/...`): a stopped plan resumes without retyping them.
They are erased at the end of the run, after 7 days for a stopped run, or by
**Forget the kept secrets**.

## Audit and correlation

Each request carries `X-Hops-Action` (the console action id, or
`bg-<task>`) and `X-Hops-Actor` (the person, or `service`), recorded in
clear in the OpenBao audit log. In B1 the audit log is declared in the
configuration; OpenBao refuses requests it cannot log, so the console warns
below 1 GiB free or 10 % of the volume.

## Roles

- Writes to `/api/secrets/*` and every secret store action: administrator.
- `GET /api/secrets/status`: operator (the Disaster Recovery card).
- The banner: everyone.
- Per-person tokens and ESO set-up, keys, listener and removal:
  administrator. Writing an ESO value: operator allowed to administer the
  child cluster (read access to its `<cluster>-kubeconfig` Secret), with
  their own token.
- A refusal of a person by OpenBao is HTTP 403; an outage stays 503.
- Secret store actions (`secrets:*`) name secrets and paths: they are shown
  in the dock, the action details, the stream and Activity to administrators
  only. The same goes for the agent logs (`secrets-agent-*.log`) in Activity
  and `/api/logs/`.

## Per-person tokens (2.10.0)

Off by default after the update: nothing changes until an administrator
turns it on. With it on, a person signed in to the console through Rancher
(single sign-on) reads and writes the **personal classes** (`ssh-cluster`,
`ssh-kvm`, `tf-kvm`, and the write-only `eso` class) with an OpenBao token
of their own. OpenBao judges each request itself and its audit log names
the person, without relying on a header set by the console.

### Who acts with which identity

| Situation | Identity shown to OpenBao |
|---|---|
| Person signed in through Rancher single sign-on, request that reads or writes a personal class | the person's token |
| Same person, other classes (`kubeconfig`, `rancher-oidc`, `replication-*`, `plan-run`, OpenTofu child tokens) | service (AppRole) |
| Shared KVM views (host table, surveys, view of all VMs) | service; the gestures on a host keep the person's token |
| Direct Rancher sign-in (user name and password), local console account | service |
| Worker thread of an action after it was launched | no new read with a person's token: the secrets are read at launch and held by the lease of the action; a late read goes through the service and is marked |
| Background tasks, command line, `sudo harvester-secrets` | service |

There is **no silent fallback**: when OpenBao refuses a person who has a
token, the person gets the refusal (HTTP 403, `openbao-denied` with the
class and name, or `openbao-person-login-failed`); the console never reads
the same secret again with the service identity. The service is used only
for sessions without an OIDC token and when the setting is off.

Activity marks each action that read a secret: **personal token**,
**service** or **late service** (read by the service after launch).
Writes carry `custom_metadata` `hops_identity` (`person` or `service`) next
to `hops_actor`.

### How it works

- One OpenBao `jwt` auth method per Rancher whose single sign-on is set:
  `jwt-hops-<console id>-p-<rancher id>`. It trusts the OIDC provider of
  that Rancher (`oidc_discovery_url` = `<rancher url>/oidc`, RS256 only,
  `default_role = person`).
- The console presents Rancher's **identity token** (`id_token`), never the
  access token: the access token carries a Rancher token usable on every
  cluster of the person. Both tokens have the same audience, so OpenBao
  cannot tell them apart; the console refuses to send a JWT that carries
  `token` or `scope`.
- Role `person`: `user_claim = sub`, `bound_audiences` = the console's OIDC
  client, `claim_mappings` `name` and `auth_provider` (metadata `name`,
  `provider`, visible in the audit log), policy `hops-person-<console id>`,
  token of 15 minutes not renewable beyond that, bound to the console
  address (`127.0.0.1/32` in B1, the console range in mode A: a login from
  anywhere else is refused with 403). Never a `groups_claim` (a role with
  one refuses every person whose token has no groups).
- The person's OpenBao token lives in the console memory only, per session.
  The console logs in at the first personal read, logs in again with the new
  identity token at each Rancher refresh (every 7 to 8 minutes with a client
  of 10 minutes) and revokes the previous token; at sign-out, session end or
  refused refresh it revokes the token and empties that person's cache
  entries. Rancher 2.14.1 gives a new `id_token` at each refresh, so no
  long-lived token is needed.
- Policy `hops-person-<console id>`: read, create and update on the
  `ssh-cluster`, `ssh-kvm` and `tf-kvm` paths and their metadata; create and
  update (no read) on `eso` and its metadata; `sys/capabilities-self`.
  Nothing on `kubeconfig`, `rancher-oidc`, `replication-*`, `plan-run`,
  transit or AppRole.
- Cache: a person's read always goes to OpenBao with their token; the value
  then feeds the shared cache. The entry (entity, reference) serves the same
  person only, during an outage, for the cache duration.
- One identity entity per person and auth method stays in OpenBao after the
  last login. It carries no policy. Removing it is the OpenBao
  administrator's job; the console lists the entities it has seen.

### Turning it on

Settings > Secret store > **Personal tokens** (administrators). For each
Rancher of Settings > Sign-in through Rancher:

1. **Set up**: in B1 the agent creates the auth method, its configuration,
   the role and the policy, without restarting OpenBao (discovery only:
   static keys are refused in B1, `person-token-static-mode-a`). In mode A
   the assistant shows the `bao` commands and the exact policy text for your
   OpenBao administrator; OpenBao must reach Rancher's discovery address, or
   use static keys taken from Rancher's JWKS (the commands carry the JWKS
   fingerprint and how to recompute it: trusting those keys is the OpenBao
   administrator's decision). The console itself never writes a policy, a
   role or an auth method.
2. **Checks**, run with a real login of the administrator who opens the
   window (single sign-on session of that same Rancher, with an identity
   token): discovery document, login, token (policies `default` and
   `hops-person-<id>`, bound to the console, at most 900 s), capabilities
   (personal classes readable, `eso` write-only, other classes and other
   consoles refused), revocation. **Activate** stays grey until the five are
   green in the window. A local account, a direct sign-in or a session of
   another Rancher see why the checks are closed
   (`openbao-person-no-token`, `person-token-other-rancher`).
3. **Activate**: at once, without restarting the console or OpenBao.
   Every single sign-on session of 2.10.0 already keeps its identity token,
   so sessions opened before activation switch to the person token at their
   next personal read. `openbao-person-no-token` is for a session without an
   identity token (local account, direct sign-in) trying a check.

**Turn off** puts everything back on the service at once and revokes the
tokens in use. **Remove** (identifier retyped) also removes the auth method
of a Rancher from OpenBao; **Remove everything** removes every method and group.

### Groups (internal OpenBao groups)

Rancher 2.14.1 does not emit a `groups` claim (and refuses the `groups`
scope, which makes the whole sign-in fail): the console never asks for it in
2.10.0. Groups are **internal** OpenBao identity groups `hops-<id>-<name>`
with a policy `hops-group-<id>-<name>` (read limited to `<class>/<cluster>`
for chosen classes and clusters), whose members are entities picked among
the ones the console has seen. Adding or removing a member applies to the
next request, on the token already issued. In B1 the agent writes the
groups; in mode A the assistant shows the commands. The setting **Reading only
through groups** reduces `hops-person-<id>` to `sys/capabilities-self`.

In B1 the agent can put any policy on a `hops-<id>-*` group:
`allowed_parameters` does not restrict it (a comma-separated string goes
around the pattern). The agent is root on the host and holds the unseal
key; the guard is the closed list of agent operations and the restore
review.

### Rancher signing keys

The console reads the fingerprint of Rancher's JWKS every hour. With static
keys (mode A), a changed key raises `openbao-jwt-keys-changed` and a banner
for administrators: **Update the keys** writes the old and the new key
(`jwt_validation_pubkeys` holds several), **Confirm the keys** drops the old one
once sessions use the new one. A JWKS unreadable three hours in a row raises
`openbao-jwt-keys-unreadable`. With discovery, OpenBao follows the keys
itself.

### Limits of per-person tokens

- In B1, the issuer, client and CA of a Rancher are pinned in root's state
  at the first set-up (trust on first use) and changed only with `sudo
  harvester-secrets person-token ...`; the pin survives the removal of the
  method.
- A person who writes a personal class can also rewrite the
  `custom_metadata` (`hops_actor`, `hops_identity`) and `max_versions` of
  the secrets of that class: the attribution shown in the console is
  declarative, the OpenBao audit log (entity, `sub`, `name`) is the
  reference.
- An identity token found invalid at a Rancher refresh closes the console
  session (the person signs in again), whether personal tokens are on or
  off.
- Revocation: a console sign-out revokes the person token at once; a
  sign-out on the Rancher side is seen at the next refresh (the refresh is
  refused), so within about 10 minutes; **Turn off** revokes within about
  30 seconds. The tokens of a console process that stopped (restart, crash)
  are not revoked: they expire with their TTL (15 minutes at most), and they
  only work from the console address.
- With **Reading only through groups** on, administrators need a group too
  for the personal classes.
- In mode A with static keys, the "keys changed" alert clears once the
  update commands are rendered, before the OpenBao administrator runs them.
- Personal reads made at the launch of an action carry `X-Hops-Action:
  bg-console` rather than the action id, and the login line of a person
  (first login and re-login at each refresh) carries `X-Hops-Actor:
  service`; the data lines and the login response still name the person
  (entity, alias, `name`).
- The rollback guard reads the console settings, not root's state: after a
  `person-token disable --remove` that failed half-way, check the Personal
  tokens tab (and `bao auth list` in mode A) before rolling back.

## External Secrets Operator in child clusters (2.10.0)

Option of the RKE2 child clusters created by the console (Cluster API on
Harvester), administrators only, OpenBao active only (`eso-needs-openbao`).
Never on a Harvester cluster. The console sets the operator, its access to
OpenBao and one store per enabled team namespace, and shows the sync state.
The `ExternalSecret` objects belong to the teams (GitOps or their own
tools); a rotation in OpenBao reaches their Secrets by itself.

### What the console sets

- **Chart**: the official `external-secrets` chart in an artefact package
  (`chart` item named `eso`, like `flux2`), installed by CAAPH in the
  `external-secrets` namespace from the child cluster's registry. The
  package carries the version (checked with chart 2.14.0, appVersion
  v2.14.0, API `external-secrets.io/v1`). Values imposed by the console:
  CRDs by the chart, webhook and cert controller on, cluster-scoped stores,
  cluster external secrets and push secrets off, bitwarden and service
  monitor off, no token cache.
- **Per enabled namespace** (label `harvester-ops.io/eso: "on"`): a service
  account `hops-eso` without any Kubernetes right and a `SecretStore`
  `hops-openbao` (provider `vault`, KV v2, the OpenBao CA inline,
  `auth.jwt` with role `eso`, token audience `hops-openbao-<console id>`,
  600 s), delivered by the ClusterResourceSet `art-eso` once the chart is
  ready. System namespaces are refused (`eso-system-namespace`).
- **In OpenBao**: one `jwt` auth method per child cluster,
  `jwt-hops-<id>-eso-<key>` (key = 8 hex of SHA-256 of
  `<harvester>/<namespace>/<cluster>`), with the public keys of the child's
  service account JWKS read by the console with the child kubeconfig (RKE2
  refuses anonymous reads of the JWKS: OpenBao never reaches the child API
  and keeps no reviewer token). Role `eso`: audience
  `hops-openbao-<id>`, `bound_claims` on the service account name
  `hops-eso`, the list of enabled namespaces and the **UID** of each
  `hops-eso` account (a namespace or account recreated with the same name is
  refused until it is enabled again: `eso-account-changed`), tokens of 10
  minutes bound to the node network of the child when known. One templated
  policy per cluster: a namespace reads only
  `eso/<key>/<its namespace>/*`.
- **Values** live under the console prefix at `eso/<key>/<namespace>/<name>`.
  The console never reads them. A person writes one from the External
  Secrets tab of the child cluster with their own token (masked fields,
  never shown again), or the team writes them directly in OpenBao.

Before the store is posed, a pod on the VM network probes OpenBao
(`GET sys/health` in TLS with the CA): `eso-openbao-unreachable` blocks.

### Mode B1: the ESO listener

In B1, OpenBao listens on `127.0.0.1` only. ESO needs a **second TLS
listener** on an address of the host. Set its address with
`harvester-secrets eso listener --addr <ip>:8212` (or the External Secrets
card of Settings > Secret store): the address is kept, and the listener is
turned on with the first child cluster that gets ESO and off with the last
one. The console's own roles stay bound to `127.0.0.1`.

- The address must be **static** on the host. At each start the B1 unit
  checks the address is present (`ip -o -4 addr`) and, if it is not,
  starts OpenBao on the loopback only and says so (the console keeps
  working, ESO stops syncing); the agent's hourly health pass reports
  `eso-listener-address-absent` and a banner, so a lost address is seen
  within an hour.
- The listener has **its own CA** (the B1 CA is constrained to `127.0.0.1`
  and its key is not kept), name-constrained to the listener address and
  made again each time the listener is turned on; the stores posed then
  carry it.
- Open the port in the host firewall to the **node networks of the child
  clusters only**, for example
  `firewall-cmd --zone=public --add-rich-rule='rule family=ipv4 source address=<child node network> port port=8212 protocol=tcp accept'`
  (and the same with `--permanent`). The console does not touch the
  firewall.
- The OpenTofu child tokens are bound to `127.0.0.1/32` and the
  unauthenticated rekey endpoints are disabled, so the listener exposes
  only what ESO needs.
- A B1 bundle built before 2.10.0 has no listener line in its template:
  `upgrade-b1` with such a bundle is refused while the listener is on
  (`eso-listener-template`).
- The other way round, a B1 bundle built by 2.10.0 is for a 2.10.0 console:
  `enable --mode b1` with it on a 2.9.x console fails ("a storage backend
  must be specified", then `b1-start-timeout`, undone cleanly). After a
  rollback, keep the B1 already posed (2.10 leaves `openbao.hcl.tpl` readable
  by 2.9 and keeps its own template in `openbao-eso.hcl.tpl`) or build the
  bundle with the 2.9.x release.
- The listener certificate is valid 730 days and is not renewed by itself:
  turn the listener off and on again (with no ESO cluster left) or move it
  before it expires; the date is in `/etc/harvester-ops-openbao/tls/eso.pem`.

The listener's cluster port is kept on the loopback (`127.0.0.1:<port + 1>`):
without it, OpenBao would also open `<address>:<port + 1>` on the child
network. Ports 8209 to 8211 and 65535 are refused.

In mode A, the OpenBao address of the console settings is the one the
stores use, so it must be reachable from the child nodes (the console's own
AppRole range is then the host address, not `127.0.0.1`). The set-up,
namespace and removal actions end with the commands for your OpenBao
administrator (auth method, role, policy of the cluster), shown in a window
with copy; the stores stay `InvalidProviderConfig` until they are run. On a
re-run, skip the first line (`bao auth enable`) when the method already
exists.

### Removing ESO (decision 300)

**Remove External Secrets** (tab of the child cluster, or `harvester-capi
eso-remove`), in this order: the `ownerReferences` that tie the synced
Secrets to **our** `ExternalSecret` objects are removed (the Secrets stay,
frozen at their last value), those objects are deleted while ESO still runs
(their finalizer is removed only by the controller; `eso-remove-stuck` if
it takes too long), then the ClusterResourceSet, the namespace labels, the
chart, and the auth method, role and policy in OpenBao. "Ours" means: in an
enabled namespace, pointing at the `hops-openbao` store, not delivered by
Flux. `ExternalSecret` objects of the teams are left alone; while any
remains, removing the chart is refused (`eso-foreign-objects`, with their
names), because the chart takes the CRDs with it.

The **Undo** of a deployment plan that posed ESO removes our objects with
the ESO phase and keeps the chart while team `ExternalSecret` objects
remain in a child cluster that stays: the chart item ends in
`eso-foreign-objects: chart kept, ...`. Remove those objects (or their
owners' GitOps entries), then undo again. The same rule holds when the
child cluster is removed by the same undo (the chart would block its
removal too): leave the `eso` chart item unchecked and the cluster removal
takes everything. Removing the `eso` chart any other way (Services or
Artefacts, after **Remove External Secrets** with the chart kept) is refused
the same way.

### Limits

- A service account token revoked in Kubernetes is accepted by OpenBao
  until it expires (10 minutes).
- The keys of a child cluster are read by the console and trusted on the
  first set-up; the hourly pass compares their fingerprint
  (`eso-keys-changed`, **Update the keys**). RKE2 service account keys do not
  rotate by themselves; a machine renewal keeps them.
- A compromised console could put its own key on a child's auth method and
  read `eso/<key>/<ns>/*`, which it can already read and write: the audit
  attribution of an ESO read is not a guarantee against a compromised
  console.
- Mode A: the tab keeps saying "waiting for the OpenBao administrator"
  even once the stores are ready (no acknowledgement path).
- A `hops-eso` account deleted by hand is reported as `eso-account-changed`
  (not as missing), and the ClusterResourceSet does not recreate it: enable
  the namespace again.
- Removing the chart also removes its release namespace `external-secrets`.
  The OpenBao entities of the ESO logins stay (the OpenBao administrator
  removes them).
- ESO logs in at each reconciliation and revokes its token after: keep
  `refreshInterval` at 1 h or more (the example the console shows) to keep
  the audit log small.

## Disabling, switching mode, going back to an older console

- `disable` is refused while a class is in OpenBao or a state is encrypted.
  It erases the secrets of stopped plan runs first. `disable --remove-b1`
  also stops and removes the B1 unit and its podman secrets, and sets the
  B1 data aside; the account, the images and the bundle stay (see below).
- B1 to A: bring every class back (`migrate --to local`), `disable
  --remove-b1`, then enable mode A.
- **Rolling the console back below 2.9.0** (Update > Roll back, or Install
  with "allow older") is refused while the store is not local, a class is in
  OpenBao or a state is encrypted (`secrets-in-openbao`; also when
  `secrets-settings.json` exists but cannot be read), with the steps to
  do first: bring every class back, put encrypted states back in clear,
  disable the store. A 2.8.x console would read the `openbao:` references as
  paths or values: clusters with a moved kubeconfig fail (a `status` action
  even reports done while kubectl failed), SSH keys are not found,
  notifications send the reference as the token. `install.sh` run by hand
  with an older release is not guarded: follow the same steps first. The
  B1 container and the 2.9.0 agent units keep running after such an
  install; the `.migrated` originals are still on disk until they expire.
- **Rolling the console back below 2.10.0** in B1 is refused while the
  settings carry a Rancher `jwt` method (set or pending), a group, the ESO
  listener or an ESO cluster (`secrets-person-token-b1`): a 2.9.x console
  would refuse its own snapshots (the 2.9.0 review does not know these
  objects). Steps first: `harvester-secrets person-token disable --remove`
  and `harvester-secrets eso remove --all` (after removing ESO from each
  child cluster). In mode A the rollback is allowed with a warning in the
  action and the response: remove the auth methods, policies and groups on
  your OpenBao yourself if you no longer want them.

### What stays after `disable --remove-b1` and after a rollback

`disable --remove-b1` stops and removes the B1 unit and its podman secret,
and sets the data aside (never deleted on the spot):

- kept on purpose: `/var/lib/harvester-ops-openbao.removed-<stamp>`,
  `/etc/harvester-ops-openbao.removed-<stamp>` (unseal keys) and the
  `.old-<stamp>` volumes of earlier restores, the snapshots in
  `/var/lib/harvester-ops-snapshots`;
- left in place: the `harvester-ops-openbao` account, the images
  `localhost/harvester-ops-openbao:*` and the Application Collection image,
  the bundle in `/var/lib/harvester-ops/secrets-agent/staged/`, the empty
  `/etc/harvester-ops/openbao`.

#### Removing the B1 leftovers, console staying on 2.9.x

Keep `/opt/harvester-ops/openbao`, `harvester-secrets-agent.path`,
`harvester-secrets-agent.service` and `harvester-secrets-rotate.timer`:
2.9.0 needs them to enable B1 again and to keep the console's own
`secret_id` rotated.

```bash
# every B1 image tag that exists: current, the version tag, and previous
# only if an upgrade ever ran (xargs -r runs podman rmi 0 times on an
# empty list, so a console that never upgraded B1 is not an error here)
sudo podman images --format '{{.Repository}}:{{.Tag}}' \
    | grep '^localhost/harvester-ops-openbao:' | xargs -r sudo podman rmi -i
# the Application Collection image the bundle loaded: same registry and
# tag `harvester-secrets bundle` used (dp.apps.rancher.io/containers/openbao
# by default; check `podman images` if the bundle came from a mirror)
sudo podman rmi -i dp.apps.rancher.io/containers/openbao:<version-tag>
sudo rm -f /var/lib/harvester-ops/secrets-agent/staged/harvester-ops-openbao-*
sudo rmdir /etc/harvester-ops/openbao 2>/dev/null || true
sudo userdel harvester-ops-openbao   # recreated automatically if B1 is enabled again
```

Keep a copy of the last snapshot and of its unseal key elsewhere first if
you may need these secrets again.

#### Removing the 2.9.0 secret store agent, after a rollback to 2.8.x

A console rolled back to 2.8.x does not know the 2.9.0 secret store units:
`harvester-secrets-agent.path` and `harvester-secrets-rotate.timer` stay
active (they only refresh a health file once the store is local). To remove
them by hand:

```bash
sudo systemctl disable --now harvester-secrets-agent.path harvester-secrets-rotate.timer \
    harvester-ops-openbao
sudo rm -f /etc/systemd/system/harvester-secrets-agent.{path,service} \
           /etc/systemd/system/harvester-secrets-rotate.timer \
           /etc/systemd/system/harvester-ops-openbao.service /usr/local/bin/harvester-secrets*
sudo systemctl daemon-reload
sudo rm -rf /opt/harvester-ops/openbao /etc/harvester-ops/openbao \
            /var/lib/harvester-ops-openbao.* /etc/harvester-ops-openbao.* \
            /var/lib/harvester-secrets-agent /var/lib/harvester-secrets-agent.lock
```

Then remove the B1 leftovers with the commands above if it was not done
already. `uninstall.sh` removes the units itself; `uninstall.sh --purge`
also removes the B1 data and keys.

## Command line

`harvester-secrets` (run as the console account, or with `sudo`; as root it
does only the agent call itself and runs the rest in a child dropped to
`harvester-ops`): `status`, `assistant`, `check`, `enable`, `disable`,
`migrate`, `confirm`, `outage-test`, `versions`, `rollback`, `rotate`,
`offline-copy`, `snapshot`, `snapshots`, `settings`, `sweep-orphans`,
`restore`, `bundle`, `upgrade-b1`, `file`, `tf-encryption`,
`tf-entity-remove`, `tf-keys`, and since 2.10.0:

- `person-token status | assistant --rancher ID --issuer URL --client-id ID
  [--ca FILE] [--keys discovery|static] [--console-cidr CIDR] | check|enable
  --rancher ID --id-token-stdin | disable [--rancher ID] [--remove] | groups
  list|set NAME --class C --cluster X [--member ENTITY]|remove NAME|only on|off
  | keys --rancher ID [--refresh|--confirm]`. The identity token is read on
  stdin only, never as an argument.
- `eso status | listener --addr IP:PORT | --off | assistant|add --cluster
  H/NS/C --namespace NS ... [--kubeconfig K] [--node-cidr CIDR] [--sa-uid
  NS=UID] | keys --cluster H/NS/C [--refresh] | remove --cluster H/NS/C |
  --all`.
- On the child cluster side, `harvester-capi eso-deploy`, `eso-status`,
  `eso-remove [--keep-chart]`.
- `restore-settle`: puts the settings in line with the last restore (run by
  the console after a restore it launched).
- With `sudo`, a `--spec FILE` must be owned by root and mode 0600.

`upgrade-b1 --bundle <file>`: new B1 image from a bundle, pre-upgrade
snapshot first, back to the previous image if OpenBao does not unseal; an
older OpenBao is refused.

A tool run by hand reads the Terraform workspace root the console recorded
at start (`<state>/console-paths.json`), unless
`HARVESTER_OPS_TF_WORKSPACES` is set.

## Not verified

- OpenBao as a cluster at a customer, Vault Enterprise with namespaces.
- Rotation of the SSH keys of a fleet of several KVM hosts (one host
  verified).
- The GitOps phase of a deployment plan stopped then resumed (a plan with
  Git secrets was stopped and resumed without retyping them, without the
  GitOps phase).
- The 30-day `secret_id` rotation fired by the timer (rotation on request
  verified; the timer refreshed health only).
- The B1 audit-space warning before refusal on a full volume (the refusal
  and its `openbao-internal` code verified in mode A).
- A console with the same console identifier as another one on a shared
  mode A server (clone, restore): both sweep each other's keys and orphans.
- 2.10.0, per-person tokens: groups fed by a `groups` claim of Rancher (no
  Rancher with a group provider on the bench: only internal groups were
  verified); a Rancher behind Active Directory or SAML; the real rotation of
  Rancher's signing key (simulated with static keys); a Terraform KVM
  declaration running longer than the person token (the mechanism was
  verified with a 17-minute KVM image fetch to node2: secrets read at
  launch with the person token, token revoked meanwhile, action done);
  per-person tokens on Vault other than 1.14.2.
- 2.10.0, External Secrets: ESO in B1 end to end with a real child cluster
  (ESO was verified in mode A on a harvlab2 child; the B1 listener, its CA,
  the unit fallback and the rollback guard on a client VM without a child
  cluster); a child cluster of a Harvester with several nodes; the window
  that shows the mode A commands (the same commands were taken from the
  assistant during the check).
