"""2.10.0 tâche 5 : test de source, aucune lecture de secret personnel dans un
fil de travail sans identité passée.

Dans les fonctions qui tournent dans le fil d'une action (liste nommée),
chaque appel d'un lecteur d'une classe personnelle (`ssh-cluster`,
`ssh-kvm`, `tf-kvm`) porte `identity=` : le service, choisi exprès (la
copie lue au déclenchement par la personne est servie, une lecture tardive
est marquée). Un appel sans identité relirait la requête (absente dans un
fil) et passerait en service sans marque. Et aucune de ces fonctions ne
calcule l'identité d'une requête (`_secret_identity`).
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
APP = ROOT / "web" / "app.py"

# fonctions de fil (actions, déroulés) qui remettent ou lisent des secrets
WORKER_FUNCS = ("run_action_thread", "_vm_transfer_runner", "_tf_decl_runner_body", "_tf_kvm_check_bases",
                "_tf_decl_runner", "_deploy_runner", "_deploy_terraform_phase", "_undo_runner", "_undo_phases")
# lecteurs d'une classe personnelle (ou qui en remettent une copie)
READERS = ("_spawn_secret_env", "_cluster_ssh_key_path", "_tf_kvm_stage", "_tf_kvm_env", "_tf_kvm_secret",
           "_kvm_virsh", "key_path", "path_of", "resolve_value", "shared_file", "materialize", "ssh_options")


def _funcs():
    tree = ast.parse(APP.read_text())
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def _name(call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)


def _violations(funcs):
    out = []
    for fn in WORKER_FUNCS:
        node = funcs[fn]
        for call in (c for c in ast.walk(node) if isinstance(c, ast.Call)):
            name = _name(call)
            if name == "_secret_identity":
                out.append(f"{fn}: computes a request identity")
            if name in READERS and not any(k.arg == "identity" for k in call.keywords):
                out.append(f"{fn}:{call.lineno} {name} without identity=")
    return out


def test_worker_functions_exist():
    funcs = _funcs()
    assert [f for f in WORKER_FUNCS if f not in funcs] == []


def test_no_person_class_read_without_identity_in_workers():
    assert _violations(_funcs()) == []


def test_workers_do_read_secrets():
    """La liste n'est pas vide de sens : chacune des fonctions qui remettent
    des secrets en appelle bien un lecteur (sinon le test ne prouverait rien)."""
    funcs = _funcs()
    for fn in ("run_action_thread", "_vm_transfer_runner", "_tf_decl_runner_body", "_tf_kvm_check_bases"):
        names = {_name(c) for c in ast.walk(funcs[fn]) if isinstance(c, ast.Call)}
        assert names & set(READERS), fn


@pytest.mark.parametrize("src,bad", [
    ("def run_action_thread(run):\n    _spawn_secret_env([run.cluster])\n", True),
    ("def run_action_thread(run):\n    _spawn_secret_env([run.cluster], identity=_sst.SERVICE)\n", False),
    ("def run_action_thread(run):\n    x = _secret_identity('ssh-kvm')\n", True),
])
def test_detector(src, bad):
    funcs = {n: ast.parse("def %s(): pass" % n).body[0] for n in WORKER_FUNCS}
    funcs["run_action_thread"] = ast.parse(src).body[0]
    assert bool(_violations(funcs)) is bad
