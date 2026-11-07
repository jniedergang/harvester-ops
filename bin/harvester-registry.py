#!/usr/bin/env python3
"""Registres des clusters enfants : déclarer, rattacher, vérifier.

    harvester-registry declare NOM --file r.yaml|- [--revision N]
    harvester-registry attach  NOM --cluster C [--kubeconfig K]     (identifiants JSON sur l'entrée standard)
    harvester-registry check   NOM --cluster C [--kubeconfig K] [--vm-network NS/NOM]
    harvester-registry list
    harvester-registry show    NOM
    harvester-registry forget  NOM [--kubeconfig-for C=K ...]
    harvester-registry deploy  NOM --cluster C --address IP --size-gib N --storage-class S
                               [--dns-name N] [--mirrors h1,h2] [--cert F --key F --ca F]
                               [--spec F] [--timeout S] [--dry-run]
    harvester-registry status  NOM --cluster C [--kubeconfig K]
    harvester-registry delete  NOM --cluster C [--kubeconfig K] [--delete-data]
    harvester-registry leftovers   --cluster C [--kubeconfig K]
    harvester-registry write-access [NOM] --cluster C [--kubeconfig K]
    harvester-registry settings [--write-sources A,B | --clear]
    harvester-registry renew-cert NOM --cluster C [--days 730] [--new-ca] [--dry-run]
    harvester-registry harvester-nodes NOM --cluster C [--remove] [--dry-run]

Une déclaration (bin/lib/registries.py) ne porte aucun secret : adresse,
CA publique, usage (images, charts), registres d'origine miroités. Les
identifiants sont saisis au rattachement, sur l'entrée standard
(`{"push": {"username", "password"}, "pull": {...}}`), jamais en argument,
et posés en Secrets dans `harvester-ops-registry` du cluster Harvester.

`deploy` pose le registre d'amorçage (bin/lib/registry_seed.py) : constats,
certificat (CA générée par openssl, ou celui du client : fichiers, ou
`--spec` JSON privé {cert, key, ca}), comptes `push` et `pull` (mots de
passe aléatoires, htpasswd par l'entrée standard), images du paquet interne
importées sur chaque nœud par ctr, objets appliqués avec l'identité de la
personne, attente de `/v2/` (401 sans compte, puis 200 avec le compte de
lecture), déclaration et Secrets posés. `delete` garde le volume sauf
`--delete-data` (point 193).

2.6.1 : `check --vm-network` sonde le registre depuis une pod brève du
réseau des VMs (bin/lib/vm_probe.py) ; le port d'écriture 5443 des
registres d'amorçage est servi par un second Service limité aux réseaux de
la console (`settings --write-sources`, lu à chaque geste), et
`write-access` y porte les registres déjà posés.

Progression sur stderr au format STEP_EVENT|<étape>|<statut>|<message> ;
résultat en JSON sur stdout. Codes : 0 succès, 1 échec, 2 refus.

Voir docs/design/2026-11-05-artefacts-registre-260-plan.md (tâches 6 et 7).
"""

import argparse
import base64
import json
import os
import secrets
import shutil
import socket
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import capi_cluster as cc  # noqa: E402
import cluster_decl  # noqa: E402
import deploy_plans as dp  # noqa: E402
import registries as rg  # noqa: E402
import registry_seed as rs  # noqa: E402
import vm_probe  # noqa: E402
import work_dir  # noqa: E402
from kube import Kube, KubeError, cluster_config  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_REFUSED = 0, 1, 2
POLL = float(os.environ.get("HARVESTER_OPS_REGISTRY_POLL", "5"))


def step(sid, status, msg=""):
    clean = " ".join(str(msg).split())
    sys.stderr.write(f"STEP_EVENT|{sid}|{status}|{clean}\n")
    sys.stderr.flush()


def out(obj):
    print(json.dumps(obj, indent=2))


def state():
    return cluster_decl.state_dir()


def kube_from(args, need_entry=False):
    """`--kubeconfig` l'emporte (la console passe celui qu'elle a préparé,
    à l'identité de la personne) ; l'entrée de configuration de `--cluster`
    (nœuds et SSH) n'est lue que si elle sert (`need_entry`) ou s'il n'y a
    pas de kubeconfig."""
    entry = cluster_config(args.cluster) if args.cluster and (need_entry or not args.kubeconfig) else None
    kc = args.kubeconfig or (entry or {}).get("kubeconfig")
    if not kc:
        raise SystemExit("give --cluster (with a kubeconfig in the configuration) or --kubeconfig")
    return Kube(kc), entry


def _decl(name):
    d = rg.load(state(), name)
    if d is None:
        step("registry", "error", f"registry-unknown: {name}")
        out({"findings": [cc.finding("registry-unknown", "block", registry=name)], "blocked": True})
    return d


def _read_doc(path):
    text = sys.stdin.read() if path == "-" else Path(path).read_text()
    try:
        return json.loads(text)
    except ValueError:
        import yaml
        return yaml.safe_load(text)


def cmd_declare(args):
    try:
        doc = _read_doc(args.file)
        if isinstance(doc, dict) and doc.get("kind") == "bootstrap":
            raise dp.ProfileError([("kind", "a bootstrap registry is declared by deploy, not by hand")])
        saved = rg.save(state(), args.name, doc, args.revision)
    except dp.PlanConflict as e:
        step("declare", "error", f"the declaration changed in the meantime (revision {e.current_revision})")
        out({"error": "conflict", "current_revision": e.current_revision})
        return EXIT_REFUSED
    except dp.ProfileError as e:
        step("declare", "error", "; ".join(f"{w}: {r}" for w, r in e.errors))
        out({"error": "invalid", "errors": [{"field": w, "reason": r} for w, r in e.errors]})
        return EXIT_REFUSED
    except (OSError, ValueError) as e:
        step("declare", "error", str(e)[:300])
        return EXIT_REFUSED
    step("declare", "done", f"{args.name} (revision {saved['revision']})")
    out(saved)
    return EXIT_OK


def cmd_attach(args):
    d = _decl(args.name)
    if d is None:
        return EXIT_REFUSED
    if d.get("kind") == "bootstrap":
        step("attach", "error", "registry-bootstrap: the accounts of a bootstrap registry are generated by the console")
        return EXIT_REFUSED
    try:
        text = sys.stdin.read() if args.spec == "-" else Path(args.spec).read_text()
        creds = rg.check_creds(json.loads(text or "null"))
    except ValueError as e:
        step("attach", "error", str(e))
        return EXIT_REFUSED
    kube, _ = kube_from(args)
    step("attach", "running", f"{args.name} on {args.cluster}: secrets in {rg.NAMESPACE}")
    try:
        res = rg.attach(kube, d, creds)
        rg.update(state(), args.name, lambda x: x.__setitem__(
            "clusters", list(dict.fromkeys((x.get("clusters") or []) + [args.cluster]))))
    except (KubeError, dp.PlanConflict, KeyError) as e:
        step("attach", "error", str(e)[:300])
        return EXIT_FAIL
    for f in res["findings"]:
        step("attach", "running", f"warning {f['code']}: the read account is the write account; "
                                  "it will be written on every child node")
    step("attach", "done", f"{', '.join(res['secrets'])}")
    out(res)
    return EXIT_OK


def write_open(kube, d, cluster):
    """Vrai si le port d'écriture d'un registre d'amorçage s'ouvre à toute
    adresse (Service non porté au réglage, ou réglage vide). Seulement sur
    le cluster qui le porte, et s'il y a bien un Service (revue 2.6.1)."""
    if (d or {}).get("kind") != "bootstrap" or d.get("cluster") != cluster:
        return False
    main = kube.get("services", rs.NAMESPACE, d["name"])
    if main is None:
        return False
    return not rs.write_access(main, kube.get("services", rs.NAMESPACE, rs.write_service_name(d["name"])))["sources"]


def cmd_check(args):
    d = rg.load(state(), args.name)
    findings = []
    if d is not None:
        kube, _ = kube_from(args)
        try:
            facts = rg.facts(kube, d, args.cluster)
            if args.vm_network and not cc.NS_NAME_RE.match(args.vm_network):
                step("vm-network", "error", "--vm-network: expected namespace/name")
                return EXIT_REFUSED
            if facts.get("attached"):
                if args.vm_network:
                    step("vm-network", "running", f"probe pod on {args.vm_network}")
                    facts["vm_probe"] = vm_probe.cached_run(state(), args.cluster, kube, args.vm_network,
                                                            [d["endpoint"]], vm_probe.probe_image(state()),
                                                            fresh=True)
                facts["write_open"] = write_open(kube, d, args.cluster)
            findings = rg.check(d, facts)
        except KubeError as e:
            findings = [cc.finding("cluster-unreachable", "block", error=str(e)[:200])]
    else:
        findings = rg.check(None, {}, args.name)
    blocked = bool(cc.blocking(findings))
    out({"registry": args.name, "cluster": args.cluster, "findings": findings, "blocked": blocked})
    return EXIT_REFUSED if blocked else EXIT_OK


def cmd_list(args):
    out(rg.list_decls(state()))
    return EXIT_OK


def cmd_show(args):
    d = rg.load(state(), args.name)
    if d is None:
        step("show", "error", f"registry-unknown: {args.name}")
        return EXIT_REFUSED
    out(d)
    return EXIT_OK


def users(name, clusters, kube_for=None):
    """Qui emploie un registre (revue m-15, tâche 11) : plans et modèles de
    cluster enregistrés, puis clusters enfants et services sur chaque
    cluster Harvester donné (Secrets étiquetés au nom du registre). Un
    cluster illisible rend la réponse inconnue : KubeError."""
    out = dp.registry_users(state(), name)
    for c in clusters:
        kube = kube_for(c) if kube_for else Kube((cluster_config(c) or {}).get("kubeconfig"))
        out += [f"cluster:{c}/{ns}" for ns in rg.child_users(kube, name)]
    return out


def _cluster_kube(c):
    try:
        entry = cluster_config(c)
    except SystemExit:
        return None
    if not (entry or {}).get("kubeconfig"):
        return None
    return Kube(entry["kubeconfig"])


def _forget_kubes(args, clusters):
    """({cluster: Kube}, [clusters sans accès]). La console passe un
    kubeconfig par cluster, à l'identité de la personne
    (`--kubeconfig-for C=chemin`) ; sinon la configuration."""
    given = dict(a.partition("=")[::2] for a in getattr(args, "kubeconfig_for", None) or [])
    kubes, missing = {}, []
    for c in clusters:
        k = Kube(given[c]) if given.get(c) else (None if given else _cluster_kube(c))
        if k is None:
            missing.append(c)
        else:
            kubes[c] = k
    return kubes, missing


def cmd_forget(args):
    d = rg.load(state(), args.name)
    used, kubes = [], {}
    client = d is not None and d.get("kind") != "bootstrap"
    if client:
        try:
            # un cluster retiré de la configuration n'emploie plus rien d'ici
            kubes, missing = _forget_kubes(args, d.get("clusters") or [])
            for c in missing:
                # revue finale 2.6.0 (m-10) : dit, jamais sauté en silence
                step("detach", "running", f"{c} is not in the console: the accounts of {args.name} "
                                          f"in {rg.NAMESPACE} there are left as they are")
            used = users(args.name, list(kubes), kubes.get)
        except (KubeError, SystemExit) as e:
            step("forget", "error", f"registry-usage-unknown: {str(e)[:200]}")
            return EXIT_REFUSED
    if used:
        step("forget", "error", f"registry-in-use: {', '.join(sorted(set(used)))}")
        return EXIT_REFUSED
    # 2.6.0, vu en réel : le compte d'écriture du client ne reste pas dans
    # un Secret du cluster Harvester une fois la déclaration oubliée ; une
    # étape par Secret retiré (revue finale I-3 : un refus en cours de route
    # laisse la trace de ce qui est déjà parti)
    for c, kube in kubes.items():
        try:
            gone = rg.detach(kube, args.name, on_gone=lambda n, c=c: step(
                "detach", "done", f"secret {rg.NAMESPACE}/{n} removed from {c}"))
        except KubeError as e:
            step("detach", "error", f"accounts of {args.name} kept on {c}: {str(e)[:200]}")
            return EXIT_REFUSED
        step("detach", "done", f"accounts removed from {c} ({len(gone)} secret(s))")
    try:
        rg.forget(state(), args.name, used)
    except KeyError:
        step("forget", "error", f"registry-unknown: {args.name}")
        return EXIT_REFUSED
    except ValueError as e:
        step("forget", "error", str(e))
        return EXIT_REFUSED
    step("forget", "done", f"{args.name} forgotten")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Registre d'amorçage
# ---------------------------------------------------------------------------
def _answers(address, port=443, timeout=2.0):
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


def _leftover_of(kube, name):
    """Restes d'un déploiement échoué : objets étiquetés au nom du registre
    dans le namespace de la console, sans déclaration (revue finale I-2).
    None si le registre est déclaré ou si rien ne reste."""
    if rg.load(state(), name) is not None:
        return None
    objs, origin, address = [], {}, None
    for kind, n in (("deployments", name), ("services", name), ("services", rs.write_service_name(name)),
                    ("configmaps", f"{name}-config"),
                    ("secrets", f"{name}-tls"), ("secrets", f"{name}-htpasswd"),
                    *[("secrets", x) for x in rg.secret_names(name).values()]):
        o = kube.get(kind, rs.NAMESPACE, n)
        if o is None or (((o.get("metadata") or {}).get("labels") or {}).get(rs.LABEL) != name):
            continue
        objs.append(f"{kind}/{n}")
        if kind == "deployments":
            origin = rs.origin_of(o)
        if kind == "services":
            address = ((o.get("metadata") or {}).get("annotations") or {}).get(rs.VIP_ANNOTATION)
    if not objs:
        return None
    return {"registry": name, "objects": objs, "origin": origin, "address": address}


def deploy_facts(kube, spec, answers=None, origin=None):
    """Ce que registry_seed.check lit du cluster et de l'installation.
    `origin` : celle de la chaîne d'un plan ; les restes d'un déploiement
    échoué de ce même plan ne prennent ni le nom ni l'adresse (sa reprise
    les recouvre, revue finale I-2)."""
    import longhorn_room as lr
    pools = [cc.pool_info(p) for p in kube.list("ippools.loadbalancer.harvesterhci.io")]
    replicas = {}
    for sc in kube.list("storageclasses"):
        if sc.get("provisioner") == "driver.longhorn.io":
            try:
                n = int(((sc.get("parameters") or {}).get("numberOfReplicas")) or 3)
            except (TypeError, ValueError):
                n = 3
            replicas[sc["metadata"]["name"]] = max(1, n)
    try:
        lh_nodes, over, minimal = lr.read_numbers(kube)
        longhorn = {"nodes": lh_nodes, "over": over, "minimal": minimal}
    except KubeError:
        longhorn = None
    wsvc = kube.get("services", rs.NAMESPACE, rs.write_service_name(spec["name"]))
    taken = rg.load(state(), spec["name"]) is not None or \
        kube.get("deployments", rs.NAMESPACE, spec["name"]) is not None or \
        (wsvc is not None and ((wsvc.get("metadata") or {}).get("labels") or {}).get(rs.LABEL) != spec["name"])
    own = None
    if taken and (origin or {}).get("plan"):
        left = _leftover_of(kube, spec["name"])
        if left and left["origin"].get("plan") == origin["plan"]:
            own, taken = left, False
    answering = False if own and own["address"] == spec["address"] else (answers or _answers)(spec["address"])
    return {"pools": pools, "vip": kube.server_host(), "address_answers": answering,
            "longhorn": longhorn, "class_replicas": replicas, "bundle": rs.bundle(state()) is not None,
            "tools_missing": rs.tools_missing(), "name_taken": taken}


def _client_tls(args):
    """Certificat du client : fichiers (--cert/--key/--ca) ou demande privée
    (--spec, JSON {cert, key, ca}) ; None pour une CA générée."""
    if args.spec:
        doc = json.loads(Path(args.spec).read_text() or "{}")
        if doc.get("cert") or doc.get("key"):
            if not (doc.get("cert") and doc.get("key") and doc.get("ca")):
                raise ValueError("a client certificate needs cert, key and ca")
            return {"crt": doc["cert"], "key": doc["key"], "ca": doc["ca"]}
        return None
    if args.cert or args.key or args.ca:
        if not (args.cert and args.key and args.ca):
            raise ValueError("--cert, --key and --ca go together")
        return {"crt": Path(args.cert).read_text(), "key": Path(args.key).read_text(),
                "ca": Path(args.ca).read_text()}
    return None


def wait_ready(decl, pull, timeout, registry_cls=None, clock=time.monotonic, sleep=time.sleep):
    """`/v2/` : 401 sans compte (authentification active), puis 200 avec le
    compte de lecture. Rend la dernière raison d'attente, None si prêt."""
    import oci_push as op
    cls = registry_cls or op.Registry
    end, why = clock() + timeout, "no answer yet"
    while True:
        try:
            anon = cls(decl["endpoint"], None, ca_pem=decl["ca_pem"], timeout=10)
            anon.request("GET", "/v2/", "")
            why = "the registry answered without authentication"
        except op.RegistryError as e:
            if "authentication required" in str(e):
                reach, _, ok = rg.probe(decl, pull, "pull", registry_cls=cls)
                if ok:
                    return None
                why = "the read account is refused" if reach else "no answer with the read account"
            else:
                why = str(e)[:200]
        if clock() >= end:
            return why
        sleep(POLL)


def cmd_deploy(args):
    try:
        origin = cc.parse_origin(args.origin)
    except ValueError as e:
        step("check", "error", str(e))
        return EXIT_REFUSED
    spec = {"name": args.name, "address": args.address, "size_gib": args.size_gib,
            "storage_class": args.storage_class}
    if args.dns_name:
        spec["dns_name"] = args.dns_name
    if not dp.NAME_RE.match(args.name) or len(args.name) > rg.NAME_MAX or args.name in rg.RESERVED:
        step("check", "error", f"invalid registry name (RFC 1123, {rg.NAME_MAX} characters at most)")
        return EXIT_REFUSED
    if args.name.endswith(rg.WRITE_NAME_SUFFIX):
        # revue 2.6.1 : `<a>-write` est le Service d'écriture du registre `a`
        step("check", "error", f"a bootstrap registry name cannot end with {rg.WRITE_NAME_SUFFIX}")
        return EXIT_REFUSED
    if not 5 <= args.size_gib <= 2048:
        step("check", "error", "size: an integer from 5 to 2048 GiB")
        return EXIT_REFUSED
    if args.dns_name and not rg.HOST_RE.fullmatch(args.dns_name):
        # revue m-4 : écrit tel quel dans l'extension du certificat
        step("check", "error", "--dns-name: a host name")
        return EXIT_REFUSED
    kube, entry = kube_from(args, need_entry=not args.dry_run)
    if not args.dry_run and not (entry or {}).get("nodes"):
        # revue m-5 : les images du registre s'importent sur les nœuds de la
        # configuration ; sans elle, un message plutôt qu'une trace
        step("check", "error", f"{args.cluster}: no nodes in the console configuration (the registry "
                               "images are imported on each node)")
        return EXIT_REFUSED
    step("check", "running", f"{args.name} on {args.cluster} at {args.address}")
    try:
        findings = rs.check(spec, deploy_facts(kube, spec, origin=origin))
    except KubeError as e:
        findings = [cc.finding("cluster-unreachable", "block", error=str(e)[:200])]
    blocked = bool(cc.blocking(findings))
    if args.dry_run or blocked:
        step("check", "error" if blocked else "done",
             ", ".join(f["code"] for f in cc.blocking(findings)) or "no blocking finding")
        out({"registry": args.name, "cluster": args.cluster, "findings": findings, "blocked": blocked})
        return EXIT_REFUSED if blocked else EXIT_OK
    step("check", "done", "no blocking finding")
    try:
        # avant tout geste : un réglage abîmé n'ouvre jamais le port d'écriture
        sources = rg.settings(state())["write_sources"]
    except rg.SettingsError as e:
        step("check", "error", str(e))
        return EXIT_REFUSED
    try:
        client_tls = _client_tls(args)
    except (OSError, ValueError) as e:
        step("tls", "error", str(e))
        return EXIT_REFUSED
    try:
        # 2.7.0 (m11) : espace de travail privé, jamais /tmp (clés de la CA)
        work = work_dir.mkdtemp("hops-registry-")
    except ValueError as e:
        step("tls", "error", str(e))
        return EXIT_REFUSED
    try:
        step("tls", "running", "client certificate" if client_tls else "CA and server certificate")
        tls = client_tls or rs.make_tls(work, args.address, args.dns_name, args.name)
        step("tls", "done", "CA of the console (10 years), certificate of 2 years" if not client_tls
             else "client certificate")
        creds = {r: {"username": r, "password": secrets.token_urlsafe(32)} for r in ("push", "pull")}
        step("accounts", "running", "push (write) and pull (read)")
        push_line = rs.make_htpasswd("push", creds["push"]["password"])
        htp = {"read": push_line + rs.make_htpasswd("pull", creds["pull"]["password"]), "write": push_line}
        step("accounts", "done", "bcrypt entries; the pull account cannot write (read-only instance)")
        decl = rs.declaration(spec, tls["ca"], args.cluster, [m for m in args.mirrors.split(",") if m])
        rg.check_decl(decl)                       # CA du client illisible, miroir mal écrit : avant tout geste
        decl["name"] = args.name
        b = rs.bundle(state())
        spec["image"] = b["images"]["registry"]
        import node_images
        node_images.import_on_nodes(entry, [b["tar"]], step, sid="images")
        step("apply", "running", f"namespace {rs.NAMESPACE}: pod, volume of {args.size_gib} GiB, address "
                                 f"{args.address}")
        # 2.8.0 (point 235) : la clé de la CA de la console est gardée dans
        # le Secret `<registre>-ca-key` (renouvellement du certificat)
        keep = [rs.ca_key_secret(args.name, tls["ca"], tls["ca_key"])] if not client_tls else []
        kube.apply(rs.manifests(spec, tls, htp, origin, write_sources=sources) + rg.attach_manifests(decl, creds)[1:]
                   + keep)
        step("apply", "running", f"port {rs.WRITE_PORT}: " + (f"reserved to {', '.join(sources)}" if sources else
                                 "open to every address (Settings > Registries restricts it to the console)"))
        step("apply", "done", f"{args.name}: deployment, service, volume, secrets")
        step("ready", "running", f"waiting for https://{args.address}/v2/")
        why = wait_ready(decl, creds["pull"], args.timeout)
        if why:
            step("ready", "error", f"not ready after {args.timeout} s: {why}")
            return EXIT_FAIL
        step("ready", "done", "authentication required without an account, read account accepted")
        saved = rg.save(state(), args.name, {k: v for k, v in decl.items() if k != "name"})
        step("declare", "done", f"{args.name} (bootstrap, revision {saved['revision']})")
    except dp.ProfileError as e:
        step("deploy", "error", "; ".join(f"{w}: {r}" for w, r in e.errors))
        return EXIT_REFUSED
    except (KubeError, RuntimeError, OSError, dp.PlanConflict) as e:
        step("deploy", "error", str(e)[:300])
        return EXIT_FAIL
    finally:
        shutil.rmtree(work, ignore_errors=True)
    out({"registry": args.name, "kind": "bootstrap", "created": True, "endpoint": args.address,
         "push_endpoint": decl["push_endpoint"], "cluster": args.cluster})
    return EXIT_OK


def cmd_status(args):
    d = rg.load(state(), args.name)
    kube, _ = kube_from(args)
    try:
        dep = kube.get("deployments", rs.NAMESPACE, args.name)
        pvc = kube.get("persistentvolumeclaims", rs.NAMESPACE, f"{args.name}-data")
        svc = kube.get("services", rs.NAMESPACE, args.name)
        wsvc = kube.get("services", rs.NAMESPACE, rs.write_service_name(args.name))
        present = {r: kube.get("secrets", rs.NAMESPACE, n) is not None for r, n in rg.secret_names(args.name).items()}
        tls = kube.get("secrets", rs.NAMESPACE, f"{args.name}-tls")
        used = None
        vol = ((pvc or {}).get("spec") or {}).get("volumeName")
        if vol:
            lv = kube.get("volumes.longhorn.io", "longhorn-system", vol)
            used = ((lv or {}).get("status") or {}).get("actualSize")
    except KubeError as e:
        step("status", "error", str(e)[:300])
        return EXIT_FAIL
    st = (dep or {}).get("status") or {}
    containers = (((dep or {}).get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or []
    ingress = ((((svc or {}).get("status") or {}).get("loadBalancer") or {}).get("ingress") or [{}])[0]
    repos = None
    if d and dep:
        pull = rg.read_creds(kube, args.name, "pull")
        if pull:
            try:
                import oci_push as op
                _, _, body = op.Registry(d["endpoint"], pull, ca_pem=d.get("ca_pem"), timeout=15).request(
                    "GET", "/v2/_catalog?n=1000", "registry:catalog:*")
                repos = len(json.loads(body).get("repositories") or [])
            except (op.RegistryError, ValueError):
                repos = None
    # seul le certificat public est lu : la clé du Secret ne sort jamais
    crt = ((tls or {}).get("data") or {}).get("tls.crt")
    try:
        not_after = rs.cert_not_after(base64.b64decode(crt).decode()) if crt else None
    except (ValueError, UnicodeDecodeError):
        not_after = None
    findings = rs.cert_findings(args.name, not_after, time.time())
    if d and d.get("kind") == "bootstrap" and dep is not None:
        try:
            has_key = kube.get("secrets", rs.NAMESPACE, f"{args.name}{rs.CA_KEY_SUFFIX}") is not None
        except KubeError:
            has_key = True
        if not has_key:
            # 2.8.0 : posé avant la 2.8.0, sa CA n'est pas gardée
            findings.append(cc.finding("registry-ca-key-missing", "ok", registry=args.name))
        if d.get("ca_next_pem"):
            try:
                pending = ca_pending(kube, d)
            except KubeError:
                pending = [{"where": "?", "reason": "unreadable"}]
            findings.append(cc.finding("registry-ca-transition-pending", "warn", registry=args.name,
                                       clusters=pending_text(pending) or "-"))
    out({"registry": args.name, "declared": d is not None, "present": dep is not None,
         "cert_not_after": not_after, "findings": findings,
         "origin": rs.origin_of(dep) if dep else {},
         "ready": bool(st.get("readyReplicas")), "image": containers[0].get("image") if containers else None,
         "address": ingress.get("ip") or (d or {}).get("address"),
         "volume": {"size": (((pvc or {}).get("spec") or {}).get("resources") or {}).get("requests", {}).get("storage"),
                    "phase": ((pvc or {}).get("status") or {}).get("phase"), "used_bytes": used} if pvc else None,
         "accounts": present, "repositories": repos,
         "write_access": rs.write_access(svc, wsvc) if svc else None})
    return EXIT_OK


def cmd_leftovers(args):
    """Restes de déploiements échoués sur un cluster (revue finale I-2) :
    registres étiquetés par la console dans son namespace, sans
    déclaration ; l'onglet les montre avec Supprimer."""
    kube, _ = kube_from(args)
    try:
        names = {((o.get("metadata") or {}).get("labels") or {}).get(rs.LABEL)
                 for kind in ("deployments", "services", "configmaps")
                 for o in kube.list(kind, rs.NAMESPACE)}
        found = [x for x in (_leftover_of(kube, n) for n in sorted(n for n in names if n)) if x]
    except KubeError as e:
        step("leftovers", "error", str(e)[:300])
        return EXIT_FAIL
    for x in found:
        x["cluster"] = args.cluster
    out({"cluster": args.cluster, "leftovers": found})
    return EXIT_OK


def cmd_write_access(args):
    """Porte les registres d'amorçage du cluster (ou le seul nommé) au
    réglage courant du port d'écriture (2.6.1) : c'est la reprise des
    registres posés par la 2.6.0, faite à l'enregistrement du réglage."""
    try:
        sources = rg.settings(state())["write_sources"]
    except rg.SettingsError as e:
        step("write-access", "error", str(e))
        return EXIT_REFUSED
    names = [args.name] if args.name else [
        s["name"] for s in rg.list_decls(state())
        if not s.get("invalid") and s.get("kind") == "bootstrap" and s.get("cluster") == args.cluster]
    kube, _ = kube_from(args)
    done = []
    for n in names:
        d = rg.load(state(), n)
        if d is None or d.get("kind") != "bootstrap" or d.get("cluster") != args.cluster:
            step("write-access", "error", f"{n} is not a bootstrap registry of {args.cluster}")
            return EXIT_REFUSED
        try:
            res = rs.reconcile_write_access(kube, n, sources, step)
        except KubeError as e:
            step("write-access", "error", f"{n}: {str(e)[:300]}")
            return EXIT_FAIL
        if res["missing"]:
            step("write-access", "running", f"{n}: no service on {args.cluster} (registry removed by hand?)")
        done.append(dict(res, registry=n))
    changed = [x["registry"] for x in done if x["changed"]]
    step("write-access", "done", (f"{', '.join(changed)} brought to " if changed else "already at ")
         + (", ".join(sources) if sources else "an open write port") + f" ({len(names)} registry(ies))")
    out({"cluster": args.cluster, "write_sources": sources, "registries": done})
    return EXIT_OK


def cmd_settings(args):
    if args.write_sources is not None or args.clear:
        try:
            cur = rg.save_settings(state(), [] if args.clear else args.write_sources)["write_sources"]
        except ValueError as e:
            step("settings", "error", str(e))
            return EXIT_REFUSED
        step("settings", "done", "write port reserved to " + ", ".join(cur) if cur
             else "write port open to every address")
    try:
        out(rg.settings(state()))
    except rg.SettingsError as e:
        step("settings", "error", str(e))
        return EXIT_FAIL
    return EXIT_OK


def cmd_delete(args):
    d = rg.load(state(), args.name)
    if d is not None and d.get("kind") != "bootstrap":
        step("delete", "error", f"{args.name} is not a bootstrap registry (use forget)")
        return EXIT_REFUSED
    kube, _ = kube_from(args)
    try:
        used = users(args.name, [args.cluster], lambda c: kube)
    except KubeError as e:
        step("delete", "error", f"registry-usage-unknown: {str(e)[:200]}")
        return EXIT_REFUSED
    # 2.7.0 (Défaire, vu en réel) : le plan défait désigne son registre ;
    # lui seul ne le retient pas (son document reste et le reposerait)
    ignored = {f"plan:{p}" for p in args.ignore_plan or []}
    used = [u for u in used if u.split(" (", 1)[0] not in ignored]
    if used:
        step("delete", "error", f"registry-in-use: {', '.join(sorted(set(used)))}")
        out({"registry": args.name, "deleted": False, "used_by": sorted(set(used))})
        return EXIT_REFUSED
    expect = getattr(args, "expect_run", "")
    if expect:
        # revue finale 2.7.0 (m-b) : la marque relue juste avant d'écrire ;
        # sans Deployment, la suppression précédente s'est arrêtée en route (I2)
        try:
            dep = kube.get("deployments", rs.NAMESPACE, args.name)
        except KubeError as e:
            step("delete", "error", f"state unreadable: {str(e)[:200]}")
            return EXIT_FAIL
        refused = cc.run_mismatch(dep, expect) if dep is not None else None
        if refused:
            step("delete", "error", refused)
            out({"registry": args.name, "deleted": False})
            return EXIT_REFUSED
    names = rg.secret_names(args.name)
    # revue finale 2.7.0 (I2) : le Deployment porte la marque de l'exécution
    # (plan-run) ; il part en DERNIER, après la fiche de la console. Une
    # suppression arrêtée en route le laisse : le Défaire reconnaît encore
    # le registre et finit le retrait quand on le relance
    objs = [("services", args.name), ("services", rs.write_service_name(args.name)),
            ("configmaps", f"{args.name}-config"),
            ("secrets", f"{args.name}-tls"), ("secrets", f"{args.name}-htpasswd"),
            ("secrets", names["push"]), ("secrets", names["pull"]), ("secrets", names["ca"]),
            ("secrets", f"{args.name}{rs.CA_KEY_SUFFIX}"), ("secrets", f"{args.name}{rs.CA_NEXT_SUFFIX}")]
    if args.delete_data:
        objs.append(("persistentvolumeclaims", f"{args.name}-data"))

    def remove(kind, n):
        o = kube.get(kind, rs.NAMESPACE, n)
        if o is None:
            return
        if ((o.get("metadata") or {}).get("labels") or {}).get(rs.LABEL) != args.name:
            # revue 2.6.1 : jamais l'objet d'un autre registre
            step("delete", "running", f"{kind} {n} kept: it does not belong to {args.name}")
            return
        kube.delete(kind, rs.NAMESPACE, n)
        step("delete", "running", f"{kind} {n}")
    try:
        for kind, n in objs:
            remove(kind, n)
        if d is not None:
            rg.delete(state(), args.name)
        remove("deployments", args.name)
    except KubeError as e:
        step("delete", "error", str(e)[:300])
        return EXIT_FAIL
    kept = "" if args.delete_data else f"; volume {args.name}-data kept (its images stay for a new deployment)"
    step("delete", "done", f"{args.name} removed{kept}")
    out({"registry": args.name, "deleted": True, "data_kept": not args.delete_data})
    return EXIT_OK


# ---------------------------------------------------------------------------
# 2.8.0 : certificat du registre d'amorçage (point 235) et nœuds Harvester
# (point 236)
# ---------------------------------------------------------------------------
K_CAPI_CLUSTER = "clusters.cluster.x-k8s.io"
PENDING_REASONS = ("renewing", "nodes", "rke2-fetch", "service-ca", "flux", "harvester-nodes")


def _decoded(sec, key):
    v = ((sec or {}).get("data") or {}).get(key)
    if v is None:
        return ((sec or {}).get("stringData") or {}).get(key)
    try:
        return base64.b64decode(v).decode(errors="replace")
    except ValueError:
        return None


def ca_pending(kube, d):
    """[{"where", "reason"}] : chaque copie de la CA du registre qui ne
    porte pas encore la CA suivante ; vide : la bascule peut se faire.
    Revue 5-8 I-6, pour chaque cluster enfant qui emploie le registre :
    renouvellement pas fini (`renewing`, marque de capi_renew), Secret des
    nœuds que DÉSIGNENT ses gabarits actuels (`nodes`), récupération de RKE2
    d'un cluster sans Internet (`rke2-fetch`), CA des services (`service-ca`),
    entrées Flux tirées du registre (`flux:<entrée>`) ; et le réglage
    `additional-ca` des nœuds Harvester quand le registre est fusionné dans
    `containerd-registry` (`harvester-nodes`). Fleet vit dans Rancher et ne
    se lit pas d'ici (avertissement de `renew-cert`)."""
    import capi_renew as cr
    import capi_manifests as mf
    import child_gitops as cg
    want = (d.get("ca_next_pem") or "").strip()
    if not want:
        return []
    name = d["name"]
    out = []

    def lacks(text):
        return not text or want not in text
    for ns in rg.child_users(kube, name):
        for cl in kube.list(K_CAPI_CLUSTER, ns):
            c = (cl.get("metadata") or {}).get("name")
            where = f"{ns}/{c}"
            try:
                cur = cr.current(kube, ns, c)
            except cr.RenewError:
                continue
            cps = [t for _r, t in cur["templates"] if t.get("kind") == "RKE2ControlPlaneTemplate"]
            sname = rg.nodes_secret_from(cps)
            sec = kube.get("secrets", ns, sname) if sname else None
            labels = ((sec or {}).get("metadata") or {}).get("labels") or {}
            uses = labels.get(rg.used_label(name)) == "used" \
                or kube.get("secrets", ns, rg.child_secret_names(c, name)["auth"]) is not None
            fetch = kube.get("secrets", ns, f"{c}-rke2-fetch")
            fetch_ours = (((fetch or {}).get("metadata") or {}).get("labels") or {}).get(rg.LABEL) == name
            svc = kube.get("secrets", ns, rg.child_secret_names(c, name)["ca"])
            if not uses and not fetch_ours and svc is None:
                continue
            if cr.marker_of(cl) is not None:
                out.append({"where": where, "reason": "renewing"})
            if uses and lacks(_decoded(sec, f"ca-{name}.crt")):
                out.append({"where": where, "reason": "nodes"})
            if fetch_ours and lacks(_decoded(fetch, "ca.crt")):
                out.append({"where": where, "reason": "rke2-fetch"})
            if svc is not None and lacks(_decoded(svc, "ca.crt")):
                out.append({"where": where, "reason": "service-ca"})
        endpoint = f"oci://{d.get('endpoint')}/"
        for crs in kube.list(mf.K_CRS, ns, selector=mf.L_ITEM):
            md = crs.get("metadata") or {}
            item = (md.get("labels") or {}).get(mf.L_ITEM) or ""
            if not item.startswith(cg.ITEM_PREFIX):
                continue
            objs = mf.objects_of(kube.get("configmaps", ns, md["name"]))
            if not any(str((o.get("spec") or {}).get("url") or "").startswith(endpoint) for o in objs):
                continue
            secs = mf.secret_objects(kube.get("secrets", ns, md["name"]))
            cas = [((x.get("stringData") or {}).get("ca.crt") or "") for x in secs
                   if (x.get("metadata") or {}).get("name", "").endswith("-ca")]
            if not cas or any(lacks(x) for x in cas):
                out.append({"where": f"{ns}/{(md.get('labels') or {}).get(mf.L_CLUSTER) or '?'}",
                            "reason": f"flux:{item[len(cg.ITEM_PREFIX):]}"})
    cur = (kube.get("settings.harvesterhci.io", None, SETTING) or {})
    try:
        merged = d.get("endpoint") in ((json.loads(cur.get("value") or "{}") or {}).get("Configs") or {})
    except ValueError:
        merged = False
    if merged and lacks((kube.get("settings.harvesterhci.io", None, ADDITIONAL_CA) or {}).get("value")):
        out.append({"where": "harvester", "reason": "harvester-nodes"})
    return out


def pending_text(pending):
    return ", ".join(f"{p['where']} ({p['reason']})" for p in pending)


def _secret_values(sec):
    return {k: base64.b64decode(v).decode() for k, v in ((sec or {}).get("data") or {}).items()}


def _swap_cert(kube, d, tls, step_id, timeout):
    """Secret TLS remplacé, pod redémarré, attente de `/v2/` avec la CA
    donnée : rend (raison d'attente ou None, secondes sans lecture)."""
    kube.apply([{"apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/tls",
                 "metadata": {"name": f"{d['name']}-tls", "namespace": rs.NAMESPACE, "labels": {rs.LABEL: d["name"]}},
                 "stringData": {"tls.crt": tls["crt"], "tls.key": tls["key"], "ca.crt": tls["ca"]}}])
    t0 = time.monotonic()
    kube.patch("deployments", rs.NAMESPACE, d["name"], rs.restart_patch(int(time.time())))
    step(step_id, "running", f"{d['name']}: certificate replaced, pod restarted")
    pull = rg.read_creds(kube, d["name"], "pull")
    why = wait_ready(dict(d, ca_pem=tls["ca"]), pull, timeout)
    return why, round(time.monotonic() - t0, 1)


def cmd_renew_cert(args):
    """Nouveau certificat serveur signé par la CA gardée (`<r>-ca-key`) ;
    `--new-ca` pour un registre d'avant 2.8.0 : nouvelle CA en transition
    (`ca_next_pem`), puis, quand chaque cluster enfant la croit (`harvester-capi
    renew --what ca`), bascule du certificat (refus
    `registry-ca-transition-pending` avant). Aucun fichier des nœuds ne change
    pour un simple renouvellement."""
    d = rg.load(state(), args.name)
    if d is None or d.get("kind") != "bootstrap" or d.get("cluster") != args.cluster:
        step("renew-cert", "error", f"{args.name} is not a bootstrap registry of {args.cluster}")
        return EXIT_REFUSED
    if not 1 <= args.days <= rs.CA_DAYS:
        step("renew-cert", "error", f"--days: 1 to {rs.CA_DAYS}")
        return EXIT_REFUSED
    d["name"] = args.name
    kube, _ = kube_from(args)
    try:
        key = kube.get("secrets", rs.NAMESPACE, f"{args.name}{rs.CA_KEY_SUFFIX}")
        nxt = kube.get("secrets", rs.NAMESPACE, f"{args.name}{rs.CA_NEXT_SUFFIX}")
    except KubeError as e:
        step("renew-cert", "error", str(e)[:300])
        return EXIT_FAIL
    if key is None and not args.new_ca and not d.get("ca_next_pem"):
        step("renew-cert", "error", f"registry-ca-key-missing: the CA of {args.name} was not kept (deployed before "
                                    "2.8.0); renew it with --new-ca")
        out({"registry": args.name, "code": "registry-ca-key-missing"})
        return EXIT_REFUSED
    address, dns = d.get("address") or d["endpoint"], d.get("dns_name")
    try:
        work = work_dir.mkdtemp("hops-registry-")
    except ValueError as e:
        step("renew-cert", "error", str(e))
        return EXIT_REFUSED
    try:
        if args.new_ca and not d.get("ca_next_pem"):
            if args.dry_run:
                out({"registry": args.name, "new_ca": True, "step": "transition"})
                return EXIT_OK
            ca = rs.make_ca(work, address, dns, args.name, prefix="next")
            kube.apply([rs.ca_key_secret(args.name, ca["ca"], ca["ca_key"], rs.CA_NEXT_SUFFIX)])
            rg.update(state(), args.name, lambda x: x.update(ca_next_pem=ca["ca"]))
            d["ca_next_pem"] = ca["ca"]
            step("renew-cert", "running", f"{args.name}: new CA kept in {args.name}{rs.CA_NEXT_SUFFIX}, in transition")
            nxt = {"data": {"ca.key": base64.b64encode(ca["ca_key"].encode()).decode(),
                            "ca.crt": base64.b64encode(ca["ca"].encode()).decode()}}
        if args.new_ca or d.get("ca_next_pem"):
            pending = ca_pending(kube, d)
            if pending:
                step("renew-cert", "error", f"registry-ca-transition-pending: {pending_text(pending)} do not trust "
                                            "the new CA yet (harvester-capi renew --what ca, renew --finish, "
                                            "GitOps entries deployed again, harvester-nodes)")
                out({"registry": args.name, "code": "registry-ca-transition-pending",
                     "clusters": sorted({p["where"] for p in pending}), "pending": pending,
                     "findings": [cc.finding("registry-ca-transition-pending", "block",
                                             clusters=pending_text(pending))], "blocked": True})
                return EXIT_REFUSED
            step("renew-cert", "running", "registry-ca-fleet-unchecked: Fleet entries live in Rancher and are "
                                          "not read here; deploy them again before the switch")
            signer = _secret_values(nxt)
        else:
            signer = _secret_values(key)
        if not signer.get("ca.key") or not signer.get("ca.crt"):
            # revue 5-8 m-6 : bascule arrêtée après le retrait de la CA
            # suivante, avant la mise à jour de la déclaration
            step("renew-cert", "error", f"registry-ca-signer-missing: the CA that should sign the certificate of "
                                        f"{args.name} is not on the cluster; deploy the registry again or run "
                                        "renew-cert --new-ca after removing ca_next_pem from its declaration")
            out({"registry": args.name, "code": "registry-ca-signer-missing",
                 "findings": [cc.finding("registry-ca-signer-missing", "block", registry=args.name)],
                 "blocked": True})
            return EXIT_REFUSED
        (Path(work) / "ca.key").write_text(signer["ca.key"])
        (Path(work) / "ca.crt").write_text(signer["ca.crt"])
        os.chmod(Path(work) / "ca.key", 0o600)
        tls = dict(rs.sign_server(work, address, dns, days=args.days), ca=signer["ca.crt"])
        not_after = rs.cert_not_after(tls["crt"])
        if args.dry_run:
            out({"registry": args.name, "not_after": not_after, "new_ca": bool(d.get("ca_next_pem"))})
            return EXIT_OK
        why, cut = _swap_cert(kube, d, tls, "renew-cert", args.timeout)
        if why:
            step("renew-cert", "error", f"not ready after {args.timeout} s: {why}")
            return EXIT_FAIL
        if d.get("ca_next_pem"):
            # bascule faite : la CA suivante devient celle du registre
            kube.apply([rs.ca_key_secret(args.name, signer["ca.crt"], signer["ca.key"])])
            kube.delete("secrets", rs.NAMESPACE, f"{args.name}{rs.CA_NEXT_SUFFIX}")
            rg.update(state(), args.name, lambda x: (x.update(ca_pem=signer["ca.crt"]), x.pop("ca_next_pem", None)))
        step("renew-cert", "done", f"{args.name}: certificate valid until "
                                   f"{time.strftime('%Y-%m-%d', time.gmtime(not_after)) if not_after else '?'}, "
                                   f"reads cut {cut} s")
    except (KubeError, RuntimeError, OSError, dp.PlanConflict, dp.ProfileError) as e:
        step("renew-cert", "error", str(e)[:300])
        return EXIT_FAIL
    finally:
        shutil.rmtree(work, ignore_errors=True)
    out({"registry": args.name, "not_after": not_after, "read_cut_seconds": cut})
    return EXIT_OK


SETTING = "containerd-registry"
ADDITIONAL_CA = "additional-ca"


AUTH_SECRET_NS = "fleet-local"
AUTH_SECRET_LABEL = "harvesterhci.io/setting"
AUTH_SECRET_TYPE = "rke.cattle.io/auth-config"
AUTH_MOVE_POLL = 3


def _auth_moved(kube, endpoint):
    """(déplacé, nom du Secret) : Harvester a retiré `Auth` du réglage pour
    ce point d'accès et rangé les identifiants dans un Secret
    `rke.cattle.io/auth-config` de `fleet-local` étiqueté au réglage, clé
    `host` = point d'accès. Source : harvester/harvester
    pkg/controller/master/setting/containerd_registry.go
    (`removeCredentialInSetting`, `syncContainerdRegistryConfigSecret`) ;
    ce contrôleur est le seul à écrire ces Secrets et la liste des registres
    du cluster `fleet-local/local` (il élague tout autre Secret étiqueté et
    réécrit la liste), d'où l'écriture par le réglage et cette vérification."""
    cur = kube.get("settings.harvesterhci.io", None, SETTING) or {}
    try:
        conf = ((json.loads(cur.get("value") or "{}") or {}).get("Configs") or {}).get(endpoint) or {}
    except ValueError:
        return False, ""
    if conf.get("Auth"):
        return False, ""
    for sec in kube.list("secrets", AUTH_SECRET_NS, selector=f"{AUTH_SECRET_LABEL}={SETTING}"):
        if sec.get("type") == AUTH_SECRET_TYPE and _decoded(sec, "host") == endpoint and _decoded(sec, "password"):
            return True, (sec.get("metadata") or {}).get("name") or ""
    return False, ""


def _other_auth_hosts(kube, before, endpoint):
    """Hôtes du réglage, autres que `endpoint`, dont Harvester garde le mot
    de passe dans un Secret auth-config (leur `Auth` est déjà retiré du
    réglage). Revue finale 2.8.0 I-1 : quand les miroirs changent, le
    contrôleur de Harvester 1.9 réécrit ces Secrets depuis le réglage, avec
    `host` seul pour un `Auth` nul (`syncContainerdRegistryConfigSecret`,
    pkg/controller/master/setting/containerd_registry.go) : écrire le réglage
    effacerait leurs identifiants."""
    try:
        configs = (json.loads(before or "{}") or {}).get("Configs") or {}
    except ValueError:
        configs = {}
    hosts = {h for h, c in configs.items() if h != endpoint and not (c or {}).get("Auth")}
    if not hosts:
        return []
    return sorted({_decoded(sec, "host") for sec in
                   kube.list("secrets", AUTH_SECRET_NS, selector=f"{AUTH_SECRET_LABEL}={SETTING}")
                   if sec.get("type") == AUTH_SECRET_TYPE and _decoded(sec, "password")
                   and _decoded(sec, "host") in hosts})


def cmd_harvester_nodes(args):
    """Fusionne le registre (miroirs, CA, compte de lecture) dans le
    réglage `containerd-registry` des nœuds Harvester, sans toucher aux
    autres entrées (`--remove` : retire la seule entrée du registre) ; CA
    (et CA suivante pendant une transition) dans `additional-ca`. Valeur
    précédente rendue masquée (retour arrière). Harvester redémarre RKE2
    sur chaque nœud (danger `rke2` du réglage).

    Revue 5-8 I-7 : le corps des écritures passe par un fichier privé,
    jamais par la ligne de commande ; Harvester range ensuite les
    identifiants dans un Secret `rke.cattle.io/auth-config` de `fleet-local`
    et les retire du réglage : c'est vérifié (`--wait` secondes), refus
    `harvester-nodes-auth-inline` sinon. Les refus et constats sont rendus
    en JSON (`findings`, `blocked`), comme `renew-cert`."""
    import hv_settings as hs
    base = {"registry": args.name, "cluster": args.cluster, "remove": bool(args.remove)}

    def refuse(code, message, rc=EXIT_REFUSED, **facts):
        step("harvester-nodes", "error", f"{code}: {message}" if code not in message else message)
        out(dict(base, code=code, findings=[cc.finding(code, "block", **facts)], blocked=True))
        return rc
    d = rg.load(state(), args.name)
    if d is None or args.cluster not in (d.get("clusters") or []):
        return refuse("registry-not-attached", f"{args.name} on {args.cluster}", cluster=args.cluster)
    d["name"] = args.name
    kube, _ = kube_from(args)
    try:
        cur = kube.get("settings.harvesterhci.io", None, SETTING) or {}
        ca_cur = kube.get("settings.harvesterhci.io", None, ADDITIONAL_CA) or {}
        creds = None if args.remove else rg.read_creds(kube, args.name, "pull")
    except KubeError as e:
        return refuse("cluster-unreachable", str(e)[:300], EXIT_FAIL)
    if not args.remove and not creds:
        return refuse("registry-not-attached", f"no read account of {args.name} on {args.cluster}",
                      cluster=args.cluster)
    before = cur.get("value") or cur.get("default") or ""
    try:
        value, found = rg.containerd_merge(before, d, creds, remove=args.remove)
    except ValueError as e:
        return refuse("harvester-nodes-value-unreadable", str(e)[:300])
    if value != before:
        try:
            others = _other_auth_hosts(kube, before, d["endpoint"])
        except KubeError as e:
            return refuse("cluster-unreachable", str(e)[:300], EXIT_FAIL)
        if others:
            found = found + [cc.finding("harvester-nodes-other-auth", "block", hosts=", ".join(others))]
            step("harvester-nodes", "error", f"harvester-nodes-other-auth: {', '.join(others)} keep a password in "
                                             f"Harvester; changing the mirrors of {SETTING} would make Harvester erase "
                                             "it. Nothing written")
    for f in found:
        step("harvester-nodes", "error" if f["level"] == "block" else "running", f"{f['code']} {json.dumps(f['facts'])}")
    if cc.blocking(found):
        out(dict(base, findings=found, blocked=True))
        return EXIT_REFUSED
    ca_before = ca_cur.get("value") or ""
    ca_value = ca_before
    for pem in (d.get("ca_pem"), d.get("ca_next_pem")):
        ca_value = rg.additional_ca_merge(ca_value, pem or "", remove=args.remove)
    result = dict(base, previous=hs.redact(SETTING, before), value=hs.redact(SETTING, value),
                  additional_ca_changed=ca_value != ca_before,
                  findings=found + [cc.finding("harvester-rke2-restart", "warn", setting=SETTING)], blocked=False)
    step("harvester-nodes", "running", "harvester-rke2-restart: Harvester restarts RKE2 on every node to apply it")
    if args.dry_run or (value == before and ca_value == ca_before):
        step("harvester-nodes", "done", "nothing to change" if value == before and ca_value == ca_before
             else "dry run: nothing written")
        out(dict(result, changed=value != before or ca_value != ca_before))
        return EXIT_OK
    try:
        if ca_value != ca_before:
            kube.patch("settings.harvesterhci.io", None, ADDITIONAL_CA, {"value": ca_value}, secret=True)
        if value != before:
            kube.patch("settings.harvesterhci.io", None, SETTING, {"value": value}, secret=True)
    except KubeError as e:
        return refuse("harvester-nodes-write-failed", str(e)[:300], EXIT_FAIL)
    if not args.remove and value != before:
        deadline = time.monotonic() + args.wait
        while True:
            try:
                moved, secret_name = _auth_moved(kube, d["endpoint"])
            except KubeError:
                moved, secret_name = False, ""
            if moved or time.monotonic() >= deadline:
                break
            time.sleep(AUTH_MOVE_POLL)
        if not moved:
            step("harvester-nodes", "error", f"harvester-nodes-auth-inline: after {args.wait} s the read password "
                                             f"of {args.name} is still in the {SETTING} setting; Harvester did not "
                                             "move it into its auth-config Secret (remove the registry with --remove)")
            out(dict(result, changed=True, blocked=True,
                     findings=result["findings"] + [cc.finding("harvester-nodes-auth-inline", "block",
                                                               registry=args.name, seconds=args.wait)]))
            return EXIT_FAIL
        step("harvester-nodes", "running", f"read account kept by Harvester in the Secret {AUTH_SECRET_NS}/"
                                           f"{secret_name} ({AUTH_SECRET_TYPE}), removed from the setting")
        result["auth_secret"] = f"{AUTH_SECRET_NS}/{secret_name}"
    step("harvester-nodes", "done", f"{args.name} {'removed from' if args.remove else 'merged into'} {SETTING}")
    out(dict(result, changed=True))
    return EXIT_OK


def parser():
    p = argparse.ArgumentParser(prog="harvester-registry", allow_abbrev=False,
                                description="Registries of the child clusters.")
    p.add_argument("--state-dir", help="console state directory (default HARVESTER_OPS_STATE_DIR)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def cluster_args(s):
        s.add_argument("--cluster", required=True)
        s.add_argument("--kubeconfig")

    s = sub.add_parser("declare", allow_abbrev=False)
    s.add_argument("name")
    s.add_argument("--file", required=True)
    s.add_argument("--revision", type=int)
    s.set_defaults(fn=cmd_declare)
    s = sub.add_parser("attach", allow_abbrev=False)
    s.add_argument("name")
    cluster_args(s)
    s.add_argument("--spec", default="-", help="credentials JSON file (private), - for stdin")
    s.set_defaults(fn=cmd_attach)
    s = sub.add_parser("check", allow_abbrev=False)
    s.add_argument("name")
    cluster_args(s)
    s.add_argument("--vm-network", help="namespace/name: probe the registry from this VM network (pod)")
    s.set_defaults(fn=cmd_check)
    s = sub.add_parser("write-access", allow_abbrev=False)
    s.add_argument("name", nargs="?")
    cluster_args(s)
    s.set_defaults(fn=cmd_write_access)
    s = sub.add_parser("settings", allow_abbrev=False)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--write-sources", help="networks of the console allowed on the write port, comma separated")
    g.add_argument("--clear", action="store_true", help="open the write port to every address")
    s.set_defaults(fn=cmd_settings)
    s = sub.add_parser("list", allow_abbrev=False)
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser("deploy", allow_abbrev=False)
    s.add_argument("name")
    cluster_args(s)
    s.add_argument("--address", required=True)
    s.add_argument("--size-gib", type=int, default=20)
    s.add_argument("--storage-class", required=True)
    s.add_argument("--dns-name")
    s.add_argument("--mirrors", default="", help="origin registries it mirrors, comma separated")
    s.add_argument("--cert")
    s.add_argument("--key")
    s.add_argument("--ca")
    s.add_argument("--spec", help="private JSON file {cert, key, ca} (client certificate)")
    s.add_argument("--timeout", type=int, default=600)
    s.add_argument("--dry-run", action="store_true", help="checks only")
    s.add_argument("--origin", default="", help="plan=P,run=ID: origin set on the objects (chain of a plan)")
    s.set_defaults(fn=cmd_deploy)
    s = sub.add_parser("status", allow_abbrev=False)
    s.add_argument("name")
    cluster_args(s)
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("leftovers", allow_abbrev=False)
    cluster_args(s)
    s.set_defaults(fn=cmd_leftovers)
    s = sub.add_parser("delete", allow_abbrev=False)
    s.add_argument("name")
    cluster_args(s)
    s.add_argument("--delete-data", action="store_true")
    s.add_argument("--ignore-plan", action="append", metavar="PLAN",
                   help="a plan being undone: it does not count as a user")
    s.add_argument("--expect-run", default="", metavar="ID",
                   help="refuse unless the Deployment carries this plan run (the undo of a plan)")
    s.set_defaults(fn=cmd_delete)
    s = sub.add_parser("renew-cert", allow_abbrev=False)
    s.add_argument("name")
    cluster_args(s)
    s.add_argument("--days", type=int, default=730, help="validity of the new certificate (default 730)")
    s.add_argument("--new-ca", action="store_true", help="registry deployed before 2.8.0: new CA, in transition")
    s.add_argument("--timeout", type=int, default=300)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_renew_cert)
    s = sub.add_parser("harvester-nodes", allow_abbrev=False)
    s.add_argument("name")
    cluster_args(s)
    s.add_argument("--remove", action="store_true", help="remove only this registry from the setting")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--wait", type=int, default=60,
                   help="seconds to wait for Harvester to move the read account out of the setting (default 60)")
    s.set_defaults(fn=cmd_harvester_nodes)
    s = sub.add_parser("show", allow_abbrev=False)
    s.add_argument("name")
    s.set_defaults(fn=cmd_show)
    s = sub.add_parser("forget", allow_abbrev=False)
    s.add_argument("name")
    s.add_argument("--kubeconfig-for", action="append", metavar="CLUSTER=PATH",
                   help="kubeconfig of an attached cluster (the console passes the person's)")
    s.set_defaults(fn=cmd_forget)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.state_dir:
        os.environ["HARVESTER_OPS_STATE_DIR"] = os.path.abspath(args.state_dir)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
