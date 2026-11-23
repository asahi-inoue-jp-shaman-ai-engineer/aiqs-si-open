import { createIndependentEntry, createRegistrationResumer } from './independent-entry.js';
import { createExistingEntry } from './existing-entry.js';

// Each selected mode owns one fresh memory-only client. No account operations
// are implemented here; the reviewed controllers retain their own boundaries.
export function createUnifiedEntry({ createClient, origin, fetch, handoff, pendingStore = null }) {
  if (typeof createClient !== 'function' || typeof handoff !== 'function') throw new Error('Entry unavailable');
  let mode = 'login', entry = null, client = null, started = false, busy = false, disposed = false;
  let generation = 0, handedOff = false;
  const fail = () => new Error('Authentication could not be completed');
  // One durable pending-registration store serves both modes (A2,
  // batch 2b item 11): signup persists and restores through it, and
  // login completes a matching pending registration before handoff
  // via the shared resumer. Without a store, behavior is unchanged.
  const resumeRegistration = pendingStore
    ? createRegistrationResumer({ origin, fetch, pendingStore })
    : null;
  // Page-level OTP send accounting, shared across mode switches:
  // abandoning a login attempt rebuilds the controller with a fresh
  // client, but the send budget and cooldown survive (Astra round-1
  // findings 3 and 4).
  const otpLedger = { sends: 0, lastSentAt: 0 };
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
      ? createIndependentEntry({ auth: client.auth, origin, fetch, handoff: send, pendingStore })
      : createExistingEntry({ auth: client.auth, handoff: send, resumeRegistration, otpLedger });
  }
  // A mode may be abandoned only from states that hold no session
  // and have pinned no identity: a failed or still-unverified login
  // or recovery attempt. Anything past verification (factor choice,
  // TOTP, password-ready, handed off) locks switching as before.
  function abandonable() {
    if (!entry) return false;
    return mode === 'login' ? ['ready', 'otp-code'].includes(entry.state)
      : mode === 'recovery' ? ['ready', 'recovery-code'].includes(entry.state)
        : entry.state === 'ready';
  }
  build();
  return Object.freeze({
    get mode() { return mode; },
    get state() { return disposed ? 'disposed' : entry.state; },
    get otp() { return disposed ? null : entry.otp ?? null; },
    get busy() { return busy; },
    get canSwitch() { return !disposed && !busy && !handedOff && (!started || abandonable()); },
    selectMode(next) {
      if (!['login', 'signup', 'recovery'].includes(next) || !this.canSwitch) throw fail();
      if (next === mode) return;
      entry.dispose(); entry = null; client = null;
      mode = next;
      try { build(); } catch { disposed = true; client = null; throw fail(); }
      // The rebuilt controller has run nothing: `started` is
      // per-controller, so a step the new controller restores on
      // construction (a pending signup verification) is not trapped
      // by the previous mode's history (Astra round-2 finding N2).
      // The shared OTP ledger is the deliberate exception: it
      // survives every rebuild.
      started = false;
    },
    async invoke(method, value) {
      // Login is email-OTP only (2026-11-23): password sign-in is no
      // longer invocable from the unified entry. The controller keeps
      // the method for the isolated existing-account template.
      const allowed = mode === 'signup' ? ['signup', 'verify', 'complete']
        : mode === 'login' ? ['requestOtpLogin', 'verifyOtpLogin', 'resendOtpLogin', 'backToEmail', 'chooseAndChallengeTOTP', 'verifyTOTP']
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
