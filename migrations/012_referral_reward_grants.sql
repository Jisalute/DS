-- Preserve exact credited amounts because legacy account_flow has only four decimals.
CREATE TABLE IF NOT EXISTS referral_reward_grants (
 flow_id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
 order_id BIGINT UNSIGNED NOT NULL,
 recipient_id BIGINT UNSIGNED NOT NULL,
 amount DECIMAL(14,6) NOT NULL,
 state VARCHAR(16) NOT NULL DEFAULT 'granted',
 recovered_refund_no VARCHAR(64) NULL,
 recovered_at DATETIME NULL,
 KEY ix_referral_grants_order (order_id, flow_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS referral_reward_reversals (
 flow_id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
 order_id BIGINT UNSIGNED NOT NULL,
 recipient_id BIGINT UNSIGNED NOT NULL,
 refund_no VARCHAR(64) NOT NULL,
 change_amount DECIMAL(14,6) NOT NULL,
 referral_before DECIMAL(14,6) NOT NULL,
 referral_after DECIMAL(14,6) NOT NULL,
 available_before DECIMAL(14,6) NOT NULL,
 available_after DECIMAL(14,6) NOT NULL,
 referral_flow_id BIGINT UNSIGNED NULL,
 available_flow_id BIGINT UNSIGNED NULL,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE KEY uq_referral_reversal_flow (referral_flow_id),
 UNIQUE KEY uq_available_reversal_flow (available_flow_id),
 KEY ix_referral_reversals_order (order_id, flow_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS order_member_reward_refunds (
 order_id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
 order_number VARCHAR(64) NOT NULL,
 user_id BIGINT UNSIGNED NOT NULL,
 refund_no VARCHAR(64) NULL,
 granted_amount DECIMAL(14,6) NOT NULL,
 recovered_amount DECIMAL(14,6) NOT NULL,
 shortfall_amount DECIMAL(14,6) NOT NULL,
 recovery_log_id BIGINT UNSIGNED NULL,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE KEY uq_member_reward_refund_order (order_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
