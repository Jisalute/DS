import json
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN

from core.config import POINTS_DISCOUNT_RATE


def max_coupon_total_yuan(merchandise_total: Decimal, points_discount: Decimal) -> Decimal:
    mt = merchandise_total if merchandise_total > 0 else Decimal("0")
    pd = points_discount if points_discount > 0 else Decimal("0")
    rem = mt - pd
    if rem < Decimal("0"):
        rem = Decimal("0")
    return rem.quantize(Decimal("1"), rounding=ROUND_CEILING)


def cap_discounts_to_merchandise_total(
    merchandise_total: Decimal,
    coupon_discount: Decimal,
    points_to_use: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    mt = merchandise_total if merchandise_total > 0 else Decimal("0")
    pu = points_to_use if points_to_use > 0 else Decimal("0")
    pd = pu * POINTS_DISCOUNT_RATE
    if pd > mt:
        pd = mt
        if POINTS_DISCOUNT_RATE and POINTS_DISCOUNT_RATE > 0:
            pu = (pd / POINTS_DISCOUNT_RATE).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
        else:
            pu = Decimal("0")
    max_c = max_coupon_total_yuan(mt, pd)
    c = coupon_discount if coupon_discount > 0 else Decimal("0")
    c = min(c, max_c)
    rem_after_points = mt - pd
    if rem_after_points < Decimal("0"):
        rem_after_points = Decimal("0")
    c = min(c, rem_after_points)
    return c, pd, pu


def parse_pending_coupon_ids(order_info: dict) -> list[int]:
    raw = order_info.get("pending_coupon_ids")
    if raw is not None and raw != "":
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8")
            if isinstance(raw, str):
                data = json.loads(raw)
            else:
                data = raw
            if not isinstance(data, list):
                return []
            return sorted({int(x) for x in data if x is not None})
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    pid = order_info.get("pending_coupon_id")
    if pid is not None and pid != "" and int(pid) != 0:
        return [int(pid)]
    return []


def parse_offline_coupon_ids(order_row: dict) -> list[int]:
    raw = order_row.get("coupon_ids")
    if raw is not None and raw != "":
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8")
            if isinstance(raw, str):
                data = json.loads(raw)
            else:
                data = raw
            if not isinstance(data, list):
                return []
            return sorted({int(x) for x in data if x is not None})
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    cid = order_row.get("coupon_id")
    if cid is not None and cid != "" and int(cid) != 0:
        return [int(cid)]
    return []
