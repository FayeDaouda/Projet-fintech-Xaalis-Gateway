// Back-office Xaalis : rapprochement des relevés opérateurs, règlement T+1, santé du grand livre.

import { api, session } from "../api.js";
import { amount, busyButton, emptyState, fmt, h, icon, operator, problemToast, toast } from "../ui.js";

const TYPES = {
  missing_in_ledger: { label: "Absent de nos livres", tone: "bad", injected: "extra" },
  missing_in_statement: { label: "Absent du relevé", tone: "warn", injected: "missing" },
  amount_mismatch: { label: "Montant différent", tone: "bad", injected: "amount" },
  duplicate: { label: "Doublon dans le relevé", tone: "info", injected: "duplicate" },
};
const INJECTED_TO_TYPE = Object.fromEntries(Object.entries(TYPES).map(([type, t]) => [t.injected, type]));

const addDays = (iso, n) => {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let day = null;

export async function render(root) {
  const s = await session().catch(() => null);
  day = day || s?.today || new Date().toISOString().slice(0, 10);

  const dateInput = h("input", { type: "date", class: "input", style: { width: "170px" }, value: day, "aria-label": "Jour comptable",
    onchange: (e) => { day = e.target.value; loadReport(); settleResult.replaceChildren(); } });
  const reportBody = h("div");
  const settleResult = h("div", { class: "stack" });
  const ledgerBody = h("div", { class: "card-body" });
  const exportLink = h("a", { class: "btn sm", href: "#", download: true }, icon("download"), "Export CSV");

  const runBtn = busyButton(h("button", { type: "button", class: "btn primary" }, icon("scale"), "Lancer le rapprochement"), async () => {
    try {
      await api.post("/operations/reconciliation", { date: day });
      toast("Rapprochement lancé", { tone: "info", detail: "Relevés des 3 opérateurs téléchargés et analysés par le worker (Polars)." });
      for (let i = 0; i < 30; i += 1) {
        await sleep(1000);
        const report = await loadReport();
        if (report && report.runs.every((r) => ["completed", "failed"].includes(r.status))) break;
      }
    } catch (error) { problemToast(error, "Rapprochement impossible"); }
  });

  const settleToday = busyButton(h("button", { type: "button", class: "btn" }, icon("clock"), "Régler le jour même"), () => settle(null));
  const settleNext = busyButton(h("button", { type: "button", class: "btn primary" }, icon("calendar"), "Simuler J+1 et régler"), () => settle(addDays(day, 1)));

  root.append(
    h("div", { class: "page-head" },
      h("div", null, h("h1", null, "Rapprochement & règlement T+1"), h("p", null, "Opérations du back-office Xaalis (rôle administrateur), tous marchands confondus. Les relevés du simulateur contiennent des anomalies injectées volontairement.")),
      h("div", { class: "actions" }, dateInput)),
    h("div", { class: "stack" },
      h("section", { class: "card" },
        h("div", { class: "card-head" }, h("h2", null, "Rapprochement des relevés opérateurs"),
          h("div", { class: "right" }, exportLink, runBtn)),
        reportBody),
      h("div", { class: "grid cols-2" },
        h("section", { class: "card" },
          h("div", { class: "card-head" }, h("h2", null, "Règlement T+1"), h("span", { class: "hint" }, "jours comptables de Dakar (XG-15)")),
          h("div", { class: "card-body stack" },
            h("p", { class: "muted" }, "Seuls les paiements rapprochés sans écart passent de « en attente » à « disponible ». Un règlement le jour même est refusé."),
            h("div", { class: "row wrap" }, settleToday, settleNext),
            settleResult)),
        h("section", { class: "card" },
          h("div", { class: "card-head" }, h("h2", null, "Santé du grand livre"), h("span", { class: "hint" }, "partie double (XG-16)"),
            h("div", { class: "right" }, h("button", { type: "button", class: "btn sm ghost", onclick: () => loadLedger() }, icon("refresh"), "Vérifier"))),
          ledgerBody))));

  async function settle(asOf) {
    try {
      const result = await api.post("/operations/settlement", asOf ? { date: day, as_of: asOf } : { date: day });
      const skipped = result.skipped_unreconciled.length;
      settleResult.replaceChildren(h("div", { class: `callout ${result.settled ? "ok" : "info"} fade-in` }, icon(result.settled ? "check" : "info"),
        h("div", null, h("strong", null, `${result.settled} paiement(s) réglé(s) — ${fmt.money(result.settled_amount)}`),
          h("div", null, skipped ? `${skipped} paiement(s) en écart laissé(s) en attente : un écart se traite avant de libérer les fonds.` : "Aucun paiement en écart."),
          h("div", { class: "subtle" }, result.settled
            ? "Les fonds réglés sont désormais virables depuis la vue d'ensemble."
            : "Rien à libérer : passez quelques paiements de plus avant le rapprochement (le relevé piégé n'en altère qu'une partie)."))));
      loadLedger();
    } catch (error) {
      settleResult.replaceChildren(h("div", { class: `callout ${error.status === 409 ? "warn" : "bad"} fade-in` }, icon(error.status === 409 ? "clock" : "alert"),
        h("div", null, h("strong", null, `${error.status} · ${error.code || error.title}`), h("div", null, error.detail || error.title))));
    }
  }

  async function loadReport() {
    exportLink.href = `/api/operations/reconciliation/${day}/export`;
    let report = null;
    let injected = [];
    try {
      [report, injected] = await Promise.all([
        api.get(`/operations/reconciliation/${day}`).catch((error) => { if (error.status === 404) return null; throw error; }),
        api.get(`/operations/anomalies/${day}`).then((r) => r.anomalies).catch(() => []),
      ]);
    } catch (error) {
      reportBody.replaceChildren(emptyState("alert", error.title, error.detail));
      return null;
    }
    exportLink.hidden = !report;
    if (!report) {
      reportBody.replaceChildren(emptyState("scale", `Aucun rapprochement pour le ${fmt.longDay(day)}`, "Lancez-le : la passerelle télécharge les relevés CSV des opérateurs et les compare au grand livre."));
      return null;
    }
    const detected = new Set(report.discrepancies.map((d) => `${TYPES[d.type]?.injected}:${d.operator_reference}`));
    const running = report.runs.some((r) => !["completed", "failed"].includes(r.status));
    reportBody.replaceChildren(
      h("div", { class: "card-body stack" },
        h("div", { class: "grid cols-4" },
          h("div", { class: "card kpi", style: { boxShadow: "none" } }, h("div", { class: "label" }, icon("check"), "Rapprochées sans écart"), h("div", { class: "value" }, report.matched)),
          ...Object.entries(TYPES).slice(0, 3).map(([type, meta]) =>
            h("div", { class: "card kpi", style: { boxShadow: "none" } }, h("div", { class: "label" }, h("span", { class: `pill ${meta.tone}`, style: { height: "18px" } }, h("span", { class: "dot" })), meta.label), h("div", { class: "value" }, report.summary[type] || 0)))),
        h("div", { class: "row wrap", style: { gap: "8px" } },
          report.runs.map((r) => h("span", { class: `pill ${r.status === "completed" ? "ok" : r.status === "failed" ? "bad" : "info"}` },
            h("span", { class: `swatch op-${r.operator}`, style: { width: "8px", height: "8px", borderRadius: "2px" } }),
            `${r.operator} · ${r.status === "completed" ? `${r.statement_lines} lignes` : r.status}`)),
          h("span", { class: "pill info" }, `Doublons : ${report.summary.duplicate || 0}`),
          running ? h("span", { class: "pill info" }, icon("loader", { class: "spin" }), "analyse en cours…") : null),
        injected.length ? h("div", { class: "callout" }, icon("sparkles"),
          h("div", { style: { flex: 1 } }, h("strong", null, "Anomalies injectées par le simulateur : "),
            h("div", { class: "row wrap", style: { marginTop: "6px", gap: "6px" } }, injected.map((a) => {
              const found = detected.has(`${a.type}:${a.operator_reference}`);
              return h("span", { class: `pill ${found ? "ok" : ""}`, title: a.operator_reference }, icon(found ? "check" : "clock"),
                `${TYPES[INJECTED_TO_TYPE[a.type]]?.label || a.type} · ${a.operator || ""}`);
            })))) : null),
      report.discrepancies.length
        ? h("div", { class: "table-wrap" }, h("table", { class: "data" },
            h("thead", null, h("tr", null, h("th", null, "Écart"), h("th", null, "Opérateur"), h("th", null, "Référence opérateur"), h("th", null, "Intent"), h("th", { class: "num" }, "Relevé"), h("th", { class: "num" }, "Grand livre"), h("th", { class: "num" }, "Occur."))),
            h("tbody", null, report.discrepancies.map((d) => h("tr", null,
              h("td", null, h("span", { class: `pill ${TYPES[d.type]?.tone || ""}` }, TYPES[d.type]?.label || d.type)),
              h("td", null, operator(d.operator)),
              h("td", { class: "mono muted" }, d.operator_reference),
              h("td", { class: "mono subtle" }, d.intent_id || "—"),
              h("td", { class: "num" }, d.statement_amount == null ? "—" : amount(d.statement_amount, { cur: false })),
              h("td", { class: "num" }, d.ledger_amount == null ? "—" : amount(d.ledger_amount, { cur: false })),
              h("td", { class: "num" }, d.occurrences))))))
        : h("div", { class: "card-foot" }, h("span", { class: "muted" }, "Aucun écart détecté.")));
    return report;
  }

  async function loadLedger() {
    try {
      const health = await api.get("/operations/ledger");
      ledgerBody.replaceChildren(h("div", { class: "stack" },
        h("div", { class: `callout ${health.balanced ? "ok" : "bad"}` }, icon(health.balanced ? "shield" : "alert"),
          h("div", null, h("strong", null, health.balanced ? "Grand livre équilibré" : "Déséquilibre détecté"),
            h("div", null, `Somme de toutes les écritures : ${fmt.number(health.totals.all)} XOF`))),
        h("dl", { class: "kv" },
          h("dt", null, "Transactions"), h("dd", { class: "num" }, fmt.number(health.transactions_count)),
          h("dt", null, "Écritures"), h("dd", { class: "num" }, fmt.number(health.entries_count)),
          h("dt", null, "Transactions déséquilibrées"), h("dd", { class: "num" }, health.unbalanced_transactions.length))));
    } catch (error) { ledgerBody.replaceChildren(emptyState("alert", error.title, error.detail)); }
  }

  await Promise.all([loadReport(), loadLedger()]);
  return () => {};
}
