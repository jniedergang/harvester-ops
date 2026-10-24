# Optional storage profiles for bare-metal joins

In **Bare-metal > Install**, select **Join** and enable **Optional storage
onboarding**. It is disabled by default and only accepts a new hostname joining
a declared cluster. Registered nodes are never reinstalled by this option.

Choose a profile and enter every required Linux NIC name, physical NIC MAC and
assigned IPv4/CIDR address. Addresses are not derived from the management IP.
Management, VM uplink and storage MACs must be distinct. Storage connections
have no gateway or DNS, `never-default=true`, and IPv6 disabled. Use the existing
VM-uplink controls separately; this extension does not replace or change shared
network configurations. Pool creation and VM uplinks run before storage readiness.

## Packaged profiles

- **Storage network only** configures two dedicated NetworkManager interfaces.
  It does not configure multipath, iSCSI, NVMe or a CSI provider, and makes no
  claim about CSI readiness. This is also a starting point for other providers.
- **Pure iSCSI and NVMe/TCP (example)** configures two iSCSI and two NVMe/TCP
  interfaces, reviewed udev/module/multipath settings and `eth-iscsi1/2` bindings.
  It excludes the exact local boot-disk WWID and HPE logical volumes from
  multipath. Enter the SCSI WWID of the actual local boot disk, not `/dev/sda`
  or a WWN with a `0x` prefix. The installer and installed kernel arguments retain
  `multipath=off`; the installed OEM profile enables multipathd for external
  storage. Review these provider settings for your hardware and deployment.

The Pure example expects an **already installed** `portworx/px-pure-csi-node`
DaemonSet. That namespace/name is profile data, not a universal CSI convention.
No profile installs a CSI chart, creates credentials, configures array access,
or provisions storage. Review the generated YAML before installation. The local
Longhorn default disk must become Ready; profile-specific CSI checks are required
only when the selected profile declares them.

## Protection and readiness

Generated configuration contains persistent NetworkManager keyfiles and an OEM
file. Boot stages check NIC identity before activating storage links, check the
boot WWID when required, and verify addresses, protocol bindings, modules and
services before advertising a profile-completion label. No debug shell is
persisted. The default partition layout is unchanged.

An initial `harvester-ops.io/onboarding=pending:NoSchedule` taint protects the
joining node. Its server-generated installation label must match this job before
a conditional UID/resourceVersion patch cordons it and removes only that taint.
Other labels and taints are preserved. Reads and writes use the person's frozen
kubeconfig from the join request, never a fallback administrative identity.

The final stage verifies physical storage NIC ownership, Node Ready, no resource
pressure, the local Longhorn disk, the boot-completion label and, if declared,
the exact CSI DaemonSet pod. New controllers with null or incomplete status stay
pending. Failure or cancellation leaves the node cordoned, or initially tainted
if protection could not be confirmed. Uncertain node patches are not retried.
There is no automatic production release, VM migration, configuration rollback,
or deletion of a registered node.

A successful preparation is not an application or storage-path acceptance test.
Validate array registration, path health and failover, VM migration and guest I/O
before deliberately releasing the node.

## Adding a provider

Profiles are administrator-reviewed release content under
`web/storage_profiles/<profile-id>/`. They are not arbitrary uploads or scripts
submitted through the installation API. Copy `storage-network-only/profile.yaml`
and give the directory a new lowercase identifier. Its manifest declares:

- `label`, `description`, and an ordered `ports` mapping of role to display label;
- `files`, explicit packaged OEM YAML basenames, with optional `{{role}}` NIC-name
  placeholders (only validated names are substituted);
- `boot_disk_exclusion` and `bootstrap_args`, if the provider needs a local SCSI
  disk exclusion or boot policy;
- `modules`, `services`, and `iscsi_ifaces`, the required completion checks;
- optional `csi_node: {namespace: ..., daemonset: ...}`, or `null` to skip CSI.

All manifest fields are required. Empty lists/maps are valid when not needed.
The number of ports and the CSI namespace/name are provider-specific. Provider
OEM files can run privileged boot commands, so review and test them as code.
No real IPs, array endpoints, credentials or customer identifiers belong there.
Add rendering, validation and readiness tests for a new profile.

This option is exposed in the single-host installation dialog. The YAML batch
profile editor does not yet serialize the onboarding object.
