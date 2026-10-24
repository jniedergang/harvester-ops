# Navigation par hôtes et par VMs (conception)

Date : 24/10/2026, relue avec le propriétaire du produit le 25/10/2026. Base : 1.99.0. Maquette : `2026-10-24-navigation-maquette.html`.

Décisions du propriétaire du produit du 24/10/2026, reprises telles quelles :
- un **hôte** est un cluster Harvester entier ou un hyperviseur KVM, jamais un nœud Harvester ;
- les hôtes sont montrés à **un seul endroit**, en deux familles : Harvester et KVM ;
- chaque hôte ouvre un sous-menu : ses VMs, **tous** ses paramètres, et un sous-menu « Disaster Recovery » ;
- un menu racine « Disaster Recovery » montre tous les contrats ; le sous-menu d'un hôte montre les mêmes données, filtrées ;
- **toutes** les VMs (Harvester et KVM) dans une seule vue, rangées dans une arborescence de dossiers propre à l'exploitant ;
- l'appartenance à un dossier est écrite **sur la VM**, qui fait foi ; la base de la console sert de cache et de repli ;
- la liste des hôtes **remplace** le sélecteur de cluster en haut du menu, avec une recherche rapide.

Décisions de la relecture du 25/10/2026 :
- l'ordre du menu de la maquette est gardé, et **toutes** les vues de configuration déjà construites (nœuds, stockage, réseau, add-ons, sécurité, monitoring, avancé, mise à jour, alimentation, bare-metal) retrouvent une place, sans perte de fonction ;
- les paramètres d'un hôte sont des **onglets** dans la page ;
- les dossiers rangent les **VMs, les conteneurs et les hôtes**, dans **une seule arborescence partagée**, ouverte par une entrée « Dossiers » en haut du menu ;
- un objet est dans **un seul** dossier, sans héritage : ranger un hôte ne range pas ses VMs (un bouton propose de les ranger aussi) ;
- supprimer un dossier non vide est **refusé**, la fenêtre propose « déplacer le contenu vers... » ;
- créer, renommer, ranger : rôle opérateur ;
- la nouvelle navigation arrive en **bascule directe**, dans une version majeure **2.0.0**, sans interrupteur ancienne/nouvelle.

## Objectifs

1. Une console multi-hôtes, pas une console « un cluster à la fois ». On ne choisit plus un cluster avant de travailler : on va à un hôte, ou à une VM.
2. Deux familles, un même geste. Un hôte Harvester et un hôte KVM s'ouvrent de la même façon, avec les mêmes entrées.
3. Rien ne se perd. Chaque onglet d'aujourd'hui a une place, écrite dans le tableau plus bas.
4. Des liens directs partout. Chaque écran a une adresse `#...` que l'on peut copier, recharger, envoyer.
5. Tenir à 60 clusters et 40 hôtes KVM sans menu de trois écrans de haut.
6. Livrer en une bascule annoncée (2.0.0), sans perdre un écran ni un lien : les anciens jetons d'adresse restent compris.
7. Vérifier avant d'agir, isoler, revenir en arrière (principes ci-dessous).

## Principes d'expérience (propriétaire du produit, 25/10/2026)

Ils valent pour chaque écran de la nouvelle navigation, et pour ceux qu'elle accueille (réplication, KVM) :
- **Vérifier avant d'agir.** Chaque geste montre dans sa fenêtre les contrôles qui le concernent, avant le bouton : droits de la personne (revue d'accès du serveur, `SelfSubjectAccessReview`, ou dry-run du webhook), capacité technique (place allouable, version, fonction présente), réseau (joignabilité, débit mesuré quand il compte). Un contrôle échoué dit pourquoi et ce qu'il faut faire.
- **Une interface légère.** Le menu reste court, la page montre l'essentiel ; le détail va dans des onglets et des fenêtres en overlay (`.modal-overlay`), qui isolent un geste ou une vue.
- **Des info-bulles claires** sur chaque contrôle, en une phrase, dans les cinq langues.
- **Traçabilité et retour arrière.** Chaque geste mutatif est une action suivie (dock, Activité). Quand le geste est réversible, l'action garde l'état d'avant et propose « Annuler » : un déplacement de dossier, un renommage, un rangement en lot.

## Ce qui existe aujourd'hui (1.96.0)

- En haut du menu, `#cluster-select` choisit le cluster courant. `App.setCluster()` le garde dans `localStorage` et recharge la vue affichée.
- Le groupe **Cluster** porte neuf entrées qui lisent toutes ce cluster courant : Overview, Storage, Network, Add-ons, Security, Monitoring & Logging, Advanced, Startup, Shutdown.
- Les groupes **Virtual machines** et **Containers** ont chacun une vue « cluster courant » et une vue « tous les clusters » (1.85.0, 1.91.0).
- **Shared resources** (GPU, Bookings, Usage) est déjà une vue de flotte.
- **KVM** (1.96.0) a ses quatre vues de flotte : Hosts, Virtual machines, Storage, Networks.
- **VM Import / Export** mêle des vues par cluster (VM Import, VMware migrations) et une vue de flotte (Migrations, all clusters).
- **Automation** (Cluster API, Terraform, Bare-metal) et **Activity** sont des entrées de flotte.
- Les `href="#overview"` existent, mais aucun routeur ne lit l'adresse : F5 rend le dernier onglet par `localStorage`, et un lien envoyé à quelqu'un n'ouvre rien de précis.

Le constat : la console est déjà à moitié une console de flotte, mais son menu reste organisé autour d'un cluster choisi d'avance, et KVM vit à part.

## Nouvelle arborescence du menu

```
[ Rechercher un hôte ou une VM ]          (raccourci « / »)
Machines virtuelles                        toutes les VMs, dossiers
Conteneurs                                 toutes les charges de conteneurs
Hôtes                                      page index de tous les hôtes
  Harvester (3)       [replier]
    * harv-paris      pastille d'état, nombre de VMs
      harv-lyon
      harvlab
  KVM (2)             [replier]
      kvm-paris-01
      kvm-paris-02
  + Ajouter un hôte
Disaster Recovery                          tous les contrats
Migrations                                 toutes les vagues et transferts
Ressources partagées
  GPU / Réservations / Utilisation
Automatisation
  Cluster API / Terraform / Bare-metal
Activité
```

L'hôte ouvert se déplie sous son nom (un seul à la fois, comme un accordéon) :

```
harv-paris                                 kvm-paris-01
  Vue d'ensemble                             Vue d'ensemble
  Machines virtuelles                        Machines virtuelles
  Conteneurs                                 Paramètres
  Imports et migrations                      Disaster Recovery
  Paramètres
  Disaster Recovery
```

Les paramètres d'un hôte ne sont pas dans le menu latéral. Ils sont dans la page, en onglets (`.sub-tabs-inline`, motif déjà utilisé par Storage et Network). Le menu reste court même avec un hôte ouvert.

Onglets de **Paramètres** d'un hôte Harvester : Nœuds, Stockage, Réseau, Add-ons, Sécurité, Monitoring et journaux, Avancé, Mise à jour, Alimentation, Connexion.

Onglets de **Paramètres** d'un hôte KVM : Système (versions de libvirt et de QEMU, CPU, mémoire), Pools, Réseaux, Étiquettes, Connexion (test, retrait).

Documentation, réglages, compte et version restent dans la barre du haut, sans changement.

Pourquoi « Machines virtuelles » en tête : c'est l'objet que l'on cherche le plus souvent. Les hôtes viennent ensuite, parce que c'est par eux qu'on administre.

## Correspondance des onglets : aujourd'hui, demain

| Aujourd'hui | Demain |
|---|---|
| Sélecteur de cluster (`#cluster-select`) | Liste des hôtes dans le menu, recherche rapide |
| Bouton `+` à côté du sélecteur | Hôtes, « + Ajouter un hôte » (choix Harvester ou KVM) |
| Cluster > Overview > Metrics | Hôte Harvester > Vue d'ensemble |
| Cluster > Overview > Cluster (blocs des nœuds) | Hôte Harvester > Paramètres > Nœuds |
| Cluster > Overview > Events | Hôte Harvester > Vue d'ensemble, bloc Événements |
| Cluster > Overview, bouton Upgrade | Hôte Harvester > Paramètres > Mise à jour |
| Cluster > Storage (volumes, images, classes) | Hôte Harvester > Paramètres > Stockage |
| Cluster > Network (7 onglets) | Hôte Harvester > Paramètres > Réseau |
| Cluster > Add-ons | Hôte Harvester > Paramètres > Add-ons |
| Cluster > Security | Hôte Harvester > Paramètres > Sécurité |
| Cluster > Monitoring & Logging | Hôte Harvester > Paramètres > Monitoring et journaux |
| Cluster > Advanced (réglages, PCI, USB, SR-IOV, paquet de support) | Hôte Harvester > Paramètres > Avancé |
| Cluster > Startup, Shutdown | Hôte Harvester > Paramètres > Alimentation (Démarrage, Arrêt, ordre des VMs) |
| Virtual machines > Current cluster's VMs (espaces de noms, Backups, modèles, configurations cloud, fenêtre Namespaces, archives de transfert, créer) | Hôte Harvester > Machines virtuelles, mêmes boutons |
| Virtual machines > All clusters VMs | Machines virtuelles (racine), avec dossiers, KVM compris |
| Containers > Current cluster's containers | Hôte Harvester > Conteneurs |
| Containers > All clusters containers | Conteneurs (racine) |
| Shared resources > GPU, Bookings, Usage | Ressources partagées, inchangé |
| KVM > Hosts (bloc par hôte, Tester, Retirer, Déclarer) | Hôtes > KVM, et Hôte KVM > Vue d'ensemble ; Tester et Retirer dans Paramètres > Connexion ; Déclarer dans « + Ajouter un hôte » |
| KVM > Virtual machines | Machines virtuelles (racine, filtre KVM) et Hôte KVM > Machines virtuelles |
| KVM > Storage | Hôte KVM > Paramètres > Pools |
| KVM > Networks | Hôte KVM > Paramètres > Réseaux |
| VM Import / Export > VM Import | Hôte Harvester > Imports et migrations > Imports |
| VM Import / Export > VMware migrations | Hôte Harvester > Imports et migrations > VMware |
| VM Import / Export > Migrations (all clusters) | Migrations (racine) |
| Automation > Cluster API, Terraform | Automatisation, inchangé |
| Automation > Bare-metal | Automatisation > Bare-metal ; « Joindre un nœud » aussi depuis Paramètres > Nœuds, cluster déjà choisi |
| Activity | Activité, inchangé ; filtre par hôte ouvert depuis la Vue d'ensemble d'un hôte |
| Settings > Clusters (connexions) | Inchangé, et raccourci dans Paramètres > Connexion de chaque hôte |
| (nouveau) | Disaster Recovery (racine) et Hôte > Disaster Recovery |
| (nouveau) | Hôtes : page index, tableau de tous les hôtes des deux familles |

Aucun écran n'est réécrit pour cette navigation. Les sections de `sections.js` reçoivent leur cluster de la route au lieu du sélecteur ; les vues KVM reçoivent un filtre d'hôte.

## Adresses et liens directs

Les adresses ne portent que des jetons d'ancre `#...`, sans chemin côté serveur, sans paramètre de requête. Un segment est passé par `encodeURIComponent`. Les noms de cluster et d'hôte sont déjà validés (RFC 1123 pour Harvester, `NAME_RE` pour KVM).

| Écran | Adresse |
|---|---|
| Toutes les VMs | `#vms` |
| Un dossier | `#vms/folder/<id du dossier>` |
| Les VMs non rangées | `#vms/folder/unfiled` |
| Toutes les VMs, panneau Hôtes, un hôte | `#vms/host/harvester/<cluster>`, `#vms/host/kvm/<hôte>` |
| Toutes les charges de conteneurs | `#containers` |
| Index des hôtes | `#hosts`, une famille : `#hosts/harvester`, `#hosts/kvm` |
| Vue d'ensemble d'un hôte | `#harvester/<cluster>` ou `#kvm/<hôte>` |
| VMs d'un hôte | `#harvester/<cluster>/vms` |
| Paramètres d'un hôte, un onglet | `#harvester/<cluster>/params/storage`, `#kvm/<hôte>/params/pools` |
| Sous-onglet d'un paramètre | `#harvester/<cluster>/params/network/overlay` |
| DR d'un hôte | `#harvester/<cluster>/dr` |
| Tous les contrats DR | `#dr` |
| Un contrat | `#dr/<id du contrat>` |
| Migrations, ressources partagées, automatisation, activité | `#migrations`, `#gpus`, `#bookings`, `#usage`, `#automation/capi`, `#activity` |

La famille fait partie de l'adresse : un cluster et un hôte KVM peuvent porter le même nom sans ambiguïté.

Compatibilité : les jetons d'aujourd'hui restent compris. `#storage` devient `#harvester/<dernier hôte Harvester>/params/storage`, `#allvms` devient `#vms`, `#kvmhosts` devient `#hosts`. Un lien vers un hôte que la personne ne voit pas (retiré, ou hors de ses droits) ouvre l'index des hôtes avec un message, pas une page vide.

Un routeur unique (`hashchange` et chargement) remplace la restauration par `localStorage`. F5 rend exactement l'écran affiché. Les choix d'interface (sous-onglet préféré, groupes repliés) restent dans `localStorage`.

## L'hôte courant remplace le cluster courant

Aujourd'hui, `currentCluster` est une variable globale lue par des dizaines d'appels. Demain :
- **la route fait foi** : sur une page d'hôte, l'hôte vient de l'adresse ; `App.getCurrentCluster()` rend le cluster de la route ;
- **les vues de flotte n'ont pas d'hôte courant** : Machines virtuelles, Conteneurs, DR racine, Migrations, Ressources partagées, Activité ;
- **le dernier hôte visité** est gardé dans `localStorage`, pour deux usages seulement : traduire un ancien jeton (`#storage`), et proposer une valeur par défaut aux pages d'automatisation qui demandent un cluster de gestion (Cluster API, Terraform). Ces pages affichent ce cluster dans leur en-tête, et la personne peut le changer ;
- changer d'hôte est une navigation : le voile de bascule (`Veil`) et la remise à zéro des refus (`AccessNotice.reset()`) restent, déclenchés par le routeur.

Point de passage unique : `setCluster(name)` devient une fonction interne du routeur. Les modules qui l'appellent aujourd'hui gardent leur code ; seule la source du nom change.

## Grande flotte : 60 clusters et 40 hôtes KVM

Le menu ne doit jamais montrer 100 lignes d'un coup. Règles :
- **Recherche** en tête du menu, raccourci `/`. Elle filtre les hôtes par nom, étiquette et famille, et propose les VMs qui correspondent (« Aller à la VM »). Entrée ouvre le premier résultat.
- **Favoris** : une étoile sur chaque hôte. Les favoris restent en tête de leur famille. Ils sont rangés par personne, dans la base de la console (comme les préférences de compte), pas dans le navigateur : on les retrouve sur un autre poste.
- **Hôtes en alerte toujours visibles** : un hôte injoignable ou en erreur n'est jamais caché par le plafond. La tête de famille résume l'état (`58 ok, 2 en alerte`).
- **Familles repliables**, état gardé dans `localStorage`.
- **Index des hôtes** (`#hosts`) : un tableau de tous les hôtes, triable et filtrable (famille, état, étiquette, version), avec les mêmes gestes que le menu.
- **Coût** : l'état des hôtes vient des lectures partagées existantes (`@shared_read`, 1.81.0) ; le menu ne lance aucune lecture par hôte. Le compte de VMs vient de la même lecture que la vue Machines virtuelles.

Regroupement par site ou par étiquette dans le menu : écarté pour la première version (voir les questions ouvertes).

## Toutes les VMs et les dossiers

### Vue

- Une liste, sur le schéma de ligne commun prévu en 1.97.0 (`kind`, `source`, `namespace`, `name`, `phase`, `cpu`, `memory_bytes`, `node`, `ips`, `labels`, `uuid`, `key`).
- Une pastille de famille sur chaque ligne (Harvester, KVM), l'hôte, l'espace de noms quand il existe, l'état, le CPU, la mémoire, les IP, et une pastille DR si la VM est dans un contrat.
- À gauche, un panneau à deux modes, comme vCenter : **Dossiers** (l'arborescence de l'exploitant) et **Hôtes** (regroupement automatique par famille puis par hôte, en lecture seule).
- Deux dossiers virtuels, non modifiables : « Toutes les VMs » et « Non rangées ».
- Les gestes d'une VM (démarrer, arrêter, console, migrer...) restent ceux de chaque famille : le menu d'une ligne est composé selon `kind`, comme prévu en 1.97.0.

### Modèle de données

Deux choses distinctes :
1. **L'arborescence** : les dossiers, leur parent, leur ordre, les dossiers vides. Elle vit dans la base de la console (`folders.db`, SQLite en WAL, comme `bookings.db`) : `folders(id, parent_id, name, position, created_at, created_by)`.
2. **L'appartenance d'une VM** : écrite sur la VM, qui fait foi.
   - Harvester : annotation `harvester-ops.io/folder` sur l'objet `VirtualMachine`, valeur = chemin du dossier, par exemple `/Production/ERP`.
   - KVM : élément de métadonnées libvirt dans un espace de noms propre, `<hops:folder xmlns:hops="https://harvester-ops.io/xmlns/folder/1" path="/Production/ERP"/>`, posé avec `virsh metadata --uri ... --key hops --set ... --config` (et `--live` si le domaine tourne).
   - Cache : table `memberships(vm_key, folder_path, source, seen_at)`, où `source` vaut `vm` (lu sur la VM) ou `console` (repli).

Pourquoi un chemin et pas un identifiant sur la VM : un chemin se relit sans la console. Si la base est perdue, la console reconstruit l'arborescence à partir des VMs. Le prix : renommer ou déplacer un dossier réécrit toutes ses VMs. C'est une action suivie, une écriture par VM, reprise là où elle s'est arrêtée.

### Qui fait foi

- Lecture : si la VM porte une valeur, elle gagne. Le cache est mis à jour à chaque lecture de la flotte. Un chemin inconnu de l'arborescence y est créé (dossier marqué « recréé depuis les VMs »).
- Écriture par une personne qui a le droit d'écrire la VM : la VM est écrite, puis le cache.
- Écriture par une personne qui a le rôle opérateur dans la console, mais pas le droit d'écrire la VM sur le cluster ou l'hôte : seule la base change (`source = console`). La ligne le dit (pastille « rangement connu de la console seulement »). Un opérateur qui a le droit pourra ensuite écrire ces rangements sur les VMs, en une action.
- Deux consoles sur les mêmes VMs : la dernière écriture sur la VM gagne, l'autre console s'aligne à sa prochaine lecture.

### Identité d'une VM au fil des déplacements

- Harvester : clé `harvester/<cluster>/<espace de noms>/<nom>`. Un transfert vers un autre cluster (1.45.0) emporte le manifeste et donc l'annotation : la VM réapparaît dans le même dossier sous sa nouvelle clé. Une VM restaurée depuis une sauvegarde garde aussi l'annotation.
- KVM : clé `kvm/<uuid>`. Le dossier suit le domaine à travers un renommage et une migration d'un hôte à l'autre (les métadonnées font partie du XML). Un clone garde ses métadonnées : il naît dans le dossier de sa source, ce qui est le comportement attendu.
- Une VM supprimée disparaît de la vue. Son entrée de cache est purgée après 30 jours sans être vue, pour qu'un hôte injoignable un moment ne vide pas ses dossiers.

### Conteneurs et hôtes dans les dossiers

L'arborescence est la même pour les trois sortes d'objets. Un dossier peut mêler VMs, conteneurs et hôtes ; la vue « Dossiers » les montre en trois groupes, les vues Machines virtuelles, Conteneurs et Hôtes filtrent par dossier.
- Conteneur : la charge (Deployment, StatefulSet, DaemonSet) porte l'annotation `harvester-ops.io/folder`, comme une VM Harvester. Clé `container/<cluster>/<espace de noms>/<sorte>/<nom>`.
- Hôte : un hôte est déjà un objet de la console (`clusters.d/`, `kvm.d/`). Son dossier est écrit dans sa déclaration, qui fait foi ; rien n'est écrit sur le cluster ou l'hyperviseur. Clé `host/harvester/<nom>` ou `host/kvm/<nom>`.

### Rôles

| Geste | viewer | operator | admin |
|---|---|---|---|
| Voir les dossiers et leur contenu | oui | oui | oui |
| Créer, renommer, ranger un dossier | non | oui | oui |
| Déplacer des objets | non | oui | oui |
| Supprimer un dossier vide | non | oui | oui |

Supprimer un dossier non vide est refusé ; la fenêtre propose de déplacer d'abord son contenu vers un autre dossier, en une action, puis de supprimer.

Les VMs visibles restent celles que l'identité de la personne voit sur chaque cluster (Rancher ou kubeconfig délégué). Un dossier peut donc sembler vide pour une personne et plein pour une autre. La vue ne compte pas les VMs cachées : elle ne révèle rien de ce que la personne ne voit pas.

### Gestes

- Glisser une ou plusieurs lignes sur un dossier du panneau.
- Menu « Déplacer vers un dossier » sur une ligne et sur la sélection (cases à cocher), avec recherche de dossier.
- Créer, renommer, supprimer un dossier : dans le panneau, en saisie sur place, sans fenêtre du navigateur.
- Avant d'appliquer, la fenêtre de déplacement dit, objet par objet, si la personne peut écrire l'objet (revue d'accès) ou si seul le rangement de la console changera.
- Chaque déplacement est une action suivie (dock, Activité), une ligne par objet touché, avec le résultat de chaque écriture. L'action garde les dossiers d'origine et propose « Annuler ».

## Disaster Recovery : menu racine et sous-menu d'hôte

Un seul modèle, deux vues, sur le moteur de réplication de la 1.99.0 :
- **Racine** (`#dr`) : tous les contrats. Par contrat : nom, source, cible, état, RPO tenu (pire ressource) contre l'objectif, nombre de ressources par état, trois derniers événements, actions. Un contrat se déplie sur ses ressources.
- **Hôte** (`#harvester/<cluster>/dr`) : les lignes de ressource de cet hôte, comme source ou comme cible, avec une colonne « côté » et l'hôte d'en face.
- Les deux vues lisent les mêmes fonctions (`plan_row()`, `resource_row()`) par `GET /api/replication/plans` et `GET /api/replication/hosts/<hôte>/resources`. Ce que montre un hôte est exactement ce que montre la racine pour ses ressources.
- Le journal d'une ressource vient de `GET /api/replication/resources/<uid>/events`. Chaque événement renvoie à son action dans l'Activité quand il en a une.
- Les actions sont rendues par le serveur avec leur rôle requis et leur droit (`allowed`). Une action non permise reste visible, grisée, avec sa raison. Aucune règle d'état n'est recopiée dans le navigateur.
- Hôte KVM : le sous-menu existe dès la première version, avec un état vide qui le dit (« la réplication des domaines KVM n'est pas encore disponible »). Il se remplira quand `resource_kind = kvm-domain` sera livré, sans changer la forme.

Vocabulaire : l'interface dit « contrat », le code et l'API disent `plan` (décision 3).

## Étapes de livraison

La nouvelle navigation arrive d'un bloc en **2.0.0** (décision du 25/10/2026). Le travail reste découpé, sur une branche, en étapes qui ont chacune leurs tests :

| Étape | Contenu | Publication |
|---|---|---|
| 1 | Routeur par ancre et liens directs dans l'interface actuelle. Anciens jetons compris. Un F5 et un lien rendent l'écran exact. | 2.0.0 (décision du 25/10/2026 : rien ne sort seul) |
| 2 | Nouveau menu : liste des hôtes, recherche, favoris, sous-menu d'hôte qui monte les sections existantes en onglets, index des hôtes. KVM entre dans la liste. | 2.0.0 |
| 3 | Vue unique des VMs (Harvester et KVM), panneau Hôtes, dossiers partagés (VMs, conteneurs, hôtes) : lecture, écriture, annuler. | 2.0.0 |
| 4 | Disaster Recovery racine et par hôte, en reprenant les écrans de réplication. | 2.0.0 |

Pas d'interrupteur ancienne/nouvelle navigation : l'ancien menu disparaît en 2.0.0. Ce qui protège les habitudes : les anciennes adresses (`#storage`, `#kvm-hosts`...) restent comprises pour toujours, la doc EN et FR et le CHANGELOG décrivent la correspondance (tableau ci-dessus), et la mise à jour reste réversible par le retour arrière de l'agent de mise à jour (1.82.0).

Tests à prévoir : routeur (chaque adresse du tableau, anciens jetons, hôte inconnu, hôte hors des droits) ; menu avec 100 hôtes (plafond, alertes jamais cachées, recherche) ; dossiers (objet qui fait foi, repli console, renommage repris après un arrêt, transfert entre clusters, clone KVM, rôle par geste, suppression refusée, annuler) ; parité des cinq langues ; listes d'onglets des tests e2e mises à jour. Vérification réelle : annotation posée et relue sur harv1, métadonnées libvirt sur un hôte KVM de banc, transfert harvlab vers harvlab2 avec dossier conservé.

## Risques

- **`currentCluster` est partout.** Des dizaines d'appels le lisent. Le routeur doit le poser avant que la vue ne charge. Sinon on revoit le défaut de 1.4.31 (onglet restauré, toile vide). Parade : un seul point d'entrée, et un test qui charge chaque adresse à froid.
- **Une annotation est une modification de la VM.** Elle apparaît dans l'historique de l'objet et change son `resourceVersion`. À vérifier en réel : aucun redémarrage demandé, aucune condition « restart required ». Et surtout : le suivi de réplication détecte une dérive de spec (`drifting`). Il doit comparer la `generation` de la spec, que les métadonnées ne changent pas, et non le `resourceVersion`. À confirmer avec le code de la 1.97.0 avant l'étape 3.
- **Droits d'écriture absents.** Avec Rancher, beaucoup de personnes lisent sans écrire. Le repli console devient alors la règle, pas l'exception. Il doit rester lisible (pastille, action « écrire sur les VMs »).
- **Renommer un gros dossier** réécrit des centaines de VMs. Action suivie, reprise, débit borné.
- **KVM transitoire** : un domaine non persistant perd ses métadonnées à l'arrêt. Le cache console garde le rangement, marqué comme tel.
- **Habitudes** : des personnes connaissent le menu actuel par cœur, et la bascule est directe. Parade : anciens jetons compris pour toujours, correspondance dans le CHANGELOG et la doc EN et FR, retour à la version précédente possible par l'agent de mise à jour.
- **Tests e2e** : les listes d'onglets figées dans les tests changent. Voulu : un onglet non placé doit faire échouer.

## Décisions du propriétaire du produit (24 et 25/10/2026)

1. Dossier écrit sur l'objet sous forme de **chemin** (lisible, reconstructible ; renommer réécrit les objets, en action suivie).
2. Favoris **par personne**, dans la base de la console.
3. « Contrat » dans l'interface, `plan` dans l'API.
4. Regroupement par site ou étiquette dans le menu : **plus tard** ; l'index des hôtes filtre déjà par étiquette.
5. **Pas de plafond** d'hôtes par famille dans le menu (décision du 25/10/2026) : tous les hôtes sont listés, familles repliables, recherche et favoris.
6. Conteneurs : **entrée racine**, et onglet Conteneurs dans le sous-menu d'un hôte Harvester.
