/* Settings → Platform (admin): the platform mailer — the bridge's own sender
   for invites, reset links and role notices — with a test button and the
   outbox log (pending / sent / failed with the error, retry). */
import { h, card, tag, toast, pageHead, fmtDateTime } from "../ui.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { dataTable } from "../components/table.js";
import { passwordInput } from "../components/form.js";
import { t } from "../i18n.js";

const STATUS_TONE = { pending: "warn", sent: "on", failed: "off" };

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
    const loadLog = () => api.get("/api/mail/log?limit=100").then((r) => { if (alive) log.update(r.rows, { force: true }); }).catch((e) => { if (alive) toast(e.message, "error"); });
    root.append(
      pageHead(t("Platform"), t("The bridge's own e-mail sender. Invites, password-reset links and role notices go out from here in the recipient's language; a user's workspace SMTP stays their channel for trade alerts.")),
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
    );
    api.get("/api/mail/config").then((c) => { if (alive) fill(c); }).catch((e) => { if (alive) toast(e.message, "error"); });
    loadLog();
    return () => { alive = false; };
  },
};
