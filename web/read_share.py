"""Lectures partagées entre les personnes connectées (v1.81.0).

Chaque écran ouvert interroge le cluster à son propre rythme : dix personnes
sur la vue d'ensemble de harv1, c'étaient dix `kubectl get vm` identiques
toutes les quelques secondes, et une console qui saturait un cœur à 60
personnes (aperçu à 5 s de réponse, mesuré). Ici, une lecture donnée (même
vue, même cluster, même identité présentée au cluster) n'est faite qu'UNE
fois à la fois : les demandes qui arrivent pendant qu'elle tourne attendent
son résultat au lieu d'en lancer une autre, et ce résultat sert encore
quelques secondes.

Ce qui écrit sur un cluster vide ses entrées (`invalidate`) : on ne relit
jamais un état d'avant sa propre modification.

Seules les réponses réussies sont gardées ; une erreur n'est partagée qu'avec
les demandes déjà en attente, puis la suivante réessaie.
"""
import threading
import time


class ReadShare:
    def __init__(self, clock=time.monotonic, max_entries=2048):
        self._clock = clock
        self._max = max_entries
        self._lock = threading.Lock()
        self._done = {}        # key -> (instant, valeur)
        self._flight = {}      # key -> [Event, valeur, erreur]
        self._gen = {}         # cluster -> génération (invalidation pendant un vol)
        self._epoch = 0        # invalidation de tout
        self.stats = {"hit": 0, "shared": 0, "miss": 0}

    def get(self, key, ttl, loader, keep=lambda v: True):
        """Valeur de `key` : gardée si elle a moins de `ttl` s, partagée si
        une lecture est en vol, sinon lue par `loader()` (qui peut lever).
        `keep(valeur)` décide si le résultat se garde pour les suivants."""
        cluster = key[0] if isinstance(key, tuple) and key else None
        with self._lock:
            hit = self._done.get(key)
            if hit is not None and self._clock() - hit[0] < ttl:
                self.stats["hit"] += 1
                return hit[1]
            flight = self._flight.get(key)
            if flight is None:
                flight = [threading.Event(), None, None]
                self._flight[key] = flight
                leader = True
                gen = (self._epoch, self._gen.get(cluster, 0))
                self.stats["miss"] += 1
            else:
                leader = False
                self.stats["shared"] += 1
        if not leader:
            flight[0].wait()
            if flight[2] is not None:
                raise flight[2]
            return flight[1]
        try:
            value = loader()
        except BaseException as e:
            flight[2] = e
            with self._lock:
                self._drop_flight(key, flight)
            flight[0].set()
            raise
        flight[1] = value
        with self._lock:
            self._drop_flight(key, flight)
            # une écriture survenue PENDANT la lecture la rend douteuse :
            # elle sert aux demandes déjà en attente, pas aux suivantes
            if keep(value) and (self._epoch, self._gen.get(cluster, 0)) == gen:
                if len(self._done) >= self._max:
                    self._prune()
                self._done[key] = (self._clock(), value)
        flight[0].set()
        return value

    def invalidate(self, cluster=None):
        """Oublie ce qui concerne `cluster` (premier élément des clés), ou
        tout quand `cluster` est None."""
        with self._lock:
            if cluster is None:
                self._done.clear()
                self._flight.clear()
                self._epoch += 1
                return
            mine = lambda k: isinstance(k, tuple) and k and k[0] == cluster  # noqa: E731
            for k in [k for k in self._done if mine(k)]:
                del self._done[k]
            # une demande qui arrive après l'écriture ne doit pas se joindre à
            # une lecture partie avant : elle en lance une nouvelle
            for k in [k for k in self._flight if mine(k)]:
                del self._flight[k]
            self._gen[cluster] = self._gen.get(cluster, 0) + 1

    def _drop_flight(self, key, flight):
        if self._flight.get(key) is flight:
            del self._flight[key]

    def _prune(self):
        """Retire la plus vieille moitié (appelé sous le verrou)."""
        for k, _ in sorted(self._done.items(), key=lambda kv: kv[1][0])[: self._max // 2]:
            del self._done[k]


# ---------------------------------------------------------------------------
# Fichiers YAML relus à chaque requête
# ---------------------------------------------------------------------------
# config.yaml et les déclarations de clusters étaient réanalysés à CHAQUE
# requête, et plusieurs fois par requête (6 lectures pour /api/activity, 12
# pour /api/vms) : l'analyseur YAML écrit en Python faisait l'essentiel du
# CPU de la console (profilé le 01/10/2026). Le contenu analysé est gardé
# tant que le fichier ne change pas (date, taille, inode : un remplacement
# atomique change l'inode), et l'analyse passe par libyaml quand elle existe.
import copy as _copy
import os as _os

import yaml as _yaml

_SafeLoader = getattr(_yaml, "CSafeLoader", _yaml.SafeLoader)
_yaml_files = {}
_yaml_lock = threading.Lock()


def yaml_file(path):
    """`yaml.safe_load` du fichier, mémorisé jusqu'à sa prochaine
    modification. Rend une copie : l'appelant peut la modifier. Lève comme
    la lecture directe (OSError, yaml.YAMLError, UnicodeDecodeError)."""
    key = str(path)
    st = _os.stat(key)
    sig = (st.st_mtime_ns, st.st_size, st.st_ino)
    with _yaml_lock:
        hit = _yaml_files.get(key)
    if hit is None or hit[0] != sig:
        with open(key, encoding="utf-8") as fh:
            data = _yaml.load(fh.read(), Loader=_SafeLoader)
        hit = (sig, data)
        with _yaml_lock:
            _yaml_files[key] = hit
    return _copy.deepcopy(hit[1])
