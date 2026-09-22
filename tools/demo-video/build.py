#!/usr/bin/env python3
"""Monte une prise : accélère les attentes, incruste les sous-titres, pose
les cartons et la musique, puis sort le MP4 et l'aperçu animé du README.

    ./build.py out/cluster-maintenance-en

Rien n'est irréversible ici : la prise (raw.webm + cues.json) reste intacte,
on peut remonter autant de fois que voulu, par exemple après avoir reformulé
un sous-titre ou traduit la scène.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from lib import timeline  # noqa: E402

TITLE_SECONDS = 2.6
END_SECONDS = 2.4
FPS = 30
MUSIC = HERE / "music" / "theme.mp3"


def probe_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True,
                         check=True).stdout.strip()
    return float(out)


def render_cards(texts, out_dir, lang):
    """Cartons de début et de fin, dessinés en HTML (la police et les couleurs
    du produit) puis photographiés : ce ffmpeg n'a pas de filtre de texte."""
    from playwright.sync_api import sync_playwright
    tpl = (HERE / "cards" / "card.html").read_text()
    made = {}
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_page(viewport={"width": 1920, "height": 1080})
        for kind in ("title", "end"):
            html = (tpl.replace("__TITLE__", texts[f"card.{kind}"])
                       .replace("__SUB__", texts.get(f"card.{kind}.sub", ""))
                       .replace("__LANG__", lang))
            p.set_content(html)
            p.wait_for_timeout(250)
            path = out_dir / f"card-{kind}.png"
            p.screenshot(path=str(path))
            made[kind] = path
        b.close()
    return made


def build(take_dir, force_music=None):
    take = Path(take_dir).resolve()
    meta = json.loads((take / "cues.json").read_text())
    scene, lang = meta["scene"], meta["lang"]
    texts = yaml.safe_load((HERE / "captions" / f"{scene}.{lang}.yaml").read_text())

    raw = take / "raw.webm"
    video_duration = probe_duration(raw)
    # Le navigateur commence à filmer un peu après la création du contexte :
    # l'écart entre le temps réel de la scène et la durée du film donne ce
    # retard, et recale tous les repères.
    offset = max(0.0, meta["wall_duration"] - video_duration)
    cues = [dict(c, t=max(0.0, c["t"] - offset)) for c in meta["cues"]]

    segments = timeline.plan_segments(video_duration, cues)
    captions = timeline.build_captions(cues, segments, texts)
    spans = timeline.fast_spans(segments)
    body = timeline.output_duration(segments)
    total = TITLE_SECONDS + body + END_SECONDS

    ass = take / "captions.ass"
    ass.write_text(timeline.ass_document(captions, spans, offset=TITLE_SECONDS))
    cards = render_cards(texts, take, lang)

    music = Path(force_music) if force_music else MUSIC
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-i", str(raw),
           "-loop", "1", "-t", str(TITLE_SECONDS), "-i", str(cards["title"]),
           "-loop", "1", "-t", str(END_SECONDS), "-i", str(cards["end"])]
    if music.exists():
        cmd += ["-stream_loop", "-1", "-i", str(music)]

    parts, chain = [], []
    for i, s in enumerate(segments):
        chain.append(
            f"[0:v]trim=start={s['start']:.3f}:end={s['end']:.3f},"
            f"setpts=(PTS-STARTPTS)/{s['factor']},fps={FPS},"
            f"scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad=1920:1080:-1:-1,setsar=1,format=yuv420p[b{i}]")
        parts.append(f"[b{i}]")
    chain.append("".join(parts) + f"concat=n={len(parts)}:v=1:a=0[body]")
    for idx, name, dur in ((1, "title", TITLE_SECONDS), (2, "end", END_SECONDS)):
        chain.append(f"[{idx}:v]fps={FPS},scale=1920:1080,setsar=1,format=yuv420p,"
                     f"fade=t=in:st=0:d=0.4,fade=t=out:st={dur - 0.4:.2f}:d=0.4[c{name}]")
    chain.append("[ctitle][body][cend]concat=n=3:v=1:a=0[joined]")
    chain.append(f"[joined]ass='{ass}'[vout]")
    maps = ["-map", "[vout]"]
    if music.exists():
        chain.append(f"[3:a]atrim=0:{total:.2f},asetpts=N/SR/TB,volume=0.16,"
                     f"afade=t=in:st=0:d=1.5,afade=t=out:st={max(0.0, total - 2.5):.2f}:d=2.5"
                     "[aout]")
        maps += ["-map", "[aout]", "-c:a", "aac", "-b:a", "128k"]

    mp4 = take / f"{scene}-{lang}.mp4"
    cmd += ["-filter_complex", ";".join(chain), *maps,
            "-c:v", "libopenh264", "-b:v", "7M", "-profile:v", "high",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4)]
    subprocess.run(cmd, check=True)

    win = timeline.preview_window(cues, segments)
    webp = take / f"{scene}-{lang}.webp"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{win['start'] + TITLE_SECONDS:.2f}", "-t", f"{win['length']:.2f}",
                    "-i", str(mp4), "-vf", "fps=12,scale=960:-2",
                    "-c:v", "libwebp_anim", "-lossless", "0", "-q:v", "62",
                    "-loop", "0", "-an", str(webp)], check=True)

    print(f"{mp4.name} : {total:.1f}s (prise {video_duration:.1f}s, "
          f"{len(captions)} sous-titres, {len(spans)} accélération(s))")
    print(f"{webp.name} : aperçu {win['length']:.0f}s à partir de {win['start']:.0f}s")
    return mp4


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("take", help="dossier de prise (out/<scene>-<lang>)")
    ap.add_argument("--music", default=None)
    args = ap.parse_args()
    build(args.take, force_music=args.music)
