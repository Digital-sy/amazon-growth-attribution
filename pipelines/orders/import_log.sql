CREATE TABLE IF NOT EXISTS `etl_file_import_log` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `target_table` VARCHAR(128) NOT NULL,
    `store_name` VARCHAR(50) NOT NULL,
    `report_month` DATE NULL,
    `source_file` VARCHAR(255) NOT NULL,
    `source_file_sha256` CHAR(64) NOT NULL,
    `file_size_bytes` BIGINT UNSIGNED NOT NULL DEFAULT 0,
    `import_batch_no` VARCHAR(64) NOT NULL,
    `status` VARCHAR(20) NOT NULL COMMENT 'running/success/failed/skipped',
    `total_rows` BIGINT UNSIGNED NOT NULL DEFAULT 0,
    `processed_rows` BIGINT UNSIGNED NOT NULL DEFAULT 0,
    `inserted_or_updated_rows` BIGINT UNSIGNED NOT NULL DEFAULT 0,
    `error_message` TEXT NULL,
    `started_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `finished_at` DATETIME NULL,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_file_table_store` (`target_table`, `store_name`, `source_file_sha256`),
    KEY `idx_status_started` (`status`, `started_at`)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='本地文件导入日志';
