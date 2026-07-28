#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出 Amazon 五类订单来源的月度、季度和半年度汇总 Excel。

数据源：dws_amz_order_source_monthly

默认行为：
- 只输出完整季度和完整半年度；
- 例如统计范围为 2025-01 至 2026-07 时：
  - 季度：2025-Q1/Q2/Q3/Q4、2026-Q1/Q2；
  - 半年度：2025上半年、2025下半年、2026上半年；
- 设置 ATTR_INCLUDE_PARTIAL_PERIODS=1 可同时输出未完结周期。

环境变量：
- LINGXING_DB_HOST / LINGXING_DB_PORT / LINGXING_DB_USER / LINGXING_DB_PASSWORD
- LINGXING_DB_NAME，默认 lingxing
- ATTR_EXPORT_START_MONTH，格式 YYYY-MM-01
- ATTR_EXPORT_END_MONTH_EXCLUSIVE，格式 YYYY-MM-01
- ATTR_STORES，逗号分隔
- ATTR_INCLUDE_PARTIAL_PERIODS，默认 0
- ATTR_PERIOD_OUTPUT，可指定输出路径
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
    print("请安装：pip install PyMySQL XlsxWriter", file=sys.stderr)
    raise SystemExit(2)

CLASS_ORDER = ["广告", "站外推广", "站内促销", "低价", "自然"]
DEFAULT_STORES = "JQ-US,MT-US,RKZ-US,SY-US"


def clean(value: Any) -> str:
    return str(value or "").strip()


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
        database=clean(os.getenv("LINGXING_DB_NAME")) or "lingxing",
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


def month_start(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date().replace(day=1)
    if isinstance(value, date):
        return value.replace(day=1)
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date().replace(day=1)


def add_month(value: date, count: int = 1) -> date:
    year = value.year
    month = value.month + count
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    return date(year, month, 1)


def month_sequence(start: date, end_exclusive: date) -> list[date]:
    result: list[date] = []
    current = start
    while current < end_exclusive:
        result.append(current)
        current = add_month(current)
    return result


@dataclass(frozen=True)
class Period:
    period_type: str
    label: str
    start: date
    end_exclusive: date
    expected_months: int
    is_complete: bool


def quarter_start(value: date) -> date:
    quarter_month = ((value.month - 1) // 3) * 3 + 1
    return date(value.year, quarter_month, 1)


def half_start(value: date) -> date:
    return date(value.year, 1 if value.month <= 6 else 7, 1)


def build_periods(
    start: date,
    end_exclusive: date,
    period_type: str,
    include_partial: bool,
) -> list[Period]:
    if period_type == "quarter":
        current = quarter_start(start)
        step = 3
    elif period_type == "half":
        current = half_start(start)
        step = 6
    else:
        raise ValueError(f"不支持的周期：{period_type}")

    periods: list[Period] = []
    while current < end_exclusive:
        period_end = add_month(current, step)
        overlap_start = max(current, start)
        overlap_end = min(period_end, end_exclusive)
        covered_months = len(month_sequence(overlap_start, overlap_end))
        is_complete = overlap_start == current and overlap_end == period_end

        if period_type == "quarter":
            quarter = (current.month - 1) // 3 + 1
            label = f"{current.year}-Q{quarter}"
        else:
            label = f"{current.year}{'上半年' if current.month == 1 else '下半年'}"

        if is_complete or include_partial:
            periods.append(
                Period(
                    period_type=period_type,
                    label=label,
                    start=overlap_start,
                    end_exclusive=overlap_end,
                    expected_months=step,
                    is_complete=is_complete and covered_months == step,
                )
            )
        current = period_end
    return periods


def resolve_filters(cur) -> tuple[date, date, list[str], bool]:
    start_env = clean(os.getenv("ATTR_EXPORT_START_MONTH"))
    end_env = clean(os.getenv("ATTR_EXPORT_END_MONTH_EXCLUSIVE"))

    if start_env:
        start = datetime.strptime(start_env, "%Y-%m-%d").date()
    else:
        value = scalar(cur, "SELECT MIN(order_month) FROM dws_amz_order_source_monthly")
        if not value:
            raise RuntimeError("dws_amz_order_source_monthly 没有数据")
        start = month_start(value)

    if end_env:
        end_exclusive = datetime.strptime(end_env, "%Y-%m-%d").date()
    else:
        value = scalar(cur, "SELECT MAX(order_month) FROM dws_amz_order_source_monthly")
        if not value:
            raise RuntimeError("dws_amz_order_source_monthly 没有数据")
        end_exclusive = add_month(month_start(value))

    if start >= end_exclusive:
        raise RuntimeError("开始月份必须早于结束月份")

    stores_env = clean(os.getenv("ATTR_STORES")) or DEFAULT_STORES
    stores = [item.strip() for item in stores_env.split(",") if item.strip()]
    if not stores:
        raise RuntimeError("店铺列表为空")

    include_partial = clean(os.getenv("ATTR_INCLUDE_PARTIAL_PERIODS")).lower() in {
        "1", "true", "yes", "y"
    }
    return start, end_exclusive, stores, include_partial


def fetch_monthly(cur, start: date, end_exclusive: date, stores: list[str]):
    placeholders = ",".join(["%s"] * len(stores))
    return query_rows(
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
        ORDER BY order_month,store_name,
          FIELD(main_order_type,'广告','站外推广','站内促销','低价','自然')
        """,
        [start.isoformat(), end_exclusive.isoformat(), *stores],
    )


def blank_metric() -> dict[str, float]:
    return {
        "classified_item_rows": 0,
        "order_sku_count": 0,
        "amazon_order_count": 0,
        "units": 0,
        "gross_item_sales": 0.0,
        "net_item_sales": 0.0,
    }


def add_row(target: dict[str, float], row: dict[str, Any]) -> None:
    target["classified_item_rows"] += int(row.get("classified_item_rows") or 0)
    target["order_sku_count"] += int(row.get("order_sku_count") or 0)
    target["amazon_order_count"] += int(row.get("amazon_order_count") or 0)
    target["units"] += int(row.get("units") or 0)
    target["gross_item_sales"] += float(row.get("gross_item_sales") or 0)
    target["net_item_sales"] += float(row.get("net_item_sales") or 0)


def aggregate_periods(
    rows: list[dict[str, Any]], periods: list[Period]
) -> tuple[dict[tuple[str, str, str], dict[str, float]], dict[tuple[str, str], set[str]]]:
    metrics: dict[tuple[str, str, str], dict[str, float]] = defaultdict(blank_metric)
    covered_months: dict[tuple[str, str], set[str]] = defaultdict(set)

    for row in rows:
        row_month = month_start(row["order_month"])
        store = clean(row.get("store_name"))
        source_type = clean(row.get("main_order_type"))
        for period in periods:
            if period.start <= row_month < period.end_exclusive:
                add_row(metrics[(period.label, store, source_type)], row)
                covered_months[(period.label, store)].add(row_month.strftime("%Y-%m"))
                break
    return metrics, covered_months


def write_period_sheet(
    workbook,
    sheet_name: str,
    periods: list[Period],
    stores: list[str],
    metrics,
    covered_months,
    formats,
) -> None:
    ws = workbook.add_worksheet(sheet_name)
    ws.hide_gridlines(2)
    ws.freeze_panes(1, 0)

    headers = [
        "统计周期", "周期状态", "店铺", "来源类型", "覆盖月份数",
        "订单-SKU数", "订单占比", "销量", "净商品销售额", "销售额占比",
    ]
    for col, header in enumerate(headers):
        ws.write(0, col, header, formats["header"])

    row_no = 1
    for period in periods:
        for store in stores:
            total_orders = sum(
                metrics[(period.label, store, source)]["order_sku_count"]
                for source in CLASS_ORDER
            )
            total_sales = sum(
                metrics[(period.label, store, source)]["net_item_sales"]
                for source in CLASS_ORDER
            )
            month_count = len(covered_months.get((period.label, store), set()))
            status = "完整周期" if period.is_complete and month_count == period.expected_months else "未完结周期"

            for source in CLASS_ORDER:
                metric = metrics[(period.label, store, source)]
                order_share = metric["order_sku_count"] / total_orders if total_orders else 0
                sales_share = metric["net_item_sales"] / total_sales if total_sales else 0
                values = [
                    period.label,
                    status,
                    store,
                    source,
                    month_count,
                    metric["order_sku_count"],
                    order_share,
                    metric["units"],
                    metric["net_item_sales"],
                    sales_share,
                ]
                cell_formats = [
                    formats["text"], formats["status" if status == "完整周期" else "partial"],
                    formats["text"], formats["text"], formats["int"], formats["int"],
                    formats["pct"], formats["int"], formats["money"], formats["pct"],
                ]
                for col, (value, cell_format) in enumerate(zip(values, cell_formats)):
                    if isinstance(value, (int, float)):
                        ws.write_number(row_no, col, value, cell_format)
                    else:
                        ws.write(row_no, col, value, cell_format)
                row_no += 1

    if row_no > 1:
        table_name = "QuarterSummary" if sheet_name.startswith("季度") else "HalfYearSummary"
        ws.add_table(
            0, 0, row_no - 1, len(headers) - 1,
            {
                "name": table_name,
                "columns": [{"header": header} for header in headers],
                "style": "Table Style Medium 2",
            },
        )

    ws.set_column("A:A", 14)
    ws.set_column("B:B", 14)
    ws.set_column("C:D", 14)
    ws.set_column("E:H", 14)
    ws.set_column("I:I", 18)
    ws.set_column("J:J", 14)


def write_workbook(
    output_path: Path,
    start: date,
    end_exclusive: date,
    stores: list[str],
    include_partial: bool,
    rows: list[dict[str, Any]],
) -> None:
    quarters = build_periods(start, end_exclusive, "quarter", include_partial)
    halves = build_periods(start, end_exclusive, "half", include_partial)
    quarter_metrics, quarter_months = aggregate_periods(rows, quarters)
    half_metrics, half_months = aggregate_periods(rows, halves)

    workbook = xlsxwriter.Workbook(output_path)
    workbook.set_properties(
        {
            "title": "Amazon五类订单来源周期汇总",
            "subject": "月度、季度和半年度订单来源结构",
            "author": "amazon-growth-attribution",
            "company": "尚亿数据",
            "comments": "低价阈值为净成交单价不高于7美元。",
        }
    )

    formats = {
        "header": workbook.add_format({
            "bold": True, "font_color": "#FFFFFF", "bg_color": "#4472C4",
            "border": 1, "align": "center", "valign": "vcenter", "text_wrap": True,
        }),
        "text": workbook.add_format({"border": 1, "valign": "top"}),
        "int": workbook.add_format({"border": 1, "num_format": "#,##0"}),
        "pct": workbook.add_format({"border": 1, "num_format": "0.00%"}),
        "money": workbook.add_format({"border": 1, "num_format": "$#,##0.00"}),
        "status": workbook.add_format({
            "border": 1, "bg_color": "#C6EFCE", "font_color": "#006100"
        }),
        "partial": workbook.add_format({
            "border": 1, "bg_color": "#FFF2CC", "font_color": "#9C6500"
        }),
    }

    write_period_sheet(
        workbook, "季度汇总", quarters, stores,
        quarter_metrics, quarter_months, formats,
    )
    write_period_sheet(
        workbook, "半年度汇总", halves, stores,
        half_metrics, half_months, formats,
    )

    ws = workbook.add_worksheet("口径说明")
    ws.hide_gridlines(2)
    ws.set_column("A:A", 24)
    ws.set_column("B:B", 95)
    title = workbook.add_format({
        "bold": True, "font_size": 18, "font_color": "#FFFFFF",
        "bg_color": "#1F4E78", "align": "left", "valign": "vcenter",
    })
    section = workbook.add_format({"bold": True, "bg_color": "#D9EAF7", "border": 1})
    wrap = workbook.add_format({"border": 1, "text_wrap": True, "valign": "top"})
    ws.merge_range("A1:B1", "Amazon五类订单来源周期汇总口径", title)

    definitions = [
        ("统计范围", f"{start:%Y-%m} 至 {add_month(end_exclusive, -1):%Y-%m}；店铺：{', '.join(stores)}。"),
        ("季度", "Q1=1-3月，Q2=4-6月，Q3=7-9月，Q4=10-12月。"),
        ("半年度", "上半年=1-6月，下半年=7-12月。"),
        ("完整周期", "默认仅输出完整季度和完整半年度；例如截至2026-07时，不包含2026-Q3和2026下半年。"),
        ("未完结周期", "设置 ATTR_INCLUDE_PARTIAL_PERIODS=1 后，可输出当前未结束的季度或半年度，并标记为未完结周期。"),
        ("低价口径", "排除站外推广、广告、站内促销后，净成交单价<=7美元。"),
        ("订单主指标", "订单-SKU数=去重店铺+Amazon订单号+SKU，可在五类之间加总。"),
        ("金额口径", "净商品销售额=item_price-item_promotion_discount，不含运费、税费、退款、平台费、FBA费和广告费。"),
        ("来源优先级", "站外推广 > 广告 > 站内促销 > 低价 > 自然。"),
    ]
    for row_idx, (term, description) in enumerate(definitions, start=1):
        ws.write(row_idx, 0, term, section)
        ws.write(row_idx, 1, description, wrap)
        ws.set_row(row_idx, 34)

    workbook.close()


def main() -> int:
    with connect_db() as conn:
        with conn.cursor() as cur:
            start, end_exclusive, stores, include_partial = resolve_filters(cur)
            rows = fetch_monthly(cur, start, end_exclusive, stores)

    if not rows:
        raise RuntimeError("指定范围内没有五类订单月度汇总数据")

    output_env = clean(os.getenv("ATTR_PERIOD_OUTPUT"))
    if output_env:
        output_path = Path(output_env).expanduser()
    else:
        output_dir = Path(__file__).resolve().parents[1] / "exports"
        output_dir.mkdir(parents=True, exist_ok=True)
        last_month = add_month(end_exclusive, -1)
        output_path = output_dir / (
            f"amazon_order_source_period_summary_"
            f"{start:%Y%m}_{last_month:%Y%m}.xlsx"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_workbook(output_path, start, end_exclusive, stores, include_partial, rows)

    quarters = build_periods(start, end_exclusive, "quarter", include_partial)
    halves = build_periods(start, end_exclusive, "half", include_partial)
    print(f"Excel已生成：{output_path}")
    print("季度：" + ", ".join(period.label for period in quarters))
    print("半年度：" + ", ".join(period.label for period in halves))
    print(f"未完结周期：{'包含' if include_partial else '不包含'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"周期汇总导出失败：{exc}", file=sys.stderr)
        raise
