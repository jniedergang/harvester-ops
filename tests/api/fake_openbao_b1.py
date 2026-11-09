"""Aides des tests 2.9.0 (tâches 6 et 7) : un faux OpenBao de mode B1 (rôles
AppRole, `secret_id` emballés et accesseurs, politiques, `sys/rotate/root`),
un faux hôte pour l'agent des secrets, et un paquet OpenBao construit contre
le faux registre de test_oci_push_175."""

import base64
import functools
import json
import re
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
_NOT_MINE = object()


ADMIN_PREFIXES = ("sys/policies/", "sys/rotate/", "sys/auth/", "sys/mounts/", "sys/audit/", "sys/raw/",
                  "identity/", "auth/token/accessors")


def admin_path(p):
    """Chemins d'administration que la politique de la console ne donne pas."""
    return (p.startswith(ADMIN_PREFIXES) or p in ("auth/token/create", "auth/token/create-orphan")
            or re.fullmatch(r"auth/approle/role/[^/]+", p) is not None
            or re.fullmatch(r"auth/approle/role/hops-agent-[^/]+/secret-id", p) is not None)


def _acl_rules(text):
    return [(p, [c.strip().strip('"') for c in caps.split(",") if c.strip()])
            for p, caps in re.findall(r'^path "([^"]+)" \{ capabilities = \[([^\]]*)\] \}$', text or "", re.M)]


def _acl_match(pattern, path):
    glob = pattern.endswith("*")
    pat = pattern[:-1] if glob else pattern
    if "+" not in pat:
        return path.startswith(pat) if glob else path == pat
    pp, ps = pat.split("/"), path.split("/")
    if len(ps) < len(pp) or (not glob and len(ps) != len(pp)):
        return False
    for i, seg in enumerate(pp):
        last = i == len(pp) - 1
        if seg == "+":
            continue
        if last and glob:
            if not ps[i].startswith(seg):
                return False
        elif seg != ps[i]:
            return False
    return True


def _acl_priority(pattern):
    """Clé de priorité d'OpenBao (la plus grande gagne) : premier joker le
    plus tard possible, pas de `*` final, moins de `+`, plus long, puis
    l'ordre lexicographique."""
    first = min([i for i, ch in enumerate(pattern) if ch in "+*"] or [len(pattern)])
    return (first, not pattern.endswith("*"), -pattern.count("+"), len(pattern), pattern)


def acl_allows(policy_text, path, cap):
    """La politique (texte rendu par openbao_policy._hcl_policy) donne-t-elle
    `cap` sur `path` ? La règle la plus précise décide, sans union."""
    rules = [(p, c) for p, c in _acl_rules(policy_text) if _acl_match(p, path)]
    if not rules:
        return False
    best = max(rules, key=lambda r: _acl_priority(r[0]))
    return cap in best[1] or (cap == "update" and "create" in best[1])


def token_accessor(tok):
    import hashlib
    return "tacc-" + hashlib.sha256(tok.encode()).hexdigest()[:16]


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
        # IMP-1 : politiques portées par une entité (id -> liste), espaces de
        # noms d'OpenBao 2.x ; un instantané fabriqué les remplit
        self.entity_policies = {}
        self.namespaces = []
        # tâche 11 : un instantané fabriqué (jetons aux politiques étrangères,
        # droits d'administration donnés à la console) ; faux = comme OpenBao
        self.crafted_admin = False           # la console obtient les chemins d'administration
        # relecture finale I3 : configuration relue par la revue (liste
        # permise), comme `initialize` la pose ; un test la modifie pour
        # simuler un instantané fabriqué
        self.default_policy = 'path "auth/token/lookup-self" {\n    capabilities = ["read"]\n}\n'
        self.role_params = {}                # rôle AppRole -> paramètres posés
        self.auth_mounts = {"token/": {"accessor": TOKEN_ACCESSOR, "type": "token"},
                            "approle/": {"accessor": "auth_approle_x", "type": "approle"}}
        self.secret_mounts = {"cubbyhole/": {"type": "cubbyhole"}, "identity/": {"type": "identity"},
                              "sys/": {"type": "system"}, "transit/": {"type": "transit"},
                              "harvester-ops/": {"type": "kv", "options": {"version": "2"}}}
        self.audit_devices = {"hops/": {"type": "file", "path": "hops/"}}
        self.token_roles = {}
        self.groups = []
        # 2.10.0 : méthodes jwt posées par l'agent (configuration et rôles
        # relus comme OpenBao 2.7.2), groupes d'identité par nom, et la
        # politique de l'agent appliquée à ses jetons (`enforce_agent`)
        self.jwt_config = {}                 # montage -> configuration posée
        self.jwt_roles = {}                  # montage -> {rôle: paramètres}
        self.group_docs = {}                 # id -> {"name", "type", "policies", "member_entity_ids", ...}
        self.enforce_agent = False
        self.agent_denied = []               # (méthode, chemin) refusés par la politique de l'agent

    # -- amorce, comme `initialize` -------------------------------------------
    def initialize(self, boot_dir):
        boot = {f: Path(boot_dir, f).read_text() for f in pol.BOOT_FILES}
        if self.role_ids:
            # instance neuve d'une restauration : ce qu'elle pose disparaît à
            # la restauration (le stockage de l'instantané remplace tout)
            import copy
            self._before_fresh = copy.deepcopy((self.accessors, self.roles, self.policies))
        for role, sid in ((pol.console_role(self.cid), boot["console-secret-id"]),
                          (pol.agent_role(self.cid), boot["agent-secret-id"])):
            self.role_ids.setdefault(role, "rid-" + secrets.token_hex(6))
            self.add_role(self.role_ids[role], sid)
            self.accessors["acc-" + secrets.token_hex(4)] = (self.role_ids[role], sid)
        self.tokens[boot["boot-token"]] = {"ttl": 900, "policies": [pol.boot_policy_name(self.cid)],
                                           "created": __import__("time").time()}
        cid = self.cid
        self.policies.update({pol.console_role(cid): pol.console_policy("harvester-ops", f"consoles/{cid}", cid,
                                                                         b1=True),
                              pol.agent_role(cid): pol.agent_policy(cid),
                              pol.boot_policy_name(cid): pol.boot_policy(cid)})
        self.role_params = {pol.console_role(cid): dict(token_policies=[pol.console_role(cid)],
                                                        **pol.console_role_params(pol.B1_CIDR)),
                            pol.agent_role(cid): dict(token_policies=[pol.agent_role(cid)],
                                                      **pol.agent_role_params())}
        self.token_roles = {pol.tf_role(cid): pol.tf_token_role_params(cid, [pol.B1_CIDR])}
        self.events.append(("initialized", None))

    def role_of(self, name):
        return self.role_ids.get(name)

    def login_policies(self, role_id):
        # comme `initialize` : token_policies = nom du rôle
        return [next((n for n, r in self.role_ids.items() if r == role_id), "default")]

    def _agent_check(self, rec, path):
        """Politique de l'agent appliquée à ses jetons (règles d'OpenBao :
        la règle la plus précise décide, `*` final, `+` par segment)."""
        tok = self._auth(rec)
        info = self.tokens.get(tok) or {}
        if pol.agent_role(self.cid) not in info.get("policies", ()):
            return True
        if path in ("auth/token/lookup-self", "auth/token/revoke-self", "auth/token/renew-self"):
            return True                     # politique `default`
        cap = {"GET": "read", "LIST": "list", "DELETE": "delete"}.get(rec["method"], "update")
        if rec["method"] == "GET" and rec["query"].get("list") == ["true"]:
            cap = "list"
        text = self.policies.get(pol.agent_role(self.cid)) or ""
        need_sudo = path.startswith("sys/auth/") or path in ("sys/rotate/root", "sys/audit", "auth/token/accessors")
        ok = acl_allows(text, path, cap) and (not need_sudo or acl_allows(text, path, "sudo"))
        if not ok:
            self.agent_denied.append((rec["method"], path))
        return ok

    def _jwt_mount_of(self, path):
        for k, v in self.auth_mounts.items():
            if v.get("type") == "jwt" and path.startswith("auth/" + k):
                return k.rstrip("/"), path[len("auth/" + k):]
        return None, None

    def _route_jwt_and_groups(self, h, rec, path, body):
        """Rend None si le chemin n'est pas l'un de ceux-là."""
        m = rec["method"]
        parts = path.split("/")
        if path.startswith("sys/auth/") and len(parts) >= 3:
            name = "/".join(parts[2:]).strip("/")
            key = name + "/"
            if m in ("POST", "PUT"):
                if key in self.auth_mounts:
                    return h._err(400, f"path is already in use at {key}")
                typ = body.get("type")
                self.auth_mounts[key] = {"type": typ, "accessor": f"auth_{typ}_" + secrets.token_hex(4),
                                         "description": body.get("description", "")}
                self.events.append(("auth-enable", name))
                return h._send(204)
            if m == "DELETE":
                gone = self.auth_mounts.pop(key, None)
                self.jwt_config.pop(name, None)
                self.jwt_roles.pop(name, None)
                if gone:
                    for e in self.entities.values():          # comme OpenBao : alias du montage retirés
                        if isinstance(e, dict) and "aliases" in e:
                            e["aliases"] = {a for a in e["aliases"] if a[0] != gone.get("accessor")}
                self.events.append(("auth-disable", name))
                return h._send(204)
            cur = self.auth_mounts.get(key)
            return h._send(200, {"data": dict(cur)}) if cur else h._err(400, f"no auth engine at {key}")
        mount, rest = self._jwt_mount_of(path)
        if mount is not None and rest == "config":
            if m in ("POST", "PUT"):
                srcs = [k for k in ("jwt_validation_pubkeys", "jwks_url", "oidc_discovery_url") if body.get(k)]
                if len(srcs) != 1:
                    return h._err(400, "exactly one of 'jwt_validation_pubkeys', 'jwks_url', 'jwks_pairs', "
                                       "'oidc_discovery_url', or 'provider_config' must be set")
                self.jwt_config[mount] = dict(body)
                self.events.append(("jwt-config", mount))
                return h._send(204)
            c = self.jwt_config.get(mount)
            if c is None:
                return h._err(404)
            out = {"bound_issuer": "", "default_role": "", "jwt_supported_algs": [], "jwt_validation_pubkeys": [],
                   "jwks_url": "", "jwks_ca_pem": "", "oidc_discovery_url": "", "oidc_discovery_ca_pem": "",
                   "oidc_client_id": "", "provider_config": {}, "namespace_in_state": True,
                   "oidc_response_mode": "", "oidc_response_types": []}
            out.update(c)
            return h._send(200, {"data": out})
        if mount is not None and (rest == "role" or rest.startswith("role/")):
            roles = self.jwt_roles.setdefault(mount, {})
            if rest == "role" or rest == "role/":
                if m == "LIST" or rec["query"].get("list") == ["true"]:
                    return h._send(200, {"data": {"keys": sorted(roles)}}) if roles else h._err(404)
                return h._err(405)
            name = rest[len("role/"):]
            if m in ("POST", "PUT"):
                # comme OpenBao : l'écriture d'un rôle existant fusionne (un
                # champ absent de la demande garde sa valeur, revue 6-7 M-2)
                roles[name] = dict(roles.get(name) or {}, **body)
                self.events.append(("jwt-role", f"{mount}/{name}"))
                return h._send(204)
            if m == "DELETE":
                roles.pop(name, None)
                self.events.append(("jwt-role-delete", f"{mount}/{name}"))
                return h._send(204)
            r = roles.get(name)
            if r is None:
                return h._err(404)
            from fake_openbao import _ttl_s
            d = {"bound_claims": None, "bound_claims_type": "string", "bound_subject": "", "groups_claim": "",
                 "token_period": 0, "token_explicit_max_ttl": 0, "token_num_uses": 0, "bound_cidrs": [],
                 "token_policies_template_claims": False, "user_claim_json_pointer": False,
                 "token_strictly_bind_ip": False, "verbose_oidc_logging": False,
                 "expiration_leeway": 0, "not_before_leeway": 0, "clock_skew_leeway": 0, "max_age": 0}
            d.update(r)
            d["policies"] = list(r.get("token_policies") or [])
            for k in ("token_ttl", "token_max_ttl"):
                d[k] = _ttl_s(r.get(k), 0)
            d["token_bound_cidrs"] = [c[:-3] if str(c).endswith("/32") else c for c in r.get("token_bound_cidrs") or []]
            return h._send(200, {"data": d})
        if path.startswith("identity/group/name/") and len(parts) == 4:
            name = parts[3]
            gid = next((i for i, g in self.group_docs.items() if g["name"] == name), None)
            if m in ("POST", "PUT"):
                if gid is None:
                    gid = "grp-" + secrets.token_hex(6)
                    self.group_docs[gid] = {"id": gid, "name": name, "type": "internal", "policies": [],
                                            "member_entity_ids": [], "alias": {}}
                    self.groups.append(gid)
                g = self.group_docs[gid]
                for k in ("type", "policies", "member_entity_ids", "metadata"):
                    if k in body:
                        v = body[k]
                        g[k] = [x for x in v.split(",") if x] if k != "type" and isinstance(v, str) else v
                self.events.append(("group-put", name))
                return h._send(200, {"data": {"id": gid, "name": name}})
            if gid is None:
                return h._err(404)
            if m == "DELETE":
                del self.group_docs[gid]
                self.groups.remove(gid)
                self.events.append(("group-delete", name))
                return h._send(204)
            return h._send(200, {"data": dict(self.group_docs[gid])})
        if path.startswith("identity/group/id/") and len(parts) == 4:
            g = self.group_docs.get(parts[3])
            if g is None:
                return h._err(404)
            if m == "DELETE":
                del self.group_docs[parts[3]]
                self.groups.remove(parts[3])
                return h._send(204)
            return h._send(200, {"data": dict(g)})
        return _NOT_MINE

    def _route(self, h, rec, path, body, raw):
        m = rec["method"]
        if self.enforce_agent and self._auth(rec) and not self._agent_check(rec, path):
            return h._err(403, "1 error occurred:\n\t* permission denied\n\n")
        if self._auth(rec):
            r = self._route_jwt_and_groups(h, rec, path, body)
            if r is not _NOT_MINE:
                return r
        if path in ("sys/storage/raft/snapshot", "sys/storage/raft/snapshot-force") and m in ("POST", "PUT") \
                and getattr(self, "_before_fresh", None) is not None and self._auth(rec) \
                and (self.snapshot_key_ok or path.endswith("-force")):
            boot = [t for t, i in self.tokens.items() if pol.boot_policy_name(self.cid) in i["policies"]]
            self.accessors, self.roles, self.policies = self._before_fresh
            self._before_fresh = None
            for t in boot:                      # le jeton d'amorce de l'instance neuve disparaît
                self.tokens.pop(t, None)
            self.restored.append(raw)
            return h._send(204)
        if path in self.fail_paths:
            code, msg = self.fail_paths.pop(path)
            return h._err(code, msg)
        if path == "sys/seal-status":
            return h._send(200, {"type": "static", "initialized": self.initialized, "sealed": self.sealed,
                                 "version": self.seal_version})
        tok = self._auth(rec)
        parts = path.split("/")
        if path == "auth/approle/login" and body.get("role_id") in self.role_ids.values() \
                and body.get("secret_id") in (self.roles.get(body.get("role_id")) or {}).get("secret_ids", ()):
            # comme OpenBao : la connexion AppRole crée (une fois) l'entité de
            # l'alias, nommé par le role_id sur le montage approle/
            key = (self.auth_mounts["approle/"]["accessor"], body["role_id"])
            if not any(key in e["aliases"] for e in self.entities.values()):
                eid = secrets.token_hex(8)
                self.entities[eid] = {"name": f"entity_{eid}", "aliases": {key}}
        if tok and m == "LIST" and path in ("auth/approle/role", "sys/policies/acl", "auth/token/roles",
                                            "identity/group/id", "identity/entity/id", "sys/namespaces"):
            keys = {"auth/approle/role": lambda: sorted(self.role_ids),
                    "sys/policies/acl": lambda: sorted(set(self.policies) | {"default", "root"}),
                    "auth/token/roles": lambda: sorted(self.token_roles),
                    "identity/group/id": lambda: list(self.groups),
                    "identity/entity/id": lambda: sorted(self.entities),
                    "sys/namespaces": lambda: [n + "/" for n in self.namespaces]}[path]()
            return h._send(200, {"data": {"keys": keys}}) if keys else h._err(404)
        if tok and m == "GET" and path == "sys/mounts":
            return h._send(200, {"data": dict(self.secret_mounts)})
        if tok and m == "GET" and path == "sys/audit":
            return h._send(200, {"data": dict(self.audit_devices)})
        if tok and m == "GET" and path.startswith("auth/token/roles/"):
            r = self.token_roles.get(parts[3])
            return h._send(200, {"data": dict(r, name=parts[3])}) if r else h._err(404)
        if tok and m in ("POST", "PUT") and path.startswith("auth/token/roles/") and len(parts) == 4:
            # une écriture de rôle de jeton fusionne avec le rôle existant (OpenBao)
            self.token_roles[parts[3]] = dict(self.token_roles.get(parts[3]) or {}, **body)
            return h._send(204, None)
        person = bool(tok) and (self.tokens.get(tok) or {}).get("kind") == "person"
        if tok and path == "auth/token/lookup-self" and tok in self.tokens and not person:
            info = self.tokens[tok]
            return h._send(200, {"data": {"accessor": token_accessor(tok), "ttl": info["ttl"],
                                          "policies": info["policies"], "period": info["ttl"]}})
        if tok and path == "auth/token/revoke-accessor":
            t = next((t for t in self.tokens if token_accessor(t) == body.get("accessor")), None)
            if t is None:
                return h._err(400, "invalid accessor")
            self.tokens.pop(t)
            self.events.append(("revoke-accessor", body.get("accessor")))
            return h._send(204)
        if path.startswith("auth/approle/role/") and tok:
            role = parts[3]
            rid = self.role_ids.get(role)
            rest = "/".join(parts[4:])
            if rid is None:
                return h._err(400, "role not found")
            if rest == "" and m == "GET":
                d = dict(self.role_params.get(role) or {"token_policies": [role]}, bind_secret_id=True)
                # comme OpenBao 2.7.2 (relevé sur la VM client en tâche 13) :
                # token_bound_cidrs rend l'adresse seule pour un /32, alors
                # que secret_id_bound_cidrs garde « 127.0.0.1/32 »
                d["token_bound_cidrs"] = [c[:-3] if str(c).endswith("/32") else c
                                          for c in d.get("token_bound_cidrs") or []]
                return h._send(200, {"data": d})
            if rest == "secret-id" and m == "LIST":
                return h._send(200, {"data": {"keys": [a for a, (r, _s) in self.accessors.items() if r == rid]}})
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
        if path == "sys/capabilities-self" and tok and not person:
            # comme la politique de la console : patch en plus sur metadata ;
            # les chemins d'administration lui sont refusés (sauf instantané
            # fabriqué, tâche 11)
            def caps(p):
                if any(p.startswith(d) for d in self.denied):
                    return ["deny"]
                if admin_path(p) and not self.crafted_admin:
                    return ["deny"]
                return ["create", "read", "update", "patch", "delete", "list"]
            out = {p: caps(p) for p in body.get("paths", [])}
            return h._send(200, dict(out, data=dict(out)))
        if path == "auth/token/accessors" and tok and m == "LIST":
            return h._send(200, {"data": {"keys": [token_accessor(t) for t in self.tokens]}})
        if path == "auth/token/lookup-accessor" and tok:
            t = next((t for t in self.tokens if token_accessor(t) == body.get("accessor")), None)
            if t is None:
                return h._err(400, "invalid accessor")
            return h._send(200, {"data": {"accessor": body["accessor"], "policies": self.tokens[t]["policies"]}})
        if path == "sys/auth" and tok:
            return h._send(200, {"data": dict(self.auth_mounts)})
        if path.startswith("sys/policies/acl/") and tok:
            name = path.split("/", 3)[3]
            if m == "GET":
                text = self.default_policy if name == "default" else self.policies.get(name)
                return h._send(200, {"data": {"name": name, "policy": text}}) if text is not None else h._err(404)
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
            if eid not in self.entities:
                return h._err(404)
            # comme `vault read identity/entity/id/<id>` : politiques et alias
            # (accesseur et chemin du montage, nom)
            paths = {v["accessor"]: k for k, v in self.auth_mounts.items()}
            e = self.entities[eid]
            return h._send(200, {"data": {"id": eid, "name": e["name"],
                                          "policies": list(self.entity_policies.get(eid) or []),
                                          "aliases": [{"mount_accessor": acc, "mount_path": "auth/" + paths.get(acc, "?"),
                                                       "name": n, "canonical_id": eid}
                                                      for acc, n in sorted(e["aliases"])]}})
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
        self.addrs = ["172.16.1.11"]          # adresses de l'hôte (écouteur ESO, 2.10.0)

    def addresses(self):
        return list(self.addrs)

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
