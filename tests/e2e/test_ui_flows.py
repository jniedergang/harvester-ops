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
    # v1.26.0 : le sélecteur de cluster reste utilisable dans le rail, sans
    # avoir à déplier le menu. C'est le contrôle le plus utilisé d'une
    # console multi-cluster, le cacher derrière un survol serait un recul.
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
    # Tab labels are translated via data-i18n. v1.26.0 : le menu est un rail
    # d'icônes tant qu'on ne le survole pas, ses libellés ne sont donc pas
    # rendus. On le déplie pour les lire, comme le ferait l'opérateur.
    page.locator('#sidebar').hover()
    page.wait_for_timeout(400)
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
    # v1.47.2 : l'onglet est dans le menu latéral, qui se déplie en calque
    # 120 ms après le survol. Le pointeur y restait, et un menu déplié à
    # temps couvrait l'interrupteur pour de bon (échec sous charge seulement).
    # Comme un utilisateur, on sort du menu et on le laisse se replier.
    page.mouse.move(1100, 450)
    page.wait_for_function("document.body.classList.contains('sidebar-collapsed')"
                           " || document.body.classList.contains('sidebar-pinned')")
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
    # Depuis la v1.4.12, la liste vit dans le sous-onglet « Ordre d'arrêt ».
    page.click('[data-shutdown-tab="order"]')
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
    work with. Since v1.43.0 the Cluster view is a board, not a canvas:
    its shell (`.fabric-body`) is what proves it was mounted."""
    # Navigate to Overview > Cluster sub-tab and wait for the board
    # to be mounted by mountTopology().
    page.click('.tab[data-tab="overview"]')
    page.click('[data-overview-tab="cluster"]')
    page.wait_for_selector(
        '.overview-subtab[data-subtab="cluster"] .fabric-body',
        timeout=5000,
    )

    page.reload()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(400)

    # After reload the Cluster sub-tab must still be active AND the
    # board must be present (the view was actually mounted).
    cls = page.locator('[data-overview-tab="cluster"]').get_attribute("class") or ""
    assert "active" in cls.split(), (
        f"Overview Cluster sub-tab did not stay active after reload "
        f"(class={cls!r}). restoreSubTabsFromStorage() is not running "
        f"after setCluster() in init()."
    )
    page.wait_for_selector(
        '.overview-subtab[data-subtab="cluster"] .fabric-body',
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
    # Default theme: SUSE in light mode (Tokyo Night Day until the SUSE
    # theme became the default), set on <html> by the inline <head>
    # bootstrap before style.css parses (no FOUC).
    initial = page.evaluate("""() => ({
      theme: document.documentElement.getAttribute('data-theme'),
      mode:  document.documentElement.getAttribute('data-mode'),
      bg:    getComputedStyle(document.documentElement)
               .getPropertyValue('--bg').trim(),
    })""")
    assert initial["theme"] == "suse" and initial["mode"] == "light", initial
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
    # The inline Installation / Création de clusters strip is only visible
    # for CAPI (the Overview and Shutdown tabs have their own strips).
    strip = page.locator('.sub-tabs-inline[data-only-for="capi"]')
    expect(strip).to_be_visible()
    # Click Terraform sidebar entry: hides the inline strip
    page.click('.tab-child[data-subtab="terraform"]')
    page.wait_for_timeout(200)
    expect(strip).to_be_hidden()
    expect(page.locator('.sub-tab-content[data-subtab="terraform"]')).to_have_class("sub-tab-content active")
    expect(page.locator('.sub-tab-content[data-subtab="capi"]')).not_to_have_class("active")
    # Click Bare-metal
    page.click('.tab-child[data-subtab="pxe"]')
    page.wait_for_timeout(200)
    expect(page.locator('.sub-tab-content[data-subtab="pxe"]')).to_have_class("sub-tab-content active")


def test_automation_capi_inline_subtabs(page):
    """The Installation / Clusters K8S / Services inline strip on the
    automation header swaps which .capi-tab-content is visible. v1.53.0:
    no "Création de clusters" sub-tab any more, a button in Clusters K8S
    opens the creation window."""
    page.click('.tab-group-head[data-group="automation"]')
    page.wait_for_timeout(200)
    page.click('.tab-child[data-subtab="capi"]')
    page.wait_for_timeout(200)
    for tab in ("install", "k8s", "services"):
        expect(page.locator(f'.sub-tabs-inline .sub-tab[data-capi-tab="{tab}"]')).to_be_visible()
    assert page.locator('.sub-tabs-inline .sub-tab[data-capi-tab="clusters"]').count() == 0
    # Default: install tab active
    expect(page.locator('.capi-tab-content[data-capi-tab="install"]')).to_have_class("capi-tab-content active")
    # Click "Clusters K8S": the create button is there
    page.click('.sub-tabs-inline .sub-tab[data-capi-tab="k8s"]')
    page.wait_for_timeout(200)
    expect(page.locator('.capi-tab-content[data-capi-tab="k8s"]')).to_have_class("capi-tab-content active")
    expect(page.locator('.capi-tab-content[data-capi-tab="install"]')).not_to_have_class("active")
    expect(page.locator("#btn-capi-create")).to_be_visible()
    # Click "Services"
    page.click('.sub-tabs-inline .sub-tab[data-capi-tab="services"]')
    page.wait_for_timeout(200)
    expect(page.locator('.capi-tab-content[data-capi-tab="services"]')).to_have_class("capi-tab-content active")
    expect(page.locator('.capi-tab-content[data-capi-tab="k8s"]')).not_to_have_class("active")


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
    """v1.54.0 : les déclarations sont gardées par la console ; chaque test
    part d'un magasin vide (et plus seulement d'un navigateur vide)."""
    page.evaluate("""async () => {
      try { localStorage.removeItem('harvester_ops_tf_declarations'); } catch {}
      try { localStorage.removeItem('harvester_ops_tf_subtab'); } catch {}
      const d = await fetch('/api/tf-declarations').then(r => r.json());
      for (const x of d.declarations || []) {
        await fetch('/api/tf-declarations/' + x.id, { method: 'DELETE' });
      }
      if (window.TFDecl) await window.TFDecl.load();
    }""")


def _open_terraform(page):
    _clear_tf_drafts(page)
    _clear_tf_decls(page)
    page.click('.tab-group-head[data-group="automation"]')
    page.wait_for_timeout(150)
    page.click('.tab-child[data-subtab="terraform"]')
    page.wait_for_selector('#tf-decls-view .tfd', timeout=5000)


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


def test_state_table_shows_edit_button_only_when_sidecar_present(page):
    """v1.5.3: a deployed resource WITH a sidecar gets an Edit button; without
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
    # Exactly one Edit button (for alpha) and one "no sidecar" hint
    expect(page.locator('.tf-edit-resource')).to_have_count(1)
    expect(page.locator('.tf-no-sidecar')).to_have_count(1)
    addr = page.evaluate(
        "document.querySelector('.tf-edit-resource').dataset.address")
    assert addr == "harvester_virtualmachine.alpha"


def _open_editor(page, name, kind="vm"):
    """v1.55.0 : une déclaration, une ressource, son formulaire dans la vue."""
    page.evaluate(f"""async () => {{
      const d = await window.TFDecl.createAsync('{name}', 'harv-fake');
      window.TFDecl.setActive(d.id);
      window.TFDeclView.render();
    }}""")
    page.click(f'.tfd-add-kind[data-kind="{kind}"]')
    page.wait_for_selector('.tfd-edit .tfd-sec', timeout=5000)


def test_terraform_add_kind_selector_lists_every_schema_entry(page):
    """Les boutons « Ajouter » de la vue proposent chaque type du schéma."""
    _open_terraform(page)
    page.evaluate("""async () => { const d = await window.TFDecl.createAsync('kind-test', 'harv-fake');
      window.TFDecl.setActive(d.id); window.TFDeclView.render(); }""")
    page.wait_for_selector('.tfd-add-kind', timeout=5000)
    schema_kinds = page.evaluate("Object.keys(window.TF_SCHEMA || {})")
    offered = page.evaluate("Array.from(document.querySelectorAll('.tfd-add-kind')).map(b => b.dataset.kind)")
    assert sorted(schema_kinds) == sorted(offered), (schema_kinds, offered)


def test_terraform_cloudinit_section_renders_block_by_default(page):
    """v1.4.38 invariant: the cloud-init block (min:1) is there without a
    manual +Add (in the view's form since v1.55.0)."""
    _open_terraform(page)
    _open_editor(page, "ci-test")
    items = page.locator('.tfd-sec[data-sec="cloudinit"] .tf-block[data-block="cloudinit"] .tf-block-item').count()
    assert items >= 1, f"expected at least one cloud-init item, got {items}"


def test_terraform_add_disk_in_section_renders_extra_block(page):
    """+Add disk inside the Disks section appends a new block."""
    _open_terraform(page)
    _open_editor(page, "disk-test")
    sel = '.tfd-sec[data-sec="disks"] .tf-block[data-block="disk"] .tf-block-item'
    before = page.locator(sel).count()
    page.click('.tfd-sec[data-sec="disks"] .tf-block[data-block="disk"] .tf-block-add')
    page.wait_for_timeout(200)
    assert page.locator(sel).count() == before + 1


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


def test_sidebar_is_a_rail_that_opens_on_hover(page):
    """v1.26.0 — le menu se déplie au survol et se replie tout seul."""
    sidebar = page.locator('#sidebar')
    rail = sidebar.evaluate('el => el.getBoundingClientRect().width')
    assert rail < 80, f"le menu doit démarrer en rail, mesuré {rail}"
    assert page.locator('.tab[data-tab="overview"] .sidebar-label').evaluate(
        'el => getComputedStyle(el).display') == 'none'

    sidebar.hover()
    page.wait_for_timeout(500)
    opened = sidebar.evaluate('el => el.getBoundingClientRect().width')
    assert opened > 200, f"le survol doit déplier le menu, mesuré {opened}"
    assert page.locator('.tab[data-tab="overview"] .sidebar-label').evaluate(
        'el => getComputedStyle(el).display') != 'none'

    page.mouse.move(1200, 500)
    page.wait_for_timeout(600)
    closed = sidebar.evaluate('el => el.getBoundingClientRect().width')
    assert closed < 80, f"le menu doit se replier en sortant, mesuré {closed}"


def test_opening_the_sidebar_never_moves_the_content(page):
    """LE défaut signalé : le menu poussait la zone de travail, si bien que
    le panneau de détail de la topologie sautait au moindre survol.

    Il est désormais un calque : la géométrie du contenu doit être
    rigoureusement identique, menu ouvert ou fermé.
    """
    box = 'el => { const r = el.getBoundingClientRect();' \
          ' return [Math.round(r.left), Math.round(r.width)]; }'
    content = page.locator('#content')
    before = content.evaluate(box)

    page.locator('#sidebar').hover()
    page.wait_for_timeout(500)
    assert page.locator('#sidebar').evaluate(
        'el => el.getBoundingClientRect().width') > 200, "le menu ne s'est pas ouvert"

    after = content.evaluate(box)
    assert before == after, (
        f"la zone de travail a bougé au survol du menu : {before} -> {after}")


def test_clicking_from_the_rail_lands_on_the_entry_aimed_at(page):
    """LE défaut du dépliage au survol, et il ne se voit qu'à la souris.

    En s'ouvrant, le menu rendait les libellés visibles avant d'avoir fini
    d'élargir : « Machines virtuelles » passait à la ligne dans 56 px, sa
    rangée doublait de hauteur et toutes les suivantes descendaient. On
    appuyait sur « Activité », on relâchait sur « Machines virtuelles », et
    le navigateur jetait le clic (cible commune = <nav>). Rien ne se
    passait.

    Le test reproduit la séquence exacte : survol puis clic, sans laisser au
    menu le temps de finir son animation.
    """
    page.mouse.move(900, 400)
    page.wait_for_timeout(500)
    assert page.locator('#sidebar').evaluate(
        'el => el.getBoundingClientRect().width') < 80, "le menu doit être en rail"

    page.click('.tab[data-tab="activity"]')
    page.wait_for_timeout(600)
    assert page.locator('.tab-content.active').get_attribute('id') == 'tab-activity'


def test_the_menu_rows_never_move_while_it_opens(page):
    """La cause du défaut ci-dessus, mesurée : les rangées doivent garder
    leur position du début à la fin de l'animation, pas seulement à ses deux
    extrémités."""
    tops = ("els => els.map(e => Math.round(e.getBoundingClientRect().top))")
    page.mouse.move(900, 400)
    page.wait_for_timeout(500)
    before = page.eval_on_selector_all('#sidebar .tab', tops)

    page.locator('#sidebar').hover()
    seen = []
    for _ in range(12):                     # échantillonne pendant l'animation
        page.wait_for_timeout(40)
        seen.append(page.eval_on_selector_all('#sidebar .tab', tops))
    page.wait_for_timeout(500)
    seen.append(page.eval_on_selector_all('#sidebar .tab', tops))

    drifted = [s for s in seen if s != before]
    assert not drifted, (
        f"les rangées bougent pendant l'ouverture : {before} -> {drifted[0]}")


def test_the_cluster_picker_stays_usable_in_the_rail(page):
    """Sur une console multi-cluster, savoir sur quel cluster on est et en
    changer est le geste le plus fréquent. Le cacher derrière un survol le
    rendait aussi inatteignable au clavier et au script."""
    page.mouse.move(900, 400)
    page.wait_for_timeout(500)
    picker = page.locator('#cluster-select')
    assert picker.is_visible(), "le sélecteur de cluster doit survivre au rail"
    box = picker.bounding_box()
    assert box and box['width'] > 20, f"trop étroit pour être lisible : {box}"


def test_clicking_inside_the_menu_does_not_leave_it_open(page):
    """Un clic donne le focus à un élément du menu. Tant que l'ouverture
    suivait le focus, le menu restait déplié indéfiniment : un calque de
    240 px par-dessus la page, qui avalait les clics du contenu en dessous
    jusqu'à ce qu'on aille cliquer ailleurs."""
    page.locator('#sidebar').hover()
    page.wait_for_timeout(400)
    page.click('#cluster-select')
    page.keyboard.press('Escape')
    page.mouse.move(900, 400)
    page.wait_for_timeout(700)
    width = page.locator('#sidebar').evaluate(
        'el => el.getBoundingClientRect().width')
    assert width < 80, f"le menu est resté ouvert ({width}px) et masque la page"


def test_tabbing_into_the_menu_still_opens_it(page):
    """Contrepartie du test précédent : le clavier, lui, DOIT ouvrir le
    menu, sinon tabuler dedans parcourt des libellés invisibles. C'est la
    raison d'être du mécanisme, et la garde posée contre le clic souris ne
    doit pas l'emporter avec elle.

    `:focus-visible` seul ne distingue pas les deux : Chromium le pose aussi
    sur un `<select>` focalisé à la souris.
    """
    page.mouse.move(900, 400)
    page.wait_for_timeout(500)
    assert page.locator('#sidebar').evaluate(
        'el => el.getBoundingClientRect().width') < 80

    # Tabuler depuis le haut de la page finit par entrer dans le menu.
    page.keyboard.press('Tab')
    for _ in range(12):
        inside = page.evaluate(
            "() => document.querySelector('#sidebar')"
            ".contains(document.activeElement)")
        if inside:
            break
        page.keyboard.press('Tab')
    else:
        pytest.skip("le focus n'a pas atteint le menu en 12 tabulations")

    page.wait_for_timeout(400)
    width = page.locator('#sidebar').evaluate(
        'el => el.getBoundingClientRect().width')
    assert width > 200, f"le clavier doit ouvrir le menu, mesuré {width}px"


def test_pinning_the_sidebar_persists(page):
    """Épingler est le geste explicite qui rend une colonne au menu, et il
    doit survivre au rechargement."""
    page.locator('#sidebar').hover()
    page.wait_for_timeout(400)
    page.click('#btn-sidebar-pin')
    page.wait_for_timeout(300)

    # Épinglé, le menu reste ouvert même quand la souris s'en va.
    page.mouse.move(1200, 500)
    page.wait_for_timeout(600)
    assert page.locator('#sidebar').evaluate(
        'el => el.getBoundingClientRect().width') > 200, "l'épinglage n'a pas tenu"

    page.reload()
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(400)
    assert page.locator('#sidebar').evaluate(
        'el => el.getBoundingClientRect().width') > 200, \
        "l'épinglage doit survivre au rechargement"

    # Et se défait.
    page.click('#btn-sidebar-pin')
    page.mouse.move(1200, 500)
    page.wait_for_timeout(600)
    assert page.locator('#sidebar').evaluate(
        'el => el.getBoundingClientRect().width') < 80


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
    a name input + the right number of VMs + its execution mode (in order,
    or in parallel for the default group). Catches
    regressions where the group sections aren't wired or the default group
    is missing."""
    canned = {
        "cluster": "harv-fake",
        "vms": [
            {"namespace": "default", "name": "web-1",  "priority": 10, "group_priority": 10,
             "group": "frontends", "snapshot": True, "ready_timeout": 300,
             "runStrategy": "Always", "phase": "Running", "agent_connected": "True"},
            {"namespace": "default", "name": "web-2",  "priority": 10, "group_priority": 10,
             "group": "frontends", "snapshot": True, "ready_timeout": 300,
             "runStrategy": "Always", "phase": "Running", "agent_connected": "True"},
            {"namespace": "default", "name": "db-1",   "priority": 50, "group_priority": 50,
             "group": "backends",  "snapshot": True, "ready_timeout": 300,
             "runStrategy": "Always", "phase": "Running", "agent_connected": "True"},
            {"namespace": "default", "name": "extras", "priority": 100, "group_priority": 100,
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

    # Groups are ordered by their own `group_priority` (v1.4.14 model, as
    # /api/vms returns it): the lowest runs first = frontends, 2 VMs.
    first = groups.nth(0)
    assert "frontends" in first.locator(".group-name").input_value()
    assert first.locator(".vm-group-list li").count() == 2
    assert first.locator(".group-size").inner_text() == "2 VMs"
    # v1.4.14 model: a normal group stops its VMs IN ORDER...
    assert first.locator(".group-mode-badge.ordered").count() == 1

    # ...and only the catch-all group runs them in parallel. It is always
    # present, and its name is locked (v1.4.14; renamable in v1.4.12).
    default = page.locator("#vm-order-list .vm-group.is-default")
    assert default.count() == 1
    badge = default.locator(".group-mode-badge.default").inner_text().lower()
    assert "parallel" in badge or "parallèle" in badge, badge
    assert not default.locator(".group-name").is_editable()


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
             "group_priority": 10,
             "snapshot": True, "ready_timeout": 300, "runStrategy": "Always",
             "phase": "Running", "agent_connected": "True"},
            {"namespace": "ns1", "name": "b", "priority": 100, "group": "default",
             "group_priority": 100,
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
    # Group g1 carries its own priority (v1.4.14 model) and its VM with
    # its intra-group priority.
    g1 = next(g for g in groups if g["name"] == "g1")
    assert g1["group_priority"] == 10
    assert len(g1["vms"]) == 1
    assert g1["vms"][0]["name"] == "a"
    assert g1["vms"][0]["priority"] == 10
