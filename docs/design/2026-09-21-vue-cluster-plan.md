# Vue Cluster : plan d'implémentation

Mise en œuvre de `docs/design/2026-09-21-vue-cluster.md`. Chaque tâche avec
ses tests, vérifiés par sabotage ; tests API avant chaque commit.

**Contraintes globales :** version `1.43.0` ; un seul appel kubectl groupé
pour la vue ; textes dans les cinq langues ; `esc` avant `innerHTML` ; bulle
d'aide sur chaque contrôle ; aucune mention d'outil d'IA, ni tiret cadratin
ni flèche Unicode dans le contenu public.

### Tâche 1 : données (`web/app.py`)
- `TOPOLOGY_KINDS` + `persistentvolumeclaims`, `replicas.longhorn.io`.
- `_vm_nics(vm, vmi)` extrait de la Fabrique et partagé.
- `_topology_vm` : `vcpu`, `memory`, `disks`, `disk_total`, `nics`, `guest_os`.
- `_topology_node` / `_build_topology` : allouable, alloué, `maintenance`.
- Tests : `tests/api/test_topology.py`.

### Tâche 2 : maintenance et isolement (`web/node_maintenance.py`, `web/app.py`)
- Module pur : `maintenance_state(node)`, `drain_possible(node, nodes)`,
  `non_migratable(node, vmis, volumes, replicas)`, `plan(...)`.
- Points d'accès : `GET /api/node/<c>/<n>/maintenance-check`,
  `POST /api/node/<c>/<n>/cordon`, `.../uncordon`, `.../maintenance`
  (`{"force": bool}`), `DELETE .../maintenance` ; actions tracées.
- Tests : `tests/api/test_node_maintenance.py`.

### Tâche 3 : l'écran (`web/static/js/cluster-map.js`)
- Blocs d'hôte, jauges, cartes, calque, filtre, panneaux d'actions (VM et
  hôte), textes cinq langues ; branchement dans `app.js`.
- Tests : `tests/e2e/test_cluster_map_view.py`.

### Tâche 4 : ménage
- Retrait de `topology.js`, de `web/static/vendor/cytoscape/`, du montage
  canevas dans `app.js` ; tests du canevas retirés ou reportés.

### Tâche 5 : réel, doc, version
- harv1 : captures, éditeur et console depuis une carte, isoler puis
  réintégrer, refus du mode maintenance.
- Doc EN et FR, CHANGELOG `1.43.0`, VERSION, mémoire ; suites complètes,
  commit, push Gitea.
