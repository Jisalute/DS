# 电商小程序后端api

## 启动前准备

```bash
# 复制示例文件为 .env
cp .env.example .env

# 编辑 .env
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=password
MYSQL_DATABASE=database

UVICORN_PORT=8000

WECHAT_APP_ID=WECHAT_APP_ID
WECHAT_APP_SECRET=WECHAT_APP_SECRET
```
---

## 运行 / 启动说明

- **Linux（systemd）**

	如果使用 systemd 管理服务，可按以下步骤操作：

	- 将服务单元文件 `ds.service` 放到 `/etc/systemd/system/`（如尚未部署服务单元）。
	- 重新加载 systemd：

		```bash
		sudo systemctl daemon-reload
		```

	- 启动服务：

		```bash
		sudo systemctl start ds.service
		```

	- 重启服务（在更新后常用）：

		```bash
		sudo systemctl restart ds.service
		```

	- 查看服务状态：

		```bash
		sudo systemctl status ds.service
		```

	- 查看实时日志：

		```bash
		sudo journalctl -u ds.service -f
		```

- **Windows**

	Windows 系统可使用 [uv](https://docs.astral.sh/uv/getting-started/installation/)（按项目约定的工具）运行与调试：

	- 安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)（根据你使用的包管理器或安装方式）。
	- 初始化虚拟环境：

		```powershell
		uv venv
		```

	- 根据 `pyproject.toml`（或项目的 .toml 配置）同步/安装依赖：

		```powershell
		uv sync
		```

	- 以调试/运行模式启动项目：

		```powershell
		uv run main.py
		```

保留说明

- 启动后，访问 `http://127.0.0.1:<port>/docs` 查看 API 文档
- 或访问 `http://127.0.0.1:<port>/redoc` 查看 ReDoc 文档

### 会员权益需求、安全契约与验证

- [业务接口文档与 V1.1 需求差异](docs/禹泽小程序接口文档.md)：区分当前接口、安全变更、未实现的咯哒权益/白酒目标契约；Q1–Q17及原资产政策未由本轮改动。
- [V1.1 会员权益接口（第一阶段）](docs/api/v1-rights.md)：`/api/v1/rights`、`/api/v1/wine` 四条已实现接口的完整契约（Q12 补贴池下发暂缓，Q13 费用未确认）。
- [认证与用户隐私契约](docs/api/security-auth.md)：session-backed Bearer、短信挑战/密码找回、用户/银行卡权限与迁移要求。
- [订单、支付与退款契约](docs/api/security-payments.md)：服务端权威价格、状态竞态、持久退款恢复和回调。
- [财务安全契约](docs/api/security-finance.md)：默认管理员访问、个人数据作用域、持久幂等发券、提款供应商未接入边界。
- [内容审核契约](docs/api/msg-sec-check.md)：微信文本审核与商品嵌套字段/最终快照写入规则。
- [隔离验证执行指南](docs/PR220-isolation-execution.md)：候选环境运行说明；不代表已经运行或通过。
- [PR218 安全验证报告](docs/PR218-security-verification-20261025.md)：baseline实测证据、隔离基础设施和候选待验/外部边界。
- [历史安全与测试报告](docs/禹泽项目安全与测试报告.md)：2026-10-21历史内容安全48项记录，不是PR218候选验收证据。
- [系统权限接口](docs/api/security-auth.md#system-merchant-role-administration)：`POST /api/system/grant-system` 和 `PUT /api/system/sentences` 需管理员 Bearer；公开 `GET /api/system/sentences` 为只读投影。密码挑战绑定账号ID，不是仅绑定手机号。
- [待完成事项](docs/待完成事项.md)：保留需求历史，分列已实现待验、外部provider及仍未实现业务。
2026-10-25 PR218最终扩展round 4的WSL和confined server suite均为141 passed；服务和测试资源已清理，90个生产源文件hash未变。完整环境版本、JUnit、cleanup与限制见[PR218验证报告](docs/PR218-security-verification-20261025.md)。这是候选隔离测试，不等于实际生产部署、全面暴露审计或真实provider交易；真实短信、微信审核/支付/退款和商户银行卡提款仍未完成外部验证或接入。
- [购物车与订单 SKU/价格契约](docs/api/cart-order-security.md)：PR8 修订、MySQL 迁移与旧数据处理、隔离验证边界。

### 旧资产桥接与日结上线门禁（变更：009）

这不是可承诺无停写的在线迁移。008 的增量触发器在历史镜像尚为空/为零时，旧账本合法扣减也可能因镜像余额不足而失败。上线必须安排维护窗口：

1. 备份并记录可恢复的数据库快照；停止所有 API 写流量、所有 worker/定时任务及外部修数作业，排空在途事务。只停新日结不够，订单、退款、旧券和旧雨点路径同样会写 users。
2. 使用部署账号执行 `uv run python scripts/migrate.py`，按顺序安装007、008、009。不要改已应用迁移的内容/校验和。DDL隐式提交，不能依赖事务回滚安装过程。
3. 在仍停写的窗口执行 `uv run python scripts/migrate_legacy_assets.py --report legacy-assets-report.json`（不加 `--dry-run` 或 `--limit`）。触发器检查现在为强制检查，`--check-triggers` 仍可作为显式预检。必须退出码0、`ok=true`、`completed=true`、`balanced=true` 且 `negative_users=[]`、`conflict_users=[]`。负数不得夹零；应查清来源后按审核流程修正旧账本，再全量重跑。
4. 确认完成状态后再恢复应用和定时任务；应用账号须能读取当前库 INFORMATION_SCHEMA 触发器信息和009状态表，但不得有触发器DDL、直接改镜像积分/雨点或改完成标志的权限。迁移账号单独授权。旧账本 users 仍是唯一余额权威。
5. 任一步失败保持停写；保留报告和已提交批次，修正原因后全量重跑。脚本中断/重启不会自动恢复完成标志。若必须回退，继续停写并按备份恢复整套数据库和匹配版本应用，不要只卸触发器或只回滚应用后继续写两套账。

迁移作业使用连接级命名锁拒绝并跑，正式开始先持久清除完成标志；每批锁定 users 行并重读余额，不覆盖分页旧快照。只有镜像缺失且没有该会员积分/雨点V1流水时自动补建；两列已相等则不写。**任何既有镜像差异（包括镜像为0），或有V1积分/雨点流水的缺行，均不自动覆盖/相加**，保持原余额，记录 `conflict_users` 及日志：user_id、两侧积分/雨点旧值（缺镜像为null）、相关流水条数、reason（AMBIGUOUS_EXISTING_BALANCE/MISSING_MIRROR_WITH_V1_HISTORY）。不输出身份/手机号。请保存受限访问的JSON报告及备份，业务人员结合旧流水、V1流水和券批次人工审核、用有来源及幂等标识的修数流程闭合账本后全量重跑；本脚本不提供自动历史合并/冲正。无争议补建批次可先提交，冲突行始终不改，失败亦不发布完成。终态仅在无冲突、全量一致快照对账通过且触发器签名始末一致时发布完成。`--limit` 正式运行清除旧完成标志但不发布；`--dry-run` 不写余额/状态（不清除原合法标志），报告 `completed=false`，不授予上线许可。

**存量非零旧雨点升级目前阻塞，不能按上述命令宣称闭环成功。** 007会对既有会员 `INSERT IGNORE (user_id, points)`，产生雨点为0的镜像；即使积分一致、V1流水/日结/操作表均为空且007/008在 `schema_migrations` 中的SHA256与原始SQL匹配，也不能证明该镜像确由007创建：007使用 `CREATE TABLE IF NOT EXISTS` 和 `INSERT IGNORE`，版本记录不记行来源，历史记录亦可能缺失。009完成表不是追认历史资产来源的凭证。当前没有可信安装前基线材料及其信任锚，脚本对这类既有差异继续拒绝，重跑不会自动解除；不能删除镜像行、改完成标志或以人工勾选绕过。

解除此阻塞所缺的真实前置证据：007之前获取且可独立核验的数据库备份/快照，含实例与库身份、时间和逐用户旧积分/雨点，并能证明当时不存在 user_assets（或证明目标行来源）；备份摘要需由独立受控存储/签署记录绑定，不能仅由待迁移库或同一输入自报；连续停写及在途事务排空的操作记录需覆盖基线取得至迁移提交；还需核对007/008实际版本和原始校验、V1 ledger/settlement/operation历史及当前逐用户锁定余额。必须先由授权方提供可核验材料和受信验证方式，才能实现基线导入及不可变逐用户来源/前后值/幂等标识审计；本仓库目前没有该输入或导入通道，不提供伪成功的成功链路。已有009如已应用，不能修改其SQL后伪装相同版本，也不得手改schema_migrations校验和；补充审计结构需另行批准兼容迁移方案（010已另用），本轮未更改007/008/009。

当前可执行的是**受控人工处置，不是自动无损迁移**：保持全部API/worker/定时任务/修数入口冻结并排空事务，先做整库可恢复备份；业务负责人和数据库运维逐用户核对旧余额、镜像、订单/退款/旧流水、V1流水、券批次和可用备份，保存独立来源证据、决策理由、审核人、处置编号、前后值及执行记录。证据不足的会员不得改账，整库保持未完成；有充分依据的修复由获授权实际执行者设计、复核并负责，不能把两侧盲目设成相同或删除镜像作为审核。处置后先由另一审核者确认来源及逐用户资产/流水守恒，再全量重跑迁移并核对完成报告；失败继续冻结或按整库备份恢复。脚本只检验所述一致性门禁，不能证明人工决策正确，也不能证明当前数据安全；执行者对真实数据修复及恢复承担责任。

日结总入口、三个可独立调用的步骤及每个会员写事务均检查桥接就绪：缺009/状态行、未完成、缺触发器或触发器安装签名变化均抛 `RightsError(STATE_CONFLICT, HTTP 503)`，不创建本事务的结算/发券/积分流水。状态共享行锁持续到事务结束，迁移清标志必须等待在途日结提交；之后的新会员事务被拒绝，所以批量任务可能先完成若干会员后失败，应修复桥接后依原业务日期幂等重跑。持有 users 元数据锁防止事务内触发器DDL竞态。券过期锁序统一为状态门禁 → system_pools → users → user_assets → 券批次。

日结审计：`rain_generated` 是该业务日积分生成量；`rain_converted` / `coupons_issued` 是该业务日真实全额雨点转券的累计值，包含历史及其他来源，不能要求其等于 `rain_generated`。无生成记录的历史余额转券也建立日结行；同日后续新余额可继续转券并累计，零余额重试不重复发放。转换先发生时以内部 `failure_code=RAIN_GENERATION_PENDING` 标识尚未执行积分生成（不是交易失败），后续生成消费此标志，只生成一次；生成/转换/批次/流水均在各自会员事务内原子提交。既有历史错误日结明细不会被自动重写，需单独按真实批次及流水审核修复。

任何正数积分生雨点提交时，日结行置为 `pending` 且清空 `settled_at`，包括先前已完成历史余额转换的行；只有后续真实转券事务提交才重新标记 `settled`。转券失败回滚不会抹掉已提交的生成量或既有历史转券累计，也不会保留虚假的完成时间；按原业务日重试生成不会重复入账，重试转券会处理剩余余额。

已知边界：触发器重装会使原完成签名失效，需重新全量迁移；部署必须先停日结再做触发器DDL，以免DDL元数据锁等待。数据库管理员/越权账号直接改镜像、绕过触发器或伪造完成行不在应用锁能阻止的范围内；应限制权限并巡检对账。全量一致快照对账为 O(会员数)，大库需维护窗口容量评估。迁移互斥锁依赖同一MySQL实例和同一库，不能协调多主独立写库。隔离测试只证明实际记录的候选快照和场景，不代表生产历史资产已完成对账或允许部署。

---

## 离线收款二维码支持

1. 项目根目录下新增 `offline/` 文件夹，会被应用挂载为静态目录。
   - 在该目录放置微信小程序域名验证文件（比如 `senIScNn8d.txt`），
     即可通过 `https://<your-domain>/offline/senIScNn8d.txt` 访问。
   - 普通扫码（非小程序码）也可以指向此路径，例如作为服务器域名验证。
   - 请在 `.env` 中设置 `HOST` 为你的公开域名（不带尾部斜杠），
     以便接口返回的 `url` 正常带上域名前缀；例如：
     `HOST=https://hzai.tech`

2. 新增接口 `POST /api/offline/permanent-qrcode`，返回的数据中
   除了微信小程序码 (`qrcode`) 外还带几个辅助字段：
   * `url` – 用于生成普通二维码的 Web 链接，例如：
     `https://<your-domain>/offline/?id=123`。
     扫码后会加载静态页面并跳转到小程序。
   * `universal_link` – 旧的链接保留，格式
     `https://<your-domain>/offline/permanentPay?merchant_id=123`。
   * `plain_qrcode` – 同 `url` 对应的 PNG 图像的 Base64 数据
     （`data:image/png;base64,...`），前端可以直接展示。

   如果 `HOST` 未配置，这些字段会返回相对路径
   `/offline/?id=123` 或 `/offline/permanentPay?....`，前端需补全域名。
   前端在生成二维码时仍应对路径做 URL 编码（当前仅包含数字，安全）。

3. 为了配合上述 URL，我们增加了一个 HTTP 路由
   `GET /offline/permanentPay`，会根据 `merchant_id` 参数
   重定向到对应小程序页面（使用微信 universal link）。
   扫描普通二维码时会先访问
   `https://<your-domain>/offline/?id=...`，此时静态目录下的
   `index.html` 会读取 `id` 并再跳转到 `/offline/permanentPay`。
   这样微信/浏览器均能正确进入小程序并传递商家 ID。

---

> [!NOTE]
>
> `<port>` 为环境变量 `UVICORN_PORT`，若未设置则默认使用 `8000`
---

## AI 辅助免责声明

本项目在 AI/大型语言模型（包括 GitHub Copilot、ChatGPT 及相关工具）的协助下开发，受到了偶尔知道自己在做什么的人类的监督。

---