#!/usr/bin/env bash
set -Eeuo pipefail

# 为历史导出脚本提供审计表名兼容：
# 当前生产表：dws_amz_product_performance_ad_monthly_audit
# 历史查询名：dws_amz_product_ad_allocation_monthly_audit

: "${LINGXING_DB_HOST:?缺少 LINGXING_DB_HOST}"
: "${LINGXING_DB_USER:?缺少 LINGXING_DB_USER}"
: "${LINGXING_DB_PASSWORD:?缺少 LINGXING_DB_PASSWORD}"

DB_PORT="${LINGXING_DB_PORT:-3306}"
DB_NAME="${LINGXING_DB_NAME:-lingxing}"
export MYSQL_PWD="$LINGXING_DB_PASSWORD"

MYSQL=(mysql
  -h "$LINGXING_DB_HOST"
  -P "$DB_PORT"
  -u "$LINGXING_DB_USER"
  "$DB_NAME"
  --default-character-set=utf8mb4
  --batch
  --raw
)

scalar() {
  "${MYSQL[@]}" --skip-column-names -e "$1"
}

SOURCE_TABLE="dws_amz_product_performance_ad_monthly_audit"
COMPAT_OBJECT="dws_amz_product_ad_allocation_monthly_audit"

source_exists="$(scalar "
SELECT COUNT(*)
FROM information_schema.TABLES
WHERE TABLE_SCHEMA='${DB_NAME}'
  AND TABLE_NAME='${SOURCE_TABLE}';")"

if [[ "$source_exists" == "0" ]]; then
  echo "缺少当前产品表现广告审计表：${DB_NAME}.${SOURCE_TABLE}" >&2
  exit 1
fi

compat_exists="$(scalar "
SELECT COUNT(*)
FROM information_schema.TABLES
WHERE TABLE_SCHEMA='${DB_NAME}'
  AND TABLE_NAME='${COMPAT_OBJECT}';")"

if [[ "$compat_exists" != "0" ]]; then
  object_type="$(scalar "
SELECT TABLE_TYPE
FROM information_schema.TABLES
WHERE TABLE_SCHEMA='${DB_NAME}'
  AND TABLE_NAME='${COMPAT_OBJECT}'
LIMIT 1;")"
  echo "兼容对象已存在：${DB_NAME}.${COMPAT_OBJECT}（${object_type}）"
  exit 0
fi

"${MYSQL[@]}" -e "
CREATE VIEW ${COMPAT_OBJECT} AS
SELECT
    order_month,
    store_name,
    product_order_items,
    product_ad_order_target,
    product_promotion_orders,
    msku_count,
    base_order_sku_count,
    offsite_order_sku_count,
    non_offsite_capacity,
    allocated_ad_orders,
    unallocated_ad_orders,
    allocation_pct,
    source_gap_order_sku,
    created_at
FROM ${SOURCE_TABLE};
"

echo "已创建兼容视图：${DB_NAME}.${COMPAT_OBJECT} -> ${SOURCE_TABLE}"
