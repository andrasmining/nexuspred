/* Fluxbridge dashboard — bootstrap, hash router, shell (sidebar + topbar),
   live stream and the reconcile polling. Build-free ES modules. */
import { $, h, clear, toast, closeDialogs, everyVisible } from "./ui.js";
import { store, can } from "./store.js";
import { api } from "./api.js";
import { actions } from "./actions.js";
import { connectStream } from "./stream.js";
import { parseHash, matchRoute, navigate as go } from "./router.js";
import { initTheme } from "./theme.js";
import { renderSidebar, titleFor } from "./components/sidebar.js";
import { renderTopbar } from "./components/topbar.js";
import { closeDrawer } from "./components/drawer.js";
import { ROUTES } from "./pages/index.js";
import { registerWorker } from "./push.js";
import { t } from "./i18n.js";

const shell = $("#shell");
const view = $("#view");
const sidebarEl = $("#sidebar");
const topbarEl = $("#topbar");
const VERSION = window.__FB_VERSION__ || "";

let collapsed = false;
try { collapsed = localStorage.getItem("np_sidebar_collapsed") === "1"; } catch (e) { /* ignore */ }

function setCollapsed(on) {
  collapsed = on;
  shell.classList.toggle("collapsed", on);
  try { localStorage.setItem("np_sidebar_collapsed", on ? "1" : "0"); } catch (e) { /* ignore */ }
  paintSidebar();
}

function navigate(path, opts = {}) {
  if (!opts.keepDrawer) shell.classList.remove("sidebar-open");
  go(path, opts);
}

function paintSidebar() {
  const r = store.get("route");
  renderSidebar(sidebarEl, { me: store.get("me"), path: r ? r.path : "/", collapsed, onToggleCollapse: setCollapsed, navigate, version: VERSION });
}

/* ------------------------------------------------------------- support view */
let supportBanner = null;
let mailBanner = null;
let noticeBanner = null;
/* alpha.99: an admin broadcast banner, dismissable per viewer */
function paintNoticeBanner() {
  const me = store.get("me");
  if (noticeBanner) { noticeBanner.remove(); noticeBanner = null; }
  const b = me && me.banner;
  if (!b) return;
  try { if (localStorage.getItem("fb_banner_dismissed") === b.id) return; } catch (e) { /* ignore */ }
  noticeBanner = h("div", { class: "support-banner warn", role: "status" },
    h("strong", null, b.title), " ", b.body,
    h("button", { type: "button", class: "btn btn-sm", onClick: () => { try { localStorage.setItem("fb_banner_dismissed", b.id); } catch (e) { /* ignore */ } noticeBanner.remove(); noticeBanner = null; } }, t("Dismiss")));
  shell.prepend(noticeBanner);
}
/* alpha.97: the release notes once per version, after the shell is up */
async function showWhatsNew() {
  try {
    const r = await api.get("/api/whatsnew");
    if (r.seen) return;
    if (r.first_login || !r.bullets.length) { await api.post("/api/whatsnew/seen", {}); return; }
    const { openDrawer, closeDrawer } = await import("./components/drawer.js");
    openDrawer({ title: t("What's new in {version}", { version: r.version }),
      body: h("div", null, h("ul", { class: "whatsnew" }, r.bullets.map((b) => h("li", null, b.replace(/\*\*|`/g, "")))), h("p", { class: "hint" }, t("The full changelog is on GitHub."))),
      foot: h("button", { type: "button", class: "btn btn-primary", onClick: async () => { closeDrawer(); try { await api.post("/api/whatsnew/seen", {}); } catch (e) { /* shown again next time */ } } }, t("Got it")),
      width: "560px" });
  } catch (e) { /* not essential */ }
}
function paintMailBanner() {
  const me = store.get("me");
  if (mailBanner) { mailBanner.remove(); mailBanner = null; }
  if (!me || !me.mail_blocked) return;
  mailBanner = h("div", { class: "support-banner warn", role: "status" },
    h("strong", null, t("E-mail not reaching you")), " ", t("Several messages to {email} could not be delivered. Check the address under Settings → Account or ask your admin to look at the mail log.", { email: me.email }));
  shell.prepend(mailBanner);
}
function paintSupportBanner() {
  const me = store.get("me");
  if (supportBanner) { supportBanner.remove(); supportBanner = null; }
  if (!me || !me.support) return;
  supportBanner = h("div", { class: "support-banner", role: "status" },
    h("strong", null, t("Support view")), " ", me.support_grant ? t("You are looking at the workspace of {email}. The user granted write access — every change is logged under your name.", { email: me.support.email })
      : t("You are looking at the workspace of {email}. Everything is read-only; nothing you do here changes their data.", { email: me.support.email }),
    h("button", { type: "button", class: "btn btn-sm", onClick: async () => {
      const { promptDialog } = await import("./ui.js");
      const text = await promptDialog({ title: t("Note to {email}", { email: me.support.email }), label: t("They see it in their inbox."), placeholder: t("What you looked at, what to do next…") });
      if (!text) return;
      try { await api.post("/api/support/note", { message: text }); toast(t("Note left"), "success"); } catch (e) { toast(e.message, "error"); }
    } }, t("Leave a note")),
    h("button", { type: "button", class: "btn btn-sm", onClick: async () => { try { await api.post("/api/support/exit"); window.location.hash = "#/settings/users"; window.location.reload(); } catch (e) { toast(e.message, "error"); } } }, t("Leave support view")));
  shell.prepend(supportBanner);
}

/* ------------------------------------------------------------- routing */
let cleanup = null;
let currentPath = null;

function render() {
  const { path, query } = parseHash();
  const me = store.get("me");
  const m = matchRoute(ROUTES, path);
  if (!m) { go("/", { replace: true }); return; }
  if (m.route.redirect) { go(m.route.redirect, { replace: true }); return; }
  const page = m.route.page;
  if (page.gate && !can(me, page.gate)) { toast(t("That page isn't available for your account"), "warn"); go("/", { replace: true }); return; }

  const samePage = currentPath !== null && matchRoute(ROUTES, currentPath)?.route.page === page;
  currentPath = path;
  if (samePage) {
    // Same page, different params (e.g. the webhook drawer deep link): the page
    // listens to `route` itself — don't tear it down.
    store.set("route", { path, params: m.params, query, title: titleFor(path) });
    paintSidebar();
    document.title = `${titleFor(path)} · Fluxbridge`;
    return;
  }
  // Tear the old page down first (it may close its drawer), then announce the route.
  closeDialogs();
  if (cleanup) { try { cleanup(); } catch (e) { console.error(e); } cleanup = null; }
  closeDrawer();
  store.set("route", { path, params: m.params, query, title: titleFor(path) });
  clear(view);
  view.scrollTop = 0;
  window.scrollTo({ top: 0 });
  try {
    cleanup = page.render(view, { params: m.params, query, navigate, store }) || null;
  } catch (e) {
    console.error(e);
    view.append(h("div", { class: "callout danger" }, t("This page failed to render: "), e.message));
  }
  paintSidebar();
  document.title = `${titleFor(path)} · Fluxbridge`;
}

/* ---------------------------------------------------------------- boot */
async function boot() {
  initTheme();
  shell.classList.toggle("collapsed", collapsed);
  $("#scrim").addEventListener("click", () => shell.classList.remove("sidebar-open"));
  renderTopbar(topbarEl, { navigate, onHamburger: () => shell.classList.toggle("sidebar-open"),
    onPrivacy: () => { currentPath = null; render(); } });   // repaint the page with (un)masked names

  try {
    await actions.loadMe();
  } catch (e) {
    view.append(h("div", { class: "callout danger" }, t("Could not load your account: "), e.message));
    return;
  }
  paintSupportBanner();
  paintMailBanner();
  paintNoticeBanner();
  // Data the shell and most pages need right away.
  await Promise.all([actions.loadSettings().catch(() => null), actions.refreshStatus(), actions.loadWebhooks(), actions.loadTradeAccounts()]);
  window.addEventListener("hashchange", render);
  render();
  showWhatsNew();                       // after the first page paint: a route render closes open drawers

  connectStream();
  registerWorker();  // push notifications (no-op where unsupported); never caches pages
  actions.refreshOrders();
  actions.refreshLogs();
  actions.refreshDiscordStatus();
  actions.loadDiscordFeed();
  if (can(store.get("me"), "admin") && (store.get("settings") || {}).auto_check_updates !== false) actions.checkUpdate();

  // Reconcile polling — the stream delivers changes instantly; these catch anything
  // missed. Paused while the tab is hidden, one catch-up poll when it comes back.
  everyVisible(15000, actions.refreshStatus);
  everyVisible(60000, actions.refreshOrders);
  everyVisible(60000, actions.refreshLogs);
  everyVisible(10000, actions.refreshDiscordStatus);
  let dirtyTimer = null;
  store.subscribe("statusDirty", () => { clearTimeout(dirtyTimer); dirtyTimer = setTimeout(actions.refreshStatus, 600); });
  store.subscribe("streamResync", () => { actions.refreshOrders(); actions.refreshLogs(); });   // after a stream gap: re-pull what we missed
  store.subscribe("me", paintSidebar);
}

boot();
