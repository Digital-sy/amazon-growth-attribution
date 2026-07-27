#!/usr/bin/env bash
set -Eeuo pipefail

# Amazon 五分类归因：三级广告统计分配。
# 1. 店铺+当地日期+SKU（高置信度）
# 2. 店铺+月份+SKU（中置信度）
# 3. 店铺+月份（低置信度，仅用于月度来源结构）
#
# 广告订单目标采用保守口径：每个店铺+日期+SKU取 MAX(SP orders, SD orders)，
# 避免SP与SD可能存在的重复归因。

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

# 先确保基础表存在。
"${MYSQL[@]}" < "$SCRIPT_DIR/create_tables.sql" 2>&1 | tee -a "$LOG_FILE"

ensure_column dwd_amz_ad_order_allocation allocation_level \
  "allocation_level VARCHAR(20) NULL COMMENT 'DAY_SKU/MONTH_SKU/STORE_MONTH' AFTER estimated_ad_flag"
ensure_column dwd_amz_ad_order_allocation allocation_confidence \
  "allocation_confidence VARCHAR(10) NULL COMMENT 'HIGH/MEDIUM/LOW' AFTER allocation_level"
ensure_column dwd_amz_order_attribution_result ad_allocation_level \
  "ad_allocation_level VARCHAR(20) NULL AFTER estimated_ad_flag"
ensure_column dwd_amz_order_attribution_result ad_allocation_confidence \
  "ad_allocation_confidence VARCHAR(10) NULL AFTER ad_allocation_level"

run_sql "创建/确认广告分配月度审计表" <<'SQL'
CREATE TABLE IF NOT EXISTS dws_amz_ad_allocation_monthly_audit (
    order_month DATE NOT NULL,
    store_name VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    raw_ad_orders BIGINT NOT NULL DEFAULT 0 COMMENT 'SP+SD原始订单数',
    conservative_target_orders BIGINT NOT NULL DEFAULT 0 COMMENT '逐日SKU取MAX(SP,SD)后求和',
    day_sku_allocated BIGINT NOT NULL DEFAULT 0,
    month_sku_allocated BIGINT NOT NULL DEFAULT 0,
    store_month_allocated BIGINT NOT NULL DEFAULT 0,
    total_allocated BIGINT NOT NULL DEFAULT 0,
    unallocated_orders BIGINT NOT NULL DEFAULT 0,
    allocation_pct DECIMAL(10,4) NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (order_month, store_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
  COMMENT='广告订单三级统计分配月度审计';
SQL

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
  STORES_CSV="$ATTR_STORES"
else
  STORES_CSV="$(scalar "SELECT GROUP_CONCAT(DISTINCT store_name ORDER BY store_name)
    FROM lingxing_ad_spend_daily WHERE ad_type IN ('SP','SD');")"
fi
STORES_CSV="${STORES_CSV//[[:space:]]/}"

if [[ -z "$START_MONTH" || -z "$END_MONTH_EXCLUSIVE" || -z "$STORES_CSV" ]]; then
  log "无法确定月份或店铺，终止"
  exit 1
fi

log "三级归因范围：$START_MONTH 至 $END_MONTH_EXCLUSIVE（结束月不含）；店铺：$STORES_CSV"

month="$START_MONTH"
while [[ "$month" < "$END_MONTH_EXCLUSIVE" ]]; do
  next_month="$(date -d "$month +1 month" '+%Y-%m-01')"
  log "========== 三级归因开始月份 $month =========="

  # 复用v2生成该月订单基础表和广告日需求表。随后覆盖其广告分配和最终分类。
  ATTR_START_MONTH="$month" \
  ATTR_END_MONTH_EXCLUSIVE="$next_month" \
  ATTR_STORES="$STORES_CSV" \
  bash "$SCRIPT_DIR/run_attribution_pipeline_v2.sh"

  run_sql "[$month] 清理旧广告分配" <<SQL
DELETE FROM dwd_amz_ad_order_allocation WHERE order_month='$month';
SQL

  # 第一级：同日同SKU，高置信度。
  run_sql "[$month] 第一级分配：同日同SKU" <<SQL
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
  d.sp_orders,d.sd_orders,d.ad_orders_raw,
  1,0,1,'DAY_SKU','HIGH','v2_hybrid_20260727'
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
     AND d.order_month='$month'
    WHERE b.order_month='$month' AND b.offsite_flag=0
    GROUP BY b.store_name,b.order_date,b.sku_key,b.amazon_order_id
  ) c
) r
INNER JOIN dws_amz_ad_order_demand_daily d
  ON d.store_name=r.store_name
 AND d.report_date=r.order_date
 AND d.sku_key=r.sku_key
 AND d.order_month='$month'
WHERE r.candidate_rank <= GREATEST(d.sp_orders,d.sd_orders);
SQL

  # 第二级：同月同SKU，中置信度。只分配第一级未选中的订单。
  run_sql "[$month] 第二级分配：同月同SKU" <<SQL
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
  t.sp_orders,t.sd_orders,t.raw_ad_orders,
  1,0,1,'MONTH_SKU','MEDIUM','v2_hybrid_20260727'
FROM (
  SELECT c.*,
    ROW_NUMBER() OVER (
      PARTITION BY c.store_name,c.sku_key
      ORDER BY c.first_purchase_date_utc,c.amazon_order_id,c.order_date
    ) AS candidate_rank,
    COUNT(*) OVER (
      PARTITION BY c.store_name,c.sku_key
    ) AS candidate_orders
  FROM (
    SELECT b.store_name,b.order_date,b.sku_key,b.amazon_order_id,
      MIN(b.purchase_date_utc) AS first_purchase_date_utc
    FROM dwd_amz_order_attribution_base b
    LEFT JOIN dwd_amz_ad_order_allocation x
      ON x.store_name=b.store_name
     AND x.order_date=b.order_date
     AND x.sku_key=b.sku_key
     AND x.amazon_order_id=b.amazon_order_id
     AND x.order_month='$month'
    WHERE b.order_month='$month'
      AND b.offsite_flag=0
      AND x.amazon_order_id IS NULL
    GROUP BY b.store_name,b.order_date,b.sku_key,b.amazon_order_id
  ) c
) r
INNER JOIN (
  SELECT
    d.store_name,d.sku_key,
    SUM(d.sp_orders) AS sp_orders,
    SUM(d.sd_orders) AS sd_orders,
    SUM(d.ad_orders_raw) AS raw_ad_orders,
    SUM(GREATEST(d.sp_orders,d.sd_orders)) AS target_orders,
    COALESCE(a.already_allocated,0) AS already_allocated,
    GREATEST(
      SUM(GREATEST(d.sp_orders,d.sd_orders))-COALESCE(a.already_allocated,0),0
    ) AS residual_orders
  FROM dws_amz_ad_order_demand_daily d
  LEFT JOIN (
    SELECT store_name,sku_key,COUNT(*) AS already_allocated
    FROM dwd_amz_ad_order_allocation
    WHERE order_month='$month'
    GROUP BY store_name,sku_key
  ) a
    ON a.store_name=d.store_name AND a.sku_key=d.sku_key
  WHERE d.order_month='$month'
  GROUP BY d.store_name,d.sku_key,a.already_allocated
) t
  ON t.store_name=r.store_name AND t.sku_key=r.sku_key
WHERE r.candidate_rank <= t.residual_orders;
SQL

  # 第三级：店铺月度兜底，低置信度。只用于月度五类结构。
  run_sql "[$month] 第三级分配：店铺月份兜底" <<SQL
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
  t.sp_orders,t.sd_orders,t.raw_ad_orders,
  1,0,1,'STORE_MONTH','LOW','v2_hybrid_20260727'
FROM (
  SELECT c.*,
    ROW_NUMBER() OVER (
      PARTITION BY c.store_name
      ORDER BY c.first_purchase_date_utc,c.amazon_order_id,c.sku_key
    ) AS candidate_rank,
    COUNT(*) OVER (PARTITION BY c.store_name) AS candidate_orders
  FROM (
    SELECT b.store_name,b.order_date,b.sku_key,b.amazon_order_id,
      MIN(b.purchase_date_utc) AS first_purchase_date_utc
    FROM dwd_amz_order_attribution_base b
    LEFT JOIN dwd_amz_ad_order_allocation x
      ON x.store_name=b.store_name
     AND x.order_date=b.order_date
     AND x.sku_key=b.sku_key
     AND x.amazon_order_id=b.amazon_order_id
     AND x.order_month='$month'
    WHERE b.order_month='$month'
      AND b.offsite_flag=0
      AND x.amazon_order_id IS NULL
    GROUP BY b.store_name,b.order_date,b.sku_key,b.amazon_order_id
  ) c
) r
INNER JOIN (
  SELECT
    d.store_name,
    SUM(d.sp_orders) AS sp_orders,
    SUM(d.sd_orders) AS sd_orders,
    SUM(d.ad_orders_raw) AS raw_ad_orders,
    SUM(GREATEST(d.sp_orders,d.sd_orders)) AS target_orders,
    COALESCE(a.already_allocated,0) AS already_allocated,
    GREATEST(
      SUM(GREATEST(d.sp_orders,d.sd_orders))-COALESCE(a.already_allocated,0),0
    ) AS residual_orders
  FROM dws_amz_ad_order_demand_daily d
  LEFT JOIN (
    SELECT store_name,COUNT(*) AS already_allocated
    FROM dwd_amz_ad_order_allocation
    WHERE order_month='$month'
    GROUP BY store_name
  ) a ON a.store_name=d.store_name
  WHERE d.order_month='$month'
  GROUP BY d.store_name,a.already_allocated
) t ON t.store_name=r.store_name
WHERE r.candidate_rank <= t.residual_orders;
SQL

  run_sql "[$month] 重建五分类明细（三级广告归因）" <<SQL
DELETE FROM dwd_amz_order_attribution_result WHERE order_month='$month';

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
  COALESCE(a.estimated_ad_flag,0),a.allocation_level,a.allocation_confidence,
  b.onsite_promotion_flag,
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
    WHEN a.allocation_level='DAY_SKU' THEN '按店铺+当地日期+SKU分配SP/SD广告订单'
    WHEN a.allocation_level='MONTH_SKU' THEN '按店铺+月份+SKU补充分配广告订单'
    WHEN a.allocation_level='STORE_MONTH' THEN '按店铺+月份低置信度兜底广告订单'
    WHEN b.onsite_promotion_flag=1
      THEN 'Percentage Off、PLM或item_promotion_discount>0'
    WHEN b.low_price_candidate_flag=1
      THEN '排除前三类后净成交单价<=10美元'
    ELSE '未命中站外、广告、站内促销或低价规则'
  END,
  a.candidate_rank,COALESCE(a.ad_orders_raw,0),'v2_hybrid_20260727'
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
DELETE FROM dws_amz_order_source_monthly WHERE order_month='$month';
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

  run_sql "[$month] 重建广告分配审计" <<SQL
DELETE FROM dws_amz_ad_allocation_monthly_audit WHERE order_month='$month';
INSERT INTO dws_amz_ad_allocation_monthly_audit (
  order_month,store_name,raw_ad_orders,conservative_target_orders,
  day_sku_allocated,month_sku_allocated,store_month_allocated,
  total_allocated,unallocated_orders,allocation_pct
)
SELECT
  '$month',d.store_name,
  SUM(d.ad_orders_raw),
  SUM(GREATEST(d.sp_orders,d.sd_orders)),
  COALESCE(a.day_sku_allocated,0),
  COALESCE(a.month_sku_allocated,0),
  COALESCE(a.store_month_allocated,0),
  COALESCE(a.total_allocated,0),
  GREATEST(SUM(GREATEST(d.sp_orders,d.sd_orders))-COALESCE(a.total_allocated,0),0),
  ROUND(
    COALESCE(a.total_allocated,0) /
    NULLIF(SUM(GREATEST(d.sp_orders,d.sd_orders)),0) * 100,4
  )
FROM dws_amz_ad_order_demand_daily d
LEFT JOIN (
  SELECT store_name,
    SUM(allocation_level='DAY_SKU') AS day_sku_allocated,
    SUM(allocation_level='MONTH_SKU') AS month_sku_allocated,
    SUM(allocation_level='STORE_MONTH') AS store_month_allocated,
    COUNT(*) AS total_allocated
  FROM dwd_amz_ad_order_allocation
  WHERE order_month='$month'
  GROUP BY store_name
) a ON a.store_name=d.store_name
WHERE d.order_month='$month'
GROUP BY d.store_name,a.day_sku_allocated,a.month_sku_allocated,
  a.store_month_allocated,a.total_allocated;
SQL

  log "[$month] 三级广告分配审计"
  "${MYSQL[@]}" --table -e "
SELECT * FROM dws_amz_ad_allocation_monthly_audit
WHERE order_month='$month' ORDER BY store_name;
" 2>&1 | tee -a "$LOG_FILE"

  log "[$month] 五分类结果"
  "${MYSQL[@]}" --table -e "
SELECT order_month,store_name,main_order_type,order_sku_count,
  ROUND(order_sku_count/SUM(order_sku_count) OVER (
    PARTITION BY order_month,store_name)*100,2) AS order_sku_pct,
  units,ROUND(net_item_sales,2) AS net_item_sales
FROM dws_amz_order_source_monthly
WHERE order_month='$month'
ORDER BY store_name,
  FIELD(main_order_type,'站外推广','广告','站内促销','低价','自然');
" 2>&1 | tee -a "$LOG_FILE"

  month="$next_month"
done

log "全部月份三级归因处理完成"
