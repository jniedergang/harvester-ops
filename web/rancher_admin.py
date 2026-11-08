"""harvester-ops : ce que la console demande à un Rancher réglé (v1.79.0).

- le test d'un Rancher (version, fournisseurs d'authentification) ;
- l'enregistrement de la console comme client OIDC de Rancher (SSO), avec les
  identifiants d'un administrateur demandés une fois et jamais gardés ;
- le chart Harvester RBAC (catalogue `rancher-charts`), installé dans le
  cluster `local` de Rancher.

Tout passe par un objet `http` (rancher_sso.Http, remplaçable dans les
tests). Aucun identifiant, jeton ni secret n'est écrit dans un journal ou un
message d'erreur.

Faits relevés sur Rancher Prime v2.14.1 (01/10/2026) :
- `/v3-public/authProviders` ne liste que les fournisseurs actifs ;
- `/rancherversion` est public : {"Version": "v2.14.1", ...} ;
- la connexion rend {"token": "token-xxxxx:...", "id": "token-xxxxx",
  "expiresAt": "..."} ; un mauvais mot de passe rend 401 ;
- un jeton ne se supprime pas lui-même par DELETE (400), mais par
  `POST /v3/tokens?action=logout` ;
- l'index du catalogue (`?link=index`) porte les annotations du chart
  `harvester-rbac` 109.0.0+up0.1.1 : rancher-version >= 2.14.0-0 < 2.15.0-0,
  kube-version >= 1.23.0-0 < 1.36.0-0, release-name harvester-rbac, et AUCUNE
  annotation de namespace.
"""

import base64
import json
import time
import urllib.parse

import rancher_servers as srv
import rancher_sso as rs

RBAC_CHART = "harvester-rbac"
RBAC_REPO = "rancher-charts"
# sans annotation catalog.cattle.io/namespace, l'interface de Rancher
# propose « default » pour un chart d'application
RBAC_DEFAULT_NAMESPACE = "default"
OIDC_SECRETS_NS = "cattle-oidc-client-secrets"
OIDC_SECRET_KEY = "client-secret-1"
OIDC_API = "/k8s/clusters/local/apis/management.cattle.io/v3/oidcclients"


class AdminError(Exception):
    """Un refus à rendre tel quel : message lisible, statut HTTP."""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def _bearer(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _msg(out):
    if isinstance(out, dict):
        return str(out.get("message") or out.get("error") or out.get("raw") or "")[:200]
    return ""


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def rancher_version(http, url, token=None):
    """La version de Rancher : `/rancherversion` (public), sinon le réglage
    `server-version`."""
    st, out = http.request("GET", url + "/rancherversion", headers={"Accept": "application/json"})
    if st == 200 and isinstance(out, dict) and (out.get("Version") or out.get("RancherVersion")):
        return out.get("Version") or out.get("RancherVersion")
    st, out = http.request("GET", url + "/v3/settings/server-version",
                           headers=_bearer(token) if token else {"Accept": "application/json"})
    if st == 200 and isinstance(out, dict) and out.get("value"):
        return out["value"]
    return None


def probe(http, url):
    """{ok, version, providers, error} : Rancher répond-il, quelle version,
    quels fournisseurs. Ne lève pas."""
    try:
        st, out = http.request("GET", url + "/v3-public/authProviders",
                               headers={"Accept": "application/json"})
        if st != 200:
            return {"ok": False, "version": None, "providers": [],
                    "error": f"Rancher answered {st} on /v3-public/authProviders"}
        providers = srv.providers_of(out)
        version = rancher_version(http, url)
    except rs.SSOError as e:
        return {"ok": False, "version": None, "providers": [], "error": e.detail or e.code}
    return {"ok": True, "version": version, "providers": providers, "error": None}


# ---------------------------------------------------------------------------
# Session d'administrateur, le temps d'un geste
# ---------------------------------------------------------------------------

class AdminSession:
    """Connexion d'un administrateur de Rancher pour un geste, puis
    déconnexion (le jeton est supprimé dans Rancher). Le mot de passe n'est
    gardé nulle part."""

    def __init__(self, http, s, provider, username, password, ttl=600):
        self.http, self.s = http, s
        if not username or not password:
            raise AdminError("the Rancher administrator's user name and password are required", 400)
        try:
            self.token, self.name, _ = rs.direct_login(http, s, provider or "local", username, password,
                                                       ttl, "harvester-ops administration")
        except rs.SSOError as e:
            if e.code == "bad-credentials":
                raise AdminError("Rancher refused these administrator credentials", 401)
            if e.code == "bad-provider":
                raise AdminError("unknown authentication provider", 400)
            if e.code == "rancher-unreachable":
                raise AdminError("Rancher is unreachable: " + (e.detail or ""), 502)
            raise AdminError("Rancher refused the sign-in", 502)

    def get(self, path):
        return self.http.request("GET", self.s["url"] + path, headers=_bearer(self.token))

    def send(self, method, path, body):
        return self.http.request(method, self.s["url"] + path, headers=_bearer(self.token),
                                 data=json.dumps(body) if body is not None else None)

    def close(self):
        rs.logout_token(self.http, self.s, self.token)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# ---------------------------------------------------------------------------
# Enregistrement SSO (OIDCClient)
# ---------------------------------------------------------------------------

def oidc_client_name(console_host):
    return srv.slug("harvester-ops-" + str(console_host or "console"))[:63]


def register_oidc(admin, name, redirect_uri, token_seconds=600, refresh_seconds=43200,
                  sleep=time.sleep, wait=30):
    """Crée (ou complète) le client OIDC `name` dans Rancher et rend
    (client_id, secret). Un client du même nom déjà présent reçoit notre
    adresse de retour en plus des siennes."""
    body = {"apiVersion": "management.cattle.io/v3", "kind": "OIDCClient",
            "metadata": {"name": name},
            "spec": {"description": "harvester-ops console", "redirectURIs": [redirect_uri],
                     "tokenExpirationSeconds": int(token_seconds),
                     "refreshTokenExpirationSeconds": int(refresh_seconds)}}
    st, out = admin.send("POST", OIDC_API, body)
    if st == 409:
        st, cur = admin.get(f"{OIDC_API}/{name}")
        if st != 200:
            raise AdminError(f"Rancher answered {st} reading the existing OIDC client")
        uris = list(((cur.get("spec") or {}).get("redirectURIs")) or [])
        if redirect_uri not in uris:
            cur.setdefault("spec", {})["redirectURIs"] = uris + [redirect_uri]
            st, out = admin.send("PUT", f"{OIDC_API}/{name}", cur)
            if st not in (200, 201):
                raise AdminError(f"Rancher refused updating the OIDC client ({st}): {_msg(out)}")
    elif st in (401, 403):
        raise AdminError("this account cannot create OIDC clients in Rancher (a Rancher administrator is needed)", 403)
    elif st == 404:
        raise AdminError("this Rancher has no OIDC provider (Rancher 2.12 or later, feature oidc-provider)", 409)
    elif st not in (200, 201):
        raise AdminError(f"Rancher refused the OIDC client ({st}): {_msg(out)}")
    # le contrôleur de Rancher pose status.clientID puis crée le secret
    deadline = time.time() + wait
    client_id = None
    while True:
        st, cur = admin.get(f"{OIDC_API}/{name}")
        client_id = ((cur or {}).get("status") or {}).get("clientID") if st == 200 else None
        if client_id:
            st, sec = admin.get(f"/k8s/clusters/local/api/v1/namespaces/{OIDC_SECRETS_NS}/secrets/"
                                + urllib.parse.quote(client_id))
            raw = ((sec or {}).get("data") or {}).get(OIDC_SECRET_KEY) if st == 200 else None
            if raw:
                try:
                    return client_id, base64.b64decode(raw).decode().strip()
                except (ValueError, UnicodeDecodeError):
                    raise AdminError("the OIDC client secret written by Rancher is unreadable")
        if time.time() >= deadline:
            raise AdminError("Rancher did not issue the OIDC client in time; try again", 504)
        sleep(1)


def unregister_oidc(admin, name):
    st, out = admin.send("DELETE", f"{OIDC_API}/{name}", None)
    if st in (401, 403):
        raise AdminError("this account cannot delete OIDC clients in Rancher (a Rancher administrator is needed)", 403)
    if st not in (200, 202, 204, 404):
        raise AdminError(f"Rancher refused deleting the OIDC client ({st}): {_msg(out)}")
    return st != 404


# ---------------------------------------------------------------------------
# Chart Harvester RBAC
# ---------------------------------------------------------------------------

def _latest(entries):
    def key(e):
        return srv.version_tuple(e.get("version")) or (0, 0, 0)
    return max(entries, key=key) if entries else None


def rbac_status(admin):
    """{installed, version, available_version, compatible, reason,
    rancher_version, kube_version, namespace, roles}."""
    out = {"installed": False, "version": None, "available_version": None, "compatible": False,
           "reason": None, "rancher_version": None, "kube_version": None,
           "namespace": None, "roles": []}
    out["rancher_version"] = rancher_version(admin.http, admin.s["url"], admin.token)
    st, cl = admin.get("/v3/clusters/local")
    out["kube_version"] = ((cl or {}).get("version") or {}).get("gitVersion") if st == 200 else None
    st, apps = admin.get("/v1/catalog.cattle.io.apps?limit=1000")
    if st in (401, 403):
        raise AdminError("this account cannot read the apps of Rancher's local cluster (a Rancher administrator is needed)", 403)
    for a in (apps or {}).get("data") or []:
        md = a.get("metadata") or {}
        chart = ((a.get("spec") or {}).get("chart") or {}).get("metadata") or {}
        if md.get("name") == RBAC_CHART or chart.get("name") == RBAC_CHART:
            out["installed"] = True
            out["version"] = chart.get("version")
            out["namespace"] = md.get("namespace")
            out["state"] = ((a.get("status") or {}).get("summary") or {}).get("state") \
                or (md.get("state") or {}).get("name")
            break
    st, idx = admin.get(f"/v1/catalog.cattle.io.clusterrepos/{RBAC_REPO}?link=index")
    entry = _latest(((idx or {}).get("entries") or {}).get(RBAC_CHART) or []) if st == 200 else None
    out["entry"] = entry
    if entry is None:
        out["reason"] = f"the chart {RBAC_CHART} is not in the {RBAC_REPO} catalog of this Rancher"
    else:
        out["available_version"] = entry.get("version")
        ann = entry.get("annotations") or {}
        rv_c = ann.get("catalog.cattle.io/rancher-version") or ""
        kv_c = ann.get("catalog.cattle.io/kube-version") or entry.get("kubeVersion") or ""
        ok_r = srv.satisfies(out["rancher_version"], rv_c) if rv_c else True
        ok_k = srv.satisfies(out["kube_version"], kv_c) if kv_c else True
        if ok_r is False:
            out["reason"] = f"Rancher {out['rancher_version']} does not match the chart's requirement ({rv_c})"
        elif ok_k is False:
            out["reason"] = f"Kubernetes {out['kube_version']} of the local cluster does not match the chart's requirement ({kv_c})"
        elif ok_r is None or ok_k is None:
            out["reason"] = "the Rancher or Kubernetes version could not be read"
        else:
            out["compatible"] = True
        out["namespace"] = out["namespace"] or ann.get("catalog.cattle.io/namespace") or RBAC_DEFAULT_NAMESPACE
        out["release_name"] = ann.get("catalog.cattle.io/release-name") or RBAC_CHART
    if out["installed"]:
        out["roles"] = rbac_roles(admin)
    return out


def rbac_roles(admin):
    """Les modèles de rôle posés par le chart (release harvester-rbac)."""
    st, rts = admin.get("/v1/management.cattle.io.roletemplates?limit=1000")
    names = []
    for r in (rts or {}).get("data") or [] if st == 200 else []:
        md = r.get("metadata") or {}
        ann = md.get("annotations") or {}
        if ann.get("meta.helm.sh/release-name") == RBAC_CHART:
            names.append({"name": md.get("name"), "display_name": r.get("displayName") or md.get("name"),
                          "context": r.get("context")})
    return sorted(names, key=lambda x: x["name"] or "")


def public_status(status):
    return {k: v for k, v in status.items() if k != "entry"}


def rbac_install(admin, status, step, sleep=time.sleep, timeout=600):
    """Installe le chart par l'API du catalogue de Rancher et attend qu'il
    soit déployé. `step(step_id, state, message)` rapporte l'avancement.
    Rend la liste des modèles de rôle posés."""
    entry = status["entry"]
    ns = status["namespace"] or RBAC_DEFAULT_NAMESPACE
    release = status.get("release_name") or RBAC_CHART
    body = {"charts": [{"chartName": RBAC_CHART, "version": entry["version"], "releaseName": release,
                        "annotations": {"catalog.cattle.io/ui-source-repo-type": "cluster",
                                        "catalog.cattle.io/ui-source-repo": RBAC_REPO},
                        "values": {}}],
            "noHooks": False, "timeout": "600s", "wait": True, "namespace": ns,
            "projectId": "", "disableOpenAPIValidation": False, "skipCRDs": False}
    step("install", "running", f"installing {RBAC_CHART} {entry['version']} in namespace {ns}")
    st, out = admin.send("POST", f"/v1/catalog.cattle.io.clusterrepos/{RBAC_REPO}?action=install", body)
    if st not in (200, 201):
        raise AdminError(f"Rancher refused the installation ({st}): {_msg(out)}")
    op_ns, op_name = (out or {}).get("operationNamespace"), (out or {}).get("operationName")
    step("install", "done", f"Rancher started the operation {op_name or '?'}")
    step("wait", "running", "waiting for the app to be deployed")
    deadline = time.time() + timeout
    last = None
    while True:
        st, app = admin.get(f"/v1/catalog.cattle.io.apps/{ns}/{release}")
        state = None
        if st == 200:
            state = ((app.get("status") or {}).get("summary") or {}).get("state") \
                or ((app.get("metadata") or {}).get("state") or {}).get("name")
        if state == "deployed":
            step("wait", "done", "deployed")
            break
        if op_ns and op_name:
            ost, op = admin.get(f"/v1/catalog.cattle.io.operations/{op_ns}/{op_name}")
            if ost == 200 and ((op.get("metadata") or {}).get("state") or {}).get("error"):
                msg = ((op.get("metadata") or {}).get("state") or {}).get("message") or "the Helm operation failed"
                raise AdminError(str(msg)[:300])
        if state and state != last:
            step("wait", "progress", f"state={state}")
            last = state
        if state in ("failed", "error"):
            raise AdminError(f"the app ended in state {state}")
        if time.time() >= deadline:
            raise AdminError(f"the app was not deployed within {timeout} s (last state: {last or 'none'})", 504)
        sleep(3)
    step("roles", "running", "reading the role templates")
    roles = rbac_roles(admin)
    step("roles", "done", ", ".join(r["display_name"] for r in roles) or "no role template found")
    return roles
