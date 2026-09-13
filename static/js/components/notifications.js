/* Notification bell (alpha.97): unread badge, a dropdown with the latest
   inbox rows, mark read on click / all at once. The count follows the live
   stream ("notification" kind) and a 60 s poll as a safety net. */
import { h, clear, fmtDateTime, everyVisible } from "../ui.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { store } from "../store.js";
import { t } from "../i18n.js";

const SEV_CLASS = { info: "", warn: "warn", critical: "off" };

export function notificationBell(navigate) {
  const count = h("span", { class: "notif-count hidden" }, "0");
  const btn = h("button", { type: "button", class: "btn btn-ghost btn-icon notif-btn", title: t("Notifications"), "aria-label": t("Notifications") }, icon("bell"), count);
  const list = h("div", { class: "notif-list" }, h("div", { class: "muted", style: "padding:10px" }, t("Loading…")));
  const allBtn = h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: async () => { try { await api.post("/api/notifications/read", {}); await load(); } catch (e) { /* the poll repaints */ } } }, t("Mark all read"));
  const panel = h("div", { class: "notif-panel" }, h("div", { class: "notif-head" }, h("strong", null, t("Notifications")), allBtn), list);
  const wrap = h("div", { class: "notif-menu" }, btn, panel);
  let unread = 0;
  function paintCount(n) {
    unread = n;
    count.textContent = n > 99 ? "99+" : String(n);
    count.classList.toggle("hidden", !n);
    btn.classList.toggle("active", !!n);
  }
  async function load() {
    try {
      const r = await api.get("/api/notifications?limit=30");
      paintCount(r.unread);
      clear(list);
      if (!r.rows.length) { list.append(h("div", { class: "muted", style: "padding:10px" }, t("Nothing yet — alerts land here, read or unread, whatever your channels say."))); return; }
      r.rows.forEach((n) => list.append(h("button", { type: "button", class: `notif-row ${n.read ? "read" : ""}`, onClick: async () => {
        wrap.classList.remove("open");
        if (!n.read) { try { await api.post("/api/notifications/read", { ids: [n.id] }); } catch (e) { /* ignore */ } }
        if (n.url && n.url.startsWith("/#/")) navigate(n.url.slice(2)); else if (n.url) window.location.href = n.url;
        load();
      } }, h("span", { class: `dot ${SEV_CLASS[n.severity] || ""}` }), h("span", { class: "notif-body" }, h("strong", null, n.title), n.message ? h("span", { class: "notif-msg" }, n.message) : null,
        h("span", { class: "notif-time" }, fmtDateTime(n.created_at))))));
    } catch (e) { clear(list); list.append(h("div", { class: "muted", style: "padding:10px" }, e.message)); }
  }
  async function poll() { try { const r = await api.get("/api/notifications/count"); paintCount(r.unread); } catch (e) { /* offline */ } }
  btn.addEventListener("click", (e) => { e.stopPropagation(); const open = !wrap.classList.contains("open"); wrap.classList.toggle("open", open); if (open) load(); });
  panel.addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", () => wrap.classList.remove("open"));
  poll();
  const stop = everyVisible(60000, poll);
  const unsub = store.subscribe("notifPing", (d) => { if (d) { paintCount(unread + 1); if (wrap.classList.contains("open")) load(); } });
  return { el: wrap, cleanup() { if (typeof stop === "function") stop(); unsub(); } };
}
