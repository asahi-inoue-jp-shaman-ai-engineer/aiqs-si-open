// Operation-level in-flight guards for submissions.
//
// A busy button alone cannot prevent a duplicate submission: the
// user can navigate back and re-enter a screen while a POST is
// still pending, and the re-rendered button is enabled again. The
// submission handlers therefore hold one of these guards for the
// whole operation. tryStart must be called synchronously at the
// top of the handler — before any await, including file reads —
// so a second tap or re-entry is rejected outright, and end must
// run from a finally block so the operation can be retried after
// any outcome.
export function createInFlight() {
  const active = new Set();
  return {
    tryStart(key) {
      if (active.has(key)) return false;
      active.add(key);
      return true;
    },
    end(key) {
      active.delete(key);
    },
    isActive(key) {
      return active.has(key);
    },
  };
}
