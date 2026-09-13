/* JSON API client. A 401 means the session expired → back to the login page. */
import { t } from "./i18n.js";

const TIMEOUT_MS = 30000;

export class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

function messageOf(data, fallback) {
  const d = data && data.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d) && d.length && d[0].msg) return d[0].msg;
  if (data && typeof data.message === "string") return data.message;
  return fallback || t("Request failed");
}

/** An AbortSignal that fires after `ms` — AbortSignal.timeout where available, else a controller + timer. */
function timeoutSignal(ms) {
  if (typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function") return { signal: AbortSignal.timeout(ms), cancel: () => {} };
  if (typeof AbortController === "undefined") return { signal: undefined, cancel: () => {} };
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), ms);
  return { signal: ctl.signal, cancel: () => clearTimeout(timer) };
}

async function request(method, path, body) {
  const { signal, cancel } = timeoutSignal(TIMEOUT_MS);
  const init = { method, credentials: "same-origin", headers: {}, signal };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let res, text;
  try {
    res = await fetch(path, init);
    text = await res.text();
  } catch (e) {
    // a hung proxy / dead server: fail loudly so buttons in finally blocks re-enable
    if (e && (e.name === "TimeoutError" || e.name === "AbortError")) throw new ApiError(t("Request timed out"), 0, null);
    throw e;
  } finally { cancel(); }
  let data;
  try { data = text ? JSON.parse(text) : {}; }
  catch {
    // not JSON (a proxy's 502/504 HTML page): never surface the raw body
    data = { detail: t("Server unavailable ({status}) — try again in a moment", { status: res.status }), non_json: true };
  }
  if (res.status === 401) {
    window.location.href = "/login";
    throw new ApiError("Signed out", 401, data);
  }
  if (!res.ok) throw new ApiError(messageOf(data, res.statusText), res.status, data);
  return data;
}

export const api = {
  get: (path) => request("GET", path),
  post: (path, body = {}) => request("POST", path, body),
  put: (path, body = {}) => request("PUT", path, body),
  del: (path, body) => request("DELETE", path, body),
};
