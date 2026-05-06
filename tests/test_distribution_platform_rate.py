"""平台计提比例：子池缩放与公司积分池倍数与 NOMINAL_SUBPOOL_SLICE 一致。"""
from decimal import Decimal

from services.finance_service import NOMINAL_SUBPOOL_SLICE


def test_nominal_slice_and_scale_example():
    assert NOMINAL_SUBPOOL_SLICE == Decimal("0.20")
    platform_rate = Decimal("0.25")
    scale = platform_rate / NOMINAL_SUBPOOL_SLICE
    assert scale == Decimal("1.25")
    base = Decimal("100")
    subsidy_ratio = Decimal("0.12")
    assert base * subsidy_ratio * scale == Decimal("15.000000")
