import { showHome } from './home.js';
import './style.css';
import { billingReturn } from './billing-return.js';
import { showAccountLink } from './account-link.js';
import { showAccountPurchase } from './account-purchase.js';
import { showApp } from './app.js';

const status = (text) => { document.getElementById('status').textContent = text; };
if (location.pathname === '/app') {
  showApp();
} else if (location.pathname === '/account/link' || location.pathname === '/account') {
  showAccountLink();
} else if (location.pathname === '/account/purchase') {
  showAccountPurchase();
} else if (location.pathname === '/' && !location.search && !location.hash) {
  showHome();
} else {
  const purchaseReturn = billingReturn(location.search);
  if (purchaseReturn) {
    document.title = 'Aiqs SI — クレジット購入';
    document.querySelector('.eyebrow').textContent = 'クレジット購入';
    document.querySelector('h1').textContent = purchaseReturn.title;
    document.querySelector('.lead').textContent = purchaseReturn.message;
    status('AIへのメッセージ例：' + purchaseReturn.prompt);
    document.querySelector('aside').hidden = true;
    document.querySelector('footer').textContent = 'この画面から照射は始まりません。';
  } else {
    // Authority-owned authorization starts in the AI, never via old provider codes.
    history.replaceState(null, '', '/oauth/consent');
    status('利用するAIに戻り、アイクスSIへの接続を始めてください。');
  }
}
