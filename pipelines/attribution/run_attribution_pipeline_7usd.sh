#!/usr/bin/env bash
set -Eeuo pipefail

# Amazon 五分类归因生产入口（7美元低价规则）。
#
# 处理步骤：
# 1. 复用产品表现月度广告统计分配流程；
# 2. 将低价阈值统一为净成交单价 <= 7 USD；
# 3. 重算低价/自然分类；
# 4. 重建店铺月度五类来源汇总。
#
# 可选环境变量：
#   ATTR_START_MONTH=2025-01-01
#   ATTR_END_MONTH_EXCLUSIVE=2026-08-01
#   ATTR_STORES='JQ-US,MT-US,RKZ-US,SY-US'

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
LOW_PRICE_USD="7.000000"
RULE_VERSION="v4_7usd_20260728"
export MYSQL_PWD="$LINGXING_DB_PASSWORD"

MYSQL=(mysql -h "$LINGXING_DB_HOST" -P "$DB_PORT" -u "$LINGXING_DB_USER"
  "$DB_NAME" --default-character-set=utf8mb4 --batch --raw)

log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG_FILE"
}

scalar() {
  "${MYSQL[@]}" --skip-column-names -e "$1"
}

log "先执行产品表现月度广告统计分配"
bash "$SCRIPT_DIR/run_attribution_pipeline_product_monthly.sh" "$@"

if [[ -n "${ATTR_START_MONTH:-}" ]]; then
  START_MONTH="$ATTR_START_MONTH"
else
  START_MONTH="$(scalar "SELECT DATE_FORMAT(MIN(order_month),'%Y-%m-01') FROM dwd_amz_order_attribution_result;")"
fi

if [[ -n "${ATTR_END_MONTH_EXCLUSIVE:-}" ]]; then
  END_MONTH_EXCLUSIVE="$ATTR_END_MONTH_EXCLUSIVE"
else
  END_MONTH_EXCLUSIVE="$(scalar "SELECT DATE_FORMAT(DATE_ADD(MAX(order_month),INTERVAL 1 MONTH),'%Y-%m-01') FROM dwd_amz_order_attribution_result;")"
fi

START_MONTH="${START_MONTH//[[:space:]]/}"
END_MONTH_EXCLUSIVE="${END_MONTH_EXCLUSIVE//[[:space:]]/}"

if [[ -n "${ATTR_STORES:-}" ]]; then
  IFS=',' read -r -a STORES <<< "$ATTR_STORES"
else
  mapfile -t STORES < <(scalar "
SELECT DISTINCT CAST(store_name AS CHAR CHARACTER SET utf8mb4)
FROM dwd_amz_order_attribution_result
WHERE order_month >= '$START_MONTH'
  AND order_month < '$END_MONTH_EXCLUSIVE'
ORDER BY store_name;")
fi

STORE_SQL_LIST=""
NORMALIZED_STORES=()
for store_raw in "${STORES[@]}"; do
  store="$(echo "$store_raw" | xargs)"
  [[ -z "$store" ]] && continue
  NORMALIZED_STORES+=("$store")
  escaped_store="${store//\'/\'\'}"
  [[ -n "$STORE_SQL_LIST" ]] && STORE_SQL_LIST+=","
  STORE_SQL_LIST+="'$escaped_store'"
done
STORES=("${NORMALIZED_STORES[@]}")

if [[ -z "$START_MONTH" || -z "$END_MONTH_EXCLUSIVE" || -z "$STORE_SQL_LIST" ]]; then
  log "无法确定7美元规则重算范围，终止"
  exit 1
fi

log "按7美元规则重算：$START_MONTH 至 $END_MONTH_EXCLUSIVE（结束月不含）；店铺：${STORES[*]}"

"${MYSQL[@]}" <<SQL 2>&1 | tee -a "$LOG_FILE"
START TRANSACTION;

UPDATE dwd_amz_order_attribution_base
SET
  low_price_candidate_flag = (
    offsite_flag = 0
    AND onsite_promotion_flag = 0
    AND net_unit_price <= $LOW_PRICE_USD
  ),
  rule_version = '$RULE_VERSION'
WHERE order_month >= '$START_MONTH'
  AND order_month < '$END_MONTH_EXCLUSIVE'
  AND store_name IN ($STORE_SQL_LIST);

UPDATE dwd_amz_order_attribution_result
SET
  low_price_flag = (
    offsite_flag = 0
    AND COALESCE(estimated_ad_flag,0) = 0
    AND onsite_promotion_flag = 0
    AND net_unit_price <= $LOW_PRICE_USD
  ),
  main_order_type = CASE
    WHEN offsite_flag = 1 THEN '站外推广'
    WHEN COALESCE(estimated_ad_flag,0) = 1 THEN '广告'
    WHEN onsite_promotion_flag = 1 THEN '站内促销'
    WHEN net_unit_price <= $LOW_PRICE_USD THEN '低价'
    ELSE '自然'
  END,
  classification_reason = CASE
    WHEN offsite_flag = 1 THEN 'promotion_ids命中MPC-'
    WHEN COALESCE(estimated_ad_flag,0) = 1
      THEN '产品表现ad_order_quantity按店铺+月份统计分配'
    WHEN onsite_promotion_flag = 1
      THEN 'Percentage Off、PLM或item_promotion_discount>0'
    WHEN net_unit_price <= $LOW_PRICE_USD
      THEN '排除前三类后净成交单价<=7美元'
    ELSE '未命中站外、广告、站内促销，且净成交单价>7美元'
  END,
  rule_version = '$RULE_VERSION'
WHERE order_month >= '$START_MONTH'
  AND order_month < '$END_MONTH_EXCLUSIVE'
  AND store_name IN ($STORE_SQL_LIST);

DELETE FROM dws_amz_order_source_monthly
WHERE order_month >= '$START_MONTH'
  AND order_month < '$END_MONTH_EXCLUSIVE'
  AND store_name IN ($STORE_SQL_LIST);

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
WHERE order_month >= '$START_MONTH'
  AND order_month < '$END_MONTH_EXCLUSIVE'
  AND store_name IN ($STORE_SQL_LIST)
GROUP BY order_month,store_name,main_order_type;

COMMIT;
SQL

log "7美元低价规则重算完成"
"${MYSQL[@]}" --table -e "
SELECT
  main_order_type,
  SUM(order_sku_count) AS order_sku_count,
  ROUND(SUM(net_item_sales),2) AS net_item_sales
FROM dws_amz_order_source_monthly
WHERE order_month >= '$START_MONTH'
  AND order_month < '$END_MONTH_EXCLUSIVE'
  AND store_name IN ($STORE_SQL_LIST)
GROUP BY main_order_type
ORDER BY FIELD(main_order_type,'广告','站外推广','站内促销','低价','自然');
" 2>&1 | tee -a "$LOG_FILE"
