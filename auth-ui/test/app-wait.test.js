import test from 'node:test';
import assert from 'node:assert/strict';
import {
  RANKING_DISPLAY_LIMIT, displayedRankingEntries, elapsedText,
  notifyWaitTextFor, proofHeadline, progressCountText, rankingCountText,
  stageTextFor, waitMailNoteFor,
} from '../src/app-wait.js';

// Field finding (2026-11-22 live check): on the scan waiting
// screen neither the elapsed seconds nor a stage sentence was in
// the screen text — both rendered only on a later timer/poll
// tick, so the entry window showed the bare initial markup. The
// helpers here back the fix: the texts exist from the first
// synchronous render on entry.

test('elapsed text exists from zero seconds, not from the first tick', () => {
  assert.equal(elapsedText(0), 'この画面を開いてから 0 秒');
  assert.equal(elapsedText(1), 'この画面を開いてから 1 秒');
  assert.equal(elapsedText(65), 'この画面を開いてから 65 秒');
});

test('stage text names the stage for queued, running and unknown jobs', () => {
  assert.match(stageTextFor({ state: 'queued', queue: { position: 3 } }, 'スキャン'),
    /順番待ちです（3番目）/);
  assert.match(stageTextFor({ state: 'queued' }, 'スキャン'), /順番待ちです/);
  assert.match(stageTextFor({ state: 'running' }, '照射'), /照射を実行しています/);
  assert.match(stageTextFor({ state: 'unknown' }, 'スキャン'), /状態を確認しています/);
  // The entry render uses a queued view before the first poll.
  assert.match(stageTextFor({ state: 'queued' }, 'スキャン'), /そのままお待ちください/);
});

test('stage text follows the state the job actually reports (LOW 6/7)', () => {
  // The scan submission response carries the job's real state:
  // already running, or terminal on a replayed submission. The
  // entry render must not manufacture "queued" for those.
  assert.match(stageTextFor({ state: 'running' }, 'スキャン'), /スキャンを実行しています/);
  // An unknown irradiation state is "checking status", never
  // "running" — the counts ride separately in progressCountText.
  assert.match(stageTextFor({ state: 'unknown' }, '照射'), /状態を確認しています/);
  assert.doesNotMatch(stageTextFor({ state: 'unknown' }, '照射'), /実行しています/);
});

test('stage text words terminal states as terminal (MEDIUM 4 progress render)', () => {
  // The progress screen renders the terminal poll payload
  // through this before the proof screen takes over.
  assert.equal(stageTextFor({ state: 'completed' }, '照射'), '照射が完了しました。');
  assert.equal(stageTextFor({ state: 'completed' }, 'スキャン'), 'スキャンが完了しました。');
  assert.equal(stageTextFor({ state: 'failed' }, '照射'), '照射が完了しませんでした。');
  assert.equal(stageTextFor({ state: 'cancelled' }, '照射'), '照射は停止済みです。');
});

test('progress count text always carries both numbers', () => {
  assert.equal(progressCountText(0, 33), '0 / 33 回');
  assert.equal(progressCountText(12, 33), '12 / 33 回');
  assert.equal(progressCountText(3, null), '3 / — 回');
});

test('progress count text never invents an unreported count (rule 7)', () => {
  // The progress screen's entry render runs before the first
  // poll: the completed side is unreported, so it renders as
  // "—" — a genuine 0 arrives only as a poll response's own
  // completed_count and renders as 0.
  assert.equal(progressCountText(null, 33), '— / 33 回');
  assert.equal(progressCountText(undefined, 99), '— / 99 回');
  assert.equal(progressCountText(undefined, undefined), '— / — 回');
});

test('proof headline claims completion only for completed jobs', () => {
  assert.equal(proofHeadline('irradiation', 'completed'), '照射が完了しました');
  assert.equal(proofHeadline('scan', 'completed'), 'スキャンが完了しました');
  for (const state of ['running', 'queued', 'failed', 'cancelled', 'unknown']) {
    assert.equal(proofHeadline('irradiation', state), null, state);
  }
  assert.equal(proofHeadline('reading', 'completed'), null);
  assert.equal(proofHeadline(null, null), null);
});

test('ranking display caps at the top 10 entries by default', () => {
  assert.equal(RANKING_DISPLAY_LIMIT, 10);
  const many = Array.from({ length: 14 }, (_, i) => ({ rank: i + 1 }));
  const shown = displayedRankingEntries(many);
  assert.equal(shown.length, 10);
  assert.equal(shown[0].rank, 1);
  assert.equal(shown[9].rank, 10);
  // Fewer than the cap renders as-is; a missing list renders empty.
  assert.equal(displayedRankingEntries(many.slice(0, 8)).length, 8);
  assert.deepEqual(displayedRankingEntries(null), []);
  assert.deepEqual(displayedRankingEntries(undefined), []);
});

test('ranking count text follows the actual number of entries shown', () => {
  // The field check saw 8 entries under a note claiming 10.
  assert.equal(rankingCountText(8), '共鳴ランキングの順に、8件を表示しています。');
  assert.equal(rankingCountText(10), '共鳴ランキングの順に、10件を表示しています。');
  assert.equal(rankingCountText(0), '共鳴ランキングの順に、0件を表示しています。');
});


test('notify-wait text says 進んでいます only for a running job (batch 2d MEDIUM 7)', () => {
  // The pre-fix screen declared 照射は進んでいます for every
  // state. A queued job is 順番待ち — the word 進んでいます must
  // not appear anywhere in its texts.
  const queued = notifyWaitTextFor(
    { operation: 'irradiation', state: 'queued', notification: 'queued' });
  assert.match(queued.title, /順番待ちです/);
  assert.doesNotMatch(queued.title + queued.text + queued.statusText, /進んでいます/);
  const running = notifyWaitTextFor(
    { operation: 'irradiation', state: 'running', notification: 'queued' });
  assert.match(running.title, /照射は進んでいます/);
  const scanQueued = notifyWaitTextFor(
    { operation: 'scan', state: 'queued', notification: 'queued' });
  assert.match(scanQueued.title, /スキャンを受け付けました。順番待ちです/);
  assert.doesNotMatch(scanQueued.title + scanQueued.text, /進んでいます/);
  // An unreported or unknown state earns no notify-wait
  // screen at all (Antigravity glass audit, finding 3): the
  // outcome is unconfirmed, and a generic accepted-title —
  // possibly with a mail promise — would state what the
  // server has not reported. The caller routes to the
  // ordinary path instead.
  assert.equal(notifyWaitTextFor(
    { operation: 'scan', state: undefined, notification: 'queued' }), null);
  assert.equal(notifyWaitTextFor(
    { operation: 'scan', state: 'unknown', notification: 'queued', deliverable: true }), null);
});

test('notify-wait text promises mail only while a mail will actually be sent', () => {
  for (const notification of ['queued', 'sending', 'armed']) {
    const texts = notifyWaitTextFor(
      { operation: 'scan', state: 'running', notification, deliverable: true });
    assert.match(texts.text, /メールでお知らせします/, notification);
  }
  const sent = notifyWaitTextFor(
    { operation: 'irradiation', state: 'running', notification: 'sent' });
  assert.match(sent.text, /完了メールを送信しました/);
  assert.doesNotMatch(sent.text, /メールでお知らせします/);
  // Suppressed / expired / none: no mail promise anywhere; the
  // history route is offered instead.
  for (const notification of ['suppressed', 'expired', 'none', undefined]) {
    const texts = notifyWaitTextFor(
      { operation: 'irradiation', state: 'queued', notification });
    assert.doesNotMatch(texts.text + texts.statusText, /メール/, String(notification));
    assert.match(texts.text, /履歴から稼働証明を確認できます/, String(notification));
  }
});

test('notify-wait text refuses terminal states (the result path owns them)', () => {
  for (const state of ['completed', 'failed', 'cancelled']) {
    assert.equal(notifyWaitTextFor(
      { operation: 'scan', state, notification: 'sent' }), null, state);
    assert.equal(notifyWaitTextFor(
      { operation: 'irradiation', state, notification: 'queued' }), null, state);
  }
});


test('waitMailNoteFor: the recorded notification lifecycle decides the mail promise (round 2)', () => {
  // A notify-mode job's queued record promises the mail even
  // when the session flag is off in this response's context.
  assert.match(waitMailNoteFor({
    operation: 'scan',
    completion: { mode: 'notify', notification: 'queued', deliverable: true },
    notifyAvailable: false,
  }), /メールでお知らせします/);
  // A wait-mode job's armed record promises the escalation mail.
  assert.match(waitMailNoteFor({
    operation: 'irradiation',
    completion: { mode: 'wait', notification: 'armed', deliverable: true },
    notifyAvailable: false,
  }), /メールでお知らせします/);
  // The replay defect: a suppressed record (already viewed)
  // must NOT promise a mail, however available mailing is.
  const suppressed = waitMailNoteFor({
    operation: 'scan', completion: { mode: 'notify', notification: 'suppressed' },
    notifyAvailable: true,
  });
  assert.doesNotMatch(suppressed, /お知らせします/);
  assert.match(suppressed, /履歴から確認できます/);
  // An already-sent mail is reported as sent, not promised.
  assert.match(waitMailNoteFor({
    operation: 'scan', completion: { mode: 'notify', notification: 'sent' },
    notifyAvailable: true,
  }), /送信済み/);
  // No completion record at all: NO promise (combined review
  // round 2, F2). A promise exists only as a recorded,
  // deliverable notification — the session flag no longer
  // resurrects one (a degraded view carries no record, and
  // the note recomputed from it must not start promising).
  const noRecord = waitMailNoteFor({
    operation: 'scan', completion: undefined, notifyAvailable: true,
  });
  assert.doesNotMatch(noRecord, /お知らせします/);
  assert.match(noRecord, /履歴から確認できます/);
  assert.match(waitMailNoteFor({
    operation: 'irradiation', completion: undefined, notifyAvailable: false,
  }), /履歴から稼働証明を確認できます/);
  // A completion object without a notification (the degraded
  // shape) promises nothing either, whatever its other keys.
  assert.doesNotMatch(waitMailNoteFor({
    operation: 'scan', completion: { deliverable: false },
    notifyAvailable: true,
  }), /お知らせします/);
});

test('waitMailNoteFor: a terminal state never claims execution continues (round 3)', () => {
  // Astra's exact shape: a failed, suppressed irradiation
  // replay — the note said the irradiation "keeps going".
  const failed = waitMailNoteFor({
    operation: 'irradiation', state: 'failed',
    completion: { mode: 'notify', notification: 'suppressed' },
    notifyAvailable: true,
  });
  assert.match(failed, /照射は終わりました/);
  assert.doesNotMatch(failed, /進みます/);
  // A completed scan whose mail is queued says it finished,
  // and still promises the mail truthfully.
  const completed = waitMailNoteFor({
    operation: 'scan', state: 'completed',
    completion: { mode: 'notify', notification: 'queued', deliverable: true },
    notifyAvailable: false,
  });
  assert.match(completed, /スキャンは終わりました/);
  assert.match(completed, /メールでもお知らせします/);
  // Non-terminal states keep the continuation wording.
  assert.match(waitMailNoteFor({
    operation: 'irradiation', state: 'running',
    completion: { mode: 'wait', notification: 'armed', deliverable: true },
    notifyAvailable: true,
  }), /進みます/);
});


test('waitMailNoteFor: a queued job is not said to be executing (combined review finding 1)', () => {
  // Queued irradiation with a deliverable mail: the note says
  // it is waiting for its turn, and still promises the mail.
  const queued = waitMailNoteFor({
    operation: 'irradiation', state: 'queued',
    completion: { mode: 'notify', notification: 'queued', deliverable: true },
    notifyAvailable: true,
  });
  assert.match(queued, /まだ順番待ちです/);
  assert.match(queued, /順番が来たら照射が始まります/);
  assert.match(queued, /メールでお知らせします/);
  assert.doesNotMatch(queued, /そのまま進みます|そのまま続きます/);
  // Queued scan without a deliverable record: queue wording,
  // history route, no continuation claim.
  const queuedScan = waitMailNoteFor({
    operation: 'scan', state: 'queued',
    completion: { mode: 'wait', notification: 'none', deliverable: false },
    notifyAvailable: false,
  });
  assert.match(queuedScan, /まだ順番待ちです/);
  assert.match(queuedScan, /結果は履歴から確認できます/);
  assert.doesNotMatch(queuedScan, /そのまま続きます/);
});

test('waitMailNoteFor: an unknown outcome claims neither execution nor mail (finding 1)', () => {
  for (const operation of ['irradiation', 'scan']) {
    const note = waitMailNoteFor({
      operation, state: 'unknown',
      completion: { mode: 'notify', notification: 'queued', deliverable: true },
      notifyAvailable: true,
    });
    assert.match(note, /結果を確認しています/, operation);
    assert.match(note, /履歴から確認できます/, operation);
    assert.doesNotMatch(note, /お知らせします|進みます|続きます/, operation);
  }
});

test('an undeliverable promise state earns no mail promise (combined review finding 2)', () => {
  // The defect: a wait-mode job recorded WITHOUT an address
  // sits 'armed' forever (the escalation requires one), yet
  // the client promised its mail. deliverable=false must win
  // over the armed state in BOTH helpers.
  const note = waitMailNoteFor({
    operation: 'irradiation', state: 'running',
    completion: { mode: 'wait', notification: 'armed', deliverable: false },
    notifyAvailable: true,
  });
  assert.doesNotMatch(note, /お知らせします/);
  assert.match(note, /履歴から稼働証明を確認できます/);
  const texts = notifyWaitTextFor({
    operation: 'scan', state: 'running', notification: 'armed',
    deliverable: false,
  });
  assert.doesNotMatch(texts.text + texts.statusText, /メールでお知らせします/);
  assert.match(texts.text, /結果は履歴から確認できます/);
  // The same state WITH eligibility keeps the promise.
  const promised = notifyWaitTextFor({
    operation: 'scan', state: 'running', notification: 'armed',
    deliverable: true,
  });
  assert.match(promised.text, /メールでお知らせします/);
});
