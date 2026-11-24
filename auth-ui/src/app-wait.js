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

// The notify-wait screen's texts, derived ONLY from what the
// submission response itself reported (batch 2d, MEDIUM 7). The
// screen used to declare "照射は進んでいます" unconditionally —
// for a job still queued, already finished, failed, or whose
// notification had been suppressed — manufacturing a state the
// server never reported, which the device-consistency rule
// (UIは実機の実際の動きと完全に一致させる) forbids. Now:
//  - "進んでいます" is said only for state 'running'; a queued
//    job is 順番待ち, an unreported state claims nothing.
//  - The completion-mail clause follows the notification
//    lifecycle the server recorded: queued/sending/armed will
//    be mailed, sent was mailed, and anything else (suppressed,
//    expired, none) earns NO mail promise — the history route
//    is offered instead. A promise state additionally requires
//    the server's per-job delivery eligibility (combined
//    review finding 2): a wait-mode job recorded without an
//    address sits 'armed' forever — the escalation requires
//    one — and must not be promised a mail it cannot receive.
//  - A terminal state earns no notify-wait screen at all
//    (null): the caller routes to the ordinary result path,
//    whose poller surfaces the outcome immediately.
const NOTIFY_TERMINAL_STATES = new Set(['completed', 'failed', 'cancelled']);

export function notifyWaitTextFor({ operation, state, notification, deliverable } = {}) {
  if (NOTIFY_TERMINAL_STATES.has(state)) return null;
  // 'unknown' earns no notify-wait screen either, and neither
  // does a state the client does not recognize (Antigravity
  // glass audit, finding 3): the outcome is unconfirmed, and a
  // generic "accepted" title — possibly with a mail promise —
  // would state what the server has not reported. The caller
  // routes to the ordinary path, whose wording stays within
  // the record.
  if (state !== 'queued' && state !== 'running') return null;
  const irradiation = operation === 'irradiation';
  const label = irradiation ? '照射' : 'スキャン';
  let title;
  if (state === 'running') {
    title = `${label}は進んでいます。この画面は閉じて大丈夫です`;
  } else if (state === 'queued') {
    title = `${label}を受け付けました。順番待ちです`;
  } else {
    title = `${label}を受け付けました`;
  }
  const destination = irradiation ? '稼働証明' : '結果';
  const promised = deliverable === true
    && (notification === 'queued' || notification === 'sending'
      || notification === 'armed');
  let mailClause;
  if (promised) {
    mailClause = `${label}が終わったら、メールでお知らせします。`
      + `メールのリンクから、${destination}へそのまま進めます。`;
  } else if (notification === 'sent') {
    mailClause = `完了メールを送信しました。`
      + `メールのリンクから、${destination}へそのまま進めます。`;
  } else if (irradiation) {
    mailClause = '終わったあとは、履歴から稼働証明を確認できます。';
  } else {
    mailClause = '結果は履歴から確認できます。';
  }
  const text = state === 'running'
    ? mailClause
    : `この画面は閉じて大丈夫です。${mailClause}`;
  const statusText = promised
    ? `${label}を受け付けました。完了はメールでお知らせします。`
    : `${label}を受け付けました。`;
  return { title, text, statusText };
}

// The closing note on the ordinary wait / progress screens
// (batch 2d, item 7 round 2): whether a completion mail may be
// promised follows the job's RECORDED notification lifecycle
// when the response carries one — not merely whether the
// session could mail in general. A replayed submission for a
// job whose notification was already suppressed (viewed) or
// never recorded must not promise a mail the server has
// already decided not to send; a wait-mode job's armed record
// promises one (the escalation), as does a queued/sending
// notify record — but only while the record is deliverable:
// the server marks a record deliverable only when delivery
// is configured AND the record carries an address and has
// not been viewed (combined review finding 2, round 2). A
// response with no completion record at all promises
// nothing: the promise lives in the record, never in a
// session-level guess.
export function waitMailNoteFor({ operation, completion, state } = {}) {
  const irradiation = operation === 'irradiation';
  const notification = completion?.notification;
  // A recorded promise state earns the mail wording only when
  // the server says THIS job's mail can actually be delivered
  // (combined review finding 2): a wait-mode job recorded
  // without an address sits 'armed' forever — the escalation
  // requires one — and the client must not promise that mail,
  // however available mailing is in general.
  const promised = (notification === 'armed' || notification === 'queued'
    || notification === 'sending') && completion?.deliverable === true;
  let mail;
  if (promised) {
    mail = 'will';
  } else if (notification === 'sent') {
    mail = 'sent';
  } else {
    // Anything else — a non-promise lifecycle state, or NO
    // recorded notification in the response at all (the
    // server's degraded view of an unreadable job carries
    // none): no mail is promised. A promise exists only as a
    // recorded, deliverable notification; the earlier
    // session-level fallback could resurrect a promise the
    // record never made (combined review round 2, F2).
    mail = 'none';
  }
  // A terminal response has nothing still running (round 3):
  // a replayed submission can return a finished job, and the
  // note must not claim the execution continues while the
  // screen catches up to the outcome.
  if (state === 'completed' || state === 'failed' || state === 'cancelled') {
    if (irradiation) {
      if (mail === 'will') return '照射は終わりました。結果はメールでもお知らせします。';
      if (mail === 'sent') return '照射は終わりました。完了メールは送信済みです。';
      return '照射は終わりました。結果は履歴から確認できます。';
    }
    if (mail === 'will') return 'スキャンは終わりました。結果はメールでもお知らせします。';
    if (mail === 'sent') return 'スキャンは終わりました。完了メールは送信済みです。';
    return 'スキャンは終わりました。結果は履歴から確認できます。';
  }
  // Queued: nothing is executing yet (combined review
  // finding 1) — the note must not borrow the running state's
  // continuation wording. Closing the screen is still safe:
  // the job starts when its turn comes.
  if (state === 'queued') {
    const startClause = irradiation
      ? 'まだ順番待ちです。画面を閉じても、順番が来たら照射が始まります。'
      : 'まだ順番待ちです。画面を閉じても、順番が来たらスキャンが始まります。';
    if (mail === 'will') return `${startClause}終わったらメールでお知らせします。`;
    if (mail === 'sent') return `${startClause}完了メールは送信済みです。`;
    return irradiation
      ? `${startClause}終わったあとは、履歴から稼働証明を確認できます。`
      : `${startClause}結果は履歴から確認できます。`;
  }
  // Unknown: the outcome is being confirmed (combined review
  // finding 1). Nothing may claim the execution continues,
  // and no mail is promised — the history route is the only
  // statement the server can stand behind.
  if (state === 'unknown') {
    return irradiation
      ? '照射の結果を確認しています。結果は履歴から確認できます。'
      : 'スキャンの結果を確認しています。結果は履歴から確認できます。';
  }
  if (irradiation) {
    if (mail === 'will') {
      return '画面を閉じても、照射はそのまま進みます。終わったらメールでお知らせします。';
    }
    if (mail === 'sent') {
      return '画面を閉じても、照射はそのまま続きます。完了メールは送信済みです。';
    }
    return '画面を閉じても、照射はそのまま続きます。終わったあとは、履歴から稼働証明を確認できます。';
  }
  if (mail === 'will') {
    return '画面を閉じても大丈夫です。その場合は、終わったらメールでお知らせします。';
  }
  if (mail === 'sent') {
    return '画面を閉じても大丈夫です。完了メールは送信済みです。';
  }
  return '画面を閉じても、処理はそのまま続きます。結果は履歴から確認できます。';
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
