"""harvester-ops : ce qu'une déclaration Terraform a le droit d'écrire (2.3.1).

OpenTofu exécute sur la machine qui le lance plus que des appels d'API : un
`provisioner "local-exec"` lance une commande, `data "external"` un
programme, un bloc `module` télécharge du code, un `backend` écrit où il
veut, `file()` ou `templatefile()` lisent n'importe quel fichier (le
kubeconfig de la console, la clé SSH d'un hôte KVM) et le provider Harvester
téléverse un fichier local (`harvester_image.file_path`) ou écrit un
kubeconfig (`harvester_bootstrap`). Une déclaration se lance au rôle
opérateur : sans contrôle, un opérateur commandait la machine de la console.

Ce module lit le code AVANT toute commande OpenTofu et refuse tout ce qui
sort de la liste permise. Il ne s'appuie pas sur OpenTofu (qui ne sait
montrer la configuration qu'après `init`, c'est-à-dire trop tard) : un
analyseur prudent qui connaît les commentaires, les chaînes et leurs
séquences d'échappement, les gabarits `${...}` et `%{...}` (imbriqués), les
heredocs, et qui refuse ce qu'il ne sait pas lire.

Liste permise, pour le code d'une ressource :
- blocs de premier niveau `resource`, `data`, `locals`, `variable`, `output` ;
- types de ressource et de source de données du provider de la cible
  (harvester ou libvirt), liste fermée ci-dessous ;
- aucun bloc `provisioner` ni `connection`, à aucune profondeur ;
- aucune fonction qui lit un fichier ou évalue un gabarit (`file*`,
  `template*`, `pathexpand`), aucune fonction de provider (`provider::`).

Le fichier `_providers.tf` que la console écrit suit sa propre règle :
`terraform { required_providers }` avec les deux sources embarquées et le
bloc `provider` attendu, rien d'autre (ni `backend`, ni `cloud`).

Ce module ne connaît ni Flask ni Terraform : il est essayé seul.
"""

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Listes permises
# ---------------------------------------------------------------------------
# Lues dans la source du provider (internal/provider/provider.go,
# ResourcesMap et DataSourcesMap). `harvester_bootstrap` n'y est pas : il
# écrit un kubeconfig sur la machine qui lance OpenTofu (~/.kube/config par
# défaut).
HARVESTER_TYPES = frozenset({
    "harvester_cloudinit_secret", "harvester_clusternetwork", "harvester_ippool",
    "harvester_image", "harvester_ssh_key", "harvester_loadbalancer", "harvester_network",
    "harvester_pcidevice", "harvester_sriov_network_device", "harvester_schedule_backup",
    "harvester_setting", "harvester_storageclass", "harvester_vlanconfig",
    "harvester_virtualmachine", "harvester_volume",
})
# Les seuls types que produit le formulaire KVM (pas de code libre sur KVM).
LIBVIRT_TYPES = frozenset({"libvirt_domain", "libvirt_volume", "libvirt_cloudinit_disk",
                           "libvirt_network", "libvirt_pool"})
TYPES_OF = {
    "harvester": {"resource": HARVESTER_TYPES, "data": HARVESTER_TYPES},
    "kvm": {"resource": LIBVIRT_TYPES, "data": frozenset()},
}
TOP_BLOCKS = ("resource", "data", "locals", "variable", "output")
NESTED_FORBIDDEN = ("provisioner", "connection")
# Attributs qui lisent un fichier de la machine de la console.
ATTRS_FORBIDDEN = {
    ("harvester_image", "file_path"): "uploads a file of the console host",
}
PROVIDER_SOURCES = {"harvester": "harvester/harvester", "libvirt": "dmacvicar/libvirt"}
_REGISTRY_HOSTS = ("registry.terraform.io/", "registry.opentofu.org/")
# Seules fonctions du bloc provider de l'en-tête KVM (URI du script ssh).
HEADER_FUNCTIONS = frozenset({"urlencode", "abspath"})
HARVESTER_KUBECONFIG = '"${path.module}/kubeconfig"'
LIBVIRT_URI_RE = re.compile(
    r'^"qemu\+sshcmd://[A-Za-z0-9_.@:\[\]-]+/(?:system|session)'
    r'\?command=\$\{urlencode\("\$\{abspath\(path\.module\)\}/kvm_ssh"\)\}"$')
# Fichiers qu'OpenTofu chargerait depuis l'espace et que la console n'écrit
# jamais : leur seule présence refuse l'exécution.
FOREIGN_SUFFIXES = (".tf.json", ".tofu", ".tofu.json", ".tfvars", ".tfvars.json")
HEADER_FILE = "_providers.tf"


def is_forbidden_function(name):
    """Une fonction qui lit un fichier de la machine ou évalue un gabarit
    (templatestring rend des fonctions accessibles depuis une chaîne
    construite à l'exécution)."""
    n = name.lower()
    return n.startswith("file") or n.startswith("template") or n == "pathexpand"


class Violation:
    """Une construction refusée : sa nature, sa ligne, le pourquoi."""

    __slots__ = ("construct", "line", "message", "file")

    def __init__(self, construct, line, message, file=""):
        self.construct, self.line, self.message, self.file = construct, line, message, file

    def as_dict(self):
        return {"construct": self.construct, "line": self.line, "file": self.file,
                "error": str(self)}

    def __str__(self):
        where = f"{self.file}, line {self.line}" if self.file else f"line {self.line}"
        return f"{where}: {self.construct}: {self.message}"

    def __repr__(self):
        return f"Violation({str(self)!r})"


class _Syntax(Exception):
    def __init__(self, line, msg):
        super().__init__(msg)
        self.line, self.msg = line, msg


# ---------------------------------------------------------------------------
# Lecture du code
# ---------------------------------------------------------------------------
class _Tok:
    __slots__ = ("kind", "val", "line", "start", "end", "tmpl", "inner")

    def __init__(self, kind, val, line, start, end, tmpl=False, inner=None):
        self.kind, self.val, self.line, self.start, self.end = kind, val, line, start, end
        self.tmpl, self.inner = tmpl, inner or []

    def is_p(self, v):
        return self.kind == "p" and self.val == v


_PUNCT3 = ("...",)
_PUNCT2 = ("::", "=>", "==", "!=", "<=", ">=", "&&", "||")
_PUNCT1 = "{}[]()=,.:?!+-*/%<>~"
_ESC = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
_NUM_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


def _id_start(ch):
    return ch == "_" or ch.isalpha()


def _id_char(ch):
    return ch in "_-" or ch.isalnum()


class _Lexer:
    """Jetons du code natif HCL. Chaque séquence de gabarit `${...}` ou
    `%{...}` est relue comme une expression ; ses jetons sont rangés à part
    (`streams`) pour le contrôle des fonctions et des mots réservés."""

    def __init__(self, text):
        self.s, self.i, self.line = text, 0, 1
        self.streams = []
        # 2.4.0 : (début, fin, genre) des commentaires, chaînes, corps de
        # heredoc et séquences de gabarit, pour char_kinds
        self.marks = []

    def err(self, msg):
        raise _Syntax(self.line, msg)

    def tokens(self, close=False):
        """Jetons jusqu'à la fin, ou jusqu'à l'accolade qui ferme un gabarit
        (`close`), consommée."""
        s, out, depth = self.s, [], 0
        while True:
            if self.i >= len(s):
                if close:
                    self.err("unterminated template sequence")
                out.append(_Tok("eof", None, self.line, self.i, self.i))
                return out
            ch = s[self.i]
            if ch in " \t" or (ch == "\r" and s.startswith("\r\n", self.i)) \
                    or (ch == "\ufeff" and self.i == 0):
                # un retour chariot seul, un saut de page ou un séparateur
                # Unicode : refusés (une ligne de plus pour l'un, pas pour
                # l'autre, et un bloc se cacherait dans une expression)
                self.i += 1
            elif ch == "\n":
                out.append(_Tok("nl", None, self.line, self.i, self.i + 1))
                self.i += 1
                self.line += 1
            elif ch == "#" or s.startswith("//", self.i):
                j = s.find("\n", self.i)
                j = len(s) if j < 0 else j
                self.marks.append((self.i, j, COMMENT))
                self.i = j
            elif s.startswith("/*", self.i):
                j = s.find("*/", self.i + 2)
                if j < 0:
                    self.err("unterminated comment")
                n = s.count("\n", self.i, j)
                start = self.i
                self.i = j + 2
                self.marks.append((start, self.i, COMMENT))
                if n:
                    # un commentaire sur plusieurs lignes coupe l'expression
                    # (lecture prudente : il ne peut rien cacher dedans)
                    out.append(_Tok("nl", None, self.line, start, self.i))
                    self.line += n
            elif ch == '"':
                out.append(self._quoted())
            elif s.startswith("<<", self.i) and self._heredoc_ahead():
                out.append(self._heredoc())
            elif ch.isdigit():
                m = _NUM_RE.match(s, self.i)
                out.append(_Tok("num", m.group(0), self.line, self.i, m.end()))
                self.i = m.end()
            elif _id_start(ch):
                j = self.i + 1
                while j < len(s) and _id_char(s[j]):
                    j += 1
                out.append(_Tok("id", s[self.i:j], self.line, self.i, j))
                self.i = j
            else:
                p = next((p for p in _PUNCT3 + _PUNCT2 if s.startswith(p, self.i)), None)
                if p is None:
                    if ch not in _PUNCT1:
                        self.err(f"unexpected character {ch!r}")
                    p = ch
                if close and p == "}":
                    if depth == 0:
                        self.i += 1
                        return out
                    depth -= 1
                elif close and p == "{":
                    depth += 1
                out.append(_Tok("p", p, self.line, self.i, self.i + len(p)))
                self.i += len(p)

    def _template_seq(self):
        """`${` ou `%{` à self.i : relit l'expression jusqu'à son accolade."""
        start = self.i
        self.i += 2
        inner = self.tokens(close=True)
        self.marks.append((start, self.i, CODE))
        inner.append(_Tok("eof", None, self.line, self.i, self.i))
        self.streams.append(inner)

    def _quoted(self):
        s, start, line = self.s, self.i, self.line
        self.i += 1
        buf, tmpl = [], False
        while True:
            if self.i >= len(s):
                raise _Syntax(line, "unterminated string")
            ch = s[self.i]
            if ch == '"':
                self.i += 1
                self.marks.append((start, self.i, STRING))
                return _Tok("str", None if tmpl else "".join(buf), line, start, self.i, tmpl=tmpl)
            if ch == "\n":
                self.err("newline in a quoted string")
            if ch == "\\":
                nxt = s[self.i + 1:self.i + 2]
                if nxt in _ESC:
                    buf.append(_ESC[nxt])
                    self.i += 2
                elif nxt in ("u", "U"):
                    n = 4 if nxt == "u" else 8
                    hexa = s[self.i + 2:self.i + 2 + n]
                    if len(hexa) != n or not all(c in "0123456789abcdefABCDEF" for c in hexa):
                        self.err("invalid escape sequence")
                    try:
                        buf.append(chr(int(hexa, 16)))
                    except (ValueError, OverflowError):
                        self.err("invalid escape sequence")
                    self.i += 2 + n
                else:
                    self.err("invalid escape sequence")
                continue
            if s.startswith("$${", self.i) or s.startswith("%%{", self.i):
                buf.append(s[self.i + 1:self.i + 3])
                self.i += 3
                continue
            if s.startswith("${", self.i) or s.startswith("%{", self.i):
                tmpl = True
                self._template_seq()
                continue
            buf.append(ch)
            self.i += 1

    def _heredoc_ahead(self):
        j = self.i + 2
        if j < len(self.s) and self.s[j] == "-":
            j += 1
        return j < len(self.s) and _id_start(self.s[j])

    def _heredoc(self):
        s, start, line = self.s, self.i, self.line
        j = self.i + 2
        if s[j] == "-":
            j += 1
        k = j
        while k < len(s) and _id_char(s[k]):
            k += 1
        delim = s[j:k]
        if s.startswith("\r\n", k):
            k += 2
        elif s.startswith("\n", k):
            k += 1
        else:
            self.err("a heredoc marker must end its line")
        self.i = k
        body = k
        self.line += 1
        tmpl = False
        at_line_start = True
        while True:
            if self.i >= len(s):
                raise _Syntax(line, f"unterminated heredoc {delim}")
            if at_line_start:
                e = s.find("\n", self.i)
                e = len(s) if e < 0 else e
                # fin la plus précoce possible (blancs tolérés autour du
                # marqueur) : la lecture ne peut pas croire « dans la chaîne »
                # ce qu'OpenTofu lirait comme du code
                if s[self.i:e].strip() == delim:
                    self.marks.append((body, self.i, HEREDOC))
                    self.i = e
                    return _Tok("str", None, line, start, self.i, tmpl=True)
                at_line_start = False
            ch = s[self.i]
            if ch == "\n":
                self.i += 1
                self.line += 1
                at_line_start = True
                continue
            if s.startswith("$${", self.i) or s.startswith("%%{", self.i):
                self.i += 3
                continue
            if s.startswith("${", self.i) or s.startswith("%{", self.i):
                tmpl = True
                self._template_seq()
                continue
            self.i += 1


# ---------------------------------------------------------------------------
# Structure : blocs, attributs, expressions
# ---------------------------------------------------------------------------
class _Block:
    __slots__ = ("type", "labels", "line", "attrs", "blocks", "label_toks")

    def __init__(self, btype, labels, line, label_toks):
        self.type, self.labels, self.line, self.label_toks = btype, labels, line, label_toks
        self.attrs, self.blocks = [], []     # attrs : (nom, ligne, jetons de la valeur)


def _skip_nl(toks, pos):
    while toks[pos].kind == "nl":
        pos += 1
    return pos


def _expr(toks, pos):
    """Fin d'une expression d'attribut : la fin de ligne hors parenthèses,
    crochets et accolades, ou l'accolade qui ferme le bloc (bloc d'une ligne)."""
    depth, start = 0, pos
    while True:
        t = toks[pos]
        if t.kind == "eof":
            if depth:
                raise _Syntax(t.line, "unclosed bracket in an expression")
            break
        if depth == 0 and (t.kind == "nl" or t.is_p("}")):
            break
        if t.kind == "p" and t.val in "([{":
            depth += 1
        elif t.kind == "p" and t.val in ")]}":
            depth -= 1
        pos += 1
    if pos == start:
        raise _Syntax(toks[start].line, "an attribute needs a value")
    return pos


def _body(toks, pos, depth, into):
    """Lit un corps (le fichier, ou l'intérieur d'un bloc) ; rend la position
    après l'accolade fermante."""
    while True:
        pos = _skip_nl(toks, pos)
        t = toks[pos]
        if t.kind == "eof":
            if depth:
                raise _Syntax(t.line, "unclosed block")
            return pos
        if t.is_p("}"):
            if not depth:
                raise _Syntax(t.line, "unexpected }")
            return pos + 1
        if t.kind != "id":
            raise _Syntax(t.line, f"unexpected {t.val or t.kind!r}: a block or an attribute was expected")
        pos += 1
        if toks[pos].is_p("="):
            end = _expr(toks, pos + 1)
            into.attrs.append((t.val, t.line, toks[pos + 1:end]))
            pos = end
        else:
            labels, ltoks = [], []
            while toks[pos].kind in ("str", "id"):
                lt = toks[pos]
                if lt.kind == "str" and lt.tmpl:
                    raise _Syntax(lt.line, "a block label cannot hold a template")
                labels.append(lt.val)
                ltoks.append(lt)
                pos += 1
            if not toks[pos].is_p("{"):
                raise _Syntax(toks[pos].line, f"block {t.val}: '{{' expected")
            blk = _Block(t.val, labels, t.line, ltoks)
            into.blocks.append(blk)
            pos = _body(toks, pos + 1, depth + 1, blk)
        nxt = toks[pos]
        if not (nxt.kind in ("nl", "eof") or nxt.is_p("}")):
            raise _Syntax(nxt.line, "a newline is expected after an attribute or a block")


CODE, STRING, HEREDOC, COMMENT = "c", "s", "h", "m"


def char_kinds(text):
    """(genre de chaque caractère, chaînes [(début, fin, interpolée)]) selon
    la lecture de ce module, ou None si le texte est illisible. Genres :
    CODE (y compris l'intérieur d'un `${...}`), STRING (guillemets compris),
    HEREDOC (corps), COMMENT. Sert aux modèles de plan (bin/lib/hcl_guard) :
    placer une valeur selon sa place, et vérifier qu'après substitution
    chaque caractère garde son genre (une valeur ne change pas la structure)."""
    lx = _Lexer(text or "")
    try:
        lx.tokens()
    except _Syntax:
        return None
    kinds = [CODE] * len(text or "")
    strings = []
    # intervalles imbriqués : le plus extérieur d'abord, l'intérieur repeint
    for start, end, kind in sorted(lx.marks, key=lambda m: (m[0], -m[1])):
        kinds[start:end] = [kind] * (end - start)
        if kind == STRING:
            strings.append((start, end, any(m[2] == CODE and start < m[0] < end for m in lx.marks)))
    return kinds, sorted(strings)


def _parse(text):
    lx = _Lexer(text)
    toks = lx.tokens()
    root = _Block("", [], 0, [])
    _body(toks, 0, 0, root)
    return root, [toks] + lx.streams


def _calls(stream):
    """(nom, espace de noms, ligne) de chaque appel de fonction d'un flux."""
    out = []
    for i, t in enumerate(stream):
        if t.kind != "id" or (i and stream[i - 1].is_p("::")):
            continue
        if i and stream[i - 1].is_p("."):
            continue
        chain, j = [t.val], i + 1
        while stream[j].is_p("::") and stream[j + 1].kind == "id":
            chain.append(stream[j + 1].val)
            j += 2
        if stream[j].is_p("("):
            out.append((chain[-1], chain[:-1], t.line))
    return out


def _keywords(streams, file):
    """Lecture de secours, indépendante de la structure : un mot
    `provisioner` ou `connection` hors chaîne, où qu'il soit."""
    out = []
    for stream in streams:
        for i, t in enumerate(stream):
            if t.kind == "id" and t.val in NESTED_FORBIDDEN and not (i and stream[i - 1].is_p(".")):
                out.append(Violation(f"{t.val} block", t.line,
                                     "provisioners run commands on the console host", file))
    return out


def _functions(streams, file, allowed=None):
    out = []
    for stream in streams:
        for name, ns, line in _calls(stream):
            shown = "::".join(ns + [name])
            if ns and ns != ["core"]:
                out.append(Violation(f"function {shown}()", line,
                                     "provider functions are not allowed", file))
            elif allowed is not None and name not in allowed:
                out.append(Violation(f"function {shown}()", line,
                                     "not allowed in the provider configuration", file))
            elif is_forbidden_function(name):
                out.append(Violation(f"function {shown}()", line,
                                     "it reads files of the console host or evaluates a template", file))
    return out


def _dedup(vs):
    seen, out = set(), []
    for v in sorted(vs, key=lambda v: (v.line, v.construct)):
        head = v.construct.split()[0]
        k = (v.line, head if head in NESTED_FORBIDDEN else v.construct)
        if k not in seen:
            seen.add(k)
            out.append(v)
    return out


def _syntax_violation(e, file):
    return Violation("unreadable code", e.line,
                     f"{e.msg}; the console only runs code it can read", file)


def _src(text, toks):
    return text[toks[0].start:toks[-1].end].strip() if toks else ""


def _walk_nested(blk, file, rtype, out, top=True):
    for name, line, vtoks in blk.attrs:
        if top and (rtype, name) in ATTRS_FORBIDDEN:
            out.append(Violation(f"{rtype}.{name}", line, ATTRS_FORBIDDEN[(rtype, name)], file))
        if rtype and rtype.startswith("libvirt_"):
            # le seul contenu de volume permis : le disque cloud-init produit
            # par la déclaration (jamais un chemin local de la console ni une
            # adresse). Attribut de bloc (`url = ...`) ou clé d'objet
            # (`content = { url = ... }`, provider 0.9).
            vals = [vtoks] if name == "url" else []
            vals += [vtoks[i + 2:] for i in range(len(vtoks) - 2)
                     if vtoks[i].kind in ("id", "str") and vtoks[i].val == "url"
                     and vtoks[i + 1].kind == "p" and vtoks[i + 1].val in ("=", ":")]
            for v in vals:
                if not (v and v[0].kind == "id" and v[0].val == "libvirt_cloudinit_disk"):
                    out.append(Violation(f"{rtype} url", line,
                                         "a volume can only take the cloud-init disk of the declaration", file))
                    break
    for sub in blk.blocks:
        if sub.type in NESTED_FORBIDDEN:
            out.append(Violation(" ".join([sub.type] + [f'"{lb}"' for lb in sub.labels]), sub.line,
                                 "provisioners run commands on the console host", file))
        if sub.type == "dynamic" and sub.labels and sub.labels[0] in NESTED_FORBIDDEN:
            out.append(Violation(f'dynamic "{sub.labels[0]}"', sub.line,
                                 "provisioners run commands on the console host", file))
        _walk_nested(sub, file, rtype, out, top=False)


def _type_hint(target_kind, btype):
    if target_kind == "kvm":
        if btype == "data":
            return "data sources are not allowed for a KVM target"
        return "not allowed for a KVM target (allowed: " + ", ".join(sorted(LIBVIRT_TYPES)) + ")"
    return ("not allowed: only the types of the embedded providers are allowed "
            "(harvester_*, except harvester_bootstrap)")


def check_resources(text, target_kind="harvester", file=""):
    """Violations du code d'une ressource de déclaration (formulaire ou code
    libre), pour une cible `harvester` ou `kvm` ; liste vide = permis."""
    try:
        root, streams = _parse(text or "")
    except _Syntax as e:
        return [_syntax_violation(e, file)]
    types = TYPES_OF.get(target_kind) or TYPES_OF["harvester"]
    if target_kind == "any":
        types = {"resource": HARVESTER_TYPES | LIBVIRT_TYPES, "data": HARVESTER_TYPES}
    out = []
    if root.attrs:
        name, line, _ = root.attrs[0]
        out.append(Violation(f"attribute {name}", line, "top-level attributes are not allowed", file))
    for blk in root.blocks:
        if blk.type not in TOP_BLOCKS:
            why = {"terraform": "backend, cloud and provider settings belong to the console",
                   "provider": "the console configures the provider itself",
                   "module": "a module fetches and runs code"}.get(
                       blk.type, "allowed: " + ", ".join(TOP_BLOCKS))
            out.append(Violation(f"{blk.type} block", blk.line, why, file))
            continue
        rtype = None
        if blk.type in ("resource", "data"):
            if len(blk.labels) != 2:
                out.append(Violation(f"{blk.type} block", blk.line, "needs a type and a name", file))
                continue
            rtype = blk.labels[0]
            if rtype not in types[blk.type]:
                out.append(Violation(f'{blk.type} "{rtype}"', blk.line, _type_hint(target_kind, blk.type), file))
        _walk_nested(blk, file, rtype, out)
    out += _keywords(streams, file)
    out += _functions(streams, file)
    return _dedup(out)


def resource_types(text):
    """Types des blocs `resource` du code (2.4.0 : rôle exigé par ce qu'un
    code libre crée) ; vide si le code est illisible (check_resources le
    refuse alors)."""
    try:
        root, _streams = _parse(text or "")
    except _Syntax:
        return []
    return sorted({b.labels[0] for b in root.blocks if b.type == "resource" and b.labels})


def check_header(text, file=HEADER_FILE):
    """Violations du fichier `_providers.tf` : `terraform { required_providers }`
    avec les sources embarquées et le bloc provider attendu, rien d'autre."""
    try:
        root, streams = _parse(text or "")
    except _Syntax as e:
        return [_syntax_violation(e, file)]
    out = []
    for name, line, _ in root.attrs:
        out.append(Violation(f"attribute {name}", line, "top-level attributes are not allowed", file))
    for blk in root.blocks:
        if blk.type == "terraform":
            for name, line, _ in blk.attrs:
                if name != "required_version":
                    out.append(Violation(f"terraform.{name}", line, "not allowed", file))
            for sub in blk.blocks:
                if sub.type != "required_providers":
                    out.append(Violation(f"{sub.type} block", sub.line,
                                         "backend, cloud and other settings are not allowed", file))
                    continue
                for pname, line, vtoks in sub.attrs:
                    src = _provider_source(vtoks)
                    want = PROVIDER_SOURCES.get(pname)
                    if want is None or src not in (want, *(h + want for h in _REGISTRY_HOSTS)):
                        out.append(Violation(f"provider {pname} source {src or '?'}", line,
                                             "only the embedded providers are allowed: "
                                             + ", ".join(PROVIDER_SOURCES.values()), file))
                for b in sub.blocks:
                    out.append(Violation(f"{b.type} block", b.line, "not allowed", file))
        elif blk.type == "provider":
            pname = blk.labels[0] if len(blk.labels) == 1 else None
            if pname not in PROVIDER_SOURCES:
                out.append(Violation(f"provider {pname or '?'}", blk.line,
                                     "only the embedded providers are allowed", file))
                continue
            if blk.blocks:
                out.append(Violation(f"{blk.blocks[0].type} block", blk.blocks[0].line, "not allowed", file))
            want = {"harvester": "kubeconfig", "libvirt": "uri"}[pname]
            for name, line, vtoks in blk.attrs:
                val = _src(text, vtoks)
                ok = name == want and (val == HARVESTER_KUBECONFIG if pname == "harvester"
                                       else bool(LIBVIRT_URI_RE.match(val)))
                if not ok:
                    out.append(Violation(f"provider {pname}.{name}", line,
                                         "differs from what the console writes", file))
        else:
            out.append(Violation(f"{blk.type} block", blk.line,
                                 "only terraform and provider blocks are allowed here", file))
    out += _keywords(streams, file)
    out += _functions(streams, file, allowed=HEADER_FUNCTIONS)
    return _dedup(out)


def _provider_source(vtoks):
    """La valeur littérale de `source` dans `{ source = "...", version = ... }`."""
    for i, t in enumerate(vtoks):
        if t.kind == "id" and t.val == "source" and i + 2 < len(vtoks) and vtoks[i + 1].is_p("=") \
                and vtoks[i + 2].kind == "str" and not vtoks[i + 2].tmpl:
            return vtoks[i + 2].val
    return None


def check_workspace(ws, known_headers=()):
    """Violations de tous les fichiers qu'OpenTofu chargerait depuis l'espace
    `ws` (dernier rempart, juste avant chaque commande, quel que soit le
    chemin qui a écrit les fichiers). `known_headers` : en-têtes que la
    console écrivait autrefois EN TÊTE de chaque fichier de ressource (espaces
    partagés d'avant la v1.5) ; contrôlés comme un en-tête."""
    ws = Path(ws)
    out = []
    try:
        entries = sorted(ws.iterdir())
    except OSError:
        return out
    for f in entries:
        name = f.name
        if name.endswith(FOREIGN_SUFFIXES):
            out.append(Violation(f"file {name}", 0, "the console never writes this kind of file", name))
            continue
        if not name.endswith(".tf"):
            continue
        if f.is_symlink() or not f.is_file():
            out.append(Violation(f"file {name}", 0, "not a regular file", name))
            continue
        try:
            with open(f, encoding="utf-8", newline="") as fh:    # tel quel : pas de \r converti
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            out.append(Violation(f"file {name}", 0, "cannot be read", name))
            continue
        if name == HEADER_FILE:
            out += check_header(text, name)
            continue
        head = next((h for h in known_headers if h and text.startswith(h)), None)
        if head:
            out += check_header(head, name)
            text = "\n" * head.count("\n") + text[len(head):]     # lignes justes
        out += check_resources(text, "any", name)
    return out
