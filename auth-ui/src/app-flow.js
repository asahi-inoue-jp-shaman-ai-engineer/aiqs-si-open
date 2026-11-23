// One-question flow state for the standalone app (UI redesign
// 2026-11-23, design: files/ui-mock-oneq/DESIGN-NOTES.md).
//
// The scan path is a single line of screens, one action each:
// target -> intention -> affirmation -> scan-confirm. Registering
// a new target is a two-step detour (target-photo -> target-name)
// that lands back on the line with the new target selected. This
// module owns the step order, the back-stack and the flow draft
// as pure state — no DOM — so the navigation rules and the
// "what will actually be submitted" resolution are testable
// without a harness.
//
// The wait-method step (foundation item 2, job-notify-resume)
// sits between the scan confirmation and the submission: the
// user picks "wait on this screen" or "notify by mail" EVERY
// time (owner decision 2026-11-23). The step is conditional —
// it is entered only while the session offers notify
// (session.notify.available); when notify cannot be offered,
// the flow has no such step in practice: the confirmation
// submits directly with mode "wait" and no screen or copy
// mentions mail (device-consistency rule 9: features that
// cannot be honoured stay out of the UI).

export const SCAN_STEPS = Object.freeze(
  ['target', 'intention', 'affirmation', 'scan-confirm', 'wait-method']);

// The affirmation the delegated path submits. The server
// contract requires the self_love_joy to be a concrete sentence
// (1..1000 chars, placeholders rejected; see
// aiqs_si_core_intake/prayer.py) — there is no empty or
// server-generated option. Asahi's decision (2026-11-23, plan A):
// in the app, both an explicit "おまかせにして次へ" and a blank
// field take this path and auto-fill the fixed sentence — the
// example sentence Asahi specified for this screen (DESIGN-
// NOTES, confirmed item 4). It is displayed on the affirmation
// screen as the example, and the confirmation screens show the
// actual sentence (never just the word "omakase"). Display and
// submission share this one constant so the two can never drift
// apart.
export const OMAKASE_AFFIRMATION = '私は、自己愛に満たされ、意図を達成しています。';

export function nextScanStep(step) {
  const index = SCAN_STEPS.indexOf(step);
  return index >= 0 && index < SCAN_STEPS.length - 1 ? SCAN_STEPS[index + 1] : null;
}

export function prevScanStep(step) {
  const index = SCAN_STEPS.indexOf(step);
  return index > 0 ? SCAN_STEPS[index - 1] : null;
}

// A minimal back-stack for the flow screens. go() pushes (never
// duplicating the current top), back() pops and returns the new
// current screen, reset() starts a fresh stack. Screens outside
// the flow (home, the target manager) can be stack entries too —
// the stack only remembers where "もどる" should land.
export function createNavigator(initial) {
  let stack = [initial];
  const current = () => stack[stack.length - 1];
  return Object.freeze({
    current,
    entries: () => [...stack],
    canBack: () => stack.length > 1,
    go(name) {
      if (name && name !== current()) stack = [...stack, name];
      return current();
    },
    back() {
      if (stack.length > 1) stack = stack.slice(0, -1);
      return current();
    },
    reset(name) {
      stack = [name];
      return current();
    },
  });
}

// The scan draft: everything the flow collects before the
// confirmation screen. affirmationMode distinguishes the user's
// own sentence ('custom') from the delegated example
// ('omakase'); the submitted value is always resolved through
// resolvedAffirmation so the confirmation screen can show the
// exact sentence that will be sent.
export function createScanDraft() {
  return {
    targetId: null,
    targetName: '',
    intention: '',
    affirmation: '',
    affirmationMode: 'custom',
  };
}

export function resolvedAffirmation(draft) {
  if (!draft) return '';
  if (draft.affirmationMode === 'omakase') return OMAKASE_AFFIRMATION;
  return typeof draft.affirmation === 'string' ? draft.affirmation : '';
}

// The first missing piece of the draft, in flow order, or null
// when the draft can be submitted. The confirmation screen uses
// this as its final gate; the per-screen handlers validate their
// own step before advancing, so a non-null result here means the
// flow was entered out of order.
export function draftProblem(draft) {
  if (!draft || !draft.targetId) return 'target';
  if (typeof draft.intention !== 'string' || !draft.intention.trim()) return 'intention';
  if (!resolvedAffirmation(draft).trim()) return 'affirmation';
  return null;
}
