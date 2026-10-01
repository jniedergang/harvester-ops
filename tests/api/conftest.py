"""Réglages communs aux tests d'API.

v1.81.0 : les lectures partagées (read_share.py) servent la même réponse
quelques secondes ; un test qui change sa simulation entre deux appels du même
écran recevrait la précédente. Coupées par défaut ici, activées par les tests
qui les vérifient (test_read_share_181.py)."""
import os

os.environ.setdefault("HARVESTER_OPS_READ_SHARE", "0")
