// Job polling with a generation guard.
//
// Clearing a timeout does not invalidate a poll whose request is
// already in flight: when the response landed after the user had
// navigated away, the old callback still ran — it rescheduled
// another poll from the new screen, navigated back to a finished
// job's result, and, with a new polling session started in the
// meantime, left TWO timers alive of which later navigation
// cleared only one (Astra adversarial review, 2026-11-22,
// MEDIUM 5; the defect predates that batch). The poller gives
// each session a generation: stop() and start() advance it, and
// the generation is checked after every await and again after
// the handler runs (the handler itself may navigate, which bumps
// the generation) before anything is rescheduled.
//
// The handler receives the raw fetch result and returns
// 'continue' to poll again or 'stop' to end the session. Keeping
// the loop here — with injectable timers and fetch — makes the
// lifecycle testable without a DOM.
export function createPoller({
  fetchJob,
  initialDelayMs = 800,
  intervalMs = 3000,
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
} = {}) {
  let generation = 0;
  let timer = null;

  function clearTimer() {
    if (timer !== null) { clearTimeoutFn(timer); timer = null; }
  }

  function stop() {
    generation += 1;
    clearTimer();
  }

  function start(jobId, handler) {
    stop();
    const gen = generation;
    const tick = async () => {
      timer = null;
      let result;
      try {
        result = await fetchJob(jobId);
      } catch (error) {
        result = { kind: 'error', error };
      }
      // Superseded while the request was in flight: the response
      // belongs to a screen the user has left. Drop it silently.
      if (gen !== generation) return;
      const action = handler(result);
      // The handler may itself have navigated (stop/start): do
      // not reschedule on its behalf.
      if (gen !== generation) return;
      if (action === 'continue') timer = setTimeoutFn(tick, intervalMs);
    };
    timer = setTimeoutFn(tick, initialDelayMs);
    return gen;
  }

  function isCurrent(gen) {
    return gen === generation;
  }

  return { start, stop, isCurrent };
}
