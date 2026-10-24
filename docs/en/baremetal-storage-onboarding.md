# Optional storage profiles for bare-metal joins

In **Bare-metal > Install**, select **Join** and enable **Optional storage
onboarding**. It is disabled by default and hidden when creating a cluster.
It only accepts a new hostname joining a declared cluster. Registered nodes are
never reinstalled by this option. Witness nodes are refused.

Choose a profile. For each storage port, pick the physical NIC by its **MAC**
and enter the assigned **IPv4/CIDR** address. Addresses are not derived from the
management IP. Management, VM uplink and storage MACs must be distinct. Storage
connections have no gateway or DNS, `never-default=true`, and IPv6 disabled. Use
the existing VM-uplink controls separately; this extension does not replace or
change shared network configurations. Pool creation and VM uplinks run before
storage readiness.

## NIC identity: the MAC, not the name

A storage NIC is identified by its MAC only, like the management and VM uplink
NICs. No Linux name is entered, and no name prefix is assumed (`ens`, `enp`,
`eth`, `eno` and others all work).

- At every boot, the generated script looks up the MAC in `/sys/class/net/*/address`.
  It accepts exactly one physical NIC (a `device` link) that is not enslaved to a
  bond or a bridge. Otherwise the storage stage stops before activating anything.
- The resolved name is kept in `/run/harvester-ops/storage-nics/<role>` for that
  boot. Profile files use `{{role}}` to refer to it.
- The NetworkManager connection is bound by `[ethernet] mac-address=`, without
  `interface-name`. A renamed NIC keeps its connection.
- After the join, the console checks the host's LinkMonitor inventory. Each MAC
  must be one physical, unbonded NIC. An automatic network configuration
  (VlanConfig) that selects the host must not list a resolved storage NIC. This
  check can only run after the join, because the names are not known before.

Before installation, the console refuses a storage MAC that LinkMonitor already
reports on a registered host.

## Installer options

`harvester.install.skipchecks=true` (or the skip checks field) is allowed with a
storage profile. The profile does not depend on the installer hardware checks.

## Packaged profiles

- **Storage network only** configures two dedicated NetworkManager interfaces.
  It does not configure multipath, iSCSI, NVMe or a CSI provider, and makes no
  claim about CSI readiness. This is also a starting point for other providers.
- **Pure iSCSI and NVMe/TCP (unverified example)** configures two iSCSI and two
  NVMe/TCP interfaces, udev tuning, multipath device tuning and `eth-iscsi1/2`
  bindings. **It has not been verified against an array by the console
  maintainers.** Review every setting for your hardware and deployment.

The Pure example uses the Harvester installer's own multipath option. Its
`multipath` block becomes `os.external_storage_config` (`enabled: true` and a
`multi_path_config` with `blacklist`, `blacklistExceptions` and
`blacklistWwids`). Harvester then renders `/etc/multipath.conf` and enables
`multipathd`, and the installer no longer adds `multipath=off`. The local boot
disk WWID you enter is added to `blacklistWwids`. Enter the SCSI WWID of the
actual local boot disk, not `/dev/sda` or a WWN with a `0x` prefix. Device tuning
that `external_storage_config` cannot express (`defaults`, `devices`, `devnode`)
goes to `/etc/multipath/conf.d/90-pure.conf`. Kernel modules are loaded by the
installer through `os.modules`.

The Pure example expects an **already installed** `portworx/px-pure-csi-node`
DaemonSet. That namespace/name is profile data, not a universal CSI convention.
No profile installs a CSI chart, creates credentials, configures array access,
or provisions storage. Review the generated YAML before installation. The local
Longhorn default disk must become Ready; profile-specific CSI checks are required
only when the selected profile declares them.

## Protection and readiness

Generated configuration contains persistent NetworkManager keyfiles and an OEM
file. Boot stages resolve the NICs before activating storage links, check the
boot WWID when required, and verify addresses, protocol bindings, modules and
services before advertising a profile-completion label. No debug shell is
persisted. The default partition layout is unchanged.

The joining node stays cordoned. How it gets there depends on the version of
the joined cluster. The console reads the `server-version` setting with the
person's frozen kubeconfig before installation.

### Harvester v1.9.0 and later: native install cordon

The console sets the installer option `install.cordoned: true` (added by
harvester/harvester#11301, present in the v1.9.0 tag). Harvester then does the
work:

- the installer adds the label `harvesterhci.io/install-cordoned=true` to the
  RKE2 configuration. Kubelet registers the node without it. RKE2 applies it
  when it initializes the node, 17 to 18 seconds later on the test bench. Until
  then the node keeps the `node.cloudprovider.kubernetes.io/uninitialized:NoSchedule`
  taint. On the bench, the cordon appeared within the same second as that
  taint was removed;
- the promote controller cordons the node once and records it with the
  annotation `harvesterhci.io/install-cordoned-processed=true`;
- nothing in Harvester lifts that cordon, and the controller never selects a
  cordoned node for promotion.

The console writes nothing to the node. It waits for the processed annotation,
then checks that the node is still unschedulable. No onboarding taint is
written and no node patch permission is required. The job stops if the node is
schedulable: Harvester leaves it schedulable when no other node is available.
An installer older than the cluster ignores the option: the label never comes,
and the wait ends with the readiness timeout and that reason.

Pre-releases of v1.9.0 and unreadable versions use the console mechanism below.

### Harvester v1.8: console taint and cordon

An initial `harvester-ops.io/onboarding=pending:NoSchedule` taint protects the
joining node. It is written to `/etc/rancher/rke2/config.yaml.d/` by the
installed system's initramfs stage, before RKE2 first starts. That stage
rewrites the file at **every boot**. It has no effect after registration:
kubelet applies registration taints only when it creates the Node object. A
later reboot does not taint the node again, unless the Node object was deleted.
The file lives in the installer's own OEM configuration, which the console
cannot edit after the join, so it is not removed.

The node's server-generated installation label must match this job before a
conditional UID/resourceVersion patch cordons it and removes only that taint.
If the node changed in between, the patch is refused as a whole, the console
reads the node again and retries, up to `HARVESTER_OPS_ONBOARDING_PATCH_ATTEMPTS`
times (5 by default). A refusal by the cluster RBAC stops at once. A patch whose
outcome is unknown (timeout) is never retried. Other labels and taints are
preserved. Reads and writes use the person's frozen kubeconfig from the join
request, never a fallback administrative identity.

Harvester promotes a joining node to the control plane while the cluster has
fewer than three management nodes, unless the node has the `worker` role. Its
promote controller sets the node unschedulable when promotion starts and makes
it schedulable again when promotion completes (`promote_controller.go`). The
status is the node annotation `harvesterhci.io/promote-status` (`running`,
`complete`, `failed`, `unknown`). On v1.8 the controller can promote a node
that the console has already cordoned.

- While the status is `running`, nothing is reported ready and the job waits.
  If promotion started before the console cordoned the node, the onboarding
  taint stays in place until then. When the status becomes `complete`, the
  console cordons the node and removes the taint.
- If the controller lifts the cordon when promotion completes, the console
  cordons the node again. Any other uncordon stops the job.
- A `failed` or `unknown` promotion stops the job; the node stays tainted or
  cordoned.

Two gaps remain on v1.8:

- between the controller's uncordon and the console's new cordon, the node can
  be schedulable for up to one polling interval (5 seconds);
- a promotion that starts after the job has ended lifts the cordon. Check the
  node before releasing it.

### Final checks

The final stage verifies physical storage NIC ownership, Node Ready, no resource
pressure, the local Longhorn disk, the boot-completion label and, if declared,
the exact CSI DaemonSet pod on this node (read with a server-side field selector,
pods being deleted are ignored). New controllers with null or incomplete status
stay pending. The checks give up after `HARVESTER_OPS_ONBOARDING_TIMEOUT` seconds
(1800 by default), within the post-installation budget. Failure or cancellation
leaves the node cordoned, or initially tainted if protection could not be
confirmed. There is no automatic production release, VM migration,
configuration rollback, or deletion of a registered node.

A successful preparation is not an application or storage-path acceptance test.
Validate array registration, path health and failover, VM migration and guest I/O
before deliberately releasing the node.

## Adding a provider

Profiles are administrator-reviewed release content under
`web/storage_profiles/<profile-id>/`. They are not arbitrary uploads or scripts
submitted through the installation API. Copy `storage-network-only/profile.yaml`
and give the directory a new lowercase identifier. Its manifest declares:

- `label`, `description`, and an ordered `ports` mapping of role to display label;
- `files`, explicit packaged OEM YAML basenames. They may only use the `fs`,
  `boot` and `network` stages: the OEM file is written during the installed
  system's initramfs stage, so its own `rootfs` and `initramfs` stages would not
  run at first boot. `{{role}}` refers to the resolved NIC name and is only
  allowed in the `network` stage;
- `boot_disk_exclusion`, if the local boot disk WWID must be excluded from
  multipath (requires `multipath`);
- `multipath`, `null` or the installer's `multi_path_config` keys (`blacklist`,
  `blacklistExceptions` as vendor/product pairs, `blacklistWwids`,
  `blacklistExceptionWwids`);
- `modules`, loaded by the installer (`os.modules`), and `services`,
  `iscsi_ifaces`, the required completion checks;
- optional `csi_node: {namespace: ..., daemonset: ...}`, or `null` to skip CSI.

All manifest fields are required. Empty lists/maps are valid when not needed.
The number of ports and the CSI namespace/name are provider-specific. Provider
OEM files can run privileged boot commands, so review and test them as code.
No real IPs, array endpoints, credentials or customer identifiers belong there.
Add rendering, validation and readiness tests for a new profile.

This option is exposed in the single-host installation dialog. The YAML batch
profile editor does not yet serialize the onboarding object.
