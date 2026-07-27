#!/usr/bin/env bash
set -Eeuo pipefail

# Amazon 五分类归因：产品表现月度粗粒度版本。
#
# 分类优先级：
#   1. 站外推广：订单 promotion_ids 命中 MPC-
#   2. 广告：产品表现月表 ad_order_quantity，按店铺+月份统计分配
#   3. 站内促销：Percentage Off / PLM- / item_promotion_discount > 0
#   4. 低价：排除前三类后净成交单价 <= 10 USD
#   5. 自然：其余订单
#
# 注意：广告属于店铺月度统计分配，不代表 Amazon 逐单真实广告归因。

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOG_DIR="$PROJECT_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="${ATTR_LOG_FILE:-$LOG_DIR/order_attribution_pipeline.log}"

: "${LINGXING_DB_HOST:?缺少 LINGXING_DB_HOST}"
: "${LINGXING_DB_USER:?缺少 LINGXING_DB_USER}"
: "${LINGXING_DB_PASSWORD:?缺少 LINGXING_DB_PASSWORD}"

DB_PORT="${LINGXING_DB_PORT:-3306}"
DB_NAME="${LINGXING_DB_NAME:-lingxing}"
export MYSQL_PWD="$LINGXING_DB_PASSWORD"

MYSQL=(mysql -h "$LINGXING_DB_HOST" -P "$DB_PORT" -u "$LINGXING_DB_USER"
  "$DB_NAME" --default-character-set=utf8mb4 --batch --raw)

log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG_FILE"
}

scalar() {
  "${MYSQL[@]}" --skip-column-names -e "$1"
}

run_sql() {
  local title="$1"
  log "$title"
  "${MYSQL[@]}" 2>&1 | tee -a "$LOG_FILE"
}

ensure_column() {
  local table_name="$1"
  local column_name="$2"
  local column_ddl="$3"
  local exists
  exists="$(scalar "
SELECT COUNT(*)
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA='${DB_NAME}'
  AND TABLE_NAME='${table_name}'
  AND COLUMN_NAME='${column_name}';")"
  if [[ "$exists" == "0" ]]; then
    log "为 ${table_name} 增加字段 ${column_name}"
    "${MYSQL[@]}" -e "ALTER TABLE ${table_name} ADD COLUMN ${column_ddl};" \
      2>&1 | tee -a "$LOG_FILE"
  fi
}

log "创建/确认归因表"
"${MYSQL[@]}" < "$SCRIPT_DIR/create_tables.sql" 2>&1 | tee -a "$LOG_FILE"

ensure_column dwd_amz_ad_order_allocation allocation_level \
  "allocation_level VARCHAR(20) NULL COMMENT 'STORE_MONTH' AFTER estimated_ad_flag"
ensure_column dwd_amz_ad_order_allocation allocation_confidence \
  "allocation_confidence VARCHAR(10) NULL COMMENT 'LOW' AFTER allocation_level"
ensure_column dwd_amz_order_attribution_result ad_allocation_level \
  "ad_allocation_level VARCHAR(20) NULL AFTER estimated_ad_flag"
ensure_column dwd_amz_order_attribution_result ad_allocation_confidence \
  "ad_allocation_confidence VARCHAR(10) NULL AFTER ad_allocation_level"

run_sql "创建/确认产品表现月度广告需求表与审计表" <<'SQL'
CREATE TABLE IF NOT EXISTS dws_amz_ad_order_demand_monthly (
    order_month DATE NOT NULL,
    store_name VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    msku_count BIGINT NOT NULL DEFAULT 0,
    product_order_items BIGINT NOT NULL DEFAULT 0,
    ad_order_quantity BIGINT NOT NULL DEFAULT 0,
    promotion_order_items BIGINT NOT NULL DEFAULT 0,
    product_units BIGINT NOT NULL DEFAULT 0,
    product_sales DECIMAL(20,4) NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (order_month, store_name),
    KEY idx_product_ad_store_month (store_name, order_month)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
  COMMENT='产品表现接口店铺月度广告订单需求';

CREATE TABLE IF NOT EXISTS dws_amz_product_performance_ad_monthly_audit (
    order_month DATE NOT NULL,
    store_name VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    product_order_items BIGINT NOT NULL DEFAULT 0,
    product_ad_order_target BIGINT NOT NULL DEFAULT 0,
    product_promotion_orders BIGINT NOT NULL DEFAULT 0,
    msku_count BIGINT NOT NULL DEFAULT 0,
    base_order_sku_count BIGINT NOT NULL DEFAULT 0,
    offsite_order_sku_count BIGINT NOT NULL DEFAULT 0,
    non_offsite_capacity BIGINT NOT NULL DEFAULT 0,
    allocated_ad_orders BIGINT NOT NULL DEFAULT 0,
    unallocated_ad_orders BIGINT NOT NULL DEFAULT 0,
    allocation_pct DECIMAL(10,4) NOT NULL DEFAULT 0,
    source_gap_order_sku BIGINT NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (order_month, store_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
  COMMENT='产品表现广告订单月度统计分配审计';
SQL

index_exists="$(scalar "
SELECT COUNT(*)
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA='${DB_NAME}'
  AND TABLE_NAME='ods_amz_all_orders_report'
  AND INDEX_NAME='idx_store_purchase_date';")"

if [[ "$index_exists" == "0" ]]; then
  log "源订单表缺少 idx_store_purchase_date，开始创建索引（仅首次较慢）"
  "${MYSQL[@]}" -e "ALTER TABLE ods_amz_all_orders_report
    ADD KEY idx_store_purchase_date (store_name, purchase_date_utc);" \
    2>&1 | tee -a "$LOG_FILE"
else
  log "源订单索引 idx_store_purchase_date 已存在"
fi

if [[ -n "${ATTR_START_MONTH:-}" ]]; then
  START_MONTH="$ATTR_START_MONTH"
else
  START_MONTH="$(scalar "SELECT DATE_FORMAT(MIN(report_month),'%Y-%m-01')
    FROM ods_lx_product_performance_monthly_msku;")"
fi

if [[ -n "${ATTR_END_MONTH_EXCLUSIVE:-}" ]]; then
  END_MONTH_EXCLUSIVE="$ATTR_END_MONTH_EXCLUSIVE"
else
  END_MONTH_EXCLUSIVE="$(scalar "SELECT DATE_FORMAT(
      DATE_ADD(MAX(report_month),INTERVAL 1 MONTH),'%Y-%m-01')
    FROM ods_lx_product_performance_monthly_msku;")"
fi

START_MONTH="${START_MONTH//[[:space:]]/}"
END_MONTH_EXCLUSIVE="${END_MONTH_EXCLUSIVE//[[:space:]]/}"

if [[ -n "${ATTR_STORES:-}" ]]; then
  IFS=',' read -r -a STORES <<< "$ATTR_STORES"
else
  mapfile -t STORES < <(scalar "
SELECT DISTINCT CAST(store_name AS CHAR CHARACTER SET utf8mb4)
FROM ods_lx_product_performance_monthly_msku
ORDER BY store_name;")
fi

STORE_SQL_LIST=""
NORMALIZED_STORES=()
for store_raw in "${STORES[@]}"; do
  store="$(echo "$store_raw" | xargs)"
  [[ -z "$store" ]] && continue
  NORMALIZED_STORES+=("$store")
  escaped_store="${store//\'/\'\'}"
  if [[ -n "$STORE_SQL_LIST" ]]; then
    STORE_SQL_LIST+=","
  fi
  STORE_SQL_LIST+="'$escaped_store'"
done
STORES=("${NORMALIZED_STORES[@]}")

if [[ -z "$START_MONTH" || -z "$END_MONTH_EXCLUSIVE" || ${#STORES[@]} -eq 0 ]]; then
  log "无法确定月份或店铺，终止"
  exit 1
fi

log "产品表现月度归因范围：$START_MONTH 至 $END_MONTH_EXCLUSIVE（结束月不含）；店铺：${STORES[*]}"

month="$START_MONTH"
while [[ "$month" < "$END_MONTH_EXCLUSIVE" ]]; do
  next_month="$(date -d "$month +1 month" '+%Y-%m-01')"
  log "========== 产品表现月度归因开始月份 $month =========="

  run_sql "[$month] 重建产品表现月度广告需求" <<SQL
DELETE FROM dws_amz_ad_order_demand_monthly
WHERE order_month='$month';

INSERT INTO dws_amz_ad_order_demand_monthly (
  order_month,store_name,msku_count,product_order_items,
  ad_order_quantity,promotion_order_items,product_units,product_sales
)
SELECT
  report_month,
  CAST(store_name AS CHAR CHARACTER SET utf8mb4) COLLATE utf8mb4_bin,
  COUNT(*),
  SUM(COALESCE(order_items,0)),
  SUM(COALESCE(ad_order_quantity,0)),
  SUM(COALESCE(promotion_order_items,0)),
  SUM(COALESCE(volume,0)),
  SUM(COALESCE(amount,0))
FROM ods_lx_product_performance_monthly_msku
WHERE report_month='$month'
  AND store_name IN ($STORE_SQL_LIST)
GROUP BY
  report_month,
  CAST(store_name AS CHAR CHARACTER SET utf8mb4) COLLATE utf8mb4_bin;
SQL

  for store in "${STORES[@]}"; do
    escaped_store="${store//\'/\'\'}"

    run_sql "[$month][$store] 重建订单归因基础表" <<SQL
DELETE FROM dwd_amz_order_attribution_base
WHERE order_month='$month'
  AND store_name='$escaped_store' COLLATE utf8mb4_bin;

INSERT INTO dwd_amz_order_attribution_base (
  source_id,store_name,order_date,order_month,purchase_date_utc,
  amazon_order_id,order_item_id,sku,sku_key,asin,quantity,currency,
  gross_item_sales,item_promotion_discount,net_item_sales,net_unit_price,
  promotion_ids,offsite_flag,shipping_promotion_flag,
  onsite_promotion_flag,low_price_candidate_flag
)
SELECT
  n.source_id,n.store_name,n.order_date,'$month',n.purchase_date_utc,
  n.amazon_order_id,n.order_item_id,n.sku,n.sku_key,n.asin,n.quantity,n.currency,
  n.gross_item_sales,n.item_promotion_discount,n.net_item_sales,n.net_unit_price,
  n.promotion_ids,
  n.promo_upper LIKE '%MPC-%',
  n.promo_upper LIKE '%FREE SHIPPING%' OR n.promo_upper LIKE '%A3JU1FCINF5SD0%',
  n.promo_upper NOT LIKE '%MPC-%' AND (
    n.promo_upper LIKE '%PERCENTAGE OFF%'
    OR n.promo_upper LIKE '%PLM-%'
    OR n.item_promotion_discount>0
  ),
  n.promo_upper NOT LIKE '%MPC-%'
    AND NOT (
      n.promo_upper LIKE '%PERCENTAGE OFF%'
      OR n.promo_upper LIKE '%PLM-%'
      OR n.item_promotion_discount>0
    )
    AND n.net_unit_price<=10.000000
FROM (
  SELECT
    id AS source_id,
    CAST(store_name AS CHAR CHARACTER SET utf8mb4) COLLATE utf8mb4_bin AS store_name,
    DATE(CONVERT_TZ(purchase_date_utc,'UTC','America/Los_Angeles')) AS order_date,
    purchase_date_utc,
    CAST(amazon_order_id AS CHAR CHARACTER SET utf8mb4) COLLATE utf8mb4_bin AS amazon_order_id,
    CAST(COALESCE(order_item_id,'') AS CHAR CHARACTER SET utf8mb4)
      COLLATE utf8mb4_bin AS order_item_id,
    sku,
    UPPER(TRIM(sku)) COLLATE utf8mb4_bin AS sku_key,
    asin,quantity,currency,
    COALESCE(item_price,0) AS gross_item_sales,
    COALESCE(item_promotion_discount,0) AS item_promotion_discount,
    COALESCE(item_price,0)-COALESCE(item_promotion_discount,0) AS net_item_sales,
    (COALESCE(item_price,0)-COALESCE(item_promotion_discount,0))/quantity
      AS net_unit_price,
    promotion_ids,
    UPPER(COALESCE(promotion_ids,'')) AS promo_upper
  FROM ods_amz_all_orders_report
  WHERE store_name='$escaped_store'
    AND purchase_date_utc >= CONVERT_TZ('$month 00:00:00',
      'America/Los_Angeles','UTC')
    AND purchase_date_utc < CONVERT_TZ('$next_month 00:00:00',
      'America/Los_Angeles','UTC')
    AND sales_channel='Amazon.com'
    AND order_status='Shipped'
    AND item_status='Shipped'
    AND quantity>0
    AND item_price>0
    AND currency='USD'
    AND sku IS NOT NULL
    AND TRIM(sku)<>''
    AND purchase_date_utc IS NOT NULL
) n;
SQL
  done

  run_sql "[$month] 按店铺月份分配产品表现广告订单" <<SQL
DELETE FROM dwd_amz_ad_order_allocation
WHERE order_month='$month';

INSERT INTO dwd_amz_ad_order_allocation (
  store_name,order_date,order_month,sku_key,amazon_order_id,
  first_purchase_date_utc,candidate_rank,candidate_orders,
  sp_orders,sd_orders,ad_orders_raw,allocated_ad_orders,
  unallocated_ad_orders,estimated_ad_flag,
  allocation_level,allocation_confidence,allocation_version
)
SELECT
  r.store_name,r.order_date,'$month',r.sku_key,r.amazon_order_id,
  r.first_purchase_date_utc,r.candidate_rank,r.candidate_orders,
  0,0,1,1,0,1,
  'STORE_MONTH','LOW','v3_product_month_20260727'
FROM (
  SELECT
    c.*,
    ROW_NUMBER() OVER (
      PARTITION BY c.store_name
      ORDER BY c.first_purchase_date_utc,c.amazon_order_id,c.sku_key,c.order_date
    ) AS candidate_rank,
    COUNT(*) OVER (PARTITION BY c.store_name) AS candidate_orders
  FROM (
    SELECT
      b.store_name,b.order_date,b.sku_key,b.amazon_order_id,
      MIN(b.purchase_date_utc) AS first_purchase_date_utc
    FROM dwd_amz_order_attribution_base b
    WHERE b.order_month='$month'
      AND b.offsite_flag=0
    GROUP BY b.store_name,b.order_date,b.sku_key,b.amazon_order_id
  ) c
) r
INNER JOIN dws_amz_ad_order_demand_monthly d
  ON d.order_month='$month'
 AND d.store_name=r.store_name
WHERE r.candidate_rank <= LEAST(d.ad_order_quantity,r.candidate_orders);
SQL

  run_sql "[$month] 重建五分类明细（产品表现月度广告）" <<SQL
DELETE FROM dwd_amz_order_attribution_result
WHERE order_month='$month';

INSERT INTO dwd_amz_order_attribution_result (
  source_id,store_name,order_date,order_month,amazon_order_id,order_item_id,
  sku,sku_key,asin,quantity,gross_item_sales,net_item_sales,net_unit_price,
  promotion_ids,offsite_flag,estimated_ad_flag,
  ad_allocation_level,ad_allocation_confidence,
  onsite_promotion_flag,low_price_flag,main_order_type,
  classification_reason,candidate_rank,ad_orders_raw,rule_version
)
SELECT
  b.source_id,b.store_name,b.order_date,b.order_month,b.amazon_order_id,
  b.order_item_id,b.sku,b.sku_key,b.asin,b.quantity,b.gross_item_sales,
  b.net_item_sales,b.net_unit_price,b.promotion_ids,b.offsite_flag,
  COALESCE(a.estimated_ad_flag,0),
  a.allocation_level,a.allocation_confidence,
  b.onsite_promotion_flag,
  b.offsite_flag=0
    AND COALESCE(a.estimated_ad_flag,0)=0
    AND b.onsite_promotion_flag=0
    AND b.low_price_candidate_flag=1,
  CASE
    WHEN b.offsite_flag=1 THEN '站外推广'
    WHEN COALESCE(a.estimated_ad_flag,0)=1 THEN '广告'
    WHEN b.onsite_promotion_flag=1 THEN '站内促销'
    WHEN b.low_price_candidate_flag=1 THEN '低价'
    ELSE '自然'
  END,
  CASE
    WHEN b.offsite_flag=1 THEN 'promotion_ids命中MPC-'
    WHEN COALESCE(a.estimated_ad_flag,0)=1
      THEN '产品表现ad_order_quantity按店铺+月份统计分配'
    WHEN b.onsite_promotion_flag=1
      THEN 'Percentage Off、PLM或item_promotion_discount>0'
    WHEN b.low_price_candidate_flag=1
      THEN '排除前三类后净成交单价<=10美元'
    ELSE '未命中站外、广告、站内促销或低价规则'
  END,
  a.candidate_rank,COALESCE(a.ad_orders_raw,0),'v3_product_month_20260727'
FROM dwd_amz_order_attribution_base b
LEFT JOIN dwd_amz_ad_order_allocation a
  ON a.store_name=b.store_name
 AND a.order_date=b.order_date
 AND a.sku_key=b.sku_key
 AND a.amazon_order_id=b.amazon_order_id
 AND a.order_month='$month'
WHERE b.order_month='$month';
SQL

  run_sql "[$month] 重建月度五分类汇总" <<SQL
DELETE FROM dws_amz_order_source_monthly
WHERE order_month='$month';

INSERT INTO dws_amz_order_source_monthly (
  order_month,store_name,main_order_type,classified_item_rows,
  order_sku_count,amazon_order_count,units,gross_item_sales,net_item_sales
)
SELECT
  order_month,store_name,main_order_type,COUNT(*),
  COUNT(DISTINCT amazon_order_id,sku_key),
  COUNT(DISTINCT amazon_order_id),SUM(quantity),
  SUM(gross_item_sales),SUM(net_item_sales)
FROM dwd_amz_order_attribution_result
WHERE order_month='$month'
GROUP BY order_month,store_name,main_order_type;
SQL

  run_sql "[$month] 重建产品表现广告分配审计" <<SQL
DELETE FROM dws_amz_product_performance_ad_monthly_audit
WHERE order_month='$month';

INSERT INTO dws_amz_product_performance_ad_monthly_audit (
  order_month,store_name,product_order_items,product_ad_order_target,
  product_promotion_orders,msku_count,base_order_sku_count,
  offsite_order_sku_count,non_offsite_capacity,allocated_ad_orders,
  unallocated_ad_orders,allocation_pct,source_gap_order_sku
)
SELECT
  d.order_month,d.store_name,
  d.product_order_items,d.ad_order_quantity,d.promotion_order_items,d.msku_count,
  COALESCE(b.base_order_sku_count,0),
  COALESCE(b.offsite_order_sku_count,0),
  COALESCE(b.non_offsite_capacity,0),
  COALESCE(a.allocated_ad_orders,0),
  GREATEST(d.ad_order_quantity-COALESCE(a.allocated_ad_orders,0),0),
  ROUND(
    COALESCE(a.allocated_ad_orders,0)/NULLIF(d.ad_order_quantity,0)*100,
    4
  ),
  d.product_order_items-COALESCE(b.base_order_sku_count,0)
FROM dws_amz_ad_order_demand_monthly d
LEFT JOIN (
  SELECT
    x.store_name,
    COUNT(*) AS base_order_sku_count,
    SUM(x.offsite_flag=1) AS offsite_order_sku_count,
    SUM(x.offsite_flag=0) AS non_offsite_capacity
  FROM (
    SELECT
      store_name,amazon_order_id,sku_key,MAX(offsite_flag) AS offsite_flag
    FROM dwd_amz_order_attribution_base
    WHERE order_month='$month'
    GROUP BY store_name,amazon_order_id,sku_key
  ) x
  GROUP BY x.store_name
) b ON b.store_name=d.store_name
LEFT JOIN (
  SELECT store_name,COUNT(*) AS allocated_ad_orders
  FROM dwd_amz_ad_order_allocation
  WHERE order_month='$month'
  GROUP BY store_name
) a ON a.store_name=d.store_name
WHERE d.order_month='$month';
SQL

  log "[$month] 产品表现广告分配审计"
  "${MYSQL[@]}" --table -e "
SELECT *
FROM dws_amz_product_performance_ad_monthly_audit
WHERE order_month='$month'
ORDER BY store_name;
" 2>&1 | tee -a "$LOG_FILE"

  log "[$month] 五分类结果"
  "${MYSQL[@]}" --table -e "
SELECT
  order_month,store_name,main_order_type,order_sku_count,
  ROUND(order_sku_count/SUM(order_sku_count) OVER (
    PARTITION BY order_month,store_name)*100,2) AS order_sku_pct,
  units,ROUND(net_item_sales,2) AS net_item_sales
FROM dws_amz_order_source_monthly
WHERE order_month='$month'
ORDER BY store_name,
  FIELD(main_order_type,'广告','站外推广','站内促销','低价','自然');
" 2>&1 | tee -a "$LOG_FILE"

  month="$next_month"
done

log "全部月份产品表现月度归因处理完成"
