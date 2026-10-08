# Presentation site and live demo

The presentation site of harvester-ops is built from this repository, in five
languages (English, French, Spanish, Italian, German), with a live demo: the
real interface of the release, running in the browser on a simulated API. It
is plain static files, hostable anywhere and under any path.

```bash
make site                 # or: python3 tools/demo-site/build.py --out dist/site
```

Output in `dist/site/`:

| Path | What it is |
|---|---|
| `index.html` | Redirects to the browser's language |
| `<lang>/index.html` | The site, one page per language |
| `assets/` | Styles, script, icon, screenshots |
| `assets/shots/<lang>/` | Screenshots taken in the demo, in the language of the page |
| `demo/` | The console interface on the simulated API |

## How it is made

| Piece | Source |
|---|---|
| Texts | `site/content/<lang>.json`, the same structure in the five languages (checked by the tests) |
| Roles in the technology table | `site/content/tech-<lang>.json`, keyed by the English role |
| Technologies and versions | `tools/demo-site/techinfo.py` reads them in the release itself: Python lockfile, Containerfile, Cluster API bundle manifest, README of the embedded libraries, `scripts/embedded-providers.env` |
| Icons | The Lucide set of the console (`web/static/js/icons.js`) |
| Screenshots | `tools/demo-site/shots.py`, taken in the demo at build time in each language (never stored in the repository) |
| Demo interface | `web/templates/index.html` rendered with the demo clusters, `web/static/` copied with relative paths |
| Demo API | `site/demo/demo-api.js`, loaded before the interface |
| Demo data | `site/demo/data.json`, anonymized recording of a real console |

The build needs Python with Jinja2 (already a dependency of the console) and,
for the screenshots, Playwright with Chromium. `--no-shots` builds without a
browser; `--demo-only` builds the demo alone.

## The simulated API

`demo-api.js` replaces `fetch`, `EventSource`, `XMLHttpRequest` and the
console WebSockets for everything under `/api/`:

- reads (GET) are answered from the recording, the closest match when the
  query differs, an empty answer when the screen was never recorded;
- every write (POST, PUT, PATCH, DELETE) becomes a simulated action: visible in
  the actions dock and the Activity tab, its steps streamed, then done;
- starting, stopping or restarting a VM changes its state in the demo
  (starting, then running after a few seconds);
- the dates of the recording slide to the moment the demo is opened, so a
  next copy "in 33 s" stays true;
- the Activity tab carries a seeded history of a week of work, filtered like
  the console does on the server;
- `#tab` and `?lang=` in the address choose the tab and the language (the
  links of the site use them);
- the VNC and serial consoles say they need a real VM.

## Refreshing the demo data

1. Start a console open on test clusters (no sign-in), for example:
   `HARVESTER_OPS_AUTH=none HARVESTER_OPS_CONFIG=<config with bind_port 8105>
   python3 web/app.py`.
2. Record what it reads, by tabs only (no gesture is made on the clusters):
   `python3 tools/demo-site/record.py --base http://127.0.0.1:8105 --out /tmp/rec.json`.
   The recording holds real names and addresses: it never leaves the machine.
3. Anonymize: `python3 tools/demo-site/sanitize.py /tmp/rec.json site/demo/data.json`.
   Cluster, node, VM and wave names become a fictional world, private
   addresses `10.20.x.y`, domains `example.internal` and `example.com`,
   cloud-init and password lines are neutralized, SSH keys and hardware MAC
   addresses replaced. The script refuses to write if a forbidden pattern is
   left (`FORBIDDEN` in the script): add the rule, never relax the list.
4. Build and look at the demo, then commit `site/demo/data.json`.

## Publishing

The site is not published at the moment: the public GitHub Pages site was
withdrawn on 2026-10-08 (the `gh-pages` branch was deleted, which unpublishes
it). Build it with `make site` and serve `dist/site/` privately to review it.
To publish it again on GitHub Pages:

```bash
tools/demo-site/publish-pages.sh            # remote "github" by default
```

then enable Pages on the `gh-pages` branch in the repository settings. The
script refuses to run with uncommitted changes in `site/`,
`tools/demo-site/`, `web/` or `VERSION`, builds `dist/site/` from the current
commit, puts it alone on the `gh-pages` branch (one commit per publication,
`.nojekyll` included) and pushes it. Pages serves that branch from its root.
Publish once the release is tested, so the site and the demo show the
released interface.

## Tests

- `tests/api/test_site_186.py`: same structure in the five languages, no em
  dash or Unicode arrow, every family pointing at a real tab and screenshot,
  versions matching the lockfile and the Containerfile, the published demo
  data free of every forbidden pattern, the anonymizer on a crafted recording
  (and refusing to write a leak), the build of the five pages and the demo.
- `tests/e2e/test_demo_site_186.py`: the demo built and served statically,
  opened with no request leaving the page, a VM stopped and started from All
  clusters VMs with the action in the dock, the seeded history dated today and
  filtered, the anchor choosing the tab.
