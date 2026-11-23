// Copy-text builders for the standalone app (design §4). Pure
// functions of the public payload only: the ranking lines are built
// from the serializer's public fields (rank / field_name / text /
// hits); entry_id stays in the payload for ordering and never
// appears in the pasted text. The destination the user pastes into
// is their own Muse or their notes — the builders neither know
// nor restrict it.

function pad(value) {
  return String(value).padStart(2, '0');
}

export function formatTimestamp(date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function scanCopyText({ intention, selfLoveJoy, entries, at }) {
  const lines = [
    `意図：${intention}`,
    `自己愛アファーメーション：${selfLoveJoy}`,
    `日時：${formatTimestamp(at)}`,
    '',
    '共鳴ランキング（順位順）',
  ];
  const ordered = [...entries].sort((a, b) => (a.rank ?? 0) - (b.rank ?? 0));
  ordered.forEach((entry, index) => {
    lines.push(`${index + 1}. ${entry.field_name}｜${entry.text}（ヒット数 ${entry.hits}）`);
  });
  return lines.join('\n');
}

export function proofCopyText({ state, requestedCount, completedCount,
                                settledCredits, publicDeviceId, proofDigest, at }) {
  const lines = [
    'アイクスSI 稼働証明',
    `日時：${formatTimestamp(at)}`,
    `状態：${state}`,
  ];
  if (typeof requestedCount === 'number' || typeof completedCount === 'number') {
    // An unreported count stays "—" in the copy too: coercing a
    // missing completed_count to 0 would state an execution
    // count the server never reported (Astra #7).
    lines.push(`照射回数：${completedCount ?? '—'} / ${requestedCount ?? '—'} 回`);
  }
  if (typeof settledCredits === 'number') {
    lines.push(`精算クレジット：${settledCredits}`);
  }
  if (publicDeviceId) lines.push(`デバイス公開ID：${publicDeviceId}`);
  if (proofDigest) lines.push(`証明ダイジェスト：${proofDigest}`);
  return lines.join('\n');
}

// Display split for the summary-first reading format (2026-11-22):
// the stored text stays one string (copy and history carry it
// whole, summary first); the result screen renders the 【要約】
// block in its own box above the body. Readings stored before the
// format change have no summary and render whole, unsplit.
export function splitReadingText(text) {
  if (typeof text !== 'string') return { summary: null, body: '' };
  const trimmed = text.trim();
  if (!trimmed.startsWith('【要約】')) return { summary: null, body: text };
  const bodyStart = trimmed.indexOf('【キーワード群】');
  if (bodyStart < 0) return { summary: null, body: text };
  return {
    summary: trimmed.slice(0, bodyStart).trim(),
    body: trimmed.slice(bodyStart),
  };
}

export function readingCopyText({ intention, selfLoveJoy, text, at }) {
  const lines = [
    `意図：${intention}`,
    `自己愛アファーメーション：${selfLoveJoy}`,
    `日時：${formatTimestamp(at)}`,
    '',
    'AIによるリーディング',
    '',
    text,
  ];
  return lines.join('\n');
}

// Net credits actually charged, from the proof bundle's settlement
// ledger (reserve/settle/release rows): the negated sum of amounts.
export function settledCreditsOf(settlement) {
  if (!Array.isArray(settlement)) return null;
  let total = 0;
  for (const row of settlement) {
    if (typeof row?.amount !== 'number') return null;
    total += row.amount;
  }
  return total === 0 ? 0 : -total;
}
