"""Lecture continue des commandes Telegram (dans le service web Railway) : réponse en quelques secondes.

Un seul fil d'exécution, en interrogation longue (long polling) : aucune requête inutile quand personne n'écrit.
Les réponses passent par la même file dédupliquée que les rapports (pas de double envoi possible).
"""

from __future__ import annotations

import logging
import secrets
import threading
import time

from sqlalchemy.engine import Engine

from stockhunter.config import Settings
from stockhunter.notifications import engine as ne

log = logging.getLogger(__name__)


def start(engine: Engine, settings: Settings, interval_s: float = 3.0) -> threading.Thread | None:
    client = ne.client_from(settings)
    if client is None:
        log.info("Telegram non configuré : lecteur de commandes désactivé")
        return None
    client.poll_timeout = 25
    owner = f"worker-{secrets.token_hex(4)}"   # un identifiant par processus : deux conteneurs ne lisent jamais ensemble

    def loop():
        while True:
            try:
                # bail de 90 s renouvelé à chaque tour ; pendant un redéploiement, le nouveau conteneur attend
                # que le bail de l'ancien expire au lieu de provoquer des erreurs 409
                if not ne.acquire_reader(engine, owner, 90):
                    time.sleep(15)
                    continue
                if ne.process_commands(engine, client, owner=owner, ttl_s=90):
                    ne.flush(engine, client, settings.telegram_max_per_run)
            except Exception:  # le lecteur ne doit jamais faire tomber le dashboard
                log.exception("lecteur de commandes Telegram")
                time.sleep(30)
            time.sleep(interval_s)

    t = threading.Thread(target=loop, name="telegram-commands", daemon=True)
    t.start()
    return t
