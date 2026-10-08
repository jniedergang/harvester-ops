# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary: ops and SRE teams that run several SUSE Harvester HCI clusters in
production, often on isolated sites (airgap). Today they switch between the
Harvester interface of each cluster, Rancher and home-made scripts; they want
one console for the whole fleet, safe automation of long operations, and a
record of what happened. They work in it daily, under time pressure during
incidents and maintenance windows, and are technical (kubectl, Kubernetes RBAC,
Longhorn, KubeVirt are familiar terms).

Secondary: people discovering the project on GitHub (README, videos), who
decide whether to try it.

## Product Purpose

harvester-ops (short name harvops) is a modern console to operate a set of
Harvester clusters, built on three pillars:

1. every cluster in one interface;
2. functions that make automation simple;
3. tracking of events.

Graceful shutdown and startup is one of those automations, not the reason the
product exists: any presentation starts from the three pillars.

Success: an operator manages the fleet from one place, runs long operations as
a single tracked gesture, and can always tell what happened, who did it and
how it ended.

## Positioning

A multi-cluster console native to Harvester that neighbors (the Harvester UI,
which covers one cluster; Rancher, which is generic) do not offer together:

- every Harvester cluster in one interface, including clusters that are off or
  refuse the reader, said as such;
- every mutating gesture is a tracked action (live steps, logs, history), and
  changes made outside the console show up too;
- the same scripts behind the button and the command line (CLI and interface
  are equal, the interface never bypasses `bin/` scripts);
- a signed, airgap-ready bundle that installs and updates itself offline;
- the person's own rights applied on the cluster (impersonation, Rancher
  single sign-on), never a shared admin.

## Operating Context

- Deployed by the operator as a container under systemd (Podman), from a
  signed tarball; updated from the interface, online or from an uploaded
  archive, with automatic rollback.
- Reads clusters through the Kubernetes API (or kubectl), acts through scripts
  run as tracked actions; SSE streams live steps; SQLite keeps history.
- Typical work: VM lifecycle across clusters, maintenance and drains, storage
  and network diagnosis, warm VMware migrations with Forklift (waves, cutover,
  rollback), Harvester upgrades, Cluster API clusters, Terraform declarations,
  bare-metal installs through Redfish, monitoring and logging setup.
- Sign-in is mandatory; roles viewer, operator, admin; optional Rancher SSO.

## Capabilities and Constraints

- Feature reference: `docs/en/capabilities.md` (and `docs/fr/capabilites.md`);
  parity with the Harvester 1.9 interface: `docs/en/harvester-parity.md`.
- Every menu offers at least the functions of the Harvester interface, with a
  modern experience (windows, menus), never a raw console.
- Stack is fixed: Flask 3, vanilla JavaScript modules (IIFE, no framework),
  SQLite in WAL mode, SSE. No runtime network dependency for the client.
- Five complete languages (English, French, German, Spanish, Italian): every
  new string ships in all five.
- Every button, icon and control has a tooltip (global on/off toggle).
- The interface stays homogeneous: reuse existing patterns (`.btn`, `.card`,
  `.sub-tabs-inline`, `.modal-overlay`, theme tokens) before adding new ones.
- Public content (docs, site, commits) carries no em dash and no Unicode arrow.
- Presentation site and live demo: `site/`, `tools/demo-site/` (see
  `docs/en/site.md`); the public site was withdrawn on 2026-10-08 and is only
  built for private review.

## Brand Commitments

- Name: harvester-ops (harvops). Independent open source project under the
  Apache License 2.0, not affiliated with, endorsed by, or supported by SUSE;
  SUSE, Harvester, Rancher and VMware are named as trademarks of their owners.
- The console ships five colour themes in dark and light, the SUSE theme being
  the default; icons are the Lucide set.

## Evidence on Hand

- Real tests on Harvester clusters before each release (physical and nested
  test clusters, a VMware ESXi and vCenter bench), recorded in `CHANGELOG.md`.
- Load measurements with simulated clusters (kwok): `docs/en/sizing.md`.
- Captioned demo videos in English and French (README), the parity table, the
  live demo on anonymized data.
- No customer, testimonial, logo, deployment count or benchmark against other
  products exists: future work must not cite or invent any until a real,
  authorized one exists.

## Product Principles

1. The fleet first: a view or gesture that only works one cluster at a time
   needs a reason.
2. Nothing happens silently: every change is a tracked action, and what the
   cluster refused is said instead of shown as an empty view.
3. Command line and interface are equal; automation is a script before it is a
   button.
4. Production-grade and airgap-ready: tested for real, signed, no runtime
   download.
5. Least privilege: the cluster sees the person, not the console.

## Accessibility & Inclusion

Keyboard-visible focus, focus kept inside dialogs, tooltips readable by
assistive technology, reduced motion respected; five languages.
