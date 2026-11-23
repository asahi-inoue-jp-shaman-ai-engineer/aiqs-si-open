// Policy price math for the irradiation count screen.
//
// The count screen shows its total immediately, while the user
// is still choosing: there is no server round-trip behind the
// slider, so the number is computed here — as an exact mirror of
// the server's one session-charge computation
// (src/aiqs_si/pricing.py session_charge_credits: round half-up
// of count × rate, in integer tenths of a credit, so at the
// standard 39 tenths = 3.9 credits/shot: 1 → 4, 9 → 35,
// 33 → 129, 66 → 257, 99 → 386).
//
// This is an ESTIMATE display only (device-consistency rule 5:
// estimates and settled values must look different). The
// authoritative amount is the prepare quote on the next screen;
// the count screen says so in its own copy, and nothing here is
// ever submitted or settled. The rate comes from the session
// payload's credits_per_shot_tenths — never a client-side
// constant — so an operator rate change reaches this display
// with the session, and an unusable rate yields null (the screen
// shows "—") instead of a computed fiction.
export function sessionChargeCredits(count, rateTenths) {
  if (!Number.isInteger(count) || count < 0) return null;
  if (!Number.isInteger(rateTenths) || rateTenths < 1) return null;
  return Math.floor((count * rateTenths + 5) / 10);
}
