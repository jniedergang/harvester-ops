# Paramétrer les réseaux kube-ovn depuis la console (VPC, subnets, overlay)

État : livré en 1.49.0, 25/09/2026. Chantier décidé par l'exploitant le 20/09
(« tout kube-ovn », formulaires et schéma vivant, sous-onglet de cluster),
confirmé le 25/09. Livré par étapes ; celle-ci couvre ce que Harvester sait
déjà utiliser pour brancher une VM : **VPC, subnets et réseaux overlay**.

## Constat sur harv1 (Harvester v1.9.0, kube-ovn v1.16.2)

- L'addon `kubeovn-operator` est actif. Un VPC (`ovn-cluster`) et trois
  subnets (`ovn-default` 10.54.0.0/16 avec NAT, `join` 100.64.0.0/16,
  `egress-external` 172.16.0.0/22 en underlay VLAN sur `eno2`).
- Un réseau overlay `default/ovn-overlay` (NAD de type `kube-ovn`,
  `provider` `ovn-overlay.default.ovn`) **sans subnet** : une VM qui s'y
  branche ne reçoit aucune adresse. Rien ne le signale dans Harvester.
- Harvester reconnaît un réseau overlay à l'étiquette
  `network.harvesterhci.io/type: OverlayNetwork` et le rattache lui-même au
  réseau de cluster `mgmt`.
- Les vues Fabrique et Réseau affichent déjà ces objets, en lecture seule.

## Ce qui est livré dans cette étape

Un sous-onglet de cluster **VPC** (à côté de Réseau et Fabrique), dans la
grammaire en blocs des vues Fabrique, Réseau et Stockage : un bloc par VPC,
lu de gauche à droite.

    VMs branchées (adresse, MAC)  |  subnets (CIDR, passerelle, usage)  |  sortie (NAT, privé, routes)

C'est le « schéma vivant » : il se relit toutes les 8 s et montre l'effet de
chaque formulaire. Cytoscape ayant été retiré en 1.43, les blocs remplacent
le graphe que prévoyait la décision du 20/09.

**Formulaires** (fenêtre modale, mêmes champs et bulles que le reste) :

- **VPC** : nom, espaces de noms autorisés ; repliés : routes statiques,
  appairages avec d'autres VPC.
- **Subnet** : nom, VPC, CIDR (un /24 libre proposé, qui ne chevauche ni les
  autres subnets ni le réseau des nœuds), passerelle (déduite : premier hôte),
  adresses exclues (la passerelle d'office), réseau overlay (un existant sans
  subnet, ou un nouveau créé avec le subnet), NAT sortant (proposé dans le
  VPC par défaut seulement, kube-ovn ne le fait pas ailleurs), DHCP pour les
  VMs (actif par défaut) ; repliés : subnet privé et subnets autorisés,
  espaces de noms.
- **Suppression** : refusée tant que des adresses du subnet sont utilisées
  (VMs ou pods, nommés), ou qu'un VPC a encore des subnets.

**Constats** affichés dans la vue : réseau overlay sans subnet (avec « créer
son subnet »), subnet dont le réseau overlay a disparu, CIDR qui se
chevauchent, subnet plein à plus de 90 %.

**Intouchables** : le VPC `ovn-cluster`, les subnets `ovn-default` et
`join`, et les subnets d'underlay (`vlan` renseigné) restent en lecture
seule dans cette étape.

## Parité CLI et traçabilité

`bin/harvester-network.py` (`inventory`, `check`, `apply`, `delete`) porte
toute l'écriture ; la console le lance en action suivie (dock, Activité).
Logique pure dans `bin/lib/ovn_net.py` (validation, CIDR, suggestions,
constats), testée seule. Écriture réservée au rôle `admin` : un réseau
modifié peut couper des VMs.

## Étapes suivantes (« tout kube-ovn »)

1. Passerelles NAT de VPC, EIP, FIP, SNAT, DNAT.
2. Groupes de sécurité et politiques de QoS.
3. Réseaux fournisseurs et VLAN (underlay), avec la représentation des
   interfaces physiques déjà faite dans la Fabrique.
4. DNS de VPC, répartiteurs de subnet, passerelles de sortie, BGP/EVPN.

## Essai réel sur harv1 (25/09/2026)

1. **Par la ligne de commande** : un subnet `lab-a` (10.200.0.0/24, NAT) dans
   le VPC par défaut avec son nouveau réseau `default/lab-a`, un VPC `lab`,
   un subnet `lab-b` dans ce VPC. Harvester a reconnu les deux réseaux
   (rattachés à `mgmt`, prêts), kube-ovn a déclaré les subnets prêts en
   quelques secondes.
2. **Une VM sur `lab-a`** (openSUSE Leap 15.6) : `eth0` a reçu 10.200.0.2
   par le DHCP de kube-ovn, et cloud-init a installé l'agent invité depuis
   Internet, donc le NAT sortant marche. La vue l'a montrée sur son subnet,
   avec son adresse.
3. **Suppressions refusées** : `lab-a` tant que la VM y était, `lab` tant
   qu'il gardait `lab-b`.
4. **Par la vue** : un subnet créé dans `lab` (CIDR 10.200.2.0/24 proposé,
   passerelle déduite, réseau nommé d'après lui, NAT grisé), modifié (DHCP
   coupé puis rétabli, la carte suit), supprimé avec son réseau ; la
   suppression de `lab-a` refusée avant tout envoi, en français. Puis tout
   retiré depuis la vue, VM d'essai comprise : harv1 est revenu à son état
   de départ (le réseau `default/ovn-overlay` sans subnet est resté, il
   était là avant).

Constats en route : un refus du script arrivait brut (« subnet-in-use
{…} ») ; la page le traduit maintenant, et ne tente même plus une
suppression qu'elle sait vouée à l'échec.
