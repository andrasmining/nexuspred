/* Right-hand drawer (detail panel) with scrim, Esc to close. One at a time.
   A real modal: focus moves inside on open, Tab cycles within the panel, the
   app shell is inert while it is open, focus returns to the opener on close. */
import { h, $, clear } from "../ui.js";
import { icon } from "../icons.js";
import { t } from "../i18n.js";

let current = null;
let closeTimer = null;
let titleSeq = 0;

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
const focusable = (root) => [...root.querySelectorAll(FOCUSABLE)].filter((el) => !el.hidden && !el.closest("[hidden], .hidden") && el.getClientRects().length);

function setShellInert(on) {
  const shell = $("#shell");
  if (!shell) return;
  if ("inert" in shell) shell.inert = on;
  else if (on) shell.setAttribute("aria-hidden", "true");
  else shell.removeAttribute("aria-hidden");
}

export function openDrawer({ title, body, foot = null, onClose = null, width = null }) {
  // a drawer opened from inside another one keeps the original opener as the element to return to
  const prevFocus = current ? current.prevFocus : document.activeElement;
  closeDrawer();
  if (closeTimer) { clearTimeout(closeTimer); closeTimer = null; }   // a drawer opened during the fade must survive it
  const root = $("#drawerRoot");
  clear(root);
  root.className = "drawer-root";
  const titleId = `drawer-title-${++titleSeq}`;
  const titleEl = h("h2", { id: titleId }, title);
  const bodyEl = h("div", { class: "drawer-body" }, body);
  const footEl = h("div", { class: "drawer-foot" }, foot);
  const closeBtn = h("button", { type: "button", class: "btn btn-ghost btn-icon", title: t("Close"), "aria-label": t("Close"), onClick: () => closeDrawer() }, icon("x"));
  const panel = h("div", { class: "drawer", role: "dialog", "aria-modal": "true", "aria-labelledby": titleId, tabindex: "-1", style: width ? `width:min(${width}, 100vw)` : null },
    h("div", { class: "drawer-head" }, titleEl, closeBtn),
    bodyEl, foot ? footEl : null);
  const scrim = h("div", { class: "drawer-scrim", onClick: () => closeDrawer() });
  root.append(scrim, panel);
  requestAnimationFrame(() => root.classList.add("open"));
  const onKey = (e) => {
    if (e.key === "Escape") { closeDrawer(); return; }
    if (e.key !== "Tab") return;
    // trap Tab / Shift+Tab inside the panel
    const els = focusable(panel);
    if (!els.length) { e.preventDefault(); panel.focus(); return; }
    const first = els[0], last = els[els.length - 1], active = document.activeElement;
    if (e.shiftKey && (active === first || !panel.contains(active))) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && (active === last || !panel.contains(active))) { e.preventDefault(); first.focus(); }
  };
  document.addEventListener("keydown", onKey);
  document.body.style.overflow = "hidden";
  setShellInert(true);
  current = { root, onClose, onKey, titleEl, bodyEl, footEl, prevFocus };
  // focus the first control of the body (or the footer), else the close button
  const target = focusable(bodyEl)[0] || focusable(footEl)[0] || closeBtn;
  try { target.focus({ preventScroll: true }); } catch (e) { /* ignore */ }
  return {
    setTitle: (v) => { titleEl.textContent = v; },
    setBody: (...c) => { clear(bodyEl); bodyEl.append(...c); },
    setFoot: (...c) => { clear(footEl); footEl.append(...c); if (!footEl.parentNode) panel.append(footEl); },
    close: closeDrawer,
  };
}

export function closeDrawer() {
  if (!current) return;
  const { root, onClose, onKey, prevFocus } = current;
  current = null;
  document.removeEventListener("keydown", onKey);
  document.body.style.overflow = "";
  setShellInert(false);
  root.classList.remove("open");
  closeTimer = setTimeout(() => { closeTimer = null; if (!current) { clear(root); root.className = ""; } }, 220);
  if (prevFocus && typeof prevFocus.focus === "function" && document.contains(prevFocus) && !root.contains(prevFocus)) {
    try { prevFocus.focus({ preventScroll: true }); } catch (e) { /* ignore */ }
  }
  if (onClose) onClose();
}
