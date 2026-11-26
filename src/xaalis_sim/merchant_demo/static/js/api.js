// Accès au serveur du marchand (/api/*). Le navigateur ne détient aucune clé secrète.

export class Problem extends Error {
  constructor(status, body) {
    const detail = typeof body?.detail === "string" ? body.detail : Array.isArray(body?.detail)
      ? body.detail.map((d) => d.msg).join(" ; ") : undefined;
    super(detail || body?.title || `HTTP ${status}`);
    this.status = status;
    this.title = body?.title || (status === 422 ? "Requête invalide" : `Erreur ${status}`);
    this.detail = detail;
    this.code = body?.code;
    this.body = body;
  }
}

// Une clé d'idempotence par intention de l'utilisateur : un double clic ou une nouvelle
// tentative réseau rejoue la même opération au lieu d'en créer une seconde.
export const newIdempotencyKey = () => crypto.randomUUID();

async function request(method, path, { body, idempotencyKey, signal } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  let response;
  try {
    response = await fetch(`/api${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new Problem(0, { title: "Serveur marchand injoignable", detail: "Vérifiez que la pile Docker est démarrée." });
  }
  const text = await response.text();
  const data = text ? safeJson(text) : null;
  if (!response.ok) throw new Problem(response.status, data);
  return data;
}

function safeJson(text) {
  try { return JSON.parse(text); } catch { return { detail: text }; }
}

export const api = {
  get: (path, options) => request("GET", path, options),
  post: (path, body, options) => request("POST", path, { ...options, body: body ?? {} }),
  delete: (path) => request("DELETE", path),
};

// Petit cache de session : le marchand courant, chargé une fois (onboarding paresseux).
let sessionPromise = null;
export function session({ refresh = false } = {}) {
  if (refresh || !sessionPromise) sessionPromise = api.get("/session").catch((error) => { sessionPromise = null; throw error; });
  return sessionPromise;
}
export function setSession(value) { sessionPromise = Promise.resolve(value); }
