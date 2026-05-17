-- 任务二：由 scripts/migrate.py 执行，勿在业务运行时 DDL
-- sessions 表（认证）
CREATE TABLE IF NOT EXISTS sessions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    token VARCHAR(256) NOT NULL UNIQUE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    expired_at DATETIME NOT NULL,
    INDEX idx_token (token),
    INDEX idx_user (user_id),
    INDEX idx_expired (expired_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- users 积分列（若 database_setup 已建表则跳过）
-- migrate.py 会按 INFORMATION_SCHEMA 判断是否执行 ALTER
