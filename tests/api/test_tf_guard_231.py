"""2.3.1 : le code d'une déclaration Terraform ne commande pas la machine de
la console.

Revue interne : le code libre (`raw`) d'une déclaration se lançait au rôle
opérateur, et OpenTofu exécute sur la machine qui le lance (provisioner
local-exec, data external, module, backend, file()...). Défense en
profondeur, essayée ici couche par couche :
  1. le code libre est réservé à l'admin (créer, modifier, planifier,
     appliquer, détruire) ;
  2. tf_guard lit tout code (formulaire ou libre) avant chaque plan ou apply,
     puis l'espace entier juste avant chaque commande (_tf_run_cmd) ;
  3. OpenTofu part d'un environnement minimal, providers du seul miroir ;
  4. les déclarations enregistrées qui enfreignent les règles sont signalées
     au démarrage (jamais supprimées).
Chaque couche a son test de mutation : la couche retirée, le test le voit.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402
import tf_guard as g  # noqa: E402
import tf_kvm as k  # noqa: E402
import tf_store as ts  # noqa: E402

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJdD ops@site"
LOCAL_EXEC = ('resource "harvester_ssh_key" "k" {\n'
              '  name       = "k"\n'
              '  public_key = "' + KEY + '"\n'
              '  provisioner "local-exec" {\n'
              '    command = "touch /tmp/hops-pwned-231"\n'
              '  }\n'
              '}\n')
SAFE_RAW = ('resource "harvester_ssh_key" "ops" {\n'
            '  name       = "ops"\n'
            '  namespace  = "default"\n'
            '  public_key = "' + KEY + '"\n'
            '}\n')


# ---------------------------------------------------------------------------
# 2. La lecture du code : ce qui est refusé, avec sa ligne
# ---------------------------------------------------------------------------
REFUSED = [
    # (nom, code, construction attendue dans le message, ligne)
    ("local-exec", LOCAL_EXEC, 'provisioner "local-exec"', 4),
    ("remote-exec", 'resource "harvester_volume" "v" {\n provisioner "remote-exec" {\n inline = ["id"]\n }\n}\n',
     'provisioner "remote-exec"', 2),
    ("file provisioner", 'resource "harvester_volume" "v" {\n provisioner "file" {\n source = "/etc/shadow"\n'
     ' destination = "/tmp/x"\n }\n}\n', 'provisioner "file"', 2),
    ("connection", 'resource "harvester_volume" "v" {\n connection {\n host = "x"\n }\n}\n', "connection", 2),
    ("when destroy", 'resource "harvester_volume" "v" {\n provisioner "local-exec" {\n when = destroy\n'
     ' command = "id"\n }\n}\n', 'provisioner "local-exec"', 2),
    ("nested deep", 'resource "harvester_virtualmachine" "v" {\n disk {\n x {\n provisioner "local-exec" {\n'
     ' command = "id"\n }\n }\n }\n}\n', 'provisioner "local-exec"', 4),
    ("dynamic provisioner", 'resource "harvester_virtualmachine" "v" {\n dynamic "provisioner" {\n'
     ' for_each = []\n content {}\n }\n}\n', 'dynamic "provisioner"', 2),
    ("null_resource", 'resource "null_resource" "x" {}\n', 'resource "null_resource"', 1),
    ("terraform_data", 'resource "terraform_data" "x" {}\n', 'resource "terraform_data"', 1),
    ("data external", 'data "external" "x" {\n program = ["sh"]\n}\n', 'data "external"', 1),
    ("remote state", 'data "terraform_remote_state" "s" {\n backend = "local"\n}\n',
     'data "terraform_remote_state"', 1),
    ("local_file", 'resource "local_file" "f" {\n filename = "/home/x/.bashrc"\n content = "x"\n}\n',
     'resource "local_file"', 1),
    ("bootstrap", 'resource "harvester_bootstrap" "b" {\n password = "x"\n}\n', 'resource "harvester_bootstrap"', 1),
    ("image upload", 'resource "harvester_image" "i" {\n name = "i"\n file_path = "/etc/shadow"\n}\n',
     "harvester_image.file_path", 3),
    ("module", 'module "m" {\n source = "git::https://example.invalid/m.git"\n}\n', "module block", 1),
    ("backend", 'terraform {\n backend "local" {\n path = "/tmp/s"\n }\n}\n', "terraform block", 1),
    ("cloud", 'terraform {\n cloud {}\n}\n', "terraform block", 1),
    ("provider", 'provider "harvester" {\n kubeconfig = "/etc/rancher/rke2/rke2.yaml"\n}\n', "provider block", 1),
    ("foreign provider", 'provider "aws" {}\n', "provider block", 1),
    ("check block", 'check "c" {\n data "external" "x" {}\n}\n', "check block", 1),
    ("file()", 'resource "harvester_ssh_key" "k" {\n name = "k"\n public_key = file("/etc/shadow")\n}\n',
     "function file()", 3),
    ("relative file()", 'locals {\n k = file("kubeconfig")\n}\n', "function file()", 2),
    ("filebase64", 'locals {\n k = filebase64("kvm_ssh_key")\n}\n', "function filebase64()", 2),
    ("fileexists", 'locals {\n k = fileexists("/root")\n}\n', "function fileexists()", 2),
    ("fileset", 'locals {\n k = fileset("/", "*")\n}\n', "function fileset()", 2),
    ("filesha256", 'locals {\n k = filesha256("/etc/shadow")\n}\n', "function filesha256()", 2),
    ("templatefile", 'locals {\n k = templatefile("/etc/shadow", {})\n}\n', "function templatefile()", 2),
    ("templatestring", 'locals {\n k = templatestring(local.t, {})\n}\n', "function templatestring()", 2),
    ("pathexpand", 'locals {\n k = pathexpand("~/.ssh/id_ed25519")\n}\n', "function pathexpand()", 2),
    ("core::file", 'locals {\n k = core::file("/etc/shadow")\n}\n', "function core::file()", 2),
    ("provider function", 'locals {\n k = provider::harvester::x(1)\n}\n',
     "function provider::harvester::x()", 2),
    # contournements
    ("unquoted labels", 'resource harvester_ssh_key k {\n provisioner local-exec {\n command = "id"\n }\n}\n',
     'provisioner "local-exec"', 2),
    ("escaped type", 'resource "null\\u005fresource" "x" {}\n', 'resource "null_resource"', 1),
    ("escaped type 8", 'resource "terraform\\U0000005fdata" "x" {}\n', 'resource "terraform_data"', 1),
    ("one-line block", 'resource "harvester_volume" "v" { provisioner "local-exec" { command = "id" } }\n',
     'provisioner "local-exec"', 1),
    ("in interpolation", 'resource "harvester_ssh_key" "k" {\n name = "${file("/etc/passwd")}"\n}\n',
     "function file()", 2),
    ("nested interpolation", 'locals {\n a = "x${ "y${ templatefile("t", {}) }" }"\n}\n',
     "function templatefile()", 2),
    ("in directive", 'locals {\n a = "%{ for f in fileset("/", "*") }${f}%{ endfor }"\n}\n',
     "function fileset()", 2),
    ("in heredoc", 'locals {\n a = <<EOT\nx ${file("/etc/shadow")}\nEOT\n}\n', "function file()", 3),
    ("after heredoc", 'locals {\n a = <<-EOT\n  x\n  EOT\n}\nresource "terraform_data" "x" {}\n',
     'resource "terraform_data"', 6),
    ("indented closer", 'locals {\n a = <<EOT\nx\n  EOT\n}\nresource "null_resource" "x" {}\n',
     'resource "null_resource"', 6),
    ("block comment newline", 'resource "harvester_volume" "v" {\n a = 1 /*\n*/ provisioner "local-exec" {\n'
     ' command = "id"\n }\n}\n', "provisioner", 3),
    ("templated label", 'resource "${"null"}_resource" "x" {}\n', "unreadable code", 1),
    ("json body", '{"resource": {"null_resource": {"x": {}}}}\n', "unreadable code", 1),
    ("unterminated string", 'locals {\n a = "x\n}\n', "unreadable code", 2),
    ("unterminated heredoc", 'locals {\n a = <<EOT\nx\n}\n', "unreadable code", 2),
    ("lone carriage return", 'locals {\n a = 1\rdata "external" "x" {}\n}\n', "unreadable code", 2),
    ("unicode separator", 'locals {\n a = 1\u2028data "external" "x" {}\n}\n', "unreadable code", 2),
    ("top attribute", 'a = 1\n', "attribute a", 1),
]

ALLOWED = [
    ("safe raw", SAFE_RAW),
    ("comment only", 'resource "harvester_ssh_key" "k" {\n  # provisioner "local-exec" {}\n'
                     '  name = "k" // file("/etc/shadow")\n  /* data "external" */\n}\n'),
    ("escaped template", 'resource "harvester_ssh_key" "k" {\n  name = "$${file(\\"/etc/x\\")} %%{x}"\n}\n'),
    ("strings", 'resource "harvester_ssh_key" "k" {\n  name = "provisioner local-exec file(x)"\n}\n'),
    ("label named like a keyword", 'resource "harvester_ssh_key" "provisioner" {\n  name = "x"\n}\n'),
    ("locals and functions", 'locals {\n  n = [for i in range(3) : format("vm-%d", i)]\n  m = { a = 1 }\n}\n'
                             'variable "v" {\n  type = string\n  default = "x"\n}\n'
                             'output "o" {\n  value = length(local.n)\n}\n'),
    ("data source", 'data "harvester_image" "i" {\n  name = "img"\n  namespace = "default"\n}\n'),
    ("heredoc with dollars", 'resource "harvester_cloudinit_secret" "c" {\n  name = "c"\n  user_data = <<-EOT\n'
                             '    #cloud-config\n    runcmd: [echo $${HOME}]\n  EOT\n}\n'),
    ("crlf", 'resource "harvester_ssh_key" "k" {\r\n  name = "k"\r\n}\r\n'),
]


@pytest.mark.parametrize("name, code, construct, line", REFUSED, ids=[r[0] for r in REFUSED])
def test_refused_constructs_name_the_construct_and_the_line(name, code, construct, line):
    vs = g.check_resources(code, "harvester", "x.tf")
    assert vs, f"{name} was allowed"
    hit = [v for v in vs if construct in v.construct]
    assert hit, [str(v) for v in vs]
    assert hit[0].line == line, str(hit[0])
    assert str(hit[0]).startswith(f"x.tf, line {line}: ")


@pytest.mark.parametrize("name, code", ALLOWED, ids=[a[0] for a in ALLOWED])
def test_allowed_code(name, code):
    assert g.check_resources(code, "harvester", "x.tf") == []


def test_a_kvm_target_takes_libvirt_types_only():
    assert g.check_resources(SAFE_RAW, "kvm")[0].construct == 'resource "harvester_ssh_key"'
    assert g.check_resources('data "libvirt_node_info" "n" {}\n', "kvm")
    url = ('resource "libvirt_volume" "v" {\n  name = "v"\n  pool = "p"\n'
           '  create = {\n    content = { url = "/etc/shadow" }\n  }\n}\n')
    assert g.check_resources(url, "kvm")[0].construct == "libvirt_volume url"
    ok = url.replace('"/etc/shadow"', "libvirt_cloudinit_disk.x.path")
    assert g.check_resources(ok, "kvm") == []


# ---------------------------------------------------------------------------
# Le code des formulaires passe, et ne peut pas devenir du code
# ---------------------------------------------------------------------------
BASE = "/var/lib/libvirt/images/p/base.qcow2"
HOST = {"name": "node2", "address": "172.16.1.12", "port": 22, "user": "ju", "uri": "qemu:///system"}
EVIL = 'x" }\nEOT\n}\nresource "null_resource" "p" {\n provisioner "local-exec" { command = "id" }\n}\n${file("/etc/shadow")} %{ if true }y%{ endif }\nEOT1'


def form_resources(evil=""):
    return [
        {"id": "a" * 12, "kind": "vm", "spec": {
            "name": "v1", "namespace": "default", "cpu": 1, "memory": "1Gi", "description": "d " + evil,
            "hostname": "h", "efi": True, "secure_boot": True, "ssh_keys": ["default/k"], "ssh_user": "sles",
            "disk": [{"name": "rootdisk", "image": "default/img", "size": "10Gi"},
                     {"name": "data", "size": "5Gi", "storage_class_name": "sc", "auto_delete": False}],
            "network_interface": [{"name": "nic-1", "network_name": "default/production", "wait_for_lease": True}],
            "cloudinit": [{"type": "noCloud", "user_data": "#cloud-config\n" + evil, "network_data": "version: 2\n"}]}},
        {"id": "b" * 12, "kind": "image", "spec": {"name": "img", "url": "https://example.invalid/i.qcow2",
                                                  "display_name": evil or "img", "checksum": "abc"}},
        {"id": "c" * 12, "kind": "ssh_key", "spec": {"name": "key" + evil.replace("\n", " "), "public_key": KEY}},
    ]


KVM_RESOURCES = [
    {"id": "d" * 12, "kind": "kvm_vm", "spec": {"name": "vm1", "pool": "p", "base_image": BASE, "network": "n",
                                                 "user_data": "#cloud-config\n" + EVIL, "description": EVIL}},
    {"id": "e" * 12, "kind": "kvm_volume", "spec": {"name": "vol", "pool": "p", "base_image": BASE}},
    {"id": "f" * 12, "kind": "kvm_network", "spec": {"name": "n2", "mode": "nat", "address": "10.9.9.1",
                                                      "prefix": 24}},
    {"id": "1" * 12, "kind": "kvm_rke2", "spec": {"name": "lab", "servers": 1, "agents": 2, "pool": "p",
                                                   "base_image": BASE, "network": "n",
                                                   "address_start": "10.2.7.50", "prefix": 24,
                                                   "gateway": "10.2.7.1", "ssh_key": KEY}},
]


@pytest.mark.parametrize("evil", ["", EVIL], ids=["plain", "injection"])
def test_every_form_kind_passes_and_form_text_cannot_become_code(evil):
    rendered, errors = wapp._tf_render_resources(form_resources(evil))
    assert not errors and len(rendered) == 3
    assert wapp._tf_guard_rendered(rendered) == []
    for r in rendered:
        body = r[3][len(wapp._TF_HEADER):]
        root, _streams = g._parse(body)
        assert [b.type for b in root.blocks] == ["resource"], body      # aucun bloc de plus


def test_kvm_forms_and_headers_pass():
    rendered, errors = wapp._tf_render_resources(KVM_RESOURCES, "kvm")
    assert not errors
    assert wapp._tf_guard_rendered(rendered, "kvm") == []
    assert g.check_header(k.header(HOST)) == []
    assert g.check_header(k.header(dict(HOST, address="fd00::1", port=2222, uri="qemu:///session"))) == []
    assert g.check_header(wapp._TF_HEADER) == []


@pytest.mark.parametrize("header, construct", [
    (wapp._TF_HEADER.replace('"${path.module}/kubeconfig"', '"/etc/rancher/rke2/rke2.yaml"'), "provider harvester.kubeconfig"),
    (wapp._TF_HEADER.replace('"harvester/harvester"', '"evil/harvester"'), "provider harvester source evil/harvester"),
    (wapp._TF_HEADER + 'terraform {\n  backend "local" {}\n}\n', "backend block"),
    (wapp._TF_HEADER + 'provider "aws" {}\n', "provider aws"),
    (wapp._TF_HEADER + 'resource "harvester_volume" "v" {}\n', "resource block"),
    (k.header(HOST).replace("urlencode(", "file("), "function file()"),
    (k.header(HOST).replace("qemu+sshcmd", "qemu+ssh"), "provider libvirt.uri"),
], ids=["kubeconfig", "source", "backend", "aws", "resource", "file", "uri"])
def test_the_header_is_only_what_the_console_writes(header, construct):
    vs = g.check_header(header)
    assert any(construct in v.construct for v in vs), [str(v) for v in vs]


@pytest.mark.parametrize("value, delim", [
    ("a\nb", "EOT"), ("a\nEOT\nb", "EOT1"), ("a\n  EOT \nEOT1\nb", "EOT2"),
])
def test_the_heredoc_delimiter_is_absent_from_the_value(value, delim):
    out = wapp._hcl_str(value)
    assert out.startswith(f"<<-{delim}\n") and out.endswith(f"\n{delim}")


def test_identifiers_and_strings_are_neutralised():
    assert wapp._hcl_id('k" {}\nresource "x') == "k_____resource__x"
    assert wapp._hcl_id("web-1.a") == "web_1_a"                      # inchangé pour les noms valides
    assert wapp._hcl_str("${file(\"/etc/x\")} %{x}") == '"$${file(\\"/etc/x\\")} %%{x}"'
    assert wapp._hcl_str("a\n${x}\n%{y}") == "<<-EOT\na\n$${x}\n%%{y}\nEOT"


def _tf_bin():
    for cand in (os.environ.get("HARVESTER_OPS_TF_BIN"), shutil.which("tofu"), shutil.which("terraform")):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def test_escaped_form_values_read_back_identical_in_terraform(tmp_path):
    """Les valeurs échappées par _hcl_str (heredoc compris) sont relues
    telles quelles par le vrai Terraform/OpenTofu : la neutralisation ne
    change pas la donnée."""
    tf = _tf_bin()
    if not tf:
        pytest.skip("neither tofu nor terraform installed")
    values = ["plain", 'q"uote', "back\\slash", "${file(\"/etc/hostname\")}", "%{ if true }x%{ endif }",
              "#cloud-config\nruncmd:\n  - echo ${HOME} %{x}\nEOT\n}\n", "a\nEOT\nEOT1\nb"]
    body = "".join(f'output "o{i}" {{\n  value = {wapp._hcl_str(v)}\n}}\n' for i, v in enumerate(values))
    (tmp_path / "main.tf").write_text(body)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "TF_CLI_CONFIG_FILE": "/dev/null",
           "CHECKPOINT_DISABLE": "1", "TF_IN_AUTOMATION": "1"}
    r = subprocess.run([tf, "apply", "-auto-approve", "-no-color", "-input=false"], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    o = json.loads(subprocess.run([tf, "output", "-json"], cwd=tmp_path, env=env, capture_output=True,
                                  text=True, timeout=60).stdout)
    # <<- retire l'indentation commune ; aucune de ces valeurs n'en a
    assert [o[f"o{i}"]["value"].rstrip("\n") for i in range(len(values))] == [v.rstrip("\n") for v in values]


# ---------------------------------------------------------------------------
# 2 bis. Dernier rempart : l'espace relu juste avant chaque commande
# ---------------------------------------------------------------------------
class _Spy:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))

        class R:
            returncode, stdout, stderr = 0, "", ""
        return R()


@pytest.mark.parametrize("name, content", [
    ("evil.tf", LOCAL_EXEC),
    ("evil.tf.json", '{"resource": {}}'),
    ("x.tofu", 'resource "terraform_data" "x" {}\n'),
    ("terraform.tfvars", 'a = 1\n'),
    ("x.auto.tfvars", 'a = 1\n'),
    ("_providers.tf", 'terraform {\n  backend "local" {}\n}\n'),
], ids=["tf", "tf.json", "tofu", "tfvars", "auto.tfvars", "header"])
def test_run_cmd_refuses_a_workspace_with_a_forbidden_file(tmp_path, monkeypatch, name, content):
    spy = _Spy()
    monkeypatch.setattr(wapp.subprocess, "run", spy)
    (tmp_path / "_providers.tf").write_text(wapp._TF_HEADER)
    (tmp_path / name).write_text(content)
    rc, out, err = wapp._tf_run_cmd(tmp_path, "", ["init", "-input=false"])
    assert rc == 126 and err.startswith("refused: ") and name in err
    assert spy.calls == []


def test_run_cmd_runs_a_clean_workspace_and_an_old_header_prefixed_file(tmp_path, monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(wapp.subprocess, "run", spy)
    (tmp_path / "_providers.tf").write_text(wapp._TF_HEADER)
    (tmp_path / "ops.tf").write_text(SAFE_RAW)
    (tmp_path / "legacy.tf").write_text(wapp._TF_HEADER + SAFE_RAW.replace('"ops"', '"old"'))
    (tmp_path / "ops.json").write_text("{}")                   # fiche de la console : jamais lue par tofu
    assert wapp._tf_run_cmd(tmp_path, "", ["plan"])[0] == 0
    assert len(spy.calls) == 1


# ---------------------------------------------------------------------------
# 3. Environnement minimal, miroir seul
# ---------------------------------------------------------------------------
SECRETS = {"VAULT_TOKEN": "hvs.secret231", "AWS_SECRET_ACCESS_KEY": "aws231", "TF_CLI_ARGS_plan": "-var-file=/etc/x",
           "TF_CLI_ARGS": "-no-color", "TF_VAR_x": "v231", "TF_PLUGIN_CACHE_DIR": "/tmp/cache231",
           "TF_DATA_DIR": "/tmp/data231", "SSH_AUTH_SOCK": "/run/agent231", "HARVESTER_OPS_SECRET": "s231",
           "TF_LOG": "TRACE", "KUBECONFIG": "/etc/rancher/rke2/rke2.yaml", "GIT_SSH_COMMAND": "evil"}


def test_opentofu_starts_from_a_minimal_environment(tmp_path, monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(wapp.subprocess, "run", spy)
    for key, val in SECRETS.items():
        monkeypatch.setenv(key, val)
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    wapp._tf_run_cmd(tmp_path, "/ws/kubeconfig", ["plan"], extra_env={"TF_VAR_lab_rke2_token": "tok"})
    env = spy.calls[0][1]["env"]
    for key in SECRETS:
        assert key not in env, key
    assert not any(v in " ".join(env.values()) for v in SECRETS.values())
    assert env["HOME"] == str(tmp_path) and env["TMPDIR"] == str(tmp_path / ".tmp")
    assert env["LANG"] == "fr_FR.UTF-8" and env["HTTPS_PROXY"] == "http://proxy:3128"
    assert env["HARVESTER_KUBECONFIG"] == "/ws/kubeconfig" and env["TF_VAR_lab_rke2_token"] == "tok"
    assert env["CHECKPOINT_DISABLE"] == "1"
    rc = Path(env["TF_CLI_CONFIG_FILE"])
    assert rc == tmp_path / "terraformrc"
    text = rc.read_text()
    assert "filesystem_mirror" in text and "direct" not in text and "plugin_cache_dir" not in text
    assert oct((tmp_path / ".tmp").stat().st_mode & 0o777) == "0o700"


def test_a_real_command_never_runs_a_provisioner(tmp_path):
    """Avec le vrai binaire : la commande est refusée, le fichier témoin
    n'apparaît pas ; contre-épreuve sans la console, le même code
    s'exécute (c'est bien une attaque)."""
    tf = _tf_bin()
    if not tf:
        pytest.skip("neither tofu nor terraform installed")
    mark = tmp_path / "pwned"
    code = f'resource "terraform_data" "x" {{\n  provisioner "local-exec" {{\n    command = "touch {mark}"\n  }}\n}}\n'
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "x.tf").write_text(code)
    for args in (["init", "-input=false"], ["apply", "-auto-approve", "-input=false"]):
        rc, _out, err = wapp._tf_run_cmd(ws, "", args, timeout=120)
        assert rc == 126 and 'provisioner "local-exec"' in err
    assert not mark.exists()
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "x.tf").write_text(code)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(raw), "TF_CLI_CONFIG_FILE": "/dev/null",
           "CHECKPOINT_DISABLE": "1"}
    for args in (["init", "-input=false"], ["apply", "-auto-approve", "-input=false"]):
        subprocess.run([tf, *args], cwd=raw, env=env, capture_output=True, timeout=120)
    assert mark.exists()


def test_the_mirror_is_the_only_provider_source_for_real(tmp_path):
    """Avec le vrai binaire et la configuration de l'espace : un provider
    absent du miroir n'est pas téléchargé (init échoue sans réseau ni
    registre)."""
    tf = _tf_bin()
    if not tf:
        pytest.skip("neither tofu nor terraform installed")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "x.tf").write_text('terraform {\n  required_providers {\n    harvester = {\n'
                             '      source = "harvester/harvester"\n    }\n  }\n}\n')
    # un bloc terraform n'est permis que dans l'en-tête de la console
    (ws / "x.tf").rename(ws / "_providers.tf")
    rc, out, err = wapp._tf_run_cmd(ws, "", ["init", "-input=false"], timeout=120)
    assert rc not in (0, 126), err                  # c'est OpenTofu qui échoue, pas la garde
    assert "registry.terraform.io" not in (ws / "terraformrc").read_text()
    assert not (ws / ".terraform" / "providers").exists()


# ---------------------------------------------------------------------------
# 1. Rôles : le code libre est réservé à l'admin
# ---------------------------------------------------------------------------
@pytest.fixture
def world(tmp_path, monkeypatch):
    store = ts.DeclStore(tmp_path / "tf.db")
    monkeypatch.setattr(wapp, "TF_DECLS", store)
    monkeypatch.setattr(wapp, "TF_WORKSPACES", tmp_path / "ws")
    kc = tmp_path / "kc"
    kc.write_text("apiVersion: v1\n")
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: str(kc) if c == "harv1" else None)
    started = []
    monkeypatch.setattr(wapp, "_tf_decl_runner", lambda *a, **kw: started.append(a))
    monkeypatch.setattr(wapp, "_tf_apply_runner", lambda *a, **kw: started.append(a))
    monkeypatch.setattr(wapp, "_tf_apply_declaration_runner", lambda *a, **kw: started.append(a))
    wapp.app.config["TESTING"] = True
    role = {"v": "admin"}
    monkeypatch.setattr(wapp, "current_role", lambda: role["v"])
    return {"store": store, "started": started, "role": role, "client": wapp.app.test_client()}


def raw_res(code, rid="9" * 12):
    return {"id": rid, "kind": "raw", "spec": {"tf": code}}


def ssh_res(rid="8" * 12, name="ops"):
    return {"id": rid, "kind": "ssh_key", "spec": {"name": name, "namespace": "default", "public_key": KEY}}


def create(world, name, resources, role="admin"):
    world["role"]["v"] = role
    return world["client"].post("/api/tf-declarations", json={"cluster": "harv1", "name": name,
                                                              "resources": resources})


def plan(world, decl_id, role):
    world["role"]["v"] = role
    return world["client"].post("/api/terraform/harv1/apply_declaration",
                                json={"declaration": {"id": decl_id}, "dry_run": True})


ROLE_TABLE = [
    # (rôle, code libre ?, contenu, création, plan)
    ("operator", True, SAFE_RAW, 403, None),
    ("viewer", True, SAFE_RAW, 403, None),
    ("admin", True, SAFE_RAW, 201, 201),
    ("admin", True, LOCAL_EXEC, 201, 422),
    ("operator", False, None, 201, 201),
]


@pytest.mark.parametrize("role, raw, code, created, planned", ROLE_TABLE,
                         ids=["operator-raw", "viewer-raw", "admin-safe-raw", "admin-local-exec", "operator-form"])
def test_roles_and_guard_on_create_and_plan(world, role, raw, code, created, planned):
    r = create(world, "d", [raw_res(code)] if raw else [ssh_res()], role)
    assert r.status_code == created, r.get_json()
    if created == 403:
        assert r.get_json().get("required") in ("admin", "operator")
        assert world["store"].list() == []
        return
    p = plan(world, r.get_json()["id"], role)
    assert p.status_code == planned, p.get_json()
    if planned == 422:
        body = p.get_json()
        assert body["code"] == "forbidden-construct"
        assert body["violations"][0]["line"] == 4 and "local-exec" in body["violations"][0]["error"]
        assert world["started"] == []
    else:
        assert len(world["started"]) == 1


def test_an_operator_cannot_edit_plan_apply_or_destroy_a_raw_declaration(world):
    d = create(world, "raw", [raw_res(SAFE_RAW)]).get_json()
    c = world["client"]
    world["role"]["v"] = "operator"
    assert c.put(f"/api/tf-declarations/{d['id']}", json={"revision": d["revision"], "name": "x"}).status_code == 403
    assert c.put(f"/api/tf-declarations/{d['id']}",
                 json={"revision": d["revision"], "resources": [ssh_res()]}).status_code == 403
    for dry in (True, False):
        r = c.post("/api/terraform/harv1/apply_declaration", json={"declaration": {"id": d["id"]}, "dry_run": dry})
        assert r.status_code == 403
        r = c.post("/api/terraform/harv1/destroy_declaration", json={"declaration": {"id": d["id"]}, "dry_run": dry})
        assert r.status_code == 403
    assert world["started"] == []
    # un opérateur ne glisse pas de code libre dans une déclaration de formulaire
    f = create(world, "form", [ssh_res()], "operator").get_json()
    r = c.put(f"/api/tf-declarations/{f['id']}", json={"revision": f["revision"],
                                                        "resources": [ssh_res(), raw_res(SAFE_RAW)]})
    assert r.status_code == 403
    world["role"]["v"] = "admin"
    assert c.put(f"/api/tf-declarations/{d['id']}", json={"revision": d["revision"], "name": "x"}).status_code == 200


def test_the_older_routes_follow_the_same_rules(world):
    c = world["client"]
    world["role"]["v"] = "operator"
    assert c.post("/api/terraform/harv1/apply", json={"kind": "raw", "spec": {"tf": SAFE_RAW}}).status_code == 403
    r = c.post("/api/terraform/harv1/apply_declaration",
               json={"declaration": {"name": "x", "resources": [{"kind": "raw", "spec": {"tf": SAFE_RAW}}]}})
    assert r.status_code == 403
    world["role"]["v"] = "admin"
    r = c.post("/api/terraform/harv1/apply", json={"kind": "raw", "spec": {"tf": LOCAL_EXEC}, "dry_run": True})
    assert r.status_code == 422 and r.get_json()["code"] == "forbidden-construct"
    r = c.post("/api/terraform/harv1/apply_declaration",
               json={"declaration": {"name": "x", "resources": [{"kind": "raw", "spec": {"tf": LOCAL_EXEC}}]}})
    assert r.status_code == 422
    assert world["started"] == []
    assert c.post("/api/terraform/harv1/apply", json={"kind": "raw", "spec": {"tf": SAFE_RAW},
                                                       "dry_run": True}).status_code == 201


def test_a_resource_named_like_the_provider_file_cannot_replace_it():
    rendered, _ = wapp._tf_render_resources([ssh_res(name="_providers")])
    assert rendered[0][0] == "_providers_2"


# ---------------------------------------------------------------------------
# 4. Au démarrage : signalées, jamais supprimées
# ---------------------------------------------------------------------------
def test_stored_declarations_that_break_the_rules_are_flagged_at_startup(world):
    store = world["store"]
    good = store.create("1" * 12, "harv1", "good", "", [ssh_res()])
    raw_ok = store.create("2" * 12, "harv1", "raw-ok", "", [raw_res(SAFE_RAW)])
    bad = store.create("3" * 12, "harv1", "bad", "", [ssh_res(), raw_res(LOCAL_EXEC)])
    sneaky = store.create("4" * 12, "harv1", "sneaky", "", [raw_res('locals {\n k = core::file("kubeconfig")\n}\n')])
    flagged = wapp._tf_decl_startup_audit()
    assert sorted(flagged) == [bad["id"], sneaky["id"]]
    assert {d["id"] for d in store.list()} == {good["id"], raw_ok["id"], bad["id"], sneaky["id"]}
    world["role"]["v"] = "viewer"
    lst = world["client"].get("/api/tf-declarations").get_json()["declarations"]
    by = {d["id"]: d for d in lst}
    assert by[good["id"]]["blocked"] == [] and by[good["id"]]["raw"] is False
    assert by[raw_ok["id"]]["blocked"] == [] and by[raw_ok["id"]]["raw"] is True
    assert "local-exec" in by[bad["id"]]["blocked"][0]["error"] and by[bad["id"]]["blocked"][0]["index"] == 1
    assert "core::file" in by[sneaky["id"]]["blocked"][0]["error"]
    # plan, apply et destroy refusés, même pour l'admin, avec la raison
    for mode in ("apply_declaration", "destroy_declaration"):
        world["role"]["v"] = "admin"
        r = world["client"].post(f"/api/terraform/harv1/{mode}", json={"declaration": {"id": bad["id"]},
                                                                      "dry_run": True})
        assert r.status_code == 422 and "local-exec" in r.get_json()["error"]
    assert world["started"] == []


# ---------------------------------------------------------------------------
# Mutations : chaque garde retirée, le test le voit
# ---------------------------------------------------------------------------
def test_mutation_without_the_nested_block_rule_local_exec_passes(monkeypatch):
    monkeypatch.setattr(g, "NESTED_FORBIDDEN", ())
    assert g.check_resources(LOCAL_EXEC) == []


def test_mutation_without_the_function_rule_file_passes(monkeypatch):
    monkeypatch.setattr(g, "is_forbidden_function", lambda name: False)
    assert g.check_resources('locals {\n k = file("/etc/shadow")\n}\n') == []


def test_mutation_without_the_type_allow_list_external_passes(monkeypatch):
    monkeypatch.setitem(g.TYPES_OF, "harvester", {"resource": frozenset({"null_resource"}),
                                                  "data": frozenset({"external"})})
    assert g.check_resources('data "external" "x" {\n program = ["sh"]\n}\n') == []


def test_mutation_without_the_route_guard_the_workspace_check_still_refuses(world, monkeypatch, tmp_path):
    """Défense en profondeur : la garde de la route retirée, le plan part,
    mais la commande est refusée par le dernier rempart."""
    monkeypatch.setattr(wapp, "_tf_guard_rendered", lambda rendered, target_kind="harvester": [])
    d = create(world, "evil", [raw_res(LOCAL_EXEC)]).get_json()
    assert plan(world, d["id"], "admin").status_code == 201          # la route ne voit plus rien
    ws = tmp_path / "ws2"
    ws.mkdir()
    (ws / "evil.tf").write_text(LOCAL_EXEC)
    spy = _Spy()
    monkeypatch.setattr(wapp.subprocess, "run", spy)
    assert wapp._tf_run_cmd(ws, "", ["plan"])[0] == 126 and spy.calls == []


def test_mutation_without_the_role_check_an_operator_gets_through(world, monkeypatch):
    monkeypatch.setattr(wapp, "_tf_is_admin", lambda: True)
    assert create(world, "d", [raw_res(SAFE_RAW)], "operator").status_code == 201


def test_mutation_inheriting_the_environment_is_seen(tmp_path, monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(wapp.subprocess, "run", spy)
    monkeypatch.setenv("VAULT_TOKEN", "hvs.secret231")
    monkeypatch.setattr(wapp, "_tf_env", lambda ws: dict(os.environ))
    wapp._tf_run_cmd(tmp_path, "", ["plan"])
    assert "VAULT_TOKEN" in spy.calls[0][1]["env"]                   # le test d'environnement le verrait


def test_mutation_unescaped_form_values_would_be_caught_by_the_guard(monkeypatch):
    """Sans la neutralisation des valeurs, l'injection par le formulaire
    devient du code : la lecture le refuse quand même."""
    monkeypatch.setattr(wapp, "_hcl_str", lambda s: f'<<-EOT\n{s}\nEOT' if "\n" in str(s) else f'"{s}"')
    rendered, _ = wapp._tf_render_resources(form_resources(EVIL))
    bad = wapp._tf_guard_rendered(rendered)
    assert bad and any("null_resource" in v["error"] or "local-exec" in v["error"]
                       or "file()" in v["error"] for v in bad)


# ---------------------------------------------------------------------------
# Écran : le code libre bridé pour un non-admin, les déclarations bloquées
# ---------------------------------------------------------------------------
def test_the_view_locks_free_code_and_shows_blocked_declarations():
    js = (ROOT / "web/static/js/tf-decl-view.js").read_text()
    assert "const rawLocked = () =>" in js and "r !== 'admin'" in js
    assert "tr('tfd.t.rawAdmin')" in js and "tr('tfd.blockedTitle')" in js and "tr('tfd.ds.blocked')" in js
    assert "esc(v.error)" in js and "esc(blocked[0].error)" in js           # texte du serveur échappé
    assert "document.addEventListener('hops:role'" in js
    i18n = (ROOT / "web/static/js/i18n.js").read_text()
    for key in ("tfd.t.rawAdmin", "tfd.rawAdminNotice", "tfd.blockedTitle", "tfd.blockedHint",
                "tfd.ds.blocked", "tfd.t.blocked"):
        assert i18n.count(f"'{key}':") == 5, key
