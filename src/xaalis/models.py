"""Registre des modèles ORM : importé par Alembic pour connaître toutes les tables."""

from xaalis.admin import models as admin_models
from xaalis.idempotency import models as idempotency_models
from xaalis.infra.db import Base
from xaalis.ledger import models as ledger_models
from xaalis.merchants import models as merchant_models
from xaalis.operators import models as operator_models
from xaalis.outbox import models as outbox_models
from xaalis.payments import models as payment_models
from xaalis.reconciliation import models as reconciliation_models
from xaalis.refunds import models as refund_models
from xaalis.settlement import models as settlement_models
from xaalis.webhooks import models as webhook_models

__all__ = [
    "Base",
    "admin_models",
    "idempotency_models",
    "ledger_models",
    "merchant_models",
    "operator_models",
    "outbox_models",
    "payment_models",
    "reconciliation_models",
    "refund_models",
    "settlement_models",
    "webhook_models",
]
