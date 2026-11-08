#!/usr/bin/env python3
"""Installe ou met à jour le provider Terraform Harvester.

Source acceptée, dans l'ordre de résolution :

  * une VERSION (`1.7.3`, `v1.7.3`) -> l'archive officielle de la release
    GitHub correspondante (ou HARVESTER_PROVIDER_BASE_URL), et la somme
    SHA-256 publiée à côté d'elle, toujours vérifiée ;
  * une URL http(s) -> téléchargée telle quelle (miroir interne, serveur
    HTTP d'un site airgap) ;
  * un CHEMIN local -> archive .zip ou binaire déjà présent sur la machine,
    seule voie utilisable sans réseau.

Le résultat est toujours le même : un binaire exécutable dans
`<dest>/bin/terraform-provider-harvester`, plus un `provider.json` qui garde
la version, l'empreinte et la provenance. harvester-ops lit ce fichier pour
afficher la version réellement active, que le binaire vienne du livrable ou
d'une mise à jour posée par l'opérateur.

Le script n'a aucune dépendance hors bibliothèque standard : il doit tourner
sur un hôte airgap où seuls python3 et le tarball du livrable existent.

Progression émise sur stderr au format STEP_EVENT|<étape>|<statut>|<message>,
consommée telle quelle par la chaîne SSE de l'interface.
"""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

REPO = "harvester/terraform-provider-harvester"
RELEASE_URL = "https://github.com/" + REPO + "/releases/download"
# Adresse des releases remplaçable (miroir interne, copie locale des fichiers
# officiels en file://), comme LIBVIRT_PROVIDER_BASE_URL : l'archive ET son
# SHA256SUMS y sont lus, la somme reste vérifiée.
BASE_URL_ENV = "HARVESTER_PROVIDER_BASE_URL"
# Développement seulement : installer une VERSION dont la somme publiée est
# illisible. Sans elle, une somme invérifiable est un échec (2.9.0).
ALLOW_UNVERIFIED_ENV = "HARVESTER_PROVIDER_ALLOW_UNVERIFIED"


def release_url():
    return (os.environ.get(BASE_URL_ENV) or RELEASE_URL).rstrip("/")

# Une version pure, éventuellement préfixée d'un v et suivie d'un suffixe de
# pré-release (`1.8.0-rc1`). Tout le reste est traité comme une URL ou un
# chemin.
VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$")

# Nom du binaire dans l'archive officielle : `terraform-provider-harvester`
# éventuellement suffixé de sa version (`..._v1.7.3`).
MEMBER_RE = re.compile(r"^terraform-provider-harvester(?:_v?\d[\w.+-]*)?$")

# Un provider fait ~55 Mo décompressé. La borne protège d'une archive
# piégée : sans elle, l'extraction écrirait sur le disque jusqu'à le remplir.
MAX_MEMBER_BYTES = 512 * 1024 * 1024

# e_machine ELF -> architecture Go, pour refuser tôt un binaire d'une autre
# architecture. Sans ce contrôle, terraform échoue bien plus loin sur un
# « fork/exec: exec format error » qui ne désigne pas la cause.
ELF_MACHINES = {0x3E: "amd64", 0xB7: "arm64"}


def step(sid, status, message=""):
    sys.stderr.write("STEP_EVENT|%s|%s|%s\n" % (sid, status, message))
    sys.stderr.flush()


class Failure(Exception):
    """Erreur attendue : un message pour l'opérateur, pas une trace."""

    def __init__(self, sid, message):
        super().__init__(message)
        self.sid = sid


def host_arch():
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    return m


# ---------------------------------------------------------------------------
# Récupération de la source
# ---------------------------------------------------------------------------
def download(url, dest, label="téléchargement"):
    """Télécharge en flux avec progression agrégée.

    Agrégée volontairement : le tampon d'évènements d'une action est borné
    (500), une ligne par bloc de 256 Kio le remplirait de bruit et chasserait
    les étapes utiles.
    """
    import urllib.request

    step("download", "running", "%s : %s" % (label, url))
    done = 0
    last = -1
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            total = int(r.headers.get("Content-Length") or 0)
            with open(dest, "wb") as f:
                while True:
                    chunk = r.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = int(done * 100 / total)
                        if pct != last and pct % 5 == 0:
                            last = pct
                            step("download", "progress",
                                 "%d%% (%d Mio)" % (pct, done // (1024 * 1024)))
    except Exception as e:
        raise Failure("download", "%s : %s" % (url, e))
    step("download", "done", "%d Mio reçus" % (done // (1024 * 1024)))
    return done


def published_sha256(version, asset_name):
    """Somme publiée dans le SHA256SUMS de la release.

    Une somme illisible (release injoignable, miroir qui ne sert pas le
    fichier, archive absente de la liste) est un échec : installer une
    version sans l'avoir vérifiée laisserait croire à une vérification
    faite (vu à la 2.8.2 pendant une panne de GitHub). Seule exception,
    pour le développement : HARVESTER_PROVIDER_ALLOW_UNVERIFIED=1, dit
    bien haut dans les étapes et dans provider.json (`verified: false`).
    """
    import urllib.request

    url = "%s/v%s/terraform-provider-harvester_%s_SHA256SUMS" % (
        release_url(), version, version)
    why = None
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            body = r.read(65536).decode("utf-8", "replace")
    except Exception as e:
        why = "somme publiée indisponible (%s)" % e
    else:
        for line in body.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == asset_name:
                return parts[0].lower()
        why = "%s absent du SHA256SUMS" % asset_name
    if os.environ.get(ALLOW_UNVERIFIED_ENV) == "1":
        step("checksum", "warn",
             "%s : EMPREINTE NON VÉRIFIÉE (%s=1, développement seulement)"
             % (why, ALLOW_UNVERIFIED_ENV))
        return ""
    raise Failure("checksum", "%s : installation refusée, une version doit être "
                  "vérifiée (miroir des fichiers officiels : %s, ou --sha256)"
                  % (why, BASE_URL_ENV))


def resolve_source(source, arch, workdir, published=True):
    """Ramène toute source à (fichier local, version déduite, empreinte
    attendue, provenance lisible). `published=False` : l'empreinte est
    donnée par --sha256, la somme publiée n'est pas lue."""
    m = VERSION_RE.match(source)
    if m:
        version = m.group(1)
        asset = "terraform-provider-harvester_%s_linux_%s.zip" % (version, arch)
        url = "%s/v%s/%s" % (release_url(), version, asset)
        step("resolve", "done", "version %s -> %s" % (version, url))
        local = workdir / asset
        download(url, local)
        return local, version, published_sha256(version, asset) if published else "", url

    if source.startswith(("http://", "https://")):
        name = source.rsplit("/", 1)[-1].split("?")[0] or "provider-download"
        step("resolve", "done", "URL %s" % source)
        local = workdir / name
        download(source, local)
        return local, "", "", source

    local = Path(source).expanduser()
    if not local.is_file():
        raise Failure("resolve", "fichier introuvable : %s" % local)
    step("resolve", "done", "fichier local %s (%d Mio)"
         % (local, local.stat().st_size // (1024 * 1024)))
    return local, "", "", str(local)


# ---------------------------------------------------------------------------
# Vérification et extraction
# ---------------------------------------------------------------------------
def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(path, expected):
    digest = sha256_of(path)
    if expected:
        if digest != expected.lower():
            raise Failure(
                "checksum",
                "empreinte incorrecte : attendu %s, obtenu %s"
                % (expected, digest))
        step("checksum", "done", "%s (vérifiée)" % digest)
    else:
        step("checksum", "done", "%s (aucune référence à comparer)" % digest)
    return digest


def check_elf(path, arch):
    """Vérifie que c'est bien un ELF de l'architecture de l'hôte."""
    with open(path, "rb") as f:
        head = f.read(20)
    if len(head) < 20 or head[:4] != b"\x7fELF":
        raise Failure("install", "le fichier installé n'est pas un binaire ELF")
    machine = int.from_bytes(head[18:20], "little")
    got = ELF_MACHINES.get(machine)
    if got and got != arch:
        raise Failure(
            "install",
            "binaire %s alors que cet hôte est %s : terraform échouerait sur "
            "un « exec format error »" % (got, arch))


def extract_member(zip_path, workdir):
    """Sort le binaire du provider de l'archive.

    Deux gardes : le nom du membre doit être un nom simple (une archive
    piégée écrirait ailleurs via `../`), et sa taille déclarée est bornée.
    """
    with zipfile.ZipFile(zip_path) as z:
        picked = None
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if "/" in name or "\\" in name or name in (".", ".."):
                continue
            if MEMBER_RE.match(name):
                picked = info
                break
        if picked is None:
            listing = ", ".join(i.filename for i in z.infolist()[:10]) or "(vide)"
            raise Failure(
                "extract",
                "aucun binaire de provider dans l'archive : %s" % listing)
        if picked.file_size > MAX_MEMBER_BYTES:
            raise Failure("extract", "membre %s trop volumineux (%d octets)"
                          % (picked.filename, picked.file_size))
        out = workdir / "extracted-binary"
        with z.open(picked) as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
    step("extract", "done", "%s (%d Mio)"
         % (picked.filename, out.stat().st_size // (1024 * 1024)))
    version = ""
    m = re.search(r"_v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$", picked.filename)
    if m:
        version = m.group(1)
    return out, version


def sniff(path):
    with open(path, "rb") as f:
        magic = f.read(4)
    if magic[:2] == b"PK":
        return "zip"
    if magic == b"\x7fELF":
        return "elf"
    return "unknown"


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------
def install(binary, dest, version, digest, origin, arch, verified=False):
    """Pose le binaire de façon atomique et écrit sa fiche."""
    bindir = dest / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    final = bindir / "terraform-provider-harvester"

    tmp = bindir / (".incoming-%d" % os.getpid())
    shutil.copyfile(binary, tmp)
    tmp.chmod(0o755)
    check_elf(tmp, arch)
    # `replace` est atomique sur le même système de fichiers : à aucun
    # instant l'emplacement ne contient un binaire tronqué, qui ferait
    # échouer un `terraform init` concurrent.
    tmp.replace(final)

    meta = {
        "version": version,
        "sha256": digest,
        "verified": bool(verified),        # empreinte comparée à une référence (somme publiée ou --sha256)
        "source": origin,
        "arch": arch,
        "size": final.stat().st_size,
        "installed_at": time.time(),
        "binary": str(final),
    }
    (dest / "provider.json").write_text(json.dumps(meta, indent=2) + "\n")
    step("install", "done", "v%s -> %s (%d Mio)"
         % (version, final, meta["size"] // (1024 * 1024)))
    return meta


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Installe ou met à jour le provider Terraform Harvester.")
    p.add_argument("source",
                   help="version (1.7.3), URL http(s) d'une archive, ou "
                        "chemin local d'un .zip ou d'un binaire")
    p.add_argument("--dest", required=True,
                   help="répertoire d'installation géré")
    p.add_argument("--sha256", default="",
                   help="empreinte attendue de la source téléchargée ; "
                        "prioritaire sur celle publiée par la release")
    p.add_argument("--version", default="",
                   help="version à enregistrer quand la source ne la porte pas")
    p.add_argument("--arch", default=host_arch(),
                   help="architecture cible (défaut : celle de l'hôte)")
    p.add_argument("--source-label", default="",
                   help="provenance à enregistrer dans provider.json à la "
                        "place du chemin réel ; sert au téléversement, où le "
                        "chemin de transit ne dit rien à l'opérateur")
    args = p.parse_args(argv)

    dest = Path(args.dest).expanduser()
    workdir = Path(tempfile.mkdtemp(prefix="harvester-provider-"))
    try:
        local, version, pub_sha, origin = resolve_source(
            args.source, args.arch, workdir, published=not args.sha256)

        expected = args.sha256 or pub_sha
        digest = verify(local, expected)

        kind = sniff(local)
        if kind == "zip":
            binary, zip_version = extract_member(local, workdir)
            version = version or zip_version
        elif kind == "elf":
            step("extract", "done", "binaire brut, pas d'archive à ouvrir")
            binary = local
        else:
            raise Failure(
                "extract",
                "format non reconnu : ni archive zip ni binaire ELF. Une page "
                "HTML d'erreur téléchargée à la place du fichier en est la "
                "cause la plus fréquente.")

        version = args.version.lstrip("v") or version
        if not version:
            raise Failure(
                "install",
                "version indéterminable : terraform exige une version pour "
                "ranger le provider dans son miroir local. Utiliser "
                "--version X.Y.Z.")

        meta = install(binary, dest, version, digest,
                       args.source_label or origin, args.arch, verified=bool(expected))
    except Failure as e:
        step(e.sid, "error", str(e))
        return 1
    except KeyboardInterrupt:
        step("install", "error", "interrompu")
        return 130
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
