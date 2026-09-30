"""harvester-ops : démarrage de découverte d'une machine nue (1.78.0).

Quand le BMC ne publie aucun disque (un iLO 4 n'en publie aucun), l'ISO
Harvester démarre une fois avec un script à elle, renvoie ce que Linux voit
(disques, liens stables, cartes réseau) et éteint la machine.

Ce module porte le déroulé, commun à la console (action suivie
`baremetal-discover:<hôte>`) et à la ligne de commande
(`harvester-baremetal.py discover`). Chacun fournit :

* `bmc` : l'accès au BMC (profil, média virtuel, amorce unique,
  alimentation), voir `RedfishBmc` pour l'interface attendue ;
* `pxe` : le serveur d'artefacts (`web/pxe_server.py`), qui publie l'ISO et
  reçoit l'inventaire ;
* `remaster(cmd, step)` : lance `harvester-iso-remaster.sh` et renvoie son
  code de sortie.

Ce que le réel a appris (banc node2, ISO v1.9.0) et que ce module respecte :

* `systemd.run=` s'exécute aussi dans l'initrd : le script sort tout de
  suite si `/etc/initrd-release` existe ;
* pas de guillemets sur la ligne noyau : le script est DANS l'ISO, le média
  est monté en `/run/initramfs/live` (iso9660), la ligne ne porte qu'un
  chemin ;
* le script éteint lui-même la machine ; curl réessaie car le réseau n'est
  pas monté à son démarrage.

Stdlib seulement : utilisable hors de la console.
"""

import base64
import hashlib
import json
import os
import re
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

TEMPLATE = Path(__file__).resolve().with_name("discover.sh.tpl")
SCRIPT_ISO_PATH = "/discover.sh"
MEDIA_MOUNT = "/run/initramfs/live"          # vu en réel, ISO v1.9.0
# Le réseau monté dans l'initrd survit à la bascule de racine : c'est ce qui
# fait marcher l'installation par média virtuel, la découverte le reprend.
KERNEL_ARGS = (f"systemd.run={MEDIA_MOUNT}{SCRIPT_ISO_PATH} "
               "systemd.run_success_action=none systemd.run_failure_action=none "
               "ip=dhcp rd.neednet=1")
EXTRA_ARGS_RE = re.compile(r"[A-Za-z0-9 ._:/,=@+-]*")
UPLOAD_URL_RE = re.compile(r"http://[A-Za-z0-9.:\[\]-]+/pxe/inventory/[A-Za-z0-9_-]+")

INVENTORY_TIMEOUT = 15 * 60
POWEROFF_TIMEOUT = 5 * 60

_CACHE_LOCKS = {}
_CACHE_LOCKS_GUARD = threading.Lock()


class DiscoveryError(Exception):
    """Échec d'une étape : `step` et un message sans secret."""

    def __init__(self, step, message):
        super().__init__(message)
        self.step = step


class Cancelled(DiscoveryError):
    pass


# ---------------------------------------------------------------------------
# Script et ligne noyau
# ---------------------------------------------------------------------------

def template_version():
    """Empreinte du gabarit : une modification du script invalide le cache."""
    return hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()[:16]


def render_script(upload_url):
    """Le script déposé dans l'ISO. Seule l'adresse de dépôt y est écrite,
    et elle est vérifiée : elle finit entre guillemets dans du shell."""
    if not UPLOAD_URL_RE.fullmatch(upload_url or ""):
        raise ValueError("invalid upload URL")
    return TEMPLATE.read_text().replace("__UPLOAD_URL__", upload_url)


def clean_extra_args(extra):
    """Arguments noyau de l'opérateur (`console=ttyS1,115200`...), espaces
    normalisés ; ValueError s'ils pourraient sortir de la ligne grub, ou
    s'ils demandent une installation."""
    extra = " ".join(str(extra or "").split())
    if extra and not EXTRA_ARGS_RE.fullmatch(extra):
        raise ValueError("invalid extra kernel arguments")
    if "harvester.install." in extra or "systemd.run" in extra:
        raise ValueError("extra kernel arguments may not start an install")
    return extra


def kernel_args(extra=""):
    extra = clean_extra_args(extra)
    return f"{KERNEL_ARGS} {extra}" if extra else KERNEL_ARGS


# ---------------------------------------------------------------------------
# ISO de découverte, en cache
# ---------------------------------------------------------------------------

def source_identity(src):
    """Identité de l'ISO source sans relire ses 7 Go : son empreinte sha256
    si le magasin l'a posée à côté, sinon taille et date."""
    src = Path(src)
    sidecar = src.with_suffix(src.suffix + ".sha256")
    if sidecar.is_file():
        digest = (sidecar.read_text().split() or [""])[0]
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            return f"sha256:{digest}"
    st = src.stat()
    return f"size:{st.st_size}:mtime:{st.st_mtime_ns}"


def cache_key(src, base_url, extra=""):
    """Même ISO, même gabarit, même adresse de la console et mêmes arguments
    = même ISO de découverte. L'adresse en fait partie : elle est gravée
    dans le script."""
    blob = json.dumps({"src": source_identity(src), "tpl": template_version(),
                       "base": base_url, "args": kernel_args(extra)},
                      sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _cache_lock(path):
    with _CACHE_LOCKS_GUARD:
        return _CACHE_LOCKS.setdefault(str(path), threading.Lock())


def ensure_iso(src, cache_dir, base_url, extra, remaster_script, remaster, step):
    """ISO de découverte pour `src`, remasterisée seulement si la clé a
    changé. Renvoie (chemin, jeton de dépôt gravé, réutilisée ?).

    Une seule ISO en cache par ISO source (7 Go chacune) ; le jeton gravé
    n'est armé côté serveur que pendant une découverte, et consommé par le
    premier dépôt."""
    src = Path(src)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.chmod(0o700)
    iso = cache_dir / f"{src.stem}.discover.iso"
    meta = cache_dir / f"{src.stem}.discover.json"
    key = cache_key(src, base_url, extra)
    with _cache_lock(iso):
        try:
            known = json.loads(meta.read_text())
        except (OSError, ValueError):
            known = {}
        if (known.get("key") == key and iso.is_file()
                and re.fullmatch(r"[A-Za-z0-9_-]{16,}", str(known.get("slot") or ""))):
            step("remaster", "done", f"ISO de découverte en cache ({iso.name})")
            return iso, known["slot"], True

        slot = secrets.token_urlsafe(24)
        script = cache_dir / f".discover-{secrets.token_hex(4)}.sh"
        tmp_iso = cache_dir / f".{iso.name}.{secrets.token_hex(4)}.part"
        try:
            script.write_text(render_script(f"{base_url}/pxe/inventory/{slot}"))
            script.chmod(0o700)
            step("remaster", "running", f"remasterisation de {src.name} pour la découverte")
            cmd = ["/usr/bin/env", "bash", str(remaster_script),
                   "--src", str(src), "--out", str(tmp_iso),
                   "--kernel-args", kernel_args(extra),
                   "--add-file", f"{script}:{SCRIPT_ISO_PATH}:0755",
                   "--work-dir", str(cache_dir)]
            if remaster(cmd, step) != 0:
                raise DiscoveryError("remaster", "ISO remastering failed")
            meta.unlink(missing_ok=True)
            os.replace(tmp_iso, iso)
            fd = os.open(meta, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"key": key, "slot": slot, "source": src.name,
                           "at": time.time()}, f)
        finally:
            for p in (script, tmp_iso, tmp_iso.with_suffix(tmp_iso.suffix + ".sha256")):
                p.unlink(missing_ok=True)
        step("remaster", "done", f"ISO de découverte prête ({iso.name})")
        return iso, slot, False


# ---------------------------------------------------------------------------
# Magasin des inventaires : un fichier par numéro de série du système
# ---------------------------------------------------------------------------

def _serial_name(serial):
    name = re.sub(r"[^A-Za-z0-9._-]", "_", str(serial or "")).strip("._")
    if not name:
        raise ValueError("no system serial")
    return name[:128]


def store_inventory(store_dir, serial, bmc_host, raw, now=None):
    """Enregistre l'inventaire brut (0600). Le brut, pas l'analyse : une
    correction de l'analyseur profite ainsi aux inventaires déjà reçus."""
    store_dir = Path(store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)
    store_dir.chmod(0o700)
    path = store_dir / f"{_serial_name(serial)}.json"
    doc = {"source": "discovery", "at": now if now is not None else time.time(),
           "system_serial": str(serial), "bmc_host": bmc_host, "raw": raw}
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f)
    os.replace(tmp, path)
    return path


def load_inventory(store_dir, bmc_host):
    """Dernier inventaire reçu pour ce BMC, ou None."""
    best = None
    for p in Path(store_dir).glob("*.json"):
        try:
            doc = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if doc.get("bmc_host") == bmc_host and (best is None or doc.get("at", 0) > best.get("at", 0)):
            best = doc
    return best


# ---------------------------------------------------------------------------
# Déroulé
# ---------------------------------------------------------------------------

def run(opts, bmc, pxe, remaster, step, cancelled=lambda: False,
        sleep=time.sleep, clock=time.time):
    """Démarrage de découverte complet. `opts` :

    host, src_iso, cache_dir, work_dir, store_dir, remaster_script,
    advertise, port, extra_args ; facultatifs inventory_timeout,
    poweroff_timeout, poll.

    Renvoie {"serial", "path", "raw", "forced_off"} ; lève DiscoveryError.
    Quoi qu'il arrive, le média est éjecté et les jetons révoqués."""
    poll = opts.get("poll", 5)
    tokens = []
    inv_path = Path(opts["work_dir"]) / f"inventory-{secrets.token_hex(6)}.txt"
    inserted = powered = done = False

    def check_cancel(sid):
        if cancelled():
            raise Cancelled(sid, "cancelled by operator")

    try:
        # --- préflight : le BMC, son lecteur virtuel, le numéro de série ---
        step("preflight", "running", f"interrogation du BMC {opts['host']}")
        profile = bmc.profile()
        if not profile.get("ok"):
            raise DiscoveryError("preflight", profile.get("error") or "BMC unreachable")
        if not profile.get("virtualmedia_path"):
            raise DiscoveryError("preflight", "no CD virtual media (iLO Advanced licence?)")
        if "Cd" not in (profile.get("boot_targets") or []):
            raise DiscoveryError("preflight", "the BMC cannot boot from virtual media")
        serial = (profile.get("serial") or "").strip() or (profile.get("uuid") or "").strip()
        if not serial:
            raise DiscoveryError("preflight", "the BMC gives no system serial")
        _serial_name(serial)
        was_on = profile.get("power_state") == "On"
        step("preflight", "done",
             f"{profile.get('model') or '?'} (série {serial}), média virtuel OK")
        check_cancel("preflight")

        # --- ISO de découverte (en cache) ---
        base_url = f"http://{opts['advertise']}:{opts['port']}"
        iso, slot, _ = ensure_iso(opts["src_iso"], opts["cache_dir"], base_url,
                                  opts.get("extra_args", ""), opts["remaster_script"],
                                  remaster, step)
        check_cancel("remaster")

        # --- publication : l'ISO, et le dépôt de l'inventaire (armé) ---
        inv_path.unlink(missing_ok=True)
        try:
            tokens.append(pxe.issue(inv_path, "inventory",
                                    ttl=opts.get("inventory_timeout", INVENTORY_TIMEOUT) + 3600,
                                    token=slot))
        except pxe.TokenInUse:
            raise DiscoveryError("serve", "a discovery with this ISO is already running")
        iso_token = pxe.issue(iso, "iso")
        tokens.append(iso_token)
        iso_url = f"{base_url}/pxe/iso/{iso_token}.iso"
        step("serve", "done", f"ISO publiée sur {opts['advertise']}:{opts['port']}")

        # --- média virtuel, amorce unique, allumage ---
        step("bmc-insert", "running", "insertion dans le lecteur virtuel")
        ok, detail = bmc.insert(iso_url)
        if not ok:
            raise DiscoveryError("bmc-insert", detail[:200])
        inserted = True
        step("bmc-insert", "done", "image montée")

        step("bmc-boot", "running", "amorce unique sur le lecteur virtuel")
        ok, detail = bmc.boot_once_cd()
        if not ok:
            raise DiscoveryError("bmc-boot", detail[:200])
        step("bmc-boot", "done", "prochaine amorce : CD virtuel")

        reset = "ForceRestart" if was_on else "On"
        step("power", "running", "redémarrage sur l'ISO" if was_on else "allumage sur l'ISO")
        ok, detail = bmc.reset(reset)
        if not ok:
            raise DiscoveryError("power", detail[:200])
        powered = True
        step("power", "done", "machine démarrée sur l'ISO")

        # --- attente de l'inventaire ---
        timeout = opts.get("inventory_timeout", INVENTORY_TIMEOUT)
        step("wait-inventory", "running",
             f"attente de l'inventaire ({timeout // 60} min au plus)")
        deadline = clock() + timeout
        while not inv_path.is_file():
            check_cancel("wait-inventory")
            if clock() >= deadline:
                raise DiscoveryError("wait-inventory",
                                     f"no inventory after {timeout // 60} min")
            sleep(poll)
        raw = inv_path.read_bytes().decode("utf-8", "replace")
        step("wait-inventory", "done", f"inventaire reçu ({len(raw)} octets)")

        # --- extinction : le script éteint la machine lui-même ---
        timeout = opts.get("poweroff_timeout", POWEROFF_TIMEOUT)
        step("power-off", "running", "attente de l'extinction par le script")
        deadline = clock() + timeout
        forced = False
        while bmc.power_state() != "Off":
            if cancelled() or clock() >= deadline:
                ok, detail = bmc.reset("ForceOff")
                forced = True
                if not ok:
                    raise DiscoveryError("power-off", f"forced power off refused: {detail[:200]}")
                break
            sleep(poll)
        done = True
        step("power-off", "done",
             "extinction forcée : la machine ne s'était pas éteinte d'elle-même"
             if forced else "machine éteinte d'elle-même")

        # --- enregistrement ---
        path = store_inventory(opts["store_dir"], serial, opts["host"], raw, now=clock())
        step("store", "done", f"inventaire enregistré (série {serial})")
        return {"serial": serial, "path": path, "raw": raw, "forced_off": forced}
    finally:
        # Échec ou annulation machine démarrée sur l'ISO : elle ne sert plus
        # à rien, l'éteindre plutôt que la laisser tourner sur le système live.
        if powered and not done:
            try:
                if bmc.power_state() != "Off":
                    bmc.reset("ForceOff")
                    step("power-off", "done", "extinction forcée après l'échec")
            except Exception:
                pass
        if inserted:
            try:
                bmc.eject()
            except Exception:           # le ménage ne masque pas la vraie erreur
                pass
        if tokens:
            pxe.revoke(*tokens)
        inv_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Accès Redfish autonome (ligne de commande)
# ---------------------------------------------------------------------------

class RedfishBmc:
    """Interface attendue par `run`, en Redfish direct (urllib, TLS non
    vérifié : les BMC sont auto-signés). La console fournit la sienne,
    construite sur ses propres fonctions Redfish ; celle-ci sert la ligne de
    commande, sans la console. Le mot de passe ne sort jamais d'ici."""

    def __init__(self, host, user, password, timeout=15):
        self.host, self._user, self._pwd, self.timeout = host, user, password, timeout
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE
        self._system = None
        self._vm_path = None

    def _req(self, path, method="GET", payload=None):
        data = json.dumps(payload or {}).encode() if method != "GET" else None
        req = urllib.request.Request(f"https://{self.host}{path}", data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        cred = base64.b64encode(f"{self._user}:{self._pwd}".encode()).decode()
        req.add_header("Authorization", f"Basic {cred}")
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=self.timeout) as r:
                body = r.read()
                return True, (json.loads(body) if method == "GET" and body else
                              body[:400].decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}"
        except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError, ValueError) as e:
            return False, type(e).__name__

    def _get(self, path):
        ok, doc = self._req(path)
        return doc if ok and isinstance(doc, dict) else None

    def _first_member(self, path):
        members = (self._get(path) or {}).get("Members") or []
        return members[0]["@odata.id"] if members else None

    @staticmethod
    def _action(resource, *names):
        pools = [(resource.get("Actions") or {}, False)]
        for vendor in ("Hp", "Hpe", "Dell"):
            pools.append((((resource.get("Oem") or {}).get(vendor) or {}).get("Actions") or {}, True))
        for pool, is_oem in pools:
            for key, val in pool.items():
                if key.split(".")[-1] in names and (val or {}).get("target"):
                    return val["target"], is_oem
        return None, False

    def _virtual_cd(self):
        mgr = self._first_member("/redfish/v1/Managers/")
        if not mgr:
            return None, None
        coll = self._get(mgr.rstrip("/") + "/VirtualMedia/") or {}
        for m in coll.get("Members") or []:
            vm = self._get(m["@odata.id"])
            if vm and {"CD", "DVD"} & {t.upper() for t in vm.get("MediaTypes") or []}:
                return m["@odata.id"], vm
        return None, None

    def profile(self):
        self._system = self._first_member("/redfish/v1/Systems/")
        s = self._get(self._system) if self._system else None
        if not s:
            return {"ok": False, "error": "Redfish system unreachable"}
        boot = s.get("Boot") or {}
        self._vm_path, _ = self._virtual_cd()
        return {"ok": True, "system_path": self._system, "virtualmedia_path": self._vm_path,
                "boot_targets": (boot.get("BootSourceOverrideTarget@Redfish.AllowableValues")
                                 or boot.get("BootSourceOverrideSupported") or []),
                "serial": s.get("SerialNumber"), "uuid": s.get("UUID"),
                "model": s.get("Model"), "power_state": s.get("PowerState")}

    def insert(self, url):
        _, vm = self._virtual_cd()
        if (vm or {}).get("Inserted"):
            self.eject()
            time.sleep(3)
            _, vm = self._virtual_cd()
        target, is_oem = self._action(vm or {}, "InsertVirtualMedia", "InsertMedia")
        if not target:
            return False, "virtual media insert not exposed by this BMC"
        payload = {"Image": url} if is_oem else {"Image": url, "Inserted": True,
                                                  "WriteProtected": True}
        return self._req(target, "POST", payload)

    def eject(self):
        _, vm = self._virtual_cd()
        target, _ = self._action(vm or {}, "EjectVirtualMedia", "EjectMedia")
        if target:
            self._req(target, "POST", {})

    def boot_once_cd(self):
        return self._req(self._system, "PATCH", {"Boot": {
            "BootSourceOverrideTarget": "Cd", "BootSourceOverrideEnabled": "Once"}})

    def reset(self, reset_type):
        return self._req(f"{self._system.rstrip('/')}/Actions/ComputerSystem.Reset/",
                         "POST", {"ResetType": reset_type})

    def power_state(self):
        return (self._get(self._system) or {}).get("PowerState")
