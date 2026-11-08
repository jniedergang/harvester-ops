"""harvester-ops : lecture et déplacement d'états Terraform, résumé d'un plan
(v1.54.0).

Chaque déclaration a désormais son propre état (son espace de travail). Les
ressources appliquées avant la v1.54 vivent dans l'état partagé du cluster :
au premier plan de leur déclaration, elles y sont reprises, sans rien
recréer sur le cluster. Le déplacement se fait sur le fichier d'état local
(format 4, celui de Terraform comme d'OpenTofu), avec une copie de chaque
fichier avant écriture.

Le résumé d'un plan (`show -json`) dit, ressource par ressource, ce qui sera
créé, modifié, remplacé ou détruit, et pour une modification quels réglages
changent, valeurs sensibles masquées.

2.9.0 (tâche 9) : un état chiffré par OpenTofu (OpenBao transit) ne se lit
plus en JSON. `addresses` le refuse (`StateEncrypted`) sans `lister`, qui
rend la sortie de `tofu state list` ; `move` prend la lecture et
l'écriture de chaque espace (`tofu state pull` / `tofu state push -` pour
un espace chiffré) et n'écrit jamais d'état en clair par-dessus un état
chiffré.

Ce module ne lance pas Terraform : il est essayé seul.
"""

import json
import os
import time
import uuid
from pathlib import Path

STATE_FILE = "terraform.tfstate"
SENSITIVE = "(sensitive)"


class StateEncrypted(Exception):
    """L'état de cet espace est chiffré : il se lit par OpenTofu."""

    def __init__(self, ws):
        super().__init__(f"the state of {Path(ws).name} is encrypted")
        self.ws = Path(ws)


def is_encrypted(state):
    return isinstance(state, dict) and "encrypted_data" in state and "encryption_version" in state


def _load(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def state_encrypted(ws):
    """Vrai si l'état de l'espace `ws` est chiffré (contenu jamais lu au-delà
    de ses clés de premier niveau)."""
    return is_encrypted(_load(Path(ws) / STATE_FILE))


def _write(path, data):
    path = Path(path)
    if is_encrypted(_load(path)):
        # jamais un état clair par-dessus un état chiffré (essai 8 :
        # `state mv -state-out` le faisait)
        raise StateEncrypted(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def read_file_state(ws):
    """Lecture JSON de l'état d'un espace en clair ; StateEncrypted sinon."""
    st = _load(Path(ws) / STATE_FILE)
    if is_encrypted(st):
        raise StateEncrypted(ws)
    return st


def write_file_state(ws, data):
    Path(ws).mkdir(parents=True, exist_ok=True)
    _write(Path(ws) / STATE_FILE, data)


def address_of(res):
    return f"{res.get('type')}.{res.get('name')}"


def addresses(ws, lister=None):
    """Les ressources gérées de l'état d'un espace de travail (module racine).
    État chiffré : `lister(ws)` rend la sortie de `tofu state list` ; sans
    lui, StateEncrypted (jamais une liste vide qui ferait croire l'espace
    vide)."""
    st = _load(Path(ws) / STATE_FILE) or {}
    if is_encrypted(st):
        if lister is None:
            raise StateEncrypted(ws)
        import tf_encryption
        return tf_encryption.parse_state_list(lister(ws))
    return sorted(address_of(r) for r in st.get("resources") or []
                  if r.get("mode") == "managed" and not r.get("module"))


def move(src_ws, dst_ws, wanted, read=read_file_state, write=write_file_state):
    """Déplace de l'état de `src_ws` vers celui de `dst_ws` les ressources
    dont l'adresse est dans `wanted`. Rend les adresses déplacées. Une
    adresse déjà présente dans la destination n'est pas touchée.

    `read(ws)` rend l'état d'un espace (dict, ou None s'il n'y en a pas) et
    `write(ws, état)` l'écrit : fichier JSON par défaut (refus d'un état
    chiffré), `tofu state pull` / `tofu state push -` pour un espace
    chiffré (l'appelant choisit par espace). Les copies `.pre-move-*` sont
    celles des fichiers tels quels (un état chiffré reste chiffré)."""
    src_p, dst_p = Path(src_ws) / STATE_FILE, Path(dst_ws) / STATE_FILE
    src = read(src_ws)
    if not src or not src.get("resources"):
        return []
    dst = read(dst_ws) or {
        "version": src.get("version", 4), "terraform_version": src.get("terraform_version"),
        "serial": 0, "lineage": str(uuid.uuid4()), "outputs": {}, "resources": [],
        "check_results": None,
    }
    have = {address_of(r) for r in dst.get("resources") or []}
    moving, staying = [], []
    for r in src["resources"]:
        a = address_of(r)
        if (r.get("mode") == "managed" and not r.get("module")
                and a in set(wanted) and a not in have):
            moving.append(r)
        else:
            staying.append(r)
    if not moving:
        return []
    stamp = time.strftime("%Y%m%d%H%M%S")
    for p in (src_p, dst_p):
        if p.exists():
            (p.parent / f"{p.name}.pre-move-{stamp}").write_bytes(p.read_bytes())
    dst["resources"] = list(dst.get("resources") or []) + moving
    dst["serial"] = int(dst.get("serial") or 0) + 1
    src["resources"] = staying
    src["serial"] = int(src.get("serial") or 0) + 1
    Path(dst_ws).mkdir(parents=True, exist_ok=True)
    write(dst_ws, dst)
    write(src_ws, src)
    return [address_of(r) for r in moving]


# ---------------------------------------------------------------------------
# Résumé d'un plan
# ---------------------------------------------------------------------------
def _action(actions):
    a = list(actions or [])
    if a in (["no-op"], ["read"]):
        return "noop"
    if a == ["create"]:
        return "create"
    if a == ["update"]:
        return "update"
    if a == ["delete"]:
        return "delete"
    if sorted(a) == ["create", "delete"]:
        return "replace"
    return "+".join(a) or "noop"


def _masked(value, sens):
    if sens is True:
        return SENSITIVE
    if isinstance(value, dict) and isinstance(sens, dict):
        return {k: _masked(v, sens.get(k)) for k, v in value.items()}
    if isinstance(value, list) and isinstance(sens, list):
        return [_masked(v, sens[i] if i < len(sens) else None) for i, v in enumerate(value)]
    return value


def _diff(before, after, path, out, limit):
    if len(out) >= limit or before == after:
        return
    if isinstance(before, dict) and isinstance(after, dict):
        for k in sorted(set(before) | set(after)):
            _diff(before.get(k), after.get(k), f"{path}.{k}" if path else k, out, limit)
        return
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        for i, (b, a) in enumerate(zip(before, after)):
            _diff(b, a, f"{path}[{i}]", out, limit)
        return
    out.append({"path": path, "before": before, "after": after})


def _scalars(obj, limit=12):
    """Les réglages simples et renseignés d'une ressource à créer."""
    out = []
    for k, v in sorted((obj or {}).items()):
        if isinstance(v, (str, int, float, bool)) and v not in ("", None) and len(out) < limit:
            out.append({"path": k, "before": None, "after": v})
    return out


def plan_summary(show, limit=40):
    """`show` : la sortie JSON de `terraform show -json <plan>`."""
    counts = {"create": 0, "update": 0, "replace": 0, "delete": 0, "noop": 0}
    changes = []
    for rc in (show or {}).get("resource_changes") or []:
        if rc.get("mode") != "managed":
            continue
        ch = rc.get("change") or {}
        act = _action(ch.get("actions"))
        counts[act] = counts.get(act, 0) + 1
        if act == "noop":
            continue
        before = _masked(ch.get("before"), ch.get("before_sensitive"))
        after = _masked(ch.get("after"), ch.get("after_sensitive"))
        fields = []
        if act in ("update", "replace"):
            _diff(before, after, "", fields, limit)
        elif act == "create":
            fields = _scalars(after)
        changes.append({"address": rc.get("address"), "type": rc.get("type"),
                        "name": rc.get("name"), "action": act, "fields": fields,
                        "replace_paths": ["/".join(str(x) for x in p)
                                          for p in rc.get("change", {}).get("replace_paths") or []][:10]})
    return {"counts": counts, "changes": changes}
