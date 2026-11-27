// Grand livre : chaque transaction comptable avec ses écritures en débit / crédit.
// La partie double rendue visible : pour chaque transaction, total débit = total crédit.

import { api } from "../api.js";
import { ACCOUNTS, OPERATORS, TRANSACTIONS, busyButton, countUp, emptyState, fmt, h, icon, pageIntro } from "../ui.js";

let scope = "merchant";

function accountLabel(entry) {
  const meta = ACCOUNTS[entry.account_kind] || { label: entry.account_kind, color: "var(--text-3)" };
  const qualifier = entry.account.split(":")[1] || "";
  const operator = OPERATORS[qualifier];
  const label = operator ? `${meta.label} · ${operator.name}` : meta.label;
  return { label, color: meta.color, qualifier: operator ? "" : qualifier };
}

function transactionCard(tx) {
  const debit = tx.entries.filter((e) => e.amount > 0).reduce((sum, e) => sum + e.amount, 0);
  const credit = -tx.entries.filter((e) => e.amount < 0).reduce((sum, e) => sum + e.amount, 0);
  return h("article", { class: "jtx" },
    h("div", { class: "jtx-head" },
      h("span", { class: "kind" }, TRANSACTIONS[tx.kind] || tx.kind),
      h("span", { class: "ref" }, tx.reference),
      tx.reference.startsWith("pi_") ? h("a", { class: "link-out", href: `#/paiements/${tx.reference}` }, "Voir le paiement", icon("arrowRight")) : null,
      h("span", { class: "when" }, fmt.dateTime(tx.created_at))),
    h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, h("th", null, "Compte"), h("th", { class: "amt" }, "Débit"), h("th", { class: "amt" }, "Crédit"))),
      h("tbody", null, tx.entries.map((entry) => {
        const account = accountLabel(entry);
        return h("tr", null,
          h("td", null, h("span", { class: "acct" }, h("span", { class: "dot", style: { background: account.color } }), account.label,
            account.qualifier ? h("small", null, account.qualifier.slice(0, 14) + "…") : null)),
          h("td", { class: "amt" }, entry.amount > 0 ? fmt.number(entry.amount) : ""),
          h("td", { class: "amt" }, entry.amount < 0 ? fmt.number(-entry.amount) : ""));
      })),
      h("tfoot", null, h("tr", null,
        h("td", null, tx.total === 0
          ? h("span", { class: "balanced" }, icon("check"), "Équilibrée · Σ = 0")
          : h("span", { class: "pill bad" }, `Déséquilibre : ${tx.total}`)),
        h("td", { class: "amt" }, fmt.number(debit)),
        h("td", { class: "amt" }, fmt.number(credit)))))));
}

export async function render(root) {
  const list = h("div", { class: "journal" });
  const more = h("button", { type: "button", class: "btn sm", hidden: true }, "Transactions plus anciennes");
  const sum = h("div", { class: "sum-zero count-up" }, "0");
  const counts = { tx: h("span", { class: "count-up" }, "0"), entries: h("span", { class: "count-up" }, "0") };
  const health = h("div", { class: "muted", style: { fontSize: "13px" } }, "Vérification…");
  let cursor = null;
  let shown = new Set();

  const scopeBtns = [["merchant", "Ce marchand"], ["platform", "Toute la plateforme"]].map(([value, label]) =>
    h("button", { type: "button", "aria-pressed": String(scope === value), onclick: () => { scope = value; scopeBtns.forEach((b, i) => b.setAttribute("aria-pressed", String(["merchant", "platform"][i] === value))); load(); } }, label));

  root.append(
    pageIntro({
      id: "ledger",
      eyebrow: "Back-office · comptabilité",
      title: "Grand livre en partie double",
      lead: "Chaque mouvement d'argent est enregistré au moins deux fois, au débit d'un compte et au crédit d'un autre, pour un total nul. Un encaissement de 10 000 XOF devient : 10 000 à recevoir de l'opérateur, 9 820 dus au marchand, 180 de commission. La base de données elle-même refuse toute transaction déséquilibrée.",
      seen: [
        [h("strong", null, "Le journal"), " : les transactions de la plus récente à la plus ancienne, écriture par écriture."],
        [h("strong", null, "Débit = crédit"), " sur chaque transaction, vérifié et affiché."],
        [h("strong", null, "La somme de toutes les écritures"), " de la plateforme, recalculée en direct : toujours 0."],
      ],
      under: [["XG-16", " équilibre permanent"], ["Triggers", " écritures immuables"], ["Contrainte", " différée au COMMIT"], ["Entiers", " jamais de float (XG-01)"], ["ADR", " 0001"]],
      actions: [h("div", { class: "segmented", role: "group", "aria-label": "Périmètre" }, scopeBtns)],
    }),
    h("div", { class: "grid split" },
      h("section", { class: "card" },
        h("div", { class: "card-head" }, icon("book", { width: 17, height: 17 }), h("h2", null, "Journal"), h("span", { class: "hint" }, "mis à jour toutes les 3 s")),
        h("div", { class: "card-body" }, list),
        h("div", { class: "card-foot row", style: { justifyContent: "center" } }, more)),
      h("div", { class: "stack" },
        h("section", { class: "card" },
          h("div", { class: "card-head" }, icon("shield", { width: 17, height: 17 }), h("h2", null, "Invariant XG-16")),
          h("div", { class: "card-body stack", style: { gap: "10px" } },
            h("div", { class: "subtle", style: { fontSize: "12.5px" } }, "Somme de toutes les écritures de la plateforme"),
            h("div", { class: "row", style: { alignItems: "baseline", gap: "10px" } }, sum, h("span", { class: "subtle" }, "XOF")),
            health,
            h("dl", { class: "kv" },
              h("dt", null, "Transactions"), h("dd", { class: "num" }, counts.tx),
              h("dt", null, "Écritures"), h("dd", { class: "num" }, counts.entries)))),
        h("section", { class: "card" },
          h("div", { class: "card-head" }, h("h2", null, "Plan de comptes")),
          h("div", { class: "card-body" }, h("dl", { class: "kv" },
            Object.values(ACCOUNTS).flatMap((a) => [
              h("dt", null, h("span", { class: "acct row" }, h("span", { class: "dot", style: { width: "8px", height: "8px", borderRadius: "2px", background: a.color } }))),
              h("dd", { style: { textAlign: "left" } }, a.label)])))))));

  async function loadHealth() {
    try {
      const report = await api.get("/operations/ledger");
      countUp(sum, report.totals.all);
      countUp(counts.tx, report.transactions_count);
      countUp(counts.entries, report.entries_count);
      health.replaceChildren(report.balanced && report.immutability_enforced
        ? h("span", { class: "pill ok" }, icon("check"), "Équilibré · triggers d'immutabilité actifs")
        : h("span", { class: "pill bad" }, icon("alert"), "Anomalie détectée"));
    } catch (error) { health.textContent = error.title; }
  }

  async function fetchPage(after) {
    const params = new URLSearchParams({ scope, first: "12" });
    if (after) params.set("after", after);
    return api.get(`/ledger?${params}`);
  }

  async function load({ quiet = false } = {}) {
    try {
      const page = await fetchPage(null);
      if (quiet) {
        const fresh = page.data.filter((tx) => !shown.has(tx.id)).reverse();
        if (fresh.length) list.querySelector(".empty")?.remove();
        fresh.forEach((tx) => { shown.add(tx.id); list.prepend(transactionCard(tx)); });
        return;
      }
      shown = new Set(page.data.map((tx) => tx.id));
      cursor = page.next_cursor;
      more.hidden = !page.has_more;
      list.replaceChildren(...(page.data.length ? page.data.map(transactionCard)
        : [emptyState("book", "Aucune écriture pour l'instant", "Un paiement réussi crée sa première transaction comptable : lancez-en un depuis le Centre de contrôle.",
            h("a", { class: "btn sm", href: "#/accueil" }, icon("play"), "Centre de contrôle"))]));
    } catch (error) { list.replaceChildren(emptyState("alert", error.title, error.detail)); }
  }

  busyButton(more, async () => {
    const page = await fetchPage(cursor);
    page.data.forEach((tx) => { shown.add(tx.id); list.append(transactionCard(tx)); });
    cursor = page.next_cursor;
    more.hidden = !page.has_more;
  });

  await Promise.all([load(), loadHealth()]);
  const timer = setInterval(() => { if (!document.hidden) { load({ quiet: true }); loadHealth(); } }, 3000);
  return () => clearInterval(timer);
}
