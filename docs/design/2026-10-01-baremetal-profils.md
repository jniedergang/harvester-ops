# Profils d'installation multi-nœuds (1.80.0)

## Le besoin

Un exploitant installe des dizaines de nœuds identiques avec un générateur
maison qui substitue les valeurs propres à chaque machine : nom d'hôte, IP
de gestion, adresses des autres réseaux (`address1=172.18.122.101/24` dans
les keyfiles NetworkManager d'`os.write_files`), `ListenAddress` de sshd.
La fenêtre d'installation (1.77, 1.78) installe une machine à la fois, tout
ressaisi à chaque fois.

## Décisions (prises, 01/10/2026)

| # | Sujet | Retenu |
|---|---|---|
| 1 | Profil | Configuration nommée : champs de la fenêtre sans identifiants de BMC ni valeurs propres à un nœud, plus le YAML avancé ; toute chaîne peut porter `{{nom}}` |
| 2 | Variables | Intégrées : `hostname`, `ip`, `mgmt_mac`, `vip` ; plus celles que déclare le profil |
| 3 | Secrets | Jamais dans un profil : saisis au lancement, ou repris d'un fichier importé (cache serveur de 15 min existant) ; jamais écrits |
| 4 | Rangement | `<état>/profiles.d/<nom>.yaml`, 0600, nom RFC 1123, déplaçable comme `clusters.d` |
| 5 | Série | Profil + tableau de machines + nom de cluster + VIP ; ligne 1 crée, les suivantes rejoignent `https://<vip>:443` une fois la ligne 1 terminée, 2 à la fois ; une action parente `baremetal-batch:<cluster>` |
| 6 | Rendu | Côté serveur ; variable absente = refus avec ligne et nom, avant d'allumer quoi que ce soit ; aperçu par ligne ; mêmes contrôles qu'une installation |
| 7 | CLI | `harvester-baremetal profile list|show|apply`, secrets par fichier 0600 ou entrée standard |

## Conception

### Éditeur réduit plutôt que la fenêtre d'installation en mode profil

La fenêtre d'installation se construit autour d'UNE machine découverte :
cartes réseau cochées par MAC lue sur son BMC, tableau des disques tiré de
son inventaire, contrôles des rôles sur cet inventaire. Un profil n'a pas
de machine ; ses MAC et ses disques sont justement ce qui varie. Un mode
« profil » aurait dû neutraliser la moitié de la fenêtre. L'éditeur retenu
prend les champs en YAML (mêmes noms que le corps de
`/api/baremetal/install`, gabarit proposé), le YAML avancé, et peut partir
d'un fichier de configuration (découpé comme à l'import, secrets jetés).
L'enregistrement fait un rendu d'essai complet (chaque variable vaut « 1 ») :
une clé inconnue ou réservée est refusée tout de suite.

### Substitution

- Champs : remplacement dans chaque chaîne, listes et dictionnaires compris.
- YAML avancé : le texte n'est **pas** substitué puis relu. Chaque variable
  devient d'abord un jeton neutre (lettres et chiffres), le YAML est lu,
  puis les jetons sont remplacés dans les chaînes. Une valeur portant `: `,
  `#` ou des guillemets arrive telle quelle, y compris dans un bloc
  `content: |`, et un `{{ip}}` nu ne se lit pas comme un dictionnaire de
  flux. Une chaîne qui n'était qu'une variable prend le type entier ou
  booléen du schéma de l'installeur à cet endroit (`vlan_id: {{vlan}}`).
- Valeurs : une ligne, 512 caractères au plus, sans caractère de contrôle
  (un saut de ligne ajouterait une ligne dans un keyfile ou `sshd_config`).

### Série

- La route de lancement contrôle **toutes** les lignes avant de créer
  l'action : substitution, puis `_bm_prepare_install`, les contrôles de
  `/api/baremetal/install` sortis de la route (schéma, disques sur
  l'inventaire de découverte du BMC, pools, nom du cluster). Une jonction
  est contrôlée sans son cluster, qui n'existe pas encore ; elle l'est de
  nouveau, complètement, à son lancement.
- Le déroulé (`bm_profiles.run_batch`, partagé avec la ligne de commande) :
  ligne 1 seule, attente de sa fin ; si elle n'est pas `done`, les autres
  sont sautées ; sinon les jonctions partent dans l'ordre, `concurrency` à
  la fois (2 par défaut, 4 au plus). Chaque ligne est une action
  `baremetal-install:<hostname>` lancée par `_bm_track`, avec le runner
  existant : pas de nouveau runner d'installation.
- L'action parente montre une étape `node-<n>` par machine (nom, BMC, mode,
  identifiant de son installation) et `result.nodes`. Son arrêt est transmis
  à l'installation en cours ; les lignes non commencées sont sautées.
- Les secrets vivent dans le plan en mémoire du déroulé, vidé à la fin ; ni
  le libellé, ni les étapes, ni le résultat n'en portent.

### CSV

Première ligne : `bmc_host`, `bmc_user`, puis les variables ; séparateur
virgule, point-virgule ou tabulation. Une colonne inconnue est refusée. La
route de la fenêtre lit une colonne `bmc_password` puis la jette (un mot de
passe ne revient pas au navigateur) ; la ligne de commande la refuse : les
mots de passe vont dans le fichier de secrets (`bmc_password`,
`bmc_passwords` par hôte).

### Ligne de commande

`profile apply` charge `web/app.py` dans le processus et appelle
`_bm_batch_start` : même contrôle, même déroulé, mêmes actions (persistées
dans la base des actions de la console). `--port` évite le port du serveur
d'artefacts d'une console qui tourne sur le même hôte.

## Vérification

- Tests (`tests/api/test_bm_profiles_180.py`) : substitution (variable
  absente, valeur à syntaxe YAML, variables dans `write_files`, type
  entier), magasin (0600, rien d'absolu, déplacement), ordre de la série
  (création seule, deux jonctions au plus, création ratée, arrêt), CSV,
  routes (authentification, admin, limites), secrets jamais rangés ni
  renvoyés, ligne de commande.
- e2e (`tests/e2e/test_bm_profiles_180.py`) : liste, éditeur, fenêtre de
  série (CSV, aperçu par ligne, lancement) sur des routes simulées.
- **Non vérifié en réel** : aucune série n'a tourné sur des machines. Chaque
  ligne est l'installation déjà vérifiée seule (création et jonction) ;
  l'enchaînement l'est par les tests seulement. À faire sur le banc de
  node2 (deux nœuds imbriqués derrière l'émulateur Redfish), sur accord.
