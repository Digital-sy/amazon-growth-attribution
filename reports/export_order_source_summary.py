#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出 Amazon 五分类订单月度汇总 Excel。

数据源：
- dws_amz_order_source_monthly
- dws_amz_product_ad_allocation_monthly_audit

环境变量：
- LINGXING_DB_HOST / LINGXING_DB_PORT / LINGXING_DB_USER / LINGXING_DB_PASSWORD
- LINGXING_DB_NAME，默认 lingxing
- ATTR_EXPORT_START_MONTH，默认结果表最早月份，格式 YYYY-MM-01
- ATTR_EXPORT_END_MONTH_EXCLUSIVE，默认结果表最大月份+1月
- ATTR_STORES，逗号分隔；默认全部店铺
- ATTR_EXPORT_OUTPUT，可指定输出文件路径
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

try:
    import pymysql
    import xlsxwriter
except ImportError as exc:
    print(f"缺少依赖：{exc}", file=sys.stderr)
    print("请在当前 Python 环境安装：pip install PyMySQL XlsxWriter", file=sys.stderr)
    raise SystemExit(2)

CLASS_ORDER = ["广告", "站外推广", "站内促销", "低价", "自然"]
CLASS_COLORS = {
    "广告": "#4472C4",
    "站外推广": "#ED7D31",
    "站内促销": "#A5A5A5",
    "低价": "#FFC000",
    "自然": "#70AD47",
}


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}")
    return value


def to_number(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    return value


def month_text(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m")
    text = str(value)
    return text[:7]


def add_month(month_start: str) -> str:
    dt = datetime.strptime(month_start, "%Y-%m-%d")
    year = dt.year + (1 if dt.month == 12 else 0)
    month = 1 if dt.month == 12 else dt.month + 1
    return f"{year:04d}-{month:02d}-01"


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
    )


def scalar(cur, sql: str, params: Iterable[Any] = ()) -> Any:
    cur.execute(sql, tuple(params))
    row = cur.fetchone()
    return next(iter(row.values())) if row else None


def query_rows(cur, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    cur.execute(sql, tuple(params))
    return list(cur.fetchall())


def resolve_filters(cur):
    start_month = os.getenv("ATTR_EXPORT_START_MONTH", "").strip()
    end_month = os.getenv("ATTR_EXPORT_END_MONTH_EXCLUSIVE", "").strip()
    stores_env = os.getenv("ATTR_STORES", "").strip()

    if not start_month:
        value = scalar(cur, "SELECT MIN(order_month) FROM dws_amz_order_source_monthly")
        if not value:
            raise RuntimeError("dws_amz_order_source_monthly 没有数据")
        start_month = value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)

    if not end_month:
        value = scalar(cur, "SELECT MAX(order_month) FROM dws_amz_order_source_monthly")
        if not value:
            raise RuntimeError("dws_amz_order_source_monthly 没有数据")
        max_month = value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)
        end_month = add_month(max_month)

    if stores_env:
        stores = [s.strip() for s in stores_env.split(",") if s.strip()]
    else:
        stores = [
            row["store_name"]
            for row in query_rows(
                cur,
                """
                SELECT DISTINCT store_name
                FROM dws_amz_order_source_monthly
                WHERE order_month >= %s AND order_month < %s
                ORDER BY store_name
                """,
                (start_month, end_month),
            )
        ]

    if not stores:
        raise RuntimeError("没有可导出的店铺")
    return start_month, end_month, stores


def fetch_data(cur, start_month: str, end_month: str, stores: list[str]):
    placeholders = ",".join(["%s"] * len(stores))
    base_params = [start_month, end_month, *stores]

    detail = query_rows(
        cur,
        f"""
        SELECT
            order_month,
            store_name,
            main_order_type,
            classified_item_rows,
            order_sku_count,
            amazon_order_count,
            units,
            gross_item_sales,
            net_item_sales
        FROM dws_amz_order_source_monthly
        WHERE order_month >= %s
          AND order_month < %s
          AND store_name IN ({placeholders})
        ORDER BY order_month, store_name,
          FIELD(main_order_type,'广告','站外推广','站内促销','低价','自然')
        """,
        base_params,
    )

    audit = query_rows(
        cur,
        f"""
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
        FROM dws_amz_product_ad_allocation_monthly_audit
        WHERE order_month >= %s
          AND order_month < %s
          AND store_name IN ({placeholders})
        ORDER BY order_month, store_name
        """,
        base_params,
    )
    return detail, audit


def aggregate(detail: list[dict[str, Any]]):
    overall = defaultdict(lambda: {"order_sku_count": 0, "units": 0, "gross": 0.0, "net": 0.0})
    monthly = defaultdict(lambda: defaultdict(lambda: {"order_sku_count": 0, "units": 0, "gross": 0.0, "net": 0.0}))
    monthly_total = defaultdict(lambda: {"order_sku_count": 0, "units": 0, "gross": 0.0, "net": 0.0})

    for row in detail:
        category = row["main_order_type"]
        month = month_text(row["order_month"])
        order_sku = int(row.get("order_sku_count") or 0)
        units = int(row.get("units") or 0)
        gross = float(row.get("gross_item_sales") or 0)
        net = float(row.get("net_item_sales") or 0)

        for target in (overall[category], monthly[month][category], monthly_total[month]):
            target["order_sku_count"] += order_sku
            target["units"] += units
            target["gross"] += gross
            target["net"] += net

    overall_total = {
        "order_sku_count": sum(v["order_sku_count"] for v in overall.values()),
        "units": sum(v["units"] for v in overall.values()),
        "gross": sum(v["gross"] for v in overall.values()),
        "net": sum(v["net"] for v in overall.values()),
    }
    return overall, overall_total, monthly, monthly_total


def build_workbook(
    output_path: Path,
    detail: list[dict[str, Any]],
    audit: list[dict[str, Any]],
    start_month: str,
    end_month: str,
    stores: list[str],
):
    overall, overall_total, monthly, monthly_total = aggregate(detail)
    months = sorted(monthly.keys())

    workbook = xlsxwriter.Workbook(output_path)
    workbook.set_properties(
        {
            "title": "Amazon五分类订单月度汇总",
            "subject": "广告、站外推广、站内促销、低价、自然订单结构",
            "author": "amazon-growth-attribution",
            "company": "尚亿数据",
            "comments": "广告订单采用领星产品表现月度广告订单量，并按店铺月份统计分配。",
        }
    )

    title_fmt = workbook.add_format(
        {"bold": True, "font_size": 18, "font_color": "#FFFFFF", "bg_color": "#1F4E78", "align": "left", "valign": "vcenter"}
    )
    subtitle_fmt = workbook.add_format({"font_color": "#666666", "font_size": 10})
    header_fmt = workbook.add_format(
        {"bold": True, "font_color": "#FFFFFF", "bg_color": "#4472C4", "border": 1, "align": "center", "valign": "vcenter"}
    )
    section_fmt = workbook.add_format(
        {"bold": True, "font_size": 12, "font_color": "#1F1F1F", "bg_color": "#D9EAF7", "border": 1, "align": "left"}
    )
    text_fmt = workbook.add_format({"border": 1, "valign": "top"})
    wrap_fmt = workbook.add_format({"border": 1, "valign": "top", "text_wrap": True})
    int_fmt = workbook.add_format({"border": 1, "num_format": "#,##0"})
    pct_fmt = workbook.add_format({"border": 1, "num_format": "0.00%"})
    money_fmt = workbook.add_format({"border": 1, "num_format": "$#,##0.00"})
    note_fmt = workbook.add_format({"font_color": "#666666", "font_size": 9, "text_wrap": True})
    kpi_label_fmt = workbook.add_format({"bold": True, "font_color": "#666666", "align": "center", "valign": "vcenter"})
    kpi_value_fmt = workbook.add_format({"bold": True, "font_size": 18, "font_color": "#1F4E78", "align": "center", "valign": "vcenter", "num_format": "#,##0"})
    kpi_money_fmt = workbook.add_format({"bold": True, "font_size": 18, "font_color": "#1F4E78", "align": "center", "valign": "vcenter", "num_format": "$#,##0.00"})

    ws = workbook.add_worksheet("总览")
    ws.hide_gridlines(2)
    ws.set_column("A:A", 2)
    ws.set_column("B:G", 16)
    ws.set_column("H:N", 14)
    ws.set_row(0, 30)
    ws.merge_range("B1:N1", "Amazon五分类订单月度汇总", title_fmt)
    ws.write("B2", f"期间：{start_month[:7]} 至 {end_month[:7]}（结束月不含）｜店铺：{', '.join(stores)}", subtitle_fmt)
    ws.write("B3", f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", subtitle_fmt)

    kpis = [
        ("总订单-SKU", overall_total["order_sku_count"], kpi_value_fmt),
        ("总销量", overall_total["units"], kpi_value_fmt),
        ("净商品销售额", overall_total["net"], kpi_money_fmt),
        ("月份数", len(months), kpi_value_fmt),
    ]
    start_cols = [1, 4, 7, 10]
    for (label, value, fmt), col in zip(kpis, start_cols):
        ws.merge_range(4, col, 4, col + 2, label, kpi_label_fmt)
        ws.merge_range(5, col, 6, col + 2, value, fmt)

    ws.merge_range("B9:H9", "全期间五类结构", section_fmt)
    headers = ["分类", "订单-SKU数", "占比", "销量", "净商品销售额", "销售额占比"]
    for c, h in enumerate(headers, 1):
        ws.write(9, c, h, header_fmt)
    row = 10
    for category in CLASS_ORDER:
        values = overall.get(category, {"order_sku_count": 0, "units": 0, "net": 0.0})
        share = values["order_sku_count"] / overall_total["order_sku_count"] if overall_total["order_sku_count"] else 0
        sales_share = values["net"] / overall_total["net"] if overall_total["net"] else 0
        ws.write(row, 1, category, text_fmt)
        ws.write_number(row, 2, values["order_sku_count"], int_fmt)
        ws.write_number(row, 3, share, pct_fmt)
        ws.write_number(row, 4, values["units"], int_fmt)
        ws.write_number(row, 5, values["net"], money_fmt)
        ws.write_number(row, 6, sales_share, pct_fmt)
        row += 1
    ws.write(row, 1, "合计", header_fmt)
    ws.write_number(row, 2, overall_total["order_sku_count"], int_fmt)
    ws.write_number(row, 3, 1, pct_fmt)
    ws.write_number(row, 4, overall_total["units"], int_fmt)
    ws.write_number(row, 5, overall_total["net"], money_fmt)
    ws.write_number(row, 6, 1, pct_fmt)

    monthly_start = 18
    ws.merge_range(monthly_start, 1, monthly_start, 13, "逐月五类占比", section_fmt)
    monthly_headers = ["月份", "总订单-SKU"] + [f"{c}数" for c in CLASS_ORDER] + [f"{c}占比" for c in CLASS_ORDER] + ["净商品销售额"]
    for c, h in enumerate(monthly_headers, 1):
        ws.write(monthly_start + 1, c, h, header_fmt)
    for r_idx, month in enumerate(months, monthly_start + 2):
        total = monthly_total[month]["order_sku_count"]
        ws.write(r_idx, 1, month, text_fmt)
        ws.write_number(r_idx, 2, total, int_fmt)
        col = 3
        for category in CLASS_ORDER:
            ws.write_number(r_idx, col, monthly[month][category]["order_sku_count"], int_fmt)
            col += 1
        for category in CLASS_ORDER:
            value = monthly[month][category]["order_sku_count"] / total if total else 0
            ws.write_number(r_idx, col, value, pct_fmt)
            col += 1
        ws.write_number(r_idx, col, monthly_total[month]["net"], money_fmt)

    if months:
        first_data_excel_row = monthly_start + 3
        last_data_excel_row = monthly_start + 2 + len(months)
        chart = workbook.add_chart({"type": "column", "subtype": "stacked"})
        for idx, category in enumerate(CLASS_ORDER):
            chart.add_series(
                {
                    "name": category,
                    "categories": f"='总览'!$B${first_data_excel_row}:$B${last_data_excel_row}",
                    "values": f"='总览'!${xlsxwriter.utility.xl_col_to_name(8 + idx)}${first_data_excel_row}:${xlsxwriter.utility.xl_col_to_name(8 + idx)}${last_data_excel_row}",
                    "fill": {"color": CLASS_COLORS[category]},
                    "border": {"none": True},
                }
            )
        chart.set_title({"name": "逐月五类订单占比"})
        chart.set_y_axis({"name": "占比", "num_format": "0%", "min": 0, "max": 1})
        chart.set_x_axis({"name": "月份", "label_position": "low"})
        chart.set_legend({"position": "bottom"})
        chart.set_size({"width": 920, "height": 360})
        chart.set_style(10)
        ws.insert_chart("I9", chart)

    ws.freeze_panes(9, 1)

    ws2 = workbook.add_worksheet("月度汇总")
    ws2.freeze_panes(1, 0)
    ws2.set_column("A:A", 12)
    ws2.set_column("B:B", 12)
    ws2.set_column("C:D", 15)
    ws2.set_column("E:E", 12)
    ws2.set_column("F:G", 16)
    ws2.set_column("H:H", 18)
    summary_headers = ["月份", "分类", "订单-SKU数", "占比", "销量", "毛商品销售额", "净商品销售额", "净销售额占比"]
    for c, h in enumerate(summary_headers):
        ws2.write(0, c, h, header_fmt)
    r = 1
    for month in months:
        total = monthly_total[month]
        for category in CLASS_ORDER:
            values = monthly[month][category]
            ws2.write(r, 0, month, text_fmt)
            ws2.write(r, 1, category, text_fmt)
            ws2.write_number(r, 2, values["order_sku_count"], int_fmt)
            ws2.write_number(r, 3, values["order_sku_count"] / total["order_sku_count"] if total["order_sku_count"] else 0, pct_fmt)
            ws2.write_number(r, 4, values["units"], int_fmt)
            ws2.write_number(r, 5, values["gross"], money_fmt)
            ws2.write_number(r, 6, values["net"], money_fmt)
            ws2.write_number(r, 7, values["net"] / total["net"] if total["net"] else 0, pct_fmt)
            r += 1
    if r > 1:
        ws2.add_table(0, 0, r - 1, len(summary_headers) - 1, {"name": "MonthlySummaryTable", "columns": [{"header": h} for h in summary_headers], "style": "Table Style Medium 2"})

    ws3 = workbook.add_worksheet("店铺月度明细")
    detail_headers = ["月份", "店铺", "分类", "明细行数", "订单-SKU数", "Amazon订单数（不可跨类加总）", "销量", "毛商品销售额", "净商品销售额", "店铺月内占比"]
    for c, h in enumerate(detail_headers):
        ws3.write(0, c, h, header_fmt)
    ws3.freeze_panes(1, 0)
    ws3.set_column("A:A", 12)
    ws3.set_column("B:C", 14)
    ws3.set_column("D:G", 18)
    ws3.set_column("H:I", 18)
    ws3.set_column("J:J", 14)
    store_month_total = defaultdict(int)
    for row in detail:
        store_month_total[(month_text(row["order_month"]), row["store_name"])] += int(row.get("order_sku_count") or 0)
    for r_idx, row in enumerate(detail, 1):
        month = month_text(row["order_month"])
        store = row["store_name"]
        total = store_month_total[(month, store)]
        vals = [
            month,
            store,
            row["main_order_type"],
            int(row.get("classified_item_rows") or 0),
            int(row.get("order_sku_count") or 0),
            int(row.get("amazon_order_count") or 0),
            int(row.get("units") or 0),
            float(row.get("gross_item_sales") or 0),
            float(row.get("net_item_sales") or 0),
            int(row.get("order_sku_count") or 0) / total if total else 0,
        ]
        formats = [text_fmt, text_fmt, text_fmt, int_fmt, int_fmt, int_fmt, int_fmt, money_fmt, money_fmt, pct_fmt]
        for c, (v, fmt) in enumerate(zip(vals, formats)):
            if isinstance(v, (int, float)):
                ws3.write_number(r_idx, c, v, fmt)
            else:
                ws3.write(r_idx, c, v, fmt)
    if detail:
        ws3.add_table(0, 0, len(detail), len(detail_headers) - 1, {"name": "StoreMonthlyDetailTable", "columns": [{"header": h} for h in detail_headers], "style": "Table Style Medium 2"})

    ws4 = workbook.add_worksheet("广告审计")
    audit_headers = [
        "月份", "店铺", "产品表现订单量", "产品表现广告目标", "产品表现促销订单", "MSKU数",
        "有效订单-SKU", "站外订单-SKU", "非站外容量", "已分配广告订单", "未分配广告订单",
        "分配完成率", "两数据源订单差异", "差异率", "结果状态", "生成时间"
    ]
    for c, h in enumerate(audit_headers):
        ws4.write(0, c, h, header_fmt)
    ws4.freeze_panes(1, 0)
    ws4.set_column("A:B", 13)
    ws4.set_column("C:K", 17)
    ws4.set_column("L:N", 14)
    ws4.set_column("O:O", 12)
    ws4.set_column("P:P", 20)
    for r_idx, row in enumerate(audit, 1):
        product_orders = int(row.get("product_order_items") or 0)
        gap = int(row.get("source_gap_order_sku") or 0)
        gap_rate = gap / product_orders if product_orders else 0
        unallocated = int(row.get("unallocated_ad_orders") or 0)
        alloc_pct = float(row.get("allocation_pct") or 0) / 100
        status = "通过" if unallocated == 0 and alloc_pct >= 0.999999 else "需检查"
        vals = [
            month_text(row["order_month"]), row["store_name"], product_orders,
            int(row.get("product_ad_order_target") or 0), int(row.get("product_promotion_orders") or 0),
            int(row.get("msku_count") or 0), int(row.get("base_order_sku_count") or 0), int(row.get("offsite_order_sku_count") or 0),
            int(row.get("non_offsite_capacity") or 0), int(row.get("allocated_ad_orders") or 0), unallocated,
            alloc_pct, gap, gap_rate, status,
            row.get("created_at").strftime("%Y-%m-%d %H:%M:%S") if hasattr(row.get("created_at"), "strftime") else str(row.get("created_at") or ""),
        ]
        fmts = [text_fmt, text_fmt] + [int_fmt] * 9 + [pct_fmt, int_fmt, pct_fmt, text_fmt, text_fmt]
        for c, (v, fmt) in enumerate(zip(vals, fmts)):
            if isinstance(v, (int, float)):
                ws4.write_number(r_idx, c, v, fmt)
            else:
                ws4.write(r_idx, c, v, fmt)
    if audit:
        ws4.add_table(0, 0, len(audit), len(audit_headers) - 1, {"name": "AdAuditTable", "columns": [{"header": h} for h in audit_headers], "style": "Table Style Medium 2"})
        ws4.conditional_format(1, 14, len(audit), 14, {"type": "text", "criteria": "containing", "value": "需检查", "format": workbook.add_format({"bg_color": "#FFC7CE", "font_color": "#9C0006"})})
        ws4.conditional_format(1, 13, len(audit), 13, {"type": "cell", "criteria": ">", "value": 0.01, "format": workbook.add_format({"bg_color": "#FFF2CC", "font_color": "#7F6000"})})

    ws5 = workbook.add_worksheet("口径说明")
    ws5.hide_gridlines(2)
    ws5.set_column("A:A", 20)
    ws5.set_column("B:B", 86)
    ws5.merge_range("A1:B1", "Amazon五分类订单口径说明", title_fmt)
    definitions = [
        ("统计范围", f"{start_month[:7]} 至 {end_month[:7]}（结束月不含）；店铺：{', '.join(stores)}。"),
        ("主统计指标", "订单-SKU数：按店铺+Amazon订单号+SKU去重。该指标在五类之间互斥、可加总，是本Excel的主订单指标。"),
        ("有效订单过滤", "订单来源为 ods_amz_all_orders_report；仅保留 sales_channel=Amazon.com、order_status=Shipped、item_status=Shipped、quantity>0、item_price>0、currency=USD、SKU非空、下单时间非空。"),
        ("时间口径", "purchase_date_utc 从 UTC 转换为 America/Los_Angeles 后确定订单日期和订单月份。"),
        ("分类优先级", "站外推广 ＞ 广告 ＞ 站内促销 ＞ 低价 ＞ 自然。Excel展示顺序为：广告、站外推广、站内促销、低价、自然。"),
        ("站外推广", "订单明细 promotion_ids 包含 MPC-。这是订单明细级确定规则，优先于月度广告统计分配。"),
        ("广告", "每店每月广告目标取 ods_lx_product_performance_monthly_msku 的 SUM(ad_order_quantity)。该字段与领星产品表现前台广告订单量一致。广告目标从非站外订单池中按固定顺序统计分配；不区分SP/SB/SBV/SD。"),
        ("广告标签限制", "广告属于店铺月度统计分配，不代表某一笔具体订单已被Amazon逐单确认为广告订单，不用于逐单追溯、申诉或SKU级精确归因。"),
        ("站内促销", "排除站外和广告后，promotion_ids 包含 Percentage Off 或 PLM-，或者 item_promotion_discount>0。仅有免运费促销不作为商品站内促销。"),
        ("低价", "排除站外、广告和站内促销后，净成交单价=(item_price-item_promotion_discount)/quantity，且净成交单价<=10美元。"),
        ("自然", "未命中站外、广告、站内促销或低价规则的剩余订单-SKU。"),
        ("销售额口径", "毛商品销售额为 item_price；净商品销售额为 item_price-item_promotion_discount。均为USD，不包含买家运费等其他金额。"),
        ("Amazon订单数", "amazon_order_count仅供参考。一个多SKU Amazon订单可能在不同SKU上被分到不同来源，因此跨分类相加会重复，不应作为五类合计口径。"),
        ("产品表现促销订单", "产品表现表 promotion_order_items用于审计和对照，不直接作为最终站内促销数量。因为同一订单可能同时有广告和促销，最终按分类优先级只进入一个类别。"),
        ("数据源差异", "产品表现 order_items 与有效订单-SKU可能存在少量差异，主要来自Shipped、USD、价格、SKU等过滤条件。广告审计表列出 source_gap_order_sku 和差异率。"),
        ("完整性要求", "每店每月：五类订单-SKU合计应等于有效订单-SKU；已分配广告订单应等于产品表现广告目标；未分配广告订单应为0。"),
        ("数据表", "最终月度汇总：dws_amz_order_source_monthly；广告审计：dws_amz_product_ad_allocation_monthly_audit；产品表现月表：ods_lx_product_performance_monthly_msku。"),
        ("规则版本", "产品表现月度广告统计分配版本；站外、站内促销、低价规则沿用订单明细五分类规则。"),
    ]
    for idx, (term, desc) in enumerate(definitions, 2):
        ws5.write(idx - 1, 0, term, section_fmt)
        ws5.write(idx - 1, 1, desc, wrap_fmt)
        ws5.set_row(idx - 1, 34)
    ws5.write(len(definitions) + 2, 0, "重要说明", section_fmt)
    ws5.write(len(definitions) + 2, 1, "本Excel适合按月、按店铺观察五类订单结构和趋势；不应把统计分配后的具体广告订单行视为Amazon逐单广告归因证据。", wrap_fmt)

    workbook.close()


def main() -> int:
    with connect_db() as conn:
        with conn.cursor() as cur:
            start_month, end_month, stores = resolve_filters(cur)
            detail, audit = fetch_data(cur, start_month, end_month, stores)

    if not detail:
        raise RuntimeError("指定范围内没有五分类月度汇总数据")

    output_env = os.getenv("ATTR_EXPORT_OUTPUT", "").strip()
    if output_env:
        output_path = Path(output_env)
    else:
        output_dir = Path(__file__).resolve().parents[1] / "exports"
        output_dir.mkdir(parents=True, exist_ok=True)
        end_inclusive = datetime.strptime(end_month, "%Y-%m-%d")
        if end_inclusive.month == 1:
            end_year, end_mon = end_inclusive.year - 1, 12
        else:
            end_year, end_mon = end_inclusive.year, end_inclusive.month - 1
        output_path = output_dir / f"amazon_order_source_summary_{start_month[:7].replace('-', '')}_{end_year:04d}{end_mon:02d}.xlsx"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    build_workbook(output_path, detail, audit, start_month, end_month, stores)
    print(f"Excel已生成：{output_path}")
    print(f"店铺：{', '.join(stores)}")
    print(f"月份：{start_month[:7]} 至 {end_month[:7]}（结束月不含）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"导出失败：{exc}", file=sys.stderr)
        raise
