"""Prochaine version KVM : contrôles de source des fenêtres de création et
de modification (vm-form.js, cloudinit-form.js, kvm-form.js) et tests
unitaires de leurs fonctions pures (node).

- Aucune valeur venue du serveur (nom de VM, d'hôte, de pool, de volume, de
  réseau, cible, MAC...) n'est interpolée brute dans un gabarit HTML : un
  analyseur de gabarits JS relève chaque ${...} et refuse un chemin de donnée
  hors esc() dans un gabarit qui contient du HTML.
- Pas de confirm(), prompt(), alert() ni de new EventSource( dans les
  nouveaux fichiers ; actions suivies par KVM.follow (SSEReconnect).
- Le user-data ne va jamais dans un attribut : les zones de texte n'ont pas
  d'attribut value, elles sont remplies par .value.
- La création Harvester passe par la coquille commune (décision 65 = A).
- withKeysAndAgent (clés SSH et agent invité ajoutés au user-data) et
  parseSize / diskItem / hotness de l'adaptateur KVM, joués dans node.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
JS = ROOT / "web" / "static" / "js"
NEW = ["vm-form.js", "cloudinit-form.js", "kvm-form.js", "kvm-storage.js"]
SRC = {f: (JS / f).read_text() for f in NEW + ["kvm.js", "vm-create.js", "vm-edit.js"]}

sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import re
def template_interpolations(src):
    """[(expression, literal_text_of_its_template)] pour chaque ${...} d'un
    gabarit JS ; lit chaînes, commentaires et gabarits imbriqués."""
    out = []
    i, n = 0, len(src)
    stack = []          # contextes : ('code', depth) | ('tpl', parts, pending)
    def code_until(i, closing_brace):
        pass
    # analyse récursive
    def parse_code(i, stop_on_brace):
        depth = 0
        while i < n:
            c = src[i]
            if src.startswith('//', i):
                j = src.find('\n', i); i = n if j < 0 else j; continue
            if src.startswith('/*', i):
                j = src.find('*/', i + 2); i = n if j < 0 else j + 2; continue
            if c in '"\'':
                j = i + 1
                while j < n and src[j] != c:
                    j += 2 if src[j] == '\\' else 1
                i = j + 1; continue
            if c == '`':
                i = parse_tpl(i + 1); continue
            if c == '/' and re.search(r'[(,=:!&|?{};]\s*$', src[max(0, i - 40):i] or '('):
                # littéral d'expression régulière
                j = i + 1; cls = False
                while j < n and (src[j] != '/' or cls):
                    if src[j] == '\\': j += 2; continue
                    if src[j] == '[': cls = True
                    elif src[j] == ']': cls = False
                    j += 1
                i = j + 1; continue
            if c == '{': depth += 1
            elif c == '}':
                if depth == 0 and stop_on_brace: return i + 1
                depth -= 1
            i += 1
        return i
    def parse_tpl(i):
        parts, exprs = [], []
        start = i
        while i < n:
            c = src[i]
            if c == '\\': i += 2; continue
            if c == '`':
                parts.append(src[start:i])
                text = ''.join(parts)
                for e in exprs: out.append((e, text))
                return i + 1
            if src.startswith('${', i):
                parts.append(src[start:i])
                j = parse_code(i + 2, True)
                exprs.append(src[i + 2:j - 1])
                i = j; start = i; continue
            i += 1
        return i
    parse_code(0, False)
    return out
DATA = re.compile(r"^(?!tr\b|esc\b)[a-z]\w*(\.\w+)+$|^(u|r|w|name|host|domain|target|mac|k|s|v|d|x)$")
def raw_data_in_html(src):
    bad = []
    for e, text in template_interpolations(src):
        e2 = " ".join(e.split())
        if DATA.match(e2) and '<' in text:
            bad.append(e2)
    return bad


@pytest.mark.parametrize("name", NEW + ["kvm.js"])
def test_no_server_value_is_interpolated_raw_into_html(name):
    found = template_interpolations(SRC[name])
    assert len(found) > 40, "l'analyseur ne voit plus les gabarits"
    assert raw_data_in_html(SRC[name]) == []


# --- règle générale : TOUTE interpolation d'un gabarit HTML ---------------------------
# Portée exacte : chaque ${...} d'un gabarit littéral qui contient « < » dans
# vm-form.js, cloudinit-form.js, kvm-form.js, kvm-storage.js et kvm.js. Elle est sûre si elle
# est : un littéral (chaîne, nombre) ; un appel unique d'un constructeur de
# HTML qui échappe ses entrées (BUILDERS : esc, icon, tr dans un attribut de
# classe...) ; une liste `.map(<constructeur>).join('')` ; un gabarit imbriqué
# (analysé lui-même) ; un ternaire dont chaque branche est sûre. Le reste
# doit figurer dans HTML_VARS (variable locale qui porte du HTML assemblé dans
# la même fonction à partir de morceaux échappés, ou constante du code), avec
# un contrôle que chaque entrée sert encore. Hors portée : les chaînes
# assemblées par « + » hors gabarit et les .innerHTML affectés d'une variable
# (couverts par la relecture et les e2e à noms hostiles).
BUILDERS = ("esc", "icon", "hostSel", "Icons.svg", "tip", "badge", "dim", "row", "f", "item", "hfield", "opt",
            "fieldHtml", "appliedBadge", "unmanagedHtml", "headHtml", "actionsHtml", "field", "li",
            "th", "checksHtml", "stateBadge", "errorBadge", "yes", "vmRow", "hostTile", "opItem",
            "existingDisksHtml", "existingNicsHtml", "renderSection",
            "VMForm.shellHtml", "VMForm.appliedBadge", "VMForm.changesHtml", "VMForm.restartBanner",
            "VMForm.reviewHtml", "TFForm.render", "CloudInitForm.renderWizard",
            "KVMForm.menuGroupHtml", "errHtml", "okHtml", "listHtml", "progressHtml", "sendFieldsHtml",
            "poolRow", "volRow", "imageRow", "netRow", "hostName")
HTML_VARS = {
    "vm-form.js": {"a[0]", "actions", "buttonHtml", "head", "html"},
    "cloudinit-form.js": set(),
    "kvm-storage.js": {"cls", "unread"},
    "kvm-form.js": {"attrs", "cls", "control", "dis", "existing", "extra", "hot", "list", "media", "ro",
                    "tip", "whyHtml"},
    "kvm.js": {"acts", "bad", "body", "byState", "cls", "data", "declare", "filters", "flags", "g", "head",
               "head(h.name)", "inner", "labels", "links", "op", "open", "partly", "remove", "sha", "state",
               "test", "value"},
}
_LIT = re.compile(r"""^('([^'\\]|\\.)*'|"([^"\\]|\\.)*"|\d+(\.\d+)?)$""")


def _top_split(e, sep):
    """Index du premier `sep` hors parenthèses, crochets et chaînes, ou -1."""
    depth, q, i = 0, None, 0
    while i < len(e):
        c = e[i]
        if q:
            if c == "\\":
                i += 2
                continue
            if c == q:
                q = None
        elif c in "'\"`":
            q = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and c == sep:
            return i
        i += 1
    return -1


def _single_call(e, names):
    for n in sorted(names, key=len, reverse=True):
        if e.startswith(n + "(") and _top_split(e[len(n):], ",") != 0:
            rest = e[len(n):]
            depth, q, i = 0, None, 0
            while i < len(rest):
                c = rest[i]
                if q:
                    if c == "\\":
                        i += 2
                        continue
                    if c == q:
                        q = None
                elif c in "'\"`":
                    q = c
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        return i == len(rest) - 1
                i += 1
    return False


def interpolation_is_safe(e):
    e = " ".join(e.split())
    while e.startswith("(") and _single_call("x" + e, {"x"}):
        e = e[1:-1].strip()
    if not e or _LIT.match(e) or "`" in e:
        return bool(e)
    q = _top_split(e, "?")
    if q > 0:
        rest = e[q + 1:]
        c = _top_split(rest, ":")
        return c > 0 and interpolation_is_safe(rest[:c]) and interpolation_is_safe(rest[c + 1:])
    if _single_call(e, BUILDERS):
        return True
    m = re.match(r"^.+\.map\((.+)\)\.join\(''\)$", e)
    if m:
        fn = m.group(1).strip()
        if fn in BUILDERS:
            return True
        m2 = re.match(r"^\(?\w+(, ?\w+)?\)? => (.+)$", fn)
        return bool(m2) and interpolation_is_safe(m2.group(2))
    return False


def unsafe_interpolations(src):
    return sorted({" ".join(e.split()) for e, text in template_interpolations(src)
                   if "<" in text and not interpolation_is_safe(e)})


@pytest.mark.parametrize("name", NEW + ["kvm.js"])
def test_every_interpolation_in_html_is_escaped_or_known(name):
    found = unsafe_interpolations(SRC[name])
    assert set(found) - HTML_VARS[name] == set(), set(found) - HTML_VARS[name]
    # une entrée qui ne sert plus est retirée (la liste ne grossit pas en silence)
    assert HTML_VARS[name] - set(found) == set()


@pytest.mark.parametrize("snippet,flagged", [
    ("`<b>${v.name}</b>`", ["v.name"]),
    ("`<b>${name}</b>`", ["name"]),
    ("`<b>${x ? v.name : ''}</b>`", ["x ? v.name : ''"]),
    ("`<b>${a + b}</b>`", ["a + b"]),
    ("`<b>${list.join(', ')}</b>`", ["list.join(', ')"]),
    ("`<b>${esc(a) + b}</b>`", ["esc(a) + b"]),
    ("`<b>${rows.map((r) => r.name).join('')}</b>`", ["rows.map((r) => r.name).join('')"]),
    ("`<b>${esc(v.name)}</b>`", []),
    ("`<b>${x ? 'a' : 'b'}</b>`", []),
    ("`<b>${icon('ok', 12)}</b>`", []),
    ("`<b>${x ? f('a') : ''}</b>`", []),
    ("`<b>${names.map(f).join('')}</b>`", []),
    ("`<b>${rows.map((r) => vmRow(r)).join('')}</b>`", []),
    ("`<b>${x ? `<i>${esc(y)}</i>` : ''}</b>`", []),
    ("`kvm-${v.host}`", []),
])
def test_the_general_scanner_table(snippet, flagged):
    assert unsafe_interpolations("const a = " + snippet + ";") == flagged


def test_the_template_scanner_catches_an_unescaped_name():
    src = "const a = `<b>${esc(v.name)}</b>`; const id = `kvm-${v.host}`; const t = `<td>${d.target}</td>`;"
    assert raw_data_in_html(src) == ["d.target"]


@pytest.mark.parametrize("name", NEW)
def test_no_browser_dialog_and_no_raw_event_source(name):
    s = SRC[name]
    assert not re.search(r"\b(confirm|prompt|alert)\(", s), name
    assert "new EventSource(" not in s


def test_kvm_actions_are_followed_through_kvm_follow():
    s = SRC["kvm-form.js"]
    assert "const follow = (id, onStep, onEnd) => KVM.follow(id, onStep, onEnd);" in s
    assert s.count("follow(out.action_id") >= 4


def test_user_data_never_lands_in_an_attribute():
    s = SRC["kvm-form.js"]
    for m in re.finditer(r'<textarea[^>]*data-ci="(userData|networkData)"[^>]*>', s):
        assert "value=" not in m.group(0)
    assert ".value = st.loadedCi.user_data" in s
    assert "ud.value = st.loaded;" in s and "st.loaded = d.user_data || '';" in s
    # le mot de passe VNC est une case : jamais une valeur dans la page
    assert "'display.password': { s: 'firmware', type: 'check'" in s


def test_harvester_creation_goes_through_the_shared_shell():
    c = SRC["vm-create.js"]
    assert "VMForm.shellHtml({ sections, head:" in c and "VMForm.mount(root, adapter)" in c
    assert "claimsAreToCreate: true" in c and "applyTemplate" in c
    html = (ROOT / "web" / "templates" / "index.html").read_text()
    order = ["cloudinit-form.js", "vm-edit.js", "vm-form.js", "vm-create.js", "kvm.js", "kvm-vm.js", "kvm-form.js"]
    pos = [html.index(f"/static/js/{f}") for f in order]
    assert pos == sorted(pos)


def test_vm_edit_reexports_the_cloud_init_assistant():
    e = SRC["vm-edit.js"]
    assert "const { CI_USER_SCHEMA, CI_NET_SCHEMA, genUserData, genNetworkData } = window.CloudInitForm;" in e
    assert "CloudInitForm.renderWizard(cluster)" in e and "CloudInitForm.wireWizard(sectionEl, cluster, {" in e
    # Harvester garde sa confirmation du navigateur (comportement inchangé)
    assert "confirmReplace: () => confirm(tr('vm.edit.ci.confirmReplace'" in e


def _node(files, script):
    prelude = "globalThis.window = globalThis; globalThis.Icons = { svg: () => '' };\n" \
        "globalThis.CSS = { escape: (s) => s };\n"
    code = prelude + "".join((JS / f).read_text() + "\n" for f in files) + script
    r = subprocess.run(["node", "-"], input=code, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOk ju@node1"


START = "systemctl enable --now qemu-guest-agent"
KEY2 = "ssh-rsa AAAAB3NzaC1yc2E it's-me"
QGA = "qemu-guest-agent"
WF = "#cloud-config\nwrite_files:\n  - path: /etc/x.yaml\n    content: |\n      packages:\n        - nano\n      runcmd: [ls]\n"

# (libellé, user-data, clés, agent, document attendu (dict) ou raison du refus, liste entre crochets modifiée)
MERGE_CASES = [
    ("empty", "", [KEY], False, {"ssh_authorized_keys": [KEY]}, False),
    ("nothing to add", "", [], False, None, False),
    ("hostname + agent", "#cloud-config\nhostname: a\n", [KEY], True,
     {"hostname": "a", "ssh_authorized_keys": [KEY], "packages": [QGA], "runcmd": [START]}, False),
    ("existing keys", "#cloud-config\nssh_authorized_keys:\n  - ssh-rsa AAAA old\nusers: []\n", [KEY], False,
     {"ssh_authorized_keys": ["ssh-rsa AAAA old", KEY], "users": []}, False),
    ("agent already there", "#cloud-config\npackages:\n  - qemu-guest-agent\n", [], True,
     {"packages": [QGA], "runcmd": [START]}, False),
    ("shell script", "#!/bin/sh\necho hi\n", [KEY], True, "not-cloud-config", False),
    ("flow lists", "#cloud-config\npackages: [vim, curl]\nruncmd: [\"echo hi\"]\n", [KEY], True,
     {"packages": ["vim", "curl", QGA], "runcmd": ["echo hi", START], "ssh_authorized_keys": [KEY]}, True),
    ("empty flow list", "#cloud-config\npackages: []  # none\n", [], True,
     {"packages": [QGA], "runcmd": [START]}, True),
    ("zero indent", "#cloud-config\npackages:\n- vim\nruncmd:\n- echo hi\nhostname: z\n", [], True,
     {"packages": ["vim", QGA], "runcmd": ["echo hi", START], "hostname": "z"}, False),
    ("four spaces", "#cloud-config\npackages:\n    - vim\n", [], True, {"packages": ["vim", QGA], "runcmd": [START]}, False),
    ("nested keys ignored", f"#cloud-config\nusers:\n  - name: a\n    ssh_authorized_keys:\n      - {KEY}\n", [KEY], False,
     {"users": [{"name": "a", "ssh_authorized_keys": [KEY]}], "ssh_authorized_keys": [KEY]}, False),
    ("write_files content", WF, [], True,
     {"write_files": [{"path": "/etc/x.yaml", "content": "packages:\n  - nano\nruncmd: [ls]\n"}],
      "packages": [QGA], "runcmd": [START]}, False),
    ("comments", "#cloud-config\npackages: # list\n  # first\n  - vim\n# about runcmd\nruncmd:\n  - ls\n", [], True,
     {"packages": ["vim", QGA], "runcmd": ["ls", START]}, False),
    ("comment inside zero indent", "#cloud-config\npackages:\n- vim\n# c\n- curl\nruncmd: []\n", [], True,
     {"packages": ["vim", "curl", QGA], "runcmd": [START]}, True),
    ("crlf", "#cloud-config\r\npackages:\r\n- vim\r\n", [KEY2], True,
     {"packages": ["vim", QGA], "runcmd": [START], "ssh_authorized_keys": [KEY2]}, False),
    ("no final newline", "#cloud-config\nhostname: n", [KEY], False, {"hostname": "n", "ssh_authorized_keys": [KEY]}, False),
    ("document start", "#cloud-config\n---\nhostname: d\n", [], True, {"hostname": "d", "packages": [QGA], "runcmd": [START]}, False),
    ("quoted key", "#cloud-config\n\"packages\":\n  - vim\n", [], True, {"packages": ["vim", QGA], "runcmd": [START]}, False),
    ("key present in flow", f"#cloud-config\nssh_authorized_keys: ['{KEY}']\n", [KEY], False, {"ssh_authorized_keys": [KEY]}, True),
    ("agent started as a list", "#cloud-config\nruncmd:\n  - [systemctl, enable, --now, qemu-guest-agent]\n", [], True,
     {"runcmd": [["systemctl", "enable", "--now", QGA]], "packages": [QGA]}, False),
    ("multi-line flow", "#cloud-config\npackages: [vim,\n  curl]\n", [], True, "flow", False),
    ("nested flow", "#cloud-config\npackages: [vim, [curl, 1]]\n", [], True, "flow", False),
    ("flow pair", "#cloud-config\npackages: [a: b]\n", [], True, "flow", False),
    ("scalar", "#cloud-config\npackages: vim\n", [], True, "not-list", False),
    ("mapping", "#cloud-config\npackages:\n  vim: 1\n", [], True, "not-list", False),
    ("anchor", "#cloud-config\nssh_authorized_keys: &k\n  - a\n", [KEY], False, "not-list", False),
    ("duplicate key", "#cloud-config\npackages:\n  - a\npackages:\n  - b\n", [], True, "duplicate", False),
    ("two documents", "#cloud-config\nhostname: a\n---\nhostname: b\n", [KEY], False, "documents", False),
    ("document end", "#cloud-config\nhostname: a\n...\n", [KEY], False, "documents", False),
]


@pytest.mark.parametrize("label,user_data,keys,agent,expect,flow", MERGE_CASES, ids=[c[0] for c in MERGE_CASES])
def test_keys_and_agent_are_merged_into_a_cloud_config(label, user_data, keys, agent, expect, flow):
    """Chaque résultat est relu par un vrai analyseur YAML (PyYAML, outil de
    test seulement) ; un refus rend le texte INCHANGÉ ; un ajout garde chaque
    ligne d'origine, dans l'ordre (sauf la ligne entre crochets complétée)."""
    import yaml
    out = _node(["cloudinit-form.js"], f"""
      const r = CloudInitForm.withKeysAndAgent({json.dumps(user_data)}, {json.dumps(keys)}, {json.dumps(agent)});
      console.log(JSON.stringify(r));""")
    if isinstance(expect, str):
        assert out["skipped"] is True and out["reason"] == expect and out["text"] == user_data
        return
    assert out["skipped"] is False and out["reason"] is None
    if expect is None:
        assert out["text"] == user_data
        return
    text = out["text"]
    assert text.startswith("#cloud-config")
    assert yaml.safe_load(text) == expect
    got = text.splitlines(keepends=True)
    orig = (user_data or "#cloud-config\n").splitlines(keepends=True)
    if not orig[-1].endswith("\n"):
        orig[-1] += "\r\n" if "\r\n" in user_data else "\n"
    it = iter(got)
    missing = [l for l in orig if not any(g == l for g in it)]
    if flow:
        assert all("[" in l for l in missing), missing
    else:
        assert missing == [], missing
    if "\r\n" in user_data:
        assert re.search(r"(?<!\r)\n", text) is None


def test_a_key_already_present_is_not_repeated():
    out = _node(["cloudinit-form.js"], f"""
      const once = CloudInitForm.withKeysAndAgent('', [{json.dumps(KEY)}], true).text;
      const twice = CloudInitForm.withKeysAndAgent(once, [{json.dumps(KEY)}], true).text;
      console.log(JSON.stringify({{ once, twice }}));""")
    assert out["once"] == out["twice"]


KVM_FILES = ["vm-form.js", "cloudinit-form.js", "kvm-form.js"]
TR = "globalThis.i18n = { t: (k, p) => k + (p ? JSON.stringify(p) : '') };\n"


@pytest.mark.parametrize("text,expect", [
    ("10G", 10 * 2 ** 30), ("10", 10 * 2 ** 30), ("512M", 512 * 2 ** 20), ("512 MiB", 512 * 2 ** 20),
    ("1.5T", int(1.5 * 2 ** 40)), ("64K", 65536), ("ten", None), ("", None), ("-1G", None), ("1G2", None),
])
def test_parse_size(text, expect):
    out = _node(KVM_FILES, TR + f"console.log(JSON.stringify(KVMForm._internals.parseSize({json.dumps(text)})));")
    assert out == expect


@pytest.mark.parametrize("card,spec,errors", [
    ({"source": "blank", "pool": "images", "size": "2G", "bus": "virtio"},
     {"device": "disk", "bus": "virtio", "source": {"kind": "blank", "pool": "images", "size_bytes": 2 * 2 ** 30, "format": "qcow2"}}, []),
    ({"source": "image", "image": "images/cirros.qcow2", "linked": True},
     {"device": "disk", "source": {"kind": "image", "pool": "images", "image": "cirros.qcow2", "image_pool": "images", "linked": True}}, []),
    ({"source": "image", "image": "imgs/a/b.qcow2", "pool": "data", "linked": False, "size": "20G"},
     {"device": "disk", "source": {"kind": "image", "pool": "data", "image": "a/b.qcow2", "image_pool": "imgs", "linked": False,
                                   "size_bytes": 20 * 2 ** 30}}, []),
    ({"source": "iso", "iso": "images/netinst.iso", "boot_order": 1},
     {"device": "cdrom", "boot_order": 1, "source": {"kind": "iso", "pool": "images", "volume": "netinst.iso"}}, []),
    ({"source": "empty", "device": "disk"}, {"device": "cdrom", "source": {"kind": "empty"}}, []),
    ({"source": "volume", "volume": "data/old.qcow2", "shareable": True, "readonly": True},
     {"device": "disk", "readonly": True, "shareable": True, "source": {"kind": "volume", "pool": "data", "volume": "old.qcow2"}}, []),
    ({"source": "blank"}, None, ["kvmf.e.pool", "kvmf.e.blankSize"]),
    ({"source": "blank", "pool": "p", "size": "lots"}, None, ["kvmf.e.size", "kvmf.e.blankSize"]),
    ({"source": "image"}, None, ["kvmf.e.image"]),
    ({"source": "iso"}, None, ["kvmf.e.iso"]),
    ({"source": "volume"}, None, ["kvmf.e.volume"]),
])
def test_disk_card_to_spec(card, spec, errors):
    out = _node(KVM_FILES, TR + f"""
      const errs = []; const d = KVMForm._internals.diskItem({json.dumps(card)}, errs, 0);
      console.log(JSON.stringify({{ d, errs: errs.map((e) => e.message.split('{{')[0]) }}));""")
    assert out["errs"] == errors
    if spec is not None:
        assert out["d"] == spec


@pytest.mark.parametrize("card,spec,errors", [
    ({"kind": "network", "network": "default"}, {"kind": "network", "source": "default", "model": "virtio"}, []),
    ({"kind": "bridge", "bridge": "br0", "model": "e1000e", "boot_order": 2},
     {"kind": "bridge", "source": "br0", "model": "e1000e", "boot_order": 2}, []),
    ({"kind": "direct", "dev": "eno1", "mode": "vepa", "mac": "52:54:00:AA:BB:CC"},
     {"kind": "direct", "source": "eno1", "model": "virtio", "mode": "vepa", "mac": "52:54:00:aa:bb:cc"}, []),
    ({"kind": "network"}, None, ["kvmf.e.nicSource"]),
    ({"kind": "network", "network": "x", "mac": "52:54:00"}, None, ["kvmf.e.mac"]),
])
def test_nic_card_to_spec(card, spec, errors):
    out = _node(KVM_FILES, TR + f"""
      const errs = []; const n = KVMForm._internals.nicItem({json.dumps(card)}, errs, 0);
      console.log(JSON.stringify({{ n, errs: errs.map((e) => e.message.split('{{')[0]) }}));""")
    assert out["errs"] == errors
    if spec is not None:
        assert out["n"] == spec


def _hotness(field, form):
    return _node(KVM_FILES, TR + f"console.log(JSON.stringify(KVMForm._internals.hotness({json.dumps(field)}, {json.dumps(form)})));")


def test_hotness_follows_the_served_table_and_the_state():
    import kvm_edit as ke
    table = list(ke.HOTNESS)
    running = {"state": "running", "persistent": True, "hotness": table, "snapshots": 0}
    for row in table:
        got = _hotness(row["field"], running)
        if row["field"] == "name":
            assert got["applied"] == "refused"
        else:
            assert got["applied"] == row["running"], row
    off = dict(running, state="shut off")
    assert _hotness("cpu.max_vcpus", off)["applied"] == "now"
    assert _hotness("name", off)["applied"] == "now"
    assert _hotness("name", dict(off, snapshots=2))["applied"] == "refused"
    assert _hotness("cpu.vcpus", dict(running, state="paused"))["applied"] == "refused"
    assert _hotness("cpu.vcpus", dict(running, persistent=False))["applied"] == "refused"
    assert _hotness("watchdog", running)["applied"] == "refused"


def test_vm_list_serves_the_edit_operations_table():
    import app as wapp
    import kvm_edit as ke
    assert wapp.KVM_VM_EDIT_OPERATIONS == {
        "edit": {"role": "operator", "states": ["running", "idle", "blocked", "shut off"], "persistent": False},
        "hot": {"role": "operator", "states": ["running", "idle", "blocked", "shut off"], "persistent": False},
    }
    assert wapp.KVM_VM_EDIT_OPERATIONS["hot"]["states"][:3] == list(ke.LIVE_STATES)
    src = (ROOT / "web" / "app.py").read_text()
    assert '"edit_operations": KVM_VM_EDIT_OPERATIONS})' in src


def test_harvester_tabs_carry_a_tooltip_in_the_shared_shell_and_the_edit_window():
    out = _node(["cloudinit-form.js", "vm-edit.js", "vm-form.js"], TR + """
      const html = VMForm.shellHtml({ sections: VMEdit.SECTIONS });
      const tips = [...html.matchAll(/data-section="([a-z]+)" class="tip" data-tip="([^"]+)"/g)].map(m => [m[1], m[2]]);
      console.log(JSON.stringify(tips));""")
    assert [t[0] for t in out] == ["general", "compute", "firmware", "disks", "network", "cloudinit",
                                   "placement", "lifecycle"]
    assert all(len(t[1]) > 10 for t in out)        # texte de repli ; les 5 langues : test_i18n
    for sid in [t[0] for t in out]:
        assert f"tr('vm.edit.tip.{sid}'" in SRC["vm-edit.js"]
    assert '<button data-section="${s.id}" class="tip" data-tip="${esc(s.tip())}">' in SRC["vm-edit.js"]


def test_no_em_dash_or_unicode_arrow_in_the_new_form_files():
    for name in NEW:
        assert "\u2014" not in SRC[name] and "\u2192" not in SRC[name], name
