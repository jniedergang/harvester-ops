#!/usr/bin/env python3
"""Faux OpenTofu pour les essais de la 2.9.0 (tâche 9, états chiffrés).

Rejoue ce que l'essai 8 du plan 2.9.0 a relevé sur OpenTofu 1.12.9 :
- le chiffrement est lu dans `_encryption.tf` (méthode principale de
  `state`, repli éventuel, `key_name`) ;
- chiffrer appelle `transit/datakey/plaintext/<clé>` d'OpenBao, lire
  appelle `transit/decrypt/<clé>`, avec BAO_ADDR / BAO_TOKEN de
  l'environnement ; un refus donne « Code: 403 » et un code 1 ;
- un état clair lu sous le bloc définitif (sans repli) est refusé
  (« encountered unencrypted payload without unencrypted method
  configured ») ;
- `state mv -state-out` écrit la destination en clair.

Chaque appel est noté dans `.fake-tofu.jsonl` de l'espace (argv, noms des
variables d'environnement, jeton, entrée standard).
"""

import base64
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

WS = Path.cwd()
STATE = WS / "terraform.tfstate"
LOG = WS / ".fake-tofu.jsonl"
ENC = WS / "_encryption.tf"


def note(argv, stdin):
    cfg = config()
    rec = {"argv": argv, "env": sorted(os.environ), "token": os.environ.get("BAO_TOKEN"),
           "cacert": os.environ.get("BAO_CACERT"), "stdin": stdin,
           "enc": [cfg[1], cfg[2]] if cfg else None}
    with open(LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")


def config():
    """(clé, méthode principale de l'état, repli) ou None."""
    if not ENC.exists():
        return None
    text = ENC.read_text()
    key = re.search(r'key_name\s*=\s*"([^"]+)"', text).group(1)
    st = re.search(r"state \{\n\s*method\s*=\s*method\.(\w+)\.\w+\n\s*enforced\s*=\s*(\w+)"
                   r"(?:\n\s*fallback \{\n\s*method\s*=\s*method\.(\w+)\.\w+)?", text)
    return key, st.group(1), st.group(3)


def bao(path, body):
    addr, tok = os.environ.get("BAO_ADDR"), os.environ.get("BAO_TOKEN")
    if not addr or not tok:
        die("key provider openbao: missing address or token")
    req = urllib.request.Request(f"{addr}/v1/{path}", data=json.dumps(body).encode(), method="PUT",
                                 headers={"X-Vault-Token": tok, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"{}").get("data") or {}
    except urllib.error.HTTPError as e:
        die(f"error while calling {path}\nCode: {e.code}. Errors:\n* permission denied")


def die(msg):
    sys.stderr.write(msg + "\n")
    sys.exit(1)


def encrypt(data, key):
    dk = bao(f"transit/datakey/plaintext/{key}", {"bits": 256})
    return {"encrypted_data": base64.b64encode(json.dumps(data).encode()).decode(),
            "encryption_version": "v0",
            "meta": {"key_provider.openbao.hops": base64.b64encode(json.dumps(
                {"ciphertext": dk.get("ciphertext"), "key": key}).encode()).decode()}}


def decrypt(doc, key):
    meta = json.loads(base64.b64decode(doc["meta"]["key_provider.openbao.hops"]))
    bao(f"transit/decrypt/{key}", {"ciphertext": meta["ciphertext"]})
    return json.loads(base64.b64decode(doc["encrypted_data"]))


def read_doc(path):
    if not path.exists() or path.stat().st_size == 0:
        return None
    doc = json.loads(path.read_text())
    cfg = config()
    if "encrypted_data" in doc:
        if not cfg or "aes_gcm" not in (cfg[1], cfg[2]):
            die("encountered encrypted payload without a method able to decrypt it")
        return decrypt(doc, cfg[0])
    if cfg and cfg[1] == "aes_gcm" and cfg[2] != "unencrypted":
        die("encountered unencrypted payload without unencrypted method configured")
    return doc


def write_doc(path, data):
    cfg = config()
    out = encrypt(data, cfg[0]) if cfg and cfg[1] == "aes_gcm" else data
    path.write_text(json.dumps(out))


def new_state(resources=()):
    return {"version": 4, "terraform_version": "1.12.9", "serial": 1, "lineage": "fake-lineage",
            "outputs": {}, "resources": list(resources), "check_results": None}


def res(addr):
    t, n = addr.split(".")
    return {"mode": "managed", "type": t, "name": n, "instances": [{"attributes": {"name": "MARKER-" + n}}]}


def main(argv):
    stdin = sys.stdin.read() if argv[:3] == ["state", "push", "-"] else None
    note(argv, stdin)
    cmd = argv[0] if argv else ""
    if (WS / f".fake-fail-{cmd}").exists():
        die(f"forced failure of {cmd}")
    if cmd == "init":
        cfg = config()
        if cfg and "aes_gcm" in (cfg[1], cfg[2]):
            bao(f"transit/datakey/plaintext/{cfg[0]}", {"bits": 256})   # init appelle déjà le fournisseur
        (WS / ".terraform").mkdir(exist_ok=True)
        print("init ok")
        return 0
    if cmd == "plan":
        st = read_doc(STATE)
        if any(a.startswith("-out=") for a in argv):
            write_doc(WS / "tfplan", {"plan": True, "prior": st})
        print("plan ok")
        return 0
    if cmd == "show":
        read_doc(WS / argv[-1])
        print(json.dumps({"resource_changes": []}))
        return 0
    if cmd == "apply":
        if "-refresh-only" not in argv and (WS / "tfplan").exists():
            read_doc(WS / "tfplan")
        st = read_doc(STATE) or new_state()
        if "-refresh-only" not in argv:
            if not any(r["name"] == "ops" for r in st["resources"]):
                st["resources"].append(res("harvester_ssh_key.ops"))
        st["serial"] = int(st.get("serial") or 0) + 1
        write_doc(STATE, st)
        print("apply ok")
        return 0
    if cmd == "destroy":
        st = read_doc(STATE) or new_state()
        st["resources"], st["serial"] = [], int(st.get("serial") or 0) + 1
        write_doc(STATE, st)
        return 0
    if argv[:2] == ["state", "list"]:
        st = read_doc(STATE) or new_state()
        for r in st["resources"]:
            print(f"{r['type']}.{r['name']}")
        return 0
    if argv[:2] == ["state", "pull"]:
        st = read_doc(STATE)
        print(json.dumps(st) if st else "", end="")
        return 0
    if argv[:3] == ["state", "push", "-"]:
        write_doc(STATE, json.loads(stdin))
        return 0
    if argv[:2] == ["state", "mv"]:
        out = next((a.split("=", 1)[1] for a in argv if a.startswith("-state-out=")), None)
        if out:
            Path(out).write_text(json.dumps(new_state([res(argv[-1])])))   # en clair (essai 8)
        return 0
    die(f"fake tofu: unsupported {argv}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
