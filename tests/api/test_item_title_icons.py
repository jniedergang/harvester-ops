"""v1.26.0 — l'en-tête récapitulatif d'un bloc ne doit pas cracher son SVG.

Signalé à l'usage, sur l'onglet Tags de l'éditeur de VM : chaque étiquette
s'affichait précédée de la balise `<svg class="icon icon-tag" viewBox="0 0 24
24" …>` écrite en toutes lettres.

Cause : `tf-form.js` échappait le titre ENTIER, et depuis le passage aux
icônes SVG les titres commençaient par `Icons.svg('tag')`. Onze titres
étaient touchés, donc onze sections de l'éditeur.

Le correctif ne consiste surtout pas à cesser d'échapper : ces titres
contiennent des valeurs venues de la spec de la VM. Le contrat sépare
désormais le NOM de l'icône (pioché dans le jeu vérifié) du TEXTE (échappé).
Ces tests verrouillent les deux moitiés.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
JS = ROOT / "web" / "static" / "js"


def test_no_item_title_embeds_icon_markup():
    """Un `Icons.svg(...)` dans un itemTitle repart en balise échappée."""
    offenders, seen = [], 0
    for path in JS.glob("*.js"):
        src = path.read_text()
        for m in re.finditer(r"itemTitle:\s*\([^)]*\)\s*=>([\s\S]{0,400}?)\n\s*(?:newItem|args|min|max|label)\s*:", src):
            seen += 1
            if "Icons.svg(" in m.group(1):
                line = src[:m.start()].count("\n") + 1
                offenders.append(f"{path.name}:{line}")
    # Garde-fou du test lui-même : une regexp qui ne capture plus rien
    # passerait au vert sans rien vérifier.
    declared = sum(p.read_text().count("itemTitle:") for p in JS.glob("*.js"))
    assert seen == declared and seen >= 11, \
        f"le test n'analyse que {seen} des {declared} itemTitle déclarés"
    assert not offenders, (
        "ces itemTitle renvoient du balisage d'icône, qui sera échappé et "
        "affiché en clair : " + ", ".join(offenders))


def test_the_renderer_escapes_the_text_but_not_the_icon():
    src = (JS / "tf-form.js").read_text()
    assert "function itemHeadHtml" in src, "le point de passage unique a disparu"
    head = src.split("function itemHeadHtml", 1)[1].split("\n  }", 1)[0]
    # Le texte est échappé...
    assert "esc(out.text" in head, "le texte doit rester échappé : il vient de la VM"
    # ...et l'icône n'est qu'un nom passé au jeu d'icônes.
    assert "Icons.svg(out.icon" in head, "l'icône doit venir du jeu vérifié"
    # Rien ne doit rendre le titre en brut.
    assert "innerHTML = out" not in head


def test_the_live_refresh_uses_the_same_path():
    """Le rafraîchissement à la frappe passait par `textContent`, qui
    réaffichait la balise dès la première touche même après correction du
    rendu initial."""
    src = (JS / "tf-form.js").read_text()
    assert "head.textContent = ndef.itemTitle" not in src
    assert "head.innerHTML = itemHeadHtml(" in src


def test_every_icon_named_by_an_item_title_exists():
    """Un nom d'icône inconnu rendrait une chaîne vide : l'en-tête perdrait
    son icône sans que rien ne le signale."""
    icons_src = (JS / "icons.js").read_text()
    known = set(re.findall(r"^\s{2,6}([a-zA-Z_][a-zA-Z0-9_]*)\s*:", icons_src,
                           re.MULTILINE))
    assert len(known) > 30, "extraction du jeu d'icônes cassée"
    used = set()
    for path in JS.glob("*.js"):
        src = path.read_text()
        for m in re.finditer(r"itemTitle:\s*\([^)]*\)\s*=>\s*\(\{([\s\S]{0,300}?)\}\)", src):
            used.update(re.findall(r"icon:\s*(?:[^,{}]*\?\s*)?'([\w-]+)'", m.group(1)))
            used.update(re.findall(r":\s*'([\w-]+)',\s*text:", m.group(1)))
    assert used, "aucun itemTitle structuré trouvé"
    unknown = sorted(n for n in used if n not in known)
    assert not unknown, f"icônes inconnues dans un itemTitle : {unknown}"
