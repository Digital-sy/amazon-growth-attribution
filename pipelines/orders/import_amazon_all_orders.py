#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将 Amazon All Orders Report TXT 流式导入 MySQL。"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import traceback
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_TABLE = "ods_amz_all_orders_report"
DEFAULT_BATCH_SIZE = 2000

HEADERS = [
    "amazon-order-id", "merchant-order-id", "purchase-date",
    "last-updated-date", "order-status", "fulfillment-channel",
    "sales-channel", "order-channel", "url", "ship-service-level",
    "product-name", "sku", "asin", "item-status", "quantity",
    "currency", "item-price", "item-tax", "shipping-price",
    "shipping-tax", "gift-wrap-price", "gift-wrap-tax",
    "item-promotion-discount", "ship-promotion-discount", "ship-city",
    "ship-state", "ship-postal-code", "ship-country", "promotion-ids",
    "cpf", "is-business-order", "purchase-order-number",
    "price-designation", "customized-url", "customized-page",
    "signature-confirmation-recommended", "buyer-identification-number",
    "buyer-identification-type", "order-item-id",
]

TARGET_COLUMNS = [
    "store_name", "report_month", "source_file", "source_file_sha256",
    "source_row_no", "import_batch_no", "amazon_order_id",
    "merchant_order_id", "purchase_date_raw", "purchase_date_utc",
    "last_updated_date_raw", "last_updated_date_utc", "order_status",
    "fulfillment_channel", "sales_channel", "order_channel", "url",
    "ship_service_level", "product_name", "sku", "asin", "item_status",
    "quantity", "currency", "item_price", "item_tax", "shipping_price",
    "shipping_tax", "gift_wrap_price", "gift_wrap_tax",
    "item_promotion_discount", "ship_promotion_discount", "ship_city",
    "ship_state", "ship_postal_code", "ship_country", "promotion_ids",
    "cpf", "is_business_order", "purchase_order_number",
    "price_designation", "customized_url", "customized_page",
    "signature_confirmation_recommended", "buyer_identification_number",
    "buyer_identification_type", "order_item_id",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导入 Amazon 所有订单报告 TXT")
    parser.add_argument("--input", required=True, help="TXT文件或目录")
    parser.add_argument("--store-name", help="单文件店铺名，例如 SY-US")
    parser.add_argument(
        "--store-map",
        default=str(PROJECT_DIR / "store_map.example.json"),
        help="文件名前缀与店铺映射JSON",
    )
    parser.add_argument("--table", default=DEFAULT_TABLE)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--create-table", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--recursive",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser.parse_args()


def validate_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError(f"非法MySQL标识符：{value!r}")
    return value


def text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text != "" else None


def parse_int(value: Any) -> int | None:
    text = text_or_none(value)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise ValueError(f"无法转换为整数：{value!r}") from exc


def parse_decimal(value: Any) -> Decimal | None:
    text = text_or_none(value)
    if text is None:
        return None
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"无法转换为金额：{value!r}") from exc


def parse_bool(value: Any) -> int | None:
    text = text_or_none(value)
    if text is None:
        return None
    normalized = text.strip().lower()
    if normalized in {"true", "1", "yes", "y"}:
        return 1
    if normalized in {"false", "0", "no", "n"}:
        return 0
    raise ValueError(f"无法转换为布尔值：{value!r}")


def parse_utc(value: Any) -> datetime | None:
    text = text_or_none(value)
    if text is None:
        return None
    normalized = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"无法解析时间：{value!r}") from exc
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        while chunk := file_obj.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_store_map(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("store map必须是JSON对象")
    return {
        str(prefix).strip().upper(): str(store).strip()
        for prefix, store in data.items()
        if str(prefix).strip() and str(store).strip()
    }


def infer_store(path: Path, store_map: dict[str, str]) -> str:
    for candidate in (path.stem.upper(), path.parent.name.upper()):
        for prefix in sorted(store_map, key=len, reverse=True):
            if candidate.startswith(prefix):
                return store_map[prefix]
    raise ValueError(
        f"无法识别店铺：{path.name}；请使用 --store-name 或修改store map"
    )


def infer_month(path: Path) -> date | None:
    text = path.stem
    match = re.search(r"(20\d{2})[-_]?((?:0[1-9])|(?:1[0-2]))", text)
    if match:
        return date(int(match.group(1)), int(match.group(2)), 1)
    match = re.search(r"(?<!\d)(\d{2})((?:0[1-9])|(?:1[0-2]))(?!\d)", text)
    if match:
        return date(2000 + int(match.group(1)), int(match.group(2)), 1)
    return None


def discover_files(input_path: Path, recursive: bool) -> list[Path]:
    if input_path.is_file():
        if input_path.suffix.lower() != ".txt":
            raise ValueError(f"不是TXT文件：{input_path}")
        return [input_path]
    if not input_path.is_dir():
        raise FileNotFoundError(input_path)
    pattern = "**/*.txt" if recursive else "*.txt"
    return sorted(path for path in input_path.glob(pattern) if path.is_file())


def iter_rows(path: Path) -> Iterator[tuple[int, dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file_obj:
        reader = csv.reader(file_obj, delimiter="\t")
        try:
            header = [value.replace("\ufeff", "").strip() for value in next(reader)]
        except StopIteration as exc:
            raise ValueError(f"空文件：{path}") from exc
        if header != HEADERS:
            missing = [field for field in HEADERS if field not in header]
            extra = [field for field in header if field not in HEADERS]
            raise ValueError(
                f"{path.name}表头异常，缺少={missing}，额外={extra}，"
                f"实际列数={len(header)}"
            )
        for row_no, values in enumerate(reader, start=2):
            if len(values) != len(header):
                raise ValueError(
                    f"{path.name}第{row_no}行列数异常："
                    f"实际{len(values)}，预期{len(header)}"
                )
            yield row_no, dict(zip(header, values))


def transform(
    raw: dict[str, str],
    *,
    store: str,
    month: date | None,
    source_file: str,
    source_hash: str,
    row_no: int,
    batch_no: str,
) -> tuple[Any, ...]:
    order_id = (raw.get("amazon-order-id") or "").strip()
    if not order_id:
        raise ValueError("amazon-order-id为空")
    return (
        store, month, source_file, source_hash, row_no, batch_no,
        order_id, text_or_none(raw.get("merchant-order-id")),
        text_or_none(raw.get("purchase-date")), parse_utc(raw.get("purchase-date")),
        text_or_none(raw.get("last-updated-date")),
        parse_utc(raw.get("last-updated-date")),
        text_or_none(raw.get("order-status")),
        text_or_none(raw.get("fulfillment-channel")),
        text_or_none(raw.get("sales-channel")),
        text_or_none(raw.get("order-channel")), text_or_none(raw.get("url")),
        text_or_none(raw.get("ship-service-level")),
        text_or_none(raw.get("product-name")), text_or_none(raw.get("sku")),
        text_or_none(raw.get("asin")), text_or_none(raw.get("item-status")),
        parse_int(raw.get("quantity")), text_or_none(raw.get("currency")),
        parse_decimal(raw.get("item-price")), parse_decimal(raw.get("item-tax")),
        parse_decimal(raw.get("shipping-price")),
        parse_decimal(raw.get("shipping-tax")),
        parse_decimal(raw.get("gift-wrap-price")),
        parse_decimal(raw.get("gift-wrap-tax")),
        parse_decimal(raw.get("item-promotion-discount")),
        parse_decimal(raw.get("ship-promotion-discount")),
        text_or_none(raw.get("ship-city")), text_or_none(raw.get("ship-state")),
        text_or_none(raw.get("ship-postal-code")),
        text_or_none(raw.get("ship-country")),
        text_or_none(raw.get("promotion-ids")), text_or_none(raw.get("cpf")),
        parse_bool(raw.get("is-business-order")),
        text_or_none(raw.get("purchase-order-number")),
        text_or_none(raw.get("price-designation")),
        text_or_none(raw.get("customized-url")),
        text_or_none(raw.get("customized-page")),
        parse_bool(raw.get("signature-confirmation-recommended")),
        text_or_none(raw.get("buyer-identification-number")),
        text_or_none(raw.get("buyer-identification-type")),
        (raw.get("order-item-id") or "").strip(),
    )


def db_connection():
    try:
        import pymysql
    except ImportError as exc:
        raise RuntimeError("请先执行 pip install -r requirements.txt") from exc
    load_dotenv(PROJECT_DIR.parent.parent / ".env")
    required = ["DB_HOST", "DB_USER", "DB_PASSWORD", "DB_DATABASE"]
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise RuntimeError(f".env缺少配置：{missing}")
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_DATABASE"],
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "15")),
        read_timeout=int(os.getenv("DB_READ_TIMEOUT", "120")),
        write_timeout=int(os.getenv("DB_WRITE_TIMEOUT", "120")),
    )


def execute_sql_file(connection, path: Path, table: str) -> None:
    sql_text = path.read_text(encoding="utf-8").replace("{{TABLE_NAME}}", table)
    statements = [statement.strip() for statement in sql_text.split(";") if statement.strip()]
    with connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
    connection.commit()


def upsert_sql(table: str) -> str:
    columns = ", ".join(f"`{column}`" for column in TARGET_COLUMNS)
    placeholders = ", ".join(["%s"] * len(TARGET_COLUMNS))
    immutable = {"store_name", "amazon_order_id", "order_item_id"}
    updates = ", ".join(
        f"`{column}`=VALUES(`{column}`)"
        for column in TARGET_COLUMNS
        if column not in immutable
    )
    return (
        f"INSERT INTO `{table}` ({columns}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {updates}"
    )


def imported(connection, table: str, store: str, source_hash: str) -> bool:
    sql = """
        SELECT 1 FROM etl_file_import_log
        WHERE target_table=%s AND store_name=%s
          AND source_file_sha256=%s AND status='success'
        LIMIT 1
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, (table, store, source_hash))
        return cursor.fetchone() is not None


def start_log(
    connection,
    *,
    table: str,
    store: str,
    month: date | None,
    path: Path,
    source_hash: str,
    batch_no: str,
) -> None:
    sql = """
        INSERT INTO etl_file_import_log (
            target_table, store_name, report_month, source_file,
            source_file_sha256, file_size_bytes, import_batch_no, status,
            started_at, finished_at, total_rows, processed_rows,
            inserted_or_updated_rows, error_message
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,'running',NOW(),NULL,0,0,0,NULL)
        ON DUPLICATE KEY UPDATE
            report_month=VALUES(report_month), source_file=VALUES(source_file),
            file_size_bytes=VALUES(file_size_bytes),
            import_batch_no=VALUES(import_batch_no), status='running',
            started_at=NOW(), finished_at=NULL, total_rows=0,
            processed_rows=0, inserted_or_updated_rows=0, error_message=NULL
    """
    with connection.cursor() as cursor:
        cursor.execute(
            sql,
            (table, store, month, path.name, source_hash, path.stat().st_size, batch_no),
        )
    connection.commit()


def finish_log(
    connection,
    *,
    table: str,
    store: str,
    source_hash: str,
    status: str,
    rows: int,
    affected: int,
    error: str | None,
) -> None:
    sql = """
        UPDATE etl_file_import_log
        SET status=%s, total_rows=%s, processed_rows=%s,
            inserted_or_updated_rows=%s, error_message=%s, finished_at=NOW()
        WHERE target_table=%s AND store_name=%s AND source_file_sha256=%s
    """
    with connection.cursor() as cursor:
        cursor.execute(
            sql,
            (status, rows, rows, affected, error, table, store, source_hash),
        )
    connection.commit()


def write_batch(connection, sql: str, rows: list[tuple[Any, ...]], retries: int) -> int:
    import pymysql
    retryable = {1205, 1213, 2002, 2003, 2006, 2013, 2055}
    for attempt in range(retries + 1):
        try:
            connection.ping(reconnect=True)
            with connection.cursor() as cursor:
                affected = cursor.executemany(sql, rows)
            connection.commit()
            return max(0, int(affected or 0))
        except pymysql.err.OperationalError as exc:
            connection.rollback()
            code = int(exc.args[0]) if exc.args else 0
            if code not in retryable or attempt >= retries:
                raise
            delay = min(60, 2 ** attempt)
            print(f"[RETRY] MySQL code={code}，{delay}s后重试")
            time.sleep(delay)
    raise RuntimeError("批量写入失败")


def validate_file(path: Path, store: str, month: date | None) -> int:
    count = 0
    for row_no, raw in iter_rows(path):
        transform(
            raw,
            store=store,
            month=month,
            source_file=path.name,
            source_hash="0" * 64,
            row_no=row_no,
            batch_no="VALIDATE_ONLY",
        )
        count += 1
    print(f"[VALID] {path.name} | {store} | {month} | {count:,} rows")
    return count


def import_file(
    connection,
    *,
    path: Path,
    store: str,
    month: date | None,
    table: str,
    batch_size: int,
    retries: int,
    force: bool,
) -> None:
    source_hash = file_sha256(path)
    if not force and imported(connection, table, store, source_hash):
        print(f"[SKIP] {path.name} 已成功导入")
        return
    batch_no = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    start_log(
        connection,
        table=table,
        store=store,
        month=month,
        path=path,
        source_hash=source_hash,
        batch_no=batch_no,
    )
    sql = upsert_sql(table)
    processed = 0
    affected = 0
    batch: list[tuple[Any, ...]] = []
    started = time.time()
    try:
        for row_no, raw in iter_rows(path):
            batch.append(
                transform(
                    raw,
                    store=store,
                    month=month,
                    source_file=path.name,
                    source_hash=source_hash,
                    row_no=row_no,
                    batch_no=batch_no,
                )
            )
            if len(batch) >= batch_size:
                affected += write_batch(connection, sql, batch, retries)
                processed += len(batch)
                batch.clear()
                if processed % (batch_size * 10) == 0:
                    speed = processed / max(time.time() - started, 0.001)
                    print(f"[RUN] {path.name} processed={processed:,} speed={speed:,.0f}/s")
        if batch:
            affected += write_batch(connection, sql, batch, retries)
            processed += len(batch)
        finish_log(
            connection,
            table=table,
            store=store,
            source_hash=source_hash,
            status="success",
            rows=processed,
            affected=affected,
            error=None,
        )
        print(f"[DONE] {path.name} rows={processed:,}")
    except Exception as exc:
        connection.rollback()
        error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"[:16000]
        finish_log(
            connection,
            table=table,
            store=store,
            source_hash=source_hash,
            status="failed",
            rows=processed,
            affected=affected,
            error=error,
        )
        raise


def main() -> None:
    args = parse_args()
    table = validate_identifier(args.table)
    input_path = Path(args.input).expanduser().resolve()
    files = discover_files(input_path, args.recursive)
    if not files:
        raise RuntimeError(f"没有找到TXT文件：{input_path}")
    store_map = load_store_map(Path(args.store_map).expanduser().resolve())
    tasks: list[tuple[Path, str, date | None]] = []
    for path in files:
        if args.store_name:
            if len(files) != 1:
                raise ValueError("--store-name只能用于单文件")
            store = args.store_name.strip()
        else:
            store = infer_store(path, store_map)
        tasks.append((path, store, infer_month(path)))

    if args.validate_only:
        total = sum(validate_file(path, store, month) for path, store, month in tasks)
        print(f"全部文件校验通过：{total:,}行")
        return

    connection = db_connection()
    try:
        if args.create_table:
            execute_sql_file(connection, PROJECT_DIR / "schema.sql", table)
            execute_sql_file(connection, PROJECT_DIR / "import_log.sql", table)
        for index, (path, store, month) in enumerate(tasks, start=1):
            print(f"===== {index}/{len(tasks)} {path.name} | {store} | {month} =====")
            import_file(
                connection,
                path=path,
                store=store,
                month=month,
                table=table,
                batch_size=max(1, args.batch_size),
                retries=max(0, args.max_retries),
                force=args.force,
            )
    finally:
        connection.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("用户中断执行", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"执行失败：{exc}", file=sys.stderr)
        sys.exit(1)
