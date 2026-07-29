#!/usr/bin/env bash
set -Eeuo pipefail

# Amazon 五类来源生产入口：四个主来源 + 站内促销展示标签。
#
# 主来源（互斥、参与合计和占比）：
#   站外推广 / 广告 / 低价 / 自然
# 展示标签（可与主来源重叠，不参与合计和占比）：
#   站内促销
#
# 低价规则：排除站外、广告和站内促销后，净成交单价 <= 10 USD。
# 因此未归入站外或广告的站内促销订单，其主来源计入自然；
# 同时在月度汇总中额外生成“站内促销”展示行。

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
LOW_PRICE_USD="10.000000"
RULE_VERSION="v5_10usd_promo_overlay_20260729"
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
  log "无法确定10美元规则重算范围，终止"
  exit 1
fi

log "按10美元+站内促销展示口径重算：$START_MONTH 至 $END_MONTH_EXCLUSIVE（结束月不含）；店铺：${STORES[*]}"

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
    WHEN onsite_promotion_flag = 0 AND net_unit_price <= $LOW_PRICE_USD THEN '低价'
    ELSE '自然'
  END,
  classification_reason = CASE
    WHEN offsite_flag = 1 THEN 'promotion_ids命中MPC-'
    WHEN COALESCE(estimated_ad_flag,0) = 1
      THEN '产品表现ad_order_quantity按店铺+月份统计分配'
    WHEN onsite_promotion_flag = 0 AND net_unit_price <= $LOW_PRICE_USD
      THEN '排除站外、广告和站内促销后净成交单价<=10美元'
    WHEN onsite_promotion_flag = 1
      THEN '站内促销仅作展示标签；主来源计入自然'
    ELSE '未命中站外、广告或低价规则'
  END,
  rule_version = '$RULE_VERSION'
WHERE order_month >= '$START_MONTH'
  AND order_month < '$END_MONTH_EXCLUSIVE'
  AND store_name IN ($STORE_SQL_LIST);

DELETE FROM dws_amz_order_source_monthly
WHERE order_month >= '$START_MONTH'
  AND order_month < '$END_MONTH_EXCLUSIVE'
  AND store_name IN ($STORE_SQL_LIST);

-- 四个互斥主来源：参与合计和占比。
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
  AND main_order_type IN ('广告','站外推广','低价','自然')
GROUP BY order_month,store_name,main_order_type;

-- 站内促销展示行：可与广告/自然等主来源重叠，不参与合计和占比。
INSERT INTO dws_amz_order_source_monthly (
  order_month,store_name,main_order_type,classified_item_rows,
  order_sku_count,amazon_order_count,units,gross_item_sales,net_item_sales
)
SELECT
  order_month,store_name,'站内促销',COUNT(*),
  COUNT(DISTINCT amazon_order_id,sku_key),
  COUNT(DISTINCT amazon_order_id),SUM(quantity),
  SUM(gross_item_sales),SUM(net_item_sales)
FROM dwd_amz_order_attribution_result
WHERE order_month >= '$START_MONTH'
  AND order_month < '$END_MONTH_EXCLUSIVE'
  AND store_name IN ($STORE_SQL_LIST)
  AND onsite_promotion_flag = 1
GROUP BY order_month,store_name;

COMMIT;
SQL

log "10美元低价规则及站内促销展示口径重算完成"
"${MYSQL[@]}" --table -e "
SELECT
  main_order_type,
  SUM(order_sku_count) AS order_sku_count,
  ROUND(SUM(net_item_sales),2) AS net_item_sales,
  CASE WHEN main_order_type='站内促销' THEN '展示项，不参与合计占比' ELSE '主来源' END AS metric_role
FROM dws_amz_order_source_monthly
WHERE order_month >= '$START_MONTH'
  AND order_month < '$END_MONTH_EXCLUSIVE'
  AND store_name IN ($STORE_SQL_LIST)
GROUP BY main_order_type
ORDER BY FIELD(main_order_type,'广告','站外推广','站内促销','低价','自然');
" 2>&1 | tee -a "$LOG_FILE"

if [[ -f "$SCRIPT_DIR/ensure_product_audit_compat.sh" ]]; then
  bash "$SCRIPT_DIR/ensure_product_audit_compat.sh"
fi
