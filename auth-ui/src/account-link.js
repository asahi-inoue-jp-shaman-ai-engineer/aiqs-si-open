import { createAccountClient } from './account-api.js';
import { createTokenProvider, installCarriedLinkRefresh } from './account-session.js';

// D-Planet linking screen (the auth-ui secondary path; the MCP tools
// run the identical flow). Verify only parks a reservation: the link
// completes solely when the user confirms the pairing after seeing
// BOTH account names, exactly like the agent-driven path.
export function showAccountLink({ fetch = globalThis.fetch, getToken } = {}) {
  document.title = 'Aiqs SI — D-Planet連携';
  const tokenProvider = getToken || createTokenProvider(location, history);
  const client = createAccountClient({ fetch, getToken: tokenProvider });
  document.body.innerHTML = `<main>
    <header><a href="/">Aiqs SI</a><span>あなたとAIの、祈りのために</span></header>
    <section class="card"><p class="eyebrow">アカウント連携</p>
      <h1>D-Planetと連携する</h1>
      <p class="lead">D-Planetで発行した連携コードを入力すると、このSIアカウントの残高をD-Planetのウォレットへ移し、共通のクレジットで使えるようになります。連携は任意です。連携しなくても、SIの残高はそのまま使えます。</p>
      <p id="status" role="status" aria-live="polite">読み込んでいます。</p>
      <form id="link-form" hidden>
        <label>D-Planetの連携コード（8桁）
          <input id="link-code" inputmode="numeric" autocomplete="one-time-code" placeholder="1234-5678">
        </label>
        <div class="actions"><button type="submit">コードを確認する</button></div>
        <p class="note">コードはD-Planetの連携画面で発行できます（有効期限は10分です）。このコードは他人に教えないでください。入力できるのは、あなた自身のSIアカウントだけです。</p>
      </form>
      <div id="link-confirm" hidden>
        <p>次の内容で連携します。お名前がご自身のものか、必ず確認してください。</p>
        <dl class="pairing">
          <div><dt>D-Planet</dt><dd id="confirm-dplanet-name"></dd></div>
          <div><dt>アイクスSI</dt><dd id="confirm-si-name"></dd></div>
        </dl>
        <p class="note">確定すると、このSIアカウントの残高は全額D-Planetのウォレットへ移ります。</p>
        <div class="actions">
          <button id="confirm-link" type="button">この内容で連携する</button>
          <button id="cancel-link" type="button" class="secondary">やめる</button>
        </div>
      </div>
      <div id="link-linked" hidden>
        <p id="linked-detail"></p>
        <details><summary>連携を解除する</summary>
          <p>解除しても、クレジットはD-Planetのウォレットに残ります（SI側へは戻りません）。再度連携すれば、またD-Planetの残高をSIで使えます。実行中・状態不明の照射がある間は解除できません。</p>
          <div class="actions"><button id="unlink" type="button" class="secondary">連携を解除する</button></div>
        </details>
      </div>
      <div id="link-login" hidden>
        <p>この画面を使うにはログインが必要です。利用しているAIからアイクスSIへの接続を開き、その案内に従ってください。</p>
        <div class="actions"><a href="/oauth/consent">ログイン・接続について</a></div>
      </div>
      <div id="referral-section" hidden>
        <h2>紹介コード</h2>
        <p class="note">あなたの紹介コード：<strong id="referral-code">—</strong></p>
        <div id="referral-entry">
          <label>もらった紹介コードを入力（登録から48時間以内・1回限り。成立すると両方に9,999クレジット）
            <input id="referral-input" type="text" autocomplete="off">
          </label>
          <div class="actions"><button id="referral-submit" type="button">コードを使う</button></div>
        </div>
        <p class="note" id="referral-done" hidden>紹介は成立済みです。</p>
        <p class="note" id="referral-status" role="status"></p>
      </div>
    </section>
    <footer>パスワードをAIやチャットへ送る必要はありません。</footer>
  </main>`;

  const element = (id) => document.getElementById(id);
  const status = (text) => { element('status').textContent = text; };
  // Keep the session when the user follows this page's own links
  // to another account page (fragment handoff, account-session.js).
  const refreshCarriedLinks = installCarriedLinkRefresh({
    target: window, document, tokenProvider,
  });
  void refreshCarriedLinks?.();
  const sections = ['link-form', 'link-confirm', 'link-linked', 'link-login'];
  let busy = false;
  let reservationId = null;

  function render(visible) {
    for (const id of sections) element(id).hidden = id !== visible;
    document.querySelectorAll('button').forEach((button) => { button.disabled = busy; });
  }

  function showLogin() {
    render('link-login');
    status('ログインが必要です。');
  }

  function showLinked(link) {
    const detail = element('linked-detail');
    let text = 'D-Planetと連携済みです。クレジットの残高はD-Planetのウォレットで管理されます。';
    const linkedAt = link && typeof link.linked_at === 'number' ? new Date(link.linked_at * 1000) : null;
    if (linkedAt && !Number.isNaN(linkedAt.getTime())) {
      text += `（連携日：${linkedAt.toLocaleDateString('ja-JP')}）`;
    }
    detail.textContent = text;
    render('link-linked');
  }

  async function load() {
    busy = true; render(null);
    const result = await client.linkStatus();
    busy = false;
    if (result.kind === 'login') return showLogin();
    if (result.kind !== 'ok') { render(null); return status('読み込めませんでした。時間をおいて、もう一度開いてください。'); }
    if (!result.data.configured) { render(null); return status('D-Planet連携は準備中です。開始までお待ちください。'); }
    if (result.data.linked) { showLinked(result.data.link); return status('D-Planetと連携済みです。'); }
    render('link-form');
    status('D-Planetで発行した連携コードを入力してください。');
  }

  element('link-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy) return;
    const code = element('link-code').value.trim();
    if (!code) return status('連携コードを入力してください。');
    busy = true; render('link-form'); status('コードを確認しています。');
    const result = await client.verifyLink(code);
    busy = false;
    if (result.kind === 'login') return showLogin();
    if (result.kind !== 'ok') { render('link-form'); return status('確認できませんでした。時間をおいて、もう一度お試しください。'); }
    const data = result.data;
    if (data.status === 'already_linked') { await load(); return; }
    if (data.error) { render('link-form'); return status(data.error); }
    if (data.status !== 'confirmation_required' || !data.reservation_id) {
      render('link-form'); return status('確認できませんでした。コードを発行し直して、もう一度お試しください。');
    }
    reservationId = data.reservation_id;
    element('confirm-dplanet-name').textContent = data.dplanet_player_name || '（名前未設定）';
    element('confirm-si-name').textContent = data.si_display_name || '（名前未設定）';
    render('link-confirm');
    status('連携する内容を確認してください。');
  });

  element('cancel-link').addEventListener('click', () => {
    reservationId = null;
    render('link-form');
    status('連携をやめました。コードはそのままでは使われていません。');
  });

  element('confirm-link').addEventListener('click', async () => {
    if (busy || !reservationId) return;
    busy = true; render('link-confirm'); status('連携を確定しています。');
    const result = await client.confirmLink(reservationId);
    busy = false;
    reservationId = null;
    if (result.kind === 'login') return showLogin();
    if (result.kind !== 'ok') { render('link-form'); return status('確定できませんでした。コードを発行し直して、もう一度お試しください。'); }
    const data = result.data;
    if (!data.linked) {
      render('link-form');
      return status(data.error || '確定できませんでした。コードを発行し直して、もう一度お試しください。');
    }
    const transfer = data.balance_transfer || {};
    let message;
    if (transfer.status === 'pending') {
      message = `連携しました。${transfer.amount || 0}クレジットの移行は保留中です。完了まで自動で再試行されます。`;
    } else if (transfer.amount > 0) {
      message = `連携しました。${transfer.amount}クレジットをD-Planetのウォレットへ移しました。`;
    } else {
      message = '連携しました。移す残高はありませんでした。';
    }
    showLinked({ linked_at: Date.now() / 1000 });
    status(message);
  });

  // Referral section: reuses the app referral endpoints with the
  // account token. The section stays hidden when the endpoint
  // answers 404 (referral switch off) or the call fails.
  const REFERRAL_ERROR_TEXT = {
    invalid_code: 'コードが見つかりませんでした。入力を確認してください。',
    own_code: '自分の紹介コードは使えません。',
    already_redeemed: '紹介コードはすでに利用済みです。',
    window_closed: 'コードを入力できる期間（登録から48時間）を過ぎています。',
    cross_unconfigured: 'コードを確認できませんでした。時間をおいて、もう一度お試しください。',
  };

  async function loadReferral() {
    const result = await client.referral();
    if (result.kind !== 'ok' || result.status === 404 || result.data.error) return;
    const info = result.data;
    element('referral-code').textContent = info.code || '—';
    element('referral-entry').hidden = Boolean(info.redeemed) || !info.window?.open;
    element('referral-done').hidden = !info.redeemed;
    element('referral-section').hidden = false;
    const gifts = Array.isArray(info.unseen_gifts) ? info.unseen_gifts : [];
    if (gifts.length) {
      const total = gifts.reduce((sum, gift) => sum + (gift.amount || 0), 0);
      element('referral-status').textContent =
        `🎁 ${total.toLocaleString('ja-JP')}クレジットが届きました！`;
      void client.markReferralGiftsSeen(gifts.map((gift) => gift.id));
    }
  }

  element('referral-submit').addEventListener('click', async () => {
    const code = element('referral-input').value.trim();
    if (!code) {
      element('referral-status').textContent = 'コードを入力してください。';
      return;
    }
    const result = await client.redeemReferral(code);
    if (result.kind === 'login') return showLogin();
    if (result.kind !== 'ok') {
      element('referral-status').textContent = 'コードを使えませんでした。時間をおいて、もう一度お試しください。';
      return;
    }
    const data = result.data;
    if (data.error) {
      element('referral-status').textContent =
        REFERRAL_ERROR_TEXT[data.error] || 'コードを使えませんでした。';
      return;
    }
    element('referral-status').textContent =
      `🎁 紹介が成立しました！${Number(data.amount ?? 9999).toLocaleString('ja-JP')}クレジットが届きました！`;
    void loadReferral();
  });

  void loadReferral();

  element('unlink').addEventListener('click', async () => {
    if (busy) return;
    busy = true; render('link-linked'); status('連携を解除しています。');
    const result = await client.unlink();
    busy = false;
    if (result.kind === 'login') return showLogin();
    if (result.kind !== 'ok') { await load(); return status('解除できませんでした。時間をおいて、もう一度お試しください。'); }
    const data = result.data;
    if (data.status === 'unlinked' || data.status === 'not_linked') {
      render('link-form');
      return status('連携を解除しました。クレジットはD-Planetのウォレットに残っています。');
    }
    await load();
    status(data.error || '解除できませんでした。');
  });

  void load();
}
