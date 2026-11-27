"""2.10.1 tâche 2 : commandes du mode A rejouables et rôle des jetons enfants
d'OpenTofu lié à l'adresse de la console (point 327 A).

* `bao auth enable` des montages `jwt` (personnes et ESO) est gardé par une
  lecture de `bao auth list` : une seconde exécution des mêmes commandes ne
  rend plus « path is already in use » ;
* le rôle `auth/token/roles/<tf>` du mode A porte `token_bound_cidrs` = adresse
  de la console, comme en B1 (127.0.0.1/32).

Mutations rapportées (rapport 2.10.1, tâche 2) : « garde retirée » (ligne
`bao auth enable` nue) -> `test_jwt_mount_enable_is_guarded` échoue sur
chaque ligne du tableau ; « rôle sans adresse » (tf_token_role_params(cid)) ->
`test_mode_a_tf_token_role_bound_to_console` échoue.

Hypothèse à confirmer au banc (tâche 1, essai 4) : la clé du montage dans
`bao auth list -format=json` est `<montage>/` sur OpenBao 2.7.2 et Vault
1.14.2 (forme documentée par les deux, non rejouée ici).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import openbao_policy as pol  # noqa: E402

CID = "1a2b3c4d5e6f"
PREFIX = f"consoles/{CID}"
PEM = "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----"
ADDR = "https://bao.example:8200"


def _guard(m):
    return f"bao auth list -format=json | grep -q '\"{m}/\"' || bao auth enable -path={m} jwt"


@pytest.mark.parametrize("cmds,mount", [
    (lambda: pol.person_token_commands(ADDR, CID, "rmgr", "client-x", "https://rancher.example/oidc", None,
                                       "10.0.0.5/32", None), f"jwt-hops-{CID}-p-rmgr"),
    (lambda: pol.person_token_commands(ADDR, CID, "rmgr", "client-x", "https://rancher.example/oidc", None,
                                       "10.0.0.5/32", [PEM]), f"jwt-hops-{CID}-p-rmgr"),
    (lambda: pol.eso_cluster_commands(ADDR, "harvester-ops", PREFIX, CID, "1d905023",
                                      "https://kubernetes.default.svc.cluster.local", [PEM], ["team-a"],
                                      "172.16.2.0/24"), f"jwt-hops-{CID}-eso-1d905023"),
])
def test_jwt_mount_enable_is_guarded(cmds, mount):
    c = cmds()
    assert _guard(mount) in c
    bare = [x for x in c if x.startswith("bao auth enable")]
    assert bare == [], "aucune ligne `bao auth enable` nue (non rejouable)"
    assert not any("sautez" in x for x in c), "plus de consigne « sautez » : la ligne se rejoue"


def test_mode_a_tf_token_role_bound_to_console():
    c = pol.mode_a_commands(ADDR, "harvester-ops", PREFIX, CID, "10.0.0.5/32")
    line = next(x for x in c if x.startswith(f"bao write auth/token/roles/{pol.tf_role(CID)} "))
    assert "token_bound_cidrs=10.0.0.5/32" in line
    assert "allowed_policies=" in line and "renewable=false" in line


@pytest.mark.parametrize("cidr", ["any", "10.0.0.5", ""])
def test_mode_a_bad_console_cidr_refused(cidr):
    with pytest.raises(ValueError):
        pol.mode_a_commands(ADDR, "harvester-ops", PREFIX, CID, cidr)


# ---------------------------------------------------------------------------
# Badge du mode A : « prêt » dès que les magasins des namespaces activés
# sont Ready (même source que le mode B1 : child_eso.stores_ready)
# ---------------------------------------------------------------------------
from test_capi_eso_cli_2100 import (CL, KEY, NS, STORE, Child, cli, world, world_ok)  # noqa: E402,F401


@pytest.mark.parametrize("namespaces,stores_ready,applied,want", [
    (["team-a", "team-b"], True, False, True),       # mode A, commandes passées : magasins prêts
    (["team-a", "team-b"], False, False, False),     # mode A, en attente de l'administrateur
    (["team-a", "team-c"], True, False, False),      # team-c sans magasin
    ([], True, False, False),                        # aucun namespace activé : rien de prêt
    (["team-a"], True, True, True),                  # B1 (appliqué) : inchangé
])
def test_status_auth_stores_ready(cli, world, capsys, namespaces, stores_ready, applied, want):
    import json
    world["settings"](mode="a", eso={"listener": None, "clusters": {KEY: {
        "mount": STORE["jwt_mount"], "keys_sha256": "ab", "applied": applied, "namespaces": namespaces}}})
    world["box"]["child"] = Child(stores_ready=stores_ready)
    assert world["run"](world_ok(), ["eso-status", "--kubeconfig", "/x", "--name", f"{NS}/{CL}", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["auth"]["stores_ready"] is want


def test_badge_ready_when_stores_ready():
    """Le badge de l'onglet lit `auth.stores_ready` avant « en attente »."""
    src = (ROOT / "web" / "static" / "js" / "capi-eso.js").read_text()
    i = src.index("function authHtml(a)")
    body = src[i:src.index("function stateHtml", i)]
    assert body.index("a.stores_ready") < body.index('data-eso-auth="waiting"')
    assert 'data-eso-auth="ready"' in body
