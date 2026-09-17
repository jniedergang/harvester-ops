"""v1.27.0 — les deux filets de sécurité de l'arrêt, trouvés en s'en servant.

Découverts en éteignant harv3 pour de vrai. Les tests automatisés ne les
voyaient pas : ils n'exercent pas le script contre un cluster.

1. L'attente du détachement des volumes Longhorn ne surveillait RIEN.
   Elle triait les volumes de VM en cherchant « virt-launcher » dans
   `workloadName`. Longhorn y met le NOM DE LA VM (« mlm », « rhel9-test ») ;
   c'est `workloadType` qui vaut `VirtualMachineInstance`, et
   « virt-launcher » n'apparaît que dans `podName`, champ qui n'était même
   pas extrait. Le filtre ne correspondait donc jamais : l'étape rendait la
   main aussitôt en annonçant « tous les volumes de VM sont détachés », et
   rangeait le disque de la VM qu'on venait d'arrêter parmi les volumes de
   pods à ignorer.

   C'est l'invariant que l'outil met en avant (« aucune perte de données
   Longhorn ») : couper l'alimentation pendant qu'un volume est encore
   attaché est exactement ce que cette étape doit empêcher.

   Vérifié sur harv1 en service : le filtre d'origine renvoyait 0
   correspondance alors que deux volumes de VM étaient attachés.

2. Le cordon se déclarait réussi quoi qu'il arrive : `|| true` avalait
   l'erreur et l'étape émettait « done » sans condition.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / "bin" / "harvester-shutdown.sh"


def src():
    return SCRIPT.read_text()


def longhorn_step():
    s = src()
    return s[s.index("step_longhorn_maintenance()"):].split("\n}", 1)[0]


def cordon_step():
    s = src()
    return s[s.index("step_cordon()"):].split("\n}", 1)[0]


# ---------------------------------------------------------------------------
# Filet 1 : les volumes Longhorn
# ---------------------------------------------------------------------------

def test_vm_volumes_are_matched_on_the_workload_type():
    step = longhorn_step()
    assert "workloadType" in step, \
        "le jsonpath doit extraire le TYPE du workload, seul discriminant fiable"
    assert "grep 'VirtualMachineInstance'" in step


def test_the_broken_virt_launcher_filter_is_gone():
    """Le nom du workload est celui de la VM : y chercher « virt-launcher »
    ne correspond à rien, jamais."""
    step = longhorn_step()
    assert "grep 'virt-launcher'" not in step
    assert "workloadsStatus[*].workloadName" not in step


def test_pod_volumes_are_still_excluded_from_the_wait():
    """L'intention de la v1.8.9 reste : ne pas attendre les volumes de pods
    (upgradelog, monitoring), qui ne se détachent jamais et brûlaient les
    trois minutes de délai."""
    step = longhorn_step()
    assert "grep -v 'VirtualMachineInstance'" in step


def test_the_two_filters_are_complementary():
    """Le même critère des deux côtés : sinon un volume pourrait être
    compté dans les deux listes, ou dans aucune."""
    step = longhorn_step()
    keep = set(re.findall(r"grep '([^']+)'", step))
    # `^$` retire les lignes vides, ce n'est pas un critère de tri.
    drop = set(re.findall(r"grep -v '([^']+)'", step)) - {"^$"}
    assert keep and drop, (keep, drop)
    assert keep == drop, \
        f"critères divergents : gardés {sorted(keep)}, exclus {sorted(drop)}"


# ---------------------------------------------------------------------------
# Filet 2 : le cordon
# ---------------------------------------------------------------------------

def test_a_failed_cordon_is_not_reported_as_success():
    step = cordon_step()
    assert "cordon \"$node\" || true" not in step, \
        "`|| true` avale l'échec et l'étape se déclare réussie"
    assert 'emit_event "cordon" "warn"' in step, \
        "un échec réel doit remonter en avertissement"
    assert "failed=$((failed + 1))" in step


def test_the_single_node_refusal_is_told_apart_from_a_real_failure():
    """Sur un mono-node, le webhook Harvester refuse de cordonner le dernier
    node. C'est légitime et sans conséquence quand on éteint tout le
    cluster, mais ça ne doit ni passer pour un succès ni pour une panne."""
    step = cordon_step()
    assert "last available node" in step
    assert 'emit_event "cordon" "skipped"' in step


def test_the_step_counts_what_it_actually_cordoned():
    step = cordon_step()
    assert 'emit_event "cordon" "done" "$ok_count node(s) cordoned"' in step, \
        "le compte doit refléter le réel, pas une constante"


def test_the_script_still_parses():
    """Garde-fou : ces tests lisent du texte, ils ne diraient rien d'un
    script devenu incorrect."""
    import subprocess
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------------------
# Filet 3 : la restauration au démarrage
#
# Trouvé en cherchant pourquoi harv1 affichait « NotReady ». Le journal du
# démarrage du 09/09/2026 dit tout :
#
#     [WARN ] patch concurrent-rebuild échoué
#     [OK   ] Cluster restauré : rebuild ON, nodes uncordoned
#
# Le patch a échoué, l'étape a annoncé le contraire, et harv1 a tourné SIX
# JOURS avec la reconstruction de réplicas Longhorn désactivée sans que rien
# ne le signale. Cause probable : au redémarrage, les nodes passent Ready
# bien avant que le webhook de Longhorn ne réponde.
# ---------------------------------------------------------------------------

STARTUP = ROOT / "bin" / "harvester-startup.sh"


def restore_step():
    s = STARTUP.read_text()
    return s[s.index("step_restore_cluster_state()"):].split("\n}", 1)[0]


def test_a_failed_rebuild_patch_is_not_announced_as_success():
    step = restore_step()
    assert 'emit_event "restore" "warn"' in step, \
        "une restauration incomplète doit remonter, pas se déclarer done"
    assert "rebuild_ok" in step


def test_the_patch_is_retried_because_longhorn_lags_behind_the_nodes():
    step = restore_step()
    assert "for attempt in" in step, \
        "sans reprise, le patch retombe sur la fenêtre où Longhorn n'est pas prêt"
    assert "sleep 10" in step


def test_the_value_is_read_back_after_patching():
    """Un patch accepté n'est pas un patch appliqué : relire est le seul
    moyen de savoir si le cluster est vraiment sorti du mode maintenance."""
    step = restore_step()
    assert "jsonpath='{.value}'" in step
    assert '"$value" != "0"' in step


def test_a_failed_uncordon_is_reported():
    step = restore_step()
    assert "uncordon_failed" in step
    assert "uncordon \"$node\" || true" not in step


def test_the_startup_script_still_parses():
    import subprocess
    r = subprocess.run(["bash", "-n", str(STARTUP)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
