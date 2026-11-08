#!/usr/bin/env python3
"""Agent de mise à jour de harvester-ops, côté hôte (v1.82.0).

Lancé en root par `harvester-ops-update.service` quand la console dépose
<état>/updates/request.json (unité `harvester-ops-update.path`). Vérifie la
signature de l'archive demandée, installe la version par son propre
`install.sh --upgrade`, redémarre la console et revient en arrière tout seul si
la nouvelle version ne répond pas. Chaque étape est écrite dans
<état>/updates/status.json, que la console lit.

Conception : docs/design/2026-10-01-mise-a-jour-console.md.

Usage : harvester-ops-update            (traite la demande en attente)
        harvester-ops-update --status   (affiche le dernier état)
"""
import json
import os
import shutil
import ssl
import stat
import subprocess
import sys
import tarfile
import time
import traceback
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import self_update as su  # noqa: E402
import container_runtime as cr  # noqa: E402
import safe_fs as sf  # noqa: E402

STATE_DIR = Path(os.environ.get("HARVESTER_OPS_STATE_DIR", "/var/lib/harvester-ops"))
ETC_DIR = Path(os.environ.get("HARVESTER_OPS_ETC", "/etc/harvester-ops"))
OPT_DIR = Path(os.environ.get("HARVESTER_OPS_OPT", "/opt/harvester-ops"))
PREFIX = Path(os.environ.get("HARVESTER_OPS_PREFIX", "/usr/local/bin"))
WORK_DIR = Path(os.environ.get("HARVESTER_OPS_UPDATE_WORK", "/var/lib/harvester-ops-update"))
LOG_DIR = Path(os.environ.get("HARVESTER_OPS_LOG_DIR", "/var/log/harvester-ops"))
UNIT_DIR = Path(os.environ.get("HARVESTER_OPS_UNIT_DIR", "/etc/systemd/system"))
SERVICE = "harvester-ops"
IMAGE = "localhost/harvester-ops:latest"
PREVIOUS = "localhost/harvester-ops:previous"
HEALTH_TIMEOUT = int(os.environ.get("HARVESTER_OPS_UPDATE_HEALTH_TIMEOUT", "180"))
UNITS = ("harvester-ops.service", "harvester-ops-update.service", "harvester-ops-update.path")


class Agent:
    def __init__(self, run=subprocess.run, sleep=time.sleep, now=time.time):
        self.run_cmd = run
        self.sleep = sleep
        self.now = now
        self.updates = STATE_DIR / "updates"
        self.status = {}
        self.log_path = None
        self.log_fd = None
        self.upd_dir = None          # <état>/updates, tenu par descripteur (2.8.3)

    # -- journal et état -------------------------------------------------
    # 2.8.3 : <état>/updates et le répertoire des journaux appartiennent au
    # compte de la console. Rien n'y est ouvert par un chemin : le répertoire
    # est tenu par descripteur, le journal est un fichier neuf (O_EXCL,
    # O_NOFOLLOW) gardé ouvert, l'état est écrit sous un nom neuf puis renommé,
    # et chaque fichier reçoit son groupe sur son descripteur. Chaque ligne de
    # journal est nettoyée des retours à la ligne et caractères de contrôle.
    def _write_log(self, text):
        if self.log_fd is None:
            return
        data = "".join(sf.clean_line(t) + "\n" for t in text.splitlines() or [""])
        try:
            os.write(self.log_fd, data.encode("utf-8", "replace"))
        except OSError:
            pass

    def log(self, msg):
        line = time.strftime("%Y-%m-%d %H:%M:%S ", time.localtime(self.now())) + sf.clean_line(msg)
        print(line, flush=True)
        self._write_log(line)

    def step(self, sid, state, message=""):
        message = sf.clean_line(message, 2000)
        steps = self.status.setdefault("steps", [])
        for s in steps:
            if s["id"] == sid:
                s.update(status=state, message=message, ts=self.now())
                break
        else:
            steps.append({"id": sid, "status": state, "message": message, "ts": self.now()})
        self.status["ts"] = self.now()
        self.log(f"[{sid}] {state} {message}".rstrip())
        self.save()

    def finish(self, state, message):
        message = sf.clean_line(message, 2000)
        self.status.update(state=state, message=message, ended=self.now())
        self.log(f"=> {state}: {message}")
        self.save()

    def save(self):
        """status.json : fichier de root, lisible par le groupe de la console,
        écrit sous un nom neuf puis renommé (un lien posé à sa place est
        remplacé, jamais suivi)."""
        if self.upd_dir is None:
            return
        data = json.dumps(self.status, indent=2).encode()
        try:
            self.upd_dir.write(su.STATUS, data, 0o644, _console_group())
        except OSError as e:
            print(f"status not written: {e}", file=sys.stderr, flush=True)

    def _open_log(self):
        """Un journal neuf dans LOG_DIR : jamais un fichier existant, jamais
        un lien posé d'avance sous le nom attendu."""
        try:
            with sf.SafeDir.open_base(LOG_DIR, create_mode=0o750, owner=_service_owner()) as d:
                stem = f"update-{time.strftime('%Y%m%d-%H%M%S', time.localtime(self.now()))}"
                for i in range(20):
                    name = f"{stem}.log" if i == 0 else f"{stem}-{i if i < 10 else sf.secrets.token_hex(4)}.log"
                    try:
                        self.log_fd = d.open_new_append(name, 0o640, _console_group())
                    except FileExistsError:
                        continue
                    self.log_path = LOG_DIR / name
                    return
        except OSError as e:
            print(f"update log not written: {e}", file=sys.stderr, flush=True)

    def _close(self):
        if self.log_fd is not None:
            os.close(self.log_fd)
            self.log_fd = None
        if self.upd_dir is not None:
            self.upd_dir.close()
            self.upd_dir = None

    def sh(self, argv, check=True, timeout=1800, **kw):
        r = self.run_cmd(argv, capture_output=True, text=True, timeout=timeout, **kw)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        if out:
            self._write_log(out)
        if check and r.returncode != 0:
            tail = out.splitlines()[-1] if out else f"exit {r.returncode}"
            raise su.UpdateError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''}: {tail}")
        return r

    def verify_in_image(self, archive, signature, signers):
        """La vérification de signature faite par le ssh-keygen de l'image de
        la console installée (v1.87.2), pour un hôte dont l'OpenSSH ne connaît
        pas `-Y` (RHEL 8). Conteneur jetable, sans réseau, système en lecture
        seule ; il ne voit que la signature et les clés de confiance, copiées
        à part, et lit l'archive sur son entrée standard. L'image est déjà
        installée et approuvée : rien n'est téléchargé."""
        runtime = _runtime()
        vdir = WORK_DIR / "verify"
        shutil.rmtree(vdir, ignore_errors=True)
        vdir.mkdir(mode=0o755)
        shutil.copyfile(signature, vdir / "release.sig")
        shutil.copyfile(signers, vdir / "signers")
        for f in vdir.iterdir():
            f.chmod(0o644)
        argv = [runtime, "run", "--rm", "-i", "--network", "none", "--read-only", "--user", "0",
                "--entrypoint", "ssh-keygen", "-v", f"{vdir}:/verify:ro,Z", IMAGE,
                "-Y", "verify", "-f", "/verify/signers", "-I", su.SIG_IDENTITY,
                "-n", su.SIG_NAMESPACE, "-s", "/verify/release.sig"]
        try:
            with open(archive, "rb") as data:
                return self.run_cmd(argv, stdin=data, capture_output=True, text=True, timeout=600)
        except FileNotFoundError:
            return None
        finally:
            shutil.rmtree(vdir, ignore_errors=True)

    # -- traitement ------------------------------------------------------
    def process(self):
        try:
            self.upd_dir = _open_updates()
        except OSError as e:
            print(f"harvester-ops-update: {e}; nothing done", file=sys.stderr, flush=True)
            return 1
        try:
            return self._process_request()
        finally:
            self._close()

    def _process_request(self):
        # La demande : un fichier ordinaire de la console, jamais un lien ni
        # un tube ; elle est retirée d'abord (l'unité de chemin ne se relance
        # pas, une demande illisible ne bloque pas les suivantes).
        if not self.upd_dir.lexists(su.REQUEST):
            return 0
        try:
            raw = self.upd_dir.read(su.REQUEST, limit=64 * 1024)
            req = json.loads(raw) if raw is not None else None
        except (OSError, ValueError):
            req = None
        self.upd_dir.set_aside(su.REQUEST)      # un répertoire posé là ne relance pas l'unité
        current = _installed_version()
        self._open_log()
        who = req.get("requested_by") if isinstance(req, dict) else None
        self.status = {"state": "running", "from": current, "to": None, "started": self.now(),
                       "requested_by": sf.clean_line(who, 128) if isinstance(who, str) else None,
                       "log": self.log_path.name if self.log_path else None, "steps": []}
        try:
            if not isinstance(req, dict):
                raise su.UpdateError("unreadable request")
            return self._process(req, current)
        except su.UpdateError as e:
            self.finish("failed", str(e))
            return 1
        except Exception as e:
            # An unexpected host/runtime error must not leave the console
            # showing an update in progress. Keep the traceback in journald;
            # do not retry commands or initiate another installation here.
            traceback.print_exc()
            self.finish("failed", f"Unexpected {type(e).__name__}; see journalctl -u harvester-ops-update.service")
            return 1

    def _process(self, req, current):
        # 1. copie dans un répertoire root : la console ne peut plus échanger
        #    l'archive entre la vérification et l'installation
        self.step("verify", "running", "copying and checking the release")
        name = str(req.get("archive") or "")
        if not su.ARCHIVE_RE.match(name):
            raise su.UpdateError("bad archive name in the request")
        # 2.8.3 : le répertoire préparé et l'archive s'ouvrent sans suivre de
        # lien, et la copie se fait depuis le descripteur ouvert.
        try:
            staged = self.upd_dir.subdir(su.STAGED)
        except OSError:
            raise su.UpdateError("the archive is not in the staging directory") from None
        with staged:
            src = _open_plain(staged, name)
            if src is None:
                raise su.UpdateError("the archive is not in the staging directory")
            if src == "hardlinked":
                raise su.UpdateError("the staged archive has several hard links; copy it instead")
            if WORK_DIR.exists():
                shutil.rmtree(WORK_DIR)
            WORK_DIR.mkdir(mode=0o700, parents=True)
            archive = WORK_DIR / name
            with src:
                _copy_bounded(src, archive)
            sig = None
            sig_src = _open_plain(staged, name + ".sig")
            if sig_src not in (None, "hardlinked"):
                sig = WORK_DIR / (name + ".sig")
                with sig_src:
                    _copy_bounded(sig_src, sig)
        info = su.inspect_archive(archive)
        self.status["to"] = info["version"]
        # relecture des corrections finales m1 : la version lue dans l'archive
        # est celle que la console a contrôlée (retour sous la 2.9.0 compris) ;
        # une archive remplacée entre son garde et ce passage est refusée
        if (req.get("version") is not None or req.get("allow_older")) \
                and str(req.get("version") or "") != info["version"]:
            raise su.UpdateError(f"the archive holds {info['version']}, not the requested "
                                 f"{req.get('version') or '(none)'}")
        signers = su.signers_file(ETC_DIR, OPT_DIR)
        if sig is not None or not su.allow_unsigned(ETC_DIR / "update.conf"):
            su.verify_signature(archive, sig, signers, runner=self.run_cmd, in_image=self.verify_in_image)
            how = f"signature checked with {signers}"
        else:
            how = "UNSIGNED, accepted because allow_unsigned=true in update.conf"
        if not su.is_newer(info["version"], current) and not req.get("allow_older"):
            raise su.UpdateError(f"{info['version']} is not newer than the installed {current}")
        self.step("verify", "done", f"{info['version']}, {how}")

        # 2. extraction
        self.step("extract", "running")
        su.extract_archive(archive, WORK_DIR)
        root = WORK_DIR / info["top"]
        self.step("extract", "done", str(root))

        # 3. sauvegarde de l'existant
        self.step("backup", "running")
        backup = WORK_DIR / "previous.tar"
        self._backup(backup)
        runtime = _runtime()
        self.sh([runtime, "tag", IMAGE, PREVIOUS], check=False)
        self.step("backup", "done", f"files and image of {current} kept")

        # 4. installation, 5. redémarrage, 6. contrôle
        try:
            self.step("install", "running", f"install.sh --upgrade of {info['version']}")
            self.sh(["bash", str(root / "install.sh"), "--upgrade"], cwd=str(root),
                    env={**os.environ, "HARVESTER_OPS_UPGRADE": "1",
                         cr.OVERRIDE: runtime})
            self.step("install", "done")
            self.step("restart", "running")
            self.sh(["systemctl", "daemon-reload"], check=False)
            self.sh(["systemctl", "restart", SERVICE])
            self.step("restart", "done")
            self.step("check", "running", f"waiting for {info['version']} to answer")
            self._wait_healthy(info["version"])
            self.step("check", "done", f"{info['version']} answers")
        except su.UpdateError as e:
            self.step("rollback", "running", str(e))
            self._rollback(backup, runtime)
            self.step("rollback", "done", f"{current} restored")
            self.finish("rolled-back", f"{info['version']} failed ({e}); {current} restored")
            return 1
        shutil.rmtree(WORK_DIR, ignore_errors=True)
        try:
            with self.upd_dir.subdir(su.STAGED) as staged:
                for n in (name, name + ".sig", name + ".check.json"):
                    staged.unlink(n)
        except OSError:
            pass
        self.finish("done", f"{current} -> {info['version']}")
        return 0

    def _backup(self, backup):
        with tarfile.open(backup, "w") as tf:
            for p in [OPT_DIR, *sorted(PREFIX.glob("harvester-*")), PREFIX / "lib",
                      *[UNIT_DIR / u for u in UNITS]]:
                if p.exists() or p.is_symlink():
                    tf.add(str(p), arcname=str(p).lstrip("/"))

    def _rollback(self, backup, runtime):
        for p in [OPT_DIR, PREFIX / "lib"]:
            shutil.rmtree(p, ignore_errors=True)
        for p in PREFIX.glob("harvester-*"):
            p.unlink(missing_ok=True)
        with tarfile.open(backup) as tf:
            try:
                tf.extractall("/", filter="tar")
            except TypeError:
                tf.extractall("/")
        self.sh([runtime, "tag", PREVIOUS, IMAGE], check=False)
        self.sh(["systemctl", "daemon-reload"], check=False)
        self.sh(["systemctl", "restart", SERVICE], check=False)
        try:
            self._wait_healthy(None)
        except su.UpdateError as e:
            self.log(f"after rollback: {e}")

    def _wait_healthy(self, version):
        deadline = self.now() + HEALTH_TIMEOUT
        last = "no answer"
        while self.now() < deadline:
            ok, got = _probe(self.run_cmd)
            if ok and (version is None or got == version):
                return
            last = f"answers as {got}" if ok else "no answer"
            self.sleep(3)
        raise su.UpdateError(f"the console did not come back as {version or 'before'} ({last})")


# ---------------------------------------------------------------------------
def _installed_version():
    try:
        return (OPT_DIR / "VERSION").read_text().strip()
    except OSError:
        return "0.0.0"


def _runtime():
    try:
        return cr.select_runtime(UNIT_DIR)
    except cr.RuntimeSelectionError as e:
        raise su.UpdateError(str(e)) from e


def _bind():
    """Port de la console, lu dans config.yaml (web.bind_port)."""
    import re
    try:
        m = re.search(r"^\s+bind_port:\s*(\d+)", (ETC_DIR / "config.yaml").read_text(), re.M)
        return int(m.group(1)) if m else 8090
    except (OSError, ValueError):
        return 8090


def _probe(run):
    """(répond, version) : /healthz du service, et la version lue DANS le
    conteneur qui tourne (jamais exposée sans connexion)."""
    port = _bind()
    ok = False
    for scheme in ("https", "http"):
        try:
            ctx = ssl._create_unverified_context() if scheme == "https" else None
            with urllib.request.urlopen(f"{scheme}://127.0.0.1:{port}/healthz", timeout=3,
                                        context=ctx) as r:
                ok = r.status == 200
                break
        except Exception:
            continue
    if not ok:
        return False, None
    r = run([_runtime(), "exec", SERVICE, "cat", "/opt/harvester-ops/VERSION"],
            capture_output=True, text=True, timeout=30)
    return True, (r.stdout or "").strip() if r.returncode == 0 else None


def _service_owner():
    """(uid, gid) du compte de la console pour un répertoire que l'agent
    recrée, ou None (compte absent, agent non root : tests)."""
    return sf.service_ids() if os.geteuid() == 0 else None


def _console_group():
    """Les fichiers de l'agent restent à root et la console les lit par son
    groupe (2.8.3 ; avant, ils lui étaient donnés par un chown qui suivait
    les liens). None si le compte n'existe pas ou si l'agent n'est pas root."""
    ids = _service_owner()
    return None if ids is None else (-1, ids[1])


def _open_updates():
    """<état>/updates tenu par descripteur. Un lien posé à sa place est
    retiré (sans être suivi) et refusé : rien n'est lu ni écrit au travers."""
    base = sf.SafeDir.open_base(STATE_DIR)
    try:
        try:
            return base.subdir("updates", create_mode=0o750, owner=_service_owner())
        except sf.UnsafePath:
            base.unlink("updates")
            raise
    finally:
        base.close()


def _open_plain(d, name):
    """Fichier ordinaire de `d` ouvert en lecture sans suivre de lien, ou
    None (absent, lien, autre chose), ou "hardlinked" (plusieurs liens
    physiques : refusé, il pourrait désigner un fichier de root)."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOCTTY,
                     dir_fd=d.fd)
    except OSError:
        return None
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        return None
    if st.st_nlink != 1:
        os.close(fd)
        return "hardlinked"
    f = os.fdopen(fd, "rb")
    f.size_at_open = st.st_size
    return f


def _copy_bounded(src, dest):
    """Copie au plus la taille vue à l'ouverture : la console, propriétaire
    du fichier, ne peut pas le faire grossir pendant la copie pour remplir le
    disque de l'hôte. Un fichier qui change de taille est refusé."""
    left = src.size_at_open
    with open(dest, "wb") as out:
        while left > 0:
            b = src.read(min(left, 1 << 20))
            if not b:
                break
            out.write(b)
            left -= len(b)
    if left != 0 or src.read(1):
        raise su.UpdateError("the staged file changed while it was copied")


def main(argv):
    if "--status" in argv:
        try:
            with _open_updates() as d:
                raw = d.read(su.STATUS)
            st = json.loads(raw) if raw else {}
        except (OSError, ValueError):
            st = {}
        print(json.dumps(st, indent=2))
        return 0
    return Agent().process()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
