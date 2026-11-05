"""2.4.0, revue B2-I4 : une valeur arrive dans le HCL généré comme du texte.

`_hcl_str` (genres à formulaire : vm, image, ssh_key...) échappe guillemet et
barre oblique inverse depuis 2.3.0 ; il échappe aussi l'interpolation `${` et
la directive `%{` (sinon Terraform évalue `${file("...")}` et lit un fichier
de la console), dans une chaîne comme dans un heredoc, et un heredoc ne peut
plus être fermé par une ligne de la valeur.

Mutation jouée : échappement de `${` retiré (les cas interpolation tombent).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402


@pytest.mark.parametrize("value,expected", [
    ('${file("/home/ju/.kube/harvester.yaml")}', '"$${file(\\"/home/ju/.kube/harvester.yaml\\")}"'),
    ("%{if true}x%{endif}", '"%%{if true}x%%{endif}"'),
    ("$${deja}", '"$$${deja}"'),
    ("50%", '"50%"'),
    ("$x", '"$x"'),
])
def test_hcl_str_neutralizes_templates(value, expected):
    assert wapp._hcl_str(value) == expected


@pytest.mark.parametrize("value,needle", [
    ('#cloud-config\nruncmd:\n  - echo ${HOME}\n', "echo $${HOME}"),
    ('#cloud-config\nruncmd:\n  - echo %{x}\n', "echo %%{x}"),
])
def test_hcl_heredoc_neutralizes_templates(value, needle):
    out = wapp._hcl_str(value)
    assert out.startswith("<<-") and needle in out


def test_hcl_heredoc_cannot_be_closed_by_the_value():
    value = "a\nEOT\nresource \"x\" \"y\" {}\n"
    out = wapp._hcl_str(value)
    head, _, rest = out.partition("\n")
    term = head[3:]
    assert term != "EOT"
    body = rest.rsplit("\n", 1)[0]
    assert term not in [ln.strip() for ln in body.split("\n")]
    assert body.startswith("a\nEOT\nresource") and out.endswith("\n" + term)


def test_image_name_value_cannot_read_a_console_file():
    hcl = wapp._render_tf_for_kind("image", {"name": "i", "display_name": '${file("/etc/hosts")}',
                                             "url": "https://h/x.img"})
    assert '${file(' not in hcl.replace("$${file(", "")
