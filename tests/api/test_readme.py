"""Les deux README (anglais, français) restent d'accord entre eux et avec
ce qui existe : mêmes vidéos, images présentes, une vidéo par scène filmée.

Le README est la première chose que voit un visiteur du dépôt public ; une
image cassée ou une vidéo citée dans une seule langue s'y voit tout de suite.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
READMES = {"en": ROOT / "README.md", "fr": ROOT / "README.fr.md"}
ASSET = re.compile(r"releases/download/v[\d.]+/([\w.-]+\.mp4)")


def _text(lang):
    return READMES[lang].read_text()


def test_both_readmes_link_the_same_videos():
    assert set(ASSET.findall(_text("en"))) == set(ASSET.findall(_text("fr")))


def test_every_filmed_scene_is_linked_in_both_languages():
    names = set()
    for scene in (ROOT / "tools" / "demo-video" / "scenes").glob("*.py"):
        m = re.search(r'^NAME\s*=\s*"([^"]+)"', scene.read_text(), re.M)
        if m:
            names.add(m.group(1))
    wanted = {f"{n}-{lang}.mp4" for n in names for lang in ("en", "fr")}
    assert wanted == set(ASSET.findall(_text("en"))), wanted ^ set(ASSET.findall(_text("en")))


def test_every_local_image_exists():
    for lang in READMES:
        for path in re.findall(r"\]\((docs/assets/[^)]+)\)", _text(lang)):
            assert (ROOT / path).exists(), f"{lang} : {path}"


def test_the_two_readmes_point_at_each_other():
    assert "(README.fr.md)" in _text("en")
    assert "(README.md)" in _text("fr")


def test_no_typographic_trace_in_the_readmes():
    for lang in READMES:
        text = _text(lang)
        assert "—" not in text and "→" not in text, lang


UA = re.compile(r"^https://github\.com/user-attachments/assets/[0-9a-f-]{36}$", re.M)


def test_each_readme_plays_every_clip_in_its_own_language():
    """GitHub n'affiche un lecteur que pour une vidéo téléversée par son
    interface web, dont l'adresse est SEULE sur sa ligne, entourée de lignes
    vides. Neuf vidéos par langue (le tour en tête de page, les huit autres
    dans « Watch it work »), et aucune n'est partagée entre les deux langues."""
    found = {}
    for lang in READMES:
        text = _text(lang)
        urls = UA.findall(text)
        assert len(urls) == 9 and len(set(urls)) == 9, (lang, len(urls))
        for u in urls:
            i = text.index(u)
            assert text[i - 2:i] == "\n\n" and text[i + len(u):i + len(u) + 2] == "\n\n", (lang, u)
        found[lang] = set(urls)
    assert not found["en"] & found["fr"]
