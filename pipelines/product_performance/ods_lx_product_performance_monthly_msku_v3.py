#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""领星产品表现月度回补：共享接口额度下的慢速稳健版。"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from typing import Any, Dict, Optional

# 导入 v2 会完成 OpenApiBase 与签名逻辑替换。
import ods_lx_product_performance_monthly_msku_v2 as base

job = base.job

# 可通过环境变量覆盖；默认适合多人共享同一路径限流额度。
job.MAX_RETRIES = int(os.getenv("MAX_RETRIES", "30"))
job.REQUEST_INTERVAL = float(os.getenv("REQUEST_INTERVAL_SECONDS", "15"))
LIMIT_WAIT_BASE = float(os.getenv("LIMIT_WAIT_BASE_SECONDS", "20"))
LIMIT_WAIT_CAP = float(os.getenv("LIMIT_WAIT_CAP_SECONDS", "300"))
ERROR_WAIT_BASE = float(os.getenv("ERROR_WAIT_BASE_SECONDS", "10"))
ERROR_WAIT_CAP = float(os.getenv("ERROR_WAIT_CAP_SECONDS", "180"))
JITTER_MIN = float(os.getenv("RETRY_JITTER_MIN_SECONDS", "5"))
JITTER_MAX = float(os.getenv("RETRY_JITTER_MAX_SECONDS", "20"))
TOKEN_REFRESH_AHEAD = int(os.getenv("TOKEN_REFRESH_AHEAD_SECONDS", "300"))


# 记录由主流程首次获取、以及后续刷新得到的 Token 信息。
_original_generate_access_token = job.OpenApiBase.generate_access_token
_original_refresh_token = job.OpenApiBase.refresh_token


def _remember_token(op_api: Any, token_resp: Any) -> Any:
    op_api._active_access_token = str(token_resp.access_token)
    op_api._active_refresh_token = str(getattr(token_resp, "refresh_token", "") or "")
    expires_in = int(getattr(token_resp, "expires_in", 3600) or 3600)
    op_api._active_token_expires_at = time.time() + expires_in
    return token_resp


async def generate_access_token_remembered(op_api: Any) -> Any:
    token_resp = await _original_generate_access_token(op_api)
    return _remember_token(op_api, token_resp)


async def refresh_token_remembered(op_api: Any, refresh_token: str) -> Any:
    token_resp = await _original_refresh_token(op_api, refresh_token)
    return _remember_token(op_api, token_resp)


job.OpenApiBase.generate_access_token = generate_access_token_remembered
job.OpenApiBase.refresh_token = refresh_token_remembered


async def refresh_active_token(op_api: Any, reason: str) -> str:
    """优先使用 refresh_token，失败时重新获取 access token。"""
    refresh_token = str(getattr(op_api, "_active_refresh_token", "") or "")
    if refresh_token:
        try:
            token_resp = await op_api.refresh_token(refresh_token)
            job.log(f"Token 已刷新：{reason}")
            return str(token_resp.access_token)
        except Exception as exc:
            job.log(f"Refresh Token 失败，将重新获取 Access Token：{exc!r}")

    token_resp = await op_api.generate_access_token()
    job.log(f"Access Token 已重新获取：{reason}")
    return str(token_resp.access_token)


async def ensure_active_token(op_api: Any, fallback_token: str) -> str:
    active_token = str(getattr(op_api, "_active_access_token", "") or fallback_token)
    expires_at = float(getattr(op_api, "_active_token_expires_at", 0) or 0)
    if expires_at and time.time() >= expires_at - TOKEN_REFRESH_AHEAD:
        return await refresh_active_token(op_api, "即将过期，提前刷新")
    return active_token


async def robust_api_request(
    op_api: Any,
    token: str,
    session: Any,
    endpoint: str,
    method: str,
    body: Optional[Dict[str, Any]] = None,
) -> Any:
    """对限流、Token过期和临时网络异常进行稳健重试。"""
    last_error: Optional[Exception] = None

    for attempt in range(1, job.MAX_RETRIES + 1):
        try:
            active_token = await ensure_active_token(op_api, token)
            resp = await op_api.request(active_token, endpoint, method, req_body=body)
            code = job.response_code(resp)
            if code == "0":
                return resp

            message = job.response_message(resp)
            error = RuntimeError(f"code={code}, msg={message}")
            last_error = error

            if code == "2001005":
                await refresh_active_token(op_api, "接口返回 access token not match")
                job.log(
                    f"Token失效，第 {attempt}/{job.MAX_RETRIES} 次请求已自动刷新；1s 后重试"
                )
                await asyncio.sleep(1)
                continue

            if attempt >= job.MAX_RETRIES:
                break

            if code == "103":
                wait = min(LIMIT_WAIT_CAP, LIMIT_WAIT_BASE * attempt)
                reason = "接口限流"
            else:
                wait = min(ERROR_WAIT_CAP, ERROR_WAIT_BASE * attempt)
                reason = f"接口错误 code={code}"

        except (json.JSONDecodeError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt >= job.MAX_RETRIES:
                break
            wait = min(ERROR_WAIT_CAP, ERROR_WAIT_BASE * attempt)
            reason = type(exc).__name__

        except Exception as exc:
            last_error = exc
            if attempt >= job.MAX_RETRIES:
                break
            wait = min(ERROR_WAIT_CAP, ERROR_WAIT_BASE * attempt)
            reason = type(exc).__name__

        jitter = random.uniform(JITTER_MIN, JITTER_MAX)
        total_wait = wait + jitter
        job.log(
            f"{reason}，第 {attempt}/{job.MAX_RETRIES} 次失败：{last_error!r}；"
            f"{total_wait:.1f}s 后重试"
        )
        await asyncio.sleep(total_wait)

    raise RuntimeError(f"接口最终失败，已重试 {job.MAX_RETRIES} 次：{last_error!r}")


job.api_request = robust_api_request


if __name__ == "__main__":
    job.log(
        "启动共享额度慢速版："
        f"request_interval={job.REQUEST_INTERVAL}s, max_retries={job.MAX_RETRIES}"
    )
    asyncio.run(job.main())
