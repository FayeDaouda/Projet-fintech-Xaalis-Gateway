// Webhooks : boîte de réception du marchand, panne simulée, relances, dead letter et rejeu.

import { api, session } from "../api.js";
import { busyButton, copyButton, emptyState, fmt, h, icon, problemToast, setBadge, toast } from "../ui.js";

const MAX_ATTEMPTS = 8;
const gatewayState = new Map(); // event_id → livraison vue par la passerelle

export async function render(root) {
  const list = h("div");
  const toggle = h("input", { type: "checkbox", role: "switch", id: "fail-toggle" });
  const statusLine = h("span", { class: "muted" });
  const counters = h("div", { class: "grid cols-3" });

  const s = await session().catch(() => null);
  root.append(
    h("div", { class: "page-head" },
      h("div", null, h("h1", null, "Webhooks"), h("p", null, "Ce que reçoit le serveur du marchand : signature HMAC vérifiée, déduplication par identifiant d'événement, relances jusqu'à 8 tentatives puis dead letter."))),
    h("div", { class: "grid split" },
      h("section", { class: "card" },
        h("div", { class: "card-head" }, h("h2", null, "Événements reçus"), h("span", { class: "hint" }, "les plus récents d'abord"),
          h("span", { class: "right pill ok live" }, h("span", { class: "dot" }), "écoute")),
        list),
      h("div", { class: "stack" },
        h("section", { class: "card" },
          h("div", { class: "card-head" }, h("h2", null, "Endpoint")),
          h("div", { class: "card-body stack" },
            h("div", { class: "endpoint-url" }, s?.webhook_endpoint?.url || "—"),
            h("dl", { class: "kv" },
              h("dt", null, "Identifiant"), h("dd", null, s ? copyButton(s.webhook_endpoint.id) : "—"),
              h("dt", null, "En-tête"), h("dd", { class: "mono" }, "X-Xaalis-Signature"),
              h("dt", null, "Format"), h("dd", { class: "mono" }, "t=<horodatage>,v1=<hmac>"),
              h("dt", null, "Algorithme"), h("dd", null, "HMAC-SHA-256 · tolérance 300 s"),
              h("dt", null, "Relances"), h("dd", null, "8 tentatives, ≈ 24 h, jitter ±20 %")))),
        h("section", { class: "card" },
          h("div", { class: "card-head" }, h("h2", null, "Panne du serveur marchand")),
          h("div", { class: "card-body stack" },
            h("label", { class: "switch", for: "fail-toggle" }, toggle, h("span", { class: "track" }), h("span", null, h("strong", null, "Répondre 503 à tous les webhooks"))),
            statusLine,
            h("div", { class: "callout info" }, icon("info"), h("div", { style: { minWidth: 0 } }, "En production, les 8 tentatives s'étalent sur ≈ 24 h. Pour la démonstration, lancez la pile avec ", h("code", { class: "mono", style: { overflowWrap: "anywhere" } }, "XAALIS_WEBHOOK_RETRY_SCHEDULE_SECONDS='[2,2,2,2,2,2,2]'"), " : elles tiennent alors en une quinzaine de secondes.")))),
        counters)));

  toggle.addEventListener("change", async () => {
    try {
      await api.post("/webhooks/failure", { enabled: toggle.checked });
      toast(toggle.checked ? "Serveur marchand en panne" : "Serveur marchand rétabli", { tone: toggle.checked ? "bad" : "ok", detail: toggle.checked ? "Les livraisons échouent : la passerelle relance." : "Les prochaines livraisons aboutiront." });
      load();
    } catch (error) { toggle.checked = !toggle.checked; problemToast(error); }
  });

  function attemptsDots(event, delivery) {
    const total = Math.max(event.attempts.length, delivery?.attempts || 0);
    return h("span", { class: "attempts", title: `${total} tentative(s) sur ${MAX_ATTEMPTS}`, "aria-label": `${total} tentative(s) sur ${MAX_ATTEMPTS}` },
      // 8 emplacements (le maximum d'une livraison) ; un rejeu ajoute ses tentatives à la suite.
      Array.from({ length: Math.max(MAX_ATTEMPTS, event.attempts.length) }, (_, i) => {
        const a = event.attempts[i];
        return h("i", { class: a ? (a.outcome === "accepted" ? "ok" : a.outcome === "duplicate" ? "dup" : "bad") : "" });
      }));
  }

  function eventRow(event) {
    const delivery = gatewayState.get(event.event_id);
    let state;
    if (event.processed) state = h("span", { class: "pill ok" }, icon("check"), "Traité");
    else if (delivery?.status === "dead_letter") state = h("span", { class: "pill bad" }, icon("alert"), "Dead letter");
    else state = h("span", { class: "pill warn" }, icon("loader", { class: "spin" }), "Relance…");
    const replay = !event.processed && delivery?.status === "dead_letter"
      ? busyButton(h("button", { type: "button", class: "btn sm" }, icon("rotate"), "Rejouer"), async () => {
          try {
            const r = await api.post(`/webhook-events/${event.event_id}/replay`);
            toast("Événement rejoué", { tone: "ok", detail: `${r.deliveries_rescheduled} livraison(s) replanifiée(s)` });
            gatewayState.delete(event.event_id);
            setTimeout(load, 600);
          } catch (error) { problemToast(error, "Rejeu impossible"); }
        })
      : null;
    const last = event.attempts.at(-1);
    return h("div", { class: "row", style: { padding: "14px 20px", borderBottom: "1px solid var(--border)", gap: "14px", flexWrap: "wrap" } },
      h("div", { style: { flex: "1 1 220px", minWidth: 0 } },
        h("div", { class: "row", style: { gap: "8px" } }, h("span", { class: "mono", style: { fontWeight: 600 } }, event.type)),
        h("div", { class: "subtle mono", style: { fontSize: "11.5px", marginTop: "2px" } }, event.event_id, " · ", event.object_id)),
      attemptsDots(event, delivery),
      h("span", { class: "subtle num", style: { width: "64px", textAlign: "right" } }, last ? fmt.time(last.at) : ""),
      h("span", { class: "row", style: { width: "200px", justifyContent: "flex-end" } }, state, replay));
  }

  async function trackFailures(events) {
    const pending = events.filter((e) => !e.processed).slice(0, 10);
    await Promise.all(pending.map(async (e) => {
      try {
        const detail = await api.get(`/webhook-events/${e.event_id}`);
        gatewayState.set(e.event_id, detail.deliveries[0]);
      } catch { /* l'état passerelle est indicatif */ }
    }));
  }

  async function load() {
    let inbox;
    try { inbox = await api.get("/webhooks"); } catch (error) { list.replaceChildren(emptyState("alert", error.title, error.detail)); return; }
    toggle.checked = inbox.failing;
    statusLine.replaceChildren(inbox.failing
      ? h("span", { class: "pill bad" }, h("span", { class: "dot" }), "En panne : chaque livraison reçoit 503")
      : h("span", { class: "pill ok" }, h("span", { class: "dot" }), "Disponible : livraisons acceptées (200)"));
    await trackFailures(inbox.events);
    const dead = inbox.events.filter((e) => !e.processed && gatewayState.get(e.event_id)?.status === "dead_letter").length;
    const retrying = inbox.events.filter((e) => !e.processed).length - dead;
    setBadge("webhooks", dead ? String(dead) : "", { alert: true });
    counters.replaceChildren(
      h("div", { class: "card kpi" }, h("div", { class: "label" }, icon("check"), "Traités"), h("div", { class: "value" }, inbox.processed)),
      h("div", { class: "card kpi" }, h("div", { class: "label" }, icon("refresh"), "En relance"), h("div", { class: "value" }, retrying)),
      h("div", { class: "card kpi" }, h("div", { class: "label" }, icon("alert"), "Dead letter"), h("div", { class: "value" }, dead)));
    list.replaceChildren(...(inbox.events.length
      ? inbox.events.map(eventRow)
      : [emptyState("webhook", "Aucun événement reçu", "Payez une commande : payment.succeeded arrivera ici en quelques secondes, signé par la passerelle.")]));
  }

  await load();
  const timer = setInterval(() => { if (!document.hidden) load(); }, 1500);
  return () => clearInterval(timer);
}
