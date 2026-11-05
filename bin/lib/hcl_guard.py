"""Garde du HCL brut des modèles de déclaration (2.4.0, revue B2-I4 et B2-I5).

Garde contre les ACCIDENTS, pas frontière de sécurité : les modèles sont
écrits par un administrateur, et la règle refuse ce qui, dans un modèle
relu, ferait entrer un secret dans l'état Terraform (affectation d'un nom de
secret, cloud-init qui porte un jeton, fichier de l'hôte lu par `file()`) ou
exécuterait autre chose que des ressources (provisioner, data, module,
provider, backend).

Deux services :

* `refusal(text)` : raison du refus d'un HCL brut, None s'il passe ;
* `render(text, values)` : variables `{{x}}` remplacées selon leur place.
  Dans une chaîne ou un heredoc, la valeur est échappée (guillemet, barre
  oblique inverse, `${`, `%{`) et arrive comme texte ; hors chaîne, seuls un
  nombre et true/false passent (une valeur ne change jamais la structure du
  modèle relu).

Un petit analyseur lexical distingue le code, les chaînes, les heredocs et
les commentaires (interpolations `${...}` d'une chaîne comptées comme code).
Bibliothèque standard seulement.
"""

import re

# noms de secrets : affectation (`=` ou `:`), clé entre guillemets, clé YAML
# ou variable d'environnement dans un heredoc
SECRET_NAME_RE = re.compile(
    r"password|passwd|secret|token|credential|private|(?:^|[_-])pass(?:$|[_-])|pwd|api[_-]?key"
    r"|(?:^|[_-])auth(?:orization)?(?:$|[_-])|bearer|_base64$", re.I)
# noms qui désignent un objet public ou le NOM d'un Secret (décision 150)
PUBLIC_NAMES = frozenset(("public_key", "ssh_keys"))
SECRET_NAME_OK_RE = re.compile(r"secret[_-]name(space)?$", re.I)
PASSWORD_TEXT_RE = re.compile(r"password|passwd|chpasswd|\bbearer\s+\S", re.I)
# fonctions qui lisent l'hôte de la console ou cachent une valeur
FUNCTIONS_RE = re.compile(
    r"\b(file|fileexists|fileset|filebase64|filebase64sha256|filebase64sha512|filemd5|filesha1"
    r"|filesha256|filesha512|templatefile|base64decode|textdecodebase64|sensitive|nonsensitive)\s*\(")
# blocs qui exécutent, lisent ou configurent autre chose que des ressources
BLOCKS_RE = re.compile(
    r"\b(provisioner|connection|variable|locals|output|module|provider|terraform|data|import"
    r"|moved|removed|check)\s*[\"{]")
_IDENT_RE = re.compile(r"[A-Za-z_][\w-]*")
_ASSIGN_HEREDOC_RE = re.compile(r"\b([A-Za-z_][\w-]*)\s*[=:](?![=>])")
_BARE_VALUE_RE = re.compile(r"-?\d+(\.\d+)?|true|false")
_HEREDOC_RE = re.compile(r"<<(-?)([A-Za-z_][\w-]*)[ \t]*\n")
_VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.-]*)\s*\}\}")

CODE, STRING, HEREDOC, COMMENT = "c", "s", "h", "m"


def _lex(text):
    """(genre de chaque caractère, chaînes [(début, fin, interpolée)]).
    Les guillemets d'une chaîne sont comptés dans la chaîne."""
    n = len(text)
    kinds = [CODE] * n
    strings = []
    # pile : ("code", profondeur d'accolades) | ("str", début, interpolée) | ("here", fin)
    stack = [["code", 0]]
    i = 0
    while i < n:
        top = stack[-1]
        c = text[i]
        if top[0] == "code":
            if c == "#" or text.startswith("//", i):
                j = text.find("\n", i)
                j = n if j < 0 else j
                kinds[i:j] = [COMMENT] * (j - i)
                i = j
                continue
            if text.startswith("/*", i):
                j = text.find("*/", i + 2)
                j = n if j < 0 else j + 2
                kinds[i:j] = [COMMENT] * (j - i)
                i = j
                continue
            if c == '"':
                kinds[i] = STRING
                stack.append(["str", i, False])
                i += 1
                continue
            m = _HEREDOC_RE.match(text, i)
            if m:
                stack.append(["here", m.group(2)])
                i = m.end()
                continue
            if c == "{":
                top[1] += 1
            elif c == "}":
                if top[1] == 0 and len(stack) > 1:
                    stack.pop()                 # fin d'une interpolation
                else:
                    top[1] = max(0, top[1] - 1)
            i += 1
            continue
        own = STRING if top[0] == "str" else HEREDOC
        if top[0] == "here" and (i == 0 or text[i - 1] == "\n"):
            j = text.find("\n", i)
            j = n if j < 0 else j
            if text[i:j].strip() == top[1]:
                stack.pop()
                i = j
                continue
        if top[0] == "str" and c == "\\":
            kinds[i:i + 2] = [own] * len(kinds[i:i + 2])
            i += 2
            continue
        if top[0] == "str" and c == '"':
            kinds[i] = STRING
            strings.append((top[1], i + 1, top[2]))
            stack.pop()
            i += 1
            continue
        if text.startswith("$${", i) or text.startswith("%%{", i):
            kinds[i:i + 3] = [own] * 3
            i += 3
            continue
        if text.startswith("${", i) or text.startswith("%{", i):
            if top[0] == "str":
                top[2] = True
            kinds[i:i + 2] = [own] * 2
            stack.append(["code", 0])
            i += 2
            continue
        kinds[i] = own
        i += 1
    return kinds, strings


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
    kinds, strings = _lex(text)
    for m in BLOCKS_RE.finditer(text):
        if kinds[m.start(1)] == CODE:
            return f"{m.group(1)} block not allowed in a template"
    for m in FUNCTIONS_RE.finditer(text):
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


def render(text, values):
    """(HCL rendu, raisons de refus [(variable, raison)]). Une variable
    absente lève KeyError(nom), comme bm_profiles.render_advanced."""
    kinds, _strings = _lex(text)
    out, refused, last = [], [], 0
    for m in _VAR_RE.finditer(text):
        name = m.group(1)
        v = values.get(name)
        if v is None or v == "":
            raise KeyError(name)
        v = str(v)
        kind = kinds[m.start()]
        if kind in (STRING, HEREDOC):
            v = _escape(v, kind)
        elif not _BARE_VALUE_RE.fullmatch(v):
            refused.append((name, "outside a string only a number or true/false is allowed"))
        out.append(text[last:m.start()])
        out.append(v)
        last = m.end()
    out.append(text[last:])
    return "".join(out), refused
