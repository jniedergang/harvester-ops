#!/usr/bin/env python3
"""Écritures root dans les répertoires du compte de la console (2.8.3).

/var/lib/harvester-ops et /var/log/harvester-ops appartiennent au compte
`harvester-ops` : son conteneur les monte en lecture-écriture. Root (l'agent
des mises à jour, `install.sh --upgrade`) qui y écrit par un chemin suivrait un
lien posé par ce compte, et écrirait ou donnerait au compte un fichier
quelconque de l'hôte. Tout passe donc ici par des descripteurs :

- la base (le répertoire de la console lui-même, dont le parent appartient à
  root) s'ouvre sans suivre de lien ; chaque composant en dessous s'ouvre
  relativement au précédent, sans suivre de lien (O_NOFOLLOW) ;
- un fichier s'écrit sous un nom temporaire neuf (O_CREAT|O_EXCL|O_NOFOLLOW),
  reçoit droits et propriétaire sur son descripteur (fchmod, fchown), puis
  prend sa place par rename(2), qui remplace un lien sans le suivre ;
- une lecture refuse un lien et tout ce qui n'est pas un fichier ordinaire ;
- un changement de propriétaire se fait sur un descripteur ouvert sans
  suivre de lien, et jamais sur un fichier à plusieurs liens physiques.

Bibliothèque standard seulement : l'agent tourne avec le Python de l'hôte.
Ligne de commande pour install.sh (voir `main`).
"""
import os
import secrets
import stat
import sys

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOCTTY
SERVICE_ACCOUNT = "harvester-ops"


class UnsafePath(OSError):
    """Un lien, ou autre chose qu'un répertoire ou un fichier ordinaire, là
    où root allait écrire ou lire."""


def service_ids(name=SERVICE_ACCOUNT):
    """(uid, gid) du compte de service, ou None s'il n'existe pas."""
    import grp
    import pwd
    try:
        pw = pwd.getpwnam(name)
    except KeyError:
        return None
    try:
        gid = grp.getgrnam(name).gr_gid
    except KeyError:
        gid = pw.pw_gid
    return pw.pw_uid, gid


def _check_name(name):
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise UnsafePath(f"invalid path component: {name!r}")


def _split(rel):
    parts = [p for p in str(rel).split("/") if p]
    for p in parts:
        _check_name(p)
    return parts


class SafeDir:
    """Un répertoire tenu par son descripteur."""

    def __init__(self, fd, label):
        self.fd = fd
        self.label = label

    # -- ouverture -------------------------------------------------------
    @classmethod
    def open_base(cls, path, create_mode=None, owner=None):
        """La base : un répertoire dont le PARENT appartient à root. Le
        dernier composant ne doit pas être un lien."""
        path = os.fspath(path)
        try:
            fd = os.open(path, _DIR_FLAGS)
        except FileNotFoundError:
            if create_mode is None:
                raise
            os.mkdir(path, create_mode)
            fd = os.open(path, _DIR_FLAGS)
            _set(fd, create_mode, owner)
        except OSError as e:
            raise UnsafePath(f"{path} is not a plain directory ({e.strerror})") from None
        return cls(fd, path)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def subdir(self, name, create_mode=None, owner=None, replace=False):
        """Le sous-répertoire `name`, sans suivre de lien. `create_mode` :
        le créer s'il manque (propriétaire `owner`). `replace` : un lien ou
        un fichier posé à sa place est retiré (sans être suivi) et le
        répertoire recréé ; sinon il est refusé."""
        _check_name(name)
        label = f"{self.label}/{name}"
        for _ in range(3):
            try:
                return SafeDir(os.open(name, _DIR_FLAGS, dir_fd=self.fd), label)
            except FileNotFoundError:
                if create_mode is None:
                    raise
                try:
                    os.mkdir(name, 0o700, dir_fd=self.fd)
                except FileExistsError:
                    continue
                fd = os.open(name, _DIR_FLAGS, dir_fd=self.fd)
                _set(fd, create_mode, owner)
                return SafeDir(fd, label)
            except OSError as e:
                # ELOOP : un lien ; ENOTDIR : un fichier ou un lien vers un fichier
                try:
                    st = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue                     # disparu entre-temps : on recommence
                if stat.S_ISDIR(st.st_mode):
                    raise
                if not replace or create_mode is None:
                    raise UnsafePath(f"{label} is not a plain directory ({e.strerror})") from None
                os.unlink(name, dir_fd=self.fd)
        raise UnsafePath(f"{label} keeps changing")

    def makedirs(self, rel, mode, owner=None, replace=True, apply_last=False):
        """Chaque composant de `rel` sous ce répertoire, créé s'il manque
        (droits `mode`, propriétaire `owner`). `apply_last` : droits et
        propriétaire posés aussi sur le dernier s'il existait déjà (comme
        `install -d -m -o`). Renvoie le dernier, à fermer par l'appelant."""
        cur = SafeDir(os.dup(self.fd), self.label)
        try:
            for name in _split(rel):
                nxt = cur.subdir(name, create_mode=mode, owner=owner, replace=replace)
                cur.close()
                cur = nxt
            if apply_last:
                _set(cur.fd, mode, owner)
        except BaseException:
            cur.close()
            raise
        return cur

    def open_rel(self, rel):
        """Le répertoire `rel` existant, sans rien créer ni suivre de lien."""
        cur = SafeDir(os.dup(self.fd), self.label)
        try:
            for name in _split(rel):
                nxt = cur.subdir(name)
                cur.close()
                cur = nxt
        except BaseException:
            cur.close()
            raise
        return cur

    def walk_to(self, rel, mode, owner=None, replace=True):
        """(répertoire parent tenu, à fermer ; nom du dernier composant) de
        `rel`, les répertoires manquants créés."""
        parts = _split(rel)
        if not parts:
            raise UnsafePath("empty path")
        return self.makedirs("/".join(parts[:-1]), mode, owner, replace), parts[-1]

    # -- fichiers --------------------------------------------------------
    def lexists(self, name):
        _check_name(name)
        try:
            os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def is_file(self, name):
        """Un fichier ordinaire, pas un lien."""
        _check_name(name)
        try:
            return stat.S_ISREG(os.stat(name, dir_fd=self.fd, follow_symlinks=False).st_mode)
        except FileNotFoundError:
            return False

    def write(self, name, data, mode, owner=None):
        """Écrit `data` (bytes, ou itérable de bytes) sous `name`, de façon
        atomique, sans jamais suivre de lien."""
        _check_name(name)
        self._set_aside_dir(name)
        fd, tmp = self.create_new(f".{name}.", mode, owner)
        try:
            with os.fdopen(fd, "wb", closefd=True) as f:
                if isinstance(data, (bytes, bytearray)):
                    f.write(data)
                else:
                    for chunk in data:
                        f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=self.fd)
            except OSError:
                pass
            raise

    def create(self, name, data, mode, owner=None):
        """Crée `name` avec `data` seulement s'il n'existe pas (lien compris) :
        écrit sous un nom neuf puis mis en place sans jamais remplacer
        (renameat2 RENAME_NOREPLACE, sinon lien physique puis retrait du
        temporaire). FileExistsError si le nom est pris."""
        _check_name(name)
        fd, tmp = self.create_new(f".{name}.", mode, owner)
        try:
            with os.fdopen(fd, "wb", closefd=True) as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            _rename_noreplace(self.fd, tmp, name)
        finally:
            try:
                os.unlink(tmp, dir_fd=self.fd)
            except OSError:
                pass

    def _set_aside_dir(self, name):
        """Un répertoire posé là où root écrit un fichier est écarté sous un
        nom caché (sans le suivre ni le parcourir) : sinon le rename
        échouerait et bloquerait chaque mise à jour."""
        try:
            st = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(st.st_mode):
            os.rename(name, f".{name}.set-aside.{secrets.token_hex(4)}",
                      src_dir_fd=self.fd, dst_dir_fd=self.fd)

    def set_aside(self, name):
        """Écarte une entrée qui n'est pas un fichier ordinaire (répertoire,
        tube...) sous un nom caché ; un fichier ou un lien est retiré."""
        _check_name(name)
        try:
            st = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(st.st_mode):
            self._set_aside_dir(name)
        else:
            self.unlink(name)

    def copy_from(self, src_path, name, mode, owner=None):
        """Copie un fichier de root (le livrable) sous `name`."""
        def chunks():
            with open(src_path, "rb") as f:
                while True:
                    b = f.read(1 << 20)
                    if not b:
                        return
                    yield b
        self.write(name, chunks(), mode, owner)

    def create_new(self, prefix, mode, owner=None, suffix=""):
        """Un fichier neuf, ouvert en écriture : (fd, nom). Le nom porte un
        suffixe aléatoire quand `prefix` se termine par '.' ou '-'."""
        for _ in range(16):
            name = prefix + (secrets.token_hex(6) if prefix.endswith((".", "-")) else "") + suffix
            _check_name(name)
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOCTTY,
                             0o600, dir_fd=self.fd)
            except FileExistsError:
                continue
            _set(fd, mode, owner)
            return fd, name
        raise UnsafePath(f"could not create a new file in {self.label}")

    def open_new_append(self, name, mode, owner=None):
        """Un fichier qui n'existait pas, ouvert en ajout ; FileExistsError
        si le nom est pris (lien compris)."""
        _check_name(name)
        fd = os.open(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOCTTY,
                     0o600, dir_fd=self.fd)
        _set(fd, mode, owner)
        return fd

    def read(self, name, limit=1 << 20):
        """Le contenu d'un fichier ordinaire (pas un lien, pas un tube, un
        seul lien physique), ou
        None s'il n'existe pas. UnsafePath sinon, ou s'il dépasse `limit`."""
        _check_name(name)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOCTTY, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        except OSError as e:
            raise UnsafePath(f"{self.label}/{name} is not a plain file ({e.strerror})") from None
        try:
            st = os.fstat(fd)
            # un lien physique vers un fichier de root (hôte sans
            # fs.protected_hardlinks) n'est pas lu non plus
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise UnsafePath(f"{self.label}/{name} is not a plain file")
        except BaseException:
            os.close(fd)
            raise
        with os.fdopen(fd, "rb") as f:
            data = f.read(limit + 1)
        if len(data) > limit:
            raise UnsafePath(f"{self.label}/{name} is too large")
        return data

    def unlink(self, name):
        """Retire une entrée (fichier ou lien, jamais suivi) ; absente : rien."""
        _check_name(name)
        try:
            os.unlink(name, dir_fd=self.fd)
        except (FileNotFoundError, IsADirectoryError):
            pass

    def chown_tree(self, owner):
        """Donne ce répertoire et tout ce qu'il contient à `owner` (uid, gid),
        sans suivre de lien : chaque entrée est ouverte sans suivre de lien et
        changée sur son descripteur ; liens, fichiers spéciaux et fichiers à
        plusieurs liens physiques sont laissés tels quels."""
        if owner is None:
            return
        os.fchown(self.fd, *owner)
        # Parcours à la main : os.fwalk ouvre les sous-répertoires sans
        # O_NOFOLLOW (il ne compare qu'après coup), ce qui laisserait root
        # ouvrir un périphérique substitué à un répertoire. Ici, un
        # répertoire ne s'ouvre que par subdir() (O_DIRECTORY|O_NOFOLLOW) et
        # un fichier n'est ouvert que si lstat le dit ordinaire.
        for name in os.listdir(self.fd):
            try:
                st = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                try:
                    sub = self.subdir(name)
                except OSError:
                    continue                     # remplacé entre-temps
                with sub:
                    sub.chown_tree(owner)
            elif stat.S_ISREG(st.st_mode):
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOCTTY,
                                 dir_fd=self.fd)
                except OSError:
                    continue                     # lien (ELOOP), disparu, illisible
                try:
                    st = os.fstat(fd)
                    if stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
                        os.fchown(fd, *owner)
                finally:
                    os.close(fd)


def _rename_noreplace(dir_fd, src, dst):
    """rename(2) qui échoue si `dst` existe (FileExistsError)."""
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        fn = libc.renameat2
    except (OSError, AttributeError):
        fn = None
    if fn is not None:
        if fn(dir_fd, src.encode(), dir_fd, dst.encode(), 1) == 0:          # RENAME_NOREPLACE
            return
        err = ctypes.get_errno()
        if err == 17:
            raise FileExistsError(err, os.strerror(err), dst)
        if err not in (22, 38, 95):                     # non pris en charge : repli
            raise OSError(err, os.strerror(err), dst)
    os.link(src, dst, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)


def _set(fd, mode, owner):
    os.fchmod(fd, mode)
    if owner is not None:
        os.fchown(fd, *owner)


def clean_line(text, limit=4000):
    """Une ligne de journal sans retour à la ligne ni caractère de contrôle :
    un contenu venu d'une demande ou d'une archive pas encore vérifiée ne peut
    pas fabriquer de lignes. Les couleurs ANSI sont retirées, le reste des
    caractères de contrôle et de mise en forme Unicode (bidi, largeur
    nulle) est échappé (\\xNN, \\uNNNN)."""
    import re
    import unicodedata
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", str(text))
    out = []
    for c in text:
        o = ord(c)
        if c == "\t" or (o >= 0x20 and o != 0x7f and not 0x80 <= o < 0xa0 and o not in (0x2028, 0x2029)
                          and unicodedata.category(c) != "Cf"):
            out.append(c)
        else:
            out.append(f"\\x{o:02x}" if o < 0x100 else f"\\u{o:04x}")
    s = "".join(out)
    return s if len(s) <= limit else s[:limit] + "..."


# ---------------------------------------------------------------------------
# Ligne de commande (install.sh)
# ---------------------------------------------------------------------------
USAGE = """usage: safe_fs.py [--owner ACCOUNT] COMMAND BASE ...
  mkdir BASE REL MODE                 create each directory of REL under BASE
  write BASE REL MODE                 write stdin to BASE/REL (atomic)
  create BASE REL MODE                write stdin to BASE/REL only if absent (exit 4 if present);
                                      --owner applies to the directories created, the file stays root's
  install SRC BASE REL MODE [--keep]  copy SRC to BASE/REL; --keep: leave an existing file
  exists BASE REL                     exit 0 if BASE/REL is a plain file, 3 otherwise
  read BASE REL                       print BASE/REL if it is a plain file (exit 3 if absent)
  chown-tree BASE REL                 give BASE/REL and its content to --owner
BASE is a directory whose parent belongs to root; nothing under it is
followed if it is a link. --owner names the account that receives what is
created (silently skipped when the account does not exist)."""


def main(argv):
    owner = None
    if len(argv) >= 2 and argv[0] == "--owner":
        # hors root (tests), rien n'est donné : fchown échouerait
        owner = service_ids(argv[1]) if os.geteuid() == 0 else None
        argv = argv[2:]
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2
    cmd, args = argv[0], argv[1:]
    keep = "--keep" in args
    args = [a for a in args if a != "--keep"]
    try:
        if cmd == "install":
            src, args = args[0], args[1:]
        base, rel = args[0], args[1]
        with SafeDir.open_base(base) as b:
            if cmd == "mkdir":
                b.makedirs(rel, int(args[2], 8), owner, apply_last=True).close()
            elif cmd == "create":
                d, name = b.walk_to(rel, 0o750, owner)
                with d:
                    try:
                        d.create(name, sys.stdin.buffer.read(), int(args[2], 8))
                    except FileExistsError:
                        return 4
            elif cmd in ("write", "install"):
                mode = int(args[2], 8)
                d, name = b.walk_to(rel, 0o750, owner)
                with d:
                    if cmd == "write":
                        d.write(name, sys.stdin.buffer.read(), mode, owner)
                    elif not (keep and d.is_file(name)):
                        d.copy_from(src, name, mode, owner)
            elif cmd == "exists":
                parts = _split(rel)
                with b.open_rel("/".join(parts[:-1])) as d:
                    return 0 if d.is_file(parts[-1]) else 3
            elif cmd == "read":
                parts = _split(rel)
                with b.open_rel("/".join(parts[:-1])) as d:
                    data = d.read(parts[-1])
                if data is None:
                    return 3
                sys.stdout.buffer.write(data)
            elif cmd == "chown-tree":
                if owner is not None:
                    with b.open_rel(rel) as d:
                        d.chown_tree(owner)
            else:
                print(USAGE, file=sys.stderr)
                return 2
    except FileNotFoundError as e:
        if cmd in ("read", "exists", "chown-tree"):
            return 3
        print(f"safe_fs: {e}", file=sys.stderr)
        return 1
    except (IndexError, ValueError):
        print(USAGE, file=sys.stderr)
        return 2
    except OSError as e:
        print(f"safe_fs: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
