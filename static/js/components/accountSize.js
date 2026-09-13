/* Account size, shown coarse: the broker's balance rounded to the usual prop-firm
   tiers (50K, 100K …) as a small pill next to the account, and a one-tap
   suggestion that sizes a follower to the same risk share as its leader.
   Nothing here ever prints the exact balance while privacy mode is on. */
import { h, fmtNum } from "../ui.js";
import { isPrivate } from "../privacy.js";
import { t } from "../i18n.js";

const TIERS_K = [10, 25, 50, 75, 100, 150, 200, 250, 300, 500, 1000];

/** The same tier the backend derives (sizing.size_tier), for figures that arrive as a balance. */
export function tierOf(balance) {
  const bal = Number(balance);
  if (!(bal > 0)) return null;
  const k = bal / 1000;
  const best = TIERS_K.reduce((a, b) => (Math.abs(b - k) < Math.abs(a - k) ? b : a));
  if (Math.abs(best - k) <= best * 0.08) return { tier: `${best}K`, size: best * 1000, exact: true };
  const r = Math.max(1, Math.round(k));
  return { tier: `≈${r}K`, size: r * 1000, exact: false };
}

/** "+12.0%" of a size, or "" without one. */
export function shareOf(amount, size) {
  const a = Number(amount), s = Number(size);
  if (!(s > 0) || !Number.isFinite(a)) return "";
  const p = (a / s) * 100;
  return `${p > 0 ? "+" : ""}${p.toFixed(Math.abs(p) < 10 ? 1 : 0)}%`;
}

/** Chips "1% · 500", "2% · 1,000", "3% · 1,500" of an account size; a tap hands the amount to onPick. */
export function percentChips(size, onPick, percents = [1, 2, 3]) {
  const s = Number(size);
  if (!(s > 0)) return null;
  return h("div", { class: "chips", style: "margin-top:6px" }, percents.map((pc) => {
    const amount = Math.round((s * pc) / 100);
    return h("button", { type: "button", class: "chip chip-suggest", style: "margin-top:0", title: t("{pc}% of the account size — tap to apply", { pc }),
      onClick: () => onPick(amount) }, `${pc}% · ${fmtNum(amount, 0)}`);
  }));
}

/** Numeric size of a size tier ({tier, size} from the API), or null. */
export function sizeOf(tier) {
  return tier && Number(tier.size) > 0 ? Number(tier.size) : null;
}

/** The pill: "50K" — hover shows the balance unless privacy mode hides it. */
export function sizePill(account) {
  const tier = account && account.tier;
  if (!tier || !tier.tier) return null;
  const title = !isPrivate() && Number(account.balance) > 0
    ? t("Balance {v} — account size from the broker's balance, rounded to the usual prop-firm sizes", { v: fmtNum(account.balance, 0) })
    : t("Account size from the broker's balance, rounded to the usual prop-firm sizes");
  return h("span", { class: `tag tier${tier.exact ? "" : " approx"}`, title }, tier.tier);
}

/** The same risk share as the leader: ratio of the two sizes, in quarter steps. */
export function suggestion(followerSize, leaderSize) {
  const f = Number(followerSize), l = Number(leaderSize);
  if (!(f > 0) || !(l > 0)) return null;
  const ratio = f / l;
  return { ratio, multiplier: Math.max(0.25, Math.round(ratio * 4) / 4), fixed: Math.max(1, Math.round(ratio)) };
}

/**
 * A chip under the account name: "≈ ×0.5" (multiplier) or "≈ 2 contracts" (fixed),
 * following the row's mode select; a tap writes the value into the row's input.
 * `inputs()` returns {mode, mult, fixed} elements of the row (looked up lazily —
 * the table renders cells before the row exists).
 */
export function suggestionChip(account, leaderSize, inputs) {
  // the reference may be a value or a function (a form field the user edits): the
  // chip re-paints itself (chip.repaint) instead of the table re-rendering rows
  const refFn = typeof leaderSize === "function" ? leaderSize : () => leaderSize;
  const size = sizeOf(account && account.tier);
  if (!size) return null;
  const chip = h("button", { type: "button", class: "chip chip-suggest" });
  let sug = suggestion(size, refFn());
  const paint = () => {
    sug = suggestion(size, refFn());
    chip.hidden = !sug;
    if (!sug) return;
    const els = inputs();
    const fixed = els && els.mode && els.mode.value === "fixed";
    chip.textContent = fixed ? t("≈ {n} contract(s)", { n: sug.fixed }) : `≈ ×${sug.multiplier}`;
    chip.title = t("Same risk share as the leader ({pct}% of its size) — tap to apply", { pct: Math.round(sug.ratio * 100) });
  };
  chip.repaint = paint;
  chip.addEventListener("click", () => {
    const els = inputs();
    if (!els || !sug) return;
    if (els.mode && els.mode.value === "fixed") { if (els.fixed) els.fixed.value = String(sug.fixed); }
    else {
      if (els.mode && els.mode.value === "same") { els.mode.value = "multiplier"; els.mode.dispatchEvent(new Event("change")); }   // "Same 1:1" cannot carry a factor
      if (els.mult) els.mult.value = String(sug.multiplier);
    }
    chip.classList.add("active");
    setTimeout(() => chip.classList.remove("active"), 600);
  });
  // the chip follows the mode select once the row is in the DOM
  queueMicrotask(() => { const els = inputs(); if (els && els.mode) els.mode.addEventListener("change", paint); paint(); });
  paint();
  return chip;
}

/** "Leader: 150K" reference line for a follower table; null without a size. */
export function leaderSizeLine(tier, label = null) {
  if (!tier || !tier.tier) return null;
  return h("div", { class: "muted", style: "font-size:12px;margin:0 0 6px" }, label || t("Leader account size: "), h("span", { class: `tag tier${tier.exact ? "" : " approx"}` }, tier.tier),
    " ", h("span", null, t("— the suggestion under each account keeps the same risk share")));
}

/** A "sized for" reference (K) as a tier object for leaderSizeLine / suggestions. */
export function sizedForTier(k) {
  const n = Number(k);
  return n > 0 ? { tier: `${n}K`, size: n * 1000, exact: true } : null;
}
