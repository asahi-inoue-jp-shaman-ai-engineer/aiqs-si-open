# SI home and authentication handoff

Local candidate, not deployed. The static build preserves the home and billing-return pages. `/oauth/consent` now explains how to reconnect through the user's AI; it does not consume provider codes, credentials, sessions, or arbitrary redirect parameters.

Run `npm ci`, `npm test`, `npm run build`. No browser provider keys or authentication environment variables are required. The pinned Supabase SDK is a **development-only dependency** for historical contract tests in `test/pkce.test.js`; `src/flow.js` and its tests are historical fixtures, not imported by the current application or shipped bundle. Those tests do not prove the current authority flow.

The AI uses SI discovery and the maintained authorization server's standard authorization endpoint with its own state, PKCE and resource. Keycloak owns login/consent; its fixed upstream provider verifies identity. This page never grants credits. Trusted internal admission and reconciled migration evidence determine new-user eligibility. Existing balances are preserved; first SI login alone is not evidence of newness.

## Remaining release proof

- Japanese maintained login/consent, grant/deny, expiry/replay and recovery in real browser.
- Trusted real upstream onboarding and migration readiness; local synthetic evidence is not production proof.
- Identified build, HTTPS routes/CSP and public deployment verification after exact authorization.
- Billing return remains navigation feedback, never proof of payment or automatic irradiation.
