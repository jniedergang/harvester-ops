"""2.9.1: the manual B1 cleanup commands of docs/{en,fr}/secrets.md name only
unit files and image references that exist in the code. Regression for the
2.9.0 release report: a hardcoded `podman rmi ... :current ... :previous`
failed on `:previous` when B1 was never upgraded, and the same block mixed
"B1 leftovers" with "the 2.9.0 agent units", which must not be removed on a
console staying on 2.9.x."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ("docs/en/secrets.md", "docs/fr/secrets.md")
UNIT_RE = re.compile(r"\bharvester-(?:secrets-agent|secrets-rotate|ops-openbao)\.(?:path|service|timer)\b")
LOCAL_IMAGE_RE = re.compile(r"\blocalhost/harvester-ops-openbao\b")
AC_IMAGE_RE = re.compile(r"\b([a-z0-9.-]+/containers/openbao)\b")

STAY_HEADING = {"docs/en/secrets.md": "#### Removing the B1 leftovers, console staying on 2.9.x",
                "docs/fr/secrets.md": "#### Retirer les restes de B1, console gardée en 2.9.x"}
ROLLBACK_HEADING = {"docs/en/secrets.md": "#### Removing the 2.9.0 secret store agent, after a rollback to 2.8.x",
                    "docs/fr/secrets.md":
                        "#### Retirer l'agent de magasin de secrets de la 2.9.0, après un retour en 2.8.x"}
AGENT_UNITS = {"harvester-secrets-agent.path", "harvester-secrets-agent.service", "harvester-secrets-rotate.timer"}


def _text(doc):
    return (ROOT / doc).read_text()


def _block(text, heading):
    """Fenced bash block right after `heading`, bash brace expansion resolved
    (`name.{a,b}` -> `name.a name.b`) so the unit regex sees both names."""
    start = text.index(heading)
    m = re.search(r"```bash\n(.*?)```", text[start:], re.S)
    assert m, f"no bash block after {heading!r}"
    brace = re.compile(r"([\w.-]+)\.\{([a-z,]+)\}")
    return brace.sub(lambda b: " ".join(f"{b.group(1)}.{o}" for o in b.group(2).split(",")), m.group(1))


def test_doc_units_are_real_unit_files():
    present = {p.name for p in (ROOT / "config" / "systemd").iterdir()}
    for doc in DOCS:
        names = set(UNIT_RE.findall(_text(doc)))
        assert names, f"no unit referenced in {doc}"
        missing = sorted(names - present)
        assert not missing, (doc, missing)


def test_doc_local_image_matches_the_agent_constant():
    agent = (ROOT / "bin" / "harvester-secrets-agent.py").read_text()
    m = re.search(r'^IMAGE = "([^"]+)"', agent, re.M)
    assert m and m.group(1) == "localhost/harvester-ops-openbao"
    for doc in DOCS:
        assert LOCAL_IMAGE_RE.search(_text(doc)), doc


def test_doc_application_collection_image_matches_the_bundle_defaults():
    bundle = (ROOT / "bin" / "lib" / "openbao_bundle.py").read_text()
    registry = re.search(r'^DEFAULT_REGISTRY = "([^"]+)"', bundle, re.M).group(1)
    repo = re.search(r'^DEFAULT_REPOSITORY = "([^"]+)"', bundle, re.M).group(1)
    want = f"{registry}/{repo}"
    for doc in DOCS:
        found = AC_IMAGE_RE.findall(_text(doc))
        assert want in found, (doc, found)


def test_stay_on_290_block_never_touches_the_agent_units_or_opt_dir():
    for doc in DOCS:
        block = _block(_text(doc), STAY_HEADING[doc])
        assert not (AGENT_UNITS & set(UNIT_RE.findall(block))), (doc, block)
        assert "/opt/harvester-ops/openbao" not in block, doc


def test_rollback_block_removes_every_agent_unit():
    for doc in DOCS:
        block = _block(_text(doc), ROLLBACK_HEADING[doc])
        assert AGENT_UNITS <= set(UNIT_RE.findall(block)), (doc, block)


def test_image_removal_never_hard_fails_on_a_missing_tag():
    """Every `podman rmi` command is either filtered through a list that was
    just queried (xargs) or uses --ignore/-i, so a tag the agent never
    created (e.g. `:previous` without an upgrade) does not abort the script."""
    for doc in DOCS:
        for line in _text(doc).splitlines():
            if "sudo podman rmi" not in line:
                continue
            tokens = line.split()
            assert "-i" in tokens or "--ignore" in tokens, line
