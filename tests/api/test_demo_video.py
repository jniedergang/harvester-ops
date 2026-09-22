"""Outillage vidéo (tools/demo-video) : montage et sous-titres.

Le tournage demande un navigateur et un vrai cluster, mais le montage est
du calcul : découpe des accélérations, report des repères dans le temps de
la vidéo finale, et génération du fichier de sous-titres. C'est ce que ces
tests couvrent, plus la parité des textes entre l'anglais et le français
(même règle que les cinq langues de l'interface : une vidéo ne part pas
avec un sous-titre manquant).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
TOOL = ROOT / "tools" / "demo-video"
sys.path.insert(0, str(TOOL))

from lib import timeline  # noqa: E402

FAST = [{"t": 20, "kind": "fast-start", "factor": 8}, {"t": 84, "kind": "fast-end"}]


def test_a_take_without_waiting_stays_one_segment():
    segs = timeline.plan_segments(60, [{"t": 3, "kind": "say", "key": "a"}])
    assert segs == [{"start": 0.0, "end": 60, "factor": 1.0}]
    assert timeline.output_duration(segs) == 60


def test_a_wait_is_cut_out_and_compressed():
    segs = timeline.plan_segments(100, FAST)
    assert [s["factor"] for s in segs] == [1.0, 8.0, 1.0]
    # 20 s réelles + 64 s à x8 (8 s) + 16 s réelles
    assert timeline.output_duration(segs) == pytest.approx(44.0)


def test_instants_are_reported_into_the_edited_film():
    segs = timeline.plan_segments(100, FAST)
    assert timeline.remap(10, segs) == pytest.approx(10)
    assert timeline.remap(52, segs) == pytest.approx(24)   # moitié de l'attente
    assert timeline.remap(90, segs) == pytest.approx(34)
    assert timeline.remap(1000, segs) == pytest.approx(44)


def test_the_speed_marker_covers_the_accelerated_part_of_the_output():
    spans = timeline.fast_spans(timeline.plan_segments(100, FAST))
    assert spans == [{"start": 20.0, "end": 28.0, "factor": 8.0}]


def test_a_wait_left_open_by_the_scene_closes_at_the_end():
    segs = timeline.plan_segments(50, [{"t": 30, "kind": "fast-start", "factor": 4}])
    assert segs[-1] == {"start": 30, "end": 50, "factor": 4.0}


def test_a_nested_or_stray_wait_is_refused():
    with pytest.raises(ValueError):
        timeline.plan_segments(50, [{"t": 1, "kind": "fast-start"},
                                    {"t": 2, "kind": "fast-start"}])
    with pytest.raises(ValueError):
        timeline.plan_segments(50, [{"t": 2, "kind": "fast-end"}])


def test_captions_hold_until_the_next_one():
    cues = [{"t": 2, "kind": "say", "key": "a"}, {"t": 9, "kind": "say", "key": "b"}]
    segs = timeline.plan_segments(30, cues)
    caps = timeline.build_captions(cues, segs, {"a": "un", "b": "deux"})
    assert caps[0]["start"] == pytest.approx(2)
    assert caps[0]["end"] == pytest.approx(9)
    assert caps[1]["end"] == pytest.approx(17)      # borné à max_hold


def test_a_caption_never_runs_past_the_end_of_the_film():
    cues = [{"t": 28, "kind": "say", "key": "a"}]
    segs = timeline.plan_segments(30, cues)
    caps = timeline.build_captions(cues, segs, {"a": "un"})
    assert caps[0]["end"] == pytest.approx(30)


def test_a_missing_caption_text_stops_the_build():
    cues = [{"t": 1, "kind": "say", "key": "absent"}]
    with pytest.raises(KeyError):
        timeline.build_captions(cues, timeline.plan_segments(10, cues), {})


def test_the_readme_preview_starts_where_the_scene_asked():
    cues = FAST + [{"t": 90, "kind": "preview"}]
    segs = timeline.plan_segments(100, cues)
    win = timeline.preview_window(cues, segs, length=8)
    assert win == {"start": pytest.approx(34), "length": 8}


def test_the_preview_never_falls_off_the_end():
    cues = [{"t": 29, "kind": "preview"}]
    segs = timeline.plan_segments(30, cues)
    assert timeline.preview_window(cues, segs, length=8)["start"] == pytest.approx(22)


def test_the_subtitle_file_carries_captions_and_speed_markers():
    doc = timeline.ass_document(
        [{"start": 1.5, "end": 4.0, "text": "un texte"}],
        [{"start": 10.0, "end": 12.0, "factor": 8}], offset=2.0)
    assert "Dialogue: 0,0:00:03.50,0:00:06.00,Caption,,0,0,0,,un texte" in doc
    assert "Dialogue: 1,0:00:12.00,0:00:14.00,Speed,,0,0,0,,x8" in doc
    assert "PlayResX: 1920" in doc


def test_a_long_caption_is_wrapped_not_cut():
    doc = timeline.ass_document([{"start": 0, "end": 3, "text": "mot " * 40}])
    line = [l for l in doc.splitlines() if l.startswith("Dialogue")][0]
    assert "\\N" in line
    assert line.count("mot") == 40


def _caption_files():
    return sorted((TOOL / "captions").glob("*.en.yaml"))


def test_every_scene_has_its_two_languages_with_the_same_keys():
    import yaml
    files = _caption_files()
    assert files, "aucun texte de sous-titres"
    for en in files:
        fr = en.with_name(en.name.replace(".en.yaml", ".fr.yaml"))
        assert fr.exists(), f"{fr.name} manquant"
        ken = set(yaml.safe_load(en.read_text()) or {})
        kfr = set(yaml.safe_load(fr.read_text()) or {})
        # Piège YAML 1.1 : une clé « off », « on », « yes » ou « no » devient
        # un booléen, et la scène ne retrouve plus son sous-titre.
        assert all(isinstance(k, str) for k in ken | kfr), f"{en.name} : clé non textuelle"
        assert ken == kfr, f"{en.name} / {fr.name} : {ken ^ kfr}"


def test_every_caption_key_used_by_a_scene_exists_in_both_languages():
    """Le tournage échouerait à la fabrication, pas au tournage : on préfère
    l'attraper ici, avant d'allumer un cluster."""
    import re
    import yaml
    for scene in sorted((TOOL / "scenes").glob("*.py")):
        if scene.name == "__init__.py":
            continue
        src = scene.read_text()
        name = re.search(r'^NAME\s*=\s*["\']([^"\']+)', src, re.M)
        assert name, f"{scene.name} : NAME manquant"
        keys = set(re.findall(r'say\(\s*["\']([^"\']+)["\']', src))
        for lang in ("en", "fr"):
            texts = yaml.safe_load((TOOL / "captions" / f"{name.group(1)}.{lang}.yaml").read_text())
            assert keys <= set(texts), f"{scene.name}/{lang} : {keys - set(texts)}"


def test_no_scene_waits_with_wait_for_function():
    """Piège payé le 22/09/2026 : `page.wait_for_function` reçoit la PROMESSE
    d'une fonction asynchrone, la juge vraie, et rend la main au premier tour.
    Deux prises ont été perdues ainsi (la maintenance n'était pas terminée).
    Les scènes attendent avec `cam.until`, qui évalue pour de bon."""
    for src in list((TOOL / "scenes").glob("*.py")) + [TOOL / "record.py"]:
        text = src.read_text()
        if "wait_for_function" in text:
            assert "def until" in text, f"{src.name} : attente par wait_for_function"
