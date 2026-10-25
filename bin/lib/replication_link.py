"""harvester-ops : essai actif du lien entre deux clusters (réplication).

Mesure ce que la réplication emprunte vraiment : l'écriture de la source
vers la cible de sauvegarde commune, puis la lecture de la cible. Une
pod temporaire sur la source écrit N Mio de données aléatoires dans la
cible de sauvegarde NFS, une pod sur la cible les relit et efface le
fichier ; les durées sont lues dans le journal des pods (lignes
`HOPS_LINK clé=valeur`).

Règles tenues :

* la cible de sauvegarde se lit sur chaque cluster (réglage Harvester
  `backup-target`, sinon la `BackupTarget` Longhorn `default`) ; deux
  cibles différentes : refus `backup-target-differs`, une cible autre que
  NFS : refus `not-supported` (S3 demanderait des identifiants dans la
  pod) ;
* image = celle du DaemonSet `longhorn-manager` du cluster même : déjà
  présente sur chaque nœud, aucun tirage d'image (airgap) ;
* les pods et le fichier sont TOUJOURS retirés, en échec comme en
  annulation (SIGTERM : l'exception remonte par le `finally`) ; un fichier
  dont l'effacement n'est pas confirmé est retiré par une pod de nettoyage ;
* droits : ceux des kubeconfigs reçus (ceux de la personne, passés par la
  console).

Vérifié en réel le 24-25/10/2026 entre harvlab (Harvester 1.8.2) et
harvlab2 (1.9.0), partage NFS du banc : écriture 99 Mio/s, lecture
103 Mio/s pour 256 Mio, pods retirées. Les outils de l'image (`sh`, `dd`,
`date +%s.%N`, `stat`) sont ceux de longhorn-manager v1.11 / v1.12.

Bibliothèque standard seulement.
"""

import json
import time

import replication as rp
import vm_transfer as vt

NS = rp.LONGHORN_NS
K_POD = "pods"
K_DS = "daemonsets.apps"
K_SETTING = "settings.harvesterhci.io"
K_LH_TARGET = "backuptargets.longhorn.io"
MANAGER_DS = "longhorn-manager"
LINK_LABEL = "harvester-ops.io/link-test"
MANAGED_BY = {"app.kubernetes.io/managed-by": "harvester-ops"}
DIR = "harvester-ops-linktest"
MOUNT = "/mnt/bt"
SIZE_MIN_MIB, SIZE_MAX_MIB = 64, 4096
MIB = 1024 ** 2
POLL_S = 2
CLEANUP_WAIT_S = 60
# refus avant toute pod (code 2) ; le reste est un échec (code 1)
REFUSALS = ("backup-target-differs", "not-supported", "image-unknown", "bad-size")


class LinkError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _md(o):
    return (o or {}).get("metadata") or {}


def _status(o):
    return (o or {}).get("status") or {}


# ---------------------------------------------------------------------------
# Faits des clusters
# ---------------------------------------------------------------------------

def backup_target_raw(kube):
    """Valeur de la cible de sauvegarde d'un cluster : réglage Harvester
    `backup-target` (JSON {type, endpoint}), sinon l'URL de la
    `BackupTarget` Longhorn `default`, rendue sous la même forme."""
    s = kube.get(K_SETTING, None, "backup-target") or {}
    raw = s.get("value") or s.get("default") or ""
    if raw:
        return raw
    bt = kube.get(K_LH_TARGET, NS, "default") or {}
    url = ((bt.get("spec") or {}).get("backupTargetURL") or "").strip()
    if not url:
        return ""
    kind = url.split("://", 1)[0].lower() if "://" in url else ""
    return json.dumps({"type": kind, "endpoint": url})


def nfs_target(src_raw, dst_raw):
    """(serveur, chemin, url) de la cible NFS commune ; LinkError sinon."""
    s, d = vt.normalize_backup_target(src_raw), vt.normalize_backup_target(dst_raw)
    if not s or not d or tuple(s) != tuple(d):
        raise LinkError("backup-target-differs", "the two clusters do not use the same backup target")
    if s[0] != "nfs":
        raise LinkError("not-supported", f"backup target of type {s[0]}: only NFS can be tested")
    host, _, path = s[1].partition(":")
    path = path.split("?", 1)[0] or "/"
    return host, path, f"nfs://{host}:{path}"


def manager_image(kube):
    """Image du DaemonSet longhorn-manager du cluster (déjà sur ses nœuds)."""
    ds = kube.get(K_DS, NS, MANAGER_DS) or {}
    containers = (((ds.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
    for c in sorted(containers, key=lambda c: c.get("name") != MANAGER_DS):
        if c.get("image"):
            return c["image"]
    raise LinkError("image-unknown", "no longhorn-manager image found on the cluster")


# ---------------------------------------------------------------------------
# Pods
# ---------------------------------------------------------------------------

def _file(test_id):
    return f"{MOUNT}/{DIR}/{test_id}.bin"


def write_script(test_id, size_mib):
    f = _file(test_id)
    return ("set -e; ok=0; d=" + f"{MOUNT}/{DIR}" + "; mkdir -p \"$d\"; "
            f"trap '[ \"$ok\" = 1 ] || rm -f {f}' EXIT; "
            f"dd if=/dev/urandom of=/scratch/data bs=1M count={int(size_mib)} 2>/dev/null; "
            "s=$(date +%s.%N); "
            f"dd if=/scratch/data of={f} bs=1M conv=fsync 2>/dev/null; "
            "e=$(date +%s.%N); ok=1; "
            f"echo \"HOPS_LINK write_start=$s write_end=$e bytes=$(stat -c %s {f})\"")


def read_script(test_id):
    f = _file(test_id)
    # le répertoire part aussi (rmdir échoue sans dommage si un autre essai
    # y a encore son fichier)
    d = f"{MOUNT}/{DIR}"
    return (f"set -e; trap 'rm -f {f}; rmdir {d} 2>/dev/null || true' EXIT; "
            "s=$(date +%s.%N); "
            f"dd if={f} of=/dev/null bs=1M 2>/dev/null; "
            "e=$(date +%s.%N); "
            f"echo \"HOPS_LINK read_start=$s read_end=$e bytes=$(stat -c %s {f})\"; "
            f"rm -f {f} && echo \"HOPS_LINK removed=1\"; rmdir {d} 2>/dev/null || true")


def cleanup_script(test_id):
    return f"rm -f {_file(test_id)}; rmdir {MOUNT}/{DIR} 2>/dev/null; echo \"HOPS_LINK removed=1\""


def pod(name, test_id, image, server, path, script, scratch_mib=0, deadline_s=600):
    """Pod temporaire : la cible de sauvegarde montée en NFS par le kubelet,
    root (le partage de Longhorn est écrit par root), arrêtée par le délai
    `activeDeadlineSeconds` si elle s'éternise."""
    volumes = [{"name": "bt", "nfs": {"server": server, "path": path}}]
    mounts = [{"name": "bt", "mountPath": MOUNT}]
    if scratch_mib:
        volumes.append({"name": "scratch", "emptyDir": {"sizeLimit": f"{int(scratch_mib) + 64}Mi"}})
        mounts.append({"name": "scratch", "mountPath": "/scratch"})
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": NS, "labels": dict(MANAGED_BY, **{LINK_LABEL: test_id})},
            "spec": {"restartPolicy": "Never", "activeDeadlineSeconds": int(deadline_s),
                     "terminationGracePeriodSeconds": 5,
                     "containers": [{"name": "link", "image": image, "imagePullPolicy": "IfNotPresent",
                                     "command": ["/bin/sh", "-c", script],
                                     "securityContext": {"runAsUser": 0}, "volumeMounts": mounts}],
                     "volumes": volumes}}


def parse_logs(text):
    """Valeurs des lignes `HOPS_LINK clé=valeur` (nombres)."""
    out = {}
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line.startswith("HOPS_LINK "):
            continue
        for tok in line.split()[1:]:
            k, sep, v = tok.partition("=")
            if not sep:
                continue
            try:
                out[k] = float(v)
            except ValueError:
                continue
    return out


def pod_logs(kube, name):
    try:
        if hasattr(kube, "logs"):
            return kube.logs(NS, name) or ""
        return kube.run("logs", name, "-n", NS, "--tail=200")
    except Exception:                           # noqa: BLE001 - journal illisible : pas de durée
        return ""


def wait_pod(kube, name, timeout_s, sleep=time.sleep, now=time.time, stop=None):
    """Attend la fin d'une pod : `Succeeded` ou `Failed`, sinon LinkError
    `timeout` (ou `stopped` quand `stop()` le demande)."""
    end = now() + timeout_s
    while True:
        if stop is not None and stop():
            raise LinkError("stopped", "stopped by the console shutdown")
        phase = _status(kube.get(K_POD, NS, name)).get("phase")
        if phase in ("Succeeded", "Failed"):
            return phase
        if now() >= end:
            raise LinkError("timeout", f"pod {name} did not finish within {int(timeout_s)} s")
        sleep(POLL_S)


def _delete(kube, name):
    try:
        kube.delete(K_POD, NS, name)
        return True
    except Exception as e:                      # noqa: BLE001
        return "notfound" in str(e).lower().replace(" ", "")


def _rate(nbytes, secs):
    return nbytes / secs if secs and secs > 0 else None


def pod_timeout(size_mib):
    """Délai d'une pod : 2 min, plus une seconde par Mio (1 Mio/s au pire)."""
    return 120 + int(size_mib)


def run(src, dst, size_mib, test_id, sleep=time.sleep, now=time.time, stop=None, emit=None):
    """L'essai complet. Rend {source_*, size_bytes, write_s, read_s,
    write_bps, read_bps, backup_target, cleanup} ; LinkError (avec `.facts`
    partiels) en échec. Pods et fichier retirés dans tous les cas."""
    emit = emit or (lambda *a: None)
    if isinstance(size_mib, bool) or not isinstance(size_mib, int) or not SIZE_MIN_MIB <= size_mib <= SIZE_MAX_MIB:
        raise LinkError("bad-size", f"size: {SIZE_MIN_MIB} to {SIZE_MAX_MIB} MiB")
    server, path, url = nfs_target(backup_target_raw(src), backup_target_raw(dst))
    img_src, img_dst = manager_image(src), manager_image(dst)
    names = {k: f"hops-link-{k}-{test_id}" for k in ("w", "r", "c")}
    out = {"backup_target": url, "size_bytes": size_mib * MIB, "write_s": None, "read_s": None,
           "write_bps": None, "read_bps": None, "cleanup": ""}
    created, written, removed = [], False, False
    deadline = pod_timeout(size_mib)
    try:
        emit("write", "running", f"writing {size_mib} MiB to the backup target from the source")
        written = True                          # dès la création : une écriture partielle est possible
        src.create(pod(names["w"], test_id, img_src, server, path, write_script(test_id, size_mib),
                       scratch_mib=size_mib, deadline_s=deadline))
        created.append((src, names["w"]))
        phase = wait_pod(src, names["w"], deadline, sleep, now, stop)
        w = parse_logs(pod_logs(src, names["w"]))
        if phase != "Succeeded" or "write_end" not in w:
            raise LinkError("write-failed", "the source pod could not write to the backup target")
        out["write_s"] = round(w["write_end"] - w["write_start"], 3)
        out["size_bytes"] = int(w.get("bytes") or out["size_bytes"])
        out["write_bps"] = _rate(out["size_bytes"], out["write_s"])
        emit("write", "done", f"written in {out['write_s']} s")
        emit("read", "running", "reading the data back from the target")
        dst.create(pod(names["r"], test_id, img_dst, server, path, read_script(test_id), deadline_s=deadline))
        created.append((dst, names["r"]))
        phase = wait_pod(dst, names["r"], deadline, sleep, now, stop)
        r = parse_logs(pod_logs(dst, names["r"]))
        removed = r.get("removed") == 1
        if phase != "Succeeded" or "read_end" not in r:
            raise LinkError("read-failed", "the target pod could not read the data from the backup target")
        out["read_s"] = round(r["read_end"] - r["read_start"], 3)
        out["read_bps"] = _rate(out["size_bytes"], out["read_s"])
        emit("read", "done", f"read in {out['read_s']} s")
        return out
    except LinkError as e:
        e.facts = out
        raise
    finally:
        for kube, name in created:
            _delete(kube, name)
        if written and not removed:
            out["cleanup"] = _cleanup(src, dst, names["c"], test_id, img_src, img_dst, server, path, sleep, now)
        else:
            out["cleanup"] = "ok"


def _cleanup(src, dst, name, test_id, img_src, img_dst, server, path, sleep, now):
    """Fichier dont l'effacement n'est pas confirmé : une pod de nettoyage,
    sur la source, sinon sur la cible. Rend `ok` ou la raison de l'échec."""
    last = ""
    for kube, image in ((src, img_src), (dst, img_dst)):
        try:
            kube.create(pod(name, test_id, image, server, path, cleanup_script(test_id),
                            deadline_s=CLEANUP_WAIT_S))
        except Exception as e:                  # noqa: BLE001 - l'autre cluster essaie
            last = f"cleanup pod refused: {str(e)[:200]}"
            continue
        try:
            phase = wait_pod(kube, name, CLEANUP_WAIT_S, sleep, now)
            done = phase == "Succeeded" and parse_logs(pod_logs(kube, name)).get("removed") == 1
        except Exception as e:                  # noqa: BLE001
            done, last = False, str(e)[:200]
        finally:
            _delete(kube, name)
        if done:
            return "ok"
        last = last or "cleanup pod failed"
    return f"failed: {last}; remove {DIR}/{test_id}.bin from the backup target by hand"
