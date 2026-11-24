import { createAccountClient } from './account-api.js';
import { createFragmentSession, installCarriedLinkRefresh } from './account-session.js';

// SI-only credit purchase page (design §5.1). The server hands out
// per-user Stripe Payment Link URLs with client_reference_id already
// attached; a pack whose link is not configured yet arrives with
// url=null and is shown as a disabled "準備中" button — never a dead
// link. Linked accounts buy in D-Planet instead (§4.4), so they get
// that guidance and no purchase buttons.
export function showAccountPurchase({ fetch = globalThis.fetch, getToken, withToken } = {}) {
  document.title = 'Aiqs SI — クレジット購入';
  // Shared-session protocol, as on the link page (batch 2e, R1).
  const session = getToken ? null : createFragmentSession({ location, history });
  const tokenProvider = getToken || session.getToken;
  const client = createAccountClient({
    fetch, getToken: tokenProvider,
    withToken: withToken || session?.withToken,
  });
  document.body.innerHTML = `<main>
    <header><a href="/">Aiqs SI</a><span>あなたとAIの、祈りのために</span></header>
    <section class="card"><p class="eyebrow">クレジット購入</p>
      <h1>クレジットを購入する</h1>
      <p class="lead">アイクスSI単独で使うクレジットは、このページから購入できます。1クレジット＝1円です。</p>
      <p id="status" role="status" aria-live="polite">読み込んでいます。</p>
      <div id="pack-list" class="pack-list" hidden></div>
      <div id="purchase-linked" hidden>
        <p>D-Planetと連携済みのアカウントは、クレジットをD-Planetで購入できます。購入したクレジットは、そのままアイクスSIでも使えます。</p>
        <div class="actions"><a href="/account/link">連携の状態を確認する</a></div>
      </div>
      <div id="purchase-login" hidden>
        <p>この画面を使うにはログインが必要です。利用しているAIからアイクスSIへの接続を開き、その案内に従ってください。</p>
        <div class="actions"><a href="/oauth/consent">ログイン・接続について</a></div>
      </div>
      <aside>
        <p>クレジットは、支払いの確認が取れてから反映されます。反映まで数十秒かかることがあります。購入後の返金は原則としてお受けできません。</p>
        <p>D-Planet（テレパシースクール）に参加すると、特典クレジットやコミュニティでもっと楽しめます。参加しなくても、アイクスSIはそのまま使えます。</p>
      </aside>
    </section>
    <footer>この画面から照射は始まりません。</footer>
  </main>`;

  const element = (id) => document.getElementById(id);
  const status = (text) => { element('status').textContent = text; };
  // Keep the session when the user follows this page's own links
  // to another account page (fragment handoff, account-session.js).
  const refreshCarriedLinks = installCarriedLinkRefresh({
    target: window, document, tokenProvider, session,
  });
  void refreshCarriedLinks?.();
  const sections = ['pack-list', 'purchase-linked', 'purchase-login'];
  const render = (visible) => {
    for (const id of sections) element(id).hidden = id !== visible;
  };

  function renderPacks(packs) {
    const list = element('pack-list');
    list.replaceChildren();
    for (const pack of packs) {
      const row = document.createElement('div');
      row.className = 'pack';
      const name = document.createElement('span');
      name.className = 'pack-name';
      name.textContent = `${pack.yen.toLocaleString('ja-JP')}円 → ${pack.credits.toLocaleString('ja-JP')}クレジット`;
      row.append(name);
      if (pack.url) {
        const link = document.createElement('a');
        link.className = 'button-link';
        link.href = pack.url;
        link.textContent = '購入ページへ';
        row.append(link);
      } else {
        const button = document.createElement('button');
        button.type = 'button';
        button.disabled = true;
        button.textContent = '準備中';
        row.append(button);
      }
      list.append(row);
    }
    render('pack-list');
  }

  void (async () => {
    const result = await client.purchase();
    if (result.kind === 'login') { render('purchase-login'); return status('ログインが必要です。'); }
    if (result.kind !== 'ok') { render(null); return status('読み込めませんでした。時間をおいて、もう一度開いてください。'); }
    const data = result.data;
    if (data.linked) { render('purchase-linked'); return status('D-Planetと連携済みです。'); }
    renderPacks(Array.isArray(data.packs) ? data.packs : []);
    status(data.configured
      ? '購入するパックを選んでください。'
      : '購入ページは準備中です。開始までお待ちください。');
  })();
}
