"""harvester-ops : des services sur les clusters créés par Cluster API (v1.52.0, B2).

Un service est un `HelmChartProxy` (CAAPH, fournisseur d'add-ons Helm de
Cluster API) posé dans l'espace de noms du cluster sur le cluster de
gestion ; il vise le cluster par une étiquette que la console pose sur le
`Cluster`. CAAPH installe le chart, le met à jour, et le désinstalle quand
le service disparaît. Ce module ne parle pas au cluster : catalogue,
contrôle d'une demande, manifestes, lecture de l'état.

Voir docs/design/2026-09-26-services-capi.md.
"""

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
KEYS = {"service", "name", "cluster", "repo", "chart", "version", "namespace",
        "values", "params"}


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
    return {
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


def finding(code, level, **facts):
    return {"code": code, "level": level, "facts": facts}


def check(spec, facts):
    """Constats sur une demande : bloquants et avertissements. `facts` :
    clusters (["ns/nom"]), caaph (bool), services (["ns/nom-du-service"]),
    no_lb (clusters encore sans kube-vip, qui le recevront)."""
    out = []
    if not facts.get("caaph"):
        out.append(finding("caaph-missing", "block"))
    if spec["service"] not in CATALOG and spec["service"] != CUSTOM:
        out.append(finding("service-unknown", "block", service=spec["service"]))
    if not NAME_RE.match(spec["name"]):
        out.append(finding("invalid-name", "block", name=spec["name"]))
    if spec["cluster"] not in set(facts.get("clusters") or []):
        out.append(finding("cluster-missing", "block", cluster=spec["cluster"]))
    if spec["cluster"] in set(facts.get("no_lb") or []):
        out.append(finding("lb-added", "ok", cluster=spec["cluster"]))
    ns = spec["cluster"].split("/", 1)[0]
    if f"{ns}/{spec['name']}" in set(facts.get("services") or []):
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


def manifest(spec):
    ns, _, _ = spec["cluster"].partition("/")
    body = {"clusterSelector": {"matchLabels": {label_of(spec["name"]): "on"}},
            "repoURL": spec["repo"], "chartName": spec["chart"],
            "releaseName": spec["name"], "namespace": spec["namespace"],
            "valuesTemplate": render_values(spec),
            "options": {"install": {"createNamespace": True}, "wait": True,
                        "timeout": "10m0s"}}
    if spec["version"]:
        body["version"] = spec["version"]
    return {"apiVersion": HCP_API, "kind": "HelmChartProxy",
            "metadata": {"name": spec["name"], "namespace": ns,
                         "labels": {MANAGED: "true"},
                         "annotations": {"harvester-ops.io/service": spec["service"]}},
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
