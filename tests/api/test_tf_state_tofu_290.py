"""2.9.0, tâche 9 : un état chiffré ne se lit plus en JSON (web/tf_state.py).

* `addresses` refuse un état chiffré sans `lister` (jamais une liste vide
  qui ferait croire l'espace vide) et rend, par `tofu state list`, les
  mêmes adresses que sur l'état clair ;
* `move` passe par la lecture et l'écriture de chaque espace (`tofu state
  pull` / `tofu state push -` pour un espace chiffré) : mêmes résultats que
  sur l'état clair, aucun état clair écrit dans un espace chiffré ;
* l'écriture JSON refuse d'écraser un état chiffré (`state mv -state-out`
  le faisait, essai 8).
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import tf_state as st  # noqa: E402

ENC = {"encrypted_data": "Y2lwaGVy", "encryption_version": "v0", "meta": {"key_provider.openbao.hops": "x"}}


def res(t, n, module=None, mode="managed"):
    r = {"mode": mode, "type": t, "name": n, "instances": [{"attributes": {"name": "MARKER"}}]}
    if module:
        r["module"] = module
    return r


FIXTURE = {"version": 4, "serial": 7, "lineage": "L", "outputs": {}, "resources": [
    res("harvester_ssh_key", "a"), res("harvester_virtualmachine", "b"),
    res("harvester_image", "i", mode="data"), res("harvester_ssh_key", "m", module="module.x")]}
LISTING = "harvester_ssh_key.a\nharvester_virtualmachine.b\ndata.harvester_image.i\nmodule.x.harvester_ssh_key.m\n"


def put(ws, doc):
    ws.mkdir(parents=True, exist_ok=True)
    (ws / st.STATE_FILE).write_text(json.dumps(doc))


def test_plain_and_encrypted_give_the_same_addresses(tmp_path):
    plain, enc = tmp_path / "p", tmp_path / "e"
    put(plain, FIXTURE)
    put(enc, ENC)
    seen = []
    got = st.addresses(enc, lister=lambda w: seen.append(w) or LISTING)
    assert got == st.addresses(plain) == ["harvester_ssh_key.a", "harvester_virtualmachine.b"]
    assert seen == [enc]


def test_encrypted_state_without_lister_raises(tmp_path):
    put(tmp_path, ENC)
    with pytest.raises(st.StateEncrypted):
        st.addresses(tmp_path)
    assert st.state_encrypted(tmp_path)


def test_a_plain_state_never_calls_the_lister(tmp_path):
    put(tmp_path, FIXTURE)
    assert st.addresses(tmp_path, lister=lambda w: pytest.fail("tofu called for a plain state"))
    assert not st.state_encrypted(tmp_path) and not st.state_encrypted(tmp_path / "none")


class FakeTofuStates:
    """`state pull` / `state push -` d'espaces chiffrés : contenu en mémoire,
    fichier sur disque toujours chiffré."""

    def __init__(self):
        self.mem, self.calls = {}, []

    def read(self, ws):
        self.calls.append(("pull", Path(ws).name))
        return json.loads(json.dumps(self.mem[str(ws)])) if str(ws) in self.mem else None

    def write(self, ws, data):
        self.calls.append(("push", Path(ws).name))
        self.mem[str(ws)] = data
        put(Path(ws), ENC)


@pytest.mark.parametrize("src_enc,dst_enc", [(False, True), (True, True), (True, False)])
def test_move_through_tofu_matches_the_plain_move(tmp_path, src_enc, dst_enc):
    wanted = {"harvester_ssh_key.a"}
    # référence : les deux espaces en clair
    ps, pd = tmp_path / "ps", tmp_path / "pd"
    put(ps, FIXTURE)
    ref = st.move(ps, pd, wanted)
    ref_src, ref_dst = json.loads((ps / st.STATE_FILE).read_text()), json.loads((pd / st.STATE_FILE).read_text())
    # mêmes espaces, chiffrés là où demandé
    s, d = tmp_path / "s", tmp_path / "d"
    tofu = FakeTofuStates()
    if src_enc:
        put(s, ENC)
        tofu.mem[str(s)] = json.loads(json.dumps(FIXTURE))
    else:
        put(s, FIXTURE)
    enc = {str(s): src_enc, str(d): dst_enc}

    def read(w):
        return tofu.read(w) if enc[str(w)] else st.read_file_state(w)

    def write(w, data):
        return tofu.write(w, data) if enc[str(w)] else st.write_file_state(w, data)
    got = st.move(s, d, wanted, read=read, write=write)
    assert got == ref == ["harvester_ssh_key.a"]
    new_src = tofu.mem[str(s)] if src_enc else json.loads((s / st.STATE_FILE).read_text())
    new_dst = tofu.mem[str(d)] if dst_enc else json.loads((d / st.STATE_FILE).read_text())
    assert new_src["resources"] == ref_src["resources"] and new_src["serial"] == ref_src["serial"]
    assert new_dst["resources"] == ref_dst["resources"]
    for ws, e in ((s, src_enc), (d, dst_enc)):
        on_disk = (ws / st.STATE_FILE).read_text()
        assert ("MARKER" in on_disk) is (not e), ws
    # copies d'avant le déplacement : les fichiers tels quels, un état chiffré reste chiffré
    for b in s.glob("terraform.tfstate.pre-move-*"):
        assert ("MARKER" in b.read_text()) is (not src_enc)


def test_default_io_refuses_an_encrypted_source(tmp_path):
    s, d = tmp_path / "s", tmp_path / "d"
    put(s, ENC)
    with pytest.raises(st.StateEncrypted):
        st.move(s, d, {"harvester_ssh_key.a"})
    assert not (d / st.STATE_FILE).exists()


def test_default_io_never_writes_a_plain_state_over_an_encrypted_one(tmp_path):
    s, d = tmp_path / "s", tmp_path / "d"
    put(s, FIXTURE)
    put(d, ENC)
    with pytest.raises(st.StateEncrypted):
        st.move(s, d, {"harvester_ssh_key.a"})
    assert json.loads((d / st.STATE_FILE).read_text()) == ENC
    with pytest.raises(st.StateEncrypted):
        st.write_file_state(d, FIXTURE)
    assert json.loads((d / st.STATE_FILE).read_text()) == ENC


def test_the_console_never_reads_an_encrypted_state_by_itself():
    """Test de source : dans web/app.py, seules `_tf_addresses` (et l'état
    vu par `tf_state` qui refuse le chiffré) lisent les adresses ; aucun
    `_tfst.addresses(` direct ailleurs, aucun `-state-out`."""
    import ast
    src = (ROOT / "web" / "app.py").read_text()
    tree = ast.parse(src)
    owners = []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for n in ast.walk(fn):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "addresses" \
                        and isinstance(n.func.value, ast.Name) and n.func.value.id == "_tfst":
                    owners.append(fn.name)
    assert set(owners) == {"_tf_addresses"}, owners
    assert '"-state-out' not in src.replace('_TF_REFUSED_ARGS = ("-state-out",)', "")
