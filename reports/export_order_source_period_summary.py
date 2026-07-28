#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兼容入口：生成包含月度、季度、半年度及原有审计 Sheet 的完整工作簿。

推荐直接运行 reports/export_order_source_summary.py。
保留本文件是为了兼容此前已经使用的周期汇总命令。

环境变量兼容：
- ATTR_PERIOD_OUTPUT：旧周期脚本的输出路径；自动转换为 ATTR_EXPORT_OUTPUT。
- 其他参数与 export_order_source_summary.py 一致。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import export_order_source_summary as full_export  # noqa: E402


def main() -> int:
    period_output = os.getenv("ATTR_PERIOD_OUTPUT", "").strip()
    if period_output and not os.getenv("ATTR_EXPORT_OUTPUT", "").strip():
        os.environ["ATTR_EXPORT_OUTPUT"] = period_output

    print("说明：周期汇总入口现已生成完整工作簿，包含原月度及审计Sheet。")
    return int(full_export.main())


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"完整来源汇总导出失败：{exc}", file=sys.stderr)
        raise
