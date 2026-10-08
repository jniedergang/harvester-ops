# Site de présentation et démo vivante

Le site de présentation de harvester-ops se construit depuis ce dépôt, en cinq
langues (anglais, français, espagnol, italien, allemand), avec une démo
vivante : la vraie interface de la version, qui tourne dans le navigateur sur
une API simulée. Ce ne sont que des fichiers statiques, hébergeables n'importe
où et sous n'importe quel chemin.

```bash
make site                 # ou : python3 tools/demo-site/build.py --out dist/site
```

Sortie dans `dist/site/` :

| Chemin | Contenu |
|---|---|
| `index.html` | Redirige vers la langue du navigateur |
| `<langue>/index.html` | Le site, une page par langue |
| `assets/` | Styles, script, icône, captures |
| `assets/shots/<langue>/` | Captures prises dans la démo, dans la langue de la page |
| `demo/` | L'interface de la console sur l'API simulée |

## Comment il est fait

| Élément | Source |
|---|---|
| Textes | `site/content/<langue>.json`, même structure dans les cinq langues (contrôlée par les tests) |
| Rôles du tableau des technologies | `site/content/tech-<langue>.json`, indexés par le rôle anglais |
| Technologies et versions | `tools/demo-site/techinfo.py` les lit dans la version elle-même : lockfile Python, Containerfile, manifeste du paquet Cluster API, README des bibliothèques embarquées, `scripts/embedded-providers.env` |
| Icônes | Le jeu Lucide de la console (`web/static/js/icons.js`) |
| Captures | `tools/demo-site/shots.py`, prises dans la démo à la construction, dans chaque langue (jamais gardées dans le dépôt) |
| Interface de la démo | `web/templates/index.html` rendu avec les clusters de la démo, `web/static/` recopié en chemins relatifs |
| API de la démo | `site/demo/demo-api.js`, chargé avant l'interface |
| Données de la démo | `site/demo/data.json`, enregistrement anonymisé d'une console réelle |

La construction demande Python avec Jinja2 (déjà une dépendance de la
console) et, pour les captures, Playwright avec Chromium. `--no-shots`
construit sans navigateur ; `--demo-only` ne construit que la démo.

## L'API simulée

`demo-api.js` remplace `fetch`, `EventSource`, `XMLHttpRequest` et les
WebSockets de la console pour tout ce qui est sous `/api/` :

- les lectures (GET) sont servies depuis l'enregistrement, la plus proche
  quand la requête diffère, une réponse vide pour un écran jamais enregistré ;
- chaque écriture (POST, PUT, PATCH, DELETE) devient une action simulée :
  visible dans le dock et l'onglet Activity, ses étapes diffusées, puis finie ;
- démarrer, arrêter ou redémarrer une VM change son état dans la démo (en
  démarrage, puis en marche après quelques secondes) ;
- les dates de l'enregistrement glissent jusqu'au moment où l'on ouvre la
  démo : une prochaine copie « dans 33 s » reste vraie ;
- l'onglet Activity porte un historique semé d'une semaine de travail, filtré
  comme la console le fait côté serveur ;
- `#onglet` et `?lang=` dans l'adresse choisissent l'onglet et la langue (les
  liens du site s'en servent) ;
- les consoles VNC et série disent qu'il leur faut une vraie VM.

## Rafraîchir les données de la démo

1. Lancer une console ouverte sur des clusters de test (sans connexion), par
   exemple : `HARVESTER_OPS_AUTH=none HARVESTER_OPS_CONFIG=<config avec
   bind_port 8105> python3 web/app.py`.
2. Enregistrer ce qu'elle lit, par les onglets seulement (aucun geste sur les
   clusters) : `python3 tools/demo-site/record.py --base
   http://127.0.0.1:8105 --out /tmp/rec.json`. L'enregistrement porte les
   vrais noms et adresses : il ne quitte jamais la machine.
3. Anonymiser : `python3 tools/demo-site/sanitize.py /tmp/rec.json
   site/demo/data.json`. Les noms de clusters, nœuds, VMs et vagues deviennent
   un monde fictif, les adresses privées `10.20.x.y`, les domaines
   `example.internal` et `example.com`, les lignes cloud-init et de mot de
   passe sont neutralisées, les clés SSH et les adresses MAC du matériel
   remplacées. Le script refuse d'écrire s'il reste un motif interdit
   (`FORBIDDEN` dans le script) : ajouter la règle, ne jamais assouplir la
   liste.
4. Construire et regarder la démo, puis commiter `site/demo/data.json`.

## Publication

Le site n'est pas publié pour l'instant : le site public sur GitHub Pages a été
retiré le 08/10/2026 (la branche `gh-pages` a été supprimée, ce qui le
dépublie). Le construire avec `make site` et servir `dist/site/` en privé pour
le relire. Pour le republier sur GitHub Pages :

```bash
tools/demo-site/publish-pages.sh            # remote « github » par défaut
```

puis activer Pages sur la branche `gh-pages` dans les réglages du dépôt. Le
script refuse de tourner s'il reste des modifications non commitées dans
`site/`, `tools/demo-site/`, `web/` ou `VERSION`, construit `dist/site/`
depuis le commit courant, le pose seul sur la branche `gh-pages` (un commit
par publication, `.nojekyll` compris) et la pousse. Pages sert cette branche
depuis sa racine. Publier une fois la release testée, pour que le site et la
démo montrent l'interface publiée.

## Tests

- `tests/api/test_site_186.py` : même structure dans les cinq langues, ni
  tiret cadratin ni flèche Unicode, chaque famille pointant vers un vrai
  onglet et une vraie capture, versions conformes au lockfile et au
  Containerfile, données publiées de la démo sans aucun motif interdit,
  l'anonymiseur sur un enregistrement fabriqué (et son refus d'écrire une
  fuite), la construction des cinq pages et de la démo.
- `tests/e2e/test_demo_site_186.py` : la démo construite et servie en
  statique, ouverte sans qu'aucune requête ne quitte la page, une VM arrêtée
  puis démarrée depuis VMs de tous les clusters avec l'action dans le dock,
  l'historique semé daté d'aujourd'hui et filtré, l'ancre qui choisit
  l'onglet.
