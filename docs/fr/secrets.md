# Magasin de secrets (OpenBao, 2.9.0)

English version: [../en/secrets.md](../en/secrets.md).

Par défaut, la console garde ses secrets dans des fichiers locaux de son
répertoire d'état : kubeconfigs des clusters, clés SSH, secret client OIDC
de Rancher, secrets des notifications et des actions annexes de
réplication, variables Terraform des déclarations KVM, secrets d'un plan de
déploiement en cours. Depuis la 2.9.0, elle peut les garder dans OpenBao à
la place, classe par classe, et revenir aux fichiers locaux à tout moment.

## Deux modes

| Mode | Où tourne OpenBao | À qui il appartient | Quand le choisir |
|---|---|---|---|
| **A** | votre propre OpenBao (ou un Vault compatible) | à vous : politiques, sauvegardes, descellement | vous faites déjà tourner un serveur de secrets |
| **B1** | un conteneur à côté de la console, sur le même hôte, `127.0.0.1:8210` | à l'agent de l'hôte de la console | vous voulez sortir les secrets du répertoire d'état sans faire tourner de serveur |

Le mode B1 emploie l'image OpenBao de la SUSE Application Collection
(`dp.apps.rancher.io/containers/openbao:2.7.2-5.4`, abonnement Rancher
Prime). harvops ne la redistribue pas : vous construisez un **paquet
OpenBao** à part, avec votre propre compte de service de l'Application
Collection (voir [Construire le paquet B1](#construire-le-paquet-b1)).

Rien ne change tant que le magasin reste en mode local : la 2.9.0 sans
OpenBao se comporte comme la 2.8.3. Seule différence : `install.sh` installe
désormais les unités de l'agent du magasin de secrets (une unité path et une
minuterie horaire, qui ne font que rafraîchir un fichier de santé).

## Ce qui peut passer dans OpenBao

| Classe | Emplacement local | Migrée |
|---|---|---|
| `kubeconfig` | `clusters.d/<n>.yaml`, `kubeconfigs/<n>.yaml` | oui, proposée seulement après la migration d'une autre classe et une panne simulée réussie |
| `ssh-cluster` | `ssh/<n>_id` (clé privée) | oui (`.pub` et `known_hosts` restent locaux) |
| `ssh-kvm` | `ssh/<host>_id` | oui, avec rotation |
| `rancher-oidc` | `rancher.d/<id>.oidc-secret` | oui |
| `replication-notify` | base de réplication, réglages des notifications | oui, champ par champ |
| `replication-hook` | en-têtes secrets des actions annexes de réplication | oui |
| `tf-kvm` | `<workspace>/.secrets/<variable>` | oui |
| `plan-run` | mémoire (2.4.0) | gardés dans OpenBao pendant le déroulé d'un plan, effacés à la fin (7 jours au plus pour un déroulé arrêté) |

Toujours locaux : les comptes de la console, `setup-token`, `htpasswd`, la
clé TLS de la console, toutes les clés publiques, les identifiants de
connexion à OpenBao eux-mêmes, et les kubeconfigs écrits dans `config.yaml`
(ils appartiennent à l'installateur).

Dans OpenBao, un secret vit à `<mount>/data/<prefix>/<class>/<path>`, avec
par défaut le montage `harvester-ops` et le préfixe `consoles/<console id>`.
Chaque écriture porte des `custom_metadata` (`hops_action`, `hops_actor`,
`hops_class`), jamais une empreinte de la valeur.

## Activer le mode A

Paramètres > Magasin de secrets > Activer OpenBao ouvre l'assistant.

1. **Serveur** : adresse (`https://`, `http` seulement sur la boucle
   locale), la CA si elle est privée, l'adresse de la console vue par
   OpenBao (CIDR), le montage et le préfixe. L'assistant montre les
   commandes `bao` à lancer sur votre serveur et le texte exact de la
   politique. La console ne crée rien hors de son préfixe.
2. **Vérifications** : adresse et certificat, état de scellement, et le
   `secret_id` emballé (son chemin de création doit être le rôle de la
   console). Avant la première activation, la connexion est marquée
   « après » : `enable` dépose les identifiants AppRole par l'agent de
   l'hôte, puis vérifie la connexion, KV v2, les capacités de chaque
   chemin, une écriture d'essai sous `<prefix>/check/`, transit et le rôle
   de jeton d'OpenTofu. Il n'active que si tout est vert. Les identifiants
   laissés sur l'hôte par un serveur précédent ne servent jamais à la
   vérification avant activation.
3. **Confirmation** : activer.

Le `secret_id` est toujours remis **emballé**. L'agent examine le jeton
d'enveloppe, vérifie son chemin de création, le déballe et écrit
`/etc/harvester-ops/openbao/secret-id` (`root:harvester-ops 0640`). Un jeton
déjà déballé ou expiré donne `openbao-unwrap-used` : quelqu'un d'autre a
peut-être lu le secret.

Vault : l'API est la même (KV v2, AppRole, enveloppe, transit). Vault 1.14.2
a été vérifié comme OpenBao. OpenTofu ne documente son fournisseur de clés
`openbao` qu'avec Vault 1.14 : avec un Vault plus récent, l'onglet État vous
demande de l'acquitter avant que les états soient chiffrés (Vault 1.17.6 a
chiffré des états sans erreur dans nos essais).

## Activer le mode B1

### Construire le paquet B1

Sur une machine qui joint l'Application Collection (ou votre miroir) :

```bash
harvester-secrets bundle --registry dp.apps.rancher.io \
    --user <service account> --password-stdin -o ./bundles/openbao/ < password-file
```

L'outil tire l'image lui-même (bibliothèque standard, pas de `podman
login`). Il vérifie sa signature cosign avec la clé publique de
l'Application Collection et épingle l'empreinte de l'index. Il écrit
`harvester-ops-openbao-<version>-<tag>.tar.gz` (environ 59 Mo, image de
202 Mo une fois chargée) avec son `.sha256`, sa provenance et la signature
telle que servie par le registre. Une étiquette autre que `2.7.2-5.4`
(épinglée) n'est acceptée qu'avec une signature embarquée valide. Un
OpenBao plus ancien que 2.7.2 est refusé.

Placez le paquet sous `bundles/openbao/` de la version de harvops avant
`install.sh`, ou gardez-le sur l'hôte.

### Poser B1

- Depuis la console : Paramètres > Magasin de secrets > Activer OpenBao,
  mode B1, choisir le paquet qu'`install.sh` a placé.
- Sur l'hôte : `sudo /usr/local/bin/harvester-secrets enable --mode b1
  --bundle <file>` (sur openSUSE, le `secure_path` de `sudo` ne contient pas
  `/usr/local/bin`).

L'agent crée le compte `harvester-ops-openbao` et charge l'image. Il écrit
l'unité `harvester-ops-openbao.service` (`--read-only`, `--network host`,
`--memory 512m`, toutes les capacités retirées), une CA et un certificat de
serveur. Il crée une clé de descellement statique
(`/etc/harvester-ops-openbao/unseal.key`, root 0400, remise au conteneur
comme secret podman). Puis il démarre OpenBao avec une initialisation
déclarative : aucun jeton root n'est jamais rendu, aucune clé de
récupération n'existe.

### Copie hors ligne de la clé de descellement

Après l'installation et après chaque rotation de la clé de descellement, la
clé en service attend sa copie hors ligne. Passer une classe dans OpenBao
est refusé (`offline-copy-ack-required`) tant qu'elle n'est pas acquittée :

1. Exporter (taper l'identifiant de la console) : l'agent remet une copie.
2. La télécharger une fois (`no-store` ; un second téléchargement répond
   410).
3. Acquitter avec l'identifiant de la clé. Une mauvaise clé est refusée.

Gardez le fichier loin des instantanés : un instantané sans sa clé ne peut
pas être restauré, et la clé sans l'instantané ne sert à rien.

## Migrer les classes et revenir

Paramètres > Magasin de secrets > Classes, ou `harvester-secrets migrate
--to openbao|local --class <c>`. Chaque secret est écrit avec `cas=0`,
relu, comparé par SHA-256 en mémoire. Puis la valeur locale est remplacée
par une référence (`openbao:<class>/<path>#<field>`). L'original est gardé
sous `<file>.migrated` (0600) pendant 7 jours (de 1 à 30,
`originals_keep_days`), ou jusqu'à **Confirmer**. **Revenir en local**
réécrit la valeur là où elle était et retire la copie d'OpenBao.

`kubeconfig` est proposée en dernier : une autre classe doit avoir migré et
**Tester la panne** doit avoir réussi.

## Cache et pannes

Les valeurs lues dans OpenBao sont gardées en mémoire 15 minutes (de 1 à
120). Jamais sur disque : les copies de travail remises à `kubectl`, `ssh`
ou `tofu` sont des fichiers 0600 sur tmpfs (`/tmp/hops-secrets` dans le
conteneur), effacés après l'action.

| Ce qui arrive | Ce que vous voyez |
|---|---|
| OpenBao arrêté, valeur en cache | l'action passe ; bandeau « OpenBao ne répond pas, depuis HH:MM ; servi depuis la mémoire jusqu'à HH:MM » |
| OpenBao arrêté, cache expiré ou vidé | `503 openbao-unreachable: <class>/<name> (<path>: ...)` |
| scellé | `openbao-sealed` |
| jeton révoqué sur le serveur | la console se reconnecte par AppRole, l'action passe |
| politique restreinte | `openbao-denied ... (capabilities: deny)` pour les classes hors de la politique |
| journal d'audit plein | `openbao-internal` après deux réponses 500, bandeau de panne ; `sys/health` dit encore descellé |

Il n'y a pas de copie locale de repli. Les connexions à un cluster déjà
ouvertes par la console continuent de fonctionner jusqu'à leur fermeture,
même quand le kubeconfig ne peut plus être relu. **Vider le cache** agit
sur le processus de la console.

## Rotation

| Secret | Comment |
|---|---|
| `secret_id` de la console | tous les 30 jours par l'agent de l'hôte (minuterie horaire), ou **Faire tourner** |
| clé de descellement de B1 | sur demande (taper l'identifiant) : nouvelle clé, redémarrage avec les deux, `sys/rotate/root`, ancienne clé retirée et gardée 14 jours pour les instantanés qui en ont besoin |
| clé SSH d'un hôte KVM | sur demande ; âge affiché, signalée après 365 jours ; nouvelle clé ajoutée, essayée, rangée, ancienne clé retirée et vérifiée refusée ; un échec au milieu remet l'état précédent |
| clé transit d'une déclaration | sur demande ; les états plus anciens restent lisibles |
| secrets que la console ne possède pas (OIDC, jeton Matrix, kubeconfig) | âge affiché ; les remplacer là où ils se règlent |

Après une rotation de clé KVM, un retour à une version ou une restauration
lancés depuis la console, son cache est vidé.

Versions (Paramètres > Magasin de secrets > Versions) liste qui a écrit
quelle version (auteur, action), jamais une valeur. L'onglet peut ramener
un secret à une version plus ancienne. Ramener la clé SSH d'un hôte KVM à
une version antérieure à sa dernière rotation donne une clé que l'hôte
refuse déjà.

## Instantanés (B1)

- Répertoire : `/var/lib/harvester-ops-snapshots` sur l'hôte, monté dans le
  conteneur de la console (créé s'il manque). Vous pouvez y monter un
  partage (NFS sous SELinux :
  `context="system_u:object_r:container_file_t:s0"`). Un partage en
  root-squash fonctionne : l'agent le lit sous le compte de la console. Le
  répertoire d'état est refusé (il est sauvegardé avec la console).
- Planification : chaque jour à `snapshot_time` (02:30 par défaut, **UTC**,
  l'horloge du conteneur), 14 gardés. Un instantané en échec est retenté
  après une heure et affiché dans État ; il n'est jamais relancé à chaque
  passage.
- Fichiers `openbao-<UTC stamp>-<key id>.snap` (0600). Un instantané est
  chiffré par la barrière : les valeurs et les textes des politiques ne
  sont pas lisibles, les noms des clés de stockage le sont.
- `sudo harvester-secrets snapshot` est refusé (il agirait en root sur
  l'état de la console) : `sudo -u harvester-ops harvester-secrets snapshot
  --dest DIR`.

### Restaurer

Paramètres > Magasin de secrets > Instantanés > Restaurer (taper
l'identifiant). Avant tout arrêt, la console liste ce que la restauration
casserait, et vous l'acquittez :

- les actions en cours (elles s'arrêtent avec la console) ;
- les déclarations chiffrées dont l'état a été écrit après l'instantané :
  la clé transit naît au premier chiffrement (souvent bien après la
  déclaration) et une rotation ajoute une version, donc un tel état peut
  demander une clé que l'instantané n'a pas et devenir illisible (un état
  réécrit avec une clé qui existait déjà est listé aussi, sans danger) ;
- les hôtes KVM dont la clé SSH a été tournée depuis (OpenBao rendrait une
  clé que l'hôte refuse) ;
- les classes passées dans OpenBao après l'instantané (leurs secrets n'y
  sont pas).

L'agent arrête alors la console et OpenBao. Il met le volume actuel de côté
pour 7 jours (`/var/lib/harvester-ops-openbao.old-<UTC stamp>`), démarre
une instance neuve avec la clé de l'instantané et le restaure. Puis il
relit la configuration restaurée face à une **liste permise** de ce que
cette console attend : noms et textes exacts des politiques (la politique
`default` comparée à l'empreinte notée à l'installation), méthodes
d'authentification `token` et `approle` seulement, les deux rôles AppRole
avec leurs politiques, plages d'adresses et `secret_id` unique, le rôle de
jeton d'OpenTofu, aucun groupe d'identité, des entités d'identité sans
politique dont les alias sont les deux rôles AppRole ou `tf-<déclaration>`
sur `token/`, aucun espace de noms, les montages et le périphérique d'audit
attendus. Toute différence refuse l'instantané et remet le volume
précédent. Ensuite, chaque jeton de l'instantané est révoqué ; la console
et l'agent se reconnectent. Les politiques de l'agent et du jeton
d'OpenTofu sont réécrites avant la relecture : un instantané qui ne fait
que les élargir est réparé, pas refusé.

Un alias d'entité `tf-<déclaration>` est accepté pour tout nom bien formé,
pas seulement pour les déclarations vivantes : un instantané pris avant le
retrait d'une déclaration garde son entité, et le rôle de jeton accepte
`tf-*` parce que la console crée l'alias d'une nouvelle déclaration à sa
première action. Une telle entité ne porte aucune politique : elle ne donne
rien de plus que la politique des jetons d'OpenTofu.

Une restauration à la main (`sudo harvester-secrets restore <file>`) ne
vérifie pas les actions en cours de la console.

## États OpenTofu chiffrés

Avec OpenBao actif et OpenTofu (pas Terraform), l'état de chaque
déclaration est chiffré par le moteur transit : une clé par déclaration, un
jeton enfant limité à cette clé et à l'action. Les états en clair existants
sont migrés à l'action suivante.

Limites connues :

- **La migration demande le provider et le cluster** : elle passe par
  `apply -refresh-only`. La première action d'une déclaration après
  l'activation (même un plan) rafraîchit donc et écrit l'état, dérive
  comprise. Un cluster injoignable fait échouer cette action à
  `encrypt-state` tant que la migration ne peut pas passer.
- **Les copies d'avant un déplacement restent en clair dans l'espace
  partagé** : quand des ressources sont reprises par une déclaration
  chiffrée, la copie `.pre-move-*` de l'espace partagé du cluster reste en
  clair (l'espace partagé n'est pas une déclaration). Retirez-la avec le
  nettoyage de l'espace.
- **Une lecture peut créer un jeton** : la vue d'une déclaration (rôle
  lecteur) dont le cache d'adresses est périmé demande à OpenBao un jeton
  enfant, et peut créer la clé transit. Ce n'est pas une action suivie.
- **Remettre en clair, puis toute action rechiffre** : le bouton sert
  « avant de désactiver OpenBao » ; il n'y a pas de refus par déclaration
  tant qu'OpenBao est actif. `disable` refuse tant qu'un état est chiffré.
- Les déplacements d'état passent par `state pull` / `state push -` ;
  `-state-out` est refusé (il écrit la destination en clair).
- Les clés transit d'une déclaration supprimée sont gardées 30 jours, puis
  retirées par le balayage quotidien, sauf si la déclaration existe de
  nouveau avec le même identifiant. La console peut retirer les clés
  `hops-<console id>-*` de son propre préfixe.

## Secrets d'un plan de déploiement

Pendant le déroulé d'un plan, les secrets tapés pour lui restent dans
OpenBao (`plan-run/<action id>/...`) : un plan arrêté reprend sans les
retaper. Ils sont effacés à la fin du déroulé, après 7 jours pour un
déroulé arrêté, ou par **Oublier les secrets gardés**.

## Audit et corrélation

Chaque requête porte `X-Hops-Action` (l'identifiant de l'action de la
console, ou `bg-<task>`) et `X-Hops-Actor` (la personne, ou `service`),
notés en clair dans le journal d'audit d'OpenBao. En B1, le journal d'audit
est déclaré dans la configuration. OpenBao refuse les requêtes qu'il ne
peut pas journaliser : la console avertit donc sous 1 Gio libre ou 10 % du
volume.

## Rôles

- Écritures sur `/api/secrets/*` et toute action du magasin de secrets :
  administrateur.
- `GET /api/secrets/status` : opérateur (la carte Disaster Recovery).
- Le bandeau : tout le monde.
- Les actions du magasin de secrets (`secrets:*`) nomment des secrets et
  des chemins : elles ne sont montrées dans le dock, le détail des actions,
  le flux et l'Activité qu'aux administrateurs.

## Désactiver, changer de mode, revenir à une console plus ancienne

- `disable` est refusé tant qu'une classe est dans OpenBao ou qu'un état
  est chiffré. Il efface d'abord les secrets des déroulés de plan arrêtés.
  `disable --remove-b1` retire aussi le conteneur B1, son unité, son compte
  et son volume.
- De B1 vers A : ramener toutes les classes (`migrate --to local`),
  `disable --remove-b1`, puis activer le mode A.
- **Revenir à une console antérieure à la 2.9.0** (Mise à jour > Revenir
  en arrière, ou Installer en autorisant une version plus ancienne) est
  refusé tant que le magasin n'est pas local, qu'une classe est dans
  OpenBao ou qu'un état est chiffré (`secrets-in-openbao`). Le refus donne les étapes
  à faire d'abord : ramener toutes les classes, remettre en clair les états
  chiffrés, désactiver le magasin. Une console 2.8.x lirait les références
  `openbao:` comme des chemins ou des valeurs : les clusters au kubeconfig
  migré échouent (une action `status` se dit même terminée alors que
  kubectl a échoué), les clés SSH sont introuvables, les notifications
  envoient la référence comme jeton. `install.sh` lancé à la main avec une
  version plus ancienne n'est pas gardé : suivez d'abord les mêmes étapes.
  Le conteneur B1 et les unités de l'agent de la 2.9.0 continuent de
  tourner après une telle installation ; les originaux `.migrated` restent
  sur disque jusqu'à leur expiration.

### Ce qui reste après `disable --remove-b1` et après un retour arrière

`disable --remove-b1` arrête et retire l'unité B1 et son secret podman, et
met les données de côté (jamais supprimées sur le moment) :

- gardés exprès : `/var/lib/harvester-ops-openbao.removed-<stamp>`,
  `/etc/harvester-ops-openbao.removed-<stamp>` (clés de descellement) et les
  volumes `.old-<stamp>` des restaurations précédentes, les instantanés de
  `/var/lib/harvester-ops-snapshots` ;
- laissés en place : le compte `harvester-ops-openbao`, les images
  `localhost/harvester-ops-openbao:*` et l'image de l'Application
  Collection, le paquet dans `/var/lib/harvester-ops/secrets-agent/staged/`,
  le répertoire vide `/etc/harvester-ops/openbao`.

Une console ramenée en 2.8.x ne connaît pas les unités du magasin de
secrets de la 2.9.0 : `harvester-secrets-agent.path` et
`harvester-secrets-rotate.timer` restent actives (elles ne font que
rafraîchir un fichier de santé une fois le magasin local). Pour tout retirer
à la main, quand vous n'avez plus besoin des instantanés ni des clés :

```bash
sudo systemctl disable --now harvester-secrets-agent.path harvester-secrets-rotate.timer
sudo rm -f /etc/systemd/system/harvester-secrets-agent.{path,service} \
           /etc/systemd/system/harvester-secrets-rotate.timer /usr/local/bin/harvester-secrets*
sudo systemctl daemon-reload
sudo rm -rf /opt/harvester-ops/openbao /etc/harvester-ops/openbao \
            /var/lib/harvester-ops-openbao.* /etc/harvester-ops-openbao.* \
            /var/lib/harvester-secrets-agent /var/lib/harvester-secrets-agent.lock
sudo podman rmi localhost/harvester-ops-openbao:current localhost/harvester-ops-openbao:previous
sudo userdel harvester-ops-openbao
```

Si ces secrets peuvent encore servir, gardez d'abord ailleurs une copie du
dernier instantané et de sa clé de descellement. `uninstall.sh` retire
lui-même les unités ; `uninstall.sh --purge` retire aussi les données et
les clés de B1.

## Ligne de commande

`harvester-secrets` (lancé sous le compte de la console, ou avec `sudo` ;
en root, il ne fait lui-même que l'appel à l'agent et lance le reste dans
un processus enfant ramené à `harvester-ops`) : `status`, `assistant`,
`check`, `enable`, `disable`, `migrate`, `confirm`, `outage-test`,
`versions`, `rollback`, `rotate`, `offline-copy`, `snapshot`, `snapshots`,
`settings`, `sweep-orphans`, `restore`, `bundle`, `upgrade-b1`, `file`,
`tf-encryption`, `tf-entity-remove`, `tf-keys`.

`upgrade-b1 --bundle <file>` : nouvelle image B1 depuis un paquet, avec
d'abord un instantané d'avant mise à jour, et retour à l'image précédente si
OpenBao ne se descelle pas ; un OpenBao plus ancien est refusé.

Un outil lancé à la main lit la racine des espaces Terraform que la console
a notée au démarrage (`<state>/console-paths.json`), sauf si
`HARVESTER_OPS_TF_WORKSPACES` est posée.

## Non vérifié

- OpenBao en cluster chez un client, Vault Enterprise avec namespaces.
- La rotation des clés SSH d'un parc de plusieurs hôtes KVM (un hôte
  vérifié).
- La phase GitOps d'un plan de déploiement arrêté puis repris (un plan avec
  des secrets Git a été arrêté et repris sans les retaper, sans la phase
  GitOps).
- La rotation du `secret_id` à 30 jours déclenchée par la minuterie
  (rotation sur demande vérifiée ; la minuterie n'a rafraîchi que la santé).
- L'avertissement d'espace d'audit de B1 avant le refus sur un volume plein
  (le refus et son code `openbao-internal` vérifiés en mode A).
- Une console qui a le même identifiant qu'une autre sur un serveur de mode
  A partagé (clone, restauration) : chacune balaie les clés et les
  orphelins de l'autre.
