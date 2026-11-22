// Only server-rendered data attributes are inputs. No URL, storage or environment
// lookup occurs here. This validates public settings; it does not authorize a host.
const invalid = () => new Error('Authentication configuration unavailable');

function canonicalOrigin(value) {
  if (typeof value !== 'string' || value.length > 2048 ||
      /[^\x21-\x7e]|[%\\?#]/.test(value)) throw invalid();
  let url;
  try { url = new URL(value); } catch { throw invalid(); }
  if (url.protocol !== 'https:' || url.username || url.password || value !== url.origin || url.port === '0') throw invalid();
  if (!url.hostname.startsWith('[')) {
    if (url.hostname.length > 253 || url.hostname.split('.').some(label =>
      label.startsWith('xn--') || !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))) throw invalid();
  }
  return value;
}

export function parseEntryRuntimeConfig(attributes, pageOrigin) {
  if (!attributes || typeof attributes !== 'object') throw invalid();
  const { runtimeMode, providerBase, publishableKey, authorityOrigin, cspConnectOrigin } = attributes;
  if (runtimeMode === undefined) {
    if (providerBase !== 'https://localhost:9420' || publishableKey !== 'sb_publishable_si_local_synthetic' ||
        authorityOrigin !== undefined || cspConnectOrigin !== undefined) throw invalid();
  } else if (runtimeMode === 'configured') {
    canonicalOrigin(providerBase);
    canonicalOrigin(authorityOrigin);
    canonicalOrigin(cspConnectOrigin);
    canonicalOrigin(pageOrigin);
    if (authorityOrigin !== pageOrigin || cspConnectOrigin !== providerBase) throw invalid();
    if (typeof publishableKey !== 'string' || !/^sb_publishable_[A-Za-z0-9_-]{1,512}$/.test(publishableKey)) throw invalid();
  } else {
    throw invalid();
  }
  return Object.freeze({ providerBase, publishableKey });
}
