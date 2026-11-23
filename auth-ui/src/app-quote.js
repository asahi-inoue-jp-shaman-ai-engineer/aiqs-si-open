// Quote freshness for the irradiation confirmation screen.
//
// Quotes are fetched asynchronously (prepareIrradiation), and the
// user can move the count slider while a quote is in flight. A
// response that lands afterwards describes a count the user no
// longer has selected: rendering it would show the previous
// count's total as the new count's total (the 2026-11-22 live
// check saw exactly that — the 33-shot total still on screen
// seconds after the slider reached 1). The gate gives each quote
// request a token; only the latest request's response may be
// applied, and only when the quote's own count still matches the
// count it was requested for. Keeping this in a pure module makes
// the race testable without a DOM.

export function createQuoteGate() {
  let latest = 0;
  return {
    begin() {
      latest += 1;
      return latest;
    },
    isCurrent(token) {
      return token === latest;
    },
  };
}

export function quoteMatchesCount(quote, count) {
  if (!quote || quote.count === null || quote.count === undefined) return false;
  return Number(quote.count) === Number(count);
}

// Confirmation-screen generations.
//
// The gate above orders quote requests WITHIN one visit to the
// confirmation screen. It cannot order them ACROSS visits: a
// debounce scheduled during visit N still fires after visit N+1
// has opened, and its request then advances the gate, so the new
// visit's own quote fails the token check while the old request's
// quote fails the slider check — the screen sat at "計算中…"
// forever with nothing left in flight (Astra adversarial review,
// 2026-11-22, MEDIUM 1). The session gives each visit a
// generation: entering or leaving the screen advances it, and
// timers/requests capture the generation they belong to.
export function createConfirmSession() {
  let generation = 0;
  return {
    enter() {
      generation += 1;
      return generation;
    },
    invalidate() {
      generation += 1;
    },
    isCurrent(gen) {
      return gen === generation;
    },
    current() {
      return generation;
    },
  };
}

// A debounce whose pending callback can be cancelled outright.
// Cancelling also invalidates a callback that is already queued,
// so a timer from a previous screen visit can never run its work
// in the next visit even if the clear itself raced.
export function createDebouncer({
  delayMs = 350,
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
} = {}) {
  let timer = null;
  let ticket = 0;
  function cancel() {
    ticket += 1;
    if (timer !== null) { clearTimeoutFn(timer); timer = null; }
  }
  function schedule(fn) {
    cancel();
    const mine = ticket;
    timer = setTimeoutFn(() => {
      timer = null;
      if (mine === ticket) fn();
    }, delayMs);
  }
  function pending() {
    return timer !== null;
  }
  return { schedule, cancel, pending };
}

// What may happen to a prepare() response once it lands. The
// freshness decision comes BEFORE any error is applied: an
// obsolete request's failure (or an obsolete scan_expired) must
// not replace the screen the user has since moved to — Astra
// reproduced an accepted irradiation being torn down by the
// original quote request failing late (MEDIUM 2). A login result
// is the one exception: authentication expiry is global, not
// visit-scoped, so it routes to login even when stale.
export function prepareDisposition(result, { current }) {
  if (result?.kind === 'login') return 'login';
  if (!current) return 'drop';
  return 'apply';
}

// Whether the parked confirmation still describes the screen the
// user is looking at: same visit (generation), same scan, and the
// count it was prepared for still selected. Busy restoration uses
// this before repainting totals from the parked confirmation —
// repainting a stale one resurrected the previous count's total
// after a quote_changed retry (Astra MEDIUM 3).
export function isConfirmationCurrent(parked, context) {
  if (!parked?.confirmation) return false;
  return parked.confirmationScanId === context.scanId
    && parked.confirmationGeneration === context.generation
    && Number(parked.preparedCount) === Number(context.count);
}
