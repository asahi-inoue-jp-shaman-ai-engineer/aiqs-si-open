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

export function createExistingEntry({ auth, handoff }) {
  if (!auth || typeof handoff !== 'function') throw failure();
  let state = 'ready', busy = false, mode = null, email = null;
  let subject = null, token = null, roster = null, selected = null, challenge = null;
  const terminal = ['disposed', 'finished', 'restart-required', 'unsupported-factor', 'password-update-unknown'];
  function active() { if (terminal.includes(state)) throw failure(); }
  function clear() {
    auth = null; handoff = null; email = null; token = null; subject = null;
    roster = null; selected = null; challenge = null;
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
  return {
    get state() { return state; },
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
