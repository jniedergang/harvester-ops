# Tests

Automated test suite for harvester-ops. Per project policy, **every modification, addition or fix** must come with a test that simulates the corresponding user action.

## Layout

```
tests/
├── conftest.py            shared fixtures (Flask server, API helper, test config)
├── api/
│   └── test_endpoints.py  HTTP-level tests (pytest), no browser
├── e2e/
│   └── test_ui_flows.py   end-to-end browser tests (Playwright)
└── README.md
```

## Running

```bash
# Install deps (once)
pip install --user pytest pytest-flask playwright
python -m playwright install chromium

# All tests (excluding live cluster tests)
make test                       # ≈ pytest -v

# Only API tests
make test-api                   # ≈ pytest tests/api/ -v

# Only E2E (browser) tests
make test-e2e                   # ≈ pytest tests/e2e/ -v

# Including live tests (need a reachable Harvester cluster + valid kubeconfig)
make test-live                  # ≈ pytest --live -v
```

## Adding a new test

1. **API change?** add a case in `tests/api/test_endpoints.py`. Use the `api` fixture:
   ```python
   def test_my_new_endpoint(api):
       status, body = api("GET", "/api/new-thing")
       assert status == 200
       assert body["expected"] == "value"
   ```

2. **UI change?** add a case in `tests/e2e/test_ui_flows.py`. Use the `page` fixture:
   ```python
   def test_my_new_button(page):
       page.click("#btn-new-thing")
       expect(page.locator("#result-panel")).to_be_visible()
   ```

3. **Bug fix?** add a regression test that reproduces the bug, then proves the fix.

## What the fixtures provide

- **`flask_server`** (session-scoped): boots `web/app.py` on a random port, with a sandboxed `config.yaml` declaring a fake cluster `harv-fake` (no real cluster needed). Test logs/bundles go under `tmp_path`.
- **`api`**: tiny request helper with assertion-friendly status check, returns `(status, json_body)`.
- **`browser`** (Playwright): a chromium browser; each test gets a fresh `page`.

## CI guidelines

- All tests should pass without `--live` (no real cluster dependency).
- `--live` runs an extended suite against a real Harvester cluster (developer-only).
- E2E tests must be deterministic — no flakiness with arbitrary timeouts. Use `page.wait_for_*` helpers.
- Each test should complete in < 5 seconds (target).
