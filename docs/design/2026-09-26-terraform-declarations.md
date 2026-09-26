# Déclarations Terraform : gardées par la console, un état chacune

État : moteur livré en v1.54.0, interface en v1.55.0. 26/09/2026.

## Pourquoi

Demande de l'exploitant : « une interface améliorée pour l'onglet
Terraform ? On ne peut pas renommer les déclarations. On est sûr que tout
fonctionne ? ». L'audit réel sur harv1 a relevé 18 défauts (les pires
corrigés en 1.52.1). Deux venaient de la conception même :

- **Les déclarations ne vivaient que dans le navigateur** (localStorage) :
  ni partagées entre opérateurs, ni sauvegardées avec la console, perdues
  avec le profil du navigateur.
- **Toutes les déclarations d'un cluster partageaient un seul état
  Terraform** : appliquer une déclaration appliquait tout le cluster, deux
  VMs du même nom dans deux déclarations écrivaient le même fichier, et le
  plan de l'une modifiait la VM de l'autre.

Décisions de l'exploitant (les deux recommandations) : **un état par
déclaration**, **déclarations gardées par la console**.

## Le modèle

- Une déclaration est une ligne SQLite (`tf-declarations.db`, à côté des
  notes et de l'historique : `/var/lib/harvester-ops` dans le service,
  `~/.local/share/harvester-ops` en développement) : identifiant de 12
  caractères hexadécimaux, cluster, nom unique dans le cluster (sans égard à
  la casse), description, ressources, **révision**, auteurs et dates, et le
  résultat du dernier apply ou destroy (`web/tf_store.py`).
- **Renommer** ne change que le nom : l'état Terraform et les ressources
  ne bougent pas, les adresses Terraform viennent du nom des ressources.
- **Révision** : chaque modification part avec la révision lue ; si
  quelqu'un est passé entre-temps, la console répond 409 avec la version
  courante, que la page affiche en le disant (pas d'écrasement silencieux).
- **Espace de travail** : `<espaces>/<cluster>/decls/<id>/`, son propre
  `terraform.tfstate`. L'espace partagé du cluster (`<espaces>/<cluster>/`)
  garde les ressources appliquées hors déclaration (chemin `/apply`) et
  celles d'avant la v1.54 tant que leur déclaration ne les a pas reprises.
- **Reprise** : au premier plan d'une déclaration, les ressources qu'elle
  décrit et qui sont dans l'état partagé passent dans son état (fichier
  d'état format 4, copie de chaque fichier avant écriture, `web/tf_state.py`),
  leurs `.tf` et `.json` quittent l'espace partagé. Rien n'est recréé sur le
  cluster.
- **Déclaratif** : l'espace suit la déclaration ; une ressource retirée de la
  déclaration voit ses fichiers disparaître et le plan annonce sa
  destruction, que l'apply exécute.
- **Plan revu, puis appliqué** : le plan est enregistré (`tfplan`) avec
  l'empreinte du contenu rendu (`tfplan.meta`) et un résumé lisible
  (création, modification réglage par réglage, remplacement, destruction ;
  valeurs sensibles masquées). Un apply qui porte cette empreinte applique
  CE plan sans replanifier ; si la déclaration a changé depuis, il est
  refusé (409, « plan outdated »).
- **Détruire une ressource à l'unité** (vue en direct) : dans l'espace de
  sa déclaration, et elle sort de la déclaration (sinon le prochain apply la
  recréait, audit D8).
- **Supprimer une déclaration** : refusé tant que son état contient des
  ressources déployées ; sinon sa ligne et son espace disparaissent.
- **Tout détruire** (onglet des ressources du cluster) : l'espace partagé et l'état de
  chaque déclaration du cluster.
- **Reprise des déclarations du navigateur** : à la première visite, celles
  que le localStorage garde encore partent à la console avec leur
  identifiant ; un nom déjà pris reçoit un suffixe ; une copie reste sous
  `harvester_ops_tf_declarations_imported`.

## API

- `GET /api/tf-declarations[?cluster=]`, `POST /api/tf-declarations`
  (`id` facultatif, pour la reprise), `GET|PUT|DELETE /api/tf-declarations/<id>`.
  Chaque déclaration rendue porte l'adresse Terraform de chaque ressource,
  ce qui est déployé (`deployed`) et le dernier plan (`last_plan`).
- `POST /api/terraform/<cluster>/apply_declaration` avec
  `{declaration: {id}, dry_run, plan_hash?}` ; `destroy_declaration` avec
  `{declaration: {id}}`. Sans identifiant, l'ancien comportement (état
  partagé) reste pour les clients de l'API qui s'en servent.
- `destroy_resource` accepte `declaration_id`.
- v1.55.0 : `GET /api/tf-declarations/<id>/code` (le code produit, fichier par
  fichier) et `GET /api/tf-declarations/<id>/history` (ses actions, en cours
  comprises ; chaque action d'une déclaration porte son identifiant, son
  auteur et son mode dans son résultat). La vue d'une déclaration porte
  aussi l'empreinte de son contenu (`content_hash`).

## Essai réel (harv1, 26/09/2026)

Une clé appliquée par l'ancien chemin (état partagé), puis une déclaration
qui la décrit : le plan l'a reprise (1 à créer, 1 inchangée, rien de
recréé) ; apply du plan revu ; une seconde déclaration avec une VM, créée ;
le plan de la première ne voyait que la sienne ; renommée, plan vide ; une
clé retirée de la déclaration, détruite par l'apply ; la VM détruite à
l'unité, sortie de sa déclaration, son disque parti ; suppression refusée
tant qu'une clé était déployée, puis destruction et suppression, espaces
effacés. L'essai a attrapé un défaut que les tests ne voyaient pas : la
route refusait une déclaration envoyée par son seul identifiant.

## L'interface (v1.55.0)

D'après la maquette validée par l'exploitant (« Onglet Terraform,
proposition ») :

- **Une vue au lieu de fenêtres empilées** (une par déclaration, puis une par
  section) : la liste à gauche, la déclaration choisie à droite, onglets
  Ressources, Code, Historique ; les formulaires s'ouvrent dans la vue,
  toutes les sections à la suite, avec un contrôle pendant la saisie.
- **États** : d'une déclaration (jamais appliquée, à jour, N à appliquer,
  modifiée depuis l'apply, erreur) et de chaque ressource (à créer,
  déployée, N réglages à changer ou à remplacer d'après le dernier plan
  encore valable, retirée donc détruite au prochain apply, incomplète).
- **Le plan se lit avant d'appliquer** : fenêtre de plan, ressource par
  ressource et réglage par réglage ; « Appliquer ce plan » envoie son
  empreinte, la console refuse s'il ne vaut plus. Le bouton « Appliquer »
  rouvre le dernier plan tant qu'il vaut pour le contenu écrit.
- **Cinq langues partout**, le schéma compris : libellés lisibles, nom
  Terraform en petit, bulle d'aide sur chaque champ (audit D14, D15).
- Détruire tout ouvre le journal (D11) ; une ressource de l'espace partagé
  s'adopte dans une déclaration depuis l'onglet des ressources du cluster.

### Essai réel (harv1)

Par les seuls clics : déclaration créée par le bouton, une clé et une VM
ajoutées par les formulaires (contrôle vert), plan lisible « 2 à créer »,
plan appliqué, VM et clé sur le cluster ; renommée sans rien toucher ;
mémoire passée de 1 à 2 Gio, le plan l'a montrée avant et après, appliquée
par le bouton « Appliquer » sur le plan relu, 2Gi lu sur la VM ; clé retirée,
plan « 1 à détruire », clé détruite ; historique des six passages ;
destruction par confirmation tapée, VM et disque partis ; déclaration
supprimée.
