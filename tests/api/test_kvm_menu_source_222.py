"""2.2.2 : la vue Machines virtuelles et la page d'un hôte KVM rendent le menu
d'une VM KVM par le même constructeur (kvm.js openVmMenu / vmMenuHtml).

Garde de source : all-vms.js ne recopie aucune règle (refus des gestes par
état, droits des opérations, « Modifier ») ; il passe les tables servies par
la console (`kvm_tables` de /api/vms-all) au constructeur partagé, et la page
de l'hôte passe par le même."""

import re
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[2] / "web" / "static" / "js"
ALL_VMS = (JS / "all-vms.js").read_text()
KVM = (JS / "kvm.js").read_text()


def body(src, head):
    """Corps d'une fonction (de sa ligne de tête à la ligne `  }` suivante)."""
    i = src.index(head)
    return src[i:src.index("\n  }\n", i)]


@pytest.mark.parametrize("copy", [
    "data-kvm-gesture", "data-kvm-op", "data-kvm-hot", "data-kvm-console",
    "gestureWhy", "operationWhy", "editWhy", "menuGroupHtml",
    "'shut off'", "\"shut off\"", "kvm.operatorOnly", "kvm.adminOnly", "kvm.why.",
    "/api/kvm/vms/",
])
def test_the_unified_view_copies_no_rule_of_the_host_page_menu(copy):
    assert copy not in ALL_VMS, copy


def test_the_unified_view_uses_the_shared_builder_with_the_served_tables():
    menu = body(ALL_VMS, "function openKvmMenu(")
    assert "KVM.openVmMenu(" in menu
    assert re.search(r"tables:\s*\(data && data\.kvm_tables\)", menu)
    assert "KVM.runGesture(" in menu
    assert "KVM.closeVmMenu" in ALL_VMS


def test_the_host_page_goes_through_the_same_builder():
    host = body(KVM, "function openMenu(")
    assert "openVmMenu(anchor, v, { tables: cur.data, role: role()" in host
    shared = body(KVM, "function openVmMenu(")
    assert "vmMenuHtml(v, o.tables, o.role)" in shared
    html = body(KVM, "function vmMenuHtml(")
    for part in ("GESTURES.map(item)", "gestureWhy(", "operationWhy(", "KVMForm.menuGroupHtml(v, t.edit_operations, have)",
                 "data-kvm-console"):
        assert part in html, part
    assert "openVmMenu, closeVmMenu: closeMenu, runGesture" in KVM
