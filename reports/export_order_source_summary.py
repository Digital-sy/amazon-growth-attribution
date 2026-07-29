#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出 Amazon 五类来源完整汇总工作簿。

主来源（互斥、参与合计和占比）：
- 广告
- 站外推广
- 低价
- 自然

展示标签（可与主来源重叠，不参与合计和占比）：
- 站内促销

工作簿：
- 总览
- 月度汇总
- 季度汇总
- 半年度汇总
- 店铺月度明细
- 广告审计
- 口径说明
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

try:
    import pymysql
    import xlsxwriter
except ImportError as exc:
    print(f"缺少依赖：{exc}", file=sys.stderr)
    raise SystemExit(2)

DISPLAY_ORDER = ["广告", "站外推广", "站内促销", "低价", "自然"]
PRIMARY_ORDER = ["广告", "站外推广", "低价", "自然"]
DISPLAY_ONLY = "站内促销"
COLORS = {
    "广告": "#4472C4",
    "站外推广": "#ED7D31",
    "低价": "#FFC000",
    "自然": "#70AD47",
}


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
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


def query_rows(cur, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    cur.execute(sql, tuple(params))
    return list(cur.fetchall())


def scalar(cur, sql: str, params: Iterable[Any] = ()) -> Any:
    cur.execute(sql, tuple(params))
    row = cur.fetchone()
    return next(iter(row.values())) if row else None


def month_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date().replace(day=1)
    if isinstance(value, date):
        return value.replace(day=1)
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date().replace(day=1)


def add_month(value: date, count: int = 1) -> date:
    idx = value.year * 12 + value.month - 1 + count
    return date(idx // 12, idx % 12 + 1, 1)


def resolve_filters(cur):
    start_text = os.getenv("ATTR_EXPORT_START_MONTH", "").strip()
    end_text = os.getenv("ATTR_EXPORT_END_MONTH_EXCLUSIVE", "").strip()
    stores_text = os.getenv("ATTR_STORES", "").strip()

    if start_text:
        start = datetime.strptime(start_text, "%Y-%m-%d").date()
    else:
        value = scalar(cur, "SELECT MIN(order_month) FROM dws_amz_order_source_monthly")
        if not value:
            raise RuntimeError("dws_amz_order_source_monthly 没有数据")
        start = month_date(value)

    if end_text:
        end_exclusive = datetime.strptime(end_text, "%Y-%m-%d").date()
    else:
        value = scalar(cur, "SELECT MAX(order_month) FROM dws_amz_order_source_monthly")
        if not value:
            raise RuntimeError("dws_amz_order_source_monthly 没有数据")
        end_exclusive = add_month(month_date(value))

    if start >= end_exclusive:
        raise RuntimeError("开始月份必须早于结束月份")

    if stores_text:
        stores = [x.strip() for x in stores_text.split(",") if x.strip()]
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
                (start, end_exclusive),
            )
        ]

    include_partial = os.getenv("ATTR_INCLUDE_PARTIAL_PERIODS", "").strip().lower() in {
        "1", "true", "yes", "y"
    }
    return start, end_exclusive, stores, include_partial


def table_exists(cur, table_name: str) -> bool:
    return bool(
        scalar(
            cur,
            """
            SELECT COUNT(*)
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
            """,
            (table_name,),
        )
    )


def resolve_audit_table(cur) -> str | None:
    for name in (
        "dws_amz_product_performance_ad_monthly_audit",
        "dws_amz_product_ad_allocation_monthly_audit",
    ):
        if table_exists(cur, name):
            return name
    return None


def fetch_data(cur, start: date, end_exclusive: date, stores: list[str]):
    placeholders = ",".join(["%s"] * len(stores))
    params = [start, end_exclusive, *stores]
    detail = query_rows(
        cur,
        f"""
        SELECT order_month,store_name,main_order_type,classified_item_rows,
               order_sku_count,amazon_order_count,units,
               gross_item_sales,net_item_sales
        FROM dws_amz_order_source_monthly
        WHERE order_month >= %s
          AND order_month < %s
          AND store_name IN ({placeholders})
        ORDER BY order_month,store_name,
          FIELD(main_order_type,'广告','站外推广','站内促销','低价','自然')
        """,
        params,
    )

    audit_table = resolve_audit_table(cur)
    audit: list[dict[str, Any]] = []
    if audit_table:
        audit = query_rows(
            cur,
            f"""
            SELECT order_month,store_name,product_order_items,
                   product_ad_order_target,product_promotion_orders,msku_count,
                   base_order_sku_count,offsite_order_sku_count,
                   non_offsite_capacity,allocated_ad_orders,
                   unallocated_ad_orders,allocation_pct,
                   source_gap_order_sku,created_at
            FROM {audit_table}
            WHERE order_month >= %s
              AND order_month < %s
              AND store_name IN ({placeholders})
            ORDER BY order_month,store_name
            """,
            params,
        )
    return detail, audit, audit_table


def blank_metric() -> dict[str, float]:
    return {
        "classified_item_rows": 0,
        "order_sku_count": 0,
        "amazon_order_count": 0,
        "units": 0,
        "gross": 0.0,
        "net": 0.0,
    }


def add_metric(target: dict[str, float], row: dict[str, Any]) -> None:
    target["classified_item_rows"] += int(row.get("classified_item_rows") or 0)
    target["order_sku_count"] += int(row.get("order_sku_count") or 0)
    target["amazon_order_count"] += int(row.get("amazon_order_count") or 0)
    target["units"] += int(row.get("units") or 0)
    target["gross"] += float(row.get("gross_item_sales") or 0)
    target["net"] += float(row.get("net_item_sales") or 0)


@dataclass(frozen=True)
class Period:
    label: str
    start: date
    end_exclusive: date
    expected_months: int
    complete: bool


def build_periods(start: date, end_exclusive: date, kind: str, include_partial: bool):
    if kind == "quarter":
        step = 3
        current = date(start.year, ((start.month - 1) // 3) * 3 + 1, 1)
    else:
        step = 6
        current = date(start.year, 1 if start.month <= 6 else 7, 1)

    periods: list[Period] = []
    while current < end_exclusive:
        end = add_month(current, step)
        overlap_start = max(start, current)
        overlap_end = min(end_exclusive, end)
        complete = overlap_start == current and overlap_end == end
        if complete or include_partial:
            if kind == "quarter":
                label = f"{current.year}-Q{((current.month - 1) // 3) + 1}"
            else:
                label = f"{current.year}{'上半年' if current.month == 1 else '下半年'}"
            periods.append(Period(label, overlap_start, overlap_end, step, complete))
        current = end
    return periods


def aggregate_period(detail: list[dict[str, Any]], periods: list[Period]):
    data = defaultdict(blank_metric)
    covered = defaultdict(set)
    for row in detail:
        m = month_date(row["order_month"])
        for p in periods:
            if p.start <= m < p.end_exclusive:
                add_metric(data[(p.label, row["store_name"], row["main_order_type"])], row)
                covered[(p.label, row["store_name"])].add(m.strftime("%Y-%m"))
                break
    return data, covered


def add_formats(workbook):
    return {
        "title": workbook.add_format({
            "bold": True, "font_size": 18, "font_color": "#FFFFFF",
            "bg_color": "#1F4E78", "align": "left", "valign": "vcenter",
        }),
        "header": workbook.add_format({
            "bold": True, "font_color": "#FFFFFF", "bg_color": "#4472C4",
            "border": 1, "align": "center", "valign": "vcenter",
            "text_wrap": True,
        }),
        "section": workbook.add_format({
            "bold": True, "bg_color": "#D9EAF7", "border": 1,
        }),
        "text": workbook.add_format({"border": 1}),
        "wrap": workbook.add_format({"border": 1, "text_wrap": True, "valign": "top"}),
        "int": workbook.add_format({"border": 1, "num_format": "#,##0"}),
        "money": workbook.add_format({"border": 1, "num_format": "$#,##0.00"}),
        "pct": workbook.add_format({"border": 1, "num_format": "0.00%"}),
        "blank": workbook.add_format({"border": 1, "align": "center", "font_color": "#808080"}),
        "overlay": workbook.add_format({"border": 1, "bg_color": "#E7E6E6"}),
        "overlay_int": workbook.add_format({"border": 1, "bg_color": "#E7E6E6", "num_format": "#,##0"}),
        "overlay_money": workbook.add_format({"border": 1, "bg_color": "#E7E6E6", "num_format": "$#,##0.00"}),
    }


def primary_totals(metrics: dict[str, dict[str, float]]) -> dict[str, float]:
    total = blank_metric()
    for source in PRIMARY_ORDER:
        m = metrics.get(source, blank_metric())
        for key in total:
            total[key] += m[key]
    return total


def write_source_rows(ws, start_row: int, metrics, formats, include_total=True):
    total = primary_totals(metrics)
    row = start_row
    for source in DISPLAY_ORDER:
        m = metrics.get(source, blank_metric())
        overlay = source == DISPLAY_ONLY
        text_fmt = formats["overlay"] if overlay else formats["text"]
        int_fmt = formats["overlay_int"] if overlay else formats["int"]
        money_fmt = formats["overlay_money"] if overlay else formats["money"]
        ws.write(row, 0, source, text_fmt)
        ws.write_number(row, 1, m["order_sku_count"], int_fmt)
        if overlay:
            ws.write(row, 2, "—", formats["blank"])
        else:
            share = m["order_sku_count"] / total["order_sku_count"] if total["order_sku_count"] else 0
            ws.write_number(row, 2, share, formats["pct"])
        ws.write_number(row, 3, m["units"], int_fmt)
        ws.write_number(row, 4, m["net"], money_fmt)
        if overlay:
            ws.write(row, 5, "—", formats["blank"])
        else:
            share = m["net"] / total["net"] if total["net"] else 0
            ws.write_number(row, 5, share, formats["pct"])
        row += 1
    if include_total:
        ws.write(row, 0, "主来源合计", formats["header"])
        ws.write_number(row, 1, total["order_sku_count"], formats["int"])
        ws.write_number(row, 2, 1, formats["pct"])
        ws.write_number(row, 3, total["units"], formats["int"])
        ws.write_number(row, 4, total["net"], formats["money"])
        ws.write_number(row, 5, 1, formats["pct"])
    return row


def write_period_sheet(workbook, name, periods, stores, detail, formats):
    ws = workbook.add_worksheet(name)
    headers = [
        "统计周期", "周期状态", "店铺", "来源类型", "指标角色",
        "覆盖月份数", "订单-SKU数", "订单占比", "销量",
        "净商品销售额", "销售额占比",
    ]
    for c, h in enumerate(headers):
        ws.write(0, c, h, formats["header"])
    data, covered = aggregate_period(detail, periods)
    r = 1
    for p in periods:
        for store in ["全部店铺", *stores]:
            by_source = {}
            for source in DISPLAY_ORDER:
                metric = blank_metric()
                targets = stores if store == "全部店铺" else [store]
                for target_store in targets:
                    source_metric = data[(p.label, target_store, source)]
                    for key in metric:
                        metric[key] += source_metric[key]
                by_source[source] = metric
            total = primary_totals(by_source)
            month_count = (
                len(set().union(*(covered.get((p.label, s), set()) for s in stores)))
                if store == "全部店铺"
                else len(covered.get((p.label, store), set()))
            )
            status = "完整周期" if p.complete and month_count == p.expected_months else "未完结周期"
            for source in DISPLAY_ORDER:
                m = by_source[source]
                overlay = source == DISPLAY_ONLY
                values = [
                    p.label, status, store, source,
                    "展示项" if overlay else "主来源",
                    month_count, m["order_sku_count"],
                    None if overlay else (m["order_sku_count"] / total["order_sku_count"] if total["order_sku_count"] else 0),
                    m["units"], m["net"],
                    None if overlay else (m["net"] / total["net"] if total["net"] else 0),
                ]
                for c, v in enumerate(values):
                    if v is None:
                        ws.write(r, c, "—", formats["blank"])
                    elif c in (6, 8):
                        ws.write_number(r, c, v, formats["overlay_int"] if overlay else formats["int"])
                    elif c == 9:
                        ws.write_number(r, c, v, formats["overlay_money"] if overlay else formats["money"])
                    elif c in (7, 10):
                        ws.write_number(r, c, v, formats["pct"])
                    else:
                        ws.write(r, c, v, formats["overlay"] if overlay else formats["text"])
                r += 1
    ws.freeze_panes(1, 0)
    ws.set_column("A:E", 15)
    ws.set_column("F:I", 14)
    ws.set_column("J:J", 18)
    ws.set_column("K:K", 14)
    if r > 1:
        ws.add_table(0, 0, r - 1, len(headers) - 1, {
            "name": "QuarterSummary" if name == "季度汇总" else "HalfYearSummary",
            "columns": [{"header": h} for h in headers],
            "style": "Table Style Medium 2",
        })


def build_workbook(output_path, detail, audit, audit_table, start, end_exclusive, stores, include_partial):
    workbook = xlsxwriter.Workbook(output_path)
    formats = add_formats(workbook)

    overall = defaultdict(blank_metric)
    monthly = defaultdict(lambda: defaultdict(blank_metric))
    store_month = defaultdict(lambda: defaultdict(blank_metric))
    months = sorted({month_date(row["order_month"]) for row in detail})

    for row in detail:
        source = row["main_order_type"]
        m = month_date(row["order_month"]).strftime("%Y-%m")
        add_metric(overall[source], row)
        add_metric(monthly[m][source], row)
        add_metric(store_month[(m, row["store_name"])][source], row)

    ws = workbook.add_worksheet("总览")
    ws.merge_range("A1:F1", "Amazon 五类订单来源汇总", formats["title"])
    ws.write("A2", f"期间：{start:%Y-%m} 至 {add_month(end_exclusive, -1):%Y-%m}", formats["text"])
    ws.write("A3", "站内促销为展示项，可与主来源重叠，不参与合计和占比。", formats["wrap"])
    headers = ["来源类型", "订单-SKU数", "订单占比", "销量", "净商品销售额", "销售额占比"]
    for c, h in enumerate(headers):
        ws.write(4, c, h, formats["header"])
    write_source_rows(ws, 5, overall, formats)

    chart = workbook.add_chart({"type": "column", "subtype": "stacked"})
    month_start_row = 14
    ws.write(month_start_row, 0, "月份", formats["header"])
    for idx, source in enumerate(PRIMARY_ORDER, 1):
        ws.write(month_start_row, idx, source, formats["header"])
    for ridx, m in enumerate([x.strftime("%Y-%m") for x in months], month_start_row + 1):
        total = primary_totals(monthly[m])
        ws.write(ridx, 0, m, formats["text"])
        for cidx, source in enumerate(PRIMARY_ORDER, 1):
            value = monthly[m][source]["order_sku_count"] / total["order_sku_count"] if total["order_sku_count"] else 0
            ws.write_number(ridx, cidx, value, formats["pct"])
    if months:
        first = month_start_row + 2
        last = month_start_row + 1 + len(months)
        for idx, source in enumerate(PRIMARY_ORDER, 1):
            col = xlsxwriter.utility.xl_col_to_name(idx)
            chart.add_series({
                "name": source,
                "categories": f"='总览'!$A${first}:$A${last}",
                "values": f"='总览'!${col}${first}:${col}${last}",
                "fill": {"color": COLORS[source]},
                "border": {"none": True},
            })
        chart.set_title({"name": "逐月主来源占比（站内促销不参与）"})
        chart.set_y_axis({"num_format": "0%", "min": 0, "max": 1})
        chart.set_legend({"position": "bottom"})
        chart.set_size({"width": 900, "height": 360})
        ws.insert_chart("H5", chart)
    ws.set_column("A:A", 18)
    ws.set_column("B:F", 16)

    ws2 = workbook.add_worksheet("月度汇总")
    headers2 = ["月份", "来源类型", "指标角色", "订单-SKU数", "订单占比", "销量", "毛商品销售额", "净商品销售额", "销售额占比"]
    for c, h in enumerate(headers2):
        ws2.write(0, c, h, formats["header"])
    r = 1
    for m in [x.strftime("%Y-%m") for x in months]:
        total = primary_totals(monthly[m])
        for source in DISPLAY_ORDER:
            v = monthly[m][source]
            overlay = source == DISPLAY_ONLY
            vals = [
                m, source, "展示项" if overlay else "主来源",
                v["order_sku_count"],
                None if overlay else (v["order_sku_count"] / total["order_sku_count"] if total["order_sku_count"] else 0),
                v["units"], v["gross"], v["net"],
                None if overlay else (v["net"] / total["net"] if total["net"] else 0),
            ]
            for c, val in enumerate(vals):
                if val is None:
                    ws2.write(r, c, "—", formats["blank"])
                elif c in (3, 5):
                    ws2.write_number(r, c, val, formats["overlay_int"] if overlay else formats["int"])
                elif c in (6, 7):
                    ws2.write_number(r, c, val, formats["overlay_money"] if overlay else formats["money"])
                elif c in (4, 8):
                    ws2.write_number(r, c, val, formats["pct"])
                else:
                    ws2.write(r, c, val, formats["overlay"] if overlay else formats["text"])
            r += 1
    ws2.freeze_panes(1, 0)
    ws2.set_column("A:C", 15)
    ws2.set_column("D:I", 17)

    quarters = build_periods(start, end_exclusive, "quarter", include_partial)
    halves = build_periods(start, end_exclusive, "half", include_partial)
    write_period_sheet(workbook, "季度汇总", quarters, stores, detail, formats)
    write_period_sheet(workbook, "半年度汇总", halves, stores, detail, formats)

    ws3 = workbook.add_worksheet("店铺月度明细")
    headers3 = ["月份", "店铺", "来源类型", "指标角色", "明细行数", "订单-SKU数", "Amazon订单数", "销量", "毛商品销售额", "净商品销售额", "店铺月内占比"]
    for c, h in enumerate(headers3):
        ws3.write(0, c, h, formats["header"])
    r = 1
    for m in [x.strftime("%Y-%m") for x in months]:
        for store in stores:
            total = primary_totals(store_month[(m, store)])
            for source in DISPLAY_ORDER:
                v = store_month[(m, store)][source]
                overlay = source == DISPLAY_ONLY
                values = [
                    m, store, source, "展示项" if overlay else "主来源",
                    v["classified_item_rows"], v["order_sku_count"], v["amazon_order_count"],
                    v["units"], v["gross"], v["net"],
                    None if overlay else (v["order_sku_count"] / total["order_sku_count"] if total["order_sku_count"] else 0),
                ]
                for c, val in enumerate(values):
                    if val is None:
                        ws3.write(r, c, "—", formats["blank"])
                    elif c in (4, 5, 6, 7):
                        ws3.write_number(r, c, val, formats["overlay_int"] if overlay else formats["int"])
                    elif c in (8, 9):
                        ws3.write_number(r, c, val, formats["overlay_money"] if overlay else formats["money"])
                    elif c == 10:
                        ws3.write_number(r, c, val, formats["pct"])
                    else:
                        ws3.write(r, c, val, formats["overlay"] if overlay else formats["text"])
                r += 1
    ws3.freeze_panes(1, 0)
    ws3.set_column("A:D", 15)
    ws3.set_column("E:K", 17)

    ws4 = workbook.add_worksheet("广告审计")
    audit_headers = [
        "月份", "店铺", "产品表现订单量", "广告目标", "产品表现促销订单",
        "MSKU数", "有效订单-SKU", "站外订单-SKU", "非站外容量",
        "已分配广告订单", "未分配广告订单", "分配完成率",
        "两数据源订单差异", "差异率", "结果状态", "生成时间",
    ]
    for c, h in enumerate(audit_headers):
        ws4.write(0, c, h, formats["header"])
    for r, row in enumerate(audit, 1):
        product_orders = int(row.get("product_order_items") or 0)
        gap = int(row.get("source_gap_order_sku") or 0)
        unallocated = int(row.get("unallocated_ad_orders") or 0)
        alloc_pct = float(row.get("allocation_pct") or 0) / 100
        values = [
            month_date(row["order_month"]).strftime("%Y-%m"), row["store_name"],
            product_orders, int(row.get("product_ad_order_target") or 0),
            int(row.get("product_promotion_orders") or 0), int(row.get("msku_count") or 0),
            int(row.get("base_order_sku_count") or 0), int(row.get("offsite_order_sku_count") or 0),
            int(row.get("non_offsite_capacity") or 0), int(row.get("allocated_ad_orders") or 0),
            unallocated, alloc_pct, gap, gap / product_orders if product_orders else 0,
            "通过" if unallocated == 0 and alloc_pct >= 0.999999 else "需检查",
            row.get("created_at").strftime("%Y-%m-%d %H:%M:%S") if hasattr(row.get("created_at"), "strftime") else str(row.get("created_at") or ""),
        ]
        for c, val in enumerate(values):
            if c in (2,3,4,5,6,7,8,9,10,12):
                ws4.write_number(r, c, val, formats["int"])
            elif c in (11,13):
                ws4.write_number(r, c, val, formats["pct"])
            else:
                ws4.write(r, c, val, formats["text"])
    if not audit:
        ws4.write(1, 0, "未找到广告审计表，当前仅导出来源汇总。", formats["wrap"])
    ws4.freeze_panes(1, 0)
    ws4.set_column("A:P", 17)

    ws5 = workbook.add_worksheet("口径说明")
    ws5.merge_range("A1:B1", "Amazon 五类来源统计口径", formats["title"])
    definitions = [
        ("主来源", "广告、站外推广、低价、自然。四类互斥，参与订单合计、销售额合计和占比计算。"),
        ("站内促销", "作为展示标签，可与广告或自然等主来源重叠；显示订单量、销量和销售额，但不参与合计与占比。"),
        ("低价", "排除站外推广、广告和站内促销后，净成交单价=(item_price-item_promotion_discount)/quantity，且净成交单价<=10美元。"),
        ("自然", "未归入站外推广、广告或低价的剩余订单；其中包含未归入广告/站外的站内促销订单。"),
        ("图表", "占比图只展示广告、站外推广、低价、自然四个主来源。"),
        ("统计周期", "月度为事实基础；季度和半年度由月度结果汇总。默认只输出完整周期。"),
        ("规则版本", "v5_10usd_promo_overlay_20260729。"),
        ("审计表", audit_table or "未找到广告审计表。"),
    ]
    for r, (term, desc) in enumerate(definitions, 1):
        ws5.write(r, 0, term, formats["section"])
        ws5.write(r, 1, desc, formats["wrap"])
    ws5.set_column("A:A", 22)
    ws5.set_column("B:B", 100)

    workbook.close()


def main() -> int:
    with connect_db() as conn:
        with conn.cursor() as cur:
            start, end_exclusive, stores, include_partial = resolve_filters(cur)
            detail, audit, audit_table = fetch_data(cur, start, end_exclusive, stores)

    if not detail:
        raise RuntimeError("指定范围内没有来源月度汇总数据")

    output_text = os.getenv("ATTR_EXPORT_OUTPUT", "").strip()
    if output_text:
        output_path = Path(output_text)
    else:
        output_dir = Path(__file__).resolve().parents[1] / "exports"
        output_dir.mkdir(parents=True, exist_ok=True)
        last = add_month(end_exclusive, -1)
        output_path = output_dir / f"amazon_order_source_summary_{start:%Y%m}_{last:%Y%m}.xlsx"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    build_workbook(output_path, detail, audit, audit_table, start, end_exclusive, stores, include_partial)
    print(f"Excel已生成：{output_path}")
    print("Sheet：总览、月度汇总、季度汇总、半年度汇总、店铺月度明细、广告审计、口径说明")
    print("占比口径：站内促销仅展示，不参与合计和占比")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"导出失败：{exc}", file=sys.stderr)
        raise
