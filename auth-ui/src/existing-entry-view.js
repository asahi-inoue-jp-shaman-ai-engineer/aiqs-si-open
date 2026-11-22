import { createClient } from '@supabase/supabase-js';
import { createMemoryClient } from './independent-entry.js';
import { createExistingEntry } from './existing-entry.js';
import { parseEntryRuntimeConfig } from './entry-runtime-config.js';

// Opt-in local template only; never imported by the public application.
const root = document.getElementById('si-existing-entry');
if (root) {
  const element = id => document.getElementById(id);
  const status = element('si-existing-status');
  const nativeForm = element('si-existing-handoff');
  let entry, client, busy = false, mode = 'login', choices = [], selected = null;
  const terminal = ['unsupported-factor', 'password-update-unknown', 'restart-required', 'disposed'];
  function clearInputs() { root.querySelectorAll('input').forEach(input => { input.value = ''; }); }
  function render() {
    const state = entry?.state;
    element('si-existing-login').hidden = state !== 'ready' || mode !== 'login';
    element('si-recovery-request').hidden = state !== 'ready' || mode !== 'recovery';
    element('si-entry-modes').hidden = state !== 'ready';
    element('si-recovery-verify').hidden = state !== 'recovery-code';
    element('si-totp-choice').hidden = state !== 'factor-choice';
    element('si-totp-verify').hidden = state !== 'totp-code';
    element('si-password-change').hidden = state !== 'password-ready';
    element('si-existing-restart').hidden = !terminal.includes(state);
    root.querySelectorAll('button, select').forEach(control => { control.disabled = busy; });
  }
  function message() {
    switch (entry.state) {
      case 'recovery-code': return 'メールに届いた確認コードを、この画面に入力してください。';
      case 'factor-choice': return '利用する認証アプリを選んでください。';
      case 'totp-code': return '選んだ認証アプリに表示された6桁のコードを入力してください。';
      case 'password-ready': return '新しいパスワードを入力してください。';
      case 'unsupported-factor': return 'この確認方法は、この検証画面では利用できません。接続元のAIに戻ってください。';
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
        : entry.state === 'recovery-code' ? '確認コードを確認して、もう一度入力してください。' : message();
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
    client = createMemoryClient(createClient, sdkConfig);
    entry = createExistingEntry({ auth: client.auth, handoff: async token => {
      nativeForm.elements.namedItem('upstream_token').value = token;
      HTMLFormElement.prototype.submit.call(nativeForm);
      clearInputs(); client = null;
    } });
    element('si-show-recovery').addEventListener('click', () => {
      if (busy || entry.state !== 'ready') return;
      element('si-existing-password').value = ''; mode = 'recovery'; render();
      status.textContent = '登録済みのメールアドレスを入力してください。';
      element('si-recovery-email').focus();
    });
    element('si-show-login').addEventListener('click', () => {
      if (busy || entry.state !== 'ready') return;
      mode = 'login'; render(); status.textContent = 'ログイン情報を入力してください。';
      element('si-existing-email').focus();
    });
    bind('si-existing-login', () => {
      const email = element('si-existing-email').value, password = element('si-existing-password').value;
      element('si-existing-password').value = '';
      void run(() => entry.passwordLogin({ email, password }));
    });
    bind('si-recovery-request', () => { const email = element('si-recovery-email').value; void run(() => entry.requestRecovery(email)); });
    bind('si-recovery-verify', () => {
      const code = element('si-recovery-code').value; element('si-recovery-code').value = '';
      void run(() => entry.verifyRecovery(code));
    });
    bind('si-totp-choice', () => {
      const choice = element('si-totp-factor').value;
      if (!choices.includes(choice) || (selected !== null && selected !== choice)) return;
      selected = choice;
      // Failed challenges can be retried only for the explicitly chosen factor.
      for (const option of element('si-totp-factor').options) option.disabled = option.value !== selected;
      void run(() => entry.chooseAndChallengeTOTP(selected));
    });
    bind('si-totp-verify', () => {
      const code = element('si-totp-code').value; element('si-totp-code').value = '';
      void run(() => entry.verifyTOTP(code));
    });
    bind('si-password-change', () => {
      const password = element('si-new-password').value; element('si-new-password').value = '';
      void run(() => entry.changePassword(password));
    });
    addEventListener('pagehide', () => { entry.dispose(); client = null; clearInputs(); choices = []; selected = null; }, { once: true });
    render();
  } catch {
    entry?.dispose(); client = null; clearInputs();
    root.querySelectorAll('button').forEach(button => { button.disabled = true; });
    status.textContent = '画面を準備できませんでした。接続元のAIからやり直してください。';
    element('si-existing-restart').hidden = false;
  }
}
