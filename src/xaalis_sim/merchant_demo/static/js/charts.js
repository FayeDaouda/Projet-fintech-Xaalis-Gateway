// Graphiques SVG sans dépendance. Couleur = identité de l'opérateur (fixe, jamais selon le
// rang) ; légende toujours présente ; infobulle au survol ; vue tableau équivalente.

import { OPERATORS, fmt, h, s } from "./ui.js";

const ORDER = ["gopay", "teranga", "ndiaga"];

function niceMax(value) {
  if (value <= 0) return 1000;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  return [1, 2, 2.5, 5, 10].find((x) => x * magnitude >= value) * magnitude;
}

function compact(n) {
  if (n >= 1_000_000) return `${fmt.number(Math.round(n / 100_000) / 10)} M`;
  if (n >= 1_000) return `${fmt.number(Math.round(n / 100) / 10)} k`;
  return fmt.number(n);
}

export function legend() {
  return h("div", { class: "legend", "aria-hidden": "true" },
    ORDER.map((op) => h("span", { class: `op op-${op}` }, h("span", { class: "swatch" }), OPERATORS[op].name)));
}

// days : [{ date, byOperator: { gopay: { volume, payments, succeeded }, … } }]
export function stackedBars(days) {
  const W = 640, H = 220, padL = 44, padR = 8, padT = 12, padB = 26;
  const totals = days.map((d) => ORDER.reduce((sum, op) => sum + (d.byOperator[op]?.volume || 0), 0));
  const max = niceMax(Math.max(...totals, 0) * 1.08);
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const slot = plotW / Math.max(days.length, 1);
  const barW = Math.min(36, slot * 0.56);
  const y = (v) => padT + plotH - (v / max) * plotH;

  const wrap = h("div", { class: "chart" });
  const tooltip = h("div", { class: "tooltip", hidden: true });
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Volume encaissé par jour et par opérateur" });

  for (let i = 0; i <= 4; i += 1) {
    const value = (max / 4) * i;
    svg.append(
      s("line", { class: "grid-line", x1: padL, x2: W - padR, y1: y(value), y2: y(value), "stroke-dasharray": i === 0 ? null : "2 4" }),
      s("text", { class: "axis-label", x: padL - 8, y: y(value) + 4, "text-anchor": "end" }, compact(value)),
    );
  }

  days.forEach((day, index) => {
    const cx = padL + slot * index + slot / 2;
    const col = s("g", { class: "col" });
    let base = 0;
    const present = ORDER.filter((op) => (day.byOperator[op]?.volume || 0) > 0);
    present.forEach((op, k) => {
      const v = day.byOperator[op].volume;
      const top = y(base + v), bottom = y(base);
      const gap = k > 0 ? 2 : 0; // séparation de 2 px entre segments empilés
      const height = Math.max(bottom - top - gap, 1);
      const isTop = k === present.length - 1;
      const r = isTop ? Math.min(4, height / 2, barW / 2) : 0;
      const x = cx - barW / 2, yy = top;
      // Extrémité arrondie seulement en haut de la pile ; base droite, ancrée sur l'axe.
      const d = r
        ? `M${x},${yy + height} V${yy + r} Q${x},${yy} ${x + r},${yy} H${x + barW - r} Q${x + barW},${yy} ${x + barW},${yy + r} V${yy + height} Z`
        : `M${x},${yy + height} V${yy} H${x + barW} V${yy + height} Z`;
      col.append(s("path", { class: "bar", d, fill: `var(--op-${op})` }));
      base += v;
    });
    col.append(s("text", { class: "axis-label", x: cx, y: H - 8, "text-anchor": "middle" }, fmt.day(day.date)));
    const hit = s("rect", { class: "hit", x: cx - slot / 2, y: padT, width: slot, height: plotH, rx: 6 });
    col.prepend(hit);
    hit.addEventListener("mouseenter", () => {
      wrap.classList.add("hovering");
      col.classList.add("on");
      tooltip.replaceChildren(
        h("div", { class: "tt-title" }, fmt.longDay(day.date)),
        ...ORDER.map((op) => h("div", { class: "tt-row" }, h("span", { class: `op op-${op}` }, h("span", { class: "swatch" }), OPERATORS[op].name),
          h("span", { class: "num" }, fmt.money(day.byOperator[op]?.volume || 0)))),
        h("div", { class: "tt-row tt-total" }, h("span", null, "Total"), h("span", { class: "num" }, fmt.money(totals[index]))));
      tooltip.hidden = false;
      const rect = svg.getBoundingClientRect();
      const scale = rect.width / W;
      tooltip.style.left = `${Math.min(Math.max(cx * scale, 90), rect.width - 90)}px`;
      tooltip.style.top = `${Math.max(y(totals[index]) * scale, 70)}px`;
    });
    hit.addEventListener("mouseleave", () => { wrap.classList.remove("hovering"); col.classList.remove("on"); tooltip.hidden = true; });
    svg.append(col);
  });

  wrap.append(svg, tooltip);
  return wrap;
}

export function volumesTable(days) {
  return h("div", { class: "table-wrap" }, h("table", { class: "data" },
    h("thead", null, h("tr", null, h("th", null, "Jour"), ORDER.map((op) => h("th", { class: "num" }, OPERATORS[op].name)), h("th", { class: "num" }, "Total"))),
    h("tbody", null, days.map((d) => h("tr", null, h("td", null, fmt.longDay(d.date)),
      ORDER.map((op) => h("td", { class: "num" }, fmt.number(d.byOperator[op]?.volume || 0))),
      h("td", { class: "num" }, h("strong", null, fmt.number(ORDER.reduce((sum, op) => sum + (d.byOperator[op]?.volume || 0), 0)))))))));
}

// rates : [{ operator, finished, succeeded, success_rate_permille }]
export function successRates(rates) {
  const byOp = Object.fromEntries(rates.map((r) => [r.operator, r]));
  return h("div", { class: "stack", style: { gap: "14px" } }, ORDER.map((op) => {
    const r = byOp[op];
    const permille = r?.success_rate_permille ?? 0;
    return h("div", null,
      h("div", { class: "row between", style: { marginBottom: "6px" } },
        h("span", { class: `op op-${op}` }, h("span", { class: "swatch" }), OPERATORS[op].name),
        r ? h("span", { class: "num" }, h("strong", null, fmt.permille(permille)), h("span", { class: "subtle" }, ` · ${r.succeeded}/${r.finished}`))
          : h("span", { class: "subtle" }, "aucun paiement")),
      h("div", { class: "meter", role: "meter", "aria-valuemin": 0, "aria-valuemax": 1000, "aria-valuenow": permille, "aria-label": `Taux de succès ${OPERATORS[op].name}` },
        h("i", { style: { width: `${permille / 10}%`, background: `var(--op-${op})` } })));
  }));
}
