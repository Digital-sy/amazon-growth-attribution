#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""与 lx-product-m 完全一致并兼容布尔参数的领星 OpenAPI 客户端。"""
from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import orjson
from Crypto.Cipher import AES

BLOCK_SIZE = 16
TOKEN_PARAM = "access_token"


def _pad(text: str) -> str:
    pad_len = BLOCK_SIZE - len(text) % BLOCK_SIZE
    return text + pad_len * chr(pad_len)


def _format_params(request_params: Optional[dict[str, Any]]) -> str:
    if not request_params or not isinstance(request_params, dict):
        return ""
    pairs: list[str] = []
    for key in sorted(request_params.keys()):
        value = request_params[key]
        if value == "" or value is None:
            continue
        if isinstance(value, bool):
            # 请求体由 orjson 序列化为 true/false；签名必须使用同样的小写形式。
            value_text = orjson.dumps(value).decode()
        elif isinstance(value, (dict, list)):
            value_text = orjson.dumps(value, option=orjson.OPT_SORT_KEYS).decode()
        else:
            value_text = str(value)
        pairs.append(f"{key}={value_text}")
    return "&".join(pairs)


def generate_sign(encrypt_key: str, request_params: dict[str, Any]) -> str:
    canonical = _format_params(request_params)
    md5_upper = hashlib.md5(canonical.encode("utf-8")).hexdigest().upper()
    cipher = AES.new(encrypt_key.encode("utf-8"), AES.MODE_ECB)
    encrypted = cipher.encrypt(_pad(md5_upper).encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


@dataclass
class AccessTokenDto:
    access_token: str
    refresh_token: str = ""
    expires_in: int = 3600


class OpenApiBase:
    """接口签名、JSON 序列化和 HTTP 层均对齐 lx-product-m。"""

    def __init__(
        self,
        host: str,
        app_id: str,
        app_secret: str,
        proxy_url: Optional[str] = None,
        session: Any = None,
        **_: Any,
    ) -> None:
        self.host = host.rstrip("/")
        self.app_id = app_id
        self.app_secret = app_secret
        self.proxy_url = proxy_url or None
        self.timeout = httpx.Timeout(180.0, connect=15.0)
        self._http: Optional[httpx.AsyncClient] = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            kwargs: dict[str, Any] = {"timeout": self.timeout}
            if self.proxy_url:
                kwargs["proxy"] = self.proxy_url
            self._http = httpx.AsyncClient(**kwargs)
        return self._http

    async def close(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    async def generate_access_token(self) -> AccessTokenDto:
        client = await self._client()
        response = await client.post(
            self.host + "/api/auth-server/oauth/access-token",
            data={"appSecret": self.app_secret, "appId": self.app_id},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        payload = response.json()
        if str(payload.get("code")) != "200":
            raise RuntimeError(f"获取领星Token失败：{payload}")
        data = payload.get("data") or {}
        token = data.get(TOKEN_PARAM) or data.get("accessToken")
        if not token:
            raise RuntimeError(f"领星Token返回结构异常：{payload}")
        return AccessTokenDto(
            access_token=str(token),
            refresh_token=str(data.get("refresh_token") or data.get("refreshToken") or ""),
            expires_in=int(data.get("expires_in") or data.get("expiresIn") or 3600),
        )

    async def refresh_token(self, refresh_token: str) -> AccessTokenDto:
        client = await self._client()
        response = await client.post(
            self.host + "/api/auth-server/oauth/refresh",
            data={"appId": self.app_id, "refreshToken": refresh_token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        payload = response.json()
        if str(payload.get("code")) != "200":
            raise RuntimeError(f"刷新领星Token失败：{payload}")
        data = payload.get("data") or {}
        token = data.get(TOKEN_PARAM) or data.get("accessToken")
        if not token:
            raise RuntimeError(f"刷新Token返回结构异常：{payload}")
        return AccessTokenDto(
            access_token=str(token),
            refresh_token=str(data.get("refresh_token") or data.get("refreshToken") or refresh_token),
            expires_in=int(data.get("expires_in") or data.get("expiresIn") or 3600),
        )

    async def request(
        self,
        access_token: str,
        route_name: str,
        method: str,
        req_params: Optional[dict[str, Any]] = None,
        req_body: Optional[dict[str, Any]] = None,
        **_: Any,
    ) -> dict[str, Any]:
        method = method.upper()
        body = dict(req_body or {})
        query_params = dict(req_params or {})

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

        request_kwargs: dict[str, Any] = {"params": query_params}
        if body:
            request_kwargs["content"] = orjson.dumps(body, option=orjson.OPT_SORT_KEYS)
            request_kwargs["headers"] = {"Content-Type": "application/json"}

        client = await self._client()
        response = await client.request(
            method,
            self.host + route_name,
            **request_kwargs,
        )
        return response.json()
