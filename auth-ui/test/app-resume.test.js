import test from 'node:test';
import assert from 'node:assert/strict';
import {
  RESUME_KEY, parseResumeToken, resumeFragmentOwned, resumeLandingFor,
  saveResumeToken, takeResumeToken,
} from '../src/app-resume.js';

// The server's token shape is 16..256 characters (app_api.py
// jobs_resume); the fixture stays inside it. The client only
// requires a link-shaped value (see app-resume.js) and lets
// the server judge validity.
const TOKEN = 'resume-token-0123456789abcdef';

function memoryStorage() {
  const map = new Map();
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => { map.set(key, String(value)); },
    removeItem: (key) => { map.delete(key); },
  };
}

test('parseResumeToken reads exactly one well-shaped resume parameter', () => {
  assert.equal(parseResumeToken(`#resume=${TOKEN}`), TOKEN);
  assert.equal(parseResumeToken(`#foo=bar&resume=${TOKEN}`), TOKEN);
  assert.equal(parseResumeToken(''), null);
  assert.equal(parseResumeToken('#'), null);
  assert.equal(parseResumeToken('#access_token=abc&state=x'), null);
  assert.equal(parseResumeToken('resume=' + TOKEN), null);
  assert.equal(parseResumeToken(null), null);
  assert.equal(parseResumeToken(undefined), null);
  // A truncated token is still a link value: it is redeemed
  // and the SERVER refuses it (landing on the error screen).
  assert.equal(parseResumeToken('#resume=short'), 'short');
  // Beyond any link shape at all: not a token.
  assert.equal(parseResumeToken(`#resume=${'x'.repeat(1025)}`), null);
  assert.equal(parseResumeToken('#resume='), null);
  // Duplicated parameters are ambiguous, not a token.
  assert.equal(parseResumeToken(`#resume=${TOKEN}&resume=${TOKEN}`), null);
});

test('resumeFragmentOwned: a resume fragment is ours only without an access_token', () => {
  assert.equal(resumeFragmentOwned(`#resume=${TOKEN}`), true);
  // The state-bound handoff owns (and strips) this one itself.
  assert.equal(resumeFragmentOwned(`#access_token=abc&resume=${TOKEN}`), false);
  assert.equal(resumeFragmentOwned('#access_token=abc&state=x'), false);
  assert.equal(resumeFragmentOwned(''), false);
  assert.equal(resumeFragmentOwned(null), false);
});

test('the stash saves once, takes once, and rejects malformed values', () => {
  const storage = memoryStorage();
  assert.equal(saveResumeToken(storage, TOKEN), true);
  assert.equal(storage.getItem(RESUME_KEY), TOKEN);
  assert.equal(takeResumeToken(storage), TOKEN);
  // Consumed: a second take finds nothing.
  assert.equal(takeResumeToken(storage), null);
  // A truncated value still stashes (the server judges it at
  // redemption); empty and oversized values never do.
  assert.equal(saveResumeToken(storage, 'short'), true);
  assert.equal(takeResumeToken(storage), 'short');
  assert.equal(saveResumeToken(storage, ''), false);
  assert.equal(saveResumeToken(storage, 'x'.repeat(1025)), false);
  storage.setItem(RESUME_KEY, '');
  assert.equal(takeResumeToken(storage), null);
  assert.equal(takeResumeToken(null), null);
  assert.equal(saveResumeToken(null, TOKEN), false);
});

test('a throwing storage degrades to no resume pending', () => {
  const broken = {
    getItem() { throw new Error('denied'); },
    setItem() { throw new Error('denied'); },
    removeItem() { throw new Error('denied'); },
  };
  assert.equal(saveResumeToken(broken, TOKEN), false);
  assert.equal(takeResumeToken(broken), null);
});

test('resumeLandingFor follows the job view state for each operation', () => {
  // Scan: completed opens the ranking; anything else opens
  // the wait screen, whose polling surfaces the real state.
  assert.equal(resumeLandingFor({ operation: 'scan', state: 'completed' }), 'ranking');
  for (const state of ['queued', 'running', 'unknown', 'failed']) {
    assert.equal(resumeLandingFor({ operation: 'scan', state }), 'scan-wait', state);
  }
  // Irradiation: in flight opens progress; terminal opens proof.
  for (const state of ['queued', 'running', 'unknown']) {
    assert.equal(resumeLandingFor({ operation: 'irradiation', state }), 'progress', state);
  }
  for (const state of ['completed', 'failed', 'cancelled']) {
    assert.equal(resumeLandingFor({ operation: 'irradiation', state }), 'proof', state);
  }
  // An operation the client does not know earns no landing.
  assert.equal(resumeLandingFor({ operation: 'reading', state: 'completed' }), null);
  assert.equal(resumeLandingFor({}), null);
  assert.equal(resumeLandingFor(), null);
});
