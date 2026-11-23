// Busy state for submit buttons.
//
// While a submit is in flight the button is disabled and its label
// changes to a "being accepted" text, so the user can see the tap
// landed and cannot double-submit. The returned restore function
// always brings back the original label and enabled state — the
// caller must run it on every outcome (success, handled error,
// exception), typically from a finally block. A second setBusy on
// an already-busy button keeps the first original label, so nested
// restores cannot strand a "…しています…" label on the button.
//
// The optional onRestore callback runs once, after the snapshot
// has been put back. Callers whose eligibility may have been
// recalculated while the submission was in flight (a refreshed
// confirmation can turn unaffordable) re-render the button from
// that current state there, so the fresh disabled state wins over
// the now-obsolete snapshot.
export function setBusy(button, busyText, onRestore) {
  if (!button) return () => {};
  if (button.dataset.busy === '1') return () => {};
  const originalText = button.textContent;
  const originalDisabled = Boolean(button.disabled);
  button.dataset.busy = '1';
  button.disabled = true;
  button.textContent = busyText;
  let restored = false;
  return () => {
    if (restored) return;
    restored = true;
    delete button.dataset.busy;
    button.disabled = originalDisabled;
    button.textContent = originalText;
    if (onRestore) onRestore();
  };
}
