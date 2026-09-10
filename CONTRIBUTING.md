# Contributing to harvester-ops

Thanks for considering a contribution. This project is built for production
SUSE Harvester clusters — code quality, test coverage and documentation
matter more than feature velocity.

## Repository layout

```
bin/                Bash scripts (shutdown / startup / status)
config/             Sample config.yaml + systemd unit
docs/               Bilingual docs (en/, fr/) — install, ops, troubleshooting
tests/api/          pytest against Flask test_client + spawned server
tests/e2e/          Playwright tests against headless Chromium
web/                Flask app, JS/CSS, templates
web/static/js/      Vanilla JS (no framework); IIFE modules on window.X
web/static/vendor/  Third-party frontend assets (noVNC, Cytoscape, tiptap, Lucide licence)
scripts/gen-icons.py  Regenerates the Lucide icon table in web/static/js/icons.js
web/requirements.txt        Pinned versions
web/requirements-lock.txt   Hash-pinned (use for production installs)
CHANGELOG.md        Conventional Changelog (## [x.y.z] — date — title)
VERSION             Single source of truth for the version string
```

## Setup

```sh
git clone https://github.com/jniedergang/harvester-ops
cd harvester-ops
pip install --user --break-system-packages -r web/requirements.txt
# For production install with hash verification:
pip install --require-hashes -r web/requirements-lock.txt
```

Run the API tests:

```sh
python3 -m pytest tests/api/ -q          # ~270 tests, < 15s
```

The e2e suite needs Playwright + a working headless chromium:

```sh
python3 -m pytest tests/e2e/ -q
```

`--live` runs tests that hit a reachable Harvester cluster; skipped by
default.

## Workflow

### Every change ships with…

1. **a test** — pytest for any backend logic, Playwright for any UI
   behaviour. Source-level tests are acceptable when DOM/browser tests
   would be flaky (e.g. asserting a CSS rule's presence).
2. **a VERSION bump** in the same commit as the code change. Tag the
   release line in `CHANGELOG.md` with the same date/version.
3. **a CHANGELOG entry** following the existing format: `## [x.y.z] —
   YYYY-MM-DD — <one-line title>`, then `### Added / Changed / Fixed`
   sections.
4. **docs** updated when behaviour visible to operators changes
   (settings, endpoints, install flow).

### Commit messages

Conventional Commits subject line:

```
fix(1.5.6): security headers + k8s name validation + rate-limit
```

The body explains the *why* and references file:line for non-obvious
changes. **Never mention Claude / AI / Anthropic** in commits, PRs,
release notes — see global rule in CLAUDE.md.

### Tests must pass before commit

```sh
# Fast suite — under 15 seconds
make test-api

# Full suite — about 90 seconds
make test
```

A pre-commit hook is shipped (see below). Tests run on every commit.

## Lint + pre-commit

A minimal `.pre-commit-config.yaml` runs:
- `bash -n` on shell scripts (syntax check)
- `python3 -m py_compile` on `web/app.py`
- `pytest tests/api/test_*.py -q --collect-only` (validates discovery)

Install and enable:

```sh
pip install --user --break-system-packages pre-commit
pre-commit install
```

## Code style

### Python

- No type hints required everywhere, but new helpers benefit from them.
- Imports at top of module; defer optional imports inside functions only
  when the dep is genuinely optional (e.g. `prometheus_client`).
- Follow the existing pattern of `log_<subsystem>` loggers (actions,
  watch, notes, capi, terraform).
- Validate input at the HTTP boundary, trust internal calls.

### JavaScript

- Vanilla JS only — no framework. IIFE modules exposing on `window.X`.
- Always `escapeHtml(...)` when interpolating server / user data into
  `innerHTML`. Use the `esc()` helper in the relevant module.
- Timers (`setInterval`, `setTimeout`) must be cleared in `beforeunload`.
- New i18n strings: add to EN + FR; IT/ES/DE fall back to EN.

### Icons

- Never paste an emoji or a pictographic character into a template,
  module or stylesheet — `tests/api/test_icons.py` scans for them. Use
  the vendored Lucide set instead:
  - in JS templates: `${Icons.svg('ok', { size: 14, cls: 'icon-ok' })}`
    (`Icons.el()` returns a DOM node for `append()`);
  - in static HTML (`index.html`, `review.html`): `<span data-icon="node"
    data-icon-size="18"></span>`, hydrated once by `Icons.mount()`;
  - on the Cytoscape canvas: `Icons.dataUri('vm')` as node `icon` data;
  - in `FloatingPanels.open({...})`: the `icon` option, not a glyph in
    `title`.
- Icons are addressed by *meaning* (`delete` vs `destroy`, `warn`,
  `node`), never by Lucide file name. To add one, add a row to `MANIFEST`
  in `scripts/gen-icons.py`, run `python3 scripts/gen-icons.py` (or
  `--from-tarball FILE` offline) and commit the regenerated blocks of
  `icons.js` / `style.css` together with the manifest. `--check` only
  compares the generated blocks, but it still needs the pinned tarball:
  online it downloads and hash-verifies it into `dist/`; in CI or an
  airgapped shell, point it at a cached artefact with
  `--from-tarball` (or an unpacked package with `--from-dir`).
- Status colour comes from a class (`.icon-ok`, `.icon-err`,
  `.icon-warn`, `.icon-run`, `.icon-dim`), never from the glyph.
- Never put an icon inside a `data-i18n` element: `applyTranslations()`
  replaces its `textContent`. Use a sibling `<span>`.

### CSS

- Use the theme vars (`var(--bg)`, `var(--accent)`, …). The 5 themes ×
  2 modes are defined in `style.css`; touching colour literals breaks
  the dark/light switcher.

## Security

- Every mutative endpoint should carry `@requires_auth` and
  `@_rate_limit("…")`.
- Path params that map to k8s names are auto-validated by the
  `before_request` hook against `_K8S_NAME_RE`. Custom path params
  must validate explicitly.
- Never echo a kubeconfig path or secret in a response body.
- New backend dependencies: regenerate `web/requirements-lock.txt`
  with `pip-compile --generate-hashes`.

## Release flow

1. Land all changes for the release on `main`.
2. Bump `VERSION` to the new value.
3. Add the `## [x.y.z] — YYYY-MM-DD — title` block at the top of
   `CHANGELOG.md`.
4. `git commit -m "chore(x.y.z): bump version"` and `git push`.
5. Smoke-test the running dev server on :8095.
