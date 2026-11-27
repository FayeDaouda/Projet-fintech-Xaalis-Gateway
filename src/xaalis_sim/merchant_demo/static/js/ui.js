// Briques d'interface : construction du DOM sans innerHTML (aucune donnée n'est interprétée
// comme du HTML), icônes, notifications, tiroir latéral et fenêtre de confirmation.

const SVG_NS = "http://www.w3.org/2000/svg";

function assign(el, props) {
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === "class") el.setAttribute("class", value);
    else if (key === "style" && typeof value === "object") {
      for (const [prop, v] of Object.entries(value)) {
        if (prop.startsWith("--")) el.style.setProperty(prop, v); else el.style[prop] = v;
      }
    }
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (key === "value" && "value" in el) el.value = value;
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, String(value));
  }
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  assign(el, props);
  append(el, children);
  return el;
}

export function s(tag, props, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  assign(el, props);
  append(el, children);
  return el;
}

// ------------------------------------------------------------------ icônes (trait 24×24)
const PATHS = {
  store: "M3 9l1.6-5h14.8L21 9M3 9h18M4 9v11h16V9M9.5 20v-6h5v6",
  dashboard: "M3 3h8v9H3zM13 3h8v5h-8zM13 10h8v11h-8zM3 14h8v7H3z",
  list: "M9 6h12M9 12h12M9 18h12M4 6h.01M4 12h.01M4 18h.01",
  webhook: "M18 17H12c-1 0-1.7.8-2.2 1.7A4 4 0 1 1 6.5 13M8 17l3-5.6c.5-.9.1-2-.4-2.9A4 4 0 1 1 17.4 5M12 6l3 5.6c.5.9 1.6 1.4 2.7 1.4a4 4 0 1 1-1.2 7.8",
  scale: "M12 3v18M7 21h10M5 7h14M5 7l-3 7a3.2 3.2 0 0 0 6 0zM19 7l-3 7a3.2 3.2 0 0 0 6 0z",
  activity: "M22 12h-4l-3 8L9 4l-3 8H2",
  check: "M20 6L9 17l-5-5",
  x: "M18 6L6 18M6 6l12 12",
  alert: "M12 9v4M12 17h.01M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
  info: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 16v-4M12 8h.01",
  refresh: "M21 12a9 9 0 1 1-2.6-6.4L21 8M21 3v5h-5",
  loader: "M12 3a9 9 0 1 0 9 9",
  arrowRight: "M5 12h14M13 6l6 6-6 6",
  copy: "M9 9h11v11H9zM5 15H4V4h11v1",
  sun: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4",
  moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z",
  zap: "M13 2L3 14h9l-1 8 10-12h-9z",
  clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 7v5l3 2",
  shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10zM9 12l2 2 4-4",
  download: "M12 3v12M7 10l5 5 5-5M5 21h14",
  undo: "M9 14L4 9l5-5M4 9h10.5a5.5 5.5 0 0 1 0 11H11",
  send: "M22 2L11 13M22 2l-7 20-4-9-9-4z",
  wallet: "M3 7h15a3 3 0 0 1 3 3v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM3 7V6a2 2 0 0 1 2-2h11M16.5 14h.01",
  phone: "M7 2h10a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1zM11 18h2",
  plus: "M12 5v14M5 12h14",
  minus: "M5 12h14",
  bag: "M5 8h14l-1 13H6zM9 8V6a3 3 0 0 1 6 0v2",
  radio: "M12 12h.01M8.5 8.5a5 5 0 0 0 0 7M15.5 8.5a5 5 0 0 1 0 7M5.6 5.6a9 9 0 0 0 0 12.8M18.4 5.6a9 9 0 0 1 0 12.8",
  book: "M4 4h11a3 3 0 0 1 3 3v14H7a3 3 0 0 1-3-3zM4 18a3 3 0 0 1 3-3h11",
  power: "M12 2v9M18.4 6.6a9 9 0 1 1-12.8 0",
  chevronRight: "M9 6l6 6-6 6",
  sparkles: "M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z",
  external: "M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5",
  menu: "M4 6h16M4 12h16M4 18h16",
  lock: "M6 11h12v10H6zM8 11V7a4 4 0 0 1 8 0v4",
  route: "M6 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM18 9a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM6 15V9a4 4 0 0 1 4-4h4M18 9v6a4 4 0 0 1-4 4h-4",
  calendar: "M4 6h16v15H4zM4 10h16M8 3v4M16 3v4",
  layers: "M12 3l9 5-9 5-9-5zM3 13l9 5 9-5",
  rotate: "M3 12a9 9 0 0 1 15.5-6.2L21 8M21 3v5h-5M21 12a9 9 0 0 1-15.5 6.2L3 16M3 21v-5h5",
  search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-4-4",
  home: "M3 11l9-7 9 7M5 10v10h14V10M10 20v-6h4v6",
  play: "M7 4l13 8-13 8z",
  eye: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
  cpu: "M7 7h10v10H7zM10 10h4v4h-4zM10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4",
  gauge: "M12 14l4-4M3.5 17a9 9 0 1 1 17 0",
  database: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  inbox: "M3 13h5l2 3h4l2-3h5M5 5h14l2 8v6H3v-6z",
};

export function icon(name, extra = {}) {
  return s(
    "svg",
    {
      viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 2,
      "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", ...extra,
    },
    s("path", { d: PATHS[name] || PATHS.info }),
  );
}

// ------------------------------------------------------------------ formatage
const MONEY = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 0 });
const DATE_TIME = new Intl.DateTimeFormat("fr-FR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "Africa/Dakar" });
const TIME = new Intl.DateTimeFormat("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "Africa/Dakar" });
const DAY = new Intl.DateTimeFormat("fr-FR", { weekday: "short", day: "numeric", timeZone: "UTC" });
const LONG_DAY = new Intl.DateTimeFormat("fr-FR", { weekday: "long", day: "numeric", month: "long", timeZone: "UTC" });

export const fmt = {
  number: (n) => MONEY.format(n ?? 0),
  money: (n) => `${MONEY.format(n ?? 0)} XOF`,
  dateTime: (iso) => (iso ? DATE_TIME.format(new Date(iso)) : "—"),
  time: (iso) => (iso ? TIME.format(new Date(iso)) : "—"),
  day: (isoDate) => DAY.format(new Date(`${isoDate}T00:00:00Z`)),
  longDay: (isoDate) => LONG_DAY.format(new Date(`${isoDate}T00:00:00Z`)),
  permille: (p) => `${MONEY.format(Math.floor(p / 10))},${p % 10} %`,
  bps: (b) => `${(b / 100).toLocaleString("fr-FR")} %`,
};

export function amount(n, { cur = true } = {}) {
  return h("span", { class: "amount" }, fmt.number(n), cur ? h("span", { class: "cur" }, "XOF") : null);
}

export const OPERATORS = {
  gopay: { name: "Go Pay", short: "GP", prefixes: ["77", "78"], bps: 90 },
  teranga: { name: "Teranga Money", short: "TM", prefixes: ["76", "70"], bps: 100 },
  ndiaga: { name: "Ndiaga Cash", short: "NC", prefixes: ["75", "77"], bps: 80 },
};

export function operator(code) {
  if (!code) return h("span", { class: "subtle" }, "—");
  const op = OPERATORS[code];
  return h("span", { class: `op op-${code}` }, h("span", { class: "swatch", "aria-hidden": "true" }), op ? op.name : code);
}

export const STATUSES = {
  created: { label: "Créé", tone: "", icon: "clock" },
  processing: { label: "En cours", tone: "info", icon: "loader" },
  succeeded: { label: "Réussi", tone: "ok", icon: "check" },
  failed: { label: "Échoué", tone: "bad", icon: "x" },
  canceled: { label: "Annulé", tone: "", icon: "x" },
  expired: { label: "Expiré", tone: "warn", icon: "clock" },
  partially_refunded: { label: "Remb. partiel", tone: "warn", icon: "undo" },
  refunded: { label: "Remboursé", tone: "", icon: "undo" },
};

export function statusPill(status) {
  const meta = STATUSES[status] || { label: status, tone: "", icon: "info" };
  return h("span", { class: `pill ${meta.tone}` }, icon(meta.icon), meta.label);
}

export const CAUSES = {
  create: "Intent créé",
  confirm: "Confirmé par le marchand",
  operator_succeeded: "Accepté par l'opérateur",
  operator_failed: "Refusé par l'opérateur",
  operator_failover: "Bascule vers un autre opérateur",
  cancel: "Annulé",
  expire: "Expiré (15 min)",
  refund_partial: "Remboursement partiel",
  refund_full: "Remboursement total",
  risk_assessed: "Évalué par l'anti-fraude",
  risk_declined: "Refusé par l'anti-fraude",
};

export const RISK = {
  approve: { label: "Risque faible", tone: "ok", icon: "shield" },
  review: { label: "À surveiller", tone: "warn", icon: "alert" },
  decline: { label: "Refusé (fraude)", tone: "bad", icon: "x" },
};

// Évaluation anti-fraude (service gRPC, ADR 0013) : décision, score en ‰, ou repli.
export function riskPill(decision, score) {
  if (!decision) return h("span", { class: "subtle" }, "—");
  const meta = RISK[decision] || { label: decision, tone: "", icon: "shield" };
  return h("span", { class: `pill ${meta.tone}`, title: "Service gRPC de scoring · deadline 150 ms" }, icon(meta.icon), meta.label,
    h("span", { style: { opacity: 0.75 } }, score == null ? " · repli" : ` · ${score} ‰`));
}

// ------------------------------------------------------------------ interactions
export function copyButton(text, label = text) {
  return h(
    "button",
    {
      type: "button", class: "copy mono", title: "Copier",
      onclick: async (event) => {
        event.stopPropagation();
        try { await navigator.clipboard.writeText(text); toast("Copié dans le presse-papiers", { tone: "ok" }); }
        catch { toast("Copie impossible", { tone: "bad" }); }
      },
    },
    label, icon("copy"),
  );
}

export function toast(title, { detail, tone = "info", timeout = 4200 } = {}) {
  const icons = { ok: "check", bad: "alert", info: "info" };
  const el = h("div", { class: `toast ${tone}` }, icon(icons[tone] || "info"),
    h("div", null, h("div", { class: "t-title" }, title), detail ? h("div", { class: "t-detail" }, detail) : null));
  document.getElementById("toasts").append(el);
  setTimeout(() => { el.classList.add("leaving"); setTimeout(() => el.remove(), 220); }, timeout);
}

export function problemToast(error, fallback = "L'opération a échoué") {
  toast(error?.title || fallback, { detail: error?.detail || error?.message, tone: "bad", timeout: 6500 });
}

let activeLayer = null;

function openLayer(build) {
  closeLayer();
  const previous = document.activeElement;
  const scrim = h("div", { class: "scrim", onclick: () => closeLayer() });
  const panel = build();
  const onKey = (event) => { if (event.key === "Escape") closeLayer(); };
  document.addEventListener("keydown", onKey);
  document.body.append(scrim, panel);
  activeLayer = { scrim, panel, onKey, previous };
  requestAnimationFrame(() => panel.querySelector("[autofocus], button, input")?.focus());
  return panel;
}

export function closeLayer() {
  if (!activeLayer) return;
  const { scrim, panel, onKey, previous } = activeLayer;
  document.removeEventListener("keydown", onKey);
  scrim.remove();
  panel.remove();
  activeLayer = null;
  previous?.focus?.();
}

export function drawer({ title, subtitle, actions = [], body }) {
  return openLayer(() =>
    h("aside", { class: "drawer", role: "dialog", "aria-modal": "true", "aria-label": typeof title === "string" ? title : "Détail" },
      h("div", { class: "drawer-head" },
        h("div", { style: { flex: 1, minWidth: 0 } }, h("div", { style: { fontSize: "17px", fontWeight: 650, letterSpacing: "-0.02em" } }, title),
          subtitle ? h("div", { class: "muted", style: { marginTop: "4px" } }, subtitle) : null),
        ...actions,
        h("button", { type: "button", class: "btn icon ghost", "aria-label": "Fermer", onclick: () => closeLayer() }, icon("x"))),
      h("div", { class: "drawer-body" }, body)));
}

export function confirmDialog({ title, body, confirmLabel = "Confirmer", tone = "primary" }) {
  return new Promise((resolve) => {
    const done = (value) => { closeLayer(); resolve(value); };
    openLayer(() =>
      h("div", { class: "modal", role: "alertdialog", "aria-modal": "true", "aria-label": title },
        h("div", { class: "card-body stack" }, h("h2", { style: { fontSize: "16px", fontWeight: 650 } }, title), h("div", { class: "muted" }, body)),
        h("div", { class: "card-foot row", style: { justifyContent: "flex-end" } },
          h("button", { type: "button", class: "btn", onclick: () => done(false) }, "Annuler"),
          h("button", { type: "button", class: `btn ${tone}`, autofocus: true, onclick: () => done(true) }, confirmLabel))));
    activeLayer.scrim.onclick = () => done(false);
  });
}

// Bouton qui affiche un indicateur pendant une action asynchrone et empêche le double envoi.
export function busyButton(btn, action) {
  btn.addEventListener("click", async (event) => {
    event.preventDefault();
    if (btn.disabled) return;
    const original = [...btn.childNodes];
    btn.disabled = true;
    btn.replaceChildren(icon("loader", { class: "spin" }), ...original.filter((n) => n.nodeType === Node.TEXT_NODE));
    try { await action(event); }
    finally { btn.disabled = false; btn.replaceChildren(...original); }
  });
  return btn;
}

export function emptyState(iconName, title, text, action) {
  return h("div", { class: "empty" }, h("div", { class: "icon-wrap" }, icon(iconName)), h("h3", null, title), text ? h("p", null, text) : null, action || null);
}

export function skeletonRows(count, cols) {
  return Array.from({ length: count }, () =>
    h("tr", null, Array.from({ length: cols }, () => h("td", null, h("span", { class: "skeleton", style: { display: "inline-block", width: `${50 + Math.random() * 40}%`, height: "12px" } }, ".")))));
}

// Pastille de compteur dans la navigation (webhooks en échec, opérateur en panne…).
export function setBadge(name, text, { alert = false } = {}) {
  const el = document.querySelector(`[data-badge="${name}"]`);
  if (!el) return;
  el.hidden = !text;
  el.textContent = text || "";
  el.classList.toggle("alert", alert);
}

// ------------------------------------------------------------------ présentation d'écran
// Le texte qui raconte chaque écran : ce que l'on voit, et ce qui tourne sous le capot.
// Repliable ; le choix est mémorisé localement (confort de lecture, sans effet métier).
export function pageIntro({ id, eyebrow, title, lead, seen = [], under = [], actions = [] }) {
  const key = `xaalis-intro-${id}`;
  let collapsed = false;
  try { collapsed = localStorage.getItem(key) === "1"; } catch { /* stockage indisponible */ }
  const toggle = h("button", { type: "button", class: "btn sm ghost intro-toggle", "aria-expanded": String(!collapsed) });
  const root = h("section", { class: `intro${collapsed ? " collapsed" : ""}`, "aria-label": title },
    h("div", { class: "intro-main" },
      h("div", null, h("div", { class: "eyebrow" }, eyebrow), h("h1", null, title), h("p", { class: "lead" }, lead)),
      actions.length ? h("div", { class: "actions" }, actions) : null),
    h("div", { class: "intro-details" },
      h("div", null, h("h2", null, "Ce que vous voyez"),
        h("ul", null, seen.map((item) => h("li", null, icon("check"), h("span", null, ...[item].flat()))))),
      h("div", null, h("h2", null, "Sous le capot"),
        h("div", { class: "tech" }, under.map(([tag, text]) => h("span", null, h("b", null, tag), text))))),
    toggle);
  const paint = () => {
    root.classList.toggle("collapsed", collapsed);
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.replaceChildren(icon(collapsed ? "plus" : "minus"), collapsed ? "Détails" : "Réduire");
  };
  toggle.addEventListener("click", () => {
    collapsed = !collapsed;
    try { localStorage.setItem(key, collapsed ? "1" : "0"); } catch { /* sans effet */ }
    paint();
  });
  paint();
  return root;
}

// Chiffre qui défile jusqu'à sa valeur (respecte « réduire les animations »).
export function countUp(el, to, { format = fmt.number, duration = 700 } = {}) {
  const from = Number(el.dataset.value || 0);
  el.dataset.value = String(to);
  if (from === to || matchMedia("(prefers-reduced-motion: reduce)").matches) { el.textContent = format(to); return; }
  const start = performance.now();
  const step = (now) => {
    const t = Math.min(1, (now - start) / duration);
    const eased = 1 - (1 - t) ** 3;
    el.textContent = format(Math.round(from + (to - from) * eased));
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

export const ACCOUNTS = {
  operator_clearing: { label: "À recevoir de l'opérateur", color: "var(--info)" },
  merchant_pending: { label: "Dû au marchand · en attente (T+1)", color: "var(--warn)" },
  merchant_available: { label: "Dû au marchand · disponible", color: "var(--brand)" },
  merchant_payout: { label: "Viré au marchand", color: "var(--text-3)" },
  xaalis_revenue: { label: "Commissions Xaalis", color: "var(--violet)" },
};

export const TRANSACTIONS = {
  payment_capture: "Encaissement d'un paiement",
  settlement: "Règlement T+1",
  refund: "Remboursement",
  payout: "Virement au marchand",
  reversal: "Extourne",
};
