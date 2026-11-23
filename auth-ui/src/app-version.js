// New-build detection for the standalone app.
//
// There is no service worker and the server answers every UI
// request with Cache-Control: no-store, and the JS bundle ships
// under a content-hashed file name — so a full reload always
// lands on the currently served build. But the SPA never
// reloads itself: in-app navigation re-fetches nothing, and a
// tab (or installed PWA) left open keeps running the old JS
// indefinitely. The 2026-11-23 live re-check hit exactly that:
// the open tab still ran the previous build while the server
// already served a different one.
//
// The build version is the bundle's content hash, which Vite
// embeds into the build output at build time (the file name
// assets/index-<hash>.js, referenced from index.html). The
// running app reads its own version back from the script URL it
// was loaded with, and reads the server's current version by
// fetching the app page itself and extracting the same hash
// from the returned HTML: the entry route serves index.html
// with no-store, and that HTML is by definition what a reload
// would run. A separate version file is not an option — the
// server's static route only serves a fixed extension list
// (no .json), and adding a server endpoint is outside the
// auth-ui change boundary. Fetching the page HTML keeps the
// whole mechanism inside auth-ui.
//
// A mismatch only ever SHOWS a banner. Nothing reloads on its
// own: an automatic reload could destroy an in-progress
// irradiation, a polling session, or half-typed input. The
// user reloads by tapping the banner's button, when it suits
// them. A failed check (offline, non-HTML answer, unparseable
// page) is silently ignored — version detection must never get
// in the way of using the app.
//
// The hash comparison carries no ordering: a mismatch can mean
// a newer deploy OR a rollback to an older one. The banner
// copy is therefore direction-neutral ("an update is
// available"), and a later check that confirms the server is
// back on the running build withdraws the banner again
// (onUpdateResolved). Astra review 2026-11-23 hardening:
// anchored URL-path extraction (no substring matches in
// comments, inline scripts, query strings, or error bodies),
// a bounded timeout plus generation guard so a stalled
// response cannot wedge every later check, a dismissed-version
// Set, clock-rewind tolerance, and one trailing check after a
// throttled trigger so a suppressed moment cannot leave a
// different build undetected indefinitely.

export const UPDATE_AVAILABLE_TEXT = 'アプリの更新があります';
export const UPDATE_DETAIL_TEXT = '更新すると、いま提供されている画面で利用できます。いまの操作は、更新するまでそのまま続けられます。';
export const UPDATE_RELOAD_TEXT = '更新する';
export const UPDATE_DISMISS_TEXT = 'あとで';

// The entry bundle's file name, anchored to the whole URL
// path: /assets/index-<hash>.js and nothing else. A query
// string, a .map suffix, or a longer path never matches.
const ENTRY_PATH_PATTERN = /^\/assets\/index-([A-Za-z0-9_-]+)\.js$/;

// Extract the content hash from one script URL. The URL is
// parsed as a URL — scheme/authority, query, and fragment are
// stripped first — so text that merely CONTAINS a bundle path
// (a query parameter, an error message) never counts.
export function versionFromScriptUrl(url) {
  if (typeof url !== 'string') return null;
  let path = url.trim();
  if (!path) return null;
  path = path.split('#')[0].split('?')[0];
  const scheme = path.match(/^[A-Za-z][A-Za-z0-9+.-]*:\/\//);
  if (scheme) {
    const rest = path.slice(scheme[0].length);
    const slash = rest.indexOf('/');
    if (slash < 0) return null;
    path = rest.slice(slash);
  }
  const match = path.match(ENTRY_PATH_PATTERN);
  return match ? match[1] : null;
}

// Candidate entry scripts of an HTML document, in document
// order, each tagged with the signals that identify the real
// entry: the explicit data-app-entry marker (placed on the
// entry tag in index.html and preserved by the build) and
// type="module" (what Vite emits for the entry; legacy
// nomodule scripts are not the entry).
function entryScriptCandidates(html) {
  const withoutComments = html.replace(/<!--[\s\S]*?-->/g, '');
  const tags = withoutComments.match(/<script\b[^>]*>/gi) || [];
  const candidates = [];
  for (const tag of tags) {
    const src = tag.match(/[\s]src\s*=\s*"([^"]*)"/i)
      || tag.match(/[\s]src\s*=\s*'([^']*)'/i);
    if (!src) continue;
    candidates.push({
      url: src[1],
      entry: /[\s]data-app-entry(\s|=|>)/i.test(tag),
      module: /[\s]type\s*=\s*"module"/i.test(tag)
        || /[\s]type\s*=\s*'module'/i.test(tag),
    });
  }
  return candidates;
}

// Extract the main bundle's content hash from a page HTML
// string or from a single script URL. Only an actual <script>
// element's src identifies the entry — comments, inline script
// bodies, and non-HTML content are never read as versions.
// Returns null when the text carries no hashed entry bundle
// (dev server, error page, unexpected markup): callers treat
// null as "unknown", never as "different".
export function extractBuildVersion(text) {
  if (typeof text !== 'string') return null;
  if (!text.includes('<')) return versionFromScriptUrl(text);
  const candidates = entryScriptCandidates(text);
  for (const tier of ['entry', 'module', 'any']) {
    for (const candidate of candidates) {
      if (tier === 'entry' && !candidate.entry) continue;
      if (tier === 'module' && !candidate.module) continue;
      const version = versionFromScriptUrl(candidate.url);
      if (version) return version;
    }
  }
  return null;
}

// The running build's version, from the URLs of the scripts the
// current document loaded. Only the entry bundle identifies the
// build; other assets are ignored by the anchored pattern.
// Callers pass the URLs entry-first when they know the order.
export function ownBuildVersion(scriptUrls) {
  if (!Array.isArray(scriptUrls)) return null;
  for (const url of scriptUrls) {
    const version = versionFromScriptUrl(url);
    if (version) return version;
  }
  return null;
}

// Version monitor: throttled checks, mismatch notification,
// banner withdrawal on a confirmed match, and per-version
// dismissal. All side effects are injected so the lifecycle is
// testable without a DOM or a network.
//
// - fetchServerVersion resolves to the server's current build
//   version (or null when it cannot be determined); a rejection
//   is treated the same as null. A fetch that never settles is
//   abandoned after timeoutMs and its late result discarded —
//   one stalled response must not disable every later check.
// - onUpdateAvailable(serverVersion) fires once per newly seen
//   server version while it differs from the running build.
//   Dismissing the banner records that version in a Set; the
//   same version never notifies again during this tab's life,
//   but a still different one does.
// - onUpdateResolved() fires when a check CONFIRMS the server
//   is serving the running build again after an announcement
//   (e.g. a rollback): the banner is withdrawn. Unknown or
//   failed responses never withdraw it.
// - A trigger suppressed by the throttle schedules exactly one
//   trailing check for when the interval elapses, so detection
//   delay stays bounded even if no further trigger arrives.
// - Overlapping checks are suppressed while a fetch is in
//   flight (they do not share its promise; callers use void).
export function createVersionMonitor({
  ownVersion,
  fetchServerVersion,
  onUpdateAvailable,
  onUpdateResolved,
  minIntervalMs = 60_000,
  timeoutMs = 15_000,
  now = () => Date.now(),
  setTimeoutFn = (fn, ms) => setTimeout(fn, ms),
  clearTimeoutFn = (id) => clearTimeout(id),
} = {}) {
  let lastCheckAt = null;
  let notifiedVersion = null;
  const dismissedVersions = new Set();
  let inFlight = false;
  let generation = 0;
  let trailingTimer = null;

  // A pending timer must never keep a Node test process alive;
  // browsers return a number without unref, which is fine.
  function unref(timer) {
    if (timer && typeof timer.unref === 'function') timer.unref();
  }

  function clearTrailing() {
    if (trailingTimer !== null) {
      clearTimeoutFn(trailingTimer);
      trailingTimer = null;
    }
  }

  function scheduleTrailing(remainingMs) {
    if (trailingTimer !== null) return;
    trailingTimer = setTimeoutFn(() => {
      trailingTimer = null;
      void check();
    }, remainingMs);
    unref(trailingTimer);
  }

  async function check() {
    if (!ownVersion || typeof fetchServerVersion !== 'function') return;
    if (inFlight) return;
    const at = now();
    if (lastCheckAt !== null) {
      const elapsed = at - lastCheckAt;
      // A wall clock that moved backwards (elapsed < 0) is a
      // reset, not a reason to stay silent until it catches up.
      if (elapsed >= 0 && elapsed < minIntervalMs) {
        scheduleTrailing(minIntervalMs - elapsed);
        return;
      }
    }
    lastCheckAt = at;
    clearTrailing();
    const gen = ++generation;
    inFlight = true;
    let timedOut = false;
    let timer = null;
    let serverVersion = null;
    try {
      serverVersion = await Promise.race([
        Promise.resolve().then(() => fetchServerVersion()).catch(() => null),
        new Promise((resolve) => {
          timer = setTimeoutFn(() => { timedOut = true; resolve(null); }, timeoutMs);
          unref(timer);
        }),
      ]);
    } finally {
      if (timer !== null) clearTimeoutFn(timer);
      if (gen === generation) inFlight = false;
    }
    // A timed-out (abandoned) fetch may still settle later; the
    // race has already discarded its result. A superseded check
    // likewise applies nothing.
    if (timedOut || gen !== generation) return;
    if (serverVersion === ownVersion) {
      // Confirmed match: withdraw an active announcement (the
      // server rolled back to the running build). Dismissal
      // history is kept — it is about versions, not about the
      // currently displayed banner.
      if (notifiedVersion !== null) {
        notifiedVersion = null;
        if (typeof onUpdateResolved === 'function') onUpdateResolved();
      }
      return;
    }
    if (!serverVersion) return;
    if (dismissedVersions.has(serverVersion)) return;
    if (serverVersion === notifiedVersion) return;
    notifiedVersion = serverVersion;
    if (typeof onUpdateAvailable === 'function') onUpdateAvailable(serverVersion);
  }

  function dismiss() {
    if (notifiedVersion) dismissedVersions.add(notifiedVersion);
  }

  return { check, dismiss };
}
