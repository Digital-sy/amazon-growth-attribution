-- Amazon订单五分类归因中间表与结果表
-- 适用数据库：MySQL 8.0+

CREATE TABLE IF NOT EXISTS `dwd_amz_order_attribution_base` (
    `source_id` BIGINT UNSIGNED NOT NULL COMMENT 'ods_amz_all_orders_report.id',
    `store_name` VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    `order_date` DATE NOT NULL COMMENT '美国太平洋时区订单日期',
    `order_month` DATE NOT NULL COMMENT '订单月份，当月1日',
    `purchase_date_utc` DATETIME NOT NULL,
    `amazon_order_id` VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    `order_item_id` VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT '',
    `sku` VARCHAR(255) NULL,
    `sku_key` VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    `asin` VARCHAR(32) NULL,
    `quantity` INT NOT NULL,
    `currency` VARCHAR(10) NULL,
    `gross_item_sales` DECIMAL(18,4) NOT NULL DEFAULT 0,
    `item_promotion_discount` DECIMAL(18,4) NOT NULL DEFAULT 0,
    `net_item_sales` DECIMAL(18,4) NOT NULL DEFAULT 0,
    `net_unit_price` DECIMAL(18,6) NOT NULL DEFAULT 0,
    `promotion_ids` TEXT NULL,
    `offsite_flag` TINYINT(1) NOT NULL DEFAULT 0 COMMENT 'promotion_ids命中MPC-',
    `shipping_promotion_flag` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '免运费类促销标识',
    `onsite_promotion_flag` TINYINT(1) NOT NULL DEFAULT 0 COMMENT 'Percentage Off、PLM或商品促销折扣>0',
    `low_price_candidate_flag` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '排除站外和站内促销后净单价<=7美元',
    `rule_version` VARCHAR(32) NOT NULL DEFAULT 'v1_20260727',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`source_id`),
    KEY `idx_base_month_store` (`order_month`, `store_name`),
    KEY `idx_base_match` (`store_name`, `order_date`, `sku_key`, `amazon_order_id`),
    KEY `idx_base_order` (`store_name`, `amazon_order_id`),
    KEY `idx_base_flags` (`order_month`, `offsite_flag`, `onsite_promotion_flag`, `low_price_candidate_flag`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
  COMMENT='Amazon有效订单商品明细归因基础表';

CREATE TABLE IF NOT EXISTS `dws_amz_ad_order_demand_daily` (
    `store_name` VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    `report_date` DATE NOT NULL,
    `order_month` DATE NOT NULL,
    `sku_key` VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    `asin` VARCHAR(50) NULL,
    `sp_orders` INT NOT NULL DEFAULT 0,
    `sd_orders` INT NOT NULL DEFAULT 0,
    `ad_orders_raw` INT NOT NULL DEFAULT 0,
    `ad_sales` DECIMAL(18,4) NOT NULL DEFAULT 0,
    `ad_cost` DECIMAL(18,4) NOT NULL DEFAULT 0,
    `clicks` BIGINT NOT NULL DEFAULT 0,
    `impressions` BIGINT NOT NULL DEFAULT 0,
    `positive_ad_type_count` TINYINT NOT NULL DEFAULT 0,
    `multi_ad_type_flag` TINYINT(1) NOT NULL DEFAULT 0,
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`store_name`, `report_date`, `sku_key`),
    KEY `idx_ad_demand_month` (`order_month`, `store_name`),
    KEY `idx_ad_demand_orders` (`order_month`, `ad_orders_raw`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
  COMMENT='SP和SD广告订单日需求，店铺+日期+SKU';

CREATE TABLE IF NOT EXISTS `dwd_amz_ad_order_allocation` (
    `store_name` VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    `order_date` DATE NOT NULL,
    `order_month` DATE NOT NULL,
    `sku_key` VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    `amazon_order_id` VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    `first_purchase_date_utc` DATETIME NOT NULL,
    `candidate_rank` INT NOT NULL,
    `candidate_orders` INT NOT NULL DEFAULT 0,
    `sp_orders` INT NOT NULL DEFAULT 0,
    `sd_orders` INT NOT NULL DEFAULT 0,
    `ad_orders_raw` INT NOT NULL DEFAULT 0,
    `allocated_ad_orders` INT NOT NULL DEFAULT 0,
    `unallocated_ad_orders` INT NOT NULL DEFAULT 0,
    `estimated_ad_flag` TINYINT(1) NOT NULL DEFAULT 0,
    `allocation_version` VARCHAR(32) NOT NULL DEFAULT 'v1_20260727',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`store_name`, `order_date`, `sku_key`, `amazon_order_id`),
    KEY `idx_allocation_month_flag` (`order_month`, `estimated_ad_flag`),
    KEY `idx_allocation_order` (`store_name`, `amazon_order_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
  COMMENT='广告订单统计分配结果，非Amazon逐单归因';

CREATE TABLE IF NOT EXISTS `dwd_amz_order_attribution_result` (
    `source_id` BIGINT UNSIGNED NOT NULL,
    `store_name` VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    `order_date` DATE NOT NULL,
    `order_month` DATE NOT NULL,
    `amazon_order_id` VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    `order_item_id` VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT '',
    `sku` VARCHAR(255) NULL,
    `sku_key` VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    `asin` VARCHAR(32) NULL,
    `quantity` INT NOT NULL,
    `gross_item_sales` DECIMAL(18,4) NOT NULL DEFAULT 0,
    `net_item_sales` DECIMAL(18,4) NOT NULL DEFAULT 0,
    `net_unit_price` DECIMAL(18,6) NOT NULL DEFAULT 0,
    `promotion_ids` TEXT NULL,
    `offsite_flag` TINYINT(1) NOT NULL DEFAULT 0,
    `estimated_ad_flag` TINYINT(1) NOT NULL DEFAULT 0,
    `onsite_promotion_flag` TINYINT(1) NOT NULL DEFAULT 0,
    `low_price_flag` TINYINT(1) NOT NULL DEFAULT 0,
    `main_order_type` VARCHAR(20) NOT NULL COMMENT '站外推广/广告/站内促销/低价/自然',
    `classification_reason` VARCHAR(100) NOT NULL,
    `candidate_rank` INT NULL,
    `ad_orders_raw` INT NOT NULL DEFAULT 0,
    `rule_version` VARCHAR(32) NOT NULL DEFAULT 'v1_20260727',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`source_id`),
    KEY `idx_result_month_type` (`order_month`, `store_name`, `main_order_type`),
    KEY `idx_result_sku_month` (`store_name`, `order_month`, `sku_key`),
    KEY `idx_result_order` (`store_name`, `amazon_order_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
  COMMENT='Amazon订单商品明细五分类最终结果';

CREATE TABLE IF NOT EXISTS `dws_amz_order_source_monthly` (
    `order_month` DATE NOT NULL,
    `store_name` VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    `main_order_type` VARCHAR(20) NOT NULL,
    `classified_item_rows` BIGINT NOT NULL DEFAULT 0,
    `order_sku_count` BIGINT NOT NULL DEFAULT 0 COMMENT '去重店铺+订单号+SKU，可加总的主订单指标',
    `amazon_order_count` BIGINT NOT NULL DEFAULT 0 COMMENT '跨类型可能重复，仅供参考',
    `units` BIGINT NOT NULL DEFAULT 0,
    `gross_item_sales` DECIMAL(20,4) NOT NULL DEFAULT 0,
    `net_item_sales` DECIMAL(20,4) NOT NULL DEFAULT 0,
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`order_month`, `store_name`, `main_order_type`),
    KEY `idx_monthly_store` (`store_name`, `order_month`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
  COMMENT='Amazon五类订单月度汇总';
