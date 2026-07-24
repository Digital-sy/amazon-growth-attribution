#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""月度MSKU回补入口：使用与 lx-product-m 一致的领星签名。"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
import sys

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import ods_lx_product_performance_monthly_msku as job
from lingxing_openapi_compat import OpenApiBase

# 替换原脚本引用的服务器 openapi.py。
job.OpenApiBase = OpenApiBase


async def refresh_token_fixed(
    token_state: job.TokenState,
    session,
    force: bool = False,
) -> None:
    if not force and not token_state.expires_soon():
        return

    api = OpenApiBase(
        job.BASE_URL,
        job.APP_ID,
        job.APP_SECRET,
        session=session,
    )

    if token_state.refresh_token:
        token_resp = await api.refresh_token(token_state.refresh_token)
    else:
        token_resp = await api.generate_access_token()

    token_state.access_token = str(token_resp.access_token)
    token_state.refresh_token = str(
        token_resp.refresh_token or token_state.refresh_token
    )
    token_state.expires_in = int(token_resp.expires_in or token_state.expires_in)
    token_state.acquired_at = time.time()
    token_state.refresh_count += 1
    job.log(f"Token 刷新成功：累计刷新 {token_state.refresh_count} 次")


job.refresh_token = refresh_token_fixed


if __name__ == "__main__":
    asyncio.run(job.main())
