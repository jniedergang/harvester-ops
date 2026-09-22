"""v1.44.10 — le panneau du paquet de support parle la langue de l'interface.

Relevé en préparant la vidéo « Activité » en français : « Bundle ready »,
« Download archive », « Loading... » étaient écrits en dur, et les étapes
n'existaient qu'en anglais et en français (l'allemand, l'espagnol et
l'italien retombaient sur l'anglais).
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SUPPORT = (ROOT / "web" / "static" / "js" / "support.js").read_text()
I18N = (ROOT / "web" / "static" / "js" / "i18n.js").read_text()


def test_no_english_left_hard_coded_in_the_support_panel():
    for text in ("Bundle ready", "Download archive", "Stream lost", "Loading...",
                 "(no past bundles)", "'Failed'", "Capturing metadata"):
        assert text not in SUPPORT, text


def test_every_support_key_exists_in_the_five_languages():
    keys = ["support.step.metadata", "support.step.archive", "support.ready",
            "support.download", "support.failed", "support.streamLost",
            "support.loading", "support.noPast", "support.anonymizedTag"]
    for k in keys:
        assert I18N.count(f"'{k}'") == 5, k
        assert f"i18n.t('{k}')" in SUPPORT, k
