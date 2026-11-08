"""2.9.0 tâche 2 : client OpenBao contre un vrai serveur de banc (`--live`).

Jamais le Vault du homelab : adresse en boucle locale seulement, port 8200
refusé, `VAULT_*` et `BAO_*` vidés. Le banc fournit (variables d'environnement
du lanceur, jamais de valeur en argument) :

    HOPS_BAO_BENCH_ADDR        https://127.0.0.1:8210
    HOPS_BAO_BENCH_CA          fichier PEM de la CA du banc
    HOPS_BAO_BENCH_TOKEN_FILE  fichier 0600 d'un jeton d'administration du banc

Le test monte un KV v2 et un transit jetables, crée un AppRole, rejoue les
cas réels (enveloppe, cas, capacités, métadonnées, liste, suppression) puis
retire tout ce qu'il a créé.
"""

import os
import secrets
import sys
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import openbao_client as oc   # noqa: E402

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def bench():
    addr = os.environ.get("HOPS_BAO_BENCH_ADDR")
    ca = os.environ.get("HOPS_BAO_BENCH_CA")
    tok_file = os.environ.get("HOPS_BAO_BENCH_TOKEN_FILE")
    if not (addr and tok_file):
        pytest.skip("no OpenBao bench (HOPS_BAO_BENCH_*)")
    u = urllib.parse.urlparse(addr)
    if u.port == 8200 or u.hostname not in ("127.0.0.1", "localhost"):
        pytest.fail("refused: bench must be on the loopback, never port 8200")
    for k in list(os.environ):
        if k.startswith(("VAULT_", "BAO_")):
            os.environ.pop(k)
    admin = oc.Client(addr, ca_pem=Path(ca).read_text() if ca else None, actor="bench", action="live-290")
    admin.set_token(Path(tok_file).read_text().strip())
    tag = secrets.token_hex(4)
    mount, role, pol = f"hops-live-{tag}", f"hops-live-{tag}", f"hops-live-{tag}"
    admin._call("POST", f"sys/mounts/{mount}", {"type": "kv", "options": {"version": "2"}})
    admin._call("POST", f"sys/mounts/{mount}-v1", {"type": "kv", "options": {"version": "1"}})
    admin._call("POST", f"sys/mounts/{mount}-tr", {"type": "transit"})
    rules = (f'path "{mount}/data/consoles/{tag}/*" {{ capabilities = ["create","read","update","delete","list"] }}\n'
             f'path "{mount}/metadata/consoles/{tag}/*" {{ capabilities = ["create","read","update","patch","list","delete"] }}\n'
             f'path "{mount}/config" {{ capabilities = ["read"] }}\n'
             f'path "{mount}-tr/*" {{ capabilities = ["create","update"] }}\n'
             f'path "auth/approle/role/{role}/secret-id" {{ capabilities = ["update"] }}\n')
    admin._call("POST", f"sys/policies/acl/{pol}", {"policy": rules})
    try:
        admin._call("POST", "sys/auth/approle", {"type": "approle"})
    except oc.OpenBaoError:
        pass                                    # déjà actif
    admin._call("POST", f"auth/approle/role/{role}", {"token_policies": [pol], "token_period": "1h"})
    _, r = admin._call("GET", f"auth/approle/role/{role}/role-id")
    role_id = r["data"]["role_id"]
    yield {"admin": admin, "addr": addr, "ca": Path(ca).read_text() if ca else None, "mount": mount,
           "role": role, "role_id": role_id, "tag": tag}
    for p in (f"auth/approle/role/{role}", f"sys/policies/acl/{pol}", f"sys/mounts/{mount}",
              f"sys/mounts/{mount}-v1", f"sys/mounts/{mount}-tr"):
        try:
            admin._call("DELETE", p)
        except oc.OpenBaoError:
            pass


def _console(b):
    """Connexion AppRole par un secret_id emballé puis déballé (lookup)."""
    _, w = b["admin"]._request("POST", f"auth/approle/role/{b['role']}/secret-id", {},
                               extra={"X-Vault-Wrap-TTL": "5m"})
    wrap = w["wrap_info"]["token"]
    c = oc.Client(b["addr"], ca_pem=b["ca"], actor="alice", action="act-live")
    sid = c.unwrap(wrap, f"auth/approle/role/{b['role']}/secret-id")["secret_id"]
    with pytest.raises(oc.OpenBaoError) as e:
        c.unwrap(wrap, f"auth/approle/role/{b['role']}/secret-id")
    assert e.value.code == "openbao-unwrap-used"
    c.login_approle(b["role_id"], sid)
    return c


def test_live_round_trip(bench):
    b = bench
    c = _console(b)
    assert c.seal_status()["sealed"] is False
    assert c.kv_mount_version(b["mount"]) == 2
    base = f"consoles/{b['tag']}"
    v = c.kv_put(b["mount"], f"{base}/kubeconfig/h1", {"kubeconfig": "K1"}, cas=0,
                 custom_metadata={"hops_action": "act-live", "hops_actor": "alice", "hops_class": "kubeconfig"})
    assert v == 1 and v.metadata_error is None
    with pytest.raises(oc.OpenBaoError) as e:
        c.kv_put(b["mount"], f"{base}/kubeconfig/h1", {"kubeconfig": "K1"}, cas=0)
    assert e.value.code == "openbao-cas-conflict"
    assert c.kv_get(b["mount"], f"{base}/kubeconfig/h1")[0] == {"kubeconfig": "K1"}
    assert c.kv_metadata(b["mount"], f"{base}/kubeconfig/h1")["custom_metadata"]["hops_actor"] == "alice"
    assert c.kv_list(b["mount"], base) == ["kubeconfig/"]
    with pytest.raises(oc.OpenBaoError) as e:
        c.kv_get(b["mount"], f"consoles/other-{b['tag']}/kubeconfig/h1")
    assert e.value.code == "openbao-denied" and "deny" in e.value.detail
    c.kv_delete_all(b["mount"], f"{base}/kubeconfig/h1")
    assert c.kv_list(b["mount"], base) == []
    c.transit_create("hops-live-k", mount=f"{b['mount']}-tr")
    c.renew_self()
    c.revoke_self()


def test_live_kv1_refused(bench):
    c = oc.Client(bench["addr"], ca_pem=bench["ca"])
    c.set_token(bench["admin"]._tok.value)
    with pytest.raises(oc.OpenBaoError) as e:
        c.kv_get(f"{bench['mount']}-v1", "x")
    assert e.value.code == "openbao-not-kv2"
