import unittest

from core.payment_amounts import prepay_fee_cents_authoritative


class TestPrepayFeeAuthoritative(unittest.TestCase):
    def test_always_server_amount(self):
        self.assertEqual(
            prepay_fee_cents_authoritative(server_payable_cents=9900, client_fee_cents=1),
            9900,
        )
        self.assertEqual(
            prepay_fee_cents_authoritative(server_payable_cents=100, client_fee_cents=100),
            100,
        )


if __name__ == "__main__":
    unittest.main()
