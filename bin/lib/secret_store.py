"""Magasin de secrets interne (2.4.0).

Interface unique par laquelle passent les secrets nouveaux de la console :
lire, écrire, supprimer, désigner par référence. En 2.4.0 un seul support,
local et en mémoire : les secrets d'un déroulé de plan (jeton du cluster,
mot de passe de l'OS, mots de passe des BMC) y vivent le temps du déroulé,
puis sont effacés quelle qu'en soit l'issue, et redemandés à la reprise.

Une référence a la forme `local:<classe>/<chemin>#<champ>`, calquée sur
`openbao:<montage>/<chemin>#<champ>` de la conception OpenBao : le jour où
un support OpenBao arrive (2.9.0), seul le préfixe change, les appelants
gardent leurs références.

Règle de chemin d'un déroulé : `plan-run/<action_id>/<nom>`, effacé par
`delete_tree("plan-run", <action_id>)` dans le `finally` du moteur.

Jamais de valeur dans un message d'erreur, une représentation ou une
sérialisation du magasin.
"""

import re
import threading

REF_RE = re.compile(r"^local:(?P<cls>[a-z0-9-]+)/(?P<path>[a-z0-9._/-]{1,200})"
                    r"#(?P<field>[a-z0-9_]{1,64})$")
# Seule classe en 2.4.0 : les secrets d'un déroulé, jamais écrits sur disque.
# Une classe `persist: True` demandera un support fichier ou OpenBao (2.9.0).
CLASSES = {"plan-run": {"persist": False}}


class SecretMissing(KeyError):
    """Le secret désigné n'est pas (ou plus) dans le magasin. Le message ne
    porte que la référence : ni valeur, ni chemin de l'état."""

    def __init__(self, ref):
        super().__init__(ref)
        self.ref = ref

    def __str__(self):
        return f"secret not found: {self.ref}"


def _check_path(path):
    """Chemin relatif sans segment vide, `.` ni `..` ; ValueError sinon."""
    if not isinstance(path, str) or not path or path.startswith("/") or path.endswith("/"):
        raise ValueError("invalid secret path")
    for seg in path.split("/"):
        if seg in ("", ".", ".."):
            raise ValueError("invalid secret path")
    return path


def parse_ref(ref):
    """(classe, chemin, champ) d'une référence ; ValueError si elle est mal
    formée, de classe inconnue, ou si son chemin sort de sa classe."""
    m = REF_RE.match(ref) if isinstance(ref, str) else None
    if not m:
        raise ValueError("invalid secret reference")
    if m["cls"] not in CLASSES:
        raise ValueError("unknown secret class")
    return m["cls"], _check_path(m["path"]), m["field"]


def is_ref(s):
    try:
        parse_ref(s)
    except ValueError:
        return False
    return True


def make_ref(cls, path, field):
    ref = f"local:{cls}/{path}#{field}"
    parse_ref(ref)
    return ref


class Store:
    """Interface commune des supports."""

    def put(self, cls, path, field, value):                 # pragma: no cover - interface
        raise NotImplementedError

    def get(self, ref):                                      # pragma: no cover - interface
        raise NotImplementedError

    def delete(self, ref):                                   # pragma: no cover - interface
        raise NotImplementedError

    def delete_tree(self, cls, path):                        # pragma: no cover - interface
        raise NotImplementedError

    def refs(self, cls, path):                               # pragma: no cover - interface
        raise NotImplementedError


class LocalStore(Store):
    """Support local : un dictionnaire en mémoire sous verrou."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data = {}

    def __repr__(self):
        with self._lock:
            n = len(self._data)
        return f"<LocalStore {n} secret(s)>"

    __str__ = __repr__

    def __getstate__(self):
        # Un magasin ne se sérialise pas avec ses valeurs.
        return {}

    def __setstate__(self, _state):
        self.__init__()

    def put(self, cls, path, field, value):
        ref = make_ref(cls, path, field)
        if CLASSES[cls]["persist"]:
            raise NotImplementedError("persistent secret classes arrive with a file or OpenBao backend")
        if not isinstance(value, str):
            raise TypeError("a secret value is a string")
        with self._lock:
            self._data[ref] = value
        return ref

    def get(self, ref):
        try:
            parse_ref(ref)
        except ValueError:
            raise SecretMissing(ref if isinstance(ref, str) else "(invalid)") from None
        with self._lock:
            try:
                return self._data[ref]
            except KeyError:
                raise SecretMissing(ref) from None

    def delete(self, ref):
        parse_ref(ref)
        with self._lock:
            return self._data.pop(ref, None) is not None

    @staticmethod
    def _in_tree(cls, path):
        """Prédicat « la référence est sous `<classe>/<chemin>` » : le chemin
        lui-même (`...#champ`) et ce qui est dessous (`.../...`). Séparateur
        exigé : `plan-run/a1` ne désigne pas `plan-run/a10`."""
        if cls not in CLASSES:
            raise ValueError("unknown secret class")
        base = f"local:{cls}/{_check_path(path)}"
        return lambda r: r.startswith(base + "/") or r.startswith(base + "#")

    def delete_tree(self, cls, path):
        inside = self._in_tree(cls, path)
        with self._lock:
            gone = [r for r in self._data if inside(r)]
            for r in gone:
                del self._data[r]
        return len(gone)

    def refs(self, cls, path):
        inside = self._in_tree(cls, path)
        with self._lock:
            return sorted(r for r in self._data if inside(r))


_STORE = None
_STORE_LOCK = threading.Lock()


def store(state_dir=None):
    """Le support configuré. En 2.4.0 toujours le magasin local en mémoire,
    un seul par processus (la console et la CLI ont chacune le leur)."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = LocalStore()
        return _STORE
