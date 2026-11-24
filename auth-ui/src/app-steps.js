// Step indicator labels for the standalone app's main flow.
//
// The flow is 対象 (1) -> スキャンの入力 (2) -> スキャン中 (3) ->
// 結果 (4) -> 照射 (5). Since the one-question redesign
// (2026-11-23) step 2 is split into one screen per question —
// intention, affirmation and the scan confirmation — and step 1
// covers picking a target plus the two registration screens.
// The proof screen is the completion state only when the job
// actually completed the flow. The proof screen is also the
// detail view for running, queued, failed and cancelled jobs
// (opened from history), so its label depends on the job: a
// completed irradiation earns the completion label, anything
// else gets a neutral details label and never claims
// completion. Screens outside the main flow (login, home,
// account, history, guide, error) have no step label — the
// indicator is hidden there rather than showing a misleading
// position.
// The wait (scan) and progress (irradiation) labels likewise
// depend on the job: they derive from the recorded state the
// caller passes as context, and only a running job earns the
// '中' wording (batch 2e, R7).
// Keeping the mapping in a pure module makes it testable without
// a DOM.
const STEP_LABELS = {
  target: 'ステップ 1 / 5 — 対象',
  'target-photo': 'ステップ 1 / 5 — 対象の登録',
  'target-name': 'ステップ 1 / 5 — 対象の登録',
  targets: 'ステップ 1 / 5 — 対象の管理',
  intention: 'ステップ 2 / 5 — 意図',
  affirmation: 'ステップ 2 / 5 — 自己愛アファーメーション',
  'scan-confirm': 'ステップ 2 / 5 — 内容の確認',
  ranking: 'ステップ 4 / 5 — 結果',
  'reading-confirm': 'ステップ 4 / 5 — 結果（リーディングの確認）',
  'reading-wait': 'ステップ 4 / 5 — 結果（リーディングを送信中）',
  'reading-result': 'ステップ 4 / 5 — 結果（リーディング）',
  count: 'ステップ 5 / 5 — 照射の回数',
  confirm: 'ステップ 5 / 5 — 照射の確認',
};

// The wait and progress labels derive from the job's recorded
// state, exactly like the body text does (batch 2e, R7): the
// fixed 'スキャン中'/'照射中' labels claimed execution for jobs
// still queued, jobs whose outcome is unknown, and terminal
// replays. '中' is earned only by 'running'; a queued job is
// まち, an unknown one is 確認中, and anything else — terminal
// states and a missing context included — takes the neutral
// noun, which claims nothing about execution.
function stateStepLabel(step, noun, state) {
  if (state === 'running') return `${step} — ${noun}中`;
  if (state === 'queued') return `${step} — ${noun}まち`;
  if (state === 'unknown') return `${step} — ${noun}を確認中`;
  return `${step} — ${noun}`;
}

export function stepLabelFor(screen, context = null) {
  if (screen === 'wait') {
    return stateStepLabel('ステップ 3 / 5', 'スキャン', context?.state);
  }
  if (screen === 'progress') {
    return stateStepLabel('ステップ 5 / 5', '照射', context?.state);
  }
  if (screen === 'proof') {
    return context?.state === 'completed' && context?.operation !== 'scan'
      ? '完了 — 稼働証明' : '稼働証明 — 詳細';
  }
  // The wait-method choice and the notify-wait screens are
  // shared by both flows (foundation item 2), so their step
  // position comes from the flow context, like the proof
  // screen's: the scan choice closes the input step (2) and
  // its notify-wait stands where the scan wait stands (3);
  // both irradiation screens belong to the irradiation step
  // (5).
  if (screen === 'wait-method') {
    return context?.operation === 'irradiation'
      ? 'ステップ 5 / 5 — 待ち方' : 'ステップ 2 / 5 — 待ち方';
  }
  if (screen === 'notify-wait') {
    return context?.operation === 'irradiation'
      ? 'ステップ 5 / 5 — 通知まち' : 'ステップ 3 / 5 — 通知まち';
  }
  // The reading wait carries a phase, like the screens above
  // carry theirs: 'creating' is claimable only after the server
  // answered ACCEPTED (Astra gate #4); while the submission is
  // still in flight the step must say 送信中, not 作成中 — the
  // fixed label once asserted creation during the send phase
  // (Antigravity glass audit, finding 1).
  if (screen === 'reading-wait') {
    return context === 'creating'
      ? 'ステップ 4 / 5 — 結果（リーディングを作成中）'
      : 'ステップ 4 / 5 — 結果（リーディングを送信中）';
  }
  return STEP_LABELS[screen] || null;
}
