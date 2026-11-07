"""harvester-ops : « Défaire le déploiement » (2.7.0, décision 128).

Moteur pur : la collecte de ce qu'une chaîne d'exécutions `deploy:` a créé,
le plan de destruction et son ordre, les phases données au moteur de
deploy_chain. Les lectures et les gestes sont injectés par l'appelant (la
console, ou la ligne de commande qui la charge).

Ce qui est défait : ce que la chaîne a créé, soit un élément d'une phase qui
porte `created: true` et `undo: {kind, ref}`. Une ressource reprise sans
marque (« already present, adopted ») n'est jamais touchée. Jamais
l'installation de Harvester, jamais la pile Cluster API (128).

Ordre de retrait (point 211), l'inverse de l'application : entrées GitOps
(2.8.0), artefacts (charts, puis manifestes, puis politiques), import dans
le Rancher de la session (2.8.0), clusters enfants, registre d'amorçage,
contenu Terraform. L'import part AVANT son cluster : sinon Rancher garde un
cluster mort (plan 2.8.0, tâche 5). Les artefacts d'un cluster enfant passent
avant ce cluster : un retrait d'artefact en échec garde son cluster (la
raison est dite), le reste continue. Le registre attend tous les clusters
choisis, le contenu Terraform attend clusters et registre (revue 4-8 I2) :
un cluster ou un registre resté là garde ce qui peut le porter.

Exceptions bornées :
- registre d'amorçage posé par le plan : proposé décoché (point 210) ;
- déclaration Terraform d'une exécution d'avant 2.7.0 (aucune marque) dont
  l'origine est ce plan : proposée décochée, création non prouvée (212) ;
- déclaration modifiée depuis son application : proposée décochée (212).

Bibliothèque standard seulement.

Voir docs/design/2026-11-07-artefacts-complets-defaire-270-plan.md.
"""

import deploy_chain as dch

KINDS_ORDER = ("gitops", "chart", "manifest", "policies", "rancher-import", "capi-cluster", "registry",
               "tf-declaration")
# phase du défaire de chaque sorte (moteur de deploy_chain)
PHASE_OF = {"gitops": "gitops", "chart": "artefacts", "manifest": "artefacts", "policies": "artefacts",
            "rancher-import": "rancher-import", "capi-cluster": "clusters", "registry": "registry",
            "tf-declaration": "terraform"}
ORDER = ("gitops", "artefacts", "rancher-import", "clusters", "registry", "terraform")
ARTEFACT_KINDS = ("chart", "manifest", "policies")
# sortes portées par un cluster enfant : `<ns>/<c>/<élément>` (GitOps et
# artefacts) ou `<ns>/<c>` (import, cluster)
ITEM_KINDS = ("gitops",) + ARTEFACT_KINDS
MAX_CHAIN = 32
NOT_PROVEN = "creation by this run not proven"
MODIFIED = "changed since it was applied"
REGISTRY_NOTE = "unchecked by default: the registry and its data outlive the plan"


class UndoError(Exception):
    """Refus explicable ; `code` le nomme."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def chain(run_id, read):
    """Résultats de la chaîne de reprises qui finit par `run_id`, du premier
    maillon au dernier. `read(id)` rend le résultat d'une action (dict) ou
    None. 32 maillons au plus ; une boucle de `resumes` est refusée."""
    out, seen = [], []
    current = run_id
    while current:
        if current in seen:
            raise UndoError("undo-chain-loop", f"the runs resume each other ({' -> '.join(seen + [current])})")
        if len(seen) >= MAX_CHAIN:
            raise UndoError("undo-chain-too-long", f"more than {MAX_CHAIN} resumed runs")
        seen.append(current)
        res = read(current)
        if not isinstance(res, dict):
            break                              # maillon disparu : on défait ce qui est connu
        out.append(dict(res, _run=current))
        current = res.get("resumes")
    return list(reversed(out))


def undo_id(kind, ref):
    return f"{kind}-{ref}"


def cluster_of(u):
    """`<ns>/<c>` du cluster enfant d'une entrée GitOps, d'un artefact, d'un
    import ou d'un cluster, sinon None."""
    if u["kind"] in ITEM_KINDS:
        return u["ref"].rsplit("/", 1)[0]
    if u["kind"] in ("capi-cluster", "rancher-import"):
        return u["ref"]
    return None


def _collect_one(found, it, u, res):
    """Marque `undo` d'un élément créé : dernier état gagnant, mais « créé
    par » reste le premier maillon (une reprise ne fait que recopier la
    marque)."""
    if not (it.get("created") and u.get("kind") in KINDS_ORDER and isinstance(u.get("ref"), str)):
        return
    key = (u["kind"], u["ref"])
    first = (found.get(key) or {}).get("run") if (found.get(key) or {}).get("proven") else None
    found[key] = {"id": undo_id(*key), "kind": u["kind"], "ref": u["ref"],
                  "phase": PHASE_OF[u["kind"]], "run": first or res["_run"], "proven": True,
                  "default": u["kind"] != "registry",
                  "note": REGISTRY_NOTE if u["kind"] == "registry" else "",
                  "revision_applied": it.get("revision_applied")}


def collect(results, tf_lookup=None, plan=None, cluster=None):
    """[Undo] de ce que la chaîne a créé, dernier état gagnant. Undo =
    {id, kind, ref, phase, run, proven, default, note}. `tf_lookup(id)` rend
    {origin, revision} d'une déclaration encore là (None sinon) : elle sert
    aux exécutions d'avant 2.7.0 (déclaration de l'origine `plan` sans
    marque) et aux déclarations modifiées depuis.

    Revue finale 2.7.0 (I1) : chaque déclaration reçoit `carries`, les
    clusters enfants que la phase Clusters de la chaîne nomme, créés ou
    repris. Tant que l'un d'eux est là, son contenu Terraform reste."""
    found, carriers = {}, set()
    for res in results:
        for ph in res.get("phases") or []:
            for it in ph.get("items") or []:
                if ph.get("id") == "clusters" and (it.get("ref") or it.get("cluster")):
                    carriers.add(it.get("ref") or f"{it['cluster']}/{it['cluster']}")
                _collect_one(found, it, it.get("undo") or {}, res)
                # 2.8.0 : un cluster porte aussi son import (`undo_more`), posé
                # seulement quand la chaîne l'a fait, même sur un cluster repris
                for x in it.get("undo_more") or []:
                    if isinstance(x, dict):
                        _collect_one(found, dict(it, created=True), x, res)
                if ph.get("id") == "terraform" and it.get("declaration_id") and not it.get("created") \
                        and tf_lookup is not None:
                    key = ("tf-declaration", it["declaration_id"])
                    d = tf_lookup(it["declaration_id"])
                    origin = (d or {}).get("origin") or {}
                    if d and origin.get("plan") == plan and not origin.get("run") and key not in found:
                        # point 212 : d'avant 2.7.0, jamais détruite d'office
                        found[key] = {"id": undo_id(*key), "kind": key[0], "ref": key[1], "phase": "terraform",
                                      "run": res["_run"], "proven": False, "default": False, "note": NOT_PROVEN,
                                      "revision_applied": it.get("revision_applied")}
    out = list(found.values())
    for u in out:
        if u["kind"] == "tf-declaration":
            u["carries"] = sorted(carriers)
        if u["kind"] == "tf-declaration" and tf_lookup is not None:
            d = tf_lookup(u["ref"])
            if d is not None and u.get("revision_applied") is not None \
                    and d.get("revision") != u["revision_applied"]:
                u.update(default=False, note=(u["note"] + "; " if u["note"] else "") + MODIFIED)
        u.pop("revision_applied", None)
    return out


def plan(undos, include=None):
    """Plan de destruction dans l'ordre de retrait (KINDS_ORDER, stable).
    `include` : identifiants choisis ; None = ceux cochés par défaut. Chaque
    entrée reçoit `depends_on` : un cluster enfant attend le retrait de ses
    artefacts, le registre celui des clusters, le contenu Terraform celui des
    clusters et du registre."""
    chosen = [u for u in undos if (u["default"] if include is None else u["id"] in include)]
    chosen.sort(key=lambda u: KINDS_ORDER.index(u["kind"]))
    by_cluster, gitops_of, import_of = {}, {}, {}
    for u in chosen:
        if u["kind"] in ARTEFACT_KINDS:
            by_cluster.setdefault(cluster_of(u), []).append(u["id"])
        elif u["kind"] == "gitops":
            gitops_of.setdefault(cluster_of(u), []).append(u["id"])
        elif u["kind"] == "rancher-import":
            import_of[u["ref"]] = u["id"]
    # point 211 et revue 4-8 I2 (décision du propriétaire du produit) : le
    # registre attend les clusters, le contenu Terraform (réseau, classe de
    # stockage, image) attend les clusters et le registre qu'il peut porter ;
    # un retrait en échec garde donc ce qui le porte
    clusters = [f"clusters:{u['id']}" for u in chosen if u["kind"] == "capi-cluster"]
    registries = [f"registry:{u['id']}" for u in chosen if u["kind"] == "registry"]
    out = []
    for u in chosen:
        deps = []
        if u["kind"] in ARTEFACT_KINDS:
            # 2.8.0 : le chart de Flux reste tant qu'une entrée GitOps du
            # cluster n'est pas retirée
            deps = [f"gitops:{i}" for i in gitops_of.get(cluster_of(u), [])]
        elif u["kind"] == "rancher-import":
            # une entrée Fleet vit dans le Rancher de l'import
            deps = [f"gitops:{i}" for i in gitops_of.get(u["ref"], [])]
        elif u["kind"] == "capi-cluster":
            deps = [f"gitops:{i}" for i in gitops_of.get(u["ref"], [])] \
                + [f"artefacts:{i}" for i in by_cluster.get(u["ref"], [])] \
                + ([f"rancher-import:{import_of[u['ref']]}"] if u["ref"] in import_of else [])
        elif u["kind"] == "registry":
            deps = list(clusters)
        elif u["kind"] == "tf-declaration":
            deps = clusters + registries
        out.append(dict(u, depends_on=deps))
    return out


def phases(steps, run_item, verify_item):
    """Une phase de deploy_chain par phase du défaire présente dans le plan.
    Une phase va à son terme même si une précédente a échoué : seuls les
    éléments qui dépendent d'un échec sont sautés."""
    out = []
    for i, pid in enumerate(ORDER):
        rows = [s for s in steps if s["phase"] == pid]
        if not rows:
            continue
        out.append(dch.Phase(pid, items=[dch.Item(s["id"], tuple(s["depends_on"])) for s in rows],
                             run_item=run_item, verify_item=verify_item, item_key="id", item_step="undo-",
                             after_failure_of=ORDER[:i]))
    return out
