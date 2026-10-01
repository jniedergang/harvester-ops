# Mise à jour de la console depuis l'interface (v1.82.0)

Demande : mettre à jour harvester-ops depuis l'interface, en ligne (la console
va chercher la dernière version) et hors ligne (on lui dépose l'archive).

## La contrainte qui décide de tout

La console tourne dans un conteneur **non root**, système de fichiers en
lecture seule. Elle ne peut ni charger une image, ni remplacer les scripts de
`/usr/local/bin`, ni redémarrer son propre service. Une mise à jour est donc
faite par un **agent côté hôte**, root, que la console ne fait que solliciter.

Conséquence de sécurité : l'agent exécute en root l'`install.sh` d'une archive
que la console (donc toute personne qui la prendrait en main) peut fournir.
**La seule chose qui protège l'hôte est la signature de l'archive, vérifiée par
l'agent** avec des clés que la console ne peut pas modifier. Une somme SHA-256
fournie par le même canal que l'archive ne prouve que l'intégrité du transfert.

## Composants

1. **Agent** `harvester-ops-update` (Python, bibliothèque standard), lancé par
   `harvester-ops-update.path` quand `/var/lib/harvester-ops/updates/request.json`
   apparaît (`harvester-ops-update.service`, `Type=oneshot`). Il :
   - retire la demande (pas de boucle de l'unité de chemin) ;
   - recopie l'archive et sa signature dans un répertoire root 0700 (la console
     ne peut plus les échanger après vérification), puis vérifie la signature
     sur la copie (`ssh-keygen -Y verify`, espace de noms
     `harvester-ops-release`), refuse un retour en arrière non demandé ;
   - extrait en refusant tout membre absolu ou en `..` ;
   - sauvegarde l'existant (image retaguée `harvester-ops:previous`, scripts,
     unités, `/opt/harvester-ops`) ;
   - lance `install.sh --upgrade` de la nouvelle version (non interactif :
     scripts, bibliothèques, image, fournisseurs embarqués, unités ; jamais la
     configuration, les comptes, les certificats) ;
   - redémarre `harvester-ops.service` et attend la nouvelle version en marche
     (`/healthz` + version lue dans le conteneur, 180 s) ;
   - sinon **revient en arrière tout seul** (fichiers, image, unités) et le dit ;
   - écrit chaque étape dans `updates/status.json`, lisible par la console.
2. **Clés de confiance** : `/etc/harvester-ops/update-signers` si l'exploitant
   en pose un (il prévaut), sinon `/opt/harvester-ops/update-signers` livré avec
   la version installée. Une archive non signée n'est acceptée que si root a
   écrit `allow_unsigned=true` dans `/etc/harvester-ops/update.conf`.
3. **Livrable** : `package.sh` signe l'archive quand une clé est fournie
   (`HARVESTER_OPS_SIGNING_KEY`) et écrit `release.json` (version, archive,
   SHA-256, signature, notes des dernières versions).
4. **Console** :
   - source en ligne réglable (par défaut les publications GitHub,
     `.../releases/latest/download/` ; un miroir interne est un simple
     répertoire HTTP qui sert les mêmes fichiers) ;
   - vérifier, télécharger (action suivie), ou recevoir une archive et sa
     signature par le navigateur (flux vers le disque, jamais en mémoire) ;
   - pré-vérification côté console (somme, signature, contenu, version) pour
     dire tôt ce qui n'ira pas, sans remplacer celle de l'agent ;
   - installer : refusé tant que des actions tournent (sauf à le forcer),
     confirmation, puis la page suit le redémarrage et se recharge sur la
     nouvelle version, ou affiche le retour en arrière ;
   - au démarrage, la console range l'issue de la dernière mise à jour dans
     l'activité (action `console-update`, avec le journal de l'agent).
5. **Interface** : onglet « Mise à jour » dans la fenêtre des versions (clic
   sur le numéro de version), réservé aux administrateurs pour agir.

## Interruption de service

Un processus unique : le redémarrage coupe la console quelques secondes (la
page se reconnecte seule). Les actions en cours mourraient avec elle, d'où le
refus par défaut. Les clusters ne sont pas touchés.

## Première fois

Une console d'avant 1.82.0 n'a pas d'agent : on passe à 1.82.0 avec
`install.sh` comme avant, les mises à jour suivantes se font par l'interface.
Une console lancée depuis les sources (développement) n'a pas d'agent non plus :
l'onglet le dit et n'offre pas d'installer.
