"""harvester-ops : le registre d'amorçage posé sur un cluster Harvester (v2.6.0).

CNCF Distribution 3.1 (image BCI de SUSE, point 181), dans un pod du RKE2
de Harvester (point 182) : un volume Longhorn, une adresse fixe servie par
le kube-vip de Harvester (service LoadBalancer annoté
`kube-vip.io/loadbalancerIPs`, comme `capi_stack.shim_manifest`), TLS
toujours (CA générée par la console ou certificat du client, point 183).

L'authentification htpasswd de Distribution n'a pas de droits : le compte
de lecture pourrait écrire (essais préalables). D'où deux conteneurs sur le
même volume (point 197) : une instance en lecture seule
(`storage.maintenance.readonly`) sur le port 443 du service, comptes `push`
et `pull` ; une instance en écriture sur 5443, compte `push` seulement.

`manifests` et `check` sont purs ; `make_tls` et `make_htpasswd` appellent
openssl et htpasswd par un `runner` injectable, clés dans des fichiers 0600,
mots de passe sur l'entrée standard, jamais en argument.

Plan : docs/design/2026-11-05-artefacts-registre-260-plan.md (tâche 7).
"""

import ipaddress
import json
import os
import shutil
import subprocess
from pathlib import Path

import capi_cluster as cc
import registries as rg

NAMESPACE = rg.NAMESPACE
LABEL = rg.LABEL
UID = 499                            # utilisateur `registry` de l'image (essais préalables)
READ_PORT, WRITE_PORT = 443, 5443
READ_TARGET, WRITE_TARGET = 5000, 5001
DATA = "/var/lib/docker-registry"
VIP_ANNOTATION = "kube-vip.io/loadbalancerIPs"
WRITE_SUFFIX = rg.WRITE_NAME_SUFFIX   # Service du port d'écriture (2.6.1)
PROBE_PERIOD = 60                     # règle des sondes : 60 s au moins
CA_DAYS, CERT_DAYS = 3650, 730
TOOLS = ("openssl", "htpasswd")


def bundle_dir(state):
    """Images du registre installées par install.sh (`bundles/registry`)."""
    return Path(os.environ.get("HARVESTER_OPS_REGISTRY_BUNDLE") or Path(state) / "registry-bundle")


def bundle(state):
    """{"tar", "images"} du paquet interne si ses sommes sont justes, sinon None."""
    import node_images as ni
    from self_update import sha256_file
    d = bundle_dir(state)
    try:
        sums = dict(reversed(line.split(None, 1)) for line in (d / ni.BUNDLE_SUMS).read_text().splitlines() if line)
        for name in (ni.BUNDLE_TAR, ni.BUNDLE_MAP):
            if sums.get(name, "").strip() != sha256_file(d / name):
                return None
        images = json.loads((d / ni.BUNDLE_MAP).read_text())["images"]
    except (OSError, ValueError, KeyError):
        return None
    return {"tar": str(d / ni.BUNDLE_TAR), "images": images}


def htpasswd_tool():
    return shutil.which("htpasswd") or shutil.which("htpasswd2")


def tools_missing():
    return [t for t, found in (("openssl", shutil.which("openssl")), ("htpasswd", htpasswd_tool())) if not found]


# ---------------------------------------------------------------------------
# Manifestes
# ---------------------------------------------------------------------------
def distribution_config(read_only):
    """Configuration de Distribution (JSON, donc YAML valide)."""
    storage = {"filesystem": {"rootdirectory": DATA}, "delete": {"enabled": False}}
    if read_only:
        # revue m-7 : l'instance de lecture ne purge jamais les envois en
        # cours de l'instance d'écriture (même volume)
        storage["maintenance"] = {"readonly": {"enabled": True}, "uploadpurging": {"enabled": False}}
    return {"version": 0.1, "log": {"level": "info"}, "storage": storage,
            "http": {"addr": f":{READ_TARGET if read_only else WRITE_TARGET}",
                     "tls": {"certificate": "/etc/registry/tls/tls.crt", "key": "/etc/registry/tls/tls.key"}},
            "auth": {"htpasswd": {"realm": "harvester-ops", "path": "/etc/registry/auth/htpasswd"}}}


def _meta(name, spec, **extra):
    return dict({"name": name, "namespace": NAMESPACE, "labels": {LABEL: spec["name"]}}, **extra)


def origin_of(deployment):
    """Origine d'un registre posé par la chaîne d'un plan : {plan, run}
    (étiquette et annotation du Deployment), clés absentes omises."""
    import capi_cluster as cc
    return cc.origin_of(deployment)


def manifests(spec, tls, htpasswd, origin=None, write_sources=None):
    """Objets du registre d'amorçage. `spec` : {"name", "address",
    "size_gib", "storage_class", "image"} ; `tls` : {"crt", "key", "ca"}
    (PEM) ; `htpasswd` : {"read": lignes des comptes push et pull, "write":
    ligne du compte push} (empreintes bcrypt seulement). `origin` (2.6.0,
    tâche 11) : {plan, run} d'une chaîne, posés comme sur un cluster enfant
    (étiquette `harvester-ops.io/plan`, annotation `harvester-ops.io/plan-run`)."""
    docs = _manifests(spec, tls, htpasswd, write_sources)
    import capi_cluster as cc
    for d in docs[1:] if origin else []:
        if origin.get("plan"):
            d["metadata"]["labels"][cc.ORIGIN_LABELS["plan"]] = origin["plan"]
        if origin.get("run") and d["kind"] == "Deployment":
            d["metadata"].setdefault("annotations", {})[cc.ORIGIN_ANNOTATIONS["run"]] = origin["run"]
    return docs


def _manifests(spec, tls, htpasswd, write_sources=None):
    n = spec["name"]
    sel = {LABEL: n}
    mounts = [{"name": "data", "mountPath": DATA},
              {"name": "config", "mountPath": "/etc/registry/conf", "readOnly": True},
              {"name": "tls", "mountPath": "/etc/registry/tls", "readOnly": True}]

    def container(role, port, conf, auth):
        # revue m-7 : volume en lecture seule pour l'instance de lecture
        vols = [dict(m, readOnly=True) if m["name"] == "data" and role == "read" else m for m in mounts]
        return {"name": role, "image": spec["image"], "imagePullPolicy": "Never",
                "args": ["serve", f"/etc/registry/conf/{conf}"],
                "ports": [{"name": role, "containerPort": port}],
                "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}},
                "readinessProbe": {"tcpSocket": {"port": port}, "periodSeconds": PROBE_PERIOD},
                "livenessProbe": {"tcpSocket": {"port": port}, "periodSeconds": PROBE_PERIOD,
                                  "initialDelaySeconds": 30},
                "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}},
                "volumeMounts": vols + [{"name": auth, "mountPath": "/etc/registry/auth", "readOnly": True}]}

    return [
        {"apiVersion": "v1", "kind": "Namespace",
         "metadata": {"name": NAMESPACE, "labels": {rg.MANAGED: "true"}}},
        {"apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/tls", "metadata": _meta(f"{n}-tls", spec),
         "stringData": {"tls.crt": tls["crt"], "tls.key": tls["key"], "ca.crt": tls["ca"]}},
        {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "metadata": _meta(f"{n}-htpasswd", spec),
         "stringData": {"read": htpasswd["read"], "write": htpasswd["write"]}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": _meta(f"{n}-config", spec),
         "data": {"read.yml": json.dumps(distribution_config(True), indent=2) + "\n",
                  "write.yml": json.dumps(distribution_config(False), indent=2) + "\n"}},
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": _meta(f"{n}-data", spec),
         "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": spec["storage_class"],
                  "resources": {"requests": {"storage": f"{spec['size_gib']}Gi"}}}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": _meta(n, spec),
         "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": sel},
                  "template": {"metadata": {"labels": sel}, "spec": {
                      "securityContext": {"runAsNonRoot": True, "runAsUser": UID, "runAsGroup": UID,
                                          "fsGroup": UID, "seccompProfile": {"type": "RuntimeDefault"}},
                      "automountServiceAccountToken": False,
                      "containers": [container("read", READ_TARGET, "read.yml", "auth-read"),
                                     container("write", WRITE_TARGET, "write.yml", "auth-write")],
                      "volumes": [
                          {"name": "data", "persistentVolumeClaim": {"claimName": f"{n}-data"}},
                          {"name": "config", "configMap": {"name": f"{n}-config"}},
                          {"name": "tls", "secret": {"secretName": f"{n}-tls",
                                                     "items": [{"key": "tls.crt", "path": "tls.crt"},
                                                               {"key": "tls.key", "path": "tls.key"}]}},
                          {"name": "auth-read", "secret": {"secretName": f"{n}-htpasswd",
                                                           "items": [{"key": "read", "path": "htpasswd"}]}},
                          {"name": "auth-write", "secret": {"secretName": f"{n}-htpasswd",
                                                            "items": [{"key": "write", "path": "htpasswd"}]}}]}}}},
        *services(spec, write_sources),
    ]


def write_service_name(name):
    return f"{name}{WRITE_SUFFIX}"


def services(spec, write_sources=None, labels=None):
    """Les deux Services LoadBalancer du registre, à la même adresse servie
    par le kube-vip de Harvester (2.6.1, essai de la tâche 13 de la 2.6.0) :
    lecture 443 ouverte à tous (les nœuds des clusters enfants), écriture
    5443 dans un Service à elle, limitée par `loadBalancerSourceRanges` aux
    réseaux de la console (`write_sources`, vide : ouverte à tous). Un seul
    Service à plages limitées couperait aussi 443 aux VMs. `labels` :
    étiquettes à garder (reprise d'un registre posé, origine d'un plan)."""
    n = spec["name"]
    sel = {LABEL: n}

    def svc(name, port_name, port, target):
        meta = _meta(name, spec, annotations={VIP_ANNOTATION: spec["address"]})
        if labels:
            meta["labels"] = dict(labels)
        # revue m-6 : kube-vip sert l'adresse sans NodePort ; sans ce champ,
        # les ports s'ouvraient aussi sur chaque nœud Harvester
        return {"apiVersion": "v1", "kind": "Service", "metadata": meta,
                "spec": {"type": "LoadBalancer", "allocateLoadBalancerNodePorts": False, "selector": sel,
                         "ports": [{"name": port_name, "port": port, "targetPort": target}]}}
    read = svc(n, "read", READ_PORT, READ_TARGET)
    write = svc(write_service_name(n), "write", WRITE_PORT, WRITE_TARGET)
    if write_sources:
        write["spec"]["loadBalancerSourceRanges"] = list(write_sources)
    return [read, write]


def write_access(main, write):
    """État du port d'écriture lu sur le cluster : {"migrated": bool (Service
    d'écriture à part, 5443 retiré du Service principal), "sources": [...]}."""
    ports = [p.get("port") for p in (((main or {}).get("spec") or {}).get("ports") or [])]
    migrated = write is not None and WRITE_PORT not in ports
    sources = sorted((((write or {}).get("spec") or {}).get("loadBalancerSourceRanges") or []))
    return {"migrated": migrated, "sources": sources if migrated else []}


def reconcile_write_access(kube, name, sources, step=None):
    """Porte le registre d'amorçage `name` au réglage courant : 5443 retiré
    du Service principal, Service d'écriture à plages limitées. D'abord le
    Service principal (le port quitte l'ancien Service), puis celui
    d'écriture : quelques secondes sans écriture, jamais deux Services sur
    le même port. Rend {"changed", "missing", "sources"}."""
    main = kube.get("services", NAMESPACE, name)
    if main is None:
        return {"changed": False, "missing": True, "sources": []}
    meta = main.get("metadata") or {}
    address = (meta.get("annotations") or {}).get(VIP_ANNOTATION)
    write = kube.get("services", NAMESPACE, write_service_name(name))
    want = sorted(sources or [])
    now = write_access(main, write)
    if now["migrated"] and now["sources"] == want:
        return {"changed": False, "missing": False, "sources": want}
    _, write_svc = services({"name": name, "address": address}, want, labels=meta.get("labels") or None)
    ports = (main.get("spec") or {}).get("ports") or []
    if WRITE_PORT in [p.get("port") for p in ports]:
        # patch de fusion (liste remplacée) plutôt qu'application côté
        # serveur : le port est retiré quel que soit le gestionnaire qui le
        # possède (vu en réel : un port ajouté par kubectl patch restait)
        kube.patch("services", NAMESPACE, name, {"spec": {"ports": [p for p in ports if p.get("port") != WRITE_PORT]}})
        if step:
            step("write-access", "running", f"{name}: port {WRITE_PORT} removed from service {name}")
    kube.apply([write_svc])
    if step:
        step("write-access", "running", f"{name}: service {write_service_name(name)} "
                                        f"({', '.join(want) if want else 'open to every address'})")
    return {"changed": True, "missing": False, "sources": want}


def declaration(spec, ca_pem, cluster, mirrors=()):
    """Déclaration console d'un registre d'amorçage (sans secret)."""
    d = {"kind": "bootstrap", "software": "distribution", "endpoint": spec["address"],
         "push_endpoint": f"{spec['address']}:{WRITE_PORT}", "ca_pem": ca_pem, "usage": ["images", "charts"],
         "mirrors": list(mirrors), "clusters": [cluster], "cluster": cluster, "address": spec["address"],
         "size_gib": spec["size_gib"], "storage_class": spec["storage_class"]}
    if spec.get("dns_name"):
        d["dns_name"] = spec["dns_name"]
    return d


# ---------------------------------------------------------------------------
# Constats
# ---------------------------------------------------------------------------
def _in_range(ip, r):
    try:
        a = int(ipaddress.IPv4Address(ip))
        return int(ipaddress.IPv4Address(r["start"])) <= a <= int(ipaddress.IPv4Address(r["end"]))
    except (ValueError, KeyError, TypeError):
        return False


def check(spec, facts):
    """Constats d'une demande de registre d'amorçage. `facts` : {"pools":
    [pool_info], "vip": adresse, "address_answers": bool, "longhorn":
    {"nodes", "over", "minimal"} ou None, "class_replicas": {classe: n},
    "bundle": bool, "tools_missing": [...], "name_taken": bool}."""
    out = []
    addr = spec.get("address") or ""
    try:
        ok = isinstance(ipaddress.ip_address(addr), ipaddress.IPv4Address)
    except ValueError:
        ok = False
    if not ok:
        out.append(cc.finding("registry-address-invalid", "block", address=addr))
    else:
        for p in facts.get("pools") or []:
            if any(_in_range(addr, r) for r in p.get("ranges") or []):
                out.append(cc.finding("registry-address-in-pool", "block", address=addr, pool=p.get("name")))
        if addr == facts.get("vip"):
            out.append(cc.finding("registry-address-vip", "block", address=addr))
        if facts.get("address_answers"):
            out.append(cc.finding("registry-address-answers", "block", address=addr))
        # l'adresse n'est pas encore servie : la sonde du réseau des VMs
        # (2.6.1) se fait au contrôle d'un cluster enfant ou d'un plan
        out.append(cc.finding(rg.VM_NETWORK_WARNING, "warn", registry=spec.get("name"), endpoint=addr,
                              network="", reason="at-use", detail=""))
    if facts.get("name_taken"):
        out.append(cc.finding("registry-name-taken", "block", registry=spec.get("name")))
    if not facts.get("bundle"):
        out.append(cc.finding("registry-images-missing", "block"))
    if facts.get("tools_missing"):
        out.append(cc.finding("registry-tools-missing", "block", tools=", ".join(facts["tools_missing"])))
    klass = spec.get("storage_class")
    replicas = (facts.get("class_replicas") or {}).get(klass)
    lh = facts.get("longhorn")
    if replicas is None:
        out.append(cc.finding("registry-storage-class-unknown", "block", storage_class=klass))
    elif lh is not None:
        import longhorn_room as lr
        nodes = lr.storage_room(lh.get("nodes") or [], [], lh.get("over", 200.0),
                                lh.get("minimal", 25.0))["schedulable_nodes"]
        if replicas > nodes:
            out.append(cc.finding("registry-replicas-nodes", "block", storage_class=klass, replicas=replicas,
                                  nodes=nodes))
        else:
            room = lr.fits(lh.get("nodes") or [], lh.get("over", 200.0), lh.get("minimal", 25.0),
                           [{"name": f"{spec.get('name')}-data", "size": int(spec.get("size_gib") or 0) << 30,
                             "replicas": replicas, "class": klass}])
            if not room["fits"]:
                out.append(cc.finding("registry-room-short", "block", size_gib=spec.get("size_gib"),
                                      storage_class=klass, replicas=replicas))
    return out


# ---------------------------------------------------------------------------
# Certificats et comptes (outils externes, runner injectable)
# ---------------------------------------------------------------------------
def _run(runner, argv, **kw):
    p = (runner or subprocess.run)(argv, capture_output=True, text=True, timeout=60, **kw)
    if p.returncode != 0:
        # le nom de l'outil seulement : sa sortie ne sert pas et l'entrée porte un secret
        raise RuntimeError(f"{Path(argv[0]).name} {argv[1] if len(argv) > 1 else ''} failed")
    return p.stdout


def make_tls(work, address, dns_name=None, name="registry", runner=None):
    """CA de 10 ans et certificat serveur de 2 ans (SAN adresse et nom),
    extensions exigées par la vérification stricte de Python 3.13 (essais
    préalables) ; clés dans `work` (0700, à effacer par l'appelant).
    Rend {"crt", "key", "ca"} en PEM."""
    work = Path(work)
    old = os.umask(0o077)
    try:
        _run(runner, ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                      "-keyout", str(work / "ca.key"), "-out", str(work / "ca.crt"), "-days", str(CA_DAYS),
                      "-subj", f"/CN=harvester-ops registry CA {name}",
                      "-addext", "basicConstraints=critical,CA:TRUE",
                      "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                      "-addext", "subjectKeyIdentifier=hash"])
        _run(runner, ["openssl", "req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                      "-keyout", str(work / "tls.key"), "-out", str(work / "tls.csr"), "-subj", f"/CN={address}"])
        san = f"IP:{address}" + (f",DNS:{dns_name}" if dns_name else "")
        (work / "ext.cnf").write_text(f"subjectAltName={san}\nbasicConstraints=critical,CA:FALSE\n"
                                      "keyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n"
                                      "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n")
        _run(runner, ["openssl", "x509", "-req", "-in", str(work / "tls.csr"), "-CA", str(work / "ca.crt"),
                      "-CAkey", str(work / "ca.key"), "-CAcreateserial", "-out", str(work / "tls.crt"),
                      "-days", str(CERT_DAYS), "-extfile", str(work / "ext.cnf")])
    finally:
        os.umask(old)
    return {"crt": (work / "tls.crt").read_text(), "key": (work / "tls.key").read_text(),
            "ca": (work / "ca.crt").read_text()}


def make_htpasswd(user, password, runner=None, tool=None):
    """Ligne htpasswd bcrypt : `htpasswd -niB <compte>`, mot de passe sur
    l'entrée standard (point 183), jamais en argument."""
    line = _run(runner, [tool or htpasswd_tool() or "htpasswd", "-niB", user], input=f"{password}\n").strip()
    if not line.startswith(f"{user}:$2"):
        raise RuntimeError("htpasswd did not produce a bcrypt entry")
    return line + "\n"
