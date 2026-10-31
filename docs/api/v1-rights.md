# V1.1 会员权益与白酒专区接口（第一阶段）

> 范围：`migrations/007_v1_rights.sql` + `services/rights_service.py` + `services/settlement_service.py` + `api/v1/`。
> 业务基线：`docs/问题清单.md` Q1-Q17 为最高优先级；与 `docs/需求文档.md` 冲突时以问题清单为准。
> **Q12（雨点补贴池 12%/8% 下发）本阶段强制暂缓**：补贴池只入不出，无任何发放明细与下发接口。
> **候选实现与上线阻断**：候选代码已通过 008 触发器桥接旧账本 `users.member_points` / `users.true_total_points` 与权益镜像；日结写旧账本，并由触发器同步镜像。009 完成状态及触发器门禁用于阻止桥接未就绪时的权益读取、资产写入和日结。标准存量升级中，007 可能创建的零雨点镜像与非零旧雨点的差异无法仅凭迁移版本和空流水证明来源；缺少可信基线及停写、事务排空证据时仍会拒绝自动回填，须人工逐用户对账，具体边界见 `README.md`。完成安全迁移、对账、切换及候选验收前，不应部署或启用 V1 日结。被动持有人自动转券采用持久循环扫尾，依赖 009 状态表；最新读取门禁尚未验证，既有测试结果不代表最新快照或生产状态。

## 0. 公共约定

**基本信息**

| 项 | 说明 |
| --- | --- |
| 基础路径 | `/api/v1` |
| 认证 | 全部接口强制 `Authorization: Bearer <token>`；用户归属取自认证身份，**不接受** 客户端传 `user_id` |
| Content-Type | 请求 `application/json`（GET 无请求体）；响应 `application/json` |
| 统一信封 | `{"code": "...", "message": "...", "data": {...}, "request_id": "..."}`，`code`/`message`/`request_id` 必返 |
| 数值约定 | 权益数量一律十进制字符串（6 位小数，如 `"8000.000000"`）；金额用整数分 `*_fen`；禁止用 float 表达数量 |
| 规则版本 | `rule_version = "v1.1-p1"` |

**统一错误响应**（异常由 `api/v1/__init__.py` 的处理器输出，HTTP 状态码按语义区分）

```json
{
  "code": "INSUFFICIENT_BALANCE",
  "message": "优惠券余额不足",
  "data": null,
  "request_id": "8f1c2d3e4a5b6c7d"
}
```

| 业务码 `code` | HTTP | 触发条件 | 前端处理建议 |
| --- | --- | --- | --- |
| `INVALID_ARGUMENT` | 422 | 数量非数字、≤0、小数超 6 位、坛数越界、幂等键格式非法 | 修正入参，直接提示 |
| `INSUFFICIENT_BALANCE` | 422 / 409 | 会员券或咯哒券不足（409 为并发下资产已变化） | 刷新资产后重试 |
| `POOL_INSUFFICIENT` | 422 / 409 | 平台咯哒蛋库存不足或咯哒值不可用 | 提示"当前库存不足"，不要自动重试 |
| `QUOTE_STALE` | 409 | `expected_unit_value` 与实时咯哒值不一致 | 重新拉取咯哒值并让用户确认 |
| `LISTING_UNAVAILABLE` | 404 / 409 | 挂牌不存在或状态不可售 | 返回列表页刷新 |
| `STATE_CONFLICT` | 409 / 503 | 资产/权属状态冲突、池未初始化、桥接未就绪或本人资产行缺失 | 提示稍后重试；503 联系运维检查迁移、触发器及资产对账，切勿显示为零 |
| `IDEMPOTENCY_CONFLICT` | 409 | 同一 `Idempotency-Key` 携带了不同参数 | 生成新键重试 |
| `SELF_TRADE_FORBIDDEN` | 422 | 买家等于挂牌店长（Q14 拦截自买自卖） | 提示不可购买本人酒坛 |

`401`（缺少/失效令牌）与 `403`（账号冻结或注销）沿用现有认证中间件，响应为 `{"detail": "..."}`。

---

## 1. 我的权益总览

**基本信息**

| 项 | 说明 |
| --- | --- |
| 用途 | 会员个人资产中心：积分、雨点、券、咯哒蛋（数量/成本/实时价值/增值进度）、咯哒券 |
| URL | `GET /api/v1/rights/me` |
| 认证/角色 | 需要登录，任意会员 |
| 请求体 | 无请求体 |
| 成功状态码 | `200` |

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 默认 | 校验 | 业务含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `Authorization` | header | string | 是 | 无 | `Bearer <token>` | 认证令牌，决定查询哪个会员 |
| `user_id` | body/query | — | **不接受** | — | 传入即被忽略 | 归属只认令牌，防止越权查询他人资产 |

**请求示例**

```
GET /api/v1/rights/me HTTP/1.1
Host: <host>
Authorization: Bearer <token>
```

非法示例：`GET /api/v1/rights/me?user_id=2`（参数被忽略，仍返回令牌本人数据，不构成越权）。

**返回参数**（`data`）

| 字段 | 类型 | 必返 | 空值含义 | 业务含义 |
| --- | --- | --- | --- | --- |
| `user_id` | string | 是 | — | 认证会员本人 ID 的十进制字符串；用于区分账户，不取客户端参数 |
| `member_level` | integer | 是 | — | 会员等级 |
| `capabilities.can_list_wine` | boolean | 是 | — | 是否可挂牌白酒（当前口径：等级 ≥ 6，Q8 待确认） |
| `points.balance` | string | 是 | — | 积分余额，长期有效不清零（Q1） |
| `rain.balance` | string | 是 | — | 雨点余额，中间值，不可提现不可兑换（Q2） |
| `coupons.balance` | string | 是 | — | 优惠券余额（= 未过期批次剩余之和） |
| `coupons.expiring_quantity` | string\|null | 是 | `null`=即将过期窗口未确认 | 即将过期券量 |
| `eggs.quantity` | string | 是 | — | 持有咯哒蛋数量 |
| `eggs.avg_cost_goda_value` | string\|null | 是 | `null`=无持仓成本基准 | 加权平均成本咯哒值（Q4） |
| `eggs.current_value` | string\|null | 是 | `null`=咯哒值不可用 | 权益计量值 = 数量 × 实时咯哒值，**不是提现金额** |
| `eggs.current_unit_value` | string\|null | 是 | `null`=平台库存为 0 | 实时咯哒值（Q6） |
| `eggs.growth_ratio` | string\|null | 是 | `null`=无成本基准 | 相对成本的增值比例，`1` 表示已达 100% 触发线 |
| `eggs.conversion_threshold_ratio` | string | 是 | — | 固定 `"1.000000"`，仅作客观规则展示 |
| `goda_coupons.balance` | string | 是 | — | 咯哒券余额，仅可兑坛装白酒 |
| `valuation_version` | integer\|null | 是 | `null`=池未初始化 | 估值版本号 |
| `rule_version` | string | 是 | — | 规则版本快照 |
| `valued_at` | string(datetime) | 是 | — | 估值时刻 |
| `blocked_questions` | array\<string\> | 是 | 空数组=无阻塞 | 影响口径的未确认问题编号 |
| `disclaimer` | string | 是 | — | 合规声明（固定文案） |

**返回示例**

```json
{
  "code": "OK",
  "message": "成功",
  "data": {
    "user_id": "1",
    "member_level": 0,
    "capabilities": {"can_list_wine": false},
    "points": {"balance": "10000.000000"},
    "rain": {"balance": "0.000000"},
    "coupons": {"balance": "100.000000", "expiring_quantity": null},
    "eggs": {
      "quantity": "8000.000000",
      "avg_cost_goda_value": "0.010000",
      "current_value": "80.000000",
      "current_unit_value": "0.010000",
      "growth_ratio": "0.000000",
      "conversion_threshold_ratio": "1.000000"
    },
    "goda_coupons": {"balance": "0.000000"},
    "valuation_version": 3,
    "rule_version": "v1.1-p1",
    "valued_at": "2026-10-31T16:00:00",
    "blocked_questions": [],
    "disclaimer": "本权益为会员消费回馈福利，非金融资产，不可提现、不可转让"
  },
  "request_id": "a1b2c3d4e5f60708"
}
```

**使用方法与业务规则**

- 前置条件：已登录，旧账本桥接迁移安全完成且触发器有效；认证会员的 `user_assets` 行必须存在。缺行或桥接未就绪返回 `503 STATE_CONFLICT`，不得按零展示；已就绪且余额确为零时正常返回零值。
- 调用顺序：进入权益中心先调本接口拿到咯哒值，再决定是否兑换。
- 幂等：纯读接口，可重复调用、可缓存数秒。
- 状态流转：无。
- 边界：平台咯哒蛋库存为 0 时 `current_unit_value`/`current_value` 为 `null`，前端显示"暂不可兑换"，不得显示 0。
- 本人 ID：响应 `data.user_id` 始终来自 Bearer 认证身份，以字符串避免大整数精度损失。前端本地待处理兑换需结合 API 环境与此 ID 隔离，不得仅按设备隔离或使用客户端传入的身份。

**变更记录**：向后兼容新增必返 `data.user_id`；旧客户端可忽略新字段，新客户端可据此区分已认证账户。

**变更记录**：向后兼容新增必返 `data.user_id`；旧客户端可忽略新字段，新客户端可据此区分已认证账户。安全变更：原先本人资产行缺失会返回虚假零余额；现在缺行、桥接未完成或触发器缺失/变更时返回 `503 STATE_CONFLICT`，前端应显示暂不可用并联系运维，不得将其作为零余额缓存。

**错误**：`401` 未登录、`403` 账号不可用、`503 STATE_CONFLICT` 桥接未就绪或本人资产行缺失。

---

## 2. 底池与实时咯哒值

**基本信息**

| 项 | 说明 |
| --- | --- |
| 用途 | 展示底池、补贴池、平台蛋库存与实时咯哒值；后台/前端同源 |
| URL | `GET /api/v1/rights/pool` |
| 认证/角色 | 需要登录，任意会员 |
| 请求体 | 无请求体 |
| 成功状态码 | `200` |

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 校验 | 业务含义 |
| --- | --- | --- | --- | --- | --- |
| `Authorization` | header | string | 是 | `Bearer <token>` | 认证令牌 |

**返回参数**（`data`）

| 字段 | 类型 | 必返 | 空值含义 | 业务含义 |
| --- | --- | --- | --- | --- |
| `total_supply` | string | 是 | — | 咯哒蛋发行总量，恒定 `100000000`，**无增发接口** |
| `pool_coupon_balance` | string | 是 | — | 底池优惠券余额（咯哒值分子） |
| `subsidy_coupon_balance` | string | 是 | — | 雨点补贴池余额，本阶段**只入不出**（Q12 暂缓） |
| `platform_egg_balance` | string | 是 | — | 平台未发放咯哒蛋库存（咯哒值分母） |
| `unit_value` | string\|null | 是 | `null`=库存为 0 | 实时咯哒值 = 底池券 ÷ 平台蛋库存（Q6） |
| `valuation_version` | integer | 是 | — | 估值版本，每次池变动 +1 |
| `valued_at` | string | 是 | — | 快照时间 |
| `rule_version` | string | 是 | — | 规则版本 |
| `blocked_questions` | array | 是 | — | 未确认问题编号（本接口当前为空） |
| `disclaimer` | string | 是 | — | 合规声明 |

**返回示例**

```json
{
  "code": "OK",
  "message": "成功",
  "data": {
    "total_supply": "100000000.000000",
    "pool_coupon_balance": "1000080.000000",
    "subsidy_coupon_balance": "20.000000",
    "platform_egg_balance": "99992000.000000",
    "unit_value": "0.010002",
    "valuation_version": 2,
    "valued_at": "2026-10-31T16:00:00",
    "rule_version": "v1.1-p1",
    "blocked_questions": [],
    "disclaimer": "本权益为会员消费回馈福利，非金融资产，不可提现、不可转让"
  },
  "request_id": "b2c3d4e5f6071829"
}
```

**使用方法与业务规则**

- 初始态：底池 `1,000,000`、平台蛋库存 `100,000,000`、咯哒值 `0.01`，仅由 `007_v1_rights.sql` 初始化一次。
- 咯哒值随兑换上升（底池增、库存减），随自动转券下降（底池减、库存回补）。
- 幂等：纯读；`valuation_version` 变化即表示价格已变，前端报价应作废。

**错误**：`401`、`403`、`503` 池未初始化。

---

## 3. 优惠券兑换咯哒蛋

**基本信息**

| 项 | 说明 |
| --- | --- |
| 用途 | 会员用优惠券兑换咯哒蛋，单事务完成扣券、80%/20% 分流、发蛋、更新加权平均成本 |
| URL | `POST /api/v1/rights/egg-exchanges` |
| 认证/角色 | 需要登录，任意会员 |
| Content-Type | `application/json` |
| 成功状态码 | `200` |

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 默认 | 校验 | 业务含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `Authorization` | header | string | 是 | — | `Bearer <token>` | 认证令牌 |
| `Idempotency-Key` | header | string | 建议必传 | 无 | 8-96 位字母/数字/`:`/`_`/`-` | 幂等键，同键同参返回首次结果 |
| `coupon_quantity` | body | string | 是 | — | 十进制字符串；`0 < 值 < 100000000000000`；≤6 位小数；长度 ≤40 | 提交的优惠券数量 |
| `expected_unit_value` | body | string\|null | 否 | `null` | 非空时同上数值范围及格式 | 前端报价咯哒值；传了就必须与实时值一致，否则 409 |

**请求示例**

```http
POST /api/v1/rights/egg-exchanges HTTP/1.1
Host: <host>
Authorization: Bearer <token>
Idempotency-Key: egg-20261031-0001
Content-Type: application/json
```

```json
{"coupon_quantity": "100", "expected_unit_value": "0.010000"}
```

非法示例：`{"coupon_quantity": "0"}` → `422 INVALID_ARGUMENT`；`{"coupon_quantity": 100}`（数字而非字符串）→ Pydantic 422；`{"coupon_quantity": "1.0000001"}` → 422（超过 6 位小数）。

**变更记录**：数值此前未限制整数位数；现限制为小于 `100000000000000`，以匹配 `DECIMAL(20,6)`，超出范围返回 422。客户端应在提交前校验；原本可提交但无法安全量化或存储的超大值不再接受。

**返回参数**（`data`）

| 字段 | 类型 | 必返 | 业务含义 |
| --- | --- | --- | --- |
| `exchange_id` | string | 是 | 兑换操作号（`EXG` + 时间 + 随机） |
| `status` | string | 是 | 固定 `succeeded` |
| `coupon_quantity` | string | 是 | 本次扣除的券 |
| `pool_in_quantity` | string | 是 | 入底池券（80%） |
| `subsidy_in_quantity` | string | 是 | 入雨点补贴池券（20%） |
| `egg_quantity` | string | 是 | 本次发放咯哒蛋 = (券×80%) ÷ 入池前咯哒值 |
| `executed_unit_value` | string | 是 | 成交咯哒值（入池前实时值） |
| `unit_value_after` | string\|null | 是 | 成交后咯哒值；若本次已转券，为转券后的值 |
| `avg_cost_goda_value` | string | 是 | 更新后的加权平均成本咯哒值（Q4） |
| `egg_quantity_after_exchange` | string | 是 | 兑换后、转券前的持仓快照（还原中间态用） |
| `egg_quantity_total` | string | 是 | 最终持有咯哒蛋；本次已转券则为 `0` |
| `goda_coupon_balance` | string | 是 | 成交后的咯哒券余额 |
| `coupon_balance_after` | string | 是 | 成交后券余额 |
| `valuation_version_before/_after` | integer | 是 | 估值版本变化（转券会额外 +1） |
| `auto_converted` | boolean | 是 | 本次事务内是否已触发并完成自动转券 |
| `rule_version` | string | 是 | 规则版本 |
| `created_at` | string | 是 | 成交时间 |
| `idempotent_replay` | boolean | 是 | `true` 表示命中幂等键、返回首次结果 |
| `auto_conversion` | object\|null | 否 | 成交后立即达到 Q4 触发线时的转券结果（字段：`conversion_id`/`egg_quantity_converted`/`goda_coupon_quantity`/`cost_basis`/`unit_value`/`unit_value_after`/`goda_coupon_balance`/`triggered_at`） |

**返回示例**

```json
{
  "code": "OK",
  "message": "兑换成功",
  "data": {
    "exchange_id": "EXG20261031160001A1B2",
    "status": "succeeded",
    "coupon_quantity": "100.000000",
    "pool_in_quantity": "80.000000",
    "subsidy_in_quantity": "20.000000",
    "egg_quantity": "8000.000000",
    "executed_unit_value": "0.010000",
    "unit_value_after": "0.010002",
    "avg_cost_goda_value": "0.010000",
    "egg_quantity_after_exchange": "8000.000000",
    "egg_quantity_total": "8000.000000",
    "goda_coupon_balance": "0.000000",
    "coupon_balance_after": "900.000000",
    "valuation_version_before": 1,
    "valuation_version_after": 2,
    "auto_converted": false,
    "rule_version": "v1.1-p1",
    "created_at": "2026-10-31T16:00:00",
    "idempotent_replay": false,
    "auto_conversion": null
  },
  "request_id": "c3d4e5f607182930"
}
```

触发转券时的返回（同一事务内完成，`data` 是最终状态）：

```json
{
  "code": "OK",
  "message": "兑换成功",
  "data": {
    "exchange_id": "EXG20261031184500C7D8",
    "egg_quantity": "80000.000000",
    "egg_quantity_after_exchange": "80000.000000",
    "egg_quantity_total": "0.000000",
    "goda_coupon_balance": "1866.640000",
    "auto_converted": true,
    "valuation_version_after": 3,
    "auto_conversion": {
      "conversion_id": "CVT20261031184500E9F0",
      "egg_quantity_converted": "80000.000000",
      "goda_coupon_quantity": "1866.640000",
      "cost_basis": "0.010000",
      "unit_value": "0.023333",
      "unit_value_after": "0.004667",
      "goda_coupon_balance": "1866.640000"
    }
  },
  "request_id": "c3d4e5f607182930"
}
```

**使用方法与业务规则**

- 前置条件：登录；券余额 ≥ 提交量；平台咯哒蛋库存 ≥ 本次发放量。
- 调用顺序：`GET /rights/pool`（展示咯哒值）→ 本接口；如需前端锁价，把当时 `unit_value` 作为 `expected_unit_value` 回传。
- 幂等：`Idempotency-Key` 与 `user_id + egg_exchange` 唯一；同键同参返回首次结果并置 `idempotent_replay=true`，同键不同参返回 409 `IDEMPOTENCY_CONFLICT`。
- 事务与加锁：先取得 `legacy_bridge_completion` 就绪行共享锁并校验桥接触发器签名，再按 **`system_pools` → `users` → `user_assets`** 顺序加锁（`users` 不可省略，镜像触发器与旧业务都是 users → user_assets）；关键字段用条件 UPDATE（`WHERE platform_egg_balance >= ?`、`WHERE coupon_balance >= ?`）二次兜底，**并发不会超发**（见 `test_concurrent_exchange_is_atomic_and_never_oversells`）。
- 死锁防护：与旧业务（`UPDATE users` 送/扣积分）在同一会员上并发时不得出现 1213/1205（见 `test_exchange_and_legacy_points_update_never_deadlock`）。
- 券批次：仅核销当前有效批次；按到期日、批次 ID 先进先出，并与扣减总余额在同一事务内完成。有效批次不足时，即使总余额足够也返回 `422 INSUFFICIENT_BALANCE`。
- 计算口径：兑换数量按**入池前**咯哒值计算（Q6）；80% 入底池，20% 入补贴池；拆分按 6 位舍入，补贴池取差额，保证 `pool_in + subsidy_in` 恒等于提交量。
- 状态流转：成交后若实时咯哒值 ≥ 成本 × 2，**在同一事务内**立即转券（Q4/Q5）：蛋清零、发咯哒券、底池按发券量扣减、蛋回补平台库存，结果附在 `auto_conversion` 并置 `auto_converted=true`。转券任何一步失败都会整笔回滚——不会出现「券已扣、蛋已发、券没转成」的中间态（见 `test_exchange_rolls_back_everything_when_conversion_fails`）。
- 被动持有人：兑换后不再操作的会员，由调度器每分钟续扫（`sweep_global_auto_conversions`，默认 500/批、每次最多 200 批）。数据库保存游标，达到分页上限后下一分钟从断点继续，不依赖本人访问或估值版本变化；失败和未达标会员下一圈重试，详见下述内部扫尾契约。
- 幂等与转券：`rights_operations.result` 保存的是**含转券在内的最终状态**，同键重试直接返回该状态，不会二次转券。
- 风控：账本 `rights_ledger` 只增不改不删（触发器强制）；兑换记录会员扣券、发蛋及三个平台池的变动。

**错误**：`401`、`403`、`409 QUOTE_STALE / IDEMPOTENCY_CONFLICT / STATE_CONFLICT`、`422 INVALID_ARGUMENT / INSUFFICIENT_BALANCE / POOL_INSUFFICIENT`、`503 STATE_CONFLICT` 桥接未就绪/触发器缺失或签名变化/池未初始化。

**就绪门禁变更（2026-11-01）**：`POST /api/v1/rights/egg-exchanges`（包括同幂等键重放）、内部 `maybe_auto_convert`、服务层 `redeem_wine_with_goda_coupons` 均在池锁前调用与日结相同的桥接就绪检查，并持有完成行共享锁直到事务提交/回滚。009 完成表缺失、行缺失、`completed=false`、008 触发器缺失或安装签名变化均拒绝写入。检查同时持有 users 表元数据锁，避免触发器 DDL 穿过在途写事务；迁移清除 completed 的排他 UPDATE 必须等待这些在途事务结束，此后新事务拒绝执行。该门禁不替代部署时停写要求。服务层兑酒目前没有公开执行 HTTP 路由；纯读接口及兑酒报价不新增资产写门禁。

HTTP 503 示例（桥接未就绪）：`{"code":"STATE_CONFLICT","message":"旧账本桥接未就绪：请执行009迁移及全量资产迁移","data":null,"request_id":"a1b2c3d4"}`。不同未就绪原因的 message 为安全域说明，不包含底层异常文本。前端不得按兑换成功刷新余额，应提示维护中并稍后使用同一幂等键重试；运营应完成迁移和触发器校验，而不是绕过门禁。请求/成功响应字段不变，新增的维护状态拒绝行为向后兼容。

### 全局自动转券内部扫尾契约（2026-11-01 变更）

本节是调度器内部调用，不新增 HTTP URL、认证、请求体或前端返回字段。前端继续使用资产/记录接口读取已提交结果。

**变更记录**：原实现每次从零开始、达到批次数上限仍视作完成，且调度器按内存估值版本跳过后续调用；现改为数据库持久游标循环扫描，不再按估值版本跳过。Q4/Q5 阈值、估值时点、发券与底池规则不变，HTTP 契约向后兼容。

**调用参数**：`batch_size` 默认 500、`max_batches` 默认 200，均为内部整数参数并至少取 1；前者限制一次查询的会员数，后者限制一次调用处理批数。每批只取有蛋会员 ID，不加载全表资产、不对资产做全表锁。

**持久化与部署**：先执行含 `rights_conversion_sweep_state` 的 `009` 迁移，再启动新调度器。表为单例 `id=1`，记录 `last_user_id`（本圈已尝试到的会员 ID）、`upper_user_id`（本圈固定 ID 上界）、`revision`（并发 CAS 版本）及更新时间；初始零游标从既有资产开始扫描，无需为旧会员回填。状态缺行返回域错误 `503 STATE_CONFLICT`，迁移缺表抛数据库异常；调度器记录错误并在下次 tick 重试，不能以空成功掩盖。迁移可重复执行；回退代码不必删除此表，切回新实现仍能续扫；不应与旧估值跳过实现并行运行。

**扫描就绪门禁**：初始化扫描上界、每个会员转券事务、每批 CAS 游标提交都先检查同一桥接就绪状态。未安装009或未完成迁移时，扫描不得初始化/推进游标；即使扫描开始时就绪，迁移在会员处理后清除完成标志，批末也会拒绝推进。已提交转券保持有效，恢复完成标志后从原游标安全重放。未就绪错误由调度器记录异常类别并在后续 tick 重试，不把全局门禁失败伪装成扫尾成功。

**状态与并发**：每圈开始在短事务内锁扫描状态行，固定当前资产最大 ID 上界；本圈结束归零，下次 tick 开始新圈，因此新增高 ID 不会无限延长当前圈，新增/重新持蛋的低 ID、此前未达标或失败会员也会在后续圈重查。每会员仍按 `system_pools → users → user_assets` 原事务锁序读取当时估值并转券，不承诺保留扫描前一度达标的历史价格。批后使用 `revision` 条件 UPDATE 提交游标；多实例竞争只允许一个推进，失败方停止本次调用，下次读数据库最新位置。状态事务不跨越会员处理，不新增反向资产锁，也不在处理期间额外占用连接。

**故障恢复**：会员失败仅记入本次 `failures`，继续处理该批其他人；底池不足、当前估值未达标等 `None` 结果计入 `users_deferred`，不透支、不清蛋。下一圈无条件再判定剩余持蛋会员，估值即使不变也会重试。批次提交前进程终止/数据库故障会重放该段；已转券会员蛋已清零，资产与操作账同事务提交，重放不会重复发券。批次提交成功但响应丢失则从已提交断点继续。无持蛋会员也完成一圈，后续新增持蛋会被新圈发现。

**内部返回参数**（非 HTTP 响应）：

| 字段 | 类型 | 必返 | 含义 |
|---|---|---|---|
| `status` | string | 是 | `partial`：本次有会员异常、游标竞争或尚未扫完本圈；否则 `succeeded`，不代表所有会员已达标转券 |
| `users_scanned/users_converted/users_deferred` | integer | 是 | 本次尝试/成功转券/返回 None 的会员数；竞争实例可能重复尝试同一会员，但不会重复转券 |
| `goda_coupon_issued` | string | 是 | 本次已提交转券量，6 位小数 |
| `batches` | integer | 是 | 本次查询批数，含用于确认结束的空批 |
| `failures` | array | 是 | 异常会员列表，空为 `[]`；每项 `user_id` integer 为内部会员 ID、`code` 固定 `SWEEP_FAILED`、`message` string 仅异常类别名（含 RightsError），不含原异常文本/业务消息/底层 SQL；日志同样仅保留类别和内部 ID，不暴露为前端错误 |
| `cycle_complete` | boolean | 是 | 本调用已成功提交本圈结束；不表示某个估值版本已永久处理完 |
| `cursor_conflict` | boolean | 是 | 本调用 CAS 失配，停止推进；后续调用重新读取数据库游标 |
| `last_user_id` | integer | 是 | 本调用最后确认位置，完成为 0；竞争时仅为本调用快照，数据库行才是权威 |
| `upper_user_id` | integer | 是 | 本次处理圈的固定上界，空资产表为 0 |
| `rule_version` | string | 是 | 当前规则版本 |
| `finished_at` | datetime | 是 | 内部完成时间 |

**运维边界**：每圈成本与持蛋会员数成正比；500×200 是单次调用处理上限，不是全局数据上限。持续失败保留持蛋并每圈重试，但永久损坏数据需按错误修复，不承诺固定完成时限；数据库/调度器持续不可用时无法保证推进。行为回归位于 `tests/test_v1_rights_sweep_mysql.py`，新增测试未在本轮执行。

---

## 4. 咯哒券兑酒报价

**基本信息**

| 项 | 说明 |
| --- | --- |
| 用途 | 按 3,000 咯哒券/坛报价，校验余额与挂牌状态，**不扣券、不锁库存、不落单** |
| URL | `POST /api/v1/wine/redemption-quotes` |
| 认证/角色 | 需要登录，任意会员（不可购买本人挂牌） |
| Content-Type | `application/json` |
| 成功状态码 | `200` |

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 默认 | 校验 | 业务含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `Authorization` | header | string | 是 | — | `Bearer <token>` | 认证令牌 |
| `listing_id` | body | integer | 是 | — | `>0` | 白酒挂牌 ID |
| `jars` | body | integer | 否 | `1` | `1-6` | 兑换坛数（Q8 一人限购 6 坛的保守上限） |

**请求示例**

```json
{"listing_id": 12, "jars": 1}
```

非法示例：`{"listing_id": 0}` → 422；`{"listing_id": 12, "jars": 7}` → 422。

**返回参数**（`data`）

| 字段 | 类型 | 必返 | 空值含义 | 业务含义 |
| --- | --- | --- | --- | --- |
| `quote_id` | string | 是 | — | 报价单号（`WQ` 前缀） |
| `listing_id` / `contract_no` | string | 是 | — | 挂牌与合同编号 |
| `jars` / `weight_jin` | integer / string | 是 | — | 坛数与合计重量 |
| `goda_coupon_quantity` | string | 是 | — | 应付咯哒券 = 3000 × 坛数 |
| `goda_coupon_balance` | string | 是 | — | 当前咯哒券余额 |
| `pool_coupon_debit` | string | 是 | — | 底池券扣减量，**Q7 明确兑酒不扣底池，恒为 `"0.000000"`** |
| `wine_price_fen` | integer | 是 | — | 酒价（分），仅酒价 |
| `fees.packaging_fen/container_fen/shipping_fen` | integer\|null | 是 | `null`=Q13 未确认（**不等于免费**） | 包装/器皿/物流费用 |
| `rule_version` / `expires_at` | string | 是 | — | 规则版本与报价有效期（15 分钟） |
| `blocked_questions` | array | 是 | — | 当前为 `["Q13"]` |
| `disclaimer` | string | 是 | — | 合规声明 |

**返回示例**

```json
{
  "code": "OK",
  "message": "报价成功",
  "data": {
    "quote_id": "WQ20261031160002C3D4",
    "listing_id": "12",
    "contract_no": "YZ-HT-0001",
    "jars": 1,
    "weight_jin": "100.000000",
    "goda_coupon_quantity": "3000.000000",
    "goda_coupon_balance": "5000.000000",
    "pool_coupon_debit": "0.000000",
    "wine_price_fen": 300000,
    "fees": {"packaging_fen": null, "container_fen": null, "shipping_fen": null},
    "rule_version": "v1.1-p1",
    "expires_at": "2026-10-31T16:15:00",
    "blocked_questions": ["Q13"],
    "disclaimer": "本权益为会员消费回馈福利，非金融资产，不可提现、不可转让"
  },
  "request_id": "d4e5f60718293041"
}
```

**使用方法与业务规则**

- 前置条件：挂牌状态 `listed`、酒坛权属与挂牌人一致、买家 ≠ 挂牌店长（Q14）、咯哒券余额 ≥ 应付量。
- 调用顺序：本接口取报价 → 第二阶段下单接口（未开放，依赖 Q13/Q15/Q16）→ 支付成功后才转权属、结算店长。
- 幂等：报价本身可重复调用，每次生成新 `quote_id`；不产生任何资产变动。
- 状态流转（挂牌）：`listed → locked → sold`，未锁定可 `withdrawn`；本阶段不实现锁定。
- 边界：报价 15 分钟过期；报价不代表库存保留；兑酒**不扣底池**，也不给买家积分（Q7）；卖家 80%/20% 结算未实现（Q9/Q10/Q15）。

**错误**：`401`、`403`、`404/409 LISTING_UNAVAILABLE`、`422 SELF_TRADE_FORBIDDEN / INSUFFICIENT_BALANCE`、`409 STATE_CONFLICT`。
