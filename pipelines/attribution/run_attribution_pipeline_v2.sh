#!/usr/bin/env bash
set -Eeuo pipefail

# Amazon订单五分类归因：按月、按店铺可重跑。
# 可选：
#   ATTR_START_MONTH=2025-01-01
#   ATTR_END_MONTH_EXCLUSIVE=2025-02-01
#   ATTR_STORES='JQ-US,MT-US,RKZ-US,SY-US'

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SQL_DIR="$PROJECT_ROOT/pipelines/attribution"
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

log "创建/确认归因表"
"${MYSQL[@]}" < "$SQL_DIR/create_tables.sql" 2>&1 | tee -a "$LOG_FILE"

index_exists="$(scalar "
SELECT COUNT(*) FROM information_schema.STATISTICS
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
  START_MONTH="$(scalar "SELECT DATE_FORMAT(MIN(report_date),'%Y-%m-01')
    FROM lingxing_ad_spend_daily WHERE ad_type IN ('SP','SD');")"
fi

if [[ -n "${ATTR_END_MONTH_EXCLUSIVE:-}" ]]; then
  END_MONTH_EXCLUSIVE="$ATTR_END_MONTH_EXCLUSIVE"
else
  END_MONTH_EXCLUSIVE="$(scalar "SELECT DATE_FORMAT(
      DATE_ADD(DATE_FORMAT(MAX(report_date),'%Y-%m-01'),INTERVAL 1 MONTH),
      '%Y-%m-01')
    FROM lingxing_ad_spend_daily WHERE ad_type IN ('SP','SD');")"
fi

START_MONTH="${START_MONTH//[[:space:]]/}"
END_MONTH_EXCLUSIVE="${END_MONTH_EXCLUSIVE//[[:space:]]/}"

if [[ -n "${ATTR_STORES:-}" ]]; then
  IFS=',' read -r -a STORES <<< "$ATTR_STORES"
else
  mapfile -t STORES < <(scalar "
SELECT DISTINCT CAST(store_name AS CHAR CHARACTER SET utf8mb4)
FROM lingxing_ad_spend_daily
WHERE ad_type IN ('SP','SD')
ORDER BY store_name;")
fi

if [[ -z "$START_MONTH" || -z "$END_MONTH_EXCLUSIVE" || ${#STORES[@]} -eq 0 ]]; then
  log "无法确定月份或店铺，终止"
  exit 1
fi

log "处理范围：$START_MONTH 至 $END_MONTH_EXCLUSIVE（结束月不含）；店铺：${STORES[*]}"

month="$START_MONTH"
while [[ "$month" < "$END_MONTH_EXCLUSIVE" ]]; do
  next_month="$(date -d "$month +1 month" '+%Y-%m-01')"
  log "========== 开始月份 $month =========="

  run_sql "[$month] 重建广告订单日需求" <<SQL
DELETE FROM dws_amz_ad_order_demand_daily WHERE order_month='$month';

INSERT INTO dws_amz_ad_order_demand_daily (
  store_name,report_date,order_month,sku_key,asin,
  sp_orders,sd_orders,ad_orders_raw,ad_sales,ad_cost,clicks,impressions,
  positive_ad_type_count,multi_ad_type_flag
)
SELECT
  CAST(store_name AS CHAR CHARACTER SET utf8mb4) COLLATE utf8mb4_bin,
  report_date,'$month',
  UPPER(TRIM(sku)) COLLATE utf8mb4_bin,
  MAX(NULLIF(asin,'')),
  SUM(CASE WHEN ad_type='SP' THEN COALESCE(orders,0) ELSE 0 END),
  SUM(CASE WHEN ad_type='SD' THEN COALESCE(orders,0) ELSE 0 END),
  SUM(COALESCE(orders,0)),SUM(COALESCE(sales,0)),SUM(COALESCE(cost,0)),
  SUM(COALESCE(clicks,0)),SUM(COALESCE(impressions,0)),
  COUNT(DISTINCT ad_type),COUNT(DISTINCT ad_type)>1
FROM lingxing_ad_spend_daily
WHERE report_date >= '$month' AND report_date < '$next_month'
  AND ad_type IN ('SP','SD') AND orders>0
  AND sku IS NOT NULL AND TRIM(sku)<>''
  AND UPPER(TRIM(sku))<>'__STORE__'
GROUP BY
  CAST(store_name AS CHAR CHARACTER SET utf8mb4) COLLATE utf8mb4_bin,
  report_date,UPPER(TRIM(sku)) COLLATE utf8mb4_bin;
SQL

  for store_raw in "${STORES[@]}"; do
    store="$(echo "$store_raw" | xargs)"
    [[ -z "$store" ]] && continue
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
    sku,UPPER(TRIM(sku)) COLLATE utf8mb4_bin AS sku_key,asin,quantity,currency,
    COALESCE(item_price,0) AS gross_item_sales,
    COALESCE(item_promotion_discount,0) AS item_promotion_discount,
    COALESCE(item_price,0)-COALESCE(item_promotion_discount,0) AS net_item_sales,
    (COALESCE(item_price,0)-COALESCE(item_promotion_discount,0))/quantity
      AS net_unit_price,
    promotion_ids,UPPER(COALESCE(promotion_ids,'')) AS promo_upper
  FROM ods_amz_all_orders_report
  WHERE store_name='$escaped_store'
    AND purchase_date_utc >= CONVERT_TZ('$month 00:00:00',
      'America/Los_Angeles','UTC')
    AND purchase_date_utc < CONVERT_TZ('$next_month 00:00:00',
      'America/Los_Angeles','UTC')
    AND sales_channel='Amazon.com'
    AND order_status='Shipped' AND item_status='Shipped'
    AND quantity>0 AND item_price>0 AND currency='USD'
    AND sku IS NOT NULL AND TRIM(sku)<>''
    AND purchase_date_utc IS NOT NULL
) n;
SQL
  done

  run_sql "[$month] 重建广告订单分配" <<SQL
DELETE FROM dwd_amz_ad_order_allocation WHERE order_month='$month';

INSERT INTO dwd_amz_ad_order_allocation (
  store_name,order_date,order_month,sku_key,amazon_order_id,
  first_purchase_date_utc,candidate_rank,candidate_orders,
  sp_orders,sd_orders,ad_orders_raw,allocated_ad_orders,
  unallocated_ad_orders,estimated_ad_flag
)
SELECT
  r.store_name,r.order_date,'$month',r.sku_key,r.amazon_order_id,
  r.first_purchase_date_utc,r.candidate_rank,r.candidate_orders,
  d.sp_orders,d.sd_orders,d.ad_orders_raw,
  LEAST(d.ad_orders_raw,r.candidate_orders),
  GREATEST(d.ad_orders_raw-r.candidate_orders,0),
  r.candidate_rank<=d.ad_orders_raw
FROM (
  SELECT c.*,
    ROW_NUMBER() OVER (
      PARTITION BY c.store_name,c.order_date,c.sku_key
      ORDER BY c.first_purchase_date_utc,c.amazon_order_id
    ) AS candidate_rank,
    COUNT(*) OVER (
      PARTITION BY c.store_name,c.order_date,c.sku_key
    ) AS candidate_orders
  FROM (
    SELECT b.store_name,b.order_date,b.sku_key,b.amazon_order_id,
      MIN(b.purchase_date_utc) AS first_purchase_date_utc
    FROM dwd_amz_order_attribution_base b
    INNER JOIN dws_amz_ad_order_demand_daily d
      ON d.store_name=b.store_name
     AND d.report_date=b.order_date
     AND d.sku_key=b.sku_key
     AND d.order_month='$month' AND d.ad_orders_raw>0
    WHERE b.order_month='$month' AND b.offsite_flag=0
    GROUP BY b.store_name,b.order_date,b.sku_key,b.amazon_order_id
  ) c
) r
INNER JOIN dws_amz_ad_order_demand_daily d
  ON d.store_name=r.store_name
 AND d.report_date=r.order_date
 AND d.sku_key=r.sku_key
 AND d.order_month='$month';
SQL

  run_sql "[$month] 重建五分类明细" <<SQL
DELETE FROM dwd_amz_order_attribution_result WHERE order_month='$month';

INSERT INTO dwd_amz_order_attribution_result (
  source_id,store_name,order_date,order_month,amazon_order_id,order_item_id,
  sku,sku_key,asin,quantity,gross_item_sales,net_item_sales,net_unit_price,
  promotion_ids,offsite_flag,estimated_ad_flag,onsite_promotion_flag,
  low_price_flag,main_order_type,classification_reason,candidate_rank,ad_orders_raw
)
SELECT
  b.source_id,b.store_name,b.order_date,b.order_month,b.amazon_order_id,
  b.order_item_id,b.sku,b.sku_key,b.asin,b.quantity,b.gross_item_sales,
  b.net_item_sales,b.net_unit_price,b.promotion_ids,b.offsite_flag,
  COALESCE(a.estimated_ad_flag,0),b.onsite_promotion_flag,
  b.offsite_flag=0 AND COALESCE(a.estimated_ad_flag,0)=0
    AND b.onsite_promotion_flag=0 AND b.low_price_candidate_flag=1,
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
      THEN '按店铺+当地日期+SKU统计分配SP/SD广告订单'
    WHEN b.onsite_promotion_flag=1
      THEN 'Percentage Off、PLM或item_promotion_discount>0'
    WHEN b.low_price_candidate_flag=1
      THEN '排除前三类后净成交单价<=10美元'
    ELSE '未命中站外、广告、站内促销或低价规则'
  END,
  a.candidate_rank,COALESCE(a.ad_orders_raw,0)
FROM dwd_amz_order_attribution_base b
LEFT JOIN dwd_amz_ad_order_allocation a
  ON a.store_name=b.store_name
 AND a.order_date=b.order_date
 AND a.sku_key=b.sku_key
 AND a.amazon_order_id=b.amazon_order_id
WHERE b.order_month='$month';
SQL

  run_sql "[$month] 重建月度五分类汇总" <<SQL
DELETE FROM dws_amz_order_source_monthly WHERE order_month='$month';

INSERT INTO dws_amz_order_source_monthly (
  order_month,store_name,main_order_type,classified_item_rows,
  order_sku_count,amazon_order_count,units,gross_item_sales,net_item_sales
)
SELECT order_month,store_name,main_order_type,
  COUNT(*),COUNT(DISTINCT amazon_order_id,sku_key),
  COUNT(DISTINCT amazon_order_id),SUM(quantity),
  SUM(gross_item_sales),SUM(net_item_sales)
FROM dwd_amz_order_attribution_result
WHERE order_month='$month'
GROUP BY order_month,store_name,main_order_type;
SQL

  log "[$month] 分类结果"
  "${MYSQL[@]}" --table -e "
SELECT order_month,store_name,main_order_type,order_sku_count,units,
  ROUND(net_item_sales,2) AS net_item_sales
FROM dws_amz_order_source_monthly
WHERE order_month='$month'
ORDER BY store_name,
  FIELD(main_order_type,'站外推广','广告','站内促销','低价','自然');" \
  2>&1 | tee -a "$LOG_FILE"

  month="$next_month"
done

log "全部月份处理完成"
