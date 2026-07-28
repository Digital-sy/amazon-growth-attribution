#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按店铺拆分导出“款号-每月成交价”Excel。

输出工作簿仅包含店铺 Sheet，每个店铺一张款号月成交价宽表。
数据读取、成交价口径和映射逻辑全部复用 export_style_monthly_deal_price.py。
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_SCRIPT = SCRIPT_DIR / "export_style_monthly_deal_price.py"


def load_base_module():
    spec = importlib.util.spec_from_file_location("style_monthly_deal_price_base", BASE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载基础导出脚本：{BASE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    # dataclass 在解析字段类型时会通过 cls.__module__ 查询 sys.modules。
    # 动态 exec_module 前必须先注册，否则 Python 3.10 会出现 NoneType.__dict__。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def safe_sheet_name(value: str, used: set[str]) -> str:
    name = re.sub(r"[\\/*?:\[\]]", "_", str(value or "店铺")).strip()[:31] or "店铺"
    candidate = name
    index = 2
    while candidate in used:
        suffix = f"_{index}"
        candidate = f"{name[:31-len(suffix)]}{suffix}"
        index += 1
    used.add(candidate)
    return candidate


def safe_table_name(value: str, used: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "_", value)
    if not base or base[0].isdigit():
        base = f"T_{base}"
    candidate = f"StyleDealPrice_{base}"
    index = 2
    while candidate in used:
        candidate = f"StyleDealPrice_{base}_{index}"
        index += 1
    used.add(candidate)
    return candidate


def add_metric(total: Any, metric: Any) -> None:
    total.order_sku_count += metric.order_sku_count
    total.units += metric.units
    total.gross += metric.gross
    total.discount += metric.discount
    total.net += metric.net
    if metric.min_price is not None:
        total.min_price = metric.min_price if total.min_price is None else min(total.min_price, metric.min_price)
    if metric.max_price is not None:
        total.max_price = metric.max_price if total.max_price is None else max(total.max_price, metric.max_price)
    total.mskus.update(metric.mskus)
    total.local_skus.update(metric.local_skus)


def write_workbook_by_store(
    output_path,
    start_month,
    end_month_exclusive,
    stores,
    months,
    long_metrics,
    audit_metrics,
    coverage,
):
    base = load_base_module()
    workbook = base.xlsxwriter.Workbook(output_path)
    workbook.set_properties({
        "title": "款号每月成交价（按店铺拆分）",
        "subject": "每个店铺一张款号月成交价宽表",
        "author": "amazon-growth-attribution",
        "company": "尚亿数据",
        "comments": "成交价=SUM(item_price-item_promotion_discount)/SUM(quantity)",
    })

    fmt_title = workbook.add_format({
        "bold": True, "font_size": 16, "font_color": "#FFFFFF",
        "bg_color": "#1F4E78", "align": "left", "valign": "vcenter",
    })
    fmt_subtitle = workbook.add_format({
        "font_color": "#404040", "bg_color": "#D9EAF7",
        "align": "left", "valign": "vcenter",
    })
    fmt_header = workbook.add_format({
        "bold": True, "font_color": "#FFFFFF", "bg_color": "#4472C4",
        "border": 1, "align": "center", "valign": "vcenter", "text_wrap": True,
    })
    fmt_text = workbook.add_format({"border": 1, "valign": "top"})
    fmt_int = workbook.add_format({"border": 1, "num_format": "#,##0"})
    fmt_money = workbook.add_format({"border": 1, "num_format": "$#,##0.00"})
    fmt_missing = workbook.add_format({
        "border": 1, "bg_color": "#FFC7CE", "font_color": "#9C0006"
    })

    wide = defaultdict(dict)
    for key, metric in long_metrics.items():
        month, store, style_no, season, category, principal = key
        wide[(store, principal, style_no, season, category)][month] = metric

    used_sheets: set[str] = set()
    used_tables: set[str] = set()
    headers = [
        "负责人（运营）", "款号", "季节", "品类", "全期间销量",
        *months,
        "全期间加权成交价", "MSKU数", "本地SKU数",
    ]

    for store in stores:
        sheet_name = safe_sheet_name(store, used_sheets)
        ws = workbook.add_worksheet(sheet_name)
        ws.hide_gridlines(2)
        ws.freeze_panes(3, 4)
        ws.merge_range(0, 0, 0, len(headers) - 1, f"{store}｜款号每月成交价", fmt_title)
        ws.merge_range(
            1, 0, 1, len(headers) - 1,
            f"月份：{months[0]} 至 {months[-1]}；成交价为销量加权平均成交价；2026-07为未完结月份。",
            fmt_subtitle,
        )
        for col, header in enumerate(headers):
            ws.write(2, col, header, fmt_header)

        store_keys = [dims for dims in wide if base.norm(dims[0]) == base.norm(store)]
        store_keys.sort(key=lambda x: (x[2], x[1], x[3], x[4]))

        row_no = 3
        for dims in store_keys:
            _, principal, style_no, season, category = dims
            month_map = wide[dims]
            total = base.Metric()
            for month in months:
                metric = month_map.get(month)
                if metric:
                    add_metric(total, metric)

            dimension_values = [principal, style_no, season, category]
            for col, value in enumerate(dimension_values):
                cell_format = fmt_missing if base.clean(value).startswith("（未") else fmt_text
                ws.write(row_no, col, value, cell_format)

            ws.write_number(row_no, 4, total.units, fmt_int)
            for idx, month in enumerate(months):
                metric = month_map.get(month)
                if metric and metric.weighted_price is not None:
                    ws.write_number(row_no, 5 + idx, metric.weighted_price, fmt_money)
                else:
                    ws.write_blank(row_no, 5 + idx, None, fmt_text)

            result_col = 5 + len(months)
            if total.weighted_price is not None:
                ws.write_number(row_no, result_col, total.weighted_price, fmt_money)
            else:
                ws.write_blank(row_no, result_col, None, fmt_text)
            ws.write_number(row_no, result_col + 1, len(total.mskus), fmt_int)
            ws.write_number(row_no, result_col + 2, len(total.local_skus), fmt_int)
            row_no += 1

        if row_no > 3:
            ws.add_table(
                2, 0, row_no - 1, len(headers) - 1,
                {
                    "name": safe_table_name(store, used_tables),
                    "columns": [{"header": header} for header in headers],
                    "style": "Table Style Medium 2",
                },
            )

        ws.set_row(0, 28)
        ws.set_row(1, 22)
        ws.set_row(2, 32)
        ws.set_column(0, 0, 16)
        ws.set_column(1, 1, 16)
        ws.set_column(2, 3, 14)
        ws.set_column(4, 4, 14)
        ws.set_column(5, 5 + len(months) - 1, 12)
        ws.set_column(5 + len(months), 7 + len(months), 18)

    workbook.close()


def main() -> int:
    base = load_base_module()
    base.write_workbook = write_workbook_by_store

    if not os.getenv("DEAL_PRICE_OUTPUT"):
        start_month = base.clean(os.getenv("DEAL_PRICE_START_MONTH")) or base.DEFAULT_START_MONTH
        end_month = base.clean(os.getenv("DEAL_PRICE_END_MONTH_EXCLUSIVE")) or base.DEFAULT_END_MONTH_EXCLUSIVE
        months = base.month_range(start_month, end_month)
        output_dir = Path(__file__).resolve().parents[1] / "exports"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / (
            f"style_monthly_deal_price_by_store_"
            f"{start_month[:7].replace('-', '')}_{months[-1].replace('-', '')}.xlsx"
        )
        os.environ["DEAL_PRICE_OUTPUT"] = str(output_path)

    return int(base.main())


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"按店铺拆分导出失败：{exc}", file=sys.stderr)
        raise
