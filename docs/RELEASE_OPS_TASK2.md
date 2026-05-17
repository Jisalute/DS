# 任务二：2c2g 性能与运维发布说明

## 连接池与 Worker

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `MYSQL_POOL_SIZE` | `5` | **每个** Uvicorn worker 进程的最大 DB 连接数 |
| `UVICORN_WORKERS` | `2` | 文档/运维参考；启动时需与 gunicorn/uvicorn `--workers` 一致 |
| 预估总连接 | `MYSQL_POOL_SIZE × workers` | 2c2g 建议 ≤ 12（为 MySQL 其它客户端留余量） |

**推荐 2c2g：**

```bash
MYSQL_POOL_SIZE=4
UVICORN_WORKERS=2
# 总连接上限约 8
```

启动示例：

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2
```

## Redis

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `REDIS_ENABLED` | `true` | 关闭后下单锁/全局限流降級 |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` | `127.0.0.1:6379/0` | |
| `ORDER_CREATE_LOCK_TTL` | `120` | 下单锁 TTL（秒），应大于最长创建订单事务 |
| `ORDER_CREATE_LOCK_RENEW` | `30` | 长事务锁续期间隔 |

## 迁移与启动 DDL

**生产推荐：**

```bash
# 1. 先执行迁移（sessions、积分列等）
python scripts/migrate.py

# 2. 再启动 API（跳过 on_startup DDL）
SKIP_STARTUP_DDL=1
RUN_MIGRATE_ON_STARTUP=0
uvicorn main:app --workers 2 ...
```

**开发/单机：**

```bash
RUN_MIGRATE_ON_STARTUP=1
# SKIP_STARTUP_DDL 留空或 false，仍可使用 database_setup 幂等建表
```

## 健康检查

- `GET /health` — 进程存活
- `GET /ready` — `SELECT 1` + 连接池统计；DB 不可用时返回 503

## 日志

- `logs/api.log` 使用 `RotatingFileHandler`（默认 10MB × 10 份）
- 响应头 `X-Request-ID` 与日志字段 `[request_id]` 对应

## 后台任务单点

- **订单过期取消**：Redis 锁 `task:order_expire_cancel` 或 MySQL `GET_LOCK(ds_order_expire_cancel)`
- **APScheduler**：仍使用 `/tmp/scheduler.lock`（Linux）；Windows 多 worker 建议仅 1 个 worker 跑调度或改用 Redis 锁（后续任务）

## 微信限流

- `WX_GLOBAL_RATE_LIMIT_ENABLED=1` 且 Redis 可用时，结算/查询接口为**全局限流**
- `WX_SETTLEMENT_MAX_PER_SEC` / `WX_QUERY_MAX_PER_SEC` 可调

## 测试

```bash
pip install -e ".[dev]"
pytest tests/ -q
```

## 变更摘要

1. `get_conn()` → DBUtils 连接池  
2. 订单过期：logger + 分布式单点锁  
3. 下单 Redis 锁：TTL 120s + 续期  
4. `points_service` 运行时不再 `ALTER TABLE`  
5. `scripts/migrate.py` + `migrations/`  
6. `FinanceService` 折扣/常量拆至 `services/finance/`  
