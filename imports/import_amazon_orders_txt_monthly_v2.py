#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兼容审计字段的 Amazon All Orders TXT 月累计导入入口。

在原导入器基础上自动补充目标表必填审计字段：
- source_file_sha256：源文件 SHA-256
- source_row_no：TXT 原始行号（表头后的首行从 2 开始）
- import_batch_no：本次导入批次号

其余预检、整月替换、事务回滚和写后核验逻辑复用
import_amazon_orders_txt_monthly.py。
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from datetime import datetime
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
        for source in source_rows:
            enriched = dict(source)
            enriched["source_file_sha256"] = file_sha256
            enriched["source_row_no"] = str(enriched.get("__line_no__") or "")
            enriched["import_batch_no"] = batch_no
            enriched_rows.append(enriched)

        return original_build_insert_rows(
            enriched_rows,
            table_columns,
            store_name=store_name,
            month_start=month_start,
            source_file=source_file,
        )

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
