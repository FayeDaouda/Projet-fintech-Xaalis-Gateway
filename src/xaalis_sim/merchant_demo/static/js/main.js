// Point d'entrée : navigation par hash, thème, état de la passerelle, marchand courant.

import { api, session, setSession } from "./api.js";
import { confirmDialog, h, icon, problemToast, toast } from "./ui.js";
import * as shop from "./views/shop.js";
import * as overview from "./views/overview.js";
import * as payments from "./views/payments.js";
import * as webhooks from "./views/webhooks.js";
import * as operations from "./views/operations.js";
import * as simulation from "./views/simulation.js";

const ROUTES = [
  { group: "Encaisser", path: "boutique", label: "Boutique", icon: "store", view: shop },
  { group: "Piloter", path: "apercu", label: "Vue d'ensemble", icon: "dashboard", view: overview },
  { group: "Piloter", path: "paiements", label: "Paiements", icon: "list", view: payments },
  { group: "Piloter", path: "webhooks", label: "Webhooks", icon: "webhook", view: webhooks, badge: "webhooks" },
  { group: "Back-office Xaalis", path: "operations", label: "Rapprochement & T+1", icon: "scale", view: operations },
  { group: "Laboratoire", path: "simulateur", label: "Simulateur opérateurs", icon: "activity", view: simulation, badge: "simulation" },
];

const view = document.getElementById("view");
let current = null; // { route, cleanup }

function renderNav() {
  const groups = new Map();
  for (const route of ROUTES) {
    if (!groups.has(route.group)) groups.set(route.group, []);
    groups.get(route.group).push(route);
  }
  document.getElementById("nav").replaceChildren(
    ...[...groups].map(([label, routes]) =>
      h("div", { class: "nav-group" }, h("div", { class: "nav-label" }, label),
        routes.map((r) => h("a", { href: `#/${r.path}`, dataset: { path: r.path } }, icon(r.icon), h("span", null, r.label),
          r.badge ? h("span", { class: "count", dataset: { badge: r.badge }, hidden: true }) : null)))),
  );
}

async function navigate() {
  const [path, ...rest] = location.hash.replace(/^#\/?/, "").split("/");
  const route = ROUTES.find((r) => r.path === path) || ROUTES[0];
  if (current?.cleanup) current.cleanup();
  for (const a of document.querySelectorAll(".nav a")) {
    if (a.dataset.path === route.path) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  document.getElementById("crumbs").replaceChildren(h("span", null, route.group), icon("chevronRight", { width: 14, height: 14 }), h("strong", null, route.label));
  document.title = `${route.label} — Xaalis`;
  closeMenu();
  view.replaceChildren();
  const cleanup = await route.view.render(view, { params: rest });
  current = { route, cleanup };
  view.focus({ preventScroll: true });
  window.scrollTo({ top: 0 });
}

// ------------------------------------------------------------------ thème
function applyThemeButton() {
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  document.getElementById("theme-toggle").replaceChildren(icon(dark ? "sun" : "moon"), dark ? "Thème clair" : "Thème sombre");
}
document.getElementById("theme-toggle").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  const next = dark ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("xaalis-theme", next); } catch { /* stockage indisponible */ }
  applyThemeButton();
});

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
  } catch (error) {
    document.getElementById("merchant-id").textContent = "passerelle injoignable";
    problemToast(error, "Connexion à la passerelle impossible");
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
  } catch {
    pill.className = "pill bad";
    pill.replaceChildren(h("span", { class: "dot" }), "Passerelle injoignable");
    pill.title = "";
  }
}

const resetBtn = document.getElementById("reset-merchant");
resetBtn.append(icon("rotate"), "Nouveau marchand");
resetBtn.addEventListener("click", async () => {
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
});

document.getElementById("docs-link").append(icon("book"), "Contrat OpenAPI", icon("external", { style: "margin-left:auto;width:13px;height:13px" }));
renderNav();
applyThemeButton();
window.addEventListener("hashchange", navigate);
navigate();
refreshMerchant();
pollHealth();
setInterval(pollHealth, 10_000);
