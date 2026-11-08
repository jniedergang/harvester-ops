"""Aides des tests 2.9.0 (tâches 6 et 7) : un faux OpenBao de mode B1 (rôles
AppRole, `secret_id` emballés et accesseurs, politiques, `sys/rotate/root`),
un faux hôte pour l'agent des secrets, et un paquet OpenBao construit contre
le faux registre de test_oci_push_175."""

import base64
import functools
import json
import secrets
import subprocess
import tempfile
from pathlib import Path

import oci_push as op
import openbao_bundle as ob
import openbao_policy as pol
import test_oci_push_175 as t175
from fake_openbao import MSG_WRAP, FakeOpenBao, new_token

TOKEN_ACCESSOR = "auth_token_1a2b3c4d"


class FakeB1(FakeOpenBao):
    """Le faux serveur, avec ce que l'agent emploie en plus."""

    def __init__(self, cid="abc123def456", **kw):
        super().__init__(**kw)
        self.cid = cid
        self.role_ids = {}                   # nom de rôle -> role_id
        self.accessors = {}                  # accesseur -> (role_id, secret_id)
        self.policies = {}
        self.events = []                     # (quoi, détail) dans l'ordre
        self.initialized = True
        self.fail_paths = {}                 # chemin -> (code, message) une fois
        self.unseals = 0
        self.entities = {}                   # id -> {"name": "entity_<id>", "aliases": {(accesseur, nom)}}

    # -- amorce, comme `initialize` -------------------------------------------
    def initialize(self, boot_dir):
        boot = {f: Path(boot_dir, f).read_text() for f in pol.BOOT_FILES}
        for role, sid in ((pol.console_role(self.cid), boot["console-secret-id"]),
                          (pol.agent_role(self.cid), boot["agent-secret-id"])):
            self.role_ids.setdefault(role, "rid-" + secrets.token_hex(6))
            self.add_role(self.role_ids[role], sid)
            self.accessors["acc-" + secrets.token_hex(4)] = (self.role_ids[role], sid)
        self.tokens[boot["boot-token"]] = {"ttl": 900, "policies": [pol.boot_policy_name(self.cid)],
                                           "created": __import__("time").time()}
        self.events.append(("initialized", None))

    def role_of(self, name):
        return self.role_ids.get(name)

    def _route(self, h, rec, path, body, raw):
        m = rec["method"]
        if path in self.fail_paths:
            code, msg = self.fail_paths.pop(path)
            return h._err(code, msg)
        if path == "sys/seal-status":
            return h._send(200, {"type": "static", "initialized": self.initialized, "sealed": self.sealed,
                                 "version": self.seal_version})
        tok = self._auth(rec)
        parts = path.split("/")
        if path.startswith("auth/approle/role/") and tok:
            role = parts[3]
            rid = self.role_ids.get(role)
            rest = "/".join(parts[4:])
            if rid is None:
                return h._err(400, "role not found")
            if rest == "role-id" and m == "GET":
                return h._send(200, {"data": {"role_id": rid}})
            if rest == "secret-id/lookup":
                acc = next((a for a, (r, s) in self.accessors.items() if r == rid and s == body.get("secret_id")),
                           None)
                return h._send(200, {"data": {"secret_id_accessor": acc}}) if acc else h._err(404)
            if rest == "custom-secret-id":
                sid = body.get("secret_id")
                # comme OpenBao 2.7.2 (revue I3 des tâches 5-7, essai P4) : un
                # secret_id déjà enregistré est refusé en 500
                if sid in (self.roles.get(rid) or {}).get("secret_ids", ()):
                    return h._err(500, "SecretID is already registered")
                self.add_role(rid, sid)
                self.accessors.setdefault("acc-" + secrets.token_hex(4), (rid, sid))
                self.events.append(("custom-secret-id", role))
                return h._send(200, {"data": {"secret_id": sid}})
            if rest == "secret-id" and m in ("POST", "PUT"):
                sid, acc = "sid-" + secrets.token_hex(8), "acc-" + secrets.token_hex(4)
                self.add_role(rid, sid)
                self.accessors[acc] = (rid, sid)
                data = {"secret_id": sid, "secret_id_accessor": acc}
                if rec["headers"].get("x-vault-wrap-ttl"):
                    w = self.wrap(data, f"auth/approle/role/{role}/secret-id")
                    return h._send(200, {"wrap_info": {"token": w, "ttl": 300,
                                                       "creation_path": f"auth/approle/role/{role}/secret-id"}})
                return h._send(200, {"data": data})
            if rest == "secret-id-accessor/destroy":
                acc = body.get("secret_id_accessor")
                r_s = self.accessors.pop(acc, None)
                if r_s:
                    self.roles[r_s[0]]["secret_ids"].discard(r_s[1])
                self.events.append(("destroy-accessor", acc))
                return h._send(204)
        if path == "sys/capabilities-self" and tok:
            # comme la politique de la console : patch en plus sur metadata
            out = {p: (["deny"] if any(p.startswith(d) for d in self.denied)
                       else ["create", "read", "update", "patch", "delete", "list"]) for p in body.get("paths", [])}
            return h._send(200, dict(out, data=dict(out)))
        if path == "sys/auth" and tok:
            return h._send(200, {"data": {"token/": {"accessor": TOKEN_ACCESSOR, "type": "token"},
                                          "approle/": {"accessor": "auth_approle_x", "type": "approle"}}})
        if path.startswith("sys/policies/acl/") and tok:
            name = path.split("/", 3)[3]
            if m == "DELETE":
                self.policies.pop(name, None)
            else:
                self.policies[name] = body.get("policy")
            self.events.append(("policy-" + m.lower(), name))
            return h._send(204)
        # entités : comme OpenBao 2.7.2 (essai P4), une entité créée par
        # `entity_alias` s'appelle `entity_<id>` ; recherche par alias
        if path.startswith("auth/token/create/") and tok and body.get("entity_alias"):
            key = (TOKEN_ACCESSOR, body["entity_alias"])
            if not any(key in e["aliases"] for e in self.entities.values()):
                eid = secrets.token_hex(8)
                self.entities[eid] = {"name": f"entity_{eid}", "aliases": {key}}
        if path == "identity/lookup/entity" and tok:
            key = (body.get("alias_mount_accessor"), body.get("alias_name"))
            eid = next((i for i, e in self.entities.items() if key in e["aliases"]), None)
            return h._send(200, {"data": {"id": eid, "name": self.entities[eid]["name"]}}) if eid else h._send(204)
        if path.startswith("identity/entity/name/") and tok:
            name = path.split("/", 3)[3]
            eid = next((i for i, e in self.entities.items() if e["name"] == name), None)
            if eid is None:
                return h._err(404)
            if m == "DELETE":
                del self.entities[eid]
                return h._send(204)
            return h._send(200, {"data": {"id": eid, "name": name}})
        if path.startswith("identity/entity/id/") and tok:
            eid = path.split("/", 3)[3]
            if m == "DELETE":
                self.entities.pop(eid, None)
                self.events.append(("entity-delete", eid))
                return h._send(204)
            return h._send(200, {"data": {"id": eid}}) if eid in self.entities else h._err(404)
        if path == "sys/rotate/root" and tok:
            self.events.append(("rotate-root", None))
            return h._send(204)
        if path == "sys/wrapping/lookup":
            w = self.wraps.get(body.get("token"))
            if not w:
                return h._err(400, MSG_WRAP)
        return super()._route(h, rec, path, body, raw)


class FakeHost:
    """L'hôte de l'agent : commandes notées, openssl réel, systemctl et
    podman simulés. `on_start` est appelé au démarrage de l'unité de B1."""

    def __init__(self, paths, digest=None):
        self.p = paths
        self.cmds = []
        self.digest = digest
        self.uids = {}
        self.active = "inactive"
        self.on_start = None
        self.on_restart = None
        self.secrets = {}                    # nom -> octets (secret podman)
        self.fail = {}                       # premier mot-clé de commande -> code de sortie
        self.chowns = []

    def run(self, argv, input=None, check=True, timeout=600):
        self.cmds.append(list(argv))
        key = " ".join(argv[:3])
        for k, rc in list(self.fail.items()):
            if key.startswith(k):
                del self.fail[k]
                if check:
                    import importlib
                    raise importlib.import_module("hsa").AgentError("host-command-failed", f"{k} failed")
                return subprocess.CompletedProcess(argv, rc, "", "failed")
        out = ""
        if argv[0] == "openssl":
            return subprocess.run(argv, input=input, capture_output=True, text=True, check=True)
        if argv[:2] == ["podman", "load"]:
            out = "Loaded image: dp.example/containers/openbao:9.9.9-1\n"
        elif argv[:3] == ["podman", "image", "inspect"]:
            out = (self.digest or "") + "\n"
        elif argv[:3] == ["podman", "secret", "create"]:
            self.secrets[argv[3]] = Path(argv[4]).read_bytes()
        elif argv[:3] == ["podman", "secret", "rm"]:
            self.secrets.pop(argv[3], None)
        elif argv[:1] == ["useradd"]:
            self.uids[argv[-1]] = 990
        elif argv[:2] == ["systemctl", "is-active"]:
            out = self.active + "\n"
        elif argv[:2] == ["systemctl", "enable"] or argv[:2] == ["systemctl", "start"] and argv[-1].startswith(
                "harvester-ops-openbao"):
            self.active = "active"
            if self.on_start:
                self.on_start()
        elif argv[:2] == ["systemctl", "restart"]:
            if self.on_restart:
                self.on_restart()
        elif argv[:2] == ["systemctl", "disable"] or argv[:2] == ["systemctl", "stop"] and argv[-1].startswith(
                "harvester-ops-openbao"):
            self.active = "inactive"
        return subprocess.CompletedProcess(argv, 0, out, "")

    def chown(self, path, uid, gid):
        self.chowns.append((str(path), uid, gid))

    def uid_of(self, user):
        return self.uids.get(user)

    def service_owner(self):
        return None

    def group_gid(self, name):
        return None

    def ran(self, *prefix):
        return [c for c in self.cmds if c[:len(prefix)] == list(prefix)]


@functools.lru_cache(maxsize=None)
def cosign_test_key():
    """Paire RSA de test (une par processus) : (clé privée, clé publique),
    qui tient le rôle de la clé de l'Application Collection."""
    d = Path(tempfile.mkdtemp(prefix="hops-test-cosign-"))
    priv, pub = d / "cosign.key", d / "cosign.pub"
    subprocess.run(["openssl", "genrsa", "-out", str(priv), "2048"], check=True, capture_output=True)
    subprocess.run(["openssl", "rsa", "-in", str(priv), "-pubout", "-out", str(pub)], check=True, capture_output=True)
    return priv, pub


def cosign_sign(payload, key=None):
    """Signature cosign d'une charge utile (base64), comme `cosign sign`
    avec une clé RSA : PKCS#1 v1.5 sur SHA-256."""
    key = key or cosign_test_key()[0]
    r = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key)], input=payload, check=True,
                       capture_output=True)
    return base64.b64encode(r.stdout).decode()


def cosign_payload(digest, reference):
    return json.dumps({"critical": {"identity": {"docker-reference": reference},
                                    "image": {"docker-manifest-digest": digest},
                                    "type": "cosign container image signature"},
                       "optional": {"creator": "test"}}, separators=(",", ":")).encode()


def add_signature(reg, repo, digest, reference=None, payload=None, signature=None):
    """Étiquette sha256-<hex>.sig du faux registre, telle que cosign la pose."""
    payload = payload if payload is not None else cosign_payload(digest, reference or f"{reg.host}/{repo}")
    reg.blobs[t175.sha(payload)] = payload
    cfg = b"{}"
    reg.blobs[t175.sha(cfg)] = cfg
    man = json.dumps({"schemaVersion": 2, "mediaType": op.M_OCI,
                      "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": t175.sha(cfg),
                                 "size": len(cfg)},
                      "layers": [{"mediaType": ob.SIMPLESIGNING, "digest": t175.sha(payload), "size": len(payload),
                                  "annotations": {ob.SIG_ANNOTATION: signature or cosign_sign(payload)}}]}).encode()
    reg.manifests[(repo, f"sha256-{digest[7:]}.sig")] = (man, op.M_OCI)


def make_registry(tag="9.9.9-1", version="9.9.9", repo="containers/openbao", signed=True):
    """Faux registre qui porte une image OpenBao (index amd64 + arm64),
    signée par la clé de test (`signed`)."""
    reg = t175.FakeRegistry(user="svc", password="Pw-AppCo-777")
    digs = {}
    for arch in ("amd64", "arm64"):
        layer = t175.payload(3000, arch.encode())
        reg.blobs[t175.sha(layer)] = layer
        cfg = json.dumps({"architecture": arch, "os": "linux", "config": {"Labels": {ob.VERSION_LABEL: version}},
                          "rootfs": {"type": "layers", "diff_ids": [t175.sha(layer)]}}).encode()
        reg.blobs[t175.sha(cfg)] = cfg
        man = json.dumps({"schemaVersion": 2, "mediaType": op.M_OCI,
                          "config": {"mediaType": op.CONFIG_TYPE[op.M_OCI], "digest": t175.sha(cfg), "size": len(cfg)},
                          "layers": [{"mediaType": op.LAYER_TYPE[op.M_OCI], "digest": t175.sha(layer),
                                      "size": len(layer)}]}).encode()
        reg.manifests[(repo, t175.sha(man))] = (man, op.M_OCI)
        digs[arch] = (t175.sha(man), len(man))
    idx = json.dumps({"schemaVersion": 2, "mediaType": op.M_OCI_INDEX, "manifests": [
        {"mediaType": op.M_OCI, "digest": d, "size": n, "platform": {"os": "linux", "architecture": a}}
        for a, (d, n) in digs.items()]}).encode()
    reg.manifests[(repo, tag)] = (idx, op.M_OCI_INDEX)
    reg.manifests[(repo, t175.sha(idx))] = (idx, op.M_OCI_INDEX)
    reg.index_digest = t175.sha(idx)
    reg.amd64 = digs["amd64"][0]
    if signed:
        add_signature(reg, repo, reg.index_digest)
    return reg


def make_bundle(out_dir, tag="9.9.9-1", version="9.9.9"):
    """Paquet valide (rendu par openbao_bundle.build) ; (chemin, infos)."""
    root = Path(__file__).resolve().parents[2]
    reg = make_registry(tag, version)
    try:
        src = op.Registry(reg.host, {"username": "svc", "password": "Pw-AppCo-777"}, plain_http=True)
        tpl = (root / "config" / "openbao" / "openbao.hcl.tpl").read_text()
        got = ob.build(src, reg.host, "containers/openbao", tag, tpl, out_dir, cosign_key=cosign_test_key()[1])
    finally:
        reg.close()
    return Path(got["path"]), got


def new_wrap_token():
    return new_token()
