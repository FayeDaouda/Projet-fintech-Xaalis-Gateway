# ADR 0012 — Console web portée par le marchand de démonstration

- **Statut** : accepté
- **Date** : 2026-11-26
- **Règles concernées** : OWASP API2 et API8, XG-03, XG-12, démonstration vidéo

## Contexte

La passerelle n'expose qu'une API. Pour la démonstration (et pour tester à la main), il faut
une interface : une boutique qui encaisse, un tableau de bord marchand, l'état des webhooks,
les opérations du back-office et le pilotage du simulateur.

Trois contraintes :

1. la clé secrète `sk_` ne doit jamais atteindre un navigateur (elle signe les écritures) ;
2. `docker compose up --build` doit continuer de fonctionner sur une machine vierge ;
3. le contrat de la passerelle ne doit pas être modifié pour les besoins d'une démo.

## Décision

- **La console est servie par le service `merchant-demo`** (port 8200), qui joue déjà le
  rôle du serveur du marchand (réception des webhooks). C'est le modèle de tout PSP :
  navigateur → serveur du marchand (clé secrète) → passerelle.
- **Backend-for-frontend** (`xaalis_sim/merchant_demo/console.py`) : le navigateur n'appelle
  que `/api/*`. Le serveur ajoute la clé secrète, signe en HMAC les remboursements et
  virements sur les octets exacts envoyés, relaie la clé d'idempotence fournie par le
  navigateur (un double clic rejoue au lieu de dupliquer) et renvoie les erreurs de la
  passerelle **telles quelles** (statut et `application/problem+json`).
- **Aucune dépendance interne** : le client (`gateway.py`) parle à la passerelle en HTTP
  selon le contrat OpenAPI, comme le SDK d'un client. Le test d'intégration le branche sur
  la vraie passerelle (PostgreSQL, Redis) : une signature refusée y échouerait.
- **Intégration paresseuse** : au premier appel, le marchand est créé par l'API
  d'administration, son endpoint de webhooks enregistré et le secret recopié dans le
  récepteur — le parcours exact d'un intégrateur.
- **GraphQL en liste fermée** (`queries.py`) : le navigateur choisit une vue, jamais une
  requête libre (principe des *persisted queries*).
- **SSE relayé avec la clé publique** : `EventSource` ne sait pas envoyer d'en-tête
  `Authorization` ; le serveur du marchand relaie le flux de la passerelle, sans CORS à
  ouvrir côté passerelle. `succeeded` n'étant pas terminal (un remboursement reste
  possible), c'est l'interface qui ferme le flux une fois le paiement abouti.
- **Interface sans étape de build** : HTML, CSS et modules ES servis tels quels. Aucune
  chaîne Node dans l'image, rien à compiler, rien qui puisse casser un démarrage à froid.
  Le DOM est construit par `createElement`/`textContent` : aucune donnée n'est interprétée
  comme du HTML (pas de XSS par une métadonnée ou un motif de remboursement).
- **Webhooks observables** : le récepteur journalise chaque tentative **authentifiée**, y
  compris celles refusées pendant une panne simulée (503). Une requête mal signée n'entre
  jamais dans le journal.

## Conséquences

- La console détient les identifiants d'administration **de développement** pour le
  back-office (rapprochement, règlement). C'est un outil local, propre à la pile Docker
  Compose de développement ; en production, ces écrans relèveraient d'une application
  d'administration distincte et authentifiée.
- L'état du marchand de démo est en mémoire : un redémarrage du service crée un nouveau
  marchand (chiffres remis à zéro), ce qui est voulu pour rejouer une démonstration.
- Le relais SSE ajoute un saut réseau ; négligeable devant la latence d'un opérateur.
- Rejeté : une SPA React/Vite (étape de build, dépendances npm, image plus lourde pour un
  gain nul ici) ; ouvrir CORS sur la passerelle pour la clé publique (modifie le contrat
  et la surface d'attaque pour un besoin de démonstration).
