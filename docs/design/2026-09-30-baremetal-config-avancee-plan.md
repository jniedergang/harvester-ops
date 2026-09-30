# Plan : configuration d'installation bare-metal complète (1.77.0)

Conception : `docs/design/2026-09-30-baremetal-config-avancee.md`.

## Global Constraints

- Version cible 1.77.0 ; commits `feat(1.77.0): ...` / `fix(1.77.0): ...`
  en anglais, orientés pourquoi. VERSION et CHANGELOG sont bumpés à la
  release (tâche 4), pas avant.
- Aucune mention d'outil ni d'attribution dans les commits, le code, la doc.
  Pas de tiret cadratin ni de flèche Unicode dans tout contenu du dépôt
  ajouté par ce chantier.
- Code et noms en anglais, commentaires et interface en français ; chaque
  chaîne d'interface dans les cinq langues (EN, FR, DE, ES, IT) de
  `web/static/js/i18n.js` (test_i18n échoue sinon).
- Vanilla JS, pas de dépendance nouvelle (PyYAML est déjà là côté serveur ;
  aucun analyseur YAML côté navigateur : c'est le serveur qui lit le YAML).
- `@requires_auth` + `@_rate_limit("…")` (limite valide pour
  `limits.parse_many`) sur chaque nouvelle route ; `escapeHtml` avant tout
  `.innerHTML` ; une info-bulle sur chaque contrôle.
- Jamais de secret (jeton, mot de passe) dans une réponse HTTP, un libellé
  d'action, un journal ou une exception remontée.
- Le rendu d'une configuration qui n'utilise que les champs d'avant (1.76.1)
  doit rester sémantiquement identique (même dictionnaire après
  `yaml.safe_load`) : `harvlab.sh` et les installations existantes en
  dépendent.
- Clés réservées à la console, refusées dans le YAML avancé et dans un
  import (sauf là où la conception dit qu'on les lit) : `install.iso_url`,
  `install.automatic`, `install.mode`, `server_url`, `token`, `os.password`.
- Toute clé inconnue du schéma (v1.9.0 de harvester-installer) est refusée
  avec son chemin pointé (`os.write_files[2].contnt`).
- Aucune donnée du client dans le dépôt : la copie de test de son fichier
  est anonymisée (noms d'hôte, adresses en 192.0.2.0/24, 198.51.100.0/24,
  203.0.113.0/24, identifiants génériques).
- `python3 -m pytest tests/api/ -q` vert avant chaque commit.

### Task 1 : schéma, construction en dictionnaire, fusion et validation

Fichiers : créer `web/harvester_install_schema.py`, modifier
`web/app.py` (`_harvester_install_config`), tests
`tests/api/test_bm_config_177.py`, fixture
`tests/api/fixtures/bm_config_177/operator.yaml` (anonymisée, même forme que
le fichier d'exploitant décrit dans la conception : agrégat de gestion 2
cartes 802.3ad + vlan_id, 10 write_files NetworkManager et sshd,
persistent_state_paths, modules, labels, dns, ntp, data_disk,
wipe_all_disks, iso_url, system_settings).

- Le schéma est un dictionnaire imbriqué des clés snake_case acceptées
  sous la racine, `os` et `install` (et les sous-structures :
  `install.management_interface`, ses `interfaces[]`, `bond_options`,
  `os.write_files[]`, `install.webhooks[]`, ...), avec le type attendu
  (`str`, `int`, `bool`, `list[str]`, `dict[str,str]`, liste d'objets,
  objet). Le tirer de `pkg/config/config.go` à l'étiquette v1.9.0 de
  https://github.com/harvester/harvester-installer (lecture seule ; les
  noms JSON en camelCase deviennent snake_case comme le fait `rename.go` ;
  vérifier les exceptions de `rename.go`). Noter en tête la version et le
  commit d'origine.
- `validate_install_config(cfg) -> list[str]` : chemins en erreur (clé
  inconnue, mauvais type), vide si valide.
- `_harvester_install_config(opts)` construit un dict depuis les champs du
  formulaire (anciens + nouveaux : `mgmt_interfaces` liste de MAC ou noms,
  `bond_mode`, `bond_miimon`, `bond_lacp_rate`, `bond_xmit_hash_policy`,
  `vlan_id`, `data_disk`, `wipe_all_disks`, `labels` texte `k=v` par ligne,
  `modules` séparés par des virgules ; `mgmt_interface` seul reste accepté),
  fusionne `advanced_yaml` (texte YAML, un dict), refuse conflits et clés
  réservées, valide, et sérialise avec `yaml.safe_dump` (ordre des clés
  conservé, `sort_keys=False`) en gardant les textes multi-lignes en bloc
  littéral `|` (représentant PyYAML dédié).
  Erreur : lever `InstallConfigError(paths: list[str], message)`.
- `split_imported_config(text) -> dict` : `form` (champs connus du
  formulaire), `advanced` (texte YAML du reste), `secrets` (`token`,
  `password`, jamais renvoyés au navigateur par la route), `notes`
  (`iso_url` retirée, mode rejoindre détecté par `server_url`), `errors`
  (chemins refusés). Un fichier qui n'est pas un dict YAML est refusé.
- Tests : rendu identique d'avant pour les configurations de
  `test_baremetal.py` et la configuration de `harvlab.sh` ; fixture
  d'exploitant découpée puis recomposée = même dict (hors `iso_url`
  remplacée) ; refus clé inconnue, clé réservée, conflit formulaire/avancé,
  mauvais type ; blocs `|` conservés ; un chemin pointé exact.

### Task 2 : routes

Fichiers : `web/app.py`, tests `tests/api/test_bm_config_routes_177.py`.

- `POST /api/baremetal/config/parse` (JSON `{text}`, 256 Kio max) :
  renvoie `{form, advanced, notes, import_id, has_token, has_password}` ;
  les secrets du fichier sont gardés côté serveur dans un cache mémoire
  par personne (utilisateur + identité), 15 min, désignés par `import_id`
  (aléatoire, 128 bits). 400 avec `errors` pour un fichier refusé.
- `POST /api/baremetal/config/preview` : même corps que l'installation
  (sans les identifiants BMC) ; renvoie `{yaml}` avec jeton et mot de passe
  masqués (`•••`), ou 400 `{error, fields}` comme l'installation.
- `POST /api/baremetal/install` : accepte les nouveaux champs,
  `advanced_yaml`, `import_id` (jeton/mot de passe repris du cache quand
  le champ du formulaire est vide ; `import_id` inconnu ou expiré = 400).
  Valide la configuration AVANT de créer l'ActionRun (400 avec `fields`
  = chemins). Le libellé de l'action reste sans secret.
- Tests : secrets absents de toute réponse (parse, preview, erreurs),
  cache par personne (une autre personne ne réutilise pas l'`import_id`),
  expiration, taille maximale, refus avant ActionRun, limites de débit
  valides.

### Task 3 : fenêtre

Fichiers : `web/static/js/bmc.js`, `web/static/js/i18n.js`,
`web/static/css/style.css` si besoin (réutiliser `.capi-form`, fieldsets,
`.form-hint`, `.btn`), tests `tests/e2e/test_bm_config_177.py` (routes
BMC simulées comme les autres e2e) et un test source si le DOM est fragile.

- Interface de gestion : cases à cocher des cartes découvertes (au moins
  une), mode d'agrégat, `lacp_rate` et `xmit_hash_policy` visibles en
  802.3ad seulement, `miimon`, VLAN facultatif.
- Disques : installation, données, « effacer tous les disques » (avec un
  avertissement).
- Système : NTP (1.76.1), libellés, modules.
- « YAML avancé » : zone de texte monospace, une aide qui cite les clés
  typiques et les clés réservées.
- Boutons « Importer une configuration » (fichier choisi, envoyé à
  `parse`, formulaire rempli, notes affichées, champs secrets marqués
  « repris du fichier ») et « Aperçu » (fenêtre ou zone avec le YAML
  masqué). Les erreurs de chemins sont montrées lisiblement.
- La fenêtre grandit en conséquence (défilement interne), reste utilisable
  à 1440x900.
- e2e : import de la fixture anonymisée, champs remplis, `iso_url`
  signalée, aperçu sans secret, refus d'une clé inconnue affiché, 802.3ad
  montre ses options, DHCP cache l'adressage statique.

### Task 4 : vérification réelle, doc, release

(Faite par le contrôleur, pas par un sous-agent.)

- Nœud imbriqué sur node2 installé par `harvlab.sh` avec une
  configuration issue de la nouvelle fonction (4 cartes virtio, réseau
  libvirt isolé pour le stockage), vérifications listées dans la
  conception ; remettre le banc dans son état.
- Doc EN/FR (`docs/en/capabilities.md`, `docs/fr/capabilites.md`) : les
  nouveaux champs, le YAML avancé, l'import, l'aperçu, les clés réservées,
  ce qui n'est pas vérifié en réel (LACP, VLAN de gestion étiqueté).
- Parité (`tools/parity/data.py`) si une ligne bare-metal change,
  CHANGELOG 1.77.0, VERSION, suite complète, paquet construit et essayé par
  l'unité, Gitea puis GitHub avec l'étiquette, release.
