// Vue d'ensemble : revenus (GraphQL), volumes par opérateur, taux de succès, soldes, virement.

import { api, newIdempotencyKey } from "../api.js";
import { legend, stackedBars, successRates, volumesTable } from "../charts.js";
import { amount, busyButton, confirmDialog, emptyState, fmt, h, icon, operator, problemToast, statusPill, toast, pageIntro } from "../ui.js";

let days = 7;
let showTable = false;

function kpi(label, iconName, value, foot) {
  return h("div", { class: "card kpi" },
    h("div", { class: "label" }, icon(iconName), label),
    h("div", { class: "value" }, fmt.number(value), h("span", { class: "cur" }, "XOF")),
    h("div", { class: "foot" }, foot));
}

function series(data) {
  const byDay = new Map();
  const { from, to } = data.period;
  for (let d = new Date(`${from}T00:00:00Z`); d <= new Date(`${to}T00:00:00Z`); d.setUTCDate(d.getUTCDate() + 1)) {
    byDay.set(d.toISOString().slice(0, 10), { date: d.toISOString().slice(0, 10), byOperator: {} });
  }
  for (const row of data.daily_volumes) {
    const day = byDay.get(row.date);
    if (day) day.byOperator[row.operator] = row;
  }
  return [...byDay.values()];
}

export async function render(root) {
  const body = h("div", { class: "stack" });
  const periodBtns = [7, 30].map((n) => h("button", { type: "button", "aria-pressed": String(days === n), onclick: () => { days = n; periodBtns.forEach((b, i) => b.setAttribute("aria-pressed", String([7, 30][i] === n))); load(); } }, `${n} jours`));
  root.append(
pageIntro({
      id: "overview",
      eyebrow: "Piloter · tableau de bord",
      title: "Vue d'ensemble de l'activité",
      lead: "Les indicateurs du marchand, calculés par l'API GraphQL de reporting en jours comptables de Dakar : volume encaissé, commissions, remboursements, revenu net, répartition par opérateur et soldes issus du grand livre.",
      seen: [
        "Les volumes par jour et par opérateur ; survolez une barre pour le détail, ou basculez en tableau.",
        "Le taux de succès de chaque opérateur, en pour mille entiers.",
        "Les soldes en attente, disponibles et virés, et le virement des fonds disponibles.",
      ],
      under: [["GraphQL", " Strawberry"], ["DataLoader", " sans N+1"], ["XG-15", " jours de Dakar"], ["XG-01", " montants entiers"], ["ADR", " 0011"]],
      actions: [h("div", { class: "segmented", role: "group", "aria-label": "Période" }, periodBtns), h("button", { type: "button", class: "btn icon", "aria-label": "Actualiser", onclick: () => load() }, icon("refresh"))],
    }),
    body);

  body.append(h("div", { class: "grid cols-4" }, Array.from({ length: 4 }, () => h("div", { class: "card kpi" }, h("span", { class: "skeleton", style: { width: "50%", height: "14px" } }, "."), h("span", { class: "skeleton", style: { width: "70%", height: "28px" } }, ".")))));

  async function payout() {
    const ok = await confirmDialog({ title: "Virer les fonds disponibles ?", body: "Le solde disponible (paiements réglés à T+1) est viré sur le compte du marchand. La requête est signée HMAC et idempotente.", confirmLabel: "Virer" });
    if (!ok) return;
    try {
      const result = await api.post("/payouts", {}, { idempotencyKey: newIdempotencyKey() });
      toast(`Virement de ${fmt.money(result.amount)} effectué`, { tone: "ok", detail: result.id });
      load();
    } catch (error) {
      if (error.code === "insufficient_balance") toast("Aucun fonds disponible", { tone: "info", detail: "Les paiements deviennent virables après le règlement T+1 (back-office)." });
      else problemToast(error, "Virement impossible");
    }
  }

  async function load({ quiet = false } = {}) {
    let data;
    try {
      data = await api.get(`/overview?days=${days}`);
    } catch (error) {
      if (!quiet) { body.replaceChildren(h("div", { class: "card" }, emptyState("alert", error.title, error.detail))); }
      return;
    }
    const { revenue, balance } = data;
    const dayRows = series(data);
    const totalPayments = data.daily_volumes.reduce((sum, r) => sum + r.payments, 0);
    const balanceTotal = balance.pending + balance.available + balance.paid_out || 1;
    const payBtn = busyButton(h("button", { type: "button", class: "btn primary block", disabled: balance.available <= 0 }, icon("send"), "Virer les fonds disponibles"), payout);

    body.replaceChildren(
      h("div", { class: "grid cols-4" },
        kpi("Volume encaissé", "wallet", revenue.gross, `${totalPayments} paiement(s) confirmé(s) sur ${days} jours`),
        kpi("Commissions Xaalis", "layers", revenue.fees, revenue.gross ? `${fmt.permille(Math.round((revenue.fees * 1000) / revenue.gross))} du volume (XG-04)` : "—"),
        kpi("Remboursé", "undo", revenue.refunded, "Remboursements partiels et totaux"),
        kpi("Revenu net", "sparkles", revenue.net, "Brut − commissions − remboursements")),
      h("div", { class: "grid split" },
        h("section", { class: "card" },
          h("div", { class: "card-head" }, h("h2", null, "Volume par jour et par opérateur"),
            h("div", { class: "right" }, h("button", { type: "button", class: "btn sm ghost", "aria-pressed": String(showTable), onclick: () => { showTable = !showTable; load({ quiet: true }); } }, icon(showTable ? "dashboard" : "list"), showTable ? "Graphique" : "Tableau"))),
          h("div", { class: "card-body stack" }, totalPayments ? [legend(), showTable ? volumesTable(dayRows) : stackedBars(dayRows)]
            : emptyState("dashboard", "Aucun paiement sur la période", "Passez une commande dans la boutique : elle apparaîtra ici.", h("a", { class: "btn sm", href: "#/boutique" }, icon("store"), "Ouvrir la boutique")))),
        h("section", { class: "card" },
          h("div", { class: "card-head" }, h("h2", null, "Soldes"), h("span", { class: "hint" }, "grand livre")),
          h("div", { class: "card-body stack" },
            h("div", null, h("div", { class: "subtle", style: { fontSize: "12.5px" } }, "Disponible au virement"),
              h("div", { class: "kpi", style: { padding: 0 } }, h("div", { class: "value" }, fmt.number(balance.available), h("span", { class: "cur" }, "XOF")))),
            h("div", { class: "balance-bar", "aria-hidden": "true" },
              h("i", { style: { flexGrow: balance.pending / balanceTotal, background: "var(--warn)" } }),
              h("i", { style: { flexGrow: balance.available / balanceTotal, background: "var(--brand)" } }),
              h("i", { style: { flexGrow: balance.paid_out / balanceTotal, background: "var(--text-3)" } })),
            h("dl", { class: "kv" },
              h("dt", null, h("span", { class: "row" }, h("span", { class: "swatch", style: { width: "9px", height: "9px", borderRadius: "3px", background: "var(--warn)" } }), "En attente (T+1)")), h("dd", null, amount(balance.pending)),
              h("dt", null, h("span", { class: "row" }, h("span", { class: "swatch", style: { width: "9px", height: "9px", borderRadius: "3px", background: "var(--brand)" } }), "Disponible")), h("dd", null, amount(balance.available)),
              h("dt", null, h("span", { class: "row" }, h("span", { class: "swatch", style: { width: "9px", height: "9px", borderRadius: "3px", background: "var(--text-3)" } }), "Déjà viré")), h("dd", null, amount(balance.paid_out))),
            payBtn))),
      h("div", { class: "grid cols-2" },
        h("section", { class: "card" },
          h("div", { class: "card-head" }, h("h2", null, "Taux de succès par opérateur"), h("span", { class: "hint" }, "en ‰, entiers")),
          h("div", { class: "card-body" }, successRates(data.success_rates))),
        h("section", { class: "card" },
          h("div", { class: "card-head" }, h("h2", null, "Derniers paiements"), h("a", { class: "right btn sm ghost", href: "#/paiements" }, "Tout voir", icon("arrowRight"))),
          data.recent.nodes.length
            ? h("div", { class: "table-wrap" }, h("table", { class: "data" }, h("tbody", null, data.recent.nodes.map((p) =>
                h("tr", { class: "clickable", onclick: () => { location.hash = `#/paiements/${p.id}`; } },
                  h("td", null, statusPill(p.status)), h("td", null, operator(p.operator)), h("td", { class: "subtle" }, fmt.dateTime(p.created_at)), h("td", { class: "num" }, amount(p.amount)))))))
            : emptyState("list", "Aucun paiement", "Les paiements de la boutique s'afficheront ici."))),
    );
  }

  await load();
  const timer = setInterval(() => { if (!document.hidden && !document.querySelector(".modal")) load({ quiet: true }); }, 8000);
  return () => clearInterval(timer);
}
