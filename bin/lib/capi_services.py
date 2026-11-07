"""harvester-ops : des services sur les clusters créés par Cluster API (v1.52.0, B2).

Un service est un `HelmChartProxy` (CAAPH, fournisseur d'add-ons Helm de
Cluster API) posé dans l'espace de noms du cluster sur le cluster de
gestion ; il vise le cluster par une étiquette que la console pose sur le
`Cluster`. CAAPH installe le chart, le met à jour, et le désinstalle quand
le service disparaît. Ce module ne parle pas au cluster : catalogue,
contrôle d'une demande, manifestes, lecture de l'état.

Voir docs/design/2026-09-26-services-capi.md.
"""

import base64
import json
import re

K_HCP = "helmchartproxies.addons.cluster.x-k8s.io"
K_HRP = "helmreleaseproxies.addons.cluster.x-k8s.io"
HCP_API = "addons.cluster.x-k8s.io/v1alpha1"
LABEL_PREFIX = "harvester-ops.io/svc-"
MANAGED = "harvester-ops.io/managed"
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,40}[a-z0-9])?$")
URL_RE = re.compile(r"^(https?|oci)://[^\s]+$")
IPAM = ("dhcp", "pool")

# Le catalogue. Les valeurs portent des marqueurs __NOM__ que la console
# remplace par les paramètres du formulaire ; `{{ ... }}` reste aux gabarits
# de CAAPH (le nom du cluster, par exemple).
CATALOG = {
    "coredns": {
        "title": "DNS (CoreDNS)",
        "repo": "https://coredns.github.io/helm",
        "chart": "coredns",
        "version": "1.47.1",
        "namespace": "dns",
        "params": {"upstream": "1.1.1.1 9.9.9.9", "hosts": "", "ipam": "dhcp"},
        "values": """isClusterService: false
serviceType: LoadBalancer
service:
  annotations:
    cloudprovider.harvesterhci.io/ipam: __IPAM__
servers:
  - zones:
      - zone: .
        use_tcp: true
    port: 53
    plugins:
      - name: errors
      - name: health
      - name: ready
      - name: log
      - name: hosts
        configBlock: |-
__HOSTS__
          fallthrough
      - name: forward
        parameters: . __UPSTREAM__
      - name: cache
        parameters: 30
""",
    },
    "podinfo": {
        "title": "podinfo (application témoin)",
        "repo": "https://stefanprodan.github.io/podinfo",
        "chart": "podinfo",
        "version": "6.15.0",
        "namespace": "podinfo",
        "params": {"ipam": "dhcp"},
        "values": """ui:
  message: "Déployé par harvester-ops sur {{ .Cluster.metadata.name }}"
service:
  type: LoadBalancer
  annotations:
    cloudprovider.harvesterhci.io/ipam: __IPAM__
""",
    },
}
CUSTOM = "custom"
# 2.6.0 (tâche 10) : un chart publié dans un registre déclaré, tiré par CAAPH
# en `oci://` avec le compte de lecture et la CA du registre
REGISTRY = "registry"
SOURCE_KEYS = ("registry", "package", "version", "item")
KEYS = {"service", "name", "cluster", "repo", "chart", "version", "namespace",
        "values", "params", "source", "origin", "by"}
BY_MAX = 128


def catalog():
    """Ce que la page propose, sans les gabarits internes."""
    return [{"key": k, "title": v["title"], "repo": v["repo"], "chart": v["chart"],
             "version": v["version"], "namespace": v["namespace"],
             "params": dict(v["params"]), "values": v["values"]}
            for k, v in CATALOG.items()]


def normalize(raw):
    raw = dict(raw or {})
    unknown = set(raw) - KEYS
    if unknown:
        raise ValueError("unknown option(s): " + ", ".join(sorted(unknown)))
    svc = str(raw.get("service") or "").strip()
    base = CATALOG.get(svc, {})
    params = dict(base.get("params") or {})
    params.update({str(k): str(v) for k, v in (raw.get("params") or {}).items()})
    source = raw.get("source")
    if source is not None:
        if not isinstance(source, dict) or set(source) != set(SOURCE_KEYS) or \
                not all(isinstance(source[k], str) and source[k] for k in SOURCE_KEYS):
            raise ValueError("source: {registry, package, version, item}")
        svc = REGISTRY
    out = {
        "service": svc,
        "name": str(raw.get("name") or svc).strip().lower(),
        "cluster": str(raw.get("cluster") or "").strip(),
        "repo": str(raw.get("repo") or base.get("repo") or "").strip(),
        "chart": str(raw.get("chart") or base.get("chart") or "").strip(),
        "version": str(raw.get("version") or base.get("version") or "").strip(),
        "namespace": str(raw.get("namespace") or base.get("namespace") or "default").strip(),
        "values": raw.get("values") if raw.get("values") is not None else base.get("values", ""),
        "params": params,
    }
    if source is not None:
        out["service"] = REGISTRY
        out["source"] = {k: source[k] for k in SOURCE_KEYS}
        out["name"] = str(raw.get("name") or source["item"]).strip().lower()
    # 2.7.0 : chart d'un paquet appliqué par un plan ou par une personne
    # (étiquettes d'élément et d'origine sur le HelmChartProxy) ; l'origine
    # est contrôlée par l'outil (capi_cluster.parse_origin)
    origin = raw.get("origin")
    if origin is not None:
        if source is None or not isinstance(origin, dict) or \
                not all(isinstance(k, str) and isinstance(v, str) for k, v in origin.items()):
            raise ValueError("origin: an object of names, for a chart from a registry")
        out["origin"] = dict(origin)
    by = raw.get("by", "")
    if not isinstance(by, str) or len(by) > BY_MAX or "\n" in by:
        raise ValueError(f"by: one line, {BY_MAX} characters at most")
    if by:
        out["by"] = by
    return out


def finding(code, level, **facts):
    return {"code": code, "level": level, "facts": facts}


def check(spec, facts):
    """Constats sur une demande : bloquants et avertissements. `facts` :
    clusters (["ns/nom"]), caaph (bool), services (["ns/nom-du-service"]),
    no_lb (clusters encore sans kube-vip, qui le recevront), service_clusters
    ({"ns/nom-du-service": ["ns/cluster" qui porte son étiquette]})."""
    out = []
    if not facts.get("caaph"):
        out.append(finding("caaph-missing", "block"))
    if spec["service"] not in CATALOG and spec["service"] not in (CUSTOM, REGISTRY) or \
            (spec["service"] == REGISTRY) != bool(spec.get("source")):
        out.append(finding("service-unknown", "block", service=spec["service"]))
    if not NAME_RE.match(spec["name"]):
        out.append(finding("invalid-name", "block", name=spec["name"]))
    if spec["cluster"] not in set(facts.get("clusters") or []):
        out.append(finding("cluster-missing", "block", cluster=spec["cluster"]))
    if spec["cluster"] in set(facts.get("no_lb") or []):
        out.append(finding("lb-added", "ok", cluster=spec["cluster"]))
    ns = spec["cluster"].split("/", 1)[0]
    # 2.7.0 (m-15) : le HelmChartProxy se nomme comme le service dans le
    # namespace ; un second cluster de ce namespace le reprendrait (ses
    # identifiants de registre passeraient à l'autre, le Secret du premier
    # resterait orphelin) : refusé
    others = [c for c in (facts.get("service_clusters") or {}).get(f"{ns}/{spec['name']}") or []
              if c != spec["cluster"]]
    if others:
        out.append(finding("service-name-taken", "block", name=spec["name"], cluster=others[0]))
    elif f"{ns}/{spec['name']}" in set(facts.get("services") or []):
        out.append(finding("service-exists", "warn", name=spec["name"]))
    if not URL_RE.match(spec["repo"]):
        out.append(finding("invalid-repo", "block", repo=spec["repo"]))
    if not spec["chart"] or not re.match(r"^[A-Za-z0-9][\w.-]*$", spec["chart"]):
        out.append(finding("invalid-chart", "block", chart=spec["chart"]))
    if not NAME_RE.match(spec["namespace"]):
        out.append(finding("invalid-namespace", "block", namespace=spec["namespace"]))
    if not spec["version"]:
        out.append(finding("version-floating", "warn"))
    if spec["params"].get("ipam") and spec["params"]["ipam"] not in IPAM:
        out.append(finding("invalid-ipam", "block", ipam=spec["params"]["ipam"]))
    try:
        import yaml
        yaml.safe_load(render_values(spec))
    except Exception as e:                       # noqa: BLE001 (message rendu tel quel)
        out.append(finding("invalid-values", "block", message=str(e).splitlines()[0][:200]))
    return out


def blocking(found):
    return [f for f in found if f["level"] == "block"]


def render_values(spec):
    """Les valeurs du chart, marqueurs du formulaire remplacés."""
    text = spec["values"] or ""
    p = spec["params"]
    hosts = "\n".join("          " + line.strip() for line in (p.get("hosts") or "").splitlines()
                      if line.strip())
    repl = {"__UPSTREAM__": p.get("upstream", ""), "__IPAM__": p.get("ipam", "dhcp"),
            "__HOSTS__": hosts}
    for k, v in repl.items():
        text = text.replace(k, v)
    # un bloc hosts vide ne garde que « fallthrough »
    return re.sub(r"(configBlock: \|-\n)\n", r"\1", text)


def label_of(name):
    return LABEL_PREFIX + name


# ---------------------------------------------------------------------------
# Charts tirés d'un registre déclaré (2.6.0, tâche 10)
# ---------------------------------------------------------------------------
def _merge(base, over):
    """Valeurs du formulaire posées sur celles de l'élément du paquet
    (dictionnaires fusionnés en profondeur, le reste remplacé)."""
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def resolve_source(spec, endpoint, chart, item_values):
    """La demande d'un chart publié devient celle d'un chart OCI : dépôt
    `oci://<registre>/charts` (rangement de oci_layout.chart_repo), nom et
    version du Chart.yaml du paquet, valeurs de l'élément puis celles du
    formulaire. Rend une copie."""
    import yaml
    out = dict(spec, repo=f"oci://{endpoint}/charts", chart=chart["name"], version=chart["version"])
    form = yaml.safe_load(spec.get("values") or "") or {}
    if not isinstance(form, dict):
        raise ValueError("values: a YAML mapping")
    merged = _merge(item_values or {}, form)
    out["values"] = yaml.safe_dump(merged, sort_keys=False, allow_unicode=True) if merged else ""
    return out


def registry_secret_names(spec, registry):
    """Secrets d'un chart publié, dans le namespace du cluster enfant :
    compte de lecture au format `config.json` de Docker (partagé par les
    services du même registre) et CA (celui que pose la création du cluster
    avec ce registre, tâche 9)."""
    child = spec["cluster"].partition("/")[2]
    return {"auth": f"{child}-chart-{registry}", "ca": f"{child}-registry-{registry}-ca"}


def registry_secrets(spec, registry, endpoint, creds, ca_pem=None, labels=None):
    """Secrets à poser pour un chart publié (`ca_pem` None : la CA existe
    déjà ou le registre n'en a pas). Compte de LECTURE seulement : il est
    lu par CAAPH sur le cluster Harvester. Étiquetés `labels` (la console,
    le registre) et à ce service : retirés avec lui s'il est le dernier à
    s'en servir, et avec le cluster."""
    names = registry_secret_names(spec, registry)
    ns = spec["cluster"].partition("/")[0]
    lab = dict(labels or {}, **{label_of(spec["name"]): "on"})
    auth = base64.b64encode(f"{creds['username']}:{creds['password']}".encode()).decode()
    docs = [{"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
             "metadata": {"name": names["auth"], "namespace": ns, "labels": dict(lab)},
             "stringData": {"config.json": json.dumps({"auths": {endpoint: {"auth": auth}}})}}]
    if ca_pem:
        docs.append({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                     "metadata": {"name": names["ca"], "namespace": ns, "labels": dict(lab)},
                     "stringData": {"ca.crt": ca_pem}})
    return docs


def secrets_of(hcp):
    """Noms des Secrets qu'un HelmChartProxy désigne (identifiants, CA)."""
    sp = (hcp or {}).get("spec") or {}
    return {n for n in (((sp.get("credentials") or {}).get("secret") or {}).get("name"),
                        ((sp.get("tlsConfig") or {}).get("caSecret") or {}).get("name")) if n}


def secrets_to_remove(removed, remaining, secrets):
    """Secrets d'un service retiré qu'aucun autre service ne désigne plus
    et que la console a posés pour un service (étiquette `svc-`) : jamais
    la CA posée par la création du cluster."""
    keep = set().union(*[secrets_of(h) for h in remaining]) if remaining else set()
    out = []
    for s in secrets:
        md = s.get("metadata") or {}
        if md.get("name") in secrets_of(removed) - keep and \
                any(k.startswith(LABEL_PREFIX) for k in (md.get("labels") or {})):
            out.append(md["name"])
    return sorted(out)


def manifest(spec, registry=None, meta=None):
    """HelmChartProxy du service. `registry` (2.6.0) : {"name", "ca": bool}
    pour un chart publié ; identifiants et CA par les Secrets de
    `registry_secret_names` (`insecureSkipTLSVerify` jamais posé). `meta`
    (2.7.0) : (étiquettes, annotations) d'un élément de paquet
    (capi_manifests.meta_of), ajoutées à celles du service."""
    ns, _, _ = spec["cluster"].partition("/")
    body = {"clusterSelector": {"matchLabels": {label_of(spec["name"]): "on"}},
            "repoURL": spec["repo"], "chartName": spec["chart"],
            "releaseName": spec["name"], "namespace": spec["namespace"],
            "valuesTemplate": render_values(spec),
            "options": {"install": {"createNamespace": True}, "wait": True,
                        "timeout": "10m0s"}}
    if spec["version"]:
        body["version"] = spec["version"]
    if registry:
        names = registry_secret_names(spec, registry["name"])
        body["credentials"] = {"secret": {"name": names["auth"], "namespace": ns}, "key": "config.json"}
        if registry.get("ca"):
            body["tlsConfig"] = {"caSecret": {"name": names["ca"], "namespace": ns}}
    labels, annotations = meta or ({}, {})
    return {"apiVersion": HCP_API, "kind": "HelmChartProxy",
            "metadata": {"name": spec["name"], "namespace": ns,
                         "labels": dict(labels, **{MANAGED: "true"}),
                         "annotations": dict(annotations, **{"harvester-ops.io/service": spec["service"]},
                                             **({"harvester-ops.io/source": "/".join(
                                                 spec["source"][k] for k in SOURCE_KEYS)}
                                                if spec.get("source") else {}))},
            "spec": body}


# ---------------------------------------------------------------------------
# État
# ---------------------------------------------------------------------------

def _cond(obj, kind):
    return next((c for c in ((obj.get("status") or {}).get("conditions") or [])
                 if c.get("type") == kind), None)


def current(obj):
    """L'objet a-t-il été traité dans sa dernière version ? (génération vue
    par son contrôleur). Sans génération (objet pas encore relu), oui."""
    gen = (obj.get("metadata") or {}).get("generation")
    seen = (obj.get("status") or {}).get("observedGeneration")
    return gen is None or (seen or 0) >= gen


HRP_OWNER_LABEL = "helmreleaseproxy.addons.cluster.x-k8s.io/helmchartproxy-name"


def state(hcps, hrps):
    """Un service par HelmChartProxy, avec ses installations (une par
    cluster retenu) : prêt, révision, message."""
    out = []
    for h in hcps:
        md, sp = h.get("metadata") or {}, h.get("spec") or {}
        ns, name = md.get("namespace"), md.get("name")
        releases = []
        for r in hrps:
            rmd, rsp, rst = r.get("metadata") or {}, r.get("spec") or {}, r.get("status") or {}
            owners = [o.get("name") for o in rmd.get("ownerReferences") or []
                      if o.get("kind") == "HelmChartProxy"]
            labels = rmd.get("labels") or {}
            if rmd.get("namespace") != ns or (name not in owners
                                              and labels.get(HRP_OWNER_LABEL) != name):
                continue
            ready = _cond(r, "Ready")
            releases.append({"cluster": (rsp.get("clusterRef") or {}).get("name"),
                             "status": rst.get("status") or "",
                             "revision": rst.get("revision"),
                             "ready": bool(ready and ready.get("status") == "True"),
                             "current": current(r),
                             "message": (ready or {}).get("message") or ""})
        ready = _cond(h, "Ready")
        out.append({"namespace": ns, "name": name,
                    "service": (md.get("annotations") or {}).get("harvester-ops.io/service") or CUSTOM,
                    "chart": sp.get("chartName"), "version": sp.get("version") or "",
                    "repo": sp.get("repoURL"), "release_namespace": sp.get("namespace"),
                    "managed": (md.get("labels") or {}).get(MANAGED) == "true",
                    "ready": bool(ready and ready.get("status") == "True"),
                    "current": current(h),
                    "message": (ready or {}).get("message") or "",
                    "releases": sorted(releases, key=lambda x: x["cluster"] or "")})
    return sorted(out, key=lambda s: (s["namespace"], s["name"]))
