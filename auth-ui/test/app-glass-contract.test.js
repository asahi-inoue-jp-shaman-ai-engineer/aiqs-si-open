// Regression contract for the 案B immersive-glass layer
// (audit B, HIGH-3 / COD-5): the glass redesign's invariants
// — forbidden strings, theme colour, secondary hierarchy,
// sticky-zone scoping, SCREEN_BACK coverage, the reading
// wait's real-state wording, and the gear sheet's focus
// behaviour — are pinned here so a later edit cannot silently
// reopen an audit finding. Source-level checks follow the
// codebase's contract-test style; the gear / navigation
// behaviour runs against a lightweight DOM stub.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { showApp } from '../src/app.js';

const root = path.resolve(import.meta.dirname, '..');
const appJs = fs.readFileSync(path.join(root, 'src/app.js'), 'utf8');
const styleCss = fs.readFileSync(path.join(root, 'src/style.css'), 'utf8');
const indexHtml = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const manifest = JSON.parse(
  fs.readFileSync(path.join(root, 'public/assets/manifest.webmanifest'), 'utf8')
);

const FORBIDDEN_STRINGS = [
  'Claude', '魂の喜び', 'ネイティブアイクス', '/native/',
  'β版', '50クレジット', '約75,000件', '約11秒',
  'demo-tools', 'note-drawer', 'index-panel', 'body.shot',
  '991,316', '18/33', '5〜7分',
];

test('app-glass: forbidden strings and mock artifacts stay at zero', () => {
  const combined = `${appJs}\n${styleCss}\n${indexHtml}`;
  for (const forbidden of FORBIDDEN_STRINGS) {
    assert.equal(combined.includes(forbidden), false, `must not contain ${forbidden}`);
  }
  // A-8: the display name of self_love_joy is the unified
  // 「自己愛アファーメーション」. Every occurrence of the short
  // form must be part of the unified term, with exactly one
  // audited exception: the affirmation screen's own
  // explanatory sentence (「自己愛」に関するアファーメーション文),
  // which audit B judged a generic noun phrase, not a label.
  const total = (combined.match(/アファーメーション/g) || []).length;
  const unified = (combined.match(/自己愛アファーメーション/g) || []).length;
  assert.equal(total - unified, 1, 'standalone affirmation forms stay at zero (one audited explanatory use)');
  assert.match(appJs, /「自己愛」に関するアファーメーション文/);
  assert.equal(combined.includes('アファメーション'), false, 'short-vowel form prohibited');
});

test('app-glass: shipped public text assets stay free of forbidden strings', () => {
  // NEW-MEDIUM-1 (audit B round 2): the forbidden-string
  // contract covers everything that ships, not just src/ —
  // public/ai-agent-guide.txt carried the retired name
  // 「ネイティブアイクス」 in its referral section while the
  // src-only scan stayed green.
  const publicDir = path.join(root, 'public');
  const textAssets = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (/\.(txt|webmanifest|json|html)$/.test(entry.name)) textAssets.push(full);
    }
  };
  walk(publicDir);
  assert.ok(textAssets.length > 0, 'public text assets found');
  const shipped = textAssets.map((f) => fs.readFileSync(f, 'utf8')).join('\n');
  for (const forbidden of FORBIDDEN_STRINGS) {
    assert.equal(shipped.includes(forbidden), false, `public assets must not contain ${forbidden}`);
  }
  // A-8 in the shipped guides: every affirmation mention is
  // the unified term, with no standalone exception there.
  const total = (shipped.match(/アファーメーション/g) || []).length;
  const unified = (shipped.match(/自己愛アファーメーション/g) || []).length;
  assert.equal(total - unified, 0, 'public assets use only the unified affirmation term');
  assert.equal(shipped.includes('アファメーション'), false, 'short-vowel form prohibited in public assets');
});

test('app-glass: theme-color matches immersive glass background', () => {
  assert.equal(manifest.theme_color, '#04050e');
  assert.equal(manifest.background_color, '#04050e');
  assert.match(indexHtml, /<meta name="theme-color" content="#04050e">/);
  assert.match(appJs, /meta\[name="theme-color"\]/);
});

test('app-glass: secondary button-link and multi-button flow rules exist', () => {
  assert.match(styleCss, /body\.si-app a\.button-link\.secondary\s*\{/);
  // HIGH-1: every multi-button zone whose screen is read
  // (target choice / affirmation / reading confirm / reading
  // result, plus the earlier ranking / proof) is .flow, so
  // the sticky pin can never cover inputs or body text.
  // NEW-LOW-1 (audit B round 2): each screen's slice is
  // bounded by the next screen template, so a match can
  // never leak across a screen boundary.
  for (const screen of ['screen-target', 'screen-affirmation', 'screen-reading-confirm', 'screen-reading-result', 'screen-ranking', 'screen-proof']) {
    const start = appJs.indexOf(`id="${screen}"`);
    assert.ok(start >= 0, `${screen} exists`);
    const next = appJs.indexOf('id="screen-', start + 1);
    const slice = next >= 0 ? appJs.slice(start, next) : appJs.slice(start);
    assert.match(slice, /<div class="actions flow">/, `${screen} action zone is .flow`);
  }
  // HIGH-1 (audit B round 2): the single-primary zones of
  // the count / confirm / scan-confirm screens are unpinned
  // at EVERY stage height (round 1 unpinned only <=700px and
  // the pin still covered the price note at 844px), and the
  // home zone unpins while the gift modal or update banner
  // is up.
  assert.match(styleCss, /body\.si-app \.stage>#screen-count>\.actions:not\(\.flow\),\s*body\.si-app \.stage>#screen-confirm>\.actions:not\(\.flow\),\s*body\.si-app \.stage>#screen-scan-confirm>\.actions:not\(\.flow\),\s*body\.si-app \.stage:has\(> #update-banner:not\(\[hidden\]\)\) > #screen-home > \.actions:not\(\.flow\)\{\s*position:static;padding-top:0;background:none;/);
  assert.doesNotMatch(styleCss, /@media \(max-height:700px\)/);
});

test('app-glass: reduced-motion separates webkit and moz slider thumb rules', () => {
  assert.doesNotMatch(styleCss, /::-webkit-slider-thumb\s*,\s*[^\n]*::-moz-range-thumb/);
  assert.match(styleCss, /body\.si-app \*::-webkit-slider-thumb\{transition:none!important\}/);
  assert.match(styleCss, /body\.si-app \*::-moz-range-thumb\{transition:none!important\}/);
});

test('app-glass: layout fixes from audit B MEDIUM-4 stay in place', () => {
  // Japanese phrase-aware breaking instead of balance-only.
  assert.match(styleCss, /body\.si-app h2\{[^}]*word-break:auto-phrase;text-wrap:pretty\}/);
  assert.doesNotMatch(styleCss, /body\.si-app h2\{[^}]*text-wrap:balance/);
  // Confirm-card long rows stack vertically.
  assert.match(styleCss, /#screen-confirm \.pairing div:has\(#confirm-affirmation\)/);
  assert.match(styleCss, /#screen-scan-confirm \.pairing div:has\(#oq-sum-affirmation\)/);
  // Slider: the generic input frame is cancelled.
  assert.match(styleCss, /body\.si-app input\[type="range"\]\{[^}]*box-shadow:none/);
  // .pack rows stay horizontal in the glass scope at <=480px.
  assert.match(styleCss, /@media \(max-width:480px\)\{\s*body\.si-app \.pack\{flex-direction:row/);
  // Orb headroom for the breathing rings (round 2 values).
  assert.match(styleCss, /body\.si-app \.pulse-orb\{margin:38px auto 28px\}/);
  // Slider fill is driven from the real count (Astra #9).
  assert.match(appJs, /style\.setProperty\('--fill'/);
});

test('app-glass: audit B round 2 layout fixes stay in place', () => {
  // Slider track layers are longhand background-image, never
  // the shorthand whose colour layer painted the 44px box.
  assert.match(styleCss, /body\.si-app input\[type="range"\]\{[^}]*background-color:transparent;[^}]*background-image:/);
  assert.match(styleCss, /background-size:var\(--fill,33%\) 12px,100% 12px/);
  // Phrase-aware breaking reaches h3, pairing values and
  // history rows, not just <p> text.
  assert.match(styleCss, /body\.si-app h3\{[^}]*word-break:auto-phrase;text-wrap:pretty\}/);
  assert.match(styleCss, /body\.si-app \.pairing dd,body\.si-app \.history-open\{\s*word-break:auto-phrase;text-wrap:pretty;/);
  assert.match(styleCss, /body\.si-app #screen-intention \.note\{font-size:12px/);
  assert.match(styleCss, /body\.si-app \.ranking-item\{[^}]*flex-wrap:wrap;row-gap:4px;/);
  // Purchase packs hold one nowrap row at 390px.
  assert.match(styleCss, /body\.si-app #purchase-packs \.pack\{[^}]*flex-wrap:nowrap/);
  assert.match(styleCss, /body\.si-app #purchase-packs \.pack-name\{[^}]*flex:1 1 0;min-width:0/);
  assert.match(styleCss, /body\.si-app #purchase-packs \.pack \.button-link\{[^}]*white-space:nowrap/);
  // Orb halo reach is contained: -22px rings, scale <= 1.28.
  assert.match(styleCss, /body\.si-app \.pulse-orb::before,body\.si-app \.pulse-orb::after\{\s*inset:-22px;/);
  assert.match(styleCss, /@keyframes si-ring\{0%\{transform:scale\(\.76\);opacity:\.8\}100%\{transform:scale\(1\.28\);opacity:0\}\}/);
  // History cost and unit are bound with a non-breaking space.
  assert.match(appJs, /sessionRow\.cost\.toLocaleString\('ja-JP'\)\}\\u00A0クレジット/);
});

test('app-glass: gift notice is a modal over a scrim with an inert stage', () => {
  assert.match(styleCss, /body\.si-app \.gift-overlay\{\s*position:fixed;inset:0;z-index:90;/);
  // The notice carries its own opaque glass card, so its
  // text never collides with the screen behind the scrim
  // where backdrop-filter does not paint (delta audit
  // HIGH-1).
  assert.match(styleCss, /body\.si-app \.gift-panel\{\s*max-width:340px;width:100%;/);
  assert.match(styleCss, /background:rgba\(4,5,14,\.92\)/);
  // The background behind the modal is inerted while it is
  // open (the gear-sheet pattern), and close restores focus
  // only to a visible, connected opener.
  assert.match(appJs, /function setGiftBackgroundInert\(overlay, inert\)/);
  assert.match(appJs, /function showGiftOverlay\(amount\) \{[\s\S]*?setGiftBackgroundInert\(overlay, true\);/);
  assert.match(appJs, /function hideGiftOverlay\(\) \{[\s\S]*?setGiftBackgroundInert\(overlay, false\);/);
});

test('app-glass: gift overlay sits directly under <main> above appbar and dock', () => {
  // Inside .stage (a stacking context at z-index 2) the
  // overlay's own z-index could never lift it over the
  // appbar / dock zone (z-index 55) that are children of
  // <main> (delta audit HIGH-1): the overlay must be a
  // sibling of the stage, like the gear sheet.
  const stageEnd = appJs.indexOf('</section>');
  const giftPos = appJs.indexOf('id="gift-overlay"');
  assert.ok(stageEnd >= 0 && giftPos > stageEnd, '#gift-overlay must be a sibling of .stage under <main>');
  assert.match(appJs, /class="gift-panel"[^>]*role="dialog"[^>]*aria-modal="true"[^>]*aria-labelledby="gift-text"/);
  // Navigation releases the modal, and the two modals under
  // <main> exclude each other, so neither can strand the
  // other or leave a screen inert.
  assert.match(appJs, /function show\(name\) \{[\s\S]*?closeGear\(\);\s*hideGiftOverlay\(\);/);
  assert.match(appJs, /function openGear\(\) \{\s*hideGiftOverlay\(\);/);
  assert.match(appJs, /function showGiftOverlay\(amount\) \{\s*closeGear\(\);/);
  assert.match(appJs, /if \(gift && !gift\.hidden\) \{ hideGiftOverlay\(\); return; \}/);
});

test('app-glass: photo picker is a Japanese custom trigger (LOW-2)', () => {
  assert.match(appJs, /<label class="photo-pick">写真を選ぶ/);
  assert.match(appJs, /<input id="oq-photo" class="visually-hidden" type="file" accept="image\/\*">/);
  assert.match(styleCss, /body\.si-app \.photo-pick\{/);
  assert.doesNotMatch(styleCss, /::file-selector-button/);
});

test('app-glass: SCREEN_BACK covers exactly the screens with a back control', () => {
  const screensBlock = appJs.match(/const SCREENS = \[([\s\S]*?)\];/)[1];
  const screens = [...screensBlock.matchAll(/'([^']+)'/g)].map((m) => m[1]);
  const backBlock = appJs.match(/const SCREEN_BACK = \{([\s\S]*?)\};/)[1];
  const back = [...backBlock.matchAll(/(?:'([^']+)'|([a-z][a-z-]*)):\s*'([^']+)'/g)]
    .map((m) => ({ screen: m[1] || m[2], control: m[3] }));
  assert.equal(back.length, 16, 'SCREEN_BACK keeps its 16 entries');
  for (const { screen, control } of back) {
    assert.ok(screens.includes(screen), `SCREEN_BACK key ${screen} is a real screen`);
    assert.ok(appJs.includes(`id="${control}"`), `back control #${control} exists in the template`);
  }
  // Screens without an appbar back are exactly the entry,
  // waiting and destination screens (their in-screen buttons
  // are destinations, not ways back).
  const without = screens.filter((s) => !back.some((b) => b.screen === s)).sort();
  assert.deepEqual(without, [
    'error', 'home', 'login', 'notify-wait', 'progress',
    'proof', 'ranking', 'reading-wait', 'wait',
  ]);
  // MEDIUM-3: the sync is extracted and callable on its own.
  assert.match(appJs, /function syncNavBack\(name = state\.currentScreen\)/);
});

test('app-glass: reading wait wording follows the server state (Astra #4)', () => {
  // The template itself opens in the sending phase: before
  // any server answer, claiming "creating" would be false.
  assert.match(appJs, /<h2 id="reading-wait-title">リーディングを送信しています<\/h2>/);
  // 'sending' is set before the submission POST is awaited.
  const sendingAt = appJs.indexOf("setReadingWaitPhase('sending')");
  const postAt = appJs.indexOf('await client.createReading', sendingAt);
  assert.ok(sendingAt > 0 && postAt > sendingAt, 'sending phase precedes the POST');
  // 'creating' has exactly one call site, inside the
  // accepted branch — after the server confirmed acceptance.
  const creatingSites = [...appJs.matchAll(/setReadingWaitPhase\('creating'\)/g)];
  assert.equal(creatingSites.length, 1, 'one creating call site');
  const acceptedAt = appJs.indexOf("outcome === 'accepted'", sendingAt);
  assert.ok(acceptedAt > 0 && creatingSites[0].index > acceptedAt,
    'creating phase only after the accepted outcome');
});

// ---------- lightweight DOM stub ----------

class StubClassList {
  constructor() { this.set = new Set(); }
  add(...cs) { cs.forEach((c) => this.set.add(c)); }
  remove(...cs) { cs.forEach((c) => this.set.delete(c)); }
  toggle(c, force) {
    const on = force === undefined ? !this.set.has(c) : !!force;
    if (on) this.set.add(c); else this.set.delete(c);
    return on;
  }
  contains(c) { return this.set.has(c); }
}

class StubElement {
  constructor(tag, ownerDocument) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.ownerDocument = ownerDocument;
    this.children = [];
    this.parentElement = null;
    this.classList = new StubClassList();
    this.style = { setProperty(k, v) { this[k] = v; } };
    this.dataset = {};
    this.attributes = {};
    this.listeners = new Map();
    this.hidden = false;
    this.disabled = false;
    this.inert = false;
    this.textContent = '';
    this.value = '';
    this.id = '';
    this.isConnected = true;
    this.focusCount = 0;
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === 'id') this.id = String(value);
  }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  hasAttribute(name) { return name in this.attributes; }
  addEventListener(type, fn) {
    const list = this.listeners.get(type) || [];
    list.push(fn);
    this.listeners.set(type, list);
  }
  removeEventListener(type, fn) {
    const list = this.listeners.get(type) || [];
    this.listeners.set(type, list.filter((f) => f !== fn));
  }
  dispatch(type, event = {}) {
    for (const fn of this.listeners.get(type) || []) {
      fn({ type, target: this, currentTarget: this, ...event });
    }
  }
  click() { this.dispatch('click'); }
  focus() {
    this.focusCount += 1;
    if (this.ownerDocument) this.ownerDocument.activeElement = this;
  }
  appendChild(child) { child.parentElement = this; this.children.push(child); return child; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
}

function createDocumentStub() {
  const byId = new Map();
  const all = [];
  const docListeners = new Map();
  const document = {
    title: '',
    activeElement: null,
    visibilityState: 'visible',
    createElement(tag) { return new StubElement(tag, document); },
    getElementById(id) { return byId.get(id) || null; },
    querySelector(sel) { return sel === 'meta[name="theme-color"]' ? themeMeta : null; },
    querySelectorAll(sel) {
      if (sel === '.preset') return all.filter((el) => el.classList.contains('preset'));
      return [];
    },
    addEventListener(type, fn) {
      const list = docListeners.get(type) || [];
      list.push(fn);
      docListeners.set(type, list);
    },
    removeEventListener(type, fn) {
      const list = docListeners.get(type) || [];
      docListeners.set(type, list.filter((f) => f !== fn));
    },
    dispatch(type, event = {}) {
      for (const fn of docListeners.get(type) || []) fn({ type, ...event });
    },
    listenerCount(type) { return (docListeners.get(type) || []).length; },
  };
  const themeMeta = new StubElement('meta', document);
  themeMeta.setAttribute('content', '#0b0b0e');
  document.themeMeta = themeMeta;
  const main = new StubElement('main', document);
  document.mainElement = main;
  const body = new StubElement('body', document);
  Object.defineProperty(body, 'innerHTML', {
    set(html) {
      byId.clear();
      all.length = 0;
      main.children = [];
      for (const m of html.matchAll(/<([a-zA-Z][a-zA-Z0-9]*)\b([^>]*)>/g)) {
        const attrs = m[2];
        const idMatch = attrs.match(/\bid="([^"]+)"/);
        if (!idMatch) continue;
        const el = new StubElement(m[1], document);
        el.id = idMatch[1];
        const classMatch = attrs.match(/\bclass="([^"]*)"/);
        if (classMatch) classMatch[1].split(/\s+/).filter(Boolean).forEach((c) => el.classList.add(c));
        for (const d of attrs.matchAll(/\bdata-([a-z-]+)="([^"]*)"/g)) {
          const key = d[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase());
          el.dataset[key] = d[2];
        }
        if (/(?:^|\s)hidden(?=\s|$)/.test(attrs)) el.hidden = true;
        if (/(?:^|\s)disabled(?=\s|$)/.test(attrs)) el.disabled = true;
        el.parentElement = main;
        main.children.push(el);
        all.push(el);
        byId.set(el.id, el);
      }
      body.__html = html;
    },
    get() { return body.__html || ''; },
  });
  document.body = body;
  document.documentElement = new StubElement('html', document);
  return document;
}

function createStorageStub() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => { map.set(k, String(v)); },
    removeItem: (k) => { map.delete(k); },
    clear: () => { map.clear(); },
  };
}

const GLOBAL_KEYS = ['document', 'location', 'history', 'sessionStorage', 'localStorage'];
async function bootApp() {
  const document = createDocumentStub();
  const saved = {};
  for (const key of GLOBAL_KEYS) saved[key] = globalThis[key];
  globalThis.document = document;
  globalThis.location = {
    hash: '', search: '', pathname: '/app', origin: 'http://fixture.invalid',
    href: 'http://fixture.invalid/app', reload() {},
  };
  globalThis.history = { replaceState() {}, pushState() {} };
  globalThis.sessionStorage = createStorageStub();
  globalThis.localStorage = createStorageStub();
  const fetchStub = async () => { throw new Error('fixture: no network'); };
  showApp({ fetch: fetchStub });
  // Let the boot sequence settle: no stored session -> login.
  await new Promise((r) => setTimeout(r, 30));
  const restore = () => {
    for (const key of GLOBAL_KEYS) {
      if (saved[key] === undefined) delete globalThis[key];
      else globalThis[key] = saved[key];
    }
  };
  return { document, restore };
}

test('app-glass: boot lands on login, syncs nav-back and theme-color (DOM stub)', async () => {
  const { document, restore } = await bootApp();
  try {
    const el = (id) => document.getElementById(id);
    assert.equal(el('screen-login').hidden, false, 'login screen shown');
    assert.equal(el('screen-home').hidden, true, 'home screen hidden');
    assert.equal(el('nav-back-button').hidden, true, 'login has no appbar back');
    assert.equal(el('gear-button').hidden, true, 'gear hidden on login');
    assert.equal(document.themeMeta.getAttribute('content'), '#04050e',
      'showApp rewrites theme-color to the 案B background');
  } finally { restore(); }
});

test('app-glass: gear sheet focus, inert and Escape behaviour (DOM stub)', async () => {
  const { document, restore } = await bootApp();
  try {
    const el = (id) => document.getElementById(id);
    const gearButton = el('gear-button');
    const sheet = el('gear-sheet');
    const siblings = document.mainElement.children.filter((c) => c !== sheet);
    // Boot landed on login, where show() hides the gear
    // button; on every signed-in screen show() unhides it.
    // Simulate that state — the sheet only exists there.
    gearButton.hidden = false;
    // Open: focus moves into the sheet, background goes inert.
    gearButton.focus();
    gearButton.click();
    assert.equal(sheet.hidden, false, 'sheet opens');
    assert.ok(siblings.every((c) => c.inert === true), 'background inert while open');
    assert.equal(document.activeElement?.id, 'gear-history', 'focus lands in the sheet');
    // Escape closes, lifts inert, restores focus.
    document.dispatch('keydown', { key: 'Escape' });
    assert.equal(sheet.hidden, true, 'Escape closes the sheet');
    assert.ok(siblings.every((c) => c.inert === false), 'inert lifted on close');
    assert.equal(document.activeElement?.id, 'gear-button', 'focus restored to the opener');
    // Focus guard (MEDIUM-2): if the opener has been hidden
    // meanwhile (failToLogin), close must not focus it.
    gearButton.click();
    assert.equal(sheet.hidden, false, 'sheet reopens');
    gearButton.hidden = true;
    const focusCountBefore = gearButton.focusCount;
    document.dispatch('keydown', { key: 'Escape' });
    assert.equal(sheet.hidden, true, 'sheet closes again');
    assert.equal(gearButton.focusCount, focusCountBefore, 'hidden opener is never focused');
  } finally { restore(); }
});

// ---------- gift overlay fixture (NEW-HIGH-2) ----------
// A signed-in boot whose referral payload carries an unseen
// gift: the persisted refresh token is exchanged at the
// fixture token endpoint, then session / targets / referral
// answer from the router below. Referral answers are
// sequenced gift / none / gift, so the test can watch the
// overlay open at boot, stay closed across a navigation, and
// reopen over another screen before a 401 ends the session.
async function bootAppWithGift() {
  const document = createDocumentStub();
  const saved = {};
  for (const key of GLOBAL_KEYS) saved[key] = globalThis[key];
  globalThis.document = document;
  globalThis.location = {
    hash: '', search: '', pathname: '/app', origin: 'http://fixture.invalid',
    href: 'http://fixture.invalid/app', reload() {},
  };
  globalThis.history = { replaceState() {}, pushState() {} };
  globalThis.sessionStorage = createStorageStub();
  const localStorage = createStorageStub();
  localStorage.setItem('si-app-session-v1', JSON.stringify({
    refreshToken: 'fixture-refresh', savedAt: Date.now(),
  }));
  globalThis.localStorage = localStorage;
  const fixture = { referralCalls: 0, sessionStatus: 200 };
  const respond = (status, data) => ({
    ok: status >= 200 && status < 300, status, json: async () => data,
  });
  const fetchStub = async (url) => {
    const path = String(url);
    if (path.includes('/protocol/openid-connect/token')) {
      return respond(200, {
        access_token: 'fixture-access', refresh_token: 'fixture-refresh-2',
        expires_in: 3600,
      });
    }
    if (path === '/api/app/session') {
      if (fixture.sessionStatus !== 200) return respond(fixture.sessionStatus, {});
      return respond(200, {
        available: 22344, credits_per_shot: 3.9,
        referral: { enabled: true }, reading: { enabled: false },
        notify: { available: false },
      });
    }
    if (path === '/api/app/targets') return respond(200, { targets: [] });
    if (path === '/api/app/referral') {
      fixture.referralCalls += 1;
      const gifts = fixture.referralCalls === 2
        ? [] : [{ id: 'gift-1', amount: 9999 }];
      return respond(200, {
        code: 'FIXT01', redeemed: false, window: { open: false },
        unseen_gifts: gifts,
      });
    }
    if (path === '/api/app/referral/gifts/seen') return respond(200, {});
    throw new Error('fixture: no network for ' + path);
  };
  showApp({ fetch: fetchStub });
  const restore = () => {
    for (const key of GLOBAL_KEYS) {
      if (saved[key] === undefined) delete globalThis[key];
      else globalThis[key] = saved[key];
    }
  };
  return { document, restore, fixture };
}

async function waitFor(predicate, label) {
  for (let i = 0; i < 80; i++) {
    if (predicate()) return;
    await new Promise((r) => setTimeout(r, 25));
  }
  assert.fail(`timed out waiting for ${label}`);
}

test('app-glass: gift overlay inerts the whole <main> background and is released on navigation and session loss (DOM stub)', async () => {
  const { document, restore, fixture } = await bootAppWithGift();
  try {
    const el = (id) => document.getElementById(id);
    const overlay = el('gift-overlay');
    const siblings = () => document.mainElement.children.filter((c) => c !== overlay);
    // Boot lands home and the unseen gift surfaces the modal:
    // EVERY sibling under <main> goes inert — the appbar and
    // the dock included, not just the stage's children (the
    // round-3 leak: setGiftBackgroundInert only reached the
    // overlay's .stage siblings while it lived in the stage).
    await waitFor(() => overlay.hidden === false, 'gift overlay to open at boot');
    assert.ok(siblings().every((c) => c.inert === true),
      'appbar, stage, gear sheet and dock all inert while the gift is open');
    assert.equal(el('gear-button').inert, true, 'appbar gear button inert');
    assert.equal(el('dock-home').inert, true, 'dock inert');
    assert.equal(overlay.inert, false, 'the overlay itself stays interactive');
    assert.equal(document.activeElement?.id, 'gift-close', 'focus lands in the dialog');
    // Escape closes it and lifts the inert everywhere.
    document.dispatch('keydown', { key: 'Escape' });
    assert.equal(overlay.hidden, true, 'Escape closes the gift overlay');
    assert.ok(siblings().every((c) => c.inert === false), 'inert lifted on Escape close');
    // Mutual exclusion: opening the gear sheet never revives
    // the gift, and the sheet still opens and closes cleanly.
    el('gear-button').click();
    assert.equal(el('gear-sheet').hidden, false, 'gear sheet opens after gift closed');
    assert.equal(overlay.hidden, true, 'gift stays closed while the sheet is open');
    document.dispatch('keydown', { key: 'Escape' });
    assert.equal(el('gear-sheet').hidden, true, 'sheet closes');
    // Navigation home re-runs the referral load (sequenced to
    // no gifts): the overlay stays closed and nothing is inert.
    el('dock-home').click();
    await waitFor(() => fixture.referralCalls === 2, 'referral reload after home navigation');
    await new Promise((r) => setTimeout(r, 50));
    assert.equal(overlay.hidden, true, 'no gift after navigation when none is unseen');
    assert.ok(siblings().every((c) => c.inert === false), 'nothing inert after navigation');
    // Over the account screen the next gift opens as a modal
    // again, with the same full-background inert.
    el('dock-account').click();
    await waitFor(() => overlay.hidden === false, 'gift overlay to reopen over account');
    assert.ok(siblings().every((c) => c.inert === true),
      'background inert again while the reopened gift is open');
    // Session loss (401) routes through failToLogin ->
    // navigate('login') -> show(), which releases the modal:
    // it can never be stranded over the login screen with the
    // login screen left inert.
    fixture.sessionStatus = 401;
    el('dock-home').click();
    await waitFor(() => el('screen-login').hidden === false, 'login screen after 401');
    assert.equal(overlay.hidden, true, 'gift overlay released on session loss');
    assert.ok(siblings().every((c) => c.inert === false), 'no inert stranded on the login screen');
  } finally { restore(); }
});

test('app-glass: re-entering showApp replaces the Escape listener (DOM stub)', async () => {
  const { document, restore } = await bootApp();
  try {
    assert.equal(document.listenerCount('keydown'), 1, 'one keydown listener after first boot');
    showApp({ fetch: async () => { throw new Error('fixture: no network'); } });
    await new Promise((r) => setTimeout(r, 30));
    assert.equal(document.listenerCount('keydown'), 1, 'keydown listener replaced, not stacked');
    // And the replacement handler still works.
    const el = (id) => document.getElementById(id);
    el('gear-button').click();
    assert.equal(el('gear-sheet').hidden, false, 'sheet opens after re-entry');
    document.dispatch('keydown', { key: 'Escape' });
    assert.equal(el('gear-sheet').hidden, true, 'Escape closes after re-entry');
  } finally { restore(); }
});
