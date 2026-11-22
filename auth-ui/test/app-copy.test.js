import test from 'node:test';
import assert from 'node:assert/strict';
import { formatTimestamp, proofCopyText, readingCopyText, scanCopyText, settledCreditsOf, splitReadingText } from '../src/app-copy.js';

const ENTRIES = [
  { entry_id: 'e2', field_name: '分野B', text: '本文その2', hits: 121, rank: 2 },
  { entry_id: 'e1', field_name: '分野A', text: '本文その1', hits: 128, rank: 1 },
  { entry_id: 'e3', field_name: '分野C', text: '本文その3', hits: 117, rank: 3 },
];

test('scan copy text follows the decided format (design §4.2)', () => {
  const text = scanCopyText({
    intention: 'みゅんが監修した漫画が、めちゃくちゃおもしろくなる',
    selfLoveJoy: 'わたしは わたしのままで 愛されている',
    entries: ENTRIES,
    at: new Date(2026, 10, 22, 5, 40),
  });
  assert.equal(text, [
    '意図：みゅんが監修した漫画が、めちゃくちゃおもしろくなる',
    '自己愛アファーメーション：わたしは わたしのままで 愛されている',
    '日時：2026-11-22 05:40',
    '',
    '共鳴ランキング（ヒット数の多い順）',
    '1. 分野A｜本文その1（ヒット数 128）',
    '2. 分野B｜本文その2（ヒット数 121）',
    '3. 分野C｜本文その3（ヒット数 117）',
  ].join('\n'));
  // entry_id is an ordering identifier only and never pasted.
  assert.ok(!text.includes('e1'));
});

test('proof copy text carries only the public proof fields', () => {
  const text = proofCopyText({
    state: '完了', requestedCount: 9, completedCount: 9, settledCredits: 351,
    publicDeviceId: 'device-public-1', proofDigest: 'a'.repeat(64),
    at: new Date(2026, 10, 22, 6, 0),
  });
  assert.ok(text.includes('状態：完了'));
  assert.ok(text.includes('照射回数：9 / 9 回'));
  assert.ok(text.includes('精算クレジット：351'));
  assert.ok(text.includes('デバイス公開ID：device-public-1'));
  assert.ok(text.includes('証明ダイジェスト：' + 'a'.repeat(64)));
});

test('settled credits are the negated settlement sum', () => {
  assert.equal(settledCreditsOf([
    { kind: 'reserve', amount: -351 }, { kind: 'settle', amount: 0 },
  ]), 351);
  assert.equal(settledCreditsOf([
    { kind: 'reserve', amount: -390 }, { kind: 'release', amount: 312 },
  ]), 78);
  assert.equal(settledCreditsOf([{ kind: 'reserve', amount: 0 }]), 0);
  assert.equal(settledCreditsOf(null), null);
  assert.equal(settledCreditsOf([{ kind: 'reserve' }]), null);
});

test('timestamps are local and zero-padded', () => {
  assert.equal(formatTimestamp(new Date(2026, 0, 5, 9, 7)), '2026-01-05 09:07');
});

test('reading text splits into summary box and body (2026-11-22 format)', () => {
  const text = [
    '【要約】',
    '要点の一行目です。',
    '要点の二行目です。',
    '',
    '【キーワード群】',
    '本文の段落です。',
    '',
    '次の段落です。',
  ].join('\n');
  const parts = splitReadingText(text);
  assert.equal(parts.summary, '【要約】\n要点の一行目です。\n要点の二行目です。');
  assert.equal(parts.body, '【キーワード群】\n本文の段落です。\n\n次の段落です。');
  // The split is display-only: summary + body rejoins to the whole.
  assert.equal(parts.summary + '\n\n' + parts.body, text);
});

test('reading text without a summary renders whole (pre-format readings)', () => {
  const legacy = '【キーワード群】\n読み解き本文';
  assert.deepEqual(splitReadingText(legacy), { summary: null, body: legacy });
  assert.deepEqual(splitReadingText(null), { summary: null, body: '' });
  assert.deepEqual(splitReadingText('【要約】だけの文'),
    { summary: null, body: '【要約】だけの文' });
});

test('reading copy text carries the AI label and the §4.2 header', () => {
  const text = readingCopyText({
    intention: '意図の文', selfLoveJoy: '自分を大切にする',
    text: '【キーワード群】\n読み解き本文',
    at: new Date(2026, 10, 22, 7, 30),
  });
  assert.equal(text, [
    '意図：意図の文',
    '自己愛アファーメーション：自分を大切にする',
    '日時：2026-11-22 07:30',
    '',
    'AIによるリーディング',
    '',
    '【キーワード群】\n読み解き本文',
  ].join('\n'));
});
