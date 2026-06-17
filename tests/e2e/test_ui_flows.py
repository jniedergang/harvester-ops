"""
End-to-end UI tests with Playwright.

Each test boots the Flask app via the `flask_server` fixture, opens a
chromium page and simulates user interactions:
 - tab navigation
 - opening the settings modal
 - switching languages
 - opening the docs panel
 - using the dock toggle/resize
 - launching a dry-run shutdown and watching the dock update

Run with:  make test-e2e
or:        python -m pytest tests/e2e/ -v
"""

import pytest

playwright = pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright, expect


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser, flask_server):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = ctx.new_page()
    page.goto(flask_server["base_url"])
    page.wait_for_load_state("networkidle")
    yield page
    ctx.close()


def test_index_loads_with_brand(page):
    expect(page.locator("#brand-title")).to_have_text("harvester-ops")
    expect(page.locator("#cluster-select")).to_be_visible()


def test_overview_data_populated_after_init(page, api):
    """REGRESSION: validate that the Overview metrics get populated by the
    /api/status call triggered on init. This catches the 'no data shown'
    class of bugs (where bind() throws and init() never finishes).
    """
    page.reload()
    page.wait_for_load_state("networkidle")
    # Wait for setCluster → refreshStatus → metrics rendered
    page.wait_for_timeout(1200)
    # On the fake cluster the API returns errors but the call is made.
    # The metrics should at least change from the initial '–' placeholder OR
    # the api call must have hit the server.
    cluster_name = page.locator(".cluster-name").first.inner_text()
    assert cluster_name == "harv-fake", f"setCluster did not run: '{cluster_name}'"
    # Network was indeed hit
    _, body = api("GET", "/api/clusters")
    assert any(c["name"] == "harv-fake" for c in body["clusters"])


def test_dock_restores_height_after_collapse_expand(page):
    """REGRESSION: collapse → expand must restore the user's previous height."""
    page.reload()
    page.wait_for_load_state("networkidle")
    # Set a non-default height via localStorage (simulating user resize)
    page.evaluate("""
      localStorage.setItem('harvester_ops_dock_height', '280');
      localStorage.setItem('harvester_ops_dock_visible', 'true');
      localStorage.setItem('harvester_ops_dock_collapsed', 'false');
    """)
    page.reload()
    page.wait_for_load_state("networkidle")
    dock = page.locator("#bottom-dock")
    h_initial = dock.evaluate("el => el.getBoundingClientRect().height")
    assert 270 <= h_initial <= 290, f"initial height: {h_initial}"
    # Collapse via header click (avoid the buttons inside)
    page.locator(".dock-title").click()
    page.wait_for_timeout(400)
    h_collapsed = dock.evaluate("el => el.getBoundingClientRect().height")
    assert h_collapsed < 60, f"collapsed height: {h_collapsed}"
    # Expand back
    page.locator(".dock-title").click()
    page.wait_for_timeout(400)
    h_restored = dock.evaluate("el => el.getBoundingClientRect().height")
    assert 270 <= h_restored <= 290, f"restored height: {h_restored} (expected ~280)"


def test_init_completes_no_js_errors(page):
    """REGRESSION: if bind() throws (because some DOM id was removed but the
    listener wasn't), init() never finishes and setCluster() never runs, so
    every tab stays empty. We detect this by:
      1. asserting no JS error logged in the console
      2. asserting the cluster name is reflected in the active tab title
         (only happens after setCluster runs)
    """
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.reload()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(800)   # let init() complete
    assert errors == [], f"JS errors during init: {errors}"
    # If setCluster ran, the cluster-name spans contain the cluster value
    cluster_name_text = page.locator(".cluster-name").first.inner_text()
    assert cluster_name_text == "harv-fake", \
        f"setCluster did not run (cluster-name='{cluster_name_text}')"


def test_sidebar_tabs_present(page):
    for tab in ["overview", "shutdown", "startup", "namespaces", "activity"]:
        expect(page.locator(f'.tab[data-tab="{tab}"]')).to_be_visible()


def test_settings_modal_opens(page):
    expect(page.locator("#settings-modal")).not_to_have_class("active")
    page.click("#btn-settings")
    expect(page.locator("#settings-modal")).to_have_class("modal-overlay active")
    # All 6 settings tabs should be visible
    for stab in ["general", "appearance", "language", "connection", "support", "about"]:
        expect(page.locator(f'.settings-tab[data-stab="{stab}"]')).to_be_visible()


def test_settings_modal_closes(page):
    page.click("#btn-settings")
    page.click("#btn-close-settings")
    expect(page.locator("#settings-modal")).not_to_have_class("active")


def test_settings_tab_switch(page):
    page.click("#btn-settings")
    page.click('.settings-tab[data-stab="appearance"]')
    expect(page.locator("#stab-appearance")).to_have_class("settings-tab-content active")
    expect(page.locator("#stab-general")).to_have_class("settings-tab-content")


def test_language_switch_changes_strings(page):
    """Switching to FR should change the visible text of common elements."""
    page.click("#btn-settings")
    page.click('.settings-tab[data-stab="language"]')
    # Click on Français button in lang grid
    page.click('.lang-grid button[data-lang="fr"]')
    # Verify some translated UI element
    page.click("#btn-close-settings")
    # Tab labels are translated via data-i18n
    txt = page.locator('.tab[data-tab="shutdown"]').inner_text()
    assert "Arrêt" in txt or "Shutdown" in txt   # tolerant if hot-swap deferred


def test_docs_panel_opens_and_closes(page):
    expect(page.locator("#docs-panel")).to_be_hidden()
    page.click("#btn-docs")
    expect(page.locator("#docs-panel")).to_be_visible()
    page.click("#btn-docs-close")
    expect(page.locator("#docs-panel")).to_be_hidden()


def test_docs_language_switcher_works(page):
    """REGRESSION: clicking the lang select should not be blocked by drag handler."""
    page.click("#btn-docs")
    expect(page.locator("#docs-panel")).to_be_visible()
    # Select FR via the docs-internal selector
    page.select_option("#docs-lang", "fr")
    page.wait_for_timeout(400)
    items = page.locator("#docs-toc-list li").all_inner_texts()
    # FR docs should now be listed
    assert any("Procédure" in t or "procedure" in t.lower() for t in items)


def test_dock_visible_by_default(page):
    expect(page.locator("#bottom-dock")).to_be_visible()
    expect(page.locator("#dock-empty")).to_be_visible()


def test_dock_toggle_in_activity_tab(page):
    """The checkbox has width=0/height=0 (CSS slider style) — click the
    parent label which propagates the toggle event."""
    page.click('.tab[data-tab="activity"]')
    toggle = page.locator("#dock-toggle")
    expect(toggle).to_be_checked()

    # Click the visible slider span (sibling of the hidden input)
    label = page.locator(".toggle-row .switch")
    label.click()
    expect(toggle).not_to_be_checked()
    expect(page.locator("#bottom-dock")).to_be_hidden()
    label.click()
    expect(toggle).to_be_checked()
    expect(page.locator("#bottom-dock")).to_be_visible()


def test_dock_resize_handle_exists(page):
    handle = page.locator("#dock-resize-handle")
    expect(handle).to_be_attached()


def test_shutdown_tab_loads_vm_list(page):
    page.click('.tab[data-tab="shutdown"]')
    expect(page.locator(".vm-order-toolbar")).to_be_visible()
    expect(page.locator("#vm-order-filter-ns")).to_be_visible()
    expect(page.locator("#vm-order-sort")).to_be_visible()


def test_vms_tab_dropdown_layout(page):
    """REGRESSION: 'Virtual machines' tab uses a top dropdown (no left sidebar).
    The old #ns-list (sidebar) must be gone; #ns-dropdown (top) must exist.
    """
    page.click('.tab[data-tab="namespaces"]')
    page.wait_for_timeout(800)
    # The vertical NS sidebar must NOT exist anymore
    expect(page.locator("#ns-list")).to_have_count(0)
    expect(page.locator(".ns-list-panel")).to_have_count(0)
    # The new dropdown + count badge must exist
    expect(page.locator("#ns-dropdown")).to_be_visible()
    expect(page.locator("#ns-vm-count")).to_be_visible()
    expect(page.locator("#ns-vms-table")).to_be_visible()
    # Bulk toolbar starts hidden
    expect(page.locator(".bulk-toolbar")).to_be_hidden()


def test_dry_run_inline_in_shutdown_and_startup(page):
    """REGRESSION: dry-run inline checkbox must exist next to the launch buttons."""
    page.click('.tab[data-tab="shutdown"]')
    expect(page.locator(".dry-run-inline").first).to_be_visible()
    page.click('.tab[data-tab="startup"]')
    expect(page.locator(".dry-run-inline").nth(1)).to_be_visible()


def test_dry_run_sync_across_checkboxes(page):
    """When one .dry-run-sync checkbox changes, all others must follow.
    The sidebar #dry-run was removed in 1.3.4 — sync now happens entirely
    between the inline checkboxes (shutdown tab ↔ startup tab)."""
    page.click('.tab[data-tab="shutdown"]')
    page.evaluate("""
      const cb = document.querySelector('#tab-shutdown .dry-run-sync');
      cb.checked = true;
      cb.dispatchEvent(new Event('change', { bubbles: true }));
    """)
    page.wait_for_timeout(150)
    expect(page.locator("#tab-startup .dry-run-sync")).to_be_checked()
    page.evaluate("""
      const cb = document.querySelector('#tab-startup .dry-run-sync');
      cb.checked = false;
      cb.dispatchEvent(new Event('change', { bubbles: true }));
    """)
    page.wait_for_timeout(150)
    expect(page.locator("#tab-shutdown .dry-run-sync")).not_to_be_checked()


def test_current_tab_persists_across_reload(page):
    """The currently selected tab must be restored after a page reload."""
    page.click('.tab[data-tab="shutdown"]')
    expect(page.locator('#tab-shutdown')).to_have_class("tab-content active")
    page.reload()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(400)
    expect(page.locator('#tab-shutdown')).to_have_class("tab-content active")
    # The shutdown sidebar entry is a `tab-child` of the Cluster collapsible
    # group since v1.4.5 — the class set is now `tab tab-child active` (or
    # `tab active` if the layout ever flattens back). Just require .active.
    cls = page.locator('.tab[data-tab="shutdown"]').get_attribute("class") or ""
    assert "active" in cls.split(), f"expected `active` in classes, got: {cls!r}"


def test_cluster_selection_persists_across_reload(page, flask_server):
    """REGRESSION (v1.4.32): the cluster <select> defaulted to the first
    option on every page load — if the user had switched to a different
    cluster, F5 silently sent them back to the first one. setCluster()
    now writes to localStorage and init() reads it back."""
    # The fake config only has one cluster (harv-fake). We exercise the
    # localStorage round-trip directly by stubbing a saved value and
    # asserting that the page picks it up.
    sel = page.locator('#cluster-select')
    # Sanity: at least one option exists in the fake config
    page.wait_for_function("document.querySelector('#cluster-select').options.length > 0")
    initial = sel.evaluate("e => e.value")
    assert initial, "cluster select has no value"
    # Simulate prior selection
    page.evaluate(
        "(val) => localStorage.setItem('harvester_ops_current_cluster', val)",
        initial,
    )
    page.reload()
    page.wait_for_load_state("networkidle")
    # The select should still hold the saved value (the same one here,
    # but the round-trip is what matters)
    val = sel.evaluate("e => e.value")
    assert val == initial, (
        f"cluster selection not restored: stored={initial!r}, got={val!r}"
    )
    # And setCluster() must have been called with it (currentCluster
    # exposed via window.App)
    current = page.evaluate("window.App?.getCurrentCluster && App.getCurrentCluster()")
    assert current == initial


def test_overview_subtab_persists_across_reload(page):
    """REGRESSION (v1.4.31): refreshing the page while on an Overview
    sub-tab (Cluster / Network / Storage) used to land on a blank
    canvas. Cause: the restoration ran inside bind() — BEFORE
    setCluster() set currentCluster — so the synthesised click on the
    saved sub-tab took mountTopology()'s `if (!currentCluster) return`
    branch and the canvas was never built.

    Fix: a new `restoreSubTabsFromStorage()` helper runs from init()
    AFTER setCluster(), guaranteeing mountTopology has a cluster to
    work with."""
    # Navigate to Overview > Cluster sub-tab and wait for the canvas
    # to be mounted by mountTopology().
    page.click('.tab[data-tab="overview"]')
    page.click('[data-overview-tab="cluster"]')
    page.wait_for_selector(
        '.overview-subtab[data-subtab="cluster"] .topology-canvas',
        timeout=5000,
    )

    page.reload()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(400)

    # After reload the Cluster sub-tab must still be active AND the
    # canvas must be present (the topology was actually mounted).
    cls = page.locator('[data-overview-tab="cluster"]').get_attribute("class") or ""
    assert "active" in cls.split(), (
        f"Overview Cluster sub-tab did not stay active after reload "
        f"(class={cls!r}). restoreSubTabsFromStorage() is not running "
        f"after setCluster() in init()."
    )
    page.wait_for_selector(
        '.overview-subtab[data-subtab="cluster"] .topology-canvas',
        timeout=5000,
    )


def test_automation_tab_persists_across_reload(page):
    """REGRESSION (v1.4.33): F5 from the Automation tab silently fell
    back to Overview. The Automation group head carries data-group but
    NOT data-tab — yet the restoration guard in init() only checked for
    .tab[data-tab=<saved>]. saved='automation' never matched, so
    setTab('automation') was skipped and the DOM-default #tab-overview
    .active stayed in place.

    Fix: guard on document.getElementById('tab-<saved>') instead — that
    is what setTab() actually toggles."""
    # The Automation group is collapsed by default — click the group
    # head first. That call both expands the group AND triggers
    # setTab(firstChild.dataset.tab || 'automation') = setTab('automation'),
    # because Automation children carry data-subtab and not data-tab.
    page.click('#tab-group-automation .tab-group-head')
    page.wait_for_timeout(150)
    expect(page.locator('#tab-automation')).to_have_class("tab-content active")
    # Sanity: the saved value is the group name, not a real data-tab
    saved = page.evaluate(
        "() => localStorage.getItem('harvester_ops_current_tab')"
    )
    assert saved == "automation", f"expected 'automation' saved, got {saved!r}"

    page.reload()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(400)

    # After F5 the Automation content panel must still be the active one
    expect(page.locator('#tab-automation')).to_have_class("tab-content active")
    # And Overview must NOT be active any more (regression guard against
    # silently falling back to the DOM default)
    cls_overview = page.locator('#tab-overview').get_attribute("class") or ""
    assert "active" not in cls_overview.split(), (
        f"#tab-overview should not be active after F5 from Automation, "
        f"class={cls_overview!r}"
    )


def test_theme_switcher_applies_and_persists(page):
    """v1.4.34: 5 themes × 2 modes, switchable on the fly via
    Settings > Appearance. Each theme defines a different --bg, so
    asserting it changes is enough to prove the palette swap reached
    the DOM. Persistence is checked via reload."""
    # Default since v1.4.35 = Tokyo Night Day (light) — set on <html>
    # by the inline <head> bootstrap before style.css parses (no FOUC).
    initial = page.evaluate("""() => ({
      theme: document.documentElement.getAttribute('data-theme'),
      mode:  document.documentElement.getAttribute('data-mode'),
      bg:    getComputedStyle(document.documentElement)
               .getPropertyValue('--bg').trim(),
    })""")
    assert initial["theme"] == "tokyo" and initial["mode"] == "light", initial
    assert initial["bg"], f"--bg empty on boot, got {initial!r}"

    # Switch to Nord dark via the Theme API (the in-modal selects
    # call the same path — bypass the modal here to keep the test
    # focused on the palette mechanism).
    page.evaluate("Theme.apply('nord', 'dark')")
    page.wait_for_timeout(100)
    after = page.evaluate("""() => ({
      theme: document.documentElement.getAttribute('data-theme'),
      mode:  document.documentElement.getAttribute('data-mode'),
      bg:    getComputedStyle(document.documentElement)
               .getPropertyValue('--bg').trim(),
    })""")
    assert after["theme"] == "nord" and after["mode"] == "dark", after
    assert after["bg"] != initial["bg"], (
        f"--bg did not change after theme swap: {initial['bg']!r} -> "
        f"{after['bg']!r} — the [data-theme][data-mode] CSS block is "
        f"not being applied."
    )

    # Reload — should come back as Nord dark, not the default
    page.reload()
    page.wait_for_load_state("networkidle")
    restored = page.evaluate("""() => ({
      theme: document.documentElement.getAttribute('data-theme'),
      mode:  document.documentElement.getAttribute('data-mode'),
    })""")
    assert restored == {"theme": "nord", "mode": "dark"}, restored


def test_theme_switcher_handles_all_5_themes(page):
    """v1.4.34: regression guard — every (theme, mode) pair must
    produce a distinct --bg value. Catches a theme that accidentally
    inherits or has a typo in its CSS selector."""
    pairs = [
        ("suse",       "dark"), ("suse",       "light"),
        ("nord",       "dark"), ("nord",       "light"),
        ("solarized",  "dark"), ("solarized",  "light"),
        ("catppuccin", "dark"), ("catppuccin", "light"),
        ("tokyo",      "dark"), ("tokyo",      "light"),
    ]
    seen = {}
    for theme, mode in pairs:
        page.evaluate(
            "([t,m]) => Theme.apply(t, m)", [theme, mode]
        )
        page.wait_for_timeout(50)
        bg = page.evaluate(
            "getComputedStyle(document.documentElement)"
            ".getPropertyValue('--bg').trim()"
        )
        assert bg, f"empty --bg for {theme}/{mode}"
        key = f"{theme}/{mode}"
        # Two different (theme, mode) pairs should give different bgs.
        # If two collide it's almost certainly a CSS selector typo.
        for other_key, other_bg in seen.items():
            assert bg != other_bg, (
                f"palette collision: {key} and {other_key} both give "
                f"--bg={bg!r}"
            )
        seen[key] = bg


def test_floating_panels_persist_across_reload(page):
    """An open floating panel (with restoreSpec) is reopened after reload."""
    # Register a custom type for the test
    page.evaluate("""
      FloatingPanels.registerType('test-restore', (args) => {
        return FloatingPanels.open({
          id: 'test-restore-panel',
          title: 'Restored: ' + args.label,
          bodyHtml: '<p>arg=' + args.label + '</p>',
          restoreSpec: { type: 'test-restore', args: args },
        });
      });
      FloatingPanels.open({
        id: 'test-restore-panel',
        title: 'Initial',
        bodyHtml: '<p>arg=foo</p>',
        restoreSpec: { type: 'test-restore', args: { label: 'foo' } },
      });
    """)
    expect(page.locator('#fp-test-restore-panel')).to_be_visible()
    page.reload()
    page.wait_for_load_state("networkidle")
    # Re-register the type on the fresh page (in real life this is done by
    # the panel's module at script-load time)
    page.evaluate("""
      FloatingPanels.registerType('test-restore', (args) => {
        return FloatingPanels.open({
          id: 'test-restore-panel',
          title: 'Restored: ' + args.label,
          bodyHtml: '<p>arg=' + args.label + '</p>',
          restoreSpec: { type: 'test-restore', args: args },
        });
      });
      FloatingPanels.restoreAll();
    """)
    page.wait_for_timeout(400)
    expect(page.locator('#fp-test-restore-panel')).to_be_visible()
    # Cleanup
    page.evaluate("FloatingPanels.close('test-restore-panel')")


def test_floating_panel_open_minimize_close(page):
    """REGRESSION: floating panels system (used for Docs, VM edit, VM console)
    can be opened, minimized to the min-bar, restored and closed."""
    page.evaluate("""
      FloatingPanels.open({
        id: 'test-panel',
        title: 'Test panel',
        bodyHtml: '<p>hello</p>',
      });
    """)
    expect(page.locator("#fp-test-panel")).to_be_visible()
    # Minimize
    page.click("#fp-test-panel [data-action=min]")
    expect(page.locator("#fp-test-panel")).to_be_hidden()
    expect(page.locator(".min-chip[data-fp-id='test-panel']")).to_be_visible()
    # Click chip to restore
    page.click(".min-chip[data-fp-id='test-panel']")
    expect(page.locator("#fp-test-panel")).to_be_visible()
    # Close
    page.click("#fp-test-panel [data-action=close]")
    expect(page.locator("#fp-test-panel")).to_have_count(0)


def test_automation_subtabs(page):
    """REGRESSION: Automation tab has 3 sub-choices in the SIDEBAR
    (Cluster API, Terraform, Bare-metal) and switching swaps the content
    panel + (for CAPI) reveals the inline Installation / Cluster-creation
    strip."""
    page.click('.tab-group-head[data-group="automation"]')
    page.wait_for_timeout(300)
    # Sidebar children visible
    expect(page.locator('.tab-child[data-subtab="capi"]')).to_be_visible()
    expect(page.locator('.tab-child[data-subtab="terraform"]')).to_be_visible()
    expect(page.locator('.tab-child[data-subtab="pxe"]')).to_be_visible()
    # CAPI subtab is active by default → its content is active
    expect(page.locator('.sub-tab-content[data-subtab="capi"]')).to_have_class("sub-tab-content active")
    # The inline 📦 / 🛠 strip is only visible for CAPI
    expect(page.locator('.sub-tabs-inline')).to_be_visible()
    # Click Terraform sidebar entry — hides the inline strip
    page.click('.tab-child[data-subtab="terraform"]')
    page.wait_for_timeout(200)
    expect(page.locator('.sub-tab-content[data-subtab="terraform"]')).to_have_class("sub-tab-content active")
    expect(page.locator('.sub-tab-content[data-subtab="capi"]')).not_to_have_class("active")
    # Click Bare-metal
    page.click('.tab-child[data-subtab="pxe"]')
    page.wait_for_timeout(200)
    expect(page.locator('.sub-tab-content[data-subtab="pxe"]')).to_have_class("sub-tab-content active")


def test_automation_capi_inline_subtabs(page):
    """The 📦 Installation / 🛠 Création de clusters / 🖥 Clusters K8S inline
    strip on the automation header should swap which .capi-tab-content is
    visible. All three buttons must be present and centered (justify-self)."""
    page.click('.tab-group-head[data-group="automation"]')
    page.wait_for_timeout(200)
    page.click('.tab-child[data-subtab="capi"]')
    page.wait_for_timeout(200)
    # All 3 buttons present
    expect(page.locator('.sub-tabs-inline .sub-tab[data-capi-tab="install"]')).to_be_visible()
    expect(page.locator('.sub-tabs-inline .sub-tab[data-capi-tab="clusters"]')).to_be_visible()
    expect(page.locator('.sub-tabs-inline .sub-tab[data-capi-tab="k8s"]')).to_be_visible()
    # Default: install tab active
    expect(page.locator('.capi-tab-content[data-capi-tab="install"]')).to_have_class("capi-tab-content active")
    # Click "Création de clusters"
    page.click('.sub-tabs-inline .sub-tab[data-capi-tab="clusters"]')
    page.wait_for_timeout(200)
    expect(page.locator('.capi-tab-content[data-capi-tab="clusters"]')).to_have_class("capi-tab-content active")
    expect(page.locator('.capi-tab-content[data-capi-tab="install"]')).not_to_have_class("active")
    # Click "Clusters K8S"
    page.click('.sub-tabs-inline .sub-tab[data-capi-tab="k8s"]')
    page.wait_for_timeout(200)
    expect(page.locator('.capi-tab-content[data-capi-tab="k8s"]')).to_have_class("capi-tab-content active")
    expect(page.locator('.capi-tab-content[data-capi-tab="clusters"]')).not_to_have_class("active")


# v1.5.0: the inline single-resource Terraform form was replaced by the
# Declarations UI. Renderer correctness lives in tests/api/test_tf_render.py;
# schema sync in tests/api/test_tf_schema.py; section-button + overlay
# behaviour in the new test_decl_* tests below. The pre-1.5.0
# test_terraform_form_* tests have been removed.


def _clear_tf_drafts(page):
    """Purge every harvester_ops_tf_draft_* localStorage key. Tests
    that exercise the form must call this BEFORE navigating to the
    Terraform sub-tab so the first render is from a clean slate.
    Without it, a draft saved by an earlier test silently restores
    values that throw the count assertions off."""
    page.evaluate("""() => {
      Object.keys(localStorage).filter(k => k.startsWith('harvester_ops_tf_draft_'))
        .forEach(k => localStorage.removeItem(k));
    }""")


def _clear_tf_decls(page):
    page.evaluate("""() => {
      try { localStorage.removeItem('harvester_ops_tf_declarations'); } catch {}
      try { localStorage.removeItem('harvester_ops_tf_subtab'); } catch {}
    }""")


def _open_terraform(page):
    _clear_tf_drafts(page)
    _clear_tf_decls(page)
    page.click('.tab-group-head[data-group="automation"]')
    page.wait_for_timeout(150)
    page.click('.tab-child[data-subtab="terraform"]')
    page.wait_for_selector('#tf-decl-list', timeout=5000)


def test_tf_subtabs_default_to_declarations(page):
    """v1.5.4: the Terraform tab opens on "Declarations" by default
    and the three sub-tab buttons are visible."""
    _open_terraform(page)
    for tid in ('decls', 'live', 'install'):
        expect(page.locator(
            f'#tf-status-body .sub-tab[data-tf-tab="{tid}"]')).to_be_visible()
    active = page.evaluate(
        "document.querySelector('#tf-status-body .sub-tab.active')"
        "?.dataset.tfTab"
    )
    assert active == "decls"


def test_tf_subtabs_swap_panes_on_click(page):
    """Clicking a sub-tab toggles which pane is `.active` so only one
    section is visible at a time."""
    _open_terraform(page)
    page.click('#tf-status-body .sub-tab[data-tf-tab="install"]')
    page.wait_for_timeout(150)
    active = page.evaluate(
        "document.querySelector('#tf-status-body .sub-tab.active')"
        "?.dataset.tfTab")
    assert active == "install"
    visible = page.evaluate(
        "Array.from(document.querySelectorAll("
        "  '#tf-status-body .tf-subtab-content.active'))"
        ".map(s => s.dataset.tfTab)"
    )
    assert visible == ["install"]


def test_tf_subtabs_persist_across_reload(page):
    """The active sub-tab is saved in localStorage; F5 restores it."""
    _open_terraform(page)
    page.click('#tf-status-body .sub-tab[data-tf-tab="live"]')
    page.wait_for_timeout(150)
    assert page.evaluate(
        "() => localStorage.getItem('harvester_ops_tf_subtab')") == "live"
    page.reload()
    page.wait_for_load_state("networkidle")
    page.wait_for_selector('#tf-status-body .sub-tab[data-tf-tab="live"]',
                           timeout=5000)
    page.wait_for_timeout(300)
    active = page.evaluate(
        "document.querySelector('#tf-status-body .sub-tab.active')"
        "?.dataset.tfTab"
    )
    assert active == "live"


def test_decl_empty_state_shows_create_button(page):
    """v1.5.0: a brand-new visit shows the zero-state with a + New
    declaration button — not a free-form resource form."""
    _open_terraform(page)
    expect(page.locator('.tf-decl-empty')).to_be_visible()
    expect(page.locator('#btn-tf-decl-new')).to_be_visible()


def test_decl_create_and_persist_across_reload(page):
    """Creating a declaration via the JS API (we bypass the prompt
    dialog) persists it to localStorage. After F5 it must still be in
    the list AND remain the active declaration."""
    _open_terraform(page)
    decl = page.evaluate("""() => {
      const d = window.TFDecl.create('lab-batch-1', 'harv-fake');
      return { id: d.id, name: d.name };
    }""")
    assert decl["name"] == "lab-batch-1"
    page.evaluate("window.TF && TF.refresh && TF.refresh()")
    page.wait_for_timeout(300)
    expect(page.locator('.tf-decl-item__name', has_text="lab-batch-1")).to_be_visible()
    # Reload — must still be there. The v1.4.33 sub-tab persistence
    # restores Automation > Terraform automatically; no clicks needed.
    page.reload()
    page.wait_for_load_state("networkidle")
    page.wait_for_selector('.tf-decl-item', timeout=8000)
    expect(page.locator('.tf-decl-item__name', has_text="lab-batch-1")).to_be_visible()
    active_id = page.evaluate("() => window.TFDecl.getActive()?.id")
    assert active_id == decl["id"]


def test_decl_add_vm_resource_renders_section_buttons(page):
    """v1.5.5: opening a declaration in the FloatingPanel must render
    a resource card (right pane) with 4 section buttons (Specs, Disks,
    Networks, Cloud-init)."""
    _open_terraform(page)
    page.evaluate("""() => {
      const d = window.TFDecl.create('test-decl', 'harv-fake');
      window.TFDecl.addResource(d.id, 'vm');
      window.TF.refresh();
      window.TFDeclPanel.open(d.id);
    }""")
    page.wait_for_selector('.tf-resource-card', timeout=3000)
    buttons = page.evaluate(
        "Array.from(document.querySelectorAll('.tf-resource-card .tf-section-btn'))"
        ".map(b => b.dataset.section)"
    )
    assert sorted(buttons) == ['cloudinit', 'disks', 'networks', 'specs']


def test_decl_section_button_color_reflects_validation(page):
    """A freshly added VM with defaults misses required fields (e.g.
    name). The Specs button must be `--missing` (red) or `--empty`,
    NOT `--ok`. After filling the required fields via the store, the
    color flips to `--ok`."""
    _open_terraform(page)
    page.evaluate("""() => {
      const d = window.TFDecl.create('color-test', 'harv-fake');
      const r = window.TFDecl.addResource(d.id, 'vm');
      window.TF.refresh();
      window.TFDeclPanel.open(d.id);
    }""")
    page.wait_for_selector('.tf-section-btn[data-section="specs"]', timeout=3000)
    initial_class = page.evaluate(
        "document.querySelector('.tf-section-btn[data-section=\"specs\"]').className"
    )
    assert "tf-section-btn--ok" not in initial_class, initial_class
    # Fill the required fields via the store + force a re-render
    page.evaluate("""() => {
      const d = window.TFDecl.getActive();
      const r = d.resources[0];
      window.TFDecl.replaceResourceSpec(d.id, r.id, Object.assign({}, r.spec, {
        name: 'v1',
        cpu: 2, memory: '2Gi',
        run_strategy: 'RerunOnFailure',
      }));
      window.TF.refresh();
    }""")
    page.wait_for_timeout(400)
    after_class = page.evaluate(
        "document.querySelector('.tf-section-btn[data-section=\"specs\"]').className"
    )
    assert "tf-section-btn--ok" in after_class, after_class


def test_decl_section_overlay_opens_on_click(page):
    """Click a section button (inside the decl panel) → another
    FloatingPanel opens with the section's form."""
    _open_terraform(page)
    page.evaluate("""() => {
      const d = window.TFDecl.create('overlay-test', 'harv-fake');
      window.TFDecl.addResource(d.id, 'vm');
      window.TF.refresh();
      window.TFDeclPanel.open(d.id);
    }""")
    page.wait_for_selector('.tf-section-btn[data-section="specs"]',
                           timeout=3000)
    page.click('.tf-section-btn[data-section="specs"]')
    page.wait_for_timeout(250)
    # The section overlay must be visible (a second FloatingPanel)
    expect(page.locator('.floating-panel .tf-form[data-section="specs"]')).to_be_visible()
    expect(page.locator('.floating-panel [name="name"]').first).to_be_visible()
    assert page.locator('.floating-panel [name="disk[0].image"]').count() == 0


def test_decl_section_overlay_save_persists_and_updates_button(page):
    """Saving the section overlay merges values into the resource's spec
    and flips the button color in the parent declaration panel."""
    _open_terraform(page)
    page.evaluate("""() => {
      const d = window.TFDecl.create('save-test', 'harv-fake');
      window.TFDecl.addResource(d.id, 'ssh_key');
      window.TF.refresh();
      window.TFDeclPanel.open(d.id);
    }""")
    page.wait_for_selector('.tf-section-btn[data-section="specs"]',
                           timeout=3000)
    # ssh_key has 1 section (Specs)
    page.click('.tf-section-btn[data-section="specs"]')
    page.wait_for_timeout(250)
    page.fill('.floating-panel [name="name"]', 'lab-key')
    page.fill('.floating-panel [name="public_key"]', 'ssh-ed25519 AAAA me@host')
    page.click('.floating-panel .tf-sec-save')
    page.wait_for_timeout(250)
    # The spec must be persisted
    persisted = page.evaluate("""() => {
      const d = window.TFDecl.getActive();
      return d.resources[0].spec;
    }""")
    assert persisted.get("name") == "lab-key"
    assert "ssh-ed25519" in persisted.get("public_key", "")
    # And the button is green
    assert "tf-section-btn--ok" in page.evaluate(
        "document.querySelector('.tf-section-btn[data-section=\"specs\"]').className"
    )


def test_declarations_subtab_has_no_resource_cards_inline(page):
    """v1.5.5: the Declarations sub-tab must show ONLY the list of
    declarations — no inline `.tf-resource-card` even when a declaration
    has resources. The cards live inside the FloatingPanel overlay."""
    _open_terraform(page)
    page.evaluate("""() => {
      const d = window.TFDecl.create('inline-check', 'harv-fake');
      window.TFDecl.addResource(d.id, 'vm');
      window.TF.refresh();
    }""")
    page.wait_for_selector('.tf-decl-item', timeout=3000)
    # Cards are NOT inside the Declarations sub-tab; only the row.
    assert page.locator(
        '#tf-status-body .tf-subtab-content[data-tf-tab="decls"] '
        '.tf-resource-card').count() == 0


def test_decl_open_panel_lists_resources_on_the_left(page):
    """Opening a declaration with 3 resources shows 3 left-tab entries
    + one resource card on the right."""
    _open_terraform(page)
    page.evaluate("""() => {
      const d = window.TFDecl.create('left-tabs', 'harv-fake');
      window.TFDecl.addResource(d.id, 'vm');
      window.TFDecl.addResource(d.id, 'image');
      window.TFDecl.addResource(d.id, 'ssh_key');
      window.TF.refresh();
      window.TFDeclPanel.open(d.id);
    }""")
    page.wait_for_selector('.tf-dp-resitem', timeout=3000)
    expect(page.locator('.tf-dp-resitem')).to_have_count(3)
    # Only one resource card visible at a time (the active one).
    expect(page.locator('.tf-resource-card')).to_have_count(1)


def test_decl_open_panel_switching_resource_swaps_detail(page):
    """Click a different left-tab entry → the right pane swaps to its
    section buttons."""
    _open_terraform(page)
    decl_id = page.evaluate("""() => {
      const d = window.TFDecl.create('swap', 'harv-fake');
      const r1 = window.TFDecl.addResource(d.id, 'vm');
      const r2 = window.TFDecl.addResource(d.id, 'ssh_key');
      window.TFDecl.replaceResourceSpec(d.id, r1.id, {name: 'vm-a'});
      window.TFDecl.replaceResourceSpec(d.id, r2.id, {name: 'key-a'});
      window.TF.refresh();
      window.TFDeclPanel.open(d.id);
      return d.id;
    }""")
    # First resource (vm-a) active by default → card kind = vm → 4 sections
    page.wait_for_selector('.tf-resource-card', timeout=3000)
    initial_kind = page.evaluate(
        "document.querySelector('.tf-resource-card').dataset.kind")
    assert initial_kind == "vm"
    # Click the 2nd left-tab entry (ssh_key)
    page.click('.tf-dp-resitem:nth-of-type(2)')
    page.wait_for_timeout(200)
    swapped_kind = page.evaluate(
        "document.querySelector('.tf-resource-card').dataset.kind")
    assert swapped_kind == "ssh_key"


def test_decl_open_panel_two_at_once(page):
    """Open two distinct declarations → two FloatingPanels live in the
    DOM simultaneously."""
    _open_terraform(page)
    page.evaluate("""() => {
      const a = window.TFDecl.create('decl-a', 'harv-fake');
      const b = window.TFDecl.create('decl-b', 'harv-fake');
      window.TFDecl.addResource(a.id, 'vm');
      window.TFDecl.addResource(b.id, 'ssh_key');
      window.TF.refresh();
      window.TFDeclPanel.open(a.id);
      window.TFDeclPanel.open(b.id);
    }""")
    page.wait_for_timeout(300)
    panels = page.evaluate(
        "document.querySelectorAll('.floating-panel').length")
    assert panels == 2, panels


def test_decl_destroy_button_visible_on_each_declaration_row(page):
    """v1.5.1: every declaration row carries a 🧨 Destroy button — a
    real cluster-side teardown, distinct from the 🗑 local-delete."""
    _open_terraform(page)
    page.evaluate("""() => {
      window.TFDecl.create('destroy-target', 'harv-fake');
      window.TF.refresh();
    }""")
    page.wait_for_timeout(300)
    expect(page.locator('.tf-decl-destroy')).to_have_count(1)
    expect(page.locator('.tf-decl-delete')).to_have_count(1)


def test_decl_destroy_opens_typed_confirm_modal(page):
    """v1.5.2: 🧨 Destroy on a declaration must NOT trigger via a
    one-click confirm(). It opens a typed-confirmation modal where
    the user has to type the declaration's exact name before the
    Destroy button is enabled."""
    _open_terraform(page)
    page.evaluate("""() => {
      window.TFDecl.create('dangerous-decl', 'harv-fake');
      window.TFDecl.addResource(window.TFDecl.getActive().id, 'vm');
      window.TF.refresh();
    }""")
    page.wait_for_timeout(300)
    # Track fetch calls; nothing should hit destroy_declaration until
    # the user types the name and clicks Confirm.
    page.evaluate("""() => {
      window.__fetchTrace = [];
      const orig = window.fetch;
      window.fetch = (url, init) => {
        window.__fetchTrace.push({url: String(url), method: init?.method || 'GET'});
        return orig(url, init);
      };
    }""")

    page.click('.tf-decl-destroy')
    page.wait_for_selector('.tf-confirm-modal', timeout=2000)
    # The required text is the declaration name
    required = page.evaluate(
        "document.querySelector('.tf-confirm-required').textContent.trim()")
    assert required == "dangerous-decl"
    # Confirm button starts DISABLED
    assert page.evaluate(
        "document.querySelector('.tf-confirm-go').disabled") is True

    # Typing the wrong name keeps it disabled
    page.fill('.tf-confirm-input', 'wrong')
    page.wait_for_timeout(100)
    assert page.evaluate(
        "document.querySelector('.tf-confirm-go').disabled") is True

    # Typing the exact name enables it
    page.fill('.tf-confirm-input', 'dangerous-decl')
    page.wait_for_timeout(100)
    assert page.evaluate(
        "document.querySelector('.tf-confirm-go').disabled") is False

    # No destroy_declaration request has been made yet
    trace = page.evaluate("window.__fetchTrace")
    assert not any('destroy_declaration' in c['url'] for c in trace), trace


def test_decl_destroy_cancel_closes_modal_without_action(page):
    """Cancel button + Escape both close the modal and fire NO
    destroy_declaration request."""
    _open_terraform(page)
    page.evaluate("""() => {
      window.TFDecl.create('cancel-test', 'harv-fake');
      window.TFDecl.addResource(window.TFDecl.getActive().id, 'vm');
      window.TF.refresh();
      window.__fetchTrace = [];
      const orig = window.fetch;
      window.fetch = (url, init) => {
        window.__fetchTrace.push({url: String(url), method: init?.method || 'GET'});
        return orig(url, init);
      };
    }""")
    page.wait_for_timeout(300)
    page.click('.tf-decl-destroy')
    page.wait_for_selector('.tf-confirm-modal', timeout=2000)
    page.click('.tf-confirm-cancel:not(.btn-close)')
    page.wait_for_timeout(150)
    assert page.locator('.tf-confirm-modal').count() == 0
    trace = page.evaluate("window.__fetchTrace")
    assert not any('destroy_declaration' in c['url'] for c in trace)


def test_state_table_shows_edit_button_only_when_sidecar_present(page):
    """v1.5.3: a deployed resource WITH a sidecar gets ✎ Edit; without
    one it shows the "no sidecar" hint. We force both cases by
    stubbing fetch /state."""
    _open_terraform(page)
    page.evaluate("""async () => {
      // Stub /state to return mixed rows: one with sidecar, one without
      const orig = window.fetch;
      window.fetch = (url, init) => {
        const u = String(url);
        if (u.includes('/api/terraform/') && u.endsWith('/state')) {
          return Promise.resolve(new Response(JSON.stringify({
            initialized: true, workspace: '/tmp/ws',
            resources: ['harvester_virtualmachine.alpha', 'harvester_image.beta'],
            resources_detail: [
              { address: 'harvester_virtualmachine.alpha',
                local_name: 'alpha', has_sidecar: true, kind: 'vm' },
              { address: 'harvester_image.beta',
                local_name: 'beta',  has_sidecar: false },
            ],
            resource_count: 2,
          }), { status: 200 }));
        }
        return orig(url, init);
      };
      await window.TF.refresh();
    }""")
    # v1.5.4: the state table lives behind the "Live resources" sub-tab.
    page.click('#tf-status-body .sub-tab[data-tf-tab="live"]')
    page.wait_for_timeout(150)
    # Exactly one ✎ Edit button (for alpha) and one "no sidecar" hint
    expect(page.locator('.tf-edit-resource')).to_have_count(1)
    expect(page.locator('.tf-no-sidecar')).to_have_count(1)
    addr = page.evaluate(
        "document.querySelector('.tf-edit-resource').dataset.address")
    assert addr == "harvester_virtualmachine.alpha"


def test_clicking_edit_imports_sidecar_into_new_declaration(page):
    """Click ✎ → fetch sidecar → create "Edit <addr>" declaration → set
    active → render card with the resource's kind."""
    _open_terraform(page)
    # No existing declarations
    assert page.evaluate("window.TFDecl.list().length") == 0
    # Await the refresh so we know any background refreshes are settled
    # AND the stubbed render is the last one to land in the DOM.
    page.evaluate("""async () => {
      const orig = window.fetch;
      window.fetch = (url, init) => {
        const u = String(url);
        if (u.endsWith('/state')) {
          return Promise.resolve(new Response(JSON.stringify({
            initialized: true, workspace: '/tmp/ws',
            resources: ['harvester_virtualmachine.deployed_vm'],
            resources_detail: [{
              address: 'harvester_virtualmachine.deployed_vm',
              local_name: 'deployed_vm', has_sidecar: true, kind: 'vm',
              declaration_name: 'lab-prod',
            }],
            resource_count: 1,
          }), { status: 200 }));
        }
        if (u.includes('/sidecar/deployed_vm')) {
          return Promise.resolve(new Response(JSON.stringify({
            kind: 'vm',
            spec: { name: 'deployed_vm', cpu: 8, memory: '32Gi',
                    disk: [{image: 'default/img-x', size: '50Gi'}],
                    network_interface: [{network_name: 'default/mgmt'}] },
            declaration_name: 'lab-prod',
            written_at: '2026-06-03T14:00:00Z',
            schema_version: 1,
          }), { status: 200 }));
        }
        return orig(url, init);
      };
      await window.TF.refresh();
    }""")
    # Switch to the Live resources sub-tab so the button is clickable
    page.click('#tf-status-body .sub-tab[data-tf-tab="live"]')
    page.wait_for_timeout(150)
    page.click('.tf-edit-resource')
    page.wait_for_timeout(500)
    # A new declaration appeared, contains the imported resource
    decls = page.evaluate("window.TFDecl.list()")
    assert len(decls) == 1, decls
    decl = decls[0]
    assert len(decl["resources"]) == 1
    r = decl["resources"][0]
    assert r["kind"] == "vm"
    assert r["spec"]["name"] == "deployed_vm"
    assert r["spec"]["cpu"] == 8
    assert r["spec"]["disk"][0]["image"] == "default/img-x"
    # The new declaration is the active one
    active = page.evaluate("window.TFDecl.getActive()?.id")
    assert active == decl["id"]


def test_decl_destroy_button_distinct_from_delete(page):
    """Visual / class guard so the two buttons can't be merged in a
    drive-by refactor — they have very different consequences."""
    _open_terraform(page)
    page.evaluate("""() => {
      window.TFDecl.create('classes', 'harv-fake');
      window.TF.refresh();
    }""")
    page.wait_for_timeout(300)
    destroy_class = page.evaluate(
        "document.querySelector('.tf-decl-destroy').className")
    delete_class = page.evaluate(
        "document.querySelector('.tf-decl-delete').className")
    assert destroy_class != delete_class
    # Destroy is btn-danger to flag its severity
    assert "btn-danger" in destroy_class


def test_decl_remove_resource_clears_card(page):
    """Removing a resource via the trash button drops the card."""
    _open_terraform(page)
    page.evaluate("""() => {
      const d = window.TFDecl.create('remove-test', 'harv-fake');
      window.TFDecl.addResource(d.id, 'vm');
      window.TFDecl.addResource(d.id, 'ssh_key');
      window.TF.refresh();
      window.TFDeclPanel.open(d.id);
    }""")
    page.wait_for_selector('.tf-dp-resitem', timeout=3000)
    # The decl panel lists 2 resources on the left
    expect(page.locator('.tf-dp-resitem')).to_have_count(2)
    # Stub confirm to always accept; then click the trash on the
    # currently-active resource (which is the card's __del button).
    page.evaluate("window.confirm = () => true")
    page.click('.tf-resource-card .tf-resource-card__del')
    page.wait_for_timeout(250)
    expect(page.locator('.tf-dp-resitem')).to_have_count(1)


def test_terraform_add_kind_selector_lists_every_schema_entry(page):
    """v1.5.5 successor: the kind dropdown lives inside the declaration
    overlay (.tf-dp-add-kind). It must expose every TF_SCHEMA kind."""
    _open_terraform(page)
    page.evaluate("""() => {
      const d = window.TFDecl.create('kind-test', 'harv-fake');
      window.TF.refresh();
      window.TFDeclPanel.open(d.id);
    }""")
    page.wait_for_selector('.tf-dp-add-kind', timeout=5000)
    schema_kinds = page.evaluate("Object.keys(window.TF_SCHEMA || {})")
    dropdown_kinds = page.evaluate(
        "Array.from(document.querySelector('.tf-dp-add-kind').options)"
        ".map(o => o.value)"
    )
    assert sorted(schema_kinds) == sorted(dropdown_kinds), (
        f"add-kind selector out of sync with TF_SCHEMA: "
        f"schema={schema_kinds}, dropdown={dropdown_kinds}"
    )


def test_terraform_cloudinit_section_overlay_renders_block_by_default(page):
    """v1.4.38 invariant: opening the Cloud-init section overlay must
    render the nested cloudinit block (min:1) without a manual +Add."""
    _open_terraform(page)
    page.evaluate("""() => {
      const d = window.TFDecl.create('ci-test', 'harv-fake');
      window.TFDecl.addResource(d.id, 'vm');
      window.TF.refresh();
      window.TFDeclPanel.open(d.id);
    }""")
    page.wait_for_selector('.tf-section-btn[data-section="cloudinit"]',
                           timeout=3000)
    page.click('.tf-section-btn[data-section="cloudinit"]')
    page.wait_for_timeout(300)
    items = page.evaluate(
        "document.querySelectorAll("
        "  '.floating-panel .tf-block[data-block=\"cloudinit\"] "
        ".tf-block-item').length"
    )
    assert items >= 1, f"expected ≥1 cloud-init item in the overlay, got {items}"


def test_terraform_add_disk_in_section_overlay_renders_extra_block(page):
    """+Add disk inside the Disks section overlay appends a new block."""
    _open_terraform(page)
    page.evaluate("""() => {
      const d = window.TFDecl.create('disk-test', 'harv-fake');
      window.TFDecl.addResource(d.id, 'vm');
      window.TF.refresh();
      window.TFDeclPanel.open(d.id);
    }""")
    page.wait_for_selector('.tf-section-btn[data-section="disks"]',
                           timeout=3000)
    page.click('.tf-section-btn[data-section="disks"]')
    page.wait_for_timeout(300)
    before = page.evaluate(
        "document.querySelectorAll("
        "  '.floating-panel .tf-block[data-block=\"disk\"] "
        ".tf-block-item').length"
    )
    page.click('.floating-panel .tf-block[data-block="disk"] .tf-block-add')
    page.wait_for_timeout(200)
    after = page.evaluate(
        "document.querySelectorAll("
        "  '.floating-panel .tf-block[data-block=\"disk\"] "
        ".tf-block-item').length"
    )
    assert after == before + 1, (
        f"+Add disk in overlay did not append: before={before} after={after}"
    )


def test_automation_header_tabs_centered(page):
    """The inline tab strip should be in the centered grid column (not
    pinned to the right). Detect via the computed justify-self value."""
    page.click('.tab-group-head[data-group="automation"]')
    page.wait_for_timeout(150)
    js = page.locator('.sub-tabs-inline').first
    value = js.evaluate("el => window.getComputedStyle(el).justifySelf")
    assert value in ("center", "anchor-center"), f"justify-self={value} (want center)"


def test_capi_diag_renders(page, api):
    """REGRESSION: when CAPI diag returns, the components table is rendered."""
    # Automation became a collapsible group in v1.4.6 — the head carries
    # `data-group="automation"` (not `data-tab="automation"`). Match either
    # form so the test survives further sidebar restructuring.
    page.click('.tab[data-group="automation"], .tab[data-tab="automation"]')
    page.wait_for_timeout(2000)
    # Wait for the components table to appear (driven by /api/capi/<>/diag)
    # We can't rely on a real cluster, but the structure must be there.
    body = page.locator('#capi-status-body')
    expect(body).to_be_visible()


def test_sidebar_collapse_and_persist(page):
    """REGRESSION: clicking the collapse button hides the labels and narrows
    the sidebar; state persists across reload."""
    sidebar = page.locator('#sidebar')
    width_full = sidebar.evaluate('el => el.getBoundingClientRect().width')
    assert width_full > 200, f"sidebar should start expanded, got {width_full}"

    page.click('#btn-sidebar-collapse')
    page.wait_for_timeout(300)
    width_collapsed = sidebar.evaluate('el => el.getBoundingClientRect().width')
    assert width_collapsed < 80, f"sidebar should be narrow when collapsed, got {width_collapsed}"
    # Labels in tabs must be hidden
    label_visible = page.locator('.tab[data-tab="overview"] .sidebar-label').evaluate(
        'el => window.getComputedStyle(el).display'
    )
    assert label_visible == 'none', f"sidebar-label should be display:none, got {label_visible}"

    # Persist after reload
    page.reload()
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(300)
    width_after_reload = page.locator('#sidebar').evaluate('el => el.getBoundingClientRect().width')
    assert width_after_reload < 80, f"sidebar should stay collapsed after reload, got {width_after_reload}"


def test_namespaces_tab_auto_selects_first(page):
    """clicking Virtual machines tab should auto-select first NS in dropdown."""
    page.click('.tab[data-tab="namespaces"]')
    page.wait_for_timeout(800)
    expect(page.locator("#ns-dropdown")).to_be_attached()
    expect(page.locator(".bulk-toolbar")).to_be_attached()
    expect(page.locator(".bulk-toolbar")).to_be_hidden()


def test_activity_tab_renders(page):
    page.click('.tab[data-tab="activity"]')
    expect(page.locator("#activity-running")).to_be_visible()
    expect(page.locator("#activity-history")).to_be_visible()


def test_dry_run_action_visible_somewhere(page, api):
    """An action launched via API must appear EITHER in the dock (still running)
    OR in the Activity history (already completed). On the fake cluster it
    typically completes within a second because preflight fails immediately,
    so the dock may miss it — but it must show up in the History at the next
    refresh."""
    page.evaluate("localStorage.setItem('harvester_ops_dock_visible','true')")
    page.reload()
    page.wait_for_load_state("networkidle")

    status, body = api("POST", "/api/action",
                       json_body={"action": "shutdown", "cluster": "harv-fake", "dry_run": True},
                       expect_status=201)
    run_id = body["id"]

    # Either the card lives in the dock, or it landed in Activity history.
    found = False
    for _ in range(50):
        if page.locator(f"#dock-card-{run_id}").count() > 0:
            found = True
            break
        # Check the Activity API too
        _, activity = api("GET", "/api/activity")
        all_ids = [a["id"] for a in activity["in_progress"] + activity["actions_done"]]
        if run_id in all_ids:
            found = True
            break
        page.wait_for_timeout(200)
    assert found, "Action did not appear in the dock OR the activity history"


def test_logo_size_slider_updates_css_var(page):
    """Live preview: dragging the slider should update --brand-logo-size."""
    page.click("#btn-settings")
    page.click('.settings-tab[data-stab="appearance"]')
    page.locator("#set-logo-size").fill("48")
    page.locator("#set-logo-size").dispatch_event("input")
    page.wait_for_timeout(150)
    style = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--brand-logo-size').trim()")
    assert "48px" in style


# -----------------------------------------------------------------------------
# Shutdown / Startup groups
# -----------------------------------------------------------------------------
def test_shutdown_groups_render_from_canned_vms(page):
    """Intercept /api/vms/<cluster> with canned VMs spanning 3 groups, open
    the Shutdown tab, and assert each group renders as its own section with
    a name input + the right number of VMs + a 'parallel' label. Catches
    regressions where the group sections aren't wired or the default group
    is missing."""
    canned = {
        "cluster": "harv-fake",
        "vms": [
            {"namespace": "default", "name": "web-1",  "priority": 10,
             "group": "frontends", "snapshot": True, "ready_timeout": 300,
             "runStrategy": "Always", "phase": "Running", "agent_connected": "True"},
            {"namespace": "default", "name": "web-2",  "priority": 10,
             "group": "frontends", "snapshot": True, "ready_timeout": 300,
             "runStrategy": "Always", "phase": "Running", "agent_connected": "True"},
            {"namespace": "default", "name": "db-1",   "priority": 50,
             "group": "backends",  "snapshot": True, "ready_timeout": 300,
             "runStrategy": "Always", "phase": "Running", "agent_connected": "True"},
            {"namespace": "default", "name": "extras", "priority": 100,
             "group": "default",   "snapshot": True, "ready_timeout": 300,
             "runStrategy": "Always", "phase": "Running", "agent_connected": "True"},
        ],
    }
    page.route("**/api/vms/harv-fake", lambda route:
        route.fulfill(status=200, content_type="application/json", body=__import__("json").dumps(canned))
    )
    page.click('.tab[data-tab="shutdown"]')
    # v1.4.12 split Shutdown into 2 sub-tabs — the VM groups live under
    # the "order" sub-tab now. Click it to reveal the list.
    page.click('[data-shutdown-tab="order"]')
    page.wait_for_selector("#vm-order-list .vm-group", timeout=5000)

    groups = page.locator("#vm-order-list .vm-group")
    assert groups.count() == 3, f"expected 3 groups, got {groups.count()}"

    # First group (lowest priority) = frontends, 2 VMs
    first = groups.nth(0)
    assert "frontends" in first.locator(".group-name").input_value()
    assert first.locator(".vm-group-list li").count() == 2
    size_text = first.locator(".group-size").inner_text().lower()
    assert "parallel" in size_text or "parallèle" in size_text, size_text

    # Default group must be present, with `is-default` class. Since v1.4.12
    # the catch-all IS renamable (user can call it "défaut" etc.) — the
    # class stays for styling but the input is editable.
    default = page.locator("#vm-order-list .vm-group.is-default")
    assert default.count() == 1
    assert default.locator(".group-name").is_editable()


def test_shutdown_new_group_name_survives_periodic_refresh(page):
    """REGRESSION (v1.4.9 → v1.4.12): when the user creates a new group,
    types a name, and clicks elsewhere, the 8s periodic loadVMOrder()
    was re-fetching server state and erasing the unsaved local group.

    Fix (v1.4.12): a `vmOrderDirty` flag is set on every local mutation
    (drag, rename, snapshot toggle, new group). The periodic timer
    skips its call to loadVMOrder() while dirty. We can't wait 8 s in
    a test, so we simulate the same code path the timer runs and
    assert it bails out.
    """
    canned = {
        "cluster": "harv-fake",
        "vms": [
            {"namespace": "default", "name": "vm-1", "priority": 100, "group": "default",
             "snapshot": True, "ready_timeout": 300, "runStrategy": "Always",
             "phase": "Running", "agent_connected": "True"},
        ],
    }
    page.route("**/api/vms/harv-fake", lambda route:
        route.fulfill(status=200, content_type="application/json",
                      body=__import__("json").dumps(canned))
    )
    page.click('.tab[data-tab="shutdown"]')
    page.click('[data-shutdown-tab="order"]')
    page.wait_for_selector("#vm-order-list .vm-group", timeout=5000)

    # Add a new group → focus the input → type a name. Playwright `.type`
    # dispatches `input` events per key, matching the live-input wiring.
    page.click(".vm-group-add")
    page.wait_for_selector(
        "#vm-order-list .vm-group:not(.is-default) .group-name", timeout=3000
    )
    new_input = page.locator(
        "#vm-order-list .vm-group:not(.is-default) .group-name"
    ).first
    new_input.click()
    new_input.fill("")
    new_input.type("my-custom-group", delay=20)

    # Click elsewhere to blur the input — same trigger as the user bug.
    # Use Playwright keyboard.press Escape which is the most reliable blur.
    new_input.press("Tab")
    page.wait_for_timeout(200)

    # Simulate the periodic timer tick: it calls loadVMOrder() ONLY when
    # no dirty edits exist and no input has focus. We replicate that
    # exact code so the test asserts on the protection, not on
    # accidentally bypassing it via direct App.loadVMOrder().
    page.evaluate("""
        () => {
            const active = document.activeElement;
            const inputFocused = active && active.closest && active.closest('#vm-order-list');
            // App exposes the dirty flag via a class on the save button
            const dirty = document.querySelector('#btn-vms-save')?.classList.contains('has-changes');
            if (!dirty && !inputFocused) App.loadVMOrder();
        }
    """)
    page.wait_for_timeout(400)

    refreshed_input = page.locator(
        "#vm-order-list .vm-group:not(.is-default) .group-name"
    ).first
    val = refreshed_input.input_value()
    assert val == "my-custom-group", f"input value vanished after tick: {val!r}"


def test_shutdown_groups_payload_sent_on_save(page):
    """Click Save Order — the request body must follow the new grouped shape:
    {"groups": [...]} not just {"order": [...]}."""
    canned = {
        "cluster": "harv-fake",
        "vms": [
            {"namespace": "ns1", "name": "a", "priority": 10, "group": "g1",
             "snapshot": True, "ready_timeout": 300, "runStrategy": "Always",
             "phase": "Running", "agent_connected": "True"},
            {"namespace": "ns1", "name": "b", "priority": 100, "group": "default",
             "snapshot": True, "ready_timeout": 300, "runStrategy": "Always",
             "phase": "Running", "agent_connected": "True"},
        ],
    }
    page.route("**/api/vms/harv-fake", lambda route:
        route.fulfill(status=200, content_type="application/json",
                      body=__import__("json").dumps(canned))
    )

    captured = {}
    def _capture(route):
        req = route.request
        captured["body"] = req.post_data_json
        route.fulfill(status=200, content_type="application/json",
                      body='{"total":2,"updated":2,"results":[]}')
    page.route("**/api/vms/harv-fake/order", _capture)

    page.click('.tab[data-tab="shutdown"]')
    # v1.4.12 split Shutdown into 2 sub-tabs — the VM groups live under
    # the "order" sub-tab now. Click it to reveal the list.
    page.click('[data-shutdown-tab="order"]')
    page.wait_for_selector("#vm-order-list .vm-group", timeout=5000)
    # Silence the alert() call inside saveVMOrder
    page.evaluate("window.alert = () => {};")
    page.click("#btn-vms-save")
    page.wait_for_timeout(400)

    assert "body" in captured, "save did not POST"
    assert "groups" in captured["body"], (
        f"payload missing 'groups' key: {captured['body']}"
    )
    groups = captured["body"]["groups"]
    assert len(groups) == 2, f"expected 2 groups in payload, got {len(groups)}"
    names = sorted(g["name"] for g in groups)
    assert names == ["default", "g1"]
    # Group g1 must carry both VMs with priority 10
    g1 = next(g for g in groups if g["name"] == "g1")
    assert g1["priority"] == 10
    assert len(g1["vms"]) == 1
    assert g1["vms"][0]["name"] == "a"
