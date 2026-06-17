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
