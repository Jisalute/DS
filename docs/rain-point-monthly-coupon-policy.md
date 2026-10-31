# Rain Point Monthly Coupon Policy

> Historical policy and implementation reference, not a statement of the current production schedule or an authorization to enable it. The candidate retains the 9.28 monthly policy, migration `003`, implementation and tests, but does **not** register the legacy monthly conversion job.

## Assessment

The existing `users.true_total_points` field is the available rain-point balance. Existing coupon expiry settlement is transactional and already writes an equivalent amount to `member_points`; this feature extends that flow instead of creating a second balance system.

The existing `director_pool` account is used by the unilevel dividend distribution, so it is the implementation mapping for the requested dividend pool. Legacy coupons without a rain-point settlement reference continue to settle into `subsidy_pool`.

## Rules

The historical policy intended to process each normal member once per `YYYY-MM` period. The retained implementation instead selects `users.status = 1`; the current `UserStatus` definition uses `0` for normal and `1` for frozen. This discrepancy requires business and engineering review before any use of the legacy function.

1. Balance greater than 330: convert `balance - 330` into one user coupon.
2. Balance from 0 through 330: convert `min(balance, 30)` into one user coupon.
3. The converted rain points are deducted atomically from `true_total_points`.
4. The coupon is valid from the first day through the last day of the settlement month. An unused coupon is marked expired after that month.
5. An expired rain-point coupon is credited to `director_pool` and the same amount is added to `users.member_points`, with a `points_log` entry.
6. A unique `(user_id, settlement_period)` key makes retries idempotent. A zero balance still creates a zero-value settlement record and no coupon.

## Candidate schedule and deployment boundary

The candidate registers V1.1 daily rights settlement at 00:00; the legacy monthly rain-point conversion registration (`monthly_rain_point_coupon`, historically intended for 00:05 on the first day of each month) is commented out. Its method and historical data remain available; do not manually invoke or re-enable it alongside V1 daily settlement. The candidate also retains the separate 23:58 legacy coupon-expiry job for existing coupons; this does not mean monthly conversion is enabled.

The two policies are not interchangeable: the monthly rule retains or converts amounts according to the 330/30 thresholds and issues a coupon for the settlement month, whereas V1.1 daily settlement converts the available rain balance 1:1 into a 30-day coupon batch. They operate on the same legacy rain balance, and running both can compete for it. The V1 selection uses positive balances without a user-status predicate; the retained monthly method selects `status = 1`, which conflicts with both its stated normal-member policy and the current status definitions.

Before any cutover, responsible operators must reconcile historical monthly settlements, balances, coupon batches, expiry, and relevant ledgers per user; business owners must confirm the intended policy, eligible statuses, and treatment of unresolved discrepancies. Do not infer a safe automatic merge from matching totals. Follow the candidate migration and stop-write instructions in `README.md` and `docs/待完成事项.md`. Migration `003` belongs to the historical implementation; applying it alone does not authorize enabling the monthly scheduler.

These statements describe repository candidate configuration only. They do not assert that production has switched, that production scheduling has changed, or that reconciliation has been completed.

## Verification

For the historical implementation, test the three boundary balances (0, 30, 330), an excess balance (331 or more), duplicate execution for the same month, concurrent execution, coupon expiry routing, and rollback when any insert/update fails. Candidate cutover also requires isolated verification of the selected policy, status eligibility, migration and per-user reconciliation; this document does not claim those checks have run.

## Change Checklist

- [x] Add 330 base and 30 monthly policy constants.
- [x] Add a unique monthly settlement ledger and coupon reference.
- [x] Add transactional conversion and idempotent retry handling.
- [x] Route policy coupon expiry to `director_pool` and credit member points.
- [x] Historical implementation included a day-1 00:05 registration; it is disabled in the current candidate.
- [x] Add migration and boundary tests.
