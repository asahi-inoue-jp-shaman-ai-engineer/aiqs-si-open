# Contributing to Aiqs SI — open shell

Thank you for your interest in improving the Aiqs SI open shell.
Contributions are welcome within the scope described below.

## Scope of this repository

This repository is the **open shell** of the Aiqs SI service: the
MCP server, the HTTP composition, the account/auth web UI, the
agent guide, and the test suite that runs against the shell.
Contributions are welcome for anything that lives here, for
example:

- documentation fixes and clarifications,
- translations of the documentation and the agent guide,
- improvements to the auth/account UI in `auth-ui/`,
- additional or improved tests in `tests/`,
- fixes and improvements to the MCP client-facing surface
  (tool schemas, serializers, error messages).

## What is not in this repository

The resonance core of the service is proprietary and is **not**
part of this repository (see [NOTICE](NOTICE)):

- the resonance matching / QRS engine,
- the morphic-field catalog dataset and its snapshots,
- the dedicated-device (diode) layer,
- the internal server-to-server delegation API,
- production operating parameters (prices, grant amounts,
  quotas).

Where the shell composes against those parts, this repository
carries clearly-marked deterministic stubs. Please do not submit
changes that try to reconstruct the proprietary core, and do not
present stub outputs as real scan inputs or results. Issues that
depend on the proprietary core cannot be reproduced from this
repository; they are routed internally when reported.

## Filing an issue

Bug reports and suggestions about the shell are welcome as
GitHub Issues. A report is most useful when it says what you
expected, what happened instead, and how to reproduce it against
this repository (the quick start in [README.md](README.md) sets
up a runnable shell).

Issues are also opened from inside the service itself: AI agents
using the Aiqs SI MCP server can file a report through the
`report_issue` tool, and those reports arrive in this issue
tracker as well.

**Security exceptions.** Vulnerabilities, and anything involving
credentials, credit/grant/referral abuse or personal data, must
**never** be filed as a public issue. Report them privately as
described in [SECURITY.md](SECURITY.md).

## Pull requests

- Pull requests are reviewed by the maintainer. Review may take
  time, and acceptance is not guaranteed.
- Keep a pull request focused on one topic, and describe the
  problem it solves. Running the test suite before submitting
  helps: `python -m unittest discover -s tests` for the Python
  shell and `npm test` in `auth-ui/` for the UI (see the quick
  start in [README.md](README.md)).
- This repository is a public export of the maintainer's tree.
  An accepted change is merged by the maintainer and reaches
  production only through the maintainer's own release process;
  a pull request here never deploys anything by itself.

## License of contributions

By submitting a contribution, you agree that it is provided
under the Apache License, Version 2.0 — the same license that
covers this repository (inbound = outbound). The names "Aiqs SI"
/ "アイクスSI" and the product logo are trademarks of the
operator and are not licensed (see [NOTICE](NOTICE)).

## Conduct

Please be respectful and constructive in issues, pull requests
and discussion. Assume good faith, keep feedback about the work
rather than the person, and help us keep this a place where
careful, good-faith contributions are easy to make.

---

## 日本語の要約

Aiqs SI の公開シェル（MCP サーバー、認証 UI、エージェント
ガイド、テスト）へのコントリビューションを歓迎します。
共鳴マッチング／QRS、ダイオード層などの中核はプロプライ
エタリで、このリポジトリには含まれません。不具合報告は
GitHub Issues へ（脆弱性や不正利用の報告は SECURITY.md の
とおり非公開で）。プルリクエストはメンテナーがレビュー
し、マージの保証はありません。本番への反映はメンテナー
自身のリリース手順でのみ行われます。コントリビューショ
ンは Apache License 2.0 のもとで提供されます。
