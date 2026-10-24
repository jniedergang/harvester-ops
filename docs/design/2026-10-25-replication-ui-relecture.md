# Relecture commune de la maquette de réplication (25/10/2026)

## Écran 1 : plans de réplication (menu racine Disaster Recovery)
- Forme : cartes ET tableau, avec bascule (comme Migrations VMware couloirs/liste).
- Chaque contrat montre : RPO tenu vs objectif (jauge), sens source -> secours (+ joignabilité), nombre de VMs et leur état, dernier événement, ET les dernières performances de transfert (débit) et la quantité de données transférées.

## Écran 2 : fenêtre de création d'un contrat
- Bloc 1 : VMs choisies par liste OU par règle d'étiquette (dr=gold : toute VM étiquetée plus tard est protégée d'office).
- Bloc 2 : phase de mesure proposée mais sautable (protéger directement avec un avertissement : RPO atteignable estimé).
- Bloc 3 : réseau absent sur le secours = bouton « Créer sur la cible » (VLAN modifiable) OU correspondance avec un réseau existant ; bloquant tant que rien n'est choisi. Colonne « Réseau d'exercice » présente dès la création.
- Bloc 3 (ajout de ju) : choix de la classe de stockage cible DISQUE PAR DISQUE dans l'interface (pas seulement par classe source).
- Bloc 4 : vagues (ordre de démarrage + condition de passage), rejouer cloud-init + configuration réseau du secours, MAC gardée/changée et CPU/mémoire réduits au secours, actions annexes HTTP (secrets masqués).

## Écran 3 : vue d'un contrat
- Onglets : Vue d'ensemble (VMs, jauges RPO, débit) / Graphiques / Journal / Réglages.
- Graphiques : 24 h par défaut, sélecteur 1 h / 7 j / 30 j.
- Basculer : fenêtre listant les étapes (planifiée ou non planifiée) + nom du contrat tapé, admin seulement.

## Écran 4 : liens entre clusters, sous-menu par cluster, alertes
- Débit : mesure passive continue (débit réel des sauvegardes) + test actif à la demande (pod temporaire).
- Sous-menu Disaster Recovery d'un cluster : contrats où il est source ET secours, badge source/secours, filtre.
- Alertes (RPO dépassé, disque qui ne se sauvegarde jamais, source encore allumée après bascule) : pastille dans le menu (Disaster Recovery + cluster), onglet Événements existant, ET notification externe configurable (webhook Matrix/Slack/mail).
- Livraison : une version avec tous ces écrans sauf les exercices de reprise (qui viendront avec leur moteur).

# Relecture de la maquette de navigation (41)
- Ordre de la barre : tel quel (Recherche, Toutes les VMs, Conteneurs, Hôtes Harvester/KVM, Disaster Recovery, Migrations, Ressources partagées, Automatisation, Activité). ATTENTION (ju) : récupérer TOUTES les vues de configuration des hôtes déjà travaillées (Fabrique, Réseau, Stockage à la VMware, nœuds, maintenance, bare-metal, mise à jour, réglages, add-ons, sécurité, KVM : pools, réseaux, images...) dans les paramètres de l'hôte, rien ne doit se perdre.
