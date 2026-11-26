"""Requêtes GraphQL de la console : liste fermée, écrite côté serveur.

Le navigateur ne transmet jamais de requête GraphQL libre : il choisit une vue, le serveur
du marchand exécute la requête correspondante (même principe que les « persisted queries »).
"""

INTENT_FIELDS = """
  id status operator amount fee net amountRefunded currency payerPhone metadata
  createdAt succeededAt riskScore riskDecision
"""

PAYMENTS = f"""
query Payments($status: IntentStatus, $operator: Operator, $first: Int!, $after: String) {{
  paymentIntents(status: $status, operator: $operator, first: $first, after: $after) {{
    nodes {{ {INTENT_FIELDS} }}
    hasNextPage
    endCursor
  }}
}}
"""

PAYMENT = f"""
query Payment($id: ID!) {{
  paymentIntent(id: $id) {{
    {INTENT_FIELDS}
    expiresAt
    refunds {{ id amount reason createdAt }}
    history {{ fromStatus toStatus cause occurredAt }}
  }}
}}
"""

OVERVIEW = f"""
query Overview($from: Date!, $to: Date!) {{
  revenue(dateFrom: $from, dateTo: $to) {{ currency gross fees refunded net }}
  dailyVolumes(dateFrom: $from, dateTo: $to) {{ date operator payments succeeded volume fees }}
  successRates(dateFrom: $from, dateTo: $to) {{ operator finished succeeded successRatePermille }}
  recent: paymentIntents(first: 6) {{ nodes {{ {INTENT_FIELDS} }} }}
}}
"""
