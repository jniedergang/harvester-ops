"""Shared Playwright fixtures for the e2e/ test directory."""

import pytest

playwright = pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def context(browser):
    """A fresh BrowserContext per test — cookies/localStorage are isolated."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    yield ctx
    ctx.close()


@pytest.fixture
def page(context, flask_server):
    """A pre-navigated page for the test app."""
    p = context.new_page()
    p.goto(flask_server["base_url"])
    p.wait_for_load_state("networkidle")
    yield p
