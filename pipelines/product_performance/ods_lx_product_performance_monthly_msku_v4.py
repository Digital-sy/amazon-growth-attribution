#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""领星产品表现月度回补：共享额度稳健版 + MSKU 主键兼容修复。"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

# v3 已包含：正确签名、共享额度慢速重试、随机退避。
import ods_lx_product_performance_monthly_msku_v3 as base

job = base.job
_original_flatten = job.flatten
_original_ensure_tables = job.ensure_tables


def flatten_fixed(
    row: Dict[str, Any],
    report_month,
    period_start,
    period_end,
    sid: int,
    store_name: str,
) -> Optional[Dict[str, Any]]:
    """清除 MSKU 首尾空白，避免 MySQL PAD SPACE 主键冲突。"""
    item = _original_flatten(
        row,
        report_month,
        period_start,
        period_end,
        sid,
        store_name,
    )
    if not item:
        return None

    msku = str(item.get("msku") or "").strip()
    if not msku:
        return None
    item["msku"] = msku
    return item


def ensure_tables_fixed(conn) -> None:
    """创建表后，将 MSKU 改为大小写敏感二进制排序规则。"""
    _original_ensure_tables(conn)
    alter_sql = f"""
    ALTER TABLE `{job.TABLE_NAME}`
      MODIFY COLUMN `msku`
      VARCHAR(255)
      CHARACTER SET utf8mb4
      COLLATE utf8mb4_bin
      NOT NULL
    """
    with conn.cursor() as cur:
        cur.execute(alter_sql)
    conn.commit()
    job.log("MSKU 字段已确认使用 utf8mb4_bin 大小写敏感排序规则")


job.flatten = flatten_fixed
job.ensure_tables = ensure_tables_fixed


if __name__ == "__main__":
    job.log(
        "启动共享额度稳健版 v4：已启用 MSKU 去空白和 utf8mb4_bin 主键兼容修复"
    )
    asyncio.run(job.main())
