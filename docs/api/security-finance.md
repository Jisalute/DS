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
