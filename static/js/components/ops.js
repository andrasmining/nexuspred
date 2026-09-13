/* alpha.99 operations widgets: Telegram linking, escalations, settings
   history, support grant, platform thresholds + canary, broadcast, rollback,
   quota hints. Each returns an element (some with .cleanup()). */
import { h, card, tag, toast, confirmDialog, fmtDateTime, clear } from "../ui.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { store } from "../store.js";
import { passwordInput } from "../components/form.js";
import { t } from "../i18n.js";

/* ---------------------------------------------------------------- Telegram (Settings → Alerts) */
export function telegramCard() {
  const status = h("div", { class: "callout" }, t("Loading…"));
  const codeBox = h("div", { class: "hidden", style: "margin-top:10px" });
  const btns = h("div", { class: "form-actions" });
  async function paint() {
    try {
      const s = await api.get("/api/telegram");
      status.className = `callout ${!s.configured ? "warn" : s.linked ? "ok" : ""}`;
      status.textContent = !s.configured ? t("Telegram is not set up on this bridge yet — an admin adds the bot under Settings → Platform.")
        : s.linked ? t("Linked to chat {name}. Alerts at or above the Telegram threshold go there; the last escalation step uses it too.", { name: s.chat_name || "?" })
        : t("Not linked. Get a code, open {bot} in Telegram and send /start followed by the code.", { bot: s.bot_name ? "@" + s.bot_name.replace(/^@/, "") : t("your bot") });
      clear(btns);
      if (!s.configured) return;
      if (s.linked) {
        btns.append(h("button", { type: "button", class: "btn btn-secondary", onClick: async () => { try { await api.post("/api/telegram/test"); toast(t("Telegram message sent"), "success"); } catch (e) { toast(e.message, "error"); } } }, icon("send"), t("Send test message")),
          h("button", { type: "button", class: "btn btn-ghost", onClick: async () => { try { await api.del("/api/telegram/link"); codeBox.classList.add("hidden"); paint(); } catch (e) { toast(e.message, "error"); } } }, t("Unlink")));
      } else {
        btns.append(h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
          try { const r = await api.post("/api/telegram/link-code"); clear(codeBox); codeBox.classList.remove("hidden");
            codeBox.append(h("div", null, t("Send this to the bot within 15 minutes:")), h("code", { style: "font-size:18px;display:inline-block;margin:6px 0;user-select:all" }, `/start ${r.code}`),
              h("div", { class: "muted", style: "font-size:12px" }, t("The page updates by itself once the chat is linked.")));
            const timer = setInterval(async () => { const s2 = await api.get("/api/telegram").catch(() => null); if (s2 && s2.linked) { clearInterval(timer); codeBox.classList.add("hidden"); paint(); toast(t("Telegram linked"), "success"); } }, 4000);
            setTimeout(() => clearInterval(timer), 15 * 60000);
          } catch (e) { toast(e.message, "error"); }
        } }, icon("link"), t("Get a link code")));
      }
    } catch (e) { status.className = "callout danger"; status.textContent = e.message; }
  }
  paint();
  return card({ title: t("Telegram"), hint: t("A chat that receives your alerts — switched on and thresholded below like the other channels.") }, status, codeBox, btns);
}

/* ---------------------------------------------------------------- escalations (Settings → Alerts) */
export function escalationsCard() {
  const box = h("div", null, h("span", { class: "muted" }, t("Loading…")));
  let alive = true, timer = null;
  async function paint() {
    try {
      const r = await api.get("/api/escalations");
      if (!alive) return;
      clear(box);
      if (!r.open.length && !r.recent.length) { box.append(h("div", { class: "muted" }, t("No escalation yet. A critical alert opens one when escalation is switched on."))); return; }
      r.open.forEach((e) => box.append(h("div", { class: "callout danger", style: "display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between" },
        h("div", null, h("strong", null, e.title), h("div", { class: "muted", style: "font-size:12px" }, t("since {when} · stage {stage}", { when: fmtDateTime(e.created_at), stage: e.stage }))),
        h("button", { type: "button", class: "btn btn-primary btn-sm", onClick: async () => { try { await api.post(`/api/escalations/${e.id}/ack`, {}); toast(t("Acknowledged"), "success"); paint(); } catch (err) { toast(err.message, "error"); } } }, icon("check"), t("Acknowledge")))));
      const done = r.recent.filter((e) => e.acked_at).slice(0, 5);
      if (done.length) box.append(h("div", { class: "muted", style: "font-size:12.5px;margin-top:6px" }, done.map((e) => h("div", null, `${fmtDateTime(e.created_at)} · ${e.title} · ${t("acknowledged by {who}", { who: e.acked_by })}`))));
    } catch (e) { if (alive) { clear(box); box.append(h("span", { class: "muted" }, e.message)); } }
  }
  paint(); timer = setInterval(paint, 20000);
  const el = card({ title: t("Escalations"), hint: t("Push at once with an acknowledge link, e-mail after 2 minutes, Telegram / SMS after 5 — until someone acknowledges.") }, box);
  el.cleanup = () => { alive = false; clearInterval(timer); };
  return el;
}

/* ---------------------------------------------------------------- settings history (Settings → General) */
export function historyCard() {
  const box = h("div", null, h("span", { class: "muted" }, t("Loading…")));
  async function paint() {
    try {
      const r = await api.get("/api/settings/history");
      clear(box);
      if (!r.versions.length) { box.append(h("div", { class: "muted" }, t("No versions yet — every meaningful change creates one."))); return; }
      box.append(h("table", { class: "data-table compact" }, h("tbody", null, r.versions.map((v, i) => h("tr", null,
        h("td", { style: "white-space:nowrap" }, fmtDateTime(v.ts)), h("td", { class: "muted" }, v.actor), h("td", null, h("code", { style: "font-size:11px" }, v.keys.slice(0, 6).join(", ") + (v.keys.length > 6 ? " …" : ""))),
        h("td", { style: "text-align:right" }, i === 0 ? tag(t("current"), "on") : h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: async () => {
          if (!(await confirmDialog({ title: t("Restore the settings of {when}?", { when: fmtDateTime(v.ts) }), body: t("Webhooks, routing, symbol map, alert and risk settings go back to that version. The current state is kept as a new version, so this can be undone the same way."), confirmText: t("Restore"), danger: true }))) return;
          try { await api.post(`/api/settings/history/${v.id}/restore`, {}); toast(t("Settings restored"), "success"); window.location.reload(); } catch (e) { toast(e.message, "error"); }
        } }, icon("refresh"), t("Restore"))))))));
    } catch (e) { clear(box); box.append(h("span", { class: "muted" }, e.message)); }
  }
  paint();
  return card({ title: t("Settings history"), hint: t("The last 30 versions of this workspace's settings with who changed what. \"Since yesterday it trades wrong\" is one click away from yesterday.") }, box);
}

/* ---------------------------------------------------------------- support grant (Settings → Account) */
export function supportGrantCard() {
  const me = store.get("me") || {};
  const sw = h("input", { type: "checkbox", class: "switch", checked: !!me.support_grant });
  const until = h("div", { class: "muted", style: "font-size:12.5px;margin-top:6px" }, me.support_grant ? t("Write access until {when}.", { when: fmtDateTime(new Date(me.support_grant.until * 1000).toISOString()) }) : t("Support can only read your workspace."));
  sw.addEventListener("change", async (e) => {
    try { const r = await api.post("/api/me/support-grant", { hours: e.target.checked ? 24 : 0 }); until.textContent = r.grant ? t("Write access until {when}.", { when: fmtDateTime(new Date(r.grant.until * 1000).toISOString()) }) : t("Support can only read your workspace."); toast(r.grant ? t("Support may change settings for 24 hours") : t("Support access revoked"), r.grant ? "warn" : "success"); }
    catch (err) { e.target.checked = !e.target.checked; toast(err.message, "error"); }
  });
  return card({ title: t("Assisted support") },
    h("label", { class: "switch-row" }, h("span", null, t("Let support change my settings for 24 hours"), h("small", null, t("An admin in the support view can then fix things for you. Every change is logged with the admin's name; the access ends by itself."))), sw), until);
}

/* ---------------------------------------------------------------- platform thresholds, channels, canary (Settings → Platform) */
export function platformOpsCard() {
  const tgToken = passwordInput({ id: "pf-tg-token", placeholder: "123456:ABC…", autocomplete: "off" });
  const tgName = h("input", { id: "pf-tg-name", placeholder: "fluxbridge_alerts_bot", autocomplete: "off" });
  const twSid = h("input", { id: "pf-tw-sid", placeholder: "AC…", autocomplete: "off" });
  const twToken = passwordInput({ id: "pf-tw-token", autocomplete: "off" });
  const twFrom = h("input", { id: "pf-tw-from", placeholder: "+41…", autocomplete: "off" });
  const lat = h("input", { id: "pf-lat", type: "number", min: 50, max: 60000, style: "max-width:140px" });
  const lag = h("input", { id: "pf-lag", type: "number", min: 20, max: 10000, style: "max-width:140px" });
  const canOn = h("input", { id: "pf-canary", type: "checkbox", class: "switch" });
  const canMin = h("input", { id: "pf-canary-min", type: "number", min: 1, max: 1440, style: "max-width:140px" });
  const canMax = h("input", { id: "pf-canary-max", type: "number", min: 100, max: 60000, style: "max-width:140px" });
  const canStatus = h("div", { class: "callout" }, "");
  const hint = h("span", { class: "save-hint" });
  function paintCanary(c) {
    const last = c.last;
    canStatus.className = `callout ${!c.enabled ? "" : !last ? "" : last.ok && !last.slow ? "ok" : last.ok ? "warn" : "danger"}`;
    canStatus.textContent = !c.enabled ? t("Canary off.") : !last ? t("Canary on — no run yet.")
      : t("Last canary {when}: {detail} in {ms} ms · {runs} runs · {share}% ok · p95 {p95} ms", { when: fmtDateTime(last.at), detail: last.detail, ms: last.ms, runs: c.runs, share: Math.round((c.ok_share || 0) * 100), p95: c.p95_ms ?? "—" });
  }
  function fill(c) {
    tgToken.input.value = c.telegram_bot_token || ""; tgName.value = c.telegram_bot_name || ""; twSid.value = c.twilio_sid || ""; twToken.input.value = c.twilio_token || ""; twFrom.value = c.twilio_from || "";
    lat.value = c.latency_p95_warn_ms; lag.value = c.loop_lag_warn_ms; canOn.checked = !!c.canary_enabled; canMin.value = c.canary_minutes; canMax.value = c.canary_max_ms; paintCanary(c.canary || {});
  }
  const save = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
    hint.textContent = t("Saving…"); hint.className = "save-hint"; save.disabled = true;
    try { const c = await api.put("/api/platform/config", { telegram_bot_token: tgToken.input.value, telegram_bot_name: tgName.value, twilio_sid: twSid.value, twilio_token: twToken.input.value, twilio_from: twFrom.value,
      latency_p95_warn_ms: Number(lat.value), loop_lag_warn_ms: Number(lag.value), canary_enabled: canOn.checked, canary_minutes: Number(canMin.value), canary_max_ms: Number(canMax.value) });
      fill(c); hint.textContent = t("Saved."); hint.className = "save-hint ok"; }
    catch (e) { hint.textContent = e.message; hint.className = "save-hint err"; } finally { save.disabled = false; }
  } }, icon("check"), t("Save"));
  const run = h("button", { type: "button", class: "btn btn-secondary", onClick: async () => {
    run.disabled = true; try { const r = await api.post("/api/platform/canary/run", {}); paintCanary(r.status); toast(r.result.ok ? t("Canary passed in {ms} ms", { ms: r.result.ms }) : t("Canary failed: {detail}", { detail: r.result.detail }), r.result.ok ? "success" : "error"); } catch (e) { toast(e.message, "error"); } finally { run.disabled = false; }
  } }, icon("play"), t("Run canary now"));
  api.get("/api/platform/config").then(fill).catch((e) => toast(e.message, "error"));
  return card({ title: t("Channels, thresholds, canary") },
    h("h3", { style: "font-size:13px;margin:0 0 8px" }, t("Telegram bot")),
    h("div", { class: "grid grid-2" }, h("div", { class: "field" }, h("label", { for: "pf-tg-token" }, t("Bot token")), tgToken.el, h("div", { class: "field-hint" }, t("From @BotFather. Users link their chat under Settings → Alerts."))),
      h("div", { class: "field" }, h("label", { for: "pf-tg-name" }, t("Bot username")), tgName)),
    h("h3", { style: "font-size:13px;margin:12px 0 8px" }, t("SMS (Twilio, last escalation step)")),
    h("div", { class: "grid grid-3" }, h("div", { class: "field" }, h("label", { for: "pf-tw-sid" }, t("Account SID")), twSid), h("div", { class: "field" }, h("label", { for: "pf-tw-token" }, t("Auth token")), twToken.el), h("div", { class: "field" }, h("label", { for: "pf-tw-from" }, t("Sender number")), twFrom)),
    h("h3", { style: "font-size:13px;margin:12px 0 8px" }, t("Latency watchdog")),
    h("div", { class: "grid grid-2" }, h("div", { class: "field" }, h("label", { for: "pf-lat" }, t("Alert when signal p95 exceeds (ms)")), lat), h("div", { class: "field" }, h("label", { for: "pf-lag" }, t("Alert when event-loop lag exceeds (ms)")), lag)),
    h("h3", { style: "font-size:13px;margin:12px 0 8px" }, t("Canary signal")),
    h("label", { class: "switch-row" }, h("span", null, t("Run a synthetic signal through the whole path"), h("small", null, t("Receipt, parsing, sizing, order, bookkeeping — in the simulator, timed; a failure or a slow run alerts the admins."))), canOn),
    h("div", { class: "grid grid-2", style: "margin-top:8px" }, h("div", { class: "field" }, h("label", { for: "pf-canary-min" }, t("Every (minutes)")), canMin), h("div", { class: "field" }, h("label", { for: "pf-canary-max" }, t("Slow above (ms)")), canMax)),
    canStatus,
    h("div", { class: "form-actions" }, save, hint, run));
}

/* ---------------------------------------------------------------- broadcast (Settings → Platform) */
export function broadcastCard() {
  const title = h("input", { id: "bc-title", maxlength: 140, placeholder: t("e.g. Maintenance Sunday 03:00–03:30 UTC") });
  const body = h("textarea", { id: "bc-body", rows: 3, maxlength: 2000, style: "font-family:inherit" });
  const roles = ["user", "broadcaster", "admin"].map((r) => ({ r, el: h("input", { type: "checkbox", checked: true }) }));
  const mail = h("input", { type: "checkbox", class: "switch", checked: true });
  const hours = h("input", { id: "bc-hours", type: "number", min: 0, max: 720, value: 24, style: "max-width:120px" });
  const hint = h("span", { class: "save-hint" });
  const send = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
    if (!(await confirmDialog({ title: t("Send to everyone selected?"), body: t("Inbox for all of them, e-mail if switched on, and a banner for the given hours."), confirmText: t("Send") }))) return;
    send.disabled = true;
    try { const sel = roles.filter((x) => x.el.checked).map((x) => x.r); const r = await api.post("/api/broadcast", { title: title.value, body: body.value, roles: sel.length === 3 ? [] : sel, mail: mail.checked, banner_hours: Number(hours.value) || 0 });
      hint.textContent = t("Sent to {n} user(s), {m} by e-mail.", { n: r.recipients, m: r.mailed }); hint.className = "save-hint ok"; title.value = ""; body.value = ""; }
    catch (e) { hint.textContent = e.message; hint.className = "save-hint err"; } finally { send.disabled = false; }
  } }, icon("send"), t("Send broadcast"));
  const clearBtn = h("button", { type: "button", class: "btn btn-ghost", onClick: async () => { try { await api.del("/api/broadcast/banner"); toast(t("Banner removed"), "success"); } catch (e) { toast(e.message, "error"); } } }, t("Remove current banner"));
  return card({ title: t("Broadcast to users"), hint: t("Maintenance windows, rule changes, a new broker: to everyone or one role, as inbox row, e-mail and a banner every viewer can dismiss.") },
    h("div", { class: "grid grid-2" }, h("div", { class: "field" }, h("label", { for: "bc-title" }, t("Title")), title), h("div", { class: "field" }, h("label", { for: "bc-hours" }, t("Banner for (hours, 0 = none)")), hours)),
    h("div", { class: "field" }, h("label", { for: "bc-body" }, t("Message")), body),
    h("div", { style: "display:flex;flex-wrap:wrap;gap:16px;align-items:center" }, roles.map((x) => h("label", { class: "check-row", style: "padding:0;border:0" }, x.el, " ", { user: t("Users"), broadcaster: t("Broadcasters"), admin: t("Admins") }[x.r])),
      h("label", { class: "check-row", style: "padding:0;border:0" }, mail, " ", t("also by e-mail"))),
    h("div", { class: "form-actions" }, send, hint, clearBtn));
}

/* ---------------------------------------------------------------- rollback (Settings → Updates) */
export function rollbackCard() {
  const box = h("div", null, h("span", { class: "muted" }, t("Loading…")));
  api.get("/api/update/rollback").then((r) => {
    clear(box);
    const p = r.point;
    if (!p) { box.append(h("div", { class: "muted" }, t("No update recorded yet — the first one-click update creates a rollback point."))); return; }
    box.append(h("div", { class: "callout" }, t("Before the last update: v{version} ({sha}) on {when}{backup}.", { version: p.version, sha: (p.sha || "").slice(0, 10), when: fmtDateTime(p.at), backup: p.backup ? t(" — snapshot {name}", { name: p.backup }) : t(" — no snapshot") })));
    const go = async (restoreDb) => {
      if (!(await confirmDialog({ title: restoreDb ? t("Roll back code and database?") : t("Roll back the code?"), body: restoreDb ? t("The checkout returns to the previous revision and the database from before the update replaces the current one at the restart (the current file is kept as fluxbridge.db.pre-rollback). Everything since the update is lost.") : t("The checkout returns to the previous revision and the bridge restarts. The database stays as it is."), confirmText: t("Roll back"), danger: true }))) return;
      try { const r = await api.post("/api/update/rollback", { restore_db: restoreDb }); toast(r.message, r.success ? "warn" : "error"); } catch (e) { toast(e.message, "error"); }
    };
    box.append(h("div", { class: "form-actions" }, h("button", { type: "button", class: "btn", onClick: () => go(false) }, icon("chevronLeft"), t("Roll back code")),
      p.backup ? h("button", { type: "button", class: "btn btn-ghost", onClick: () => go(true) }, t("Roll back code + database")) : null));
  }).catch((e) => { clear(box); box.append(h("span", { class: "muted" }, e.message)); });
  return card({ title: t("Rollback") }, box);
}

/* ---------------------------------------------------------------- quota hint for page heads */
export function quotaHint(kind) {
  const q = ((store.get("me") || {}).quotas || {})[kind];
  if (!q || q.max == null) return null;
  return h("span", { class: `tag ${q.used >= q.max ? "off" : ""}`, title: t("Your role's limit — an admin can raise it") }, t("{used} of {max}", { used: q.used, max: q.max }));
}
