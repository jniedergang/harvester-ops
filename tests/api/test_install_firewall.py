"""Regression guards for the install.sh firewall step (v1.4.20).

These run at source level — no install.sh execution required — so they
catch refactors that quietly drop the firewall opening (which would
re-introduce the "the port works but only locally" footgun we hit in
v1.4.19).
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
INSTALL_SH = ROOT / "install.sh"


def test_install_sh_defines_setup_firewall():
    """`setup_firewall` function must exist."""
    src = INSTALL_SH.read_text()
    assert "setup_firewall()" in src or "setup_firewall ()" in src


def test_install_sh_uses_permanent_flag():
    """The recurring bug we want to never see again: opening a firewalld
    port WITHOUT --permanent is a runtime-only rule that vanishes at the
    next reload. The install.sh helper MUST commit permanently."""
    src = INSTALL_SH.read_text()
    assert "--permanent" in src, (
        "install.sh adds firewall ports without --permanent — runtime-only "
        "rules are lost on the next reload (v1.4.19 footgun)."
    )
    # And the runtime is re-synced via --reload right after
    assert "firewall-cmd --reload" in src


def test_install_sh_handles_both_firewalld_and_ufw():
    """Support the two managed firewalls (RHEL/SUSE → firewalld, Debian
    /Ubuntu → ufw). Plain iptables/nftables is acceptable to skip with
    a hint."""
    src = INSTALL_SH.read_text()
    assert "firewall-cmd" in src
    assert "ufw " in src
    # And mentions iptables/nftables fallbacks in the hint
    assert "iptables" in src
    assert "nftables" in src


def test_install_sh_wires_setup_firewall_into_main():
    """The helper must actually be CALLED from main(), not just declared.
    A common refactor mistake."""
    src = INSTALL_SH.read_text()
    # Find the main() function
    main_start = src.find("main()")
    assert main_start > 0
    # Look for the call after main() definition
    main_body = src[main_start:main_start + 3000]
    assert "setup_firewall" in main_body, (
        "setup_firewall is declared but never called from main()"
    )


def test_install_sh_reads_port_from_config():
    """We must open the port that the user actually configured in
    config.yaml — not a hard-coded 8090."""
    src = INSTALL_SH.read_text()
    # The yq read of web.bind_port appears at least twice (status print +
    # firewall step). We require both.
    assert src.count("web.bind_port") >= 2, (
        "install.sh should read web.bind_port from config.yaml for the "
        "firewall step (don't hard-code 8090)."
    )
