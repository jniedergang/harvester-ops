"""Montage d'une prise : accélérations, report des repères, sous-titres.

Fonctions pures, sans ffmpeg ni navigateur : la scène produit des repères
(« dis ce texte », « ici on accélère »), ce module calcule le découpage de
la vidéo de sortie et le fichier de sous-titres. C'est la partie qu'on peut
tester sans cluster ni carte graphique.

Vocabulaire :
- *source*     : la vidéo brute sortie du navigateur, en temps réel.
- *sortie*     : la vidéo montée, où les attentes sont accélérées.
- *segment*    : un morceau de source joué à une vitesse donnée.
"""

import textwrap

SAY = "say"
FAST_START = "fast-start"
FAST_END = "fast-end"
PREVIEW = "preview"


def plan_segments(duration, cues):
    """Découpe [0, duration] en segments, un par changement de vitesse.

    Rend une liste de dicts {start, end, factor}. `factor` vaut 1 en temps
    réel, 8 (ou ce que demande le repère) sur une attente accélérée.
    """
    if duration <= 0:
        raise ValueError("durée de source nulle")
    fast = []
    open_at = None
    open_factor = None
    for c in sorted(cues, key=lambda c: c["t"]):
        if c["kind"] == FAST_START:
            if open_at is not None:
                raise ValueError("accélération déjà ouverte à %.1fs" % open_at)
            open_at, open_factor = max(0.0, c["t"]), float(c.get("factor", 8))
        elif c["kind"] == FAST_END:
            if open_at is None:
                raise ValueError("fin d'accélération sans début à %.1fs" % c["t"])
            end = min(duration, c["t"])
            if end > open_at:
                fast.append((open_at, end, open_factor))
            open_at = open_factor = None
    if open_at is not None:
        # La scène s'est terminée pendant une attente : on ferme à la fin.
        if duration > open_at:
            fast.append((open_at, duration, open_factor))

    segments = []
    cursor = 0.0
    for start, end, factor in fast:
        if start > cursor:
            segments.append({"start": cursor, "end": start, "factor": 1.0})
        segments.append({"start": start, "end": end, "factor": factor})
        cursor = end
    if cursor < duration:
        segments.append({"start": cursor, "end": duration, "factor": 1.0})
    return segments


def remap(t, segments):
    """Instant de la source -> instant dans la vidéo montée."""
    out = 0.0
    for s in segments:
        if t <= s["start"]:
            return out
        span = min(t, s["end"]) - s["start"]
        out += span / s["factor"]
        if t <= s["end"]:
            return out
    return out


def output_duration(segments):
    return sum((s["end"] - s["start"]) / s["factor"] for s in segments)


def fast_spans(segments):
    """Intervalles accélérés, exprimés dans la vidéo montée (pour le repère
    « ×8 » affiché à l'écran)."""
    spans = []
    for s in segments:
        if s["factor"] > 1:
            spans.append({
                "start": remap(s["start"], segments),
                "end": remap(s["end"], segments),
                "factor": s["factor"],
            })
    return spans


def build_captions(cues, segments, texts, min_hold=1.8, max_hold=8.0):
    """Repères de parole -> sous-titres datés dans la vidéo montée.

    Un sous-titre tient jusqu'au suivant, borné par `min_hold`/`max_hold`.
    Une clé absente du fichier de textes est une erreur : mieux vaut casser
    la fabrication qu'expédier une vidéo avec un trou.
    """
    says = [c for c in sorted(cues, key=lambda c: c["t"]) if c["kind"] == SAY]
    missing = [c["key"] for c in says if c["key"] not in texts]
    if missing:
        raise KeyError("textes manquants : %s" % ", ".join(sorted(set(missing))))
    end_of_film = output_duration(segments)
    out = []
    for i, c in enumerate(says):
        start = remap(c["t"], segments)
        nxt = remap(says[i + 1]["t"], segments) if i + 1 < len(says) else end_of_film
        end = min(max(start + min_hold, min(nxt, start + max_hold)), end_of_film)
        if end <= start:
            continue
        out.append({"start": start, "end": end, "text": texts[c["key"]]})
    return out


def preview_window(cues, segments, length=8.0):
    """Fenêtre à extraire pour l'aperçu animé du README."""
    for c in sorted(cues, key=lambda c: c["t"]):
        if c["kind"] == PREVIEW:
            start = remap(c["t"], segments)
            break
    else:
        start = 0.0
    total = output_duration(segments)
    return {"start": max(0.0, min(start, max(0.0, total - length))),
            "length": min(length, total)}


def _ass_time(t):
    t = max(0.0, t)
    h, rest = divmod(t, 3600)
    m, s = divmod(rest, 60)
    return "%d:%02d:%05.2f" % (int(h), int(m), s)


def _wrap(text, width=62):
    return "\\N".join(textwrap.wrap(text, width=width)) or text


def ass_document(captions, spans=(), width=1920, height=1080,
                 font="Open Sans", size=44, offset=0.0):
    """Fichier ASS : les sous-titres en bas, le repère d'accélération en bas
    à droite, au-dessus du dock (en haut il couvrait la barre d'outils). `offset` décale tout (carton de titre ajouté devant).

    `BorderStyle: 3` dessine un cartouche derrière le texte, et ce cartouche
    prend la couleur de CONTOUR (pas celle de fond) : sans épaisseur de
    contour, les sous-titres se perdent sur l'interface claire.
    """
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{size},&H00FFFFFF,&H4D0E2B24,&H00000000,0,3,9,0,2,140,140,56,1
Style: Speed,{font},{int(size * 0.8)},&H0078BA30,&H4D0E2B24,&H00000000,-1,3,7,0,3,50,60,230,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for c in captions:
        lines.append("Dialogue: 0,%s,%s,Caption,,0,0,0,,%s"
                     % (_ass_time(c["start"] + offset), _ass_time(c["end"] + offset),
                        _wrap(c["text"])))
    for s in spans:
        lines.append("Dialogue: 1,%s,%s,Speed,,0,0,0,,x%d"
                     % (_ass_time(s["start"] + offset), _ass_time(s["end"] + offset),
                        int(s["factor"])))
    return head + "\n".join(lines) + "\n"
