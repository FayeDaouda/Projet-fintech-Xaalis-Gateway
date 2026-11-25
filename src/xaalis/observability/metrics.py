"""Métriques Prometheus exposées sur ``/metrics``.

Toutes les métriques de la passerelle sont déclarées ici, pour qu'un seul fichier
documente ce qui est mesuré et sous quels labels (cardinalité maîtrisée : la route
est le *gabarit* ``/v1/payment-intents/{intent_id}``, jamais le chemin brut).
"""

from __future__ import annotations

from prometheus_client import Histogram

HTTP_REQUEST_DURATION = Histogram(
    "xaalis_http_request_duration_seconds",
    "Durée de traitement des requêtes HTTP, par route.",
    labelnames=("method", "route", "status"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.25, 0.5, 1.0, 2.5, 5.0),
)
