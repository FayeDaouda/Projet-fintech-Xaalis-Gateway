"""Métriques Prometheus exposées sur ``/metrics``.

Toutes les métriques de la passerelle sont déclarées ici, pour qu'un seul fichier
documente ce qui est mesuré et sous quels labels (cardinalité maîtrisée : la route
est le *gabarit* ``/v1/payment-intents/{intent_id}``, jamais le chemin brut).
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUEST_DURATION = Histogram(
    "xaalis_http_request_duration_seconds",
    "Durée de traitement des requêtes HTTP, par route.",
    labelnames=("method", "route", "status"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.25, 0.5, 1.0, 2.5, 5.0),
)

# Rafraîchies à chaque lecture de /metrics depuis PostgreSQL et Redis : elles reflètent
# l'état partagé par tous les processus (API et worker), pas celui d'un seul. Avec plusieurs
# processus uvicorn, la valeur la plus récente fait foi (multiprocess_mode="mostrecent").
PAYMENT_INTENTS = Gauge(
    "xaalis_payment_intents",
    "Nombre d'intents par statut et par opérateur.",
    labelnames=("status", "operator"),
    multiprocess_mode="mostrecent",
)
CIRCUIT_BREAKER_STATE = Gauge(
    "xaalis_circuit_breaker_state",
    "État du disjoncteur par opérateur : 0 fermé, 1 semi-ouvert, 2 ouvert.",
    labelnames=("operator",),
    multiprocess_mode="mostrecent",
)
OUTBOX_PENDING = Gauge(
    "xaalis_outbox_pending_messages",
    "Messages de l'outbox non publiés, par famille (webhooks, commandes opérateurs).",
    labelnames=("family",),
    multiprocess_mode="mostrecent",
)
RECONCILIATION_ALERTS = Gauge(
    "xaalis_reconciliation_alerts_open",
    "Alertes de rapprochement non résolues, par type.",
    labelnames=("kind",),
    multiprocess_mode="mostrecent",
)
OPERATOR_CALLBACKS = Counter(
    "xaalis_operator_callbacks_total",
    "Callbacks opérateurs reçus, par opérateur et par issue de traitement.",
    labelnames=("operator", "disposition"),
)
WEBHOOK_DELIVERIES = Gauge(
    "xaalis_webhook_deliveries",
    "Livraisons de webhooks par statut (pending = profondeur de la file, dead_letter).",
    labelnames=("status",),
    multiprocess_mode="mostrecent",
)
RECONCILIATION_DISCREPANCIES = Gauge(
    "xaalis_reconciliation_discrepancies",
    "Écarts du dernier rapprochement terminé de chaque opérateur, par type.",
    labelnames=("operator", "type"),
    multiprocess_mode="mostrecent",
)
RISK_ASSESSMENTS = Counter(
    "xaalis_risk_assessments_total",
    "Évaluations anti-fraude, par décision (approve, review, decline, hold) et par source.",
    labelnames=("decision", "source"),
)
RISK_LATENCY = Histogram(
    "xaalis_risk_call_duration_seconds",
    "Durée des appels gRPC au service de scoring (deadline 150 ms).",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.15, 0.25, 0.5),
)
