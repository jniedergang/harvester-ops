#!/usr/bin/env python3
"""harvester-resources : les objets d'un cluster Harvester que la console range
sous Cluster (Storage, Network, Add-ons, Security) et dans la fenêtre Backups.

Toute écriture de la console sur ces objets passe par ce script (parité CLI) ;
il s'utilise aussi seul.

  harvester-resources addon --cluster harv1 --namespace kube-system --name descheduler --enable
  harvester-resources addon --cluster harv1 --namespace kube-system --name descheduler --disable

  harvester-resources backup create  --cluster harv1 --namespace default --vm web [--type snapshot] [--name N] [--freeze 5s]
  harvester-resources backup restore --cluster harv1 --namespace default --name B (--new-vm NAME [--keep-mac] | --replace [--delete-policy delete]) [--halt]
  harvester-resources backup delete  --cluster harv1 --namespace default --name B
  harvester-resources schedule create --cluster harv1 --namespace default --name S --vm web --cron "0 2 * * *" --retain 7 --max-failure 3 [--type snapshot]
  harvester-resources schedule update --cluster harv1 --namespace default --name S [--cron "0 3 * * *"] --retain 10 --max-failure 4
  harvester-resources schedule suspend|resume|delete --cluster harv1 --namespace default --name S
  harvester-resources volsnap restore --cluster harv1 --namespace default --name SNAP --new-volume NAME
  harvester-resources volsnap delete  --cluster harv1 --namespace default --name SNAP

  harvester-resources create --cluster harv1 --kind image|storageclass|sshkey|secret|network|volume --spec request.json
  harvester-resources delete --cluster harv1 --kind KIND [--namespace default] --name NAME
  harvester-resources sc-default --cluster harv1 --name harv-rep1
  harvester-resources volume-expand --cluster harv1 --namespace default --name web-root --size 40Gi
  harvester-resources addon-values --cluster harv1 --namespace NS --name ADDON --values values.yaml
  harvester-resources yaml --cluster harv1 --kind vm --namespace default --name web --file web.yaml [--dry-run]
  harvester-resources yaml --cluster harv1 --kind image --create --file image.yaml

  harvester-resources vm pause|unpause|softreboot|restart|force-stop --cluster harv1 --namespace default --name web
  harvester-resources vm delete --cluster harv1 --namespace default --name web [--remove-volumes web-root,web-data]
  harvester-resources vm clone --cluster harv1 --namespace default --name web --new-name web2 [--with-data] [--start]
  harvester-resources vm eject --cluster harv1 --namespace default --name web --volume cdrom [--delete-volume]
  harvester-resources vm add-volume --cluster harv1 --namespace default --name web --claim data [--bus scsi]
  harvester-resources vm remove-volume --cluster harv1 --namespace default --name web --volume data
  harvester-resources vm migrate --cluster harv1 --namespace default --name web [--node n2]
  harvester-resources vm abort-migration --cluster harv1 --namespace default --name web
  harvester-resources vm template --cluster harv1 --namespace default --name web --template-name web-tpl [--with-data]
  harvester-resources vm cloudinit --cluster harv1 --namespace default --name web --user-data u.yaml [--guest-agent]
  harvester-resources vm insert-cdrom --cluster harv1 --namespace default --name web --volume cd --image default/iso
  harvester-resources vm eject-image --cluster harv1 --namespace default --name web --volume cd
  harvester-resources vm add-nic --cluster harv1 --namespace default --name web --iface nic2 --network default/vlan20 [--mac ..]
  harvester-resources vm remove-nic --cluster harv1 --namespace default --name web --iface nic2
  harvester-resources vm cpumem --cluster harv1 --namespace default --name web --cpu 4 --memory 8Gi
  harvester-resources vm storage-migrate --cluster harv1 --namespace default --name web --volume web-root --target web-root-fast
  harvester-resources vm cancel-storage-migration --cluster harv1 --namespace default --name web
  harvester-resources vm quota --cluster harv1 --namespace default --name web --size 20Gi
  harvester-resources vm access --cluster harv1 --namespace default --name web --kind basic --users ops --password-file pw
  harvester-resources vm access --cluster harv1 --namespace default --name web --kind ssh --users ops --keys default/k1

  harvester-resources host basics --cluster harv1 --node n1 [--custom-name "rack 2"] [--console-url https://10.0.0.21] [--labels-file l.json]
  harvester-resources host tags --cluster harv1 --node n1 --tags fast,ssd
  harvester-resources host disk-add --cluster harv1 --node n1 --disk BLOCKDEVICE [--provisioner LonghornV1|LonghornV2|lvm] [--vg VG] [--format|--no-format] [--tag ssd]
  harvester-resources pools-apply --kubeconfig new.yaml --node n1 --spec pools.json [--no-classes] [--timeout 1800]
  harvester-resources host disk-remove --cluster harv1 --node n1 --disk BLOCKDEVICE
  harvester-resources host disk-set --cluster harv1 --node n1 --disk BLOCKDEVICE [--tags a,b] [--scheduling on|off]
  harvester-resources host hugepages --cluster harv1 --node n1 [--thp-enabled madvise] [--thp-shmem never] [--thp-defrag madvise]
  harvester-resources host ksmtuned --cluster harv1 --node n1 [--run run] [--mode standard|high|customized] [--thres 20] [--merge on|off] [--params p.json]
  harvester-resources host cpu-manager --cluster harv1 --node n1 --enable|--disable
  harvester-resources host oob --cluster harv1 --node n1 --bmc-host 10.0.0.21 [--bmc-port 623] --username admin --password-file pw [--insecure] [--interval 1h]
  harvester-resources host oob --cluster harv1 --node n1 --off
  harvester-resources host power --cluster harv1 --node n1 --operation shutdown|poweron|reboot
  harvester-resources host delete --cluster harv1 --node n3

  harvester-resources namespace create --cluster harv1 --name team-a [--description ..] [--labels-file l.json]
  harvester-resources namespace update --cluster harv1 --name team-a [--description ..] [--labels-file l.json] [--annotations-file a.json]
  harvester-resources namespace quota  --cluster harv1 --name team-a --size 100Gi   (0 removes it)
  harvester-resources namespace delete --cluster harv1 --name team-a

  harvester-resources volume clone --cluster harv1 --namespace default --name data --new-name data2 [--no-data]
  harvester-resources volume export --cluster harv1 --namespace default --name data --display-name data-img [--target-namespace ns] [--storage-class sc]
  harvester-resources volume snapshot --cluster harv1 --namespace default --name data --snapshot-name data-s1
  harvester-resources volume copy --cluster harv1 --namespace default --name data --new-name data-fast --storage-class fast
  harvester-resources volume cancel-expand --cluster harv1 --namespace default --name data
  harvester-resources volume describe --cluster harv1 --namespace default --name data --description "..."
  harvester-resources image edit --cluster harv1 --namespace default --name image-x [--description ..] [--labels-file l.json]
  harvester-resources image clone --cluster harv1 --namespace default --name image-x --display-name copy
  harvester-resources image encrypt|decrypt --cluster harv1 --namespace default --name image-x --display-name enc --storage-class enc-sc
  harvester-resources image download --cluster harv1 --namespace default --name image-x --out image.gz
  harvester-resources image upload --cluster harv1 --namespace default --file disk.qcow2 --display-name disk [--storage-class sc] [--checksum SHA512] [--port 8092]
  harvester-resources image download --cluster harv1 --namespace default --name image-x --out f.qcow2   (CDI images: through a downloader)
  harvester-resources image prepare-download --cluster harv1 --namespace default --name image-x   (CDI images: the qcow2 made ready)

  harvester-resources template set-default|delete-version --cluster harv1 --namespace default --name web --version default/web-2
  harvester-resources template delete --cluster harv1 --namespace default --name web
  harvester-resources cloudtpl create|update|delete --cluster harv1 --namespace default --name base [--type user|network] [--file t.yaml] [--description ..]
  harvester-resources storageclass --cluster harv1 --spec sc.json        (Longhorn v1/v2, encryption, LVM, topologies)
  harvester-resources secret create|update --cluster harv1 --namespace default --name reg --spec s.json
  harvester-resources sshkey update --cluster harv1 --namespace default --name ops --public-key-file k.pub [--description ..]

  harvester-resources clusternetwork create|delete --cluster harv1 --name data [--description ..]
  harvester-resources netconfig create|update --cluster harv1 --spec vc.json          (cartes, bond, MTU, nœuds)
  harvester-resources netconfig migrate --cluster harv1 --name data-all --target data2
  harvester-resources netconfig delete --cluster harv1 --name data-all
  harvester-resources vmnet update --cluster harv1 --namespace default --name vlan20 --spec n.json   (VLAN, plages, route)
  harvester-resources lb create|update --cluster harv1 --spec lb.json ; lb delete --namespace default --name web
  harvester-resources ippool create|update --cluster harv1 --spec p.json ; ippool delete|release --name lan [--ip 10.0.0.7]
  harvester-resources hostnet create|update --cluster harv1 --spec h.json ; hostnet delete --name stor
  harvester-resources netsetting set --cluster harv1 --name storage-network|vm-migration-network|rwx-network --spec s.json
  harvester-resources netsetting clear --cluster harv1 --name storage-network

  harvester-resources setting set --cluster harv1 --name log-level --value-file v.txt     (valeur dans un fichier privé)
  harvester-resources setting reset --cluster harv1 --name log-level
  harvester-resources setting test-backup-target --cluster harv1
  harvester-resources supportbundle create --cluster harv1 --spec b.json ; supportbundle delete --name bundle-x
  harvester-resources kubeconfig create --cluster harv1 --name ci --role view [--namespace default] --duration 24h --out ci.yaml
  harvester-resources device pci-enable|pci-disable --cluster harvlab --name harvlab-n3-000005000 [--name ...]
  harvester-resources device usb-enable|usb-disable --cluster harvlab --name harvlab-n1-0627-0001-002002
  harvester-resources device sriov --cluster harvlab --name harvlab-n3-enp6s0 --vfs 2     (0 désactive)
  harvester-resources upgrade version-add --cluster harv1 --version-file version.yaml
  harvester-resources upgrade start --cluster harv1 --version v1.9.0 [--version-file version.yaml] [--no-log]
  harvester-resources upgrade start --cluster harv1 --iso harvester-v1.9.0-amd64.iso --checksum SHA512   (airgap)
  harvester-resources upgrade follow|logs|dismiss|abort --cluster harv1 --name hvst-upgrade-xxxxx [--out logs.zip]
  harvester-resources monlog output-apply|flow-apply|amc-apply --cluster harv1 --spec request.json
  harvester-resources vmimport source-apply|import-create --cluster harv1 --spec request.json
  harvester-resources vmimport import-follow|import-delete --cluster harv1 --namespace ns --name imp
  harvester-resources project create|update --kubeconfig rancher-session.yaml --spec project.json [--id p-xxxxx]
  harvester-resources project move|ns-quota --kubeconfig rancher-session.yaml --namespace ns [--id p-xxxxx] [--spec quota.json]
  harvester-resources member add|remove --kubeconfig rancher-session.yaml [--scope project --project p-xxxxx] --principal ID --role ROLE | --id BINDING
  harvester-resources monlog output-delete|flow-delete|amc-delete --cluster harv1 --kind Flow --namespace ns --name n
  harvester-resources kubeconfig revoke --cluster harv1 --name ci

Sorties : 0 fait, 1 échec, 2 refusé par le contrôle, 3 annulé. Les étapes
s'écrivent sur stderr en `STEP_EVENT|étape|statut|message`, que la console
relaie au dock.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from kube import Kube, KubeError, cluster_config  # noqa: E402
import hv_backups as hb  # noqa: E402
import hv_objects as ho  # noqa: E402
import hv_yaml as hy  # noqa: E402
import hv_vm as hv  # noqa: E402
import hv_host as hh  # noqa: E402
import hv_ns as hn  # noqa: E402
import hv_storage as hs  # noqa: E402
import hv_advanced as hadv  # noqa: E402
import hv_net as hnet  # noqa: E402
import hv_settings as hset  # noqa: E402
import hv_devices as hdev  # noqa: E402
import hv_upgrade as hup  # noqa: E402
import hv_monlog as hml  # noqa: E402
import hv_vmimport as hvi  # noqa: E402
import hv_projects as hpj  # noqa: E402
import hv_members as hmb  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_BLOCKED, EXIT_CANCELLED = 0, 1, 2, 3
K_ADDON = "addons.harvesterhci.io"


class Cancelled(Exception):
    pass


def step(sid, status, msg=""):
    clean = " ".join(str(msg).split())
    sys.stderr.write(f"STEP_EVENT|{sid}|{status}|{clean}\n")
    sys.stderr.flush()


def _on_signal(signum, frame):
    raise Cancelled(f"signal {signum}")


def refusal(msg):
    """Un refus d'un webhook de Harvester, dit sans l'enveloppe de kubectl :
    « Error from server (BadRequest): admission webhook "validator.harvesterhci.io"
    denied the request: descheduler addon cannot be enabled as not enough
    nodes exist in the cluster » (vu sur harv1, un seul nœud)."""
    text = str(msg)
    if "denied the request:" in text:
        return "Harvester refused: " + text.split("denied the request:", 1)[1].strip()
    return text


def kube_from(args):
    entry = cluster_config(args.cluster) if args.cluster else None
    kc = args.kubeconfig or (entry or {}).get("kubeconfig")
    if not kc:
        raise SystemExit("give --cluster (with a kubeconfig in the configuration) or --kubeconfig")
    return Kube(kc)


# ---------------------------------------------------------------------------
# Add-ons (addons.harvesterhci.io)
#
# Activer un add-on installe son chart (Harvester le déploie par un HelmChart),
# le désactiver le retire. Vu sur harv1 (Harvester v1.9.0) : le statut passe
# par AddonEnabling / AddonDisabling puis AddonDeploySuccessful / AddonDisabled,
# et un échec se lit dans AddonDeployFailed ou la condition OperationFailed.
# ---------------------------------------------------------------------------

def addon_state(obj):
    """(statut, message d'échec éventuel) d'un Addon."""
    st = (obj or {}).get("status") or {}
    failed = ""
    for c in st.get("conditions") or []:
        if c.get("type") == "OperationFailed" and str(c.get("status")) == "True":
            failed = c.get("message") or c.get("reason") or "operation failed"
    return st.get("status") or "", failed


def addon_settled(status, failed, want):
    """True si l'add-on a atteint l'état voulu, False s'il a échoué, None
    s'il est encore en chemin."""
    if "Failed" in status or failed:
        return False
    if want and status in ("AddonDeploySuccessful", "AddonUpdateSuccessful", "AddonDeployed"):
        return True
    if not want and status == "AddonDisabled":
        return True
    return None


def set_addon(kube, ns, name, want, timeout=900, sleep=time.sleep, now=time.time):
    obj = kube.get(K_ADDON, ns, name)
    if obj is None:
        raise ValueError(f"no add-on {ns}/{name} on this cluster")
    label = "enable" if want else "disable"
    status, failed = addon_state(obj)
    if bool((obj.get("spec") or {}).get("enabled")) == want and addon_settled(status, "", want):
        step("patch", "done", f"{ns}/{name} is already {'enabled' if want else 'disabled'}")
        step("wait", "done", status)
        return EXIT_OK
    step("patch", "running", f"{label} {ns}/{name}")
    kube.patch(K_ADDON, ns, name, {"spec": {"enabled": want}})
    step("patch", "done", f"spec.enabled = {str(want).lower()}")
    step("wait", "running", "Harvester applies the change")
    deadline = now() + timeout
    last = None
    # Le contrôleur met quelques secondes à prendre la main : un statut encore
    # « succès » de l'état précédent ne doit pas être lu comme la fin.
    sleep(3)
    while now() < deadline:
        obj = kube.get(K_ADDON, ns, name) or {}
        status, failed = addon_state(obj)
        if status != last:
            step("wait", "running", status or "pending")
            last = status
        done = addon_settled(status, failed, want)
        if done is True and bool((obj.get("spec") or {}).get("enabled")) == want:
            step("wait", "done", status)
            return EXIT_OK
        if done is False:
            step("wait", "error", failed or status)
            return EXIT_FAIL
        sleep(5)
    step("wait", "error", f"still {last or 'pending'} after {timeout} s")
    return EXIT_FAIL


def cmd_addon(args):
    kube = kube_from(args)
    return set_addon(kube, args.namespace, args.name, args.enable, timeout=args.timeout)


# ---------------------------------------------------------------------------
# v1.58.0 : sauvegardes, instantanés, planifications, instantanés de volumes
# ---------------------------------------------------------------------------

def _wait(kube, kind, ns, name, done, timeout, sleep=time.sleep, now=time.time, label="wait"):
    """Relit l'objet jusqu'à `done(obj)` (True fini, False échec, None en cours)."""
    deadline = now() + timeout
    last = None
    while now() < deadline:
        obj = kube.get(kind, ns, name)
        res, msg = done(obj)
        if msg and msg != last:
            step(label, "running", msg)
            last = msg
        if res is True:
            step(label, "done", msg or "done")
            return EXIT_OK
        if res is False:
            step(label, "error", msg or "failed")
            return EXIT_FAIL
        sleep(5)
    step(label, "error", f"not finished after {timeout} s")
    return EXIT_FAIL


def _backup_done(obj):
    if obj is None:
        return None, "waiting for the backup object"
    st = obj.get("status") or {}
    err = (st.get("error") or {}).get("message")
    if err:
        return False, err
    if st.get("readyToUse"):
        return True, "ready"
    prog = st.get("progress")
    return None, f"{prog} %" if prog is not None else "in progress"


def cmd_backup(args):
    kube = kube_from(args)
    ns = args.namespace
    if args.action == "create":
        if not args.vm:
            raise ValueError("--vm is required")
        name = args.name or f"{args.vm}-{time.strftime('%Y%m%d-%H%M%S')}"
        if args.type == "backup":
            target = kube.get("settings.harvesterhci.io", None, "backup-target")
            if not (target or {}).get("value") or '"endpoint":""' in (target or {}).get("value", "").replace(" ", ""):
                raise ValueError("no backup target is set on this cluster (Harvester setting backup-target)")
        supports = hb.crd_has_spec_field(kube.get("customresourcedefinitions.apiextensions.k8s.io", None, hb.K_BACKUP),
                                         "fsFreezeDeadline")
        if args.freeze and not supports:
            step("create", "running", "this Harvester has no freeze deadline (1.9 and later): its default applies")
        step("create", "running", f"{args.type} of {ns}/{args.vm}: {name}")
        kube.create(hb.backup_manifest(ns, args.vm, name, args.type, freeze=args.freeze, supports_freeze=supports))
        step("create", "done", name)
        return _wait(kube, hb.K_BACKUP, ns, name, _backup_done, args.timeout)
    if args.action == "delete":
        step("delete", "running", f"{ns}/{args.name}")
        kube.delete(hb.K_BACKUP, ns, hb.check_name(args.name, "backup"))
        return _wait(kube, hb.K_BACKUP, ns, args.name,
                     lambda o: (True, "deleted") if o is None else (None, "deleting"), args.timeout, label="delete")
    # restore
    b = kube.get(hb.K_BACKUP, ns, hb.check_name(args.name, "backup"))
    if b is None:
        raise ValueError(f"no backup or snapshot {ns}/{args.name}")
    if not (b.get("status") or {}).get("readyToUse"):
        raise ValueError(f"{args.name} is not ready yet")
    source = ((b.get("spec") or {}).get("source") or {}).get("name")
    if args.replace:
        vm = kube.get("virtualmachines.kubevirt.io", ns, source)
        if vm is None:
            raise ValueError(f"the original VM {ns}/{source} no longer exists: restore into a new VM")
        if kube.get("virtualmachineinstances.kubevirt.io", ns, source) is not None:
            raise ValueError(f"stop {ns}/{source} first: Harvester only replaces a stopped VM")
        target, new_vm = source, False
    else:
        if not args.new_vm:
            raise ValueError("give --new-vm NAME or --replace")
        if kube.get("virtualmachines.kubevirt.io", ns, args.new_vm) is not None:
            raise ValueError(f"a VM {ns}/{args.new_vm} already exists")
        target, new_vm = args.new_vm, True
    crd = kube.get("customresourcedefinitions.apiextensions.k8s.io", None, hb.K_RESTORE)
    import vm_transfer as vt
    man = hb.restore_manifest(ns, args.name, target, new_vm, keep_mac=args.keep_mac, halt=args.halt,
                              delete_policy=args.delete_policy,
                              name=f"restore-{target}-{time.strftime('%Y%m%d%H%M%S')}"[:63],
                              supports_halt=vt.restore_supports_halt(crd),
                              from_snapshot=((b.get("spec") or {}).get("type") == "snapshot"))
    step("restore", "running", f"{args.name} into {'new VM ' if new_vm else ''}{ns}/{target}")
    kube.create(man)
    rname = man["metadata"]["name"]

    def restored(obj):
        if obj is None:
            return None, "waiting for the restore object"
        st = obj.get("status") or {}
        for c in st.get("conditions") or []:
            if c.get("type") == "Failure" and str(c.get("status")) == "True":
                return False, c.get("message") or c.get("reason") or "restore failed"
        if st.get("complete"):
            return True, f"{ns}/{target} restored"
        prog = [c.get("message") for c in st.get("conditions") or [] if c.get("type") == "Progressing"]
        return None, prog[0] if prog and prog[0] else "restoring"
    return _wait(kube, hb.K_RESTORE, ns, rname, restored, args.timeout, label="restore")


def cmd_schedule(args):
    kube = kube_from(args)
    ns, name = args.namespace, hb.check_name(args.name)
    if args.action == "create":
        retain = args.retain if args.retain is not None else 7
        man = hb.schedule_manifest(ns, name, args.vm, args.cron, retain,
                                   args.max_failure if args.max_failure is not None else 3, args.type)
        if args.type == "backup":
            target = kube.get("settings.harvesterhci.io", None, "backup-target")
            if not (target or {}).get("value"):
                raise ValueError("no backup target is set on this cluster: schedule snapshots, or set one")
        step("create", "running", f"{args.type} of {ns}/{args.vm}, {man['spec']['cron']}, keep {retain}")
        kube.create(man)
        step("create", "done", name)
        return EXIT_OK
    if args.action == "update":
        cur = kube.get(hb.K_SCHEDULE, ns, name)
        if cur is None:
            raise ValueError(f"no schedule {ns}/{name}")
        sp = cur.get("spec") or {}
        patch = hb.schedule_patch(args.cron or sp.get("cron"),
                                  args.retain if args.retain is not None else sp.get("retain"),
                                  args.max_failure if args.max_failure is not None else sp.get("maxFailure"))
        kube.patch(hb.K_SCHEDULE, ns, name, patch)
        cron = patch["spec"]["cron"]
        step("update", "running", f"{ns}/{name}: {cron}, keep {patch['spec']['retain']}, "
                                  f"stop after {patch['spec']['maxFailure']} failures")
        # Harvester recopie le cron dans le CronJob déclencheur svmb-<uid>
        job = f"svmb-{(cur.get('metadata') or {}).get('uid')}"
        return _wait(kube, "cronjobs.batch", "harvester-system", job,
                     lambda o: (True, "schedule updated") if ((o or {}).get("spec") or {}).get("schedule") == cron
                     else (None, "waiting for Harvester to reschedule"), 120, label="update")
    if args.action in ("suspend", "resume"):
        want = args.action == "suspend"
        step(args.action, "running", f"{ns}/{name}")
        kube.patch(hb.K_SCHEDULE, ns, name, {"spec": {"suspend": want}})
        step(args.action, "done", "suspended" if want else "resumed")
        return EXIT_OK
    step("delete", "running", f"{ns}/{name} (its backups stay)")
    kube.delete(hb.K_SCHEDULE, ns, name)
    step("delete", "done", name)
    return EXIT_OK


def cmd_volsnap(args):
    kube = kube_from(args)
    ns, name = args.namespace, hb.check_name(args.name, "snapshot")
    snap = kube.get(hb.K_VOLSNAP, ns, name)
    if snap is None:
        raise ValueError(f"no volume snapshot {ns}/{name}")
    if args.action == "delete":
        owner = next((o.get("name") for o in (snap.get("metadata") or {}).get("ownerReferences") or []
                      if o.get("kind") == "VirtualMachineBackup"), None)
        if owner:
            raise ValueError(f"this snapshot belongs to the VM snapshot {owner}: delete that one instead")
        step("delete", "running", f"{ns}/{name}")
        kube.delete(hb.K_VOLSNAP, ns, name)
        step("delete", "done", name)
        return EXIT_OK
    # restore
    st = snap.get("status") or {}
    if not st.get("readyToUse"):
        raise ValueError(f"{name} is not ready yet")
    new = hb.check_name(args.new_volume, "new volume")
    if kube.get("persistentvolumeclaims", ns, new) is not None:
        raise ValueError(f"a volume {ns}/{new} already exists")
    src_pvc = kube.get("persistentvolumeclaims", ns, ((snap.get("spec") or {}).get("source") or {})
                       .get("persistentVolumeClaimName") or "")
    sc = args.storage_class or ((src_pvc or {}).get("spec") or {}).get("storageClassName")
    size = args.size or st.get("restoreSize")
    step("restore", "running", f"{ns}/{name} into a new volume {new} ({size})")
    kube.create(hb.volume_restore_manifest(ns, name, new, size, sc))

    def bound(obj):
        phase = ((obj or {}).get("status") or {}).get("phase")
        return (True, f"{ns}/{new} bound") if phase == "Bound" else (None, phase or "pending")
    return _wait(kube, "persistentvolumeclaims", ns, new, bound, args.timeout, label="restore")


# ---------------------------------------------------------------------------
# v1.59.0 : créer et supprimer les objets des sections
# ---------------------------------------------------------------------------

def _read_json(path):
    import json
    raw = sys.stdin.read() if path == "-" else Path(path).read_text()
    return json.loads(raw)


def _default_class(kube):
    for sc in kube.list(ho.K["storageclass"]):
        ann = (sc.get("metadata") or {}).get("annotations") or {}
        if ann.get("storageclass.kubernetes.io/is-default-class") == "true":
            return (sc.get("metadata") or {}).get("name")
    return None


def cmd_create(args):
    kube = kube_from(args)
    spec = _read_json(args.spec)
    kind = args.kind
    image = None
    if kind == "volume" and spec.get("image"):
        ref = str(spec["image"])
        ins, iname = ref.split("/", 1) if "/" in ref else (spec.get("namespace") or "default", ref)
        image = kube.get(ho.K["image"], ins, iname)
        if image is None:
            raise ValueError(f"no image {ins}/{iname}")
    default_class = _default_class(kube) if kind == "image" else None
    sc_obj = None
    if kind == "image":
        # v1.74.0 : le backend de l'image suit sa classe (cdi hors Longhorn v1)
        sc_name = str(spec.get("storage_class") or default_class or "").strip()
        sc_obj = kube.get(ho.K["storageclass"], None, sc_name) if sc_name else None
    man = ho.normalize(kind, spec, default_class=default_class, image=image, sc_obj=sc_obj)
    meta = man["metadata"]
    ns = meta.get("namespace")
    if meta.get("name") and kube.get(ho.K[kind], ns, meta["name"]) is not None:
        raise ValueError(f"{kind} {ns + '/' if ns else ''}{meta['name']} already exists")
    step("create", "running", f"{kind} {ns + '/' if ns else ''}{meta.get('name') or meta.get('generateName', '') + '…'}")
    made = kube.create(man)
    name = (made.get("metadata") or {}).get("name") or meta.get("name")
    step("create", "done", name)
    if kind == "image":
        def imported(obj):
            st = (obj or {}).get("status") or {}
            for c in st.get("conditions") or []:
                if c.get("type") == "RetryLimitExceeded" and str(c.get("status")) == "True":
                    return False, c.get("message") or "download failed"
            prog = st.get("progress")
            if prog == 100 and any(c.get("type") == "Imported" and str(c.get("status")) == "True"
                                   for c in st.get("conditions") or []):
                return True, f"{ns}/{name} imported"
            return None, f"{prog or 0} %"
        return _wait(kube, ho.K["image"], ns, name, imported, args.timeout, label="import")
    if kind == "sshkey":
        def valid(obj):
            conds = ((obj or {}).get("status") or {}).get("conditions") or []
            if any(c.get("type") == "validated" and str(c.get("status")) == "True" for c in conds):
                return True, "key validated"
            bad = [c for c in conds if c.get("type") == "validated" and str(c.get("status")) == "False"]
            return (False, bad[0].get("message") or "invalid key") if bad else (None, "validating")
        return _wait(kube, ho.K["sshkey"], ns, name, valid, 120, label="validate")
    if kind == "volume":
        def bound(obj):
            phase = ((obj or {}).get("status") or {}).get("phase")
            return (True, f"{ns}/{name} bound") if phase == "Bound" else (None, phase or "pending")
        return _wait(kube, ho.K["volume"], ns, name, bound, args.timeout, label="bind")
    return EXIT_OK


def cmd_delete(args):
    kube = kube_from(args)
    kind, ns, name = args.kind, args.namespace, args.name
    if kind in ho.NAMESPACED and not ns:
        raise ValueError("--namespace is required")
    obj = kube.get(ho.K[kind], ns if kind in ho.NAMESPACED else None, name)
    if obj is None:
        raise ValueError(f"no {kind} {ns + '/' if ns else ''}{name}")
    vms = kube.list("virtualmachines.kubevirt.io") if kind in ("network", "secret", "volume") else []
    pvcs = kube.list("persistentvolumeclaims") if kind in ("image", "storageclass") else []
    images = kube.list(ho.K["image"]) if kind == "storageclass" else []
    users = ho.vms_using(kind, ns if kind in ho.NAMESPACED else None, name, vms, pvcs, images)
    if kind == "volume":
        pods = [p for p in kube.list("pods", ns)
                if any((v.get("persistentVolumeClaim") or {}).get("claimName") == name
                       for v in (p.get("spec") or {}).get("volumes") or [])]
        users += [f"pod {ns}/{(p.get('metadata') or {}).get('name')}" for p in pods
                  if not ((p.get("metadata") or {}).get("name") or "").startswith("virt-launcher-")]
    if users and kind != "sshkey":
        raise ValueError(f"still used by {', '.join(users[:6])}{' …' if len(users) > 6 else ''}")
    if users:
        step("check", "done", f"the VMs {', '.join(users[:6])} keep the key they received")
    step("delete", "running", f"{kind} {ns + '/' if ns else ''}{name}")
    kube.delete(ho.K[kind], ns if kind in ho.NAMESPACED else None, name)
    return _wait(kube, ho.K[kind], ns if kind in ho.NAMESPACED else None, name,
                 lambda o: (True, "deleted") if o is None else (None, "deleting"), args.timeout, label="delete")


def cmd_sc_default(args):
    """Changer la classe par défaut. Harvester refuse d'en poser une seconde
    (« default storage class harv-rep1 already exists, please reset it first »,
    vu sur harv1) : l'ancienne est retirée d'abord, puis la nouvelle posée."""
    kube = kube_from(args)
    target = ho._name(args.name, "storage class")
    classes = kube.list(ho.K["storageclass"])
    if not any((c.get("metadata") or {}).get("name") == target for c in classes):
        raise ValueError(f"no storage class {target}")
    off = {"storageclass.kubernetes.io/is-default-class": "false",
           "storageclass.beta.kubernetes.io/is-default-class": "false"}
    on = {"storageclass.kubernetes.io/is-default-class": "true",
          "storageclass.beta.kubernetes.io/is-default-class": "true"}
    previous = []
    for c in classes:
        n = (c.get("metadata") or {}).get("name")
        ann = (c.get("metadata") or {}).get("annotations") or {}
        if n != target and "true" in (ann.get("storageclass.kubernetes.io/is-default-class"),
                                      ann.get("storageclass.beta.kubernetes.io/is-default-class")):
            step("default", "running", f"{n} is no longer the default")
            kube.patch(ho.K["storageclass"], None, n, {"metadata": {"annotations": off}})
            previous.append(n)
    step("default", "running", f"{target} becomes the default class")
    try:
        kube.patch(ho.K["storageclass"], None, target, {"metadata": {"annotations": on}})
    except KubeError:
        # remettre l'ancienne : ne jamais laisser le cluster sans classe par défaut
        for n in previous:
            kube.patch(ho.K["storageclass"], None, n, {"metadata": {"annotations": on}})
        raise
    step("default", "done", target)
    return EXIT_OK


def cmd_volume_expand(args):
    kube = kube_from(args)
    ns, name = args.namespace, args.name
    new = ho._size(args.size)
    pvc = kube.get(ho.K["volume"], ns, name)
    if pvc is None:
        raise ValueError(f"no volume {ns}/{name}")
    cur = ((pvc.get("spec") or {}).get("resources") or {}).get("requests", {}).get("storage")
    if ho.size_bytes(new) is None or (ho.size_bytes(cur) and ho.size_bytes(new) <= ho.size_bytes(cur)):
        raise ValueError(f"a volume only grows: give more than {cur}")
    sc = kube.get(ho.K["storageclass"], None, (pvc.get("spec") or {}).get("storageClassName") or "")
    if sc is not None and not sc.get("allowVolumeExpansion"):
        raise ValueError("its storage class does not allow expansion")
    step("expand", "running", f"{ns}/{name}: {cur} to {new}")
    kube.patch(ho.K["volume"], ns, name, {"spec": {"resources": {"requests": {"storage": new}}}})

    def grown(obj):
        cap = ((obj or {}).get("status") or {}).get("capacity", {}).get("storage")
        if cap and ho.size_bytes(cap) and ho.size_bytes(cap) >= ho.size_bytes(new):
            return True, f"{ns}/{name} is {cap}"
        conds = ((obj or {}).get("status") or {}).get("conditions") or []
        wait = [c.get("type") for c in conds]
        if "FileSystemResizePending" in wait or "Resizing" in wait:
            return None, "resizing (a running VM sees it after a restart)"
        return None, f"requested {new}, capacity {cap}"
    return _wait(kube, ho.K["volume"], ns, name, grown, args.timeout, label="expand")


def cmd_addon_values(args):
    kube = kube_from(args)
    text = ho.check_values(Path(args.values).read_text() if args.values != "-" else sys.stdin.read())
    obj = kube.get(K_ADDON, args.namespace, args.name)
    if obj is None:
        raise ValueError(f"no add-on {args.namespace}/{args.name} on this cluster")
    step("values", "running", f"new configuration for {args.namespace}/{args.name}")
    kube.patch(K_ADDON, args.namespace, args.name, {"spec": {"valuesContent": text}})
    step("values", "done", "saved")
    if not (obj.get("spec") or {}).get("enabled"):
        step("wait", "done", "the add-on is disabled: the configuration applies when it is enabled")
        return EXIT_OK
    time.sleep(3)
    return set_addon(kube, args.namespace, args.name, True, timeout=args.timeout)


# ---------------------------------------------------------------------------
# v1.60.0 : modifier ou créer un objet par son YAML (« Edit YAML » de Harvester)
# ---------------------------------------------------------------------------

def _load_doc(kube, path):
    """Le fichier (YAML ou JSON) en objet. JSON d'abord ; puis PyYAML s'il est
    là ; sinon kubectl lui-même fait la conversion (hôte airgap sans PyYAML)."""
    import json
    raw = sys.stdin.read() if path == "-" else Path(path).read_text()
    try:
        return json.loads(raw)
    except ValueError:
        pass
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml is not None:
        try:
            docs = [d for d in yaml.safe_load_all(raw) if d is not None]
        except yaml.YAMLError as e:
            raise ValueError(f"not valid YAML: {str(e).splitlines()[0]}") from None
        if len(docs) != 1:
            raise ValueError("one object at a time: the file holds " + str(len(docs)))
        return docs[0]
    out = kube.run("create", "--dry-run=client", "-o", "json", "-f", "-", input=raw)
    return json.loads(out)


def cmd_yaml(args):
    kube = kube_from(args)
    s = hy.spec_of(args.kind)
    obj = _load_doc(kube, args.file)
    creating = bool(args.create)
    if not creating and not args.name:
        raise ValueError("--name is required to replace an object (or give --create)")
    if s["namespaced"] and not creating and not args.namespace:
        raise ValueError("--namespace is required")
    obj = hy.check_target(args.kind, obj, args.namespace, args.name, creating=creating)
    meta = obj["metadata"]
    ref = f"{meta.get('namespace') + '/' if meta.get('namespace') else ''}{meta.get('name') or meta.get('generateName', '') + '…'}"
    verb = "create" if creating else "replace"
    step("check", "running", f"Harvester checks the {s['kind']} {ref}")
    import json
    body = json.dumps(obj)

    def send(*extra):
        # Le resourceVersion du texte garde contre l'écrasement : un conflit
        # peut sortir dès l'essai à blanc (vu sur harv1), il est dit en clair.
        try:
            return kube.run(verb, *extra, "-f", "-", "-o", "json", input=body)
        except KubeError as e:
            if "the object has been modified" in str(e):
                raise ValueError("someone changed this object since it was opened: "
                                 "reload it and apply your change again") from None
            raise
    send("--dry-run=server")
    step("check", "done", "accepted by the cluster's checks")
    if args.dry_run:
        return EXIT_OK
    step(verb, "running", f"{'create' if creating else 'save'} {s['kind']} {ref}")
    made = json.loads(send())
    step(verb, "done", f"{ref} {'created' if creating else 'saved'} "
                       f"(version {(made.get('metadata') or {}).get('resourceVersion', '?')})")
    return EXIT_OK


# ---------------------------------------------------------------------------
# v1.60.0 : les gestes sur une VM du menu de Harvester
# ---------------------------------------------------------------------------
K_VM = "virtualmachines.kubevirt.io"
K_VMI = "virtualmachineinstances.kubevirt.io"
K_VMIM = "virtualmachineinstancemigrations.kubevirt.io"
K_TPL = "virtualmachinetemplates.harvesterhci.io"
K_TPLV = "virtualmachinetemplateversions.harvesterhci.io"


def _put_sub(kube, path, body=None):
    import json
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(body or {}, f)
        tmp = f.name
    try:
        kube.run("replace", "--raw", path, "-f", tmp)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _vmi_state(kube, ns, name):
    vmi = kube.get(K_VMI, ns, name)
    if vmi is None:
        return None, set()
    conds = {c.get("type") for c in (vmi.get("status") or {}).get("conditions") or []
             if str(c.get("status")) == "True"}
    return vmi, conds


def vm_pause(kube, args, pause=True):
    ns, name = args.namespace, args.name
    vmi, conds = _vmi_state(kube, ns, name)
    if vmi is None:
        raise ValueError(f"{ns}/{name} is not running")
    if pause and "Paused" in conds:
        step("pause", "done", f"{ns}/{name} is already paused")
        return EXIT_OK
    if not pause and "Paused" not in conds:
        step("unpause", "done", f"{ns}/{name} is not paused")
        return EXIT_OK
    verb = "pause" if pause else "unpause"
    step(verb, "running", f"{ns}/{name}")
    _put_sub(kube, hv.subresource(ns, name, verb))

    def settled(obj):
        c = {x.get("type") for x in ((obj or {}).get("status") or {}).get("conditions") or []
             if str(x.get("status")) == "True"}
        if ("Paused" in c) == pause:
            return True, f"{ns}/{name} {'paused' if pause else 'running again'}"
        return None, "waiting for KubeVirt"
    return _wait(kube, K_VMI, ns, name, settled, 120, label=verb)


def vm_softreboot(kube, args):
    ns, name = args.namespace, args.name
    vmi, conds = _vmi_state(kube, ns, name)
    if vmi is None:
        raise ValueError(f"{ns}/{name} is not running")
    if "AgentConnected" not in conds:
        raise ValueError("a soft reboot goes through the guest agent, which is not connected: "
                         "install qemu-guest-agent in the VM, or use Restart")
    step("softreboot", "running", f"{ns}/{name}: the guest reboots itself")
    _put_sub(kube, hv.subresource(ns, name, "softreboot"))
    step("softreboot", "done", "reboot requested to the guest")
    return EXIT_OK


def vm_restart(kube, args):
    ns, name = args.namespace, args.name
    if kube.get(K_VMI, ns, name) is None:
        raise ValueError(f"{ns}/{name} is not running")
    step("restart", "running", f"{ns}/{name}: stop within the grace period, then start")
    _put_sub(kube, hv.subresource(ns, name, "restart", vmi=False))
    step("restart", "done", "restart requested")
    return EXIT_OK


def vm_force_stop(kube, args):
    """Arrêt immédiat, comme « Force Stop » : la VM passe à l'arrêt voulu,
    puis son instance est supprimée sans délai de grâce."""
    ns, name = args.namespace, args.name
    if kube.get(K_VM, ns, name) is None:
        raise ValueError(f"no VM {ns}/{name}")
    step("stop", "running", f"{ns}/{name}: run strategy Halted")
    kube.patch(K_VM, ns, name, {"spec": {"runStrategy": "Halted"}})
    if kube.get(K_VMI, ns, name) is not None:
        step("stop", "running", "the instance is stopped without waiting for the guest")
        kube.run("delete", K_VMI, name, "-n", ns, "--grace-period=0", "--force", "--wait=false")
    return _wait(kube, K_VMI, ns, name, lambda o: (True, f"{ns}/{name} stopped") if o is None else (None, "stopping"),
                 180, label="stop")


def vm_delete(kube, args):
    ns, name = args.namespace, args.name
    vm = kube.get(K_VM, ns, name)
    if vm is None:
        raise ValueError(f"no VM {ns}/{name}")
    remove = [r.strip() for r in (args.remove_volumes or "").split(",") if r.strip()]
    ann, remove = hv.delete_plan(vm, remove)
    # les Secrets cloud-init qu'aucune autre VM n'utilise partent avec elle
    secrets = []
    if args.remove_cloudinit:
        others = [v for v in kube.list(K_VM, ns) if (v.get("metadata") or {}).get("name") != name]
        used = {s for v in others for s in hv.cloudinit_secrets(v)}
        secrets = [s for s in hv.cloudinit_secrets(vm) if s not in used]
    if remove:
        step("volumes", "running", f"deleted with the VM: {', '.join(remove)}")
        kube.patch(K_VM, ns, name, {"metadata": {"annotations": ann}})
    kept = [x["claim"] for x in hv.vm_volumes(vm) if x["claim"] and x["claim"] not in remove]
    if kept:
        step("volumes", "done", f"kept: {', '.join(kept)}")
    step("delete", "running", f"VM {ns}/{name}")
    kube.delete(K_VM, ns, name)
    code = _wait(kube, K_VM, ns, name, lambda o: (True, f"{ns}/{name} deleted") if o is None else (None, "deleting"),
                 args.timeout, label="delete")
    if code != EXIT_OK:
        return code
    # Harvester supprime les volumes annotés ; la console s'en assure
    for pvc in remove:
        if kube.get("persistentvolumeclaims", ns, pvc) is not None:
            try:
                kube.delete("persistentvolumeclaims", ns, pvc)
            except KubeError:
                pass
        _wait(kube, "persistentvolumeclaims", ns, pvc,
              lambda o, p=pvc: (True, f"volume {p} deleted") if o is None else (None, "deleting volume"),
              300, label="volumes")
    for s in secrets:
        try:
            kube.delete("secrets", ns, s)
            step("cloudinit", "done", f"cloud-init secret {s} deleted")
        except KubeError:
            pass
    return EXIT_OK


def vm_clone(kube, args):
    ns, name = args.namespace, args.name
    new = hv.check_name(args.new_name, "new name")
    vm = kube.get(K_VM, ns, name)
    if vm is None:
        raise ValueError(f"no VM {ns}/{name}")
    if kube.get(K_VM, ns, new) is not None:
        raise ValueError(f"a VM {ns}/{new} already exists")
    pvcs = {x["claim"]: kube.get("persistentvolumeclaims", ns, x["claim"]) or {}
            for x in hv.vm_volumes(vm) if x["claim"]}
    out, renames = hv.clone_manifest(vm, new, with_data=args.with_data, pvcs=pvcs, start=args.start)
    made_secrets = []
    for old in hv.cloudinit_secrets(vm):
        src = kube.get("secrets", ns, old)
        if src is None:
            continue
        copy_ = hv.cloudinit_copy(src, new)
        hv.rename_secret_refs(out, old, copy_["metadata"]["name"])
        made_secrets.append(copy_)
    what = "with its data" if args.with_data else "without its data (volumes start again from their image, or empty)"
    step("check", "running", f"{ns}/{name} to {ns}/{new}, {what}")
    import json
    kube.run("create", "--dry-run=server", "-f", "-", "-o", "name", input=json.dumps(out))
    step("check", "done", ", ".join(f"{a} to {b}" for a, b in renames.items()) or "no volume to copy")
    for s in made_secrets:
        kube.create(s)
        step("cloudinit", "done", f"cloud-init copied to {s['metadata']['name']}")
    step("clone", "running", f"VM {ns}/{new}")
    kube.create(out)
    step("clone", "done", f"{ns}/{new} created; Harvester creates its volumes")

    def ready(obj):
        missing = [c for c in renames.values()
                   if ((kube.get("persistentvolumeclaims", ns, c) or {}).get("status") or {}).get("phase") != "Bound"]
        if not missing:
            return True, f"{len(renames)} volume(s) ready"
        return None, f"waiting for {', '.join(missing)}"
    if renames:
        return _wait(kube, K_VM, ns, new, ready, args.timeout, label="volumes")
    return EXIT_OK


def vm_eject(kube, args):
    ns, name = args.namespace, args.name
    vm = kube.get(K_VM, ns, name)
    if vm is None:
        raise ValueError(f"no VM {ns}/{name}")
    out, claim = hv.eject_patch(vm, args.volume)
    ts = (out.get("spec") or {}).get("template", {}).get("spec", {})
    patch = [{"op": "test", "path": "/metadata/resourceVersion", "value": vm["metadata"]["resourceVersion"]},
             {"op": "replace", "path": "/spec/template/spec/domain/devices/disks",
              "value": ts.get("domain", {}).get("devices", {}).get("disks", [])},
             {"op": "replace", "path": "/spec/template/spec/volumes", "value": ts.get("volumes", [])}]
    ann = (out.get("metadata") or {}).get("annotations") or {}
    if hv.VCT in ann:
        patch.append({"op": "replace", "path": "/metadata/annotations/" + hv.VCT.replace("/", "~1"),
                      "value": ann[hv.VCT]})
    elif hv.VCT in ((vm.get("metadata") or {}).get("annotations") or {}):
        patch.append({"op": "remove", "path": "/metadata/annotations/" + hv.VCT.replace("/", "~1")})
    import json
    step("eject", "running", f"{args.volume} out of {ns}/{name}")
    kube.run("patch", K_VM, name, "-n", ns, "--type", "json", "-p", json.dumps(patch))
    running = kube.get(K_VMI, ns, name) is not None
    step("eject", "done", "ejected" + ("; the running VM sees it after a restart" if running else ""))
    if claim and args.delete_volume:
        if running:
            step("volume", "done", f"the CD-ROM volume {claim} is kept until the VM restarts")
        else:
            kube.delete("persistentvolumeclaims", ns, claim)
            step("volume", "done", f"volume {claim} deleted")
    return EXIT_OK


def vm_hotplug(kube, args, add=True):
    ns, name = args.namespace, args.name
    vm = kube.get(K_VM, ns, name)
    if vm is None:
        raise ValueError(f"no VM {ns}/{name}")
    if kube.get(K_VMI, ns, name) is None:
        raise ValueError(f"{ns}/{name} is stopped: add or remove the disk in its settings instead")
    vols = {x["volume"]: x for x in hv.vm_volumes(vm)}
    if add:
        claim = hv.check_name(args.claim, "volume")
        pvc = kube.get("persistentvolumeclaims", ns, claim)
        if pvc is None:
            raise ValueError(f"no volume {ns}/{claim}")
        if any(x["claim"] == claim for x in vols.values()):
            raise ValueError(f"{claim} is already a disk of this VM")
        vname = args.volume or claim
        if vname in vols:
            raise ValueError(f"this VM already has a disk named {vname}")
        body = hv.hotplug_body(vname, claim, args.bus)
        step("hotplug", "running", f"{claim} into {ns}/{name} as {vname} ({args.bus})")
        _put_sub(kube, hv.subresource(ns, name, "addvolume", vmi=False), body)
    else:
        vname = args.volume
        x = vols.get(vname)
        if not x:
            raise ValueError(f"no disk {vname} on this VM")
        if not x["hotpluggable"]:
            raise ValueError(f"{vname} was not plugged while running: remove it in the VM settings (cold)")
        step("hotplug", "running", f"{vname} out of {ns}/{name}")
        _put_sub(kube, hv.subresource(ns, name, "removevolume", vmi=False), {"name": vname})

    def settled(obj):
        st = {v.get("name"): v.get("phase") for v in ((obj or {}).get("status") or {}).get("volumeStatus") or []}
        if add and st.get(vname) == "Ready":
            return True, f"{vname} attached"
        if not add and vname not in st:
            return True, f"{vname} detached"
        return None, st.get(vname) or "pending"
    return _wait(kube, K_VMI, ns, name, settled, 300, label="hotplug")


def vm_migrate(kube, args):
    ns, name = args.namespace, args.name
    vmi = kube.get(K_VMI, ns, name)
    if vmi is None:
        raise ValueError(f"{ns}/{name} is not running: nothing to migrate")
    if hv.active_migrations(kube.list(K_VMIM, ns), name):
        raise ValueError(f"{ns}/{name} is already migrating")
    here = (vmi.get("status") or {}).get("nodeName")
    if not here:
        # vu sur harvlab : un nœud momentanément inconnu laissait choisir le
        # nœud même de la VM, et la migration restait à « Scheduling »
        raise ValueError(f"the node {ns}/{name} runs on is not known yet: try again in a moment")
    if args.node:
        if args.node == here:
            raise ValueError(f"{ns}/{name} already runs on {here}")
        node = kube.get("nodes", None, args.node)
        if node is None:
            raise ValueError(f"no node {args.node}")
        if (node.get("spec") or {}).get("unschedulable"):
            raise ValueError(f"{args.node} is cordoned or in maintenance")
    step("migrate", "running", f"{ns}/{name} from {here}" + (f" to {args.node}" if args.node else ""))
    made = kube.create(hv.migration_manifest(ns, name, args.node))
    mig = (made.get("metadata") or {}).get("name")

    def done(obj):
        phase = ((obj or {}).get("status") or {}).get("phase")
        if phase == "Succeeded":
            now = ((kube.get(K_VMI, ns, name) or {}).get("status") or {}).get("nodeName")
            return True, f"{ns}/{name} now runs on {now}"
        if phase == "Failed":
            return False, "the migration failed"
        if obj is None:
            return False, "the migration was cancelled"
        return None, phase or "pending"
    return _wait(kube, K_VMIM, ns, mig, done, args.timeout, label="migrate")


def vm_abort_migration(kube, args):
    ns, name = args.namespace, args.name
    active = hv.active_migrations(kube.list(K_VMIM, ns), name)
    if not active:
        raise ValueError(f"{ns}/{name} is not migrating")
    for m in active:
        mig = m["metadata"]["name"]
        step("abort", "running", f"migration {mig}")
        kube.delete(K_VMIM, ns, mig)
    step("abort", "done", f"{ns}/{name} stays where it runs")
    return EXIT_OK


def _images_from_volumes(kube, ns, vm, label):
    """Exporte chaque volume de la VM en image et attend leur import."""
    images = {}
    for x in hv.vm_volumes(vm):
        if not x["claim"] or x["kind"] == "cdrom":
            continue
        pvc = kube.get("persistentvolumeclaims", ns, x["claim"]) or {}
        sc = (pvc.get("spec") or {}).get("storageClassName")
        step("export", "running", f"{x['claim']} to an image")
        img = kube.create(hv.export_image_manifest(ns, x["claim"], f"{label}-{x['volume']}", None if (sc or "").startswith(("lh-", "longhorn-")) else sc))
        images[x["claim"]] = img
    for claim, img in list(images.items()):
        iname = img["metadata"]["name"]

        def imported(obj):
            st = (obj or {}).get("status") or {}
            for c in st.get("conditions") or []:
                if c.get("type") == "RetryLimitExceeded" and str(c.get("status")) == "True":
                    return False, c.get("message") or "export failed"
            if st.get("progress") == 100 and any(c.get("type") == "Imported" and str(c.get("status")) == "True"
                                                  for c in st.get("conditions") or []):
                return True, f"{claim} exported to {iname}"
            return None, f"{claim}: {st.get('progress') or 0} %"
        code = _wait(kube, "virtualmachineimages.harvesterhci.io", ns, iname, imported, 7200, label="export")
        if code != EXIT_OK:
            raise RuntimeError(f"export of {claim} failed")
        images[claim] = kube.get("virtualmachineimages.harvesterhci.io", ns, iname)
    return images


def vm_template(kube, args):
    ns, name = args.namespace, args.name
    vm = kube.get(K_VM, ns, name)
    if vm is None:
        raise ValueError(f"no VM {ns}/{name}")
    tname = hv.check_name(args.template_name, "template name")
    existing = kube.get(K_TPL, ns, tname)
    images = _images_from_volumes(kube, ns, vm, tname) if args.with_data else None
    tmpl, version = hv.template_objects(vm, tname, args.description or "", images=images)
    import json
    if existing is None:
        step("template", "running", f"template {ns}/{tname}")
        kube.create(tmpl)
    else:
        step("template", "running", f"new version of the template {ns}/{tname}")
    made = kube.create(version)
    vname = made["metadata"]["name"]
    if existing is None or args.set_default:
        kube.patch(K_TPL, ns, tname, {"spec": {"defaultVersionId": f"{ns}/{vname}"}})

    def numbered(obj):
        v = ((obj or {}).get("status") or {}).get("version")
        return (True, f"{ns}/{tname} version {v} ({vname})") if v else (None, "Harvester numbers the version")
    return _wait(kube, K_TPLV, ns, vname, numbered, 120, label="template")


def vm_cloudinit(kube, args):
    ns, name = args.namespace, args.name
    vm = kube.get(K_VM, ns, name)
    if vm is None:
        raise ValueError(f"no VM {ns}/{name}")
    user = Path(args.user_data).read_text() if args.user_data else ""
    net = Path(args.network_data).read_text() if args.network_data else ""
    if args.guest_agent:
        user = hv.with_guest_agent(user)
    secrets = hv.cloudinit_secrets(vm)
    import base64
    import json
    data = {"userdata": base64.b64encode(user.encode()).decode(),
            "networkdata": base64.b64encode(net.encode()).decode()}
    if secrets:
        step("cloudinit", "running", f"secret {secrets[0]}")
        kube.patch("secrets", ns, secrets[0], {"data": data})
        step("cloudinit", "done", "saved; the VM reads it at its next boot")
    else:
        sec = hv.cloudinit_secret(name, ns, user, net)
        step("cloudinit", "running", f"new secret {sec['metadata']['name']}")
        kube.create(sec)
        out = hv.attach_cloudinit(vm, sec["metadata"]["name"])
        ts = out["spec"]["template"]["spec"]
        patch = [{"op": "test", "path": "/metadata/resourceVersion", "value": vm["metadata"]["resourceVersion"]},
                 {"op": "replace", "path": "/spec/template/spec/volumes", "value": ts["volumes"]},
                 {"op": "replace", "path": "/spec/template/spec/domain/devices/disks",
                  "value": ts["domain"]["devices"]["disks"]}]
        kube.run("patch", K_VM, name, "-n", ns, "--type", "json", "-p", json.dumps(patch))
        step("cloudinit", "done", "attached; the VM reads it at its next boot")
    if args.ssh_names is not None:
        names = [n for n in args.ssh_names.split(",") if n.strip()]
        kube.patch(K_VM, ns, name, {"metadata": {"annotations": hv.ssh_names_annotation(names)}})
    return EXIT_OK


def _vm_json_patch(kube, vm, out, fields):
    """Remplace des champs de la VM par ceux de `out`, gardé par sa version
    (un changement fait entre-temps est refusé, pas écrasé)."""
    import json
    ops = [{"op": "test", "path": "/metadata/resourceVersion", "value": vm["metadata"]["resourceVersion"]}]
    paths = {"volumes": ("/spec/template/spec/volumes", lambda o: hv._tspec(o).get("volumes", [])),
             "disks": ("/spec/template/spec/domain/devices/disks", lambda o: hv._tspec(o)["domain"]["devices"].get("disks", [])),
             "interfaces": ("/spec/template/spec/domain/devices/interfaces",
                            lambda o: hv._tspec(o)["domain"]["devices"].get("interfaces", [])),
             "networks": ("/spec/template/spec/networks", lambda o: hv._tspec(o).get("networks", []))}
    for f in fields:
        path, get = paths[f]
        ops.append({"op": "replace" if f in ("disks", "interfaces") or get(vm) else "add", "path": path, "value": get(out)})
    ann_key = "/metadata/annotations/" + hv.VCT.replace("/", "~1")
    old_ann = (vm.get("metadata") or {}).get("annotations") or {}
    new_ann = (out.get("metadata") or {}).get("annotations") or {}
    if new_ann.get(hv.VCT) != old_ann.get(hv.VCT):
        if hv.VCT in new_ann:
            ops.append({"op": "add", "path": ann_key, "value": new_ann[hv.VCT]})
        else:
            ops.append({"op": "remove", "path": ann_key})
    try:
        kube.run("patch", K_VM, vm["metadata"]["name"], "-n", vm["metadata"]["namespace"], "--type", "json",
                 "-p", json.dumps(ops))
    except KubeError as e:
        if "test operation" in str(e) or "the object has been modified" in str(e):
            raise ValueError("the VM changed meanwhile: try again") from None
        raise


def vm_insert_cdrom(kube, args):
    ns, name = args.namespace, args.name
    vm = kube.get(K_VM, ns, name)
    if vm is None:
        raise ValueError(f"no VM {ns}/{name}")
    ref = str(args.image or "")
    ins, iname = ref.split("/", 1) if "/" in ref else (ns, ref)
    image = kube.get("virtualmachineimages.harvesterhci.io", ins, iname)
    if image is None:
        raise ValueError(f"no image {ins}/{iname}")
    out, claim = hv.insert_cdrom(vm, args.volume, image)
    step("insert", "running", f"{ins}/{iname} into the drive {args.volume} of {ns}/{name}")
    _vm_json_patch(kube, vm, out, ["volumes"])

    def ready(obj):
        if kube.get(K_VMI, ns, name) is None:
            return True, "inserted; the VM sees it when it starts"
        st = {v.get("name"): v.get("phase") for v in ((obj or {}).get("status") or {}).get("volumeStatus") or []}
        return (True, f"{claim} in the drive {args.volume}") if st.get(args.volume) == "Ready" else (None, st.get(args.volume) or "pending")
    return _wait(kube, K_VMI, ns, name, ready, args.timeout, label="insert")


def vm_eject_image(kube, args):
    ns, name = args.namespace, args.name
    vm = kube.get(K_VM, ns, name)
    if vm is None:
        raise ValueError(f"no VM {ns}/{name}")
    out, claims = hv.eject_image(vm, args.volume)
    step("eject", "running", f"image out of the drive {args.volume} of {ns}/{name} (the drive stays)")
    _vm_json_patch(kube, vm, out, ["volumes"])

    def out_of_vmi(obj):
        if obj is None:
            return True, "ejected"
        st = {v.get("name") for v in ((obj or {}).get("status") or {}).get("volumeStatus") or []}
        return (True, "ejected") if args.volume not in st else (None, "detaching")
    code = _wait(kube, K_VMI, ns, name, out_of_vmi, 300, label="eject")
    for c in claims:
        try:
            kube.delete("persistentvolumeclaims", ns, c)
            step("volume", "done", f"volume {c} deleted")
        except KubeError as e:
            step("volume", "error", f"volume {c} kept: {str(e)[:160]}")
    return code


def _apply_by_migration(kube, args):
    """Une carte réseau branchée ou débranchée s'applique par une migration
    (KubeVirt, liaison en pont) ; sans autre nœud, au prochain démarrage."""
    ns, name = args.namespace, args.name
    vmi = kube.get(K_VMI, ns, name)
    if vmi is None:
        step("apply", "done", "the VM is stopped: the change applies when it starts")
        return EXIT_OK
    here = (vmi.get("status") or {}).get("nodeName")
    others = [n for n in kube.list("nodes")
              if (n.get("metadata") or {}).get("name") != here and not (n.get("spec") or {}).get("unschedulable")
              and any(c.get("type") == "Ready" and c.get("status") == "True" for c in (n.get("status") or {}).get("conditions") or [])]
    if not others:
        step("apply", "done", "no other node to migrate to: the change applies at the next restart")
        return EXIT_OK
    step("apply", "running", "live migration to apply the change")
    args.node = None
    return vm_migrate(kube, args)


def vm_add_nic(kube, args):
    ns, name = args.namespace, args.name
    vm = kube.get(K_VM, ns, name)
    if vm is None:
        raise ValueError(f"no VM {ns}/{name}")
    ref = str(args.network or "")
    nns, nname = ref.split("/", 1) if "/" in ref else (ns, ref)
    nad = kube.get("network-attachment-definitions.k8s.cni.cncf.io", nns, nname)
    if nad is None:
        raise ValueError(f"no VM network {nns}/{nname}")
    if not hv.nad_hotpluggable(nad):
        raise ValueError(f"the network {nns}/{nname} is not hot-pluggable (a bridge network outside harvester-system is needed)")
    out = hv.add_nic(vm, args.iface, f"{nns}/{nname}", args.mac)
    step("nic", "running", f"{args.iface} on {nns}/{nname} for {ns}/{name}")
    _vm_json_patch(kube, vm, out, ["interfaces", "networks"])
    step("nic", "done", f"{args.iface} added to the VM")
    return _apply_by_migration(kube, args)


def vm_remove_nic(kube, args):
    ns, name = args.namespace, args.name
    vm = kube.get(K_VM, ns, name)
    if vm is None:
        raise ValueError(f"no VM {ns}/{name}")
    out = hv.remove_nic(vm, args.iface)
    step("nic", "running", f"{args.iface} unplugged from {ns}/{name}")
    _vm_json_patch(kube, vm, out, ["interfaces"])
    step("nic", "done", f"{args.iface} marked absent")
    return _apply_by_migration(kube, args)


def vm_cpumem(kube, args):
    """CPU et mémoire à chaud : KubeVirt applique le changement par une
    migration à chaud (stratégie LiveUpdate)."""
    ns, name = args.namespace, args.name
    vm = kube.get(K_VM, ns, name)
    if vm is None:
        raise ValueError(f"no VM {ns}/{name}")
    if kube.get(K_VMI, ns, name) is None:
        raise ValueError(f"{ns}/{name} is stopped: change its CPU and memory in its settings")
    ops = hv.cpumem_patch(vm, args.cpu, args.memory)
    import json
    ops.insert(0, {"op": "test", "path": "/metadata/resourceVersion", "value": vm["metadata"]["resourceVersion"]})
    info = hv.cpumem_info(vm)
    step("hotplug", "running", f"{ns}/{name}: CPU {info['sockets']} to {args.cpu or info['sockets']}, "
                              f"memory {info['memory']} to {args.memory or info['memory']}")
    kube.run("patch", K_VM, name, "-n", ns, "--type", "json", "-p", json.dumps(ops))

    # vu sur harvlab : la spec de la VMI change tout de suite ; ce qui est
    # APPLIQUÉ se lit dans son état (currentCPUTopology, memory), après la
    # migration que KubeVirt lance (LiveUpdate)
    seen = {}

    def applied(obj):
        conds = {c.get("type"): c for c in ((obj or {}).get("status") or {}).get("conditions") or []
                 if str(c.get("status")) == "True"}
        if "RestartRequired" in conds:
            return False, "the cluster asks for a restart: " + (conds["RestartRequired"].get("message") or "")[:200]
        st = (kube.get(K_VMI, ns, name) or {}).get("status") or {}
        cpu_now = (st.get("currentCPUTopology") or {}).get("sockets")
        mem = st.get("memory") or {}
        cpu_ok = not args.cpu or (cpu_now is not None and int(cpu_now) == int(args.cpu))
        mem_ok = not args.memory or hv.quantity(mem.get("guestRequested")) == hv.quantity(args.memory)
        if not (cpu_ok and mem_ok):
            return None, f"live update in progress (CPU {cpu_now or '?'}, memory {mem.get('guestCurrent') or '?'})"
        if args.memory and hv.quantity(mem.get("guestCurrent")) != hv.quantity(args.memory):
            # l'invité branche la mémoire par blocs (virtio-mem) : lui laisser une minute
            first = seen.setdefault("at", time.time())
            if time.time() - first < 60:
                return None, f"the guest takes the memory in: {mem.get('guestCurrent')} of {args.memory}"
            return True, (f"CPU {cpu_now}; memory {args.memory} requested, the guest sees {mem.get('guestCurrent')} "
                          "so far (its kernel must accept hot-plugged memory, virtio-mem)")
        return True, f"{ns}/{name} now has {cpu_now} vCPU and {mem.get('guestCurrent') or args.memory}"
    return _wait(kube, K_VM, ns, name, applied, args.timeout, label="hotplug")


def _replace_retry(kube, ns, name, change, tries=6):
    """Relire, modifier, remplacer ; recommencer si l'objet a changé entre-
    temps. Vu sur harvlab : pendant une migration de stockage, le contrôleur
    de Harvester réécrit la VM (ses annotations) et un remplacement direct
    échouait en conflit."""
    for i in range(tries):
        vm = kube.get(K_VM, ns, name)
        if vm is None:
            raise ValueError(f"no VM {ns}/{name}")
        out = change(vm)
        try:
            return kube.replace(out)
        except KubeError as e:
            if "the object has been modified" not in str(e) or i == tries - 1:
                raise
            time.sleep(1)


def vm_storage_migrate(kube, args, cancel=False):
    ns, name = args.namespace, args.name
    vm = kube.get(K_VM, ns, name)
    if vm is None:
        raise ValueError(f"no VM {ns}/{name}")
    if cancel:
        hv.cancel_storage_migration(vm)          # contrôle d'avance : une migration en cours
        # vu sur harvlab : annuler après la bascule de KubeVirt remettait la VM
        # sur l'ancien volume au prochain redémarrage (écritures perdues)
        targets = {e.get("targetVolume") for e in hv.claim_templates(vm) if e.get("targetVolume")}
        vmi_claims = {(v.get("persistentVolumeClaim") or {}).get("claimName")
                      for v in ((kube.get(K_VMI, ns, name) or {}).get("spec") or {}).get("volumes") or []}
        if targets & vmi_claims:
            raise ValueError("the copy is finished: the VM already runs on the target volume, "
                             "the migration can no longer be cancelled")
        step("storage", "running", f"storage migration of {ns}/{name} cancelled")
        _replace_retry(kube, ns, name, hv.cancel_storage_migration)
        step("storage", "done", "the VM keeps its volumes")
        return EXIT_OK
    if kube.get(K_VMI, ns, name) is None:
        raise ValueError(f"{ns}/{name} is stopped: a storage migration moves a running VM's volume")
    target = kube.get("persistentvolumeclaims", ns, args.target or "")
    source = kube.get("persistentvolumeclaims", ns, args.volume or "")
    if target is None:
        raise ValueError(f"no volume {ns}/{args.target}: create the target volume first")
    if source is not None:
        s = ho.size_bytes(((source.get("spec") or {}).get("resources") or {}).get("requests", {}).get("storage"))
        d = ho.size_bytes(((target.get("spec") or {}).get("resources") or {}).get("requests", {}).get("storage"))
        if s and d and d < s:
            raise ValueError("the target volume is smaller than the source")
    others = [v for v in kube.list(K_VM, ns) if (v.get("metadata") or {}).get("name") != name]
    hv.storage_migration(vm, args.volume, target, others)          # contrôle d'avance
    step("storage", "running", f"{args.volume} of {ns}/{name} moves to {args.target} while the VM runs")
    _replace_retry(kube, ns, name, lambda v: hv.storage_migration(v, args.volume, target, others))

    def done(obj):
        vcts = hv.claim_templates(obj)
        if any(e.get("targetVolume") for e in vcts):
            conds = {c.get("type") for c in ((obj or {}).get("status") or {}).get("conditions") or []
                     if str(c.get("status")) == "True"}
            return None, "copying" if "VolumesChange" in conds else "waiting for the migration"
        claims = {x["claim"] for x in hv.vm_volumes(obj)}
        return (True, f"{ns}/{name} now uses {args.target}") if args.target in claims else (False, "the migration was undone")
    return _wait(kube, K_VM, ns, name, done, args.timeout, label="storage")


def vm_quota(kube, args):
    ns, name = args.namespace, args.name
    if kube.get(K_VM, ns, name) is None:
        raise ValueError(f"no VM {ns}/{name}")
    existing = kube.get("resourcequotas.harvesterhci.io", ns, hv.QUOTA_NAME)
    obj = hv.quota_object(existing, ns, name, args.size)
    if obj is None:
        step("quota", "done", f"{ns}/{name} has no snapshot quota")
        return EXIT_OK
    step("quota", "running", f"snapshot quota of {ns}/{name}: {args.size or 'none'}")
    if existing is None:
        kube.create(obj)
    else:
        kube.replace(obj)
    step("quota", "done", f"{ns}/{name}: {args.size or 'no quota'}")
    return EXIT_OK


def vm_access(kube, args):
    ns, name = args.namespace, args.name
    vm = kube.get(K_VM, ns, name)
    if vm is None:
        raise ValueError(f"no VM {ns}/{name}")
    password = Path(args.password_file).read_text().rstrip("\n") if args.password_file else None
    keys = []
    for ref in [r for r in (args.keys or "").split(",") if r.strip()]:
        kns, kname = ref.split("/", 1) if "/" in ref else (ns, ref)
        kp = kube.get("keypairs.harvesterhci.io", kns, kname)
        if kp is None:
            raise ValueError(f"no SSH key {kns}/{kname}")
        keys.append({"namespace": kns, "name": kname, "public_key": (kp.get("spec") or {}).get("publicKey")})
    users = [u for u in (args.users or "").split(",")]
    secret, out = hv.access_credential(vm, args.kind, users, password=password, keys=keys)
    step("access", "running", f"{'password' if args.kind == 'basic' else 'SSH keys'} for {', '.join(u for u in users if u)} on {ns}/{name}")
    kube.create(secret)
    try:
        kube.replace(out)
    except KubeError:
        kube.delete("secrets", ns, secret["metadata"]["name"])
        raise
    running = kube.get(K_VMI, ns, name) is not None
    # vu sur harv1 : un nouvel accès n'est pas mis à jour à chaud
    # (RestartRequired), comme « Save and Restart » dans Harvester
    step("access", "done", "the guest agent applies it at the VM's next restart" if running
         else "the guest agent applies it when the VM starts")
    return EXIT_OK


VM_ACTIONS = {
    "pause": lambda k, a: vm_pause(k, a, True), "unpause": lambda k, a: vm_pause(k, a, False),
    "softreboot": vm_softreboot, "restart": vm_restart, "force-stop": vm_force_stop,
    "delete": vm_delete, "clone": vm_clone, "eject": vm_eject,
    "add-volume": lambda k, a: vm_hotplug(k, a, True), "remove-volume": lambda k, a: vm_hotplug(k, a, False),
    "migrate": vm_migrate, "abort-migration": vm_abort_migration, "template": vm_template,
    "cloudinit": vm_cloudinit,
    "insert-cdrom": vm_insert_cdrom, "eject-image": vm_eject_image,
    "add-nic": vm_add_nic, "remove-nic": vm_remove_nic,
    "cpumem": vm_cpumem, "storage-migrate": vm_storage_migrate,
    "cancel-storage-migration": lambda k, a: vm_storage_migrate(k, a, cancel=True),
    "quota": vm_quota, "access": vm_access,
}


def cmd_vm(args):
    kube = kube_from(args)
    hv.check_name(args.name, "VM name")
    return VM_ACTIONS[args.action](kube, args)


# ---------------------------------------------------------------------------
# Hôtes (v1.62.0) : la fenêtre « Modifier la configuration » d'un hôte de
# Harvester et ses gestes (CPU manager, alimentation hors bande, suppression).
# ---------------------------------------------------------------------------

LH_NS = "longhorn-system"


def _node(kube, name):
    node = kube.get("nodes", None, name)
    if node is None:
        raise ValueError(f"no host {name}")
    return node


def _lh_node(kube, name):
    lh = kube.get(hh.K_LHNODE, LH_NS, name)
    if lh is None:
        raise ValueError(f"Longhorn does not know the host {name}")
    return lh


def _split(text):
    return [t.strip() for t in (text or "").split(",") if t.strip()]


def host_basics(kube, args):
    node = _node(kube, args.node)
    labels = _read_json(args.labels_file) if args.labels_file else None
    patch = hh.basics_patch(node, args.custom_name, args.console_url, labels)
    step("host", "running", f"settings of {args.node}")
    kube.patch("nodes", None, args.node, patch)
    step("host", "done", f"{args.node} updated")
    return EXIT_OK


def host_tags(kube, args):
    lh = _lh_node(kube, args.node)
    patch = hh.lh_node_patch(lh, tags=_split(args.tags))
    step("tags", "running", f"host tags of {args.node}: {', '.join(patch['spec']['tags']) or 'none'}")
    kube.patch(hh.K_LHNODE, LH_NS, args.node, patch)
    step("tags", "done", "Longhorn places the replicas of the classes with these tags on this host")
    return EXIT_OK


def _block_device(kube, args):
    bd = kube.get(hh.K_BD, LH_NS, args.disk or "")
    if bd is None or (bd.get("spec") or {}).get("nodeName") != args.node:
        raise ValueError(f"no disk {args.disk} on {args.node}")
    return bd


def _bd_provisioned(path, node):
    """Fin du provisionnement d'un BlockDevice, pour _wait."""
    def done(obj):
        st = (obj or {}).get("status") or {}
        conds = {c.get("type"): c for c in st.get("conditions") or []}
        fail = [c.get("message") for c in conds.values() if str(c.get("status")) == "False" and c.get("reason") == "Failed"]
        if fail:
            return False, fail[0]
        if st.get("provisionPhase") == "Provisioned":
            return True, f"{path} is a storage disk of {node}"
        return None, st.get("provisionPhase") or "formatting and mounting"
    return done


def host_disk_add(kube, args):
    bd = _block_device(kube, args)
    tags = _split(args.tag) if getattr(args, "tag", None) is not None else None
    out = hh.disk_add(bd, force_format=args.format, provisioner=args.provisioner, vg=args.vg, tags=tags)
    path = (bd.get("spec") or {}).get("devPath")
    step("disk", "running", f"{path} added to {args.node}"
         + (" (formatted)" if out["spec"]["fileSystem"]["forceFormatted"] else "")
         + (f", tags {', '.join(out['spec']['tags'])}" if out["spec"].get("tags") else ""))
    kube.replace(out)
    return _wait(kube, hh.K_BD, LH_NS, args.disk, _bd_provisioned(path, args.node), args.timeout, label="disk")


def host_disk_remove(kube, args):
    bd = _block_device(kube, args)
    out = hh.disk_remove(bd)
    path = (bd.get("spec") or {}).get("devPath")
    step("disk", "running", f"{path} leaves {args.node}: its replicas move to the other disks first")
    kube.replace(out)

    def done(obj):
        st = (obj or {}).get("status") or {}
        if st.get("provisionPhase") == "Unprovisioned":
            return True, f"{path} no longer holds volumes"
        return None, st.get("provisionPhase") or "moving the replicas"
    return _wait(kube, hh.K_BD, LH_NS, args.disk, done, args.timeout, label="disk")


def host_disk_set(kube, args):
    lh = _lh_node(kube, args.node)
    sched = None if args.scheduling is None else args.scheduling == "on"
    tags = None if args.tags is None else _split(args.tags)
    patch = hh.lh_node_patch(lh, disk=args.disk, disk_tags=tags, scheduling=sched)
    step("disk", "running", f"disk {args.disk} of {args.node}")
    kube.patch(hh.K_LHNODE, LH_NS, args.node, patch)
    step("disk", "done", "saved")
    return EXIT_OK


def host_hugepages(kube, args):
    _node(kube, args.node)
    if kube.get(hh.K_HUGEPAGE, None, args.node) is None:
        raise ValueError("this Harvester has no huge page settings (Harvester 1.7 or later)")
    patch = hh.hugepage_patch(args.thp_enabled, args.thp_shmem, args.thp_defrag)
    step("hugepages", "running", f"transparent huge pages of {args.node}")
    kube.patch(hh.K_HUGEPAGE, None, args.node, patch)
    step("hugepages", "done", "applied by the node manager")
    return EXIT_OK


def host_ksmtuned(kube, args):
    _node(kube, args.node)
    if kube.get(hh.K_KSM, None, args.node) is None:
        raise ValueError(f"no ksmtuned settings for {args.node}")
    params = _read_json(args.params) if args.params else None
    merge = None if args.merge is None else args.merge == "on"
    patch = hh.ksmtuned_patch(args.run, args.mode, args.thres, merge, params)
    step("ksmtuned", "running", f"memory page merging (KSM) of {args.node}")
    kube.patch(hh.K_KSM, None, args.node, patch)
    step("ksmtuned", "done", "applied by the node manager")
    return EXIT_OK


def host_cpu_manager(kube, args):
    if args.enable is None:
        raise ValueError("give --enable or --disable")
    node = _node(kube, args.node)
    vmis = [v for v in kube.list(K_VMI) if ((v.get("status") or {}).get("nodeName")) == args.node]
    patch = hh.cpu_manager_request(node, args.enable, vmis)
    step("cpumanager", "running", f"{'enabling' if args.enable else 'disabling'} the CPU manager of {args.node}: "
         "Harvester restarts the node's Kubernetes agent (the VMs keep running)")
    kube.patch("nodes", None, args.node, patch)
    want = "true" if args.enable else "false"

    def done(obj):
        st = hh.cpu_manager_status(obj)
        if st["status"] == "failed":
            return False, "Harvester could not change the CPU manager (see the update-cpu-manager job)"
        if st["status"] == "success" and st["label"] == want:
            return True, f"CPU manager {'enabled' if args.enable else 'disabled'} on {args.node}"
        return None, {"requested": "requested", "running": "the node's agent restarts"}.get(st["status"], "waiting")
    return _wait(kube, "nodes", None, args.node, done, args.timeout, label="cpumanager")


def _seeder_enabled(kube):
    addon = kube.get(K_ADDON, "harvester-system", "harvester-seeder")
    return bool(((addon or {}).get("spec") or {}).get("enabled"))


def host_oob(kube, args):
    _node(kube, args.node)
    inv = kube.get(hh.K_INVENTORY, "harvester-system", args.node)
    if args.off:
        if inv is None:
            step("oob", "done", f"{args.node} has no out-of-band access")
            return EXIT_OK
        step("oob", "running", f"out-of-band access of {args.node} removed")
        kube.delete(hh.K_INVENTORY, "harvester-system", args.node)
        step("oob", "done", "the BMC credentials secret is kept")
        return EXIT_OK
    if not _seeder_enabled(kube):
        raise ValueError("enable the harvester-seeder add-on first (Add-ons)")
    ref = ((((inv or {}).get("spec") or {}).get("baseboardSpec") or {}).get("connection") or {}).get("authSecretRef") or {}
    sns, sname = ref.get("namespace") or "harvester-system", ref.get("name") or f"{args.node}-bmc"
    if args.password_file:
        password = Path(args.password_file).read_text().rstrip("\n")
        existing = kube.get("secrets", sns, sname)
        secret = hh.bmc_secret(args.node, args.username, password, existing)
        secret["metadata"].update({"name": sname, "namespace": sns})
        step("oob", "running", f"BMC credentials of {args.node} saved in the secret {sns}/{sname}")
        (kube.replace if existing else kube.create)(secret)
    elif kube.get("secrets", sns, sname) is None:
        raise ValueError("give the BMC user name and password")
    obj = hh.inventory(args.node, args.bmc_host, args.bmc_port, sns, sname, insecure=args.insecure,
                       events=not args.no_events, interval=args.interval, existing=inv)
    step("oob", "running", f"{args.node} reached out of band at {args.bmc_host}")
    (kube.replace if inv else kube.create)(obj)
    started = time.time()

    def done(o):
        st = (o or {}).get("status") or {}
        if st.get("status") == "inventoryNodeReady":
            return True, f"the BMC answers: {args.node} is {st.get('machinePowerState') or 'known'}"
        err = hh.bmc_error(o)
        # vu sur harvlab : le seeder réessaie sans fin et garde la condition
        # d'un essai précédent ; passé 45 s sans réponse, on dit pourquoi
        if err and time.time() - started > 45:
            return False, f"the BMC cannot be reached: {err}"
        return None, st.get("status") or "the seeder contacts the BMC"
    return _wait(kube, hh.K_INVENTORY, "harvester-system", args.node, done, min(args.timeout, 300), label="oob")


def host_power(kube, args):
    node = _node(kube, args.node)
    inv = kube.get(hh.K_INVENTORY, "harvester-system", args.node)
    patch = hh.power_check(node, inv, args.operation)
    step("power", "running", f"{args.operation} of {args.node} through its BMC")
    kube.patch(hh.K_INVENTORY, "harvester-system", args.node, patch)
    # comme l'action powerAction de Harvester : vider lastJobName (sous-ressource
    # status) est ce qui fait lancer un nouveau travail au seeder. Vu sur
    # harvlab : sans lui, la demande restait sans suite et l'état du travail
    # précédent faisait croire l'action finie.
    kube.run("patch", hh.K_INVENTORY, args.node, "-n", "harvester-system", "--subresource=status",
             "--type", "merge", "-p", json.dumps({"status": {"powerAction": {"lastJobName": ""}}}))
    prefix = f"{args.node}-{args.operation}-"

    def done(o):
        st = (o or {}).get("status") or {}
        job = (st.get("powerAction") or {}).get("lastJobName") or ""
        if not job.startswith(prefix):
            return None, "the seeder prepares the BMC job"
        bj = kube.get(hh.K_BMC_JOB, "harvester-system", job) or {}
        conds = {c.get("type"): str(c.get("status")) for c in (bj.get("status") or {}).get("conditions") or []}
        if conds.get("Failed") == "True":
            return False, f"{args.operation} of {args.node} failed (BMC job {job})"
        if conds.get("Completed") == "True":
            return True, f"{args.operation} of {args.node} done" + (
                f" (machine {st.get('machinePowerState')})" if st.get("machinePowerState") else "")
        return None, "the BMC is working"
    return _wait(kube, hh.K_INVENTORY, "harvester-system", args.node, done, min(args.timeout, 900), label="power")


def host_delete(kube, args):
    node = _node(kube, args.node)
    kind, ns, name = hh.delete_target(node, kube.list("nodes"))
    step("host", "running", f"{args.node} removed from the cluster"
         + (f" (Cluster API machine {ns}/{name})" if ns else ""))
    kube.delete(kind, ns, name)

    def done(o):
        return (True, f"{args.node} is no longer in the cluster") if o is None else (None, "the node is being removed")
    return _wait(kube, "nodes", None, args.node, done, args.timeout, label="host")


# ---------------------------------------------------------------------------
# Pools de disques de données (v1.78.0) : après une installation bare-metal
# (étape « pools » de la console) ou dans un pipeline, les disques de chaque
# pool provisionnés dans Longhorn avec leur étiquette, et une classe
# `longhorn-<tag>` par pool. Vu sur le banc (NDM 1.8) : les BlockDevices
# portent un UUID, se retrouvent par série ou WWN ; `engineVersion` n'est
# pas mis par défaut ; NDM formate, monte sous
# /var/lib/harvester/extra-disks/<nom> et recopie spec.tags dans Longhorn.
# ---------------------------------------------------------------------------

def _pools_find(kube, node, pools, deadline, sleep, now):
    """{(tag, index): BlockDevice} une fois tous les disques vus par NDM.
    Juste après l'installation, NDM (et même son CRD) n'existe pas encore :
    on attend, puis on dit clairement ce qui manque."""
    want = [(p["tag"], i, d) for p in pools for i, d in enumerate(p["disks"])]
    step("find", "running", f"{len(want)} disk(s) of {node} among node-disk-manager's block devices")
    last = None
    while True:
        bds = kube.list(hh.K_BD, LH_NS)
        found, missing = {}, []
        for tag, i, d in want:
            bd = hh.match_block_device(bds, node, d)
            if bd is None:
                missing.append(hh.disk_label(d))
            else:
                found[(tag, i)] = bd
        names = [(b.get("metadata") or {}).get("name") for b in found.values()]
        if len(set(names)) != len(names):
            raise ValueError("two pool disks resolve to the same block device")
        if not missing:
            step("find", "done", f"{len(found)} disk(s) found")
            return found, bds
        msg = f"waiting for {', '.join(missing)}"
        if msg != last:
            step("find", "running", msg)
            last = msg
        if now() >= deadline:
            step("find", "error", f"not found on {node} (by serial or WWN): {', '.join(missing)}")
            return None, bds
        sleep(10)


def _pools_lh_tags(kube, node, name, tag, deadline, sleep, now):
    """L'étiquette sur le disque Longhorn (NDM la recopie ; on la pose si
    elle manque, sans retirer les autres)."""
    while True:
        lh = kube.get(hh.K_LHNODE, LH_NS, node)
        disk = (((lh or {}).get("spec") or {}).get("disks") or {}).get(name)
        if disk is not None:
            tags = list(disk.get("tags") or [])
            if tag not in tags:
                kube.patch(hh.K_LHNODE, LH_NS, node, hh.lh_node_patch(lh, disk=name, disk_tags=tags + [tag]))
            return True
        if now() >= deadline:
            return False
        sleep(5)


def pools_apply(kube, node, pools, timeout, classes=True, sleep=time.sleep, now=time.time):
    deadline = now() + timeout
    # une classe existante à un autre sélecteur : refus AVANT de formater quoi que ce soit
    existing = {}
    if classes:
        for p in pools:
            existing[p["tag"]] = hh.pool_class_state(kube.get(hs.K_SC, None, hh.pool_class_name(p["tag"])), p["tag"])
    found, bds = _pools_find(kube, node, pools, deadline, sleep, now)
    if found is None:
        return EXIT_BLOCKED
    todo = []
    lh = kube.get(hh.K_LHNODE, LH_NS, node)
    for p in pools:
        for i, d in enumerate(p["disks"]):
            bd = found[(p["tag"], i)]
            name = bd["metadata"]["name"]
            path = (bd.get("spec") or {}).get("devPath") or hh.disk_label(d)
            if hh.pool_disk_state(bd, p["tag"]) == "done":
                step("disk", "done", f"{path} is already in the pool {p['tag']}")
            else:
                # jamais le disque système, le disque de données ni celui de Longhorn
                why = hh.system_use(bd, bds, lh)
                if why:
                    raise ValueError(f"{why}: it cannot join the pool {p['tag']}")
                todo.append((p["tag"], name, path, bd))
            found[(p["tag"], i)] = (name, path)
    for tag, name, path, bd in todo:
        out = hh.disk_add(bd, force_format=True, provisioner="LonghornV1", tags=[tag])
        step("disk", "running", f"{path} formatted into the pool {tag}")
        kube.replace(out)
    for tag, name, path, _ in todo:
        rc = _wait(kube, hh.K_BD, LH_NS, name, _bd_provisioned(path, node), max(1, deadline - now()),
                   sleep=sleep, now=now, label="disk")
        if rc != EXIT_OK:
            return rc
    for p in pools:
        for i, _ in enumerate(p["disks"]):
            name, path = found[(p["tag"], i)]
            if not _pools_lh_tags(kube, node, name, p["tag"], deadline, sleep, now):
                step("longhorn", "error", f"Longhorn does not show {path} on {node} yet")
                return EXIT_FAIL
    step("longhorn", "done", "the disks carry their pool tag in Longhorn")
    if not classes:
        step("classes", "done", "joining node: the cluster's storage classes are kept as they are")
        return EXIT_OK
    for p in pools:
        cname = hh.pool_class_name(p["tag"])
        if existing[p["tag"]] == "same" or \
                hh.pool_class_state(kube.get(hs.K_SC, None, cname), p["tag"]) == "same":
            step("classes", "running", f"{cname} already exists")
            continue
        obj = ho.storageclass_manifest({"name": cname, "replicas": p["replicas"], "disk_selector": p["tag"],
                                        "stale_timeout": 30, "migratable": True})
        step("classes", "running", f"{cname}: {p['replicas']} replica(s) on the disks tagged {p['tag']}")
        kube.create(obj)
    step("classes", "done", ", ".join(hh.pool_class_name(p["tag"]) for p in pools))
    return EXIT_OK


def cmd_pools_apply(args):
    hh.check_node(args.node)
    spec = _read_json(args.spec)
    pools = hh.check_pools((spec or {}).get("pools") if isinstance(spec, dict) else None)
    if not pools:
        raise ValueError("pools: at least one pool")
    kube = kube_from(args)
    return pools_apply(kube, args.node, pools, args.timeout, classes=not args.no_classes)


HOST_ACTIONS = {
    "basics": host_basics, "tags": host_tags, "disk-add": host_disk_add, "disk-remove": host_disk_remove,
    "disk-set": host_disk_set, "hugepages": host_hugepages, "ksmtuned": host_ksmtuned,
    "cpu-manager": host_cpu_manager, "oob": host_oob, "power": host_power, "delete": host_delete,
}


def cmd_host(args):
    kube = kube_from(args)
    hh.check_node(args.node)
    return HOST_ACTIONS[args.action](kube, args)


# ---------------------------------------------------------------------------
# Namespaces (v1.62.0) : le menu Namespaces de Harvester.
# ---------------------------------------------------------------------------

def ns_create(kube, args):
    if kube.get("namespaces", None, args.name) is not None:
        raise ValueError(f"the namespace {args.name} already exists")
    labels = _read_json(args.labels_file) if args.labels_file else None
    obj = hn.manifest(args.name, args.description or "", labels)
    step("namespace", "running", f"namespace {args.name} created")
    kube.create(obj)
    step("namespace", "done", f"{args.name} is ready")
    return EXIT_OK


def ns_update(kube, args):
    obj = kube.get("namespaces", None, args.name)
    if obj is None:
        raise ValueError(f"no namespace {args.name}")
    labels = _read_json(args.labels_file) if args.labels_file else None
    annotations = _read_json(args.annotations_file) if args.annotations_file else None
    patch = hn.update_patch(obj, args.description, labels, annotations)
    step("namespace", "running", f"namespace {args.name} changed")
    kube.patch("namespaces", None, args.name, patch)
    step("namespace", "done", "saved")
    return EXIT_OK


def ns_quota(kube, args):
    if kube.get("namespaces", None, args.name) is None:
        raise ValueError(f"no namespace {args.name}")
    size = str(args.size or "0")
    nbytes = 0 if size == "0" else hv.quantity(size)
    if nbytes is None:
        raise ValueError("quota: a size such as 100Gi, or 0 to remove it")
    existing = kube.get(hn.K_QUOTA, args.name, hn.QUOTA_NAME)
    obj = hn.quota_object(existing, args.name, nbytes)
    if obj is None:
        step("quota", "done", f"{args.name} has no snapshot quota")
        return EXIT_OK
    step("quota", "running", f"snapshot quota of the namespace {args.name}: {size if nbytes else 'none'}")
    (kube.replace if existing else kube.create)(obj)
    step("quota", "done", f"{args.name}: {size if nbytes else 'no quota'}")
    return EXIT_OK


def ns_delete(kube, args):
    obj = kube.get("namespaces", None, args.name)
    if obj is None:
        raise ValueError(f"no namespace {args.name}")
    what = hn.delete_check(args.name, obj, kube.list(K_VM, args.name), kube.list("persistentvolumeclaims", args.name))
    step("namespace", "running", f"namespace {args.name} deleted with {len(what['vms'])} VM(s) and "
         f"{len(what['volumes'])} volume(s)")
    kube.delete("namespaces", None, args.name)

    def done(o):
        if o is None:
            return True, f"{args.name} is gone"
        return None, "Kubernetes removes what the namespace holds"
    return _wait(kube, "namespaces", None, args.name, done, args.timeout, label="namespace")


NS_ACTIONS = {"create": ns_create, "update": ns_update, "quota": ns_quota, "delete": ns_delete}


def cmd_namespace(args):
    kube = kube_from(args)
    hn.check_name(args.name)
    return NS_ACTIONS[args.action](kube, args)


# ---------------------------------------------------------------------------
# Volumes et images (v1.63.0) : les actions de Harvester, refaites au kubectl
# (pkg/api/volume et pkg/api/image de Harvester 1.9).
# ---------------------------------------------------------------------------

def _pvc(kube, ns, name):
    pvc = kube.get(hs.K_PVC, ns, name)
    if pvc is None:
        raise ValueError(f"no volume {ns}/{name}")
    return pvc


def _sc(kube, name):
    return kube.get(hs.K_SC, None, name or "") if name else None


def _mounted_by_pod(kube, ns, name):
    for pod in kube.list("pods", ns):
        if (pod.get("status") or {}).get("phase") in ("Succeeded", "Failed"):
            continue
        for v in ((pod.get("spec") or {}).get("volumes") or []):
            if ((v.get("persistentVolumeClaim") or {}).get("claimName")) == name:
                return (pod.get("metadata") or {}).get("name")
    return None


def vol_clone(kube, args):
    src = _pvc(kube, args.namespace, args.name)
    if kube.get(hs.K_PVC, args.namespace, args.new_name or "") is not None:
        raise ValueError(f"a volume {args.namespace}/{args.new_name} already exists")
    obj = hs.clone_pvc(src, args.new_name, with_data=not args.no_data)
    step("clone", "running", f"{args.namespace}/{args.name} cloned to {args.new_name}"
         + ("" if args.no_data else " with its data"))
    kube.create(obj)

    def done(o):
        phase = ((o or {}).get("status") or {}).get("phase")
        return (True, f"{args.new_name} is ready") if phase == "Bound" else (None, f"volume {phase or 'pending'}")
    return _wait(kube, hs.K_PVC, args.namespace, args.new_name, done, args.timeout, label="clone")


def vol_export(kube, args):
    src = _pvc(kube, args.namespace, args.name)
    pv = kube.get(hs.K_PV, None, (src.get("spec") or {}).get("volumeName") or "")
    sc_name = args.storage_class or (src.get("spec") or {}).get("storageClassName")
    sc_obj, src_sc = _sc(kube, sc_name), _sc(kube, (src.get("spec") or {}).get("storageClassName"))
    if sc_obj is None:
        raise ValueError(f"no storage class {sc_name}")
    target_ns = args.target_namespace or args.namespace
    if not (hs.is_longhorn_v1(src_sc) and hs.is_longhorn_v1(sc_obj)):
        # vu dans le serveur de Harvester : hors Longhorn v1, un volume monté ne s'exporte pas
        pod = _mounted_by_pod(kube, args.namespace, args.name)
        if pod:
            raise ValueError(f"the volume is used by the pod {pod}: stop its VM first")
    obj = hs.export_image(src, args.display_name, target_ns, sc_name, sc_obj, src_sc, encrypted=hs.pv_encrypted(pv))
    step("export", "running", f"{args.namespace}/{args.name} exported to the image {args.display_name}")
    made = kube.create(obj)
    name = (made.get("metadata") or {}).get("name")

    def done(o):
        state, msg = hs.image_state(o)
        if state == "ready":
            return True, f"image {args.display_name} ({target_ns}/{name}) is ready"
        if state == "failed":
            return False, msg
        return None, f"exporting {((o or {}).get('status') or {}).get('progress') or 0} %"
    return _wait(kube, hs.K_IMAGE, target_ns, name, done, args.timeout, label="export")


def vol_snapshot(kube, args):
    src = _pvc(kube, args.namespace, args.name)
    sc = _sc(kube, (src.get("spec") or {}).get("storageClassName"))
    prov = (sc or {}).get("provisioner")
    setting = kube.get("settings.harvesterhci.io", None, "csi-driver-config")
    snap_class = hs.csi_snapshot_class((setting or {}).get("value") or (setting or {}).get("default"), prov)
    if kube.get(hs.K_SNAP, args.namespace, args.snapshot_name or "") is not None:
        raise ValueError(f"a snapshot {args.snapshot_name} already exists")
    obj = hs.volume_snapshot(src, args.snapshot_name, snap_class, prov)
    step("snapshot", "running", f"snapshot {args.snapshot_name} of {args.namespace}/{args.name}")
    kube.create(obj)

    def done(o):
        st = (o or {}).get("status") or {}
        if st.get("error"):
            return False, (st["error"].get("message") or "failed")[:200]
        return (True, f"snapshot {args.snapshot_name} ready") if st.get("readyToUse") else (None, "taking the snapshot")
    return _wait(kube, hs.K_SNAP, args.namespace, args.snapshot_name, done, args.timeout, label="snapshot")


def vol_copy(kube, args):
    src = _pvc(kube, args.namespace, args.name)
    if _sc(kube, args.storage_class) is None:
        raise ValueError(f"no storage class {args.storage_class}")
    for kind in (hs.K_PVC, hs.K_DV):
        if kube.get(kind, args.namespace, args.new_name or "") is not None:
            raise ValueError(f"{args.namespace}/{args.new_name} already exists")
    pod = _mounted_by_pod(kube, args.namespace, args.name)
    if pod:
        raise ValueError(f"the volume is used by the pod {pod}: stop its VM first")
    obj = hs.data_volume(src, args.new_name, args.storage_class)
    step("copy", "running", f"{args.namespace}/{args.name} copied to {args.new_name} ({args.storage_class}); the original stays")
    kube.create(obj)

    def done(o):
        st = (o or {}).get("status") or {}
        phase = st.get("phase")
        if phase == "Succeeded":
            return True, f"{args.new_name} is a copy of {args.name}"
        if phase in ("Failed",):
            return False, "the copy failed"
        return None, f"{phase or 'pending'} {st.get('progress') or ''}".strip()
    return _wait(kube, hs.K_DV, args.namespace, args.new_name, done, args.timeout, label="copy")


def vol_cancel_expand(kube, args):
    """Comme l'action cancelExpand de Harvester : PV gardé (Retain), PVC
    supprimé puis recréé à sa capacité réelle sur le même PV, politique
    d'origine rétablie."""
    pvc = _pvc(kube, args.namespace, args.name)
    if not hs.resizing(pvc):
        raise ValueError("the volume is not being expanded")
    vms = hs.used_by_vms(args.name, args.namespace, kube.list(K_VM, args.namespace))
    if vms:
        raise ValueError(f"the volume is used by the VM {', '.join(vms)}")
    pv_name = (pvc.get("spec") or {}).get("volumeName")
    pv = kube.get(hs.K_PV, None, pv_name or "")
    if pv is None:
        raise ValueError("the volume has no persistent volume")
    new = hs.recreated_pvc(pvc)
    policy = (pv.get("spec") or {}).get("persistentVolumeReclaimPolicy") or "Delete"
    step("cancel", "running", f"expansion of {args.namespace}/{args.name} cancelled: the claim is recreated "
         f"at {new['spec']['resources']['requests']['storage']} on the same volume")
    kube.patch(hs.K_PV, None, pv_name, {"spec": {"persistentVolumeReclaimPolicy": "Retain"}})
    try:
        kube.delete(hs.K_PVC, args.namespace, args.name)
        for _ in range(60):
            if kube.get(hs.K_PVC, args.namespace, args.name) is None:
                break
            time.sleep(1)
        else:
            raise RuntimeError("the claim was not deleted")
        kube.run("patch", hs.K_PV, pv_name, "--type", "json", "-p",
                 json.dumps([{"op": "remove", "path": "/spec/claimRef"}]))
        kube.create(new)
    finally:
        kube.patch(hs.K_PV, None, pv_name, {"spec": {"persistentVolumeReclaimPolicy": policy}})

    def done(o):
        phase = ((o or {}).get("status") or {}).get("phase")
        return (True, f"{args.name} is back at its size") if phase == "Bound" else (None, f"volume {phase or 'pending'}")
    return _wait(kube, hs.K_PVC, args.namespace, args.name, done, args.timeout, label="cancel")


def vol_describe(kube, args):
    _pvc(kube, args.namespace, args.name)
    kube.patch(hs.K_PVC, args.namespace, args.name, hs.description_patch(None, args.description))
    step("describe", "done", f"description of {args.namespace}/{args.name} saved")
    return EXIT_OK


VOLUME_ACTIONS = {"clone": vol_clone, "export": vol_export, "snapshot": vol_snapshot, "copy": vol_copy,
                  "cancel-expand": vol_cancel_expand, "describe": vol_describe}


def cmd_volume(args):
    kube = kube_from(args)
    hs.check_name(args.name, "volume name")
    return VOLUME_ACTIONS[args.action](kube, args)


def _image(kube, ns, name):
    img = kube.get(hs.K_IMAGE, ns, name)
    if img is None:
        raise ValueError(f"no image {ns}/{name}")
    return img


def _wait_image(kube, ns, name, display, timeout, label):
    def done(o):
        state, msg = hs.image_state(o)
        if state == "ready":
            return True, f"image {display} ({ns}/{name}) is ready"
        if state == "failed":
            return False, msg
        return None, msg or f"importing {((o or {}).get('status') or {}).get('progress') or 0} %"
    return _wait(kube, hs.K_IMAGE, ns, name, done, timeout, label=label)


def img_edit(kube, args):
    img = _image(kube, args.namespace, args.name)
    labels = _read_json(args.labels_file) if args.labels_file else None
    patch = hs.image_edit_patch(img, args.description, labels)
    step("image", "running", f"image {args.namespace}/{args.name} changed")
    kube.run("patch", hs.K_IMAGE, args.name, "-n", args.namespace, "--type", "json", "-p", json.dumps(patch))
    step("image", "done", "saved")
    return EXIT_OK


def img_clone(kube, args):
    img = _image(kube, args.namespace, args.name)
    obj = hs.clone_image(img, args.display_name)
    step("image", "running", f"image {args.display_name} downloaded again from {obj['spec'].get('url')}")
    made = kube.create(obj)
    return _wait_image(kube, args.namespace, made["metadata"]["name"], args.display_name, args.timeout, "image")


def img_crypto(kube, args, operation):
    img = _image(kube, args.namespace, args.name)
    sc = _sc(kube, args.storage_class)
    if sc is None:
        raise ValueError(f"no storage class {args.storage_class}")
    obj = hs.crypto_image(img, operation, args.display_name, args.storage_class, sc)
    step("image", "running", f"image {args.display_name}: {operation}ed copy of {args.name}")
    made = kube.create(obj)
    return _wait_image(kube, args.namespace, made["metadata"]["name"], args.display_name, args.timeout, "image")


def img_prepare_download(kube, args, img=None):
    """v1.74.0 : une image CDI se télécharge par un downloader du même nom
    (Harvester convertit le volume en qcow2) : le créer et l'attendre. Une
    image Longhorn v1 n'a rien à préparer."""
    img = img or _image(kube, args.namespace, args.name)
    if not hs.is_cdi(img):
        step("download", "done", f"image {args.name}: Longhorn image, nothing to prepare")
        return EXIT_OK
    if hs.image_state(img)[0] != "ready":
        raise ValueError(f"image {args.name} is not imported yet")
    if kube.get(hs.K_DOWNLOADER, args.namespace, args.name) is None:
        kube.create(hs.downloader_manifest(args.namespace, args.name))
        step("download", "running", f"downloader {args.name} created: Harvester converts the volume to qcow2")
    return _wait(kube, hs.K_DOWNLOADER, args.namespace, args.name, hs.downloader_state, args.timeout, label="download")


def img_download(kube, args):
    img = _image(kube, args.namespace, args.name)
    out = Path(args.out)
    if hs.is_cdi(img):
        if img_prepare_download(kube, args, img) != EXIT_OK:
            return EXIT_FAIL
        path = hs.cdi_download_path(args.namespace, args.name)
        step("download", "running", f"image {args.name} to {out.name} (qcow2; Harvester removes the downloader after)")
        n = 0
        with open(out, "wb") as f:
            for chunk in kube.raw_stream(path):
                f.write(chunk)
                n += len(chunk)
        step("download", "done", f"{n} bytes written")
        return EXIT_OK
    sc = _sc(kube, ((img.get("status") or {}).get("storageClassName")))
    path = hs.download_path(hs.backing_image_of(img, sc))
    step("download", "running", f"image {args.name} to {out.name} (gzip)")
    n = 0
    with open(out, "wb") as f:
        for chunk in kube.raw_stream(path):
            f.write(chunk)
            n += len(chunk)
    step("download", "done", f"{n} bytes written")
    return EXIT_OK


def _local_ip_for(host):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 443))
        return s.getsockname()[0]
    finally:
        s.close()


def img_upload(kube, args):
    """L'envoi d'un fichier : servi une seule fois par un petit serveur HTTP à
    jeton, que le cluster télécharge (source « download ») ; le serveur
    s'arrête quand l'image est prête. Le port doit être joignable depuis
    les nœuds du cluster."""
    import secrets
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    src = Path(args.file)
    if not src.is_file():
        raise ValueError(f"no file {args.file}")
    token = secrets.token_urlsafe(24)
    size = src.stat().st_size
    sent = {"n": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _head(self):
            if self.path.split("?", 1)[0] != f"/image/{token}":
                self.send_response(404)
                self.end_headers()
                return False
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            return True

        # vu sur harv1 : le téléchargeur de Longhorn demande d'abord un HEAD
        # (taille du fichier) ; sans réponse, « got 501 status code »
        def do_HEAD(self):   # noqa: N802
            self._head()

        def do_GET(self):   # noqa: N802
            if not self._head():
                return
            with open(src, "rb") as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    sent["n"] += len(chunk)

    try:
        srv = ThreadingHTTPServer(("0.0.0.0", int(args.port)), H)
    except OSError as e:
        # vu sur node1 : un autre service tenait le port ; le dire, pas une trace
        raise ValueError(f"port {args.port} cannot be used on this machine ({e.strerror}): "
                         "choose another one (--port, HARVESTER_OPS_IMAGE_UPLOAD_PORT)") from None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        host = args.advertise or _local_ip_for(kube.server_host() or "127.0.0.1")
        url = f"http://{host}:{srv.server_address[1]}/image/{token}"
        sc_name = args.storage_class or _default_class(kube)
        obj = hs.upload_image(args.namespace, args.display_name, url, args.storage_class,
                              checksum=args.checksum, file_name=args.file_name or src.name,
                              description=args.description or "",
                              sc_obj=kube.get(ho.K["storageclass"], None, sc_name) if sc_name else None)
        step("upload", "running", f"{src.name} ({size} bytes) offered to the cluster from {host}")
        made = kube.create(obj)
        return _wait_image(kube, args.namespace, made["metadata"]["name"], args.display_name, args.timeout, "upload")
    finally:
        srv.shutdown()
        srv.server_close()


IMAGE_ACTIONS = {"edit": img_edit, "clone": img_clone, "encrypt": lambda k, a: img_crypto(k, a, "encrypt"),
                 "decrypt": lambda k, a: img_crypto(k, a, "decrypt"), "download": img_download, "upload": img_upload,
                 "prepare-download": img_prepare_download}


def cmd_image(args):
    kube = kube_from(args)
    if args.action != "upload":
        hs.check_name(args.name or "", "image name")
    return IMAGE_ACTIONS[args.action](kube, args)


# ---------------------------------------------------------------------------
# « Advanced » (v1.64.0) : modèles et leurs versions, modèles cloud-init,
# classes de stockage complètes, secrets typés, clés SSH modifiables.
# ---------------------------------------------------------------------------

def cmd_template(args):
    kube = kube_from(args)
    ns, name = args.namespace, args.name
    tpl = kube.get(hadv.K_TEMPLATE, ns, name)
    if tpl is None:
        raise ValueError(f"no template {ns}/{name}")
    if args.action == "set-default":
        patch = hadv.set_default_patch(args.version)
        vns, vname = args.version.split("/", 1)
        v = kube.get(hadv.K_VERSION, vns, vname)
        if v is None or ((v.get("spec") or {}).get("templateId")) != f"{ns}/{name}":
            raise ValueError(f"{args.version} is not a version of {ns}/{name}")
        step("template", "running", f"{args.version} becomes the default version of {ns}/{name}")
        kube.patch(hadv.K_TEMPLATE, ns, name, patch)
        step("template", "done", "saved")
        return EXIT_OK
    if args.action == "delete-version":
        hadv.delete_version_check(tpl, args.version)
        vns, vname = args.version.split("/", 1)
        step("template", "running", f"version {args.version} deleted")
        kube.delete(hadv.K_VERSION, vns, vname)
        step("template", "done", "deleted")
        return EXIT_OK
    step("template", "running", f"template {ns}/{name} and its versions deleted")
    kube.delete(hadv.K_TEMPLATE, ns, name)

    def done(o):
        return (True, f"{ns}/{name} is gone") if o is None else (None, "deleting")
    return _wait(kube, hadv.K_TEMPLATE, ns, name, done, args.timeout, label="template")


def cmd_cloudtpl(args):
    kube = kube_from(args)
    ns, name = args.namespace, args.name
    cur = kube.get(hadv.K_CM, ns, name)
    if args.action == "delete":
        if cur is None or ((cur.get("metadata") or {}).get("labels") or {}).get(hadv.CLOUD_LABEL) not in ("user", "network"):
            raise ValueError(f"no cloud-init template {ns}/{name}")
        kube.delete(hadv.K_CM, ns, name)
        step("cloudtpl", "done", f"{ns}/{name} deleted")
        return EXIT_OK
    text = Path(args.file).read_text() if args.file else ""
    if args.action == "create":
        if cur is not None:
            raise ValueError(f"{ns}/{name} already exists")
        obj = hadv.cloud_template(name, ns, args.type or "user", text, args.description or "")
        kube.create(obj)
    else:
        if cur is None:
            raise ValueError(f"no cloud-init template {ns}/{name}")
        kind = ((cur.get("metadata") or {}).get("labels") or {}).get(hadv.CLOUD_LABEL)
        obj = hadv.cloud_template(name, ns, kind, text or (cur.get("data") or {}).get("cloudInit", ""),
                                  args.description if args.description is not None else "")
        cur["data"] = obj["data"]
        ann = (cur["metadata"].setdefault("annotations", {}))
        if args.description is not None:
            if args.description:
                ann[hadv.DESC] = args.description[:1000]
            else:
                ann.pop(hadv.DESC, None)
        kube.replace(cur)
    step("cloudtpl", "done", f"{ns}/{name} saved")
    return EXIT_OK


def cmd_storageclass(args):
    kube = kube_from(args)
    spec = _read_json(args.spec)
    obj = hadv.storage_class(spec)
    if kube.get(hs.K_SC, None, obj["metadata"]["name"]) is not None:
        raise ValueError(f"the storage class {obj['metadata']['name']} already exists")
    if spec.get("encrypted"):
        sns, sname = str(spec.get("secret")).split("/", 1)
        sec = kube.get("secrets", sns, sname)
        if sec is None or not hadv.crypto_secret_ok(sec):
            raise ValueError(f"{sns}/{sname} is not an encryption secret (CRYPTO_KEY_* keys)")
    step("storageclass", "running", f"storage class {obj['metadata']['name']} ({obj['provisioner']})")
    kube.create(obj)
    step("storageclass", "done", "created")
    return EXIT_OK


def cmd_secret(args):
    kube = kube_from(args)
    spec = _read_json(args.spec)
    ns, name = args.namespace, args.name
    if args.action == "create":
        if kube.get("secrets", ns, name) is not None:
            raise ValueError(f"the secret {ns}/{name} already exists")
        if spec.get("type") == "crypto":
            obj = hadv.crypto_secret(name, ns, spec.get("passphrase") or "", spec.get("cipher") or "aes-xts-plain64",
                                     spec.get("hash") or "sha256", spec.get("size") or "256", spec.get("pbkdf") or "argon2i")
        else:
            obj = hadv.typed_secret(name, ns, spec.get("type") or "Opaque", spec.get("fields") or {})
        step("secret", "running", f"secret {ns}/{name} ({obj['type']})")
        kube.create(obj)
    else:
        cur = kube.get("secrets", ns, name)
        if cur is None:
            raise ValueError(f"no secret {ns}/{name}")
        obj = hadv.secret_update(cur, spec.get("fields") or {})
        step("secret", "running", f"new values for {ns}/{name}")
        kube.replace(obj)
    step("secret", "done", "saved")
    return EXIT_OK


def cmd_sshkey(args):
    kube = kube_from(args)
    kp = kube.get("keypairs.harvesterhci.io", args.namespace, args.name)
    if kp is None:
        raise ValueError(f"no SSH key {args.namespace}/{args.name}")
    key = Path(args.public_key_file).read_text() if args.public_key_file else (kp.get("spec") or {}).get("publicKey")
    obj = hadv.keypair_update(kp, key, args.description)
    step("sshkey", "running", f"SSH key {args.namespace}/{args.name} changed")
    kube.replace(obj)

    def done(o):
        st = (o or {}).get("status") or {}
        fp = st.get("fingerPrint")
        return (True, f"fingerprint {fp}") if fp else (None, "Harvester computes the fingerprint")
    return _wait(kube, "keypairs.harvesterhci.io", args.namespace, args.name, done, 120, label="sshkey")


# ---------------------------------------------------------------------------
# v1.65.0 : le menu Networks de Harvester
# ---------------------------------------------------------------------------

def _gone(what):
    def done(o):
        return (True, f"{what} deleted") if o is None else (None, "deleting")
    return done


def cmd_clusternetwork(args):
    kube = kube_from(args)
    name = args.name
    if args.action == "create":
        obj = hnet.cluster_network(name, args.description or "")
        if kube.get(hnet.K_CN, None, name) is not None:
            raise ValueError(f"the cluster network {name} already exists")
        kube.create(obj)
        step("clusternetwork", "done", f"cluster network {name} created; give it a network configuration")
        return EXIT_OK
    rows = hnet.cluster_network_rows(kube.list(hnet.K_CN), kube.list(hnet.K_VC), [], kube.list(hnet.K_NAD), [])
    row = next((r for r in rows if r["name"] == name), None)
    if row is None:
        raise ValueError(f"no cluster network {name}")
    hnet.delete_cluster_network_check(row)
    kube.delete(hnet.K_CN, None, name)
    return _wait(kube, hnet.K_CN, None, name, _gone(f"cluster network {name}"), args.timeout, label="clusternetwork")


def _config_wait(kube, name, timeout, now=time.time):
    nodes = kube.list("nodes")
    t0 = now()

    def done(o):
        if o is None:
            return False, f"the configuration {name} disappeared"
        return hnet.config_settled(o, kube.list(hnet.K_VS), nodes, elapsed=now() - t0)
    return _wait(kube, hnet.K_VC, None, name, done, timeout, label="netconfig")


def _config_blockers(kube, vc):
    """Les VMs en marche sur les réseaux de VM du réseau de cluster d'une
    configuration : Harvester refuse de toucher au lien sous elles."""
    cn = ((vc or {}).get("spec") or {}).get("clusterNetwork")
    refs = [f"{n['metadata']['namespace']}/{n['metadata']['name']}" for n in kube.list(hnet.K_NAD)
            if hnet.nad_cluster_network(n) == cn]
    return hnet.vms_on(kube.list("virtualmachineinstances.kubevirt.io"), refs)


def cmd_netconfig(args):
    kube = kube_from(args)
    if args.action in ("create", "update"):
        spec = _read_json(args.spec)
        name = spec.get("name")
        cur = kube.get(hnet.K_VC, None, name) if name else None
        if args.action == "create":
            if cur is not None:
                raise ValueError(f"the configuration {name} already exists")
            obj = hnet.vlan_config(spec)
            if kube.get(hnet.K_CN, None, obj["spec"]["clusterNetwork"]) is None:
                raise ValueError(f"no cluster network {obj['spec']['clusterNetwork']}")
            kube.create(obj)
            step("netconfig", "running", f"configuration {name}: {', '.join(obj['spec']['uplink']['nics'])} "
                                         f"bonded ({obj['spec']['uplink']['bondOptions']['mode']})")
        else:
            if cur is None:
                raise ValueError(f"no configuration {name}")
            new = hnet.vlan_config(spec, current=cur)
            blockers = hnet.update_blockers(cur, new, kube.list("nodes"),
                                            kube.list("virtualmachineinstances.kubevirt.io"), kube.list(hnet.K_NAD))
            if blockers:
                raise ValueError("stop these VMs first, they use this cluster network: " + ", ".join(blockers))
            kube.replace(new)
            step("netconfig", "running", f"configuration {name} changed")
        return _config_wait(kube, name, args.timeout)
    name = args.name
    cur = kube.get(hnet.K_VC, None, name)
    if cur is None:
        raise ValueError(f"no configuration {name}")
    if ((cur.get("spec") or {}).get("clusterNetwork")) == "mgmt":
        raise ValueError("the configurations of mgmt are managed by Harvester")
    blockers = _config_blockers(kube, cur)
    if blockers:
        raise ValueError("stop these VMs first, they use this cluster network: " + ", ".join(blockers))
    if args.action == "migrate":
        patch = hnet.migrate_patch(args.target)
        if kube.get(hnet.K_CN, None, args.target) is None:
            raise ValueError(f"no cluster network {args.target}")
        kube.patch(hnet.K_VC, None, name, patch)
        step("netconfig", "running", f"configuration {name} moves to {args.target}")
        return _config_wait(kube, name, args.timeout)
    kube.delete(hnet.K_VC, None, name)
    return _wait(kube, hnet.K_VC, None, name, _gone(f"configuration {name}"), args.timeout, label="netconfig")


def cmd_vmnet(args):
    kube = kube_from(args)
    ns, name = args.namespace, args.name
    nad = kube.get(hnet.K_NAD, ns, name)
    if nad is None:
        raise ValueError(f"no VM network {ns}/{name}")
    out, changed = hnet.network_update(nad, _read_json(args.spec))
    if changed:
        running = hnet.vms_on(kube.list("virtualmachineinstances.kubevirt.io"), [f"{ns}/{name}"])
        if running:
            raise ValueError("its VLANs change only with its VMs stopped: " + ", ".join(running))
    kube.replace(out)
    step("vmnet", "done", f"VM network {ns}/{name} changed" + (" (VLANs)" if changed else ""))
    return EXIT_OK


def cmd_lb(args):
    kube = kube_from(args)
    if args.action == "delete":
        kube.delete(hnet.K_LB, args.namespace, args.name)
        return _wait(kube, hnet.K_LB, args.namespace, args.name,
                     _gone(f"load balancer {args.namespace}/{args.name}"), args.timeout, label="lb")
    spec = _read_json(args.spec)
    ns, name = spec.get("namespace") or "default", spec.get("name")
    cur = kube.get(hnet.K_LB, ns, name) if name else None
    if args.action == "create":
        if cur is not None:
            raise ValueError(f"the load balancer {ns}/{name} already exists")
        obj = hnet.load_balancer(spec)
        if obj["spec"].get("ipPool") and kube.get(hnet.K_POOL, None, obj["spec"]["ipPool"]) is None:
            raise ValueError(f"no IP pool {obj['spec']['ipPool']}")
        kube.create(obj)
        step("lb", "running", f"load balancer {ns}/{name} ({obj['spec']['ipam']})")
    else:
        if cur is None:
            raise ValueError(f"no load balancer {ns}/{name}")
        kube.replace(hnet.load_balancer(spec, current=cur))
        step("lb", "running", f"load balancer {ns}/{name} changed")

    def done(o):
        [row] = hnet.lb_rows([o]) if o else [{}]
        if not o:
            return False, "the load balancer disappeared"
        if row["address"] and row["ready"]:
            return True, f"{row['address']} -> {', '.join(row['backends']) or 'no backend'}"
        if row["address"]:
            # adresse obtenue, pas encore de VM prête derrière : c'est l'état
            # d'un équilibreur sans VM, pas un échec
            return True, f"{row['address']} ({row['message'] or 'no backend ready yet'})"
        return None, row["message"] or "waiting for an address"
    return _wait(kube, hnet.K_LB, ns, name, done, args.timeout, label="lb")


def cmd_ippool(args):
    kube = kube_from(args)
    if args.action in ("delete", "release"):
        pool = kube.get(hnet.K_POOL, None, args.name)
        if pool is None:
            raise ValueError(f"no IP pool {args.name}")
        if args.action == "release":
            kube.patch(hnet.K_POOL, None, args.name, hnet.release_patch(pool, args.ip))
            step("ippool", "running", f"{args.ip} released from {args.name}")

            def freed(o):
                left = ((o or {}).get("status") or {}).get("allocated") or {}
                return (True, f"{args.ip} is free") if args.ip not in left else (None, "releasing")
            return _wait(kube, hnet.K_POOL, None, args.name, freed, args.timeout, label="ippool")
        hnet.delete_pool_check(pool)
        kube.delete(hnet.K_POOL, None, args.name)
        return _wait(kube, hnet.K_POOL, None, args.name, _gone(f"IP pool {args.name}"), args.timeout, label="ippool")
    spec = _read_json(args.spec)
    name = spec.get("name")
    cur = kube.get(hnet.K_POOL, None, name) if name else None
    if args.action == "create":
        if cur is not None:
            raise ValueError(f"the IP pool {name} already exists")
        kube.create(hnet.ip_pool(spec))
    else:
        if cur is None:
            raise ValueError(f"no IP pool {name}")
        kube.replace(hnet.ip_pool(spec, current=cur))

    def done(o):
        [row] = hnet.pool_rows([o]) if o else [{}]
        if not o:
            return False, "the IP pool disappeared"
        if row["ready"]:
            return True, f"{row['available']} of {row['total']} addresses free"
        return (False, row["message"]) if row["ready"] is False and row["message"] else (None, "waiting for Harvester")
    return _wait(kube, hnet.K_POOL, None, name, done, args.timeout, label="ippool")


def cmd_hostnet(args):
    kube = kube_from(args)
    if args.action == "delete":
        cur = kube.get(hnet.K_HNC, None, args.name)
        if cur is None:
            raise ValueError(f"no host network {args.name}")
        hnet.delete_hostnet_check(cur)
        kube.delete(hnet.K_HNC, None, args.name)
        return _wait(kube, hnet.K_HNC, None, args.name, _gone(f"host network {args.name}"), args.timeout, label="hostnet")
    spec = _read_json(args.spec)
    name = spec.get("name")
    nodes = kube.list("nodes")
    cur = kube.get(hnet.K_HNC, None, name) if name else None
    if args.action == "create":
        if cur is not None:
            raise ValueError(f"the host network {name} already exists")
        obj = hnet.host_network(spec, nodes)
        if kube.get(hnet.K_CN, None, obj["spec"]["clusterNetwork"]) is None:
            raise ValueError(f"no cluster network {obj['spec']['clusterNetwork']}")
        kube.create(obj)
        step("hostnet", "running", f"host network {name}: {obj['spec']['clusterNetwork']}-br.{obj['spec']['vlanID']} "
                                   f"({obj['spec']['mode']})")
    else:
        if cur is None:
            raise ValueError(f"no host network {name}")
        kube.replace(hnet.host_network(spec, nodes, current=cur))
        step("hostnet", "running", f"host network {name} changed")
    t0 = time.time()
    return _wait(kube, hnet.K_HNC, None, name, lambda o: hnet.hostnet_settled(o, nodes, elapsed=time.time() - t0),
                 args.timeout, label="hostnet")


def cmd_netsetting(args):
    kube = kube_from(args)
    kind = args.name
    if kind not in hnet.NET_SETTINGS:
        raise ValueError("setting: " + ", ".join(hnet.NET_SETTINGS))
    spec = {"disable": True} if args.action == "clear" else _read_json(args.spec)
    value = hnet.setting_value(kind, spec)
    if kind == "storage-network" and value:
        running = [f"{v['metadata']['namespace']}/{v['metadata']['name']}"
                   for v in kube.list("virtualmachineinstances.kubevirt.io")]
        if running:
            raise ValueError("Harvester changes the storage network with every VM stopped; still running: "
                             + ", ".join(sorted(running)[:12]) + (" ..." if len(running) > 12 else ""))
        # le webhook compte les volumes, pas les VMs : une VM à peine arrêtée
        # garde ses volumes attachés quelques secondes (vu en réel sur harvlab)
        busy = hnet.attached_volumes(kube.list("volumes.longhorn.io", "longhorn-system"),
                                     kube.list("persistentvolumeclaims"))
        if busy:
            raise ValueError("Harvester changes the storage network with every volume detached; still attached: "
                             + ", ".join(busy[:12]) + (" ..." if len(busy) > 12 else ""))
    before = kube.get(hnet.K_SETTING, None, kind) or {}
    kube.patch(hnet.K_SETTING, None, kind, {"value": value})
    step("netsetting", "running", f"{kind}: " + (value or "back to the management network"))

    def done(o):
        # la condition d'avant peut encore dire True : attendre qu'elle bouge
        if (o or {}).get("metadata", {}).get("resourceVersion") == before.get("metadata", {}).get("resourceVersion"):
            return None, "waiting for Harvester"
        return hnet.setting_settled(o)
    return _wait(kube, hnet.K_SETTING, None, kind, done, args.timeout, label="netsetting")


# ---------------------------------------------------------------------------
# v1.67.0 : réglages de Harvester, paquet de support, kubeconfig sûr
# ---------------------------------------------------------------------------

def cmd_setting(args):
    kube = kube_from(args)
    if args.action == "test-backup-target":
        try:
            kube.run("get", "--raw", hset.BACKUP_HEALTH_PATH, timeout=60)
        except KubeError as e:
            step("setting", "error", "the backup target does not answer: " + refusal(e)[:240])
            return EXIT_FAIL
        step("setting", "done", "the backup target answers")
        return EXIT_OK
    name = args.name
    cur = kube.get(hset.K_SETTING, None, name)
    if cur is None:
        raise ValueError(f"no setting {name}")
    if args.action == "reset":
        value = ""
        hset.validate(name, "")
    else:
        raw = Path(args.value_file).read_text() if args.value_file else ""
        value = hset.validate(name, hset.unmask(name, raw, cur.get("value") or ""))
    before = (cur.get("metadata", {}).get("annotations") or {}).get(hset.HASH_ANN)
    kube.patch(hset.K_SETTING, None, name, {"value": value if value else None})
    step("setting", "running", f"{name}: " + ("back to the default" if not value else "new value written"))
    t0 = time.time()
    return _wait(kube, hset.K_SETTING, None, name,
                 lambda o: hset.settled(o or {}, before, elapsed=time.time() - t0), args.timeout, label="setting")


def cmd_supportbundle(args):
    kube = kube_from(args)
    if args.action == "delete":
        kube.delete(hset.K_BUNDLE, hset.BUNDLE_NS, args.name)
        return _wait(kube, hset.K_BUNDLE, hset.BUNDLE_NS, args.name,
                     lambda o: (True, f"support bundle {args.name} deleted") if o is None else (None, "deleting"),
                     120, label="supportbundle")
    obj = hset.bundle_manifest(_read_json(args.spec))
    name = obj["metadata"]["name"]
    for ns in obj["spec"].get("extraCollectionNamespaces") or []:
        if kube.get("namespaces", None, ns) is None:
            raise ValueError(f"namespace {ns} not found")
    kube.create(obj)
    step("supportbundle", "running", f"support bundle {name} requested")

    def done(o):
        st = (o or {}).get("status") or {}
        if o is None:
            return False, "the support bundle disappeared"
        if st.get("state") == "ready":
            return True, f"{name} ready: {st.get('filename')} ({st.get('filesize') or 0} bytes)"
        if st.get("state") == "error":
            return False, f"{name} failed"
        return None, f"collecting {st.get('progress') or 0} %"
    return _wait(kube, hset.K_BUNDLE, hset.BUNDLE_NS, name, done, args.timeout, label="supportbundle")


def cmd_kubeconfig(args):
    kube = kube_from(args)
    if args.action == "revoke":
        sa = kube.get("serviceaccounts", hset.KC_NS, f"kc-{args.name}")
        if sa is None:
            raise ValueError(f"no kubeconfig {args.name}")
        for kind in ("clusterrolebindings", "rolebindings"):
            for b in kube.list(kind, None, selector=f"{hset.KC_LABEL}={args.name}"):
                kube.delete(kind, b["metadata"].get("namespace"), b["metadata"]["name"])
        kube.delete("serviceaccounts", hset.KC_NS, f"kc-{args.name}")
        step("kubeconfig", "done", f"kubeconfig {args.name} revoked: its token no longer works")
        return EXIT_OK
    sa, binding, seconds = hset.kubeconfig_objects({"name": args.name, "role": args.role, "namespace": args.namespace or "",
                                                    "duration": args.duration, "description": args.description or ""})
    if kube.get("clusterroles", None, args.role) is None:
        raise ValueError(f"the role {args.role} does not exist on this cluster")
    if args.namespace and kube.get("namespaces", None, args.namespace) is None:
        raise ValueError(f"no namespace {args.namespace}")
    if kube.get("serviceaccounts", hset.KC_NS, sa["metadata"]["name"]) is not None:
        raise ValueError(f"a kubeconfig {args.name} exists already: revoke it first")
    if kube.get("namespaces", None, hset.KC_NS) is None:
        kube.create({"apiVersion": "v1", "kind": "Namespace",
                     "metadata": {"name": hset.KC_NS, "labels": {"harvester-ops.io/managed": "true"}}})
    kube.create(sa)
    kube.create(binding)
    step("kubeconfig", "running", f"account kc-{args.name} with the role {args.role} "
                                  + (f"in {args.namespace}" if args.namespace else "on the cluster"))
    token = kube.run("create", "token", sa["metadata"]["name"], "-n", hset.KC_NS, f"--duration={seconds}s").strip()
    vip = ((kube.get("configmaps", "harvester-system", "vip") or {}).get("data") or {}).get("ip") or kube.server_host()
    ca = ((kube.get("configmaps", hset.KC_NS, "kube-root-ca.crt") or {}).get("data") or {}).get("ca.crt") or ""
    text = hset.kubeconfig_yaml(args.cluster_name or args.cluster or "harvester", f"https://{vip}:6443", ca, token,
                                sa["metadata"]["name"])
    out = Path(args.out)
    out.touch(mode=0o600)
    out.write_text(text)
    step("kubeconfig", "done", f"kubeconfig {args.name} written, expires {sa['metadata']['annotations'][hset.KC_EXPIRES]}")
    return EXIT_OK


def cmd_device(args):
    """v1.68.0 : passthrough PCI et USB, fonctions virtuelles SR-IOV."""
    kube = kube_from(args)
    names = list(dict.fromkeys(args.name or []))
    if not names:
        raise ValueError("--name: at least one device")
    for n in names:
        hdev.check_name(n)
    addons = [a for a in [kube.get("addons.harvesterhci.io", hdev.ADDON[0], hdev.ADDON[1])] if a]
    if not hdev.addon_enabled(addons):
        raise ValueError("the pcidevices-controller add-on is disabled: enable it in Add-ons first")
    act = args.action
    if act == "sriov":
        if len(names) != 1:
            raise ValueError("sriov: one network device at a time")
        name = names[0]
        dev = kube.get(hdev.K_SRIOV, None, name)
        if dev is None:
            raise ValueError(f"no SR-IOV network device {name}")
        patch = hdev.sriov_patch(dev, args.vfs, kube.list(hdev.K_PCICLAIM, None))
        n = patch["spec"]["numVFs"]
        kube.patch(hdev.K_SRIOV, None, name, patch)
        step("device", "running", f"{name}: " + (f"{n} virtual functions requested" if n else "SR-IOV disabled"))
        return _wait(kube, hdev.K_SRIOV, None, name, lambda o: hdev.sriov_settled(o, n), args.timeout, label="device")
    pci = act.startswith("pci-")
    enable = act.endswith("-enable")
    kind, ckind = (hdev.K_PCI, hdev.K_PCICLAIM) if pci else (hdev.K_USB, hdev.K_USBCLAIM)
    vms = kube.list("virtualmachines.kubevirt.io", None)
    running = hdev.running_set(kube.list("virtualmachineinstances.kubevirt.io", None))
    devs = {}
    for n in names:                                # tout vérifier avant le premier geste
        d = kube.get(kind, None, n)
        if d is None:
            raise ValueError(f"no {'PCI' if pci else 'USB'} device {n}")
        claim = kube.get(ckind, None, n)
        if enable and claim is not None:
            raise ValueError(f"{n}: passthrough is already enabled")
        if not enable and claim is None:
            raise ValueError(f"{n}: passthrough is not enabled")
        if enable and pci:
            hdev.pci_claim(d, args.user)            # refus sans groupe IOMMU
        if not enable:
            hdev.pci_disable_check(n, vms, running) if pci else hdev.usb_disable_check(d, vms, running)
        devs[n] = d
    rc = EXIT_OK
    for n, d in devs.items():
        if enable:
            kube.create(hdev.pci_claim(d, args.user) if pci else hdev.usb_claim(d, args.user))
            step("device", "running", f"{n}: passthrough requested")
        else:
            # Harvester 1.8 laisse l'allocation d'une carte retirée d'une VM
            # arrêtée et refuse alors de la rendre ; la 1.9 la recalcule :
            # on fait de même (vu sur harvlab, pcidevices v1.8.2)
            for vns, vname, patch in (hdev.stale_allocations(n, vms, running) if pci else []):
                kube.patch("virtualmachines.kubevirt.io", vns, vname, patch)
                step("device", "running", f"{vns}/{vname} is stopped and no longer lists {n}: "
                                          "its stale allocation is cleared, as Harvester 1.9 does")
            kube.delete(ckind, None, n)
            step("device", "running", f"{n}: passthrough being disabled")
    for n in devs:
        if pci:
            done = (lambda o, e=enable: hdev.pci_settled(o, e))
            r = _wait(kube, ckind, None, n, done, args.timeout, label="device")
        else:
            def done(o, n=n, e=enable):
                return hdev.usb_settled(o, kube.get(ckind, None, n), e)
            r = _wait(kube, kind, None, n, done, args.timeout, label="device")
        rc = rc if r == EXIT_OK else r
    return rc


# ---------------------------------------------------------------------------
# v1.69.0 : mise à jour de Harvester (versions, lancement en ligne ou depuis
# un ISO de la console, suivi jusqu'au bout, journaux, Dismiss, abandon)
# ---------------------------------------------------------------------------

HARVESTER_PROXY = "/api/v1/namespaces/harvester-system/services/https:harvester:8443/proxy"


def _server_version(kube):
    return ((kube.get("settings.harvesterhci.io", None, "server-version") or {}).get("value")) or ""


def _serve_iso(path, port, host_hint, advertise=None):
    """Guichet à jeton pour un ISO : HEAD avec la taille, GET entier ou par
    plage (le pod de Harvester lit les 128 premiers octets, puis l'importeur
    CDI télécharge le tout ; vu dans image/cdi/common.go). Plusieurs
    requêtes par jeton ; arrêté par l'appelant."""
    import secrets
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    token = secrets.token_urlsafe(24)
    size = Path(path).stat().st_size
    stats = {"bytes": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _range(self):
            m = re.match(r"^bytes=(\d+)-(\d*)$", self.headers.get("Range") or "")
            if not m:
                return None
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else size - 1
            return (a, min(b, size - 1)) if a < size else None

        def _head(self, body):
            if self.path.split("?", 1)[0] != f"/iso/{token}":
                self.send_response(404)
                self.end_headers()
                return None
            rng = self._range() if body else None
            if rng:
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {rng[0]}-{rng[1]}/{size}")
                self.send_header("Content-Length", str(rng[1] - rng[0] + 1))
            else:
                self.send_response(200)
                self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            return rng or (0, size - 1)

        def do_HEAD(self):   # noqa: N802
            self._head(False)

        def do_GET(self):   # noqa: N802
            rng = self._head(True)
            if rng is None:
                return
            left = rng[1] - rng[0] + 1
            with open(path, "rb") as f:
                f.seek(rng[0])
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    left -= len(chunk)
                    stats["bytes"] += len(chunk)

    try:
        srv = ThreadingHTTPServer(("0.0.0.0", int(port)), H)
    except OSError as e:
        raise ValueError(f"port {port} cannot be used on this machine ({e.strerror}): "
                         "choose another one (--port, HARVESTER_OPS_IMAGE_UPLOAD_PORT)") from None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = advertise or _local_ip_for(host_hint or "127.0.0.1")
    return srv, f"http://{host}:{srv.server_address[1]}/iso/{token}", stats


def _iso_release(path):
    """Le harvester-release.yaml de l'ISO, lu par xorriso sans le monter."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "hr.yaml"
        r = subprocess.run(["xorriso", "-osirrox", "on", "-indev", str(path), "-extract", "/harvester-release.yaml",
                            str(out)], capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not out.exists():
            raise ValueError(f"{Path(path).name} is not a Harvester ISO (no harvester-release.yaml)")
        return hup.release_info(out.read_text())


def _sha512(path):
    import hashlib
    h = hashlib.sha512()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def _follow_upgrade(kube, name, timeout, sleep=None, now=None, api_grace=2700):
    """Suit l'Upgrade jusqu'au bout. L'API du cluster disparaît pendant la
    mise à jour de RKE2 et le redémarrage d'un nœud (surtout en mononœud) :
    une coupure de moins de 45 min n'est pas un échec."""
    sleep, now = sleep or time.sleep, now or time.time      # résolus à l'appel (tests)
    deadline = now() + timeout
    last, down_since = None, None
    while now() < deadline:
        try:
            u = kube.get(hup.K_UPGRADE, hup.NS, name)
            img = None
            if u and ((u.get("status") or {}).get("imageID")):
                ins, _, iname = u["status"]["imageID"].partition("/")
                img = kube.get(hup.K_IMAGE, ins, iname)
        except (KubeError, subprocess.SubprocessError, OSError):
            if down_since is None:
                down_since = now()
                step("upgrade", "running", "the cluster API does not answer (expected while Kubernetes and the "
                                           "hosts restart): waiting")
            elif now() - down_since > api_grace:
                step("upgrade", "error", f"the cluster API has not answered for {int(api_grace / 60)} min")
                return EXIT_FAIL
            sleep(15)
            continue
        if down_since is not None:
            step("upgrade", "running", f"the cluster API answers again after {int(now() - down_since)} s")
            down_since = None
        res, msg = hup.settled(u)
        v = hup.upgrade_view(u, img) if u else None
        if v and v["image_progress"] is not None and not any(
                c["type"] == "ImageReady" and c["status"] == "True" for c in v["conditions"]):
            msg = f"{msg}, ISO {v['image_progress']} %"
        if msg != last:
            step("upgrade", "running" if res is None else ("done" if res else "error"), msg)
            last = msg
        if res is True:
            return EXIT_OK
        if res is False:
            return EXIT_FAIL
        sleep(15)
    step("upgrade", "error", f"not finished after {timeout} s; the upgrade goes on in Harvester, follow it again")
    return EXIT_FAIL


def cmd_upgrade(args):
    kube = kube_from(args)
    act = args.action
    if act == "version-add":
        v = hup.version_from_yaml(Path(args.version_file).read_text())
        if kube.get(hup.K_VERSION, hup.NS, v["name"]) is not None:
            raise ValueError(f"version {v['name']} already exists")
        kube.create(hup.version_manifest(v["name"], v["iso_url"], v["checksum"], v["release_date"],
                                         v["min_upgradable"], v["tags"]))
        ok, why = hup.eligible(_server_version(kube), v["name"], v["min_upgradable"])
        step("upgrade", "done", f"version {v['name']} added" + ("" if ok else f" (not usable from this cluster: {why})"))
        return EXIT_OK
    if act == "version-delete":
        name = hup.check_name(args.name, "version")
        kube.delete(hup.K_VERSION, hup.NS, name)
        step("upgrade", "done", f"version {name} deleted")
        return EXIT_OK
    if act in ("dismiss", "abort", "resume-node", "follow", "logs"):
        name = hup.check_name(args.name, "upgrade")
        u = kube.get(hup.K_UPGRADE, hup.NS, name)
        if u is None:
            raise ValueError(f"no upgrade {name}")
        v = hup.upgrade_view(u)
        if act == "dismiss":
            if v["completed"] is None:
                raise ValueError("the upgrade is still running")
            kube.patch(hup.K_UPGRADE, hup.NS, name, {"metadata": {"labels": {hup.L_READ: "true"}}})
            step("upgrade", "done", f"{name} dismissed: Harvester removes its log collector and volume")
            return EXIT_OK
        if act == "abort":
            if not v["can_abort"]:
                raise ValueError("Harvester refuses to stop an upgrade once its hosts are being upgraded"
                                 if v["completed"] is None else "the upgrade is already over")
            kube.delete(hup.K_UPGRADE, hup.NS, name)
            step("upgrade", "running", f"{name} deleted: Harvester cleans up (repository, ISO image, logs)")
            return _wait(kube, hup.K_UPGRADE, hup.NS, name,
                         lambda o: (True, f"{name} stopped and cleaned up") if o is None else (None, "cleaning up"),
                         900, label="upgrade")
        if act == "resume-node":
            kube.patch(hup.K_UPGRADE, hup.NS, name, hup.pause_patch(u, args.node, "unpause"))
            step("upgrade", "done", f"{args.node} resumed")
            return EXIT_OK
        if act == "follow":
            return _follow_upgrade(kube, name, args.timeout)
        # journaux : fabriquer une archive puis la télécharger (proxy de service)
        log = v["log_name"]
        if not log:
            raise ValueError("this upgrade keeps no logs (disabled, or already dismissed)")
        if not args.out:
            raise ValueError("--out: where to write the archive")
        base = f"{HARVESTER_PROXY}/v1/harvester/harvesterhci.io.upgradelogs/{hup.NS}/{log}"
        archive = json.loads(kube.run("create", "--raw", f"{base}?action=generate", "-f", "-", input="{}") or '""')
        step("upgrade", "running", f"packaging the logs: {archive}")

        def ready(o):
            a = (((o or {}).get("status") or {}).get("archives") or {}).get(archive) or {}
            if a.get("reason"):
                return False, a["reason"]
            return (True, f"archive {archive} ready") if a.get("ready") else (None, "packaging")
        if _wait(kube, hup.K_UPGRADELOG, hup.NS, log, ready, 900, label="upgrade") != EXIT_OK:
            return EXIT_FAIL
        out = Path(args.out)
        out.touch(mode=0o600)
        with open(out, "wb") as fh:
            for chunk in kube.raw_stream(f"{base}/download?archiveName={archive}"):
                fh.write(chunk)
        step("upgrade", "done", f"logs saved ({out.stat().st_size} bytes)")
        return EXIT_OK

    # start
    current = _server_version(kube)
    ups = kube.list(hup.K_UPGRADE, hup.NS)
    busy = hup.running(ups)
    if busy:
        raise ValueError(f"upgrade {busy} is still in progress")
    pend = hup.cleanup_pending(ups)
    if pend:
        raise ValueError("Harvester is still cleaning up after " + ", ".join(pend))
    srv = None
    try:
        if args.iso:
            iso = Path(args.iso)
            if not iso.is_file():
                raise ValueError(f"no ISO {args.iso}")
            rel = _iso_release(iso)
            ok, why = hup.eligible(current, rel["harvester"], rel["min_upgradable"])
            if ok is False:
                raise ValueError(f"{iso.name} ({rel['harvester']}) cannot upgrade this cluster ({current}): {why}")
            if args.checksum:
                step("upgrade", "running", f"checking the SHA-512 of {iso.name}")
                if _sha512(iso) != args.checksum.strip().lower():
                    raise ValueError(f"{iso.name}: SHA-512 mismatch, the file is damaged")
            srv, url, stats = _serve_iso(iso, args.port, kube.server_host(), args.advertise)
            step("upgrade", "running", f"{iso.name} ({rel['harvester']}) offered to the cluster from "
                                       f"{url.split('/iso/')[0]}")
            img = kube.create(hup.os_image_manifest(f"harvester-{rel['harvester']}", url, args.checksum or ""))
            iname = img["metadata"]["name"]

            def imported(o):
                st = (o or {}).get("status") or {}
                for c in st.get("conditions") or []:
                    if c.get("type") == "RetryLimitExceeded" and c.get("status") == "True":
                        return False, c.get("message") or "import failed"
                if any(c.get("type") == "Imported" and c.get("status") == "True" for c in st.get("conditions") or []):
                    return True, f"ISO imported ({stats['bytes']} bytes sent)"
                return None, f"importing the ISO: {st.get('progress') or 0} %"
            if _wait(kube, hup.K_IMAGE, hup.NS, iname, imported, 7200, label="upgrade") != EXIT_OK:
                return EXIT_FAIL
            man = hup.upgrade_manifest(image=f"{hup.NS}/{iname}", log=not args.no_log,
                                       skip_single=args.skip_single_replica)
            target = rel["harvester"]
        else:
            name = hup.check_name(args.version, "version")
            ver = kube.get(hup.K_VERSION, hup.NS, name)
            if ver is None and args.version_file:
                v = hup.version_from_yaml(Path(args.version_file).read_text())
                ver = kube.create(hup.version_manifest(v["name"], v["iso_url"], v["checksum"], v["release_date"],
                                                       v["min_upgradable"], v["tags"]))
                step("upgrade", "running", f"version {v['name']} added")
            if ver is None:
                raise ValueError(f"no version {name}: add it first")
            ok, why = hup.eligible(current, name, (ver.get("spec") or {}).get("minUpgradableVersion"))
            if ok is False:
                raise ValueError(f"{name} cannot upgrade this cluster ({current}): {why}")
            man = hup.upgrade_manifest(version=name, log=not args.no_log, skip_single=args.skip_single_replica)
            target = name
        made = kube.create(man)
        uname = made["metadata"]["name"]
        step("upgrade", "running", f"{uname}: {current} -> {target} started")
        if srv is not None:
            # l'ISO est importé : le guichet n'est plus utile
            srv.shutdown()
            srv.server_close()
            srv = None
        return _follow_upgrade(kube, uname, args.timeout)
    finally:
        if srv is not None:
            srv.shutdown()
            srv.server_close()


# ---------------------------------------------------------------------------
# v1.70.0 : Monitoring & Logging (sorties, flux, AlertmanagerConfig)
# ---------------------------------------------------------------------------

_ML_KINDS = {"Output": hml.K_OUTPUT, "ClusterOutput": hml.K_COUTPUT, "Flow": hml.K_FLOW, "ClusterFlow": hml.K_CFLOW,
             "AlertmanagerConfig": hml.K_AMC}


def _ml_secrets(kube, ns, secrets):
    """Les valeurs saisies d'un champ secret deviennent un Secret du
    namespace de l'objet (créé ou complété) ; seule la référence reste.
    Rend True si une valeur a été écrite."""
    import base64
    wrote = False
    for field, ref in (secrets or {}).items():
        if not ref or ref.get("value") in (None, ""):
            continue
        name, key = hml.check_name(ref.get("name"), "secret"), str(ref.get("key") or "").strip()
        if not key:
            raise ValueError(f"{field}: the key of the secret")
        data = {key: base64.b64encode(str(ref["value"]).encode()).decode()}
        cur = kube.get("secrets", ns, name)
        if cur is None:
            kube.create({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                         "metadata": {"name": name, "namespace": ns, "labels": {"harvester-ops.io/managed": "true"}},
                         "data": data})
            step("monlog", "running", f"secret {ns}/{name} created for {field}")
        else:
            kube.patch("secrets", ns, name, {"data": data})
            step("monlog", "running", f"secret {ns}/{name}: key {key} set")
        ref.pop("value", None)
        wrote = True
    return wrote


def _ml_put(kube, obj):
    kind = _ML_KINDS[obj["kind"]]
    ns, name = obj["metadata"]["namespace"], obj["metadata"]["name"]
    cur = kube.get(kind, ns, name)
    if cur is None:
        kube.create(obj)
        return "created"
    if cur.get("spec") == obj["spec"]:
        return "unchanged"
    obj["metadata"]["resourceVersion"] = cur["metadata"]["resourceVersion"]
    kube.replace(obj)
    return "updated"


def cmd_monlog(args):
    kube = kube_from(args)
    act = args.action
    if act.endswith("-delete"):
        kind = args.kind
        if kind not in _ML_KINDS:
            raise ValueError("--kind: " + ", ".join(_ML_KINDS))
        ns, name = hml.check_name(args.namespace, "namespace"), hml.check_name(args.name)
        if kind in ("Output", "ClusterOutput"):
            flows = hml.flow_rows(kube.list(hml.K_FLOW, None) + kube.list(hml.K_CFLOW, None))
            users = [f"{f['namespace']}/{f['name']}" for f in flows
                     if (kind == "Output" and f["namespace"] == ns and name in f["local"])
                     or (kind == "ClusterOutput" and name in f["global"])]
            if users:
                raise ValueError(f"{kind} {name} is used by " + ", ".join(users) + ": change or delete them first")
        kube.delete(_ML_KINDS[kind], ns, name)
        step("monlog", "done", f"{kind} {ns}/{name} deleted")
        return EXIT_OK
    spec = _read_json(args.spec)
    if act == "output-apply":
        obj = hml.output_manifest(spec)
    elif act == "flow-apply":
        outs = hml.output_rows(kube.list(hml.K_OUTPUT, None) + kube.list(hml.K_COUTPUT, None))
        obj = hml.flow_manifest(spec, outs)
    else:
        obj = hml.amc_manifest(spec)
        for r in spec.get("receivers") or []:
            _ml_secrets(kube, obj["metadata"]["namespace"], {k: v for k, v in r.items() if isinstance(v, dict)})
        obj = hml.amc_manifest(spec)
    kind, ns, name = obj["kind"], obj["metadata"]["namespace"], obj["metadata"]["name"]
    audit = (obj["spec"].get("loggingRef") or "") == hml.AUDIT_REF
    lg = hml.logging_of(kube.list(hml.K_LOGGING, None), audit) if kind != "AlertmanagerConfig" else None
    before = hml.checks(lg)
    wrote = act == "output-apply" and _ml_secrets(kube, ns, spec.get("secrets"))
    what = _ml_put(kube, obj)
    if what == "unchanged" and wrote:
        what = "updated (secret)"          # fluentd reçoit la nouvelle valeur : un contrôle suit
    step("monlog", "running", f"{kind} {ns}/{name} {what}")
    if kind == "AlertmanagerConfig":
        addon = hml.addon_state([kube.get("addons.harvesterhci.io", *hml.ADDON_MON)], hml.ADDON_MON)
        step("monlog", "done", f"{name} saved" + ("" if addon["enabled"] else
                                                  " (rancher-monitoring is disabled: it takes effect once enabled)"))
        return EXIT_OK
    addon = hml.addon_state([kube.get("addons.harvesterhci.io", *hml.ADDON_LOG)], hml.ADDON_LOG)
    if not addon["enabled"]:
        step("monlog", "done", f"{name} saved (rancher-logging is disabled: it takes effect once enabled)")
        return EXIT_OK
    t0, seen = time.time(), {}

    def done(o):
        # l'objet accepté par l'opérateur, puis la configuration de fluentd
        # qu'il en tire acceptée par son contrôle : sinon fluentd garde
        # l'ancienne et rien n'arrive, sans autre signe
        res, msg = hml.settled(o, deadline_passed=time.time() - t0 > args.grace)
        if res is not True or lg is None or what == "unchanged" or (kind.endswith("Output") and not (o.get("status") or {}).get("active")):
            return res, msg
        seen.setdefault("at", time.time())
        v = hml.config_verdict(before, hml.checks(hml.logging_of(kube.list(hml.K_LOGGING, None), audit)))
        if v is None:
            if time.time() - seen["at"] > ML_CHECK_GRACE:
                return True, msg + "; fluentd's configuration unchanged"
            return None, "waiting for fluentd's configuration check"
        if v[0]:
            return True, f"{msg}; fluentd configuration {v[1]} checked"
        return False, (f"fluentd refused its new configuration ({v[1]}): {_ml_config_error(kube, v[1]) or 'see the configcheck pod'}; "
                       "the previous configuration stays in place")
    return _wait(kube, _ML_KINDS[kind], ns, name, done, args.timeout, label="monlog")


ML_CHECK_GRACE = 180


def _ml_config_error(kube, conf_hash):
    try:
        return hml.config_error(kube.run("logs", "-n", hml.CTRL_NS, "-l", f"logging.banzaicloud.io/config-hash={conf_hash}",
                                         "--tail", "40", timeout=30))
    except Exception:      # noqa: BLE001 : le journal manque, le verdict reste
        return ""


# ---------------------------------------------------------------------------
# v1.71.0 : imports de VM (vm-import-controller : VMware, OpenStack, OVA)
# ---------------------------------------------------------------------------

VMI_STUCK_AFTER = 300          # s sans changer d'état avant de lire le journal du contrôleur
VMI_SOURCE_WAIT = 180          # s : une source se vérifie en quelques secondes
VMI_SOURCE_LOG_AFTER = 15      # s : une source toujours sans état fait lire le journal


def _vmi_log(kube, name):
    """Les lignes d'erreur du journal du contrôleur qui citent `name` (le
    statut des sources et des imports ne porte pas la raison)."""
    try:
        text = kube.run("logs", "-n", hvi.CTRL_NS, f"deploy/{hvi.CTRL_DEPLOY}", "--tail", "3000", timeout=30)
    except Exception:      # noqa: BLE001 : journal illisible, on n'en dit rien
        return []
    return hvi.log_lines(text, name)


def _vmi_secret(kube, ns, secret):
    """Crée ou complète le secret d'une source ; rend son nom."""
    import base64
    data = {k: base64.b64encode(v.encode()).decode() for k, v in secret["data"].items()}
    cur = kube.get("secrets", ns, secret["name"])
    if cur is None:
        kube.create({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                     "metadata": {"name": secret["name"], "namespace": ns, "labels": {"harvester-ops.io/managed": "true"}},
                     "data": data})
        step("vmimport", "running", f"secret {ns}/{secret['name']} created")
    else:
        kube.patch("secrets", ns, secret["name"], {"data": data})
        step("vmimport", "running", f"secret {ns}/{secret['name']}: {', '.join(sorted(secret['data']))} set")


def _vmi_sources(kube):
    return hvi.source_rows({t: kube.list(hvi.K_SRC[t], None) for t in hvi.TYPES})


def _vmi_wait_source(kube, t, ns, name, timeout):
    """Une source neuve est vérifiée aussitôt : prête, pas prête, ou sans
    état (secret absent, identifiants refusés : le contrôleur réessaie sans
    fin, seul son journal le dit ; vu contre vcsim, lu après 15 s)."""
    t0 = time.time()

    def done(o):
        state = hvi.source_state(o)
        if state == "ready":
            return True, f"{hvi.KIND[t]} {ns}/{name} ready: the controller reached it"
        if state == "notready":
            lines = _vmi_log(kube, name)
            return False, f"{hvi.KIND[t]} {ns}/{name} not ready: " + (hvi.log_error(lines[-1]) if lines else "the controller could not reach it")
        if time.time() - t0 > VMI_SOURCE_LOG_AFTER:
            lines = _vmi_log(kube, name)
            if lines:
                return False, f"{hvi.KIND[t]} {ns}/{name} never checked: {hvi.log_error(lines[-1])}"
        return None, "waiting for the controller to check the source"
    rc = _wait(kube, hvi.K_SRC[t], ns, name, done, timeout, label="vmimport")
    if rc != EXIT_OK and hvi.source_state(kube.get(hvi.K_SRC[t], ns, name)) == "pending":
        # jamais vérifiée : secret illisible, identifiants refusés... le journal seul le dit
        lines = _vmi_log(kube, name)
        step("vmimport", "error", f"{hvi.KIND[t]} {ns}/{name} never checked: "
             + (hvi.log_error(lines[-1]) if lines else "see the controller log"))
    return rc


def _vmi_follow(kube, ns, name, timeout, sleep=None, now=None):
    """Suit l'import jusqu'à la VM en marche. La progression est celle des
    images ; un état figé plus de VMI_STUCK_AFTER s fait lire le journal du
    contrôleur, qui seul dit pourquoi l'import boucle."""
    sleep = sleep or time.sleep
    now = now or time.time
    deadline, last_msg, phase, since = now() + timeout, None, None, now()
    while now() < deadline:
        o = kube.get(hvi.K_IMPORT, ns, name)
        images = kube.list(hvi.K_IMAGE, ns, selector=f"{hvi.L_IMPORTED}=true") if o else []
        cur = ((o or {}).get("status") or {}).get("importStatus") or ""
        if cur != phase:
            phase, since = cur, now()
        stuck = ""
        if o and now() - since > VMI_STUCK_AFTER and cur not in (hvi.DONE,) + hvi.FAILED:
            stuck = hvi.stuck_reason(_vmi_log(kube, name))
            if not stuck and cur == "":
                src = ((o.get("spec") or {}).get("sourceCluster") or {})
                t = hvi.TYPE_OF_KIND.get(str(src.get("kind") or "").lower())
                s = kube.get(hvi.K_SRC[t], src.get("namespace") or ns, src.get("name")) if t else None
                if hvi.source_state(s) != "ready":
                    stuck = f"the source {src.get('name')} is not ready: check it in the Sources tab"
        res, msg = hvi.settled(o, images, stuck)
        if res is False and o and cur in hvi.FAILED and msg.endswith("see the controller log"):
            lines = _vmi_log(kube, name)
            if lines:
                msg = f"{cur}: {hvi.log_error(lines[-1])}"
        if msg != last_msg:
            step("vmimport", "running" if res is None else ("done" if res else "error"), msg)
            last_msg = msg
        if res is True:
            return EXIT_OK
        if res is False:
            return EXIT_FAIL
        sleep(10)
    step("vmimport", "error", f"not finished after {timeout} s; the import goes on in Harvester, follow it again")
    return EXIT_FAIL


def cmd_vmimport(args):
    kube = kube_from(args)
    act = args.action
    if act in ("source-apply", "source-recheck"):
        if act == "source-apply":
            spec = _read_json(args.spec)
            src, secret = hvi.source_manifest(spec)
        else:
            t = args.type
            if t not in hvi.TYPES:
                raise ValueError("--type: vmware, openstack or ova")
            cur = kube.get(hvi.K_SRC[t], hvi.check_name(args.namespace, "namespace"), hvi.check_name(args.name))
            if cur is None:
                raise ValueError(f"no {hvi.KIND[t]} {args.namespace}/{args.name}")
            src, secret = {"apiVersion": hvi.API, "kind": hvi.KIND[t],
                           "metadata": {"name": args.name, "namespace": args.namespace}, "spec": cur.get("spec") or {}}, None
        t = hvi.TYPE_OF_KIND[src["kind"].lower()]
        ns, name = src["metadata"]["namespace"], src["metadata"]["name"]
        imports = kube.list(hvi.K_IMPORT, None)
        cur = kube.get(hvi.K_SRC[t], ns, name)
        if cur is not None:
            users = hvi.source_users({"type": t, "namespace": ns, "name": name}, imports)
            if users:
                raise ValueError(f"{hvi.KIND[t]} {ns}/{name} is used by imports in progress: " + ", ".join(users))
        if secret:
            _vmi_secret(kube, ns, secret)
        elif (src["spec"].get("credentials") or {}).get("name") and act == "source-apply":
            ref = src["spec"]["credentials"]
            if kube.get("secrets", ref["namespace"], ref["name"]) is None:
                raise ValueError(f"no secret {ref['namespace']}/{ref['name']}")
        if cur is not None:
            # une source prête n'est jamais revérifiée : la recréer la fait vérifier
            kube.delete(hvi.K_SRC[t], ns, name)
            for _ in range(30):
                if kube.get(hvi.K_SRC[t], ns, name) is None:
                    break
                time.sleep(1)
        kube.create(src)
        step("vmimport", "running", f"{hvi.KIND[t]} {ns}/{name} {'checked again' if cur is not None else 'created'}")
        return _vmi_wait_source(kube, t, ns, name, min(args.timeout, VMI_SOURCE_WAIT))
    if act == "source-delete":
        t = args.type
        if t not in hvi.TYPES:
            raise ValueError("--type: vmware, openstack or ova")
        ns, name = hvi.check_name(args.namespace, "namespace"), hvi.check_name(args.name)
        cur = kube.get(hvi.K_SRC[t], ns, name)
        if cur is None:
            raise ValueError(f"no {hvi.KIND[t]} {ns}/{name}")
        users = hvi.source_users({"type": t, "namespace": ns, "name": name}, kube.list(hvi.K_IMPORT, None))
        if users:
            raise ValueError(f"{hvi.KIND[t]} {ns}/{name} is used by imports in progress: " + ", ".join(users))
        kube.delete(hvi.K_SRC[t], ns, name)
        ref = (cur.get("spec") or {}).get("credentials") or {}
        if args.with_secret and ref.get("name"):
            sec = kube.get("secrets", ref.get("namespace") or ns, ref["name"])
            if sec and ((sec.get("metadata") or {}).get("labels") or {}).get("harvester-ops.io/managed") == "true":
                kube.delete("secrets", ref.get("namespace") or ns, ref["name"])
                step("vmimport", "running", f"secret {ref['name']} deleted")
        step("vmimport", "done", f"{hvi.KIND[t]} {ns}/{name} deleted")
        return EXIT_OK
    if act == "import-create":
        spec = _read_json(args.spec)
        nads = [f"{(n.get('metadata') or {}).get('namespace')}/{(n.get('metadata') or {}).get('name')}"
                for n in kube.list("network-attachment-definitions.k8s.cni.cncf.io", None)]
        classes = [(c.get("metadata") or {}).get("name") for c in kube.list(ho.K["storageclass"])
                   if (c.get("parameters") or {}).get("harvesterhci.io/isInternalStorageClass") != "true"]
        obj = hvi.import_manifest(spec, _vmi_sources(kube), nads, classes)
        ns, name = obj["metadata"]["namespace"], obj["metadata"]["name"]
        if kube.get(hvi.K_IMPORT, ns, name) is not None:
            raise ValueError(f"an import {ns}/{name} already exists")
        final = hvi.imported_name(spec["source"]["type"], obj["spec"]["virtualMachineName"])
        if final:
            vm = kube.get("virtualmachines.kubevirt.io", ns, final)
            if vm is not None and ((vm.get("metadata") or {}).get("labels") or {}).get(hvi.L_IMPORTED) != "true":
                raise ValueError(f"a VM {ns}/{final} already exists: the import would loop without creating it")
        kube.create(obj)
        step("vmimport", "running", f"import {ns}/{name} created" + (f": VM {final}" if final else ""))
        return _vmi_follow(kube, ns, name, args.timeout)
    ns, name = hvi.check_name(args.namespace, "namespace"), hvi.check_name(args.name)
    if act == "import-follow":
        if kube.get(hvi.K_IMPORT, ns, name) is None:
            raise ValueError(f"no import {ns}/{name}")
        return _vmi_follow(kube, ns, name, args.timeout)
    if act == "import-delete":
        o = kube.get(hvi.K_IMPORT, ns, name)
        if o is None:
            raise ValueError(f"no import {ns}/{name}")
        phase = ((o.get("status") or {}).get("importStatus") or "")
        kube.delete(hvi.K_IMPORT, ns, name)
        step("vmimport", "done", f"import {ns}/{name} deleted" + (
            ": the VM and its images stay" if phase == hvi.DONE else
            "; its images go with it, a VM already created stays"))
        return EXIT_OK
    raise ValueError("action: source-apply, source-recheck, source-delete, import-create, import-follow, import-delete")


# ---------------------------------------------------------------------------
# v1.72.0 : projets Rancher (API de Rancher avec le jeton de la personne)
# ---------------------------------------------------------------------------

def _rancher_session(kubeconfig):
    """Rancher, lu dans le kubeconfig de la session (mandataire + fichier de
    jeton). Un kubeconfig direct ne donne pas de projets."""
    r = hpj.rancher_of(Path(kubeconfig).read_text()) if kubeconfig and Path(kubeconfig).exists() else None
    if r is None:
        raise ValueError("projects live in Rancher: sign in to the console through Rancher to manage them")
    return r


def _rancher_call(r, method, path, body=None):
    import ssl
    import urllib.error
    import urllib.request
    token = Path(r["token_file"]).read_text().strip() if r.get("token_file") else r.get("token")
    ctx = ssl.create_default_context(cafile=r["ca_file"]) if r.get("ca_file") else ssl.create_default_context()
    if r.get("insecure"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(r["url"] + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            msg = (json.loads(raw) or {}).get("message") or raw
        except ValueError:
            msg = raw
        raise ValueError(f"Rancher refused ({e.code}): {str(msg)[:300]}") from None
    except (urllib.error.URLError, OSError) as e:
        raise ValueError(f"Rancher unreachable: {getattr(e, 'reason', e)}") from None


def _project_members(kube, cid, pid):
    out = []
    for n in kube.list("namespaces", None):
        p = hpj.ns_project(n, cid)
        if p["project"] == pid and p["state"] != "foreign":
            out.append((n.get("metadata") or {}).get("name"))
    return out


def cmd_project(args):
    r = _rancher_session(args.kubeconfig)
    cid = r["cid"]
    act = args.action
    if act == "create":
        body = hpj.project_body(_read_json(args.spec), cid)
        _, out = _rancher_call(r, "POST", "/v3/projects", body)
        step("project", "done", f"project {body['name']} created ({(out or {}).get('id', '?').split(':')[-1]})")
        return EXIT_OK
    if act in ("update", "delete"):
        pid = hpj.check_pid(args.id)
        _, cur = _rancher_call(r, "GET", f"/v3/projects/{cid}:{pid}")
        row = (hpj.project_rows([cur], cid) or [{}])[0]
        if act == "delete":
            if row.get("system") or row.get("default"):
                raise ValueError(f"{row.get('name')} is Rancher's {'System' if row.get('system') else 'Default'} project: it stays")
            kube = kube_from(args)
            left = _project_members(kube, cid, pid)
            if left:
                raise ValueError(f"project {row.get('name')} still holds namespaces ({', '.join(left)}): move them out first")
            _rancher_call(r, "DELETE", f"/v3/projects/{cid}:{pid}")
            step("project", "done", f"project {row.get('name')} deleted")
            return EXIT_OK
        body = hpj.project_body(_read_json(args.spec), cid)
        new = dict(cur)
        for k in ("name", "description", "resourceQuota", "namespaceDefaultResourceQuota", "containerDefaultResourceLimit"):
            new[k] = body[k]
        _rancher_call(r, "PUT", f"/v3/projects/{cid}:{pid}", new)
        step("project", "done", f"project {body['name']} saved")
        return EXIT_OK
    kube = kube_from(args)
    ns = hpj.check_name(args.namespace, "namespace")
    if act == "move":
        pid = hpj.check_pid(args.id) if args.id else None
        if pid:
            _rancher_call(r, "GET", f"/v3/projects/{cid}:{pid}")        # le projet existe et se lit
            # le webhook de Rancher sur le cluster vérifie le droit manage-namespaces de la personne
            kube.run("annotate", "namespace", ns, f"{hpj.ANN_PROJECT}={cid}:{pid}", "--overwrite")
            kube.run("label", "namespace", ns, f"{hpj.ANN_PROJECT}={pid}", "--overwrite")
            step("project", "done", f"namespace {ns} moved to project {pid}")
        else:
            kube.run("annotate", "namespace", ns, f"{hpj.ANN_PROJECT}-", f"{hpj.ANN_QUOTA}-")
            kube.run("label", "namespace", ns, f"{hpj.ANN_PROJECT}-")
            step("project", "done", f"namespace {ns} out of any project")
        return EXIT_OK
    if act == "ns-quota":
        cur = kube.get("namespaces", None, ns)
        if cur is None:
            raise ValueError(f"no namespace {ns}")
        p = hpj.ns_project(cur, cid)
        if p["state"] == "none" or p["state"] == "foreign":
            raise ValueError(f"namespace {ns} is not in a project of this cluster: its quota would be ignored")
        _, proj = _rancher_call(r, "GET", f"/v3/projects/{cid}:{p['project']}")
        lim = hpj.check_ns_quota(_read_json(args.spec).get("limit") or {}, (hpj.project_rows([proj], cid) or [{}])[0])
        value = hpj.ns_quota(lim)
        if value:
            kube.run("annotate", "namespace", ns, f"{hpj.ANN_QUOTA}={value}", "--overwrite")
        else:
            kube.run("annotate", "namespace", ns, f"{hpj.ANN_QUOTA}-")
        step("project", "running", f"namespace {ns}: quota " + (value or "back to the project default"))

        def done(o):
            q = hpj.ns_project(o, cid)
            if q["quota_ok"] is False:
                return False, q["quota_message"] or "Rancher refused the quota"
            if q["quota_ok"] and (not value or q["quota"] == lim):
                return True, f"namespace {ns}: quota applied by Rancher"
            return None, "waiting for Rancher to apply the quota"
        return _wait(kube, "namespaces", None, ns, done, args.timeout, label="project")
    raise ValueError("action: create, update, delete, move, ns-quota")


# ---------------------------------------------------------------------------
# v1.73.0 : membres Rancher du cluster et des projets
# ---------------------------------------------------------------------------

def _members_target(r, args):
    cid = r["cid"]
    if args.scope == "project":
        target = f"{cid}:{hpj.check_pid(args.project)}"
        return target, "/v3/projectroletemplatebindings", f"?projectId={target}"
    if args.scope != "cluster":
        raise ValueError("--scope: cluster or project")
    return cid, "/v3/clusterroletemplatebindings", f"?clusterId={cid}"


def _member_users(r, bindings):
    """Les utilisateurs Rancher des liaisons, pour les nommer et reconnaître
    les comptes système (un membre qui ne lit pas les utilisateurs : sans)."""
    out = {}
    for uid in {b.get("userId") for b in bindings if b.get("userId")}:
        try:
            _, u = _rancher_call(r, "GET", f"/v3/users/{uid}")
            out[uid] = u or {}
        except ValueError:
            pass
    return out


def cmd_member(args):
    r = _rancher_session(args.kubeconfig)
    target, path, qs = _members_target(r, args)
    if args.action == "add":
        body = hmb.binding_body(args.scope, target, args.principal, args.role)
        _, rt = _rancher_call(r, "GET", f"/v3/roletemplates/{hmb.check_binding(args.role)}")
        if (rt or {}).get("context") != args.scope or (rt or {}).get("locked"):
            raise ValueError(f"role {args.role}: not a role for a {args.scope}")
        _rancher_call(r, "POST", path, body)
        step("member", "done", f"{args.principal}: {rt.get('name') or args.role} of the {args.scope}")
        return EXIT_OK
    if args.action == "remove":
        bid = hmb.check_binding(args.id)
        _, lst = _rancher_call(r, "GET", path + qs)
        items = (lst or {}).get("data") or []
        row = hmb.check_removal(hmb.member_rows(items, users=_member_users(r, items)), bid, args.scope)
        _rancher_call(r, "DELETE", f"{path}/{bid}")
        step("member", "done", f"{row['name']}: {row['role']} of the {args.scope} removed")
        return EXIT_OK
    raise ValueError("action: add or remove")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="harvester-resources", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("addon", help="enable or disable a Harvester add-on")
    sp.set_defaults(fn=cmd_addon)
    sp.add_argument("--cluster", help="cluster name in the configuration")
    sp.add_argument("--kubeconfig", help="kubeconfig of the Harvester cluster")
    sp.add_argument("--namespace", required=True)
    sp.add_argument("--name", required=True)
    grp = sp.add_mutually_exclusive_group(required=True)
    grp.add_argument("--enable", dest="enable", action="store_true")
    grp.add_argument("--disable", dest="enable", action="store_false")
    sp.add_argument("--timeout", type=int, default=900, help="seconds to wait for Harvester")

    def common(p):
        p.add_argument("--cluster", help="cluster name in the configuration")
        p.add_argument("--kubeconfig", help="kubeconfig of the Harvester cluster")
        p.add_argument("--namespace", required=True)
        p.add_argument("--timeout", type=int, default=3600, help="seconds to wait for Harvester")

    sp = sub.add_parser("backup", help="VM backups and snapshots: create, restore, delete")
    sp.set_defaults(fn=cmd_backup)
    sp.add_argument("action", choices=("create", "restore", "delete"))
    common(sp)
    sp.add_argument("--name")
    sp.add_argument("--vm")
    sp.add_argument("--type", choices=("backup", "snapshot"), default="backup")
    sp.add_argument("--new-vm")
    sp.add_argument("--replace", action="store_true", help="restore over the original VM (stopped)")
    sp.add_argument("--keep-mac", action="store_true")
    sp.add_argument("--halt", action="store_true", help="leave the restored VM stopped")
    sp.add_argument("--delete-policy", choices=("retain", "delete"), default="retain",
                    help="restore --replace: delete or keep the previous volumes")
    sp.add_argument("--freeze", choices=hb.FREEZE, help="create: file system freeze deadline (Harvester 1.9)")

    sp = sub.add_parser("schedule", help="scheduled VM backups or snapshots")
    sp.set_defaults(fn=cmd_schedule)
    sp.add_argument("action", choices=("create", "update", "suspend", "resume", "delete"))
    common(sp)
    sp.add_argument("--name", required=True)
    sp.add_argument("--vm")
    sp.add_argument("--cron")
    sp.add_argument("--retain", help="copies kept (create: 7 by default; update: unchanged when omitted)")
    sp.add_argument("--max-failure", help="failures before suspension (create: 3; update: unchanged when omitted)")
    sp.add_argument("--type", choices=("backup", "snapshot"), default="backup")

    sp = sub.add_parser("volsnap", help="volume snapshots: restore into a new volume, delete")
    sp.set_defaults(fn=cmd_volsnap)
    sp.add_argument("action", choices=("restore", "delete"))
    common(sp)
    sp.add_argument("--name", required=True)
    sp.add_argument("--new-volume")
    sp.add_argument("--storage-class")
    sp.add_argument("--size")

    sp = sub.add_parser("create", help="create an image, storage class, SSH key, secret, VM network or volume")
    sp.set_defaults(fn=cmd_create)
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--kind", choices=ho.KINDS, required=True)
    sp.add_argument("--spec", required=True, help="JSON request, '-' for stdin")
    sp.add_argument("--timeout", type=int, default=3600)

    sp = sub.add_parser("delete", help="delete one of those objects if nothing uses it")
    sp.set_defaults(fn=cmd_delete)
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--kind", choices=ho.KINDS, required=True)
    sp.add_argument("--namespace")
    sp.add_argument("--name", required=True)
    sp.add_argument("--timeout", type=int, default=600)

    sp = sub.add_parser("sc-default", help="make a storage class the default one")
    sp.set_defaults(fn=cmd_sc_default)
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--name", required=True)

    sp = sub.add_parser("volume-expand", help="grow a volume")
    sp.set_defaults(fn=cmd_volume_expand)
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--namespace", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--size", required=True)
    sp.add_argument("--timeout", type=int, default=600)

    sp = sub.add_parser("addon-values", help="change the configuration (values) of an add-on")
    sp.set_defaults(fn=cmd_addon_values)
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--namespace", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--values", required=True, help="YAML file, '-' for stdin")
    sp.add_argument("--timeout", type=int, default=900)

    sp = sub.add_parser("yaml", help="replace an object by its edited YAML, or create one from YAML")
    sp.set_defaults(fn=cmd_yaml)
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--kind", choices=sorted(hy.KINDS), required=True)
    sp.add_argument("--namespace")
    sp.add_argument("--name", help="the object replaced (not with --create)")
    sp.add_argument("--file", required=True, help="YAML or JSON file, '-' for stdin")
    sp.add_argument("--create", action="store_true", help="create a new object instead of replacing one")
    sp.add_argument("--dry-run", action="store_true", help="only let the cluster check it")

    sp = sub.add_parser("vm", help="the VM actions of Harvester's menu")
    sp.set_defaults(fn=cmd_vm)
    sp.add_argument("action", choices=sorted(VM_ACTIONS))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--namespace", required=True)
    sp.add_argument("--name", required=True, help="the VM")
    sp.add_argument("--remove-volumes", help="delete: volumes (PVC names) deleted with the VM, comma separated")
    sp.add_argument("--keep-cloudinit", dest="remove_cloudinit", action="store_false",
                    help="delete: keep the cloud-init secrets")
    sp.add_argument("--new-name", help="clone: name of the copy")
    sp.add_argument("--with-data", action="store_true", help="clone/template: copy the volumes' data")
    sp.add_argument("--start", action="store_true", help="clone: start the copy")
    sp.add_argument("--volume", help="eject/add-volume/remove-volume: the disk name in the VM")
    sp.add_argument("--delete-volume", action="store_true", help="eject: delete the CD-ROM volume")
    sp.add_argument("--claim", help="add-volume: the volume (PVC) to plug")
    sp.add_argument("--bus", default="scsi", help="add-volume: scsi (default), virtio or sata")
    sp.add_argument("--node", help="migrate: the target node (any node by default)")
    sp.add_argument("--template-name", help="template: name of the template (a new version if it exists)")
    sp.add_argument("--description")
    sp.add_argument("--set-default", action="store_true", help="template: make the new version the default")
    sp.add_argument("--user-data", help="cloudinit: file with the user-data")
    sp.add_argument("--network-data", help="cloudinit: file with the network-data")
    sp.add_argument("--guest-agent", action="store_true", help="cloudinit: install qemu-guest-agent")
    sp.add_argument("--ssh-names", help="cloudinit: SSH key pairs shown on the VM, comma separated")
    sp.add_argument("--image", help="insert-cdrom: the image (namespace/name) to put in the drive")
    sp.add_argument("--iface", help="add-nic/remove-nic: the interface name")
    sp.add_argument("--network", help="add-nic: the VM network (namespace/name)")
    sp.add_argument("--mac", help="add-nic: a MAC address (chosen by the cluster if absent)")
    sp.add_argument("--cpu", help="cpumem: the new number of vCPUs (sockets)")
    sp.add_argument("--memory", help="cpumem: the new memory, e.g. 8Gi")
    sp.add_argument("--target", help="storage-migrate: the target volume (an existing, unused PVC)")
    sp.add_argument("--size", help="quota: the VM's snapshot quota, e.g. 20Gi (0 removes it)")
    sp.add_argument("--kind", choices=("basic", "ssh"), help="access: a password, or SSH keys")
    sp.add_argument("--users", help="access: user names, comma separated")
    sp.add_argument("--password-file", help="access: file holding the password (never on the command line)")
    sp.add_argument("--keys", help="access: SSH key pairs (namespace/name), comma separated")
    sp.add_argument("--timeout", type=int, default=3600)
    sp = sub.add_parser("pools-apply", help="data-disk pools of a host: disks provisioned with their tag, "
                                             "and a storage class longhorn-<tag> per pool")
    sp.set_defaults(fn=cmd_pools_apply)
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--node", required=True, help="the host (Kubernetes node name)")
    sp.add_argument("--spec", required=True,
                    help='JSON file: {"pools": [{"tag", "replicas", "disks": [{"serial", "wwn", "path"}]}]}')
    sp.add_argument("--no-classes", action="store_true",
                    help="provision only (a node joining a cluster that has its classes)")
    sp.add_argument("--timeout", type=int, default=1800, help="seconds to wait for node-disk-manager and Longhorn")
    sp = sub.add_parser("host", help="a host's settings and actions, as in Harvester")
    sp.set_defaults(fn=cmd_host)
    sp.add_argument("action", choices=sorted(HOST_ACTIONS))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--node", required=True, help="the host (Kubernetes node name)")
    sp.add_argument("--custom-name", help="basics: name shown for the host ('' removes it)")
    sp.add_argument("--console-url", help="basics: address of the host's console, e.g. its BMC ('' removes it)")
    sp.add_argument("--labels-file", help="basics: JSON object of the host's labels (the visible ones)")
    sp.add_argument("--tags", help="tags/disk-set: tags, comma separated ('' removes them)")
    sp.add_argument("--disk", help="disk-*: the block device name")
    sp.add_argument("--provisioner", default="LonghornV1", choices=("LonghornV1", "LonghornV2", "lvm"))
    sp.add_argument("--vg", help="disk-add: the LVM volume group (lvm provisioner)")
    sp.add_argument("--tag", help="disk-add: tags of the disk, comma separated (Longhorn disk tags, e.g. a pool)")
    fmt = sp.add_mutually_exclusive_group()
    fmt.add_argument("--format", dest="format", action="store_true", default=None, help="disk-add: format the disk")
    fmt.add_argument("--no-format", dest="format", action="store_false", help="disk-add: keep its ext4/XFS file system")
    sp.add_argument("--scheduling", choices=("on", "off"), help="disk-set: Longhorn may place replicas on it")
    sp.add_argument("--thp-enabled", choices=hh.THP_ENABLED)
    sp.add_argument("--thp-shmem", choices=hh.THP_SHMEM)
    sp.add_argument("--thp-defrag", choices=hh.THP_DEFRAG)
    sp.add_argument("--run", choices=hh.KSM_RUN, help="ksmtuned: stop, run or prune")
    sp.add_argument("--mode", choices=("standard", "high", "customized"))
    sp.add_argument("--thres", help="ksmtuned: free memory threshold, 0 to 100 %%")
    sp.add_argument("--merge", choices=("on", "off"), help="ksmtuned: merge pages across NUMA nodes")
    sp.add_argument("--params", help="ksmtuned customized: JSON file with sleepMsec, boost, decay, minPages, maxPages")
    en = sp.add_mutually_exclusive_group()
    en.add_argument("--enable", dest="enable", action="store_true", default=None)
    en.add_argument("--disable", dest="enable", action="store_false")
    sp.add_argument("--bmc-host", help="oob: the BMC address")
    sp.add_argument("--bmc-port", default=623, help="oob: the BMC port (623)")
    sp.add_argument("--username", help="oob: BMC user name")
    sp.add_argument("--password-file", help="oob: file holding the BMC password (never on the command line)")
    sp.add_argument("--insecure", action="store_true", help="oob: accept the BMC's certificate without checking it")
    sp.add_argument("--no-events", action="store_true", help="oob: do not collect the BMC's hardware events")
    sp.add_argument("--interval", default="1h", help="oob: polling interval of the events (1h)")
    sp.add_argument("--off", action="store_true", help="oob: remove the out-of-band access")
    sp.add_argument("--operation", choices=hh.POWER_OPS, help="power: shutdown, poweron or reboot")
    sp.add_argument("--timeout", type=int, default=3600)
    sp = sub.add_parser("namespace", help="create, change, delete a namespace; its snapshot quota")
    sp.set_defaults(fn=cmd_namespace)
    sp.add_argument("action", choices=sorted(NS_ACTIONS))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--name", required=True, help="the namespace")
    sp.add_argument("--description")
    sp.add_argument("--labels-file", help="JSON object of the namespace's labels")
    sp.add_argument("--annotations-file", help="update: JSON object of its annotations")
    sp.add_argument("--size", help="quota: total size of the snapshots, e.g. 100Gi (0 removes it)")
    sp.add_argument("--timeout", type=int, default=900)
    sp = sub.add_parser("volume", help="a volume's actions, as in Harvester")
    sp.set_defaults(fn=cmd_volume)
    sp.add_argument("action", choices=sorted(VOLUME_ACTIONS))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--namespace", required=True)
    sp.add_argument("--name", required=True, help="the volume (PVC)")
    sp.add_argument("--new-name", help="clone/copy: the new volume")
    sp.add_argument("--no-data", action="store_true", help="clone: a new empty volume with the same settings")
    sp.add_argument("--display-name", help="export: the image name")
    sp.add_argument("--target-namespace", help="export: namespace of the image (the volume's by default)")
    sp.add_argument("--storage-class", help="export/copy: the target storage class")
    sp.add_argument("--snapshot-name", help="snapshot: its name")
    sp.add_argument("--description", help="describe: the text ('' removes it)")
    sp.add_argument("--timeout", type=int, default=3600)

    sp = sub.add_parser("image", help="an image's actions, as in Harvester")
    sp.set_defaults(fn=cmd_image)
    sp.add_argument("action", choices=sorted(IMAGE_ACTIONS))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--namespace", required=True)
    sp.add_argument("--name", help="the image (object name)")
    sp.add_argument("--display-name", help="clone/encrypt/decrypt/upload: the new image's name")
    sp.add_argument("--description")
    sp.add_argument("--labels-file", help="edit: JSON object of the image's labels")
    sp.add_argument("--storage-class", help="encrypt/decrypt/upload: the target storage class")
    sp.add_argument("--out", help="download: file written (gzip for a Longhorn image, qcow2 for a CDI image)")
    sp.add_argument("--file", help="upload: the image file")
    sp.add_argument("--file-name", help="upload: the original file name (label iso or raw_qcow2)")
    sp.add_argument("--checksum", help="upload: SHA512 of the file")
    sp.add_argument("--port", default=8092, help="upload: port the cluster downloads from (8092)")
    sp.add_argument("--advertise", help="upload: address the cluster reaches this machine at")
    sp.add_argument("--timeout", type=int, default=7200)
    sp = sub.add_parser("template", help="a VM template's versions, as in Harvester")
    sp.set_defaults(fn=cmd_template)
    sp.add_argument("action", choices=("set-default", "delete-version", "delete"))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--namespace", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--version", help="set-default/delete-version: namespace/name of the version")
    sp.add_argument("--timeout", type=int, default=300)

    sp = sub.add_parser("cloudtpl", help="cloud-init templates (user-data, network-data)")
    sp.set_defaults(fn=cmd_cloudtpl)
    sp.add_argument("action", choices=("create", "update", "delete"))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--namespace", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--type", choices=("user", "network"))
    sp.add_argument("--file", help="the template text")
    sp.add_argument("--description")

    sp = sub.add_parser("storageclass", help="a storage class from Harvester's form (Longhorn v1/v2, encryption, LVM)")
    sp.set_defaults(fn=cmd_storageclass)
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--spec", required=True, help="JSON request, '-' for stdin")

    sp = sub.add_parser("secret", help="create a typed secret, or give an existing one new values")
    sp.set_defaults(fn=cmd_secret)
    sp.add_argument("action", choices=("create", "update"))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--namespace", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--spec", required=True, help="JSON file (values never on the command line)")

    sp = sub.add_parser("sshkey", help="change an SSH key pair's public key or description")
    sp.set_defaults(fn=cmd_sshkey)
    sp.add_argument("action", choices=("update",))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--namespace", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--public-key-file")
    sp.add_argument("--description")
    # v1.65.0 : le menu Networks de Harvester
    sp = sub.add_parser("clusternetwork", help="create or delete a cluster network")
    sp.set_defaults(fn=cmd_clusternetwork)
    sp.add_argument("action", choices=("create", "delete"))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--name", required=True)
    sp.add_argument("--description")
    sp.add_argument("--timeout", type=int, default=300)

    sp = sub.add_parser("netconfig", help="a cluster network's configurations: NICs, bond, MTU, nodes")
    sp.set_defaults(fn=cmd_netconfig)
    sp.add_argument("action", choices=("create", "update", "migrate", "delete"))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--spec", help="create/update: JSON request")
    sp.add_argument("--name", help="migrate/delete: the configuration")
    sp.add_argument("--target", help="migrate: the cluster network it moves to")
    sp.add_argument("--timeout", type=int, default=600)

    sp = sub.add_parser("vmnet", help="change a VM network: VLAN, trunk ranges, route, description")
    sp.set_defaults(fn=cmd_vmnet)
    sp.add_argument("action", choices=("update",))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--namespace", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--spec", required=True)

    sp = sub.add_parser("lb", help="Harvester load balancers for VMs")
    sp.set_defaults(fn=cmd_lb)
    sp.add_argument("action", choices=("create", "update", "delete"))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--spec", help="create/update: JSON request")
    sp.add_argument("--namespace", default="default")
    sp.add_argument("--name")
    sp.add_argument("--timeout", type=int, default=300)

    sp = sub.add_parser("ippool", help="IP pools of the load balancers")
    sp.set_defaults(fn=cmd_ippool)
    sp.add_argument("action", choices=("create", "update", "delete", "release"))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--spec", help="create/update: JSON request")
    sp.add_argument("--name")
    sp.add_argument("--ip", help="release: the allocated address to free")
    sp.add_argument("--timeout", type=int, default=120)

    sp = sub.add_parser("hostnet", help="host networks (an address per node on a VLAN)")
    sp.set_defaults(fn=cmd_hostnet)
    sp.add_argument("action", choices=("create", "update", "delete"))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--spec", help="create/update: JSON request")
    sp.add_argument("--name")
    sp.add_argument("--timeout", type=int, default=300)

    sp = sub.add_parser("netsetting", help="storage, VM migration and RWX networks (Harvester settings)")
    sp.set_defaults(fn=cmd_netsetting)
    sp.add_argument("action", choices=("set", "clear"))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--name", required=True, choices=("storage-network", "vm-migration-network", "rwx-network"))
    sp.add_argument("--spec", help="set: JSON request")
    sp.add_argument("--timeout", type=int, default=1800)
    # v1.67.0 : réglages, paquet de support, kubeconfig
    sp = sub.add_parser("setting", help="change or reset a Harvester setting; test the backup target")
    sp.set_defaults(fn=cmd_setting)
    sp.add_argument("action", choices=("set", "reset", "test-backup-target"))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--name")
    sp.add_argument("--value-file", help="set: the new value, in a file (it may hold secrets)")
    sp.add_argument("--timeout", type=int, default=300)

    sp = sub.add_parser("supportbundle", help="Harvester's support bundle")
    sp.set_defaults(fn=cmd_supportbundle)
    sp.add_argument("action", choices=("create", "delete"))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--spec")
    sp.add_argument("--name")
    sp.add_argument("--timeout", type=int, default=3600)

    sp = sub.add_parser("kubeconfig", help="a kubeconfig limited to a role, with a token that expires")
    sp.set_defaults(fn=cmd_kubeconfig)
    sp.add_argument("action", choices=("create", "revoke"))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--name", required=True)
    sp.add_argument("--role", default="view")
    sp.add_argument("--namespace", default="", help="create: limit the role to this namespace")
    sp.add_argument("--duration", default="24h")
    sp.add_argument("--description")
    sp.add_argument("--out", help="create: where to write the file (mode 0600)")
    sp.add_argument("--cluster-name", help="create: the cluster name written in the file")
    sp = sub.add_parser("device", help="PCI and USB passthrough, SR-IOV virtual functions")
    sp.set_defaults(fn=cmd_device)
    sp.add_argument("action", choices=("pci-enable", "pci-disable", "usb-enable", "usb-disable", "sriov"))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--name", action="append", help="device name (PCIDevice, USBDevice or SriovNetworkDevice), repeatable")
    sp.add_argument("--vfs", type=int, default=0, help="sriov: number of virtual functions, 0 to disable")
    sp.add_argument("--user", default="admin", help="enable: the userName written in the claim")
    sp.add_argument("--timeout", type=int, default=600)
    sp = sub.add_parser("upgrade", help="upgrade Harvester: versions, start (online or from an ISO), follow, logs")
    sp.set_defaults(fn=cmd_upgrade)
    sp.add_argument("action", choices=("version-add", "version-delete", "start", "follow", "logs", "dismiss",
                                       "abort", "resume-node"))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--name", help="the upgrade (follow, logs, dismiss, abort, resume-node) or the version (version-delete)")
    sp.add_argument("--version", help="start: the Version to upgrade to")
    sp.add_argument("--version-file", help="version-add, start: a published version.yaml")
    sp.add_argument("--iso", help="start: an ISO of the console, served to the cluster (air-gapped path)")
    sp.add_argument("--checksum", help="start --iso: its SHA-512, checked before serving it")
    sp.add_argument("--port", default=os.environ.get("HARVESTER_OPS_IMAGE_UPLOAD_PORT", "8092"))
    sp.add_argument("--advertise", help="start --iso: the address the cluster reaches the console at")
    sp.add_argument("--no-log", action="store_true", help="start: do not collect the upgrade logs")
    sp.add_argument("--skip-single-replica", action="store_true",
                    help="start: skip Harvester's check of detached single-replica volumes")
    sp.add_argument("--node", help="resume-node: the paused host")
    sp.add_argument("--out", help="logs: where to write the archive (mode 0600)")
    sp.add_argument("--timeout", type=int, default=6 * 3600)
    sp = sub.add_parser("monlog", help="logging outputs and flows, Alertmanager configurations")
    sp.set_defaults(fn=cmd_monlog)
    sp.add_argument("action", choices=("output-apply", "output-delete", "flow-apply", "flow-delete", "amc-apply", "amc-delete"))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--spec", help="apply: the request as JSON (secret values are turned into Secrets)")
    sp.add_argument("--kind", help="delete: Output, ClusterOutput, Flow, ClusterFlow or AlertmanagerConfig")
    sp.add_argument("--namespace")
    sp.add_argument("--name")
    sp.add_argument("--grace", type=int, default=90, help="apply: seconds before an unprocessed object is an error")
    sp.add_argument("--timeout", type=int, default=480, help="apply: seconds for the operator, then fluentd's check")
    sp = sub.add_parser("vmimport", help="VM imports: VMware, OpenStack and OVA sources, imports followed to the running VM")
    sp.set_defaults(fn=cmd_vmimport)
    sp.add_argument("action", choices=("source-apply", "source-recheck", "source-delete", "import-create", "import-follow", "import-delete"))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig")
    sp.add_argument("--spec", help="source-apply, import-create: the request as JSON (credentials become a Secret)")
    sp.add_argument("--type", help="source-recheck, source-delete: vmware, openstack or ova")
    sp.add_argument("--namespace")
    sp.add_argument("--name")
    sp.add_argument("--with-secret", action="store_true", help="source-delete: also delete the secret the console created")
    sp.add_argument("--timeout", type=int, default=4 * 3600, help="seconds: source check, or the import to the running VM")
    sp = sub.add_parser("project", help="Rancher projects of the cluster: create, change, delete, move a namespace, namespace quota")
    sp.set_defaults(fn=cmd_project)
    sp.add_argument("action", choices=("create", "update", "delete", "move", "ns-quota"))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig", help="the kubeconfig of a Rancher session (it points at Rancher's proxy)")
    sp.add_argument("--spec", help="create, update: {name, description, quota, ns_default, container}; ns-quota: {limit}")
    sp.add_argument("--id", help="update, delete, move: the project id (p-xxxxx); move without it: out of any project")
    sp.add_argument("--namespace")
    sp.add_argument("--timeout", type=int, default=120)
    sp = sub.add_parser("member", help="Rancher members of the cluster or of a project: add, remove")
    sp.set_defaults(fn=cmd_member)
    sp.add_argument("action", choices=("add", "remove"))
    sp.add_argument("--cluster")
    sp.add_argument("--kubeconfig", help="the kubeconfig of a Rancher session (it points at Rancher's proxy)")
    sp.add_argument("--scope", choices=("cluster", "project"), default="cluster")
    sp.add_argument("--project", help="project scope: the project id (p-xxxxx)")
    sp.add_argument("--principal", help="add: the Rancher principal (local://u-xxxxx, keycloakoidc_user://..., ..._group://...)")
    sp.add_argument("--role", help="add: the role template (cluster-owner, cluster-member, project-member, read-only...)")
    sp.add_argument("--id", help="remove: the role binding id")
    args = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, _on_signal)
    try:
        return args.fn(args)
    except ValueError as e:
        step("check", "error", str(e))
        return EXIT_BLOCKED
    except (KubeError, RuntimeError) as e:
        step(args.cmd, "error", refusal(e)[:300])
        return EXIT_FAIL
    except Cancelled:
        return EXIT_CANCELLED


if __name__ == "__main__":
    sys.exit(main())
