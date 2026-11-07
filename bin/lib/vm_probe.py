"""harvester-ops : joignabilité d'un registre depuis le réseau des VMs (v2.6.1).

Point 192 du plan 2.6.0 (décision 198 : promis pour la 2.6.1). La console et
le cluster Harvester joignent un registre ; les VMs d'un cluster enfant, sur
leur réseau (NetworkAttachmentDefinition de Multus), peuvent ne pas le
joindre (VLAN sans route, pare-feu). Une pod brève, attachée à ce réseau, en
juge comme une VM :

- une adresse : celle que l'IPAM du réseau a posée sur `net1`, sinon un bail
  DHCP (`udhcpc` de bci-busybox, rendu à la sortie par `-R`) ;
- par point d'accès : nom résolu par le DNS reçu du réseau (si c'est un nom),
  route par `net1`, connexion TCP, poignée de main TLS (`ssl_client` sur la
  connexion de `nc`, le nom en SNI, une réponse HTTP reçue, 401 compris ; la
  CA est vérifiée par la console, busybox ne le fait pas).

Moindre privilège, comme l'essai préalable qui l'a prouvé (05/11/2026,
harvlab2) : namespace dédié `harvester-ops-probe`, capacités `NET_ADMIN`
(adresse et routes) et `NET_RAW` (socket DHCP) seulement, tout le reste
retiré, aucun jeton de compte de service, système de fichiers en lecture
seule sauf un `/tmp` en mémoire. Root dans le conteneur : des capacités
ajoutées à un utilisateur non root ne sont pas effectives (containerd ne
pose pas de capacités ambiantes).

Nettoyage : Job à `activeDeadlineSeconds` et `ttlSecondsAfterFinished`
(le Job et sa pod partent seuls même si la console meurt), et suppression
en cascade dans `finally`.

Image : `probe` du paquet interne du registre (scripts/registry-bundle.json),
épinglée par empreinte, importée sur les nœuds avec le registre d'amorçage.

Plan : docs/design/2026-11-05-artefacts-registre-260-plan.md (point 192).
"""

import fcntl
import ipaddress
import json
import os
import re
import secrets
import time
from pathlib import Path

NAMESPACE = "harvester-ops-probe"
LABEL = "harvester-ops.io/probe"
MANAGED = "harvester-ops.io/managed"
NETWORKS_ANNOTATION = "k8s.v1.cni.cncf.io/networks"
CLUSTER_NETWORK_LABEL = "network.harvesterhci.io/clusternetwork"   # étiquette des NAD de Harvester
NODE_NETWORK_PREFIX = "network.harvesterhci.io/"                   # nœuds : <préfixe><réseau de cluster>=true
CAPABILITIES = ["NET_ADMIN", "NET_RAW"]
INTERFACE = "net1"
DHCP_WAIT = 20                        # secondes pour obtenir un bail
TTL_AFTER = 60                        # le Job fini disparaît seul
CACHE_SECONDS = 300                   # contrôles d'une fenêtre : une pod par réseau et par 5 min
POLL = float(os.environ.get("HARVESTER_OPS_VM_PROBE_POLL", "2"))
CACHE_FILE = "vm-probe-cache.json"

# Raisons d'une sonde qui n'a pas pu juger (le constat reste « non vérifié »)
REASONS = ("no-network", "at-use", "network-missing", "forbidden", "image-missing", "no-address",
           "not-started", "timeout", "ipam-incomplete")
# Étapes que la sonde ne peut pas juger : adresse posée par l'IPAM du réseau
# sans passerelle ni DNS (kube-ovn...), alors qu'une VM en recevrait peut-être
UNKNOWN = ("nodns", "nogw")
# Étape qui a échoué pour un point d'accès
FAILED = ("dns", "route", "tcp", "tls")

HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


def timeout_default():
    try:
        return max(30, int(os.environ.get("HARVESTER_OPS_VM_PROBE_TIMEOUT", "90")))
    except ValueError:
        return 90


def parse_endpoint(endpoint):
    """(hôte, port) d'un point d'accès `hôte[:port]` (443 par défaut) ;
    ValueError sinon. IPv4 ou nom ; IPv6 refusée (pas de sonde IPv6)."""
    ep = str(endpoint or "").strip()
    if ep.startswith("https://"):
        ep = ep[len("https://"):]
    ep = ep.split("/", 1)[0]
    host, sep, port = ep.rpartition(":")
    if not sep:
        host, port = ep, "443"
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"{endpoint}: invalid port")
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        if ":" in host or not HOST_RE.match(host):
            raise ValueError(f"{endpoint}: an IPv4 address or a host name") from None
    return host, int(port)


def network_ref(network):
    """(namespace, nom) d'un réseau `namespace/nom` ; ValueError sinon."""
    ns, sep, name = str(network or "").partition("/")
    if not sep or not ns or not name or "/" in name:
        raise ValueError(f"{network}: expected namespace/name")
    return ns, name


def node_selector(nad):
    """Nœuds qui portent le réseau de cluster du NAD (étiquette posée par
    Harvester), vide pour un NAD sans réseau de cluster (kube-ovn)."""
    cn = (((nad or {}).get("metadata") or {}).get("labels") or {}).get(CLUSTER_NETWORK_LABEL)
    return {f"{NODE_NETWORK_PREFIX}{cn}": "true"} if cn else {}


# Script de la pod : sh de busybox. Une ligne par résultat, lue dans le
# journal : `LEASE|<adresse>|dhcp|ipam`, `NOLEASE|<raison>`,
# `T|<hôte>|<port>|dns=..|route=..|tcp=..|tls=..` (ok, fail, skip).
SCRIPT = r"""
set -u
IF=net1
P=
# arrêt de la pod (délai, suppression) : udhcpc rend son bail (-R) ; sh en
# PID 1 ignore SIGTERM sans ce piège
trap '[ -n "$P" ] && kill "$P" 2>/dev/null; sleep 1; exit 0' TERM INT
mkdir -p /tmp/p
cat > /tmp/p/dhcp.sh <<'EOF'
#!/bin/sh
case "$1" in
  bound|renew)
    ip addr add "$ip/${mask:-24}" dev "$interface" 2>/dev/null
    echo "$ip" > /tmp/p/ip
    echo "${router:-}" | cut -d' ' -f1 > /tmp/p/router
    echo "${dns:-}" | cut -d' ' -f1 > /tmp/p/dns ;;
esac
exit 0
EOF
chmod 700 /tmp/p/dhcp.sh
# poignée de main TLS par ssl_client sur la connexion ouverte par nc (fd 0) :
# le nom part en SNI (wget n'en enverrait pas avec une adresse, et un
# serveur derrière un mandataire répond alors par un autre certificat)
cat > /tmp/p/tls.sh <<'EOF'
#!/bin/sh
exec 3<&0
if [ -n "$TLS_SNI" ]; then set -- -n "$TLS_SNI"; else set --; fi
printf 'GET /v2/ HTTP/1.0\r\nHost: %s\r\nConnection: close\r\n\r\n' "$TLS_HOST" \
  | timeout 10 ssl_client -s 3 "$@" > /tmp/p/resp 2>/tmp/p/err
EOF
chmod 700 /tmp/p/tls.sh
i=0
while ! ip link show "$IF" >/dev/null 2>&1; do
  i=$((i+1)); [ "$i" -gt 10 ] && { echo "NOLEASE|no-interface"; exit 0; }; sleep 1
done
ip link set "$IF" up
IPAM=
A=$(ip -4 -o addr show dev "$IF" | awk '{print $4}' | head -n1)
if [ -n "$A" ]; then
  echo "LEASE|${A%/*}|ipam"
  IPAM=1
  ip route show dev "$IF" | awk '/via/{print $3; exit}' > /tmp/p/router
  : > /tmp/p/dns
else
  udhcpc -f -i "$IF" -R -n -t 6 -T 3 -s /tmp/p/dhcp.sh -x hostname:harvester-ops-probe -x lease:600 \
    > /tmp/p/udhcpc.log 2>&1 &
  P=$!
  i=0
  while [ ! -s /tmp/p/ip ]; do
    i=$((i+1)); [ "$i" -gt "$DHCP_WAIT" ] && break; sleep 1
  done
  if [ ! -s /tmp/p/ip ]; then
    echo "NOLEASE|no-address"; kill "$P" 2>/dev/null; exit 0
  fi
  echo "LEASE|$(cat /tmp/p/ip)|dhcp"
fi
R=$(cat /tmp/p/router 2>/dev/null)
D=$(cat /tmp/p/dns 2>/dev/null)
for T in $TARGETS; do
  H=${T%:*}; PORT=${T##*:}
  dns=skip; route=fail; tcp=skip; tls=skip
  if echo "$H" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
    IP=$H
  else
    IP=
    if [ -n "$D" ]; then
      IP=$(timeout 6 nslookup "$H" "$D" 2>/dev/null | awk '/^Name:/{n=1} n&&/^Address/{sub(/^Address[^:]*:[ \t]*/,""); split($0,a," "); print a[1]; exit}')
    elif [ -n "$IPAM" ]; then
      echo "T|$H|$PORT|dns=nodns|route=skip|tcp=skip|tls=skip"; continue
    fi
    if [ -n "$IP" ]; then dns=ok; else dns=fail; echo "T|$H|$PORT|dns=$dns|route=skip|tcp=skip|tls=skip"; continue; fi
  fi
  if ip route get "$IP" 2>/dev/null | grep -q "dev $IF"; then
    route=ok
  elif [ -n "$R" ] && ip route replace "$IP/32" via "$R" dev "$IF" 2>/dev/null; then
    route=ok
  elif [ -z "$R" ] && [ -n "$IPAM" ]; then
    route=nogw
  fi
  if [ "$route" = ok ]; then
    if timeout 8 nc -z -w 5 "$IP" "$PORT" </dev/null >/dev/null 2>&1; then
      tcp=ok
      if [ "$dns" = ok ]; then TLS_SNI=$H; else TLS_SNI=; fi
      TLS_HOST=$H; export TLS_SNI TLS_HOST
      rm -f /tmp/p/resp
      timeout 14 nc -w 5 "$IP" "$PORT" -e /tmp/p/tls.sh </dev/null >/dev/null 2>&1
      if grep -q '^HTTP/1' /tmp/p/resp 2>/dev/null; then tls=ok; else tls=fail; fi
    else
      tcp=fail
    fi
  fi
  echo "T|$H|$PORT|dns=$dns|route=$route|tcp=$tcp|tls=$tls"
done
[ -n "$P" ] && { kill "$P" 2>/dev/null; sleep 1; }
exit 0
"""


def job_manifest(name, network, nad, image, targets, deadline):
    """Job de la sonde. `targets` : [(hôte, port)]."""
    ns, nad_name = network_ref(network)
    labels = {LABEL: "vm-network", MANAGED: "true"}
    pod = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        # le temps de rendre le bail (udhcpc -R) à l'arrêt
        "terminationGracePeriodSeconds": 5,
        "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{
            "name": "probe", "image": image, "imagePullPolicy": "IfNotPresent",
            "command": ["/bin/sh", "-c", SCRIPT],
            "env": [{"name": "TARGETS", "value": " ".join(f"{h}:{p}" for h, p in targets)},
                    {"name": "DHCP_WAIT", "value": str(DHCP_WAIT)}],
            "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"memory": "32Mi"}},
            "securityContext": {"runAsUser": 0, "runAsNonRoot": False, "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True, "privileged": False,
                                "capabilities": {"drop": ["ALL"], "add": list(CAPABILITIES)}},
            "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}]}],
        "volumes": [{"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "1Mi"}}]}
    sel = node_selector(nad)
    if sel:
        pod["nodeSelector"] = sel
    return {"apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
            "spec": {"backoffLimit": 0, "activeDeadlineSeconds": int(deadline),
                     "ttlSecondsAfterFinished": TTL_AFTER,
                     "template": {"metadata": {"labels": labels, "annotations": {NETWORKS_ANNOTATION: json.dumps(
                         [{"namespace": ns, "name": nad_name, "interface": INTERFACE}])}},
                                  "spec": pod}}}


def namespace_manifest():
    return {"apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": NAMESPACE, "labels": {MANAGED: "true"}}}


def parse_log(text, targets):
    """{"address", "how", "reason", "results": {"hôte:port": {...}}} du
    journal de la pod."""
    out = {"address": None, "how": None, "reason": None, "results": {}}
    for line in (text or "").splitlines():
        parts = line.strip().split("|")
        if parts[0] == "LEASE" and len(parts) == 3:
            out["address"], out["how"] = parts[1], parts[2]
        elif parts[0] == "NOLEASE" and len(parts) == 2:
            out["reason"] = "not-started" if parts[1] == "no-interface" else "no-address"
        elif parts[0] == "T" and len(parts) == 7:
            steps = dict(p.split("=", 1) for p in parts[3:] if "=" in p)
            out["results"][f"{parts[1]}:{parts[2]}"] = steps
    for h, p in targets:
        r = out["results"].get(f"{h}:{p}")
        if r is not None:
            r["unknown"] = any(v in UNKNOWN for v in r.values())
            failed = next((s for s in FAILED if r.get(s) == "fail"), None)
            r["ok"] = failed is None and not r["unknown"] and r.get("tls") == "ok"
            r["failed"] = failed or (None if r["ok"] or r["unknown"] else "tcp")
    return out


def _waiting(pod):
    """(raison d'attente du conteneur, message) d'une pod qui n'a pas démarré."""
    for cs in ((pod.get("status") or {}).get("containerStatuses") or []):
        w = (cs.get("state") or {}).get("waiting") or {}
        if w.get("reason"):
            return w["reason"], (w.get("message") or "")[:200]
    return None, ""


IMAGE_REASONS = ("ErrImagePull", "ImagePullBackOff", "ErrImageNeverPull", "InvalidImageName")


def _sandbox_error(kube, pod_name):
    """Message de l'événement FailedCreatePodSandBox (Multus) de la pod."""
    try:
        for ev in kube.list("events", NAMESPACE):
            io = ev.get("involvedObject") or {}
            if io.get("name") == pod_name and ev.get("reason") in ("FailedCreatePodSandBox", "FailedScheduling"):
                return f"{ev.get('reason')}: {(ev.get('message') or '')[:200]}"
    except Exception:        # noqa: BLE001 : un message de plus, jamais un échec de la sonde
        return ""
    return ""


def _unchecked(network, reason, detail=""):
    return {"network": network, "status": "unchecked", "reason": reason, "detail": detail[:300],
            "address": None, "results": {}}


def run(kube, network, endpoints, image, timeout=None, clock=time.monotonic, sleep=time.sleep):
    """Sonde `endpoints` (["hôte[:port]"]) depuis `network` (namespace/nom).
    Rend {"network", "status": "probed"|"unchecked", "reason", "detail",
    "address", "how", "results": {point d'accès: {"ok", "failed", "dns",
    "route", "tcp", "tls"}}}. Jamais d'exception pour une sonde impossible :
    le statut `unchecked` porte la raison (REASONS)."""
    from kube import KubeError
    timeout = timeout or timeout_default()
    if not network:
        return _unchecked(network, "no-network")
    try:
        targets = [parse_endpoint(ep) for ep in endpoints]
        ns, nad_name = network_ref(network)
    except ValueError as e:
        return _unchecked(network, "network-missing", str(e))
    if not image:
        return _unchecked(network, "image-missing", "the probe image is not in this installation")
    try:
        nad = kube.get("network-attachment-definitions.k8s.cni.cncf.io", ns, nad_name)
    except KubeError as e:
        return _unchecked(network, "forbidden" if "forbidden" in str(e).lower() else "network-missing", str(e))
    if nad is None:
        return _unchecked(network, "network-missing", f"{network} not found")
    name = f"probe-{secrets.token_hex(4)}"
    try:
        kube.apply([namespace_manifest()])
        kube.create(job_manifest(name, network, nad, image, targets, timeout + 30))
    except KubeError as e:
        reason = "forbidden" if "forbidden" in str(e).lower() else "not-started"
        return _unchecked(network, reason, str(e))
    try:
        end = clock() + timeout
        pod = None
        while True:
            pods = kube.list("pods", NAMESPACE, selector=f"job-name={name}")
            pod = pods[0] if pods else None
            phase = ((pod or {}).get("status") or {}).get("phase")
            if phase in ("Succeeded", "Failed"):
                break
            reason, msg = _waiting(pod or {})
            if reason in IMAGE_REASONS:
                return _unchecked(network, "image-missing", f"{reason}: {msg}")
            if clock() >= end:
                if phase == "Running":
                    return _unchecked(network, "timeout", f"no result after {timeout} s")
                detail = _sandbox_error(kube, ((pod or {}).get("metadata") or {}).get("name")) if pod else ""
                return _unchecked(network, "not-started", detail or f"{reason or phase or 'no pod'} after {timeout} s")
            sleep(POLL)
        text = kube.logs(NAMESPACE, pod["metadata"]["name"], tail=200)
        got = parse_log(text, targets)
        if got["address"] is None:
            return _unchecked(network, got["reason"] or "no-address",
                              f"no address on {network} (no DHCP lease within {DHCP_WAIT} s, no IPAM)"
                              if got["reason"] != "not-started" else f"no interface {INTERFACE} in the pod")
        results = {}
        for ep, (h, p) in zip(endpoints, targets):
            r = got["results"].get(f"{h}:{p}")
            results[ep] = r if r is not None else {"ok": False, "failed": "tcp"}
        return {"network": network, "status": "probed", "reason": None, "detail": "",
                "address": got["address"], "how": got["how"], "results": results}
    except KubeError as e:
        return _unchecked(network, "not-started", str(e))
    finally:
        try:
            kube.delete("jobs", NAMESPACE, name, cascade="background")
        except KubeError:
            pass                                    # ttlSecondsAfterFinished le retire de toute façon


def _key(cluster, network, endpoints):
    return "|".join([str(cluster), str(network)] + sorted(endpoints))


def _lock_path(path, key):
    import hashlib
    return path.with_name(f".{path.name}.{hashlib.sha256(key.encode()).hexdigest()[:16]}.lock")


def cached_run(state, cluster, kube, network, endpoints, image, fresh=False, now=time.time, runner=None):
    """`run` avec un cache de CACHE_SECONDS dans le répertoire d'état : les
    contrôles d'une fenêtre (relancés à chaque saisie) ne posent pas une
    pod chacun. Un verrou sérialise les sondes : la seconde demande de la
    même clé lit le résultat de la première. `fresh` : sonde neuve (geste
    explicite, bouton Vérifier). Un verrou par clé (cluster, réseau,
    adresses) : des sondes sans rapport ne s'attendent pas. Un résultat
    « non vérifié » n'est pas gardé : il dépend souvent des droits de la
    personne (revue 2.6.1)."""
    runner = runner or run
    path = Path(state) / CACHE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    key = _key(cluster, network, endpoints)
    def read():
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}
    with open(_lock_path(path, key), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        hit = read().get(key)
        if not fresh and hit and now() - hit.get("at", 0) < CACHE_SECONDS:
            return dict(hit["result"], cached=True)
        res = runner(kube, network, endpoints, image)
        if res.get("status") != "probed":
            return res
        with open(path.with_name(f".{path.name}.lock"), "a+") as wlock:   # écriture du fichier commun
            fcntl.flock(wlock, fcntl.LOCK_EX)
            cache = {k: v for k, v in read().items() if now() - v.get("at", 0) < CACHE_SECONDS}
            cache[key] = {"at": now(), "result": res}
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text(json.dumps(cache))
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        return res


def probe_image(state):
    """Référence épinglée de l'image de la sonde (paquet interne), None si
    le paquet manque ou si ses sommes sont fausses."""
    import registry_seed as rs
    b = rs.bundle(state)
    return ((b or {}).get("images") or {}).get("probe")
