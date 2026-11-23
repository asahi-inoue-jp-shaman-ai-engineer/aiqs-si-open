import { createClient } from '@supabase/supabase-js';
import { createMemoryClient, createIndependentEntry, createBrowserPendingStore } from './independent-entry.js';
import { parseEntryRuntimeConfig } from './entry-runtime-config.js';

// Loaded only by the opt-in synthetic Keycloak template, never the public app.
const root = document.getElementById('si-entry');
if (root) {
  const element = (id) => document.getElementById(id);
  const status = element('si-status');
  const nativeForm = element('si-handoff');
  let busy = false;
  let client;
  let entry;
  function clearInputs() {
    root.querySelectorAll('input').forEach(input => { input.value = ''; });
  }
  function render() {
    const state = entry?.state;
    // A restored 'verification' state also offers login (Astra
    // round-1 F10): the email may already be confirmed, and signing
    // in is what completes the pending registration.
    element('si-login-form').hidden = !(state === 'ready' || state === 'verification');
    element('si-signup-form').hidden = state !== 'ready';
    element('si-verify-form').hidden = state !== 'verification';
    element('si-retry-complete').hidden = state !== 'completion';
    root.querySelectorAll('button').forEach(button => { button.disabled = busy; });
  }
  async function run(action) {
    if (busy || !entry) return;
    busy = true;
    render();
    status.textContent = '確認しています…';
    try {
      await action();
      if (entry.state === 'verification') status.textContent = '同じ画面で、メールの確認コードを入力してください。';
    } catch {
      status.textContent = entry.state === 'completion'
        ? '登録完了を確認できませんでした。「登録完了を再確認」で再試行してください。'
        : entry.state === 'signup-pending'
          ? '登録結果を確認できませんでした。この試行を閉じ、接続元のAIからやり直してください。'
          : '認証を確認できませんでした。入力内容を確認してください。';
    } finally {
      busy = false;
      render();
    }
  }
  try {
    const sdkConfig = parseEntryRuntimeConfig(root.dataset, location.origin);
    const destination = new URL(nativeForm.action);
    if (destination.origin !== location.origin || destination.username || destination.password ||
        nativeForm.method.toLowerCase() !== 'post') throw new Error();
    client = createMemoryClient(createClient, sdkConfig);
    entry = createIndependentEntry({ auth: client.auth, origin: location.origin,
      pendingStore: createBrowserPendingStore(),
      fetch: (url, options) => fetch(url, { ...options, signal: AbortSignal.timeout(10000) }),
      handoff: async token => {
        nativeForm.elements.namedItem('upstream_token').value = token;
        HTMLFormElement.prototype.submit.call(nativeForm);
        // Native submit constructs its POST body before returning. No token URL,
        // storage, postMessage, or external callback destination is used.
        clearInputs();
        client = null;
      },
    });
    element('si-login-form').addEventListener('submit', event => {
      event.preventDefault();
      const email = element('si-login-email').value;
      const password = element('si-login-password').value;
      element('si-login-password').value = '';
      void run(() => entry.login({ email, password }));
    });
    element('si-signup-form').addEventListener('submit', event => {
      event.preventDefault();
      const email = element('si-signup-email').value;
      const password = element('si-signup-password').value;
      const referralCode = element('si-referral').value || null;
      element('si-signup-password').value = '';
      void run(() => entry.signup({ email, password, referralCode }));
    });
    element('si-verify-form').addEventListener('submit', event => {
      event.preventDefault();
      const code = element('si-code').value;
      element('si-code').value = '';
      void run(async () => { await entry.verify(code); render(); await entry.complete(); });
    });
    element('si-retry-complete').addEventListener('click', () => { void run(() => entry.complete()); });
    addEventListener('pagehide', () => { entry.dispose(); client = null; clearInputs(); }, { once: true });
    render();
  } catch {
    entry?.dispose();
    client = null;
    clearInputs();
    root.querySelectorAll('button').forEach(button => { button.disabled = true; });
    status.textContent = '検証画面を初期化できませんでした。接続元のAIからやり直してください。';
  }
}
