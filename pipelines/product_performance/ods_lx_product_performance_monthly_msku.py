#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""领星产品表现：按自然月、单店铺、MSKU 回补到 lingxing 库。"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

import aiohttp
import pymysql

try:
    from openapi import OpenApiBase
except ModuleNotFoundError:
    sys.path.insert(0, os.getenv("BI_CODE_ROOT", "/data/bi_scripts/BI"))
    from openapi import OpenApiBase

BASE_URL = os.getenv("LINGXING_BASE_URL", "https://openapi.lingxing.com")
APP_ID = os.getenv("LINGXING_APP_ID", "").strip()
APP_SECRET = os.getenv("LINGXING_APP_SECRET", "").strip()
PROXY_URL = os.getenv("PROXY_URL", "").strip()

STORE_LIST_ENDPOINT = "/erp/sc/data/seller/lists"
PERFORMANCE_ENDPOINT = "/bd/productPerformance/openApi/asinList"

DB_NAME = os.getenv("LINGXING_DB_NAME", "lingxing")
TABLE_NAME = os.getenv("TARGET_TABLE", "ods_lx_product_performance_monthly_msku")
LOG_TABLE = os.getenv("LOAD_LOG_TABLE", "etl_lx_product_performance_monthly_log")
DB_CONFIG = {
    "host": os.getenv("LINGXING_DB_HOST", os.getenv("DB_HOST", "rm-wz91237y91oasq45fco.mysql.rds.aliyuncs.com")),
    "port": int(os.getenv("LINGXING_DB_PORT", os.getenv("DB_PORT", "3306"))),
    "user": os.getenv("LINGXING_DB_USER", os.getenv("DB_USER", "SYSJ001")),
    "password": os.getenv("LINGXING_DB_PASSWORD", os.getenv("DB_PASSWORD", "")),
    "database": DB_NAME,
    "charset": "utf8mb4",
    "autocommit": False,
    "connect_timeout": 15,
    "read_timeout": 300,
    "write_timeout": 300,
}

REQUEST_INTERVAL = float(os.getenv("REQUEST_INTERVAL_SECONDS", "1.2"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "4"))
PAGE_SIZE = 10000


def log(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def require_env() -> None:
    missing = []
    if not APP_ID:
        missing.append("LINGXING_APP_ID")
    if not APP_SECRET:
        missing.append("LINGXING_APP_SECRET")
    if not DB_CONFIG["password"]:
        missing.append("LINGXING_DB_PASSWORD/DB_PASSWORD")
    if missing:
        raise RuntimeError("缺少环境变量：" + ", ".join(missing))


def month_start(value: str) -> date:
    return datetime.strptime(value, "%Y-%m").date().replace(day=1)


def month_end(value: date) -> date:
    return value.replace(day=calendar.monthrange(value.year, value.month)[1])


def iter_months(start: str, end: str) -> Iterable[Tuple[date, date]]:
    current = month_start(start)
    stop = month_start(end)
    if current > stop:
        raise ValueError("start-month 不能晚于 end-month")
    while current <= stop:
        yield current, min(month_end(current), date.today())
        current = date(current.year + (current.month == 12), current.month % 12 + 1, 1)


def response_code(resp: Any) -> str:
    value = resp.get("code") if isinstance(resp, dict) else getattr(resp, "code", None)
    return "0" if value is None else str(value)


def response_message(resp: Any) -> str:
    if isinstance(resp, dict):
        return str(resp.get("message") or resp.get("msg") or "")
    return str(getattr(resp, "message", None) or getattr(resp, "msg", None) or "")


def response_data(resp: Any) -> Any:
    return resp.get("data") if isinstance(resp, dict) else getattr(resp, "data", None)


def rows_total(resp: Any) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    data = response_data(resp)
    total = None
    rows: Any = []
    if isinstance(data, dict):
        rows = data.get("list") or data.get("rows") or []
        total = data.get("total")
    elif isinstance(data, list):
        rows = data
    return [x for x in rows if isinstance(x, dict)], int(total) if total is not None else None


def token_value(resp: Any, *names: str) -> Any:
    for name in names:
        value = resp.get(name) if isinstance(resp, dict) else getattr(resp, name, None)
        if value not in (None, ""):
            return value
    data = response_data(resp)
    if isinstance(data, dict):
        for name in names:
            if data.get(name) not in (None, ""):
                return data[name]
    return None


async def api_request(op_api: OpenApiBase, token: str, session: aiohttp.ClientSession,
                      endpoint: str, method: str, body: Optional[Dict[str, Any]] = None) -> Any:
    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = await op_api.request(token, endpoint, method, req_body=body, session=session)
            code = response_code(resp)
            if code == "0":
                return resp
            raise RuntimeError(f"code={code}, msg={response_message(resp)}")
        except Exception as exc:
            last_error = exc
            if attempt == MAX_RETRIES:
                break
            wait = 3 * attempt
            log(f"接口失败，第 {attempt}/{MAX_RETRIES} 次：{exc!r}，{wait}s后重试")
            await asyncio.sleep(wait)
    raise RuntimeError(f"接口最终失败：{last_error!r}")


def extract_store(store: Dict[str, Any]) -> Tuple[str, str]:
    sid = store.get("sid") or store.get("seller_id") or store.get("sellerId")
    name = (store.get("name") or store.get("store_name") or store.get("storeName")
            or store.get("seller_name") or store.get("sellerName"))
    return str(sid or "").strip(), str(name or "").strip()


def choose_sid_item(items: Any, sid: str) -> Dict[str, Any]:
    if isinstance(items, dict):
        return items
    if not isinstance(items, list):
        return {}
    valid = [x for x in items if isinstance(x, dict)]
    for item in valid:
        if str(item.get("sid", "")) == str(sid):
            return item
    return valid[0] if valid else {}


def as_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(Decimal(str(value)))
    except Exception:
        return None


def as_decimal(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        return str(Decimal(str(value)))
    except Exception:
        return None


def flatten(row: Dict[str, Any], report_month: date, period_start: date,
            period_end: date, sid: int, store_name: str) -> Optional[Dict[str, Any]]:
    price = choose_sid_item(row.get("price_list"), str(sid))
    asin_info = choose_sid_item(row.get("asins"), str(sid))
    parent_info = choose_sid_item(row.get("parent_asins"), str(sid))
    msku = price.get("seller_sku") or row.get("msku")
    if not msku:
        return None
    return {
        "report_month": report_month,
        "period_start": period_start,
        "period_end": period_end,
        "sid": sid,
        "store_name": store_name,
        "country": price.get("country"),
        "msku": str(msku),
        "sku": price.get("local_sku") or row.get("sku"),
        "asin": asin_info.get("asin") or row.get("asin"),
        "parent_asin": parent_info.get("parent_asin") or row.get("parent_asin"),
        "item_name": row.get("item_name"),
        "volume": as_int(row.get("volume")),
        "order_items": as_int(row.get("order_items")),
        "amount": as_decimal(row.get("amount")),
        "promotion_volume": as_int(row.get("promotion_volume")),
        "promotion_order_items": as_int(row.get("promotion_order_items")),
        "promotion_amount": as_decimal(row.get("promotion_amount")),
        "clicks": as_int(row.get("clicks")),
        "sessions_total": as_int(row.get("sessions_total")),
        "page_views_total": as_int(row.get("page_views_total")),
        "ad_order_quantity": as_int(row.get("ad_order_quantity")),
        "ad_direct_order_quantity": as_int(row.get("ad_direct_order_quantity")),
        "ad_sales_amount": as_decimal(row.get("ad_sales_amount")),
        "ad_direct_sales_amount": as_decimal(row.get("ad_direct_sales_amount")),
        "spend": as_decimal(row.get("spend")),
        "impressions": as_int(row.get("impressions")),
        "currency_code": row.get("currency_code"),
        "raw_json": json.dumps(row, ensure_ascii=False, default=str),
    }


def connect_db() -> pymysql.connections.Connection:
    return pymysql.connect(**DB_CONFIG)


def ensure_tables(conn: pymysql.connections.Connection) -> None:
    main_sql = f"""
    CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (
      `report_month` DATE NOT NULL,
      `period_start` DATE NOT NULL,
      `period_end` DATE NOT NULL,
      `sid` BIGINT NOT NULL,
      `store_name` VARCHAR(100) NOT NULL,
      `country` VARCHAR(64) NULL,
      `msku` VARCHAR(255) NOT NULL,
      `sku` VARCHAR(255) NULL,
      `asin` VARCHAR(64) NULL,
      `parent_asin` VARCHAR(64) NULL,
      `item_name` VARCHAR(1000) NULL,
      `volume` BIGINT NULL,
      `order_items` BIGINT NULL,
      `amount` DECIMAL(20,6) NULL,
      `promotion_volume` BIGINT NULL,
      `promotion_order_items` BIGINT NULL,
      `promotion_amount` DECIMAL(20,6) NULL,
      `clicks` BIGINT NULL,
      `sessions_total` BIGINT NULL,
      `page_views_total` BIGINT NULL,
      `ad_order_quantity` BIGINT NULL,
      `ad_direct_order_quantity` BIGINT NULL,
      `ad_sales_amount` DECIMAL(20,6) NULL,
      `ad_direct_sales_amount` DECIMAL(20,6) NULL,
      `spend` DECIMAL(20,6) NULL,
      `impressions` BIGINT NULL,
      `currency_code` VARCHAR(32) NULL,
      `raw_json` LONGTEXT NULL,
      `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      `modify_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      PRIMARY KEY (`report_month`,`sid`,`msku`),
      KEY `idx_store_month` (`store_name`,`report_month`),
      KEY `idx_asin` (`asin`),
      KEY `idx_ad_orders` (`report_month`,`store_name`,`ad_order_quantity`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
    """
    log_sql = f"""
    CREATE TABLE IF NOT EXISTS `{LOG_TABLE}` (
      `report_month` DATE NOT NULL,
      `sid` BIGINT NOT NULL,
      `store_name` VARCHAR(100) NOT NULL,
      `status` VARCHAR(20) NOT NULL,
      `interface_total` BIGINT NULL,
      `fetched_rows` BIGINT NULL,
      `written_rows` BIGINT NULL,
      `started_at` DATETIME NULL,
      `finished_at` DATETIME NULL,
      `error_message` TEXT NULL,
      PRIMARY KEY (`report_month`,`sid`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
    """
    with conn.cursor() as cur:
        cur.execute(main_sql)
        cur.execute(log_sql)
    conn.commit()


def already_success(conn: pymysql.connections.Connection, report_month: date, sid: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(f"SELECT status FROM `{LOG_TABLE}` WHERE report_month=%s AND sid=%s", (report_month, sid))
        row = cur.fetchone()
    return bool(row and row[0] == "SUCCESS")


def mark_log(conn: pymysql.connections.Connection, report_month: date, sid: int,
             store_name: str, status: str, interface_total: Optional[int] = None,
             fetched_rows: Optional[int] = None, written_rows: Optional[int] = None,
             error: Optional[str] = None) -> None:
    sql = f"""
    INSERT INTO `{LOG_TABLE}`
      (report_month,sid,store_name,status,interface_total,fetched_rows,written_rows,started_at,finished_at,error_message)
    VALUES (%s,%s,%s,%s,%s,%s,%s,IF(%s='RUNNING',NOW(),NULL),IF(%s IN ('SUCCESS','FAILED'),NOW(),NULL),%s)
    ON DUPLICATE KEY UPDATE
      store_name=VALUES(store_name), status=VALUES(status),
      interface_total=VALUES(interface_total), fetched_rows=VALUES(fetched_rows),
      written_rows=VALUES(written_rows),
      started_at=IF(VALUES(status)='RUNNING',NOW(),started_at),
      finished_at=IF(VALUES(status) IN ('SUCCESS','FAILED'),NOW(),NULL),
      error_message=VALUES(error_message)
    """
    with conn.cursor() as cur:
        cur.execute(sql, (report_month, sid, store_name, status, interface_total,
                          fetched_rows, written_rows, status, status, error))
    conn.commit()


COLUMNS = [
    "report_month","period_start","period_end","sid","store_name","country","msku","sku","asin",
    "parent_asin","item_name","volume","order_items","amount","promotion_volume",
    "promotion_order_items","promotion_amount","clicks","sessions_total","page_views_total",
    "ad_order_quantity","ad_direct_order_quantity","ad_sales_amount","ad_direct_sales_amount",
    "spend","impressions","currency_code","raw_json",
]


def replace_rows(conn: pymysql.connections.Connection, report_month: date, sid: int,
                 rows: List[Dict[str, Any]]) -> int:
    placeholders = ",".join(["%s"] * len(COLUMNS))
    insert_sql = f"INSERT INTO `{TABLE_NAME}` ({','.join(f'`{c}`' for c in COLUMNS)}) VALUES ({placeholders})"
    values = [tuple(row.get(c) for c in COLUMNS) for row in rows]
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM `{TABLE_NAME}` WHERE report_month=%s AND sid=%s", (report_month, sid))
            for i in range(0, len(values), 1000):
                cur.executemany(insert_sql, values[i:i + 1000])
        conn.commit()
        return len(values)
    except Exception:
        conn.rollback()
        raise


async def fetch_store_month(op_api: OpenApiBase, token: str, session: aiohttp.ClientSession,
                            report_month: date, period_start: date, period_end: date,
                            sid: int, store_name: str, page_size: int) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    offset = 0
    total: Optional[int] = None
    by_msku: Dict[str, Dict[str, Any]] = {}
    page = 0
    while True:
        body = {
            "offset": offset,
            "length": page_size,
            "sort_field": "volume",
            "sort_type": "desc",
            "sid": str(sid),
            "start_date": period_start.isoformat(),
            "end_date": period_end.isoformat(),
            "summary_field": "msku",
            "currency_code": "USD",
            "is_recently_enum": False,
            "purchase_status": 0,
        }
        resp = await api_request(op_api, token, session, PERFORMANCE_ENDPOINT, "POST", body)
        raw_rows, api_total = rows_total(resp)
        if total is None:
            total = api_total
        page += 1
        for raw in raw_rows:
            item = flatten(raw, report_month, period_start, period_end, sid, store_name)
            if item:
                by_msku[item["msku"]] = item
        offset += len(raw_rows)
        log(f"{report_month:%Y-%m} {store_name} page={page} fetched={len(raw_rows)} accumulated={len(by_msku)} total={total}")
        if not raw_rows or len(raw_rows) < page_size or (total is not None and offset >= total):
            break
        await asyncio.sleep(REQUEST_INTERVAL)
    return list(by_msku.values()), total


async def main() -> None:
    parser = argparse.ArgumentParser(description="领星产品表现月度MSKU历史回补")
    parser.add_argument("--start-month", default="2025-01")
    parser.add_argument("--end-month", default="2025-01")
    parser.add_argument("--stores", default="JQ-US")
    parser.add_argument("--page-size", type=int, default=PAGE_SIZE)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    require_env()
    if not 1 <= args.page_size <= 10000:
        raise ValueError("page-size 必须为1~10000")
    targets = [x.strip() for x in args.stores.split(",") if x.strip()]

    timeout = aiohttp.ClientTimeout(total=180)
    connector = aiohttp.TCPConnector(ssl=False, limit_per_host=5)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector, trust_env=True) as session:
        if PROXY_URL:
            os.environ["HTTP_PROXY"] = PROXY_URL
            os.environ["HTTPS_PROXY"] = PROXY_URL
        op_api = OpenApiBase(BASE_URL, APP_ID, APP_SECRET, session=session)
        token_resp = await op_api.generate_access_token()
        token = token_value(token_resp, "access_token", "accessToken")
        if not token:
            raise RuntimeError(f"获取Token失败：{token_resp!r}")

        store_resp = await api_request(op_api, str(token), session, STORE_LIST_ENDPOINT, "GET")
        store_rows, _ = rows_total(store_resp)
        available: Dict[str, Tuple[int, str]] = {}
        for raw in store_rows:
            sid_text, name = extract_store(raw)
            if sid_text and name:
                available[name.upper()] = (int(sid_text), name)
        missing = [name for name in targets if name.upper() not in available]
        if missing:
            raise RuntimeError(f"店铺不存在：{missing}；接口店铺示例：{sorted(available)[:50]}")

        conn = connect_db()
        try:
            ensure_tables(conn)
            for start, end in iter_months(args.start_month, args.end_month):
                for target in targets:
                    sid, _ = available[target.upper()]
                    if not args.force and already_success(conn, start, sid):
                        log(f"[SKIP] {start:%Y-%m} {target} 已成功")
                        continue
                    mark_log(conn, start, sid, target, "RUNNING")
                    try:
                        rows, total = await fetch_store_month(op_api, str(token), session, start, start, end, sid, target, args.page_size)
                        written = replace_rows(conn, start, sid, rows)
                        mark_log(conn, start, sid, target, "SUCCESS", total, len(rows), written)
                        log(f"[DONE] {start:%Y-%m} {target} rows={written}")
                    except Exception as exc:
                        mark_log(conn, start, sid, target, "FAILED", error=repr(exc))
                        raise
                    await asyncio.sleep(REQUEST_INTERVAL)
        finally:
            conn.close()


if __name__ == "__main__":
    asyncio.run(main())
