# 利用者登録・認可の接続契約

2026-11-03。実装済みなのはOAuthリソースサーバー側と検証済みIDの内部登録。ブラウザでの利用登録・ログイン・同意画面、実認証事業者、Museでの認可は未接続。

## 現在の実装

2026-11-03追加：既存Supabase再利用のため、明示指定のユーザー用profileを追加。`--signing-algorithm ES256 --supabase-user-client <UUID>`ではtyp=JWTを用いるが、SI専用の単一audience・client allowlist・openidとaiqs:useの両scope・authenticated role・非匿名・UUID sub/session_idを必須にする。通常JWTを一般許可する変更ではない。未指定の既定とworkerは従来どおり。詳細・未配備のhook条件は `SUPABASE_ACTIVATION_PACKET.md`。実Supabase token互換性はまだ未検証。

2026-11-03追記: 起動時の `--signing-algorithm ES256` でEC P-256署名を明示選択できる。既定はRS256。以下のRS256記述は既定設定を指す。トークンが指定する方式への自動追従はしない。typ=at+jwt、audience、scope、client_id等の要件は維持する。実Supabaseの全トークン契約に対応済みという意味ではない。

- 外部の認可サーバーが発行するRS256署名のアクセストークンを検証する。IDトークンは受け付けない（typ=at+jwtが必要）。
- 固定した公開鍵、HTTPS issuer、サービスaudienceを使用。exp/iat/iss/aud/sub/client_id必須、aiqs:use scope必須。トークンのalgや公開鍵URLを信用して検証方法を変更しない。
- 利用者キーは検証済みのissuerとsubの組。メールアドレス一致でアカウントを結合しない。MCP引数から利用者ID・料金を受け取らない。
- 初回の利用者向けツール呼出で内部アカウントを一度作成。初回キャンペーンとして1,000クレジットを同一transactionで一度だけ付与する。OAuthクライアントの登録と利用者アカウントの登録は別物。
- BearerトークンをDBへ保存しない。SDKが401の認可メタデータを返す。protected resource metadataにはissuerとresourceを掲載する。
- stateless HTTPで各リクエストを認証する。stdioの起動元ownerはHTTPへ引き継がない。

## 起動

公開鍵と認可サーバーは事前に対象を確認する。秘密鍵をこのサーバーに渡さない。

```powershell
$env:PYTHONPATH = "$PWD/src"
python -B -m aiqs_si.http_server --database ./development.sqlite --issuer https://ISSUER --audience https://SERVICE/mcp --public-key ./issuer-public.pem --port 8765
```

サーバーは127.0.0.1へbindする。現段階は合成マスタ・模擬機器・テスト価格のみ。デプロイして本番サービスとして扱わない。実認証サーバーの登録/ログイン/同意、鍵更新・失効、HTTPS配信とホスト許可、利用開始・購入導線が残る。

## 証拠と限界

tests/test_auth.pyで一時RSA鍵をメモリ上に生成し、正しい署名・不正署名・issuer/audience/期限/scope・IDトークン混同を検証。StarletteのASGI TestClientで401 challenge、metadata、MCP tool call、別subjectからの結果取得拒否を確認。これを外部ネットワークやMuseでの成功とは呼ばない。

公式根拠：
- https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization
- https://pyjwt.readthedocs.io/en/stable/api.html

このアダプタはRFC9068型のJWTアクセス・トークンを前提にする。別形式のproviderを選ぶ際は黙って検証を緩めず、provider公式の検証契約と照合する。

## 公開鍵更新の接続準備

`--public-key` と排他的に `--jwks-url https://ISSUER/keys` を指定できる。運用者が設定したURLだけへ取得し、トークン内jku等を接続先に使わない。RS256公開署名鍵のみを扱い、60秒キャッシュ後に更新する。更新失敗後は期限切れ鍵を保持せず拒否し、5秒間の取得抑制を置く。新kidの反映に最大60秒かかり得る。これは鍵更新への追従であり、個別トークンの即時失効を実装したものではない。

MCP公式仕様ではユーザーとの対話・トークン発行は認可サーバーの役割。現行サービスのJWKS対応だけで登録/ログインが完成したとは扱わない。認可サーバーのdiscovery、client登録方式、PKCE、resource audience、scope、実際のclient互換を接続先ごとに検証する。

2026-11-03一次情報照合：Auth0の標準トークン形式はazp、RFC9068形式はclient_idとされる。またresource/audienceの相違でMCP接続が失敗する事例が公式に記載されている。Auth0の契約/採用は未決定であり、どのproviderでもそのまま動くと主張しない。

- https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization
- https://support.auth0.com/center/s/article/receive-the-client-id-claim-instead-of-the-azp-claim-in-an-auth0-access-token
- https://support.auth0.com/center/s/article/mcp-audience-error-with-auth0

## 初回キャンペーンと共通残高（朝陽承認）

新規ユーザー登録時に1,000クレジットを一度だけ付与する。残高の所有者はユーザーでありAIクライアントではない。同じissuer/subの利用者はclient_idが異なっても同じ残高を使い、接続追加・再送・再起動で再付与しない。台帳にはsignup_campaign_v1として記録する。既存アカウントへの遡及付与は行わない。

認可サーバーは異なるAIクライアントに対して同じ利用者を同じsubjectで識別できる構成が必要。クライアント別subjectや別issuerをメール等で自動統合しない。このローカル実装は人間一人につき複数アカウント作成を検出するものではない。実登録・認可画面は引き続き未接続。
