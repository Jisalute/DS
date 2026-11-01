# FIN-001 / FIN-002 财务安全契约

## 变更记录与迁移

2026-10-25：所有注册财务数据路由（包括原无鉴权报表、配置读写、提现审核和最后注册的商户提现路由）统一要求有效 Bearer 会话。仅原非敏感 `GET /` 状态接口保持公开，精确限定方法、路径和处理函数；不扩大到任何财务数据接口，也不改变独立 `/health`、`/ready` 路由。旧 `key` / `admin_key` 不授予权限；不保留财务数据匿名或共享密钥兼容路径。管理员由 `core.auth.is_platform_admin` 判定，不接受客户端角色。客户端必须同批迁移：401 清除会话并重新登录，403 显示无权限，不自动循环登录。

数据库先执行 `migrations/006_security_finance.sql`。新表 `finance_requests` 保存可信操作者、区分大小写的请求 key、操作、规范参数摘要、原始结果；`finance_events` 保存结算/退款/折扣事件。两表与余额、券、账本使用同一个 InnoDB 事务，无运行时 DDL。不得对尚未对账事件做 TTL 删除。回滚应用前保留这些持久身份表；不得删除后重放旧业务。

## 所有路由通用认证和数据作用域

**基本信息**：沿用注册的完整 URL（无 `/finance` 前缀）、HTTP method、原请求和成功响应结构；Content-Type / Accept 为 `application/json`；所有成功响应 HTTP200。下表列出的个人接口可以由本人或平台管理员访问；其余所有财务数据路由，包括所有 `/api/admin/*`、平台/公益资金池查询、全用户预览、提现查询/列表/指南、配置、清池和管理写入，均只允许平台管理员。未知/后续新增财务路由默认管理员，不能自动继承个人作用域。唯一公开财务路由 `GET /` 无请求体、无参数，返回 `{"message":"财务管理系统API运行中","version":"3.2.0"}`，字段均为固定字符串，不含财务或用户信息；其他方法/路径无此豁免。

| 本人接口（GET，除注明者） | 请求作用域和业务 |
|---|---|
| `/api/coupons/expiring` | 忽略外部目标，以会话 id 查询；days 默认3，1..30 |
| `/api/coupons/use`（POST） | coupon_id、user_id 正整数；order_type 可选 normal/member；用户必须本人，且券归属/有效期/未使用满足条件 |
| `/api/donate/true-total-points`（POST） | user_id 正整数且本人；amount 正数；锁用户余额，1:1捐赠，用户支出与公益池收入同事务 |
| `/api/rewards/referral`, `/api/rewards/flow` | user_id 可选；省略强制本人，不代表全部用户 |
| `/api/reports/points/subsidy`, `/api/reports/points/unilevel`, `/api/reports/points/referral-team`, `/api/reports/points/all`, `/api/reports/points-flow` | user_id 可选，省略强制本人 |
| `/api/reports/order-points` | user_id 可选，省略强制本人；order_no 只是额外过滤，不能扩大作用域；start_date/end_date yyyy-MM-dd 必填 |
| `/api/reports/subsidy/weekly`, `/api/reports/subsidy/monthly` | user_id 可选，省略强制本人；year>=2024，week1..53 或 month1..12 |
| `/api/reports/points/member/weekly`, `/api/reports/points/member/monthly` | 同上，会员积分个人报表 |
| `/api/reports/points/merchant/weekly`, `/api/reports/points/merchant/monthly` | 同上，只能看到本账号商家积分，不能替换商户用户 id |
| `/api/reports/unilevel/points-flow`, `/api/reports/withdrawal`, `/api/reports/points/all-flows`, `/api/reports/points/member/detail` | user_id 可选，省略强制本人；其余原日期/分页/类型过滤不能扩大作用域 |

管理员可明确传 user_id 查询目标，或留空查询全用户；普通用户传他人 user_id 在执行服务前 HTTP403。`/api/reports/platform/flow-summary` 和 `/api/reports/points/all-detail` 含平台级信息，始终管理员，不因传本人 user_id 放行。所有财务读写使用权威会话认证，撤销令牌同样拒绝。
所有个人报表汇总和记录使用同一用户过滤；周/月补贴、会员积分、商家积分和联创点数汇总不得去掉 user_id。周补贴 data.summary.company_points_balance 对指定用户作用域返回 null，记录若为平台积分来源，其 points_source.current_balance 同样为 null；只有管理员未指定用户的全局报告才返回平台池余额。个人订单积分报告的 platform_points_included 为0，不读取全局池补入个人合计。客户端应把 null 显示为无权限/不适用，不能解释为实际零余额。

**请求参数**：Authorization header 必填，字符串 `Bearer <token>`；无默认值；必须为未撤销有效会话。个人接口 user_id query 是正整数，可选（use/donate 必填）；默认本人，仅管理员能扩大作用域。页码 page>=1；原各接口 page_size 上限仍保留。报表 GET 无请求体。

**请求体示例**：`GET /api/reports/points/subsidy`，Headers `Authorization: Bearer <token>`、`Accept: application/json`，无请求体。非法示例：本人 token 请求 `GET /api/reports/points/subsidy?user_id=<其他用户>` 返回403，即使同时提供 `admin_key`。

**返回参数**：ResponseModel 的 success:boolean 必返（是否成功）、message:string 必返（描述）、data:接口原业务对象/可空。个人报表数据保持原字段，不引入新的分页包装；其嵌套记录只能来自经过作用域过滤的数据。无记录返回原空 users/records/flows 数组和零汇总，不用他人数据补空结果。分页接口沿用 data.pagination.page/page_size/total/total_pages 或原 page/page_size/total/records 结构，不新增 `has_more`。

**返回示例**：`{"success":true,"message":"查询成功","data":{"summary":{"total_users":0,"report_type":"subsidy_points"},"users":[]}}`。

**使用方法与业务规则**：登录后调用；个人查询无需先调用管理员接口。GET 无资金副作用。券使用锁券行，竞争请求只能一个从 unused 变为 used；日期超出有效期拒绝。捐赠用户余额锁在整个扣款/收入/双流水事务内，竞争不能丢失余额。提现审核的 auditor 原客户端字段不再作为可信操作者，实际审计记录使用管理员会话 id，审核状态和流水同事务。原业务分配比例、会员政策、雨点月330/每日30规则不变。

**错误**：401 无效/过期/撤销会话（重新登录）；403 非管理员或跨用户（不要换 key 重试）；422 参数缺失/格式不合法；400 余额不足、券不可用等业务冲突；500 数据库/内部失败，整笔回滚。标准错误体 `{"detail":"只能访问本人财务数据"}`。无独立业务错误码字段。

## 持久发券接口

**基本信息**：以下均 POST、平台管理员、HTTP200 成功；JSON响应。发行请求新增必填 `Idempotency-Key` header，1..128 个 ASCII 可打印非空白字符，区分大小写。身份为 `(会话管理员id,key)`，跨操作复用 key 也会冲突；网络结果未知时只能用原 key/原参数重试。无自动过期。

| URL | 参数位置、类型、必填、默认、范围及意义 |
|---|---|
| `/api/coupons/distribute` | query user_id:int 必填>0；amount:number 必填且为正整数张数（每张1元，非整数不再截断）；coupon_type:string 默认user，user/merchant；applicable_product_type:string 默认all，all/normal_only/member_only；valid_days:int 默认COUPON_VALID_DAYS，0..365 |
| `/api/coupons/distribute-batch` | JSON user_ids:int[] 必填非空目标；amount:number 必填>0 为单张券面额；coupon_type、applicable_product_type同上（默认user/all） |
| `/api/coupons/exchange` | query count:int 可选>0，每用户最大张数；省略沿用系统配置，原每用户1000上限保留 |
| `/api/fund-pools/transform-to-coupon` | query pool_type:string 必填，public_welfare/maintain_pool/shop_pool/city_pool/branch_pool/fund_pool；user_id:int 必填>0；amount:number 必填正整数元；coupon_type、applicable_product_type同上；remark:string 可选操作备注，默认null |

**请求体示例**：`POST /api/coupons/distribute?user_id=123&amount=2&coupon_type=user&applicable_product_type=all&valid_days=30`，Headers `Authorization: Bearer <admin-token>`、`Idempotency-Key: issue-20261025-123-01`，无请求体。批量请求 `POST /api/coupons/distribute-batch` 同样 headers，加 `Content-Type: application/json`，Body `{"user_ids":[123,124],"amount":1,"coupon_type":"user","applicable_product_type":"all"}`。非法示例：amount=1.5 的单人发行422；缺 key422；普通用户即使提供 key403。

**返回参数**：外层 success:boolean、message:string、data:object 必返。单人发行 data.coupon_ids:int[] 为持久化券 id；count:int 券数；amount_per_coupon:number 固定1；total_amount:number 总面额；applicable_product_type:string 适用范围；valid_days:int 天数。转正同结构但无 applicable_product_type/valid_days。批量 data.success_count:int 成功券数、failed_users:object[] 每项 user_id:int/reason:string、coupon_ids:int[]；交换 data.success_count:int 成功人数、total_users:int 锁定候选人数、failed_users:array（事务成功时空）、total_coupons_issued:int 总券数、message:string 摘要。

**返回示例**：`{"success":true,"message":"已发放 2 张 1 元优惠券","data":{"coupon_ids":[41,42],"count":2,"amount_per_coupon":1.0,"total_amount":2.0,"applicable_product_type":"all","valid_days":30}}`。批量全失败允许 `{"success":true,"message":"成功发放 0 张优惠券","data":{"success_count":0,"failed_users":[{"user_id":123,"reason":"余额不足"}],"coupon_ids":[]}}`。

**使用方法与业务规则**：单人发行按张1:1扣 true_total_points，整个请求不足则无券/无流水/无成功幂等记录；锁用户行后读取权威余额。批量保留原业务允许的逐目标业务失败语义，但每个失败目标先回滚 SAVEPOINT，绝不提交该目标半成品；数据库错误终止并回滚全部。全平台兑换一个事务按用户id顺序锁定，避免过去分批独立提交造成中途失败后重试重复发放。转正锁目标/池余额，池支出、券、日志和请求结果一起提交。原 key 重放返回原券 id，不重新扣款。没有记录时返回空券数组，不伪造券。池转正不扣目标雨点，政策不变。

**错误**：401/403如前；422缺 key/非法参数；409 `{"detail":"Idempotency-Key 已用于不同请求"}`（必须修正调用方，不能自动换 key掩盖冲突）；400余额不足或目标不存在；500数据库失败。未知响应先以原身份重试；失败事务不遗留可见成功记录。

## 支付/退款事务内部契约（非 HTTP）

- `FinanceService.settle_order(order_no,user_id,order_id,points_to_use=0,coupon_discount=0,external_conn=conn)`：在订单行锁后，以 finance_events settlement/order_no 判重；所有资产、账本、优惠券核销和可信支付状态迁移在传入连接完成。绝不提交或关闭传入连接；异常必须传播。无外部连接时自己提交。PaymentOwner 只在可信支付/零元结算路径调用。
- `reverse_split_on_refund(order_number,external_conn=conn,event_key=out_refund_no,refund_amount=Decimal(refund_fee)/100)`：订单锁和单笔稳定退款号去重；同号不同金额拒绝。按累计现金退款/订单实付比例回冲原实际账本，累计目标四位小数减已回冲值避免末笔舍入累积；覆盖原两种精确订单 token 备注，不能匹配同前缀其他订单。与退款终态同事务，不独立 commit。累计退款不得超过实付。退款账本可追溯原订单及 finance_events 中退款号/各账户金额。
- `FinanceService.revoke_order_discounts(order_number,external_cur=cur)`：只在完整订单状态 refunded 时退回积分/有效券并按原余额上限收回奖励；finance_events discount_refund/order_number 去重。部分退款不提前全退折扣；调用方在累计全额退款终态执行。所有错误导致外层事务回滚，事件标记不能先于资产提交。
- 消费赠送会员积分回收：累计全额退款时从原订单 `points_log` 的已知赠送记录求和，先恢复订单使用的积分，再按现有余额上限收回；`order_member_reward_refunds` 按订单唯一保存原赠送额、实收额、未追回差额、退款号和回收流水 ID，均与退款终态同事务。差额大于零创建 `member-reward:<order_id>` / `member_reward_shortfall` 人工对账 case，不制造负余额、不撤销其他交易、不拒绝已成功的现金退款；差额处置与结案仍需批准后的人工流程。原流水条件包括历史线下 `related_order IS NULL` 且原因精确匹配订单号的记录，不能按当前奖励配置重算。
- **并发阻断项（未修复）**：退款资金池回冲按账户名顺序取得池锁，而线上/线下支付结算可先持有平台池再获取子池；不同订单间可形成 `branch_pool → platform_revenue_pool` 与 `platform_revenue_pool → branch_pool` 锁环。线上积分抵扣还可能先锁公司积分池，日补贴可先锁用户再锁补贴池；零元订单/线下回调先锁券，集成 008 用户资产镜像触发器还取得 `user_assets` 行锁。不得将单纯调整退款排序、迟取平台门闩或重试宣称为全局锁序修复。需要跨全部相关入口与触发器设计统一顺序，隔离 MySQL 中用真实独立连接、确定性交错测试两种先后及余额/事务完整性；在此之前不得将支付退款并发安全标记完成。
- 退款结果本地落账的有限死锁恢复：`RefundService.apply_result` 在完整事务上下文回滚退出后，只对提交开始前明确的 `pymysql.err.OperationalError(1213, ...)` 最多执行三次完整事务尝试；每次重新读取并校验原退款号及可信结果，重新计算本地资产、事件和人工 case。外部 refund/query 不在重试范围。1205、连接错误、提交开始后的任何异常和其他错误不重试；重试耗尽继续抛出，沿用原失败、不确认成功和原退款号恢复路径。有限重试不消除锁环、不保证活性，本地回滚也不代表外部退款失败。此前为 `change` 的意图失败后仍为 `change`，分钟调度不选该状态，需原退款号人工同步查询或后续可信回调，不能宣称全自动恢复。隔离测试使用真实 MySQL 和注入错误，不能证明真实微信重投、查询或到账；供应商行为未经验证。
- 日补贴事务故障处理：普通用户处理后，平台用户26分支的数据库异常和业务异常必须向事务外传播，使 `get_conn()` 回滚全部写入；外层返回失败，不执行成功后的自动清除配置。补贴分母中的公司积分池查询发生异常时也返回失败，不再把异常当作余额为零。此更改不调整补贴比例或其他业务政策；MySQL 1205/1213 实际回滚行为须在隔离数据库故障注入验收。
- 分账配置安全边界：SQL 查询失败、无效 JSON 结构、非法或合计超限的比例不得回退为默认比例并继续结算；配置列为 SQL NULL 或字段缺失仍按原默认值。更新分配比例使用独立物理连接和按库名派生的 MySQL 命名锁，串行化参与该协议的配置写入者；读取、合并、校验后只更新本次目标池，事务提交或回滚后释放锁并关闭连接。补贴和董事手动配置只修改自己的 JSON 键，初始化只补缺失的 allocation，不覆盖已有配置。缺少账户或现有配置损坏时拒绝，先由授权人员核实并修复，不静默覆盖。命名锁仅协调同一 MySQL 实例且遵循协议的写入者；多字段更新与资金事务仍可能产生行锁死锁。提交结果或释放锁结果未知时先查询配置，不盲目重放。分配比例与平台计提比例仍由独立连接分别读取，并发变更时可能来自不同时点，尚无一致快照保证。
- 推荐奖励退款回收（2026-11-01）：上线前执行 `migrations/012_referral_reward_grants.sql`，必须先于新代码及其结算/退款作业部署。首购会员、线上普通商品及线下普通商品推荐奖励授予，在订单结算事务内同时写旧 `account_flow` 和新 `referral_reward_grants`；以原发放流水 ID 为主键，保存订单内部 ID、原受益人以及两个余额实际增加的六位精度数额。两个余额增量不同或用户不存在则整个授予事务失败，不得自行重算比例或补录猜测值。
- 仅累计全额退款成功时，在退款终态事务按订单锁、再按受益人 ID/流水 ID 顺序锁行，使用 `finance_events` 的 `(referral_refund, order_number)` 唯一事件和授予状态去重。精确记录必须与原流水的订单备注、受益人、类型和将授予金额按数据库四位规则舍入后的金额一致；原受益人两项余额均足额时扣除实际授予额。`referral_reward_reversals` 以原流水 ID 唯一保存退款号、六位负向变动和两项余额的六位变动前后值；兼容性 `account_flow` 两条流水只有四位小数，不可用来替代该精确账本。不看当前推荐关系或当前奖励配置。部分退款不提前回收，部分回收的业务政策未定。
- 已消耗、余额不足、原始流水与精确授予记录不一致时，不扣负、不截断、不撤销别的交易；原授予记录转 `manual` 并创建逐流水开放对账 case。历史奖励的 `account_flow.change_amount` 只有四位小数，不能还原曾入账的六位余额：没有新表关联的旧流水（包括旧版 `推荐奖励自动发放（订单#<id>）` 备注）创建 `referral_legacy_precision` 对账 case，不自动回收。即使需人工处理，已由微信确认的退款成功事实、退款订单终态、回收结果和对账 case 仍须在同一事务持久化；不能因为权益不足拒绝依法应退货款。数据库事务失败仍不确认本地成功，由原退款号恢复重试。
- 对账：管理员通过 `/refund/reconciliation/cases` 发现待处理事项，`referral-grant:<flow_id>` 对应 `referral_reward_grants` 和 `account_flow.id`，`referral-legacy:<flow_id>` 对应旧 `account_flow.id`；精确成功回收记录对应 `referral_reward_reversals.flow_id`。`referral_insufficient_balance` 是精确授予无法足额扣回，`referral_record_mismatch` 是记录不一致，`referral_legacy_precision` 是旧流水缺少六位证明。按已批准售后规则与原支付、退款、流水、实际余额和外部凭证核实处理，未实现自动消债、解封或结案 API。不要因 case 创建而向客户重复退款。
- 报表变更：`GET /api/reports/points/all-flows` 保持 Bearer、本人/管理员作用域及原 `user_id`、`start_date`、`end_date`、`page`、`page_size` 参数和响应包络。新退款追回的两条 `account_flow` 明细以 `referral_reward_reversals.referral_flow_id/available_flow_id` 唯一关联精确账本，`records[].change_amount` 和 `records[].balance_after` 为六位精度数值，`flow_type` 为“退款收回推荐奖励”、`flow_category` 为“支出”；`summary.total_expense`、`summary.net_flow` 和对应 breakdown 采用同样精确金额。两条记录分别代表推荐累计和可用雨点的变动，原报表总计按两条记录各计一次，并非两次现金退款。未关联的历史流水保持原四位值，不根据备注猜测。`GET /api/reports/points/all` 的可用雨点 `total_deducted` 同样使用关联的精确变动；字段仍为原字符串格式。例：扣回 0.000001 时相关明细 `{"flow_type":"退款收回推荐奖励","flow_category":"支出","change_amount":-0.000001,"balance_after":0.123456}`。这项修复不追溯改变旧四位流水，也不保证旧授权发放流水具有六位精度。普通用户跨账号请求仍403，匿名401，分页非法422；无数据维持原空记录结构。
- 授予侧精度修正：综合流水 `/api/reports/points/all-flows` 的推荐收入明细和收入/分类汇总、`/api/reports/points/all` 和 `/api/reports/points/referral-team` 收入汇总，以及 `/api/rewards/referral`、`/api/rewards/flow`、管理员 `/api/rewards/pending` 的推荐发放明细，以 `referral_reward_grants.flow_id = account_flow.id` 精确关联并使用授权表六位 `amount`；无授权表记录的历史流水保留原四位金额，团队奖励不变。例：原账户流水金额为 `0.1235`、对应授权 `0.123456` 时，推荐收入及 `points_issued` 为 `0.123456`，全额追回后推荐分类净变动为零。各接口原数值/字符串类型、认证、过滤、分页和响应结构不变；原发放流水的 `balance_after` 没有六位快照，仍是旧四位数据，不能据此推算精确历史余额。
- 迁移不回填旧四位流水。回滚部署前必须暂停/排空支付结算与退款恢复并保存、核对新表及事件/对账证据；保留表不等于旧代码能继续安全处理新增退款，旧代码无此追偿保护，不应在未制定安全前向修复和经批准人工流程前恢复流量。不得删表后重放授予或退款。
- 旧 `FinanceService.refund_order` 自主改状态/独立回冲入口删除，所有退款调用方必须迁往 PaymentOwner 持久意图流程。

## 商户银行卡提现：明确未接入边界

`POST /api/withdraw/merchant-to-bankcard` 仍管理员、使用原 JSON schema（out_request_no/amount/account_type/bank_memo/remark/notify_url）；不新增路由或伪造成功。**返回 HTTP503** `{"detail":"商户提现供应商尚未接入，未创建申请或扣款"}`，无成功返回体、无记录和资产写入。原查询 `/api/withdraw/merchant-to-bankcard/query?out_request_no=...`、列表和历史记录保留，均管理员；历史 INIT 不代表微信受理。实际提款适配器、商户提款产品权限/凭据、结果查询/回调对账未具备，功能不能宣称实现或启用。BankcardService 现有结算卡绑定能力不等于提款能力。

## 验收和外部证明边界

新增 `tests/test_security_finance.py` 在 guard 认可的真实隔离 MySQL 测试持久重放/异参冲突、单人多张不足整笔回滚、事件与资产共同回滚；`tests/isolation/test_finance_business.py` 由隔离 harness 覆盖实际 HTTP 并发余额竞争/重放。执行命令：隔离环境初始化迁移后 `uv run pytest tests/test_security_finance.py tests/isolation/test_finance_business.py -q`。本实现阶段未运行测试、构建、lint或格式化；这些不是已通过声明。真实微信/短信/提款供应商证明不由合成数据或受控适配器替代。

## Issue #227：实际锁图与隔离复现交接（2026-11-02，未修复）

本节是候选工作树的静态调用链调查、协调者报告的抽象 InnoDB 反序模型实验及**尚未执行**的真实服务复现方案，不是并发安全验收。未修改财务实现、迁移、业务比例、月结/日结开关或退款政策；不照搬 PR #228 的回收逻辑。不能关闭 #227，不能据此部署。HTTP 请求、响应、错误及幂等契约均维持上文。

### 实际入口与持锁链

下列箭头表示同一事务取得/保留锁后继续取得另一资源，不表示单条 SQL 的执行计划保证。`F(x)` 为 `finance_accounts.account_type=x`，`U(x)` 为 users 主键，`A(x)` 为 user_assets 主键，`C(x)` 为旧 coupons 主键。订单锁、事件唯一键、外键父行共享锁同样必须计入，不能只搜索 `FOR UPDATE`。

| 真实入口及调用链 | 当前锁行为与定位 |
|---|---|
| `api/order/order.py:OrderManager.create` → 零元 `FinanceService.settle_order` | 创建事务先 `U(买家)`，再购物车/SKU、按 ID 的券锁，插入订单/明细后在**同一连接**结算；不能在 settle_order 内才取得全局门闩。非零创建同样已经取得用户/SKU/券锁。 |
| `api/wechat_pay/routes.py:_online_pay_success_once` → `_settle_order_internal` | 订单 → settlement 事件 → `U(买家)`；会员/普通商品赠送更新触发 `A(买家)`；会员首购奖励可能先取得推荐人/团队成员行；分账 `F(platform_revenue_pool)` → 配置迭代顺序的子池，普通推荐奖励在 fund_pool 分支取得推荐人/镜像，最后 `F(company_points)`，并核销券。入口的 1213 重试不是锁序。 |
| `OfflineService.unified_order` 零元分支；`_offline_pay_success_once` 可信回调 → `OfflineService.on_paid` | offline_order → 旧券条件 UPDATE → on_paid 插入平台订单 → `F(platform_revenue_pool)` → 各子池/推荐人 → 买家积分及镜像 → `F(company_points)`。订单 INSERT 的 users 外键检查还可能提前取得买家父行共享锁，不能把第一条 users UPDATE 当作第一次用户锁。 |
| `RefundService._apply_result_once` → `reverse_split_on_refund` | orders → refund intent → refund_split 事件/同单既往事件 → 以 account_type 排序的真实原分账账户；可出现 `F(branch_pool)` → `F(company_points)` → `U(平台商户)` → `F(platform_revenue_pool)`。仅末笔全退再处理授予记录/原受益人、订单折扣/买家/券，最后同步 offline_order。不能单改退款排序。 |
| `split_order_funds` → `_execute_split`（可独立调用的旧服务入口） | orders/legacy_split 事件 → `U(1)` → `F(platform_revenue_pool)` → 配置子池。当前仓库未检出生产调用方，但公开函数仍可执行，不能当作已不存在。 |
| `/api/subsidy/distribute` → `distribute_daily_subsidy`；调度器同名方法 | 写事务遍历用户，更新 `U(x)` → 触发 `A(x)` → `F(subsidy_pool)`，后续再取其他用户；平台26分支还取 `U(26)`、`F(company_points)`。配置及分母的前置读使用其他连接，不替代写事务锁。调度器注释注明停用不等于管理员入口不可达。 |
| `settle_expired_unused_coupons`（scheduler 调用） | 一个事务跨全部候选券：`C(x)` → `F(director_pool 或 subsidy_pool)` → `U(持有人)` → `A(持有人)`，再处理下一张券。 |
| `settle_monthly_rain_points`、单人/批量发券、全平台换券 | 多次用户/券写入；批量请求跨多个目标持锁。不能仅规范池顺序而遗漏多用户顺序。保持现有业务启停，不据此启用月结。 |
| `transform_pool_to_coupon`、`donate_true_total_points`、`distribute_unilevel_dividend` | 池转券 users → 池 → 券；捐赠 users/镜像 → public_welfare；联创分红多个 users/镜像 → director_pool。它们与支付/退款共享资源，属于完整切换范围。 |
| `RightsService`、`SettlementService` 的权益写入 | 就绪状态共享锁/用户表元数据锁 →（需时）system_pools → users → user_assets → 权益券批次/流水。雨点生成不取 system_pools。旧 coupons 与 rights_coupon_batches 不是同一张表。 |

触发器：008 的 `trg_users_assets_mirror_update` 仅在 member_points/true_total_points 有实际变化时沿 `U(x) → A(x)` 更新镜像；非资产用户资料 UPDATE 不必取 A。insert 触发器在 users INSERT 后 `INSERT IGNORE user_assets`，仍有唯一键检查/行锁，不能遗漏。007 的 rights_ledger 两个触发器直接拒绝 UPDATE/DELETE，system_pools 触发器保持发行总量，没有额外跨表锁。`database_setup.py` 安装的 orders→users、order_items→orders/products、cart→users/products 及 SKU 外键也可能产生父行锁；实际隔离库须以 `SHOW CREATE TABLE` 和 `INFORMATION_SCHEMA.KEY_COLUMN_USAGE` 核对，不能假设历史库 DDL 与源码一致。

### 有源码证据的锁环

1. 不同订单支付/退款：支付持有 `F(platform_revenue_pool)` 等待 `F(branch_pool)`，退款持有 branch 等待 platform。退款排序中间即便还经过 company/merchant，也不消除环。
2. 支付/补贴：线上普通商品结算已持有买家 U/A，持有 platform 后仍需 subsidy；补贴可先持有另一用户 U/A 和 subsidy，再遍历到该买家等待其 U。仅将补贴的首次池锁前移，不处理所有多用户入口，仍不能证明无环。
3. 支付/旧券过期：支付已持有 U、后续核销 C；过期持有 C 后再取池/U。零元/线下路径原本先 C 后池/U，和线上 users 先行路径不一致。池转券、捐赠、联创分红让局部池排序还会与 users 顺序相交。
4. 资产镜像：任何持有 A 再等待 U 的新路径都会与008的 U→A反向；当前检查的权益方法已主动先锁 U。这个约束是必须保留的正确实现，不应为简化财务门闩而删除。

### 已执行的抽象行锁模型（协调者报告，非真实交易复现）

2026-11-02，协调者 Main 报告：在本轮自建内部网络的 MySQL 8.0.46 临时库 `dsiso_locksmoke`，使用两条独立连接执行 `platform → branch` 与 `branch → platform` 的行锁模型；两端各持首锁后，通过 barrier 同步请求第二把锁。连接3994收到 MySQL 1213，连接3993取得第二锁；两端均已回滚，临时库已 DROP。本章节编辑者未重跑该实验。

该结果仅证明此次抽象 InnoDB 反序模型可以死锁，**没有调用真实支付/退款服务，没有证明真实交易链已复现，更没有证明锁环已消除**。barrier 同步也不等于下述观察连接确认等待边的三连接方案已执行。真实服务、触发器/外键、金额守恒与事务完整性回归仍未执行；下述方案仍标记未运行，#227保持未修复。

### 真实 MySQL 独立连接、确定性交错复现方案（未运行）

仅在 `scripts.security_isolation.safety.guard()` 认可的可销毁本地隔离库操作，不连接生产。准备两条独立物理连接 T1/T2，另用观察连接 O；记录 `SELECT CONNECTION_ID(),DATABASE(),@@transaction_isolation`，确认三者 ID 不同及两个池行均已初始化。以下 SQL 使用 `SELECT ... FOR UPDATE` 取得真实 InnoDB 行锁，不改余额，结束显式 ROLLBACK。它只复现上述真实业务锁边，**不是调用实际支付/退款服务的回归测试**。

```sql
-- T1：支付的池顺序前半段（保持事务，不提交）
START TRANSACTION;
SELECT account_type FROM finance_accounts
 WHERE account_type='platform_revenue_pool' FOR UPDATE;

-- T2：退款的池顺序前半段（保持事务，不提交）
START TRANSACTION;
SELECT account_type FROM finance_accounts
 WHERE account_type='branch_pool' FOR UPDATE;

-- T1：另一个终端执行；该语句应进入等待
SELECT account_type FROM finance_accounts
 WHERE account_type='branch_pool' FOR UPDATE;

-- O：MySQL 8，在看到 T1→T2 确切等待关系后，才执行 T2 下一步。
-- 不用 sleep 或“线程已启动”代替数据库等待证据。
SELECT rt.PROCESSLIST_ID AS waiting_connection,
       bt.PROCESSLIST_ID AS blocking_connection,
       rl.OBJECT_NAME, rl.INDEX_NAME, rl.LOCK_DATA
 FROM performance_schema.data_lock_waits w
 JOIN performance_schema.threads rt ON rt.THREAD_ID=w.REQUESTING_THREAD_ID
 JOIN performance_schema.threads bt ON bt.THREAD_ID=w.BLOCKING_THREAD_ID
 JOIN performance_schema.data_locks rl ON rl.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID
                                     AND rl.ENGINE=w.ENGINE;

-- T2：确认 O 的等待边后，形成闭环；MySQL 自行选择牺牲事务。
SELECT account_type FROM finance_accounts
 WHERE account_type='platform_revenue_pool' FOR UPDATE;
-- 两个终端都执行，即使其中一个已被回滚。
ROLLBACK;
```

观察权限不足或非 MySQL 8 时本方案不能给出确定性交错证明，不以定时等待替代；由隔离管理员提供受限观察权限，禁止向生产应用账号扩权。第二次将 T1/T2 的第一阶段加锁及第二阶段请求的先后完全交换；记录具体阻塞边和两端结果，不假定哪条连接收到1213。若禁用了 InnoDB deadlock detector，可能只有1205超时，需记录配置，不能声称捕获1213。保留受限访问的锁图/结果，不能只保存“没有异常”。

其他锁环的复现应包装**真实服务游标**，在已成功取得指定行锁后用事件屏障暂停；另一连接进入目标请求后由 O 确认真实 wait 边再释放屏障，不能用模拟游标或把 OperationalError 注入当作死锁复现。资产镜像场景必须安装并核实008触发器，观察等待对象确为 user_assets；仅测 users 竞争不能覆盖触发器。失败时捕获每个线程异常并在 finally 释放屏障、回滚/关闭全部连接，避免遗留连接或等待事务。

### 完整修复必须覆盖的切换与验收范围

- 在**首个相关资源锁之前**统一协议：至少覆盖在线创建（含非零）、线上可信支付、线下预支付/零元/回调、独立 on_paid、退款终态及其独立回冲/折扣入口、旧分账、补贴、旧券过期、月结、单/批量发券、全平台兑换、池转券、捐赠、联创分红、分配配置/多池清理和实际初始化写入者。涉及的候选文件为 `api/order/order.py`、`api/wechat_pay/routes.py`、`services/offline_service.py`、`services/refund_service.py`、`services/finance_service.py`；权益/桥接、购物车/SKU及外键路径也须证明没有逆边。只在 `_add_pool_balance` 加锁为时已晚。
- 协议必须明确订单/事件、finance_accounts、system_pools、全部受益人 users、user_assets、旧券/权益批次及外键父行的顺序；奖励遍历有多层动态受益人，不能假设只有买家。若选择粗粒度事务门闩，应证明所有参与入口均在 U/C/SKU/订单/FK 锁之前取得且持续至提交/回滚，评估全库串行化容量和独立连接自阻塞。不能凭加一个门闩就宣称安全。
- 最终回归需要真实服务与独立连接，分别强制支付先/退款先、支付先/补贴先、支付先/过期先、零元/线下及跨用户奖励、镜像/权益写入，观察实际阻塞后继续。**成功标准是双方合法完成且没有1213/1205，不是将死锁当成功、xfail或重试耗尽后忽略。** 事务前后核对原余额/镜像相等、池守恒、券终态、精确授予/回收、事件唯一性与退款终态；注入提交前失败验证所有本地写入回滚，重放不重复。
- 业务政策冲突（需求docx日结1:1与 issue 旧月结330/30）仍未解决。锁序修复不得借机切换任一方案；008/009历史来源及上线门禁维持不变。当前没有上述完整切换及真实服务确定性交错回归，本节明确保持“未修复/未验收”。

### 统一协议实施前的进一步定位（未提交局部代码）

继续检查两种可行设计后，当前仍不能严格证明完整覆盖与可用性，因此未加入不完整的门闩/排序实现：

1. **在连接借出时统一取得事务门闩并不可直接采用。** `core/database.py:get_conn` 同时服务只读查询、资金事务和嵌套借连接，池为 `blocking=True`。`_settle_order_internal` 已持有外部事务时，`get_pool_allocations`、`get_distribution_platform_rate`、会员奖励比例读取另借连接；若所有连接默认取同一排他门闩，外层等待内层返回而内层等待外层释放，形成应用级自阻塞。若只锁写连接，又必须显式迁移每个事务拥有者，不能靠 SQL 首词推断或跳过锁竞争。配置比例读取还维持独立时点行为，不能在本修复中擅自改成新业务快照语义。
2. **适配器事务完整性独立修复（待验）**：此前 `core/db_adapter.py:PyMySQLAdapter.execute` 对 InterfaceError/OperationalError 自动关闭连接、重连并重放当前语句（含1213/1205），可能丢失前序写入而提交后缀。现已删除重放、保存并成对退出借连接上下文；详见下节。该修复不取得统一门闩，不消除全局锁环。`FinanceService.apply_withdrawal` 和 `check_director_promotion` 仍使用适配器，但数据库错误不会再被适配器当作可恢复单语句继续执行。
3. **按资源预锁也尚缺稳定受益人集合。** `_create_pending_rewards_v2` 按当前推荐链/层级寻找多个团队受益人（约1062–1152行），普通推荐奖励及退款精确授予回收又使用不同的受益人来源；`set_referrer` 和 `check_director_promotion` 可并行修改关系/资格。需要将关系、资格读取与最终受益人主键排序锁定纳入协议并重读校验；只将买家/当前直接推荐人排序会漏掉后续替代受益人，还可能因不同买家已有首锁构成 U↔U 环。
4. **协议首锁的实际修改范围继续扩大。** `api/finance/routes.py:/api/subsidy/fund` 直接更新 subsidy_pool；`set_pool_allocations` 绕开连接池、用独立物理连接和自己的命名锁再按账户名写多池；`clear_fund_pools` 按请求输入顺序锁多池。数据库初始化/迁移、购物车与商品更新也持 SKU/product/用户外键相关锁。不能只改五个结算文件而声称覆盖所有可达写入口；需要把这些所有者纳入锁图，给出与权益就绪共享锁和DDL维护窗口一致的顺序。

协调者已授权必要时修改 core/db_adapter、订单/购物车/商品和共享路径，文件范围不再是阻断理由。继续检查后，尚未实现的关键约束如下：

- 若采用显式写事务门闩，不能在原 `get_conn()` 上无差别启用。必须拆分并迁移所有写事务所有者，包括同一方法内的多次提交；每次 commit/rollback 后新事务都须重新取得首锁。外部连接/游标内部入口须验证调用方已经持有协议锁，而不是在已持资源锁后补取。仅重入 Python 上下文不能证明数据库锁仍存在。
- 不能通过 ContextVar/线程变量把嵌套连接默认为同一事务：`OrderManager.export_to_excel` 在外层连接上下文调用 detail 再借连接，`OfflineService.get_order_detail` 调发券查询，且 async 方法中存在 await。内层自行 commit/rollback、上下文跨任务继承以及不同连接的锁所有权必须区分；共享连接会改变既有事务提交边界，跳过内层门闩则只对已证明纯读入口安全。
- 配置写使用独立连接和 allocation 命名锁。新增行锁门闩若位于 allocation 锁两侧的顺序不同，会引入命名锁↔行锁环；该连接必须与普通配置写统一先后，并保持提交后释放及独立物理关闭语义，不能留命名锁归还池。
- 粗粒度串行方案能在参与事务之间阻止动态用户环，但必须覆盖关系/资格写及所有依赖的外键资源写入者，并避免把 provider 网络调用、长报表、后台整批持锁纳入全库独占区。当前已定位 `OfflineService.refresh_qrcode` 在连接内 await 外部二维码服务，scheduler 有一个事务跨多个订单状态更新；不能用“所有连接加锁”证明可用性。需明确写事务与外部调用边界，不能以有限超时当成功或屏蔽失败。

这些是可以通过跨入口事务改造解决的工程约束，不是声称问题在理论上不可修复；本片尚未完成相应实现与覆盖证明，因此没有提交局部门闩、SQL识别代理或不真实的回归测试。真实服务确定性交错测试同样尚未提交/执行。#227继续未修复；业务政策冲突不作为锁序问题的技术阻断理由。

### 独立 P0：适配器整笔事务完整性（实现待验，非锁序修复）

`core/db_adapter.py` 的 `PyMySQLAdapter` 内部调用契约变更：

- `execute(sql,params)` 仍返回原 ResultProxy，SQL校验及命名参数转换保持原规则；借连接后、创建游标及执行任何业务语句前显式调用 `conn.begin()`。DBUtils 3.2 的 SteadyDB 只有此调用才设置 `_transaction`，否则即使 autocommit=False 也可能透明重连并重放当前语句；不能只删除适配器自己的重连。任何校验/转换/执行失败均由适配器原样传播、不重放，立即尝试回滚整笔事务、关闭游标并退出借连接上下文。DBUtils 在显式事务中的故障处理可能替换底层会话，但仍必须抛出原错误而不重放失败业务语句；适配器随即释放该连接，不能继续后缀。错误日志不再输出整条 SQL/参数。
- 失败适配器处于不可提交状态；捕获 execute 异常后继续 execute 或 commit 会被拒绝。调用方必须显式 rollback 结束失败事务后才能开启新事务，不能只重试失败后缀。`close` 丢弃未提交事务，但不替代显式 rollback 对失败状态的确认。
- `commit` 获得数据库成功确认后只释放游标/借连接上下文，不再主动执行 rollback；释放阶段的普通异常只记录，不把已知提交成功变为业务失败。清理按 finally 链始终尝试归还连接并清空适配器引用。提交操作本身抛错仍保持失败状态、清理资源并传播，不自动重试，不能据本地回滚推断供应商或不确定提交结果。`rollback` 释放资源并重置失败状态。`begin()` 正常退出提交、异常退出回滚、最终释放；`with adapter` 同样处理整笔事务。清理异常不得覆盖原始执行/提交异常。
- 仓库实际实例由 FinanceService 创建：`apply_withdrawal`、`set_referrer`、`check_director_promotion` 成功时 commit、异常时 rollback，保留原业务返回值；内部 `_update_user_balance`/`_insert_points_log` 仍属于调用方事务。仓库未检出生产 `.begin()` 调用方；脚本仅实例化适配器调用 SQL 校验，无数据库事务。无需保留错误重放的兼容分支。不增加 HTTP 路由或修改 HTTP 请求/响应结构。

新增 `tests/isolation/test_db_adapter_atomicity.py`（本实现阶段未执行）：真实 InnoDB 临时表和独立阻塞连接制造1205，先写行1再阻塞行2，断言两行均保持原值、只有一次连接借出且已退出、捕获异常后不可提交/写入；另以真实已写数据库配合1213/InterfaceError语句故障注入，验证不会重连提交后缀、整笔回滚及显式新事务可复用；覆盖直接 close 丢弃和适配器上下文提交。注入1213不是实际死锁复现，所有断言均为待运行回归，不是通过证据。协调者应在 guard 认可的隔离环境运行该文件及原财务/安全套件。此修复仅声明阻止适配器单语句重放造成的事务拆分，#227全局锁环仍未修复。

补充生产连接栈回归（同文件，未执行）：使用与生产一致的 DBUtils PooledDB 配置及实际 `core.database.get_conn`，独立连接对测试自有物理连接执行 `KILL CONNECTION`，分别在已写前序余额后断开再执行后缀/提交，断言抛错、余额无后缀提交、失败后不可提交、池借出计数归零且显式新事务使用新物理连接。该测试要求隔离数据库账号能够终止自身连接，不连接生产，也不以裸 PyMySQL 替代池。另在真实池 commit 已落库后注入丢失确认错误，验证原错误传播、不会重试且已提交余额仍存在：**清理/rollback不能证明不确定提交没有发生**，实际业务必须用原持久身份查账恢复。该“确认丢失”是受控故障注入，不声称模拟所有真实网络时序。独立1205及语句注入覆盖继续保留。

提交成功后的清理回归（同文件，未执行）：在真实DBUtils池提交余额后，分别注入“若再 rollback 则断链”、游标 close 故障、借连接上下文退出故障；验证不再主动 rollback、commit 正常返回、真实余额已提交、上下文已退出、池借出计数归零和适配器引用清空。它与“提交操作本身丢失确认”的抛错契约分开，不能因后者需要保留不确定性而把已确认成功的提交报告为失败。
