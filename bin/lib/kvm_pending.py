"""harvester-ops : « redémarrage nécessaire » des VMs KVM, sans relire leur XML.

Savoir qu'une VM attend un redémarrage demande de comparer ses deux
définitions (vivante et inactive) : deux `dumpxml` par VM. La liste des VMs
(`/api/kvm/vms`) ne peut pas payer ce prix à chaque lecture. On garde donc un
repère par VM, écrit par la console quand elle sait (après chaque
modification ou geste à chaud, et à chaque lecture de la fenêtre
« Modifier », qui compare les deux XML) :

    <état>/kvm.d/pending.json : {"<hôte>/<vm>": {"id": <Id libvirt>, "fields": [...]}}

L'`Id` libvirt d'une VM change à chaque démarrage du processus QEMU : le
repère ne vaut que tant que la VM tourne avec le même `Id`. Une VM vue
arrêtée, redémarrée (autre `Id`) ou disparue perd son repère à la lecture
suivante de la liste. Une modification faite hors de la console (virsh)
n'est connue qu'à l'ouverture de sa fenêtre « Modifier ».
"""

import contextlib
import fcntl
import json
import os
from pathlib import Path

LIVE_STATES = ("running", "idle", "blocked", "paused", "in shutdown", "pmsuspended")
FILE = "pending.json"


def _path(state):
    return Path(state) / "kvm.d" / FILE


@contextlib.contextmanager
def _locked(state):
    d = Path(state) / "kvm.d"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(d / "pending.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def load(state):
    """Les repères ({clé: {id, fields}}) ; {} si le fichier manque ou est illisible."""
    try:
        data = json.loads(_path(state).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(v, dict) and isinstance(v.get("fields"), list)}


def _save(state, data):
    p = _path(state)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, sort_keys=True)
    os.replace(tmp, p)


def record(state, host, name, dom_id, fields):
    """Note (ou efface, `fields` vide ou VM sans Id) les changements en
    attente d'une VM qui tourne avec l'Id `dom_id`."""
    key = f"{host}/{name}"
    with _locked(state):
        data = load(state)
        if fields and isinstance(dom_id, int):
            entry = {"id": dom_id, "fields": sorted({str(f) for f in fields})}
            if data.get(key) == entry:
                return
            data[key] = entry
        elif key in data:
            del data[key]
        else:
            return
        _save(state, data)


def annotate(state, vms, read_hosts):
    """Pose `pending` (liste, vide si rien) sur chaque VM de la liste et
    retire les repères périmés : VM arrêtée, redémarrée (autre Id) ou absente
    d'un hôte lu (`read_hosts` : hôtes lus avec succès ; ceux en échec
    gardent leurs repères)."""
    data = load(state)
    seen, stale = set(), []
    for v in vms:
        key = f"{v.get('host')}/{v.get('name')}"
        seen.add(key)
        e = data.get(key)
        if e and v.get("state") in LIVE_STATES and v.get("id") == e.get("id"):
            v["pending"] = list(e["fields"])
        else:
            v["pending"] = []
            if e:
                stale.append(key)
    hosts = set(read_hosts)
    stale += [k for k in data if k not in seen and k.split("/", 1)[0] in hosts]
    if stale:
        with _locked(state):
            cur = load(state)
            for k in stale:
                # relu sous verrou : un repère réécrit entre-temps (autre Id) reste
                if k in cur and (k not in data or cur[k] == data[k]):
                    del cur[k]
            _save(state, cur)
    return vms
