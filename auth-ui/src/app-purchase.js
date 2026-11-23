// Purchase-guide helpers for the standalone app (UI redesign
// increment 4, 2026-11-23, design: files/ui-mock-oneq/
// DESIGN-NOTES.md screen 24).
//
// The purchase itself stays on the existing purchase flow: a
// pack tap follows the server-issued payment link and leaves
// the app. So that the user can come back to the irradiation
// confirmation afterwards and start right away (owner-confirmed
// item 7: buy in place, return to the confirmation), the app
// stores ONE return intent in session storage before the tap
// navigates away, and consumes it on the next boot. The intent
// carries only what the confirmation needs to rebuild itself —
// the scan id and, for irradiation, the chosen count (null when
// the purchase detoured from the reading confirmation, where no
// count exists yet). It never carries amounts: the restored
// confirmation re-prepares its quote from the server, so a
// balance that has not caught up yet simply shows the shortage
// branch again instead of a stale total.
//
// Everything here is pure / storage-shaped so the rules are
// testable without a DOM. Storage failures (private mode,
// quota) degrade to "no return intent" — the purchase itself
// is never blocked by bookkeeping.

export const PURCHASE_RETURN_KEY = 'aiqs-si.purchase-return';

function validScanId(value) {
  return typeof value === 'string' && value.length > 0;
}

function validCount(value) {
  return value === null
    || (Number.isInteger(value) && value >= 1 && value <= 99);
}

// Builds the intent to store, or null when the context cannot
// support a return (no scan to come back to).
export function purchaseReturnIntent(context) {
  if (!context || !validScanId(context.scanId)) return null;
  const count = context.count === undefined ? null : context.count;
  if (!validCount(count)) return null;
  return { scanId: context.scanId, count };
}

export function savePurchaseReturn(storage, context) {
  const intent = purchaseReturnIntent(context);
  if (!intent || !storage) return false;
  try {
    storage.setItem(PURCHASE_RETURN_KEY, JSON.stringify(intent));
    return true;
  } catch {
    return false;
  }
}

// Reads AND removes the stored intent: a return is consumed
// exactly once, so a later plain visit never gets hijacked by
// a stale purchase detour. Anything malformed reads as null.
export function takePurchaseReturn(storage) {
  if (!storage) return null;
  let raw = null;
  try {
    raw = storage.getItem(PURCHASE_RETURN_KEY);
    storage.removeItem(PURCHASE_RETURN_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  let parsed = null;
  try { parsed = JSON.parse(raw); } catch { return null; }
  return purchaseReturnIntent(parsed);
}

// Drops a stored intent without reading it (a pack tap from a
// context with nothing to return to must not leave an older
// detour behind).
export function clearPurchaseReturn(storage) {
  if (!storage) return;
  try { storage.removeItem(PURCHASE_RETURN_KEY); } catch { /* ignore */ }
}

// The shortage a quote leaves behind, in credits: how much
// more the user needs. Only a negative numeric balance-after
// is a shortage; anything else (unknown balance included) is
// null — the UI must not invent a number it was not given.
export function shortageCredits(balanceAfter) {
  return typeof balanceAfter === 'number' && balanceAfter < 0
    ? -balanceAfter : null;
}

// One pack row's label, in the purchase page's own format
// ("5,000円 → 5,555クレジット"). Values come from the server's
// purchase payload; a pack without usable numbers renders no
// label rather than a made-up one.
export function packLabel(pack) {
  if (!pack || typeof pack.yen !== 'number' || typeof pack.credits !== 'number') {
    return '';
  }
  return `${pack.yen.toLocaleString('ja-JP')}円 → ${pack.credits.toLocaleString('ja-JP')}クレジット`;
}
