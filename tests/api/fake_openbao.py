"""Faux serveur OpenBao pour les tests 2.9.0 (bibliothèque standard).

Reproduit les réponses relevées sur le banc réel (tâche 1 du plan 2.9.0) :
codes HTTP et messages `errors` exacts. État en mémoire : montages KV v2
(et v1), versions, `custom_metadata`, AppRole, jetons, enveloppes, capacités,
transit, instantanés raft. Chaque requête est notée dans `log` (méthode,
chemin, en-têtes, corps).

    srv = FakeOpenBao(); srv.start(); ...; srv.stop()

Pannes injectables : `fail_next` (liste de (code, corps)), `sealed`,
`audit_full` (500 « internal error » sur toute requête journalisée),
`hang` (le serveur ne répond pas), `denied` (préfixes refusés).
"""

import base64
import json
import secrets
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MSG_CAS = "check-and-set parameter did not match the current version"
MSG_WRAP = "wrapping token is not valid or does not exist"
MSG_SEALED = "Vault is sealed"
MSG_INTERNAL = "internal error"
MSG_DENIED = "permission denied"
MSG_ALIAS = "invalid 'entity_alias' value"
MSG_SNAP_KEY = ("could not verify hash file, possibly the snapshot is using a different autoseal key; "
                "use the snapshot-force API to bypass this check")
MSG_BAD_SECRET_ID = "invalid secret id"
# Sans jeton ou sans audit : sys/health, seal-status, wrapping/lookup.
_UNAUTH = ("sys/health", "sys/seal-status", "sys/wrapping/lookup", "auth/approle/login", "sys/wrapping/unwrap")


def _ttl_s(v, default):
    """Durée d'OpenBao (`90`, `90s`, `5m`, `1h`) en secondes."""
    v = str(v or "").strip()
    try:
        if v.endswith(("s", "m", "h")):
            return int(v[:-1]) * {"s": 1, "m": 60, "h": 3600}[v[-1]]
        return int(v) if v else default
    except ValueError:
        return default


def new_token(prefix="s."):
    return prefix + secrets.token_urlsafe(18)[:24]


class FakeOpenBao:
    def __init__(self, tls=None):
        self.tls = tls                       # (cert_file, key_file) ou None
        self.lock = threading.RLock()
        self.log = []
        self.fail_next = []
        self.sealed = False
        self.audit_full = False
        self.hang = 0.0
        self.denied = []                     # préfixes de chemin refusés (403)
        self.mounts = {"harvester-ops": 2, "transit": "transit"}
        self.kv = {}                         # (montage, chemin) -> {"versions": {n: data|None}, "current", "custom", "max"}
        self.roles = {}                      # role_id -> {"secret_ids": set, "ttl", "period"}
        self.tokens = {}                     # jeton -> {"ttl", "policies", "created"}
        self.wraps = {}                      # jeton d'enveloppe -> {"creation_path", "data", "expires"}
        self.transit = {}                    # clé -> version
        self.snapshot = b"SNAPSHOT-" + secrets.token_bytes(32)
        self.snapshot_key_ok = True
        self.restored = []
        self.token_ttl = 3600
        self.renew_fails = False
        self.lose_reply = 0                  # écritures KV dont la réponse arrive après le délai du client
        self.lose_sleep = 0.0
        self.child_ttl = None                # durée imposée aux jetons enfants (essais d'expiration)
        self.datakeys = []                   # clés transit dont une clé de données a été tirée
        self.seal_version = "2.0.0-HEAD"     # `version` de sys/seal-status (Vault : 1.x)
        self.httpd = None

    # -- vie du serveur ----------------------------------------------------
    def start(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.httpd.daemon_threads = True
        if self.tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*self.tls)
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        return self

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    @property
    def addr(self):
        scheme = "https" if self.tls else "http"
        return f"{scheme}://127.0.0.1:{self.httpd.server_address[1]}"

    # -- aides des tests -----------------------------------------------------
    def add_role(self, role_id="role-1", secret_id=None, ttl=None):
        sid = secret_id or ("sid-" + secrets.token_hex(8))
        self.roles.setdefault(role_id, {"secret_ids": set(), "ttl": ttl or self.token_ttl})
        self.roles[role_id]["secret_ids"].add(sid)
        return role_id, sid

    def add_token(self, ttl=None, policies=("default",)):
        t = new_token()
        self.tokens[t] = {"ttl": ttl or self.token_ttl, "policies": list(policies), "created": time.time()}
        return t

    def wrap(self, data, creation_path, ttl=300):
        t = new_token()
        self.wraps[t] = {"creation_path": creation_path, "data": data, "expires": time.time() + ttl}
        return t

    def put_raw(self, mount, path, data):
        e = self.kv.setdefault((mount, path), {"versions": {}, "current": 0, "custom": None, "max": 10})
        e["current"] += 1
        e["versions"][e["current"]] = dict(data)
        return e["current"]

    def requests(self, path_part=""):
        return [r for r in self.log if path_part in r["path"]]

    # -- gestionnaire HTTP ---------------------------------------------------
    def _handler(self):
        srv = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, body=None, raw=None):
                data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
                self.send_response(code)
                if raw is None and body is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if data:
                    self.wfile.write(data)

            def _err(self, code, *msgs):
                self._send(code, {"errors": list(msgs)})

            def handle_one(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                u = urlparse(self.path)
                path = u.path[len("/v1/"):] if u.path.startswith("/v1/") else u.path.lstrip("/")
                rec = {"method": self.command, "path": path, "query": parse_qs(u.query),
                       "headers": {k.lower(): v for k, v in self.headers.items()}, "raw": raw}
                with srv.lock:
                    srv.log.append(rec)
                if srv.hang:
                    time.sleep(srv.hang)
                with srv.lock:
                    if srv.fail_next:
                        code, body = srv.fail_next.pop(0)
                        return self._send(code, body)
                try:
                    body = json.loads(raw) if raw and self.headers.get("Content-Type") == "application/json" else {}
                except ValueError:
                    body = {}
                with srv.lock:
                    return srv._route(self, rec, path, body, raw)

            do_GET = do_POST = do_PUT = do_DELETE = do_LIST = do_PATCH = handle_one

        return H

    def _auth(self, rec):
        tok = rec["headers"].get("x-vault-token")
        info = self.tokens.get(tok)
        if not info:
            return None
        if time.time() - info["created"] > info["ttl"]:
            return None
        return tok

    def _route(self, h, rec, path, body, raw):
        m = rec["method"]
        if path == "sys/health":
            if self.sealed:
                return h._send(503, {"initialized": True, "sealed": True, "version": "2.0.0-HEAD"})
            return h._send(200, {"initialized": True, "sealed": False, "version": "2.0.0-HEAD"})
        if path == "sys/seal-status":
            return h._send(200, {"type": "static", "initialized": True, "sealed": self.sealed,
                                 "version": self.seal_version})
        if self.sealed:
            return h._err(503, MSG_SEALED)
        if self.audit_full:
            return h._err(500, MSG_INTERNAL)
        if path == "auth/approle/login":
            role = self.roles.get(body.get("role_id"))
            if not role:
                return h._err(400, "invalid role or secret ID")
            if body.get("secret_id") not in role["secret_ids"]:
                return h._err(400, MSG_BAD_SECRET_ID)
            t = new_token()
            self.tokens[t] = {"ttl": role["ttl"], "policies": ["harvester-ops"], "created": time.time()}
            return h._send(200, {"auth": {"client_token": t, "lease_duration": role["ttl"], "renewable": True,
                                          "accessor": "acc-" + secrets.token_hex(4)}})
        if path == "sys/wrapping/lookup":
            w = self.wraps.get(body.get("token"))
            if not w or w["expires"] < time.time():
                return h._err(400, MSG_WRAP)
            return h._send(200, {"data": {"creation_path": w["creation_path"], "creation_ttl": 300}})
        if path == "sys/wrapping/unwrap":
            w = self.wraps.pop(rec["headers"].get("x-vault-token"), None)
            if not w or w["expires"] < time.time():
                return h._err(400, MSG_WRAP)
            return h._send(200, {"data": w["data"]})
        tok = self._auth(rec)
        if not tok:
            return h._err(403, MSG_DENIED)
        if path == "sys/capabilities-self":
            out = {}
            for p in body.get("paths", []):
                out[p] = ["deny"] if any(p.startswith(d) for d in self.denied) else ["create", "read", "update", "delete", "list"]
            res = dict(out)
            if len(out) == 1:
                res["capabilities"] = next(iter(out.values()))
            res["data"] = dict(res)
            return h._send(200, res)
        if any(path.startswith(d) for d in self.denied):
            return h._err(403, MSG_DENIED)
        if path == "auth/token/renew-self":
            if self.renew_fails:
                return h._err(403, MSG_DENIED)
            info = self.tokens[tok]
            info["created"] = time.time()
            return h._send(200, {"auth": {"client_token": tok, "lease_duration": info["ttl"], "renewable": True}})
        if path == "auth/token/lookup-self":
            info = self.tokens[tok]
            return h._send(200, {"data": {"ttl": info["ttl"], "policies": info["policies"], "period": info["ttl"]}})
        if path == "auth/token/revoke-self":
            self.tokens.pop(tok, None)
            return h._send(204)
        if path.startswith("auth/token/create/"):
            alias = body.get("entity_alias")
            if alias is not None and not str(alias).startswith("tf-"):
                return h._err(400, MSG_ALIAS)
            t = new_token()
            ttl = self.child_ttl if self.child_ttl is not None else _ttl_s(body.get("ttl"), 60)
            self.tokens[t] = {"ttl": ttl, "policies": body.get("policies", []), "created": time.time(),
                              "alias": alias, "role": path[len("auth/token/create/"):]}
            return h._send(200, {"auth": {"client_token": t, "lease_duration": ttl, "renewable": False}})
        if path.startswith("sys/internal/ui/mounts/"):
            mount = path.split("/", 4)[4].strip("/")
            v = self.mounts.get(mount)
            if v is None:
                return h._err(403, "preflight capability check returned 403, please ensure client's policies grant access to the path")
            if v == "transit":
                return h._send(200, {"data": {"type": "transit", "options": None, "path": mount + "/"}})
            return h._send(200, {"data": {"type": "kv", "options": {"version": str(v)}, "path": mount + "/"}})
        if path == "sys/storage/raft/snapshot" and m == "GET":
            return h._send(200, raw=self.snapshot)
        if path in ("sys/storage/raft/snapshot", "sys/storage/raft/snapshot-force") and m in ("POST", "PUT"):
            if path.endswith("snapshot") and not self.snapshot_key_ok:
                return h._err(400, MSG_SNAP_KEY)
            self.restored.append(raw)
            return h._send(204)
        top, _, rest = path.partition("/")
        if self.mounts.get(top) == "transit":
            alias = self.tokens.get(tok, {}).get("alias")
            if alias is not None:
                # politique fixe à gabarit des jetons d'OpenTofu (essai 8) :
                # datakey/plaintext et decrypt de la seule clé de son alias
                op, _, key = rest.partition("/")
                if op not in ("datakey", "decrypt") or not key.split("/")[-1].endswith("-" + alias):
                    return h._err(403, MSG_DENIED)
            return self._transit(h, rest, body)
        if top in self.mounts:
            return self._kv(h, m, top, rest, body, rec)
        return h._err(404)

    def _transit(self, h, rest, body):
        op, _, key = rest.partition("/")
        if op == "keys":
            if key.endswith("/rotate"):
                k = key[: -len("/rotate")]
                if k not in self.transit:
                    return h._err(400, "key not found")
                self.transit[k] += 1
                return h._send(204)
            self.transit.setdefault(key, 1)
            return h._send(204)
        if op == "datakey" and key.startswith("plaintext/"):
            k = key[len("plaintext/"):]
            if k not in self.transit:
                return h._err(400, "encryption key not found")
            pt = base64.b64encode(secrets.token_bytes(int(body.get("bits", 256)) // 8)).decode()
            self.datakeys.append(k)
            return h._send(200, {"data": {"plaintext": pt, "ciphertext": "vault:v1:" + pt[::-1]}})
        if key not in self.transit:
            return h._err(400, "encryption key not found")
        if op == "encrypt":
            return h._send(200, {"data": {"ciphertext": "vault:v1:" + body.get("plaintext", "")[::-1]}})
        if op == "decrypt":
            ct = body.get("ciphertext", "")
            return h._send(200, {"data": {"plaintext": ct[len("vault:v1:"):][::-1]}})
        return h._err(404)

    def _kv(self, h, m, mount, rest, body, rec):
        version = self.mounts[mount]
        if version == 1:
            return h._err(404)        # data/<x> n'existe pas sur un montage v1
        kind, _, path = rest.partition("/")
        if kind == "config":
            return h._send(200, {"data": {"max_versions": 10, "cas_required": False, "delete_version_after": "0s"}})
        if kind == "data":
            e = self.kv.get((mount, path))
            if m == "GET":
                if not e or not e["current"]:
                    return h._err(404)
                v = int(rec["query"].get("version", [e["current"]])[0])
                if v not in e["versions"]:
                    return h._err(404)
                if v in e.get("deleted", ()):
                    # suppression douce ou destruction : 404 avec les métadonnées
                    return h._send(404, {"data": {"data": None, "metadata": {
                        "version": v, "created_time": "2026-11-08T10:00:00Z",
                        "deletion_time": "2026-11-08T11:00:00Z", "destroyed": False}}})
                return h._send(200, {"data": {"data": e["versions"][v], "metadata": {
                    "version": v, "created_time": "2026-11-08T10:00:00Z", "custom_metadata": e["custom"],
                    "deletion_time": "", "destroyed": False}}})
            if m in ("POST", "PUT"):
                cas = (body.get("options") or {}).get("cas")
                cur = e["current"] if e else 0
                if cas is not None and int(cas) != cur:
                    return h._err(400, MSG_CAS)
                e = self.kv.setdefault((mount, path), {"versions": {}, "current": 0, "custom": None, "max": 10})
                e["current"] += 1
                e["versions"][e["current"]] = body.get("data") or {}
                for old in sorted(e["versions"])[: max(0, len(e["versions"]) - e["max"])]:
                    del e["versions"][old]
                if srv_lose := self.lose_reply:
                    self.lose_reply = srv_lose - 1
                    time.sleep(self.lose_sleep)       # écriture appliquée, réponse perdue (délai)
                return h._send(200, {"data": {"version": e["current"], "created_time": "2026-11-08T10:00:00Z"},
                                     "warnings": None})
            if m == "DELETE":
                if e and e["current"]:
                    e.setdefault("deleted", set()).add(e["current"])
                return h._send(204)
        if kind == "destroy" and m in ("POST", "PUT"):
            e = self.kv.get((mount, path))
            for v in body.get("versions") or []:
                if e and int(v) in e["versions"]:
                    e.setdefault("deleted", set()).add(int(v))
            return h._send(204)
        if kind == "metadata":
            if m == "LIST" or rec["query"].get("list") == ["true"]:
                prefix = path.rstrip("/") + "/" if path.strip("/") else ""
                keys = set()
                for (mt, p) in self.kv:
                    if mt == mount and p.startswith(prefix):
                        nxt = p[len(prefix):]
                        keys.add(nxt.split("/")[0] + "/" if "/" in nxt else nxt)
                if not keys:
                    return h._err(404)
                return h._send(200, {"data": {"keys": sorted(keys)}})
            e = self.kv.get((mount, path))
            if m == "GET":
                if not e:
                    return h._err(404)
                return h._send(200, {"data": {
                    "current_version": e["current"], "oldest_version": min(e["versions"] or [0]),
                    "max_versions": e["max"], "custom_metadata": e["custom"],
                    "versions": {str(v): {"created_time": "2026-11-08T10:00:00Z", "deletion_time": "",
                                          "destroyed": False} for v in e["versions"]}}})
            if m in ("POST", "PUT", "PATCH"):
                if not e:
                    e = self.kv.setdefault((mount, path), {"versions": {}, "current": 0, "custom": None, "max": 10})
                if "custom_metadata" in body:
                    e["custom"] = body["custom_metadata"]
                if "max_versions" in body:
                    e["max"] = int(body["max_versions"])
                return h._send(204)
            if m == "DELETE":
                self.kv.pop((mount, path), None)        # feuille seulement : aucun récursif
                return h._send(204)
        return h._err(404)


def b64(s):
    return base64.b64encode(s.encode()).decode()
