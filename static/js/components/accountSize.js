/* Account size, shown coarse: the broker's balance rounded to the usual prop-firm
   tiers (50K, 100K …) as a small pill next to the account, and a one-tap
   suggestion that sizes a follower to the same risk share as its leader.
   Nothing here ever prints the exact balance while privacy mode is on. */
import { h, fmtNum } from "../ui.js";
import { isPrivate } from "../privacy.js";
import { t } from "../i18n.js";

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
  const sug = suggestion(sizeOf(account && account.tier), leaderSize);
  if (!sug) return null;
  const chip = h("button", { type: "button", class: "chip chip-suggest" });
  const paint = () => {
    const els = inputs();
    const fixed = els && els.mode && els.mode.value === "fixed";
    chip.textContent = fixed ? t("≈ {n} contract(s)", { n: sug.fixed }) : `≈ ×${sug.multiplier}`;
    chip.title = t("Same risk share as the leader ({pct}% of its size) — tap to apply", { pct: Math.round(sug.ratio * 100) });
  };
  chip.addEventListener("click", () => {
    const els = inputs();
    if (!els) return;
    if (els.mode && els.mode.value === "fixed") { if (els.fixed) els.fixed.value = String(sug.fixed); }
    else if (els.mult) els.mult.value = String(sug.multiplier);
    chip.classList.add("active");
    setTimeout(() => chip.classList.remove("active"), 600);
  });
  // the chip follows the mode select once the row is in the DOM
  queueMicrotask(() => { const els = inputs(); if (els && els.mode) els.mode.addEventListener("change", paint); paint(); });
  paint();
  return chip;
}

/** "Leader: 150K" reference line for a follower table; null without a size. */
export function leaderSizeLine(tier) {
  if (!tier || !tier.tier) return null;
  return h("div", { class: "muted", style: "font-size:12px;margin:0 0 6px" }, t("Leader account size: "), h("span", { class: `tag tier${tier.exact ? "" : " approx"}` }, tier.tier),
    " ", h("span", null, t("— the suggestion under each account keeps the same risk share")));
}
