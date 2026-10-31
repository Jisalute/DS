-- Completion is written only by a full, reconciled legacy asset migration.
CREATE TABLE IF NOT EXISTS legacy_bridge_completion (
    id TINYINT UNSIGNED NOT NULL PRIMARY KEY,
    completed BOOLEAN NOT NULL DEFAULT FALSE,
    trigger_signature CHAR(64) NULL,
    completed_at DATETIME NULL,
    CONSTRAINT chk_legacy_bridge_singleton CHECK (id = 1)
) ENGINE=InnoDB;

INSERT IGNORE INTO legacy_bridge_completion (id, completed) VALUES (1, FALSE);

CREATE TABLE IF NOT EXISTS rights_conversion_sweep_state (
    id TINYINT UNSIGNED NOT NULL PRIMARY KEY,
    last_user_id BIGINT UNSIGNED NOT NULL DEFAULT 0,
    upper_user_id BIGINT UNSIGNED NOT NULL DEFAULT 0,
    revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO rights_conversion_sweep_state(id) VALUES(1);
