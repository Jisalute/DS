#!/usr/bin/env python3
"""旧账本资产迁移：users → user_assets（漏洞 1 & 2 的数据侧修复）。

做两件事
--------
1. `users.member_points`      → `user_assets.points`
2. `users.true_total_points`  → `user_assets.rain_balance`

幂等
----
* 按 user_id 游标分页；正式写入先锁 users 行并重读当前余额，再写镜像；
* 每批独立提交；仅补建无V1积分/雨点流水的缺失镜像，已有差异拒绝自动覆盖；
* 正式迁移以连接级命名锁拒绝并跑，开始即清除009完成状态；
* 仅无限量正式迁移且全量一致快照对账通过，才发布与当前触发器绑定的完成状态。

对账
----
运行前后各出一份全库对账：
    旧账本 SUM(users.member_points)  ==  V1.1 SUM(user_assets.points)
    旧账本 SUM(users.true_total_points) == V1.1 SUM(user_assets.rain_balance)
迁移完成后必须**完全相等**且逐会员零差异，否则进程以退出码 1 结束。

负值
----
旧账本出现负数说明上游存在越界扣减 Bug（`migrations/008` 的镜像触发器已对
`users` 的写入直接 `SIGNAL` 拒绝）。本脚本**不夹 0、不做任何圆场**：负值会员
只登记到 `negative_users`、不写镜像，最终退出码 1，要求业务方先修数据再重跑。
把负值悄悄变成 0 会让账面凭空多出资产，且永远查不出来。

注意：本脚本直接写 `user_assets.points / rain_balance`，这是**唯一**允许绕过
`services/legacy_assets_bridge.py` 的场合——它本身就是建立初始镜像的作业。
日常业务代码必须走 bridge 写 users 侧，由触发器镜像。

用法
----
    uv run python scripts/migrate_legacy_assets.py                # 正式执行
    uv run python scripts/migrate_legacy_assets.py --dry-run      # 只出报告，不写库
    uv run python scripts/migrate_legacy_assets.py --report out.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Iterator, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.database import get_conn  # noqa: E402
from core.logging import get_logger  # noqa: E402
from services import legacy_assets_bridge as bridge  # noqa: E402
from services.rights_service import format_amount, to_decimal  # noqa: E402

logger = get_logger(__name__)

DEFAULT_BATCH_SIZE = 500


def iter_legacy_users(cur, batch_size: int, limit: Optional[int] = None) -> Iterator[list[dict]]:
    """游标分页读取旧账本（keyset pagination，避免 OFFSET 越翻越慢）。"""
    last_id = 0
    fetched = 0
    while True:
        size = batch_size
        if limit is not None:
            remaining = limit - fetched
            if remaining <= 0:
                return
            size = min(size, remaining)
        cur.execute(
            """SELECT id, member_points, true_total_points
               FROM users WHERE id > %s ORDER BY id LIMIT %s""",
            (last_id, size),
        )
        rows = list(cur.fetchall() or [])
        if not rows:
            return
        last_id = int(rows[-1]["id"])
        fetched += len(rows)
        yield rows
        if len(rows) < size:
            return


def _is_negative(value: Decimal) -> bool:
    """旧账本出现负值 = 上游越界扣减的 Bug，迁移不得替它圆场。"""
    return value < 0


def migrate_batch(
    cur,
    rows: Iterable[dict],
    dry_run: bool,
    offenders: Optional[list[dict]] = None,
    conflicts: Optional[list[dict]] = None,
) -> dict:
    """迁移一批会员，返回本批统计。

    负值会员只登记、**不写库**（镜像侧 `CHECK (points >= 0)` 也不可能接受负值）。
    这类会员必须由业务方先查清越界扣减来源并修正旧账本，再重跑本脚本；
    脚本最终以退出码 1 结束，不会给出「已对齐」的假象。
    """
    stats = {
        "scanned": 0,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "negative": 0,
        "conflicts": 0,
    }
    for row in rows:
        user_id = int(row["id"])
        if not dry_run:
            # A paginated snapshot is not authoritative: serialize with the trigger's users lock.
            cur.execute(
                "SELECT id, member_points, true_total_points FROM users WHERE id = %s FOR UPDATE",
                (user_id,),
            )
            row = cur.fetchone()
            if row is None:
                continue
        points = to_decimal(row["member_points"] or 0)
        rain = to_decimal(row["true_total_points"] or 0)
        stats["scanned"] += 1

        if _is_negative(points) or _is_negative(rain):
            stats["negative"] += 1
            logger.error(
                "legacy_negative_balance user=%s points=%s rain=%s（拒绝迁移，请先修正旧账本）",
                user_id, points, rain,
            )
            if offenders is not None:
                offenders.append({
                    "user_id": user_id,
                    "member_points": format_amount(points),
                    "true_total_points": format_amount(rain),
                })
            continue

        cur.execute(
            "SELECT points, rain_balance FROM user_assets WHERE user_id = %s" + (" FOR UPDATE" if not dry_run else ""),
            (user_id,),
        )
        mirror = cur.fetchone()
        if mirror and to_decimal(mirror["points"]) == points and to_decimal(mirror["rain_balance"]) == rain:
            stats["unchanged"] += 1
            continue
        cur.execute(
            """SELECT COUNT(*) AS n FROM rights_ledger
               WHERE user_id = %s AND asset_type IN ('points', 'rain')""",
            (user_id,),
        )
        ledger_count = int(cur.fetchone()["n"])
        # No provenance proves an existing difference is a copy error, even without a ledger.
        if mirror is not None or ledger_count:
            detail = {
                "user_id": user_id,
                "legacy_points": format_amount(points),
                "legacy_rain": format_amount(rain),
                "mirror_points": format_amount(to_decimal(mirror["points"])) if mirror else None,
                "mirror_rain": format_amount(to_decimal(mirror["rain_balance"])) if mirror else None,
                "points_rain_ledger_rows": ledger_count,
                "reason": "AMBIGUOUS_EXISTING_BALANCE" if mirror else "MISSING_MIRROR_WITH_V1_HISTORY",
            }
            stats["conflicts"] += 1
            if conflicts is not None:
                conflicts.append(detail)
            logger.error("legacy_assets_manual_review %s", json.dumps(detail, ensure_ascii=False))
            continue
        if dry_run:
            stats["inserted"] += 1
            continue
        cur.execute(
            """INSERT INTO user_assets (user_id, points, rain_balance)
               VALUES (%s, %s, %s)""",
            (user_id, points, rain),
        )
        stats["inserted"] += 1
    return stats


def reconcile(dry_run: bool) -> dict:
    """对账：旧账本合计 vs V1.1 镜像合计，必须完全相等。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            conn.rollback()
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            report = bridge.parity_report(cur)
    return report


def _print_report(title: str, report: dict) -> None:
    print(f"\n=== {title} ===")
    print(f"  会员数                      : {report['users']}")
    print(f"  旧账本积分合计 (users)      : {format_amount(report['legacy_points'])}")
    print(f"  V1.1 镜像积分合计           : {format_amount(report['v1_points'])}")
    print(f"  积分差额                    : {format_amount(report['drift_points'])}")
    print(f"  旧账本雨点合计 (users)      : {format_amount(report['legacy_rain'])}")
    print(f"  V1.1 镜像雨点合计           : {format_amount(report['v1_rain'])}")
    print(f"  雨点差额                    : {format_amount(report['drift_rain'])}")
    print(f"  积分不一致会员数            : {report['points_mismatch_users']}")
    print(f"  雨点不一致会员数            : {report['rain_mismatch_users']}")
    print(f"  缺失镜像行的会员数          : {report['assets_row_missing']}")
    print(f"  是否完全对齐                : {'是' if report['balanced'] else '否'}")


def run(batch_size: int = DEFAULT_BATCH_SIZE, limit: Optional[int] = None, dry_run: bool = False) -> dict:
    """Serialize apply jobs; publish completion only after full consistent reconciliation."""
    if batch_size <= 0 or (limit is not None and limit <= 0):
        raise ValueError("batch_size and limit must be positive")
    totals = {"scanned": 0, "inserted": 0, "updated": 0, "unchanged": 0, "negative": 0, "conflicts": 0}
    offenders: list[dict] = []
    conflicts: list[dict] = []
    started_at = datetime.now()
    completed = False
    with get_conn() as conn:
        with conn.cursor() as cur:
            lock_name = None
            if not dry_run:
                cur.execute("SELECT CONCAT('legacy_bridge:', LEFT(SHA2(DATABASE(), 256), 50)) AS name")
                lock_name = cur.fetchone()["name"]
                cur.execute("SELECT GET_LOCK(%s, 0) AS acquired", (lock_name,))
                if cur.fetchone()["acquired"] != 1:
                    raise bridge.LegacyBridgeError("旧账本资产迁移已在运行")
            try:
                if not dry_run:
                    cur.execute("UPDATE legacy_bridge_completion SET completed = FALSE, trigger_signature = NULL, completed_at = NULL WHERE id = 1")
                    if cur.rowcount == 0:
                        cur.execute("SELECT id FROM legacy_bridge_completion WHERE id = 1")
                        if not cur.fetchone():
                            raise bridge.LegacyBridgeError("009迁移完成状态行缺失")
                    conn.commit()
                signature = bridge.trigger_signature(cur)
                conn.rollback()
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
                before = bridge.parity_report(cur)
                conn.rollback()
                _print_report("迁移前对账", before)
                for rows in iter_legacy_users(cur, batch_size, limit):
                    stats = migrate_batch(cur, rows, dry_run, offenders, conflicts)
                    for key in totals:
                        totals[key] += stats[key]
                    if not dry_run:
                        conn.commit()
                conn.rollback()
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
                if not dry_run:
                    cur.execute("SELECT id FROM legacy_bridge_completion WHERE id = 1 FOR UPDATE")
                # Metadata lock prevents trigger replacement until publication commits.
                cur.execute("SELECT id FROM users LIMIT 0")
                final_signature = bridge.trigger_signature(cur)
                after = bridge.parity_report(cur)
                completed = not dry_run and limit is None and not offenders and not conflicts and bool(after["balanced"]) and signature == final_signature
                if completed:
                    cur.execute("UPDATE legacy_bridge_completion SET completed = TRUE, trigger_signature = %s, completed_at = NOW() WHERE id = 1", (signature,))
                    conn.commit()
                else:
                    conn.rollback()
                _print_report("迁移后对账", after)
            finally:
                conn.rollback()
                if lock_name is not None:
                    cur.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
    return {
        "mode": "dry-run" if dry_run else "apply",
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "batch_size": batch_size,
        "limit": limit,
        "written": totals,
        "before": bridge.format_parity(before),
        "after": bridge.format_parity(after),
        "balanced": bool(after["balanced"]),
        "negative_users": offenders,
        "conflict_users": conflicts,
        "completed": completed,
        "ok": completed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="旧账本资产迁移与对账（幂等）")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="每批会员数，默认 500")
    parser.add_argument("--limit", type=int, default=None, help="仅迁移前 N 个会员（灰度/演练）")
    parser.add_argument("--dry-run", action="store_true", help="只出对账报告，不写库")
    parser.add_argument("--report", type=Path, default=None, help="把结构化报告写入 JSON 文件")
    parser.add_argument("--check-triggers", action="store_true", help="同时校验镜像触发器已安装")
    args = parser.parse_args()

    if args.check_triggers:
        with get_conn() as conn:
            with conn.cursor() as cur:
                bridge.assert_bridge_installed(cur)
        print("镜像触发器已安装 ✓")

    result = run(batch_size=args.batch_size, limit=args.limit, dry_run=args.dry_run)

    if args.report:
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入: {args.report}")
    if result["conflict_users"]:
        print("\n历史双账本差异需人工审核，未自动覆盖；完整明细见日志及 --report 输出：")
        for item in result["conflict_users"]:
            print(json.dumps(item, ensure_ascii=False))

    # 迁移模式要求严格相等且无负值会员；dry-run 本来就是"迁移前"状态，以是否可对齐为主
    if not args.dry_run and not result["ok"]:
        if result["negative_users"]:
            print(f"\n发现 {len(result['negative_users'])} 个负值会员，已拒绝迁移（旧账本越界扣减 Bug）：")
            for item in result["negative_users"][:20]:
                print(
                    f"  user={item['user_id']} member_points={item['member_points']} "
                    f"true_total_points={item['true_total_points']}"
                )
            print("请先修正旧账本，再重跑本脚本；不要通过改脚本/改约束绕过。")
        if not result["balanced"]:
            print("\n对账未通过：新旧账本仍存在差额，请勿继续上线。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
