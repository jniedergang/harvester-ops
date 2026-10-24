"""Modèles de création de VM KVM gardés par la console.

Un modèle est un spec du formulaire (pas du XML), rangé dans
`<état>/kvm-templates/<nom>.json` (fichier 0600, répertoire 0700),
utilisable sur tous les hôtes. Jamais de secret : le user-data du
cloud-init (il peut porter des mots de passe) et toute valeur non vide
d'une clé dont le nom contient password, passwd, secret, token, key,
credential ou private (casse ignorée, à toute profondeur) sont retirés à
l'enregistrement, et la liste de ce qui a été retiré est rendue pour que
l'interface le dise.

Propriétaire : un modèle note qui l'a enregistré (`owner`). Le remplacer ou
le supprimer est refusé à une autre personne, sauf `replace_other` (la
console ne le donne qu'à un administrateur). Le modèle appartient ensuite à
qui l'a enregistré. Un modèle sans propriétaire (enregistré avant cette
règle, ou par la CLI sans --owner) reste modifiable par chacun.

Écrit par bin/harvester-kvm.py (`template save|delete`, action suivie de la
console), lu directement par la console.
"""

import json
import os
import re
from pathlib import Path

import kvm_decl

TEMPLATES_DIR = "kvm-templates"
TEMPLATE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TEMPLATE_MAX = 1 << 20              # taille d'un modèle enregistré
TEMPLATES_MAX = 500
# Nom de clé qui désigne un secret (casse ignorée, n'importe où dans le nom)
SECRET_KEY_RE = re.compile(r"password|passwd|secret|token|key|credential|private", re.I)
OWNER_MAX = 128


class TemplateError(ValueError):
    pass


class TemplateOwnerError(TemplateError):
    """Le modèle appartient à une autre personne."""


def check_owner(owner):
    if owner is None:
        return None
    if not isinstance(owner, str) or not owner or len(owner) > OWNER_MAX or any(
            ord(c) < 32 or ord(c) == 127 for c in owner):
        raise TemplateError(f"owner: a name without control characters, {OWNER_MAX} "
                            "characters at most")
    return owner


def check_name(name):
    if not isinstance(name, str) or not TEMPLATE_RE.match(name):
        raise TemplateError("template name: letters, digits, '.', '_' and '-', 64 characters "
                            "at most, starting with a letter or a digit")
    return name


def templates_dir(state):
    return Path(state) / TEMPLATES_DIR


def path_of(state, name):
    return templates_dir(state) / f"{check_name(name)}.json"


def scrub(spec):
    """(spec sans secret, liste des champs retirés en notation pointée)."""
    removed = []

    def walk(value, where):
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                here = f"{where}.{k}" if where else str(k)
                if here == "cloudinit.user_data" and v:
                    removed.append(here)
                    continue
                if isinstance(k, str) and SECRET_KEY_RE.search(k) and v not in (None, "", [], {}):
                    removed.append(here)
                    continue
                out[k] = walk(v, here)
            return out
        if isinstance(value, list):
            return [walk(v, f"{where}[{i}]") for i, v in enumerate(value)]
        return value
    if not isinstance(spec, dict):
        raise TemplateError("a template is a spec object")
    clean = walk(spec, "")
    return clean, removed


def _check_owner_of(state, name, owner, replace_other):
    """Refuse de toucher au modèle d'une autre personne (TemplateOwnerError)."""
    try:
        current = load(state, name).get("owner")
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        current = None               # illisible : on peut le remplacer
    if current and current != owner and not replace_other:
        raise TemplateOwnerError(f"template {name} belongs to {current}: only they or an "
                                 "administrator can replace or delete it")


def save(state, name, spec, owner=None, replace_other=False):
    """Enregistre (remplace) un modèle ; rend les champs retirés.
    TemplateOwnerError si le modèle existant appartient à une autre personne."""
    path = path_of(state, name)
    check_owner(owner)
    _check_owner_of(state, name, owner, replace_other)
    clean, removed = scrub(spec)
    doc = {"name": name, "spec": clean, "removed": removed}
    if owner:
        doc["owner"] = owner
    data = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    if len(data) > TEMPLATE_MAX:
        raise TemplateError(f"the template is larger than {TEMPLATE_MAX // 1024} KiB")
    if not path.exists() and len(list_names(state)) >= TEMPLATES_MAX:
        raise TemplateError(f"at most {TEMPLATES_MAX} templates")
    kvm_decl.write_private(path, data)
    return removed


def load(state, name):
    """{name, spec, removed} ; FileNotFoundError s'il n'existe pas."""
    path = path_of(state, name)
    with open(path, encoding="utf-8") as f:
        doc = json.loads(f.read(TEMPLATE_MAX + 1))
    if not isinstance(doc, dict) or not isinstance(doc.get("spec"), dict):
        raise TemplateError(f"template {name} is unreadable")
    owner = doc.get("owner") if isinstance(doc.get("owner"), str) else None
    return {"name": name, "spec": doc["spec"], "removed": list(doc.get("removed") or []),
            "owner": owner}


def list_names(state):
    d = templates_dir(state)
    try:
        return sorted(p.stem for p in d.glob("*.json")
                      if p.is_file() and TEMPLATE_RE.match(p.stem))
    except OSError:
        return []


def list_all(state):
    """[{name, updated, removed}] triés par nom ; un fichier illisible est
    rendu avec `error` (sans chemin)."""
    out = []
    for name in list_names(state):
        path = path_of(state, name)
        try:
            doc = load(state, name)
            out.append({"name": name, "updated": int(os.stat(path).st_mtime),
                        "removed": doc["removed"], "owner": doc["owner"]})
        except (OSError, ValueError):
            out.append({"name": name, "error": "unreadable template"})
    return out


def delete(state, name, owner=None, replace_other=False):
    """Supprime un modèle ; FileNotFoundError s'il n'existe pas,
    TemplateOwnerError s'il appartient à une autre personne."""
    path = path_of(state, name)
    check_owner(owner)
    _check_owner_of(state, name, owner, replace_other)
    os.unlink(path)
