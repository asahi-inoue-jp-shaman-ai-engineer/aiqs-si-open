// Live-status texts for the waiting, progress and proof
// screens. Pure functions so the exact strings are testable
// without a DOM.
//
// The 2026-11-22 live check found the scan waiting screen's text
// was only the initial markup ("スキャンを受け付けました" /
// "順番に処理されます。そのままお待ちください。" / the orb):
// the elapsed clock wrote its first text on the first 1-second
// interval tick (it cleared the element on entry), and the stage
// sentence arrived only with the first poll (scheduled 800ms
// after entry, then every 3s). Nothing rendered synchronously on
// entry, so a capture — or a user — in that window saw neither.
// The screens now render these texts synchronously on entry and
// keep updating them from the same helpers.

// The elapsed clock measures how long THIS screen has been
// open, not how long the job has been running (the job's start
// is the server's business, and a reopened screen restarts the
// count at zero). The wording says so outright — a bare
// "経過 ○ 秒" invites the reader to mistake it for the job's
// own elapsed time (実機一致の鉄則2, 2026-11-23 audit).
export function elapsedText(seconds) {
  return `この画面を開いてから ${seconds} 秒`;
}

export function stageTextFor(view, kindLabel) {
  if (view?.state === 'queued') {
    const position = view.queue?.position;
    return position
      ? `${kindLabel}は順番待ちです（${position}番目）。順番に処理されます。そのままお待ちください。`
      : `${kindLabel}は順番待ちです。順番に処理されます。そのままお待ちください。`;
  }
  if (view?.state === 'unknown') return '状態を確認しています。そのままお待ちください。';
  // Terminal states are worded as what they are. The scan entry
  // render uses this with the submission response's own state
  // (which may already be running — or even terminal on a
  // replayed submission), and the irradiation progress screen
  // renders the terminal poll payload through it before the
  // proof screen takes over. Neither may borrow the running
  // sentence.
  if (view?.state === 'completed') return `${kindLabel}が完了しました。`;
  if (view?.state === 'failed') return `${kindLabel}が完了しませんでした。`;
  if (view?.state === 'cancelled') return `${kindLabel}は停止済みです。`;
  return `${kindLabel}を実行しています。そのままお待ちください。`;
}

// The irradiation progress bar alone is not a readable status:
// the completed/total counts ride along as text at all times,
// including while the job is still queued. A count the server
// has not confirmed yet renders as "—", never as a number: the
// progress screen's entry render runs before the first poll, and
// printing "0" there would state a server fact nobody has
// reported (device-consistency rule 7, 2026-11-23 audit). Once a
// poll lands, done is the response's own completed_count —
// including a genuine 0 for a job that has completed nothing.
export function progressCountText(done, total) {
  const doneText = done === null || done === undefined ? '—' : String(done);
  const totalText = total === null || total === undefined ? '—' : String(total);
  return `${doneText} / ${totalText} 回`;
}

// Headline for the proof screen. The proof screen doubles as the
// detail view for jobs in every state (opened from history), so
// only a completed job earns a completion headline — mirroring
// the step indicator's rule in app-steps.js.
export function proofHeadline(operation, state) {
  if (state !== 'completed') return null;
  if (operation === 'irradiation') return '照射が完了しました';
  if (operation === 'scan') return 'スキャンが完了しました';
  return null;
}

// The ranking note states how many entries are actually on
// screen; the server may return fewer than the cap (the live
// check saw 8 entries under a note claiming 10).
export function rankingCountText(count) {
  return `共鳴ランキングの順に、${count}件を表示しています。`;
}

// The ranking's default display is the top 10 entries
// (2026-11-23 decision): the screen renders at most this many,
// strongest resonance first (the payload arrives rank-sorted),
// and the note above states the number actually shown. The
// result copy keeps the full entry list — only the on-screen
// display is capped.
export const RANKING_DISPLAY_LIMIT = 10;

export function displayedRankingEntries(entries) {
  return (Array.isArray(entries) ? entries : []).slice(0, RANKING_DISPLAY_LIMIT);
}
