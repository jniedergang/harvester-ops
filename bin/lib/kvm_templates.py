"""Modèles de création de VM KVM gardés par la console.

Un modèle est un spec du formulaire (pas du XML), rangé dans
`<état>/kvm-templates/<nom>.json` (fichier 0600, répertoire 0700),
utilisable sur tous les hôtes. Jamais de secret : le user-data du
cloud-init (il peut porter des mots de passe) et toute valeur texte d'une
clé nommée comme un mot de passe sont retirés à l'enregistrement, et la
liste de ce qui a été retiré est rendue pour que l'interface le dise.

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
SECRET_KEYS = ("password", "passwd", "vnc_password", "secret", "token")


class TemplateError(ValueError):
    pass


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
                if isinstance(k, str) and k.lower() in SECRET_KEYS and isinstance(v, str) and v:
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


def save(state, name, spec):
    """Enregistre (remplace) un modèle ; rend les champs retirés."""
    path = path_of(state, name)
    clean, removed = scrub(spec)
    data = json.dumps({"name": name, "spec": clean, "removed": removed},
                      indent=2, sort_keys=True) + "\n"
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
    return {"name": name, "spec": doc["spec"], "removed": list(doc.get("removed") or [])}


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
                        "removed": doc["removed"]})
        except (OSError, ValueError):
            out.append({"name": name, "error": "unreadable template"})
    return out


def delete(state, name):
    """Supprime un modèle ; FileNotFoundError s'il n'existe pas."""
    os.unlink(path_of(state, name))
