// Pure state helpers for the standalone app's target list.
//
// The target list is the user's proof that a registration landed.
// A failed list fetch (503/429/500 JSON from the admission limits,
// a network error, an unparsable body) must therefore never be
// rendered as "no targets": the last known list stays on screen and
// the caller shows a failure notice instead. These helpers keep
// that decision out of the DOM code so it is testable on its own.

// Maps an app-client result for GET /api/app/targets onto the
// target-list state. Returns { targets, failed } on a definitive
// answer (success replaces the list, failure keeps the current
// one), or { login: true } when the session is gone.
export function targetsFromLoad(current, result) {
  const known = Array.isArray(current) ? current : [];
  if (result && result.kind === 'login') return { login: true, targets: known };
  if (result && result.kind === 'ok' && !result.data?.error
      && Array.isArray(result.data?.targets)) {
    return { targets: result.data.targets, failed: false };
  }
  return { targets: known, failed: true };
}

// The display record for a just-registered target, built from the
// register response ({"target_id": ...}) and the form values the
// user just submitted. Returns null when the response carries no
// usable target id — the caller then falls back to a plain reload.
export function registeredTarget(data, { name, hasPhoto2 = false } = {}) {
  if (!data || typeof data !== 'object') return null;
  const id = data.target_id || data.target?.id;
  if (typeof id !== 'string' || !id) return null;
  return {
    id,
    name: data.target?.name || name || '',
    has_photo: true,
    has_photo2: Boolean(data.target?.has_photo2 ?? hasPhoto2),
  };
}

// Reconciles a list response that predates a local mutation.
// loadTargets snapshots the cache (knownAtStart) and a generation
// counter before awaiting; when a registration, deletion or rename
// landed while the request was in flight, the response describes
// an older world and must not overwrite the newer local state:
// - a failure keeps the current cache untouched (and is a failure);
// - a success is merged with the mutations applied meanwhile —
//   rows deleted locally stay deleted, rows added locally stay
//   added, and locally renamed rows keep their new name.
export function reconcileStaleLoad(knownAtStart, current, result) {
  const known = Array.isArray(current) ? current : [];
  if (result && result.kind === 'login') return { login: true, targets: known };
  if (!result || result.kind !== 'ok' || result.data?.error
      || !Array.isArray(result.data?.targets)) {
    return { targets: known, failed: true };
  }
  const startIds = new Set(
    (Array.isArray(knownAtStart) ? knownAtStart : []).map((row) => row.id));
  const currentById = new Map(known.map((row) => [row.id, row]));
  const merged = [];
  const seen = new Set();
  for (const row of result.data.targets) {
    if (seen.has(row.id)) continue;
    seen.add(row.id);
    if (startIds.has(row.id) && !currentById.has(row.id)) continue; // deleted meanwhile
    merged.push(currentById.get(row.id) ?? row); // local rename wins
  }
  for (const row of known) {
    if (!seen.has(row.id)) merged.push(row); // added meanwhile
  }
  return { targets: merged, failed: false };
}

// Removes a target from the known list after a successful delete,
// so the failure-preserving load that may follow cannot bring the
// deleted row back.
export function withoutTarget(current, id) {
  const known = Array.isArray(current) ? current : [];
  return known.filter((row) => row.id !== id);
}

// Applies a successful rename to the known list, so a follow-up
// failed load keeps showing the new name instead of the cached one.
export function withRenamedTarget(current, id, name) {
  const known = Array.isArray(current) ? current : [];
  return known.map((row) => (row.id === id ? { ...row, name } : row));
}

// Adds a just-registered target to the known list (idempotently:
// a replayed registration keeps the existing row) so it stays
// visible and selectable even when the follow-up list fetch fails.
export function withRegisteredTarget(current, target) {
  const known = Array.isArray(current) ? current : [];
  if (!target) return known;
  if (known.some((row) => row.id === target.id)) return known;
  return [...known, target];
}
