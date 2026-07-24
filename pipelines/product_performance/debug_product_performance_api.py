#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐项探测领星产品表现接口，定位签名/权限/参数问题。"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from lingxing_openapi_compat import OpenApiBase

HOST = os.getenv("LINGXING_BASE_URL", "https://openapi.lingxing.com").rstrip("/")
APP_ID = os.getenv("LINGXING_APP_ID", "").strip()
APP_SECRET = os.getenv("LINGXING_APP_SECRET", "").strip()
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None
ENDPOINT = "/bd/productPerformance/openApi/asinList"


def summarize(result: dict) -> str:
    code = result.get("code")
    msg = result.get("msg") or result.get("message")
    data = result.get("data") or {}
    total = data.get("total") if isinstance(data, dict) else None
    rows = data.get("list") if isinstance(data, dict) else None
    count = len(rows) if isinstance(rows, list) else None
    return f"code={code}, msg={msg!r}, total={total}, rows={count}"


async def main() -> None:
    if not APP_ID or not APP_SECRET:
        raise RuntimeError("缺少 LINGXING_APP_ID / LINGXING_APP_SECRET")

    api = OpenApiBase(HOST, APP_ID, APP_SECRET, proxy_url=PROXY_URL)
    try:
        token = await api.generate_access_token()
        print(f"Token OK: len={len(token.access_token)}, expires={token.expires_in}")

        stores = await api.request(
            token.access_token,
            "/erp/sc/data/seller/lists",
            "GET",
            req_body={},
        )
        print("store-list:", summarize(stores))

        base = {
            "offset": 0,
            "length": 1,
            "sort_field": "volume",
            "sort_type": "desc",
            "start_date": "2025-01-01",
            "end_date": "2025-01-31",
            "summary_field": "msku",
        }

        variants = [
            ("A_sid_string_minimal", {**base, "sid": "11548"}),
            ("B_sid_int_minimal", {**base, "sid": 11548}),
            ("C_sid_list_minimal", {**base, "sid": [11548]}),
            (
                "D_full_current_body",
                {
                    **base,
                    "sid": "11548",
                    "currency_code": "USD",
                    "is_recently_enum": False,
                    "purchase_status": 0,
                },
            ),
            (
                "E_full_without_bool",
                {
                    **base,
                    "sid": "11548",
                    "currency_code": "USD",
                    "purchase_status": 0,
                },
            ),
        ]

        for name, body in variants:
            await asyncio.sleep(1.2)
            try:
                result = await api.request(
                    token.access_token,
                    ENDPOINT,
                    "POST",
                    req_body=body,
                )
                print(f"{name}: {summarize(result)}")
            except Exception as exc:
                print(f"{name}: EXCEPTION {exc!r}")
    finally:
        await api.close()


if __name__ == "__main__":
    asyncio.run(main())
