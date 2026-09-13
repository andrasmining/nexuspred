/* Broadcaster cockpit (alpha.98): the business in one view — subscribers per
   status, revenue, growth per week, signals and latency per listing, the tier,
   and announcements to subscribers. */
import { h, card, tag, toast, pageHead, fmtDateTime, clear, errText } from "../ui.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { dataTable } from "../components/table.js";
import { columnChart, mount, unmount } from "../charts.js";
import { t } from "../i18n.js";

const money = (cents, cur) => `${(Number(cents) / 100).toFixed(2)} ${(cur || "").toUpperCase()}`;
export const TIER_LABEL = () => ({ gold: t("Gold"), silver: t("Silver"), bronze: t("Bronze") });
export function tierTag(tier) { return tier ? h("span", { class: `tag tier-badge ${tier}`, title: t("Tier from facts: time published, verified trades, subscribers, fan-out errors") }, TIER_LABEL()[tier] || tier) : null; }

function kpi(label) {
  const v = h("div", { class: "v" }, h("span", null, "—"));
  const s = h("div", { class: "s muted", style: "font-size:12px" }, "");
  return { el: h("div", { class: "kpi" }, h("div", { class: "k" }, label), v, s), set(val, sub = "") { v.firstChild.textContent = val; s.textContent = sub; } };
}

export default {
  title: t("Cockpit"),
  gate: "publish",
  render(root) {
    const k = { subs: kpi(t("Subscribers")), revenue: kpi(t("Monthly revenue")), listings: kpi(t("Listings")), paused: kpi(t("Needs attention")) };
    const growthBox = h("div", { class: "viz-box" });
    const table = dataTable({
      empty: t("Nothing published yet — publish a webhook or a copy group under Webhooks / Copy Trading."),
      compact: true,
      columns: [
        { label: t("Listing"), render: (l) => [h("strong", null, l.title), " ", tag(l.kind === "copy" ? t("copy trading") : t("signal"), l.kind === "copy" ? "accent" : ""), " ", tierTag(l.tier)] },
        { label: t("Subscribers"), className: "num", render: (l) => [String(l.subscribers), l.paused || l.unpaid || l.pending ? h("span", { class: "muted", style: "font-size:11px;margin-left:6px" }, `${l.pending ? t("{n} pending", { n: l.pending }) + " " : ""}${l.paused ? t("{n} paused", { n: l.paused }) + " " : ""}${l.unpaid ? t("{n} unpaid", { n: l.unpaid }) : ""}`) : null] },
        { label: t("Revenue / month"), className: "num", render: (l) => Object.keys(l.revenue).length ? Object.entries(l.revenue).map(([c, v]) => money(v, c)).join(" · ") : (l.paid ? "0" : t("free")) },
        { label: t("Signals 30 d"), className: "num", render: (l) => l.signals_30d ? `${l.signals_30d.executed || 0} ✓ · ${l.signals_30d.errors || 0} ✗` : "—" },
        { label: t("Error rate"), className: "num", render: (l) => l.error_rate == null ? "—" : h("span", { class: l.error_rate > 0.05 ? "" : "muted", style: l.error_rate > 0.05 ? "color:var(--red)" : "" }, `${(l.error_rate * 100).toFixed(1)} %`) },
        { label: t("Latency p50 / p95"), className: "num", render: (l) => l.latency ? `${l.latency.p50} / ${l.latency.p95} ms` : "—" },
        { label: t("Net 30 d"), className: "num", render: (l) => l.record && l.record.net_30d != null ? h("span", { style: `color:${l.record.net_30d >= 0 ? "var(--green)" : "var(--red)"}` }, `${l.record.net_30d >= 0 ? "+" : ""}${Number(l.record.net_30d).toFixed(2)}`) : "—" },
        { label: t("Since"), render: (l) => l.published_at ? l.published_at.slice(0, 10) : "—" },
      ],
    });
    // announcements
    const listingSel = h("select", { id: "ann-listing" }, h("option", { value: "" }, t("All my listings")));
    const title = h("input", { id: "ann-title", maxlength: 120, placeholder: t("e.g. No trading today — FOMC") });
    const body = h("textarea", { id: "ann-body", rows: 4, maxlength: 2000, placeholder: t("What your subscribers should know: pauses, rollovers, strategy changes."), style: "font-family:inherit" });
    const left = h("span", { class: "muted", style: "font-size:12.5px" }, "");
    const hint = h("span", { class: "save-hint" });
    const sendBtn = h("button", { type: "button", class: "btn btn-primary", onClick: async () => {
      hint.textContent = t("Sending…"); hint.className = "save-hint"; sendBtn.disabled = true;
      try {
        const r = await api.post("/api/announcements", { listing_key: listingSel.value, title: title.value, body: body.value });
        hint.textContent = t("Sent to {n} subscriber(s), {m} by e-mail.", { n: r.recipients, m: r.mailed }); hint.className = "save-hint ok";
        title.value = ""; body.value = ""; toast(t("Announcement sent"), "success"); load();
      } catch (e) { hint.textContent = e.message; hint.className = "save-hint err"; toast(e.message, "error"); } finally { sendBtn.disabled = false; }
    } }, icon("send"), t("Send announcement"));
    const sentList = h("div");
    let alive = true;
    async function load() {
      try {
        const r = await api.get("/api/broadcaster/cockpit");
        if (!alive) return;
        const tt = r.totals;
        k.subs.set(String(tt.subscribers), t("{a} active · {p} pending", { a: tt.active, p: tt.pending }));
        k.revenue.set(Object.keys(tt.revenue).length ? Object.entries(tt.revenue).map(([c, v]) => money(v, c)).join(" · ") : "0", t("paid subscriptions, current period"));
        k.listings.set(String(tt.listings), t("published"));
        k.paused.set(String(tt.paused + tt.unpaid), t("{p} paused · {u} unpaid", { p: tt.paused, u: tt.unpaid }));
        table.update(r.listings, { force: true });
        mount(growthBox, (w) => columnChart(r.growth.map((g) => ({ label: g.week.slice(5), value: g.new })), { width: w, height: 160, valueFmt: (v) => String(Math.round(v)) }));
        clear(listingSel); listingSel.append(h("option", { value: "" }, t("All my listings")), r.listing_keys.map((l) => h("option", { value: l.key }, l.title)));
        left.textContent = t("{n} announcement(s) left today", { n: r.announce_left_today });
        clear(sentList);
        if (!r.announcements.length) sentList.append(h("div", { class: "muted" }, t("No announcement sent yet.")));
        r.announcements.forEach((a) => sentList.append(h("div", { class: "callout", style: "margin-bottom:8px" }, h("strong", null, a.title), " ", h("span", { class: "muted", style: "font-size:12px" }, `${fmtDateTime(a.created_at)} · ${a.listing_key ? (r.listing_keys.find((l) => l.key === a.listing_key) || {}).title || a.listing_key : t("all listings")} · ${t("{n} recipient(s)", { n: a.recipients })}`),
          h("div", { style: "white-space:pre-wrap;font-size:13.5px;margin-top:4px" }, a.body))));
      } catch (e) { if (alive) toast(errText(e.message), "error"); }
    }
    root.append(
      pageHead(t("Cockpit"), t("Your marketplace business in one view: subscribers, revenue, growth, the health of every listing, and a line to your subscribers."), [
        h("button", { class: "btn", onClick: load }, icon("refresh"), t("Refresh"))]),
      h("div", { class: "kpis" }, Object.values(k).map((x) => x.el)),
      card({ title: t("New subscribers per week") }, growthBox),
      card({ title: t("Listings"), hint: t("Tiers come from facts the bridge knows — Bronze after a week with trades or a subscriber, Silver after 30 days with 30 verified trades and a clean fan-out, Gold after 90 days with 100 verified trades, five subscribers and under 2 % errors.") }, table.el),
      card({ title: t("Announcement to subscribers"), hint: t("Reaches every subscriber's inbox and push, and their e-mail if they keep announcements on. At most three a day.") },
        h("div", { class: "grid grid-2" },
          h("div", { class: "field" }, h("label", { for: "ann-listing" }, t("Listing")), listingSel),
          h("div", { class: "field" }, h("label", { for: "ann-title" }, t("Title")), title)),
        h("div", { class: "field" }, h("label", { for: "ann-body" }, t("Message")), body),
        h("div", { class: "form-actions" }, sendBtn, hint, left),
        h("h3", { style: "font-size:14px;margin:16px 0 8px" }, t("Sent")), sentList),
    );
    load();
    return () => { alive = false; unmount(growthBox); };
  },
};
