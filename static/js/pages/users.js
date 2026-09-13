/* Settings → Users (admin): invites, accounts & feature grants, password resets, audit log. */
import { h, card, tag, toast, confirmDialog, copyText, pageHead, fmtDateTime } from "../ui.js";
import { icon } from "../icons.js";
import { openDrawer, closeDrawer } from "../components/drawer.js";
import { api } from "../api.js";
import { store } from "../store.js";
import { dataTable } from "../components/table.js";
import { t } from "../i18n.js";

const ACTION_LABEL = {
  invite_create: t("Invite created"), invite_revoke: t("Invite revoked"), user_delete: t("User deleted"),
  role_set: t("Role changed"), role_request: t("Role requested"), support_view: t("Support view"),
  feature_set: t("Feature changed"), password_reset: t("Password reset"), password_change: t("Password changed"),
  flatten_all: t("Flatten all"), subscribe: t("Subscribed"), unsubscribe: t("Unsubscribed"),
  webhook_share: t("Marketplace publish"), subscriber_remove: t("Subscriber removed"),
  mail_config: t("Platform mailer"), backup_config: t("Backup settings"), backup_run: t("Backup run"), backup_download: t("Backup downloaded"), backup_delete: t("Backup deleted"),
  heartbeat_config: t("Heartbeat"), incident: t("Incident"), announce: t("Announcement"),
  login_ok: t("Signed in"), login_failed: t("Failed sign-in"), login_blocked: t("Rate limited"),
  agent_pairing_code: t("Agent pairing code"), agent_bundle: t("Agent download (preconfigured)"), agent_paired: t("Agent paired"), agent_pair_failed: t("Agent pairing failed"), agent_revoke: t("Agent revoked"),
};

const RANK = { user: 0, broadcaster: 1, admin: 2 };
const ROLE_LABEL = { user: t("User"), broadcaster: t("Broadcaster"), admin: t("Admin") };
const roleTag = (r) => tag(ROLE_LABEL[r] || r, r === "admin" ? "accent" : r === "broadcaster" ? "amber" : "");

export default {
  title: t("Users"),
  gate: "admin",
  render(root) {
    const me = store.get("me") || {};
    const linkBox = (label) => {
      const inp = h("input", { readonly: true, class: "mono input-sm", onClick: (e) => e.target.select() });
      const el = h("div", { class: "callout ok hidden" }, h("div", { class: "field", style: "margin:0" }, h("label", null, label), inp,
        h("div", { class: "form-actions", style: "margin-top:8px" }, h("button", { type: "button", class: "btn btn-sm btn-primary", onClick: async () => toast((await copyText(inp.value)) ? t("Link copied") : t("Copy failed"), "success") }, icon("copy"), t("Copy link")))));
      return { el, show(url, text) { inp.value = url; el.querySelector("label").textContent = text || label; el.classList.remove("hidden"); } };
    };

    // ---- invites
    const inviteEmail = h("input", { type: "email", placeholder: t("name@example.com"), autocomplete: "off" });
    const inviteRole = h("select", { class: "input-sm" }, [["user", t("User")], ["broadcaster", t("Broadcaster")], ["admin", t("Admin")]].map(([v, l]) => h("option", { value: v }, l)));
    const inviteSend = h("input", { type: "checkbox", class: "switch" });
    const inviteLink = linkBox(t("Invite link — share it with the new user"));
    const inviteBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
      inviteBtn.disabled = true;   // one invite per click
      try {
        const email = inviteEmail.value.trim();
        // `elevated`, not `is_admin`: some WAFs block bodies containing is_admin.
        const r = await api.post("/api/users/invite", { role: inviteRole.value, email, send_email: inviteSend.checked });
        const url = r.url || `${window.location.origin}/register?code=${r.code || ""}`;
        inviteLink.show(url);
        copyText(url);
        if (inviteSend.checked && email) toast(r.emailed ? t("Invite emailed to {email}", { email }) : t("Invite created — email not sent (SMTP not configured)"), r.emailed ? "success" : "warn");
        else toast(t("Invite link created and copied"), "success");
        loadInvites(); loadAudit();
      } catch (e) { toast(e.message, "error"); } finally { inviteBtn.disabled = false; }
    } }, icon("plus"), t("Create invite"));

    const invites = dataTable({ empty: t("No open invites"), columns: [
      { label: t("Invite link"), render: (i) => h("code", { style: "font-size:11px" }, `${window.location.origin}/register?code=${i.code}`) },
      { label: t("For"), render: (i) => i.email || t("anyone") },
      { label: t("Role"), render: (i) => roleTag(i.role || (i.is_admin ? "admin" : "user")) },
      { label: t("Created"), render: (i) => fmtDateTime(i.created_at) },
      { label: "", render: (i) => h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: async () => {
        try { await api.del(`/api/invites/${i.code}`); toast(t("Invite revoked")); loadInvites(); loadAudit(); } catch (e) { toast(e.message, "error"); }
      } }, t("Revoke")) },
    ] });

    // ---- alpha.98: the application next to the requester's track record; trial or permanent
    const trialSelect = (value = "30") => h("select", { class: "input-sm" }, [["0", t("Permanent")], ["30", t("30-day trial")], ["90", t("90-day trial")], ["180", t("180-day trial")]].map(([v, l]) => h("option", { value: v, selected: v === value }, l)));
    const rec = (r) => !r || !r.trades ? h("div", { class: "muted" }, t("No journal trades — nothing to verify yet.")) : h("dl", { class: "kv" },
      h("dt", null, t("Trades")), h("dd", null, `${r.trades} · ${r.verified ? t("broker-verified") : t("{p}% verified", { p: Math.round((r.verified_share || 0) * 100) })}`),
      h("dt", null, t("Win rate")), h("dd", null, `${Math.round((r.win_rate || 0) * 100)} %`),
      h("dt", null, t("Profit factor")), h("dd", null, r.profit_factor == null ? "—" : Number(r.profit_factor).toFixed(2)),
      h("dt", null, t("Net P&L")), h("dd", null, Number(r.net_pnl || 0).toFixed(2)),
      h("dt", null, t("Max drawdown")), h("dd", null, Number(r.max_drawdown || 0).toFixed(2)),
      h("dt", null, t("Days active")), h("dd", null, String(r.days_active || 0)));
    async function openApplication(u) {
      let a;
      try { a = await api.get(`/api/users/${u.id}/application`); } catch (e) { toast(e.message, "error"); return; }
      const days = trialSelect("30");
      const note = a.note || {};
      const approve = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
        try { await api.post(`/api/users/${u.id}/role`, { role: "broadcaster", days: Number(days.value) }); closeDrawer(); toast(t("{email} is now a {role}", { email: u.email, role: t("Broadcaster") }), "success"); loadUsers(); loadAudit(); } catch (e) { toast(e.message, "error"); }
      } }, icon("check"), t("Approve"));
      const decline = h("button", { type: "button", class: "btn btn-ghost", onClick: async () => {
        try { await api.post(`/api/users/${u.id}/role`, { role: "user" }); closeDrawer(); toast(t("Request declined"), "warn"); loadUsers(); } catch (e) { toast(e.message, "error"); }
      } }, t("Decline"));
      openDrawer({ title: t("Broadcaster request: {email}", { email: u.email }), width: "560px",
        body: h("div", null,
          h("p", { class: "muted", style: "margin-top:0" }, t("Requested {when} · member since {since} · {n} subscription(s)", { when: fmtDateTime(a.requested_at), since: fmtDateTime(a.member_since), n: a.subscriptions })),
          h("h3", { style: "font-size:14px" }, t("Application")),
          h("dl", { class: "kv" }, h("dt", null, t("Strategy")), h("dd", null, note.strategy || "—"), h("dt", null, t("Instruments")), h("dd", null, note.instruments || "—"),
            h("dt", null, t("Experience")), h("dd", null, note.experience || "—"), h("dt", null, t("Link")), h("dd", null, note.link ? h("a", { href: note.link, target: "_blank", rel: "noopener noreferrer" }, note.link) : "—")),
          h("h3", { style: "font-size:14px" }, t("Track record (journal)")), rec(a.record),
          h("div", { class: "field", style: "margin-top:12px" }, h("label", null, t("Approve as")), days, h("div", { class: "field-hint" }, t("A trial ends by itself: three days before, you get a summary and can extend it here.")))),
        foot: h("div", { style: "display:flex;gap:8px" }, approve, decline) });
    }
    function openTrial(u) {
      const days = trialSelect("30");
      const save = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
        try { const r = await api.post(`/api/users/${u.id}/role`, { role: "broadcaster", days: Number(days.value) }); closeDrawer(); toast(r.expires_at ? t("Trial extended until {date}", { date: r.expires_at.slice(0, 10) }) : t("Broadcaster role made permanent"), "success"); loadUsers(); loadAudit(); } catch (e) { toast(e.message, "error"); }
      } }, icon("check"), t("Save"));
      openDrawer({ title: t("Trial of {email}", { email: u.email }), width: "460px",
        body: h("div", null, h("p", { class: "muted", style: "margin-top:0" }, t("Currently until {date}.", { date: u.role_expires_at.slice(0, 10) })), h("div", { class: "field" }, h("label", null, t("From today")), days)),
        foot: save });
    }

    // ---- users
    const resetLink = linkBox(t("Password-reset link"));
    const users = dataTable({ empty: t("No users"), columns: [
      { label: t("Email"), render: (u) => [u.email, u.id === me.id ? [" ", tag(t("you"))] : null] },
      { label: t("Role"), render: (u) => {
        const role = u.role || (u.is_admin ? "admin" : "user");
        const sel = h("select", { class: "input-sm", "aria-label": t("Role of {email}", { email: u.email }), disabled: u.id === 1 || u.id === me.id,
          onChange: async (e) => {
            const next = e.target.value;
            const losing = RANK[role] >= 1 && RANK[next] < 1;
            if (losing && !(await confirmDialog({ title: t("Withdraw the Broadcaster role from {email}?", { email: u.email }),
              body: t("Their listings are unpublished and subscribers' subscriptions end (Stripe subscriptions are cancelled); their copy groups leave the marketplace but keep running for their own accounts. Nothing is deleted."), confirmText: t("Withdraw"), danger: true }))) { e.target.value = role; return; }
            try { const r = await api.post(`/api/users/${u.id}/role`, { role: next }); toast(t("Role of {email} set to {role}", { email: u.email, role: ROLE_LABEL[next] }), "success"); if (r.effects && r.effects.listings) toast(t("{n} listing(s) unpublished", { n: r.effects.listings }), "warn"); loadUsers(); loadAudit(); }
            catch (err) { e.target.value = role; toast(err.message, "error"); }
          } },
          [["user", t("User")], ["broadcaster", t("Broadcaster")], ["admin", t("Admin")]].map(([v, l]) => h("option", { value: v, selected: v === role }, l)));
        return h("span", { style: "display:inline-flex;gap:6px;align-items:center;flex-wrap:wrap" }, sel,
          u.role_request ? h("button", { type: "button", class: "btn btn-primary btn-sm", title: t("Requested {when}", { when: fmtDateTime(u.role_requested_at) }), onClick: () => openApplication(u) }, icon("check"), t("Review {role} request", { role: ROLE_LABEL[u.role_request] })) : null,
          role === "broadcaster" && u.role_expires_at ? h("button", { type: "button", class: "btn btn-ghost btn-sm", title: t("Trial Broadcaster — click to extend or make permanent"), onClick: () => openTrial(u) }, icon("clock"), t("until {date}", { date: u.role_expires_at.slice(0, 10) })) : null);
      } },
      { label: t("Discord Signals"), render: (u) => h("input", { type: "checkbox", class: "switch", checked: (u.features || {}).discord_signals === true, title: t("Grant the Discord listener module"), onChange: async (e) => {
        try { await api.post(`/api/users/${u.id}/features`, { feature: "discord_signals", enabled: e.target.checked }); toast(t("Discord Signals {state} for {email}", { state: e.target.checked ? t("enabled") : t("disabled"), email: u.email }), "success"); loadAudit(); }
        catch (err) { e.target.checked = !e.target.checked; toast(err.message, "error"); }
      } }) },
      { label: t("2FA"), render: (u) => u.totp_enabled ? tag(t("on"), "on") : u.totp_required ? h("span", { title: t("Enrolment pending — asked for at the next sign-in") }, tag(t("pending"), "warn")) : tag(t("off"), "off") },
      { label: t("Created"), render: (u) => fmtDateTime(u.created_at) },
      { label: t("Last sign-in"), render: (u) => u.last_login_at ? h("span", { title: u.last_login_ip ? t("from {ip}", { ip: u.last_login_ip }) : "" }, fmtDateTime(u.last_login_at)) : t("never") },
      { label: "", render: (u) => h("div", { class: "users-actions" },
        h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: async () => {
          try {
            const r = await api.post(`/api/users/${u.id}/reset`);
            if (r.emailed) {
              toast(t("Reset link emailed to {email}", { email: u.email }), "success");
            } else {
              resetLink.show(r.url, t("Password-reset link for {email} — single use, expires in 24 h", { email: u.email }));
              copyText(r.url);
              toast(t("Reset link created and copied"), "success");
            }
            loadAudit();
          } catch (e) { toast(e.message, "error"); }
        } }, icon("key"), t("Reset password")),
        h("button", { type: "button", class: "btn btn-ghost btn-sm", title: t("Log this user out of every browser and phone (lost device, leaked cookie)."), onClick: async () => {
          if (!(await confirmDialog({ title: t("Sign {email} out everywhere?", { email: u.email }), body: t("Every session of this user is ended immediately; they sign in again with their password."), confirmText: t("Sign out everywhere") }))) return;
          try { await api.post(`/api/users/${u.id}/sessions/revoke`); toast(t("Sessions revoked"), "success"); loadAudit(); } catch (e) { toast(e.message, "error"); }
        } }, icon("logout"), t("Sign out everywhere")),
        u.totp_enabled || u.totp_required ? h("button", { type: "button", class: "btn btn-ghost btn-sm", title: t("Lost authenticator and backup codes: drops both, signs the user out everywhere; they enrol again at the next sign-in."), onClick: async () => {
          if (!(await confirmDialog({ title: t("Reset two-factor setup for {email}?", { email: u.email }), body: t("Their authenticator secret and backup codes are deleted and every session ends. They sign in with the password and set up two-factor authentication again."), confirmText: t("Reset 2FA"), danger: true }))) return;
          try { await api.post(`/api/users/${u.id}/2fa/reset`); toast(t("Two-factor setup reset"), "success"); loadUsers(); loadAudit(); } catch (e) { toast(e.message, "error"); }
        } }, icon("key"), t("Reset 2FA")) : null,
        u.id === me.id ? null : h("button", { type: "button", class: "btn btn-ghost btn-sm", title: t("Open this user's workspace read-only to help with a support question. Every read shows their data; nothing can be changed; the visit is logged."), onClick: async () => {
          try { await api.post(`/api/users/${u.id}/support`); window.location.hash = "#/"; window.location.reload(); } catch (e) { toast(e.message, "error"); }
        } }, icon("user"), t("Support view")),
        u.id === me.id ? null : h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: async () => {
          if (!(await confirmDialog({ title: t("Delete {email}?", { email: u.email }), body: t("Their area and all its data (webhooks, tokens, logs) are removed. This cannot be undone."), confirmText: t("Delete user"), danger: true }))) return;
          try { await api.del(`/api/users/${u.id}`); toast(t("User deleted"), "success"); loadUsers(); loadAudit(); } catch (e) { toast(e.message, "error"); }
        } }, icon("trash"), t("Delete"))) },
    ] });

    const audit = dataTable({ empty: t("No admin actions yet"), compact: true, columns: [
      { label: t("When"), render: (r) => fmtDateTime(r.created_at) },
      { label: t("Admin"), render: (r) => r.actor_email || "—" },
      { label: t("Action"), render: (r) => ACTION_LABEL[r.action] || r.action },
      { label: t("Target"), render: (r) => r.target || "—" },
      { label: t("Detail"), render: (r) => r.detail || "" },
    ] });

    async function loadUsers() { try { const r = await api.get("/api/users"); users.update(r.users || r); } catch (e) { /* ignore */ } }
    async function loadInvites() { try { const list = await api.get("/api/invites"); invites.update(list.filter((i) => !i.used_by)); } catch (e) { /* ignore */ } }
    const logins = dataTable({ empty: t("No sign-ins recorded yet"), compact: true, columns: [
      { label: t("When"), render: (r) => fmtDateTime(r.created_at) },
      { label: t("Email"), render: (r) => r.actor_email || "—" },
      { label: t("Result"), render: (r) => r.action === "login_ok" ? tag(t("ok"), "ok") : r.action === "login_blocked" ? tag(t("rate limited"), "warn") : tag(t("failed"), "error") },
      { label: "IP", render: (r) => h("code", null, r.target || "—") },
      { label: t("Detail"), render: (r) => r.detail || "" },
    ] });
    async function loadAudit() {
      try { audit.update(await api.get("/api/audit")); } catch (e) { /* ignore */ }
      try { logins.update(await api.get("/api/audit?kind=logins")); } catch (e) { /* ignore */ }
    }

    root.append(
      pageHead(t("Users"), t("Registration is invite-only. Every user gets their own isolated area; admins can invite, grant features and reset passwords.")),
      card({ title: t("Create invite") },
        h("div", { class: "grid grid-2" },
          h("div", { class: "field" }, h("label", null, t("Invitee email (optional)")), inviteEmail, h("div", { class: "field-hint" }, t("Pre-fills the sign-up form; leave empty for an open invite."))),
          h("div", null,
            h("div", { class: "field" }, h("label", null, t("Role of the new account")), inviteRole, h("div", { class: "field-hint" }, t("User consumes and runs own webhooks and copy groups · Broadcaster also publishes on the marketplace · Admin operates the platform."))),
            h("label", { class: "switch-row" }, h("span", null, t("Email the invite link"), h("small", null, t("Requires SMTP under Settings → Alerts."))), inviteSend))),
        h("div", { class: "form-actions" }, inviteBtn), inviteLink.el),
      card({ title: t("Accounts"), hint: t("Toggle Discord Signals to grant a user the Discord listener module — its navigation, settings and live connection appear only for users you enable it for.") }, users.el, resetLink.el),
      card({ title: t("Open invites") }, invites.el),
      card({ title: t("Admin activity"), hint: t("Recent admin actions: invites, removals, feature grants, password resets, emergency flattens.") }, audit.el),
      card({ title: t("Sign-ins"), hint: t("Every successful, failed and rate-limited sign-in with the client IP (last 100).") }, logins.el),
    );
    loadUsers(); loadInvites(); loadAudit();
    return () => {};
  },
};
