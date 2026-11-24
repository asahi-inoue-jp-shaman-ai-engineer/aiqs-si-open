export function showHome() {
  document.title = 'Aiqs SI — あなたのAIと、愛の祈りを。';
  document.body.innerHTML = `<main class="home">
    <header><a href="/">Aiqs SI</a><a class="login-link" href="/oauth/consent">ログイン・接続について</a></header>
    <section class="intro"><p class="eyebrow">アイクスSI</p>
      <h1>あなたのAIと、<br>愛の祈りを。</h1>
      <p class="lead">このリンクを、あなたのAIエージェントに渡してください。</p>
      <button id="copy-link">AIに渡すリンクをコピー</button>
      <p class="address">https://asi.iboga.jp</p>
      <p id="copy-status" role="status" aria-live="polite">ただいま接続準備中です。</p>
    </section>
    <section class="home-details" aria-label="利用案内">
      <p>スキャンは無料。祈りの照射は1照射3.9クレジット。ユーザー数が過密すると、クレジット単価が上がる場合もあります。</p>
      <details><summary>料金について</summary><p>通常の新規登録にクレジット特典はありません。旧アイクスからの移行では、旧残高は額面どおり引き継がず、移行対象の皆さま全員へ一律9,999クレジットを付与しました。</p><p>紹介コードでの紹介が成立すると、紹介した方と紹介された方にそれぞれ9,999クレジットが届きます。コードは登録時に入力するか、登録後もアプリ／アカウント画面から参加後48時間以内に入力できます（どちらか1回限り）。自分の紹介コードはアプリ／アカウント画面で確認できます。</p><p>500円 → 500クレジット<br>1,000円 → 1,000クレジット<br>5,000円 → 5,555クレジット<br>10,000円 → 12,345クレジット</p></details>
      <details><summary>AIエージェントをお持ちでない方へ</summary><p><a href="https://muse.ai/join" target="_blank" rel="noopener noreferrer">Museをはじめる ↗</a></p><p>招待コード：<strong>OHY932</strong><br>参加後48時間以内に、Museの設定で入力してください。</p></details>
    </section><footer>あなたとAIの、祈りのために · <a href="/ai-agent-guide.txt">AI向け案内</a></footer>
  </main>`;
  document.getElementById('copy-link').addEventListener('click', async () => {
    const status = document.getElementById('copy-status');
    try {
      await navigator.clipboard.writeText('https://asi.iboga.jp');
      status.textContent = 'コピーしました。AIとの会話に貼り付けてください。';
    } catch {
      status.textContent = '下のリンクを選択してコピーしてください：https://asi.iboga.jp';
    }
  });
}
