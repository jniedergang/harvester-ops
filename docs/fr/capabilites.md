# Capacités

harvester-ops est une console d'exploitation pour clusters SUSE Harvester.
Né comme outillage de séquençage électrique, il couvre aujourd'hui la
majeure partie de la surface day-2. Cette page fait le tour de chaque
domaine de capacité, son rôle, et où il vit (CLI vs. console web).

Tout est **multi-cluster** (un seul `config.yaml` déclare N clusters) et
chaque opération mutative est **tracée comme une action** avec ses logs
live et un historique conservé.

---

## 1. Séquençage électrique (CLI + console)

Le cœur d'origine, et la seule partie nécessaire pour un déploiement
shutdown/startup pur. Disponible en bash auditable et reflété dans la
console.

- **Shutdown gracieux** — 8 étapes ordonnées : pre-flight → snapshot etcd
  → snapshot VM optionnel → arrêt VM ordonné → maintenance Longhorn →
  cordon → arrêt workers → arrêt control-plane.
- **Startup** — 5 étapes : démarrer le premier control-plane → démarrer
  le reste → attendre les nodes Ready → restaurer l'état du cluster →
  redémarrer les VMs en ordre de groupe inversé (parallèle dans un
  groupe).
- **Groupes d'ordonnancement VM** — les VMs s'arrêtent/redémarrent par
  groupes configurables : séquentiel *entre* groupes, parallèle *dans* un
  groupe, avec priorité par groupe.
- **Invariants garantis** : aucune perte de données Longhorn, aucune perte
  de quorum etcd en cours de shutdown, arrêt ACPI gracieux avant
  détachement du stockage.

→ Référence pas à pas complète : [procedure-operationnelle.md](procedure-operationnelle.md).

```bash
harvester-status   --cluster prod
harvester-shutdown --cluster prod --interactive
harvester-startup  --cluster prod
```

## 2. Cycle de vie VM (CLI partiel, console complet)

Gérer les machines virtuelles KubeVirt sans quitter la console.

- Liste VM par namespace avec `runStrategy` et phase VMI.
- Start/stop en masse via `runStrategy` (une VM ou tout un namespace).
- **Snapshots** — créer un `VirtualMachineBackup` (type=snapshot) par VM ;
  restaurer depuis un snapshot.
- **Live migration** — déplacer une VM en marche entre nodes, avec
  vérifications migration-info préalables.
- **Console série** — accès à une VM dans le navigateur.
- **Édition inline** — modifier CPU / mémoire / disques / réseaux et la
  charge cloud-init, puis appliquer.

La CLI expose le sous-ensemble start/stop via `harvester-status` /
`-shutdown -N <ns>`.

## 3. Observabilité cluster (console)

- **Topologie live** rendue avec Cytoscape sur trois vues : Cluster
  (nodes), Réseau, et Stockage (volumes Longhorn), avec click-pour-détail.
- **Métriques d'overview** : nodes, VMs en marche, nombre de volumes
  Longhorn et limite de rebuild, table des nodes.
- **`/metrics` Prometheus** — compteurs/durées d'actions, gauge in-flight,
  issues des appels kubectl.
- **`/healthz/ready`** — readiness probe renvoyant 503 si la config, les
  clusters ou la base d'actions sont en défaut (`/healthz` pour la
  liveness).

## 4. Cluster API — clusters RKE2 downstream (console)

Provisionner et opérer des clusters Kubernetes downstream sur Harvester
via le Cluster API Provider Harvester (CAPHV).

- **Installer la stack depuis un bundle airgap** — cert-manager, CAPI
  core, les providers RKE2 bootstrap/control-plane, CAPHV et une
  ClusterClass — avec progression pas à pas dans le dock.
- **Créer des clusters** via un wizard guidé (sizing, image, SSH, réseau,
  CNI), avec aperçu YAML (dry-run) avant apply.
- **Opérer** les clusters managés : scaler (patch la topologie),
  télécharger le kubeconfig, voir spec/conditions/machines, upgrades K8s,
  supprimer.
- **Gestion des bundles** — bundles airgap horodatés avec marqueur actif,
  inspect, upload, download, et contrôle de compatibilité avec la version
  Harvester.

## 5. Terraform — infrastructure as code (console)

Piloter le provider Terraform pour Harvester depuis des déclarations
sauvegardées.

- **Déclarations** — bundles nommés et persistés de N ressources
  hétérogènes (VMs, images VM, clés SSH, HCL brut), éditées section par
  section (Specs / Disques / Réseaux / Cloud-init) et appliquées en un
  coup.
- **Apply / destroy** avec streaming live du plan et de l'apply ; modale
  de confirmation typée sur chaque point d'entrée de destroy.
- **Éditer les ressources déployées** — chaque ressource appliquée écrit
  un sidecar JSON pour recharger et éditer sa spec d'origine depuis le
  sous-onglet Live.

## 6. Bare-metal (console)

- **Découverte BMC / Redfish** — pointer un ou plusieurs endpoints BMC et
  lire les profils des nodes (modèle, NICs, état d'alimentation).
- **Actions d'alimentation** via Redfish.
- **Provisionnement PXE / DHCP / HTTP** : socle (en cours).

## 7. Support aux opérations (CLI + console)

- **Config multi-cluster** — déclarer les clusters dans `config.yaml` ;
  ajouter / éditer / supprimer et uploader kubeconfig + clé SSH depuis la
  console ; tests de connexion kubeconfig et SSH.
- **Notes collaboratives** — notes rich-text synchronisées en live
  (Yjs + Tiptap) attachées par cluster et par node, synchronisées entre
  onglets et opérateurs.
- **Support bundles** — collecter logs et état cluster dans un tarball
  avec **anonymisation** (placeholders stables comme `<<NODE-1>>`,
  `<<IP-NODE-1>>`) ; un outil de dé-anonymisation séparé inverse
  l'opération depuis la table de mapping pour le passage au support.

## 8. Transversal

- **Tracking d'actions + dock** — un dock bas persistant montre les
  actions en cours et récentes sur chaque onglet, avec streaming live des
  steps/logs en SSE (reconnexion automatique). Une action en échec porte
  l'erreur sous-jacente (dernière ligne stderr `kubectl` / script) dans le
  dock, la table Activité et le panneau de détails — jamais un simple
  `exit 1`.
- **Historique d'actions durable** — les 500 derniers runs (avec leurs
  événements step/log) sont persistés en SQLite et resservis par l'onglet
  Activité et son replay de détails, y compris après redémarrage de l'UI.
  L'éviction mémoire n'affecte que l'attachement SSE live, jamais
  l'historique visible.
- **Internationalisation** — EN + FR complets ; IT / ES / DE retombent
  sur EN.
- **Thèmes** — 5 thèmes de couleur × sombre/clair.
- **Accessibilité** — focus clavier visible, focus trap de dialogue,
  tooltips sur chaque contrôle (désactivables globalement).

---

## Ce qui nécessite quoi

| Vous voulez… | Il vous faut |
|---|---|
| Juste un shutdown/startup sûr | Les scripts CLI seuls (pas d'UI, pas de podman) |
| Un dashboard + gestion VM | La console web |
| Provisionner des clusters downstream | Console web + un bundle airgap CAPHV |
| Des VMs gérées par Terraform | Console web + le binaire du provider Terraform |
| Un fonctionnement 100 % hors-ligne | Le tarball : wheels bundlés + image OCI |

Voir [architecture.md](architecture.md) pour la façon dont les surfaces se
posent sur le moteur partagé, et [installation.md](installation.md) pour
démarrer.
