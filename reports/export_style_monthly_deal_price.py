#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出“款号-每月成交价”Excel。

主数据源：
- lingxing.ods_amz_all_orders_report：Amazon订单商品明细
- lingxing.listing：店铺+MSKU映射本地SKU及负责人
- lingxing.lxpm_product_category_snapshot：本地SKU映射款号/SPU、季节、品类

成交价口径：SUM(item_price - item_promotion_discount) / SUM(quantity)
月份按 purchase_date_utc 从 UTC 转 America/Los_Angeles 后归属。
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

try:
    import pymysql
    import xlsxwriter
except ImportError as exc:
    print(f"缺少依赖：{exc}", file=sys.stderr)
    print("请安装：pip install PyMySQL XlsxWriter", file=sys.stderr)
    raise SystemExit(2)

DEFAULT_START_MONTH = "2025-01-01"
DEFAULT_END_MONTH_EXCLUSIVE = "2026-08-01"
DEFAULT_STORES = "JQ-US,MT-US,RKZ-US,SY-US"


def clean(value: Any) -> str:
    return str(value or "").strip()


def norm(value: Any) -> str:
    return clean(value).upper()


def env_required(name: str) -> str:
    value = clean(os.getenv(name))
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}")
    return value


def connect_db():
    return pymysql.connect(
        host=env_required("LINGXING_DB_HOST"),
        port=int(os.getenv("LINGXING_DB_PORT", "3306")),
        user=env_required("LINGXING_DB_USER"),
        password=env_required("LINGXING_DB_PASSWORD"),
        database=os.getenv("LINGXING_DB_NAME", "lingxing"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=15,
        read_timeout=1200,
        write_timeout=1200,
    )


def month_range(start_month: str, end_month_exclusive: str) -> list[str]:
    start = datetime.strptime(start_month, "%Y-%m-%d")
    end = datetime.strptime(end_month_exclusive, "%Y-%m-%d")
    if start >= end:
        raise ValueError("开始月份必须早于结束月份")
    result: list[str] = []
    current = start
    while current < end:
        result.append(current.strftime("%Y-%m"))
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return result


def table_columns(cur, schema: str, table: str) -> set[str]:
    cur.execute(
        """
        SELECT COLUMN_NAME
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
        """,
        (schema, table),
    )
    return {row["COLUMN_NAME"] for row in cur.fetchall()}


def require_columns(cur, schema: str, table: str, required: Iterable[str]) -> None:
    columns = table_columns(cur, schema, table)
    if not columns:
        raise RuntimeError(f"表不存在：{schema}.{table}")
    missing = [name for name in required if name not in columns]
    if missing:
        raise RuntimeError(
            f"表 {schema}.{table} 缺少字段：{missing}；实际字段：{sorted(columns)}"
        )


def query_rows(cur, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    cur.execute(sql, tuple(params))
    return list(cur.fetchall())


def parse_custom_fields(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def custom_field_value(value: Any, field_name: str) -> str:
    values: list[str] = []
    for item in parse_custom_fields(value):
        if clean(item.get("name")) != field_name:
            continue
        field_value = ""
        for key in ("val_text", "val", "value", "field_value"):
            if item.get(key) not in (None, ""):
                field_value = clean(item.get(key))
                break
        if field_value and field_value not in values:
            values.append(field_value)
    return "|".join(values)


def category_fallback(category_name: Any, category_path: Any) -> str:
    name = clean(category_name)
    if name:
        return name
    path = clean(category_path)
    if not path:
        return ""
    for separator in (">", "/", "／", "\\"):
        if separator in path:
            parts = [part.strip() for part in path.split(separator) if part.strip()]
            if parts:
                return parts[-1]
    return path


def fetch_order_msku_monthly(
    cur,
    schema: str,
    start_month: str,
    end_month_exclusive: str,
    stores: list[str],
) -> list[dict[str, Any]]:
    placeholders = ",".join(["%s"] * len(stores))
    sql = f"""
    SELECT
        DATE_FORMAT(CONVERT_TZ(purchase_date_utc,'UTC','America/Los_Angeles'),'%%Y-%%m') AS order_month,
        CAST(store_name AS CHAR CHARACTER SET utf8mb4) AS store_name,
        UPPER(TRIM(CAST(sku AS CHAR CHARACTER SET utf8mb4))) AS msku,
        COUNT(DISTINCT amazon_order_id, UPPER(TRIM(CAST(sku AS CHAR CHARACTER SET utf8mb4)))) AS order_sku_count,
        SUM(quantity) AS units,
        SUM(COALESCE(item_price,0)) AS gross_item_sales,
        SUM(COALESCE(item_promotion_discount,0)) AS item_promotion_discount,
        SUM(COALESCE(item_price,0)-COALESCE(item_promotion_discount,0)) AS net_item_sales,
        MIN((COALESCE(item_price,0)-COALESCE(item_promotion_discount,0))/NULLIF(quantity,0)) AS min_deal_price,
        MAX((COALESCE(item_price,0)-COALESCE(item_promotion_discount,0))/NULLIF(quantity,0)) AS max_deal_price
    FROM `{schema}`.`ods_amz_all_orders_report`
    WHERE purchase_date_utc >= CONVERT_TZ(%s,'America/Los_Angeles','UTC')
      AND purchase_date_utc < CONVERT_TZ(%s,'America/Los_Angeles','UTC')
      AND store_name IN ({placeholders})
      AND sales_channel='Amazon.com'
      AND order_status='Shipped'
      AND item_status='Shipped'
      AND quantity>0
      AND item_price>0
      AND currency='USD'
      AND sku IS NOT NULL
      AND CHAR_LENGTH(TRIM(CAST(sku AS CHAR)))>0
      AND purchase_date_utc IS NOT NULL
    GROUP BY
        DATE_FORMAT(CONVERT_TZ(purchase_date_utc,'UTC','America/Los_Angeles'),'%%Y-%%m'),
        CAST(store_name AS CHAR CHARACTER SET utf8mb4),
        UPPER(TRIM(CAST(sku AS CHAR CHARACTER SET utf8mb4)))
    ORDER BY order_month, store_name, msku
    """
    params = [f"{start_month} 00:00:00", f"{end_month_exclusive} 00:00:00", *stores]
    return query_rows(cur, sql, params)


def fetch_listing_map(cur, schema: str, table: str, stores: list[str]) -> dict[tuple[str, str], dict[str, Any]]:
    placeholders = ",".join(["%s"] * len(stores))
    rows = query_rows(
        cur,
        f"""
        SELECT `店铺`,`店铺id`,`MSKU`,`SKU`,`负责人`,`状态`
        FROM `{schema}`.`{table}`
        WHERE `店铺` IN ({placeholders})
          AND `MSKU` IS NOT NULL
          AND CHAR_LENGTH(TRIM(CAST(`MSKU` AS CHAR)))>0
        """,
        stores,
    )
    result: dict[tuple[str, str], dict[str, Any]] = {}
    score_by_key: dict[tuple[str, str], tuple[int, int, int]] = {}
    for row in rows:
        key = (norm(row.get("店铺")), norm(row.get("MSKU")))
        if not key[0] or not key[1]:
            continue
        status = int(row.get("状态") or 0)
        principal = clean(row.get("负责人"))
        local_sku = clean(row.get("SKU"))
        score = (1 if status == 1 else 0, 1 if principal else 0, 1 if local_sku else 0)
        if key not in result or score > score_by_key[key]:
            result[key] = {
                "store": clean(row.get("店铺")),
                "sid": clean(row.get("店铺id")),
                "msku": clean(row.get("MSKU")),
                "local_sku": local_sku,
                "principal": principal,
                "status": status,
            }
            score_by_key[key] = score
    return result


def fetch_product_map(cur, schema: str, table: str) -> dict[str, dict[str, Any]]:
    rows = query_rows(
        cur,
        f"""
        SELECT sku,product_name,spu,category_name,category_path,custom_fields_json
        FROM `{schema}`.`{table}`
        WHERE sku IS NOT NULL AND CHAR_LENGTH(TRIM(CAST(sku AS CHAR)))>0
        """,
    )
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        sku_key = norm(row.get("sku"))
        if not sku_key:
            continue
        season = custom_field_value(row.get("custom_fields_json"), "季节")
        category = custom_field_value(row.get("custom_fields_json"), "品类")
        if not category:
            category = category_fallback(row.get("category_name"), row.get("category_path"))
        result[sku_key] = {
            "local_sku": clean(row.get("sku")),
            "product_name": clean(row.get("product_name")),
            "style_no": clean(row.get("spu")),
            "season": season,
            "category": category,
        }
    return result


@dataclass
class Metric:
    order_sku_count: int = 0
    units: int = 0
    gross: float = 0.0
    discount: float = 0.0
    net: float = 0.0
    min_price: float | None = None
    max_price: float | None = None
    mskus: set[str] = field(default_factory=set)
    local_skus: set[str] = field(default_factory=set)

    def add(self, row: dict[str, Any], msku: str, local_sku: str) -> None:
        self.order_sku_count += int(row.get("order_sku_count") or 0)
        self.units += int(row.get("units") or 0)
        self.gross += float(row.get("gross_item_sales") or 0)
        self.discount += float(row.get("item_promotion_discount") or 0)
        self.net += float(row.get("net_item_sales") or 0)
        if row.get("min_deal_price") is not None:
            value = float(row["min_deal_price"])
            self.min_price = value if self.min_price is None else min(self.min_price, value)
        if row.get("max_deal_price") is not None:
            value = float(row["max_deal_price"])
            self.max_price = value if self.max_price is None else max(self.max_price, value)
        if msku:
            self.mskus.add(msku)
        if local_sku:
            self.local_skus.add(local_sku)

    @property
    def weighted_price(self) -> float | None:
        return self.net / self.units if self.units > 0 else None


def mapping_reason(listing: dict[str, Any] | None, product: dict[str, Any] | None) -> str:
    issues: list[str] = []
    if not listing:
        return "未匹配Listing（店铺+MSKU）"
    if not clean(listing.get("local_sku")):
        issues.append("Listing本地SKU为空")
    if not clean(listing.get("principal")):
        issues.append("负责人为空")
    if not product:
        issues.append("未匹配产品管理")
        return "；".join(issues)
    if not clean(product.get("style_no")):
        issues.append("款号/SPU为空")
    if not clean(product.get("season")):
        issues.append("季节为空")
    if not clean(product.get("category")):
        issues.append("品类为空")
    return "已完整匹配" if not issues else "；".join(issues)


def enrich_and_aggregate(order_rows, listing_map, product_map):
    long_metrics = defaultdict(Metric)
    audit_metrics = defaultdict(Metric)
    coverage = defaultdict(Metric)
    for row in order_rows:
        month = clean(row.get("order_month"))
        store = clean(row.get("store_name"))
        msku = clean(row.get("msku"))
        listing = listing_map.get((norm(store), norm(msku)))
        local_sku = clean(listing.get("local_sku")) if listing else ""
        principal = clean(listing.get("principal")) if listing else ""
        product = product_map.get(norm(local_sku)) if local_sku else None
        style_no = clean(product.get("style_no")) if product else ""
        season = clean(product.get("season")) if product else ""
        category = clean(product.get("category")) if product else ""
        reason = mapping_reason(listing, product)
        key = (
            month,
            store,
            style_no or "（未匹配款号）",
            season or "（未维护）",
            category or "（未维护）",
            principal or "（未维护）",
        )
        long_metrics[key].add(row, msku, local_sku)
        audit_metrics[(store, msku, reason)].add(row, msku, local_sku)
        coverage[month].add(row, msku, local_sku)
    return long_metrics, audit_metrics, coverage


def write_workbook(output_path, start_month, end_month_exclusive, stores, months, long_metrics, audit_metrics, coverage):
    workbook = xlsxwriter.Workbook(output_path)
    workbook.set_properties({
        "title": "款号每月成交价",
        "subject": "按款号、店铺、负责人统计销量加权平均成交价",
        "author": "amazon-growth-attribution",
        "company": "尚亿数据",
        "comments": "成交价=SUM(item_price-item_promotion_discount)/SUM(quantity)",
    })
    fmt_title = workbook.add_format({"bold": True,"font_size": 18,"font_color": "#FFFFFF","bg_color": "#1F4E78","align": "left","valign": "vcenter"})
    fmt_header = workbook.add_format({"bold": True,"font_color": "#FFFFFF","bg_color": "#4472C4","border": 1,"align": "center","valign": "vcenter","text_wrap": True})
    fmt_section = workbook.add_format({"bold": True,"bg_color": "#D9EAF7","border": 1,"valign": "top"})
    fmt_text = workbook.add_format({"border": 1,"valign": "top"})
    fmt_wrap = workbook.add_format({"border": 1,"valign": "top","text_wrap": True})
    fmt_int = workbook.add_format({"border": 1,"num_format": "#,##0"})
    fmt_money = workbook.add_format({"border": 1,"num_format": "$#,##0.00"})
    fmt_missing = workbook.add_format({"border": 1,"bg_color": "#FFC7CE","font_color": "#9C0006"})

    ws = workbook.add_worksheet("款号月成交价_宽表")
    ws.freeze_panes(1, 6)
    headers = ["店铺","负责人（运营）","款号","季节","品类","全期间销量"] + months + ["全期间加权成交价","MSKU数","本地SKU数"]
    for col, header in enumerate(headers):
        ws.write(0, col, header, fmt_header)
    wide = defaultdict(dict)
    for key, metric in long_metrics.items():
        month, store, style_no, season, category, principal = key
        wide[(store, principal, style_no, season, category)][month] = metric
    row_no = 1
    for dims in sorted(wide.keys(), key=lambda x: (x[0], x[2], x[1], x[3], x[4])):
        store, principal, style_no, season, category = dims
        month_map = wide[dims]
        total = Metric()
        for month in months:
            metric = month_map.get(month)
            if metric:
                total.order_sku_count += metric.order_sku_count
                total.units += metric.units
                total.gross += metric.gross
                total.discount += metric.discount
                total.net += metric.net
                total.mskus.update(metric.mskus)
                total.local_skus.update(metric.local_skus)
        for col, value in enumerate([store, principal, style_no, season, category]):
            ws.write(row_no, col, value, fmt_missing if clean(value).startswith("（未") else fmt_text)
        ws.write_number(row_no, 5, total.units, fmt_int)
        for idx, month in enumerate(months):
            metric = month_map.get(month)
            if metric and metric.weighted_price is not None:
                ws.write_number(row_no, 6 + idx, metric.weighted_price, fmt_money)
            else:
                ws.write_blank(row_no, 6 + idx, None, fmt_text)
        col = 6 + len(months)
        if total.weighted_price is not None:
            ws.write_number(row_no, col, total.weighted_price, fmt_money)
        else:
            ws.write_blank(row_no, col, None, fmt_text)
        ws.write_number(row_no, col + 1, len(total.mskus), fmt_int)
        ws.write_number(row_no, col + 2, len(total.local_skus), fmt_int)
        row_no += 1
    if row_no > 1:
        ws.add_table(0,0,row_no-1,len(headers)-1,{"name":"StyleMonthlyDealPriceWide","columns":[{"header":h} for h in headers],"style":"Table Style Medium 2"})
    ws.set_column("A:A",12); ws.set_column("B:B",16); ws.set_column("C:C",16); ws.set_column("D:E",14); ws.set_column("F:F",14)
    ws.set_column(6,6+len(months)-1,12); ws.set_column(6+len(months),8+len(months),18)

    ws2 = workbook.add_worksheet("款号月成交价_长表")
    long_headers = ["月份","店铺","负责人（运营）","款号","季节","品类","订单-SKU数","销量","毛商品销售额","商品促销折扣","净商品销售额","销量加权成交价","最低单件成交价","最高单件成交价","MSKU数","本地SKU数"]
    for col, header in enumerate(long_headers): ws2.write(0,col,header,fmt_header)
    row_no = 1
    for key in sorted(long_metrics.keys()):
        month, store, style_no, season, category, principal = key
        metric = long_metrics[key]
        values = [month,store,principal,style_no,season,category,metric.order_sku_count,metric.units,metric.gross,metric.discount,metric.net,metric.weighted_price,metric.min_price,metric.max_price,len(metric.mskus),len(metric.local_skus)]
        formats = [fmt_text]*6 + [fmt_int,fmt_int] + [fmt_money]*6 + [fmt_int,fmt_int]
        for col,(value,cell_fmt) in enumerate(zip(values,formats)):
            if value is None: ws2.write_blank(row_no,col,None,cell_fmt)
            elif isinstance(value,(int,float)): ws2.write_number(row_no,col,value,cell_fmt)
            else: ws2.write(row_no,col,value,fmt_missing if col in (2,3,4,5) and clean(value).startswith("（未") else cell_fmt)
        row_no += 1
    if row_no > 1:
        ws2.add_table(0,0,row_no-1,len(long_headers)-1,{"name":"StyleMonthlyDealPriceLong","columns":[{"header":h} for h in long_headers],"style":"Table Style Medium 2"})
    ws2.freeze_panes(1,0); ws2.set_column("A:F",16); ws2.set_column("G:P",16)

    ws3 = workbook.add_worksheet("映射审计")
    audit_headers = ["店铺","MSKU","映射结果","订单-SKU数","销量","净商品销售额","销量加权成交价"]
    for col, header in enumerate(audit_headers): ws3.write(0,col,header,fmt_header)
    row_no = 1
    for key in sorted(audit_metrics.keys(), key=lambda x: (x[2] == "已完整匹配", x[0], x[1])):
        store, msku, reason = key; metric = audit_metrics[key]
        values = [store,msku,reason,metric.order_sku_count,metric.units,metric.net,metric.weighted_price]
        for col,value in enumerate(values):
            if col <= 2: ws3.write(row_no,col,value,fmt_text if reason == "已完整匹配" else fmt_missing)
            elif col in (3,4): ws3.write_number(row_no,col,value,fmt_int)
            else: ws3.write_number(row_no,col,value or 0,fmt_money)
        row_no += 1
    if row_no > 1:
        ws3.add_table(0,0,row_no-1,len(audit_headers)-1,{"name":"MappingAudit","columns":[{"header":h} for h in audit_headers],"style":"Table Style Medium 2"})
    ws3.freeze_panes(1,0); ws3.set_column("A:B",16); ws3.set_column("C:C",38); ws3.set_column("D:G",18)

    ws4 = workbook.add_worksheet("月份覆盖")
    coverage_headers = ["月份","是否有订单数据","订单-SKU数","销量","净商品销售额","状态说明"]
    for col, header in enumerate(coverage_headers): ws4.write(0,col,header,fmt_header)
    today = date.today()
    for row_idx, month in enumerate(months,1):
        metric = coverage.get(month); has_data = bool(metric and metric.units > 0)
        month_dt = datetime.strptime(month,"%Y-%m")
        is_current_month = today.year == month_dt.year and today.month == month_dt.month
        status = "缺少订单数据" if not has_data else (f"未完结月份，截至{today.isoformat()}" if is_current_month else "已有数据")
        ws4.write(row_idx,0,month,fmt_text); ws4.write(row_idx,1,"是" if has_data else "否",fmt_text if has_data else fmt_missing)
        ws4.write_number(row_idx,2,metric.order_sku_count if metric else 0,fmt_int); ws4.write_number(row_idx,3,metric.units if metric else 0,fmt_int)
        ws4.write_number(row_idx,4,metric.net if metric else 0,fmt_money); ws4.write(row_idx,5,status,fmt_text if has_data else fmt_missing)
    ws4.set_column("A:B",16); ws4.set_column("C:E",18); ws4.set_column("F:F",28)

    ws5 = workbook.add_worksheet("口径说明")
    ws5.hide_gridlines(2); ws5.set_column("A:A",22); ws5.set_column("B:B",95); ws5.merge_range("A1:B1","款号每月成交价口径说明",fmt_title)
    definitions = [
        ("统计范围",f"{start_month[:7]} 至 {end_month_exclusive[:7]}（结束月不含）；店铺：{', '.join(stores)}。"),
        ("主订单来源","lingxing.ods_amz_all_orders_report。"),
        ("成交价主指标","销量加权成交价=SUM(item_price-item_promotion_discount)/SUM(quantity)，不是订单行单价的简单平均。"),
        ("有效订单过滤","sales_channel=Amazon.com、order_status=Shipped、item_status=Shipped、quantity>0、item_price>0、currency=USD、SKU非空、下单时间非空。"),
        ("时间口径","purchase_date_utc 从UTC转换为 America/Los_Angeles 后归属订单月份。"),
        ("关联链路","订单店铺+MSKU→listing店铺+MSKU→Listing本地SKU和负责人→产品管理快照SKU→SPU/款号、季节、品类。"),
        ("季节口径","读取产品管理 custom_fields_json 中 name=季节 的 val_text/val。"),
        ("品类口径","优先读取 custom_fields_json 中 name=品类；缺失时回退 category_name，再回退 category_path 最末级。"),
        ("店铺口径","以订单表 store_name 为统计店铺；Listing店铺仅用于匹配本地SKU和负责人。"),
        ("负责人口径","读取Listing当前负责人。若同一店铺+MSKU有多个候选，优先在售、有负责人、有本地SKU的记录。"),
        ("不包含项目","不含买家运费、销售税、退款、平台费、FBA费、广告费等。"),
        ("2026年7月","若订单明细尚未导入，月份覆盖页显示缺少数据；导入后重新运行即可补齐。"),
        ("历史负责人限制","负责人取当前Listing快照，不代表历史月份当时的负责人。"),
    ]
    for idx,(term,desc) in enumerate(definitions,2):
        ws5.write(idx-1,0,term,fmt_section); ws5.write(idx-1,1,desc,fmt_wrap); ws5.set_row(idx-1,36)
    workbook.close()


def main() -> int:
    start_month = clean(os.getenv("DEAL_PRICE_START_MONTH")) or DEFAULT_START_MONTH
    end_month_exclusive = clean(os.getenv("DEAL_PRICE_END_MONTH_EXCLUSIVE")) or DEFAULT_END_MONTH_EXCLUSIVE
    stores = [v.strip() for v in (clean(os.getenv("DEAL_PRICE_STORES")) or clean(os.getenv("ATTR_STORES")) or DEFAULT_STORES).split(",") if v.strip()]
    if not stores: raise RuntimeError("店铺列表为空")
    db_schema = clean(os.getenv("LINGXING_DB_NAME")) or "lingxing"
    listing_schema = clean(os.getenv("DEAL_PRICE_LISTING_SCHEMA")) or db_schema
    listing_table = clean(os.getenv("DEAL_PRICE_LISTING_TABLE")) or "listing"
    product_schema = clean(os.getenv("DEAL_PRICE_PRODUCT_SCHEMA")) or db_schema
    product_table = clean(os.getenv("DEAL_PRICE_PRODUCT_TABLE")) or "lxpm_product_category_snapshot"
    months = month_range(start_month,end_month_exclusive)
    with connect_db() as conn:
        with conn.cursor() as cur:
            require_columns(cur,db_schema,"ods_amz_all_orders_report",["purchase_date_utc","store_name","amazon_order_id","sku","quantity","currency","item_price","item_promotion_discount","sales_channel","order_status","item_status"])
            require_columns(cur,listing_schema,listing_table,["店铺","店铺id","MSKU","SKU","负责人","状态"])
            require_columns(cur,product_schema,product_table,["sku","product_name","spu","category_name","category_path","custom_fields_json"])
            print("读取订单月度MSKU聚合...")
            order_rows = fetch_order_msku_monthly(cur,db_schema,start_month,end_month_exclusive,stores)
            if not order_rows: raise RuntimeError("指定范围内没有有效订单数据")
            print("读取Listing映射..."); listing_map = fetch_listing_map(cur,listing_schema,listing_table,stores)
            print("读取产品管理快照..."); product_map = fetch_product_map(cur,product_schema,product_table)
    print("关联并汇总款号月成交价...")
    long_metrics,audit_metrics,coverage = enrich_and_aggregate(order_rows,listing_map,product_map)
    output_env = clean(os.getenv("DEAL_PRICE_OUTPUT"))
    if output_env: output_path = Path(output_env)
    else:
        output_dir = Path(__file__).resolve().parents[1] / "exports"; output_dir.mkdir(parents=True,exist_ok=True)
        output_path = output_dir / f"style_monthly_deal_price_{start_month[:7].replace('-','')}_{months[-1].replace('-','')}.xlsx"
    output_path.parent.mkdir(parents=True,exist_ok=True)
    print("生成Excel...")
    write_workbook(output_path,start_month,end_month_exclusive,stores,months,long_metrics,audit_metrics,coverage)
    unmatched = sum(1 for (_,_,reason) in audit_metrics.keys() if reason != "已完整匹配")
    print(f"Excel已生成：{output_path}")
    print(f"月份：{months[0]} 至 {months[-1]}")
    print(f"店铺：{', '.join(stores)}")
    print(f"订单月度MSKU聚合行：{len(order_rows):,}")
    print(f"款号月度结果行：{len(long_metrics):,}")
    print(f"存在映射问题的店铺+MSKU组合：{unmatched:,}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"导出失败：{exc}", file=sys.stderr)
        raise
