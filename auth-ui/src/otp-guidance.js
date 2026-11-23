// Guidance lines for the OTP code step of the unified entry,
// derived ONLY from the controller's otp snapshot
// ({ attemptsLeft, sendsLeft, resendWaitMs }) — the page-level
// send/attempt accounting the existing-entry controller owns
// (budgets in existing-entry.js; the snapshot is a getter that
// recomputes from the shared send ledger on every read). The
// view renders these lines verbatim and never invents a number
// the snapshot did not carry: a field that is missing or not a
// finite number drops its line instead of being guessed, and a
// null snapshot yields no lines at all.
export function otpGuidanceLines(otp) {
  if (!otp || typeof otp !== 'object') return [];
  const lines = [];
  const { attemptsLeft, sendsLeft, resendWaitMs } = otp;
  if (Number.isFinite(attemptsLeft)) {
    lines.push(attemptsLeft > 0
      ? `このコードは、あと${attemptsLeft}回まで入力できます。`
      : 'このコードの入力回数の上限に達しました。');
  }
  if (Number.isFinite(sendsLeft)) {
    if (sendsLeft <= 0) {
      lines.push('この画面で送れるコードの上限に達しました。');
    } else if (Number.isFinite(resendWaitMs) && resendWaitMs > 0) {
      lines.push(`コードの再送は、あと約${Math.ceil(resendWaitMs / 1000)}秒待ってからできます。この画面では、あと${sendsLeft}回まで送れます。`);
    } else {
      lines.push(`コードが届かない場合は、再送できます。この画面では、あと${sendsLeft}回まで送れます。`);
    }
  }
  return lines;
}
