"""Disaster Recovery dans la navigation 2.0 (tâche 16) : contrôles source.

Les écrans de la 1.101.0 (replication*.js) ne recalculent aucune règle du
moteur : un bouton d'action vient de `actions: [{name, role, allowed}]` rendu
par le serveur (`plan_actions`, `allowed_actions` de bin/lib/replication.py),
grisé avec sa raison quand `allowed` est faux. Aucune table « état -> actions
permises » n'est recopiée dans le navigateur. Les branchements de la
navigation (adresse d'un contrat, pastilles par hôte, lien vers l'action
d'Activité) sont vérifiés ici à la source, en e2e dans
tests/e2e/test_dr_200.py.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
JS = ROOT / "web/static/js"
FILES = ["replication.js", "replication-plan.js", "replication-create.js"]

# états de ressource du moteur (bin/lib/replication.py, STATE_ACTIONS)
STATES = ("draft", "measuring", "protecting", "protected", "lagging", "drifting", "degraded",
          "source-unreachable", "failing_over", "failed_over", "unprotecting", "unprotected", "error")
ACTIONS = ("measure-start", "measure-stop", "protect", "failover", "failover-unplanned", "pause", "resume",
           "unprotect", "unprotect-delete-backups", "delete")


def _function(src, name):
    """Corps d'une fonction `function name(...) { ... }` (accolades équilibrées)."""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", src)
    assert m, name
    i, depth = m.end(), 1
    while depth:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
    return src[m.end():i - 1]


def role_rule_violations(src):
    """Écarts d'un module de réplication à la règle « le serveur décide ».

    - `actionButton` grise selon `a.allowed` et rien d'autre (ni rang de rôle,
      ni état) ;
    - aucun `allowed` n'est calculé dans le navigateur (affectation ou
      littéral `allowed:`) ;
    - aucune table état -> actions (`protected: ['failover', ...]`).
    """
    out = []
    if "function actionButton" in src:
        body = _function(src, "actionButton")
        if "a.allowed" not in body:
            out.append("actionButton does not read a.allowed")
        if re.search(r"roleRank|\brole\b\s*[=!<>]|\.state\b", body):
            out.append("actionButton derives its own rule")
    if re.search(r"\.allowed\s*=(?!=)|\ballowed\s*:", src):
        out.append("allowed computed in the browser")
    states = "|".join(re.escape(s) for s in STATES)
    acts = "|".join(re.escape(a) for a in ACTIONS)
    if re.search(r"['\"]?(?:" + states + r")['\"]?\s*:\s*\[\s*['\"](?:" + acts + r")['\"]", src):
        out.append("state -> actions table copied")
    return out


@pytest.mark.parametrize("name", FILES)
def test_replication_screens_take_actions_from_the_server(name):
    assert role_rule_violations((JS / name).read_text(encoding="utf-8")) == []


def test_action_buttons_are_greyed_with_their_reason():
    src = (JS / "replication.js").read_text(encoding="utf-8")
    body = _function(src, "actionButton")
    assert "deniedTip(a)" in body and "disabled" in body
    tip = _function(src, "deniedTip")
    assert "a.reason" in tip and "a.role" in tip


# Mutations : une copie qui recalcule `allowed` doit être attrapée.
MUTATIONS = [
    ("role rank instead of allowed",
     "a.allowed ? '' : ' disabled aria-disabled=\"true\"'",
     "roleRank(row.role) >= 2 ? '' : ' disabled aria-disabled=\"true\"'"),
    ("allowed recomputed",
     "const a = action(row, name);",
     "const a = action(row, name); if (a) a.allowed = row.state !== 'failed_over';"),
    ("state table copied",
     "const roleRank = ",
     "const ALLOWED = { protected: ['failover', 'pause'] };\n  const roleRank = "),
]


@pytest.mark.parametrize("why,old,new", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_dr_mutation_role_copy(why, old, new):
    src = (JS / "replication.js").read_text(encoding="utf-8")
    assert old in src, why
    assert role_rule_violations(src.replace(old, new, 1)), why


# --- branchements de la navigation -------------------------------------------------

def test_contract_links_are_addresses_and_the_root_follows_them():
    src = (JS / "replication.js").read_text(encoding="utf-8")
    assert 'href="#"' not in src
    assert src.count('href="#dr/${enc(') >= 4
    click = _function(src, "onClick")
    assert "App.openDrPlan" in click and "cur.mode === 'all'" in click
    plan = (JS / "replication-plan.js").read_text(encoding="utf-8")
    assert "isOpen" in plan and "Router.syncFromScreen()" in plan
    assert 'href="#activity/action/${enc(e.action_id)}"' in plan


def test_host_badges_count_source_and_recovery_site():
    src = (JS / "replication.js").read_text(encoding="utf-8")
    paint = _function(src, "paintBadges")
    assert "a.source_host === h || a.target_host === h" in paint
    assert '[data-rp-badge="host"]' in paint
    nav = (JS / "host-nav.js").read_text(encoding="utf-8")
    assert re.search(r"h\.kind === 'harvester' \? `<a class=\"rp-menu-badge hnav-dr-badge tip\" data-rp-badge=\"host\"", nav)


def test_the_management_network_is_never_offered_as_a_mapping():
    """Le moteur ne fait correspondre qu'un réseau multus à un autre réseau
    multus de la cible (vm_transfer.retarget_vm, contrôle `network-unmapped`
    sur les NetworkAttachmentDefinitions) : proposer le réseau de gestion
    (pod) dans la liste créerait un contrat que `check` refuse."""
    src = (JS / "replication-create.js").read_text(encoding="utf-8")
    nets = _function(src, "netsHtml")
    assert "noDstNet" in nets and "noDstNetHint" in nets
    code = re.sub(r"^\s*//.*$", "", nets.split("data-rpc-netform")[0], flags=re.M)   # commentaires exclus
    assert not re.search(r"\bpod\b|mgmt|management", code, re.I)
    vt = (ROOT / "bin/lib/vm_transfer.py").read_text(encoding="utf-8")
    assert 'mul["networkName"] = networks[mul["networkName"]]' in vt
