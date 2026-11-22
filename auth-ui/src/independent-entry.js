// Isolated candidate. Configuration and handoff must come from the trusted host,
// never query parameters. This module does not grant credits or verify identity.
const failure = () => new Error('Authentication could not be completed');
const uuid = (value) => typeof value === 'string' &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value) &&
  value !== '00000000-0000-0000-0000-000000000000';

function httpsOrigin(value) {
  const url = new URL(value);
  if (url.protocol !== 'https:' || url.username || url.password ||
      url.href !== url.origin + '/') throw failure();
  return url.origin;
}

export function createMemoryClient(createClient, { providerBase, publishableKey }) {
  const origin = httpsOrigin(providerBase);
  if (typeof createClient !== 'function' || typeof publishableKey !== 'string' ||
      !/^sb_publishable_[A-Za-z0-9_-]{1,512}$/.test(publishableKey)) throw failure();
  return createClient(origin, publishableKey, { auth: {
    persistSession: false, autoRefreshToken: false, detectSessionInUrl: false,
  } });
}

export function createIndependentEntry({ auth, origin, fetch: request, handoff }) {
  const completionUrl = httpsOrigin(origin) + '/onboarding/complete';
  if (!auth || typeof request !== 'function' || typeof handoff !== 'function') throw failure();
  let state = 'ready';
  let busy = false;
  let pending = null;
  let accessToken = null;

  function active() { if (state === 'disposed' || state === 'finished') throw failure(); }
  function clear() { pending = null; accessToken = null; auth = null; request = null; handoff = null; }
  async function run(allowed, action) {
    if (busy || !allowed.includes(state)) throw failure();
    busy = true;
    try { return await action(); } catch { throw failure(); } finally { busy = false; }
  }
  function session(result, subject) {
    if (result?.error || !uuid(result?.data?.user?.id) ||
        (subject && result.data.user.id !== subject) ||
        result.data.session?.user?.id !== result.data.user.id ||
        typeof result.data.session?.access_token !== 'string' ||
        !/^[A-Za-z0-9._~-]{1,16384}$/.test(result.data.session.access_token)) throw failure();
    return result.data.session.access_token;
  }
  async function sameUser(subject) {
    const result = await auth.getUser();
    active();
    if (result?.error || result?.data?.user?.id !== subject) throw failure();
  }
  async function finish(subject) {
    await sameUser(subject);
    const callback = handoff;
    const token = accessToken;
    state = 'finished'; // Even an ambiguous callback failure must not hand off twice.
    clear();
    await callback(token);
    return { kind: 'handed-off' };
  }
  return {
    get state() { return state; },
    login({ email, password }) {
      return run(['ready'], async () => {
        const result = await auth.signInWithPassword({ email, password });
        active();
        accessToken = session(result);
        try { return await finish(result.data.user.id); }
        finally { accessToken = null; }
      });
    },
    signup({ email, password, referralCode = null }) {
      return run(['ready'], async () => {
        if (typeof email !== 'string' || !email.trim() || typeof password !== 'string' || !password ||
            (referralCode !== null && (typeof referralCode !== 'string' || !referralCode || referralCode.length > 128))) throw failure();
        // Freeze the original choices, including absence. Never reread editable UI.
        pending = Object.freeze({ email, referralCode, subject: null });
        state = 'signup-pending';
        const result = await auth.signUp({ email, password, options: { data: {
          si_registration: 'v1', referral_code: referralCode,
        } } });
        active();
        if (result?.error || !uuid(result?.data?.user?.id)) throw failure();
        pending = Object.freeze({ ...pending, subject: result.data.user.id });
        state = 'verification';
        return { kind: 'verification-required' };
      });
    },
    verify(code) {
      return run(['verification'], async () => {
        if (typeof code !== 'string' || !/^[0-9]{6,10}$/.test(code)) throw failure();
        const result = await auth.verifyOtp({ email: pending.email, token: code, type: 'signup' });
        active();
        accessToken = session(result, pending.subject);
        state = 'completion';
        return { kind: 'completion-required' };
      });
    },
    complete() {
      return run(['completion'], async () => {
        await sameUser(pending.subject);
        const response = await request(completionUrl, {
          method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + accessToken },
          body: JSON.stringify({ subject: pending.subject }),
          redirect: 'error', credentials: 'omit', cache: 'no-store',
        });
        active();
        if (!response.ok) throw failure();
        const body = await response.json();
        active();
        if (!body || Object.keys(body).length !== 1 || body.registration_complete !== true) throw failure();
        return finish(pending.subject);
      });
    },
    dispose() { state = 'disposed'; clear(); },
  };
}
