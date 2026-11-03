"""Une machine porte-t-elle déjà un système ? (2.2.0, vérifier avant d'agir)

Lecture pure de ce que le BMC publie déjà, sans rien changer sur la
machine. Deux dialectes Redfish, tous deux vus en réel :

* iLO 4 (ProLiant Gen9) : `Systems/1/BIOS/Boot/` (OEM HPE), `BootSources`
  (entrées UEFI avec leur chemin de périphérique) et
  `PersistentBootConfigOrder` ; l'iLO 4 ne publie ni disque ni volume
  (`Storage` vide), ces entrées sont donc le seul signe disponible. Elles
  reflètent le DERNIER démarrage de la machine ;
* Redfish standard (iDRAC 9, iLO 5) : `Systems/<id>/BootOptions`
  (`DisplayName`, `UefiDevicePath`, `BootOptionReference`) et
  `Boot.BootOrder`.

Un signe : une entrée UEFI qui lance un chargeur `\\EFI\\...` depuis une
partition de disque (`HD(n,GPT|MBR,...)`), hors clé USB, CD et réseau ; la
même en tête de l'ordre de démarrage ; un volume logique (RAID) existant.
Rien ne permet de lire les partitions elles-mêmes par Redfish : sans liste
d'entrées lisible, l'état est « inconnu », jamais « vide ». Une entrée peut
survivre à l'effacement d'un disque (NVRAM) : le signe est alors à tort
« présent », ce qui ne fait que demander une confirmation de plus.
"""

import re

# Chargeur UEFI sur une partition de disque : HD(<n>,GPT|MBR,...) puis un
# fichier sous \EFI\. Le chemin peut commencer par le contrôleur (PciRoot...)
# ou directement par HD(...) (entrée « Unknown » de l'iLO 4).
_LOADER_RE = re.compile(r"HD\(\d+,(?:GPT|MBR),[^)]*\)/(\\EFI\\[^/]+?\.efi)\s*$", re.I)
# Supports amovibles ou réseau : jamais un système de la machine
_REMOVABLE_RE = re.compile(r"USB\(|UsbClass\(|CDROM\(|Uri\(|MAC\(|iSCSI\(", re.I)

PRESENT, ABSENT, UNKNOWN = "present", "absent", "unknown"


def loader_of(path):
    """Chargeur `\\EFI\\...efi` lancé depuis une partition de disque, ou None."""
    p = str(path or "").strip()
    if not p or _REMOVABLE_RE.search(p):
        return None
    m = _LOADER_RE.search(p)
    return m.group(1) if m else None


def evaluate(entries, order=None, volumes=None):
    """État et signes, à partir de ce que le BMC a publié.

    entries : liste de {"id", "name", "path"} (None = liste illisible) ;
    order   : identifiants dans l'ordre de démarrage ;
    volumes : volumes logiques [{"name", "controller"}] (None = non lus).
    Rend {"state", "evidence": [{"code", ...}]} ; les codes sont traduits
    par la page (bmc.js), jamais du texte libre."""
    evidence = []
    systems = {}
    for e in entries or []:
        loader = loader_of(e.get("path"))
        if loader:
            systems[e.get("id")] = e
            evidence.append({"code": "boot-entry", "name": e.get("name") or e.get("id") or "",
                             "loader": loader, "disk": e.get("id") or ""})
    first = next((x for x in (order or []) if x), None)
    if first is not None and first in systems:
        evidence.insert(0, {"code": "boots-from-disk", "name": systems[first].get("name") or first})
    for v in volumes or []:
        evidence.append({"code": "volume", "name": v.get("name") or "", "controller": v.get("controller") or ""})
    if evidence:
        return {"state": PRESENT, "evidence": evidence}
    if entries is None:
        return {"state": UNKNOWN, "evidence": []}
    return {"state": ABSENT, "evidence": []}


def confirm_name(serial, host):
    """Nom à taper pour installer quand un système semble présent : le
    numéro de série, sinon l'adresse du BMC (ce que montre la fenêtre)."""
    s = str(serial or "").strip()
    return s or str(host or "").strip()


def confirmed(check, typed):
    """Vrai quand l'installation peut partir sur cette machine : aucun
    système présent (« absent » ou « inconnu »), ou le nom tapé est exactement
    `confirm_name` (espaces autour tolérés). Partagé par la fenêtre
    d'installation et les séries de profils (2.2.1)."""
    if (check or {}).get("state") != PRESENT:
        return True
    return str(typed or "").strip() == check.get("confirm_name")


def entries_from_hpe(boot):
    """Entrées de `Systems/1/BIOS/Boot/` (iLO 4), ou None si illisible."""
    if not isinstance(boot, dict) or not isinstance(boot.get("BootSources"), list):
        return None, []
    entries = [{"id": b.get("StructuredBootString") or "", "name": (b.get("BootString") or "").strip(),
                "path": b.get("UEFIDevicePath") or ""}
               for b in boot["BootSources"] if isinstance(b, dict)]
    order = [x for x in (boot.get("PersistentBootConfigOrder") or []) if isinstance(x, str)]
    return entries, order


def entries_from_standard(options, boot_order):
    """Entrées de `BootOptions` (Redfish standard) : `options` = documents
    des membres ; `boot_order` = `Boot.BootOrder`."""
    if options is None:
        return None, []
    entries = [{"id": o.get("BootOptionReference") or o.get("Id") or "",
                "name": (o.get("DisplayName") or o.get("Name") or "").strip(),
                "path": o.get("UefiDevicePath") or ""}
               for o in options if isinstance(o, dict)]
    return entries, [x for x in (boot_order or []) if isinstance(x, str)]
