import { createClient } from '@supabase/supabase-js';
import { createMemoryClient, createBrowserPendingStore } from './independent-entry.js';
import { createUnifiedEntry } from './unified-entry.js';
import { parseEntryRuntimeConfig } from './entry-runtime-config.js';
import { otpGuidanceLines } from './otp-guidance.js';

// Explicit authority template entry; never imported by the main application.
const root = document.getElementById('si-unified-entry');
if (root) {
  const element = id => document.getElementById(id);
  const status = element('si-existing-status');
  const nativeForm = element('si-unified-handoff');
  let entry, busy = false, choices = [], selected = null, guidanceTimer = null;
  const terminal = ['unsupported-factor', 'password-update-unknown', 'restart-required', 'signup-pending', 'disposed'];
  function clearInputs() { root.querySelectorAll('input').forEach(input => { input.value = ''; }); }
  function stopGuidanceTimer() {
    if (guidanceTimer) { clearInterval(guidanceTimer); guidanceTimer = null; }
  }
  // The OTP guidance (attempts left, sends left, resend wait) is
  // painted ONLY from the controller's otp snapshot, re-read on
  // every paint (otp-guidance.js). While a resend wait is running
  // out on the code step, a 1-second ticker re-paints from the
  // same getter — the snapshot recomputes the wait from the
  // ledger's real last-sent timestamp, so the countdown is the
  // value itself, never a locally decremented guess. The ticker
  // stops the moment the wait ends or the step is left.
  function paintOtpGuidance() {
    const lines = otpGuidanceLines(entry?.otp);
    const nodes = [];
    lines.forEach((line, index) => {
      if (index > 0) nodes.push(document.createElement('br'));
      nodes.push(document.createTextNode(line));
    });
    element('si-otp-guidance').replaceChildren(...nodes);
  }
  function renderOtpGuidance() {
    const active = entry?.mode === 'login' && entry?.state === 'otp-code';
    element('si-otp-guidance').hidden = !active;
    stopGuidanceTimer();
    if (!active) return;
    paintOtpGuidance();
    if ((entry.otp?.resendWaitMs ?? 0) > 0) {
      guidanceTimer = setInterval(() => {
        if (!entry || entry.mode !== 'login' || entry.state !== 'otp-code') { stopGuidanceTimer(); return; }
        paintOtpGuidance();
        if ((entry.otp?.resendWaitMs ?? 0) <= 0) stopGuidanceTimer();
      }, 1000);
    }
  }
  function render() {
    const state = entry?.state;
    element('si-otp-request').hidden = !(entry.mode === 'login' && (state === 'ready' || state === 'otp-sending'));
    element('si-otp-verify').hidden = state !== 'otp-code';
    renderOtpGuidance();
    element('si-recovery-request').hidden = state !== 'ready' || entry.mode !== 'recovery';
    element('si-entry-modes').hidden = !entry?.canSwitch;
    for (const mode of ['login', 'signup', 'recovery']) {
      element('si-show-' + mode).setAttribute('aria-pressed', String(entry?.mode === mode));
    }
    element('si-signup-form').hidden = state !== 'ready' || entry.mode !== 'signup';
    element('si-verify-form').hidden = state !== 'verification';
    element('si-retry-complete').hidden = state !== 'completion';
    element('si-recovery-verify').hidden = state !== 'recovery-code';
    element('si-totp-choice').hidden = state !== 'factor-choice';
    element('si-totp-verify').hidden = state !== 'totp-code';
    element('si-password-change').hidden = state !== 'password-ready';
    element('si-existing-restart').hidden = !terminal.includes(state);
    root.querySelectorAll('button, select').forEach(control => { control.disabled = busy; });
  }
  function message() {
    switch (entry.state) {
      case 'verification': return 'メールに届いた確認コードを、この画面に入力してください。';
      case 'completion': return '登録完了を確認できませんでした。「登録完了を再確認」で再試行してください。';
      case 'signup-pending': return '登録結果を確認できませんでした。接続元のAIからやり直してください。';
      case 'recovery-code': return 'メールに届いた確認コードを、この画面に入力してください。';
      case 'otp-sending': return '確認コードを送っています…';
      case 'otp-code': return 'メールに届いた6桁のコードを入力してください。届かない場合は、少し待ってから再送できます。';
      case 'factor-choice': return '利用する認証アプリを選んでください。';
      case 'totp-code': return '選んだ認証アプリに表示された6桁のコードを入力してください。';
      case 'password-ready': return '新しいパスワードを入力してください。';
      case 'unsupported-factor': return 'この確認方法は、この画面では利用できません。接続元のAIに戻ってください。';
      case 'password-update-unknown': return 'パスワードが変更されたか確認できませんでした。この画面から再送せず、接続元のAIからやり直してください。';
      case 'restart-required': return '認証を確認できませんでした。接続元のAIからやり直してください。';
      case 'finished': return '接続を続けています…';
      default: return '認証を確認できませんでした。入力内容を確認してください。';
    }
  }
  function showChoices(ids) {
    choices = [...ids];
    const select = element('si-totp-factor');
    select.replaceChildren(new Option('認証アプリを選択', ''));
    choices.forEach((id, index) => select.add(new Option(`認証アプリ ${index + 1}`, id)));
  }
  async function run(action) {
    if (busy || !entry || terminal.includes(entry.state) || entry.state === 'finished') return;
    busy = true; render(); status.textContent = '確認しています…';
    try {
      const result = await action();
      if (result?.kind === 'choose-totp') showChoices(result.factorIds);
      status.textContent = message();
    } catch {
      status.textContent = entry.state === 'factor-choice'
        ? '確認できませんでした。選んだ認証アプリで、もう一度確認を始めてください。'
        : entry.state === 'recovery-code' ? '確認コードを確認して、もう一度入力してください。'
        : entry.state === 'otp-code' ? 'コードが違うか、期限が切れています。確認してもう一度入力してください。'
        : entry.state === 'ready' && entry.mode === 'login'
          ? (entry.otp?.resendWaitMs > 0
            ? 'コードを送ったばかりです。少し待ってから、もう一度お試しください。'
            : 'うまくいきませんでした。メールアドレスを確認するか、新しいコードを送ってください。')
          : message();
    } finally {
      busy = false;
      if (terminal.includes(entry.state)) clearInputs();
      render();
    }
  }
  function bind(formId, action) {
    element(formId).addEventListener('submit', event => {
      event.preventDefault();
      if (!busy) action();
    });
  }
  try {
    const sdkConfig = parseEntryRuntimeConfig(root.dataset, location.origin);
    const destination = new URL(nativeForm.action);
    if (destination.origin !== location.origin || destination.username || destination.password ||
        nativeForm.method.toLowerCase() !== 'post') throw new Error();
    entry = createUnifiedEntry({
      createClient: () => createMemoryClient(createClient, sdkConfig), origin: location.origin,
      pendingStore: createBrowserPendingStore(),
      fetch: (url, options) => fetch(url, { ...options, signal: AbortSignal.timeout(10000) }),
      handoff: async token => {
        nativeForm.elements.namedItem('upstream_token').value = token;
        HTMLFormElement.prototype.submit.call(nativeForm);
        clearInputs();
      },
    });
    for (const mode of ['login', 'signup', 'recovery']) {
      element('si-show-' + mode).addEventListener('click', () => {
        if (busy || !entry.canSwitch) return;
        // Keep the server-generated hidden nonce intact until native submission.
        root.querySelectorAll('input:not([type=hidden])').forEach(input => { input.value = ''; });
        try { entry.selectMode(mode); render(); status.textContent = 'メールアドレスを入力してください。'; }
        catch { status.textContent = '接続元のAIからやり直してください。'; render(); }
      });
    }
    bind('si-signup-form', () => {
      const email = element('si-signup-email').value, password = element('si-signup-password').value;
      const referralCode = element('si-referral').value || null;
      element('si-signup-password').value = '';
      void run(() => entry.invoke('signup', { email, password, referralCode }));
    });
    bind('si-verify-form', () => {
      const code = element('si-code').value; element('si-code').value = '';
      void run(async () => { await entry.invoke('verify', code); return entry.invoke('complete'); });
    });
    element('si-retry-complete').addEventListener('click', () => { void run(() => entry.invoke('complete')); });
    bind('si-otp-request', () => {
      const email = element('si-otp-email').value;
      void run(() => entry.invoke('requestOtpLogin', email));
    });
    bind('si-otp-verify', () => {
      const code = element('si-otp-code').value; element('si-otp-code').value = '';
      void run(() => entry.invoke('verifyOtpLogin', code));
    });
    element('si-otp-resend').addEventListener('click', () => {
      if (busy) return;
      const wait = entry.otp?.resendWaitMs ?? 0;
      if (wait > 0) {
        status.textContent = `コードの再送は${Math.ceil(wait / 1000)}秒ほど待ってから押してください。`;
        return;
      }
      void run(() => entry.invoke('resendOtpLogin'));
    });
    element('si-otp-back').addEventListener('click', () => {
      if (busy) return;
      void run(() => entry.invoke('backToEmail'))
        .then(() => { status.textContent = 'メールアドレスを入力してください。'; });
    });
    bind('si-recovery-request', () => { const email = element('si-recovery-email').value; void run(() => entry.invoke('requestRecovery', email)); });
    bind('si-recovery-verify', () => {
      const code = element('si-recovery-code').value; element('si-recovery-code').value = '';
      void run(() => entry.invoke('verifyRecovery', code));
    });
    bind('si-totp-choice', () => {
      const choice = element('si-totp-factor').value;
      if (!choices.includes(choice) || (selected !== null && selected !== choice)) return;
      selected = choice;
      // Failed challenges can be retried only for the explicitly chosen factor.
      for (const option of element('si-totp-factor').options) option.disabled = option.value !== selected;
      void run(() => entry.invoke('chooseAndChallengeTOTP', selected));
    });
    bind('si-totp-verify', () => {
      const code = element('si-totp-code').value; element('si-totp-code').value = '';
      void run(() => entry.invoke('verifyTOTP', code));
    });
    bind('si-password-change', () => {
      const password = element('si-new-password').value; element('si-new-password').value = '';
      void run(() => entry.invoke('changePassword', password));
    });
    addEventListener('pagehide', () => { stopGuidanceTimer(); entry.dispose(); clearInputs(); choices = []; selected = null; }, { once: true });
    render();
  } catch {
    entry?.dispose(); clearInputs();
    root.querySelectorAll('button').forEach(button => { button.disabled = true; });
    status.textContent = '画面を準備できませんでした。接続元のAIからやり直してください。';
    element('si-existing-restart').hidden = false;
  }
}
