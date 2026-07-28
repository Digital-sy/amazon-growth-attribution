#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兼容审计字段及 quantity=0 的 Amazon All Orders TXT 月累计导入入口。

在原导入器基础上自动补充目标表必填审计字段：
- source_file_sha256：源文件 SHA-256
- source_row_no：TXT 原始行号（表头后的首行从 2 开始）
- import_batch_no：本次导入批次号

Amazon All Orders 原始报告中，取消/未完成的订单明细可能出现 quantity=0。
ODS 原始层应保留这些记录，因此本入口允许 quantity=0，但仍拒绝负数。
成交价和有效订单统计在下游继续使用 quantity>0 过滤。

其余预检、整月替换、事务回滚和写后核验逻辑复用
import_amazon_orders_txt_monthly.py。
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


def main() -> int:
    path = resolve_txt_path(sys.argv)
    base = load_base_module()

    file_sha256 = sha256_file(path)
    try:
        store_name, month_start = base.parse_filename(path)
    except Exception:
        store_name, month_start = "UNKNOWN", "0000-00-00"

    batch_no = (
        "txt_"
        + str(store_name).replace("-", "")
        + "_"
        + str(month_start)[:7].replace("-", "")
        + "_"
        + datetime.now().strftime("%Y%m%d%H%M%S")
        + "_"
        + file_sha256[:8]
    )

    original_build_insert_rows = base.build_insert_rows

    def build_insert_rows_with_audit(
        source_rows: list[dict[str, str]],
        table_columns: list[dict[str, Any]],
        *,
        store_name: str,
        month_start: str,
        source_file: str,
    ):
        enriched_rows: list[dict[str, str]] = []
        zero_quantity_indexes: set[int] = set()

        for row_index, source in enumerate(source_rows):
            enriched = dict(source)
            enriched["source_file_sha256"] = file_sha256
            enriched["source_row_no"] = str(enriched.get("__line_no__") or "")
            enriched["import_batch_no"] = batch_no

            # 基础导入器旧逻辑要求 quantity>0。对于 Amazon 原始报告中合法的
            # quantity=0 行，先临时改为1通过结构和类型校验，随后在结果元组中恢复0。
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

        stats["zero_quantity_rows"] = len(zero_quantity_indexes)
        return insert_columns, insert_rows, stats

    base.build_insert_rows = build_insert_rows_with_audit

    print(f"导入审计文件SHA256：{file_sha256}")
    print(f"导入审计批次号：{batch_no}")
    return int(base.main())


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"执行失败：{exc}", file=sys.stderr)
        raise
