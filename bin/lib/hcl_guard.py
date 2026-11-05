"""HCL brut des modèles de déclaration : secrets et valeurs de variables
(2.4.0, revues B2-I4, B2-I5, B3-I1, B3-I3).

Ce qu'un code Terraform a le droit d'écrire (blocs, types de ressource,
fonctions qui lisent la machine de la console, provisioners) est décidé par
`web/tf_guard.py`, seule règle pour les déclarations comme pour les modèles :
ce module n'en garde aucune copie. Il ajoute ce qui est propre aux modèles :

* `refusal(text)` : raison du refus d'un HCL brut qui ferait entrer un secret
  dans l'état Terraform (affectation d'un nom de secret, cloud-init qui porte
  un mot de passe ou un jeton, fonction qui cache une valeur), None s'il
  passe. Garde contre les accidents d'un modèle écrit par un admin ;
* `render(text, values)` : variables `{{x}}` remplacées selon leur place.
  Dans une chaîne ou un heredoc, la valeur est échappée (guillemet, barre
  oblique inverse, `${`, `%{`) et arrive comme texte ; hors chaîne, seuls un
  nombre et true/false passent. Une valeur ne porte ni saut de ligne ni
  caractère de contrôle (un saut de ligne suivi du marqueur fermait le
  heredoc), et le résultat est RELU par la lecture de tf_guard : chaque
  caractère, du modèle comme des valeurs, doit garder son genre (une valeur
  `$` suivie d'une valeur `{...}` ouvrait sinon une interpolation). Une
  valeur ne change jamais la structure du modèle relu (décision 161 : un
  opérateur lance le code écrit par un admin, pas le sien).

Bibliothèque standard seulement (tf_guard l'est aussi).
"""

import re
import sys
from pathlib import Path

try:
    import tf_guard as _tfg
except ImportError:                     # appelé hors de la console (ligne de commande, tests)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "web"))
    import tf_guard as _tfg

# noms de secrets : affectation (`=` ou `:`), clé entre guillemets, clé YAML
# ou variable d'environnement dans un heredoc
SECRET_NAME_RE = re.compile(
    r"password|passwd|secret|token|credential|private|(?:^|[_-])pass(?:$|[_-])|pwd|api[_-]?key"
    r"|(?:^|[_-])auth(?:orization)?(?:$|[_-])|bearer|_base64$", re.I)
# noms qui désignent un objet public ou le NOM d'un Secret (décision 150)
PUBLIC_NAMES = frozenset(("public_key", "ssh_keys"))
SECRET_NAME_OK_RE = re.compile(r"secret[_-]name(space)?$", re.I)
PASSWORD_TEXT_RE = re.compile(r"password|passwd|chpasswd|\bbearer\s+\S", re.I)
# fonctions qui cachent une valeur au relecteur (les fonctions qui lisent la
# machine sont refusées par tf_guard, pour tout code)
SECRET_FUNCTIONS_RE = re.compile(r"\b(base64decode|textdecodebase64|sensitive|nonsensitive)\s*\(")
_IDENT_RE = re.compile(r"[A-Za-z_][\w-]*")
_ASSIGN_HEREDOC_RE = re.compile(r"\b([A-Za-z_][\w-]*)\s*[=:](?![=>])")
_BARE_VALUE_RE = re.compile(r"-?\d+(\.\d+)?|true|false")
_VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.-]*)\s*\}\}")

CODE, STRING, HEREDOC, COMMENT = _tfg.CODE, _tfg.STRING, _tfg.HEREDOC, _tfg.COMMENT


def _decode_key(raw):
    """Contenu d'une clé entre guillemets, échappements HCL décodés."""
    def esc(m):
        s = m.group(0)
        if s[1] in "uU":
            try:
                return chr(int(s[2:], 16))
            except ValueError:
                return s
        return {"n": "\n", "t": "\t", "r": "\r"}.get(s[1], s[1])
    return re.sub(r"\\(u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|.)", esc, raw)


def _secret_name(name):
    return bool(SECRET_NAME_RE.search(name)) and name not in PUBLIC_NAMES \
        and not SECRET_NAME_OK_RE.search(name)


def refusal(text):
    """Raison du refus d'un HCL brut de modèle, None s'il passe."""
    if not isinstance(text, str):
        return None
    if PASSWORD_TEXT_RE.search(text):
        return "secret-looking value"
    lexed = _tfg.char_kinds(text)
    if lexed is None:
        return None                     # illisible : tf_guard le refuse (structure)
    kinds, strings = lexed
    for m in SECRET_FUNCTIONS_RE.finditer(text):
        if kinds[m.start(1)] == CODE:
            return f"function {m.group(1)}() not allowed in a template"
    ends = {e: (s, interp) for s, e, interp in strings}
    n = len(text)
    for i, c in enumerate(text):
        if kinds[i] != CODE or c not in "=:":
            continue
        nxt = text[i + 1] if i + 1 < n else ""
        prev = text[i - 1] if i else ""
        if nxt in "=>" or prev in "=!<>:" or (c == ":" and nxt == ":"):
            continue
        j = i - 1
        while j >= 0 and text[j] in " \t":
            j -= 1
        if j < 0:
            continue
        if text[j] == '"' and (j + 1) in ends:
            start, interp = ends[j + 1]
            if interp:
                return "computed key not allowed in a template"
            if _secret_name(_decode_key(text[start + 1:j])):
                return "secret-looking value"
        elif text[j] == ")" and c == "=":
            return "computed key not allowed in a template"
        else:
            k = j
            while k >= 0 and (text[k].isalnum() or text[k] in "_-"):
                k -= 1
            name = text[k + 1:j + 1]
            if name and _IDENT_RE.fullmatch(name) and (k < 0 or text[k] != ".") and _secret_name(name):
                return "secret-looking value"
    # heredocs : clés YAML et variables d'environnement d'un cloud-init
    for m in _ASSIGN_HEREDOC_RE.finditer(text):
        if kinds[m.start(1)] == HEREDOC and _secret_name(m.group(1)):
            return "secret-looking value"
    return None


def _escape(value, kind):
    if kind == STRING:
        value = value.replace("\\", "\\\\").replace('"', '\\"')
    return value.replace("${", "$${").replace("%{", "%%{")


STRUCTURE_CHANGED = "the value changes the structure of the template"


def _structure_changed(text, kinds, result, spans):
    """Variable dont la valeur change la structure à la relecture du
    résultat (revue B3-I1), None sinon. `spans` : [(position dans le
    résultat, longueur, genre attendu, variable)] des valeurs ; le texte du
    modèle entre elles doit garder les genres de sa lecture d'origine."""
    relexed = _tfg.char_kinds(result)
    names = [sp[3] for sp in spans]
    if relexed is None:
        return ", ".join(dict.fromkeys(names)) or "?"
    rk = relexed[0]
    src, at = 0, 0
    for (pos, length, kind, name), m in zip(spans, _VAR_RE.finditer(text)):
        if rk[at:pos] != kinds[src:m.start()]:         # texte du modèle avant la valeur
            return name
        if any(k != kind for k in rk[pos:pos + length]):
            return name
        src, at = m.end(), pos + length
    if rk[at:] != kinds[src:]:
        return names[-1] if names else "?"
    return None


def render(text, values):
    """(HCL rendu, raisons de refus [(variable, raison)]). Une variable
    absente lève KeyError(nom), comme bm_profiles.render_advanced. Refus :
    caractère de contrôle, valeur nue hors chaîne, ou structure changée à la
    relecture du résultat (revue B3-I1)."""
    lexed = _tfg.char_kinds(text)
    if lexed is None:
        return text, [("", "the template cannot be read")]
    kinds = lexed[0]
    out, refused, spans, last, pos = [], [], [], 0, 0
    for m in _VAR_RE.finditer(text):
        name = m.group(1)
        v = values.get(name)
        if v is None or v == "":
            raise KeyError(name)
        v = str(v)
        kind = kinds[m.start()]
        if any(ord(c) < 32 or ord(c) == 127 for c in v):
            refused.append((name, "control character or line break"))
        elif kind in (STRING, HEREDOC):
            v = _escape(v, kind)
        elif not _BARE_VALUE_RE.fullmatch(v):
            refused.append((name, "outside a string only a number or true/false is allowed"))
        out.append(text[last:m.start()])
        pos += m.start() - last
        spans.append((pos, len(v), kind, name))
        out.append(v)
        pos += len(v)
        last = m.end()
    out.append(text[last:])
    result = "".join(out)
    if not refused:
        bad = _structure_changed(text, kinds, result, spans)
        if bad:
            refused.append((bad, STRUCTURE_CHANGED))
    return result, refused
