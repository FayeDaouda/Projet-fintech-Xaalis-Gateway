// Centre de contrôle : la plateforme en direct. Chaque paiement est suivi de service en
// service sur la carte (boutique → API → outbox → worker → anti-fraude → opérateur →
// callback → grand livre → webhook → marchand), à partir de l'état réel de la passerelle.

import { api, newIdempotencyKey, session } from "../api.js";
import { OPERATORS, busyButton, countUp, fmt, h, icon, pageIntro, problemToast, s, toast } from "../ui.js";

const W = 150, H = 58;
const TOP = 46, BOTTOM = 196;
const X = [20, 215, 410, 605, 800];
const NODES = [
  { id: "shop", title: "Boutique", sub: "Maison Sandaga", icon: "store", x: X[0], y: TOP },
  { id: "api", title: "API Xaalis", sub: "FastAPI · REST", icon: "cpu", x: X[1], y: TOP },
  { id: "outbox", title: "Outbox", sub: "même transaction", icon: "database", x: X[2], y: TOP },
  { id: "worker", title: "Worker", sub: "tâches asynchrones", icon: "zap", x: X[3], y: TOP },
  { id: "risk", title: "Anti-fraude", sub: "gRPC · 150 ms", icon: "shield", x: X[4], y: TOP },
  { id: "operator", title: "Opérateur", sub: "débit mobile money", icon: "phone", x: X[4], y: BOTTOM },
  { id: "callback", title: "Callback", sub: "signé HMAC", icon: "lock", x: X[3], y: BOTTOM },
  { id: "ledger", title: "Grand livre", sub: "partie double · Σ 0", icon: "book", x: X[2], y: BOTTOM },
  { id: "webhook", title: "Webhook", sub: "signé · 8 essais", icon: "webhook", x: X[1], y: BOTTOM },
  { id: "merchant", title: "Marchand", sub: "vérifie, déduplique", icon: "inbox", x: X[0], y: BOTTOM },
];
// Segments : index de départ → index d'arrivée, dans l'ordre du parcours.
const EDGES = NODES.slice(0, -1).map((_, i) => [i, i + 1]);
const STAGE = {
  submitted: [0, 1, 2, 3],   // boutique → API → outbox → worker → anti-fraude
  charged: [4, 5, 6],        // anti-fraude → opérateur → callback → grand livre
  operatorOnly: [4, 5],      // anti-fraude → opérateur → callback (échec opérateur)
  notified: [7, 8],          // grand livre → webhook → marchand
};

function edgePath([a, b]) {
  const from = NODES[a], to = NODES[b];
  if (from.y === to.y) {
    const y = from.y + H / 2;
    return from.x < to.x ? `M${from.x + W},${y} L${to.x},${y}` : `M${from.x},${y} L${to.x + W},${y}`;
  }
  const cx = from.x + W / 2;
  return `M${cx},${from.y + H} L${cx},${to.y}`;
}

function buildMap() {
  const svg = s("svg", { class: "map", viewBox: "0 0 970 272", role: "img", "aria-label": "Carte du système Xaalis en direct" });
  svg.append(
    s("text", { class: "zone-label", x: 20, y: 28 }, "ALLER · DE LA BOUTIQUE À L'OPÉRATEUR"),
    s("text", { class: "zone-label", x: 20, y: 182 }, "RETOUR · DE L'OPÉRATEUR AU MARCHAND"),
  );
  const paths = EDGES.map((edge) => {
    const d = edgePath(edge);
    svg.append(s("path", { class: "edge", d }), s("path", { class: "edge-flow", d }));
    return s("path", { d, fill: "none", stroke: "none" });
  });
  paths.forEach((p) => svg.append(p));
  const nodes = NODES.map((n) => {
    const count = s("text", { class: "count", x: n.x + W - 12, y: n.y + 20, "text-anchor": "end" }, "");
    const g = s("g", { class: "node" },
      s("rect", { x: n.x, y: n.y, width: W, height: H, rx: 12 }),
      s("g", { class: "ico", transform: `translate(${n.x + 13},${n.y + 17}) scale(0.95)` },
        s("svg", { width: 24, height: 24, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 1.8, "stroke-linecap": "round", "stroke-linejoin": "round" },
          icon(n.icon).firstChild.cloneNode(true))),
      s("text", { class: "title", x: n.x + 44, y: n.y + 26 }, n.title),
      s("text", { class: "sub", x: n.x + 44, y: n.y + 42 }, n.sub),
      count);
    svg.append(g);
    return { g, count, n: 0 };
  });
  const pulses = s("g");
  svg.append(pulses);
  return { svg, paths, nodes, pulses };
}

export async function render(root) {
  const map = buildMap();
  const feed = h("ul", { class: "feed", "aria-live": "polite" });
  const stats = {
    volume: h("span", { class: "count-up" }, "0"),
    payments: h("span", { class: "count-up" }, "0"),
    rate: h("span", { class: "count-up" }, "—"),
    net: h("span", { class: "count-up" }, "0"),
  };
  const statFoot = { payments: h("div", { class: "foot" }, "aujourd'hui, heure de Dakar"), rate: h("div", { class: "foot" }, "paiements arrivés à une issue") };
  let alive = true;
  const s0 = await session().catch(() => null);
  const links = s0?.links || {};

  // ------------------------------------------------------------ animation de la carte
  function flash(index, tone = "hit") {
    const node = map.nodes[index];
    node.g.classList.remove("hit", "hit-bad", "hit-gold");
    void node.g.getBBox();
    node.g.classList.add(tone);
    setTimeout(() => node.g.classList.remove(tone), 900);
  }

  function bump(index) {
    const node = map.nodes[index];
    node.n += 1;
    node.count.textContent = `×${node.n}`;
  }

  function travel(edgeIndex, color) {
    return new Promise((resolve) => {
      const path = map.paths[edgeIndex];
      const length = path.getTotalLength();
      const dot = s("circle", { r: 5.5, class: "pulse", fill: color, style: `color:${color}` });
      map.pulses.append(dot);
      const duration = matchMedia("(prefers-reduced-motion: reduce)").matches ? 1 : 520;
      const start = performance.now();
      const step = (now) => {
        const t = Math.min(1, (now - start) / duration);
        const p = path.getPointAtLength(length * (1 - (1 - t) ** 2));
        dot.setAttribute("cx", p.x);
        dot.setAttribute("cy", p.y);
        if (t < 1 && alive) requestAnimationFrame(step);
        else { dot.remove(); resolve(); }
      };
      requestAnimationFrame(step);
    });
  }

  let chain = Promise.resolve();
  function run(edges, { color = "var(--brand)", end = "hit" } = {}) {
    chain = chain.then(async () => {
      if (!alive) return;
      if (edges.length) flash(EDGES[edges[0]][0], "hit");
      for (const e of edges) {
        await travel(e, color);
        const target = EDGES[e][1];
        bump(target);
        flash(target, e === edges.at(-1) ? end : "hit");
      }
    });
  }

  // ------------------------------------------------------------ fil d'activité
  function feedItem({ tone, iconName, text, detail, at }) {
    return h("li", null,
      h("span", { class: `fi ${tone}` }, icon(iconName)),
      h("div", { class: "ft" }, text, detail ? h("small", null, detail) : null),
      h("span", { class: "fw" }, fmt.time(at || new Date().toISOString())));
  }
  function push(item) {
    feed.prepend(feedItem(item));
    while (feed.children.length > 40) feed.lastChild.remove();
  }

  const opName = (code) => OPERATORS[code]?.name || code || "opérateur";
  function describe(p, status) {
    const amount = fmt.money(p.amount);
    if (status === "processing") return { tone: "info", iconName: "send", text: `Paiement de ${amount} confirmé`, detail: `${p.id} · ${p.payer_phone}`, at: p.created_at };
    if (status === "succeeded") return { tone: "ok", iconName: "check", text: `Paiement réussi via ${opName(p.operator)} · ${amount}`, detail: `commission ${fmt.money(p.fee)} · anti-fraude ${p.risk_score ?? "repli"} ‰`, at: p.succeeded_at };
    if (status === "failed" && p.risk_decision === "decline") return { tone: "bad", iconName: "shield", text: `Bloqué par l'anti-fraude · ${amount}`, detail: `score ${p.risk_score} ‰ · aucun débit demandé` };
    if (status === "failed") return { tone: "bad", iconName: "x", text: `Refusé par ${opName(p.operator)} · ${amount}`, detail: p.id };
    if (status === "partially_refunded" || status === "refunded") return { tone: "gold", iconName: "undo", text: `Remboursement de ${fmt.money(p.amount_refunded)}`, detail: p.id };
    if (status === "created") return { tone: "", iconName: "bag", text: `Commande de ${amount} créée`, detail: p.id, at: p.created_at };
    return null;
  }

  // ------------------------------------------------------------ suivi des changements
  const known = new Map();   // intent → statut déjà vu
  const delivered = new Set(); // événements webhook traités
  let primed = false;

  function onPayment(p) {
    const before = known.get(p.id);
    if (before === p.status) return;
    known.set(p.id, p.status);
    if (!primed) return;
    const item = describe(p, p.status);
    if (item) push(item);
    const fresh = before === undefined || before === "created";
    if (p.status === "processing" && fresh) run(STAGE.submitted);
    else if (p.status === "succeeded") {
      run(fresh ? [...STAGE.submitted, ...STAGE.charged] : STAGE.charged, { end: "hit" });
    } else if (p.status === "failed" && p.risk_decision === "decline") {
      run(fresh ? STAGE.submitted : [], {});
      chain = chain.then(() => { flash(4, "hit-bad"); });
    } else if (p.status === "failed") {
      run(fresh ? [...STAGE.submitted, ...STAGE.operatorOnly] : STAGE.operatorOnly, { color: "var(--bad)", end: "hit-bad" });
    }
  }

  function onWebhook(e) {
    const ok = e.processed;
    const key = `${e.event_id}:${e.attempts.length}`;
    if (delivered.has(key)) return;
    delivered.add(key);
    if (!primed) return;
    const last = e.attempts.at(-1);
    if (ok && last?.outcome === "accepted") {
      push({ tone: "gold", iconName: "webhook", text: `Webhook ${e.type} livré et vérifié`, detail: `${e.event_id} · tentative ${e.attempts.length}`, at: last.at });
      run(STAGE.notified, { color: "var(--gold)", end: "hit-gold" });
    } else if (last?.outcome === "unavailable") {
      push({ tone: "bad", iconName: "alert", text: `Webhook refusé par le marchand (503)`, detail: `${e.type} · tentative ${e.attempts.length}/8`, at: last.at });
    }
  }

  async function poll() {
    try {
      const activity = await api.get("/activity");
      [...activity.payments].reverse().forEach(onPayment);
      [...activity.webhooks].reverse().forEach(onWebhook);
      if (!primed) {
        primed = true;
        const history = activity.payments.slice(0, 8).reverse();
        history.forEach((p) => { const item = describe(p, p.status); if (item) push(item); });
        if (!history.length) feed.append(h("li", null, h("span", { class: "fi" }, icon("activity")), h("div", { class: "ft" }, "En attente du premier paiement", h("small", null, "Lancez un paiement de démonstration ci-dessus")), h("span")));
      }
    } catch { /* la pastille d'état de la passerelle signale déjà l'incident */ }
  }

  async function refreshStats() {
    try {
      const o = await api.get("/overview?days=1");
      const payments = o.daily_volumes.reduce((n, r) => n + r.payments, 0);
      const succeeded = o.daily_volumes.reduce((n, r) => n + r.succeeded, 0);
      const finished = o.success_rates.reduce((n, r) => n + r.finished, 0);
      const ok = o.success_rates.reduce((n, r) => n + r.succeeded, 0);
      countUp(stats.volume, o.revenue.gross);
      countUp(stats.payments, succeeded);
      statFoot.payments.textContent = `${succeeded} réussi(s) sur ${payments} confirmé(s) aujourd'hui`;
      if (finished) countUp(stats.rate, Math.floor((ok * 1000) / finished), { format: (v) => fmt.permille(v) });
      countUp(stats.net, o.revenue.net);
    } catch { /* indicateurs facultatifs */ }
  }

  // ------------------------------------------------------------ actions de démonstration
  async function demo(kind) {
    try {
      await api.post("/demo/payment", { kind }, { idempotencyKey: newIdempotencyKey() });
    } catch (error) { problemToast(error, "Paiement de démonstration impossible"); }
  }
  const launch = busyButton(h("button", { type: "button", class: "btn primary" }, icon("play"), "Lancer un paiement"), () => demo("normal"));
  const burst = busyButton(h("button", { type: "button", class: "btn" }, icon("layers"), "Rafale × 5"), async () => {
    for (let i = 0; i < 5; i += 1) { await demo("normal"); await new Promise((r) => setTimeout(r, 350)); }
    toast("5 paiements lancés", { tone: "info", detail: "Suivez-les sur la carte et dans le fil d'activité." });
  });
  const suspect = busyButton(h("button", { type: "button", class: "btn danger" }, icon("shield"), "Paiement suspect"), () => demo("suspect"));

  const stat = (label, iconName, el, unit, foot) => h("div", { class: "card stat" },
    h("div", { class: "label" }, icon(iconName), label),
    h("div", { class: "value" }, el, unit ? h("small", null, unit) : null), foot);

  root.append(
    pageIntro({
      id: "home",
      eyebrow: "Centre de contrôle",
      title: "La passerelle en direct",
      lead: "Chaque paiement traverse ici l'ensemble de la plateforme : réception par l'API, file transactionnelle, évaluation anti-fraude, débit chez l'opérateur, écriture au grand livre et notification signée du marchand. Rien n'est simulé côté Xaalis : la carte reflète l'état réel de la base.",
      seen: [
        [h("strong", null, "La carte du système"), " : un point lumineux suit chaque paiement de service en service."],
        [h("strong", null, "Le fil d'activité"), " : chaque événement métier, au moment où il se produit."],
        [h("strong", null, "Les indicateurs du jour"), ", calculés en jour comptable de Dakar."],
      ],
      under: [["Outbox", " transactionnel"], ["gRPC", " 150 ms"], ["HMAC", " callbacks et webhooks"], ["XG-16", " Σ écritures = 0"], ["OpenTelemetry", " traces"]],
      actions: [suspect, burst, launch],
    }),
    h("div", { class: "stack" },
      h("div", { class: "hero-stats" },
        stat("Volume encaissé", "wallet", stats.volume, "XOF", h("div", { class: "foot" }, "paiements réussis aujourd'hui")),
        stat("Paiements réussis", "check", stats.payments, "", statFoot.payments),
        stat("Taux de succès", "gauge", stats.rate, "", statFoot.rate),
        stat("Revenu net marchand", "sparkles", stats.net, "XOF", h("div", { class: "foot" }, "brut − commissions − remboursements"))),
      h("div", { class: "stack" },
        h("section", { class: "card map-card" },
          h("div", { class: "card-head" }, icon("activity", { width: 17, height: 17 }), h("h2", null, "Carte du système"),
            h("span", { class: "right pill ok live" }, h("span", { class: "dot" }), "en direct")),
          h("div", { class: "map-wrap" }, map.svg),
          h("div", { class: "map-legend" },
            h("span", null, h("i", { style: { background: "var(--brand)" } }), "paiement"),
            h("span", null, h("i", { style: { background: "var(--gold)" } }), "webhook"),
            h("span", null, h("i", { style: { background: "var(--bad)" } }), "refus"),
            h("span", { style: { marginLeft: "auto" } }, "Compteurs : passages depuis l'ouverture de la page"))),
        h("section", { class: "card" },
          h("div", { class: "card-head" }, icon("radio", { width: 17, height: 17 }), h("h2", null, "Activité"), h("span", { class: "hint" }, "chaque événement métier, au moment où il se produit")),
          feed)),
      h("section", { class: "card stack-strip", "aria-label": "Plateforme" },
        h("span", null, h("b", null, "Stack"), " FastAPI · PostgreSQL 16 · Redis · gRPC · OpenTelemetry"),
        h("span", { class: "sep" }),
        h("span", null, "Opérateurs : ", Object.values(OPERATORS).map((o) => o.name).join(" · ")),
        h("span", { class: "sep" }),
        links.grafana ? h("a", { href: links.grafana, target: "_blank", rel: "noopener" }, icon("gauge"), "Grafana", icon("external")) : null,
        links.jaeger ? h("a", { href: links.jaeger, target: "_blank", rel: "noopener" }, icon("eye"), "Traces Jaeger", icon("external")) : null,
        links.docs ? h("a", { href: links.docs, target: "_blank", rel: "noopener" }, icon("book"), "Contrat OpenAPI", icon("external")) : null)),
  );

  await Promise.all([poll(), refreshStats()]);
  const fast = setInterval(() => { if (!document.hidden) poll(); }, 1000);
  const slow = setInterval(() => { if (!document.hidden) refreshStats(); }, 4000);
  return () => { alive = false; clearInterval(fast); clearInterval(slow); };
}
