"""v1.6.4 — topology VM detail panel actions + live re-grouping.

Source-level guards (no browser) for two Overview/topology fixes:

  1. Selecting a VM in the topology detail panel must expose the full
     operational action set (notes, edit, console, snapshot, migrate,
     start/stop), not just notes/edit/snapshot. Start/stop are available
     without the destructive unlock (parity with the Virtual machines
     tab); only delete stays gated.

  2. refresh() must compare each element's parent, not just the set of
     element ids — otherwise a VM that starts/stops keeps its id and is
     only recoloured in place instead of moving from the "Stopped /
     unscheduled" bucket to its host node.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
TOPO = (ROOT / "web" / "static" / "js" / "topology.js").read_text()
I18N = (ROOT / "web" / "static" / "js" / "i18n.js").read_text()


# ---------------------------------------------------------------------------
# Issue 2 — full action set on a selected VM
# ---------------------------------------------------------------------------

def test_vm_detail_exposes_full_action_set():
    for act in ("vm-notes", "vm-edit", "vm-console", "vm-snap",
                "vm-migrate", "vm-start", "vm-stop", "vm-delete"):
        assert f'data-act="{act}"' in TOPO, f"missing VM action button {act}"


def test_console_and_migrate_wired_to_their_modules():
    assert "window.VMConsole?.open?.(" in TOPO, "vm-console not wired to VMConsole"
    assert "window.VMMigrate?.open?.(" in TOPO, "vm-migrate not wired to VMMigrate"


def test_start_stop_not_gated_by_destructive_unlock():
    """The power actions must dispatch without requiring destructiveUnlocked
    — that's the whole point of the parity fix. We assert the dispatcher
    early-returns for start/stop before any unlock check."""
    assert "if (act === 'vm-start' || act === 'vm-stop') return runAction();" in TOPO


def test_delete_still_gated_by_destructive_unlock():
    """Irreversible actions keep the safety lock."""
    assert "act === 'vm-delete'" in TOPO
    # delete/cordon/drain share the unlock branch
    assert "if (!destructiveUnlocked) { alert(confirmI18n('topology.lockedHint')); return; }" in TOPO


def test_start_stop_are_contextual_on_run_strategy():
    """Show Start when Halted, Stop otherwise — mirrors the VM tab."""
    assert "v.run_strategy === 'Halted'" in TOPO


def test_i18n_has_console_and_migrate_keys_en_and_fr():
    # Two occurrences each: the EN block and the FR block.
    assert I18N.count("'topology.action.console'") >= 2
    assert I18N.count("'topology.action.migrate'") >= 2
    assert "'Migrate'" in I18N and "'Migrer'" in I18N


# ---------------------------------------------------------------------------
# Issue 1 — refresh re-groups a started/stopped VM
# ---------------------------------------------------------------------------

def test_refresh_compares_parent_not_just_ids():
    # The parent-aware helpers replaced the id-only ones.
    assert "_structureMap" in TOPO and "_cyStructureMap" in TOPO
    assert "_mapsEqual(newStruct, oldStruct)" in TOPO
    # The naive id-only comparison must be gone from the refresh decision.
    assert "_setsEqual(newIds, oldIds)" not in TOPO


def test_structure_map_reads_actual_compound_parent():
    """Must read e.parent() (rendered), not e.data('parent') (can be stale
    after an in-place data merge)."""
    assert "e.parent().nonempty()" in TOPO
