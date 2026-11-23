import test from 'node:test';
import assert from 'node:assert/strict';
import {
  UPDATE_AVAILABLE_TEXT, UPDATE_DETAIL_TEXT, UPDATE_DISMISS_TEXT,
  UPDATE_RELOAD_TEXT, createVersionMonitor, extractBuildVersion,
  ownBuildVersion,
} from '../src/app-version.js';

// Field finding (2026-11-23): with no service worker and an SPA
// that never reloads itself, an open tab kept running the old
// build while the server already served the new one. These tests
// pin the detection contract: the version is the entry bundle's
// content hash, a mismatch notifies (once per version), and any
// failure to determine a version is silently "unknown".
//
// Astra review (2026-11-23) hardening pinned here: extraction is
// anchored URL parsing over real <script> elements (decoys in
// comments, inline scripts, query strings, and error bodies do
// not count); a stalled fetch is abandoned after a timeout and
// its late result discarded; a confirmed match withdraws the
// announcement (rollback) while unknown responses never do;
// dismissed versions are remembered as a Set (B→C→B stays
// silent); a backwards clock jump does not suppress checks; a
// throttled trigger schedules exactly one trailing check. The
// banner copy is direction-neutral because a hash comparison
// cannot tell a newer deploy from a rollback.

const ENTRY_HTML = '<!doctype html><html lang="ja"><head>'
  + '<script type="module" crossorigin src="/assets/index-DmnR-7q7.js"></script>'
  + '<link rel="stylesheet" crossorigin href="/assets/index-BxJsFudp.css">'
  + '</head><body></body></html>';

const flush = async () => {
  for (let i = 0; i < 20; i += 1) await Promise.resolve();
};

test('extracts the entry bundle hash from page HTML', () => {
  assert.equal(extractBuildVersion(ENTRY_HTML), 'DmnR-7q7');
});

test('extraction ignores stylesheets and unhashed scripts', () => {
  assert.equal(extractBuildVersion('<link href="/assets/index-BxJsFudp.css">'), null);
  assert.equal(extractBuildVersion('<script src="/src/main.js"></script>'), null);
  assert.equal(extractBuildVersion('<script src="/assets/other-Ab12Cd34.js"></script>'), null);
});

test('extraction returns null for non-strings and empty input', () => {
  assert.equal(extractBuildVersion(null), null);
  assert.equal(extractBuildVersion(undefined), null);
  assert.equal(extractBuildVersion(''), null);
  assert.equal(extractBuildVersion(42), null);
});

test('extraction ignores decoy bundle paths in comments and inline scripts', () => {
  const commented = '<!-- <script type="module" src="/assets/index-OLD11111.js"></script> -->'
    + ENTRY_HTML;
  assert.equal(extractBuildVersion(commented), 'DmnR-7q7');
  const inline = '<script>var u = "/assets/index-FAKE2222.js";</script>' + ENTRY_HTML;
  assert.equal(extractBuildVersion(inline), 'DmnR-7q7');
  assert.equal(extractBuildVersion('{"error":"missing /assets/index-FAKE2222.js"}'), null);
});

test('extraction prefers the module entry over an earlier nomodule script', () => {
  const legacyFirst = '<script nomodule src="/assets/index-LEGACY11.js"></script>'
    + ENTRY_HTML;
  assert.equal(extractBuildVersion(legacyFirst), 'DmnR-7q7');
});

test('extraction prefers the explicit entry marker over other module scripts', () => {
  const html = '<script type="module" src="/assets/index-OTHER9999.js"></script>'
    + '<script type="module" data-app-entry src="/assets/index-REAL11111.js"></script>';
  assert.equal(extractBuildVersion(html), 'REAL11111');
});

test('extraction parses URLs instead of matching substrings', () => {
  assert.equal(extractBuildVersion('/loader.js?next=/assets/index-FAKE2222.js'), null);
  assert.equal(extractBuildVersion('/assets/index-FAKE2222.js.map'), null);
  assert.equal(extractBuildVersion('https://asi.iboga.jp/assets/index-Ab12Cd34.js?v=1#x'), 'Ab12Cd34');
  assert.equal(
    extractBuildVersion('<script type="module" src="/assets/index-Ab12Cd34.js?x=1"></script>'),
    'Ab12Cd34');
});

test('own version comes from the loaded entry script URL', () => {
  assert.equal(ownBuildVersion([
    'https://asi.iboga.jp/assets/index-DmnR-7q7.js',
  ]), 'DmnR-7q7');
  assert.equal(ownBuildVersion([
    'https://asi.iboga.jp/assets/favicon-32.png',
    '/assets/index-Zz9Yy8Xx.js',
  ]), 'Zz9Yy8Xx');
  assert.equal(ownBuildVersion([]), null);
  assert.equal(ownBuildVersion(null), null);
});

test('banner copy is direction-neutral and names the user-initiated reload', () => {
  assert.equal(UPDATE_AVAILABLE_TEXT, 'アプリの更新があります');
  assert.match(UPDATE_DETAIL_TEXT, /更新するまでそのまま続けられます/);
  // A hash mismatch can be a rollback, so the copy must not
  // claim the served build is "new" or "the latest".
  assert.doesNotMatch(UPDATE_AVAILABLE_TEXT + UPDATE_DETAIL_TEXT, /新しい|最新/);
  assert.equal(UPDATE_RELOAD_TEXT, '更新する');
  assert.equal(UPDATE_DISMISS_TEXT, 'あとで');
});

function fakeTimers() {
  let nextId = 1;
  const pending = new Map();
  return {
    setTimeoutFn: (fn, ms) => {
      const id = nextId;
      nextId += 1;
      pending.set(id, { fn, ms });
      return id;
    },
    clearTimeoutFn: (id) => { pending.delete(id); },
    delays: () => [...pending.values()].map((t) => t.ms),
    fireAll: () => {
      const timers = [...pending.values()];
      pending.clear();
      for (const t of timers) t.fn();
    },
  };
}

function monitorHarness({
  own = 'aaa111', server = 'bbb222', interval = 60_000,
  timeoutMs, fakeTimers: useFakeTimers = false,
} = {}) {
  let clock = 1_000_000;
  let fetchCount = 0;
  let serverVersion = server;
  let fetchImpl = async () => serverVersion;
  const notified = [];
  const resolved = [];
  const timers = useFakeTimers ? fakeTimers() : null;
  const monitor = createVersionMonitor({
    ownVersion: own,
    fetchServerVersion: () => { fetchCount += 1; return fetchImpl(); },
    onUpdateAvailable: (version) => notified.push(version),
    onUpdateResolved: () => resolved.push(true),
    minIntervalMs: interval,
    ...(timeoutMs !== undefined ? { timeoutMs } : {}),
    ...(timers
      ? { setTimeoutFn: timers.setTimeoutFn, clearTimeoutFn: timers.clearTimeoutFn }
      : {}),
    now: () => clock,
  });
  return {
    monitor, notified,
    resolvedCount: () => resolved.length,
    advance: (ms) => { clock += ms; },
    fetches: () => fetchCount,
    setServer: (version) => { serverVersion = version; },
    setFetch: (impl) => { fetchImpl = impl; },
    timerDelays: () => (timers ? timers.delays() : []),
    fireTimers: () => { if (timers) timers.fireAll(); },
  };
}

test('matching versions never notify', async () => {
  const h = monitorHarness({ server: 'aaa111' });
  await h.monitor.check();
  assert.deepEqual(h.notified, []);
  assert.equal(h.fetches(), 1);
});

test('a different server version notifies once with that version', async () => {
  const h = monitorHarness();
  await h.monitor.check();
  h.advance(120_000);
  await h.monitor.check();
  assert.deepEqual(h.notified, ['bbb222']);
  assert.equal(h.fetches(), 2);
});

test('checks are throttled to the minimum interval', async () => {
  const h = monitorHarness();
  await h.monitor.check();
  await h.monitor.check();
  h.advance(59_999);
  await h.monitor.check();
  assert.equal(h.fetches(), 1);
  h.advance(1);
  await h.monitor.check();
  assert.equal(h.fetches(), 2);
});

test('a throttled trigger schedules exactly one trailing check', async () => {
  const h = monitorHarness({ fakeTimers: true });
  await h.monitor.check();
  assert.equal(h.fetches(), 1);
  h.advance(30_000);
  await h.monitor.check();
  assert.equal(h.fetches(), 1);
  assert.deepEqual(h.timerDelays(), [30_000]);
  await h.monitor.check();
  assert.deepEqual(h.timerDelays(), [30_000]);
  h.advance(30_000);
  h.fireTimers();
  await flush();
  assert.equal(h.fetches(), 2);
  assert.deepEqual(h.notified, ['bbb222']);
});

test('a backwards clock jump does not suppress the next check', async () => {
  const h = monitorHarness({ server: 'aaa111' });
  await h.monitor.check();
  assert.equal(h.fetches(), 1);
  h.advance(-3_600_000);
  await h.monitor.check();
  assert.equal(h.fetches(), 2);
});

test('a failed fetch is ignored and does not notify', async () => {
  const h = monitorHarness();
  h.setFetch(async () => { throw new Error('offline'); });
  await h.monitor.check();
  assert.deepEqual(h.notified, []);
});

test('checks recover after a rejected fetch', async () => {
  const h = monitorHarness();
  h.setFetch(async () => { throw new Error('offline'); });
  await h.monitor.check();
  assert.deepEqual(h.notified, []);
  h.setFetch(async () => 'bbb222');
  h.advance(60_000);
  await h.monitor.check();
  assert.deepEqual(h.notified, ['bbb222']);
  assert.equal(h.fetches(), 2);
});

test('a stalled fetch times out and the next check can run again', async () => {
  const h = monitorHarness({ timeoutMs: 30 });
  h.setFetch(() => new Promise(() => {}));
  await h.monitor.check();
  assert.equal(h.fetches(), 1);
  assert.deepEqual(h.notified, []);
  h.setFetch(async () => 'bbb222');
  h.advance(60_000);
  await h.monitor.check();
  assert.deepEqual(h.notified, ['bbb222']);
  assert.equal(h.fetches(), 2);
});

test('the late result of a timed-out fetch is discarded', async () => {
  const h = monitorHarness({ timeoutMs: 30 });
  let releaseStale;
  h.setFetch(() => new Promise((resolve) => { releaseStale = resolve; }));
  await h.monitor.check();
  assert.deepEqual(h.notified, []);
  h.setFetch(async () => 'ccc333');
  h.advance(60_000);
  await h.monitor.check();
  assert.deepEqual(h.notified, ['ccc333']);
  releaseStale('bbb222');
  await flush();
  assert.deepEqual(h.notified, ['ccc333']);
});

test('an undeterminable server version is ignored', async () => {
  const h = monitorHarness({ server: null });
  await h.monitor.check();
  assert.deepEqual(h.notified, []);
});

test('an unknown own version disables checking entirely', async () => {
  const h = monitorHarness({ own: null });
  await h.monitor.check();
  assert.equal(h.fetches(), 0);
  assert.deepEqual(h.notified, []);
});

test('dismissed versions stay silent when they return (B, C, then B again)', async () => {
  const h = monitorHarness();
  await h.monitor.check();
  assert.deepEqual(h.notified, ['bbb222']);
  h.monitor.dismiss();
  h.setServer('ccc333');
  h.advance(120_000);
  await h.monitor.check();
  assert.deepEqual(h.notified, ['bbb222', 'ccc333']);
  h.monitor.dismiss();
  h.setServer('bbb222');
  h.advance(120_000);
  await h.monitor.check();
  assert.deepEqual(h.notified, ['bbb222', 'ccc333']);
});

test('dismissing does not silence a different version', async () => {
  const h = monitorHarness();
  await h.monitor.check();
  h.monitor.dismiss();
  h.setServer('ccc333');
  h.advance(120_000);
  await h.monitor.check();
  assert.deepEqual(h.notified, ['bbb222', 'ccc333']);
});

test('a confirmed match withdraws the announcement and re-arms it', async () => {
  const h = monitorHarness();
  await h.monitor.check();
  assert.deepEqual(h.notified, ['bbb222']);
  h.setServer('aaa111');
  h.advance(60_000);
  await h.monitor.check();
  assert.equal(h.resolvedCount(), 1);
  h.setServer('bbb222');
  h.advance(60_000);
  await h.monitor.check();
  assert.deepEqual(h.notified, ['bbb222', 'bbb222']);
});

test('an unknown response never withdraws the announcement', async () => {
  const h = monitorHarness();
  await h.monitor.check();
  assert.deepEqual(h.notified, ['bbb222']);
  h.setServer(null);
  h.advance(60_000);
  await h.monitor.check();
  assert.equal(h.resolvedCount(), 0);
  h.setServer('aaa111');
  h.advance(60_000);
  await h.monitor.check();
  assert.equal(h.resolvedCount(), 1);
});

test('dismissal history survives a rollback withdrawal', async () => {
  const h = monitorHarness();
  await h.monitor.check();
  h.monitor.dismiss();
  h.setServer('aaa111');
  h.advance(60_000);
  await h.monitor.check();
  assert.equal(h.resolvedCount(), 1);
  h.setServer('bbb222');
  h.advance(60_000);
  await h.monitor.check();
  assert.deepEqual(h.notified, ['bbb222']);
});

test('overlapping checks are suppressed while a fetch is in flight', async () => {
  const h = monitorHarness();
  let release;
  h.setFetch(() => new Promise((resolve) => { release = resolve; }));
  const first = h.monitor.check();
  h.advance(120_000);
  const second = h.monitor.check();
  await flush();
  assert.equal(h.fetches(), 1);
  release('bbb222');
  await Promise.all([first, second]);
  assert.deepEqual(h.notified, ['bbb222']);
});
