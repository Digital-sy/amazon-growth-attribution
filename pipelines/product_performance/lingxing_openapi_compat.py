#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""与 lx-product-m / lx-forecast-pipeline 一致的领星 OpenAPI 兼容客户端。"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp
from Crypto.Cipher import AES

BLOCK_SIZE = 16
TOKEN_PARAM = "access_token"


def _json_dumps_sorted(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _format_params(request_params: Optional[dict[str, Any]]) -> str:
    if not request_params:
        return ""
    pairs: list[str] = []
    for key in sorted(request_params.keys()):
        value = request_params[key]
        if value in (None, ""):
            continue
        if isinstance(value, (dict, list)):
            value_text = _json_dumps_sorted(value)
        else:
            value_text = str(value)
        pairs.append(f"{key}={value_text}")
    return "&".join(pairs)


def _pad(text: str) -> bytes:
    raw = text.encode("utf-8")
    pad_len = BLOCK_SIZE - len(raw) % BLOCK_SIZE
    return raw + bytes([pad_len]) * pad_len


def generate_sign(encrypt_key: str, request_params: dict[str, Any]) -> str:
    canonical = _format_params(request_params)
    md5_upper = hashlib.md5(canonical.encode("utf-8")).hexdigest().upper()
    key = encrypt_key.encode("utf-8")
    if len(key) not in (16, 24, 32):
        raise RuntimeError(
            f"app_id 长度必须为16/24/32字节，当前为{len(key)}"
        )
    encrypted = AES.new(key, AES.MODE_ECB).encrypt(_pad(md5_upper))
    return base64.b64encode(encrypted).decode("utf-8")


@dataclass
class AccessTokenDto:
    access_token: str
    refresh_token: str = ""
    expires_in: int = 3600


class OpenApiBase:
    """保持原脚本调用接口不变，但使用仓库内已验证的签名算法。"""

    def __init__(
        self,
        host: str,
        app_id: str,
        app_secret: str,
        proxy_url: Optional[str] = None,
        session: Optional[aiohttp.ClientSession] = None,
        **_: Any,
    ) -> None:
        self.host = host.rstrip("/")
        self.app_id = app_id
        self.app_secret = app_secret
        self.proxy_url = proxy_url
        self.session = session
        self._owned_session: Optional[aiohttp.ClientSession] = None

    async def _get_session(
        self,
        supplied: Optional[aiohttp.ClientSession] = None,
    ) -> aiohttp.ClientSession:
        if supplied is not None:
            return supplied
        if self.session is not None:
            return self.session
        if self._owned_session is None or self._owned_session.closed:
            timeout = aiohttp.ClientTimeout(total=180)
            self._owned_session = aiohttp.ClientSession(
                timeout=timeout,
                trust_env=True,
            )
        return self._owned_session

    async def close(self) -> None:
        if self._owned_session is not None and not self._owned_session.closed:
            await self._owned_session.close()

    async def generate_access_token(self) -> AccessTokenDto:
        session = await self._get_session()
        url = self.host + "/api/auth-server/oauth/access-token"
        async with session.post(
            url,
            data={"appId": self.app_id, "appSecret": self.app_secret},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            ssl=False,
        ) as response:
            payload = await response.json(content_type=None)

        if str(payload.get("code")) != "200":
            raise RuntimeError(f"获取领星Token失败：{payload}")
        data = payload.get("data") or {}
        token = data.get("access_token") or data.get("accessToken")
        if not token:
            raise RuntimeError(f"领星Token返回结构异常：{payload}")
        return AccessTokenDto(
            access_token=str(token),
            refresh_token=str(
                data.get("refresh_token") or data.get("refreshToken") or ""
            ),
            expires_in=int(data.get("expires_in") or data.get("expiresIn") or 3600),
        )

    async def refresh_token(self, refresh_token: str) -> AccessTokenDto:
        session = await self._get_session()
        url = self.host + "/api/auth-server/oauth/refresh"
        async with session.post(
            url,
            data={"appId": self.app_id, "refreshToken": refresh_token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            ssl=False,
        ) as response:
            payload = await response.json(content_type=None)

        if str(payload.get("code")) != "200":
            raise RuntimeError(f"刷新领星Token失败：{payload}")
        data = payload.get("data") or {}
        token = data.get("access_token") or data.get("accessToken")
        if not token:
            raise RuntimeError(f"刷新Token返回结构异常：{payload}")
        return AccessTokenDto(
            access_token=str(token),
            refresh_token=str(
                data.get("refresh_token") or data.get("refreshToken") or refresh_token
            ),
            expires_in=int(data.get("expires_in") or data.get("expiresIn") or 3600),
        )

    async def request(
        self,
        access_token: str,
        route_name: str,
        method: str,
        req_params: Optional[dict[str, Any]] = None,
        req_body: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        supplied_session = kwargs.pop("session", None)
        session = await self._get_session(supplied_session)
        query_params = dict(req_params or {})
        body = dict(req_body or {})

        sign_source = dict(body)
        sign_source.update(query_params)
        sign_params = {
            "app_key": self.app_id,
            TOKEN_PARAM: access_token,
            "timestamp": str(int(time.time())),
        }
        sign_source.update(sign_params)
        sign_params["sign"] = generate_sign(self.app_id, sign_source)
        query_params.update(sign_params)

        headers = dict(kwargs.pop("headers", {}) or {})
        request_kwargs: dict[str, Any] = {
            "params": query_params,
            "ssl": False,
            **kwargs,
        }
        if body:
            headers.setdefault("Content-Type", "application/json")
            request_kwargs["data"] = _json_dumps_sorted(body).encode("utf-8")
        if headers:
            request_kwargs["headers"] = headers

        async with session.request(
            method.upper(),
            self.host + route_name,
            **request_kwargs,
        ) as response:
            return await response.json(content_type=None)
