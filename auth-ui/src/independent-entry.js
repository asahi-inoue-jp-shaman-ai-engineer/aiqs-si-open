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

// Durable pending-registration record (A2, batch 2b item 11). The
// record holds ONLY the signup email and the upstream user id —
// never a password, never a token — and exists so an interrupted
// signup can resume: email confirmation and /onboarding/complete
// are separate steps, and keeping the pending subject in page
// memory alone stranded confirmed users with no SI identity when
// the completion call never ran. Storage failures are soft: the
// in-memory flow continues exactly as before.
// Records live under IMMUTABLE per-record keys (Astra round-2
// F12/N4, round-3 F12): 'si-pending-registration-v1:<subject>:<id>'
// where <id> is unique to one save. Deletion names the exact key
// a flow loaded or saved — there is no read-compare-remove window
// and no shared key through which one tab (or one account) can
// ever delete another's record. A newer save for the same subject
// supersedes that subject's older keys; other subjects' keys are
// never touched. load() returns the newest record across
// subjects (construction-time restoration only); subject-scoped
// flows use loadFor(subject).
//
// The pre-round-3 layout stored ONE record under the bare key
// 'si-pending-registration-v1' (Astra round-3 N7): a valid legacy
// record is migrated on first sight — copied under an immutable
// key for its subject (unless that subject already has a record,
// which is newer by construction) and the legacy key removed —
// so an interrupted signup from before the upgrade still
// completes instead of being silently abandoned.
const PENDING_PREFIX = 'si-pending-registration-v1:';
const LEGACY_PENDING_KEY = 'si-pending-registration-v1';
const COMPLETED_PREFIX = 'si-completed-registration-v1:';

// Historical records live under MUTABLE keys (the bare key, or
// the round-3 per-subject key with no record-id segment): any
// tab — including a still-open old version — can replace their
// content at any time, so deleting them can never be made safe
// (Astra round-7 N17/N19). Completion therefore does not delete
// a historical record; it writes an IMMUTABLE tombstone naming
// the completed identity, and every load path suppresses a
// historical record whose tombstone exists. Whether a record
// is historical is a property of its KEY LAYOUT, never of
// object identity.
function isHistoricalKey(key) {
  if (key === LEGACY_PENDING_KEY) return true;
  if (typeof key !== 'string' || !key.startsWith(PENDING_PREFIX)) {
    return false;
  }
  return !key.slice(PENDING_PREFIX.length).includes(':');
}
function tombstoneKey(record) {
  // The identity covers every stored field (Astra round-8
  // N21): two historical records that differ — even only in
  // referral data — must not share a tombstone. The encoding is
  // a VERSIONED JSON tuple (Astra round-10 R10-N2): the earlier
  // ':'-joined form was not injective — referral null and ''
  // collided, and unescaped separators let different stored
  // tuples produce the same key. JSON preserves types and
  // boundaries exactly, so distinct identities never share a
  // tombstone and identical ones always do. The pending-store
  // feature has never shipped (it is introduced by the same
  // unreleased batch as this fix), so no legacy-format markers
  // exist to migrate.
  return COMPLETED_PREFIX + 'v2:' + JSON.stringify(
    [record.subject, record.savedAt, record.email,
     record.referralCode ?? null]);
}
let recordCounter = 0;

function newRecordId() {
  recordCounter += 1;
  try {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) {
      return crypto.randomUUID();
    }
  } catch { /* fall through to the local id */ }
  return 'r' + Date.now().toString(36) + '-' + recordCounter + '-' +
    Math.floor(Math.random() * 0xffffffff).toString(36);
}

export function createLocalPendingStore(storage) {
  if (!storage || typeof storage.getItem !== 'function' ||
      typeof storage.setItem !== 'function' ||
      typeof storage.removeItem !== 'function') throw failure();
  // savedAt orders records for newest-wins loads; it increases on
  // every save from this store so two saves inside one
  // millisecond still order correctly.
  let lastSavedAt = 0;
  const nextSavedAt = () => {
    lastSavedAt = Math.max(Date.now(), lastSavedAt + 1);
    return lastSavedAt;
  };
  function parseRecord(key, raw) {
    if (!raw) return null;
    const value = JSON.parse(raw);
    if (!value || !uuid(value.subject) ||
        typeof value.email !== 'string' || !value.email) return null;
    // A non-finite timestamp is rejected outright (Astra
    // round-11 R10-N2 residual): JSON serializes Infinity as
    // null, so an infinite savedAt would collide with a
    // missing one inside the tombstone key and let one
    // identity's completion suppress a different record.
    // Records this store writes always carry finite savedAt.
    if (typeof value.savedAt === 'number' &&
        !Number.isFinite(value.savedAt)) return null;
    return { key, email: value.email, subject: value.subject,
             referralCode: typeof value.referralCode === 'string'
               ? value.referralCode : null,
             savedAt: typeof value.savedAt === 'number' ? value.savedAt : 0 };
  }
  function readKey(key) {
    // Per-candidate isolation (Astra round-12 R12-N1): one
    // malformed record — unparseable JSON under any key — must
    // yield null for THAT candidate only. Letting the parse
    // throw aborted the whole enumeration, so a single corrupt
    // record (even another subject's) suppressed every valid
    // pending registration and the resumer silently completed
    // nothing.
    try { return parseRecord(key, storage.getItem(key)); }
    catch { return null; }
  }
  function keysWithPrefix(prefix) {
    const keys = [];
    if (typeof storage.key !== 'function' ||
        typeof storage.length !== 'number') return keys;
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (typeof key === 'string' && key.startsWith(prefix)) {
        keys.push(key);
      }
    }
    return keys;
  }
  function subjectKeys(subject) {
    return keysWithPrefix(PENDING_PREFIX + subject + ':');
  }
  function identityOf(record) {
    return JSON.stringify([record.subject, record.email,
                           record.referralCode ?? null]);
  }
  function beats(a, b) {
    // THE winner rule, shared by every selection in this store
    // (Astra round-11 N23): newest savedAt wins; a tie is broken
    // by the full identity tuple and then by key — never by
    // enumeration order. The round-11 code let load() and
    // loadFor() enumerate different candidate sets (the bare
    // historical key was invisible to one of them) and broke
    // ties by storage order, so the two APIs could return
    // different records for the same store, and a failed
    // migration could make the answer REGRESS to an older
    // record. One enumeration + one comparator, applied by
    // both APIs over the same candidates, makes disagreement
    // impossible by construction.
    if (a.savedAt !== b.savedAt) return a.savedAt > b.savedAt;
    const ai = identityOf(a);
    const bi = identityOf(b);
    if (ai !== bi) return ai > bi;
    return a.key >= b.key;
  }
  function pickWinner(records) {
    // Tombstoned candidates are filtered BEFORE ranking (Astra
    // round-10 R10-N1): choosing the maximum first and filtering
    // afterwards lets a retired newer record hide a live older
    // one.
    let winner = null;
    for (const record of records) {
      if (record && !isTombstoned(record) &&
          (!winner || beats(record, winner))) {
        winner = record;
      }
    }
    return winner;
  }
  function newestOf(keys) {
    return pickWinner(keys.map(readKey).filter(Boolean));
  }
  function storedCandidates() {
    // Every stored pending record, from EVERY layout: the bare
    // historical key, the per-subject historical keys, and the
    // immutable per-record keys (Astra round-11 N23 — the bare
    // key used to be invisible to load()'s enumeration).
    const keys = [];
    try {
      if (storage.getItem(LEGACY_PENDING_KEY)) {
        keys.push(LEGACY_PENDING_KEY);
      }
    } catch { /* the prefixed enumeration still applies */ }
    keys.push(...keysWithPrefix(PENDING_PREFIX));
    return keys.map(readKey).filter(Boolean);
  }
  // Migration fallbacks, kept PER SUBJECT and PER IDENTITY
  // (Astra round-5 N7/N10, round-11 N23): when a legacy
  // record's copy cannot be written, the record itself remains
  // usable as a selection candidate. Keeping only ONE fallback
  // per subject let a second failed migration overwrite a
  // newer fallback with an older record; every failed record
  // is retained, and the shared winner rule picks among them.
  // clear() can remove the historical source key a fallback
  // names.
  const fallbackBySubject = new Map();
  function addFallback(record) {
    let byIdentity = fallbackBySubject.get(record.subject);
    if (!byIdentity) {
      byIdentity = new Map();
      fallbackBySubject.set(record.subject, byIdentity);
    }
    byIdentity.set(identityOf(record), record);
  }
  function migrateOne(sourceKey, legacy) {
    // Copy BEFORE delete (Astra round-4 N7): the destination is
    // written first; if that write fails, the source stays and
    // the legacy record is retained as this subject's fallback.
    // The source is removed only once the copy exists (or a
    // newer record for the subject already supersedes it).
    // A tombstoned identity is never re-copied (Astra round-8
    // N7): the check belongs on the success path too, not only
    // in the failure branch.
    if (isTombstoned({ ...legacy, key: sourceKey })) return null;
    try {
      const existing = newestOf(subjectKeys(legacy.subject));
      // The source may be NEWER than the existing copy (an old
      // tab replaced it after the first migration, Astra
      // round-9 N23): only a copy at least as fresh supersedes
      // the source; otherwise fall through and publish the
      // source's newer state. (newestOf returns live records
      // only, so the existing copy is never a tombstoned one.)
      if (existing && existing.savedAt >= legacy.savedAt) {
        return existing;
      }
      const stored = { email: legacy.email, subject: legacy.subject,
                       referralCode: legacy.referralCode,
                       savedAt: legacy.savedAt };
      const key = PENDING_PREFIX + legacy.subject + ':' + newRecordId();
      storage.setItem(key, JSON.stringify(stored));
      // The tombstone check above ran BEFORE publication; the
      // identity may have been completed (and tombstoned) by
      // another tab while this copy was being written (Astra
      // round-10 N7/N10). Recheck after publication: a copy of
      // a completed registration is not a live record, whatever
      // this in-flight call goes on to return.
      if (isTombstoned(stored)) return null;
      // The source is NEVER removed by migration (Astra round-8
      // N20): it is a mutable key another writer may have just
      // replaced, and get-then-remove cannot be made atomic.
      // The copy supersedes it in every load order; completion
      // tombstones retire the identity.
      return { key, ...stored };
    } catch {
      const fallback = { ...legacy, key: sourceKey };
      // A tombstoned identity stays retired even when its
      // migration copy cannot be written: never re-register it
      // as a live fallback.
      if (isTombstoned(fallback)) return null;
      addFallback(fallback);
      return fallback;
    }
  }
  function migrateLegacy() {
    // Both historical layouts (Astra round-4 N7/N10): the bare
    // global key of rounds 1-2, and the per-subject key without
    // a record-id segment from round 3. Every subject's outcome
    // is retained (round-5: a single shared fallback used to
    // discard the other subjects').
    let migrated = null;
    try {
      const legacy = readKey(LEGACY_PENDING_KEY);
      if (legacy) migrated = migrateOne(LEGACY_PENDING_KEY, legacy);
    } catch { /* best-effort */ }
    // Per-key isolation (Astra round-13 R13-N1): one malformed
    // historical record must not abort the migration of every
    // later key. The enumeration itself keeps an outer guard
    // (Astra round-14 R14-N2): a transient storage.key() /
    // length failure must not escape into the caller's outer
    // catch and suppress candidate selection, which reads the
    // records independently.
    try {
      for (const key of keysWithPrefix(PENDING_PREFIX)) {
        try {
          const rest = key.slice(PENDING_PREFIX.length);
          if (!rest.includes(':')) {
            const legacy = readKey(key);
            if (legacy) {
              const result = migrateOne(key, legacy);
              if (result && !migrated) migrated = result;
            }
          }
        } catch { /* best-effort per key */ }
      }
    } catch { /* best-effort enumeration */ }
    return migrated;
  }
  function isTombstoned(record) {
    // Suppression is by IDENTITY, for records under any key
    // layout (Astra round-9 N7/N10): a migration copy published
    // under a fresh immutable key AFTER the tombstone was
    // written is still the completed registration.
    if (!record) return false;
    try { return storage.getItem(tombstoneKey(record)) !== null; }
    catch { return false; }
  }
  function liveFallbacks(subject) {
    // A cached fallback is only as good as its source (Astra
    // round-6 N16): another tab may have migrated and completed
    // the registration since, removing the source. Validate
    // existence and identity against storage before use; a
    // stale fallback is dropped, never resurrected.
    const byIdentity = fallbackBySubject.get(subject);
    if (!byIdentity) return [];
    const live = [];
    for (const [identity, record] of [...byIdentity]) {
      let current = null;
      try { current = parseRecord(record.key, storage.getItem(record.key)); }
      catch { current = null; }
      // EXACT source equality — every identity field (Astra
      // round-12 R12-N2): comparing only subject and email let
      // a superseded fallback resurrect after its replacement
      // (same email, newer savedAt, different referral) was
      // completed and tombstoned, because the replaced source
      // still "matched" the obsolete cached record.
      if (!current || current.subject !== record.subject ||
          current.email !== record.email ||
          current.referralCode !== record.referralCode ||
          current.savedAt !== record.savedAt ||
          isTombstoned(record)) {
        byIdentity.delete(identity);
      } else {
        live.push(record);
      }
    }
    return live;
  }
  function allLiveFallbacks() {
    return [...fallbackBySubject.keys()].flatMap(liveFallbacks);
  }
  return Object.freeze({
    load() {
      try {
        // Migration runs for its side effects (copies
        // published, fallbacks registered); its return value
        // never decides the answer (Astra round-10 N7/N10). A
        // migration fallback is a real pending registration
        // whose copy could not be written (Astra round-5 N7):
        // it enters the same candidate set as every stored
        // record, and the one shared winner rule decides.
        migrateLegacy();
        return pickWinner([...storedCandidates(),
                           ...allLiveFallbacks()]);
      } catch { return null; }
    },
    loadFor(subject) {
      // The record for ONE account (Astra round-2 F12/N4):
      // subject-scoped flows — resumption at login, the shared
      // resumer — must only ever see, complete, or clear their
      // own account's record. Another subject's record is not
      // stale garbage to them; it is simply not theirs, and it
      // stays untouched under its own key.
      try {
        // The same enumeration and winner rule as load(),
        // restricted to this subject (Astra round-11 N23):
        // migration success or failure never determines which
        // records are eligible, so the two APIs cannot
        // disagree about the same store.
        migrateLegacy();
        return pickWinner([
          ...storedCandidates().filter(
            (record) => record.subject === subject),
          ...liveFallbacks(subject),
        ]);
      } catch { return null; }
    },
    save(record) {
      try {
        // Snapshot the superseded keys BEFORE publishing the new
        // one (Astra round-4 F12): cleanup may only ever remove
        // keys that existed before this save — a key another tab
        // publishes after the snapshot is never in the set, so
        // two tabs saving concurrently cannot delete each other's
        // fresh records.
        const superseded = subjectKeys(record.subject);
        const stored = { email: record.email, subject: record.subject,
                         referralCode: record.referralCode ?? null,
                         savedAt: nextSavedAt() };
        const key = PENDING_PREFIX + record.subject + ':' + newRecordId();
        storage.setItem(key, JSON.stringify(stored));
        for (const old of superseded) storage.removeItem(old);
        return { key, ...stored };
      } catch { return null; /* best-effort durability */ }
    },
    clear(record) {
      // Delete EXACTLY the record that was loaded or saved,
      // named by its immutable key. A record another tab wrote
      // since — for this subject or any other — is a different
      // key and cannot be affected. HISTORICAL records (mutable
      // keys) are never deleted at all (Astra round-7 N17/N19):
      // an immutable tombstone retires their identity instead,
      // and every load path suppresses them from then on.
      try {
        const key = typeof record === 'string' ? record : record?.key;
        if (typeof key !== 'string' ||
            (key !== LEGACY_PENDING_KEY &&
             !key.startsWith(PENDING_PREFIX))) return;
        if (record && typeof record === 'object' &&
            isHistoricalKey(key)) {
          try { storage.setItem(tombstoneKey(record), '1'); }
          catch { /* suppression is best-effort */ }
          fallbackBySubject.delete(record.subject);
          return;
        }
        storage.removeItem(key);
        if (record && typeof record === 'object') {
          fallbackBySubject.delete(record.subject);
          // If a HISTORICAL copy of this same identity still
          // exists (the migration source this record was copied
          // from), retire it with a tombstone too — deleting
          // only the immutable copy would let the next load
          // re-migrate the source and resurrect the completed
          // registration (Astra round-8 N7). Each source is
          // retired in ISOLATION, through the guarded read
          // (Astra round-13 R13-N1): a malformed bare source
          // throwing here used to abort the loop before the
          // per-subject source was reached, so the completed
          // registration came back on the next load.
          for (const sourceKey of [LEGACY_PENDING_KEY,
                                   PENDING_PREFIX + record.subject]) {
            try {
              const source = readKey(sourceKey);
              if (source && source.subject === record.subject &&
                  source.email === record.email &&
                  source.savedAt === record.savedAt &&
                  source.referralCode === record.referralCode) {
                storage.setItem(tombstoneKey(source), '1');
              }
            } catch { /* best-effort per source */ }
          }
        }
      } catch { /* best-effort */ }
    },
  });
}

// The pending store for browser views (Astra round-1 F11):
// merely READING window.localStorage can throw under browser
// policy, and a view that evaluates the getter unguarded disables
// every entry control when it does. Acquisition lives here,
// guarded; an unavailable store yields null and the controllers
// run exactly as they did before durable pending state existed.
export function createBrowserPendingStore() {
  let storage = null;
  try { storage = window.localStorage; } catch { storage = null; }
  if (!storage) return null;
  try { return createLocalPendingStore(storage); } catch { return null; }
}

// The existing-entry resumption callback (A2): completes a durable
// pending registration for the user who just authenticated, using
// THEIR session token, before any handoff. The lookup is
// subject-scoped (Astra round-2 F12/N4): another account's record
// is never read, completed, or cleared by this callback. A
// matching record whose completion fails throws, so the handoff
// stops instead of presenting a confirmed-but-unusable identity
// as finished.
export function createRegistrationResumer({ origin, fetch: request, pendingStore }) {
  const completionUrl = httpsOrigin(origin) + '/onboarding/complete';
  if (typeof request !== 'function' || !pendingStore) throw failure();
  return async ({ subject, token }) => {
    // Subject-scoped (Astra round-2 F12/N4): only THIS account's
    // record is ever read, completed, or cleared here. Another
    // account's record under its own key is not this flow's to
    // discard — its owner's tab may still be registering.
    let saved = null;
    try { saved = pendingStore.loadFor(subject); } catch { saved = null; }
    if (!saved) return;
    const response = await request(completionUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
      body: JSON.stringify({ subject }),
      redirect: 'error', credentials: 'omit', cache: 'no-store',
    });
    if (!response.ok) throw failure();
    const body = await response.json();
    if (!body || Object.keys(body).length !== 1 ||
        body.registration_complete !== true) throw failure();
    // Success clears only the record that was completed (Astra
    // round-2 N4): another tab's newer registration — for this
    // subject or any other — is a different record and survives.
    try { pendingStore.clear(saved); }
    catch { /* best-effort */ }
  };
}

export function createIndependentEntry({ auth, origin, fetch: request, handoff, pendingStore = null }) {
  const completionUrl = httpsOrigin(origin) + '/onboarding/complete';
  if (!auth || typeof request !== 'function' || typeof handoff !== 'function') throw failure();
  let state = 'ready';
  let busy = false;
  let pending = null;
  let pendingRecord = null;
  let accessToken = null;
  // Fail-soft durable pending record (A2): every access is guarded
  // so a broken store can never break authentication itself.
  function loadPending() {
    if (!pendingStore) return null;
    try { return pendingStore.load(); } catch { return null; }
  }
  function loadPendingFor(subject) {
    if (!pendingStore) return null;
    try { return pendingStore.loadFor(subject); } catch { return null; }
  }
  function persistPending() {
    if (!pendingStore || !pending?.subject) return;
    try {
      const stored = pendingStore.save({ email: pending.email,
                                         referralCode: pending.referralCode,
                                         subject: pending.subject });
      if (stored) pendingRecord = stored;
    } catch { /* best-effort */ }
  }
  function clearPending(record) {
    if (!pendingStore || !record) return;
    try { pendingStore.clear(record); } catch { /* best-effort */ }
  }
  // Resume an interrupted signup on construction: a persisted
  // record restores the verification step, so a page reload (the
  // views dispose their controllers on pagehide) no longer loses
  // the pending registration.
  const restored = loadPending();
  if (restored && uuid(restored.subject)) {
    pending = Object.freeze({ email: restored.email,
                              referralCode: restored.referralCode,
                              subject: restored.subject });
    pendingRecord = restored;
    state = 'verification';
  }

  function active() { if (state === 'disposed' || state === 'finished') throw failure(); }
  function clear() { pending = null; pendingRecord = null; accessToken = null; auth = null; request = null; handoff = null; }
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
  async function postCompletion(subject) {
    await sameUser(subject);
    const response = await request(completionUrl, {
      method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + accessToken },
      body: JSON.stringify({ subject }),
      redirect: 'error', credentials: 'omit', cache: 'no-store',
    });
    active();
    if (!response.ok) throw failure();
    const body = await response.json();
    active();
    if (!body || Object.keys(body).length !== 1 || body.registration_complete !== true) throw failure();
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
      // Login is also the way out of a restored 'verification'
      // state (Astra round-1 F10): a user whose email is already
      // confirmed — in another tab, or before an interruption —
      // has nothing to verify, and the pending record is completed
      // (or dropped, if it names someone else) by the authenticated
      // resumption below. Without this, the restored state offered
      // no path to the sign-in that finishes the registration.
      return run(['ready', 'verification'], async () => {
        const result = await auth.signInWithPassword({ email, password });
        active();
        accessToken = session(result);
        try {
          // Authenticated resumption (A2): a pending registration
          // naming THIS user is completed before the handoff — the
          // server verifies the session against the sealed creation
          // receipt, so an interrupted signup heals at login instead
          // of stranding a confirmed user without an SI identity.
          // BOTH records are reconciled (Astra round-2 N3): this
          // session's in-memory signup (the only record there is
          // when no durable store exists) and the durable record
          // FOR THIS USER (subject-scoped: another account's
          // record is never this login's to touch). A failed
          // completion fails the login (the records survive for
          // the next try); it never hands off as if registration
          // were finished.
          const userId = result.data.user.id;
          let completed = false;
          if (pending && pending.subject === userId) {
            await postCompletion(userId);
            completed = true;
          } else if (pending && pending.subject) {
            pending = null; // This session's abandoned attempt.
            pendingRecord = null;
          }
          const saved = loadPendingFor(userId);
          if (saved) {
            if (!completed) await postCompletion(userId);
            clearPending(saved);
          }
          return await finish(userId);
        }
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
        persistPending();
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
        await postCompletion(pending.subject);
        clearPending(pendingRecord);
        return finish(pending.subject);
      });
    },
    abandonPending() {
      // Discard a pending registration (e.g. a mistyped signup
      // email, or a restored record the user does not recognize)
      // and return to the entry forms. The upstream account, if
      // created, simply stays unconfirmed; nothing was provisioned.
      if (busy || state !== 'verification') throw failure();
      clearPending(pendingRecord);
      pending = null;
      pendingRecord = null;
      state = 'ready';
      return { kind: 'abandoned' };
    },
    dispose() { state = 'disposed'; clear(); },
  };
}
