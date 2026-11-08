# Secret store (OpenBao, 2.9.0)

The console keeps its secrets (cluster kubeconfigs, SSH keys, the Rancher
OIDC client secret, replication notification and hook secrets, Terraform
variables of KVM declarations, the secrets of a running deployment plan) in
local files of its state directory by default. Since 2.9.0 it can keep them
in OpenBao instead, class by class, and come back to local files at any
time.

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
was checked like OpenBao. OpenTofu documents its `openbao` key provider with
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
- encrypted declarations whose transit key was created or rotated after the
  snapshot (their state becomes unreadable);
- KVM hosts whose SSH key was rotated since (OpenBao would give back a key
  the host refuses);
- classes moved to OpenBao after the snapshot (their secrets are not in it).

The agent then stops the console and OpenBao, keeps the current volume
aside for 7 days (`/var/lib/harvester-ops-openbao.old-<UTC stamp>`), starts a
fresh instance with the key of the snapshot, restores it, and reviews the
restored configuration against an **allowlist** of what this console
expects: exact policy names and texts (the `default` policy compared with
the hash recorded at install), auth methods `token` and `approle` only, the
two AppRole roles with their policies, address ranges and single
`secret_id`, the OpenTofu token role, no identity group, the expected
mounts and audit device. Any difference refuses the snapshot and puts the
previous volume back. Then every token of the snapshot is revoked; the
console and the agent log in again. The agent and the OpenTofu token
policies are rewritten before the review, so a snapshot that only widens
them is repaired, not refused.

Restoring by hand (`sudo harvester-secrets restore <file>`) does not check
the console's running actions.

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
- Secret store actions (`secrets:*`) name secrets and paths: they are shown
  in the dock, the action details, the stream and Activity to administrators
  only.

## Disabling, switching mode, going back to an older console

- `disable` is refused while a class is in OpenBao or a state is encrypted.
  It erases the secrets of stopped plan runs first. `disable --remove-b1`
  also removes the B1 container, unit, account and volume.
- B1 to A: bring every class back (`migrate --to local`), `disable
  --remove-b1`, then enable mode A.
- **Rolling the console back below 2.9.0** (Update > Roll back, or Install
  with "allow older") is refused while the store is not local, a class is in
  OpenBao or a state is encrypted (`secrets-in-openbao`), with the steps to
  do first: bring every class back, put encrypted states back in clear,
  disable the store. A 2.8.x console would read the `openbao:` references as
  paths or values: clusters with a moved kubeconfig fail (a `status` action
  even reports done while kubectl failed), SSH keys are not found,
  notifications send the reference as the token. `install.sh` run by hand
  with an older release is not guarded: follow the same steps first. The
  B1 container and the 2.9.0 agent units keep running after such an
  install; the `.migrated` originals are still on disk until they expire.

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

A console rolled back to 2.8.x does not know the 2.9.0 secret store units:
`harvester-secrets-agent.path` and `harvester-secrets-rotate.timer` stay
active (they only refresh a health file once the store is local). To remove
everything by hand once you no longer need the snapshots and keys:

```bash
sudo systemctl disable --now harvester-secrets-agent.path harvester-secrets-rotate.timer
sudo rm -f /etc/systemd/system/harvester-secrets-agent.{path,service} \
           /etc/systemd/system/harvester-secrets-rotate.timer /usr/local/bin/harvester-secrets*
sudo systemctl daemon-reload
sudo rm -rf /opt/harvester-ops/openbao /etc/harvester-ops/openbao \
            /var/lib/harvester-ops-openbao.* /etc/harvester-ops-openbao.* \
            /var/lib/harvester-secrets-agent /var/lib/harvester-secrets-agent.lock
sudo podman rmi localhost/harvester-ops-openbao:current localhost/harvester-ops-openbao:previous
sudo userdel harvester-ops-openbao
```

Keep a copy of the last snapshot and of its unseal key elsewhere first if
you may need these secrets again.
`uninstall.sh` removes the units itself; `uninstall.sh --purge` also
removes the B1 data and keys.

## Command line

`harvester-secrets` (run as the console account, or with `sudo`; as root it
does only the agent call itself and runs the rest in a child dropped to
`harvester-ops`): `status`, `assistant`, `check`, `enable`, `disable`,
`migrate`, `confirm`, `outage-test`, `versions`, `rollback`, `rotate`,
`offline-copy`, `snapshot`, `snapshots`, `settings`, `sweep-orphans`,
`restore`, `bundle`, `upgrade-b1`, `file`, `tf-encryption`,
`tf-entity-remove`, `tf-keys`.

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
