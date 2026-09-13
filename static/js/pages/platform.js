/* Settings → Platform (admin): the platform mailer — the bridge's own sender
   for invites, reset links and role notices — with a test button and the
   outbox log (pending / sent / failed with the error, retry). */
import { h, card, tag, toast, pageHead, fmtDateTime, clear, confirmDialog } from "../ui.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { dataTable } from "../components/table.js";
import { passwordInput } from "../components/form.js";
import { t } from "../i18n.js";
import { platformOpsCard, broadcastCard } from "../components/ops.js";

const STATUS_TONE = { pending: "warn", sent: "on", failed: "off" };
const HEALTH_TONE = { ok: "on", degraded: "warn", down: "off" };
const CHECK_LABEL = () => ({ database: t("Database"), disk: t("Disk"), backup: t("Backup"), brokers: t("Brokers"), history: t("History writer"),
  event_loop: t("Event loop"), mail: t("Mail outbox"), streams: t("Live streams"), latency: t("Signal latency (1 h)") });

/* alpha.96: the deep health check, refreshed every 30 s. */
function healthCard() {
  const box = h("div", null, h("span", { class: "muted" }, t("Loading…")));
  const head = h("div", { class: "callout" }, "");
  let alive = true, timer = null;
  async function paint() {
    try {
      const r = await api.get("/api/platform/health");
      if (!alive) return;
      head.className = `callout ${r.status === "ok" ? "ok" : r.status === "degraded" ? "warn" : "danger"}`;
      head.textContent = (r.status === "ok" ? t("All checks pass") : r.status === "degraded" ? t("Degraded — see the checks below") : t("Down — something essential is failing"))
        + ` · ${t("version")} ${r.version} · ${t("up {h} h", { h: Math.floor(r.uptime_s / 3600) })}`;
      const L = CHECK_LABEL();
      clear(box);
      box.append(h("table", { class: "data-table compact" }, h("tbody", null, Object.entries(r.checks).map(([k, c]) =>
        h("tr", null, h("td", { style: "width:180px" }, L[k] || k), h("td", null, tag(c.status, HEALTH_TONE[c.status] || "")), h("td", { class: "muted" }, c.detail))))));
    } catch (e) { if (alive) { head.className = "callout danger"; head.textContent = e.message; } }
  }
  paint(); timer = setInterval(paint, 30000);
  return { el: card({ title: t("Health"), hint: t("The same result answers at /readyz for uptime monitors (bearer NEXUSPRED_METRICS_TOKEN or ?token=): 200 while ok or degraded, 503 when down. The public page at /status shows a summary without account data.") }, head, box), cleanup() { alive = false; clearInterval(timer); } };
}

/* alpha.96: platform heartbeat — an outbound ping a monitor expects; when it stops, the monitor alerts. */
function heartbeatCard() {
  const url = h("input", { id: "hb-url", placeholder: "https://hc-ping.com/…", autocomplete: "off" });
  const interval = h("input", { id: "hb-interval", type: "number", min: 30, max: 3600, step: 10, value: 60, style: "max-width:140px" });
  const hint = h("span", { class: "save-hint" });
  const last = h("div", { class: "muted", style: "font-size:12.5px" }, "");
  const paintLast = (r) => { last.textContent = !r.url ? t("Off.") : r.last_at ? (r.ok ? t("Last ping {when}: delivered", { when: fmtDateTime(r.last_at) }) : t("Last ping {when}: failed — {err}", { when: fmtDateTime(r.last_at), err: r.error })) : t("No ping yet."); };
  const save = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
    hint.textContent = t("Saving…"); hint.className = "save-hint"; save.disabled = true;
    try { const r = await api.put("/api/platform/heartbeat", { url: url.value, interval: Number(interval.value) || 60 }); paintLast(r); hint.textContent = r.url ? (r.pinged ? t("Saved and pinged.") : t("Saved, but the first ping failed.")) : t("Saved (off)."); hint.className = `save-hint ${!r.url || r.pinged ? "ok" : "err"}`; }
    catch (e) { hint.textContent = e.message; hint.className = "save-hint err"; } finally { save.disabled = false; }
  } }, icon("check"), t("Save"));
  api.get("/api/platform/heartbeat").then((r) => { url.value = r.url || ""; interval.value = r.interval || 60; paintLast(r); }).catch(() => null);
  return card({ title: t("Platform heartbeat"), hint: t("Unlike the per-workspace watchdog under Settings → Alerts, this ping covers the whole bridge and carries the deep-health result: healthchecks.io gets /fail when the bridge is down, other monitors get ?status=.") },
    h("div", { class: "grid grid-2" },
      h("div", { class: "field" }, h("label", { for: "hb-url" }, t("Ping URL")), url, h("div", { class: "field-hint" }, t("Empty = off. GET every interval; anything below HTTP 400 counts as delivered."))),
      h("div", { class: "field" }, h("label", { for: "hb-interval" }, t("Interval (seconds)")), interval)),
    h("div", { class: "form-actions" }, save, hint), last);
}

/* alpha.96: incidents shown on the public status page. */
function incidentsCard() {
  const STATES = () => [["investigating", t("investigating")], ["identified", t("identified")], ["monitoring", t("monitoring")], ["resolved", t("resolved")]];
  const title = h("input", { id: "inc-title", placeholder: t("Short title, e.g. Tradovate demo logins failing"), maxlength: 140 });
  const status = h("select", { id: "inc-status" }, STATES().map(([v, l]) => h("option", { value: v }, l)));
  const body = h("textarea", { id: "inc-body", rows: 3, placeholder: t("What is affected, what you know, what you are doing."), maxlength: 2000, style: "font-family:inherit" });
  const list = h("div");
  let editing = null;
  const hint = h("span", { class: "save-hint" });
  async function load() {
    try {
      const rows = await api.get("/api/incidents");
      clear(list);
      if (!rows.length) { list.append(h("div", { class: "muted" }, t("No incidents. The status page says all systems operational."))); return; }
      rows.forEach((inc) => list.append(h("div", { class: "callout", style: "display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between" },
        h("div", null, h("strong", null, inc.title), " ", tag(t(inc.status), inc.status === "resolved" ? "on" : inc.status === "monitoring" ? "warn" : "off"),
          h("div", { class: "muted", style: "font-size:12px" }, t("updated {when} · {n} update(s)", { when: fmtDateTime(inc.updated_at), n: (inc.updates || []).length }))),
        h("div", { class: "users-actions" },
          h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: () => { editing = inc.id; title.value = inc.title; status.value = inc.status; body.value = ""; hint.textContent = t("Editing {title} — add an update and save.", { title: inc.title }); hint.className = "save-hint"; title.focus(); } }, icon("edit"), t("Update")),
          h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: async () => {
            if (!(await confirmDialog({ title: t("Delete incident {title}?", { title: inc.title }), body: t("It disappears from the status page and its history is gone."), confirmText: t("Delete"), danger: true }))) return;
            try { await api.del(`/api/incidents/${inc.id}`); load(); } catch (e) { toast(e.message, "error"); }
          } }, icon("trash"), t("Delete"))))));
    } catch (e) { clear(list); list.append(h("div", { class: "muted" }, e.message)); }
  }
  const save = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
    try {
      const payload = { title: title.value, status: status.value, body: body.value };
      if (editing) await api.put(`/api/incidents/${editing}`, payload); else await api.post("/api/incidents", payload);
      editing = null; title.value = ""; body.value = ""; status.value = "investigating"; hint.textContent = t("Published on the status page."); hint.className = "save-hint ok"; load();
    } catch (e) { hint.textContent = e.message; hint.className = "save-hint err"; }
  } }, icon("check"), t("Publish"));
  const cancel = h("button", { type: "button", class: "btn btn-ghost", onClick: () => { editing = null; title.value = ""; body.value = ""; hint.textContent = ""; } }, t("New incident"));
  load();
  return card({ title: t("Incidents on the status page"), actions: [h("a", { class: "btn btn-ghost btn-sm", href: "/status", target: "_blank", rel: "noopener" }, icon("external"), t("Open status page"))] },
    list,
    h("div", { class: "grid grid-2", style: "margin-top:12px" },
      h("div", { class: "field" }, h("label", { for: "inc-title" }, t("Title")), title),
      h("div", { class: "field" }, h("label", { for: "inc-status" }, t("Status")), status)),
    h("div", { class: "field" }, h("label", { for: "inc-body" }, t("Update text")), body),
    h("div", { class: "form-actions" }, save, cancel, hint));
}

export default {
  title: t("Platform"),
  gate: "admin",
  render(root) {
    const provider = h("select", { id: "mail-provider" },
      h("option", { value: "off" }, t("Off — workspace SMTP only")),
      h("option", { value: "smtp" }, "SMTP"),
      h("option", { value: "resend" }, "Resend"),
      h("option", { value: "postmark" }, "Postmark"));
    const host = h("input", { id: "mail-host", placeholder: "smtp.eu.mailgun.org", autocomplete: "off" });
    const port = h("input", { id: "mail-port", type: "number", min: 1, max: 65535, value: 587, style: "max-width:140px" });
    const username = h("input", { id: "mail-username", placeholder: t("SMTP login (optional)"), autocomplete: "off" });
    const password = passwordInput({ id: "mail-password", placeholder: t("SMTP password"), autocomplete: "off" });
    const apiKey = passwordInput({ id: "mail-api-key", placeholder: "re_… / server token", autocomplete: "off" });
    const fromAddr = h("input", { id: "mail-from", type: "email", placeholder: "noreply@your-domain.com", autocomplete: "off" });
    const fromName = h("input", { id: "mail-from-name", placeholder: "Fluxbridge", autocomplete: "off" });
    const replyTo = h("input", { id: "mail-reply-to", type: "email", placeholder: t("support@… (optional)"), autocomplete: "off" });
    const hint = h("span", { class: "save-hint" });
    const testHint = h("span", { class: "save-hint" });
    const status = h("div", { class: "callout" }, "");
    const locked = h("div", { class: "callout warn hidden" }, "");
    const smtpBox = h("div", { class: "grid grid-2" },
      h("div", { class: "field" }, h("label", { for: "mail-host" }, t("SMTP host")), host),
      h("div", { class: "field" }, h("label", { for: "mail-port" }, t("SMTP port")), port, h("div", { class: "field-hint" }, t("587 with STARTTLS, 465 with TLS."))),
      h("div", { class: "field" }, h("label", { for: "mail-username" }, t("SMTP username")), username),
      h("div", { class: "field" }, h("label", { for: "mail-password" }, t("SMTP password")), password.el));
    const apiBox = h("div", { class: "field" }, h("label", { for: "mail-api-key" }, t("API key")), apiKey.el,
      h("div", { class: "field-hint" }, t("Resend: an API key with sending permission. Postmark: the server token of a transactional stream.")));
    function paintBoxes() {
      smtpBox.classList.toggle("hidden", provider.value !== "smtp");
      apiBox.classList.toggle("hidden", provider.value !== "resend" && provider.value !== "postmark");
    }
    provider.addEventListener("change", paintBoxes);
    function paintStatus(c) {
      status.className = `callout ${c.configured ? "ok" : "warn"}`;
      status.textContent = c.configured
        ? t("Live: invites, reset links and role notices go out through {provider}. Queued {pending}, sent {sent}, failed {failed}.", { provider: c.provider, pending: c.counts.pending, sent: c.counts.sent, failed: c.counts.failed })
        : t("Off: platform mail falls back to the SMTP settings of the workspace that sends it — a user without their own SMTP sends nothing.");
      const l = c.env_locked || [];
      locked.classList.toggle("hidden", !l.length);
      locked.textContent = l.length ? t("Pinned by the environment (NEXUSPRED_MAIL_*): {keys}. Those fields are read-only here.", { keys: l.join(", ") }) : "";
      for (const [k, el] of Object.entries({ provider, host, port, username, from_addr: fromAddr, from_name: fromName, reply_to: replyTo })) el.disabled = l.includes(k);
      password.input.disabled = l.includes("password"); apiKey.input.disabled = l.includes("api_key");
    }
    function fill(c) {
      provider.value = c.provider || "off"; host.value = c.host || ""; port.value = c.port || 587; username.value = c.username || "";
      password.input.value = c.password || ""; apiKey.input.value = c.api_key || ""; fromAddr.value = c.from_addr || ""; fromName.value = c.from_name || "";
      replyTo.value = c.reply_to || ""; paintBoxes(); paintStatus(c);
    }
    const saveBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
      hint.textContent = t("Saving…"); hint.className = "save-hint"; saveBtn.disabled = true;
      try {
        const c = await api.put("/api/mail/config", { provider: provider.value, host: host.value, port: Number(port.value) || 587, username: username.value,
          password: password.input.value, api_key: apiKey.input.value, from_addr: fromAddr.value, from_name: fromName.value, reply_to: replyTo.value });
        fill(c); hint.textContent = t("Saved."); hint.className = "save-hint ok"; toast(t("Platform mailer saved"), "success");
      } catch (e) { hint.textContent = e.message; hint.className = "save-hint err"; toast(e.message, "error"); } finally { saveBtn.disabled = false; }
    } }, icon("check"), t("Save"));
    const testBtn = h("button", { type: "button", class: "btn btn-secondary", onClick: async () => {
      testHint.textContent = t("Sending…"); testHint.className = "save-hint"; testBtn.disabled = true;
      try {
        const r = await api.post("/api/mail/test");
        if (r.status === "sent") { testHint.textContent = t("Sent via {route} — check your inbox.", { route: r.route }); testHint.className = "save-hint ok"; toast(t("Test e-mail sent"), "success"); }
        else { testHint.textContent = r.error || t("Not sent"); testHint.className = "save-hint err"; toast(t("Test e-mail failed"), "error"); }
        loadLog();
      } catch (e) { testHint.textContent = e.message; testHint.className = "save-hint err"; toast(e.message, "error"); } finally { testBtn.disabled = false; }
    } }, icon("bell"), t("Send test e-mail to me"));
    const log = dataTable({
      empty: t("No e-mail has been queued yet."),
      compact: true,
      columns: [
        { label: t("When"), render: (r) => fmtDateTime(r.created_at) },
        { label: t("To"), render: (r) => r.to },
        { label: t("Kind"), render: (r) => h("code", null, r.kind || "mail") },
        { label: t("Subject"), render: (r) => r.subject },
        { label: t("Status"), render: (r) => [tag(r.status, STATUS_TONE[r.status] || ""), r.attempts > 1 ? h("span", { class: "muted", style: "margin-left:6px;font-size:11px" }, t("{n} attempts", { n: r.attempts })) : null] },
        { label: t("Route / error"), render: (r) => r.status === "sent" ? h("span", { class: "muted" }, r.route) : h("span", { class: r.last_error ? "" : "muted", style: r.last_error ? "font-size:12px;color:var(--red)" : "font-size:12px" }, r.last_error || (r.status === "pending" ? t("waiting until {when}", { when: fmtDateTime(r.next_at) }) : "—")) },
        { label: "", render: (r) => r.status === "failed" ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: async () => {
          try { const x = await api.post(`/api/mail/retry/${r.id}`); toast(x.status === "sent" ? t("Sent") : (x.error || t("Still failing")), x.status === "sent" ? "success" : "error"); loadLog(); } catch (e) { toast(e.message, "error"); }
        } }, icon("refresh"), t("Retry")) : null },
      ],
    });
    let alive = true;
    const health = healthCard();
    const loadLog = () => api.get("/api/mail/log?limit=100").then((r) => { if (alive) log.update(r.rows, { force: true }); }).catch((e) => { if (alive) toast(e.message, "error"); });
    root.append(
      pageHead(t("Platform"), t("What keeps the whole bridge running: the platform mailer, the deep health check, the heartbeat to your monitor and the incidents on the public status page.")),
      card({ title: t("Platform mailer") },
        status, locked,
        h("div", { class: "grid grid-2" },
          h("div", { class: "field" }, h("label", { for: "mail-provider" }, t("Provider")), provider),
          h("div", { class: "field" }, h("label", { for: "mail-from" }, t("Sender address")), fromAddr, h("div", { class: "field-hint" }, t("Use a domain with SPF and DKIM set up for the provider, or the mail lands in spam."))),
          h("div", { class: "field" }, h("label", { for: "mail-from-name" }, t("Sender name")), fromName),
          h("div", { class: "field" }, h("label", { for: "mail-reply-to" }, t("Reply-to")), replyTo)),
        smtpBox, apiBox,
        h("div", { class: "form-actions" }, saveBtn, hint, testBtn, testHint),
        h("p", { class: "hint" }, t("Delivery is queued: a failed send is retried after 1, 5, 15, 60 and 360 minutes, then marked failed. An address that keeps failing is flagged on the user's dashboard."))),
      card({ title: t("Mail log"), actions: [h("button", { class: "btn btn-ghost btn-sm", onClick: loadLog }, icon("refresh"), t("Refresh"))] }, log.el),
      health.el, heartbeatCard(), incidentsCard(), platformOpsCard(), broadcastCard(),
    );
    api.get("/api/mail/config").then((c) => { if (alive) fill(c); }).catch((e) => { if (alive) toast(e.message, "error"); });
    loadLog();
    return () => { alive = false; health.cleanup(); };
  },
};
