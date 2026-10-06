#!/usr/bin/env python3
"""Technologies et versions de harvester-ops, lues dans le dépôt (v1.86.0).

Rien n'est écrit à la main : chaque version vient du fichier qui la fixe
pour la release (lockfile Python, Containerfile, paquet Cluster API, README
des bibliothèques embarquées). La page technique du site dit donc toujours
la vérité de la version construite.

    python3 tools/demo-site/techinfo.py        # affiche le JSON
"""
import json
import re
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Rôle de chaque composant, en anglais (le site le traduit pour les plus
# visibles ; les autres restent des noms techniques).
ROLES = {
    "flask": "web framework of the console",
    "werkzeug": "WSGI toolkit under Flask",
    "jinja2": "page templates",
    "flask-limiter": "rate limits on every mutating endpoint",
    "flask-sock": "WebSockets for the VNC and serial consoles",
    "prometheus-client": "metrics endpoint",
    "pyyaml": "configuration and Kubernetes manifests",
    "passlib": "password hashing of console accounts",
    "bcrypt": "password hashing backend",
    "markdown": "release notes and documentation rendering",
    "y-py": "shared editing of notes",
    "itsdangerous": "signed session cookies",
}
# noVNC n'embarque pas son numéro de version dans ses fichiers : celui de la
# copie embarquée est noté ici (CHANGELOG, v1.38). Tiptap, sans numéro
# lisible dans son paquet, reste « embedded ».
PINNED = {"novnc": "1.7.0"}
FRONT = {
    "lucide": ("Lucide icons", "the monochrome icon set"),
    "xterm": ("xterm.js", "serial console of VMs"),
    "novnc": ("noVNC", "graphical console of VMs, shared between people"),
    "tiptap": ("Tiptap", "rich-text notes"),
    "yjs": ("Yjs", "real-time shared notes"),
}


def _python_libs():
    out = []
    for line in (ROOT / "web" / "requirements-lock.txt").read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)==([^\s\\;]+)", line)
        if m and m.group(1).lower() in ROLES:
            out.append({"name": m.group(1), "version": m.group(2), "role": ROLES[m.group(1).lower()]})
    return out


def _containerfile():
    t = (ROOT / "container" / "Containerfile").read_text()
    base = re.search(r"^FROM\s+(\S+)", t, re.M).group(1)
    arg = lambda n: (re.search(rf"^ARG\s+{n}=(\S+)", t, re.M) or [None, ""])[1]
    py = base.rsplit(":", 1)[-1]
    runtime = [
        {"name": "Python", "version": py, "role": "language of the console and its tools"},
        {"name": "SUSE BCI Python image", "version": base, "role": "base of the container image"},
        {"name": "SQLite", "version": "WAL", "role": "actions history and notes, no database server"},
    ]
    tools = [
        {"name": "kubectl", "version": arg("KUBECTL_VERSION"), "role": "Kubernetes client, fallback of the direct API reads"},
        {"name": "yq", "version": arg("YQ_VERSION"), "role": "YAML processing in the scripts"},
        {"name": "OpenTofu", "version": arg("TOFU_VERSION"), "role": "applies Terraform declarations (MPL-2.0)"},
        {"name": "xorriso", "version": "openSUSE package", "role": "Harvester ISO remastering for bare metal"},
        {"name": "OpenSSH client", "version": "openSUSE package", "role": "node access for shutdown and startup"},
    ]
    return runtime, tools


def _frontend():
    out = []
    vend = ROOT / "web" / "static" / "vendor"
    for d, (name, role) in FRONT.items():
        ver = ""
        for f in sorted((vend / d).rglob("*")):
            if not f.is_file() or f.stat().st_size > 3_000_000:
                continue
            try:
                t = f.read_text(errors="ignore")
            except OSError:
                continue
            m = (re.search(r"^- Version:\s*([0-9][\w.-]*)", t, re.M)
                 or re.search(rf"{re.escape(name.split('.')[0])}\S*\s+([0-9]+\.[0-9]+\.[0-9]+)", t)
                 or re.search(rf"{d}@([0-9]+\.[0-9]+\.[0-9]+)", t))
            if m:
                ver = m.group(1)
                break
        out.append({"name": name, "version": ver or PINNED.get(d, "embedded"), "role": role})
    return out


def _capi():
    dist = ROOT / "dist"
    bundles = sorted(dist.glob("capi-bundle-*.tar.gz")) if dist.exists() else []
    if not bundles:
        return [], []
    with tarfile.open(bundles[-1]) as tf:
        man = json.load(tf.extractfile("capi-bundle/manifest.json"))
    comps = [{"name": c["name"], "version": c["version"], "role": "Cluster API component"}
             for c in man.get("components", [])]
    return comps, man.get("bundle", {}).get("compatible_harvester_versions", [])


def collect():
    runtime, tools = _containerfile()
    env = (ROOT / "scripts" / "embedded-providers.env").read_text()
    tfp = re.search(r"^TF_PROVIDER_VERSION=(\S+)", env, re.M).group(1)
    tools.append({"name": "terraform-provider-harvester", "version": tfp, "role": "Harvester resources in Terraform declarations"})
    capi, harv = _capi()
    harvester = [{"name": "Harvester HCI", "version": ", ".join(harv) or "v1.7 to v1.9",
                  "role": "clusters operated (tested for real on v1.8 and v1.9)"},
                 {"name": "Forklift", "version": "v1.8.2 images", "role": "warm VMware migrations"},
                 {"name": "Rancher", "version": "2.14", "role": "single sign-on and inherited rights (OIDC provider)"}]
    return {
        "version": (ROOT / "VERSION").read_text().strip(),
        "groups": [
            ["runtime", runtime], ["python", _python_libs()], ["frontend", _frontend()],
            ["tools", tools], ["capi", capi], ["harvester", harvester],
        ],
    }


if __name__ == "__main__":
    print(json.dumps(collect(), indent=1))
