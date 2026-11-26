"""Test de charge : création d'intents de paiement (objectif : 200/s, p95 < 150 ms).

Le scénario respecte les règles de la passerelle au lieu de les contourner :

- **XG-13, 100 requêtes/min par clé** : 200 créations/s = 12 000/min ; on crée donc au
  démarrage un pool de marchands (200 par défaut, ``XAALIS_LT_MERCHANTS``) et chaque
  utilisateur virtuel garde sa propre clé, à 1 requête/s, soit 60/min par clé ;
- **XG-13, 20 créations/min par payeur** : chaque requête tire un numéro aléatoire ;
- **idempotence** : une ``Idempotency-Key`` unique par requête, comme un vrai client.

Lancement : ``scripts/loadtest.sh`` (2 minutes, 200 utilisateurs, rapport dans
``loadtest/results/``).
"""

from __future__ import annotations

import os
import random
import uuid
from typing import Any

from locust import constant_throughput, events, task
from locust.contrib.fasthttp import FastHttpUser
from locust.env import Environment

POOL_SIZE = int(os.environ.get("XAALIS_LT_MERCHANTS", "200"))
ADMIN_EMAIL = os.environ.get("XAALIS_SEED_ADMIN_EMAIL", "admin@xaalis.example")
ADMIN_PASSWORD = os.environ.get("XAALIS_SEED_ADMIN_PASSWORD", "dev-only-admin-password")
PREFIXES = ("77", "78", "76", "70", "75")

_keys: list[str] = []


@events.test_start.add_listener
def create_merchant_pool(environment: Environment, **_: Any) -> None:
    """Crée le pool de marchands une fois, avant le début de la mesure."""
    import requests  # dépendance de Locust : disponible aussi dans l'image officielle

    host = environment.host or "http://localhost:8000"
    with requests.Session() as http:
        token = http.post(
            f"{host}/v1/admin/auth/token",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            timeout=30,
        ).json()["access_token"]
        http.headers["Authorization"] = f"Bearer {token}"
        for index in range(POOL_SIZE):
            response = http.post(
                f"{host}/v1/admin/merchants", json={"name": f"Charge {index}"}, timeout=30
            )
            response.raise_for_status()
            _keys.append(response.json()["secret_key"])


class Merchant(FastHttpUser):
    """Un marchand qui crée un intent par seconde (débit constant, pas de rafale)."""

    wait_time = constant_throughput(1)

    def on_start(self) -> None:
        self.secret_key = _keys[random.randrange(len(_keys))]  # noqa: S311

    @task
    def create_payment_intent(self) -> None:
        phone = f"+221{random.choice(PREFIXES)}{random.randrange(10**7):07d}"  # noqa: S311
        with (
            self.client.post(
                "/v1/payment-intents",
                json={
                    "amount": random.randrange(100, 500_000),  # noqa: S311
                    "currency": "XOF",
                    "payer_phone": phone,
                    "metadata": {"order_id": uuid.uuid4().hex[:12]},
                },
                headers={
                    "Authorization": f"Bearer {self.secret_key}",
                    "Idempotency-Key": str(uuid.uuid4()),
                },
                name="POST /v1/payment-intents",
                catch_response=True,
            ) as response
        ):
            # L'énoncé tolère les 429 (limite de débit) : on les compte sans les traiter en
            # échec, tout autre code que 201 en est un.
            if response.status_code in (201, 429):
                response.success()
            else:
                response.failure(f"HTTP {response.status_code}")
