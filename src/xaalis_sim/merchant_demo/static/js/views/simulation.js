// Simulateur d'opérateurs : pannes, latence, taux d'échec. Sert à montrer la bascule.

import { api } from "../api.js";
import { OPERATORS, busyButton, emptyState, fmt, h, icon, problemToast, setBadge, toast } from "../ui.js";

export async function render(root) {
  const cards = h("div", { class: "grid cols-3" });
  const presentation = busyButton(h("button", { type: "button", class: "btn primary" }, icon("sparkles"), "Mode présentation"), async () => {
    try { await api.post("/simulation/presentation"); toast("Mode présentation activé", { tone: "ok", detail: "Callbacks en 1,5 s, aucun échec ni doublon aléatoire." }); load(); }
    catch (error) { problemToast(error); }
  });
  const reset = busyButton(h("button", { type: "button", class: "btn" }, icon("rotate"), "Profils réalistes"), async () => {
    try { await api.delete("/simulation"); toast("Profils réalistes rétablis", { tone: "info", detail: "Latences variables et échecs aléatoires de chaque opérateur." }); load(); }
    catch (error) { problemToast(error); }
  });

  root.append(
    h("div", { class: "page-head" },
      h("div", null, h("h1", null, "Simulateur opérateurs"), h("p", null, "Les trois opérateurs simulés répondent comme de vrais partenaires : latence, refus, callbacks en double. Mettez-en un en panne pour voir la passerelle basculer.")),
      h("div", { class: "actions" }, reset, presentation)),
    cards,
    h("div", { class: "callout info", style: { marginTop: "16px" } }, icon("route"),
      h("div", null, h("strong", null, "Comment Xaalis choisit. "),
        "Parmi les opérateurs qui acceptent le préfixe du payeur, le moins cher d'abord (XG-06), puis le meilleur taux de succès sur 5 minutes ; un opérateur dont le disjoncteur est ouvert passe en dernier. Une panne franche (refus certain) déclenche la bascule ; une réponse ambiguë (délai dépassé) fige l'opérateur pour ne jamais débiter deux fois.")));

  async function setOutage(op, seconds) {
    try {
      await api.post("/simulation/outage", { operator: op, seconds });
      toast(seconds ? `${OPERATORS[op].name} en panne pour ${seconds / 60} min` : `${OPERATORS[op].name} rétabli`, { tone: seconds ? "bad" : "ok", detail: seconds ? "Les paiements basculent vers l'opérateur suivant." : undefined });
      load();
    } catch (error) { problemToast(error); }
  }

  function card(op, state) {
    const meta = OPERATORS[op];
    const down = state.outage;
    return h("section", { class: `card op-card${down ? " down" : ""}`, style: { "--op-color": `var(--op-${op})` } },
      h("div", { class: "op-title" }, h("span", { class: "op-logo", "aria-hidden": "true" }, meta.short),
        h("div", null, h("div", { class: "op-name" }, meta.name), h("div", { class: "subtle", style: { fontSize: "12px" } }, `Préfixes ${meta.prefixes.join(", ")} · coût ${fmt.bps(meta.bps)}`)),
        h("span", { style: { marginLeft: "auto" } }, down
          ? h("span", { class: "pill bad" }, h("span", { class: "dot" }), state.outage_remaining_seconds ? `Panne · ${state.outage_remaining_seconds} s` : "En panne")
          : h("span", { class: "pill ok live" }, h("span", { class: "dot" }), "En ligne"))),
      h("dl", { class: "kv" },
        h("dt", null, "Latence du callback"), h("dd", { class: "num" }, `${state.latency_ms[0].toLocaleString("fr-FR")}–${state.latency_ms[1].toLocaleString("fr-FR")} ms`),
        h("dt", null, "Refus aléatoires"), h("dd", { class: "num" }, `${Math.round(state.failure_rate * 100)} %`),
        h("dt", null, "Callbacks en double"), h("dd", { class: "num" }, `${Math.round(state.duplicate_rate * 100)} %`)),
      down
        ? h("button", { type: "button", class: "btn block", onclick: () => setOutage(op, 0) }, icon("power"), "Rétablir")
        : h("button", { type: "button", class: "btn danger block", onclick: () => setOutage(op, 120) }, icon("zap"), "Simuler une panne (2 min)"));
  }

  async function load() {
    try {
      const chaos = await api.get("/simulation");
      cards.replaceChildren(...Object.keys(OPERATORS).map((op) => card(op, chaos[op])));
      const outages = Object.values(chaos).filter((c) => c.outage).length;
      setBadge("simulation", outages ? String(outages) : "", { alert: true });
    } catch (error) {
      cards.replaceChildren(h("div", { class: "card", style: { gridColumn: "1 / -1" } }, emptyState("alert", error.title, error.detail)));
    }
  }

  await load();
  const timer = setInterval(() => { if (!document.hidden) load(); }, 1000);
  return () => clearInterval(timer);
}
