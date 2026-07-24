#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""领星产品表现月度回补：共享接口额度下的慢速稳健版。"""
from __future__ import annotations

import asyncio
import json
import os
import random
from typing import Any, Dict, Optional

# 导入 v2 会完成 OpenApiBase 与 Token 刷新逻辑替换。
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


async def robust_api_request(
    op_api: Any,
    token: str,
    session: Any,
    endpoint: str,
    method: str,
    body: Optional[Dict[str, Any]] = None,
) -> Any:
    """对限流和临时网络异常进行长退避重试。"""
    last_error: Optional[Exception] = None

    for attempt in range(1, job.MAX_RETRIES + 1):
        try:
            # 新兼容客户端使用 httpx；保留 session 参数仅为了兼容原函数签名。
            resp = await op_api.request(token, endpoint, method, req_body=body)
            code = job.response_code(resp)
            if code == "0":
                return resp

            message = job.response_message(resp)
            error = RuntimeError(f"code={code}, msg={message}")
            last_error = error

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
