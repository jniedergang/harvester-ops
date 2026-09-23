"""v1.44.10 : l'extinction n'éteignait qu'UN nœud sur trois.

Constaté en filmant la démonstration : le journal annonçait « 3
control-plane node(s) éteints » après une seule ligne « CP shutdown (1/3) »,
et les trois machines du banc tournaient toujours.

Cause : `ssh` lit l'entrée standard. Appelé dans une boucle
`while read ... done < <(liste)`, il AVALE le reste de la liste, la boucle
s'arrête après le premier tour, et l'étape se déclare réussie. C'est
l'invariant central du produit (éteindre un cluster proprement) qui tombait
en silence.

Le correctif tient en deux caractères, `-n`, et ces tests le tiennent : le
premier reproduit la boucle pour de vrai avec un faux `ssh`, les suivants
gardent les appels du script.
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
COMMON = ROOT / "bin" / "lib" / "common.sh"
SHUTDOWN = ROOT / "bin" / "harvester-shutdown.sh"


def run_loop(tmp_path, ssh_flags_ok=True):
    """Rejoue la boucle d'extinction avec un faux ssh qui note ses appels."""
    fake = tmp_path / "bin"
    fake.mkdir()
    calls = tmp_path / "calls.txt"
    # `printf`, pas `echo` : le `echo` de dash prend un premier argument
    # « -n » pour son propre drapeau, avale le flag qu'on teste et colle
    # toutes les lignes ensemble.
    (fake / "ssh").write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {calls}\n"
        # Un vrai ssh consomme l'entrée standard quand on ne lui passe pas -n.
        "case \" $* \" in *\" -n \"*) : ;; *) cat > /dev/null ;; esac\n")
    (fake / "ssh").chmod(0o755)
    script = tmp_path / "loop.sh"
    script.write_text(f"""#!/usr/bin/env bash
source "{COMMON}"
SSH_USER=rancher
SSH_OPTS="-o StrictHostKeyChecking=accept-new"
DRY_RUN=0
while IFS='|' read -r host ip; do
    [[ -z "$host" ]] && continue
    ssh_exec "$ip" "sudo shutdown -h +0" || true
done < <(printf 'n3|10.0.0.3\\nn2|10.0.0.2\\nn1|10.0.0.1\\n')
""")
    script.chmod(0o755)
    env = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}", NO_COLOR="1")
    subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True,
                   check=True, timeout=60)
    return calls.read_text().splitlines() if calls.exists() else []


def test_every_node_of_the_list_is_reached(tmp_path):
    lines = run_loop(tmp_path)
    assert len(lines) == 3, f"seulement {len(lines)} nœud(s) atteint(s) : {lines}"
    assert all("10.0.0." in line for line in lines)


def test_the_helper_passes_dash_n_to_ssh():
    text = COMMON.read_text()
    for helper in ("ssh_exec()", "ssh_exec_quiet()"):
        body = text[text.index(helper):].split("\n}", 1)[0]
        assert " ssh -n " in body, f"{helper} : ssh sans -n, la boucle appelante sera vidée"


def test_the_shutdown_still_sends_its_command_through_the_helper():
    """Si un jour l'étape appelle `ssh` en direct, elle repassera à côté du
    correctif : le garde-fou vit ici."""
    text = SHUTDOWN.read_text()
    step = text[text.index("step_shutdown_cp()"):].split("\n}", 1)[0]
    assert "ssh_exec " in step
    assert "\n        ssh " not in step
