# Bare-metal : installer Harvester sur une machine vierge

L'onglet Bare-metal transforme un serveur vide en node Harvester
opérationnel sans que personne aille jusqu'à la baie, branche une clé USB
ou clique dans l'installeur. La console pilote la machine par son BMC.

Rien de magique ni de spécifique ici : c'est le mode d'installation
zéro-touch de Harvester, piloté de bout en bout par la console.

---

## Ce qu'il faut avant de commencer

| Prérequis | Pourquoi | Comment le vérifier |
|---|---|---|
| Le BMC parle Redfish | Tout passe par lui | La découverte liste le node |
| Le BMC expose un **média virtuel CD** | L'ISO est monté par le réseau | La carte du node propose **Installer Harvester** ; sinon elle affiche *pas de média virtuel* |
| Le BMC sait amorcer sur `Cd` | La machine doit démarrer une fois sur cet ISO | Même indication que ci-dessus |
| La console est joignable **depuis le BMC** | C'est le BMC qui télécharge l'ISO | Un `curl` vers la console depuis une machine du réseau BMC |

Sur les iLO 4/5 de HPE, le média virtuel est une fonction **iLO
Advanced**. Sans cette licence le BMC n'expose aucun lecteur CD, et la
console le dit tout de suite plutôt que d'échouer trente secondes après
le début d'une installation.

La carte du node indique aussi combien de disques le firmware sait
amorcer. S'il n'en voit aucun, la machine n'a pas de cible
d'installation exploitable et il n'y a rien à installer dessus.

---

## Le déroulé

```
console (votre serveur)                        machine cible
  ISO Harvester officiel ──remaster──► ISO patché
        │                                      │
        │  HTTP simple, jeton à usage unique
        ▼                                      ▼
  /pxe/iso/<jeton>.iso     ◄──── BMC : insertion du média virtuel (URL)
  /pxe/config/<jeton>.yaml ◄──── l'installeur y lit ses réponses
        │
        └─ Redfish : amorce unique sur Cd, mise sous tension, install auto
```

### 1. Amener un ISO dans le magasin

**Images d'installation** télécharge un ISO **côté serveur, en flux**.
Le fichier ne transite jamais par votre navigateur : un ISO Harvester
pèse environ 7,6 Go, et un upload de cette taille depuis un navigateur
serait tamponné en mémoire à l'arrivée.

Collez l'URL de l'éditeur, lancez le téléchargement, suivez le
pourcentage dans le dock. Le magasin montre ce qui est sur le disque et
combien de place il reste.

L'ISO n'est **volontairement pas** embarqué dans le tarball de release :
le toolkit entier pèse 135 Mo, l'ISO à lui seul cinquante fois plus.

### 2. Découvrir le BMC

Saisissez une ou plusieurs adresses de BMC, un utilisateur et un mot de
passe. Les identifiants ne sont **jamais stockés côté serveur** : ils
restent dans la page le temps de la session, et chaque action les
renvoie.

Chaque BMC joignable revient sous forme de carte : modèle, numéro de
série, BIOS, mémoire, NICs avec leurs adresses MAC, état d'alimentation,
et la possibilité ou non d'installer la machine.

> **Attention avec une machine éteinte.** Un BMC ne relit pas le matériel
> hors tension : il rejoue l'inventaire du **dernier POST**. Sur une
> machine qui n'a pas démarré depuis des mois, cet inventaire peut être
> vieux de plusieurs mois. L'installation commence donc par un préflight
> qui allume la machine et lit la réalité avant de décider quoi que ce
> soit.

### 3. Renseigner le node

**Installer Harvester** ouvre un formulaire :

- **Image** : quel ISO du magasin.
- **Node** : nom d'hôte, disque d'installation (`/dev/sda`...), interface
  de management (choisie parmi les NICs qui viennent d'être découvertes),
  adressage (statique ou DHCP), IP / masque / passerelle, VIP du cluster,
  DNS.
- **Accès** : le token du cluster, le mot de passe OS, et éventuellement
  des clés publiques SSH.

Le token et le mot de passe n'apparaissent jamais dans une réponse, dans
un libellé d'action, ni dans une ligne de log.

### 4. Suivre l'installation

L'installation est une action trackée comme les autres : elle apparaît
dans le dock dès la confirmation et diffuse ses étapes.

| Étape | Ce qui se passe |
|---|---|
| `preflight` | Allume la machine, attend le POST, lit l'inventaire **réel** |
| `remaster` | Patche l'ISO pour qu'il s'installe sans opérateur (voir plus bas) |
| `serve` | Publie l'ISO et la configuration derrière des jetons à usage unique |
| `bmc-insert` | Monte l'ISO en média virtuel |
| `bmc-boot` | Programme une amorce **unique** sur `Cd` |
| `power-on` | Redémarre la machine sur l'ISO |
| `wait-install` | Attend que le node s'installe et redémarre |
| `wait-api` | Attend l'API Harvester sur la VIP |

L'amorce unique compte : la machine démarre l'ISO **une seule fois**,
puis reprend son ordre d'amorçage normal et se relève sur le système
fraîchement installé. Rien à défaire à la main ensuite.

---

## Pourquoi l'ISO est remasterisé

Le mode sans opérateur de Harvester s'active depuis la **ligne de
commande du noyau** :

```
harvester.install.automatic=true
harvester.install.config_url=<url>
ip=dhcp rd.neednet=1
```

Un média virtuel monte une image telle quelle ; il n'offre aucun moyen
d'y ajouter des arguments noyau. D'où la remasterisation.

**Pourquoi les arguments réseau comptent.** L'installeur va chercher sa
configuration à `config_url` *avant* de configurer le moindre réseau. Une
installation PXE a déjà un réseau, monté par le paramètre `ip=` du noyau.
Le même ISO démarré depuis un média virtuel n'en a aucun : sans
`ip=dhcp rd.neednet=1`, l'installeur reste là indéfiniment sans jamais
émettre une seule requête, et sans rien afficher qui l'explique. Ces deux
arguments sont toujours injectés.

**Comment l'injection se fait.** L'ISO officiel n'a qu'une configuration
grub porteuse des entrées de menu ; celle de l'EFI ne fait que la
charger. Ses entrées se terminent déjà par `${extra_iso_cmdline}`, une
variable qu'elle source depuis `/boot/grub2/harvester.cfg`. Poser cette
seule variable suffit, et cela vaut pour toutes les entrées et les deux
firmwares à la fois. Sur un ISO plus ancien dépourvu de cette variable,
les arguments sont ajoutés directement aux lignes de noyau.

**Comment l'image est reconstruite.** Elle ne l'est pas : elle est
recopiée avec `xorriso -boot_image any replay`, qui rejoue le dispositif
d'amorçage d'origine. C'est essentiel, parce que l'amorce El Torito de
cet ISO est **UEFI seulement et cachée** (ce n'est pas un fichier de
l'arborescence) : une image reconstruite par `mkisofs` ne démarrerait
pas du tout. La copie évite aussi de déballer 7,7 Go : l'étape prend
environ deux minutes.

Trois choses sont vérifiées sur l'image produite, chacune échouant
silencieusement sinon :

- le label de volume est toujours **`COS_LIVE`** : le noyau monte son
  système de fichiers racine via `root=live:CDLABEL=COS_LIVE`, donc
  perdre le label produit un ISO qui démarre puis ne se retrouve plus ;
- l'amorce El Torito a survécu ;
- les arguments d'installation sont bien dans l'image.

Si l'ISO source ne porte aucune entrée grub, la remasterisation refuse au
lieu de produire une image silencieusement inerte.

## Regarder une installation qui ne dit rien

L'installeur tourne sur la première console de la machine. Rien n'en
remonte vers la console qui l'a lancée : une installation bloquée
ressemble exactement à une installation lente. Deux arguments noyau
aident, tous deux disponibles dans la section **Avancé** du formulaire :

- `console=ttyS1,115200` renvoie l'installation sur le port série virtuel
  du BMC, auquel on peut ensuite s'attacher. Vérifier quel port COM votre
  BMC expose : sur les iLO 4 de HPE c'est `Com2`, d'où `ttyS1`, et le
  réglage BIOS `SerialConsolePort` doit d'abord être mis à `Virtual`.
- `harvester.install.skipchecks=true` passe outre les contrôles matériels.
  Sans lui, une machine qui échoue à un prérequis minimal abandonne
  l'installation et ne le dit que sur cette console invisible.

---

## Pourquoi un second serveur HTTP

La console sert en **HTTPS avec un certificat auto-signé** et exige une
authentification sur tout. Un BMC qui va chercher un ISO ne sait faire ni
l'un ni l'autre : il ne s'authentifie pas, et il ne fait pas confiance à
ce certificat.

Le serveur d'artefacts est donc un listener séparé et minimal, en HTTP
simple (port 8091 par défaut). Ce n'est pas un serveur de fichiers : il
n'expose que deux chemins, chacun adressé par un jeton aléatoire à usage
unique et à durée de vie limitée, révoqué dès la fin de l'installation.

```
GET /pxe/iso/<jeton>.iso
GET /pxe/config/<jeton>.yaml
```

Il gère les requêtes `Range` de HTTP, parce que les BMC téléchargent les
images de plusieurs gigaoctets par morceaux.

Ce port doit être joignable **depuis le réseau du BMC**. Sur un hôte avec
pare-feu, pensez à l'ouvrir.

---

## Dépannage

**« pas de média virtuel » sur la carte du node.** Le BMC n'expose aucun
lecteur CD ou ne sait pas amorcer dessus. Sur iLO, vérifier la licence
Advanced. Aucun contournement depuis la console : sans média virtuel, la
machine doit être installée autrement.

**La découverte renvoie un inventaire vide.** La machine est éteinte et
n'a jamais POSTé depuis le dernier redémarrage du BMC. L'allumer,
attendre le POST, redécouvrir.

**Le BMC ne va jamais chercher l'ISO.** Il n'atteint pas le serveur
d'artefacts : vérifier la route depuis le réseau du BMC vers la console,
et le pare-feu de cet hôte.

**La machine démarre l'ISO mais attend un opérateur.** Les arguments
noyau ne sont pas arrivés dans le chargeur réellement utilisé
(typiquement : EFI patché et pas le BIOS hérité, ou l'inverse). L'étape
de remasterisation indique combien de `grub.cfg` elle a patchés ; un ISO
Harvester sain en donne au moins deux.

**L'installation se déroule mais l'API ne répond jamais sur la VIP.** La
VIP est sur un autre sous-réseau que l'interface de management, ou elle
est déjà prise. Vérifier qu'elle est libre avant de lancer.

---

Voir [capabilites.md](capabilites.md) pour le reste du toolkit et
[architecture.md](architecture.md) pour l'articulation des surfaces.
