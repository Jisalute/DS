# 线下订单详情与本人优惠券

## 变更记录

2026-11-02：修复通过 query `user_id` 读取他人优惠券的问题。

| 项目 | 变更前 | 变更后 | 影响范围 |
|---|---|---|---|
| `user_id` | 必填，直接作为券查询身份 | 可选，省略使用登录身份；提供时必须与登录用户一致 | 线下扫码支付页、收银台详情 |
| 权限 | 登录后可替换券查询目标 | 包括管理员在内，均只能查询本人优惠券 | 不提供管理员代查豁免 |
| HTTP 异常 | 服务抛出的 HTTP 异常被 broad except 转成400 | 原样保留 HTTP 状态码和 detail | 跨用户请求固定403，不变成400 |

**兼容性与迁移**：保留原 URL、方法、成功响应和本人 `user_id` 参数，无字段删除或废弃日期。已核对候选前端 `api/offline.js`、`api/order.js`、`utils/payment.js`、`subPackages/page2/api/payment.js`：它们携带本地 userInfo 的本人 ID，或缺 ID 时省略 query，均支持；不需要修改这些调用。前端建议省略 `user_id`，以 Bearer 会话为唯一身份来源。若缓存用户与 token 不一致，403 后修正会话/缓存，不得换其他用户 ID 重试。大整数 ID 不应先经过 JavaScript Number 的精度截断；省略 query 即可避免本接口的该问题。

#### 线下订单详情

**基本信息**：

- 用途：顾客扫码打开支付页后读取订单展示字段及当前登录用户的券列表；商户也可读取自己的收款单。
- 完整 URL：`GET {{BASE_URL}}/api/offline/dingdan/xiangqing/{order_no}`，`BASE_URL` 为部署服务根地址。当前注册无其他详情路由别名；`/offline`、`/pay-bridge` 为收款落地页，不是本接口别名。
- 登录/角色：需要有效 Bearer 会话；无商家角色要求，不提供跨用户券查询权限。
- 请求 `Accept: application/json`；无请求体，不要求请求 Content-Type。响应 `Content-Type: application/json`；成功 HTTP200。

**请求参数**：

| 参数 | 位置 | 类型 / JSON 类型 | 必填 | 默认值 | 校验、含义 | 合法 / 非法示例 |
|---|---|---|---|---|---|---|
| Authorization | header | string | 是 | 无 | `Bearer <token>`，服务端认证取得本人身份 | 有效会话 / 无效或过期 token |
| order_no | path | string | 是 | 无 | 已存在的完整线下订单号；沿用字符串路径，无新增长度校验；不是券身份凭据 | `OFF-synthetic` / 不存在的 `OFF-missing` |
| user_id | query | integer / number | 否 | null（使用本人） | 兼容字段，不参与决定查询身份；提供时必须数值等于会话 id；无额外范围校验，不相等统一403 | 本人7 / 他人8→403，`abc`→422 |

**请求体示例**：

```http
GET {{BASE_URL}}/api/offline/dingdan/xiangqing/OFF-synthetic
Authorization: Bearer <current-user-token>
Accept: application/json
```

无请求体。兼容调用（会话用户7）：`GET {{BASE_URL}}/api/offline/dingdan/xiangqing/OFF-synthetic?user_id=7`，Headers 同上。

非法示例（会话用户7）：`GET {{BASE_URL}}/api/offline/dingdan/xiangqing/OFF-synthetic?user_id=8` →403；`?user_id=abc` 或 `?user_id=` →422。拒绝跨用户请求时不调用订单/券查询服务。

**返回参数**：保持原字段，不分页。所有列出的字段均必返，除标记可空外不为 null。

| 字段 | 类型 | 空值 / 枚举 | 业务含义 / 示例 |
|---|---|---|---|
| code | integer | 固定0 | 成功标识 |
| message | string | `查询成功` | 成功提示 |
| data | object | 不为空 | 订单和本人券列表 |
| data.order_no | string | 无 | 订单号，`OFF-synthetic` |
| data.amount | integer | 单位分 | 原订单金额，1000 |
| data.store_name | string | 可空字符串 | 店铺名，`示例店铺` |
| data.product_name | string | 可空字符串 | 商品展示名，`示例商品` |
| data.status | integer | 1待支付、2已支付、4已退款 | 原始线下订单状态，不表示本次发生支付 |
| data.merchant_id | integer | 无 | 收款商户的用户ID，99；不是券归属用户 |
| data.coupons | array<object> | 无券为 `[]` | 仅会话本人的未使用券列表 |
| data.coupons[].id | integer | 无 | 优惠券ID，71 |
| data.coupons[].coupon_type | string | user用户券、merchant商户券 | 券类型，`user` |
| data.coupons[].amount | number | 单位元 | 券面额，1.0；区别订单 amount 的分 |
| data.coupons[].applicable_product_type | string | all、normal_only、member_only | 不限、仅普通商品、仅会员商品 |
| data.coupons[].applicable_product_type_text | string | 不限制、仅普通商品、仅会员商品；未知值显示未知 | 适用范围中文展示 |
| data.coupons[].status | string | 本接口查询 unused | 券使用状态 |
| data.coupons[].valid_from | string | `YYYY-MM-DD` | 有效期开始日，`2026-11-01` |
| data.coupons[].valid_to | string | `YYYY-MM-DD` | 有效期结束日，`2026-11-30` |
| data.coupons[].used_at | string或null | `YYYY-MM-DD HH:mm:ss`，未使用通常null | 实际使用时间 |
| data.coupons[].created_at | string | `YYYY-MM-DD HH:mm:ss` | 发券时间，`2026-11-01 09:00:00` |

**返回示例**：

```json
{
  "code": 0,
  "message": "查询成功",
  "data": {
    "order_no": "OFF-synthetic",
    "amount": 1000,
    "store_name": "示例店铺",
    "product_name": "示例商品",
    "status": 1,
    "merchant_id": 99,
    "coupons": [{
      "id": 71,
      "coupon_type": "user",
      "amount": 1.0,
      "applicable_product_type": "all",
      "applicable_product_type_text": "不限制",
      "status": "unused",
      "valid_from": "2026-11-01",
      "valid_to": "2026-11-30",
      "used_at": null,
      "created_at": "2026-11-01 09:00:00"
    }]
  }
}
```

无券成功响应：

```json
{"code":0,"message":"查询成功","data":{"order_no":"OFF-synthetic","amount":1000,"store_name":"示例店铺","product_name":"示例商品","status":1,"merchant_id":99,"coupons":[]}}
```

**使用方法与业务规则**：

1. 先登录，扫码取得订单号，再调用详情；无需是收款商户本人，不新增实名、会员、手机号绑定前置要求。保留原服务按订单号读取顾客扫码展示信息的行为，不能把它改成仅商户可读，否则会阻断顾客扫码。
2. 资源作用域分别处理：订单是扫码展示信息，券查询身份始终为 `current_user.id`。订单商户ID、query目标ID、客户端角色均不能扩大券归属。管理员同样只能读取本人券。
3. GET 只读、可重复调用；无需 Idempotency-Key，不锁定券、不扣券/积分、不绑定付款人、不发起交易，也不改变订单状态。实际支付仍需调用统一下单接口并由支付链路独立验证身份、券归属和状态；详情成功不证明支付成功。
4. 沿用现有 `FinanceService.list_available` 行为：查询本人的 `unused` 券，按创建时间倒序；现实现未按订单金额、有效期或商品适用范围进一步过滤，前端不可将返回列表理解为保证能用于本单。最终可用性由支付校验决定。本修复不改变发券/用券政策。
5. 无券返回空数组；不存在订单返回400。已支付/退款订单仍可读取原始状态，前端按状态禁止重复发起支付。详情不因二维码过期新增拒绝规则；并发用券后列表可能过时，应刷新并处理实际支付的拒绝响应。
6. 本接口没有新增接口级频率/额度限制。无权请求不以他人券补齐列表，不做后台代查或失败成功兜底。

**错误**：无独立业务错误码；HTTP异常响应使用 `detail`，参数校验 detail 为数组。

| HTTP | 触发条件 | 响应示例 | 前端建议 |
|---|---|---|---|
| 401 | 缺失、无效、过期、已撤销会话（认证依赖） | `{"detail":"缺少认证令牌"}` | 重新登录 |
| 403 | `user_id` 不等于本人；或认证拒绝冻结/注销账号 | `{"detail":"只能查询本人优惠券"}` | 修正身份缓存或提示无权限，不冒用他人身份 |
| 422 | query 非整数/空值等参数校验失败 | `{"detail":[{"type":"int_parsing","loc":["query","user_id"],"msg":"Input should be a valid integer, unable to parse string as an integer","input":"abc"}]}` | 修正参数；其余校验字段依框架版本 |
| 400 | 订单不存在；现有非HTTP服务异常处理仍沿用400 | `{"detail":"订单不存在"}` | 校验订单号，停止支付流程；不可显示支付成功 |
| 503 | 认证服务不可用，或下层主动抛出503 | `{"detail":"认证服务暂不可用"}` | 稍后重试，不解释成无券 |

下层显式抛出的 HTTPException 保留原状态码/detail（包括403），不再被通用异常处理改写成400。缺少 Bearer 时路由同时使用 HTTPBearer 安全依赖，实际缺头错误可能因所用框架版本先返回403；客户端不得依赖缺头一定为401。

## 回归覆盖与验证边界

`tests/test_offline_detail_identity.py` 覆盖省略/本人query、跨用户/伪管理员拒绝且不调用服务、非法query、下层HTTP异常原样传播、不存在订单兼容、认证失败，以及使用真实详情服务加受控数据库/券查询替身的顾客扫码路径（商户不是当前用户，只查本人券）。测试未连接生产或真实数据库，不替代真实会话、数据库和小程序联调。本实现按任务约束未执行测试、build、lint或formatter；由整合者统一验证。
