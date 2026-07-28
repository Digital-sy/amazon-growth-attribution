#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兼容审计字段、quantity=0 及缺失 order_item_id 的月累计导入入口。

在原导入器基础上自动补充：
- source_file_sha256：源文件 SHA-256
- source_row_no：TXT 原始行号
- import_batch_no：本次导入批次号
- order_item_id：当 TXT 不提供时生成稳定的合成明细 ID

Amazon All Orders 原始报告中，取消/未完成订单可能出现 quantity=0。
ODS 原始层应保留这些记录，因此允许 quantity=0，但仍拒绝负数。
成交价及有效订单统计在下游继续使用 quantity>0 过滤。
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_SCRIPT = SCRIPT_DIR / "import_amazon_orders_txt_monthly.py"


def load_base_module():
    spec = importlib.util.spec_from_file_location("amazon_orders_txt_import_base", BASE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载基础导入器：{BASE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def resolve_txt_path(argv: list[str]) -> Path:
    if len(argv) < 2 or argv[1].startswith("-"):
        raise RuntimeError("缺少 TXT 文件路径")
    path = Path(argv[1]).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"TXT文件不存在：{path}")
    return path


def is_zero_quantity(base: Any, source: dict[str, str]) -> bool:
    raw = base.source_value(source, "quantity")
    text = base.clean(raw)
    if text == "":
        return False
    try:
        return Decimal(text.replace(",", "")) == 0
    except InvalidOperation:
        return False


def synthetic_order_item_id(base: Any, source: dict[str, str], store_name: str, month_start: str) -> str:
    """生成当前月累计文件内稳定且足够唯一的明细 ID。"""
    line_no = base.clean(source.get("__line_no__"))
    parts = [
        store_name,
        month_start,
        base.clean(base.source_value(source, "amazon_order_id")),
        base.clean(base.source_value(source, "sku")),
        base.clean(base.source_value(source, "asin")),
        base.clean(base.source_value(source, "purchase_date_utc")),
        base.clean(base.source_value(source, "quantity")),
        base.clean(base.source_value(source, "item_price")),
        line_no,
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"txt-{line_no}-{digest}"


def validate_unique_order_items(insert_columns: list[str], insert_rows: list[tuple[Any, ...]]) -> None:
    if "amazon_order_id" not in insert_columns or "order_item_id" not in insert_columns:
        return
    order_idx = insert_columns.index("amazon_order_id")
    item_idx = insert_columns.index("order_item_id")
    seen: set[tuple[str, str]] = set()
    duplicates: list[tuple[str, str]] = []
    for row in insert_rows:
        key = (str(row[order_idx] or ""), str(row[item_idx] or ""))
        if key in seen:
            duplicates.append(key)
            if len(duplicates) >= 5:
                break
        seen.add(key)
    if duplicates:
        raise RuntimeError(f"导入数据仍存在重复 amazon_order_id+order_item_id：{duplicates}")


def main() -> int:
    path = resolve_txt_path(sys.argv)
    base = load_base_module()

    file_sha256 = sha256_file(path)
    try:
        parsed_store, parsed_month = base.parse_filename(path)
    except Exception:
        parsed_store, parsed_month = "UNKNOWN", "0000-00-00"

    batch_no = (
        "txt_"
        + str(parsed_store).replace("-", "")
        + "_"
        + str(parsed_month)[:7].replace("-", "")
        + "_"
        + datetime.now().strftime("%Y%m%d%H%M%S")
        + "_"
        + file_sha256[:8]
    )

    original_build_insert_rows = base.build_insert_rows

    def build_insert_rows_with_compat(
        source_rows: list[dict[str, str]],
        table_columns: list[dict[str, Any]],
        *,
        store_name: str,
        month_start: str,
        source_file: str,
    ):
        enriched_rows: list[dict[str, str]] = []
        zero_quantity_indexes: set[int] = set()
        synthetic_id_count = 0

        for row_index, source in enumerate(source_rows):
            enriched = dict(source)
            enriched["source_file_sha256"] = file_sha256
            enriched["source_row_no"] = str(enriched.get("__line_no__") or "")
            enriched["import_batch_no"] = batch_no

            if not base.clean(base.source_value(enriched, "order_item_id")):
                enriched["order_item_id"] = synthetic_order_item_id(
                    base, enriched, store_name, month_start
                )
                synthetic_id_count += 1

            # 基础导入器旧逻辑要求 quantity>0。合法的 quantity=0 行先临时改为1，
            # 通过类型校验后再恢复为0。
            if is_zero_quantity(base, enriched):
                zero_quantity_indexes.add(row_index)
                enriched["quantity"] = "1"

            enriched_rows.append(enriched)

        insert_columns, insert_rows, stats = original_build_insert_rows(
            enriched_rows,
            table_columns,
            store_name=store_name,
            month_start=month_start,
            source_file=source_file,
        )

        if zero_quantity_indexes:
            if "quantity" not in insert_columns:
                raise RuntimeError("目标写入字段缺少 quantity，无法恢复零数量记录")
            quantity_index = insert_columns.index("quantity")
            restored_rows: list[tuple[Any, ...]] = []
            for row_index, row in enumerate(insert_rows):
                if row_index in zero_quantity_indexes:
                    values = list(row)
                    values[quantity_index] = 0
                    restored_rows.append(tuple(values))
                else:
                    restored_rows.append(row)
            insert_rows = restored_rows

        validate_unique_order_items(insert_columns, insert_rows)
        stats["zero_quantity_rows"] = len(zero_quantity_indexes)
        stats["synthetic_order_item_id_rows"] = synthetic_id_count
        print(f"quantity=0 保留行数：{len(zero_quantity_indexes):,}")
        print(f"自动生成 order_item_id 行数：{synthetic_id_count:,}")
        return insert_columns, insert_rows, stats

    base.build_insert_rows = build_insert_rows_with_compat

    print(f"导入审计文件SHA256：{file_sha256}")
    print(f"导入审计批次号：{batch_no}")
    return int(base.main())


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"执行失败：{exc}", file=sys.stderr)
        raise
