#!/usr/bin/env python3
"""Tire les données de la démo d'un enregistrement de la console (v1.86.0).

    python3 tools/demo-site/sanitize.py recording.json site/demo/data.json

L'enregistrement (record.py) porte les noms, adresses et objets réels des
clusters de test. Ce script les remplace par un monde fictif cohérent (les
mêmes correspondances partout : chemins et contenus), neutralise ce qui
ressemble à un secret (cloud-init, mots de passe, clés SSH), puis REFUSE
d'écrire si un motif interdit subsiste : la sortie part sur un site public.
"""
import hashlib
import json
import re
import sys

# Ordre important : le plus long d'abord (harvlab2 avant harvlab).
NAMES = [
    ("harvlab2-n1", "nantes-n1"), ("harvlab-n1", "lyon-n1"), ("harvlab-n2", "lyon-n2"), ("harvlab-n3", "lyon-n3"),
    ("harv4-node1", "lille-n1"), ("harv1.home.lo", "paris-n1"), ("harv1-node1", "paris-n1"),
    ("vmwlab-src-1", "erp-app-01"), ("vmwlab-src-2", "crm-db-01"), ("vmwlab-src-3", "win-ad-01"),
    ("vmwlab-vc", "vcenter-dc1"), ("vmwlab-dc", "dc1"), ("vmwlab-esx1", "esx-01"), ("vmwlab", "vcenter-dc1"),
    ("harvlab2", "nantes-dr"), ("harvlab", "lyon-lab"), ("harv1", "paris-prod"), ("harv4", "lille-edge"),
    ("bmcfg", "bm-staging"),
    ("aiscaleimage-test", "image-builder"), ("aiscaleimage", "imgbuild"), ("aiscale", "imgbuild"), ("hops-upd", "ops-client"), ("onit-repro", "support-repro"),
    ("mgmt-v030", "mgmt-01"), ("station1", "workstation-01"), ("mlm", "suse-manager"),
    ("demo-a", "wave-erp"), ("demo-b", "wave-crm"), ("vague-en3", "wave-pilot-2"), ("vague-fr2", "wave-pilot-3"),
    ("vague-b", "wave-pilot-1"), ("mig-lanes", "migrated"),
]
DOMAINS = [(r"([a-z0-9-]+\.)*home\.zypp\.fr", "rancher.example.com"), (r"([a-z0-9-]+\.)*zypp\.fr", "example.com"),
           (r"\bhome\.lo\b", "example.internal")]
# Chemins et identités du poste qui a enregistré.
PATHS = [("/home/ju/workspace/HARVESTER-OPS", "/opt/harvester-ops"),
         ("/home/ju/.local/share/harvester-ops", "/var/lib/harvester-ops"),
         ("/home/ju/.config/harvester-ops", "/etc/harvester-ops"), ("/home/ju", "/home/operator")]
CLUSTER_TEXT = {
    "paris-prod": "Production, Paris datacenter",
    "lyon-lab": "Three-node lab: maintenance, live migration, devices",
    "nantes-dr": "Recovery site, target of the VMware migrations",
    "lille-edge": "Edge site, installed by the console (bare metal)",
    "bm-staging": "Two-node batch installed by the console (bare metal), powered off",
}
SECRET_LINE = re.compile(r"(?i)(password|passwd|chpasswd|ssh_pwauth|hashed_passwd|plain_text)")
FORBIDDEN = [r"172\.16\.", r"home\.lo", r"zypp\.fr", r"harvlab", r"\bharv[14]\b", r"vmwlab", r"julien",
             r"niedergang(?!/harvester-ops)", r"hotmail", r"USE6236", r"CNFCP", r"BEGIN [A-Z ]*KEY",
             r"(?i)password\s*[:=]\s*\S", r"\bbmcfg\b", r"/home/ju", r"\bju@", r"xl170", r"aiscale",
             r"(?i)homelab", r"\bnode[1-5]\b", r"tests/bench", r"(?i)imbriqu", r"\"ju\""]


def fake_b64(seed, n):
    h = b""
    i = 0
    while len(h) * 4 // 3 < n:
        h += hashlib.sha256(f"{seed}{i}".encode()).digest()
        i += 1
    import base64
    return base64.b64encode(h).decode()[:n]


def ip(m):
    a, b = int(m.group(1)), int(m.group(2))
    return f"10.20.{a}.{b}"


def mac(m):
    s = m.group(0)
    if s in ("00:00:00:00:00:00",) or s.lower().startswith("00:50:56"):
        return s             # VMware : préfixe générique, sans identité
    h = hashlib.sha256(s.lower().encode()).hexdigest()
    return "52:54:00:" + ":".join(h[i:i + 2] for i in (0, 2, 4))


def text(s):
    for a, b in NAMES:
        s = re.sub(rf"(?<![A-Za-z0-9]){re.escape(a)}(?![A-Za-z0-9])", b, s)
    for p, r in DOMAINS:
        s = re.sub(p, r, s)
    for a, b in PATHS:
        s = s.replace(a, b)
    s = re.sub(r"\bju@[A-Za-z0-9.-]+", "operator@admin-host", s)
    s = re.sub(r"\bnode[1-5]-xl170r\b", "admin-host", s)
    s = re.sub(r"\bnode([1-5])\b", r"host\1", s)
    s = re.sub(r"172\.16\.(\d+)\.(\d+)", ip, s)
    s = re.sub(r"172\.16\.0\.0", "10.20.0.0", s)
    s = re.sub(r"(?i)\b[0-9a-f]{2}(:[0-9a-f]{2}){5}\b", mac, s)
    s = re.sub(r"(ssh-(?:rsa|ed25519) )([A-Za-z0-9+/=]{20,})",
               lambda m: m.group(1) + fake_b64(m.group(2), len(m.group(2))), s)
    if "\n" in s and SECRET_LINE.search(s):
        s = "\n".join(line for line in s.split("\n") if not SECRET_LINE.search(line))
    return s


def walk(o, key=""):
    if isinstance(o, dict):
        return {text(k): walk(v, k) for k, v in o.items()}
    if isinstance(o, list):
        return [walk(v, key) for v in o]
    if isinstance(o, str):
        if key.lower() in ("userdata", "user_data", "networkdata", "network_data"):
            return "#cloud-config\npackage_update: true\n"
        if o == "ju":
            return "operator"
        return text(o)
    return o


def main(src, dst):
    rec = json.load(open(src))
    out = {text(k): walk(v) for k, v in rec.items()}
    cl = out.get("/api/clusters", {}).get("body", {}).get("clusters") or []
    for c in cl:
        c["description"] = CLUSTER_TEXT.get(c["name"], "")
    found = {m.group(1) for k in out for m in [re.match(r"^/api/vms/([^/?]+)$", k)] if m}
    # le plus parlant d'abord : la démo s'ouvre sur le premier
    order = ["lyon-lab", "paris-prod", "nantes-dr", "lille-edge", "bm-staging"]
    clusters = [c for c in order if c in found] + sorted(found - set(order))
    import os
    data = {"meta": {"clusters": clusters, "recorded_at": int(os.path.getmtime(src)),
                     "source": "harvester-ops test clusters, anonymized"}, "responses": out}
    raw = json.dumps(data, ensure_ascii=False)
    left = [(p, sorted({m.group(0) for m in re.finditer(p, raw)})[:5]) for p in FORBIDDEN if re.search(p, raw)]
    if left:
        print("motifs interdits restants, rien n'est écrit :", left, file=sys.stderr)
        sys.exit(1)
    with open(dst, "w") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(out)} réponses, clusters {clusters} -> {dst}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
