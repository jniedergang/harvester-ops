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
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import self_update as su  # noqa: E402

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

    # -- journal et état -------------------------------------------------
    def log(self, msg):
        line = time.strftime("%Y-%m-%d %H:%M:%S ", time.localtime(self.now())) + msg
        print(line, flush=True)
        if self.log_path:
            with open(self.log_path, "a") as f:
                f.write(line + "\n")

    def step(self, sid, state, message=""):
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
        self.status.update(state=state, message=message, ended=self.now())
        self.log(f"=> {state}: {message}")
        self.save()

    def save(self):
        su.write_json_atomic(self.updates / su.STATUS, self.status, mode=0o644)
        _give_to_service(self.updates / su.STATUS)

    def sh(self, argv, check=True, timeout=1800, **kw):
        r = self.run_cmd(argv, capture_output=True, text=True, timeout=timeout, **kw)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        if out and self.log_path:
            with open(self.log_path, "a") as f:
                f.write(out + "\n")
        if check and r.returncode != 0:
            tail = out.splitlines()[-1] if out else f"exit {r.returncode}"
            raise su.UpdateError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''}: {tail}")
        return r

    # -- traitement ------------------------------------------------------
    def process(self):
        req_path = self.updates / su.REQUEST
        req = su.read_json(req_path)
        # La demande est retirée d'abord : l'unité de chemin ne se relance
        # pas, et une demande illisible ne bloque pas les suivantes.
        try:
            req_path.unlink()
        except FileNotFoundError:
            return 0
        current = _installed_version()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log_path = LOG_DIR / f"update-{time.strftime('%Y%m%d-%H%M%S', time.localtime(self.now()))}.log"
        self.status = {"state": "running", "from": current, "to": None, "started": self.now(),
                       "requested_by": (req or {}).get("requested_by"),
                       "log": self.log_path.name, "steps": []}
        try:
            if not isinstance(req, dict):
                raise su.UpdateError("unreadable request")
            return self._process(req, current)
        except su.UpdateError as e:
            self.finish("failed", str(e))
            return 1
        finally:
            _give_to_service(self.log_path)

    def _process(self, req, current):
        # 1. copie dans un répertoire root : la console ne peut plus échanger
        #    l'archive entre la vérification et l'installation
        self.step("verify", "running", "copying and checking the release")
        staged = (self.updates / su.STAGED).resolve()
        name = str(req.get("archive") or "")
        if not su.ARCHIVE_RE.match(name):
            raise su.UpdateError("bad archive name in the request")
        src = (staged / name)
        if src.is_symlink() or src.resolve().parent != staged or not src.is_file():
            raise su.UpdateError("the archive is not in the staging directory")
        if WORK_DIR.exists():
            shutil.rmtree(WORK_DIR)
        WORK_DIR.mkdir(mode=0o700, parents=True)
        archive = WORK_DIR / name
        shutil.copyfile(src, archive)
        sig_src = staged / (name + ".sig")
        sig = None
        if sig_src.is_file() and not sig_src.is_symlink():
            sig = WORK_DIR / (name + ".sig")
            shutil.copyfile(sig_src, sig)
        info = su.inspect_archive(archive)
        self.status["to"] = info["version"]
        signers = su.signers_file(ETC_DIR, OPT_DIR)
        if sig is not None or not su.allow_unsigned(ETC_DIR / "update.conf"):
            su.verify_signature(archive, sig, signers, runner=self.run_cmd)
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
                    env={**os.environ, "HARVESTER_OPS_UPGRADE": "1"})
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
            src.unlink()
            sig_src.unlink(missing_ok=True)
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
    for r in ("podman", "docker"):
        if shutil.which(r):
            return r
    return "podman"


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


def _give_to_service(path):
    """Le fichier d'état et le journal se lisent depuis la console."""
    try:
        import grp
        import pwd
        shutil.chown(path, pwd.getpwnam("harvester-ops").pw_uid, grp.getgrnam("harvester-ops").gr_gid)
    except (KeyError, OSError, TypeError):
        pass


def main(argv):
    if "--status" in argv:
        print(json.dumps(su.read_json(STATE_DIR / "updates" / su.STATUS, {}), indent=2))
        return 0
    return Agent().process()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
