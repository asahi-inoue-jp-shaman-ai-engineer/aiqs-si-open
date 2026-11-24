import { createAppClient } from './app-api.js';
import { createAccountClient } from './account-api.js';
import { createAppAuth } from './app-auth.js';
import {
  clearPurchaseReturn, packLabel, savePurchaseReturn, shortageCredits,
  takePurchaseReturn,
} from './app-purchase.js';
import {
  reconcileStaleLoad, registeredTarget, targetsFromLoad,
  withRegisteredTarget, withRenamedTarget, withoutTarget,
} from './app-targets.js';
import { stepLabelFor } from './app-steps.js';
import { setBusy } from './app-busy.js';
import { createInFlight } from './app-inflight.js';
import { proofCopyText, readingCopyText, scanCopyText, settledCreditsOf, splitReadingText } from './app-copy.js';
import {
  createConfirmSession, createQuoteGate,
  isConfirmationCurrent, prepareDisposition, quoteMatchesCount,
} from './app-quote.js';
import { sessionChargeCredits } from './app-price.js';
import { createPoller } from './app-poll.js';
import {
  displayedRankingEntries, elapsedText, notifyWaitTextFor, proofHeadline,
  progressCountText, progressTitleFor, rankingCountText, stageTextFor,
  waitMailNoteFor,
} from './app-wait.js';
import { normalizePhotoFile } from './app-image.js';
import {
  IRRADIATION_REJECTED_ERRORS, IRRADIATION_REJECTED_RESPONSES,
  READING_REJECTED_ERRORS, READING_REJECTED_RESPONSES,
  REGISTRATION_REJECTED_ERRORS, SCAN_REJECTED_ERRORS,
  SCAN_REJECTED_RESPONSES,
  capturePaidOperation, capturePhotoSelection, captureRegistrationSubmission,
  captureScanSubmission, classifyMutationResult, createGenerationTracker,
  createPaidOperationLedger, createReadingFollowerRegistry,
  isIrradiationMutationSuccess,
  isReadingMutationInProgress, isReadingMutationSuccess,
  isRegistrationMutationSuccess, isRejectedResponse,
  isScanMutationSuccess, photoSelectionApplies, progressPercent,
  readingBinding, readingBindingApplies, readingConfirmRecalcApplies,
  readingConfirmSubmitEnabled, readingReplayPrecedence,
  retireRequestKey,
  scanViewForReading, submissionScreenAllowed,
  takeVerifiedStashedReadingOutcome, verifyTerminalReadingPoll,
} from './app-lifecycle.js';
import {
  OMAKASE_AFFIRMATION, createNavigator, createScanDraft, draftProblem,
  resolvedAffirmation,
} from './app-flow.js';
import {
  parseResumeToken, resumeFragmentOwned, resumeLandingFor,
  saveResumeToken, takeResumeToken,
} from './app-resume.js';
import {
  UPDATE_AVAILABLE_TEXT, UPDATE_DETAIL_TEXT, UPDATE_DISMISS_TEXT,
  UPDATE_RELOAD_TEXT, createVersionMonitor, extractBuildVersion,
  ownBuildVersion,
} from './app-version.js';

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
const SCAN_QUOTA_TEXT = '本日のスキャン上限に達しました。明日またお試しください。';
const SCAN_BUSY_TEXT = '現在スキャンが混み合っています。時間をおいてお試しください。';
const SCAN_UNAVAILABLE_TEXT = 'スキャンの準備ができていません。時間をおいてお試しください。';
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
  // 案B イマーシブ・グラス (2026-11-23): the glass design layer in
  // style.css is scoped to this class, so the other surfaces that
  // share the stylesheet (home, account pages) keep their look.
  document.body.classList.add('si-app');
  // 案B background (audit B MEDIUM-1): the browser chrome takes
  // the immersive-glass background colour, not the legacy dark
  // the shared shell page still declares for its other surfaces.
  const themeMeta = document.querySelector('meta[name="theme-color"]');
  if (themeMeta) themeMeta.setAttribute('content', '#04050e');
  // Capture the entry script's identity BEFORE the body is
  // replaced below: an entry script located in the body would
  // be gone from the document afterwards (its JS keeps running,
  // but querySelectorAll could no longer find it), which would
  // silently disable new-build detection (Astra 2026-11-23).
  // Entry-marked scripts come first, then module scripts, then
  // the rest — ownBuildVersion takes the first that parses.
  const ownScriptUrls = [...document.querySelectorAll('script[src]')]
    .map((script) => ({
      src: script.getAttribute('src'),
      entry: script.hasAttribute('data-app-entry'),
      module: script.getAttribute('type') === 'module',
    }))
    .sort((a, b) => Number(b.entry) - Number(a.entry)
      || Number(b.module) - Number(a.module))
    .map(({ src }) => src);
  // A completion-mail resume link (foundation item 2) may have
  // brought the user here: capture its token from the fragment
  // before anything can rewrite the URL, and stash it in
  // session storage so the landing survives the login
  // round-trip (the boot sequence consumes it after home is
  // reached). A fragment this module owns — a resume parameter
  // and no access_token — is stripped at once: the token does
  // not sit in the address bar. A fragment carrying an
  // access_token belongs to the state-bound handoff and is
  // left for it.
  const resumeToken = parseResumeToken(location.hash);
  if (resumeToken) {
    saveResumeToken(sessionStorage, resumeToken);
    if (resumeFragmentOwned(location.hash)) {
      history.replaceState(null, '', location.pathname + location.search);
    }
  }
  const auth = createAppAuth({
    fetch, location, history, storage: sessionStorage,
    persistentStorage: localStorage, crypto: globalThis.crypto,
  });
  const client = createAppClient({
    fetch, getToken: () => auth.getToken(),
    withToken: (dispatch) => auth.withCurrentToken(dispatch),
  });
  // The purchase guide (screen-purchase) reads the existing
  // account purchase payload — the same endpoint the standalone
  // purchase page renders — with the app's own token.
  const accountClient = createAccountClient({
    fetch, getToken: () => auth.getToken(),
    withToken: (dispatch) => auth.withCurrentToken(dispatch),
  });

  document.body.innerHTML = `<main>
    <div class="appbar">
      <div class="appbar-left">
        <button type="button" id="nav-back-button" class="glass-btn nav-back" aria-label="もどる" hidden><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg></button>
        <a class="brand" href="/"><b>AIQS</b> SI</a>
      </div>
      <button type="button" id="gear-button" class="glass-btn gear-button" aria-label="メニュー（履歴・アカウント・購入）" hidden><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg></button>
    </div>
    <section class="stage">
      <h1 id="app-title">アイクスSI</h1>
      <p id="status" role="status" aria-live="polite">読み込んでいます。</p>
      <p id="app-step" class="step-indicator" hidden></p>
      <div id="update-banner" class="update-banner" role="status" hidden>
        <p><strong id="update-banner-title"></strong><br><span id="update-banner-detail"></span></p>
        <div class="actions">
          <button type="button" id="update-reload"></button>
          <button type="button" id="update-dismiss" class="secondary"></button>
        </div>
      </div>

      <div id="screen-login" hidden>
        <div class="pulse-orb" aria-hidden="true"></div>
        <p class="lead">アイクスSIを、AIエージェントなしでそのまま使えるアプリです。スキャンは1日9回まで無料です。照射は回数分のクレジットで実行します。</p>
        <div class="actions"><button type="button" id="login-button">ログイン／新規登録</button></div>
        <p class="note">新規登録はメールアドレスと確認コードだけで完了します（招待コードは不要です）。ログイン画面で登録とログインの両方ができます。</p>
      </div>

      <div id="screen-home" hidden>
        <div class="pulse-orb" aria-hidden="true"></div>
        <dl class="pairing">
          <div><dt>残高</dt><dd id="home-balance">—</dd></div>
          <div><dt>照射の単価</dt><dd id="home-price">—</dd></div>
        </dl>
        <div class="actions">
          <button type="button" id="home-register" hidden>まず対象を登録する</button>
          <button type="button" id="home-scan">スキャンを始める</button>
        </div>
        <p class="note">履歴・アカウント・購入は、画面下のドックからいつでも開けます。</p>
      </div>

      <div id="screen-account" hidden>
        <h2>アカウント</h2>
        <dl class="pairing">
          <div><dt>残高</dt><dd id="account-balance">—</dd></div>
          <div><dt>照射の単価</dt><dd id="account-price">—</dd></div>
        </dl>
        <div id="referral-card" hidden>
          <h2>紹介コード</h2>
          <p class="note" id="referral-window-note" hidden></p>
          <details id="referral-details">
            <summary>紹介コードを使う・自分のコードを見る</summary>
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
          </details>
        </div>
        <aside id="sub-banner" class="sub-banner" hidden>
          <p class="sub-banner-label">関連プロダクト</p>
          <p class="sub-banner-title">テレスク（D-Planet）</p>
          <p>年額プラン69%オフ（3,716円/年）</p>
          <p><a href="https://tschool.iboga.jp/subscribe.html" target="_blank" rel="noopener">テレスクのサブスクを見る ↗</a></p>
        </aside>
        <div class="actions">
          <button type="button" id="account-purchase" class="secondary">クレジットを買う</button>
          <a class="button-link secondary" href="/account">アカウントページ</a>
          <button type="button" id="account-back" class="secondary">ホームへ</button>
        </div>
      </div>

      <div id="screen-purchase" hidden>
        <h2>クレジット購入</h2>
        <p class="note" id="purchase-shortage" hidden></p>
        <dl class="pairing">
          <div><dt>現在の残高</dt><dd id="purchase-balance">—</dd></div>
        </dl>
        <p>購入するパックを選んでください。1クレジット＝1円です。</p>
        <p class="note">クレジットは、支払いの確認が取れてから反映されます。反映まで数十秒かかることがあります。購入後の返金は原則としてお受けできません。</p>
        <p class="note">ユーザー数が過密すると、クレジット単価が上がる場合もあります。</p>
        <div id="purchase-packs" class="pack-list"></div>
        <div id="purchase-linked" hidden>
          <p>D-Planetと連携済みのアカウントは、クレジットをD-Planetで購入できます。購入したクレジットは、そのままアイクスSIでも使えます。</p>
        </div>
        <p class="note" id="purchase-status" role="status"></p>
        <div class="actions">
          <button type="button" id="purchase-back" class="secondary">もどる</button>
          <button type="button" id="purchase-home" class="secondary">ホームへ</button>
        </div>
      </div>

      <div id="screen-target" hidden>
        <h2>祈りを届ける相手は誰ですか？</h2>
        <p class="note form-error" id="target-load-error" role="alert" hidden>対象の一覧を取得できませんでした。時間をおいて、もう一度お試しください。</p>
        <div id="target-choices" class="choice-list"></div>
        <p class="note" id="target-empty" hidden>登録済みの対象がありません。下のボタンから登録してください。</p>
        <div class="actions flow">
          <button type="button" id="target-add">新しい対象を登録する</button>
          <button type="button" id="target-manage" class="secondary">対象の管理</button>
          <button type="button" id="target-back" class="secondary">ホームへ</button>
        </div>
        <p class="note">選ぶと、そのまま次へ進みます。</p>
      </div>

      <div id="screen-target-photo" hidden>
        <h2>対象の写真を選んでください</h2>
        <p class="note">顔が分かる写真がおすすめです。写真は観測の座標になります。</p>
        <label class="photo-pick">写真を選ぶ
          <input id="oq-photo" class="visually-hidden" type="file" accept="image/*">
        </label>
        <p class="note" id="oq-photo-name" role="status"></p>
        <p class="note form-error" id="oq-photo-error" role="alert" hidden></p>
        <div class="actions">
          <button type="button" id="oq-photo-next" disabled>この写真にする</button>
          <button type="button" id="oq-photo-back" class="secondary">もどる</button>
        </div>
      </div>

      <div id="screen-target-name" hidden>
        <h2>この対象の名前を教えてください</h2>
        <label>名前
          <input id="oq-name" type="text" autocomplete="off">
        </label>
        <p class="note">あとから変更できます。</p>
        <p class="note">登録した対象（名前と写真）はアイクスSIのサーバーに保存され、エージェント経由の利用でも同じ対象が使えます。削除は「対象の管理」からでき、削除すると写真も消えます。</p>
        <p class="note form-error" id="oq-name-error" role="alert" hidden></p>
        <div class="actions">
          <button type="button" id="oq-name-submit">登録して次へ</button>
          <button type="button" id="oq-name-back" class="secondary">もどる</button>
        </div>
      </div>

      <div id="screen-intention" hidden>
        <h2>どんな意図で祈りますか？</h2>
        <label>インテンション（意図）
          <textarea id="oq-intention" rows="3"></textarea>
        </label>
        <p class="note">意図は記録されます。共鳴の選定そのものには混ぜません。</p>
        <p class="note form-error" id="oq-intention-error" role="alert" hidden></p>
        <div class="actions">
          <button type="button" id="oq-intention-next">次へ</button>
          <button type="button" id="oq-intention-back" class="secondary">もどる</button>
        </div>
      </div>

      <div id="screen-affirmation" hidden>
        <h2>自己愛アファーメーション</h2>
        <p>「私は」から始まり、「自己愛」に関するアファーメーション文をセットすると、共鳴が強くなります。</p>
        <p class="note">例文: 「<span id="oq-affirmation-example"></span>」</p>
        <label>自分で決める
          <textarea id="oq-affirmation" rows="3"></textarea>
        </label>
        <p class="note">空欄のまま進むと、例文が自動で入ります。</p>
        <p class="note form-error" id="oq-affirmation-error" role="alert" hidden></p>
        <div class="actions flow">
          <button type="button" id="oq-affirmation-next">次へ</button>
          <button type="button" id="oq-affirmation-omakase" class="secondary">おまかせにして次へ</button>
          <button type="button" id="oq-affirmation-back" class="secondary">もどる</button>
        </div>
      </div>

      <div id="screen-scan-confirm" hidden>
        <h2>この内容でスキャンしますか？</h2>
        <dl class="pairing">
          <div><dt>対象</dt><dd id="oq-sum-target">—</dd></div>
          <div><dt>意図</dt><dd id="oq-sum-intention">—</dd></div>
          <div><dt>自己愛アファーメーション</dt><dd id="oq-sum-affirmation">—</dd></div>
          <div><dt>料金</dt><dd>無料</dd></div>
        </dl>
        <p class="note">スキャンは1日9回まで無料です。</p>
        <p class="note form-error" id="oq-confirm-error" role="alert" hidden></p>
        <div class="actions">
          <button type="button" id="oq-confirm-submit">スキャンを始める</button>
          <button type="button" id="oq-confirm-back" class="secondary">もどる</button>
        </div>
      </div>

      <div id="screen-wait-method" hidden>
        <h2 id="wait-method-title">少し時間がかかります</h2>
        <p class="note">待ち方を選んでください。</p>
        <div class="choice-list">
          <button type="button" id="wait-method-wait" class="choice-btn">
            <span class="choice-name">この画面で待つ</span>
            <span class="choice-meta" id="wait-method-wait-meta">進み具合を見ながら待ちます</span>
          </button>
          <button type="button" id="wait-method-notify" class="choice-btn">
            <span class="choice-name">通知で受け取る</span>
            <span class="choice-meta">画面を閉じて大丈夫。終わったらメールで知らせます</span>
          </button>
        </div>
        <p class="note" id="wait-method-note" hidden>メール通知は今は使えません。「この画面で待つ」から始めてください。</p>
        <div class="actions">
          <button type="button" id="wait-method-back" class="secondary">もどる</button>
        </div>
      </div>

      <div id="screen-targets" hidden>
        <h2>対象の管理</h2>
        <p class="note form-error" id="targets-load-error" role="alert" hidden>対象の一覧を取得できませんでした。時間をおいて、もう一度お試しください。</p>
        <div id="target-list"></div>
        <div class="actions">
          <button type="button" id="targets-add">対象を追加する</button>
          <button type="button" id="targets-back" class="secondary">ホームへ</button>
        </div>
      </div>

      <div id="screen-wait" hidden>
        <h2 id="wait-title">スキャンを受け付けました</h2>
        <div class="pulse-orb" aria-hidden="true"></div>
        <p id="wait-text">順番に処理されます。そのままお待ちください。</p>
        <p class="note" id="wait-elapsed" role="status"></p>
        <p class="note" id="wait-note">結果は履歴から確認できます。</p>
        <div class="skeleton" aria-hidden="true">
          <div class="skeleton-row"></div>
          <div class="skeleton-row"></div>
          <div class="skeleton-row"></div>
        </div>
      </div>

      <div id="screen-notify-wait" hidden>
        <h2 id="notify-wait-title">この画面は閉じて大丈夫です</h2>
        <p id="notify-wait-text">終わったら、メールでお知らせします。</p>
        <div class="actions">
          <button type="button" id="notify-wait-home" class="secondary">ホームへ</button>
        </div>
      </div>

      <div id="screen-ranking" hidden>
        <h2>共鳴ランキング</h2>
        <p class="note" id="ranking-count-note">共鳴ランキングの順に表示しています。</p>
        <ol id="ranking-list"></ol>
        <p class="note"><span id="ranking-notice">${NO_READING_NOTICE}</span>（<a href="${GUIDE_URL}">エージェント経由の案内</a>）</p>
        <div class="actions flow">
          <button type="button" id="ranking-next">次へ</button>
          <button type="button" id="ranking-copy" class="secondary">結果をコピー</button>
          <button type="button" id="ranking-guide" class="secondary">Muse を始める（紹介コードの案内）</button>
          <button type="button" id="ranking-rescan" class="secondary">もう一度スキャン</button>
          <button type="button" id="ranking-done" class="secondary">ホームへ戻る（ここまで）</button>
        </div>
        <p id="copy-status" class="note" role="status"></p>
      </div>

      <div id="screen-reading-confirm" hidden>
        <h2>ランキングをリーディングしますか？</h2>
        <p class="note">キーワード群・インテンション・自己愛アファーメーションの3つの視点から、共鳴ランキングを読み解きます。</p>
        <dl class="pairing">
          <div><dt>価格</dt><dd id="reading-confirm-price">—</dd></div>
          <div><dt>実行後の残高</dt><dd id="reading-confirm-after">—</dd></div>
        </dl>
        <p class="note">${READING_TRANSPARENCY}</p>
        <p class="note" id="reading-confirm-insufficient" hidden>残高が不足しています。下のボタンから購入できます。</p>
        <div class="actions flow">
          <button type="button" id="reading-confirm-purchase" hidden>クレジットを購入する</button>
          <button type="button" id="reading-confirm-submit">リーディングする</button>
          <button type="button" id="reading-confirm-skip" class="secondary">しないで照射へ進む</button>
          <button type="button" id="reading-confirm-back" class="secondary">ランキングへ戻る</button>
        </div>
      </div>

      <div id="screen-reading-wait" hidden>
        <h2 id="reading-wait-title">リーディングを送信しています</h2>
        <div class="pulse-orb" aria-hidden="true"></div>
        <p id="reading-wait-text">リーディングの依頼をサーバーへ送信しています。そのままお待ちください。</p>
        <p class="note" id="reading-wait-elapsed" role="status"></p>
      </div>

      <div id="screen-reading-result" hidden>
        <h2>リーディング</h2>
        <p class="note">AIによるリーディング</p>
        <div id="reading-summary" class="reading-summary" hidden></div>
        <p id="reading-text"></p>
        <div class="actions flow">
          <button type="button" id="reading-copy" class="secondary">リーディングをコピー</button>
          <button type="button" id="reading-irradiate">照射へ進む</button>
          <button type="button" id="reading-back" class="secondary">ランキングへ戻る</button>
          <button type="button" id="reading-done" class="secondary">ホームへ戻る（ここまで）</button>
        </div>
        <p id="reading-copy-status" class="note" role="status"></p>
      </div>

      <div id="screen-count" hidden>
        <h2>照射の回数は？</h2>
        <p class="count-now"><span id="count-now">33</span> 回（<span id="count-strength">弱</span>）</p>
        <input id="count-slider" type="range" min="1" max="99" step="1" value="33" aria-label="照射回数">
        <div class="actions">
          <button type="button" class="secondary preset" data-count="33">弱 33回</button>
          <button type="button" class="secondary preset" data-count="66">中 66回</button>
          <button type="button" class="secondary preset" data-count="99">強 99回</button>
        </div>
        <p class="note">強さの目安：弱 1〜33回／中 34〜66回／強 67〜99回</p>
        <dl class="pairing">
          <div><dt>単価</dt><dd id="count-unit">—</dd></div>
          <div><dt>合計</dt><dd id="count-total">—</dd></div>
        </dl>
        <p class="note">合計は目安です。確定額は、次の確認画面で表示します。</p>
        <p class="note">ユーザー数が過密すると、クレジット単価が上がる場合もあります。</p>
        <div class="actions">
          <button type="button" id="count-next">この回数で確認へ</button>
          <button type="button" id="count-back" class="secondary">もどる</button>
        </div>
      </div>

      <div id="screen-confirm" hidden>
        <h2>照射の確認</h2>
        <dl class="pairing">
          <div><dt>対象</dt><dd id="confirm-target">—</dd></div>
          <div><dt>インテンション</dt><dd id="confirm-intention">—</dd></div>
          <div><dt>自己愛アファーメーション</dt><dd id="confirm-affirmation">—</dd></div>
          <div><dt>回数</dt><dd id="confirm-count">—</dd></div>
          <div><dt>単価</dt><dd id="confirm-unit">—</dd></div>
          <div><dt>合計</dt><dd id="confirm-total">—</dd></div>
          <div><dt>残高</dt><dd id="confirm-balance">—</dd></div>
          <div><dt>照射後の残高</dt><dd id="confirm-after">—</dd></div>
        </dl>
        <p class="note">ユーザー数が過密すると、クレジット単価が上がる場合もあります。</p>
        <p class="note" id="confirm-insufficient" hidden>残高が不足しています（あと <span id="confirm-shortage">—</span> クレジット）。下のボタンから購入できます。</p>
        <div class="actions">
          <button type="button" id="confirm-submit">照射を始める</button>
          <button type="button" id="confirm-back" class="secondary">回数を選び直す</button>
        </div>
      </div>

      <div id="screen-progress" hidden>
        <h2 id="progress-title">照射</h2>
        <div class="pulse-orb" aria-hidden="true"></div>
        <p id="progress-text">照射を実行しています。</p>
        <p class="note" id="progress-count" role="status"></p>
        <div class="progress-track" role="progressbar" aria-label="照射の進み具合">
          <div id="progress-bar" class="progress-bar"></div>
        </div>
        <p class="note" id="progress-elapsed" role="status"></p>
        <p class="note" id="progress-note">稼働証明は履歴から確認できます。</p>
      </div>

      <div id="screen-proof" hidden>
        <h2>稼働証明</h2>
        <h3 id="proof-headline" hidden></h3>
        <dl class="pairing">
          <div><dt>状態</dt><dd id="proof-state">—</dd></div>
          <div><dt>完了回数</dt><dd id="proof-count">—</dd></div>
          <div><dt>精算クレジット</dt><dd id="proof-settled">—</dd></div>
          <div><dt>デバイス公開ID</dt><dd id="proof-device">—</dd></div>
          <div><dt>証明ダイジェスト</dt><dd id="proof-digest">—</dd></div>
          <div><dt>現在の残高</dt><dd id="proof-balance">—</dd></div>
        </dl>
        <div class="actions flow">
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
          <button type="button" id="error-retry" hidden>もう一度試す</button>
          <button type="button" id="error-home" class="secondary">ホームへ</button>
        </div>
      </div>

      <aside><p id="app-footnote">${FOOTNOTE_FIXED}</p></aside>
      <footer>パスワードをAIやチャットへ送る必要はありません。</footer>
    </section>
      <div id="gear-sheet" class="gear-sheet" hidden>
        <div class="gear-panel" role="dialog" aria-modal="true" aria-label="メニュー">
          <h2>メニュー</h2>
          <div class="gear-list">
            <button type="button" id="gear-history" class="choice-btn">
              <span class="choice-name">履歴</span>
              <span class="choice-meta">過去のスキャンと稼働証明</span>
            </button>
            <button type="button" id="gear-account" class="choice-btn">
              <span class="choice-name">アカウント</span>
              <span class="choice-meta">残高・紹介コード</span>
            </button>
            <button type="button" id="gear-purchase" class="choice-btn">
              <span class="choice-name">クレジット購入</span>
              <span class="choice-meta">残高 <span id="gear-balance">—</span></span>
            </button>
          </div>
          <p class="note" id="gear-note">結果や稼働証明は、履歴からいつでも確認できます。</p>
          <div class="actions"><button type="button" id="gear-close" class="secondary">閉じる</button></div>
        </div>
      </div>

      <div id="gift-overlay" class="gift-overlay" hidden>
        <div class="gift-panel" role="dialog" aria-modal="true" aria-labelledby="gift-text">
          <p class="gift-emoji">🎁</p>
          <p id="gift-text">9,999クレジットが届きました！</p>
          <p class="note" id="gift-balance"></p>
          <div class="actions"><button type="button" id="gift-close">閉じる</button></div>
        </div>
      </div>

    <div class="dock-zone" id="dock-zone" hidden>
      <nav class="dock" aria-label="メインナビゲーション">
        <button type="button" class="item" id="dock-home"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/><path d="M3 10a2 2 0 0 1 .709-1.528l7-5.999a2 2 0 0 1 2.582 0l7 5.999A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>ホーム</button>
        <button type="button" class="item" id="dock-history"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/></svg>履歴</button>
        <button type="button" class="item" id="dock-account"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="5"/><path d="M20 21a8 8 0 0 0-16 0"/></svg>アカウント</button>
        <button type="button" class="item" id="dock-purchase"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4Z"/><path d="M3 6h18"/><path d="M16 10a4 4 0 0 1-8 0"/></svg>購入</button>
      </nav>
    </div>
  </main>`;

  const element = (id) => document.getElementById(id);
  const status = (text) => { element('status').textContent = text; };
  const SCREENS = ['login', 'home', 'account', 'purchase', 'target', 'target-photo',
    'target-name', 'targets', 'intention', 'affirmation', 'scan-confirm',
    'wait-method', 'wait', 'notify-wait', 'ranking', 'reading-confirm',
    'reading-wait', 'reading-result',
    'count', 'confirm', 'progress', 'proof', 'history', 'guide', 'error'];
  // 案B (2026-11-23): the appbar back navigation (iOS HIG /
  // Material 3 grammar, DESIGN-NOTES 対応表) is the single back
  // control. It dispatches to the current screen's own back
  // button — the in-screen buttons stay in the DOM with their
  // exact handlers and are hidden by the design layer — so back
  // behaviour per screen is unchanged. Screens whose footer
  // buttons are destinations rather than a way back (ranking,
  // proof, notify-wait, error) are not in this map and keep
  // their in-screen buttons.
  const SCREEN_BACK = {
    account: 'account-back',
    purchase: 'purchase-back',
    target: 'target-back',
    'target-photo': 'oq-photo-back',
    'target-name': 'oq-name-back',
    targets: 'targets-back',
    intention: 'oq-intention-back',
    affirmation: 'oq-affirmation-back',
    'scan-confirm': 'oq-confirm-back',
    'wait-method': 'wait-method-back',
    'reading-confirm': 'reading-confirm-back',
    'reading-result': 'reading-back',
    count: 'count-back',
    confirm: 'confirm-back',
    history: 'history-back',
    guide: 'guide-back',
  };
  let elapsedTimer = null;
  // Submission guards live here, not on the buttons: a busy button
  // can be bypassed by navigating back and re-entering a screen,
  // so each submit handler holds its operation from its first
  // synchronous step until it settles.
  const inFlight = createInFlight();
  let elapsedStartedAt = 0;
  const state = {
    session: null, targets: [], scanJob: null, scanView: null,
    scanTargetNames: {},
    countScanId: null, selectedCount: 33, confirmInsufficient: false,
    confirmScanId: null, confirmation: null, preparedCount: null,
    confirmationScanId: null, confirmationGeneration: 0,
    currentScreen: null,
    scanKey: null, targetKey: null, irradiateKey: null, proofJob: null,
    targetsGeneration: 0, proofStep: null, waitStep: null, progressStep: null,
    guideReturn: 'home',
    readingKey: null, readingConfirmation: null, currentReading: null,
    referral: null,
    // Purchase guide (increment 4): where the guide was entered
    // from (its もどる target), and the shortage each quote left
    // behind — server-derived numbers only, kept so the guide
    // can state the exact amount still needed.
    purchaseReturn: null, confirmShortage: null, readingShortage: null,
    // The wait/notify choice (foundation item 2): the context
    // of the shared wait-method / notify-wait screens (which
    // operation they serve, and whether a refused notify
    // submission suppressed the notify choice for this visit),
    // and the irradiation choice the next submission freezes
    // into its operation record. The scan choice travels in
    // the submission snapshot instead (app-lifecycle.js).
    waitMethod: null, completionMode: 'wait',
    // The error screen's retry: the action the current error
    // offers to run again, or null when retrying cannot help.
    errorRetry: null,
    // One-question flow (app-flow.js): the scan draft the step
    // screens fill in, the back-stack for their もどる buttons,
    // and the normalized photo waiting for the name step of a
    // target registration.
    draft: createScanDraft(), nav: createNavigator('home'),
    pendingPhoto: null,
    // Astra 2026-11-23 (#2/#3/#9): the reading visit's binding
    // (scan + generation), whether its prepare has completed,
    // and the photo step's visit state. pendingPhotoName is the
    // filename captured with the selection, never re-read from
    // the file input after an await.
    readingBinding: null, readingPrepared: false,
    photoVisit: 0, photoResetPending: true, pendingPhotoName: '',
    // Delta review R2/R5: immutable operation records whose
    // outcomes are not yet known. A registration submission
    // (payload + request key, frozen together) survives a lost
    // response so recovery can reconcile with the SAME key
    // instead of minting a new one over an unknown outcome. The
    // paid operations (reading / irradiation) live in the
    // paidLedger below, keyed by (operation, scan) — round 2
    // (S2): one record per operation type let scan B overwrite
    // scan A's unresolved record.
    registrationSubmission: null,
    // Delta review round 2 (N3): while the confirmation screen
    // shows the recovery view for an unresolved irradiation,
    // this is that operation's frozen record; the submit button
    // reconciles IT, never the mutable confirmation state.
    confirmRecovery: null,
  };
  // Generation trackers (app-lifecycle.js): navigation (#5),
  // reading visits (#2), scan submissions (#4) and photo
  // selections (#3). A late response applies only while its
  // captured generation is still current.
  const navTracker = createGenerationTracker();
  const readingTracker = createGenerationTracker();
  const scanFlowTracker = createGenerationTracker();
  const photoSelectionTracker = createGenerationTracker();
  const photoVisitTracker = createGenerationTracker();
  // Unresolved paid operations (S2), scoped to this app
  // instance's authenticated session: the scope is set at boot
  // once a token exists and cleared when login is routed to, so
  // records never leak across accounts.
  const paidLedger = createPaidOperationLedger();
  // Follower coordination for in-progress readings (round 5,
  // N3): overlapping followers for one (operation, scan) are
  // generational — only the latest may settle the ledger
  // record; a superseded follower stashes the terminal outcome
  // it verified for the current one to consume. Session-scoped
  // memory like the ledger; entries are cleared on settle.
  const readingFollowers = createReadingFollowerRegistry();

  // Polling runs through the generation-guarded poller
  // (app-poll.js): stopPoll invalidates a poll whose request is
  // still in flight, not just the pending timer.
  const poller = createPoller({ fetchJob: (jobId) => client.job(jobId) });

  // New-build detection (app-version.js): the SPA never reloads
  // itself, so an open tab would run the old build forever. The
  // monitor compares the running bundle's content hash with the
  // one the server currently serves and only shows a banner —
  // the user reloads when it suits them, never automatically
  // mid-irradiation or mid-input. The banner copy lives in the
  // pure module so it is testable.
  element('update-banner-title').textContent = UPDATE_AVAILABLE_TEXT;
  element('update-banner-detail').textContent = UPDATE_DETAIL_TEXT;
  element('update-reload').textContent = UPDATE_RELOAD_TEXT;
  element('update-dismiss').textContent = UPDATE_DISMISS_TEXT;
  const versionMonitor = createVersionMonitor({
    ownVersion: ownBuildVersion(ownScriptUrls),
    fetchServerVersion: async () => {
      // The deadline covers the body read as well as the fetch:
      // a response that sends headers and then stalls must not
      // wedge version detection (the monitor also abandons a
      // fetch that never settles, as a second line of defense).
      // redirect: 'error' and the content-type check keep a
      // login page or a JSON error body from being read as the
      // app page (Astra 2026-11-23).
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 15_000);
      try {
        const response = await fetch('/app', {
          cache: 'no-store', redirect: 'error', signal: controller.signal,
        });
        if (!response?.ok) return null;
        const contentType = response.headers?.get?.('content-type') || '';
        if (contentType && !contentType.includes('text/html')) return null;
        return extractBuildVersion(await response.text());
      } catch {
        return null;
      } finally {
        clearTimeout(timer);
      }
    },
    onUpdateAvailable: () => { element('update-banner').hidden = false; },
    onUpdateResolved: () => { element('update-banner').hidden = true; },
  });
  function stopPoll() {
    poller.stop();
  }
  function stopElapsed() {
    if (elapsedTimer !== null) { clearInterval(elapsedTimer); elapsedTimer = null; }
  }
  // Quiet elapsed-seconds clock for the waiting screens: the total
  // duration is unknown, so the honest progress signal is the stage
  // text plus how long the user has been waiting. The first text is
  // rendered synchronously on entry — the previous version cleared
  // the element and wrote only on the first 1-second tick, so the
  // entry window showed no elapsed time at all (2026-11-22 live
  // check finding; see app-wait.js).
  function startElapsed(elementId) {
    stopElapsed();
    elapsedStartedAt = Date.now();
    const render = () => {
      const seconds = Math.floor((Date.now() - elapsedStartedAt) / 1000);
      const el = element(elementId);
      if (el) el.textContent = elapsedText(seconds);
    };
    render();
    elapsedTimer = setInterval(render, 1000);
  }
  // The appbar back button mirrors the current screen's own
  // back control (see SCREEN_BACK): visible only while that
  // control is present, shown and enabled. Extracted from
  // show() (audit B MEDIUM-3) so state changes made after
  // show() — a back control hidden/shown mid-visit — can
  // re-sync the appbar button instead of leaving it stale.
  function syncNavBack(name = state.currentScreen) {
    const backId = SCREEN_BACK[name];
    const backControl = backId ? element(backId) : null;
    element('nav-back-button').hidden = !(backControl && !backControl.hidden && !backControl.disabled);
  }
  function show(name) {
    stopPoll();
    stopElapsed();
    // Leaving the confirmation screen ends its visit: any quote
    // request still belonging to that visit must never apply to
    // whatever screen comes next (or to a later visit).
    if (name !== 'confirm') {
      confirmSession.invalidate();
    }
    state.currentScreen = name;
    // The gear button rides on every signed-in screen (the login
    // screen has no session to show a menu for), and any
    // navigation closes an open sheet.
    element('gear-button').hidden = name === 'login';
    // Closing through closeGear (not a bare hidden flip) also
    // lifts the background inert the sheet applied (gate #6).
    // The gift modal gets the same treatment: any navigation
    // releases it, so it can never be stranded over the
    // destination screen with that screen left inert
    // (delta audit HIGH-1).
    closeGear();
    hideGiftOverlay();
    // 案B chrome sync (visual layer): the floating dock rides on
    // the same screens as the gear button and marks the current
    // destination; the appbar back button shows only when the
    // current screen's own back control is present and visible
    // (the reading result's back control is itself conditional).
    element('dock-zone').hidden = name === 'login';
    for (const item of ['home', 'history', 'account', 'purchase']) {
      element('dock-' + item).classList.toggle('on', item === name);
    }
    syncNavBack(name);
    for (const screen of SCREENS) element('screen-' + screen).hidden = screen !== name;
    syncStepLabel(name);
  }
  // The step label for the stateful screens derives from the
  // job state the flow last recorded (batch 2e, R7): proof from
  // its job, wait/progress from theirs, the wait-method pair
  // from the flow context. Poll renders call this again as the
  // state moves, so the label never stays at the entry claim.
  function syncStepLabel(name) {
    const step = element('app-step');
    const label = name === 'proof' ? stepLabelFor('proof', state.proofStep)
      : name === 'wait' ? stepLabelFor('wait', state.waitStep)
        : name === 'progress' ? stepLabelFor('progress', state.progressStep)
          : (name === 'wait-method' || name === 'notify-wait')
            ? stepLabelFor(name, state.waitMethod)
            : stepLabelFor(name);
    step.hidden = !label;
    step.textContent = label || '';
  }
  // Navigation ownership (delta review R1, the rest of #5):
  // EVERY navigation intent goes through navigate(), which
  // first advances the navigation generation — retiring every
  // in-flight load/submission continuation — and retires the
  // reading visit when the flow is left. Continuations check
  // ownership BEFORE handle(), error routing or any state
  // application, so a late response (success OR failure) can
  // never replace a screen the user has since moved to. The
  // plain show() remains only for intra-visit re-renders that
  // must keep the visit alive (the reading confirmation →
  // reading wait transition, and re-rendering the reading
  // confirmation inside the same visit).
  function retireReadingVisit() {
    if (!state.readingBinding) return;
    readingTracker.begin();
    state.readingBinding = null;
    state.readingConfirmation = null;
    state.readingPrepared = false;
  }

  // Monotonic visit counter for deferred result acks (batch 2d,
  // MEDIUM 6 round 3): every navigation retires the previous
  // visit, and a visibility-deferred ack may fire only while
  // the visit that produced it is still the current one.
  let ackVisitCounter = 0;

  // Navigation-intent retirement (combined review finding 4):
  // an async navigation entry point expresses the intent BEFORE
  // its first await, and everything the previous screen left
  // running retires at the intent — not when the destination
  // finally renders. The navigation generation advances (late
  // loads discard themselves), the deferred-ack visit ends (a
  // result the user is leaving can no longer be acked as
  // viewed), and the previous job poll stops: its in-flight
  // response is dropped by the poller's own generation guard,
  // so a terminal poll can no longer override the destination
  // the user just chose. Entry points that load before they
  // navigate (showHome / showAccount / showTargets /
  // showHistory / showTargetPick / showProof / the history
  // detail load) begin their load with this; the synchronous
  // flow intents (flowGo / flowBack / startScanFlow /
  // startRegistration) go through it too, and navigate()
  // itself. No navigation intent advances the navigation
  // generation any other way (combined review round 2, F4).
  function beginNavIntent() {
    ackVisitCounter += 1;
    stopPoll();
    return navTracker.begin();
  }

  function navigate(name) {
    beginNavIntent();
    // A navigation intent retires EVERY in-flight submission
    // continuation at once (S1) — including the scan flow,
    // whose tracker general navigation previously never
    // invalidated, so a stale scan failure could replace the
    // account screen with an error. (Explicit retries re-begin
    // the scan tracker when they are invoked, so recovery keeps
    // working.)
    scanFlowTracker.begin();
    state.confirmRecovery = null;
    retireReadingVisit();
    show(name);
  }

  function failToLogin() {
    // The authenticated session is over: its unresolved paid
    // operation records go with it (S2 session scoping).
    paidLedger.setScope(null);
    navigate('login');
    status('ログインが必要です。');
  }
  // The one error shape (increment 4, DESIGN-NOTES screen 25):
  // a factual title and text, a retry when retrying can help,
  // and the way home — nothing else. The text states only what
  // the app knows (the load or the operation did not complete);
  // when the cause is unknown it is NOT asserted — an earlier
  // version of the generic text blamed maintenance the app had
  // no evidence for. Showing an error never clears the flow
  // draft, so もどる/再スキャン lands back on the user's own
  // inputs.
  function showErrorScreen({ title, text, retryLabel = null, onRetry = null, statusText = null }) {
    state.errorRetry = onRetry;
    element('error-title').textContent = title;
    element('error-text').textContent = text;
    const retry = element('error-retry');
    retry.hidden = !onRetry;
    retry.textContent = retryLabel || 'もう一度試す';
    navigate('error');
    status(statusText || title);
  }

  function failGeneric() {
    showErrorScreen({
      title: '読み込めませんでした',
      text: '時間をおいて、もう一度お試しください。',
      retryLabel: 'もう一度試す',
      onRetry: () => { void showHome(); },
      statusText: 'エラーが発生しました。',
    });
  }

  // Rescan after a scan-side failure or expiry: the retry must
  // not replay the failed scan's request key (the server would
  // return its stored outcome), so a fresh key is minted — and
  // the draft (target, intention, affirmation) is kept exactly
  // as entered, landing back on the scan confirmation. When
  // the draft itself is unusable (the failure arrived via a
  // history-reopened scan, with no flow behind it), the retry
  // falls back to a fresh flow from the target pick.
  function retryRescan() {
    if (draftProblem(state.draft)) { startScanFlow(); return; }
    state.scanKey = newKey();
    showScanConfirm();
  }

  function failScanExpired() {
    showErrorScreen({
      title: 'スキャンの有効期限が切れました',
      text: '照射していないスキャンは24時間で失効します。再スキャンは無料です。',
      retryLabel: '再スキャンする（無料）',
      onRetry: retryRescan,
      statusText: 'スキャン失効',
    });
  }

  // A completed scan whose result could not be loaded (batch
  // 2d, MEDIUM 8 round 3): NOT the expired screen — nothing
  // here establishes expiry — and no result ack: the user has
  // not seen a result. History is the recovery path.
  function failResultUnavailable() {
    showErrorScreen({
      title: '結果を読み込めませんでした',
      text: '結果の取得に失敗しました。時間をおいて、履歴から開き直してください。',
      retryLabel: '履歴を開く',
      onRetry: () => { void showHistory(); },
      statusText: '結果の取得に失敗しました。',
    });
  }

  // The documented missing-scan rejection (delta review
  // round 4, N2): the server answered 404 "Scan not found" —
  // the scan the paid operation was prepared against no
  // longer exists, and the operation was NOT executed (the
  // reading was refused before reservation; the irradiation's
  // confirmation claim was released). The caller has already
  // settled the record and retired the key through the
  // classification, so the only honest way forward is a fresh
  // scan from home — never a replay against the missing scan.
  function failScanNotFound() {
    showErrorScreen({
      title: 'スキャンが見つかりませんでした',
      text: '対象のスキャンが見つからなかったため、この操作は実行されていません。ホームからスキャンをやり直してください。',
      retryLabel: 'ホームへもどる',
      onRetry: () => { void showHome(); },
      statusText: 'スキャンが見つかりません',
    });
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

  // With a navGen, the response is applied only while that
  // navigation still owns the screen (R1): ownership is checked
  // BEFORE handle() can route an error or login over a newer
  // screen. Callers without a navigation (none today) pass null.
  async function loadSession(navGen = null) {
    const result = await client.session();
    if (navGen !== null && !navTracker.isCurrent(navGen)) return null;
    const data = handle(result);
    if (!data) return null;
    if (data.error) { failGeneric(); return null; }
    state.session = data;
    return data;
  }

  async function showHome() {
    const navGen = beginNavIntent();
    const data = await loadSession(navGen);
    if (!data || !navTracker.isCurrent(navGen)) return;
    element('home-balance').textContent = data.available === null || data.available === undefined
      ? '確認できませんでした' : `${data.available.toLocaleString('ja-JP')} クレジット`;
    element('home-price').textContent =
      `1回 ${data.credits_per_shot} クレジット。ユーザー数が過密すると、クレジット単価が上がる場合もあります`;
    element('app-footnote').textContent = data.reading?.enabled
      ? FOOTNOTE_READING : FOOTNOTE_FIXED;
    // First-run guidance: with no registered target yet, the
    // primary action is registering one — "scan" only becomes the
    // primary action once a target exists. A failed list fetch
    // leaves the default arrangement (the state is unknown, so the
    // home screen must not claim there are no targets).
    const loaded = await loadTargets();
    if (!loaded || !navTracker.isCurrent(navGen)) return;
    const noTargets = !loaded.failed && loaded.targets.length === 0;
    element('home-register').hidden = !noTargets;
    element('home-scan').classList.toggle('secondary', noTargets);
    navigate('home');
    // Arriving home is one of the version-check moments; the
    // monitor throttles, so frequent returns cost nothing.
    void versionMonitor.check();
    status('ホーム');
    // The referral card itself lives on the account screen now;
    // arriving home still runs the load so an unseen gift surfaces
    // as the celebration overlay (the overlay markup sits outside
    // the screens, so it can appear over any of them).
    if (data.referral?.enabled) await loadReferral(navTracker.current());
  }

  async function showAccount() {
    const navGen = beginNavIntent();
    const data = await loadSession(navGen);
    if (!data || !navTracker.isCurrent(navGen)) return;
    element('account-balance').textContent = data.available === null || data.available === undefined
      ? '確認できませんでした' : `${data.available.toLocaleString('ja-JP')} クレジット`;
    element('account-price').textContent =
      `1回 ${data.credits_per_shot} クレジット。ユーザー数が過密すると、クレジット単価が上がる場合もあります`;
    const referralEnabled = Boolean(data.referral?.enabled);
    element('referral-card').hidden = !referralEnabled;
    element('sub-banner').hidden = !referralEnabled;
    navigate('account');
    status('アカウント');
    if (referralEnabled) await loadReferral(navTracker.current());
  }

  // The purchase guide (increment 4, DESIGN-NOTES screen 24):
  // the in-app entrance to buying credits. It renders the
  // existing account purchase payload (packs, per-user payment
  // links, the linked-account guidance) and nothing else — the
  // payment itself still happens on the server-issued link,
  // exactly as on the standalone purchase page. Entries: the
  // gear sheet, the account screen, and the two shortage
  // branches (irradiation / reading confirmations), which hand
  // over the exact shortage their quote computed so the guide
  // can state the amount still needed. The balance is reloaded
  // on entry and rendered only from the server's answer; an
  // unreadable balance says so instead of showing a number.
  async function showPurchase(context = { from: 'home' }) {
    state.purchaseReturn = context;
    navigate('purchase');
    // Ownership for this visit is the generation navigate()
    // just opened; the continuations below check against it.
    const navGen = navTracker.current();
    status('クレジット購入');
    const shortage = typeof context.shortage === 'number' ? context.shortage : null;
    const shortageNote = element('purchase-shortage');
    shortageNote.hidden = shortage === null;
    if (shortage !== null) {
      const goal = context.from === 'reading' ? 'リーディング' : '照射';
      shortageNote.textContent = `残高が不足しています（あと ${shortage.toLocaleString('ja-JP')} クレジット）。`
        + `購入すると、確認画面に戻ってそのまま${goal}を始められます。`;
    }
    element('purchase-balance').textContent = '—';
    element('purchase-packs').replaceChildren();
    element('purchase-linked').hidden = true;
    element('purchase-status').textContent = '読み込んでいます。';
    const data = await loadSession(navGen);
    if (!data || !navTracker.isCurrent(navGen) || state.currentScreen !== 'purchase') return;
    element('purchase-balance').textContent = data.available === null || data.available === undefined
      ? '確認できませんでした' : `${data.available.toLocaleString('ja-JP')} クレジット`;
    const result = await accountClient.purchase();
    if (!navTracker.isCurrent(navGen) || state.currentScreen !== 'purchase') return;
    if (result.kind === 'login') { failToLogin(); return; }
    if (result.kind !== 'ok') {
      // A failed load is stated as a failure — never rendered
      // as "no packs" or "not ready" (the account client's
      // requireSuccess contract exists for exactly this).
      element('purchase-status').textContent =
        '購入案内を読み込めませんでした。時間をおいて、もう一度お試しください。';
      return;
    }
    const payload = result.data;
    if (payload.linked) {
      element('purchase-linked').hidden = false;
      element('purchase-status').textContent = '';
      return;
    }
    renderPurchasePacks(Array.isArray(payload.packs) ? payload.packs : []);
    element('purchase-status').textContent = payload.configured
      ? '' : '購入ページは準備中です。開始までお待ちください。';
  }

  function renderPurchasePacks(packs) {
    const list = element('purchase-packs');
    list.replaceChildren();
    for (const pack of packs) {
      const label = packLabel(pack);
      if (!label) continue;
      const row = document.createElement('div');
      row.className = 'pack';
      const name = document.createElement('span');
      name.className = 'pack-name';
      name.textContent = label;
      row.append(name);
      if (pack.url) {
        const link = document.createElement('a');
        link.className = 'button-link';
        link.href = pack.url;
        link.textContent = '購入ページへ';
        // Following the link leaves the app: record where to
        // land on return BEFORE the navigation, or clear an
        // older detour when this context has no way back.
        link.addEventListener('click', persistPurchaseReturn);
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
  }

  function persistPurchaseReturn() {
    const context = state.purchaseReturn;
    if (context && context.scanId) {
      savePurchaseReturn(sessionStorage, {
        scanId: context.scanId, count: context.count ?? null,
      });
    } else {
      clearPurchaseReturn(sessionStorage);
    }
  }

  // The guide's もどる returns to where it was opened from:
  // a shortage branch re-enters its confirmation (which
  // prepares a fresh quote against the current balance), the
  // account entry returns to the account screen, anything else
  // goes home.
  function purchaseBack() {
    const context = state.purchaseReturn;
    if (context?.from === 'confirm' && state.confirmScanId) {
      void showConfirm(state.confirmScanId);
      return;
    }
    if (context?.from === 'reading' && context.scanId) {
      // The return destination is the CAPTURED purchase
      // context's scan (round 2, N2): entering the guide
      // retired the reading visit and cleared its confirmation,
      // so depending on that object lost the way back and fell
      // through to Home. Resolve the scan's context and prepare
      // a FRESH reading confirmation for it.
      const scanId = context.scanId;
      void (async () => {
        const gen = navTracker.current();
        const view = await resolveScanView(scanId);
        if (!navTracker.isCurrent(gen)) return;
        if (!view) { void showHome(); return; }
        await showReadingConfirm(scanId);
      })();
      return;
    }
    if (context?.from === 'account') { void showAccount(); return; }
    void showHome();
  }

  // Boot-time landing for a purchase detour: the stored intent
  // is consumed once and the confirmation it names is rebuilt
  // from fresh server values (a new quote, the current
  // balance). A balance that has not caught up yet simply
  // shows the shortage branch again — the restore never
  // assumes the payment landed.
  async function restorePurchaseReturn() {
    const intent = takePurchaseReturn(sessionStorage);
    if (!intent || state.currentScreen !== 'home') return;
    // Restoration ownership (combined review round 3, F4):
    // the restore loads before it navigates, so it captures
    // the current navigation generation and re-checks it
    // before ANY continuation — error routing included. A
    // newer intent expressed while the scan fetch was in
    // flight (the replay: Rescan from home) retires the
    // restore; its late response must not replace the screen
    // the user has since built.
    const restoreGen = navTracker.current();
    if (typeof intent.count === 'number') {
      state.countScanId = intent.scanId;
      state.selectedCount = intent.count;
      await showConfirm(intent.scanId);
      if (navTracker.isCurrent(restoreGen)
          && state.currentScreen === 'confirm') {
        status('購入から戻りました。残高を確認して、照射を始められます。');
      }
    } else {
      // The reading confirmation needs its scan context back
      // (#10): fetch and validate the named scan and bind it as
      // the scan view BEFORE preparing. An expired or unreadable
      // scan is handled explicitly as an error, never restored
      // as a context-free confirmation whose ランキングへ戻る
      // silently does nothing.
      const scanResult = await client.job(intent.scanId);
      if (!navTracker.isCurrent(restoreGen)) return;
      if (scanResult.kind === 'login') { failToLogin(); return; }
      if (scanResult.kind !== 'ok' || scanResult.data.error
          || scanResult.data.operation !== 'scan'
          || scanResult.data.state !== 'completed') {
        showErrorScreen({
          title: '購入から戻れませんでした',
          text: '戻り先のスキャンを確認できませんでした。時間をおいて、履歴から結果を確認してください。',
          retryLabel: 'ホームへ',
          onRetry: () => { void showHome(); },
          statusText: '購入復帰エラー',
        });
        return;
      }
      state.scanView = scanResult.data;
      await showReadingConfirm(intent.scanId);
      if (navTracker.isCurrent(restoreGen)
          && state.currentScreen === 'reading-confirm') {
        status('購入から戻りました。残高を確認して、リーディングを始められます。');
      }
    }
  }

  // The one answer for a resume link that cannot be opened
  // (foundation item 2): the token may be unknown, expired or
  // malformed, or the job may be unreadable — the app does not
  // assert which, it states only that the link did not open
  // the result and points at the ordinary entrance (history,
  // via home).
  function showResumeLinkError() {
    showErrorScreen({
      title: 'リンクを開けませんでした',
      text: 'このリンクでは結果を開けませんでした。結果は履歴から確認できます。',
      retryLabel: 'ホームへ',
      onRetry: () => { void showHome(); },
      statusText: '再開リンクエラー',
    });
  }

  // Boot-time landing for a completion-mail resume link
  // (foundation item 2): the stashed token is consumed once
  // and redeemed against the user's own session; the job is
  // then loaded through the ordinary job view and the landing
  // follows the state THAT view reports — a completed scan
  // opens its ranking, an unfinished one its wait screen, an
  // irradiation its proof or progress screen. Nothing about
  // the job is taken from the link itself.
  async function restoreResumeLanding() {
    const token = takeResumeToken(sessionStorage);
    if (!token || state.currentScreen !== 'home') return;
    const navGen = navTracker.current();
    const redeemed = await client.resume(token);
    if (!navTracker.isCurrent(navGen)) return;
    if (redeemed.kind === 'login') { failToLogin(); return; }
    if (redeemed.kind !== 'ok' || redeemed.data.error
        || typeof redeemed.data.job_id !== 'string') {
      showResumeLinkError();
      return;
    }
    const jobId = redeemed.data.job_id;
    const viewResult = await client.job(jobId);
    if (!navTracker.isCurrent(navGen)) return;
    if (viewResult.kind === 'login') { failToLogin(); return; }
    if (viewResult.kind !== 'ok' || viewResult.data.error) {
      showResumeLinkError();
      return;
    }
    const view = viewResult.data;
    const landing = resumeLandingFor({
      operation: redeemed.data.operation, state: view.state,
      contentRemoved: view.content_removed === true,
    });
    if (landing === 'expired') { failScanExpired(); return; }
    if (landing === 'ranking') { showRanking(view); return; }
    if (landing === 'scan-wait') {
      state.scanJob = jobId;
      enterScanWait(view);
      return;
    }
    if (landing === 'proof') { void showProof(jobId, view); return; }
    if (landing === 'progress') { beginIrradiationProgress(view); return; }
    showResumeLinkError();
  }

  // Whether the completion-mail choice can be offered at all
  // (foundation item 2): the session response is the only
  // source — delivery configured AND a verified address on
  // the token. While false, the choice screen is skipped, the
  // submissions carry mode "wait" and no copy mentions mail
  // (device-consistency rule 9).
  function notifyAvailable() {
    return state.session?.notify?.available === true;
  }

  // The gear sheet: history / account / purchase, reachable from
  // every signed-in screen. Its note is neutral navigation
  // guidance only (combined review finding 3): the sheet is
  // global, opened over whatever job state the user left
  // behind, so it must not state that a job is still running
  // or promise a completion mail — a completed result whose
  // notification was already suppressed would make both
  // claims false. Per-job state and mail wording live on the
  // job's own screens, derived from its recorded state.
  // Focus ownership for the sheet (Astra gate #6): while the
  // sheet is open, the background (every sibling of the sheet
  // inside main — appbar, stage, dock) is inert, so Tab and
  // programmatic focus stay inside the sheet; closing restores
  // focus to whatever held it before (normally the gear
  // button). The element the sheet was opened from is captured
  // on the closed→open transition only.
  let gearReturnFocus = null;

  // Focus may only return to an element that is actually
  // visible: an element inside a since-hidden screen passes a
  // bare !el.hidden check while being unreachable, so the
  // whole ancestor chain is consulted (delta audit HIGH-1).
  function isVisibleForFocus(el) {
    if (!el || !el.isConnected) return false;
    for (let cur = el; cur; cur = cur.parentElement) {
      if (cur.hidden) return false;
    }
    return true;
  }

  function setGearBackgroundInert(sheet, inert) {
    for (const sibling of sheet.parentElement.children) {
      if (sibling !== sheet) sibling.inert = inert;
    }
  }

  function openGear() {
    hideGiftOverlay();
    const available = state.session?.available;
    element('gear-balance').textContent = available === null || available === undefined
      ? '—' : `${available.toLocaleString('ja-JP')} クレジット`;
    element('gear-note').textContent =
      '結果や稼働証明は、履歴からいつでも確認できます。';
    const sheet = element('gear-sheet');
    if (sheet.hidden) {
      gearReturnFocus = document.activeElement;
    }
    sheet.hidden = false;
    setGearBackgroundInert(sheet, true);
    element('gear-history').focus();
  }

  function closeGear() {
    const sheet = element('gear-sheet');
    const wasOpen = !sheet.hidden;
    sheet.hidden = true;
    if (!wasOpen) return;
    setGearBackgroundInert(sheet, false);
    // Never restore focus into an element that has since been
    // hidden (failToLogin hides the gear button itself):
    // focusing a hidden element is a no-op at best (MEDIUM-2).
    if (isVisibleForFocus(gearReturnFocus)) {
      gearReturnFocus.focus();
    }
    gearReturnFocus = null;
  }

  const REFERRAL_ERROR_TEXT = {
    invalid_code: 'コードが見つかりませんでした。入力を確認してください。',
    own_code: '自分の紹介コードは使えません。',
    already_redeemed: '紹介コードはすでに利用済みです。',
    window_closed: 'コードを入力できる期間（登録から48時間）を過ぎています。',
    cross_unconfigured: 'コードを確認できませんでした。時間をおいて、もう一度お試しください。',
  };

  // The gift notice is a modal (audit B round 2,
  // NEW-HIGH-1): it lives directly under <main> next to the
  // gear sheet (delta audit HIGH-1 — inside .stage its
  // stacking context trapped it under the appbar and the
  // dock), and while it is open every sibling under <main>
  // is inert — the same pattern as the gear sheet — with
  // focus moving into the dialog and back out on close.
  let giftReturnFocus = null;

  function setGiftBackgroundInert(overlay, inert) {
    for (const sibling of overlay.parentElement.children) {
      if (sibling !== overlay) sibling.inert = inert;
    }
  }

  function showGiftOverlay(amount) {
    closeGear();
    element('gift-text').textContent =
      `${Number(amount).toLocaleString('ja-JP')}クレジットが届きました！`;
    const available = state.session?.available;
    element('gift-balance').textContent =
      available === null || available === undefined ? ''
        : `現在の残高：${Number(available).toLocaleString('ja-JP')} クレジット`;
    const overlay = element('gift-overlay');
    if (overlay.hidden) {
      giftReturnFocus = document.activeElement;
    }
    overlay.hidden = false;
    setGiftBackgroundInert(overlay, true);
    element('gift-close').focus();
  }

  function hideGiftOverlay() {
    const overlay = element('gift-overlay');
    const wasOpen = !overlay.hidden;
    overlay.hidden = true;
    if (!wasOpen) return;
    setGiftBackgroundInert(overlay, false);
    if (isVisibleForFocus(giftReturnFocus)) {
      giftReturnFocus.focus();
    }
    giftReturnFocus = null;
  }

  async function loadReferral(navGen = null) {
    const result = await client.referral();
    // A newer navigation retires this load before it can paint
    // the referral card or surface the gift overlay (R1).
    if (navGen !== null && !navTracker.isCurrent(navGen)) return false;
    if (result.kind === 'login') { failToLogin(); return false; }
    if (result.kind !== 'ok' || result.data.error) return false;
    const info = result.data;
    state.referral = info;
    element('referral-code').textContent = info.code || '—';
    element('referral-entry').hidden = Boolean(info.redeemed) || !info.window?.open;
    element('referral-done').hidden = !info.redeemed;
    // The redemption window (48 hours from registration) and the
    // benefit live outside the collapsed details: a user who never
    // opens them must still see the deadline before it passes.
    const windowNote = element('referral-window-note');
    if (!info.redeemed && info.window?.open) {
      const closes = info.window.closes_at ? new Date(info.window.closes_at) : null;
      const deadline = closes && !Number.isNaN(closes.getTime())
        ? `${closes.toLocaleString('ja-JP')}まで` : '登録から48時間以内';
      windowNote.textContent = `紹介コードの入力は${deadline}・1回限りです。`
        + '成立すると、あなたと紹介した方の両方に9,999クレジットが届きます。';
      windowNote.hidden = false;
    } else {
      windowNote.hidden = true;
      windowNote.textContent = '';
    }
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
    const navGen = navTracker.current();
    const result = await client.redeemReferral(code);
    if (!navTracker.isCurrent(navGen)) return;
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
    navigate('guide');
    status('Muse の始め方（紹介コードの案内）');
  }

  // Resolves the scan view for a scan id (#10): the parked view
  // only when it IS that scan, otherwise a fresh fetch. Reading
  // actions resolve their context through here, by the reading's
  // own scan_id — never by assuming the parked view matches.
  async function resolveScanView(scanId) {
    if (!scanId) return null;
    if (state.scanView?.job_id === scanId) return state.scanView;
    const navGen = navTracker.current();
    const result = await client.job(scanId);
    // Ownership before routing (R1): a fetch that lands after a
    // newer navigation applies nothing, not even an error route.
    if (!navTracker.isCurrent(navGen)) return null;
    if (result.kind === 'login') { failToLogin(); return null; }
    if (result.kind !== 'ok' || result.data.error) return null;
    state.scanView = result.data;
    return result.data;
  }

  // Loads the target list without ever letting a failed fetch
  // masquerade as an empty list: on failure the last known targets
  // stay in state and the caller gets { failed: true } so it can
  // show the failure next to the target picker instead of the
  // "no targets" copy. Returns null only when the session is gone.
  async function loadTargets() {
    // Generation guard: snapshot the mutation generation and the
    // cache before awaiting. A registration, deletion or rename
    // that lands while this request is in flight bumps the
    // generation, and the late response — which describes an older
    // world — is reconciled with the newer local state instead of
    // overwriting it (a stale failure must not discard a target
    // that was just registered, a stale success must not bring
    // back one that was just deleted).
    const generation = state.targetsGeneration;
    const knownAtStart = state.targets;
    const result = await client.targets();
    const loaded = generation === state.targetsGeneration
      ? targetsFromLoad(state.targets, result)
      : reconcileStaleLoad(knownAtStart, state.targets, result);
    if (loaded.login) { failToLogin(); return null; }
    state.targets = loaded.targets;
    return loaded;
  }

  function formError(id, text) {
    const el = element(id);
    el.hidden = !text;
    el.textContent = text || '';
    if (text) status(text);
  }

  // --- One-question scan flow (app-flow.js owns the state) ---
  // Each step screen has exactly one action; picking/answering
  // advances immediately. The navigator remembers where each
  // screen's もどる lands, including the entries that start
  // outside the flow (home, the target manager).
  const flowRenderers = {
    home: () => showHome(),
    targets: () => showTargets(),
    target: () => showTargetPick(),
    'target-photo': () => showTargetPhoto(),
    'target-name': () => showTargetName(),
    intention: () => showIntention(),
    affirmation: () => showAffirmation(),
    'scan-confirm': () => showScanConfirm(),
    'wait-method': () => showWaitMethod(),
  };

  function flowGo(name) {
    // A flow step change is a navigation intent (R1): it retires
    // in-flight continuations from the previous screen at once,
    // not only when the (possibly async) renderer lands.
    beginNavIntent();
    retireReadingVisit();
    state.nav.go(name);
    flowRenderers[name]?.();
  }

  // Unsubmitted edits are saved to the draft before any
  // navigation (#9): typing and then pressing もどる must not
  // lose the edit, and re-entering the step shows it again.
  function saveFlowEdits() {
    if (state.currentScreen === 'intention') {
      state.draft.intention = element('oq-intention').value;
    }
    if (state.currentScreen === 'affirmation') {
      const value = element('oq-affirmation').value;
      if (value) {
        state.draft.affirmationMode = 'custom';
        state.draft.affirmation = value;
      } else if (state.draft.affirmationMode === 'custom') {
        state.draft.affirmation = value;
      }
    }
  }

  function flowBack() {
    saveFlowEdits();
    beginNavIntent();
    retireReadingVisit();
    // Leaving mid-submission supersedes that submission: its
    // late response must not take over the flow the user has
    // since started (#4).
    scanFlowTracker.begin();
    const name = state.nav.back();
    (flowRenderers[name] || flowRenderers.home)();
  }

  function startScanFlow() {
    beginNavIntent();
    retireReadingVisit();
    scanFlowTracker.begin();
    state.draft = createScanDraft();
    state.scanKey = newKey();
    state.nav.reset('target');
    void showTargetPick();
  }

  // Registration entry from outside the scan flow (home's
  // first-run button, the target manager's add button): the
  // stack keeps the origin so the photo step's もどる returns
  // there, and a finished registration continues into the flow.
  function startRegistration(origin) {
    beginNavIntent();
    retireReadingVisit();
    // A NEW registration starts from a fresh draft (#9): the
    // previous flow's intention/affirmation must not leak into
    // the target registered next, and the photo step is
    // explicitly reset for the new visit.
    state.draft = createScanDraft();
    state.photoResetPending = true;
    state.photoVisit = photoVisitTracker.begin();
    state.nav.reset(origin);
    flowGo('target-photo');
  }

  async function showTargetPick() {
    const navGen = beginNavIntent();
    const loaded = await loadTargets();
    if (!loaded || !navTracker.isCurrent(navGen)) return;
    const targets = loaded.targets;
    const list = element('target-choices');
    list.replaceChildren();
    for (const target of targets) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'choice-btn';
      const name = document.createElement('span');
      name.className = 'choice-name';
      name.textContent = target.name;
      const meta = document.createElement('span');
      meta.className = 'choice-meta';
      meta.textContent = target.has_photo ? '写真 登録済み' : '写真なし';
      button.append(name, meta);
      button.addEventListener('click', () => {
        state.draft.targetId = target.id;
        state.draft.targetName = target.name;
        flowGo('intention');
      });
      list.append(button);
    }
    element('target-load-error').hidden = !loaded.failed;
    // A failed fetch with nothing cached must not claim that no
    // targets are registered — that state is simply unknown.
    element('target-empty').hidden = targets.length > 0 || loaded.failed;
    navigate('target');
    status(loaded.failed ? '対象の一覧を取得できませんでした' : '対象の選択');
  }

  function showTargetPhoto() {
    // Two different entries share this renderer (#9): starting
    // a NEW registration resets the step, while re-rendering an
    // EXISTING step (もどる from the name step) preserves the
    // normalized photo and the chosen file's name for the rest
    // of the visit.
    if (state.photoResetPending) {
      state.photoResetPending = false;
      // An unresolved registration attempt (R5) keeps its frozen
      // key — and its frozen photo — for recovery; a fresh key is
      // minted only when no attempt is awaiting its outcome.
      state.targetKey = state.registrationSubmission
        ? state.registrationSubmission.requestKey : newKey();
      state.pendingPhoto = state.registrationSubmission
        ? state.registrationSubmission.photoBase64 : null;
      state.pendingPhotoName = '';
      photoSelectionTracker.begin();
      element('oq-photo').value = '';
      element('oq-photo-name').textContent = '';
      element('oq-photo-next').disabled = !state.pendingPhoto;
    } else {
      element('oq-photo-name').textContent = state.pendingPhotoName;
      element('oq-photo-next').disabled = !state.pendingPhoto;
    }
    formError('oq-photo-error', null);
    navigate('target-photo');
    status('対象の登録（写真）');
  }

  async function onPhotoChosen() {
    const input = element('oq-photo');
    const file = input.files && input.files[0];
    if (!file) return;
    // Snapshot the file and the selection generation BEFORE
    // awaiting (#3): the previous payload is cleared and Next
    // disabled at once, and the conversion result applies only
    // while this selection and this registration visit are
    // still current — a slower conversion for photo A can never
    // land under photo B's name.
    const selection = capturePhotoSelection(
      file, photoSelectionTracker.begin(), state.photoVisit);
    state.pendingPhoto = null;
    state.pendingPhotoName = selection.fileName;
    element('oq-photo-name').textContent = selection.fileName;
    element('oq-photo-next').disabled = true;
    formError('oq-photo-error', null);
    const result = await normalizePhotoFile(selection.file);
    if (!photoSelectionApplies(selection, {
      generation: photoSelectionTracker.current(), visit: state.photoVisit,
    })) return;
    if (!result.ok) {
      state.pendingPhoto = null;
      element('oq-photo-next').disabled = true;
      formError('oq-photo-error', PHOTO_ERROR_TEXT[result.reason]
        || '写真を読み込めませんでした。別の写真を選び直してください。');
      return;
    }
    if (!result.base64) {
      state.pendingPhoto = null;
      element('oq-photo-next').disabled = true;
      return;
    }
    state.pendingPhoto = result.base64;
    element('oq-photo-name').textContent = selection.fileName;
    element('oq-photo-next').disabled = false;
  }

  function showTargetName() {
    formError('oq-name-error', null);
    navigate('target-name');
    status('対象の登録（名前）');
  }

  async function submitTargetRegistration() {
    // The guard comes first — before anything async — so a
    // double tap cannot start two registrations in parallel.
    if (!inFlight.tryStart('target-save')) return;
    try {
      // Payload and request key are frozen TOGETHER (R5): while
      // a previous attempt's outcome is unknown, this submit
      // replays that exact record — the inputs currently on
      // screen, however edited, do not become a separate
      // operation until the frozen attempt resolves.
      let record = state.registrationSubmission;
      if (!record) {
        const name = element('oq-name').value;
        if (!name.trim()) {
          formError('oq-name-error', '対象の名前を入れてください。');
          return;
        }
        if (!state.pendingPhoto) {
          formError('oq-name-error', '写真を選んでください。');
          return;
        }
        record = captureRegistrationSubmission(
          { name, photoBase64: state.pendingPhoto }, state.targetKey);
        if (!record) return;
        state.registrationSubmission = record;
      }
      const name = record.name;
      formError('oq-name-error', null);
      const visit = state.photoVisit;
      // Navigation ownership for this continuation (S1): the
      // registration previously checked only the photo visit, so
      // a success that landed after the user went Home replaced
      // Home with the intention step.
      const navGen = navTracker.current();
      const restoreBusy = setBusy(element('oq-name-submit'), '対象を登録しています…');
      let result;
      try {
        result = await client.registerTarget({
          request_key: record.requestKey, name: record.name,
          birthday: '', gender: '',
          photo_base64: record.photoBase64, photo2_base64: '',
        });
      } finally {
        restoreBusy();
      }
      if (result.kind === 'login') { failToLogin(); return; }
      // Definitive-outcome classification (round 3, T1): kind
      // 'ok' alone does not end this attempt — a generic HTTP
      // 500 JSON body is 'ok' too, and its outcome is unknown.
      const outcome = classifyMutationResult(result, {
        isSuccess: isRegistrationMutationSuccess,
        rejectedErrors: REGISTRATION_REJECTED_ERRORS,
      });
      if (outcome === 'ambiguous') {
        // A superseded attempt's failure paints nothing (S1):
        // neither a newer registration visit nor a newer
        // navigation may be replaced by its error screen. The
        // frozen record AND its key stay for same-key recovery.
        if (visit !== state.photoVisit || !navTracker.isCurrent(navGen)) return;
        // Transport failure (#8) and ambiguous server failure
        // alike: the draft, the photo and the typed name are
        // all still here — the recovery returns to the name
        // step with them intact, and resubmitting replays the
        // frozen record under its original key.
        showErrorScreen({
          title: '対象を登録できませんでした',
          text: '通信が途中で切れた可能性があります。入力と写真はそのまま残っています。',
          retryLabel: '入力に戻る',
          onRetry: () => { showTargetName(); },
          statusText: '登録エラー',
        });
        return;
      }
      // Success or a known business rejection: this attempt has
      // DEFINITIVELY ended, so its frozen record is released AND
      // its request key is retired with it (R5 + round 2 S3) —
      // an edited resubmission must never send a different
      // payload under the same key. Both steps are identity-
      // guarded: a newer visit's record and key are never
      // touched. This is reconciliation, so it runs even when
      // the screen continuation below is stale.
      if (state.registrationSubmission === record) {
        state.registrationSubmission = null;
        state.targetKey = retireRequestKey(state.targetKey, record.requestKey, newKey);
      }
      // A newer registration visit or navigation supersedes
      // this response's SCREEN application (S1): the late
      // success reconciles above but must not take over the
      // screen the user has since moved to.
      if (visit !== state.photoVisit || !navTracker.isCurrent(navGen)) return;
      const data = result.data;
      if (outcome === 'rejected') {
        formError('oq-name-error', '対象を登録できませんでした。入力と写真を確認してください。');
        return;
      }
      status('対象を登録しました。');
      // Keep the new target in the local list and select it for
      // the flow, so a failed follow-up list fetch cannot make a
      // successful registration look like it never happened. The
      // generation bump also makes any list fetch started before
      // this registration reconcile instead of overwriting it.
      const created = registeredTarget(data, { name, hasPhoto2: false });
      state.pendingPhoto = null;
      if (!created) {
        // The response carried no usable id: fall back to a fresh
        // list rather than inventing a selection.
        state.nav.reset('target');
        void showTargetPick();
        return;
      }
      state.targets = withRegisteredTarget(state.targets, created);
      state.targetsGeneration += 1;
      state.draft.targetId = created.id;
      state.draft.targetName = created.name;
      // The flow continues from the pick step with the new target
      // selected; もどる from the intention step returns there.
      state.nav.reset('target');
      flowGo('intention');
    } finally {
      inFlight.end('target-save');
    }
  }

  function showIntention() {
    element('oq-intention').value = state.draft.intention;
    formError('oq-intention-error', null);
    navigate('intention');
    status('意図の入力');
  }

  function submitIntention() {
    const value = element('oq-intention').value;
    if (!value.trim()) {
      formError('oq-intention-error', '意図を入れてください。');
      return;
    }
    formError('oq-intention-error', null);
    state.draft.intention = value;
    flowGo('affirmation');
  }

  function showAffirmation() {
    element('oq-affirmation').value =
      state.draft.affirmationMode === 'custom' ? state.draft.affirmation : '';
    formError('oq-affirmation-error', null);
    navigate('affirmation');
    status('自己愛アファーメーションの入力');
  }

  function submitAffirmation(mode) {
    if (mode === 'omakase') {
      state.draft.affirmationMode = 'omakase';
    } else {
      const value = element('oq-affirmation').value;
      if (value.trim()) {
        state.draft.affirmationMode = 'custom';
        state.draft.affirmation = value;
      } else {
        // A blank field takes the delegated path too (Asahi
        // decision 2026-11-23, plan A): the fixed sentence shown
        // as the example on this screen is auto-filled, and the
        // confirmation screen shows that actual sentence.
        state.draft.affirmationMode = 'omakase';
      }
    }
    // A fresh request key for the confirmation visit: any edit
    // after a submission attempt passes through here again, so a
    // changed input never replays under the previous key, while
    // a retry from the confirmation screen itself (no edit)
    // keeps its key and replays idempotently.
    state.scanKey = newKey();
    flowGo('scan-confirm');
  }

  function showScanConfirm() {
    element('oq-sum-target').textContent = state.draft.targetName || '—';
    element('oq-sum-intention').textContent = state.draft.intention || '—';
    // The affirmation row shows the exact sentence that will be
    // submitted — for the delegated path, the fixed sentence
    // itself, never just the word "omakase" (display requirement
    // ①, 2026-11-23).
    const resolved = resolvedAffirmation(state.draft);
    element('oq-sum-affirmation').textContent = state.draft.affirmationMode === 'omakase'
      ? `${resolved}（おまかせ）` : resolved;
    formError('oq-confirm-error', null);
    navigate('scan-confirm');
    status('スキャンの内容確認');
  }

  // The wait-method choice (foundation item 2), one screen for
  // both flows: which operation it serves — and whether a
  // refused notify submission suppressed the notify choice for
  // this visit — comes from state.waitMethod. Picking a choice
  // IS the submission (one action per screen): "wait" submits
  // into the polling screens, "notify" submits into the
  // notify-wait screen. The screen exists only while the
  // session offers notify; the suppressed re-render after a
  // refusal is the one exception, and there the notify choice
  // is hidden rather than offered again.
  function showWaitMethod() {
    const context = state.waitMethod ?? { operation: 'scan', notifySuppressed: false };
    const irradiation = context.operation === 'irradiation';
    const suppressed = Boolean(context.notifySuppressed);
    element('wait-method-title').textContent = irradiation
      ? '照射には少し時間がかかります' : 'スキャンには少し時間がかかります';
    element('wait-method-wait-meta').textContent = irradiation
      ? '進み具合を見ながら待ちます' : '波紋を見ながら待ちます';
    if (!suppressed) {
      element('wait-method-wait-meta').textContent
        += '。閉じた場合は、終わったらメールでお知らせします';
    }
    element('wait-method-notify').hidden = suppressed;
    element('wait-method-note').hidden = !suppressed;
    navigate('wait-method');
    status('待ち方の選択');
  }

  function submitWaitMethodChoice(mode) {
    if (state.waitMethod?.operation === 'irradiation') {
      state.completionMode = mode;
      void submitIrradiation();
      return;
    }
    void submitScanFlow(mode);
  }

  // The notify-wait screen (foundation item 2): the job was
  // accepted with mode "notify", so this screen's whole job is
  // the declaration that closing is safe — the completion mail
  // carries the resume link back to the result / proof. It
  // shows no address (the server never returns one) and no
  // expected time (nothing has reported one).
  //
  // Every sentence derives from the submission response itself
  // (batch 2d, MEDIUM 7): the screen used to declare
  // "照射は進んでいます" for whatever state the job was
  // actually in — queued, finished, failed, notification
  // suppressed — a state the server never reported. A response
  // that already reports a terminal state earns no notify-wait
  // screen at all: the caller falls back to the ordinary wait /
  // progress path, whose poller surfaces the outcome at once.
  function enterNotifyWait(operation, data) {
    const texts = notifyWaitTextFor({
      operation,
      state: data?.state,
      notification: data?.completion?.notification,
      deliverable: data?.completion?.deliverable === true,
    });
    if (!texts) return false;
    state.waitMethod = { operation, notifySuppressed: false };
    element('notify-wait-title').textContent = texts.title;
    element('notify-wait-text').textContent = texts.text;
    navigate('notify-wait');
    status(texts.statusText);
    return true;
  }

  async function showTargets() {
    const navGen = beginNavIntent();
    const loaded = await loadTargets();
    if (!loaded || !navTracker.isCurrent(navGen)) return;
    const targets = loaded.targets;
    element('targets-load-error').hidden = !loaded.failed;
    const list = element('target-list');
    list.replaceChildren();
    if (!targets.length && !loaded.failed) {
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
          const navGen = navTracker.current();
          const result = await client.renameTarget(target.id, input.value);
          if (!navTracker.isCurrent(navGen)) return;
          const data = handle(result);
          if (!data) return;
          // Reflect the rename in the local cache at once: the
          // follow-up list fetch may fail, and the failure-
          // preserving load must not revert to the old name.
          if (data.renamed === true) {
            state.targets = withRenamedTarget(state.targets, target.id, input.value);
            state.targetsGeneration += 1;
          }
          status('名前を変更しました。');
          void showTargets();
        });
        row.replaceChildren(name, input, save);
      });
      const remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'secondary'; remove.textContent = '削除';
      remove.addEventListener('click', async () => {
        if (remove.dataset.armed) {
          const navGen = navTracker.current();
          const result = await client.deleteTarget(target.id);
          if (!navTracker.isCurrent(navGen)) return;
          const data = handle(result);
          if (!data) return;
          // Reflect the deletion in the local cache at once: the
          // follow-up list fetch may fail, and the failure-
          // preserving load must not resurrect the deleted target.
          if (data.deleted === true) {
            state.targets = withoutTarget(state.targets, target.id);
            state.targetsGeneration += 1;
          }
          status('対象を削除しました（写真も消えました）。');
          void showTargets();
        } else {
          remove.dataset.armed = '1';
          remove.textContent = '削除すると写真も消えます。もう一度押すと削除';
        }
      });
      row.append(name, rename, remove);
      list.append(row);
    }
    navigate('targets');
    status(loaded.failed ? '対象の一覧を取得できませんでした' : '対象の管理');
  }

  // The "I saw the result" ack (batch 2d, MEDIUM 6): the
  // status GET is side-effect free, so a view is recorded only
  // when a terminal result is actually rendered — the ranking,
  // the proof, a terminal failure screen. Fire-and-forget: a
  // failed ack must never disturb the result on screen (the
  // worst case is a completion mail that arrives anyway), and
  // the server itself refuses to record views for in-flight
  // jobs, so an over-eager call site cannot cancel a mail
  // still owed.
  // 'unknown' is not acknowledgeable (round 2): it is an
  // in-flight state for the poller, and its outcome may still
  // be resolved — the server refuses to record those views too.
  const TERMINAL_STATES = new Set(['completed', 'failed', 'cancelled']);
  function ackResultViewed(jobId) {
    if (typeof jobId !== 'string' || !jobId) return;
    // A hidden document has shown nobody anything (round 2):
    // defer the ack until the document is actually visible.
    // The deferral is owned by the visit that produced it
    // (round 3): if any navigation retires that visit before
    // the document becomes visible, the ack dies with it —
    // the result it belonged to was never presented.
    if (typeof document !== 'undefined' && document.hidden) {
      const visit = ackVisitCounter;
      document.addEventListener('visibilitychange', function onVisible() {
        if (document.hidden) return;
        document.removeEventListener('visibilitychange', onVisible);
        if (ackVisitCounter !== visit) return;
        void client.markViewed(jobId);
      });
      return;
    }
    void client.markViewed(jobId);
  }

  // Poll one job: onUpdate receives each non-terminal view,
  // onTerminal the terminal view (the full payload, so callers
  // can render from it directly). The poller's generation guard
  // drops responses that land after navigation or after a newer
  // polling session started.
  function pollJob(jobId, onUpdate, onTerminal) {
    poller.start(jobId, (result) => {
      if (result.kind === 'login') { failToLogin(); return 'stop'; }
      if (result.kind !== 'ok') { failGeneric(); return 'stop'; }
      const view = result.data;
      // A non-2xx JSON error body (503 server_busy, 429
      // rate_limited) also arrives as kind 'ok', carrying no
      // state at all: it is not a job view, and treating it as
      // a terminal one once rendered a false failure and an
      // empty proof (Antigravity glass audit, finding 4). Only
      // a payload with a recognized state may drive screens.
      if (result.status < 200 || result.status >= 300
          || !view || typeof view.state !== 'string' || view.error) {
        failGeneric();
        return 'stop';
      }
      if (view.state === 'queued' || view.state === 'running' || view.state === 'unknown') {
        onUpdate(view);
        return 'continue';
      }
      if (!TERMINAL_STATES.has(view.state)) { failGeneric(); return 'stop'; }
      onTerminal(view);
      return 'stop';
    });
  }

  function showRanking(view) {
    // Usability guard at the shared entry point (batch 2d,
    // MEDIUM 8): every route into the ranking — polling,
    // resume landing, history, the reading screen's back path
    // — passes through here, so the guard lives here, before
    // any rendering or ack. A ranking may be presented only
    // from a usable completed-scan view (operation 'scan' with
    // a result payload). Anything else completed-shaped is
    // routed by its evidence: explicit content removal goes to
    // the expired screen; an incomplete or degraded view (the
    // server's fallback shape carries operation null and no
    // result) goes to a plain unavailable screen with NO ack —
    // unavailability is not expiry, and an unseen result must
    // not be recorded as seen.
    if (view?.state === 'completed') {
      // Explicit removal evidence takes precedence over any
      // payload the view still carries (round 4): the cleanup
      // that removes scan content can leave a synthesized scan
      // object behind, and removal — not the leftover shape —
      // is the truth about this result. The expiry branch is
      // scan-only (round 5): the removal flag also exists on
      // irradiation views, and an irradiation view reaching
      // this guard (no ordinary path does) must fail usability
      // below, not borrow the scan's expiry explanation.
      if (view.content_removed === true && view.operation === 'scan') {
        failScanExpired();
        return;
      }
      if (!(view.operation === 'scan' && view.result?.scan)) {
        failResultUnavailable();
        return;
      }
    }
    state.scanView = view;
    // Default display is the top 10 entries (2026-11-23
    // decision); the copy action still carries the full list.
    const entries = displayedRankingEntries(view.result?.scan?.final_entries);
    const list = element('ranking-list');
    list.replaceChildren();
    entries.forEach((entry, index) => {
      const item = document.createElement('li');
      item.className = 'ranking-item';
      item.style.animationDelay = `${index * 60}ms`;
      const field = document.createElement('strong');
      field.textContent = entry.field_name;
      item.append(field, document.createTextNode(`｜${entry.text}（ヒット数 ${entry.hits}）`));
      list.append(item);
    });
    element('copy-status').textContent = '';
    // The note states the number of entries actually on screen:
    // the server may return fewer than the cap (the 2026-11-22
    // live check saw 8 entries under a note claiming 10).
    element('ranking-count-note').textContent = rankingCountText(entries.length);
    // The one primary action is "次へ": with the in-app reading
    // enabled it leads to the reading question screen, otherwise
    // straight to the irradiation confirmation. The reading
    // offer itself lives on that question screen, not here.
    const reading = state.session?.reading;
    element('ranking-notice').textContent = reading?.enabled
      ? readingOnNotice(reading.credits) : NO_READING_NOTICE;
    navigate('ranking');
    status('スキャンが完了しました。共鳴ランキングを表示しています。');
    // The ack follows the completed render (round 3): it is
    // recorded only once this ranking is actually presented.
    ackResultViewed(view.job_id);
  }

  // A prepare response applies only to the reading visit that
  // asked for it (#2): a late answer for scan A must never
  // overwrite scan B's confirmation, and its errors must not
  // tear down B's screen either. Login still routes globally.
  async function prepareReading(scanId, binding = null) {
    // Navigation ownership travels with the preparation (round
    // 3, T2): a submission-triggered refresh (quote_changed and
    // friends) captured only the reading binding, so when an
    // Account navigation STARTED while the refresh was pending,
    // the preparation's error routing ran before the caller's
    // ownership check and replaced the pending navigation with
    // the error screen. The navigation generation captured
    // here is checked BEFORE any error routing or state
    // application below. Login still routes globally.
    const navGen = navTracker.current();
    const result = await client.prepareReading(scanId);
    if (result.kind === 'login') { failToLogin(); return null; }
    if (!navTracker.isCurrent(navGen)) return null;
    if (binding && !readingBindingApplies(binding, state.readingBinding)) return null;
    if (result.kind !== 'ok') { failGeneric(); return null; }
    const data = result.data;
    if (data.error === 'scan_expired') { failScanExpired(); return null; }
    if (data.error) { failGeneric(); return null; }
    if (data.scan_id && data.scan_id !== scanId) return null;
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
    // The way out of a shortage is the purchase guide, in
    // place (increment 4): the button appears with the note,
    // carrying the exact shortage the quote left behind.
    element('reading-confirm-purchase').hidden = !insufficient;
    state.readingShortage = shortageCredits(data.balance_after);
    // Eligibility is the pure readingConfirmSubmitEnabled
    // (round 8): this data IS the prepared confirmation for
    // this visit, so `prepared` is true here — the live
    // prepared flag is set just below, as before.
    element('reading-confirm-submit').disabled = !readingConfirmSubmitEnabled({
      confirmation: data, prepared: true, inFlightActive: inFlight.isActive('reading'),
    });
    element('reading-confirm-submit').textContent =
      `リーディングする（${data.quote.credits}クレジット）`;
    // The prepare for THIS scan has completed: the dependent
    // actions (submit was handled above, skip) may act now.
    state.readingPrepared = true;
    element('reading-confirm-skip').disabled = false;
  }

  async function showReadingConfirm(scanId) {
    // Entry clears the previous scan's confirmation and money
    // displays and disables every dependent action until THIS
    // scan's prepare completes (#2). The visit is bound to the
    // scan id plus a fresh generation, and the request key is
    // frozen for the whole visit (transport retries reuse it).
    const binding = readingBinding(scanId, readingTracker.begin());
    // Entry is a navigation: it retires the previous visit
    // (navigate clears state.readingBinding), so the new visit's
    // binding is installed AFTER the navigation lands.
    navigate('reading-confirm');
    state.readingBinding = binding;
    state.readingConfirmation = null;
    state.readingPrepared = false;
    state.readingShortage = null;
    state.readingKey = newKey();
    element('reading-confirm-price').textContent = '—';
    element('reading-confirm-after').textContent = '—';
    element('reading-confirm-insufficient').hidden = true;
    element('reading-confirm-purchase').hidden = true;
    element('reading-confirm-submit').disabled = true;
    element('reading-confirm-submit').textContent = 'リーディングする';
    element('reading-confirm-skip').disabled = true;
    status('リーディングしますか？');
    const data = await prepareReading(scanId, binding);
    if (data && readingBindingApplies(binding, state.readingBinding)) {
      renderReadingConfirm(data);
    }
  }

  function readingFailure(text) {
    // A creation failure can be retried from the reading
    // confirmation (a fresh prepare); the daily cap cannot —
    // retrying today would fail the same way, so it offers no
    // retry button rather than a useless one.
    const scanId = state.readingBinding?.scanId ?? state.readingConfirmation?.scan_id;
    const retryable = text === READING_FAILED_TEXT && Boolean(scanId);
    showErrorScreen({
      title: 'リーディングを作成できませんでした',
      text,
      retryLabel: retryable ? 'もう一度試す' : null,
      onRetry: retryable ? () => { void showReadingConfirm(scanId); } : null,
      statusText: 'リーディング失敗',
    });
  }

  async function submitReading() {
    if (!inFlight.tryStart('reading')) return;
    // The restore re-renders the button from the current
    // confirmation: a refreshed confirmation may have become
    // unaffordable while the submission was in flight, and that
    // fresh disabled state must survive the busy restore.
    const restoreBusy = setBusy(
      element('reading-confirm-submit'), 'リーディングを受け付けています…',
      () => { if (state.readingConfirmation) renderReadingConfirm(state.readingConfirmation); });
    try {
      await submitReadingInner();
    } finally {
      // Release the guard BEFORE restoring: the restore re-render
      // computes the button's disabled state from eligibility AND
      // in-flight state, and rendering while the guard is still
      // held left a refreshed, retryable confirmation disabled
      // with nothing left to re-render it (Astra MEDIUM 3).
      inFlight.end('reading');
      restoreBusy();
    }
  }

  async function submitReadingInner() {
    // The submission is bound to the visit that confirmed it
    // (#2): confirmation id and request key are captured once
    // and stay frozen across the transport retry, and every
    // continuation applies only while that visit is current —
    // a result for scan A can no longer be paired with scan
    // B's view or navigation.
    const binding = state.readingBinding;
    const confirmation = state.readingConfirmation;
    if (!confirmation || !binding || !state.readingPrepared) return;
    // The paid operation record (R2): frozen at submission and
    // kept while the outcome is unknown, so a transport failure
    // recovers with the SAME key instead of falling through to
    // the generic home recovery and minting a different key on
    // re-entry over an outcome nobody knows yet.
    // Re-entering the SAME scan's reading while a previous
    // attempt is still unresolved reuses that attempt's record
    // (R2): a fresh key is minted only once the earlier outcome
    // is known, never over the top of it. Records are kept per
    // (operation, scan) in the ledger (round 2, S2): another
    // scan's unresolved reading BLOCKS this submission — its
    // recovery is installed so the earlier outcome is
    // reconciled first, instead of being silently overwritten.
    let record = paidLedger.get('reading', confirmation.scan_id);
    // A record that already exists makes this submission a
    // REPLAY of an unresolved attempt (round 6, N3 residual):
    // the replay pre-registers its follower generation before
    // its POST is awaited. A brand-new record is a first
    // submission — no pre-registration; its accepted follow-up
    // registers in the follower itself, as before.
    const isReplay = Boolean(record);
    if (!record) {
      const blocking = paidLedger.firstUnresolvedExcept('reading', confirmation.scan_id);
      if (blocking) { installReadingRecovery(blocking); return; }
      record = capturePaidOperation({
        operation: 'reading',
        confirmationId: confirmation.confirmation_id,
        requestKey: state.readingKey,
        scanId: confirmation.scan_id,
      });
      if (!record) return;
      paidLedger.put(record);
    }
    await runReadingSubmission(record, binding, { replay: isReplay });
  }

  // The reading wait screen's wording follows the server's
  // actual state (Astra gate #4): 'sending' while the mutation
  // is still in flight (no acceptance yet), 'creating' only
  // after the server answered ACCEPTED. Every submission —
  // first send, same-key replay, recovery — starts at
  // 'sending', so a stale 'creating' claim never carries over.
  function setReadingWaitPhase(phase) {
    const creating = phase === 'creating';
    element('reading-wait-title').textContent = creating
      ? 'リーディングを作成しています'
      : 'リーディングを送信しています';
    element('reading-wait-text').textContent = creating
      ? 'リーディングを作成しています。そのままお待ちください。'
      : 'リーディングの依頼をサーバーへ送信しています。そのままお待ちください。';
    // The step indicator follows the same phase (Antigravity
    // glass audit, finding 1): it is rendered from the screen
    // name alone, so without this sync it kept the fixed
    // 'creating' label during the send phase.
    if (state.currentScreen === 'reading-wait') {
      const step = element('app-step');
      const label = stepLabelFor('reading-wait', creating ? 'creating' : 'sending');
      step.hidden = !label;
      step.textContent = label || '';
    }
  }

  // One reading submission attempt sequence for a frozen record:
  // the initial send plus one same-key replay after a lost
  // response. Every continuation applies only while the visit
  // binding is current (R1) — leaving the flow retires the
  // binding, and a late success or failure then applies nothing.
  // A terminal answer (success or any explicit server error)
  // releases the operation record; a transport failure keeps it
  // and installs the same-key recovery instead of failGeneric.
  async function runReadingSubmission(record, binding, { replay = false } = {}) {
    const confirmation = {
      confirmation_id: record.confirmationId, scan_id: record.scanId,
    };
    // Successor ownership BEFORE the replay POST is awaited
    // (round 6, N3 residual): a replay (an existing record
    // re-entered, or a same-key recovery) registers its
    // follower generation NOW, superseding the previous
    // attempt's follower immediately. From this point the old
    // follower can no longer settle the record out from under
    // this submission: a terminal view its in-flight poll
    // returns is stashed for this generation instead, and the
    // response handling below consumes that stash before the
    // delayed accepted/ambiguous branches. The token is
    // carried into followReadingInProgress unchanged (no
    // re-registration: advancing the generation again would
    // break the stash hand-off and the current-generation
    // checks built on this registration). First submissions
    // (no pre-existing record) do NOT pre-register.
    const followerGeneration = replay
      ? readingFollowers.register(record)
      : null;
    // Screen permission needs BOTH the visit binding and the
    // navigation generation this submission started under
    // (round 2, S1): navigating to the account screen mid-flight
    // retires the navigation generation, so a reading that
    // resolves first can no longer discard the account
    // navigation and land on reading-result instead.
    const navGen = navTracker.current();
    const current = () => submissionScreenAllowed({
      navCurrent: navTracker.isCurrent(navGen),
      bindingCurrent: readingBindingApplies(binding, state.readingBinding),
    });
    // Intra-visit transition: plain show() keeps the visit's
    // binding alive (navigate() would retire it).
    show('reading-wait');
    // Real-state wording (Astra gate #4): until the server has
    // answered, the only established fact is that the request
    // is being sent — the wait screen must not claim the
    // reading is already being created. The wording flips to
    // the creating phase only in the accepted branch below,
    // once the server's acceptance is in hand.
    setReadingWaitPhase('sending');
    status('リーディングを送信しています');
    startElapsed('reading-wait-elapsed');
    // The client's per-call deadline bounds this await (round
    // 7): a POST whose response — body included — never lands
    // resolves as kind 'error' at the deadline instead of
    // hanging the submission (and its in-flight guard) forever
    // while a stashed terminal outcome waits unconsumed. The
    // abandoned response itself is fenced inside the client
    // and never reaches this continuation.
    let result = await client.createReading(record.confirmationId, record.requestKey);
    if (result.kind === 'error') {
      // A lost response is recovered by replaying the same
      // request_key: the server answers with the stored outcome
      // and never charges or generates twice. A deadline
      // overrun takes this same path: the replay is bounded
      // too, and if it also overruns, the result classifies
      // ambiguous below and reaches the stash-first handling.
      await new Promise((resolve) => setTimeout(resolve, 1500));
      if (!current()) return;
      result = await client.createReading(record.confirmationId, record.requestKey);
    }
    if (result.kind === 'login') { failToLogin(); return; }
    // Definitive-outcome classification (round 3, T1): only a
    // validated success or a KNOWN business rejection settles
    // the outcome — release the record even when this screen
    // continuation is stale (round 2, S2), because
    // reconciliation is separate from screen permission. A
    // generic 5xx JSON body, an unrecognized code or a
    // misshapen success is AMBIGUOUS: the record and its key
    // stay, and the same-key recovery below owns the outcome.
    // (Explicit terminal failures retry as NEW operations from
    // their own error screens — never as a replay of this key.)
    const outcome = classifyMutationResult(result, {
      isSuccess: isReadingMutationSuccess,
      isAccepted: isReadingMutationInProgress,
      rejectedErrors: READING_REJECTED_ERRORS,
      rejectedResponses: READING_REJECTED_RESPONSES,
    });
    // An ACCEPTED answer (round 4, N1) settles nothing: the
    // replay returned a reading that is still being generated,
    // so the record and its key stay owned by this attempt and
    // the follow-up below tracks the reading to its terminal
    // state — exactly as an accepted irradiation job owns its
    // progress screen.
    if (outcome === 'success' || outcome === 'rejected') {
      paidLedger.settle(record);
      // The replay's own definitive answer ends the operation:
      // drop the follower registration with it (round 6, N3
      // residual) — including any generation pre-registered
      // above — so no stale generation or stash outlives the
      // record and supersedes a later legitimate follower.
      readingFollowers.clear(record);
      if (outcome === 'rejected') {
        // The ended attempt's key retires with it (round 3,
        // T4): the refreshed confirmation prepared by the
        // branches below is a DIFFERENT request body and must
        // never be sent under this attempt's key. Identity-
        // guarded — a newer visit's own key is never touched.
        state.readingKey = retireRequestKey(state.readingKey, record.requestKey, newKey);
      }
    }
    // Consume a verified terminal outcome the superseded
    // follower stashed while this replay POST was in flight,
    // BEFORE either non-terminal branch (round 6, N3
    // residual): it takes precedence over 'accepted' (whose
    // delayed reserved view would start a follower over a
    // record that is already over, with no ledger entry left
    // to follow) and over 'ambiguous' (whose recovery prompt
    // would offer a same-key retry against a record that no
    // longer exists). The precedence decision is the pure
    // readingReplayPrecedence helper. Settlement here is
    // unconditional on screen permission; painting is not —
    // applyVerifiedReadingTerminalView paints only while
    // current() holds, exactly like the follower's own
    // terminal path. The stash consulted here is THIS record's
    // (round 7): the registry is bound to the immutable record,
    // so a previous reading's outcome for the same scan is
    // never visible to this submission. A POST that hit its
    // client deadline arrives here as kind 'error' and
    // classifies 'ambiguous', so this same stash-first
    // consumption is what a timed-out replay reaches.
    const stashedView = (outcome === 'accepted' || outcome === 'ambiguous')
      ? takeVerifiedStashedReadingOutcome(readingFollowers, {
        record,
        readingId: outcome === 'accepted' ? result.data?.reading_id : undefined,
      })
      : null;
    if (readingReplayPrecedence({ outcome, stashedView }) === 'stashed-terminal') {
      applyVerifiedReadingTerminalView(record, stashedView, current);
      return;
    }
    if (!current()) return;
    if (outcome === 'ambiguous') { installReadingRecovery(record); return; }
    if (outcome === 'accepted') {
      // Acceptance is now confirmed by the server's own
      // answer: the reading really is being created from this
      // point, so the wait screen may say so (Astra gate #4).
      setReadingWaitPhase('creating');
      status('リーディングを作成しています');
      void followReadingInProgress(
        record, result.data.reading_id, current, followerGeneration);
      return;
    }
    const data = result.data;
    if (isRejectedResponse(result, READING_REJECTED_RESPONSES)) {
      // The scan is gone (round 4, N2): refused before any
      // reservation, so there is nothing to recover against
      // it — the factual way out is a fresh scan from home.
      failScanNotFound();
      return;
    }
    if (data.error === 'reading_daily_cap') { readingFailure(READING_CAP_TEXT); return; }
    if (data.error === 'reading_unavailable' || data.error === 'reading_invalid_output') {
      readingFailure(READING_FAILED_TEXT);
      return;
    }
    if (data.error === 'reading_insufficient_balance') {
      status('残高が不足しています。');
      const fresh = await prepareReading(confirmation.scan_id, binding);
      if (fresh && current()) { show('reading-confirm'); renderReadingConfirm(fresh); }
      return;
    }
    if (data.error === 'quote_changed' || data.error === 'confirmation_expired'
        || data.error === 'confirmation_not_found'
        || data.error === 'confirmation_already_used') {
      status('確認の有効期限が切れました。もう一度お試しください。');
      const fresh = await prepareReading(confirmation.scan_id, binding);
      if (fresh && current()) { show('reading-confirm'); renderReadingConfirm(fresh); }
      return;
    }
    if (data.error) { failGeneric(); return; }
    state.readingKey = newKey();
    const view = scanViewForReading(
      { scan_id: data.scan_id || confirmation.scan_id }, state.scanView);
    showReadingResult(data, view);
  }

  // Settle a VERIFIED terminal reading view through the
  // normal path: release the record and the follower
  // registration, then paint only while the caller still owns
  // the screen (a stale transition reconciles by settling
  // alone). Shared by the in-progress follower and by the
  // replay path consuming a stashed terminal outcome (round
  // 6, N3 residual) so both settle identically.
  function applyVerifiedReadingTerminalView(record, view, current) {
    paidLedger.settle(record);
    readingFollowers.clear(record);
    if (view.state === 'completed') {
      if (!current()) return;
      state.readingKey = newKey();
      const scanView = scanViewForReading(
        { scan_id: view.scan_id || record.scanId }, state.scanView);
      showReadingResult(view, scanView);
      return;
    }
    state.readingKey = retireRequestKey(state.readingKey, record.requestKey, newKey);
    if (!current()) return;
    readingFailure(view.error_code === 'reading_daily_cap'
      ? READING_CAP_TEXT : READING_FAILED_TEXT);
  }

  // Follow an accepted-but-unfinished reading to its terminal
  // state (round 4, N1): the mutation replay answered with the
  // reading_view of a reading still being generated, so this
  // attempt keeps owning the record and polls the reading by
  // id — the same GET the history screen uses — until the
  // server reports completed or failed. Only a terminal view
  // settles the record: completed renders the result, failed
  // routes to the reading failure screen with the key retired.
  // Losing screen ownership (or the record itself, e.g. the
  // session ended) stops the follow-up silently: the record
  // stays in the ledger, and re-entering the reading replays
  // the same key, whose stored outcome is the answer.
  async function followReadingInProgress(record, readingId, current, preRegisteredGeneration = null) {
    // Follower generations (round 5, N3): registering here
    // supersedes any older follower for this (operation, scan).
    // (Round 6, N3 residual: a replay's follower instead carries
    // the generation its submission pre-registered before the
    // replay POST — see the token handling below.)
    // Only the CURRENT follower may settle the record or retire
    // its key; a superseded follower that observes a verified
    // terminal view stashes it as the identity's pending
    // outcome and exits, and the current follower consumes the
    // stash (re-verified) on its next cycle — an active
    // screen's follow-up is never silently ended by an older
    // follower's late answer.
    // A replay submission may hand over the generation it
    // pre-registered before its POST (round 6, N3 residual):
    // that token is used AS-IS — re-registering here would
    // advance the generation and break the stash hand-off and
    // current-generation checks built on the pre-registration.
    // Callers without a token (first submissions) register
    // here, as before.
    const generation = preRegisteredGeneration
      ?? readingFollowers.register(record);
    const isCurrentFollower = () => readingFollowers
      .isCurrent(record, generation);
    // Callers guarantee this follower is current.
    const applyTerminalView = (view) => {
      applyVerifiedReadingTerminalView(record, view, current);
    };
    while (paidLedger.get('reading', record.scanId) === record) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      if (paidLedger.get('reading', record.scanId) !== record) break;
      if (!isCurrentFollower()) {
        // Superseded between cycles: the newer follower polls
        // on its own; this one has nothing in flight to hand
        // over, so it simply stops.
        return;
      }
      // Consume a terminal outcome a superseded follower
      // stashed for this identity BEFORE anything else: it is
      // re-verified against THIS follower's reading and scan
      // (round 5, N4 applies to the stash too), and an
      // unverifiable stash proves nothing and is dropped.
      const stashed = readingFollowers.takeOutcome(record);
      if (stashed) {
        const stashedView = verifyTerminalReadingPoll(stashed, {
          readingId, scanId: record.scanId,
        });
        if (stashedView) { applyTerminalView(stashedView); return; }
      }
      if (!current()) return;
      const result = await client.reading(readingId);
      // Record binding after the await (round 7): while this
      // poll was in flight, this record may have settled AND a
      // different operation for the same scan — a new reading
      // under a fresh request_key — may have taken its ledger
      // slot. This answer belongs to the record this follower
      // captured; if that is no longer the ledger's current
      // record, the answer is discarded ENTIRELY: no stash
      // (the registry slot now belongs to the other record),
      // no settle, no paint, no login routing.
      if (paidLedger.get('reading', record.scanId) !== record) return;
      if (result.kind === 'login') { failToLogin(); return; }
      // Only a verified terminal view of THIS reading for THIS
      // scan settles anything (round 5, N4): non-2xx answers,
      // identity mismatches, incomplete shapes and non-terminal
      // states prove nothing — ownership is kept and the
      // follow-up continues rather than abandoning live work.
      const view = verifyTerminalReadingPoll(result, {
        readingId, scanId: record.scanId,
      });
      if (!view) continue;
      if (!isCurrentFollower()) {
        // This follower was superseded while its poll was in
        // flight: hand the verified outcome over instead of
        // settling a record the newer follower still owns.
        readingFollowers.stashOutcome(record, result);
        return;
      }
      applyTerminalView(view);
      return;
    }
    // The record was settled outside this follower (e.g. a
    // re-entry replay answered terminally): drop this
    // follower's registration if it is still the current one,
    // so no stale generation or stash outlives the operation.
    if (isCurrentFollower()) readingFollowers.clear(record);
  }

  function installReadingRecovery(record) {
    showErrorScreen({
      title: 'リーディングを送信できませんでした',
      text: '通信が途中で切れた可能性があります。結果はまだ確定していません。もう一度試すと、同じ送信として確認します。',
      retryLabel: 'もう一度試す',
      onRetry: () => { void recoverReadingOperation(record); },
      statusText: 'リーディング送信エラー',
    });
  }

  // Same-key recovery for a reading whose outcome is unknown
  // (R2): re-acquires CURRENT reading-visit ownership for the
  // record's scan (the original visit may have been retired by
  // the navigation to the error screen), then replays the
  // frozen record. The confirmation → wait transition inside
  // the recovery keeps the new binding, as the normal flow does.
  async function recoverReadingOperation(record) {
    if (paidLedger.get('reading', record.scanId) !== record) return;
    if (!inFlight.tryStart('reading')) return;
    const binding = readingBinding(record.scanId, readingTracker.begin());
    try {
      navigate('reading-wait');
      state.readingBinding = binding;
      state.readingConfirmation = {
        confirmation_id: record.confirmationId, scan_id: record.scanId,
      };
      state.readingPrepared = true;
      // Recovery IS a replay of an existing record: it
      // pre-registers its follower generation too (round 6,
      // N3 residual).
      await runReadingSubmission(record, binding, { replay: true });
    } finally {
      // Release the guard BEFORE any re-render (round 3, N1):
      // when the recovery's replay ends in quote_changed, the
      // submission renders the refreshed confirmation WHILE
      // this recovery still holds the guard, so its submit
      // button computes disabled — and nothing re-rendered it
      // after the release, stranding an affordable, prepared
      // confirmation. Recompute the CURRENT confirmation's
      // eligibility now, exactly as submitReading()'s busy
      // restore does.
      //
      // Round 8: the recomputation is judged against the
      // CURRENT live binding, NOT this recovery's captured
      // binding. While this recovery held the guard (its POST
      // bounded at 20s, plus the 1.5s replay pause), the user
      // could re-enter the confirmation screen under a NEW
      // binding; that screen rendered its submit button
      // disabled because the guard was still held. When this
      // recovery's submission then returned early — before
      // consuming the follower stash — the old equality check
      // skipped the re-render (the new binding differs from
      // the captured one) and stranded the new screen's button
      // disabled with the guard free and the stashed outcome
      // inert. readingConfirmRecalcApplies keeps the real
      // guards — current screen, a prepared, quote-carrying
      // confirmation, and its scan matching the CURRENT
      // binding's scan — and renderReadingConfirm recomputes
      // affordability/in-flight through the same pure
      // eligibility the renderer always uses, so an
      // unaffordable or unprepared confirmation is never
      // enabled by this release. Once enabled, the user's own
      // submit goes through submitReadingInner, whose replay
      // of the still-unresolved record consumes the stash
      // first, as before.
      inFlight.end('reading');
      if (readingConfirmRecalcApplies({
        screen: state.currentScreen,
        confirmation: state.readingConfirmation,
        prepared: state.readingPrepared,
        liveBinding: state.readingBinding,
      })) {
        renderReadingConfirm(state.readingConfirmation);
      }
    }
  }

  function showReadingResult(reading, scanView) {
    state.currentReading = reading;
    // The context is the reading's OWN scan (#10): a parked view
    // of a different scan is not adopted, and a stale one is
    // dropped rather than left for copy/back to trip over.
    if (reading?.scan_id) {
      state.scanView = scanViewForReading(reading, scanView ?? state.scanView);
    } else if (scanView) {
      state.scanView = scanView;
    }
    // Summary-first format (2026-11-22): the 【要約】 block renders
    // in its own box above the body; the stored text itself stays
    // whole (copy uses it as-is). Older readings have no summary
    // and render unsplit; expired text shows the retention notice.
    const summaryElement = element('reading-summary');
    // The retention notice is shown ONLY when the server
    // itself reports the text as expired (round 4, N1): a
    // missing text on its own is not evidence of expiry — an
    // unfinished reading's view carries text: null with
    // text_expired: false, and rendering the notice for it
    // stated a falsehood. (Terminal readings reach this
    // renderer only in a completed state, where the server
    // sets text_expired exactly when no text was retained.)
    if (reading.text_expired) {
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
    navigate('reading-result');
    status('リーディングを表示しています。');
  }

  // Stage sentences live in app-wait.js (stageTextFor) so the
  // exact texts are unit-tested; the waiting screens render one
  // synchronously on entry, before the first poll lands.

  async function submitScanFlow(completionMode = 'wait') {
    if (!inFlight.tryStart('scan')) return;
    try {
      // Final gate: the step screens validated their own step
      // before advancing, so a problem here means the flow was
      // entered out of order — say so in place and stay.
      if (draftProblem(state.draft)) {
        formError('oq-confirm-error', '入力が不足しています。もどって確認してください。');
        return;
      }
      formError('oq-confirm-error', null);
      // Snapshot the whole submission (#4): the request, the
      // returned job's target label and the request key all come
      // from this frozen copy, never from the live draft the
      // user may edit for a newer flow while this one is in
      // flight. The completion choice (foundation item 2) is
      // frozen into the snapshot too, so a same-key replay
      // presents the same choice the first attempt presented.
      const snapshot = captureScanSubmission(state.draft, {
        affirmation: resolvedAffirmation(state.draft),
        requestKey: state.scanKey,
        completionMode,
      });
      await runScanSubmission(snapshot, scanFlowTracker.current(), navTracker.current());
    } finally {
      inFlight.end('scan');
    }
  }

  // The error screen's retry for a transport-failed scan (#8):
  // the SAME snapshot and the SAME request key are replayed —
  // if the server accepted the original attempt, the replay
  // returns its stored outcome instead of creating a second
  // job. (An explicitly terminal-failed scan is different: its
  // retry goes through retryRescan, which mints a fresh key.)
  async function retryScanSubmission(snapshot, flowGen, navGen) {
    if (!inFlight.tryStart('scan')) return;
    try {
      await runScanSubmission(snapshot, flowGen, navGen);
    } finally {
      inFlight.end('scan');
    }
  }

  async function runScanSubmission(snapshot, flowGen, navGen) {
    // Screen permission (round 2, S1): the flow generation AND
    // the navigation generation must both still own the screen.
    // General navigation (the gear's account screen, Home) does
    // not touch the scan flow tracker, so checking the flow
    // alone let a stale transport failure replace the account
    // screen with this submission's error.
    const ownsScreen = () => submissionScreenAllowed({
      navCurrent: navTracker.isCurrent(navGen),
      bindingCurrent: scanFlowTracker.isCurrent(flowGen),
    });
    const restoreBusy = setBusy(element('oq-confirm-submit'), 'スキャンを受け付けています…');
    let result;
    try {
      result = await client.scan({
        request_key: snapshot.requestKey, target_ids: [snapshot.targetId],
        intention: snapshot.intention,
        self_love_joy: snapshot.affirmation,
        completion: { mode: snapshot.completionMode },
      });
    } finally {
      restoreBusy();
    }
    if (result.kind === 'login') { failToLogin(); return; }
    // Definitive-outcome classification (round 3, T1): a scan
    // is free, but its request key is a replay contract — a
    // generic HTTP 500 JSON body is kind 'ok' too, and treating
    // ANY data.error as an explicit terminal failure discarded
    // that contract. Only a validated success proceeds, only a
    // KNOWN business rejection is terminal, and everything else
    // is ambiguous with the same-key recovery below.
    const outcome = classifyMutationResult(result, {
      isSuccess: isScanMutationSuccess,
      rejectedErrors: SCAN_REJECTED_ERRORS,
      rejectedResponses: SCAN_REJECTED_RESPONSES,
    });
    if (outcome === 'ambiguous') {
      // A STALE failure is discarded before it can install any
      // recovery UI (delta review R3): the user has already left
      // this submission behind, so its lost response must not
      // park an error screen over the newer flow.
      if (!ownsScreen()) return;
      // Transport failure / ambiguous outcome: the server may
      // have accepted it, so recovery replays this exact
      // submission instead of returning home and losing both
      // the inputs and the request identity (#8). The explicitly
      // resumed recovery acquires CURRENT flow ownership
      // (scanFlowTracker.begin()) while retaining the original
      // snapshot and key — otherwise its accepted response would
      // be discarded by the very generation check that protects
      // the flow (R3).
      showErrorScreen({
        title: 'スキャンを送信できませんでした',
        text: '通信が途中で切れた可能性があります。送信内容はそのまま残っています。もう一度試すと、同じ送信として再試行します。',
        retryLabel: 'もう一度試す',
        onRetry: () => {
          void retryScanSubmission(snapshot, scanFlowTracker.begin(), navTracker.current());
        },
        statusText: 'スキャン送信エラー',
      });
      return;
    }
    const data = result.data;
    if (outcome === 'rejected') {
      // A superseded flow's error applies nothing at all (R1).
      if (!ownsScreen()) return;
      if (isRejectedResponse(result, SCAN_REJECTED_RESPONSES)) {
        // The target itself is gone (round 4, N2): the server
        // answered 404 "Target not found" and did not execute.
        // Retire the attempt's key (identity-guarded) and route
        // to target selection with the fact — the same-key
        // recovery must never keep resending a deleted target,
        // and the confirmation screen cannot repair a target
        // that no longer exists.
        state.scanKey = retireRequestKey(state.scanKey, snapshot.requestKey, newKey);
        showErrorScreen({
          title: '対象が見つかりませんでした',
          text: '選択した対象が見つからなかったため、スキャンは実行されていません。対象を選び直してください。',
          retryLabel: '対象を選び直す',
          onRetry: () => { void showTargetPick(); },
          statusText: '対象が見つかりません',
        });
        return;
      }
      if (data.error === 'notify_unavailable') {
        // The notify choice was refused before anything was
        // consumed and the attempt's intent was discarded
        // (foundation item 2): the visit keeps its request key
        // and may present the wait choice instead. Reopen the
        // choice with notify suppressed — the fact is stated
        // there, and "wait" resubmits this same snapshot's
        // inputs under the same key with mode "wait".
        state.waitMethod = { operation: 'scan', notifySuppressed: true };
        showWaitMethod();
        return;
      }
      // Typed intake rejections (batch 2b item 7, ported with
      // the foundation merge): quota exhaustion, the exposure-
      // budget stop and a missing configuration are facts about
      // the service, not about the user's input — say which one
      // happened instead of blaming the input. The server
      // created no job and recorded nothing under this request
      // key, so the visit keeps its key, exactly like the
      // generic rejection below.
      if (data.error === 'scan_quota_exhausted' || data.error === 'scan_budget_exhausted'
          || data.error === 'scan_unavailable') {
        showScanConfirm();
        formError('oq-confirm-error',
          data.error === 'scan_quota_exhausted' ? SCAN_QUOTA_TEXT
            : data.error === 'scan_budget_exhausted' ? SCAN_BUSY_TEXT
              : SCAN_UNAVAILABLE_TEXT);
        return;
      }
      // Return to the confirmation the snapshot belongs to, so
      // the message lands in place.
      showScanConfirm();
      formError('oq-confirm-error', 'スキャンを受け付けられませんでした。入力を確認してください。');
      return;
    }
    state.scanJob = data.job_id;
    // Remember which target this scan was submitted for, in
    // this session only: the irradiation confirmation's target
    // row reads it back (the job view has no target field).
    // The label comes from the snapshot (#4) — the draft may
    // already name a different target for a newer flow.
    state.scanTargetNames[data.job_id] = snapshot.targetName;
    // A superseded flow's response is recorded but never takes
    // over the newer flow's navigation (#4).
    if (!ownsScreen()) return;
    state.scanKey = newKey();
    // The landing follows the RECORDED choice in the response
    // (foundation item 2), not the choice this attempt asked
    // for: a replay can legitimately answer with the first
    // attempt's recorded mode. Notify opens the notify-wait
    // screen; anything else opens the polling wait screen.
    if (data.completion?.mode === 'notify' && enterNotifyWait('scan', data)) return;
    enterScanWait(data);
  }

  function enterScanWait(data) {
    // The step label derives from the recorded state from the
    // first render (batch 2e, R7): set the context BEFORE the
    // navigation renders it, and refresh it on every poll.
    state.waitStep = { state: data.state || 'unknown' };
    navigate('wait');
    element('wait-title').textContent = 'スキャンを受け付けました';
    // Stage text and the elapsed clock render synchronously on
    // entry, from the state the submission itself reported —
    // the job may already be running, and a replayed submission
    // can even return a terminal state; claiming "queued" for
    // all of those manufactured a stage the server never said
    // (Astra LOW 6). Only a missing state falls back, to the
    // honest "checking status" wording.
    element('wait-text').textContent =
      stageTextFor(data.state ? data : { state: 'unknown' }, 'スキャン');
    // Closing this screen is safe either way; the mail clause
    // is promised only while the session can actually mail
    // (foundation item 2): a wait-mode job left unviewed is
    // escalated to a completion mail by the server.
    element('wait-note').textContent = waitMailNoteFor({
      // A response that reports no state earns the unknown
      // wording here too, exactly like the stage text above
      // (Antigravity glass audit, finding 3): the note must
      // not claim the execution continues on an unreported
      // state. Poll updates always carry a validated state.
      operation: 'scan', completion: data.completion, state: data.state || 'unknown',
    });
    status('スキャン受付済み');
    startElapsed('wait-elapsed');
    pollJob(data.job_id,
      (view) => {
        state.waitStep = { state: view.state };
        syncStepLabel('wait');
        element('wait-text').textContent = stageTextFor(view, 'スキャン');
        // The closing note is recomputed from EVERY received
        // view, not just the entry render (combined review
        // finding 1): the state and the recorded notification
        // lifecycle move while this screen is open, and a note
        // frozen at entry can end up contradicting the stage
        // text right above it.
        element('wait-note').textContent = waitMailNoteFor({
          operation: 'scan', completion: view.completion, state: view.state,
        });
      },
      (terminal) => {
        if (terminal.state === 'completed') { showRanking(terminal); return; }
        // A degraded terminal view (unreadable payload: the
        // operation did not survive the read) earns the same
        // unavailable route as showRanking/showProof, and no
        // ack — there is no result the owner could have seen
        // (Antigravity glass audit, finding 4).
        if (terminal.operation !== 'scan') { failResultUnavailable(); return; }
        showErrorScreen({
          title: 'スキャンが完了しませんでした',
          text: 'スキャンは無料です。もう一度お試しください。',
          retryLabel: '再スキャンする（無料）',
          onRetry: retryRescan,
          statusText: 'スキャン失敗',
        });
        // After the render, like every ack (round 3).
        ackResultViewed(terminal.job_id);
      });
  }

  // Freshness gate for irradiation quotes: a quote response may
  // only become the active confirmation when it answers the
  // latest request, its own count matches the count it was
  // requested for, and that count is still the selected one. A
  // late response for a previous count is dropped, never
  // rendered — the 2026-11-22 live check saw the previous
  // count's total linger for seconds after the count moved.
  const quoteGate = createQuoteGate();

  // Visit generations for the confirmation screen (app-quote.js):
  // the gate orders requests within a visit, the session orders
  // visits against each other. show() invalidates the session
  // whenever another screen is shown; showConfirm enters a new
  // generation. (The count is chosen on its own screen since
  // increment 3, so no debounce timer remains here: entering the
  // confirmation screen issues exactly one prepare per visit,
  // and going back to change the count starts a new visit.)
  const confirmSession = createConfirmSession();

  async function prepare(count) {
    const token = quoteGate.begin();
    const generation = confirmSession.current();
    const scanId = state.confirmScanId;
    const result = await client.prepareIrradiation(scanId, count);
    // Freshness BEFORE any error is applied (Astra MEDIUM 2): a
    // late failure — or a late scan_expired — from a request the
    // user has since moved past must not tear down the current
    // screen (in the reproduction, an accepted irradiation's
    // progress screen). Only a login result still routes, since
    // authentication expiry is global.
    const disposition = prepareDisposition(result, {
      current: confirmSession.isCurrent(generation) && state.confirmScanId === scanId,
    });
    if (disposition === 'login') { failToLogin(); return null; }
    if (disposition === 'drop') return null;
    if (result.kind !== 'ok') { failGeneric(); return null; }
    const data = result.data;
    if (data.error === 'scan_expired') { failScanExpired(); return null; }
    if (data.error) { failGeneric(); return null; }
    if (!quoteGate.isCurrent(token) || !quoteMatchesCount(data.quote, count)
        || state.selectedCount !== count) {
      return null;
    }
    state.confirmation = data;
    state.preparedCount = count;
    state.confirmationScanId = scanId;
    state.confirmationGeneration = generation;
    return data;
  }

  // The count screen (increment 3): its own step in the
  // one-question flow, split off the confirmation screen. The
  // slider and the strength presets only move the selection;
  // the total beside them is the policy estimate (app-price.js,
  // the exact mirror of the server's charge math on the
  // session's rate), labelled as an estimate on screen. Nothing
  // is requested from the server here — the authoritative quote
  // is prepared on the confirmation screen that follows.
  function showCount(scanId) {
    // Same guard as the confirmation screen: while a submission
    // is in flight, the irradiation path stays frozen.
    if (inFlight.isActive('irradiation')) return;
    if (state.countScanId !== scanId) state.selectedCount = 33;
    state.countScanId = scanId;
    element('count-slider').value = String(state.selectedCount);
    renderCount();
    navigate('count');
    status('照射の回数');
  }

  function renderCount() {
    const count = Number(element('count-slider').value);
    state.selectedCount = count;
    // 案B slider grammar (visual only): the filled track and the
    // active preset follow the same value the number shows.
    element('count-slider').style.setProperty('--fill', `${((count - 1) / 98) * 100}%`);
    for (const preset of document.querySelectorAll('.preset')) {
      preset.classList.toggle('active', Number(preset.dataset.count) === count);
    }
    element('count-now').textContent = String(count);
    element('count-strength').textContent = STRENGTH(count);
    const perShot = state.session?.credits_per_shot;
    element('count-unit').textContent = perShot === null || perShot === undefined
      ? '—' : `1回 ${perShot} クレジット`;
    const total = sessionChargeCredits(count, state.session?.credits_per_shot_tenths);
    element('count-total').textContent = total === null
      ? '—' : `${total.toLocaleString('ja-JP')} クレジット`;
  }

  // While no quote for the currently selected count has landed,
  // the totals are explicitly undecided and submission is locked:
  // the previous count's total must never sit on screen — or be
  // acted on — as if it belonged to the new count.
  function renderConfirmPending() {
    // Balance joins the totals as a placeholder (#6): until the
    // prepare response lands, NO money row shows a number — the
    // session's older balance must never sit next to a fresh
    // quote as if the two agreed.
    element('confirm-balance').textContent = '計算中…';
    element('confirm-unit').textContent = '計算中…';
    element('confirm-total').textContent = '計算中…';
    element('confirm-after').textContent = '計算中…';
    element('confirm-insufficient').hidden = true;
    state.confirmInsufficient = false;
    state.confirmShortage = null;
    element('confirm-submit').textContent = '照射を始める';
    element('confirm-submit').disabled = true;
  }

  function renderConfirm(data) {
    const quote = data.quote;
    // All money rows render together from THIS accepted prepare
    // response (#6): its available balance, its quote, and its
    // projected balance-after — never the session's stale copy.
    element('confirm-balance').textContent = typeof data.available === 'number'
      ? `${data.available.toLocaleString('ja-JP')} クレジット`
      : (typeof data.balance_after === 'number' && typeof quote.credits === 'number'
        ? `${(data.balance_after + quote.credits).toLocaleString('ja-JP')} クレジット`
        : '確認できませんでした');
    element('confirm-count').textContent = `${quote.count} 回（${STRENGTH(quote.count)}）`;
    element('confirm-unit').textContent = `1回 ${quote.credits_per_shot} クレジット`;
    element('confirm-total').textContent = `${quote.credits.toLocaleString('ja-JP')} クレジット`;
    element('confirm-after').textContent = data.balance_after === null || data.balance_after === undefined
      ? '確認できませんでした' : `${data.balance_after.toLocaleString('ja-JP')} クレジット`;
    const insufficient = typeof data.balance_after === 'number' && data.balance_after < 0;
    element('confirm-insufficient').hidden = !insufficient;
    state.confirmShortage = shortageCredits(data.balance_after);
    if (insufficient) {
      element('confirm-shortage').textContent = state.confirmShortage.toLocaleString('ja-JP');
    }
    state.confirmInsufficient = insufficient;
    // An unaffordable confirmation does not dead-end (confirmed
    // item 7, 2026-11-23): the primary button becomes the way to
    // purchase, in place, and stays tappable. A submittable one
    // is the approval itself. Either way an in-flight submission
    // locks the button.
    element('confirm-submit').textContent = insufficient ? 'クレジットを購入する' : '照射を始める';
    element('confirm-submit').disabled = inFlight.isActive('irradiation');
  }

  // The explicit recovery view for an unresolved irradiation
  // (round 2, N3): bound to the OPERATION's frozen count and
  // quote, never to the newest mutable confirmation. Preparing
  // a fresh confirmation for a different count and then
  // reconciling the old operation paired an approval screen
  // (66) with a replayed request (33) and a progress total of
  // 66 — this view makes the screen describe the operation
  // that will actually be reconciled, and its button runs the
  // same-key recovery for that record.
  function showIrradiationRecovery(record) {
    confirmSession.enter();
    state.confirmScanId = record.scanId;
    state.confirmation = null;
    state.preparedCount = null;
    state.confirmationScanId = null;
    state.confirmInsufficient = false;
    state.confirmShortage = null;
    navigate('confirm');
    state.confirmRecovery = record;
    element('confirm-count').textContent = typeof record.count === 'number'
      ? `${record.count} 回（${STRENGTH(record.count)}）` : '—';
    element('confirm-unit').textContent = '—';
    element('confirm-total').textContent = typeof record.quoteCredits === 'number'
      ? `${record.quoteCredits.toLocaleString('ja-JP')} クレジット` : '—';
    element('confirm-balance').textContent = '—';
    element('confirm-after').textContent = '—';
    element('confirm-insufficient').hidden = true;
    element('confirm-submit').textContent = '送信結果を確認する';
    element('confirm-submit').disabled = false;
    void renderConfirmContext(record.scanId);
    status('照射の送信結果がまだ確認できていません。送信したときの内容で確認します。');
  }

  async function showConfirm(scanId) {
    // While an irradiation submission is in flight, re-entering
    // the confirmation screen must not replace the active
    // operation (its confirmation and request key are frozen).
    if (inFlight.isActive('irradiation')) return;
    // An unresolved irradiation for this scan owns the visit
    // (round 2, N3): the recovery view for the frozen operation
    // replaces the fresh-quote flow until its outcome is known.
    const unresolved = paidLedger.get('irradiation', scanId);
    if (unresolved) { showIrradiationRecovery(unresolved); return; }
    // A new visit starts a new generation: a quote response from
    // a previous visit (for a previous count, after the user went
    // back and changed it) must never apply here.
    confirmSession.enter();
    state.confirmScanId = scanId;
    state.confirmation = null;
    state.preparedCount = null;
    state.confirmationScanId = null;
    state.irradiateKey = newKey();
    // The count row shows the user's own selection synchronously
    // on entry; the money rows wait for the quote.
    element('confirm-count').textContent =
      `${state.selectedCount} 回（${STRENGTH(state.selectedCount)}）`;
    navigate('confirm');
    status('照射の確認');
    void renderConfirmContext(scanId);
    renderConfirmPending();
    const data = await prepare(state.selectedCount);
    if (data) renderConfirm(data);
  }

  // The full picture on the irradiation confirmation (display
  // requirement ②, 2026-11-23): target, intention and the
  // affirmation actually used — the exact sentences the scan's
  // job view returns — sit above the money rows, so the user
  // confirms everything before approving. Values come only from
  // the server (the in-memory scan view when it is this scan, a
  // job fetch otherwise); anything unavailable stays "—" rather
  // than being reconstructed on the client.
  async function renderConfirmContext(scanId) {
    element('confirm-target').textContent = '—';
    element('confirm-intention').textContent = '—';
    element('confirm-affirmation').textContent = '—';
    // The balance row is NOT rendered here (#6): it belongs to
    // the prepare response and is drawn by renderConfirm /
    // renderConfirmPending only.
    // The target row's source of truth is the job view's
    // `targets` (an array of target names): the foundation fixed
    // that API shape, so once it lands, a scan reopened from
    // history fills the row too. Until the foundation release
    // merges, the job view carries no targets, and the row falls
    // back to this session's own submission record — the name
    // the user picked for this scan moments ago — and otherwise
    // stays "—" rather than being reconstructed.
    const recorded = state.scanTargetNames[scanId];
    if (recorded) element('confirm-target').textContent = recorded;
    const contextGen = confirmSession.current();
    let view = state.scanView?.job_id === scanId ? state.scanView : null;
    if (!view) {
      const result = await client.job(scanId);
      if (result.kind === 'login') { failToLogin(); return; }
      if (result.kind === 'ok' && !result.data.error) view = result.data;
    }
    // A newer confirmation visit (or navigation away) retires
    // this context render (#5).
    if (!confirmSession.isCurrent(contextGen)) return;
    if (!view || state.currentScreen !== 'confirm' || state.confirmScanId !== scanId) return;
    if (Array.isArray(view.targets) && view.targets.length) {
      element('confirm-target').textContent = view.targets.join('、');
    }
    element('confirm-intention').textContent = view.intention || '—';
    element('confirm-affirmation').textContent = view.self_love_joy || '—';
  }

  // Re-request the quote for the selected count and render it.
  // Used when a busy restore finds the parked confirmation stale:
  // with the count chosen on its own screen there is no slider
  // event left to trigger this, so the restore asks directly.
  async function refreshConfirmQuote() {
    if (inFlight.isActive('irradiation')) return;
    const data = await prepare(state.selectedCount);
    if (data) renderConfirm(data);
  }

  async function submitIrradiation() {
    if (!inFlight.tryStart('irradiation')) return;
    // The restore re-renders from the parked confirmation ONLY
    // when that confirmation still matches this visit, this scan
    // and the count now selected — a count change during the
    // submission makes it stale, and repainting it resurrected
    // the previous count's total with no refresh scheduled
    // (Astra MEDIUM 3). A stale or missing confirmation restores
    // to the pending state and re-requests the current selection
    // instead. On screens other than the confirmation screen
    // (a successful submission has moved to progress) there is
    // nothing to restore.
    const restoreBusy = setBusy(
      element('confirm-submit'), '照射を受け付けています…',
      () => {
        if (state.currentScreen !== 'confirm') return;
        if (isConfirmationCurrent(state, {
          scanId: state.confirmScanId,
          generation: confirmSession.current(),
          count: state.selectedCount,
        })) {
          renderConfirm(state.confirmation);
        } else {
          renderConfirmPending();
          void refreshConfirmQuote();
        }
      });
    try {
      await submitIrradiationInner();
    } finally {
      // Release the guard BEFORE restoring, for the same reason
      // as readings: renderConfirm computes disabled from
      // eligibility AND in-flight state, so a restore rendered
      // while the guard is still held strands a refreshed
      // confirmation in the disabled state (Astra MEDIUM 3).
      inFlight.end('irradiation');
      restoreBusy();
    }
  }

  async function submitIrradiationInner() {
    const count = state.selectedCount;
    if (!state.confirmation || state.preparedCount !== count) {
      const data = await prepare(count);
      if (!data) return;
      renderConfirm(data);
      // prepare() applies a quote only while its count is still
      // the selected one; if the selection moved on anyway, the
      // pending render owns the screen now — do not irradiate a
      // count the user no longer sees confirmed.
      if (state.preparedCount !== state.selectedCount) return;
    }
    // The paid operation record (R2): confirmation id and
    // request key frozen together. A transport failure no
    // longer falls through handle() to the generic home
    // recovery — the outcome is unknown, so the same-key
    // recovery below owns it until a terminal answer arrives.
    // Same discipline as readings (R2): re-entering the same
    // scan's irradiation while an attempt is unresolved replays
    // that attempt's record instead of minting a new key.
    // Records are kept per (operation, scan) in the ledger
    // (round 2, S2): another scan's unresolved irradiation
    // BLOCKS this submission — its recovery is installed so
    // the earlier outcome is reconciled first. The record
    // freezes the operation's own count and quote with it
    // (round 2, N3), so recovery and progress describe THIS
    // operation even if a newer confirmation was prepared
    // meanwhile.
    let record = paidLedger.get('irradiation', state.confirmScanId);
    if (!record) {
      const blocking = paidLedger.firstUnresolvedExcept('irradiation', state.confirmScanId);
      if (blocking) { installIrradiationRecovery(blocking); return; }
      record = capturePaidOperation({
        operation: 'irradiation',
        confirmationId: state.confirmation.confirmation_id,
        requestKey: state.irradiateKey,
        scanId: state.confirmScanId,
        count: state.confirmation.quote?.count ?? count,
        quoteCredits: state.confirmation.quote?.credits,
        completionMode: state.completionMode,
      });
      if (!record) return;
      paidLedger.put(record);
    }
    const navGen = navTracker.current();
    const result = await client.irradiate(
      record.confirmationId, record.requestKey, record.completionMode);
    if (result.kind === 'login') { failToLogin(); return; }
    // Definitive-outcome classification (round 3, T1): only a
    // validated success or a KNOWN business rejection releases
    // the record — even when the screen continuation is stale
    // (round 2, S2), because settling is reconciliation, not
    // painting. A generic 5xx JSON body or an unrecognized code
    // is AMBIGUOUS: record and key stay for same-key recovery.
    const outcome = classifyMutationResult(result, {
      isSuccess: isIrradiationMutationSuccess,
      rejectedErrors: IRRADIATION_REJECTED_ERRORS,
      rejectedResponses: IRRADIATION_REJECTED_RESPONSES,
    });
    if (outcome !== 'ambiguous') {
      paidLedger.settle(record);
      if (outcome === 'rejected') {
        // The ended attempt's key retires with it (round 3,
        // T4): a refreshed confirmation is a DIFFERENT request
        // body and must never be sent under this attempt's key.
        // Identity-guarded — a newer visit's key is untouched.
        state.irradiateKey = retireRequestKey(state.irradiateKey, record.requestKey, newKey);
      }
    }
    // Ownership before routing (R1): the user left mid-flight —
    // the outcome reconciles through history, and nothing here
    // may replace the newer screen.
    if (!navTracker.isCurrent(navGen)) return;
    if (outcome === 'ambiguous') { installIrradiationRecovery(record); return; }
    const data = result.data;
    if (data.error === 'notify_unavailable') {
      // The fresh notify submission was refused BEFORE the
      // confirmation was claimed (foundation item 2): nothing
      // was consumed, the record is already settled and its key
      // retired above, and the parked confirmation was never
      // claimed, so it is still usable. Reopen the choice with
      // notify suppressed — "wait" resubmits the same
      // confirmation under the fresh key with mode "wait".
      state.waitMethod = { operation: 'irradiation', notifySuppressed: true };
      showWaitMethod();
      return;
    }
    if (data.error === 'quote_changed') {
      status('単価が変わりました。確認画面の金額を見直してください。');
      const fresh = await prepare(count);
      if (fresh) renderConfirm(fresh);
      return;
    }
    if (isRejectedResponse(result, IRRADIATION_REJECTED_RESPONSES)) {
      // The scan is gone (round 4, N2): the record is already
      // settled and the key retired above, so the missing scan
      // can no longer block other scans' paid operations.
      failScanNotFound();
      return;
    }
    if (data.error) { failGeneric(); return; }
    state.irradiateKey = newKey();
    landIrradiationSubmission(data, record);
  }

  function installIrradiationRecovery(record) {
    showErrorScreen({
      title: '照射を送信できませんでした',
      text: '通信が途中で切れた可能性があります。結果はまだ確定していません。もう一度試すと、同じ送信として確認します。',
      retryLabel: 'もう一度試す',
      onRetry: () => { void recoverIrradiationOperation(record); },
      statusText: '照射送信エラー',
    });
  }

  // Same-key recovery for an irradiation whose outcome is
  // unknown (R2): replay the frozen record; a terminal answer
  // releases it, another transport failure re-installs the
  // recovery. quote_changed means the server explicitly did NOT
  // execute — the record is released and the confirmation is
  // rebuilt fresh for the record's scan.
  async function recoverIrradiationOperation(record) {
    if (paidLedger.get('irradiation', record.scanId) !== record) return;
    if (!inFlight.tryStart('irradiation')) return;
    try {
      const navGen = navTracker.current();
      const result = await client.irradiate(
        record.confirmationId, record.requestKey, record.completionMode);
      if (result.kind === 'login') { failToLogin(); return; }
      // Definitive-outcome classification (round 3, T1): only a
      // validated success or a KNOWN business rejection settles
      // the record, even when this continuation's screen
      // ownership is gone (round 2, S2). Ambiguous outcomes
      // (kind 'error', generic 5xx JSON, unrecognized codes)
      // keep the record and its key for another same-key replay.
      const outcome = classifyMutationResult(result, {
        isSuccess: isIrradiationMutationSuccess,
        rejectedErrors: IRRADIATION_REJECTED_ERRORS,
        rejectedResponses: IRRADIATION_REJECTED_RESPONSES,
      });
      if (outcome !== 'ambiguous') {
        paidLedger.settle(record);
        if (outcome === 'rejected') {
          // The ended attempt's key retires with it (round 3,
          // T4), identity-guarded like registration's.
          state.irradiateKey = retireRequestKey(state.irradiateKey, record.requestKey, newKey);
        }
      }
      if (!navTracker.isCurrent(navGen)) return;
      if (outcome === 'ambiguous') { installIrradiationRecovery(record); return; }
      const data = result.data;
      if (data.error === 'quote_changed') {
        // Re-enter the confirmation screen (round 2, N1): the
        // in-flight guard must be RELEASED first — showConfirm
        // refuses entry while a submission is held, which left
        // the error screen up with no retry and no new prepare.
        // The transition is then awaited with this recovery's
        // navigation ownership intact (showConfirm opens the
        // new visit itself).
        if (record.scanId) {
          inFlight.end('irradiation');
          await showConfirm(record.scanId);
        }
        return;
      }
      if (isRejectedResponse(result, IRRADIATION_REJECTED_RESPONSES)) {
        // The scan is gone (round 4, N2): settled and retired
        // above, so the recovery ends here with the fact and
        // the way back to a fresh scan — not another replay.
        failScanNotFound();
        return;
      }
      if (data.error) { failGeneric(); return; }
      state.irradiateKey = newKey();
      landIrradiationSubmission(data, record);
    } finally {
      inFlight.end('irradiation');
    }
  }

  // Where an accepted irradiation submission lands (foundation
  // item 2): the RECORDED choice in the response governs, not
  // the choice this attempt asked for — a same-key replay can
  // legitimately answer with the first attempt's recorded
  // mode. Notify opens the notify-wait screen and lets the
  // completion mail carry the user back to the proof; anything
  // else opens the progress screen as before.
  function landIrradiationSubmission(data, record) {
    if (data.completion?.mode === 'notify'
        && enterNotifyWait('irradiation', data)) return;
    beginIrradiationProgress(data, record);
  }

  function beginIrradiationProgress(data, record = null) {
    // Heading and step label derive from the recorded state
    // from the first render (batch 2e, R7): the context is set
    // BEFORE the navigation renders the step label, and the
    // heading is stamped here and on every poll — a queued or
    // unknown job never opens under a fixed 照射中 claim.
    state.progressStep = { state: data.state || 'unknown' };
    navigate('progress');
    element('progress-title').textContent =
      progressTitleFor(state.progressStep.state);
    element('progress-bar').style.width = '0%';
    // The entry sentence derives from the submission response's
    // own state (#7): a running job says so, a replayed terminal
    // state says so, and a missing state falls back to the
    // explicit unknown wording — never a fixed "queued" claim.
    element('progress-text').textContent =
      stageTextFor(data.state ? data : { state: 'unknown' }, '照射');
    // The counts are text from the first moment and on every
    // poll, queued states included — the bar alone left the live
    // check with no numbers on screen. The completed side stays
    // "—" until the first poll reports it: the total is the
    // accepted quote's own count (a server-issued value), but no
    // completed count has been reported yet, and printing 0
    // would state it as one (device-consistency rule 7).
    // The total is THIS operation's count (round 2, N3): the
    // server response's own requested count first, then the
    // frozen record's count — never the newest mutable
    // confirmation, which may describe a different operation
    // the user prepared after this one was submitted.
    const operationTotal = data.execution?.requested_count ?? record?.count ?? null;
    element('progress-count').textContent =
      progressCountText(null, operationTotal);
    // The elapsed clock joins the wait screens' wording: how
    // long this screen has been open, stated as such (rule 2).
    // There is no stop button here — stopping is not part of
    // the UI (owner decision 2026-11-23); the job runs to
    // completion on the server either way. The closing note
    // promises the completion mail only while the session can
    // actually mail (foundation item 2).
    element('progress-note').textContent = waitMailNoteFor({
      // Same normalization as the scan entry (finding 3).
      operation: 'irradiation', completion: data.completion, state: data.state || 'unknown',
    });
    startElapsed('progress-elapsed');
    status('照射受付済み');
    const renderProgress = (view) => {
      state.progressStep = { state: view.state };
      syncStepLabel('progress');
      element('progress-title').textContent = progressTitleFor(view.state);
      // A missing completed_count stays unreported (#7): the
      // count text keeps "—" and no percentage is derived.
      const done = view.execution?.completed_count ?? null;
      const total = view.execution?.requested_count ?? operationTotal;
      const percent = progressPercent(done, total);
      element('progress-bar').style.width = `${percent ?? 0}%`;
      element('progress-count').textContent = progressCountText(done, total);
      element('progress-text').textContent = view.state === 'running'
          && done !== null && total !== null
        ? `照射を実行しています（${done} / ${total} 回）。`
        : stageTextFor(view, '照射');
      // The closing note is recomputed from EVERY received
      // view too (combined review finding 1): the terminal
      // render below runs this BEFORE the proof load starts,
      // so the screen can never show a completion headline
      // over a note still claiming the irradiation continues.
      element('progress-note').textContent = waitMailNoteFor({
        operation: 'irradiation', completion: view.completion, state: view.state,
      });
    };
    pollJob(data.job_id,
      (view) => { renderProgress(view); },
      (terminal) => {
        // Render the terminal payload HERE first: counts and
        // state from this very response, before any follow-up
        // fetch starts. Terminal responses used to bypass the
        // count renderer entirely, so a stalled proof fetch left
        // the progress screen at the previous poll's counts under
        // a "running" sentence indefinitely (Astra MEDIUM 4).
        // The payload itself is then handed to the proof screen
        // instead of being fetched a second time.
        renderProgress(terminal);
        void showProof(data.job_id, terminal);
      });
  }

  // prefetched: a job view the caller already holds (the
  // terminal poll payload, a history row's fetch). Rendering
  // from it directly keeps the proof screen from depending on a
  // second fetch of the same job; the balance below is still
  // refreshed independently.
  async function showProof(jobId, prefetched = null) {
    const navGen = beginNavIntent();
    let data = prefetched;
    if (!data) {
      const result = await client.job(jobId);
      if (!navTracker.isCurrent(navGen)) return;
      data = handle(result);
      if (!data) return;
    }
    // Degraded views earn no result presentation (batch 2d,
    // MEDIUM 8 round 4): the server's fallback shape pairs a
    // terminal state with an unrecognized (null) operation —
    // rendering the proof screen from it displayed placeholder
    // fields as a result and acknowledged it as seen. Route to
    // the unavailable screen instead, as the ranking guard does.
    if (TERMINAL_STATES.has(data.state)
        && data.operation !== 'scan' && data.operation !== 'irradiation') {
      failResultUnavailable();
      return;
    }
    state.proofJob = data;
    state.proofStep = { operation: data.operation, state: data.state };
    // A completed job says so at headline level on the proof
    // screen itself, not only in the step indicator — the live
    // check found no completion wording here. Non-completed
    // states (this screen is also their detail view) get none.
    const headline = proofHeadline(data.operation, data.state);
    element('proof-headline').hidden = !headline;
    element('proof-headline').textContent = headline || '';
    element('proof-state').textContent = STATE_LABEL[data.state] || data.state;
    const completed = data.execution?.completed_count;
    const requested = data.execution?.requested_count;
    // Unreported counts stay "—" (#7): a payload without a
    // completed_count has not said zero.
    element('proof-count').textContent = requested === null || requested === undefined
      ? `${completed ?? '—'} 回` : `${completed ?? '—'} / ${requested} 回`;
    const settled = settledCreditsOf(data.settlement);
    element('proof-settled').textContent = settled === null ? '—' : `${settled.toLocaleString('ja-JP')} クレジット`;
    element('proof-device').textContent = data.device?.public_device_id || '—';
    const digest = data.proof_digest || '';
    element('proof-digest').textContent = digest ? digest.slice(0, 16) + '…' : '—';
    element('proof-ranking').hidden = !(data.operation === 'scan' && data.state === 'completed');
    const session = await loadSession(navGen);
    // loadSession has already routed a failure (login or generic
    // error) to its own screen; continuing here would overwrite
    // that screen with the proof view. A newer navigation during
    // the balance refresh retires this render too (#5).
    if (!session || !navTracker.isCurrent(navGen)) return;
    element('proof-balance').textContent = session.available === null || session.available === undefined
      ? '確認できませんでした' : `${session.available.toLocaleString('ja-JP')} クレジット`;
    element('proof-copy-status').textContent = '';
    navigate('proof');
    status('稼働証明を表示しています。');
    // The ack belongs HERE (batch 2d, MEDIUM 6 round 2): only
    // once the proof screen has actually been navigated to with
    // this job's data current — an earlier ack (before the
    // session load above) recorded "viewed" for renders that a
    // failed load or an intervening navigation prevented from
    // ever being seen. The state check stays: this screen is
    // also the in-flight detail view.
    if (TERMINAL_STATES.has(data.state)) ackResultViewed(jobId);
  }

  async function showHistory() {
    const navGen = beginNavIntent();
    const result = await client.history();
    // Ownership before handle() (R1): a late history failure
    // must not replace a newer screen with the error screen.
    if (!navTracker.isCurrent(navGen)) return;
    const data = handle(result);
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
      row.className = 'secondary history-open';
      const when = new Date(sessionRow.created_at);
      const kindLabel = sessionRow.operation === 'scan' ? 'スキャン'
        : sessionRow.operation === 'reading' ? 'リーディング' : '照射';
      // The count joins the row only when the history API
      // returns one for the session (the same receive-point
      // pattern as the summary fields below: the foundation
      // shape is still landing, and a row without the field
      // renders exactly as before — never a reconstructed
      // number).
      const countPart = typeof sessionRow.count === 'number'
        ? `／${sessionRow.count.toLocaleString('ja-JP')} 回` : '';
      // The cost and its unit are bound with a non-breaking
      // space (audit B round 2): with a regular space the
      // row could wrap between the number and クレジット
      // and strand the unit on its own line. The characters
      // themselves are unchanged.
      row.textContent = `${when.toLocaleString('ja-JP')}／${kindLabel}${countPart}／`
        + `${STATE_LABEL[sessionRow.state] || sessionRow.state}／${sessionRow.cost.toLocaleString('ja-JP')}\u00A0クレジット`;
      // The display summary (target names, intention and the
      // affirmation actually used) rides along when the history
      // API returns it (2026-11-23 decision). The API shape is
      // fixed on the foundation side — each session gains
      // `targets` / `intention` / `self_love_joy`, snapshotted
      // at submission time outside the scan content's 24-hour
      // expiry — but the foundation release has not merged yet,
      // so rows without the fields render exactly as before.
      const detailLines = [];
      if (Array.isArray(sessionRow.targets) && sessionRow.targets.length) {
        detailLines.push(`対象：${sessionRow.targets.join('、')}`);
      }
      if (typeof sessionRow.intention === 'string' && sessionRow.intention) {
        detailLines.push(`インテンション：${sessionRow.intention}`);
      }
      if (typeof sessionRow.self_love_joy === 'string' && sessionRow.self_love_joy) {
        detailLines.push(`自己愛アファーメーション：${sessionRow.self_love_joy}`);
      }
      row.addEventListener('click', async () => {
        // Detail loads are navigation too (#5): a newer
        // navigation retires this continuation before it can
        // apply a screen.
        const detailGen = beginNavIntent();
        if (sessionRow.operation === 'reading') {
          const result = await client.reading(sessionRow.reading_id);
          if (!navTracker.isCurrent(detailGen)) return;
          const reading = handle(result);
          if (!reading) return;
          // History lists reserved and failed readings too:
          // only a completed one has a result to show. Rendering
          // the result screen for the others presented an empty
          // reading as if it were the outcome (Antigravity
          // glass audit, finding 5).
          if (reading.error || reading.state !== 'completed') {
            showErrorScreen({
              title: reading.state === 'reserved'
                ? 'リーディングを作成中です'
                : 'リーディングを表示できませんでした',
              text: reading.state === 'reserved'
                ? '作成が完了するまで少しお待ちいただき、履歴から開き直してください。'
                : 'このリーディングは完了していません。履歴から他の結果を確認できます。',
              retryLabel: '履歴へもどる',
              onRetry: () => { void showHistory(); },
              statusText: reading.state === 'reserved' ? 'リーディング作成中' : 'リーディング未完了',
            });
            return;
          }
          let scanView = null;
          if (reading.scan_id) {
            const jobResult = await client.job(reading.scan_id);
            if (jobResult.kind === 'login') { failToLogin(); return; }
            if (jobResult.kind === 'ok' && !jobResult.data.error) scanView = jobResult.data;
          }
          if (!navTracker.isCurrent(detailGen)) return;
          state.scanView = scanView;
          showReadingResult(reading, scanView);
          return;
        }
        const jobResult = await client.job(sessionRow.job_id);
        if (!navTracker.isCurrent(detailGen)) return;
        const view = handle(jobResult);
        if (!view) return;
        if (view.operation === 'scan' && view.state === 'completed'
            && view.content_removed === true) { failScanExpired(); return; }
        if (view.operation === 'scan' && view.state === 'completed') showRanking(view);
        else void showProof(sessionRow.job_id, view);
      });
      const wrap = document.createElement('div');
      wrap.className = 'history-record';
      wrap.append(row);
      for (const line of detailLines) {
        const detail = document.createElement('p');
        detail.className = 'note';
        detail.textContent = line;
        wrap.append(detail);
      }
      list.append(wrap);
    }
    navigate('history');
    status('履歴（直近20件）');
  }

  // Photos are normalized before they are sent (app-image.js):
  // shrunk to a 2048px long edge and re-encoded as JPEG, so a
  // high-pixel phone photo no longer hits the server size limits.
  // A photo the browser cannot decode (HEIC in a browser without
  // HEIC support, a corrupt file) is reported here, next to the
  // form, instead of being sent and rejected — or, as before,
  // silently read as "no photo chosen".
  const PHOTO_ERROR_TEXT = {
    decode_failed: '写真を読み込めませんでした。HEICなどの形式は、このブラウザでは扱えないことがあります。写真アプリでJPEGに変換するか、写真のスクリーンショットを保存して、選び直してください。',
    too_large: '写真が大きすぎます。もう少し小さい写真を選び直してください。',
  };

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
    hideGiftOverlay();
  });
  // 案B chrome wiring: the appbar back button clicks the current
  // screen's own back control (see SCREEN_BACK), and the dock
  // items run the same destinations as the gear sheet's rows.
  element('nav-back-button').addEventListener('click', () => {
    const backId = SCREEN_BACK[state.currentScreen];
    const backControl = backId ? element(backId) : null;
    if (backControl && !backControl.hidden && !backControl.disabled) backControl.click();
  });
  element('dock-home').addEventListener('click', () => { void showHome(); });
  element('dock-history').addEventListener('click', () => { void showHistory(); });
  element('dock-account').addEventListener('click', () => { void showAccount(); });
  element('dock-purchase').addEventListener('click', () => { void showPurchase({ from: 'gear' }); });
  element('gear-button').addEventListener('click', openGear);
  element('gear-close').addEventListener('click', closeGear);
  // The Escape handler replaces any previous registration:
  // showApp() can run again on the same document (entry
  // re-entry), and stacked anonymous listeners would each
  // fire closeGear on every Escape (audit B MEDIUM-2).
  if (document.__siGearKeydown) {
    document.removeEventListener('keydown', document.__siGearKeydown);
  }
  document.__siGearKeydown = (event) => {
    if (event.key !== 'Escape') return;
    const gift = element('gift-overlay');
    if (gift && !gift.hidden) { hideGiftOverlay(); return; }
    const sheet = element('gear-sheet');
    if (sheet && !sheet.hidden) closeGear();
  };
  document.addEventListener('keydown', document.__siGearKeydown);
  element('gear-sheet').addEventListener('click', (event) => {
    if (event.target === event.currentTarget) closeGear();
  });
  element('gear-history').addEventListener('click', () => { closeGear(); void showHistory(); });
  element('gear-account').addEventListener('click', () => { closeGear(); void showAccount(); });
  element('gear-purchase').addEventListener('click', () => { closeGear(); void showPurchase({ from: 'gear' }); });
  element('home-register').addEventListener('click', () => { startRegistration('home'); });
  element('home-scan').addEventListener('click', () => { startScanFlow(); });
  element('account-back').addEventListener('click', () => { void showHome(); });
  element('account-purchase').addEventListener('click', () => { void showPurchase({ from: 'account' }); });
  element('purchase-back').addEventListener('click', () => { purchaseBack(); });
  element('purchase-home').addEventListener('click', () => { void showHome(); });
  element('ranking-guide').addEventListener('click', () => { showGuide('ranking'); });
  element('guide-back').addEventListener('click', () => {
    if (state.guideReturn === 'ranking' && state.scanView) showRanking(state.scanView);
    else void showHome();
  });
  // The example sentence on the affirmation screen and the
  // sentence the delegated path submits are one constant
  // (app-flow.js), so the display can never drift from the
  // submission.
  element('oq-affirmation-example').textContent = OMAKASE_AFFIRMATION;
  element('target-add').addEventListener('click', () => {
    state.photoResetPending = true;
    state.photoVisit = photoVisitTracker.begin();
    flowGo('target-photo');
  });
  element('target-manage').addEventListener('click', () => { void showTargets(); });
  element('target-back').addEventListener('click', () => { void showHome(); });
  element('oq-photo').addEventListener('change', () => { void onPhotoChosen(); });
  element('oq-photo-next').addEventListener('click', () => { flowGo('target-name'); });
  element('oq-photo-back').addEventListener('click', () => { flowBack(); });
  element('oq-name-submit').addEventListener('click', () => { void submitTargetRegistration(); });
  element('oq-name-back').addEventListener('click', () => { flowBack(); });
  element('oq-intention').addEventListener('input', () => {
    state.draft.intention = element('oq-intention').value;
  });
  element('oq-affirmation').addEventListener('input', () => {
    const value = element('oq-affirmation').value;
    if (value) {
      state.draft.affirmationMode = 'custom';
      state.draft.affirmation = value;
    } else if (state.draft.affirmationMode === 'custom') {
      state.draft.affirmation = value;
    }
  });
  element('oq-intention-next').addEventListener('click', () => { submitIntention(); });
  element('oq-intention-back').addEventListener('click', () => { flowBack(); });
  element('oq-affirmation-next').addEventListener('click', () => { submitAffirmation('custom'); });
  element('oq-affirmation-omakase').addEventListener('click', () => { submitAffirmation('omakase'); });
  element('oq-affirmation-back').addEventListener('click', () => { flowBack(); });
  element('oq-confirm-submit').addEventListener('click', () => {
    // The wait/notify choice (foundation item 2) is its own
    // step after the confirmation, asked every time — but only
    // while the session offers notify. Without the offer the
    // confirmation submits directly with mode "wait" and no
    // screen or copy mentions mail. The draft gate runs here
    // first so an out-of-order entry still fails in place on
    // this screen (submitScanFlow keeps its own as backstop).
    if (draftProblem(state.draft)) {
      formError('oq-confirm-error', '入力が不足しています。もどって確認してください。');
      return;
    }
    formError('oq-confirm-error', null);
    if (notifyAvailable()) {
      state.waitMethod = { operation: 'scan', notifySuppressed: false };
      flowGo('wait-method');
      return;
    }
    void submitScanFlow('wait');
  });
  element('wait-method-wait').addEventListener('click', () => {
    submitWaitMethodChoice('wait');
  });
  element('wait-method-notify').addEventListener('click', () => {
    submitWaitMethodChoice('notify');
  });
  element('wait-method-back').addEventListener('click', () => {
    if (state.waitMethod?.operation === 'irradiation') {
      // Back to the irradiation confirmation: a fresh visit
      // re-prepares its quote, exactly like returning from the
      // count screen does.
      if (state.confirmScanId) void showConfirm(state.confirmScanId);
      return;
    }
    flowBack();
  });
  element('notify-wait-home').addEventListener('click', () => { void showHome(); });
  element('oq-confirm-back').addEventListener('click', () => { flowBack(); });
  element('targets-add').addEventListener('click', () => { startRegistration('targets'); });
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
  element('ranking-next').addEventListener('click', () => {
    if (!state.scanView) return;
    if (state.session?.reading?.enabled) {
      void showReadingConfirm(state.scanView.job_id);
    } else {
      showCount(state.scanView.job_id);
    }
  });
  element('reading-confirm-submit').addEventListener('click', () => { void submitReading(); });
  element('reading-confirm-purchase').addEventListener('click', () => {
    void showPurchase({
      from: 'reading', scanId: state.readingConfirmation?.scan_id,
      count: null, shortage: state.readingShortage,
    });
  });
  element('reading-confirm-skip').addEventListener('click', () => {
    if (!state.readingPrepared || !state.readingConfirmation) return;
    if (state.readingConfirmation.scan_id !== state.readingBinding?.scanId) return;
    showCount(state.readingConfirmation.scan_id);
  });
  element('reading-confirm-back').addEventListener('click', () => {
    void (async () => {
      const scanId = state.readingBinding?.scanId;
      if (!scanId) {
        if (state.scanView) showRanking(state.scanView);
        return;
      }
      const view = await resolveScanView(scanId);
      if (view) showRanking(view);
    })();
  });
  element('reading-copy').addEventListener('click', () => {
    void (async () => {
      const reading = state.currentReading;
      if (!reading || !reading.text) return;
      const view = reading.scan_id
        ? await resolveScanView(reading.scan_id) : state.scanView;
      void copyText(readingCopyText({
        intention: view?.intention || '', selfLoveJoy: view?.self_love_joy || '',
        text: reading.text, at: new Date(),
      }), element('reading-copy-status'), 'リーディングをコピーしました。');
    })();
  });
  element('reading-irradiate').addEventListener('click', () => {
    if (state.currentReading) showCount(state.currentReading.scan_id);
  });
  element('reading-back').addEventListener('click', () => {
    void (async () => {
      const scanId = state.currentReading?.scan_id;
      if (!scanId) {
        if (state.scanView) showRanking(state.scanView);
        return;
      }
      const view = await resolveScanView(scanId);
      if (view) showRanking(view);
    })();
  });
  element('reading-done').addEventListener('click', () => { void showHome(); });
  element('ranking-rescan').addEventListener('click', () => { startScanFlow(); });
  element('ranking-done').addEventListener('click', () => { void showHome(); });
  element('count-slider').addEventListener('input', renderCount);
  for (const button of document.querySelectorAll('.preset')) {
    button.addEventListener('click', () => {
      element('count-slider').value = button.dataset.count;
      renderCount();
    });
  }
  element('count-next').addEventListener('click', () => {
    if (state.countScanId) void showConfirm(state.countScanId);
  });
  element('count-back').addEventListener('click', () => {
    if (state.scanView) showRanking(state.scanView);
    else void showHome();
  });
  element('confirm-submit').addEventListener('click', () => {
    // The recovery view's button (round 2, N3) reconciles the
    // frozen unresolved operation — it never submits the
    // mutable confirmation state.
    if (state.confirmRecovery) {
      const record = state.confirmRecovery;
      state.confirmRecovery = null;
      void recoverIrradiationOperation(record);
      return;
    }
    // An unaffordable confirmation turns the primary button
    // into the purchase branch (renderConfirm set the flag and
    // the label); since increment 4 the branch lands on the
    // in-app purchase guide, carrying the scan, the count and
    // the exact shortage, so buying there leads straight back
    // to this confirmation.
    if (state.confirmInsufficient) {
      void showPurchase({
        from: 'confirm', scanId: state.confirmScanId,
        count: state.selectedCount, shortage: state.confirmShortage,
      });
      return;
    }
    // The wait/notify choice (foundation item 2) is asked on
    // its own screen after this confirmation, every time —
    // but only while the session offers notify. Without the
    // offer the confirmation submits directly with mode
    // "wait", as it did before the choice existed.
    if (notifyAvailable()) {
      state.waitMethod = { operation: 'irradiation', notifySuppressed: false };
      state.completionMode = 'wait';
      showWaitMethod();
      return;
    }
    state.completionMode = 'wait';
    void submitIrradiation();
  });
  element('confirm-back').addEventListener('click', () => {
    if (state.confirmScanId) showCount(state.confirmScanId);
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
  element('error-retry').addEventListener('click', () => {
    // Consume before running: the retried operation may land
    // on another error, which installs its own retry.
    const retry = state.errorRetry;
    state.errorRetry = null;
    if (retry) retry();
  });
  element('update-reload').addEventListener('click', () => { location.reload(); });
  element('update-dismiss').addEventListener('click', () => {
    versionMonitor.dismiss();
    element('update-banner').hidden = true;
  });
  // Returning to a backgrounded tab (or a resumed PWA) is the
  // moment a stale build matters most: check then, throttled.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') void versionMonitor.check();
  });

  void (async () => {
    await auth.completeRedirect();
    auth.takeFragment();
    void versionMonitor.check();
    const token = await auth.getToken();
    if (!token) { navigate('login'); status('ログインしてください。'); return; }
    // The paid-operation ledger's scope is this authenticated
    // session (S2); failToLogin clears it when the session ends.
    paidLedger.setScope('app-session');
    await showHome();
    // A stored purchase detour (a pack tap left for the
    // payment link) lands back on its confirmation — but only
    // when home was actually reached.
    await restorePurchaseReturn();
    // A completion-mail resume link lands after the purchase
    // return: both are boot landings gated on home, and the
    // purchase detour is the older, narrower one.
    await restoreResumeLanding();
  })();
}
