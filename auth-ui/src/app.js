import { createAppClient } from './app-api.js';
import { createAppAuth } from './app-auth.js';
import { proofCopyText, readingCopyText, scanCopyText, settledCreditsOf, splitReadingText } from './app-copy.js';

// アイクスSI (si-standalone-app-design-20261122): the fixed-
// flow standalone app. Every screen is a fixed layout with template
// copy; the only dynamic values are the user's own inputs and the
// server's public results. There is no chat. The one exception to
// the fixed copy is the optional AI reading
// (native-reading-design-20261122): generated text appears only on
// the reading result screen, always under its fixed
// 「AIによるリーディング」 label, and only when the server says
// the feature is enabled — the screens say so honestly either way.
const NO_READING_NOTICE = 'このアプリでは結果の読み解き（リーディング）は行いません。'
  + '読み解きは、結果をコピーしてご自身の Muse に渡すか、エージェント経由のご利用でどうぞ。';
const readingOnNotice = (credits) => 'このアプリでは、共鳴ランキングの読み解き（リーディング）を'
  + `${credits}クレジットで利用できます。結果をコピーしてご自身の Muse に渡す読み解きも、引き続き利用できます。`;
const FOOTNOTE_FIXED = '表示される文章はすべて定型文です。このアプリが結果を解釈したり、助言したりすることはありません。';
const FOOTNOTE_READING = 'リーディングの文章だけはAIがその場で作成します。それ以外の表示はすべて定型文です';
const READING_TRANSPARENCY = 'リーディングの作成には、共鳴ランキング・意図・自己愛アファーメーションの内容をAIサービス（OpenRouter）へ送信します';
const READING_FAILED_TEXT = 'リーディングを作成できませんでした。クレジットは消費されていません。時間をおいてもう一度お試しください';
const READING_CAP_TEXT = '本日のリーディング回数の上限に達しました。明日またお試しください';
const READING_EXPIRED_TEXT = '保存期間を過ぎたため、本文は表示できません';
const GUIDE_URL = '/ai-agent-guide.txt';
const STRENGTH = (count) => (count <= 33 ? '弱' : count <= 66 ? '中' : '強');
const STATE_LABEL = {
  queued: '順番待ち', running: '実行中', completed: '完了',
  failed: '失敗', cancelled: '停止済み', unknown: '状態を確認中',
};

export function showApp({ fetch = globalThis.fetch } = {}) {
  document.title = 'アイクスSI';
  const auth = createAppAuth({
    fetch, location, history, storage: sessionStorage,
    persistentStorage: localStorage, crypto: globalThis.crypto,
  });
  const client = createAppClient({ fetch, getToken: () => auth.getToken() });

  document.body.innerHTML = `<main>
    <header><a href="/">Aiqs SI</a><span>あなたとAIの、祈りのために</span></header>
    <section class="card"><p class="eyebrow">アイクスSI</p>
      <h1 id="app-title">アイクスSI</h1>
      <p id="status" role="status" aria-live="polite">読み込んでいます。</p>

      <div id="screen-login" hidden>
        <p class="lead">アイクスSIを、AIエージェントなしでそのまま使えるアプリです。スキャンは1日9回まで無料です。照射は回数分のクレジットで実行します。</p>
        <div class="actions"><button type="button" id="login-button">ログイン／新規登録</button></div>
        <p class="note">新規登録はメールアドレスと確認コードだけで完了します（招待コードは不要です）。ログイン画面で登録とログインの両方ができます。</p>
      </div>

      <div id="screen-home" hidden>
        <dl class="pairing">
          <div><dt>残高</dt><dd id="home-balance">—</dd></div>
          <div><dt>照射の単価</dt><dd id="home-price">—</dd></div>
        </dl>
        <div id="referral-card" hidden>
          <h2>紹介コード</h2>
          <p class="note">あなたの紹介コード：<strong id="referral-code">—</strong></p>
          <div class="actions"><button type="button" id="referral-copy" class="secondary">コードをコピー</button></div>
          <p class="note" id="referral-copy-status" role="status"></p>
          <div id="referral-entry">
            <label>もらった紹介コードを入力（登録から48時間以内・1回限り。成立すると両方に9,999クレジット）
              <input id="referral-input" type="text" autocomplete="off">
            </label>
            <div class="actions"><button type="button" id="referral-submit">コードを使う</button></div>
          </div>
          <p class="note" id="referral-done" hidden>紹介は成立済みです。</p>
          <p class="note" id="referral-status" role="status"></p>
        </div>
        <aside id="sub-banner" class="sub-banner" hidden>
          <p class="sub-banner-label">関連プロダクト</p>
          <p class="sub-banner-title">テレスク（D-Planet）</p>
          <p>年額プラン69%オフ（3,716円/年）</p>
          <p><a href="https://tschool.iboga.jp/subscribe.html" target="_blank" rel="noopener">テレスクのサブスクを見る ↗</a></p>
        </aside>
        <div id="gift-overlay" class="gift-overlay" hidden>
          <p class="gift-emoji">🎁</p>
          <p id="gift-text">9,999クレジットが届きました！</p>
          <p class="note" id="gift-balance"></p>
          <div class="actions"><button type="button" id="gift-close">閉じる</button></div>
        </div>
        <div class="actions">
          <button type="button" id="home-scan">スキャンする</button>
          <button type="button" id="home-targets" class="secondary">対象の管理</button>
          <button type="button" id="home-history" class="secondary">履歴</button>
          <a class="button-link secondary" href="/account/purchase">クレジットを買う</a>
          <a class="button-link secondary" href="/account">アカウント</a>
          <button type="button" id="home-guide" class="secondary">Muse を始める</button>
        </div>
      </div>

      <div id="screen-scan" hidden>
        <h2>スキャン</h2>
        <p class="note">スキャンは1日9回まで無料です。対象を選び、意図と自己愛アファーメーションを入れてください。</p>
        <label>対象
          <select id="scan-target"></select>
        </label>
        <p class="note" id="scan-no-target" hidden>登録済みの対象がありません。先に対象を登録してください。
          <button type="button" id="scan-goto-register" class="secondary">対象を登録する</button></p>
        <label>意図（インテンション）
          <input id="scan-intention" type="text" autocomplete="off">
        </label>
        <label>自己愛アファーメーション
          <input id="scan-joy" type="text" autocomplete="off">
        </label>
        <p class="note">自己愛アファーメーションは、ご本人が決めた文を入れる場所です。</p>
        <div class="actions">
          <button type="button" id="scan-submit">スキャンする（無料）</button>
          <button type="button" id="scan-back" class="secondary">ホームへ</button>
        </div>
      </div>

      <div id="screen-target-form" hidden>
        <h2>対象の登録</h2>
        <label>対象の名前
          <input id="target-name" type="text" autocomplete="off">
        </label>
        <label>写真
          <input id="target-photo" type="file" accept="image/*">
        </label>
        <label>写真2（任意）
          <input id="target-photo2" type="file" accept="image/*">
        </label>
        <label>生年月日（任意）
          <input id="target-birthday" type="text" autocomplete="off">
        </label>
        <label>性別（任意）
          <input id="target-gender" type="text" autocomplete="off">
        </label>
        <p class="note">登録した対象（名前と写真）はアイクスSIのサーバーに保存され、エージェント経由の利用でも同じ対象が使えます。削除は「対象の管理」からでき、削除すると写真も消えます。</p>
        <div class="actions">
          <button type="button" id="target-save">登録する</button>
          <button type="button" id="target-cancel" class="secondary">戻る</button>
        </div>
      </div>

      <div id="screen-targets" hidden>
        <h2>対象の管理</h2>
        <div id="target-list"></div>
        <div class="actions">
          <button type="button" id="targets-add">対象を追加する</button>
          <button type="button" id="targets-back" class="secondary">ホームへ</button>
        </div>
      </div>

      <div id="screen-wait" hidden>
        <h2 id="wait-title">スキャンを受け付けました</h2>
        <p id="wait-text">順番に処理されます。そのままお待ちください。</p>
      </div>

      <div id="screen-ranking" hidden>
        <h2>共鳴ランキング</h2>
        <p class="note">ヒット数の多い順に、上位10件を表示しています。</p>
        <ol id="ranking-list"></ol>
        <p class="note"><span id="ranking-notice">${NO_READING_NOTICE}</span>（<a href="${GUIDE_URL}">エージェント経由の案内</a>）</p>
        <div class="actions">
          <button type="button" id="ranking-copy">結果をコピー</button>
          <button type="button" id="ranking-reading" hidden>リーディングする</button>
          <button type="button" id="ranking-guide" class="secondary">Muse を始める（紹介コードの案内）</button>
          <button type="button" id="ranking-irradiate">照射へ進む</button>
          <button type="button" id="ranking-rescan" class="secondary">もう一度スキャン</button>
          <button type="button" id="ranking-done" class="secondary">ここまでにする</button>
        </div>
        <p id="copy-status" class="note" role="status"></p>
      </div>

      <div id="screen-reading-confirm" hidden>
        <h2>リーディングの確認</h2>
        <dl class="pairing">
          <div><dt>価格</dt><dd id="reading-confirm-price">—</dd></div>
          <div><dt>実行後の残高</dt><dd id="reading-confirm-after">—</dd></div>
        </dl>
        <p class="note">${READING_TRANSPARENCY}</p>
        <p class="note" id="reading-confirm-insufficient" hidden>残高が不足しています。<a href="/account/purchase">クレジット購入ページ</a>から購入できます。</p>
        <div class="actions">
          <button type="button" id="reading-confirm-submit">リーディングする</button>
          <button type="button" id="reading-confirm-back" class="secondary">ランキングへ戻る</button>
        </div>
      </div>

      <div id="screen-reading-wait" hidden>
        <h2>リーディングを作成しています</h2>
        <p>リーディングを作成しています。そのままお待ちください。</p>
      </div>

      <div id="screen-reading-result" hidden>
        <h2>リーディング</h2>
        <p class="note">AIによるリーディング</p>
        <div id="reading-summary" class="reading-summary" hidden></div>
        <p id="reading-text" style="white-space: pre-wrap"></p>
        <div class="actions">
          <button type="button" id="reading-copy">リーディングをコピー</button>
          <button type="button" id="reading-irradiate">照射へ進む</button>
          <button type="button" id="reading-back" class="secondary">ランキングへ戻る</button>
          <button type="button" id="reading-done" class="secondary">ここまでにする</button>
        </div>
        <p id="reading-copy-status" class="note" role="status"></p>
      </div>

      <div id="screen-confirm" hidden>
        <h2>照射の確認</h2>
        <label>照射回数：<span id="confirm-count-label">33</span>回（<span id="confirm-strength">弱</span>）
          <input id="confirm-slider" type="range" min="1" max="99" step="1" value="33">
        </label>
        <div class="actions">
          <button type="button" class="secondary preset" data-count="33">33回</button>
          <button type="button" class="secondary preset" data-count="66">66回</button>
          <button type="button" class="secondary preset" data-count="99">99回</button>
        </div>
        <p class="note">強さの目安：弱 1〜33回／中 34〜66回／強 67〜99回</p>
        <dl class="pairing">
          <div><dt>単価</dt><dd id="confirm-unit">—</dd></div>
          <div><dt>合計</dt><dd id="confirm-total">—</dd></div>
          <div><dt>照射後の残高</dt><dd id="confirm-after">—</dd></div>
        </dl>
        <p class="note" id="confirm-insufficient" hidden>残高が不足しています。<a href="/account/purchase">クレジット購入ページ</a>から購入できます。</p>
        <div class="actions">
          <button type="button" id="confirm-submit">この内容で照射する</button>
          <button type="button" id="confirm-back" class="secondary">ランキングへ戻る</button>
        </div>
      </div>

      <div id="screen-progress" hidden>
        <h2>照射中</h2>
        <p id="progress-text">照射を実行しています。</p>
        <div class="actions"><button type="button" id="progress-stop" class="secondary">停止する</button></div>
        <p class="note">停止すると、待機中の場合は全額が戻り、実行中の場合は次の1回の区切りで止まって、完了した分だけが精算されます。</p>
      </div>

      <div id="screen-proof" hidden>
        <h2>稼働証明</h2>
        <dl class="pairing">
          <div><dt>状態</dt><dd id="proof-state">—</dd></div>
          <div><dt>完了回数</dt><dd id="proof-count">—</dd></div>
          <div><dt>精算クレジット</dt><dd id="proof-settled">—</dd></div>
          <div><dt>デバイス公開ID</dt><dd id="proof-device">—</dd></div>
          <div><dt>証明ダイジェスト</dt><dd id="proof-digest">—</dd></div>
          <div><dt>現在の残高</dt><dd id="proof-balance">—</dd></div>
        </dl>
        <div class="actions">
          <button type="button" id="proof-copy">証明をコピー</button>
          <button type="button" id="proof-ranking" hidden>ランキングを見る</button>
          <button type="button" id="proof-history" class="secondary">履歴へ</button>
          <button type="button" id="proof-home" class="secondary">ホームへ</button>
        </div>
        <p id="proof-copy-status" class="note" role="status"></p>
      </div>

      <div id="screen-history" hidden>
        <h2>履歴（直近20件）</h2>
        <div id="history-list"></div>
        <div class="actions"><button type="button" id="history-back" class="secondary">ホームへ</button></div>
      </div>

      <div id="screen-guide" hidden>
        <h2>Muse を始める</h2>
        <p class="note">結果の読み解き（リーディング）は、ご自身の Muse を通じて利用できます。Muse をまだ使っていない方は、次の3ステップで始められます。</p>
        <h3>ステップ1：Muse に参加する</h3>
        <p><a href="https://muse.ai/join">https://muse.ai/join</a> から参加してください。</p>
        <h3>ステップ2：紹介コードを入力する（参加から48時間以内）</h3>
        <p>参加から48時間以内に、紹介コードを入力してください。Muse の左下の歯車をタップして設定を開き、「紹介コードを利用」をタップします（入力画面のタイトルは「招待コードを利用」と表示されます）。コード <strong>OHY932</strong> を入れて「確認」を押してください。</p>
        <img class="guide-img" src="/assets/referral-step-1.png" alt="Muse の左下の歯車をタップして設定を開く" loading="lazy">
        <img class="guide-img" src="/assets/referral-step-2.png" alt="設定の「紹介コードを利用」をタップ" loading="lazy">
        <img class="guide-img" src="/assets/referral-step-3.png" alt="入力画面でコード OHY932 を入れて「確認」を押す" loading="lazy">
        <h3>ステップ3：自分の Muse にアイクスSIの案内を読ませる</h3>
        <p>自分の Muse に <a href="${GUIDE_URL}">https://asi.iboga.jp/ai-agent-guide.txt</a> を読ませて、「アイクスSIを使いたい」と伝えてください。</p>
        <div class="actions">
          <button type="button" id="guide-back" class="secondary">戻る</button>
        </div>
      </div>

      <div id="screen-error" hidden>
        <h2 id="error-title">エラー</h2>
        <p id="error-text"></p>
        <div class="actions">
          <button type="button" id="error-home" class="secondary">ホームへ</button>
          <button type="button" id="error-rescan" hidden>再スキャンする（無料）</button>
        </div>
      </div>

      <aside><p id="app-footnote">${FOOTNOTE_FIXED}</p></aside>
    </section>
    <footer>パスワードをAIやチャットへ送る必要はありません。</footer>
  </main>`;

  const element = (id) => document.getElementById(id);
  const status = (text) => { element('status').textContent = text; };
  const SCREENS = ['login', 'home', 'scan', 'target-form', 'targets', 'wait',
    'ranking', 'reading-confirm', 'reading-wait', 'reading-result',
    'confirm', 'progress', 'proof', 'history', 'guide', 'error'];
  let pollTimer = null;
  const state = {
    session: null, targets: [], scanJob: null, scanView: null,
    confirmScanId: null, confirmation: null, preparedCount: null,
    scanKey: null, targetKey: null, irradiateKey: null, proofJob: null,
    guideReturn: 'home',
    readingKey: null, readingConfirmation: null, currentReading: null,
    referral: null,
  };

  function stopPoll() {
    if (pollTimer !== null) { clearTimeout(pollTimer); pollTimer = null; }
  }
  function show(name) {
    stopPoll();
    for (const screen of SCREENS) element('screen-' + screen).hidden = screen !== name;
  }
  function failToLogin() {
    show('login');
    status('ログインが必要です。');
  }
  function failGeneric() {
    show('error');
    element('error-title').textContent = '読み込めませんでした';
    element('error-text').textContent = '時間をおいて、もう一度開いてください。機器のメンテナンス中は、受け付けた順に処理されます。';
    element('error-rescan').hidden = true;
    status('エラーが発生しました。');
  }
  function handle(result) {
    if (result.kind === 'login') { failToLogin(); return null; }
    if (result.kind !== 'ok') { failGeneric(); return null; }
    return result.data;
  }
  function newKey() {
    return globalThis.crypto?.randomUUID ? globalThis.crypto.randomUUID()
      : 'app-' + Date.now() + '-' + Math.random().toString(36).slice(2);
  }

  async function loadSession() {
    const data = handle(await client.session());
    if (!data) return null;
    if (data.error) { failGeneric(); return null; }
    state.session = data;
    return data;
  }

  async function showHome() {
    const data = await loadSession();
    if (!data) return;
    element('home-balance').textContent = data.available === null || data.available === undefined
      ? '確認できませんでした' : `${data.available.toLocaleString('ja-JP')} クレジット`;
    element('home-price').textContent =
      `1回 ${data.credits_per_shot} クレジット。ユーザー数が過密すると、クレジット単価が上がる場合もあります`;
    element('app-footnote').textContent = data.reading?.enabled
      ? FOOTNOTE_READING : FOOTNOTE_FIXED;
    show('home');
    status('ホーム');
    const referralEnabled = Boolean(data.referral?.enabled);
    element('referral-card').hidden = !referralEnabled;
    element('sub-banner').hidden = !referralEnabled;
    if (referralEnabled) await loadReferral();
  }

  const REFERRAL_ERROR_TEXT = {
    invalid_code: 'コードが見つかりませんでした。入力を確認してください。',
    own_code: '自分の紹介コードは使えません。',
    already_redeemed: '紹介コードはすでに利用済みです。',
    window_closed: 'コードを入力できる期間（登録から48時間）を過ぎています。',
    cross_unconfigured: 'コードを確認できませんでした。時間をおいて、もう一度お試しください。',
  };

  function showGiftOverlay(amount) {
    element('gift-text').textContent =
      `${Number(amount).toLocaleString('ja-JP')}クレジットが届きました！`;
    const available = state.session?.available;
    element('gift-balance').textContent =
      available === null || available === undefined ? ''
        : `現在の残高：${Number(available).toLocaleString('ja-JP')} クレジット`;
    element('gift-overlay').hidden = false;
  }

  async function loadReferral() {
    const result = await client.referral();
    if (result.kind !== 'ok' || result.data.error) return false;
    const info = result.data;
    state.referral = info;
    element('referral-code').textContent = info.code || '—';
    element('referral-entry').hidden = Boolean(info.redeemed) || !info.window?.open;
    element('referral-done').hidden = !info.redeemed;
    // Unseen gifts surface as the celebration overlay (notification
    // only — the credits already landed when the code was redeemed),
    // then are marked seen so the overlay shows once.
    const gifts = Array.isArray(info.unseen_gifts) ? info.unseen_gifts : [];
    if (!gifts.length) return false;
    showGiftOverlay(gifts.reduce((sum, gift) => sum + (gift.amount || 0), 0));
    void client.markReferralGiftsSeen(gifts.map((gift) => gift.id));
    return true;
  }

  async function submitReferral() {
    const code = element('referral-input').value.trim();
    if (!code) {
      element('referral-status').textContent = 'コードを入力してください。';
      return;
    }
    const result = await client.redeemReferral(code);
    if (result.kind === 'login') { failToLogin(); return; }
    if (result.kind !== 'ok') { failGeneric(); return; }
    const data = result.data;
    if (data.error) {
      element('referral-status').textContent =
        REFERRAL_ERROR_TEXT[data.error] || 'コードを使えませんでした。';
      return;
    }
    element('referral-status').textContent = '紹介が成立しました！';
    await showHome();
    if (element('gift-overlay').hidden) showGiftOverlay(data.amount ?? 9999);
  }

  function showGuide(from) {
    state.guideReturn = from;
    show('guide');
    status('Muse の始め方（紹介コードの案内）');
  }

  async function loadTargets() {
    const data = handle(await client.targets());
    if (!data) return null;
    state.targets = Array.isArray(data.targets) ? data.targets : [];
    return state.targets;
  }

  async function showScan() {
    const targets = await loadTargets();
    if (!targets) return;
    const select = element('scan-target');
    select.replaceChildren();
    for (const target of targets) {
      const option = document.createElement('option');
      option.value = target.id;
      option.textContent = target.name;
      select.append(option);
    }
    element('scan-no-target').hidden = targets.length > 0;
    element('scan-submit').disabled = targets.length === 0;
    state.scanKey = newKey();
    show('scan');
    status('スキャンの入力');
  }

  async function showTargetForm() {
    state.targetKey = newKey();
    show('target-form');
    status('対象の登録');
  }

  async function showTargets() {
    const targets = await loadTargets();
    if (!targets) return;
    const list = element('target-list');
    list.replaceChildren();
    if (!targets.length) {
      const p = document.createElement('p');
      p.className = 'note';
      p.textContent = '登録済みの対象はありません。';
      list.append(p);
    }
    for (const target of targets) {
      const row = document.createElement('div');
      row.className = 'pack';
      const name = document.createElement('span');
      name.className = 'pack-name';
      name.textContent = target.name + (target.has_photo ? '' : '（写真なし）');
      const rename = document.createElement('button');
      rename.type = 'button'; rename.className = 'secondary'; rename.textContent = '名前の変更';
      rename.addEventListener('click', () => {
        const input = document.createElement('input');
        input.type = 'text'; input.value = target.name;
        const save = document.createElement('button');
        save.type = 'button'; save.textContent = '保存する';
        save.addEventListener('click', async () => {
          const result = await client.renameTarget(target.id, input.value);
          if (handle(result)) { status('名前を変更しました。'); void showTargets(); }
        });
        row.replaceChildren(name, input, save);
      });
      const remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'secondary'; remove.textContent = '削除';
      remove.addEventListener('click', async () => {
        if (remove.dataset.armed) {
          const result = await client.deleteTarget(target.id);
          if (handle(result)) { status('対象を削除しました（写真も消えました）。'); void showTargets(); }
        } else {
          remove.dataset.armed = '1';
          remove.textContent = '削除すると写真も消えます。もう一度押すと削除';
        }
      });
      row.append(name, rename, remove);
      list.append(row);
    }
    show('targets');
    status('対象の管理');
  }

  function pollJob(jobId, onTerminal) {
    stopPoll();
    const tick = async () => {
      const result = await client.job(jobId);
      if (result.kind === 'login') { failToLogin(); return; }
      if (result.kind !== 'ok') { failGeneric(); return; }
      const view = result.data;
      if (view.state === 'queued' || view.state === 'running' || view.state === 'unknown') {
        onTerminal(null, view);
        pollTimer = setTimeout(tick, 3000);
        return;
      }
      onTerminal(view, view);
    };
    pollTimer = setTimeout(tick, 800);
  }

  function showRanking(view) {
    state.scanView = view;
    const entries = view.result?.scan?.final_entries || [];
    const list = element('ranking-list');
    list.replaceChildren();
    for (const entry of entries) {
      const item = document.createElement('li');
      const field = document.createElement('strong');
      field.textContent = entry.field_name;
      item.append(field, document.createTextNode(`｜${entry.text}（ヒット数 ${entry.hits}）`));
      list.append(item);
    }
    element('copy-status').textContent = '';
    const reading = state.session?.reading;
    if (reading?.enabled) {
      element('ranking-notice').textContent = readingOnNotice(reading.credits);
      const button = element('ranking-reading');
      button.hidden = false;
      button.textContent = `リーディングする（${reading.credits}クレジット）`;
    } else {
      element('ranking-notice').textContent = NO_READING_NOTICE;
      element('ranking-reading').hidden = true;
    }
    show('ranking');
    status('スキャンが完了しました。共鳴ランキングを表示しています。');
  }

  async function prepareReading(scanId) {
    const result = await client.prepareReading(scanId);
    if (result.kind === 'login') { failToLogin(); return null; }
    if (result.kind !== 'ok') { failGeneric(); return null; }
    const data = result.data;
    if (data.error === 'scan_expired') {
      show('error');
      element('error-title').textContent = 'スキャンの有効期限が切れました';
      element('error-text').textContent = '照射していないスキャンは24時間で失効します。再スキャンは無料です。';
      element('error-rescan').hidden = false;
      status('スキャン失効');
      return null;
    }
    if (data.error) { failGeneric(); return null; }
    state.readingConfirmation = data;
    return data;
  }

  function renderReadingConfirm(data) {
    element('reading-confirm-price').textContent =
      `${data.quote.credits.toLocaleString('ja-JP')} クレジット`;
    element('reading-confirm-after').textContent =
      data.balance_after === null || data.balance_after === undefined
        ? '確認できませんでした' : `${data.balance_after.toLocaleString('ja-JP')} クレジット`;
    const insufficient = typeof data.balance_after === 'number' && data.balance_after < 0;
    element('reading-confirm-insufficient').hidden = !insufficient;
    element('reading-confirm-submit').disabled = insufficient;
    element('reading-confirm-submit').textContent =
      `${data.quote.credits}クレジットでリーディングする`;
  }

  async function showReadingConfirm(scanId) {
    state.readingKey = newKey();
    show('reading-confirm');
    status('リーディングの確認');
    const data = await prepareReading(scanId);
    if (data) renderReadingConfirm(data);
  }

  function readingFailure(text) {
    show('error');
    element('error-title').textContent = 'リーディングを作成できませんでした';
    element('error-text').textContent = text;
    element('error-rescan').hidden = true;
    status('リーディング失敗');
  }

  async function submitReading() {
    if (!state.readingConfirmation) return;
    show('reading-wait');
    status('リーディングを作成しています');
    let result = await client.createReading(
      state.readingConfirmation.confirmation_id, state.readingKey);
    if (result.kind === 'error') {
      // A lost response is recovered by replaying the same
      // request_key: the server answers with the stored outcome
      // and never charges or generates twice.
      await new Promise((resolve) => setTimeout(resolve, 1500));
      result = await client.createReading(
        state.readingConfirmation.confirmation_id, state.readingKey);
    }
    if (result.kind === 'login') { failToLogin(); return; }
    if (result.kind !== 'ok') { failGeneric(); return; }
    const data = result.data;
    if (data.error === 'reading_daily_cap') { readingFailure(READING_CAP_TEXT); return; }
    if (data.error === 'reading_unavailable' || data.error === 'reading_invalid_output') {
      readingFailure(READING_FAILED_TEXT);
      return;
    }
    if (data.error === 'reading_insufficient_balance') {
      status('残高が不足しています。');
      const fresh = await prepareReading(state.readingConfirmation.scan_id);
      if (fresh) { show('reading-confirm'); renderReadingConfirm(fresh); }
      return;
    }
    if (data.error === 'quote_changed' || data.error === 'confirmation_expired'
        || data.error === 'confirmation_not_found'
        || data.error === 'confirmation_already_used') {
      status('確認の有効期限が切れました。もう一度お試しください。');
      const fresh = await prepareReading(state.readingConfirmation.scan_id);
      if (fresh) { show('reading-confirm'); renderReadingConfirm(fresh); }
      return;
    }
    if (data.error) { failGeneric(); return; }
    state.readingKey = newKey();
    showReadingResult(data, state.scanView);
  }

  function showReadingResult(reading, scanView) {
    state.currentReading = reading;
    if (scanView) state.scanView = scanView;
    // Summary-first format (2026-11-22): the 【要約】 block renders
    // in its own box above the body; the stored text itself stays
    // whole (copy uses it as-is). Older readings have no summary
    // and render unsplit; expired text shows the retention notice.
    const summaryElement = element('reading-summary');
    if (reading.text_expired || !reading.text) {
      summaryElement.hidden = true;
      summaryElement.textContent = '';
      element('reading-text').textContent = READING_EXPIRED_TEXT;
    } else {
      const parts = splitReadingText(reading.text);
      summaryElement.hidden = !parts.summary;
      summaryElement.textContent = parts.summary ?? '';
      element('reading-text').textContent = parts.body;
    }
    element('reading-copy-status').textContent = '';
    element('reading-back').hidden = !state.scanView;
    show('reading-result');
    status('リーディングを表示しています。');
  }

  function waitTextFor(view, kindLabel) {
    if (view.state === 'queued') {
      const position = view.queue?.position;
      return position
        ? `${kindLabel}は順番待ちです（${position}番目）。順番に処理されます。そのままお待ちください。`
        : `${kindLabel}は順番待ちです。順番に処理されます。そのままお待ちください。`;
    }
    if (view.state === 'unknown') return '状態を確認しています。そのままお待ちください。';
    return `${kindLabel}を実行しています。そのままお待ちください。`;
  }

  function submitScan() {
    return (async () => {
      const targetId = element('scan-target').value;
      const intention = element('scan-intention').value;
      const joy = element('scan-joy').value;
      if (!targetId || !intention.trim() || !joy.trim()) {
        status('対象・意図・自己愛アファーメーションをすべて入れてください。');
        return;
      }
      const data = handle(await client.scan({
        request_key: state.scanKey, target_ids: [targetId],
        intention, self_love_joy: joy,
      }));
      if (!data) return;
      if (data.error) { status('スキャンを受け付けられませんでした。入力を確認してください。'); return; }
      state.scanJob = data.job_id;
      state.scanKey = newKey();
      show('wait');
      element('wait-title').textContent = 'スキャンを受け付けました';
      element('wait-text').textContent = '順番に処理されます。そのままお待ちください。';
      status('スキャン受付済み');
      pollJob(data.job_id, (terminal, view) => {
        if (!terminal) { element('wait-text').textContent = waitTextFor(view, 'スキャン'); return; }
        if (terminal.state === 'completed') { showRanking(terminal); return; }
        show('error');
        element('error-title').textContent = 'スキャンが完了しませんでした';
        element('error-text').textContent = 'スキャンは無料です。もう一度お試しください。';
        element('error-rescan').hidden = false;
        status('スキャン失敗');
      });
    })();
  }

  async function prepare(count) {
    const result = await client.prepareIrradiation(state.confirmScanId, count);
    if (result.kind === 'login') { failToLogin(); return null; }
    if (result.kind !== 'ok') { failGeneric(); return null; }
    const data = result.data;
    if (data.error === 'scan_expired') {
      show('error');
      element('error-title').textContent = 'スキャンの有効期限が切れました';
      element('error-text').textContent = '照射していないスキャンは24時間で失効します。再スキャンは無料です。';
      element('error-rescan').hidden = false;
      status('スキャン失効');
      return null;
    }
    if (data.error) { failGeneric(); return null; }
    state.confirmation = data;
    state.preparedCount = count;
    return data;
  }

  function renderConfirm(data) {
    const quote = data.quote;
    element('confirm-count-label').textContent = String(quote.count);
    element('confirm-strength').textContent = STRENGTH(quote.count);
    element('confirm-unit').textContent = `1回 ${quote.credits_per_shot} クレジット`;
    element('confirm-total').textContent = `${quote.credits.toLocaleString('ja-JP')} クレジット`;
    element('confirm-after').textContent = data.balance_after === null || data.balance_after === undefined
      ? '確認できませんでした' : `${data.balance_after.toLocaleString('ja-JP')} クレジット`;
    const insufficient = typeof data.balance_after === 'number' && data.balance_after < 0;
    element('confirm-insufficient').hidden = !insufficient;
    element('confirm-submit').disabled = insufficient;
  }

  async function showConfirm(scanId) {
    state.confirmScanId = scanId;
    state.irradiateKey = newKey();
    element('confirm-slider').value = '33';
    show('confirm');
    status('照射の確認');
    const data = await prepare(33);
    if (data) renderConfirm(data);
  }

  let prepareTimer = null;
  function queuePrepare() {
    const count = Number(element('confirm-slider').value);
    element('confirm-count-label').textContent = String(count);
    element('confirm-strength').textContent = STRENGTH(count);
    if (prepareTimer !== null) clearTimeout(prepareTimer);
    prepareTimer = setTimeout(async () => {
      prepareTimer = null;
      const data = await prepare(count);
      if (data) renderConfirm(data);
    }, 350);
  }

  async function submitIrradiation() {
    const count = Number(element('confirm-slider').value);
    if (!state.confirmation || state.preparedCount !== count) {
      const data = await prepare(count);
      if (!data) return;
      renderConfirm(data);
    }
    const data = handle(await client.irradiate(state.confirmation.confirmation_id,
      state.irradiateKey));
    if (!data) return;
    if (data.error === 'quote_changed') {
      status('単価が変わりました。確認画面の金額を見直してください。');
      const fresh = await prepare(count);
      if (fresh) renderConfirm(fresh);
      return;
    }
    if (data.error) { failGeneric(); return; }
    state.irradiateKey = newKey();
    show('progress');
    element('progress-text').textContent = '照射を受け付けました。順番に処理されます。';
    status('照射受付済み');
    pollJob(data.job_id, (terminal, view) => {
      if (!terminal) {
        const done = view.execution?.completed_count ?? 0;
        const total = state.confirmation?.quote?.count ?? '';
        element('progress-text').textContent = view.state === 'queued'
          ? waitTextFor(view, '照射')
          : `照射を実行しています（${done} / ${total} 回）。`;
        return;
      }
      void showProof(terminal.job_id || data.job_id);
    });
    element('progress-stop').onclick = async () => {
      const result = await client.cancelJob(data.job_id);
      if (handle(result)) status('停止を受け付けました。');
    };
  }

  async function showProof(jobId) {
    const data = handle(await client.job(jobId));
    if (!data) return;
    state.proofJob = data;
    element('proof-state').textContent = STATE_LABEL[data.state] || data.state;
    const completed = data.execution?.completed_count;
    const requested = data.execution?.requested_count;
    element('proof-count').textContent = requested === null || requested === undefined
      ? `${completed ?? 0} 回` : `${completed ?? 0} / ${requested} 回`;
    const settled = settledCreditsOf(data.settlement);
    element('proof-settled').textContent = settled === null ? '—' : `${settled.toLocaleString('ja-JP')} クレジット`;
    element('proof-device').textContent = data.device?.public_device_id || '—';
    const digest = data.proof_digest || '';
    element('proof-digest').textContent = digest ? digest.slice(0, 16) + '…' : '—';
    element('proof-ranking').hidden = !(data.operation === 'scan' && data.state === 'completed');
    const session = await loadSession();
    element('proof-balance').textContent = !session || session.available === null || session.available === undefined
      ? '確認できませんでした' : `${session.available.toLocaleString('ja-JP')} クレジット`;
    element('proof-copy-status').textContent = '';
    show('proof');
    status('稼働証明を表示しています。');
  }

  async function showHistory() {
    const data = handle(await client.history());
    if (!data) return;
    const list = element('history-list');
    list.replaceChildren();
    const sessions = Array.isArray(data.sessions) ? data.sessions : [];
    if (!sessions.length) {
      const p = document.createElement('p');
      p.className = 'note';
      p.textContent = 'まだ履歴はありません。';
      list.append(p);
    }
    for (const sessionRow of sessions) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'secondary';
      const when = new Date(sessionRow.created_at);
      const kindLabel = sessionRow.operation === 'scan' ? 'スキャン'
        : sessionRow.operation === 'reading' ? 'リーディング' : '照射';
      row.textContent = `${when.toLocaleString('ja-JP')}／${kindLabel}／`
        + `${STATE_LABEL[sessionRow.state] || sessionRow.state}／${sessionRow.cost.toLocaleString('ja-JP')} クレジット`;
      row.addEventListener('click', async () => {
        if (sessionRow.operation === 'reading') {
          const result = await client.reading(sessionRow.reading_id);
          const reading = handle(result);
          if (!reading) return;
          let scanView = null;
          if (reading.scan_id) {
            const jobResult = await client.job(reading.scan_id);
            if (jobResult.kind === 'ok' && !jobResult.data.error) scanView = jobResult.data;
          }
          state.scanView = scanView;
          showReadingResult(reading, scanView);
          return;
        }
        const view = handle(await client.job(sessionRow.job_id));
        if (!view) return;
        if (view.operation === 'scan' && view.state === 'completed') showRanking(view);
        else void showProof(sessionRow.job_id);
      });
      const wrap = document.createElement('div');
      wrap.className = 'actions';
      wrap.append(row);
      list.append(wrap);
    }
    show('history');
    status('履歴（直近20件）');
  }

  function readFileAsBase64(input) {
    return new Promise((resolve) => {
      const file = input.files && input.files[0];
      if (!file) { resolve(''); return; }
      const reader = new FileReader();
      reader.onload = () => {
        const text = String(reader.result || '');
        resolve(text.includes(',') ? text.slice(text.indexOf(',') + 1) : '');
      };
      reader.onerror = () => resolve('');
      reader.readAsDataURL(file);
    });
  }

  async function saveTarget() {
    const name = element('target-name').value;
    if (!name.trim()) { status('対象の名前を入れてください。'); return; }
    const photo = await readFileAsBase64(element('target-photo'));
    const photo2 = await readFileAsBase64(element('target-photo2'));
    if (!photo) { status('写真を選んでください。'); return; }
    const data = handle(await client.registerTarget({
      request_key: state.targetKey, name,
      birthday: element('target-birthday').value,
      gender: element('target-gender').value,
      photo_base64: photo, photo2_base64: photo2,
    }));
    if (!data) return;
    if (data.error) { status('対象を登録できませんでした。入力と写真を確認してください。'); return; }
    status('対象を登録しました。');
    void showScan();
  }

  async function copyText(text, statusElement, doneMessage) {
    try {
      await navigator.clipboard.writeText(text);
      statusElement.textContent = doneMessage;
    } catch {
      statusElement.textContent = 'コピーできませんでした。ブラウザの設定を確認してください。';
    }
  }

  // --- wiring ---
  element('login-button').addEventListener('click', () => { void auth.beginLogin(); });
  element('referral-copy').addEventListener('click', () => {
    const code = state.referral?.code;
    if (!code) return;
    void copyText(code, element('referral-copy-status'), '紹介コードをコピーしました。');
  });
  element('referral-submit').addEventListener('click', () => { void submitReferral(); });
  element('gift-close').addEventListener('click', () => {
    element('gift-overlay').hidden = true;
  });
  element('home-scan').addEventListener('click', () => { void showScan(); });
  element('home-targets').addEventListener('click', () => { void showTargets(); });
  element('home-history').addEventListener('click', () => { void showHistory(); });
  element('home-guide').addEventListener('click', () => { showGuide('home'); });
  element('ranking-guide').addEventListener('click', () => { showGuide('ranking'); });
  element('guide-back').addEventListener('click', () => {
    if (state.guideReturn === 'ranking' && state.scanView) showRanking(state.scanView);
    else void showHome();
  });
  element('scan-goto-register').addEventListener('click', () => { void showTargetForm(); });
  element('scan-submit').addEventListener('click', submitScan);
  element('scan-back').addEventListener('click', () => { void showHome(); });
  element('target-save').addEventListener('click', () => { void saveTarget(); });
  element('target-cancel').addEventListener('click', () => { void showScan(); });
  element('targets-add').addEventListener('click', () => { void showTargetForm(); });
  element('targets-back').addEventListener('click', () => { void showHome(); });
  element('ranking-copy').addEventListener('click', () => {
    const view = state.scanView;
    if (!view) return;
    const entries = view.result?.scan?.final_entries || [];
    void copyText(scanCopyText({
      intention: view.intention || '', selfLoveJoy: view.self_love_joy || '',
      entries, at: new Date(),
    }), element('copy-status'), '結果をコピーしました。ご自身の Muse に貼って読み解いてもらえます。');
  });
  element('ranking-reading').addEventListener('click', () => {
    if (state.scanView) void showReadingConfirm(state.scanView.job_id);
  });
  element('reading-confirm-submit').addEventListener('click', () => { void submitReading(); });
  element('reading-confirm-back').addEventListener('click', () => {
    if (state.scanView) showRanking(state.scanView);
  });
  element('reading-copy').addEventListener('click', () => {
    const reading = state.currentReading;
    if (!reading || !reading.text) return;
    const view = state.scanView;
    void copyText(readingCopyText({
      intention: view?.intention || '', selfLoveJoy: view?.self_love_joy || '',
      text: reading.text, at: new Date(),
    }), element('reading-copy-status'), 'リーディングをコピーしました。');
  });
  element('reading-irradiate').addEventListener('click', () => {
    if (state.currentReading) void showConfirm(state.currentReading.scan_id);
  });
  element('reading-back').addEventListener('click', () => {
    if (state.scanView) showRanking(state.scanView);
  });
  element('reading-done').addEventListener('click', () => { void showHome(); });
  element('ranking-irradiate').addEventListener('click', () => {
    if (state.scanView) void showConfirm(state.scanView.job_id);
  });
  element('ranking-rescan').addEventListener('click', () => { void showScan(); });
  element('ranking-done').addEventListener('click', () => { void showHome(); });
  element('confirm-slider').addEventListener('input', queuePrepare);
  for (const button of document.querySelectorAll('.preset')) {
    button.addEventListener('click', () => {
      element('confirm-slider').value = button.dataset.count;
      queuePrepare();
    });
  }
  element('confirm-submit').addEventListener('click', () => { void submitIrradiation(); });
  element('confirm-back').addEventListener('click', () => {
    if (state.scanView) showRanking(state.scanView);
  });
  element('proof-copy').addEventListener('click', () => {
    const view = state.proofJob;
    if (!view) return;
    void copyText(proofCopyText({
      state: STATE_LABEL[view.state] || view.state,
      requestedCount: view.execution?.requested_count,
      completedCount: view.execution?.completed_count,
      settledCredits: settledCreditsOf(view.settlement),
      publicDeviceId: view.device?.public_device_id,
      proofDigest: view.proof_digest, at: new Date(),
    }), element('proof-copy-status'), '証明をコピーしました。');
  });
  element('proof-ranking').addEventListener('click', () => {
    if (state.proofJob) showRanking(state.proofJob);
  });
  element('proof-history').addEventListener('click', () => { void showHistory(); });
  element('proof-home').addEventListener('click', () => { void showHome(); });
  element('history-back').addEventListener('click', () => { void showHome(); });
  element('error-home').addEventListener('click', () => { void showHome(); });
  element('error-rescan').addEventListener('click', () => { void showScan(); });

  void (async () => {
    await auth.completeRedirect();
    auth.takeFragment();
    const token = await auth.getToken();
    if (!token) { show('login'); status('ログインしてください。'); return; }
    await showHome();
  })();
}
