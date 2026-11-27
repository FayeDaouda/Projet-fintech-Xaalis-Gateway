// Paiements : liste filtrable (GraphQL, pagination par curseur), détail et remboursement.

import { api, newIdempotencyKey, session } from "../api.js";
import { ACCOUNTS, CAUSES, OPERATORS, STATUSES, TRANSACTIONS, amount, busyButton, closeLayer, copyButton, drawer, emptyState, fmt, h, icon, operator, problemToast, riskPill, skeletonRows, statusPill, toast, pageIntro } from "../ui.js";

const filters = { status: "", operator: "" };
const REFUNDABLE = new Set(["succeeded", "partially_refunded"]);

export async function render(root, { params }) {
  const tbody = h("tbody");
  const more = h("button", { type: "button", class: "btn sm", hidden: true }, "Charger plus");
  const counter = h("span", { class: "hint" });
  let cursor = null;
  let rows = [];
  let pages = 1;

  const statusSelect = h("select", { class: "select", style: { width: "190px" }, "aria-label": "Filtrer par statut", onchange: (e) => { filters.status = e.target.value; reload(); } },
    h("option", { value: "" }, "Tous les statuts"),
    Object.entries(STATUSES).map(([value, meta]) => h("option", { value, selected: filters.status === value }, meta.label)));
  const operatorSelect = h("select", { class: "select", style: { width: "190px" }, "aria-label": "Filtrer par opérateur", onchange: (e) => { filters.operator = e.target.value; reload(); } },
    h("option", { value: "" }, "Tous les opérateurs"),
    Object.entries(OPERATORS).map(([value, op]) => h("option", { value, selected: filters.operator === value }, op.name)));

  root.append(
pageIntro({
      id: "payments",
      eyebrow: "Piloter · paiements",
      title: "Paiements et remboursements",
      lead: "Tous les paiements du marchand, filtrables et paginés par curseur. Chaque paiement révèle son historique de transitions, son évaluation anti-fraude, ses écritures comptables et sa trace distribuée ; un remboursement se déclenche en deux clics, signé et idempotent.",
      seen: [
        "La liste rafraîchie en direct ; les nouveaux paiements sont mis en évidence.",
        "Le détail d'un paiement : machine à états, anti-fraude, écritures au grand livre, trace Jaeger.",
        "Le remboursement partiel ou total, borné par le montant restant.",
      ],
      under: [["Machine à états", " 409 sinon"], ["HMAC", " requêtes signées"], ["XG-08 à 11", " remboursements"], ["XG-03", " numéros masqués"], ["ADR", " 0002 · 0008"]],
      actions: [statusSelect, operatorSelect],
    }),
    h("section", { class: "card" },
      h("div", { class: "card-head" }, h("h2", null, "Intents de paiement"), counter,
        h("div", { class: "right" }, h("button", { type: "button", class: "btn sm ghost", onclick: () => reload() }, icon("refresh"), "Actualiser"))),
      h("div", { class: "table-wrap" }, h("table", { class: "data" },
        h("thead", null, h("tr", null, h("th", { class: "num" }, "Montant"), h("th", null, "Statut"), h("th", null, "Opérateur"), h("th", null, "Payeur"), h("th", null, "Commande"), h("th", null, "Date"), h("th", null, "Identifiant"))),
        tbody)),
      h("div", { class: "card-foot row", style: { justifyContent: "center" } }, more)));

  function row(p, fresh) {
    return h("tr", { class: `clickable${fresh ? " flash" : ""}`, tabindex: 0, onclick: () => openDetail(p.id), onkeydown: (e) => { if (e.key === "Enter") openDetail(p.id); } },
      h("td", { class: "num" }, amount(p.amount), p.amount_refunded ? h("div", { class: "subtle", style: { fontSize: "12px" } }, `− ${fmt.number(p.amount_refunded)} remboursé`) : null),
      h("td", null, statusPill(p.status)),
      h("td", null, operator(p.operator)),
      h("td", { class: "mono muted" }, p.payer_phone),
      h("td", { class: "muted" }, p.metadata?.order_id || "—"),
      h("td", { class: "muted" }, fmt.dateTime(p.created_at)),
      h("td", { class: "mono subtle" }, p.id));
  }

  async function fetchPage(after) {
    const query = new URLSearchParams({ first: "25" });
    if (filters.status) query.set("status", filters.status);
    if (filters.operator) query.set("operator", filters.operator);
    if (after) query.set("after", after);
    return (await api.get(`/payments?${query}`)).payment_intents;
  }

  function paint(previousIds = null) {
    if (!rows.length) {
      tbody.replaceChildren(h("tr", null, h("td", { colspan: 7 }, emptyState("list", "Aucun paiement", filters.status || filters.operator ? "Aucun résultat pour ces filtres." : "Passez une commande dans la boutique pour voir le premier paiement.",
        h("a", { class: "btn sm", href: "#/boutique" }, icon("store"), "Ouvrir la boutique")))));
    } else {
      tbody.replaceChildren(...rows.map((p) => row(p, previousIds && !previousIds.has(p.id))));
    }
    counter.textContent = rows.length ? `${rows.length}${more.hidden ? "" : "+"} résultat(s)` : "";
  }

  async function reload({ quiet = false } = {}) {
    if (!quiet) tbody.replaceChildren(...skeletonRows(6, 7));
    try {
      const previous = quiet ? new Set(rows.map((r) => r.id)) : null;
      const page = await fetchPage(null);
      rows = page.nodes;
      cursor = page.end_cursor;
      pages = 1;
      more.hidden = !page.has_next_page;
      paint(previous);
    } catch (error) {
      tbody.replaceChildren(h("tr", null, h("td", { colspan: 7 }, emptyState("alert", error.title, error.detail))));
    }
  }

  busyButton(more, async () => {
    try {
      const page = await fetchPage(cursor);
      rows = rows.concat(page.nodes);
      cursor = page.end_cursor;
      pages += 1;
      more.hidden = !page.has_next_page;
      paint();
    } catch (error) { problemToast(error); }
  });

  async function openDetail(id) {
    if (location.hash !== `#/paiements/${id}`) history.replaceState(null, "", `#/paiements/${id}`);
    const content = h("div", { class: "stack" }, h("div", { class: "skeleton", style: { height: "120px" } }, "."));
    drawer({ title: "Paiement", subtitle: copyButton(id), body: content });
    const panel = document.querySelector(".drawer");
    const observer = new MutationObserver(() => {
      if (!panel.isConnected) { observer.disconnect(); if (location.hash.startsWith("#/paiements/")) history.replaceState(null, "", "#/paiements"); }
    });
    observer.observe(document.body, { childList: true });
    await fillDetail(id, content);
  }

  async function fillDetail(id, content) {
    let p;
    try { p = await api.get(`/payment-intents/${id}`); } catch (error) { content.replaceChildren(emptyState("alert", error.title, error.detail)); return; }
    const remaining = p.amount - p.amount_refunded;
    const failover = p.history.some((e) => e.cause === "operator_failover");
    const sections = [
      h("div", { class: "row between" }, h("div", null, h("div", { class: "kpi", style: { padding: 0 } }, h("div", { class: "value" }, fmt.number(p.amount), h("span", { class: "cur" }, "XOF"))),
        h("div", { class: "subtle" }, p.metadata?.order_id ? `Commande ${p.metadata.order_id}` : "Sans référence de commande")), statusPill(p.status)),
      h("section", null, h("h3", null, "Détails"),
        h("dl", { class: "kv" },
          h("dt", null, "Opérateur"), h("dd", null, operator(p.operator)),
          h("dt", null, "Anti-fraude"), h("dd", null, riskPill(p.risk_decision, p.risk_score)),
          h("dt", null, "Payeur"), h("dd", { class: "mono" }, p.payer_phone),
          h("dt", null, "Commission Xaalis"), h("dd", null, amount(p.fee)),
          h("dt", null, "Net marchand"), h("dd", null, amount(p.net)),
          h("dt", null, "Remboursé"), h("dd", null, amount(p.amount_refunded)),
          h("dt", null, "Créé le"), h("dd", null, fmt.dateTime(p.created_at)),
          h("dt", null, "Réussi le"), h("dd", null, fmt.dateTime(p.succeeded_at)))),
      failover ? h("div", { class: "callout warn" }, icon("route"), h("div", null, h("strong", null, "Bascule d'opérateur. "), "Refus certain du premier opérateur (panne ou disjoncteur ouvert) : le paiement a été relancé chez le suivant. Une réponse ambiguë, elle, n'entraîne jamais de bascule (ADR 0007).")) : null,
      h("section", null, h("h3", null, "Historique"),
        h("ol", { class: "timeline" }, p.history.map((e, i) =>
          h("li", { class: i === p.history.length - 1 && !["succeeded", "partially_refunded", "refunded"].includes(e.to_status) && e.to_status !== "failed" ? "active" : e.to_status === "failed" ? "failed" : "done" },
            h("span", { class: "node" }, icon(e.cause === "operator_failover" ? "route" : e.to_status === "failed" ? "x" : "check")),
            h("div", null, h("div", { class: "row between" }, h("span", { class: "title" }, CAUSES[e.cause] || e.cause), h("span", { class: "time" }, fmt.time(e.occurred_at))),
              h("div", { class: "detail" }, e.from_status ? `${STATUSES[e.from_status]?.label || e.from_status} → ${STATUSES[e.to_status]?.label || e.to_status}` : STATUSES[e.to_status]?.label || e.to_status)))))),
      h("section", null, h("h3", null, "Remboursements"),
        p.refunds.length
          ? h("div", { class: "stack", style: { gap: "8px" } }, p.refunds.map((r) => h("div", { class: "row between callout" },
              h("span", { class: "row" }, icon("undo"), h("span", null, h("strong", null, fmt.money(r.amount)), r.reason ? h("span", { class: "subtle" }, ` · ${r.reason}`) : null)),
              h("span", { class: "subtle" }, fmt.dateTime(r.created_at)))))
          : h("p", { class: "subtle" }, "Aucun remboursement.")),
    ];
    sections.push(await ledgerSection(p));
    const s = await session().catch(() => null);
    if (s?.links?.jaeger) {
      const tags = encodeURIComponent(JSON.stringify({ "xaalis.intent_id": p.id }));
      sections.push(h("section", null, h("h3", null, "Observabilité"),
        h("a", { class: "btn sm", href: `${s.links.jaeger}/search?service=xaalis-worker&tags=${tags}&lookback=2d`, target: "_blank", rel: "noopener" },
          icon("eye"), "Voir la trace distribuée dans Jaeger", icon("external")),
        h("p", { class: "subtle", style: { fontSize: "12px", marginTop: "8px" } }, "Une seule trace relie l'API, le worker, l'anti-fraude, l'opérateur, le callback et le webhook (ADR 0014).")));
    }
    if (REFUNDABLE.has(p.status) && remaining > 0) sections.push(refundForm(p, remaining, content));
    content.replaceChildren(...sections.filter(Boolean));
  }

  // Écritures du grand livre liées au paiement : encaissement, règlement, remboursements.
  async function ledgerSection(p) {
    const references = [p.id, ...p.refunds.map((r) => r.id)];
    try {
      const pages = await Promise.all(references.map((ref) => api.get(`/ledger?scope=platform&reference=${ref}&first=10`)));
      const transactions = pages.flatMap((page) => page.data).sort((a, b) => a.created_at.localeCompare(b.created_at));
      if (!transactions.length) return h("section", null, h("h3", null, "Grand livre"), h("p", { class: "subtle" }, "Aucune écriture : un paiement n'est comptabilisé qu'une fois réussi."));
      return h("section", null, h("h3", null, "Grand livre"),
        h("div", { class: "stack", style: { gap: "8px" } }, transactions.map((tx) => h("div", { class: "callout", style: { display: "block" } },
          h("div", { class: "row between", style: { marginBottom: "6px" } }, h("strong", null, TRANSACTIONS[tx.kind] || tx.kind),
            h("span", { class: `pill ${tx.total === 0 ? "ok" : "bad"}` }, icon(tx.total === 0 ? "check" : "alert"), "Σ = ", String(tx.total))),
          h("dl", { class: "kv" }, tx.entries.flatMap((e) => [
            h("dt", null, (ACCOUNTS[e.account_kind] || { label: e.account_kind }).label),
            h("dd", { class: "num", style: { color: e.amount > 0 ? "var(--text)" : "var(--text-2)" } }, `${e.amount > 0 ? "+" : "−"}${fmt.number(Math.abs(e.amount))}`)]))))),
        h("a", { class: "link-out", href: "#/grand-livre", style: { marginTop: "8px" } }, "Ouvrir le grand livre", icon("arrowRight")));
    } catch {
      return null; // le grand livre est un complément : le détail reste lisible sans lui
    }
  }

  function refundForm(p, remaining, content) {
    let key = newIdempotencyKey();
    const input = h("input", { class: "input left", id: "refund-amount", inputmode: "numeric", value: String(remaining), "aria-describedby": "refund-help" });
    const reason = h("input", { class: "input", id: "refund-reason", maxlength: 255, placeholder: "Ex. article retourné" });
    input.addEventListener("input", () => { key = newIdempotencyKey(); });
    const submit = busyButton(h("button", { type: "submit", class: "btn primary" }, icon("undo"), "Rembourser"), async () => {
      const value = Number(input.value.replace(/\s/g, ""));
      if (!Number.isInteger(value) || value < 1 || value > remaining) {
        input.setAttribute("aria-invalid", "true");
        toast("Montant invalide", { tone: "bad", detail: `Entre 1 et ${fmt.money(remaining)} (XG-09).` });
        return;
      }
      try {
        const refund = await api.post(`/payment-intents/${p.id}/refunds`, { amount: value, reason: reason.value.trim() || undefined }, { idempotencyKey: key });
        toast(`Remboursement de ${fmt.money(refund.amount)} effectué`, { tone: "ok", detail: "Requête signée HMAC · webhook refund.succeeded envoyé" });
        key = newIdempotencyKey();
        await fillDetail(p.id, content);
        reload({ quiet: true });
      } catch (error) { problemToast(error, "Remboursement refusé"); }
    });
    return h("section", null, h("h3", null, "Nouveau remboursement"),
      h("form", { class: "stack", style: { gap: "12px" }, onsubmit: (e) => e.preventDefault() },
        h("div", { class: "field" }, h("label", { for: "refund-amount" }, "Montant"),
          h("div", { class: "input-group" }, input, h("span", { class: "addon right" }, "XOF")),
          h("span", { class: "help", id: "refund-help" }, `Au plus ${fmt.money(remaining)} restant (XG-09) · dans les 30 jours (XG-08) · commission non extournée (XG-10).`)),
        h("div", { class: "field" }, h("label", { for: "refund-reason" }, "Motif (facultatif)"), reason),
        h("div", { class: "row", style: { justifyContent: "flex-end" } }, submit)));
  }

  await reload();
  if (params[0]) openDetail(params[0]);
  const timer = setInterval(() => { if (!document.hidden && pages === 1 && !document.querySelector(".drawer")) reload({ quiet: true }); }, 5000);
  return () => { clearInterval(timer); closeLayer(); };
}
