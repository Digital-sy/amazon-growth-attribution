#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出 Amazon 五分类订单来源汇总 Excel。

工作簿保留原有月度与审计 Sheet，并新增季度、半年度汇总：
- 总览
- 月度汇总
- 季度汇总
- 半年度汇总
- 店铺月度明细
- 广告审计
- 口径说明

数据源：
- dws_amz_order_source_monthly
- dws_amz_product_ad_allocation_monthly_audit

环境变量：
- LINGXING_DB_HOST / LINGXING_DB_PORT / LINGXING_DB_USER / LINGXING_DB_PASSWORD
- LINGXING_DB_NAME，默认 lingxing
- ATTR_EXPORT_START_MONTH，默认结果表最早月份，格式 YYYY-MM-01
- ATTR_EXPORT_END_MONTH_EXCLUSIVE，默认结果表最大月份+1月
- ATTR_STORES，逗号分隔；默认全部店铺
- ATTR_INCLUDE_PARTIAL_PERIODS，默认0；设为1时包含未完结季度/半年度
- ATTR_EXPORT_OUTPUT，可指定输出文件路径
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from dataclasses import dataclass
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


def month_text(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m")
    return str(value)[:7]


def month_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date().replace(day=1)
    if isinstance(value, date):
        return value.replace(day=1)
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date().replace(day=1)


def add_month_date(value: date, count: int = 1) -> date:
    month_index = value.year * 12 + value.month - 1 + count
    return date(month_index // 12, month_index % 12 + 1, 1)


def add_month(month_start: str) -> str:
    return add_month_date(datetime.strptime(month_start, "%Y-%m-%d").date()).isoformat()


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

    if datetime.strptime(start_month, "%Y-%m-%d") >= datetime.strptime(end_month, "%Y-%m-%d"):
        raise RuntimeError("开始月份必须早于结束月份")

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

    include_partial = os.getenv("ATTR_INCLUDE_PARTIAL_PERIODS", "").strip().lower() in {
        "1", "true", "yes", "y"
    }
    return start_month, end_month, stores, include_partial


def fetch_data(cur, start_month: str, end_month: str, stores: list[str]):
    placeholders = ",".join(["%s"] * len(stores))
    params = [start_month, end_month, *stores]

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
        params,
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
        params,
    )
    return detail, audit


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
    target["gross"] += float(row.get("gross_item_sales") or row.get("gross") or 0)
    target["net"] += float(row.get("net_item_sales") or row.get("net") or 0)


def aggregate(detail: list[dict[str, Any]]):
    overall = defaultdict(blank_metric)
    monthly = defaultdict(lambda: defaultdict(blank_metric))
    monthly_total = defaultdict(blank_metric)

    for row in detail:
        category = row["main_order_type"]
        month = month_text(row["order_month"])
        add_metric(overall[category], row)
        add_metric(monthly[month][category], row)
        add_metric(monthly_total[month], row)

    overall_total = blank_metric()
    for metric in overall.values():
        add_metric(overall_total, metric)
    return overall, overall_total, monthly, monthly_total


@dataclass(frozen=True)
class Period:
    label: str
    start: date
    end_exclusive: date
    expected_months: int
    complete_by_range: bool


def build_periods(start_month: str, end_month: str, kind: str, include_partial: bool) -> list[Period]:
    start = datetime.strptime(start_month, "%Y-%m-%d").date()
    end = datetime.strptime(end_month, "%Y-%m-%d").date()

    if kind == "quarter":
        first = date(start.year, ((start.month - 1) // 3) * 3 + 1, 1)
        step = 3
    elif kind == "half":
        first = date(start.year, 1 if start.month <= 6 else 7, 1)
        step = 6
    else:
        raise ValueError(f"未知周期类型：{kind}")

    periods: list[Period] = []
    current = first
    while current < end:
        period_end = add_month_date(current, step)
        overlap_start = max(current, start)
        overlap_end = min(period_end, end)
        complete = overlap_start == current and overlap_end == period_end

        if kind == "quarter":
            label = f"{current.year}-Q{((current.month - 1) // 3) + 1}"
        else:
            label = f"{current.year}{'上半年' if current.month == 1 else '下半年'}"

        if complete or include_partial:
            periods.append(
                Period(
                    label=label,
                    start=overlap_start,
                    end_exclusive=overlap_end,
                    expected_months=step,
                    complete_by_range=complete,
                )
            )
        current = period_end
    return periods


def aggregate_periods(detail: list[dict[str, Any]], periods: list[Period], stores: list[str]):
    metrics = defaultdict(blank_metric)
    covered_months = defaultdict(set)

    for row in detail:
        row_month = month_date(row["order_month"])
        store = row["store_name"]
        source = row["main_order_type"]
        for period in periods:
            if period.start <= row_month < period.end_exclusive:
                add_metric(metrics[(period.label, "全部店铺", source)], row)
                add_metric(metrics[(period.label, store, source)], row)
                covered_months[(period.label, "全部店铺")].add(month_text(row["order_month"]))
                covered_months[(period.label, store)].add(month_text(row["order_month"]))
                break

    return metrics, covered_months, ["全部店铺", *stores]


def write_period_sheet(workbook, sheet_name: str, periods: list[Period], detail, stores, formats) -> None:
    ws = workbook.add_worksheet(sheet_name)
    ws.freeze_panes(1, 0)
    ws.hide_gridlines(2)

    headers = [
        "统计周期", "周期状态", "店铺", "分类", "覆盖月份数",
        "订单-SKU数", "占比", "销量", "毛商品销售额",
        "净商品销售额", "净销售额占比",
    ]
    for col, header in enumerate(headers):
        ws.write(0, col, header, formats["header"])

    metrics, covered_months, display_stores = aggregate_periods(detail, periods, stores)
    row_no = 1

    for period in periods:
        for store in display_stores:
            total_orders = sum(metrics[(period.label, store, source)]["order_sku_count"] for source in CLASS_ORDER)
            total_net = sum(metrics[(period.label, store, source)]["net"] for source in CLASS_ORDER)
            month_count = len(covered_months.get((period.label, store), set()))
            complete = period.complete_by_range and month_count == period.expected_months
            status = "完整周期" if complete else "未完结/缺月"

            for source in CLASS_ORDER:
                metric = metrics[(period.label, store, source)]
                values = [
                    period.label,
                    status,
                    store,
                    source,
                    month_count,
                    metric["order_sku_count"],
                    metric["order_sku_count"] / total_orders if total_orders else 0,
                    metric["units"],
                    metric["gross"],
                    metric["net"],
                    metric["net"] / total_net if total_net else 0,
                ]
                cell_formats = [
                    formats["text"],
                    formats["status"] if status == "完整周期" else formats["partial"],
                    formats["text"], formats["text"], formats["int"],
                    formats["int"], formats["pct"], formats["int"],
                    formats["money"], formats["money"], formats["pct"],
                ]
                for col, (value, cell_format) in enumerate(zip(values, cell_formats)):
                    if isinstance(value, (int, float)):
                        ws.write_number(row_no, col, value, cell_format)
                    else:
                        ws.write(row_no, col, value, cell_format)
                row_no += 1

    if row_no > 1:
        table_name = "QuarterSummaryTable" if sheet_name == "季度汇总" else "HalfYearSummaryTable"
        ws.add_table(
            0, 0, row_no - 1, len(headers) - 1,
            {
                "name": table_name,
                "columns": [{"header": header} for header in headers],
                "style": "Table Style Medium 2",
            },
        )

    ws.set_column("A:D", 15)
    ws.set_column("E:H", 14)
    ws.set_column("I:J", 18)
    ws.set_column("K:K", 14)


def build_workbook(output_path: Path, detail, audit, start_month: str, end_month: str, stores, include_partial: bool):
    overall, overall_total, monthly, monthly_total = aggregate(detail)
    months = sorted(monthly.keys())
    quarters = build_periods(start_month, end_month, "quarter", include_partial)
    halves = build_periods(start_month, end_month, "half", include_partial)

    workbook = xlsxwriter.Workbook(output_path)
    workbook.set_properties(
        {
            "title": "Amazon五分类订单来源汇总",
            "subject": "月度、季度、半年度订单来源结构",
            "author": "amazon-growth-attribution",
            "company": "尚亿数据",
            "comments": "低价阈值为排除前三类后净成交单价不高于7美元。",
        }
    )

    title_fmt = workbook.add_format({"bold": True, "font_size": 18, "font_color": "#FFFFFF", "bg_color": "#1F4E78", "align": "left", "valign": "vcenter"})
    subtitle_fmt = workbook.add_format({"font_color": "#666666", "font_size": 10})
    header_fmt = workbook.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#4472C4", "border": 1, "align": "center", "valign": "vcenter", "text_wrap": True})
    section_fmt = workbook.add_format({"bold": True, "font_size": 12, "font_color": "#1F1F1F", "bg_color": "#D9EAF7", "border": 1, "align": "left"})
    text_fmt = workbook.add_format({"border": 1, "valign": "top"})
    wrap_fmt = workbook.add_format({"border": 1, "valign": "top", "text_wrap": True})
    int_fmt = workbook.add_format({"border": 1, "num_format": "#,##0"})
    pct_fmt = workbook.add_format({"border": 1, "num_format": "0.00%"})
    money_fmt = workbook.add_format({"border": 1, "num_format": "$#,##0.00"})
    kpi_label_fmt = workbook.add_format({"bold": True, "font_color": "#666666", "align": "center", "valign": "vcenter"})
    kpi_value_fmt = workbook.add_format({"bold": True, "font_size": 18, "font_color": "#1F4E78", "align": "center", "valign": "vcenter", "num_format": "#,##0"})
    kpi_money_fmt = workbook.add_format({"bold": True, "font_size": 18, "font_color": "#1F4E78", "align": "center", "valign": "vcenter", "num_format": "$#,##0.00"})
    status_fmt = workbook.add_format({"border": 1, "bg_color": "#C6EFCE", "font_color": "#006100"})
    partial_fmt = workbook.add_format({"border": 1, "bg_color": "#FFF2CC", "font_color": "#9C6500"})

    period_formats = {
        "header": header_fmt,
        "text": text_fmt,
        "int": int_fmt,
        "pct": pct_fmt,
        "money": money_fmt,
        "status": status_fmt,
        "partial": partial_fmt,
    }

    ws = workbook.add_worksheet("总览")
    ws.hide_gridlines(2)
    ws.set_column("A:A", 2)
    ws.set_column("B:G", 16)
    ws.set_column("H:N", 14)
    ws.set_row(0, 30)
    ws.merge_range("B1:N1", "Amazon五分类订单来源汇总", title_fmt)
    ws.write("B2", f"期间：{start_month[:7]} 至 {end_month[:7]}（结束月不含）｜店铺：{', '.join(stores)}", subtitle_fmt)
    ws.write("B3", f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", subtitle_fmt)

    kpis = [
        ("总订单-SKU", overall_total["order_sku_count"], kpi_value_fmt),
        ("总销量", overall_total["units"], kpi_value_fmt),
        ("净商品销售额", overall_total["net"], kpi_money_fmt),
        ("月份数", len(months), kpi_value_fmt),
    ]
    for (label, value, fmt), col in zip(kpis, [1, 4, 7, 10]):
        ws.merge_range(4, col, 4, col + 2, label, kpi_label_fmt)
        ws.merge_range(5, col, 6, col + 2, value, fmt)

    ws.merge_range("B9:H9", "全期间五类结构", section_fmt)
    headers = ["分类", "订单-SKU数", "占比", "销量", "净商品销售额", "销售额占比"]
    for col, header in enumerate(headers, 1):
        ws.write(9, col, header, header_fmt)

    row_no = 10
    for category in CLASS_ORDER:
        metric = overall[category]
        ws.write(row_no, 1, category, text_fmt)
        ws.write_number(row_no, 2, metric["order_sku_count"], int_fmt)
        ws.write_number(row_no, 3, metric["order_sku_count"] / overall_total["order_sku_count"] if overall_total["order_sku_count"] else 0, pct_fmt)
        ws.write_number(row_no, 4, metric["units"], int_fmt)
        ws.write_number(row_no, 5, metric["net"], money_fmt)
        ws.write_number(row_no, 6, metric["net"] / overall_total["net"] if overall_total["net"] else 0, pct_fmt)
        row_no += 1

    ws.write(row_no, 1, "合计", header_fmt)
    ws.write_number(row_no, 2, overall_total["order_sku_count"], int_fmt)
    ws.write_number(row_no, 3, 1, pct_fmt)
    ws.write_number(row_no, 4, overall_total["units"], int_fmt)
    ws.write_number(row_no, 5, overall_total["net"], money_fmt)
    ws.write_number(row_no, 6, 1, pct_fmt)

    monthly_start = 18
    ws.merge_range(monthly_start, 1, monthly_start, 13, "逐月五类占比", section_fmt)
    monthly_headers = ["月份", "总订单-SKU"] + [f"{category}数" for category in CLASS_ORDER] + [f"{category}占比" for category in CLASS_ORDER] + ["净商品销售额"]
    for col, header in enumerate(monthly_headers, 1):
        ws.write(monthly_start + 1, col, header, header_fmt)

    for row_idx, month in enumerate(months, monthly_start + 2):
        total = monthly_total[month]["order_sku_count"]
        ws.write(row_idx, 1, month, text_fmt)
        ws.write_number(row_idx, 2, total, int_fmt)
        col = 3
        for category in CLASS_ORDER:
            ws.write_number(row_idx, col, monthly[month][category]["order_sku_count"], int_fmt)
            col += 1
        for category in CLASS_ORDER:
            share = monthly[month][category]["order_sku_count"] / total if total else 0
            ws.write_number(row_idx, col, share, pct_fmt)
            col += 1
        ws.write_number(row_idx, col, monthly_total[month]["net"], money_fmt)

    if months:
        first_excel_row = monthly_start + 3
        last_excel_row = monthly_start + 2 + len(months)
        chart = workbook.add_chart({"type": "column", "subtype": "stacked"})
        for idx, category in enumerate(CLASS_ORDER):
            chart.add_series({
                "name": category,
                "categories": f"='总览'!$B${first_excel_row}:$B${last_excel_row}",
                "values": f"='总览'!${xlsxwriter.utility.xl_col_to_name(8 + idx)}${first_excel_row}:${xlsxwriter.utility.xl_col_to_name(8 + idx)}${last_excel_row}",
                "fill": {"color": CLASS_COLORS[category]},
                "border": {"none": True},
            })
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
    for col, header in enumerate(summary_headers):
        ws2.write(0, col, header, header_fmt)

    row_no = 1
    for month in months:
        total = monthly_total[month]
        for category in CLASS_ORDER:
            metric = monthly[month][category]
            values = [
                month, category, metric["order_sku_count"],
                metric["order_sku_count"] / total["order_sku_count"] if total["order_sku_count"] else 0,
                metric["units"], metric["gross"], metric["net"],
                metric["net"] / total["net"] if total["net"] else 0,
            ]
            formats = [text_fmt, text_fmt, int_fmt, pct_fmt, int_fmt, money_fmt, money_fmt, pct_fmt]
            for col, (value, fmt) in enumerate(zip(values, formats)):
                if isinstance(value, (int, float)):
                    ws2.write_number(row_no, col, value, fmt)
                else:
                    ws2.write(row_no, col, value, fmt)
            row_no += 1

    if row_no > 1:
        ws2.add_table(0, 0, row_no - 1, len(summary_headers) - 1, {
            "name": "MonthlySummaryTable",
            "columns": [{"header": header} for header in summary_headers],
            "style": "Table Style Medium 2",
        })

    write_period_sheet(workbook, "季度汇总", quarters, detail, stores, period_formats)
    write_period_sheet(workbook, "半年度汇总", halves, detail, stores, period_formats)

    ws3 = workbook.add_worksheet("店铺月度明细")
    detail_headers = ["月份", "店铺", "分类", "明细行数", "订单-SKU数", "Amazon订单数（不可跨类加总）", "销量", "毛商品销售额", "净商品销售额", "店铺月内占比"]
    for col, header in enumerate(detail_headers):
        ws3.write(0, col, header, header_fmt)
    ws3.freeze_panes(1, 0)
    ws3.set_column("A:A", 12)
    ws3.set_column("B:C", 14)
    ws3.set_column("D:G", 18)
    ws3.set_column("H:I", 18)
    ws3.set_column("J:J", 14)

    store_month_total = defaultdict(int)
    for row in detail:
        store_month_total[(month_text(row["order_month"]), row["store_name"])] += int(row.get("order_sku_count") or 0)

    for row_idx, row in enumerate(detail, 1):
        month = month_text(row["order_month"])
        store = row["store_name"]
        total = store_month_total[(month, store)]
        values = [
            month, store, row["main_order_type"], int(row.get("classified_item_rows") or 0),
            int(row.get("order_sku_count") or 0), int(row.get("amazon_order_count") or 0),
            int(row.get("units") or 0), float(row.get("gross_item_sales") or 0),
            float(row.get("net_item_sales") or 0), int(row.get("order_sku_count") or 0) / total if total else 0,
        ]
        formats = [text_fmt, text_fmt, text_fmt, int_fmt, int_fmt, int_fmt, int_fmt, money_fmt, money_fmt, pct_fmt]
        for col, (value, fmt) in enumerate(zip(values, formats)):
            if isinstance(value, (int, float)):
                ws3.write_number(row_idx, col, value, fmt)
            else:
                ws3.write(row_idx, col, value, fmt)

    if detail:
        ws3.add_table(0, 0, len(detail), len(detail_headers) - 1, {
            "name": "StoreMonthlyDetailTable",
            "columns": [{"header": header} for header in detail_headers],
            "style": "Table Style Medium 2",
        })

    ws4 = workbook.add_worksheet("广告审计")
    audit_headers = [
        "月份", "店铺", "产品表现订单量", "产品表现广告目标", "产品表现促销订单", "MSKU数",
        "有效订单-SKU", "站外订单-SKU", "非站外容量", "已分配广告订单", "未分配广告订单",
        "分配完成率", "两数据源订单差异", "差异率", "结果状态", "生成时间",
    ]
    for col, header in enumerate(audit_headers):
        ws4.write(0, col, header, header_fmt)
    ws4.freeze_panes(1, 0)
    ws4.set_column("A:B", 13)
    ws4.set_column("C:K", 17)
    ws4.set_column("L:N", 14)
    ws4.set_column("O:O", 12)
    ws4.set_column("P:P", 20)

    for row_idx, row in enumerate(audit, 1):
        product_orders = int(row.get("product_order_items") or 0)
        gap = int(row.get("source_gap_order_sku") or 0)
        gap_rate = gap / product_orders if product_orders else 0
        unallocated = int(row.get("unallocated_ad_orders") or 0)
        alloc_pct = float(row.get("allocation_pct") or 0) / 100
        status = "通过" if unallocated == 0 and alloc_pct >= 0.999999 else "需检查"
        values = [
            month_text(row["order_month"]), row["store_name"], product_orders,
            int(row.get("product_ad_order_target") or 0), int(row.get("product_promotion_orders") or 0),
            int(row.get("msku_count") or 0), int(row.get("base_order_sku_count") or 0),
            int(row.get("offsite_order_sku_count") or 0), int(row.get("non_offsite_capacity") or 0),
            int(row.get("allocated_ad_orders") or 0), unallocated, alloc_pct, gap, gap_rate, status,
            row.get("created_at").strftime("%Y-%m-%d %H:%M:%S") if hasattr(row.get("created_at"), "strftime") else str(row.get("created_at") or ""),
        ]
        formats = [text_fmt, text_fmt] + [int_fmt] * 9 + [pct_fmt, int_fmt, pct_fmt, text_fmt, text_fmt]
        for col, (value, fmt) in enumerate(zip(values, formats)):
            if isinstance(value, (int, float)):
                ws4.write_number(row_idx, col, value, fmt)
            else:
                ws4.write(row_idx, col, value, fmt)

    if audit:
        ws4.add_table(0, 0, len(audit), len(audit_headers) - 1, {
            "name": "AdAuditTable",
            "columns": [{"header": header} for header in audit_headers],
            "style": "Table Style Medium 2",
        })
        ws4.conditional_format(1, 14, len(audit), 14, {
            "type": "text", "criteria": "containing", "value": "需检查",
            "format": workbook.add_format({"bg_color": "#FFC7CE", "font_color": "#9C0006"}),
        })
        ws4.conditional_format(1, 13, len(audit), 13, {
            "type": "cell", "criteria": ">", "value": 0.01,
            "format": workbook.add_format({"bg_color": "#FFF2CC", "font_color": "#7F6000"}),
        })

    ws5 = workbook.add_worksheet("口径说明")
    ws5.hide_gridlines(2)
    ws5.set_column("A:A", 22)
    ws5.set_column("B:B", 92)
    ws5.merge_range("A1:B1", "Amazon五分类订单口径说明", title_fmt)

    definitions = [
        ("统计范围", f"{start_month[:7]} 至 {end_month[:7]}（结束月不含）；店铺：{', '.join(stores)}。"),
        ("统计周期", "工作簿同时保留月度结果，并新增季度、半年度汇总。Q1=1-3月、Q2=4-6月、Q3=7-9月、Q4=10-12月；上半年=1-6月、下半年=7-12月。"),
        ("完整周期", "默认只输出完整季度和完整半年度。设置 ATTR_INCLUDE_PARTIAL_PERIODS=1 可包含未完结周期，并标记为未完结/缺月。"),
        ("主统计指标", "订单-SKU数：按店铺+Amazon订单号+SKU去重。该指标在五类之间互斥、可加总，是本Excel的主订单指标。"),
        ("有效订单过滤", "订单来源为 ods_amz_all_orders_report；仅保留 sales_channel=Amazon.com、order_status=Shipped、item_status=Shipped、quantity>0、item_price>0、currency=USD、SKU非空、下单时间非空。"),
        ("时间口径", "purchase_date_utc 从 UTC 转换为 America/Los_Angeles 后确定订单日期和订单月份。"),
        ("分类优先级", "站外推广 ＞ 广告 ＞ 站内促销 ＞ 低价 ＞ 自然。Excel展示顺序为：广告、站外推广、站内促销、低价、自然。"),
        ("站外推广", "订单明细 promotion_ids 包含 MPC-。这是订单明细级确定规则，优先于月度广告统计分配。"),
        ("广告", "每店每月广告目标取 ods_lx_product_performance_monthly_msku 的 SUM(ad_order_quantity)。广告目标从非站外订单池中按固定顺序统计分配，不区分SP/SB/SBV/SD。"),
        ("广告标签限制", "广告属于店铺月度统计分配，不代表某一笔具体订单已被Amazon逐单确认为广告订单，不用于逐单追溯、申诉或SKU级精确归因。"),
        ("站内促销", "排除站外和广告后，promotion_ids 包含 Percentage Off 或 PLM-，或者 item_promotion_discount>0。仅有免运费促销不作为商品站内促销。"),
        ("低价", "排除站外、广告和站内促销后，净成交单价=(item_price-item_promotion_discount)/quantity，且净成交单价<=7美元。"),
        ("自然", "未命中站外、广告、站内促销或低价规则的剩余订单-SKU。"),
        ("销售额口径", "毛商品销售额为 item_price；净商品销售额为 item_price-item_promotion_discount。均为USD，不包含买家运费等其他金额。"),
        ("Amazon订单数", "amazon_order_count仅供参考。一个多SKU Amazon订单可能在不同SKU上被分到不同来源，因此跨分类相加会重复，不应作为五类合计口径。"),
        ("产品表现促销订单", "产品表现表 promotion_order_items用于审计和对照，不直接作为最终站内促销数量。"),
        ("数据源差异", "产品表现 order_items 与有效订单-SKU可能存在少量差异，广告审计表列出 source_gap_order_sku 和差异率。"),
        ("完整性要求", "每店每月：五类订单-SKU合计应等于有效订单-SKU；已分配广告订单应等于产品表现广告目标；未分配广告订单应为0。"),
        ("数据表", "最终月度汇总：dws_amz_order_source_monthly；季度和半年度由月度汇总动态聚合；广告审计：dws_amz_product_ad_allocation_monthly_audit。"),
        ("规则版本", "低价阈值为7美元；广告仍为产品表现月度统计分配。"),
    ]
    for idx, (term, desc) in enumerate(definitions, 2):
        ws5.write(idx - 1, 0, term, section_fmt)
        ws5.write(idx - 1, 1, desc, wrap_fmt)
        ws5.set_row(idx - 1, 38)

    ws5.write(len(definitions) + 2, 0, "重要说明", section_fmt)
    ws5.write(len(definitions) + 2, 1, "本Excel适合按月、季度、半年度和店铺观察五类订单结构及趋势；不应把统计分配后的具体广告订单行视为Amazon逐单广告归因证据。", wrap_fmt)

    workbook.close()


def main() -> int:
    with connect_db() as conn:
        with conn.cursor() as cur:
            start_month, end_month, stores, include_partial = resolve_filters(cur)
            detail, audit = fetch_data(cur, start_month, end_month, stores)

    if not detail:
        raise RuntimeError("指定范围内没有五分类月度汇总数据")

    output_env = os.getenv("ATTR_EXPORT_OUTPUT", "").strip()
    if output_env:
        output_path = Path(output_env)
    else:
        output_dir = Path(__file__).resolve().parents[1] / "exports"
        output_dir.mkdir(parents=True, exist_ok=True)
        end_inclusive = add_month_date(datetime.strptime(end_month, "%Y-%m-%d").date(), -1)
        output_path = output_dir / f"amazon_order_source_summary_{start_month[:7].replace('-', '')}_{end_inclusive:%Y%m}.xlsx"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    build_workbook(output_path, detail, audit, start_month, end_month, stores, include_partial)

    quarters = build_periods(start_month, end_month, "quarter", include_partial)
    halves = build_periods(start_month, end_month, "half", include_partial)
    print(f"Excel已生成：{output_path}")
    print(f"店铺：{', '.join(stores)}")
    print(f"月份：{start_month[:7]} 至 {end_month[:7]}（结束月不含）")
    print("季度：" + ", ".join(period.label for period in quarters))
    print("半年度：" + ", ".join(period.label for period in halves))
    print("Sheet：总览、月度汇总、季度汇总、半年度汇总、店铺月度明细、广告审计、口径说明")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"导出失败：{exc}", file=sys.stderr)
        raise
