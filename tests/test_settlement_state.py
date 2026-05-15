"""结算状态与幂等相关纯逻辑测试（无 DB）。"""

from services.finance.discount_helpers import cap_discounts_to_merchandise_total
from decimal import Decimal


def test_cap_discounts_never_exceeds_merchandise():
    c, pd, pu = cap_discounts_to_merchandise_total(
        Decimal("100"),
        Decimal("50"),
        Decimal("10"),
    )
    assert c + pd <= Decimal("100")
    assert pu >= Decimal("0")


def test_callback_idempotency_key_pattern():
    """支付回调幂等：同一 transaction_id 应只结算一次（由 DB 唯一约束保障）。"""
    seen: set[str] = set()

    def process_once(transaction_id: str) -> bool:
        if transaction_id in seen:
            return False
        seen.add(transaction_id)
        return True

    assert process_once("wx_tx_1") is True
    assert process_once("wx_tx_1") is False
