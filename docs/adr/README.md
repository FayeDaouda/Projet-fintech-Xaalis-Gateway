# Architecture Decision Records

Format [Nygard](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions) :
contexte, décision, conséquences. Un ADR accepté n'est pas réécrit : on le remplace par un
nouvel ADR qui le déclare « remplacé ».

| N° | Décision | Statut |
|---|---|---|
| [0001](0001-modele-du-grand-livre.md) | Modèle du grand livre en partie double | accepté |
| [0002](0002-concurrence-et-verrouillage.md) | READ COMMITTED et verrous pessimistes explicites | accepté |
| [0003](0003-outbox-transactionnel-et-worker.md) | File de tâches : outbox transactionnel + worker asyncio | accepté |
| [0004](0004-hachage-des-cles-et-des-mots-de-passe.md) | Clés d'API en HMAC-SHA-256 + pepper, mots de passe en Argon2id | accepté |
| [0005](0005-idempotence-des-requetes.md) | Idempotence en base, dans la transaction de l'effet | accepté |
| [0006](0006-limitation-de-debit.md) | Limitation de débit : fenêtre glissante exacte (Redis + Lua) | accepté |
| [0007](0007-routage-disjoncteur-et-bascule.md) | Routage, disjoncteur et bascule sans double débit | accepté |
| [0008](0008-remboursements-et-solde-marchand.md) | Remboursements : comptes débités, verrous, ordre des contrôles | accepté |
| [0009](0009-relances-des-webhooks.md) | Webhooks : signature, relances sur 24 h, dead letter, rejeu | accepté |
| [0010](0010-traitement-des-releves-polars.md) | Traitement des relevés avec Polars (types stricts, XG-01) | accepté |
| [0011](0011-temps-reel-et-reporting.md) | Temps réel par LISTEN/NOTIFY, GraphQL avec DataLoader | accepté |
| [0012](0012-console-web-du-marchand-de-demonstration.md) | Console web portée par le marchand de démo (clé secrète côté serveur) | accepté |
| [0013](0013-scoring-de-risque-grpc.md) | Scoring de risque en gRPC : deadline 150 ms, repli borné par un plafond | accepté |
| [0014](0014-traces-opentelemetry-et-grafana.md) | Traces OpenTelemetry à travers l'outbox, Grafana provisionné | accepté |
