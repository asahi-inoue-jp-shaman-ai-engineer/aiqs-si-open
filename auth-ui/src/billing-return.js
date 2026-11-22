// A redirect is navigation, not proof of payment or credit delivery.
export function billingReturn(search) {
  const values = new URLSearchParams(search).getAll('billing');
  if (values.length !== 1 || !['success', 'cancel'].includes(values[0])) return null;
  return values[0] === 'success' ? {
    title: '購入のお手続きから戻りました。',
    message: '支払い確認後にクレジットが反映されます。利用しているAIに戻り、購入状況と残高をご確認ください。',
    prompt: 'アイクスSIの今回の購入状況と、現在のクレジット残高を確認して。'
  } : {
    title: '購入のお手続きを閉じました。',
    message: '購入を続ける場合は、利用しているAIに戻って購入リンクを開いてください。すでにお支払いされた場合は、先に購入状況をご確認ください。',
    prompt: 'アイクスSIの今回の購入状況を確認して。'
  };
}
