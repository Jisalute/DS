-- Durable order creation results; committed atomically with the order and stock.
CREATE TABLE IF NOT EXISTS order_creation_idempotency (
    user_id BIGINT UNSIGNED NOT NULL,
    key_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    request_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    order_number VARCHAR(32) NOT NULL,
    need_pay BOOLEAN NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, key_hash),
    UNIQUE KEY uk_order_creation_idempotency_order (order_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
