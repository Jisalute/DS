# Payment and order security contract

## Change record (2026-10-25)

ORDER-001/002/003, PAY-001/002, order/merchant privacy: protected aliases now use the same authenticated handlers; client prices never set merchandise prices; fulfillment and expiration lock the authoritative order; refunds use a committed intent before provider I/O; API-v3 callbacks require RSA signature and AES-GCM resource and acknowledge only after local commit. No business allocation, monthly rain-point330/30, coupon cap or membership policy change is authorized. This document describes implementation, not executed acceptance evidence.

## Shared HTTP/error contract

Client calls use `Authorization: Bearer <session-backed-token>`, JSON request/response. Missing/expired/revoked credentials:401; authenticated wrong owner:403; missing order:404; conflicting state or immutable refund identity:409; invalid typed input:422; provider/DB unavailable or unknown external result:503 (provider prepay failure502). Error JSON is `{"detail":"reason"}`. 401 requires login,403 must not cause infinite relogin;409 requires refresh/reconcile;503 must retain the same logical identity and query rather than create a new refund. No shared `key`/`admin_key` grants authority. Callback calls deliberately do not require customer Bearer tokens.

## Order creation and trusted price/stock

**Basic information:** POST `/order/create`; logged-in buyer only;200 JSON. `user_id` must equal the token owner. `buy_now` defaults false; immediate purchase requires `buy_now_items`, each with `product_id` and optional `sku_id` as a positive decimal string up to unsigned BIGINT maximum or a JavaScript-safe positive integer (SKU omission only for single-SKU products), strict positive integer `quantity`, and optional nonnegative legacy `price` ignored for pricing. Booleans, fractional values and unsafe numeric IDs are rejected. Existing address/delivery/points/coupon/idempotency fields retain the schema. `merchant_id` optional must match actual products' owner. All items must belong to one merchant. Cart SKU/product identity is validated. Price comes from `product_skus.price`; conditional stock decrement checks affected rows in the same transaction as header/items, so failure leaves neither partial order nor consumed inventory. Zero cash orders settle locally in the same transaction, not by synthetic payment callback.

Cart checkout requires the buyer-confirmed `cart_snapshot` array of `{product_id:string,sku_id:string,quantity:integer}` for all cart orders. The snapshot is the locally selected subset, independent of the database `selected` flag; the server locks cart rows, checks each selected SKU and quantity, and purchases/deletes only those rows. Missing/duplicate/invalid snapshots return422; an absent or changed snapshot row returns409 `{"detail":{"code":"CART_SNAPSHOT_CHANGED","message":"购物车已变化，请重新确认"}}`, meaning this attempt created no order; other 409s have different meanings. Clients obtain exact IDs from the additive `product_id_str`/`sku_id_str` cart fields. `user_id` and cart mutation product/SKU IDs also accept decimal strings; numeric values outside JavaScript's safe-integer range return422. Previously committed same-key requests replay their original result before checking the current cart. This is a breaking change for old cart clients; see [cart migration and examples](cart-order-security.md). Prices remain server-authoritative.

**Request example:** `POST /order/create`, Bearer header, `{"user_id":101,"buy_now":true,"delivery_way":"pickup","buy_now_items":[{"product_id":3,"sku_id":5,"quantity":1,"price":"0.01"}]}`. Illegal quantity examples:0,-1,1.5,true,"1"; these do not create orders.

**Response:** existing order creation envelope remains; order number string identifies the persisted order and `need_pay` boolean distinguishes actual cash payment from zero cash. Frontend displays service-calculated totals rather than legacy item price. Insufficient stock400/409 requires refresh; invalid SKU/product or merchant mapping422.

## Prepayment (all aliases)

**Basic information:** POST `/wechat-pay/create-order`, `/api/wechat-pay/create-order`, `/order/pay`; owner Bearer required;200 JSON. No request-body authority or openid override. Order must be pending_pay. Coupon IDs and points are fixed at creation, not mutable after prepay creation (409 if supplied values differ). Network is outside DB locks.

| Request field | Location/type | Required/default | Meaning and validation |
|---|---|---|---|
|out_trade_no (order_id alias)|body string|yes|owned order number|
|total_fee|body integer cents|yes positive|legacy display amount; trusted server payable wins|
|openid|body string|yes|must equal authenticated buyer's stored binding|
|description|body string|optional 商品支付|payment description|
|coupon_ids/coupon_id|body integer array/integer|optional stored|must equal stored discounts|
|points_to_use|body decimal|optional stored|must equal stored pending points|

Example `POST /order/pay`, Authorization Bearer, `{"out_trade_no":"synthetic-order","total_fee":100,"openid":"synthetic-binding"}`. Illegal: another user's order/binding returns403. Zero-cash pending order returns409, not fake ZERO_ORDER payment success.

| Response field | Type/required | Meaning |
|---|---|---|
|prepay_id|string yes|provider prepay identifier|
|pay_params|object yes|arguments passed unchanged to wx.requestPayment|
|pay_params.appId|string yes|configured mini-program app ID|
|pay_params.timeStamp|string yes|provider payment timestamp|
|pay_params.nonceStr|string yes|payment nonce|
|pay_params.package|string yes|prepay_id=...|
|pay_params.signType|string yes|RSA|
|pay_params.paySign|string yes|signature; never log|
|total_fee|integer yes|server cents charged|
|wechat_raw_response|object yes|existing provider prepay response; not payment completion evidence|

Success shape `{"prepay_id":"wx-test","pay_params":{"appId":"test","timeStamp":"1","nonceStr":"test","package":"prepay_id=wx-test","signType":"RSA","paySign":"signature"},"total_fee":100,"wechat_raw_response":{"prepay_id":"wx-test"}}`. Client return/navigation is never trusted as paid.

## Refund application, audit, direct submission and recovery

**Change record (issue 222; breaking authorization change):** `/wechat-pay/refund` and `/api/wechat-pay/refund` previously allowed the buyer to execute a provider refund, implicitly handling an application; they now require the order's merchant or an administrator. Buyers must call `/refund/apply` and await merchant review; a legacy buyer direct call returns 403. Application approval and direct execution have distinct identities; old clients must not assume that a direct call approves an application. Offline direct execution accepts optional `refund_request_id`, previously unavailable. No fields or routes are removed.

**Basic information:** all client calls use `Authorization: Bearer <session-backed-token>`, JSON request/response and HTTP 200 on successful handling. Endpoints and required actor:

| Method and URL | Actor | Purpose |
|---|---|---|
| POST `/refund/apply` | owning buyer | create one application, no provider refund |
| POST `/refund/audit`, `/merchant/approve_refund` | owning merchant or platform admin | approve or reject that application; approval creates/replays its refund intent |
| POST `/wechat-pay/refund`, `/api/wechat-pay/refund` | owning merchant or platform admin | execute/replay a direct online refund |
| POST `/api/offline/tuikuan` | owning seller or platform admin | execute/replay a direct offline refund |
| GET `/refund/progress/{order_number}`, `/refund/query/{order_number}` | owning buyer, owning merchant or platform admin | read local application/order snapshot; no request body |
| POST `/refund/sync-status/{order_number}` | platform admin | recover latest intent, no request body |

**Request parameters** (all body fields are JSON, unless indicated):

| Endpoint | Field | Type, required/default | Validation and meaning |
|---|---|---|---|
| apply/audit/online direct | `order_number` | string, required | existing order number |
| apply | `refund_type` | string, required | `refund_only` or `return`; e.g. `refund` is invalid |
| apply | `reason_code` | string, required | buyer's application reason |
| audit | `approve` | boolean, required | approve or reject |
| audit | `reject_reason` | string/null, optional/null | rejection reason |
| audit | `merchant_address` | string/null, optional/null | required for first approval of `return` |
| online direct | `refund_fee` | integer/null, optional/null | positive integer cents, no larger than original cash paid; null means original full amount, not remaining balance; booleans, strings and fractions invalid |
| offline direct | `order_no` | string, required | existing offline order number |
| offline direct | `refund_amount` | integer/null, optional/null | same units, default and validation as `refund_fee` |
| online direct | `reason` | string, optional/empty | at most 512 characters, persisted locally, not an authorization factor or currently sent to provider |
| online/offline direct | `refund_request_id` | string/null, optional/null | 1–64 characters; stable identity for one logical refund; omitted/null selects legacy `default` |
| progress/query/sync | `order_number` | path string, required | order whose snapshot/latest intent is requested |

**Examples** (replace the synthetic tokens; never reuse them):

```http
POST /refund/apply
Authorization: Bearer <buyer-token>
Content-Type: application/json

{"order_number":"synthetic-order","refund_type":"refund_only","reason_code":"not needed"}
```

Merchant audit: `POST /merchant/approve_refund` with merchant Bearer and `{"order_number":"synthetic-order","approve":true,"merchant_address":null}`. Merchant direct partial: `POST /wechat-pay/refund` with merchant Bearer and `{"order_number":"synthetic-order","refund_fee":50,"refund_request_id":"item-1"}`. Offline: `POST /api/offline/tuikuan` with seller Bearer and `{"order_no":"OFF-synthetic","refund_amount":50,"refund_request_id":"item-1"}`. Admin recovery: `POST /refund/sync-status/synthetic-order`, admin Bearer, no body. Invalid direct example `{"order_number":"synthetic-order","refund_fee":true}` returns 422; buyer direct returns 403.

**Response fields:** apply and audit return `{"ok":true}` (`ok`: boolean, always true on success). Direct and sync return:

| Field | Type | Meaning |
|---|---|---|
| `success` | boolean, required | local intent handled; not proof money arrived |
| `refund_no` | string, required | durable provider refund number, stable for this identity |
| `status` | string, required | `prepared`, `unknown`, `processing`, `success`, `closed`, or `change` (abnormal) |
| `message` | string, required | safe explanatory text |

Example `{"success":true,"refund_no":"RF-synthetic","status":"processing","message":"退款状态已持久化；受理不代表到账"}`. Offline returns `{"code":0,"message":"退款受理成功","data":{"success":true,"refund_no":"RF-synthetic","status":"processing","message":"退款状态已持久化；受理不代表到账"}}`; `code` is integer, `message` string and `data` has the same fields above. No empty list or pagination is involved.

**Use, identity and states:** buyer applies only against their eligible paid order; only one application per order is currently supported. Creation returns 409 when any refund intent or historical `orders.refund_no` already exists, including successful partial, processing or closed refunds: the application has no agreed remaining-amount contract. This check and intent creation serialize on the order lock; no automatic remaining amount is inferred. Existing historical applications with earlier partial refunds can still encounter the aggregate limit on approval and require reconciliation. Approving an `applied` application creates `application:<id>`; repeated audit returns/reuses its original number. `seller_ok` and `refund_success` without a matching intent require reconciliation; a rejected application cannot be approved. Audit applies the original full cash amount. Direct uses `direct:<order>:<refund_request_id>` and never changes an application. An existing direct key remains replayable if a historical application also exists. A new direct request with an `applied`, `seller_ok`, rejected or otherwise unresolved application returns 409; after `refund_success`, an explicit new key can start an independent partial refund, while omitted key is ambiguous and returns 409. Same key with changed amount returns 409. Successful partial refunds require a distinct stable key for each additional logical refund; reservations and prior successes cannot exceed original cash. Unknown/processing retains the reservation and cannot create a new refund while the order is refunding. Closed restores the previous order status; closed/change blocks *new* identities pending reconciliation. The application state is updated only for its own intent. Partial success restores previous fulfillment state; cumulative full success marks refunded and revokes discounts once.

**Recovery and operational limits:** order lock and intent commit precede provider I/O. Timeout or unverifiable response is unknown; retain the same request identity. A 30-second lease prevents concurrent recovery. Only a cryptographically verified `RESOURCE_NOT_EXISTS` response permits a same-number resubmission; unsigned errors are not assumed signed or trusted. `change` may be queried by admin with the original number and can become `success`, but is never resubmitted. Historical `refund_no` without intent is quarantined. There is currently **no implemented evidence-backed reconciliation approval/unblock workflow** for closed/change: operators must reconcile externally and agree a controlled remediation; do not manually create a new refund as an automatic workaround. Per [WeChat refund API](https://pay.weixin.qq.com/doc/v3/merchant/4013071036), provider acceptance is not arrival, one payment supports at most 50 partial refunds, and distinct partial refund calls should be at least one minute apart; callers/operators must schedule accordingly, and retries retain the same number. No additional per-client API limit is introduced here.

**Errors and migration:** `401` missing/expired token (login), `403` wrong actor (do not retry as buyer), `404` missing order/intent (reconcile if historical), `400` duplicate/ineligible application, `409` state/identity/amount conflict (refresh and reconcile, never rotate an unknown key), `422` malformed input (correct request), `503` provider/storage failure or unknown result (retain original key and use admin recovery). Typical response `{"detail":"同一退款申请金额不可变更"}`. Merchant clients should persist a separate stable key and amount per direct refund, use audit for an existing application, and schedule separate partial refunds according to provider limits. Legacy direct requests that were implicitly associated with an application have no unambiguous client-key association: use audit to replay that application, not a newly invented direct key. Existing direct keys continue to replay their original intents. The current candidate also requires migration `012_referral_reward_grants.sql`; historical reconciliation prerequisites remain in force.

**Read responses:** progress returns `{}` when no application, otherwise `id` integer, `order_number`, `refund_type`, `reason`, `status`, nullable `reject_reason` and `merchant_address`, `created_at` and `updated_at`. Query returns `order_number`, `order_status`, nullable `refund_no` and `transaction_id`, `total_amount` in yuan, timestamps, nullable `refund_apply` (`id`, `refund_type`, `reason`, `status`, nullable `reject_reason`, timestamps), `account_flows:[]` and `wechat_status_note`. These are local snapshots, not provider proof.

## Callback protocol and replay

**Basic information:** POST `/wechat-pay/notify`, `/api/wechat-pay/notify`, `/api/offline/zhifu/notify`; refund POST `/wechat-pay/refund-notify`, `/api/wechat-pay/refund-notify`. `Content-Type: application/json`; no Bearer. Required headers `Wechatpay-Signature` base64 RSA-SHA256, `Wechatpay-Timestamp` integer seconds within300s, `Wechatpay-Nonce` string, `Wechatpay-Serial` configured public-key ID. Header signature covers exact raw body. `WX_MOCK_MODE` never bypasses signature or decryption.

Body `{id:string,event_type:string,resource:{ciphertext:base64 AES256-GCM,nonce:string,associated_data:string}}`. Payment event `TRANSACTION.SUCCESS`; decrypted payload requires `out_trade_no`, `transaction_id`, `trade_state:"SUCCESS"`, matching `mchid` (or configured service-provider identity), appid when present, `amount:{total:integer,currency:"CNY"}`. Refund notifications must pair `REFUND.SUCCESS`/`SUCCESS`, `REFUND.CLOSED`/`CLOSED`, or `REFUND.ABNORMAL`/`ABNORMAL`, per [official notification contract](https://pay.weixin.qq.com/doc/v3/merchant/4013071196); payload requires matching merchant identity, known `out_refund_no`, exact `amount.refund`/`amount.total`/currency and any supplied order/transaction/submerchant binding must match intent. Malformed/unsupported/mismatched400; unsigned/untrusted key401; database/settlement/commit failure503. Example illegal plaintext resource receives401 before persistence. Refund API responses, including 404, require signature verification against configured platform public-key ID and raw body; request `Wechatpay-Serial` is the platform public-key ID, not merchant certificate serial. See [official APIv3 signatures](https://pay.weixin.qq.com/doc/v3/merchant/4012365342).

**Response:**204 with no response body ONLY after all order state, settlement, coupon/points and payment_events/refund terminal state commit. 4xx/5xx means not accepted. Valid duplicate business transaction (even different notification ID) acknowledges204 only with prior durable event/terminal evidence. Transaction IDs unique across payment events. Refund number is never parsed to guess another order. Payment status outside pending_pay without durable event fails and requires reconciliation; no fulfillment overwrites cancelled/refunding/refunded states. Scheduler queries pending cash orders and applies the same settlement path. Provider compatibility and real signatures require final controlled external validation; local synthetic RSA/AES is not real WeChat proof.

## Fulfillment and privacy

POST `/order/status` merchant/admin body `{order_number,new_status,reason?}` allows pending_ship->pending_recv only; merchant and admin cannot confirm receipt on the buyer's behalf (pending_recv->completed returns409). POST `/order/confirm-receive` buyer body `{order_number}` only pending_recv->completed. Trusted payment settlement and the existing automatic receipt task retain their separate internal paths. POST `/merchant/ship` belonging merchant/admin body `{order_number,tracking_number?,express_company?,logistics_type?,item_desc?,scan_text?}` only pending_ship->pending_recv; physical logistics requires6..32-char tracking; pickup/virtual may omit. Success `{ok:true,...}` existing fields (`local_updated`, `message`) remain. Each operation locks and checks owner/state on same connection. Refund-first rejects fulfillment; fulfillment-first can still request refund from resulting allowed state. Expiration locks pending_pay before restoring stock and cancelling; auto-receive and shipping status scheduler use conditional transitions and never override refunding.

GET `/order/{user_id}` buyer/admin; GET `/order/detail/{order_number}` buyer/admin; GET `/order/merchant/{merchant_id}` belonging merchant/admin; GET `/merchant/orders` merchant/admin scope derived from identity. No request bodies; existing order projections remain, unauthorized never receives them. All three `/order/export`, `/order/export/by-time`, `/order/export/daily-summary` are platform-admin-only, retain existing XLSX outputs and request schemas. `/merchant/withdraw` global pool operation now admin-only (actual bank payout contract belongs finance). Cart add/decrease/read/remove all require token owner matching user_id; add/decrease quantity strict positive integer. Lists with no data return `[]` (existing paginated merchant/offline envelope retained). Offline list buyer/seller parameter must match identity unless admin; config-check admin-only.

## Durable offline merchant wallet transfer

Existing offline allocation and actual merchant wallet payout policy is preserved, but no network is hidden inside settlement. `payment_merchant_transfers` outbox is committed alongside local assets, uses stable32-character batch/detail number, queries prior unknown attempts before same-number resubmission, and is recovered every minute with >=60-second provider-query spacing. FINISHED is not treated as success when failure counts/amounts disagree. Closed/failed entries require manual reconciliation; never generate replacement money transfers automatically. This is the existing ordinary merchant wallet batch-transfer product, NOT bankcard withdrawal. API compatibility reference: [official batch query contract](https://pay.weixin.qq.com/doc/v3/merchant/4012458877): nested transfer_batch, NOT_FOUND, need_query_detail=false. Provider account/product eligibility remains an external release gate.

## Migration, configuration and rollout

Apply `migrations/005_security_payments.sql` before enabling routes/jobs: three InnoDB tables, unique logical refund identity, globally unique out_refund_no, unique payment transaction and durable transfer batch. No runtime DDL; no production data mutations performed. Existing in-flight legacy refunds require provider reconciliation before migration into intents; do not auto-resubmit old refunds. Forward rollback must drain/reconcile intents/outbox first; dropping tables loses money-recovery evidence and is not a rollback procedure.

Use existing WECHAT_PAY_MCH_ID, SP_MCH_ID if enabled, WECHAT_APP_ID, API_V3_KEY, PUB_KEY_ID, private/public key paths and public_base_url. Secrets provisioned by authorized operator, never in docs/tests. Background tasks controlled by main lifecycle ENABLE_BACKGROUND_TASKS; route registration starts no threads. Client rollout must add Bearer on all protected aliases, keep discount selections fixed after creation and retain refund identity after503. Test deployments must block external network; no production SMS/payment/refund/transfer calls are authorized.

## Proposed verification (not executed by implementer)

Coordinator: `uv run pytest tests/test_security_payments.py tests/isolation/test_payment_business.py tests/isolation/test_order_state_race.py tests/isolation/test_order_transitions.py` inside guarded isolated MySQL/Redis harness. Include unauthorized alias matrix, real DB callback commit fault and signed replay, last-stock contention, both refund/ship/receive orderings, expiration versus payment, partial cumulative refunds and durable transfer query recovery. Controlled provider boundary tests demonstrate local recovery only; real payment/refund/transfer remains unverified until authorized dedicated merchant validation.

## Reconciliation quarantine and operator tool (additional change)

GET `/refund/reconciliation/cases` requires platform-admin Bearer; no query parameters or body; 200 JSON `{items:[]}` when empty, at most 200 oldest open cases. Non-admin 403, anonymous 401. Response `items` contains `case_key`, `order_number`, nullable `transaction_id`, nullable `total_fee` in cents, nullable `refund_no`, `reason`, `state` and `created_at`. Reasons include `paid_after_local_close`, `legacy_refund_missing_intent`, `referral_legacy_precision`, `referral_insufficient_balance`, `referral_record_mismatch` and `member_reward_shortfall`. Referral case keys identify original grant flows; `member-reward:<order_id>` identifies `order_member_reward_refunds`, which stores the original, recovered and shortfall amounts. Follow the authorized operator procedure in `security-finance.md`. These cases do not mean the provider refund failed; no automatic case-closing API is provided.

Minute recovery now also queries offline pending cash orders and cancelled online cash orders with no reconciliation case. Verified payment after cancellation creates a durable open case and error event before rejecting the callback (no success ACK, fulfillment, stock consumption or unearned asset grant). Legacy refund_no missing an intent is explicitly quarantined automatically, retaining trusted original order/payment/refund identity. Operator must inspect provider transaction/refund state and apply an approved reconciliation procedure: original full/partial legacy request amount is not recoverable safely from order total alone, and expired-order restocked inventory cannot be resold silently. This is a business/operational blocking prerequisite, not fabricated automatic recovery or permission to send a new refund. Cases have no TTL deletion.

Existing offline merchant template notification is delivered separately after the durable wallet transfer reaches proven successful FINISHED. `notification_sent` persists local delivery bookkeeping; provider template delivery has no exactly-once primitive, so a crash after provider accept and before bookkeeping may duplicate the informational message, never money. Failed messages remain recoverable; finished money transfers remain terminal during message retries.

## Reviewed recovery corrections

Offline first prepayment now persists `security_prepay_frozen=1` even for full-price/no-coupon orders; later discount changes reject before provider I/O. Payment query consumers rotate primary-key cursors and wrap at end so old unpaid/not-found rows cannot permanently starve later online/offline orders. Direct refund reasons (max512 characters) are persisted on the intent; a retry of an original direct refund_request_id resolves its original intent even if a historical application exists. Existing legacy refund_no without intent is quarantined before any new number can be created.

Merchant-wallet payout claim and refund reservation both lock the same order first. A still-prepared/unattempted payout is held while refund is pending. An already attempted/unknown/completed payout blocks new refund submission409 until operator reconciles merchant funds; no automatic bankcard/wallet clawback product is invented. Template API HTTP200 is accepted only when JSON errcode=0; rejected informational deliveries remain retryable.

Additional authored isolated scenarios: `tests/isolation/test_order_overlap.py` duplicate ship/receipt competition and pay/expiry both lock orderings; `tests/isolation/test_offline_callbacks.py` signed offline callback settlement/final-commit failure, complete rollback and replay with exact10-point grant, single event/outbox; transition tests direct-key replay after new application and prepared-vs-attempted payout/refund outcomes. All are unexecuted by implementer.

Payout reservation correction: pending refund holds (not permanently closes) an unattempted payout. Partial SUCCESS reopens only the remaining original payout using cumulative refunded-cash proportion, rounded to integer cents; CLOSED restores the held eligible amount unchanged. Full refund closes zero remainder. Stable batch identity is safe to reuse because attempts remain zero; once attempted, operator reconciliation is required. Migration retains original_amount to prevent repeated partial-rounding drift.

## Payment settlement deadlock recovery

For an already verified cash payment notification or scheduler query result, the online and offline settlement handlers retry the complete local database transaction at most three attempts, with the same order number, transaction ID and amount, only for an unwrapped PyMySQL `OperationalError` 1213 raised during transaction work. Each attempt exits the connection context and rolls back before the next one; it rechecks the order, payment identity and durable event. Provider queries, payment creation, wallet transfers and messages are not replayed by this retry. Offline merchant transfers remain an outbox entry committed with the settlement, not a network call within it.

Business rejections, 1205 lock timeouts, connection failures and commit errors (including uncertain commit outcomes) are not retried by this mechanism. Exhaustion propagates the original database error; the callback retains its existing failure/503 response rather than acknowledging an uncommitted payment. Callback redelivery and scheduler recovery depend on the external provider and enabled jobs; neither their timing nor successful external delivery is guaranteed. This bounded retry does not eliminate deadlocks, correct lock ordering or guarantee progress.

Zero-cash order creation/settlement is outside these callback handlers and is not covered by this retry; it has no cash-payment callback to redeliver. The separately authored isolated MySQL tests inject errors after local writes to exercise rollback, bounded replay, identity, asset/event uniqueness and error propagation; they do not establish real provider behavior or prove that concurrent deadlocks cannot occur.
