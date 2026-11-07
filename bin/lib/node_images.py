"""harvester-ops : images importées dans le containerd des nœuds Harvester (v2.6.0).

Sans registre joignable, une image arrive sur un nœud par `ctr images
import`, par SSH : c'était le geste de `harvester-capi install
--push-images` (paquet Cluster API de l'installation d'avant Turtles) ; le
registre d'amorçage de la 2.6.0 en a besoin aussi pour sa propre image.
Un seul code pour les deux (extrait de `harvester-capi push_images`).

Bibliothèque standard seulement (plus ssh).
"""

import re
import shlex
import subprocess
from pathlib import Path

# L'archive arrive sur l'entrée standard ; gzip ou tar nu, les deux passent.
REMOTE = ("set -eo pipefail; TMP=$(mktemp); cat > \"$TMP\"; "
          "{ zcat \"$TMP\" 2>/dev/null || cat \"$TMP\"; } | sudo /var/lib/rancher/rke2/bin/ctr "
          "--address /run/k3s/containerd/containerd.sock --namespace k8s.io images import -; "
          "rm -f \"$TMP\"")


def ssh_options(ssh):
    opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new", "-p", str(ssh.get("port", 22))]
    if ssh.get("key"):
        opts += ["-i", ssh["key"]]
    return opts


def import_on_nodes(entry, files, step, sid="images", runner=subprocess.run):
    """Chaque archive de `files` importée sur chaque nœud de `entry` (entrée
    de cluster de la configuration : `nodes[].ip`, `ssh`). `step(étape,
    statut, message)` reçoit l'avancement. RuntimeError au premier échec, qui
    nomme le nœud et le fichier, jamais la sortie de la commande."""
    ssh = entry.get("ssh") or {}
    nodes = [n.get("ip") for n in entry.get("nodes") or [] if n.get("ip")]
    step(sid, "running", f"{len(files)} image(s) to load on {len(nodes)} node(s)")
    opts = ssh_options(ssh)
    for node in nodes:
        for f in files:
            with open(f, "rb") as fh:
                p = runner(["ssh", *opts, f"{ssh.get('user', 'rancher')}@{node}", REMOTE],
                           stdin=fh, capture_output=True, timeout=600)
            if p.returncode != 0:
                raise RuntimeError(f"{node}: import of {Path(f).name} failed")
            step(sid, "running", f"{node}: {Path(f).name}")
    step(sid, "done", f"{len(files) * len(nodes)} image import(s)")
    return {"nodes": len(nodes), "imports": len(files) * len(nodes)}


# 2.7.0 : retrait de la pile Turtles (`harvester-capi uninstall
# --remove-images`). Une référence d'image n'a ni espace ni guillemet : tout
# autre caractère est refusé avant de composer la commande distante.
IMAGE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$")
CTR = ("sudo /var/lib/rancher/rke2/bin/ctr --address /run/k3s/containerd/containerd.sock "
       "--namespace k8s.io images rm")


def remove_on_nodes(entry, names, step, sid="images", runner=subprocess.run):
    """Les images `names` effacées du containerd de chaque nœud de `entry`,
    par SSH (le pendant de `import_on_nodes`). Une image absente d'un nœud
    n'est pas une erreur. RuntimeError au premier nœud en échec (nommé,
    jamais la sortie de la commande) ou pour une référence refusée."""
    bad = [n for n in names if not IMAGE_REF_RE.match(n or "")]
    if bad:
        raise RuntimeError(f"invalid image reference: {bad[0][:80]!r}")
    ssh = entry.get("ssh") or {}
    nodes = [n.get("ip") for n in entry.get("nodes") or [] if n.get("ip")]
    step(sid, "running", f"{len(names)} image(s) to remove from {len(nodes)} node(s)")
    if not names:
        step(sid, "done", "no image to remove")
        return {"nodes": len(nodes), "removed": 0}
    opts = ssh_options(ssh)
    # `|| true` : ctr échoue pour une image déjà absente, ce n'est pas une panne
    remote = "for i in " + " ".join(shlex.quote(n) for n in names) + \
             f"; do {CTR} \"$i\" >/dev/null 2>&1 || true; done"
    for node in nodes:
        p = runner(["ssh", *opts, f"{ssh.get('user', 'rancher')}@{node}", remote],
                   capture_output=True, timeout=300)
        if p.returncode != 0:
            raise RuntimeError(f"{node}: image removal failed")
        step(sid, "running", f"{node}: {len(names)} image(s) removed")
    step(sid, "done", f"{len(names)} image(s) removed from {len(nodes)} node(s)")
    return {"nodes": len(nodes), "removed": len(names) * len(nodes)}


BUNDLE_TAR = "registry-images.tar"
BUNDLE_MAP = "registry-images.json"
BUNDLE_SUMS = "SHA256SUMS"


def bundle_from_package(package, out_dir):
    """Le paquet interne d'images (`scripts/registry-bundle.json`, construit
    par `harvester-artefacts pack`) rendu prêt pour les nœuds : archive pour
    `ctr images import` (`registry-images.tar`), noms épinglés par élément
    (`registry-images.json`, `{élément: <dépôt>@<empreinte>}`, ce qu'un pod
    référence avec `imagePullPolicy: Never`) et leurs sommes. Le paquet est
    extrait et vérifié (SHA256SUMS) dans un répertoire temporaire."""
    import json
    import shutil
    import tempfile

    import artefacts as art
    import oci_layout as ol
    from self_update import sha256_file

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="hops-bundle-", dir=out))
    try:
        art.extract_verified(package, work / "x")
        lay = ol.Layout(work / "x" / "images")
        index = json.loads((work / "x" / art.INDEX).read_text())
        names = [n for it in index["items"] for n in it.get("images") or []]
        ol.export_ctr_tar(lay, names, out / BUNDLE_TAR)
        images = {it["name"]: ol.pinned_name(lay.find(it["images"][0]))
                  for it in index["items"] if it.get("images")}
        (out / BUNDLE_MAP).write_text(json.dumps({"package": f"{index['name']}-{index['version']}",
                                                  "images": images}, indent=2) + "\n")
        (out / BUNDLE_SUMS).write_text("".join(f"{sha256_file(out / n)}  {n}\n" for n in (BUNDLE_TAR, BUNDLE_MAP)))
        return {"images": images, "names": names}
    finally:
        shutil.rmtree(work, ignore_errors=True)
