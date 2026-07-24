-- 历史文件全部导入后再执行，减少导入期间的索引维护成本。

ALTER TABLE `ods_amz_all_orders_report`
    ADD KEY `idx_store_purchase_date` (`store_name`, `purchase_date_utc`),
    ADD KEY `idx_store_asin_purchase_date` (`store_name`, `asin`, `purchase_date_utc`),
    ADD KEY `idx_store_sku_purchase_date` (`store_name`, `sku`, `purchase_date_utc`),
    ADD KEY `idx_order_status` (`order_status`),
    ADD KEY `idx_sales_channel` (`sales_channel`);
