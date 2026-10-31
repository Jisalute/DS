# Authentication, operator mutations and user/bankcard privacy

## Change record and client migration

2026-10-25: AUTH-001/002, USER-001 and user/bankcard PRIVACY-001 cutover. Apply numbered migration `004_security_auth.sql` with the tracked migration runner before serving requests. It creates InnoDB SMS limit/challenge and operator-audit tables, ensures point audit columns, and explicitly deletes existing sessions once. Stateless JWTs lacking `sid` are always rejected; `users.token` is never an authentication source. Existing clients must discard stored tokens and log in again. No shared key grants permissions. Optional legacy `key`/`admin_key` fields are ignored, not credentials. No refresh-token flow exists.

2026-11-01 candidate: password reset now requires the issuance response's `session_id`. This is a breaking change: old clients receive 422 and cannot reset. Ship the client and server together; retain the latest send response in the client and submit its `session_id` with the same mobile and SMS code. No legacy bypass. Existing challenges and counters remain in migration 004's tables; no new SQL migration or counter reset is required. During a rolling deployment old instances still accept requests without the handle, so keep reset unavailable until all instances have cut over. Do not roll back to old code as a security fix.

All protected requests use `Authorization: Bearer <token>`. JSON endpoints accept/return application/json; SMS bankcard sending uses application/x-www-form-urlencoded. Responses are HTTP200 unless stated. Error shape is `{"detail":"reason"}`; 401 invalid/expired/revoked/missing credential means log in again, 403 ownership/role/status means do not retry as another ID, 400 business rejection means correct the input, 422 schema rejection means correct format, 429 wait for limit window, 503 means service unavailable (never treat it as sent). Server failures never imply success. HTTP500 may occur on transaction failure; no successful local transaction is inferred.

## Identity and revocation contract

`POST /user/auth`: unchanged body `mobile` string, `password` string, optional `name` (custom name forbidden for new registration). Returns `uid` integer internal account ID, `token` string Bearer JWT, `level` integer membership star, `is_new` boolean. Example request `POST /user/auth`, header Content-Type application/json, body `{"mobile":"19900000001","password":"Synthetic-pass-42!"}`; success `{"uid":42,"token":"<credential>","level":0,"is_new":false}`. Wrong password yields400; frozen/deleted403. JWT issuance locks the user and compares the hash used for password verification so a concurrent password change cannot mint a valid stale credential. UUID is issued/accepted only with ENABLE_UUID_AUTH; its disabled issuance defaults to JWT. `POST /wechat/login` retains WeChat-code verification and its existing response; its 124-character opaque token uses the same sessions table. The unused hardcoded-key WechatService.generate_token issuer was removed.

Every supported token authenticates through unexpired sessions joined to active users. JWT includes signed user_id, sid, exp, iat, type=access and session ownership is checked. No identity cache or legacy fallback. Authentication storage outage503 fails closed. Password reset/change, admin password reset, freeze/delete/unfreeze and admin mobile change delete all target-user sessions in their mutation transaction; unrelated users remain logged in. `invalidate_token(token)` deletes the JWT sid or exact opaque token and propagates DB errors. There is no new logout HTTP route.

## 当前认证账户身份

### 基本信息

`GET /user/me/identity` 返回服务器通过当前有效会话认证的账户 ID，供客户端区分账户，例如隔离订单幂等操作。需登录；普通用户、商户及平台管理员均可调用，但管理员也只能得到自己的 ID。无特定角色或会员资格要求。请求无请求体，故不要求请求 `Content-Type`；响应为 `application/json`，成功 HTTP 200。路径以部署服务根 URL 为准，例如 `https://<api-host>/user/me/identity`。

### 请求参数

| 参数 | 位置 | 类型/JSON 类型 | 必填 | 默认值 | 格式、校验及含义 | 合法 / 非法示例 |
|---|---|---|---|---|---|---|
| Authorization | header | string / 不适用 | 是 | 无 | `Bearer <token>`；token 必须是当前有效、未撤销的服务端会话凭证 | `Bearer <valid-token>` / 缺失或 `Bearer invalid` |

无 path、query、cookie 或 body 参数，无请求体。未声明的 query 参数被框架忽略，不是身份选择器；例如 `?user_id=123&mobile=19900000000` 也只返回会话本人，客户端不应传这些参数。不要将 token 放在 URL。

请求示例：

```http
GET /user/me/identity HTTP/1.1
Host: <api-host>
Authorization: Bearer <valid-token>
Accept: application/json
```

无请求体。非法认证示例：相同请求省略 `Authorization`，返回 401，不返回身份。

### 返回参数

| 字段 | JSON 类型 | 必返 | 空值/枚举 | 含义 | 示例 |
|---|---|---|---|---|---|
| user_id | string | 是 | 不为空；无枚举 | 已认证账户的十进制用户 ID 字符串，不是手机号、JWT 中未经核验的声明或请求选择的用户 | `"9007199254740993"` |

成功示例（注意是字符串而非 JSON 数字）：

```json
{"user_id":"9007199254740993"}
```

### 使用方法与业务规则

先取得有效的登录 Bearer，再调用此接口；以返回的完整字符串作为当前账户的标识，避免转换为 JavaScript `Number` 导致大整数精度丢失。仅按账户隔离本地订单幂等记录，不能把此 ID 当认证凭证或替代订单接口的服务端授权；不要信任本地保存的手机号、请求中的目标 ID 或管理员可查询其他人的 `/user/info` 作为当前会话身份。每次登录、切换账号或恢复会话时重新取得并核对身份；不能跨账户沿用结果，认证失败时不要使用旧身份继续处理订单。返回仅有 `user_id`，不返回手机号、openid、角色或 token。

该接口只读且幂等，无状态流转、额度扣减、费用或 `Idempotency-Key` 要求；并发请求分别验证会话，不会变更账户数据。无接口专属限流配置；部署层可能有限制。会话失效、撤销、账户冻结或注销后依现有认证规则拒绝请求，不存在空结果或匿名身份。有效会话检查依赖认证数据库；存储故障不得降级为成功。

### 错误

无独立业务错误码；错误响应使用项目统一的 `detail` 字符串。

| HTTP | 触发条件 | 响应示例 | 客户端处理 |
|---|---|---|---|
| 401 | 缺少、无效、过期或撤销的凭证；禁用的 UUID 认证 | `{\"detail\":\"缺少认证令牌\"}` 或 `{\"detail\":\"认证令牌无效或已过期\"}` | 停止当前账户操作，重新登录；缺少令牌时还返回 `WWW-Authenticate: Bearer` |
| 403 | 会话账户已冻结或注销 | `{\"detail\":\"账号已冻结或注销\"}` | 停止操作，按账户状态处理 |
| 503 | 认证存储不可用 | `{\"detail\":\"认证服务暂不可用\"}` | 停止操作，稍后重试；不得沿用旧身份冒充成功 |

### 兼容性与变更记录

新增此独立路径及响应模型，不改变 `/user/info`、登录响应或其他既有接口；旧客户端无需迁移，未废弃字段。新客户端应调用本接口获取会话本人 ID，并将字符串原样保存；登录响应中的数值 `uid` 及可由管理员查询他人的 `/user/info` 均不提供本接口的精确身份契约。本接口不依赖权益接口。

## Send password-reset SMS

### Basic information

`POST /user/reset-password/sms`, anonymous, application/json, HTTP200. Internally queries and locks the current account at the supplied mobile to bind its trusted account ID to the challenge. Registered and unregistered mobiles follow the same supplier issuance flow and return the same accepted response shape; an unknown account stores no redeemable account identity. Sends only to the supplied validated mainland-China mobile. Codes and account-existence results cannot be returned to clients or logs.

### Request parameters

| Field | Location | Type | Required/default | Validation and meaning | Legal / illegal |
|---|---|---|---|---|---|
| mobile | body | string | required | `^1\d{10}$`; SMS target and reset identity | `19900000001` / `abc` |
| device_id | body | string | required | 16–128 characters; client installation identifier (not an authentication factor) | `installation-000001` / `x` |

Request: `POST /user/reset-password/sms`, `Content-Type: application/json`, body `{"mobile":"19900000001","device_id":"installation-000001"}`. Illegal body `{"mobile":"abc","device_id":"x"}` →422.

### Response parameters

| Field | Type | Required | Meaning/example |
|---|---|---|---|
| session_id | string | yes | 32-character random challenge identity; `aabb...` |
| expired_in | integer | yes | lifetime seconds,300 |
| retry_after | integer | yes | minimum mobile issuance interval seconds,60 |
| status | string | yes | `accepted` means provider accepted, NOT delivered |

Example `{"session_id":"0123456789abcdef0123456789abcdef","expired_in":300,"retry_after":60,"status":"accepted"}`. No `mock_code` field, no null-code success, no data-empty success.

### Usage and business rules

Call issuance before reset. Only latest challenge for the same mobile/purpose/subject is usable, even if a newer send was rejected/unknown. Six-digit cryptographically random code; stored HMAC uses independent SMS_CODE_SECRET (minimum32 characters), bound to random session. TTL5 minutes. Scope counters use MySQL row locks, not process memory: mobile5/hour with60-second cooldown, IP20/hour, device10/hour, global SMS_GLOBAL_HOURLY_LIMIT/hour (default1000). Client cannot select trusted IP: request.client.host is used, not forwarded headers. Deploy behind a correctly configured trusted proxy to avoid all clients sharing one transport peer. For password reset, only the latest accepted, unexpired, unconsumed challenge with matching account and session_id reaches the mobile+purpose scoped five-attempt/hour counter; it persists across resend, including successful checks. Wrong, expired or superseded handles do not spend that counter. Bankcard accounting is unchanged. Successful business operation and consume commit together; rejected attempts are committed before400. Do not blindly retry uncertain sends or claim exactly-once SMS delivery.

**Residual availability risk / issue 222 not fully resolved:** the session_id is returned to the anonymous sender, not proof of phone ownership. Someone who initiates a send or learns the latest session_id can still submit five wrong codes and exhaust the shared limit, and issuance cooldown/limits can also be abused. Retaining the cross-resend limit prevents bypassing the existing brute-force bound but cannot guarantee recovery availability against this attacker. Do not characterize this candidate as eliminating denial of service; further independently reviewed recovery/anti-abuse design is required.

### Errors

422 malformed fields;429 any issuance limit;503 missing secret/provider configuration, supplier rejection/transport timeout/unconfirmed acceptance;500 DB persistence failure. Example `{"detail":"短信请求过于频繁"}`. Unknown provider outcomes are stored `unknown`, definitive rejections `rejected`; neither authorizes a reset. Pending is persisted before the network request. No DB locks are held during provider calls.

## Redeem password reset

`POST /user/reset-password`, anonymous, `Content-Type: application/json`, HTTP200 on success; no role or Authorization required.

| Body field | Type | Required/default | Validation and meaning | Legal / illegal |
|---|---|---|---|---|
| mobile | string | required | Exact mobile used when issuing | `19900000001` / different mobile |
| session_id | string | required | **Changed:** latest issuance response, exactly 32 lowercase hexadecimal characters; treat as a secret bearer challenge and never log it | `0123456789abcdef0123456789abcdef` / `bad` |
| sms_code | string | required | Exactly six digits, from the SMS | `382746` / `1` |
| new_password | string | required | 8–72 characters | `New-synthetic-pass-42!` / `short` |

Request: `POST /user/reset-password`, header `Content-Type: application/json`, body `{"mobile":"19900000001","session_id":"0123456789abcdef0123456789abcdef","sms_code":"382746","new_password":"New-synthetic-pass-42!"}`. Missing/malformed `session_id` or malformed code returns422. Success HTTP200 `{"msg":"密码已重置"}`: `msg` is a required non-null human-readable string.

First request SMS; retain the returned handle for that specific latest send and submit it with the code. A new send supersedes the previous challenge even if the provider rejects it. 400 `{"detail":"验证码错误或已过期"}` covers invalid, expired, consumed, superseded, wrong-purpose/subject/account/handle, and attempt-locked challenges, including unknown accounts; obtain a new challenge subject to existing issuance and attempt limits. The same challenge can succeed only once under concurrency; successful password update, consumption, session revocation and audit are one transaction. 422 `{"detail":[...]}` means correct the request; 500 means transaction failure, not success. Never infer delivery from provider acceptance. The mobile owner is locked and must match the account ID stored at issuance; mobile reassignment invalidates outstanding challenges. No unregistered-target404 is returned. After success, clear stored credentials and return to login.

## Operator mutation matrix

All rows require a configured ADMIN_USER_IDS account's valid Bearer, even with forged/old shared keys. Operator ID comes from authentication; reason comes from business request/default. `security_operator_audit` stores action,target,operator,before/after,reason,time in the same transaction; password hashes/codes are never audit values. No request-time DDL for these operations.

| Method URL | Request (all listed fields required unless optional) | Return fields | Rules |
|---|---|---|---|
| POST /user/set-status | JSON mobile string,new_status enum0normal/1frozen/2deleted,reason string optional default后台调整 | success boolean,msg string | lock target, change status,revoke,audit |
| PUT /user/freeze; PUT /user/unfreeze | JSON mobile string,reason optional,admin_key optional ignored | success boolean,msg string | revoke on either operation so unfreeze cannot resurrect sessions |
| PUT /admin/user/reset-pwd | JSON mobile string,new_password string,admin_key optional ignored | msg string | hash update,revoke,audit |
| POST /user/upgrade | query mobile string | old_level,new_level integers | increment one, maximum6, lock+audit |
| POST /user/set-level | JSON mobile string,new_level integer0–6,reason optional | old_level,new_level integers | lock+audit |
| POST /user/grant-merchant | query mobile string | msg string | set flag1, audit existing/new state |
| PUT /user/mobile | query user_id positive integer,old_mobile,new_mobile strings,key optional ignored | msg string | old identity match,unique new mobile,revoke,audit |
| POST /points | JSON mobile,type enum member/merchant,amount numeric,reason optional | msg string | trusted operator_id,generated event_id,source=admin; points balance before/after and operator audit same transaction |
| POST /admin/unilevel/set | JSON user_id integer,level schema range | user_id,old_level,new_level integers,changed boolean,message string | lock user,upsert level,audit |
| POST /admin/platform-return-address | query name,phone,province,city,district,detail strings,admin_key optional ignored | msg string | administrator configuration,audit |

Example operator request: `POST /user/set-level`, headers Authorization Bearer <admin>, Content-Type application/json, body `{"mobile":"19900000001","new_level":3,"reason":"授权调整"}`; success `{"old_level":2,"new_level":3}`. Illegal new_level7 →422. All return scalar fields above are required, nonnull; msg/message strings are human-readable, not codes. Missing target404; maximum/duplicate identity400; no role403; DB/audit failure500 rolls back state. Existing successful no-change responses may report unchanged state. Repeated point adjustments are separate intentional events, not a client-idempotency contract; do not auto-retry unknown outcomes. This does not redesign financial policy.

## Personal-resource scope and privacy

Public entrypoints only: password/WeChat login, password-reset issuance/redemption, explicitly public platform-return address. All personal routes below require Bearer. Mobile/user_id selectors are checked before business/provider access: only the authenticated identity or explicitly configured platform admin may select another user's personal resource.

- `/user/info`, `/user/list` (list admin-only), `/user/mobile` (admin-only), `/points/balance`, `/points/log`, `/points/summary`, `/my` coupons preserve their prior response structures.
- `POST /user/self-delete` additionally requires matching mobile and password; atomic delete-status/revoke/audit. Returns msg string. Self-service always uses authenticated user's ID (even admin cannot delete someone else through this flow).
- `/user/update-profile` is self-only, matching mobile; old_password required for new_password; row-lock verification then hash/revoke/audit together. Nickname content review remains required and avatar_path cannot bypass avatar review.
- `POST /user/bind-referrer`, `GET /user/refer-direct`, `GET /user/refer-team`, `GET /reward/list`, `GET /user/unilevel`, `GET /user/is-merchant`, return-address read/write now require self/admin mobile scope. Existing request bodies/query parameters and return structures unchanged. Referral/team rows retain their relationship business data; anonymous/cross-owner enumeration denied.
- `GET /reward/by-order/{order_id}` is admin-only rather than exposing every recipient to order-ID enumeration.
- `/address` creation/update/default/delete/list retain server-side ownership checks.
- `GET /unilevel/status`, `POST /unilevel/promote`, `GET /user/referral-qr`, `POST /user/refresh-referral-qr` require self/admin user_id scope. GET queries have no request body; POST promotion/refresh retain query user_id, no body. Existing response schemas unchanged.
- `POST /user/get-phone` JSON code,user_id must match authenticated owner before external phone exchange. `POST /user/decrypt-phone` requires Bearer and exchanged openid must match authenticated openid; retains code,encrypted_data,iv JSON and phone string response. No arbitrary account phone mutation.
- Avatar upload/review/clear retain owner checks. Commented triple-quoted points-clearing endpoints are not registered API routes.

Example privacy read `GET /user/info?mobile=19900000001`, header Authorization Bearer <owner>, no body. Replacing mobile with another account produces403 `{"detail":"只能查询本人信息"}`; anonymous401. No query key can bypass it. Frontends must not use403 as a reason to keep retrying login.

## Bankcards

All `/api/bankcard/*` paths require Bearer and derive user_id exclusively from it: bind, sms/send, modify/apply, modify/status, list,status,logs,my,default/set. Existing card ownership predicates retained.

### Send

`POST /api/bankcard/sms/send`, form parameters account_number string16–30 decimal digits required, device_id string16–128 required, purpose enum bankcard_bind (default) / bankcard_modify. Trusted target is current user's registered mobile, never a client mobile field. Invalid account `abc`422. Example form `account_number=6222021234567890000&purpose=bankcard_bind&device_id=installation-000001`. Headers Authorization Bearer <owner>, Content-Type application/x-www-form-urlencoded. HTTP200 `{"code":0,"message":"验证码已发送","data":{"session_id":"0123456789abcdef0123456789abcdef","expired_in":300}}`. code integer0 and message string required; data.session_id string and data.expired_in integer required. Accepted is not delivered. Same limits and errors as reset; unbound mobile400. mock_code removed.

### Bind and modify

`POST /api/bankcard/bind` keeps JSON account_name(2–100),account_number(16–30 digits),account_bank(2–50),bank_name(2–128),bank_branch_id(optional≤128),bank_address_code(6 digits),sms_code(6 digits),session_id(32 chars). Bind must redeem the matching bankcard_bind challenge for authenticated user+exact card. Local binding and challenge consumption/audit share one transaction after external settlement-data verification. Invalid code returns400 and commits failed attempt; local write failure rolls back consumption. Returns existing envelope code0,message,data(account_id integer,action created/updated/already_bound,verify_method wechat_api,verify_status success,msg ok).

`POST /api/bankcard/modify/apply` keeps new_account_name,new_account_number,new_account_bank,new_bank_name,optional bank_branch_id/bank_address_code and now requires six-digit sms_code AND32-char session_id. Redeems bankcard_modify only before external application; unlike local bind this external operation cannot roll back SMS authorization after sending to provider. API correctly awaits the async service. Returns envelope with data.msg,data.application_no,data.status=pending_review,data.sub_mchid. Foreign purpose/card/session fails400 before external modification. Frontend must request fresh authorization after rejected external application; no blind retry with consumed code.

`GET /api/bankcard/logs?limit=50` has no body, limit1–1000; returns code/message/data array. Each item id(integer),operation_type(string),target_id(integer/null),old_val/new_val(object/null),created_at(string) only. Shared admin_key and IP removed from response. `/my` preserves masked account number/tail and now masks account_name too; never returns full card number. Other bankcard schemas unchanged; request errors400, anonymous401, ownership403, provider/storage failures cannot be interpreted as success.

## Provider configuration and operations

Set .env SMS_SECRET_ID, SMS_SECRET_KEY, SMS_SDK_APP_ID, SMS_SIGN_NAME, SMS_RESET_TEMPLATE_ID, SMS_BANKCARD_TEMPLATE_ID, SMS_REGION, SMS_TIMEOUT_SECONDS and independent SMS_CODE_SECRET. Templates accept exactly one code parameter; provider account requires approved signature/templates and SMS send/status-pull permissions. Missing values explicitly503, never fixed/mock SMS. Production secrets are operator-injected; not in repository. Provider message serial is stored, plaintext code is not. `reconcile_sms_receipts(mobile,begin_time,end_time)` is an operator-only Python service callable (not a public endpoint), queries up to100 provider receipts and persists delivered/failed separately from accepted. Narrow query windows must be used; unknown sends without returned serial are not automatically retried/reconstructed as accepted. Operator must reconcile supplier records before requesting a new send; no exactly-once claim.

Official references: [SendSms](https://www.tencentcloud.com/document/api/382/40536), [Python signing/SDK](https://www.tencentcloud.com/document/product/382/37259), [PullSmsSendStatusByPhoneNumber](https://www.tencentcloud.com/document/product/382/40518). Adapter signs actual HTTPS TC3-HMAC-SHA256 requests, version2021-01-11. Real delivery and signing compatibility require authorized internal mobile + real account credentials; controlled adapter tests are not that evidence.

## Regression execution (coordinator only)

`uv run pytest tests/isolation/test_auth_business.py -q` within guarded isolated runner environment. Tests add real-MySQL/app HTTP ownership checks; all-token revoke/unrelated-user validity; two reset consumers; attempt limits and resend retention; purpose/mobile/expiry rejection; forced audit failure rollback; legacy JWT rejection/logout; real adapter request at controlled HTTP boundary. No tests/build/lint/formatters were executed by AuthOwner. Migration must run before app requests; rollback requires restore/migration plan and cannot safely resurrect legacy credentials. No production mutation or SMS was performed.

Additional privacy change: `/api/bankcard/status` `wechat_display_info` is an explicit optional-field projection of `account_type`, `account_bank`, `verify_result` only; arbitrary upstream fields (especially account number/name) are never forwarded. Default-card mutation also records the authenticated actor and selected account ID in its local transaction. Boundary projection regression: `uv run pytest tests/test_security_auth_privacy.py -q` (not executed by AuthOwner).

## Review corrections and additional registered routes

`GET /all-points?user_id=<positive integer>` is a live route (unlike the commented legacy point-clear handlers). It now requires owner/admin Bearer; no body. Returns unchanged UserAllPointsResponse (four channel balances and total); anonymous401, other user's ID403. Audit review also corrected bankcard poll projections: selected user_id is retained for approved-card uniqueness checks and modify_application_no/modify_fail_reason are retained for `/status` pending/failed display. Critical logs and provider error display never interpolate raw exceptions.

Password-reset challenges additionally bind to the account ID found under a user row lock at issuance. Unknown account receives the same accepted issuance shape but no redeemable account identity. Redemption locks current mobile owner first, then attempt counter and challenge; its stored account ID must match. Mobile changes invalidate outstanding challenges for that target in the same transaction. An old-mobile code cannot follow the number to a different account. Both existing and new-registration password login verify submitted mobile and password again inside the user lock used to insert the session. Neither stale password nor stale mobile can issue a session after concurrent credential/identity change. Auth DB lookup and bankcard SMS lookup+network run in worker threads rather than blocking async event loop. Unused active-user/cache probe wrappers removed; no startup cache shim.

### System merchant role administration

`POST /api/system/grant-system`, authenticated configured platform admin, query user_id required positive integer; is_merchant required integer0/1/2 (ordinary/merchant/third-party-platform, preserves existing business enum); key optional string ignored. No request body. Headers Authorization Bearer <admin>. Example `/api/system/grant-system?user_id=42&is_merchant=2`; success200 `{"msg":"is_merchant 已更新","user_id":42,"is_merchant":2}`. All three response fields required nonnull: msg human description, user_id selected target, is_merchant committed type. Illegal is_merchant3→422. Missing/revoked Bearer401; ordinary/merchant even with old key403; missing target404; audit/storage failure500 and transaction rolls back. Locks user, records trusted operator and actual old/new role in same transaction; repeating desired type has no further role effect but records authorized intent. Literal shared-key authorization removed without compatibility bypass. Clients must migrate to administrator Bearer.

`PUT /api/system/sentences` also requires platform admin; JSON optional banner_sentence and system_sentence (at least one nonnull), strings under existing schema constraints. Example headers Authorization Bearer <admin>, Content-Type application/json, body `{"banner_sentence":"欢迎"}`. No-field body `{}`400; anonymous401/ordinary403. Success200 envelope status string `success`, message string human result, data fields id integer,banner_sentence/system_sentence strings,created_at/updated_at datetimes or null. Before/after values and trusted operator committed together. `GET /api/system/sentences` remains public and read-only; empty table returns data.id=null, blank strings, null times without writing default rows. Region GET /api/provinces,/api/cities/{province_code},/api/districts/{city_code} remain public read-only with existing responses. No other registered system write routes exist in the scanned module.

Fresh-account status consistency: migration004 sets only the `users.status` column default to0 (NORMAL); it never updates existing frozen1/deleted2 rows. Password registration and WeChat registration already write NORMAL0 explicitly. Harness bootstrap defaults match the same enum. Regression covers new password/WeChat users normal and an existing frozen user still denied by login and session issuance.
