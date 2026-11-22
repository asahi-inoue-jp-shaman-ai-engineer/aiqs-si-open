import { createIndependentEntry } from './independent-entry.js';
import { createExistingEntry } from './existing-entry.js';

// Each selected mode owns one fresh memory-only client. No account operations
// are implemented here; the reviewed controllers retain their own boundaries.
export function createUnifiedEntry({ createClient, origin, fetch, handoff }) {
  if (typeof createClient !== 'function' || typeof handoff !== 'function') throw new Error('Entry unavailable');
  let mode = 'login', entry = null, client = null, started = false, busy = false, disposed = false;
  let generation = 0, handedOff = false;
  const fail = () => new Error('Authentication could not be completed');
  function build() {
    const current = ++generation;
    client = createClient();
    const send = async token => {
      if (disposed || current !== generation || handedOff) throw fail();
      handedOff = true;
      client = null;
      await handoff(token);
    };
    entry = mode === 'signup'
      ? createIndependentEntry({ auth: client.auth, origin, fetch, handoff: send })
      : createExistingEntry({ auth: client.auth, handoff: send });
  }
  build();
  return Object.freeze({
    get mode() { return mode; },
    get state() { return disposed ? 'disposed' : entry.state; },
    get busy() { return busy; },
    get canSwitch() { return !disposed && !started && !busy && !handedOff; },
    selectMode(next) {
      if (!['login', 'signup', 'recovery'].includes(next) || !this.canSwitch) throw fail();
      if (next === mode) return;
      entry.dispose(); entry = null; client = null;
      mode = next;
      try { build(); } catch { disposed = true; client = null; throw fail(); }
    },
    async invoke(method, value) {
      const allowed = mode === 'signup' ? ['signup', 'verify', 'complete']
        : mode === 'login' ? ['passwordLogin', 'chooseAndChallengeTOTP', 'verifyTOTP']
          : ['requestRecovery', 'verifyRecovery', 'chooseAndChallengeTOTP', 'verifyTOTP', 'changePassword'];
      if (disposed || busy || handedOff || !allowed.includes(method)) throw fail();
      started = true; busy = true;
      try {
        const result = await entry[method](value);
        if (disposed) throw fail();
        return result;
      } catch { throw fail(); }
      finally { busy = false; }
    },
    dispose() {
      disposed = true; generation++;
      entry?.dispose(); entry = null; client = null;
      createClient = null; fetch = null; handoff = null;
    },
  });
}
