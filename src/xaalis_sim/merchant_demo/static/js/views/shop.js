// Boutique : panier, paiement Mobile Money, suivi en direct par SSE jusqu'au reçu.

import { api, newIdempotencyKey } from "../api.js";
import { CAUSES, OPERATORS, amount, copyButton, fmt, h, icon, operator, problemToast, s } from "../ui.js";

const PRODUCTS = [
  { id: "wax", name: "Pagne wax Ndar", desc: "Coton imprimé, 6 yards, motifs de Saint-Louis.", price: 12500, art: "art-wax", tag: "Best-seller",
    glyph: "M22 30h56l-6 42H28zM22 30c10 8 46 8 56 0M31 52h38" },
  { id: "bissap", name: "Bissap premium", desc: "Fleurs d'hibiscus de Kaolack, infusion à froid, 1 L.", price: 3500, art: "art-bissap",
    glyph: "M42 18h16v12l8 10v38a6 6 0 0 1-6 6H40a6 6 0 0 1-6-6V40l8-10zM34 56h32" },
  { id: "cafe", name: "Café Touba", desc: "Torréfié au djar et au poivre de Guinée, 500 g.", price: 4000, art: "art-cafe",
    glyph: "M24 42h44v16a18 18 0 0 1-18 18h-8a18 18 0 0 1-18-18zM68 48h5a8 8 0 0 1 0 16h-5M38 20c-4 6 4 9 0 15M52 20c-4 6 4 9 0 15" },
  { id: "panier", name: "Panier tressé de Thiès", desc: "Fibres de rônier tressées à la main.", price: 15000, art: "art-panier", tag: "Artisanal",
    glyph: "M18 46h64l-8 30H26zM31 46l12-22M69 46L57 24M26 60h48" },
  { id: "sandales", name: "Sandales cuir Ngor", desc: "Cuir tanné végétal, semelle cousue.", price: 18000, art: "art-sandales",
    glyph: "M32 18c9 0 13 11 13 31s-4 31-13 31-12-13-12-31 3-31 12-31zM20 46h25M68 18c9 0 12 13 12 31s-3 31-12 31-13-11-13-31 4-31 13-31zM55 46h25" },
  { id: "thiouraye", name: "Thiouraye de Dakar", desc: "Encens traditionnel, bois et résines, 250 g.", price: 6500, art: "art-encens",
    glyph: "M22 58h56a28 15 0 0 1-56 0zM40 50c-4-8 6-12 2-20M52 48c-4-8 6-12 2-22M64 50c-4-7 4-10 2-17" },
];

const cart = new Map(); // id → quantité ; conservé d'une vue à l'autre
let phone = "";
let checkout = null; // paiement en cours ou terminé
let repaint = () => {}; // rendu de la vue affichée (le flux SSE survit à la navigation)

const total = () => PRODUCTS.reduce((sum, p) => sum + p.price * (cart.get(p.id) || 0), 0);
const digits = (value) => value.replace(/\D/g, "").slice(0, 9);
const pretty = (d) => [d.slice(0, 2), d.slice(2, 5), d.slice(5, 7), d.slice(7, 9)].filter(Boolean).join(" ");

function candidates(d) {
  const prefix = d.slice(0, 2);
  return Object.entries(OPERATORS)
    .filter(([, op]) => op.prefixes.includes(prefix))
    .sort(([, a], [, b]) => a.bps - b.bps);
}

export async function render(root) {
  const productsEl = h("div", { class: "products" });
  const side = h("div", { class: "cart stack" });

  function productCard(p) {
    const qty = cart.get(p.id) || 0;
    const control = qty
      ? h("div", { class: "stepper", role: "group", "aria-label": `Quantité de ${p.name}` },
          h("button", { type: "button", "aria-label": "Retirer un", onclick: () => setQty(p.id, qty - 1) }, icon("minus")),
          h("span", { "aria-live": "polite" }, qty),
          h("button", { type: "button", "aria-label": "Ajouter un", onclick: () => setQty(p.id, qty + 1) }, icon("plus")))
      : h("button", { type: "button", class: "btn sm", onclick: () => setQty(p.id, 1) }, icon("plus"), "Ajouter");
    return h("article", { class: "card product" },
      h("div", { class: `art ${p.art}` },
        p.tag ? h("span", { class: "tag" }, p.tag) : null,
        s("svg", { viewBox: "0 0 100 100", fill: "none", stroke: "#fff", "stroke-width": 3, "stroke-linecap": "round", "stroke-linejoin": "round", "stroke-opacity": 0.92, "aria-hidden": "true" },
          s("path", { d: p.glyph }))),
      h("div", { class: "info" },
        h("div", { class: "pname" }, p.name),
        h("div", { class: "pdesc" }, p.desc),
        h("div", { class: "buy" }, h("span", { class: "price" }, fmt.money(p.price)), control)));
  }

  function setQty(id, qty) {
    if (checkout && !checkout.finished) return;
    if (qty <= 0) cart.delete(id); else cart.set(id, Math.min(qty, 20));
    paint();
  }

  function paint() {
    productsEl.replaceChildren(...PRODUCTS.map(productCard));
    side.replaceChildren(...(checkout ? progressPanel() : cartPanel()));
  }

  // ---------------------------------------------------------------- panier et saisie
  function cartPanel() {
    const lines = PRODUCTS.filter((p) => cart.has(p.id));
    const sum = total();
    const d = digits(phone);
    const ops = candidates(d);
    const validPhone = d.length === 9 && ops.length > 0;
    const input = h("input", {
      class: "input lg", id: "payer-phone", inputmode: "numeric", autocomplete: "tel-national",
      placeholder: "77 123 45 67", value: pretty(d), "aria-describedby": "route-hint",
      "aria-invalid": d.length === 9 && !ops.length ? "true" : null,
      oninput: (event) => {
        const before = digits(phone);
        phone = digits(event.target.value);
        event.target.value = pretty(phone);
        if (before.slice(0, 2) !== phone.slice(0, 2) || (before.length === 9) !== (phone.length === 9)) {
          paint();
          const again = document.getElementById("payer-phone");
          again?.focus();
          again?.setSelectionRange(again.value.length, again.value.length);
        }
      },
    });
    const payBtn = h("button", { type: "button", class: "btn primary lg block", disabled: !(validPhone && sum >= 100) },
      icon("lock"), sum ? `Payer ${fmt.money(sum)}` : "Payer");
    payBtn.addEventListener("click", () => startPayment(lines, sum, d));

    const hint = h("div", { class: "route-hint", id: "route-hint" });
    if (d.length >= 2 && ops.length) {
      const [first, ...others] = ops;
      hint.append(icon("route", { width: 15, height: 15 }), "Routage Xaalis : ", operator(first[0]),
        h("span", { class: "subtle" }, ` ${fmt.bps(first[1].bps)}`),
        others.length ? h("span", { class: "subtle" }, ` · repli ${others.map(([, o]) => o.name).join(", ")}`) : null);
    } else if (d.length >= 2) {
      hint.append(icon("alert", { width: 15, height: 15 }), h("span", null, "Préfixe non pris en charge (70, 75, 76, 77, 78)."));
    } else {
      hint.append(h("span", { class: "subtle" }, "Go Pay, Teranga Money et Ndiaga Cash acceptés."));
    }

    return [
      h("section", { class: "card", "aria-labelledby": "cart-title" },
        h("div", { class: "card-head" }, icon("bag", { width: 17, height: 17 }), h("h2", { id: "cart-title" }, "Votre panier"),
          h("span", { class: "right subtle" }, lines.length ? `${[...cart.values()].reduce((a, b) => a + b, 0)} article(s)` : "")),
        h("div", { class: "card-body" },
          lines.length
            ? h("div", null,
                lines.map((p) => h("div", { class: "cart-line" }, h("span", { class: "qty" }, cart.get(p.id)), h("span", { class: "grow" }, p.name),
                  h("span", { class: "num" }, fmt.money(p.price * cart.get(p.id))))),
                h("div", { class: "divider" }),
                h("div", { class: "totals" },
                  h("div", { class: "row muted" }, h("span", null, "Livraison Dakar"), h("span", null, "Offerte")),
                  h("div", { class: "row grand" }, h("span", null, "Total"), h("span", { class: "num" }, fmt.money(sum)))))
            : h("div", { class: "muted", style: { textAlign: "center", padding: "12px 0" } }, "Ajoutez un article pour commencer."))),
      h("section", { class: "card", "aria-labelledby": "pay-title" },
        h("div", { class: "card-head" }, icon("phone", { width: 17, height: 17 }), h("h2", { id: "pay-title" }, "Paiement Mobile Money")),
        h("div", { class: "card-body stack" },
          h("div", { class: "field" },
            h("label", { for: "payer-phone" }, "Numéro du payeur"),
            h("div", { class: "input-group" }, h("span", { class: "addon lg" }, "🇸🇳 +221"), input),
            hint),
          h("div", { class: "chips", "aria-label": "Numéros de test" },
            [["781234512", "78 · Go Pay"], ["771234599", "77 · Ndiaga / Go Pay"], ["761234567", "76 · Teranga"]].map(([n, label]) =>
              h("button", { type: "button", class: "chip", onclick: () => { phone = n; paint(); } }, icon("zap", { width: 12, height: 12 }), label))),
          payBtn,
          h("p", { class: "subtle", style: { fontSize: "12px", textAlign: "center" } },
            icon("shield", { width: 13, height: 13, style: "vertical-align:-2px;margin-right:4px" }),
            "Clé secrète conservée côté serveur · requêtes idempotentes"))),
    ];
  }

  // ---------------------------------------------------------------- paiement
  async function startPayment(lines, sum, d) {
    const orderId = `CMD-${new Date().getFullYear()}-${String(Math.floor(Math.random() * 9000) + 1000)}`;
    checkout = { orderId, amount: sum, phone: d, status: "creating", events: [], log: [], intent: null, finished: false, keys: { create: newIdempotencyKey(), confirm: newIdempotencyKey() } };
    repaint();
    try {
      const intent = await api.post("/checkout", {
        amount: sum, payer_phone: `+221${d}`, order_id: orderId,
        description: lines.map((p) => `${cart.get(p.id)} × ${p.name}`).join(", ").slice(0, 255),
      }, { idempotencyKey: checkout.keys.create });
      checkout.intent = intent;
      checkout.status = intent.status;
      checkout.events.push({ status: "created", cause: "create", at: intent.created_at });
      repaint();
      await openStream(intent.id);
      await api.post(`/payment-intents/${intent.id}/confirm`, {}, { idempotencyKey: checkout.keys.confirm });
    } catch (error) {
      checkout.status = "error";
      checkout.error = error;
      checkout.finished = true;
      closeStream();
      repaint();
      problemToast(error, "Paiement impossible");
    }
  }

  function logLine(kind, text) {
    checkout.log.push({ at: new Date().toISOString(), kind, text });
    const pre = document.getElementById("sse-log");
    if (pre) { pre.replaceChildren(...renderLog()); pre.scrollTop = pre.scrollHeight; }
  }

  function openStream(intentId) {
    return new Promise((resolve) => {
      const source = new EventSource(`/api/payment-intents/${intentId}/stream`);
      checkout.source = source;
      const ready = setTimeout(resolve, 1500); // on confirme même si l'ouverture tarde : le flux rattrape
      source.onopen = () => { logLine("c", `connexion établie · GET /v1/payment-intents/${intentId}/stream (pk_test)`); clearTimeout(ready); resolve(); };
      source.addEventListener("status_changed", (event) => {
        const data = JSON.parse(event.data);
        logLine("e", { id: event.lastEventId, data });
        onStatus(data);
      });
      source.onerror = () => { if (checkout && !checkout.finished) logLine("c", "flux interrompu · reconnexion automatique (Last-Event-ID)…"); };
    });
  }

  function closeStream() { checkout?.source?.close(); }

  async function onStatus(data) {
    if (!checkout || checkout.events.some((e) => e.status === data.status)) return;
    checkout.events.push({ status: data.status, cause: data.cause, at: data.occurred_at });
    checkout.status = data.status;
    const terminal = ["succeeded", "failed", "canceled", "expired"].includes(data.status);
    if (terminal) { checkout.finished = true; closeStream(); logLine("c", "état final atteint · flux fermé"); }
    try {
      checkout.detail = await api.get(`/payment-intents/${checkout.intent.id}`);
    } catch { /* le détail est un plus : le flux suffit au suivi */ }
    repaint();
    if (data.status === "succeeded") { cart.clear(); waitForWebhook(checkout.intent.id); }
  }

  async function waitForWebhook(intentId) {
    const started = Date.now();
    while (checkout?.intent?.id === intentId && Date.now() - started < 15000) {
      try {
        const inbox = await api.get("/webhooks");
        const hit = inbox.events.find((e) => e.object_id === intentId && e.type === "payment.succeeded" && e.processed);
        if (hit) { checkout.webhook = hit; repaint(); return; }
      } catch { return; }
      await new Promise((r) => setTimeout(r, 800));
    }
  }

  function renderLog() {
    return checkout.log.flatMap((line) => {
      const time = h("span", { class: "t" }, fmt.time(line.at), " ");
      if (line.kind === "c") return [time, h("span", { class: "c" }, `: ${line.text}`), "\n"];
      const { id, data } = line.text;
      return [time, h("span", { class: "k" }, "id: "), id, "  ", h("span", { class: "k" }, "event: "), "status_changed\n",
        "         ", h("span", { class: "k" }, "data: "),
        h("span", { class: "s" }, JSON.stringify({ previous_status: data.previous_status, status: data.status, cause: data.cause })), "\n"];
    });
  }

  function progressPanel() {
    const c = checkout;
    const detail = c.detail || c.intent;
    const reached = (status) => c.events.some((e) => e.status === status);
    const failed = ["failed", "canceled", "expired"].includes(c.status) || c.status === "error";
    const success = c.status === "succeeded";
    const failover = c.detail?.history?.find((e) => e.cause === "operator_failover");

    const steps = [
      { key: "created", title: "Commande enregistrée", detail: c.intent ? h("span", null, copyButton(c.intent.id)) : "Création de l'intent…", at: c.events.find((e) => e.status === "created")?.at },
      { key: "processing", title: "Transmise à l'opérateur",
        detail: detail?.operator ? h("span", null, "Routage Xaalis → ", operator(detail.operator), failover ? h("span", { class: "pill warn", style: { marginLeft: "6px" } }, icon("route"), "bascule") : null) : "Choix de l'opérateur le moins cher et disponible",
        at: c.events.find((e) => e.status === "processing")?.at },
      { key: "waiting", title: "Validation par le client", detail: success ? "Code secret saisi sur le téléphone" : "Le client confirme sur son téléphone…" },
      { key: "final", title: success ? "Paiement confirmé" : failed ? "Paiement non abouti" : "Confirmation", detail: success ? "Callback opérateur reçu et vérifié" : failed ? (c.error?.detail || CAUSES[c.events.at(-1)?.cause] || "") : "", at: success || failed ? c.events.at(-1)?.at : null },
    ];
    const state = (key) => {
      if (key === "created") return reached("created") ? "done" : "active";
      if (key === "processing") return reached("processing") ? "done" : reached("created") ? "active" : "pending";
      if (key === "waiting") return success ? "done" : reached("processing") && !failed ? "active" : failed ? "pending" : "pending";
      return success ? "done" : failed ? "failed" : "pending";
    };

    const ring = success ? "ok" : failed ? "bad" : "wait";
    const head = h("div", { class: "pay-status" },
      h("div", { class: `ring ${ring}` }, icon(success ? "check" : failed ? "x" : "phone")),
      h("div", { class: "big" }, fmt.money(c.amount)),
      h("div", { class: "muted" }, success ? "Paiement réussi — merci pour votre commande !" : failed ? "Le paiement n'a pas abouti." : `En attente de validation sur le +221 ${pretty(c.phone)}`),
      h("div", { class: "subtle", style: { fontSize: "12px" } }, `Commande ${c.orderId}`));

    const timeline = h("ol", { class: "timeline" }, steps.map((step) => {
      const st = state(step.key);
      return h("li", { class: st },
        h("span", { class: "node" }, icon(st === "done" ? "check" : st === "failed" ? "x" : st === "active" ? "loader" : "clock")),
        h("div", null, h("div", { class: "row between" }, h("span", { class: "title" }, step.title), step.at ? h("span", { class: "time" }, fmt.time(step.at)) : null),
          step.detail ? h("div", { class: "detail" }, step.detail) : null));
    }));

    const panels = [h("section", { class: "card" }, h("div", { class: "card-body" }, head, timeline))];

    if (success && detail) {
      panels.push(h("section", { class: "card fade-in" },
        h("div", { class: "card-head" }, icon("check", { width: 17, height: 17 }), h("h2", null, "Reçu"), h("span", { class: "right" }, h("span", { class: "pill ok" }, icon("check"), "Réussi"))),
        h("div", { class: "card-body" },
          h("dl", { class: "kv" },
            h("dt", null, "Montant payé"), h("dd", null, amount(detail.amount)),
            h("dt", null, "Commission Xaalis"), h("dd", null, amount(detail.fee)),
            h("dt", null, "Net marchand"), h("dd", null, h("strong", null, amount(detail.net))),
            h("dt", null, "Opérateur"), h("dd", null, operator(detail.operator)),
            h("dt", null, "Payeur"), h("dd", { class: "mono" }, detail.payer_phone),
            h("dt", null, "Webhook payment.succeeded"), h("dd", null, c.webhook
              ? h("span", { class: "pill ok", title: "Signature HMAC vérifiée par le serveur marchand" }, icon("check"), "reçu et vérifié")
              : h("span", { class: "pill" }, icon("loader", { class: "spin" }), "en attente…"))),
          failover ? h("div", { class: "callout warn", style: { marginTop: "14px" } }, icon("route"),
            h("div", null, h("strong", null, "Bascule automatique. "), "Le premier opérateur a refusé de façon certaine : Xaalis a relancé le paiement chez le suivant, sans risque de double débit.")) : null),
        h("div", { class: "card-foot row between" },
          h("a", { class: "btn sm ghost", href: `#/paiements/${detail.id}` }, "Voir dans les paiements", icon("arrowRight")),
          h("button", { type: "button", class: "btn sm primary", onclick: () => { checkout = null; paint(); } }, icon("plus"), "Nouvelle commande"))));
    } else if (failed) {
      panels.push(h("div", { class: "row", style: { justifyContent: "flex-end" } },
        h("button", { type: "button", class: "btn", onclick: () => { checkout = null; paint(); } }, icon("refresh"), "Réessayer")));
    }

    panels.push(h("section", { class: "terminal", "aria-label": "Flux Server-Sent Events" },
      h("div", { class: "bar" }, h("span", { class: "lights" }, h("i"), h("i"), h("i")), h("span", null, "Flux SSE en direct"),
        h("span", { style: { marginLeft: "auto" } }, c.finished ? "fermé" : h("span", { class: "pill ok live", style: { height: "18px", fontSize: "11px" } }, h("span", { class: "dot" }), "en direct"))),
      h("pre", { id: "sse-log" }, renderLog())));
    return panels;
  }

  root.append(
    h("section", { class: "shop-hero" },
      h("div", { class: "eyebrow" }, "Boutique de démonstration"),
      h("h1", null, "Maison Sandaga"),
      h("p", null, "L'artisanat de Dakar, payé en quelques secondes par Mobile Money. Chaque paiement traverse la vraie passerelle Xaalis : routage, opérateur, webhooks et grand livre."),
      h("div", { class: "badges" },
        h("span", null, icon("zap"), "Suivi en temps réel"),
        h("span", null, icon("route"), "Bascule automatique"),
        h("span", null, icon("shield"), "Signatures HMAC"))),
    h("div", { class: "grid split" }, productsEl, side),
  );
  repaint = paint;
  paint();
  // Le flux reste ouvert si l'on change de vue : le reçu sera prêt au retour.
  return () => { repaint = () => {}; };
}
