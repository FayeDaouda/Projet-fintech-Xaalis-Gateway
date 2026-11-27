// Point d'entrée : écran de démarrage, navigation par hash, palette de commandes (⌘K),
// thème, état de la passerelle, marchand courant.

import { api, newIdempotencyKey, session, setSession } from "./api.js";
import { closeLayer, confirmDialog, h, icon, problemToast, toast } from "./ui.js";
import * as home from "./views/home.js";
import * as shop from "./views/shop.js";
import * as overview from "./views/overview.js";
import * as payments from "./views/payments.js";
import * as webhooks from "./views/webhooks.js";
import * as operations from "./views/operations.js";
import * as ledger from "./views/ledger.js";
import * as simulation from "./views/simulation.js";

const ROUTES = [
  { group: "Plateforme", path: "accueil", label: "Centre de contrôle", icon: "home", view: home },
  { group: "Encaisser", path: "boutique", label: "Boutique", icon: "store", view: shop },
  { group: "Piloter", path: "apercu", label: "Vue d'ensemble", icon: "dashboard", view: overview },
  { group: "Piloter", path: "paiements", label: "Paiements", icon: "list", view: payments },
  { group: "Piloter", path: "webhooks", label: "Webhooks", icon: "webhook", view: webhooks, badge: "webhooks" },
  { group: "Back-office Xaalis", path: "operations", label: "Rapprochement & T+1", icon: "scale", view: operations },
  { group: "Back-office Xaalis", path: "grand-livre", label: "Grand livre", icon: "book", view: ledger },
  { group: "Laboratoire", path: "simulateur", label: "Simulateur opérateurs", icon: "activity", view: simulation, badge: "simulation" },
];

const view = document.getElementById("view");
let current = null; // { route, cleanup }
let links = {};

function renderNav() {
  const groups = new Map();
  for (const route of ROUTES) {
    if (!groups.has(route.group)) groups.set(route.group, []);
    groups.get(route.group).push(route);
  }
  const external = [["grafana", "Grafana", "gauge"], ["jaeger", "Traces Jaeger", "eye"], ["docs", "Contrat OpenAPI", "book"]]
    .filter(([key]) => links[key]);
  document.getElementById("nav").replaceChildren(
    ...[...groups].map(([label, routes]) =>
      h("div", { class: "nav-group" }, h("div", { class: "nav-label" }, label),
        routes.map((r) => h("a", { href: `#/${r.path}`, dataset: { path: r.path } }, icon(r.icon), h("span", null, r.label),
          r.badge ? h("span", { class: "count", dataset: { badge: r.badge }, hidden: true }) : null)))),
    external.length ? h("div", { class: "nav-group" }, h("div", { class: "nav-label" }, "Observabilité"),
      external.map(([key, label, ic]) => h("a", { href: links[key], target: "_blank", rel: "noopener" }, icon(ic), h("span", null, label),
        icon("external", { style: "margin-left:auto;width:13px;height:13px;opacity:.6" })))) : null,
  );
  highlight(current?.route);
}

function highlight(route) {
  for (const a of document.querySelectorAll(".nav a[data-path]")) {
    if (route && a.dataset.path === route.path) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
}

async function navigate() {
  const [path, ...rest] = location.hash.replace(/^#\/?/, "").split("/");
  const route = ROUTES.find((r) => r.path === path) || ROUTES[0];
  if (current?.cleanup) current.cleanup();
  current = { route, cleanup: null };
  highlight(route);
  document.getElementById("crumbs").replaceChildren(h("span", null, route.group), icon("chevronRight", { width: 14, height: 14 }), h("strong", null, route.label));
  document.title = `${route.label} — Xaalis`;
  closeMenu();
  view.replaceChildren();
  view.classList.remove("fade-in");
  void view.offsetWidth;
  view.classList.add("fade-in");
  const cleanup = await route.view.render(view, { params: rest });
  if (current.route === route) current.cleanup = cleanup;
  else cleanup?.();
  view.focus({ preventScroll: true });
  window.scrollTo({ top: 0 });
}

// ------------------------------------------------------------------ thème
function isDark() {
  return document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
}
function applyThemeButton() {
  document.getElementById("theme-toggle").replaceChildren(icon(isDark() ? "sun" : "moon"), isDark() ? "Thème clair" : "Thème sombre");
}
function toggleTheme() {
  const next = isDark() ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("xaalis-theme", next); } catch { /* stockage indisponible */ }
  applyThemeButton();
}
document.getElementById("theme-toggle").addEventListener("click", toggleTheme);

// ------------------------------------------------------------------ menu mobile
const sidebar = document.getElementById("sidebar");
const menuBtn = document.getElementById("menu-btn");
function closeMenu() { sidebar.classList.remove("open"); menuBtn.setAttribute("aria-expanded", "false"); }
menuBtn.append(icon("menu"));
menuBtn.addEventListener("click", () => {
  const open = sidebar.classList.toggle("open");
  menuBtn.setAttribute("aria-expanded", String(open));
});

// ------------------------------------------------------------------ marchand et santé
export async function refreshMerchant(fresh) {
  try {
    const s = fresh || (await session());
    const name = document.getElementById("merchant-name");
    name.textContent = s.name;
    name.classList.remove("skeleton");
    document.getElementById("merchant-id").textContent = s.merchant_id;
    document.querySelector("#merchant-card .avatar").textContent = s.name.slice(0, 1);
    if (s.links && JSON.stringify(s.links) !== JSON.stringify(links)) { links = s.links; renderNav(); }
    return s;
  } catch (error) {
    document.getElementById("merchant-id").textContent = "passerelle injoignable";
    problemToast(error, "Connexion à la passerelle impossible");
    return null;
  }
}

async function pollHealth() {
  const pill = document.getElementById("health-pill");
  try {
    const ready = await api.get("/health");
    const ok = Object.values(ready.checks || {}).every((check) => check === "ok");
    pill.className = `pill ${ok ? "ok live" : "warn"}`;
    pill.replaceChildren(h("span", { class: "dot" }), ok ? "Passerelle opérationnelle" : "Passerelle dégradée");
    pill.title = Object.entries(ready.checks || {}).map(([k, v]) => `${k} : ${v}`).join(" · ");
    return ok;
  } catch {
    pill.className = "pill bad";
    pill.replaceChildren(h("span", { class: "dot" }), "Passerelle injoignable");
    pill.title = "";
    return false;
  }
}

async function resetMerchant() {
  const ok = await confirmDialog({
    title: "Repartir d'un marchand neuf ?",
    body: "Un nouveau marchand est créé par l'API d'administration (chiffres à zéro, nouvel endpoint de webhooks). L'ancien reste intact en base.",
    confirmLabel: "Créer",
  });
  if (!ok) return;
  try {
    const fresh = await api.post("/session/reset");
    setSession(fresh);
    await refreshMerchant(fresh);
    toast(`Marchand ${fresh.merchant_id} créé`, { tone: "ok" });
    navigate();
  } catch (error) { problemToast(error, "Création impossible"); }
}
const resetBtn = document.getElementById("reset-merchant");
resetBtn.append(icon("rotate"), "Nouveau marchand");
resetBtn.addEventListener("click", resetMerchant);

// ------------------------------------------------------------------ palette de commandes
async function demoPayment(kind) {
  try {
    await api.post("/demo/payment", { kind }, { idempotencyKey: newIdempotencyKey() });
    toast(kind === "suspect" ? "Paiement suspect lancé" : "Paiement de démonstration lancé", { tone: "info", detail: "Suivez-le dans le Centre de contrôle." });
  } catch (error) { problemToast(error); }
}

function commands() {
  const go = (r) => ({ label: r.label, group: "Aller à", icon: r.icon, run: () => { location.hash = `#/${r.path}`; } });
  const open = (key, label, ic) => (links[key] ? [{ label, group: "Ouvrir", icon: ic, run: () => window.open(links[key], "_blank", "noopener") }] : []);
  return [
    ...ROUTES.map(go),
    { label: "Lancer un paiement de démonstration", group: "Action", icon: "play", run: () => demoPayment("normal") },
    { label: "Lancer un paiement suspect (anti-fraude)", group: "Action", icon: "shield", run: () => demoPayment("suspect") },
    { label: "Mode présentation des opérateurs", group: "Action", icon: "sparkles", run: async () => { try { await api.post("/simulation/presentation"); toast("Mode présentation activé", { tone: "ok" }); } catch (e) { problemToast(e); } } },
    { label: "Nouveau marchand", group: "Action", icon: "rotate", run: resetMerchant },
    { label: isDark() ? "Passer au thème clair" : "Passer au thème sombre", group: "Affichage", icon: isDark() ? "sun" : "moon", run: toggleTheme },
    ...open("grafana", "Tableau de bord Grafana", "gauge"),
    ...open("jaeger", "Traces Jaeger", "eye"),
    ...open("docs", "Contrat OpenAPI", "book"),
  ];
}

function openPalette() {
  if (document.querySelector(".palette")) return;
  const all = commands();
  let selected = 0;
  let shown = all;
  const list = h("ul", { role: "listbox", id: "palette-list" });
  const input = h("input", { type: "text", placeholder: "Rechercher un écran ou une action…", "aria-label": "Rechercher", "aria-controls": "palette-list", autocomplete: "off" });
  const scrim = h("div", { class: "scrim" });
  const panel = h("div", { class: "palette", role: "dialog", "aria-modal": "true", "aria-label": "Palette de commandes" },
    icon("search", { class: "pi" }), input, list,
    h("div", { class: "hint" }, h("span", null, h("kbd", null, "↑"), " ", h("kbd", null, "↓"), " naviguer"), h("span", null, h("kbd", null, "↵"), " valider"), h("span", null, h("kbd", null, "Échap"), " fermer")));
  const close = () => { scrim.remove(); panel.remove(); document.removeEventListener("keydown", onKey, true); };
  const paint = () => {
    list.replaceChildren(...shown.map((c, i) => h("li", { role: "option", "aria-selected": String(i === selected), onmousemove: () => { if (selected !== i) { selected = i; paint(); } }, onclick: () => { close(); c.run(); } },
      icon(c.icon), h("span", null, c.label), h("span", { class: "grp" }, c.group))));
    list.children[selected]?.scrollIntoView({ block: "nearest" });
  };
  const filter = () => {
    const q = input.value.trim().toLowerCase().normalize("NFD").replace(/\p{M}/gu, "");
    shown = all.filter((c) => c.label.toLowerCase().normalize("NFD").replace(/\p{M}/gu, "").includes(q));
    selected = 0;
    paint();
  };
  const onKey = (event) => {
    if (event.key === "Escape") { event.preventDefault(); close(); }
    else if (event.key === "ArrowDown") { event.preventDefault(); selected = Math.min(selected + 1, shown.length - 1); paint(); }
    else if (event.key === "ArrowUp") { event.preventDefault(); selected = Math.max(selected - 1, 0); paint(); }
    else if (event.key === "Enter" && shown[selected]) { event.preventDefault(); const c = shown[selected]; close(); c.run(); }
  };
  scrim.addEventListener("click", close);
  input.addEventListener("input", filter);
  document.addEventListener("keydown", onKey, true);
  document.body.append(scrim, panel);
  paint();
  input.focus();
}

document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); closeLayer(); openPalette(); }
});
const searchBtn = document.getElementById("search-btn");
searchBtn.append(icon("search"), h("span", { class: "lbl" }, "Rechercher…"), h("kbd", null, navigator.platform.includes("Mac") ? "⌘K" : "Ctrl K"));
searchBtn.addEventListener("click", openPalette);

// ------------------------------------------------------------------ démarrage
async function boot() {
  const steps = ["Connexion à la passerelle Xaalis", "Intégration du marchand Maison Sandaga", "Synchronisation du temps réel"];
  const items = steps.map((label) => h("li", null, h("i"), label));
  const splash = h("div", { class: "splash", role: "status", "aria-live": "polite" },
    h("div", { class: "box" },
      h("div", { class: "mark", "aria-hidden": "true" },
        h("span", null, icon("x", { style: "color:#fff;width:30px;height:30px", "stroke-width": 2.6 }))),
      h("h2", null, "Xaalis Gateway"),
      h("ol", null, items)));
  document.body.append(splash);
  const started = performance.now();
  const done = (i) => items[i].classList.add("done");

  renderNav();
  applyThemeButton();
  await pollHealth();
  done(0);
  await refreshMerchant();
  done(1);
  window.addEventListener("hashchange", navigate);
  await navigate();
  done(2);
  const wait = Math.max(0, 1100 - (performance.now() - started));
  setTimeout(() => { splash.classList.add("gone"); setTimeout(() => splash.remove(), 500); }, wait);
  setInterval(pollHealth, 10_000);
}

boot();
