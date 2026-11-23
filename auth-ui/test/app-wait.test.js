import test from 'node:test';
import assert from 'node:assert/strict';
import {
  RANKING_DISPLAY_LIMIT, displayedRankingEntries, elapsedText,
  proofHeadline, progressCountText, rankingCountText, stageTextFor,
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
