// State-bound access-token handoff for the account pages.
//
// A token may arrive in the URL fragment (#access_token=...&state=...)
// when the user's own connection flow sends the browser to an account
// page. A bare fragment token is NEVER adopted (Astra final round
// #1): adoption requires the fragment's state to match a pending
// handoff nonce that this browser armed earlier in the same tab
// session. The nonce is a random value kept only in sessionStorage;
// a crafted link cannot know it, so an unsolicited token — for
// example an attacker's, which would silently turn the victim's
// browser into the attacker's account — is ignored. The nonce is
// single-use: a successful adoption consumes it, so the same link
// cannot be replayed.
//
// The token itself is read exactly once and kept only in this
// closure — never in web storage, never in a cookie. Any fragment
// carrying an access_token is stripped from the URL immediately,
// adopted or not: bearer tokens do not sit in the address bar.
const HANDOFF_KEY = 'si-fragment-handoff-v1';
// The handoff state as ONE record (batch 2f): the pending
// nonce and the carry expectation it was armed with live in a
// single sessionStorage value under this key, so every
// transition — arm, bind, consume, invalidate — is one write,
// or an ordered removal whose failure leaves either the whole
// previous state or nothing, never half of it. Rounds 1-4 of
// batch 2e kept finding the seam between the two records this
// replaces (the nonce as a bare string under HANDOFF_KEY, the
// expectation as JSON under CARRY_KEY below): an arm whose
// second step failed left a nonce without its expectation, an
// invalidation whose first step succeeded left the inverse,
// and each patch only moved the seam. The legacy keys are
// still READ — strictly — when no v2 record exists, so a tab
// session armed by the previous release survives the deploy
// boundary; every write goes to the single record.
const RECORD_KEY = 'si-fragment-handoff-v2';

function defaultStorage() {
  try { return globalThis.sessionStorage ?? null; } catch { return null; }
}

function randomNonce() {
  const crypto = globalThis.crypto;
  if (!crypto?.getRandomValues) return null;
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  let hex = '';
  for (const byte of bytes) hex += byte.toString(16).padStart(2, '0');
  return hex;
}

// A carry binding in its stored JSON shape: 'none', {sid} or
// {rev}; in memory, null means "no expectation recorded" and
// undefined means "not a binding at all".
function carryToStored(carry) {
  if (carry === null || carry === undefined) return null;
  if (carry === 'none') return 'none';
  if (typeof carry.sid === 'string' && carry.sid) return { sid: carry.sid };
  if (typeof carry.rev === 'string' && carry.rev) return { rev: carry.rev };
  return undefined;
}

// Strictly validate a stored carry value (the `carry` field of
// the v2 record): returns the binding (null / 'none' / {sid} /
// {rev}), or undefined when the shape is not exactly one of
// the protocol's forms.
function carryFromStored(value) {
  if (value === null) return null;
  if (value === 'none') return 'none';
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return undefined;
  }
  const keys = Object.keys(value);
  if (keys.length !== 1) return undefined;
  if (keys[0] === 'sid' && typeof value.sid === 'string' && value.sid) {
    return { sid: value.sid };
  }
  if (keys[0] === 'rev' && typeof value.rev === 'string' && value.rev) {
    return { rev: value.rev };
  }
  return undefined;
}

// The legacy (pre-2f) carry record, strictly parsed: the
// binding plus the nonce it was armed for, or undefined when
// the shape is not exactly one of the protocol's forms or the
// nonce association is missing (batch 2e round 4 rules,
// unchanged).
function legacyCarryBinding(parsed) {
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return undefined;
  }
  let binding = null;
  let bindingKey = null;
  if (parsed.none === true) { binding = 'none'; bindingKey = 'none'; }
  else if (typeof parsed.sid === 'string' && parsed.sid) {
    binding = { sid: parsed.sid }; bindingKey = 'sid';
  } else if (typeof parsed.rev === 'string' && parsed.rev) {
    binding = { rev: parsed.rev }; bindingKey = 'rev';
  }
  if (binding === null) return undefined;
  for (const key of Object.keys(parsed)) {
    if (key !== bindingKey && key !== 'nonce') return undefined;
  }
  if (typeof parsed.nonce !== 'string' || !parsed.nonce) return undefined;
  return { binding, nonce: parsed.nonce };
}

// The pending handoff state, read ONCE and strictly (batch
// 2f). Returns one of:
// - { state: 'record', nonce, carry, legacy } — a validated
//   pending nonce and the expectation recorded with it
//   (carry null = externally armed, no expectation);
// - { state: 'absent' } — confirmed: no record under any key;
// - { state: 'invalid' } — state is present but cannot be
//   trusted: a read threw, a stored value is unparseable or
//   malformed, or legacy metadata is orphaned/unassociated.
// Absence is decided ONLY by a getItem result of null or
// undefined: a stored '' (or any other present value) is a
// value, parsed and judged, never "nothing" (batch 2e round
// 4, contract 3b). 'invalid' never downgrades to an external
// handoff anywhere in this module.
function readHandoffState(store) {
  let raw;
  try { raw = store.getItem(RECORD_KEY); } catch { return { state: 'invalid' }; }
  if (raw !== null && raw !== undefined) {
    let parsed;
    try { parsed = JSON.parse(raw); } catch { return { state: 'invalid' }; }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return { state: 'invalid' };
    }
    const keys = Object.keys(parsed);
    if (keys.length !== 2 || !keys.includes('nonce') || !keys.includes('carry')) {
      return { state: 'invalid' };
    }
    if (typeof parsed.nonce !== 'string' || !parsed.nonce) {
      return { state: 'invalid' };
    }
    const carry = carryFromStored(parsed.carry);
    if (carry === undefined) return { state: 'invalid' };
    return { state: 'record', nonce: parsed.nonce, carry, legacy: false };
  }
  // Legacy fallback: the two-key state written before batch
  // 2f, validated with the round-4 rules.
  let nonce;
  try { nonce = store.getItem(HANDOFF_KEY); } catch { return { state: 'invalid' }; }
  let carryRaw;
  try { carryRaw = store.getItem(CARRY_KEY); } catch { return { state: 'invalid' }; }
  const carryPresent = carryRaw !== null && carryRaw !== undefined;
  if (nonce === null || nonce === undefined) {
    // An expectation without its nonce is orphaned metadata:
    // present, but unverifiable — not absence.
    return carryPresent ? { state: 'invalid' } : { state: 'absent' };
  }
  if (typeof nonce !== 'string' || !nonce) return { state: 'invalid' };
  if (!carryPresent) {
    return { state: 'record', nonce, carry: null, legacy: true };
  }
  let parsed;
  try { parsed = JSON.parse(carryRaw); } catch { return { state: 'invalid' }; }
  const legacy = legacyCarryBinding(parsed);
  if (legacy === undefined) return { state: 'invalid' };
  // A well-formed record naming a DIFFERENT nonce is stale:
  // it speaks only for that dead nonce and counts as no
  // record for this one (round 4).
  if (legacy.nonce !== nonce) {
    return { state: 'record', nonce, carry: null, legacy: true };
  }
  return { state: 'record', nonce, carry: legacy.binding, legacy: true };
}

// The single write every arming transition is made of: the
// nonce and its expectation land together or not at all. A
// failed write leaves the previous state — legacy or record —
// completely intact (batch 2f). The write is a COMMIT and is
// verified by reading it back (batch 2i — the same class as
// batch 2h F3 on the app side): a setItem that neither throws
// nor stores must not report a landed arm, or callers would
// hand out a nonce no record attests and record carry
// expectations that do not exist. After a verified write the
// legacy keys are swept best-effort: the record takes
// precedence on every read, so a surviving legacy key is inert
// (and consumption removes the legacy keys before the record).
function writeHandoffState(store, nonce, carry) {
  const stored = carryToStored(carry);
  if (stored === undefined) return false;
  const serialized = JSON.stringify({ nonce, carry: stored });
  try {
    store.setItem(RECORD_KEY, serialized);
    if (store.getItem(RECORD_KEY) !== serialized) return false;
  } catch { return false; }
  sweepLegacyHandoffKeys(store);
  return true;
}

function sweepLegacyHandoffKeys(store) {
  try { store.removeItem(CARRY_KEY); } catch { /* record takes precedence */ }
  try { store.removeItem(HANDOFF_KEY); } catch { /* record takes precedence */ }
}

// Remove the handoff state in the one order that can never
// separate a nonce from its expectation, confirming each
// removal by reading the key back BEFORE the next key is
// touched (batch 2i, Astra batch2h diff review blocker 1 and
// batch 2i round 1). The danger state is a bare legacy nonce:
// with no expectation and no record it authenticates any
// claim. The keys therefore come down as:
//   1. the legacy nonce key — while the expectation and the
//      record still stand, a nonce whose removal cannot be
//      confirmed stays bound; aborting here changes nothing;
//   2. the record — it binds nonce and expectation inside
//      itself, so once the legacy nonce is confirmed gone, a
//      record whose removal cannot be confirmed still keeps
//      its nonce bound; aborting here leaves the record alone
//      preserving the association;
//   3. the expectation key — with both nonce carriers
//      confirmed gone, an expectation whose removal cannot
//      be confirmed is inert: no nonce remains for it to bind.
// Verifying only after all three removals (the first shape of
// this fix) detected a silently ignored legacy-nonce removal
// too late: the record was already deleted and the surviving
// nonce stood bare, replayable against a later account's
// session. A removeItem that neither throws nor removes — a
// silently ignoring store — now fails the consumption at the
// first unconfirmed key, and with it the adoption that
// required it. A store whose reads lie is outside this check,
// as it is for every read-back in the module: claim
// verification itself trusts reads.
function handoffKeyAbsent(store, key) {
  let raw;
  try { raw = store.getItem(key); } catch { return false; }
  return raw === null || raw === undefined;
}

function removeHandoffKey(store, key) {
  try { store.removeItem(key); } catch { return false; }
  return handoffKeyAbsent(store, key);
}

function removeHandoffState(store) {
  if (!removeHandoffKey(store, HANDOFF_KEY)) return false;
  if (!removeHandoffKey(store, RECORD_KEY)) return false;
  if (!removeHandoffKey(store, CARRY_KEY)) return false;
  return true;
}

// Arm a fresh handoff nonce and return it. The connection flow is
// expected to echo it back as the fragment's state parameter, the
// standard OAuth state discipline. Arming is safe at page load: the
// nonce is generated in this browser and never leaves it except as
// the echoed state of a flow this browser started toward.
//
// A fresh nonce starts a fresh expectation (batch 2e round 4, R1):
// any carry record was armed for the nonce it names, and a NEW
// nonce must not inherit a previous carry's expectation — an
// ordinary external handoff would otherwise be judged against a
// binding it never carried and refused. Under the single record
// (batch 2f) the retirement and the arming are the SAME write:
// the new record carries carry=null, and if the write cannot
// land, no fresh nonce is armed either (fail closed) and the
// previous nonce and its expectation stay in place together.
// Reusing the pending nonce (ensureFragmentHandoff) is the only
// path that preserves an existing expectation.
export function armFragmentHandoff(storage) {
  const store = storage ?? defaultStorage();
  if (!store) return null;
  const nonce = randomNonce();
  if (!nonce) return null;
  if (!writeHandoffState(store, nonce, null)) return null;
  return nonce;
}

// The pending nonce, arming one if this tab session has none
// yet. A state that cannot be verified is replaced wholesale
// by the fresh arm: the single write destroys the unverifiable
// record atomically, and arming adopts nothing, so no
// downgrade is possible.
export function ensureFragmentHandoff(storage) {
  const store = storage ?? defaultStorage();
  if (!store) return null;
  const state = readHandoffState(store);
  if (state.state === 'record') return state.nonce;
  return armFragmentHandoff(store);
}

// The handoff in full: the token plus the shared-session
// BINDING the carrying link was written under (batch 2e rounds
// 2-3, R1). A carried link names its originating binding in the
// fragment — the lineage sid, the revision for a legacy record
// without one, or an explicit marker that no shared record
// existed — and the carrying side records the same binding in
// the same record as the nonce. At adoption the three must agree:
// the fragment's claim, the binding recorded with the nonce,
// and the CURRENT shared binding. Any disagreement refuses the
// token: a token carried under account A must not be adopted
// into account B's lineage just because the nonce still
// matches, and a claim cannot be dropped or invented to
// downgrade a carried handoff into an unchecked one. External
// connection handoffs are the inverse shape: no carry record,
// no claim — anything else is refused.
export function takeFragmentHandoff(location, history, storage, persistentStorage) {
  const hash = typeof location?.hash === 'string' ? location.hash : '';
  if (!hash.startsWith('#')) return null;
  const params = new URLSearchParams(hash.slice(1));
  const values = params.getAll('access_token');
  if (values.length === 0) return null;
  history.replaceState(null, '', location.pathname + location.search);
  if (values.length !== 1 || !values[0] || values[0].length > 8192) return null;
  // The no-crypto guarantee is enforced at adoption too, not only
  // when arming: a context that cannot produce randomness must
  // never adopt a handed-over token.
  if (!randomNonce()) return null;
  const store = storage ?? defaultStorage();
  if (!store) return null;
  // The pending state, read once and strictly (batch 2f): the
  // nonce and the expectation recorded with it come from the
  // SAME record, so the pair this adoption is judged against
  // can never be a mix of two generations. A state that is
  // present but unverifiable refuses the adoption outright:
  // it is not evidence of an external handoff.
  const pending = readHandoffState(store);
  if (pending.state !== 'record') return null;
  const states = params.getAll('state');
  if (states.length !== 1 || states[0] !== pending.nonce) return null;
  // A legacy pair is migrated into the single record BEFORE
  // anything is deleted (batch 2f round 2): the two legacy
  // keys have no record protecting them, so deleting the
  // expectation first and failing on the nonce would leave
  // the nonce armed without its expectation — a claim could
  // then be stripped and the fragment adopted unchecked. If
  // the migration write cannot land, the adoption is refused
  // with the legacy pair completely intact.
  if (pending.legacy
      && !writeHandoffState(store, pending.nonce, pending.carry)) {
    return null;
  }
  // The binding the carrying side recorded for this nonce
  // (null = externally armed, not a carry).
  const expected = pending.carry;
  // Single-use is a hard condition, not best effort: if the
  // state cannot be consumed, the token is not adopted (it
  // would otherwise stay replayable). Consumption removes the
  // keys in the order documented at removeHandoffState —
  // each removal confirmed before the next key is touched —
  // synchronously, before the token is processed, so a
  // failure can never separate the nonce from its
  // expectation (batch 2f; ordering pinned in batch 2i).
  if (!removeHandoffState(store)) return null;
  // The fragment's binding claim: AT MOST ONE claim across all
  // three parameter kinds. Duplicates, coexistence and empty
  // values are malformed — never silently prioritized.
  const sids = params.getAll('lineage');
  const revs = params.getAll('lrev');
  const nones = params.getAll('lstate');
  if (sids.length + revs.length + nones.length > 1) return null;
  let carried = null;
  if (sids.length === 1) {
    if (!sids[0]) return null;
    carried = { sid: sids[0] };
  } else if (revs.length === 1) {
    if (!revs[0]) return null;
    carried = { rev: revs[0] };
  } else if (nones.length === 1) {
    if (nones[0] !== 'none') return null;
    carried = 'none';
  }
  // The claim must be exactly what the nonce was armed for:
  // a carried handoff cannot shed its claim, and an external
  // handoff cannot acquire one.
  if (expected !== null) {
    if (!bindingsEqual(carried, expected)) return null;
  } else if (carried !== null) {
    return null;
  }
  // And the claim must still describe the shared state NOW:
  // the lineage may have moved since the link was written.
  // The comparison runs against a VERIFIED binding (batch 2e
  // round 4, R1): a current state that cannot be read back is
  // not 'none', and the carry is refused rather than adopted
  // under an unverifiable lineage. The verified snapshot rides
  // back with the handoff as `lineage`, so the destination
  // session binds to the state that was actually validated —
  // never to a second, independent read that could observe a
  // different state and bind the token to it.
  let lineage;
  let identity;
  if (carried !== null) {
    const persistent = persistentStorage ?? defaultPersistentStorage();
    if (persistent) {
      // Binding and identity from ONE derivation (batch 2i):
      // the state the claim is validated against and the
      // identity the destination binds the token to are the
      // same observed state, never two reads that could
      // observe different states.
      const current = verifiedSharedIdentity(persistent);
      if (current === null || current.state === 'unverifiable') {
        return null;
      }
      const currentBinding = current.state === 'absent'
        ? 'none' : current.binding;
      if (currentBinding === null) return null;
      if (!bindingsEqual(carried, currentBinding)) return null;
      lineage = currentBinding === 'none' ? null
        : currentBinding.sid ? { rev: null, sid: currentBinding.sid }
        : { rev: currentBinding.rev, sid: null };
      identity = current.state === 'record'
        ? {
          state: 'record',
          refreshToken: current.refreshToken,
          rev: current.rev,
          sid: current.sid,
        }
        : { state: 'absent' };
    }
  }
  return { token: values[0], binding: carried, lineage, identity };
}

export function takeFragmentToken(location, history, storage, persistentStorage) {
  const handoff = takeFragmentHandoff(location, history, storage, persistentStorage);
  return handoff ? handoff.token : null;
}

// --- Shared-session lineage binding (batch 2e, R1) ---
// The account pages join the app's cross-tab session protocol.
// A fragment token captured at page load used to be served
// forever, so an account page kept acting on credentials the
// app had long replaced — a newer app session did not retire
// the page's older token. The provider now binds the captured
// token to the shared session record's LINEAGE (sid) at
// capture time and retires it the moment the lineage moves:
// a replacement login, a fragment adoption or a sign-out in
// any tab starts a new lineage; a disappearance of an
// established record invalidates; a record appearing where
// none existed at capture also retires the capture. A mere
// rotation keeps the lineage, so the app tab's routine
// refresh never kills an open account page. Records written
// before lineages existed carry no sid; there the revision
// comparison is the fallback, exactly as the app fences.
const SHARED_SESSION_KEY = 'si-app-session-v1';
const SHARED_EPOCH_KEY = 'si-app-session-epoch-v1';
// The binding a carried link was written under, recorded in
// sessionStorage next to the handoff nonce (batch 2e round 3,
// R1): {sid} for a lineage record, {rev} for a legacy record
// without a lineage, {"none":true} for no shared record at all
// — plus, since round 4, the pending nonce the record was
// armed for, which is the only nonce it can ever speak for.
// Since batch 2f this key is legacy: the expectation lives in
// the single handoff record (RECORD_KEY) together with the
// nonce, and this key is only read (strictly) as part of the
// pre-2f fallback and swept on every write.
const CARRY_KEY = 'si-fragment-handoff-carry-v1';

// The shared session as ONE verified snapshot (batch 2g, R1):
// the projection of the verified derivation that the APP
// (app-auth.js) loads and fences by — token, revision and
// lineage read from the same records under the same shape
// rules as verifiedSharedCarryBinding, so the app surface and
// the account surface can never classify the same stored
// bytes differently. The app used to parse the records
// itself, leniently (the derivation removed below in this
// batch did the same on this side): a stored 'null' literal
// or '{}' collapsed into absence, and a session record
// carrying a lineage but no revision inherited the epoch
// record's revision — so a foreign (or corrupted) session
// record left the app's retained fragment token dispatchable
// under the clear tombstone its adoption had written (Astra
// batch2f diff review, R1 app-side variant).
//
// Batch 2h (Astra batch2g diff review, F2): the snapshot and
// the carry binding are now projections of ONE derivation —
// deriveSharedState below — not two judgment paths that
// happen to share parsing helpers. They used to be free to
// disagree, and did, on exactly one shape: a legacy session
// record (no rev, no sid of its own) whose epoch companion
// is ABSENT. The derivation now decides that shape once
// (see the deferral branch): a valid unnamed session, which
// this projection reports with rev and sid null, and which
// names no carry binding, so the binding projection is null
// for it and carries refuse it. The difference between the
// projections is a difference of question — what session is
// held vs what binding can be named — decided in one place,
// not a difference of judgment about the bytes. The
// derivation also reports `unnamed`: whether
// a session record carries neither revision nor lineage of
// its own (its revision, when one exists, is deferred to
// the epoch record). Consumers judge record-shape questions
// by that flag, never by the derived revision — a deferred
// revision is non-null, and judging by it let such records
// slip past the access-only naming in app-auth.js and stay
// unnamed forever (F4).
//
// Returns null when there is no persistent storage at all.
// Otherwise one of:
// - { state: 'absent' } — confirmed: neither key holds a
//   record.
// - { state: 'unverifiable' } — a present record cannot be
//   read back, or its shape is one the protocol never writes.
// - { state: 'record', refreshToken, rev, sid, unnamed } —
//   the session record's token (null when only a clear
//   tombstone exists), the revision the state attests, and
//   its lineage. A session record carrying a lineage reports
//   that lineage and its OWN revision (never the epoch's);
//   the epoch record supplies the revision only for a legacy
//   session record carrying neither field — the same
//   deferral as the verified binding.
export function verifiedSharedSessionSnapshot(persistentStorage) {
  if (!persistentStorage) return null;
  const derived = deriveSharedState(persistentStorage);
  if (derived.classification === 'absent') return { state: 'absent' };
  if (derived.classification === 'unverifiable') {
    return { state: 'unverifiable' };
  }
  return {
    state: 'record',
    refreshToken: derived.refreshToken,
    rev: derived.rev,
    sid: derived.sid,
    unnamed: derived.unnamed,
  };
}

// Read one shared-state key in three states (batch 2f):
// 'absent' is decided ONLY by a null/undefined result — a
// stored '' or any other present value is a record to be
// judged, never nothing. Anything present that cannot be
// parsed into a plain object is 'unreadable'.
function readSharedRecord(persistentStorage, key) {
  let raw;
  try { raw = persistentStorage.getItem(key); } catch { return { state: 'unreadable' }; }
  if (raw === null || raw === undefined) return { state: 'absent' };
  let parsed;
  try { parsed = JSON.parse(raw); } catch { return { state: 'unreadable' }; }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { state: 'unreadable' };
  }
  return { state: 'record', record: parsed };
}

const isNonEmptyString = (value) => typeof value === 'string' && value.length > 0;

// The binding a SESSION record carries, strictly (batch 2f):
// the record must be a session record at all — a non-empty
// string refreshToken is what makes it one; a present record
// without one ({} above all) is a shape this protocol never
// writes, i.e. unverifiable, NOT confirmed absence (batch 2e
// round 4, contract 2). The fields the binding depends on are
// type-checked when present; unknown extra fields are
// tolerated (several writer generations exist). Returns the
// binding ({sid} / {rev}), 'epoch' when the record is a valid
// session whose revision lives in the epoch record (the
// legacy shape), or null when the shape is invalid.
function sessionRecordBinding(record) {
  if (!isNonEmptyString(record.refreshToken)) return null;
  // Recognized fields are validated WHENEVER PRESENT (batch
  // 2f round 2): no writer produces a null or mistyped
  // identity field, so {refreshToken, sid, rev: null} is not
  // a session record with a blemish — it is not a session
  // record at all.
  if ('rev' in record && !isNonEmptyString(record.rev)) return null;
  if ('sid' in record && !isNonEmptyString(record.sid)) return null;
  if ('owner' in record && !isNonEmptyString(record.owner)) return null;
  if ('savedAt' in record && (typeof record.savedAt !== 'number'
      || !Number.isFinite(record.savedAt))) return null;
  // The epoch field belongs to the OTHER record kind: the
  // lineage reader would interpret it as this record's own
  // revision while the verified derivation defers to the
  // epoch record, so the two could name different lineages
  // for one accepted record (batch 2f round 3). No session
  // writer produces it — its presence is a foreign shape.
  if ('epoch' in record) return null;
  if ('sid' in record) return { sid: record.sid };
  if ('rev' in record) return { rev: record.rev };
  return 'epoch';
}

// The binding an EPOCH record carries, strictly (batch 2f;
// variants separated in round 2): the tombstone form
// {rev, owner, kind:'clear', sid, at} and the legacy numeric
// form {epoch, owner} never mix — a record naming both
// revision fields, or a numeric form carrying any sid key,
// is a shape no writer produces. Recognized metadata
// (owner, kind, at) is validated whenever present; unknown
// extension fields are tolerated. Returns the binding, or
// null when the record yields none ({} above all).
function epochRecordBinding(record) {
  if ('owner' in record && !isNonEmptyString(record.owner)) return null;
  if ('kind' in record && record.kind !== 'clear') return null;
  if ('at' in record && (typeof record.at !== 'number'
      || !Number.isFinite(record.at))) return null;
  if ('sid' in record && !isNonEmptyString(record.sid)) return null;
  if ('rev' in record && 'epoch' in record) return null;
  if ('rev' in record) {
    if (!isNonEmptyString(record.rev)) return null;
    return 'sid' in record ? { sid: record.sid } : { rev: record.rev };
  }
  if ('epoch' in record) {
    if (typeof record.epoch !== 'number' || !Number.isFinite(record.epoch)) {
      return null;
    }
    if ('sid' in record) return null; // numeric form: no sid, ever
    return { rev: `legacy:${record.epoch}` };
  }
  return null;
}

// The REVISION an epoch record attests, or null when the
// record is not a well-shaped epoch record. Used where a
// legacy SESSION defers to the epoch for its revision: the
// session-lineage reader pairs such a session with the
// epoch's revision and a null sid, so the verified binding
// must carry the revision too — inheriting the epoch's sid
// would name a lineage the reader never reports, and the
// carried token would retire on its first fence check
// (batch 2f round 2).
function epochRecordRevision(record) {
  if (epochRecordBinding(record) === null) return null;
  if ('rev' in record) return record.rev;
  return `legacy:${record.epoch}`;
}

// THE derivation of the shared state (batch 2h, F2): every
// surface's view of the shared session — the carry binding
// and the app snapshot alike — is a projection of this one
// classification, computed by this one judgment path. Batch
// 2g left the binding and the snapshot as separate paths
// that shared parsing helpers and still disagreed on the
// legacy shape below; two paths that CAN disagree eventually
// do, and the disagreement is itself the defect.
//
// Returns one of:
// - { classification: 'absent' } — confirmed: neither key
//   holds a record.
// - { classification: 'unverifiable' } — a present record
//   cannot be read back, or its shape is one the protocol
//   never writes.
// - { classification: 'record', refreshToken, rev, sid,
//   binding, unnamed } — the session record's token (null
//   when only a clear tombstone exists), the revision the
//   state attests, its lineage, the carry binding the state
//   names (null for exactly one shape: the unnamed legacy
//   session without its epoch companion, which names no
//   binding — see the deferral branch), and whether a
//   session record carries neither revision nor lineage of
//   its own.
function deriveSharedState(persistentStorage) {
  const session = readSharedRecord(persistentStorage, SHARED_SESSION_KEY);
  if (session.state === 'unreadable') {
    return { classification: 'unverifiable' };
  }
  if (session.state === 'record') {
    const binding = sessionRecordBinding(session.record);
    if (binding === null) return { classification: 'unverifiable' };
    if (binding === 'epoch') {
      // A valid legacy session whose revision, if any, lives
      // in the epoch record. A companion that is present but
      // malformed leaves the state unverifiable: it spoke,
      // and what it said cannot be trusted. A companion that
      // is ABSENT is the one shape the two projections answer
      // differently — by design, and decided HERE, once
      // (batch 2h, F2): the record is a valid unnamed session
      // (it dates from the first persistent-session release,
      // which wrote the session record before the epoch
      // record existed), and the app — which holds that very
      // token and fences it by whole identity, token value
      // included — loads it and names it at the first
      // rotation. But the state names NO carry binding: a
      // carry cannot name a lineage that does not exist, and
      // the account surface, which holds only a fragment
      // token and a binding claim, cannot fence an unnamed
      // record by value the way the app can — so the binding
      // projection is null for this shape, and carries refuse
      // it exactly as they refuse the unverifiable. The
      // projections differ because they answer different
      // questions (what session is held vs what binding can
      // be named), never because they judge the bytes
      // differently. When the companion speaks, the binding
      // is the epoch's REVISION, never its sid (see
      // epochRecordRevision).
      const epoch = readSharedRecord(persistentStorage, SHARED_EPOCH_KEY);
      if (epoch.state === 'unreadable') {
        return { classification: 'unverifiable' };
      }
      if (epoch.state === 'absent') {
        return {
          classification: 'record',
          refreshToken: session.record.refreshToken,
          rev: null, sid: null, binding: null, unnamed: true,
        };
      }
      const rev = epochRecordRevision(epoch.record);
      if (rev === null) return { classification: 'unverifiable' };
      return {
        classification: 'record',
        refreshToken: session.record.refreshToken,
        rev, sid: null, binding: { rev }, unnamed: true,
      };
    }
    return {
      classification: 'record',
      refreshToken: session.record.refreshToken,
      rev: isNonEmptyString(session.record.rev) ? session.record.rev : null,
      sid: binding.sid ?? null,
      binding, unnamed: false,
    };
  }
  const epoch = readSharedRecord(persistentStorage, SHARED_EPOCH_KEY);
  if (epoch.state === 'unreadable') {
    return { classification: 'unverifiable' };
  }
  if (epoch.state === 'absent') return { classification: 'absent' };
  const binding = epochRecordBinding(epoch.record);
  if (binding === null) return { classification: 'unverifiable' };
  return {
    classification: 'record',
    refreshToken: null,
    rev: epochRecordRevision(epoch.record),
    sid: isNonEmptyString(epoch.record.sid) ? epoch.record.sid : null,
    binding, unnamed: false,
  };
}

// The current shared binding, VERIFIED (batch 2e round 4, R1;
// shapes hardened in batch 2f): the binding projection of the
// one derivation of the shared state (deriveSharedState —
// batch 2h made the projection literal: every surface reads
// the state there or through its projections, and batch 2g
// removed the lenient twin). A state that cannot be read
// back — a throwing read, an unparseable value, or a present
// record whose shape the protocol never writes ({} / [] / a
// session record with no usable refresh token) — yields null
// (UNVERIFIABLE) instead of collapsing into 'none'. Null is
// also the projection for the one verifiable record that
// names no binding — the unnamed legacy session without its
// epoch companion (batch 2h, F2; the decision and its
// rationale live in deriveSharedState) — and consumers
// refuse both, since neither can be bound to a carry. 'none'
// from this function means CONFIRMED absence: both keys the
// binding could ride on were read and held no record. The
// destination must refuse a carried handoff whose current
// binding cannot be verified: round 3's collapse let a stale
// 'none' carry validate while the shared state had in fact
// already moved, and the token was then bound to the moved
// lineage by a second read.
export function verifiedSharedCarryBinding(persistentStorage) {
  if (!persistentStorage) return null;
  const derived = deriveSharedState(persistentStorage);
  if (derived.classification === 'absent') return 'none';
  if (derived.classification === 'unverifiable') return null;
  return derived.binding;
}

// The shared state as ONE identity (batch 2i): classification,
// refresh token, revision, lineage and binding from a single
// pass of the one derivation. The carry binding and the app
// snapshot are projections of deriveSharedState; this is the
// projection the ACCOUNT supply fence compares whole, the
// way the app's fence compares its snapshot identity — token
// value included (batch 2g round 2 on the app side; batch 2h
// F1 made that clause unconditional there). The account fence
// used to compare only the lineage projection ({rev, sid}),
// so a session record that copied the adopted tombstone's
// rev/sid and carried a foreign refresh token passed it and
// the fragment token kept being served under a session it
// never belonged to (Astra batch2h diff review, blocker 2 —
// F1's account-side counterpart). Returns null when there is
// no persistent storage at all; otherwise { state: 'absent' },
// { state: 'unverifiable' }, or { state: 'record',
// refreshToken, rev, sid, binding } (binding null for the one
// shape that names none — see deriveSharedState).
export function verifiedSharedIdentity(persistentStorage) {
  if (!persistentStorage) return null;
  const derived = deriveSharedState(persistentStorage);
  if (derived.classification === 'absent') return { state: 'absent' };
  if (derived.classification === 'unverifiable') {
    return { state: 'unverifiable' };
  }
  return {
    state: 'record',
    refreshToken: derived.refreshToken,
    rev: derived.rev,
    sid: derived.sid,
    binding: derived.binding,
  };
}

// The binding the carrying side writes under (batch 2f): the
// VERIFIED derivation, so a shared state that cannot be read
// back stops the carry instead of being named 'none' here and
// refused at the destination anyway. Returns undefined when
// there is no persistent storage at all (no shared state
// exists that could move, so no binding is needed or
// possible), null when the state is unverifiable, else the
// binding.
function currentCarryBinding(persistentStorage) {
  if (!persistentStorage) return undefined;
  return verifiedSharedCarryBinding(persistentStorage);
}

export function bindingsEqual(a, b) {
  if (a === 'none' || b === 'none') return a === b;
  if (!a || !b) return a === b;
  if (a.sid || b.sid) return Boolean(a.sid) && a.sid === b.sid;
  if (a.rev || b.rev) return Boolean(a.rev) && a.rev === b.rev;
  return false;
}

// Record the binding the next carried links are written under.
// The expectation is stored IN the handoff record, under the
// pending nonce it belongs to (batch 2f): the rewrite is a
// single write of the whole record, so the expectation can
// only ever speak for the nonce it was recorded with, and a
// failed write leaves the previous expectation untouched.
// Returns false when the expectation could not be recorded —
// including when there is no pending nonce to associate with,
// or the pending state cannot be verified: a carry that
// cannot bind its expectation must not write token-bearing
// hrefs at all (fail closed).
export function armCarryBinding(storage, binding) {
  const store = storage ?? defaultStorage();
  if (!store) return binding === null;
  const state = readHandoffState(store);
  if (state.state === 'invalid') return false;
  if (state.state === 'absent') return binding === null;
  return writeHandoffState(store, state.nonce, binding);
}

// The carry expectation's state FOR A GIVEN pending nonce
// (batch 2e round 4, R1; derived from the single record since
// batch 2f), strictly:
// - 'record': a binding ({sid} / {rev} / 'none') recorded
//   with THIS nonce;
// - 'absent': nothing recorded for this nonce — no state at
//   all, a state under a different nonce, or a record whose
//   expectation is empty;
// - 'invalid': state is present but cannot be trusted.
// 'invalid' is NOT evidence of an external handoff (round 3
// collapsed it into exactly that): unverifiable metadata must
// refuse the adoption, never downgrade it to unchecked.
export function readCarryBindingState(storage, pendingNonce) {
  const store = storage ?? defaultStorage();
  if (!store) return { state: 'absent' };
  const state = readHandoffState(store);
  if (state.state === 'invalid') return { state: 'invalid' };
  if (state.state === 'absent') return { state: 'absent' };
  if (state.nonce !== pendingNonce) return { state: 'absent' };
  if (state.carry === null) return { state: 'absent' };
  return { state: 'record', binding: state.carry };
}

// The binding recorded for the pending nonce, or null when no
// applicable (valid, associated) expectation exists.
export function readCarryBinding(storage) {
  const store = storage ?? defaultStorage();
  if (!store) return null;
  const state = readHandoffState(store);
  return state.state === 'record' ? state.carry : null;
}

function defaultPersistentStorage() {
  try { return globalThis.localStorage ?? null; } catch { return null; }
}

// The lineage projection of a shared-state identity (batch
// 2i; the tri-state reader this replaces projected the same
// shape from verifiedSharedCarryBinding since the batch 2f
// additional round): the sid, or the revision for a legacy
// record without one — exactly the shape takeFragmentHandoff
// snapshots. Confirmed absence and no storage at all project
// to a null lineage. A state that cannot be classified, or a
// record naming no binding, projects to undefined: the fence
// treats both as unreadable and retires, exactly as adoption
// refuses them — a record naming no binding must not collapse
// into the same null lineage as confirmed absence, behind
// which a token bound to that emptiness was once served
// indefinitely (batch 2f round 3).
function lineageOfIdentity(identity) {
  if (identity === null || identity.state === 'absent') return null;
  if (identity.state !== 'record' || !identity.binding) return undefined;
  return identity.binding.sid
    ? { rev: null, sid: identity.binding.sid }
    : { rev: identity.binding.rev, sid: null };
}

function lineageSuperseded(binding, current) {
  if (!binding && !current) return false;
  if (!binding || !current) return true;
  if (binding.sid && current.sid) return binding.sid !== current.sid;
  return binding.rev !== current.rev;
}

// The fragment session for one account page: the captured
// token plus its lineage binding. getToken serves the token
// only while its lineage is still the shared one; withToken
// adds the app's dispatch discipline (batch 2d round 5) —
// fence, selection and dispatch in ONE synchronous block, so
// no promise boundary can carry a stale token into a request.
export function createFragmentSession({
  location, history, storage, persistentStorage,
} = {}) {
  const persistent = persistentStorage ?? defaultPersistentStorage();
  ensureFragmentHandoff(storage);
  // The handoff validates its binding claim against the carry
  // record and the current shared state inside
  // takeFragmentHandoff (batch 2e round 3, R1): a token that
  // arrives here is either an external handoff (no claim) or a
  // carry whose binding still describes the shared state. The
  // lineage fence below then retires it the moment the shared
  // state moves on.
  const handoff = takeFragmentHandoff(location, history, storage, persistent);
  const token = handoff ? handoff.token : null;
  // For a carried handoff, bind to the lineage snapshot that
  // takeFragmentHandoff VERIFIED (batch 2e round 4, R1): an
  // independent re-read here could observe a state the claim
  // was never validated against and bind the token to it.
  // External handoffs bind through the fence's own reader:
  // the snapshot and every later check are then the SAME
  // verified derivation, so the snapshot can never be a value
  // that derivation cannot produce — the collapsing reader's
  // empty lineage {rev: null, sid: null}, behind which a later
  // real lineage compared equal on the revision fallback and
  // the token was never retired. A snapshot that cannot be
  // verified binds the token to nothing servable: it retires
  // at the first check, exactly as if the state had moved.
  //
  // Since batch 2i the binding snapshot is the state's whole
  // IDENTITY — refresh token, revision and lineage from the
  // one derivation — not the lineage alone (Astra batch2h
  // diff review, blocker 2): the lineage comparison cannot
  // see a session record that copies the captured rev/sid
  // and carries a foreign refresh token, which is precisely
  // the substitution the app side's unconditional token
  // clause closed in batch 2h (F1). For a carried handoff
  // the identity is the one takeFragmentHandoff verified in
  // the same derivation that validated the claim; otherwise
  // it is read once here, synchronously, in the same block
  // as the adoption.
  const capture = handoff && handoff.identity !== undefined
    ? handoff.identity
    : verifiedSharedIdentity(persistent);
  const binding = handoff && handoff.lineage !== undefined
    ? handoff.lineage
    : lineageOfIdentity(capture);
  const current = () => {
    if (!token) return null;
    if (binding === undefined) return null;
    if (!persistent) return token;
    // The fence is fail-closed about readability too (batch
    // 2f round 3): a shared state that cannot be read back
    // right now is unverifiable, not "still absent".
    const now = verifiedSharedIdentity(persistent);
    if (now === null || now.state === 'unverifiable') return null;
    // A record appearing where none was captured, or an
    // established record disappearing, retires the capture
    // (batch 2i): appearance is a new lineage and
    // disappearance is invalidation. The classification
    // comparison also sees the shapes the lineage projection
    // cannot — a record naming no binding projects to the
    // same null lineage as confirmed absence.
    if (capture && capture.state !== now.state) return null;
    const nowLineage = lineageOfIdentity(now);
    if (nowLineage === undefined) return null;
    if (lineageSuperseded(binding, nowLineage)) return null;
    if (capture && capture.state === 'record' && now.state === 'record') {
      if (capture.refreshToken === null) {
        // Bound to a tombstone (batch 2i): the shared state
        // must still be EXACTLY that tombstone — same
        // revision, same lineage, still no session record.
        // A tombstone does not rotate, so no tolerance
        // applies: any session record standing where it
        // stood retires the token, even one copying the
        // tombstone's rev/sid verbatim — the app side's
        // unconditional token clause (batch 2h, F1) in the
        // account fence's terms.
        if (now.refreshToken !== null
            || now.rev !== capture.rev
            || now.sid !== capture.sid) return null;
      } else if (now.rev === capture.rev
          && now.refreshToken !== capture.refreshToken) {
        // Same attested revision, different token (batch
        // 2i): the record was replaced in place under a
        // copied revision. A genuine rotation moves the
        // revision together with the token in one commit
        // and keeps the lineage, so it never trips this
        // clause; the copied-revision substitution is
        // exactly what does.
        return null;
      }
    }
    return token;
  };
  return Object.freeze({
    getToken: async () => current(),
    // The synchronous fence for surfaces that cannot cross a
    // promise boundary before acting (the carried-link click
    // handler): same check, no await.
    getTokenSync: () => current(),
    withToken: (dispatch) => dispatch(current()),
  });
}

export function createTokenProvider(location, history, storage, persistentStorage) {
  const session = createFragmentSession({
    location, history, storage, persistentStorage,
  });
  return session.getToken;
}

// Carrying the session across the account pages' own navigation.
// The token lives only in this closure, so a plain link from one
// account page to another (a full document load) used to drop it
// and the destination showed its login guidance despite the user
// being signed in. The carry stays inside the state-bound handoff
// design above: account-page links get the token in their URL
// fragment TOGETHER WITH the pending handoff nonce as state, and
// the destination adopts it only when that state matches its own
// armed nonce (the same tab session, so it does). A link is NEVER
// rewritten with a bare token: without a state to bind, the href
// is left exactly as it was. A fragment is never sent to the
// server, and the destination strips it from the URL on arrival.
// Only /account paths are rewritten: those are the pages that
// consume a fragment token, and nowhere else gets the token in
// a URL.
export function hrefWithFragmentToken(href, token, state, binding) {
  if (!token || !state || typeof href !== 'string') return href;
  if (!href.startsWith('/account')) return href;
  const base = href.split('#')[0];
  // The binding claim (batch 2e round 3, R1): the lineage, the
  // revision for a legacy record, or the explicit marker that
  // no shared record existed when the link was written. No
  // binding (no persistent storage) means no claim.
  let claim = '';
  if (binding === 'none') claim = '&lstate=none';
  else if (binding?.sid) claim = `&lineage=${encodeURIComponent(binding.sid)}`;
  else if (binding?.rev) claim = `&lrev=${encodeURIComponent(binding.rev)}`;
  return `${base}#access_token=${encodeURIComponent(token)}&state=${encodeURIComponent(state)}${claim}`;
}

// Remove carried tokens from account anchors (batch 2e round
// 2, R1): when the session is gone or superseded, token-bearing
// hrefs written earlier must not survive to be followed — a
// click would otherwise carry a dead account's token into the
// destination and have it adopted under the current lineage.
export function clearCarriedTokens(document) {
  if (!document?.querySelectorAll) return 0;
  let cleared = 0;
  for (const anchor of document.querySelectorAll('a[href^="/account"]')) {
    const current = anchor.getAttribute('href');
    if (typeof current === 'string' && current.includes('#access_token=')) {
      anchor.setAttribute('href', current.split('#')[0]);
      cleared += 1;
    }
  }
  return cleared;
}

// Keep carried links usable across document restoration (delta
// review R4). The initial carry rewrites anchors once, at page
// initialization — but when the browser restores THIS document
// from its back/forward cache, initialization does not run
// again, and by then the nonce baked into the anchors has been
// consumed by the destination page: following the link again
// would arrive with a dead state and show login guidance. Two
// refresh points fix that without weakening single-use:
//   - a pageshow with persisted=true (the restoration signal)
//     re-runs the carry, rewriting every anchor with the
//     CURRENT pending nonce (a fresh one is armed if the old
//     was consumed — the destination arms/matches in the same
//     tab session, so the new state binds exactly like the
//     first carry did);
//   - a capture-phase click on an account anchor rewrites THAT
//     anchor just before it is followed, using the token this
//     installer already resolved and the current pending nonce.
// A consumed nonce is never replayed: every refresh reads the
// pending nonce fresh from storage via carryTokenToAccountLinks
// / ensureFragmentHandoff.
export function installCarriedLinkRefresh({
  target, document, tokenProvider, storage, session, persistentStorage,
} = {}) {
  if (!document?.querySelectorAll || typeof tokenProvider !== 'function') return null;
  const persistent = persistentStorage ?? defaultPersistentStorage();
  // The click handler cannot await: when the caller supplies
  // the fragment session, the token is re-checked through its
  // SYNCHRONOUS fence at click time (batch 2e round 2, R1).
  // The cached token from the last refresh is only a fallback
  // for injected providers without a session, and it is
  // discarded the moment any check finds the session gone.
  const syncPeek = session && typeof session.getTokenSync === 'function'
    ? () => session.getTokenSync() : null;
  let cachedToken = null;
  const invalidate = () => {
    cachedToken = null;
    // The nonce dies with its carry expectation (batch 2e
    // round 4, R1): a fragment retained from before the
    // invalidation must not stay presentable afterwards. Since
    // batch 2f the two are ONE record; consumption removes the
    // keys in the ordered, per-key-verified sequence documented
    // at removeHandoffState (batch 2i), so no failure ordering
    // can leave a claim-strippable nonce armed without its
    // expectation — an unconfirmed removal stops the sequence
    // with the nonce still bound (by the record, or by the
    // surviving pair), and the worst case is an orphaned
    // expectation, which reads as invalid and binds nothing.
    // A legacy pair is migrated into the record before anything
    // is deleted, and an unverifiable state is not deleted at
    // all (batch 2f round 2): when migration cannot land the
    // state is simply left in place.
    const store = storage ?? defaultStorage();
    if (store) {
      const state = readHandoffState(store);
      if (state.state === 'record'
          && (!state.legacy
              || writeHandoffState(store, state.nonce, state.carry))) {
        removeHandoffState(store);
      }
    }
    clearCarriedTokens(document);
  };
  const refresh = async () => {
    const token = syncPeek ? syncPeek() : await tokenProvider();
    if (!token) {
      invalidate();
      return 0;
    }
    cachedToken = token;
    return carryTokenToAccountLinks(document, token, storage, persistent);
  };
  if (typeof target?.addEventListener === 'function') {
    target.addEventListener('pageshow', (event) => {
      if (event?.persisted) void refresh();
    });
  }
  if (typeof document.addEventListener === 'function') {
    document.addEventListener('click', (event) => {
      const anchor = typeof event?.target?.closest === 'function'
        ? event.target.closest('a[href^="/account"]') : null;
      if (!anchor) return;
      const token = syncPeek ? syncPeek() : cachedToken;
      if (!token) {
        // The session is gone or superseded: no carry, and any
        // token an earlier carry baked into the anchors is
        // stripped rather than followed into the destination.
        invalidate();
        return;
      }
      cachedToken = token;
      const state = ensureFragmentHandoff(storage);
      if (!state) return;
      // Re-bind the carry to the CURRENT shared state and name
      // it in the href (batch 2e round 3, R1): the destination
      // accepts only if the binding still holds when it loads.
      // The binding is the VERIFIED derivation (batch 2f): an
      // unverifiable shared state stops the carry here.
      const verified = currentCarryBinding(persistent);
      if (verified === null) {
        invalidate();
        return;
      }
      const binding = verified === undefined ? null : verified;
      if (binding !== null && !armCarryBinding(storage, binding)) {
        invalidate();
        return;
      }
      const current = anchor.getAttribute('href');
      const next = hrefWithFragmentToken(current, token, state, binding);
      if (next !== current) anchor.setAttribute('href', next);
    }, true);
  }
  return refresh;
}

export function carryTokenToAccountLinks(document, token, storage, persistentStorage) {
  if (!token || !document?.querySelectorAll) return 0;
  const state = ensureFragmentHandoff(storage);
  if (!state) return 0;
  // The carry records the binding it is written under in the
  // handoff record and names it in every href, so the
  // destination can refuse the token if the shared state has
  // moved by the time the link is followed (batch 2e rounds
  // 2-3, R1). The binding is the VERIFIED derivation (batch
  // 2f): when the shared state cannot be verified, or the
  // binding cannot be recorded, no tokenized href is written
  // at all (fail closed).
  const verified = currentCarryBinding(
    persistentStorage ?? defaultPersistentStorage());
  if (verified === null) return 0;
  const binding = verified === undefined ? null : verified;
  if (binding !== null && !armCarryBinding(storage, binding)) return 0;
  let rewritten = 0;
  for (const anchor of document.querySelectorAll('a[href^="/account"]')) {
    const current = anchor.getAttribute('href');
    const next = hrefWithFragmentToken(current, token, state, binding);
    if (next !== current) {
      anchor.setAttribute('href', next);
      rewritten += 1;
    }
  }
  return rewritten;
}
