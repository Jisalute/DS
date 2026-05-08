"""预下单/支付金额：权威金额仅以服务端计算为准（防客户端改价）"""


def prepay_fee_cents_authoritative(*, server_payable_cents: int, client_fee_cents: int) -> int:
    """
    返回微信预下单使用的应付金额（分）。始终采用服务端金额，不信任客户端传参。

    Args:
        server_payable_cents: 服务端根据订单、券、积分算出的应付（分）
        client_fee_cents: 客户端上报金额（分）；调用方应自行打日志比对，本函数不参与决策
    """
    return int(server_payable_cents)
