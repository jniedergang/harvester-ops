"""harvester-ops : démarrage de découverte d'une machine nue (1.78.0).

Quand le BMC ne publie aucun disque (un iLO 4 n'en publie aucun), l'ISO
Harvester démarre une fois avec un script à elle, renvoie ce que Linux voit
(disques, liens stables, cartes réseau) et éteint la machine.

Ce module porte le déroulé, commun à la console (action suivie
`baremetal-discover:<hôte>`) et à la ligne de commande
(`harvester-baremetal.py discover`). Chacun fournit :

* `bmc` : l'accès au BMC (profil, média virtuel, amorce unique,
  alimentation), voir `RedfishBmc` pour l'interface attendue ; `eject()`
  renvoie (ok, détail) ;
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
# ISO de découverte, propre à chaque découverte
# ---------------------------------------------------------------------------
#
# Pas de cache : l'adresse de dépôt, avec son jeton à usage unique, est gravée
# dans le script de l'ISO. Une ISO par découverte, dans le répertoire de
# travail de la découverte, effacée à la fin : aucune autre découverte ne
# peut la remplacer pendant qu'elle est servie, ni réutiliser son jeton.

def build_iso(src, work_dir, base_url, extra, remaster_script, remaster, step):
    """Remasterise `src` pour une découverte. Renvoie (chemin de l'ISO,
    jeton de dépôt gravé). L'appelant efface l'ISO (`iso_leftovers`)."""
    src = Path(src)
    work_dir = Path(work_dir)
    token = secrets.token_urlsafe(24)
    tag = secrets.token_hex(6)
    iso = work_dir / f"discover-{tag}.iso"
    part = iso.with_name(iso.name + ".part")     # écriture en cours
    script = work_dir / f"discover-{tag}.sh"
    try:
        script.write_text(render_script(f"{base_url}/pxe/inventory/{token}"))
        script.chmod(0o600)
        step("remaster", "running", f"remasterisation de {src.name} pour la découverte")
        cmd = ["/usr/bin/env", "bash", str(remaster_script),
               "--src", str(src), "--out", str(part),
               "--kernel-args", kernel_args(extra),
               "--add-file", f"{script}:{SCRIPT_ISO_PATH}:0755",
               "--work-dir", str(work_dir)]
        if remaster(cmd, step) != 0:
            raise DiscoveryError("remaster", "ISO remastering failed")
        os.replace(part, iso)
    except BaseException:
        for p in iso_leftovers(iso):
            p.unlink(missing_ok=True)
        raise
    finally:
        script.unlink(missing_ok=True)
    step("remaster", "done", f"ISO de découverte prête ({iso.name})")
    return iso, token


def iso_leftovers(iso):
    """Tout ce qu'une remasterisation peut laisser pour cette ISO, y compris
    une écriture interrompue."""
    iso = Path(iso)
    part = iso.with_name(iso.name + ".part")
    return [iso, iso.with_name(iso.name + ".sha256"), part,
            part.with_name(part.name + ".sha256")]


# ---------------------------------------------------------------------------
# Liaison de l'inventaire à la machine (section `== dmi`)
# ---------------------------------------------------------------------------

# Valeurs de remplissage des firmwares : elles n'identifient rien.
_PLACEHOLDERS = {"", "not specified", "not available", "none", "n/a", "na", "0",
                 "default string", "to be filled by o.e.m.", "system serial number",
                 "0123456789", "chassis serial number", "unknown"}
_PLACEHOLDER_UUIDS = {"00000000-0000-0000-0000-000000000000",
                      "ffffffff-ffff-ffff-ffff-ffffffffffff",
                      "03000200-0400-0500-0006-000700080009"}


def read_dmi(raw):
    """`{"serial": ..., "uuid": ...}` de la section `== dmi` (None si vide)."""
    out = {"serial": None, "uuid": None}
    cur = None
    for line in str(raw or "").splitlines():
        if line.startswith("== "):
            cur = line[3:].strip()
            continue
        if cur == "dmi":
            key, _, value = line.partition(" ")
            if key in out:
                out[key] = value.strip() or None
    return out


def usable_serial(value):
    v = (value or "").strip()
    return v if v.lower() not in _PLACEHOLDERS else None


def usable_uuid(value):
    v = (value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", v):
        return None
    return v if v not in _PLACEHOLDER_UUIDS else None


def _uuid_swapped(u):
    """Les trois premiers champs en ordre d'octets inversé : certains BMC et
    le noyau ne lisent pas la structure SMBIOS dans le même ordre."""
    a, b, c, rest = u.split("-", 3)
    flip = lambda h: "".join(reversed([h[i:i + 2] for i in range(0, len(h), 2)]))  # noqa: E731
    return "-".join([flip(a), flip(b), flip(c), rest])


def check_binding(raw, bmc_serial, bmc_uuid):
    """Compare l'identité lue par Linux à celle du BMC piloté.

    Renvoie (verdict, message) : "ok", "unchecked" (rien de comparable) ou
    "mismatch" (l'inventaire vient d'une autre machine). Les numéros de
    série ne sont pas des secrets : ils figurent dans le message."""
    dmi = read_dmi(raw)
    ds, rs = usable_serial(dmi["serial"]), usable_serial(bmc_serial)
    du, ru = usable_uuid(dmi["uuid"]), usable_uuid(bmc_uuid)
    checked = []
    if ds and rs:
        if ds.lower() != rs.lower():
            return "mismatch", (f"l'inventaire vient d'une autre machine : série {ds} "
                                f"vue par Linux, {rs} donnée par le BMC")
        checked.append(f"série {rs}")
    if du and ru:
        if du not in (ru, _uuid_swapped(ru)):
            return "mismatch", (f"l'inventaire vient d'une autre machine : UUID {du} "
                                f"vu par Linux, {ru} donné par le BMC")
        checked.append(f"UUID {ru}")
    if checked:
        return "ok", "même machine (" + ", ".join(checked) + ")"
    return "unchecked", (f"liaison non vérifiée : Linux donne série {dmi['serial'] or 'vide'}, "
                         f"UUID {dmi['uuid'] or 'vide'} ; le BMC série {bmc_serial or 'vide'}, "
                         f"UUID {bmc_uuid or 'vide'}")


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

    host, src_iso, work_dir (0700, propre à la découverte ou partagé),
    store_dir, remaster_script, advertise, port, extra_args ; facultatifs
    inventory_timeout, poweroff_timeout, poll.

    Renvoie {"serial", "path", "raw", "forced_off", "binding"} ; lève
    DiscoveryError. Quoi qu'il arrive : média éjecté (ou avertissement),
    jetons révoqués, ISO et dépôt effacés."""
    poll = opts.get("poll", 5)
    tokens = []
    work_dir = Path(opts["work_dir"])
    inv_path = work_dir / f"inventory-{secrets.token_hex(6)}.txt"
    iso = None
    inserted = powered = done = False

    def check_cancel(sid):
        if cancelled():
            raise Cancelled(sid, "cancelled by operator")

    try:
        # --- préflight : le BMC, son lecteur virtuel, son identité ---
        step("preflight", "running", f"interrogation du BMC {opts['host']}")
        profile = bmc.profile()
        if not profile.get("ok"):
            raise DiscoveryError("preflight", profile.get("error") or "BMC unreachable")
        if not profile.get("virtualmedia_path"):
            raise DiscoveryError("preflight", "no CD virtual media (iLO Advanced licence?)")
        if "Cd" not in (profile.get("boot_targets") or []):
            raise DiscoveryError("preflight", "the BMC cannot boot from virtual media")
        bmc_serial = (profile.get("serial") or "").strip()
        bmc_uuid = (profile.get("uuid") or "").strip()
        serial = bmc_serial or bmc_uuid
        if not serial:
            raise DiscoveryError("preflight", "the BMC gives no system serial")
        _serial_name(serial)
        was_on = profile.get("power_state") == "On"
        step("preflight", "done",
             f"{profile.get('model') or '?'} (série {serial}), média virtuel OK")
        check_cancel("preflight")

        # --- ISO de cette découverte, avec son jeton de dépôt ---
        base_url = f"http://{opts['advertise']}:{opts['port']}"
        iso, upload_token = build_iso(opts["src_iso"], work_dir, base_url,
                                      opts.get("extra_args", ""), opts["remaster_script"],
                                      remaster, step)
        check_cancel("remaster")

        # --- publication : l'ISO, et le dépôt de l'inventaire ---
        tokens.append(pxe.issue(inv_path, "inventory",
                                ttl=opts.get("inventory_timeout", INVENTORY_TIMEOUT) + 3600,
                                token=upload_token))
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

        # --- l'inventaire vient-il bien de cette machine ? ---
        binding, message = check_binding(raw, bmc_serial, bmc_uuid)
        if binding == "mismatch":
            raise DiscoveryError("verify", message)
        step("verify", "done" if binding == "ok" else "warn", message)

        # --- enregistrement ---
        path = store_inventory(opts["store_dir"], serial, opts["host"], raw, now=clock())
        step("store", "done", f"inventaire enregistré (série {serial})")
        return {"serial": serial, "path": path, "raw": raw, "forced_off": forced,
                "binding": binding}
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
                res = bmc.eject()
                ok, detail = res if isinstance(res, tuple) else (True, "")
            except Exception as e:      # le ménage ne masque pas la vraie erreur
                ok, detail = False, type(e).__name__
            if not ok:
                step("bmc-eject", "warn",
                     f"média virtuel non éjecté ({str(detail)[:120]}) : l'éjecter depuis le BMC")
        if tokens:
            pxe.revoke(*tokens)
        inv_path.unlink(missing_ok=True)
        if iso is not None:
            for p in iso_leftovers(iso):
                p.unlink(missing_ok=True)


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
        if not target:
            return False, "virtual media eject not exposed by this BMC"
        return self._req(target, "POST", {})

    def boot_once_cd(self):
        return self._req(self._system, "PATCH", {"Boot": {
            "BootSourceOverrideTarget": "Cd", "BootSourceOverrideEnabled": "Once"}})

    def reset(self, reset_type):
        return self._req(f"{self._system.rstrip('/')}/Actions/ComputerSystem.Reset/",
                         "POST", {"ResetType": reset_type})

    def power_state(self):
        return (self._get(self._system) or {}).get("PowerState")
