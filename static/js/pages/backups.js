/* Settings → Backups (admin): automatic verified snapshots with rotation,
   an encrypted off-site copy (S3-compatible bucket or e-mail), run-now,
   download and delete. */
import { h, card, tag, toast, pageHead, fmtDateTime, confirmDialog, clear } from "../ui.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { dataTable } from "../components/table.js";
import { passwordInput } from "../components/form.js";
import { t } from "../i18n.js";

const mb = (bytes) => bytes == null ? "—" : bytes < 1048576 ? `${Math.round(bytes / 1024)} KB` : `${(bytes / 1048576).toFixed(1)} MB`;

export default {
  title: t("Backups"),
  gate: "admin",
  render(root) {
    const enabled = h("input", { id: "bk-enabled", type: "checkbox", class: "switch" });
    const hour = h("input", { id: "bk-hour", type: "number", min: 0, max: 23, value: 21, style: "max-width:110px" });
    const minute = h("input", { id: "bk-minute", type: "number", min: 0, max: 59, value: 15, style: "max-width:110px" });
    const keepD = h("input", { id: "bk-daily", type: "number", min: 1, max: 60, value: 7, style: "max-width:110px" });
    const keepW = h("input", { id: "bk-weekly", type: "number", min: 0, max: 52, value: 4, style: "max-width:110px" });
    const keepM = h("input", { id: "bk-monthly", type: "number", min: 0, max: 36, value: 3, style: "max-width:110px" });
    const offsite = h("select", { id: "bk-offsite" },
      h("option", { value: "off" }, t("Off — local snapshots only")),
      h("option", { value: "s3" }, t("S3-compatible bucket (R2, B2, Hetzner, AWS)")),
      h("option", { value: "mail" }, t("E-mail to the admins (small databases)")));
    const s3Endpoint = h("input", { id: "bk-s3-endpoint", placeholder: "https://<account>.r2.cloudflarestorage.com", autocomplete: "off" });
    const s3Bucket = h("input", { id: "bk-s3-bucket", placeholder: "fluxbridge-backups", autocomplete: "off" });
    const s3Region = h("input", { id: "bk-s3-region", placeholder: "auto", autocomplete: "off" });
    const s3Prefix = h("input", { id: "bk-s3-prefix", placeholder: "fluxbridge/", autocomplete: "off" });
    const s3Access = h("input", { id: "bk-s3-access", placeholder: t("Access key ID"), autocomplete: "off" });
    const s3Secret = passwordInput({ id: "bk-s3-secret", placeholder: t("Secret access key"), autocomplete: "off" });
    const mailMax = h("input", { id: "bk-mail-max", type: "number", min: 1, max: 25, value: 20, style: "max-width:110px" });
    const s3Box = h("div", { class: "grid grid-2" },
      h("div", { class: "field" }, h("label", { for: "bk-s3-endpoint" }, t("Endpoint")), s3Endpoint),
      h("div", { class: "field" }, h("label", { for: "bk-s3-bucket" }, t("Bucket")), s3Bucket),
      h("div", { class: "field" }, h("label", { for: "bk-s3-region" }, t("Region")), s3Region, h("div", { class: "field-hint" }, t("R2 and B2 accept \"auto\"; AWS needs the bucket's region."))),
      h("div", { class: "field" }, h("label", { for: "bk-s3-prefix" }, t("Key prefix")), s3Prefix),
      h("div", { class: "field" }, h("label", { for: "bk-s3-access" }, t("Access key ID")), s3Access),
      h("div", { class: "field" }, h("label", { for: "bk-s3-secret" }, t("Secret access key")), s3Secret.el));
    const mailBox = h("div", { class: "field" }, h("label", { for: "bk-mail-max" }, t("Largest attachment (MB)")), mailMax,
      h("div", { class: "field-hint" }, t("Needs the platform mailer (Settings → Platform). Above this size the push fails and the alarm fires — switch to S3 then.")));
    const paintBoxes = () => { s3Box.classList.toggle("hidden", offsite.value !== "s3"); mailBox.classList.toggle("hidden", offsite.value !== "mail"); };
    offsite.addEventListener("change", paintBoxes);
    const status = h("div", { class: "callout" }, "");
    const hint = h("span", { class: "save-hint" });
    const testHint = h("span", { class: "save-hint" });
    const runHint = h("span", { class: "save-hint" });
    function paintStatus(st) {
      const lv = st.last_verified;
      status.className = `callout ${!st.enabled ? "warn" : st.stale ? "danger" : "ok"}`;
      clear(status);
      status.append(
        h("div", null, !st.enabled ? t("Automatic backups are off. The database is only as safe as your last manual download.")
          : lv ? t("Last verified backup {when} ({age} h ago), {n} snapshot(s) kept, next run {next} UTC.", { when: fmtDateTime(lv.created_at), age: st.age_hours, n: st.count, next: (st.next_at || "").slice(11, 16) })
          : t("No verified backup yet — the first run is at {next} UTC, or press \"Back up now\".", { next: (st.next_at || "").slice(11, 16) })),
        h("div", { class: "muted", style: "margin-top:4px;font-size:12px" }, t("Folder {dir} · {free} free", { dir: st.dir, free: mb(st.free_bytes) })));
    }
    function fill(c) {
      enabled.checked = !!c.enabled; hour.value = c.hour_utc; minute.value = c.minute_utc; keepD.value = c.keep_daily; keepW.value = c.keep_weekly; keepM.value = c.keep_monthly;
      offsite.value = c.offsite || "off"; s3Endpoint.value = c.s3_endpoint || ""; s3Bucket.value = c.s3_bucket || ""; s3Region.value = c.s3_region || "auto";
      s3Prefix.value = c.s3_prefix || ""; s3Access.value = c.s3_access_key || ""; s3Secret.input.value = c.s3_secret_key || ""; mailMax.value = c.mail_max_mb || 20; paintBoxes();
    }
    const body = () => ({ enabled: enabled.checked, hour_utc: Number(hour.value), minute_utc: Number(minute.value), keep_daily: Number(keepD.value), keep_weekly: Number(keepW.value),
      keep_monthly: Number(keepM.value), offsite: offsite.value, s3_endpoint: s3Endpoint.value, s3_bucket: s3Bucket.value, s3_region: s3Region.value, s3_prefix: s3Prefix.value,
      s3_access_key: s3Access.value, s3_secret_key: s3Secret.input.value, mail_max_mb: Number(mailMax.value) });
    const saveBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
      hint.textContent = t("Saving…"); hint.className = "save-hint"; saveBtn.disabled = true;
      try { const r = await api.put("/api/backups/config", body()); fill(r.config); paintStatus(r.status); hint.textContent = t("Saved."); hint.className = "save-hint ok"; toast(t("Backup settings saved"), "success"); }
      catch (e) { hint.textContent = e.message; hint.className = "save-hint err"; toast(e.message, "error"); } finally { saveBtn.disabled = false; }
    } }, icon("check"), t("Save"));
    const testBtn = h("button", { type: "button", class: "btn btn-secondary", onClick: async () => {
      testHint.textContent = t("Testing…"); testHint.className = "save-hint"; testBtn.disabled = true;
      try { const r = await api.post("/api/backups/offsite/test"); testHint.textContent = r.detail; testHint.className = `save-hint ${r.ok ? "ok" : "err"}`; toast(r.ok ? t("Off-site target reachable") : t("Off-site test failed"), r.ok ? "success" : "error"); }
      catch (e) { testHint.textContent = e.message; testHint.className = "save-hint err"; } finally { testBtn.disabled = false; }
    } }, icon("refresh"), t("Test off-site target"));
    const runBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
      runHint.textContent = t("Writing snapshot…"); runHint.className = "save-hint"; runBtn.disabled = true;
      try {
        const r = await api.post("/api/backups/run");
        runHint.textContent = r.backup.verified ? t("Written and verified: {name}", { name: r.backup.name }) : t("Written but NOT verified: {detail}", { detail: r.backup.integrity });
        runHint.className = `save-hint ${r.backup.verified ? "ok" : "err"}`; paintStatus(r.status); load();
        if (r.backup.offsite_error) toast(t("Off-site push failed: {err}", { err: r.backup.offsite_error }), "error"); else toast(t("Backup written"), "success");
      } catch (e) { runHint.textContent = e.message; runHint.className = "save-hint err"; toast(e.message, "error"); } finally { runBtn.disabled = false; }
    } }, icon("download"), t("Back up now"));
    const table = dataTable({
      empty: t("No snapshots yet."),
      compact: true,
      columns: [
        { label: t("Snapshot"), render: (b) => h("code", null, b.name) },
        { label: t("Created"), render: (b) => fmtDateTime(b.created_at) },
        { label: t("Size"), className: "num", render: (b) => mb(b.size) },
        { label: t("Verified"), render: (b) => b.verified ? tag(t("verified"), "on") : h("span", { title: `${b.integrity} ${(b.mismatch || []).join(", ")}` }, tag(t("not verified"), "off")) },
        { label: t("Keeps"), render: (b) => (b.roles || []).map((r) => tag(r, "")) },
        { label: t("Off-site"), render: (b) => b.offsite ? tag(b.offsite.split(":")[0], "on") : b.offsite_error ? h("span", { title: b.offsite_error }, tag(t("failed"), "off")) : h("span", { class: "muted" }, "—") },
        { label: "", render: (b) => h("div", { class: "users-actions" },
          h("a", { class: "btn btn-ghost btn-sm", href: `/api/backups/${encodeURIComponent(b.name)}`, download: "" }, icon("download"), t("Download")),
          h("button", { type: "button", class: "btn btn-ghost btn-sm", onClick: async () => {
            if (!(await confirmDialog({ title: t("Delete snapshot {name}?", { name: b.name }), body: t("The local file is removed. An off-site copy stays where it is."), confirmText: t("Delete"), danger: true }))) return;
            try { const r = await api.del(`/api/backups/${encodeURIComponent(b.name)}`); paintStatus(r.status); load(); toast(t("Snapshot deleted"), "success"); } catch (e) { toast(e.message, "error"); }
          } }, icon("trash"), t("Delete"))) },
      ],
    });
    let alive = true;
    const load = () => api.get("/api/backups").then((r) => { if (!alive) return; fill(r.config); paintStatus(r.status); table.update(r.backups, { force: true }); }).catch((e) => { if (alive) toast(e.message, "error"); });
    root.append(
      pageHead(t("Backups"), t("A consistent copy of the whole database every day at the quiet hour, opened and checked before it counts, seven daily / four weekly / three monthly kept, optionally encrypted and pushed off the server. Secrets that must not leave the host (signing key, push key, unused invites) are never in a snapshot.")),
      card({ title: t("Status"), actions: [runBtn] }, status, h("div", { class: "form-actions" }, runHint)),
      card({ title: t("Schedule and retention") },
        h("label", { class: "switch-row" }, h("span", null, t("Automatic daily backups"), h("small", null, t("Off = only manual downloads."))), enabled),
        h("div", { class: "grid grid-3", style: "margin-top:12px" },
          h("div", { class: "field" }, h("label", { for: "bk-hour" }, t("Hour (UTC)")), hour, h("div", { class: "field-hint" }, t("21:15 UTC is after the CME close."))),
          h("div", { class: "field" }, h("label", { for: "bk-minute" }, t("Minute")), minute),
          h("div", null),
          h("div", { class: "field" }, h("label", { for: "bk-daily" }, t("Keep daily")), keepD),
          h("div", { class: "field" }, h("label", { for: "bk-weekly" }, t("Keep weekly (Sundays)")), keepW),
          h("div", { class: "field" }, h("label", { for: "bk-monthly" }, t("Keep monthly (1st)")), keepM))),
      card({ title: t("Off-site copy") },
        h("div", { class: "field" }, h("label", { for: "bk-offsite" }, t("Target")), offsite, h("div", { class: "field-hint" }, t("Every snapshot is encrypted with the bridge's own key before it leaves the server. Decrypt with: python -m app.backups decrypt FILE.db.enc FILE.db"))),
        s3Box, mailBox,
        h("div", { class: "form-actions" }, saveBtn, hint, testBtn, testHint)),
      card({ title: t("Snapshots"), actions: [h("button", { class: "btn btn-ghost btn-sm", onClick: load }, icon("refresh"), t("Refresh"))] }, table.el),
    );
    load();
    return () => { alive = false; };
  },
};
