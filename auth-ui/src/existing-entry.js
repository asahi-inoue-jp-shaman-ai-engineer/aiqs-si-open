// Isolated existing-account controller. The host must give this attempt exclusive
// ownership of an in-memory SDK auth instance. Server verification remains the
// authority for identity and MFA assurance; no browser AAL claim is trusted.
const failure = () => new Error('Authentication could not be completed');
const uuid = value => typeof value === 'string' &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value) &&
  value !== '00000000-0000-0000-0000-000000000000';
const opaque = value => typeof value === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(value);
const bearer = value => typeof value === 'string' && /^[A-Za-z0-9._~-]{1,16384}$/.test(value);

function factors(user) {
  const all = user.factors === undefined ? [] : user.factors;
  if (!Array.isArray(all)) throw failure();
  const seen = new Set();
  return all.map(factor => {
    if (!factor || !opaque(factor.id) || seen.has(factor.id) ||
        !['verified', 'unverified'].includes(factor.status) ||
        !['totp', 'phone', 'webauthn', 'recovery_code'].includes(factor.factor_type)) throw failure();
    seen.add(factor.id);
    return Object.freeze({ id: factor.id, status: factor.status, type: factor.factor_type });
  }).sort((a, b) => a.id.localeCompare(b.id));
}

// Email OTP login discipline (2026-11-23): one issued code accepts a
// bounded number of verification attempts, the sends per page session
// are capped, and a resend must wait out a cooldown. These are
// page-level brakes only; the provider's own code expiry and rate
// limits remain the server-side authority.
const OTP_CODE_ATTEMPTS = 5;
const OTP_MAX_SENDS = 5;
const OTP_RESEND_COOLDOWN_MS = 60000;

export function createExistingEntry({ auth, handoff, resumeRegistration = null, otpLedger = null }) {
  if (!auth || typeof handoff !== 'function') throw failure();
  if (resumeRegistration !== null && typeof resumeRegistration !== 'function') throw failure();
  if (otpLedger !== null && (typeof otpLedger !== 'object' ||
      !Number.isFinite(otpLedger.sends) || !Number.isFinite(otpLedger.lastSentAt))) throw failure();
  // The send ledger is page-level accounting: the host may share one
  // ledger across controller instances (mode switches rebuild the
  // controller), so clear() must never reset it.
  const ledger = otpLedger ?? { sends: 0, lastSentAt: 0 };
  let state = 'ready', busy = false, mode = null, email = null;
  let subject = null, token = null, roster = null, selected = null, challenge = null;
  let otpAttempts = 0;
  const terminal = ['disposed', 'finished', 'restart-required', 'unsupported-factor', 'password-update-unknown'];
  function active() { if (terminal.includes(state)) throw failure(); }
  function clear() {
    auth = null; handoff = null; resumeRegistration = null; email = null; token = null; subject = null;
    roster = null; selected = null; challenge = null;
    otpAttempts = 0;
  }
  function stop(next) { state = next; clear(); }
  async function run(allowed, action) {
    if (busy || !allowed.includes(state)) throw failure();
    busy = true;
    try { return await action(); } catch { throw failure(); } finally { busy = false; }
  }
  async function sessionMatches() {
    const result = await auth.getSession();
    active();
    if (result?.error || result?.data?.session?.access_token !== token ||
        result.data.session.user?.id !== subject) throw failure();
  }
  async function verifiedUser() {
    const result = await auth.getUser(token);
    active();
    if (result?.error || result?.data?.user?.id !== subject) throw failure();
    const current = factors(result.data.user);
    if (roster !== null && JSON.stringify(current) !== JSON.stringify(roster)) throw failure();
    return current;
  }
  async function boundary() { await sessionMatches(); active(); return verifiedUser(); }
  function pin(result) {
    if (result?.error || !uuid(result?.data?.user?.id) ||
        result.data.session?.user?.id !== result.data.user.id ||
        !bearer(result.data.session?.access_token)) throw failure();
    subject = result.data.user.id;
    token = result.data.session.access_token;
  }
  async function finish() {
    await boundary();
    active();
    // Authenticated resumption (A2, batch 2b item 11): when the
    // host holds a durable pending SI registration for this exact
    // user, it completes the registration BEFORE the handoff. A
    // failure stops the handoff — the user is never presented as
    // finished while their identity cannot be provisioned — and
    // the durable record survives for the next login.
    if (resumeRegistration) {
      await resumeRegistration({ subject, token });
      active();
    }
    const send = handoff, credential = token;
    stop('finished'); // An ambiguous handoff cannot be repeated.
    await send(credential);
    return { kind: 'handed-off' };
  }
  async function ready() {
    const verifiedRoster = await boundary();
    active();
    roster = verifiedRoster;
    const verified = roster.filter(factor => factor.status === 'verified');
    if (verified.length) {
      const totp = verified.filter(factor => factor.type === 'totp');
      if (!totp.length) { stop('unsupported-factor'); return { kind: 'unsupported-factor' }; }
      state = 'factor-choice';
      return { kind: 'choose-totp', factorIds: totp.map(factor => factor.id) };
    }
    if (mode === 'recovery') { state = 'password-ready'; return { kind: 'password-ready' }; }
    return finish();
  }
  // Centralized OTP dispatch accounting (Astra round-1 finding 3):
  // the send is reserved and the cooldown starts BEFORE dispatch,
  // so a request the provider accepted but whose response was lost
  // still spends the send — the page-level budget bounds actual
  // sends, not only acknowledged ones. The same gate serves the
  // initial request and resends, so navigating back and forth
  // cannot bypass the cooldown either.
  async function dispatchOtp() {
    if (ledger.sends >= OTP_MAX_SENDS) { stop('restart-required'); throw failure(); }
    if (ledger.lastSentAt && Date.now() - ledger.lastSentAt < OTP_RESEND_COOLDOWN_MS) throw failure();
    ledger.sends += 1; ledger.lastSentAt = Date.now();
    let result;
    try { result = await auth.signInWithOtp({ email, options: { shouldCreateUser: false } }); }
    catch { result = null; }
    active();
    if (!result || result.error || !result.data) throw failure();
    // Attempts reset only on acknowledged issuance: a rejected or
    // ambiguous resend must not replenish guesses against the code
    // that is still current (Astra round-2 finding N1).
    otpAttempts = 0;
  }
  return {
    get state() { return state; },
    // Snapshot for the view's OTP guidance (attempts left on the
    // current code, sends left in this page session, resend wait).
    get otp() {
      return Object.freeze({
        attemptsLeft: OTP_CODE_ATTEMPTS - otpAttempts,
        sendsLeft: OTP_MAX_SENDS - ledger.sends,
        resendWaitMs: Math.max(0, ledger.lastSentAt + OTP_RESEND_COOLDOWN_MS - Date.now()),
      });
    },
    passwordLogin({ email: loginEmail, password }) {
      return run(['ready'], async () => {
        mode = 'login';
        try {
          const result = await auth.signInWithPassword({ email: loginEmail, password });
          active(); pin(result);
          return await ready();
        } catch { if (!terminal.includes(state)) stop('restart-required'); throw failure(); }
        finally { password = null; loginEmail = null; }
      });
    },
    // Email OTP login (2026-11-23): the primary sign-in for existing
    // accounts. shouldCreateUser stays false, so an address without
    // an account never mints a provider user here; registration is
    // the separate signup flow with its SI marker and finalization.
    // A failed request creates no session and pins no identity, so
    // it returns to 'ready' and the address can be corrected in
    // place; a failed verification likewise stays on the code step
    // until the per-code attempt budget is spent.
    requestOtpLogin(loginEmail) {
      return run(['ready'], async () => {
        if (typeof loginEmail !== 'string' || !loginEmail.trim()) throw failure();
        mode = 'login'; email = loginEmail;
        state = 'otp-sending';
        // A failed request creates no session and pins no identity,
        // so it returns to 'ready' and the address can be corrected
        // in place (the send itself stays spent — see dispatchOtp).
        try { await dispatchOtp(); }
        catch { if (!terminal.includes(state)) state = 'ready'; throw failure(); }
        state = 'otp-code';
        return { kind: 'otp-code-required' };
      });
    },
    verifyOtpLogin(code) {
      return run(['otp-code'], async () => {
        try {
          if (typeof code !== 'string' || !/^[0-9]{6}$/.test(code)) throw failure();
          let result;
          try { result = await auth.verifyOtp({ email, token: code, type: 'email' }); }
          catch { result = null; }
          active();
          if (!result || result.error || !result.data) {
            // A rejected or unreachable verification spends one
            // attempt on this code; a spent code returns the flow
            // to 'ready', where a fresh code can be requested.
            otpAttempts += 1;
            if (otpAttempts >= OTP_CODE_ATTEMPTS) { otpAttempts = 0; state = 'ready'; }
            throw failure();
          }
          // The code is consumed once verification succeeds; a
          // failure in pin or the post-authentication boundaries
          // cannot be retried with it, so it terminates exactly
          // like the password path (Astra round-1 finding 2).
          try {
            pin(result);
            email = null; otpAttempts = 0;
            return await ready();
          } catch { if (!terminal.includes(state)) stop('restart-required'); throw failure(); }
        } finally { code = null; }
      });
    },
    resendOtpLogin() {
      return run(['otp-code'], async () => {
        // A failed or ambiguous resend keeps the current code step
        // and its remaining attempt budget: the previous (or
        // just-dispatched) code may still arrive and be verified.
        await dispatchOtp();
        return { kind: 'otp-code-required' };
      });
    },
    backToEmail() {
      return run(['otp-code'], async () => {
        // The send budget deliberately survives the trip back, so
        // alternating request/back cannot mint unlimited sends.
        email = null; otpAttempts = 0;
        state = 'ready';
        return { kind: 'email-required' };
      });
    },
    requestRecovery(originalEmail) {
      return run(['ready'], async () => {
        if (typeof originalEmail !== 'string' || !originalEmail.trim()) throw failure();
        email = originalEmail;
        mode = 'recovery';
        state = 'recovery-sending';
        try {
          const result = await auth.resetPasswordForEmail(email);
          active();
          if (result?.error || !result?.data) throw failure();
          state = 'recovery-code';
          return { kind: 'recovery-code-required' };
        } catch { if (!terminal.includes(state)) stop('restart-required'); throw failure(); }
      });
    },
    verifyRecovery(code) {
      return run(['recovery-code'], async () => {
        if (typeof code !== 'string' || !/^[0-9]{6,10}$/.test(code)) throw failure();
        try {
          const result = await auth.verifyOtp({ email, token: code, type: 'recovery' });
          active(); pin(result);
          email = null;
          return await ready();
        } catch { if (!terminal.includes(state)) stop('restart-required'); throw failure(); }
        finally { code = null; }
      });
    },
    chooseAndChallengeTOTP(factorId) {
      return run(['factor-choice'], async () => {
        challenge = null;
        if (!roster.some(factor => factor.id === factorId && factor.status === 'verified' && factor.type === 'totp') ||
            (selected !== null && selected !== factorId)) throw failure();
        selected = factorId;
        try {
          await boundary();
          active();
          const result = await auth.mfa.challenge({ factorId: selected });
          active();
          if (result?.error || !opaque(result?.data?.id) || result.data.type !== 'totp' ||
              !Number.isFinite(result.data.expires_at) || result.data.expires_at <= Date.now() / 1000) throw failure();
          await boundary();
          active();
          challenge = Object.freeze({ id: result.data.id, expiresAt: result.data.expires_at });
          state = 'totp-code';
          return { kind: 'totp-code-required' };
        } catch { challenge = null; throw failure(); }
      });
    },
    verifyTOTP(code) {
      return run(['totp-code'], async () => {
        const current = challenge;
        challenge = null; state = 'factor-choice';
        try {
          if (typeof code !== 'string' || !/^[0-9]{6}$/.test(code) || !current || current.expiresAt <= Date.now() / 1000) throw failure();
          await boundary();
          active();
          const result = await auth.mfa.verify({ factorId: selected, challengeId: current.id, code });
          active();
          if (result?.error || result?.data?.user?.id !== subject ||
              !bearer(result?.data?.access_token) || result.data.access_token === token) throw failure();
          token = result.data.access_token;
          await boundary();
          active();
          if (mode === 'recovery') { state = 'password-ready'; return { kind: 'password-ready' }; }
          return await finish();
        } finally { code = null; }
      });
    },
    changePassword(password) {
      return run(['password-ready'], async () => {
        if (typeof password !== 'string' || !password) throw failure();
        let updating = false;
        try {
          await boundary();
          active();
          updating = true;
          const result = await auth.updateUser({ password });
          active();
          if (result?.error || result?.data?.user?.id !== subject) throw failure();
          await boundary();
          active();
          return await finish();
        } catch {
          if (!terminal.includes(state)) stop(updating ? 'password-update-unknown' : 'restart-required');
          throw failure();
        } finally { password = null; }
      });
    },
    dispose() { stop('disposed'); },
  };
}
